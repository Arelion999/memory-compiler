"""Домен статей: запись и чтение содержимого базы знаний.

save_lesson / edit_article / read_article / delete_article, контексты статьи
(save_contexts, context_gaps), справочные типы (save_runbook, save_decision,
save_secret, save_tracking, save_from_template, save_compact), обратные ссылки
(backlinks), приём извне (compile, ingest, import_obsidian, git_capture).

Вынесено из handlers.py в v1.84.0 — четвёртый разрез тем же приёмом
(handlers_reports v1.64.0, journal v1.81.0, handlers_search v1.83.0). Шов выбран
транзитивным замыканием связности: домен тянет наружу ровно один хелпер ядра.

⚠️ _cut_section_body ИМПОРТИРУЕТСЯ ОТЛОЖЕННО внутри context_gaps: он нужен ещё
_render_block и first_touch_context в handlers (старт-контекст) и остаётся там.
Импорт на уровне модуля дал бы цикл handlers ↔ handlers_articles, а отложенный
вдобавок сохраняет тестам патч на handlers.
⚠️ _MD_LINK_RE/_WIKI_LINK_RE/_strip_code берём У ВЛАДЕЛЬЦА (handlers_reports), а не
через handlers: путь через handlers дал бы лишнюю петлю на загрузке.
handlers реэкспортирует все имена — tools.py, тесты и handlers_reports
(_validate_repo_path) ходят через handlers.<имя> как прежде.
"""

import asyncio
import os
import re
import subprocess
from datetime import datetime
from typing import Optional

from mcp.types import TextContent

import memory_compiler.search as _search
from memory_compiler import embed_queue, reflexes
from memory_compiler.config import (
    KNOWLEDGE_DIR, PROJECTS, _discover_projects, article_meta,
    is_secret_article, save_article_meta, track_access,
)
from memory_compiler.search import embed_document, index_document
from memory_compiler.storage import (
    TEMPLATES, article_title_tags, auto_tags, decrypt_content, encrypt_content,
    extract_git_refs, extract_secret_identifiers, find_existing_article, git_commit,
    is_duplicate_entry, is_encrypted, log_event, make_slug, mark_dependents,
    mark_superseded, merge_into_article, project_dir, regenerate_index,
    safe_article_path, safe_project_dir, today_log_path, update_active_context,
    update_cross_references,
)
from memory_compiler.handlers_reports import _MD_LINK_RE, _WIKI_LINK_RE, _strip_code


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


async def save_lesson(topic: str, content: str, project: str, tags: list = None,
                      force_new: bool = False, supersedes: str = "",
                      verified: str = "", triggers: list = None,
                      verify: list = None) -> list[TextContent]:
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
        # ⚠️ «Проверено» — метка ШАПКИ, и она обязана быть в _HEADER_META_PREFIXES
        # (storage), иначе строка утечёт в тело: в превью поиска, в индекс и в
        # ИИ-контексты. На «наивном разборе шапки» база уже горела в v1.43.0.
        vline = f"\n**Проверено:** {verified.strip()}" if verified and verified.strip() else ""
        article_text = f"""# {topic}\n\n**Дата:** {ts}\n**Проект:** {project}\n**Теги:** {', '.join(tags) if tags else '—'}{vline}\n\n## Записи\n\n### {ts}\n{content}\n"""
        article_path.write_text(article_text, encoding="utf-8")
        await asyncio.to_thread(regenerate_index)
        action = f"\u2705 Создано: {project}/{article_path.name}"

    # 3. Git-линковка — извлечь и добавить git-ссылки. Раздел обновляется, не стирая
    # записи, которые merge_into_article дописал после него (см. upsert_git_refs).
    git_refs = extract_git_refs(content, topic)
    if git_refs:
        from memory_compiler.storage import upsert_git_refs
        article_text = article_path.read_text(encoding="utf-8")
        article_path.write_text(upsert_git_refs(article_text, git_refs), encoding="utf-8")

    # 3a. Рефлексы (v1.78.0): при чём статье всплывать самой — раздел «## Рефлексы».
    reflex_note = ""
    if triggers:
        article_text = article_path.read_text(encoding="utf-8")
        new_text, added, rejected = reflexes.add_triggers(article_text, triggers)
        if new_text != article_text:
            article_path.write_text(new_text, encoding="utf-8")
            reflexes.invalidate()
        reflex_note = reflexes.describe_added(added, rejected)

    # Проверка (v1.79.0): чем подтвердить факт об узле живой командой.
    if verify:
        cur = article_path.read_text(encoding="utf-8")
        new_text, v_added, v_rejected = reflexes.add_verify(cur, verify)
        if new_text != cur:
            article_path.write_text(new_text, encoding="utf-8")
            reflexes.invalidate()
        note = reflexes.describe_verify(v_added, v_rejected)
        reflex_note = (reflex_note + "\n" + note).strip() if note else reflex_note

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

    # 11a. Связь «отменяет»: пометить опровергнутые статьи. Неудача (опечатка в
    # имени, файла нет) НЕ роняет сохранение — знание важнее связи.
    superseded_ok = []
    for old_name in [s.strip() for s in (supersedes or "").split(",") if s.strip()]:
        if await asyncio.to_thread(mark_superseded, project, old_name,
                                   article_path.name, topic):
            superseded_ok.append(old_name)

    # 12. Git commit
    await asyncio.to_thread(git_commit, f"save: {topic} [{project}]")

    result = action
    if superseded_ok:
        result += f"\n⚠️ Отменены этой поправкой: {', '.join(superseded_ok)}"
    if reflex_note:
        result += "\n" + reflex_note

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


