"""Жизненный цикл открытого вопроса: частичное закрытие и поправки рядом (v1.70.0).

Две находки 26–27.08.2026.

A. СКЛЕЕННЫЙ ВОПРОС НЕВОЗМОЖНО ЗАКРЫТЬ ЧАСТИЧНО. Замер по базе: открытых
   вопросов 67, из них 35 (52%) содержат несколько тем сразу. Живой случай — в
   одном вопросе по niks_ut соседствуют опровергнутый барьер платформ и два
   живых пункта (вынести COM-строку, второй шаг обновления). Закрыть целиком
   значит похоронить живое, оставить — транслировать опровергнутое.

B. СВЯЗЬ «ОТМЕНЯЕТ» НЕ ДОТЯГИВАЛАСЬ ДО ВОПРОСОВ. v1.69.0 научил выдачу
   предупреждать об отменённых СТАТЬЯХ, но `open_questions` и подсказка первого
   обращения продолжали выдавать тот же опровергнутый вывод — проверено живьём
   сразу после релиза.
"""

import pytest

from memory_compiler import storage

HEAD = "# %s\n\n**Дата:** 2026-08-26 10:00\n**Проект:** demo\n**Теги:** тест\n"


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "KNOWLEDGE_DIR", tmp_path)
    import memory_compiler.config as cfg
    monkeypatch.setattr(cfg, "KNOWLEDGE_DIR", tmp_path)
    (tmp_path / "demo").mkdir()
    return "demo"


# ── A. частичное закрытие ───────────────────────────────────────────────────

def test_remainder_keeps_the_live_part(proj):
    """Закрыли решённую часть — живая осталась открытой, отдельным вопросом."""
    storage.add_question(proj, "Нужен контур 8.3.27 для выката. Плюс вынести COM-строку из модуля формы.")
    n = storage.close_questions(proj, "контур 8.3.27",
                                remainder="Вынести COM-строку подключения к БП из модуля формы")
    assert n == 1
    open_now = storage.open_questions_list(proj)
    assert len(open_now) == 1
    assert "COM-строку" in open_now[0]["text"]
    assert "контур" not in open_now[0]["text"], "закрытая часть не должна тянуться дальше"


def test_closed_part_stays_in_history(proj):
    storage.add_question(proj, "Первый пункт. Второй пункт.")
    storage.close_questions(proj, "Первый пункт", remainder="Второй пункт")
    closed = [q for q in storage.parse_questions(proj) if q["status"] == "closed"]
    assert closed and "Первый пункт" in closed[0]["text"], "история закрытия обязана остаться"


def test_close_without_remainder_works_as_before(proj):
    """Позитивный контроль: обычное закрытие не изменилось."""
    storage.add_question(proj, "Единственная тема вопроса")
    assert storage.close_questions(proj, "Единственная") == 1
    assert storage.open_questions_list(proj) == []


def test_remainder_is_ignored_when_nothing_matched(proj):
    """Промах по тексту не должен заводить остаток: иначе появится вопрос-двойник."""
    storage.add_question(proj, "Живой вопрос")
    assert storage.close_questions(proj, "такого нет", remainder="Остаток") == 0
    assert len(storage.open_questions_list(proj)) == 1


# ── B. поправки проекта рядом с вопросами ───────────────────────────────────
# ⚠️ ПЕРВАЯ РЕАЛИЗАЦИЯ ОТВЕРГНУТА ЗАМЕРОМ. Пробовал помечать вопрос, чьи слова
# пересекаются с заголовком отменённой статьи: на живых данных niks_ut пометку
# получали 4 вопроса из 29, и два из них ложно («Добить шесть объектов…»,
# «Добить ТЧ РеализацияТоваровУслуг…»). Причина та же, что у «переформулировок»
# в v1.66.0: в проекте все вопросы про одну задачу, словарь общий, лексической
# границы не существует. Отбор по РЕДКИМ словам (IDF по вопросам проекта) дал
# ровно тот же результат — 4 пометки, 2 ложные.
#
# Вместо угадывания отдаём НАБЛЮДАЕМЫЙ ФАКТ: какие поправки в проекте есть.
# Сопоставляет читающий. Замер: поправки существуют в 1 проекте из 47, то есть
# в остальных сигнала не будет вовсе.

def test_corrections_of_the_project_are_listed(proj, tmp_path):
    (tmp_path / "demo" / "staroe.md").write_text(HEAD % "Старый вывод", encoding="utf-8")
    storage.mark_superseded("demo", "staroe.md", "popravka.md", "Барьера платформ нет")
    assert storage.project_corrections("demo") == [("popravka.md", "Барьера платформ нет")]


def test_no_corrections_no_signal(proj, tmp_path):
    """Позитивный контроль: где поправок нет, там и сигнала нет."""
    (tmp_path / "demo" / "obychnaya.md").write_text(HEAD % "Обычная", encoding="utf-8")
    assert storage.project_corrections("demo") == []


def test_same_correction_is_listed_once(proj, tmp_path):
    for name in ("a.md", "b.md"):
        (tmp_path / "demo" / name).write_text(HEAD % name, encoding="utf-8")
        storage.mark_superseded("demo", name, "popravka.md", "Одна поправка")
    assert len(storage.project_corrections("demo")) == 1, "поправка, отменившая двоих, — одна строка"
