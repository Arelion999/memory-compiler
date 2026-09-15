"""Дата ДД.ММ.ГГГГ — не версия (регрессия 15.09.2026, проект tabel_hzti).

finish_task дважды затёр tracking/release и tracking/deployment с 0.4.0 на 15.09.2026:
versioning.is_date_like узнавал только ГГГГ.ММ.ДД, русская дата 15.09.2026 проходила
в экстрактор как X.Y.Z, max_version выбирал её (15 > 0), и guard отката молчал —
для него это не откат, а «новее».
"""
import asyncio

from memory_compiler import versioning


def test_is_date_like_russian_date():
    assert versioning.is_date_like("15.09.2026")
    assert versioning.is_date_like("1.10.2025")
    assert versioning.is_date_like("31.12.2099")


def test_is_date_like_keeps_versions_and_iso_dates():
    assert versioning.is_date_like("2024.06.25")      # прежнее поведение
    assert not versioning.is_date_like("0.4.0")
    assert not versioning.is_date_like("1.20.1")
    assert not versioning.is_date_like("32.01.2026")  # дня 32 нет
    assert not versioning.is_date_like("15.13.2026")  # месяца 13 нет
    assert not versioning.is_date_like("15.09.26")    # год двумя цифрами датой не считаем


def test_extract_facts_prefers_version_over_russian_date():
    from memory_compiler.storage import extract_facts_from_text
    facts = extract_facts_from_text(
        "15.09.2026, сервер WIN-E52K5S975V7, после установки 0.4.0.",
        "Релиз 0.4.0 и установка на прод 15.09.2026: итог",
    )
    assert facts.get("version") == ["0.4.0"], facts


def test_release_tag_with_russian_date_keeps_version(knowledge_dir):
    """Пример 3 репро: release-тег, в заголовке и версия, и дата — ветка release в save_lesson
    и auto_update_tracking для deployment."""
    from memory_compiler.handlers import save_lesson
    from memory_compiler.storage import save_tracking_article, load_tracking
    save_tracking_article("testproj", "release", {"version": "0.4.0"})
    save_tracking_article("testproj", "deployment", {"version": "0.4.0", "server": "WIN-E52K5S975V7"})

    asyncio.run(save_lesson(
        "Релиз 0.4.0 и установка на прод 15.09.2026: итог и предупреждение backup_path_relative",
        "Релиз v0.4.0 выпущен через release.yml. Владелец поставил 0.4.0 на WIN-E52K5S975V7 15.09.2026 около 22:09.",
        "testproj",
        ["release", "deploy"],
    ))

    assert load_tracking("testproj", "release")["current"]["version"] == "0.4.0", "дата затёрла release"
    assert load_tracking("testproj", "deployment")["current"]["version"] == "0.4.0", "дата затёрла deployment"


def test_auto_update_with_russian_date_keeps_version(knowledge_dir):
    """Пример 2 репро: без release-тега, дата в начале текста — только auto_update_tracking."""
    from memory_compiler.handlers import save_lesson
    from memory_compiler.storage import save_tracking_article, load_tracking
    save_tracking_article("testproj", "deployment", {"version": "0.4.0", "server": "WIN-E52K5S975V7"})

    asyncio.run(save_lesson(
        "Прод 0.4.0 после установки: старый backend, проверка состояния, правило брандмауэра",
        "15.09.2026, сервер WIN-E52K5S975V7, после установки 0.4.0. Владелец проверил службу.",
        "testproj",
        ["deploy", "firewall"],
    ))

    assert load_tracking("testproj", "deployment")["current"]["version"] == "0.4.0", "дата затёрла deployment"


def test_auto_update_with_russian_date_still_takes_new_version(knowledge_dir):
    """Контроль: дата рядом с НОВОЙ версией не мешает обновлению трекера."""
    from memory_compiler.handlers import save_lesson
    from memory_compiler.storage import save_tracking_article, load_tracking
    save_tracking_article("testproj", "deployment", {"version": "0.4.0", "server": "WIN-E52K5S975V7"})

    asyncio.run(save_lesson(
        "Обновление прода",
        "15.09.2026 на WIN-E52K5S975V7 поставили 0.4.1.",
        "testproj",
        ["deploy"],
    ))

    assert load_tracking("testproj", "deployment")["current"]["version"] == "0.4.1", "новая версия не подхватилась"
