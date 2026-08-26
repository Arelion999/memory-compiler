"""Журнал сессий и накопительные открытые вопросы (v1.58.0).

До этого `_session.md` перезаписывался целиком: замер 2026-08-26 по аудиту
(7965 вызовов, 502 сессии) показал 948 записанных открытых вопросов и 916
затёртых следующей сессией того же проекта — 96%. В статью они не попадали
вообще, то есть исчезали бесследно.
"""

import pytest

from memory_compiler import storage


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "KNOWLEDGE_DIR", tmp_path)
    import memory_compiler.config as cfg
    monkeypatch.setattr(cfg, "KNOWLEDGE_DIR", tmp_path)
    (tmp_path / "demo").mkdir()
    return "demo"


# ── журнал сессий ───────────────────────────────────────────────────────────

def test_previous_session_survives_new_one(proj):
    storage.append_session(proj, "первая сессия", open_questions="висит вопрос А")
    storage.append_session(proj, "вторая сессия", open_questions="висит вопрос Б")
    text = (storage._session_path(proj)).read_text(encoding="utf-8")
    assert "первая сессия" in text, "прошлая сессия затёрта — ровно тот баг, что чиним"
    assert "вторая сессия" in text


def test_latest_session_returns_newest_block_only(proj):
    storage.append_session(proj, "старое")
    storage.append_session(proj, "новое")
    latest = storage.latest_session(proj)
    assert "новое" in latest and "старое" not in latest


def test_old_single_session_format_is_migrated_not_lost(proj):
    """Файл прежнего формата обязан стать первым блоком журнала, а не исчезнуть."""
    (storage._session_path(proj)).write_text(
        "# Сессия: demo\n\n**Дата:** 2026-08-20 10:00\n\n## Что сделано\n"
        "старый формат\n\n## Открытые вопросы\nстарый вопрос\n", encoding="utf-8")
    storage.append_session(proj, "новая запись")
    text = (storage._session_path(proj)).read_text(encoding="utf-8")
    assert "старый формат" in text and "старый вопрос" in text
    assert "новая запись" in text
    assert storage.latest_session(proj).count("Что сделано") == 1


def test_journal_is_capped(proj):
    for i in range(storage.MAX_SESSIONS + 5):
        storage.append_session(proj, "сессия %d" % i)
    text = (storage._session_path(proj)).read_text(encoding="utf-8")
    assert text.count("**Что сделано:**") == storage.MAX_SESSIONS
    assert "сессия %d" % (storage.MAX_SESSIONS + 4) in text, "новейшая должна остаться"
    assert "сессия 0" not in text, "самая старая должна вытесняться"


# ── открытые вопросы ────────────────────────────────────────────────────────

def test_question_added_and_listed(proj):
    assert storage.add_question(proj, "Почему падает деплой?") is True
    items = storage.open_questions_list(proj)
    assert len(items) == 1 and items[0]["status"] == "open"
    assert "деплой" in items[0]["text"]


def test_same_question_is_not_duplicated(proj):
    storage.add_question(proj, "Что с DNS?")
    assert storage.add_question(proj, "что с   dns ?") is False, "дубль вопроса заведён"
    assert len(storage.open_questions_list(proj)) == 1


def test_closed_question_leaves_list_but_stays_in_file(proj):
    storage.add_question(proj, "Ждём ответа клиента по тарифу")
    storage.add_question(proj, "Не проверен бэкап базы")
    assert storage.close_questions(proj, "тариф") == 1
    left = storage.open_questions_list(proj)
    assert len(left) == 1 and "бэкап" in left[0]["text"]
    text = (storage._questions_path(proj)).read_text(encoding="utf-8")
    assert "тарифу" in text and "closed" in text, "закрытый вопрос должен оставаться историей"


def test_close_without_match_changes_nothing(proj):
    storage.add_question(proj, "Единственный вопрос")
    assert storage.close_questions(proj, "ничего похожего") == 0
    assert len(storage.open_questions_list(proj)) == 1


def test_reopened_after_close_is_allowed(proj):
    """Закрытый вопрос можно задать снова — дедуп смотрит только на открытые."""
    storage.add_question(proj, "Причина обрывов")
    storage.close_questions(proj, "обрывов")
    assert storage.add_question(proj, "Причина обрывов") is True
    assert len(storage.open_questions_list(proj)) == 1


def test_empty_question_is_ignored(proj):
    assert storage.add_question(proj, "   ") is False
    assert storage.open_questions_list(proj) == []


def test_limit_applies_to_open_list(proj):
    for i in range(7):
        storage.add_question(proj, "вопрос номер %d" % i)
    assert len(storage.open_questions_list(proj, limit=3)) == 3
    assert len(storage.open_questions_list(proj)) == 7


# ── одноразовый перенос вопросов из прежних файлов сессий ───────────────────

def test_seed_skips_reports_that_there_are_no_questions():
    """«Хвостов нет» — это отсутствие вопроса, а не вопрос."""
    from memory_compiler.maintenance import _is_real_question
    assert _is_real_question("Почему падает деплой на втором шаге?") is True
    assert _is_real_question("Хвостов по задаче нет, владелец доволен.") is False
    assert _is_real_question("Нет открытых вопросов") is False
    assert _is_real_question("—") is False
    assert _is_real_question("") is False


def test_seed_migrates_questions_and_is_idempotent(proj, monkeypatch):
    import memory_compiler.config as cfg
    from memory_compiler import maintenance
    monkeypatch.setattr(cfg, "PROJECTS", [proj])
    (storage._session_path(proj)).write_text(
        "# Сессия: demo\n\n**Дата:** 2026-08-20 10:00\n\n## Что сделано\n"
        "работа\n\n## Открытые вопросы\nНе проверен бэкап базы клиента\n",
        encoding="utf-8")

    maintenance.seed_questions_from_sessions(dry_run=True)
    assert storage.open_questions_list(proj) == [], "dry-run не должен писать"

    assert maintenance.seed_questions_from_sessions(dry_run=False) == 1
    items = storage.open_questions_list(proj)
    assert len(items) == 1 and "бэкап" in items[0]["text"]

    maintenance.seed_questions_from_sessions(dry_run=False)
    assert len(storage.open_questions_list(proj)) == 1, "повторный проход завёл дубль"
