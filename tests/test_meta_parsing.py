"""Разбор метаданных шапки и то, что ломалось из-за него.

Корень всех тестов ниже один: закрывающие звёздочки метки «**Теги:**» стоят ПОСЛЕ
двоеточия, поэтому split(':', 1)[1] отдаёт '** ftp, docker'. Пока значение только
читали — это был шум в выдаче. Как только lint с fix=True записал его обратно, в
файлы уехало '**Теги:** ** ftp, docker': 126 статей, 81 из них секреты, 15 с двойной
порчей. Секреты не самолечатся — merge_into_article, единственный код, вычищавший
'*', им отказывает по построению.

Существующие тесты этого не ловили: они проверяли, что теги нормализованы в нижний
регистр, но не проверяли, во что превратилась сама строка в файле.
"""
import asyncio

from memory_compiler.maintenance import heal_header_markup
from memory_compiler.storage import parse_meta_value, merge_into_article, regenerate_index


# ─── parse_meta_value (чистая функция) ───────────────────────────────────────

def test_parse_meta_value_strips_label_asterisks():
    """Обычная строка: закрывающие '**' метки не должны попасть в значение."""
    assert parse_meta_value("**Теги:** ftp, docker") == "ftp, docker"


def test_parse_meta_value_heals_single_and_double_corruption():
    """Порченые строки из прода читаются как чистые — и одинарные, и двойные."""
    assert parse_meta_value("**Теги:** ** ftp, docker") == "ftp, docker"
    assert parse_meta_value("**Теги:** ** ** ftp, docker") == "ftp, docker"


def test_parse_meta_value_keeps_time_colons():
    """Режем только по метке: в значении '**Дата:**' есть свои двоеточия."""
    assert parse_meta_value("**Дата:** 2026-04-16 15:39") == "2026-04-16 15:39"


# ─── lint: не портить и лечить ───────────────────────────────────────────────

def _article(kd, name, tags_line, body="Тело статьи про деплой."):
    p = kd / "testproj"
    p.mkdir(exist_ok=True)
    f = p / name
    f.write_text(
        f"# Статья {name}\n\n**Дата:** 2026-01-01 10:00\n**Проект:** testproj\n"
        f"{tags_line}\n\n## Записи\n\n### 2026-01-01 10:00\n{body}\n",
        encoding="utf-8")
    return f


def _tags_line(path):
    return next(l for l in path.read_text(encoding="utf-8").splitlines()
                if l.startswith("**Теги:**"))


def test_lint_fix_does_not_corrupt_tags(knowledge_dir):
    """ГЛАВНЫЙ РЕГРЕСС: нормализация регистра записывала '**Теги:** ** ftp, mcp'.

    С v1.54.2 регистр вообще не трогается (см. тест про коллизии ниже): здоровая
    строка обязана пережить fix без единого изменения, включая заглавные буквы.
    """
    from memory_compiler.handlers import lint
    f = _article(knowledge_dir, "case.md", "**Теги:** ftp, MCP")
    asyncio.run(lint(project="testproj", fix=True))
    line = _tags_line(f)
    assert line == "**Теги:** ftp, MCP", f"lint изменил здоровую строку тегов: {line!r}"
    # Проверять ЗНАЧЕНИЕ, а не подстроку: сама метка «**Теги:**» кончается на '**',
    # поэтому '** ftp' находится и в правильной строке.
    assert parse_meta_value(line) == "ftp, MCP"


def test_lint_fix_heals_already_corrupted_tags(knowledge_dir):
    """Уже испорченная строка после прохода lint становится чистой — но регистр
    сохраняется: чинится РАЗМЕТКА метки, а не написание тегов."""
    from memory_compiler.handlers import lint
    f = _article(knowledge_dir, "dirty.md", "**Теги:** ** ftp, MCP")
    asyncio.run(lint(project="testproj", fix=True))
    assert _tags_line(f) == "**Теги:** ftp, MCP"


