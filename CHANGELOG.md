# Changelog

## v8.0.0 — 2026-04-13

Безопасность: авторизация, шифрование секретов, аудит. 32 MCP tools, 37 тестов.

### Добавлено

- **Авторизация** — `MC_API_KEY` env var, AuthMiddleware (Bearer token + cookie), логин-страница с cookie на 30 дней, обратная совместимость (без ключа — открытый доступ)
- **Шифрование секретов** — `save_secret(topic, content, project)` шифрует AES-256 (Fernet), `read_article` расшифровывает, в поиске показывается `[зашифровано]`
- **Аудит** — каждый вызов MCP tool логируется в `_audit.log` (без content), новая вкладка "Аудит" в Web UI, endpoint `/api/audit`
- **Web UI** — логин-страница, вкладка "Аудит"
- **requirements.txt** — `cryptography>=42.0.0`

## v7.0.0 — 2026-04-13

7 новых фич для AI-разработки. 31 MCP tool, 32 теста.

### Добавлено

- **Snippet Search** — `search_snippets(query, lang, project)` — поиск по кодовым блокам в статьях
- **Runbook Mode** — `save_runbook(topic, steps, project)` + `get_runbook(project, filename)` — пошаговые инструкции с чекбоксами и прогрессом
- **Error Pattern Matching** — `search_error(error_text, project)` — поиск по трейсбекам и кодам ошибок с ре-ранжированием
- **Project Dependencies** — `set_project_deps(project, depends_on)` + `get_project_deps(project)` — граф зависимостей, автоподтягивание контекста в `start_task`
- **Decision Log** — `save_decision(title, decision, alternatives, reasoning, project)` + `search_decisions(query, project)` — журнал архитектурных решений
- **Article Templates** — `save_from_template(template, fields, project)` + `list_templates()` — шаблоны: bug, setup, 1c, deploy, integration

### Улучшено

- **Diff-Aware Save** — `save_lesson` теперь показывает diff: `+N строк, теги: +tag1, +tag2`
- **start_task** — автоматически подтягивает контекст из зависимых проектов

## v6.0.0 — 2026-04-13

Полный рефакторинг: монолит `server.py` (2480 строк) разбит на пакет `memory_compiler/` из 7 модулей.

### Изменения

- **refactor:** `server.py` → пакет `memory_compiler/` (config, search, storage, handlers, tools, api, ui)
- **refactor:** `server.py` теперь thin launcher (12 строк)
- **test:** pytest suite — 18 тестов (config, storage, search, handlers)
- **build:** Dockerfile обновлён для пакетной структуры, HEALTHCHECK добавлен
- **docs:** README обновлён — структура проекта, тесты, все 19 инструментов

## v5.0.0 — 2026-04-13

### Добавлено

- `start_task(topic)` — комбинированный tool: поиск + загрузка сессии + активный контекст
- `finish_task(topic, content, project)` — комбинированный tool: save_lesson + save_session

## v4.2.0 — 2026-04-12

### Добавлено

- Динамическое управление проектами: `add_project`, `remove_project`, `list_projects`
- Проекты создаются автоматически при `save_lesson`
- Убраны enum-ограничения из tool schemas

## v4.1.0 — 2026-04-12

### Добавлено

- Git-линковка: извлечение коммитов, issues, тегов, веток из контента
- Секция "Git-ссылки" в статьях

## v4.0.0 — 2026-04-12

### Добавлено

- CRUD статей: `delete_article`, `edit_article`, `read_article`
- `search_by_tag` + кликабельные теги в UI
- `article_history` (git log)
- Экспорт проекта (`/api/export`)
- Markdown-рендеринг в UI
- Фильтр по проекту в поиске
- Автотегирование (14 regex-правил)
- Stale-уведомления при `load_session`
- Интерактивный граф (клик, hover)
- Тёмная/светлая тема
- Breadcrumbs
- Кнопка удаления в UI

## v3.0.0 — 2026-04-12

### Добавлено

- Session Handoff: `save_session`, `load_session`
- Temporal Decay (last_accessed, access_count в ранжировании)
- Сжатый индекс: `get_summary`
- Q&A tool: `ask` с цитатами
- Обнаружение противоречий при `save_lesson`
- Cross-references между статьями
- Knowledge Graph + визуализация в web UI
- Active Context (FIFO 10 действий)
- Compile UI (превью + запуск)
- Analytics (топ обращений, неиспользуемые)

## v2.0.0 — 2026-04-11

### Добавлено

- Гибридный поиск: Whoosh BM25F + sentence-transformers semantic search
- Chunking статей для точного семантического поиска
- Кэш embeddings для быстрого старта
- Web UI (5 вкладок: поиск, добавление, граф, компиляция, аналитика)

## v1.0.0 — 2026-04-10

### Начальный релиз

- MCP-сервер с SSE транспортом
- `save_lesson`, `search`, `get_context`, `compile`, `lint`, `reindex`
- Whoosh BM25F полнотекстовый поиск
- Автокомпиляция daily логов в статьи
- Git-версионирование knowledge base
- Docker + docker-compose
