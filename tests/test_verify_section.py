"""Раздел «## Проверка»: исполняемая цитата факта об узле.

Цитата — это команда, которую агент выполнит на боевом железе. Поэтому приём строгий:
read-only глагол, никаких цепочек и перенаправлений."""
import pytest
from memory_compiler import reflexes as rx


@pytest.mark.parametrize("command", [
    "/system identity print", "docker ps", "stat /volume1/knowledge",
    "curl -I http://192.0.2.10:8765/api/health", "systemctl status nginx",
])
def test_read_only_commands_are_accepted(command):
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
