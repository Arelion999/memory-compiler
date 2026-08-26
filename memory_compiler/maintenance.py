"""Одноразовые maintenance-проходы по базе знаний.

Ремедиация issue #2: save_lesson писал запись и в статью, и в daily-лог, а compile
мержил её из лога ВТОРОЙ раз — в статьях появлялись дубли секций '### <ts>' и ложное
«Обновлено» == «Дата». Код починен (дедуп в merge_into_article); этот модуль чинит
уже задвоенные статьи.

Запуск на NAS (источник правды — /knowledge в контейнере):
    docker exec memory-compiler-mcp python -m memory_compiler.maintenance --dry-run
    docker exec memory-compiler-mcp python -m memory_compiler.maintenance

После боевого прогона нужен reindex (MCP tool / Web UI): preview и body лежат
в whoosh-индексе как STORED-поля.
"""
import json
import re
import sys

from memory_compiler.config import KNOWLEDGE_DIR, PROJECTS, is_secret_article
from memory_compiler.storage import dedupe_article_sections, git_commit, parse_meta_value


def dedupe_all_articles(dry_run: bool = False) -> tuple[int, int]:
    """Пройти все статьи всех проектов, удалить задвоенные секции.
    Возвращает (статей затронуто, секций удалено)."""
    total_removed = 0
    touched = 0
    for proj in PROJECTS:
        proj_dir = KNOWLEDGE_DIR / proj
        if not proj_dir.exists():
            continue
        for md in sorted(proj_dir.glob("*.md")):
            if md.name.startswith("_"):
                continue  # служебные (_active_context и т.п.)
            try:
                text = md.read_text(encoding="utf-8")
            except Exception as e:
                print(f"!! {proj}/{md.name}: не прочитать ({e})")
                continue
            if is_secret_article(text, md.name):
                continue  # секреты не трогаем
            fixed, removed = dedupe_article_sections(text)
            if removed:
                touched += 1
                total_removed += removed
                print(f"{proj}/{md.name}: -{removed} дубл.")
                if not dry_run:
                    md.write_text(fixed, encoding="utf-8")
    print(f"\nИтого: статей {touched}, секций-дублей {total_removed}"
          + (" [dry-run, ничего не записано]" if dry_run else ""))
    if not dry_run and total_removed:
        git_commit(f"maintenance: дедуп задвоенных секций (issue #2) — "
                   f"статей {touched}, секций {total_removed}")
    return touched, total_removed


# ─── Ремедиация порчи разметки шапки ─────────────────────────────────────────

# Строка метаданных, у которой значение начинается с мусорных звёздочек:
# '**Теги:** ** ftp, mcp'. Требуем ПРОБЕЛ после звёздочек — иначе под правило
# попал бы легитимный жирный текст вида '**Теги:** **важно**'.
_JUNK_META_RE = re.compile(r"^(\*\*[^:*]+:\*\*)\s*\*+\s+\S")

# Заголовок секции с мусором: '### ** 2026-04-16 15:39'. Осторожно: правим ТОЛЬКО
# когда после звёздочек идёт дата — ровно та форма, которую порождал
# merge_into_article. Легитимный '### **Важно**' не трогаем.
_JUNK_HEADING_RE = re.compile(r"^###\s+\*+\s+(\d{4}-\d{2}-\d{2}.*)$")


