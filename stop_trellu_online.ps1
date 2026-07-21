$ErrorActionPreference = "SilentlyContinue"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServerPidFile = Join-Path $Root "data\trellu-server.pid"
$TunnelPidFile = Join-Path $Root "data\trellu-tunnel.pid"

if (Test-Path $TunnelPidFile) {
    $TunnelPid = Get-Content $TunnelPidFile | Select-Object -First 1
    if ($TunnelPid) {
        Stop-Process -Id ([int]$TunnelPid) -Force
    }
    Remove-Item $TunnelPidFile -Force
}

Get-Process cloudflared | Stop-Process -Force

if (Test-Path $ServerPidFile) {
    $ServerPid = Get-Content $ServerPidFile | Select-Object -First 1
    if ($ServerPid) {
        Stop-Process -Id ([int]$ServerPid) -Force
    }
    Remove-Item $ServerPidFile -Force
}

$Connections = Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue
$Pids = $Connections | Select-Object -ExpandProperty OwningProcess -Unique
foreach ($Pid in $Pids) {
    if ($Pid -and $Pid -ne 0) {
        Stop-Process -Id $Pid -Force
    }
}

Write-Host "Servidor local e tunel Cloudflare parados."
