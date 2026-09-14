"""Раздел «## Проверка»: исполняемая цитата факта об узле.

Цитата — это команда, которую агент выполнит на боевом железе. Поэтому приём строгий:
read-only глагол, никаких цепочек и перенаправлений."""
import pytest
from memory_compiler import reflexes as rx


@pytest.mark.parametrize("command", [
    "/system identity print", "docker ps", "stat /volume1/knowledge",
    "curl -I http://192.0.2.10:8765/api/health", "systemctl status nginx",
    "cat /etc/hostname", "ssh -p 2222 node ls",
])
def test_read_only_commands_are_accepted(command):
    """Позитивный контроль: без него «отвергнуто» проходило бы и на сломанном приёме.
    `ssh -p 2222` — порт через пробел, это не пароль. Объединяет прежний
    test_stable_commands_still_accepted (ревью 14.09.2026: дублировал этот набор)."""
    assert rx.verification_problem("команда", command) is None


@pytest.mark.parametrize("command", [
    "rm -rf /tmp/x", "/system reset-configuration", "ip address add 192.0.2.5/24",
    "docker restart memory-compiler-mcp", "cat /etc/passwd > /tmp/leak",
    "show version && rm -rf /", "print; reboot", "echo $(rm -rf /)",
])
def test_writing_and_chained_commands_are_rejected(command):
    assert rx.verification_problem("команда", command) is not None


@pytest.mark.parametrize("command", [
    "/system identity print reset-configuration",
    "docker ps remove-all",
    "systemctl status delete-old-logs",
])
def test_destructive_subcommand_through_hyphen_is_rejected(command):
    """Запрет обязан быть сильнее разрешения, в том числе у составных подкоманд.

    «reset-configuration» не совпадает с «reset» по точному сравнению слов, поэтому сброс
    конфигурации RouterOS проезжал как годная цитата за любым читающим глаголом (ревью
    13.09.2026). Цитату исполняет агент на боевом железе — цена пропуска несоизмерима с
    ценой ложного отказа."""
    assert rx.verification_problem("команда", command) is not None


def test_expected_value_must_be_plain_single_line():
    assert rx.verification_problem("ожидается", "KHV-GW") is None
    assert rx.verification_problem("ожидается", "") is not None
    assert rx.verification_problem("ожидается", "две\nстроки") is not None


ARTICLE = ("# Роутер KHV\n\n**Дата:** 2026-09-13\n\n## Рефлексы\n- цель: 192.0.2.10\n\n"
           "## Записи\n\n### 2026-09-13\nработает\n")


def test_parse_verify_reads_pairs():
    text = ARTICLE + "\n## Проверка\n- команда: /system identity print\n- ожидается: KHV-GW\n"
    assert rx.parse_verify(text) == [("/system identity print", "KHV-GW")]


def test_parse_verify_ignores_fenced_and_unpaired():
    """Команда без «ожидается» — не проверка: сверять будет нечего. Блок кода — не раздел."""
    text = (ARTICLE + "\n## Проверка\n- команда: /system identity print\n"
            "```\n- команда: rm -rf /\n- ожидается: нет\n```\n")
    assert rx.parse_verify(text) == []


def test_add_verify_writes_section_and_rejects_dangerous():
    text, added, rejected = rx.add_verify(ARTICLE, ["/system identity print => KHV-GW"])
    assert added == [("/system identity print", "KHV-GW")]
    assert "## Проверка" in text and "- ожидается: KHV-GW" in text
    assert rx.parse_verify(text) == added        # записанное читается обратно
    _t, added2, rejected2 = rx.add_verify(ARTICLE, ["rm -rf / => пусто"])
    assert added2 == [] and rejected2 and "читать состояние" in rejected2[0][1]


def test_add_verify_requires_target_trigger():
    """Проверять нечего, если статья не привязана к узлу."""
    no_target = "# Просто статья\n\n**Дата:** 2026-09-13\n\n## Записи\n\nтекст\n"
    _t, added, rejected = rx.add_verify(no_target, ["docker ps => memory-compiler"])
    assert added == [] and "цель" in rejected[0][1]


