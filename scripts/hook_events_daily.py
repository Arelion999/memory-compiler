"""События хуков памяти по дням: рефлексы, живая карточка, гейт, отказы записи.

Чем приживаемость механизма меряется ПО ДНЯМ, а не суммой за всё время: журнал
`mc_hooks.log` накапливается с 26.08.2026, а карточка появилась 14.09 — сумма за
весь журнал сравнивала бы разные эпохи. Отдельно печатается доля покрытых ошибок
hit/(hit+miss): абсолютный `reflex.miss` падает вместе с активностью и сам по
себе ничего не говорит.

Журнал лежит вне репозитория (`~/.claude/hooks/mc_hooks.log`) — хук клиента
живёт там же.

Запуск: python scripts/hook_events_daily.py [--days 14] [--log PATH] [--detail gate.card]
"""
import argparse
import collections
import json
import os

WATCH = ("gate.card", "gate.block", "probe.verified", "probe.reachable", "probe.error",
         "reflex.hit", "reflex.miss", "verify.added", "nudge.shown", "mc.fail")
COLS = (("card", "gate.card"), ("block", "gate.block"), ("verif", "probe.verified"),
        ("reach", "probe.reachable"), ("r.hit", "reflex.hit"), ("r.miss", "reflex.miss"),
        ("v.add", "verify.added"), ("nudge", "nudge.shown"), ("fail", "mc.fail"))


def _sources(path: str):
    """Журнал и его архивы, от старого к свежему.

    ⚠️ Без архивов отчёт молча теряет историю: журнал ротируется по 2 МБ
    (`mc_guard._rotate_hook_log`), и всё, что старше последней ротации, лежит в
    `mc_hooks.log.1` / `.log.2`. Замер «по дням» именно этим и живёт.
    """
    found = []
    for n in range(9, 0, -1):
        archive = "%s.%d" % (path, n)
        if os.path.exists(archive):
            found.append(archive)
    if os.path.exists(path):
        found.append(path)
    return found


def read(path: str):
    days = collections.defaultdict(collections.Counter)
    sessions = collections.defaultdict(set)
    detail = collections.defaultdict(list)
    files = _sources(path)
    if not files:
        print("журнал не найден: %s" % path)
    for src in files:
        with open(src, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                day, act = ev.get("ts", "")[:10], ev.get("action", "")
                if not day:
                    continue
                if act in WATCH:
                    days[day][act] += 1
                    detail[(day, act)].append("%s %s" % (ev.get("tool", ""),
                                                         (ev.get("detail") or "")[:90]))
                if ev.get("session"):
                    sessions[day].add(ev["session"])
    return days, sessions, detail


def main():
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--log", default=os.path.expanduser(r"~\.claude\hooks\mc_hooks.log"))
    ap.add_argument("--detail", help="показать строки этого события (напр. gate.card)")
    args = ap.parse_args()

    days, sessions, detail = read(args.log)
    order = sorted(days)[-args.days:]
    header = ["день", "сесс"] + [name for name, _a in COLS] + ["hit%"]
    print("  ".join(h.rjust(6) for h in header))
    for day in order:
        cnt = days[day]
        hit, miss = cnt["reflex.hit"], cnt["reflex.miss"]
        share = "%d%%" % round(100.0 * hit / (hit + miss)) if hit + miss else "—"
        row = [day[5:], str(len(sessions[day]))] + [str(cnt[a]) for _n, a in COLS] + [share]
        print("  ".join(v.rjust(6) for v in row))

    if args.detail:
        print("\n=== %s ===" % args.detail)
        for day in order:
            for line in detail[(day, args.detail)]:
                print("%s  %s" % (day[5:], line))


if __name__ == "__main__":
    main()
