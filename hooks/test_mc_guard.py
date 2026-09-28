# -*- coding: utf-8 -*-
"""Тесты сторожа памяти mc_guard (единый скрипт для Claude Code и Kimi Code).

Запуск:  MC_GUARD_CLIENT=claude python test_mc_guard.py   — профиль Claude Code
         MC_GUARD_CLIENT=kimi python test_mc_guard.py     — профиль Kimi Code
         (без MC_GUARD_CLIENT — claude, консервативно)
         флаг --live — плюс живой досыл по REST (создаёт и оставляет статью,
         удалить вручную)

Профиль-зависимые проверки (блок Stop: JSON-decision против stderr+exit 2;
текстовый против JSON вывод emit; пути состояния) ветвятся по PROFILE.
Процессные тесты передают профиль дочернему процессу через MC_GUARD_CLIENT.
"""
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

PROFILE = "kimi" if "kimi" in (os.environ.get("MC_GUARD_CLIENT") or "").lower() else "claude"
HOOKS_SUBDIR = ".kimi-code" if PROFILE == "kimi" else ".claude"

spec = importlib.util.spec_from_file_location(
    "mc_guard", str(pathlib.Path(__file__).with_name("mc_guard.py")))
mg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mg)
mg._set_client(PROFILE)

# Хук читает чужие конфиги машины (ssh-mcp.json, конфиг Desktop). Тесты их видеть НЕ должны:
# реальный адрес роутера поменял бы ожидания, и тесты зависели бы от машины.
mg.SSH_MCP_CONFIG = pathlib.Path(tempfile.gettempdir()) / "mc-guard-test-no-ssh-mcp.json"
mg.DESKTOP_CONFIG = pathlib.Path(tempfile.gettempdir()) / "mc-guard-test-no-desktop.json"
mg._target_map_cache = None

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


# --------------------------------------------------- профиль-зависимые хелперы
def _stop_outcome(sid):
    """(заблокирован ли ход, текст причины) от cmd_stop — по профилю клиента.

    kimi: блок — это exit 2 и причина текстом в stderr, stdout пуст.
    claude: блок — это JSON {"decision": "block", "reason": ...} через emit, rc 0.
    """
    import contextlib
    import io
    if PROFILE == "kimi":
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = mg.cmd_stop({"session_id": sid, "transcript_path": ""})
        return rc == 2, buf.getvalue()
    got = []
    saved_emit = mg.emit
    mg.emit = lambda payload: got.append(payload)
    try:
        mg.cmd_stop({"session_id": sid, "transcript_path": ""})
    finally:
        mg.emit = saved_emit
    text = " ".join(str(g.get("reason") or "") for g in got)
    blocked = any(g.get("decision") == "block"
                  or (g.get("hookSpecificOutput") or {}).get("decision") == "block"
                  for g in got)
    return blocked, text


