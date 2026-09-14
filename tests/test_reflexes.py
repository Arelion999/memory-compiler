"""Рефлексы памяти (v1.78.0): нормализация, триггеры, индекс, памятки."""
import time

import pytest

from memory_compiler import reflexes as rx


# ─── нормализация и ключевые строки ошибки ─────────────────────────────────
def test_error_normalization_ignores_quotes_paths_numbers():
    live = "/usr/bin/bash: -c: line 101: unexpected EOF while looking for matching `''"
    assert rx.normalize_error("unexpected EOF while looking for matching") in rx.normalize_error(live)
    a = rx.normalize_error(r"FileNotFoundError: [Errno 2] No such file: 'C:\Users\x\a.txt'")
    b = rx.normalize_error("FileNotFoundError: [Errno 13] No such file: '/tmp/y/b.txt'")
    assert a == b


def test_error_key_lines_drop_exit_code_frames_and_carets():
    err = ("Exit code 1\nTraceback (most recent call last):\n"
           '  File "C:\\x.py", line 18, in <module>\n'
           "    print(s)\n    ~~~~~^^^\n"
           "UnicodeEncodeError: 'charmap' codec can't encode character '\\u2264'")
    lines = rx.error_key_lines(err)
    assert lines[-1].startswith("UnicodeEncodeError")
    assert not any(l.startswith(("Exit code", "File ")) for l in lines)
    assert "~~~~~^^^" not in lines


# ─── разбор и запись раздела «## Рефлексы» ─────────────────────────────────
ARTICLE = """# Грабли SFTP на NAS

**Дата:** 2026-04-12 10:00
**Проект:** infra
**Теги:** nas

## Записи

### 2026-04-12 10:00
SFTP на NAS выключен, файлы гнать через tar и http.server.

```
## Рефлексы
- ошибка: это пример в блоке кода, а не триггер
```

## Рефлексы
- ошибка: Unable to start subsystem: sftp
- цель: 192.0.2.10
- файл: memory_compiler/ui.py
- Ошибка: unable to start SUBSYSTEM: sftp
- ошибка: timeout
- заметка: не триггер
просто строка

### 2026-04-13 09:00
- ошибка: запись после раздела, а не триггер
"""


def test_parse_triggers_reads_section_only():
    assert rx.parse_triggers(ARTICLE) == [
        ("error", "Unable to start subsystem: sftp"),
        ("target", "192.0.2.10"),
        ("file", "memory_compiler/ui.py"),
    ]


def test_add_triggers_creates_section_before_git_refs_and_dedupes():
    text = ("# T\n\n## Записи\n\n### 2026-01-01 10:00\nтело\n\n"
            "## Git-ссылки\n**Теги:** v1.0.0\n")
    new, added, rejected = rx.add_triggers(text, [
        "ошибка: Unable to start subsystem: sftp",
        "цель: 192.0.2.10",
        "ерунда без вида",
        "ошибка: timeout",
    ])
    assert added == [("error", "Unable to start subsystem: sftp"), ("target", "192.0.2.10")]
    assert [r[0] for r in rejected] == ["ерунда без вида", "ошибка: timeout"]
    assert new.index("## Рефлексы") < new.index("## Git-ссылки")
    assert rx.parse_triggers(new) == added
    again, added_again, _ = rx.add_triggers(new, ["ошибка: unable to start subsystem: SFTP"])
    assert again == new and added_again == []


def test_add_triggers_appends_into_existing_section():
    new, added, _ = rx.add_triggers(ARTICLE, ["файл: deploy/Dockerfile"])
    assert added == [("file", "deploy/Dockerfile")]
    assert [k for k, _ in rx.parse_triggers(new)] == ["error", "target", "file", "file"]
    assert "### 2026-04-13 09:00" in new


def test_add_triggers_accepts_single_string():
    """Строка вместо списка — не повод отвергать её посимвольно."""
    _new, added, rejected = rx.add_triggers("# T\n", "ошибка: Unable to start subsystem: sftp")
    assert added == [("error", "Unable to start subsystem: sftp")] and rejected == []


def test_trigger_value_cannot_inject_markup():
    """Значение — одна строка без управляющих символов: иначе оно рвёт разметку статьи."""
    for bad in ("ошибка: строка\n## Записи",
                "ошибка: Some long error text here" + chr(13) + "## Записи",
                "ошибка: Some long error text here" + chr(0x2028) + "## Записи",
                "ошибка: Some long error" + chr(7) + " text here"):
        new, added, rejected = rx.add_triggers("# T\n", [bad])
        assert added == [] and rejected, repr(bad)
        assert new == "# T\n"


