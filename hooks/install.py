#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Установщик хука mc_guard (сторож memory-compiler) для Claude Code и Kimi Code.

Один запуск делает всё:
  1. копирует hooks/mc_guard.py репозитория в hooks-каталоги клиентов
     (байт-в-байт; state/pending/logs клиентов не трогаются);
  2. перегенерирует секции хуков в конфигах клиентов (Claude settings.json —
     ключ "hooks" целиком; Kimi config.toml — только маркированный блок,
     чужие секции не трогаются); перед записью — бэкап с отметкой времени;
  3. пишет mc_guard.env рядом с установленной копией (боевые значения из
     gitignored .env репозитория; существующий файл не затирается без
     --force-env).

Использование:
    python hooks/install.py [--client both|claude|kimi] [--dry-run] [--force-env]

Модуль импортируем без побочных эффектов: вся работа — в функциях, пути
конфигов передаются параметрами (тесты работают на фикстурах).
"""

import difflib
import json
import os
import shutil
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("USERPROFILE") or Path.home())
REPO_ROOT = Path(__file__).resolve().parent.parent
GUARD_SRC = REPO_ROOT / "hooks" / "mc_guard.py"

CLIENTS = {
    "claude": {"dir": HOME / ".claude" / "hooks", "config": HOME / ".claude" / "settings.json"},
    "kimi": {"dir": HOME / ".kimi-code" / "hooks", "config": HOME / ".kimi-code" / "config.toml"},
}

MARK_BEGIN = "# >>> mc_guard hooks (generated, не править руками)"
MARK_END = "# <<< mc_guard hooks"

# Единый источник хук-записей: (событие, matcher, подкоманда mc_guard, timeout).
# matcher: строка; None — записи без matcher; dict — свой matcher на клиента.
HOOKS = [
    ("PreToolUse", r"Bash|.*PowerShell.*", "nul_guard", 5),
    ("PreToolUse", r"Bash|.*PowerShell.*", "gate", 10),
    ("PreToolUse", r"mcp__(mikrotik|ssh|synology|1c|ftp-zarina)__.*", "gate", 10),
    ("PreToolUse", r"mcp__memory-compiler__(save_.*|finish_task|edit_article|consolidate|compile)",
     "intent", 5),
    ("PreToolUse", r"mcp__memory-compiler__.*", "session_arg", 5),
    ("SessionStart", {"claude": "startup|clear|compact", "kimi": "startup|resume"},
     "session_start", 25),
    ("UserPromptSubmit", None, "freshness", 10),
    ("Stop", None, "stop", 10),
    ("PostCompact", {"claude": None, "kimi": "manual|auto"}, "compact", 5),
    ("PostToolUse", r"mcp__memory-compiler__.*", "mark", 5),
    ("PostToolUse", r"Bash|.*PowerShell.*|mcp__(mikrotik|ssh|synology|1c|ftp-[a-z0-9-]+)__.*",
     "probe", 5),
    ("PostToolUse", r"mcp__memory-compiler__(save_lesson|finish_task|edit_article)", "nudge", 5),
    ("PostToolUse", "Read", "reflex", 5),
    ("PostToolUseFailure",
     r"mcp__memory-compiler__(save_.*|finish_task|edit_article|consolidate|compile)", "fail", 10),
    ("PostToolUseFailure",
     r"Bash|.*PowerShell.*|mcp__(mikrotik|ssh|synology|1c|ftp-[a-z0-9-]+)__.*", "reflex", 5),
]


# ── Kimi Work (desktop, daimon): хуки через манифест плагина ─────────────────
# Плагин Kimi Work объявляет hooks [{event, command, matcher?, timeout?}]
# (HookDefSchema, 16 событий); рантайм daimon исполняет их на событиях сессии
# сам — это нативная замена [[hooks]] из config.toml, которого daimon не читает.
# Matcher'ы обобщены под имена инструментов Kimi Work mcp__plugin-<plugin>_<server>__*:
# mc_guard сам сводит их к mcp__<server>__<tool>, поэтому НАЗНАЧЕНИЕ записей
# (что гейтить) совпадает с CLI-клиентами, меняется только орфография матчера.
# Подмножество HOOKS намеренно: intent/probe/nudge/reflex/compact — после первой
# живой проверки payload (YAGNI до подтверждённой необходимости).
KIMIWORK_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest",
                   "PermissionResult", "UserPromptSubmit", "Stop", "StopFailure", "Interrupt",
                   "SessionStart", "SessionEnd", "SubagentStart", "SubagentStop",
                   "PreCompact", "PostCompact", "Notification")
KIMIWORK_HOOKS = [
    ("SessionStart", None, "session_start", 25),
    ("UserPromptSubmit", None, "freshness", 10),
    ("PreToolUse", r"Bash|.*PowerShell.*", "nul_guard", 5),
    ("PreToolUse", r"mcp__plugin-(mikrotik|ssh|synology|1c|ftp-zarina)_.*__.*", "gate", 10),
    ("PreToolUse", r"mcp__plugin-memory-compiler_memory-compiler__.*", "session_arg", 5),
    ("Stop", None, "stop", 10),
    ("PostToolUse", r"mcp__plugin-memory-compiler_memory-compiler__.*", "mark", 5),
]


def resolve_hook_python():
    """Абсолютный путь интерпретатора для hook-команды плагина.

    PATH процесса daimon не гарантирует python; к тому же команда манифеста
    должна быть самодостаточной (runtime исполняет её без нашего окружения).
    """
    exe = shutil.which("python") or sys.executable
    try:
        return str(Path(exe).resolve())
    except Exception:
        return exe


def build_kimiwork_hooks(python_exe, script_rel="./hooks/mc_guard.py"):
    """Массив hooks для kimi.plugin.json из KIMIWORK_HOOKS."""
    out = []
    for event, matcher, sub, timeout in KIMIWORK_HOOKS:
        entry = {
            "event": event,
            "command": '"%s" %s %s --client=kimi' % (python_exe, script_rel, sub),
            "timeout": timeout,
        }
        if matcher:
            entry["matcher"] = matcher
        out.append(entry)
    return out


def update_plugin_manifest(manifest_path, hooks):
    """Переписать ключ hooks в kimi.plugin_json-подобном манифесте + бамп версии.

    Возвращает report-dict; бэкап .bak-<ts> рядом. Повторный вызов идемпотентен
    по содержимому hooks (версия бампится каждый раз — это наша cachebuster-конвенция).
    """
    path = Path(manifest_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["hooks"] = hooks
    ver = str(data.get("version") or "0.1.0")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    data["version"] = ver.split("+local.")[0] + "+local." + stamp
    backup = path.with_name("%s.bak-%s" % (path.name, stamp))
    shutil.copyfile(path, backup)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"backup": backup.name, "version": data["version"]}


def resolve_kimiwork_plugin_dir(share_dir=None):
    """Каталог исходников плагина в daimon-share.

    share_dir: параметр/тесты → env KIMI_SHARE_DIR → %APPDATA%/kimi-desktop/daimon-share
    (конвенция register_personal.sh). Публичных литералов с именем пользователя
    нет: APPDATA резолвится в рантайме.
    """
    share = Path(share_dir) if share_dir else None
    if share is None:
        env = os.environ.get("KIMI_SHARE_DIR")
        share = Path(env) if env else Path(os.environ.get("APPDATA", "")) / "kimi-desktop" / "daimon-share"
    return share / "plugin-sources" / "personal" / "memory-compiler"


def _matcher(matcher, client):
    if isinstance(matcher, dict):
        return matcher[client]
    return matcher


def _command(script_path, sub, client):
    """Команда вызова хука: путь с прямыми слэшами (работает и в bash, и в cmd)."""
    return 'python "%s" %s --client=%s' % (
        str(script_path).replace("\\", "/"), sub, client)


# ------------------------------------------------------------------ генерация
def build_claude_settings(old_text, script_path):
    """settings.json Claude Code: ключ hooks заменяется целиком, остальное как есть."""
    data = json.loads(old_text)
    hooks = {}
    for event, matcher, sub, timeout in HOOKS:
        entry = {"type": "command", "command": _command(script_path, sub, "claude"),
                 "shell": "bash", "timeout": timeout}
        record = {}
        m = _matcher(matcher, "claude")
        if m:
            record["matcher"] = m
        record["hooks"] = [entry]
        hooks.setdefault(event, []).append(record)
    data["hooks"] = hooks
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def build_kimi_block(script_path):
    """Маркированный блок хуков для config.toml Kimi Code."""
    lines = [MARK_BEGIN]
    for event, matcher, sub, timeout in HOOKS:
        lines.append("[[hooks]]")
        lines.append('event = "%s"' % event)
        m = _matcher(matcher, "kimi")
        if m:
            lines.append("matcher = '%s'" % m)
        lines.append("command = '%s'" % _command(script_path, sub, "kimi"))
        lines.append("timeout = %d" % timeout)
        lines.append("")
    lines.append(MARK_END)
    return lines


def build_kimi_config(old_text, script_path):
    """config.toml Kimi Code: заменяется только блок mc_guard-хуков.

    Приоритет: маркированный блок → существующие [[hooks]] с mc_guard.py
    (со своим комментарием-заголовком) → вставка перед секцией dayz →
    в конец файла. Прочий текст сохраняется байт-в-байт."""
    block = build_kimi_block(script_path)
    lines = old_text.splitlines()
    tail_nl = "\n" if old_text.endswith("\n") else ""

    def _line_of(marker):
        for i, line in enumerate(lines):
            if line.strip() == marker:
                return i
        return None

    begin, end = _line_of(MARK_BEGIN), _line_of(MARK_END)
    if begin is not None and end is not None and begin <= end:
        new_lines = lines[:begin] + block + lines[end + 1:]
        return "\n".join(new_lines) + tail_nl

    idxs = [i for i, line in enumerate(lines)
            if "mc_guard.py" in line and line.lstrip().startswith("command")]
    if idxs:
        # Начало: вверх от первой записи до её [[hooks]]…
        start = idxs[0]
        while start > 0 and lines[start].strip() != "[[hooks]]":
            start -= 1
        # …и дальше по сплошному блоку комментариев/пустых строк (заголовок
        # секции), стоп на первой не-комментарийной строке.
        while start > 0 and (not lines[start - 1].strip()
                             or lines[start - 1].lstrip().startswith("#")):
            start -= 1
        # Ведущие пустые строки остаются прежней секции.
        while start < len(lines) and not lines[start].strip():
            start += 1
        # Конец: последняя непустая строка последней записи с mc_guard.py.
        end = idxs[-1]
        while end + 1 < len(lines) and lines[end + 1].strip():
            end += 1
        return "\n".join(lines[:start] + block + lines[end + 1:]) + tail_nl

    # Секции нет: вставить перед заголовком dayz-хуков, иначе в конец файла.
    insert_at = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#") and "dayz" in line.lower():
            while i > 0 and (not lines[i - 1].strip()
                             or lines[i - 1].lstrip().startswith("#")):
                i -= 1
            insert_at = i
            break
    if insert_at is None:
        insert_at = len(lines)
        block = [""] + block
    new_lines = lines[:insert_at] + block + [""] + lines[insert_at:]
    return "\n".join(new_lines) + tail_nl


# ------------------------------------------------------------------ env-файл
def _read_env_file(path):
    """KEY=VALUE из .env (строки, # — комментарий). Битый/отсутствующий — {}."""
    data = {}
    try:
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip().strip('"').strip("'")
    except Exception:
        pass
    return data


def default_env_values(repo_root=None):
    """Боевые значения для mc_guard.env на этой машине: ключи из .env репо,
    пути — от расположения репозитория."""
    root = Path(repo_root) if repo_root else REPO_ROOT
    env_file = root / ".env"
    repo_env = _read_env_file(env_file)
    missing = [k for k in ("MC_API_URL", "MC_API_KEY") if not repo_env.get(k)]
    values = {
        "MC_API_URL": repo_env.get("MC_API_URL") or "http://127.0.0.1:8765",
        "MC_KNOWLEDGE_DIR": str(root / "knowledge").replace("\\", "/"),
        "MC_ENV_FILE": str(env_file).replace("\\", "/"),
    }
    if repo_env.get("MC_API_KEY"):
        values["MC_API_KEY"] = repo_env["MC_API_KEY"]
    return values, missing


def _inherit_api_url(old_script):
    """Адрес сервера из дефолта РАНЕЕ установленной копии mc_guard.py.

    Миграционный путь первого запуска: прежние копии несли боевой адрес в
    коде, репозиторный .env его не имеет — наследуем, чтобы REST (очередь,
    рефлексы) не деградировал до loopback-заглушки."""
    import re
    try:
        text = Path(old_script).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    m = re.search(r'MC_API_URL[^\n]*?"(https?://[^"]+)"', text)
    return m.group(1) if m else None


def render_env_file(values, mask=False):
    lines = ["# Локальные значения mc_guard (машинно-специфичные, в репо не коммитить).",
             "# Пишет hooks/install.py; перезапись — только с --force-env."]
    for key in ("MC_API_URL", "MC_KNOWLEDGE_DIR", "MC_ENV_FILE", "MC_API_KEY"):
        if key not in values:
            continue
        value = "***" if (mask and key == "MC_API_KEY") else values[key]
        lines.append("%s=%s" % (key, value))
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ запись
def _backup(path):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = path.with_name("%s.bak-%s" % (path.name, stamp))
    shutil.copyfile(path, dst)
    return dst


def _write_if_changed(path, new_text, dry_run, report, label, make_backup=True,
                      show_diff=True):
    """Записать текст, если отличается. Возвращает True, если запись была/нужна."""
    old = None
    try:
        old = Path(path).read_text(encoding="utf-8")
    except Exception:
        pass
    if old == new_text:
        report.append("%s: без изменений" % label)
        return False
    if dry_run:
        report.append("%s: БУДЕТ изменён%s" % (label, "" if old is not None else " (новый файл)"))
        # diff не печатаем, когда старое содержимое может нести секрет (mc_guard.env)
        if old is not None and show_diff:
            diff = list(difflib.unified_diff(old.splitlines(), new_text.splitlines(),
                                             fromfile=str(path), tofile=label + " (новый)",
                                             lineterm="", n=2))
            report.extend(diff)
        return True
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if old is not None and make_backup:
        report.append("%s: бэкап %s" % (label, _backup(Path(path)).name))
    Path(path).write_text(new_text, encoding="utf-8")
    report.append("%s: записан" % label)
    return True


def install_client(client, guard_src=None, hooks_dir=None, config_path=None,
                   env_values=None, dry_run=False, force_env=False, report=None):
    """Установка одного клиента. Пути — параметрами (тесты подставляют фикстуры)."""
    report = report if report is not None else []

    if client == "kimiwork":
        # Плагин Kimi Work (desktop, daimon): копия скрипта + env в <plugin>/hooks,
        # а хуки — в манифест плагина (рантайм daimon исполняет их сам).
        hdir = Path(hooks_dir) if hooks_dir else resolve_kimiwork_plugin_dir() / "hooks"
        dst = hdir / "mc_guard.py"
        if env_values is None:
            env_values, missing = default_env_values()
            if "MC_API_URL" in missing:
                inherited = _inherit_api_url(dst)
                if inherited:
                    env_values["MC_API_URL"] = inherited
                    missing.remove("MC_API_URL")
                    report.append("%s: MC_API_URL унаследован из установленной копии" % client)
            for key in missing:
                report.append("%s: ⚠ в .env репозитория нет %s — проверь %s"
                              % (client, key, hdir / "mc_guard.env"))
        new_bytes = src.read_bytes() if (src := Path(guard_src) if guard_src else GUARD_SRC) else None
        old_bytes = dst.read_bytes() if dst.exists() else None
        if old_bytes == new_bytes:
            report.append("%s: mc_guard.py совпадает" % client)
        elif dry_run:
            report.append("%s: mc_guard.py БУДЕТ обновлён (%s)" % (client, dst))
        else:
            hdir.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(new_bytes)
            report.append("%s: mc_guard.py скопирован (%s)" % (client, dst))
        env_path = hdir / "mc_guard.env"
        if env_path.exists() and not force_env:
            report.append("%s: mc_guard.env уже есть, пропущено (--force-env для перезаписи)"
                          % client)
        else:
            changed = _write_if_changed(env_path, render_env_file(env_values, mask=dry_run),
                                        dry_run, report, "%s: mc_guard.env" % client,
                                        make_backup=False, show_diff=False)
            if changed and dry_run:
                report.extend("    " + line
                              for line in render_env_file(env_values, mask=True).splitlines())
        manifest = hdir.parent / "kimi.plugin.json"
        if not manifest.exists():
            report.append("%s: ⚠ манифест не найден: %s — hooks не обновлены" % (client, manifest))
            return report
        hooks = build_kimiwork_hooks(resolve_hook_python())
        if dry_run:
            report.append("%s: dry-run, манифест не тронут (hooks=%d)" % (client, len(hooks)))
        else:
            rep = update_plugin_manifest(manifest, hooks)
            report.append("%s: манифест обновлён (hooks=%d, backup=%s, version=%s)"
                          % (client, len(hooks), rep["backup"], rep["version"]))
        return report

    src = Path(guard_src) if guard_src else GUARD_SRC
    hdir = Path(hooks_dir) if hooks_dir else CLIENTS[client]["dir"]
    cfg = Path(config_path) if config_path else CLIENTS[client]["config"]
    dst = hdir / "mc_guard.py"

    # Значения mc_guard.env решаем ДО копирования скрипта: адрес сервера можно
    # унаследовать из ранее установленной копии, а копирование её перезапишет.
    if env_values is None:
        env_values, missing = default_env_values()
        if "MC_API_URL" in missing:
            inherited = _inherit_api_url(dst)
            if inherited:
                env_values["MC_API_URL"] = inherited
                missing.remove("MC_API_URL")
                report.append("%s: MC_API_URL унаследован из установленной копии" % client)
        for key in missing:
            report.append("%s: ⚠ в .env репозитория нет %s — проверь %s"
                          % (client, key, hdir / "mc_guard.env"))

    # 1. копия скрипта (байт-в-байт)
    new_bytes = src.read_bytes()
    old_bytes = dst.read_bytes() if dst.exists() else None
    if old_bytes == new_bytes:
        report.append("%s: mc_guard.py совпадает" % client)
    elif dry_run:
        report.append("%s: mc_guard.py БУДЕТ обновлён (%s)" % (client, dst))
    else:
        hdir.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(new_bytes)
        report.append("%s: mc_guard.py скопирован (%s)" % (client, dst))

    # 2. конфиг
    if client == "claude":
        old_cfg = cfg.read_text(encoding="utf-8") if cfg.exists() else "{}"
        new_cfg = build_claude_settings(old_cfg, dst)
    else:
        old_cfg = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
        new_cfg = build_kimi_config(old_cfg, dst)
    _write_if_changed(cfg, new_cfg, dry_run, report, "%s: %s" % (client, cfg.name))

    # 3. mc_guard.env рядом с копией
    env_path = hdir / "mc_guard.env"
    if env_path.exists() and not force_env:
        report.append("%s: mc_guard.env уже есть, пропущено (--force-env для перезаписи)"
                      % client)
    else:
        changed = _write_if_changed(env_path, render_env_file(env_values, mask=dry_run),
                                    dry_run, report, "%s: mc_guard.env" % client,
                                    make_backup=False, show_diff=False)
        if changed and dry_run:
            # Показать, что было бы записано, со скрытым ключом.
            report.extend("    " + line
                          for line in render_env_file(env_values, mask=True).splitlines())
    return report


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    client = "both"
    dry_run = "--dry-run" in args
    force_env = "--force-env" in args
    for arg in args:
        if arg.startswith("--client="):
            client = arg.split("=", 1)[1].strip().lower()
    if client not in ("both", "claude", "kimi", "kimiwork"):
        print("Неизвестный --client=%r (ожидается both|claude|kimi|kimiwork)" % client)
        return 2
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    report = []
    if dry_run:
        report.append("РЕЖИМ dry-run: ничего не пишется, бэкапы не создаются.")
    for name in (("claude", "kimi") if client == "both" else (client,)):
        install_client(name, dry_run=dry_run, force_env=force_env, report=report)
    print("\n".join(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
