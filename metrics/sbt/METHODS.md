# Spontaneous Breathing Trial (SBT) — Methods

Methods & data-dictionary reference for the **SBT** scorecard tile — the reader-facing "how is it
defined?" companion to `CLAUDE.md`. Prose is hand-written; the **stamped facts block** at the bottom
(headline rate, exact thresholds, mode/drug lists, NEE factors, provenance) is regenerated from
`config.json` + the tile feed on every rebuild.

## 1. What the tile shows
Among eligible ventilated-ICU patient-days, how often an SBT — a transition from controlled ventilation
to a spontaneous support mode — was **delivered**. Descriptive QI. Definitions follow Jain et al. (CCM;
DOI 10.1097/01.ccm.0001184980.06827.9a).

## 2. Research question
Across eligible ventilated ICU patient-days, how often is an SBT actually delivered? SBT is the
breathing-trial half of the ABCDE liberation bundle and the natural pair to SAT.

## 3. Unit of analysis
One **calendar day (US/Central)** on which an `encounter_block` is on IMV and in an ICU location — the
same universe as SAT. SBT builds its **own** full ICU∩IMV respiratory-support waterfall via the
shared `common.resp_support.build_waterfall` substrate rather than reusing SAT's sedation-scoped
cache, so never-sedated ventilated patients are included.

## 4. Denominator, numerator, and the exclusion-toggle model
The dashboard presents **one** SBT rate under a **broadest-by-default** model with eight fixed-effect
exclusion toggles (`plans/04_sbt_exclusion_toggle_model.md`):

- **All toggles OFF (broadest):** numerator = any spontaneous-mode presence that day; denominator = all
  vent-ICU patient-days.
- **All toggles ON (strict, the tile headline):** strict SBT (controlled→support transition) among
  **transition-candidate** eligible days.

The eight toggles, and whether each constrains the denominator, numerator, or both:

**Candidate-day filters (denominator):**
1. exclude tracheostomy days (`sbt_eligibility.exclude_trach_days`)
2. exclude continuous-paralytic days (`sbt_eligibility.exclude_paralytic_days`; agents in
   `sbt_paralytics.paralytic_categories`)
3. require ≥ `sbt_eligibility.controlled_min_hours` of controlled ventilation accrued (cumulative since
   intubation; controlled modes in `sbt_modes.controlled_modes`)
4. require ≥ `sbt_eligibility.stability_min_hours` of stable **oxygenation** (FiO₂ ≤ `fio2_max`,
   PEEP ≤ `peep_max`, SpO₂ ≥ `spo2_min`)
5. require ≥ 2 h of **low vasopressors** (norepinephrine-equivalent ≤ `ne_equiv_max_mcg_kg_min`)
6. require a controlled→support transition (also drops days parked on a spontaneous mode with no
   transition — those are not missed SBTs)

**Trial-quality filters (numerator):**
7. require the support episode sustained ≥ `sbt_observation.support_min_minutes`
8. require low PEEP on support (≤ `sbt_observation.ps_peep_max` PS / ≤ `sbt_observation.cpap_peep_max` CPAP)

**Norepinephrine-equivalent (NEE):** clifpy has no NEE helper. Each vasopressor dose is standardized to
mcg/kg/min, then combined with published factors (`sbt_vasopressors.ne_equivalent_factors`: norepi 1,
epi 1, phenylephrine /10, dopamine /100, vasopressin ×2.5; inotropes 0). A running pressor with missing
weight makes that hour un-assessable (not silently 0).

**So the tile headline denominator** = eligible transition-candidate days (all six denominator toggles
on); the numerator = days with a strict SBT delivered. The legacy all-eligible rate is kept as a
federation row. Live values are stamped below.

## 5. Data model (how the toggles are computed live)
`02` emits 6 raw per-day denominator bits; `03` emits 8 numerator-subset bits from per-day attempt
episodes; `04` packs these into a 14-bit per-day mask and writes a per-(unit,period) mask histogram;
`05` sums numerator/denominator **live in JS** per the active toggles. Reconciliation asserts guarantee
the mask sums equal the eligible/delivered counts.

