# memory-compiler

**English** · [Русский](README.ru.md)

A personal knowledge base for AI assistants. An MCP server with hybrid search, auto-compilation and a web interface.

## Why

AI assistants don't remember context between sessions. memory-compiler fixes that: decisions, bugs and configurations are stored as markdown articles, indexed, and served over MCP or an HTTP API. The assistant looks up similar cases before a task and records new solutions afterwards.

## Quick start

```bash
pip install -r requirements.txt
mkdir -p knowledge
PROJECTS=my-project,infra,general python server.py
```

The server starts on `http://localhost:8765`. Or via Docker:

```bash
docker-compose up -d --build
```

### Connecting to Claude Code

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

### Connecting to Claude Code Desktop

Full walkthrough: [docs/claude-desktop-setup.en.md](docs/claude-desktop-setup.en.md) — MCP, the memory-autopilot skill, hooks, dependency setup.

## Features

### 46 MCP tools

**Search and read:**

| Tool | Description |
|------|-------------|
| `search(query, project)` | Hybrid BM25F + semantic search with temporal decay |
| `ask(question, project)` | Q&A — an answer with quotes from articles |
| `search_by_tag(tag, project)` | Every article carrying a given tag |
| `search_snippets(query, lang, project)` | Search across code blocks |
| `search_error(error_text, project)` | Search across tracebacks and error codes |
| `search_decisions(query, project)` | Search the decision log |
| `read_article(project, filename)` | Full article text |
| `get_context(project, query)` | Top relevant articles ahead of a task |
| `get_summary(project)` | Condensed project summary (~200 tokens) |

**Write and edit:**

| Tool | Description |
|------|-------------|
| `save_lesson(topic, content, project, tags)` | Save with a diff report, auto-merge, auto-tagging and contradiction detection |
| `save_decision(title, decision, reasoning, project, alternatives)` | Record an architectural decision (`alternatives` is optional) |
| `save_runbook(topic, steps, project)` | Create a step-by-step runbook with checkboxes |
| `save_from_template(template, fields, project)` | Create an article from a template (bug, setup, 1c, deploy, integration) |
| `save_secret(topic, content, project)` | Save an encrypted article (passwords, keys) |
| `edit_article(project, filename, content, append)` | Replace or append |
| `delete_article(project, filename)` | Delete an article |
| `get_runbook(project, filename)` | Fetch a runbook with its progress |
| `list_templates()` | List available templates |

**Sessions:**

| Tool | Description |
|------|-------------|
| `save_session(project, summary, ...)` | Save context for the next session |
| `load_session(project)` | Load context plus notifications about stale articles |
| `get_active_context(project)` | Feed of the last 10 actions |

**Temporal state:**

| Tool | Description |
|------|-------------|
| `save_tracking(project, entity, facts)` | Bi-temporal snapshot: current state plus history (versions, deploys, configs) |
| `get_current(project, entity)` | Read the current status out of a tracking article |

**Combined:**

| Tool | Description |
|------|-------------|
| `start_task(topic, project)` | Begin a task: search + session + context + decisions + runbooks |
| `finish_task(topic, content, project)` | Close a task: save the lesson and the session |

**Project management:**

| Tool | Description |
|------|-------------|
| `add_project(name)` | Create a new project |
| `remove_project(name)` | Delete a project along with all its articles |
| `list_projects()` | List projects with article counts |
| `set_project_deps(project, depends_on)` | Declare dependencies between projects |
| `get_project_deps(project)` | Read a project's dependencies |

**Maintenance:**

| Tool | Description |
|------|-------------|
| `ingest(url, project, ...)` | Pull knowledge from a URL (HTML→markdown) or from raw_text (PDF/documents) |
| `import_obsidian(vault_path, project, folder_mapping, dry_run)` | Import notes from an Obsidian vault (frontmatter, tags, wiki links) |
| `git_capture(repo_path, project, ...)` | Harvest knowledge from git commits (repo_path or git_log_raw) |
| `knowledge_gap(repo_path, project, days)` | Find topics present in git commits but missing from the knowledge base |
| `compile(dry_run, project, since)` | Compile daily logs into wiki articles |
| `lint(project, fix)` | Check for duplicates, staleness and tags |
| `reindex()` | Rebuild the index |
| `article_history(project, filename)` | Git history of an article |

