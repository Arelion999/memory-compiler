# Changelog

Semantic versioning: major.minor.patch. Versions below 1.0 were development milestones (v8-v12 pre-release).

## v1.7.8 — 2026-05-19

Web UI single-word search не возвращает результаты.

### Fixed

- **Single-word queries из Web UI/MCP теперь работают**. `is_low_confidence_query` требовал `>=2 content tokens`. Однословные запросы вроде `memory-compiler`, `nginx`, `postgres` после фильтра stopwords давали 1 токен, флажились low-confidence, и search возвращал пустой результат. На скриншоте UI это видно как «Ничего не найдено» при наличии 23 статей в проекте.
- Default `min_content_tokens` снижен с 2 до 1 — теперь low-confidence только при ZERO content tokens (только stopwords / noise).
- В stopwords добавлены question words (`what/when/where/why/how/who/which` + `что/когда/где/почему/зачем/кто/какой/какая/какие`), чтобы `"what's next"` остался low-confidence (был 1 token "what" без них → не low; теперь "what" стопворд → 0 tokens → low).

### Tests

- 135/135 pass (was 134). +1 test: `test_low_confidence_single_word_passes`. Updated `test_low_confidence_query_mixed`: однотокенный "давай продолжим nginx" больше НЕ low-confidence (это позитивное изменение — "nginx" actionable).

## v1.7.7 — 2026-05-18

Lint cleanup: убраны false positives + автофикс битых ссылок.

### Fixed

- **Check 1/2 пропускает service files** — `_active_context.md`, `_session.md`, `_log.md`, `_reflections.md`, `tracking_*.md` управляются движком и не имеют yaml header by design. Раньше lint флажил их за пустоту/отсутствие метаданных (~60 false positives на 24 проектах).
- **Auto-fix dead refs в `lint(fix=true)`** — каждая `[text](dead.md)` заменяется на голый `text`. Содержимое статьи сохранено, только убраны битые ссылки. Идемпотентно.

### Tests

- 134/134 (was 132). +2: `test_lint_check2_skips_service_files`, `test_lint_fix_removes_dead_refs`.

## v1.7.6 — 2026-05-18

Cache-invalidate `.embeddings.pkl` при смене `LATE_CHUNKING` flag.

### Fixed

- `LATE_CHUNKING` меняет топологию embeddings (whole-doc vs N chunks per article).
  Раньше pkl tag хранил только `model` — переключение flag без смены модели не
  инвалидировало кэш, и в RAM грузились embeddings от другого режима. Теперь
  pkl содержит `late_chunking` поле, при mismatch — rebuild.
- Legacy pkl без поля `late_chunking` рассматривается как валидный для текущего
  значения flag (no invalidation, preserves cache for users on the same setting).

### Tests

- 132/132 (was 131). +1 test: late_chunking mismatch triggers rebuild.

### Rationale

`e5-base` имеет `max_seq_length=512` — это значит для длинных статей late
chunking теряет хвост (truncate до первых ~2000 chars). Правильный режим для
512-context моделей — chunking по `###`. Late chunking имеет смысл только при
long-context моделях типа BGE-M3 (max=8192).

## v1.7.5 — 2026-05-18

Закрыты 3 проблемы пропущенные в v1.7.4 (second-pass audit).

### Fixed