async def edit_article(project: str, filename: str, content: str = "", append: bool = False,
                       triggers: list = None, verify: list = None) -> list[TextContent]:
    try:
        fpath = safe_article_path(project, filename)
    except ValueError as e:
        return [TextContent(type="text", text=f"❌ Небезопасный путь: {e}")]
    if not fpath.exists():
        return [TextContent(type="text", text=f"Статья не найдена: {project}/{filename}")]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    has_content = bool((content or "").strip())
    if not has_content and not triggers and not verify:
        return [TextContent(type="text",
                            text="❌ Нечего менять: передай content, triggers и/или verify.")]

    old_text = fpath.read_text(encoding="utf-8")
    # Секретность определяется ДО записи: тело такой статьи не должно
    # существовать в открытом виде (инвариант save_secret/read_article).
    is_secret = is_secret_article(old_text, filename)
    if is_secret and has_content:
        from memory_compiler.config import MC_ENCRYPT_KEY
        if not MC_ENCRYPT_KEY:
            return [TextContent(type="text", text=(
                "❌ Секретная статья, но MC_ENCRYPT_KEY не задан — правка отклонена, "
                "чтобы не раскрыть секрет в plaintext."))]

    if has_content and append:
        # Для секрета шифруем дописываемое тело отдельным ENC:-блоком
        # (read_article расшифровывает построчно), заголовок секции — нет.
        body_add = encrypt_content(content) if is_secret else content
        text = old_text.rstrip() + f"\n\n### {ts}\n{body_add}\n"
        fpath.write_text(text, encoding="utf-8")
    elif has_content:
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

    # Рефлексы (v1.78.0): раздел пишется открытым текстом и в секрете — это адреса и
    # тексты ошибок, а не содержимое секрета; тело статьи не расшифровывается.
    reflex_note = ""
    if triggers:
        cur = fpath.read_text(encoding="utf-8")
        new_text, added, rejected = reflexes.add_triggers(cur, triggers)
        if new_text != cur:
            fpath.write_text(new_text, encoding="utf-8")
            reflexes.invalidate()
        reflex_note = reflexes.describe_added(added, rejected)

    # Проверка (v1.79.0): чем подтвердить факт об узле живой командой.
    # ⚠️ Открытым текстом ложится и в секрет, поэтому телом секрета для проверки на
    # утечку идёт `content` — это ЕДИНСТВЕННОЕ расшифрованное тело, которое здесь есть
    # (то, что строкой ниже шифруется в ENC:). У правки без content его нет: хранимое
    # тело не расшифровывается намеренно, иначе секции потребовали бы MC_ENCRYPT_KEY.
    if verify and is_secret and not has_content:
        # ⚠️ FAIL-CLOSED. Цитата ложится в ОТКРЫТУЮ часть секрета, а сверить её с телом
        # тут нечем: хранимое тело намеренно не расшифровывается (иначе секционная
        # правка потребовала бы MC_ENCRYPT_KEY — см. соседний инвариант про триггеры).
        # Молча принять значило бы пропустить в открытый текст пароль из тела статьи.
        reflex_note = (reflex_note + "\n⚠️ Проверка не принята: в секретную статью цитата "
                       "идёт только вместе с content — иначе её не с чем сверить, и "
                       "значение из тела секрета уехало бы в открытую часть.").strip()
    elif verify:
        cur = fpath.read_text(encoding="utf-8")
        new_text, v_added, v_rejected = reflexes.add_verify(
            cur, verify, secret_body=content if is_secret else "")
        if new_text != cur:
            fpath.write_text(new_text, encoding="utf-8")
            reflexes.invalidate()
        note = reflexes.describe_verify(v_added, v_rejected)
        reflex_note = (reflex_note + "\n" + note).strip() if note else reflex_note

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
    # Одни триггеры содержимое не меняют — зависимым нечего помечать обновлёнными.
    cascaded = mark_dependents(project, filename, ts) if has_content else 0

    log_event(project, "edit_article", f"{filename}" + (f" (cascade: {cascaded})" if cascaded else ""))
    await asyncio.to_thread(git_commit, f"edit: {filename} [{project}]")

    if has_content:
        msg = f"\u270f\ufe0f {'Дописано' if append else 'Обновлено'}: {project}/{filename}"
    else:
        msg = f"🧷 Статья: {project}/{filename}"
    if reflex_note:
        msg += "\n" + reflex_note
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
    # Отложенно: _cut_section_body нужен ещё _render_block и first_touch_context в
    # handlers (старт-контекст) и остаётся там; см. шапку модуля.
    from memory_compiler.handlers import _cut_section_body

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
