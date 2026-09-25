"""Ложные авто-апдейты трекеров, которые не закрыл отсев версий-планов (v1.92.4).

Замер 25.09.2026 по журналам базы (_log.md, строки auto_update) и реплей всех save_lesson
проектов с трекерами на коде v1.92.4 нашли ещё три класса:

1. Версия чужого продукта в той же заметке. Кандидатом был максимум по всем версиям
   релевантных предложений: «v1.10.1 апгрейд mcp SDK 1.28.1 + v1.10.2» поднял трекер
   release до 1.28.1, «Claude Desktop 1.52386 (Electron 44.2.0)» — трекер claude-desktop
   до 44.2.0.
2. Обрывок адреса как версия: «A.B.C.xx» давал версию A.B.C, потому что опережающая
   проверка отсекала только «.цифру». Потолок скачка major +100 пропускал такой обрывок
   поверх 1.x, следующие поднимали трекер лестницей.
3. IP-поля: server трекера за сентябрь шесть раз сменился на адреса ДРУГИХ узлов из
   релевантного предложения. Роль-гард молчал: старое значение было описанием, не IP.

Тексты — сжатые пересказы живых заметок. Адреса заменены на RFC 5737, имён клиентов нет.
"""
import pytest

from memory_compiler.storage import (
    auto_update_tracking, extract_facts_from_text, load_tracking, save_tracking_article,
)

HOME_EXIT = "домашний выход (VPS уровня 1)"


def _field(entity: str, key: str) -> str:
    return str(load_tracking("testproj", entity)["current"][key])


# ─── Класс 1: версия чужого продукта ─────────────────────────────────────────


def test_foreign_sdk_version_does_not_win_over_own_release(knowledge_dir):
    """Журнал 16.07: заметка о двух выпусках и апгрейде SDK. Максимум по версиям
    заметки выбрал версию SDK, трекер release ушёл 1.10.1 → 1.28.1."""
    save_tracking_article("testproj", "release", {"version": "1.10.1"})
    auto_update_tracking(
        "testproj",
        "v1.10.1 — апгрейд и пин MCP SDK: mcp[cli]>=1.0.0 (не пиннут, на NAS 1.27.0) → "
        "mcp[cli]==1.28.1. Совместимость: 324 теста против 1.28.1. Проверено: контейнер "
        "mcp 1.28.1, health 1.10.1.\n"
        "v1.10.2 — регламент: деплой и живая проверка перед тегом.",
        topic="v1.10.1 апгрейд mcp SDK 1.28.1 + v1.10.2 регламент (деплой перед тегом)")
    assert _field("release", "version") == "1.10.2"


def test_foreign_sdk_version_does_not_win_on_release_tag_branch(knowledge_dir):
    """Та же заметка с тегом release: ветка release-тега в save_lesson брала максимум
    по версиям заголовка и тоже выбирала версию SDK."""
    import asyncio
    from memory_compiler.handlers import save_lesson
    save_tracking_article("testproj", "release", {"version": "1.10.1"})
    asyncio.run(save_lesson(
        "v1.10.1 апгрейд mcp SDK 1.28.1 + v1.10.2 регламент (деплой перед тегом)",
        "Два релиза. v1.10.1 — пин mcp[cli]==1.28.1, health 1.10.1. v1.10.2 — регламент.",
        "testproj", ["release"]))
    assert _field("release", "version") == "1.10.2"


def test_foreign_version_in_topic_does_not_send_release_tag_branch_to_body(knowledge_dir):
    """Журнал 29.07: «mcp 1.29.0: бамп оправдан …» с тегом release. Версия заголовка
    чужая, и если отсев шёл бы до выбора источника, ветка ушла бы в тело и взяла «2.0.0»
    из рассказа про ветку SDK 2.x (так v1.92.4 записал обрывок IP)."""
    import asyncio
    from memory_compiler.handlers import save_lesson
    save_tracking_article("testproj", "release", {"version": "1.51.1"})
    asyncio.run(save_lesson(
        "mcp 1.29.0: бамп оправдан ради report_progress и лимита 4 МиБ",
        "В релизных заметках 2.0.0 и 2.0.0rc1 прогресс указан как отсутствующий: "
        "ни на 1.28.1, ни на 2.0.0 его нет.",
        "testproj", ["release", "mcp"]))
    assert _field("release", "version") == "1.51.1"


