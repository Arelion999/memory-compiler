"""Факты прошлых сессий подключены к стартовому контексту (v1.62.0).

Диагностика 2026-08-26: `_reflections.md` писался на каждом finish_task —
103 КБ, 39 проектов, 632 факта — и не читался НИКЕМ. Первым решением было
перестать писать, но выборка это опровергла: годного содержимого 96%
(«сертификат Let's Encrypt на app.dymok27.ru», «ЧекККМ_проведение: 2,2–5,0с →
10,47с»). Механизм работал, выход был в никуда.
"""

import pytest

from memory_compiler import handlers, storage


@pytest.fixture
def proj(tmp_path, monkeypatch):
    import memory_compiler.config as cfg
    monkeypatch.setattr(storage, "KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setattr(cfg, "KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setattr(handlers, "KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setattr(cfg, "PROJECTS", ["demo"])
    (tmp_path / "demo").mkdir()
    return "demo"


def _write(project, facts):
    storage.append_reflections(project, facts)


def test_relevant_fact_is_returned(proj):
    _write(proj, ["Выпущен сертификат Let's Encrypt на app.example.ru через syno-letsencrypt",
                  "Перечисление СтатусыЗаказов: НеСогласован, КОтгрузке, Закрыт"])
    rows = storage.relevant_reflections(proj, {"сертификат", "летсencrypt"}, 4)
    assert len(rows) == 1 and "Encrypt" in rows[0]


def test_unrelated_facts_are_not_returned(proj):
    _write(proj, ["Перечисление СтатусыЗаказов: НеСогласован, КОтгрузке, Закрыт"])
    assert storage.relevant_reflections(proj, {"сертификат", "домен"}, 4) == []


def test_no_topic_words_means_no_facts(proj):
    """Без темы не вываливаем всё подряд — это шум, а не справка."""
    _write(proj, ["Выпущен сертификат Let's Encrypt на app.example.ru через syno-letsencrypt"])
    assert storage.relevant_reflections(proj, set(), 4) == []


def test_junk_is_filtered(proj):
    """Отсев минимальный: только слишком короткое и служебное «proj: Имя (5)»."""
    _write(proj, ["короткий", "antilopa: Антилопа (5)",
                  "Полноценный факт про сертификат домена и его продление"])
    rows = storage.relevant_reflections(proj, {"сертификат", "антилопа", "короткий"}, 5)
    assert len(rows) == 1, rows
    assert "Полноценный" in rows[0]


def test_valuable_short_looking_facts_survive(proj):
    """Жадный фильтр по последнему символу и кавычкам выбрасывал ценное — так нельзя."""
    _write(proj, ['"Загрузка данных из внешнего источника" — типовая обработка, форма с деревом полей',
                  "УЗБЕКИСТАН 46.8.194.10 стал полноценным узлом, доступ по ключу,"])
    rows = storage.relevant_reflections(proj, {"загрузка", "узбекистан"}, 5)
    assert len(rows) == 2, rows


def test_limit_applies(proj):
    _write(proj, ["Факт номер %d про сертификат домена и продление ключа" % i for i in range(6)])
    assert len(storage.relevant_reflections(proj, {"сертификат"}, 3)) == 3


def test_missing_file_is_survivable(proj):
    assert storage.relevant_reflections(proj, {"что-нибудь"}, 4) == []


def test_more_overlap_ranks_higher(proj):
    _write(proj, ["Сертификат домена выпущен и лежит в каталоге архива",
                  "Сертификат домена выпущен, продление настроено через cron еженедельно"])
    rows = storage.relevant_reflections(proj, {"сертификат", "продление", "cron"}, 2)
    assert "продление" in rows[0].lower(), rows


@pytest.mark.asyncio
async def test_start_task_shows_facts(proj, monkeypatch):
    _write(proj, ["Выпущен сертификат Let's Encrypt на app.example.ru через syno-letsencrypt"])
    monkeypatch.setattr(handlers, "_whoosh_async", lambda *a, **k: _empty())
    res = await handlers.start_task("продлить сертификат домена", proj)
    text = res[0].text
    assert "Факты прошлых сессий" in text, text[:300]
    assert "Encrypt" in text


@pytest.mark.asyncio
async def test_start_task_silent_without_matching_facts(proj, monkeypatch):
    _write(proj, ["Перечисление СтатусыЗаказов: НеСогласован, КОтгрузке, Закрыт"])
    monkeypatch.setattr(handlers, "_whoosh_async", lambda *a, **k: _empty())
    res = await handlers.start_task("настроить резервное копирование", proj)
    assert "Факты прошлых сессий" not in res[0].text


async def _empty():
    return []