- **P1 (HIGH): Path traversal в save_lesson / save_session / save_runbook / save_decision / save_secret / save_from_template / save_tracking / save_compact** — v1.7.4 закрыл только edit/read/delete_article, но все handlers использующие `project_dir(project)` имели ту же уязвимость. Введён `safe_project_dir()` — отвергает project с `..`, `/`, `\`, или резолвящийся вне KNOWLEDGE_DIR. Применён ко всем 14 call-сайтам в handlers.py. Plus добавлен top-level ValueError handler в `call_tool` dispatcher — graceful response при любом safe_*-провале.
- **P2 (MEDIUM): `safe_article_path` обходился через project="."** — точка не содержит `/`, `\`, `..` (проверки v1.7.4), но `KNOWLEDGE_DIR / "." → KNOWLEDGE_DIR` — даёт доступ к root-уровню файлам. Усилена проверка: project должен быть STRICT subdir, не сам KNOWLEDGE_DIR.
- **P3 (MEDIUM): `mark_dependents` теперь cross-project** — раньше пробегал только по `*.md` в проекте edited статьи. Cross-project ссылки `[text](../proj/file.md)` из других проектов не помечались. Теперь итерирует по всем проектам с правильным паттерном для каждого случая (intra-vs-cross).

### Tests

- 131/131 pass (было 127). +4 теста: P1 traversal через save_lesson, P2 dot project + safe_project_dir, P3 cross-project mark_dependents.

### Migration

Backward-compatible. Никаких env / config изменений.

## v1.7.4 — 2026-05-18

Hardening: устранены 12 проблем найденных в code-audit после миграции на e5-base.

### Fixed — HIGH severity

- **Path traversal в `edit_article` / `read_article` / `delete_article`** — теперь все три используют `safe_article_path()` с проверкой что итоговый путь не выходит за пределы `KNOWLEDGE_DIR`. Defense-in-depth поверх MC_API_KEY auth.
- **`rebuild_embeddings` atomic swap** — раньше сначала очищал `_embeddings = {}`, потом encode. Если encode падал (OOM, network) — глобальный dict оставался пустой и semantic search молча возвращал `[]`. Теперь строится локально, swap в globals только после успешного encode.
- **Lint Check 9 cross-project + кириллица** — regex не поддерживал `../other_proj/file.md` (проверял существование только в текущем проекте) и не ловил русские имена файлов. Переписан с двумя capture groups; resolve target правильно учитывает project segment.

### Fixed — MEDIUM severity

- **`embed_document` теперь использует `_chunk_article`** — новые статьи через `save_lesson` / `edit_article` получают такую же представительность как rebuild_embeddings (раньше использовался `_doc_text_for_embedding` — только preview).
- **Lint Check 8 (orphans) через link-parsing** — раньше substring-match на `a.name in body` давал false positives (raw mention в тексте) и false negatives (link без `.md`). Теперь парсит markdown-links регулярным выражением.
- **`mark_dependents` устойчив к unreadable files** — try/except вокруг `read_text` чтобы PermissionError / UnicodeDecodeError не ломал edit_article.

### Fixed — LOW severity

- **`asyncio.create_task` для bg rebuild сохраняется в переменной** — иначе Python GC может убрать task мид-flight.
- **`append_reflections` атомарная запись** — write tmp + rename вместо direct write_text, защита от torn writes при concurrent finish_task.
- **`extract_reflections` пропускает отрицания** — sentences с "не настроил" / "did not configure" / "n't" больше не извлекаются как факты.
- **`load_embeddings` логирует причину провала** — model mismatch / corrupt pkl / invalid schema теперь печатает явный warning вместо silent fail.
- **`_log.md` ротируется** — при превышении `LOG_ROTATE_BYTES` (256KB default) старое содержимое перемещается в `_log.archive.md`, активный лог обнуляется.
- **Warning при embeddings dict > 10k entries** — индикатор memory pressure для роста корпуса.

### Tests

- 127/127 pass (было 117). +10 новых: 1 path traversal, 1 atomic rebuild, 2 lint dead-ref (cross-proj + cyrillic), 1 embed_document parity, 1 orphan link-parsing, 1 mark_dependents read failure, 1 negation skip, 1 atomic write, 1 log rotation.

### Migration

Никаких env / config изменений не требуется — все fixes backward-compatible. Чтобы переопределить порог ротации лога: `from memory_compiler import storage; storage.LOG_ROTATE_BYTES = N`.

## v1.7.3 — 2026-05-18

Hotfix: rebuild_embeddings вынесен из startup-хука в background task.

### Fixed

- **Контейнер уходит в unhealthy при смене EMBED_MODEL** — startup lifespan вызывал `rebuild_embeddings()` синхронно если кеш `.embeddings.pkl` несовместим. Для BGE-M3 на CPU это 5-15 минут, в течение которых `/api/health` не отвечает и Docker healthcheck помечает контейнер как unhealthy. Теперь rebuild идёт через `asyncio.create_task` + `run_in_executor` в фоне, сервер стартует мгновенно, semantic search возвращает пусто до завершения фоновой задачи (BM25 + reranker работают сразу).

## v1.7.2 — 2026-05-18

Hotfix: HF Hub offline-режим по умолчанию + пробрасывание новых ML env-vars в compose.

### Fixed

- **OSError Errno 99 (Cannot assign requested address)** — после загрузки модели sentence-transformers продолжал стучаться в huggingface.co (telemetry/проверка обновлений). На сетях с rate-limit/firewall это валит контейнер при старте. `HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1` по умолчанию в compose — работаем из локального кеша, никаких сетевых вызовов после первого load.
- В docker-compose.yml добавлен passthrough для `EMBED_BATCH_SIZE`, `EMBED_MAX_SEQ_LENGTH` (из v1.7.1) — без этого env-vars не доходили до контейнера.

### Migration

- Если ставишь EMBED_MODEL на новую модель — временно установи `HF_HUB_OFFLINE=0` для первой загрузки, затем верни обратно в 1.

## v1.7.1 — 2026-05-18

Hotfix: батчинг при reindex с большими моделями (BGE-M3 и аналоги).

### Fixed

- **OOM при `rebuild_embeddings` на BGE-M3** — `model.encode(docs)` без batch_size пытался разместить полный тензор `(N=540, seq=8192, hidden=1024)` ~ 18GB peak allocation, что валилось `RuntimeError: alloc_cpu.cpp:127 Cannot allocate memory` даже на 32GB-хостах. Добавлены `EMBED_BATCH_SIZE` (default 8) и `EMBED_MAX_SEQ_LENGTH` (default 2048, cap для long-context моделей).
- `get_embed_model()` теперь принудительно устанавливает `max_seq_length` после load (если он выше cap). Long-context модели типа BGE-M3 имеют default 8192 — это съедает память при batch encoding без реальной нужды для статей памяти-компилера.

### Tests

- 117/117 pass (было 115). +2 теста на env-override batch/seq params.

## v1.7.0 — 2026-05-18

Все 5 отложенных фич из v1.6.0 research-плана. Через env-флаги и feature gates, чтобы продакшен не сломался — все опасные изменения opt-in, дефолты совместимы с v1.6.0.

### Контекст

После v1.6.0 остался отложенный список: embedding upgrade, late chunking, reflective loop, Schema.md, SPLADE. Этот релиз закрывает все 5 — реализованы аккуратно с feature-flags для миграционно опасных вещей.

### Added

- **Env-driven embedding model** — `EMBED_MODEL` переключает модель векторизации. Дефолт остался `paraphrase-multilingual-MiniLM-L12-v2` (384 dim) для backward compat. Рекомендуемый upgrade: `EMBED_MODEL=BAAI/bge-m3` (+13 MTEB, 1024 dim, multilingual). `.embeddings.pkl` теперь версионируется: содержит `model` field, автоматически инвалидируется при смене модели и триггерит rebuild.
- **Late chunking** (env `LATE_CHUNKING=true`) — pragmatic-версия Jina AI pattern: при включении статья эмбеддится целиком одним вектором вместо разбиения на `###`-чанки. Сохраняет контекст между секциями (anaphoric refs). Лучше работает с long-context моделями (BGE-M3 max=8192) — на дефолтном MiniLM может truncating длинные статьи, поэтому включать имеет смысл вместе с `EMBED_MODEL` upgrade.
- **Reflective Memory (rule-based)** — `finish_task` автоматически извлекает atomic facts из content+session_summary через regex на bullets, numbered lists и action-verbs (настроил/исправил/добавил/configured/fixed/...). Факты копятся в `<project>/_reflections.md` (FIFO 20, newest-first). Без внешней LLM — это упрощённая версия Prospective Reflection из arXiv 2503.08026.
- **Schema.md per-project** — новый tool `init_schema(project)` создаёт `_schema.md` шаблон с секциями Сущности/Связи/Stylistic/Glossary. Идемпотентно (не перезаписывает существующий). Контракт проекта как явный артефакт по паттерну Karpathy LLM Wiki.
- **SPLADE 3-way hybrid skeleton** (env `SPLADE_ENABLED=true`) — инфраструктура для третьего канала retrieval (BM25 + dense + sparse-learned) с RRF merge. Сейчас stub-реализация возвращает пустой dict — graceful 2-way fallback. Готово к drop-in замене когда появится хороший multilingual SPLADE checkpoint.