@pytest.mark.parametrize("secret_body", [
    "логин UserAI/Zq7-demo-Pass9\n",
    "password=Zq7-demo-Pass9\n",
    "пароль: Zq7-demo-Pass9\n",
])
def test_secret_leak_catches_password_glued_to_neighbour(secret_body):
    """Тело секрета режется не только по пробелам.

    «логин/пароль» и «password=…» — обычная форма записи доступов. При разрезе лишь по
    пробелам пароль остаётся склеенным с соседом, сравнение по подстроке его не находит,
    и защита начинает зависеть от вёрстки строки в статье (ревью 13.09.2026)."""
    _t, added, rejected = rx.add_verify(
        ARTICLE, ["curl -I http://192.0.2.10/Zq7-demo-Pass9 => 200"], secret_body=secret_body)
    assert added == []
    assert "секрет" in rejected[0][1]


def test_open_parts_of_article_are_not_treated_as_secret():
    """Адрес узла открыт в самой статье (триггер «цель:»), секретом он не является.

    Иначе самая естественная цитата к секретной статье — проверка того самого адреса —
    отвергается, и фича бесполезна там, где нужнее всего: на секреты приходится 13% чтений."""
    # Значение в теле — «nas-arelion.local», и оно же стоит в цитате: без исключения
    # открытого текста проверка отвергла бы её как утечку секрета.
    body = "хост nas-arelion.local, пароль Zq7-demo-Pass9\n"
    article = ARTICLE.replace("- цель: 192.0.2.10",
                              "- цель: 192.0.2.10\n- цель: nas-arelion.local")
    _t, added, rejected = rx.add_verify(
        article, ["curl -I http://nas-arelion.local:8765/api/health => 200"], secret_body=body)
    assert added == [("curl -I http://nas-arelion.local:8765/api/health", "200")], rejected
    # позитивный контроль: пароль из того же тела по-прежнему не пропускается
    _t2, added2, rejected2 = rx.add_verify(
        article, ["curl -I http://nas-arelion.local/Zq7-demo-Pass9 => 200"], secret_body=body)
    assert added2 == [] and "секрет" in rejected2[0][1]


def test_verify_must_not_carry_secret_value():
    """Раздел «## Проверка» не шифруется: значение из тела секрета в команду не пускаем."""
    secret_body = "логин UserAI / пароль Zq7-demo-Pass9\n"     # синтетика, не боевой
    _t, added, rejected = rx.add_verify(
        ARTICLE, ["curl -I http://192.0.2.10/Zq7-demo-Pass9 => 200"], secret_body=secret_body)
    assert added == [] and "секрет" in rejected[0][1]
    # позитивный контроль: обычная цитата в секретную статью проходит
    _t2, added2, _r2 = rx.add_verify(ARTICLE, ["docker ps => memory-compiler"],
                                     secret_body=secret_body)
    assert added2 == [("docker ps", "memory-compiler")]


# ─── живая карточка: цитата проверки и штамп свежести в памятке ──────────────
@pytest.fixture
def fresh(monkeypatch):
    """Как в tests/test_reflexes.py: индекс рефлексов не кэшируется между тестами."""
    monkeypatch.setattr(rx, "REFLEX_RESCAN_SEC", 0)
    rx.invalidate()


def test_memo_carries_verify_and_stamp(knowledge_dir, fresh):
    import memory_compiler.config as cfg
    (knowledge_dir / "testproj" / "router.md").write_text(
        ARTICLE + "\n## Проверка\n- команда: /system identity print\n- ожидается: KHV-GW\n",
        encoding="utf-8")
    cfg.probe_stamp("testproj/router.md", "verified")
    memo = rx.find_memos("target", "192.0.2.10")[0]
    assert memo.verify == [("/system identity print", "KHV-GW")]
    assert memo.probe["level"] == "verified"
    text = rx.render("target", [memo], "192.0.2.10")
    assert "проверено" in text and "/system identity print" in text


