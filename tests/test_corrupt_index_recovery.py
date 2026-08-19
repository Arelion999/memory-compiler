"""Битый Whoosh-индекс эквивалентен отсутствующему (инцидент 2026-08-19).

Обрезанный _MAIN_*.toc в knowledge/.whoosh_index ронял сервер в crash-loop:
whoosh exists_in на битом TOC не возвращает False, а кидает — TypeError
«ord() expected a character» (read_varint на пустом/мусорном TOC) или
UnpicklingError «pickle data was truncated» (обрезанный TOC). exists_in ловит
только EmptyIndexError; startup_prepare_index/get_index не ловили ничего,
lifespan падал, restart=always циклился.

Правильное поведение: залогировать, увести битый каталог в
.whoosh_index.corrupt-<дата> (post-mortem, НЕ удалять) и собрать индекс заново.
Отдельный случай — TOC цел, битые СЕГМЕНТЫ: открытие проходит, падает чтение
(struct.error из reader) — ловится на rebuild-попытке, карантин + чистый пересбор.
"""
import gc

import pytest

import memory_compiler.search as sm


def _drop_index_handles():
    """Имитация свежего процесса (сценарий инцидента: сервер только стартует).
    Сброс _ix оставляет mmap сегментов жить в циклах ссылок до сборщика — а под
    Windows незакрытый handle блокирует и порчу файла тестом, и rename каталога."""
    sm._ix = None  # conftest восстановит после теста
    gc.collect()


def _corrupt_toc(index_dir, mode: str):
    """Испортить TOC одним из трёх реальных способов порчи (обрыв синка/диска)."""
    toc = next(index_dir.glob("_MAIN_*.toc"))
    if mode == "truncated":
        with toc.open("r+b") as f:
            f.truncate(17)
    elif mode == "empty":
        with toc.open("r+b") as f:
            f.truncate(0)
    else:  # garbage
        toc.write_bytes(b"\xde\xad\xbe\xef" * 5)
    return toc


@pytest.mark.parametrize("mode", ["truncated", "empty", "garbage"])
def test_startup_prepare_index_rebuilds_on_corrupt_toc(knowledge_dir, mode):
    """Точка инцидента: exists_in на битом TOC кидал, старт сервера падал."""
    n_before = sm.rebuild_index()
    _drop_index_handles()
    _corrupt_toc(sm.INDEX_DIR, mode)

    count = sm.startup_prepare_index()  # раньше: TypeError/UnpicklingError

    assert count == n_before >= 1  # индекс пересобран из knowledge/


def test_get_index_rebuilds_on_corrupt_toc(knowledge_dir):
    """Вторая точка открытия (exists_in/open_dir): через get_index идёт
    rebuild_index, так что защита только в startup_prepare_index не спасла бы."""
    sm.rebuild_index()
    _drop_index_handles()
    _corrupt_toc(sm.INDEX_DIR, "empty")

    ix = sm.get_index()

    with ix.searcher() as s:
        assert s.doc_count() >= 1


def test_corrupt_index_quarantined_not_deleted(knowledge_dir):
    """Битый каталог уводится в .whoosh_index.corrupt-<дата> целиком — материал
    для post-mortem; на прежнем месте собирается валидный индекс."""
    sm.rebuild_index()
    _drop_index_handles()
    toc = _corrupt_toc(sm.INDEX_DIR, "truncated")
    corrupt_bytes = toc.read_bytes()

    sm.startup_prepare_index()

    quarantined = list(knowledge_dir.glob(".whoosh_index.corrupt-*"))
    assert len(quarantined) == 1
    assert (quarantined[0] / toc.name).read_bytes() == corrupt_bytes
    from whoosh import index as wi
    assert wi.exists_in(str(sm.INDEX_DIR))  # новый индекс на прежнем месте валиден


def test_startup_prepare_index_rebuilds_on_corrupt_segment(knowledge_dir):
    """TOC цел, битый сегмент: exists_in True, open_dir OK, падает ЧТЕНИЕ
    (struct.error «unpack requires a buffer of 4 bytes» из reader) — раньше
    пролетало сквозь rebuild_index в lifespan тем же crash-loop'ом."""
    sm.rebuild_index()
    _drop_index_handles()
    seg = next(sm.INDEX_DIR.glob("*.seg"))
    seg.write_bytes(b"\x00" * 8)

    count = sm.startup_prepare_index()

    assert count >= 1  # карантин + чистый пересбор, не исключение
