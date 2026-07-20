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


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    if "--heal-markup" in sys.argv:
        heal_header_markup(dry_run=dry)
    else:
        dedupe_all_articles(dry_run=dry)
