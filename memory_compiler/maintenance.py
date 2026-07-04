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
import sys

from memory_compiler.config import KNOWLEDGE_DIR, PROJECTS, is_secret_article
from memory_compiler.storage import dedupe_article_sections, git_commit


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


if __name__ == "__main__":
    dedupe_all_articles(dry_run="--dry-run" in sys.argv)
