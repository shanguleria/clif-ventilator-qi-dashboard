# 02 — Bundle Scorecard Tile-Feed Contract (v1)

**Status:** authoritative for the `05_scorecard.py` registry refactor and for every metric
vertical (LPV, ARDS proning, SAT, SBT, mobilization) that contributes a tile.
**Owner:** the `lpv` project (it owns the bundle scorecard). **Consumers:** any CLIF QI vertical
that wants a tile.
**Created:** 2026-06-03. Update the Change Log at the bottom on every contract change.

---

## 1. Why a contract

The bundle scorecard (`output/05_scorecard.html`) is a *combiner*: one glanceable tile per ICU
ventilator/liberation QI metric. Today only the **LPV** tile is real (computed inline in
`code/05_scorecard.py`); SAT / SBT / ARDS-proning / mobilization are styled placeholders.

We are **not** merging every metric's logic into one script, and **not** forcing them onto one
denominator (same lesson as LPV's component-separation: different metrics have wildly different
denominators, source tables, and grains). Instead each metric is its **own vertical pipeline** —
possibly its own repository — and emits a small, pre-aggregated, **PHI-free `tile_feed_<metric>.json`**.
The scorecard loops over a registry of these feeds and renders each with one shared tile component.

Adding a metric becomes: *build the vertical → emit a tile feed → add one registry entry.* Never edit
the combiner's core.

---

## 2. The shared tile component (what every feed renders into)

The existing LPV tile already defines the visual vocabulary, and the contract generalizes exactly it:

```
┌─────────────────────────┐
│        [icon]           │
│      <title>            │   ← metric title
│      <subtitle>         │   ← grey one-liner (the headline definition)
│         ╭───╮           │
│        ( 24% )  donut   │   ← headline rate (num/den), donut sub-label = headline.label
│         ╰───╯           │
│   <den_label / n>       │   ← e.g. "1,854 patients" or "12,340 patient-hours · 8,201 patient-days"
│   ▓▓▓▓░░░  Goal ≥ 90%   │   ← optional goal bar (omitted if goal == null)
│   Seg A  ▓▓▓▓░  61%     │   ← 0–3 segment mini-bars (LPV uses Plateau / ∆P / Vt-severe)
│   Seg B  ▓▓░░░  19%     │
│   ╱╲__╱╲ sparkline      │   ← optional trend over the feed's finest period grain
│            View details→│   ← optional link to the metric's own dashboard
└─────────────────────────┘
```

LPV maps onto this with: headline = Vt≤8; segments = [Plateau≤30, ∆P≤15, Vt≤8 in severe];
sparkline = weekly/monthly headline series. Proning maps on naturally: headline = adherent (or
proned) rate; segments = the alternate-denominator bound and/or "ever proned"; no goal bar (or a
guideline target if the user wants one).

---

## 3. File: `tile_feed_<metric>.json`

One small JSON per metric vertical. **Aggregated counts only — never any `hospitalization_id`,
`patient_id`, dates-of-service, or row-level data.** (The scorecard re-checks this at build time and
fails if those substrings appear.)

```jsonc
{
  "schema_version": 1,
  "metric_id": "proning",                 // unique slug; also the registry key + placeholder it replaces
  "title": "ARDS Proning",
  "subtitle": "PROSEVA-eligible ARDS, prone ≥16 h",
  "icon": "prone",                        // key into the scorecard ICON/illustration set (lpv/sat/sbt/prone/mob)
  "detail_href": "proning_dashboard.html",// optional; tile links here IFF that file ships alongside the scorecard
  "goal": null,                           // optional target fraction (draws goal bar); null = no goal bar
  "generated": "2026-06-03T12:00",
  "note": "UChicago position table charts only proning episodes; …",  // optional small caption under the tile

  // ---- Grain declaration: which slices this feed actually provides ----
  "grain": {
    "units":   ["__ALL__"],               // unit keys provided. ["__ALL__"] = site-wide only (no per-unit).
    "periods": ["all"]                    // subset of ["all","month","week"] provided.
  },

  // ---- Headline (the donut) ----
  "headline": {
    "label": "adherent",                  // donut sub-label
    "den_label": "of PROSEVA-eligible",   // grey line under the donut
    "n_unit": "patients",                 // noun for the denominator count ("patients" / "patient-days" …)
    "cells": {
      // cells[unitKey][periodKey] = {num, den, n}
      "__ALL__": { "all": { "num": 213, "den": 1854, "n": 1854 } }
    }
  },

  // ---- Segments: 0–3 mini-bars (optional) ----
  "segments": [
    { "key": "everprone",  "label": "Ever proned",
      "cells": { "__ALL__": { "all": { "num": 350, "den": 1854 } } } },
    { "key": "documented", "label": "Documented subset",
      "cells": { "__ALL__": { "all": { "num": 213, "den": 350 } } } }
  ]
}
```

