"""Серверный рендеринг Markdown статей в безопасный HTML (CommonMark + GFM).

Почему на сервере, а не в браузере: санитайзинг и подсветка живут в Python
(зрелые библиотеки markdown-it-py / nh3 / pygments), UI остаётся самодостаточным
(без CDN и вендоренных JS-либ), клиент просто вставляет уже очищенный HTML.

Безопасность (XSS) — два рубежа:
  1. markdown-it рендерит с html=False → любой сырой HTML во вводе экранируется,
     а validateLink режет javascript:/vbscript:/data: в ссылках;
  2. nh3 (ammonia) дополнительно чистит вывод по строгому allowlist тегов/атрибутов.
"""
from __future__ import annotations

from functools import lru_cache

import nh3
from markdown_it import MarkdownIt
from pygments import highlight as _pyg_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

# ─── Подсветка синтаксиса ────────────────────────────────────────────────────

# Тема для тёмного оформления (по умолчанию) и для светлого (по data-theme=light).
_STYLE_DARK = "native"
_STYLE_LIGHT = "friendly"


def _highlight(code: str, lang: str, _attrs) -> str:
    """Callback markdown-it: возвращает HTML-подсветку (spans) или "".

    Пустая строка → markdown-it сам экранирует код и обернёт в <pre><code>,
    сохранив class="language-<lang>". Так неизвестные языки не ломают рендер.
    """
    if not lang:
        return ""
    try:
        lexer = get_lexer_by_name(lang, stripnl=False)
    except ClassNotFound:
        return ""
    return _pyg_highlight(code, lexer, HtmlFormatter(nowrap=True))


_md = MarkdownIt(
    "commonmark",
    {"html": False, "linkify": False, "breaks": False, "highlight": _highlight},
).enable(["table", "strikethrough"])


# ─── Санитайзинг вывода (defense-in-depth) ───────────────────────────────────

_ALLOWED_TAGS = {
    "p", "br", "hr", "blockquote", "pre", "code", "span",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "em", "strong", "del", "s", "a", "img",
    "table", "thead", "tbody", "tr", "th", "td",
}

# class нужен для подсветки (span/code/pre) и языковой метки (code language-*).
_ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title"},
    "code": {"class"},
    "span": {"class"},
    "pre": {"class"},
}

_URL_SCHEMES = {"http", "https", "mailto"}


def render_markdown(text: str) -> str:
    """Отрендерить Markdown в безопасный HTML для веб-просмотра статьи."""
    html = _md.render(text or "")
    return nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        url_schemes=_URL_SCHEMES,
        link_rel="noopener noreferrer nofollow",
    )


@lru_cache(maxsize=1)
def pygments_css() -> str:
    """CSS подсветки для обеих тем: тёмная — базовая, светлая — по data-theme=light.

    Токены Pygments — это <span class="..."> внутри <pre>, поэтому селекторы
    привязаны к '.card .body pre'. Светлые правила имеют выше специфичность
    (лишний атрибут), поэтому перекрывают тёмные при data-theme=light.
    """
    dark = HtmlFormatter(style=_STYLE_DARK).get_style_defs(".card .body pre")
    light = HtmlFormatter(style=_STYLE_LIGHT).get_style_defs(
        "[data-theme=light] .card .body pre"
    )
    return dark + "\n" + light
