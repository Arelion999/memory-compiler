"""Тесты отбора кандидатов и порядка выдачи (v1.31.0).

Стерегут три изменения, принятые ЗАМЕРОМ на 140 реальных запросах аудит-лога
(scripts/diag_retrieval.py + scripts/eval_pipeline.py):
  * OrGroup вместо AndGroup — при AndGroup канал BM25 был пуст на 48.6% запросов;
  * скоуп проекта ВНУТРИ запроса — иначе топ-N набирается по всей базе и своих
    вытесняют чужие проекты;
  * детерминированный тай-брейк — иначе порядок равных скоров зависел от
    рандомизации хэшей строк и гулял между процессами.
Каждое можно откатить через env, и это тоже покрыто.
"""
import memory_compiler.search as _smod
from memory_compiler.search import whoosh_search, rebuild_index


def _no_semantic(monkeypatch):
    """Отключить семантический канал: тесты про BM25 не должны зависеть от модели."""
    monkeypatch.setattr(_smod, "_embeddings", {})


def test_multiword_partial_match_found_with_or_group(knowledge_dir, monkeypatch):
    """OrGroup: документ, покрывший ЧАСТЬ термов запроса, остаётся кандидатом.

    При AndGroup такой запрос обнулял канал BM25 целиком — именно это и происходило
    на 75% реальных запросов от шести слов."""
    _no_semantic(monkeypatch)
    (knowledge_dir / "testproj" / "nginx.md").write_text(
        "# Nginx настройка прокси\n\n**Теги:** nginx\n\nПроксирование запросов.\n",
        encoding="utf-8")
    rebuild_index()

    results = whoosh_search("nginx сертификат отсутствующеевкорпусеслово",
                            project="testproj", limit=10)
    assert any(r["file"] == "nginx.md" for r in results), \
        f"частичное совпадение потеряно при OrGroup: {results}"


def test_legacy_and_group_drops_partial_match(knowledge_dir, monkeypatch):
    """Откат SEARCH_QUERY_GROUP=and возвращает прежнее поведение: все термы обязательны.

    Тест фиксирует ЦЕНУ старого дефолта, а не желаемое поведение."""
    _no_semantic(monkeypatch)
    monkeypatch.setattr(_smod, "SEARCH_QUERY_GROUP", "and")
    (knowledge_dir / "testproj" / "nginx.md").write_text(
        "# Nginx настройка прокси\n\n**Теги:** nginx\n\nПроксирование запросов.\n",
        encoding="utf-8")
    rebuild_index()

    results = whoosh_search("nginx сертификат отсутствующеевкорпусеслово",
                            project="testproj", limit=10)
    assert results == [], f"AndGroup обязан требовать все термы, получено: {results}"


def test_scope_aware_pool_not_starved_by_other_projects(knowledge_dir, monkeypatch):
    """Скоуп внутри запроса: кандидаты набираются ИЗ проекта.

    Раньше оба канала брали топ-N по всей базе и лишь потом отбрасывали чужие проекты,
    поэтому статью своего проекта вытесняли более сильные совпадения соседних.
    Здесь 40 статей general забивают общий топ, а нужная лежит в testproj."""
    _no_semantic(monkeypatch)
    for i in range(40):
        (knowledge_dir / "general" / f"docker_{i:02d}.md").write_text(
            f"# Docker docker docker выпуск {i}\n\n**Теги:** docker\n\n"
            "docker docker docker docker docker\n", encoding="utf-8")
    (knowledge_dir / "testproj" / "target.md").write_text(
        "# Развёртывание сервиса\n\n**Теги:** deploy\n\n"
        "Упоминание docker одной строкой.\n", encoding="utf-8")
    rebuild_index()

    results = whoosh_search("docker", project="testproj", limit=10)
    assert any(r["file"] == "target.md" for r in results), \
        f"статью своего проекта вытеснили чужие: {results}"


def test_scope_filter_none_for_all_and_in_legacy_mode(knowledge_dir, monkeypatch):
    """_scope_filter не должен ничего фильтровать при project='all' и при откате."""
    assert _smod._scope_filter("all") is None
    assert _smod._scope_filter("testproj") is not None
    monkeypatch.setattr(_smod, "SEARCH_SCOPE_AWARE", False)
    assert _smod._scope_filter("testproj") is None, \
        "в legacy-режиме фильтр обязан отключаться — скоуп остаётся пост-фильтром"


