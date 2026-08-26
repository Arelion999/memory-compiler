#!/bin/bash
# Daily backup of memory-compiler knowledge base
# Install on NAS:
#   1) copy to /usr/local/bin/mc-backup.sh (chmod +x)
#   2) edit KB_DIR/BACKUP_DIR below to your paths
#   3) add to /etc/crontab: 0 4 * * * root /usr/local/bin/mc-backup.sh

# Paths (override via env if needed)
KB_DIR="${KB_DIR:-/path/to/memory-compiler/knowledge}"
BACKUP_DIR="${BACKUP_DIR:-/path/to/memory-compiler/backups}"
LOG="${MC_BACKUP_LOG:-/var/log/mc-backup.log}"   # переопределяется в тестах
KEEP_DAYS=7

mkdir -p "$BACKUP_DIR"

date=$(date +%Y-%m-%d)
archive="$BACKUP_DIR/knowledge-$date.tar.gz"

# Индексы в архив не кладём — они пересобираются из статей.
#
# ⚠️ ШАБЛОНЫ СО ЗВЁЗДОЧКОЙ, а не точные имена. Прежние `.whoosh_index` и
# `.embeddings.pkl` не покрывали соседние реальные имена: `.whoosh_index.corrupt-
# 20260819/` (44 МБ битого индекса) и `.embeddings.pkl.bak-v1180` уезжали в
# КАЖДЫЙ ежедневный архив. Та же дыра была в .gitignore базы и стоила 590 МБ
# истории — вычищено 2026-08-26, архив упал с 493 МБ.
tar -czf "$archive" \
    --exclude=".whoosh_index*" \
    --exclude=".embeddings.pkl*" \
    --exclude=".embeddings_*" \
    --exclude=".git.old*" \
    -C "$(dirname "$KB_DIR")" \
    "$(basename "$KB_DIR")" 2>>"$LOG"

if [ $? -eq 0 ]; then
    size=$(du -h "$archive" | cut -f1)
    echo "[$(date -Iseconds)] Backup created: $archive ($size)" >> "$LOG"
else
    echo "[$(date -Iseconds)] Backup FAILED" >> "$LOG"
    exit 1
fi

# Rotate: keep last N days
find "$BACKUP_DIR" -name "knowledge-*.tar.gz" -mtime +$KEEP_DAYS -delete 2>>"$LOG"
