"""Тесты web-endpoints, отвечающих за фасетную фильтрацию (теги + проект).

Регрессии, которые они стерегут (v1.7.22):
  - клик по тегу-чипу запускал полнотекстовый поиск вместо точной
    фильтрации по тегу → счётчик "1c (50)" не совпадал с выдачей (0 статей);
  - выпадающий список проектов не сужал ни статьи, ни облако тегов.
"""
import asyncio
import json

from memory_compiler.api import web_tags, web_by_tag, web_search, web_article


class FakeRequest:
    """Минимальный stand-in под starlette.Request для прямого вызова endpoint'ов."""

    def __init__(self, query=None, path=None):
        self.query_params = query or {}
        self.path_params = path or {}


def _json(resp):
    return json.loads(resp.body)


def _write(kd, project, name, tags, title="T"):
    p = kd / project
    p.mkdir(exist_ok=True)
    (p / name).write_text(
        f"# {title}\n\n"
        f"**Дата:** 2026-01-01 10:00\n"
        f"**Проект:** {project}\n"
        f"**Теги:** {tags}\n\n"
        "## Записи\nтело статьи\n",
        encoding="utf-8",
    )


def test_web_article_returns_rendered_html(knowledge_dir):
    """web_article отдаёт и сырой content, и отрендеренный безопасный content_html."""
    _write(knowledge_dir, "testproj", "md.md", "test")
    (knowledge_dir / "testproj" / "md.md").write_text(
        "# Заголовок\n\n## Подзаголовок\n\n**жирный**\n\n```powershell\nGet-Process\n```\n",
        encoding="utf-8",
    )
    data = _json(asyncio.run(web_article(FakeRequest(path={"project": "testproj", "filename": "md.md"}))))
    assert "content" in data and "content_html" in data
    html = data["content_html"]
    assert "<h1>Заголовок</h1>" in html
    assert "<h2>Подзаголовок</h2>" in html
    assert "<strong>жирный</strong>" in html
    assert "<pre>" in html and "language-powershell" in html
    assert "```" not in html and "##" not in html  # разметка не «сырая»


def test_by_tag_returns_every_article_with_that_tag(knowledge_dir):
    _write(knowledge_dir, "testproj", "a1.md", "1c, deploy")
    _write(knowledge_dir, "testproj", "a2.md", "1c")
    _write(knowledge_dir, "general", "g1.md", "1c")
    data = _json(asyncio.run(web_by_tag(FakeRequest(query={"tag": "1c"}))))
    assert {a["file"] for a in data["articles"]} == {"a1.md", "a2.md", "g1.md"}
    # форма элементов совместима с renderResults в UI
    assert all({"title", "project", "file", "preview"} <= a.keys() for a in data["articles"])


def test_by_tag_count_matches_tags_facet(knowledge_dir):
    """Гарантия консистентности: сколько чип насчитал — столько статей и вернётся."""
    _write(knowledge_dir, "testproj", "a1.md", "1c")
    _write(knowledge_dir, "testproj", "a2.md", "1c")
    _write(knowledge_dir, "general", "g1.md", "1c")
    facet = _json(asyncio.run(web_tags(FakeRequest())))
    count_1c = next(t["count"] for t in facet["tags"] if t["tag"] == "1c")
    arts = _json(asyncio.run(web_by_tag(FakeRequest(query={"tag": "1c"}))))
    assert len(arts["articles"]) == count_1c == 3


def test_by_tag_scoped_to_project(knowledge_dir):
    _write(knowledge_dir, "testproj", "a1.md", "1c")
    _write(knowledge_dir, "general", "g1.md", "1c")
    data = _json(asyncio.run(web_by_tag(FakeRequest(query={"tag": "1c", "project": "testproj"}))))
    assert {a["file"] for a in data["articles"]} == {"a1.md"}


def test_tags_facet_scoped_to_project(knowledge_dir):
    _write(knowledge_dir, "testproj", "a1.md", "1c, deploy")
    _write(knowledge_dir, "general", "g1.md", "backup")
    all_tags = {t["tag"] for t in _json(asyncio.run(web_tags(FakeRequest())))["tags"]}
    assert {"1c", "deploy", "backup"} <= all_tags
    proj_tags = {t["tag"] for t in _json(asyncio.run(web_tags(FakeRequest(query={"project": "testproj"}))))["tags"]}
    assert "1c" in proj_tags and "deploy" in proj_tags
    assert "backup" not in proj_tags


def test_search_respects_project_filter(knowledge_dir):
    from memory_compiler.search import rebuild_index, rebuild_embeddings
    _write(knowledge_dir, "testproj", "nginx_here.md", "web", title="nginx config testproj")
    _write(knowledge_dir, "general", "nginx_other.md", "web", title="nginx config general")
    rebuild_index()
    rebuild_embeddings()
    data = _json(asyncio.run(web_search(FakeRequest(query={"q": "nginx", "project": "testproj"}))))
    projects = {r["project"] for r in data["results"]}
    assert projects <= {"testproj"}
    assert any(r["file"] == "nginx_here.md" for r in data["results"])


