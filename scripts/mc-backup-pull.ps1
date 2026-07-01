[CmdletBinding()]
param(
    [string]$Source   = "C:\Users\areli\SynologyDrive\DEV\memory-compiler\backups",
    [string]$EnvFile  = "C:\Users\areli\SynologyDrive\DEV\memory-compiler\.env",
    [string]$Dest     = "C:\Backups\memory-compiler",
    [int]$KeepDays    = 30,
    [int]$KeepSecrets = 5,
    [switch]$Verify
)

$ErrorActionPreference = "Stop"

$archivesDir = Join-Path $Dest "archives"
$secretsDir  = Join-Path $Dest "secrets"
$logFile     = Join-Path $Dest "pull.log"

function Write-Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-ddTHH:mm:ss"), $msg
    Add-Content -Path $logFile -Value $line
    Write-Host $line
}

New-Item -ItemType Directory -Force -Path $archivesDir | Out-Null
New-Item -ItemType Directory -Force -Path $secretsDir  | Out-Null

if (-not (Test-Path $Source)) {
    Write-Log "FATAL: source not found: $Source"
    exit 1
}

# 1. Копируем только отсутствующие архивы (сравнение по имени)
$copied = 0
Get-ChildItem -Path $Source -Filter "knowledge-*.tar.gz" -File | ForEach-Object {
    $target = Join-Path $archivesDir $_.Name
    if (-not (Test-Path $target)) {
        Copy-Item $_.FullName $target
        $copied++
    }
}

# 2. Снимок секретов (.env), хранить последние $KeepSecrets
if (Test-Path $EnvFile) {
    $stamp = Get-Date -Format "yyyy-MM-dd"
    Copy-Item $EnvFile (Join-Path $secretsDir ".env-$stamp") -Force
    Get-ChildItem -Path $secretsDir -Filter ".env-*" -File |
        Sort-Object Name -Descending |
        Select-Object -Skip $KeepSecrets |
        Remove-Item -Force
} else {
    Write-Log "WARN: env file not found: $EnvFile"
}

# 3. Ретенция по дате В ИМЕНИ файла; архивы за 1-е число месяца не трогаем
$cutoff = (Get-Date).AddDays(-$KeepDays)
$deleted = 0
Get-ChildItem -Path $archivesDir -Filter "knowledge-*.tar.gz" -File | ForEach-Object {
    if ($_.Name -match 'knowledge-(\d{4})-(\d{2})-(\d{2})\.tar\.gz') {
        $fileDate = Get-Date -Year ([int]$Matches[1]) -Month ([int]$Matches[2]) -Day ([int]$Matches[3])
        if (([int]$Matches[3]) -ne 1 -and $fileDate -lt $cutoff) {
            Remove-Item $_.FullName -Force
            $deleted++
        }
    }
}

$total = (Get-ChildItem $archivesDir -Filter 'knowledge-*.tar.gz' -File).Count
Write-Log ("OK: copied={0} deleted={1} archives_total={2}" -f $copied, $deleted, $total)

# 4. Опциональная проверка восстановления
if ($Verify) {
    & (Join-Path $PSScriptRoot "mc-backup-verify.ps1") -ArchiveDir $archivesDir
    if ($LASTEXITCODE -ne 0) { Write-Log "VERIFY FAILED"; exit 1 }
    Write-Log "VERIFY PASS"
}
