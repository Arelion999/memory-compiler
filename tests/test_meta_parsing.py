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
    """ГЛАВНЫЙ РЕГРЕСС: нормализация регистра записывала '**Теги:** ** ftp, mcp'."""
    from memory_compiler.handlers import lint
    f = _article(knowledge_dir, "case.md", "**Теги:** ftp, MCP")
    asyncio.run(lint(project="testproj", fix=True))
    line = _tags_line(f)
    assert line == "**Теги:** ftp, mcp", f"lint испортил строку тегов: {line!r}"
    # Проверять ЗНАЧЕНИЕ, а не подстроку: сама метка «**Теги:**» кончается на '**',
    # поэтому '** ftp' находится и в правильной строке.
    assert parse_meta_value(line) == "ftp, mcp"


def test_lint_fix_heals_already_corrupted_tags(knowledge_dir):
    """Уже испорченная строка после прохода lint становится чистой."""
    from memory_compiler.handlers import lint
    f = _article(knowledge_dir, "dirty.md", "**Теги:** ** ftp, MCP")
    asyncio.run(lint(project="testproj", fix=True))
    assert _tags_line(f) == "**Теги:** ftp, mcp"


def test_lint_fix_touches_only_header_tags_line(knowledge_dir):
    """text.replace шёл по ВСЕМУ документу и правил строки тегов внутри записей —
    у daily-агрегатов их десятки."""
    from memory_compiler.handlers import lint
    p = knowledge_dir / "testproj"
    p.mkdir(exist_ok=True)
    f = p / "agg.md"
    f.write_text(
        "# Агрегат\n\n**Дата:** 2026-01-01 10:00\n**Проект:** testproj\n"
        "**Теги:** ftp, MCP\n\n## Записи\n\n### 2026-01-01 10:00\n"
        "Первая запись.\n**Теги:** ftp, MCP\n\n### 2026-01-02 10:00\nВторая.\n",
        encoding="utf-8")
    asyncio.run(lint(project="testproj", fix=True))
    lines = [l for l in f.read_text(encoding="utf-8").splitlines()
             if l.startswith("**Теги:**")]
    assert lines[0] == "**Теги:** ftp, mcp"
    assert lines[1] == "**Теги:** ftp, MCP", "правка уехала в тело записи"


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
