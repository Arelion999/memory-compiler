"""
Tool handler implementations for memory-compiler MCP server.
All async functions return list[TextContent].
"""
import asyncio
import os
import re
import shutil
import subprocess
from contextvars import ContextVar
from datetime import datetime, timedelta, date
from typing import Optional

import numpy as np
from mcp.types import TextContent, ResourceLink

from memory_compiler.config import (
    KNOWLEDGE_DIR, PROJECTS, track_access, article_meta, save_article_meta,
    _discover_projects, is_secret_article,
)
from memory_compiler.search import (
    whoosh_search, index_document, embed_document,
    rebuild_index, rebuild_embeddings,
)
# Модульный импорт: _embeddings/_embed_texts переприсваиваются в rebuild_embeddings
# (свап нового dict). Импорт `from search import _embeddings` заморозил бы ССЫЛКУ на
# старый объект — delete/remove чистили бы устаревший dict, а semantic-поиск ходил по
# новому → удалённая статья оставалась бы фантомом. Обращаемся через модуль.
import memory_compiler.search as _search
from memory_compiler import embed_queue
from memory_compiler.storage import (
    today_log_path, project_dir, find_existing_article,
    merge_into_article, is_duplicate_entry, make_preview,
    article_title_tags, parse_meta_value, _parse_frontmatter,
    regenerate_index, git_commit,
    update_active_context,
    append_session, append_note, latest_session, RUNNING_MARK, add_question, close_questions, open_questions_list,
    relevant_reflections,
    auto_tags, extract_secret_identifiers, extract_git_refs, format_git_refs,
    update_cross_references,
    extract_snippets, extract_errors, TEMPLATES,
    read_project_deps, write_project_deps,
    encrypt_content, decrypt_content, is_encrypted,
    log_event, mark_dependents,
    extract_reflections, append_reflections,
    safe_article_path, safe_project_dir, make_slug,
    strip_code_blocks,
)

_CTX_INSTRUCTIONS = (
    "Для каждой секции из pending напиши ОДНО краткое предложение (≤25 слов, рус.), "
    "ситуирующее секцию в документе: что покрывает и как связана с остальным. Не повторяй "
    "заголовок дословно; добавь различающий контекст (проект, сущность, связь). "
    "sections — полная структура статьи для ситуирования; full_text содержит шапку и "
    "тела только pending-секций. Уже наполненные секции не переписывай. Верни через "
    "save_contexts как contexts=[{heading, context}, …]."
)
# Бюджет символов full_text на ОДНУ статью; смысл — размер батча (limit=5 → ~40к).
# Тратится только на полезное: шапку и тела pending-секций (без frontmatter, без
# уже наполненных и без append-лог секций), делится water-fill'ом — см. context_gaps.
_CTX_FULLTEXT_CAP = 8000


# ─── save_lesson ─────────────────────────────────────────────────────────────


async def _index_embed(text: str, filename: str, project: str) -> None:
    """Текстовый индекс — сразу, вектор — фоном.

    B2: обе операции уходят с event loop, иначе `embed_document` (encode модели плюс
    `_index_lock`) морозил весь сервер — /api/health, параллельные MCP-вызовы, SSE.
    save_article_meta/git_commit НАМЕРЕННО остаются на loop: перенос save_article_meta
    в поток дал бы гонку с track_access (loop мутирует article_meta ↔ поток итерирует
    его в json.dumps → 'dict changed size during iteration').

    ⚠️ РАЗДЕЛЕНИЕ СИНХРОННОГО И ФОНОВОГО (v1.59.0). Уход в поток снимал нагрузку с
    сервера, но КЛИЕНТ всё равно ждал инференс: замер показал медиану записи 7081 мс
    против 275 мс у чтения, хвост до 183 с и 1.1% записей в клиентском таймауте.
    Профиль: whoosh 71 мс, git 36 мс, поиск дублей 4 мс, encode статьи — 1855 мс.
    Поэтому whoosh остаётся в ожидании (статья находится текстом сразу), а вектор
    считает фоновый воркер. Плата — несколько секунд, пока статья не видна
    семантическому поиску. MC_EMBED_ASYNC=0 возвращает прежнее поведение.
    """
    await asyncio.to_thread(index_document, text, filename, project)
    if embed_queue.ASYNC_ENABLED:
        embed_queue.enqueue(text, filename, project)
    else:
        await asyncio.to_thread(embed_document, text, filename, project)


async def save_lesson(topic: str, content: str, project: str, tags: list = None, force_new: bool = False) -> list[TextContent]:
    try:
        safe_project_dir(project)
    except ValueError as e:
        return [TextContent(type="text", text=f"❌ Небезопасный параметр: {e}")]
    tags = tags or []
    # Автотегирование — дополнить пользовательские теги автоматическими
    auto = auto_tags(content, topic)
    existing_lower = {t.lower() for t in tags}
    tags = tags + [t for t in auto if t not in existing_lower]
    now = datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M")
    slug = make_slug(topic)

    # 1. Always append to daily log (audit trail)
    log_path = today_log_path()
    separator = "\n---\n" if log_path.exists() and log_path.stat().st_size > 0 else ""
    entry = f"""{separator}\n## {topic}\n\n**Время:** {ts}\n**Проект:** {project}\n**Теги:** {', '.join(tags) if tags else '—'}\n\n{content}\n"""
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(entry)

    # 2. Find existing article or create new
    existing = None if force_new else find_existing_article(topic, content, project)

    if existing:
        # Update existing article
        old_text = existing.read_text(encoding="utf-8")
        old_line_count = len(old_text.splitlines())
        merge_into_article(existing, content, tags, ts)
        article_path = existing
        article_text = article_path.read_text(encoding="utf-8")
        new_text = article_path.read_text(encoding="utf-8")
        new_line_count = len(new_text.splitlines())
        diff_lines = new_line_count - old_line_count
        # Find new tags
        old_tags_set = set()
        for line in old_text.splitlines()[:10]:
            if line.startswith("**Теги:**"):
                old_tags_set = {t.strip().lower() for t in line.split(":", 1)[1].strip().split(",") if t.strip() and t.strip() != "—"}
        new_tags_added = [t for t in tags if t.lower() not in old_tags_set]
        diff_info = f" (+{diff_lines} строк" if diff_lines > 0 else f" ({diff_lines} строк"
        if new_tags_added:
            diff_info += f", теги: +{', +'.join(new_tags_added)}"
        diff_info += ")"
        action = f"\U0001f504 Обновлено: {project}/{article_path.name}{diff_info}"
    else:
        # Create new article
        article_path = safe_project_dir(project) / f"{slug}.md"
        # Handle name collision: подобрать ПЕРВОЕ свободное имя (дата, затем счётчик).
        # Раньше проверялся только один запасной путь → 3-е сохранение за день с тем же
        # slug перезаписывало 2-е (потеря урока).
        if article_path.exists():
            base = safe_project_dir(project)
            day = now.strftime('%Y%m%d')
            article_path = base / f"{slug}_{day}.md"
            n = 2
            while article_path.exists():
                article_path = base / f"{slug}_{day}_{n}.md"
                n += 1
        article_text = f"""# {topic}\n\n**Дата:** {ts}\n**Проект:** {project}\n**Теги:** {', '.join(tags) if tags else '—'}\n\n## Записи\n\n### {ts}\n{content}\n"""
        article_path.write_text(article_text, encoding="utf-8")
        await asyncio.to_thread(regenerate_index)
        action = f"\u2705 Создано: {project}/{article_path.name}"

    # 3. Git-линковка — извлечь и добавить git-ссылки
    git_refs = extract_git_refs(content, topic)
    if git_refs:
        refs_text = format_git_refs(git_refs)
        article_text = article_path.read_text(encoding="utf-8")
        if "## Git-ссылки" not in article_text:
            article_text = article_text.rstrip() + f"\n\n## Git-ссылки\n{refs_text}\n"
        else:
            # Обновить существующую секцию — дополнить новыми
            existing_end = article_text.index("## Git-ссылки") + len("## Git-ссылки")
            article_text = article_text[:existing_end] + f"\n{refs_text}\n"
        article_path.write_text(article_text, encoding="utf-8")

    # 4. Update search indexes
    article_text = article_path.read_text(encoding="utf-8")
    await _index_embed(article_text, article_path.name, project)

    # 6. Обнаружение противоречий УБРАНО (v1.54.1). Предупреждение уходило только в
    # текст ответа — статья не помечалась, задача не заводилась, в журнал не писалось,
    # то есть сигнал жил ровно до конца реплики. Задачу «какое значение актуально»
    # решает tracking ниже (шаг 10): не предупреждает, а обновляет, и знает, что
    # новее. Подробности и цена — в docstring storage.detect_contradictions.

    # 7. Cross-references
    saved_key = f"{project}/{article_path.name}"
    update_cross_references(topic, project, saved_key)

    # 8. Active Context
    update_active_context(project, topic, content)

    # 9. Track access
    track_access([saved_key])

    # 10. Auto-update existing tracking articles (version, IP, port, URL)
    tracking_updates = []
    tags_lower = {t.lower() for t in tags}

    # Release tag → ensure tracking/release exists and update
    if "release" in tags_lower or "релиз" in tags_lower:
        # Версию берём ГАРДИРОВАННЫМ extract_facts_from_text (IP-коллизия, дата-фильтр,
        # cue-логика), НЕ наивным regex v?(\d+\.\d+\.\d+): release-заметка с IP вида
        # 192.0.2.100 иначе давала «версию» 192.0.2 (первые 3 октета) и, т.к.
        # 192 > любого мажора, guard не считал это откатом и затирал трекер вживую.
        from memory_compiler.storage import extract_facts_from_text, save_tracking_article
        from memory_compiler import versioning
        versions = (extract_facts_from_text(topic).get("version")
                    or extract_facts_from_text(content).get("version"))
        if versions:
            version = versioning.max_version(versions)
            r = save_tracking_article(project, "release", {"version": version}, guard_version_regression=True)
            if r["action"] != "unchanged":
                tracking_updates.append({
                    "entity": "release",
                    "old": r["old_current"],
                    "new": r["new_current"],
                    "path": r["path"],
                })

    # General auto-update: scan content for facts matching existing tracking
    from memory_compiler.storage import auto_update_tracking
    auto_updates = auto_update_tracking(project, content, topic)
    tracking_updates.extend(auto_updates)

    # Re-index updated tracking articles
    for upd in tracking_updates:
        fpath = KNOWLEDGE_DIR / upd["path"]
        if fpath.exists():
            updated_text = fpath.read_text(encoding="utf-8")
            await _index_embed(updated_text, fpath.name, project)

    # 11. Project journal (Karpathy LLM Wiki pattern)
    log_event(project, "save_lesson", f"{topic} → {article_path.name}")

    # 12. Git commit
    await asyncio.to_thread(git_commit, f"save: {topic} [{project}]")

    result = action
    if git_refs:
        refs_summary = ", ".join(f"{k}: {', '.join(v)}" for k, v in git_refs.items())
        result += f"\n\U0001f517 Git: {refs_summary}"
    for upd in tracking_updates:
        # Show what changed
        old = upd["old"]
        new = upd["new"]
        changed_keys = [k for k in new if k != "since" and old.get(k) != new.get(k)]
        if changed_keys:
            diff = ", ".join(f"{k}: {old.get(k, '—')} → {new.get(k)}" for k in changed_keys)
            result += f"\n🔄 tracking/{upd['entity']}: {diff}"
    return [TextContent(type="text", text=result)]


# ─── get_context ─────────────────────────────────────────────────────────────


async def get_context(project: str, query: str = None) -> list[TextContent]:
    if query:
        # Wider pool for reranker — top results refined by cross-encoder
        results = await _whoosh_async(query, project=project, limit=10)
        cross = await _whoosh_async(query, project="all", limit=10) if project != "all" else []
        seen = {r["file"] for r in results}
        for r in cross:
            if r["file"] not in seen and r["project"] != project:
                results.append(r)
                if len(results) >= 15:
                    break
        if not results:
            return [TextContent(type="text", text=f"Ничего не найдено по '{query}' в {project}.")]
        results = await _rerank_async(query, results, top_k=5)
        out = [f"# Контекст: {project} (query: {query})\n"]
        for r in results:
            preview = "\n".join(r["preview"].splitlines()[:8])
            scores = f"score: {r['score']}"
            if "rerank_score" in r:
                scores += f", rerank: {r['rerank_score']:.2f}"
            out.append(f"---\n### [{r['project']}] {r['title']} ({scores})\n{preview}\n")
        return [TextContent(type="text", text="\n".join(out))]
    else:
        proj_path = safe_project_dir(project)
        articles = sorted(proj_path.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not articles:
            return [TextContent(type="text", text=f"База знаний по '{project}' пуста.")]
        selected = [(a, a.read_text(encoding="utf-8")) for a in articles[:5]]
        out = [f"# Контекст: {project}\n"]
        for path, text in selected:
            # make_preview, как в ветке с query выше: срез [:8] по сырому файлу
            # отдавал YAML-frontmatter — из 24 строк превью контенту принадлежало 0.
            # Один и тот же инструмент отвечал по-разному с запросом и без него.
            preview = make_preview(text, n=8)
            out.append(f"---\n### {path.stem}\n{preview}\n")
        return [TextContent(type="text", text="\n".join(out))]


# ─── search ──────────────────────────────────────────────────────────────────

# Бюджет времени на cross-encoder rerank. Если модель холодная (лениво грузится
# при первом запросе на NAS) или кандидатов много — predict может не уложиться в
# MCP-таймаут клиента и весь запрос падал в -32001, теряя уже найденные hybrid-хиты.
# По истечении бюджета отдаём результат БЕЗ rerank (мягкая деградация: hybrid-порядок
# хуже reranked, но это лучше пустой ошибки). Настраивается env SEARCH_RERANK_BUDGET_S.
SEARCH_RERANK_BUDGET_S = float(os.environ.get("SEARCH_RERANK_BUDGET_S", "20"))
# Размер пула кандидатов под reranker. Было 20, но bge-reranker-v2-m3 на слабом CPU
# (NAS J4125) реранкает ВСЕ кандидаты одним predict — 20 пар часто не укладывались в
# бюджет SEARCH_RERANK_BUDGET_S и rerank отваливался (мягкая деградация без reranker).
# 10 даёт ~2x меньше forward-pass'ей при небольшой потере recall. Тюнится env.
SEARCH_CANDIDATE_POOL = int(os.environ.get("SEARCH_CANDIDATE_POOL", "10"))
# Cross-encoder reranker ВЫКЛЮЧЕН по умолчанию. Замерено 2026-07-18 харнессом
# scripts/eval_retrieval.py на 132 РЕАЛЬНЫХ запросах аудит-лога (ground truth —
# статьи, которые действительно открыли после поиска):
#   hybrid         MRR 0.4634  recall@1 0.3636  recall@5 0.5833  recall@10 0.6515  [0.45 с/запрос]
#   hybrid+rerank  MRR 0.4535  recall@1 0.3561  recall@5 0.5758  recall@10 0.6515  [14.5 с/запрос]
# Прироста нет (сдвиг ровно по ОДНОМУ запросу на уровень — шум) при цене ×32.
# recall@10 совпал структурно: пул кандидатов = 10 и меряем на @10, значит reranker
# лишь переставляет те же 10 документов и новых внести не может. Польза cross-encoder
# в литературе берётся из переранжирования БОЛЬШОГО пула (50-100) в короткий топ —
# вытащить релевантное с 40-го места; при пуле 10 спасать нечего, а поднять пул на этом
# CPU невозможно (14.5 с за 10 кандидатов → ~70 с за 50). Включить: RERANK_ENABLED=1 —
# осмысленно только вместе с бо́льшим SEARCH_CANDIDATE_POOL и/или лёгкой моделью,
# и обязательно с повторным замером тем же харнессом.
RERANK_ENABLED = os.environ.get("RERANK_ENABLED", "false").lower() in ("1", "true", "yes")


async def _whoosh_async(query: str, project: str = "all", limit: int = 10) -> list[dict]:
    """whoosh_search в потоке: он CPU-тяжёлый (semantic dot-product по всем эмбеддингам +
    при холодном старте ленивая загрузка embed-модели). На event loop он замораживал весь
    сервер (/api/health, параллельные запросы). Общий помощник для всех async-хендлеров."""
    return await asyncio.to_thread(whoosh_search, query, project=project, limit=limit)


async def _rerank_async(query: str, results: list[dict], top_k: int) -> list[dict]:
    """rerank под бюджетом времени в потоке. При таймауте/ошибке — best-effort: отдаём
    hybrid-результаты как есть (обрезанные до top_k) вместо -32001. wait_for отменяет
    ожидание, но фоновый поток допишет predict вхолостую — результат уже у пользователя.

    При выключенном reranker'е выходим СРАЗУ, не запуская поток. Выставить
    SEARCH_RERANK_BUDGET_S=0 было бы недостаточно: wait_for снял бы ожидание, но
    фоновый поток всё равно досчитал бы predict и сжёг те же ~14.5 с CPU впустую."""
    if not RERANK_ENABLED:
        return results[:top_k]
    from memory_compiler.search import rerank
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(rerank, query, results, top_k=top_k),
            timeout=SEARCH_RERANK_BUDGET_S,
        )
    except (asyncio.TimeoutError, Exception):
        return results[:top_k]