def test_runtime_version_in_parentheses_does_not_move_app_tracker(knowledge_dir):
    """Журнал 11.09: «Стенд: Claude Desktop 1.52386 (Electron 44.2.0)». Трекер узнал
    заметку по своему полю electron и взял версию Electron — 1.52386.0.0 → 44.2.0."""
    save_tracking_article("testproj", "claude-desktop",
                          {"version": "1.52386.0.0", "electron": "44.2.0"})
    auto_update_tracking(
        "testproj",
        "РЕЗУЛЬТАТ: фикс подтверждён. Стенд: Claude Desktop 1.52386 (Electron 44.2.0).",
        topic="Фикс Browser tools в Claude Desktop подтверждён тестом")
    assert _field("claude-desktop", "version") == "1.52386.0.0"


def test_own_release_named_next_to_other_product_still_moves_tracker(knowledge_dir):
    """Позитивный контроль: версия, которую заметка хоть раз называет своим выпуском,
    остаётся кандидатом, даже если рядом она же стоит после имени продукта."""
    save_tracking_article("testproj", "release", {"version": "1.4.0"})
    auto_update_tracking(
        "testproj",
        "PR #28 и #31 влиты, release v1.5.0 опубликован. В сборке Server Panel 1.5.0.",
        topic="Мониторинг GitHub")
    assert _field("release", "version") == "1.5.0"


@pytest.mark.parametrize("text, names, want", [
    ("Стенд: Claude Desktop 1.52386 (Electron 44.2.0)", ("claude-desktop",), {"44.2.0"}),
    ("апгрейд mcp SDK 1.28.1, пин mcp[cli]==1.28.1, против 1.28.1", ("release",), {"1.28.1"}),
    ("SDK обновлён до 1.29.0", ("release",), {"1.29.0"}),
    ("NAS обновлён до 1.2.4", ("nas",), set()),
    # перечисление наследует владельца первой версии — и чужого, и своего
    ("tapo_control 7.1.26/7.1.27, xiaomi_miot 1.1.1→1.1.4", ("release",),
     {"7.1.26", "7.1.27", "1.1.1", "1.1.4"}),
    ("Сессия ранжирования: v1.30.0 → 1.40.0, двенадцать релизов", ("release",), set()),
    ("Релиз v1.3.223 + 1.3.224 и деплой", ("release",), set()),
    # vX.Y.Z после имени — не чужая: так пишут и свои теги
    ("memory-compiler v1.20.1 — фикс IP-коллизии", ("release",), set()),
    ("hysteria v2.11.0 / Blitz 2.5.3", ("release",), {"2.5.3"}),
    # своя хоть раз — своя везде
    ("релиз v1.5.0 опубликован, в сборке Server Panel 1.5.0", ("release",), set()),
    ("CLAUDE_CODE_DESKTOP_APP_VERSION=1.52386.6.0", ("claude-desktop",), set()),
    # обычное строчное слово английской прозы — не имя продукта (находка ревью)
    ("deployment: running 1.4.2 in prod, fixed in 1.4.2", ("deployment",), set()),
    ("pytest 9.0.2 зелёный, xiaomi_miot 1.1.5, node-red 4.0.2", ("release",),
     {"1.1.5", "4.0.2"}),
])
def test_foreign_versions_by_word_before_version(text, names, want):
    from memory_compiler.storage import foreign_versions
    assert foreign_versions(text, names) == want


def test_project_named_before_version_is_own_release(knowledge_dir):
    """Позитивный контроль: имя проекта перед голой версией — свой выпуск, не чужой."""
    save_tracking_article("testproj", "deployment", {"version": "1.3.237"})
    auto_update_tracking("testproj", "Testproj 1.3.238 на проде, deployment проверен.",
                         topic="Выкатка")
    assert _field("deployment", "version") == "1.3.238"


# ─── Класс 2: обрывок адреса как версия ──────────────────────────────────────


