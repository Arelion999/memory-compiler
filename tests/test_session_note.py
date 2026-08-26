"""Заметка по ходу сессии (v1.65.0).

Замер 2026-08-26 по аудиту (7965 вызовов, 502 сессии): дополнить контекст ПОСРЕДИ
работы было нечем — единственный путь `save_session` пересобирает сводку сессии
целиком и потому зовётся в 10% сессий. Работа после последней загрузки контекста:
медиана 25 минут, p90 101. Всё, что происходило в эти минуты, для параллельной
сессии и для следующего старта не существовало.

`session_note` — дешёвая противоположность: одна строка дописывается в текущий
блок журнала, сводка не пересобирается, git не трогается.
"""

from datetime import datetime

import pytest

from memory_compiler import storage


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "KNOWLEDGE_DIR", tmp_path)
    import memory_compiler.config as cfg
    monkeypatch.setattr(cfg, "KNOWLEDGE_DIR", tmp_path)
    (tmp_path / "demo").mkdir()
    return "demo"


def test_note_creates_running_block(proj):
    storage.append_note(proj, "перелив прода в дев упёрся в версию SQL")
    latest = storage.latest_session(proj)
    assert "перелив прода в дев упёрся в версию SQL" in latest
    assert storage.RUNNING_MARK in latest, "блок незакрытой сессии должен быть помечен"


def test_second_note_joins_the_same_block(proj):
    storage.append_note(proj, "первая заметка")
    storage.append_note(proj, "вторая заметка")
    blocks = storage._split_session_blocks(storage._session_path(proj).read_text(encoding="utf-8"))
    assert len(blocks) == 1, "заметки одной сессии не должны плодить блоки журнала"
    assert "первая заметка" in blocks[0] and "вторая заметка" in blocks[0]


def test_repeated_note_is_not_duplicated(proj):
    storage.append_note(proj, "одно и то же")
    storage.append_note(proj, "одно и то же")
    assert storage.latest_session(proj).count("одно и то же") == 1


def test_finish_absorbs_notes_into_its_block(proj):
    """Итог сессии обязан вобрать заметки, а не встать рядом отдельной сессией."""
    storage.append_note(proj, "по дороге выяснилось про регламентные задания")
    storage.append_session(proj, "перелив прода в дев выполнен")
    blocks = storage._split_session_blocks(storage._session_path(proj).read_text(encoding="utf-8"))
    assert len(blocks) == 1, "заметки и итог — одна и та же сессия"
    assert "по дороге выяснилось про регламентные задания" in blocks[0]
    assert "перелив прода в дев выполнен" in blocks[0]
    assert storage.RUNNING_MARK not in blocks[0], "закрытая сессия не остаётся «в работе»"


def test_notes_do_not_erase_previous_sessions(proj):
    storage.append_session(proj, "вчерашняя сессия")
    storage.append_note(proj, "сегодняшняя заметка")
    text = storage._session_path(proj).read_text(encoding="utf-8")
    assert "вчерашняя сессия" in text
    blocks = storage._split_session_blocks(text)
    assert len(blocks) == 2 and "сегодняшняя заметка" in blocks[0]


def test_note_does_not_reopen_a_finished_session(proj):
    """После finish заметка начинает НОВЫЙ блок: дописать в закрытую сессию —
    значит задним числом переписать её итог."""
    storage.append_session(proj, "итог закрытой сессии")
    storage.append_note(proj, "заметка уже следующей работы")
    blocks = storage._split_session_blocks(storage._session_path(proj).read_text(encoding="utf-8"))
    assert len(blocks) == 2
    assert "заметка уже следующей работы" in blocks[0]
    assert "итог закрытой сессии" in blocks[1]


def test_stale_running_block_is_not_continued(proj):
    """Блок «в работе» с прошлой датой — брошенная сессия. Новая заметка идёт в
    новый блок, иначе вчерашняя работа и сегодняшняя слипаются в одну."""
    storage._session_path(proj).write_text(
        "# Сессии: demo\n\n## 2026-01-01 09:00 %s\n\n- 09:05 старая заметка\n"
        % storage.RUNNING_MARK, encoding="utf-8")
    storage.append_note(proj, "сегодняшняя работа")
    blocks = storage._split_session_blocks(storage._session_path(proj).read_text(encoding="utf-8"))
    assert len(blocks) == 2, "брошенный блок продолжать нельзя"
    assert "сегодняшняя работа" in blocks[0] and "старая заметка" in blocks[1]


def test_notes_are_capped_per_session(proj):
    for i in range(storage.MAX_NOTES + 8):
        storage.append_note(proj, "заметка %d" % i)
    latest = storage.latest_session(proj)
    assert latest.count("заметка ") <= storage.MAX_NOTES + 1, "блок не должен расти без предела"
    assert "заметка %d" % (storage.MAX_NOTES + 7) in latest, "свежая обязана остаться"


@pytest.mark.asyncio
async def test_tool_does_not_touch_git(proj, monkeypatch):
    """Дешевизна — смысл инструмента: `git add -A` по всей базе стоит 5.5 с, и
    ради одной строки его не платят. Заметка доедет с ближайшим сохранением."""
    from memory_compiler import handlers
    called = []
    monkeypatch.setattr(handlers, "git_commit", lambda *a, **k: called.append(a))
    out = await handlers.session_note("нашёл причину в конфиге nginx", proj)
    assert not called, "session_note не должен коммитить"
    assert "нашёл причину" in storage.latest_session(proj)
    assert "✅" in out[0].text


@pytest.mark.asyncio
async def test_running_session_is_always_shown_at_start(proj, monkeypatch):
    """Незакрытая сессия показывается на старте ВСЕГДА, без совпадения слов.

    Обычную прошлую сессию start_task показывает лишь при пересечении с темой —
    для того, что происходит прямо сейчас, это неверно: заметка писалась именно
    затем, чтобы её увидели, в том числе параллельная сессия с другой темой.
    """
    from memory_compiler import handlers
    storage.append_note(proj, "прод отдаёт 502 после рестарта")
    out = await handlers.start_task("совершенно посторонняя тема запроса", proj)
    text = out[0].text
    assert "прод отдаёт 502 после рестарта" in text
    assert "в работе" in text.lower(), "статус незакрытой сессии должен быть виден"


def test_note_counts_as_a_write_for_freshness():
    """Заметка обязана считаться записью: иначе параллельная сессия её не увидит,
    а своя — получит напоминание сразу после того, как заметку написала."""
    from memory_compiler import tools
    assert "session_note" in tools._FRESHNESS_WRITE_TOOLS
