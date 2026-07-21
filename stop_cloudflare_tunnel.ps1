$ErrorActionPreference = "SilentlyContinue"

Get-Process cloudflared | Stop-Process -Force
Write-Host "Cloudflare Tunnel parado."

Write-Host "Se quiser parar tambem o servidor local, feche o processo Python/uvicorn pelo terminal ou Gerenciador de Tarefas."
