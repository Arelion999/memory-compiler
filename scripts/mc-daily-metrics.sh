#!/bin/bash
# Суточный замер качества памяти: считает сервер, а не человек.
#
# Install on NAS:
#   1) copy to /usr/local/bin/mc-daily-metrics.sh (chmod +x)
#   2) задать ENV_FILE ниже — путь к .env базы (как MC_DIR у mc-watcher.sh)
#   3) add to /etc/crontab (поля через ТАБЫ — формат Synology!):
#        30 4 * * *	root	/usr/local/bin/mc-daily-metrics.sh
#   4) перезапустить crond: /usr/syno/bin/synosystemctl restart crond
#
# ⚠️ ЗАМЕР ДЁРГАЕТСЯ У РАБОТАЮЩЕГО СЕРВЕРА, а не запускается вторым процессом
# python в контейнере. Первая редакция делала `docker exec … python -m …`, и это
# было опасно вдвойне: параллельный процесс переписывает pickle эмбеддингов мимо
# внутрипроцессного лока (затирая то, что сервер посчитал за это время), а сам
# процесс падал на выходе с кодом 134 при успешно записанном замере — провал и
# успех становились неотличимы в логе. Поймано первым же прогоном на NAS.
#
# ⚠️ ОКНО СМЕЩЕНО ОТ ПОЛУНОЧИ: бэкап базы идёт в 04:10, git-снимок 04:10-04:20.
# Замер в 04:30 видит сутки целиком и не делит их с обслуживанием.
#
# Пропуск обязан быть ВИДЕН: недоступный сервер, отсутствие ключа и ответ без
# result дают строку в лог и ненулевой код, а не тишину — иначе в ряду замеров
# появится молчаливая дыра.

URL="${MC_METRICS_URL:-http://127.0.0.1:8765/api/metrics/daily}"
LOG="${MC_METRICS_LOG:-/var/log/mc-daily-metrics.log}"
HOURS="${MC_METRICS_HOURS:-24}"
CURL="${CURL:-curl}"
ENV_FILE="${MC_ENV_FILE:-/path/to/memory-compiler/.env}"

ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }

# REST закрыт ключом (Authorization: Bearer). Ключ НЕ зашит в скрипт и не идёт в
# query: в URL он утёк бы в access-логи прокси и uvicorn — ровно поэтому ?key=
# из REST-путей и убрали. Берём из .env базы, в лог не пишем никогда.
KEY="${MC_API_KEY:-}"
if [ -z "$KEY" ] && [ -f "$ENV_FILE" ]; then
  KEY=$(sed -n 's/^MC_API_KEY=//p' "$ENV_FILE" | head -1 | tr -d '"'"'"'\r')
fi
if [ -z "$KEY" ]; then
  echo "[$(ts)] FAIL: нет MC_API_KEY (смотри $ENV_FILE)" >> "$LOG"
  exit 2
fi

out=$("$CURL" -sS --max-time 120 -X POST "$URL" \
      -H "Authorization: Bearer $KEY" \
      -H 'Content-Type: application/json' \
      -d "{\"hours\": $HOURS}" 2>&1)
code=$?

if [ $code -ne 0 ]; then
  echo "[$(ts)] FAIL (curl $code): $out" >> "$LOG"
  exit $code
fi

case "$out" in
  *'"result"'*)
    { echo "[$(ts)] OK"; echo "$out"; } >> "$LOG"
    ;;
  *)
    echo "[$(ts)] FAIL (ответ без result): $out" >> "$LOG"
    exit 1
    ;;
esac

exit 0
