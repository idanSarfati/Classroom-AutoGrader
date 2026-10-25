<#
.SYNOPSIS
  Stop stale/orphan Streamlit dashboard processes for this project and free
  the dashboard port, so `streamlit run app.py` always binds cleanly.

.DESCRIPTION
  Finds and stops:
    1. Every process listening on the dashboard port (default 8501) and the
       ancestors of that listener (streamlit.exe -> python.exe trees).
    2. Any python/streamlit process whose command line runs THIS project's
       app.py - catches orphans that no longer hold the port (e.g. already
       bumped to 8502 by Streamlit's silent port fallback).

  Scoped to this project's path, so other Streamlit apps on the machine are
  never touched. Idempotent: safe to run when nothing is running. Stops the
  whole process tree, then waits until the port is actually released.

.EXAMPLE
  .\Stop-Streamlit.ps1            # stop this app's processes, free port 8501
  .\Stop-Streamlit.ps1 -List      # dry run: show what would be stopped
  .\Stop-Streamlit.ps1 -Port 8502
#>
param(
  [int]$Port = 8501,
  [switch]$List
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectPattern = [regex]::Escape($root)

$targets = @()

function Add-Target($procId, $reason) {
  if ($targets | Where-Object { $_.Id -eq $procId }) { return }
  try { $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $procId" } catch { $proc = $null }
  if (-not $proc) { return }
  $script:targets += [PSCustomObject]@{
    Id          = $proc.ProcessId
    Name        = $proc.Name
    ParentId    = $proc.ParentProcessId
    Reason      = $reason
    CommandLine = $proc.CommandLine
  }
}

# 1) Whatever owns the dashboard port right now, plus its project-owned
#    ancestors (killing only the leaf would leave the streamlit.exe wrapper
#    waiting around and possibly respawning confusion later).
$portOwners = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
  Where-Object { $_.OwningProcess -gt 0 } |
  Select-Object -ExpandProperty OwningProcess -Unique
foreach ($procId in $portOwners) {
  Add-Target $procId "listening on port $Port"
  # Walk up the parent chain while the process belongs to this project.
  $cursor = $procId
  for ($i = 0; $i -lt 6; $i++) {
    try { $parent = (Get-CimInstance Win32_Process -Filter "ProcessId = $cursor").ParentProcessId } catch { break }
    if (-not $parent -or $parent -le 4) { break }
    try { $parentProc = Get-CimInstance Win32_Process -Filter "ProcessId = $parent" } catch { break }
    if (-not $parentProc) { break }
    if ($parentProc.CommandLine -notmatch $projectPattern) { break }
    Add-Target $parent "parent of port $Port listener"
    $cursor = $parent
  }
}

# 2) Any python/streamlit process launching this project's app.py - catches
#    orphans that no longer hold the port at all.
$appProcesses = Get-CimInstance Win32_Process | Where-Object {
  $_.Name -match '^(python|pythonw|streamlit)(\.exe)?$' -and
  $_.CommandLine -and
  $_.CommandLine -match $projectPattern -and
  $_.CommandLine -match 'app\.py'
}
foreach ($proc in $appProcesses) {
  Add-Target $proc.ProcessId "runs this project's app.py"
}

if (-not $targets) {
  Write-Host "No Streamlit dashboard processes found for this project (port $Port is free). Nothing to stop."
  exit 0
}

foreach ($t in $targets) {
  $cmd = ($t.CommandLine -replace '\s+', ' ').Trim()
  Write-Host ("  [{0}] {1} (PID {2})" -f $t.Reason, $t.Name, $t.Id)
  Write-Host ("      {0}" -f $cmd)
}
if ($List) {
  Write-Host "Dry run (-List): nothing was stopped."
  exit 0
}

foreach ($t in $targets) {
  # Use Start-Process rather than `& taskkill 2>&1`: killing the tree rooted at
  # the port listener usually takes the parents down first, so the later
  # iterations hit "process not found" on stderr - and with $ErrorAction-
  # Preference=Stop an in-stream stderr record would abort the whole cleanup.
  # Start-Process gives us the exit code without touching the error stream.
  $kill = Start-Process -FilePath "taskkill.exe" -ArgumentList @("/PID", $t.Id, "/T", "/F") -WindowStyle Hidden -PassThru -Wait
  if ($kill.ExitCode -eq 0) {
    Write-Host ("Stopped PID {0} ({1})." -f $t.Id, $t.Name)
  } else {
    # 128 = process already gone (usually because /T already took it down
    # as an ancestor of an earlier target) - that is success for our purpose.
    if ($kill.ExitCode -eq 128) {
      Write-Host ("PID {0} was already gone (stopped as part of an earlier tree)." -f $t.Id)
    } else {
      Write-Warning ("Could not stop PID {0} (taskkill exit {1}); re-run from an elevated shell." -f $t.Id, $kill.ExitCode)
    }
  }
}

# Wait until the port is actually released before reporting success.
$deadline = (Get-Date).AddSeconds(10)
do {
  Start-Sleep -Milliseconds 300
  $still = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
} while ($still -and (Get-Date) -lt $deadline)
if ($still) {
  Write-Warning ("Port {0} is still in use (PID {1})." -f $Port, ($still.OwningProcess -join ','))
} else {
  Write-Host ("Port {0} is free." -f $Port)
}