## 6. Data sources (CLIF tables)
| Table | Columns / signal | Role |
|---|---|---|
| `respiratory_support` | waterfall device (`imv`), `mode_category`, `fio2_set`, `peep_set`, `pressure_support_set`, `tracheostomy` | controlled/support modes, stability, trach |
| `medication_admin_continuous` | vasopressors (NEE) + continuous paralytics | stability screen + paralytic exclusion |
| `vitals` | `spo2` (stability) + `weight_kg` (mcg/kg/min normalization) | stability screen + NEE |
| `adt` | ICU intervals, `location_type`, `location_name` | ICU localization + unit attribution |
| `patient` / `hospitalization` | demographics, timing | cohort framing |

## 7. Time-period & unit slicing
- **Time period** keys by the patient-day's date: month `"YYYY-MM"`, ISO week `"YYYY-Www"`; published
  grain `periods = ["all","month","week"]` (SBT weekly denominators are robust).
- **Unit** = ICU `location_type` (default) or specific `location_name` (toggle), nested within type.
- Slices below `reporting.small_cell_min_den` are grayed, not hidden.

## 8. Caveats
- **Charting cadence** — a short support episode is invisible at sites charting only hourly, so delivery
  is a lower bound; `pct_native` (share of native vs scaffold rows) is surfaced as a coverage diagnostic.
- **CPAP pressure** is read from `peep_set` (CLIF has no dedicated CPAP column).
- Bolus paralytics (in `medication_admin_intermittent`) are intentionally out of scope.

## 9. Definition provenance & change log
Stamped below. `definition_version` (`sbt-v1`) bumps only when the eligibility/denominator definition
changes.

<!-- AUTOGEN:START -->

*The block below is machine-generated by `docs/build_methods.py` from the shared `definitions/` + pipeline code — do not edit by hand; edits are overwritten on the next refresh. It holds only site-invariant **definitions**; current per-site numbers live in each tile feed + `output/<site>/output_to_share/manifest.json`.*

**Tile:** Spontaneous Breathing Trial — Controlled→support transition among transition-candidate vent-days  
**Drill-down:** `sbt_dashboard.html`  
**Definition version:** `sbt-v1`

**Headline definition:** SBT delivered — denominator: of transition-candidate vent-days (unit = patient-days). _Current per-site value: see the tile feed / `output_to_share/manifest.json`._

**Segments reported:** SBT, any length, On spont · elig, On spont · all-days

**Definitional parameters** (from `definitions/sbt.json`):

| Parameter | Value |
|---|---|
| `definition_version` | sbt-v1 |
| `sbt_modes.controlled_modes` | assist control-volume control, pressure control, pressure-regulated volume control, simv |
| `sbt_modes.support_modes` | pressure support/cpap |
| `sbt_eligibility.controlled_min_hours` | 12 |
| `sbt_eligibility.stability_min_hours` | 2 |
| `sbt_eligibility.fio2_max` | 0.5 |
| `sbt_eligibility.peep_max` | 8 |
| `sbt_eligibility.spo2_min` | 88 |
| `sbt_eligibility.ne_equiv_max_mcg_kg_min` | 0.2 |
| `sbt_eligibility.exclude_trach_days` | true |
| `sbt_eligibility.exclude_paralytic_days` | true |
| `sbt_paralytics.paralytic_categories` | cisatracurium, rocuronium, vecuronium, atracurium, pancuronium |
| `sbt_vasopressors.ne_equivalent_factors.norepinephrine` | 1.0 |
| `sbt_vasopressors.ne_equivalent_factors.epinephrine` | 1.0 |
| `sbt_vasopressors.ne_equivalent_factors.phenylephrine` | 0.1 |
| `sbt_vasopressors.ne_equivalent_factors.dopamine` | 0.01 |
| `sbt_vasopressors.ne_equivalent_factors.vasopressin` | 2.5 |
| `sbt_vasopressors.ne_equivalent_factors.dobutamine` | 0.0 |
| `sbt_vasopressors.ne_equivalent_factors.milrinone` | 0.0 |
| `sbt_vasopressors.ne_equivalent_factors.isoproterenol` | 0.0 |
| `sbt_vasopressors.ne_equivalent_factors.angiotensin` | 0.0 |
| `sbt_vasopressors.vasopressin_categories` | vasopressin |
| `sbt_observation.support_min_minutes` | 2 |
| `sbt_observation.ps_peep_max` | 8 |
| `sbt_observation.cpap_peep_max` | 5 |
| `sbt_observation.require_transition` | true |
| `reporting.unit_attribution_anchor` | day_icu_location |
| `reporting.small_cell_min_den` | 10 |