**Contextual generation (contextual retrieval):**

| Tool | Description |
|------|-------------|
| `context_gaps(project, limit)` | List articles that still need AI section context (for backfill) |
| `save_contexts(project, filename, contexts)` | Write AI section context into frontmatter and re-embed the article |

### Hybrid search + reranking

Three layers of relevance:

- **BM25F** (Whoosh) — full-text with field weights (title x5, tags x3, body x1)
- **Semantic** — bi-encoder embeddings (default `paraphrase-multilingual-MiniLM-L12-v2`; the `EMBED_MODEL` env var switches to `BAAI/bge-m3` 1024d or `Alibaba-NLP/gte-multilingual-base`, and the cache invalidates itself on a model change)
- **Cross-encoder reranking** — `BAAI/bge-reranker-v2-m3`, **disabled by default** (`RERANK_ENABLED=1` turns it on). Measured over 132 real queries (`scripts/eval_retrieval.py`), it delivers no gain on this corpus at ×32 the latency — details below. SPLADE 3-way is optionally available via `SPLADE_ENABLED=true`

Channels are merged with **RRF** (Reciprocal Rank Fusion) rather than a weighted sum: it needs no calibration between BM25 and cosine similarity.

```
rrf(doc) = Σ_channel 1 / (RRF_K + rank_channel(doc))     # BM25 + semantic (+ SPLADE)
scaled   = rrf × 3000 × (0.7 + 0.3 × decay_factor)
final    = top_k (search: 8, get_context: 5)   # + cross_encoder.rerank if RERANK_ENABLED=1
```

Temporal decay pushes fresh and frequently used articles higher.

**Candidate selection** (`SEARCH_QUERY_GROUP`, `SEARCH_POOL`, `SEARCH_SCOPE_AWARE`) governs WHAT reaches the merge step. Diagnostics (`scripts/diag_retrieval.py`) over 140 real queries showed the main loss lived here, not in the weights:

| Knob | Default | What it used to be | Measurement |
|---|---|---|---|
| `SEARCH_QUERY_GROUP` | `or` | `and` — a document had to contain EVERY term; the BM25 channel was **empty on 48.6% of queries**, and on queries of 6+ words on 75% | MRR +0.046, recall@1 +8 queries out of 140 |
| `SEARCH_SCOPE_AWARE` | `true` | the project scope was applied AFTER taking the global top-20: across 43 projects that left ~2 BM25 candidates and ~9.7 semantic ones | recall@10 +9 queries |
| `SEARCH_POOL` | `100` | `limit*2` (=20): on 12.9% of queries the target sat in the wide pool but not the narrow one | folded into the row above |

Result over 140 queries (`scripts/eval_pipeline.py`): **MRR 0.4242 → 0.4914, recall@1 0.3071 → 0.3714, recall@5 0.5857 → 0.6714, recall@10 0.65 → 0.7286.** To roll back: `SEARCH_QUERY_GROUP=and SEARCH_POOL=20 SEARCH_SCOPE_AWARE=false` reproduces the old numbers exactly.

⚠️ **The figures above were taken with a pre-v1.32.0 harness.** On the cleaned set (`load_golden` with unreachable targets filtered out, n=132) the same code yields **MRR 0.5212, recall@1 0.3939, recall@10 0.7727**, while the old pipeline yields 0.4499. Search behaviour did not change; the harness was under-reporting, not the code. Versions may only be compared against each other under an identical harness version.

**Current baseline (harness v1.40.0, n=110): MRR 0.6013, recall@1 0.4818, recall@10 0.8364.** The latest shift is again the harness, not search: opening an article after a CHANGE OF WORK (a `finish_task`, `save_lesson`, `edit_article`… happened between the query and the open) is no longer credited to the preceding query. The signal is event-based rather than time-based, and it splits the data sharply: without a change of work the median gap is 12 s and no pair exceeds 10 minutes, whereas with one the median is 3.7 hours. That is why no time threshold was needed. On a single snapshot: without boundaries n=131 / MRR 0.5514, with boundaries n=110 / MRR 0.6013.

