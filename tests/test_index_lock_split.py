"""Разделение _index_lock (25.09.2026): Whoosh и эмбеддинги — под разными замками.

Пересборка Whoosh держит свой замок ~4,5 мин (замер 25.09.2026: 270 с на 4443
документах, 99% — запись в Whoosh). Под общим замком поиск, снимок эмбеддингов,
«похожие» и запись вектора ждали её целиком. Поток, держащий _ix_lock, их не задерживает.
"""
import threading
import time

import numpy as np
import pytest

HOLD_SEC = 1.5        # столько «пересборка» держит замок в соседнем потоке
WAIT_MAX_SEC = 0.5    # дольше путь эмбеддингов ждать не вправе


def _hold(lock, held, seconds):
    with lock:
        held.set()
        time.sleep(seconds)


def _elapsed_while_held(lock, fn):
    """Сколько длится fn(), пока соседний поток держит lock HOLD_SEC секунд."""
    held = threading.Event()
    holder = threading.Thread(target=_hold, args=(lock, held, HOLD_SEC), daemon=True)
    holder.start()
    assert held.wait(5), "поток-держатель не взял замок"
    t0 = time.monotonic()
    fn()
    spent = time.monotonic() - t0
    holder.join()
    return spent


@pytest.fixture
def emb(monkeypatch):
    """Два вектора в словаре эмбеддингов и модель без модели: к делу относится замок."""
    import memory_compiler.search as sm
    vec = np.array([1.0, 0.0], dtype=np.float32)
    monkeypatch.setattr(sm, "_embeddings", {
        "testproj/test_article.md": vec,
        "testproj/other.md": np.array([0.8, 0.6], dtype=np.float32)})
    monkeypatch.setattr(sm, "get_embed_model", lambda: object())
    monkeypatch.setattr(sm, "encode_query", lambda text: vec)
    monkeypatch.setattr(sm, "encode_passages", lambda texts, progress_label=None: [vec for _ in texts])
    return sm


@pytest.mark.parametrize("name", ["semantic_search", "snapshot_embeddings",
                                  "related_articles", "embed_document"])
def test_embedding_paths_do_not_wait_whoosh_lock(emb, name):
    sm = emb
    calls = {
        "semantic_search": lambda: sm.semantic_search("docker", limit=5),
        "snapshot_embeddings": sm.snapshot_embeddings,
        "related_articles": lambda: sm.related_articles("testproj/test_article.md", limit=5),
        "embed_document": lambda: sm.embed_document("# Новая\n\nтекст\n", "new.md", "testproj"),
    }
    spent = _elapsed_while_held(sm._ix_lock, calls[name])
    assert spent < WAIT_MAX_SEC, (
        f"{name} ждал Whoosh-замок {spent:.2f} с — эмбеддинги снова под общим замком")


def test_whoosh_writer_still_waits_whoosh_lock(knowledge_dir):
    """Позитивный контроль: запись в Whoosh вне пересборки ждёт _ix_lock. Без него тест
    выше прошёл бы и на коде, где замков нет вовсе."""
    import memory_compiler.search as sm
    sm.get_index()
    spent = _elapsed_while_held(
        sm._ix_lock, lambda: sm.index_document("# Док\n\nтекст\n", "doc.md", "testproj"))
    assert spent >= HOLD_SEC * 0.8, f"запись в Whoosh не ждала замок: {spent:.2f} с"


# ─── очередь записей в индекс на время пересборки ─────────────────────────────
# Пересборка Whoosh держит _ix_lock ~4,5 мин, и запись в базу ждала её целиком. Теперь
# index_document во время пересборки кладёт поля статьи в очередь, а rebuild_index
# пишет её отдельным writer'ом сразу после своего commit.

REBUILD = "rebuild"   # имя потока пересборки: замедление и подмены срабатывают только в нём