### Tests

- 115/115 pass (было 100). +15 новых: 3 embedding-upgrade, 2 late-chunking, 5 reflective, 2 schema, 3 SPLADE.

### Migration

- **Дефолты неизменны.** Push v1.7.0 не ломает существующие deploy.
- Чтобы попробовать BGE-M3: на NAS в `.env` добавь `EMBED_MODEL=BAAI/bge-m3`, перезапусти контейнер, выполни `mcp__memory-compiler__reindex` через любой клиент. Все 540 embeddings пересчитаются (~5-15 мин), `.embeddings.pkl` сохранится с новым model-tag.
- Чтобы включить late chunking с BGE-M3: `LATE_CHUNKING=true` + повторный reindex.
- Чтобы попробовать SPLADE: оставь выключенным до появления хорошей multilingual модели; код к ней готов.

## v1.6.0 — 2026-05-18

Качество retrieval + аккуратность базы знаний. Влияние research-обзора по новым техникам RAG/agent-memory (Karpathy LLM Wiki, MTEB-2026).

### Контекст

`bge-reranker-base` уже был в коде с v1.3, но вызывался только в `start_task` — обычный `search()`/`get_context()` довольствовался hybrid RRF без cross-encoder reranker'а. Также Karpathy в апреле 2026 формализовал паттерн `index.md`+`log.md`+`Schema`+`Lint` для compounding knowledge artifacts — у нас была половина (index.md, stale_facts), не было per-project journal и проверок целостности ссылок.

