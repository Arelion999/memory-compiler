"""Гейт: ни один потребитель не парсит статью срезами по СЫРОМУ файлу.

Класс бага. Формат хранения вырос — в v1.28.0 появился YAML-frontmatter `contexts:`
(ИИ-пересказ секций для contextual retrieval). Потребители, написанные раньше, брали
заголовок как `lines[0]`, а теги искали в `lines[:10]` по сырому тексту. Frontmatter
их туда не пускает: медиана 13 строк, p90 40, максимум 275. Заголовком становился
литерал '---', теги терялись МОЛЧА — исключения нет, статья просто не находится.

Следы на проде: 127 записей '- [---](…) —' в index.md из 1726, 92 статьи невидимы
для навигации по тегам, у 125 описание MCP-ресурса — литерал 'contexts:', 92 ложные
тревоги lint «нет метаданных», и 125 статей выпали из golden-набора retrieval_eval —
измерение ослепло ровно на новейшем формате.

Почему это не поймали 619 тестов: ни один из 114 документов, которые генерируют
фикстуры, не имеет frontmatter длиннее пары строк, и ни один не длиннее 300 символов.
Поэтому здесь статья РЕАЛИСТИЧНАЯ: frontmatter заведомо длиннее любого среза.
"""
import asyncio

import pytest

TITLE = "Настройка nginx на проде example.ru"
TAGS = "nginx, docker, deploy"

# Frontmatter должен быть длинным И В СИМВОЛАХ, И В СТРОКАХ. Это разные пороги:
# символы бьют по срезам text[:300], строки — по lines[:10]/[:15]. Первая версия
# фикстуры была длинной только в символах (9 строк), заголовок попадал в [:15],
# и часть гейта молча теряла чувствительность. В базе медиана 13 строк, p90 40,
# максимум 275 — берём выше 15, чтобы промахивались оба среза.
FM_ARTICLE = (
    "---\n"
    "contexts:\n"
    + "".join(
        f"  - heading: Раздел {n}\n"
        f'    context: "Описание раздела {n}: что настраивали, какие параметры '
        f'трогали и почему выбрали именно такой вариант конфигурации."\n'
        for n in range(1, 10)
    )
    + "---\n"
    f"# {TITLE}\n"
    "\n"
    "**Дата:** 2026-01-01 10:00\n"
    "**Обновлено:** 2026-01-02 11:00\n"
    "**Проект:** testproj\n"
    f"**Теги:** {TAGS}\n"
    "\n"
    "## Записи\n"
    "\n"
    "### 2026-01-01 10:00\n"
    "Проксирование настроено на порт 8080, кэш включён, конфиг уехал на прод.\n"
)


@pytest.fixture
def fm_article(knowledge_dir):
    """Реалистичная статья с длинным frontmatter в проекте testproj."""
    p = knowledge_dir / "testproj"
    p.mkdir(exist_ok=True)
    f = p / "fm_nginx.md"
    f.write_text(FM_ARTICLE, encoding="utf-8")
    return f


def test_fixture_is_realistic(fm_article):
    """Страховка на сам гейт: если frontmatter укоротят, тесты ниже станут зелёными
    по неверной причине — они просто перестанут воспроизводить условие бага."""
    head = FM_ARTICLE.split("\n# ")[0]
    assert len(head) > 300, "frontmatter короче среза по символам"
    assert len(head.splitlines()) > 15, "frontmatter короче срезов lines[:10]/[:15]"


def test_naive_slicing_still_fails_on_this_fixture():
    """Чувствительность гейта, зафиксированная навсегда: на ЭТОЙ статье наивный
    разбор — ровно тот, что стоял в коде до фикса, — даёт '---' вместо заголовка
    и не находит теги. Значит тесты выше зелены потому, что потребители починены,
    а не потому, что фикстура перестала воспроизводить условие."""
    lines = FM_ARTICLE.splitlines()
    assert lines[0].lstrip("# ").strip() == "---"
    assert not any(l.startswith("**Теги:**") for l in lines[:10])
    assert not any(l.strip().startswith("# ") for l in lines[:10])