def heal_header_markup(dry_run: bool = False) -> tuple:
    """Вычистить порчу разметки шапки, оставленную lint с fix=True.

    Что чинит: значения метаданных с мусорными '**' (в т.ч. двойными), заголовки
    секций '### ** <дата>', повторные '**Обновлено:**' в шапке.

    ⚠️ Секреты НЕ пропускаем, в отличие от dedupe_all_articles: 81 из 126 порченых
    статей — именно секреты, и самолечиться они не могут (merge_into_article им
    отказывает). Правятся ТОЛЬКО строки шапки и заголовки секций; строка ENC:
    ни под одно правило не подходит и остаётся нетронутой.

    Порядок обязателен: сначала выкатить починенный код, потом этот проход. Иначе
    первый же тег с заглавной буквой испортит статьи заново.
    """
    stats = {"tags": 0, "headings": 0, "updated_dupes": 0}
    touched = 0
    for proj in PROJECTS:
        proj_dir = KNOWLEDGE_DIR / proj
        if not proj_dir.exists():
            continue
        for md in sorted(proj_dir.glob("*.md")):
            try:
                text = md.read_text(encoding="utf-8")
            except Exception as e:
                print(f"!! {proj}/{md.name}: не прочитать ({e})")
                continue
            lines = text.splitlines()
            header_end = next((i for i, l in enumerate(lines)
                               if l.startswith("## Записи") or l.startswith("### ")),
                              len(lines))
            out, seen_updated, changes = [], False, []
            for i, line in enumerate(lines):
                if _JUNK_META_RE.match(line):
                    label = line.split(":", 1)[0] + ":**"
                    line = f"{label} {parse_meta_value(line)}"
                    stats["tags"] += 1
                    changes.append("шапка")
                m = _JUNK_HEADING_RE.match(line)
                if m:
                    line = f"### {m.group(1)}"
                    stats["headings"] += 1
                    changes.append("заголовок")
                if i < header_end and line.startswith("**Обновлено:**"):
                    if seen_updated:
                        stats["updated_dupes"] += 1
                        changes.append("дубль Обновлено")
                        continue  # второй и далее — выбрасываем
                    seen_updated = True
                out.append(line)
            if not changes:
                continue
            touched += 1
            print(f"{proj}/{md.name}: {', '.join(sorted(set(changes)))}")
            if not dry_run:
                fixed = "\n".join(out) + ("\n" if text.endswith("\n") else "")
                md.write_text(fixed, encoding="utf-8")
    print(f"\nИтого: статей {touched}; строк метаданных {stats['tags']}, "
          f"заголовков {stats['headings']}, дублей «Обновлено» {stats['updated_dupes']}"
          + (" [dry-run, ничего не записано]" if dry_run else ""))
    if not dry_run and touched:
        git_commit(f"maintenance: вычищена порча разметки шапки от lint fix=True — "
                   f"статей {touched}")
    return touched, stats


# ─── Ремедиация утёкшей разметки вызова ──────────────────────────────────────
# Замер 2026-07-27: 208 живых статей с хвостом «…</content> + блоки полей +
# </invoke>» — клиентский парсер не закрывал параметр, и остаток блока вызова
# въезжал в значение content (guard в call_tool с v1.50.0 не пускает НОВЫЕ
# утечки; этот проход чистит накопленные). Поля НЕ выбрасываются:
# session_summary/open_questions переносятся прозой, tags сливаются в шапку.
# Якорь — ТОЛЬКО строка, оканчивающаяся '</content>': упоминания в середине
# строк (статьи про сам баг) и fenced-код не трогаются. Одиночная строка
# '</invoke>' вне кода — всегда мусор. Блоки полей БЕЗ якоря не трогаются
# и считаются в suspicious — смотреть их в dry-run глазами.

_LEAK_ANCHOR = "</content>"
_LEAK_FIELD_OPEN_RE = re.compile(r"^<(session_summary|open_questions|tags)>")
_LEAK_PARAM_OPEN_RE = re.compile(r'^<parameter name="([\w-]+)">')
_LEAK_LABELS = {"session_summary": "Итог сессии", "open_questions": "Открытые вопросы",
                "alternatives": "Альтернативы", "decisions": "Решения",
                "reasoning": "Обоснование", "content": "Содержание"}


def _try_json_list(raw: str):
    """JSON-массив строк или None (битые «[не json» восстановлению не подлежат)."""
    try:
        val = json.loads(raw)
    except Exception:
        return None
    if isinstance(val, list) and all(isinstance(x, str) for x in val):
        return val
    return None