### Added

- **Cross-encoder reranker подключён к `search()` и `get_context()`** — индустриальный паттерн 2026: hybrid retrieval → top-20 кандидатов → cross-encoder rerank → top-K. По RAG-бенчмаркам это +25–40% precision над hybrid alone при ~50ms overhead. В выводе теперь видно `score: 44.8, rerank: 9.21` для каждого результата.
- **Дефолтный reranker — `BAAI/bge-reranker-v2-m3`** (multilingual, на базе BGE-M3). Был `bge-reranker-base` (английский). Для русскоязычной базы это значительный прирост качества. Управляется через env `RERANKER_MODEL` — на NAS с малой RAM можно поставить `cross-encoder/ms-marco-MiniLM-L-6-v2`.
- **Per-project `_log.md`** — append-only журнал событий по Karpathy LLM Wiki. Записываются `save_lesson`, `edit_article`, `lint`. Отделён от глобального `_audit.log` (JSON, все вызовы) — это человеко-читаемый narrative проекта.
- **`lint` расширен**: проверка `сирота` (no inbound refs — статья на которую никто не ссылается) и `dead reference` (markdown-link на несуществующий файл). Дополняет существующие checks (метаданные, устаревшее, дубли).
- **Cascade-mark при `edit_article`** — когда статья меняется, на каждой строке которая ссылается на неё в других статьях проекта обновляется маркер `🔄 обновлено: YYYY-MM-DD HH:MM`. Идемпотентно — re-edit просто обновляет timestamp. Решает «temporal blindness» проблему из Karpathy gist.