def test_stamp_set_after_indexing_reaches_the_card(knowledge_dir, fresh):
    """Штамп обязан доехать до карточки БЕЗ правки статьи и без инвалидации индекса.

    ⚠️ Дефект, найденный живой проверкой 14.09.2026: штамп снимался при индексации, а
    индекс перечитывает статью только по смене её подписи (mtime/ctime/размер). Сайдкар
    статью не меняет — на проде `/api/probe` проштамповал три статьи, а следующий
    `/api/reflex` вернул `probe: {}` у всех. Соседние тесты этого не видели: они ставят
    штамп до первого обхода базы, поэтому индекс подхватывал его заодно.

    Здесь порядок обратный и рескан запрещён: сначала индекс прогревается обходом, затем
    ставится штамп, а статья остаётся нетронутой — ровно как в бою."""
    import memory_compiler.config as cfg
    (knowledge_dir / "testproj" / "router.md").write_text(ARTICLE, encoding="utf-8")
    assert rx.find_memos("target", "192.0.2.10"), "статья должна находиться по цели"
    # Индекс собран. Дальше рескан запрещён — так штамп может доехать только чтением
    # в момент выдачи, а не через пересборку.
    import memory_compiler.reflexes as rx_mod
    monkey_rescan = rx_mod.REFLEX_RESCAN_SEC
    rx_mod.REFLEX_RESCAN_SEC = 10_000
    try:
        cfg.probe_stamp("testproj/router.md", "verified")
        memo = rx.find_memos("target", "192.0.2.10")[0]
        assert memo.probe.get("level") == "verified", "штамп не доехал до карточки"
        assert "проверено живой командой" in rx.render("target", [memo], "192.0.2.10")
    finally:
        rx_mod.REFLEX_RESCAN_SEC = monkey_rescan


@pytest.mark.parametrize("broken", ["не словарь", {"level": "verified"}, {"date": "2026-09-13"}])
def test_broken_sidecar_hides_stamp_but_not_the_card(knowledge_dir, fresh, broken):
    """Порча сайдкара гасит штамп, а не выдачу памятки.

    `.article_meta.json` — вспомогательный файл: его правят руками, он переживает сбои
    записи. Разбор статьи весь обёрнут в try/except, и штамп не должен быть единственным
    местом, где битый файл роняет карточку целиком (ревью 13.09.2026)."""
    import memory_compiler.config as cfg
    (knowledge_dir / "testproj" / "router.md").write_text(ARTICLE, encoding="utf-8")
    cfg.article_meta["testproj/router.md"] = {"last_probe": broken}
    memo = rx.find_memos("target", "192.0.2.10")[0]
    assert memo.probe == {}
    assert "192.0.2.10" in rx.render("target", [memo], "192.0.2.10")


def test_stale_stamp_is_a_hint_not_a_hide(knowledge_dir, fresh):
    """Протухший факт остаётся в выдаче: это подсказка перепроверить, а не удаление."""
    import memory_compiler.config as cfg
    (knowledge_dir / "testproj" / "router.md").write_text(ARTICLE, encoding="utf-8")
    cfg.probe_stamp("testproj/router.md", "stale")
    memo = rx.find_memos("target", "192.0.2.10")[0]
    assert "перепроверк" in rx.render("target", [memo], "192.0.2.10")


# ─── вердикт по каждой цитате: чтение штампа в момент выдачи ─────────────────
QUOTE_A, QUOTE_B, QUOTE_C = ("/system identity print", "/system resource print",
                             "/ip address print")
KEY = "testproj/router.md"


def _article_with(quotes):
    """Статья про узел с заданными цитатами проверки (пустой список — раздела нет)."""
    if not quotes:
        return ARTICLE
    section = "".join(f"- команда: {c}\n- ожидается: {e}\n" for c, e in quotes)
    return ARTICLE.replace("## Записи", "## Проверка\n" + section + "\n## Записи")


