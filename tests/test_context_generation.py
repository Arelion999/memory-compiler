import asyncio

import memory_compiler.search as s
import memory_compiler.storage as st
import memory_compiler.handlers as h

FM_ARTICLE = (
    "---\n"
    'contexts:\n'
    '  - heading: "Доступ"\n'
    '    context: "ИИ-контекст доступа"\n'
    "---\n"
    "# nginx_niksdv (203.0.113.99)\n\n"
    "**Теги:** infra ssh\n\n"
    "### Доступ\nSSH root@host\n\n"
    "### Бэкап\nrsync на NAS\n"
)

def test_chunk_article_frontmatter_aware_title_and_sections():
    chunks = s._chunk_article(FM_ARTICLE, "infra/nginx_niksdv.md")
    joined = " ".join(t for _, t in chunks)
    assert "nginx_niksdv (203.0.113.99)" in joined
    assert "---" not in joined
    assert any("Бэкап" in t for _, t in chunks)
    dostup = [t for _, t in chunks if "SSH root@host" in t][0]
    assert "ИИ-контекст доступа" in dostup


def test_parse_article_and_embed_title_frontmatter_aware(monkeypatch):
    import numpy as np
    fields = s._parse_article(FM_ARTICLE, "nginx_niksdv.md", "infra")
    assert fields["title"] == "nginx_niksdv (203.0.113.99)"
    assert "infra" in fields["tags"] and "ssh" in fields["tags"]
    assert not fields["preview"].lstrip().startswith("---")

    monkeypatch.setattr(s, "encode_passages",
                        lambda texts, progress_label=None: [np.array([1.0, 0.0]) for _ in texts])
    s._embeddings.clear(); s._embed_texts.clear(); s._chunk_hashes.clear()
    s.embed_document(FM_ARTICLE, "nginx_niksdv.md", "infra")
    assert s._embed_texts["infra/nginx_niksdv.md"] == "nginx_niksdv (203.0.113.99)"


def test_article_contexts_list_format_and_section_headings():
    ctx = s._article_contexts(FM_ARTICLE)
    assert ctx == {"Доступ": "ИИ-контекст доступа"}
    assert s._article_contexts("# T\n\nтело") == {}
    assert s.section_headings(FM_ARTICLE) == ["Доступ", "Бэкап"]


def test_merge_contexts_roundtrip_special_chars():
    art = "# T\n\n**Теги:** x\n\n### A: спец\nтело\n"
    out = st.merge_contexts(art, {"A: спец": 'ctx с "кавычкой" и :'})
    assert "# T" in out and "### A: спец" in out and "тело" in out
    ctx = s._article_contexts(out)
    assert ctx == {"A: спец": 'ctx с "кавычкой" и :'}
    out2 = st.merge_contexts(out, {"B": "второй"})
    ctx2 = s._article_contexts(out2)
    assert ctx2 == {"A: спец": 'ctx с "кавычкой" и :', "B": "второй"}


def test_context_gaps_scan(knowledge_dir):
    base = knowledge_dir / "testproj"
    (base / "multi.md").write_text(
        "# Multi\n\n**Теги:** t\n\n### A\nтело A\n\n### B\nтело B\n", encoding="utf-8")
    (base / "single.md").write_text(
        "# Single\n\n**Теги:** t\n\nодна секция\n", encoding="utf-8")
    (base / "done.md").write_text(
        "---\ncontexts:\n  - heading: \"A\"\n    context: \"c\"\n  - heading: \"B\"\n    context: \"c\"\n---\n"
        "# Done\n\n**Теги:** t\n\n### A\nт\n\n### B\nт\n", encoding="utf-8")
    # NOTE: adjust the secret article's header to match is_secret_article's REAL detection
    (base / "sekret.md").write_text(
        "# Sek\n\n**Теги:** secret\n**Секрет:** да\n\n### A\nт\n\n### B\nт\n", encoding="utf-8")

    import json
    res = asyncio.run(h.context_gaps("testproj", 10))
    payload = json.loads("".join(c.text for c in res))
    names = {a["filename"] for a in payload["articles"]}
    assert "multi.md" in names
    assert "single.md" not in names
    assert "done.md" not in names
    assert "sekret.md" not in names
    art = next(a for a in payload["articles"] if a["filename"] == "multi.md")
    assert art["sections"] == ["A", "B"] and "тело A" in art["full_text"]
    assert "instructions" in payload


def test_save_contexts_validates_and_writes(knowledge_dir, monkeypatch):
    import numpy as np
    monkeypatch.setattr(s, "encode_passages",
                        lambda texts, progress_label=None: [np.array([1.0, 0.0]) for _ in texts])
    art = knowledge_dir / "testproj" / "multi.md"
    art.write_text("# Multi\n\n**Теги:** t\n\n### Раздел A\nтело A\n\n### Раздел B\nтело B\n",
                   encoding="utf-8")
    res = asyncio.run(h.save_contexts("testproj", "multi.md", [
        {"heading": "Раздел A", "context": "контекст A"},
        {"heading": "Нет такого", "context": "мимо"},
    ]))
    txt = "".join(c.text for c in res)
    assert "skipped" in txt.lower() and "Нет такого" in txt
    disk = art.read_text(encoding="utf-8")
    ctx = s._article_contexts(disk)
    assert ctx == {"Раздел A": "контекст A"}
    assert disk.startswith("---")
    assert "### Раздел B" in disk and "тело B" in disk