def test_equal_scores_ordered_by_path_not_hash(knowledge_dir, monkeypatch):
    """Равные скоры разрешаются по пути, а не порядком обхода set().

    Документ, найденный ТОЛЬКО ключевым каналом, и документ, найденный ТОЛЬКО
    семантикой, оба стоят первыми в своих каналах — RRF даёт им идентичный скор.
    Раньше их порядок брался из хэша строки и менялся между процессами."""
    proj = knowledge_dir / "testproj"
    (proj / "aaa.md").write_text(
        "# Первая\n\n**Теги:** t\n\nсодержимое про ключодин\n", encoding="utf-8")
    (proj / "zzz.md").write_text(
        "# Вторая\n\n**Теги:** t\n\nсодержимое про ключдва\n", encoding="utf-8")
    rebuild_index()

    # Сценарий 1: ключевой канал даёт aaa.md, семантический — zzz.md.
    monkeypatch.setattr(_smod, "semantic_search",
                        lambda q, limit=10, keep=None: [("testproj/zzz.md", 0.9)])
    files = [r["file"] for r in whoosh_search("ключодин", project="testproj", limit=10)]
    assert files[:2] == ["aaa.md", "zzz.md"], \
        f"равные скоры упорядочены не по пути: {files}"

    # Сценарий 2 (зеркальный): каналы меняются файлами местами. Порядок обязан
    # остаться алфавитным — значит его определяет путь, а не то, какой канал нашёл.
    monkeypatch.setattr(_smod, "semantic_search",
                        lambda q, limit=10, keep=None: [("testproj/aaa.md", 0.9)])
    files = [r["file"] for r in whoosh_search("ключдва", project="testproj", limit=10)]
    assert files[:2] == ["aaa.md", "zzz.md"], \
        f"порядок зависит от канала, а не от пути: {files}"


def test_repeated_search_is_stable(knowledge_dir, monkeypatch):
    """Повторный одинаковый запрос отдаёт идентичный порядок (не только набор)."""
    _no_semantic(monkeypatch)
    for i in range(12):
        (knowledge_dir / "testproj" / f"a{i:02d}.md").write_text(
            f"# Статья {i}\n\n**Теги:** docker\n\nодинаковое тело про docker\n",
            encoding="utf-8")
    rebuild_index()
    first = [r["file"] for r in whoosh_search("docker", project="testproj", limit=10)]
    for _ in range(3):
        assert [r["file"] for r in
                whoosh_search("docker", project="testproj", limit=10)] == first


def test_load_shared_paths_fills_from_existing_index(knowledge_dir, monkeypatch):
    """load_shared_paths поднимает набор из готового индекса, без пересборки."""
    monkeypatch.setattr(_smod, "PROJECTS", ["testproj", "general"])
    (knowledge_dir / "general" / "notify.md").write_text(
        "# Общий канал\n\n**Теги:** shared, alerts\n\nКанал уведомлений.\n",
        encoding="utf-8")
    rebuild_index()
    assert "general/notify.md" in _smod._shared_paths

    # Имитируем рестарт процесса: индекс на диске есть, набор в памяти пуст.
    monkeypatch.setattr(_smod, "_shared_paths", set())
    n = _smod.load_shared_paths(_smod.get_index())
    assert n >= 1 and "general/notify.md" in _smod._shared_paths, \
        "набор кросс-проектных статей не восстановился из индекса"


def test_startup_prepare_index_restores_shared_paths(knowledge_dir, monkeypatch):
    """Быстрый путь старта (индекс есть, схема совпала) обязан наполнить _shared_paths.

    Регресс: раньше startup_prepare_index возвращал doc_count() и выходил, а набор
    заполнялся только внутри rebuild_index — после каждого рестарта контейнера
    статьи shared/global переставали быть кросс-проектными до явного reindex."""
    monkeypatch.setattr(_smod, "PROJECTS", ["testproj", "general"])
    (knowledge_dir / "general" / "notify.md").write_text(
        "# Общий канал\n\n**Теги:** global\n\nКанал уведомлений.\n", encoding="utf-8")
    rebuild_index()
    monkeypatch.setattr(_smod, "_shared_paths", set())

    _smod.startup_prepare_index()
    assert "general/notify.md" in _smod._shared_paths, \
        "после старта с готовым индексом кросс-проектные статьи не восстановлены"


