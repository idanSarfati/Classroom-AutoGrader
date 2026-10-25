<#
.SYNOPSIS
  Run the Streamlit dashboard as a silent, always-on Windows background
  service via Task Scheduler - no terminal window, nothing to launch by hand.

.DESCRIPTION
  Registers a Task Scheduler job that runs `Run-Dashboard.ps1 -Service` at
  every user logon, completely hidden, and keeps the dashboard up
  indefinitely:

    * Trigger   : AtLogOn, delayed 30s so the network/desktop are ready and
                  the health check does not race a cold boot.
    * Hidden    : the action is wscript.exe running Launch-DashboardService.vbs,
                  which starts PowerShell with SW_HIDE. No console or terminal
                  window is ever created - `powershell.exe -WindowStyle Hidden`
                  on its own is not enough, because the console host can realise
                  a visible window first. Output goes to
                  logs\dashboard-service.log.
    * Port      : 8501, pinned by .streamlit\config.toml. Run-Dashboard.ps1
                  stops any stale instance first, so the port is always served
                  by the CURRENT code.
    * Always on : ExecutionTimeLimit = PT0S (unlimited) so a server that has
                  been up for days is never killed by the default 72h limit,
                  plus RestartOnFailure (999 x 1min) so the task is relaunched
                  automatically if the server ever exits.
    * Overlap   : MultipleInstances = IgnoreNew, so a second logon never
                  starts a competing instance on the same port.
    * Battery   : allowed to start and to keep running on battery, and never
                  stopped when the machine goes idle.

  IMPORTANT: the task runs in the user's interactive session (RunLevel
  Limited, LogonType Interactive), which is what makes localhost:8501
  reachable from the browser and lets the dashboard use the signed-in Google
  credentials. It therefore only runs while you are logged on - it is NOT a
  SYSTEM service, and that is deliberate.

  No Administrator rights are required: the job is registered for the current
  user in the normal task folder.

.EXAMPLE
  .\Register-DashboardService.ps1
  .\Register-DashboardService.ps1 -StartNow      # also start it right away
  .\Register-DashboardService.ps1 -Port 8501 -TaskName "ClassroomAutoGrader-Dashboard"

  Stop / remove it later:
  .\Unregister-DashboardService.ps1
