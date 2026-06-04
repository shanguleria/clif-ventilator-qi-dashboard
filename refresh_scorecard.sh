#!/usr/bin/env bash
#
# refresh_scorecard.sh — re-render the QI bundle scorecard ONLY (no CLIF re-read).
#
# Re-emits the LPV tile feed from the existing parquets, then runs the scorecard combiner, which
# collects every metric in config.json -> metrics (each at metrics/<id>/output/final/tile_feed_<id>.json),
# validates each (schema_version == 1 + PHI-free), copies each feed's detail dashboard into
# output/dashboard/ so its "View details ->" link resolves, and rebuilds output/dashboard/scorecard.html.
# Reuses the existing metrics/lpv/output/*.parquet artifacts (no CLIF re-read), so it is fast and safe
# to run anytime a metric re-emits its tile feed.
#
# To rebuild the underlying LPV data first, run ./run_bundle.sh (01 -> scorecard) instead.
#
# Usage:
#   ./refresh_scorecard.sh
#
set -euo pipefail

cd "$(dirname "$0")"

PY=".venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "ERROR: $PY not found. Create the venv first:"
  echo "    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi
if [[ ! -f config.json ]]; then
  echo "ERROR: config.json not found. Copy the template and edit it:"
  echo "    cp config.example.json config.json   # then set clif_data_path, site, timezone"
  exit 1
fi
if [[ ! -f metrics/lpv/output/02_patient_day_status.parquet ]]; then
  echo "ERROR: metrics/lpv/output/02_patient_day_status.parquet not found — the LPV pipeline hasn't been built."
  echo "    Run the full pipeline first:  ./run_bundle.sh"
  exit 1
fi

"$PY" metrics/lpv/code/05_tile_feed.py    # re-emit the LPV feed from current parquets (no CLIF read)
"$PY" scorecard/build_scorecard.py        # collect every metric feed + rebuild scorecard.html

echo ""
echo "Done. Open the QI scorecard:  output/dashboard/scorecard.html"
