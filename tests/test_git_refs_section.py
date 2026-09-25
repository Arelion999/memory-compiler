"""Раздел «## Git-ссылки» обновляется, не стирая соседний текст (v1.78.0).

До фикса save_lesson заменял ВСЁ после заголовка раздела. merge_into_article пишет
записи в конец файла, то есть после раздела, и второе сохранение с git-ссылкой
стирало только что слитую запись вместе с прежними ссылками.
"""
import asyncio

from memory_compiler.handlers import save_lesson
from memory_compiler.storage import upsert_git_refs

ARTICLE = (
    "# Статья\n\n**Дата:** 2026-01-01 10:00\n**Проект:** testproj\n**Теги:** test\n\n"
    "## Записи\n\n### 2026-01-01 10:00\nпервая запись\n\n"
    "## Git-ссылки\n**Теги:** v1.0.0\n\n"
    "### 2026-01-02 10:00\nвторая запись после раздела\n"
)


def test_upsert_keeps_text_after_section_and_merges_refs():
    new = upsert_git_refs(ARTICLE, {"tag": ["v1.1.0"], "commit": ["abc1234"]})
    assert "вторая запись после раздела" in new
    assert "**Теги:** v1.0.0, v1.1.0" in new
    assert "**Коммиты:** abc1234" in new
    assert new.count("## Git-ссылки") == 1


def test_upsert_creates_section_when_missing():
    text = "# Статья\n\n## Записи\n\n### 2026-01-01 10:00\nзапись\n"
    new = upsert_git_refs(text, {"tag": ["v2.0.0"]})
    assert new.endswith("\n\n## Git-ссылки\n**Теги:** v2.0.0\n")


def test_upsert_keeps_foreign_lines_inside_section():
    """update_cross_references дописывает ссылку в конец файла — в тело последнего раздела."""
    text = ("# T\n\n## Записи\n\n### 2026-01-01 10:00\nтело\n\n"
            "## См. также\n- [a](a.md)\n\n"
            "## Git-ссылки\n**Теги:** v1.0.0\n- [b](b.md) — ссылка, дописанная в конец файла\n")
    new = upsert_git_refs(text, {"tag": ["v1.1.0"]})
    assert "- [b](b.md) — ссылка, дописанная в конец файла" in new
    assert "**Теги:** v1.0.0, v1.1.0" in new


def test_upsert_ignores_heading_inside_code_block():
    text = ("# T\n\n## Записи\n\n### 2026-01-01 10:00\nпример:\n```\n## Git-ссылки\n"
            "**Теги:** v0.1\n```\nхвост\n")
    new = upsert_git_refs(text, {"tag": ["v2.0.0"]})
    assert "```\n## Git-ссылки\n**Теги:** v0.1\n```\nхвост\n" in new
    assert new.endswith("\n\n## Git-ссылки\n**Теги:** v2.0.0\n")


def test_second_save_with_git_ref_keeps_merged_entry(knowledge_dir):
    asyncio.run(save_lesson("Тема с тегом", "первая запись про v1.0.0", "testproj"))
    asyncio.run(save_lesson("Тема с тегом", "вторая запись про v1.1.0", "testproj"))
    files = list((knowledge_dir / "testproj").glob("тема_с_тегом*.md"))
    assert len(files) == 1, f"слияние не пошло в одну статью: {files}"  # позитивный контроль
    text = files[0].read_text(encoding="utf-8")
    assert "первая запись про v1.0.0" in text
    assert "вторая запись про v1.1.0" in text
    refs = text.split("## Git-ссылки", 1)[1]
    assert "v1.0.0" in refs and "v1.1.0" in refs


def test_save_lesson_file_hash_does_not_become_commit(knowledge_dir):
    """Живой случай 25.09.2026 (v1.92.8): «sha1 7ff1e2f6 тот же» дал ответ «🔗 Git:
    commit: 7ff1e2f6» и раздел «Коммиты» в статье. Позитивный контроль — настоящий
    коммит в соседней статье: без него «коммитов нет» прошло бы и на отключённой
    git-линковке."""
    out = asyncio.run(save_lesson("Сверка трекера после переименования",
                                  "На NAS файл один, sha1 7ff1e2f6 тот же.", "testproj"))
    ctrl = asyncio.run(save_lesson("Выпуск с тегом",
                                   "Коммит 3c8f5a1, тег v1.92.5, push в 16:59.", "testproj"))
    assert "commit: 3c8f5a1" in ctrl[0].text
    assert "**Коммиты:** 3c8f5a1" in (knowledge_dir / "testproj" / "выпуск_с_тегом.md").read_text(encoding="utf-8")
    assert "commit" not in out[0].text
    text = next((knowledge_dir / "testproj").glob("сверка_трекера*.md")).read_text(encoding="utf-8")
    assert "sha1 7ff1e2f6 тот же" in text
    assert "Коммиты" not in text
