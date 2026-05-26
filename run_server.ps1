$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$logDir = Join-Path $PSScriptRoot "data"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "server.log"
"$(Get-Date -Format o) Starting Stilta Evidence Graph Lab" | Out-File -FilePath $log -Encoding utf8
$python = "C:\Users\ASUS\miniconda3\python.exe"
& $python server.py *>> $log