def _write(knowledge_dir, quotes):
    (knowledge_dir / "testproj" / "router.md").write_text(
        _article_with(quotes), encoding="utf-8")


def _stamp(check_by_command):
    import memory_compiler.config as cfg
    cfg.article_meta[KEY] = {"checks": check_by_command}


def test_card_shows_the_worst_verdict_among_current_quotes(knowledge_dir, fresh):
    """У статьи несколько цитат, вердикт у каждой свой — показываем ХУДШИЙ: пока хоть одна
    цитата протухла, статья врёт, и предупреждение важнее галочки соседней команды."""
    _write(knowledge_dir, [(QUOTE_A, "KHV-GW"), (QUOTE_B, "RB5009")])
    _stamp({QUOTE_A: {"date": "2026-09-10T10:00:00", "level": "stale"},
            QUOTE_B: {"date": "2026-09-12T10:00:00", "level": "verified"}})
    memo = rx.find_memos("target", "192.0.2.10")[0]
    assert memo.probe == {"level": "stale", "date": "2026-09-10T10:00:00"}
    assert "перепроверк" in rx.render("target", [memo], "192.0.2.10")
    # Цитаты статьи — аргумент: без них сверять ключи не с чем, и вердикт не показывается.
    assert rx.probe_stamp_of(KEY, [(QUOTE_A, "KHV-GW")])["level"] == "stale"
    assert rx.probe_stamp_of(KEY) == {}


def test_card_takes_the_latest_date_of_the_shown_level(knowledge_dir, fresh):
    """Дата — самая свежая среди записей ПОКАЗАННОГО уровня, а не по статье целиком: иначе
    «требует перепроверки (с 13.09)» указывало бы на дату чужой удачной проверки."""
    _write(knowledge_dir, [(QUOTE_A, "KHV-GW"), (QUOTE_B, "RB5009"), (QUOTE_C, "192.0.2.10/24")])
    _stamp({QUOTE_A: {"date": "2026-09-10T10:00:00", "level": "stale"},
            QUOTE_B: {"date": "2026-09-13T10:00:00", "level": "verified"},
            QUOTE_C: {"date": "2026-09-11T10:00:00", "level": "stale"}})
    assert rx.find_memos("target", "192.0.2.10")[0].probe == {
        "level": "stale", "date": "2026-09-11T10:00:00"}
    # только галочки — показывается самая свежая из них
    _stamp({QUOTE_A: {"date": "2026-09-10T10:00:00", "level": "verified"},
            QUOTE_B: {"date": "2026-09-13T10:00:00", "level": "verified"}})
    assert rx.find_memos("target", "192.0.2.10")[0].probe == {
        "level": "verified", "date": "2026-09-13T10:00:00"}


def test_verdict_of_a_removed_quote_does_not_surface(knowledge_dir, fresh):
    """Цитату убрали из статьи — её вердикт больше не про эту статью.

    Иначе stale от давно удалённой команды висел бы на живом факте вечно: перепроверить
    его нечем, такой цитаты в статье уже нет."""
    _write(knowledge_dir, [(QUOTE_A, "KHV-GW"), (QUOTE_B, "RB5009")])
    _stamp({QUOTE_A: {"date": "2026-09-10T10:00:00", "level": "stale"},
            QUOTE_B: {"date": "2026-09-12T10:00:00", "level": "verified"}})
    assert rx.find_memos("target", "192.0.2.10")[0].probe["level"] == "stale"
    _write(knowledge_dir, [(QUOTE_B, "RB5009")])        # цитата A удалена из статьи
    assert rx.find_memos("target", "192.0.2.10")[0].probe["level"] == "verified"
    _write(knowledge_dir, [])                           # цитат не осталось вовсе
    memo = rx.find_memos("target", "192.0.2.10")[0]
    assert memo.probe == {}
    assert "192.0.2.10" in rx.render("target", [memo], "192.0.2.10")


