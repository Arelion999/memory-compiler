"""Упоминание будущей версии не двигает трекеры (v1.92.4).

Живой случай 25.09.2026, дважды за минуту. save_lesson «Решения владельца …, порядок
выпуска v1.92.2» ответил «🔄 tracking/deployment: version: 1.91.0 → 1.92.2» и
«🔄 tracking/release: version: 1.92.1 → 1.92.2», следом то же сделал finish_task «Ревью
фикса reindex (v1.92.2) и передача выпуска». Версия 1.92.2 тогда ещё не вышла, обе
заметки называли её планом. Трекеры откатывали руками.

Механизм. auto_update_tracking признаёт заметку своей по вхождению имени сущности или
любого значения current (у deployment совпало models_ready=True с «lint fix=True», у
release — «v1.92.1» в тексте), берёт версии из topic и таких предложений и выбирает
наибольшую. План всегда больше текущей версии, поэтому max выбирал именно его, а guard
отката не возражал: для него это «новее».

Тот же класс по журналам базы (_log.md, 39 авто-апдейтов версии, даты и IP не в счёт):
из 11 ложных 9 — версия, которую заметка сама ставит в план, требование, отрицание или
условие: «что нужно установщику 0.4.0», «требуют HA 2026.9.0», «релиз v1.50.0 НЕ
оформлен», «версия < 0.4.0» и оба случая 25.09.

Тексты заметок ниже — сжатые пересказы живых: сохранены версии, даты, предложения, по
которым трекер признавал заметку своей, и предложения с планом.
"""
import asyncio

import pytest

from memory_compiler.storage import auto_update_tracking, load_tracking, save_tracking_article

DEPLOY_FIELDS = {"container": "mc-mcp", "port": "8765", "deploy": "volume + watcher"}


def _version(entity: str) -> str:
    return str(load_tracking("testproj", entity)["current"]["version"])


def test_owner_decisions_with_planned_release_move_no_tracker(knowledge_dir):
    """Первый живой случай целиком, через save_lesson: план выпуска в заголовке и в
    теле не поднимает ни release (узнал заметку по «v1.92.1»), ни deployment (узнал по
    «fix=True» ↔ models_ready: true)."""
    from memory_compiler.handlers import save_lesson
    save_tracking_article("testproj", "release", {"version": "1.92.1"})
    save_tracking_article("testproj", "deployment", {
        "version": "1.91.0", **DEPLOY_FIELDS, "health_http": 200, "models_ready": True,
        "documents": 4027, "tests": 1342})

    out = asyncio.run(save_lesson(
        "Решения владельца 25.09.2026: трекер с «:», цитаты-проверки (а), рабочий проект "
        "сессии между рестартами, порядок выпуска v1.92.2",
        "Владелец ответил на открытые вопросы 25.09.2026 около 15:30.\n\n"
        "1. Фикс reindex (v1.92.2) выпускать сразу. После него /api/health во время "
        "reindex отвечает, а search ждёт конца скана в пуле потоков.\n\n"
        "2. В _tracking_filename заменять «:» на «-», как уже заменяются «/» и «\\» "
        "(v1.92.1).\n\n"
        "3. Цитаты-проверки: выбран вариант (а). Отвергнут (б) — фоновая запись в статьи "
        "без автора, тот же класс риска, что lint fix=True.",
        "testproj", ["decision", "owner", "tracking"]))

    assert "🔄 tracking/" not in out[0].text, out[0].text
    assert (_version("release"), _version("deployment")) == ("1.92.1", "1.91.0")