def _resource_links(items) -> list[ResourceLink]:
    """Построить ResourceLink на memory://<проект>/<файл> для результатов поиска.
    items: iterable dict'ов с ключами project/file (+ optional title/desc). Секреты
    (secret_) пропускаются — как ресурс недоступны; дубли по project/file схлопываются.
    Клиент (Claude Desktop) рендерит их как кликабельные ссылки в выводе инструмента."""
    links: list[ResourceLink] = []
    seen: set[str] = set()
    for r in items:
        project, filename = r.get("project"), r.get("file")
        if not project or not filename or filename.startswith("secret_"):
            continue
        key = f"{project}/{filename}"
        if key in seen:
            continue
        seen.add(key)
        links.append(ResourceLink(
            type="resource_link",
            uri=f"memory://{project}/{filename}",
            name=key,
            title=r.get("title", "") or "",
            description=r.get("desc", "") or "",
            mimeType="text/markdown",
        ))
    return links


# Структурированная выдача search собирается ЗДЕСЬ, а не из resource-ссылок в
# tools.py. Ссылок на секреты нет НАМЕРЕННО (как ресурс секрет недоступен — это
# верная политика, см. read_resource), но сборка структуры из ссылок молча теряла
# секретные попадания: панель MCP Apps не показывала их вовсе, и счётчик «найдено»
# расходился с текстовой выдачей того же вызова. Ни исключения, ни предупреждения.
# Передаём через ContextVar, а не модульную переменную: он привязан к задаче,
# поэтому параллельные вызовы не перепутают выдачу.
search_payload_var: ContextVar[dict | None] = ContextVar("search_payload", default=None)


async def search(query: str, project: str = "all") -> list[TextContent]:
    # Industry pattern 2026: fetch wider candidate pool, then cross-encoder rerank to final K.
    # Bigger N for reranker → +25-40% precision over hybrid alone (RAG benchmarks).
    results = await _whoosh_async(query, project=project, limit=SEARCH_CANDIDATE_POOL)

    # Авто-фолбэк на project=all: узкий скоуп часто промахивается по общей сущности,
    # физически лежащей в другом проекте (напр. канал уведомлений / общий креденшл).
    # Вместо «Ничего не найдено» переспрашиваем по всем проектам и помечаем выдачу.
    fallback_all = False
    if not results and project != "all":
        results = await _whoosh_async(query, project="all", limit=SEARCH_CANDIDATE_POOL)
        fallback_all = bool(results)

    if not results:
        # Пустую выдачу тоже объявляем явно: иначе панель прочитала бы payload
        # предыдущего вызова и показала чужие результаты под новым запросом.
        search_payload_var.set({"query": query, "count": 0, "results": []})
        return [TextContent(type="text", text=f"Ничего не найдено: '{query}'")]

    results = await _rerank_async(query, results, top_k=8)

    track_access([f"{r['project']}/{r['file']}" for r in results])

    header = f"# Поиск: '{query}'\n"
    if fallback_all:
        header += (f"\n*В проекте «{project}» ничего не найдено — показаны результаты "
                   f"по всем проектам (возможно, общая/кросс-проектная сущность).*\n")
    links: list[ResourceLink] = []
    found: list[dict] = []
    secrets = {}
    for r in results:
        secret = is_secret_article(r.get("preview", ""), r.get("file", ""))
        if secret:
            r["preview"] = f"# {r['title']}\n\n[зашифровано — используй read_article для просмотра]"
        secrets[f"{r['project']}/{r['file']}"] = secret
    # Текст выдачи собирает ОДИН бюджет: голове полное превью, хвосту короткое
    # (см. _render_search_results). Ссылки и структурная выдача строятся по ВСЕМ
    # результатам независимо от того, сколько текста досталось каждому: обрезка
    # превью не должна прятать найденное от панели MCP Apps.
    out = [_render_search_results(results, header, query)]
    for r in results:
        secret = secrets.get(f"{r['project']}/{r['file']}", False)
        scores = _scores(r)
        # Resource link на статью — клиент открывает/прикрепляет как memory://-ресурс.
        # Секреты не линкуем (как ресурс они недоступны).
        if not secret:
            links.append(ResourceLink(
                type="resource_link",
                uri=f"memory://{r['project']}/{r['file']}",
                name=f"{r['project']}/{r['file']}",
                title=r.get("title", ""),
                description=scores,
                mimeType="text/markdown",
            ))
        # А в структурированную выдачу секрет ВХОДИТ — с флагом. Панель покажет его
        # с замком, а открывать будет через read_article (тот расшифровывает), не
        # через memory://. uri у секрета остаётся идентификатором статьи и НЕ
        # разрешается как ресурс — на это и указывает secret.
        found.append({
            "uri": f"memory://{r['project']}/{r['file']}",
            "name": f"{r['project']}/{r['file']}",
            "title": r.get("title", "") or "",
            "score": scores,
            "project": r["project"],
            "file": r["file"],
            "secret": bool(secret),
        })

    search_payload_var.set({"query": query, "count": len(found), "results": found})
    return [TextContent(type="text", text="\n".join(out)), *links]


# ─── _parse_daily_entries (private helper for compile) ───────────────────────


def _parse_daily_entries(text: str) -> list[dict]:
    """Parse daily log into individual entries. Split only by --- separator."""
    entries = []
    # Split by --- separator only (not by ## headers which may be inside content)
    parts = re.split(r'\n---\n', text)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        lines = part.splitlines()
        # Find the top-level ## title (first ## in the entry)
        title = ""
        title_idx = -1
        for i, line in enumerate(lines):
            if line.startswith("## "):
                title = line[3:].strip()
                title_idx = i
                break
        if not title:
            continue
        # Extract metadata from lines after title
        project = "general"
        tags = []
        ts = ""
        body_start = title_idx + 1
        for i in range(title_idx + 1, min(title_idx + 8, len(lines))):
            line = lines[i]
            if line.startswith("**Время:**") or line.startswith("**Дата:**"):
                ts = line.split(":", 1)[1].strip().lstrip("*").rstrip("*").strip()
                body_start = i + 1
            elif line.startswith("**Проект:**"):
                project = line.split(":", 1)[1].strip().lstrip("*").rstrip("*").strip()
                body_start = i + 1
            elif line.startswith("**Теги:**"):
                tags_str = line.split(":", 1)[1].strip().lstrip("*").rstrip("*").strip()
                tags = [t.strip() for t in tags_str.split(",") if t.strip() and t.strip() != "\u2014"]
                body_start = i + 1
            elif line.strip() == "":
                body_start = i + 1
            elif not line.startswith("**"):
                break  # body started
        body = "\n".join(lines[body_start:]).strip()
        if body:
            entries.append({"topic": title, "project": project, "tags": tags, "timestamp": ts, "content": body})
    return entries


# ─── compile ─────────────────────────────────────────────────────────────────


async def compile(dry_run: bool = True, project: str = None, since: str = None) -> list[TextContent]:
    daily_dir = KNOWLEDGE_DIR / "daily"
    if not daily_dir.exists():
        return [TextContent(type="text", text="Дневных логов нет.")]
    logs = sorted(daily_dir.glob("*.md"))
    if not logs:
        return [TextContent(type="text", text="Дневных логов нет.")]

    # Filter by date
    if since:
        logs = [l for l in logs if l.stem >= since]

    out = []
    total_entries = 0
    updated = 0
    created = 0
    skipped = 0  # дубли: запись уже в статье (save_lesson пишет и в статью, и в лог)
    processed_logs = []

    for log in logs:
        text = log.read_text(encoding="utf-8")
        entries = _parse_daily_entries(text)
        if not entries:
            continue

        for entry in entries:
            # Filter by project
            if project and entry["project"] != project:
                continue
            total_entries += 1

            if entry["project"] not in PROJECTS:
                entry["project"] = "general"

            existing = find_existing_article(entry["topic"], entry["content"], entry["project"])

            if dry_run:
                if existing and is_duplicate_entry(existing.read_text(encoding="utf-8"),
                                                   entry["content"], entry["timestamp"] or ""):
                    out.append(f"  ⏭ Уже в статье: «{entry['topic']}» → {existing.name}")
                elif existing:
                    out.append(f"  \U0001f504 Мерж: \u00ab{entry['topic']}\u00bb \u2192 {existing.name}")
                else:
                    slug = make_slug(entry['topic'])
                    out.append(f"  \u2705 Новая: \u00ab{entry['topic']}\u00bb \u2192 {entry['project']}/{slug}.md")
            else:
                ts = entry["timestamp"] or datetime.now().strftime("%Y-%m-%d %H:%M")
                if existing:
                    if merge_into_article(existing, entry["content"], entry["tags"], ts) == "duplicate":
                        skipped += 1  # уже в статье (issue #2) — не дописываем и не переиндексируем
                        continue
                    article_text = existing.read_text(encoding="utf-8")
                    await _index_embed(article_text, existing.name, entry["project"])
                    updated += 1
                else:
                    slug = make_slug(entry['topic'])
                    article_path = project_dir(entry["project"]) / f"{slug}.md"
                    if article_path.exists():
                        article_path = project_dir(entry["project"]) / f"{slug}_{datetime.now().strftime('%Y%m%d')}.md"
                    article_text = f"# {entry['topic']}\n\n**Дата:** {ts}\n**Проект:** {entry['project']}\n**Теги:** {', '.join(entry['tags']) if entry['tags'] else '\u2014'}\n\n## Записи\n\n### {ts}\n{entry['content']}\n"
                    article_path.write_text(article_text, encoding="utf-8")
                    await _index_embed(article_text, article_path.name, entry["project"])
                    created += 1

        processed_logs.append(log)

    if dry_run:
        header = f"# Compile preview \u2014 {total_entries} записей из {len(processed_logs)} логов\n"
        if not out:
            return [TextContent(type="text", text="Нечего компилировать.")]
        return [TextContent(type="text", text=header + "\n".join(out))]
    else:
        # Archive processed daily logs
        archive_dir = daily_dir / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        for log in processed_logs:
            log.rename(archive_dir / log.name)

        await asyncio.to_thread(regenerate_index)
        await asyncio.to_thread(git_commit, f"compile: {total_entries} entries, {updated} updated, {created} created, {skipped} skipped")
        summary = f"\u2705 Скомпилировано: {total_entries} записей \u2014 {updated} обновлено, {created} создано, {len(processed_logs)} логов архивировано" + (f" (пропущено дублей: {skipped})" if skipped else "")
        return [TextContent(type="text", text=summary)]



# ─── lint ────────────────────────────────────────────────────────────────────






# ─── Session Handoff ─────────────────────────────────────────────────────────


async def save_session(project: str, summary: str, decisions: str = "", open_questions: str = "") -> list[TextContent]:
    """Дописать сессию в журнал проекта; открытые вопросы — в накопительный список.

    ⚠️ Раньше файл сессии ПЕРЕЗАПИСЫВАЛСЯ целиком, и открытые вопросы жили лишь
    до следующей сессии того же проекта. Замер 2026-08-26 по аудиту (7965 вызовов,
    502 сессии): вопросы фиксировались в 68% вызовов finish_task — 948 штук,
    медиана 235 символов — и 916 из них (96%) затирались. В статью они не
    попадают вообще. Теперь сессии накапливаются (последние MAX_SESSIONS), а
    вопросы ведутся отдельным файлом со статусом и закрываются явно.
    """
    await asyncio.to_thread(append_session, project, summary, decisions, open_questions)
    added = await asyncio.to_thread(add_question, project, open_questions) if open_questions else False
    await asyncio.to_thread(git_commit, f"session: {project}")
    msg = f"✅ Контекст сессии сохранён: {project}/_session.md"
    if added:
        n = len(open_questions_list(project))
        msg += f"\n❓ Открытый вопрос добавлен в {project}/_questions.md (всего открытых: {n})"
    return [TextContent(type="text", text=msg)]


async def session_note(note: str, project: str) -> list[TextContent]:
    """Заметка по ходу сессии: одна строка в текущий блок журнала.

    Дополнять контекст ПОСРЕДИ работы было нечем: `save_session` пересобирает
    сводку целиком и зовётся в 10% сессий (замер 2026-08-26 по аудиту, 502
    сессии). При этом работа после последней загрузки контекста — медиана 25
    минут, p90 101: всё найденное в эти минуты для параллельной сессии и для
    следующего старта не существовало.

    Дёшево по построению: без git-коммита (5.5 с на `git add -A`), без
    пересборки сводки, без индексации — файл журнала служебный и в поиск не
    попадает (search.SERVICE_FILES).
    """
    await asyncio.to_thread(append_note, project, note)
    return [TextContent(type="text", text=f"✅ Заметка записана в {project}/_session.md")]