### Tests

- 100/100 pass (было 85). +15 новых: 3 reranker integration, 5 log.md, 3 lint extensions, 2 reranker config, 2 cascade-mark.

### Migration

- Embedding-кэш `.embeddings.pkl` ломать не нужно — embedding модель та же (`paraphrase-multilingual-MiniLM-L12-v2`).
- При первом `start_task` или `search` после апгрейда сервер скачает `bge-reranker-v2-m3` (~570MB) с HuggingFace — будет 1-минутная задержка, fallback на `None` если HF недоступен. Чтобы остаться на старом `base` reranker'е: `RERANKER_MODEL=BAAI/bge-reranker-base`.

## v1.5.2 — 2026-05-12

Hotfix.

### Fixed

- **`consolidate` пропускал длинные статьи** — embeddings для статей с `###`-секциями хранятся как chunk-ключи (`path#chunk0`). Старая фильтрация `if "#" in p: continue` отбрасывала такие статьи целиком — проекты типа `project-8` (длинные статьи) показывали «меньше 2 статей в выборке». Теперь chunks агрегируются в mean-vector на parent-статью.

## v1.5.1 — 2026-05-12

Hotfix.

### Fixed

- **`consolidate`** обращался к копии ссылки `_embeddings` сделанной во время import — после `load_embeddings()`/`rebuild_embeddings` ссылка устаревала, инструмент рапортовал «embeddings ещё не построены» при наличии данных. Теперь читает `_smod._embeddings` напрямую.
- **`gap_report` cosine-фильтр** — раньше для `project="all"` пропускал «решено где-то ещё» только если решение было в проекте конкретного запроса. Теперь любой match cosine >= 0.55 в любом проекте считается «знание есть, retrieval не находит» и не показывается как gap. Чище отделяет реальные пробелы от проблем поиска.

## v1.5.0 — 2026-05-12

Качество retrieval feedback loop — soft-fallback вместо тишины, актуальные gaps, дедупликация.

### Контекст

`gap_report` на боевой базе показал что **54% поисковых запросов возвращают пусто** — это не значит что 54% знаний отсутствуют, многие запросы — старые задачи которые давно закрыты статьями. Но клиенту видна тишина. Этот релиз — про честный feedback и реальные gaps.

### Added

- **Soft-fallback retrieval в whoosh_search** — когда top_score < 35 (старая граница где возвращалось `[]`), но >= 18 (новая нижняя граница), возвращается до 3 результатов с пометкой `confidence: low` ЕСЛИ они разделяют токены или стемы с запросом. Пользователь получает подсказку «возможно ты имел в виду это» вместо тишины. Ниже 18 — действительно пусто.
- **Smarter `gap_report`** — фильтрует «решённые» gaps через дополнительную semantic-similarity проверку (cosine ≥ 0.55 к существующим статьям). Если запрос дал пусто в BM25, но есть близкая по смыслу статья — это не gap, она просто не нашлась через keyword-поиск (отдельная проблема). Отчёт теперь показывает только **актуальные** пробелы.
- **`consolidate(project, min_sim)`** — новый MCP-tool. Попарное cosine similarity всех embeddings проекта, возвращает кандидаты на слияние с sim >= порога (default 0.78). НЕ мержит автоматически — список для ручной проверки. Полезно для борьбы с накоплением дубликатов из разных сессий.

### Tests

- 85/85 pass (было 82). +1 soft-fallback, +2 consolidate.

## v1.4.0 — 2026-05-12

Continuous memory через compact-границы, морфология поиска для русского, e2e-тесты.

### Added