@pytest.mark.parametrize("text", [
    "обрывки IP вида 198.51.100.xx поднимали трекер",
    "подсеть 192.0.2.x — офис",
    "подсеть 192.0.2.х — кириллическая х",
    "allowed-address 203.0.113.X/32",
    "соединения к 198.51.100.x:443",
    "маска 192.0.2.* в правиле",
])
def test_masked_address_is_not_a_version(text):
    assert extract_facts_from_text(text).get("version") is None


@pytest.mark.parametrize("text, want", [
    ("Выпущен v1.92.2.", ["1.92.2"]),
    ("установщик Panel-Setup-1.1.0.exe", ["1.1.0"]),
    ("git diff v1.3.157..HEAD", ["1.3.157"]),
])
def test_version_before_dot_is_still_a_version(text, want):
    assert extract_facts_from_text(text).get("version") == want


def test_address_fragments_do_not_climb_the_tracker(knowledge_dir):
    """Живая лестница: трекер, уже испорченный обрывком адреса, следующим обрывком
    поднимался дальше (95 → 192): скачок major в пределах +100 гард пропускал."""
    save_tracking_article("testproj", "release", {"version": "192.0.2"})
    auto_update_tracking("testproj", "release 192.0.2: трафик из подсети 198.51.100 отклонён",
                         topic="Проверка")
    assert _field("release", "version") == "192.0.2"


def test_next_major_is_still_accepted(knowledge_dir):
    """Позитивный контроль гарда: переход на следующий major — обычный выпуск."""
    save_tracking_article("testproj", "release", {"version": "1.9.3"})
    auto_update_tracking("testproj", "release v2.0.0 выпущен", topic="Выпуск")
    assert _field("release", "version") == "2.0.0"


# ─── Класс 3: IP-поля ────────────────────────────────────────────────────────


def test_description_in_ip_field_is_not_replaced_by_address(knowledge_dir):
    """Журнал 13.09: server хранил описание «VPS-2, хостинг, Токио» и стал адресом.
    Роль-гард молчал, потому что старое значение — не IP."""
    save_tracking_article("testproj", HOME_EXIT, {
        "server": "VPS-2, хостинг HFC 12 GB, Токио", "ipv4": "203.0.113.10"})
    auto_update_tracking("testproj", "Xray на VPS-2 слушает 203.0.113.10:443.",
                         topic="Дом ушёл со старого Токио на VPS-2")
    assert _field(HOME_EXIT, "server") == "VPS-2, хостинг HFC 12 GB, Токио"


def test_other_node_in_same_sentence_does_not_take_ip_field(knowledge_dir):
    """Журнал 15–21.09: предложение, узнанное по адресу самого узла, называло туннель
    соседнего — server получал первый адрес предложения, то есть чужой."""
    save_tracking_article("testproj", HOME_EXIT, {"server": "203.0.113.10", "ssh": "vps-2"})
    auto_update_tracking(
        "testproj",
        "БОЕВОЙ КАНАЛ: с NAS через socks5h://192.0.2.2:1080 выходной IP = 203.0.113.10 "
        "(Токио, VPS-2) — верно.",
        topic="Вечерняя проверка: всё в норме")
    assert _field(HOME_EXIT, "server") == "203.0.113.10"


def test_address_list_with_slash_is_not_a_subnet(knowledge_dir):
    """Журнал 21.09: «triggers по vps-2/nas/203.0.113.10/192.0.2.100». Адрес перед «/1…»
    читался как подсеть и выпадал, второй оставался единственным и уходил в server."""
    save_tracking_article("testproj", HOME_EXIT, {"server": "192.0.2.20", "ipv4": "203.0.113.10"})
    auto_update_tracking(
        "testproj",
        "Сохранено как bug-статья с triggers по vps-2/nas/203.0.113.10/192.0.2.100 — "
        "напомнит использовать connectionName.",
        topic="Починка бага MCP ssh: server= вместо connectionName=")
    assert _field(HOME_EXIT, "server") == "192.0.2.20"


@pytest.mark.parametrize("text, want", [
    ("triggers по 203.0.113.10/192.0.2.100", ["203.0.113.10", "192.0.2.100"]),
    ("сеть 192.0.2.0/24 закрыта", None),
])
def test_slash_after_address_is_a_subnet_only_with_prefix_length(text, want):
    assert extract_facts_from_text(text).get("ip") == want
