# memory-compiler

Персональная база знаний для AI-ассистентов. MCP-сервер с гибридным поиском, автокомпиляцией и веб-интерфейсом.

## Зачем

AI-ассистенты не помнят контекст между сессиями. memory-compiler решает эту проблему: решения, баги, конфигурации сохраняются в markdown-статьях, индексируются и доступны через MCP или HTTP API. Ассистент ищет похожие кейсы перед задачей и записывает новые решения после.

## Быстрый старт

```bash
pip install -r requirements.txt
mkdir -p knowledge
PROJECTS=my-project,infra,general python server.py
```

Сервер запустится на `http://localhost:8765`. Или через Docker:

```bash
docker-compose up -d --build
```

### Подключение к Claude Code

```json
{
  "mcpServers": {
    "memory-compiler": {
      "type": "sse",
      "url": "http://localhost:8765/sse"
    }
  }
}
```

## Возможности

### 33 MCP-инструмента

**Поиск и чтение:**

| Инструмент | Описание |
|------------|----------|
| `search(query, project)` | Гибридный поиск BM25F + semantic с temporal decay |
| `ask(question, project)` | Q&A — ответ с цитатами из статей |
| `search_by_tag(tag, project)` | Все статьи с указанным тегом |
| `search_snippets(query, lang, project)` | Поиск по кодовым блокам |
| `search_error(error_text, project)` | Поиск по трейсбекам и кодам ошибок |
| `search_decisions(query, project)` | Поиск по журналу решений |
| `read_article(project, filename)` | Полный текст статьи |
| `get_context(project, query)` | Топ релевантных статей перед задачей |
| `get_summary(project)` | Сжатая сводка проекта (~200 токенов) |

**Запись и редактирование:**

| Инструмент | Описание |
|------------|----------|
| `save_lesson(topic, content, project, tags)` | Сохранить с diff-отчётом, автомержем, автотегами, детекцией противоречий |
| `save_decision(title, decision, alternatives, reasoning, project)` | Записать архитектурное решение |
| `save_runbook(topic, steps, project)` | Создать пошаговую инструкцию с чекбоксами |
| `save_from_template(template, fields, project)` | Создать статью по шаблону (bug, setup, 1c, deploy, integration) |
| `save_secret(topic, content, project)` | Сохранить зашифрованную статью (пароли, ключи) |
| `edit_article(project, filename, content, append)` | Заменить или дописать |
| `delete_article(project, filename)` | Удалить статью |
| `get_runbook(project, filename)` | Получить runbook с прогрессом |
| `list_templates()` | Список доступных шаблонов |

**Сессии:**

| Инструмент | Описание |
|------------|----------|
| `save_session(project, summary, ...)` | Сохранить контекст для следующей сессии |
| `load_session(project)` | Загрузить контекст + уведомления об устаревших статьях |
| `get_active_context(project)` | Лента последних 10 действий |

**Комбинированные:**

| Инструмент | Описание |
|------------|----------|
| `start_task(topic, project)` | Начать задачу: поиск в базе + загрузка сессии + активный контекст |
| `finish_task(topic, content, project)` | Завершить задачу: сохранить урок + сессию |

**Управление проектами:**

| Инструмент | Описание |
|------------|----------|
| `add_project(name)` | Создать новый проект |
| `remove_project(name)` | Удалить проект со всеми статьями |
| `list_projects()` | Список проектов с количеством статей |
| `set_project_deps(project, depends_on)` | Установить зависимости между проектами |
| `get_project_deps(project)` | Получить зависимости проекта |

**Обслуживание:**

| Инструмент | Описание |
|------------|----------|
| `git_capture(repo_path, project, ...)` | Автосбор знаний из git-коммитов (repo_path или git_log_raw) |
| `compile(dry_run, project, since)` | Компиляция дневных логов в wiki-статьи |
| `lint(project, fix)` | Проверка: дубли, устаревшее, теги |
| `reindex()` | Переиндексация |
| `article_history(project, filename)` | Git-история статьи |

### Гибридный поиск

Два движка, объединённых в одну формулу:

- **BM25F** — полнотекстовый с весами полей (title x5, tags x3, body x1)
- **Semantic** — векторные embeddings (`paraphrase-multilingual-MiniLM-L12-v2`), русский + английский

```
score = (0.4 × BM25 + 0.6 × semantic) × decay_factor
```