Previous baseline (harness v1.36.0, n=129): MRR 0.5512, recall@1 0.4419, recall@10 0.7597. That change was also harness-side: `search_by_tag` was dropped as a source because it does not rank at all — it walks files, matches the tag and returns hits in directory-traversal order. Its clicks were measuring the wrong pipeline. The set shrank by just 3 queries while MRR rose by 0.03: tag "queries" were shadowing genuine search queries and **claiming their opens**. The source-selection criterion was fixed at the same time — it used to be "the set scores better with it" (selection on the metric), and is now "the source exercises the pipeline under measurement" (verified against the code).

**What remains after v1.31.0** (breakdown from `scripts/diag_retrieval.py`, n=132): target ranked first for 39.4%, present but not first for 37.9%, below tenth place in the pool for 18.9%, candidate top-up failed for 0.8%, not found by any channel for 3.0%. In other words **candidate selection is closed, the cut-off was never at fault, and the entire remainder (56.8%) is ranking**: the target is in the pool, only the order is wrong. The remaining 3.0% is not a search defect but noise in the ground truth: an open attributed to a query made hours earlier.

**Ranking tuning** (`RRF_K`, `DECAY_WEIGHT`, `BM25_TITLE_B`/`BM25_TAGS_B`/`BM25_BODY_B`, `RRF_WEIGHT_BM25`/`RRF_WEIGHT_SEMANTIC`) covers the weights INSIDE the selected pool. They apply at query time, so an experiment takes about a minute. Grid: `scripts/eval_ranking.py`.

Channel weights and field boosts were validated on the cleaned set (n=132) — **no improvements, defaults kept**. Field boosts: `title x3 tags x2` gives MRR 0.5382 against 0.5212, i.e. 3 queries out of 132 — noise; `title x8` is already worse than the baseline. Channel weights: disabling semantics lifts MRR to 0.5493 and recall@1 to 0.4470, but the bootstrap CI is [−0.0158, +0.0738] (zero inside), and per query **27 get worse against 25 better** — the win rests on a handful of large improvements and would have been a regression in practice. ⚠️ At n=132 only shifts from ~0.024 MRR are distinguishable against an available ceiling of 0.31 — beyond that the limit is the size of the set, not the supply of ideas.

A grid run over 137 real queries (2026-07-19) found no improvements — **defaults left alone**: `b=1.0` wins a single query out of 137, i.e. noise. The "flat zone" of `RRF_K` at 10/20/60 turned out not to be a property of the data but an **identity**: with a two-channel top, the cut-off `max(top×0.5, 32)` equals exactly the score of a single-channel rank-1 document (`3000/(K+1)`), so the surviving set does not depend on `K`. It breaks at `K=120`, where a single-channel document physically cannot reach the absolute floor of 32 (`3000/121 = 24.8`) — hence recall@10 dropping to 0.49. ⚠️ When changing `RRF_K`, keep this coupling with the constant 32 in mind.

⚠️ **The measurement noise floor is up to 4 queries out of 140 on recall@1.** The order of equally scored documents used to come from iterating a `set()`, i.e. from string-hash randomisation: the same code under different `PYTHONHASHSEED` values produced MRR between 0.4847 and 0.4970. Since v1.31.0 the order is deterministic (raw score, ties broken by path) and runs match byte for byte. Anything smaller than this floor must not be accepted as an improvement.

⚠️ **Temporal decay cannot be measured with this harness** — a limitation of the method, not a defect. `track_access` updates `last_accessed` on every result, and the golden set is by construction made of articles that were opened, so every target sits in the decay≈1.0 zone and shares the same multiplier (absolute scores move, ordering does not). `DECAY_WEIGHT` must not be tuned against these numbers: they are insensitive to it.

**Why the reranker is off.** Measured by `scripts/eval_retrieval.py` over 132 real queries from the audit log (ground truth being the articles actually opened after a search):