async def open_questions(project: str = "all") -> list[TextContent]:
    """Незакрытые вопросы — то, на чём останавливались в прошлых сессиях."""
    import memory_compiler.config as _cfg
    projects = [project] if project != "all" else [p for p in _cfg.PROJECTS if p != "daily"]
    parts, total = [], 0
    for proj in projects:
        try:
            items = await asyncio.to_thread(open_questions_list, proj)
        except ValueError:
            continue
        if not items:
            continue
        total += len(items)
        parts.append(f"\n## {proj} ({len(items)})")
        for q in items[:10]:
            age = ""
            try:
                d = datetime.strptime(q["opened"][:10], "%Y-%m-%d")
                days = (datetime.now() - d).days
                age = f" · {days} дн назад" if days else " · сегодня"
            except ValueError:
                pass
            parts.append(f"- **{q['opened']}**{age}\n  {q['text'][:400]}")
    if not total:
        where = "во всех проектах" if project == "all" else f"в {project}"
        return [TextContent(type="text", text=f"Открытых вопросов {where} нет.")]
    tail = "\n\n*Решённый закрывать через `close_question(project, match)` — по куску текста.*"
    return [TextContent(type="text", text=f"# Открытые вопросы ({total})" + "\n".join(parts) + tail)]


async def close_question(project: str, match: str) -> list[TextContent]:
    """Закрыть вопрос(ы), чей текст содержит match."""
    n = await asyncio.to_thread(close_questions, project, match)
    if not n:
        return [TextContent(type="text", text=f"⚠️ В {project} не найдено открытых вопросов по «{match}».")]
    await asyncio.to_thread(git_commit, f"questions: close in {project}")
    left = len(open_questions_list(project))
    return [TextContent(type="text", text=f"✅ Закрыто вопросов: {n}. Осталось открытых в {project}: {left}")]


async def load_session(project: str) -> list[TextContent]:
    session_path = safe_project_dir(project) / "_session.md"
    parts = []
    if session_path.exists():
        parts.append(session_path.read_text(encoding="utf-8"))
    else:
        parts.append(f"Нет сохранённой сессии для {project}.")

    # Уведомления о stale статьях
    proj_path = KNOWLEDGE_DIR / project
    stale_count = 0
    if proj_path.exists():
        for md in proj_path.glob("*.md"):
            if md.name.startswith("_"):
                continue
            text = md.read_text(encoding="utf-8")
            for line in text.splitlines()[:10]:
                if line.startswith("**Обновлено:**") or line.startswith("**Дата:**"):
                    date_str = line.split(":", 1)[1].strip().lstrip("*").rstrip("*").strip()[:10]
                    try:
                        dt = datetime.strptime(date_str, "%Y-%m-%d")
                        if (datetime.now() - dt).days > 90:
                            stale_count += 1
                    except ValueError:
                        pass
                    break
    if stale_count > 0:
        parts.append(f"\n\u26a0\ufe0f {stale_count} статей в {project} не обновлялись >90 дней. Запусти `lint` для деталей.")

    return [TextContent(type="text", text="\n".join(parts))]


# ─── get_summary ─────────────────────────────────────────────────────────────




# ─── ask ─────────────────────────────────────────────────────────────────────


ASK_TOP_K = 5  # ответу нужна горстка точных источников, а не широкая выдача как у search
ASK_HEAD_LINES = 200  # потолок строк шапки; у статей без '### '-секций там лежит всё тело


def ask_fragment(text: str, question: str, limit: int = 300) -> str:
    """Наиболее релевантная вопросу секция статьи, обрезанная до limit символов.

    Секции — по '### ' (запись статьи). Скор секции — сколько РАЗНЫХ значимых слов
    вопроса в ней встретилось. Пустая строка, если не совпало ничего: вызывающий
    подставит preview.

    ⚠️ Шапка статьи в кандидаты НЕ идёт: это метаданные, а не ответ. Раньше шла —
    нулевым куском split'а оказывался YAML-frontmatter + заголовок + **Дата/Проект/
    Теги** + '## Записи'. С появлением contexts:-frontmatter (v1.28.0, ИИ-пересказ
    КАЖДОЙ секции) эта шапка стала матчить почти любой релевантный вопрос и
    выигрывать скор — на живой базе 5 фрагментов из 5 не содержали ни строчки
    контента: сырой YAML либо меташапка. Фильтруем ПОСТРОЧНО (article_body_lines),
    а не выбрасываем нулевой кусок целиком: у ~15% статей (ingest/импорт) нет ни
    одной '### '-секции, и всё тело лежит именно там."""
    q_words = {w.lower() for w in question.split() if len(w) > 2}
    if not q_words:
        return ""
    from memory_compiler.search import _strip_frontmatter
    from memory_compiler.storage import article_body_lines
    sections = _strip_frontmatter(text).split("\n### ")
    sections[0] = "\n".join(article_body_lines(sections[0], limit=ASK_HEAD_LINES))
    best_score, best_sec = 0, ""
    for sec in sections:
        low = sec.lower()
        score = sum(1 for w in q_words if w in low)
        if score > best_score:
            best_score, best_sec = score, sec.strip()
    return best_sec[:limit].strip() if best_score else ""


async def ask_sources(question: str, project: str = "all") -> tuple:
    """Источники для ответа на вопрос: (список источников, был ли фолбэк на все проекты).

    Общее ядро MCP-тула ask (рендерит в текст) и /api/ask (отдаёт JSON) — чтобы
    ассистент и веб-UI отвечали на один вопрос одинаково, а не разными конвейерами.
    Конвейер тот же, что у search: широкий пул кандидатов -> фолбэк на project=all
    -> cross-encoder rerank. Раньше ask брал whoosh top-5 без реранка и без фолбэка,
    т.е. отвечал ХУЖЕ, чем search (реранкер даёт +25-40% precision).
    """
    results = await _whoosh_async(question, project=project, limit=SEARCH_CANDIDATE_POOL)
    fallback_all = False
    if not results and project != "all":
        results = await _whoosh_async(question, project="all", limit=SEARCH_CANDIDATE_POOL)
        fallback_all = bool(results)
    if not results:
        return [], False

    results = await _rerank_async(question, results, top_k=ASK_TOP_K)
    track_access([r["project"] + "/" + r["file"] for r in results])

    sources = []
    for r in results:
        # Секретные статьи не цитируем. ⚠️ Проверять по ФАЙЛУ, а не по preview:
        # _index_safe_text САМ вырезает строку '**Секрет:** да', собирая плейсхолдер
        # из титула и тегов, — в preview признак не выживает, и от проверки оставался
        # только префикс имени 'secret_'. Пока у всех секретов базы префикс есть, это
        # не стреляло, но это ровно тот баг, который чинили в v1.25.0, подключённый
        # к неверному источнику истины: статья с флагом, но без префикса, утекла бы.
        fpath = KNOWLEDGE_DIR / r["project"] / r["file"]
        raw = fpath.read_text(encoding="utf-8") if fpath.exists() else ""
        secret = bool(is_secret_article(raw or r.get("preview", ""), r.get("file", "")))
        fragment = ""
        if not secret:
            if raw:
                fragment = ask_fragment(raw, question)
            if not fragment:
                fragment = "\n".join(r["preview"].splitlines()[:5])
        sources.append({
            "project": r["project"],
            "file": r["file"],
            "title": r.get("title", r["file"]),
            "score": r["score"],
            "rerank": round(r["rerank_score"], 3) if "rerank_score" in r else None,
            "fragment": fragment,
            "secret": secret,
        })
    return sources, fallback_all


async def ask(question: str, project: str = "all") -> list[TextContent]:
    sources, fallback_all = await ask_sources(question, project=project)
    if not sources:
        return [TextContent(type="text", text=f"Не найдено информации по: '{question}'")]

    header = f"# Ответ на: {question}\n"
    if fallback_all:
        header += (f"\n*В проекте «{project}» ничего не найдено — показаны результаты "
                   f"по всем проектам (возможно, общая/кросс-проектная сущность).*\n")
    out = [header]
    for s in sources:
        label = "[" + s["project"] + "/" + s["file"] + "]"
        scores = "score: " + str(s["score"])
        if s["rerank"] is not None:
            scores += ", rerank: %.2f" % s["rerank"]
        body = "[зашифровано — используй read_article для просмотра]" if s["secret"] else s["fragment"]
        out.append("---\n**" + label + "** (" + scores + ")\n> " + body + "\n")

    return [TextContent(type="text", text="\n".join(out))]


# ─── get_active_context ──────────────────────────────────────────────────────


async def get_active_context(project: str) -> list[TextContent]:
    ctx_path = safe_project_dir(project) / "_active_context.md"
    if not ctx_path.exists():
        return [TextContent(type="text", text=f"Нет активного контекста для {project}.")]
    text = ctx_path.read_text(encoding="utf-8")
    return [TextContent(type="text", text=text)]


# ─── CRUD статей ─────────────────────────────────────────────────────────────


async def delete_article(project: str, filename: str) -> list[TextContent]:
    try:
        fpath = safe_article_path(project, filename)
    except ValueError as e:
        return [TextContent(type="text", text=f"❌ Небезопасный путь: {e}")]
    if not fpath.exists():
        return [TextContent(type="text", text=f"Статья не найдена: {project}/{filename}")]
    fpath.unlink()
    # Remove from indexes: под локом + журнал _deleted_parents (фоновый rebuild
    # не воскресит) + персист pkl (иначе после рестарта статья вернётся из кэша).
    key = f"{project}/{filename}"
    await asyncio.to_thread(_search.remove_embedding, key)
    article_meta.pop(key, None)          # loop: dict-op, чтобы не гонка с track_access
    save_article_meta()                   # loop: json.dumps итерирует article_meta
    await asyncio.to_thread(_search.delete_document, key)  # точечно, вне event loop
    await asyncio.to_thread(regenerate_index)
    await asyncio.to_thread(git_commit, f"delete: {filename} [{project}]")
    return [TextContent(type="text", text=f"\U0001f5d1\ufe0f Удалено: {project}/{filename}")]


async def edit_article(project: str, filename: str, content: str, append: bool = False) -> list[TextContent]:
    try:
        fpath = safe_article_path(project, filename)
    except ValueError as e:
        return [TextContent(type="text", text=f"❌ Небезопасный путь: {e}")]
    if not fpath.exists():
        return [TextContent(type="text", text=f"Статья не найдена: {project}/{filename}")]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    old_text = fpath.read_text(encoding="utf-8")
    # Секретность определяется ДО записи: тело такой статьи не должно
    # существовать в открытом виде (инвариант save_secret/read_article).
    is_secret = is_secret_article(old_text, filename)
    if is_secret:
        from memory_compiler.config import MC_ENCRYPT_KEY
        if not MC_ENCRYPT_KEY:
            return [TextContent(type="text", text=(
                "❌ Секретная статья, но MC_ENCRYPT_KEY не задан — правка отклонена, "
                "чтобы не раскрыть секрет в plaintext."))]

    if append:
        # Для секрета шифруем дописываемое тело отдельным ENC:-блоком
        # (read_article расшифровывает построчно), заголовок секции — нет.
        body_add = encrypt_content(content) if is_secret else content
        text = old_text.rstrip() + f"\n\n### {ts}\n{body_add}\n"
        fpath.write_text(text, encoding="utf-8")
    else:
        # Сохраняем ПОЛНУЮ шапку (титул + все **Ключ:** строки, включая
        # **Секрет:** да и **Обновлено:**), обрываемся на пустой строке после
        # метаблока или на первом '## ' — НЕ на **Теги:** (старый баг терял
        # всё, что шло после тегов).
        header_lines = []
        meta_started = False
        for line in old_text.splitlines():
            s = line.strip()
            if s.startswith("## "):
                break
            if s == "" and meta_started:
                break
            header_lines.append(line)
            if re.match(r"\*\*.+?:\*\*", s):
                meta_started = True
        header = "\n".join(header_lines).rstrip()
        if "**Обновлено:**" in header:
            header = re.sub(r"\*\*Обновлено:\*\*.*", f"**Обновлено:** {ts}", header)
        else:
            header = header + f"\n**Обновлено:** {ts}"
        if is_secret and "**Секрет:** да" not in header:
            header = header + "\n**Секрет:** да"
        body = encrypt_content(content) if is_secret else content
        fpath.write_text(f"{header}\n\n{body}\n", encoding="utf-8")

    # Индексация: у секрета в индекс/эмбеддинги идёт ТОЛЬКО плейсхолдер
    # (титул + теги), как в save_secret — тело не попадает в поиск.
    if is_secret:
        disk_lines = fpath.read_text(encoding="utf-8").splitlines()
        title = disk_lines[0].lstrip("# ").strip() if disk_lines else filename
        tags_line = next((l for l in disk_lines[:12] if l.lower().startswith("**теги:**")),
                         "**Теги:** secret")
        index_src = f"# {title}\n\n{tags_line}\n\n[зашифрованная статья]"
    else:
        index_src = fpath.read_text(encoding="utf-8")
    await _index_embed(index_src, filename, project)

    # Cascade-mark: refresh marker on lines that link to this file
    cascaded = mark_dependents(project, filename, ts)

    log_event(project, "edit_article", f"{filename}" + (f" (cascade: {cascaded})" if cascaded else ""))
    await asyncio.to_thread(git_commit, f"edit: {filename} [{project}]")

    msg = f"\u270f\ufe0f {'Дописано' if append else 'Обновлено'}: {project}/{filename}"
    if cascaded:
        msg += f"\n\U0001f504 Маркер обновления проставлен в {cascaded} зависимых статьях"
    return [TextContent(type="text", text=msg)]


def _is_log_heading(h: str) -> bool:
    """True для append-лог секций вида '### 2026-07-17' / '### 2026-07-17 14:30' —
    их не имеет смысла контекстуализировать (каждая новая запись — новый заголовок,
    статья иначе никогда не покинула бы список пробелов)."""
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}", h.strip()))


def _body_sections(body: str) -> tuple[str, list[tuple[str, str]]]:
    """(преамбула, [(заголовок, тело)]) из тела статьи БЕЗ frontmatter — той же
    line-based логикой, что _chunk_article. Преамбула — всё до первого '### '
    (заголовок статьи, дата, теги): нужна модели, чтобы ситуировать секции."""
    pre_lines: list[str] = []
    acc: list[tuple[str, list[str]]] = []
    for line in body.splitlines():
        if line.startswith("### "):
            acc.append((line[4:].strip(), []))
        elif acc:
            acc[-1][1].append(line)
        else:
            pre_lines.append(line)
    return "\n".join(pre_lines).strip(), [(hd, "\n".join(ls).strip()) for hd, ls in acc]


def _fair_section_budgets(lengths: list[int], total: int) -> list[int]:
    """Распределить бюджет символов по секциям (max-min fairness): короткие получают
    свою длину целиком, остаток делится поровну между длинными. Не зависит от порядка
    секций — хвостовая получает столько же, сколько первая (head-срез всего файла
    отдавал хвосту ноль)."""
    budgets = [0] * len(lengths)
    open_idx = set(range(len(lengths)))
    left = total
    while open_idx:
        fair = left // len(open_idx)
        fits = {i for i in open_idx if lengths[i] <= fair}
        if not fits:
            for i in open_idx:
                budgets[i] = fair
            break
        for i in fits:
            budgets[i] = lengths[i]
            left -= lengths[i]
        open_idx -= fits
    return budgets


