"""Build the ARDS cohort for the proning QI project.

Screening question: "did this ICU stay ever look like ARDS?". The cohort is a
Berlin moderate-severe ARDS phenotype on invasive ventilation, defined purely
on physiology so any CLIF site can reproduce it. Trial-specific machinery
(enrollment-enrichment windows, ECMO/pregnancy/influenza/DNR exclusions,
fuzzy-window enrollment ABG) is deliberately omitted — that machinery exists to
clean up a causal effect estimate, and proning QI is descriptive.

ARDS screening at T₀ (Berlin moderate-severe gate):
    - age ≥ 18
    - device_category == "imv"
    - peep_set   ≥ 5  cmH2O
    - fio2_set   ≥ 0.4
    - pf_ratio  ≤ 300
    - in an ICU location at the ABG time

T₀ = earliest ABG meeting all criteria within an encounter_block.
One row per patient (earliest T₀ across encounter_blocks).

No raw PHI is printed to stdout; only aggregate counts and summary stats.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

import clifpy

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BUNDLE_ROOT = Path(__file__).resolve().parents[3]        # repo root (holds bundle_config.py)
import sys as _sys
if str(_BUNDLE_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_BUNDLE_ROOT))
import bundle_config as _bc                                # multi-site config + output resolver
_METRIC, _SITE = "proning", _bc.active_site()
CONFIG_PATH = PROJECT_ROOT / "config" / "config.json"     # legacy path (load_config now uses bundle_config)
OUTPUT_DIR = _bc.metric_output_dir(_METRIC, _SITE)         # output/<site>/metrics/proning
INTERMEDIATE_DIR = OUTPUT_DIR / "intermediate"
CACHE_DIR = INTERMEDIATE_DIR / "_cache"
FINAL_DIR = OUTPUT_DIR / "final"
LOGS_DIR = OUTPUT_DIR / "logs"

IMV_CATEGORY = "imv"  # UChicago stores device_category lowercase

log = logging.getLogger("proning.cohort")


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
    Cached parquets and waterfall scaffold rows can demote to object dtype.
    """
    s = pd.to_datetime(series, errors="coerce")
    if getattr(s.dt, "tz", None) is None:
        s = s.dt.tz_localize(tz, ambiguous="NaT", nonexistent="shift_forward")
    else:
        s = s.dt.tz_convert(tz)
    return s


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load_abgs_cached(co: clifpy.ClifOrchestrator) -> pd.DataFrame:
    if cpath("abgs").exists():
        log.info("cache hit: abgs")
        return pd.read_parquet(cpath("abgs"))
    co.load_table("labs", filters={"lab_category": ["po2_arterial"]})
    df = co.labs.df
    df.to_parquet(cpath("abgs"), index=False)
    log.info("wrote cache: abgs (%d rows)", len(df))
    return df


def load_spo2_cached(co: clifpy.ClifOrchestrator, hosp_ids: list[str]) -> pd.DataFrame:
    """SpO2 vitals for the (arterial-gas-having) cohort hospitalizations — used for the
    S/F surrogate onset definition. Scoped to the same hospitalizations as the waterfall."""
    if cpath("spo2").exists():
        log.info("cache hit: spo2")
        return pd.read_parquet(cpath("spo2"))
    co.load_table("vitals", filters={"hospitalization_id": hosp_ids, "vital_category": ["spo2"]})
    df = co.vitals.df[["hospitalization_id", "recorded_dttm", "vital_value"]].copy()
    df.to_parquet(cpath("spo2"), index=False)
    log.info("wrote cache: spo2 (%d rows)", len(df))
    return df


def load_small_tables(co: clifpy.ClifOrchestrator) -> None:
    for t in ("patient", "hospitalization", "adt"):
        co.load_table(t)
        df = getattr(co, t).df
        log.info("loaded %s: %d rows", t, 0 if df is None else len(df))


# ---------------------------------------------------------------------------
# Encounter stitching (cached)
# ---------------------------------------------------------------------------
def stitch_cached(co: clifpy.ClifOrchestrator) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
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
    hosp_s.to_parquet(cpath("hosp_stitched"), index=False)
    adt_s.to_parquet(cpath("adt_stitched"), index=False)
    mapping.to_parquet(cpath("encounter_mapping"), index=False)
    log.info("wrote cache: stitched %d hospitalizations → %d encounter_blocks",
             mapping["hospitalization_id"].nunique(), mapping["encounter_block"].nunique())
    return hosp_s, adt_s, mapping


