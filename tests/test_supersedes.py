"""Связь «отменяет»: поправка не даёт читать опровергнутую статью как факт (v1.69.0).

Живой случай владельца 26.08.2026. По одной теме в проекте лежали четыре статьи:

    Барьера платформ нет: режим совместимости 8.3.27 …          97.6   ← поправка
    Контур 8.3.27 для выката ГСМ на прод не нужен …             96.8   ← поправка
    Итог дня 26.08: контур 8.3.27 на деве для переноса на прод  94.5   ← ОТМЕНЕНО
    Готовый порядок подъёма контура 8.3.27 на дев-сервере       92.3   ← ОТМЕНЕНО

Выдача показала все четыре РАВНОПРАВНО, агент взял ту, что выше по релевантности,
и заявил владельцу необходимость поднимать контур, которого не нужно. Поправка при
этом существовала и лежала в той же выдаче.

Текстовых признаков поправке хватало («мой прежний вывод был НЕВЕРНЫМ», тег
«поправка»), а машиночитаемой связи с отменёнными статьями не было. Её и заводим:
`supersedes` при сохранении проставляет пометку в ОБЕ стороны, чтение
опровергнутой статьи показывает предупреждение, а выдача подтягивает поправку,
даже если та не прошла по релевантности.
"""

import pytest

from memory_compiler import handlers, storage


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setattr(handlers, "KNOWLEDGE_DIR", tmp_path)
    import memory_compiler.config as cfg
    monkeypatch.setattr(cfg, "KNOWLEDGE_DIR", tmp_path)
    (tmp_path / "demo").mkdir()
    return "demo"


def _write(proj_dir, name, title, body="Старый вывод."):
    p = proj_dir / name
    p.write_text(f"# {title}\n\n**Дата:** 2026-08-26 17:25\n**Проект:** demo\n"
                 f"**Теги:** тест\n\n## Записи\n\n### 2026-08-26 17:25\n{body}\n",
                 encoding="utf-8")
    return p


def test_link_is_written_in_both_directions(proj, tmp_path):
    old = _write(tmp_path / "demo", "staroe.md", "Контур нужен")
    storage.mark_superseded("demo", "staroe.md", "popravka.md", "Барьера платформ нет")
    text = old.read_text(encoding="utf-8")
    assert storage.SUPERSEDED_MARK in text, "у отменённой статьи обязана быть пометка"
    assert "popravka.md" in text, "пометка должна вести на поправку"
    assert "Барьера платформ нет" in text, "и называть её, иначе непонятно, чем отменена"


def test_superseded_article_is_flagged_on_read(proj, tmp_path):
    """Читающий обязан увидеть предупреждение ДО текста, а не после."""
    _write(tmp_path / "demo", "staroe.md", "Контур нужен")
    storage.mark_superseded("demo", "staroe.md", "popravka.md", "Барьера платформ нет")
    assert storage.superseded_by("demo", "staroe.md") == ("popravka.md", "Барьера платформ нет")


def test_fresh_article_is_not_flagged(proj, tmp_path):
    """Позитивный контроль: обычная статья пометки не получает."""
    _write(tmp_path / "demo", "obychnaya.md", "Обычная статья")
    assert storage.superseded_by("demo", "obychnaya.md") is None


def test_marking_twice_does_not_duplicate(proj, tmp_path):
    old = _write(tmp_path / "demo", "staroe.md", "Контур нужен")
    storage.mark_superseded("demo", "staroe.md", "popravka.md", "Поправка")
    storage.mark_superseded("demo", "staroe.md", "popravka.md", "Поправка")
    assert old.read_text(encoding="utf-8").count(storage.SUPERSEDED_MARK) == 1


def test_missing_target_does_not_break_saving(proj):
    """Опечатка в имени файла не должна ронять сохранение поправки: знание важнее
    связи, и терять его из-за неверной ссылки нельзя."""
    assert storage.mark_superseded("demo", "нет-такого.md", "popravka.md", "Поправка") is False


def test_article_cannot_supersede_itself(proj, tmp_path):
    _write(tmp_path / "demo", "sama.md", "Сама себя")
    assert storage.mark_superseded("demo", "sama.md", "sama.md", "Сама себя") is False


@pytest.mark.asyncio
async def test_search_warns_about_superseded_hit(proj, tmp_path):
    """Главное поведение: отменённая статья в выдаче помечена, а не выдана молча."""
    _write(tmp_path / "demo", "staroe.md", "Контур 8.3.27 нужен для выката")
    storage.mark_superseded("demo", "staroe.md", "popravka.md", "Барьера платформ нет")
    results = [{"project": "demo", "file": "staroe.md", "title": "Контур 8.3.27 нужен для выката",
                "score": 95, "preview": "# Контур 8.3.27 нужен\nподнимаем контур"}]
    out = handlers._render_search_results(results, "# Поиск\n", query="контур 8.3.27")
    assert "отменена" in out.lower(), "выдача обязана предупредить об отмене"
    assert "Барьера платформ нет" in out, "и назвать поправку, чтобы было куда идти"


@pytest.mark.asyncio
async def test_correction_is_pulled_in_even_with_low_score(proj, tmp_path):
    """Поправка обязана доехать, даже если по релевантности она ниже отменённой —
    ровно этот случай и произошёл: поправка была в базе, но агент взял верхнюю."""
    _write(tmp_path / "demo", "staroe.md", "Контур нужен")
    _write(tmp_path / "demo", "popravka.md", "Барьера платформ нет", "Контур НЕ нужен.")
    storage.mark_superseded("demo", "staroe.md", "popravka.md", "Барьера платформ нет")
    found = [{"project": "demo", "file": "staroe.md", "title": "Контур нужен",
              "score": 95, "preview": "# Контур нужен\nподнимаем контур"}]
    out = await handlers.attach_corrections(found)
    files = [r["file"] for r in out]
    assert "popravka.md" in files, "поправка не попала в выдачу"
    assert files.index("popravka.md") < files.index("staroe.md"), "поправка должна идти выше"