def _cut_section_body(body: str, budget: int) -> tuple[str, bool]:
    """Обрезать тело секции до бюджета по границе строки (если она не слишком рано),
    пометив срез многоточием. Влезает целиком → как есть."""
    if len(body) <= budget:
        return body, False
    head = body[:budget]
    nl = head.rfind("\n")
    if nl > budget * 0.6:
        head = head[:nl]
    return head + "\n…", True


async def context_gaps(project: str = "all", limit: int = 5) -> list[TextContent]:
    """Выдать батч статей, требующих ИИ-контекст: многосекционные, не-секретные,
    у которых есть '### '-секции без записи в contexts. Timestamp-секции (append-лог)
    игнорируются — иначе статьи-логи никогда не покидали бы список пробелов.
    full_text собирается ПОСЕКЦИОННО: шапка + тела только pending-секций (дельта
    headings−have), бюджет _CTX_FULLTEXT_CAP делится water-fill'ом. Head-срез сырого
    файла резал всегда хвост, а frontmatter/наполненные/лог-секции съедали бюджет
    (худший случай: contexts: 5312 из 8000). Stateless."""
    import json
    from memory_compiler.config import KNOWLEDGE_DIR, PROJECTS
    from memory_compiler.search import section_headings, _article_contexts, _strip_frontmatter

    projects = [project] if project and project != "all" else list(PROJECTS)
    articles, remaining = [], 0
    for proj in projects:
        pdir = KNOWLEDGE_DIR / proj
        if not pdir.exists():
            continue
        for md in sorted(pdir.glob("*.md")):
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue
            if is_secret_article(text, md.name):
                continue
            headings = [hd for hd in section_headings(text) if not _is_log_heading(hd)]
            if len(headings) < 2:
                continue
            have = set(_article_contexts(text).keys())
            if all(hd in have for hd in headings):
                continue
            remaining += 1
            if len(articles) < limit:
                body = _strip_frontmatter(text)
                bl = body.splitlines()
                title = bl[0].lstrip("# ").strip() if bl else md.stem
                pending = [hd for hd in headings if hd not in have]
                pend_set = set(pending)
                preamble, secs = _body_sections(body)
                parts = [("", preamble)] + [(hd, sb) for hd, sb in secs if hd in pend_set]
                budgets = _fair_section_budgets([len(sb) for _, sb in parts], _CTX_FULLTEXT_CAP)
                pieces, truncated = [], False
                for (hd, sb), bud in zip(parts, budgets):
                    piece, cut = _cut_section_body(sb, bud)
                    truncated = truncated or cut
                    if piece.strip():
                        pieces.append(f"### {hd}\n{piece}" if hd else piece)
                articles.append({
                    "project": proj, "filename": md.name, "title": title,
                    "sections": headings, "pending": pending,
                    "full_text": "\n\n".join(pieces), "truncated": truncated,
                })
    payload = {"remaining": remaining, "instructions": _CTX_INSTRUCTIONS, "articles": articles}
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


def _norm_ws(s: str) -> str:
    """Нормализовать пробелы для устойчивого матча заголовков: любые прогоны whitespace
    (пробелы, табы, NBSP) → один пробел, обрезка краёв. str.split() трактует NBSP как
    пробел, поэтому кривые импортированные заголовки матчатся с чистыми."""
    return " ".join(s.split())


async def save_contexts(project: str, filename: str, contexts: list) -> list[TextContent]:
    """Сохранить ИИ-контексты секций во frontmatter статьи (генератор контекста,
    Release 2). Принимает список {heading, context}; принимаются только заголовки,
    реально существующие в статье (### -секции) — иначе попадание чужого/устаревшего
    контекста. Секретные статьи исключены: контекст сохраняется в открытом виде,
    а тело секрета — нет."""
    from memory_compiler.storage import merge_contexts
    from memory_compiler.search import section_headings, _article_contexts

    try:
        fpath = safe_article_path(project, filename)
    except ValueError as e:
        return [TextContent(type="text", text=f"❌ Небезопасный путь: {e}")]
    if not fpath.exists():
        return [TextContent(type="text", text=f"Статья не найдена: {project}/{filename}")]

    text = fpath.read_text(encoding="utf-8")
    if is_secret_article(text, filename):
        return [TextContent(type="text", text=(
            "❌ Секретная статья — ИИ-контексты не сохраняются (тело секрета не должно "
            "участвовать в генерации контекста)."))]

    valid = section_headings(text)
    # Матч заголовков устойчив к whitespace: в импортированных статьях (Obsidian и т.п.)
    # заголовки бывают с табами/повторными пробелами/NBSP, которые не воспроизвести при
    # передаче через JSON. Нормализуем пробелы для СРАВНЕНИЯ, но храним КАНОНИЧЕСКИЙ
    # заголовок (как его извлекает _chunk_article) — иначе _section_context не найдёт контекст.
    norm_map: dict = {}
    for h in valid:
        norm_map.setdefault(_norm_ws(h), h)
    accepted: dict = {}
    skipped: list = []
    for item in contexts or []:
        heading = item.get("heading") if isinstance(item, dict) else None
        context = item.get("context") if isinstance(item, dict) else None
        canon = norm_map.get(_norm_ws(heading)) if isinstance(heading, str) else None
        if canon is not None and isinstance(context, str) and context.strip():
            accepted[canon] = " ".join(context.split())[:300]
        else:
            skipped.append(heading if isinstance(heading, str) else str(item))

    if not accepted:
        return [TextContent(type="text", text=(
            f"Ничего не сохранено — ни один заголовок не совпал с секциями статьи.\n"
            f"skipped: {skipped}"))]

    new_text = merge_contexts(text, accepted)
    fpath.write_text(new_text, encoding="utf-8")
    await _index_embed(new_text, filename, project)

    still_missing = [hd for hd in valid if hd not in _article_contexts(new_text)]

    log_event(project, "save_contexts", f"{filename} (+{len(accepted)}, skip {len(skipped)})")
    await asyncio.to_thread(git_commit, f"contexts: {filename} [{project}]")

    msg = f"✅ Контексты сохранены: {project}/{filename} (+{len(accepted)}: {list(accepted)})"
    if skipped:
        msg += f"\nskipped: {skipped}"
    if still_missing:
        msg += f"\nstill_missing: {still_missing}"
    return [TextContent(type="text", text=msg)]


async def read_article(project: str, filename: str) -> list[TextContent]:
    try:
        fpath = safe_article_path(project, filename)
    except ValueError as e:
        return [TextContent(type="text", text=f"❌ Небезопасный путь: {e}")]
    if not fpath.exists():
        return [TextContent(type="text", text=f"Статья не найдена: {project}/{filename}")]
    text = fpath.read_text(encoding="utf-8")
    # Decrypt encrypted sections
    lines = text.splitlines()
    decrypted_lines = []
    for line in lines:
        if is_encrypted(line):
            decrypted_lines.append(decrypt_content(line))
        else:
            decrypted_lines.append(line)
    text = "\n".join(decrypted_lines)
    key = f"{project}/{filename}"
    track_access([key])
    return [TextContent(type="text", text=text)]


async def search_by_tag(tag: str, project: str = "all") -> list[TextContent]:
    from memory_compiler.search import _strip_frontmatter

    tag_lower = tag.lower().strip()
    results = []
    check_projects = PROJECTS if project == "all" else [project]
    for proj in check_projects:
        proj_path = KNOWLEDGE_DIR / proj
        if not proj_path.exists():
            continue
        for md in proj_path.glob("*.md"):
            if md.name.startswith("_"):
                continue
            text = md.read_text(encoding="utf-8")
            lines = _strip_frontmatter(text).splitlines()
            title = lines[0].lstrip("# ").strip() if lines else md.stem
            for line in lines[:10]:
                if line.lower().startswith("**теги:**"):
                    tags_str = line.split(":", 1)[1].strip()
                    article_tags = [t.strip().lower().strip("*").strip() for t in tags_str.split(",")]
                    if tag_lower in article_tags:
                        preview = make_preview(text)
                        results.append({"title": title, "project": proj, "file": md.name, "preview": preview})
                    break
    if not results:
        return [TextContent(type="text", text=f"Статей с тегом '{tag}' не найдено.")]
    track_access([f"{r['project']}/{r['file']}" for r in results])
    out = [f"# Тег: {tag} ({len(results)} статей)\n"]
    for r in results:
        out.append(f"---\n### [{r['project']}] {r['title']}\n{r['file']}\n")
    return [TextContent(type="text", text="\n".join(out)), *_resource_links(results)]


# Блоки, которые пишет САМ сервер: add_see_also_links (семантические соседи) и
# git-ссылки. Их содержимое — не «кто сослался», а «что похоже», и на этот вопрос уже
# отвечает related. Замер базы 2026-07-21: ручных связей 264, авто-ссылок 2608 —
# без отсечения бэклинки были бы на 90% шумом.
_AUTO_LINK_BLOCKS = ("## См. также", "## Git-ссылки")




def _manual_link_body(text: str) -> str:
    """Тело для РУЧНЫХ связей: без кода и без машинных блоков ссылок."""
    cut = len(text)
    for marker in _AUTO_LINK_BLOCKS:
        i = text.find(marker)
        if i != -1:
            cut = min(cut, i)
    return _strip_code(text[:cut])






def _line_links_to(line: str, source_project: str, target_project: str,
                   target_file: str, target_stem: str) -> bool:
    """Ссылается ли строка на целевую статью.

    Вики-цель разрешается по ИМЕНИ ФАЙЛА: на живой базе из 173 целей по имени файла
    разрешились 124, по заголовку — НОЛЬ. Эвристики по заголовкам нет намеренно.
    """
    if any(t.strip() == target_stem for t in _WIKI_LINK_RE.findall(line)):
        return True
    for raw in _MD_LINK_RE.findall(line):
        href = raw.split("#", 1)[0].strip()
        if not href.endswith(target_file):
            continue
        # Имя файла может совпасть в разных проектах — требуем либо явный проект в
        # пути, либо ссылку внутри своего же проекта.
        if f"{target_project}/{target_file}" in href or source_project == target_project:
            return True
    return False


def _collect_backlinks(target_project: str, target_file: str) -> list[dict]:
    """Обойти базу и собрать ручные ссылки на статью. Синхронно (~0.45 с на 1777
    файлов) — вызывать через asyncio.to_thread, как всё тяжёлое.

    Отдельный индекс не заводится СОЗНАТЕЛЬНО: полсекунды дешевле, чем ещё одно
    состояние, которое придётся держать в синхроне с базой.
    """
    # Без pathlib: в этом модуле Path импортируется локально в одном месте, а имя
    # статьи всегда оканчивается на .md — отрезать суффикс достаточно и честнее.
    target_stem = target_file[:-3] if target_file.endswith(".md") else target_file
    found = []
    for proj in _discover_projects():
        pdir = KNOWLEDGE_DIR / proj
        if not pdir.exists():
            continue
        for md in pdir.glob("*.md"):
            # Служебные файлы (_log, _active_context, ...) ведёт движок: ссылка оттуда
            # означает «статья существует», а не «кто-то на неё сослался».
            if md.name.startswith("_"):
                continue
            if proj == target_project and md.name == target_file:
                continue  # самоссылка — не связь
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue
            body = _manual_link_body(text)
            if target_stem not in body and target_file not in body:
                continue  # дешёвый отсев до построчного разбора
            title, _ = article_title_tags(text, fallback=md.stem)
            for line in body.splitlines():
                if _line_links_to(line, proj, target_project, target_file, target_stem):
                    found.append({"project": proj, "file": md.name, "title": title,
                                  "context": line.strip()})
                    break   # одной строки контекста достаточно
    return found


async def backlinks(project: str, filename: str) -> list[TextContent]:
    """Кто ссылается на статью — обратное направление РУЧНЫХ связей."""
    try:
        fpath = safe_article_path(project, filename)   # traversal, как в read/delete
    except ValueError as e:
        return [TextContent(type="text", text=f"❌ Небезопасный путь: {e}")]
    if not fpath.exists():
        return [TextContent(type="text", text=f"Статья не найдена: {project}/{filename}")]

    found = await asyncio.to_thread(_collect_backlinks, project, filename)
    if not found:
        return [TextContent(type="text", text=(
            f"На статью {project}/{filename} нет ручных ссылок.\n"
            "Считаются только связи, проставленные вручную: авто-блоки «См. также» "
            "не учитываются — они про семантическую близость, её показывает related."))]

    out = [f"# Ссылаются на {project}/{filename} ({len(found)})\n"]
    for r in found:
        out.append(f"---\n### [{r['project']}] {r['title']}\n{r['file']}\n> {r['context']}\n")
    return [TextContent(type="text", text="\n".join(out))]




# ─── Комбинированные tools (start/finish task) ──────────────────────────────


# Бюджет стартового контекста. Прежние 400/600 символов резали последнюю
# сессию на полуслове; блок журнала берётся целиком, а лимит служит потолком.
SESSION_CHARS = 1800
SESSION_MAX_QUESTIONS = 5
SESSION_Q_CHARS = 300

# ── Общий бюджет стартового контекста (v1.65.0) ─────────────────────────────
# До этого у каждого блока был свой срез в символах, и лимиты не знали друг о
# друге. Замер 2026-08-26 на боевой базе: 50% показанных открытых вопросов
# резались по 300 символов, ещё 12 не показывались вовсе — а у 28 проектов из 46
# весь стартовый контекст не дотягивал и до 1500 символов, то есть место было.
# Размер ответа при этом гулял от 457 до 6808: общего потолка не существовало,
# он складывался стихийно из суммы независимых срезов.
START_BUDGET = 6000        # потолок на обрезаемые блоки, символов
START_BLOCK_FLOOR = 120    # меньше — огрызок; блок не показываем, долю возвращаем


