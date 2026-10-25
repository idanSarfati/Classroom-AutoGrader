' ============================================================================
' Launch-DashboardService.vbs
'
' Windowless launcher for the always-on dashboard service.
'
' WHY THIS FILE EXISTS
' --------------------
' Task Scheduler's action was `powershell.exe -WindowStyle Hidden -File
' Run-Dashboard.ps1 -Service`, and that is NOT enough to guarantee silence on
' this machine. Two windows still appeared at logon:
'
'   1. The Streamlit server's own console (fixed in Run-Dashboard.ps1 by
'      launching it with -WindowStyle Hidden).
'   2. The PowerShell action itself. `-WindowStyle Hidden` is a request, not a
'      guarantee: the console host (conhost.exe, and Windows Terminal when it
'      takes over console delegation) can still realise a visible window before
'      the style is applied, especially at logon when the desktop is still
'      initialising.
'
' WScript.Shell.Run with intWindowStyle = 0 asks the shell to create the child
' with SW_HIDE from the very first moment, which is the only reliable way to get
' a truly invisible launch. wscript.exe itself is already windowless, so no
' window of ours is ever realised on screen.
'
' This launcher WAITS for the dashboard to finish (see bWaitOnReturn below), so
' the scheduled task stays in the Running state and keeps supervising it.
'
' The dashboard itself is unaffected: it is a localhost web server, so it needs
' no console, no window and no terminal to run.
'
' Registered by Register-DashboardService.ps1 as the task action:
'     wscript.exe  "Launch-DashboardService.vbs"  "<Run-Dashboard.ps1>"  <port>
' ============================================================================
Option Explicit

Dim shell, fso
Dim scriptPath, scriptDir, runner, port
Dim projectDir, psExe, command, exitCode

Set fso   = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

' Locate things relative to THIS file, so the service keeps working if the
' project is ever moved.
scriptPath = WScript.ScriptFullName
scriptDir  = fso.GetParentFolderName(scriptPath)
runner     = scriptDir & "\Run-Dashboard.ps1"
port       = "8501"

If WScript.Arguments.Count >= 1 Then runner = WScript.Arguments(0)
If WScript.Arguments.Count >= 2 Then port   = WScript.Arguments(1)

If Not fso.FileExists(runner) Then
  ' Nothing visible to log to yet, so record it where the service log lives.
  On Error Resume Next
  Dim stream, logFile
  logFile = scriptDir & "\logs\dashboard-service.log"
  Set stream = fso.OpenTextFile(logFile, 8, True)  ' 8 = ForAppending
  If Err.Number = 0 Then
    stream.WriteLine Now() & "  ERROR: Run-Dashboard.ps1 not found at " & runner
    stream.Close
  End If
  On Error GoTo 0
  WScript.Quit 1
End If

' Prefer the project's own virtual environment, so the service runs the same
' interpreter (and the same installed packages) as a manual launch.
projectDir = fso.GetParentFolderName(scriptDir)
psExe = shell.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\WindowsPowerShell\v1.0\powershell.exe"
If Not fso.FileExists(psExe) Then psExe = "powershell.exe"

' -WindowStyle Hidden is kept as a second layer of defence, but the decisive
' setting is the intWindowStyle 0 passed to Run below.
command = """" & psExe & """ -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & runner & """ -Service -Port " & port & " -NoBrowser"

' 0 = SW_HIDE (invisible window).
'
' bWaitOnReturn is deliberately TRUE, even though it looks counter-intuitive for
' a "background service". Two reasons:
'   1. The task action must stay alive for as long as the dashboard runs. If the
'      action exits, Task Scheduler marks the task COMPLETE and stops supervising
'      it - the dashboard survives only as an orphan process, and if it ever
'      crashes nothing restarts it. Waiting keeps the task in the Running state,
'      which is what drives the RestartOnFailure policy.
'   2. WScript.Shell.Run returns the child's exit code, so the real exit status
'      of Run-Dashboard.ps1 reaches Task Scheduler instead of being flattened to
'      a blanket 0. That is what lets a genuine failure (exit 1) trigger the
'      automatic restart rather than being silently reported as success.
'
' The wait costs nothing and is invisible: wscript.exe has no window, and the
' PowerShell it waits on has none either.
exitCode = shell.Run(command, 0, True)

' Propagate the real status so RestartOnFailure can do its job.
WScript.Quit exitCode
