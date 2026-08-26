"""Служебные файлы движка вне поискового индекса + план работ не заводится вопросом (v1.63.0).

Оба наблюдения найдены по ходу сессии 2026-08-26:

1. `_reflections.md` попал в выдачу с оценкой 88.1 рядом со статьёй, содержащей
   тот же текст, — и это при том, что start_task показывает факты отдельным
   блоком. То же верно для `_session.md`, `_questions.md`, `_log.md`.
2. В open_questions за одну сессию легли четыре записи — один и тот же перечень
   оставшихся работ в разных редакциях.
"""

import pytest

from memory_compiler import search, storage


# ── служебные файлы вне индекса ─────────────────────────────────────────────

def test_service_files_are_listed_explicitly():
    for name in ("_session.md", "_reflections.md", "_questions.md",
                 "_active_context.md", "_log.md", "_compact_history.md"):
        assert name in search.SERVICE_FILES, name


def test_ordinary_articles_starting_with_underscore_are_not_service():
    """⚠️ Маска `_*` выбросила бы обычные статьи — в базе такие есть."""
    for name in ("_гайд_по_быстрой_прокачке_crew_skills.md",
                 "_как_выполнить_текстовый_фрагмент_кода_в_1с.md",
                 "_12_f10_admin_tooling_команды.md"):
        assert name not in search.SERVICE_FILES, name


def test_service_files_skipped_by_whoosh_index(tmp_path, monkeypatch):
    import memory_compiler.config as cfg
    # KNOWLEDGE_DIR / INDEX_DIR / PROJECTS импортированы в search ПО ЗНАЧЕНИЮ —
    # патчить надо у него, патч config сюда не доедет.
    monkeypatch.setattr(cfg, "KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setattr(cfg, "PROJECTS", ["demo"])
    monkeypatch.setattr(search, "KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setattr(search, "PROJECTS", ["demo"])
    monkeypatch.setattr(search, "INDEX_DIR", tmp_path / ".whoosh_index")
    monkeypatch.setattr(search, "_index", None, raising=False)
    proj = tmp_path / "demo"
    proj.mkdir()
    (proj / "статья.md").write_text("# Статья\n\nПро сертификаты домена.", encoding="utf-8")
    (proj / "_session.md").write_text("# Сессии\n\nПро сертификаты домена.", encoding="utf-8")
    (proj / "_подчёркивание_но_статья.md").write_text("# Статья\n\nПро домен.", encoding="utf-8")

    search.rebuild_index()
    ix = search.get_index()
    with ix.searcher() as s:
        paths = {d["path"] for d in s.documents()}
    assert "demo/статья.md" in paths
    assert "demo/_подчёркивание_но_статья.md" in paths, "обычная статья выброшена маской"
    assert "demo/_session.md" not in paths, "служебный файл попал в индекс"


# ── план работ ≠ открытый вопрос ────────────────────────────────────────────

PLANS = [
    "Дальше по плану диагностики: (3) живое зеркало git базы; (4) stale_facts внутрь start_task; (5) судьба reflections.",
    "Следующие работы из диагностики: (2) вычистить git базы; (3) внешнее зеркало; (4) stale_facts; (5) reflections.",
    "Не сделано, ждёт решения: (1) вынести эмбеддинг в фон; (2) вычистить git; (3) зеркало; (4) stale_facts.",
    "Из аудита контекста сессии не сделаны: (B) бюджет контекста; (C) session_note; (D) stale_facts; (E) reflections.",
    "Предложения не реализованы, ждут решения владельца: (A) журнал сессий; (B) бюджет; (C) session_note.",
]
QUESTIONS = [
    "Владелец решает: (1) запускать ли admin-скрипт с -IncludeVBS (скорость против безопасности), (2) оставить как есть.",
    "Чей магазин за IP 92.37.133.0 — он в одной подсети с Комсомольском и второй день лидер по обрывам.",
    "Провайдер публичной подсети ещё не уведомлён. Не решено, поднимать ли временный обход через домашний канал.",
]


@pytest.mark.parametrize("text", PLANS)
def test_plan_list_is_recognised(text):
    assert storage.is_plan_list(text) is True, text[:60]


@pytest.mark.parametrize("text", QUESTIONS)
def test_real_question_is_not_a_plan(text):
    """Позитивный контроль: вопрос с вариантами остаётся вопросом."""
    assert storage.is_plan_list(text) is False, text[:60]


def test_plan_is_not_added_as_question(tmp_path, monkeypatch):
    import memory_compiler.config as cfg
    monkeypatch.setattr(storage, "KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setattr(cfg, "KNOWLEDGE_DIR", tmp_path)
    (tmp_path / "demo").mkdir()

    assert storage.add_question("demo", PLANS[0]) is False
    assert storage.open_questions_list("demo") == []

    assert storage.add_question("demo", QUESTIONS[1]) is True
    assert len(storage.open_questions_list("demo")) == 1
