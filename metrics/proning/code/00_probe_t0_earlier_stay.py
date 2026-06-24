"""Deep dive on the 'proned before T₀' patients (esp. earlier-ICU-stay).

Questions: did they arrive proned from an outside facility? If not, what was their
oxygenation (P/F, S/F) immediately before they were proned, and WHY didn't that
timepoint qualify as T₀ (severe ARDS on IMV in ICU)?

Reads derived caches only (no raw CLIF); prints AGGREGATES ONLY.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CODE_DIR = PROJECT_ROOT / "code"


def _load_cohort_module():
    spec = importlib.util.spec_from_file_location("proning_cohort", CODE_DIR / "01_build_cohort.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def _blk(s):
    s = pd.to_numeric(s, errors="coerce")
    return s.astype("Int64").astype(str).where(s.notna())


def _pct(s, qs=(.05, .25, .50, .75, .95)):
    s = pd.to_numeric(s, errors="coerce").dropna()
    return "  (none)" if s.empty else "  " + "  ".join(f"p{int(q*100):>2}={s.quantile(q):.0f}" for q in qs)


def main() -> None:
    cm = _load_cohort_module()
    cfg = cm.load_config(cm.CONFIG_PATH); tz = cfg["timezone"]
    inter, cache = cm.INTERMEDIATE_DIR, cm.CACHE_DIR

    pl = pd.read_parquet(inter / "metrics_patient_level.parquet")
    co = pd.read_parquet(inter / "cohort.parquet")[
        ["encounter_block", "admission_dttm", "icu_in_dttm_at_t0"]]  # admission_type_category already in pl
    pl["blk"] = _blk(pl["encounter_block"]); co["blk"] = _blk(co["encounter_block"])
    pl = pl.merge(co.drop(columns="encounter_block"), on="blk", how="left")
    for c in ["T0", "first_prone_dttm", "admission_dttm", "icu_in_dttm_at_t0"]:
        pl[c] = cm._coerce_dttm(pl[c], tz)

    proned = pl[pl["any_prone"]].copy()
    before = proned[pd.to_numeric(proned["time_to_prone_from_t0_hours"], errors="coerce") <= 0].copy()
    before["earlier_stay"] = before["first_prone_dttm"] < before["icu_in_dttm_at_t0"]
    nb = len(before)
    print(f"\n=== Deep dive: proned before T₀  (n={nb} of {len(proned)} proned; "
          f"earlier ICU stay={int(before['earlier_stay'].sum())}) ===\n")

    # ---- [1] transfer / arrived-proned -----------------------------------
    print("[1] Admission type (was it an outside-facility transfer?):")
    print("    all before-T₀:   " + str(before["admission_type_category"].astype("string").str.lower()
                                        .value_counts(dropna=False).to_dict()))
    es = before[before["earlier_stay"]]
    print("    earlier-stay:    " + str(es["admission_type_category"].astype("string").str.lower()
                                        .value_counts(dropna=False).to_dict()))
    adm_to_prone = (before["first_prone_dttm"] - before["admission_dttm"]).dt.total_seconds() / 3600.0
    print(f"\n[2] Hours from hospital admission to first prone (arrived-proned proxy):")
    print("    all before-T₀:" + _pct(adm_to_prone))
    print(f"    proned ≤6h after admission:  {int((adm_to_prone <= 6).sum())} ({100*(adm_to_prone<=6).mean():.1f}%)")
    print(f"    proned ≤24h after admission: {int((adm_to_prone <= 24).sum())} ({100*(adm_to_prone<=24).mean():.1f}%)")

    # ---- build full paired oxygenation events (not ICU-restricted) -------
    mapping = pd.read_parquet(cache / "encounter_mapping.parquet")
    abg_df = pd.read_parquet(cache / "abgs.parquet")
    wf = cm._normalize_waterfall(pd.read_parquet(cache / "resp_waterfall.parquet"), tz)
    abg = cm.extract_abgs(abg_df, mapping)
    pf = cm.attach_vent_and_compute_pf(abg, wf, tz)          # encounter_block, abg_time, pao2, device, peep, fio2, pf_ratio
    pf["blk"] = _blk(pf["encounter_block"]); pf["abg_time"] = cm._coerce_dttm(pf["abg_time"], tz)

    spo2 = cm.load_spo2_cached(cm.build_orchestrator(cfg),
                               abg_df["hospitalization_id"].dropna().astype(str).unique().tolist())
    sfev = cm.extract_spo2(spo2, mapping)
    sf = cm.attach_vent_and_compute_sf(sfev, wf, tz)         # encounter_block, sf_time, spo2, device, peep, fio2, sf_ratio
    sf["blk"] = _blk(sf["encounter_block"]); sf["sf_time"] = cm._coerce_dttm(sf["sf_time"], tz)

    # ICU intervals per block (for the 'in ICU at gas time' qualify check)
    adt = pd.read_parquet(cache / "adt_stitched.parquet")
    adt["location_category"] = adt["location_category"].astype("string").str.lower()
    icu = adt[adt["location_category"] == "icu"].copy()
    icu["blk"] = _blk(icu["encounter_block"])
    icu["in_dttm"] = cm._coerce_dttm(icu["in_dttm"], tz); icu["out_dttm"] = cm._coerce_dttm(icu["out_dttm"], tz)
    icu_by = {b: g for b, g in icu.groupby("blk", sort=False)}

    def in_icu(blk, t):
        g = icu_by.get(blk)
        return False if g is None else bool(((g["in_dttm"] <= t) & (g["out_dttm"] >= t)).any())

    pf_by = {b: g.sort_values("abg_time") for b, g in pf.groupby("blk", sort=False)}
    sf_by = {b: g.sort_values("sf_time") for b, g in sf.groupby("blk", sort=False)}

    LOOK = pd.Timedelta(hours=24)
    rows = []
    for _, r in before.iterrows():
        b, fp = r["blk"], r["first_prone_dttm"]
        rec = {"earlier_stay": r["earlier_stay"]}
        g = pf_by.get(b)
        if g is not None:
            w = g[(g["abg_time"] <= fp) & (g["abg_time"] >= fp - LOOK)]
            if len(w):
                last = w.iloc[-1]
                rec.update(pf=last["pf_ratio"], pf_imv=(last["device_category"] == cm.IMV_CATEGORY),
                           pf_fio2=last["fio2_set"], pf_peep=last["peep_set"],
                           pf_icu=in_icu(b, last["abg_time"]))
        gs = sf_by.get(b)
        if gs is not None:
            w = gs[(gs["sf_time"] <= fp) & (gs["sf_time"] >= fp - LOOK)]
            if len(w):
                rec["sf"] = w.iloc[-1]["sf_ratio"]
        rows.append(rec)
    d = pd.DataFrame(rows)

    print("\n[3] Oxygenation in the 24h BEFORE first prone (nearest paired arterial gas):")
    print(f"    had an arterial P/F in that window: {int(d['pf'].notna().sum())} / {nb}")
    print("    pre-prone P/F:" + _pct(d["pf"]))
    for thr in (300, 200, 150):
        print(f"    pre-prone P/F ≤ {thr}: {int((d['pf'] <= thr).sum())} "
              f"({100*(d['pf'] <= thr).mean():.1f}% of all before-T₀)")
    print("    pre-prone S/F (SpO2/FiO2):" + _pct(d["sf"]))

    # ---- [4] why didn't the pre-prone gas qualify as T₀? ----------------
    has = d[d["pf"].notna()].copy()
    print(f"\n[4] Of the {len(has)} with a pre-prone arterial gas — why it did NOT qualify as T₀")
    print("    (T₀ needs: P/F≤300 AND IMV AND FiO2≥0.4 AND PEEP≥5 AND in ICU). Failing criteria:")
    print(f"    P/F > 300 (not severe by gas):  {int((has['pf'] > 300).sum())} ({100*(has['pf']>300).mean():.1f}%)")
    print(f"    not on IMV at the gas:          {int((~has['pf_imv'].astype(bool)).sum())} "
          f"({100*(~has['pf_imv'].astype(bool)).mean():.1f}%)")
    print(f"    FiO2 < 0.4 at the gas:          {int((has['pf_fio2'] < 0.4).sum())} ({100*(has['pf_fio2']<0.4).mean():.1f}%)")
    print(f"    PEEP < 5 at the gas:            {int((has['pf_peep'] < 5).sum())} ({100*(has['pf_peep']<5).mean():.1f}%)")
    print(f"    NOT in ICU at the gas:          {int((~has['pf_icu'].astype(bool)).sum())} "
          f"({100*(~has['pf_icu'].astype(bool)).mean():.1f}%)")
    qual = (has['pf'] <= 300) & has['pf_imv'].astype(bool) & (has['pf_fio2'] >= 0.4) & \
           (has['pf_peep'] >= 5) & has['pf_icu'].astype(bool)
    print(f"    >> pre-prone gas WOULD have qualified as T₀: {int(qual.sum())} ({100*qual.mean():.1f}%) "
          "— i.e. a T₀-anchoring miss, severe-on-vent-in-ICU before the recorded T₀")
    print()


if __name__ == "__main__":
    sys.exit(main())
