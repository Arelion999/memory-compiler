"""
Tool handler implementations for memory-compiler MCP server.
All async functions return list[TextContent].
"""
import re
import shutil
import subprocess
from datetime import datetime
from typing import Optional

import numpy as np
from mcp.types import TextContent

from memory_compiler.config import (
    KNOWLEDGE_DIR, PROJECTS, track_access, article_meta, save_article_meta,
    _discover_projects,
)
from memory_compiler.search import (
    whoosh_search, index_document, embed_document,
    rebuild_index, rebuild_embeddings, _embeddings, _embed_texts,
)
from memory_compiler.storage import (
    today_log_path, project_dir, find_existing_article,
    merge_into_article, regenerate_index, git_commit,
    update_active_context, detect_contradictions,
    auto_tags, extract_git_refs, format_git_refs,
    update_cross_references,
    extract_snippets, extract_errors, TEMPLATES,
    read_project_deps, write_project_deps,
    encrypt_content, decrypt_content, is_encrypted,
)


# ─── save_lesson ─────────────────────────────────────────────────────────────


async def save_lesson(topic: str, content: str, project: str, tags: list = None, force_new: bool = False) -> list[TextContent]:
    tags = tags or []
    # Автотегирование — дополнить пользовательские теги автоматическими
    auto = auto_tags(content, topic)
    existing_lower = {t.lower() for t in tags}
    tags = tags + [t for t in auto if t not in existing_lower]
    now = datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M")
    slug = re.sub(r"[^\w\-]", "_", topic.lower())[:50]

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
        article_path = project_dir(project) / f"{slug}.md"
        # Handle name collision
        if article_path.exists():
            article_path = project_dir(project) / f"{slug}_{now.strftime('%Y%m%d')}.md"
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
            r = save_tracking_article(project, "release", {"version": version})
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

    # 11. Git commit
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
        # Search in target project + cross-project results
        results = whoosh_search(query, project=project, limit=3)
        cross = whoosh_search(query, project="all", limit=3) if project != "all" else []
        # Add cross-project results that aren't already in main results
        seen = {r["file"] for r in results}
        for r in cross:
            if r["file"] not in seen and r["project"] != project:
                results.append(r)
                if len(results) >= 5:
                    break
        if not results:
            return [TextContent(type="text", text=f"Ничего не найдено по '{query}' в {project}.")]
        out = [f"# Контекст: {project} (query: {query})\n"]
        for r in results:
            preview = "\n".join(r["preview"].splitlines()[:8])
            out.append(f"---\n### [{r['project']}] {r['title']} (score: {r['score']})\n{preview}\n")
        return [TextContent(type="text", text="\n".join(out))]
    else:
        proj_path = project_dir(project)
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