def test_release_handed_over_note_moves_no_tracker(knowledge_dir):
    """Второй живой случай целиком, через finish_task: версия фикса в заголовке и рассказ
    о первом случае («поднял … до 1.92.2 до выпуска») трекеры не двигают."""
    from memory_compiler.handlers import finish_task
    save_tracking_article("testproj", "release",
                          {"version": "1.92.1", "commit": "d97984b", "tag": "v1.92.1"})
    save_tracking_article("testproj", "deployment", {"version": "1.92.1", **DEPLOY_FIELDS})

    out = asyncio.run(finish_task(
        "Ревью high фикса reindex (v1.92.2) и передача выпуска; решения владельца по трём "
        "вопросам",
        "/code-review high по фиксу reindex: 6 находок, две исправлены по TDD. "
        "Полный прогон: 1514 passed, 1 skipped.\n"
        "Выпуск v1.92.2 (деплой, живая проверка, коммит, тег, push) ведёт параллельная "
        "сессия.\n"
        "Попутно: save_lesson с упоминанием «v1.92.2» автотрекингом поднял tracking/release "
        "и deployment до 1.92.2 до выпуска. Обе записи откачены на 1.92.1.",
        "testproj", ["reindex", "code-review", "tracking"]))

    assert "🔄 tracking/" not in out[0].text, out[0].text
    assert (_version("release"), _version("deployment")) == ("1.92.1", "1.92.1")


def test_target_version_named_only_in_topic_moves_no_tracker(knowledge_dir):
    """Журнал tabel_hzti 14.09: версия, «нужная установщику», жила только в заголовке,
    тело называло текущую — трекеры ушли на 0.4.0 за сутки до выпуска."""
    save_tracking_article("testproj", "release", {"version": "0.3.43"})
    updates = auto_update_tracking(
        "testproj", "Разобрал, что знает код установки 0.3.43 о прежних версиях.",
        topic="Цепочка установки 0.3.x: что знает код и что нужно установщику 0.4.0 "
              "для перехода")
    assert updates == []
    assert _version("release") == "0.3.43"


def test_required_version_moves_no_tracker(knowledge_dir):
    """Журнал home_assistant 13.09: «требуют HA 2026.9.0» — условие чужого компонента,
    а трекер release ушёл на 2026.9.0 при стоящей 2026.6.4."""
    save_tracking_article("testproj", "release", {"version": "2026.6.4"})
    updates = auto_update_tracking(
        "testproj", "tapo_control 7.1.26/7.1.27 требуют HA 2026.9.0 — на 2026.6.4 не ставится",
        topic="Обновления HACS 13.09")
    assert updates == []
    assert _version("release") == "2026.6.4"


def test_version_after_comparison_moves_no_tracker(knowledge_dir):
    """Журнал tabel_hzti 15.09: «версия < 0.4.0» — условие проверки, не состояние."""
    save_tracking_article("testproj", "release", {"version": "0.3.43"})
    updates = auto_update_tracking(
        "testproj", "M4: build_release.py — BuildError, если у установщика версия < 0.4.0",
        topic="Установщик 0.4: 5 замечаний проверки исправлены")
    assert updates == []
    assert _version("release") == "0.3.43"


def test_release_tag_skips_not_released_version(knowledge_dir):
    """Журнал memory-compiler 27.07: заметка с тегом release «релиз v1.50.0 НЕ оформлен»
    подняла трекер, хотя выпуска не было. Ветка release-тега идёт мимо
    auto_update_tracking, поэтому проверяется отдельно."""
    from memory_compiler.handlers import save_lesson
    save_tracking_article("testproj", "release", {"version": "1.49.0"})
    out = asyncio.run(save_lesson(
        "Guard утёкшей разметки — код готов (701 тест), релиз v1.50.0 НЕ оформлен",
        "НЕ СДЕЛАНО: релиз v1.50.0 по регламенту — release.ps1 -Bump minor.",
        "testproj", ["release", "wip"]))
    assert "🔄 tracking/" not in out[0].text, out[0].text
    assert _version("release") == "1.49.0"