| Configuration | MRR | recall@1 | recall@5 | recall@10 | time/query |
|---|---|---|---|---|---|
| hybrid | **0.4634** | **0.3636** | **0.5833** | 0.6515 | **0.45 s** |
| hybrid + rerank | 0.4535 | 0.3561 | 0.5758 | 0.6515 | 14.5 s |

There is no gain (the shift is one query per level, i.e. noise) at 32× the cost. `recall@10` matched structurally: the candidate pool equals 10, so the reranker merely permutes the same 10 documents.

**Reopened and closed again (v1.35.0).** The caveat above — "worth enabling together with a larger pool" — became testable after v1.31.0, when the pool grew to 100. The measurement was repeated on the cleaned set (n=132): without rerank MRR **0.5151** / recall@1 **0.3939**; reranking the top-20 by preview gives 0.4769 / 0.3485 (**0.038 MRR worse than baseline**, twice the distinguishability threshold) at 8.0 s/query; reranking the top-20 by full body gives 0.5073 / 0.3939, i.e. within noise and with an exact recall@1 match, at 22.2 s/query. The per-query tally is +27/−28 — a perfect zero. A truncated preview explains the HARM but not the absence of benefit. The conclusion got stronger: there is no gain even on the pool for which the reranker was proposed to return — and that is on 20 cores, not on the NAS.

### Contextual Retrieval

