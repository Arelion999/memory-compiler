#!/bin/bash
# Снимок git-истории базы знаний одним самодостаточным файлом.
#
# Install on NAS:
#   1) copy to /usr/local/bin/mc-git-bundle.sh (chmod +x)
#   2) set BACKUP_DIR below (или через env)
#   3) /etc/crontab: 10 4 * * * root /usr/local/bin/mc-git-bundle.sh
#
# ЗАЧЕМ ОТДЕЛЬНО ОТ mc-backup.sh. Ежедневный tar содержит .git целиком, но чтобы
# добраться до истории, архив надо распаковать. Bundle — готовый к `git clone`
# файл: из него разворачивается рабочее зеркало за секунды, и им же проверяется,
# что история читается, а не просто лежит.
#
# ⚠️ GIT ЕСТЬ ТОЛЬКО ВНУТРИ КОНТЕЙНЕРА. На самом DSM его нет (проверено: `git
# --version` по ssh отвечает «not found»), поэтому и `git clone ssh://` к NAS
# невозможен — некому запустить git-upload-pack. Отсюда единственный рабочий
# путь: гонять git внутри контейнера, а вывод писать наружу перенаправлением.
#
# ⚠️ ВЫВОД В STDOUT, А НЕ ФАЙЛ В /knowledge. Контейнеру примонтирована только
# база, и файл, созданный внутри неё, попал бы и в индексацию, и в git, и в
# следующий бэкап — ровно тем классом мусора, из-за которого история распухла
# до 590 МБ (см. v1.59.1).

BACKUP_DIR="${BACKUP_DIR:-/path/to/memory-compiler/backups}"
CONTAINER="${CONTAINER:-memory-compiler-mcp}"
DOCKER="${DOCKER:-/usr/local/bin/docker}"
LOG="${MC_BUNDLE_LOG:-/var/log/mc-git-bundle.log}"
KEEP_DATED=4                                  # сколько датированных копий держим

mkdir -p "$BACKUP_DIR"
latest="$BACKUP_DIR/knowledge-git.bundle"
dated="$BACKUP_DIR/knowledge-git-$(date +%Y-%m-%d).bundle"
tmp="$latest.tmp"

if ! $DOCKER exec "$CONTAINER" git -C /knowledge bundle create - --all > "$tmp" 2>>"$LOG"; then
    echo "[$(date -Iseconds)] BUNDLE FAILED" >> "$LOG"
    rm -f "$tmp"
    exit 1
fi

# Пустой/обрезанный файл лучше не публиковать: он молча заменил бы рабочий снимок.
if [ ! -s "$tmp" ]; then
    echo "[$(date -Iseconds)] BUNDLE EMPTY — снимок не заменён" >> "$LOG"
    rm -f "$tmp"
    exit 1
fi

mv "$tmp" "$latest"
cp "$latest" "$dated"
size=$(du -h "$latest" | cut -f1)
heads=$($DOCKER exec "$CONTAINER" sh -c 'git -C /knowledge rev-list --count HEAD' 2>/dev/null)
echo "[$(date -Iseconds)] bundle ok: $latest ($size, коммитов $heads)" >> "$LOG"

# Датированные копии — на случай, если свежий снимок окажется испорчен.
ls -1t "$BACKUP_DIR"/knowledge-git-*.bundle 2>/dev/null | tail -n +$((KEEP_DATED + 1)) | while read -r old; do
    rm -f "$old"
done