def _leak_block_at(lines: list, i: int):
    """Блок утёкшего поля с начала строки i: (имя, значение, next_i) или None.
    Значение может тянуться до закрывающего тега В КОНЦЕ строки; нет закрытия
    в пределах 30 строк — не блок (случайное совпадение, не трогаем)."""
    m_p = _LEAK_PARAM_OPEN_RE.match(lines[i])
    m_f = None if m_p else _LEAK_FIELD_OPEN_RE.match(lines[i])
    if not (m_p or m_f):
        return None
    name = (m_p or m_f).group(1)
    close = "</parameter>" if m_p else f"</{name}>"
    open_len = len(m_p.group(0)) if m_p else len(f"<{name}>")
    buf = []
    own_close = f"</{name}>"
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
        # незакрытый хвост «дотянулся» бы до чужого закрытия ниже по статье и съел
        # весь текст между ними.
        if j > i and (not lines[j].strip() or _LEAK_PARAM_OPEN_RE.match(lines[j])):
            break
        buf.append(cur)
    # Незакрытый параметрический блок (v1.54.3): клиент не дописал закрывающий тег
    # вовсе — значение берём как остаток ОДНОЙ строки. Голая форма '<session_summary>'
    # без 'parameter name' так не лечится: это другой класс (см. suspicious).
    if m_p:
        return name, lines[i][open_len:].strip(), i + 1
    return None


def heal_leaked_call_text(text: str) -> tuple:
    """Вычистить хвосты утёкшей разметки из текста одной статьи.
    Возвращает (текст, stats): anchors — срезанных якорей '</content>',
    invokes — удалённых строк '</invoke>', fields — перенесённых полей,
    suspicious — блоков без якоря (НЕ тронуты)."""
    lines = text.splitlines()
    stats = {"anchors": 0, "invokes": 0, "fields": 0, "suspicious": 0, "open_tails": 0}
    # Строка **Теги:** ИМЕННО шапки (до первого '## …'): в «## Git-ссылки»
    # живёт вторая такая строка с git-тегами — её сливать нельзя.
    header_has_tags = False
    for l in lines:
        if l.startswith("## "):
            break
        if l.startswith("**Теги:**"):
            header_has_tags = True
            break
    out: list = []
    collected_tags: list = []
    in_fence = False
    i, n = 0, len(lines)

    def _consume(pos: int, moved: list) -> int:
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
                moved.append(f"{_LEAK_LABELS.get(name, name)}: {val}")
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
            moved: list = []
            i = _consume(i, moved)
            for para in moved:
                if out and out[-1].strip():
                    out.append("")
                out.append(para)
            continue
        # Форма БЕЗ якоря '</content>' (v1.54.3): параметр закрыт корректно, а следом
        # с новой строки въехали блоки '<parameter name="q">…', последний обычно вообще
        # не закрыт. 216 живых статей вне daily/ на 2026-08-12, свежайшая — того же дня.
        # ГОЛЫЕ '<session_summary>…' без 'parameter name' сюда НЕ попадают и остаются
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
                existing = [t.strip() for t in parse_meta_value(l).split(",") if t.strip()]
                for t in collected_tags:      # поштучно: дедуп и против шапки, и внутри собранных
                    if t not in existing:
                        existing.append(t)
                out[k] = "**Теги:** " + ", ".join(existing)
                break
    fixed = "\n".join(out) + ("\n" if text.endswith("\n") else "")
    return fixed, stats


def heal_leaked_markup(dry_run: bool = False) -> tuple:
    """Проход по живым статьям: PROJECTS + daily/ (glob НЕрекурсивный, поэтому
    daily/archive/ — вне индекса, решение владельца 2026-07-21 — не зацепляется).
    Секреты НЕ пропускаем: хвост лежит открытым текстом после ENC-блока, а сам
    шифротекст ни под одно правило не подходит и остаётся нетронутым."""
    totals = {"anchors": 0, "invokes": 0, "fields": 0, "suspicious": 0, "open_tails": 0}
    touched = 0
    dirs = [KNOWLEDGE_DIR / p for p in PROJECTS] + [KNOWLEDGE_DIR / "daily"]
    for proj_dir in dirs:
        if not proj_dir.exists():
            continue
        for md in sorted(proj_dir.glob("*.md")):
            try:
                text = md.read_text(encoding="utf-8")
            except Exception as e:
                print(f"!! {proj_dir.name}/{md.name}: не прочитать ({e})")
                continue
            fixed, stats = heal_leaked_call_text(text)
            totals["suspicious"] += stats["suspicious"]
            if stats["suspicious"]:
                print(f"?? {proj_dir.name}/{md.name}: блоков без якоря {stats['suspicious']} (не тронуты)")
            if fixed == text:
                continue
            touched += 1
            for key in ("anchors", "invokes", "fields"):
                totals[key] += stats[key]
            print(f"{proj_dir.name}/{md.name}: якорей {stats['anchors']}, "
                  f"полей {stats['fields']}, invoke {stats['invokes']}")
            if not dry_run:
                md.write_text(fixed, encoding="utf-8")
    print(f"\nИтого: статей {touched}; якорей {totals['anchors']}, "
          f"полей {totals['fields']}, invoke {totals['invokes']}, "
          f"подозрительных {totals['suspicious']}"
          + (" [dry-run, ничего не записано]" if dry_run else ""))
    if not dry_run and touched:
        git_commit(f"maintenance: вычищена утёкшая разметка вызова — статей {touched}")
    return touched, totals



