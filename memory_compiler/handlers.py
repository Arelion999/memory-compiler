"""
Tool handler implementations for memory-compiler MCP server.
All async functions return list[TextContent].
"""
import asyncio
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, date
from typing import Optional

import numpy as np
from mcp.types import TextContent

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
from memory_compiler.storage import (
    today_log_path, project_dir, find_existing_article,
    merge_into_article, regenerate_index, git_commit,
    update_active_context, detect_contradictions,
    auto_tags, extract_secret_identifiers, extract_git_refs, format_git_refs,
    update_cross_references,
    extract_snippets, extract_errors, TEMPLATES,
    read_project_deps, write_project_deps,
    encrypt_content, decrypt_content, is_encrypted,
    log_event, mark_dependents,
    extract_reflections, append_reflections,
    safe_article_path, safe_project_dir, make_slug,
)


# ─── save_lesson ─────────────────────────────────────────────────────────────


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
        # Handle name collision
        if article_path.exists():
            article_path = safe_project_dir(project) / f"{slug}_{now.strftime('%Y%m%d')}.md"
        article_text = f"""# {topic}\n\n**Дата:** {ts}\n**Проект:** {project}\n**Теги:** {', '.join(tags) if tags else '—'}\n\n## Записи\n\n### {ts}\n{content}\n"""
        article_path.write_text(article_text, encoding="utf-8")
        regenerate_index()
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
    index_document(article_text, article_path.name, project)
    embed_document(article_text, article_path.name, project)

    # 6. Обнаружение противоречий
    saved_key = f"{project}/{article_path.name}"
    contradictions = detect_contradictions(content, project, exclude_path=saved_key)

    # 7. Cross-references
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
        import re as _re
        m = _re.search(r'v?(\d+\.\d+\.\d+)', topic) or _re.search(r'v?(\d+\.\d+\.\d+)', content)
        if m:
            version = m.group(1)
            from memory_compiler.storage import save_tracking_article
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
            index_document(updated_text, fpath.name, project)
            embed_document(updated_text, fpath.name, project)

    # 11. Project journal (Karpathy LLM Wiki pattern)
    log_event(project, "save_lesson", f"{topic} → {article_path.name}")

    # 12. Git commit
    git_commit(f"save: {topic} [{project}]")

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
    if contradictions:
        # Filter contradictions that were auto-resolved
        if tracking_updates:
            bad_words = ["версия", "version", "ip", "порт", "port", "url"]
            contradictions = [c for c in contradictions if not any(w in c.lower() for w in bad_words)]
        if contradictions:
            result += "\n\n\u26a0\ufe0f Возможные противоречия:\n" + "\n".join(f"  - {c}" for c in contradictions)
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
            preview = "\n".join(text.splitlines()[:8])
            out.append(f"---\n### {path.stem}\n{preview}\n")
        return [TextContent(type="text", text="\n".join(out))]


# ─── search ──────────────────────────────────────────────────────────────────

# Бюджет времени на cross-encoder rerank. Если модель холодная (лениво грузится
# при первом запросе на NAS) или кандидатов много — predict может не уложиться в
# MCP-таймаут клиента и весь запрос падал в -32001, теряя уже найденные hybrid-хиты.
# По истечении бюджета отдаём результат БЕЗ rerank (мягкая деградация: hybrid-порядок
# хуже reranked, но это лучше пустой ошибки). Настраивается env SEARCH_RERANK_BUDGET_S.
SEARCH_RERANK_BUDGET_S = float(os.environ.get("SEARCH_RERANK_BUDGET_S", "20"))


async def _whoosh_async(query: str, project: str = "all", limit: int = 10) -> list[dict]:
    """whoosh_search в потоке: он CPU-тяжёлый (semantic dot-product по всем эмбеддингам +
    при холодном старте ленивая загрузка embed-модели). На event loop он замораживал весь
    сервер (/api/health, параллельные запросы). Общий помощник для всех async-хендлеров."""
    return await asyncio.to_thread(whoosh_search, query, project=project, limit=limit)


async def _rerank_async(query: str, results: list[dict], top_k: int) -> list[dict]:
    """rerank под бюджетом времени в потоке. При таймауте/ошибке — best-effort: отдаём
    hybrid-результаты как есть (обрезанные до top_k) вместо -32001. wait_for отменяет
    ожидание, но фоновый поток допишет predict вхолостую — результат уже у пользователя."""
    from memory_compiler.search import rerank
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(rerank, query, results, top_k=top_k),
            timeout=SEARCH_RERANK_BUDGET_S,
        )
    except (asyncio.TimeoutError, Exception):
        return results[:top_k]


async def search(query: str, project: str = "all") -> list[TextContent]:
    # Industry pattern 2026: fetch wider candidate pool, then cross-encoder rerank to final K.
    # Bigger N for reranker → +25-40% precision over hybrid alone (RAG benchmarks).
    results = await _whoosh_async(query, project=project, limit=20)

    # Авто-фолбэк на project=all: узкий скоуп часто промахивается по общей сущности,
    # физически лежащей в другом проекте (напр. канал уведомлений / общий креденшл).
    # Вместо «Ничего не найдено» переспрашиваем по всем проектам и помечаем выдачу.
    fallback_all = False
    if not results and project != "all":
        results = await _whoosh_async(query, project="all", limit=20)
        fallback_all = bool(results)

    if not results:
        return [TextContent(type="text", text=f"Ничего не найдено: '{query}'")]

    results = await _rerank_async(query, results, top_k=8)

    track_access([f"{r['project']}/{r['file']}" for r in results])

    header = f"# Поиск: '{query}'\n"
    if fallback_all:
        header += (f"\n*В проекте «{project}» ничего не найдено — показаны результаты "
                   f"по всем проектам (возможно, общая/кросс-проектная сущность).*\n")
    out = [header]
    for r in results:
        if is_secret_article(r.get("preview", ""), r.get("file", "")):
            r["preview"] = f"# {r['title']}\n\n[зашифровано — используй read_article для просмотра]"
        preview_lines = r["preview"].splitlines()[:10]
        scores = f"score: {r['score']}"
        if "rerank_score" in r:
            scores += f", rerank: {r['rerank_score']:.2f}"
        out.append(f"---\n### [{r['project']}] {r['title']} ({scores})\n" + "\n".join(preview_lines) + "\n")

    return [TextContent(type="text", text="\n".join(out))]


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
                if existing:
                    out.append(f"  \U0001f504 Мерж: \u00ab{entry['topic']}\u00bb \u2192 {existing.name}")
                else:
                    slug = make_slug(entry['topic'])
                    out.append(f"  \u2705 Новая: \u00ab{entry['topic']}\u00bb \u2192 {entry['project']}/{slug}.md")
            else:
                ts = entry["timestamp"] or datetime.now().strftime("%Y-%m-%d %H:%M")
                if existing:
                    merge_into_article(existing, entry["content"], entry["tags"], ts)
                    article_text = existing.read_text(encoding="utf-8")
                    index_document(article_text, existing.name, entry["project"])
                    embed_document(article_text, existing.name, entry["project"])
                    updated += 1
                else:
                    slug = make_slug(entry['topic'])
                    article_path = project_dir(entry["project"]) / f"{slug}.md"
                    if article_path.exists():
                        article_path = project_dir(entry["project"]) / f"{slug}_{datetime.now().strftime('%Y%m%d')}.md"
                    article_text = f"# {entry['topic']}\n\n**Дата:** {ts}\n**Проект:** {entry['project']}\n**Теги:** {', '.join(entry['tags']) if entry['tags'] else '\u2014'}\n\n## Записи\n\n### {ts}\n{entry['content']}\n"
                    article_path.write_text(article_text, encoding="utf-8")
                    index_document(article_text, article_path.name, entry["project"])
                    embed_document(article_text, article_path.name, entry["project"])
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

        regenerate_index()
        git_commit(f"compile: {total_entries} entries, {updated} updated, {created} created")
        summary = f"\u2705 Скомпилировано: {total_entries} записей \u2014 {updated} обновлено, {created} создано, {len(processed_logs)} логов архивировано"
        return [TextContent(type="text", text=summary)]