def _weighted_budgets(want: list[int], weight: list[float], total: int,
                      floor: int = START_BLOCK_FLOOR) -> list[int]:
    """Взвешенный water-fill: блок берёт min(запрос, своя доля), неиспользованное
    делится между теми, кому не хватило, пропорционально весам (приоритетам).

    От `_fair_section_budgets` отличается двумя вещами, обеими нужными здесь:
    вес (открытые вопросы важнее runbook'ов) и отсечка огрызков — блок, которому
    досталось меньше `floor`, не показывается вовсе, а его доля возвращается в
    пул. Результат не зависит от порядка блоков.
    """
    n = len(want)
    budgets = [0] * n
    if total <= 0 or not n:
        return budgets
    open_idx = {i for i in range(n) if want[i] > 0 and weight[i] > 0}
    left = total
    while open_idx:
        wsum = sum(weight[i] for i in open_idx)
        if wsum <= 0:
            break
        # доля блока в оставшемся пуле пропорциональна его весу
        fits = {i for i in open_idx if want[i] <= left * weight[i] / wsum}
        if not fits:
            for i in open_idx:
                budgets[i] = int(left * weight[i] / wsum)
            left -= sum(budgets[i] for i in open_idx)
            break
        for i in fits:
            budgets[i] = want[i]
            left -= want[i]
        open_idx -= fits
    # огрызки отбрасываем: обрывок в 30 символов не контекст, а шум
    for i in range(n):
        if 0 < budgets[i] < min(floor, want[i]):
            left += budgets[i]
            budgets[i] = 0
    # освободившееся (и остаток от целочисленного деления) доливаем тем, кто уже
    # показывается и не насытился. Отброшенным не доливаем — иначе огрызок
    # вернулся бы из мёртвых. Порядок задан весом, при равенстве индексом:
    # раздача обязана быть воспроизводимой.
    while left > 0:
        hungry = [i for i in range(n) if budgets[i] and budgets[i] < want[i]]
        if not hungry:
            break
        share = max(1, left // len(hungry))
        for i in sorted(hungry, key=lambda i: (-weight[i], i)):
            if left <= 0:
                break
            take = min(share, want[i] - budgets[i], left)
            budgets[i] += take
            left -= take
    return budgets


# ── Бюджет выдачи search (v1.67.0) ──────────────────────────────────────────
# `search` отдавал 8 результатов с превью в 10 строк КАЖДЫЙ — одинаково первому
# и восьмому. Замер 26.08.2026: это 64% всех символов, которые инструменты
# возвращают за неделю (2626 тыс. из 4125 тыс.), медиана выдачи 13132 символа.
# Хвост столько не стоит, и это показали два независимых замера:
#   • baseline retrieval_eval: recall@3 0.667, recall@5 0.78, recall@10 0.84 —
#     позиции 6-8 добавляют около 6% попаданий на ~37% объёма;
#   • 345 пар «запрос → открытая статья»: слова запроса стоят в ЗАГОЛОВКЕ у 76%,
#     в первых трёх строках у 87%, в первых четырёх у 91%; строки 5-10 дают 9%.
# Поэтому голове — полное превью, хвосту — короткое, на всё — общий потолок.
# ⚠️ ПОРЯДОК И СОСТАВ НЕ ТРОГАЕМ: правка про рендер, ранжирование то же.
SEARCH_BUDGET = 7000       # потолок на всю выдачу, символов
SEARCH_HEAD = 3            # позиций с полным превью (по recall@3)
SEARCH_HEAD_WEIGHT = 3.0   # во столько раз голова важнее хвоста при дележе


def _query_words(text: str) -> set[str]:
    """Значимые слова запроса: короткие и служебные выкидываем."""
    return {w for w in re.sub(r"[^а-яёa-z0-9]+", " ", (text or "").lower()).split()
            if len(w) > 3 and w not in _QUERY_STOP}


_QUERY_STOP = {"как", "что", "где", "для", "при", "это", "или", "был", "все",
               "еще", "ещё", "уже", "про", "него", "нужно", "надо"}


def _fit_preview(preview: str, budget: int, qwords: set[str]) -> str:
    """Уместить превью в бюджет, оставляя строки СО СЛОВАМИ ЗАПРОСА.

    ⚠️ ОТБОР ПО ЗАПРОСУ, А НЕ ПЕРВЫЕ N СТРОК — так решил замер. Сжатие хвоста
    первыми строками теряло сигнал: слова запроса оставались в блоке целевой
    статьи у 74% пар против 81% при полном превью (−7 п.п.). Отбор по запросу
    в ТОМ ЖЕ бюджете даёт 79% на хвосте и 82% в голове, то есть возвращает
    почти всё даром. Замер: 418 golden-пар «запрос → открытая статья».

    ⚠️ ПОРЯДОК СТРОК СОХРАНЯЕТСЯ: превью читают как связный текст, а
    перетасованные цитаты читаются как обрывки.
    """
    lines = preview.splitlines()
    if not lines:
        return ""
    head, body = lines[0], lines[1:]
    left = budget - len(head)
    if left <= 0 or not body:
        return head
    hit = [i for i, l in enumerate(body) if qwords & _query_words(l)] if qwords else []
    rest = [i for i in range(len(body)) if i not in set(hit)]
    chosen: set[int] = set()
    for i in hit + rest:                      # сначала совпавшие, потом добор с начала
        need = len(body[i]) + 1
        if need > left:
            continue
        chosen.add(i)
        left -= need
    if not chosen:
        return head
    out, skipped = [head], False
    for i in range(len(body)):
        if i in chosen:
            if skipped:
                out.append("…")
                skipped = False
            out.append(body[i])
        elif out:
            skipped = True
    return "\n".join(out)


def _render_search_results(results: list[dict], header: str = "", query: str = "") -> str:
    """Собрать выдачу поиска в пределах SEARCH_BUDGET.

    Бюджет делится тем же water-fill'ом, что и стартовый контекст: короткий
    результат берёт своё целиком, неиспользованное достаётся длинным, вес задаёт
    позиция. Заголовок остаётся у КАЖДОГО результата — в нём 76% сигнала, и
    безымянная строка в выдаче бесполезна.
    """
    qwords = _query_words(query)
    if not results:
        return header
    want, weight = [], []
    for i, r in enumerate(results):
        preview = r.get("preview", "") or ""
        head_line = f"---\n### [{r['project']}] {r['title']} ({_scores(r)})\n"
        want.append(len(head_line) + len(preview) + 1)
        weight.append(SEARCH_HEAD_WEIGHT if i < SEARCH_HEAD else 1.0)
    budgets = _weighted_budgets(want, weight, max(SEARCH_BUDGET - len(header), 0), floor=0)
    parts = [header] if header else []
    for r, bud in zip(results, budgets):
        title_line = f"---\n### [{r['project']}] {r['title']} ({_scores(r)})\n"
        left = bud - len(title_line)
        # ⚠️ Второго среза по строкам здесь НЕТ: превью уже собрано
        # make_preview(n=10) в search.py. Резать одно и то же дважды значит
        # считать бюджет по объёму, которого в выдаче не будет, — тогда он
        # распределяется впустую, и голова получает столько же, сколько хвост.
        body = r.get("preview", "") or ""
        if left <= 0:
            parts.append(title_line)       # заголовок отдаём всегда
            continue
        if len(body) > left:
            body = _fit_preview(body, left, qwords)
        parts.append(title_line + body + "\n")
    return "".join(parts)


def _scores(r: dict) -> str:
    s = f"score: {r['score']}"
    if "rerank_score" in r:
        s += f", rerank: {r['rerank_score']:.2f}"
    return s


class _Block:
    """Кусок стартового контекста: заголовок, пункты и приоритет.

    Пункты — целые смысловые единицы (вопрос, находка, факт). Внутри бюджета
    они набираются ЦЕЛИКОМ, пока влезают: половина вопроса хуже, чем вопрос и
    честная пометка «ещё 3».
    """

    __slots__ = ("key", "header", "items", "weight", "sep")

    def __init__(self, key: str, header: str, items: list[str], weight: float,
                 sep: str = "\n"):
        self.key = key
        self.header = header
        self.items = [i for i in items if i and i.strip()]
        self.weight = weight
        self.sep = sep

    @property
    def want(self) -> int:
        if not self.items:
            return 0
        return len(self.header) + sum(len(i) + len(self.sep) for i in self.items)


def _render_block(block: "_Block", budget: int) -> str:
    """Собрать блок в пределах бюджета: целые пункты, пока влезают; последний
    подрезается по границе строки; не поместившиеся — считаются вслух."""
    if not block.items or budget <= 0:
        return ""
    left = budget - len(block.header)
    if left <= 0:
        return ""
    shown, cut_tail = [], False
    for item in block.items:
        need = len(item) + len(block.sep)
        if need <= left:
            shown.append(item)
            left -= need
            continue
        # последний влезающий подрезаем, только если от него остаётся смысл
        if not shown and left > START_BLOCK_FLOOR:
            piece, _ = _cut_section_body(item, left)
            shown.append(piece)
            left = 0
            cut_tail = True
        break
    if not shown:
        return ""
    hidden = len(block.items) - len(shown) - (1 if cut_tail else 0)
    parts = [block.header, *shown]
    if hidden > 0:
        parts.append(f"*…ещё {hidden} — спроси `open_questions` / `search`*"
                     if block.key == "questions" else f"*…ещё {hidden}*")
    return block.sep.join(parts)


async def start_task(topic: str, project: str = "all") -> list[TextContent]:
    """Начать задачу: hybrid retrieval (BM25+semantic) + cross-encoder rerank + filter by relevance.

    Continuation intent: if topic is a generic "continue" phrase (mostly stopwords),
    skip semantic search entirely — load active context + last session for the project.
    Industry pattern: continuation is session restoration, not RAG.

    Объём выдачи держит ОДИН бюджет (START_BUDGET), а не срез у каждого блока:
    блоки заявляют желаемую длину и приоритет, `_weighted_budgets` раздаёт
    water-fill'ом. Прежние независимые лимиты (сессия 1800, вопрос 300, факт 220,
    compact 600, решение 100) вели себя ровно наоборот нужному — резали там, где
    место было (28 проектов из 46 не выбирали и 1500 символов), и не давали
    потолка там, где выдача разрасталась (457..6808 символов на вызов).
    """
    from memory_compiler.search import is_low_confidence_query
    MIN_SCORE = 15  # min hybrid score
    MIN_RERANK = 0.0  # cross-encoder score threshold (BAAI/bge-reranker-base outputs ~[-10, 10])
    parts = []
    blocks: list[_Block] = []

    # Topic words for relevance checks
    topic_words = {w.lower() for w in re.split(r'[\s\-_,.:;]+', topic) if len(w) > 3}

    # Continuation intent — skip RAG, go straight to session restoration
    is_continuation = is_low_confidence_query(topic)

    parts.append(f"# Контекст для: {topic}\n")

    if is_continuation:
        parts.append("*Запрос распознан как «продолжить работу» — показываю недавнюю активность по проекту.*\n")
        relevant = []
    else:
        # 1. Hybrid retrieval — берём top-20, ререйнкер выбирает top-3
        candidates = await _whoosh_async(topic, project=project, limit=20)
        candidates = [r for r in candidates if r.get("score", 0) >= MIN_SCORE]
        reranked = await _rerank_async(topic, candidates, top_k=5)
        # Final filter by rerank_score (reranker may say all are weak)
        relevant = [r for r in reranked if r.get("rerank_score", 1.0) >= MIN_RERANK]
        if not relevant and reranked:
            relevant = reranked[:1]  # at least show top-1 even if low

        if relevant:
            track_access([f"{r['project']}/{r['file']}" for r in relevant])
            found_items = []
            for r in relevant[:3]:
                preview = "\n".join(r["preview"].splitlines()[:4])
                scores = f"hybrid: {r.get('score', 0)}"
                if "rerank_score" in r:
                    scores += f", rerank: {r['rerank_score']:.2f}"
                found_items.append(f"### [{r['project']}] {r['title']} ({scores})\n{preview}")
            blocks.append(_Block("found", f"## Найдено ({len(relevant)} релевантных, hybrid+rerank)",
                                 found_items, weight=2.5, sep="\n\n"))
        else:
            parts.append("*Похожих кейсов не найдено в базе.*\n")

    # 2. Determine target project
    target_project = project if project != "all" else (relevant[0]["project"] if relevant else "general")

    # 3. Active context — на continuation показываем всё, иначе фильтр по topic_words
    ctx_path = KNOWLEDGE_DIR / target_project / "_active_context.md"
    if ctx_path.exists():
        ctx_text = ctx_path.read_text(encoding="utf-8")
        if is_continuation:
            # Continuation intent → recent activity wholesale (top 5)
            ctx_lines = [l for l in ctx_text.splitlines() if l.startswith("- [")]
            blocks.append(_Block("activity", f"## Недавняя активность в {target_project}",
                                 ctx_lines[:5], weight=1.5))
        elif topic_words:
            relevant_lines = []
            for line in ctx_text.splitlines():
                if not line.startswith("- ["):
                    continue
                line_words = set(re.findall(r'[а-яА-ЯёЁa-zA-Z]{4,}', line.lower()))
                if topic_words & line_words:
                    relevant_lines.append(line)
            blocks.append(_Block("activity", f"## Связанные действия в {target_project}",
                                 relevant_lines[:3], weight=1.5))

    # 3-. Факты прошлых сессий по теме. Файл `_reflections.md` до v1.62.0 писался
    # на каждом finish_task и не читался НИКЕМ — 103 КБ в 39 проектах впустую.
    # Отдаём только пересекающиеся с темой и не больше четырёх: это справка,
    # а не второй поиск.
    try:
        facts = relevant_reflections(target_project, topic_words, limit=4)
    except Exception:
        facts = []
    blocks.append(_Block("facts", f"## Факты прошлых сессий ({target_project})",
                         [f"- {f}" for f in facts], weight=1.5))

    # 3a. Сроки на исходе. Инструмент stale_facts за 4.5 месяца не позвали НИ РАЗУ
    # (замер по аудиту): проверка, которую надо вспомнить и вызвать, механизмом
    # актуальности не работает. Показываем сам, по проекту, и только когда есть
    # что сказать — через кэш, иначе скан добавил бы к старту 400-650 мс.
    try:
        deadlines = await asyncio.to_thread(stale_summary, target_project, 30, 3)
    except Exception:
        deadlines = []                       # сроки не должны ронять старт задачи
    dl_items = []
    for d in deadlines:
        when = "истёк" if d["days_left"] < 0 else f"осталось {d['days_left']} дн"
        dl_items.append(f"- **{d['title'][:90]}** — {d['date']}, {when}")
    blocks.append(_Block("deadlines", f"## Сроки на исходе ({target_project})",
                         dl_items, weight=2.0))

    # 3b. Открытые вопросы проекта — то, на чём остановились и не закрыли.
    # Показываем ВСЕГДА, когда они есть: до v1.58.0 96% зафиксированных вопросов
    # затирались следующей сессией и до следующего старта не доезжали вовсе.
    # Длину вопроса держит бюджет, а не срез по 300 символов: замер 2026-08-26 —
    # так обрезалась ПОЛОВИНА показанных вопросов (18 из 36).
    try:
        pending_q = open_questions_list(target_project, limit=SESSION_MAX_QUESTIONS)
    except ValueError:
        pending_q = []
    blocks.append(_Block("questions", f"## Открытые вопросы ({target_project})",
                         [f"- **{q['opened']}** — {q['text']}" for q in pending_q],
                         weight=3.0))

    # 4. Session — на continuation показываем всегда, иначе фильтр по словам.
    # Берём ПОСЛЕДНИЙ БЛОК ЖУРНАЛА целиком, а не срез файла по символам: файл
    # накопительный, и срез отдавал бы свежую сессию вперемешку со старыми,
    # обрываясь на полуслове.
    session_text = latest_session(target_project)
    if session_text:
        # Незакрытая сессия — это «что происходит прямо сейчас», её показываем
        # ВСЕГДА и первым делом: заметку писали именно затем, чтобы её увидели,
        # в том числе параллельная сессия с другой темой.
        running = RUNNING_MARK in session_text.splitlines()[0]
        show_session = running or is_continuation
        if not show_session and topic_words:
            session_words = set(re.findall(r'[а-яА-ЯёЁa-zA-Z]{4,}', session_text.lower()))
            show_session = bool(topic_words & session_words)
        if show_session:
            header = (f"## Сессия в работе ({target_project}) — не закрыта" if running
                      else f"## Предыдущая сессия ({target_project})")
            blocks.append(_Block("session", header, [session_text], weight=3.0))

    # 4b. Compact history — резюме сжатий контекста (новое в v1.4.0)
    # Continuous memory через compact-границы. Показываем только при continuation
    # или явных topic_words (не засорять обычный поиск).
    compact_path = KNOWLEDGE_DIR / target_project / "_compact_history.md"
    if compact_path.exists() and (is_continuation or topic_words):
        compact_text = compact_path.read_text(encoding="utf-8")
        # Парсим первый ## блок (самый свежий)
        cblocks = re.split(r"^## ", compact_text, flags=re.MULTILINE)
        recent_block = cblocks[1].strip() if len(cblocks) > 1 else ""
        blocks.append(_Block("compact",
                             f"## Compact history ({target_project}) — последний сжатый контекст",
                             [f"## {recent_block}"] if recent_block else [], weight=0.8))

    # 5. Search in dependent projects (только релевантные)
    deps = read_project_deps(target_project)
    if deps:
        dep_results = []
        for dep in deps:
            dr = await _whoosh_async(topic, project=dep, limit=2)
            dep_results.extend([r for r in dr if r.get("score", 0) >= MIN_SCORE])
        if dep_results:
            dep_results.sort(key=lambda r: -r.get("score", 0))
            dep_items = []
            for r in dep_results[:2]:
                preview = "\n".join(r["preview"].splitlines()[:3])
                dep_items.append(f"### [{r['project']}] {r['title']} (score: {r['score']})\n{preview}")
            blocks.append(_Block("deps", f"## Из зависимых проектов ({', '.join(deps)})",
                                 dep_items, weight=1.0, sep="\n\n"))

    # 5. Relevant decisions (brief, only high-score)
    decision_results = await _whoosh_async(topic, project=target_project, limit=10)
    decisions_found = []
    for r in decision_results:
        if r.get("score", 0) < 30:
            continue
        fpath = KNOWLEDGE_DIR / r["project"] / r["file"]
        if not fpath.exists():
            continue
        if r["file"].startswith("decision_") or "**Тип:** decision" in fpath.read_text(encoding="utf-8")[:500]:
            # Extract first line of decision section
            text = fpath.read_text(encoding="utf-8")
            decision_line = ""
            for line in text.splitlines():
                if line.startswith("## Решение"):
                    idx = text.splitlines().index(line)
                    if idx + 1 < len(text.splitlines()):
                        decision_line = text.splitlines()[idx + 1].strip()
                    break
            decisions_found.append(f"- **{r['title']}** — {decision_line[:100]}")
    blocks.append(_Block("decisions", "## Решения по теме", decisions_found[:3], weight=1.2))

    # 6. Relevant runbooks (brief, only matching)
    proj_path = KNOWLEDGE_DIR / target_project
    runbooks_found = []
    if proj_path.exists():
        for md in proj_path.glob("*.md"):
            if md.name.startswith("_"):
                continue
            head = md.read_text(encoding="utf-8")[:300]
            if "**Тип:** runbook" not in head:
                continue
            title = head.splitlines()[0].lstrip("# ").strip() if head.splitlines() else md.stem
            # Check relevance: any topic word in title
            title_words = {w.lower() for w in topic.split() if len(w) > 3}
            if title_words & {w.lower() for w in title.split()}:
                total = head.count("- [ ]") + head.count("- [x]")
                runbooks_found.append(f"- **{title}** ({md.name}, {total} шагов)")
    blocks.append(_Block("runbooks", "## Runbooks", runbooks_found[:3], weight=0.5))

    # 7. Раздача общего бюджета: короткий блок берёт своё целиком, неиспользованное
    # достаётся тем, кому не хватило, приоритет решает, кого резать первым.
    live = [b for b in blocks if b.items]
    budgets = _weighted_budgets([b.want for b in live], [b.weight for b in live], START_BUDGET)
    for b, bud in zip(live, budgets):
        rendered = _render_block(b, bud)
        if rendered:
            parts.append("\n" + rendered + "\n")

    parts.append("\n---\n*Приступай к задаче. По завершении вызови `finish_task`.*")
    return [TextContent(type="text", text="\n".join(parts))]


def _project_from_cwd(cwd: str) -> Optional[str]:
    """Сопоставить cwd с существующим проектом по имени директории.

    Алгоритм: ищем по компонентам пути (от глубокого к мелкому) первое
    совпадение с проектом из list_projects. Например:
      cwd = /home/user/dev/myapp/backend → проверяем 'backend', потом 'myapp', потом 'dev'
    Возвращает первое найденное имя проекта (lowercase) или None.
    """
    import memory_compiler.config as _cfg
    if not cwd:
        return None
    # Нормализуем разделители (Windows / Unix)
    parts = re.split(r"[/\\]", cwd.strip())
    parts = [p for p in parts if p]  # strip empty
    projects_set = set(p.lower() for p in _cfg.PROJECTS)
    # Iterate from deepest dir towards root — last (most specific) match wins
    for component in reversed(parts):
        normalized = component.lower().strip()
        if normalized in projects_set:
            return normalized
    return None


async def route_project(text: str = "", cwd: str = "", top_k: int = 3) -> list[TextContent]:
    """Авто-определение лучших проектов под текст запроса.

    Параметры:
      text  — описание задачи / упоминание сущности (опционально)
      cwd   — текущий рабочий каталог клиента (опционально, СИЛЬНЫЙ сигнал)
      top_k — сколько кандидатов вернуть

    Алгоритм:
      0. Если cwd содержит имя существующего проекта → возвращаем его с score 100 (override)
      1. Substring match — имя проекта целиком в тексте (вес: 50)
      2. Token overlap — слова из имени проекта в тексте (вес: 30)
      3. Content match — поиск text в статьях проекта (вес: 20)

    Используется клиентом (скил/CLI) когда нет явного project. Без хардкода клиентов.
    """
    import memory_compiler.config as _cfg

    # 0. CWD override — сильнейший сигнал. Если рабочий каталог совпадает с проектом, берём его.
    if cwd:
        cwd_proj = _project_from_cwd(cwd)
        if cwd_proj:
            return [TextContent(type="text", text=(
                f"# Route project\n\n"
                f"*cwd:* `{cwd}` → проект `{cwd_proj}` (score: 100, источник: cwd-match)\n\n"
                f"→ Используй `project=\"{cwd_proj}\"`."
            ))]

    text_lower = (text or "").lower()
    if not text_lower.strip() and not cwd:
        return [TextContent(type="text", text="# Route project\n\n*Пустой запрос и нет cwd — нечего роутить.*")]

    text_tokens = set(re.findall(r"[\wа-яё-]{3,}", text_lower))

    # Получить актуальный список проектов
    projects = [p for p in _cfg.PROJECTS if p not in ("daily",)]
    scores: dict[str, float] = {}

    for proj in projects:
        proj_lower = proj.lower()
        s = 0.0

        # 1. Substring — имя проекта целиком
        if proj_lower in text_lower:
            s += 50

        # 2. Token overlap — части имени проекта (по - и _)
        proj_tokens = set(re.split(r"[-_]", proj_lower))
        proj_tokens.discard("")
        proj_tokens -= {"ut", "buh", "site", "ru", "khv"}  # generic suffixes
        overlap = proj_tokens & text_tokens
        if proj_tokens:
            s += 30 * (len(overlap) / len(proj_tokens))

        # 3. Content match — есть ли в проекте статьи на тему текста
        if text_lower.strip():
            try:
                results = await _whoosh_async(text, project=proj, limit=3)
                content_score = sum(r.get("score", 0) for r in results) / 100
                s += min(20, content_score * 2)
            except Exception:
                pass

        if s > 0:
            scores[proj] = round(s, 1)

    if not scores:
        proj_list = ", ".join(projects[:10]) + ("..." if len(projects) > 10 else "")
        return [TextContent(type="text", text=(
            f"# Route project\n\n"
            f"*Не удалось подобрать проект для: «{text[:100]}»*\n\n"
            f"Доступные проекты: {proj_list}\n\n"
            f"Если уверен — передай `project=` явно. Иначе используй `general`."
        ))]

    # Детерминизм: при равном score тай-брейк по алфавиту, а не по порядку PROJECTS
    # (= os.listdir — зависит от ФС). Иначе один и тот же запрос в разных сессиях
    # роутился в разные проекты → кросс-проектные дубли статей.
    sorted_scores = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:top_k]
    parts = [f"# Route project\n\n*Запрос:* «{text[:120]}»\n"]
    parts.append("\n## Топ кандидатов\n")
    for proj, sc in sorted_scores:
        confidence = "высокая" if sc >= 50 else ("средняя" if sc >= 25 else "низкая")
        parts.append(f"- **{proj}** — score {sc} ({confidence})")

    best, best_score = sorted_scores[0]
    # Почти равные сильные кандидаты: алфавитный тай-брейк детерминирован, но не
    # «правилен» — молчаливый выбор одного из двух и порождал дубли. Просим уточнить.
    ambiguous = (len(sorted_scores) > 1 and sorted_scores[1][1] >= 25
                 and best_score - sorted_scores[1][1] < 5)
    if ambiguous:
        second = sorted_scores[1][0]
        parts.append(f"\n→ Неоднозначно: «{best}» и «{second}» почти равны (разрыв "
                     f"{round(best_score - sorted_scores[1][1], 1)}) — уточни проект явно "
                     f"или используй `general`.")
    elif best_score >= 25:
        parts.append(f"\n→ Используй `project=\"{best}\"` для save/start_task.")
    else:
        parts.append("\n→ Все совпадения слабые — лучше уточнить у пользователя или использовать `general`.")

    return [TextContent(type="text", text="\n".join(parts))]



