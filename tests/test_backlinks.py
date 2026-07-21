"""Обратные связи: кто ссылается на статью.

Считаются только РУЧНЫЕ связи. Замер базы 2026-07-21: ручных ссылок 264 на 118 статей,
а авто-блоков «См. также» — 2608. Авто-ссылки сгенерированы по семантике, то есть
повторяют сайдбар похожих; попади они в выдачу, бэклинки были бы на 90% шумом и
рассказывали бы не «кто сослался», а «что похоже» — вопрос, на который уже отвечает
related.

Цели вики-ссылок разрешаются по ИМЕНИ ФАЙЛА: из 173 целей живой базы по имени файла
разрешились 124, по заголовку — НОЛЬ, алиасов '|' — ноль. Поэтому эвристики по
заголовкам нет: её незачем писать и нечем проверить.
"""
import asyncio

import memory_compiler.config as _cfg
from memory_compiler.handlers import backlinks


def _write(kd, project, name, body, title="Заголовок"):
    p = kd / project
    p.mkdir(exist_ok=True)
    (p / name).write_text(
        f"# {title}\n\n**Проект:** {project}\n**Теги:** test\n"
        f"**Дата:** 2026-07-21 10:00\n\n## Записи\n\n{body}\n",
        encoding="utf-8",
    )
    _cfg.PROJECTS = _cfg._discover_projects()


def _text(result):
    return result[0].text


def test_finds_wiki_link_from_body(knowledge_dir):
    _write(knowledge_dir, "testproj", "target.md", "тело цели")
    _write(knowledge_dir, "testproj", "source.md",
           "Разбор опирается на [[target]] — там детали.", title="Источник")

    out = _text(asyncio.run(backlinks(project="testproj", filename="target.md")))
    assert "source.md" in out, f"вики-ссылка из тела не найдена: {out}"


def test_finds_markdown_link_from_body(knowledge_dir):
    _write(knowledge_dir, "testproj", "target.md", "тело цели")
    _write(knowledge_dir, "testproj", "source_md.md",
           "См. разбор в [цели](../testproj/target.md) выше.", title="Источник-2")

    out = _text(asyncio.run(backlinks(project="testproj", filename="target.md")))
    assert "source_md.md" in out, f"markdown-ссылка из тела не найдена: {out}"


def test_ignores_auto_see_also_block(knowledge_dir):
    """Ссылка из авто-блока «См. также» — не бэклинк, а результат семантики."""
    _write(knowledge_dir, "testproj", "target.md", "тело цели")
    p = knowledge_dir / "testproj"
    (p / "auto.md").write_text(
        "# Авто\n\n**Проект:** testproj\n**Теги:** test\n**Дата:** 2026-07-21 10:00\n\n"
        "## Записи\n\nтело без ссылок\n\n"
        "## См. также\n- [Цель](../testproj/target.md) (2026-07-21)\n",
        encoding="utf-8")
    _cfg.PROJECTS = _cfg._discover_projects()

    out = _text(asyncio.run(backlinks(project="testproj", filename="target.md")))
    assert "auto.md" not in out, f"ссылка из авто-блока попала в бэклинки: {out}"


def test_ignores_git_refs_block(knowledge_dir):
    """«## Git-ссылки» — тоже машинный блок."""
    _write(knowledge_dir, "testproj", "target.md", "тело цели")
    p = knowledge_dir / "testproj"
    (p / "gitref.md").write_text(
        "# Гит\n\n**Проект:** testproj\n**Теги:** test\n**Дата:** 2026-07-21 10:00\n\n"
        "## Записи\n\nтело\n\n## Git-ссылки\n- [Цель](../testproj/target.md)\n",
        encoding="utf-8")
    _cfg.PROJECTS = _cfg._discover_projects()

    out = _text(asyncio.run(backlinks(project="testproj", filename="target.md")))
    assert "gitref.md" not in out


def test_returns_context_line(knowledge_dir):
    """Без строки контекста бэклинк бесполезен: непонятно, в связи с чем сослались."""
    _write(knowledge_dir, "testproj", "target.md", "тело цели")
    _write(knowledge_dir, "testproj", "source.md",
           "Ротацию убрали намеренно, обоснование в [[target]].", title="Источник")

    out = _text(asyncio.run(backlinks(project="testproj", filename="target.md")))
    assert "Ротацию убрали намеренно" in out, f"нет строки контекста: {out}"