# ─── lint ────────────────────────────────────────────────────────────────────


async def lint(project: str = "all", fix: bool = False) -> list[TextContent]:
    """Check knowledge base health."""
    issues = []
    fixed = []
    check_projects = PROJECTS if project == "all" else [project]

    for proj in check_projects:
        proj_path = KNOWLEDGE_DIR / proj
        if not proj_path.exists():
            continue
        articles = list(proj_path.glob("*.md"))
        if not articles:
            continue

        for a in articles:
            # Service files (_*.md, tracking_*.md) lack yaml metadata
            # by design — they are engine-managed. Skip Check 1/2 for them.
            is_service = a.name.startswith("_") or a.name.startswith("tracking_")
            text = a.read_text(encoding="utf-8")
            lines = text.splitlines()

            if not is_service:
                # Check 1: Empty or minimal
                body = "\n".join(lines[5:]).strip()  # skip header
                if len(body) < 50:
                    issues.append(f"\u26a0\ufe0f [{proj}] {a.name} \u2014 пустая/минимальная статья ({len(body)} символов)")

                # Check 2: Missing metadata
                has_project = any(l.startswith("**Проект:**") for l in lines[:10])
                has_tags = any(l.startswith("**Теги:**") for l in lines[:10])
                has_date = any(l.startswith("**Дата:**") or l.startswith("**Обновлено:**") for l in lines[:10])
                if not has_project or not has_tags or not has_date:
                    missing = []
                    if not has_project: missing.append("Проект")
                    if not has_tags: missing.append("Теги")
                    if not has_date: missing.append("Дата")
                    issues.append(f"\u26a0\ufe0f [{proj}] {a.name} \u2014 нет метаданных: {', '.join(missing)}")

            # Check 3: Stale (>90 days)
            updated = None
            for line in lines[:10]:
                if line.startswith("**Обновлено:**") or line.startswith("**Дата:**"):
                    date_str = line.split(":", 1)[1].strip().lstrip("*").rstrip("*").strip()[:10]
                    try:
                        updated = datetime.strptime(date_str, "%Y-%m-%d")
                    except ValueError:
                        pass
                    break
            if updated and (datetime.now() - updated).days > 90:
                days = (datetime.now() - updated).days
                issues.append(f"\u2139\ufe0f [{proj}] {a.name} \u2014 устарела ({days} дней без обновления)")

            # Check 4: Tag normalization
            for line in lines[:10]:
                if line.startswith("**Теги:**"):
                    tags_str = line.split(":", 1)[1].strip()
                    raw_tags = [t.strip() for t in tags_str.split(",") if t.strip() and t.strip() != "\u2014"]
                    lower_tags = [t.lower() for t in raw_tags]
                    if raw_tags != lower_tags and raw_tags:
                        if fix:
                            new_line = f"**Теги:** {', '.join(lower_tags)}"
                            text = text.replace(line, new_line)
                            a.write_text(text, encoding="utf-8")
                            fixed.append(f"\U0001f527 [{proj}] {a.name} \u2014 теги нормализованы")
                        else:
                            issues.append(f"\u2139\ufe0f [{proj}] {a.name} \u2014 теги с разным регистром: {', '.join(raw_tags)}")
                    break

        # Check 5: Duplicates (semantic similarity) — compare parent articles only.
        # Снимок актуального _embeddings под локом (см. snapshot_embeddings) — иначе
        # фоновый rebuild может мутировать dict во время comprehension (RuntimeError).
        proj_embeddings = {k: v for k, v in _search.snapshot_embeddings().items()
                          if k.startswith(f"{proj}/") and "#chunk" not in k}
        keys = list(proj_embeddings.keys())
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                sim = float(np.dot(proj_embeddings[keys[i]], proj_embeddings[keys[j]]))
                if sim > 0.85:
                    name_i = keys[i].split("/", 1)[-1]
                    name_j = keys[j].split("/", 1)[-1]
                    issues.append(f"\u26a0\ufe0f [{proj}] Возможный дубль (sim={sim:.2f}): {name_i} \u2194 {name_j}")

            # Check 6: Stale rotation (>180 days -> archive)
            if updated and (datetime.now() - updated).days > 180:
                days = (datetime.now() - updated).days
                if fix:
                    archive_dir = proj_path / "archive"
                    archive_dir.mkdir(parents=True, exist_ok=True)
                    a.rename(archive_dir / a.name)
                    fixed.append(f"\U0001f527 [{proj}] {a.name} \u2192 archive/ ({days} дней)")
                else:
                    issues.append(f"\u26a0\ufe0f [{proj}] {a.name} \u2014 кандидат на архивацию ({days} дней)")

        # Check 7: Cross-references — find related articles
        if len(keys) >= 2:
            for key in keys:
                name = key.split("/", 1)[-1]
                related = []
                for other_key in keys:
                    if other_key == key:
                        continue
                    if key in proj_embeddings and other_key in proj_embeddings:
                        sim = float(np.dot(proj_embeddings[key], proj_embeddings[other_key]))
                        if 0.6 < sim < 0.85:  # related but not duplicate
                            other_name = other_key.split("/", 1)[-1]
                            related.append(other_name)
                if related:
                    issues.append(f"\u2139\ufe0f [{proj}] {name} \u2014 связанные: {', '.join(related[:3])}")

        # Check 8: Orphan articles (no inbound refs) — Karpathy LLM Wiki pattern
        # Markdown link parsing (not substring match) avoids false positives
        # from raw filename mentions in prose.
        non_service = [a for a in articles
                       if not a.name.startswith("_")
                       and not a.name.startswith("tracking_")]
        if len(non_service) > 1:
            import re as _re_orph
            md_link_orph = _re_orph.compile(
                r"\[[^\]]+\]\(\s*"
                r"(?:\./)?"
                r"(?:\.\./[\w.\-]+/)?"
                r"([\w.\-]+\.md)\s*\)"
            )
            referenced = set()
            for a in non_service:
                try:
                    body = a.read_text(encoding="utf-8")
                except Exception:
                    continue
                for m in md_link_orph.finditer(body):
                    target = m.group(1)
                    if target != a.name:
                        referenced.add(target)
            for a in non_service:
                if a.name not in referenced:
                    issues.append(f"\u2139\ufe0f [{proj}] {a.name} \u2014 сирота (no inbound refs)")

        # Check 9: Dead cross-references — markdown links to missing .md files
        # Supports: ./file.md, file.md, ../other_proj/file.md (cross-project resolution)
        # Cyrillic filenames covered: \w under Unicode flag matches кириллицу.
        import re as _re
        md_link = _re.compile(
            r"\[[^\]]+\]\(\s*"
            r"(?:\./)?"
            r"(?:\.\./([\w.\-]+)/)?"
            r"([\w.\-]+\.md)\s*\)"
        )
        for a in articles:
            if a.name.startswith("_"):
                continue
            try:
                atext = a.read_text(encoding="utf-8")
            except Exception:
                continue
            seen_dead = set()
            # Two passes: first collect dead refs, then optionally strip them when fix=True.
            dead_matches = []  # list of (match, display) for fix pass
            for m in md_link.finditer(atext):
                cross_proj, target = m.group(1), m.group(2)
                if cross_proj:
                    target_path = KNOWLEDGE_DIR / cross_proj / target
                    display = f"../{cross_proj}/{target}"
                else:
                    target_path = proj_path / target
                    display = target
                if not target_path.exists():
                    if display not in seen_dead:
                        seen_dead.add(display)
                        issues.append(f"\u26a0\ufe0f [{proj}] {a.name} \u2014 dead reference \u2192 {display}")
                    dead_matches.append((m.group(0), display))
            # Fix pass \u2014 replace each `[text](dead.md)` with bare `text`, preserving content
            if fix and dead_matches:
                new_text = atext
                replaced = 0
                for full_match, display in dead_matches:
                    # Extract link text from `[text](url)` and replace whole match with `text`
                    link_text_m = re.match(r"\[([^\]]+)\]\(", full_match)
                    if not link_text_m:
                        continue
                    link_text = link_text_m.group(1)
                    if full_match in new_text:
                        new_text = new_text.replace(full_match, link_text)
                        replaced += 1
                if replaced > 0 and new_text != atext:
                    a.write_text(new_text, encoding="utf-8")
                    fixed.append(f"\U0001f527 [{proj}] {a.name} \u2014 \u0443\u0434\u0430\u043b\u0435\u043d\u043e {replaced} \u0431\u0438\u0442\u044b\u0445 \u0441\u0441\u044b\u043b\u043e\u043a")

    if fix:
        regenerate_index()
        fixed.append("\U0001f527 index.md перегенерирован")

    out = [f"# Lint \u2014 проверка базы знаний\n"]
    if issues:
        out.append(f"## Проблемы ({len(issues)})\n")
        out.extend(issues)
    if fixed:
        out.append(f"\n## Исправлено ({len(fixed)})\n")
        out.extend(fixed)
    if not issues and not fixed:
        out.append("\u2705 Проблем не найдено")
    # Project journal — record what lint found
    for proj in check_projects:
        proj_issues = sum(1 for i in issues if f"[{proj}]" in i)
        proj_fixed = sum(1 for f in fixed if f"[{proj}]" in f)
        if proj_issues or proj_fixed or fix:
            log_event(proj, "lint", f"{proj_issues} issues, {proj_fixed} fixed")

    return [TextContent(type="text", text="\n".join(out))]


