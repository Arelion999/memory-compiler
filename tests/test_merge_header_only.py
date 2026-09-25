"""merge_into_article правит метаданные ТОЛЬКО шапки.

Цикл обновления «**Теги:**», «**Обновлено:**» и «**Дата:**» шёл по ВСЕМ строкам файла.
В «## Git-ссылки» есть своя строка «**Теги:**» — git-теги, её пишет upsert_git_refs, —
и каждое слияние вливало в неё теги статьи, а upsert_git_refs потом держал их как
git-теги вечно. Замер боевой базы 25.09.2026: 130 статей в 14 проектах, 1171 лишнее
значение (niksdesk/alembic_на_чистой_бд_…: «**Теги:** unsafenewenumvalueusage, alembic,
backend, bug, …, v1.3.101»). Ветки «Дата» и «Обновлено» того же цикла дописывали
«**Обновлено:**» внутрь записей, у которых content сам начинался с шапки.

В каждом тесте позитивный контроль: теги ШАПКИ обязаны слиться. Без него «git-строка
не тронута» проходило бы и на слиянии, которое не сработало вовсе.
"""
import asyncio

from memory_compiler.handlers import save_lesson
from memory_compiler.storage import merge_into_article

TS = "2026-09-25 12:00"
HEAD = "# Статья\n\n**Дата:** 2026-01-01 10:00\n**Проект:** testproj\n"
GIT = "## Git-ссылки\n**Теги:** v1.0.0\n"


def _article(knowledge_dir, name, text):
    f = knowledge_dir / "testproj" / name
    f.write_text(text, encoding="utf-8")
    return f


def _header(text):
    lines = text.splitlines()
    return lines[:next(i for i, l in enumerate(lines) if l.startswith(("## ", "### ")))]


def _git_tags(text):
    """Строка «**Теги:**» раздела «## Git-ссылки»."""
    section = text.split("## Git-ссылки\n", 1)[1]
    return next(l for l in section.splitlines() if l.startswith("**Теги:**"))


def test_merge_keeps_git_tags_out_of_article_tags(knowledge_dir):
    """Воспроизведение 25.09.2026: в разделе стало «**Теги:** beta, v1.0.0»."""
    f = _article(knowledge_dir, "a.md", HEAD + "**Теги:** alpha\n\n## Записи\n\n"
                 "### 2026-01-01 10:00\nпервая запись\n\n" + GIT)
    assert merge_into_article(f, "вторая запись", ["beta"], TS) == "merged"
    text = f.read_text(encoding="utf-8")
    assert "**Теги:** alpha, beta" in _header(text)               # позитивный контроль
    assert _git_tags(text) == "**Теги:** v1.0.0"
    assert text.rstrip().endswith("вторая запись")


def test_duplicate_entry_keeps_git_tags_when_header_has_no_tags(knowledge_dir):
    """Ветка повтора (_merge_tags_only) брала ПЕРВУЮ строку «**Теги:**» в файле: без
    тегов в шапке ею оказывалась строка git-тегов."""
    f = _article(knowledge_dir, "dup.md", HEAD + "\n## Записи\n\n"
                 "### 2026-01-01 10:00\nта же запись\n\n" + GIT)
    assert merge_into_article(f, "та же запись", ["beta"], "2026-01-01 10:00") == "duplicate"
    assert _git_tags(f.read_text(encoding="utf-8")) == "**Теги:** v1.0.0"


def test_duplicate_entry_merges_tags_into_header(knowledge_dir):
    """Позитивный контроль ветки повтора: теги шапки сливаются, git-теги на месте."""
    f = _article(knowledge_dir, "dup2.md", HEAD + "**Теги:** alpha\n\n## Записи\n\n"
                 "### 2026-01-01 10:00\nта же запись\n\n" + GIT)
    assert merge_into_article(f, "та же запись", ["beta"], "2026-01-01 10:00") == "duplicate"
    text = f.read_text(encoding="utf-8")
    assert "**Теги:** alpha, beta" in _header(text)
    assert _git_tags(text) == "**Теги:** v1.0.0"


def test_old_format_article_keeps_git_tags(knowledge_dir):
    """Статья без «## Записи» и без «### »: прежняя граница шапки уходила в конец
    файла, и раздел «## Git-ссылки» считался частью шапки."""
    f = _article(knowledge_dir, "old.md", HEAD + "**Теги:** alpha\n\n## Шаги\n\n"
                 "- [ ] шаг\n\n" + GIT)
    merge_into_article(f, "новая запись", ["beta"], TS)
    text = f.read_text(encoding="utf-8")
    assert "**Теги:** alpha, beta" in _header(text)               # позитивный контроль
    assert _git_tags(text) == "**Теги:** v1.0.0"


def test_merge_leaves_meta_lines_inside_entries(knowledge_dir):
    """content, начинавшийся с собственной шапки: цикл вставлял после её «**Дата:**»
    строку «**Обновлено:**», переписывал её «Обновлено» и сливал теги в её «Теги»."""
    entry = ("# Вложенная\n\n**Дата:** 2026-05-01\n**Обновлено:** 2026-05-01 22:41\n"
             "**Теги:** чужие\n\nтекст записи")
    f = _article(knowledge_dir, "nested.md", HEAD + "**Теги:** alpha\n\n## Записи\n\n"
                 "### 2026-01-01 10:00\n" + entry + "\n")
    merge_into_article(f, "новая запись", ["beta"], TS)
    text = f.read_text(encoding="utf-8")
    header = _header(text)
    assert f"**Обновлено:** {TS}" in header                       # позитивный контроль
    assert "**Теги:** alpha, beta" in header
    assert entry in text, "метаданные внутри записи переписаны"


def test_frontmatter_article_keeps_git_tags_and_yaml(knowledge_dir):
    """Шапка идёт после frontmatter contexts: — он возвращается байт-в-байт."""
    fm = ('---\ncontexts:\n  - heading: "### 2026-01-01 10:00"\n'
          '    context: "про ## Git-ссылки и **Теги:** в тексте"\n---\n')
    f = _article(knowledge_dir, "fm.md", fm + HEAD + "**Теги:** alpha\n\n## Записи\n\n"
                 "### 2026-01-01 10:00\nпервая запись\n\n" + GIT)
    merge_into_article(f, "вторая запись", ["beta"], TS)
    text = f.read_text(encoding="utf-8")
    assert text.startswith(fm)
    assert "**Теги:** alpha, beta" in _header(text[len(fm):])     # позитивный контроль
    assert _git_tags(text) == "**Теги:** v1.0.0"


def test_repeated_saves_keep_only_versions_in_git_tags(knowledge_dir):
    """Сквозной путь save_lesson: слияние, затем upsert_git_refs читает строку раздела
    и сохраняет всё, что в ней нашёл, — утечка закреплялась навсегда."""
    asyncio.run(save_lesson("Тема с тегом", "первая запись про v1.0.0", "testproj", ["alpha"]))
    asyncio.run(save_lesson("Тема с тегом", "вторая запись про v1.1.0", "testproj", ["beta"]))
    files = list((knowledge_dir / "testproj").glob("тема_с_тегом*.md"))
    assert len(files) == 1, f"слияние не пошло в одну статью: {files}"  # позитивный контроль
    text = files[0].read_text(encoding="utf-8")
    header_tags = next(l for l in _header(text) if l.startswith("**Теги:**"))
    assert "alpha" in header_tags and "beta" in header_tags
    assert _git_tags(text) == "**Теги:** v1.0.0, v1.1.0"
