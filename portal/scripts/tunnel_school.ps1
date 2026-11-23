# Public HTTPS address for a network that blocks Cloudflare's tunnel port.
#
# The school network allows outbound 443 but blocks 7844, which cloudflared
# needs, so run_public.ps1 cannot open its quick tunnel there. This script
# tunnels the portal over SSH port 443 instead and writes the address to
# portal\data\public_url.txt.
#
# Free providers are used in order and the first one that answers wins:
# Pinggy has no warning page but closes an anonymous tunnel after an hour;
# Serveo keeps the tunnel open longer but may show a one-click warning page
# to a visitor. Either address changes when the tunnel is reopened.
$ErrorActionPreference = 'Stop'

$portal = Split-Path -Parent $PSScriptRoot
$work = Split-Path -Parent $portal
$python = Join-Path $work '.venv\Scripts\python.exe'
$ssh = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
$urlFile = Join-Path $portal 'data\public_url.txt'
$uvout = Join-Path $portal 'data\uvicorn.out.log'
$uverr = Join-Path $portal 'data\uvicorn.err.log'

$providers = @(
    @{
        Name = 'Pinggy'
        Host = 'a.pinggy.io'
        Forward = '-R0:localhost:8000'
        Log = 'pinggy.log'
        Pattern = 'https://[a-zA-Z0-9-]+\.free\.pinggy\.net'
    },
    @{
        Name = 'Serveo'
        Host = 'serveo.net'
        Forward = '-R 80:localhost:8000'
        Log = 'serveo.log'
        Pattern = 'https://[a-zA-Z0-9-]+\.serveousercontent\.com'
    }
)

function Test-Portal {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8000/healthz' -TimeoutSec 3
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Read-TunnelUrl($logPath, $pattern) {
    if (-not (Test-Path $logPath)) { return $null }
    $match = Select-String -Path $logPath -Pattern $pattern | Select-Object -Last 1
    if ($match) { return $match.Matches[0].Value }
    return $null
}

function Find-Tunnel($provider) {
    return Get-CimInstance Win32_Process -Filter "Name = 'ssh.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*localhost:8000*$($provider.Host)*" }
}

if (-not (Test-Portal)) {
    Write-Host 'Starting the portal...'
    $cmd = "cmd /c `"`"$python`" -m uvicorn portal.main:app --host 127.0.0.1 --port 8000 >> `"$uvout`" 2>> `"$uverr`"`""
    [void]([wmiclass]'Win32_Process').Create($cmd, $work, $null)
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-Portal) { break }
    }
}

if (-not (Test-Portal)) {
    Write-Error 'The portal did not start. See portal\data\uvicorn.err.log.'
    exit 1
}
Write-Host 'Portal: http://127.0.0.1:8000'

# Reuse a tunnel that is already open and has announced its address.
$chosen = $null
$url = $null
foreach ($provider in $providers) {
    $existing = Find-Tunnel $provider
    if (-not $existing) { continue }
    $logPath = Join-Path $portal "data\$($provider.Log)"
    $url = Read-TunnelUrl $logPath $provider.Pattern
    if ($url) { $chosen = $provider; break }
    # A connection without an address is stuck; drop it before retrying.
    foreach ($proc in $existing) {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

if (-not $url) {
    foreach ($provider in $providers) {
        Write-Host "Opening the public HTTPS address ($($provider.Name) over port 443)..."
        $logPath = Join-Path $portal "data\$($provider.Log)"
        Remove-Item $logPath -ErrorAction SilentlyContinue
        $cmd = "cmd /c `"`"$ssh`" -p 443 -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -T $($provider.Forward) $($provider.Host) > `"$logPath`" 2>&1`""
        [void]([wmiclass]'Win32_Process').Create($cmd, $work, $null)
        for ($i = 0; $i -lt 40; $i++) {
            Start-Sleep -Milliseconds 500
            $url = Read-TunnelUrl $logPath $provider.Pattern
            if ($url) { break }
        }
        if ($url) { $chosen = $provider; break }
        Write-Host "  $($provider.Name) did not answer; trying the next one."
    }
}

if (-not $url) {
    Write-Error 'No tunnel opened. See portal\data\pinggy.log and portal\data\serveo.log.'
    exit 1
}

Set-Content -Path $urlFile -Value $url -Encoding ascii
Write-Host ''
Write-Host "Public address: $url"
Write-Host "Tunnel: $($chosen.Name), port 443. Run this script again if the address stops working."
