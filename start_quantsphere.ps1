$ErrorActionPreference = 'SilentlyContinue'
$root = "C:\Users\Dyudhi T G\quantsphere"

Write-Host "=== Starting QuantSphere ===" -ForegroundColor Cyan

# 1. PostgreSQL (no Windows service was registered - needs admin rights we
#    don't have - so it must be started manually every time).
$pgUp = Test-NetConnection -ComputerName localhost -Port 5432 -WarningAction SilentlyContinue
if (-not $pgUp.TcpTestSucceeded) {
    Write-Host "Starting PostgreSQL..."
    & "C:\Program Files\PostgreSQL\18\bin\pg_ctl.exe" start -D "C:\Program Files\PostgreSQL\18\data" -w
} else {
    Write-Host "PostgreSQL already running."
}

# 2. Ollama (local AI mentor)
$ollamaUp = Get-Process ollama -ErrorAction SilentlyContinue
if (-not $ollamaUp) {
    Write-Host "Starting Ollama..."
    Start-Process "C:\Users\Dyudhi T G\AppData\Local\Programs\Ollama\ollama app.exe"
    Start-Sleep -Seconds 5
} else {
    Write-Host "Ollama already running."
}

# 3. QuantSphere API + frontend server
$qsUp = Test-NetConnection -ComputerName localhost -Port 8000 -WarningAction SilentlyContinue
if (-not $qsUp.TcpTestSucceeded) {
    Write-Host "Starting QuantSphere server..."
    Start-Process -WindowStyle Hidden -WorkingDirectory $root `
        -FilePath "$root\.venv\Scripts\python.exe" `
        -ArgumentList "-m uvicorn app.main:app --host 127.0.0.1 --port 8000"
    Start-Sleep -Seconds 3
} else {
    Write-Host "QuantSphere server already running."
}

Write-Host "Opening QuantSphere..." -ForegroundColor Green
Start-Process "http://localhost:8000"
