"""Build the ventilated-ICU patient-DAY universe for the SAT QI vertical.

Unit of analysis = one row per (encounter_block, ICU calendar day in US/Central)
on which the patient was on invasive mechanical ventilation (IMV) AND in an ICU
location. This is the SAT denominator universe before the sedation/eligibility
filter (applied in 02).

Pipeline:
    patient/hospitalization/adt   -> stitch encounters (cached)
    medication_admin_continuous   -> SAT-relevant + dex + paralytic infusions (cached)
    respiratory_support           -> waterfall (cached), scoped to ICU + sedation hosps
    waterfall                     -> IMV intervals (consecutive-row segmentation)
    IMV  ∩  ICU adt intervals     -> ventilated-ICU intervals (+ location_type unit)
    intervals                     -> expand to calendar days -> cohort.parquet

Loader / waterfall / range-join machinery is ADAPTED from the proning sibling
(/CLIF/proning/code/01_build_cohort.py) so this project stays self-contained for
federation. No raw PHI is printed — only aggregate counts.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import timedelta
from pathlib import Path

import duckdb
import pandas as pd

import clifpy

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BUNDLE_ROOT = Path(__file__).resolve().parents[3]        # repo root (holds bundle_config.py)
import sys as _sys
if str(_BUNDLE_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_BUNDLE_ROOT))
import bundle_config as _bc                                # multi-site config + output resolver
_METRIC, _SITE = "sat", _bc.active_site()
CONFIG_PATH = PROJECT_ROOT / "config.json"                 # legacy path (load_config now uses bundle_config)
OUTPUT_DIR = _bc.metric_output_dir(_METRIC, _SITE)         # output/<site>/metrics/sat
INTERMEDIATE_DIR = OUTPUT_DIR / "intermediate"
CACHE_DIR = INTERMEDIATE_DIR / "_cache"
FINAL_DIR = OUTPUT_DIR / "final"
LOGS_DIR = OUTPUT_DIR / "logs"

IMV_CATEGORY = "imv"            # UChicago stores device_category lowercase
ICU_CATEGORY = "icu"
# Trailing IMV segment (last waterfall record of a block) is capped to this many
# hours so a final record can't extend ventilation indefinitely. The ICU
# intersection trims it further.
TRAILING_IMV_CAP_H = 24

log = logging.getLogger("sat.cohort")


# ---------------------------------------------------------------------------
# Dirs / config / orchestrator  (adapted from proning)
# ---------------------------------------------------------------------------
def _ensure_dirs() -> None:
    for d in (INTERMEDIATE_DIR, CACHE_DIR, FINAL_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def cpath(name: str) -> Path:
    return CACHE_DIR / f"{name}.parquet"


def load_config(path: Path = CONFIG_PATH) -> dict:
    return _bc.effective(_METRIC, _SITE)


def build_orchestrator(cfg: dict) -> clifpy.ClifOrchestrator:
    ds = cfg["primary_dataset"]
    _ensure_dirs()
    return clifpy.ClifOrchestrator(
        data_directory=ds["data_path"],
        filetype=ds["file_format"],
        timezone=cfg["timezone"],
        output_directory=str(OUTPUT_DIR),
    )


def _coerce_dttm(series: pd.Series, tz: str) -> pd.Series:
    """Normalize a datetime column to tz-aware ``datetime64[us, tz]``.
    Cached parquets and waterfall scaffold rows can demote to object dtype."""
    s = pd.to_datetime(series, errors="coerce")
    if getattr(s.dt, "tz", None) is None:
        s = s.dt.tz_localize(tz, ambiguous="NaT", nonexistent="shift_forward")
    else:
        s = s.dt.tz_convert(tz)
    return s


def sat_med_sets(cfg: dict) -> dict:
    """Lowercased med_category sets from config (case-insensitive matching)."""
    m = cfg["sat_medications"]
    return {
        "sat_relevant": {c.lower() for c in m["sedative_analgesic_categories"]},
        "dex": {c.lower() for c in m["dexmedetomidine_categories"]},
        "paralytic": {c.lower() for c in m["paralytic_categories"]},
    }


# ---------------------------------------------------------------------------
# Loading + stitching  (adapted from proning)
# ---------------------------------------------------------------------------
def load_small_tables(co: clifpy.ClifOrchestrator) -> None:
    for t in ("patient", "hospitalization", "adt"):
        co.load_table(t)
        df = getattr(co, t).df
        log.info("loaded %s: %d rows", t, 0 if df is None else len(df))


def stitch_cached(co: clifpy.ClifOrchestrator):
    if cpath("encounter_mapping").exists():
        log.info("cache hit: stitched hosp/adt/mapping")
        hosp_s = pd.read_parquet(cpath("hosp_stitched"))
        adt_s = pd.read_parquet(cpath("adt_stitched"))
        mapping = pd.read_parquet(cpath("encounter_mapping"))
        if co.hospitalization is not None:
            co.hospitalization.df = hosp_s
        if co.adt is not None:
            co.adt.df = adt_s
        co.encounter_mapping = mapping
        return hosp_s, adt_s, mapping
    co.stitch_time_interval = 6
    co.run_stitch_encounters()
    mapping = co.encounter_mapping
    if mapping is None:
        raise RuntimeError("encounter stitching did not produce a mapping")
    hosp_s = co.hospitalization.df
    adt_s = co.adt.df
    for df in (hosp_s, adt_s, mapping):
        if "hospitalization_id" in df.columns:
            df["hospitalization_id"] = df["hospitalization_id"].astype(str)
    hosp_s.to_parquet(cpath("hosp_stitched"), index=False)
    adt_s.to_parquet(cpath("adt_stitched"), index=False)
    mapping.to_parquet(cpath("encounter_mapping"), index=False)
    log.info("wrote cache: stitched %d hospitalizations -> %d encounter_blocks",
             mapping["hospitalization_id"].nunique(), mapping["encounter_block"].nunique())
    return hosp_s, adt_s, mapping


def load_infusions_cached(co: clifpy.ClifOrchestrator, mapping: pd.DataFrame,
                          med_sets: dict, tz: str) -> pd.DataFrame:
    """Load the SAT-relevant + dex + paralytic continuous infusions, attach
    encounter_block, normalize dttm/category, and cache."""
    if cpath("infusions").exists():
        log.info("cache hit: infusions")
        inf = pd.read_parquet(cpath("infusions"))
        inf["admin_dttm"] = _coerce_dttm(inf["admin_dttm"], tz)
        return inf

    all_cats = sorted(med_sets["sat_relevant"] | med_sets["dex"] | med_sets["paralytic"])
    co.load_table("medication_admin_continuous", filters={"med_category": all_cats})
    df = co.medication_admin_continuous.df
    log.info("loaded medication_admin_continuous (filtered): %d rows", len(df))

    keep = ["hospitalization_id", "admin_dttm", "med_category", "med_dose",
            "med_dose_unit", "mar_action_category"]
    keep = [c for c in keep if c in df.columns]
    inf = df[keep].copy()
    inf["hospitalization_id"] = inf["hospitalization_id"].astype(str)
    inf["med_category"] = inf["med_category"].astype("string").str.strip().str.lower()
    if "mar_action_category" in inf.columns:
        inf["mar_action_category"] = inf["mar_action_category"].astype("string").str.strip().str.lower()
    inf = inf.merge(mapping[["hospitalization_id", "encounter_block"]].astype({"hospitalization_id": str}),
                    on="hospitalization_id", how="left")
    inf = inf.dropna(subset=["encounter_block", "admin_dttm"])
    inf.to_parquet(cpath("infusions"), index=False)
    log.info("wrote cache: infusions (%d rows, %d encounter_blocks)",
             len(inf), inf["encounter_block"].nunique())
    inf["admin_dttm"] = _coerce_dttm(inf["admin_dttm"], tz)
    return inf


# ---------------------------------------------------------------------------
# Respiratory waterfall (cached — the expensive step), scoped to cohort hosps
# ---------------------------------------------------------------------------
def _normalize_waterfall(wf: pd.DataFrame, tz: str) -> pd.DataFrame:
    wf = wf.copy()
    wf["recorded_dttm"] = _coerce_dttm(wf["recorded_dttm"], tz)
    for col in ("device_category", "mode_category"):
        if col in wf.columns:
            wf[col] = wf[col].astype("string").str.strip().str.lower()
    return wf


def waterfall_cached(wf_shared: pd.DataFrame, scope_hosp_ids: list[str], tz: str) -> pd.DataFrame:
    """Filter the shared union waterfall (common.build_shared.ensure_shared) to SAT's ICU∩sedation
    scope, write the stage-local slice to CACHE_DIR/resp_waterfall.parquet (the path stage 03 reads),
    and return it normalized. Cleaning already happened PRE-waterfall inside load_clean (the shared
    build); `_normalize_waterfall` is idempotent and kept so stage 03 (which re-reads + re-applies it)
    is unchanged. The filtered slice is byte-identical to SAT's old per-vertical build (verified 0-diff
    on content columns at consolidation)."""
    scope = set(map(str, scope_hosp_ids))
    wf = wf_shared[wf_shared["hospitalization_id"].astype(str).isin(scope)].copy()
    # The shared waterfall carries encounter_block as float64 (int block-id + NaN from the left-merge);
    # SAT keys ICU intervals on a clean int-string encounter_block ("110135"), so cast to match — else
    # the str-vs-float imv∩icu join silently yields zero.
    wf["encounter_block"] = wf["encounter_block"].astype("Int64").astype(str)
    log.info("waterfall: filtered shared union to %d hospitalizations (ICU + sedation; %d rows)",
             len(scope), len(wf))
    wf.to_parquet(cpath("resp_waterfall"), index=False)
    return _normalize_waterfall(wf, tz)


# ---------------------------------------------------------------------------
# IMV intervals  ∩  ICU intervals  ->  ventilated-ICU intervals
# ---------------------------------------------------------------------------
def build_imv_intervals(wf: pd.DataFrame) -> pd.DataFrame:
    """Consecutive-row segmentation of the waterfall device timeline. Each
    record holds until the next record; keep segments where device==imv. The
    trailing record of a block is capped at TRAILING_IMV_CAP_H."""
    w = wf.dropna(subset=["encounter_block", "recorded_dttm"]).copy()
    w["encounter_block"] = w["encounter_block"].astype(str)
    # DETERMINISM: a stitched block can carry two rows at the same recorded_dttm (overlapping
    # hospitalizations, ~0.1% of rows); the shift(-1) segment then depends on their order. Add content
    # tie-breaks so the surviving segment (and thus imv presence at that instant) is order-invariant.
    _tb = [c for c in ["device_category", "mode_category", "fio2_set", "peep_set"] if c in w.columns]
    w = w.sort_values(["encounter_block", "recorded_dttm"] + _tb, na_position="last", kind="stable")
    w["seg_end"] = w.groupby("encounter_block")["recorded_dttm"].shift(-1)
    cap = w["recorded_dttm"] + timedelta(hours=TRAILING_IMV_CAP_H)
    w["seg_end"] = w["seg_end"].fillna(cap)
    imv = w.loc[w["device_category"] == IMV_CATEGORY,
                ["encounter_block", "recorded_dttm", "seg_end"]].copy()
    imv = imv.rename(columns={"recorded_dttm": "seg_start"})
    imv = imv[imv["seg_end"] > imv["seg_start"]]
    log.info("IMV segments: %d (across %d encounter_blocks)",
             len(imv), imv["encounter_block"].nunique())
    return imv


def build_icu_intervals(adt_s: pd.DataFrame, mapping: pd.DataFrame, tz: str) -> pd.DataFrame:
    # clifpy's stitched-adt carries its OWN (non-canonical) encounter_block — we
    # re-derive the canonical block id from `mapping` via hospitalization_id.
    a = adt_s.drop(columns=[c for c in ["encounter_block"] if c in adt_s.columns]).copy()
    a["hospitalization_id"] = a["hospitalization_id"].astype(str)
    a = a.merge(mapping[["hospitalization_id", "encounter_block"]], on="hospitalization_id", how="left")
    a["location_category"] = a["location_category"].astype("string").str.strip().str.lower()
    icu = a.loc[a["location_category"] == ICU_CATEGORY,
                ["encounter_block", "in_dttm", "out_dttm", "location_type", "location_name"]].copy()
    icu["encounter_block"] = icu["encounter_block"].astype(str)
    icu = icu[icu["encounter_block"].notna() & (icu["encounter_block"] != "nan")]
    icu["in_dttm"] = _coerce_dttm(icu["in_dttm"], tz)
    icu["out_dttm"] = _coerce_dttm(icu["out_dttm"], tz)
    icu["location_type"] = icu["location_type"].astype("string").str.strip().str.lower()
    # location_name = the specific physical unit (finer than location_type); carried alongside so
    # each patient-day can ALSO be attributed to its specific unit (nested within the chosen type).
    # Keep RAW case (unlike location_type) so the unit code matches the other verticals' feeds —
    # the scorecard unions name keys across feeds, so casing must agree (LPV emits raw e.g. "N09S").
    icu["location_name"] = icu["location_name"].astype("string").str.strip()
    icu["location_name"] = icu["location_name"].fillna("unknown").replace("", "unknown")
    icu = icu.dropna(subset=["in_dttm", "out_dttm"])
    icu = icu[icu["out_dttm"] > icu["in_dttm"]]
    return icu


def intersect_imv_icu(imv: pd.DataFrame, icu: pd.DataFrame) -> pd.DataFrame:
    """Range-join IMV segments with ICU intervals -> ventilated-ICU sub-intervals
    carrying the ICU location_type (unit)."""
    con = duckdb.connect()
    con.register("imv", imv)
    con.register("icu", icu)
    joined = con.execute(
        """
        SELECT imv.encounter_block AS encounter_block,
               greatest(imv.seg_start, icu.in_dttm)  AS vstart,
               least(imv.seg_end,  icu.out_dttm)      AS vend,
               icu.location_type                       AS unit,
               icu.location_name                       AS unit_name
        FROM imv JOIN icu
          ON imv.encounter_block = icu.encounter_block
         AND imv.seg_start < icu.out_dttm
         AND imv.seg_end   > icu.in_dttm
        """
    ).fetchdf()
    con.close()
    joined = joined[joined["vend"] > joined["vstart"]].reset_index(drop=True)
    log.info("ventilated-ICU sub-intervals: %d (across %d encounter_blocks)",
             len(joined), joined["encounter_block"].nunique())
    return joined


# ---------------------------------------------------------------------------
# Expand ventilated-ICU intervals to calendar days (US/Central)
# ---------------------------------------------------------------------------
def expand_to_days(vint: pd.DataFrame, tz: str) -> pd.DataFrame:
    """One row per (encounter_block, local calendar day). Per day we keep the
    unit with the most ventilated-ICU overlap and the total vented-ICU minutes
    and day window bounds."""
    v = vint.copy()
    v["vstart"] = _coerce_dttm(v["vstart"], tz)
    v["vend"] = _coerce_dttm(v["vend"], tz)

    recs = []
    one_day = timedelta(days=1)
    for r in v.itertuples(index=False):
        d = r.vstart.normalize()           # local midnight of start day
        last = r.vend
        while d <= last:
            day_lo = d
            day_hi = d + one_day
            lo = max(r.vstart, day_lo)
            hi = min(r.vend, day_hi)
            if hi > lo:
                # icu_day as a stable "YYYY-MM-DD" string -> robust merge keys
                # across parquet round-trips and DuckDB GROUP BY (date dtypes
                # otherwise silently mismatch).
                recs.append((r.encounter_block, d.strftime("%Y-%m-%d"), r.unit, r.unit_name,
                             (hi - lo).total_seconds() / 60.0, lo, hi))
            d = d + one_day
    cols = ["encounter_block", "icu_day", "unit", "unit_name", "overlap_min", "day_in", "day_out"]
    dd = pd.DataFrame.from_records(recs, columns=cols)
    if dd.empty:
        return dd

    # Collapse to one row per (block, day). Unit attribution = the unit the patient STARTED the
    # ICU-day in — the earliest ICU interval of the day (min day_in). ADT locations are mutually
    # exclusive, so min(day_in) is unique per (block, day): the pick is tie-free / order-invariant
    # by construction. (The old rule was max-overlap unit with no tie-break key, so a day split
    # evenly between two units flipped on input row order — see the determinism note in
    # plans/phase2_implementation_plan.md.) `unit` and `unit_name` both come from that same earliest
    # interval, so location_type and specific-unit are always consistent, which also lets us drop the
    # separate nested unit_name re-pick. Duration aggregates (minutes / in / out) are unchanged.
    # Clinical rationale: SAT/SBT are morning, nursing-driven, so the unit that owns the trial
    # opportunity is the one the patient is in going into the day; this also matches proning's
    # single-instant unit attribution. The trailing sort keys (unit, unit_name) only make the
    # (should-be-impossible) exact day_in tie a total order too.
    dd = dd.sort_values(["encounter_block", "icu_day", "day_in", "unit", "unit_name"])
    agg = (dd.groupby(["encounter_block", "icu_day"], as_index=False)
             .agg(unit=("unit", "first"),                 # start-of-day unit (earliest interval)
                  unit_name=("unit_name", "first"),
                  vented_icu_minutes=("overlap_min", "sum"),
                  day_in=("day_in", "min"),
                  day_out=("day_out", "max")))
    agg["unit"] = agg["unit"].fillna("unknown").replace("", "unknown")
    agg["unit_name"] = agg["unit_name"].fillna("unknown").replace("", "unknown")
    log.info("ventilated-ICU patient-days: %d (across %d encounter_blocks); specific units: %d",
             len(agg), agg["encounter_block"].nunique(), agg["unit_name"].nunique())
    return agg


# ---------------------------------------------------------------------------
# Demographics assembly
# ---------------------------------------------------------------------------
def attach_demographics(days: pd.DataFrame, co: clifpy.ClifOrchestrator,
                        hosp_s: pd.DataFrame, mapping: pd.DataFrame) -> pd.DataFrame:
    pat_cols = ["patient_id", "sex_category", "race_category", "ethnicity_category", "death_dttm"]
    # DETERMINISM: sort before dedup so a (rare) duplicate patient_id with differing demographics
    # keeps a fixed row, not an order-dependent one.
    _pcols = [c for c in pat_cols if c in co.patient.df.columns]
    pat = (co.patient.df[_pcols].sort_values(_pcols, na_position="last")
           .drop_duplicates("patient_id", keep="first"))

    hosp_cols = ["hospitalization_id", "patient_id", "age_at_admission",
                 "admission_dttm", "discharge_dttm", "admission_type_category", "discharge_category"]
    hosp = hosp_s[[c for c in hosp_cols if c in hosp_s.columns]].copy()
    hosp["hospitalization_id"] = hosp["hospitalization_id"].astype(str)

    m = mapping[["hospitalization_id", "encounter_block"]].astype({"hospitalization_id": str}).copy()
    m["encounter_block"] = m["encounter_block"].astype(str)
    # Block-level: primary row = earliest-admission hospitalization; list of all hids.
    # DETERMINISM: tie-break the block-primary pick and the id list by hospitalization_id so two
    # hospitalizations sharing an admission_dttm resolve the same way and the id list is byte-stable.
    hb = m.merge(hosp, on="hospitalization_id", how="left").sort_values(
        ["encounter_block", "admission_dttm", "hospitalization_id"], na_position="last")
    block_primary = hb.drop_duplicates("encounter_block", keep="first")
    ids_per_block = (m.groupby("encounter_block")["hospitalization_id"]
                       .apply(lambda s: sorted(s)).rename("hospitalization_ids").reset_index())

    out = (days
           .merge(block_primary, on="encounter_block", how="left")
           .merge(ids_per_block, on="encounter_block", how="left")
           .merge(pat, on="patient_id", how="left"))
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="Delete _cache/ and rebuild everything.")
    ap.add_argument("--refresh-waterfall", action="store_true", help="Force waterfall rebuild only.")
    args = ap.parse_args()

    _ensure_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(LOGS_DIR / "01_build_cohort.log", mode="w")],
    )

    if args.refresh and CACHE_DIR.exists():
        shutil.rmtree(CACHE_DIR); CACHE_DIR.mkdir()
        log.info("cleared full cache")
    elif args.refresh_waterfall and cpath("resp_waterfall").exists():
        cpath("resp_waterfall").unlink()
        log.info("cleared waterfall cache")

    cfg = load_config()
    tz = cfg["timezone"]
    med_sets = sat_med_sets(cfg)
    log.info("site=%s timezone=%s | SAT-relevant=%s dex=%s paralytic=%s", cfg.get("site"), tz,
             sorted(med_sets["sat_relevant"]), sorted(med_sets["dex"]), sorted(med_sets["paralytic"]))

    co = build_orchestrator(cfg)
    from common.build_shared import ensure_shared
    wf_shared, mapping, hosp_s, adt_s = ensure_shared(
        co, tz, _SITE, waterfall_version=_bc.WATERFALL_VERSION)
    mapping["encounter_block"] = mapping["encounter_block"].astype(str)
    mapping["hospitalization_id"] = mapping["hospitalization_id"].astype(str)

    inf = load_infusions_cached(co, mapping, med_sets, tz)

    # Scope the waterfall: hospitalizations that are BOTH in ICU and have a
    # SAT-relevant infusion (the only blocks that can ever be eligible).
    icu = build_icu_intervals(adt_s, mapping, tz)
    icu_blocks = set(icu["encounter_block"].unique())
    sat_blocks = set(inf.loc[inf["med_category"].isin(med_sets["sat_relevant"]), "encounter_block"].astype(str).unique())
    cohort_blocks = icu_blocks & sat_blocks
    scope_hosp_ids = sorted(
        mapping.loc[mapping["encounter_block"].isin(cohort_blocks), "hospitalization_id"].unique())
    log.info("scope: %d ICU blocks, %d sedation blocks -> %d cohort blocks (%d hospitalizations)",
             len(icu_blocks), len(sat_blocks), len(cohort_blocks), len(scope_hosp_ids))
    if not scope_hosp_ids:
        raise RuntimeError("empty cohort scope — check encounter_block alignment between adt and mapping")

    wf = waterfall_cached(wf_shared, scope_hosp_ids, tz)

    imv = build_imv_intervals(wf)
    vint = intersect_imv_icu(imv, icu)
    # DETERMINISM: intersect_imv_icu returns a raw DuckDB join (no row-order guarantee); sort to a
    # total order so the cached parquet is byte-stable across runs.
    _vcols = [c for c in ["encounter_block", "vstart", "vend", "unit", "unit_name"] if c in vint.columns]
    vint = vint.sort_values(_vcols, na_position="last").reset_index(drop=True)
    vint.to_parquet(INTERMEDIATE_DIR / "vent_icu_intervals.parquet", index=False)

    days = expand_to_days(vint, tz)
    cohort = attach_demographics(days, co, hosp_s, mapping)
    cohort.to_parquet(INTERMEDIATE_DIR / "cohort.parquet", index=False)

    # CONSORT-like flow (counts only).
    n_blocks_vent = cohort["encounter_block"].nunique()
    n_pts = cohort["patient_id"].nunique() if "patient_id" in cohort.columns else None
    flow = pd.DataFrame([
        {"step": 1, "label": "encounter_blocks in ICU with a SAT-relevant infusion",
         "n_encounter_blocks": len(cohort_blocks), "n_patient_days": None},
        {"step": 2, "label": "ventilated-ICU patient-days (IMV ∩ ICU, day-expanded)",
         "n_encounter_blocks": int(n_blocks_vent), "n_patient_days": int(len(cohort))},
    ])
    flow.to_csv(FINAL_DIR / "cohort_flow.csv", index=False)

    log.info("CONSORT flow:")
    for _, r in flow.iterrows():
        log.info("  [%d] %-55s blocks=%s days=%s", r["step"], r["label"],
                 r["n_encounter_blocks"], r["n_patient_days"])
    log.info("ventilated-ICU patient-days: %d | blocks: %d | patients: %s",
             len(cohort), n_blocks_vent, n_pts)
    log.info("wrote: cohort.parquet, vent_icu_intervals.parquet, cohort_flow.csv")


if __name__ == "__main__":
    main()
