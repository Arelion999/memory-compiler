# Настройка memory-compiler в Claude Desktop

Полноценная интеграция: MCP + hooks + правила выбора проекта/tool. Рассчитано на Windows (пути через `C:\Users\<user>`), но работает и на macOS/Linux (замените пути).

## Содержание

1. [Запуск сервера](#1-запуск-сервера)
2. [Подключение MCP](#2-подключение-mcp)
3. [Глобальный CLAUDE.md (правила)](#3-глобальный-claudemd-правила)
4. [Hooks (settings.json)](#4-hooks-settingsjson)
5. [Проверка работы](#5-проверка-работы)
6. [Настройка зависимостей проектов](#6-настройка-зависимостей-проектов)
7. [Ограничения Claude Desktop](#7-ограничения-claude-desktop)

---

## 1. Запуск сервера

**Локально (Docker):**
```bash
git clone https://github.com/Arelion999/memory-compiler.git
cd memory-compiler
cp .env.example .env
# Заполнить MC_API_KEY и MC_ENCRYPT_KEY в .env
docker-compose up -d
```

**На NAS (Synology):**
- Смонтировать репо в SynologyDrive
- Создать `.env` с `MC_API_KEY`, `MC_ENCRYPT_KEY` (опционально `GIT_REPOS_PATH`, `OBSIDIAN_VAULT_PATH`)
- Запустить через Container Manager или SSH: `docker-compose up -d`

**Проверка:**
```bash
curl http://localhost:8765/api/health
# {"status": "ok", ...}
```

---

## 2. Подключение MCP

Claude Desktop читает конфиг из своего sandbox:
```
C:\Users\<user>\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json
```

Добавить сервер в `mcpServers`:

```json
{
  "mcpServers": {
    "memory-compiler": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "http://<host>:8765/sse?key=<MC_API_KEY>",
        "--allow-http",
        "--transport",
        "sse-only"
      ]
    }
  }
}
```

**Важно:**
- Спецсимволы в ключе URL-кодируйте (`$` → `%24`)
- `--transport sse-only` обязателен (без него fallback вызывает таймауты)
- `--allow-http` нужен только для незашифрованного соединения (локалка)

---

## 3. Глобальный CLAUDE.md (правила)

Файл читается Claude при каждом старте сессии. Путь:
```
C:\Users\<user>\.claude\CLAUDE.md
```

Минимальный рабочий шаблон:

```markdown
# Глобальные правила

## Память (memory-compiler MCP)

**ПЕРЕД нетривиальной задачей** (баг, доработка, настройка, интеграция, деплой):
- Вызови `memory-compiler:start_task(topic="описание")`

**ПОСЛЕ решения задачи:**
- Вызови `memory-compiler:finish_task(topic, content, project)`

Это НЕ опционально. Не начинай работу без start_task. Не завершай без finish_task.

## Выбор проекта (при save/finish_task)

| Проект | Что туда |
|--------|----------|
| `infra` | Серверы, сеть, DNS, SSL, NAS, VPN |
| `<свой-продукт>` | Разработка собственного ПО |
| `<клиент>` | Работа по конкретному клиенту |
| `personal` | Личное |

Если сомневаешься — `search` или `get_context`, возьми project из похожего кейса.

## Какой tool выбрать

- **save_lesson** — дефолт: проблема → решение, факты, конфигурации
- **save_decision** — выбор между альтернативами: title, decision, alternatives, reasoning
- **save_runbook** — пошаговая инструкция с чекбоксами (деплой, настройка)
- **save_from_template** — шаблоны: bug, setup, 1c, deploy, integration
- **save_secret** — пароли, ключи, creds (AES-256)
- **ingest** — импорт из URL или текста
- **git_capture** — знания из git-коммитов

## Использование поиска

- В середине задачи когда нужен контекст → `get_context(project, query)`
- При ошибке → `search_error(error_text, project)` ПЕРВЫМ
- Обзор проекта → `get_summary(project)`
```

---

## 4. Hooks (settings.json)

Claude Desktop поддерживает **5 hooks** из `~/.claude/settings.json`:
- `SessionStart`, `UserPromptSubmit`, `Stop`, `PostToolUse`, `PostCompact`

**НЕ поддерживаются** в Desktop: PreToolUse, FileChanged, Notification, SubagentStart/Stop, PreCompact и др.

Полный рабочий пример (`C:\Users\<user>\.claude\settings.json`):

```json
{
  "hooks": {
    "SessionStart": [{
      "hooks": [{
        "type": "command",
        "command": "echo '{\"hookSpecificOutput\":{\"hookEventName\":\"SessionStart\",\"additionalContext\":\"MEMORY-COMPILER: База знаний доступна. При нетривиальных задачах — memory-compiler:start_task ПЕРЕД работой, memory-compiler:finish_task ПОСЛЕ.\"}}'",
        "timeout": 5
      }]
    }],
    "UserPromptSubmit": [{
      "hooks": [{
        "type": "command",
        "command": "echo '{\"hookSpecificOutput\":{\"hookEventName\":\"UserPromptSubmit\",\"additionalContext\":\"ПРАВИЛА memory-compiler (игнор если тривиально):\\n1) Нетривиальная задача — start_task(topic).\\n2) Нужны креды — search ПЕРВЫМ, не спрашивай.\\n3) Ошибка/traceback — search_error ПЕРВЫМ.\\n4) Выбор альтернатив — save_decision.\\n5) Пошаговая инструкция — save_runbook.\\n6) URL документации — ingest(url).\\n7) В КОНЦЕ задачи — finish_task.\"}}'",
        "timeout": 5
      }]
    }],
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "echo '{\"hookSpecificOutput\":{\"hookEventName\":\"Stop\",\"decision\":\"block\",\"reason\":\"СТОП. Была ли решена нетривиальная задача? Если ДА и НЕ вызвал memory-compiler:finish_task — ВЫЗОВИ СЕЙЧАС. Если тривиальная или уже записал — продолжай.\"}}'",
        "timeout": 5
      }]
    }],
    "PostToolUse": [{
      "matcher": "mcp__memory-compiler__(save_lesson|finish_task|save_decision|save_runbook|save_from_template|save_secret|ingest|import_obsidian|git_capture|edit_article)",
      "hooks": [{
        "type": "command",
        "command": "echo '{\"hookSpecificOutput\":{\"additionalContext\":\"Записано в базу знаний.\"}}'",
        "timeout": 3
      }]
    }],
    "PostCompact": [{
      "hooks": [{
        "type": "command",
        "command": "echo '{\"hookSpecificOutput\":{\"hookEventName\":\"PostCompact\",\"additionalContext\":\"КОНТЕКСТ СЖАТ. Если были решения которые не записаны — вызови finish_task.\"}}'",
        "timeout": 5
      }]
    }]
  }
}
```

**Ключевые моменты:**
- `Stop.decision: "block"` — блокирует завершение сессии пока Claude не запишет урок
- `PostToolUse.matcher` — regex, ловит все save_* tools
- `PostCompact` — напоминание при автоматическом сжатии контекста

---

## 5. Проверка работы

После сохранения конфигов **полностью перезапустить** Claude Desktop (tray → Quit → открыть снова). Горячая перезагрузка не подхватывает hooks.

В новой сессии проверить:

1. **system-reminder при отправке сообщения:**
   ```
   UserPromptSubmit hook additional context: ПРАВИЛА memory-compiler...
   ```
   Если видно — hook работает.

2. **MCP доступен:**
   ```
   Вызовите memory-compiler:list_projects
   ```
   Должен вернуть список проектов.

3. **Пробный цикл:**
   - Задать нетривиальную задачу
   - Claude должен вызвать `start_task` первым действием
   - После решения — `finish_task`
   - `Stop` hook не выпустит без записи

---

## 6. Настройка зависимостей проектов

`start_task` автоматически подтягивает контекст из зависимых проектов. Настройте:

```python
# Клиентские проекты зависят от общих знаний
set_project_deps(project="client-a", depends_on=["work", "infra"])
set_project_deps(project="client-b", depends_on=["work", "infra"])

# Веб-приложение зависит от инфраструктуры
set_project_deps(project="myapp", depends_on=["infra", "work"])

# Разработка продукта зависит от инфры
set_project_deps(project="memory-compiler", depends_on=["infra"])
```

Теперь `start_task(project="myapp", topic="...")` найдёт похожие кейсы в `infra` и `work` автоматически.

---

## 7. Ограничения Claude Desktop

| Хотелка | Работает в Desktop? | Почему |
|---------|---------------------|--------|
| start_task / finish_task / save_* | ✅ | MCP tools |
| Hooks SessionStart/UserPromptSubmit/Stop/PostToolUse/PostCompact | ✅ | Поддерживаются |
| Авто-search_error при traceback | ❌ | Нужен PreToolUseFailure (только CLI) |
| Авто-git_capture после commit | ❌ | Нужен FileChanged (только CLI) |
| PreToolUse блокировка опасных операций | ❌ | Только CLI |
| Desktop/CLI общий MCP config | ❌ | Раздельные файлы (`claude_desktop_config.json` vs `~/.claude.json`) |
| Env-substitution `${VAR}` в MCP config | ❌ | Только CLI (Desktop хранит plaintext) |

**Для максимума в Desktop** — настройте все 5 hooks + расширьте `matcher` на все save_* tools + зависимости проектов.

---

## Диагностика

**Hook не срабатывает?**
- Перезапустите Desktop (tray → Quit → снова)
- Проверьте `settings.json` на валидность JSON: `python -c "import json; json.load(open('...', encoding='utf-8'))"`
- Проверьте что `timeout` в пределах 1-60 сек

**MCP tool недоступен?**
- Проверьте сервер: `curl http://<host>:8765/api/health`
- Проверьте `MC_API_KEY` в URL (URL-кодирование спецсимволов)
- Логи Desktop: `%APPDATA%\Claude\logs\`

**Stop hook блокирует вечно?**
- Claude должен вызвать finish_task чтобы разблокировать
- Если застрял — временно закомментируйте `Stop` hook
