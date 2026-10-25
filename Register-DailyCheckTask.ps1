#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Register a Windows Task Scheduler job that runs the read-only daily check
  once a day at a fixed time, with robust catch-up behaviour.

  Missed-run guarantees:
    * -StartWhenAvailable (StartWhenAvailable=true in the task XML): if the
      scheduled time passed while the machine was shut down or asleep, the
      task runs automatically as soon as the machine boots up / wakes.
    * A second logon trigger (AtLogOn, 2-minute delay) fires the same action
      shortly after every logon as a belt-and-braces net. The action passes
      --skip-if-today-done, so when today's report already exists the extra
      trigger exits 0 immediately - at most one report folder per day.
    * MultipleInstances=IgnoreNew + a restart-on-failure policy so
      overlapping triggers never run two scans side by side.
    * After registration the script VERIFIES StartWhenAvailable really stuck
      on the registered task, and immediately runs the task when today's
      report is still missing (registration-time catch-up).

.EXAMPLE
  .\Register-DailyCheckTask.ps1
  .\Register-DailyCheckTask.ps1 -At 06:00 -TaskName "ClassroomAutoGrader-DailyCheck"
  .\Register-DailyCheckTask.ps1 -NoBootTrigger   # skip the extra AtLogOn trigger
#>
param(
  [string]$At = "06:00",
  [string]$TaskName = "ClassroomAutoGrader-DailyCheck",
  [switch]$NoBootTrigger,
  [switch]$SkipImmediateRun
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

# --skip-if-today-done makes every trigger idempotent: the boot trigger and
# StartWhenAvailable catch-up can both fire after a powered-off morning, but
# only the first one that finds no report for today actually scans.
$action = New-ScheduledTaskAction -Execute $py -Argument "-m src.daily_check --once --skip-if-today-done" -WorkingDirectory $root

$dailyTrigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($At, "HH:mm", $null))
$triggers = @($dailyTrigger)
if (-not $NoBootTrigger) {
  # Belt-and-braces: run shortly after logon too. Combined with
  # --skip-if-today-done this is a no-op on days the 06:00 run succeeded.
  $bootTrigger = New-ScheduledTaskTrigger -AtLogOn
  $bootTrigger.Delay = "PT2M"   # give the network stack a moment after logon
  $triggers += $bootTrigger
}

$settings = New-ScheduledTaskSettingsSet `
  -StartWhenAvailable `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
  -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings -Description "Classroom AutoGrader daily READ-ONLY check (never pushes grades/comments/returns). Catch-up: StartWhenAvailable + logon trigger, idempotent per day." -Force | Out-Null

# --- Verify the registration actually stuck -------------------------------
$registered = Get-ScheduledTask -TaskName $TaskName
if (-not $registered.Settings.StartWhenAvailable) {
  Write-Warning "StartWhenAvailable is NOT set on '$TaskName' - missed 06:00 runs will not catch up after boot. Re-run this script as Administrator and check for errors above."
} else {
  Write-Host "Verified: StartWhenAvailable=true on '$TaskName' (missed runs catch up after boot/wake)."
}
Write-Host ("Triggers: " + (($registered.Triggers | ForEach-Object { $_.CimClass.CimClassName -replace '^MSFT_Task', '' }) -join ', '))

# --- Registration-time immediate execution check --------------------------
# If today's report folder is still missing (e.g. this script is being run
# precisely because the machine was off at 06:00), run the task right now so
# the dashboard has a fresh snapshot instead of a stale one.
if (-not $SkipImmediateRun) {
  $today = Get-Date -Format "yyyy-MM-dd"
  $checksDir = Join-Path $root "exports\daily_checks"
  $todayReport = $false
  if (Test-Path $checksDir) {
    $todayReport = @(Get-ChildItem -Path $checksDir -Directory -Filter "$today`_*" |
      Where-Object { Test-Path (Join-Path $_.FullName "daily_check.json") }).Count -gt 0
  }
  if ($todayReport) {
    Write-Host "Today's report already exists - no immediate run needed."
  } else {
    Write-Host "No report for today ($today) yet - starting '$TaskName' now (catch-up)..."
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Catch-up started. Progress: Get-ScheduledTaskInfo -TaskName `"$TaskName`""
  }
}

Write-Host "Registered '$TaskName': daily at $At + logon catch-up (read-only check)."
Write-Host "Run manually: schtasks /run /tn `"$TaskName`""
