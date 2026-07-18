"""Тесты tool ask (v1.25.0): конвейер поиска + выбор фрагмента + секреты.

До этого у ask не было ни одного выделенного теста, хотя он отставал от search:
брал whoosh top-5 без cross-encoder реранка и без фолбэка на project=all, а фрагмент
резал из СЫРОГО файла в обход проверки на секретность (которая в search есть).
"""
import asyncio

import pytest

from memory_compiler.handlers import ask, ask_fragment


def _text(resp):
    return resp[0].text


def _write(kd, project, name, title, body, tags="test", secret=False):
    p = kd / project
    p.mkdir(exist_ok=True)
    head = f"# {title}\n\n**Дата:** 2026-01-01 10:00\n**Проект:** {project}\n**Теги:** {tags}\n"
    if secret:
        head += "**Секрет:** да\n"
    (p / name).write_text(f"{head}\n## Записи\n\n### 2026-01-01 10:00\n{body}\n", encoding="utf-8")


# ─── ask_fragment (чистая функция) ───────────────────────────────────────────

def test_fragment_picks_section_with_most_question_words():
    text = ("# T\n\n## Записи\n"
            "\n### 2026-01-01 10:00\nсовсем про другое, огурцы и помидоры\n"
            "\n### 2026-01-02 10:00\nнастройка nginx для проксирования backend\n")
    frag = ask_fragment(text, "как сделать настройка nginx проксирования")
    assert "nginx" in frag and "огурцы" not in frag


def test_fragment_empty_when_nothing_matches():
    assert ask_fragment("# T\n\n### 2026\nсовершенно посторонний текст\n", "квантовая хромодинамика") == ""


def test_fragment_respects_limit():
    long_body = "nginx " * 500
    frag = ask_fragment(f"# T\n\n### 2026\n{long_body}\n", "nginx", limit=100)
    assert len(frag) <= 100


def test_fragment_ignores_short_words_and_empty_question():
    """Слова ≤2 символов не значимы; пустой вопрос не должен матчить всё подряд."""
    assert ask_fragment("# T\n\n### 2026\nтекст\n", "и в на") == ""
    assert ask_fragment("# T\n\n### 2026\nтекст\n", "") == ""


# ─── ask (хендлер) ───────────────────────────────────────────────────────────

def test_ask_returns_fragment_with_scores(knowledge_dir):
    from memory_compiler.search import rebuild_index
    _write(knowledge_dir, "testproj", "nginx.md", "Проксирование nginx",
           "настройка nginx для проксирования backend на порт 8080", tags="nginx")
    rebuild_index()
    out = _text(asyncio.run(ask("настройка проксирования nginx", project="testproj")))
    assert "Ответ на:" in out
    assert "testproj/nginx.md" in out
    assert "score:" in out
    assert "8080" in out, "во фрагмент не попало релевантное содержимое"


def test_ask_reports_nothing_found(knowledge_dir):
    from memory_compiler.search import rebuild_index
    rebuild_index()
    out = _text(asyncio.run(ask("зурбаган квакозябра фырфырфыр", project="testproj")))
    assert "Не найдено информации" in out


def test_ask_falls_back_to_all_projects(knowledge_dir, monkeypatch):
    """Узкий скоуп промахивается по сущности из другого проекта — ask переспрашивает по всем."""
    import memory_compiler.search as _smod
    from memory_compiler.search import rebuild_index
    monkeypatch.setattr(_smod, "PROJECTS", ["testproj", "general"])
    _write(knowledge_dir, "general", "vpnhost.md", "Шлюз ВПН зурбаган",
           "адрес шлюза зурбаган для подключения", tags="vpn")
    rebuild_index()
    out = _text(asyncio.run(ask("шлюз зурбаган", project="testproj")))
    assert "general/vpnhost.md" in out
    assert "показаны результаты по всем проектам" in out


def test_ask_does_not_quote_secret_articles(knowledge_dir):
    """Регресс: ask резал фрагмент из сырого файла в обход проверки секретности."""
    from memory_compiler.search import rebuild_index
    _write(knowledge_dir, "testproj", "secret_vpn.md", "Доступ ВПН квакозябра",
           "пароль суперсекрет123 для квакозябра", tags="vpn", secret=True)
    rebuild_index()
    out = _text(asyncio.run(ask("квакозябра доступ", project="testproj")))
    if "secret_vpn.md" in out:
        assert "суперсекрет123" not in out, "секретное тело утекло во фрагмент ask"
        assert "зашифровано" in out


# ─── /api/ask ────────────────────────────────────────────────────────────────

class FakeRequest:
    def __init__(self, query=None):
        self.query_params = query or {}
        self.path_params = {}


def _json(resp):
    import json
    return json.loads(resp.body)


def test_web_ask_returns_structured_sources(knowledge_dir):
    """UI и ассистент отвечают одним конвейером: endpoint отдаёт те же источники."""
    from memory_compiler.api import web_ask
    from memory_compiler.search import rebuild_index
    _write(knowledge_dir, "testproj", "nginx.md", "Проксирование nginx",
           "настройка nginx для проксирования backend на порт 8080", tags="nginx")
    rebuild_index()
    d = _json(asyncio.run(web_ask(FakeRequest({"q": "настройка проксирования nginx",
                                               "project": "testproj"}))))
    assert d["question"] == "настройка проксирования nginx"
    assert d["answers"], "источники не вернулись"
    a = d["answers"][0]
    assert a["project"] == "testproj" and a["file"] == "nginx.md"
    assert a["title"] and "score" in a and "fragment" in a
    assert a["secret"] is False


def test_web_ask_empty_question(knowledge_dir):
    from memory_compiler.api import web_ask
    d = _json(asyncio.run(web_ask(FakeRequest({"q": "   "}))))
    assert d["answers"] == [] and d["fallback_all"] is False


def test_web_ask_marks_secret_without_leaking(knowledge_dir):
    """Секретная статья: флаг secret + ПУСТОЙ fragment, тело наружу не уходит."""
    from memory_compiler.api import web_ask
    from memory_compiler.search import rebuild_index
    _write(knowledge_dir, "testproj", "secret_vpn.md", "Доступ ВПН квакозябра",
           "пароль суперсекрет123 для квакозябра", tags="vpn", secret=True)
    rebuild_index()
    d = _json(asyncio.run(web_ask(FakeRequest({"q": "квакозябра доступ", "project": "testproj"}))))
    for a in d["answers"]:
        if a["file"] == "secret_vpn.md":
            assert a["secret"] is True
            assert a["fragment"] == ""
        assert "суперсекрет123" not in str(a)


def test_ask_top_k_is_bounded(knowledge_dir):
    """ask отдаёт горстку источников, а не всю выдачу — иначе «ответ» превращается в свалку."""
    from memory_compiler.handlers import ASK_TOP_K
    from memory_compiler.search import rebuild_index
    for i in range(12):
        _write(knowledge_dir, "testproj", f"doc{i}.md", f"Документ про nginx {i}",
               f"настройка nginx вариант {i}", tags="nginx")
    rebuild_index()
    out = _text(asyncio.run(ask("настройка nginx", project="testproj")))
    assert out.count("---\n**[") <= ASK_TOP_K
