"""Путь файла трекера: разделители в entity и границы проекта (v1.92.1).

Живой случай 23.09.2026: save_tracking с entity вида «owner/repo PR #94» упал на
сервере — `[Errno 2] No such file or directory: '/knowledge/<проект>/tracking_owner/
.repo PR #94.md.….tmp'`. Имя собиралось как f"tracking_{entity}.md" без очистки, и
«/» делал из него подкаталог, которого нет. Повтор с другим entity прошёл, а
упавшая запись осталась висеть в очереди хуков.

Очищаются ТОЛЬКО разделители пути. Живые трекеры названы как есть — с пробелами,
заглавными, скобками, тире и кириллицей (65 файлов на 25.09.2026), и слаг вроде
make_slug переименовал бы их, то есть осиротил бы трекеры вместе с историей версий.

Попутно тот же зонд показал, что save_tracking_article брал project_dir без проверки
имени: project="../escape" клал трекер ВНЕ базы. Остальные пишущие пути давно ходят
через safe_project_dir.
"""
import asyncio

import pytest

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
