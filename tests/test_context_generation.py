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
