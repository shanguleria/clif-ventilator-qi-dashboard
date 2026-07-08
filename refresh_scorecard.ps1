<#
refresh_scorecard.ps1 - Windows (PowerShell) equivalent of refresh_scorecard.sh.

Re-render the QI bundle scorecard ONLY (no CLIF re-read): re-emit the LPV tile feed from the existing
parquets, rebuild scorecard.html, refresh the methods docs, and re-assemble output_to_share. Fast; safe
to run anytime a metric re-emits its tile feed. To rebuild the underlying LPV data first, run
.\run_bundle.ps1 instead.

Usage:
  .\refresh_scorecard.ps1                 # site = $env:CLIF_SITE, else 'uchicago'
  .\refresh_scorecard.ps1 -Site mimic
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
if (-not (Test-Path "output\$Site\metrics\lpv\02_patient_day_status.parquet")) {
    Write-Host "ERROR: output\$Site\metrics\lpv\02_patient_day_status.parquet not found - the LPV pipeline hasn't been built for site '$Site'."
    Write-Host "    Run the full pipeline first:  .\run_bundle.ps1 -Site $Site"
    exit 1
}
Write-Host ">>> site: $Site  (output -> output/$Site/)"

$steps = @(
    "metrics/lpv/code/05_tile_feed.py",   # re-emit the LPV feed from current parquets (no CLIF read)
    "scorecard/build_scorecard.py",       # collect every metric feed + rebuild scorecard.html
    "docs/build_methods.py",              # refresh the living methods docs from feeds + config
    "scorecard/collect_to_share.py"       # assemble PHI-free deliverables -> output/<site>/output_to_share/
)

foreach ($step in $steps) {
    & $PY $step
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: step failed ($step), exit $LASTEXITCODE"
        exit $LASTEXITCODE
    }
}

Write-Host ""
Write-Host "Done. Open the QI scorecard:  output/$Site/dashboard/scorecard.html"
Write-Host "  Methods docs refreshed under docs/ (index) + metrics/<id>/METHODS.md"
Write-Host "  Deliverables to share:       output/$Site/output_to_share/"
