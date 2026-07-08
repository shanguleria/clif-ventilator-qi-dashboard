# Lung-Protective Ventilation (LPV) Adherence — Methods

Methods & data-dictionary reference for the **LPV** scorecard tile — the reader-facing "how is it
defined?" companion to `CLAUDE.md`. Prose is hand-written; the **stamped facts block** at the bottom
(thresholds + `definition_version`) is regenerated on every rebuild. It holds only **site-invariant
definitions** — the current per-site headline *number* is not stamped here (it lives on the tile + in
the tile feed / `output_to_share/manifest.json`), so the doc never churns per run or site. **LPV has no
`config.json`** — its thresholds are Python module constants, which the generator scrapes directly from
the pipeline code (so they stay live too).

## 1. What the tile shows
Adherence to lung-protective ventilation among adult ICU patients on invasive mechanical ventilation.
The tile **headline** is tidal-volume adherence at **≤ `SCORECARD_VT_CUTOFF` mL/kg PBW**; three segment
bars report plateau pressure, driving pressure, and Vt-in-severe. Descriptive epidemiology — no outcome
modeling.

## 2. Research question
How adherent is IMV care to the LPV bundle — low tidal volume, limited plateau pressure, limited driving
pressure? Definitions follow ARDSNet / Devine PBW.

## 3. Unit of analysis
The **ventilated patient-day** (per `hospitalization_id` × calendar day), built from IMV intervals in an
ICU location. Adherence is computed from per-interval time weighted within the day. Cohort = adult
(age ≥ 18) ICU hospitalizations with ≥ 1 IMV episode; no ARDS restriction.

## 4. Denominator — assessable patient-days
A measure's denominator is the patient-days that are **assessable** for that measure: a day with ≥
`MIN_ASSESSABLE_MIN` minutes of the relevant signal charted. The three components have very different
missingness, so **each is reported on its own denominator** (not forced onto a single composite
denominator):
- **Tidal volume** (`tidal_volume_obs`/`_set`, per PBW) — densely charted.
- **Plateau pressure** — manually obtained, expected sparse.
- **Driving pressure** (∆P = plateau − PEEP) — limited by plateau availability.

**Two rates are reported** (`code/03_aggregate.py`), and the drill-down shows both:
- **Assessable rate** = adherent ÷ **assessable** patient-days — the tile **headline**; "of the days we
  could evaluate, how often was ventilation lung-protective."
- **Crude adherence** = adherent ÷ **all** cohort patient-days (`n_total`) — not-assessable days stay in
  the denominator. "Credit only when documented, counted over every vent-ICU day."

The gap between them **is the measure's charting missingness**: large for sparsely-charted plateau/∆P
(few days assessable, so crude ≪ assessable), ≈0 for densely-charted tidal volume (crude ≈ assessable).
The headline uses the assessable rate; crude is the conservative companion.

**Predicted body weight (PBW, Devine/ARDSNet):** male 50 + 2.3·(height_in − 60), female 45.5 + 2.3·
(height_in − 60); height from `vitals` (`height_cm`, median per patient), converted to inches.

## 5. Numerator — adherent time
A patient-day is **adherent** for a measure when the fraction of assessable time within threshold is ≥
`ADHERENCE_FRACTION`. Thresholds:
- **Tidal volume** — headline uses ≤ `SCORECARD_VT_CUTOFF` mL/kg PBW (the scorecard tile). The
  per-dashboard pipeline default is ≤ `VT_MAX_DEFAULT` (a slider; "less negotiable" plateau/∆P are fixed).
- **Plateau pressure** ≤ `PLATEAU_MAX` cmH₂O.
- **Driving pressure** ≤ `DP_MAX` cmH₂O.
- **Vt-in-severe** — Vt ≤ cutoff restricted to severe respiratory failure (P/F ≤ `PF_THRESHOLD`, or an
  S/F surrogate ≤ `SF_THRESHOLD` at SpO₂ ≤ `SPO2_MAX_FOR_SF`, with PEEP ≥ `PEEP_MIN`; O₂/FiO₂ matched
  within `O2_FIO2_LOOKBACK`).

