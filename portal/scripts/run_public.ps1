# Start the portal and its temporary public HTTPS address (Cloudflare Quick
# Tunnel) with one command. The tunnel needs this computer to stay on and
# gives a new address every time cloudflared is restarted; the current address
# is written to portal\data\public_url.txt.
$ErrorActionPreference = 'Stop'

$portal = Split-Path -Parent $PSScriptRoot
$work = Split-Path -Parent $portal
$python = Join-Path $work '.venv\Scripts\python.exe'
$tools = Join-Path $portal 'data\tools'
$cloudflared = Join-Path $tools 'cloudflared.exe'
$log = Join-Path $portal 'data\cloudflared.log'
$urlFile = Join-Path $portal 'data\public_url.txt'
$uvout = Join-Path $portal 'data\uvicorn.out.log'
$uverr = Join-Path $portal 'data\uvicorn.err.log'

function Test-Portal {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8000/healthz' -TimeoutSec 3
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
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

if (-not (Test-Path $cloudflared)) {
    New-Item -ItemType Directory -Force -Path $tools | Out-Null
    Write-Host 'Downloading cloudflared (first run only)...'
    Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile $cloudflared
}

if (-not (Get-Process cloudflared -ErrorAction SilentlyContinue)) {
    Write-Host 'Opening the public HTTPS address...'
    $cmd = "cmd /c `"`"$cloudflared`" tunnel --url http://127.0.0.1:8000 --no-autoupdate --logfile `"$log`" --loglevel info > NUL 2>&1`""
    [void]([wmiclass]'Win32_Process').Create($cmd, $work, $null)
    Start-Sleep -Seconds 12
}

$url = $null
if (Test-Path $log) {
    $match = Select-String -Path $log -Pattern 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' | Select-Object -Last 1
    if ($match) { $url = $match.Matches[0].Value }
}

if (-not $url) {
    Write-Error "The tunnel is starting but no address was found yet. Check portal\data\cloudflared.log."
    exit 1
}

Set-Content -Path $urlFile -Value $url -Encoding ascii
Write-Host ''
Write-Host "Public address: $url"
Write-Host 'Keep this computer on; the address changes if the tunnel restarts.'
