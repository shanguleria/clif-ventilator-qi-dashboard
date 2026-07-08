# ARDS Proning — Methods

Methods & data-dictionary reference for the **ARDS proning** scorecard tile — the reader-facing "how is
it defined?" companion to `CLAUDE.md`. Prose is hand-written; the **stamped facts block** at the bottom
(headline rate, exact thresholds, provenance) is regenerated from `config/config.json` + the tile feed
on every rebuild.

## 1. What the tile shows
Among ARDS patients who reach **PROSEVA-strict** eligibility for prone positioning, the proportion **ever
proned** (a process floor — see caveats). Descriptive QI, not a causal study.

## 2. Research question
How often do ARDS patients eligible for proning actually receive it, and how quickly? Definitions target
PROSEVA (Guérin, NEJM 2013).

## 3. Unit of analysis
One row **per eligible encounter** (not patient-day). Cohort entry is at **T₀**, the first
ARDS-qualifying arterial blood gas; eligibility is evaluated forward from there. Time and unit are
anchored at **T_eligible** (the PROSEVA decision-point, §7).

## 4. Denominator — PROSEVA-strict eligibility
Two nested gates, both config-driven (`config/config.json`):

**(a) ARDS cohort (T₀)** — the first time point in a hospitalization meeting *all* of: age ≥ 18, on IMV
(`device_category == "imv"`), PEEP ≥ `ards_cohort.peep_min`, FiO₂ ≥ `ards_cohort.fio2_min`, and
PaO₂/FiO₂ ≤ `ards_cohort.pf_max`, in an ICU location. Berlin imaging criteria are not used (CLIF 2.1.0
lacks structured imaging); P/F ≤ 300 is the physiologic proxy. One row per patient (earliest T₀).

**(b) Proning eligibility (PROSEVA-strict, persistent re-evaluation)** — an encounter is eligible iff:
1. a **first qualifying ABG** post-T₀ meets IMV, PEEP ≥ `proning_eligibility.peep_min`, FiO₂ ≥
   `proning_eligibility.fio2_min`, P/F ≤ `proning_eligibility.pf_max` (call its time `T_first`);
2. a **second qualifying ABG** at or after `T_first + proning_eligibility.sustained_hours` (severity
   persisted past the stabilization window);
3. **no extubation event** in that stabilization window.

`T_eligible = T_first + sustained_hours` is the decision-point. Intermediate non-qualifying ABGs (during
weaning/recovery) do not disqualify. An optional **S/F surrogate onset** (`ards_cohort.use_sf_surrogate`,
default off) is a sensitivity lens only.

## 5. Numerator — proned
Reconstructed from the CLIF `position` table: sessions are contiguous runs of
`position_category == "prone"`; a gap > `proning_observation.session_gap_minutes` ends a session. The
headline numerator is **ever-proned** (any prone session). A session ≥
`proning_observation.adherent_session_hours` is the legacy "adherent" count (kept in the federation CSV
only). The dashboard instead describes the **first prone-session duration** distribution.

**IMV-era timeline:** time-to-prone clocks and first-session duration are computed over the first prone
session starting **≥ T₀**. Prone sessions before T₀ are **awake / pre-intubation proning** (COVID-era
HFNC/NIV), flagged (`awake_proned`) and excluded from timing, but still counted in ever-proned.

Observation is joined to eligibility at the **encounter_block grain** — each eligible block aggregates
over all its stitched `hospitalization_ids` (a session may be charted under any id).

## 6. Data sources (CLIF tables)
| Table | Columns / signal | Role |
|---|---|---|
| `respiratory_support` | waterfall device/mode, PEEP, FiO₂, set Vt, extubation | IMV window, eligibility physiology |
| `labs` | `po2_arterial` | P/F at T₀ and the qualifying ABGs |
| `position` | `position_category == "prone"` | prone-session detection (numerator) |
| `adt` | ICU intervals, `location_type`, `location_name` | ICU localization + unit at T_eligible |
| `medication_admin_continuous` | vasopressor + continuous-NMB presence at T₀ | PROSEVA Table-1 rows |
| `vitals` | `spo2` | S/F surrogate onset (sensitivity option, default off) |
| `patient` / `hospitalization` | demographics, timing, `death_dttm` | cohort framing / censoring |

