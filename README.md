# memory-compiler

Персональная база знаний для AI-ассистентов. MCP-сервер с гибридным поиском, автокомпиляцией дневных логов в wiki-статьи и веб-интерфейсом.

**2235 строк** | **16 MCP-инструментов** | **14 REST API endpoints** | **5 вкладок Web UI**

## Зачем

Claude Code, Claude Desktop и другие AI-ассистенты не помнят контекст между сессиями. memory-compiler решает эту проблему: каждое решение, баг, конфигурация сохраняются в markdown-статьях, индексируются и доступны через MCP или HTTP API. Ассистент ищет похожие кейсы перед задачей и записывает новые решения после.

## Архитектура

```
┌──────────────────┐     SSE/MCP      ┌─────────────────────┐
│  Claude Code     │ ◄──────────────► │  memory-compiler    │
│  Claude Desktop  │                  │  (Python + Starlette)│
│  Любой MCP-клиент│     HTTP API     │                     │
└──────────────────┘ ◄──────────────► │  :8765              │
                                      └────────┬────────────┘
┌──────────────────┐                           │
│  Web UI          │ ◄─────────────────────────┘
│  (встроенный)    │     GET/POST /api/*
└──────────────────┘
         │
         ▼
┌─────────────────────────────────────────────┐
│  knowledge/  (Docker volume)                │
│  ├── daily/          дневные логи           │
│  ├── project-a/      проект A               │
│  ├── project-b/      проект B               │
│  ├── ...             (настраиваемые)        │
│  ├── general/        общее                  │
│  ├── .whoosh_index/  BM25F индекс           │
│  ├── .embeddings.pkl semantic кэш           │
│  └── .article_meta.json  метрики доступа    │
└─────────────────────────────────────────────┘
```

## Стек

| Компонент | Технология |
|-----------|-----------|
| Сервер | Python 3.12, MCP SDK, Starlette, Uvicorn |
| Полнотекстовый поиск | Whoosh 2.7 (BM25F с весами полей) |
| Семантический поиск | sentence-transformers `paraphrase-multilingual-MiniLM-L12-v2` |
| Хранилище | Markdown-файлы + Git версионирование |
| Контейнеризация | Docker (опционально) |
| Транспорт MCP | SSE (Server-Sent Events) |

## MCP-инструменты (16 шт.)

### Поиск и чтение

| Инструмент | Описание |
|------------|----------|
| `search(query, project)` | Гибридный поиск BM25F + semantic с temporal decay |
| `ask(question, project)` | Q&A — вопрос на естественном языке, ответ с цитатами из статей |
| `search_by_tag(tag, project)` | Все статьи с указанным тегом |
| `read_article(project, filename)` | Полный текст статьи |
| `get_context(project, query)` | Контекст перед задачей — топ релевантных статей |
| `get_summary(project)` | Сжатая сводка проекта (~200 токенов) |

### Запись и редактирование

| Инструмент | Описание |
|------------|----------|
| `save_lesson(topic, content, project, tags)` | Сохранить/обновить статью с автомержем, автотегами, детекцией противоречий |
| `edit_article(project, filename, content, append)` | Заменить содержимое или дописать секцию |
| `delete_article(project, filename)` | Удалить статью из базы |

### Сессии и контекст

| Инструмент | Описание |
|------------|----------|
| `save_session(project, summary, decisions, open_questions)` | Сохранить контекст сессии для передачи следующей |
| `load_session(project)` | Загрузить контекст + уведомления об устаревших статьях |
| `get_active_context(project)` | Лента последних 10 действий по проекту |

### Обслуживание

| Инструмент | Описание |
|------------|----------|
| `compile(dry_run, project, since)` | Скомпилировать дневные логи в wiki-статьи |
| `lint(project, fix)` | Проверка здоровья: дубли, stale, пустые, теги |
| `reindex()` | Полная переиндексация BM25F + embeddings + index.md |
| `article_history(project, filename)` | Git-история изменений статьи |

