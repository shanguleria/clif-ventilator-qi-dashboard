# Portability validation — MIMIC-IV CLIF (second-site test)

**Result: the QI bundle ran end-to-end on a second, independent CLIF site (MIMIC-IV CLIF v1.1.0),
producing all four metric tiles with only two small, inert code fixes.** This validates the
multi-site architecture (`bundle_config.py` + `definitions/` + `sites/<site>.json` → `output/<site>/`)
and de-risks consortium recruitment: a new site is a new `sites/<site>.json` profile plus, at most, a
handful of vocabulary/robustness fixes.

Run date: 2026-07-08. Home site: UChicago (CLIF v2.1.0). Validation site: MIMIC-IV CLIF v1.1.0
(MIMIC-IV-Ext-CLIF, derived from MIMIC-IV v3.1).

## Cross-site headline rates

| Metric | Definition (headline) | MIMIC | UChicago |
|---|---|---|---|
| LPV | Tidal volume ≤ 8 mL/kg PBW, of assessable vent-ICU patient-days | **67.5%** (58,656/86,917) | 83.1% (61,305/73,739) |
| Proning | Ever proned, of PROSEVA-eligible encounters | **17.0%** (523/3,080) | 18.9% (350/1,854) |
| SAT | SAT performed, of eligible vent-sedation days | **58.9%** (57,251/97,264) | 41.8% (25,277/60,429) |
| SBT | SBT delivered, of eligible transition-candidate days | **44.7%** (12,846/28,735) | 25.3% (8,706/34,439) |

<!-- src: MIMIC output/mimic/metrics/<m>/final/tile_feed_<m>.json headline.cells.__ALL__.all; UChicago output/uchicago/... -->

MIMIC vent-ICU cohort (LPV universe): 154,575 patient-days / 34,874 hospitalizations / 31,285 patients
<!-- src: output/mimic/metrics/lpv/01_cohort_summary.json -->.

**Interpretation caveat (central to any manuscript):** these are *process rates bounded by
documentation*, not pure quality signals. Cross-site differences partly reflect charting practice, not
just care — e.g. plateau pressure and height are charted at different completeness across sites, and SAT/
SBT delivery is a lower bound under hourly charting. The coverage diagnostics below must travel with the
rates.

## Portability findings (what differed, what broke)

Vocabulary was remarkably close — MIMIC's `device_category` (`IMV`), `med_category`, `vital_category`,
`lab_category` (`po2_arterial`), and `position_category` (`prone`) matched UChicago's definitions with no
overrides needed. The only real deltas were data-access + two code assumptions:

1. **Age computation (LPV).** LPV computed age as `admission_dttm − birth_date`; MIMIC's `birth_date` is
   100% null and its `admission_dttm` is tz-aware while `birth_date` was tz-naive (subtracting the mix
   raises). **Fix:** use the CLIF-canonical `age_at_admission` (fully populated at both sites; the other
   three verticals already use it) — verified to give the identical 166,814 adults at UChicago
   (`metrics/lpv/code/01_cohort.py`). Inert.
2. **Scorecard verification (combiner).** `scorecard/build_scorecard.py` asserted UChicago-specific LPV
   magic numbers (Vt ~83%, plateau ~85.8%, dP ~48%) as universal invariants → failed on any other site.
   **Fix:** gate those to `site == uchicago` as regression guards; run generic sanity (rate ∈ (0,1]) for
   all sites. Inert for UChicago.

Already handled in the refactor (committed earlier, inert): LPV `IMV` comparison made case-insensitive;
proning's `position` load guarded so a site lacking that table degrades to an empty tile instead of
crashing (MIMIC has `position`, so it ran — 3.16M rows, 8,425 `prone`).

## Data-quality / coverage diagnostics (MIMIC)

- **LPV plateau sparsity:** composite (all-three) assessable on only ~7.8% of patient-days
  <!-- src: output/mimic/metrics/lpv/03_aggregate_summary.json -->, because MIMIC charts
  `plateau_pressure_obs` on ~13.6% of respiratory_support rows. The Vt headline (den 86,917) is far
  better covered than the composite. Report component-separated (as the LPV vertical already does).
- **Height:** `height_cm` populated on ~90.8% of MIMIC vent-ICU patient-days (PBW computable) — better
  coverage than feared <!-- src: LPV 01_cohort run log -->.
- **Severity source:** P/F from `po2_arterial` on 77.8% of classified days, S/F surrogate on 22.2%
  <!-- src: output/mimic/metrics/lpv/02d_severity_summary.json -->.
- **Date de-identification:** MIMIC `*_dttm` are UTC and future-shifted per patient, so ISO-week keys
  span 2110-W02 → 2214-W18 (5,009 weeks) <!-- src: MIMIC scorecard build log -->. Bucketed in UTC
  (`sites/mimic.json` timezone). Time-series axes are not real calendar dates; within-patient intervals
  are preserved. Fine for all-time / distributional comparison; the temporal trend view is not
  cross-site-comparable on the calendar axis.

## Known limitations / follow-ups

- **LPV by-type view drops two MIMIC units.** LPV's hardcoded `UNIT_ORDER_REST` (canonical ICU-type list)
  omits MIMIC's `cvicu_icu`/`cardiac_icu`, so they fall out of the by-ICU-type breakdown (the `__ALL__`
  headline is complete; the specific-unit dimension carries all 8 MIMIC units). Follow-up: make LPV's
  unit list data-driven like the other verticals (`metrics/lpv/code/{04_dashboard,05_tile_feed}.py`).
- **LPV MIMIC drill-down HTML not built** (`04_dashboard` is slow; the scorecard drops the link
  gracefully). Re-run `metrics/lpv/code/04_dashboard.py` under `CLIF_SITE=mimic` when needed.
- proning/sat/sbt drill-downs + the MIMIC scorecard are built at `output/mimic/dashboard/`.

## How to reproduce

```bash
# one-time: create sites/mimic.json (copy sites/uchicago.example.json; set MIMIC path, timezone UTC, clif_version 1.1.0)
CLIF_SITE=mimic ./run_bundle.sh          # LPV + scorecard; or run each vertical's stages
# proning/sat/sbt build their own ~35-min waterfall on first run (cached thereafter under output/mimic/.../_cache)
CLIF_SITE=mimic python scorecard/build_scorecard.py
```

## Bottom line

The bundle is genuinely portable: a full second-site run required no definition changes and two inert
fixes, both now generalized. The site-level rates are the raw material for a consortium descriptive-QI
manuscript; the coverage diagnostics are the guardrail that keeps documentation-vs-care from being
mistaken for care-vs-care.
