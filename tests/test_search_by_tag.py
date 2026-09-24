"""search_by_tag: свежие сверху, не больше limit, одна строка на статью (v1.91.0).

Замер 24.09.2026: тег bugfix — 985 статей и 150 тыс. символов за вызов, плюс
resource_link на каждую строку (кириллица в URI закодирована, текст втрое
больше). Клиент резал ответ, и модель видела случайное начало списка.
"""
import asyncio

from memory_compiler import handlers


def _art(kd, name, title, tags, date=None, updated=None):
    lines = [f"# {title}", ""]
    if date:
        lines.append(f"**Дата:** {date}")
    if updated:
        lines.append(f"**Обновлено:** {updated}")
    lines += [f"**Теги:** {tags}", "", "## Записи", "", "Текст."]
    (kd / "testproj" / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _lines(out):
    return [line for line in out[0].text.splitlines() if line.startswith("- [")]


def test_freshest_first_by_updated_then_date(knowledge_dir):
    _art(knowledge_dir, "old.md", "Старая", "tagx", date="2026-01-05 10:00")
    _art(knowledge_dir, "upd.md", "Обновлённая", "tagx", date="2025-12-01 10:00",
         updated="2026-09-20 12:00")
    _art(knowledge_dir, "mid.md", "Средняя", "tagx", date="2026-05-01 10:00")
    got = _lines(asyncio.run(handlers.search_by_tag("tagx", "testproj")))
    assert got == [
        "- [testproj] Обновлённая — upd.md (2026-09-20)",
        "- [testproj] Средняя — mid.md (2026-05-01)",
        "- [testproj] Старая — old.md (2026-01-05)",
    ]


def test_default_limit_and_honest_tail(knowledge_dir):
    for i in range(35):
        _art(knowledge_dir, f"a{i:02d}.md", f"Статья {i:02d}", "many",
             date=f"2026-01-{i % 28 + 1:02d} 10:00")
    out = asyncio.run(handlers.search_by_tag("many", "testproj"))
    assert len(_lines(out)) == 30
    assert out[0].text.startswith("# Тег: many — 30 из 35, свежие сверху")
    assert out[0].text.rstrip().endswith(
        "*…ещё 5 — сузь `project`, подними `limit` или ищи через `search`*")


def test_header_counts_every_project_including_hidden(knowledge_dir):
    """Счёт по проектам полный: по нему видно, куда сузить `project`."""
    _art(knowledge_dir, "m1.md", "Один", "mix", date="2026-03-01 10:00")
    _art(knowledge_dir, "m2.md", "Два", "mix", date="2026-03-02 10:00")
    (knowledge_dir / "general" / "m3.md").write_text(
        "# Три\n\n**Дата:** 2026-03-03 10:00\n**Теги:** mix\n\n## Записи\n\nТекст.\n", encoding="utf-8")
    text = asyncio.run(handlers.search_by_tag("mix", "all", limit=1))[0].text
    assert text.splitlines()[0] == "# Тег: mix — 1 из 3, свежие сверху (по проектам: testproj 2, general 1)"


def test_limit_is_respected_and_clamped(knowledge_dir):
    for i in range(5):
        _art(knowledge_dir, f"b{i}.md", f"Б {i}", "few", date=f"2026-02-0{i + 1} 10:00")
    assert len(_lines(asyncio.run(handlers.search_by_tag("few", "testproj", limit=2)))) == 2
    assert len(_lines(asyncio.run(handlers.search_by_tag("few", "testproj", limit=0)))) == 1
    assert len(_lines(asyncio.run(handlers.search_by_tag("few", "testproj", limit="мусор")))) == 5


def test_dmy_date_is_normalized_to_iso_for_sorting(knowledge_dir):
    """«24.09.2025» лексикографически обгоняет ISO-даты (первый символ «2» < «2», но
    дальше «4» > «0»), поэтому старую статью с датой в формате ДД.ММ.ГГГГ раньше
    показывало ВЫШЕ свежей — дата обязана переводиться в ISO перед сравнением и
    в выводе."""
    _art(knowledge_dir, "dmy.md", "Старая ДД.ММ.ГГГГ", "datefmt", date="24.09.2025 10:00")
    _art(knowledge_dir, "iso.md", "Свежая ISO", "datefmt", date="2026-01-05 10:00")
    got = _lines(asyncio.run(handlers.search_by_tag("datefmt", "testproj")))
    assert got == [
        "- [testproj] Свежая ISO — iso.md (2026-01-05)",
        "- [testproj] Старая ДД.ММ.ГГГГ — dmy.md (2025-09-24)",
    ]


def test_answer_is_one_text_block(knowledge_dir):
    out = asyncio.run(handlers.search_by_tag("docker", "testproj"))   # статья из фикстуры
    assert len(out) == 1 and out[0].type == "text"
    assert "- [testproj] Test Article — test_article.md (2026-01-01)" in out[0].text


def test_secret_is_listed_without_content(knowledge_dir):
    (knowledge_dir / "testproj" / "secret_x.md").write_text(
        "# Доступ к узлу\n\n**Дата:** 2026-03-01 10:00\n**Теги:** node\n**Секрет:** да\n\nENC:abc\n",
        encoding="utf-8")
    text = asyncio.run(handlers.search_by_tag("node", "testproj"))[0].text
    assert "- [testproj] Доступ к узлу — secret_x.md (2026-03-01)" in text and "ENC:" not in text