async def search(query: str, project: str = "all") -> list[TextContent]:
    results = whoosh_search(query, project=project, limit=8)
    if not results:
        return [TextContent(type="text", text=f"Ничего не найдено: '{query}'")]

    # Track access
    track_access([f"{r['project']}/{r['file']}" for r in results])

    out = [f"# Поиск: '{query}'\n"]
    for r in results:
        # Hide encrypted content in search results
        if "**Секрет:** да" in r.get("preview", ""):
            r["preview"] = f"# {r['title']}\n\n[зашифровано — используй read_article для просмотра]"
        preview_lines = r["preview"].splitlines()[:10]
        out.append(f"---\n### [{r['project']}] {r['title']} (score: {r['score']})\n" + "\n".join(preview_lines) + "\n")

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
                    slug = re.sub(r'[^\w\-]', '_', entry['topic'].lower())[:50]
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
                    slug = re.sub(r'[^\w\-]', '_', entry['topic'].lower())[:50]
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
            text = a.read_text(encoding="utf-8")
            lines = text.splitlines()

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

        # Check 5: Duplicates (semantic similarity) — compare parent articles only
        proj_embeddings = {k: v for k, v in _embeddings.items()
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
    return [TextContent(type="text", text="\n".join(out))]


# ─── Session Handoff ─────────────────────────────────────────────────────────


async def save_session(project: str, summary: str, decisions: str = "", open_questions: str = "") -> list[TextContent]:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    session_path = project_dir(project) / "_session.md"
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
    session_path = project_dir(project) / "_session.md"
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
    proj_path = project_dir(project)
    articles = sorted(proj_path.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    # Исключаем служебные файлы
    articles = [a for a in articles if not a.name.startswith("_")]
    if not articles:
        return [TextContent(type="text", text=f"Проект {project} пуст.")]

    lines = [f"# {project} \u2014 сводка ({len(articles)} статей)\n"]
    for a in articles[:20]:
        text = a.read_text(encoding="utf-8")
        file_lines = text.splitlines()
        title = file_lines[0].lstrip("# ").strip() if file_lines else a.stem
        tags = ""
        for fl in file_lines[:10]:
            if fl.lower().startswith("**теги:**"):
                tags = fl.split(":", 1)[1].strip()
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
    results = whoosh_search(question, project=project, limit=5)
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
    ctx_path = project_dir(project) / "_active_context.md"
    if not ctx_path.exists():
        return [TextContent(type="text", text=f"Нет активного контекста для {project}.")]
    text = ctx_path.read_text(encoding="utf-8")
    return [TextContent(type="text", text=text)]


# ─── CRUD статей ─────────────────────────────────────────────────────────────


async def delete_article(project: str, filename: str) -> list[TextContent]:
    fpath = KNOWLEDGE_DIR / project / filename
    if not fpath.exists():
        return [TextContent(type="text", text=f"Статья не найдена: {project}/{filename}")]
    fpath.unlink()
    # Remove from indexes
    key = f"{project}/{filename}"
    _embeddings.pop(key, None)
    # Remove chunks too
    chunk_keys = [k for k in _embeddings if k.startswith(key + "#")]
    for ck in chunk_keys:
        _embeddings.pop(ck, None)
    _embed_texts.pop(key, None)
    article_meta.pop(key, None)
    save_article_meta()
    rebuild_index()
    regenerate_index()
    git_commit(f"delete: {filename} [{project}]")
    return [TextContent(type="text", text=f"\U0001f5d1\ufe0f Удалено: {project}/{filename}")]


async def edit_article(project: str, filename: str, content: str, append: bool = False) -> list[TextContent]:
    fpath = KNOWLEDGE_DIR / project / filename
    if not fpath.exists():
        return [TextContent(type="text", text=f"Статья не найдена: {project}/{filename}")]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    if append:
        text = fpath.read_text(encoding="utf-8")
        text = text.rstrip() + f"\n\n### {ts}\n{content}\n"
        fpath.write_text(text, encoding="utf-8")
    else:
        # Сохраняем заголовок и метаданные, заменяем тело
        old_text = fpath.read_text(encoding="utf-8")
        lines = old_text.splitlines()
        header_lines = []
        for line in lines:
            header_lines.append(line)
            if line.strip() == "" and len(header_lines) > 3:
                break
            if line.startswith("**Теги:**"):
                header_lines.append("")
                break
        # Обновляем дату
        header = "\n".join(header_lines)
        if "**Обновлено:**" in header:
            header = re.sub(r"\*\*Обновлено:\*\*.*", f"**Обновлено:** {ts}", header)
        else:
            header = header.rstrip() + f"\n**Обновлено:** {ts}\n"
        fpath.write_text(f"{header}\n{content}\n", encoding="utf-8")

    article_text = fpath.read_text(encoding="utf-8")
    index_document(article_text, filename, project)
    embed_document(article_text, filename, project)
    git_commit(f"edit: {filename} [{project}]")
    return [TextContent(type="text", text=f"\u270f\ufe0f {'Дописано' if append else 'Обновлено'}: {project}/{filename}")]


async def read_article(project: str, filename: str) -> list[TextContent]:
    fpath = KNOWLEDGE_DIR / project / filename
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
    """Начать задачу: hybrid retrieval (BM25+semantic) + cross-encoder rerank + filter by relevance."""
    from memory_compiler.search import rerank
    MIN_SCORE = 15  # min hybrid score
    MIN_RERANK = 0.0  # cross-encoder score threshold (BAAI/bge-reranker-base outputs ~[-10, 10])
    parts = []

    # Topic words for relevance checks
    topic_words = {w.lower() for w in re.split(r'[\s\-_,.:;]+', topic) if len(w) > 3}

    # 1. Hybrid retrieval — берём top-20, ререйнкер выбирает top-3
    candidates = whoosh_search(topic, project=project, limit=20)
    candidates = [r for r in candidates if r.get("score", 0) >= MIN_SCORE]
    reranked = rerank(topic, candidates, top_k=5)
    # Final filter by rerank_score (reranker may say all are weak)
    relevant = [r for r in reranked if r.get("rerank_score", 1.0) >= MIN_RERANK]
    if not relevant and reranked:
        relevant = reranked[:1]  # at least show top-1 even if low

    parts.append(f"# Контекст для: {topic}\n")
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

    # 3. Active context — только записи где title пересекается с темой
    ctx_path = KNOWLEDGE_DIR / target_project / "_active_context.md"
    if ctx_path.exists() and topic_words:
        ctx_text = ctx_path.read_text(encoding="utf-8")
        relevant_lines = []
        for line in ctx_text.splitlines():
            if not line.startswith("- ["):
                continue
            line_words = set(re.findall(r'[а-яА-ЯёЁa-zA-Z]{4,}', line.lower()))
            # match if at least one topic word appears
            if topic_words & line_words:
                relevant_lines.append(line)
        if relevant_lines:
            parts.append(f"\n## Связанные действия в {target_project}\n")
            parts.extend(relevant_lines[:3])
            parts.append("")

    # 4. Session — только если содержит слова темы
    session_path = KNOWLEDGE_DIR / target_project / "_session.md"
    if session_path.exists() and topic_words:
        session_text = session_path.read_text(encoding="utf-8")
        session_words = set(re.findall(r'[а-яА-ЯёЁa-zA-Z]{4,}', session_text.lower()))
        if topic_words & session_words:
            parts.append(f"\n## Предыдущая сессия ({target_project})\n{session_text[:400]}{'...' if len(session_text) > 400 else ''}\n")

    # 5. Search in dependent projects (только релевантные)
    deps = read_project_deps(target_project)
    if deps:
        dep_results = []
        for dep in deps:
            dr = whoosh_search(topic, project=dep, limit=2)
            dep_results.extend([r for r in dr if r.get("score", 0) >= MIN_SCORE])
        if dep_results:
            dep_results.sort(key=lambda r: -r.get("score", 0))
            parts.append(f"\n## Из зависимых проектов ({', '.join(deps)})\n")
            for r in dep_results[:2]:
                preview = "\n".join(r["preview"].splitlines()[:3])
                parts.append(f"### [{r['project']}] {r['title']} (score: {r['score']})\n{preview}\n")

    # 5. Relevant decisions (brief, only high-score)
    decision_results = whoosh_search(topic, project=target_project, limit=10)
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

    parts.append("\n*Задача записана в базу знаний.*")
    return [TextContent(type="text", text="\n".join(parts))]


# ─── Управление проектами ────────────────────────────────────────────────────


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
    name = name.strip()
    proj_path = KNOWLEDGE_DIR / name
    if not proj_path.exists():
        return [TextContent(type="text", text=f"Проект '{name}' не найден.")]
    # Посчитать статьи
    articles = list(proj_path.glob("*.md"))
    # Require explicit confirmation to delete project with articles
    if articles and not confirm:
        return [TextContent(type="text", text=f"⚠️ Проект '{name}' содержит {len(articles)} статей. Для удаления передайте confirm=True. Это действие необратимо.")]
    if articles:
        # Удалить все статьи из индексов
        for md in articles:
            key = f"{name}/{md.name}"
            _embeddings.pop(key, None)
            chunk_keys = [k for k in list(_embeddings.keys()) if k.startswith(key + "#")]
            for ck in chunk_keys:
                _embeddings.pop(ck, None)
            _embed_texts.pop(key, None)
            article_meta.pop(key, None)
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
    results = whoosh_search(query, project=project, limit=10)
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
    slug = re.sub(r"[^\w\-]", "_", topic.lower())[:50]

    steps_text = "\n".join(f"- [ ] {step}" for step in steps)
    article_text = f"""# {topic}

**Дата:** {ts}
**Проект:** {project}
**Теги:** {', '.join(tags)}
**Тип:** runbook

## Шаги

{steps_text}
"""
    article_path = project_dir(project) / f"{slug}.md"
    if article_path.exists():
        article_path = project_dir(project) / f"{slug}_{datetime.now().strftime('%Y%m%d')}.md"
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
    results = whoosh_search(query, project=project, limit=10)

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
    slug = re.sub(r"[^\w\-]", "_", title.lower())[:50]

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
    article_path = project_dir(project) / f"decision_{slug}.md"
    if article_path.exists():
        article_path = project_dir(project) / f"decision_{slug}_{datetime.now().strftime('%Y%m%d')}.md"
    article_path.write_text(article_text, encoding="utf-8")

    index_document(article_text, article_path.name, project)
    embed_document(article_text, article_path.name, project)
    update_active_context(project, f"Decision: {title}", decision)
    regenerate_index()
    git_commit(f"decision: {title} [{project}]")

    return [TextContent(type="text", text=f"\U0001f4cc Решение записано: {project}/{article_path.name}")]


async def search_decisions(query: str, project: str = "all") -> list[TextContent]:
    """Search only decision articles."""
    results = whoosh_search(query, project=project, limit=15)

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
    auto = auto_tags(content, topic)
    existing_lower = {t.lower() for t in tags}
    tags = tags + [t for t in auto if t not in existing_lower]
    if "secret" not in [t.lower() for t in tags]:
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
    article_path = project_dir(project) / f"secret_{slug}.md"
    if article_path.exists():
        article_path = project_dir(project) / f"secret_{slug}_{datetime.now().strftime('%Y%m%d')}.md"
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
