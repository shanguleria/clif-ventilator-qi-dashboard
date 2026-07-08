<#
run_site.ps1 - Windows (PowerShell) equivalent of run_site.sh: FULL multi-metric build for one site,
TIMED end-to-end. Phases: LPV pipeline + scorecard (run_bundle.ps1) -> proning -> sat -> sbt ->
recombine (refresh_scorecard.ps1). Appends one row PER PHASE to output/<site>/run_timings.csv as each
phase finishes (long format), so an interrupted run keeps whatever completed. Prints a summary.

Usage:
  .\run_site.ps1 -Site <id>              # or set $env:CLIF_SITE first
#>
param([string]$Site = $(if ($env:CLIF_SITE) { $env:CLIF_SITE } else { "uchicago" }))

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$env:CLIF_SITE = $Site
$env:TIMING_SUPPRESS = "1"   # run_site owns timing; stop run_bundle from logging its own duplicate row

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
    "run_started_utc,site,runner,phase,seconds,hms,git_sha" | Out-File -FilePath $csv -Encoding utf8
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
    ("{0},{1},run_site,{2},{3},{4},{5}" -f $startUtc, $Site, $name, $d, (Fmt $d), $sha) | Out-File -FilePath $csv -Append -Encoding utf8
    Write-Host (">>> {0}: {1}   (logged -> {2})" -f $name, (Fmt $d), $csv)
}
function Run-Vertical([string]$m) {
    # Numbered pipeline stages only (01_ .. 05_), in order; skip 00_* probes and bare helper modules.
    Get-ChildItem "metrics\$m\code\*.py" |
        Where-Object { $_.Name -match '^[0-9]' -and $_.Name -notmatch '^00_' } |
        Sort-Object Name |
        ForEach-Object { & $PY $_.FullName; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE } }
}

Write-Host ">>> full timed run - site: $Site   output -> $out/   timing -> $csv"
$swTotal = [System.Diagnostics.Stopwatch]::StartNew()
Time-Phase "lpv_bundle" { .\run_bundle.ps1 -Site $Site }
Time-Phase "proning"    { Run-Vertical proning }
Time-Phase "sat"        { Run-Vertical sat }
Time-Phase "sbt"        { Run-Vertical sbt }
Time-Phase "refresh"    { .\refresh_scorecard.ps1 -Site $Site }
$swTotal.Stop(); $total = [int]$swTotal.Elapsed.TotalSeconds
("{0},{1},run_site,TOTAL,{2},{3},{4}" -f $startUtc, $Site, $total, (Fmt $total), $sha) | Out-File -FilePath $csv -Append -Encoding utf8

Write-Host ""
Write-Host "=================== timing (site: $Site) ==================="
for ($i = 0; $i -lt $names.Count; $i++) { "  {0,-12} {1}" -f $names[$i], (Fmt $secs[$i]) | Write-Host }
"  {0,-12} {1}" -f "TOTAL", (Fmt $total) | Write-Host
Write-Host "  logged -> $csv   (one row per phase; open it to compare cold vs warm runs)"
Write-Host "  scorecard -> $out\dashboard\scorecard.html   |   deliverables -> $out\output_to_share\"
