"""Тесты целостности и конкурентности (v1.7.26).

Аудит выявил:
  - #5  stale-ссылка _embeddings: delete_article/remove_project импортируют объект по
        имени; после фонового rebuild_embeddings он переприсваивается → .pop по старому
        объекту → удалённая статья остаётся фантомом в semantic-поиске;
  - #13 неатомарная запись .article_meta.json/index.md/pickle → краш при docker restart
        (= деплой) оставляет полуфайл; .article_meta.json при этом обнуляет всю аналитику;
  - #3  гонки reindex (фон) vs embed/search (event loop) на общем _embeddings/pickle.
"""
import asyncio
import json
import threading

import numpy as np


# ─── #5 stale-ссылка _embeddings ─────────────────────────────────────────────

def test_delete_article_clears_current_embeddings(knowledge_dir):
    """delete_article чистит АКТУАЛЬНЫЙ search._embeddings, а не устаревшую ссылку
    (после свопа в rebuild_embeddings объект другой)."""
    import memory_compiler.search as search_mod
    from memory_compiler.handlers import delete_article
    proj = knowledge_dir / "testproj"
    (proj / "todelete.md").write_text("# Del\n\nтело", encoding="utf-8")
    # Симулируем состояние ПОСЛЕ свопа: search._embeddings — новый объект.
    search_mod._embeddings = {
        "testproj/todelete.md": np.array([1.0, 0.0]),
        "testproj/todelete.md#c1": np.array([0.5, 0.5]),
    }
    search_mod._embed_texts = {"testproj/todelete.md": "Del"}
    asyncio.run(delete_article("testproj", "todelete.md"))
    assert "testproj/todelete.md" not in search_mod._embeddings, "статья осталась в _embeddings"
    assert "testproj/todelete.md#c1" not in search_mod._embeddings, "чанк остался в _embeddings"
    assert "testproj/todelete.md" not in search_mod._embed_texts


def test_remove_project_clears_current_embeddings(knowledge_dir):
    """remove_project тоже чистит актуальный объект _embeddings."""
    import memory_compiler.search as search_mod
    import memory_compiler.config as cfg
    from memory_compiler.handlers import remove_project
    proj = knowledge_dir / "delproj"
    proj.mkdir()
    (proj / "a.md").write_text("# A\n\nтело", encoding="utf-8")
    cfg.PROJECTS = cfg._discover_projects()
    search_mod._embeddings = {"delproj/a.md": np.array([1.0, 0.0])}
    search_mod._embed_texts = {"delproj/a.md": "A"}
    asyncio.run(remove_project("delproj", confirm=True))
    assert "delproj/a.md" not in search_mod._embeddings, "статья удалённого проекта осталась фантомом"


# ─── #13 атомарная запись ────────────────────────────────────────────────────

def test_save_article_meta_atomic(knowledge_dir):
    """save_article_meta пишет атомарно — JSON валиден, .tmp-остатков нет."""
    import memory_compiler.config as cfg
    cfg.article_meta = {"testproj/a.md": {"access_count": 3, "created": "2026-01-01"}}
    cfg.save_article_meta()
    p = cfg.ARTICLE_META_PATH
    assert p.exists()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["testproj/a.md"]["access_count"] == 3
    leftovers = list(p.parent.glob(f".{p.name}.*"))
    assert not leftovers, f"остались tmp-файлы: {leftovers}"


def test_atomic_write_text_roundtrip(knowledge_dir):
    """atomic_write_text перезаписывает без полуфайла и без tmp-мусора."""
    from memory_compiler.config import atomic_write_text
    target = knowledge_dir / "atomic_target.txt"
    atomic_write_text(target, "первая версия")
    assert target.read_text(encoding="utf-8") == "первая версия"
    atomic_write_text(target, "вторая версия")
    assert target.read_text(encoding="utf-8") == "вторая версия"
    assert not list(knowledge_dir.glob(".atomic_target.txt.*")), "tmp-файл не убран"


# ─── lost-update / zombie при свопе rebuild_embeddings ───────────────────────

