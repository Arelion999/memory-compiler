# Настройка memory-compiler в Claude Code Desktop

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

## 3. Установка скила memory-autopilot

Скил автоматизирует весь цикл работы с памятью: ищет контекст, определяет проект, выбирает tool, сохраняет результат. Пользователь не думает о памяти — скил делает всё сам.

**Установка:**
```bash
# Из репо memory-compiler
mkdir -p ~/.claude/skills/memory-autopilot
cp skills/memory-autopilot/SKILL.md ~/.claude/skills/memory-autopilot/SKILL.md
```

**Что делает скил:**
- Автоматически триггерится на задачи, факты, вопросы, ошибки
- Определяет проект по контексту (таблица маппинга внутри)
- Выбирает правильный tool по дереву решений (save_lesson / save_decision / save_runbook / save_from_template / save_secret)
- Вызывает start_task в начале и finish_task в конце
- Ищет креды и контекст в базе без вопросов пользователю

**Настройка проектов:** отредактируйте таблицу "Определение проекта" в SKILL.md под свои проекты.

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

Скил memory-autopilot заменяет большинство хуков. Оставьте только два:

```json
{
  "hooks": {
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "echo '{\"hookSpecificOutput\":{\"hookEventName\":\"Stop\",\"decision\":\"block\",\"reason\":\"СТОП. Была ли решена нетривиальная задача? Если ДА и НЕ вызвал memory-compiler:finish_task — ВЫЗОВИ СЕЙЧАС.\"}}'",
        "timeout": 5
      }]
    }],
    "PostCompact": [{
      "hooks": [{
        "type": "command",
        "command": "echo '{\"hookSpecificOutput\":{\"hookEventName\":\"PostCompact\",\"additionalContext\":\"КОНТЕКСТ БЫЛ СЖАТ. Если были нетривиальные решения которые не записаны — вызови memory-compiler:finish_task сейчас.\"}}'",
        "timeout": 5
      }]
    }]
  }
}
```

- **Stop** — страховка: блокирует завершение сессии если finish_task не был вызван
- **PostCompact** — напоминание при сжатии контекста

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
| MCP tools (все 38) | ✅ | Полная поддержка |
| Скил memory-autopilot | ✅ | Автотриггер по description |
| Hooks Stop/PostCompact | ✅ | Страховка |
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
- Проверьте `MC_API_KEY` в URL (URL-кодирование спецсимволов)
- Логи Desktop: `%APPDATA%\Claude\logs\`

**Stop hook блокирует?**
- Claude должен вызвать finish_task чтобы разблокировать
- Если застрял — временно закомментируйте Stop hook