def test_empty_checks_fall_back_to_last_probe(knowledge_dir, fresh):
    """Пустой блок checks — это «записей нет», а не «вердикты сняты»: читается last_probe,
    как у статьи без checks вовсе (старый хук, ручной вызов ручки)."""
    _write(knowledge_dir, [(QUOTE_A, "KHV-GW")])
    _stamp({})
    import memory_compiler.config as cfg
    cfg.article_meta[KEY]["last_probe"] = {"date": "2026-09-12T10:00:00", "level": "verified"}
    assert rx.find_memos("target", "192.0.2.10")[0].probe["level"] == "verified"


@pytest.mark.parametrize("checks", [
    "не словарь",
    {QUOTE_A: "не словарь"},
    {QUOTE_A: {"level": "verified"}},
    {QUOTE_A: {"date": "2026-09-12T10:00:00"}},
    {QUOTE_A: {"date": 20260912, "level": "verified"}},
    {QUOTE_A: {"date": "2026-09-12T10:00:00", "level": "чепуха"}},
])
def test_broken_checks_hide_the_stamp_but_not_the_card(knowledge_dir, fresh, checks):
    """Сайдкар правят руками и он переживает сбои записи: битая запись ГАСИТ штамп, а не
    роняет карточку — та же граница, что у last_probe. И не откатывается на last_probe:
    раз статья перешла на вердикты по цитатам, старое поле про неё уже не знает."""
    import memory_compiler.config as cfg
    _write(knowledge_dir, [(QUOTE_A, "KHV-GW")])
    _stamp(checks)
    cfg.article_meta[KEY]["last_probe"] = {"date": "2026-09-12T10:00:00", "level": "verified"}
    memo = rx.find_memos("target", "192.0.2.10")[0]
    assert memo.probe == {}
    assert "192.0.2.10" in rx.render("target", [memo], "192.0.2.10")


@pytest.mark.asyncio
async def test_save_lesson_writes_verify_section(knowledge_dir):
    """Цитату задаёт пользователь при сохранении — сервер её не выдумывает."""
    from memory_compiler import handlers
    await handlers.save_lesson(topic="Роутер KHV", content="Адрес и доступы.",
                               project="testproj", triggers=["цель: 192.0.2.10"],
                               verify=["/system identity print => KHV-GW"])
    # ⚠️ Файл берём ПО ИМЕНИ, а не первым из glob("*.md"): в каталоге проекта лежат и
    # статья фикстуры, и служебные _log.md/_active_context.md, которые save_lesson пишет
    # тем же вызовом. Порядок обхода каталога не равен порядку записи — «*.md» отдавал
    # test_article.md, и тест падал при полностью рабочем коде.
    path = next((knowledge_dir / "testproj").glob("роутер*.md"))
    assert rx.parse_verify(path.read_text(encoding="utf-8")) == [
        ("/system identity print", "KHV-GW")]


def test_verify_into_secret_without_content_is_refused(knowledge_dir, monkeypatch):
    """Цитата ложится в ОТКРЫТУЮ часть секрета, а сверить её с телом здесь нечем.

    Хранимое тело намеренно не расшифровывается: иначе секционная правка потребовала бы
    MC_ENCRYPT_KEY (инвариант из test_edit_article_triggers_on_secret_need_no_key). Приняв
    такую цитату молча, сервер вынес бы значение из тела секрета в открытый текст — фича
    против ложного доверия стала бы каналом утечки."""
    import asyncio

    import memory_compiler.config as cfg
    from memory_compiler.handlers import edit_article

    monkeypatch.setattr(cfg, "MC_ENCRYPT_KEY", "")
    path = knowledge_dir / "testproj" / "secret_nas.md"
    body = ("# Доступы NAS 192.0.2.10\n\n**Дата:** 2026-01-01 10:00\n**Секрет:** да\n\n"
            "## Рефлексы\n- цель: 192.0.2.10\n\nENC:abcdef\n")
    path.write_text(body, encoding="utf-8")
    res = asyncio.run(edit_article("testproj", "secret_nas.md",
                                   verify=["/system identity print => KHV-GW"]))
    text = path.read_text(encoding="utf-8")
    assert "не принята" in res[0].text
    assert rx.parse_verify(text) == [], "цитата легла в открытую часть секрета"
    assert text == body, "тело секрета тронуто"

    # ⚠️ ПОЗИТИВНЫЙ КОНТРОЛЬ: та же правка без content, но по обычной статье — цитата
    # принимается. Без него «не принята» проходило бы и на сценарии, где verify не
    # работает вовсе, и тест сторожил бы пустоту.
    plain = knowledge_dir / "testproj" / "router_plain.md"
    plain.write_text("# Роутер KHV\n\n**Дата:** 2026-01-01 10:00\n\n"
                     "## Рефлексы\n- цель: 192.0.2.10\n\n## Записи\nработает\n",
                     encoding="utf-8")
    asyncio.run(edit_article("testproj", "router_plain.md",
                             verify=["/system identity print => KHV-GW"]))
    assert rx.parse_verify(plain.read_text(encoding="utf-8")) == [
        ("/system identity print", "KHV-GW")]