def test_release_tag_planned_topic_version_does_not_fall_back_to_body(knowledge_dir):
    """Регрессия v1.92.4, живой случай 25.09.2026: finish_task с тегом release и
    заголовком «Выпуск v1.92.4: версия-план …» записал tracking/release = 95.104.240.
    «версия-план» совпало с маркером «план», версия заголовка ушла в план, и ветка
    release-тега взяла версии тела: max выбрал обрывок IP из примера («…xx»), guard
    скачка major его пропустил. Заголовок с версией отключает тело, как до v1.92.4:
    отсев плана идёт ПОСЛЕ выбора источника. Цена — промах, если в заголовке план,
    а выпуск назван только в теле."""
    from memory_compiler.handlers import save_lesson
    save_tracking_article("testproj", "release",
                          {"version": "1.92.4", "commit": "60b723b", "tag": "v1.92.4"})
    out = asyncio.run(save_lesson(
        "Выпуск v1.92.4: версия-план в заметке не двигает трекеры release и deployment",
        "Выпущено: коммит 60b723b, тег v1.92.4, push.\n"
        "НЕ ЗАКРЫТО: версия чужого продукта (mcp SDK 1.28.1), обрывки IP вида 10.20.30.xx.",
        "testproj", ["bugfix", "tracking", "release"]))
    assert "🔄 tracking/release" not in out[0].text, out[0].text
    assert _version("release") == "1.92.4"


def test_release_tag_still_takes_released_version(knowledge_dir):
    """Позитивный контроль ветки release-тега: заметка о выпуске двигает трекер."""
    from memory_compiler.handlers import save_lesson
    save_tracking_article("testproj", "release", {"version": "1.92.1"})
    asyncio.run(save_lesson(
        "Выпуск v1.92.2: фоновый reindex больше не останавливает сервер",
        "Выпущено: тег v1.92.2, push. Живая проверка на проде прошла.",
        "testproj", ["release"]))
    assert _version("release") == "1.92.2"


def test_released_version_wins_over_next_planned_one(knowledge_dir):
    """Позитивный контроль auto_update_tracking: вышедшая версия берётся, а следующая,
    названная планом в соседнем предложении, — нет (max без отсева выбрал бы её)."""
    save_tracking_article("testproj", "release", {"version": "1.92.1"})
    auto_update_tracking(
        "testproj",
        "release: выпущен v1.92.2, тег v1.92.2. "
        "Следующий release v1.92.3 будет про разделение замка.",
        topic="Итоги дня")
    assert _version("release") == "1.92.2"


@pytest.mark.parametrize("text, want", [
    ("Фикс reindex (v1.92.2) выпускать сразу", {"1.92.2"}),
    ("Решения владельца: порядок выпуска v1.92.2", {"1.92.2"}),
    ("автотрекингом поднял трекер до 1.92.2 до выпуска", {"1.92.2"}),
    ("что нужно установщику 0.4.0 для перехода", {"0.4.0"}),
    ("План этапа 3: установщик 0.4.0", {"0.4.0"}),
    ("релиз v1.50.0 НЕ оформлен", {"1.50.0"}),
    ("Следующий релиз v1.93.0 будет про замок", {"1.93.0"}),
    ("BuildError, если версия < 0.4.0", {"0.4.0"}),
    ("в requirements mcp[cli]>=1.28.1", {"1.28.1"}),
    ("платформа 8.3.25.1286 требуется для конфигурации", {"8.3.25.1286"}),
])
def test_pending_versions_catch_plan_need_negation_and_condition(text, want):
    from memory_compiler.storage import pending_versions
    assert pending_versions(text) == want


@pytest.mark.parametrize("text", [
    "Выпущен v1.92.2: тег v1.92.2, живая проверка на проде 1.92.2",
    "Выпуск v1.92.2 и тег v1.92.2 запушены",
    "VERSION 1.92.1 -> 1.92.2, в CHANGELOG => 1.92.2",
    "Секция CHANGELOG v1.50.0 написана, доков не требует",
    "Пример из чужого README:\n```\nнужно поставить 1.2.3\n```\nУ нас стоит 1.2.4.",
])
def test_pending_versions_leave_release_facts_alone(text):
    from memory_compiler.storage import pending_versions
    assert pending_versions(text) == set()