- **`save_compact(project, summary)`** — новый MCP-tool для сохранения промежуточного резюме при сжатии контекста. Запись в `<project>/_compact_history.md` (FIFO 5). Подтягивается автоматически в `start_task` при continuation-запросах — даёт **continuous memory** через compact-границы.
- **PostCompact hook улучшен** — вместо «не забудь finish_task» теперь предписывает: определить проект → `save_compact(summary)` → finish_task если задача завершена. Минимизирует потерю контекста при сжатии.
- **Bilingual Snowball stemmer** в Whoosh-анализаторе — русские и английские словоформы редуцируются к базовым основам. `настройка/настройки/настроить → настр`, `deploys/deploying/deploy → deploy`. Boost recall для inflected запросов без false-positives. Нет внешних зависимостей (Whoosh уже включает Snowball).
- **End-to-end интеграционные тесты** — новый `tests/test_e2e.py`: full task lifecycle, secret roundtrip, case-insensitive project, continuation intent, route_project с cwd, compact history persists, tracking nested list. Защита от регрессий типа v1.1.2 YAML и v1.2.0 case-merge.

### Changed

- **Анализатор Whoosh:** `RegexTokenizer | LowercaseFilter` → `RegexTokenizer | LowercaseFilter | _BilingualStemFilter`. Старые индексы — нужен `reindex()` после деплоя для использования стемминга.

### Tests

- 82/82 pass (было 71). +11: 4 для config (стеммер), 7 e2e.

## v1.3.0 — 2026-05-12

Качество retrieval, расширенное автотегирование, observability базы знаний.

### Added

- **`route_project(text, cwd)`** — cwd теперь СИЛЬНЫЙ override-сигнал. Если basename рабочего каталога совпадает с существующим проектом → возвращается сразу со score 100, без content-match.
- **Reciprocal Rank Fusion (RRF)** в hybrid search — заменил weighted sum (0.4*BM25 + 0.6*semantic) на rank-based RRF (k=60). Не требует ручной калибровки весов, устойчив к выбросам шкал. Industry standard (Cormack et al., 2009; Microsoft, Vespa).
- **`gap_report(project, days, limit)`** — анализ audit-лога: запросы с пустым/слабым результатом (top_score<35), топ-темы по частоте, проекты-сироты (≤2 статей).
- **`stale_facts(project, warn_days)`** — поиск истекающих фактов: SSL-сертификаты по «valid until DATE», tracking-frontmatter (current.until/expires/valid_to), плюс секреты/cert/license старше 180 дней без обновления.
- **+14 правил автотегирования** (было 14, стало 28): добавлены `mysql`, `mssql`, `mongodb`, `vpn`, `dns`, `performance`, `security`, `testing`, `api`, `monitoring`, `backup`, `refactor`, `docs`. Расширена поддержка русского языка во всех правилах.

### Changed

- **`route_project`** — параметры `text` и `cwd` теперь оба опциональные (раньше требовался text). Минимум один.
- **Auto-tagging** активен в save_lesson (через который проходит и finish_task) — теги добавляются к переданным пользователем, а не заменяют их.

### Tests

- 71/71 pass (было 67). +4 новых теста: cwd-match, route_project с cwd, gap_report, stale_facts.

## v1.2.0 — 2026-05-12

Качество поиска контекста — case-insensitive проекты, confidence-aware search, continuation intent, авто-роутинг проекта.

### Проблема

При возврате в проект терялся контекст:
- Project с разным регистром (`MyProj` vs `myproj`) создавался как два разных проекта, контекст разделялся
- Search на стоп-фразы вроде «давай продолжим» возвращал случайные статьи — top-совпадение по семантической близости общеязыковых слов
- В новой сессии при continuation-фразе ассистент получал нерелевантную сессию из соседнего проекта вместо последней активности

### Fixed

