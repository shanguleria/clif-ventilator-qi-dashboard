"""Build the ventilated-ICU patient-DAY universe for the SBT QI vertical.

Unit of analysis = one row per (encounter_block, ICU calendar day in US/Central)
on which the patient was on invasive mechanical ventilation (IMV) AND in an ICU
location — identical to the SAT vertical's cohort. This is the SBT denominator
universe before the eligibility filter (>=12h controlled + >=2h stable + non-trach,
applied in 02).

Pipeline:
    patient/hospitalization/adt   -> stitch encounters (cached)
    respiratory_support           -> waterfall (shared common.resp_support.build_waterfall)
    waterfall                     -> IMV intervals (consecutive-row segmentation)
    IMV  ∩  ICU adt intervals     -> ventilated-ICU intervals (+ location_type unit)
    intervals                     -> expand to calendar days -> cohort.parquet

SBT builds its OWN full ICU∩IMV respiratory_support waterfall (all ICU∩IMV blocks,
including never-sedated patients) via the shared `common.resp_support.build_waterfall`
— it no longer seeds from the SAT vertical's SAT-sedation-scoped cache, which silently
dropped never-sedated ventilated-ICU patients. The waterfall is cached in `_cache/`
with a `.version` sidecar that auto-rebuilds on a WATERFALL_VERSION bump. No raw PHI
is printed.

Machinery adapted from ../sat/code/01_build_cohort.py so this vertical stays
self-contained for federation.
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
_METRIC, _SITE = "sbt", _bc.active_site()
CONFIG_PATH = PROJECT_ROOT / "config.json"                 # legacy path (load_config now uses bundle_config)
OUTPUT_DIR = _bc.metric_output_dir(_METRIC, _SITE)         # output/<site>/metrics/sbt
INTERMEDIATE_DIR = OUTPUT_DIR / "intermediate"
CACHE_DIR = INTERMEDIATE_DIR / "_cache"
FINAL_DIR = OUTPUT_DIR / "final"
LOGS_DIR = OUTPUT_DIR / "logs"

IMV_CATEGORY = "imv"            # UChicago stores device_category lowercase
ICU_CATEGORY = "icu"
TRAILING_IMV_CAP_H = 24

log = logging.getLogger("sbt.cohort")


# ---------------------------------------------------------------------------
# Dirs / config / orchestrator  (adapted from SAT)
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
    """Normalize a datetime column to tz-aware ``datetime64[us, tz]``."""
    s = pd.to_datetime(series, errors="coerce")
    if getattr(s.dt, "tz", None) is None:
        s = s.dt.tz_localize(tz, ambiguous="NaT", nonexistent="shift_forward")
    else:
        s = s.dt.tz_convert(tz)
    return s


# ---------------------------------------------------------------------------
# Loading + stitching  (adapted from SAT)
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


# ---------------------------------------------------------------------------
# Respiratory waterfall (cached — the expensive step)
# ---------------------------------------------------------------------------
def _normalize_waterfall(wf: pd.DataFrame, tz: str) -> pd.DataFrame:
    wf = wf.copy()
    wf["recorded_dttm"] = _coerce_dttm(wf["recorded_dttm"], tz)
    for col in ("device_category", "mode_category"):
        if col in wf.columns:
            wf[col] = wf[col].astype("string").str.strip().str.lower()
    return wf


def waterfall_cached(wf_shared: pd.DataFrame, scope_hosp_ids: list[str], tz: str) -> pd.DataFrame:
    """Filter the shared union waterfall (common.build_shared.ensure_shared) to SBT's FULL ICU scope
    (all ICU∩IMV blocks, incl. never-sedated), write the stage-local slice to
    CACHE_DIR/resp_waterfall.parquet (the path stages 02/03 read), and return it normalized. Cleaning
    already happened PRE-waterfall inside load_clean (the shared build); `_normalize_waterfall` is
    idempotent and kept so stages 02/03 (which re-read + re-apply it) are unchanged. The filtered slice
    is byte-identical to SBT's old per-vertical build (verified 0-diff at consolidation)."""
    scope = set(map(str, scope_hosp_ids))
    wf = wf_shared[wf_shared["hospitalization_id"].astype(str).isin(scope)].copy()
    # The shared waterfall carries encounter_block as float64 (int block-id + NaN from the left-merge);
    # SBT keys ICU intervals on a clean int-string encounter_block ("110135"), so cast to match — else
    # the str-vs-float imv∩icu join silently yields zero.
    wf["encounter_block"] = wf["encounter_block"].astype("Int64").astype(str)
    log.info("waterfall: filtered shared union to %d hospitalizations (all ICU; %d rows)",
             len(scope), len(wf))
    wf.to_parquet(cpath("resp_waterfall"), index=False)
    return _normalize_waterfall(wf, tz)


# ---------------------------------------------------------------------------
# IMV intervals  ∩  ICU intervals  ->  ventilated-ICU intervals
# ---------------------------------------------------------------------------
def build_imv_intervals(wf: pd.DataFrame) -> pd.DataFrame:
    """Consecutive-row segmentation of the waterfall device timeline; keep
    device==imv segments; trailing record of a block capped."""
    w = wf.dropna(subset=["encounter_block", "recorded_dttm"]).copy()
    w["encounter_block"] = w["encounter_block"].astype(str)
    w = w.sort_values(["encounter_block", "recorded_dttm"])
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
    # location_name = specific physical unit (finer than location_type). Keep RAW case so the unit
    # code matches the other verticals' feeds (the scorecard unions name keys across feeds).
    icu["location_name"] = icu["location_name"].astype("string").str.strip()
    icu["location_name"] = icu["location_name"].fillna("unknown").replace("", "unknown")
    icu = icu.dropna(subset=["in_dttm", "out_dttm"])
    icu = icu[icu["out_dttm"] > icu["in_dttm"]]
    return icu