def test_lint_fix_touches_only_header_tags_line(knowledge_dir):
    """text.replace шёл по ВСЕМУ документу и правил строки тегов внутри записей —
    у daily-агрегатов их десятки."""
    from memory_compiler.handlers import lint
    p = knowledge_dir / "testproj"
    p.mkdir(exist_ok=True)
    f = p / "agg.md"
    f.write_text(
        "# Агрегат\n\n**Дата:** 2026-01-01 10:00\n**Проект:** testproj\n"
        "**Теги:** ** ftp, MCP\n\n## Записи\n\n### 2026-01-01 10:00\n"
        "Первая запись.\n**Теги:** ** ftp, MCP\n\n### 2026-01-02 10:00\nВторая.\n",
        encoding="utf-8")
    asyncio.run(lint(project="testproj", fix=True))
    lines = [l for l in f.read_text(encoding="utf-8").splitlines()
             if l.startswith("**Теги:**")]
    assert lines[0] == "**Теги:** ftp, MCP"
    assert lines[1] == "**Теги:** ** ftp, MCP", "правка уехала в тело записи"


def test_lint_reports_case_collisions_not_every_capital_letter(knowledge_dir):
    """Check 4 сообщает о РЕАЛЬНЫХ коллизиях (один тег в разных написаниях), а не
    о каждой заглавной букве.

    До v1.54.2 проверка была `raw_tags != lower_tags`, то есть требовала от ВСЕХ
    тегов нижнего регистра и ругалась на MAX, ПУЭ, LG, QR-код — правильные имена и
    аббревиатуры. На живой базе это давало 32 ложных пункта из 41, а fix их
    «нормализовал», то есть портил. Коллизия же реальна: search_by_tag и чипы
    /api/tags разводят MAX и max по разным ведёркам.
    """
    from memory_compiler.handlers import lint
    _article(knowledge_dir, "coll_a.md", "**Теги:** MAX, api")
    _article(knowledge_dir, "coll_b.md", "**Теги:** max, docker")
    _article(knowledge_dir, "unique_caps.md", "**Теги:** ПУЭ, УЗО, ESPHome")

    out = asyncio.run(lint(project="testproj"))
    text = out[0].text

    assert "max" in text.lower() and "коллизи" in text.lower(), \
        f"реальная коллизия MAX/max не найдена в выдаче:\n{text}"
    # ПОЗИТИВНЫЙ КОНТРОЛЬ наоборот: уникальные заглавные не должны попадать в отчёт
    assert "ПУЭ" not in text and "ESPHome" not in text, \
        f"lint ругается на уникальные заглавные теги:\n{text}"


def test_lint_ignores_links_to_repository_docs(knowledge_dir):
    """Ссылка на файл РЕПОЗИТОРИЯ (README-пара, docs/*) — не потерянная связь между
    статьями: таких файлов в knowledge/ нет и быть не должно, а Check 9 ищет цель в
    каталоге проекта базы. До v1.54.4 они вечно висели как dead reference, причём
    fix вырезал бы из статьи само указание на файл.

    ПОЗИТИВНЫЙ КОНТРОЛЬ обязателен: настоящая битая ссылка на статью должна ловиться
    по-прежнему, иначе проверка просто ослепла.
    """
    from memory_compiler.handlers import lint
    p = knowledge_dir / "testproj"
    p.mkdir(exist_ok=True)
    (p / "doc_ref.md").write_text(
        "# Док\n\n**Дата:** 2026-01-01 10:00\n**Проект:** testproj\n\n## Записи\n\n"
        "### 2026-01-01 10:00\nПереключатель языка: [Русский](README.ru.md), "
        "политика — [security.md](security.md).\n", encoding="utf-8")
    (p / "broken_ref.md").write_text(
        "# Битая\n\n**Дата:** 2026-01-01 10:00\n**Проект:** testproj\n\n## Записи\n\n"
        "### 2026-01-01 10:00\nСм. [соседняя](не_существует_статья.md).\n",
        encoding="utf-8")

    text = asyncio.run(lint(project="testproj"))[0].text

    assert "README.ru.md" not in text and "security.md" not in text, \
        f"ссылки на файлы репозитория снова считаются битыми:\n{text}"
    assert "не_существует_статья.md" in text, \
        f"настоящая битая ссылка перестала ловиться — проверка ослепла:\n{text}"


