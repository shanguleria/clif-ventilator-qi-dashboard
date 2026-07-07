"""Treatment characteristics at T₀ and T_eligible (PROSEVA Table-1 enrichment).

For each cohort encounter_block this stage derives three descriptive treatment
characteristics (modeled on the PROSEVA cohort description) at TWO anchors so the
dashboard can compare the cohort between ARDS onset and the proning decision-point:
    - T₀         — the first ARDS-qualifying ABG (cohort entry; all blocks).
    - T_eligible — T_first + 12 h, the PROSEVA decision-point (eligible blocks only;
                   read from proning_eligibility.parquet, written by stage 02).

Per anchor (suffix t0 / te):
    - on_vasopressor_at_<a> (bool)  — any continuous vasopressor infusion running at the anchor
    - on_nmb_at_<a>         (bool)  — any continuous neuromuscular blocker running at the anchor
    - vt_set_at_<a>         (float) — set tidal volume (mL) charted at/just before the anchor
(Physiology — P/F, FiO₂, PEEP — at both anchors is carried in cohort.parquet (T₀) and
proning_eligibility.parquet (T_eligible); this stage adds only the treatment columns.)

These are *presence* checks (is an infusion open at the T₀ instant?), so no
norepinephrine-equivalent / weight normalization is needed — unlike the SBT
vertical's NEE stability screen. The detection logic is the instant-evaluated
analogue of ``sbt_detect.paralytic_day_flag`` (segment opens at a charted dose,
runs until the drug's next record, trailing-capped); it is reproduced here so the
proning vertical stays self-contained for federation.

Inputs:
    output/intermediate/cohort.parquet                  (T₀ + hospitalization_ids per block)
    output/intermediate/_cache/encounter_mapping.parquet (hosp → encounter_block)
    output/intermediate/_cache/resp_waterfall.parquet    (tidal_volume_set, already cached by 01)
    medication_admin_continuous                          (vasopressors + paralytics; new CLIF read)

Output:
    output/intermediate/t0_treatments.parquet  (one row per encounter_block)

No raw PHI to stdout — only counts and aggregate quantiles.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CODE_DIR = PROJECT_ROOT / "code"

log = logging.getLogger("proning.t0_treatments")

STOP_ACTION = "stop"  # mar_action_category that ends a continuous infusion


def _blk(series: pd.Series) -> pd.Series:
    """Canonical encounter_block key: integer-valued string (NaN-safe).

    cohort.parquet stores encounter_block as float64 ('18621.0'), the stitch mapping
    as int32 ('18621'); a plain .astype(str) of each diverges and silently breaks the
    join (lessons.md dtype-join trap). Normalize both sides to '18621' python strings.
    """
    s = pd.to_numeric(series, errors="coerce")
    return s.astype("Int64").astype(str).where(s.notna())


def _load_cohort_module():
    path = CODE_DIR / "01_build_cohort.py"
    spec = importlib.util.spec_from_file_location("proning_cohort", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cohort_hosp_ids(cohort: pd.DataFrame) -> list[str]:
    """All hospitalization_ids in the cohort, including stitched encounter blocks."""
    ids: set[str] = set()
    for hids in cohort["hospitalization_ids"]:
        if isinstance(hids, (list, tuple, np.ndarray)):
            ids.update(str(h) for h in hids if h is not None and str(h) != "<NA>")
        elif hids is not None and str(hids) != "<NA>":
            ids.add(str(hids))
    ids.update(cohort["hospitalization_id"].astype(str).tolist())
    return sorted(ids)


def _on_at_instant(
    mac: pd.DataFrame, cats: set[str], anchor: pd.DataFrame, tz: str, cap: timedelta
) -> pd.Series:
    """Per encounter_block: True iff any infusion in `cats` is open at the block's
    anchor time (`anchor` has columns encounter_block + 't').

    A charted record with med_dose>0 and mar_action != 'stop' opens a segment from
    admin_dttm until the same (block, drug)'s next record (trailing-capped). The
    block is flagged if any such segment covers the anchor (on_start <= t < seg_end).
    Returns a boolean Series indexed by encounter_block.
    """
    blocks = _blk(anchor["encounter_block"])
    flag = pd.Series(False, index=blocks.values)
    if mac is None or mac.empty or not cats:
        return flag

    df = mac[mac["med_category"].astype("string").str.lower().isin(cats)].copy()
    if df.empty:
        return flag
    df["encounter_block"] = _blk(df["encounter_block"])
    df["med_category"] = df["med_category"].astype("string").str.lower()
    df["admin_dttm"] = cohort_mod._coerce_dttm(df["admin_dttm"], tz)
    df = df.dropna(subset=["encounter_block", "admin_dttm"])
    df["med_dose"] = pd.to_numeric(df.get("med_dose"), errors="coerce")
    if "mar_action_category" in df.columns:
        df["__stop"] = df["mar_action_category"].astype("string").str.strip().str.lower().eq(STOP_ACTION)
    else:
        df["__stop"] = False

    # Segment end = the same (block, drug)'s next charted time, else trailing cap.
    df = df.sort_values(["encounter_block", "med_category", "admin_dttm"])
    df["seg_end"] = df.groupby(["encounter_block", "med_category"])["admin_dttm"].shift(-1)
    df["seg_end"] = df["seg_end"].fillna(df["admin_dttm"] + cap)
    on = df[(df["med_dose"].fillna(0) > 0) & (~df["__stop"]) & (df["seg_end"] > df["admin_dttm"])]

    ai = anchor[["encounter_block", "t"]].copy()
    ai["encounter_block"] = _blk(ai["encounter_block"])
    ai["t"] = cohort_mod._coerce_dttm(ai["t"], tz)
    t_by_block = ai.dropna(subset=["encounter_block"]).set_index("encounter_block")["t"]

    covered: set[str] = set()
    for blk, g in on.groupby("encounter_block", sort=False):
        tv = t_by_block.get(blk)
        if tv is None or pd.isna(tv):
            continue
        if ((g["admin_dttm"] <= tv) & (g["seg_end"] > tv)).any():
            covered.add(blk)
    flag.loc[list(covered)] = True
    return flag


def _vt_set_at(anchor: pd.DataFrame, tz: str, lookback_h: float) -> pd.Series:
    """Set tidal volume (mL) at/just before the anchor time via merge_asof backward on
    the cached waterfall (tolerance = lookback_h). `anchor` has columns encounter_block
    + 't'. Returns a float Series indexed by encounter_block."""
    blocks = _blk(anchor["encounter_block"])
    out = pd.Series(np.nan, index=blocks.values, dtype="float64")

    wf_path = cohort_mod.CACHE_DIR / "resp_waterfall.parquet"
    if not wf_path.exists():
        log.warning("resp_waterfall cache missing — vt_set_at_t0 left NaN")
        return out
    wf = pd.read_parquet(wf_path, columns=["encounter_block", "recorded_dttm", "tidal_volume_set"])
    wf = wf.dropna(subset=["encounter_block", "recorded_dttm", "tidal_volume_set"]).copy()
    if wf.empty:
        return out
    wf["encounter_block"] = _blk(wf["encounter_block"])
    wf["recorded_dttm"] = cohort_mod._coerce_dttm(wf["recorded_dttm"], tz)
    wf["tidal_volume_set"] = pd.to_numeric(wf["tidal_volume_set"], errors="coerce")
    wf = wf.dropna(subset=["tidal_volume_set", "encounter_block"]).sort_values("recorded_dttm")

    base = anchor[["encounter_block", "t"]].copy()
    base["encounter_block"] = _blk(base["encounter_block"])
    base["t"] = cohort_mod._coerce_dttm(base["t"], tz)
    base = base.dropna(subset=["encounter_block", "t"]).sort_values("t")

    merged = pd.merge_asof(
        base, wf,
        left_on="t", right_on="recorded_dttm",
        by="encounter_block", direction="backward",
        tolerance=pd.Timedelta(hours=lookback_h),
    )
    vt = merged.set_index("encounter_block")["tidal_volume_set"]
    out.loc[vt.index] = vt.values
    return out


def main() -> None:
    global cohort_mod
    cohort_mod = _load_cohort_module()
    cohort_mod._ensure_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(cohort_mod.LOGS_DIR / "02b_t0_treatments.log", mode="w"),
        ],
    )

    cfg = cohort_mod.load_config(cohort_mod.CONFIG_PATH)
    tz = cfg["timezone"]
    tcfg = cfg.get("t0_treatments", {})
    vaso_cats = {c.lower() for c in tcfg.get("vasopressor_categories", [])}
    nmb_cats = {c.lower() for c in tcfg.get("paralytic_categories", [])}
    cap = timedelta(hours=float(tcfg.get("infusion_trailing_cap_hours", 24)))
    vt_lookback = float(tcfg.get("vt_lookback_hours", 6))
    log.info("site=%s tz=%s vaso=%d cats, nmb=%d cats, vt_lookback=%.0fh",
             cfg.get("site"), tz, len(vaso_cats), len(nmb_cats), vt_lookback)

    inter = cohort_mod.INTERMEDIATE_DIR
    cohort_path = inter / "cohort.parquet"
    if not cohort_path.exists():
        raise FileNotFoundError(f"{cohort_path} not found. Run code/01_build_cohort.py first.")
    cohort = pd.read_parquet(cohort_path)
    t0 = cohort[["encounter_block", "hospitalization_id", "hospitalization_ids", "T0"]].copy()
    t0["encounter_block"] = _blk(t0["encounter_block"])

    cohort_hids = _cohort_hosp_ids(cohort)
    log.info("cohort: %d encounter_blocks, %d hospitalization_ids",
             t0["encounter_block"].nunique(), len(cohort_hids))

    # --- load medications (vasopressors + paralytics) for the cohort ----------
    all_cats = sorted(vaso_cats | nmb_cats)
    co = cohort_mod.build_orchestrator(cfg)
    co.load_table("medication_admin_continuous",
                  filters={"hospitalization_id": cohort_hids, "med_category": all_cats})
    mac = co.medication_admin_continuous.df
    if mac is None:
        mac = pd.DataFrame(columns=["hospitalization_id", "admin_dttm", "med_category", "med_dose"])
    else:
        mac = mac.copy()
    log.info("loaded medication_admin_continuous: %d rows (%d hospitalizations)",
             len(mac), mac["hospitalization_id"].nunique() if not mac.empty else 0)

    # Map hospitalization_id → encounter_block via the cached stitch mapping.
    if not mac.empty:
        mapping = pd.read_parquet(cohort_mod.CACHE_DIR / "encounter_mapping.parquet")
        h2b = (mapping.assign(hospitalization_id=lambda d: d["hospitalization_id"].astype(str))
               .set_index("hospitalization_id")["encounter_block"])
        mac["hospitalization_id"] = mac["hospitalization_id"].astype(str)
        mac["encounter_block"] = _blk(mac["hospitalization_id"].map(h2b))
        mac = mac.dropna(subset=["encounter_block"])

    # --- anchors: T₀ (all cohort blocks) and T_eligible (eligible blocks only) -
    anchor_t0 = t0[["encounter_block", "T0"]].rename(columns={"T0": "t"})
    anchor_te = pd.DataFrame(columns=["encounter_block", "t"])
    elig_path = inter / "proning_eligibility.parquet"
    if elig_path.exists():
        elig = pd.read_parquet(elig_path)
        elig = elig[elig["eligible"]] if "eligible" in elig.columns else elig
        anchor_te = elig[["encounter_block", "T_eligible"]].rename(columns={"T_eligible": "t"}).copy()
        anchor_te["encounter_block"] = _blk(anchor_te["encounter_block"])
        anchor_te = anchor_te.dropna(subset=["encounter_block", "t"])
    else:
        log.warning("proning_eligibility.parquet missing — T_eligible treatment columns left empty; "
                    "run code/02_proning_eligibility.py first")

    out = pd.DataFrame({"encounter_block": t0["encounter_block"].astype(str).values})

    def _attach(suffix, anchor):
        vaso = _on_at_instant(mac, vaso_cats, anchor, tz, cap)
        nmb = _on_at_instant(mac, nmb_cats, anchor, tz, cap)
        vt = _vt_set_at(anchor, tz, vt_lookback)
        out[f"on_vasopressor_at_{suffix}"] = out["encounter_block"].map(vaso).fillna(False).astype(bool)
        out[f"on_nmb_at_{suffix}"] = out["encounter_block"].map(nmb).fillna(False).astype(bool)
        out[f"vt_set_at_{suffix}"] = out["encounter_block"].map(vt).astype("float64")

    _attach("t0", anchor_t0)
    _attach("te", anchor_te)

    out_path = inter / "t0_treatments.parquet"
    out.to_parquet(out_path, index=False)

    n = len(out)
    n_te = len(anchor_te)
    for suffix, scope, denom in [("t0", "T₀ (all cohort)", n), ("te", "T_eligible (eligible)", n_te)]:
        n_vaso = int(out[f"on_vasopressor_at_{suffix}"].sum())
        n_nmb = int(out[f"on_nmb_at_{suffix}"].sum())
        vt_ok = int(out[f"vt_set_at_{suffix}"].notna().sum())
        d = denom if denom else 1
        log.info("at %s: on vaso %d (%.1f%%); on NMB %d (%.1f%%); set Vt charted %d (%.1f%%)",
                 scope, n_vaso, 100 * n_vaso / d, n_nmb, 100 * n_nmb / d, vt_ok, 100 * vt_ok / d)

    vt_ok = out["vt_set_at_t0"].notna()
    if vt_ok.any():
        q = out.loc[vt_ok, "vt_set_at_t0"].quantile([0.25, 0.5, 0.75])
        log.info("  set Vt (mL) median %.0f (IQR %.0f–%.0f)", q[0.5], q[0.25], q[0.75])
    log.info("wrote: %s", out_path.relative_to(PROJECT_ROOT.parents[1]))


if __name__ == "__main__":
    main()
