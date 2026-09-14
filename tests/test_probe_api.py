"""Штамп пробы: быстро меняющееся состояние живёт в сайдкаре, не в теле статьи.

Писать штамп в статью значило бы git add -A на каждую команду к железу — 5,5 с и
десятки коммитов в день."""
import asyncio
import json

import pytest

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


# ─── цель списком и нижний регистр проекта (v1.79.0, N3 / M6) ──────────────────
NODE_ARTICLE = ("# Узел NODE\n\n**Дата:** 2026-09-13\n\n## Рефлексы\n- цель: node-demo\n\n"
                "## Записи\n\n### 2026-09-13\nработает\n")


def test_probe_accepts_target_list(knowledge_dir, monkeypatch):
    """Узел зовут именем ssh-соединения и адресом — карточка обязана найти статью в обоих.

    Хук шлёт цель СПИСКОМ (плюс в теле команды бывают посторонние адреса), reachable
    штампует статьи любой из целей."""
    import memory_compiler.api as api
    monkeypatch.setattr(reflexes, "REFLEX_RESCAN_SEC", 0)
    reflexes.invalidate()
    pdir = knowledge_dir / "testproj"
    (pdir / "router.md").write_text(ARTICLE, encoding="utf-8")     # цель: 192.0.2.10
    (pdir / "node.md").write_text(NODE_ARTICLE, encoding="utf-8")  # цель: node-demo
    monkeypatch.setattr(api, "MC_API_KEY", "k", raising=False)
    code, data = _probe({"target": ["node-demo", "192.0.2.10"], "level": "reachable"},
                        headers={"authorization": "Bearer k"})
    assert code == 200
    assert sorted(data["stamped"]) == ["testproj/node.md", "testproj/router.md"], data


def test_probe_rejects_too_many_or_broken_targets(knowledge_dir, monkeypatch):
    """Пусто / больше трёх / битый элемент / неверный level → 400 (после авторизации)."""
    import memory_compiler.api as api
    _router(knowledge_dir, monkeypatch)
    monkeypatch.setattr(api, "MC_API_KEY", "k", raising=False)
    for bad in ([], ["a", "b", "c", "d"], [""], [123], "   ", None):
        code, _ = _probe({"target": bad, "level": "reachable"},
                         headers={"authorization": "Bearer k"})
        assert code == 400, bad
    # валидная цель, но чужой уровень — тоже 400
    code, _ = _probe({"target": ["192.0.2.10"], "level": "нет"},
                     headers={"authorization": "Bearer k"})
    assert code == 400


def test_probe_string_target_still_works(knowledge_dir, monkeypatch):
    """Обратная совместимость: одиночная строка принимается как и раньше."""
    import memory_compiler.api as api
    _router(knowledge_dir, monkeypatch)
    monkeypatch.setattr(api, "MC_API_KEY", "k", raising=False)
    code, data = _probe({"target": "192.0.2.10", "level": "reachable"},
                        headers={"authorization": "Bearer k"})
    assert code == 200
    assert data["stamped"] == ["testproj/router.md"], data


def test_probe_normalizes_project_case(knowledge_dir, monkeypatch):
    """verified с project='Infra' штампует статью infra/... (нижний регистр).

    Сайдкар штампа кладётся по ключу project/file, а MCP-хендлеры пишут статьи под
    нормализованным (нижним) проектом. Без приведения штамп лёг бы на фантомный ключ
    Infra/router.md мимо настоящей статьи infra/router.md."""
    import memory_compiler.api as api
    monkeypatch.setattr(reflexes, "REFLEX_RESCAN_SEC", 0)
    reflexes.invalidate()
    monkeypatch.setattr(api, "MC_API_KEY", "k", raising=False)
    code, data = _probe({"target": "192.0.2.10", "level": "verified",
                         "project": "Infra", "file": "router.md"},
                        headers={"authorization": "Bearer k"})
    assert code == 200
    assert data["stamped"] == ["infra/router.md"], data
    assert cfg.article_meta["infra/router.md"]["last_probe"]["level"] == "verified"
    assert "Infra/router.md" not in cfg.article_meta   # фантомный ключ не создан