def test_lint_does_not_pair_stubs_of_extracted_secrets(knowledge_dir, monkeypatch):
    """Заглушки от вынесенных секретов — не дубли друг друга.

    Когда значение уносят в зашифрованный секрет, на месте статьи остаётся шаблон
    «вынесено в секрет, смотреть read_article(...)». Тело почти целиком из этой
    обвязки, поэтому такие заглушки похожи ПО ПОСТРОЕНИЮ — тот же случай, что уже
    исключён для самих секретов и служебных файлов. На проде это дало sim=0.92 между
    «Ключ для WINDOWS» и «Строка подключения к GIT» — статьями о совершенно разном.

    ПОЗИТИВНЫЙ КОНТРОЛЬ обязателен: обычная похожая пара обязана ловиться, иначе
    Check 5 просто ослеп бы вместе с ложным срабатыванием.
    """
    import numpy as np
    from memory_compiler import handlers as h
    from memory_compiler.handlers import lint
    p = knowledge_dir / "stubproj"
    p.mkdir(exist_ok=True)
    head = "**Дата:** 2026-01-01 10:00\n**Проект:** stubproj\n**Теги:** test\n"

    def write(name, body):
        (p / name).write_text(
            f"# {name}\n\n{head}\n## Записи\n\n### 2026-01-01 10:00\n{body}\n",
            encoding="utf-8")

    write("stub_key.md", 'Значение вынесено в зашифрованный секрет 12.08.2026.\n'
                         'Смотреть: `read_article("stubproj", "secret_key.md")`.')
    write("stub_conn.md", 'Значение вынесено в зашифрованный секрет 12.08.2026.\n'
                          'Смотреть: `read_article("stubproj", "secret_conn.md")`.')
    write("plain_a.md", "Обычная статья про развёртывание на NAS.")
    write("plain_b.md", "Обычная статья про развёртывание на NAS, вторая.")

    # Пары внутри каждой двойки одинаково «похожи» — разводит их только фильтр.
    near = np.array([0.9987, 0.05], dtype=float)
    base = np.array([1.0, 0.0], dtype=float)
    monkeypatch.setattr(h._search, "snapshot_embeddings", lambda: {
        "stubproj/stub_key.md": base, "stubproj/stub_conn.md": near,
        "stubproj/plain_a.md": base, "stubproj/plain_b.md": near,
    })

    def dup_lines(txt):
        return [ln for ln in txt.splitlines() if "Возможный дубль" in ln]

    def stub_pairs(txt):
        return [ln for ln in dup_lines(txt)
                if "stub_key.md" in ln or "stub_conn.md" in ln]

    # Контроль на сам тест: с обезвреженной сигнатурой заглушки ОБЯЗАНЫ дать пару.
    # Без этого шага тест зеленел бы и от того, что векторы разошлись, а не от фильтра.
    import re as _re
    orig_re = h.SECRET_POINTER_RE
    monkeypatch.setattr(h, "SECRET_POINTER_RE", _re.compile(r"(?!x)x"))
    without_filter = asyncio.run(lint(project="stubproj"))[0].text
    assert stub_pairs(without_filter), \
        f"тест не воспроизводит исходную поломку — проверять нечего:\n{without_filter}"

    # Возвращаем ТОЛЬКО сигнатуру: monkeypatch.undo() снял бы и патч эмбеддингов,
    # и позитивный контроль ниже проверял бы пустой снимок вместо пары plain_*.
    monkeypatch.setattr(h, "SECRET_POINTER_RE", orig_re)
    text = asyncio.run(lint(project="stubproj"))[0].text

    assert not stub_pairs(text), \
        f"заглушки от вынесенных секретов снова считаются дублями:\n{text}"
    assert "plain_a.md ↔ plain_b.md" in text, \
        f"обычные дубли перестали ловиться — Check 5 ослеп:\n{text}"


