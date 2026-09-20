"""Состав стартового контекста по транскриптам Claude Code: что занимает бюджет.

Размер выдачи `start_task` держит один потолок (`START_BUDGET`), и раздаётся он
water-fill'ом: освободившееся место достаётся голодным соседям. Поэтому «убрать
блок» экономии не даёт, пока блоки голодны, — а голодны они у половины вызовов.
Скрипт показывает, кто именно занимает место и сколько вызовов упирается в
потолок, то есть даёт ту цифру, по которой решают, менять ли сам потолок.

Мерить надо ЗДЕСЬ, а не по полю size в `_audit.log`: аудит считает отданное
сервером, транскрипт — полученное моделью.

⚠️ Выдачу опознаём по НАЧАЛУ текста («# Контекст для:»), а не по вхождению:
та же строка попадается в чтениях `handlers_sessions.py` и в логах, и тогда в
выборку приезжают куски исходников (замер 20.09.2026: два «вызова» на 50 и 36
тыс. символов оказались чтением кода).

Запуск: python scripts/start_context_usage.py [--days 7] [--root DIR]
"""
import argparse
import collections
import datetime
import glob
import json
import os
import re
import statistics

MARK = "# Контекст для:"
# Верхнеуровневые блоки стартового контекста. Заголовок несёт ещё и имя проекта
# в скобках, поэтому сверяем по началу строки.
BLOCKS = ("Найдено", "Открытые вопросы", "Сроки на исходе", "Факты прошлых сессий",
          "Связанные действия", "Недавняя активность", "Сессия в работе",
          "Предыдущая сессия", "Compact history", "Решения по теме", "Runbooks",
          "Из зависимых проектов")


def collect(root: str, days: int):
    """Тексты выдач start_task за окно, без дублей по tool_use_id."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    seen, out = set(), []
    for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if MARK not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    try:
                        when = datetime.datetime.fromisoformat(
                            rec.get("timestamp", "").replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if when < cutoff:
                        continue
                    for item in ((rec.get("message") or {}).get("content") or []):
                        if not isinstance(item, dict) or item.get("type") != "tool_result":
                            continue
                        tid = item.get("tool_use_id")
                        if not tid or tid in seen:
                            continue
                        body = item.get("content")
                        text = ("".join(c.get("text", "") for c in body if isinstance(c, dict))
                                if isinstance(body, list) else str(body or ""))
                        if not text.lstrip().startswith(MARK):
                            continue
                        seen.add(tid)
                        out.append((bool(rec.get("isSidechain")), text))
        except OSError:
            continue
    return out


def report(rows, ceiling: int):
    texts = [t for _side, t in rows]
    if not texts:
        print("выдач start_task за окно не найдено")
        return
    sizes = sorted(len(t) for t in texts)
    total = sum(sizes)
    p90 = sizes[max(int(len(sizes) * 0.9) - 1, 0)]
    near = sum(1 for s in sizes if s > ceiling * 0.9)
    print("выдач %d (сайдчейн %d), символов всего %d"
          % (len(rows), sum(1 for s, _t in rows if s), total))
    print("размер: медиана %d, p90 %d, max %d, min %d"
          % (statistics.median(sizes), p90, sizes[-1], sizes[0]))
    print("у потолка %d (>90%%): %d из %d" % (ceiling, near, len(sizes)))

    # ⚠️ Блок считается до следующего «## » ЛЮБОГО уровня, а тело сессии само
    # состоит из заголовков-дат, поэтому «Предыдущая сессия» здесь занижена, а
    # её хвост попадает в «прочее». Для долей Найдено/Вопросы это неважно, для
    # выводов о блоке сессии — важно: смотреть отдельно.
    head = re.compile(r"^## (%s)" % "|".join(map(re.escape, BLOCKS)), re.M)
    chars, hits = collections.Counter(), collections.Counter()
    for text in texts:
        bounds = [m.start() for m in re.finditer(r"^## ", text, re.M)]
        for m in head.finditer(text):
            nxt = [b for b in bounds if b > m.start()]
            end = nxt[0] if nxt else len(text)
            chars[m.group(1)] += end - m.start()
            hits[m.group(1)] += 1
    print("\n%-26s%9s%8s%9s%10s" % ("блок", "симв", "доля", "вызовов", "на вызов"))
    for name, n in chars.most_common():
        print("%-26s%9d%7.1f%%%9d%10d"
              % (name, n, 100.0 * n / total, hits[name], n // max(hits[name], 1)))
    rest = total - sum(chars.values())
    print("%-26s%9d%7.1f%%" % ("прочее (шапка, хвост)", rest, 100.0 * rest / total))

    expired = sum(1 for t in texts if "истёк" in t)
    print("\nвыдач с истёкшим сроком: %d (с v1.89.0 ожидается 0)" % expired)
    scores = [float(x) for x in re.findall(r"hybrid: (\d+\.?\d*)", "\n".join(texts))]
    if scores:
        scores.sort()
        print("hybrid-скоры показанных находок: n=%d, min %.1f, p10 %.1f, медиана %.1f"
              % (len(scores), scores[0], scores[max(int(len(scores) * 0.1) - 1, 0)],
                 statistics.median(scores)))


def main():
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--root", default=os.path.expanduser(r"~\.claude\projects"))
    ap.add_argument("--ceiling", type=int, default=4500, help="START_BUDGET для отчёта")
    args = ap.parse_args()
    report(collect(args.root, args.days), args.ceiling)


if __name__ == "__main__":
    main()
