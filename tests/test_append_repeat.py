"""Повтор дописывания не пишет дубль (v1.88.1).

15.09.2026 в статью легли две одинаковые записи подряд: edit_article(append) упал в
окне рестарта контейнера, и вызов повторили дважды. Хук клиента больше не заставляет
посторонние сессии повторять чужие вызовы, но повтор вызова, чей ответ потерялся по
дороге (рестарт, клиентский таймаут), остаётся штатным сценарием: модель учат
повторять упавшую запись. Сервер узнаёт повтор по ПОСЛЕДНЕЙ записи статьи.
"""

import asyncio
from datetime import datetime, timedelta

from memory_compiler.handlers import edit_article

HEAD = (
    "# МОТ: Veeam не делает копий ВМ\n\n"
    "**Дата:** 2026-09-15 17:25\n"
    "**Проект:** testproj\n"
    "**Теги:** veeam, backup\n\n"
    "## Записи\n"
)
BODY = "### 15.09.2026 — кто разбирает\nВладелец поручил разбор Константину."


def _article(knowledge_dir, text=HEAD):
    path = knowledge_dir / "testproj" / "veeam.md"
    path.write_text(text, encoding="utf-8")
    return path


def _append(content):
    return asyncio.run(edit_article(project="testproj", filename="veeam.md",
                                    content=content, append=True))[0].text


def _count(path, needle="Владелец поручил разбор"):
    return path.read_text(encoding="utf-8").count(needle)


def test_repeated_append_is_not_written_twice(knowledge_dir):
    path = _article(knowledge_dir)
    assert "Дописано" in _append(BODY)
    again = _append(BODY)
    assert _count(path) == 1, "повтор того же дописывания записал дубль"
    assert "Уже дописано" in again, again


def test_other_text_is_appended(knowledge_dir):
    """Позитивный контроль: другой текст — новая запись, а не повтор."""
    path = _article(knowledge_dir)
    _append(BODY)
    _append("### 15.09.2026 — итог\nРазбор закончен.")
    text = path.read_text(encoding="utf-8")
    assert "Владелец поручил разбор" in text and "Разбор закончен" in text


def test_same_text_after_the_window_is_a_new_entry(knowledge_dir):
    """Тот же текст спустя часы — осознанная запись, а не потерянный ответ."""
    old = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
    path = _article(knowledge_dir, HEAD + f"\n### {old}\n{BODY}\n")
    _append(BODY)
    assert _count(path) == 2


def test_only_the_last_entry_counts(knowledge_dir):
    """Между попытками легла другая запись — сравнивать не с чем, пишем."""
    path = _article(knowledge_dir)
    _append(BODY)
    _append("Промежуточная запись.")
    _append(BODY)
    assert _count(path) == 2


def test_longer_text_with_the_same_start_is_not_a_repeat(knowledge_dir):
    path = _article(knowledge_dir)
    _append(BODY)
    _append(BODY + "\nДополнение: срок — пятница.")
    assert "срок — пятница" in path.read_text(encoding="utf-8")


def test_repeat_with_triggers_duplicates_neither_entry_nor_trigger(knowledge_dir):
    """Повтор несёт не только текст, но и триггеры: ни запись, ни строка рефлекса не
    дублируются. Держится это на идемпотентности reflexes.add_triggers — фиксируем
    явно, а не надеемся на свойство чужого модуля (ревью 15.09.2026)."""
    path = _article(knowledge_dir)

    def call():
        return asyncio.run(edit_article(project="testproj", filename="veeam.md", content=BODY,
                                        append=True, triggers=["цель: 192.0.2.10"]))[0].text

    call()
    again = call()
    assert _count(path) == 1, "повтор с триггерами записал дубль"
    assert path.read_text(encoding="utf-8").count("192.0.2.10") == 1, "повтор продублировал строку рефлекса"
    assert "Уже дописано" in again, again


def test_secret_append_is_never_deduplicated(knowledge_dir, monkeypatch):
    """У секрета тело зашифровано со случайным IV — сравнивать не с чем, и повтор в секрет
    пишется как есть: лучше дубль, чем молча потерянное содержимое."""
    import memory_compiler.config as cfg
    from memory_compiler.handlers import read_article, save_secret
    monkeypatch.setattr(cfg, "MC_ENCRYPT_KEY", "test-secret-key-123")
    asyncio.run(save_secret(topic="VPN keys", project="testproj", content="basetokenccc wg-key"))
    fname = next(p.name for p in (knowledge_dir / "testproj").glob("*.md")
                 if "**Секрет:** да" in p.read_text(encoding="utf-8"))
    for _ in range(2):
        asyncio.run(edit_article(project="testproj", filename=fname,
                                 content="appendtokenddd", append=True))
    got = asyncio.run(read_article(project="testproj", filename=fname))[0].text
    assert got.count("appendtokenddd") == 2, "повтор в секрет потерял содержимое"


def test_repeat_is_recognised_before_a_trailing_section(knowledge_dir):
    """Вызов с триггерами дописывает раздел ПОСЛЕ записи — повтор всё равно узнаётся."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    path = _article(knowledge_dir, HEAD + f"\n### {now}\n{BODY}\n\n## Рефлексы\n- цель: 192.0.2.10\n")
    _append(BODY)
    assert _count(path) == 1, "раздел после записи помешал узнать повтор"