def test_file_trigger_needs_directory():
    """Голое имя (README.md) сработало бы на одноимённый файл любого репозитория."""
    _new, added, rejected = rx.add_triggers("# T\n", ["файл: README.md", "файл: docs/README.md"])
    assert added == [("file", "docs/README.md")]
    assert [r[0] for r in rejected] == ["файл: README.md"]


def test_git_refs_heading_inside_code_block_is_ignored():
    text = "# T\n\n## Записи\n\n### 2026-01-01 10:00\nпример:\n```\n## Git-ссылки\n```\nтело\n"
    new, added, _ = rx.add_triggers(text, ["цель: nas-main"])
    assert added == [("target", "nas-main")]
    assert rx.parse_triggers(new) == [("target", "nas-main")]
    assert new.rstrip().endswith("- цель: nas-main")


def test_describe_added_lists_kinds_and_rejections():
    note = rx.describe_added([("error", "x" * 20), ("target", "nas-main"), ("error", "y" * 20)],
                             [("цель: x", "длина")])
    assert note.splitlines()[0] == "🧷 Рефлексы: +3 (ошибка, цель)"
    assert "«цель: x» не принят: длина" in note


# ─── индекс и памятки ───────────────────────────────────────────────────────
def _art(kd, project, name, title, body, date="2026-01-01 10:00", extra_head=""):
    pdir = kd / project
    pdir.mkdir(exist_ok=True)
    path = pdir / name
    path.write_text(
        f"# {title}\n\n**Дата:** {date}\n**Проект:** {project}\n**Теги:** t\n{extra_head}\n"
        f"## Записи\n\n### {date}\n{body}\n", encoding="utf-8")
    return path


@pytest.fixture
def fresh(monkeypatch):
    monkeypatch.setattr(rx, "REFLEX_RESCAN_SEC", 0)
    rx.invalidate()


def test_error_trigger_finds_article(knowledge_dir, fresh):
    _art(knowledge_dir, "testproj", "sftp.md", "SFTP на NAS выключен",
         "Файлы переносить через tar и http.server.\n\n## Рефлексы\n"
         "- ошибка: Unable to start subsystem: sftp")
    memos = rx.find_memos("error", 'Exit code 1\n{"message": "SFTP connection failed: '
                                   'Unable to start subsystem: sftp"}')
    assert [(m.file, m.via) for m in memos] == [("sftp.md", "trigger")]
    assert memos[0].snippet.startswith("Файлы переносить")


def test_error_needs_explicit_trigger_quoting_is_not_enough(knowledge_dir, fresh):
    """Дословный канал отвергнут замером 13.09.2026: из 18 срабатываний по базе ~12 — шум
    (общие сообщения, процитированные в логах статей). Для ошибки нужен явный триггер."""
    err = "Exit code 2\n/usr/bin/bash: -c: line 101: unexpected EOF while looking for matching `''"
    path = _art(knowledge_dir, "testproj", "quoted.md", "Про bash -c",
                "Падает так: /usr/bin/bash: -c: line 7: unexpected EOF while looking for matching `''")
    assert rx.find_memos("error", err) == []
    # позитивный контроль: та же статья с явным триггером находится
    path.write_text(path.read_text(encoding="utf-8")
                    + "\n## Рефлексы\n- ошибка: unexpected EOF while looking for matching\n",
                    encoding="utf-8")
    assert [(m.file, m.via) for m in rx.find_memos("error", err)] == [("quoted.md", "trigger")]


def test_superseded_service_and_daily_are_skipped(knowledge_dir, fresh):
    trig = "\n\n## Рефлексы\n- цель: 10.20.30.40"
    _art(knowledge_dir, "testproj", "old.md", "Старое", "было" + trig,
         extra_head="**Отменена:** new.md — Новое (2026-02-01)\n")
    _art(knowledge_dir, "testproj", "_session.md", "Журнал", "служебное" + trig)
    _art(knowledge_dir, "daily", "2026-01-01.md", "Лог", "лог" + trig)
    _art(knowledge_dir, "testproj", "new.md", "Новое", "стало" + trig)
    assert [m.file for m in rx.find_memos("target", "10.20.30.40")] == ["new.md"]


@pytest.mark.parametrize("where, superseded", [
    ("body_head", True), ("in_frontmatter", False), ("mentioned_in_entries", False)])
