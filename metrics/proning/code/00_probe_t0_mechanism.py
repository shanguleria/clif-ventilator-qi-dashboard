"""Mechanism split for the "proned before T₀" subset.

Follow-up to 00_probe_t0_vs_prone.py: of the proned patients whose first prone is at/before
T₀, decompose WHY — (A) the prone belongs to an EARLIER ICU stay (multi-stay / long stitched
block), vs (B) same ICU stay but the qualifying ARTERIAL gas is missing/non-qualifying during
the pre-T₀ gap. Uses only derived caches; prints AGGREGATES ONLY (no patient rows).

    output/intermediate/metrics_patient_level.parquet
    output/intermediate/cohort.parquet                  (icu_in_dttm_at_t0, admission/discharge)
    output/intermediate/_cache/adt_stitched.parquet     (ICU intervals per block)
    output/intermediate/_cache/abgs.parquet             (arterial PaO₂ events)
    output/intermediate/_cache/encounter_mapping.parquet
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


def main() -> None:
    cm = _load_cohort_module()
    cfg = cm.load_config(cm.CONFIG_PATH); tz = cfg["timezone"]
    inter, cache = cm.INTERMEDIATE_DIR, cm.CACHE_DIR

    pl = pd.read_parquet(inter / "metrics_patient_level.parquet")
    co = pd.read_parquet(inter / "cohort.parquet")[
        ["encounter_block", "icu_in_dttm_at_t0", "admission_dttm", "discharge_dttm"]]
    pl["blk"] = _blk(pl["encounter_block"]); co["blk"] = _blk(co["encounter_block"])
    pl = pl.merge(co.drop(columns="encounter_block"), on="blk", how="left")
    for c in ["T0", "first_prone_dttm", "icu_in_dttm_at_t0", "admission_dttm", "discharge_dttm"]:
        pl[c] = cm._coerce_dttm(pl[c], tz)

    proned = pl[pl["any_prone"]].copy()
    before = proned[pd.to_numeric(proned["time_to_prone_from_t0_hours"], errors="coerce") <= 0].copy()
    nb = len(before)
    print(f"\n=== 'Proned before T₀' mechanism split  (n = {nb} of {len(proned)} proned) ===\n")

    # ---- ICU intervals per block (adt_stitched) --------------------------
    adt = pd.read_parquet(cache / "adt_stitched.parquet")
    adt["location_category"] = adt["location_category"].astype("string").str.lower()
    icu = adt[adt["location_category"] == "icu"].copy()
    icu["blk"] = _blk(icu["encounter_block"])
    icu["in_dttm"] = cm._coerce_dttm(icu["in_dttm"], tz)
    n_icu_stays = icu.groupby("blk").size()
    before["n_icu_stays"] = before["blk"].map(n_icu_stays).fillna(0).astype(int)

    # ---- (A) earlier-ICU-stay vs same-stay -------------------------------
    earlier_stay = before["first_prone_dttm"] < before["icu_in_dttm_at_t0"]
    print("[A] Earlier ICU stay vs same ICU stay (icu_in_dttm_at_t0 = the T₀ ICU interval):")
    print(f"    first prone BEFORE the T₀ ICU stay began (earlier stay): {int(earlier_stay.sum())} "
          f"({100*earlier_stay.mean():.1f}%)")
    print(f"    first prone within the T₀ ICU stay, before T₀ (same stay): {int((~earlier_stay).sum())} "
          f"({100*(~earlier_stay).mean():.1f}%)")
    print(f"    # ICU stays in the encounter block: "
          f"1 stay={int((before['n_icu_stays']==1).sum())}, "
          f"2={int((before['n_icu_stays']==2).sum())}, "
          f"≥3={int((before['n_icu_stays']>=3).sum())}")

    # ---- long-span / merged-ID flag --------------------------------------
    span_d = (before["discharge_dttm"] - before["admission_dttm"]).dt.total_seconds() / 86400.0
    print(f"\n[B] Encounter span (admission→discharge, days): "
          f"median={span_d.median():.1f}, p90={span_d.quantile(.9):.1f}, max={span_d.max():.1f}")
    print(f"    blocks with span >60d: {int((span_d>60).sum())}; >200d (likely merged-id): "
          f"{int((span_d>200).sum())}")

    # ---- (C) arterial gas in the pre-T₀ gap [first_prone, T0) ------------
    abg = pd.read_parquet(cache / "abgs.parquet")[
        ["hospitalization_id", "lab_collect_dttm", "lab_value_numeric"]].copy()
    mp = pd.read_parquet(cache / "encounter_mapping.parquet")
    mp["hospitalization_id"] = mp["hospitalization_id"].astype(str)
    abg["hospitalization_id"] = abg["hospitalization_id"].astype(str)
    abg["blk"] = _blk(abg["hospitalization_id"].map(mp.set_index("hospitalization_id")["encounter_block"]))
    abg = abg.dropna(subset=["blk", "lab_collect_dttm"])
    abg["lab_collect_dttm"] = cm._coerce_dttm(abg["lab_collect_dttm"], tz)
    abg = abg[pd.to_numeric(abg["lab_value_numeric"], errors="coerce") > 0]
    abg_by_blk = {b: g for b, g in abg.groupby("blk", sort=False)}

    n_gap_gas = []
    for _, r in before.iterrows():
        g = abg_by_blk.get(r["blk"])
        if g is None:
            n_gap_gas.append(0); continue
        m = (g["lab_collect_dttm"] >= r["first_prone_dttm"]) & (g["lab_collect_dttm"] < r["T0"])
        n_gap_gas.append(int(m.sum()))
    before["n_gap_gas"] = n_gap_gas
    no_gas = (before["n_gap_gas"] == 0)
    print(f"\n[C] Arterial PaO₂ events in the gap [first prone … T₀) — the window where they were"
          f"\n    proned but 'not yet ARDS':")
    print(f"    NO arterial gas in the gap (missing sampling):     {int(no_gas.sum())} "
          f"({100*no_gas.mean():.1f}%)")
    print(f"    ≥1 arterial gas in the gap (drawn but not P/F≤300): {int((~no_gas).sum())} "
          f"({100*(~no_gas).mean():.1f}%)")
    print(f"    gap-gas count: median={int(before['n_gap_gas'].median())}, "
          f"p90={int(before['n_gap_gas'].quantile(.9))}, max={int(before['n_gap_gas'].max())}")

    # ---- combined 2x2 (earlier-stay × gas) -------------------------------
    print("\n[D] Combined mechanism (earlier-ICU-stay × gas-in-gap):")
    for es, eslab in [(True, "earlier ICU stay"), (False, "same ICU stay ")]:
        sub = before[earlier_stay == es]
        ng = (sub["n_gap_gas"] == 0)
        print(f"    {eslab}: {len(sub):>3}  | no-gas {int(ng.sum()):>3}  | has-gas {int((~ng).sum()):>3}")
    print()


if __name__ == "__main__":
    sys.exit(main())
