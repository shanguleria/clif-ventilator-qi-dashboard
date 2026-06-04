# metrics/ — one folder per QI vertical

Each subfolder is a self-contained metric pipeline that runs on a site's CLIF data and emits **one
PHI-free tile feed** at `metrics/<id>/output/final/tile_feed_<id>.json` (plus an `<id>_dashboard.html`
drill-down). The bundle scorecard (`../scorecard/build_scorecard.py`) collects every metric listed in
`config.json → metrics` and renders them — it computes nothing metric-specific itself.

Current metrics:
- **`lpv/`** — lung-protective ventilation (the reference implementation: `01_cohort` → `04_dashboard`
  + `05_tile_feed`).
- **`proning/`** — ARDS proning (PROSEVA-strict eligibility).
- **`sat/`** — spontaneous awakening trials.

Each metric keeps its own `CLAUDE.md`; the bundle root `CLAUDE.md` holds shared conventions.

## Adding a metric
1. Create `metrics/<id>/` with its own pipeline (clone an existing metric's shape).
2. Emit `metrics/<id>/output/final/tile_feed_<id>.json` conforming to
   [`../contract/tile_feed_contract.md`](../contract/tile_feed_contract.md) (schema_version 1,
   PHI-free, `num`/`den` per cell, a `provenance` block). Validate against
   `../contract/tile_feed.schema.json`.
3. Add `"<id>"` to `config.json → metrics` (and a slot in `scorecard/build_scorecard.py` `TILE_ORDER`
   if it's a new tile).
4. `./run_bundle.sh` (or `./refresh_scorecard.sh`). Until the feed exists the tile shows a
   "Coming soon…" placeholder.

The seam is deliberately thin: a metric stays independent and could be split back out to its own repo
later — the contract is all the scorecard depends on.
