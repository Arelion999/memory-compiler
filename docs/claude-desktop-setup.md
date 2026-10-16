# Настройка memory-compiler в Claude Code Desktop

[English](claude-desktop-setup.en.md) · **Русский**

Интеграция через MCP + скил memory-autopilot. Рассчитано на Windows (пути через `C:\Users\<user>`), но работает и на macOS/Linux (замените пути).

## Содержание

1. [Запуск сервера](#1-запуск-сервера)
2. [Подключение MCP](#2-подключение-mcp)
3. [Установка скила memory-autopilot](#3-установка-скила-memory-autopilot)
4. [Глобальный CLAUDE.md](#4-глобальный-claudemd)
5. [Hooks (страховка)](#5-hooks-страховка)
6. [Проверка работы](#6-проверка-работы)
7. [Настройка зависимостей проектов](#7-настройка-зависимостей-проектов)
8. [Ограничения Claude Desktop](#8-ограничения-claude-desktop)

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

В `claude_desktop_config.json` добавить сервер:

```json
{
  "mcpServers": {
    "memory-compiler": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "http://<host>:8765/mcp",
        "--allow-http",
        "--header",
        "X-Api-Key:<MC_API_KEY>",
        "--transport",
        "http-only"
      ]
    }
  }
}
```

**Важно:**
- Ключ передаётся заголовком `X-Api-Key`, в значении нет пробела. На Windows Desktop запускает `mcp-remote` через `cmd.exe`, и пробел в `Authorization: Bearer <ключ>` разрывает команду.
- `--transport http-only` — Streamable HTTP без отката на SSE. По умолчанию `mcp-remote` при ответе 404 или 405 откатывается на устаревший SSE-транспорт.
- `--allow-http` нужен только для незашифрованного соединения (локалка).
- Старые конфиги с `/sse?key=…` и `--transport sse-only` продолжают работать, но переводить их стоит на `/mcp`. SSE-клиент, потеряв поток, переподключается сам и не повторяет `initialize`: до v1.90.1 после этого каждый вызов падал с `-32602 Invalid request parameters`.

---

## 3. Установка скила memory-autopilot

Скил автоматизирует весь цикл работы с памятью: ищет контекст, определяет проект, выбирает tool, сохраняет результат. Пользователь не думает о памяти — скил делает всё сам.

**Установка:**
```bash
# Из репо memory-compiler
mkdir -p ~/.claude/skills/memory-autopilot
cp -R skills/memory-autopilot/. ~/.claude/skills/memory-autopilot/
```

**Что делает скил:**
- Автоматически триггерится на задачи, факты, вопросы, ошибки
- Определяет проект через `route_project(text, cwd)` и список проектов сервера
- Выбирает правильный tool по дереву решений (save_lesson / save_decision / save_runbook / save_from_template / save_secret)
- Вызывает start_task в начале и finish_task в конце
- Ищет креды и контекст в базе без вопросов пользователю

**Настройка проектов:** создайте проекты через `add_project` или первую запись, передавайте рабочий каталог в `route_project`. Таблицы маппинга в скиле нет; редактировать его под проекты не нужно.

---

## 4. Глобальный CLAUDE.md

Путь: `~/.claude/CLAUDE.md`

Минимальный шаблон (скил берёт основную работу на себя):

```markdown
# Глобальные правила

## Память (memory-compiler MCP)

Управление памятью автоматизировано через скил `memory-autopilot`.
Скил сам вызывает start_task, finish_task, выбирает tool и проект.

**ЗАПРЕЩЕНО** использовать mcp__memory (create_entities, add_observations и т.д.) — устаревшая система.
Для ВСЕХ операций с памятью — ТОЛЬКО memory-compiler tools (mcp__memory-compiler__*).
```

---

## 5. Hooks (страховка)

Установите сторож из репозитория, из его корня:

```bash
python hooks/install.py --client=claude --dry-run
python hooks/install.py --client=claude
```

Установщик копирует `mc_guard.py` в `~/.claude/hooks/`, делает бэкап
`~/.claude/settings.json` и заменяет в нём секцию `hooks`. Если у вас есть
свои хуки, сохраните их и объедините с результатом установки. Проверьте
`~/.claude/hooks/mc_guard.env`: `MC_API_URL`, `MC_API_KEY`, при необходимости
`MC_KNOWLEDGE_DIR`. Эти машинные значения не коммитятся в репозиторий.

Сторож загружает стартовый контекст, передаёт id чата серверу, напоминает
о чужих записях, требует чтения базы перед инфраструктурными вызовами,
ведёт очередь записей и доставляет рефлексы. `Stop` проверяет состояние
записей; `PostCompact` напоминает сохранить контекст через `save_compact`.
Подробности: [hooks/README.md](../hooks/README.md).

---

## 6. Проверка работы

После установки **перезапустите** Claude Code Desktop.

1. **Скил загружен:**
   В списке скилов должен появиться `memory-autopilot`.

2. **MCP доступен:**
   Скажите "проверь доступность базы знаний" — скил должен вызвать `list_projects`.

3. **Пробный цикл:**
   - Скажите факт: "сервер X на площадке Y" — скил должен сохранить через `save_lesson`
   - Поставьте задачу: "настрой nginx для нового сайта" — скил должен вызвать `start_task`, подтянуть контекст, а в конце `finish_task`

---

## 7. Настройка зависимостей проектов

`start_task` автоматически подтягивает контекст из зависимых проектов:

```python
set_project_deps(project="client-a", depends_on=["work", "infra"])
set_project_deps(project="myapp", depends_on=["infra", "work"])
```

---

## 8. Ограничения Claude Desktop

| Функция | Статус | Комментарий |
|---------|--------|-------------|
| MCP tools (все 50) | ✅ | Полная поддержка |
| Скил memory-autopilot | ✅ | Автотриггер по description |
| Hooks mc_guard | ✅ | Гейт, очередь, свежесть, рефлексы, compact |
| Авто-search_error при traceback | ✅ | Через скил (Фаза 0) |
| Авто-git_capture после commit | ❌ | Нет FileChanged в Desktop |

---

## Диагностика

**Скил не триггерится?**
- Убедитесь что `~/.claude/skills/memory-autopilot/SKILL.md` существует
- Перезапустите Desktop
- Проверьте что скил есть в списке (описание начинается с "Use ALWAYS")

**MCP tool недоступен?**
- Проверьте сервер: `curl http://<host>:8765/api/health`
- Проверьте ключ в `--header X-Api-Key:<ключ>`
- Логи Desktop: `%APPDATA%\Claude\logs\`

**Stop hook блокирует?**
- Проверьте `~/.claude/hooks/mc_hooks.log` и очередь `~/.claude/hooks/pending`: повторите или исправьте неуспешную запись.
- После решения задачи вызовите `finish_task`; не считайте сообщение об ошибке успешной записью.