def test_auto_lint_loop_is_report_only():
    """Фоновая задача не должна молча писать в базу: она правила 1800 статей раз в
    неделю и не оставляла следа в аудите (audit_log пишется только на MCP-пути)."""
    import pathlib
    import memory_compiler.api as api_mod
    src = pathlib.Path(api_mod.__file__).read_text(encoding="utf-8")
    body = src[src.index("async def auto_lint_loop"):]
    body = body[:body.index("@asynccontextmanager")]
    # Смотрим на ВЫЗОВ, а не на текст: в докстринге задачи слова 'fix=True' стоят
    # намеренно — там объяснено, почему так делать нельзя.
    calls = [l for l in body.splitlines() if "_lint(" in l and not l.strip().startswith("#")]
    assert calls, "вызов lint в фоновой задаче не найден — тест устарел"
    assert all("fix=False" in c for c in calls), f"авто-линт снова правит базу: {calls}"


# ─── merge_into_article: заголовок секции ────────────────────────────────────

def test_merge_builds_clean_section_heading(knowledge_dir):
    """'### ** 2026-04-16' в 44 статьях: заголовок строился из значения '**Дата:**'
    тем же split. Такой заголовок не распознаёт _is_log_heading (regex ждёт дату с
    начала строки) — статьи навсегда застревали в выдаче context_gaps, а
    is_duplicate_entry переставал ловить повторы."""
    p = knowledge_dir / "testproj"
    p.mkdir(exist_ok=True)
    f = p / "old_format.md"
    f.write_text(
        "# Старая статья\n\n**Дата:** 2026-01-01 10:00\n**Проект:** testproj\n"
        "**Теги:** test\n\nСтарое тело без секции Записи.\n", encoding="utf-8")
    merge_into_article(f, "новая запись", ["test"], "2026-01-02 11:00")
    text = f.read_text(encoding="utf-8")
    assert "### 2026-01-01 10:00" in text
    assert "### ** " not in text, "заголовок секции получил мусорные звёздочки"


def test_merge_does_not_duplicate_updated_line(knowledge_dir):
    """Ветка '**Дата:**' проверяла только СЛЕДУЮЩУЮ строку: если '**Обновлено:**'
    стояло в шапке ниже, вставлялось второе, а первое обновляла своя ветка."""
    p = knowledge_dir / "testproj"
    p.mkdir(exist_ok=True)
    f = p / "hdr.md"
    f.write_text(
        "# Статья\n\n**Дата:** 2026-01-01 10:00\n**Проект:** testproj\n"
        "**Обновлено:** 2026-01-01 10:00\n**Теги:** test\n\n"
        "## Записи\n\n### 2026-01-01 10:00\nТело.\n", encoding="utf-8")
    merge_into_article(f, "новая запись", ["test"], "2026-01-02 11:00")
    lines = f.read_text(encoding="utf-8").splitlines()
    header = lines[:lines.index("## Записи")]
    assert sum(1 for l in header if l.startswith("**Обновлено:**")) == 1


# ─── regenerate_index: заголовок и теги от тела ──────────────────────────────

def test_index_uses_real_title_for_frontmatter_articles(knowledge_dir):
    """В проде 127 записей index.md из 1726 выглядели как '- [---](…) —': заголовок
    брался как lines[0] по сырому файлу и упирался в открывающий '---' frontmatter."""
    p = knowledge_dir / "testproj"
    p.mkdir(exist_ok=True)
    (p / "fm.md").write_text(
        "---\ncontexts:\n  - heading: Раздел\n    context: \"Описание раздела.\"\n---\n"
        "# Настоящий заголовок статьи\n\n**Дата:** 2026-01-01 10:00\n"
        "**Проект:** testproj\n**Теги:** nginx, docker\n\n## Записи\n\n"
        "### 2026-01-01 10:00\nТело.\n", encoding="utf-8")
    regenerate_index()
    index = (knowledge_dir / "index.md").read_text(encoding="utf-8")
    assert "Настоящий заголовок статьи" in index
    assert "[---]" not in index, "заголовком статьи стал разделитель frontmatter"
    assert "nginx, docker" in index
    assert "** nginx" not in index


# ─── миграция: heal_header_markup ────────────────────────────────────────────