# ─── старшинство штампа: reachable не затирает вердикт по факту (v1.81.1) ─────────
def test_reachable_does_not_erase_fact_verdict(knowledge_dir):
    """«Узел отвечает» — свойство цели, verified/stale — свойство цитаты статьи.

    Слот штампа один, и раньше последний писал. Живая проверка 14.09.2026: verified в
    19:20:58, через секунду команда к тому же узлу без цитаты прислала reachable по той же
    цели, и в сайдкаре остался reachable. Карточка показывает ТОЛЬКО verified и stale, так
    что вердикт пропадал раньше, чем его кто-то видел, — а вместе с ним и предупреждение о
    протухшем факте."""
    for verdict in ("verified", "stale"):
        key = f"testproj/{verdict}.md"
        cfg.probe_stamp(key, verdict, save=False)
        before = dict(cfg.article_meta[key]["last_probe"])
        cfg.probe_stamp(key, "reachable", save=False)
        assert cfg.article_meta[key]["last_probe"] == before, verdict
    # Позитивный контроль: без вердикта reachable ложится как прежде. Без него проверка
    # выше зеленела бы и на поломке «reachable не пишет вовсе».
    cfg.probe_stamp("testproj/fresh.md", "reachable", save=False)
    assert cfg.article_meta["testproj/fresh.md"]["last_probe"]["level"] == "reachable"
    # Битый штамп (сайдкар правят руками) вердиктом не считается: карточка его и так не
    # показывает (probe_stamp_of), а запрет перезаписи оставил бы его навсегда.
    cfg.article_meta["testproj/broken.md"] = {"last_probe": {"level": "verified"}}
    cfg.probe_stamp("testproj/broken.md", "reachable", save=False)
    assert cfg.article_meta["testproj/broken.md"]["last_probe"]["level"] == "reachable"


def test_probe_endpoint_keeps_verdict_through_later_reachable(knowledge_dir, monkeypatch):
    """Сценарий инцидента целиком: через ручку и до карточки.

    Хук исполнил цитату — verified; следующая команда к тому же узлу без цитаты — reachable
    по той же цели. Вердикт обязан дожить до карточки, а ответ ручки — не врать, что статью
    проштамповали заново. Вторым проходом то же для stale (заодно verified → stale)."""
    import memory_compiler.api as api
    _router(knowledge_dir, monkeypatch)
    monkeypatch.setattr(api, "MC_API_KEY", "k", raising=False)
    auth = {"authorization": "Bearer k"}
    for verdict in ("verified", "stale"):
        code, _ = _probe({"target": "192.0.2.10", "level": verdict,
                          "project": "testproj", "file": "router.md"}, headers=auth)
        assert code == 200
        code, data = _probe({"target": ["192.0.2.10"], "level": "reachable"}, headers=auth)
        assert code == 200
        assert cfg.article_meta["testproj/router.md"]["last_probe"]["level"] == verdict
        assert "testproj/router.md" not in data["stamped"], (verdict, data)
        code, card = _call(web_reflex, {"kind": "target", "text": ["192.0.2.10"]})
        assert code == 200 and card["memos"], card
        assert card["memos"][0]["probe"]["level"] == verdict, (verdict, card["memos"][0])


def test_new_verdict_still_replaces_old_one(knowledge_dir):
    """Страж: сменить вердикт может только новый вердикт — повторный прогон цитаты.

    Без этой проверки «исправление», делающее штамп вечным, прошло бы тесты выше и сломало
    бы перепроверку: факт, починенный на узле, остался бы stale навсегда."""
    key = "testproj/router.md"
    cfg.probe_stamp(key, "stale", save=False)
    cfg.probe_stamp(key, "verified", save=False)
    assert cfg.article_meta[key]["last_probe"]["level"] == "verified"
    cfg.probe_stamp(key, "stale", save=False)
    assert cfg.article_meta[key]["last_probe"]["level"] == "stale"