def test_embed_during_rebuild_survives_swap(knowledge_dir, monkeypatch):
    """Статья, сохранённая ПОКА rebuild_embeddings кодирует базу (encode идёт вне
    лока, на живой базе ~35-40 мин), не должна теряться при свопе новых диктов."""
    import memory_compiler.search as search_mod
    state = {"rebuild_encode": True}

    def fake_encode(texts):
        if state["rebuild_encode"]:
            state["rebuild_encode"] = False  # не рекурсим на encode от embed_document
            # конкурентное сохранение во время «долгого» encode пересборки
            search_mod.embed_document("# Fresh\n\nтело", "fresh.md", "testproj")
        return [np.array([1.0, 0.0]) for _ in texts]

    monkeypatch.setattr(search_mod, "encode_passages", fake_encode)
    search_mod._embeddings.clear()
    search_mod._embed_texts.clear()
    search_mod._chunk_hashes.clear()
    search_mod.rebuild_embeddings()
    assert "testproj/fresh.md" in search_mod._embeddings, \
        "свежесохранённая статья потеряна при свопе rebuild_embeddings"
    assert "testproj/fresh.md" in search_mod._embed_texts


def test_delete_during_rebuild_no_zombie(knowledge_dir, monkeypatch):
    """Статья, удалённая ПОКА rebuild_embeddings кодирует (файл уже прочитан с
    диска пересборкой), не должна «воскресать» при свопе."""
    import memory_compiler.search as search_mod
    state = {"rebuild_encode": True}

    def fake_encode(texts):
        if state["rebuild_encode"]:
            state["rebuild_encode"] = False
            search_mod.remove_embedding("testproj/test_article.md")
        return [np.array([1.0, 0.0]) for _ in texts]

    monkeypatch.setattr(search_mod, "encode_passages", fake_encode)
    search_mod._embeddings.clear()
    search_mod._embed_texts.clear()
    search_mod._chunk_hashes.clear()
    search_mod.rebuild_embeddings()
    zombie = [k for k in search_mod._embeddings if k.startswith("testproj/test_article.md")]
    assert not zombie, f"удалённая статья воскресла при свопе: {zombie}"


def test_delete_article_persists_pkl(knowledge_dir):
    """После delete_article статьи нет и в .embeddings.pkl — иначе после рестарта
    сервер поднимет её из кэша фантомом (файл удалён, а semantic её находит)."""
    import pickle
    import memory_compiler.search as search_mod
    from memory_compiler.handlers import delete_article
    proj = knowledge_dir / "testproj"
    (proj / "todel2.md").write_text("# Del2\n\nтело", encoding="utf-8")
    search_mod._embeddings["testproj/todel2.md"] = np.array([1.0, 0.0])
    search_mod._embed_texts["testproj/todel2.md"] = "Del2"
    asyncio.run(delete_article("testproj", "todel2.md"))
    data = pickle.loads((knowledge_dir / ".embeddings.pkl").read_bytes())
    assert "testproj/todel2.md" not in data["embeddings"], "фантом в pkl после удаления"


# ─── #3 конкурентность embed/search ──────────────────────────────────────────

def test_concurrent_embed_and_search_no_crash(knowledge_dir, monkeypatch):
    """Одновременные embed_document (мутация _embeddings + запись pickle) и
    semantic_search (итерация по _embeddings) не должны падать
    (RuntimeError: dictionary changed size during iteration / торн pickle)."""
    import memory_compiler.search as search_mod
    monkeypatch.setattr(search_mod, "encode_passages",
                        lambda texts: [np.array([1.0, 0.0]) for _ in texts])
    monkeypatch.setattr(search_mod, "encode_query", lambda q: np.array([1.0, 0.0]))
    monkeypatch.setattr(search_mod, "get_embed_model", lambda: object())
    search_mod._embeddings = {f"testproj/a{i}.md": np.array([1.0, 0.0]) for i in range(60)}
    search_mod._embed_texts = {}
    errors = []

    def writer():
        try:
            for i in range(120):
                search_mod.embed_document(f"# t{i}\n\nтело статьи {i}", f"w{i}.md", "testproj")
        except Exception as e:  # noqa
            errors.append(repr(e))

    def reader():
        try:
            for _ in range(240):
                search_mod.semantic_search("запрос", limit=5)
        except Exception as e:  # noqa
            errors.append(repr(e))

    threads = [threading.Thread(target=writer),
               threading.Thread(target=reader),
               threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"конкурентный доступ упал: {errors[:3]}"
    # pickle на диске читается (не торн)
    from memory_compiler.search import load_embeddings
    assert load_embeddings() in (True, False)  # не кидает исключение