def test_migration_heals_and_spares_legitimate_bold(knowledge_dir, monkeypatch):
    """Чинит порчу, но НЕ трогает легитимный жирный заголовок.

    Разница ровно в пробеле: '### ** 2026-04-16' — след merge_into_article,
    '### **НИКС**' — обычный markdown. В базе таких 10 строк; широкий regex
    превратил бы их в '### НИКС**'."""
    import memory_compiler.maintenance as mnt
    monkeypatch.setattr(mnt, "git_commit", lambda *a, **k: None)
    p = knowledge_dir / "testproj"
    p.mkdir(exist_ok=True)
    f = p / "dirty.md"
    f.write_text(
        "# Статья\n\n**Дата:** 2026-01-01 10:00\n**Обновлено:** 2026-01-01 10:00\n"
        "**Проект:** testproj\n**Обновлено:** 2026-01-02 11:00\n"
        "**Теги:** ** ** ftp, mcp\n\n## Записи\n\n"
        "### ** 2026-01-01 10:00\nТело.\n\n### **НИКС**\nЛегитимный жирный заголовок.\n",
        encoding="utf-8")
    heal_header_markup(dry_run=False)
    text = f.read_text(encoding="utf-8")
    assert "**Теги:** ftp, mcp" in text
    assert "### 2026-01-01 10:00" in text and "### ** 2026" not in text
    assert "### **НИКС**" in text, "миграция испортила легитимный жирный заголовок"
    header = text.split("## Записи")[0]
    assert header.count("**Обновлено:**") == 1


def test_migration_dry_run_writes_nothing(knowledge_dir):
    f = _article(knowledge_dir, "d.md", "**Теги:** ** ftp")
    before = f.read_text(encoding="utf-8")
    heal_header_markup(dry_run=True)
    assert f.read_text(encoding="utf-8") == before


def test_migration_does_not_touch_encrypted_body(knowledge_dir, monkeypatch):
    """Секреты миграция ЧИНИТ (81 из 126 порченых — секреты), но строка ENC:
    ни под одно правило не подходит и обязана остаться байт-в-байт."""
    import memory_compiler.maintenance as mnt
    monkeypatch.setattr(mnt, "git_commit", lambda *a, **k: None)
    p = knowledge_dir / "testproj"
    p.mkdir(exist_ok=True)
    f = p / "secret_x.md"
    enc = "ENC:gAAAAABqO9p3zfJJqaK0jzjkHpLSLN_t1A861uWp2sCGhrE"
    f.write_text(
        f"# Секрет\n\n**Дата:** 2026-01-01 10:00\n**Проект:** testproj\n"
        f"**Теги:** ** ssh, sftp\n**Секрет:** да\n\n{enc}\n", encoding="utf-8")
    heal_header_markup(dry_run=False)
    text = f.read_text(encoding="utf-8")
    assert enc in text, "шифртекст пострадал"
    assert "**Теги:** ssh, sftp" in text


# ─── get_runbook: обход пути и гейт секретов ─────────────────────────────────

def test_get_runbook_rejects_path_traversal(knowledge_dir):
    """Путь собирался конкатенацией без safe_article_path — '../../' читал файл вне
    базы, тогда как read_article тот же путь отвергает."""
    from memory_compiler.handlers import get_runbook
    outside = knowledge_dir.parent / "outside_secret.txt"
    outside.write_text("ZZOUTSIDEZZ", encoding="utf-8")
    out = asyncio.run(get_runbook("testproj", "../../outside_secret.txt"))[0].text
    assert "ZZOUTSIDEZZ" not in out


def test_get_runbook_does_not_dump_secret(knowledge_dir):
    """Хендлер отдавал сырой файл целиком без единой проверки секретности."""
    from memory_compiler.handlers import get_runbook
    p = knowledge_dir / "testproj"
    p.mkdir(exist_ok=True)
    (p / "secret_rb.md").write_text(
        "# Ранбук с доступами\n\n**Проект:** testproj\n**Теги:** test\n"
        "**Секрет:** да\n\n- [x] шаг\nПароль ZZTOPSECRETZZ\n", encoding="utf-8")
    out = asyncio.run(get_runbook("testproj", "secret_rb.md"))[0].text
    assert "ZZTOPSECRETZZ" not in out
    assert "зашифровано" in out
