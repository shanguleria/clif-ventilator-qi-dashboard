# Spontaneous Awakening Trial (SAT) — Methods

Methods & data-dictionary reference for the **SAT** scorecard tile. This is the reader-facing "how is
it defined?" companion to the implementation-facing `CLAUDE.md`. Prose is hand-written; the **stamped
facts block** at the bottom (headline rate, exact thresholds, drug lists, provenance) is regenerated
from `config.json` + the tile feed on every scorecard rebuild, so the specifics never drift.

## 1. What the tile shows
The share of **eligible ventilated-ICU patient-days on which a Spontaneous Awakening Trial (daily
interruption of sedation) was performed.** Descriptive quality-improvement epidemiology — no outcome
modeling. See the live headline in the stamped block below.

## 2. Research question
Across eligible ventilated ICU patient-days, how often is sedation actually interrupted? Definitions
follow Kress et al. (NEJM 2000) for the daily-interruption protocol and the half-dose-restart benchmark.

## 3. Unit of analysis
One **calendar day (US/Central)** on which an `encounter_block` is simultaneously on invasive mechanical
ventilation (IMV) and in an ICU location. A vent-ICU stay spanning midnight yields one row per calendar
day; eligibility and SAT detection are attributed per day. DST fall-back days are 25 h (never hard-coded
to 24 h). Patient-level ("ever-SAT") is a secondary segment only.

## 4. Denominator — eligible SAT-opportunity days
A vent-ICU patient-day is **eligible** iff the patient is receiving **≥ 1 SAT-relevant continuous
infusion** that day, i.e. a sedative/analgesic whose rate must reach 0 for a SAT to count. The
SAT-relevant set is `sat_medications.sedative_analgesic_categories` (config). **Dexmedetomidine is
deliberately excluded** from that set — it may continue during a SAT (`sat_medications.dexmedetomidine_categories`).

Exclusions from the denominator:
- **Continuous paralytic (NMBA) days** — the one SAT safety-screen exclusion CLIF can observe
  (`sat_eligibility.exclude_paralytic_days`; agents in `sat_medications.paralytic_categories`).
- **Dex-only days** (only dexmedetomidine, nothing to interrupt) — handling set by
  `sat_eligibility.dex_only_days`.

Other classic safety-screen exclusions (active seizures, alcohol withdrawal, myocardial ischemia, raised
ICP) are **not reliably codable in CLIF**, so this is *crude eligibility, not full safety-screen-passed
eligibility* — surfaced as a caveat on the tile.

**So, "how did you define the denominator for a given month's SAT rate?"** → all vent-ICU patient-days
whose calendar date falls in that month (`YYYY-MM`, §7), on ≥ 1 drug in the SAT-relevant set, minus
paralytic and dex-only days per the config toggles above. The exact drug lists and toggle values for the
current build are in the stamped block below.

## 5. Numerator — SAT performed
On an eligible day, an interval where **all** SAT-relevant infusions are simultaneously at **rate 0**
(held) for ≥ `sat_observation.hold_min_minutes`, while the patient remains ventilated and SAT-relevant
sedation was present earlier that day. A rate-0 is read from charted dose-0 rows and explicit
`mar_action` stop/start events (`sat_observation.zero_dose_sources`). Resumption is **not** required for
the SAT to count (`sat_observation.require_resume`). Dexmedetomidine running is ignored.

Descriptive add-ons (drill-down + tile segment bars, each a % of SATs delivered): **resumed sedation**,
**not resumed that day**, **off IMV (extubated) by end of the SAT day** (alive, on the pure-IMV
timeline). The Kress dose-resumption ratio (% restarted at ≤ `sat_observation.kress_half_dose_threshold`
of prior dose) is a drill-down figure, kept off the headline.

## 6. Data sources (CLIF tables)
| Table | Columns / signal | Role |
|---|---|---|
| `medication_admin_continuous` | sedative/analgesic infusions, `med_dose`, `mar_action`; dexmedetomidine; paralytics | the SAT signal (holds) + eligibility |
| `respiratory_support` | waterfall `device_category == "imv"`, extubation | IMV window, same-day-extubation outcome |
| `adt` | `location_category == "icu"`, `location_type`, `location_name` | ICU localization + unit attribution |
| `hospitalization` | admission/discharge, `age_at_admission` | encounter framing |
| `patient` | `death_dttm` | alive-at-next-midnight for the extubation outcome |
| `vitals` / `patient_assessments` | RASS (sedation depth) | secondary validation lens only |

## 7. Time-period & unit slicing
- **Time period** keys by the patient-day's calendar date: month `"YYYY-MM"` and ISO week `"YYYY-Www"`.
  Each granularity partitions the patient-days exactly. Published grain: `periods = ["all","month","week"]`.
