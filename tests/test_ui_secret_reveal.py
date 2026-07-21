"""Раскрытие секрета во вкладке «Ответы» Web UI.

Маскировка там жёсткая: ask_sources НЕ кладёт тело секрета в ответ вовсе
(fragment=""), поэтому «показать» — это не снятие CSS-маски, а дотягивание статьи.
Канал для этого уже есть и уже расшифровывает — /api/article под тем же ключом, что
и весь Web UI. Новый эндпоинт не заводится СПЕЦИАЛЬНО: лишняя точка выдачи plaintext
— это лишняя поверхность, а маска во вкладке ответов защищает от случайного взгляда
в списке, а не от того, кто уже прошёл авторизацию.
"""
import asyncio
import json
import re
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "memory_compiler" / "ui.py"
SRC = UI.read_text(encoding="utf-8")


class FakeRequest:
    def __init__(self, query=None, path=None):
        self.query_params = query or {}
        self.path_params = path or {}


def _json(resp):
    return json.loads(resp.body)


def _js_function(name: str) -> str:
    """Тело функции из ui.py по имени — до строки, начинающейся с '}' в нулевой колонке."""
    m = re.search(rf"^(?:async )?function {name}\(.*?^}}", SRC, re.S | re.M)
    assert m, f"в ui.py нет функции {name}"
    return m.group(0)


# ─── контракт бэкенда, на который опирается кнопка ──────────────────────────
# Тест ПИНУЕТ существующее поведение (не TDD-driven): кнопка ничего не расшифровывает
# сама, она лишь показывает то, что endpoint и так отдаёт. Если fail-closed сломается,
# упадёт здесь, а не «когда-нибудь в браузере».

def test_article_endpoint_decrypts_secret_under_auth(knowledge_dir, monkeypatch):
    import memory_compiler.config as cfg
    from memory_compiler.storage import encrypt_content
    from memory_compiler import api

    monkeypatch.setattr(cfg, "MC_ENCRYPT_KEY", "test-encrypt-key-123")
    monkeypatch.setattr(api, "MC_API_KEY", "test-api-key-456")

    body = encrypt_content("Пароль от стенда: P@ssw0rd!")
    (knowledge_dir / "testproj" / "secret_probe.md").write_text(
        f"# Проба\n\n**Секрет:** да\n\n## Содержание\n\n{body}\n", encoding="utf-8")

    d = _json(asyncio.run(api.web_article(FakeRequest(
        path={"project": "testproj", "filename": "secret_probe.md"}))))
    assert "P@ssw0rd!" in d["content"], "секрет не расшифрован при настроенном ключе доступа"


def test_article_endpoint_keeps_ciphertext_without_api_key(knowledge_dir, monkeypatch):
    """Fail-closed: без MC_API_KEY endpoint не раскрывает секрет — иначе шифрование
    на диске не защищало бы ни от чего."""
    import memory_compiler.config as cfg
    from memory_compiler.storage import encrypt_content
    from memory_compiler import api

    monkeypatch.setattr(cfg, "MC_ENCRYPT_KEY", "test-encrypt-key-123")
    monkeypatch.setattr(api, "MC_API_KEY", "")

    body = encrypt_content("Пароль от стенда: P@ssw0rd!")
    (knowledge_dir / "testproj" / "secret_probe2.md").write_text(
        f"# Проба\n\n**Секрет:** да\n\n## Содержание\n\n{body}\n", encoding="utf-8")

    d = _json(asyncio.run(api.web_article(FakeRequest(
        path={"project": "testproj", "filename": "secret_probe2.md"}))))
    assert "P@ssw0rd!" not in d["content"], "секрет раскрыт при пустом ключе доступа"
    assert "ENC:" in d["content"]


# ─── сама кнопка ─────────────────────────────────────────────────────────────

def test_secret_answer_offers_reveal_control():
    """У секретного источника во вкладке «Ответы» есть управление раскрытием.

    Раньше там стояла статическая подпись «откройте статью для просмотра» — тупик:
    пользователь уже нашёл нужное, но должен уйти на другую вкладку."""
    render = _js_function("doAsk")
    assert "askReveal(" in render, "секретный источник не предлагает раскрытие"


def test_reveal_uses_existing_article_endpoint():
    """Раскрытие идёт через /api/article — НЕ через новый эндпоинт выдачи plaintext."""
    reveal = _js_function("askReveal")
    assert '"/api/article/' in reveal, "раскрытие не использует существующий endpoint статьи"
    assert "fetch(" in reveal


def test_reveal_labels_are_translated():
    """Подписи кнопки — через словарь: гейт i18n требует пары ru/en для каждого t()."""
    reveal_keys = set(re.findall(r"""\bt\(\s*["'](ask\.secret[^"']*)["']\s*\)""", SRC))
    assert reveal_keys, "подписи раскрытия не берутся из словаря"
