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

### Подключение к Claude Code Desktop

Полная инструкция: [docs/claude-desktop-setup.md](docs/claude-desktop-setup.md) — MCP, скил memory-autopilot, hooks, настройка зависимостей.

## Возможности

### 46 MCP-инструмента

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

**Temporal state:**

| Инструмент | Описание |
|------------|----------|
| `save_tracking(project, entity, facts)` | Bi-temporal снимок: текущее состояние + история (версии, деплои, конфиги) |
| `get_current(project, entity)` | Получить текущий статус из tracking-статьи |

**Комбинированные:**

| Инструмент | Описание |
|------------|----------|
| `start_task(topic, project)` | Начать задачу: поиск + сессия + контекст + решения + runbooks |
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
| `ingest(url, project, ...)` | Загрузка знаний из URL (HTML→markdown) или raw_text (PDF/документы) |
| `import_obsidian(vault_path, project, folder_mapping, dry_run)` | Импорт заметок из Obsidian vault (frontmatter, теги, wiki-ссылки) |
| `git_capture(repo_path, project, ...)` | Автосбор знаний из git-коммитов (repo_path или git_log_raw) |
| `knowledge_gap(repo_path, project, days)` | Найти темы в git-коммитах, которых нет в базе знаний |
| `compile(dry_run, project, since)` | Компиляция дневных логов в wiki-статьи |
| `lint(project, fix)` | Проверка: дубли, устаревшее, теги |
| `reindex()` | Переиндексация |
| `article_history(project, filename)` | Git-история статьи |

**Контекстная генерация (contextual retrieval):**

| Инструмент | Описание |
|------------|----------|
| `context_gaps(project, limit)` | Выдать статьи, которым нужен ИИ-контекст секций (для бэкфилла) |
| `save_contexts(project, filename, contexts)` | Записать ИИ-контекст секций во frontmatter и ре-эмбеддить статью |

### Гибридный поиск + reranking

Три уровня релевантности:

- **BM25F** (Whoosh) — полнотекстовый с весами полей (title x5, tags x3, body x1)
- **Semantic** — bi-encoder embeddings (дефолт `paraphrase-multilingual-MiniLM-L12-v2`; через env `EMBED_MODEL` переключается на `BAAI/bge-m3` 1024d или `Alibaba-NLP/gte-multilingual-base`, кэш авто-инвалидируется при смене)
- **Cross-encoder reranking** — `BAAI/bge-reranker-v2-m3` (multilingual, built on BGE-M3, сильный RU+EN) пересортировывает пул кандидатов (`SEARCH_CANDIDATE_POOL`, дефолт 10 — урезано с 20 под слабый CPU NAS) для финального выбора (precision@3 +15-20%); опционально SPLADE 3-way через `SPLADE_ENABLED=true`

Слияние каналов — **RRF** (Reciprocal Rank Fusion), не взвешенная сумма: не требует калибровки между BM25 и cosine-сходством.

```
rrf(doc) = Σ_channel 1 / (RRF_K + rank_channel(doc))     # BM25 + semantic (+ SPLADE)
scaled   = rrf × 3000 × (0.7 + 0.3 × decay_factor)
final    = cross_encoder.rerank(top-N кандидатов) → top_k (search: 8, get_context: 5)
```

Temporal decay — свежие и часто используемые статьи выше в результатах.

### Contextual Retrieval

Перед эмбеддингом к каждому чанку добавляется контекст-заголовок, ситуирующий кусок в документе (метод Anthropic) — запрос находит нужную секцию, даже если ключевых слов нет в её теле.

- **Фаза 1 (метаданные):** контекст-заголовок `[проект · заголовок · секция · теги]` автоматически, для всех статей.
- **Фаза 2 (ИИ-контекст):** для многосекционных статей секции получают ИИ-сгенерированную фразу-контекст (одна строка на секцию), хранится в YAML-frontmatter (`contexts:`) и берётся вместо метаданных. Наполняется через два инструмента:
  - `context_gaps(project, limit)` — выдаёт статьи, которым нужен контекст (многосекционные, не-секретные, без `contexts:`), с секциями, телом и инструкцией. Append-log `### {дата}`-записи игнорируются.
  - `save_contexts(project, filename, contexts)` — валидирует заголовки, пишет `contexts:` во frontmatter, инкрементально ре-эмбеддит статью.

Формат строки эмбеддинга версионируется (`context_format_version` в `.embeddings.pkl`) — при несовпадении кэш пересобирается полностью.

### Бэкфилл ИИ-контекста

ИИ-контекст (фаза 2) наслаивается постепенно и не требуется для работы поиска — статья без него использует метаданные фазы 1. Чтобы наполнить существующую базу, попросите ассистента запустить бэкфилл — это цикл «tool-dance», который ассистент выполняет сам:

```
Пока context_gaps(project) возвращает статьи:
  1. context_gaps(project, limit=5)      — получить батч статей-пробелов
  2. для каждой секции — написать одну фразу (≤25 слов), ситуирующую её в документе
  3. save_contexts(project, filename, [{heading, context}, …])
  4. повторять, пока remaining == 0
```

Бэкфилл прерываемый и возобновляемый (состояние = сам frontmatter): новые статьи подхватятся при следующем проходе. Для большой базы распараллеливается по проектам (разные файлы — без конфликтов). Пример запроса ассистенту: «запусти бэкфилл ИИ-контекста по проекту infra» или «по всем проектам».

