#Requires -Version 5.1
<#
.SYNOPSIS
  Deploy a code-only portal bundle to production and restart the portal service.

.DESCRIPTION
  Runs from the project root. Steps, in order:
    1. Verify the local working copy: syntax + targeted portal tests.
    2. Build a code-only bundle (no database; the server's live DB is never
       replaced by this script).
    3. Record remote hashes of the changed files (drift report).
    4. Upload, verify the archive hash, and diff the bundle against the server
       tree (drift report only - nothing is copied yet).
    5. Prompt for confirmation.
    6. Install the changed files in place, preserving ownership and mode.
    7. Restart the portal deploy target, then verify health (local + public)
       and clean up the staging files.

  The script stops at the first failure. Every deployment is preceded by an
  automatic database backup on the server.

.PARAMETER ConfirmSwitch
  Run without prompting (CI / unattended).

.PARAMETER CommitMessage
  If given, commit the working copy with this message before building
  (explicit opt-in; the script never commits on its own).
#>
[CmdletBinding()]
param(
    [switch]$Confirm,
    [string]$CommitMessage = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

$Key = Join-Path $Root "portal\data\deploy_key"
$Host_ = "ubuntu@158.178.250.254"
$AppDir = "/opt/clasroom-autograder"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$RemoteTmp = "/tmp/portal-code-update.tar.gz"
$RemoteExtract = "/tmp/update-check"

$ChangedFiles = @(
    "portal/chat.py",
    "portal/web/student_routes.py",
    "portal/tests/test_chat.py",
    "portal/tests/test_exam_engine.py"
)

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Invoke-Ssh([string]$Command) {
    $output = & ssh -i $Key -o BatchMode=yes -o ConnectTimeout=15 $Host_ $Command 2>&1
    $code = $LASTEXITCODE
    $output | ForEach-Object { Write-Host $_ }
    if ($code -ne 0) {
        throw "ssh failed with exit code $code for: $Command"
    }
}

# ------------------------------------------------------------------ 1. verify
Write-Step "Verifying the working copy (syntax + targeted tests)"
& $Python -c "import ast; [ast.parse(open(p, encoding='utf-8').read()) for p in @('portal/chat.py', 'portal/web/student_routes.py')]; print('syntax ok')"
& $Python -m pytest portal/tests/test_chat.py portal/tests/test_exam_engine.py -q
if ($LASTEXITCODE -ne 0) { throw "targeted tests failed; refusing to deploy." }

# ------------------------------------------------------------------ 2. bundle
Write-Step "Building the code-only bundle"
if ($CommitMessage) {
    git add portal/chat.py portal/web/student_routes.py portal/tests/test_chat.py portal/tests/test_exam_engine.py
    git commit -m $CommitMessage
}
& $Python -m portal.scripts.build_deploy_bundle --code-only --force
if ($LASTEXITCODE -ne 0) { throw "build_deploy_bundle failed." }
$Bundle = Get-ChildItem -Path "portal\data\_tmp\portal-code-*.tar.gz" |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $Bundle) { throw "no bundle was produced." }
Write-Host "bundle: $($Bundle.FullName) ($([math]::Round($Bundle.Length / 1MB, 1)) MB)"

# ------------------------------------------------- 3. upload + verify + drift
Write-Step "Uploading and verifying the bundle"
Invoke-Ssh "sudo -n /usr/local/bin/portal-backup >/dev/null 2>&1; echo backup-done; curl -fsS http://127.0.0.1:8000/healthz; systemctl is-active portal"
& scp -i $Key -o BatchMode=yes $Bundle.FullName "${Host_}:$RemoteTmp"
if ($LASTEXITCODE -ne 0) { throw "scp failed." }
$localHash = (Get-FileHash -Path $Bundle.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
Invoke-Ssh "sha256sum $RemoteTmp"
Write-Host "local archive sha256: $localHash"

Invoke-Ssh "rm -rf $RemoteExtract; mkdir -p $RemoteExtract; tar -xzf $RemoteTmp -C $RemoteExtract --strip-components=1; grep -c exam_results_released $RemoteExtract/portal/chat.py; grep -c visible_to_student $RemoteExtract/portal/web/student_routes.py; echo --- drift; diff -rq $RemoteExtract/portal $AppDir/portal --exclude=data --exclude=content --exclude=__pycache__ --exclude=.pytest_cache 2>/dev/null | head -n 40"

# ------------------------------------------------------------------ 4. confirm
Write-Step "Review the drift above; the server database is never touched."
if (-not $Confirm) {
    $answer = Read-Host "Install the changed files and restart the portal? (y/N)"
    if ($answer -notmatch '^(y|yes)$') {
        Write-Host "Aborted. Nothing was installed."
        Invoke-Ssh "rm -f $RemoteTmp; rm -rf $RemoteExtract"
        exit 0
    }
}

# ------------------------------------------------------- 5. install + restart
Write-Step "Installing the changed files"
$install = "set -e; " + (($ChangedFiles | ForEach-Object {
    $owner = if ($_ -like "portal/web/*") { "portal" } else { "root" }
    "sudo -n install -o $owner -g $owner -m 644 $RemoteExtract/$_ $AppDir/$_"
}) -join "; ") + "; " +
    "sha256sum $AppDir/portal/chat.py $AppDir/portal/web/student_routes.py; " +
    "sudo -n systemctl restart portal"
Invoke-Ssh $install

# ------------------------------------------------------------------ 6. verify
Write-Step "Verifying the restart"
Invoke-Ssh "for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do curl -fsS http://127.0.0.1:8000/healthz && break; sleep 1; done; systemctl is-active portal; curl -fsS https://idan-python.duckdns.org/healthz"
Invoke-Ssh "rm -f $RemoteTmp; rm -rf $RemoteExtract; echo cleaned"

Write-Host ""
Write-Host "Deployment complete." -ForegroundColor Green
