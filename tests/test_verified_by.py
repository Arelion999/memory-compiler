"""Поле «чем проверено» у save_lesson (v1.71.0).

Из разбора владельца 27.08.2026: компилер принимает вывод модели как факт, не
различая проверенное инструментом и выведенное косвенно. Живой случай — статья
про потерю поддержки после /LoadCfg: вывод сделан по косвенному признаку, не
проверен в конфигураторе, и неверный факт прожил в базе до ручной проверки.

Замер по базе: маркеры неуверенности («вероятно», «похоже», «скорее всего») есть
в 168 статьях из 3050 (5%) — то есть класс существует, но пометки у него нет.

⚠️ ПОЛЕ ЖИВЁТ В ШАПКЕ, а это самое опасное место в формате: на «наивном разборе
шапки» база уже горела (11 мест, порча 126 статей, v1.43.0). Поэтому тесты здесь
проверяют не столько само поле, сколько ЦЕЛОСТЬ шапки после его появления.
"""

import pytest

from memory_compiler import handlers, storage


@pytest.mark.asyncio
async def test_verified_is_written_to_header(knowledge_dir, monkeypatch):
    monkeypatch.setattr(handlers, "git_commit", lambda *a, **k: None)
    await handlers.save_lesson("Тема проверки", "Тело статьи.", "testproj",
                               verified="прогон pytest, 964 теста")
    art = next(p for p in (knowledge_dir / "testproj").glob("*.md")
               if p.name.startswith("тема"))
    text = art.read_text(encoding="utf-8")
    assert "**Проверено:** прогон pytest, 964 теста" in text


@pytest.mark.asyncio
async def test_header_still_parses_after_new_field(knowledge_dir, monkeypatch):
    """Главное: канон разбора шапки не должен сбиться из-за новой метки."""
    monkeypatch.setattr(handlers, "git_commit", lambda *a, **k: None)
    await handlers.save_lesson("Разбор шапки", "Тело статьи.", "testproj",
                               tags=["альфа", "бета"], verified="живой вызов на проде")
    art = next(p for p in (knowledge_dir / "testproj").glob("*.md")
               if p.name.startswith("разбор"))
    text = art.read_text(encoding="utf-8")
    title, tags = storage.article_title_tags(text, art.stem)
    assert title == "Разбор шапки", "заголовок потерян"
    assert "альфа" in tags and "бета" in tags, "теги потеряны"


@pytest.mark.asyncio
async def test_verified_does_not_leak_into_preview(knowledge_dir, monkeypatch):
    """Метка — метаданные, а не контент: в превью и в индекс она попадать не должна.

    Без внесения в _HEADER_META_PREFIXES строка утекла бы в тело — и в выдачу
    поиска, и в ИИ-контексты, съедая бюджет ни за что.
    """
    monkeypatch.setattr(handlers, "git_commit", lambda *a, **k: None)
    await handlers.save_lesson("Превью без метки", "Содержательная строка.",
                               "testproj", verified="curl к /api/health")
    art = next(p for p in (knowledge_dir / "testproj").glob("*.md")
               if p.name.startswith("превью"))
    preview = storage.make_preview(art.read_text(encoding="utf-8"), n=10)
    assert "Содержательная строка." in preview
    assert "Проверено" not in preview, "метка шапки утекла в превью"


@pytest.mark.asyncio
async def test_field_is_optional(knowledge_dir, monkeypatch):
    """Позитивный контроль: без параметра статья прежняя, метки нет."""
    monkeypatch.setattr(handlers, "git_commit", lambda *a, **k: None)
    await handlers.save_lesson("Без проверки", "Тело.", "testproj")
    art = next(p for p in (knowledge_dir / "testproj").glob("*.md")
               if p.name.startswith("без"))
    assert "**Проверено:**" not in art.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_lint_does_not_complain_about_new_field(knowledge_dir, monkeypatch):
    """Линт не должен считать новую метку дефектом — иначе выдача снова зарастёт
    шумом, из которого её только что вычистили (41 → 0 в v1.54.4)."""
    monkeypatch.setattr(handlers, "git_commit", lambda *a, **k: None)
    await handlers.save_lesson("Линт и метка", "Тело статьи.", "testproj",
                               tags=["тест"], verified="прогон тестов")
    out = await handlers.lint("testproj", fix=False)
    text = out[0].text
    assert "Проверено" not in text, f"линт ругается на новую метку: {text[:400]}"