- **Unit** = ICU `location_type` of the day (default grouping), or specific `location_name` (the
  "Group ICUs by" toggle) nested within the type. A day with no ICU `location_type` → `"unknown"`,
  folded into `__ALL__` for the tile.
- Slices with denominator < `reporting.small_cell_min_den` are grayed, not hidden.

## 8. Caveats
- **Crude eligibility** — CLIF cannot encode all SAT safety exclusions (seizure, withdrawal, ischemia,
  ↑ICP), so the denominator over-counts truly-eligible days.
- **Charting cadence** — continuous-infusion charting is ~hourly; short holds may be missed, so the rate
  is a documented lower bound (`reporting.denominator_mode = documented_plus_bound`).
- **Dexmedetomidine** intentionally does not block a SAT.

## 9. Definition provenance & change log
Definition version, code SHA, and generated date are stamped below. The `definition_version`
(`sat-v1`) bumps only when the eligibility/denominator definition changes — that is the signal that this
prose may need a wording pass.

<!-- AUTOGEN:START -->

*The block below is machine-generated by `docs/build_methods.py` from the tile feed + config — do not edit by hand; edits are overwritten on the next refresh.*

**Tile:** Spontaneous Awakening Trial — Sedation held ≥30 min on eligible vent-sedation days  
**Drill-down:** `sat_dashboard.html`

**Current headline (all units · all time):** SAT performed = 25,277 / 60,429 = 41.8% (of eligible vent-sedation days; n_unit = patient-days)

**Provenance:**

| Field | Value |
|---|---|
| definition_version | sat-v1 |
| code_version (git SHA) | 66fd93f |
| clif_version | 2.1.0 |
| site_id | UChicago |
| generated | 2026-07-07T14:01 |

**Grain published to the scorecard:**

- Units (7): `__ALL__`, `medical_icu`, `mixed_cardiothoracic_icu`, `surgical_icu`, `mixed_neuro_icu`, `general_icu`, `burn_icu`
- Periods: `all`, `month`, `week`
- Segments: Resumed sedation, Not resumed (day), Extubated same day

**Definitional parameters** (from `metrics/sat/config.json`):

| Parameter | Value |
|---|---|
| `sat_medications.sedative_analgesic_categories` | propofol, midazolam, fentanyl, hydromorphone, morphine, remifentanil, ketamine |
| `sat_medications.dexmedetomidine_categories` | dexmedetomidine |
| `sat_medications.paralytic_categories` | cisatracurium |
| `sat_eligibility.exclude_paralytic_days` | true |
| `sat_eligibility.dex_only_days` | exclude |
| `sat_observation.hold_min_minutes` | 30 |
| `sat_observation.zero_dose_sources` | dose_zero, mar_action_stop |
| `sat_observation.require_resume` | false |
| `sat_observation.kress_half_dose_threshold` | 0.5 |
| `reporting.unit_attribution_anchor` | day_icu_location |
| `reporting.small_cell_min_den` | 10 |
| `reporting.denominator_mode` | documented_plus_bound |

**Config documentation strings** (verbatim):

> **_comment_sat_medications** — med_category values CONFIRMED by code/00_probe_documentation.py at UChicago (clif_medication_admin_continuous, 6.37M rows). SAT-relevant set = drugs whose infusion must be at rate 0 for a SAT (GABAergic sedatives + opioids). Dexmedetomidine is allowed to continue and is NOT in the SAT-relevant set. Only continuous paralytic present is cisatracurium. lorazepam/rocuronium/vecuronium are NOT charted as continuous infusions here. pentobarbital (barbiturate-coma) intentionally excluded — those patients are not SAT candidates.

> **_comment_sat_observation** — Locked 2026-06-03 from probe + user: holds directly observable (dose==0 rows 4.88% + explicit mar_action stop/start). hold_min_minutes=30 (charting ~hourly). require_resume=false (a successful SAT may not resume; resume only needed for the Kress ratio).

> **_comment_reporting** — denominator_mode=documented_plus_bound: headline = documented-SAT days / all eligible vent-sedation days (a real rate, holds observable); ONE sensitivity-bound segment among well-charted days + explicit-SAT-field cross-check.

**Tile caveat note (shown on the scorecard):**

> • Eligible = vent-ICU days on ≥1 sedative infusion (propofol/benzo/opioid), non-paralytic (57% of vent-ICU days); dexmedetomidine may continue• SAT = all SAT-relevant infusions held to rate 0 (charted dose-0 / mar stop-start)• Bars (of SATs): resumed sedation · not resumed that day · off IMV (extubated) by end of the SAT day — alive, pure-IMV timeline so ICU-transfer-while-intubated does not count• Crude screen — CLIF can't encode all SAT safety exclusions (seizure, withdrawal, ischemia, ↑ICP)

<!-- AUTOGEN:END -->
