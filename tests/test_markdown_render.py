"""Тесты серверного рендеринга Markdown (критерии приёмки задачи 2026-07-09).

Покрывают:
  1. Заголовки #/##/### → <h1..h3>, решётки не видны.
  2. Тройные backtick → <pre><code>, backtick не видны, переносы/отступы сохранены.
  3. Списки, ссылки, цитаты, таблицы, HR.
  4. Существующие статьи (жирный/инлайн-код) не ломаются.
  5. HTML-инъекции из текста не исполняются (XSS).
"""
import re

from memory_compiler.markdown_render import render_markdown, pygments_css


# ─── 1. Заголовки ────────────────────────────────────────────────────────────

def test_headers_render_without_hashes():
    html = render_markdown("# Заголовок 1\n## Заголовок 2\n### Заголовок 3")
    assert "<h1>Заголовок 1</h1>" in html
    assert "<h2>Заголовок 2</h2>" in html
    assert "<h3>Заголовок 3</h3>" in html
    assert "# Заголовок" not in html  # решётки не видны


def test_headers_h4_h6():
    html = render_markdown("#### h4\n##### h5\n###### h6")
    assert "<h4>h4</h4>" in html and "<h5>h5</h5>" in html and "<h6>h6</h6>" in html


# ─── 2. Fenced code blocks ───────────────────────────────────────────────────

def test_fenced_code_block_no_backticks():
    src = "```powershell\nGet-Process | Where-Object {$_.CPU -gt 10}\n```"
    html = render_markdown(src)
    assert "<pre>" in html and "<code" in html
    assert "```" not in html  # backtick не видны
    assert "language-powershell" in html  # языковая метка сохранена


def test_fenced_code_preserves_whitespace_and_indent():
    src = "```\nline1\n    indented\n\nline3\n```"
    html = render_markdown(src)
    # переносы и 4-пробельный отступ сохранены внутри блока
    assert "line1\n    indented\n\nline3" in html


def test_fenced_code_unknown_language_still_renders():
    html = render_markdown("```неведомыйязык\nкод тут\n```")
    assert "<pre>" in html and "код тут" in html


def test_fenced_code_escapes_html_inside():
    html = render_markdown("```\n<script>alert(1)</script>\n```")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# ─── 3. Списки, ссылки, цитаты, таблицы, HR ──────────────────────────────────

def test_unordered_and_nested_lists():
    html = render_markdown("- a\n- b\n  - nested")
    assert html.count("<ul>") == 2  # вложенный список
    assert "<li>a</li>" in html


def test_ordered_list():
    html = render_markdown("1. one\n2. two")
    assert "<ol>" in html and "<li>one</li>" in html


def test_links_render():
    html = render_markdown("[текст](https://example.com)")
    assert '<a ' in html and 'href="https://example.com"' in html
    assert ">текст</a>" in html


def test_blockquote():
    html = render_markdown("> цитата")
    assert "<blockquote>" in html and "цитата" in html


def test_table():
    html = render_markdown("| a | b |\n|---|---|\n| 1 | 2 |")
    assert "<table>" in html and "<th>a</th>" in html and "<td>1</td>" in html


def test_horizontal_rule():
    html = render_markdown("текст\n\n---\n\nещё")
    assert "<hr" in html


# ─── 4. Обратная совместимость (жирный / инлайн-код) ─────────────────────────

def test_bold_and_inline_code_preserved():
    html = render_markdown("**жирный** и `инлайн-код`")
    assert "<strong>жирный</strong>" in html
    assert "<code>инлайн-код</code>" in html


def test_italic_and_strikethrough():
    html = render_markdown("*курсив* и ~~зачёркнуто~~")
    assert "<em>курсив</em>" in html
    assert "<s>зачёркнуто</s>" in html


# ─── 5. XSS ──────────────────────────────────────────────────────────────────

def test_raw_script_neutralized():
    html = render_markdown("до\n\n<script>alert(1)</script>\n\nпосле")
    assert "<script" not in html.lower()


def test_img_onerror_neutralized():
    # сырой <img> экранируется целиком → живого элемента с onerror нет
    html = render_markdown("<img src=x onerror=alert(1)>").lower()
    assert "<img" not in html


def test_javascript_link_neutralized():
    # текст ссылки может остаться, но живого href="javascript:" быть не должно
    html = render_markdown("[клик](javascript:alert(1))").lower()
    assert 'href="javascript' not in html
    assert 'href=javascript' not in html


def test_markdown_image_javascript_src_neutralized():
    html = render_markdown("![alt](javascript:alert(1))").lower()
    assert 'src="javascript' not in html
    assert 'src=javascript' not in html


def test_raw_html_anchor_with_handler_escaped():
    # html=False: сырой <a onclick=...> экранируется, живого тега нет
    html = render_markdown('<a href="https://x.com" onclick="alert(1)">x</a>').lower()
    assert "<a " not in html          # нет живого <a>
    assert "&lt;a" in html            # исходный тег стал инертным текстом


# ─── Подсветка: CSS для обеих тем ────────────────────────────────────────────

def test_pygments_css_has_both_themes():
    css = pygments_css()
    assert ".card .body pre" in css
    assert "[data-theme=light] .card .body pre" in css
    assert len(css) > 200
