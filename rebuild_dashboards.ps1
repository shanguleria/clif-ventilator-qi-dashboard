<#
rebuild_dashboards.ps1 - Windows (PowerShell) equivalent of rebuild_dashboards.sh.

Re-render every metric dashboard + the scorecard from EXISTING cached data. Use this to iterate on
dashboard LAYOUT / STYLE without a full pipeline run: it re-runs only the HTML-rendering stages (which
read already-built parquets/JSON - NO CLIF re-read, NO waterfall rebuild), then hands off to
.\refresh_scorecard.ps1 to re-emit the LPV tile feed, recombine feeds into the scorecard, refresh the
methods docs, and re-assemble output_to_share.

A metric whose cached inputs are absent (never built for this site) is SKIPPED with a note; a metric
whose inputs exist is run and a genuine rendering bug aborts the build loudly.

To rebuild the underlying DATA (after a CLIF refresh or metric-logic change), run .\run_site.ps1.

Usage:
  .\rebuild_dashboards.ps1                 # site = $env:CLIF_SITE, else 'uchicago'
  .\rebuild_dashboards.ps1 -Site mimic
#>
param([string]$Site = $(if ($env:CLIF_SITE) { $env:CLIF_SITE } else { "uchicago" }))

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$env:CLIF_SITE = $Site

$PY = Join-Path ".venv" "Scripts\python.exe"
if (-not (Test-Path $PY)) {
    Write-Host "ERROR: $PY not found. Create the venv first:"
    Write-Host "    python -m venv .venv; .venv\Scripts\python.exe -m pip install -r requirements.txt"
    exit 1
}
if (-not (Test-Path "sites\$Site.json")) {
    Write-Host "ERROR: site profile sites\$Site.json not found (copy sites\uchicago.example.json)."
    exit 1
}
Write-Host ">>> site: $Site  (re-rendering dashboards from cached data -> output/$Site/)"

$out = "output\$Site\metrics"

# metric, dashboard script, cached-input guard (a stage-04/05 artifact that must exist to render).
$dashboards = @(
    @{ Metric = "lpv";     Script = "metrics/lpv/code/04_dashboard.py";     Guard = "$out\lpv\03_monthly_unit_summary.parquet" },
    @{ Metric = "proning"; Script = "metrics/proning/code/05_dashboard.py"; Guard = "$out\proning\intermediate\dashboard_payload.json" },
    @{ Metric = "sat";     Script = "metrics/sat/code/05_dashboard.py";     Guard = "$out\sat\intermediate\metrics_patient_day_level.parquet" },
    @{ Metric = "sbt";     Script = "metrics/sbt/code/05_dashboard.py";     Guard = "$out\sbt\intermediate\metrics_patient_day_level.parquet" }
)

foreach ($d in $dashboards) {
    Write-Host ""
    Write-Host "=================================================================="
    if (-not (Test-Path $d.Guard)) {
        Write-Host (">>> {0} - SKIPPED (cached inputs missing: {1})" -f $d.Metric, $d.Guard)
        Write-Host "    build it first:  .\run_site.ps1 -Site $Site"
        continue
    }
    Write-Host (">>> {0} - {1}" -f $d.Metric, $d.Script)
    Write-Host "=================================================================="
    & $PY $d.Script
    if ($LASTEXITCODE -ne 0) {
        Write-Host ("ERROR: {0} dashboard failed (exit {1})" -f $d.Metric, $LASTEXITCODE)
        exit $LASTEXITCODE
    }
}

# Re-emit the LPV feed, recombine all feeds into the scorecard, refresh methods, re-assemble share set.
Write-Host ""
Write-Host "=================================================================="
Write-Host ">>> scorecard + methods + output_to_share  (.\refresh_scorecard.ps1)"
Write-Host "=================================================================="
& (Join-Path $PSScriptRoot "refresh_scorecard.ps1") -Site $Site
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "Done. Dashboards re-rendered from cached data (no CLIF re-read)."
Write-Host "  Scorecard:     output/$Site/dashboard/scorecard.html"
Write-Host "  Deliverables:  output/$Site/output_to_share/"
