# Changelog

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