# ---------------------------------------------------------------------------
# Respiratory waterfall (cached — expensive ~35 min step)
# ---------------------------------------------------------------------------
def waterfall_cached(wf_shared: pd.DataFrame, abg_df: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    """Filter the shared union waterfall (common.build_shared.ensure_shared) to proning's ABG-having
    scope and write the stage-local slice to CACHE_DIR/resp_waterfall.parquet — the exact path stages
    02/02b read. Cleaning already happened PRE-waterfall inside load_clean (the shared build), so no
    post-hoc normalize here (matches the pre-consolidation behavior); the returned slice is byte-identical
    to proning's old per-vertical build filtered to the same hosp-ids (verified 0-diff at consolidation)."""
    abg_hosp_ids = set(abg_df["hospitalization_id"].dropna().astype(str).unique())
    wf = wf_shared[wf_shared["hospitalization_id"].astype(str).isin(abg_hosp_ids)].copy()
    log.info("waterfall: filtered shared union to %d ABG-having hospitalizations (%d rows)",
             len(abg_hosp_ids), len(wf))
    wf.to_parquet(cpath("resp_waterfall"), index=False)
    return wf, f"shared union (wf={_bc.WATERFALL_VERSION})"


def _normalize_waterfall(wf: pd.DataFrame, tz: str) -> pd.DataFrame:
    """Post-waterfall cleanup applied every time (whether fresh or from cache).

    - Coerce ``recorded_dttm`` to tz-aware.
    - Lowercase device_category, mode_category (UChicago site convention).
    - FiO2 unit detection via p95; clip implausible FiO2 ∈ [0.15, 1.0] and
      PEEP ∈ [0, 40] to NaN.
    """
    wf = wf.copy()
    wf["recorded_dttm"] = _coerce_dttm(wf["recorded_dttm"], tz)
    for col in ("device_category", "mode_category"):
        if col in wf.columns:
            wf[col] = wf[col].astype("string").str.strip().str.lower()
    fio2 = wf["fio2_set"]
    p95 = fio2.dropna().quantile(0.95) if fio2.notna().any() else None
    if p95 is not None and p95 > 1.5:
        wf["fio2_set"] = fio2 / 100.0
        note = f"percent-encoded (p95={p95:.2f}, max={fio2.max():.1f}) → /100"
    else:
        note = f"fraction (p95={p95:.3f}, max={fio2.max():.3f})" if p95 is not None else "empty"
    bad_mask = wf["fio2_set"].notna() & ~wf["fio2_set"].between(0.15, 1.0)
    n_bad = int(bad_mask.sum())
    wf.loc[bad_mask, "fio2_set"] = pd.NA
    peep = wf["peep_set"]
    peep_bad_mask = peep.notna() & ~peep.between(0, 40)
    n_peep_bad = int(peep_bad_mask.sum())
    wf.loc[peep_bad_mask, "peep_set"] = pd.NA
    log.info("normalize: device_category/mode_category lowercased; "
             "fio2 %s; clipped %d implausible fio2, %d implausible peep",
             note, n_bad, n_peep_bad)
    return wf


# ---------------------------------------------------------------------------
# ABG extraction + as-of merge + P/F
# ---------------------------------------------------------------------------
def extract_abgs(abg_df: pd.DataFrame, mapping: pd.DataFrame) -> pd.DataFrame:
    mask = abg_df["lab_value_numeric"].notna() & (abg_df["lab_value_numeric"] > 0)
    abg = abg_df.loc[mask, ["hospitalization_id", "lab_collect_dttm", "lab_value_numeric"]].copy()
    abg = abg.rename(columns={"lab_collect_dttm": "abg_time", "lab_value_numeric": "pao2"})
    abg = abg.merge(
        mapping[["hospitalization_id", "encounter_block"]], on="hospitalization_id", how="left"
    )
    abg = abg.dropna(subset=["abg_time", "encounter_block"])
    log.info("arterial PaO2 events: %d (across %d encounter_blocks)",
             len(abg), abg["encounter_block"].nunique())
    return abg


def attach_vent_and_compute_pf(abg: pd.DataFrame, wf: pd.DataFrame, tz: str) -> pd.DataFrame:
    cols = ["encounter_block", "recorded_dttm", "device_category", "peep_set",
            "fio2_set", "mode_category"]
    wf_s = wf[cols].dropna(subset=["encounter_block", "recorded_dttm"]).copy()
    wf_s["recorded_dttm"] = _coerce_dttm(wf_s["recorded_dttm"], tz)
    abg = abg.copy()
    abg["abg_time"] = _coerce_dttm(abg["abg_time"], tz)
    wf_s = wf_s.sort_values("recorded_dttm")
    abg_s = abg.sort_values("abg_time")
    pf = pd.merge_asof(
        abg_s, wf_s,
        left_on="abg_time", right_on="recorded_dttm",
        by="encounter_block",
        direction="backward",
        tolerance=pd.Timedelta("6h"),
    )
    n_stale = pf["fio2_set"].isna().sum()
    log.info("ABGs dropped for stale/absent vent state (>6h) or bad fio2: %d", int(n_stale))
    pf = pf.dropna(subset=["fio2_set"])
    pf["pf_ratio"] = pf["pao2"] / pf["fio2_set"]
    in_band = pf["pf_ratio"].between(10, 1000)
    log.info("ABGs dropped for implausible P/F (<10 or >1000): %d", int((~in_band).sum()))
    return pf.loc[in_band].copy()


def extract_spo2(spo2_df: pd.DataFrame, mapping: pd.DataFrame) -> pd.DataFrame:
    s = spo2_df.copy()
    s["spo2"] = pd.to_numeric(s["vital_value"], errors="coerce")
    s = s[s["spo2"].between(1, 100)]
    s = s.rename(columns={"recorded_dttm": "sf_time"})[["hospitalization_id", "sf_time", "spo2"]]
    s = s.merge(mapping[["hospitalization_id", "encounter_block"]], on="hospitalization_id", how="left")
    s = s.dropna(subset=["sf_time", "encounter_block"])
    log.info("SpO2 events: %d (across %d encounter_blocks)", len(s), s["encounter_block"].nunique())
    return s


def attach_vent_and_compute_sf(spo2_ev: pd.DataFrame, wf: pd.DataFrame, tz: str) -> pd.DataFrame:
    """S/F = SpO2 / FiO2, pairing each SpO2 with the most recent vent state (≤6h, like P/F)."""
    cols = ["encounter_block", "recorded_dttm", "device_category", "peep_set",
            "fio2_set", "mode_category"]
    wf_s = wf[cols].dropna(subset=["encounter_block", "recorded_dttm"]).copy()
    wf_s["recorded_dttm"] = _coerce_dttm(wf_s["recorded_dttm"], tz)
    sf = spo2_ev.copy()
    sf["sf_time"] = _coerce_dttm(sf["sf_time"], tz)
    wf_s = wf_s.sort_values("recorded_dttm")
    sf_s = sf.sort_values("sf_time")
    m = pd.merge_asof(
        sf_s, wf_s,
        left_on="sf_time", right_on="recorded_dttm",
        by="encounter_block", direction="backward", tolerance=pd.Timedelta("6h"),
    )
    m = m.dropna(subset=["fio2_set"])
    m["sf_ratio"] = m["spo2"] / m["fio2_set"]
    in_band = m["sf_ratio"].between(20, 2000)
    return m.loc[in_band].copy()


def restrict_to_icu(pf: pd.DataFrame, adt_s: pd.DataFrame, time_col: str = "abg_time") -> pd.DataFrame:
    if "encounter_block" not in adt_s.columns:
        raise RuntimeError("adt was not stitched — missing encounter_block")
    icu = adt_s.loc[
        adt_s["location_category"] == "icu",
        ["encounter_block", "in_dttm", "out_dttm"],
    ].copy()
    con = duckdb.connect()
    con.register("pf", pf)
    con.register("icu", icu)
    joined = con.execute(
        f"""
        SELECT pf.*,
               icu.in_dttm  AS icu_in_dttm,
               icu.out_dttm AS icu_out_dttm
        FROM pf
        JOIN icu
          ON pf.encounter_block = icu.encounter_block
         AND pf.{time_col} BETWEEN icu.in_dttm AND icu.out_dttm
        """
    ).fetchdf()
    con.close()
    joined = (
        joined.sort_values(["encounter_block", time_col, "icu_in_dttm"])
        .drop_duplicates(subset=["encounter_block", time_col], keep="first")
    )
    log.info("events in ICU (%s): %d (from %d pre-ICU-filter)", time_col, len(joined), len(pf))
    return joined


# ---------------------------------------------------------------------------
# T₀: ARDS screening
# ---------------------------------------------------------------------------
def compute_t0(pf_icu: pd.DataFrame, sf_icu: pd.DataFrame | None,
               hosp_s: pd.DataFrame, screen: dict) -> pd.DataFrame:
    """Earliest ARDS-qualifying event per encounter_block (one row per block).

    A time point qualifies if: on IMV, PEEP ≥ peep_min, FiO2 ≥ fio2_min, in an ICU, age ≥ 18,
    AND oxygenation is severe by EITHER an arterial P/F ≤ pf_max OR — when no arterial gas
    qualifies — the S/F surrogate (SpO2/FiO2 ≤ sf_max while SpO2 ≤ spo2_max). T0 = the earliest
    such event (across both sources)."""
    pf_max = screen.get("pf_max", 300)
    fio2_min = screen.get("fio2_min", 0.4)
    peep_min = screen.get("peep_min", 5)
    use_sf = screen.get("use_sf_surrogate", True)
    sf_max = screen.get("sf_max", 315)
    spo2_max = screen.get("spo2_max", 97)
    hp = hosp_s[["hospitalization_id", "patient_id", "age_at_admission"]].drop_duplicates()

    common = ["encounter_block", "hospitalization_id", "patient_id", "age_at_admission",
              "t0_time", "pao2_at_t0", "fio2_at_t0", "peep_at_t0", "pf_at_t0",
              "spo2_at_t0", "sf_at_t0", "t0_source", "icu_in_dttm_at_t0"]

    pf = pf_icu.merge(hp, on="hospitalization_id", how="left")
    pf_cand = pf[
        (pf["device_category"] == IMV_CATEGORY) & (pf["peep_set"] >= peep_min)
        & (pf["fio2_set"] >= fio2_min) & (pf["pf_ratio"] <= pf_max)
        & (pf["age_at_admission"] >= 18)
    ].copy()
    pf_cand = pf_cand.rename(columns={
        "abg_time": "t0_time", "pao2": "pao2_at_t0", "fio2_set": "fio2_at_t0",
        "peep_set": "peep_at_t0", "pf_ratio": "pf_at_t0", "icu_in_dttm": "icu_in_dttm_at_t0"})
    pf_cand["spo2_at_t0"] = np.nan
    pf_cand["sf_at_t0"] = np.nan
    pf_cand["t0_source"] = "pf"
    frames = [pf_cand[common]]

    if use_sf and sf_icu is not None and not sf_icu.empty:
        sf = sf_icu.merge(hp, on="hospitalization_id", how="left")
        sf_cand = sf[
            (sf["device_category"] == IMV_CATEGORY) & (sf["peep_set"] >= peep_min)
            & (sf["fio2_set"] >= fio2_min) & (sf["spo2"] <= spo2_max)
            & (sf["sf_ratio"] <= sf_max) & (sf["age_at_admission"] >= 18)
        ].copy()
        sf_cand = sf_cand.rename(columns={
            "sf_time": "t0_time", "fio2_set": "fio2_at_t0", "peep_set": "peep_at_t0",
            "spo2": "spo2_at_t0", "sf_ratio": "sf_at_t0", "icu_in_dttm": "icu_in_dttm_at_t0"})
        sf_cand["pao2_at_t0"] = np.nan
        sf_cand["pf_at_t0"] = np.nan
        sf_cand["t0_source"] = "sf"
        frames.append(sf_cand[common])

    cand = pd.concat(frames, ignore_index=True)
    t0 = (cand.sort_values("t0_time")
          .drop_duplicates(subset=["encounter_block"], keep="first")
          .rename(columns={"t0_time": "T0"}))
    return t0[["encounter_block", "hospitalization_id", "patient_id", "age_at_admission",
               "T0", "pao2_at_t0", "fio2_at_t0", "peep_at_t0", "pf_at_t0",
               "spo2_at_t0", "sf_at_t0", "t0_source", "icu_in_dttm_at_t0"]]


# ---------------------------------------------------------------------------
# Output assembly
# ---------------------------------------------------------------------------
def assemble_cohort_row(
    cohort: pd.DataFrame,
    co: clifpy.ClifOrchestrator,
    hosp_s: pd.DataFrame,
) -> pd.DataFrame:
    pat_cols = ["patient_id", "sex_category", "race_category", "ethnicity_category", "death_dttm"]
    pat = co.patient.df[pat_cols].drop_duplicates(subset=["patient_id"])
    hosp_cols = [
        "hospitalization_id", "admission_dttm", "discharge_dttm",
        "admission_type_category", "discharge_category",
    ]
    hosp_slim = hosp_s[hosp_cols]
    mapping = co.encounter_mapping[["hospitalization_id", "encounter_block"]]
    hosp_ids_per_block = (
        mapping.groupby("encounter_block")["hospitalization_id"].apply(list).rename("hospitalization_ids")
    )
    out = cohort.merge(pat, on="patient_id", how="left", suffixes=("", "_pat"))
    out = out.merge(hosp_slim, on="hospitalization_id", how="left")
    out = out.merge(hosp_ids_per_block, on="encounter_block", how="left")
    keep = [
        "patient_id", "encounter_block", "hospitalization_id", "hospitalization_ids",
        "icu_in_dttm_at_t0",
        "T0", "pao2_at_t0", "fio2_at_t0", "peep_at_t0", "pf_at_t0",
        "spo2_at_t0", "sf_at_t0", "t0_source",
        "age_at_admission", "sex_category", "race_category", "ethnicity_category",
        "admission_type_category", "admission_dttm", "discharge_dttm", "discharge_category",
        "death_dttm",
    ]
    return out[[c for c in keep if c in out.columns]]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true",
                    help="Delete output/intermediate/_cache/ and rebuild everything.")
    ap.add_argument("--refresh-waterfall", action="store_true",
                    help="Keep other caches; force waterfall rebuild.")
    args = ap.parse_args()

    _ensure_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOGS_DIR / "01_build_cohort.log", mode="w"),
        ],
    )

    if args.refresh and CACHE_DIR.exists():
        shutil.rmtree(CACHE_DIR); CACHE_DIR.mkdir()
        log.info("cleared full cache")
    elif args.refresh_waterfall and cpath("resp_waterfall").exists():
        cpath("resp_waterfall").unlink()
        log.info("cleared waterfall cache")

    cfg = load_config(CONFIG_PATH)
    tz = cfg["timezone"]
    log.info("site=%s timezone=%s", cfg.get("site"), tz)

    screen = cfg.get("ards_cohort", {})
    co = build_orchestrator(cfg)
    from common.build_shared import ensure_shared
    wf_shared, mapping, hosp_s, adt_s = ensure_shared(
        co, tz, _SITE, waterfall_version=_bc.WATERFALL_VERSION)
    abg_df = load_abgs_cached(co)
    wf, fio2_note = waterfall_cached(wf_shared, abg_df)

    abg = extract_abgs(abg_df, mapping)
    pf = attach_vent_and_compute_pf(abg, wf, tz)
    pf_icu = restrict_to_icu(pf, adt_s, time_col="abg_time")

    # S/F surrogate (config ards_cohort.use_sf_surrogate): backfill onset from SpO2 when no
    # qualifying arterial P/F. Scoped to the same (arterial-gas-having) hospitalizations as the
    # waterfall, so it reclassifies onset for those; pure-SpO2-only patients need a wider waterfall.
    sf_icu = None
    if screen.get("use_sf_surrogate", True):
        abg_hosp_ids = abg_df["hospitalization_id"].dropna().astype(str).unique().tolist()
        spo2_df = load_spo2_cached(co, abg_hosp_ids)
        sf_ev = extract_spo2(spo2_df, mapping)
        sf = attach_vent_and_compute_sf(sf_ev, wf, tz)
        sf_icu = restrict_to_icu(sf, adt_s, time_col="sf_time")

    t0 = compute_t0(pf_icu, sf_icu, hosp_s, screen)
    src = t0["t0_source"].value_counts().to_dict()
    log.info("encounters with a T₀: %d (patients: %d) — T₀ source: %s",
             t0["encounter_block"].nunique(), t0["patient_id"].nunique(), src)

    # One row per patient — earliest T₀
    t0_one = t0.sort_values("T0").drop_duplicates(subset=["patient_id"], keep="first")
    log.info("after one-per-patient (earliest T₀): %d patients", t0_one["patient_id"].nunique())

    cohort_final = assemble_cohort_row(t0_one, co, hosp_s)
    cohort_final.to_parquet(INTERMEDIATE_DIR / "cohort.parquet", index=False)

    # Concise CONSORT-like flow for downstream reporting
    flow = pd.DataFrame([
        {"step": 1, "label": "encounter_blocks meeting ARDS screen at T₀",
         "n_encounter_blocks": int(t0["encounter_block"].nunique()),
         "n_patients": int(t0["patient_id"].nunique())},
        {"step": 2, "label": "one row per patient (earliest T₀)",
         "n_encounter_blocks": int(t0_one["encounter_block"].nunique()),
         "n_patients": int(t0_one["patient_id"].nunique())},
    ])
    flow.to_csv(FINAL_DIR / "cohort_flow.csv", index=False)

    log.info("CONSORT flow:")
    for _, row in flow.iterrows():
        log.info("  [%d] %-50s n_patients=%d  n_blocks=%d",
                 row["step"], row["label"], row["n_patients"], row["n_encounter_blocks"])
    log.info("fio2 convention: %s", fio2_note)
    log.info("wrote: cohort.parquet, cohort_flow.csv, 01_build_cohort.log")


if __name__ == "__main__":
    main()
