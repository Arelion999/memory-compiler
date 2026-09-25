"""Путь файла трекера: недопустимые символы в entity и границы проекта (v1.92.1, v1.92.3).

Живой случай 23.09.2026: save_tracking с entity вида «owner/repo PR #94» упал на
сервере — `[Errno 2] No such file or directory: '/knowledge/<проект>/tracking_owner/
.repo PR #94.md.….tmp'`. Имя собиралось как f"tracking_{entity}.md" без очистки, и
«/» делал из него подкаталог, которого нет. Повтор с другим entity прошёл, а
упавшая запись осталась висеть в очереди хуков.

v1.92.3: «:» сервер на Linux в имени принимает, а Windows — нет, и трекер
infra/tracking_KHV NAT: … не доезжал в локальное зеркало Synology Drive. Теперь в «-»
уходят все символы, недопустимые в имени файла Windows. Файл, записанный раньше под
старым именем, читается по своему entity и переименовывается первой же записью —
иначе после обновления чтение его не нашло бы, а запись завела бы рядом новый файл
без истории.

Очищаются ТОЛЬКО недопустимые символы. Живые трекеры названы как есть — с пробелами,
заглавными, скобками, тире и кириллицей (65 файлов на 25.09.2026), и слаг вроде
make_slug переименовал бы их, то есть осиротил бы трекеры вместе с историей версий.

Попутно тот же зонд показал, что save_tracking_article брал project_dir без проверки
имени: project="../escape" клал трекер ВНЕ базы. Остальные пишущие пути давно ходят
через safe_project_dir.
"""
import asyncio
import re

import pytest

import memory_compiler.storage as st
from memory_compiler.storage import load_tracking, save_tracking_article


def _tracking_files(proj_dir):
    """Все трекеры проекта, включая вложенные, — путями от каталога проекта."""
    return sorted(p.relative_to(proj_dir).as_posix() for p in proj_dir.rglob("tracking_*"))


def test_entity_with_slash_is_saved_and_read_back(knowledge_dir):
    """Живой отказ целиком, через инструменты: запись ложится с первого раза, и
    get_current по тому же entity её находит."""
    from memory_compiler.handlers import get_current, save_tracking
    out = asyncio.run(save_tracking(project="testproj", entity="owner/repo PR #94",
                                    facts={"status": "open"}))
    assert "создан" in out[0].text, out[0].text
    got = "".join(c.text for c in asyncio.run(get_current("testproj", "owner/repo PR #94")))
    assert "**status:** open" in got, got


@pytest.mark.parametrize("entity, file_name", [
    ("owner/repo PR #94", "tracking_owner-repo PR #94.md"),
    ("gw\\router", "tracking_gw-router.md"),
    ("a/b\\c", "tracking_a-b-c.md"),
])
def test_path_separators_become_dash_in_a_flat_file(knowledge_dir, entity, file_name):
    """Разделитель пути в entity → «-», файл лежит прямо в каталоге проекта, а entity
    в статье остаётся таким, как его назвали. Проект новый: каталог заводит запись."""
    save_tracking_article("newproj", entity, {"status": "open"})

    assert _tracking_files(knowledge_dir / "newproj") == [file_name]
    data = load_tracking("newproj", entity)
    assert data["entity"] == entity
    assert data["current"]["status"] == "open"


@pytest.mark.parametrize("entity, file_name", [
    ("домашний выход (VPS уровня 1)", "tracking_домашний выход (VPS уровня 1).md"),
    ("Лид — миграция БП → УХ", "tracking_Лид — миграция БП → УХ.md"),
    ("CRM PR-94", "tracking_CRM PR-94.md"),
])
def test_existing_tracker_is_found_and_updated_in_place(knowledge_dir, entity, file_name):
    """Позитивный контроль: трекер, лежащий на диске под прежним именем, находится по
    своему entity и обновляется на месте — без второго файла рядом."""
    proj = knowledge_dir / "testproj"
    (proj / file_name).write_text(
        "---\ntype: tracking\nproject: testproj\n"
        f'entity: "{entity}"\n'
        "current:\n  status: в работе\n  since: 2026-09-01\nhistory: []\n---\n"
        f"# Testproj — current state ({entity})\n",
        encoding="utf-8")

    assert load_tracking("testproj", entity)["current"]["status"] == "в работе"
    r = save_tracking_article("testproj", entity, {"status": "готово"})

    assert r["action"] == "updated"
    assert _tracking_files(proj) == [file_name]
    data = load_tracking("testproj", entity)
    assert data["current"]["status"] == "готово"
    assert data["history"][0]["status"] == "в работе"


