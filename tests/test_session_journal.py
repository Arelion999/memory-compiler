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


def test_old_format_is_read_whole_before_any_new_write(proj):
    """Старый файл обязан читаться ЦЕЛИКОМ ещё до первой записи в новом формате.

    Разбор шёл по любому «## », а старый формат САМ состоит из разделов
    «## Что сделано» / «## Решения», поэтому первым блоком становилась строка
    «**Дата:** …» — 26 символов вместо всей сессии. Ветка «старый формат целиком»
    была недостижима: условие проверяло наличие «## », а он там есть всегда.
    Замер 2026-08-26 на боевой базе: 38 проектов из 41, у всех в стартовом
    контексте вместо содержания стояла одна строка с датой.
    """
    storage._session_path(proj).write_text(
        "# Сессия: demo\n\n**Дата:** 2026-08-20 10:00\n\n"
        "## Что сделано\nразобран перелив прода в дев\n\n"
        "## Решения\nрегламентные задания в копии запрещены\n\n"
        "## Открытые вопросы\nне проверен рестарт после обновления\n",
        encoding="utf-8")
    latest = storage.latest_session(proj)
    assert "разобран перелив прода в дев" in latest, "содержание сессии потеряно"
    assert "регламентные задания в копии запрещены" in latest
    assert "2026-08-20 10:00" in latest, "дата старой сессии должна остаться"


def test_old_format_becomes_exactly_one_block(proj):
    """При миграции старый файл — ОДИН блок, а не четыре псевдо-сессии.

    Иначе разделы старого файла занимали бы 4 слота из MAX_SESSIONS и вытесняли
    настоящие сессии.
    """
    storage._session_path(proj).write_text(
        "# Сессия: demo\n\n**Дата:** 2026-08-20 10:00\n\n"
        "## Что сделано\nстарый формат\n\n## Решения\nстарое решение\n\n"
        "## Открытые вопросы\nстарый вопрос\n", encoding="utf-8")
    storage.append_session(proj, "новая запись")
    blocks = storage._split_session_blocks(storage._session_path(proj).read_text(encoding="utf-8"))
    assert len(blocks) == 2, "ожидались ровно два блока: новая сессия и старая целиком"
    assert "новая запись" in blocks[0]
    assert all(w in blocks[1] for w in ("старый формат", "старое решение", "старый вопрос"))


def test_new_format_split_is_not_broken_by_inner_headings(proj):
    """Позитивный контроль: блоки нового формата по-прежнему разделяются, а
    подзаголовки ВНУТРИ блока новой сессией не считаются."""
    storage.append_session(proj, "первая")
    storage.append_session(proj, "вторая\n\n## Детали\nподраздел внутри блока")
    blocks = storage._split_session_blocks(storage._session_path(proj).read_text(encoding="utf-8"))
    assert len(blocks) == 2, "подзаголовок внутри блока не должен плодить сессии"
    assert "вторая" in blocks[0] and "подраздел внутри блока" in blocks[0]
    assert "первая" in blocks[1]