# ─── Session Handoff ─────────────────────────────────────────────────────────


async def save_session(project: str, summary: str, decisions: str = "", open_questions: str = "") -> list[TextContent]:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    session_path = safe_project_dir(project) / "_session.md"
    text = f"""# Сессия: {project}

**Дата:** {now}

## Что сделано
{summary}

## Решения
{decisions or '\u2014'}

## Открытые вопросы
{open_questions or '\u2014'}
"""
    session_path.write_text(text, encoding="utf-8")
    git_commit(f"session: {project}")
    return [TextContent(type="text", text=f"\u2705 Контекст сессии сохранён: {project}/_session.md")]


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


async def get_summary(project: str) -> list[TextContent]:
    proj_path = safe_project_dir(project)
    articles = sorted(proj_path.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    # Исключаем служебные файлы
    articles = [a for a in articles if not a.name.startswith("_")]
    if not articles:
        return [TextContent(type="text", text=f"Проект {project} пуст.")]

    lines = [f"# {project} \u2014 сводка ({len(articles)} статей)\n"]
    for a in articles[:20]:
        text = a.read_text(encoding="utf-8")
        file_lines = text.splitlines()
        # Заголовок: первый H1 (# ...), пропуская YAML-frontmatter (---) и пустые строки
        title = a.stem
        for fl in file_lines[:15]:
            s = fl.strip()
            if s.startswith("# "):
                title = s.lstrip("# ").strip()
                break
        tags = ""
        for fl in file_lines[:10]:
            if fl.lower().startswith("**теги:**"):
                # split по первому ':' оставляет закрывающие ** от '**Теги:**' — срезаем
                tags = fl.split(":", 1)[1].strip().lstrip("*").strip()
                break
        # Первые 2 строки тела (после метаданных)
        body_lines = []
        body_started = False
        for fl in file_lines:
            if fl.startswith("## Записи") or fl.startswith("### "):
                body_started = True
                continue
            if body_started and fl.strip() and not fl.startswith("**"):
                body_lines.append(fl.strip())
                if len(body_lines) >= 2:
                    break
        brief = " ".join(body_lines)[:120]
        lines.append(f"- **{title}** ({tags}) \u2014 {brief}")

    return [TextContent(type="text", text="\n".join(lines))]


# ─── ask ─────────────────────────────────────────────────────────────────────


async def ask(question: str, project: str = "all") -> list[TextContent]:
    results = await _whoosh_async(question, project=project, limit=5)
    if not results:
        return [TextContent(type="text", text=f"Не найдено информации по: '{question}'")]

    track_access([f"{r['project']}/{r['file']}" for r in results])

    out = [f"# Ответ на: {question}\n"]
    for r in results:
        # Читаем полный текст статьи для извлечения релевантных фрагментов
        fpath = KNOWLEDGE_DIR / r["project"] / r["file"]
        if not fpath.exists():
            continue
        text = fpath.read_text(encoding="utf-8")
        # Ищем параграфы, содержащие слова из вопроса
        q_words = set(w.lower() for w in question.split() if len(w) > 2)
        sections = text.split("\n### ")
        best_sections = []
        for sec in sections:
            sec_lower = sec.lower()
            matches = sum(1 for w in q_words if w in sec_lower)
            if matches > 0:
                best_sections.append((matches, sec.strip()))
        best_sections.sort(key=lambda x: x[0], reverse=True)

        if best_sections:
            # Берём лучший фрагмент (до 300 символов)
            fragment = best_sections[0][1][:300].strip()
            out.append(f"---\n**[{r['project']}/{r['file']}]** (релевантность: {r['score']})\n> {fragment}\n")
        else:
            preview = "\n".join(r["preview"].splitlines()[:5])
            out.append(f"---\n**[{r['project']}/{r['file']}]** (релевантность: {r['score']})\n> {preview}\n")

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
    _search.remove_embedding(key)
    article_meta.pop(key, None)
    save_article_meta()
    rebuild_index()
    regenerate_index()
    git_commit(f"delete: {filename} [{project}]")
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
    index_document(index_src, filename, project)
    embed_document(index_src, filename, project)

    # Cascade-mark: refresh marker on lines that link to this file
    cascaded = mark_dependents(project, filename, ts)

    log_event(project, "edit_article", f"{filename}" + (f" (cascade: {cascaded})" if cascaded else ""))
    git_commit(f"edit: {filename} [{project}]")

    msg = f"\u270f\ufe0f {'Дописано' if append else 'Обновлено'}: {project}/{filename}"
    if cascaded:
        msg += f"\n\U0001f504 Маркер обновления проставлен в {cascaded} зависимых статьях"
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
            lines = text.splitlines()
            title = lines[0].lstrip("# ").strip() if lines else md.stem
            for line in lines[:10]:
                if line.lower().startswith("**теги:**"):
                    tags_str = line.split(":", 1)[1].strip()
                    article_tags = [t.strip().lower().strip("*").strip() for t in tags_str.split(",")]
                    if tag_lower in article_tags:
                        preview = "\n".join(lines[:8])
                        results.append({"title": title, "project": proj, "file": md.name, "preview": preview})
                    break
    if not results:
        return [TextContent(type="text", text=f"Статей с тегом '{tag}' не найдено.")]
    track_access([f"{r['project']}/{r['file']}" for r in results])
    out = [f"# Тег: {tag} ({len(results)} статей)\n"]
    for r in results:
        out.append(f"---\n### [{r['project']}] {r['title']}\n{r['file']}\n")
    return [TextContent(type="text", text="\n".join(out))]


async def article_history(project: str, filename: str) -> list[TextContent]:
    fpath = KNOWLEDGE_DIR / project / filename
    if not fpath.exists():
        return [TextContent(type="text", text=f"Статья не найдена: {project}/{filename}")]
    rel_path = f"{project}/{filename}"
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-20", "--", rel_path],
            cwd=str(KNOWLEDGE_DIR), capture_output=True, text=True
        )
        log = result.stdout.strip()
        if not log:
            return [TextContent(type="text", text=f"Нет git-истории для {rel_path}")]
        return [TextContent(type="text", text=f"# История: {rel_path}\n\n```\n{log}\n```")]
    except Exception as e:
        return [TextContent(type="text", text=f"Ошибка git: {e}")]