# ─── вердикт по КАЖДОЙ цитате: у статьи их несколько, а слот штампа один ──────────
QUOTE_A = "/system identity print"
QUOTE_B = "/system resource print"
TWO_QUOTES = ("# Роутер KHV\n\n**Дата:** 2026-09-13\n\n## Рефлексы\n- цель: 192.0.2.10\n\n"
              f"## Проверка\n- команда: {QUOTE_A}\n- ожидается: KHV-GW\n"
              f"- команда: {QUOTE_B}\n- ожидается: RB5009\n\n"
              "## Записи\n\n### 2026-09-13\nработает\n")
KEY = "testproj/router.md"


def _two_quotes(knowledge_dir, monkeypatch):
    """Статья про узел с ДВУМЯ цитатами проверки; возвращает заголовки авторизации."""
    import memory_compiler.api as api
    monkeypatch.setattr(reflexes, "REFLEX_RESCAN_SEC", 0)
    reflexes.invalidate()
    (knowledge_dir / "testproj" / "router.md").write_text(TWO_QUOTES, encoding="utf-8")
    monkeypatch.setattr(api, "MC_API_KEY", "k", raising=False)
    return {"authorization": "Bearer k"}


def _verdict(headers, level, command=None):
    payload = {"target": "192.0.2.10", "level": level,
               "project": "testproj", "file": "router.md"}
    if command is not None:
        payload["command"] = command
    return _probe(payload, headers=headers)


def _card_level():
    """Уровень штампа, каким его видит карточка узла (тот же путь, что у хука)."""
    code, card = _call(web_reflex, {"kind": "target", "text": ["192.0.2.10"]})
    assert code == 200 and card["memos"], card
    return card["memos"][0]["probe"].get("level")


def test_verified_of_another_quote_does_not_erase_stale(knowledge_dir, monkeypatch):
    """Сценарий инцидента: цитата A протухла (stale), позже исполнили цитату B (verified).

    Слот штампа один на статью, поэтому verified затирал stale и предупреждение исчезало,
    хотя статья продолжала врать. Вердикт — свойство ЦИТАТЫ, а не статьи."""
    headers = _two_quotes(knowledge_dir, monkeypatch)
    assert _verdict(headers, "stale", QUOTE_A)[0] == 200
    assert _verdict(headers, "verified", QUOTE_B)[0] == 200
    checks = cfg.article_meta[KEY]["checks"]
    assert {k: v["level"] for k, v in checks.items()} == {QUOTE_A: "stale", QUOTE_B: "verified"}
    assert _card_level() == "stale"


def test_new_verdict_of_the_same_quote_replaces_it(knowledge_dir, monkeypatch):
    """Починенный на узле факт обязан выходить из stale: вердикт цитаты меняет ТОЛЬКО
    новый прогон ТОЙ ЖЕ цитаты. Иначе правка «stale навсегда» прошла бы тест выше."""
    headers = _two_quotes(knowledge_dir, monkeypatch)
    _verdict(headers, "stale", QUOTE_A)
    _verdict(headers, "verified", QUOTE_A)
    assert cfg.article_meta[KEY]["checks"][QUOTE_A]["level"] == "verified"
    assert _card_level() == "verified"


def test_command_key_collapses_whitespace(knowledge_dir, monkeypatch):
    """Ключ — команда со схлопнутыми пробелами: хук шлёт цитату так, как её прочитал, а в
    статье она может стоять с двойным пробелом. Нормализация ОДНА на запись и на чтение,
    иначе вердикт молча не найдёт свою цитату."""
    headers = _two_quotes(knowledge_dir, monkeypatch)
    assert _verdict(headers, "stale", f"  {QUOTE_A.replace(' ', '   ')} ")[0] == 200
    assert QUOTE_A in cfg.article_meta[KEY]["checks"]
    assert _card_level() == "stale"