async def save_compact(project: str, summary: str) -> list[TextContent]:
    """Сохранить промежуточный summary при сжатии контекста (PostCompact event).

    Используется когда контекст разговора был сжат и Claude хочет сохранить
    краткое описание ТОГО ЧТО БЫЛО до сжатия (чтобы не потерялось).

    Файл: <project>/_compact_history.md — FIFO из 5 последних event'ов.
    Подтягивается в start_task — даёт continuous memory через compact-границы.
    """
    proj_dir = safe_project_dir(project)
    cpath = proj_dir / "_compact_history.md"
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    new_block = f"## {now}\n{summary.strip()}\n"

    existing_blocks: list[str] = []
    if cpath.exists():
        text = cpath.read_text(encoding="utf-8")
        # Парсим существующие ## блоки
        current_block = []
        for line in text.splitlines():
            if line.startswith("## ") and current_block:
                existing_blocks.append("\n".join(current_block))
                current_block = [line]
            elif line.startswith("## "):
                current_block = [line]
            elif current_block:
                current_block.append(line)
        if current_block:
            existing_blocks.append("\n".join(current_block))

    # FIFO: новый сверху, всего 5
    all_blocks = [new_block] + existing_blocks
    all_blocks = all_blocks[:5]

    header = f"# Compact history: {project}\n\nКраткие резюме до сжатия контекста (FIFO 5):\n"
    cpath.write_text(header + "\n" + "\n".join(all_blocks) + "\n", encoding="utf-8")

    return [TextContent(type="text", text=(
        f"💾 Compact summary сохранён: {project}/_compact_history.md\n"
        f"({len(all_blocks)} последних резюме хранится).\n"
        f"При следующем start_task этого проекта будет подтянут."
    ))]












async def finish_task(topic: str, content: str, project: str, tags: list = None,
                      session_summary: str = "", open_questions: str = "") -> list[TextContent]:
    """Завершить задачу: save_lesson + save_session. Один вызов вместо двух."""
    parts = []

    # 1. Сохранить урок
    lesson_result = await save_lesson(topic, content, project, tags)
    parts.append(lesson_result[0].text)

    # 2. Сохранить сессию
    if session_summary:
        session_result = await save_session(project, session_summary, "", open_questions or "")
        parts.append(session_result[0].text)

    # 3. Prospective reflection — извлечь atomic facts из content + session_summary
    reflections = extract_reflections(content + "\n" + (session_summary or ""))
    if reflections:
        append_reflections(project, reflections)
        parts.append(f"\U0001f9e0 Reflections: +{len(reflections)} atomic facts")

    parts.append("\n*Задача записана в базу знаний.*")
    return [TextContent(type="text", text="\n".join(parts))]


# ─── Управление проектами ────────────────────────────────────────────────────


async def init_schema(project: str) -> list[TextContent]:
    """Create a _schema.md template in the project directory (Karpathy LLM Wiki pattern).

    Idempotent: if _schema.md already exists, returns a hint without overwriting.
    The schema is a human-edited contract — entities, relations, stylistic conventions —
    that lint and save_lesson can later use to enforce consistency.
    """
    proj_dir = safe_project_dir(project)
    schema_path = proj_dir / "_schema.md"
    if schema_path.exists():
        return [TextContent(type="text", text=(
            f"ℹ️ _schema.md уже существует в {project}. "
            f"Открой и отредактируй вручную: {schema_path}"
        ))]

    template = f"""# Schema: {project}

Контракт проекта — какие сущности существуют, какие связи бывают, какой стиль статей.
Используется `lint` и `save_lesson` для проверки соответствия (TODO).

## Сущности

<!-- Перечисли типы статей в проекте и их обязательные поля. Пример:
- ticket — заявка клиента (id, status, client, assignee)
- runbook — пошаговая инструкция (steps, verification)
- decision — архитектурное решение (alternatives, reasoning)
-->

## Связи

<!-- Какие отношения между сущностями. Пример:
- ticket → client (поле client в frontmatter)
- ticket → runbook (через общий тег)
-->

## Stylistic

<!-- Стилистические соглашения проекта. Пример:
- Все runbook-статьи имеют чекбоксы `- [ ]` для шагов
- В tracking_*.md current.version всегда semver
- Заголовки секций на русском
-->

## Glossary

<!-- Специфические термины и аббревиатуры проекта. -->
"""
    schema_path.write_text(template, encoding="utf-8")
    log_event(project, "init_schema", "_schema.md template created")
    return [TextContent(type="text", text=(
        f"✅ Создан шаблон _schema.md в {project}. "
        f"Отредактируй файл: добавь сущности, связи, conventions проекта."
    ))]


async def add_project(name: str) -> list[TextContent]:
    import memory_compiler.config as _cfg
    name = re.sub(r'[^\w\-]', '', name.lower().strip())
    if not name:
        return [TextContent(type="text", text="Некорректное имя проекта.")]
    proj_path = KNOWLEDGE_DIR / name
    if proj_path.exists():
        return [TextContent(type="text", text=f"Проект '{name}' уже существует.")]
    proj_path.mkdir(parents=True, exist_ok=True)
    _cfg.PROJECTS[:] = _discover_projects()
    await asyncio.to_thread(git_commit, f"add project: {name}")
    return [TextContent(type="text", text=f"\u2705 Проект '{name}' создан. Всего проектов: {len(_cfg.PROJECTS)}")]


