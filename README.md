# CLIF ICU Ventilator-QI Bundle Dashboard

A reproducible [CLIF](https://clif-consortium.github.io/website/) **monorepo** that builds a
glanceable **ICU ventilator / liberation QI bundle scorecard** — one tile per metric — plus a
detailed drill-down per metric. Each metric is its own self-contained pipeline that emits a small,
PHI-free **tile feed**; the scorecard is a combiner that collects them.

The bundle is **multi-site**: any CLIF site clones this repo, drops in a small per-site profile
(`sites/<site>.json`), and runs one command to produce its own scorecard under `output/<site>/`.
The **clinical definitions are shared and versioned** (`definitions/`), so numbers are comparable
across sites. It is **descriptive epidemiology only** — no outcome modeling.

![CLIF](https://img.shields.io/badge/CLIF-2.x%20(1.1.0%20validated)-blue) ![python](https://img.shields.io/badge/python-3.10%2B-blue) ![license](https://img.shields.io/badge/license-MIT-green)

**Metrics shipped today (all live):** **LPV** (lung-protective ventilation — the reference
implementation), **ARDS proning**, **SAT** (spontaneous awakening trials), **SBT** (spontaneous
breathing trials). Mobilization is a placeholder tile. The bundle has been run end-to-end on two
independent CLIF sites (UChicago v2.1.0 and MIMIC-IV CLIF v1.1.0) — see
[`docs/portability_mimic.md`](docs/portability_mimic.md).

---

## What it produces

Each build writes a self-contained, shippable bundle to **`output/<site>/dashboard/`**
(e.g. `output/uchicago/dashboard/`, `output/mimic/dashboard/`) — output is namespaced by site, so
several sites coexist in one clone:

- **`scorecard.html`** — the glanceable ICU ventilator-QI bundle scorecard (open this).
- **`lpv_dashboard.html`**, **`proning_dashboard.html`**, **`sat_dashboard.html`**,
  **`sbt_dashboard.html`** — each metric's detailed drill-down (the scorecard tiles link here).

The whole `output/<site>/dashboard/` folder travels together (the HTML files cross-link by relative
name). `lpv_dashboard.html` (~8 MB, Plotly inlined, works offline) has four tabs: **Tidal Volume**,
**Component breakdown**, **By unit & over time**, and **Distributions & cohort**.

---

## Definitions & provenance

This is what makes cross-site numbers trustworthy and auditable — if someone asks *"how was the
denominator for month X's SAT rate defined, and by which version of the code?"*, the answer is here.

### Shared, versioned definitions — `definitions/`

Every clinical knob lives in **`definitions/<metric>.json`** (`lpv`, `proning`, `sat`, `sbt`):
thresholds, hour windows, eligibility rules, and the canonical (lowercased) drug/mode category
lists. These are **identical across sites** and committed to the repo, and each carries a
**`definition_version`** string. Sites do **not** edit definitions; a site profile may only supply
*additive* `vocabulary_overrides` when its category strings differ. (LPV's `definitions/lpv.json` is
a thin stub — its thresholds are named Python constants in `metrics/lpv/code/02_features.py`; see
[Customizing](#customizing-the-lpv-analytic-choices).)

### Living methods docs — `docs/`

Human-readable methods, one per tile, that never drift from the code:

- **[`docs/README.md`](docs/README.md)** — the methods / data-dictionary index.
- **`metrics/<id>/METHODS.md`** — per-metric definitions, denominators, and data sources (prose +
  an auto-stamped facts block).
- **[`docs/scorecard_methods.md`](docs/scorecard_methods.md)** — how the combiner assembles tiles.

The facts blocks (thresholds, drug/mode lists, current headline numbers, provenance) are
**machine-stamped from `definitions/` + the tile feeds by [`docs/build_methods.py`](docs/build_methods.py)**,
which runs at the end of `run_bundle.sh` / `refresh_scorecard.sh`. So the docs are regenerated on
every build and cannot silently diverge from the pipeline.

### Provenance block — on every tile feed

Every `tile_feed_<metric>.json` carries a PHI-free provenance block, so any number on the scorecard
is traceable to the exact code and definitions that produced it:

```json
"provenance": {
  "site_id": "UChicago",        "clif_version": "2.1.0",
  "code_version": "12c3d31",    "definition_version": "sat-v1",
  "generated": "2026-07-07T17:41"
}
```

`code_version` is the git SHA; `definition_version` comes from `definitions/<metric>.json`. Pooling
across sites stays honest because a feed announces which definition/code produced it.

### Portability report

[`docs/portability_mimic.md`](docs/portability_mimic.md) is a worked second-site example — cohort
sizes, cross-site headline rates, coverage diagnostics, and the two small fixes the MIMIC run
required (both generalized).

---

## Quick start (existing profile)

```bash
# 1. Clone
git clone https://github.com/shanguleria/clif-ventilator-qi-dashboard.git && cd clif-ventilator-qi-dashboard

# 2. One shared Python environment for the whole bundle
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 3. Point a site profile at your CLIF data (definitions/ are shared — do not edit them)
cp sites/uchicago.example.json sites/uchicago.json     # then edit data_path, timezone, etc.

# 4. Build the LPV pipeline + methods docs + scorecard for that site
./run_bundle.sh --site uchicago                        # or: CLIF_SITE=uchicago ./run_bundle.sh

# 5. Open the scorecard (tiles link to each metric's drill-down)
open output/uchicago/dashboard/scorecard.html          # macOS  (Linux: xdg-open)
```

`run_bundle.sh` builds the **LPV** pipeline, regenerates the methods docs, and renders the scorecard.
**proning / sat / sbt are their own pipelines** — run their stages under the same `CLIF_SITE`
(each builds a respiratory-support waterfall on first run, ~35 min, cached thereafter); the scorecard
then collects whatever feeds exist. See [Onboarding a new site](#onboarding-a-new-site) for the full
sequence.

### The site profile — `sites/<site>.json`

| Field | Meaning |
|---|---|
| `site_id` | Your site label — appears in the dashboard title + each feed's provenance |
| `data_path` | Absolute path to your CLIF tables directory |
| `file_format` | `parquet` (default), `csv`, etc. — passed to clifpy |
| `timezone` | Your site's tz (e.g. `US/Central`), used for calendar-day binning. **Use `UTC` for date-shifted de-identified data like MIMIC** |
| `clif_version` | Your CLIF version string (descriptive; recorded in provenance) |
| `enabled_metrics` | Which metric tiles to build, e.g. `["lpv","proning","sat","sbt"]`. Omit a metric you can't support; a slot with no feed shows a "Coming soon…" placeholder |
| `unit_labels` | *(optional)* Friendly display names for specific ICU units, e.g. `{"N09S":"MICU North"}` — see [Grouping ICUs](#grouping-icus-by-specific-unit) |
| `vocabulary_overrides` | *(optional)* Additive overrides when your category strings differ from `definitions/` |

`sites/*.json` is **gitignored** so your data path stays local — commit only `sites/*.example.json`.
`definitions/` **is** committed (shared, non-secret). Ready-made examples: `sites/uchicago.example.json`,
`sites/mimic.example.json`.

### Grouping ICUs by specific unit

Every by-unit breakdown (`location_type` vs `location_name`) has a **"Group ICUs by"** toggle: **ICU type** (CLIF `location_type`, e.g.
`medical_icu`) or **Specific unit** (CLIF `location_name`, the physical unit). The specific-unit grain
is computed nested within the type, so type-level numbers never change. The toggle appears only when
at least one `location_type` splits into multiple `location_name`s. Unmapped codes show raw; add a
`unit_labels` map to your profile to show friendly names (no code change, scorecard rebuild only).

---

## Onboarding a new site

Cloning the repo onto a machine that has a new site's CLIF data and running it end-to-end:

```bash
# 1. Clone + one shared venv
git clone https://github.com/shanguleria/clif-ventilator-qi-dashboard.git && cd clif-ventilator-qi-dashboard
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. Create the site profile (definitions/ stay shared and unchanged)
cp sites/uchicago.example.json sites/site3.json         # set site_id, data_path, timezone, clif_version, enabled_metrics

# 3. CONFIRM VOCABULARY before trusting numbers (aggregate summaries only — no patient rows)
CLIF_SITE=site3 .venv/bin/python metrics/lpv/code/00_probe_missingness.py    # device/mode categories, plateau/height coverage
CLIF_SITE=site3 .venv/bin/python metrics/sat/code/00_probe_documentation.py  # med_category drug inventory

# 4. Build all metrics for the site
CLIF_SITE=site3 ./run_bundle.sh                          # LPV + methods + scorecard
for m in proning sat sbt; do                             # each ~35-min waterfall on first run, then cached
  for s in metrics/$m/code/0*.py; do CLIF_SITE=site3 .venv/bin/python "$s"; done
done
CLIF_SITE=site3 .venv/bin/python scorecard/build_scorecard.py   # collect all four feeds

# 5. Open it
open output/site3/dashboard/scorecard.html
```

**What the clone gives you:** all code + the shared `definitions/` + `docs/` + the example profiles.
**What it does *not*** (gitignored, by design): any data, `output/`, real `sites/*.json`, `.claude/`
logs, and `assets/` artwork (the scorecard falls back to inline SVG — cosmetic only), so a fresh
clone still builds.

**The one real caveat — vocabulary.** CLIF permissible values vary by site (casing, exact strings).
If a site's categories differ from `definitions/`, a metric can silently under-count. Eyeball the
probes in step 3 and, if needed, add a `vocabulary_overrides` block to the profile. Things to check:
- `respiratory_support.device_category` (`IMV`); `mode_category` controlled/support strings
- `medication_admin_continuous.med_category` sedative/paralytic/vasopressor names
- `labs.lab_category` arterial PaO₂ (`po2_arterial`); `vitals` (`spo2` / `height_cm` / `weight_kg`);
  `adt.location_category` (`icu`); `position_category` (`prone`)

**Two hard-failure gotchas:** clifpy loads files named **`clif_<table>.parquet`** — differently named
files won't load; and a very different `clif_version` can shift column names (clifpy applies its
bundled schema regardless). **Data safety:** run it on the site's machine — raw data stays local; if
you need help debugging, share only aggregate output/logs (counts, category value_counts), never rows.

See [`docs/portability_mimic.md`](docs/portability_mimic.md) for a real worked example (MIMIC needed
exactly two small, now-generalized fixes).

---

## The LPV bundle

Three components, evaluated on **mode-eligible IMV time**, time-weighted within each patient-day
(a day is "adherent" for a measure if ≥80% of its assessable time meets the threshold, with a
≥60-minute assessable floor):

1. **Tidal volume** ≤ 6 mL/kg predicted body weight (PBW) — *cutoff is adjustable in the dashboard*
2. **Plateau pressure** ≤ 30 cm H₂O — fixed
3. **Driving pressure** (∆P = Plateau − PEEP) ≤ 15 cm H₂O — fixed

Each component is reported on **its own denominator** (plus a strict joint composite), because the
components have very different missingness (Vt densely charted, plateau sparse) — a single composite
would force well-measured Vt to share plateau's small denominator. PBW uses the Devine/ARDSnet
formula from `patient.sex_category` and height (`vitals.height_cm`).

### CLIF tables required (LPV metric)

CLIF 2.x, as the `file_format` in the site profile (default `parquet`):

| Table | Used for |
|---|---|
| `patient` | `sex_category` (PBW) |
| `hospitalization` | `age_at_admission` (adult filter — the CLIF-canonical column) |
| `adt` | ICU location windows (`location_category == 'icu'`, `location_type`, `location_name`) |
| `respiratory_support` | `device_category == 'IMV'`, `mode_category`, `tidal_volume_obs/set`, `plateau_pressure_obs`, `peep_obs/set`, `fio2_set` |
| `vitals` | `vital_category == 'height_cm'` (PBW); `vital_category == 'spo2'` (severity S/F surrogate) |
| `labs` | `lab_category == 'po2_arterial'` (severity P/F ratio) |

(proning / sat / sbt use additional tables — see each metric's `METHODS.md` and `CLAUDE.md`.) Standard
CLIF mCIDE category values are assumed; outlier handling uses clifpy's built-in ranges.

### Severity stratifier (LPV)

The LPV dashboard includes a **Severity filter** ("severe respiratory failure" = P/F < 300, or the
S/F surrogate < 315 at SpO₂ ≤ 97%, **and** PEEP > 5; FiO₂/PEEP paired within a 4-hour lookback; worst
oxygenation of the day) — **not** full Berlin ARDS. Thresholds are named constants in
`metrics/lpv/code/02d_severity.py`.

---

## Repository layout

```
bundle_config.py        # site-aware config+output resolver: merges definitions/ ⊕ sites/<site>.json
definitions/            # SHARED, versioned clinical spec — thresholds, category lists, definition_version
  lpv.json  proning.json  sat.json  sbt.json
sites/                  # per-site profiles (real ones gitignored; *.example.json committed)
  uchicago.example.json  mimic.example.json
requirements.txt        # one shared venv for the whole bundle
run_bundle.sh           # one-command build for a site: LPV pipeline -> methods docs -> scorecard
refresh_scorecard.sh    # fast scorecard-only rebuild (no CLIF re-read)
contract/               # the tile-feed spec + JSON Schema (the only thing the scorecard depends on)
docs/                   # living methods/data-dictionary (build_methods.py + index + scorecard doc + portability report)
metrics/                # one folder per QI vertical (see metrics/README.md)
  lpv/      code/ ... METHODS.md   # the reference metric: 01_cohort -> 04_dashboard + 05_tile_feed
  proning/  code/ ... METHODS.md
  sat/      code/ ... METHODS.md
  sbt/      code/ ... METHODS.md
scorecard/              # build_scorecard.py — the combiner (collects feeds, renders scorecard.html)
output/<site>/          # per-site build artifacts (gitignored)
  metrics/<id>/{intermediate,final}/ ...   # each metric's outputs + tile_feed_<id>.json
  dashboard/            # the shippable bundle: scorecard.html + each metric's drill-down
```

Each metric emits `output/<site>/metrics/<id>/final/tile_feed_<id>.json` (+ its `<id>_dashboard.html`).
The combiner collects every metric in the site's `enabled_metrics`, ships the drill-downs into
`output/<site>/dashboard/`, and renders the scorecard.

## Pipeline (LPV metric + combiner)

`run_bundle.sh --site <id>` runs these in order (writing under `output/<site>/`):

| Step | Script | Output |
|---|---|---|
| 1 | `metrics/lpv/code/01_cohort.py` | adult IMV-on-ICU patient-day cohort + PBW |
| 2 | `metrics/lpv/code/02_features.py` | per-component adherence (`02_patient_day_status`, `02_intervals`) |
| 2d | `metrics/lpv/code/02d_severity.py` | severe-respiratory-failure flag per patient-day |
| 3 | `metrics/lpv/code/03_aggregate.py` | (time × unit, severity) rollups + Vt-cutoff grid |
| 4 | `metrics/lpv/code/04_dashboard.py` | `output/<site>/metrics/lpv/final/lpv_dashboard.html` |
| 5 | `metrics/lpv/code/05_tile_feed.py` | `output/<site>/metrics/lpv/final/tile_feed_lpv.json` |
| → | `docs/build_methods.py` | regenerates the METHODS docs' auto-stamped facts |
| → | `scorecard/build_scorecard.py` | **`output/<site>/dashboard/scorecard.html`** (collects every enabled metric) |

proning / sat / sbt are their own pipelines under `metrics/<id>/`; run their stages under the same
`CLIF_SITE` when their data updates (first run builds a ~35-min waterfall, cached thereafter). The
combiner just collects whatever feeds exist.

### The scorecard is a combiner, not a place to add metric logic

It is **registry-driven**: each metric is its own vertical that emits a PHI-free
`tile_feed_<metric>.json` (spec: [`contract/tile_feed_contract.md`](contract/tile_feed_contract.md),
schema: `contract/tile_feed.schema.json`). The combiner renders each through the **same tile
component** (donut + up to 3 segments + optional goal bar + sparkline) and copies its detail dashboard
into `output/<site>/dashboard/`. A coarse feed (e.g. proning is site-wide / all-time only) shows a
**`· site-wide` / `· all-time` badge** when the global Unit/Week filters are finer than it provides,
so a number is never silently mislabeled. A slot with no feed shows a **"Coming soon…"** placeholder.

So adding a metric is: *build the vertical → emit a tile feed → add its id to the site's
`enabled_metrics`* — no scorecard code change. See [`metrics/README.md`](metrics/README.md).

Tile artwork is read from `assets/<LPV|SAT|SBT|Proning|Mobilization>.png` (embedded at build time);
that folder is gitignored, and the scorecard **falls back to inline SVG icons** when absent — so a
fresh clone still builds.

### Recommended first: check your data

Before trusting the dashboard, run the **data-quality probes** (aggregated summaries only — no
patient rows), under your `CLIF_SITE`:

```bash
CLIF_SITE=<site> .venv/bin/python metrics/lpv/code/00_probe_missingness.py     # variable completeness for the IMV cohort
CLIF_SITE=<site> .venv/bin/python metrics/lpv/code/01b_cohort_assessment.py    # cohort sanity checks (after step 1)
CLIF_SITE=<site> .venv/bin/python metrics/lpv/code/02c_component_probe.py      # per-component assessability (after step 2)
```

Pay attention to **plateau-pressure completeness** and **height availability** — these drive how much
of the cohort is assessable at your site.

---

## Customizing the LPV analytic choices

The key parameters are named constants near the top of `metrics/lpv/code/02_features.py` (mirrored in
`03`/`04`):

| Parameter | Default | Where |
|---|---|---|
| Vt/kg cutoff (default; slider overrides) | 6 mL/kg | `VT_MAX_DEFAULT` |
| Plateau / driving-pressure thresholds | 30 / 15 cm H₂O | `PLATEAU_MAX`, `DP_MAX` |
| Adherence fraction / assessable floor | 80% / 60 min | `ADHERENCE_FRACTION`, `MIN_ASSESSABLE_MIN` |
| Carry-forward windows | Vt/PEEP 2 h, plateau/mode 6 h | `CF_FAST`, `CF_SLOW` |
| Eligible ventilator modes | AC-VC, PRVC, SIMV, PC | `ELIGIBLE_MODES` |
| Scorecard headline Vt cutoff / goal | 8 mL/kg / 90% | `SCORECARD_VT_CUTOFF`, `LPV_GOAL` (in `05_tile_feed.py`) |

The **6-hour plateau carry-forward** reflects a Q4-shift plateau-charting cadence; widen it if your
site charts plateau less often. Re-run `run_bundle.sh` after any change.

---

## Data safety

- The pipelines read CLIF tables but **embed only aggregated values** (rates, counts, binned
  histograms, per-period aggregated Table 1s) — **no patient-level rows**. Tile feeds carry only
  `num`/`den` counts and are PHI-checked at build time.
- `output/`, `sites/*.json`, `feeds/*.json`, and `assets/` are gitignored; nothing patient-adjacent
  is committed.
- Dashboards use real ICU **unit** labels (within-site). For audience-facing / consortium use, review
  your consortium's anonymization expectations (per-site displays should use anonymized "Site N").

## Acknowledgements

Built on the [Common Longitudinal ICU Format (CLIF)](https://clif-consortium.github.io/website/) and
the [`clifpy`](https://pypi.org/project/clifpy/) library. Licensed under MIT (see `LICENSE`).