Temporal decay — свежие и часто используемые статьи выше в результатах.

### Умное сохранение

При `save_lesson` автоматически:

1. Запись в дневной лог (аудит-трейл)
2. Автотегирование (14 regex-правил)
3. Поиск существующей статьи по смыслу — мерж вместо дубля
4. Обнаружение противоречий (IP, версии, URL, порты)
5. Cross-references в связанных статьях
6. Обновление ленты активного контекста
7. Извлечение git-ссылок (коммиты, issues, теги)
8. Git commit

### Веб-интерфейс

Встроенный мобильный UI на `http://localhost:8765`. Тёмная/светлая тема.

- **Поиск** — фильтр по проекту, кликабельные теги, markdown-рендеринг, breadcrumbs
- **Добавить** — форма записи
- **Граф** — интерактивная визуализация связей между статьями
- **Компиляция** — превью и запуск
- **Аналитика** — топ по обращениям, неиспользуемые статьи
- **Аудит** — лог всех MCP-обращений

### REST API

13 endpoints: health, поиск, сохранение, CRUD статей, проекты, граф знаний, аналитика, теги, компиляция (превью/запуск), экспорт.

### Автоматизация

- Автокомпиляция дневных логов в 02:00
- Git-версионирование всех изменений
- Кэш embeddings для быстрого старта
- Уведомления об устаревших статьях

## Безопасность

Три уровня защиты, каждый включается через env переменную:

| Уровень | Переменная | Что делает |
|---------|-----------|------------|
| Авторизация | `MC_API_KEY` | Логин-страница + cookie 30 дней, Bearer token, ?key= в URL |
| Шифрование | `MC_ENCRYPT_KEY` | AES-256 для секретных статей (save_secret) |
| Аудит | автоматически | Лог всех MCP-вызовов, вкладка "Аудит" в Web UI |

Без переменных — открытый доступ (обратная совместимость). Подробнее: [docs/security.md](docs/security.md)

## Структура проекта

```
memory-compiler/
├── server.py                  # Entry point (12 строк)
├── memory_compiler/
│   ├── __init__.py
│   ├── config.py              # Константы, метаданные, shared state
│   ├── search.py              # Whoosh BM25F + semantic search
│   ├── storage.py             # Статьи, git, утилиты, автотегирование
│   ├── handlers.py            # Реализация MCP-инструментов
│   ├── tools.py               # Регистрация MCP tools и диспетчер
│   ├── api.py                 # REST endpoints, Starlette app
│   └── ui.py                  # Web UI HTML шаблон
├── tests/
│   ├── conftest.py            # Фикстуры (tmp knowledge dir)
│   ├── test_config.py
│   ├── test_storage.py
│   ├── test_search.py
│   └── test_handlers.py
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## Тесты

```bash
pip install pytest pytest-asyncio
pytest tests/ -v
```

## Стек

| Компонент | Технология |
|-----------|-----------|
| Сервер | Python 3.12, MCP SDK, Starlette, Uvicorn |
| Полнотекстовый поиск | Whoosh (BM25F) |
| Семантический поиск | sentence-transformers |
| Хранилище | Markdown + Git |

## Рабочий процесс

```
1. Начало задачи    → start_task("тема")       ← поиск + сессия + контекст
2. В процессе       → ask("как настроить X?")
3. Решение найдено  → finish_task(...)          ← урок + сессия
```

## Конфигурация

Проекты создаются динамически через `add_project()` или автоматически при `save_lesson()`. Каждый проект — отдельная директория в `knowledge/` с markdown-статьями.

Опционально можно задать начальные проекты через переменную окружения:

```bash
PROJECTS=backend,infra,general python server.py
```

### Git Capture

Два режима автосбора знаний из git:

**Режим 1 — repo_path** (сервер читает git напрямую):
```bash
# .env
GIT_REPOS_PATH=/path/to/your/repos
```

```
git_capture(repo_path="/repos/my-project", project="myapp", auto_save=true)
```

**Режим 2 — git_log_raw** (клиент передаёт вывод git log):
```bash
# Claude запускает локально:
git log --format="%H|%s|%an|%aI" --numstat --since="7 days ago"
# и передаёт вывод в git_log_raw параметр
```

Повторные вызовы с `repo_path` автоматически обрабатывают только новые коммиты.