async def remove_project(name: str, confirm: bool = False) -> list[TextContent]:
    import memory_compiler.config as _cfg
    from memory_compiler.storage import normalize_project
    name = normalize_project(name)
    proj_path = KNOWLEDGE_DIR / name
    if not proj_path.exists():
        return [TextContent(type="text", text=f"Проект '{name}' не найден.")]
    # Посчитать статьи
    articles = list(proj_path.glob("*.md"))
    # Require explicit confirmation to delete project with articles
    if articles and not confirm:
        return [TextContent(type="text", text=f"⚠️ Проект '{name}' содержит {len(articles)} статей. Для удаления передайте confirm=True. Это действие необратимо.")]
    if articles:
        keys = [f"{name}/{md.name}" for md in articles]
        for key in keys:
            article_meta.pop(key, None)  # loop: dict-op (гонка с track_access при выносе)

        def _rm_embeds():  # индекс-путь в worker-потоке (persist=False в цикле, один персист)
            for key in keys:
                _search.remove_embedding(key, persist=False)
            _search.persist_embeddings()
        await asyncio.to_thread(_rm_embeds)
    # Удалить папку (блокирующий I/O — вне event loop)
    await asyncio.to_thread(shutil.rmtree, str(proj_path))
    save_article_meta()
    _cfg.PROJECTS[:] = _discover_projects()
    await asyncio.to_thread(_search.delete_project_documents, name)  # точечно, вне event loop
    await asyncio.to_thread(regenerate_index)
    await asyncio.to_thread(git_commit, f"remove project: {name} ({len(articles)} articles)")
    return [TextContent(type="text", text=f"\U0001f5d1\ufe0f Проект '{name}' удалён ({len(articles)} статей). Осталось проектов: {len(_cfg.PROJECTS)}")]


async def list_projects() -> list[TextContent]:
    import memory_compiler.config as _cfg
    _cfg.PROJECTS[:] = _discover_projects()
    lines = [f"# Проекты ({len(_cfg.PROJECTS)})\n"]
    for proj in _cfg.PROJECTS:
        proj_path = KNOWLEDGE_DIR / proj
        if proj_path.exists():
            articles = [f for f in proj_path.glob("*.md") if not f.name.startswith("_")]
            size = sum(f.stat().st_size for f in articles)
            lines.append(f"- **{proj}** \u2014 {len(articles)} статей, {round(size/1024, 1)} KB")
        else:
            lines.append(f"- **{proj}** \u2014 пуст")
    return [TextContent(type="text", text="\n".join(lines))]


# ─── Snippet search ────────────────────────────────────────────────────────


async def search_snippets(query: str, lang: str = None, project: str = "all") -> list[TextContent]:
    """Search code snippets in knowledge base."""
    results = await _whoosh_async(query, project=project, limit=10)
    if not results:
        return [TextContent(type="text", text=f"Сниппетов не найдено: '{query}'")]

    found = []
    for r in results:
        fpath = KNOWLEDGE_DIR / r["project"] / r["file"]
        if not fpath.exists():
            continue
        text = fpath.read_text(encoding="utf-8")
        snippets = extract_snippets(text)
        for s in snippets:
            if lang and s["lang"] != lang:
                continue
            # Check if query words appear in code
            q_words = set(w.lower() for w in query.split() if len(w) > 2)
            code_lower = s["code"].lower()
            matches = sum(1 for w in q_words if w in code_lower)
            if matches > 0:
                found.append({
                    "article": f"{r['project']}/{r['file']}",
                    "lang": s["lang"],
                    "context": s["context"],
                    "code": s["code"][:500],
                    "relevance": matches,
                })

    found.sort(key=lambda x: x["relevance"], reverse=True)
    if not found:
        return [TextContent(type="text", text=f"Сниппетов с '{query}' не найдено.")]

    out = [f"# Сниппеты: '{query}' ({len(found)} найдено)\n"]
    link_items = []
    for s in found[:10]:
        out.append(f"---\n**[{s['article']}]** ({s['lang']}) — {s['context']}\n```{s['lang']}\n{s['code']}\n```\n")
        if "/" in s["article"]:
            p, f = s["article"].split("/", 1)
            link_items.append({"project": p, "file": f, "title": s.get("context", "")})
    return [TextContent(type="text", text="\n".join(out)), *_resource_links(link_items)]


# ─── Runbook ───────────────────────────────────────────────────────────────


async def save_runbook(topic: str, steps: list, project: str, tags: list = None) -> list[TextContent]:
    """Create a runbook article with checklist steps."""
    tags = tags or []
    auto = auto_tags(" ".join(steps), topic)
    existing_lower = {t.lower() for t in tags}
    tags = tags + [t for t in auto if t not in existing_lower]
    if "runbook" not in [t.lower() for t in tags]:
        tags.append("runbook")

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    slug = make_slug(topic)

    steps_text = "\n".join(f"- [ ] {step}" for step in steps)
    article_text = f"""# {topic}

**Дата:** {ts}
**Проект:** {project}
**Теги:** {', '.join(tags)}
**Тип:** runbook

## Шаги

{steps_text}
"""
    article_path = safe_project_dir(project) / f"{slug}.md"
    if article_path.exists():
        article_path = safe_project_dir(project) / f"{slug}_{datetime.now().strftime('%Y%m%d')}.md"
    article_path.write_text(article_text, encoding="utf-8")

    await _index_embed(article_text, article_path.name, project)
    await asyncio.to_thread(regenerate_index)
    await asyncio.to_thread(git_commit, f"runbook: {topic} [{project}]")

    return [TextContent(type="text", text=f"\U0001f4cb Runbook создан: {project}/{article_path.name} ({len(steps)} шагов)")]


async def get_runbook(project: str, filename: str) -> list[TextContent]:
    """Read runbook and parse step statuses."""
    # safe_article_path: без него путь собирался конкатенацией, и '../../file' читал
    # файл ВНЕ базы. Инвариант в проекте давно есть — read_article/edit_article/
    # delete_article закрыты, — а get_runbook остался единственным хендлером мимо него.
    try:
        fpath = safe_article_path(project, filename)
    except ValueError:
        return [TextContent(type="text", text=f"Runbook не найден: {project}/{filename}")]
    if not fpath.exists():
        return [TextContent(type="text", text=f"Runbook не найден: {project}/{filename}")]
    text = fpath.read_text(encoding="utf-8")
    # Гейт секретов: хендлер отдавал сырой файл целиком без единой проверки — для
    # настоящих секретов наружу уходил ENC-шифртекст, для флаговых — plaintext.
    if is_secret_article(text, fpath.name):
        return [TextContent(type="text", text=(
            f"Runbook {project}/{filename} — секретная статья.\n"
            f"[зашифровано — используй read_article для просмотра]"))]
    track_access([f"{project}/{filename}"])

    total = text.count("- [ ]") + text.count("- [x]")
    done = text.count("- [x]")
    progress = f"{done}/{total}" if total > 0 else "нет шагов"

    return [TextContent(type="text", text=f"\U0001f4cb Прогресс: {progress}\n\n{text}")]


# ─── Error search ──────────────────────────────────────────────────────────


async def search_error(error_text: str, project: str = "all") -> list[TextContent]:
    """Search for similar errors in knowledge base."""
    # Extract key parts from error text
    error_patterns = extract_errors(error_text)

    # Build search query from error patterns + original text
    search_terms = []
    for ep in error_patterns:
        search_terms.append(ep["text"][:50])
    if not search_terms:
        # Fallback: use last line of error (usually the exception)
        lines = error_text.strip().splitlines()
        search_terms = [lines[-1][:100]] if lines else [error_text[:100]]

    query = " ".join(search_terms)[:200]
    results = await _whoosh_async(query, project=project, limit=10)

    # Re-rank by error pattern overlap
    ranked = []
    for r in results:
        fpath = KNOWLEDGE_DIR / r["project"] / r["file"]
        if not fpath.exists():
            continue
        text = fpath.read_text(encoding="utf-8")
        article_errors = extract_errors(text)

        # Score boost for matching error types
        boost = 0
        for ae in article_errors:
            for ep in error_patterns:
                if ae["type"] == ep["type"]:
                    boost += 10
                    # Extra boost for matching error text
                    if ep["text"][:30].lower() in ae["text"].lower():
                        boost += 20
        r["score"] = r.get("score", 0) + boost
        ranked.append(r)

    ranked.sort(key=lambda x: x["score"], reverse=True)
    if not ranked:
        return [TextContent(type="text", text=f"Похожих ошибок не найдено.")]

    track_access([f"{r['project']}/{r['file']}" for r in ranked[:5]])

    out = [f"# Похожие ошибки ({len(ranked)} найдено)\n"]
    for r in ranked[:5]:
        preview = "\n".join(r["preview"].splitlines()[:8])
        out.append(f"---\n### [{r['project']}] {r['title']} (score: {r['score']})\n{preview}\n")
    return [TextContent(type="text", text="\n".join(out)), *_resource_links(ranked[:5])]


# ─── Project dependencies ─────────────────────────────────────────────────


async def set_project_deps(project: str, depends_on: list) -> list[TextContent]:
    """Set project dependencies."""
    # Validate projects exist
    for dep in depends_on:
        if dep == project:
            return [TextContent(type="text", text=f"Проект не может зависеть от себя.")]

    write_project_deps(project, depends_on)
    await asyncio.to_thread(git_commit, f"deps: {project} -> {', '.join(depends_on)}")
    return [TextContent(type="text", text=f"\U0001f517 Зависимости {project}: {', '.join(depends_on) if depends_on else 'нет'}")]


async def get_project_deps(project: str) -> list[TextContent]:
    """Get project dependencies."""
    deps = read_project_deps(project)
    if not deps:
        return [TextContent(type="text", text=f"Проект {project} не имеет зависимостей.")]
    return [TextContent(type="text", text=f"\U0001f517 {project} зависит от: {', '.join(deps)}")]


# ─── Decisions ─────────────────────────────────────────────────────────────


async def save_decision(title: str, decision: str, reasoning: str, project: str,
                        alternatives: str = "", tags: list = None) -> list[TextContent]:
    """Save an architectural/technical decision.

    ⚠️ alternatives НЕОБЯЗАТЕЛЕН, и порядок параметров поэтому не совпадает с
    порядком полей в статье. MCP-клиент срезает `required` у строковых параметров
    (см. tests/test_tool_schemas.py) — обязательности модель не видит и молча поле
    опускает, а решение без альтернатив и так законный случай. Диспетчер зовёт
    через **arguments, так что перестановка вызовам не видна.
    """
    tags = tags or []
    auto = auto_tags(f"{decision} {reasoning}", title)
    existing_lower = {t.lower() for t in tags}
    tags = tags + [t for t in auto if t not in existing_lower]
    if "decision" not in [t.lower() for t in tags]:
        tags.append("decision")

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    slug = make_slug(title)
    # Пустая секция читалась бы как «забыли заполнить». Отсутствие выбора —
    # сам по себе факт о решении, и он должен быть различим в статье.
    alternatives_text = (alternatives or "").strip() or "не рассматривались"

    article_text = f"""# {title}

**Дата:** {ts}
**Проект:** {project}
**Теги:** {', '.join(tags)}
**Тип:** decision

## Решение
{decision}

## Альтернативы
{alternatives_text}

## Обоснование
{reasoning}
"""
    article_path = safe_project_dir(project) / f"decision_{slug}.md"
    if article_path.exists():
        article_path = safe_project_dir(project) / f"decision_{slug}_{datetime.now().strftime('%Y%m%d')}.md"
    article_path.write_text(article_text, encoding="utf-8")

    await _index_embed(article_text, article_path.name, project)
    update_active_context(project, f"Decision: {title}", decision)
    await asyncio.to_thread(regenerate_index)
    await asyncio.to_thread(git_commit, f"decision: {title} [{project}]")

    return [TextContent(type="text", text=f"\U0001f4cc Решение записано: {project}/{article_path.name}")]


async def search_decisions(query: str, project: str = "all") -> list[TextContent]:
    """Search only decision articles."""
    results = await _whoosh_async(query, project=project, limit=15)

    # Filter to decision articles only
    decisions = []
    for r in results:
        fpath = KNOWLEDGE_DIR / r["project"] / r["file"]
        if not fpath.exists():
            continue
        text = fpath.read_text(encoding="utf-8")
        if "**Тип:** decision" in text or r["file"].startswith("decision_"):
            decisions.append(r)

    if not decisions:
        return [TextContent(type="text", text=f"Решений по '{query}' не найдено.")]

    track_access([f"{r['project']}/{r['file']}" for r in decisions])
    out = [f"# Решения: '{query}' ({len(decisions)})\n"]
    for r in decisions:
        preview = "\n".join(r["preview"].splitlines()[:8])
        out.append(f"---\n### [{r['project']}] {r['title']} (score: {r['score']})\n{preview}\n")
    return [TextContent(type="text", text="\n".join(out)), *_resource_links(decisions)]


# ─── Templates ─────────────────────────────────────────────────────────────


async def save_from_template(template: str, fields: dict, project: str, tags: list = None) -> list[TextContent]:
    """Create article from template."""
    if template not in TEMPLATES:
        available = ", ".join(TEMPLATES.keys())
        return [TextContent(type="text", text=f"Шаблон '{template}' не найден. Доступные: {available}")]

    tmpl = TEMPLATES[template]
    # Check required fields
    missing = [f for f in tmpl["fields"] if f not in fields]
    if missing:
        return [TextContent(type="text", text=f"Не хватает полей: {', '.join(missing)}. Нужны: {', '.join(tmpl['fields'])}")]

    # Build content from template
    content = tmpl["format"].format(**{f: fields.get(f, "") for f in tmpl["fields"]})
    topic = fields.get("topic") or fields.get(tmpl["fields"][0], template)[:80]

    # Delegate to save_lesson for indexing/git/etc
    return await save_lesson(topic, content, project, tags)


async def list_templates() -> list[TextContent]:
    """List available article templates."""
    out = ["# Шаблоны статей\n"]
    for name, tmpl in TEMPLATES.items():
        fields = ", ".join(tmpl["fields"])
        out.append(f"- **{name}** — {tmpl['description']}\n  Поля: `{fields}`")
    return [TextContent(type="text", text="\n".join(out))]


async def save_secret(topic: str, content: str, project: str, tags: list = None) -> list[TextContent]:
    """Save an encrypted secret article."""
    from memory_compiler.config import MC_ENCRYPT_KEY
    if not MC_ENCRYPT_KEY:
        return [TextContent(type="text", text="MC_ENCRYPT_KEY не задан. Шифрование невозможно.")]

    tags = tags or []
    # auto_tags (фикс.словарь) + безопасные идентификаторы (логин/хост/IP из тела) —
    # чтобы секрет находился по имени сущности (логин/хост), т.к. тело не
    # индексируется. extract_secret_identifiers НЕ тянет значения паролей/токенов.
    auto = auto_tags(content, topic) + extract_secret_identifiers(content, topic)
    existing_lower = {t.lower() for t in tags}
    for t in auto:
        if t.lower() not in existing_lower:
            tags.append(t)
            existing_lower.add(t.lower())
    if "secret" not in existing_lower:
        tags.append("secret")

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    slug = re.sub(r"[^\w\-]", "_", topic.lower())[:50]

    encrypted_body = encrypt_content(content)

    article_text = f"""# {topic}

**Дата:** {ts}
**Проект:** {project}
**Теги:** {', '.join(tags)}
**Секрет:** да

## Содержание

{encrypted_body}
"""
    article_path = safe_project_dir(project) / f"secret_{slug}.md"
    # Коллизия имени: первое свободное имя (дата, затем счётчик) — иначе 3-е сохранение
    # секрета за день с тем же topic перезаписывало 2-е (потеря секрета).
    if article_path.exists():
        base = safe_project_dir(project)
        day = datetime.now().strftime('%Y%m%d')
        article_path = base / f"secret_{slug}_{day}.md"
        n = 2
        while article_path.exists():
            article_path = base / f"secret_{slug}_{day}_{n}.md"
            n += 1
    article_path.write_text(article_text, encoding="utf-8")

    # Index with title+tags only (not encrypted content) for searchability
    index_text = f"# {topic}\n\n**Теги:** {', '.join(tags)}\n\n[зашифрованная статья]"
    await _index_embed(index_text, article_path.name, project)
    await asyncio.to_thread(regenerate_index)
    update_active_context(project, f"Secret: {topic}", "[зашифровано]")
    track_access([f"{project}/{article_path.name}"])
    await asyncio.to_thread(git_commit, f"secret: {topic} [{project}]")

    return [TextContent(type="text", text=f"\U0001f512 Секрет сохранён: {project}/{article_path.name}")]