@pytest.fixture
def slow_rebuild(knowledge_dir, monkeypatch):
    """Настоящий rebuild_index, замедленный на 0,3 с на каждый файл скана."""
    import memory_compiler.search as sm
    monkeypatch.setattr(sm, "PROJECTS", ["testproj", "general"])
    for i in range(3):
        (knowledge_dir / "testproj" / f"extra{i}.md").write_text(
            f"# Extra {i}\n\n**Теги:** t\n\nтекст {i}\n", encoding="utf-8")
    sm.get_index()
    real_parse = sm._parse_article

    def parse(text, filename, project):
        if threading.current_thread().name == REBUILD:
            time.sleep(0.3)
        return real_parse(text, filename, project)

    monkeypatch.setattr(sm, "_parse_article", parse)
    yield sm
    # Поток пересборки, переживший тест, держит модульный _ix_lock: следующие тесты
    # воркера, пишущие в индекс, повисли бы навсегда вместо падения.
    for th in threading.enumerate():
        if th.name == REBUILD:
            th.join(30)
            if th.is_alive():
                pytest.exit("поток пересборки не завершился — _ix_lock занят, дальше прогон повиснет",
                            returncode=1)


def _start(sm):
    """Запустить пересборку в потоке и дождаться флага. Итог — в box."""
    box = {}

    def run():
        try:
            box["count"] = sm.rebuild_index()
        except BaseException as e:   # noqa: BLE001 — тест проверяет и сбой
            box["error"] = e

    t = threading.Thread(target=run, name=REBUILD, daemon=True)
    t.start()
    deadline = time.monotonic() + 5
    while not sm._ix_rebuilding["v"]:
        assert time.monotonic() < deadline, "пересборка не подняла флаг"
        time.sleep(0.01)
    return t, box


def _paths_with(sm, word):
    """Пути статей, в тексте которых индекс находит слово."""
    from whoosh.qparser import QueryParser
    ix = sm.get_index()
    with ix.searcher() as s:
        q = QueryParser("body", ix.schema).parse(word)
        return {hit["path"] for hit in s.search(q, limit=None)}


def test_write_during_rebuild_returns_at_once_and_lands_after_it(slow_rebuild):
    sm = slow_rebuild
    t, box = _start(sm)
    t0 = time.monotonic()
    sm.index_document("# Во время\n\n**Теги:** t\n\nzqqueuedone\n", "queued.md", "testproj")
    spent = time.monotonic() - t0
    t.join(10)
    assert spent < WAIT_MAX_SEC, f"запись ждала пересборку {spent:.2f} с"
    assert "error" not in box, box
    assert "testproj/queued.md" in _paths_with(sm, "zqqueuedone")
    assert not sm._ix_pending and not sm._ix_rebuilding["v"]


def test_last_queued_write_of_a_path_wins(slow_rebuild):
    sm = slow_rebuild
    t, _box = _start(sm)
    sm.index_document("# Док\n\n**Теги:** t\n\nzqfirstversion\n", "twice.md", "testproj")
    sm.index_document("# Док\n\n**Теги:** t\n\nzqsecondversion\n", "twice.md", "testproj")
    t.join(10)
    assert "testproj/twice.md" in _paths_with(sm, "zqsecondversion")
    assert "testproj/twice.md" not in _paths_with(sm, "zqfirstversion")


def test_write_during_commit_is_not_stuck_in_the_queue(slow_rebuild, monkeypatch):
    """Запись, вставшая в очередь за время commit пересборки, дописывается хвостом."""
    sm = slow_rebuild
    ix = sm.get_index()
    real_writer = ix.writer
    fired = {"v": False}

    def writer(*a, **k):
        w = real_writer(*a, **k)
        if threading.current_thread().name == REBUILD and not fired["v"]:
            fired["v"] = True
            real_commit = w.commit

            def commit(*ca, **ck):
                sm.index_document("# Коммит\n\n**Теги:** t\n\nzqduringcommit\n",
                                  "during_commit.md", "testproj")
                return real_commit(*ca, **ck)

            w.commit = commit
        return w

    monkeypatch.setattr(ix, "writer", writer)
    t, box = _start(sm)
    t.join(10)
    assert fired["v"], "подмена commit не сработала — тест ничего не проверил"
    assert "error" not in box, box
    assert "testproj/during_commit.md" in _paths_with(sm, "zqduringcommit")
    assert not sm._ix_pending


