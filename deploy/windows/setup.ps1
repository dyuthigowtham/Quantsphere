#Requires -RunAsAdministrator
<#
  QuantSphere native Windows deployment (no Docker, no WSL2).

  Installs Postgres, Ollama, and NSSM via Chocolatey (you already have
  Chocolatey on this machine), creates the quantsphere DB role/database,
  writes a production .env at the repo root, builds/refreshes a venv, and
  registers two always-on Windows services (QuantSphere, OllamaServe) via
  NSSM so both survive reboots without anyone being logged in.

  Does NOT touch the Windows Firewall and does NOT open any inbound ports  - 
  the app only listens on 127.0.0.1:8000. Public exposure is handled
  separately by Cloudflare Tunnel (see deploy/windows/README.md), which
  makes an outbound-only connection and needs no port-forwarding.

  Safe to re-run: every step checks for existing state before changing it.
#>

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
$EnvFile = Join-Path $RepoRoot ".env"
$LogDir = Join-Path $RepoRoot "deploy\windows\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function New-RandomHex($bytes) {
    # Windows PowerShell 5.1 runs on .NET Framework, which lacks the static
    # RandomNumberGenerator.Fill() helper added in .NET 6 - use the
    # long-standing RNGCryptoServiceProvider API instead, which works on both.
    $buf = New-Object byte[] $bytes
    $rng = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
    $rng.GetBytes($buf)
    $rng.Dispose()
    -join ($buf | ForEach-Object { $_.ToString("x2") })
}

function Start-OrRestartService($name) {
    # Handles ANY starting state (Running, Stopped, or a stuck Paused left
    # over from earlier nssm confusion) - Start-Service alone only works
    # from Stopped, which is what caused a hard failure on a Paused service.
    $svc = Get-Service $name
    if ($svc.Status -ne "Stopped") {
        try { Stop-Service $name -Force -ErrorAction Stop } catch {
            Write-Host "Could not cleanly stop $name ($($_.Exception.Message)) - trying to start anyway." -ForegroundColor Yellow
        }
        $waited = 0
        while ((Get-Service $name).Status -ne "Stopped" -and $waited -lt 15) { Start-Sleep -Seconds 1; $waited++ }
    }
    Start-Service $name
}

Write-Host "== 1/7: Installing Postgres, Ollama, NSSM, cloudflared via Chocolatey ==" -ForegroundColor Cyan
$pgSuperPassword = New-RandomHex 16
choco install postgresql16 -y --params "/Password:$pgSuperPassword" --no-progress
choco install ollama -y --no-progress
choco install nssm -y --no-progress
choco install cloudflared -y --no-progress

# Chocolatey updates machine PATH; re-import it into this session so psql/ollama/nssm resolve without a new shell.
$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")

Write-Host "== 2/7: Locating psql and creating the quantsphere role/database ==" -ForegroundColor Cyan
# Pinned to the v16 install this script itself manages - deliberately NOT a
# recursive search under C:\Program Files\PostgreSQL, because this machine
# also has an unrelated pre-existing PostgreSQL 18 install running (found
# while debugging this script) that must never be touched or connected to.
$PgHome = "C:\Program Files\PostgreSQL\16"
$psqlPath = Join-Path $PgHome "bin\psql.exe"
if (-not (Test-Path $psqlPath)) { throw "psql.exe not found at $psqlPath - check the postgresql16 install succeeded." }
$pgConfPath = Join-Path $PgHome "data\postgresql.conf"

# v16's own port - NOT assumed to be 5432, since that port may already be
# taken by another PostgreSQL install on this machine (as it was here).
$pgPort = "5432"
$portLine = Get-Content $pgConfPath | Where-Object { $_ -match "^\s*port\s*=\s*(\d+)" } | Select-Object -First 1
if ($portLine -match "^\s*port\s*=\s*(\d+)") { $pgPort = $Matches[1] }
Write-Host "v16 is configured for port $pgPort" -ForegroundColor Cyan

# Wait for the postgres Windows service to actually be Running before
# testing connectivity - right after choco's install returns, the service
# can still be mid-startup, which used to cause a false "not reachable" and
# an unnecessary password prompt.
$pgService = Get-Service postgresql-x64-16 -ErrorAction SilentlyContinue
if ($pgService) {
    $waited = 0
    while ($pgService.Status -ne "Running" -and $waited -lt 30) {
        Start-Sleep -Seconds 2; $waited += 2
        $pgService = Get-Service $pgService.Name
    }
}

# A prior run may have already persisted a known-good password (either from
# a fresh install, or from deploy\windows\reset-postgres-password.ps1) -
# prefer that over the just-generated one so re-runs are idempotent.
$savedPwFile = "C:\ProgramData\QuantSphere\postgres_superuser_password.txt"
if (Test-Path $savedPwFile) {
    $pgSuperPassword = (Get-Content $savedPwFile -Raw).Trim()
}

$pgReachable = $false
$env:PGPASSWORD = $pgSuperPassword
for ($attempt = 1; $attempt -le 5 -and -not $pgReachable; $attempt++) {
    try { & $psqlPath -U postgres -h localhost -p $pgPort -c "SELECT 1" *> $null; $pgReachable = ($LASTEXITCODE -eq 0) } catch {}
    if (-not $pgReachable) { Start-Sleep -Seconds 3 }
}
if ($pgReachable) {
    New-Item -ItemType Directory -Force -Path "C:\ProgramData\QuantSphere" | Out-Null
    Set-Content -Path $savedPwFile -Value $pgSuperPassword -Encoding ascii
} else {
    Write-Host "Could not connect with the generated/saved password after several retries." -ForegroundColor Yellow
    Write-Host "If this is a genuinely pre-existing install with a different password, enter it now." -ForegroundColor Yellow
    Write-Host "If you're not sure, Ctrl+C and run deploy\windows\reset-postgres-password.ps1 instead." -ForegroundColor Yellow
    $securePw = Read-Host -AsSecureString
    $env:PGPASSWORD = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto([System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePw))
}

