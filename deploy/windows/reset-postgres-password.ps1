#Requires -RunAsAdministrator
<#
  Recovery script: resets the postgres superuser password on a fresh,
  otherwise-empty local install where the actual password was lost (e.g.
  setup.ps1's readiness check raced the service and asked you for a
  password you never really had).

  Deliberately scoped to C:\Program Files\PostgreSQL\16 only - this machine
  also has an unrelated pre-existing PostgreSQL 18 install running (found
  while debugging this script), and this never touches that instance's
  service, config, or port. QuantSphere only ever talks to the v16 one.

  Safe because it only touches auth config temporarily and this is a brand
  new install with no real data in it yet. Uses the standard "temporarily
  allow trust auth, reset the password, restore auth config" technique.
#>

$ErrorActionPreference = "Stop"
$PgHome = "C:\Program Files\PostgreSQL\16"

$service = Get-Service postgresql-x64-16 -ErrorAction SilentlyContinue
if (-not $service) { throw "Service postgresql-x64-16 not found - is the v16 install still present?" }
Write-Host "Found service: $($service.Name)" -ForegroundColor Cyan

$psql = Join-Path $PgHome "bin\psql.exe"
if (-not (Test-Path $psql)) { throw "psql.exe not found at $psql." }
$hbaPath = Join-Path $PgHome "data\pg_hba.conf"
$confPath = Join-Path $PgHome "data\postgresql.conf"
if (-not (Test-Path $hbaPath)) { throw "$hbaPath not found - unexpected PostgreSQL layout." }

# This machine has more than one PostgreSQL install; port 5432 was already
# taken by the unrelated pre-existing one, so v16 auto-selected a different
# port. Read it from its own postgresql.conf instead of assuming 5432.
$pgPort = "5432"
$portLine = Get-Content $confPath | Where-Object { $_ -match "^\s*port\s*=\s*(\d+)" } | Select-Object -First 1
if ($portLine -match "^\s*port\s*=\s*(\d+)") { $pgPort = $Matches[1] }
Write-Host "v16 is configured for port $pgPort" -ForegroundColor Cyan

$backupPath = "$hbaPath.qs-backup"

Write-Host "Backing up $hbaPath" -ForegroundColor Cyan
Copy-Item $hbaPath $backupPath -Force

Write-Host "Temporarily switching local auth to trust..." -ForegroundColor Cyan
$original = Get-Content $hbaPath
$trusted = $original | ForEach-Object {
    if ($_ -match "^\s*(host|local)\s") {
        ($_ -replace "\s(scram-sha-256|md5|password)\s*$", " trust")
    } else { $_ }
}
Set-Content -Path $hbaPath -Value $trusted -Encoding ascii

Write-Host "Restarting $($service.Name)..." -ForegroundColor Cyan
Restart-Service $service.Name -Force
$waited = 0
while ((Get-Service $service.Name).Status -ne "Running" -and $waited -lt 30) { Start-Sleep -Seconds 2; $waited += 2 }
Start-Sleep -Seconds 2

function New-RandomHex($bytes) {
    $buf = New-Object byte[] $bytes
    $rng = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
    $rng.GetBytes($buf)
    $rng.Dispose()
    -join ($buf | ForEach-Object { $_.ToString("x2") })
}
$newPassword = New-RandomHex 16

Write-Host "Setting a new postgres superuser password on port $pgPort..." -ForegroundColor Cyan
& $psql -U postgres -h localhost -p $pgPort -c "ALTER USER postgres WITH PASSWORD '$newPassword';"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Failed to set the password even with trust auth - restoring pg_hba.conf and stopping." -ForegroundColor Red
    Copy-Item $backupPath $hbaPath -Force
    Restart-Service $service.Name -Force
    throw "Password reset failed."
}

Write-Host "Restoring original pg_hba.conf and restarting..." -ForegroundColor Cyan
Copy-Item $backupPath $hbaPath -Force
Remove-Item $backupPath
Restart-Service $service.Name -Force
Start-Sleep -Seconds 5

$saveDir = "C:\ProgramData\QuantSphere"
New-Item -ItemType Directory -Force -Path $saveDir | Out-Null
$saveFile = Join-Path $saveDir "postgres_superuser_password.txt"
Set-Content -Path $saveFile -Value $newPassword -Encoding ascii
$portFile = Join-Path $saveDir "postgres_port.txt"
Set-Content -Path $portFile -Value $pgPort -Encoding ascii

Write-Host ""
Write-Host "Done. New postgres superuser password: $newPassword" -ForegroundColor Green
Write-Host "Also saved to $saveFile so this never gets lost again." -ForegroundColor Green
Write-Host "v16 port ($pgPort) saved to $portFile." -ForegroundColor Green
Write-Host "Re-run setup.ps1 now - it will pick up from here." -ForegroundColor Cyan
