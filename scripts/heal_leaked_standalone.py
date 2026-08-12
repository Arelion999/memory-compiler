#!/usr/bin/env python3
"""Standalone-версия maintenance.heal_leaked_markup для запуска НА NAS системным
python3 (3.8): у UserAI нет sudo на docker exec, а импорт memory_compiler требует
python 3.10+ (str | None в storage.py). Логика — КОПИЯ heal_leaked_call_text;
эквивалентность держит tests/test_maintenance_leaked.py::test_standalone_equivalent
(оба варианта гоняются на общем наборе кейсов). Меняешь грамматику — меняй ОБА файла,
тест упадёт, если забыл.

Запуск (dry-run по умолчанию, пишет только с --apply):
    python3 scripts/heal_leaked_standalone.py /path/to/knowledge [--apply]

daily/archive/ не трогается (glob нерекурсивный + каталог archive не в обходе).
Git-коммита здесь нет: базу коммитит сервер (git add -A на следующей MCP-записи
или git_capture).
"""
import json
import re
import sys
from pathlib import Path

_LEAK_ANCHOR = "</content>"
_LEAK_FIELD_OPEN_RE = re.compile(r"^<(session_summary|open_questions|tags)>")
_LEAK_PARAM_OPEN_RE = re.compile(r'^<parameter name="([\w-]+)">')
_LEAK_LABELS = {"session_summary": "Итог сессии", "open_questions": "Открытые вопросы",
                "alternatives": "Альтернативы", "decisions": "Решения",
                "reasoning": "Обоснование", "content": "Содержание"}


def _try_json_list(raw):
    try:
        val = json.loads(raw)
    except Exception:
        return None
    if isinstance(val, list) and all(isinstance(x, str) for x in val):
        return val
    return None


def _leak_block_at(lines, i):
    m_p = _LEAK_PARAM_OPEN_RE.match(lines[i])
    m_f = None if m_p else _LEAK_FIELD_OPEN_RE.match(lines[i])
    if not (m_p or m_f):
        return None
    name = (m_p or m_f).group(1)
    close = "</parameter>" if m_p else "</{}>".format(name)
    open_len = len(m_p.group(0)) if m_p else len("<{}>".format(name))
    buf = []
    own_close = "</{}>".format(name)
    for j in range(i, min(i + 30, len(lines))):
        cur = lines[j][open_len:] if j == i else lines[j]
        if cur.rstrip().endswith(close):
            buf.append(cur.rstrip()[:-len(close)])
            return name, "\n".join(buf).strip(), j + 1
        # Смешанная форма с прода: открыт '<parameter name="q">', закрыт '</q>'.
        if m_p and cur.rstrip().endswith(own_close):
            buf.append(cur.rstrip()[:-len(own_close)])
            return name, "\n".join(buf).strip(), j + 1
        # Граница незакрытого блока: следующий блок или пустая строка. Без неё
        # незакрытый хвост дотянулся бы до чужого закрытия ниже и съел текст между.
        if j > i and (not lines[j].strip() or _LEAK_PARAM_OPEN_RE.match(lines[j])):
            break
        buf.append(cur)
    # Незакрытый параметрический блок (v1.54.3): клиент не дописал закрывающий тег
    # вовсе — значение берём как остаток ОДНОЙ строки. Голая форма '<session_summary>'
    # без 'parameter name' так не лечится: это другой класс (см. suspicious).
    if m_p:
        return name, lines[i][open_len:].strip(), i + 1
    return None


