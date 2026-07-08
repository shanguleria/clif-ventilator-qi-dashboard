#!/usr/bin/env bash
#
# run_site.sh — FULL multi-metric build for one site, TIMED end-to-end.
#
#   phase 1  LPV pipeline + scorecard + methods + output_to_share   (run_bundle.sh)
#   phase 2  proning pipeline   (01_build_cohort .. 05_dashboard)
#   phase 3  sat pipeline
#   phase 4  sbt pipeline
#   phase 5  recombine all 4 feeds + re-assemble output_to_share     (refresh_scorecard.sh)
#
# Records per-phase + total wall-clock to output/<site>/run_timings.csv (one row per run, so cold
# vs. warm-cache runs accumulate) and prints a summary. First run builds the ~35-min-each
# respiratory-support waterfalls (cached afterward), so expect ~1.5-2 h cold, minutes warm.
#
# Usage:
#   ./run_site.sh --site <id>            # or: CLIF_SITE=<id> ./run_site.sh
#
set -euo pipefail
cd "$(dirname "$0")"

SITE="${CLIF_SITE:-uchicago}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --site) SITE="$2"; shift 2;;
    --site=*) SITE="${1#*=}"; shift;;
    *) echo "usage: ./run_site.sh [--site <id>]"; exit 1;;
  esac
done
export CLIF_SITE="$SITE"

PY=".venv/bin/python"
[[ -x "$PY" ]] || { echo "ERROR: $PY not found — create the venv: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1; }
[[ -f "sites/$SITE.json" ]] || { echo "ERROR: sites/$SITE.json not found (copy sites/uchicago.example.json)"; exit 1; }

export TIMING_SUPPRESS=1   # run_site owns timing; stop run_bundle from logging its own duplicate row

OUT="output/$SITE"; mkdir -p "$OUT"
CSV="$OUT/run_timings.csv"
[[ -f "$CSV" ]] || echo "run_started_utc,site,runner,phase,seconds,hms,git_sha" > "$CSV"

START_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
SHA="$(git rev-parse --short HEAD 2>/dev/null || echo NA)"
fmt(){ printf '%dh%02dm%02ds' $(($1/3600)) $((($1%3600)/60)) $(($1%60)); }

# bash 3.2-safe (macOS): parallel indexed arrays. Each phase is appended to the CSV the moment it
# finishes (long format), so an interrupted run still keeps whatever phases completed.
NAMES=(); SECS=()
time_phase(){
  local name="$1"; shift
  echo ""; echo "========== $name =========="
  local t0=$SECONDS
  "$@"
  local d=$((SECONDS-t0))
  NAMES+=("$name"); SECS+=("$d")
  echo "$START_UTC,$SITE,run_site,$name,$d,$(fmt "$d"),$SHA" >> "$CSV"
  echo ">>> $name: $(fmt "$d")   (logged -> $CSV)"
}
# Run a metric's numbered pipeline stages (01_ .. 05_) in order, SKIPPING 00_* probes/diagnostics
# (those are on-demand tools that may read outputs later stages produce — running them in the build
# crashes on a fresh clone). Mirrors each metric's own run_pipeline.sh. The [0-9]* glob also skips
# bare helper modules (sat_infusions.py, sbt_detect.py, sbt_vasopressors.py), which are imported, not run.
vertical(){
  local m="$1" s
  for s in metrics/"$m"/code/[0-9]*.py; do
    case "$(basename "$s")" in 00_*) continue;; esac
    "$PY" "$s"
  done
}

echo ">>> full timed run — site: $SITE   output -> $OUT/   timing -> $CSV"
T0=$SECONDS
time_phase "lpv_bundle" ./run_bundle.sh --site "$SITE"
time_phase "proning"    vertical proning
time_phase "sat"        vertical sat
time_phase "sbt"        vertical sbt
time_phase "refresh"    ./refresh_scorecard.sh --site "$SITE"
TOTAL=$((SECONDS-T0))
echo "$START_UTC,$SITE,run_site,TOTAL,$TOTAL,$(fmt "$TOTAL"),$SHA" >> "$CSV"

echo ""
echo "=================== timing (site: $SITE) ==================="
for i in "${!NAMES[@]}"; do printf '  %-12s %s\n' "${NAMES[$i]}" "$(fmt "${SECS[$i]}")"; done
printf '  %-12s %s\n' "TOTAL" "$(fmt "$TOTAL")"
echo "  logged -> $CSV   (one row per phase; open it to compare cold vs warm runs)"
echo "  scorecard -> $OUT/dashboard/scorecard.html   |   deliverables -> $OUT/output_to_share/"
