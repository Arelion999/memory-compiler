import memory_compiler.search as s

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
