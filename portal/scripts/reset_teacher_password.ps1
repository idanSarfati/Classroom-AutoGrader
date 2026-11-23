# Reset the teacher password on the live Oracle server.
#
# Run from the project folder in PowerShell:
#
#   powershell -ExecutionPolicy Bypass -File portal\scripts\reset_teacher_password.ps1
#
# The new password is typed twice at the hidden prompt and never leaves this
# computer: it travels to the server over SSH inside the encrypted session.
# Nothing is written to shell history or to this file.

param(
    [string]$Username = "idan.sarfati",
    [string]$Name = "Idan Sarfati",
    [string]$Server = "ubuntu@158.178.250.254"
)

$ErrorActionPreference = "Stop"
$key = Join-Path $PSScriptRoot "..\data\deploy_key" | Resolve-Path

Write-Host "Resetting the password for '$Username' on $Server"
Write-Host "You will be asked for the new password twice; typing stays hidden."
Write-Host ""

$remote = "cd /opt/clasroom-autograder && sudo -u portal .venv/bin/python -m portal.scripts.bootstrap_teacher --username $Username --name '$Name'"
ssh -t -i $key $Server $remote

Write-Host ""
Write-Host "If you saw 'Teacher '$Username' updated', sign in at https://158.178.250.254.sslip.io/login"
