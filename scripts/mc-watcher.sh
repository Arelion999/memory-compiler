#!/bin/bash
# Auto-restart memory-compiler container when source code changes.
# Install on NAS:
#   1) copy to /usr/local/bin/mc-watcher.sh (chmod +x)
#   2) set MC_DIR below to your memory_compiler/ directory
#   3) add to /etc/crontab: */1 * * * * root /usr/local/bin/mc-watcher.sh
#
# Детект по SHA1 СОДЕРЖИМОГО (*.py + VERSION), НЕ по mtime: SynologyDrive не
# обновляет mtime монотонно (приносит файлы со старым timestamp и не атомарно),
# из-за чего mtime-версия watcher'а пропускала изменения и/или рестартила на
# полпути синка (напр. новый tools.py, но ещё старый VERSION → health показывал
# старую версию). Плюс VERSION раньше вообще не отслеживался (не *.py).
#
# Debounce: рестарт только когда хэш стабилен 2 прогона подряд (~1 мин без
# изменений = синк устоялся) — не перезапускаем контейнер на промежуточном
# состоянии. Цена: деплой задерживается примерно на 1 минуту.

MC_DIR="${MC_DIR:-/path/to/memory-compiler/memory_compiler}"
VERSION_FILE="${VERSION_FILE:-$MC_DIR/../VERSION}"
STATE="${MC_STATE:-/var/log/mc-watcher.state}"       # последний ЗАДЕПЛОЕННЫЙ хэш
PENDING="${MC_PENDING:-/var/log/mc-watcher.pending}" # кандидат "hash count" для debounce
LOG="${MC_LOG:-/var/log/mc-watcher.log}"
DOCKER="${DOCKER:-/usr/local/bin/docker}"
CONTAINER="${CONTAINER:-memory-compiler-mcp}"

EMPTY_SHA1="da39a3ee5e6b4b0d3255bfef95601890afd80709"  # sha1 пустого ввода

# Хэш содержимого всех *.py (с путями) + VERSION.
current=$( { find "$MC_DIR" -name "*.py" -type f -exec sha1sum {} \; ;
             [ -f "$VERSION_FILE" ] && sha1sum "$VERSION_FILE" ; } 2>/dev/null \
           | sort | sha1sum | awk '{print $1}')

# Пусто/ошибка find (каталог недоступен во время синка) — ничего не делаем.
[ -z "$current" ] && exit 0
[ "$current" = "$EMPTY_SHA1" ] && exit 0

last=$(cat "$STATE" 2>/dev/null || echo "")
if [ "$current" = "$last" ]; then
  : > "$PENDING"   # уже задеплоено — сбросить незавершённый кандидат
  exit 0
fi

# Изменение есть. Debounce: считаем, сколько прогонов подряд хэш неизменен.
phash=""; pcount=0
if [ -f "$PENDING" ]; then
  read -r phash pcount < "$PENDING" 2>/dev/null || { phash=""; pcount=0; }
fi
if [ "$current" = "$phash" ]; then
  pcount=$((pcount + 1))
else
  pcount=1
fi
echo "$current $pcount" > "$PENDING"

if [ "$pcount" -lt 2 ]; then
  echo "[$(date -Iseconds)] change pending (hash ${current:0:12}, seen $pcount) — жду стабилизации синка" >> "$LOG"
  exit 0
fi

echo "[$(date -Iseconds)] stable change -> restart $CONTAINER (hash ${current:0:12})" >> "$LOG"
$DOCKER restart "$CONTAINER" >> "$LOG" 2>&1
echo "$current" > "$STATE"
: > "$PENDING"
