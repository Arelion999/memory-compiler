<#
.SYNOPSIS
  Release script for memory-compiler — исполняемая версия регламента релиза.
  Проверяет инвариант, гоняет тесты и скан утечек, коммитит весь диф, тегает, пушит.

.EXAMPLE
  scripts/release.ps1 -Bump patch -Summary "фикс автотеггера" -Type fix
  scripts/release.ps1 -Bump 1.2.3 -Summary "суть"

.NOTES
  Перед запуском: код готов, тесты написаны, доки обновлены, секция CHANGELOG добавлена.
#>
param(
    [Parameter(Mandatory = $true)][string]$Bump,   # patch|minor|major|X.Y.Z
    [string]$Summary = "",
    [ValidateSet("fix", "feat", "security", "docs", "refactor", "chore", "release")]
    [string]$Type = "release"
)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not (Test-Path VERSION)) { Write-Error "VERSION file missing"; exit 1 }
$current = (Get-Content VERSION -Raw).Trim()

switch -Regex ($Bump) {
    '^(patch|minor|major)$' {
        $p = $current.Split('.')
        $maj = [int]$p[0]; $min = [int]$p[1]; $pat = [int]$p[2]
        switch ($Bump) {
            'patch' { $pat++ }
            'minor' { $min++; $pat = 0 }
            'major' { $maj++; $min = 0; $pat = 0 }
        }
        $new = "$maj.$min.$pat"
    }
    '^\d+\.\d+\.\d+$' { $new = $Bump }
    default { Write-Error "Invalid: $Bump (expected patch|minor|major|X.Y.Z)"; exit 1 }
}

Write-Host "== Релиз $current -> $new ==" -ForegroundColor Cyan

# --- Шаг 3: инвариант — секция CHANGELOG под новую версию должна существовать ---
$topLine = Select-String -Path CHANGELOG.md -Pattern '^## v(\d+\.\d+\.\d+)' | Select-Object -First 1
$topCl = if ($topLine) { $topLine.Matches[0].Groups[1].Value } else { "" }
if ($topCl -ne $new) {
    Write-Error "Верхняя секция CHANGELOG.md = v$topCl, ожидалось v$new. Сначала добавь секцию '## v$new — $(Get-Date -Format yyyy-MM-dd)'."
    exit 1
}

# --- Шаг 1: тесты ---
Write-Host "== pytest ==" -ForegroundColor Cyan
python -m pytest tests/ -q
if ($LASTEXITCODE -ne 0) { Write-Error "Тесты упали — релиз отменён."; exit 1 }

# --- Шаг 4: bump VERSION ---
Set-Content -Path VERSION -Value $new -NoNewline

# --- Шаг 5: проверка на утечки (staged) ---
Write-Host "== git add -A ==" -ForegroundColor Cyan
git add -A
git status --short

Write-Host "== gitleaks (staged) ==" -ForegroundColor Cyan
if (Get-Command gitleaks -ErrorAction SilentlyContinue) {
    gitleaks protect --staged --no-banner --redact
    if ($LASTEXITCODE -ne 0) { Write-Error "gitleaks нашёл секреты — релиз отменён."; exit 1 }
}
else {
    Write-Error "gitleaks не установлен — шаг проверки утечек пропустить нельзя."; exit 1
}
if (git config --get alias.audit-secrets) {
    Write-Host "== git audit-secrets ==" -ForegroundColor Cyan
    git audit-secrets
    if ($LASTEXITCODE -ne 0) { Write-Error "audit-secrets нашёл секреты — релиз отменён."; exit 1 }
}

# --- Шаги 6-8: коммит, тег, push ---
$msg = "${Type}: v$new"
if ($Summary) { $msg = "$msg — $Summary" }
Write-Host ""
Write-Host "Коммит:  $msg"
Write-Host "Тег:     v$new (аннотированный)"
Write-Host "Push:    origin --follow-tags"
Write-Host ""
Write-Host ">>> ШАГ 6: СЕЙЧАС задеплой рабочее дерево на NAS (VERSION уже поднят) и проверь" -ForegroundColor Yellow
Write-Host "    вживую: /api/health отдаёт v$new; при смене транспорта — /mcp handshake + /sse 401."
Write-Host "    Тег ставим ТОЛЬКО после успешной живой верификации (см. регламент релиза)."
$yn = Read-Host "Задеплоено и верифицировано вживую? Продолжить коммит+тег+push? [y/N]"
if ($yn -ne "y") { Write-Host "Отменено. VERSION поднят и файлы staged — откати вручную при желании."; exit 0 }

git commit -m $msg
git tag -a "v$new" -m $msg
git push --follow-tags

Write-Host ""
Write-Host "Готово: v$new запушен вместе с тегом." -ForegroundColor Green
Write-Host "Автодеплой подхватит изменения на NAS в течение ~1 мин (при изменениях зависимостей — python deploy_image.py)."
