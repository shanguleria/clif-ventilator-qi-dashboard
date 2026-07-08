#!/usr/bin/env bash
#
# run_bundle.sh — build the ICU ventilator-QI bundle end-to-end.
#
# Runs the LPV metric pipeline, then the scorecard combiner, using the project virtualenv:
#   metrics/lpv/code/01_cohort.py    -> metrics/lpv/output/01_cohort_patient_days.parquet
#   metrics/lpv/code/02_features.py  -> metrics/lpv/output/02_patient_day_status.parquet, 02_intervals.parquet
#   metrics/lpv/code/02d_severity.py -> metrics/lpv/output/02d_severity.parquet
#   metrics/lpv/code/03_aggregate.py -> metrics/lpv/output/03_*_unit_summary.parquet, 03_vt_grid_*.parquet
#   metrics/lpv/code/04_dashboard.py -> metrics/lpv/output/final/lpv_dashboard.html
#   metrics/lpv/code/05_tile_feed.py -> metrics/lpv/output/final/tile_feed_lpv.json
#   scorecard/build_scorecard.py     -> output/dashboard/scorecard.html
#       (collects every metric listed in config 'metrics' from metrics/<id>/output/final/ — open this)
#
# Other metrics (proning, sat, ...) are their own pipelines under metrics/<id>/; run those in their
# own dir when their data updates. The combiner just collects whatever feeds already exist.
#
# Prereqs (see README.md): a .venv with requirements installed, and a config.json
# (copy config.example.json -> config.json and edit it for your site).
#
# Usage:
#   ./run_bundle.sh
#
set -euo pipefail

cd "$(dirname "$0")"

# Site selector: ./run_bundle.sh [--site <id>] (default $CLIF_SITE or uchicago). Output -> output/<site>/.
SITE="${CLIF_SITE:-uchicago}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --site) SITE="$2"; shift 2;;
    --site=*) SITE="${1#*=}"; shift;;
    *) echo "unknown arg: $1"; echo "usage: ./run_bundle.sh [--site <id>]"; exit 1;;
  esac
done
export CLIF_SITE="$SITE"

PY=".venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "ERROR: $PY not found. Create the venv first:"
  echo "    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi
if [[ ! -f "sites/$SITE.json" ]]; then
  echo "ERROR: site profile sites/$SITE.json not found. Create it (copy sites/uchicago.json)"
  echo "    and set data_path, timezone, clif_version, enabled_metrics for site '$SITE'."
  exit 1
fi
echo ">>> site: $SITE  (output -> output/$SITE/)"

steps=(
  "metrics/lpv/code/01_cohort.py"
  "metrics/lpv/code/02_features.py"
  "metrics/lpv/code/02d_severity.py"
  "metrics/lpv/code/03_aggregate.py"
  "metrics/lpv/code/04_dashboard.py"
  "metrics/lpv/code/05_tile_feed.py"
  "scorecard/build_scorecard.py"
  "docs/build_methods.py"
  "scorecard/collect_to_share.py"   # assemble PHI-free deliverables -> output/<site>/output_to_share/
)

for step in "${steps[@]}"; do
  echo ""
  echo "=================================================================="
  echo ">>> $step"
  echo "=================================================================="
  "$PY" "$step"
done

echo ""
echo "Done. Open the QI scorecard:  output/$SITE/dashboard/scorecard.html"
echo "  (the whole output/$SITE/dashboard/ folder is the shippable bundle: scorecard + per-metric drill-downs)"
echo "  Deliverables to share with the coordinating center:  output/$SITE/output_to_share/"
