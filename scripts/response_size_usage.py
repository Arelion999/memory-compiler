"""Из чего состоят ответы базы, как их получила модель (транскрипты Claude Code).

Дополняет scripts/model_side_usage.py: тот считает объём по инструментам, этот —
состав. Сколько в read_article служебных разделов и frontmatter, сколько весит
подсказка первого обращения, сколько статей и resource_link отдаёт search_by_tag,
как часто start_task повторяет статью в разных блоках. Базовая линия — замер
24.09.2026 перед v1.91.0: read_article 41,3% ответов базы, «См. также» 11,5% его
объёма, повтор статьи в 29% выдач start_task.

⚠️ Раздел считается до следующей `## ` ИЛИ `### `: записи `### дата` стоят и после
«См. также». С одинарной границей замер 24.09.2026 сначала завысил долю вдвое.

Запуск: python scripts/response_size_usage.py [--days 7] [--root DIR] [--prefix mcp__memory-compiler__]
"""
import argparse
import glob
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime

DEFAULT_PREFIX = "mcp__memory-compiler__"
FIRST_TOUCH = "📌 **Первое обращение к `"
# Заголовок находки до v1.91.0 нёс оценку «(hybrid: 90)», с v1.91.0 — нет. Замер
# «до и после» обязан понимать оба вида.
_FOUND = re.compile(r"^### \[[^\]]+\] (.+?)(?: \((?:hybrid|score)[^)]*\))?$", re.M)
_BOLD = re.compile(r"^- (?:\[[^\]]+\] )?\*\*(.+?)\*\*", re.M)


def _ts(o):
    try:
        return datetime.fromisoformat(o["timestamp"].replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _texts(content):
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [x.get("text", "") or "" for x in content if isinstance(x, dict)]
    return []


def iter_results(files, since=0.0, prefix=DEFAULT_PREFIX):
    """(инструмент, блоки текста) каждого результата, как их получила модель.
    Дубли по tool_use_id и реплики сабагентов (isSidechain) не считаются."""
    seen = set()
    for path in files:
        names = {}
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                content = (o.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        names[b.get("id")] = b.get("name", "")
                if o.get("isSidechain") or _ts(o) < since:
                    continue
                for b in content:
                    if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                        continue
                    tid = b.get("tool_use_id")
                    name = names.get(tid, "")
                    if not name.startswith(prefix) or tid in seen:
                        continue
                    seen.add(tid)
                    yield name[len(prefix):], _texts(b.get("content"))


def section_chars(text, heading):
    """Длина раздела: от строки-заголовка до следующей `## `/`### ` или конца."""
    total, inside = 0, False
    for line in text.split("\n"):
        if line.strip() == heading:
            inside = True
            total += len(line) + 1
            continue
        if inside:
            if line.startswith("## ") or line.startswith("### "):
                inside = False
                continue
            total += len(line) + 1
    return total


def _norm(s):
    return re.sub(r"\W+", " ", s.lower()).strip()


def _blocks(text):
    out = {}
    for part in re.split(r"\n(?=## )", text):
        m = re.match(r"## ([^\n(]+)", part)
        if m:
            out[m.group(1).strip()] = part
    return out


def measure(files, since=0.0, prefix=DEFAULT_PREFIX):
    tools = defaultdict(int)
    ra = dict.fromkeys(("n", "chars", "see_also", "git_links", "frontmatter"), 0)
    ft = {"n": 0, "chars": 0}
    bt = dict.fromkeys(("n", "chars", "lines", "links"), 0)
    st = dict.fromkeys(("n", "with_repeat", "repeats", "title_dup_chars"), 0)
    for tool, blocks in iter_results(files, since, prefix):
        text = "".join(blocks)
        tools[tool] += len(text)
        i = text.find(FIRST_TOUCH)
        if i != -1:
            j = text.find("⚠️ **Пока вы работали", i)
            ft["n"] += 1
            ft["chars"] += (j if j != -1 else len(text)) - i
        if tool == "read_article":
            ra["n"] += 1
            ra["chars"] += len(text)
            ra["see_also"] += section_chars(text, "## См. также")
            ra["git_links"] += section_chars(text, "## Git-ссылки")
            if text.startswith("---\n"):
                end = text.find("\n---\n", 4)
                if end != -1:
                    ra["frontmatter"] += end + 5
        elif tool == "search_by_tag":
            bt["n"] += 1
            bt["chars"] += len(text)
            bt["lines"] += sum(1 for line in text.split("\n")
                               if line.startswith("- [") or line.startswith("### ["))
            bt["links"] += sum(1 for b in blocks if b.startswith("[Resource link"))
        elif tool == "start_task" and text.lstrip().startswith("# Контекст для:"):
            st["n"] += 1
            parts = _blocks(text)
            found_block = next((v for k, v in parts.items() if k.startswith("Найдено")), "")
            found = _FOUND.findall(found_block)
            for t in found:
                if f"\n# {t}\n" in found_block:
                    st["title_dup_chars"] += len(t) + 3
            others = []
            for k, v in parts.items():
                if k.startswith(("Связанные действия", "Решения по теме", "Недавняя активность")):
                    others += _BOLD.findall(v)
            fset = {_norm(t) for t in found}
            rep = (sum(1 for t in others if _norm(t) in fset)
                   + len(others) - len({_norm(t) for t in others}))
            if rep:
                st["with_repeat"] += 1
                st["repeats"] += rep
    return {"tools": dict(tools), "total": sum(tools.values()),
            "read_article": ra, "first_touch": ft, "search_by_tag": bt, "start_task": st}


def _pct(a, b):
    return f"{a / b:.1%}" if b else "—"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Состав ответов базы по транскриптам Claude Code")
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--root", default=os.path.join(os.path.expanduser("~"), ".claude", "projects"))
    ap.add_argument("--prefix", default=DEFAULT_PREFIX)
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    since = time.time() - args.days * 86400
    files = [p for p in glob.glob(os.path.join(args.root, "**", "*.jsonl"), recursive=True)
             if os.path.getmtime(p) >= since]
    r = measure(files, since, args.prefix)
    total = r["total"]
    print(f"Ответы базы за {args.days:g} дн: {total:,} символов")
    for tool in ("read_article", "start_task", "search", "search_by_tag"):
        n = r["tools"].get(tool, 0)
        print(f"  {tool:14} {n:>10,}  {_pct(n, total)}")
    ra = r["read_article"]
    print(f"read_article: {ra['n']} чтений; «См. также» {_pct(ra['see_also'], ra['chars'])}, "
          f"«Git-ссылки» {_pct(ra['git_links'], ra['chars'])}, "
          f"frontmatter {_pct(ra['frontmatter'], ra['chars'])}")
    ft = r["first_touch"]
    print(f"«Первое обращение»: {ft['n']} раз, {_pct(ft['chars'], total)} ответов базы")
    bt = r["search_by_tag"]
    if bt["n"]:
        print(f"search_by_tag: {bt['n']} вызовов, статей на вызов {bt['lines'] / bt['n']:.1f}, "
              f"resource_link {bt['links']}, символов на вызов {bt['chars'] // bt['n']:,}")
    st = r["start_task"]
    if st["n"]:
        print(f"start_task: {st['n']} выдач, с повтором статьи {st['with_repeat']} "
              f"({_pct(st['with_repeat'], st['n'])}), повторов {st['repeats']}, заголовок в "
              f"отрывке {_pct(st['title_dup_chars'], r['tools'].get('start_task', 0))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
