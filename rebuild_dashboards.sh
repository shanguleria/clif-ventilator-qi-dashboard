#!/usr/bin/env bash
#
# rebuild_dashboards.sh — re-render every metric dashboard + the scorecard from EXISTING cached data.
#
# Use this to iterate on dashboard LAYOUT / STYLE without paying for a full pipeline run. It re-runs
# only the HTML-rendering stages (which read the already-built parquets/JSON — NO CLIF re-read, NO
# waterfall rebuild), then hands off to ./refresh_scorecard.sh to re-emit the LPV tile feed, recombine
# all feeds into the scorecard, refresh the methods docs, and re-assemble output_to_share/.
#
#   metrics/lpv/code/04_dashboard.py       -> output/<site>/metrics/lpv/final/lpv_dashboard.html
#   metrics/proning/code/05_dashboard.py   -> output/<site>/metrics/proning/final/proning_dashboard.html
#   metrics/sat/code/05_dashboard.py       -> output/<site>/metrics/sat/final/sat_dashboard.html
#   metrics/sbt/code/05_dashboard.py       -> output/<site>/metrics/sbt/final/sbt_dashboard.html
#   ./refresh_scorecard.sh                 -> output/<site>/dashboard/scorecard.html (+ methods + share)
#
# A metric whose cached inputs are absent (never built for this site) is SKIPPED with a note — but a
# metric whose inputs exist is run under `set -e`, so a genuine rendering bug you just introduced
# aborts the build loudly instead of being silently skipped.
#
# To rebuild the underlying DATA (after a CLIF refresh or a metric-logic change), run ./run_site.sh.
#
# Usage:
#   ./rebuild_dashboards.sh [--site <id>]      # or: CLIF_SITE=<id> ./rebuild_dashboards.sh
#
set -euo pipefail
cd "$(dirname "$0")"

# Site selector (default $CLIF_SITE or uchicago). Output -> output/<site>/.
SITE="${CLIF_SITE:-uchicago}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --site) SITE="$2"; shift 2;;
    --site=*) SITE="${1#*=}"; shift;;
    *) echo "unknown arg: $1"; echo "usage: ./rebuild_dashboards.sh [--site <id>]"; exit 1;;
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
  echo "ERROR: site profile sites/$SITE.json not found (copy sites/uchicago.json)."
  exit 1
fi
echo ">>> site: $SITE  (re-rendering dashboards from cached data -> output/$SITE/)"

OUT="output/$SITE/metrics"

# Each row: "<metric> <dashboard-script> <cached-input-that-must-exist>".
# The guard input is a stage-04/05-produced artifact; if it's missing the metric was never built for
# this site, so we skip it rather than crash. If it's present we render under `set -e`.
render(){
  local metric="$1" script="$2" guard="$3"
  echo ""
  echo "=================================================================="
  if [[ ! -f "$guard" ]]; then
    echo ">>> $metric — SKIPPED (cached inputs missing: $guard)"
    echo "    build it first:  ./run_site.sh --site $SITE"
    return 0
  fi
  echo ">>> $metric — $script"
  echo "=================================================================="
  "$PY" "$script"
}

render lpv     metrics/lpv/code/04_dashboard.py     "$OUT/lpv/03_monthly_unit_summary.parquet"
render proning metrics/proning/code/05_dashboard.py "$OUT/proning/intermediate/dashboard_payload.json"
render sat     metrics/sat/code/05_dashboard.py     "$OUT/sat/intermediate/metrics_patient_day_level.parquet"
render sbt     metrics/sbt/code/05_dashboard.py     "$OUT/sbt/intermediate/metrics_patient_day_level.parquet"

# Re-emit the LPV feed, recombine all feeds into the scorecard, refresh methods, re-assemble share set.
echo ""
echo "=================================================================="
echo ">>> scorecard + methods + output_to_share  (./refresh_scorecard.sh)"
echo "=================================================================="
./refresh_scorecard.sh --site "$SITE"

echo ""
echo "Done. Dashboards re-rendered from cached data (no CLIF re-read)."
echo "  Scorecard:     output/$SITE/dashboard/scorecard.html"
echo "  Deliverables:  output/$SITE/output_to_share/"