# ─── Tracking (bi-temporal current state) ────────────────────────────────


async def save_tracking(project: str, entity: str, facts: dict, narrative: str = "") -> list[TextContent]:
    """Save/update tracking article (current state snapshot with history)."""
    from memory_compiler.storage import save_tracking_article
    result = save_tracking_article(project, entity, facts, narrative)

    if result["action"] == "unchanged":
        return [TextContent(type="text", text=f"ℹ️ tracking/{entity} не изменился")]

    if result["action"] == "created":
        msg = f"✅ tracking/{entity} создан в {project}"
    else:
        old_s = ", ".join(f"{k}={v}" for k, v in result["old_current"].items() if k != "since")
        new_s = ", ".join(f"{k}={v}" for k, v in result["new_current"].items() if k != "since")
        msg = f"🔄 tracking/{entity} в {project}\n  было: {old_s}\n  стало: {new_s}"

    fpath = KNOWLEDGE_DIR / result["path"]
    if fpath.exists():
        text = fpath.read_text(encoding="utf-8")
        await _index_embed(text, fpath.name, project)

    await asyncio.to_thread(git_commit, f"tracking: {project}/{entity} {result['action']}")
    return [TextContent(type="text", text=msg)]


async def get_current(project: str, entity: str) -> list[TextContent]:
    """Get current state from tracking article."""
    from memory_compiler.storage import load_tracking, tracking_version_status
    data = load_tracking(project, entity)
    if not data:
        return [TextContent(type="text", text=f"tracking/{entity} не найден в {project}")]

    current = data.get("current") or {}
    history = data.get("history") or []
    lines = [f"# {project}/{entity} — текущее состояние\n"]
    for k, v in current.items():
        lines.append(f"- **{k}:** {v}")
    if history:
        lines.append(f"\n**История:** {len(history)} записей")

    # Read-time авторитет версий (детерминированно, не по датам): максимум по
    # current+history + пометка отката/устаревания. Показываем ТОЛЬКО когда есть что
    # сообщить (max_known != current) — если трекер актуален, шума нет. НЕ мутирует tracking.
    status = tracking_version_status(data)
    if status and status["max_known"] != status["current"]:
        src = "в истории" if status["max_source"] == "history" else "текущая"
        lines.append(f"\n**Макс. известная версия:** {status['max_known']} ({src})")
        if status["stale"]:
            lines.append(
                f"\u26a0\ufe0f Текущая ({status['current']}) ниже максимума истории "
                f"— откат или устаревание трекера."
            )
    return [TextContent(type="text", text="\n".join(lines))]


# ─── Git capture ──────────────────────────────────────────────────────────


_ALLOWED_REPO_ROOTS = ["/repos", "/tmp"]  # configurable via GIT_CAPTURE_ALLOWED_ROOTS env
_SINCE_SAFE_RE = re.compile(r'^[\w\s\-:./,]+$')
_MAX_RAW_INPUT = 5 * 1024 * 1024  # 5 MB


def _validate_repo_path(repo_path: str) -> Optional[str]:
    """Validate repo_path is under allowed roots. Returns error msg or None."""
    import os
    import memory_compiler.config as _cfg

    # Get allowed roots (env override)
    roots_env = os.environ.get("GIT_CAPTURE_ALLOWED_ROOTS")
    roots = roots_env.split(",") if roots_env else _ALLOWED_REPO_ROOTS

    try:
        resolved = os.path.realpath(repo_path)
    except Exception:
        return "Некорректный путь."

    # Must be under at least one allowed root
    for root in roots:
        root_resolved = os.path.realpath(root)
        if resolved == root_resolved or resolved.startswith(root_resolved + os.sep):
            # Explicitly block knowledge dir and app dir
            kd = os.path.realpath(str(_cfg.KNOWLEDGE_DIR))
            if resolved == kd or resolved.startswith(kd + os.sep):
                return "Доступ к knowledge dir запрещён."
            return None

    return f"repo_path должен быть под одним из: {', '.join(roots)}"


async def git_capture(repo_path: str = None, project: str = "", since: str = None,
                      auto_save: bool = False, group_by: str = "prefix",
                      git_log_raw: str = None) -> list[TextContent]:
    """Capture knowledge from git commits.

    Two modes:
    - repo_path: server reads git log directly from a local/mounted repo (must be under /repos or /tmp)
    - git_log_raw: client sends raw output of `git log --format="%H|%s|%an|%aI" --numstat`
    """
    from memory_compiler.storage import (
        parse_git_log, parse_git_log_raw, group_commits, format_capture_group,
        read_last_capture, write_last_capture,
    )

    if not repo_path and not git_log_raw:
        return [TextContent(type="text", text="Нужен repo_path или git_log_raw.")]

    # Validate since (defense in depth — subprocess uses list args, but reject suspicious input)
    if since and not re.match(r'^[0-9a-f]{7,40}$', since) and not _SINCE_SAFE_RE.match(since):
        return [TextContent(type="text", text="since содержит недопустимые символы.")]

    # Limit git_log_raw size (DoS prevention)
    if git_log_raw and len(git_log_raw) > _MAX_RAW_INPUT:
        return [TextContent(type="text", text=f"git_log_raw слишком большой ({len(git_log_raw)} bytes, макс {_MAX_RAW_INPUT}).")]

    source_label = repo_path or "(raw input)"

    if git_log_raw:
        # Parse from raw text — no repo access needed
        commits = parse_git_log_raw(git_log_raw)
    else:
        # Validate repo_path (path traversal prevention)
        path_err = _validate_repo_path(repo_path)
        if path_err:
            return [TextContent(type="text", text=path_err)]

        # Validate repo
        check = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=repo_path, capture_output=True, text=True,
        )
        if check.returncode != 0:
            return [TextContent(type="text", text="Указанный путь — не git-репозиторий.")]

        # Determine since
        effective_since = since
        if not effective_since:
            last_hash = read_last_capture(project, repo_path)
            if last_hash:
                effective_since = last_hash

        commits = parse_git_log(repo_path, effective_since)

    if not commits:
        msg = "Новых коммитов нет." if (since or (repo_path and read_last_capture(project, repo_path))) else "Коммитов не найдено."
        return [TextContent(type="text", text=msg)]

    # Group commits
    groups = group_commits(commits, group_by)

    # Format results
    parts = [f"# Git Capture: {source_label}\n"]
    parts.append(f"**Коммитов:** {len(commits)} | **Групп:** {len(groups)} | **Режим:** {'auto_save' if auto_save else 'preview'}\n")

    saved_count = 0
    for group_name, group_commits_list in sorted(groups.items(), key=lambda x: -len(x[1])):
        content = format_capture_group(group_name, group_commits_list)
        topic = f"git: {group_name} ({len(group_commits_list)} commits)"

        if auto_save:
            result = await save_lesson(
                topic=topic,
                content=content,
                project=project,
                tags=["git-capture", group_name],
            )
            saved_count += 1
            parts.append(f"- Saved: **{group_name}** ({len(group_commits_list)} commits)")
        else:
            parts.append(f"\n## {group_name} ({len(group_commits_list)} commits)\n")
            parts.append(content)

    # Track last captured commit
    if commits and repo_path:
        write_last_capture(project, repo_path, commits[0]["hash"])

    if auto_save:
        parts.append(f"\n*Сохранено {saved_count} статей в проект '{project}'.*")

    return [TextContent(type="text", text="\n".join(parts))]


# ─── Ingest (external sources) ────────────────────────────────────────────


async def ingest(project: str, url: str = None, raw_text: str = None,
                 source: str = None, topic: str = None,
                 auto_save: bool = False) -> list[TextContent]:
    """Ingest knowledge from external sources (URL or raw text).

    Two modes:
    - url: server fetches the page, converts HTML to markdown
    - raw_text + source: client passes pre-extracted text (PDF, etc.)
    """
    from memory_compiler.storage import fetch_url

    if not url and not raw_text:
        return [TextContent(type="text", text="Нужен url или raw_text.")]

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    if url:
        try:
            text, content_type, page_title = fetch_url(url)
        except ValueError as e:
            return [TextContent(type="text", text=f"Ошибка загрузки: {e}")]
        effective_topic = topic or page_title
        effective_source = url
    else:
        text = raw_text
        effective_topic = topic or source or "Ingest"
        effective_source = source or "raw input"

    # Truncate if too long
    max_chars = 50000
    truncated = False
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True

    # Format content with source metadata
    content = f"**Источник:** {effective_source}\n**Дата:** {ts}\n\n{text}"
    if truncated:
        content += f"\n\n*[обрезано до {max_chars} символов]*"

    if auto_save:
        result = await save_lesson(
            topic=effective_topic,
            content=content,
            project=project,
            tags=["ingest", "external"],
        )
        return result
    else:
        # Preview mode — return extracted text
        preview = text[:3000]
        if len(text) > 3000:
            preview += f"\n\n*...ещё {len(text) - 3000} символов*"
        parts = [
            f"# Ingest: {effective_topic}\n",
            f"**Источник:** {effective_source}",
            f"**Размер:** {len(text)} символов",
            f"**Режим:** preview (auto_save=true для сохранения)\n",
            "---\n",
            preview,
        ]
        return [TextContent(type="text", text="\n".join(parts))]


# ─── Import Obsidian vault ────────────────────────────────────────────────


async def import_obsidian(vault_path: str, project: str,
                          folder_mapping: dict = None,
                          dry_run: bool = True,
                          skip_inbox: bool = True) -> list[TextContent]:
    """Import notes from an Obsidian vault into the knowledge base.

    Parses YAML frontmatter, converts wiki-links to bold text, preserves tags.
    folder_mapping maps Obsidian subfolders to KB projects (e.g. {"Работа": "work"}).
    """
    from memory_compiler.storage import parse_obsidian_note, _flatten_import_body, _clean_see_also
    from pathlib import Path

    vault = Path(vault_path)
    if not vault.exists() or not vault.is_dir():
        return [TextContent(type="text", text=f"Vault не найден: {vault_path}")]

    folder_mapping = folder_mapping or {}

    # Collect .md files (skip .obsidian, .git, .trash)
    skip_dirs = {".obsidian", ".git", ".trash"}
    if skip_inbox:
        skip_dirs.add("Inbox")

    notes = []
    for md_path in vault.rglob("*.md"):
        # Skip hidden dirs
        if any(p in skip_dirs for p in md_path.parts):
            continue
        try:
            text = md_path.read_text(encoding="utf-8")
        except Exception:
            continue
        if not text.strip():
            continue
        notes.append((md_path, text))

    # Process
    stats = {"total": len(notes), "saved": 0, "skipped": 0, "errors": 0}
    summaries = []

    for md_path, text in notes:
        rel = md_path.relative_to(vault)
        parts = rel.parts

        # Determine target project via folder mapping
        target_project = project
        for part in parts:
            if part in folder_mapping:
                target_project = folder_mapping[part]
                break

        parsed = parse_obsidian_note(text)
        # Topic: frontmatter.title → first # heading → filename
        topic = parsed["title"]
        if not topic:
            for line in parsed["body"].splitlines()[:20]:
                if line.startswith("# "):
                    topic = line[2:].strip()
                    break
        if not topic:
            topic = md_path.stem

        # Баг 1: сплющить встроенный compiler-scaffold (### <дата>/**Источник:**/дубли),
        # иначе save_lesson обернёт в свой ### ts → два блока.
        # Баг 3: отбросить голые псевдоссылки в «См. также».
        content = _clean_see_also(_flatten_import_body(parsed["body"])).strip()
        if not content:
            stats["skipped"] += 1
            continue
        content = f"**Источник:** Obsidian/{rel.as_posix()}\n\n{content}"

        # Tags: frontmatter tags + "obsidian-import" + folder name
        tags = list(parsed["tags"])
        tags.append("obsidian-import")
        if len(parts) > 1:
            tags.append(parts[0].lower())

        if dry_run:
            summaries.append(f"- [{target_project}] {topic} (tags: {', '.join(tags[:5])})")
            stats["saved"] += 1
        else:
            try:
                # Баг 3: force_new — не мёржить разные Obsidian-заметки в одну статью
                # (склейка роняла «См. также» в чужие блоки). Каждая заметка → своя статья.
                await save_lesson(topic=topic, content=content, project=target_project,
                                  tags=tags, force_new=True)
                stats["saved"] += 1
                if stats["saved"] <= 10:
                    summaries.append(f"✓ [{target_project}] {topic}")
            except Exception as e:
                stats["errors"] += 1
                summaries.append(f"✗ {topic}: {e}")

    mode = "dry-run (preview)" if dry_run else "saved"
    out = [
        f"# Obsidian Import: {vault_path}\n",
        f"**Режим:** {mode}",
        f"**Найдено:** {stats['total']} | **Импортировано:** {stats['saved']} | **Пропущено:** {stats['skipped']} | **Ошибок:** {stats['errors']}\n",
    ]
    if dry_run and len(summaries) > 20:
        out.append("## Первые 20 (всего " + str(len(summaries)) + "):")
        out.extend(summaries[:20])
        out.append(f"\n*...ещё {len(summaries) - 20}. Передайте dry_run=False для импорта.*")
    else:
        out.extend(summaries[:30])
        if len(summaries) > 30:
            out.append(f"*...ещё {len(summaries) - 30}*")

    return [TextContent(type="text", text="\n".join(out))]


# ─── Knowledge gap detector ───────────────────────────────────────────────

# ─── Отчёты живут в handlers_reports (v1.64.0) ───────────────────────────────
# Реэкспорт, а не переезд по вызывающим: tools.py и 26 имён в тестах ходят
# через handlers.<имя>, и ломать этот адрес ради разреза файла незачем.
from memory_compiler.handlers_reports import (  # noqa: E402,F401
    lint, gap_report, consolidate, knowledge_gap, get_summary, article_history,
    stale_facts, stale_summary, _scan_stale, _link_targets, _base_link_index,
    SECRET_POINTER_RE, _MD_LINK_RE, _WIKI_LINK_RE, _strip_code,
    _STALE_CACHE, _STALE_TTL,
)