def test_superseded_skip_agrees_with_storage(knowledge_dir, fresh, where, superseded):
    """«Статья отменена» рефлексы решают той же функцией, что storage.superseded_by. В
    v1.78.0 они смотрели ещё и начало СЫРОГО файла — обход бага mark_superseded, который
    ставил метку внутрь длинного frontmatter, — и расходились с superseded_by. Писатель
    починен, скан боевой базы 13.09.2026: четыре метки, все в шапке тела. Строка внутри
    YAML — порча файла, а не метка, и отменённой такую статью не считает никто."""
    from memory_compiler import storage
    mark = "**Отменена:** new.md — Новое (2026-02-01)\n"
    front = "---\ncontexts:\n" + "".join(
        f"  - heading: h{i}\n    context: c{i}\n" for i in range(8)) + "---\n"
    head = "# Старое\n\n**Дата:** 2026-01-01 10:00\n**Теги:** t\n"
    tail = "\n## Записи\n\n### 2026-01-01 10:00\nбыло\n\n## Рефлексы\n- цель: nas-old\n"
    text = {"body_head": front + head + mark + tail,
            "in_frontmatter": front.replace("---\n", "---\n" + mark, 1) + head + tail,
            "mentioned_in_entries": front + head + tail.replace("было\n", "было\n" + "\n" * 14 + mark)}
    (knowledge_dir / "testproj" / "old.md").write_text(text[where], encoding="utf-8")
    assert (storage.superseded_by("testproj", "old.md") is not None) == superseded
    # позитивный контроль в самих вариантах: без метки в шапке памятка находится
    assert (rx.find_memos("target", "nas-old") == []) == superseded


def test_target_title_channel_ranks_home_project_then_secret(knowledge_dir, fresh):
    _art(knowledge_dir, "general", "note.md", "Заметка про 192.0.2.55", "текст", date="2026-03-01 10:00")
    _art(knowledge_dir, "testproj", "plain.md", "Сервер 192.0.2.55 после переезда", "текст",
         date="2026-02-01 10:00")
    _art(knowledge_dir, "testproj", "secret_dev.md", "Доступы dev 192.0.2.55", "ENC:abc")
    _art(knowledge_dir, "testproj", "other.md", "Сервер 192.0.2.550", "не тот адрес")
    memos = rx.find_memos("target", ["192.0.2.55"], cwd=r"C:\Users\x\DEV\TestProj")
    # свой проект выше чужого; внутри — указатель на доступы выше более свежей статьи
    assert [m.file for m in memos] == ["secret_dev.md", "plain.md", "note.md"]
    assert memos[0].secret and memos[0].snippet == ""


def test_title_channel_ignores_plain_words_and_public_resolvers(knowledge_dir, fresh):
    """Замер ревью: «admin» давал 10 чужих секретов, «bridge» — 4 статьи не про цель."""
    _art(knowledge_dir, "testproj", "secret_a.md", "Доступы admin к серверу", "ENC:abc")
    _art(knowledge_dir, "testproj", "dns.md", "Пинг 8.8.8.8 не проходит — норма схемы", "текст")
    assert rx.find_memos("target", ["admin"]) == []
    assert rx.find_memos("target", ["8.8.8.8"]) == []
    # позитивный контроль: явный триггер и на простое слово работает
    _art(knowledge_dir, "testproj", "bridge.md", "Мост", "текст\n\n## Рефлексы\n- цель: bridge")
    assert [m.file for m in rx.find_memos("target", ["bridge"])] == ["bridge.md"]


# ─── короткие ЯВНЫЕ цели: два разных порога ──────────────────────────────────
def test_three_char_explicit_target_is_accepted():
    """Живой случай 14.09.2026: база 1С в реестре MCP зовётся «mot» — три символа.

    Цель выбрасывалась до поиска, карточка по ней не приходила никогда, а триггер
    «цель: mot» отвергался как «слишком общая цель». Явный триггер сравнивается ТОЧНО
    (v in wanted), поэтому мусора от короткого имени он не даёт."""
    assert rx.trigger_problem("target", "mot") is None
    new, added, rejected = rx.add_triggers("# T\n", ["цель: mot"])
    assert added == [("target", "mot")] and rejected == []
    assert rx.parse_triggers(new) == [("target", "mot")]


@pytest.mark.parametrize("value", ["mo", "mo.", "::1"])
def test_shorter_targets_and_stop_list_are_still_rejected(value):
    """Порог опущен до трёх символов, а не снят: «mo» — две буквы, «mo.» становится ими
    после нормализации (точка снимается), «::1» проходит по длине и держится стоп-списком.
    Позитивный контроль к соседнему тесту: без него «принимаем всё» тоже зеленело бы."""
    assert rx.trigger_problem("target", value) is not None


