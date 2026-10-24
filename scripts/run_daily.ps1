<#
.SYNOPSIS
  Scheduled entry point for Portfolio Pulse run-daily (Windows Task Scheduler).
.DESCRIPTION
  Runs `python -m portfolio_pulse run-daily` with the project's own venv (no activation needed),
  copies the console output to logs\scheduler\, deletes copies older than -KeepDays, and exits
  with run-daily's exit code so Task Scheduler's "Last Run Result" shows it:
    0 = all steps OK or skipped      1 = some steps failed, export still ran
    2 = export failed                3 = another run was already running
    9 = setup problem (venv not found)
.EXAMPLE
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_daily.ps1
#>
param([int]$KeepDays = 30)

$ErrorActionPreference = 'Continue'   # Python logs to stderr; that must not stop this script
$root   = Split-Path -Parent $PSScriptRoot
$py     = Join-Path $root '.venv\Scripts\python.exe'
$logDir = Join-Path $root 'logs\scheduler'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$log = Join-Path $logDir ("run-daily-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))

function Write-Log([string]$line) {
    Write-Host $line
    Add-Content -Path $log -Value $line -Encoding utf8
}

Set-Location $root                     # so .env and relative paths resolve the same as a manual run
$env:PYTHONUTF8 = '1'
Write-Log "=== run-daily started $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="

if (-not (Test-Path $py)) {
    Write-Log "Python not found at $py. Create the venv, then run: pip install -e ."
    exit 9
}

& $py -m portfolio_pulse run-daily 2>&1 | ForEach-Object { Write-Log "$_" }
$code = $LASTEXITCODE
Write-Log "=== run-daily finished with exit code $code ==="

Get-ChildItem -Path $logDir -Filter 'run-daily-*.log' |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$KeepDays) } |
    Remove-Item -ErrorAction SilentlyContinue

exit $code