# ─── Комбинированные tools (start/finish task) ──────────────────────────────


async def start_task(topic: str, project: str = "all") -> list[TextContent]:
    """Начать задачу: hybrid retrieval (BM25+semantic) + cross-encoder rerank + filter by relevance.

    Continuation intent: if topic is a generic "continue" phrase (mostly stopwords),
    skip semantic search entirely — load active context + last session for the project.
    Industry pattern: continuation is session restoration, not RAG.
    """
    from memory_compiler.search import is_low_confidence_query
    MIN_SCORE = 15  # min hybrid score
    MIN_RERANK = 0.0  # cross-encoder score threshold (BAAI/bge-reranker-base outputs ~[-10, 10])
    parts = []

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
            parts.append(f"## Найдено ({len(relevant)} релевантных, hybrid+rerank)\n")
            for r in relevant[:3]:
                preview = "\n".join(r["preview"].splitlines()[:4])
                scores = f"hybrid: {r.get('score', 0)}"
                if "rerank_score" in r:
                    scores += f", rerank: {r['rerank_score']:.2f}"
                parts.append(f"### [{r['project']}] {r['title']} ({scores})\n{preview}\n")
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
            if ctx_lines:
                parts.append(f"\n## Недавняя активность в {target_project}\n")
                parts.extend(ctx_lines[:5])
                parts.append("")
        elif topic_words:
            relevant_lines = []
            for line in ctx_text.splitlines():
                if not line.startswith("- ["):
                    continue
                line_words = set(re.findall(r'[а-яА-ЯёЁa-zA-Z]{4,}', line.lower()))
                if topic_words & line_words:
                    relevant_lines.append(line)
            if relevant_lines:
                parts.append(f"\n## Связанные действия в {target_project}\n")
                parts.extend(relevant_lines[:3])
                parts.append("")

    # 4. Session — на continuation показываем всегда, иначе фильтр по словам
    session_path = KNOWLEDGE_DIR / target_project / "_session.md"
    if session_path.exists():
        session_text = session_path.read_text(encoding="utf-8")
        if is_continuation:
            parts.append(f"\n## Предыдущая сессия ({target_project})\n{session_text[:600]}{'...' if len(session_text) > 600 else ''}\n")
        elif topic_words:
            session_words = set(re.findall(r'[а-яА-ЯёЁa-zA-Z]{4,}', session_text.lower()))
            if topic_words & session_words:
                parts.append(f"\n## Предыдущая сессия ({target_project})\n{session_text[:400]}{'...' if len(session_text) > 400 else ''}\n")

    # 4b. Compact history — резюме сжатий контекста (новое в v1.4.0)
    # Continuous memory через compact-границы. Показываем только при continuation
    # или явных topic_words (не засорять обычный поиск).
    compact_path = KNOWLEDGE_DIR / target_project / "_compact_history.md"
    if compact_path.exists() and (is_continuation or topic_words):
        compact_text = compact_path.read_text(encoding="utf-8")
        # Парсим первый ## блок (самый свежий)
        blocks = re.split(r"^## ", compact_text, flags=re.MULTILINE)
        recent_block = blocks[1].strip() if len(blocks) > 1 else ""
        if recent_block:
            parts.append(f"\n## Compact history ({target_project}) — последний сжатый контекст\n## {recent_block[:600]}{'...' if len(recent_block) > 600 else ''}\n")

    # 5. Search in dependent projects (только релевантные)
    deps = read_project_deps(target_project)
    if deps:
        dep_results = []
        for dep in deps:
            dr = await _whoosh_async(topic, project=dep, limit=2)
            dep_results.extend([r for r in dr if r.get("score", 0) >= MIN_SCORE])
        if dep_results:
            dep_results.sort(key=lambda r: -r.get("score", 0))
            parts.append(f"\n## Из зависимых проектов ({', '.join(deps)})\n")
            for r in dep_results[:2]:
                preview = "\n".join(r["preview"].splitlines()[:3])
                parts.append(f"### [{r['project']}] {r['title']} (score: {r['score']})\n{preview}\n")

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
    if decisions_found:
        parts.append(f"\n## Решения по теме\n")
        parts.extend(decisions_found[:3])
        parts.append("")

    # 6. Relevant runbooks (brief, only matching)
    proj_path = KNOWLEDGE_DIR / target_project
    if proj_path.exists():
        runbooks_found = []
        for md in proj_path.glob("*.md"):
            if md.name.startswith("_"):
                continue
            head = md.read_text(encoding="utf-8")[:300]
            if "**Тип:** runbook" not in head:
                continue
            title = head.splitlines()[0].lstrip("# ").strip() if head.splitlines() else md.stem
            # Check relevance: any topic word in title
            topic_words = {w.lower() for w in topic.split() if len(w) > 3}
            if topic_words & {w.lower() for w in title.split()}:
                total = head.count("- [ ]") + head.count("- [x]")
                runbooks_found.append(f"- **{title}** ({md.name}, {total} шагов)")
        if runbooks_found:
            parts.append(f"\n## Runbooks\n")
            parts.extend(runbooks_found[:3])
            parts.append("")

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