## Поисковая система

Гибридный поиск из двух движков:

**BM25F (Whoosh)** — полнотекстовый поиск с весами полей:
- title: x5 (название статьи приоритетнее)
- tags: x3 (теги — вторые по важности)
- body: x1 (тело статьи)

**Semantic search** — векторные embeddings через `paraphrase-multilingual-MiniLM-L12-v2`:
- Поддержка русского и английского языков
- Статьи разбиваются по секциям `###` (чанкинг) для точного поиска
- Дедупликация: лучший score на статью, не на чанк

**Формула**: `score = (0.4 * BM25_normalized + 0.6 * semantic_similarity) * decay_factor`

**Temporal decay** — свежие и часто используемые статьи получают бонус:
- Файл `.article_meta.json` хранит `last_accessed` и `access_count` для каждой статьи
- `decay_factor = 1 / (1 + days_since_access / 30)` — статья, к которой обращались вчера, весит больше статьи, забытой на 3 месяца

## Умное сохранение (save_lesson)

При каждом вызове `save_lesson` автоматически выполняется цепочка из 8 шагов:

1. **Аудит-трейл** — запись в дневной лог `daily/YYYY-MM-DD.md`
2. **Автотегирование** — 14 правил regex добавляют теги по контенту (docker, nginx, 1c, postgres, ssh, frontend, backend, redis, mikrotik, nas, git, mcp, deploy, bugfix)
3. **Поиск существующей статьи** — semantic similarity > 0.75 → мерж вместо создания новой
4. **Мерж или создание** — новый контент добавляется как секция `### YYYY-MM-DD HH:MM`
5. **Обнаружение противоречий** — regex-паттерны (IP, версии, URL, порты) сравниваются с существующими статьями; если найдены расхождения → предупреждение в ответе
6. **Cross-references** — в связанных статьях (sim 0.55-0.85) автоматически добавляется секция "См. также" со ссылкой
7. **Active Context** — обновляется FIFO-лента последних 10 действий проекта
8. **Git commit** — все изменения коммитятся автоматически

## Веб-интерфейс

Встроенный мобильный веб-интерфейс на `http://<host>:8765/`. Тёмная тема по умолчанию, переключение на светлую через кнопку.

### 5 вкладок:

**Поиск** — основная вкладка:
- Полнотекстовый + семантический поиск
- Фильтр по проекту (dropdown)
- Кликабельные теги (tag chips с количеством статей)
- Breadcrumbs (project > filename)
- Markdown-рендеринг статей (заголовки, bold, code, списки)
- Развёрнутый просмотр полного текста
- Кнопка удаления на каждой карточке

**Добавить** — форма ручного создания:
- Тема, проект, теги, содержание
- Сохранение через тот же save_lesson (с автотегами, мержем и т.д.)

**Граф** — визуализация знаний:
- Force-directed layout на Canvas
- Узлы = статьи, цвет по проекту, размер по количеству обращений
- Связи = семантическая близость (> 0.5)
- Интерактивный: клик → переход к проекту, hover → tooltip
- Адаптивная высота под экран

**Компиляция** — управление дневными логами:
- Превью: что будет скомпилировано (мерж vs новая статья)
- Кнопка "Применить" — запуск компиляции

**Аналитика** — статистика использования:
- Топ статей по обращениям
- Никогда не открытые статьи
- Общая статистика (количество, отслеживание)