The **goal line** on the tile is `LPV_GOAL`.

## 6. Data sources (CLIF tables)
| Table | Columns / signal | Role |
|---|---|---|
| `respiratory_support` | `device_category == "imv"`, `tidal_volume_obs`/`_set`, `plateau_pressure_obs`, `peep_obs`/`_set`, `fio2_set`, `mode_category`, `tracheostomy` | the LPV signals |
| `vitals` | `height_cm` (PBW), `spo2` (S/F surrogate) | PBW + severity |
| `labs` | `pao2` | P/F severity strata |
| `adt` | ICU intervals, `location_type`, `location_name` | ICU localization + unit attribution |
| `patient` / `hospitalization` | `sex_category`, birth date / `age_at_admission` | PBW sex term, adult filter |

## 7. Time-period & unit slicing
- **Time period** keys by the patient-day's date: month `"YYYY-MM"` and ISO week `"YYYY-Www"`; published
  grain `periods = ["all","month","week"]`.
- **Unit** = ICU `location_type` (default, `assigned_unit` = most-IMV-rows/day) or specific
  `location_name` (`assigned_unit_name`, nested within the chosen type) via the "Group ICUs by" toggle.
  Optional friendly names via `config.json → unit_labels`.

## 8. Caveats
- **Plateau/∆P sparsity** — plateau is manually obtained; its denominator is much smaller than Vt's, so
  component-separated reporting is used (a lone composite would force dense Vt onto sparse plateau's
  denominator).
- **Vt source** — observed if present, else set.

## 9. Definition provenance & change log
Stamped below. `DEFINITION_VERSION` (`lpv-v1`) bumps only when the eligibility/denominator definition
changes. Because LPV's constants are code-scraped, a renamed constant shows as "(see code)" in the table
below rather than silently disappearing.

<!-- AUTOGEN:START -->

*The block below is machine-generated by `docs/build_methods.py` from the shared `definitions/` + pipeline code — do not edit by hand; edits are overwritten on the next refresh. It holds only site-invariant **definitions**; current per-site numbers live in each tile feed + `output/<site>/output_to_share/manifest.json`.*

**Tile:** LPV Adherence — Tidal volume ≤ 8 mL/kg PBW  
**Drill-down:** `lpv_dashboard.html`  
**Definition version:** `lpv-v1` · goal line 90%

**Headline definition:** adherent — denominator: of assessable (unit = patient-days). _Current per-site value: see the tile feed / `output_to_share/manifest.json`._

**Segments reported:** Plateau ≤ 30, ∆P ≤ 15, Vt ≤ 8 · severe

**Definitional constants** (LPV has no `config.json`; scraped from the pipeline code — the tile headline uses Vt ≤ `SCORECARD_VT_CUTOFF`, the pipeline default is Vt ≤ `VT_MAX_DEFAULT`):

| Constant | Value | Source |
|---|---|---|
| ADHERENCE_FRACTION | 0.80 | `metrics/lpv/code/02_features.py` |
| VT_MAX_DEFAULT | 6.0 | `metrics/lpv/code/02_features.py` |
| PLATEAU_MAX | 30.0 | `metrics/lpv/code/02_features.py` |
| DP_MAX | 15.0 | `metrics/lpv/code/02_features.py` |
| PF_THRESHOLD | 300.0 | `metrics/lpv/code/02d_severity.py` |
| SF_THRESHOLD | 315.0 | `metrics/lpv/code/02d_severity.py` |
| SPO2_MAX_FOR_SF | 97.0 | `metrics/lpv/code/02d_severity.py` |
| PEEP_MIN | 5.0 | `metrics/lpv/code/02d_severity.py` |
| O2_FIO2_LOOKBACK | pd.Timedelta(hours=4) | `metrics/lpv/code/02d_severity.py` |
| SCORECARD_VT_CUTOFF | 8.0 | `metrics/lpv/code/05_tile_feed.py` |
| LPV_GOAL | 0.90 | `metrics/lpv/code/05_tile_feed.py` |
| DEFINITION_VERSION | lpv-v1 | `metrics/lpv/code/05_tile_feed.py` |

<!-- AUTOGEN:END -->