async def consolidate(project: str = "all", min_sim: float = 0.90) -> list[TextContent]:
    """Найти семантически похожие статьи в проекте — кандидаты на слияние.

    Использует кэшированные embeddings: попарное cosine similarity между всеми
    статьями проекта. Возвращает группы где sim >= min_sim. НЕ мержит автоматически
    (слияние требует ручной проверки — статьи могут быть тонко разные).

    Параметры:
      project — фильтр ("all" = все)
      min_sim — порог similarity (0.78 — найти близкие но не дубли,
                0.85+ — почти точные дубли)
    """
    # ВАЖНО: импортируем модуль, не значения. _embeddings в search.py
    # переприсваивается (rebuild/load), а импорт `from search import _embeddings`
    # сохранил бы ссылку на устаревший dict.
    import memory_compiler.search as _smod
    import numpy as np
    import memory_compiler.config as _cfg

    if not _smod._embeddings:
        return [TextContent(type="text", text="# Consolidate\n\n*Embeddings ещё не построены. Запусти reindex().*")]

    # Аггрегация: для статей с ### секциями хранятся chunk-keys (path#chunk0).
    # Берём MEAN-vector по всем chunks статьи как её представление.
    # Параллельно фильтруем по проекту и сервисным файлам.
    article_chunks: dict[str, list] = {}  # parent_path -> list of vectors
    for k, v in _smod._embeddings.items():
        parent = k.split("#", 1)[0]
        if "/" not in parent:
            continue
        proj = parent.split("/", 1)[0]
        fname = parent.split("/", 1)[1]
        if fname.startswith("_"):
            continue
        if project != "all" and proj != project:
            continue
        article_chunks.setdefault(parent, []).append(v)

    if len(article_chunks) < 2:
        return [TextContent(type="text", text=(
            f"# Consolidate ({project})\n\n*Меньше 2 статей в выборке — нечего сравнивать.*"
        ))]

    paths = list(article_chunks.keys())
    # Mean-vector per article
    vectors = np.array([np.mean(article_chunks[p], axis=0) for p in paths])
    # Нормализация для cosine = dot product
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1
    vectors = vectors / norms

    # Попарная similarity matrix
    sim_matrix = vectors @ vectors.T
    # Upper triangle (i < j)
    pairs = []
    n = len(paths)
    for i in range(n):
        for j in range(i + 1, n):
            sim = float(sim_matrix[i, j])
            if sim >= min_sim:
                pairs.append({"a": paths[i], "b": paths[j], "sim": sim})

    pairs.sort(key=lambda p: -p["sim"])

    parts = [f"# Consolidate report ({project})\n"]
    parts.append(f"*Порог similarity: {min_sim}, найдено пар: {len(pairs)}*\n")

    if not pairs:
        parts.append("\n*Нет дублей выше порога. База чистая.*")
        return [TextContent(type="text", text="\n".join(parts))]

    parts.append(f"\n## Топ кандидатов на слияние\n")
    parts.append("Каждая строка = пара статей с близкой темой. Проверь вручную — мерж через `edit_article(append=true)` + `delete_article` для дубликата.\n")
    for p in pairs[:25]:
        title_a = _smod._embed_texts.get(p["a"], p["a"]).split("\n")[0][:60]
        title_b = _smod._embed_texts.get(p["b"], p["b"]).split("\n")[0][:60]
        parts.append(f"\n**sim {p['sim']:.2f}**")
        parts.append(f"- A: `{p['a']}` — {title_a}")
        parts.append(f"- B: `{p['b']}` — {title_b}")

    if len(pairs) > 25:
        parts.append(f"\n*…и ещё {len(pairs) - 25} пар.*")

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