def intersect_imv_icu(imv: pd.DataFrame, icu: pd.DataFrame) -> pd.DataFrame:
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
    v = vint.copy()
    v["vstart"] = _coerce_dttm(v["vstart"], tz)
    v["vend"] = _coerce_dttm(v["vend"], tz)

    recs = []
    one_day = timedelta(days=1)
    for r in v.itertuples(index=False):
        d = r.vstart.normalize()
        last = r.vend
        while d <= last:
            day_lo = d
            day_hi = d + one_day
            lo = max(r.vstart, day_lo)
            hi = min(r.vend, day_hi)
            if hi > lo:
                recs.append((r.encounter_block, d.strftime("%Y-%m-%d"), r.unit, r.unit_name,
                             (hi - lo).total_seconds() / 60.0, lo, hi))
            d = d + one_day
    cols = ["encounter_block", "icu_day", "unit", "unit_name", "overlap_min", "day_in", "day_out"]
    dd = pd.DataFrame.from_records(recs, columns=cols)
    if dd.empty:
        return dd

    dd = dd.sort_values(["encounter_block", "icu_day", "overlap_min"], ascending=[True, True, False])
    agg = (dd.groupby(["encounter_block", "icu_day"])
             .agg(unit=("unit", "first"),
                  vented_icu_minutes=("overlap_min", "sum"),
                  day_in=("day_in", "min"),
                  day_out=("day_out", "max"))
             .reset_index())
    agg["unit"] = agg["unit"].fillna("unknown").replace("", "unknown")

    # Specific unit (location_name), NESTED within the chosen type: among that day's intervals
    # whose location_type == the chosen unit, pick the location_name with the most overlap.
    dn = dd.merge(agg[["encounter_block", "icu_day", "unit"]]
                  .rename(columns={"unit": "chosen_unit"}), on=["encounter_block", "icu_day"])
    dn = dn[dn["unit"] == dn["chosen_unit"]]
    name_agg = (dn.groupby(["encounter_block", "icu_day", "unit_name"])["overlap_min"].sum()
                .reset_index()
                .sort_values(["encounter_block", "icu_day", "overlap_min", "unit_name"],
                             ascending=[True, True, False, True])
                .drop_duplicates(["encounter_block", "icu_day"]))
    agg = agg.merge(name_agg[["encounter_block", "icu_day", "unit_name"]],
                    on=["encounter_block", "icu_day"], how="left")
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
    pat = co.patient.df[[c for c in pat_cols if c in co.patient.df.columns]].drop_duplicates("patient_id")

    hosp_cols = ["hospitalization_id", "patient_id", "age_at_admission",
                 "admission_dttm", "discharge_dttm", "admission_type_category", "discharge_category"]
    hosp = hosp_s[[c for c in hosp_cols if c in hosp_s.columns]].copy()
    hosp["hospitalization_id"] = hosp["hospitalization_id"].astype(str)

    m = mapping[["hospitalization_id", "encounter_block"]].astype({"hospitalization_id": str}).copy()
    m["encounter_block"] = m["encounter_block"].astype(str)
    hb = m.merge(hosp, on="hospitalization_id", how="left").sort_values(["encounter_block", "admission_dttm"])
    block_primary = hb.drop_duplicates("encounter_block", keep="first")
    ids_per_block = (m.groupby("encounter_block")["hospitalization_id"]
                       .apply(list).rename("hospitalization_ids").reset_index())

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
    log.info("site=%s timezone=%s", cfg.get("site"), tz)

    co = build_orchestrator(cfg)
    from common.build_shared import ensure_shared
    wf_shared, mapping, hosp_s, adt_s = ensure_shared(
        co, tz, _SITE, waterfall_version=_bc.WATERFALL_VERSION)
    mapping["encounter_block"] = mapping["encounter_block"].astype(str)
    mapping["hospitalization_id"] = mapping["hospitalization_id"].astype(str)

    icu = build_icu_intervals(adt_s, mapping, tz)

    # SBT's waterfall scope = all ICU hospitalizations (filtered from the shared union waterfall).
    icu_blocks = set(icu["encounter_block"].unique())
    scope_hosp_ids = sorted(
        mapping.loc[mapping["encounter_block"].isin(icu_blocks), "hospitalization_id"].unique())
    log.info("scope: %d ICU encounter_blocks (%d hospitalizations)",
             len(icu_blocks), len(scope_hosp_ids))

    wf = waterfall_cached(wf_shared, scope_hosp_ids, tz)

    imv = build_imv_intervals(wf)
    vint = intersect_imv_icu(imv, icu)
    vint.to_parquet(INTERMEDIATE_DIR / "vent_icu_intervals.parquet", index=False)

    days = expand_to_days(vint, tz)
    cohort = attach_demographics(days, co, hosp_s, mapping)
    cohort.to_parquet(INTERMEDIATE_DIR / "cohort.parquet", index=False)

    n_blocks_vent = cohort["encounter_block"].nunique()
    n_pts = cohort["patient_id"].nunique() if "patient_id" in cohort.columns else None
    flow = pd.DataFrame([
        {"step": 1, "label": "ICU encounter_blocks (waterfall scope)",
         "n_encounter_blocks": len(icu_blocks), "n_patient_days": None},
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
