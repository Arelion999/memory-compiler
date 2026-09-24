"""start_task: статья один раз, без оценок, строка отрывка до 240 (v1.91.0).

Замер 24.09.2026 по транскриптам: в 15 выдачах из 51 одна статья повторялась в
«Найдено», «Связанных действиях» и «Решениях по теме»; заголовок в «Найдено»
повторялся первой строкой отрывка — 2,8% объёма. Замер @link28rus: оценка hybrid
между запросами не откалибрована (статья не по теме пришла с 98,4), строка
отрывка у статей без переносов — целый абзац (p90 335, максимум 582 символа).
"""
import pytest

from memory_compiler import handlers, storage


@pytest.fixture
def proj(tmp_path, monkeypatch):
    import memory_compiler.config as cfg
    from memory_compiler import handlers_sessions
    monkeypatch.setattr(storage, "KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setattr(cfg, "KNOWLEDGE_DIR", tmp_path)
    # start_task живёт в handlers_sessions и читает СВОЙ KNOWLEDGE_DIR (v1.86.0).
    monkeypatch.setattr(handlers_sessions, "KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setattr(cfg, "PROJECTS", ["demo"])
    (tmp_path / "demo").mkdir()
    return tmp_path / "demo"


def _decision(pdir, name, title, choice):
    (pdir / name).write_text(
        f"# {title}\n\n**Дата:** 2026-09-01 10:00\n**Тип:** decision\n**Теги:** выбор\n\n"
        f"## Решение\n{choice}\n", encoding="utf-8")


def _hit(name, title, score=90):
    return {"project": "demo", "file": name, "title": title, "score": score,
            "preview": f"# {title}\nстрока тела 1\nстрока тела 2\nстрока тела 3\nстрока тела 4"}


def _fake(monkeypatch, hits):
    async def fake(query, project="all", limit=20):
        return [dict(h) for h in hits]
    monkeypatch.setattr(handlers, "_whoosh_async", fake)


@pytest.mark.asyncio
async def test_decision_found_is_not_repeated(proj, monkeypatch):
    _decision(proj, "decision_broker.md", "Выбор брокера сообщений", "Mosquitto")
    _fake(monkeypatch, [_hit("decision_broker.md", "Выбор брокера сообщений")])
    text = (await handlers.start_task("выбор брокера сообщений", "demo"))[0].text
    assert "## Найдено" in text
    assert "## Решения по теме" not in text, "решение из «Найдено» повторено в «Решениях по теме»"


@pytest.mark.asyncio
async def test_decision_outside_found_is_still_shown(proj, monkeypatch):
    """Позитивный контроль: сверка не выбрасывает решения, которых в «Найдено» нет."""
    for i in range(3):
        (proj / f"note{i}.md").write_text(f"# Заметка {i}\n\n**Теги:** выбор\n\nтекст\n",
                                          encoding="utf-8")
    _decision(proj, "decision_broker.md", "Выбор брокера сообщений", "Mosquitto")
    hits = [_hit(f"note{i}.md", f"Заметка {i}") for i in range(3)]
    hits.append(_hit("decision_broker.md", "Выбор брокера сообщений", 80))
    _fake(monkeypatch, hits)
    text = (await handlers.start_task("выбор брокера сообщений", "demo"))[0].text
    assert "## Решения по теме" in text and "Mosquitto" in text


@pytest.mark.asyncio
async def test_activity_line_about_found_article_is_skipped(proj, monkeypatch):
    (proj / "_active_context.md").write_text(
        "- [2026-09-20 10:00] **Выбор брокера сообщений** — сохранено решение про брокер\n"
        "- [2026-09-21 11:00] **Настройка брокера на NAS** — поднят контейнер брокера\n",
        encoding="utf-8")
    _decision(proj, "decision_broker.md", "Выбор брокера сообщений", "Mosquitto")
    _fake(monkeypatch, [_hit("decision_broker.md", "Выбор брокера сообщений")])
    text = (await handlers.start_task("выбор брокера сообщений", "demo"))[0].text
    assert "сохранено решение про брокер" not in text, "строка активности повторила статью из «Найдено»"
    assert "Настройка брокера на NAS" in text, "другая строка активности обязана остаться"


@pytest.mark.asyncio
async def test_found_preview_does_not_repeat_the_title(proj, monkeypatch):
    _decision(proj, "decision_broker.md", "Выбор брокера сообщений", "Mosquitto")
    _fake(monkeypatch, [_hit("decision_broker.md", "Выбор брокера сообщений")])
    text = (await handlers.start_task("выбор брокера сообщений", "demo"))[0].text
    assert "\n# Выбор брокера сообщений\n" not in text
    assert ("### [demo] Выбор брокера сообщений\n"
            "строка тела 1\nстрока тела 2\nстрока тела 3\nстрока тела 4") in text


@pytest.mark.asyncio
async def test_found_header_carries_no_score(proj, monkeypatch):
    """Оценка между запросами не откалибрована: число в заголовке находки вводит в
    заблуждение и занимает место. Порядок находок и так идёт по ней."""
    _decision(proj, "decision_broker.md", "Выбор брокера сообщений", "Mosquitto")
    _fake(monkeypatch, [_hit("decision_broker.md", "Выбор брокера сообщений")])
    text = (await handlers.start_task("выбор брокера сообщений", "demo"))[0].text
    assert "(hybrid:" not in text and "(score:" not in text


@pytest.mark.asyncio
async def test_long_preview_line_is_clipped(proj, monkeypatch):
    _decision(proj, "decision_broker.md", "Выбор брокера сообщений", "Mosquitto")
    hit = _hit("decision_broker.md", "Выбор брокера сообщений")
    hit["preview"] = "# Выбор брокера сообщений\n" + "а" * 600 + "\nкороткая строка"
    _fake(monkeypatch, [hit])
    text = (await handlers.start_task("выбор брокера сообщений", "demo"))[0].text
    assert "а" * 240 + "…\nкороткая строка" in text, "строка отрывка длиннее 240 символов"
    assert "а" * 241 not in text
