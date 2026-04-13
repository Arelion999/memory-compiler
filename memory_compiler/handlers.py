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
        merge_into_article(existing, content, tags, ts)
        article_path = existing
        article_text = article_path.read_text(encoding="utf-8")
        action = f"\U0001f504 Обновлено: {project}/{article_path.name}"
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

    # 10. Git commit
    git_commit(f"save: {topic} [{project}]")

    result = action
    if git_refs:
        refs_summary = ", ".join(f"{k}: {', '.join(v)}" for k, v in git_refs.items())
        result += f"\n\U0001f517 Git: {refs_summary}"
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
    """Начать задачу: поиск в базе + загрузка сессии. Один вызов вместо трёх."""
    parts = []

    # 1. Поиск похожих кейсов
    search_results = whoosh_search(topic, project=project, limit=5)
    if search_results:
        track_access([f"{r['project']}/{r['file']}" for r in search_results])
        parts.append(f"# Контекст для: {topic}\n")
        parts.append(f"## Найдено в базе ({len(search_results)} статей)\n")
        for r in search_results:
            preview = "\n".join(r["preview"].splitlines()[:6])
            parts.append(f"### [{r['project']}] {r['title']} (score: {r['score']})\n{preview}\n")
    else:
        parts.append(f"# Контекст для: {topic}\n\nПохожих кейсов не найдено.\n")

    # 2. Загрузка сессии
    target_project = project if project != "all" else (search_results[0]["project"] if search_results else "general")
    session_path = KNOWLEDGE_DIR / target_project / "_session.md"
    if session_path.exists():
        session_text = session_path.read_text(encoding="utf-8")
        parts.append(f"\n## Предыдущая сессия ({target_project})\n{session_text}\n")

    # 3. Active context
    ctx_path = KNOWLEDGE_DIR / target_project / "_active_context.md"
    if ctx_path.exists():
        ctx_text = ctx_path.read_text(encoding="utf-8")
        parts.append(f"\n## Последние действия\n{ctx_text}\n")

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


async def remove_project(name: str) -> list[TextContent]:
    import memory_compiler.config as _cfg
    name = name.strip()
    proj_path = KNOWLEDGE_DIR / name
    if not proj_path.exists():
        return [TextContent(type="text", text=f"Проект '{name}' не найден.")]
    # Посчитать статьи
    articles = list(proj_path.glob("*.md"))
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
