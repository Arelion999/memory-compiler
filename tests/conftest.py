"""Shared test fixtures."""
import os

# Тесты гоняются офлайн: модели эмбеддингов/reranker берутся из локального кэша HF,
# без сетевых обращений к huggingface.co. Иначе при недоступности HF Hub (504/timeout)
# падал test_embed_during_rebuild_survives_swap и др. — сеть не должна влиять на прогон.
# Контейнер уже работает с этими флагами (docker-compose.yml). setdefault — чтобы явный
# внешний HF_HUB_OFFLINE=0 (если кто-то намеренно тестирует онлайн) не перетирался.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

# Эмбеддинги в тестах считаются СИНХРОННО. В проде вектор уезжает в фоновую очередь
# (v1.59.0: запись 7 с → 0.3 с), но тест, сохраняющий статью и тут же ищущий её
# семантикой, на асинхронном режиме стал бы гонкой — зелёной или красной в
# зависимости от того, успел ли воркер. Тесты самой очереди включают режим сами.
os.environ.setdefault("MC_EMBED_ASYNC", "0")

import pytest
from pathlib import Path


@pytest.fixture
def knowledge_dir(tmp_path):
    """Create a temporary knowledge directory with test data."""
    kd = tmp_path / "knowledge"
    kd.mkdir()
    proj = kd / "testproj"
    proj.mkdir()
    article = proj / "test_article.md"
    article.write_text(
        "# Test Article\n\n"
        "**Дата:** 2026-01-01 10:00\n"
        "**Проект:** testproj\n"
        "**Теги:** docker, test\n\n"
        "## Записи\n\n"
        "### 2026-01-01 10:00\n"
        "Test content about docker deployment on NAS.\n",
        encoding="utf-8",
    )
    daily = kd / "daily"
    daily.mkdir()
    general = kd / "general"
    general.mkdir()
    return kd


@pytest.fixture(autouse=True)
def patch_knowledge_dir(knowledge_dir, monkeypatch):
    """Patch KNOWLEDGE_DIR for all tests across all modules that import it."""
    import memory_compiler.config as cfg
    import memory_compiler.storage as storage_mod
    import memory_compiler.search as search_mod
    import memory_compiler.handlers as handlers_mod
    import memory_compiler.api as api_mod
    import memory_compiler.maintenance as maintenance_mod
    import memory_compiler.handlers_reports as reports_mod
    import memory_compiler.handlers_search as search_handlers_mod
    import memory_compiler.handlers_articles as articles_mod
    import memory_compiler.handlers_sessions as sessions_mod

    # Patch config module (canonical source)
    monkeypatch.setattr(cfg, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(cfg, "INDEX_DIR", knowledge_dir / ".whoosh_index")
    monkeypatch.setattr(cfg, "ARTICLE_META_PATH", knowledge_dir / ".article_meta.json")
    monkeypatch.setattr(cfg, "article_meta", {})
    monkeypatch.setattr(cfg, "PROJECTS", ["testproj", "general"])

    # Patch local bindings in modules that use `from config import KNOWLEDGE_DIR`
    monkeypatch.setattr(storage_mod, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(search_mod, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(search_mod, "INDEX_DIR", knowledge_dir / ".whoosh_index")
    monkeypatch.setattr(search_mod, "EMBEDDINGS_PATH", knowledge_dir / ".embeddings.pkl")
    monkeypatch.setattr(handlers_mod, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(api_mod, "KNOWLEDGE_DIR", knowledge_dir)
    # ⚠️ maintenance тоже держит СВОЙ модульный KNOWLEDGE_DIR (from config import ...).
    # Без этой строки тест maintenance уходит работать по БОЕВОЙ базе — а функции там
    # пишущие, разовые проходы по всем статьям. Поймано ровно так: тест сообщил
    # «статей 0», потому что проекта testproj в проде нет. Повезло.
    monkeypatch.setattr(maintenance_mod, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(maintenance_mod, "PROJECTS", ["testproj", "general"])
    # ⚠️ handlers_reports (v1.64.0) — тот же класс: отчёты уехали из handlers в
    # свой модуль и держат СВОИ KNOWLEDGE_DIR/PROJECTS. Без этих строк линт
    # молча сканирует боевую базу и ничего не находит в tmp — так и упали
    # одиннадцать тестов сразу после разреза.
    monkeypatch.setattr(reports_mod, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(reports_mod, "PROJECTS", ["testproj", "general"])
    # ⚠️ handlers_search (v1.83.0) — тот же класс третий раз: поиск и ask уехали из
    # handlers и держат СВОИ KNOWLEDGE_DIR/PROJECTS. Без этих строк ask_sources и
    # attach_corrections читают БОЕВУЮ базу вместо tmp — молча, потому что файла
    # просто не окажется и фрагмент выйдет пустым.
    monkeypatch.setattr(search_handlers_mod, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(search_handlers_mod, "PROJECTS", ["testproj", "general"])
    # ⚠️ handlers_articles (v1.84.0) — тот же класс четвёртый раз: запись статей уехала
    # из handlers и держит СВОИ KNOWLEDGE_DIR/PROJECTS. Без этих строк save_lesson,
    # edit_article, backlinks и context_gaps пишут/читают БОЕВУЮ базу вместо tmp.
    monkeypatch.setattr(articles_mod, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(articles_mod, "PROJECTS", ["testproj", "general"])
    # ⚠️ handlers_sessions (v1.86.0) — тот же класс пятый раз: журнал, старт/финиш
    # задачи и стартовый контекст уехали из handlers и держат СВОЙ KNOWLEDGE_DIR.
    # Без этой строки start_task/finish_task/load_session пишут и читают БОЕВУЮ базу
    # вместо tmp — молча. PROJECTS модулю не нужен: open_questions читает его как
    # memory_compiler.config.PROJECTS живьём (setattr на несуществующий атрибут упал бы).
    monkeypatch.setattr(sessions_mod, "KNOWLEDGE_DIR", knowledge_dir)

    # Reset whoosh index so it gets recreated in tmp dir
    monkeypatch.setattr(search_mod, "_ix", None)
    # Очередь записей в индекс — модульное состояние: флаг, оставшийся от упавшего теста,
    # отправлял бы в очередь все записи следующих.
    monkeypatch.setattr(search_mod, "_ix_pending", {})
    monkeypatch.setattr(search_mod, "_ix_rebuilding", {"v": False})
    # Изоляция эмбеддингов между тестами: часть тестов присваивает _embeddings напрямую
    # (2-мерные векторы), утечка ломала последующие тесты при прогоне подмножества.
    # monkeypatch авто-восстанавливает после каждого теста.
    monkeypatch.setattr(search_mod, "_embeddings", {})
    monkeypatch.setattr(search_mod, "_embed_texts", {})
    monkeypatch.setattr(search_mod, "_chunk_hashes", {})
    # Снимки свежести (v1.88.1) пишутся в файл, только когда путь задан, а задаёт его
    # lifespan сервера. Тест, поднявший lifespan, иначе оставил бы путь выставленным, и
    # следующие тесты писали бы во временный каталог машины и читали оттуда чужие снимки.
    import memory_compiler.freshness as freshness_mod
    monkeypatch.setattr(freshness_mod, "STATE_PATH", None)
    monkeypatch.setattr(freshness_mod, "_loaded", [False])
    monkeypatch.setattr(freshness_mod, "_last_save", [0.0])
