<#
.SYNOPSIS
  Start the dashboard cleanly: free the port from stale instances first,
  then launch `streamlit run app.py` from the project venv.

.DESCRIPTION
  Guarantees a single, current-code Streamlit instance:
    1. Stops any orphan/stale Streamlit processes for this project
       (Stop-Streamlit.ps1) so the port never stays held by old code.
    2. Verifies the port is actually free.
    3. Launches streamlit from .venv (falls back to PATH python) and waits
       for the health endpoint, reporting the URL or a clear error.

  With -Service the script also stays alive for as long as the server does, so
  Windows Task Scheduler (Register-DashboardService.ps1) can keep the dashboard
  running permanently with no terminal window:
    * output is mirrored to logs\dashboard-service.log (nothing is visible),
    * when the server dies the script exits 1, which Task Scheduler's
      RestartOnFailure turns into an automatic restart,
    * when another instance deliberately takes the port over (for example you
      ran this script by hand), the script exits 0 instead - so the background
      service never fights your manual launch.

.EXAMPLE
  .\Run-Dashboard.ps1
  .\Run-Dashboard.ps1 -Port 8501 -NoBrowser
  .\Run-Dashboard.ps1 -Service -Port 8501 -NoBrowser   # used by Task Scheduler
#>
param(
  [int]$Port = 8501,
  [switch]$NoBrowser,
  [switch]$KeepOld,
  [switch]$Service
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

# --- logging ---------------------------------------------------------------
# In service mode there is no visible console, so every line is mirrored to
# logs\dashboard-service.log (git-ignored via *.log) for later diagnosis.
$logPath = $null
if ($Service) {
  $logDir = Join-Path $root "logs"
  if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
  $logPath = Join-Path $logDir "dashboard-service.log"
}

function Write-Log([string]$Message) {
  $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
  if ($logPath) {
    try {
      # Keep the log bounded: if it grew past ~1 MB keep only the recent tail.
      if ((Test-Path $logPath) -and ((Get-Item $logPath).Length -gt 1MB)) {
        $tail = Get-Content $logPath -Tail 2000
        Set-Content -Path $logPath -Value $tail -Encoding utf8
      }
      Add-Content -Path $logPath -Value $line -Encoding utf8
    } catch { }
  }
  Write-Host $line
}

if (-not $KeepOld) {
  # Stop-Streamlit is best-effort: its own warnings (e.g. an unkillable PID)
  # must never abort the launch - the port-free check below is the real gate.
  try {
    & (Join-Path $root "Stop-Streamlit.ps1") -Port $Port
  } catch {
    Write-Log ("WARNING: Stop-Streamlit reported an issue: {0}" -f $_.Exception.Message)
  }
}

# Fail fast if something else (not ours) still holds the port.
$listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($listener) {
  Write-Log ("WARNING: Port {0} is still held by PID {1} (not this project). Resolve that first." -f $Port, ($listener.OwningProcess -join ','))
  if (-not $Service) { exit 1 }
  # In service mode do not give up immediately: the holder may be a previous
  # instance of ours that Task Scheduler is still tearing down after a restart.
  # Wait for the port, and only bail out if it never frees up.
  $freeDeadline = (Get-Date).AddSeconds(90)
  while ((Get-Date) -lt $freeDeadline) {
    Start-Sleep -Seconds 3
    $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    if (-not $listener) { break }
  }
  if ($listener) {
    Write-Log ("ERROR: Port {0} never became free (held by PID {1}). Exiting so Task Scheduler can retry." -f $Port, ($listener.OwningProcess -join ','))
    exit 1
  }
  Write-Log ("Port {0} became free after the previous instance shut down." -f $Port)
}

$streamlit = Join-Path $root ".venv\Scripts\streamlit.exe"
$useVenv = Test-Path $streamlit
if (-not $useVenv) { $streamlit = "streamlit" }

$launchArgs = @("run", "app.py", "--server.port", $Port)
if ($NoBrowser) { $launchArgs += "--server.headless true" }

# --- One launch attempt ----------------------------------------------------
# Factored into a function so the interactive path and the service supervision
# loop run exactly the same code. Returns the process plus the health verdict.
function Start-DashboardOnce {
  Write-Log ("Starting: {0} {1}" -f $streamlit, ($launchArgs -join ' '))

  # In service mode the Streamlit server MUST NOT create a console window: this
  # script is launched hidden by Task Scheduler, so a window here would appear
  # on the desktop at every logon with nothing attached to it. Streamlit is a
  # console app, so -WindowStyle Hidden is what suppresses that window.
  #
  # In interactive (non-service) mode the window is left visible on purpose:
  # when you run this by hand the live server log is the whole point.
  $startArgs = @{
    FilePath         = $streamlit
    ArgumentList     = $launchArgs
    WorkingDirectory = $root
    PassThru         = $true
  }
  if ($Service) { $startArgs.WindowStyle = "Hidden" }
  $serverProc = Start-Process @startArgs

  # Wait for the server so success/failure is explicit. Two phases, because
  # Windows PowerShell 5.1's Invoke-WebRequest is unreliable while the server is
  # still booting: an early connection abort can poison its connection pool and
  # make every later probe fail even though the server is healthy.
  #   Phase 1: raw TCP connect to the port (immune to HTTP-layer quirks).
  #   Phase 2: confirm the health endpoint answers "ok".
  # app.py pulls in pandas, the Google API client and Streamlit itself; on a
  # cold import cache the port may only be bound after a couple of minutes, so
  # the deadline is generous on purpose and exits early the moment we are up.
  $deadline = (Get-Date).AddSeconds(240)
  $portOpen = $false
  while ((Get-Date) -lt $deadline) {
    if ($serverProc.HasExited) { break }
    $client = New-Object System.Net.Sockets.TcpClient
    try {
      $connect = $client.ConnectAsync("localhost", $Port)
      if ($connect.Wait(1000) -and $client.Connected) { $portOpen = $true; break }
    } catch { } finally { $client.Close() }
    Start-Sleep -Milliseconds 500
  }

  $healthy = $false
  if ($portOpen) {
    $healthUrl = "http://localhost:{0}/_stcore/health" -f $Port
    for ($attempt = 0; $attempt -lt 10 -and -not $healthy; $attempt++) {
      try {
        $resp = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 5
        if ($resp.StatusCode -eq 200 -and $resp.Content -match 'ok') { $healthy = $true }
      } catch { Start-Sleep -Milliseconds 500 }
    }
  }

  if ($healthy) {
    Write-Log ("Dashboard is up: http://localhost:{0}  (PID {1}, current code)" -f $Port, $serverProc.Id)
  } elseif ($serverProc.HasExited) {
    Write-Log ("ERROR: Streamlit exited early (code {0}). Run `"{1} run app.py`" in a terminal to see the error." -f $serverProc.ExitCode, $streamlit)
  } elseif ($portOpen) {
    Write-Log ("WARNING: Port {0} is open (PID {1}) but the health endpoint did not answer 'ok'. Verify http://localhost:{0} in a browser." -f $Port, $serverProc.Id)
  } else {
    Write-Log ("WARNING: Streamlit (PID {0}) is running but did not bind port {1} within 240s. Check its window/output." -f $serverProc.Id, $Port)
  }

  return [PSCustomObject]@{
    Process  = $serverProc
    Healthy  = $healthy
    PortOpen = $portOpen
  }
}

# --- Process tree helper ---------------------------------------------------
# NOTE: `streamlit.exe run app.py` does not listen on the port itself - it
# spawns a child python.exe (and that in turn another) which is the real
# server. So "is the port still ours?" must consider the WHOLE process tree
# rooted at the launched process, not just its PID. Comparing against a single
# PID makes the service exit spuriously, orphaning a healthy server.
function Get-DescendantPids([int]$RootId) {
  $ids = New-Object System.Collections.Generic.List[int]
  $ids.Add($RootId)
  $queue = New-Object System.Collections.Generic.List[int]
  $queue.Add($RootId)
  while ($queue.Count -gt 0) {
    $current = $queue[0]
    $queue.RemoveAt(0)
    try {
      $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $current" -ErrorAction SilentlyContinue
      foreach ($child in $children) {
        if (-not $ids.Contains($child.ProcessId)) {
          $ids.Add($child.ProcessId)
          $queue.Add($child.ProcessId)
        }
      }
    } catch { }
  }
  return ,$ids
}

# ---------------------------------------------------------------------------
# Interactive (non-service) mode: launch once, report, done. The visible
# Streamlit window and its live log are the point of running it by hand.
# ---------------------------------------------------------------------------
if (-not $Service) {
  $run = Start-DashboardOnce
  if (-not $run.Healthy -and $run.Process.HasExited) { exit 1 }
  exit 0
}

# ---------------------------------------------------------------------------
# Service mode: launch once, then supervise forever.
#
# The script IS the scheduled task's action, so it must never exit while the
# dashboard is meant to be running - exiting marks the task complete and Task
# Scheduler stops supervising it.
#
# We deliberately RELAUNCH the server ourselves rather than exiting and
# trusting the task's RestartOnFailure. That setting is configured (999 x 1min)
# and verified in the task XML, but on this machine it did not reliably fire
# after the action exited 1: the task went to Ready and simply stayed there
# with the dashboard down. Self-supervision makes recovery immediate and
# independent of that behaviour; the task-level restart remains a useful
# backstop if this whole script ever dies.
#
#   * server dies            -> wait briefly, then relaunch it here,
#   * another instance took over the port (e.g. you ran Run-Dashboard.ps1 by
#     hand) -> exit 0, because a fresh instance is intentionally serving the
#     dashboard and fighting it would just yank the port away from you.
# ---------------------------------------------------------------------------
Write-Log "Service mode: supervising the dashboard (restarts it automatically if it stops)."

$run = Start-DashboardOnce
if (-not $run.Healthy) {
  # Could not come up at all. Exit non-zero so the task's RestartOnFailure is
  # the backstop; also gives the log a clear, findable failure.
  Write-Log ("ERROR: the dashboard did not start successfully on port {0}. Exiting so Task Scheduler can retry." -f $Port)
  exit 1
}

while ($true) {
  Start-Sleep -Seconds 15

  $ourPids = Get-DescendantPids -RootId $run.Process.Id
  $owner = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue

  if ($owner) {
    if ($ourPids -contains $owner.OwningProcess) {
      # Normal, healthy state: our own server still owns the port.
      continue
    }
    # Someone else took the port. They may be mid-startup, so give them a
    # moment before concluding this was a deliberate hand-over.
    Start-Sleep -Seconds 5
    $owner = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    if ($owner -and -not ($ourPids -contains $owner.OwningProcess)) {
      Write-Log ("Port {0} is now served by PID {1} (another instance took over). Exiting cleanly so it is left alone." -f $Port, $owner.OwningProcess)
      exit 0
    }
    continue
  }

  # Nothing is listening. Either the server crashed or it is shutting down.
  # Wait briefly so a restart-in-progress is not mistaken for a failure.
  Start-Sleep -Seconds 5
  if (-not (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)) {
    $stillAlive = $false
    foreach ($ourPid in $ourPids) {
      try {
        if (Get-Process -Id $ourPid -ErrorAction SilentlyContinue) { $stillAlive = $true; break }
      } catch { }
    }
    if ($stillAlive) {
      # The tree is alive but not serving yet - still booting or shutting down.
      Write-Log ("Dashboard stopped listening on port {0} but its process is still starting/shutting down. Waiting." -f $Port)
      continue
    }
    # Make sure no half-dead remnants of the old tree are left holding the
    # port, then start a fresh server.
    Write-Log ("Dashboard stopped serving port {0}. Relaunching it." -f $Port)
    & (Join-Path $root "Stop-Streamlit.ps1") -Port $Port | Out-Null
    Start-Sleep -Seconds 3
    $run = Start-DashboardOnce
    if (-not $run.Healthy) {
      Write-Log ("ERROR: relaunch on port {0} did not come up. Exiting so Task Scheduler can retry." -f $Port)
      exit 1
    }
  }
}