- **Case-insensitive проекты (Jira-pattern)** — `normalize_project()` приводит имена к lowercase + trim. Применяется централизованно в диспетчере `tools.py` для всех MCP-tools.
- **Auto-migration на startup** — `merge_case_duplicates()` в lifespan: переименовывает / сливает case-варианты директорий при первом запуске, идемпотентно.
- **Confidence-aware search**:
  - Стоп-список (RU+EN, ~80 слов) включая continuation-глаголы.
  - `is_low_confidence_query()` — отбрасывает запросы с <2 content-токенами.
  - Search возвращает пустой результат вместо мусора: top-score < 35 → empty; relative threshold ужесточён 0.4 → 0.5.
- **Continuation intent в `start_task`** — если topic классифицирован как continuation: пропускает RAG, показывает топ-5 активного контекста + последнюю сессию проекта целиком (industry pattern: session restoration ≠ semantic search).

### Added

- `is_low_confidence_query()` + `_content_tokens()` + `_STOPWORDS` в search.py
- `normalize_project()` + `merge_case_duplicates()` в storage.py
- Новый tool `route_project(text)` — авто-определение проекта по тексту запроса (BM25 по именам и контенту, без хардкода в клиентах)
- 8 новых тестов

### Skill memory-autopilot

- Удалена статичная таблица проектов — клиент сам выбирает через `route_project()` или `list_projects()`
- Добавлено правило continuation: «продолжим по X» → start_task, НЕ search
- Подчёркнут lowercase-стиль

### Industry references

Cohere rerank thresholds, LangChain similarity_score_threshold retriever, REIC/Adaptive RAG (CRAG), OpenAI Agents SDK Sessions, Jira project key normalization.

## v1.1.2 — 2026-04-18

Fix YAML frontmatter parser — nested lists inside nested dicts.

### Fixed

- **_parse_frontmatter** теперь использует PyYAML (yaml.safe_load) — корректный парсинг на любой глубине вложенности
- Fallback custom-парсер улучшен: отслеживает `last_nested_dict_key`, корректно обрабатывает списки внутри вложенных словарей (раньше перезаписывал весь dict пустым list-ом)
- **save_tracking_article / list_tracking_articles** защищены от корруптных tracking-файлов: если `current` не dict — пропускается / регенерируется вместо краха
- Репродюсер бага: tracking-статья с `current.apk_files: [- file1, - file2]` раньше крашила `finish_task` с ошибкой `dictionary update sequence element #0 has length N; 2 is required`

### Added

- `pyyaml>=6.0` в requirements.txt
- 3 новых теста: nested list in nested dict, corrupted current survival, list_tracking skips corrupted

### Credit

Bug report + root-cause analysis — @Arelion999 (внутренний фикс).

## v1.1.1 — 2026-04-18

Умный детектор противоречий.

### Fixed

- **detect_contradictions** теперь учитывает:
  - **/24 подсеть** — IP в разных подсетях без общих сущностей = разные сервера (не конфликт)
  - **контекст сущности** — если в статьях упоминаются разные сущности (nginx vs postgres), одна подсеть с разными IP = не конфликт
  - **переезд между подсетями** с той же сущностью (nginx был 10.x, стал 192.168.x) → **предупреждение** (важный случай миграции)
- Добавлен список `_ENTITY_KEYWORDS` (nas, nginx, mikrotik, postgres, redis, memory-compiler, prod/dev и т.д.)
- 4 новых теста на детектор противоречий

## v1.1.0 — 2026-04-18

Скил memory-autopilot, консолидация систем памяти, оптимизация базы знаний.

### Новое

- **Скил memory-autopilot** — автоматическое управление памятью без ручных команд. Дерево решений для выбора tool (save_lesson/save_decision/save_runbook/save_from_template), автоопределение проекта, 4 фазы (старт → работа → сохранение → завершение). Устанавливается в `~/.claude/skills/memory-autopilot/`
- **Фаза 0 — классификация входа** — скил автоматически определяет тип сообщения (задача, факт, вопрос, ошибка) и выбирает действие
- **skills/ директория** — скилы поставляются с проектом, копируются в `~/.claude/skills/`

### Улучшено