async def stale_facts(project: str = "all", warn_days: int = 30) -> list[TextContent]:
    """Найти статьи с устаревающими фактами: SSL-сертификаты, версии, expiration dates.

    Сканирует статьи на:
      1. Даты «valid until», «до», «expires», «истекает»
      2. Tracking-статьи с полями `until`, `expires`, `valid_to`
      3. Статьи старше 180 дней с тегами ssl/cert/password/license — кандидаты на ротацию

    Параметры:
      project   — фильтр по проекту ("all" = все)
      warn_days — за сколько дней начинать предупреждать (default 30)
    """
    from memory_compiler.storage import _parse_frontmatter
    import memory_compiler.config as _cfg

    today = datetime.now().date()
    warn_until = today + timedelta(days=warn_days)
    stale_180 = today - timedelta(days=180)

    # Regex для дат вида: "valid until 2026-10-11", "до 11.10.2026", "expires 2026/10/11"
    DATE_PATTERNS = [
        # ISO YYYY-MM-DD with prefix
        re.compile(r'(?:valid\s*(?:until|to|till)|до|expires?|истекает|действителен\s*до|valid_to)\s*[:=]?\s*(\d{4})[-/](\d{1,2})[-/](\d{1,2})', re.IGNORECASE),
        # DD.MM.YYYY with prefix
        re.compile(r'(?:valid\s*(?:until|to|till)|до|expires?|истекает|действителен\s*до)\s*[:=]?\s*(\d{1,2})[./](\d{1,2})[./](\d{4})', re.IGNORECASE),
    ]

    # Список проектов для скана
    projects = []
    for p in _cfg.PROJECTS:
        if p == "daily":
            continue
        if project != "all" and p != project:
            continue
        projects.append(p)

    expired = []      # дата уже прошла
    expiring = []     # < warn_days
    stale_secrets = []  # старше 180 дней + тег ssl/cert/password/license

    for proj in projects:
        proj_path = project_dir(proj)
        for md in proj_path.glob("*.md"):
            if md.name.startswith("_"):
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue

            # Title (первая # строка)
            title = md.stem
            for line in text.splitlines()[:5]:
                if line.startswith("# "):
                    title = line[2:].strip()
                    break

            # 1. Поиск дат-expirations в тексте + tracking frontmatter
            found_dates = []
            for pat in DATE_PATTERNS:
                for m in pat.finditer(text):
                    g = m.groups()
                    try:
                        if pat is DATE_PATTERNS[0]:
                            y, mo, d = int(g[0]), int(g[1]), int(g[2])
                        else:
                            d, mo, y = int(g[0]), int(g[1]), int(g[2])
                        dt = date(y, mo, d)
                        # Sanity: ignore years <2020 or >2050
                        if 2020 <= y <= 2050:
                            found_dates.append(dt)
                    except (ValueError, TypeError):
                        continue

            # Tracking frontmatter: current.until / expires / valid_to
            try:
                fm, _ = _parse_frontmatter(text)
                if isinstance(fm, dict):
                    current = fm.get("current") if isinstance(fm.get("current"), dict) else {}
                    for key in ("until", "expires", "valid_to", "valid_until"):
                        v = current.get(key) or fm.get(key)
                        if isinstance(v, str):
                            for pat in DATE_PATTERNS:
                                m = pat.search(f"until {v}")
                                if m:
                                    g = m.groups()
                                    try:
                                        if pat is DATE_PATTERNS[0]:
                                            dt = date(int(g[0]), int(g[1]), int(g[2]))
                                        else:
                                            dt = date(int(g[2]), int(g[1]), int(g[0]))
                                        found_dates.append(dt)
                                    except Exception:
                                        pass
            except Exception:
                pass

            # Классифицировать
            for dt in found_dates:
                rel = f"{proj}/{md.name}"
                days_left = (dt - today).days
                entry = {"path": rel, "title": title, "date": dt.isoformat(), "days_left": days_left}
                if days_left < 0:
                    expired.append(entry)
                elif days_left <= warn_days:
                    expiring.append(entry)

            # 2. Старые secret/ssl/cert/license статьи (по тегам)
            for line in text.splitlines()[:15]:
                if not line.lower().startswith("**теги:**") and not line.lower().startswith("теги:"):
                    continue
                tags_lower = line.lower()
                if any(t in tags_lower for t in ("ssl", "cert", "password", "creds", "license", "лицензи", "секрет", "secret")):
                    mtime = date.fromtimestamp(md.stat().st_mtime)
                    if mtime < stale_180:
                        days_old = (today - mtime).days
                        stale_secrets.append({"path": f"{proj}/{md.name}", "title": title, "age_days": days_old})
                    break

    # Дедуп
    def dedup(items, key="path"):
        seen = set()
        out = []
        for it in items:
            k = (it[key], it.get("date", ""))
            if k in seen:
                continue
            seen.add(k)
            out.append(it)
        return out

    expired = sorted(dedup(expired), key=lambda x: x["days_left"])
    expiring = sorted(dedup(expiring), key=lambda x: x["days_left"])
    stale_secrets = sorted({s["path"]: s for s in stale_secrets}.values(),
                            key=lambda x: -x["age_days"])

    parts = [f"# Stale Facts Report{(' (' + project + ')') if project != 'all' else ''}\n"]

    parts.append(f"\n## ⚠️ Уже истекло ({len(expired)})\n")
    if expired:
        for e in expired[:15]:
            parts.append(f"- **{e['title']}** ({e['path']}) — {e['date']}, {-e['days_left']} дн назад")
    else:
        parts.append("*Нет истёкших фактов.*")

    parts.append(f"\n## 🔔 Истекает в ближайшие {warn_days} дн ({len(expiring)})\n")
    if expiring:
        for e in expiring[:15]:
            parts.append(f"- **{e['title']}** ({e['path']}) — {e['date']}, осталось {e['days_left']} дн")
    else:
        parts.append("*Ничего не истекает в ближайшее время. 👍*")

    parts.append(f"\n## 🕰️ Секреты/сертификаты старше 180 дней — рассмотреть ротацию ({len(stale_secrets)})\n")
    if stale_secrets:
        for s in stale_secrets[:15]:
            parts.append(f"- **{s['title']}** ({s['path']}) — {s['age_days']} дн без обновления")
    else:
        parts.append("*Все секреты свежие.*")

    return [TextContent(type="text", text="\n".join(parts))]


