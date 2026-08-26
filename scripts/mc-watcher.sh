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
#
# Cooldown (MC_COOLDOWN, по умолчанию 300 с): не чаще одного рестарта в 5 минут.
# Причина — каждый рестарт убивает MCP-сессии клиентов, и следующий вызов падает
# с -32001 (замер 2026-07-20: 76 рестартов за сутки активной разработки, оба
# разобранных инцидента совпали с ними). Правки *.py идут пачками, применять
# каждую отдельным рестартом незачем.
#
# ⚠️ СМЕНА VERSION ОБХОДИТ COOLDOWN. Это релиз: его надо применить и проверить
# сразу, а не через пять минут. Задерживать релиз ради тишины в сессиях —
# ровно тот случай, когда лечение хуже болезни.

MC_DIR="${MC_DIR:-/path/to/memory-compiler/memory_compiler}"
VERSION_FILE="${VERSION_FILE:-$MC_DIR/../VERSION}"
STATE="${MC_STATE:-/var/log/mc-watcher.state}"       # последний ЗАДЕПЛОЕННЫЙ хэш
PENDING="${MC_PENDING:-/var/log/mc-watcher.pending}" # кандидат "hash count" для debounce
LOG="${MC_LOG:-/var/log/mc-watcher.log}"
LAST_TS="${MC_LAST_TS:-/var/log/mc-watcher.last}"    # unixtime последнего рестарта
VER_STATE="${MC_VER_STATE:-/var/log/mc-watcher.version}"  # задеплоенный VERSION
COOLDOWN="${MC_COOLDOWN:-300}"                       # сек между рестартами
DOCKER="${DOCKER:-/usr/local/bin/docker}"
CONTAINER="${CONTAINER:-memory-compiler-mcp}"

EMPTY_SHA1="da39a3ee5e6b4b0d3255bfef95601890afd80709"  # sha1 пустого ввода

# Хэш содержимого всех *.py (с путями) + VERSION.
current=$( { find "$MC_DIR" -name "*.py" -type f -exec sha1sum {} \; ;
             [ -f "$VERSION_FILE" ] && sha1sum "$VERSION_FILE" ; } 2>/dev/null \
           | sort | sha1sum | awk '{print $1}')

# ⚠️ КАТАЛОГА НЕТ — ЖАЛУЕМСЯ, А НЕ МОЛЧИМ. Раньше пустой хэш выглядел так же,
# как «изменений нет», и watcher молча простаивал. Живой случай 2026-08-26:
# рабочую копию /usr/local/bin/mc-watcher.sh перезаписали файлом ИЗ РЕПОЗИТОРИЯ,
# где MC_DIR — шаблонный /path/to/...; деплой встал на полчаса, и в логе не было
# ни строчки. Пишем в лог не чаще раза в час, чтобы не залить его при cron */1.
if [ ! -d "$MC_DIR" ]; then
  stamp="${MC_MISSING_STAMP:-/var/log/mc-watcher.missing}"
  now=$(date +%s); prev=$(cat "$stamp" 2>/dev/null || echo 0)
  if [ $((now - prev)) -ge 3600 ]; then
    echo "[$(date -Iseconds)] ОШИБКА: MC_DIR не существует ($MC_DIR) — watcher ничего не деплоит" >> "$LOG"
    echo "$now" > "$stamp"
  fi
  exit 1
fi

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

# Cooldown: пропускаем рестарт, если недавно уже перезапускались. Кандидат
# остаётся в PENDING — изменение не теряется, применится следующим прогоном.
version_now=$(cat "$VERSION_FILE" 2>/dev/null || echo "")
version_was=$(cat "$VER_STATE" 2>/dev/null || echo "")
if [ "$version_now" = "$version_was" ]; then
  last_ts=$(cat "$LAST_TS" 2>/dev/null || echo 0)
  now_ts=$(date +%s)
  age=$((now_ts - last_ts))
  if [ "$age" -lt "$COOLDOWN" ]; then
    echo "[$(date -Iseconds)] change held (hash ${current:0:12}): рестарт был $age с назад, cooldown $COOLDOWN с" >> "$LOG"
    exit 0
  fi
fi

echo "[$(date -Iseconds)] stable change -> restart $CONTAINER (hash ${current:0:12})" >> "$LOG"
$DOCKER restart "$CONTAINER" >> "$LOG" 2>&1
echo "$current" > "$STATE"
date +%s > "$LAST_TS"
cat "$VERSION_FILE" > "$VER_STATE" 2>/dev/null
: > "$PENDING"