def test_queued_shared_article_becomes_cross_project(slow_rebuild):
    sm = slow_rebuild
    t, _box = _start(sm)
    sm.index_document("# Общая\n\n**Теги:** shared\n\nzqshared\n", "common.md", "testproj")
    t.join(10)
    assert "testproj/common.md" in sm._shared_paths


def test_failed_rebuild_still_writes_the_queue(slow_rebuild, monkeypatch):
    sm = slow_rebuild
    slow_parse = sm._parse_article
    calls = {"n": 0}
    queued = threading.Event()

    def parse(text, filename, project):
        if threading.current_thread().name == REBUILD:
            calls["n"] += 1
            if calls["n"] == 2:
                queued.wait(5)   # сбой — только когда запись уже стоит в очереди
                raise RuntimeError("сбой пересборки посреди скана")
        return slow_parse(text, filename, project)

    monkeypatch.setattr(sm, "_parse_article", parse)
    t, box = _start(sm)
    sm.index_document("# После сбоя\n\n**Теги:** t\n\nzqaftercrash\n", "survivor.md", "testproj")
    assert "testproj/survivor.md" in sm._ix_pending, "запись не встала в очередь"
    queued.set()
    t.join(10)
    assert isinstance(box.get("error"), RuntimeError), box
    assert not sm._ix_rebuilding["v"], "флаг не снят после сбоя"
    assert "testproj/survivor.md" in _paths_with(sm, "zqaftercrash")


def test_edit_during_rebuild_leaves_one_copy_of_the_article(slow_rebuild):
    """Статья, которую скан пересборки пишет с диска, правится во время пересборки: в
    индексе ровно одна копия, с новым текстом. Запись очереди тем же writer'ом, что и
    скан, оставила бы две — Whoosh ищет прежнюю версию unique path только в закоммиченном
    (проба 25.09.2026, whoosh 2.7.4), и лишнюю не сняли бы ни следующие записи, ни reindex."""
    sm = slow_rebuild
    t, box = _start(sm)
    sm.index_document("# Extra 0\n\n**Теги:** t\n\nzqeditedduringrebuild\n", "extra0.md", "testproj")
    t.join(10)
    assert "error" not in box, box
    with sm.get_index().searcher() as s:
        copies = list(s.documents(path="testproj/extra0.md"))
    assert len(copies) == 1, copies
    assert "testproj/extra0.md" in _paths_with(sm, "zqeditedduringrebuild")


def test_write_outside_rebuild_is_indexed_at_once(knowledge_dir):
    """Позитивный контроль: вне пересборки запись идёт в индекс сразу, мимо очереди."""
    import memory_compiler.search as sm
    sm.get_index()
    sm.index_document("# Сразу\n\n**Теги:** t\n\nzqimmediate\n", "now.md", "testproj")
    assert "testproj/now.md" in _paths_with(sm, "zqimmediate")
    assert not sm._ix_pending


def test_next_write_picks_up_what_a_failed_tail_left(knowledge_dir):
    """Хвост, не записанный пересборкой, дописывает следующая запись вне пересборки."""
    import memory_compiler.search as sm
    sm.get_index()
    left = sm._parse_article("# Хвост\n\n**Теги:** t\n\nzqleftover\n", "left.md", "testproj")
    sm._ix_pending[left["path"]] = left
    sm.index_document("# Другая\n\n**Теги:** t\n\nтекст\n", "other.md", "testproj")
    assert "testproj/left.md" in _paths_with(sm, "zqleftover")
    assert not sm._ix_pending


