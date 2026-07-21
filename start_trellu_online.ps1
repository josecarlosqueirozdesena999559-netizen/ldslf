$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$PidDir = Join-Path $Root "data"
$ServerPidFile = Join-Path $PidDir "trellu-server.pid"
$TunnelPidFile = Join-Path $PidDir "trellu-tunnel.pid"

if (-not (Test-Path $PidDir)) {
    New-Item -ItemType Directory -Path $PidDir | Out-Null
}

$Cloudflared = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
if (-not $Cloudflared) {
    $Cloudflared = Get-ChildItem -Path $env:LOCALAPPDATA, $env:ProgramFiles -Recurse -Filter cloudflared.exe -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
}
if (-not $Cloudflared) {
    Write-Host "cloudflared nao encontrado." -ForegroundColor Yellow
    exit 1
}

function Test-TrelluServer {
    try {
        Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8000/api/state" -TimeoutSec 3 | Out-Null
        return $true
    } catch {
        return $false
    }
}

try {
    if (-not (Test-TrelluServer)) {
        throw "Servidor local offline"
    }
    Write-Host "Servidor local rodando em http://127.0.0.1:8000" -ForegroundColor Green
} catch {
    Write-Host "Iniciando servidor local em http://127.0.0.1:8000 ..." -ForegroundColor Cyan
    $Server = Start-Process -FilePath python `
        -ArgumentList "-m uvicorn web_main:app --host 127.0.0.1 --port 8000" `
        -WorkingDirectory $Root `
        -WindowStyle Hidden `
        -PassThru
    Set-Content -Path $ServerPidFile -Value $Server.Id
    Start-Sleep -Seconds 4
    if (-not (Test-TrelluServer)) {
        Write-Host "Servidor local nao respondeu. Veja uvicorn.err.log." -ForegroundColor Red
        exit 1
    }
    Write-Host "Servidor local iniciado em segundo plano. PID $($Server.Id)" -ForegroundColor Green
}

$TunnelRunning = @(Get-Process cloudflared -ErrorAction SilentlyContinue).Count -gt 0
if ($TunnelRunning) {
    Write-Host "Tunel Cloudflare ja esta rodando." -ForegroundColor Green
} else {
    Write-Host "Iniciando tunel Cloudflare em segundo plano..." -ForegroundColor Cyan
    $Tunnel = Start-Process -FilePath $Cloudflared `
        -ArgumentList "tunnel --config `"$Root\cloudflared-trellu.yml`" run trellu-online" `
        -WorkingDirectory $Root `
        -WindowStyle Hidden `
        -PassThru
    Set-Content -Path $TunnelPidFile -Value $Tunnel.Id
    Start-Sleep -Seconds 3
    Write-Host "Tunel Cloudflare iniciado em segundo plano. PID $($Tunnel.Id)" -ForegroundColor Green
}

Write-Host ""
Write-Host "Dominio fixo online: https://trellu.online" -ForegroundColor Cyan
Write-Host "Pode fechar o navegador e esta janela. Enquanto o computador ficar ligado, o servidor continua monitorando/operando." -ForegroundColor Green
Write-Host "Para parar tudo, rode .\stop_trellu_online.ps1" -ForegroundColor Yellow
Write-Host ""
