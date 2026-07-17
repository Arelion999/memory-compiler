import memory_compiler.search as s
import memory_compiler.storage as st

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