@pytest.mark.asyncio
async def test_verify_parameter_declared_in_schemas():
    """Параметр, которого нет в объявленной схеме, мост Claude Desktop срежет по дороге —
    так уже теряли _client_session (v1.76.0)."""
    from memory_compiler import tools
    schemas = {t.name: t.inputSchema for t in await tools.list_tools()}
    for name in ("save_lesson", "edit_article"):
        assert "verify" in schemas[name]["properties"], name


@pytest.mark.parametrize("command,reason", [
    ("uptime", "читающий глагол"),
    ("cat /tmp/report.txt", "эфемерный путь"),
    ("cat /proc/meminfo", "эфемерный путь"),
    ("curl -u admin:changeme-pass9 http://192.0.2.10/rest/system/resource", "логин с паролем"),
    ("curl http://user:changeme-pass9@192.0.2.10/api", "логин с паролем"),
    ("mysql -pchangemePass9 -e status", "логин с паролем"),
    ("sshpass -p changemePass9 ssh node uptime", "логин с паролем"),
    ("docker ps --filter token=changemePass9", "логин с паролем"),
])
def test_volatile_and_credential_commands_are_rejected(command, reason):
    """Цитату исполняет агент, а раздел «## Проверка» пишется открытым текстом.

    Эфемерный путь протухает к следующей команде и даёт stale на здоровом факте; логин с
    паролем в цитате уехал бы в открытый текст статьи. Замер 14.09.2026: /tmp и счётчики —
    самый частый вид негодного кандидата, креды нашлись в сотнях read-only фрагментов.
    Причина проверяется дословно — иначе «отвергнуто» проходило бы и не по тому основанию."""
    problem = rx.verification_problem("команда", command)
    assert problem is not None and reason in problem


def test_credentials_rejected_in_expected_value():
    problem = rx.verification_problem("ожидается", "password=changeme-pass9")
    assert problem is not None and "логин с паролем" in problem
    assert rx.verification_problem("ожидается", "RB5009UG") is None


# ─── регрессии ревью качества 14.09.2026: регистр, порты, uid:gid, PGPASSWORD, sudo -S ──
@pytest.mark.parametrize("checker,args", [
    (rx.verification_problem, ("команда", "Get-Content -Path C:/x")),
    (rx.verification_problem, ("команда", "Test-NetConnection -Port 443")),
    (rx.credential_problem, ("Select-String -Pattern error app.log",)),
    (rx.credential_problem, ("Select-Object -Property Name",)),
    (rx.credential_problem, ("Copy-Item a b -PassThru",)),
    (rx.verification_problem, ("команда", "redis-cli -p6379 ping")),
    (rx.verification_problem, ("команда", "psql -p5432 -c status")),
    (rx.trigger_problem, ("error", "Get-Content -Path 'C:/x' : Cannot find path")),
    (rx.credential_problem, ("docker run -u 1000:1000 alpine id",)),
    (rx.credential_problem, ("sudo -s",)),
], ids=["powershell-path", "powershell-port", "powershell-pattern", "powershell-property",
        "powershell-passthru", "redis-port", "psql-port",
        "trigger-powershell-path", "docker-uid-gid", "sudo-shell"])
