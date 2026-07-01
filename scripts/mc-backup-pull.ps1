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
# secrets\ holds cleartext .env (MC_ENCRYPT_KEY) — restrict to current user
icacls $secretsDir /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" | Out-Null

if (-not (Test-Path $Source)) {
    Write-Log "FATAL: source not found: $Source"
    exit 1
}

# 1. Копируем только отсутствующие архивы (сравнение по имени)
$copied = 0
Get-ChildItem -Path $Source -Filter "knowledge-*.tar.gz" -File | ForEach-Object {
    if ($_.Length -eq 0) { Write-Log ("WARN: skip zero-byte source {0}" -f $_.Name); return }
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
        # Sort by name works because stamp is yyyy-MM-dd (lexical == chronological)
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

# NAS source keeps ~7 daily archives; if newest source archive is >7 days old,
# the task has been down long enough to risk losing a monthly (-01) archive.
$newest = Get-ChildItem -Path $Source -Filter "knowledge-*.tar.gz" -File |
    Where-Object { $_.Name -match 'knowledge-(\d{4})-(\d{2})-(\d{2})\.tar\.gz' } |
    Sort-Object Name -Descending | Select-Object -First 1
if ($newest -and $newest.Name -match 'knowledge-(\d{4})-(\d{2})-(\d{2})\.tar\.gz') {
    $newestDate = Get-Date -Year ([int]$Matches[1]) -Month ([int]$Matches[2]) -Day ([int]$Matches[3])
    if ($newestDate -lt (Get-Date).AddDays(-7)) {
        Write-Log ("WARN: newest source archive {0} older than 7 days — pull may be missing archives" -f $newest.Name)
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