- **CLAUDE.md упрощён** — правила выбора проекта/tool перенесены в скил, CLAUDE.md содержит только ссылку
- **Hooks сокращены** — SessionStart, UserPromptSubmit, PostToolUse удалены (заменены скилом). Остались Stop и PostCompact как страховка
- **docs/claude-desktop-setup.md** — обновлена документация: скил вместо ручных правил и хуков
- **Lint auto-fix** — нормализация 82 статей с разным регистром тегов (obsidian-import)

### Безопасность

- **Шифрование паролей** — 13 plain-text статей с паролями из Obsidian-импорта пересохранены через `save_secret` (AES-256), открытые версии удалены
- **MCP Memory очистка** — удалены plain-text пароли из встроенного knowledge graph, удалены дубли сущностей

### Оптимизация базы знаний

- **Перераспределение work** — 22 статьи из свалки `work` (88 шт) перемещены в профильные клиентские проекты
- **edit_article в скиле** — перед созданием новой статьи скил проверяет существование похожей, дописывает вместо дублирования
- **git_capture** — первый запуск для двух проектов (24 коммита → 7 статей)

### Удалено

- Скилы start-task и done-task (заменены memory-autopilot)
- Хуки SessionStart, UserPromptSubmit, PostToolUse (заменены скилом)

## v1.0.0 — 2026-04-15

Первый production-готовый релиз. 38 MCP tools, 54 теста. Полный auto-memory pipeline.

### Новое

- **VERSION file + /api/version endpoint** — версия видна в health, UI show badge, scripts/release.sh для auto-bump
- **Tracking articles (bi-temporal)** — snapshot current state с history (YAML frontmatter, type: tracking). 2 tool: `save_tracking`, `get_current`
- **Auto-extract facts при finish_task** — сервер сканирует content на regex (version/IP/port/URL), обновляет существующие tracking. Historical markers фильтруют прошлое. Safe (no auto-create)
- **Auto-update tracking/release** при теге `release` — из topic извлекается версия, обновляется tracking

### Из pre-release (v8 — v12)

#### Поиск
- Hybrid retrieval (BM25F + semantic + temporal decay)
- Cross-encoder reranking (BAAI/bge-reranker-base, 280MB, multilingual)
- start_task — фильтрация блоков по релевантности темы

#### Интеграции
- `ingest` — загрузка из URL или raw_text (HTML→markdown, без внешних deps)
- `import_obsidian` — импорт vault (frontmatter, теги, wiki-ссылки)
- `git_capture` — автосбор знаний из git-коммитов (dual mode: repo_path или git_log_raw)
- `knowledge_gap` — темы активные в git без покрытия в KB

#### Web UI
- Obsidian-style animated graph (drag/zoom/pan, top-K edges per node, orphan marker)
- Snippets с подсветкой совпадений в поиске
- Auto-scroll к первому match при expand
- Расшифровка ENC: на лету

#### Инфраструктура
- **Автодеплой на NAS** — cron + mtime watcher, container restart на изменение кода
- **Daily backup** — 7-day rotation tar.gz
- **Auto-lint weekly** — воскресенье 3 AM
- **Auto-compile daily** — 2 AM
- Security hardening (path traversal, since validation, DoS limits)

#### Безопасность
- MC_API_KEY auth (Bearer + cookie + query param)
- MC_ENCRYPT_KEY AES-256 для secret articles
- Audit log всех MCP tool calls
- Confirm required для destructive operations

#### Прочее
- set_project_deps — зависимости между проектами
- PostToolUse hook matcher для всех save_* tools
- 3 runbook'а в memory-compiler project

### Сломано намеренно

- Версии в CHANGELOG переименованы: было v8.0.0-v12.0.0 → теперь pre-release `0.8.0`-`0.12.0` (или просто исторические этапы до v1.0.0)
- Git history очищена от утечек через `git filter-repo`

## v0.12.0 (pre-release) — 2026-04-15

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
- **Project deps** — настроены примерные зависимости между проектами.
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
