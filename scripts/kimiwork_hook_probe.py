"""Пробник хуков для эксперимента: исполняет ли ядро kimi-code внутри daimon (Kimi Work)
зарегистрированные в runtime/kimi-code/config.toml [[hooks]].

Протокол: каждый вызов дописывает строку в %USERPROFILE%/.kimi-code/hooks/kimiwork_probe.log
с событием, argv и первыми 300 символами stdin. Пустой лог после перезапуска Kimi Work
и выполненных действий = ядро daimon хуков не исполняет (гипотеза мертва).

Создан 01.10.2026 для статьи об альтернативах хукам mc_guard в Kimi Work.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG = Path.home() / ".kimi-code" / "hooks" / "kimiwork_probe.log"


def main() -> int:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {"_raw": raw[:300]}
    line = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "argv": sys.argv[1:],
        "payload_head": json.dumps(payload, ensure_ascii=False)[:300],
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    # exit 0 — не блокировать вызов ни в коем случае
    return 0


if __name__ == "__main__":
    sys.exit(main())