def test_short_target_is_found_by_its_own_trigger_only(knowledge_dir, fresh):
    """Трёхсимвольная цель находит статью по явному триггеру — и только свою.

    Точность: «mot» и «mot_prod» — разные узлы, сравнение точное, а не по подстроке."""
    _art(knowledge_dir, "testproj", "mot.md", "База MOT",
         "текст\n\n## Рефлексы\n- цель: mot")
    _art(knowledge_dir, "testproj", "motprod.md", "База MOT PROD",
         "текст\n\n## Рефлексы\n- цель: mot_prod")
    assert [m.file for m in rx.find_memos("target", ["mot"])] == ["mot.md"]
    assert [m.file for m in rx.find_memos("target", ["mot_prod"])] == ["motprod.md"]


def test_short_target_does_not_reach_the_title_channel(knowledge_dir, fresh):
    """Канал заголовков держит ПРЕЖНИЙ порог (MIN_TARGET): там короткое имя даёт мусор —
    замер 13.09.2026, «admin» приводил 10 чужих секретов. Опущен только порог явного
    триггера, и наивная правка «MIN_TARGET = 3» на всё сразу обязана уронить этот тест."""
    _art(knowledge_dir, "testproj", "db1.md", "Бэкап db1 упал", "текст")
    assert rx.find_memos("target", ["db1"]) == []
    # позитивный контроль: адресоподобная цель от MIN_TARGET символов по заголовку находится
    _art(knowledge_dir, "testproj", "db12.md", "Бэкап db12 упал", "текст")
    assert [(m.file, m.via) for m in rx.find_memos("target", ["db12"])] == [("db12.md", "title")]


def test_snippet_skips_meta_labels(knowledge_dir, fresh):
    _art(knowledge_dir, "testproj", "dec.md", "Решение про NAS",
         "**Тип:** decision\n## Решение\nДеплой только через ватчер.\n\n## Рефлексы\n- цель: nas-dec")
    assert rx.find_memos("target", "nas-dec")[0].snippet == "Деплой только через ватчер."


def test_file_trigger_matches_path_suffix_on_boundary(knowledge_dir, fresh):
    _art(knowledge_dir, "testproj", "ui.md", "Плейсхолдер в ui.py",
         "не упоминать имя плейсхолдера\n\n## Рефлексы\n- файл: memory_compiler/ui.py")
    hit = rx.find_memos("file", r"C:\Users\x\DEV\memory-compiler\memory_compiler\ui.py")
    assert [m.file for m in hit] == ["ui.md"]
    assert rx.find_memos("file", r"C:\x\memory_compiler\ui_app.py") == []
    assert rx.find_memos("file", r"C:\x\other_memory_compiler\ui.py") == []


def test_exclude_limit_and_render_budget(knowledge_dir, fresh):
    for i in range(5):
        _art(knowledge_dir, "testproj", f"a{i}.md", f"Статья {i} " + "очень длинный заголовок " * 25,
             "x" * 500 + "\n\n## Рефлексы\n- цель: nas-main", date=f"2026-01-0{i + 1} 10:00")
    memos = rx.find_memos("target", "nas-main", exclude=["testproj/a4.md"])
    assert [m.file for m in memos] == ["a3.md", "a2.md", "a1.md"]
    text = rx.render("target", memos, rx.describe("target", "nas-main"))
    assert text.startswith("Память (рефлекс по цели nas-main)")
    assert len(text) <= rx.RENDER_BUDGET
    shown = rx.shown_in(memos, text)
    assert 0 < len(shown) < len(memos), "бюджет не сработал — тест потерял смысл"
    assert [m.file for m in shown] == ["a3.md", "a2.md"][:len(shown)]
    assert rx.render("target", [], "nas-main") == ""