### Field rules
- **Rate** displayed = `num / den` (null/`—` when `den == 0`). `n` is the count shown in the denominator
  line (usually `== den`, but may differ, e.g. LPV shows patient-hours too).
- **Cell keys must match the scorecard's existing bucket keys**: period `"all"`; month `"YYYY-MM"`
  (e.g. `"2023-10"`); ISO week `"YYYY-Www"` (e.g. `"2023-W42"`). Unit keys: `"__ALL__"` plus the
  canonical unit slugs (`medical_icu`, `mixed_cardiothoracic_icu`, `surgical_icu`, `mixed_neuro_icu`,
  `general_icu`, `burn_icu`).
- A feed only needs to populate the `(unit, period)` cells named in its `grain`. It does **not** need
  every unit or every period.

---

## 4. Grain fallback (how coarse tiles survive the scorecard's global filters)

The scorecard has global **Unit** and **Week/Month** chips. A coarse feed opts out gracefully:

| User selects | Feed provides | Tile shows | Badge |
|---|---|---|---|
| unit = Neuro ICU | `units: ["__ALL__"]` only | site-wide value | `· site-wide` |
| week = 2023-W42 | `periods: ["all"]` only | all-time value | `· all-time` |
| unit = All, period = all | (any) | exact value | none |

Rule: when the selected slice isn't in the feed's `grain`, the scorecard **falls back to the coarsest
cell the feed does provide** (`__ALL__` / `all`) and renders a small badge so the number is never
silently mislabeled. (This directly implements the global "no silent caps / label what was dropped"
principle.)

**Proning is expected to be coarse:** only ~350 ever-proned patients across *all* time at UChicago, so
per-unit-per-week cells would be near-empty and misleading. Ship proning as `units:["__ALL__"]`,
`periods:["all"]` (optionally add `"month"` if monthly counts stay non-trivial). LPV stays fine-grained
(`all 7 units`, `["all","month","week"]`).

---

## 5. Registry wiring (lpv side — done in the lpv chat, after proning emits its feed)

`config.json` gains an optional list of external feeds to ingest (paths relative to the lpv repo or
absolute). The LPV feed is generated by the scorecard itself; others are read from disk:

```jsonc
"scorecard_tiles": [
  "../proning/output/final/tile_feed_proning.json"
  // "../sat/output/tile_feed_sat.json", …
]
```

`05_scorecard.py` will:
1. Build the **LPV feed in-memory** from its existing rollup (refactor `counts()`/`rollup()` to emit a
   v1 feed dict instead of the bespoke LPV payload).
2. Read each path in `scorecard_tiles`; validate `schema_version == 1` and the PHI-free check.
3. Render every feed through one shared tile renderer (donut + segments + optional goal bar + sparkline
   + grain-fallback badge). Missing/invalid feed → the styled **placeholder** tile (current behavior),
   so the scorecard always builds even if a sibling project hasn't run.

This refactor is **lpv-side** and is *not* required for the proning session — proning only needs to
emit a conformant `tile_feed_proning.json`.

> **Status: implemented 2026-06-03.** `code/05_scorecard.py` is now registry-driven exactly as above
> (LPV built in-memory, external feeds read from `config.json` → `scorecard_tiles`, one shared JS tile
> renderer with grain-fallback badges, placeholder fallback). The scorecard now writes to the shippable
> bundle **`output/dashboard/`** (`scorecard.html` + `lpv_dashboard.html`), and **copies each feed's
> `detail_href` file into `output/dashboard/`** so the tile's "View details →" link resolves when the
> folder is shared. Proning (`../proning/output/final/tile_feed_proning.json`) is the first live external
> tile.