def test_streamable_http_mount_additive():
    """v1.10.0: Streamable HTTP смонтирован на /mcp, /sse НЕ удалён (additive)."""
    from mcp.server import Server
    from memory_compiler.api import create_starlette_app
    app = create_starlette_app(Server("test"))
    paths = [getattr(r, "path", None) for r in app.routes]
    assert "/mcp" in paths, f"/mcp не смонтирован: {paths}"
    assert "/sse" in paths, "/sse удалён — транспорт должен быть additive"


def test_health_reports_models_ready(knowledge_dir, monkeypatch):
    """Публичный /api/health отдаёт признак готовности моделей.

    Регресс-риск: поле должно быть именно ПУБЛИЧНЫМ (без auth), иначе состояние
    прогрева не увидит ни Docker healthcheck, ни человек, у которого «поиск молчит».
    Без него «модель ещё грузится» неотличимо от «сервер сломан»: health отвечает ok,
    потому что индекс открыт, а семантический запрос ждёт загрузки под локом."""
    from memory_compiler.api import web_health
    import memory_compiler.search as _s

    monkeypatch.setattr(_s, "_embed_model", None)
    body = _json(asyncio.run(web_health(FakeRequest())))
    assert body["models_ready"] is False, body
    assert "status" in body and "version" in body, "поле не должно ломать публичный ответ"

    monkeypatch.setattr(_s, "_embed_model", object())
    assert _json(asyncio.run(web_health(FakeRequest())))["models_ready"] is True


def test_warm_runs_dummy_encode_not_just_construct(monkeypatch):
    """Прогрев обязан прогнать один текст, а не только сконструировать модель.

    Конструирование и первый forward-pass — разные расходы: без фиктивного encode
    первый живой запрос платил за инициализацию токенизатора и аллокации, даже когда
    модель формально «загружена»."""
    from memory_compiler.api import warm_models
    import memory_compiler.search as _s

    calls = []
    monkeypatch.setattr(_s, "get_embed_model", lambda: calls.append("construct"))
    monkeypatch.setattr(_s, "encode_query", lambda t: calls.append(f"encode:{t}"))

    asyncio.run(warm_models())
    assert calls[0] == "construct", f"модель не сконструирована: {calls}"
    assert any(c.startswith("encode:") for c in calls), \
        f"прогрев не прогнал ни одного текста — первый запрос заплатит за forward-pass: {calls}"


def test_warm_survives_model_failure(monkeypatch):
    """Сбой прогрева не должен ронять старт: модели догрузятся лениво при первом запросе."""
    from memory_compiler.api import warm_models
    import memory_compiler.search as _s

    def boom():
        raise RuntimeError("нет модели в кэше")

    monkeypatch.setattr(_s, "get_embed_model", boom)
    assert asyncio.run(warm_models()) == 0.0, "сбой прогрева обязан быть проглочен"


def test_warm_skips_reranker_when_disabled(monkeypatch):
    """Выключенный reranker не грузится вовсе: ~0.6 ГБ RSS за модель без пользы."""
    from memory_compiler import api as _api
    import memory_compiler.search as _s

    monkeypatch.setattr(_api, "RERANK_ENABLED", False)
    monkeypatch.setattr(_s, "get_embed_model", lambda: None)
    monkeypatch.setattr(_s, "encode_query", lambda t: None)
    loaded = []
    monkeypatch.setattr(_s, "get_reranker_model", lambda: loaded.append(1))

    asyncio.run(_api.warm_models())
    assert loaded == [], "reranker загружен несмотря на выключенный флаг"


def test_ui_styles_bare_pre_inside_card():
    """Голый <pre> — прямой потомок .card — обязан переносить строки.

    Регресс (замечен визуально 2026-07-19): у превью компиляции и блоков аналитики
    своих стилей не было, браузерный дефолт white-space:pre не переносит — длинные
    строки вылезали за правый край карточки. Селектор должен быть ПРЯМЫМ, иначе
    заденет блоки кода в .body.rendered, где перенос не нужен."""
    from memory_compiler.ui import WEB_HTML

    assert ".card>pre{" in WEB_HTML, "нет правила для голого <pre> в карточке"
    rule = WEB_HTML[WEB_HTML.index(".card>pre{"):]
    rule = rule[:rule.index("}")]
    assert "pre-wrap" in rule and "break-word" in rule, f"строки не переносятся: {rule}"
    assert "overflow-x:auto" in rule, f"нет страховки на неразрывную строку: {rule}"
    # блоки кода в отрендеренных статьях НЕ должны переноситься
    code = WEB_HTML[WEB_HTML.index(".card .body.rendered pre{"):]
    code = code[:code.index("}")]
    assert "white-space:pre" in code and "pre-wrap" not in code, \
        f"правило задело блоки кода — там перенос ломает форматирование: {code}"
