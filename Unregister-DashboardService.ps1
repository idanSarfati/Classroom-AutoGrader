<#
.SYNOPSIS
  Stop and remove the always-on dashboard background service registered by
  Register-DashboardService.ps1.

.DESCRIPTION
  1. Stops the scheduled task (which also terminates the dashboard server it
     is responsible for).
  2. Removes the task registration entirely.
  3. Optionally frees port 8501 by stopping any leftover dashboard processes
     (default: yes) - Stop-Streamlit.ps1 is used, which is scoped to this
     project so other Streamlit apps are never touched.

  Safe to run when nothing is registered (reports that and exits 0), and safe
  to run twice. No Administrator rights required.

.EXAMPLE
  .\Unregister-DashboardService.ps1
  .\Unregister-DashboardService.ps1 -KeepServer   # remove the task but leave the running dashboard alone
  .\Unregister-DashboardService.ps1 -List         # show what would happen, change nothing
#>
[CmdletBinding()]
param(
  [int]$Port = 8501,
  [string]$TaskName = "ClassroomAutoGrader-Dashboard",
  [switch]$KeepServer,
  [switch]$List
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
  Write-Host "No dashboard service task named '$TaskName' is registered. Nothing to do."
  exit 0
}

$state = $task.State
Write-Host ("Found '{0}' (state: {1})." -f $TaskName, $state)

if ($List) {
  Write-Host ("Would stop the task and remove the registration (port {0})." -f $Port)
  if ($KeepServer) {
    Write-Host "Would leave the running dashboard alone (-KeepServer)."
  } else {
    Write-Host "Would then free port $Port via Stop-Streamlit.ps1."
  }
  exit 0
}

# 1) Stop the task first. Ending the task terminates its action process tree,
#    which includes the Streamlit server it launched - so the dashboard goes
#    down with the service, exactly as intended.
if ($state -eq "Running") {
  Write-Host "Stopping the running dashboard service..."
  Stop-ScheduledTask -TaskName $TaskName
  # Give the action tree a moment to release the port before we check it.
  Start-Sleep -Seconds 3
} else {
  Write-Host "Task is not running, so no server to stop."
}

# 2) Remove the registration.
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host ("Removed the '{0}' scheduled task." -f $TaskName)

# 3) Free the port from anything left behind. Without this, a stray process can
#    keep holding 8501 and the next manual `streamlit run app.py` silently
#    falls back to 8502 - the exact confusion this service is meant to avoid.
if ($KeepServer) {
  Write-Host "-KeepServer: leaving any running dashboard process untouched."
} else {
  $stopper = Join-Path $root "Stop-Streamlit.ps1"
  if (Test-Path $stopper) {
    try {
      & $stopper -Port $Port
    } catch {
      Write-Warning ("Stop-Streamlit reported an issue: {0}" -f $_.Exception.Message)
    }
  } else {
    Write-Warning "Stop-Streamlit.ps1 not found; port $Port was not checked."
  }
}

$listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
Write-Host ""
if ($listener) {
  Write-Host ("Dashboard service removed. Note: port {0} is still held by PID {1}." -f $Port, ($listener.OwningProcess -join ','))
} else {
  Write-Host ("Dashboard service removed and port {0} is free." -f $Port)
}
Write-Host "To bring the dashboard back permanently: .\Register-DashboardService.ps1"
Write-Host "Or start it once by hand:                      .\Run-Dashboard.ps1"