## REST API (14 endpoints)

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/` | Веб-интерфейс |
| GET | `/api/health` | Статус, количество документов, проекты, usage |
| GET | `/api/search?q=...` | Гибридный поиск |
| POST | `/api/save` | Сохранить запись `{topic, content, project, tags}` |
| GET | `/api/projects/{project}` | Список статей проекта |
| GET | `/api/article/{project}/{filename}` | Полный текст статьи |
| GET | `/api/graph` | Knowledge graph (nodes + edges) |
| GET | `/api/analytics` | Статистика использования |
| GET | `/api/compile/preview` | Превью компиляции |
| POST | `/api/compile/run` | Запуск компиляции |
| GET | `/api/export/{project}` | Экспорт всех статей проекта (JSON) |
| POST | `/api/delete` | Удаление статьи `{project, filename}` |
| GET | `/api/tags` | Все теги с количеством статей |
| GET | `/sse` | MCP SSE transport |

## Автоматизация

- **Автокомпиляция** — ежедневно в 02:00 дневные логи компилируются в проектные статьи
- **Git версионирование** — каждое изменение (save, edit, delete, compile) коммитится автоматически
- **Персистентные embeddings** — кэш `.embeddings.pkl` для быстрого старта (~45 сек вместо пересчёта)
- **Автотегирование** — 14 regex-правил дополняют пользовательские теги
- **Stale-уведомления** — при загрузке сессии предупреждает о статьях >90 дней без обновления

## Структура файлов проекта

```
memory-compiler/
├── server.py           # сервер (единственный файл)
├── requirements.txt    # зависимости
├── Dockerfile          # для Docker-запуска
├── docker-compose.yml
└── knowledge/          # база знаний (создаётся автоматически)
```

## Формат статьи

```markdown
# Название статьи

**Дата:** 2026-04-12 01:45
**Обновлено:** 2026-04-13 00:36
**Проект:** infra
**Теги:** docker, mcp, memory-compiler

## Записи

### 2026-04-12 01:45
Первая запись — описание проблемы и решения.

### 2026-04-13 00:36
Дополнение — новые факты, обновления.

## См. также
- [Связанная статья](../infra/related.md) (2026-04-13)
```

## Текущая база знаний

| Проект | Статей | Размер |
|--------|--------|--------|
Проекты и количество статей настраиваются в переменной `PROJECTS` в `server.py`.

## Запуск

### Без Docker (проще всего)

```bash
pip install -r requirements.txt
mkdir -p knowledge
PROJECTS=project-a,project-b,general python server.py
```

Сервер запустится на `http://localhost:8765`.

### С Docker

```bash
docker-compose up -d --build
```

Время первого запуска: ~1-2 минуты (скачивание модели sentence-transformers).
Последующие запуски: ~45 секунд.

## Подключение MCP-клиентов

### Claude Code (.mcp.json в проекте)
```json
{
  "mcpServers": {
    "memory-compiler": {
      "type": "sse",
      "url": "http://<your-host>:8765/sse"
    }
  }
}
```

### Claude Desktop (claude_desktop_config.json)
```json
{
  "mcpServers": {
    "memory-compiler": {
      "type": "sse",
      "url": "http://<your-host>:8765/sse"
    }
  }
}
```

## Рабочий процесс

```
1. Начало задачи:
   memory-compiler:search("тема задачи")    ← найти похожие кейсы
   memory-compiler:load_session("project")   ← продолжить с прошлой сессии

2. Работа:
   memory-compiler:ask("как настроить X?")   ← Q&A по базе
   memory-compiler:read_article(...)         ← полный текст

3. После решения:
   memory-compiler:save_lesson(...)          ← записать решение (автоматически)

4. Конец сессии:
   memory-compiler:save_session(...)         ← сохранить контекст
```

## История версий

| Версия | Дата | Строк | Основные изменения |
|--------|------|-------|--------------------|
| v1 | 12.04.2026 | ~400 | Базовый MCP: save_lesson, search, get_context |
| v2 | 12.04.2026 | ~800 | Wiki-style compile, lint, Whoosh BM25F, semantic search, web UI |
| v3 | 13.04.2026 | 1834 | Session handoff, temporal decay, get_summary, ask, active context, contradiction detection, cross-references, knowledge graph, compile UI, analytics |
| v4 | 13.04.2026 | 2235 | CRUD (delete, edit, read), search_by_tag, article_history, auto-tagging, stale alerts, markdown rendering, theme toggle, interactive graph, breadcrumbs, export |
