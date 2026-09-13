"""Штамп пробы: быстро меняющееся состояние живёт в сайдкаре, не в теле статьи.

Писать штамп в статью значило бы git add -A на каждую команду к железу — 5,5 с и
десятки коммитов в день."""
import asyncio
import json

import memory_compiler.config as cfg
from memory_compiler import reflexes
from memory_compiler.api import web_reflex

ARTICLE = ("# Роутер KHV\n\n**Дата:** 2026-09-13\n\n## Рефлексы\n- цель: 192.0.2.10\n\n"
           "## Проверка\n- команда: /system identity print\n- ожидается: KHV-GW\n\n"
           "## Записи\n\n### 2026-09-13\nработает\n")


class JsonRequest:
    """Стенд под starlette.Request: ручка читает только await request.json().

    Клиента Starlette в проекте нет (httpx в requirements.txt тоже нет) — endpoint'ы
    зовутся напрямую, как в test_reflex_api.py и test_web_api.py."""

    def __init__(self, payload=None, raw=None, headers=None):
        self._payload, self._raw = payload, raw
        # Заголовки нужны пишущей ручке: она сама проверяет Bearer, потому что
        # AuthMiddleware монтируется только при заданном MC_API_KEY.
        self.headers = headers or {}
        self.cookies = {}

    async def json(self):
        return json.loads(self._raw) if self._raw is not None else self._payload


def _call(handler, payload, headers=None):
    resp = asyncio.run(handler(JsonRequest(payload, headers=headers)))
    return resp.status_code, json.loads(resp.body)


def _probe(payload, headers=None):
    # Импорт внутри: пока ручки нет, красный виден на трёх новых тестах, а не на сборе файла.
    from memory_compiler.api import web_probe
    return _call(web_probe, payload, headers=headers)


def _router(knowledge_dir, monkeypatch):
    """Статья про узел с триггером «цель:» и цитатой проверки."""
    monkeypatch.setattr(reflexes, "REFLEX_RESCAN_SEC", 0)
    reflexes.invalidate()
    (knowledge_dir / "testproj" / "router.md").write_text(ARTICLE, encoding="utf-8")


def test_probe_stamp_writes_sidecar_not_article(knowledge_dir):
    article = knowledge_dir / "testproj" / "router.md"
    article.write_text("# Роутер\n", encoding="utf-8")
    before = article.read_text(encoding="utf-8")
    cfg.probe_stamp("testproj/router.md", "verified")
    meta = cfg.article_meta["testproj/router.md"]["last_probe"]
    assert meta["level"] == "verified" and meta["date"][:2] == "20"
    assert article.read_text(encoding="utf-8") == before, "тело статьи не трогаем"


def test_probe_marks_only_the_article_whose_quote_ran(knowledge_dir, monkeypatch):
    """`verified` относится к КОНКРЕТНОМУ факту, а не ко всей цели.

    По адресу узла находится и статья с цитатой, и секрет с доступами, попавший по адресу
    в заголовке. Штамп «проверено живой командой» на секрете означал бы, что его данные
    кто-то сверял, — а его команду никто не запускал. Фича против ложного доверия сама бы
    его и создавала (ревью 13.09.2026)."""
    import memory_compiler.api as api
    monkeypatch.setattr(reflexes, "REFLEX_RESCAN_SEC", 0)
    reflexes.invalidate()
    pdir = knowledge_dir / "testproj"
    (pdir / "router.md").write_text(ARTICLE, encoding="utf-8")
    (pdir / "secret_router.md").write_text(
        "# Доступы 192.0.2.10\n\n**Дата:** 2026-09-13\n**Секрет:** да\n\nENC:xxx\n",
        encoding="utf-8")
    monkeypatch.setattr(api, "MC_API_KEY", "k", raising=False)
    status, _body = _probe({"target": "192.0.2.10", "level": "verified",
                            "project": "testproj", "file": "router.md"},
                           headers={"authorization": "Bearer k"})
    assert status == 200
    assert cfg.article_meta["testproj/router.md"]["last_probe"]["level"] == "verified"
    assert "last_probe" not in cfg.article_meta.get("testproj/secret_router.md", {})