async def gap_report(project: str = "all", days: int = 30, limit: int = 10) -> list[TextContent]:
    """Knowledge gap report — выявить чего не хватает в базе знаний.

    Анализирует audit-лог за последние N дней и находит:
      1. Поиски с пустыми / слабыми результатами (top_score < 35) — что ищут, но не находят
      2. Топ часто-запрашиваемые темы — нагрузка на каждый проект
      3. Проекты-сироты — мало статей или мало внешних обращений

    Параметры:
      project  — фильтр по проекту ("all" = все)
      days     — окно в днях (default 30)
      limit    — top-N результатов в каждой секции
    """
    from memory_compiler.storage import read_audit_log
    from memory_compiler.search import is_low_confidence_query
    import memory_compiler.config as _cfg

    # Берём с большим запасом — фильтруем по дате потом
    entries = read_audit_log(limit=5000)
    if not entries:
        return [TextContent(type="text", text="# Knowledge Gap Report\n\n*Audit-лог пуст — нет данных для анализа.*")]

    cutoff_dt = datetime.now() - timedelta(days=days)
    cutoff_ts = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")

    # Извлечь поисковые запросы (search, start_task, ask, search_error, search_decisions, search_snippets)
    SEARCH_TOOLS = {"search", "start_task", "ask", "search_error", "search_decisions", "search_snippets", "get_context"}
    queries: list[dict] = []  # {q, tool, project, ts}
    for e in entries:
        if e.get("ts", "") < cutoff_ts:
            continue
        if e.get("tool") not in SEARCH_TOOLS:
            continue
        args = e.get("args", {})
        # Разные tools называют запрос по-разному
        q = args.get("query") or args.get("topic") or args.get("question") or args.get("error_text", "")
        if not q or not isinstance(q, str):
            continue
        proj = args.get("project", "all")
        if project != "all" and proj != "all" and proj != project:
            continue
        queries.append({"q": q, "tool": e["tool"], "project": proj, "ts": e["ts"]})

    if not queries:
        return [TextContent(type="text", text=f"# Knowledge Gap Report\n\n*За {days} дн нет поисковых запросов{(' для проекта ' + project) if project != 'all' else ''}.*")]

    # 1. Найти запросы с пустым / слабым результатом.
    # Дополнительная фильтрация: даже если whoosh_search вернул пусто, проверяем
    # semantic cosine — статья по этой теме могла появиться позже, после промаха.
    # Такие «решённые» gaps не показываем — фокусируемся на актуальных.
    from memory_compiler.search import semantic_search
    SOLVED_THRESHOLD = 0.55  # cosine sim к существующим статьям

    # Свежие запросы приоритетнее (актуальные пробелы), а дорогую re-search
    # (whoosh + semantic e5-encode на КАЖДЫЙ запрос) ограничиваем — иначе на
    # большом логе gap_report таймаутит на NAS.
    queries.sort(key=lambda x: x.get("ts", ""), reverse=True)
    _GAP_MAX_CHECKS = 50

    empty_queries: list[dict] = []
    seen_queries: set[str] = set()  # дедупликация по тексту
    checks = 0
    for item in queries:
        q = item["q"].strip()
        if is_low_confidence_query(q):
            continue
        if q.lower() in seen_queries:
            continue
        seen_queries.add(q.lower())
        if checks >= _GAP_MAX_CHECKS:
            break
        checks += 1
        try:
            results = await _whoosh_async(q, project=item["project"] if item["project"] != "all" else "all", limit=3)
        except Exception:
            continue
        # whoosh_search вернул что-то с приличным score — это НЕ gap
        if results and results[0].get("score", 0) >= 35 and results[0].get("confidence") != "low":
            continue
        # Решён? Semantic similarity к ближайшей статье в КАКОМ-ЛИБО проекте.
        # Даже если запрос делался с project=infra, статья может быть в memory-compiler —
        # для целей gap-анализа это означает «знание есть, просто scope неверный»,
        # что является retrieval-проблемой, а не gap.
        try:
            sem_hits = await asyncio.to_thread(semantic_search, q, limit=1)
            if sem_hits and sem_hits[0][1] >= SOLVED_THRESHOLD:
                continue  # solved somewhere — not a real gap
        except Exception:
            pass
        # Реальный gap
        top_score = results[0].get("score", 0) if results else 0
        empty_queries.append({**item, "top_score": top_score})

    # 2. Топ часто-запрашиваемые темы (по content tokens)
    from memory_compiler.search import _content_tokens
    topic_freq: dict[str, int] = {}
    for item in queries:
        for tok in _content_tokens(item["q"]):
            if len(tok) >= 4:  # фильтр коротких токенов
                topic_freq[tok] = topic_freq.get(tok, 0) + 1
    top_topics = sorted(topic_freq.items(), key=lambda kv: -kv[1])[:limit]

    # 3. Проекты-сироты — проекты с малым числом статей
    project_stats = []
    for proj in _cfg.PROJECTS:
        if proj == "daily":
            continue
        if project != "all" and proj != project:
            continue
        try:
            count = len(list(project_dir(proj).glob("*.md")))
        except Exception:
            count = 0
        project_stats.append((proj, count))
    project_stats.sort(key=lambda kv: kv[1])
    orphan_projects = [(p, c) for p, c in project_stats if c <= 2][:limit]

    # Формируем отчёт
    parts = [f"# Knowledge Gap Report ({days} дн{', проект: ' + project if project != 'all' else ''})\n"]
    parts.append(f"*Проанализировано {len(queries)} поисковых запросов.*\n")

    parts.append(f"\n## 1. Реальные gaps — запросы без покрытия ({len(empty_queries)})\n")
    if empty_queries:
        parts.append(f"Запросы где НИ BM25 (>=35), НИ semantic-similarity к существующим статьям (>=`{SOLVED_THRESHOLD}`) не нашли ничего. Это актуальные пробелы — кандидаты на новые статьи:\n")
        for item in empty_queries[:limit]:
            score_info = f"score: {item['top_score']:.0f}" if item['top_score'] > 0 else "пусто"
            parts.append(f"- «{item['q'][:80]}» ({item['tool']}, {item['project']}, {score_info})")
    else:
        parts.append("*Все запросы получали релевантные ответы. 👍*")

    parts.append(f"\n## 2. Топ темы в запросах\n")
    if top_topics:
        parts.append("Слова которые чаще всего ищут — проверь покрытие в базе:\n")
        for tok, freq in top_topics:
            parts.append(f"- **{tok}** — {freq} раз")
    else:
        parts.append("*Недостаточно данных для топа.*")

    parts.append(f"\n## 3. Проекты-сироты (≤2 статей)\n")
    if orphan_projects:
        parts.append("Малонаполненные проекты — возможно стоит влить в соседние:\n")
        for proj, count in orphan_projects:
            parts.append(f"- `{proj}` — {count} статей")
    else:
        parts.append("*Все проекты заполнены нормально.*")

    return [TextContent(type="text", text="\n".join(parts))]


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
    git_commit(f"add project: {name}")
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
        # Удалить все статьи из индексов (persist=False в цикле, один персист в конце)
        for md in articles:
            key = f"{name}/{md.name}"
            _search.remove_embedding(key, persist=False)
            article_meta.pop(key, None)
        _search.persist_embeddings()
    # Удалить папку
    shutil.rmtree(str(proj_path))
    save_article_meta()
    _cfg.PROJECTS[:] = _discover_projects()
    rebuild_index()
    regenerate_index()
    git_commit(f"remove project: {name} ({len(articles)} articles)")
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
    for s in found[:10]:
        out.append(f"---\n**[{s['article']}]** ({s['lang']}) — {s['context']}\n```{s['lang']}\n{s['code']}\n```\n")
    return [TextContent(type="text", text="\n".join(out))]


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

    index_document(article_text, article_path.name, project)
    embed_document(article_text, article_path.name, project)
    regenerate_index()
    git_commit(f"runbook: {topic} [{project}]")

    return [TextContent(type="text", text=f"\U0001f4cb Runbook создан: {project}/{article_path.name} ({len(steps)} шагов)")]


async def get_runbook(project: str, filename: str) -> list[TextContent]:
    """Read runbook and parse step statuses."""
    fpath = KNOWLEDGE_DIR / project / filename
    if not fpath.exists():
        return [TextContent(type="text", text=f"Runbook не найден: {project}/{filename}")]
    text = fpath.read_text(encoding="utf-8")
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
    return [TextContent(type="text", text="\n".join(out))]


# ─── Project dependencies ─────────────────────────────────────────────────


async def set_project_deps(project: str, depends_on: list) -> list[TextContent]:
    """Set project dependencies."""
    # Validate projects exist
    for dep in depends_on:
        if dep == project:
            return [TextContent(type="text", text=f"Проект не может зависеть от себя.")]

    write_project_deps(project, depends_on)
    git_commit(f"deps: {project} -> {', '.join(depends_on)}")
    return [TextContent(type="text", text=f"\U0001f517 Зависимости {project}: {', '.join(depends_on) if depends_on else 'нет'}")]


async def get_project_deps(project: str) -> list[TextContent]:
    """Get project dependencies."""
    deps = read_project_deps(project)
    if not deps:
        return [TextContent(type="text", text=f"Проект {project} не имеет зависимостей.")]
    return [TextContent(type="text", text=f"\U0001f517 {project} зависит от: {', '.join(deps)}")]


