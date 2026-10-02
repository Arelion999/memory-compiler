# Kimi Work (daimon): детект клиента падает в claude-fallback + Bash-хук вызывает nul_guard вместо gate

**Репозиторий:** Arelion999/memory-compiler
**Проверено на:** HEAD `f95877f` (2026-10-01, «docs: установка в Kimi Work — плагин со всем целиком»), рабочее дерево без локальных правок.
**Окружение:** Kimi Work desktop (daemon daimon), сервер memory-compiler 1.96.0, установка плагина по свежей инструкции из того же коммита.

---

## Баг 1 — клиент определяется как claude, журнал уходит в `~/.claude/hooks/`

**Что происходит.** Все хуки манифеста генерируются с аргументом `--client=kimi` (`install.py:111`), но `main()` в `mc_guard.py` вырезает этот аргумент **до** вызова `_ensure_client(event)`:

```python
# mc_guard.py:2949-2954
argv = []
for arg in sys.argv[1:]:
    if arg.startswith("--client="):
        continue                      # <-- значение теряется здесь
    argv.append(arg)
...
event = read_event()
_ensure_client(event)                 # <-- а детект вызывается уже без него
```

Цепочка детекта (`detect_client`, mc_guard.py:114–138): argv (пусто) → env `MC_GUARD_CLIENT` (пусто) → `event.client_type` (daimon в событии не передаёт) → эвристика → **fallback claude**. Итог: `_apply_client` настраивает `~/.claude/hooks/mc_hooks.log`, хуки Kimi Work пишут журнал не в тот профиль, фрешность-счётчики живут отдельно от реального клиента.

Тот же дефект у подкоманды `stats` — `_ensure_client({})` вызывается с пустым событием (mc_guard.py:2963).

**Воспроизведение.** Установить хуки в Kimi Work, отправить любое сообщение → записей в `~/.kimi-code/hooks/` нет, всё падает в `~/.claude/hooks/mc_hooks.log` с событием `client.fallback`.

**Предлагаемый фикс** — запоминать значение при вырезании:

```python
# mc_guard.py, main()
for arg in sys.argv[1:]:
    if arg.startswith("--client="):
        os.environ.setdefault("MC_GUARD_CLIENT", arg.split("=", 1)[1])
        continue
    argv.append(arg)
```

`detect_client()` подхватит его по env-ветке. Минимально инвазивно, ничего не ломает в других клиентах.

---

## Баг 2 — Bash-команды не проходят gate в Kimi Work

**Что происходит.** В `KIMIWORK_HOOKS` (`install.py:81-89`) Bash-матчер повешен на `nul_guard`:

```python
# install.py:84
("PreToolUse", r"Bash|.*PowerShell.*", "nul_guard", 5),
```

Рядом MCP-инструменты на чужое железо гейтятся полноценным `gate` (install.py:85), и в claude-профиле аналогичный матчер вызывает именно `gate`. Получается: в Claude исполнение Bash на живое железо без свежего чтения базы блокируется, а в Kimi Work — нет. Похоже на недоперенос из claude-профиля при допиливании Kimi Work-установки во вчерашнем коммите.

**Фикс:**

```python
("PreToolUse", r"Bash|.*PowerShell.*", "gate", 10),
```

(либо добавить второй хук с `gate` поверх `nul_guard`, если NUL-guard тоже нужен на Bash — но судя по составу CLAUDE-хуков, gate там и есть основной.)

---

## Как проверили / workaround

Оба фикса применены локально и проверены живыми прогонами: ssh на хост без свежего чтения базы блокируется («СТОП: за последние 15 минут база не читалась»), после вызова `search` — `gate.pass` в `~/.kimi-code/hooks/mc_hooks.log`, журнал идёт в правильный профиль. Патч живёт только в локальном плагине и будет затираться при каждом обновлении скрипта из апстрима — поэтому и пишем.

Спасибо за memory-compiler, хук-механика с рефлексами — отличная штука.