# Idempotent by construction rather than by checking psql's own output for
# "does it already exist" - CREATE ROLE/CREATE DATABASE simply fail
# harmlessly (printed to console, non-fatal since nothing here redirects
# their streams) when the role/database is already there. A prior version
# of this script tried to detect existence by regex-matching psql's -tAc
# output, which silently misfired and skipped CREATE ROLE entirely on a
# fresh database, leaving CREATE DATABASE ... OWNER quantsphere to fail
# with "role does not exist".
#
# Only ever (re)set the quantsphere role's password when we're also about
# to write a fresh .env with that same value - otherwise a plain re-run of
# this script would rotate the DB password out from under an .env that
# still has the old one, breaking an already-working install.
$envExists = Test-Path $EnvFile
if (-not $envExists) {
    $appDbPassword = New-RandomHex 16
    & $psqlPath -U postgres -h localhost -p $pgPort -c "CREATE ROLE quantsphere WITH LOGIN PASSWORD '$appDbPassword';"
    & $psqlPath -U postgres -h localhost -p $pgPort -c "ALTER ROLE quantsphere WITH PASSWORD '$appDbPassword';"
} else {
    Write-Host "quantsphere role's password left untouched since .env already exists." -ForegroundColor Yellow
    & $psqlPath -U postgres -h localhost -p $pgPort -c "CREATE ROLE quantsphere WITH LOGIN;"
}
& $psqlPath -U postgres -h localhost -p $pgPort -c "CREATE DATABASE quantsphere OWNER quantsphere;"
Remove-Item Env:\PGPASSWORD

Write-Host "== 3/7: Writing production .env ==" -ForegroundColor Cyan
if ($envExists) {
    Write-Host "$EnvFile already exists  -  leaving it untouched. Delete it first if you want a fresh one." -ForegroundColor Yellow
} else {
    $jwtSecret = New-RandomHex 32
    @"
ENVIRONMENT=production
DATABASE_URL=postgresql+asyncpg://quantsphere:$appDbPassword@localhost:$pgPort/quantsphere
JWT_SECRET=$jwtSecret
JWT_EXPIRE_MINUTES=1440
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_TEXT_MODEL=llama3
OLLAMA_VISION_MODEL=llava
MT5_ENABLED=false
MEDIA_ROOT=$($RepoRoot -replace '\\','/')/media
MAX_UPLOAD_MB=8
"@ | Set-Content -Path $EnvFile -Encoding utf8
    Write-Host "Wrote $EnvFile (kept out of git via .gitignore)." -ForegroundColor Green
}

Write-Host "== 4/7: Building the venv and installing QuantSphere ==" -ForegroundColor Cyan
$venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    python -m venv (Join-Path $RepoRoot ".venv")
}
& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install "$RepoRoot" --quiet

Write-Host "== 5/7: Registering OllamaServe as an always-on service ==" -ForegroundColor Cyan
$ollamaExe = (Get-Command ollama.exe -ErrorAction SilentlyContinue).Source
if (-not $ollamaExe) { throw "ollama.exe not found on PATH  -  open a new terminal and re-run this script." }
if (-not (Get-Service OllamaServe -ErrorAction SilentlyContinue)) {
    nssm install OllamaServe $ollamaExe "serve"
    nssm set OllamaServe AppStdout (Join-Path $LogDir "ollama.out.log")
    nssm set OllamaServe AppStderr (Join-Path $LogDir "ollama.err.log")
    nssm set OllamaServe Start SERVICE_AUTO_START
}
# Using the Start-Service/Stop-Service cmdlets rather than shelling out to
# `nssm restart` - redirecting a native command's stderr (e.g. `2>$null`)
# under $ErrorActionPreference = "Stop" turns it into a terminating error in
# Windows PowerShell 5.1 even when nssm itself handled the situation fine.
Start-OrRestartService OllamaServe
Start-Sleep -Seconds 3

Write-Host "== 6/7: Pulling Ollama models (this can take a while the first time) ==" -ForegroundColor Cyan
& $ollamaExe pull llama3
# llama3.2-vision uses the "mllama" architecture, which this Ollama build
# can't load ("unknown model architecture: 'mllama'") - llava is the
# confirmed-working fallback, matching what dev already settled on.
& $ollamaExe pull llava

Write-Host "== 7/7: Registering the QuantSphere app as an always-on service ==" -ForegroundColor Cyan
if (-not (Get-Service QuantSphere -ErrorAction SilentlyContinue)) {
    nssm install QuantSphere $venvPython "-m uvicorn app.main:app --host 127.0.0.1 --port 8000"
    nssm set QuantSphere AppDirectory $RepoRoot
    nssm set QuantSphere AppStdout (Join-Path $LogDir "quantsphere.out.log")
    nssm set QuantSphere AppStderr (Join-Path $LogDir "quantsphere.err.log")
    nssm set QuantSphere Start SERVICE_AUTO_START
}
Start-OrRestartService QuantSphere

Start-Sleep -Seconds 3
try {
    $health = Invoke-RestMethod http://127.0.0.1:8000/health -TimeoutSec 5
    Write-Host "QuantSphere is up: $($health | ConvertTo-Json -Compress)" -ForegroundColor Green
} catch {
    Write-Host "QuantSphere didn't respond on :8000 yet  -  check deploy\windows\logs\quantsphere.err.log" -ForegroundColor Red
}

Write-Host ""
Write-Host "Done. Next step: set up Cloudflare Tunnel for public access  -  see deploy\windows\README.md." -ForegroundColor Cyan