def test_tracker_is_never_written_outside_the_base(knowledge_dir):
    """Небезопасный project отклоняется ДО записи: вне базы ни файла, ни каталога."""
    with pytest.raises(ValueError):
        save_tracking_article("../escape", "probe", {"status": "open"})
    assert not (knowledge_dir.parent / "escape").exists()


def test_tracker_outside_the_base_is_not_read(knowledge_dir):
    """Чтение по небезопасному project не выходит из базы, даже когда там лежит
    подходящий трекер."""
    outside = knowledge_dir.parent / "escape"
    outside.mkdir()
    (outside / "tracking_probe.md").write_text(
        "---\ntype: tracking\nentity: probe\ncurrent:\n  status: чужое\n---\n# probe\n",
        encoding="utf-8")

    with pytest.raises(ValueError):
        load_tracking("../escape", "probe")


# ─── v1.92.3: символы, недопустимые в имени файла Windows ────────────────────
LIVE_ENTITY = "KHV NAT: прод NiksDesk → BackupSrv МОТ"


@pytest.mark.parametrize("entity, file_name", [
    (LIVE_ENTITY, "tracking_KHV NAT- прод NiksDesk → BackupSrv МОТ.md"),
    ('a*b?c"d<e>f|g', "tracking_a-b-c-d-e-f-g.md"),
    ("tab\tnew\nline", "tracking_tab-new-line.md"),
    ("owner/repo PR #94", "tracking_owner-repo PR #94.md"),
    ("домашний выход (VPS уровня 1)", "tracking_домашний выход (VPS уровня 1).md"),
])
def test_file_name_has_no_chars_windows_rejects(entity, file_name):
    """«:*?"<>|» и управляющие символы Windows в имени файла не принимает — такой
    трекер не доезжает в локальное зеркало Drive. Они уходят в «-», как разделители
    пути; остальное имя (пробелы, скобки, кириллица, «#») остаётся как есть."""
    assert st._tracking_filename(entity) == file_name


def test_entity_with_colon_is_saved_flat_and_read_back(knowledge_dir):
    """Живой случай целиком, через инструменты: трекер с «:» ложится в файл с «-», а
    get_current по исходному entity его находит."""
    from memory_compiler.handlers import get_current, save_tracking
    out = asyncio.run(save_tracking(project="testproj", entity=LIVE_ENTITY,
                                    facts={"status": "проброс работает"}))
    assert "создан" in out[0].text, out[0].text
    assert _tracking_files(knowledge_dir / "testproj") == [
        "tracking_KHV NAT- прод NiksDesk → BackupSrv МОТ.md"]
    got = "".join(c.text for c in asyncio.run(get_current("testproj", LIVE_ENTITY)))
    assert "**status:** проброс работает" in got, got


# Настоящий «:» в имени файла Windows создать не даёт (NTFS прочтёт его как поток
# данных), а тесты гоняются на Windows. Поэтому в тестах совместимости недопустимым
# объявлен ещё и «#»: проверяется механизм «старое имя → новое», а не набор символов.
OLD_ENTITY = "KHV NAT# прод"
OLD_NAME = "tracking_KHV NAT# прод.md"
NEW_NAME = "tracking_KHV NAT- прод.md"


@pytest.fixture
def hash_is_unsafe(monkeypatch):
    monkeypatch.setattr(st, "_TRACKING_UNSAFE_CHARS",
                        re.compile(r'[/\\:*?"<>|\x00-\x1f#]'), raising=False)


