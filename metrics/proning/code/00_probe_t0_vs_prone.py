"""Diagnostic probe: why are some proned patients proned at/before T₀?

The dashboard surfaced that ~20 % of proned eligible patients have their first prone
session at or before T₀ (the first ARDS-qualifying ABG). This probes whether that is
(a) a timing artifact — T₀ (the qualifying blood gas) charted long after the patient
was already in the ICU / already proned — or (b) genuinely pre-onset proning.

Reads only derived intermediates (no raw CLIF), prints AGGREGATES ONLY (no patient rows):
    output/intermediate/metrics_patient_level.parquet  (T0, T_eligible, T_first_qualifying_abg,
                                                         first_prone_dttm, any_prone, ttp/ttp0)
    output/intermediate/cohort.parquet                 (admission_dttm, icu_in_dttm_at_t0)
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
    path = CODE_DIR / "01_build_cohort.py"
    spec = importlib.util.spec_from_file_location("proning_cohort", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pctiles(s: pd.Series, qs=(0, .01, .05, .10, .25, .50, .75, .90, .95, 1.0)) -> str:
    s = pd.to_numeric(s, errors="coerce").dropna()
    if s.empty:
        return "  (none)"
    return "  " + "  ".join(f"p{int(q*100):>2}={s.quantile(q):.1f}" for q in qs)


def _buckets(hours_before: pd.Series, edges, labels) -> None:
    """hours_before = how many hours BEFORE the anchor (positive numbers)."""
    s = pd.to_numeric(hours_before, errors="coerce").dropna()
    n = len(s)
    prev = 0.0
    for hi, lab in zip(edges, labels):
        m = (s > prev) & (s <= hi)
        print(f"    {lab:<22} {int(m.sum()):>5}  ({100*m.sum()/n:5.1f}%)")
        prev = hi
    m = s > prev
    print(f"    {'> ' + labels[-1].split('–')[-1]:<22} {int(m.sum()):>5}  ({100*m.sum()/n:5.1f}%)")


def main() -> None:
    cohort_mod = _load_cohort_module()
    cfg = cohort_mod.load_config(cohort_mod.CONFIG_PATH)
    tz = cfg["timezone"]
    inter = cohort_mod.INTERMEDIATE_DIR

    pl = pd.read_parquet(inter / "metrics_patient_level.parquet")
    cohort = pd.read_parquet(inter / "cohort.parquet")[
        ["encounter_block", "admission_dttm", "icu_in_dttm_at_t0"]].copy()
    pl["encounter_block"] = pl["encounter_block"].astype(str)
    cohort["encounter_block"] = cohort["encounter_block"].astype(str)
    pl = pl.merge(cohort, on="encounter_block", how="left")

    for c in ["T0", "T_eligible", "T_first_qualifying_abg", "first_prone_dttm",
              "admission_dttm", "icu_in_dttm_at_t0"]:
        if c in pl.columns:
            pl[c] = cohort_mod._coerce_dttm(pl[c], tz)

    proned = pl[pl["any_prone"]].copy()
    n_pr = len(proned)
    print(f"\n=== Proning-vs-T₀ probe  (proned eligible patients: {n_pr}) ===\n")

    # --- 1. how many proned at/before T0 ----------------------------------
    ttp0 = pd.to_numeric(proned["time_to_prone_from_t0_hours"], errors="coerce")
    n_before = int((ttp0 <= 0).sum())
    print(f"[1] first prone at or before T₀ (ttp0 ≤ 0): {n_before} / {n_pr} "
          f"({100*n_before/n_pr:.1f}%)")
    print("    ttp0 = hours from T₀ to first prone, percentiles (negative = before T₀):")
    print(_pctiles(ttp0))

    before = proned[ttp0 <= 0].copy()
    hours_before_t0 = -pd.to_numeric(before["time_to_prone_from_t0_hours"], errors="coerce")
    print(f"\n[2] Of those {n_before} proned-before-T₀, how long BEFORE T₀ (hours):")
    print(_pctiles(hours_before_t0))
    _buckets(hours_before_t0, [1, 6, 24, 72, 168],
             ["≤1h", "1–6h", "6–24h", "24–72h", "72–168h"])

    # --- 3. is T0 just LATE? compare T0 to admission / ICU-in -------------
    def _hrs(a, b):
        return (pl[a] - pl[b]).dt.total_seconds() / 3600.0

    print("\n[3] Is T₀ (the qualifying ABG) charted late? Across ALL proned:")
    print("    T₀ − hospital admission (h):" + _pctiles(_hrs("T0", "admission_dttm").loc[proned.index]))
    print("    T₀ − ICU-in at T₀     (h):" + _pctiles(_hrs("T0", "icu_in_dttm_at_t0").loc[proned.index]))

    # --- 4. where does first_prone fall vs admission / ICU-in ------------
    fp_adm = _hrs("first_prone_dttm", "admission_dttm").loc[before.index]
    fp_icu = _hrs("first_prone_dttm", "icu_in_dttm_at_t0").loc[before.index]
    print(f"\n[4] For the {n_before} proned-before-T₀, first prone relative to other anchors:")
    print(f"    first prone BEFORE hospital admission: {int((fp_adm < 0).sum())} "
          f"({100*(fp_adm < 0).mean():.1f}%)")
    print(f"    first prone BEFORE ICU-in (at T₀):     {int((fp_icu < 0).sum())} "
          f"({100*(fp_icu < 0).mean():.1f}%)")
    print("    first prone − hospital admission (h):" + _pctiles(fp_adm))
    print("    first prone − ICU-in at T₀     (h):" + _pctiles(fp_icu))

    # --- 5. first_prone vs T_first (first SEVERE qualifying ABG) ---------
    if "T_first_qualifying_abg" in proned.columns:
        tf = (proned["first_prone_dttm"] - proned["T_first_qualifying_abg"]).dt.total_seconds() / 3600.0
        print("\n[5] first prone − T_first (first PROSEVA-severe ABG) among proned (h):")
        print(_pctiles(tf))
        print(f"    first prone before T_first: {int((tf < 0).sum())} / {n_pr} "
              f"({100*(tf < 0).mean():.1f}%)")
    print()


if __name__ == "__main__":
    sys.exit(main())
