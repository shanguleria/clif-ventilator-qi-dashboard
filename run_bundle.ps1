<#
run_bundle.ps1 - Windows (PowerShell) equivalent of run_bundle.sh.

Build the ICU ventilator-QI bundle end-to-end for one site, using the project virtualenv:
  LPV pipeline (01_cohort -> 05_tile_feed) -> scorecard combiner -> methods docs -> output_to_share.
Other metrics (proning, sat, sbt) are their own pipelines under metrics/<id>/; run their stages the
same way (see README "Onboarding a new site"). The combiner collects whatever feeds already exist.

Prereqs: a .venv with requirements installed, and sites/<site>.json for your site.

Usage:
  .\run_bundle.ps1                 # site = $env:CLIF_SITE, else 'uchicago'
  .\run_bundle.ps1 -Site mimic

If PowerShell blocks the script, allow local scripts for this session:
  Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
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
    Write-Host "ERROR: site profile sites\$Site.json not found. Create it (copy sites\uchicago.example.json)"
    Write-Host "    and set data_path, timezone, clif_version, enabled_metrics for site '$Site'."
    exit 1
}
Write-Host ">>> site: $Site  (output -> output/$Site/)"

$startUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
try { $sha = (git rev-parse --short HEAD).Trim() } catch { $sha = "NA" }
function Fmt([int]$s) { "{0}h{1:d2}m{2:d2}s" -f [int]($s/3600), [int](($s%3600)/60), ($s%60) }
$sw = [System.Diagnostics.Stopwatch]::StartNew()

$steps = @(
    "metrics/lpv/code/01_cohort.py",
    "metrics/lpv/code/02_features.py",
    "metrics/lpv/code/02d_severity.py",
    "metrics/lpv/code/03_aggregate.py",
    "metrics/lpv/code/04_dashboard.py",
    "metrics/lpv/code/05_tile_feed.py",
    "scorecard/build_scorecard.py",
    "docs/build_methods.py",
    "scorecard/collect_to_share.py"    # assemble PHI-free deliverables -> output/<site>/output_to_share/
)

foreach ($step in $steps) {
    Write-Host ""
    Write-Host "=================================================================="
    Write-Host ">>> $step"
    Write-Host "=================================================================="
    & $PY $step
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: step failed ($step), exit $LASTEXITCODE"
        exit $LASTEXITCODE
    }
}

$sw.Stop(); $elapsed = [int]$sw.Elapsed.TotalSeconds

# Timing -> output/<site>/run_timings.csv (long format). run_site.ps1 sets $env:TIMING_SUPPRESS.
if (-not $env:TIMING_SUPPRESS) {
    New-Item -ItemType Directory -Force -Path "output\$Site" | Out-Null
    $csv = "output\$Site\run_timings.csv"
    if (-not (Test-Path $csv)) { "run_started_utc,site,runner,phase,seconds,hms,git_sha" | Out-File -FilePath $csv -Encoding utf8 }
    ("{0},{1},run_bundle,lpv_bundle,{2},{3},{4}" -f $startUtc, $Site, $elapsed, (Fmt $elapsed), $sha) | Out-File -FilePath $csv -Append -Encoding utf8
}

Write-Host ""
Write-Host ("Done in {0}.  Open the QI scorecard:  output/$Site/dashboard/scorecard.html" -f (Fmt $elapsed))
Write-Host "  (the whole output/$Site/dashboard/ folder is the shippable bundle: scorecard + per-metric drill-downs)"
Write-Host "  Deliverables to share with the coordinating center:  output/$Site/output_to_share/"
if (-not $env:TIMING_SUPPRESS) { Write-Host "  Timing logged -> output/$Site/run_timings.csv" }