# ─── общий хелпер ────────────────────────────────────────────────────────────

def test_article_title_tags(fm_article):
    from memory_compiler.storage import article_title_tags
    title, tags = article_title_tags(FM_ARTICLE, fallback="fallback")
    assert title == TITLE
    assert tags == TAGS


# ─── MCP-хендлеры ────────────────────────────────────────────────────────────

def test_get_context_preview_has_no_yaml(fm_article):
    """Точный двойник архетипа ask_fragment: из 24 строк превью контенту
    принадлежало 0. Ветка БЕЗ query — с query превью шло от make_preview."""
    from memory_compiler.handlers import get_context
    out = asyncio.run(get_context("testproj"))[0].text
    assert "contexts:" not in out and "heading:" not in out
    assert TITLE in out


def test_get_summary_keeps_title_and_tags(fm_article):
    from memory_compiler.handlers import get_summary
    out = asyncio.run(get_summary("testproj"))[0].text
    assert TITLE in out, "заголовком стало имя файла"
    assert "nginx" in out and "docker" in out, "теги потерялись"


def test_lint_does_not_report_false_missing_metadata(fm_article):
    """92 ложные тревоги «нет метаданных» на статьях, где метаданные есть."""
    from memory_compiler.handlers import lint
    out = asyncio.run(lint(project="testproj", fix=False))[0].text
    assert "fm_nginx.md — нет метаданных" not in out


# ─── MCP-ресурсы (пассивный контекст модели) ─────────────────────────────────

def test_resource_title_and_description(fm_article):
    from memory_compiler.tools import _resource_title, _resource_description
    assert _resource_title(FM_ARTICLE, "fm_nginx.md") == TITLE
    desc = _resource_description(FM_ARTICLE)
    assert desc != "contexts:", "в описание ресурса уехал литерал YAML-ключа"
    assert not desc.startswith("---")


# ─── REST ────────────────────────────────────────────────────────────────────

class FakeRequest:
    def __init__(self, query=None, path=None):
        self.query_params = query or {}
        self.path_params = path or {}


def _json(resp):
    import json
    return json.loads(resp.body)


def test_web_article_title(fm_article):
    from memory_compiler.api import web_article
    d = _json(asyncio.run(web_article(FakeRequest(
        path={"project": "testproj", "filename": "fm_nginx.md"}))))
    assert d["title"] == TITLE


def test_web_graph_nodes_are_named(fm_article):
    from memory_compiler.api import web_graph
    d = _json(asyncio.run(web_graph(FakeRequest())))
    node = next((n for n in d["nodes"] if n["id"].endswith("fm_nginx.md")), None)
    assert node is not None
    assert node["title"] == TITLE, "узел графа остался безымянной точкой"
    assert "nginx" in node["tags"]


def test_web_export_titles(fm_article):
    from memory_compiler.api import web_export
    d = _json(asyncio.run(web_export(FakeRequest(path={"project": "testproj"}))))
    art = next((a for a in d["articles"] if a["filename"] == "fm_nginx.md"), None)
    assert art is not None and art["title"] == TITLE


def test_article_tags_visible_to_tag_navigation(fm_article):
    """92 статьи были невидимы для облака тегов и фильтра по тегу."""
    from memory_compiler.api import _article_tags
    assert set(_article_tags(fm_article)) >= {"nginx", "docker", "deploy"}


# ─── измерение качества поиска ───────────────────────────────────────────────

def test_golden_set_includes_frontmatter_articles(fm_article, knowledge_dir):
    """125 статей (7.6% корпуса) молча выпадали из known-item набора: заголовком
    оказывался '---', длина 3 < 8, и фильтр их отбрасывал. Сеть была слепа ровно
    к новейшему формату — там, где регрессия вероятнее всего."""
    from memory_compiler.retrieval_eval import build_known_item_set
    items = build_known_item_set(str(knowledge_dir))
    assert any(i["expected"] == {"testproj/fm_nginx.md"} for i in items), \
        "статья с frontmatter не попала в golden-набор"