def test_powershell_ports_and_uid_are_not_credentials(checker, args):
    """Позитивный контроль к находкам I-1/I-2 ревью качества (регрессия задачи 2).

    Общий `re.IGNORECASE` у кредов ловил параметры PowerShell (`-Path`, `-Port`,
    `-PassThru`, `-Property`) — дословная ошибка PowerShell как триггер отвергалась с
    причиной про пароль, хотя до задачи 2 принималась. «-p»/«-u» без разбора числового
    аргумента путали порт redis/psql и uid:gid `docker run` с паролем."""
    assert checker(*args) is None


@pytest.mark.parametrize("value", [
    # раунд 1 (v1.78.x): \b не срабатывает после словного символа, дефис в X-API-KEY,
    # Authorization Bearer/Basic и sudo -S не проверялись вовсе.
    "PGPASSWORD=changeme-pass9 psql -h 192.0.2.10",
    "MYSQL_PWD=changeme-pass9 mysql",
    "access_token=changeme-pass9",
    "curl -H X-API-KEY=changeme-pass9 http://192.0.2.10/",
    "Authorization: Bearer changeme-pass9-token",
    "Authorization: Basic Y2hhbmdlbWU=",
    "printf changeme-pass9 | sudo -S systemctl status nginx",
    "--password=changeme-pass9",
    "-p=changeme-pass9",
    "curl -I http://192.0.2.10/health?token=changeme-pass9",
    # раунд 2 (ревью 14.09.2026)
    "DB_PASS=changemePass9 docker compose ps",              # п.2 pass= / *_PASS=
    "MYSQL_PASS=changemePass9 mysql",                       # п.2
    "pass = changemePass9",                                 # п.2 пробелы вокруг «=»
    "New-LocalUser -Name x -Password changemePass9",        # п.3 -Password <значение>
    "vrunner --db-user admin --db-pwd changemePass9",       # п.3 --pwd <значение>
    "wget --password changemePass9 https://h/x",            # п.3 --password <значение>
    "rac infobase summary list --cluster-pwd=changemePass9",  # п.3 --*-pwd= (без регрессии)
    "Authorization: Token changemePass9",                   # п.8 Token
    "Authorization: ApiKey changemePass9",                  # п.8 ApiKey
    'curl -H "X-API-Key: changemePass9" https://h',          # п.9 заголовок-секрет
    'curl -H "X-Auth-Token: changemePass9" https://h',       # п.9
    'curl -H "PRIVATE-TOKEN: changemePass9" https://h',      # п.9
    "curl --user admin:changemePass9 https://h",             # п.10 --user u:p
    "curl -uadmin:changemePass9 https://h",                  # п.10 -uu:p слитно
    "mysql -pchangemePass9 -e status",                       # п.6 слитный пароль сохранён
])
def test_new_credential_patterns_are_rejected(value):
    """Явные креды в цитате/триггере отвергаются с причиной про логин с паролем.

    Раунд 1 (v1.78.x) и раунд 2 (ревью 14.09.2026: pass= / *_PASS=, -Password/--pwd через
    пробел, заголовки -H, --user / -uu:p слитно, Authorization Token/ApiKey). Причина
    проверяется ДОСЛОВНО — иначе «не None» проходило бы и не по тому основанию."""
    problem = rx.credential_problem(value)
    assert problem is not None and "логин с паролем" in problem


def test_trigger_catches_password_piped_into_sudo():
    """`sudo -S` читает пароль со stdin — конвейер перед ним несёт креды в чистом виде,
    даже без цепочек: у `trigger_problem` (в отличие от `verification_problem`) проверки
    цепочек нет вовсе, и без явного правила на `sudo -S` строка проезжала бы в триггер."""
    problem = rx.trigger_problem(
        "error", "printf changeme-pass9 | sudo -S systemctl status nginx")
    assert problem is not None and "логин с паролем" in problem


