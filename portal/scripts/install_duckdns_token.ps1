# Install the DuckDNS token on the server, once.
#
# After this, the server refreshes idan-python.duckdns.org every day by itself,
# so the free name is never deleted for inactivity. Nothing else to remember.
#
# Run from the project folder:
#
#   powershell -ExecutionPolicy Bypass -File portal\scripts\install_duckdns_token.ps1
#
# Get the token at https://www.duckdns.org while signed in: it is the long
# string shown next to the "idan-python" entry, not the account password.
#
# The token is typed at a hidden prompt. It is not echoed, not saved on this
# computer and not part of this project; it travels over SSH straight into
# /etc/duckdns/duckdns.conf (root only, mode 600).
param(
    [string]$Domain = "idan-python",
    [string]$Server = "ubuntu@158.178.250.254"
)

$ErrorActionPreference = "Stop"

$key = Resolve-Path (Join-Path $PSScriptRoot "..\data\deploy_key")
$installer = Resolve-Path (Join-Path $PSScriptRoot "..\deploy\duckdns-install-conf.sh")

Write-Host "DuckDNS token for '$Domain' - paste it and press Enter."
Write-Host "(Nothing appears while you paste. Cancel with Ctrl+C.)"
$secure = Read-Host -AsSecureString "Token"
$token = [System.Net.NetworkCredential]::new("", $secure).Password

if ([string]::IsNullOrWhiteSpace($token) -or $token -match '\s') {
    throw "empty token, or it contains spaces. Nothing was changed."
}
if ($token.Length -lt 20) {
    throw "that is far too short for a DuckDNS token. Nothing was changed."
}
if ($token -notmatch '^[0-9a-fA-F-]+$') {
    Write-Host "note: a DuckDNS token is normally hex digits and dashes."
    Write-Host "      Continuing anyway; DuckDNS will answer KO if it is wrong."
}

$conf = "DUCKDNS_DOMAIN=$Domain`nDUCKDNS_TOKEN=$token`n"

Write-Host "Uploading the installer..."
scp -q -i $key $installer "${Server}:/tmp/duckdns-install-conf.sh"
if ($LASTEXITCODE -ne 0) { throw "scp failed. Nothing was changed." }

$answer = ($conf | ssh -i $key $Server "sudo sh /tmp/duckdns-install-conf.sh" 2>&1)
$answer | ForEach-Object { Write-Host $_ }

if ($LASTEXITCODE -ne 0) {
    throw "the server could not finish; see the message above."
}
if ($answer -notmatch '(^|\s)OK') {
    throw "DuckDNS did not answer OK, so the token is probably wrong. The site itself still works: the name points at the reserved IP."
}

Write-Host ""
Write-Host "Done. The record was refreshed now and will be refreshed daily at 06:17,"
Write-Host "so idan-python.duckdns.org no longer expires."