#>
[CmdletBinding()]
param(
  [int]$Port = 8501,
  [string]$TaskName = "ClassroomAutoGrader-Dashboard",
  [switch]$StartNow
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

$runner = Join-Path $root "Run-Dashboard.ps1"
if (-not (Test-Path $runner)) { throw "Run-Dashboard.ps1 not found next to this script ($root)." }

# --- Build the action ------------------------------------------------------
# The action is wscript.exe running Launch-DashboardService.vbs, NOT
# powershell.exe directly.
#
# This is deliberate. `powershell.exe -WindowStyle Hidden` is only a REQUEST for
# an invisible window - the console host (conhost.exe, or Windows Terminal when
# it owns console delegation, as on this machine) can still realise a visible
# console before the style takes effect. That is exactly how two stray windows
# ended up on the desktop at logon. WScript.Shell.Run(..., 0, ...) applies
# SW_HIDE at creation time, and wscript.exe is itself windowless, so no console
# is ever created at all.
#
# -Service (inside the launcher) also makes Run-Dashboard.ps1 log to file and
# STAY ALIVE for as long as the server runs - if the action exited immediately,
# Task Scheduler would consider the task finished and tear the server down with
# its process tree.
$launcher = Join-Path $root "Launch-DashboardService.vbs"
if (-not (Test-Path $launcher)) {
  throw "Launch-DashboardService.vbs not found next to this script ($root). It is required to start the dashboard without a visible window."
}

$wscript = Join-Path $env:SystemRoot "System32\wscript.exe"
if (-not (Test-Path $wscript)) { $wscript = "wscript.exe" }

# The launcher takes Run-Dashboard.ps1's path and the port as arguments, so the
# script stays correct if the project is moved.
$argumentList = "`"$launcher`" `"$runner`" $Port"

$action = New-ScheduledTaskAction -Execute $wscript -Argument $argumentList -WorkingDirectory $root

# --- Trigger: every logon, after a short settling delay --------------------
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT30S"

# Run in the user's own interactive session (needed for localhost + Google
# credentials). RunLevel Limited => no UAC prompt, no admin needed.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

# --- Settings: unlimited runtime + automatic restart -----------------------
# ExecutionTimeLimit MUST be 0 (PT0S). The default is 72 hours, which silently
# kills a long-lived server after three days - the exact "it worked yesterday"
# failure this service is meant to eliminate.
# RestartCount must be a positive number: -1 serialises to an empty <Count>
# element, which makes Register-ScheduledTask fail with 0x80041319.
$settings = New-ScheduledTaskSettingsSet `
  -StartWhenAvailable `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
# Never stop the dashboard just because the machine went idle.
$settings.IdleSettings.StopOnIdleEnd = $false
$settings.IdleSettings.RestartOnIdle = $false

$description = "Classroom AutoGrader Streamlit dashboard: silent always-on background service on port $Port. Launched hidden at logon via Run-Dashboard.ps1 -Service; restarts automatically if it stops. Logs: logs\dashboard-service.log. Remove with Unregister-DashboardService.ps1."

Write-Host "Registering '$TaskName' (dashboard service on port $Port)..."
Register-ScheduledTask `
  -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
  -Settings $settings -Description $description -Force | Out-Null

# --- Verify the registration actually stuck --------------------------------
# Read the task back and confirm the settings whose absence breaks this
# silently: an unlimited runtime and a working restart policy.
#
# This read-back is deliberately defensive. `Register-ScheduledTask` can return
# before the Task Scheduler store has replicated the new job - and that window
# is widest immediately after the same task name was just unregistered. A naive
# `Get-ScheduledTask -ErrorAction Stop` then fails on a registration that is in
# fact perfectly fine. So poll both query APIs, tolerate CIM throws, and only
# give up after a generous window.
function Wait-ForTask([string]$Name, [int]$TimeoutSeconds = 45) {
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    $task = $null
    try { $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue } catch { $task = $null }
    if (-not $task) {
      # Fall back to the schtasks store, which can be readable a moment sooner.
      try {
        $csv = schtasks /Query /FO CSV 2>$null | ConvertFrom-Csv
        if ($csv | Where-Object { $_.TaskName -eq "\$Name" }) {
          $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
        }
      } catch { $task = $null }
    }
    if ($task) { return $task }
    Start-Sleep -Seconds 2
  }
  return $null
}

$registered = Wait-ForTask -Name $TaskName
if (-not $registered) {
  throw ("The task was submitted but has not appeared in Task Scheduler yet as '{0}'. Wait a minute and check taskschd.msc (it is very likely registered)." -f $TaskName)
}

$taskXml = [xml](schtasks /Query /TN $TaskName /XML ONE)
$xmlSettings = $taskXml.Task.Settings

$problems = @()
if ($xmlSettings.ExecutionTimeLimit -ne "PT0S") {
  $problems += "ExecutionTimeLimit is '$($xmlSettings.ExecutionTimeLimit)' instead of PT0S - a long-running server would eventually be killed."
}
if (-not $registered.Settings.StartWhenAvailable) {
  $problems += "StartWhenAvailable is not set - a missed logon trigger will not catch up."
}
if (-not $registered.Settings.RestartCount -or $xmlSettings.RestartOnFailure.Count -le 0) {
  $problems += "RestartOnFailure is not configured - the dashboard will not come back on its own."
}

if ($problems) {
  Write-Warning "Registration verification FAILED:"
  $problems | ForEach-Object { Write-Warning "  - $_" }
  Write-Warning "Re-run this script; if it persists, inspect the task in taskschd.msc."
} else {
  Write-Host "Verified: unlimited runtime (PT0S) + auto-restart (999 x 1min) + StartWhenAvailable."
}

$triggerName = (($registered.Triggers | ForEach-Object { $_.CimClass.CimClassName }) -join ", ") -replace '^MSFT_Task', ''
Write-Host ("Trigger: {0} (30s after logon)" -f $triggerName)
Write-Host ("Action:  {0} {1}" -f $wscript, $argumentList)

if ($StartNow) {
  Write-Host "Starting the dashboard now (it normally starts at next logon)..."
  Start-ScheduledTask -TaskName $TaskName
  # Wait for the port so the user gets a definitive answer here rather than
  # having to check the browser. Cold imports of pandas/Google API can take a
  # while, hence the generous deadline.
  $deadline = (Get-Date).AddSeconds(240)
  $up = $false
  while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 3
    $client = New-Object System.Net.Sockets.TcpClient
    try {
      $connect = $client.ConnectAsync("localhost", $Port)
      if ($connect.Wait(1500) -and $client.Connected) { $up = $true; break }
    } catch { } finally { $client.Close() }
  }
  if ($up) {
    Write-Host ("Dashboard is up: http://localhost:{0}" -f $Port)
  } else {
    Write-Warning ("The task started but port {0} did not open within 240s. Check logs\dashboard-service.log and Get-ScheduledTaskInfo -TaskName `"{1}`"." -f $Port, $TaskName)
  }
} else {
  Write-Host "The dashboard starts automatically at your next logon."
  Write-Host ("To start it right now: Start-ScheduledTask -TaskName `"$TaskName`"")
}

Write-Host ""
Write-Host ("Registered '{0}' - silent, always-on dashboard on http://localhost:{1}." -f $TaskName, $Port)
Write-Host "Log:      logs\dashboard-service.log"
Write-Host ("Status:   Get-ScheduledTask -TaskName `"$TaskName`"")
Write-Host ("Pause:    Stop-ScheduledTask -TaskName `"$TaskName`"")
Write-Host ("Resume:   Start-ScheduledTask -TaskName `"$TaskName`"")
Write-Host "Remove:   .\Unregister-DashboardService.ps1"