@pytest.mark.parametrize("value", [
    "date -u +%H:%M:%S",                                     # п.5 date -u + формат времени
    "docker exec x date -u +'%H:%M:%S'",                     # п.5
    "date -u",                                               # п.5
    "ls -plah /volume1",                                     # п.6 пучок коротких опций
    "ss -plnt",                                              # п.6
    "netstat -plnt",                                         # п.6
    "find /etc/nginx -name x.conf -path y -prune -print",   # п.6 имена опций -p*
    "openssl req -pwfile /tmp/p -new",                       # п.6 -pwfile
    "gcc -pthread -o x x.c",                                 # п.6
    "openssl x509 -in cert.pem -noout -pubkey",             # п.6
    "docker run -p8080:80 nginx",                            # п.6 порт-маппинг -p8080:80
    "PGPASSWORD=$DB_PASS psql -h 192.0.2.10 -c status",     # п.7 ссылка на переменную
    'curl -H "Authorization: Bearer $TOKEN" https://192.0.2.10/api',  # п.7 Bearer $VAR
    'curl -u "admin:$API_PASS" https://192.0.2.10/rest',    # п.7 -u user:$VAR
    'curl -H "X-API-Key: $KEY" https://h',                   # п.7 + п.9 заголовок с $VAR
    "TOKEN=%MYTOKEN% && echo x",                             # п.7 значение %VAR%
    "docker login --password-stdin -u admin",              # п.3 --password-stdin
    "psql --no-password -h 192.0.2.10 -c status",          # п.3 --no-password
    "Set-LocalUser -Name x -PasswordNeverExpires 1",       # п.3 -PasswordNeverExpires
    "New-LocalUser -Name x -Password $pw",                 # п.3 -Password $var
    "New-LocalUser -Name x -Password (Read-Host -AsSecureString)",  # п.3 -Password (...)
    "bypass=1",                                             # п.2 bypass=
    "systemctl show nginx | grep bypass",                  # п.2 слово bypass в тексте
    "surpass=1",                                            # п.2 surpass=
    "curl --no-pass=1 http://h",                            # п.2 --no-pass=
    "OLDPWD=/tmp cd -",                                     # п.4 OLDPWD= (буква перед pwd)
    'curl -H "Content-Type: application/json" https://h',   # п.9 обычный заголовок
])
def test_round2_false_positives_are_not_credentials(value):
    """Ложные отказы, закрытые ревью 14.09.2026: имена/пучки опций `-p*`, `date -u +фмт`,
    ссылки на переменные ($VAR/%VAR%), пробельные флаги PowerShell, `bypass`/`surpass`/
    `--no-pass`, обычные заголовки. Позитивный контроль к new_credential_patterns: без
    него правило, режущее всё подряд, тоже прошло бы тесты на отказ."""
    assert rx.credential_problem(value) is None


def test_credential_regex_has_no_catastrophic_backtracking():
    """п.4: URL-ветка `://[^\\s/@]+:[^\\s/@]+@` на строке двоеточий без «@» давала
    катастрофический бэктрекинг (50 КБ ≈ 7 с до правки; classes `[^\\s/@:]*` и `[^\\s/@]+`
    больше не пересекаются по «:»). credential_problem публичная и длину не режет. Порог
    0.5 с ловит именно катастрофу (секунды): реальный замер после правки — единицы мс."""
    import time
    for payload in ("://" + "a:" * 25000, "://" + ":" * 50000, ":" * 50000,
                    "-p" + "1" * 50000, "-H " + "token" * 10000, "x" * 50000):
        t0 = time.perf_counter()
        rx.credential_problem(payload)
        assert time.perf_counter() - t0 < 0.5, f"катастрофический бэктрекинг на {payload[:12]!r}"
