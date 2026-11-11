"""Редактирование существующего секрета через авторизованный Web API."""
import asyncio

import pytest
from mcp.server import Server
from starlette.testclient import TestClient

from memory_compiler import api, config, storage
from memory_compiler.handlers import edit_article, save_secret


@pytest.fixture
def secret_article(knowledge_dir, monkeypatch):
    monkeypatch.setattr(config, "MC_ENCRYPT_KEY", "test-web-secret-edit")
    monkeypatch.setattr(api, "MC_API_KEY", "test-web-access")
    asyncio.run(save_secret("Веб правка", "исходное тело\n## Заголовок", "testproj", ["web"]))
    path, = (knowledge_dir / "testproj").glob("secret_*.md")
    url = f"/api/article/testproj/{path.name}"
    return path, url


@pytest.fixture
def client(secret_article):
    app = api.create_starlette_app(Server("test"))
    return TestClient(app, cookies={"mc_token": "test-web-access"})


def test_web_edits_existing_secret_in_place(client, secret_article):
    path, url = secret_article
    asyncio.run(edit_article("testproj", path.name, "дописанный блок", append=True))
    old_lines = path.read_text(encoding="utf-8").splitlines()

    article = client.get(url)
    assert article.json()["secret_editable"] is True
    assert article.headers["Cache-Control"] == "no-store"
    opened = client.get(url, params={"edit": "secret"})
    assert opened.headers["Cache-Control"] == "no-store"
    snapshot = opened.json()
    assert snapshot["blocks"] == ["исходное тело\n## Заголовок", "дописанный блок"]

    edited = "новое содержимое\n<script>sample</script>"
    response = client.patch(url, json={**snapshot, "blocks": [edited, "дописанный блок"]})
    assert response.status_code == 200
    assert list(path.parent.glob("secret_*.md")) == [path]
    new_lines = path.read_text(encoding="utf-8").splitlines()
    assert [line for line in old_lines if not storage.is_encrypted(line)] == [
        line for line in new_lines if not storage.is_encrypted(line)
    ]
    assert old_lines[-1] == new_lines[-1]
    assert edited.splitlines()[0] not in "\n".join(new_lines)
    assert [storage.decrypt_content(line) for line in new_lines if storage.is_encrypted(line)] == [
        edited, "дописанный блок",
    ]
    reopened = client.get(url).json()
    assert edited in reopened["content"]
    assert "<script>sample</script>" not in reopened["content_html"]

    stale = client.patch(url, json={**snapshot, "blocks": ["старая правка", "дописанный блок"]})
    assert stale.status_code == 409
    assert path.read_text(encoding="utf-8").splitlines() == new_lines


def test_web_secret_edit_requires_authentication(client, secret_article, monkeypatch):
    path, url = secret_article
    before = path.read_bytes()
    snapshot = client.get(url, params={"edit": "secret"}).json()
    payload = {**snapshot, "blocks": ["попытка правки"]}
    anonymous = TestClient(client.app)
    assert anonymous.get(url, params={"edit": "secret"}).status_code == 401
    assert anonymous.patch(url, json=payload).status_code == 401

    monkeypatch.setattr(api, "MC_API_KEY", "")
    open_app = TestClient(api.create_starlette_app(Server("open-test")))
    assert open_app.get(url, params={"edit": "secret"}).status_code == 403
    assert open_app.patch(url, json=payload).status_code == 403
    assert open_app.get(url).json().get("secret_editable") is not True
    assert path.read_bytes() == before


def test_web_secret_edit_rejects_wrong_key(client, secret_article, monkeypatch):
    path, url = secret_article
    before = path.read_bytes()
    snapshot = client.get(url, params={"edit": "secret"}).json()
    monkeypatch.setattr(config, "MC_ENCRYPT_KEY", "test-wrong-key")
    assert client.get(url, params={"edit": "secret"}).status_code == 422
    assert client.patch(url, json={**snapshot, "blocks": ["замена"]}).status_code == 422
    assert path.read_bytes() == before


def test_web_secret_edit_rejects_plain_article_and_invalid_payload(client, secret_article):
    path, url = secret_article
    before = path.read_bytes()
    plain_url = "/api/article/testproj/test_article.md"
    assert client.get(plain_url, params={"edit": "secret"}).status_code == 409
    snapshot = client.get(url, params={"edit": "secret"}).json()
    assert client.patch(plain_url, json=snapshot).status_code == 409
    assert client.patch(url, json={**snapshot, "blocks": []}).status_code == 400
    assert client.patch(url, json={**snapshot, "blocks": [None]}).status_code == 400
    assert path.read_bytes() == before