def test_semantic_only_result_still_has_preview(knowledge_dir, monkeypatch):
    """Превью читается лениво — но у документа, попавшего в выдачу, оно обязано быть.

    Регресс-риск оптимизации: карточка результата, найденного только семантикой,
    больше не читает файл на этапе слияния (иначе это до SEARCH_POOL дисковых чтений
    на запрос). Дочитывание перенесено на финальный срез, и вот это и проверяется."""
    proj = knowledge_dir / "testproj"
    (proj / "only_sem.md").write_text(
        "# Статья без ключевых слов запроса\n\n**Теги:** t\n\n"
        "## Записи\n\n### 2026-01-01 10:00\nСодержательное тело статьи для превью.\n",
        encoding="utf-8")
    rebuild_index()
    monkeypatch.setattr(_smod, "semantic_search",
                        lambda q, limit=10, keep=None: [("testproj/only_sem.md", 0.9)])

    results = whoosh_search("совершенноотсутствующийтерм", project="testproj", limit=10)
    assert results, "документ, найденный только семантикой, потерялся"
    assert results[0]["preview"], f"превью не дочитано: {results[0]}"
    for key in ("_preview_of", "_raw", "_path", "bm25"):
        assert key not in results[0], f"служебное поле {key} утекло в выдачу"


def test_pool_never_below_legacy_width(knowledge_dir, monkeypatch):
    """Пул не сужается относительно исторического limit*2 при маленьком SEARCH_POOL.

    Ширина считается как max(SEARCH_POOL, limit*2): вызывающий с большим limit
    (api.py просит 15) не должен получить МЕНЬШЕ кандидатов, чем до изменения."""
    _no_semantic(monkeypatch)
    monkeypatch.setattr(_smod, "SEARCH_POOL", 5)
    for i in range(30):
        (knowledge_dir / "testproj" / f"d{i:02d}.md").write_text(
            f"# Статья {i}\n\n**Теги:** docker\n\nтело про docker\n", encoding="utf-8")
    rebuild_index()

    # limit=15 → пул = max(5, 30) = 30, поэтому выдача упирается в limit, а не в пул.
    results = whoosh_search("docker", project="testproj", limit=15)
    assert len(results) > 5, \
        f"пул сузился до SEARCH_POOL и обрезал выдачу: получено {len(results)}"


def test_rrf_channel_weights_default_neutral():
    """Дефолт — равные веса каналов: поведение прежнее, замер ничего не изменил."""
    assert _smod.RRF_WEIGHT_BM25 == 1.0
    assert _smod.RRF_WEIGHT_SEMANTIC == 1.0


def test_rrf_weight_zero_disables_channel(knowledge_dir, monkeypatch):
    """Вес 0 отключает канал: документ, найденный ТОЛЬКО им, выпадает из выдачи.

    Ручка нужна для дешёвого перезамера (замер 2026-07-19 значимого выигрыша не дал,
    но при большем наборе вопрос откроется снова)."""
    proj = knowledge_dir / "testproj"
    (proj / "kw.md").write_text(
        "# Ключевая\n\n**Теги:** t\n\nтело про уникальныйтерм\n", encoding="utf-8")
    (proj / "sem.md").write_text(
        "# Семантическая\n\n**Теги:** t\n\nсовсем другое\n", encoding="utf-8")
    rebuild_index()
    monkeypatch.setattr(_smod, "semantic_search",
                        lambda q, limit=10, keep=None: [("testproj/sem.md", 0.9)])

    both = {r["file"] for r in whoosh_search("уникальныйтерм", project="testproj", limit=10)}
    assert {"kw.md", "sem.md"} <= both, f"при равных весах должны быть оба: {both}"

    monkeypatch.setattr(_smod, "RRF_WEIGHT_SEMANTIC", 0.0)
    only_kw = {r["file"] for r in whoosh_search("уникальныйтерм", project="testproj", limit=10)}
    assert "sem.md" not in only_kw, \
        f"при нулевом весе семантики её единственный документ не должен выживать: {only_kw}"