Before embedding, every chunk is prefixed with a context header that situates the fragment inside its document (Anthropic's method) — a query finds the right section even when its body contains none of the keywords.

- **Phase 1 (metadata):** a context header `[project · title · section · tags]`, automatic, for every article.
- **Phase 2 (AI context):** in multi-section articles each section gets an AI-generated context phrase (one line per section), stored in the YAML frontmatter (`contexts:`) and used in place of the metadata. It is populated through two tools:
  - `context_gaps(project, limit)` — returns articles that need context (multi-section, non-secret, no `contexts:`), together with their sections, body and instructions. Append-log `### {date}` entries are ignored.
  - `save_contexts(project, filename, contexts)` — validates the headings, writes `contexts:` into the frontmatter and incrementally re-embeds the article.

The embedding line format is versioned (`context_format_version` in `.embeddings.pkl`) — on a mismatch the cache is rebuilt from scratch.

### Adaptive chunking

The window size is **fitted to the length of the section**, so that the section fits into the chunk budget whole instead of being cut off at a fixed ceiling.

This is not cosmetic: before v1.28.0 a hard ceiling of `600 × 4 = 2400` characters per section applied, and everything beyond it was **silently discarded**. Measured: **26.3% of the text in the base** (1,252,908 characters) never reached the embeddings at all, while 70.7% of real queries led to a truncated article.

Three configurations over 133 real queries (`scripts/eval_chunking.py`):

| Chunking | MRR | recall@5 | recall@10 | Chunks | Coverage |
|---|---|---|---|---|---|
| fixed 600×4 (pre-v1.28.0) | 0.4599 | 0.5865 | 0.6541 | 8034 | ~72% |
| adaptive, budget 4 | 0.4580 | 0.6165 | 0.6842 | 6771 | ~85% |
| **adaptive, budget 16** (current) | **0.4626** | **0.6165** | **0.6842** | **7219** | **~93%** |

Adaptive mode produces **fewer** chunks at **greater** coverage: a wide window covers a section in fewer windows and picks up the tail along the way. The gain is honestly modest — +4 queries out of 133 into the top-5/top-10, with MRR unmoved: what gets fixed is "not found at all", not "not found first".

Tuning: `CHUNK_ADAPTIVE` (default `true`), `CHUNK_WINDOW_MAX` (1200 — the e5-base input limit, ~512 tokens), `CHUNK_SUBCHUNKS_CAP` (16). Changing any of them changes the chunk texts and invalidates `.embeddings.pkl` → a full rebuild.

### Backfilling AI context

AI context (phase 2) is layered in gradually and is not required for search to work — an article without it falls back to phase 1 metadata. To populate an existing base, ask your assistant to run the backfill; it is a "tool dance" loop the assistant performs on its own:

```
While context_gaps(project) returns articles:
  1. context_gaps(project, limit=5)      — fetch a batch of gap articles
  2. for each section — write one phrase (≤25 words) situating it in the document
  3. save_contexts(project, filename, [{heading, context}, …])
  4. repeat until remaining == 0
```

The backfill is interruptible and resumable (the state is the frontmatter itself): new articles are picked up on the next pass. For a large base it parallelises across projects (distinct files, no conflicts). Example request to an assistant: "run the AI context backfill for the infra project" or "for every project".

### Smart saving

`save_lesson` automatically performs:

1. A write into the daily log (audit trail)
2. Auto-tagging (14 regex rules)
3. A semantic lookup for an existing article — merge instead of duplicate
4. Contradiction detection (IPs, versions, URLs, ports) — role-aware: distinct IP roles (private vs public) and well-known DNS (8.8.8.8, 1.1.1.1, …) produce no false positives; CIDR notation is not compared as a host
5. Cross-references in related articles
6. An update to the active-context feed
7. Extraction of git references (commits, issues, tags)
8. A git commit

### Web interface

A built-in mobile-friendly UI at `http://localhost:8765`. Dark and light themes.

- **Search** — snippets with match highlighting (yellow), a project filter, clickable tags, markdown rendering, breadcrumbs, auto-scroll to the first match
- **Answers** — a question against the base: the same pipeline as the `ask` tool (wide candidate pool → fallback across all projects; cross-encoder rerank optional, see above), returning relevant fragments annotated with their source article. There is no generation: the server hosts no LLM, so this is retrieval with sources rather than an invented answer
- **Command palette** — `Ctrl+K` (and `Cmd+K`) from any tab: debounced search, `↑`/`↓` to navigate, `Enter` to open expanded, `Esc` to close
- **Similar** — a sidebar of semantically close articles next to an expanded one: a proximity score, click to jump, and a "follow/frozen" button that pins the list while navigating
- **Version timeline** — tracking articles get a slider over their bi-temporal snapshots: scroll a fact's history with validity intervals and changed fields highlighted
- **Add** — an entry form
- **Graph** — an Obsidian-style animated graph: drag, zoom, pan, project filter, hover highlighting of links
- **Compilation** — preview and run
- **Analytics** — most accessed, never used
- **Audit** — a log of every MCP call

### REST API

20 REST endpoints (`/api/*`): health, version, login/auth, search, answers from the base (retrieval with sources), semantically similar articles, a fact's version timeline, saving, article CRUD, projects, the knowledge graph, analytics, tags, compilation (preview/run), export, audit, logs. Plus `/` (Web UI), `/login` and `/sse` (the MCP transport).

### Automation

- Daily logs auto-compiled at 02:00
- Git versioning of every change
- An embeddings cache for a fast start
- Notifications about stale articles

## Security

Three layers of protection, each enabled by an env variable:

| Layer | Variable | What it does |
|-------|----------|--------------|
| Authorisation | `MC_API_KEY` | Login page + 30-day cookie, Bearer token, `?key=` in the URL |
| Encryption | `MC_ENCRYPT_KEY` | AES-256 for secret articles (save_secret) |
| Audit | automatic | A log of every MCP call, "Audit" tab in the Web UI |

With no variables set, access is open (backwards compatibility). More detail: [docs/security.md](docs/security.md)

## Backups

A two-link scheme (NAS → PC) whose links are independent:

- **NAS** — a daily `tar` of the `knowledge/` directory (`scripts/mc-backup.sh`, cron 04:00, 7-day rotation). Archives land in `backups/`.
- **PC** — an independent copy of the archives plus a snapshot of `.env` (`scripts/mc-backup-pull.ps1`, Task Scheduler job "memory-compiler backup pull", 05:00). Retention is 30 days for dailies, with monthly slices (`-01`) kept indefinitely. A restore drill verifies the integrity of the newest archive (`scripts/mc-backup-verify.ps1`).

### Installation (Windows, the PC link)

The `Source`/`EnvFile` paths default to values derived from the script's own location (`<repo>/scripts/`), so the parameters can be omitted. Registering the daily job (replace `<repo>` with the path to your clone):

```powershell
schtasks /Create /TN "memory-compiler backup pull" /SC DAILY /ST 05:00 /F `
  /TR "pwsh.exe -NoProfile -ExecutionPolicy Bypass -File `"<repo>\scripts\mc-backup-pull.ps1`" -Verify"
```

A one-off run and check: `pwsh -File scripts\mc-backup-pull.ps1 -Verify` (log at `C:\Backups\memory-compiler\pull.log`). To restore: unpack the desired `archives\knowledge-*.tar.gz` into the memory-compiler directory, put back the matching `secrets\.env-*` (it holds `MC_ENCRYPT_KEY`), and bring the container up.

## Project layout

```
memory-compiler/
├── server.py                  # Entry point (11 lines)
├── memory_compiler/
│   ├── __init__.py
│   ├── config.py              # Constants, metadata, shared state
│   ├── search.py              # Whoosh BM25F + semantic + reranking + contextual retrieval
│   ├── storage.py             # Articles, git, utilities, auto-tagging
│   ├── handlers.py            # MCP tool implementations
│   ├── tools.py               # MCP tool registration, dispatcher, resources, prompts
│   ├── api.py                 # REST endpoints, Starlette app
│   ├── ui.py                  # Web UI HTML template
│   ├── markdown_render.py     # Server-side MD rendering (markdown-it-py + Pygments + nh3)
│   ├── obs.py                 # Observability: structured logging, request_id, error counters
│   ├── retrieval_eval.py      # Search evaluation: behavioural golden set + known-item, recall@k/MRR
│   └── maintenance.py         # One-off maintenance passes
├── tests/                     # pytest (offline: HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1)
│   ├── conftest.py            # Fixtures (tmp knowledge dir)
│   └── test_*.py              # search, storage, handlers, contextual retrieval, web, concurrency …
├── skills/
│   └── memory-autopilot/
│       └── SKILL.md           # The memory autopilot skill for Claude Code
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## Tests

```bash
pip install pytest pytest-asyncio
pytest tests/ -v
```

## Stack

| Component | Technology |
|-----------|------------|
| Server | Python 3.12, MCP SDK, Starlette, Uvicorn |
| Full-text search | Whoosh (BM25F) |
| Semantic search | sentence-transformers |
| Storage | Markdown + Git |

## Workflow

### With memory-autopilot (recommended)

The `memory-autopilot` skill automates the whole cycle — it looks up context, picks the tool and saves the result. The user simply works while memory is managed invisibly.

Installation: copy `skills/memory-autopilot/SKILL.md` to `~/.claude/skills/memory-autopilot/SKILL.md`.

### Manual mode

```
1. Task begins     → start_task("topic")        ← search + session + context
2. In progress     → ask("how do I configure X?")
3. Solution found  → finish_task(...)           ← lesson + session
```

## Configuration

Projects are created dynamically through `add_project()`, or automatically on `save_lesson()`. Each project is its own directory under `knowledge/`, holding markdown articles.

Initial projects can optionally be declared through an environment variable:

```bash
PROJECTS=backend,infra,general python server.py
```

### Description language

`MC_LANG=en` switches tool and prompt descriptions to English — the text an MCP client shows the model when it picks a call. Defaults to `ru`; any unknown value also yields Russian.

⚠️ Server responses stay in Russian: only the descriptions are translated. This is a deliberate half-step — descriptions are what the model reads when choosing a tool, and they matter more.

### Git Capture

Two modes for harvesting knowledge from git:

**Mode 1 — repo_path** (the server reads git directly):
```bash
# .env
GIT_REPOS_PATH=/path/to/your/repos
```

```
git_capture(repo_path="/repos/my-project", project="myapp", auto_save=true)
```

**Mode 2 — git_log_raw** (the client passes the output of git log):
```bash
# Claude runs locally:
git log --format="%H|%s|%an|%aI" --numstat --since="7 days ago"
# and passes the output in the git_log_raw parameter
```

Repeat calls with `repo_path` automatically process only new commits.

## License

[MIT](LICENSE)