### Умное сохранение

При `save_lesson` автоматически:

1. Запись в дневной лог (аудит-трейл)
2. Автотегирование (14 regex-правил)
3. Поиск существующей статьи по смыслу — мерж вместо дубля
4. Обнаружение противоречий (IP, версии, URL, порты) — role-aware: разные роли IP (private vs public) и well-known DNS (8.8.8.8, 1.1.1.1, …) не дают FP; CIDR-нотация не сравнивается как host
5. Cross-references в связанных статьях
6. Обновление ленты активного контекста
7. Извлечение git-ссылок (коммиты, issues, теги)
8. Git commit

### Веб-интерфейс

Встроенный мобильный UI на `http://localhost:8765`. Тёмная/светлая тема.

- **Поиск** — snippets с подсветкой совпадений (жёлтый), фильтр по проекту, кликабельные теги, markdown-рендеринг, breadcrumbs, auto-scroll к первому match
- **Командная палитра** — `Ctrl+K` (и `Cmd+K`) из любой вкладки: поиск с дебаунсом, `↑`/`↓` — навигация, `Enter` — открыть развёрнутой, `Esc` — закрыть
- **Похожие** — сайдбар семантически близких статей рядом с раскрытой: оценка близости, клик — переход, кнопка «следит/заморожен» фиксирует список при навигации
- **Добавить** — форма записи
- **Граф** — Obsidian-style анимированный граф: drag, zoom, pan, фильтр по проектам, hover-подсветка связей
- **Компиляция** — превью и запуск
- **Аналитика** — топ по обращениям, неиспользуемые статьи
- **Аудит** — лог всех MCP-обращений

### REST API

18 REST endpoints (`/api/*`): health, версия, логин/auth, поиск, семантически похожие статьи, сохранение, CRUD статей, проекты, граф знаний, аналитика, теги, компиляция (превью/запуск), экспорт, аудит, логи. Плюс `/` (Web UI), `/login`, `/sse` (MCP-транспорт).

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

## Резервное копирование

Двухзвенная схема (NAS → ПК), звенья независимы:

- **NAS** — ежедневный `tar` каталога `knowledge/` (`scripts/mc-backup.sh`, cron 04:00, ротация 7 дней). Архивы кладутся в `backups/`.
- **ПК** — независимая копия архивов + снимок `.env` (`scripts/mc-backup-pull.ps1`, задача Task Scheduler «memory-compiler backup pull», 05:00). Ретенция 30 дней для ежедневных + месячные срезы (`-01`) хранятся бессрочно. Restore-drill проверяет целостность свежего архива (`scripts/mc-backup-verify.ps1`).

### Установка (Windows, звено ПК)

Пути `Source`/`EnvFile` по умолчанию выводятся из расположения скрипта (`<repo>/scripts/`), так что параметры можно не задавать. Регистрация ежедневной задачи (замени `<repo>` на путь к своему клону):

```powershell
schtasks /Create /TN "memory-compiler backup pull" /SC DAILY /ST 05:00 /F `
  /TR "pwsh.exe -NoProfile -ExecutionPolicy Bypass -File `"<repo>\scripts\mc-backup-pull.ps1`" -Verify"
```

Разовый прогон и проверка: `pwsh -File scripts\mc-backup-pull.ps1 -Verify` (лог — `C:\Backups\memory-compiler\pull.log`). Восстановление: распаковать нужный `archives\knowledge-*.tar.gz` в каталог memory-compiler, вернуть соответствующий `secrets\.env-*` (в нём `MC_ENCRYPT_KEY`), поднять контейнер.

## Структура проекта

```
memory-compiler/
├── server.py                  # Entry point (11 строк)
├── memory_compiler/
│   ├── __init__.py
│   ├── config.py              # Константы, метаданные, shared state
│   ├── search.py              # Whoosh BM25F + semantic + reranking + contextual retrieval
│   ├── storage.py             # Статьи, git, утилиты, автотегирование
│   ├── handlers.py            # Реализация MCP-инструментов
│   ├── tools.py               # Регистрация MCP tools, диспетчер, resources, prompts
│   ├── api.py                 # REST endpoints, Starlette app
│   ├── ui.py                  # Web UI HTML шаблон
│   ├── markdown_render.py     # Серверный рендер MD статей (markdown-it-py + Pygments + nh3)
│   ├── obs.py                 # Observability: structured logging, request_id, счётчики ошибок
│   └── maintenance.py         # Одноразовые проходы обслуживания
├── tests/                     # pytest (офлайн: HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1)
│   ├── conftest.py            # Фикстуры (tmp knowledge dir)
│   └── test_*.py              # search, storage, handlers, contextual retrieval, web, concurrency …
├── skills/
│   └── memory-autopilot/
│       └── SKILL.md           # Скил автоуправления памятью для Claude Code
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

### С memory-autopilot (рекомендуется)

Скил `memory-autopilot` автоматизирует весь цикл — ищет контекст, выбирает tool, сохраняет результат. Пользователь просто работает, память управляется невидимо.

Установка: скопируйте `skills/memory-autopilot/SKILL.md` в `~/.claude/skills/memory-autopilot/SKILL.md`.

### Ручной режим

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

## Лицензия

[MIT](LICENSE)