def test_cross_project_link_found(knowledge_dir):
    _write(knowledge_dir, "testproj", "target.md", "тело цели")
    _write(knowledge_dir, "otherproj", "outer.md",
           "Связано с [[target]] из соседнего проекта.", title="Чужой проект")

    out = _text(asyncio.run(backlinks(project="testproj", filename="target.md")))
    assert "outer.md" in out and "otherproj" in out


def test_dead_target_does_not_break(knowledge_dir):
    """Битая цель (28% вики-ссылок живой базы) не должна ронять вызов."""
    _write(knowledge_dir, "testproj", "source.md",
           "Ссылка в никуда: [[nonexistent_target_xyz]].", title="Источник")

    out = _text(asyncio.run(backlinks(project="testproj", filename="source.md")))
    assert "❌" not in out


def test_no_backlinks_is_explicit(knowledge_dir):
    _write(knowledge_dir, "testproj", "lonely.md", "никто не ссылается")

    out = _text(asyncio.run(backlinks(project="testproj", filename="lonely.md")))
    assert "lonely.md" in out or "нет" in out.lower()


def test_ignores_links_inside_code_fences(knowledge_dir):
    """`[[…]]` в блоке кода — не ссылка. Это синтаксис TOML (массив таблиц).

    На живой базе 2026-07-21: `[[tool.mypy.overrides]]` встречается 11 раз, `[[X]]` —
    13 раз. Сегодня безвредно (не разрешаются), но стоит завести статью с таким именем —
    и получим ложную связь из куска конфига.
    """
    _write(knowledge_dir, "testproj", "tool.mypy.overrides.md", "тело цели")
    p = knowledge_dir / "testproj"
    (p / "конфиг.md").write_text(
        "# Конфиг\n\n**Проект:** testproj\n**Теги:** test\n**Дата:** 2026-07-21 10:00\n\n"
        "## Записи\n\nПример настройки:\n\n```toml\n[[tool.mypy.overrides]]\n"
        "ignore_missing_imports = true\n```\n\nВот и всё.\n",
        encoding="utf-8")
    _cfg.PROJECTS = _cfg._discover_projects()

    out = _text(asyncio.run(backlinks(project="testproj", filename="tool.mypy.overrides.md")))
    assert "конфиг.md" not in out, f"TOML из блока кода принят за вики-ссылку: {out}"


def test_unclosed_brackets_do_not_swallow_text(knowledge_dir):
    """Незакрытая `[[` в прозе — не ссылка и не «цель длиной в абзац».

    Живой случай (лог линта 2026-07-21): статья описывала триггер автодополнения
    словами «инлайн [[ или # → дропдаун», закрывающих скобок не было, и разбор
    проглатывал сотни символов до первой `]`. В backlinks это безвредно (не
    разрешается), но линт печатал такую «цель» на двадцать строк.
    """
    from memory_compiler.handlers import _link_targets

    text = ("Инлайн [[ или # → дропдаун. Vanilla JS: слушать input, "
            "regex /(\\[\\[)/ и открывать список.\n\nА это настоящая: [[target]].")
    wiki, _ = _link_targets(text)
    assert wiki == {"target"}, f"разбор поймал мусор: {wiki}"


def test_bracketed_phrase_is_not_a_link(knowledge_dir):
    """Скобки вокруг фразы с пробелами — не имя статьи: имена пробелов не содержат."""
    from memory_compiler.handlers import _link_targets

    wiki, _ = _link_targets("Ссылка на [[несколько слов подряд]] и на [[target]].")
    assert wiki == {"target"}, f"фраза с пробелами принята за ссылку: {wiki}"


def test_rejects_traversal(knowledge_dir):
    out = _text(asyncio.run(backlinks(project="..", filename="outside.md")))
    assert "❌" in out, f"traversal не отвергнут: {out}"


def test_does_not_count_self_reference(knowledge_dir):
    """Статья, упоминающая саму себя, не бэклинк на себя же."""
    _write(knowledge_dir, "testproj", "target.md", "Здесь ссылка на [[target]] саму себя.")

    out = _text(asyncio.run(backlinks(project="testproj", filename="target.md")))
    assert out.count("target.md") <= 2, f"самоссылка попала в выдачу: {out}"
