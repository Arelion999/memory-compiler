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


# ─── длинный frontmatter (код-ревью v1.78.0, 13.09.2026) ────────────────────
# Реалистичный `contexts:` (ИИ-пересказ секций, v1.28.0): длиннее срезов шапки И в
# строках, И в символах — в базе медиана 13 строк, p90 40, максимум 275.
LONG_FRONT = "---\ncontexts:\n" + "".join(
    f"  - heading: Раздел {n}\n"
    f'    context: "Раздел {n}: что проверяли на контуре 8.3.27, какие параметры трогали '
    f'и почему прежний вывод тогда казался верным."\n'
    for n in range(1, 10)) + "---\n"


def _write_long(proj_dir, name, title):
    p = proj_dir / name
    p.write_text(LONG_FRONT + f"# {title}\n\n**Дата:** 2026-08-26 17:25\n**Проект:** demo\n"
                 f"**Теги:** тест\n\n## Записи\n\n### 2026-08-26 17:25\nСтарый вывод.\n",
                 encoding="utf-8")
    return p


def test_mark_after_long_frontmatter_goes_into_body_header(proj, tmp_path):
    """Шапку искали в первых 12 строках СЫРОГО файла: за длинным frontmatter её не
    находили, и метка вставала строкой 1 — ВНУТРЬ YAML. Он ломался, а потребители,
    читающие тело после frontmatter, метку не видели. Класс «наивный разбор шапки»."""
    yaml = pytest.importorskip("yaml")
    assert len(LONG_FRONT.splitlines()) > 15 and len(LONG_FRONT) > 900, \
        "frontmatter короче срезов шапки — тест не воспроизводит условие бага"
    old = _write_long(tmp_path / "demo", "staroe.md", "Контур нужен")
    assert storage.mark_superseded("demo", "staroe.md", "popravka.md", "Барьера платформ нет")
    text = old.read_text(encoding="utf-8")
    assert text.startswith(LONG_FRONT), "frontmatter обязан остаться нетронутым"
    body = text[len(LONG_FRONT):].splitlines()
    marks = [i for i, line in enumerate(body) if line.startswith(storage.SUPERSEDED_MARK)]
    assert len(marks) == 1, "метка ровно одна"
    assert body.index("**Теги:** тест") < marks[0] < body.index("## Записи"), \
        "метка — в шапке тела: после метаданных, до записей"
    # позитивный контроль: метку видят, YAML разбирается по-настоящему, шапка цела
    assert storage.superseded_by("demo", "staroe.md") == ("popravka.md", "Барьера платформ нет")
    front = storage._parse_frontmatter(text)[0]
    assert front == yaml.safe_load(LONG_FRONT[4:-4]) and len(front["contexts"]) == 9
    assert storage.article_title_tags(text) == ("Контур нужен", "тест")


def test_mark_stays_visible_when_contexts_frontmatter_appears_later(proj, tmp_path):
    """Все четыре метки боевой базы (скан 13.09.2026) стоят у статей БЕЗ frontmatter,
    а еженедельный бэкфилл контекстов дописывает contexts: сверху. Метка уезжает за
    срезы сырого файла — superseded_by читал lines[:14], project_corrections [:900]
    символов, — и поправка молча выпадает из выдачи и стартового контекста."""
    old = _write(tmp_path / "demo", "staroe.md", "Контур нужен")
    storage.mark_superseded("demo", "staroe.md", "popravka.md", "Барьера платформ нет")
    contexts = {f"Раздел {n}": f"Раздел {n}: что проверяли на контуре 8.3.27, какие "
                               f"параметры трогали и почему прежний вывод казался верным."
                for n in range(1, 10)}
    old.write_text(storage.merge_contexts(old.read_text(encoding="utf-8"), contexts),
                   encoding="utf-8")
    assert old.read_text(encoding="utf-8").index(storage.SUPERSEDED_MARK) > 900, \
        "frontmatter короче срезов — тест не воспроизводит условие"
    assert storage.superseded_by("demo", "staroe.md") == ("popravka.md", "Барьера платформ нет")
    assert storage.project_corrections("demo") == [("popravka.md", "Барьера платформ нет")]


def test_article_mentioning_the_mark_can_still_be_superseded(proj, tmp_path):
    """«Уже помечена» решалось вхождением метки В ЛЮБОМ МЕСТЕ файла, а читатели ищут её
    в шапке тела. Статья, где метка просто упомянута (хотя бы описание самой связи), не
    помечалась никогда — и молча: хендлер лишь не рапортовал об отмене."""
    _write(tmp_path / "demo", "pro_metku.md", "Как устроена связь «отменяет»",
           body=f"Отменённая статья получает строку `{storage.SUPERSEDED_MARK} <файл>`.")
    assert storage.superseded_by("demo", "pro_metku.md") is None
    assert storage.mark_superseded("demo", "pro_metku.md", "popravka.md", "Поправка"), \
        "упоминание метки в тексте — ещё не метка"
    assert storage.superseded_by("demo", "pro_metku.md") == ("popravka.md", "Поправка")
