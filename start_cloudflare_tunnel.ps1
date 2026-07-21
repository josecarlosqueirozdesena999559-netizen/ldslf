$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Cloudflared = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
if (-not $Cloudflared) {
    $Cloudflared = Get-ChildItem -Path $env:LOCALAPPDATA, $env:ProgramFiles -Recurse -Filter cloudflared.exe -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
}
if (-not $Cloudflared) {
    Write-Host "cloudflared nao encontrado. Instale com:" -ForegroundColor Yellow
    Write-Host "winget install --id Cloudflare.cloudflared -e --accept-package-agreements --accept-source-agreements"
    exit 1
}

try {
    Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8000/api/state" -TimeoutSec 3 | Out-Null
    Write-Host "Servidor local ja esta rodando em http://127.0.0.1:8000" -ForegroundColor Green
} catch {
    Write-Host "Iniciando servidor local em http://127.0.0.1:8000 ..." -ForegroundColor Cyan
    Start-Process -FilePath python -ArgumentList "-m uvicorn web_main:app --host 127.0.0.1 --port 8000" -WorkingDirectory $Root -WindowStyle Hidden
    Start-Sleep -Seconds 4
}

Write-Host ""
Write-Host "Abrindo Cloudflare Tunnel..." -ForegroundColor Cyan
Write-Host "Copie o link https://...trycloudflare.com que aparecer abaixo." -ForegroundColor Yellow
Write-Host "Para parar o tunel, pressione CTRL+C nesta janela." -ForegroundColor Yellow
Write-Host ""

& $Cloudflared tunnel --protocol http2 --url http://127.0.0.1:8000
