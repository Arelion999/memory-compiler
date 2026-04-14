# memory-compiler

MCP-сервер базы знаний для AI-ассистентов. 34 tools, Python, Docker.

## Структура

```
memory_compiler/
  config.py    — константы, метаданные
  search.py    — Whoosh BM25F + semantic search
  storage.py   — статьи, git, утилиты
  handlers.py  — реализация MCP tools
  tools.py     — регистрация + диспетчер
  api.py       — REST endpoints, Starlette
  ui.py        — Web UI HTML
```

## Команды

```bash
pytest tests/ -v          # тесты (37)
python server.py          # локальный запуск
docker-compose up -d      # деплой
```

## Деплой на NAS

Код монтируется через SynologyDrive volume → docker restart применяет изменения.
Перезапуск: paramiko SSH → `sudo /usr/local/bin/docker restart memory-compiler-mcp`

## Память (memory-compiler MCP)

**ПЕРЕД нетривиальной задачей** (баг, доработка, настройка, интеграция, деплой):
- Вызови `memory-compiler:start_task(topic="описание задачи")`

**ПОСЛЕ решения задачи:**
- Вызови `memory-compiler:finish_task(topic, content, project)`

Это НЕ опционально. Не начинай работу без start_task. Не завершай без finish_task.
