# GOLD RIPPER :: Windows installer (PowerShell 5+)
# - venv + deps + optional local training
# - Task Scheduler tasks (logon + boot) with auto-restart
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "== GOLD RIPPER Windows installer ==" -ForegroundColor Yellow

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
  Write-Host "[!] Python not found. Install Python 3.10+ from python.org (check 'Add to PATH')." -ForegroundColor Red
  exit 1
}

if (-not (Test-Path ".venv")) { python -m venv .venv }
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip -q
& ".\.venv\Scripts\python.exe" -m pip install -q -r requirements.txt
Write-Host "[+] dependencies installed"

if (-not (Test-Path ".env")) {
  Copy-Item ".env.example" ".env"
  Write-Host "[+] created .env - EDIT IT (Bitget keys for LIVE mode)"
}

if (-not (Test-Path "data\history\gold_1h.csv")) {
  Write-Host "[+] fetching gold history..."
  & ".\.venv\Scripts\python.exe" data\fetch_history.py
}
if (-not (Test-Path "data\calibration.json")) {
  Write-Host "[+] training locally (~5 min)..."
  & ".\.venv\Scripts\python.exe" train.py
}

$pythonExe = (Resolve-Path ".\.venv\Scripts\python.exe").Path
$root = (Get-Location).Path

# Task Scheduler: run at logon + boot, restart on failure
$action  = New-ScheduledTaskAction -Execute $pythonExe -Argument "`"$root\ripper.py`"" -WorkingDirectory $root
$trigger1 = New-ScheduledTaskTrigger -AtLogOn
$trigger2 = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
Register-ScheduledTask -TaskName "GoldRipper" -Action $action -Trigger $trigger1,$trigger2 -Settings $settings -Force | Out-Null

$wdAction  = New-ScheduledTaskAction -Execute $pythonExe -Argument "`"$root\watchdog.py`"" -WorkingDirectory $root
Register-ScheduledTask -TaskName "GoldRipperWatchdog" -Action $wdAction -Trigger $trigger1 -Settings $settings -Force | Out-Null

Start-ScheduledTask -TaskName "GoldRipper"
Write-Host ""
Write-Host "[OK] GOLD RIPPER installed (paper mode). Task Scheduler: GoldRipper" -ForegroundColor Green
Write-Host "     LIVE: edit .env -> RIPPER_MODE=LIVE + Bitget keys, then Restart-ScheduledTask -TaskName GoldRipper"