---

## 6. Handoff brief → the proning session (paste when opening the `/CLIF/proning` chat)

> Finish the proning QI pipeline. Stages 01–03 are built & verified (10,369 ARDS → 1,854
> PROSEVA-eligible → 350 ever-proned / 213 adherent ≥16 h). Remaining: **`04_metrics.py`** and
> **`05_dashboard.py`** (both stubs), per `.claude/claude-todo.md` and `plans/experimental_approach.md`.
>
> **Open decision to resolve first** (flagged at the top of that project's progress log): how
> `04_metrics.py` treats the 1,504 eligible patients with **no position data** — (A) impute
> not-proned → 18.9 % proned; (B) documented subset only → 60.9 % adherent; (C) report both as
> bounding rates *(the project's own recommendation)*. The user deferred this to the proning session —
> **ask before coding 04.**
>
> **Extra deliverable for the bundle scorecard:** in addition to the project's own metrics CSV +
> dashboard, `04_metrics.py` must emit **`output/final/tile_feed_proning.json`** conforming to
> `…/lpv/plans/02_scorecard_tile_contract.md` (this file) — schema_version 1, PHI-free, grain
> `units:["__ALL__"]`, `periods:["all"]` (add `"month"` only if monthly counts stay non-trivial).
> Map the chosen denominator framing onto: **headline donut** = the primary rate, **segments** = the
> bound(s) / "ever proned". Set `note` to the position-table coverage caveat. The lpv scorecard reads
> this file via its `scorecard_tiles` config list — no copy needed if the relative path resolves.

---

## 7. Provenance (v1.1 — pooling-ready, additive)

Each feed MAY carry a top-level `provenance` block. The scorecard ignores it; a coordinating center
REQUIRES it to trust + version what it pools. `schema_version` stays `1` (additive — existing feeds
keep working).

```jsonc
"provenance": {
  "site_id": "UChicago",            // internal label; the center anonymizes to "Site N"
  "code_version": "0922120",        // bundle git SHA at run time
  "clif_version": "2.1.0",
  "definition_version": "lpv-v1",   // bumps only when eligibility/denominator changes
  "generated": "2026-06-03T18:40"
}
```

Pooling rules (recorded so producers emit the right shape; the central aggregator itself is deferred):
- Pool at **site level** (`__ALL__` cells): `consortium_rate = Σ_site num / Σ_site den`. Per-unit
  pooling waits on cross-site ICU-taxonomy harmonization.
- Sites must share a `definition_version` to be pooled together; group by it and flag drift.
- Real `site_id` stays internal; any per-site display uses anonymized **"Site 1…N"**.

A machine-checkable JSON Schema lives beside this doc at **`contract/tile_feed.schema.json`**.

---

## Change Log
- **2026-06-03** — v1 created. Generalizes the existing LPV tile (donut + ≤3 segments + sparkline) into
  a per-metric `tile_feed_<metric>.json`; adds a `grain` declaration + global-filter fallback so coarse
  metrics (proning) coexist with fine-grained ones (LPV). Proning chosen as the first external tile;
  denominator framing deferred to the proning session.
- **2026-06-03** — §5 implemented in `code/05_scorecard.py` (registry-driven; LPV in-memory feed; external
  feeds from `config.json` → `scorecard_tiles`; one shared renderer; grain-fallback badges; placeholder
  fallback). Output moved to the shippable bundle `output/dashboard/` (`scorecard.html` +
  `lpv_dashboard.html`); each feed's `detail_href` dashboard is copied into `output/dashboard/`. Proning
  is the first live external tile (ever-proned 350/1854 = 18.9%; PHI-free, coarse grain).
- **2026-06-03** — repo became the `clif-ventilator-qi-dashboard` monorepo. The combiner is now
  `scorecard/build_scorecard.py`; it collects each metric in `config.json → metrics` (an enable-list)
  from `metrics/<id>/output/final/tile_feed_<id>.json` — **LPV included (full symmetry)**, emitted by
  `metrics/lpv/code/05_tile_feed.py`. The old `config.scorecard_tiles` path list is retired; the
  placeholder label is now "Coming soon…".
- **2026-06-03 (v1.1)** — added the optional `provenance` block (§7) + `contract/tile_feed.schema.json`.