def _is_real_question(text: str) -> bool:
    """Отсеять записи вида «хвостов нет» — это ОТСУТСТВИЕ вопроса, а не вопрос.

    Без фильтра список открытых вопросов сразу наполнялся бы отчётами о том,
    что вопросов не осталось, и терял смысл ровно там, где должен помогать.
    """
    q = (text or "").strip().strip("*_ ").lower()
    if len(q) < 15:
        return False
    # ⚠️ Не startswith по списку фраз: «Хвостов ПО ЗАДАЧЕ нет» не начинается с
    # «хвостов нет», и такая запись проезжала фильтр (поймано тестом). Отрицание
    # ищем в пределах первой фразы, отсчитывая от предмета — хвосты, вопросы,
    # задачи, блокеры.
    return not _NO_QUESTION_RE.search(q)


_NO_QUESTION_RE = re.compile(
    r"^(?:нет\b|отсутств|none\b|n/?a\b|всё закрыто|все закрыто)"
    r"|^(?:хвост\w*|вопрос\w*|задач\w*|проблем\w*|блокер\w*|замечани\w*)"
    r"[^.?!]{0,60}?\b(?:нет|не оста\w+|закрыт\w*|решен\w*|решён\w*)\b",
    re.IGNORECASE,
)


def seed_questions_from_sessions(dry_run: bool = True):
    """Одноразово: перенести открытые вопросы из файлов сессий в _questions.md.

    Без этого прохода накопительный список стартует пустым, а последние
    зафиксированные вопросы 41 проекта так и остались бы только внутри блока
    журнала — то есть невидимыми для start_task и open_questions.

    Идемпотентен: add_question не заводит дубль уже открытого вопроса.
    """
    from memory_compiler.storage import add_question, _session_path
    import memory_compiler.config as _cfg

    seeded = skipped = 0
    for proj in sorted(p for p in _cfg.PROJECTS if p != "daily"):
        try:
            path = _session_path(proj)
        except ValueError:
            continue
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        found = []
        # Оба формата: новый «**Открытые вопросы:** …» внутри блока журнала и
        # старый раздел «## Открытые вопросы» однозаписного файла.
        NL = chr(10)
        pat_new = r"\*\*Открытые вопросы:\*\*\s*(.+?)(?=" + NL + r"\s*" + NL + r"|" + NL + r"## |\Z)"
        pat_old = r"^## Открытые вопросы\s*" + NL + r"(.+?)(?=" + NL + r"## |\Z)"
        for m in re.finditer(pat_new, text, re.S):
            found.append(m.group(1).strip())
        for m in re.finditer(pat_old, text, re.S | re.M):
            found.append(m.group(1).strip())
        for q in found:
            if not _is_real_question(q):
                continue
            if dry_run:
                print(f"{proj}: + {q[:90]}")
                seeded += 1
                continue
            if add_question(proj, q):
                seeded += 1
            else:
                skipped += 1
    print(f"\nИтого вопросов заведено: {seeded}, пропущено дублей: {skipped}"
          + (" [dry-run, ничего не записано]" if dry_run else ""))
    return seeded


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    if "--heal-markup" in sys.argv:
        heal_header_markup(dry_run=dry)
    elif "--heal-leaked" in sys.argv:
        heal_leaked_markup(dry_run=dry)
    elif "--seed-questions" in sys.argv:
        seed_questions_from_sessions(dry_run=dry)
    else:
        dedupe_all_articles(dry_run=dry)