def _write_tracker(path, entity, status):
    path.write_text(
        "---\ntype: tracking\nproject: testproj\n"
        f'entity: "{entity}"\n'
        f"current:\n  status: {status}\n  since: 2026-09-01\nhistory: []\n---\n"
        f"# Testproj — current state ({entity})\n",
        encoding="utf-8")


def test_old_name_is_read_and_renamed_by_first_write(knowledge_dir, hash_is_unsafe):
    """Трекер, записанный до v1.92.3 под старым именем, находится по своему entity, а
    первая запись переносит его под новое имя вместе с историей — без второго файла."""
    proj = knowledge_dir / "testproj"
    _write_tracker(proj / OLD_NAME, OLD_ENTITY, "в работе")

    assert load_tracking("testproj", OLD_ENTITY)["current"]["status"] == "в работе"
    assert _tracking_files(proj) == [OLD_NAME], "чтение не переименовывает"

    r = save_tracking_article("testproj", OLD_ENTITY, {"status": "готово"})

    assert r["action"] == "updated"
    assert _tracking_files(proj) == [NEW_NAME]
    data = load_tracking("testproj", OLD_ENTITY)
    assert data["current"]["status"] == "готово"
    assert data["history"][0]["status"] == "в работе"


def test_unchanged_write_still_renames_old_file(knowledge_dir, hash_is_unsafe):
    """Запись без изменений фактов тоже переносит файл: переименование — ровно тот
    результат, ради которого такую запись и делают."""
    proj = knowledge_dir / "testproj"
    _write_tracker(proj / OLD_NAME, OLD_ENTITY, "в работе")

    r = save_tracking_article("testproj", OLD_ENTITY, {"status": "в работе"})

    assert r["action"] == "unchanged"
    assert _tracking_files(proj) == [NEW_NAME]
    assert load_tracking("testproj", OLD_ENTITY)["current"]["status"] == "в работе"


def test_old_name_never_overwrites_existing_new_file(knowledge_dir, hash_is_unsafe):
    """Файл под новым именем уже есть — он главный: старый не переезжает поверх него
    (перенос затёр бы свежую историю) и остаётся лежать, как лежал."""
    proj = knowledge_dir / "testproj"
    _write_tracker(proj / OLD_NAME, OLD_ENTITY, "старое")
    _write_tracker(proj / NEW_NAME, OLD_ENTITY, "новое")

    assert load_tracking("testproj", OLD_ENTITY)["current"]["status"] == "новое"
    save_tracking_article("testproj", OLD_ENTITY, {"status": "новее"})

    assert _tracking_files(proj) == sorted([NEW_NAME, OLD_NAME])
    assert load_tracking("testproj", OLD_ENTITY)["current"]["status"] == "новее"
    assert "status: старое" in (proj / OLD_NAME).read_text(encoding="utf-8")


def test_tool_reports_rename_and_moves_index_entry(knowledge_dir, hash_is_unsafe):
    """Инструмент на записи без новых фактов не молчит о переносе: называет новое имя,
    индексирует файл под ним и убирает из индекса прежнее — иначе поиск вёл бы на
    файл, которого нет."""
    from memory_compiler.handlers import save_tracking
    import memory_compiler.search as sm
    proj = knowledge_dir / "testproj"
    _write_tracker(proj / OLD_NAME, OLD_ENTITY, "в работе")
    sm.index_document((proj / OLD_NAME).read_text(encoding="utf-8"), OLD_NAME, "testproj")

    out = asyncio.run(save_tracking(project="testproj", entity=OLD_ENTITY,
                                    facts={"status": "в работе"}))

    assert NEW_NAME in out[0].text, out[0].text
    with sm.get_index().searcher() as s:
        paths = {f["path"] for f in s.all_stored_fields()}
    assert f"testproj/{NEW_NAME}" in paths
    assert f"testproj/{OLD_NAME}" not in paths
