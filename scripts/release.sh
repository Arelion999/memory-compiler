#!/bin/bash
# Release script for memory-compiler — исполняемая версия регламента релиза
# Проверяет инвариант, гоняет тесты и скан утечек, коммитит весь диф, тегает, пушит.
#
# Usage:
#   bash scripts/release.sh patch  "суть релиза" [fix|feat|security|docs|refactor|chore]
#   bash scripts/release.sh minor  "суть релиза"
#   bash scripts/release.sh 1.2.3  "суть релиза"
#
# Перед запуском: код готов, тесты написаны, доки обновлены, секция CHANGELOG добавлена.

set -e
cd "$(dirname "$0")/.."

BUMP="$1"
SUMMARY="$2"
TYPE="${3:-release}"

[ ! -f VERSION ] && { echo "VERSION file missing"; exit 1; }
CURRENT=$(cat VERSION | tr -d '[:space:]')

if [ -z "$BUMP" ]; then
    echo "Usage: $0 {patch|minor|major|X.Y.Z} \"суть\" [type]"
    echo "Current: $CURRENT"
    exit 1
fi

case "$BUMP" in
    patch|minor|major)
        IFS='.' read -ra P <<< "$CURRENT"
        MAJOR=${P[0]}; MINOR=${P[1]}; PATCH=${P[2]}
        case "$BUMP" in
            patch) PATCH=$((PATCH+1)) ;;
            minor) MINOR=$((MINOR+1)); PATCH=0 ;;
            major) MAJOR=$((MAJOR+1)); MINOR=0; PATCH=0 ;;
        esac
        NEW="$MAJOR.$MINOR.$PATCH"
        ;;
    [0-9]*.[0-9]*.[0-9]*) NEW="$BUMP" ;;
    *) echo "Invalid: $BUMP (expected patch|minor|major|X.Y.Z)"; exit 1 ;;
esac

echo "== Релиз $CURRENT → $NEW =="

# --- Шаг 3: инвариант — секция CHANGELOG под новую версию должна существовать ---
TOP_CL=$(grep -m1 -oE '^## v[0-9]+\.[0-9]+\.[0-9]+' CHANGELOG.md | sed 's/^## v//')
if [ "$TOP_CL" != "$NEW" ]; then
    echo "ОШИБКА: верхняя секция CHANGELOG.md = v$TOP_CL, ожидалось v$NEW."
    echo "Сначала добавь секцию '## v$NEW — $(date +%F)' в CHANGELOG.md."
    exit 1
fi

# --- Шаг 1: тесты ---
echo "== pytest =="
python -m pytest tests/ -q || { echo "ОШИБКА: тесты упали — релиз отменён."; exit 1; }

# --- Шаг 4: bump VERSION ---
echo "$NEW" > VERSION

# --- Шаг 5: проверка на утечки (staged) ---
echo "== git add -A =="
git add -A
git status --short

echo "== gitleaks (staged) =="
if command -v gitleaks >/dev/null 2>&1; then
    gitleaks protect --staged --no-banner --redact || { echo "ОШИБКА: gitleaks нашёл секреты — релиз отменён."; exit 1; }
else
    echo "ОШИБКА: gitleaks не установлен — шаг проверки утечек пропустить нельзя."; exit 1
fi
if git config --get alias.audit-secrets >/dev/null 2>&1; then
    echo "== git audit-secrets =="
    git audit-secrets || { echo "ОШИБКА: audit-secrets нашёл секреты — релиз отменён."; exit 1; }
fi

# --- Шаги 6-8: коммит, тег, push ---
MSG="$TYPE: v$NEW"
[ -n "$SUMMARY" ] && MSG="$MSG — $SUMMARY"
echo ""
echo "Коммит:  $MSG"
echo "Тег:     v$NEW (аннотированный)"
echo "Push:    origin master --follow-tags"
echo ""
echo ">>> ШАГ 6: СЕЙЧАС задеплой рабочее дерево на NAS (VERSION уже поднят) и проверь"
echo "    вживую: /api/health отдаёт v$NEW; при смене транспорта — /mcp handshake + /sse 401."
echo "    Тег ставим ТОЛЬКО после успешной живой верификации (см. регламент релиза)."
read -p "Задеплоено и верифицировано вживую? Продолжить коммит+тег+push? [y/N] " yn
[ "$yn" != "y" ] && { echo "Отменено. VERSION уже поднят и файлы staged — откати вручную при желании."; exit 0; }

git commit -m "$MSG"
git tag -a "v$NEW" -m "$MSG"
git push --follow-tags

echo ""
echo "Готово: v$NEW запушен вместе с тегом."
echo "Автодеплой подхватит изменения на NAS в течение ~1 мин (при изменениях зависимостей — python deploy_image.py)."