def heal_leaked_call_text(text):
    lines = text.splitlines()
    stats = {"anchors": 0, "invokes": 0, "fields": 0, "suspicious": 0, "open_tails": 0}
    header_has_tags = False
    for l in lines:
        if l.startswith("## "):
            break
        if l.startswith("**Теги:**"):
            header_has_tags = True
            break
    out = []
    collected_tags = []
    in_fence = False
    i, n = 0, len(lines)

    def _consume(pos, moved):
        """Собрать подряд идущие блоки утёкших полей начиная со строки pos."""
        while pos < n:
            j = pos
            while j < n and not lines[j].strip():
                j += 1
            if j >= n:
                break
            if lines[j].strip() == "</invoke>":
                stats["invokes"] += 1
                pos = j + 1
                break
            blk = _leak_block_at(lines, j)
            if not blk:
                break
            name, val, pos = blk
            stats["fields"] += 1
            if name == "tags":
                parsed = _try_json_list(val)
                if parsed and header_has_tags:
                    collected_tags.extend(parsed)
                elif parsed:
                    moved.append("Теги: " + ", ".join(parsed))
                # битое значение тегов не восстановимо — разметка уходит
            elif val:
                moved.append("{}: {}".format(_LEAK_LABELS.get(name, name), val))
        return pos

    while i < n:
        line = lines[i]
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if in_fence:
            out.append(line)
            i += 1
            continue
        stripped = line.rstrip()
        if stripped == "</invoke>":
            stats["invokes"] += 1
            i += 1
            continue
        if stripped.endswith(_LEAK_ANCHOR):
            stats["anchors"] += 1
            head = stripped[:-len(_LEAK_ANCHOR)].rstrip()
            if head:
                out.append(head)
            i += 1
            moved = []
            i = _consume(i, moved)
            for para in moved:
                if out and out[-1].strip():
                    out.append("")
                out.append(para)
            continue
        # Форма БЕЗ якоря '</content>' (v1.54.3): параметр закрыт корректно, а следом
        # с новой строки въехали блоки '<parameter name="q">…', последний обычно вообще
        # не закрыт. 216 живых статей вне daily/ на 2026-08-12. ГОЛЫЕ
        # '<session_summary>…' без 'parameter name' сюда НЕ попадают и остаются
        # suspicious: это другой класс, в v1.51.0 такие разбирались руками.
        if _LEAK_PARAM_OPEN_RE.match(line) and _leak_block_at(lines, i):
            stats["open_tails"] += 1
            moved = []
            i = _consume(i, moved)
            for para in moved:
                if out and out[-1].strip():
                    out.append("")
                out.append(para)
            continue
        if _leak_block_at(lines, i):
            stats["suspicious"] += 1
        out.append(line)
        i += 1
    if collected_tags:
        for k, l in enumerate(out):
            if l.startswith("## "):
                break
            if l.startswith("**Теги:**"):
                existing = [t.strip() for t in l.split(":**", 1)[1].split(",") if t.strip()]
                for t in collected_tags:      # поштучно: дедуп и против шапки, и внутри собранных
                    if t not in existing:
                        existing.append(t)
                out[k] = "**Теги:** " + ", ".join(existing)
                break
    fixed = "\n".join(out) + ("\n" if text.endswith("\n") else "")
    return fixed, stats


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("usage: heal_leaked_standalone.py /path/to/knowledge [--apply]")
        return 2
    root = Path(args[0])
    apply = "--apply" in sys.argv
    hidden = {".whoosh_index", ".git", "logs", "backups"}
    dirs = sorted(d for d in root.iterdir()
                  if d.is_dir() and d.name not in hidden and not d.name.startswith("."))
    totals = {"anchors": 0, "invokes": 0, "fields": 0, "suspicious": 0, "open_tails": 0}
    touched = 0
    for proj_dir in dirs:
        for md in sorted(proj_dir.glob("*.md")):   # нерекурсивно: daily/archive вне обхода
            try:
                text = md.read_text(encoding="utf-8")
            except Exception as e:
                print("!! {}/{}: не прочитать ({})".format(proj_dir.name, md.name, e))
                continue
            fixed, stats = heal_leaked_call_text(text)
            totals["suspicious"] += stats["suspicious"]
            if stats["suspicious"]:
                print("?? {}/{}: блоков без якоря {} (не тронуты)".format(
                    proj_dir.name, md.name, stats["suspicious"]))
            if fixed == text:
                continue
            touched += 1
            for key in ("anchors", "invokes", "fields", "open_tails"):
                totals[key] += stats[key]
            print("{}/{}: якорей {}, хвостов без якоря {}, полей {}, invoke {}".format(
                proj_dir.name, md.name, stats["anchors"], stats["open_tails"],
                stats["fields"], stats["invokes"]))
            if apply:
                md.write_text(fixed, encoding="utf-8")
    print("\nИтого: статей {}; якорей {}, хвостов без якоря {}, полей {}, invoke {}, "
          "подозрительных {}{}".format(
              touched, totals["anchors"], totals["open_tails"], totals["fields"],
              totals["invokes"], totals["suspicious"],
              "" if apply else " [dry-run, ничего не записано]"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
