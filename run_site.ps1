<#
run_site.ps1 - Windows (PowerShell) equivalent of run_site.sh: FULL multi-metric build for one site,
TIMED end-to-end. Phases: LPV pipeline + scorecard (run_bundle.ps1) -> proning -> sat -> sbt ->
recombine (refresh_scorecard.ps1). Records per-phase + total wall-clock to
output/<site>/run_timings.csv (one row per run) and prints a summary.

Usage:
  .\run_site.ps1 -Site <id>              # or set $env:CLIF_SITE first
#>
param([string]$Site = $(if ($env:CLIF_SITE) { $env:CLIF_SITE } else { "uchicago" }))

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$env:CLIF_SITE = $Site

$PY = Join-Path ".venv" "Scripts\python.exe"
if (-not (Test-Path $PY)) {
    Write-Host "ERROR: $PY not found. Create the venv: python -m venv .venv; .venv\Scripts\python.exe -m pip install -r requirements.txt"
    exit 1
}
if (-not (Test-Path "sites\$Site.json")) {
    Write-Host "ERROR: sites\$Site.json not found (copy sites\uchicago.example.json)."
    exit 1
}

$out = "output\$Site"
New-Item -ItemType Directory -Force -Path $out | Out-Null
$csv = Join-Path $out "run_timings.csv"
if (-not (Test-Path $csv)) {
    "run_started_utc,lpv_bundle_s,proning_s,sat_s,sbt_s,refresh_s,total_s,total_hms,git_sha" | Out-File -FilePath $csv -Encoding utf8
}

$startUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
try { $sha = (git rev-parse --short HEAD).Trim() } catch { $sha = "NA" }
function Fmt([int]$s) { "{0}h{1:d2}m{2:d2}s" -f [int]($s/3600), [int](($s%3600)/60), ($s%60) }

$names = @(); $secs = @()
function Time-Phase([string]$name, [scriptblock]$block) {
    Write-Host ""; Write-Host "========== $name =========="
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    & $block
    if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: phase '$name' failed (exit $LASTEXITCODE)"; exit $LASTEXITCODE }
    $sw.Stop(); $d = [int]$sw.Elapsed.TotalSeconds
    $script:names += $name; $script:secs += $d
    Write-Host (">>> {0}: {1}" -f $name, (Fmt $d))
}
function Run-Vertical([string]$m) {
    Get-ChildItem "metrics\$m\code\0*.py" | ForEach-Object { & $PY $_.FullName; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE } }
}

Write-Host ">>> full timed run - site: $Site  (output -> $out/)"
$swTotal = [System.Diagnostics.Stopwatch]::StartNew()
Time-Phase "LPV+scorecard" { .\run_bundle.ps1 -Site $Site }
Time-Phase "proning"       { Run-Vertical proning }
Time-Phase "sat"           { Run-Vertical sat }
Time-Phase "sbt"           { Run-Vertical sbt }
Time-Phase "refresh"       { .\refresh_scorecard.ps1 -Site $Site }
$swTotal.Stop(); $total = [int]$swTotal.Elapsed.TotalSeconds

Write-Host ""
Write-Host "=================== timing (site: $Site) ==================="
for ($i = 0; $i -lt $names.Count; $i++) { "  {0,-14} {1}" -f $names[$i], (Fmt $secs[$i]) | Write-Host }
"  {0,-14} {1}" -f "TOTAL", (Fmt $total) | Write-Host
("{0},{1},{2},{3},{4},{5},{6},{7},{8}" -f $startUtc, $secs[0], $secs[1], $secs[2], $secs[3], $secs[4], $total, (Fmt $total), $sha) | Out-File -FilePath $csv -Append -Encoding utf8
Write-Host "  logged -> $csv"
Write-Host "  scorecard -> $out\dashboard\scorecard.html   |   deliverables -> $out\output_to_share\"