## 7. Time-period & unit slicing
- **Time anchor = T_eligible.** Period keys: year `"YYYY"`, month `"YYYY-MM"`, ISO week `"YYYY-Www"`;
  each partitions the cohort exactly. **Published tile grain is month-coarse** (`periods = ["all","month"]`)
  by design — with a small eligible cohort over many years, ~96% of weeks have < 10 eligible. On a
  scorecard **week** pick the combiner resolves proning to that week's *containing month*.
- **Unit = ICU `location_type` at T_eligible** (or specific `location_name` via the toggle), from a
  DuckDB range-join on stitched `adt`. T_eligible in a non-ICU gap → `"unknown"` (folded into `__ALL__`).
- Config: `reporting.{unit_attribution_anchor, small_cell_min_den}`.

## 8. Caveats
- **Ever-proned is a process floor.** This site's `position` table charts only proning episodes, so
  patients with no position record are imputed not-proned; the true rate is ≥ the reported one.
- **Awake proning** (pre-T₀, COVID-era) is flagged and excluded from timing but kept in ever-proned.
- Fine-slice stats (Table 1, time-to-prone CDF) stay site-wide/all-time at this N.

## 9. Definition provenance & change log
Stamped below. `definition_version` (`proning-v1`) bumps only when the eligibility/denominator
definition changes.

<!-- AUTOGEN:START -->

*The block below is machine-generated by `docs/build_methods.py` from the shared `definitions/` + pipeline code — do not edit by hand; edits are overwritten on the next refresh. It holds only site-invariant **definitions**; current per-site numbers live in each tile feed + `output/<site>/output_to_share/manifest.json`.*

**Tile:** ARDS Proning — PROSEVA-eligible ARDS, proned  
**Drill-down:** `proning_dashboard.html`  
**Definition version:** `proning-v1`

**Headline definition:** ever proned — denominator: of PROSEVA-eligible (unit = patients). _Current per-site value: see the tile feed / `output_to_share/manifest.json`._

**Definitional parameters** (from `definitions/proning.json`):

| Parameter | Value |
|---|---|
| `definition_version` | proning-v1 |
| `ards_cohort.pf_max` | 300 |
| `ards_cohort.fio2_min` | 0.4 |
| `ards_cohort.peep_min` | 5 |
| `ards_cohort.use_sf_surrogate` | false |
| `ards_cohort.sf_max` | 315 |
| `ards_cohort.spo2_max` | 97 |
| `proning_eligibility.pf_max` | 150 |
| `proning_eligibility.fio2_min` | 0.6 |
| `proning_eligibility.peep_min` | 5 |
| `proning_eligibility.sustained_hours` | 12 |
| `t0_treatments.vasopressor_categories` | norepinephrine, epinephrine, phenylephrine, dopamine, vasopressin, angiotensin |
| `t0_treatments.paralytic_categories` | cisatracurium, rocuronium, vecuronium, atracurium, pancuronium |
| `t0_treatments.infusion_trailing_cap_hours` | 24 |
| `t0_treatments.vt_lookback_hours` | 6 |
| `proning_observation.session_gap_minutes` | 60 |
| `proning_observation.adherent_session_hours` | 16 |
| `reporting.unit_attribution_anchor` | T_eligible |
| `reporting.small_cell_min_den` | 10 |

**Tile caveat:** a per-site caveat note is shown on the scorecard tile and stored in the tile feed (`note`); it may quote that site's coverage figures. See the hand-written Caveats section above for the site-invariant version.

<!-- AUTOGEN:END -->