@pytest.mark.parametrize("bad", [123, ["x"], {"a": 1}, None, "", "   ", "x" * 301])
def test_probe_rejects_broken_command(knowledge_dir, monkeypatch, bad):
    """Поле уходит в сайдкар и сравнивается с цитатой статьи: чужой тип, пустая строка и
    строка длиннее 300 символов отбиваются, а не оседают ключом, который не совпадёт ни с
    чем. `null` — тоже отказ: «поле есть» и «поля нет» обязаны различаться явно, иначе
    сломанный хук молча получал бы поведение старого. Проверка идёт ДО ветки уровня —
    reachable с кривым полем тоже 400."""
    headers = _two_quotes(knowledge_dir, monkeypatch)
    code, _ = _probe({"target": "192.0.2.10", "level": "verified", "project": "testproj",
                      "file": "router.md", "command": bad}, headers=headers)
    assert code == 400, bad
    code, _ = _probe({"target": "192.0.2.10", "level": "reachable", "command": bad},
                     headers=headers)
    assert code == 400, bad
    assert "last_probe" not in cfg.article_meta.get(KEY, {})


def test_command_of_max_length_is_accepted(knowledge_dir, monkeypatch):
    """Позитивный контроль к отказам: 300 символов после strip — ещё годная команда.
    Без него «отбиваем всё подряд» прошло бы тест выше."""
    headers = _two_quotes(knowledge_dir, monkeypatch)
    command = "show " + "x" * 295
    assert len(command) == 300
    assert _verdict(headers, "verified", f"  {command} ")[0] == 200
    assert command in cfg.article_meta[KEY]["checks"]


def test_reachable_does_not_write_checks(knowledge_dir, monkeypatch):
    """«Узел жив» — свойство цели, а не цитаты: слот цитаты им не занимаем и вердикт по
    факту им не подменяем (инвариант v1.81.1)."""
    headers = _two_quotes(knowledge_dir, monkeypatch)
    code, data = _probe({"target": "192.0.2.10", "level": "reachable", "command": QUOTE_A},
                        headers=headers)
    assert code == 200 and data["stamped"] == [KEY]
    assert "checks" not in cfg.article_meta[KEY]


def test_verdict_without_command_keeps_v1_81_1_behaviour(knowledge_dir, monkeypatch):
    """Старый хук поля не шлёт: вердикт ложится в last_probe, блок checks не заводится,
    карточка читает штамп по-прежнему. Хук выкатывается ПОСЛЕ сервера, поэтому этот путь
    обязан остаться рабочим."""
    headers = _two_quotes(knowledge_dir, monkeypatch)
    assert _verdict(headers, "stale")[0] == 200
    entry = cfg.article_meta[KEY]
    assert entry["last_probe"]["level"] == "stale" and "checks" not in entry
    assert _card_level() == "stale"


def test_checks_cap_keeps_the_freshest(knowledge_dir):
    """Потолок держит сайдкар в размере: цитаты переписывают, и ключей за годы накопится
    больше, чем цитат в статье. Вытесняется давняя запись, а повторный прогон уводит
    цитату в конец очереди — иначе действующую цитату вытеснил бы мусор от удалённых."""
    for i in range(cfg.PROBE_CHECKS_MAX + 5):
        cfg.probe_stamp(KEY, "verified", save=False, command=f"show cmd{i}")
    checks = cfg.article_meta[KEY]["checks"]
    assert len(checks) == cfg.PROBE_CHECKS_MAX
    assert "show cmd0" not in checks and f"show cmd{cfg.PROBE_CHECKS_MAX + 4}" in checks
    oldest = next(iter(checks))
    cfg.probe_stamp(KEY, "verified", save=False, command=oldest)
    cfg.probe_stamp(KEY, "verified", save=False, command="show fresh")
    assert oldest in cfg.article_meta[KEY]["checks"], "повторный прогон не обновил очередь"


def test_probe_stamp_ignores_command_for_reachable(knowledge_dir):
    """Тот же запрет, но на уровне хранилища: сюда ручка приходит не одна."""
    cfg.probe_stamp("testproj/node.md", "reachable", save=False, command=QUOTE_A)
    assert "checks" not in cfg.article_meta["testproj/node.md"]
