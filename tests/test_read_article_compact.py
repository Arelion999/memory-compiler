"""read_article прячет служебные разделы (v1.91.0).

Замер 24.09.2026: в телах статей, прочитанных за 14 дней, авто-раздел «См. также»
занимал 11,5%, «Git-ссылки» 0,4%, frontmatter contexts: 1,3%. Модели они почти не
нужны, а оплачиваются на каждом следующем шаге сессии.
"""
import asyncio

from memory_compiler import handlers

ARTICLE = (
    "# Статья про роутер\n\n"
    "**Дата:** 2026-09-01 10:00\n"
    "**Проект:** testproj\n"
    "**Теги:** router\n\n"
    "## Записи\n\n"
    "### 2026-09-01 10:00\n"
    "Первая запись.\n\n"
    "## См. также\n"
    "- [Соседняя статья](../testproj/sosed.md) (2026-08-01)\n"
    "- [Ещё одна](../testproj/esche.md) (2026-08-02)\n\n"
    "### 2026-09-02 11:00\n"
    "Запись после раздела — обязана остаться.\n\n"
    "## Git-ссылки\n"
    "**Коммиты:** abc1234\n"
)


def _read(knowledge_dir, name, text, **kw):
    (knowledge_dir / "testproj" / name).write_text(text, encoding="utf-8")
    return asyncio.run(handlers.read_article("testproj", name, **kw))[0].text


def test_service_sections_are_hidden_but_later_entries_stay(knowledge_dir):
    out = _read(knowledge_dir, "r.md", ARTICLE)
    assert "Соседняя статья" not in out and "abc1234" not in out
    assert "Запись после раздела — обязана остаться." in out, (
        "запись ### после «См. также» пропала: граница раздела одинарная")
    assert "Первая запись." in out


def test_footnote_names_only_what_was_hidden(knowledge_dir):
    out = _read(knowledge_dir, "r.md", ARTICLE)
    assert out.endswith(
        "*Скрыто: «См. также» (статей: 2), «Git-ссылки» — целиком: full=true*"), out[-200:]
    only_see = ARTICLE.split("## Git-ссылки")[0]
    out2 = _read(knowledge_dir, "s.md", only_see)
    assert out2.endswith("*Скрыто: «См. также» (статей: 2) — целиком: full=true*"), out2[-200:]


def test_full_returns_everything(knowledge_dir):
    out = _read(knowledge_dir, "r.md", ARTICLE, full=True)
    assert "Соседняя статья" in out and "abc1234" in out and "Скрыто" not in out


def test_full_accepts_int_and_string_one(knowledge_dir):
    """Флаг full от клиента, который не привёл тип, может приехать как число 1
    или строка '1' — `str(full).lower() == "true"` их отвергал."""
    out_int = _read(knowledge_dir, "r1.md", ARTICLE, full=1)
    assert "Соседняя статья" in out_int and "abc1234" in out_int
    out_str = _read(knowledge_dir, "r2.md", ARTICLE, full="1")
    assert "Соседняя статья" in out_str and "abc1234" in out_str
    out_str_upper = _read(knowledge_dir, "r3.md", ARTICLE, full="TRUE")
    assert "Соседняя статья" in out_str_upper and "abc1234" in out_str_upper


def test_frontmatter_is_hidden(knowledge_dir):
    fm = '---\ncontexts:\n  - heading: "A"\n    context: "служебный контекст"\n---\n'
    out = _read(knowledge_dir, "f.md", fm + ARTICLE)
    assert "contexts:" not in out and "служебный контекст" not in out
    assert out.startswith("# Статья про роутер")


def test_plain_article_is_unchanged(knowledge_dir):
    plain = ARTICLE.split("## См. также")[0]
    assert _read(knowledge_dir, "p.md", plain) == _read(knowledge_dir, "p.md", plain, full=True)


def test_heading_inside_code_block_is_not_a_section(knowledge_dir):
    text = ARTICLE.split("## См. также")[0] + "```\n## См. также\n- пример формата\n```\n"
    out = _read(knowledge_dir, "c.md", text)
    assert "- пример формата" in out and "Скрыто" not in out


def test_secret_is_decrypted_and_compacted(knowledge_dir, monkeypatch):
    import memory_compiler.config as cfg
    from memory_compiler import storage
    monkeypatch.setattr(cfg, "MC_ENCRYPT_KEY", "test-secret-key-123")
    enc = storage.encrypt_content("логин admin, пароль qwerty")
    text = ("# Доступ к роутеру\n\n**Дата:** 2026-09-01 10:00\n**Теги:** secret\n**Секрет:** да\n\n"
            f"{enc}\n\n## См. также\n- [Сосед](../testproj/sosed.md) (2026-08-01)\n")
    out = _read(knowledge_dir, "secret_router.md", text)
    assert "пароль qwerty" in out and "Сосед" not in out


def test_full_rewrite_keeps_service_sections(knowledge_dir):
    """Модель читает короткую версию и переписывает статью целиком — разделы,
    которых она не видела, обязаны остаться в файле."""
    (knowledge_dir / "testproj" / "r.md").write_text(ARTICLE, encoding="utf-8")
    asyncio.run(handlers.edit_article("testproj", "r.md", content="Новое тело статьи.", append=False))
    disk = (knowledge_dir / "testproj" / "r.md").read_text(encoding="utf-8")
    assert "Новое тело статьи." in disk
    assert "- [Соседняя статья](../testproj/sosed.md) (2026-08-01)" in disk
    assert "**Коммиты:** abc1234" in disk


def test_section_sent_by_author_is_not_duplicated(knowledge_dir):
    (knowledge_dir / "testproj" / "r.md").write_text(ARTICLE, encoding="utf-8")
    content = "Новое тело.\n\n## См. также\n- [Своя ссылка](../testproj/own.md)"
    asyncio.run(handlers.edit_article("testproj", "r.md", content=content, append=False))
    disk = (knowledge_dir / "testproj" / "r.md").read_text(encoding="utf-8")
    assert disk.count("## См. также") == 1 and "Своя ссылка" in disk
    assert "**Коммиты:** abc1234" in disk, "«Git-ссылки» автор не присылал — раздел обязан переехать"


def test_heading_mentioned_inside_fence_does_not_block_transfer(knowledge_dir):
    """Упоминание «## Git-ссылки» внутри ```-примера — не присланный автором раздел,
    а прежняя проверка `heading not in content` была подстрокой по всему тексту и
    молча отменяла перенос."""
    (knowledge_dir / "testproj" / "r.md").write_text(ARTICLE, encoding="utf-8")
    content = "Новое тело.\n\nПример формата:\n```\n## Git-ссылки\n**Коммиты:** пример\n```\n"
    asyncio.run(handlers.edit_article("testproj", "r.md", content=content, append=False))
    disk = (knowledge_dir / "testproj" / "r.md").read_text(encoding="utf-8")
    assert disk.count("## Git-ссылки") == 2, "раздел из старого файла обязан переехать рядом с примером"
    assert "**Коммиты:** abc1234" in disk, "старый раздел «Git-ссылки» пропал"
