#!/bin/bash
# Auto-restart memory-compiler container when source code changes
# Install on NAS:
#   1) copy to /usr/local/bin/mc-watcher.sh (chmod +x)
#   2) edit MC_DIR below to point to your memory_compiler/ directory
#   3) add to /etc/crontab: */1 * * * * root /usr/local/bin/mc-watcher.sh

# Path to memory_compiler/ source dir (override via env if needed)
MC_DIR="${MC_DIR:-/path/to/memory-compiler/memory_compiler}"
STATE="/var/log/mc-watcher.state"
LOG="/var/log/mc-watcher.log"
DOCKER="/usr/local/bin/docker"
CONTAINER="memory-compiler-mcp"

current=$(find "$MC_DIR" -name "*.py" -printf "%T@\n" 2>/dev/null | sort -n | tail -1)
[ -z "$current" ] && exit 0

last=$(cat "$STATE" 2>/dev/null || echo "0")

if [ "$current" != "$last" ]; then
  echo "[$(date -Iseconds)] Change detected, restarting $CONTAINER" >> "$LOG"
  $DOCKER restart "$CONTAINER" >> "$LOG" 2>&1
  echo "$current" > "$STATE"
fi
