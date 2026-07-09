"""Shared prelude (Phase 2a): build ONE encounter stitching + union-scope respiratory_support waterfall
that proning/sat/sbt filter from, instead of each building its own.

`ensure_shared(co, tz, site, waterfall_version=...)`:
  1. loads patient/hospitalization/adt and stitches encounters ONCE (6 h interval) → `_shared/`
     (`encounter_mapping.parquet` + `hosp_stitched`/`adt_stitched`), matching each vertical's
     `stitch_cached` (all three used `stitch_time_interval = 6`);
  2. computes the UNION waterfall scope = ABG-having hosp-ids (labs `po2_arterial`, proning's scope)
     ∪ all-ICU-block hosp-ids (adt `location_category == "icu"`, sbt's scope; SAT's ICU∩sedation ⊆ this);
  3. builds the clifpy waterfall over that union via `common.resp_support.build_waterfall`, cached in
     `_shared/` (scope-keyed filename, so a changed union auto-rebuilds — no fixed-name/version-only cache).

Returns `(waterfall_df, encounter_mapping, hosp_stitched, adt_stitched)`. Each vertical then filters the
waterfall to its own hosp-id set and writes its stage-local `_cache/resp_waterfall.parquet` slice, so its
stages 02/03 stay unchanged.

Why a union-scope build is per-vertical-correct: clifpy fills/scaffolds each `hospitalization_id`
INDEPENDENTLY (Phase 2/3 group by id), so a union build filtered to a vertical's ids yields the same
per-hosp rows its own build would — EXCEPT clifpy Phase 1's site-global most-frequent device/mode labels
(`most_common_{imv,nippv,cmv}_name`) are computed over the whole input; widening scope can in principle
shift those labels on heuristic-imputed rows. At a single site these are stable, so acceptance is an
*explained-diff* (expect ~0), not byte-identical — same standard as Phase 1.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

_ABG_LAB_CATEGORY = "po2_arterial"     # proning's ABG-having scope key
_ICU_CATEGORY = "icu"                  # adt location_category for the all-ICU scope
_STITCH_INTERVAL_H = 6                 # matches every vertical's stitch_cached


def _load_small_tables(co) -> None:
    for t in ("patient", "hospitalization", "adt"):
        if getattr(co, t, None) is None:
            co.load_table(t)


def _stitch(co, shared: Path, verbose: bool):
    """Stitch encounters once (or read the cached shared stitch). Returns (hosp_s, adt_s, mapping).

    Always loads patient/hospitalization/adt onto `co` first — the verticals rely on `co.patient.df`
    (demographics) etc. even when the stitch itself is a cache hit."""
    _load_small_tables(co)
    map_path = shared / "encounter_mapping.parquet"
    hosp_path = shared / "hosp_stitched.parquet"
    adt_path = shared / "adt_stitched.parquet"
    if map_path.exists() and hosp_path.exists() and adt_path.exists():
        if verbose:
            print("[build_shared] cache hit: shared encounter stitching")
        hosp_s = pd.read_parquet(hosp_path)
        adt_s = pd.read_parquet(adt_path)
        mapping = pd.read_parquet(map_path)
        co.hospitalization.df = hosp_s          # reattach the STITCHED hosp/adt (overwrites raw loads)
        co.adt.df = adt_s
        co.encounter_mapping = mapping
        return hosp_s, adt_s, mapping

    co.stitch_time_interval = _STITCH_INTERVAL_H
    co.run_stitch_encounters()
    mapping = co.encounter_mapping
    if mapping is None:
        raise RuntimeError("encounter stitching did not produce a mapping")
    hosp_s = co.hospitalization.df
    adt_s = co.adt.df
    for df in (hosp_s, adt_s, mapping):
        if "hospitalization_id" in df.columns:
            df["hospitalization_id"] = df["hospitalization_id"].astype(str)
    hosp_s.to_parquet(hosp_path, index=False)
    adt_s.to_parquet(adt_path, index=False)
    mapping.to_parquet(map_path, index=False)
    if verbose:
        print(f"[build_shared] stitched {mapping['hospitalization_id'].nunique():,} hosps "
              f"-> {mapping['encounter_block'].nunique():,} encounter_blocks")
    return hosp_s, adt_s, mapping


def _abg_having_ids(co) -> list[str]:
    """Hospitalizations with an arterial PaO2 (proning's ABG-having waterfall scope)."""
    if getattr(co, "labs", None) is None:
        co.load_table("labs", filters={"lab_category": [_ABG_LAB_CATEGORY]})
    df = co.labs.df
    if df is None or "hospitalization_id" not in df.columns:
        return []
    return df["hospitalization_id"].dropna().astype(str).unique().tolist()


def _all_icu_ids(adt_s: pd.DataFrame, mapping: pd.DataFrame) -> list[str]:
    """Hospitalizations in any all-ICU encounter_block (sbt's waterfall scope)."""
    # adt_s already carries an encounter_block from stitching; drop it and re-attach from the
    # authoritative mapping (mirrors each vertical's build_icu_intervals) to avoid a merge collision.
    a = adt_s.drop(columns=[c for c in ["encounter_block"] if c in adt_s.columns]).copy()
    a["hospitalization_id"] = a["hospitalization_id"].astype(str)
    a["location_category"] = a["location_category"].astype("string").str.strip().str.lower()
    m = mapping[["hospitalization_id", "encounter_block"]].copy()
    m["hospitalization_id"] = m["hospitalization_id"].astype(str)
    a = a.merge(m, on="hospitalization_id", how="left")
    icu_blocks = a.loc[a["location_category"] == _ICU_CATEGORY, "encounter_block"].dropna().unique()
    return m.loc[m["encounter_block"].isin(icu_blocks), "hospitalization_id"].unique().tolist()


def ensure_shared(co, tz: str, site: str, *, waterfall_version: str, data_version=None, verbose=True):
    """Build (once) the shared stitch + union-scope waterfall; reuse the scope-keyed cache when fresh.

    Returns (waterfall_df, encounter_mapping, hosp_stitched, adt_stitched).
    """
    import bundle_config as _bc
    from common.resp_support import build_waterfall

    shared = _bc.shared_cache_dir(site)                 # output/<site>/_shared/  (created by bundle_config)
    hosp_s, adt_s, mapping = _stitch(co, shared, verbose)

    # union scope: ABG-having ∪ all-ICU (cached so warm runs skip the labs/adt id derivation)
    scope_path = shared / "union_scope.json"
    if scope_path.exists():
        union = json.loads(scope_path.read_text())
        if verbose:
            print(f"[build_shared] cache hit: union scope ({len(union):,} hosps)")
    else:
        abg_ids = set(_abg_having_ids(co))
        icu_ids = set(_all_icu_ids(adt_s, mapping))
        union = sorted(abg_ids | icu_ids)
        scope_path.write_text(json.dumps(union))
        if verbose:
            print(f"[build_shared] union scope: {len(union):,} hosps "
                  f"(ABG-having={len(abg_ids):,} ∪ all-ICU={len(icu_ids):,}; "
                  f"overlap={len(abg_ids & icu_ids):,})")

    wf = build_waterfall(
        co.data_directory, co.filetype, tz, union, mapping,
        cache_dir=shared, waterfall_version=waterfall_version, data_version=data_version,
        verbose=verbose,           # cache_name=None -> scope-keyed filename (union change auto-rebuilds)
    )
    return wf, mapping, hosp_s, adt_s


# --------------------------------------------------------------------------- standalone validation
if __name__ == "__main__":
    import sys
    from pathlib import Path as _P
    _ROOT = _P(__file__).resolve().parents[1]
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    import bundle_config as _bc

    site = _bc.active_site()
    cfg = _bc.effective("sbt", site)                    # nested primary_dataset shape
    import clifpy
    ds = cfg["primary_dataset"]
    co = clifpy.ClifOrchestrator(
        data_directory=ds["data_path"], filetype=ds["file_format"],
        timezone=cfg["timezone"], output_directory=str(_bc.output_root(site)),
    )
    wf, mapping, hosp_s, adt_s = ensure_shared(
        co, cfg["timezone"], site, waterfall_version=_bc.WATERFALL_VERSION,
    )
    print(f"\n[VALIDATION] shared waterfall rows={len(wf):,} cols={len(wf.columns)} "
          f"hosps={wf['hospitalization_id'].nunique():,} "
          f"blocks={mapping['encounter_block'].nunique():,}")
    print(f"[VALIDATION] recorded_dttm dtype={wf['recorded_dttm'].dtype}  "
          f"nulls={int(wf['recorded_dttm'].isna().sum())}")