def test_dance_roundtrip_reembeds_with_ai_context(knowledge_dir, monkeypatch):
    import numpy as np, json
    monkeypatch.setattr(s, "encode_passages",
                        lambda texts, progress_label=None: [np.array([1.0, 0.0]) for _ in texts])
    art = knowledge_dir / "testproj" / "multi.md"
    art.write_text("# Multi\n\n**Теги:** t\n\n### A\nтело A\n\n### B\nтело B\n", encoding="utf-8")

    before = dict(s._chunk_article(art.read_text(encoding="utf-8"), "testproj/multi.md"))
    a_before = next(txt for txt in before.values() if "тело A" in txt)

    asyncio.run(h.save_contexts("testproj", "multi.md",
                                [{"heading": "A", "context": "ИИ-контекст A"}]))

    after = dict(s._chunk_article(art.read_text(encoding="utf-8"), "testproj/multi.md"))
    a_after = next(txt for txt in after.values() if "тело A" in txt)
    assert "ИИ-контекст A" in a_after and a_after != a_before

    payload = json.loads("".join(c.text for c in asyncio.run(h.context_gaps("testproj", 10))))
    art_entry = next(a for a in payload["articles"] if a["filename"] == "multi.md")
    assert art_entry["sections"] == ["A", "B"]


def test_tools_registered_and_dispatch(knowledge_dir):
    import memory_compiler.tools as t
    names = {tool.name for tool in asyncio.run(t.list_tools())}
    assert {"context_gaps", "save_contexts"} <= names
    res = asyncio.run(t.call_tool("context_gaps", {"project": "testproj", "limit": 5}))
    assert res and res[0].type == "text"


def test_search_by_tag_frontmatter_title_and_preview(knowledge_dir, monkeypatch):
    import numpy as np
    monkeypatch.setattr(s, "encode_passages",
                        lambda texts, progress_label=None: [np.array([1.0, 0.0]) for _ in texts])
    art = knowledge_dir / "testproj" / "fm.md"
    art.write_text(
        "---\ncontexts:\n  - heading: \"A\"\n    context: \"c\"\n---\n"
        "# Заголовок FM\n\n**Теги:** marker\n\n### A\nтело A\n\n### B\nтело B\n",
        encoding="utf-8")
    import memory_compiler.handlers as h
    res = asyncio.run(h.search_by_tag("marker", "testproj"))
    text_out = "".join(c.text for c in res if c.type == "text")
    assert "Заголовок FM" in text_out
    assert "contexts:" not in text_out and '"heading"' not in text_out
    link_titles = [c.title for c in res if c.type == "resource_link"]
    assert link_titles == ["Заголовок FM"]


def test_chunk_article_late_chunking_strips_frontmatter(monkeypatch):
    monkeypatch.setattr(s, "LATE_CHUNKING", True)
    chunks = s._chunk_article(FM_ARTICLE, "infra/nginx_niksdv.md")
    assert len(chunks) == 1
    _, chunk_text = chunks[0]
    assert "---" not in chunk_text and "contexts:" not in chunk_text
    assert "nginx_niksdv (203.0.113.99)" in chunk_text


def test_context_gaps_ignores_timestamp_log_sections(knowledge_dir):
    import json
    base = knowledge_dir / "testproj"
    (base / "log.md").write_text(
        "# Log\n\n**Теги:** t\n\n### 2026-07-17 14:30\nзапись 1\n\n### 2026-07-16 09:00\nзапись 2\n",
        encoding="utf-8")
    (base / "ref.md").write_text(
        "# Ref\n\n**Теги:** t\n\n### Установка\nшаги\n\n### Обновление\n\n### 2026-07-17 10:00\nправка\n",
        encoding="utf-8")
    payload = json.loads("".join(c.text for c in asyncio.run(h.context_gaps("testproj", 10))))
    names = {a["filename"] for a in payload["articles"]}
    assert "log.md" not in names  # только timestamp-секции → не пробел
    ref = next(a for a in payload["articles"] if a["filename"] == "ref.md")
    assert ref["sections"] == ["Установка", "Обновление"]  # timestamp-секция отфильтрована


def test_save_contexts_whitespace_insensitive_heading(knowledge_dir, monkeypatch):
    """Заголовки с нестандартным whitespace (таб/повторы/NBSP — артефакт импорта)
    матчатся по нормализованным пробелам, но хранится КАНОНИЧЕСКИЙ заголовок статьи,
    и чанкинг применяет ИИ-контекст к секции."""
    import numpy as np
    monkeypatch.setattr(s, "encode_passages",
                        lambda texts, progress_label=None: [np.array([1.0, 0.0]) for _ in texts])
    art = knowledge_dir / "testproj" / "ws.md"
    art.write_text(
        "# WS\n\n**Теги:** t\n\n### 1.\t  Что такое X\nтело1\n\n### 2.   Настройка\nтело2\n",
        encoding="utf-8")
    res = asyncio.run(h.save_contexts("testproj", "ws.md", [
        {"heading": "1. Что такое X", "context": "про X"},       # нормализованные пробелы
        {"heading": "2. Настройка", "context": "про настройку"},
    ]))
    txt = "".join(c.text for c in res)
    assert "skipped" not in txt   # оба заголовка совпали, ничего не пропущено

    disk = art.read_text(encoding="utf-8")
    ctx = s._article_contexts(disk)
    assert set(ctx.values()) == {"про X", "про настройку"}
    # ключи — канонические (с исходным whitespace); чанкинг применяет контекст к секции
    chunks = dict(s._chunk_article(disk, "testproj/ws.md"))
    body1 = next(t for t in chunks.values() if "тело1" in t)
    assert "про X" in body1
