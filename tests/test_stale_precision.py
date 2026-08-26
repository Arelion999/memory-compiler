"""Точность проверки сроков и её показ при старте задачи (v1.61.0).

Замер 2026-08-26 на боевой базе: голое «до» перед датой давало 22 записи «уже
истекло», настоящими сроками из них были единицы. Русский предлог многозначен,
а даты в статьях чаще исторические:

    «пометки поставлены ДО 04.02.2026»      — «до» = «раньше чем»
    «закрытие отложено до 01.07.2026»       — прошедшее событие
    «3.1 мертва (поддержка до 01.03.2023)»  — чужой продукт, справка

После ужесточения: 0 ложных «истекло» и 4 предупреждения, все четыре истинные
(два SSL-сертификата, подписка ИТС, сгорающие стартмани).
"""

import time
from datetime import date, timedelta

import pytest

from memory_compiler import handlers


@pytest.fixture
def base(tmp_path, monkeypatch):
    import memory_compiler.config as cfg
    from memory_compiler import storage
    # KNOWLEDGE_DIR импортирован ПО ЗНАЧЕНИЮ в каждый модуль — патчить надо всюду,
    # иначе project_dir() из storage уводит скан в боевой каталог.
    monkeypatch.setattr(cfg, "KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setattr(handlers, "KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setattr(storage, "KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setattr(cfg, "PROJECTS", ["demo"])
    (tmp_path / "demo").mkdir()
    handlers._STALE_CACHE.clear()

    def add(name, body):
        (tmp_path / "demo" / name).write_text(
            "# %s\n\n**Дата:** 2026-08-01 10:00\n\n%s\n" % (name[:-3], body), encoding="utf-8")
    return add


def _soon(days=10):
    return (date.today() + timedelta(days=days)).strftime("%d.%m.%Y")


def _scan(project="demo", warn=30):
    return handlers._scan_stale(project, warn)


def test_real_deadline_is_caught(base):
    base("ssl.md", "Сертификат wildcard, действителен до %s, продлевать заранее." % _soon())
    res = _scan()
    assert len(res["expiring"]) == 1, res
    assert "ssl" in res["expiring"][0]["path"]


def test_before_meaning_is_not_a_deadline(base):
    """«поставлены ДО даты» — это «раньше чем», а не срок."""
    base("marks.md", "Пометки удаления поставлены ДО %s, дальше их не ставили." % _soon())
    assert _scan()["expiring"] == []


def test_past_event_is_not_a_deadline(base):
    base("okdesk.md", "Работа выполнена, но закрытие отложено до %s по правилу биллинга." % _soon())
    assert _scan()["expiring"] == []


def test_third_party_support_note_is_not_a_deadline(base):
    base("versions.md", "Редакция 3.1 мертва (поддержка до 01.03.2023), 3.2 заморожена.")
    assert _scan()["expired"] == [], "чужая историческая справка попала в сроки"


def test_link_to_another_article_does_not_duplicate_its_deadline(base):
    """Дата из раздела «См. также» — чужой факт, он посчитан там, где живёт."""
    base("decision.md", "## См. также\n- [Подписка сгорает %s](../general/подписка.md)" % _soon())
    assert _scan()["expiring"] == []


def test_long_expired_is_history_not_a_task(base):
    old = (date.today() - timedelta(days=200)).strftime("%d.%m.%Y")
    base("old.md", "Лицензия действительна до %s." % old)
    assert _scan()["expired"] == [], "протухшее полгода назад — история, а не задача"


def test_recently_expired_is_reported(base):
    recent = (date.today() - timedelta(days=10)).strftime("%d.%m.%Y")
    base("recent.md", "Домен оплачен до %s." % recent)
    assert len(_scan()["expired"]) == 1


def test_summary_is_cached(base):
    base("ssl.md", "Сертификат действителен до %s." % _soon())
    t0 = time.perf_counter()
    first = handlers.stale_summary("demo", 30, 3)
    cold = time.perf_counter() - t0
    t0 = time.perf_counter()
    second = handlers.stale_summary("demo", 30, 3)
    warm = time.perf_counter() - t0
    assert first == second
    assert warm < cold or warm < 0.005, "кэш не сработал: %.4f против %.4f" % (warm, cold)


def test_summary_sorted_by_urgency_and_limited(base):
    for i, days in enumerate((25, 3, 12)):
        base("d%d.md" % i, "Лицензия действительна до %s." % _soon(days))
    rows = handlers.stale_summary("demo", 30, 2)
    assert len(rows) == 2, "лимит не применён"
    assert rows[0]["days_left"] <= rows[1]["days_left"], "срочное должно быть выше"


@pytest.mark.asyncio
async def test_start_task_shows_deadlines(base, monkeypatch):
    """Ради этого всё и делалось: проверку показывают, а не ждут её вызова."""
    base("ssl.md", "Сертификат домена действителен до %s." % _soon(5))
    monkeypatch.setattr(handlers, "_whoosh_async", lambda *a, **k: _empty())
    res = await handlers.start_task("проверить сертификат", "demo")
    text = res[0].text
    assert "Сроки на исходе" in text, text[:400]
    assert "осталось" in text


@pytest.mark.asyncio
async def test_start_task_silent_without_deadlines(base, monkeypatch):
    """Пустой блок не добавляем: шум обесценивает предупреждение."""
    base("plain.md", "Обычная статья без сроков.")
    monkeypatch.setattr(handlers, "_whoosh_async", lambda *a, **k: _empty())
    res = await handlers.start_task("обычная задача", "demo")
    assert "Сроки на исходе" not in res[0].text


async def _empty():
    return []