def test_probe_requires_auth_when_key_is_empty(knowledge_dir, monkeypatch):
    """Ручка ПИШЕТ в состояние базы, значит открытой быть не может.

    AuthMiddleware монтируется только при заданном MC_API_KEY: при пустом ключе ручка
    осталась бы доступна любому, кто дотянулся до порта, и позволяла бы пометить чужие
    факты протухшими. Fail-closed, как у _maybe_decrypt_secret_lines."""
    import memory_compiler.api as api
    _router(knowledge_dir, monkeypatch)
    monkeypatch.setattr(api, "MC_API_KEY", "", raising=False)
    status, _body = _probe({"target": "192.0.2.10", "level": "stale"})
    assert status in (401, 403)
    assert "last_probe" not in cfg.article_meta.get("testproj/router.md", {})


def test_probe_saves_sidecar_once_per_request(knowledge_dir, monkeypatch):
    """Полная перезапись .article_meta.json на каждую статью — синхронно в event loop.

    Тот самый класс, которым в проекте уже дважды вешали сервер (git add -A в хендлере)."""
    import memory_compiler.api as api
    monkeypatch.setattr(reflexes, "REFLEX_RESCAN_SEC", 0)
    reflexes.invalidate()
    pdir = knowledge_dir / "testproj"
    for name in ("router.md", "router2.md"):
        (pdir / name).write_text(ARTICLE, encoding="utf-8")
    monkeypatch.setattr(api, "MC_API_KEY", "k", raising=False)
    saves = []
    monkeypatch.setattr(cfg, "save_article_meta", lambda: saves.append(1))
    _probe({"target": "192.0.2.10", "level": "reachable"},
           headers={"authorization": "Bearer k"})
    assert len(saves) <= 1, f"сайдкар переписан {len(saves)} раз за один запрос"


def test_probe_stamp_rejects_unknown_level(knowledge_dir):
    cfg.probe_stamp("testproj/router2.md", "чепуха")
    assert "last_probe" not in cfg.article_meta.get("testproj/router2.md", {})


def test_probe_endpoint_stamps_articles_of_target(knowledge_dir, monkeypatch):
    """«Узел жив» садится на все статьи этой цели — и ручка смонтирована в приложении.

    Уровень именно `reachable`: он относится к цели. У `verified`/`stale` контракт строже —
    им обязателен ключ статьи, чью цитату исполнял хук (см. соседний тест)."""
    from mcp.server import Server
    import memory_compiler.api as api
    from memory_compiler.api import create_starlette_app

    _router(knowledge_dir, monkeypatch)
    monkeypatch.setattr(api, "MC_API_KEY", "k", raising=False)
    code, data = _probe({"target": "192.0.2.10", "level": "reachable"},
                        headers={"authorization": "Bearer k"})
    assert code == 200
    assert data["stamped"] == ["testproj/router.md"], data
    assert cfg.article_meta["testproj/router.md"]["last_probe"]["level"] == "reachable"
    paths = [getattr(r, "path", None) for r in create_starlette_app(Server("test")).routes]
    assert "/api/probe" in paths, f"ручка не смонтирована: {paths}"


def test_probe_endpoint_rejects_unknown_level(knowledge_dir, monkeypatch):
    """Уровень — закрытый список: чужое слово не должно оседать в сайдкаре.

    Ключ валидный намеренно: иначе запрос отбивался бы авторизацией и проверка уровня
    оставалась бы недостижимой — тест зеленел бы по посторонней причине."""
    import memory_compiler.api as api
    _router(knowledge_dir, monkeypatch)
    monkeypatch.setattr(api, "MC_API_KEY", "k", raising=False)
    code, data = _probe({"target": "192.0.2.10", "level": "ок"},
                        headers={"authorization": "Bearer k"})
    assert code == 400, data
    assert "last_probe" not in cfg.article_meta.get("testproj/router.md", {})


def test_reflex_memo_carries_verification_quote(knowledge_dir, monkeypatch):
    """Карточка узла несёт цитату: сверять с боевым выводом будет клиент, не сервер."""
    _router(knowledge_dir, monkeypatch)
    code, data = _call(web_reflex, {"kind": "target", "text": ["192.0.2.10"]})
    assert code == 200
    assert data["memos"], data
    # В JSON кортежи пар становятся списками.
    assert data["memos"][0]["verify"] == [["/system identity print", "KHV-GW"]]