def _hook_env(home, **extra):
    """Окружение процессного прогона: изолированный HOME и профиль клиента."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    env.update(USERPROFILE=str(home), APPDATA=str(home), MC_GUARD_CLIENT=PROFILE)
    env.update(extra)
    return env


def _state_dir(home):
    """Каталог состояния хука при HOME=home для текущего профиля."""
    return home / HOOKS_SUBDIR / "hooks" / "state"


# --------------------------------------------------- детектор удалённых команд
def hits(cmd):
    m = mg.HEREDOC_RE.search(cmd)
    if m:
        cmd = cmd[:m.start()]
    return bool(mg.REMOTE_CMD_RE.search(cmd))


BLOCK = [
    "ssh admin@192.0.2.10 uptime",
    "ssh -p 2222 UserAI@nas 'sudo docker restart mc'",
    "cd /tmp && ssh nas ls",
    "scp file.tar UserAI@nas:/tmp/",
    "sudo ssh root@10.0.0.1",
    "cat x | ssh nas 'cat > /tmp/y'",
    "$(ssh nas hostname)",
    "Enter-PSSession -ComputerName SRV1",
    "plink -batch admin@rb5009 /export",
]
PASS = [
    "ls -la /tmp",
    "cd /c/Users/tester/.claude/skills/memory-autopilot && cp SKILL.md SKILL.md.bak",
    "grep -n ssh ~/.ssh/config",
    "echo 'ищи доступы пароль ssh в базе'",
    "python -c \"print('ssh')\"",
    "git commit -m 'фикс: ssh-доступ описан в статье'",
    "cat > f.md <<'EOF'\nтекст про ssh nas и scp file\nEOF",
    "ls ~/.ssh/",
    "docker ps | grep sshd",
]
for c in BLOCK:
    check(hits(c), "ПРОПУЩЕНА (должна блокироваться): " + c)
for c in PASS:
    check(not hits(c), "ЛОЖНЫЙ БЛОК: " + c)


# ------------------------------------------------------------------- очередь
tmp = pathlib.Path(tempfile.mkdtemp(prefix="mcq_"))
mg.PENDING_DIR = tmp / "pending"
mg.STATE_DIR = tmp / "state"
mg.HOOK_LOG = tmp / "hooks.log"      # журнал статистики боевой — тесты в него не пишут

EV_SAVE = {
    "session_id": "q-test", "tool_use_id": "toolu_AAA",
    "tool_name": "mcp__memory-compiler__save_lesson",
    "tool_input": {"topic": "Тема урока", "content": "Тело урока",
                   "project": "general", "tags": ["тест"]},
}

# 1. intent кладёт payload в очередь
mg.cmd_intent(EV_SAVE)
q = mg._pending_all()
check(len(q) == 1, "intent не создал запись очереди")
rec = mg._pending_load(q[0]) if q else {}
check(rec.get("args", {}).get("content") == "Тело урока", "intent потерял content")

# 2. успешный mark снимает запись
mg.cmd_mark(dict(EV_SAVE, tool_name="mcp__memory-compiler__save_lesson"))
check(len(mg._pending_all()) == 0, "mark не снял запись очереди после успеха")

# 3. секрет в очередь без содержимого
EV_SECRET = {
    "session_id": "q-test", "tool_use_id": "toolu_SEC",
    "tool_name": "mcp__memory-compiler__save_secret",
    "tool_input": {"topic": "Доступы к роутеру", "content": "пароль-в-открытую",
                   "project": "infra"},
}
mg.cmd_intent(EV_SECRET)
sec = mg._pending_load(mg._pending_path(EV_SECRET)) or {}
check(sec.get("args", {}).get("content") == "", "СЕКРЕТ УТЁК В ОЧЕРЕДЬ открытым текстом")
check(sec.get("manual_only") is True, "секрет должен досылаться только моделью")
mg._pending_drop(mg._pending_path(EV_SECRET))

# 4. fail оставляет запись и объясняет класс отказа
mg.cmd_intent(EV_SAVE)
out = []
mg.emit = lambda payload: out.append(payload)
mg.cmd_fail(dict(EV_SAVE, tool_response="MCP error -32001: Request timed out"))
check(len(mg._pending_all()) == 1, "fail потерял запись очереди")
ctx = (out[0]["hookSpecificOutput"]["additionalContext"] if out else "")
check("-32001" in ctx and "ПОВТОРИ" in ctx.upper(), "fail не подсказал повтор на -32001")
rec = mg._pending_load(mg._pending_path(EV_SAVE)) or {}
check(rec.get("attempts") == 1, "fail не посчитал попытку")

# 5. свежую запись автодосыл не трогает — сперва шанс модели
sent, left, _desc = mg._flush_queue(force=True)
check(sent == 0 and left == 1, "свежая запись не должна досылаться сразу")

# 6. Stop блокирует, пока очередь не пуста, но не более трёх раз.
# Форма блока профильная: kimi — exit 2 + причина в stderr, claude — JSON decision
# (см. _stop_outcome); проверяемое поведение одно.
st_path = mg.STATE_DIR / "q-test.json"
mg.STATE_DIR.mkdir(parents=True, exist_ok=True)
st_path.write_text(json.dumps({"last_read_ts": time.time()}), encoding="utf-8")
for i in range(5):
    blocked, reason = _stop_outcome("q-test")
    got = blocked and "не записано" in reason
    if i < 3:
        check(got, "Stop не заблокировал ход при непустой очереди (попытка %d)" % (i + 1))
    else:
        check(not got, "Stop долбит про очередь больше трёх раз")

# 7. _already_in_audit: запись, прошедшая повтором, снимается с очереди
audit_rows = [
    {"ts": "2026-08-26 12:00:00", "tool": "save_lesson",
     "args": {"topic": "Тема урока", "project": "general"}},
]
mg._tail_audit = lambda: audit_rows
rec = {"tool": "save_lesson", "ts": "2026-08-26 11:59:00",
       "args": {"topic": "Тема урока", "project": "general"}}
check(mg._already_in_audit(rec), "повторно прошедший вызов не опознан в аудите")
rec_other = dict(rec, args={"topic": "Другая тема", "project": "general"})
check(not mg._already_in_audit(rec_other), "ложное опознание чужой темы в аудите")
rec_old = {"tool": "save_lesson", "ts": "2026-08-26 12:30:00",
           "args": {"topic": "Тема урока", "project": "general"}}
check(not mg._already_in_audit(rec_old),
      "запись из аудита СТАРШЕ попытки не должна считаться доставкой")

# 7b. Очередь принадлежит сессии (инцидент 15.09.2026). Вызов edit_article упал в окне
# рестарта контейнера, повтор с другим tool_use_id прошёл, а исходная запись осталась:
# _already_in_audit ждёт зеркало аудита, которое отстаёт на минуты. Stop показывал её
# ВСЕМ сессиям, и чужая сессия послушно повторила вызов — в статье лёг дубль.
_q_dirs = (mg.PENDING_DIR, mg.STATE_DIR, mg.emit)
tmp_own = pathlib.Path(tempfile.mkdtemp(prefix="mcq_own_"))
mg.PENDING_DIR = tmp_own / "pending"
mg.STATE_DIR = tmp_own / "state"
mg.STATE_DIR.mkdir(parents=True, exist_ok=True)
mg.emit = lambda payload: None
unavailable = "server memory-compiler unavailable"

EV_TRY = {
    "session_id": "q-owner", "tool_use_id": "toolu_TRY1",
    "tool_name": "mcp__memory-compiler__edit_article",
    "tool_input": {"project": "infra", "filename": "veeam.md", "append": True,
                   "content": "кто разбирает"},
}
mg.cmd_intent(EV_TRY)
mg.cmd_fail(dict(EV_TRY, tool_response=unavailable))
# Повтор несёт _client_session: его дописывает хук session_arg, у упавшей попытки его нет.
EV_RETRY = dict(EV_TRY, tool_use_id="toolu_TRY2",
                tool_input=dict(EV_TRY["tool_input"], _client_session="q-owner"))
mg.cmd_intent(EV_RETRY)
mg.cmd_mark(EV_RETRY)
check(not mg._pending_path(EV_TRY).exists(),
      "успешный повтор не снял упавшую попытку того же вызова — Stop потребует повторить уже записанное")

# Позитивный контроль: другой текст — другой вызов, его упавшая попытка остаётся.
EV_OTHER_TEXT = dict(EV_TRY, tool_use_id="toolu_TRY3",
                     tool_input=dict(EV_TRY["tool_input"], content="другая запись"))
mg.cmd_intent(EV_OTHER_TEXT)
mg.cmd_fail(dict(EV_OTHER_TEXT, tool_response=unavailable))
EV_OK4 = dict(EV_TRY, tool_use_id="toolu_TRY4")
mg.cmd_intent(EV_OK4)
mg.cmd_mark(EV_OK4)
check(mg._pending_path(EV_OTHER_TEXT).exists(),
      "успех одного вызова снял упавшую попытку ДРУГОГО, с иным текстом — содержание пропадёт")
mg._pending_drop(mg._pending_path(EV_OTHER_TEXT))

# Успех того же вызова в ЧУЖОЙ сессии попытку владельца не снимает.
mg.cmd_intent(EV_TRY)
mg.cmd_fail(dict(EV_TRY, tool_response=unavailable))
EV_FOREIGN_OK = dict(EV_TRY, session_id="q-other", tool_use_id="toolu_TRY5")
mg.cmd_intent(EV_FOREIGN_OK)
mg.cmd_mark(EV_FOREIGN_OK)
check(mg._pending_path(EV_TRY).exists(),
      "успех ЧУЖОЙ сессии снял упавшую попытку этой — владелец не узнает, что запись не прошла")


def _stop_blocks(sid):
    (mg.STATE_DIR / (sid + ".json")).write_text(
        json.dumps({"last_read_ts": time.time()}), encoding="utf-8")
    blocked, reason = _stop_outcome(sid)
    return blocked and "не записано" in reason


def _hook_context(fn, event):
    got = []
    mg.emit = lambda payload: got.append(payload)
    fn(event)
    return " ".join((g.get("hookSpecificOutput") or {}).get("additionalContext", "")
                    for g in got)


# Запись EV_TRY висит у q-owner. Stop останавливает только её владельца.
check(not _stop_blocks("q-bystander"),
      "Stop остановил сессию ЧУЖОЙ записью очереди — так 15.09.2026 посторонняя сессия "
      "повторила чужой edit_article и записала дубль")
check(_stop_blocks("q-owner"), "Stop не остановил сессию-владельца её же незаписанным вызовом")

# Подсказка к сообщению тоже говорит только о своём.
for sid in ("q-bystander", "q-owner"):
    (mg.STATE_DIR / (sid + ".json")).write_text(
        json.dumps({"seen_audit_ts": "2026-08-26 12:00:00"}), encoding="utf-8")
check("veeam.md" not in _hook_context(mg.cmd_freshness, {"session_id": "q-bystander"}),
      "подсказка к сообщению велит повторить ЧУЖОЙ вызов из очереди")
check("veeam.md" in _hook_context(mg.cmd_freshness, {"session_id": "q-owner"}),
      "подсказка к сообщению молчит о своём незаписанном вызове")

# SessionStart: свежая запись живой чужой сессии не показывается — новая сессия её
# повторила бы; брошенная (старше порога) показывается, иначе содержание пропадёт.
check("veeam.md" not in _hook_context(mg.cmd_session_start, {"session_id": "q-newcomer"}),
      "SessionStart показал свежую запись живой чужой сессии")
_p = mg._pending_path(EV_TRY)
_rec = mg._pending_load(_p) or {}
_rec["created"] = time.time() - getattr(mg, "ORPHAN_MIN_AGE", 1800) - 60
_p.write_text(json.dumps(_rec, ensure_ascii=False), encoding="utf-8")
check("veeam.md" in _hook_context(mg.cmd_session_start, {"session_id": "q-newcomer"}),
      "SessionStart не показал брошенную запись чужой сессии — содержание пропадёт")
check(not _stop_blocks("q-bystander"),
      "Stop остановил постороннюю сессию брошенной чужой записью — её место на SessionStart")

# Брошенную запись показали новой сессии, она повторила вызов — запись закрыта, иначе
# её увидит и повторит ещё и следующая сессия.
EV_ADOPT = dict(EV_TRY, session_id="q-newcomer", tool_use_id="toolu_TRY6")
mg.cmd_intent(EV_ADOPT)
mg.cmd_mark(EV_ADOPT)
check(not mg._pending_path(EV_TRY).exists(),
      "повтор брошенной записи другой сессией не снял её — следующая сессия повторит снова")

shutil.rmtree(tmp_own, ignore_errors=True)
mg.PENDING_DIR, mg.STATE_DIR, mg.emit = _q_dirs

# 8. сборка REST-payload из finish_task не теряет итог сессии и вопросы
fin = {"tool": "finish_task", "ts": "2026-08-26 12:00:00",
       "args": {"topic": "Т", "content": "C", "project": "p",
                "session_summary": "SS", "open_questions": "OQ", "tags": ["a"]}}
pl = mg._rest_payload(fin)
check(pl and "SS" in pl["content"] and "OQ" in pl["content"],
      "досыл finish_task теряет session_summary/open_questions")
check(pl and pl["project"] == "p" and pl["tags"] == ["a"], "досыл теряет проект/теги")

# 8b. гейт не трогает посторонние инструменты, даже если матчер клиента их пропустил
mg.HOOK_LOG = tmp / "hooks.log"
gate_out = []
mg.emit = lambda payload: gate_out.append(payload)
for name in ("TaskOutput", "Read", "Glob", "mcp__okdesk__issue_list", "WebFetch"):
    gate_out.clear()
    mg.cmd_gate({"session_id": "gate-x", "tool_name": name, "tool_input": {}})
    check(not gate_out, "гейт вмешался в посторонний инструмент %s" % name)
for name in ("mcp__mikrotik__mikrotik_get_interfaces", "mcp__ssh__execute-command",
             "mcp__synology__list_shares", "mcp__1c__query", "mcp__ftp-zarina__list-directory"):
    gate_out.clear()
    mg.cmd_gate({"session_id": "gate-y-%s" % name, "tool_name": name, "tool_input": {}})
    check(gate_out and gate_out[0]["hookSpecificOutput"]["permissionDecision"] == "deny",
          "гейт пропустил живую инфраструктуру без чтения базы: %s" % name)

# 9. статусная строка не падает и укладывается в одну строку
_sl = []
mg.STATE_DIR.mkdir(parents=True, exist_ok=True)
(mg.STATE_DIR / "sl.json").write_text(
    json.dumps({"last_read_ts": time.time() - 120, "project": "infra"}), encoding="utf-8")
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    mg.cmd_statusline({"session_id": "sl"})
line = buf.getvalue().strip()
check(line.startswith("[mc]") and len(line.splitlines()) == 1,
      "statusline вернул не одну строку: %r" % line)
check("infra" in line and "2м" in line, "statusline не показал проект/возраст чтения: %r" % line)

_real_log = pathlib.Path.home() / HOOKS_SUBDIR / "hooks" / "mc_hooks.log"
check(not _real_log.exists()
      or "q-test" not in _real_log.read_text(encoding="utf-8", errors="replace"),
      "ТЕСТЫ ЗАГРЯЗНЯЮТ боевой журнал статистики")
shutil.rmtree(tmp, ignore_errors=True)

# ------------------------------------------------------- живой досыл (--live)
if "--live" in sys.argv:
    tmp2 = pathlib.Path(tempfile.mkdtemp(prefix="mcl_"))
    mg.PENDING_DIR = tmp2 / "pending"
    mg.STATE_DIR = tmp2 / "state"
    mg.HOOK_LOG = tmp2 / "hooks.log"
    mg._tail_audit = lambda: []
    mg.FLUSH_MIN_AGE = 0
    ev = {"session_id": "live", "tool_use_id": "toolu_LIVE",
          "tool_name": "mcp__memory-compiler__save_lesson",
          "tool_input": {"topic": "Живой досыл очереди mc_guard (удалить)",
                         "content": "Проверка REST-доставки из локальной очереди.",
                         "project": "general", "tags": ["тест"]}}
    mg.cmd_intent(ev)
    sent, left, _d = mg._flush_queue(force=True)
    check(sent == 1 and left == 0, "живой досыл по REST не сработал: sent=%s left=%s" % (sent, left))
    shutil.rmtree(tmp2, ignore_errors=True)
    print("live: статья 'Живой досыл очереди mc_guard (удалить)' создана в general — удалить")

# ------------------------------------------- подсказка «ищи по сущности» (27.08)
# Замер по журналу: из 35 блокировок гейта осмысленную подсказку получили 14,
# мусорную — тоже 14, пустую — 7. Мусор выглядел как «$sp = "C:\Users\…\
# AppData\Local\Temp\..."»: _target_hint брал первые 80 символов команды,
# потому что у Bash/PowerShell в tool_input есть только поле command. Искать по
# такому нечего — гейт снимался формально, польза нулевая.

def hint(tool, ti):
    return mg._target_hint({"tool_name": tool, "tool_input": ti})

# сущности вытаскиваются из команды
check(hint("PowerShell", {"command": "Test-NetConnection 192.0.2.55 -Port 1433"}) == "192.0.2.55",
      "IP из команды не извлечён")
check("nas-ds723" in hint("Bash", {"command": "ssh nas-ds723 'docker ps'"}),
      "хост ssh не извлечён")
check("vps-demo.example.pro" in hint("Bash", {"command": "curl -s https://vps-demo.example.pro/api/health"}),
      "домен не извлечён")
check("memory-compiler-mcp" in hint("Bash", {"command": "sudo docker restart memory-compiler-mcp"}),
      "имя контейнера не извлечено")

# сырая команда в подсказку НЕ попадает
for junk in (r'$sp = "C:\Users\tester\AppData\Local\Temp\claude"',
             "$pw = ConvertTo-SecureString 'secret' -AsPlainText -Force",
             'python -c "import json; print(1)"'):
    h = hint("PowerShell", {"command": junk})
    check(h == "" or ("$" not in h and chr(92) not in h and "'" not in h),
          "в подсказку уехала сырая команда: %r" % h)

# у MCP-инструментов поведение прежнее
check(hint("mcp__1c__execute_query", {"project": "niks_ut"}) == "niks_ut",
      "поле project у MCP-инструмента потеряно")
check(hint("mcp__mikrotik__mikrotik_get_routes", {"host": "192.0.2.55"}) == "192.0.2.55",
      "поле host у MCP-инструмента потеряно")

# ---------------------------------------------- id чата для сервера (11.09)
# У Claude Desktop одна MCP-сессия на все чаты Code, получившие memory-compiler из
# claude_desktop_config.json (мост mcp-remote), и сервер склеивал их снимки
# свежести. Хук кладёт session_id в аргументы: они доезжают любым маршрутом.

got = []
mg.emit = lambda payload: got.append(payload)
EV_READ = {"session_id": "365ab5bc-c657-4ff0-9821-532597ce66b8",
           "tool_name": "mcp__memory-compiler__read_article",
           "tool_input": {"project": "infra", "filename": "x.md"}}
mg.cmd_session_arg(EV_READ)
hso = got[0]["hookSpecificOutput"] if got else {}
check(hso.get("hookEventName") == "PreToolUse", "session_arg: ответ не PreToolUse")
check("permissionDecision" not in hso,
      "session_arg: хук не решает за права — updatedInput claude.exe применяет и без allow (проверено 11.09)")
upd = hso.get("updatedInput") or {}
check(upd.get(mg.CLIENT_SESSION_ARG) == EV_READ["session_id"], "session_arg: id чата не добавлен")
check(upd.get("project") == "infra" and upd.get("filename") == "x.md",
      "session_arg: исходные аргументы потеряны")
check(EV_READ["tool_input"] == {"project": "infra", "filename": "x.md"},
      "session_arg: исходный tool_input испорчен на месте")

for ev, why in (
        (dict(EV_READ, session_id=""), "без session_id"),
        (dict(EV_READ, session_id="a b"), "session_id с пробелом"),
        (dict(EV_READ, tool_input="строка"), "tool_input не объект"),
        (dict(EV_READ, tool_input={"project": "infra",
                                   mg.CLIENT_SESSION_ARG: EV_READ["session_id"]}), "id уже стоит")):
    got.clear()
    mg.cmd_session_arg(ev)
    check(not got, "session_arg не должен ничего менять: " + why)

# Сервер v1.77.0 объявил поле в схеме, модель его видит и может заполнить сама —
# хук ставит поверх id этого чата.
got.clear()
mg.cmd_session_arg(dict(EV_READ, tool_input={"project": "infra", mg.CLIENT_SESSION_ARG: "made-up-by-model"}))
upd = (got[0]["hookSpecificOutput"] if got else {}).get("updatedInput") or {}
check(upd.get(mg.CLIENT_SESSION_ARG) == EV_READ["session_id"],
      "session_arg: значение, заполненное моделью, не перезаписано id чата")

got.clear()
mg.cmd_session_arg(dict(EV_READ, tool_input={"project": "infra", "note": "битое �"}))
check(not got, "session_arg переслал ввод с U+FFFD — это след порчи кодировки, такое не трогаем")

# ------------------------------------ session_arg через НАСТОЯЩИЙ stdin (11.09)
# Проверки выше зовут функцию напрямую и чтение stdin обходят. Живой прогон хука
# испортил кириллицу: клиент пишет в stdin UTF-8, а python на Windows читает
# его в кодировке консоли (cp1251), и updatedInput уезжал на сервер кракозябрами
# («Доезжает» → «Р”РѕРµР·Р¶Р°РµС‚»). Здесь хук запускается процессом, как его
# зовёт клиент, без PYTHONIOENCODING/PYTHONUTF8 в окружении.
EV_UTF = {"session_id": "0b7a1f3e-5c2d-4e8f-9a61-2d4c8e0f7b35",
          "tool_name": "mcp__memory-compiler__session_note",
          "tool_input": {"project": "infra", "note": "Доезжает ли кириллица — «ёлка» №1"}}
env = _hook_env(tmp)             # журнал хука пишется во временный HOME, не в боевой
proc = subprocess.run(
    [sys.executable, str(pathlib.Path(__file__).with_name("mc_guard.py")), "session_arg"],
    input=json.dumps(EV_UTF, ensure_ascii=False).encode("utf-8"),
    capture_output=True, timeout=60, env=env)
try:
    out_ev = json.loads(proc.stdout.decode("utf-8") or "{}")
except Exception:
    out_ev = {}
upd = (out_ev.get("hookSpecificOutput") or {}).get("updatedInput") or {}
check(upd.get("note") == EV_UTF["tool_input"]["note"],
      "session_arg (процесс): кириллица испорчена при чтении stdin: %r" % upd.get("note"))
check(upd.get(mg.CLIENT_SESSION_ARG) == EV_UTF["session_id"],
      "session_arg (процесс): id чата не добавлен")

# ------------------------------------------------------------ рефлексы (13.09)
# Памятка из базы по ошибке, файлу и цели. Сервер подменён: проверяется поведение
# хука — что спрашивает, когда молчит, куда кладёт ответ.
tmp3 = pathlib.Path(tempfile.mkdtemp(prefix="mcr_"))
mg.STATE_DIR = tmp3 / "state"
mg.HOOK_LOG = tmp3 / "hooks.log"
mg.PENDING_DIR = tmp3 / "pending"
calls = []
REPLY = {"memos": [{"project": "infra", "file": "sftp.md"}], "text": "Память (рефлекс по ошибке): …"}
mg._post_json = lambda path, payload, timeout=mg.REFLEX_TIMEOUT: (calls.append((path, payload)) or REPLY)
got = []
mg.emit = lambda payload: got.append(payload)

EV_FAIL = {"session_id": "rx-1", "hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
           "cwd": "C:\\DEV\\x", "tool_input": {"command": "sftp nas"},
           "error": "Exit code 1\nSFTP connection failed: Unable to start subsystem: sftp"}
mg.cmd_reflex(EV_FAIL)
check(calls and calls[0][0] == "/api/reflex" and calls[0][1]["kind"] == "error",
      "reflex: ошибка не ушла на сервер")
check(calls and calls[0][1]["text"] == EV_FAIL["error"] and calls[0][1]["cwd"] == "C:\\DEV\\x",
      "reflex: текст ошибки или cwd потеряны")
hso = got[0]["hookSpecificOutput"] if got else {}
check(hso.get("hookEventName") == "PostToolUseFailure" and "Память" in hso.get("additionalContext", ""),
      "reflex: памятка не отдана в additionalContext")

calls.clear(); got.clear()
mg.cmd_reflex(EV_FAIL)
check(not calls and not got, "reflex: та же ошибка запрошена повторно в той же сессии")

calls.clear(); got.clear()
mg.cmd_reflex(dict(EV_FAIL, error="Exit code 2\nдругая ошибка"))
check(calls and calls[0][1]["exclude"] == ["infra/sftp.md"],
      "reflex: уже показанная памятка не исключена из следующего запроса")

# Kimi Code шлёт error объектом {"code", "message"}, а не строкой — раньше гейт
# isinstance(err, str) молча пропускал такие ошибки (живой замер 28.09.2026).
calls.clear(); got.clear()
mg.cmd_reflex(dict(EV_FAIL, session_id="rx-16",
                   error={"code": "internal", "message": "Exit code 1\nSFTP connection failed"}))
check(calls and calls[0][1]["text"] == "Exit code 1\nSFTP connection failed",
      "reflex: error-объект Kimi (code/message) не разобран")
check(got and "Память" in got[0]["hookSpecificOutput"].get("additionalContext", ""),
      "reflex: по error-объекту Kimi памятка не отдана")
calls.clear(); got.clear()
mg.cmd_reflex(dict(EV_FAIL, session_id="rx-17", error={"code": "internal"}))
check(not calls and not got, "reflex: error-объект без message — лишний запрос")

for ev, why in ((dict(EV_FAIL, is_interrupt=True, session_id="rx-2"), "прерывание"),
                (dict(EV_FAIL, tool_name="mcp__memory-compiler__save_lesson", session_id="rx-3"), "свой инструмент"),
                (dict(EV_FAIL, hook_event_name="PostToolUse", session_id="rx-4"), "PostToolUse не на Read"),
                (dict(EV_FAIL, error="", session_id="rx-5"), "пустая ошибка")):
    calls.clear(); got.clear()
    mg.cmd_reflex(ev)
    check(not calls and not got, "reflex: лишний запрос — " + why)

calls.clear(); got.clear()
EV_READ_F = {"session_id": "rx-6", "hook_event_name": "PostToolUse", "tool_name": "Read",
             "tool_input": {"file_path": "C:\\DEV\\memory-compiler\\memory_compiler\\ui.py"}}
mg.cmd_reflex(EV_READ_F)
check(calls and calls[0][1]["kind"] == "file", "reflex: чтение файла не спросило сервер")
check(got and got[0]["hookSpecificOutput"]["hookEventName"] == "PostToolUse",
      "reflex: не тот hookEventName у файла")
calls.clear(); got.clear()
mg.cmd_reflex(dict(EV_READ_F, session_id="rx-7",
                   tool_input={"file_path": "C:\\Users\\a\\.claude\\projects\\x\\y.jsonl"}))
check(not calls, "reflex: транскрипт Claude Code не должен давать запрос")

calls.clear(); got.clear()
mg._post_json = lambda path, payload, timeout=mg.REFLEX_TIMEOUT: None
mg.cmd_reflex(dict(EV_FAIL, session_id="rx-8", error="Exit code 1\nновая ошибка"))
check(not got, "reflex: при недоступном сервере хук не должен ничего выводить")

# сбой включает общий предохранитель и НЕ сжигает ключ ошибки (ревью №3, №4)
check(mg._reflex_is_down(), "reflex: сбой запроса не включил предохранитель")
calls.clear(); got.clear()
mg._post_json = lambda path, payload, timeout=mg.REFLEX_TIMEOUT: (calls.append((path, payload)) or REPLY)
mg.cmd_reflex(dict(EV_FAIL, session_id="rx-8", error="Exit code 1\nещё одна ошибка"))
check(not calls, "reflex: при включённом предохранителе хук всё равно пошёл на сервер")
mg._reflex_down_clear()
calls.clear()
mg.cmd_reflex(dict(EV_FAIL, session_id="rx-8", error="Exit code 1\nновая ошибка"))
check(calls, "reflex: сбой сжёг ключ — та же ошибка после восстановления не запрошена")

# разные JSON-ошибки не склеиваются по последней строке «}» (ревью №2)
calls.clear(); got.clear()
J1 = 'Exit code 1\n{\n  "code": "E1",\n  "message": "first failure of the kind"\n}'
J2 = 'Exit code 1\n{\n  "code": "E2",\n  "message": "second failure of another kind"\n}'
mg.cmd_reflex(dict(EV_FAIL, session_id="rx-12", error=J1))
mg.cmd_reflex(dict(EV_FAIL, session_id="rx-12", error=J2))
check(len(calls) == 2, "reflex: две разные JSON-ошибки склеились в один ключ")

# рефлексы не пишут в файл состояния гейта, запись атомарная (ревью №1)
st_file = mg.STATE_DIR / "rx-13.json"
st_file.write_text(json.dumps({"last_read_ts": 123.0, "did_infra": True}), encoding="utf-8")
before_st = st_file.read_text(encoding="utf-8")
mg.cmd_reflex(dict(EV_FAIL, session_id="rx-13", error="Exit code 1\nошибка для отдельного файла"))
check(st_file.read_text(encoding="utf-8") == before_st, "reflex: состояние гейта перезаписано рефлексом")
check((mg.STATE_DIR / "rx-13.reflex.json").exists(), "reflex: нет отдельного файла состояния рефлексов")
mg.save_state({"session_id": "rx-14"}, {"a": 1})
check(json.loads((mg.STATE_DIR / "rx-14.json").read_text(encoding="utf-8")) == {"a": 1},
      "save_state: записанное не читается")
check(not list(mg.STATE_DIR.glob("*.tmp")), "save_state: остались временные файлы")

# длинная ошибка уходит головой и хвостом, как режет сервер
calls.clear()
big = "Exit code 1\n" + "x" * 20000 + "\nFinalError: the real reason at the end"
mg.cmd_reflex(dict(EV_FAIL, session_id="rx-15", error=big))
sent = calls[0][1]["text"] if calls else ""
check(len(sent) <= 4001 and sent.endswith("the real reason at the end"),
      "reflex: длинная ошибка не обрезана головой и хвостом")

mg._post_json = lambda path, payload, timeout=mg.REFLEX_TIMEOUT: (calls.append((path, payload, timeout)) or REPLY)
mg.STATE_DIR.mkdir(parents=True, exist_ok=True)
(mg.STATE_DIR / "rx-9.json").write_text(json.dumps({"last_read_ts": time.time()}), encoding="utf-8")
calls.clear(); got.clear()
mg.cmd_gate({"session_id": "rx-9", "tool_name": "Bash",
             "tool_input": {"command": "ssh admin@192.0.2.10 uptime"}})
check(calls and calls[0][1]["kind"] == "target" and "192.0.2.10" in calls[0][1]["text"],
      "gate: цель не спрошена у сервера")
hso = got[0]["hookSpecificOutput"] if got else {}
check("permissionDecision" not in hso and "Память" in hso.get("additionalContext", ""),
      "gate: при пропуске памятка не легла в additionalContext")
check(calls and calls[0][2] <= 0.7, "gate: запрос из гейта ждёт дольше 0.7 с")

# ─── карточка вместо блока (v1.79.0) ─────────────────────────────────────────
# Карточка по цели = знание об узле доставлено, блокировать незачем. Замер 13.09.2026:
# карточка готова для 70% целей (по IP 91%), а блок стоит лишний круг. Цель, о которой
# база молчит, — ровно пробел знаний, там блокировка остаётся.
calls.clear(); got.clear()
mg.cmd_gate({"session_id": "rx-card", "tool_name": "mcp__ssh__execute-command",
             "tool_input": {"connectionName": "nas-card", "cmdString": "uptime"}})
hso = got[0]["hookSpecificOutput"] if got else {}
check("permissionDecision" not in hso and "Память" in hso.get("additionalContext", ""),
      "gate: карточка есть, а вызов всё равно заблокирован")
st_card = json.loads((mg.STATE_DIR / "rx-card.json").read_text(encoding="utf-8"))
check(time.time() - float(st_card.get("last_read_ts") or 0) < 30,
      "gate: карточка не засчитана как чтение базы — следующий вызов снова упрётся в гейт")

# ⚠️ Дедуп памятки и основание для пропуска — РАЗНЫЕ вопросы. Памятка показывается раз
# на ключ за сессию, а «по цели есть знание» верно всё время. Живая проверка 13.09.2026:
# вторая команда к тому же узлу блокировалась, хотя карточка была показана минуту назад.
(mg.STATE_DIR / "rx-card.json").write_text(json.dumps({}), encoding="utf-8")
got.clear()
mg.cmd_gate({"session_id": "rx-card", "tool_name": "mcp__ssh__execute-command",
             "tool_input": {"connectionName": "nas-card", "cmdString": "uptime"}})
hso = got[0]["hookSpecificOutput"] if got else {}
check("permissionDecision" not in hso,
      "gate: повторный выход на тот же узел заблокирован — дедуп памятки съел пропуск")

# Карточки нет — блокировка остаётся прежней, с подсказкой в причине.
saved_reply = mg._post_json
mg._post_json = lambda path, payload, timeout=mg.REFLEX_TIMEOUT: (
    calls.append((path, payload, timeout)) or {"memos": [], "text": ""})
calls.clear(); got.clear()
mg.cmd_gate({"session_id": "rx-10", "tool_name": "Bash", "tool_input": {"command": "ssh nas-x uptime"}})
hso = got[0]["hookSpecificOutput"] if got else {}
check(hso.get("permissionDecision") == "deny" and "Ищи по сущности" in hso.get("permissionDecisionReason", ""),
      "gate: без карточки блокировка пропала — гейт перестал держать пробел знаний")
mg._post_json = saved_reply

got.clear()
mg.cmd_fail({"session_id": "rx-11", "tool_use_id": "toolu_RX",
             "tool_name": "mcp__memory-compiler__finish_task",
             "error": "MCP error -32001: Request timed out"})
ctx = got[0]["hookSpecificOutput"]["additionalContext"] if got else ""
check("-32001" in ctx and "ПОВТОРИ" in ctx.upper(),
      "fail: подсказка не сработала по полю error (у PostToolUseFailure нет tool_response)")

# ─── классы отказа записи (15.09.2026) ───────────────────────────────────────
# «could not be parsed» бывает обрывом генерации и битым синтаксисом — лечатся
# противоположно, различаем по raw. В -32602 есть слово «validation», но вызов до
# сервера дошёл: прежняя подсказка «до сервера не дошёл» там врала.
ERR_PARSE = ("InputValidationError: Tool input could not be parsed as JSON.\n"
             "You sent (first 200 of 90 bytes): {...}\n"
             "Common causes: unescaped backslashes in paths, raw control characters, truncated output.")
ERR_32602 = ('MCP error -32602: Input validation error: Invalid arguments for tool finish_task: [\n'
             '  {\n    "expected": "string",\n    "code": "invalid_type",\n    "path": [\n'
             '      "project"\n    ],\n    "message": "Invalid input: expected string, received undefined"\n  }\n]')


def fail_hint(tool_input, error):
    got.clear()
    mg.cmd_fail({"session_id": "fc-1", "tool_use_id": "toolu_FC",
                 "tool_name": "mcp__memory-compiler__finish_task",
                 "tool_input": tool_input, "error": error})
    return got[0]["hookSpecificOutput"]["additionalContext"] if got else ""


ctx = fail_hint({"__unparsedToolInput": {"raw": '{"topic": "Т", "project": "general", '
                                                '"content": "тело", "tags": тест, память}'}}, ERR_PARSE)
check("синтаксис" in ctx and "ДРОБИ" not in ctx.upper(),
      "fail: целый raw с битым синтаксисом лечат дроблением (класс 2)")
ctx = fail_hint({"__unparsedToolInput": {"raw": '{"topic": "Т", "project": "general", '
                                                '"content": "длинное тело оборвалось посреди сло'}}, ERR_PARSE)
check("ДРОБИ" in ctx.upper(), "fail: оборванный raw не подсказал дробить content (класс 1)")
ctx = fail_hint({"__unparsedToolInput": {"raw": '{"topic": "Т", "project": "general", '
                                                '"content": "код if (x) {y}'}}, ERR_PARSE)
check("ДРОБИ" in ctx.upper(), "fail: обрыв внутри строки, кончающийся на }, принят за битый синтаксис")
ctx = fail_hint({"__unparsedToolInput": {"raw": '{"topic": "Т", "project": "general", "content": "тело"}'}},
                ERR_32602)
check("__unparsedToolInput" in ctx and "прямыми" in ctx and "не дошёл" not in ctx,
      "fail: -32602 при обёртке raw не подсказал звать прямыми параметрами (класс 3)")
ctx = fail_hint({"topic": "Т", "content": "тело"}, ERR_32602)
check("project" in ctx and "не дошёл" not in ctx,
      "fail: -32602 без обёртки не назвал пропущенное поле")

# ─── цель из полей инфраинструментов (v1.79.0) ───────────────────────────────
# ssh-MCP отдаёт connectionName + cmdString, mikrotik — command/params. Пока смотрели
# только host/ip/command, цель не извлекалась в 48 из 140 блокировок (замер 13.09.2026):
# на самом частом инфраинструменте канал «цель» молчал.
check(mg._target_hint({"tool_input": {"connectionName": "nas-card",
                                      "cmdString": "docker ps"}}) == "nas-card",
      "target_hint: connectionName у ssh-MCP не прочитан")
check("192.0.2.1" in mg._target_hint({"tool_input": {"command": "/ip address print",
                                                     "params": "192.0.2.1"}}),
      "target_hint: params у mikrotik не прочитаны")
check(mg._target_hint({"tool_input": {"host": "192.0.2.10"}}) == "192.0.2.10",
      "target_hint: прежние поля перестали работать")
check(mg._target_hint({"tool_input": {"cmdString": "ls -la"}}) == "",
      "target_hint: выдумал цель там, где сущности нет")

# ─── вердикт живой проверки (v1.79.0) ────────────────────────────────────────
# Сравнение с ожидаемым делает КЛИЕНТ: вывод боевого узла — недоверенный ввод, на
# сервер уходит только вердикт (reachable | verified | stale).
sent = []
saved_post = mg._post_json
mg._post_json = lambda path, payload, timeout=None: (sent.append((path, payload)) or {})
EV_PROBE = {"session_id": "probe-1", "tool_name": "mcp__ssh__execute-command",
            "hook_event_name": "PostToolUse",
            "tool_input": {"connectionName": "192.0.2.10",
                           "cmdString": "/system identity print"},
            "tool_response": "name: KHV-GW"}
# ⚠️ Пара помнит СВОЮ статью: verified/stale относятся к конкретному факту, и без
# (project, file) сервер такой вердикт отбивает 400 (ревью ручки 13.09.2026).
mg._reflex_state_save(EV_PROBE, {"keys": [], "shown": [], "verify": {
    "192.0.2.10": [["testproj", "router.md", "/system identity print", "KHV-GW"]]}})
mg.cmd_probe(EV_PROBE)
check(sent and sent[-1][1].get("level") == "verified",
      "probe: совпадение с ожидаемым не дало verified")
check(sent and (sent[-1][1].get("project"), sent[-1][1].get("file")) == ("testproj", "router.md"),
      "probe: вердикт без ключа статьи — сервер отобьёт его как 400")
check(sent and sent[-1][1].get("command") == "/system identity print",
      "probe: команда исполненной цитаты не ушла в payload — вердикт по цитате не запишется (v1.82.0)")
check(sent and "KHV-GW" not in json.dumps(sent[-1][1], ensure_ascii=False),
      "probe: сырой вывод боевой команды уехал на сервер")

sent.clear()
mg.cmd_probe(dict(EV_PROBE, tool_response="name: OTHER-GW"))
check(sent and sent[-1][1].get("level") == "stale",
      "probe: несовпадение значения не дало stale")

sent.clear()
mg.cmd_probe(dict(EV_PROBE, tool_response="", error="connect ETIMEDOUT"))
check(sent == [], "probe: сетевой таймаут ошибочно засчитан как протухание факта")

# Отказ доступа — это ОТВЕТ узла, а не молчание: цитата не отработала, факт протух.
sent.clear()
mg.cmd_probe(dict(EV_PROBE, tool_response="Permission denied (publickey)"))
check(sent and sent[-1][1].get("level") == "stale", "probe: отказ доступа не дал stale")

sent.clear()
mg.cmd_probe(dict(EV_PROBE, tool_input={"command": "ls -la"}))
check(sent == [], "probe: без распознанной цели всё равно шлём вердикт")

# Локальная команда с адресом в аргументах — не выход на узел: право слать вердикт
# проверяем у себя, как в гейте, а не доверяем матчеру клиента.
sent.clear()
mg.cmd_probe({"session_id": "probe-1", "tool_name": "Bash", "hook_event_name": "PostToolUse",
              "tool_input": {"command": "ping -n 1 192.0.2.10"}, "tool_response": "ok"})
check(sent == [], "probe: локальная команда с адресом засчитана выходом на узел")

# Цитата соседнего узла не смеет решать судьбу этого факта.
sent.clear()
mg.cmd_probe(dict(EV_PROBE, tool_input={"connectionName": "192.0.2.99",
                                        "cmdString": "/system identity print"}))
check(sent and sent[-1][1].get("level") == "reachable" and "project" not in sent[-1][1],
      "probe: цитата чужой цели применена к этому узлу")
check(sent and "command" not in sent[-1][1],
      "probe: reachable шлёт command, хотя цитаты не было (v1.82.0)")

# Сквозной контур: карточка гейта кладёт цитату в state, исход команды её сверяет.
# ⚠️ Ключ хранения обязан совпасть с ключом чтения — разойдись они, verified не
# случился бы НИКОГДА, а проверки выше этого не заметили бы: там state кладут руками.
mg._post_json = lambda path, payload, timeout=None: (
    sent.append((path, payload)) or
    {"memos": [{"project": "testproj", "file": "router.md", "targets": ["192.0.2.10"],
                "verify": [["/system identity print", "KHV-GW"]]}],
     "text": "Память (карточка узла): …"})
EV_CARD = {"session_id": "probe-2", "tool_name": "mcp__ssh__execute-command",
           "tool_input": {"connectionName": "192.0.2.10", "cmdString": "/system identity print"}}
(mg.STATE_DIR / "probe-2.json").write_text(
    json.dumps({"last_read_ts": time.time()}), encoding="utf-8")
got.clear(); sent.clear()
mg.cmd_gate(EV_CARD)
sent.clear()
mg._post_json = lambda path, payload, timeout=None: (sent.append((path, payload)) or {})
mg.cmd_probe(dict(EV_CARD, hook_event_name="PostToolUse", tool_response="name: KHV-GW"))
check(sent and sent[-1][1].get("level") == "verified",
      "probe: цитата из карточки не доехала до сверки — ключ хранения разошёлся с чтением")
mg._post_json = saved_post

# ─── визиты и сверка команд RouterOS (v1.80.0) ───────────────────────────────
saved_post_p3 = mg._post_json
mg._post_json = lambda path, payload, timeout=None: (sent.append((path, payload)) or {})
PAIR_RB = [["testproj", "router.md", "/system identity print", "RB-DEMO"]]
mg._target_map_cache = {"mikrotik": "192.0.2.1"}

sent.clear()
EV_MT = {"session_id": "probe-3", "tool_name": "mcp__mikrotik__mikrotik_get_system_identity",
         "hook_event_name": "PostToolUse", "tool_input": {},
         "tool_response": '[{"name": "RB-DEMO"}]'}
mg._reflex_state_save(EV_MT, {"keys": [], "shown": [], "verify": {"192.0.2.1": PAIR_RB}})
mg.cmd_probe(EV_MT)
check(sent and sent[-1][1].get("level") == "verified",
      "probe: выделенный инструмент mikrotik не засчитан как /system identity print")
check("192.0.2.1" in (mg._reflex_state(EV_MT).get("visits") or {}),
      "визиты: выход на роутер не записан")

sent.clear()
EV_MT_SLASH = {"session_id": "probe-4", "tool_name": "mcp__mikrotik__mikrotik_execute_command",
               "hook_event_name": "PostToolUse",
               "tool_input": {"command": "/system/identity/print"},
               "tool_response": '[{"name": "RB-DEMO"}]'}
mg._reflex_state_save(EV_MT_SLASH, {"keys": [], "shown": [], "verify": {"192.0.2.1": PAIR_RB}})
mg.cmd_probe(EV_MT_SLASH)
check(sent and sent[-1][1].get("level") == "verified",
      "probe: написание команды RouterOS через слэши не совпало с цитатой через пробелы")

mg.cmd_probe({"session_id": "probe-5", "tool_name": "mcp__1c__execute_query",
              "hook_event_name": "PostToolUse", "tool_input": {"base": "demo_base"},
              "tool_response": "ok"})
check("demo_base" not in (mg._reflex_state({"session_id": "probe-5"}).get("visits") or {}),
      "визиты: у 1С нет строки команды — цитату сверять нечем, визит записывать незачем")
mg.cmd_probe({"session_id": "probe-7", "tool_name": "mcp__ssh__execute-command",
              "hook_event_name": "PostToolUse",
              "tool_input": {"connectionName": "192.0.2.77", "cmdString": "cat /etc/hostname"},
              "tool_response": "", "error": "connect ETIMEDOUT"})
check("192.0.2.77" not in (mg._reflex_state({"session_id": "probe-7"}).get("visits") or {}),
      "визиты: узел молчит (сетевой сбой) — визитом это не считается")
mg.cmd_probe({"session_id": "probe-8", "tool_name": "Bash", "hook_event_name": "PostToolUse",
              "tool_input": {"command": "ssh admin@192.0.2.10 cat /etc/hostname"},
              "tool_response": "NODE-DEMO"})
visits_bash = mg._reflex_state({"session_id": "probe-8"}).get("visits") or {}
check("192.0.2.10" in visits_bash and "admin" not in visits_bash,
      "визиты: из ssh в Bash записан не адрес узла, а %r" % list(visits_bash))
mg._target_map_cache = None
mg._post_json = saved_post_p3

# ─── после принятой цитаты карточка обновляется с сервера (ревью 14.09.2026) ───
# Прежний кэш брал пары из АРГУМЕНТОВ вызова: цитата, которую сервер отверг (секрет без
# content, команда вне белого списка, несуществующая статья), всё равно становилась штампом
# verified — в том числе на секрете. Теперь хук забывает карточку, и следующий выход на
# узел берёт у сервера только принятые цитаты.
mg._reflex_down_clear()
saved_post_b = mg._post_json
mg._post_json = lambda path, payload, timeout=None: (sent.append((path, payload)) or {})
EV_ACC = {"session_id": "vb-1", "tool_name": "mcp__memory-compiler__edit_article",
          "tool_input": {"project": "Testproj", "filename": "node.md",
                         "triggers": ["цель: 192.0.2.10"],
                         "verify": ["cat /etc/hostname => NODE-DEMO"]},
          "tool_response": "🧷 Статья: testproj/node.md\n🔎 Проверка: +1"}
mg._reflex_state_save(EV_ACC, {"keys": ["target:aaa", "error:bbb"],
                               "shown": ["testproj/node.md", "infra/other.md"]})
mg.cmd_mark(EV_ACC)
st_acc = mg._reflex_state(EV_ACC)
check(st_acc.get("keys") == ["error:bbb"] and st_acc.get("shown") == ["infra/other.md"],
      "обновление карточки: после принятой цитаты карточка узла не забыта: %r" % st_acc)
check(not st_acc.get("verify"),
      "обновление карточки: пары из аргументов вызова не должны попадать в сверку")

# У save_lesson имя файла придумывает сервер — статья берётся из его ответа.
EV_LESSON = {"session_id": "vb-1l", "tool_name": "mcp__memory-compiler__save_lesson",
             "tool_input": {"project": "testproj", "topic": "узел demo",
                            "verify": ["cat /etc/hostname => NODE-DEMO"]},
             "tool_response": "✅ Создано: testproj/узел_demo.md\n🔎 Проверка: +1"}
mg._reflex_state_save(EV_LESSON, {"keys": ["target:aaa"], "shown": ["testproj/узел_demo.md"]})
mg.cmd_mark(EV_LESSON)
st_l = mg._reflex_state(EV_LESSON)
check(st_l.get("keys") == [] and st_l.get("shown") == [],
      "обновление карточки: у save_lesson статья из ответа сервера не забыта: %r" % st_l)

for sid, resp, why in (
        ("vb-2", "🧷 Статья: testproj/secret_node-demo.md\n⚠️ Проверка не принята: в секретную "
                 "статью цитата идёт только вместе с content", "секрет без content"),
        ("vb-3", "🧷 Статья: testproj/node.md\n⚠️ Проверка «uptime => load» не принята: команда "
                 "проверки должна содержать читающий глагол", "команда вне белого списка"),
        ("vb-4", "Статья не найдена: testproj/nope.md", "несуществующая статья")):
    ev = dict(EV_ACC, session_id=sid, tool_response=resp)
    mg._reflex_state_save(ev, {"keys": ["target:aaa"], "shown": ["testproj/node.md"]})
    mg.cmd_mark(ev)
    st_rej = mg._reflex_state(ev)
    check(st_rej.get("keys") == ["target:aaa"] and not st_rej.get("verify"),
          "обновление карточки: отвергнутая цитата (%s) тронула состояние сверки: %r" % (why, st_rej))

# Сквозной контур: карточка без цитаты → запись → следующий выход на узел. Сервер отдаёт
# только принятую цитату и не повторяет уже показанную статью (exclude).
server_b = {"verify": []}


def _fake_server_b(path, payload, timeout=None):
    sent.append((path, payload))
    if path != "/api/reflex":
        return {}
    if "testproj/node.md" in (payload.get("exclude") or []):
        return {"memos": [], "text": ""}
    return {"memos": [{"project": "testproj", "file": "node.md", "targets": ["192.0.2.10"],
                       "verify": list(server_b["verify"])}],
            "text": "Память (карточка узла): …"}


mg._post_json = _fake_server_b
for sid, resp, accepted, want in (
        ("vb-e2e-ok", "🧷 Статья: testproj/node.md\n🔎 Проверка: +1", True, "verified"),
        ("vb-e2e-rej", "🧷 Статья: testproj/node.md\n⚠️ Проверка «cat /etc/hostname => NODE-DEMO» "
                       "не принята: креды в цитате", False, "reachable")):
    server_b["verify"] = []
    (mg.STATE_DIR / (sid + ".json")).write_text(
        json.dumps({"last_read_ts": time.time()}), encoding="utf-8")
    ev_node = {"session_id": sid, "tool_name": "mcp__ssh__execute-command",
               "tool_input": {"connectionName": "192.0.2.10", "cmdString": "cat /etc/hostname"}}
    mg.cmd_gate(ev_node)                               # карточка узла, цитаты в ней ещё нет
    mg.cmd_mark(dict(EV_ACC, session_id=sid, tool_response=resp))
    if accepted:
        server_b["verify"] = [["cat /etc/hostname", "NODE-DEMO"]]
    mg.cmd_gate(ev_node)
    sent.clear()
    mg.cmd_probe(dict(ev_node, hook_event_name="PostToolUse", tool_response="NODE-DEMO"))
    probes = [p for path, p in sent if path == "/api/probe"]
    check(probes and probes[-1].get("level") == want,
          "обновление карточки: %s дал %r, ожидалось %r"
          % (sid, probes[-1].get("level") if probes else None, want))

# ─── сверка команды: начало команды и целые слова (ревью 14.09.2026) ─────────
mg._post_json = lambda path, payload, timeout=None: (sent.append((path, payload)) or {})
PAIR_HOST = [["Infra", "node.md", "cat /etc/hostname", "NODE-DEMO"]]
for sid, tool, ti, out, want in (
        ("vm-1", "mcp__ssh__execute-command",
         {"connectionName": "192.0.2.10", "cmdString": "cat /etc/hostname.bak"}, "NODE-OLD", "reachable"),
        ("vm-2", "mcp__ssh__execute-command",
         {"connectionName": "192.0.2.10", "cmdString": "docker exec web cat /etc/hostname"}, "web-1",
         "reachable"),
        ("vm-3", "mcp__ssh__execute-command",
         {"connectionName": "192.0.2.10", "cmdString": "sudo -n cat /etc/hostname"}, "NODE-DEMO",
         "verified"),
        ("vm-4", "Bash", {"command": "ssh admin@192.0.2.10 cat /etc/hostname"}, "NODE-DEMO", "verified"),
        ("vm-5", "Bash", {"command": "ssh -p 2222 admin@192.0.2.10 'cat /etc/hostname'"}, "NODE-DEMO",
         "verified")):
    mg._reflex_state_save({"session_id": sid}, {"verify": {"192.0.2.10": PAIR_HOST}})
    sent.clear()
    mg.cmd_probe({"session_id": sid, "tool_name": tool, "hook_event_name": "PostToolUse",
                  "tool_input": ti, "tool_response": out})
    check(sent and sent[-1][1].get("level") == want,
          "сверка команды: %r дала %r, ожидалось %r"
          % (ti, sent[-1][1].get("level") if sent else None, want))
    if want == "verified" and sent:
        check(sent[-1][1].get("project") == "infra",
              "сверка команды: проект не приведён к нижнему регистру: %r" % sent[-1][1])

mg._target_map_cache = {"mikrotik": "192.0.2.1"}
mg._reflex_state_save({"session_id": "vm-6"},
                      {"verify": {"192.0.2.1": [["testproj", "router.md", "/export", "RB-DEMO"]]}})
sent.clear()
mg.cmd_probe({"session_id": "vm-6", "tool_name": "mcp__mikrotik__mikrotik_execute_command",
              "hook_event_name": "PostToolUse", "tool_input": {"command": "/interface/export"},
              "tool_response": "# interfaces"})
check(sent and sent[-1][1].get("level") == "reachable",
      "сверка команды: /export совпал с /interface export и дал ложный stale")
mg._reflex_state_save({"session_id": "vm-7"}, {"verify": {
    "192.0.2.1": [["testproj", "router.md", "/system resource print", "7.99.1"]]}})
sent.clear()
mg.cmd_probe({"session_id": "vm-7", "tool_name": "mcp__mikrotik__mikrotik_system_info",
              "hook_event_name": "PostToolUse", "tool_input": {},
              "tool_response": '[{"version": "7.99.1 (stable)"}]'})
check(sent and sent[-1][1].get("level") == "verified",
      "сверка команды: mikrotik_system_info не засчитан как /system resource print")
mg._target_map_cache = None
# RouterOS бывает и за ssh-MCP: слэши сводятся по виду команды, а не по инструменту.
mg._reflex_state_save({"session_id": "vm-8"}, {"verify": {"192.0.2.10": [
    ["testproj", "router.md", "/system identity print", "RB-DEMO"]]}})
sent.clear()
mg.cmd_probe({"session_id": "vm-8", "tool_name": "mcp__ssh__execute-command",
              "hook_event_name": "PostToolUse",
              "tool_input": {"connectionName": "192.0.2.10", "cmdString": "/system/identity/print"},
              "tool_response": "name: RB-DEMO"})
check(sent and sent[-1][1].get("level") == "verified",
      "сверка команды: RouterOS через ssh-MCP со слэшами не совпал с цитатой через пробелы")
mg.cmd_probe({"session_id": "vm-9", "tool_name": "mcp__synology__list_shares",
              "hook_event_name": "PostToolUse", "tool_input": {}, "tool_response": "ok"})
check(not (mg._reflex_state({"session_id": "vm-9"}).get("visits") or {}),
      "визиты: у synology нет строки команды — визит не нужен")

# Пары собираются по ВСЕМ целям: карточка по имени и по адресу кладёт их под разные ключи.
mg._target_map_cache = {"node-demo": "192.0.2.10"}
mg._reflex_state_save({"session_id": "vm-10"}, {"verify": {
    "node-demo": [["testproj", "a.md", "uname -n", "node"]],
    "192.0.2.10": [["testproj", "b.md", "cat /etc/hostname", "NODE-DEMO"]]}})
sent.clear()
mg.cmd_probe({"session_id": "vm-10", "tool_name": "mcp__ssh__execute-command",
              "hook_event_name": "PostToolUse",
              "tool_input": {"connectionName": "node-demo", "cmdString": "cat /etc/hostname"},
              "tool_response": "NODE-DEMO"})
check(sent and sent[-1][1].get("level") == "verified" and sent[-1][1].get("file") == "b.md",
      "сверка: пары под адресом не видны при выходе по имени: %r" % (sent[-1][1] if sent else None))
mg._target_map_cache = None

# Повторная карточка той же цели приходит без уже показанных статей (exclude): замена списка
# стирала бы их цитаты. Статья, пришедшая заново, заменяет СВОИ пары целиком.
EV_LK = {"session_id": "vm-11", "cwd": ""}
for memo in ({"project": "testproj", "file": "x.md", "targets": ["192.0.2.10"],
              "verify": [["cat /etc/hostname", "NODE-DEMO"]]},
             {"project": "testproj", "file": "y.md", "targets": ["192.0.2.10"],
              "verify": [["docker ps", "web-1"]]},
             {"project": "testproj", "file": "x.md", "targets": ["192.0.2.10"],
              "verify": [["cat /etc/os-release", "VERSION_ID"]]}):
    mg._post_json = lambda path, payload, timeout=None, memo=memo: {"memos": [memo], "text": "Память …"}
    st_lk = mg._reflex_state(EV_LK)
    st_lk["keys"] = []
    mg._reflex_state_save(EV_LK, st_lk)
    mg._reflex_lookup(EV_LK, "target", ["192.0.2.10"])
stored = sorted((p[1], p[2]) for p in
                ((mg._reflex_state(EV_LK).get("verify") or {}).get("192.0.2.10") or []))
check(stored == [("x.md", "cat /etc/os-release"), ("y.md", "docker ps")],
      "карточки: повторная карточка стёрла чужие цитаты или оставила устаревшие: %r" % stored)

# Вердикт, который не дошёл до сервера, виден в журнале.
log_mark = mg.HOOK_LOG.stat().st_size if mg.HOOK_LOG.exists() else 0
mg._post_json = lambda path, payload, timeout=None: None
mg.cmd_probe({"session_id": "vm-12", "tool_name": "mcp__ssh__execute-command",
              "hook_event_name": "PostToolUse",
              "tool_input": {"connectionName": "192.0.2.10", "cmdString": "uptime"},
              "tool_response": "up"})
log_tail = ""
if mg.HOOK_LOG.exists():
    with mg.HOOK_LOG.open("rb") as f:
        f.seek(log_mark)
        log_tail = f.read().decode("utf-8", errors="replace")
log_recs = []
for line in log_tail.splitlines():
    try:
        log_recs.append(json.loads(line))
    except ValueError:
        pass
check(any(r.get("action") == "probe.error" and r.get("session") == "vm-12" for r in log_recs),
      "probe: вердикт, который не дошёл до сервера, не отмечен в журнале")
mg._reflex_down_clear()

# ─── устойчивость к битому состоянию (ревью 14.09.2026) ──────────────────────
# Исключение в обновлении карточки роняло cmd_mark раньше снятия записи из очереди: Stop
# потом требовал повторить уже прошедший вызов.
mg._post_json = lambda path, payload, timeout=None: (sent.append((path, payload)) or {})
EV_BAD = {"session_id": "vb-bad", "tool_use_id": "toolu_BADSTATE",
          "tool_name": "mcp__memory-compiler__edit_article",
          "tool_input": {"project": "testproj", "filename": "node.md",
                         "verify": ["cat /etc/hostname => X"]},
          "tool_response": "🧷 Статья: testproj/node.md\n🔎 Проверка: +1"}
for bad_state in ({"keys": "не список", "shown": 5, "visits": ["x"], "verify": ["y"]},
                  ["весь файл списком"]):
    mg.cmd_intent(EV_BAD)
    mg._reflex_state_path(EV_BAD).write_text(json.dumps(bad_state, ensure_ascii=False), encoding="utf-8")
    try:
        mg.cmd_mark(EV_BAD)
        crashed = ""
    except Exception as e:
        crashed = repr(e)
    check(not crashed, "устойчивость: битое состояние рефлексов роняет cmd_mark: %s" % crashed)
    check(not mg._pending_path(EV_BAD).exists(),
          "устойчивость: прошедший вызов остался в очереди незаписанного")
mg._reflex_state_path({"session_id": "vb-bad2"}).write_text(json.dumps(
    {"visits": {"192.0.2.10": {"ts": "вчера"}}, "verify": {"192.0.2.10": "не список"}},
    ensure_ascii=False), encoding="utf-8")
try:
    mg.cmd_probe({"session_id": "vb-bad2", "tool_name": "mcp__ssh__execute-command",
                  "hook_event_name": "PostToolUse",
                  "tool_input": {"connectionName": "192.0.2.10", "cmdString": "uptime"},
                  "tool_response": "up"})
    crashed = ""
except Exception as e:
    crashed = repr(e)
check(not crashed, "устойчивость: битое состояние роняет cmd_probe: %s" % crashed)
(mg.STATE_DIR / "vb-bad3.json").write_text(json.dumps({"last_read_ts": time.time()}), encoding="utf-8")
mg._reflex_state_path({"session_id": "vb-bad3"}).write_text(
    json.dumps({"keys": 7, "shown": "x", "targets": 5}), encoding="utf-8")
try:
    mg.cmd_gate({"session_id": "vb-bad3", "tool_name": "mcp__ssh__execute-command",
                 "tool_input": {"connectionName": "192.0.2.10", "cmdString": "uptime"}})
    crashed = ""
except Exception as e:
    crashed = repr(e)
check(not crashed, "устойчивость: битое состояние роняет гейт: %s" % crashed)
mg._post_json = saved_post_b
mg._reflex_down_clear()

# ─── ревью 14.09.2026 (3): вердикт по адресату, полное равенство, раскладка по цели ───
mg._reflex_down_clear()
saved_post_d = mg._post_json

# N1: карточка с двумя статьями РАЗНЫХ узлов раскладывается по слотам цели-источника
# (memo["targets"] от сервера), не размазывается — цитата статьи A не ложится под узел B.
EV_D = {"session_id": "d-1", "cwd": ""}
mg._post_json = lambda path, payload, timeout=None: (sent.append((path, payload)) or {
    "memos": [
        {"project": "infra", "file": "nas.md", "targets": ["nas-demo", "192.0.2.20"],
         "verify": [["cat /etc/hostname", "NAS-DEMO"]]},
        {"project": "infra", "file": "vps.md", "targets": ["192.0.2.50"],
         "verify": [["cat /etc/hostname", "VPS-DEMO"]]}],
    "text": "Память (карточка узла): …"})
mg._reflex_lookup(EV_D, "target", ["nas-demo", "192.0.2.20", "192.0.2.50"])
store_d = mg._reflex_state(EV_D).get("verify") or {}
check(store_d.get("nas-demo") == [["infra", "nas.md", "cat /etc/hostname", "NAS-DEMO"]]
      and store_d.get("192.0.2.20") == [["infra", "nas.md", "cat /etc/hostname", "NAS-DEMO"]]
      and store_d.get("192.0.2.50") == [["infra", "vps.md", "cat /etc/hostname", "VPS-DEMO"]],
      "N1: пары не разложены по цели-источнику: %r" % store_d)

# Выход на nas → verified nas.md; статья vps (чужой узел) не участвует, ложного stale нет.
mg._post_json = lambda path, payload, timeout=None: (sent.append((path, payload)) or {})
mg._target_map_cache = {"nas-demo": "192.0.2.20"}
sent.clear()
mg.cmd_probe({"session_id": "d-1", "tool_name": "mcp__ssh__execute-command",
              "hook_event_name": "PostToolUse",
              "tool_input": {"connectionName": "nas-demo", "cmdString": "cat /etc/hostname"},
              "tool_response": "NAS-DEMO"})
pr = [p for path, p in sent if path == "/api/probe"]
check(pr and pr[-1].get("level") == "verified" and pr[-1].get("file") == "nas.md",
      "N1: verified не по статье адресата: %r" % (pr[-1] if pr else None))

# N1 вердикт по адресату: посторонний адрес из ТЕЛА команды не уходит в target вердикта
# и его статья не решает судьбу факта адресата.
mg._reflex_state_save({"session_id": "d-2"}, {"verify": {
    "192.0.2.20": [["infra", "nas.md", "cat /etc/hostname", "NAS-DEMO"]],
    "192.0.2.50": [["infra", "vps.md", "cat /etc/hostname", "VPS-DEMO"]]}})
sent.clear()
mg.cmd_probe({"session_id": "d-2", "tool_name": "Bash", "hook_event_name": "PostToolUse",
              "tool_input": {"command": 'ssh nas-demo "curl -s http://192.0.2.50/h; cat /etc/hostname"'},
              "tool_response": "NAS-DEMO"})
pr = [p for path, p in sent if path == "/api/probe"]
tg = pr[-1].get("target") if pr else []
check(pr and "192.0.2.50" not in tg and "192.0.2.20" in tg,
      "N1: посторонний адрес из тела попал в target вердикта: %r" % tg)
check(pr and pr[-1].get("file") != "vps.md",
      "N1: вердикт по чужой статье из тела команды: %r" % (pr[-1] if pr else None))
mg._target_map_cache = None

# N2: полное равенство слов — любой хвост после цитаты меняет вывод → reachable.
PAIR_N2 = [["infra", "n.md", "docker ps", "web-1"]]
for sid, cmd, out, want in (
        ("n2-1", "docker ps", "web-1", "verified"),
        ("n2-2", "docker ps -a", "web-1", "reachable"),
        ("n2-3", "docker ps | grep nginx", "web-1", "reachable"),
        ("n2-4", "docker ps > /tmp/x", "", "reachable"),
        ("n2-4b", "sudo -n docker ps", "web-1", "verified")):
    mg._reflex_state_save({"session_id": sid}, {"verify": {"192.0.2.60": PAIR_N2}})
    sent.clear()
    mg.cmd_probe({"session_id": sid, "tool_name": "mcp__ssh__execute-command",
                  "hook_event_name": "PostToolUse",
                  "tool_input": {"connectionName": "192.0.2.60", "cmdString": cmd},
                  "tool_response": out})
    check(sent and sent[-1][1].get("level") == want,
          "N2: %r дало %r, ожидалось %r" % (cmd, sent[-1][1].get("level") if sent else None, want))

# N2 params: непустой params у mikrotik меняет вывод (фильтр where/=) → reachable; пустой — сверяем.
mg._target_map_cache = {"mikrotik": "192.0.2.1"}
mg._reflex_state_save({"session_id": "n2-5"}, {"verify": {
    "192.0.2.1": [["infra", "rb.md", "/ip address print", "192.0.2.1/24"]]}})
sent.clear()
mg.cmd_probe({"session_id": "n2-5", "tool_name": "mcp__mikrotik__mikrotik_execute_command",
              "hook_event_name": "PostToolUse",
              "tool_input": {"command": "/ip/address/print", "params": {"where": "disabled=yes"}},
              "tool_response": "[]"})
check(sent and sent[-1][1].get("level") == "reachable",
      "N2: непустой params mikrotik не обнулил сверку: %r" % (sent[-1][1] if sent else None))
mg._reflex_state_save({"session_id": "n2-6"}, {"verify": {
    "192.0.2.1": [["infra", "rb.md", "/ip address print", "192.0.2.1/24"]]}})
sent.clear()
mg.cmd_probe({"session_id": "n2-6", "tool_name": "mcp__mikrotik__mikrotik_execute_command",
              "hook_event_name": "PostToolUse",
              "tool_input": {"command": "/ip/address/print", "params": {}},
              "tool_response": "192.0.2.1/24 on ether1"})
check(sent and sent[-1][1].get("level") == "verified",
      "N2: пустой params должен сверяться нормально: %r" % (sent[-1][1] if sent else None))
# Статья без targets (сервер старше v1.80.0) в сверку не идёт вовсе: verified по ней не
# случится, но и ложного вердикта не будет. Сторож на ОСОЗНАННЫЙ fallback, а не на баг.
EV_OLD_SRV = {"session_id": "d-3", "cwd": ""}
mg._post_json = lambda path, payload, timeout=None: {
    "memos": [{"project": "infra", "file": "old.md", "verify": [["docker ps", "web-1"]]}],
    "text": "Память (карточка узла): …"}
mg._reflex_lookup(EV_OLD_SRV, "target", ["192.0.2.70"])
check(not (mg._reflex_state(EV_OLD_SRV).get("verify") or {}),
      "fallback: статья без targets не должна попадать в сверку: %r"
      % (mg._reflex_state(EV_OLD_SRV).get("verify") or {}))
mg._post_json = lambda path, payload, timeout=None: (sent.append((path, payload)) or {})
sent.clear()
mg.cmd_probe({"session_id": "d-3", "tool_name": "mcp__ssh__execute-command",
              "hook_event_name": "PostToolUse",
              "tool_input": {"connectionName": "192.0.2.70", "cmdString": "docker ps"},
              "tool_response": "web-1"})
check(sent and sent[-1][1].get("level") == "reachable" and "file" not in sent[-1][1],
      "fallback: без targets вердикт обязан остаться reachable: %r" % (sent[-1][1] if sent else None))

mg._target_map_cache = None
mg._post_json = saved_post_d
mg._reflex_down_clear()

# ─── ревью 14.09.2026 (4): вердикт без адресата, регистр проекта, чужой слот ───
saved_post_e = mg._post_json
mg._post_json = lambda path, payload, timeout=None: (sent.append((path, payload)) or {})

# I1: адресат не установлен (у mikrotik нет ключа в карте целей — конфиг Desktop не прочитан
# или переименован). Цели тогда берутся из ТЕЛА команды, и вердикт по ним выносить нельзя:
# команда исполнялась на роутере, а штамп лёг бы на статью про адрес из аргумента.
mg._target_map_cache = {}
mg._reflex_state_save({"session_id": "e-1"}, {"verify": {
    "192.0.2.50": [["infra", "vps.md", "/ping 192.0.2.50 count=3", "sent=3"]]}})
sent.clear()
mg.cmd_probe({"session_id": "e-1", "tool_name": "mcp__mikrotik__mikrotik_execute_command",
              "hook_event_name": "PostToolUse",
              "tool_input": {"command": "/ping 192.0.2.50 count=3"},
              "tool_response": "sent=3 received=3"})
pe = [p for path, p in sent if path == "/api/probe"]
check(pe and pe[-1].get("level") == "reachable" and "file" not in pe[-1],
      "I1: без адресата вынесен вердикт по цели из тела команды: %r" % (pe[-1] if pe else None))
mg._target_map_cache = None

# M1: сервер присылает проект как есть, а путь статьи хук приводит к нижнему регистру —
# сравнение в shown обязано быть регистронезависимым, иначе карточка не забудется.
EV_CASE = {"session_id": "e-2", "tool_name": "mcp__memory-compiler__edit_article",
           "tool_input": {"project": "Infra", "filename": "node.md",
                          "verify": ["cat /etc/hostname => NODE-DEMO"]},
           "tool_response": "🧷 Статья: Infra/node.md\n🔎 Проверка: +1"}
mg._reflex_state_save(EV_CASE, {"keys": ["target:aaa"], "shown": ["Infra/node.md"]})
mg.cmd_mark(EV_CASE)
st_case = mg._reflex_state(EV_CASE)
check(st_case.get("shown") == [],
      "M1: статья с заглавным проектом не забыта из shown: %r" % st_case.get("shown"))

# M3: сервер прислал цель, которой в запросе не было. Слот по ней заводить нельзя — иначе
# расхождение нормализаций на сервере молча похоронит сверку; промах виден в журнале.
EV_SLOT = {"session_id": "e-3", "cwd": ""}
mg._post_json = lambda path, payload, timeout=None: {
    "memos": [{"project": "infra", "file": "alien.md", "targets": ["чужая-цель-99"],
               "verify": [["docker ps", "web-1"]]}],
    "text": "Память (карточка узла): …"}
log_mark_e = mg.HOOK_LOG.stat().st_size if mg.HOOK_LOG.exists() else 0
mg._reflex_lookup(EV_SLOT, "target", ["192.0.2.80"])
check(not (mg._reflex_state(EV_SLOT).get("verify") or {}),
      "M3: заведён слот по цели, которой в запросе не было: %r"
      % (mg._reflex_state(EV_SLOT).get("verify") or {}))
tail_e = ""
if mg.HOOK_LOG.exists():
    with mg.HOOK_LOG.open("rb") as f:
        f.seek(log_mark_e)
        tail_e = f.read().decode("utf-8", errors="replace")
check("slot_miss" in tail_e, "M3: промах слота не отмечен в журнале")

mg._post_json = saved_post_e
mg._reflex_down_clear()

# ─── подсказка дописать цитату (v1.80.0) ─────────────────────────────────────
got.clear()
VISIT_NOW = {"node-demo": {"ts": time.time(), "kind": "ssh"}}
EV_SAVE_NODE = {"session_id": "nudge-1", "tool_name": "mcp__memory-compiler__save_lesson",
                "tool_input": {"topic": "Узел node-demo переехал", "project": "testproj",
                               "content": "Хост node-demo отвечает как NODE-DEMO."},
                "tool_response": "✅ Создано: testproj/узел_node-demo.md"}
mg._reflex_state_save(EV_SAVE_NODE, {"keys": [], "shown": [], "visits": dict(VISIT_NOW)})
mg.cmd_nudge(EV_SAVE_NODE)
ctx = (got[0]["hookSpecificOutput"]["additionalContext"] if got else "")
check("verify" in ctx and "node-demo" in ctx and "edit_article" in ctx,
      "подсказка: не пришла или без цели и вызова: %r" % ctx[:120])
check("testproj/узел_node-demo.md" in ctx,
      "подсказка: путь сохранённой статьи не подставлен")

got.clear()
mg.cmd_nudge(EV_SAVE_NODE)
check(not got, "подсказка: повтор по тому же узлу в одной сессии")

for ev, why in (
        (dict(EV_SAVE_NODE, session_id="nudge-2",
              tool_input=dict(EV_SAVE_NODE["tool_input"], verify=["cat /etc/hostname => X"])),
         "в вызове уже есть verify"),
        (dict(EV_SAVE_NODE, session_id="nudge-3",
              tool_name="mcp__memory-compiler__save_secret"), "секрет"),
        (dict(EV_SAVE_NODE, session_id="nudge-4",
              tool_response="✅ Создано: testproj/secret_node.md"), "секретный файл"),
        (dict(EV_SAVE_NODE, session_id="nudge-5",
              tool_input={"topic": "Про другое", "project": "testproj",
                          "content": "Ничего про узлы."}), "запись не про узел"),
):
    got.clear()
    mg._reflex_state_save(ev, {"keys": [], "shown": [], "visits": dict(VISIT_NOW)})
    mg.cmd_nudge(ev)
    check(not got, "подсказка: лишнее срабатывание — " + why)

got.clear()
EV_OLD = dict(EV_SAVE_NODE, session_id="nudge-6")
mg._reflex_state_save(EV_OLD, {"keys": [], "shown": [],
                               "visits": {"node-demo": {"ts": time.time() - 9000, "kind": "ssh"}}})
mg.cmd_nudge(EV_OLD)
check(not got, "подсказка: визит старше окна в 2 часа")

got.clear()
EV_CAP = {"session_id": "nudge-7", "tool_name": "mcp__memory-compiler__edit_article",
          "tool_input": {"project": "testproj", "filename": "sosedi.md",
                         "content": "Узлы node-a, node-b и node-c в одной сети."},
          "tool_response": "✏️ Дописано: testproj/sosedi.md"}
mg._reflex_state_save(EV_CAP, {"visits": {n: {"ts": time.time(), "kind": "ssh"}
                                          for n in ("node-a", "node-b", "node-c")}})
mg.cmd_nudge(EV_CAP)
ctx_cap = got[0]["hookSpecificOutput"]["additionalContext"] if got else ""
check(sum(n in ctx_cap for n in ("node-a", "node-b", "node-c")) == 2,
      "подсказка: потолок в два узла не соблюдён: %r" % ctx_cap[:120])
check(mg._token_in("192.0.2.1", "роутер 192.0.2.1.")
      and not mg._token_in("node-demo", "node-demo.local")
      and not mg._token_in("192.0.2.1", "узел 192.0.2.10"),
      "подсказка: граница токена — точка в конце предложения или соседнее имя")
check("nudge" in mg.COMMANDS, "подсказка: команда не зарегистрирована в COMMANDS")

# ─── ревью хука 14.09.2026: подсказка и запись состояния ─────────────────────
# M2: подсказка приходила на неуспешную запись и брала путь из первого попавшегося `x/y.md`.
got.clear()
EV_FAILED = {"session_id": "nudge-8", "tool_name": "mcp__memory-compiler__edit_article",
             "tool_input": {"project": "testproj", "filename": "nope.md", "content": "Узел node-demo."},
             "tool_response": "Статья не найдена: testproj/nope.md"}
mg._reflex_state_save(EV_FAILED, {"visits": dict(VISIT_NOW)})
mg.cmd_nudge(EV_FAILED)
check(not got, "подсказка: запись не удалась, а подсказка пришла")

got.clear()
EV_PATH = dict(EV_SAVE_NODE, session_id="nudge-12",
               tool_response="Похожие: infra/old.md\n✅ Создано: testproj/узел_node-demo.md")
mg._reflex_state_save(EV_PATH, {"visits": dict(VISIT_NOW)})
mg.cmd_nudge(EV_PATH)
ctx_path = got[0]["hookSpecificOutput"]["additionalContext"] if got else ""
check("testproj/узел_node-demo.md" in ctx_path and "old.md" not in ctx_path,
      "подсказка: путь взят не из строки маркера записи: %r" % ctx_path[:160])
check("а цитаты у статьи" not in ctx_path,
      "подсказка: хук утверждает, что цитаты у статьи нет, хотя этого не знает")

# Визит лежит под адресом, а в записи модель пишет имя соединения: узел ищется по всем именам,
# и второй раз его не просят ни по имени, ни по адресу.
got.clear()
EV_ALIAS = {"session_id": "nudge-9", "tool_name": "mcp__memory-compiler__save_lesson",
            "tool_input": {"topic": "nas-demo: обновлён docker", "project": "testproj",
                           "content": "Проверено."},
            "tool_response": "✅ Создано: testproj/nas.md"}
mg._reflex_state_save(EV_ALIAS, {"visits": {"192.0.2.20": {
    "ts": time.time(), "kind": "ssh", "names": ["192.0.2.20", "nas-demo"]}}})
mg.cmd_nudge(EV_ALIAS)
ctx_alias = got[0]["hookSpecificOutput"]["additionalContext"] if got else ""
check("nas-demo" in ctx_alias and "цель: 192.0.2.20" in ctx_alias,
      "подсказка: узел по имени соединения не узнан или адрес не подставлен в триггер: %r"
      % ctx_alias[:200])
got.clear()
mg.cmd_nudge(dict(EV_ALIAS, tool_input=dict(EV_ALIAS["tool_input"], topic="192.0.2.20: диск")))
check(not got, "подсказка: тот же узел по адресу попросили второй раз")

# M3: пока подсказка решала, probe той же сессии записал визит — запись nudged его не затирает.
got.clear()
EV_RACE = dict(EV_SAVE_NODE, session_id="nudge-10")
mg._reflex_state_save(EV_RACE, {"visits": dict(VISIT_NOW)})
saved_token_in = mg._token_in
race_mark = {"done": False}


def _token_in_race(needle, hay):
    if not race_mark["done"]:
        race_mark["done"] = True
        st_r = mg._reflex_state(EV_RACE)
        st_r.setdefault("visits", {})["192.0.2.99"] = {"ts": time.time(), "kind": "ssh"}
        mg._reflex_state_save(EV_RACE, st_r)
    return saved_token_in(needle, hay)


mg._token_in = _token_in_race
mg.cmd_nudge(EV_RACE)
mg._token_in = saved_token_in
check(race_mark["done"], "подсказка (тест гонки): подмена _token_in не сработала")
check("192.0.2.99" in (mg._reflex_state(EV_RACE).get("visits") or {}),
      "подсказка: запись nudged затёрла визит, записанный параллельным probe")

# M4: 200 визитов на записи в 1.6 МБ стоили 7.1 с — выше таймаута хука.
got.clear()
EV_BIG = {"session_id": "nudge-11", "tool_name": "mcp__memory-compiler__finish_task",
          "tool_input": {"topic": "итог", "project": "testproj", "content": "слово " * 280000},
          "tool_response": "✅ Создано: testproj/itog.md"}
mg._reflex_state_save(EV_BIG, {"visits": {
    "192.0.2.%d" % i: {"ts": time.time(), "kind": "ssh", "names": ["192.0.2.%d" % i, "node-%03d" % i]}
    for i in range(1, 201)}})
t_big = time.time()
mg.cmd_nudge(EV_BIG)
dt_big = time.time() - t_big
check(dt_big < 3.0, "подсказка: 200 визитов на записи в 1.6 МБ — %.2f с "
      "(порог мягкий: важно, что это не 7 с, как до предпроверки)" % dt_big)

# Битое состояние и кривой вход не роняют подсказку.
for sid, st_bad, ti_bad in (
        ("nudge-bad1", {"visits": ["x"]}, None),
        ("nudge-bad2", {"visits": {"node-demo": "вчера"}}, None),
        ("nudge-bad3", {"visits": {"node-demo": {"ts": "вчера", "names": "не список"}}}, None),
        ("nudge-bad4", {"visits": dict(VISIT_NOW), "nudged": 5}, None),
        ("nudge-bad5", {"visits": dict(VISIT_NOW)}, "не объект")):
    got.clear()
    ev_bad = dict(EV_SAVE_NODE, session_id=sid)
    if ti_bad is not None:
        ev_bad["tool_input"] = ti_bad
    mg._reflex_state_path(ev_bad).write_text(json.dumps(st_bad, ensure_ascii=False), encoding="utf-8")
    try:
        mg.cmd_nudge(ev_bad)
        crashed = ""
    except Exception as e:
        crashed = repr(e)
    check(not crashed, "подсказка: битое состояние или вход роняют cmd_nudge (%s): %s" % (sid, crashed))

# Основное состояние гейта тоже бывает битым: снятие из очереди не должно падать.
EV_BADMAIN = {"session_id": "mark-bad", "tool_use_id": "toolu_BADMAIN",
              "tool_name": "mcp__memory-compiler__search", "tool_input": {"query": "x"}}
mg.cmd_intent(EV_BADMAIN)
mg.state_path(EV_BADMAIN).write_text(json.dumps(["не объект"]), encoding="utf-8")
try:
    mg.cmd_mark(EV_BADMAIN)
    crashed = ""
except Exception as e:
    crashed = repr(e)
check(not crashed and not mg._pending_path(EV_BADMAIN).exists(),
      "устойчивость: битое основное состояние роняет cmd_mark: %s" % crashed)

# ─── замок записи состояния рефлексов (ревью 14.09.2026, M3) ─────────────────
EV_LOCK = {"session_id": "lock-1"}
lock_path = mg._reflex_state_path(EV_LOCK).with_name(mg._reflex_state_path(EV_LOCK).name + ".lock")
saved_wait = getattr(mg, "REFLEX_LOCK_WAIT", None)
mg.REFLEX_LOCK_WAIT = 0.05
mg._reflex_state_save(EV_LOCK, {"visits": {"192.0.2.10": {"ts": 1}}})
lock_path.write_text("", encoding="utf-8")                       # замок держит сосед
res_lock = mg._reflex_state_update(EV_LOCK, lambda st: st.update(visits={}))
check(res_lock is None and (mg._reflex_state(EV_LOCK).get("visits") or {}),
      "замок: запись прошла мимо занятого замка")
stale_ts = time.time() - getattr(mg, "REFLEX_LOCK_STALE", 5.0) - 5
os.utime(lock_path, (stale_ts, stale_ts))                        # сосед убит посреди записи
res_lock = mg._reflex_state_update(EV_LOCK, lambda st: st.update(nudged=["192.0.2.10"]))
check(res_lock is not None and mg._reflex_state(EV_LOCK).get("nudged") == ["192.0.2.10"],
      "замок: брошенный замок не снят, запись не прошла")
check(not lock_path.exists(), "замок: после записи файл замка остался")

# Упавшее чтение под замком не превращается в пустой снимок: запись пропускается.
mg._reflex_state_save(EV_LOCK, {"visits": {"192.0.2.10": {"ts": 1}}, "nudged": ["x"]})
orig_read_text = pathlib.Path.read_text


def _deny_read(self, *a, **k):
    if self.name.endswith(".reflex.json"):
        raise PermissionError("файл заменяет сосед")
    return orig_read_text(self, *a, **k)


pathlib.Path.read_text = _deny_read
try:
    res_lock = mg._reflex_state_update(EV_LOCK, lambda st: st.update(nudged=[]))
finally:
    pathlib.Path.read_text = orig_read_text
check(res_lock is None and mg._reflex_state(EV_LOCK).get("nudged") == ["x"],
      "замок: упавшее чтение затёрло состояние пустым снимком")
if saved_wait is not None:
    mg.REFLEX_LOCK_WAIT = saved_wait

# Сквозной замер: процессы одной сессии по общему барьеру старта пишут визиты и цитаты
# карточки. Без замка терялось 343 из 400 визитов и 266 из 400 цитат (замер 14.09.2026).
RACE_WORKER = """
import importlib.util, pathlib, sys, time
spec = importlib.util.spec_from_file_location("mc_guard_race", sys.argv[1])
mg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mg)
state = pathlib.Path(sys.argv[2])
mg.STATE_DIR = state
mg.HOOK_LOG = state / "race.log"
mg.PENDING_DIR = state / "pending"
mg._target_map_cache = {}
role, start, addrs = sys.argv[3], float(sys.argv[4]), sys.argv[5].split(",")