**Config documentation strings** (verbatim):

> **_comment_cohort** — SBT = ventilated-ICU patient-days (IMV ∩ ICU, day-expanded). SBT builds its OWN full ICU∩IMV respiratory_support waterfall over all ICU∩IMV blocks — including never-sedated patients, exactly the population the liberal 'all IMV' denominator + 'on spontaneous mode at all' numerator need — via the shared common.resp_support.build_waterfall substrate (it does NOT reuse the SAT vertical's SAT-sedation-scoped cache). The ~35-min build is cached with a WATERFALL_VERSION sidecar and rebuilds automatically when that version bumps; no seeding knob.

> **_comment_sbt_modes** — CLIF 2.1.0 respiratory_support mode_category permissible values (lowercased by the clifpy waterfall). CONTROLLED ventilation = device imv + a controlled mode; SUPPORT (the SBT target) = pressure support/cpap. Confirm against your site's charted mode_category strings.

> **_comment_sbt_eligibility** — Jain et al. (CCM; DOI 10.1097/01.ccm.0001184980.06827.9a): a ventilated-ICU day is SBT-eligible iff >=12h controlled ventilation has accrued (cumulative-since-intubation, before the day's opportunity) AND a >=2h contiguous window that day holds FiO2<=0.50, PEEP<=8, SpO2>=88, norepinephrine-equivalent<=0.2 mcg/kg/min, AND the patient is not tracheostomized that day (trached patients excluded from numerator AND denominator). FiO2 is a fraction (waterfall scales to <=1.0).

> **_comment_sbt_paralytics** — A patient on a CONTINUOUS neuromuscular blocker (paralytic) has no respiratory drive and is categorically NOT an SBT candidate that day. With exclude_paralytic_days=true, any vent-ICU day overlapping a continuous-paralytic infusion (med_dose>0, not a 'stop' mar_action; carried forward to the next record, trailing capped at 24h) is given eligibility_status 'excluded_paralytic' -> dropped from the eligible denominator and shown as a 'not eligible / justified' reason in the decomposition (analogous to deep sedation precluding an SAT). paralytic_categories are CLIF medication_admin_continuous med_category strings (lowercased match); at UChicago the present continuous agents are cisatracurium + rocuronium. Bolus-only paralytics (e.g. succinylcholine, intubation-dose rocuronium) charted in medication_admin_intermittent are NOT captured here.

> **_comment_sbt_vasopressors** — Norepinephrine-equivalent (NEE) dose. clifpy has NO NEE helper; we standardize each vasopressor dose to mcg/min via clifpy.unit_converter.standardize_dose_to_base_units (merges weight_kg from vitals), divide by weight -> mcg/kg/min, then apply these factors and sum concurrent infusions. Factors = standard published NEE (Goradia 2021 / Kotani 2023): norepi 1, epi 1, phenylephrine/10, dopamine/100, vasopressin x2.5 (vasopressin in u/min, NOT weight-normalized). Inotropes (dobutamine/milrinone/isoproterenol) and angiotensin default to factor 0 (not pressors for this screen); set a nonzero factor to include them. Swap to Jain's exact factors here if obtained.

> **_comment_sbt_observation** — SBT delivered (numerator) = a CONTROLLED->SUPPORT mode transition that day, sustained >=2 min, with mode pressure support/cpap and PEEP<=8 (pressure-support arm) or PEEP<=5 (CPAP arm). Transition-only (user decision): a patient parked on support all day with no transition does NOT count. Detection runs on native-resolution waterfall rows; sites charting only hourly cannot resolve sub-hourly trials -> delivery is a lower bound (pct_native diagnostic surfaced).

**Tile caveat:** a per-site caveat note is shown on the scorecard tile and stored in the tile feed (`note`); it may quote that site's coverage figures. See the hand-written Caveats section above for the site-invariant version.

<!-- AUTOGEN:END -->
