#!/bin/bash
# Daily backup of memory-compiler knowledge base
# Install on NAS:
#   1) copy to /usr/local/bin/mc-backup.sh (chmod +x)
#   2) add to /etc/crontab: 0 4 * * * root /usr/local/bin/mc-backup.sh

KB_DIR="/path/to/memory-compiler/knowledge"
BACKUP_DIR="/path/to/memory-compiler/backups"
LOG="/var/log/mc-backup.log"
KEEP_DAYS=7

mkdir -p "$BACKUP_DIR"

date=$(date +%Y-%m-%d)
archive="$BACKUP_DIR/knowledge-$date.tar.gz"

# Create archive (exclude .whoosh_index and .embeddings.pkl — they're rebuilt)
tar -czf "$archive" \
    --exclude=".whoosh_index" \
    --exclude=".embeddings.pkl" \
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