# ─── Decisions ─────────────────────────────────────────────────────────────


async def save_decision(title: str, decision: str, alternatives: str, reasoning: str,
                        project: str, tags: list = None) -> list[TextContent]:
    """Save an architectural/technical decision."""
    tags = tags or []
    auto = auto_tags(f"{decision} {reasoning}", title)
    existing_lower = {t.lower() for t in tags}
    tags = tags + [t for t in auto if t not in existing_lower]
    if "decision" not in [t.lower() for t in tags]:
        tags.append("decision")

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    slug = make_slug(title)

    article_text = f"""# {title}

**Дата:** {ts}
**Проект:** {project}
**Теги:** {', '.join(tags)}
**Тип:** decision

## Решение
{decision}

## Альтернативы
{alternatives}

## Обоснование
{reasoning}
"""
    article_path = safe_project_dir(project) / f"decision_{slug}.md"
    if article_path.exists():
        article_path = safe_project_dir(project) / f"decision_{slug}_{datetime.now().strftime('%Y%m%d')}.md"
    article_path.write_text(article_text, encoding="utf-8")

    index_document(article_text, article_path.name, project)
    embed_document(article_text, article_path.name, project)
    update_active_context(project, f"Decision: {title}", decision)
    regenerate_index()
    git_commit(f"decision: {title} [{project}]")

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
    return [TextContent(type="text", text="\n".join(out))]


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
    if article_path.exists():
        article_path = safe_project_dir(project) / f"secret_{slug}_{datetime.now().strftime('%Y%m%d')}.md"
    article_path.write_text(article_text, encoding="utf-8")

    # Index with title+tags only (not encrypted content) for searchability
    index_text = f"# {topic}\n\n**Теги:** {', '.join(tags)}\n\n[зашифрованная статья]"
    index_document(index_text, article_path.name, project)
    embed_document(index_text, article_path.name, project)
    regenerate_index()
    update_active_context(project, f"Secret: {topic}", "[зашифровано]")
    track_access([f"{project}/{article_path.name}"])
    git_commit(f"secret: {topic} [{project}]")

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
        index_document(text, fpath.name, project)
        embed_document(text, fpath.name, project)

    git_commit(f"tracking: {project}/{entity} {result['action']}")
    return [TextContent(type="text", text=msg)]


async def get_current(project: str, entity: str) -> list[TextContent]:
    """Get current state from tracking article."""
    from memory_compiler.storage import load_tracking
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
    from memory_compiler.storage import parse_obsidian_note
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

        content = parsed["body"].strip()
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
                await save_lesson(topic=topic, content=content, project=target_project, tags=tags)
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


async def knowledge_gap(repo_path: str = None, project: str = "all",
                        days: int = 30, git_log_raw: str = None) -> list[TextContent]:
    """Find topics active in git commits but missing in the knowledge base.

    Extracts topics from commit messages (conventional prefix + file paths),
    compares against existing articles via semantic similarity.
    Returns ranked list of gaps — topics with low KB coverage.
    """
    from memory_compiler.storage import parse_git_log, parse_git_log_raw, group_commits
    from memory_compiler.search import _embeddings, get_embed_model

    # Get commits
    if git_log_raw:
        commits = parse_git_log_raw(git_log_raw)
    elif repo_path:
        from memory_compiler.handlers import _validate_repo_path
        err = _validate_repo_path(repo_path)
        if err:
            return [TextContent(type="text", text=err)]
        commits = parse_git_log(repo_path, f"{days} days ago")
    else:
        return [TextContent(type="text", text="Нужен repo_path или git_log_raw.")]

    if not commits:
        return [TextContent(type="text", text="Коммитов не найдено.")]

    # Extract topic candidates from commit messages
    # Strip conventional prefix, split by conjunctions, collect noun-phrase-ish chunks
    topics = {}
    for c in commits:
        msg = c["message"]
        # Strip prefix
        msg = re.sub(r'^(fix|feat|refactor|docs|chore|build|test|style|perf|ci)[\(:][^:]*:\s*', '', msg, flags=re.IGNORECASE)
        msg = re.sub(r'^(fix|feat|refactor|docs|chore|build|test|style|perf|ci):\s*', '', msg, flags=re.IGNORECASE)
        # Take first 60 chars as topic candidate
        topic_text = msg[:80].strip()
        if len(topic_text) < 10:
            continue
        topics[topic_text] = topics.get(topic_text, 0) + 1

    if not topics:
        return [TextContent(type="text", text="Не удалось извлечь темы из коммитов.")]

    # Compute coverage via semantic similarity with existing articles
    model = get_embed_model()
    if not model:
        return [TextContent(type="text", text="Embeddings недоступны.")]

    # Filter embeddings by project
    kb_keys = [k for k in _embeddings.keys() if "#chunk" not in k]
    if project and project != "all":
        kb_keys = [k for k in kb_keys if k.startswith(f"{project}/")]
    if not kb_keys:
        return [TextContent(type="text", text=f"В проекте '{project}' нет статей для сравнения.")]

    # Encode topics
    topic_list = list(topics.keys())
    topic_vectors = model.encode(topic_list, show_progress_bar=False)

    # Find max similarity for each topic
    import numpy as np
    gaps = []
    for i, topic_text in enumerate(topic_list):
        tv = topic_vectors[i]
        tv = tv / (np.linalg.norm(tv) + 1e-8)
        max_sim = 0.0
        best_match = None
        for k in kb_keys:
            kv = _embeddings[k]
            sim = float(np.dot(tv, kv))
            if sim > max_sim:
                max_sim = sim
                best_match = k
        gaps.append({
            "topic": topic_text,
            "count": topics[topic_text],
            "max_sim": max_sim,
            "best_match": best_match,
        })

    # Sort by count desc + low similarity = real gaps
    gaps.sort(key=lambda g: (-g["count"], g["max_sim"]))

    # Filter: gap = similarity < 0.5
    real_gaps = [g for g in gaps if g["max_sim"] < 0.5]

    out = [f"# Knowledge Gap Report\n"]
    out.append(f"**Коммитов:** {len(commits)} | **Тем:** {len(topics)} | **Пробелов:** {len(real_gaps)}\n")

    if real_gaps:
        out.append("## Пробелы (нет статей)\n")
        for g in real_gaps[:15]:
            out.append(f"- **{g['topic']}** (×{g['count']}, max_sim: {g['max_sim']:.2f})")
    else:
        out.append("*Все темы покрыты статьями с достаточным сходством.*")

    # Well-covered for reference
    covered = [g for g in gaps if g["max_sim"] >= 0.5]
    if covered:
        out.append("\n## Покрытые темы (для справки)\n")
        for g in covered[:5]:
            out.append(f"- {g['topic']} → {g['best_match']} ({g['max_sim']:.2f})")

    return [TextContent(type="text", text="\n".join(out))]
