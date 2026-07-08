# CLIF ICU Ventilator-QI Bundle Dashboard

*ICU ventilator / liberation quality-improvement bundle — lung-protective ventilation, ARDS proning,
and spontaneous awakening/breathing trials — as a glanceable multi-site scorecard.*

A reproducible [CLIF](https://clif-consortium.github.io/website/) **monorepo**: one tile per metric on a
scorecard, plus a detailed drill-down per metric. Each metric is its own self-contained pipeline that
emits a small, PHI-free **tile feed**; the scorecard is a combiner that collects them. The bundle is
**multi-site** — any CLIF site clones the repo, adds a per-site profile, and runs one command to produce
its own scorecard and a PHI-free deliverables folder under `output/<site>/`.

![CLIF](https://img.shields.io/badge/CLIF-2.x-blue) ![python](https://img.shields.io/badge/python-3.11%2B-blue) ![license](https://img.shields.io/badge/license-MIT-green)

---

## Objective

Report, as **descriptive epidemiology** (no outcome or causal modeling), how often four evidence-based
ICU ventilator / liberation practices are delivered, at the level of the ventilated ICU patient-day (or
eligible encounter), broken down by ICU unit and over time:

| Metric | Measures | Reference |
|---|---|---|
| **LPV** — Lung-Protective Ventilation | tidal volume ≤ 8 mL/kg PBW (+ plateau ≤ 30, ∆P ≤ 15 components) | ARDSNet |
| **Proning** — ARDS prone positioning | ever proned among PROSEVA-strict–eligible encounters | Guérin, NEJM 2013 |
| **SAT** — Spontaneous Awakening Trial | daily sedation interruption on eligible vent-sedation days | Kress, NEJM 2000 |
| **SBT** — Spontaneous Breathing Trial | controlled→spontaneous transition on eligible days | Jain, CCM |

Mobilization is a placeholder tile. The pipeline covers cohort derivation, per-patient-day/-encounter
feature construction, rule application, and unit × time aggregation. It has run end-to-end on two
independent CLIF sites (UChicago on CLIF 2.1.0, and MIMIC-IV-Ext-CLIF — the MIMIC→CLIF conversion,
release v1.1.0, which implements the CLIF 2.x spec — see [`docs/portability_mimic.md`](docs/portability_mimic.md)).

---

## Required CLIF tables and fields

**CLIF 2.x** (also validated on MIMIC-IV-Ext-CLIF, the MIMIC→CLIF conversion release v1.1.0), read via [`clifpy`](https://pypi.org/project/clifpy/)
as the `file_format` in the site profile (default `parquet`, files named `clif_<table>.parquet`). Standard
CLIF mCIDE category values are assumed; outlier handling uses clifpy's built-in ranges. All four metrics
process `respiratory_support` through clifpy's waterfall (so `device_category` casing is normalized) and
attribute ICU units via `adt`.

**Shared by all four metrics:**

| Table | Fields / categorical values |
|---|---|
| `respiratory_support` | `device_category == "imv"`, `mode_category`, `fio2_set`, `peep_obs`/`peep_set`, `tracheostomy`, extubation events |
| `adt` | ICU windows: `location_category == "icu"`, `location_type` (default unit grain), `location_name` (specific-unit grain) |
| `patient` | `sex_category`, `death_dttm` |
| `hospitalization` | `age_at_admission` (adult filter), admission/discharge timing |

**Additional, per metric:**

| Metric | Additional tables & fields |
|---|---|
| **LPV** | `respiratory_support`: `tidal_volume_obs`/`_set`, `plateau_pressure_obs`. `vitals`: `height_cm` (PBW), `spo2` (S/F surrogate). `labs`: `pao2` (P/F severity strata) |
| **Proning** | `labs`: `po2_arterial` (P/F gating at T₀/eligibility). `position`: `position_category == "prone"` (numerator). `medication_admin_continuous`: vasopressor + continuous-paralytic presence at T₀ (PROSEVA Table 1). `vitals`: `spo2` (optional S/F onset) |
| **SAT** | `medication_admin_continuous`: sedative/analgesic `med_category` (propofol, midazolam, fentanyl, hydromorphone, morphine, remifentanil, ketamine), `med_dose`, `mar_action` (stop/start); dexmedetomidine (ignored, may continue); continuous paralytic (excludes the day). `patient_assessments`/`vitals`: RASS (secondary validation only) |
| **SBT** | `respiratory_support`: `mode_category` controlled (AC-VC, PC, PRVC, SIMV) vs support (PS/CPAP), `pressure_support_set`, `peep_set`. `medication_admin_continuous`: vasopressors (norepinephrine-equivalent stability screen), continuous paralytics (exclusion). `vitals`: `spo2`, `weight_kg` |

If a site's category strings differ from the shared `definitions/`, add a `vocabulary_overrides` block to
its profile (see [Configuration](#configuration)); confirm first with the probes in
[Onboarding a new site](#onboarding-a-new-site).

---

## Cohort identification

Unit of analysis is the **ventilated ICU patient-day** (`hospitalization_id` × calendar day, binned in
the site timezone), except proning, which is the **eligible encounter**.

- **LPV** — adult (≥18) ICU hospitalizations with ≥1 IMV episode during an ICU window. No ARDS
  restriction. Assessability floors apply per component (a day counts only with enough charted signal).
- **Proning** — two nested gates: an **ARDS cohort** at T₀ (age ≥18, IMV, PEEP ≥5, FiO₂ ≥0.4, P/F ≤300, in
  ICU), then **PROSEVA-strict eligibility** (a first qualifying ABG with FiO₂ ≥0.6 & P/F ≤150, a second
  ≥12 h later, no extubation in between). One row per eligible encounter; ever-proned is a *process floor*
  (the `position` table charts only proning episodes).
- **SAT** — ventilated ICU patient-days with ≥1 SAT-relevant continuous sedative/analgesic infusion.
  Continuous-paralytic days and dexmedetomidine-only days are excluded.
- **SBT** — same vent-ICU-day universe (built from its own ICU∩IMV waterfall, so never-sedated patients
  are included). Strict eligibility = transition-candidate day (≥12 h controlled ventilation, a ≥2 h
  stable-physiology window, non-trach, non-paralytic, with a controlled→support transition present).

Each metric emits a PHI-free `tile_feed_<metric>.json` (num/den at every unit × period grain) plus a
detailed dashboard HTML; proning/sat/sbt also emit a federation-shareable `metrics_site_summary.csv` and
`metrics_slices.csv`. See [Where outputs land](#where-outputs-land).

---

## Configuration

Clinical definitions are **shared and versioned**; only site access differs.

- **`definitions/<metric>.json`** — the shared clinical spec (thresholds, hour windows, canonical drug/
  mode category lists), identical across sites, each with a `definition_version`. **Committed; sites do
  not edit these.** (LPV's is a stub — its thresholds are named Python constants; see
  [Customizing](#customizing-the-lpv-analytic-choices).)
- **`sites/<site>.json`** — your per-site profile. Copy an example and edit:

  ```bash
  cp sites/uchicago.example.json sites/<site>.json
  ```

  | Field | Meaning |
  |---|---|
  | `site_id` | Site label — appears in the dashboard title + each feed's provenance |
  | `data_path` | Absolute path to your CLIF tables directory |
  | `file_format` | `parquet` (default), `csv`, … — passed to clifpy |
  | `timezone` | Calendar-day binning tz (e.g. `US/Central`). **Use `UTC` for date-shifted de-identified data like MIMIC** |
  | `clif_version` | Your CLIF version string (recorded in provenance) |
  | `enabled_metrics` | Which tiles to build, e.g. `["lpv","proning","sat","sbt"]` — omit any you can't support |
  | `unit_labels` | *(optional)* friendly names for specific ICU units, e.g. `{"N09S":"MICU North"}` |
  | `vocabulary_overrides` | *(optional)* additive overrides when your category strings differ from `definitions/` |

  `sites/*.json` is **gitignored** (holds your local data path); commit only `sites/*.example.json`.
  The active site is the env var `CLIF_SITE` (default `uchicago`) or the `--site`/`-Site` runner flag.

### Where outputs land

Everything a run produces is namespaced under **`output/<site>/`** (entirely gitignored):

| Path | Contents | Shareable? |
|---|---|---|
| `output/<site>/metrics/<id>/intermediate/`, `.../final/` | per-metric working + final artifacts, **including row-level parquet** — the PHI / working space (the consortium's `output_phi` analogue) | **No** — never leaves the machine |
| `output/<site>/dashboard/` | rendered `scorecard.html` + per-metric drill-downs (aggregate-only HTML) | aggregate |
| `output/<site>/feeds/` | collected PHI-free tile feeds | yes |
| **`output/<site>/output_to_share/`** | **the deliverables a coordinating center receives**: `feeds/<site>_tile_feed_<m>.json` (poolable num/den data, hard PHI-checked), `dashboards/`, `methods/`, and `manifest.json` (versions + per-metric headline num/den + file inventory) | **yes — upload this** |

`output_to_share/` is assembled by `scorecard/collect_to_share.py` (run automatically at the end of the
runners). The feeds in it are re-checked for `hospitalization_id`/`patient_id` and the build aborts if a
row-level id ever appears. A `run_site.sh` run also writes **`output/<site>/run_timings.csv`** — per-phase
+ total wall-clock, one row per run (for reporting how long a site takes, cold vs. warm cache).

---

## Prerequisites

- **Python 3.11+** (`python3 --version`; required by the pinned `pandas` 3.0). No R required.
- One shared virtualenv for the whole bundle:

  ```bash
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\python.exe -m pip install -r requirements.txt
  ```

  The **compute stack is pinned** to validated versions in `requirements.txt` so every site computes
  identical numbers — notably [`clifpy`](https://pypi.org/project/clifpy/)`==0.4.9` (it loads CLIF tables
  + runs the respiratory-support waterfall; **later releases changed CLIF datetime tz-handling and break
  the pipeline** with "cannot subtract tz-naive and tz-aware" errors), plus pinned `pandas`/`numpy`/
  `pyarrow`/`duckdb`/`scipy`. Presentation libraries (plotly, matplotlib, …) are floored, not pinned.
- Your CLIF tables as `clif_<table>.parquet` at the profile's `data_path`.

---

## Running the pipeline

**Full timed build of all four metrics (recommended):** `run_site.sh` runs LPV + proning + sat + sbt +
scorecard end-to-end, **times each phase**, and appends per-phase + total wall-clock to
`output/<site>/run_timings.csv` (one row per run, so cold vs. warm-cache runs accumulate).

```bash
./run_site.sh --site <site>              # macOS/Linux   (Windows: .\run_site.ps1 -Site <site>)
open output/<site>/dashboard/scorecard.html
```

First run builds the ~35-min-each respiratory-support waterfalls (cached afterward), so budget ~1.5–2 h
cold, minutes warm; a timing summary prints at the end.

**LPV-only building block:** `run_bundle.sh` builds just the LPV pipeline → scorecard → methods docs →
`output_to_share/` (it's what `run_site.sh` calls for phase 1). Handy when only LPV changed:

```bash
./run_bundle.sh --site <site>            # or: CLIF_SITE=<site> ./run_bundle.sh
```

**Windows (PowerShell):**

```powershell
# first time, if scripts are blocked:  Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\run_bundle.ps1 -Site <site>
Invoke-Item output\<site>\dashboard\scorecard.html
```

**The other three metrics (proning / sat / sbt) are their own pipelines** — run their stages under the
same `CLIF_SITE` (each builds a respiratory-support waterfall on first run, ~35 min, cached thereafter):

```bash
for m in proning sat sbt; do for s in metrics/$m/code/0*.py; do CLIF_SITE=<site> .venv/bin/python "$s"; done; done
```

Then re-collect: `./refresh_scorecard.sh --site <site>` (or `.\refresh_scorecard.ps1 -Site <site>`) —
a fast scorecard + methods + `output_to_share` rebuild with **no CLIF re-read**. Full onboarding walkthrough
(including vocabulary confirmation) in [Onboarding a new site](#onboarding-a-new-site).

---

## Pipeline steps

`run_bundle.sh` / `run_bundle.ps1` run, in order (writing under `output/<site>/`):

| Step | Script | Output |
|---|---|---|
| 1 | `metrics/lpv/code/01_cohort.py` | adult IMV-on-ICU patient-day cohort + PBW |
| 2 | `metrics/lpv/code/02_features.py` | per-component adherence (`02_patient_day_status`, `02_intervals`) |
| 2d | `metrics/lpv/code/02d_severity.py` | severe-respiratory-failure flag per patient-day |
| 3 | `metrics/lpv/code/03_aggregate.py` | (time × unit, severity) rollups + Vt-cutoff grid |
| 4 | `metrics/lpv/code/04_dashboard.py` | `output/<site>/metrics/lpv/final/lpv_dashboard.html` |
| 5 | `metrics/lpv/code/05_tile_feed.py` | `output/<site>/metrics/lpv/final/tile_feed_lpv.json` |
| → | `scorecard/build_scorecard.py` | **`output/<site>/dashboard/scorecard.html`** (collects every enabled metric) |
| → | `docs/build_methods.py` | regenerates the METHODS docs' auto-stamped facts |
| → | `scorecard/collect_to_share.py` | assembles `output/<site>/output_to_share/` |

Proning / sat / sbt each follow a `01_build_cohort → 02_*_eligibility → 03_*_observation → 04_metrics →
05_dashboard` shape and emit their own `tile_feed_<id>.json` + `metrics_site_summary.csv`.

---

## Project structure

```
bundle_config.py        # site-aware config+output resolver: merges definitions/ ⊕ sites/<site>.json
definitions/            # SHARED, versioned clinical spec — thresholds, category lists, definition_version
  lpv.json  proning.json  sat.json  sbt.json
sites/                  # per-site profiles (real ones gitignored; *.example.json committed)
  uchicago.example.json  mimic.example.json
requirements.txt        # one shared venv for the whole bundle
run_site.sh / .ps1          # FULL timed build of all 4 metrics (wraps run_bundle + verticals + refresh); logs run_timings.csv
run_bundle.sh / .ps1        # LPV pipeline + scorecard + methods + output_to_share (phase 1 of run_site)
refresh_scorecard.sh / .ps1 # fast scorecard-only rebuild (no CLIF re-read)
contract/               # the tile-feed spec + JSON Schema (the only thing the scorecard depends on)
docs/                   # living methods/data-dictionary (build_methods.py + index + scorecard doc + portability report)
metrics/                # one folder per QI vertical (see metrics/README.md)
  lpv/      code/ ... METHODS.md   # reference metric: 01_cohort -> 04_dashboard + 05_tile_feed
  proning/  code/ ... METHODS.md
  sat/      code/ ... METHODS.md
  sbt/      code/ ... METHODS.md
scorecard/              # build_scorecard.py (combiner) + collect_to_share.py (deliverables assembler)
output/<site>/          # per-site build artifacts (gitignored) — see "Where outputs land"
  metrics/<id>/{intermediate,final}/ ...   # PHI/working space
  dashboard/            # scorecard.html + drill-downs
  output_to_share/      # PHI-free deliverables for the coordinating center
```

---

## Definitions & provenance

If someone asks *"how was the denominator for month X's SAT rate defined, and by which version of the
code?"*, the answer is auditable:

- **Shared definitions** (`definitions/<metric>.json`, above) carry a `definition_version`.
- **Living methods docs** — [`docs/README.md`](docs/README.md) indexes one `metrics/<id>/METHODS.md` per
  tile plus [`docs/scorecard_methods.md`](docs/scorecard_methods.md). Their facts blocks (thresholds,
  drug/mode lists, current headline numbers) are **machine-stamped from `definitions/` + the tile feeds by
  [`docs/build_methods.py`](docs/build_methods.py)** on every build, so docs never drift from code.
- **Provenance block** — every `tile_feed_<metric>.json` (and the `output_to_share/manifest.json`) carries
  `{site_id, code_version (git SHA), clif_version, definition_version, generated}`, so any scorecard number
  traces to the exact code + definitions that produced it. Pooling across sites stays honest.
- **Portability report** — [`docs/portability_mimic.md`](docs/portability_mimic.md) is a worked second-site
  example (cohort sizes, cross-site rates, coverage diagnostics, the two inert fixes MIMIC required).

---

## The LPV bundle (reference metric)

Three components, evaluated on **mode-eligible IMV time**, time-weighted within each patient-day (a day is
"adherent" for a measure if ≥80% of its assessable time meets the threshold, with a ≥60-minute assessable
floor):

1. **Tidal volume** ≤ 6 mL/kg predicted body weight (PBW) — *cutoff adjustable in the dashboard (headline
   tile uses ≤ 8)*
2. **Plateau pressure** ≤ 30 cm H₂O — fixed
3. **Driving pressure** (∆P = Plateau − PEEP) ≤ 15 cm H₂O — fixed

Each is reported on **its own denominator** (plus a strict joint composite), because the components have
very different missingness (Vt densely charted, plateau sparse). PBW uses the Devine/ARDSNet formula from
`patient.sex_category` and `vitals.height_cm`. `lpv_dashboard.html` (~8 MB, Plotly inlined, offline) has
tabs: Tidal Volume, Component breakdown, By unit & over time, Distributions & cohort.

**Severity stratifier:** a "severe respiratory failure" filter (P/F < 300, or S/F surrogate < 315 at
SpO₂ ≤ 97%, **and** PEEP > 5; worst oxygenation of the day) — not full Berlin ARDS. Thresholds are constants
in `metrics/lpv/code/02d_severity.py`.

### Customizing the LPV analytic choices

Named constants near the top of `metrics/lpv/code/02_features.py` (mirrored in `03`/`04`):

| Parameter | Default | Where |
|---|---|---|
| Vt/kg cutoff (default; slider overrides) | 6 mL/kg | `VT_MAX_DEFAULT` |
| Plateau / driving-pressure thresholds | 30 / 15 cm H₂O | `PLATEAU_MAX`, `DP_MAX` |
| Adherence fraction / assessable floor | 80% / 60 min | `ADHERENCE_FRACTION`, `MIN_ASSESSABLE_MIN` |
| Carry-forward windows | Vt/PEEP 2 h, plateau/mode 6 h | `CF_FAST`, `CF_SLOW` |
| Eligible ventilator modes | AC-VC, PRVC, SIMV, PC | `ELIGIBLE_MODES` |
| Scorecard headline Vt cutoff / goal | 8 mL/kg / 90% | `SCORECARD_VT_CUTOFF`, `LPV_GOAL` (in `05_tile_feed.py`) |

### The scorecard is a combiner, not a place to add metric logic

Registry-driven: each metric emits a PHI-free `tile_feed_<metric>.json` (spec:
[`contract/tile_feed_contract.md`](contract/tile_feed_contract.md), schema:
`contract/tile_feed.schema.json`); the combiner renders each through the same tile component and copies its
drill-down into `output/<site>/dashboard/`. A coarse feed shows a `· site-wide` / `· all-time` badge when
the global filters are finer than it provides. Adding a metric is: *build the vertical → emit a tile feed →
add its id to the site's `enabled_metrics`* — no combiner code change. See
[`metrics/README.md`](metrics/README.md). Tile artwork (`assets/`, gitignored) falls back to inline SVG when
absent, so a fresh clone still builds.

### Grouping ICUs by specific unit

Every by-unit breakdown (`location_type` vs `location_name`) has a **"Group ICUs by"** toggle: ICU type
(e.g. `medical_icu`) or specific unit (the physical `location_name`), computed nested within the type so
type-level numbers never change. The toggle appears only when a `location_type` splits into multiple
`location_name`s. Add a `unit_labels` map to your profile for friendly names.

---

## Onboarding a new site

```bash
# 1. clone + one shared venv
git clone https://github.com/shanguleria/clif-ventilator-qi-dashboard.git && cd clif-ventilator-qi-dashboard
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. create the site profile (definitions/ stay shared and unchanged)
cp sites/uchicago.example.json sites/site3.json         # set site_id, data_path, timezone, clif_version, enabled_metrics

# 3. CONFIRM VOCABULARY before trusting numbers (aggregate summaries only — no patient rows)
CLIF_SITE=site3 .venv/bin/python metrics/lpv/code/00_probe_missingness.py    # device/mode categories, plateau/height coverage
CLIF_SITE=site3 .venv/bin/python metrics/sat/code/00_probe_documentation.py  # med_category drug inventory

# 4. build all four metrics (timed; first run builds the ~35-min-each waterfalls)
./run_site.sh --site site3                               # LPV + proning + sat + sbt + scorecard; timing -> output/site3/run_timings.csv

open output/site3/dashboard/scorecard.html
```

(Windows: substitute `.\run_bundle.ps1 -Site site3` / `.\refresh_scorecard.ps1 -Site site3`, and
`.venv\Scripts\python.exe` for the per-stage calls with `$env:CLIF_SITE="site3"` set.)

**The one real caveat — vocabulary.** CLIF permissible values vary by site (casing, exact strings). If a
site's categories differ from `definitions/`, a metric can silently under-count. Eyeball the probes and, if
needed, add a `vocabulary_overrides` block to the profile. **Hard-failure gotchas:** clifpy needs files named
`clif_<table>.parquet`; a very different `clif_version` can shift column names. `docs/portability_mimic.md`
is a real worked example (MIMIC needed exactly two small, now-generalized fixes).

---

## Data safety

- Pipelines read CLIF tables but **embed only aggregated values** (rates, counts, binned histograms,
  per-period aggregated Table 1s) — no patient-level rows. Tile feeds carry only `num`/`den` counts and are
  PHI-checked at build time and again when assembled into `output_to_share/`.
- `output/`, `sites/*.json`, `feeds/*.json`, and `assets/` are gitignored; nothing patient-adjacent is
  committed. The per-metric `output/<site>/metrics/…` parquet is the PHI/working space and is never placed
  in `output_to_share/`.
- Dashboards use real within-site ICU unit labels. For audience-facing / consortium use, review your
  consortium's anonymization expectations (per-site displays should use anonymized "Site N").

## Acknowledgements

Built on the [Common Longitudinal ICU Format (CLIF)](https://clif-consortium.github.io/website/) and the
[`clifpy`](https://pypi.org/project/clifpy/) library. Licensed under MIT (see `LICENSE`).
