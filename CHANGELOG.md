# Changelog

## v12.0.0 — 2026-04-15

Search quality + UX. 36 MCP tools, 49 тестов.

### Поиск

- **Cross-encoder reranking** — после hybrid retrieval (BM25 + semantic + decay) топ-20 кандидатов пересортируются `BAAI/bge-reranker-base` (280MB, multilingual, lazy load). Precision@3 +15-20%. Graceful degradation при ошибке загрузки модели.
- **start_task релевантность** — все блоки фильтруются по теме: search >= score 15, active_context только пересекающиеся записи, session показывается только при совпадении слов, deps — только релевантные. Раньше выгружались последние 10 действий и полная сессия независимо от темы.

### Web UI

- **Snippets с подсветкой** — `/api/search` возвращает `snippets` (строки с совпадениями + контекст ±1 строка, max 5 на статью). UI рендерит monospace-блоки вместо общего preview. Слова запроса подсвечиваются `<mark>` жёлтым в title, snippets и развёрнутой статье.
- **Auto-scroll** к первому совпадению при expand статьи.
- **Расшифровка ENC: на лету** — поиск работает по содержимому секретов (только для авторизованных).
- **Счётчик совпадений** в meta строке карточки.

### Граф

- **Top-K edges per node** — после Obsidian-импорта (209 статей) граф имел 10725 связей ("волосяной шар"). Теперь max 8 strongest связей на узел → 650 edges, читаемо.
- **Orphan marker** — узлы без связей подсвечиваются серым (50% opacity, меньший размер) — честный сигнал "статья изолирована".
- **Live PROJECTS list** — `web_graph/analytics/tags` теперь вызывают `_discover_projects()` на каждый запрос. Раньше использовали кэш PROJECTS — проекты, созданные в других процессах (docker exec), не появлялись в графе до рестарта.

### Прочее

- **Stats fix** — `tools.py` инкрементировал счётчик только для 5 legacy ключей. Теперь учитываются все 36 tools.
- **PostToolUse hook matcher** — расширен с `(save_lesson|finish_task)` до полного списка: `save_decision`, `save_runbook`, `save_from_template`, `save_secret`, `ingest`, `import_obsidian`, `git_capture`, `edit_article`.
- **CLAUDE.md правила** — добавлены таблицы выбора проекта (9 проектов) и tool (8 типов).
- **Project deps** — настроены: `niksdesk/work` → `[infra, ...]`, `1c-clients` → `[work, infra]`.
- **3 runbook'a** в `memory-compiler`: деплой на NAS, рестарт контейнера, ручной backup.
- **MIT License** добавлен.
- **docs/claude-desktop-setup.md** — гайд настройки Desktop.

## v11.0.0 — 2026-04-14

Obsidian import + Knowledge gap detection. 36 MCP tools, 49 тестов.

### Добавлено

- **import_obsidian** — `import_obsidian(vault_path, project, folder_mapping, dry_run, skip_inbox)` — импорт заметок из Obsidian vault. Парсит YAML frontmatter, inline-теги (#tag), wiki-ссылки ([[X]] → **X**, [[X|Y]] → **Y**). Поддержка маппинга подпапок в проекты KB. dry_run по умолчанию.
- **knowledge_gap** — `knowledge_gap(repo_path, project, days, git_log_raw)` — находит темы активные в git-коммитах, но не покрытые статьями в базе. Извлекает темы из commit messages (убирает conventional prefix), сравнивает с embeddings существующих статей. Порог gap: similarity < 0.5.
- **storage.py** — `parse_obsidian_note()` — парсер Obsidian notes без внешних зависимостей

## v10.0.0 — 2026-04-14

Infrastructure hardening: security fixes + autodeploy + backup + scheduled tasks.

### Безопасность

- **git_capture path traversal (CRITICAL)** — валидация `repo_path` под `/repos` или `/tmp`, блокировка KNOWLEDGE_DIR. Настраивается через `GIT_CAPTURE_ALLOWED_ROOTS` env
- **since validation (HIGH)** — whitelist regex `[\w\s\-:./,]+` для non-hash значений
- **git_log_raw size limit (MEDIUM)** — макс 5MB для защиты от DoS
- **remove_project confirm** — требует `confirm=True` если в проекте есть статьи

### Инфраструктура

- **Автодеплой на NAS** — `mc-watcher.sh` + cron (minute) — автоперезапуск контейнера при изменении `*.py` по mtime
- **Daily backup** — `mc-backup.sh` + cron (4 AM) — tar.gz с ротацией 7 дней в `backups/`
- **Auto-lint weekly** — воскресенье 3 AM в lifespan, с `fix=True`
- **.env.example** — документация всех env-переменных

## v9.0.0 — 2026-04-14

Git Capture, Ingest, Obsidian-граф, start_task context. 34 MCP tools, 37 тестов.

### Добавлено

- **Git Capture** — `git_capture(repo_path, project, since, auto_save, group_by, git_log_raw)` — анализ git-истории любого репозитория, группировка коммитов по conventional commit prefix / файловой структуре, автосохранение как статьи в KB
- **Dual mode** — два режима: `repo_path` (сервер читает git log из смонтированного репо) и `git_log_raw` (клиент передаёт сырой вывод `git log`)
- **Last capture tracking** — `_last_capture.json` запоминает последний обработанный коммит, повторный вызов обрабатывает только новые
- **Docker: /repos mount** — `GIT_REPOS_PATH` env → монтируется как `/repos:ro` для repo_path режима (опционально)
- **Dockerfile** — `git config --global --add safe.directory '*'` для mounted repos
- **start_task: decisions + runbooks** — при старте задачи показывает релевантные архитектурные решения (score > 30, краткий формат) и подходящие runbooks. Фильтрация по релевантности — 0 overhead при отсутствии совпадений
- **Web UI: расшифровка секретов** — секретные статьи (ENC:) расшифровываются для авторизованных пользователей в веб-интерфейсе
- **Граф знаний (Obsidian-style)** — полная переделка: все статьи из FS (не только embeddings), живая force-simulation, drag узлов, zoom/pan, фильтр по проектам, hover-подсветка связей с tooltip, tag-based edges, touch-поддержка для мобилки
- **Ingest** — `ingest(url, project, raw_text, source, topic, auto_save)` — загрузка знаний из URL (HTML→markdown) или raw_text (PDF, документы). Preview по умолчанию, auto_save для сохранения. Без внешних зависимостей

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
