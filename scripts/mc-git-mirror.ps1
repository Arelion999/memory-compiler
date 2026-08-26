<#
.SYNOPSIS
    Живое зеркало git-истории базы знаний на ПК, вне NAS.

.DESCRIPTION
    Разворачивает и обновляет локальный bare-репозиторий из снимка
    knowledge-git.bundle, который NAS кладёт в backups/ (mc-git-bundle.sh).

    ЗАЧЕМ. Ежедневные архивы содержат .git, но добраться до истории можно только
    распаковав их целиком. Зеркало даёт историю под рукой: `git log`, `git show`,
    восстановление отдельного файла — без NAS и без распаковки. Диагностика
    2026-08-26 показала, что рабочего зеркала не было: локальный клон отстал на
    месяц (3 коммита против 3398 на проде), потому что Synology Drive
    синхронизирует статьи, но не .git базы.

    ПОЧЕМУ ЧЕРЕЗ BUNDLE, А НЕ `git clone ssh://`. На DSM нет git (есть только
    внутри контейнера), поэтому git-upload-pack на стороне NAS запустить некому.

.EXAMPLE
    pwsh -File scripts\mc-git-mirror.ps1
    schtasks /create /tn "memory-compiler git mirror" /tr "pwsh -File <repo>\scripts\mc-git-mirror.ps1" /sc daily /st 05:10
#>
[CmdletBinding()]
param(
    # Снимок из синхронизированной папки backups/ репозитория.
    [string]$Bundle = (Join-Path (Split-Path $PSScriptRoot -Parent) "backups\knowledge-git.bundle"),
    # Зеркало держим ВНЕ SynologyDrive: синк — не бэкап, удаления доезжают.
    [string]$Mirror = "C:\Backups\memory-compiler\knowledge-git-mirror.git",
    [string]$LogFile = "C:\Backups\memory-compiler\git-mirror.log"
)

$ErrorActionPreference = "Stop"

function Write-Log([string]$Message) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-ddTHH:mm:ss"), $Message
    $dir = Split-Path $LogFile -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
    Write-Host $line
}

if (-not (Test-Path $Bundle)) {
    Write-Log "FAIL: снимок не найден: $Bundle"
    exit 1
}

$age = (Get-Date) - (Get-Item $Bundle).LastWriteTime
if ($age.TotalDays -gt 3) {
    # Не ошибка, но снимок несвежий: NAS мог не отработать или встал синк.
    Write-Log ("WARN: снимку {0:N1} суток — проверь mc-git-bundle.sh на NAS" -f $age.TotalDays)
}

try {
    if (-not (Test-Path $Mirror)) {
        Write-Log "зеркала нет — разворачиваю из снимка"
        $parent = Split-Path $Mirror -Parent
        if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        & git clone --mirror --quiet -- $Bundle $Mirror
        if ($LASTEXITCODE -ne 0) { throw "git clone вернул $LASTEXITCODE" }
    } else {
        # Bundle выступает обычным remote: fetch дотягивает только новое.
        & git --git-dir=$Mirror fetch --quiet --prune -- $Bundle "+refs/heads/*:refs/heads/*"
        if ($LASTEXITCODE -ne 0) { throw "git fetch вернул $LASTEXITCODE" }
    }

    # Проверяем не факт запуска, а результат: история обязана читаться.
    $commits = (& git --git-dir=$Mirror rev-list --count HEAD 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $commits) { throw "зеркало не отдаёт историю" }
    $files = (& git --git-dir=$Mirror ls-tree -r --name-only HEAD 2>$null | Measure-Object -Line).Lines
    if ($files -lt 50) { throw "в HEAD зеркала всего $files файлов — снимок неполон" }

    $size = "{0:N0} МБ" -f ((Get-ChildItem $Mirror -Recurse -File | Measure-Object Length -Sum).Sum / 1MB)
    Write-Log "OK: коммитов $commits, файлов в HEAD $files, размер $size"
    exit 0
} catch {
    Write-Log "FAIL: $_"
    exit 1
}