# ─── memo несёт цель-источник (v1.79.0, N1) ──────────────────────────────────
def test_target_memo_carries_only_its_own_source_target(knowledge_dir, fresh):
    """Корень критической находки: memo обязано знать, ПО КАКОЙ цели статья найдена.

    Хук шлёт узел списком (имя ssh-соединения + адрес + посторонние адреса из тела
    команды). Без поля targets он раскладывал цитату статьи про узел A под все цели и
    ложно метил stale статью про узел B. Каждая статья несёт ТОЛЬКО свои цели-источники.
    Позитивный контроль: без правки поле пустое/одинаковое — ассерты это ловят."""
    _art(knowledge_dir, "testproj", "a.md", "Узел A",
         "текст\n\n## Рефлексы\n- цель: node-demo")
    _art(knowledge_dir, "testproj", "b.md", "Узел B",
         "текст\n\n## Рефлексы\n- цель: 192.0.2.50")
    memos = {m.file: m for m in
             rx.find_memos("target", ["node-demo", "192.0.2.10", "192.0.2.50"])}
    assert set(memos) == {"a.md", "b.md"}, memos
    assert memos["a.md"].targets == ["node-demo"]
    assert "192.0.2.50" not in memos["a.md"].targets
    assert memos["b.md"].targets == ["192.0.2.50"]
    assert "node-demo" not in memos["b.md"].targets


def test_target_memo_via_title_carries_matched_address(knowledge_dir, fresh):
    """Найдено по заголовку (адрес в title, без триггера) — в targets именно та цель,
    по которой совпал заголовок, а не все запрошенные."""
    _art(knowledge_dir, "testproj", "c.md", "Сервер 192.0.2.20 после переезда", "текст")
    memos = rx.find_memos("target", ["node-demo", "192.0.2.20"])
    assert [m.file for m in memos] == ["c.md"]
    assert memos[0].via == "title"
    assert memos[0].targets == ["192.0.2.20"]


def test_error_and_file_memos_have_empty_targets(knowledge_dir, fresh):
    """Каналы ошибки и файла к цели не относятся — targets пустой список."""
    _art(knowledge_dir, "testproj", "e.md", "Ошибка SFTP",
         "текст\n\n## Рефлексы\n- ошибка: Unable to start subsystem: sftp")
    _art(knowledge_dir, "testproj", "f.md", "Файл ui.py",
         "текст\n\n## Рефлексы\n- файл: memory_compiler/ui.py")
    em = rx.find_memos("error", "Exit code 1\nUnable to start subsystem: sftp")
    fm = rx.find_memos("file", r"C:\x\memory_compiler\ui.py")
    assert [m.file for m in em] == ["e.md"] and em[0].targets == []
    assert [m.file for m in fm] == ["f.md"] and fm[0].targets == []


def test_rescan_waits_for_ttl_or_invalidate(knowledge_dir, monkeypatch):
    monkeypatch.setattr(rx, "REFLEX_RESCAN_SEC", 3600)
    rx.invalidate()
    path = _art(knowledge_dir, "testproj", "late.md", "Поздняя", "тело")
    assert rx.find_memos("target", "nas-late") == []
    path.write_text(path.read_text(encoding="utf-8") + "\n## Рефлексы\n- цель: nas-late\n",
                    encoding="utf-8")
    assert rx.find_memos("target", "nas-late") == []          # кэш ещё свежий
    rx.invalidate()
    assert [m.file for m in rx.find_memos("target", "nas-late")] == ["late.md"]


# ─── конкуренция: скан держит замок, event loop и пул потоков ждать не должны ──
def test_invalidate_does_not_wait_for_running_scan():
    rx._lock.acquire()
    try:
        t0 = time.monotonic()
        rx.invalidate()
        assert time.monotonic() - t0 < 0.05
    finally:
        rx._lock.release()


def test_find_serves_snapshot_while_scan_holds_lock(knowledge_dir, fresh):
    _art(knowledge_dir, "testproj", "snap.md", "Снимок", "текст\n\n## Рефлексы\n- цель: nas-snap")
    rx.refresh_index(force=True)
    rx._lock.acquire()
    try:
        t0 = time.monotonic()
        memos = rx.find_memos("target", "nas-snap")
        assert time.monotonic() - t0 < 1.0
        assert [m.file for m in memos] == ["snap.md"]
    finally:
        rx._lock.release()


def test_section_bounds_accepts_custom_titles():
    """Разбор раздела переиспользуется для «## Проверка»: логика границ и блоков кода
    одна на оба раздела, иначе они разойдутся молча."""
    lines = ["# Т", "", "## Проверка", "- команда: /system identity print", "", "## Записи", "х"]
    # Конец — ближайший заголовок (не включается), поэтому пустая строка 4 ещё внутри
    # раздела: ровно так же раздел ведёт себя и для «## Рефлексы».
    assert rx._section_bounds(lines, rx.VERIFY_TITLES) == [(2, 5)]
    # позитивный контроль: прежний вызов без аргумента ищет «## Рефлексы»
    lines2 = ["## Рефлексы", "- цель: 192.0.2.10", "## Записи"]
    assert rx._section_bounds(lines2) == [(0, 2)]