def fake(path, payload, timeout=None):
    if path == "/api/reflex":
        t = payload["text"][0]
        return {"memos": [{"project": "p", "file": t + ".md", "targets": [t],
                           "verify": [["uname -n", t]]}],
                "text": "card"}
    return {}


mg._post_json = fake
while time.time() < start:
    time.sleep(0.0005)
for a in addrs:
    ev = {"session_id": "race", "tool_name": "mcp__ssh__execute-command",
          "hook_event_name": "PostToolUse",
          "tool_input": {"connectionName": a, "cmdString": "uptime"}, "tool_response": "up"}
    if role == "probe":
        mg.cmd_probe(ev)
    else:
        mg._reflex_lookup(ev, "target", [a])
"""
race_dir = pathlib.Path(tempfile.mkdtemp(prefix="mcrace_"))
# ⚠️ Кладём БРОШЕННЫЙ замок (процесс убит посреди записи): его снятие — и есть та гонка,
# ради которой замок снимается переименованием, а не unlink. Без этой строки откат на
# unlink оставил бы набор зелёным (ревью 14.09.2026, I2).
race_stale_lock = race_dir / "race.reflex.json.lock"
race_stale_lock.write_text("", encoding="utf-8")
_st_ts = time.time() - mg.REFLEX_LOCK_STALE - 5
os.utime(race_stale_lock, (_st_ts, _st_ts))
race_start = time.time() + 1.5
race_procs, race_want_v, race_want_s = [], set(), set()
for i, role in enumerate(["probe"] * 4 + ["lookup"] * 4):
    addrs = ["192.0.2.%d" % (i * 10 + j + 1) for j in range(10)]
    (race_want_v if role == "probe" else race_want_s).update(addrs)
    race_procs.append(subprocess.Popen(
        [sys.executable, "-c", RACE_WORKER, str(pathlib.Path(__file__).with_name("mc_guard.py")),
         str(race_dir), role, repr(race_start), ",".join(addrs)]))
race_codes = [p.wait(timeout=120) for p in race_procs]
try:
    race_st = json.loads((race_dir / "race.reflex.json").read_text(encoding="utf-8"))
except Exception:
    race_st = {}
race_lost_v = race_want_v - set((race_st.get("visits") or {}).keys())
race_lost_s = race_want_s - set((race_st.get("verify") or {}).keys())
check(not any(race_codes) and not race_lost_v and not race_lost_s,
      "замок: параллельные хуки одной сессии потеряли визиты %d/40 и цитаты %d/40 (коды %r)"
      % (len(race_lost_v), len(race_lost_s), race_codes))
check(not list(race_dir.glob("*.lock")), "замок: после параллельных записей остались файлы замка")
check(not list(race_dir.glob("*.dead.*")),
      "замок: остались следы снятия брошенного замка (*.dead.*)")
shutil.rmtree(race_dir, ignore_errors=True)

# Сквозная проверка: секреты чужих конфигов не утекают ни в запрос, ни в подсказку, ни в
# состояние, ни в журнал. С ПОЗИТИВНЫМ КОНТРОЛЕМ — адрес узла обязан дойти до запроса, иначе
# «секрета нет» проходило бы и на хуке, который вообще ничего не отправил.
leak_dir = pathlib.Path(tempfile.mkdtemp(prefix="mcleak_"))
SECRET_KEY, SECRET_PW = "КЛЮЧ-УТЕЧКИ-7f3a", "ПАРОЛЬ-УТЕЧКИ-9c1d"
(leak_dir / "ssh.json").write_text(json.dumps({"node-demo": {
    "name": "node-demo", "host": "192.0.2.10", "privateKey": SECRET_KEY}}, ensure_ascii=False),
    encoding="utf-8")
(leak_dir / "desk.json").write_text(json.dumps({"mcpServers": {"mikrotik": {"env": {
    "MIKROTIK_HOST": "192.0.2.1", "MIKROTIK_PASSWORD": SECRET_PW}}}}, ensure_ascii=False),
    encoding="utf-8")
mg.SSH_MCP_CONFIG, mg.DESKTOP_CONFIG = leak_dir / "ssh.json", leak_dir / "desk.json"
mg._target_map_cache = None
saved_post_leak = mg._post_json
mg._post_json = lambda path, payload, timeout=None: (sent.append((path, payload)) or {})
sent.clear(); got.clear()
mg.cmd_probe({"session_id": "leak-1", "tool_name": "mcp__ssh__execute-command",
              "hook_event_name": "PostToolUse",
              "tool_input": {"connectionName": "node-demo", "cmdString": "cat /etc/hostname"},
              "tool_response": "NODE-DEMO"})
mg.cmd_probe({"session_id": "leak-1", "tool_name": "mcp__mikrotik__mikrotik_get_system_identity",
              "hook_event_name": "PostToolUse", "tool_input": {},
              "tool_response": '[{"name": "RB-DEMO"}]'})
mg.cmd_nudge({"session_id": "leak-1", "tool_name": "mcp__memory-compiler__save_lesson",
              "tool_input": {"topic": "node-demo и роутер 192.0.2.1", "project": "testproj",
                             "content": "Проверено."},
              "tool_response": "✅ Создано: testproj/leak.md"})
artefacts = json.dumps(sent, ensure_ascii=False) + json.dumps(got, ensure_ascii=False)
artefacts += "".join(p.read_text(encoding="utf-8", errors="replace")
                     for p in mg.STATE_DIR.glob("leak-1*"))
if mg.HOOK_LOG.exists():
    artefacts += mg.HOOK_LOG.read_text(encoding="utf-8", errors="replace")
check(SECRET_KEY not in artefacts and SECRET_PW not in artefacts,
      "карта целей: секрет чужого конфига утёк в запрос, подсказку, состояние или журнал")
check(sent and "192.0.2.10" in json.dumps(sent[0][1]) and got,
      "карта целей (позитивный контроль): адрес не дошёл до запроса или подсказки нет")
mg._post_json = saved_post_leak
mg.SSH_MCP_CONFIG = pathlib.Path(tempfile.gettempdir()) / "mc-guard-test-no-ssh-mcp.json"
mg.DESKTOP_CONFIG = pathlib.Path(tempfile.gettempdir()) / "mc-guard-test-no-desktop.json"
mg._target_map_cache = None
shutil.rmtree(leak_dir, ignore_errors=True)

# Подсказка через НАСТОЯЩИЙ stdin, как её зовёт клиент: python на Windows читает stdin
# в кодировке консоли, и кириллица в подсказке портилась бы (урок session_arg 11.09).
sid_n = "nudge-proc"
st_dir_n = _state_dir(tmp3)
st_dir_n.mkdir(parents=True, exist_ok=True)
(st_dir_n / (sid_n + ".reflex.json")).write_text(json.dumps(
    {"visits": {"node-demo": {"ts": time.time(), "kind": "ssh"}}}), encoding="utf-8")
envn = _hook_env(tmp3)
procn = subprocess.run(
    [sys.executable, str(pathlib.Path(__file__).with_name("mc_guard.py")), "nudge"],
    input=json.dumps(dict(EV_SAVE_NODE, session_id=sid_n), ensure_ascii=False).encode("utf-8"),
    capture_output=True, timeout=60, env=envn)
# Под Kimi Code информационный PostToolUse уходит простым текстом, под Claude Code —
# JSON-обёрткой hookSpecificOutput (кириллица в нём — \uXXXX, это нормально).
ctxn = procn.stdout.decode("utf-8", errors="replace")
if PROFILE == "kimi":
    check("node-demo" in ctxn and "цитат" in ctxn and "hookSpecificOutput" not in ctxn,
          "подсказка (процесс): не пришла, ушла JSON-блобом или кириллица испорчена: %r" % ctxn[:80])
else:
    try:
        ctxn = (json.loads(ctxn or "{}").get("hookSpecificOutput") or {}).get("additionalContext") or ""
    except ValueError:
        ctxn = ""
    check("node-demo" in ctxn and "цитат" in ctxn,
          "подсказка (процесс): не пришла или кириллица испорчена: %r" % ctxn[:80])

# ─── карта целей: имя соединения ↔ адрес (v1.80.0) ───────────────────────────
# Узел зовут по-разному: замер 14.09.2026 — половина упоминаний только имя, 41% только
# адрес. Хук разворачивает цель в пару. Конфиги чужие: в ssh-mcp.json приватные ключи,
# в конфиге Desktop пароль роутера — читаем ТОЛЬКО name/host и MIKROTIK_HOST.
tmap_dir = pathlib.Path(tempfile.mkdtemp(prefix="mcmap_"))
(tmap_dir / "ssh-mcp.json").write_text(json.dumps({
    "node-demo": {"name": "node-demo", "host": "192.0.2.10", "port": 22,
                  "username": "root", "privateKey": "СЕКРЕТНЫЙ-КЛЮЧ-НЕ-ДОЛЖЕН-УТЕЧЬ"},
}, ensure_ascii=False), encoding="utf-8")
(tmap_dir / "desktop.json").write_text(json.dumps({
    "mcpServers": {"mikrotik": {"env": {"MIKROTIK_HOST": "192.0.2.1",
                                        "MIKROTIK_PASSWORD": "ПАРОЛЬ-НЕ-ДОЛЖЕН-УТЕЧЬ"}}},
}, ensure_ascii=False), encoding="utf-8")
mg.SSH_MCP_CONFIG = tmap_dir / "ssh-mcp.json"
mg.DESKTOP_CONFIG = tmap_dir / "desktop.json"
mg._target_map_cache = None

hint_ssh = mg._target_hint({"tool_name": "mcp__ssh__execute-command",
                            "tool_input": {"connectionName": "node-demo",
                                           "cmdString": "cat /etc/hostname"}})
check("node-demo" in hint_ssh and "192.0.2.10" in hint_ssh,
      "карта целей: имя соединения не развёрнуто в адрес: %r" % hint_ssh)
hint_mt = mg._target_hint({"tool_name": "mcp__mikrotik__mikrotik_get_system_identity",
                           "tool_input": {}})
check(hint_mt == "192.0.2.1",
      "карта целей: у mikrotik цель не подставлена из конфига: %r" % hint_mt)
check(mg._target_hint({"tool_name": "Bash", "tool_input": {"command": "ls -la"}}) == "",
      "карта целей: выдумана цель там, где сущности нет")
map_dump = json.dumps(mg._target_map(), ensure_ascii=False)
check("СЕКРЕТНЫЙ-КЛЮЧ" not in map_dump + hint_ssh and "ПАРОЛЬ-НЕ-ДОЛЖЕН" not in map_dump + hint_mt,
      "карта целей: из чужого конфига в карту попал секрет")

mg.SSH_MCP_CONFIG = tmap_dir / "нет-такого.json"
mg.DESKTOP_CONFIG = tmap_dir / "нет-такого.json"
mg._target_map_cache = None
check(mg._target_hint({"tool_name": "mcp__ssh__execute-command",
                       "tool_input": {"connectionName": "node-demo"}}) == "node-demo",
      "карта целей: без конфигов поведение должно остаться прежним")
(tmap_dir / "битый.json").write_text("{не json", encoding="utf-8")
mg.SSH_MCP_CONFIG = tmap_dir / "битый.json"
mg._target_map_cache = None
check(mg._target_hint({"tool_name": "mcp__ssh__execute-command",
                       "tool_input": {"connectionName": "node-demo"}}) == "node-demo",
      "карта целей: битый конфиг не должен ломать извлечение цели")
# ⚠️ user@host после опций: регулярка хоста спотыкалась о значение опции (`-p 2222`), не знала
# plink, а из `ssh admin@адрес` вытаскивала имя пользователя как цель (проверено 14.09.2026).
for cmd, want, not_want in (
        ("ssh -p 2222 root@node-demo uptime", "node-demo", "root"),
        ("plink -batch admin@rb-demo /export", "rb-demo", "admin"),
        ("scp file.tar user@192.0.2.10:/srv/", "192.0.2.10", "user"),
        ("ssh admin@192.0.2.10 cat /etc/hostname", "192.0.2.10", "admin")):
    got_h = mg._entities_from_command(cmd)
    check(want in got_h and not_want not in [x.strip() for x in got_h.split(",")],
          "цель из команды: %r дала %r" % (cmd, got_h))
check(mg._entities_from_command("ssh nas-demo 'docker ps'") == "nas-demo",
      "цель из команды: прежний вид «ssh хост» перестал работать")
mg.SSH_MCP_CONFIG = pathlib.Path(tempfile.gettempdir()) / "mc-guard-test-no-ssh-mcp.json"
mg.DESKTOP_CONFIG = pathlib.Path(tempfile.gettempdir()) / "mc-guard-test-no-desktop.json"
mg._target_map_cache = None
shutil.rmtree(tmap_dir, ignore_errors=True)

# ─── ревью хука 14.09.2026: адресат команды и визиты ─────────────────────────
# Ревью нашло: `x@y` внутри удалённой команды вытеснял сам узел (github.com вместо nas-demo),
# plink без user@ не разбирался, визитами становились 127.0.0.1, 8.8.8.8, `--tail`, имя
# контейнера и аргументы mikrotik, а один узел по имени и адресу давал два визита.
rv_dir = pathlib.Path(tempfile.mkdtemp(prefix="mcrev_"))
(rv_dir / "ssh.json").write_text(json.dumps({
    "nas-demo": {"host": "192.0.2.20", "port": 22},           # формат README: имя — ключ записи
    "default": {"host": "192.0.2.30"},
}), encoding="utf-8")
(rv_dir / "desk.json").write_text(json.dumps(
    {"mcpServers": {"mikrotik": {"env": {"MIKROTIK_HOST": "192.0.2.1"}}}}), encoding="utf-8")
mg.SSH_MCP_CONFIG, mg.DESKTOP_CONFIG = rv_dir / "ssh.json", rv_dir / "desk.json"
mg._target_map_cache = None
check(mg._target_map().get("nas-demo") == "192.0.2.20",
      "карта целей: объектный формат конфига без name внутри записи не прочитан")

for cmd, want in (
        ("ssh -p 2222 root@node-demo uptime", "node-demo"),
        ("plink -batch admin@rb-demo /export", "rb-demo"),
        ("plink -batch rb-demo /export", "rb-demo"),
        ("scp file.tar user@192.0.2.10:/srv/", "192.0.2.10"),
        ("ssh -J jump@bastion-1 root@node-demo uptime", "node-demo"),
        ("ssh -l admin1 node-demo uptime", "node-demo"),
        ("ssh -o StrictHostKeyChecking=no node-demo uptime", "node-demo"),
        ('rsync -av -e "ssh -p 22" src user@node-demo:/dst', "node-demo"),
        ("ssh root@[2001:db8::10] uptime", "2001:db8::10"),
        ("git clone git@github.com:org/repo.git", "")):
    got_dest = mg._ssh_destination(cmd)[0]
    check(got_dest == want, "адресат ssh: %r дал %r, ожидалось %r" % (cmd, got_dest, want))

for cmd, first in (
        ('ssh node-demo "curl -s http://admin:x@192.0.2.50/api"', "node-demo"),
        ('ssh mail-1 "grep user@example.com /var/log/mail.log"', "mail-1"),
        ('ssh nas-demo "git clone git@github.com:org/repo.git"', "nas-demo")):
    ents = [x.strip() for x in mg._entities_from_command(cmd).split(",")]
    check(ents and ents[0] == first,
          "цель из команды: %r вытеснен чужим x@y внутри удалённой команды: %r" % (first, ents))

check(mg._resolve_targets("Bash", "nas-demo, memory-compiler-mcp, node.example.local")
      == "nas-demo, 192.0.2.20, memory-compiler-mcp",
      "карта целей: адрес должен идти сразу за именем, иначе его отрезает потолок в три")
check("192.0.2.30" in mg._target_hint({"tool_name": "mcp__ssh__execute-command",
                                       "tool_input": {"cmdString": "uptime"}}),
      "карта целей: ssh-MCP без connectionName ходит через соединение default")

saved_post_rv = mg._post_json
mg._post_json = lambda path, payload, timeout=None: (sent.append((path, payload)) or {})


def _visits(sid):
    return mg._reflex_state({"session_id": sid}).get("visits") or {}


mg.cmd_probe({"session_id": "rv-1", "tool_name": "mcp__ssh__execute-command",
              "hook_event_name": "PostToolUse",
              "tool_input": {"connectionName": "nas-demo", "cmdString": "uptime"},
              "tool_response": "up 5 days"})
v1 = _visits("rv-1")
check(list(v1) == ["192.0.2.20"]
      and set((v1.get("192.0.2.20") or {}).get("names") or []) == {"nas-demo", "192.0.2.20"},
      "визиты: один узел по имени и адресу должен давать ОДИН визит по адресу: %r" % v1)
for sid, tool, ti in (
        ("rv-2", "Bash", {"command": "ssh nas-demo 'curl -s http://127.0.0.1:8765/api/health'"}),
        ("rv-3", "Bash", {"command": "ssh nas-demo ping -c1 8.8.8.8"}),
        ("rv-4", "Bash", {"command": "ssh nas-demo 'docker logs --tail 50 memory-compiler-mcp'"}),
        ("rv-5", "mcp__mikrotik__mikrotik_add_ip_address",
         {"address": "192.0.2.55/24", "interface": "ether2"}),
        ("rv-6", "mcp__mikrotik__mikrotik_set_system_identity", {"name": "RB-NEW"})):
    mg.cmd_probe({"session_id": sid, "tool_name": tool, "hook_event_name": "PostToolUse",
                  "tool_input": ti, "tool_response": "ok"})
    want = {"192.0.2.1"} if tool.startswith("mcp__mikrotik__") else {"192.0.2.20"}
    check(set(_visits(sid)) == want,
          "визиты: шум вместо адресата команды %r: %r" % (ti, sorted(_visits(sid))))
mg.cmd_probe({"session_id": "rv-7", "tool_name": "Bash", "hook_event_name": "PostToolUse",
              "tool_input": {"command": "ssh 127.0.0.1 uptime"}, "tool_response": "ok"})
check(not _visits("rv-7"), "визиты: локальный адрес — не узел инфраструктуры")

saved_cap = mg.REFLEX_STATE_CAP
mg.REFLEX_STATE_CAP = 3
for host in ("192.0.2.41", "192.0.2.42", "192.0.2.43", "192.0.2.41", "192.0.2.44"):
    mg.cmd_probe({"session_id": "rv-8", "tool_name": "Bash", "hook_event_name": "PostToolUse",
                  "tool_input": {"command": "ssh %s uptime" % host}, "tool_response": "ok"})
mg.REFLEX_STATE_CAP = saved_cap
check(set(_visits("rv-8")) == {"192.0.2.43", "192.0.2.41", "192.0.2.44"},
      "визиты: обновлённый узел вытеснен раньше старого: %r" % sorted(_visits("rv-8")))
mg._post_json = saved_post_rv
mg.SSH_MCP_CONFIG = pathlib.Path(tempfile.gettempdir()) / "mc-guard-test-no-ssh-mcp.json"
mg.DESKTOP_CONFIG = pathlib.Path(tempfile.gettempdir()) / "mc-guard-test-no-desktop.json"
mg._target_map_cache = None
shutil.rmtree(rv_dir, ignore_errors=True)

(tmp3 / "env").write_text("MC_API_KEY=test\n", encoding="utf-8")
envx = _hook_env(tmp3, MC_API_URL="http://127.0.0.1:9", MC_ENV_FILE=str(tmp3 / "env"))
t_start = time.time()
proc = subprocess.run(
    [sys.executable, str(pathlib.Path(__file__).with_name("mc_guard.py")), "reflex"],
    input=json.dumps(dict(EV_FAIL, session_id="rx-proc"), ensure_ascii=False).encode("utf-8"),
    capture_output=True, timeout=60, env=envx)
check(proc.returncode == 0 and not proc.stdout.strip(),
      "reflex (процесс): при недоступном сервере есть вывод или код не 0")
check(time.time() - t_start < 10, "reflex (процесс): недоступный сервер держит хук слишком долго")
shutil.rmtree(tmp3, ignore_errors=True)


# ── ротация журнала не теряет историю ───────────────────────────────────────
# Было: при превышении HOOK_LOG_MAX journal() оставлял ПОСЛЕДНИЕ 5000 строк, а
# остальное удалял безвозвратно. 20.09.2026 в логе лежало 17 269 строк за 26
# дней, и именно по ним мерились приживаемость живой карточки по дням и доля
# покрытых ошибок — после первой же ротации такой замер стал бы невозможен,
# причём молча. Теперь старое уезжает в архив рядом с логом.

tmp_rot = pathlib.Path(tempfile.mkdtemp(prefix="mcrot_"))
_log_keep, _max_keep = mg.HOOK_LOG, mg.HOOK_LOG_MAX
mg.HOOK_LOG = tmp_rot / "hooks.log"
mg.HOOK_LOG_MAX = 2000                      # маленький порог: тест не пишет мегабайты

_oldest = json.dumps({"ts": "2026-01-01 00:00:00", "action": "oldest.marker"},
                     ensure_ascii=False)
mg.HOOK_LOG.write_text(_oldest + "\n" + ("x" * 60 + "\n") * 60, encoding="utf-8")
check(mg.HOOK_LOG.stat().st_size > mg.HOOK_LOG_MAX, "ротация: подготовка не превысила порог")

mg.journal({"session_id": "rot-test"}, "after.rotate")

check("after.rotate" in mg.HOOK_LOG.read_text(encoding="utf-8"),
      "ротация: свежая запись не легла в журнал")
check(mg.HOOK_LOG.stat().st_size < mg.HOOK_LOG_MAX,
      "ротация: журнал не ужался")
_arch = tmp_rot / "hooks.log.1"
check(_arch.exists(), "ротация: архив не создан — история удалена безвозвратно")
check(_arch.exists() and "oldest.marker" in _arch.read_text(encoding="utf-8"),
      "ротация: самая старая запись потеряна вместо переезда в архив")

# второй проход: прежний архив не затирается, а сдвигается
mg.HOOK_LOG.write_text(("y" * 60 + "\n") * 60, encoding="utf-8")
mg.journal({"session_id": "rot-test"}, "second.rotate")
check((tmp_rot / "hooks.log.2").exists(), "ротация: второй проход не сдвинул архив")
_arch2 = tmp_rot / "hooks.log.2"
check(_arch2.exists() and "oldest.marker" in _arch2.read_text(encoding="utf-8"),
      "ротация: первый архив затёрт вторым — история всё равно теряется")

mg.HOOK_LOG, mg.HOOK_LOG_MAX = _log_keep, _max_keep
shutil.rmtree(tmp_rot, ignore_errors=True)

# ── Stop отличает ОТКАЗ от ЦИТАТЫ отказа ───────────────────────────────────
# За 26 дней журнала stop.excuse сработал 3 раза, и 2 из них — 20.09.2026 в
# сессии, которая ОБСУЖДАЛА сам механизм: фразы стояли в кавычках как цитата
# правила, а хук прочитал их как отказ работать и дважды заблокировал ход.
# Настоящее срабатывание за весь журнал одно (29.08). Ложная блокировка стоит
# хода работы — дороже, чем всё, что этот страж экономит.

check(mg._is_excuse("У меня нет доступа к роутеру, запусти сам"),
      "Stop: настоящий отказ перестал распознаваться")
check(mg._is_excuse("Нужен пароль от NAS"),
      "Stop: отказ про пароль перестал распознаваться")
check(not mg._is_excuse("Фразы «нет доступа», «сделай сам» без поиска запрещены"),
      "Stop: цитата правила в кавычках принята за отказ")
check(not mg._is_excuse('Хук ловит фразу "нет доступа" в тексте ответа'),
      "Stop: цитата в прямых кавычках принята за отказ")
check(not mg._is_excuse("В коде `нет доступа` — это маркер EXCUSE_RE"),
      "Stop: фраза в кодовой вставке принята за отказ")
check(mg._is_excuse("«Роутер» настроен, но у меня нет доступа к нему"),
      "Stop: кавычки в НАЧАЛЕ строки погасили настоящий отказ дальше по тексту")


# ── постоянное напоминание — не на каждом сообщении ────────────────────────
# Замер 20.09.2026 по транскриптам: за 7 дней текст вставлен 1296 раз (493 776
# символов), и лишь 44 вставки несли что-то сверх него — «контекст устарел» 23,
# «не записано» 15, «досланы» 6. То есть 96,6% вставок повторяли одно и то же,
# по 5-6 копий на сессию. Условные блоки при этом обязаны приходить ВСЕГДА:
# чужая запись в базу — новость, которая стареет.

tmp_rule = pathlib.Path(tempfile.mkdtemp(prefix="mcrule_"))
_sd_keep = mg.STATE_DIR
mg.STATE_DIR = tmp_rule
_ev_rule = {"session_id": "rule-test"}

_out1 = []
mg.emit = lambda payload: _out1.append(payload)
mg.cmd_freshness(_ev_rule)
check(any(mg.RULE in json.dumps(p, ensure_ascii=False) for p in _out1),
      "напоминание: первое сообщение сессии осталось без правила")

_out2 = []
mg.emit = lambda payload: _out2.append(payload)
mg.cmd_freshness(_ev_rule)
check(not any(mg.RULE in json.dumps(p, ensure_ascii=False) for p in _out2),
      "напоминание: повторено на следующем же сообщении")

_st = mg.load_state(_ev_rule)
_st["rule_ts"] = time.time() - mg.RULE_EVERY_SEC - 60
mg.save_state(_ev_rule, _st)
_out3 = []
mg.emit = lambda payload: _out3.append(payload)
mg.cmd_freshness(_ev_rule)
check(any(mg.RULE in json.dumps(p, ensure_ascii=False) for p in _out3),
      "напоминание: не вернулось после истечения интервала")

mg.STATE_DIR = _sd_keep
shutil.rmtree(tmp_rule, ignore_errors=True)


# ── исправленный повтор снимает отказ валидации (25.09.2026) ────────────────
# SessionStart раз за разом показывал «БРОШЕНО ДРУГИМИ СЕССИЯМИ»: 23.09 — 10 записей,
# 25.09 — 16. По транскриптам легла каждая: модель угадывала имена полей (title вместо
# topic, summary вместо topic/content, facts списком), сервер отбивал вызов на
# валидации, исправленный повтор той же сессии проходил через 3–64 с. Снять запись было
# некому: _close_retried ждёт ТЕХ ЖЕ аргументов, а _already_in_audit ключуется по
# topic/filename, которых у такой записи нет. Повтор по подсказке SessionStart записал бы
# дубль. Тексты ошибок и наборы полей — из разобранных записей очереди (*.json.delivered),
# имена клиентов заменены.

tmp_fix = pathlib.Path(tempfile.mkdtemp(prefix="mcfix_"))
_fix_keep = (mg.PENDING_DIR, mg.STATE_DIR, mg.HOOK_LOG, mg.emit, mg._tail_audit, mg._post_save)
mg.PENDING_DIR = tmp_fix / "pending"
mg.STATE_DIR = tmp_fix / "state"
mg.STATE_DIR.mkdir(parents=True, exist_ok=True)
mg.HOOK_LOG = tmp_fix / "hooks.log"
mg.emit = lambda payload: None
mg._tail_audit = lambda *a, **k: []     # зеркало аудита отстаёт на минуты — снять должен сам успех
mg._post_save = lambda payload: (False, "тест: REST выключен")   # тесты в базу не пишут

ERR_NO_ENTITY = (
    'MCP error -32602: Input validation error: Invalid arguments for tool save_tracking: [\n'
    '  {\n    "expected": "string",\n    "code": "invalid_type",\n    "path": [\n'
    '      "entity"\n    ],\n    "message": "Invalid input: expected string, received undefined"\n'
    '  },\n  {\n    "code": "invalid_type",\n    "expected": "nonoptional",\n    "path": [\n'
    '      "facts"\n    ],\n    "message": "Invalid input: expected nonoptional, received undefined"\n'
    '  }\n]')
ERR_FACTS_LIST = ("Input validation error: ['Проверка 23.09.2026 09:13: тревог нет', "
                  "'traffic.json 23.09: used=3650.02 limit_gb=9313'] is not of type 'object', 'string'")
ERR_NO_TOPIC = (
    'MCP error -32602: Input validation error: Invalid arguments for tool finish_task: [\n'
    '  {\n    "expected": "string",\n    "code": "invalid_type",\n    "path": [\n'
    '      "topic"\n    ],\n    "message": "Invalid input: expected string, received undefined"\n'
    '  },\n  {\n    "expected": "string",\n    "code": "invalid_type",\n    "path": [\n'
    '      "content"\n    ],\n    "message": "Invalid input: expected string, received undefined"\n'
    '  }\n]')
ERR_LESSON_TOPIC = (
    'MCP error -32602: Input validation error: Invalid arguments for tool save_lesson: [\n'
    '  {\n    "expected": "string",\n    "code": "invalid_type",\n    "path": [\n'
    '      "topic"\n    ],\n    "message": "Invalid input: expected string, received undefined"\n'
    '  }\n]')
ERR_ENOENT = ("[Errno 2] No such file or directory: "
              "'/knowledge/proj-fixa/tracking_owner/.PR #94.md.5bn2vrz_.tmp'")


def _mc(sid, tid, tool, args):
    return {"session_id": sid, "tool_use_id": tid,
            "tool_name": "mcp__memory-compiler__" + tool, "tool_input": args}


def _failed(ev, error):
    """Вызов прошёл PreToolUse и упал: в очереди запись с текстом отказа."""
    mg.cmd_intent(ev)
    mg.cmd_fail(dict(ev, hook_event_name="PostToolUseFailure", error=error))
    return mg._pending_path(ev)


def _succeeded(ev):
    mg.cmd_intent(ev)
    mg.cmd_mark(dict(ev, hook_event_name="PostToolUse"))


# Цепочка одной регламентной сессии 23.09: title вместо entity → facts списком → «/» в
# entity (ENOENT, баг сервера) → удачный повтор. Обе записи валидации — мусор, запись
# ENOENT — нет: сервер упал посреди записи, и легло ли содержание, знает только проверка.
EV_T1 = _mc("fix-a", "toolu_FIX_A1", "save_tracking",
            {"project": "proj-fixa", "title": "PR #94", "content": "ревью закрыто", "tags": ["pr"]})
EV_T2 = _mc("fix-a", "toolu_FIX_A2", "save_tracking",
            {"project": "proj-fixa", "entity": "PR #94", "facts": ["ревью закрыто", "5 пунктов"],
             "tags": ["pr"]})
EV_T3 = _mc("fix-a", "toolu_FIX_A3", "save_tracking",
            {"project": "proj-fixa", "entity": "owner/PR #94", "facts": {"ревью": "закрыто"},
             "tags": ["pr"]})
p_t1 = _failed(EV_T1, ERR_NO_ENTITY)
p_t2 = _failed(EV_T2, ERR_FACTS_LIST)
p_t3 = _failed(EV_T3, ERR_ENOENT)
_succeeded(_mc("fix-a", "toolu_FIX_A4", "save_tracking",
               {"project": "proj-fixa", "entity": "PR #94", "facts": {"ревью": "закрыто"},
                "tags": ["pr"]}))
check(not p_t1.exists(), "исправленный повтор не снял отказ -32602 (title вместо entity) — запись "
      "станет «брошенной», и повтор по подсказке SessionStart даст дубль")
check(not p_t2.exists(), "исправленный повтор не снял отказ «Input validation error» (facts списком)")
check(p_t3.exists(), "успех снял отказ ENOENT — сервер упал посреди записи, содержание могло не "
      "лечь, снимать без проверки нельзя")

# finish_task с summary вместо topic/content: исправленный повтор через 4 с.
EV_F1 = _mc("fix-b", "toolu_FIX_B1", "finish_task",
            {"project": "proj-fixb", "summary": "Проверка VPS: тревог нет"})
p_f1 = _failed(EV_F1, ERR_NO_TOPIC)
_succeeded(_mc("fix-b", "toolu_FIX_B2", "finish_task",
               {"project": "proj-fixb", "topic": "Проверка VPS", "content": "Тревог нет."}))
check(not p_f1.exists(), "finish_task: повтор с topic/content не снял отказ с summary")
check(not _stop_blocks("fix-b"),
      "Stop требует повторить то, что уже легло исправленным повтором, — второй повтор даст дубль")

# Отказ транспорта -32602 (сессия SSE до инициализации) тоже не доходит до инструмента.
EV_J1 = _mc("fix-j", "toolu_FIX_J1", "save_lesson",
            {"project": "proj-fixj", "topic": "Урок J", "content": "Тело J"})
p_j1 = _failed(EV_J1, "MCP error -32602: Invalid request parameters")
_succeeded(_mc("fix-j", "toolu_FIX_J2", "save_lesson",
               {"project": "proj-fixj", "topic": "Урок J", "content": "Тело J, уточнено"}))
check(not p_j1.exists(), "повтор не снял отказ транспорта -32602 — до инструмента вызов не дошёл")

# Симптом целиком: через полчаса новая сессия видит брошенным только отказ ENOENT.
for _p in mg._pending_all():
    _r = mg._pending_load(_p) or {}
    _r["created"] = time.time() - mg.ORPHAN_MIN_AGE - 60
    _p.write_text(json.dumps(_r, ensure_ascii=False), encoding="utf-8")
_ctx = _hook_context(mg.cmd_session_start, {"session_id": "fix-newcomer"})
check("proj-fixb" not in _ctx,
      "SessionStart показал брошенной запись, легшую исправленным повтором, — ложная тревога 23–25.09")
check("proj-fixa" in _ctx, "SessionStart промолчал о брошенном отказе ENOENT — содержание могло не лечь")

for _p in mg._pending_all():
    mg._pending_drop(_p)
mg.emit = lambda payload: None

# Успех снимает только отказ ТОГО ЖЕ инструмента: finish_task не лечит save_lesson.
EV_L1 = _mc("fix-c", "toolu_FIX_C1", "save_lesson",
            {"project": "proj-fixc", "title": "Урок C", "content": "Тело C"})
p_l1 = _failed(EV_L1, ERR_LESSON_TOPIC)
_succeeded(_mc("fix-c", "toolu_FIX_C2", "finish_task",
               {"project": "proj-fixc", "topic": "Итог C", "content": "Сделано."}))
check(p_l1.exists(), "успех ДРУГОГО инструмента снял отказ валидации save_lesson — урок пропадёт")

# Чужая живая сессия: её отказ повторит владелец, успех посторонней сессии его не снимает.
EV_L2 = _mc("fix-d", "toolu_FIX_D1", "save_lesson",
            {"project": "proj-fixd", "title": "Урок D", "content": "Тело D"})
p_l2 = _failed(EV_L2, ERR_LESSON_TOPIC)
_succeeded(_mc("fix-e", "toolu_FIX_E1", "save_lesson",
               {"project": "proj-fixd", "topic": "Урок E", "content": "Тело E"}))
check(p_l2.exists(), "успех ЧУЖОЙ сессии снял отказ валидации — владелец не узнает, что урок не лёг")

# Запись без сессии (клиент не прислал session_id) ничья: чужой успех её не присваивает.
EV_L6 = _mc("", "toolu_FIX_K1", "save_lesson",
            {"project": "proj-fixk", "title": "Урок K", "content": "Тело K"})
p_l6 = _failed(EV_L6, ERR_LESSON_TOPIC)
_succeeded(_mc("fix-k", "toolu_FIX_K2", "save_lesson",
               {"project": "proj-fixk", "topic": "Урок K2", "content": "Тело K2"}))
check(p_l6.exists(), "успех посторонней сессии снял отказ записи без сессии — это не её повтор")

# Старше окна — это уже не повтор, а другая запись: отказ остаётся.
EV_L3 = _mc("fix-f", "toolu_FIX_F1", "save_lesson",
            {"project": "proj-fixf", "title": "Урок F", "content": "Тело F"})
p_l3 = _failed(EV_L3, ERR_LESSON_TOPIC)
_r = mg._pending_load(p_l3) or {}
_r["created"] = time.time() - getattr(mg, "REJECTED_RETRY_WINDOW", 600) - 60
p_l3.write_text(json.dumps(_r, ensure_ascii=False), encoding="utf-8")
_succeeded(_mc("fix-f", "toolu_FIX_F2", "save_lesson",
               {"project": "proj-fixf", "topic": "Другой урок", "content": "Другое тело"}))
check(p_l3.exists(), "успех через много минут снял старый отказ валидации — это уже не повтор")

# Таймаут: вызов мог дойти и записать — повтор с другими аргументами его не снимает.
EV_E1 = _mc("fix-g", "toolu_FIX_G1", "edit_article",
            {"project": "proj-fixg", "filename": "veeam.md", "append": True,
             "content": "кто разбирает"})
p_e1 = _failed(EV_E1, "Error: Request timed out")
_succeeded(_mc("fix-g", "toolu_FIX_G2", "edit_article",
               {"project": "proj-fixg", "filename": "veeam.md", "append": True,
                "content": "кто разбирает, дополнено"}))
check(p_e1.exists(), "успех снял отказ по таймауту с другими аргументами — запись могла не лечь")

# Параллельный вызов ещё в полёте (intent есть, исхода нет) — это не отказ. Сними его, и
# упади он потом, cmd_fail не найдёт записи: содержание не попадёт в досыл.
EV_L4 = _mc("fix-h", "toolu_FIX_H1", "save_lesson",
            {"project": "proj-fixh", "topic": "Урок H1", "content": "Тело H1"})
mg.cmd_intent(EV_L4)
_succeeded(_mc("fix-h", "toolu_FIX_H2", "save_lesson",
               {"project": "proj-fixh", "topic": "Урок H2", "content": "Тело H2"}))
check(mg._pending_path(EV_L4).exists(), "успех снял запись параллельного вызова, который ещё в полёте")

# «could not be parsed» — клиент не собрал JSON, сервер вызова не видел. Повтор тут дробят
# на части, и первая удачная часть не значит, что легло всё.
EV_L5 = _mc("fix-i", "toolu_FIX_I1", "save_lesson",
            {"__unparsedToolInput": {"raw": '{"project": "proj-fixi", "topic": "Урок I", '
                                            '"content": "очень длинн'}})
p_l5 = _failed(EV_L5, ERR_PARSE)
_succeeded(_mc("fix-i", "toolu_FIX_I2", "save_lesson",
               {"project": "proj-fixi", "topic": "Урок I, часть 1", "content": "Первая часть."}))
check(p_l5.exists(), "успех первой части снял отказ «could not be parsed» — остальные части не напомнятся")

mg.PENDING_DIR, mg.STATE_DIR, mg.HOOK_LOG, mg.emit, mg._tail_audit, mg._post_save = _fix_keep
shutil.rmtree(tmp_fix, ignore_errors=True)


# ------------------------------------------------ промах поиска в /mc-stats
# С v1.91.0 сервер пишет в запись аудита search число найденного (args._count). Короткая, но
# непустая выдача (один результат) бывает короче SEARCH_MISS_SIZE, а пустая с notice — длиннее,
# поэтому промах — это _count == 0. Без _count (старые записи, другие поисковые инструменты,
# _count: None) — прежний порог по размеру. Та же логика — analytics.quality() сервера.
from datetime import datetime as _dt, timedelta as _td

_stats_keep = mg._tail_audit
_now = _dt.now()


def _audit(sec_ago, size, **args):
    return {"ts": (_now - _td(seconds=sec_ago)).strftime(mg.AUDIT_TS_FMT),
            "tool": "search", "size": size, "args": args}


_stats_rows = [
    _audit(50, 150, query="один результат", _count=1),
    _audit(40, 420, query="пусто с notice", _count=0),
    _audit(30, 90, query="старая запись"),
    _audit(25, 80, query="count None", _count=None),
    _audit(20, 5000, query="старая полная"),
]
mg._tail_audit = lambda *a, **k: [dict(r) for r in _stats_rows]
_missed = sorted(r["args"]["query"] for r in mg._product_stats(1)["misses"])
check(_missed == ["count None", "пусто с notice", "старая запись"],
      "промахи поиска не по _count: %r" % _missed)
mg._tail_audit = _stats_keep

# ─── готовая цитата проверки из увиденного вывода (вариант (а), 25.09.2026) ──
# Замер 20.09: напоминание с шаблоном «<команда> => <значение>» после 15.09 ни разу не
# стало цитатой. Хук сам видел вывод — пусть предлагает готовую пару. Замер 25.09: годная
# пара есть примерно у каждого десятого напоминания, поэтому только стабильные read-only
# команды из белого списка, цельные, с однострочным значением; иначе прежний шаблон.
pair_dir = pathlib.Path(tempfile.mkdtemp(prefix="mcpair_"))
(pair_dir / "ssh.json").write_text(json.dumps({"nas-demo": {"host": "192.0.2.20"}}),
                                   encoding="utf-8")
(pair_dir / "desk.json").write_text(json.dumps(
    {"mcpServers": {"mikrotik": {"env": {"MIKROTIK_HOST": "192.0.2.1"}}}}), encoding="utf-8")
mg.SSH_MCP_CONFIG, mg.DESKTOP_CONFIG = pair_dir / "ssh.json", pair_dir / "desk.json"
mg._target_map_cache = None


def _ssh_ev(cmd, out, sid="pair-x"):
    return {"session_id": sid, "tool_name": "mcp__ssh__execute-command",
            "hook_event_name": "PostToolUse",
            "tool_input": {"connectionName": "nas-demo", "cmdString": cmd}, "tool_response": out}


def _tool_ev(tool, out, ti=None):
    return {"session_id": "pair-x", "tool_name": tool, "hook_event_name": "PostToolUse",
            "tool_input": ti or {}, "tool_response": out}


OS_RELEASE = ('NAME="Ubuntu"\nVERSION_ID="24.04"\nPRETTY_NAME="Ubuntu 24.04.4 LTS"\n'
              'ID=ubuntu\n')
IPIFY = "curl -s -m 15 --proxy socks5h://10.99.98.2:1080 https://api.ipify.org"
for ev, want in (
        (_ssh_ev("cat /etc/hostname", "VPS-JP2\n"), ("cat /etc/hostname", "VPS-JP2")),
        (_ssh_ev("cat /opt/niksdesk/VERSION", "1.3.245\n"),
         ("cat /opt/niksdesk/VERSION", "1.3.245")),
        (_ssh_ev("cat /etc/os-release", OS_RELEASE), ("cat /etc/os-release", "Ubuntu 24.04.4 LTS")),
        (_ssh_ev(IPIFY, "203.0.113.10"), (IPIFY, "203.0.113.10")),
        (_ssh_ev("sudo cat /etc/hostname", "NAS-TEST\n"),
         ("sudo cat /etc/hostname", "NAS-TEST")),
        (_ssh_ev("/system identity print", "  name: RB5009UG\n"),
         ("/system identity print", "RB5009UG")),
        (_ssh_ev("/system resource print", "  uptime: 1w4d\n  version: 7.24.2 (stable)\n"),
         ("/system resource print", "7.24.2 (stable)")),
        (_tool_ev("Bash", {"stdout": "NAS-TEST\n", "stderr": "", "interrupted": False},
                  {"command": "ssh nas-demo cat /etc/hostname"}),
         ("cat /etc/hostname", "NAS-TEST")),
        (_tool_ev("mcp__mikrotik__mikrotik_get_system_identity", '{"name": "RB5009UG"}'),
         ("/system identity print", "RB5009UG")),
        (_tool_ev("mcp__mikrotik__mikrotik_system_info",
                  [{"type": "text", "text": '[{"uptime": "1w", "version": "7.24.2 (stable)"}]'}]),
         ("/system resource print", "7.24.2 (stable)")),
):
    got_pair = mg._ready_pair(ev, ev["tool_name"] == "Bash")
    check(got_pair == want, "готовая пара: %r дал %r, ожидалось %r"
          % (ev["tool_input"].get("cmdString") or ev["tool_name"], got_pair, want))

for ev, why in (
        (_ssh_ev("cat /etc/hostname | tr a-z A-Z", "VPS-JP2"), "конвейер"),
        (_ssh_ev("cat /etc/hostname; uname -r", "VPS-JP2\n6.8"), "цепочка"),
        (_ssh_ev("cat /etc/hostname > /tmp/h", ""), "перенаправление"),
        (_ssh_ev("date -u", "Thu Sep 25 06:02:16 UTC 2026"), "время — не стабильный факт"),
        (_ssh_ev("hostname", "niksdesk\n"), "сервер отвергнет: нет читающего глагола"),
        (_ssh_ev("uname -r", "6.8.0-139-generic\n"), "сервер отвергнет: нет читающего глагола"),
        (_ssh_ev("cat /etc/hostname", "pc\n"), "сервер требует значение от 3 символов"),
        (_ssh_ev("cat /etc/passwd", "root:x:0:0:root:/root:/bin/bash"), "не из белого списка"),
        (_ssh_ev("cat /etc/hostname", "a\nb\n"), "многострочный вывод"),
        (_ssh_ev("cat /etc/hostname", "x" * 81), "длиннее 80 символов"),
        (_ssh_ev("cat /etc/hostname", "\x1b[?1hVPS\x1b="), "управляющие символы"),
        (_ssh_ev("cat /etc/hostname", "  \n"), "пустой вывод"),
        (_ssh_ev("cat /etc/hostname", "cat: /etc/hostname: Permission denied"), "отказ доступа"),
        (_ssh_ev("cat /etc/hostname", "cat: /etc/hostname: No such file or directory"), "ошибка"),
        (_ssh_ev(IPIFY, "<html>502 Bad Gateway</html>"), "вместо IP страница ошибки"),
        (_ssh_ev("curl -s -o /tmp/x -w %{remote_ip} https://api.ipify.org", "203.0.113.10"),
         "curl пишет файл на узел — цитату потом исполнят"),
        (_ssh_ev("curl -s --proxy socks5h://$(hostname):1080 https://api.ipify.org",
                 "203.0.113.10"), "подстановка команды в опции curl"),
        (_ssh_ev("cat /etc/os-release", 'NAME="Ubuntu"\n'), "нет PRETTY_NAME"),
        (dict(_ssh_ev("cat /etc/hostname", "VPS-JP2"), error="exit 1"), "команда упала"),
        (_tool_ev("Bash", {"stdout": "VPS-JP2\n"}, {"command": "cat /etc/hostname"}),
         "локальная команда — не выход на узел"),
        (_tool_ev("Bash", {"stdout": "VPS-JP2\n", "interrupted": True},
                  {"command": "ssh nas-demo cat /etc/hostname"}), "прервана"),
        (_tool_ev("mcp__mikrotik__mikrotik_execute_command", "  name: RB5009UG",
                  {"command": "/system/identity/print", "params": {"where": "x"}}),
         "params фильтруют вывод"),
):
    check(mg._ready_pair(ev, ev["tool_name"] == "Bash") is None,
          "готовая пара: ложная пара — " + why)

saved_post_pair = mg._post_json
mg._post_json = lambda path, payload, timeout=None: {}
mg.cmd_probe(_ssh_ev("cat /etc/hostname", "VPS-JP2\n", sid="pair-probe"))
mg.cmd_probe(_ssh_ev("uptime", "up 5 days", sid="pair-probe"))
seen_p = (_visits("pair-probe").get("192.0.2.20") or {}).get("seen") or []
check([p[:2] for p in seen_p] == [["cat /etc/hostname", "VPS-JP2"]],
      "готовая пара: probe не запомнил пару или обычная команда её затёрла: %r" % seen_p)
for cmd, out in (("cat /etc/hostname", "node-a"), ("cat /opt/a/VERSION", "1.0"),
                 ("cat /opt/b/VERSION", "2.0"), ("cat /opt/c/VERSION", "3.0"),
                 ("cat /etc/hostname", "node-a2")):
    mg.cmd_probe(_ssh_ev(cmd, out + "\n", sid="pair-cap"))
seen_c = (_visits("pair-cap").get("192.0.2.20") or {}).get("seen") or []
check([p[0] for p in seen_c] == ["cat /opt/b/VERSION", "cat /opt/c/VERSION", "cat /etc/hostname"]
      and seen_c[-1][1] == "node-a2",
      "готовая пара: потолок три команды, повтор команды заменяет значение: %r" % seen_c)
mg._post_json = saved_post_pair

got.clear()
mg.emit = lambda payload: got.append(payload)     # секции выше глушат вывод
t_now = time.time()
EV_READY = {"session_id": "pair-nudge", "tool_name": "mcp__memory-compiler__save_lesson",
            "tool_input": {"topic": "Узел nas-demo: имя хоста", "project": "testproj",
                           "content": "nas-demo отвечает как VPS-JP2."},
            "tool_response": "✅ Создано: testproj/nas.md"}
mg._reflex_state_save(EV_READY, {"visits": {"192.0.2.20": {
    "ts": t_now, "kind": "ssh", "names": ["192.0.2.20", "nas-demo"],
    "seen": [["cat /etc/os-release", "Ubuntu 24.04.4 LTS", t_now - 9000],
             ["cat /etc/hostname", "VPS-JP2", t_now - 60]]}}})
log_mark_pair = mg.HOOK_LOG.stat().st_size if mg.HOOK_LOG.exists() else 0
mg.cmd_nudge(EV_READY)
ctx_r = got[0]["hookSpecificOutput"]["additionalContext"] if got else ""
check('verify=["cat /etc/hostname => VPS-JP2"]' in ctx_r,
      "готовая пара: напоминание не подставило увиденную пару: %r" % ctx_r[:200])
check("os-release" not in ctx_r, "готовая пара: подставлена пара старше окна напоминания")
check("<команда>" not in ctx_r, "готовая пара: при готовой паре остался шаблон")
check('triggers=["цель: 192.0.2.20"]' in ctx_r and 'filename="nas.md"' in ctx_r,
      "готовая пара: вызов без цели или без файла статьи: %r" % ctx_r[:200])
tail_r = ""
if mg.HOOK_LOG.exists():
    with mg.HOOK_LOG.open("rb") as f:
        f.seek(log_mark_pair)
        tail_r = f.read().decode("utf-8", errors="replace")
check('"nudge.ready"' in tail_r, "готовая пара: в журнале нет nudge.ready — конверсию не посчитать")

got.clear()
mg._reflex_state_save(dict(EV_READY, session_id="pair-nudge-2"),
                      {"visits": {"192.0.2.20": {"ts": t_now, "kind": "ssh",
                                                 "names": ["192.0.2.20", "nas-demo"]}}})
mg.cmd_nudge(dict(EV_READY, session_id="pair-nudge-2"))
ctx_t = got[0]["hookSpecificOutput"]["additionalContext"] if got else ""
check("<команда> => <значение>" in ctx_t,
      "готовая пара: без увиденной пары должен остаться прежний шаблон: %r" % ctx_t[:200])
mg.SSH_MCP_CONFIG = pathlib.Path(tempfile.gettempdir()) / "mc-guard-test-no-ssh-mcp.json"
mg.DESKTOP_CONFIG = pathlib.Path(tempfile.gettempdir()) / "mc-guard-test-no-desktop.json"
mg._target_map_cache = None
shutil.rmtree(pair_dir, ignore_errors=True)

# ── детект клиента: argv > env > client_type > эвристика > fallback ─────────
_saved_argv = sys.argv[:]
_saved_mgc = os.environ.get("MC_GUARD_CLIENT")


def _detect(argv=(), env=None, event=None):
    sys.argv = ["mc_guard.py"] + list(argv)
    if env is None:
        os.environ.pop("MC_GUARD_CLIENT", None)
    else:
        os.environ["MC_GUARD_CLIENT"] = env
    try:
        return mg.detect_client(event)
    finally:
        sys.argv = _saved_argv
        if _saved_mgc is None:
            os.environ.pop("MC_GUARD_CLIENT", None)
        else:
            os.environ["MC_GUARD_CLIENT"] = _saved_mgc


check(_detect(argv=["--client=kimi"], env="claude", event={"client_type": "claude_code"})
      == ("kimi", "argv"), "detect_client: argv не в приоритете над env")
check(_detect(env="kimi", event={"client_type": "claude_code"}) == ("kimi", "env"),
      "detect_client: env не в приоритете над client_type")
check(_detect(event={"client_type": "kimi_code_cli", "tool_use_id": "t1"}) == ("kimi", "event"),
      "detect_client: client_type kimi_code_cli не распознан")
check(_detect(event={"tool_call_id": "c1"}) == ("kimi", "heuristic"),
      "detect_client: tool_call_id без tool_use_id не сработал как эвристика")
check(_detect(event={"prompt": [{"type": "text", "text": "привет"}]}) == ("kimi", "heuristic"),
      "detect_client: prompt списком не сработал как эвристика")
check(_detect(event={"session_id": "s", "tool_use_id": "t1"}) == ("claude", "fallback"),
      "detect_client: пустые сигнатуры обязаны давать fallback claude")
check(mg._EMIT_PLAIN == (PROFILE == "kimi"),
      "профиль: _set_client не выставил режим emit под тестируемый профиль")


# ── локальный mc_guard.env: слой между os.environ и дефолтами ───────────────
_env_dir = pathlib.Path(tempfile.mkdtemp(prefix="mclocalenv_"))
_env_path = _env_dir / "mc_guard.env"
_env_path.write_text("# комментарий\n\nMC_TEST_URL=http://file-value:1\n"
                     "MC_TEST_KD=%s\n" % str(_env_dir / "knowledge").replace("\\", "/"),
                     encoding="utf-8")
_saved_env_path = mg._LOCAL_ENV_PATH
mg._LOCAL_ENV_PATH = _env_path
mg._reset_local_env()
os.environ.pop("MC_TEST_URL", None)
check(mg._conf("MC_TEST_URL") == "http://file-value:1",
      "mc_guard.env: значение из файла рядом со скриптом не подхвачено")
os.environ["MC_TEST_URL"] = "http://env-value:2"
check(mg._conf("MC_TEST_URL") == "http://env-value:2",
      "mc_guard.env: os.environ обязан быть выше файла")
os.environ.pop("MC_TEST_URL", None)
check(mg._conf("MC_TEST_MISSING") is None and mg._conf("MC_TEST_MISSING", "d") == "d",
      "mc_guard.env: отсутствующий ключ не дал None/дефолт")
mg._LOCAL_ENV_PATH = _env_dir / "нет-такого.env"
mg._reset_local_env()
check(mg._conf("MC_TEST_URL") is None,
      "mc_guard.env: без файла значения обязаны быть нейтральными")
mg._LOCAL_ENV_PATH = _saved_env_path
mg._reset_local_env()
shutil.rmtree(_env_dir, ignore_errors=True)


# ── профильный вывод: kimi — текстом, claude — JSON; служебное — всегда JSON ─
# Kimi Code читает JSON только у permissionDecision; additionalContext не понимает:
# простой stdout хука (exit 0) показывается пользователю как есть. Поэтому в профиле
# kimi ЛЮБОЕ информационное сообщение (только hookEventName + additionalContext)
# уходит текстом на любом событии, в профиле claude — JSON-обёрткой, а служебные
# ответы (permissionDecision, updatedInput) — JSON в обоих профилях.
# Прогон — процессом, как хук зовёт клиент; REST заглушён на несуществующий порт.
tmp_txt = pathlib.Path(tempfile.mkdtemp(prefix="mctext_"))
(tmp_txt / "env").write_text("MC_API_KEY=test\n", encoding="utf-8")
env_txt = _hook_env(tmp_txt, MC_API_URL="http://127.0.0.1:9", MC_ENV_FILE=str(tmp_txt / "env"))
_guard_py = str(pathlib.Path(__file__).with_name("mc_guard.py"))


def _run_hook(cmd, event, argv=()):
    proc = subprocess.run([sys.executable, _guard_py, cmd] + list(argv),
                          input=json.dumps(event, ensure_ascii=False).encode("utf-8"),
                          capture_output=True, timeout=60, env=env_txt)
    return (proc.returncode,
            proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"))


def _info_context(out, what):
    """Текст additionalContext информационного сообщения — по профилю клиента."""
    if PROFILE == "kimi":
        check("hookSpecificOutput" not in out,
              "%s: в профиле kimi сообщение ушло JSON-блобом: %r" % (what, out[:120]))
        return out
    try:
        ctx = (json.loads(out or "{}").get("hookSpecificOutput") or {}).get("additionalContext")
    except ValueError:
        ctx = None
    check(ctx is not None,
          "%s: в профиле claude сообщение ушло не JSON с additionalContext: %r" % (what, out[:120]))
    return ctx or ""


_rc, _txt, _err = _run_hook("freshness", {"session_id": "plain-fresh", "prompt": "проверка"})
check(_rc == 0 and "memory-compiler" in _info_context(_txt, "UserPromptSubmit"),
      "профильный вывод: UserPromptSubmit без правила: %r" % _txt[:120])

_rc, _txt, _err = _run_hook("session_start", {"session_id": "plain-start"})
check(_rc == 0 and "ОБЯЗАТЕЛЬНО" in _info_context(_txt, "SessionStart"),
      "профильный вывод: SessionStart пуст: %r" % _txt[:120])

# compact (PostCompact): напоминание про save_compact доезжает в обоих профилях.
_rc, _txt, _err = _run_hook("compact", {"session_id": "plain-compact"})
_ctx_compact = _info_context(_txt, "PostCompact")
check(_rc == 0 and "save_compact" in _ctx_compact and "route_project" in _ctx_compact,
      "compact: напоминание про save_compact не пришло: %r" % _txt[:120])

# permissionDecision гейта не должен превратиться в текст: это служебный ответ.
_rc, _txt, _err = _run_hook("gate", {"session_id": "plain-gate", "hook_event_name": "PreToolUse",
                                     "tool_name": "mcp__mikrotik__mikrotik_get_interfaces",
                                     "tool_input": {}})
try:
    _gate_json = json.loads(_txt)
except ValueError:
    _gate_json = {}
check(_rc == 0
      and ((_gate_json.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"),
      "профильный вывод: PreToolUse с permissionDecision перестал быть JSON: %r" % _txt[:120])

# PostToolUseFailure — наблюдательное событие, но текст всё равно показывается.
_rc, _txt, _err = _run_hook("fail", {"session_id": "plain-fail", "tool_use_id": "toolu_PF",
                                     "tool_name": "mcp__memory-compiler__save_lesson",
                                     "tool_input": {"topic": "Т", "content": "Тело",
                                                    "project": "general"},
                                     "error": "MCP error -32001: Request timed out"})
check(_rc == 0 and "ПОВТОРИТЬ" in _info_context(_txt, "PostToolUseFailure"),
      "профильный вывод: PostToolUseFailure пуст: %r" % _txt[:120])

# Блок Stop — главная развилка профилей: kimi — exit 2 и причина текстом в stderr
# (JSON в stdout Kimi показал бы сырым блобом), claude — JSON decision в stdout, rc 0.
_st_dir = _state_dir(tmp_txt)
_st_dir.mkdir(parents=True, exist_ok=True)
(_st_dir / "plain-stop.json").write_text(json.dumps({"did_write": True}), encoding="utf-8")
_rc, _txt, _err = _run_hook("stop", {"session_id": "plain-stop", "transcript_path": ""})
if PROFILE == "kimi":
    check(_rc == 2 and "finish_task" in _err,
          "блок Stop: нет exit 2 с причиной в stderr: rc=%r err=%r" % (_rc, _err[:120]))
    check(not _txt.strip(),
          "блок Stop: в stdout уехал JSON — Kimi покажет его сырым блобом: %r" % _txt[:120])
else:
    try:
        _stop_json = json.loads(_txt or "{}")
    except ValueError:
        _stop_json = {}
    check(_rc == 0 and _stop_json.get("decision") == "block"
          and "finish_task" in str(_stop_json.get("reason") or ""),
          "блок Stop: нет JSON decision=block при rc 0: rc=%r out=%r" % (_rc, _txt[:120]))
    check(not _err.strip(), "блок Stop: в stderr уехала причина в профиле claude: %r" % _err[:120])

# ── nul_guard: редирект в зарезервированное имя Windows ──────────────────────
# Блок — JSON permissionDecision=deny в ОБОИХ профилях (это служебный ответ).
for _cmd in ("echo x > nul", "type f > con", "cmd 2>aux.txt"):
    _rc, _txt, _err = _run_hook("nul_guard", {
        "session_id": "nul-d", "hook_event_name": "PreToolUse", "tool_name": "Bash",
        "tool_input": {"command": _cmd}})
    try:
        _deny = (json.loads(_txt or "{}").get("hookSpecificOutput") or {}).get("permissionDecision")
    except ValueError:
        _deny = None
    check(_rc == 0 and _deny == "deny",
          "nul_guard: не заблокирован редирект %r (профиль %s): %r" % (_cmd, PROFILE, _txt[:100]))

for _cmd in ('echo "> nul в кавычках"',
             "cat > f.md <<'EOF'\nпиши > nul вот так\nEOF",
             "echo ok > out.txt"):
    _rc, _txt, _err = _run_hook("nul_guard", {
        "session_id": "nul-p", "hook_event_name": "PreToolUse", "tool_name": "Bash",
        "tool_input": {"command": _cmd}})
    check(_rc == 0 and not _txt.strip(),
          "nul_guard: ложный блок на %r: %r" % (_cmd, _txt[:100]))

# На PowerShell тоже срабатывает, на Read — не срабатывает.
_rc, _txt, _err = _run_hook("nul_guard", {
    "session_id": "nul-ps", "hook_event_name": "PreToolUse", "tool_name": "PowerShell",
    "tool_input": {"command": "echo x > nul"}})
check("deny" in _txt, "nul_guard: на PowerShell не сработал: %r" % _txt[:100])
_rc, _txt, _err = _run_hook("nul_guard", {
    "session_id": "nul-rd", "hook_event_name": "PreToolUse", "tool_name": "Read",
    "tool_input": {"file_path": "nul"}})
check(_rc == 0 and not _txt.strip(), "nul_guard: вмешался в Read: %r" % _txt[:100])

shutil.rmtree(tmp_txt, ignore_errors=True)

# ── нейтральные дефолты без машинно-специфичных значений ─────────────────────
# Репо публичный: без mc_guard.env и переменных окружения скрипт обязан встать на
# loopback и отсутствие KNOWLEDGE_DIR (свежесть/счётчики молча деградируют).
# Проверка — процессом на КОПИИ скрипта: дефолты читаются при импорте.
_env2 = pathlib.Path(tempfile.mkdtemp(prefix="mcdef_"))
_guard_copy = _env2 / "mc_guard.py"
shutil.copyfile(str(pathlib.Path(__file__).with_name("mc_guard.py")), _guard_copy)
_PROBE = ("import importlib.util,sys;"
          "spec=importlib.util.spec_from_file_location('mg',sys.argv[1]);"
          "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
          "print(m.API_URL);print(m.KNOWLEDGE_DIR);print(m.ENV_FILE)")
_env_clean = {k: v for k, v in _hook_env(_env2).items()
              if k not in ("MC_API_URL", "MC_KNOWLEDGE_DIR", "MC_ENV_FILE")}
proc_d = subprocess.run([sys.executable, "-c", _PROBE, str(_guard_copy)],
                        capture_output=True, timeout=60, env=_env_clean)
_lines = proc_d.stdout.decode("utf-8", errors="replace").splitlines()
check(_lines[:3] == ["http://127.0.0.1:8765", "None", "None"],
      "дефолты: без env и mc_guard.env должны быть 127.0.0.1/None/None: %r" % _lines)
(_env2 / "mc_guard.env").write_text(
    "MC_API_URL=http://127.0.0.1:9999\nMC_KNOWLEDGE_DIR=%s\n"
    % str(_env2 / "knowledge").replace("\\", "/"), encoding="utf-8")
proc_d = subprocess.run([sys.executable, "-c", _PROBE, str(_guard_copy)],
                        capture_output=True, timeout=60, env=_env_clean)
_lines = proc_d.stdout.decode("utf-8", errors="replace").splitlines()
check(len(_lines) >= 2 and _lines[0] == "http://127.0.0.1:9999"
      and _lines[1].endswith("knowledge"),
      "дефолты: mc_guard.env рядом со скриптом не подхвачен: %r" % _lines)
proc_d = subprocess.run([sys.executable, "-c", _PROBE, str(_guard_copy)],
                        capture_output=True, timeout=60,
                        env=dict(_env_clean, MC_API_URL="http://127.0.0.1:7777"))
_lines = proc_d.stdout.decode("utf-8", errors="replace").splitlines()
check(_lines and _lines[0] == "http://127.0.0.1:7777",
      "дефолты: os.environ обязан быть выше mc_guard.env: %r" % _lines)
shutil.rmtree(_env2, ignore_errors=True)


# ── install.py: генератор конфигов и установщик ─────────────────────────────
# Фикстурные settings.json/config.toml во временных каталогах; install.py
# импортируется модулем (main под __main__), пути передаются параметрами.
spec_i = importlib.util.spec_from_file_location(
    "mc_guard_install", str(pathlib.Path(__file__).with_name("install.py")))
inst = importlib.util.module_from_spec(spec_i)
spec_i.loader.exec_module(inst)

inst_dir = pathlib.Path(tempfile.mkdtemp(prefix="mcinst_"))
_script_c = inst_dir / "claude" / "hooks" / "mc_guard.py"
_script_k = inst_dir / "kimi" / "hooks" / "mc_guard.py"
_settings_fx = inst_dir / "settings.json"
_config_fx = inst_dir / "config.toml"

_settings_fx.write_text(json.dumps({
    "env": {"SOME_FLAG": "1"},
    "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "grep -Eqi 'nul' && exit 2", "shell": "bash",
         "timeout": 5}]}],
              "PostCompact": [{"hooks": [{"type": "command",
                                          "command": "echo '{inline-legacy}'", "timeout": 5}]}]},
    "language": "russian",
}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

# (а) settings.json: hooks заменены, прочие ключи сохранены
_new_settings = inst.build_claude_settings(_settings_fx.read_text(encoding="utf-8"), _script_c)
_parsed = json.loads(_new_settings)
check(_parsed.get("language") == "russian" and _parsed.get("env") == {"SOME_FLAG": "1"},
      "install: settings.json потерял прочие ключи")
_hooks_json = json.dumps(_parsed.get("hooks"), ensure_ascii=False)
check("nul_guard" in _hooks_json and "grep -Eqi" not in _hooks_json
      and "inline-legacy" not in _hooks_json,
      "install: старые grep-nul и inline-PostCompact не вытеснены nul_guard/compact")
check(sum(len(v) for v in _parsed["hooks"].values()) == len(inst.HOOKS),
      "install: в settings.json не все записи хуков")
check("--client=claude" in _hooks_json and "startup|clear|compact" in _hooks_json,
      "install: в settings.json нет --client=claude или SessionStart matcher")
_pre = _parsed["hooks"]["PreToolUse"]
check(all("shell" in h["hooks"][0] and "timeout" in h["hooks"][0] for h in _pre)
      and any(h.get("matcher") is None for h in _parsed["hooks"]["PostCompact"]),
      "install: формат записи Claude (shell/timeout/matcher) не соблюдён")

# (б) config.toml: маркерный блок заменяет секцию mc_guard, чужое — байт-в-байт
_DAYZ = ("# ------------------------------------------------- dayz-хуки\n"
         "[[hooks]]\nevent = \"PreToolUse\"\nmatcher = 'Bash'\n"
         "command = 'python \"X:/dayz/guard_git.py\"'\ntimeout = 15\n")
_OLD_MCG = ("# Заголовок секции mc_guard, копия хука: hooks/mc_guard.py\n"
            "[[hooks]]\nevent = \"Stop\"\n"
            "command = 'python \"X:/old/mc_guard.py\" stop'\ntimeout = 10\n\n")
_config_fx.write_text('default_model = "demo"\n\n' + _OLD_MCG + "\n" + _DAYZ, encoding="utf-8")
_new_cfg = inst.build_kimi_config(_config_fx.read_text(encoding="utf-8"), _script_k)
check(inst.MARK_BEGIN in _new_cfg and inst.MARK_END in _new_cfg,
      "install: config.toml не получил маркированный блок")
check('default_model = "demo"' in _new_cfg and _DAYZ in _new_cfg,
      "install: config.toml потерял чужие ключи или dayz-секцию")
check("X:/old/mc_guard.py" not in _new_cfg and "Заголовок секции mc_guard" not in _new_cfg,
      "install: старая секция mc_guard не вытеснена маркированным блоком")
check("--client=kimi" in _new_cfg and "manual|auto" in _new_cfg
      and "startup|resume" in _new_cfg,
      "install: в config.toml нет --client=kimi / PostCompact matcher / SessionStart matcher")
check(_new_cfg.index(inst.MARK_END) < _new_cfg.index("dayz-хуки"),
      "install: маркированный блок должен стоять перед dayz-секцией")

# Повторная генерация по маркерам — тот же текст (идемпотентность генератора).
check(inst.build_kimi_config(_new_cfg, _script_k) == _new_cfg,
      "install: config.toml не идемпотентен (повтор по маркерам)")
# Ни маркеров, ни mc_guard-секции: вставка перед dayz.
_cfg_no_mcg = 'default_model = "demo"\n\n' + _DAYZ
_ins = inst.build_kimi_config(_cfg_no_mcg, _script_k)
check(inst.MARK_BEGIN in _ins and _ins.index(inst.MARK_END) < _ins.index("dayz-хуки")
      and _ins.endswith(_DAYZ.rstrip("\n") + ("\n" if _DAYZ.endswith("\n") else "")),
      "install: вставка блока без существующей секции сломана")
# Совсем без dayz — блок в конец файла.
_ins2 = inst.build_kimi_config('default_model = "demo"\n', _script_k)
check(_ins2.startswith('default_model = "demo"') and inst.MARK_BEGIN in _ins2,
      "install: вставка блока в конец файла сломана")

# (в/г/д) install_client на фикстурах: запись, идемпотентность, env, dry-run
_hooks_c = inst_dir / "c-hooks"
_hooks_k = inst_dir / "k-hooks"
_settings_i = inst_dir / "i-settings.json"
_config_i = inst_dir / "i-config.toml"
shutil.copyfile(_settings_fx, _settings_i)
shutil.copyfile(_config_fx, _config_i)
_env_vals = {"MC_API_URL": "http://127.0.0.1:8765", "MC_KNOWLEDGE_DIR": "X:/repo/knowledge",
             "MC_ENV_FILE": "X:/repo/.env", "MC_API_KEY": "test-key"}

_rep = inst.install_client("claude", guard_src=inst.GUARD_SRC, hooks_dir=_hooks_c,
                           config_path=_settings_i, env_values=dict(_env_vals), report=[])
check((_hooks_c / "mc_guard.py").read_bytes() == inst.GUARD_SRC.read_bytes(),
      "install: копия mc_guard.py не байт-в-байт")
check((_hooks_c / "mc_guard.env").exists(), "install: mc_guard.env не записан")
check("test-key" in (_hooks_c / "mc_guard.env").read_text(encoding="utf-8"),
      "install: в записанном mc_guard.env нет ключа")
check(json.loads(_settings_i.read_text(encoding="utf-8"))["hooks"],
      "install: settings.json после установки без hooks")
check(any(".bak-" in p.name for p in inst_dir.iterdir()),
      "install: не создан бэкап конфига при первой записи")

# Идемпотентность: повторный прогон ничего не меняет и не делает новых бэкапов.
_baks = [p.name for p in inst_dir.iterdir() if ".bak-" in p.name]
_rep2 = inst.install_client("claude", guard_src=inst.GUARD_SRC, hooks_dir=_hooks_c,
                            config_path=_settings_i, env_values=dict(_env_vals), report=[])
check(any("без изменений" in line or "совпадает" in line for line in _rep2),
      "install: повторный прогон не распознан как «без изменений»")
check([p.name for p in inst_dir.iterdir() if ".bak-" in p.name] == _baks,
      "install: идемпотентный прогон создал лишний бэкап")

# (г) mc_guard.env не затирается без --force-env
(_hooks_c / "mc_guard.env").write_text("MC_API_KEY=handmade\n", encoding="utf-8")
inst.install_client("claude", guard_src=inst.GUARD_SRC, hooks_dir=_hooks_c,
                    config_path=_settings_i, env_values=dict(_env_vals), report=[])
check((_hooks_c / "mc_guard.env").read_text(encoding="utf-8") == "MC_API_KEY=handmade\n",
      "install: mc_guard.env затёрт без --force-env")
_rep3 = inst.install_client("claude", guard_src=inst.GUARD_SRC, hooks_dir=_hooks_c,
                            config_path=_settings_i, env_values=dict(_env_vals),
                            force_env=True, report=[])
check("test-key" in (_hooks_c / "mc_guard.env").read_text(encoding="utf-8"),
      "install: --force-env не перезаписал mc_guard.env")

# (д) --dry-run не пишет ничего: ни конфиг, ни копию, ни env, ни бэкапы
_dry_dir = inst_dir / "dry"
_dry_dir.mkdir()
_dry_settings = _dry_dir / "settings.json"
_dry_hooks = _dry_dir / "hooks"
shutil.copyfile(_settings_fx, _settings_i)   # возвращаем «старый» вариант конфига
_dry_settings.write_text(_settings_fx.read_text(encoding="utf-8"), encoding="utf-8")
_before = _dry_settings.read_bytes()
_rep4 = inst.install_client("claude", guard_src=inst.GUARD_SRC, hooks_dir=_dry_hooks,
                            config_path=_dry_settings, env_values=dict(_env_vals),
                            dry_run=True, report=[])
check(_dry_settings.read_bytes() == _before and not _dry_hooks.exists()
      and not [p for p in _dry_dir.iterdir() if ".bak-" in p.name],
      "install: dry-run что-то записал")
check(any("БУДЕТ" in line for line in _rep4),
      "install: dry-run не показал предстоящие изменения")
_env_shown = "\n".join(_rep4)
check("test-key" not in _env_shown, "install: dry-run показал реальный MC_API_KEY")

# kimi-клиент install_client тоже проходит на фикстуре
_k_settings = inst_dir / "k-config.toml"
_k_settings.write_text(_config_fx.read_text(encoding="utf-8"), encoding="utf-8")
inst.install_client("kimi", guard_src=inst.GUARD_SRC, hooks_dir=_hooks_k,
                    config_path=_k_settings, env_values=dict(_env_vals), report=[])
check(inst.MARK_BEGIN in _k_settings.read_text(encoding="utf-8")
      and (_hooks_k / "mc_guard.py").exists(),
      "install: установка kimi-клиента на фикстуре сломана")

# Наследование MC_API_URL из ранее установленной копии (у прежних копий адрес
# был зашит в коде, а в .env репозитория его нет — без наследования REST деградирует
# до loopback-заглушки при первой установке).
_inh_dir = inst_dir / "inh"
_inh_dir.mkdir()
(_inh_dir / "mc_guard.py").write_text(
    'API_URL = (os.environ.get("MC_API_URL") or "http://203.0.113.9:8765").rstrip("/")\n',
    encoding="utf-8")
check(inst._inherit_api_url(_inh_dir / "mc_guard.py") == "http://203.0.113.9:8765",
      "install: наследование MC_API_URL из старой копии сломано")
check(inst._inherit_api_url(_inh_dir / "нет-такого.py") is None,
      "install: наследование обязано молча деградировать без старой копии")
_vals_nr, _miss_nr = inst.default_env_values(repo_root=_inh_dir)   # без .env вовсе
check("MC_API_URL" in _miss_nr and "MC_API_KEY" in _miss_nr,
      "install: без .env репозитория предупреждение обязано назвать оба ключа")
check(_vals_nr.get("MC_API_URL") == "http://127.0.0.1:8765" and "MC_API_KEY" not in _vals_nr,
      "install: нейтральные дефолты env-значений сломаны")

shutil.rmtree(inst_dir, ignore_errors=True)


if fails:
    print("ОШИБКИ:")
    print("\n".join(" - " + f for f in fails))
    sys.exit(1)
print("mc_guard OK [%s]: %d команд, очередь, аудит-сверка, Stop-лимит, профили, nul_guard"
      % (PROFILE, len(BLOCK) + len(PASS)))
