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


def _write(kd, project, name, title, body, tags="test", secret=False, contexts=None):
    """contexts=[(заголовок, ИИ-контекст), ...] добавляет YAML-frontmatter, как его
    пишет генератор контекста (v1.28.0). Фикстуры без него не воспроизводили реальный
    формат базы — из-за этого баг с цитированием шапки жил незамеченным."""
    p = kd / project
    p.mkdir(exist_ok=True)
    head = f"# {title}\n\n**Дата:** 2026-01-01 10:00\n**Проект:** {project}\n**Теги:** {tags}\n"
    if secret:
        head += "**Секрет:** да\n"
    fm = ""
    if contexts:
        fm = "---\ncontexts:\n" + "".join(
            f"  - heading: {h}\n    context: \"{c}\"\n" for h, c in contexts) + "---\n"
    (p / name).write_text(f"{fm}{head}\n## Записи\n\n### 2026-01-01 10:00\n{body}\n", encoding="utf-8")


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


def test_fragment_never_quotes_yaml_frontmatter():
    """Регресс: contexts:-frontmatter (v1.28.0) — это ИИ-пересказ ВСЕХ секций статьи,
    поэтому он матчит почти любой релевантный вопрос. Как нулевая «секция» он побеждал
    при равном скоре (первый максимум), и в UI уезжал сырой YAML вместо текста."""
    text = ("---\n"
            "contexts:\n"
            "  - heading: Доступы\n"
            '    context: "Раздел про запуск сайта example.ru: багфиксы и вёрстка."\n'
            "---\n"
            "# Запуск сайта example.ru\n"
            "\n"
            "**Дата:** 2026-01-01 10:00\n"
            "**Теги:** deploy\n"
            "\n"
            "## Записи\n"
            "\n"
            "### Доступы\n"
            "пароль администратора example.ru хранится в vault\n")
    frag = ask_fragment(text, "какой пароль у сайта example.ru")
    assert "contexts:" not in frag and "heading:" not in frag, "во фрагмент утёк YAML-frontmatter"
    assert "vault" in frag


def test_fragment_never_quotes_article_header():
    """Шапка статьи — метаданные, а не ответ. Заголовок и **Дата:**/**Теги:** матчат
    слова вопроса не хуже тела (а часто лучше — там название сущности), но цитировать
    их бессмысленно. Второй вариант той же поломки, уже без frontmatter."""
    text = ("# Настройка nginx на example.ru\n"
            "\n"
            "**Дата:** 2026-01-01 10:00\n"
            "**Проект:** testproj\n"
            "**Теги:** nginx\n"
            "\n"
            "## Записи\n"
            "\n"
            "### 2026-01-01 10:00\n"
            "проксирование nginx на порт 8080\n")
    frag = ask_fragment(text, "настройка nginx example.ru")
    assert "**Дата:**" not in frag and "**Теги:**" not in frag, "во фрагмент утекла меташапка"
    assert "8080" in frag


def test_fragment_still_works_without_sections():
    """Статьи без '### '-секций (ingest/импорт — около 15% базы) держат тело сразу
    после заголовка. Шапку нельзя выбрасывать целиком: у них там весь контент."""
    text = ("# Импортированная заметка\n"
            "\n"
            "Проксирование nginx настроено на порт 8080.\n")
    assert "8080" in ask_fragment(text, "nginx проксирование")


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


def test_secret_findable_by_credential_intent(knowledge_dir):
    """Тело секрета не индексируется, а заголовки у них звучат как «Полные доступы …»
    или «SSH/SFTP креды …» — слова «пароль» там нет ни разу. Поэтому вопрос «какой
    пароль у X» цеплялся за секрет ровно одним словом (именем сущности) и проигрывал
    обычным статьям. Синонимы интента подставляются НА ИНДЕКСАЦИИ (_index_safe_text),
    файл на диске не меняется, и это разом покрывает все существующие секреты."""
    from memory_compiler.search import rebuild_index
    _write(knowledge_dir, "testproj", "secret_dostupy.md", "Полные доступы квакозябра",
           "тело секрета", tags="secret", secret=True)
    rebuild_index()
    out = _text(asyncio.run(ask("какой пароль у квакозябра", project="testproj")))
    assert "secret_dostupy.md" in out, "секрет не находится по слову «пароль»"
    assert "зашифровано" in out, "и при этом он обязан остаться замаскированным"


def test_ask_answers_carry_content_not_metadata(knowledge_dir):
    """Сквозная проверка: в выдаче ask не должно быть ни YAML-frontmatter, ни меташапки.
    На живой базе так выглядели 5 фрагментов из 5 — ответ не содержал ни строчки контента."""
    from memory_compiler.search import rebuild_index
    _write(knowledge_dir, "testproj", "launch.md", "Запуск сайта квакозябра",
           "пароль администратора квакозябра лежит в vault", tags="deploy",
           contexts=[("Записи", "Раздел про запуск сайта квакозябра: багфиксы и вёрстка.")])
    rebuild_index()
    out = _text(asyncio.run(ask("какой пароль у сайта квакозябра", project="testproj")))
    assert "contexts:" not in out and "heading:" not in out
    assert "**Дата:**" not in out and "**Теги:**" not in out
    assert "vault" in out, "ответ не содержит ни строчки контента статьи"


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