def test_delete_drops_the_queued_write(knowledge_dir):
    """Удаление снимает запись статьи из очереди — иначе следующая запись её воскресит."""
    import memory_compiler.search as sm
    sm.get_index()
    gone = sm._parse_article("# Удалённая\n\n**Теги:** t\n\nzqdeleted\n", "gone.md", "testproj")
    sm._ix_pending[gone["path"]] = gone
    sm.delete_document("testproj/gone.md")
    sm.index_document("# Другая\n\n**Теги:** t\n\nтекст\n", "other.md", "testproj")
    assert "testproj/gone.md" not in _paths_with(sm, "zqdeleted")


def test_failed_batch_write_goes_back_to_the_queue(knowledge_dir, monkeypatch):
    """Сбой записи пачки не теряет статьи: пачка возвращается в очередь, исключение летит
    дальше, а следующая запись в индекс дописывает её вместе со своей статьёй."""
    import memory_compiler.search as sm
    ix = sm.get_index()
    real_writer = ix.writer

    def broken_writer(*a, **k):
        w = real_writer(*a, **k)

        def commit(*ca, **ck):
            raise RuntimeError("сбой записи в индекс")

        w.commit = commit
        return w

    monkeypatch.setattr(ix, "writer", broken_writer)
    with pytest.raises(RuntimeError):
        sm.index_document("# Упала\n\n**Теги:** t\n\nzqbatchfailed\n", "failed.md", "testproj")
    assert "testproj/failed.md" in sm._ix_pending
    monkeypatch.setattr(ix, "writer", real_writer)
    sm.index_document("# Другая\n\n**Теги:** t\n\nтекст\n", "other.md", "testproj")
    assert "testproj/failed.md" in _paths_with(sm, "zqbatchfailed")
    assert not sm._ix_pending


def test_project_delete_drops_only_its_queued_writes(knowledge_dir):
    """Удаление проекта снимает из очереди записи его статей — и только его: у проекта
    с тем же началом имени записи остаются."""
    import memory_compiler.search as sm
    sm.get_index()
    for text, name, proj in (("# Своя\n\n**Теги:** t\n\nтекст\n", "mine.md", "testproj"),
                             ("# Соседа\n\n**Теги:** t\n\nтекст\n", "near.md", "testproj2"),
                             ("# Общая\n\n**Теги:** t\n\nтекст\n", "keep.md", "general")):
        fields = sm._parse_article(text, name, proj)
        sm._ix_pending[fields["path"]] = fields
    sm.delete_project_documents("testproj")
    assert set(sm._ix_pending) == {"testproj2/near.md", "general/keep.md"}


def test_delete_during_rebuild_removes_the_article(slow_rebuild):
    """Удаление во время пересборки ждёт её и снимает статью, хотя скан её записал."""
    sm = slow_rebuild
    t, box = _start(sm)
    d = threading.Thread(target=sm.delete_document, args=("testproj/extra1.md",), daemon=True)
    d.start()
    t.join(10)
    d.join(10)
    assert "error" not in box, box
    assert not d.is_alive(), "удаление не завершилось"
    with sm.get_index().searcher() as s:
        assert not list(s.documents(path="testproj/extra1.md"))


def test_queued_article_that_lost_shared_tag_leaves_cross_project(slow_rebuild, knowledge_dir):
    """Статья, у которой в записи из очереди снят тег shared, уходит из кросс-проектных,
    хотя скан прочитал с диска версию с тегом."""
    sm = slow_rebuild
    (knowledge_dir / "testproj" / "unshared.md").write_text(
        "# Была общей\n\n**Теги:** shared\n\nтекст\n", encoding="utf-8")
    t, _box = _start(sm)
    sm.index_document("# Была общей\n\n**Теги:** t\n\nтекст\n", "unshared.md", "testproj")
    t.join(10)
    assert "testproj/unshared.md" not in sm._shared_paths
