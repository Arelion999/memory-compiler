"""
Storage module: article management, git versioning, and helper utilities.
"""
import re
import subprocess
from datetime import datetime, date
from pathlib import Path
from typing import Optional

from memory_compiler.config import (
    KNOWLEDGE_DIR, PROJECTS, article_meta, save_article_meta,
    _discover_projects,
)

# ─── Utilities ────────────────────────────────────────────────────────────────


def today_log_path() -> Path:
    d = date.today().isoformat()
    p = KNOWLEDGE_DIR / "daily" / f"{d}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def project_dir(project: str) -> Path:
    import memory_compiler.config as _cfg
    p = KNOWLEDGE_DIR / project
    p.mkdir(parents=True, exist_ok=True)
    if project not in _cfg.PROJECTS:
        _cfg.PROJECTS = _discover_projects()
    return p


# ─── Article finding ─────────────────────────────────────────────────────────


def find_existing_article(topic: str, content: str, project: str) -> Optional[Path]:
    """Find existing article by semantic similarity or slug match."""
    from memory_compiler.search import _embeddings, get_embed_model
    import numpy as np

    proj_path = project_dir(project)
    if not proj_path.exists():
        return None

    slug = re.sub(r"[^\w\-]", "_", topic.lower())[:50]
    articles = list(proj_path.glob("*.md"))
    if not articles:
        return None

    # 1. Exact slug match (strip date prefix from old-format filenames)
    for a in articles:
        stem = a.stem
        clean_stem = re.sub(r"^\d{8}_", "", stem)  # remove YYYYMMDD_ prefix
        if clean_stem == slug:
            return a

    # 2. Semantic similarity match
    if not _embeddings:
        return None
    model = get_embed_model()
    query_text = f"{topic} {content[:300]}"
    q_vec = model.encode([query_text], normalize_embeddings=True)[0]

    best_path = None
    best_sim = 0.0
    for key, vec in _embeddings.items():
        if not key.startswith(f"{project}/") or key.startswith("daily/"):
            continue
        sim = float(np.dot(q_vec, vec))
        if sim > best_sim:
            best_sim = sim
            best_path = key

    if best_sim >= 0.75 and best_path:
        candidate = KNOWLEDGE_DIR / best_path
        if candidate.exists():
            return candidate

    return None


# ─── Article merging ─────────────────────────────────────────────────────────


def merge_into_article(article_path: Path, new_content: str, new_tags: list[str], ts: str):
    """Merge new content into existing article, update tags and timestamp."""
    text = article_path.read_text(encoding="utf-8")
    lines = text.splitlines()

    # Update tags — merge old and new
    new_tag_set = set(new_tags) if new_tags else set()
    updated_lines = []
    for line in lines:
        if line.startswith("**Теги:**"):
            old_tags_str = line.split(":", 1)[1].strip().strip("*").strip()
            old_tags = {t.strip().strip("*").strip() for t in old_tags_str.split(",") if t.strip().strip("*").strip() and t.strip() != "—"}
            merged_tags = sorted(old_tags | new_tag_set)
            updated_lines.append(f"**Теги:** {', '.join(merged_tags) if merged_tags else '—'}")
        elif line.startswith("**Обновлено:**"):
            updated_lines.append(f"**Обновлено:** {ts}")
        elif line.startswith("**Дата:**"):
            # Keep original date, add/update Обновлено after it
            updated_lines.append(line)
            # Check if next line is Обновлено — if not, insert it
            idx = lines.index(line)
            if idx + 1 < len(lines) and lines[idx + 1].startswith("**Обновлено:**"):
                pass  # will be updated in the loop
            else:
                updated_lines.append(f"**Обновлено:** {ts}")
        else:
            updated_lines.append(line)

    # Add new entry section
    updated_text = "\n".join(updated_lines)
    if "## Записи" not in updated_text:
        # First update of old-format article: wrap existing content
        # Find where body starts (after metadata)
        body_start = 0
        for i, line in enumerate(updated_lines):
            if line.startswith("**") and ":" in line:
                body_start = i + 1
            elif i > 0 and line.strip() == "" and body_start > 0:
                body_start = i + 1
                break
        # Extract existing body
        existing_body = "\n".join(updated_lines[body_start:]).strip()
        # Find original date
        orig_date = ts
        for line in updated_lines:
            if line.startswith("**Дата:**"):
                orig_date = line.split(":", 1)[1].strip()
                break
        # Rebuild with sections
        header = "\n".join(updated_lines[:body_start])
        updated_text = f"{header}\n\n## Записи\n\n### {orig_date}\n{existing_body}\n\n### {ts}\n{new_content}\n"
    else:
        updated_text += f"\n\n### {ts}\n{new_content}\n"

    article_path.write_text(updated_text, encoding="utf-8")


# ─── Index regeneration ──────────────────────────────────────────────────────


def regenerate_index():
    """Auto-generate index.md from all project articles."""
    import memory_compiler.config as _cfg

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    sections = []
    total = 0

    for proj in _cfg.PROJECTS:
        proj_path = KNOWLEDGE_DIR / proj
        if not proj_path.exists():
            continue
        articles = sorted(proj_path.glob("*.md"))
        if not articles:
            continue
        items = []
        for a in articles:
            text = a.read_text(encoding="utf-8")
            lines = text.splitlines()
            title = lines[0].lstrip("# ").strip() if lines else a.stem
            tags = ""
            for line in lines[:10]:
                if line.lower().startswith("**теги:**"):
                    tags = line.split(":", 1)[1].strip()
                    break
            items.append(f"- [{title}](./{proj}/{a.name}) — {tags}")
        total += len(articles)
        sections.append(f"### {proj} ({len(articles)} ст.)\n" + "\n".join(items))

    # Daily log stats
    daily_dir = KNOWLEDGE_DIR / "daily"
    daily_count = len(list(daily_dir.glob("*.md"))) if daily_dir.exists() else 0

    index_text = f"""# Knowledge Base Index

Автоматически обновлено: {now}

## Проекты

{chr(10).join(sections)}

## Статистика
- Всего статей: {total}
- Daily логов: {daily_count}
- Последнее обновление: {now}
"""
    index_path = KNOWLEDGE_DIR / "index.md"
    index_path.write_text(index_text, encoding="utf-8")


# ─── Git versioning ──────────────────────────────────────────────────────────


def git_init():
    """Initialize git repo in knowledge dir if not exists."""
    git_dir = KNOWLEDGE_DIR / ".git"
    if not git_dir.exists():
        subprocess.run(["git", "init"], cwd=str(KNOWLEDGE_DIR), capture_output=True)
        subprocess.run(["git", "config", "user.email", "memory-compiler@nas"], cwd=str(KNOWLEDGE_DIR), capture_output=True)
        subprocess.run(["git", "config", "user.name", "memory-compiler"], cwd=str(KNOWLEDGE_DIR), capture_output=True)
        # Gitignore for index/cache files
        gitignore = KNOWLEDGE_DIR / ".gitignore"
        gitignore.write_text(".whoosh_index/\n.embeddings.pkl\n", encoding="utf-8")
        git_commit("init knowledge base")


def git_commit(message: str):
    """Stage all and commit."""
    try:
        subprocess.run(["git", "add", "-A"], cwd=str(KNOWLEDGE_DIR), capture_output=True)
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(KNOWLEDGE_DIR), capture_output=True
        )
        if result.returncode != 0:  # there are staged changes
            subprocess.run(
                ["git", "commit", "-m", message],
                cwd=str(KNOWLEDGE_DIR), capture_output=True
            )
    except Exception:
        pass  # git not available — silently skip


# ─── Active context ──────────────────────────────────────────────────────────


def update_active_context(project: str, topic: str, content: str):
    """Обновить файл активного контекста проекта (FIFO, 10 записей)."""
    ctx_path = project_dir(project) / "_active_context.md"
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    brief = content.replace("\n", " ")[:150]
    new_entry = f"- [{now}] **{topic}** — {brief}"

    entries = []
    if ctx_path.exists():
        text = ctx_path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("- ["):
                entries.append(line)

    entries.insert(0, new_entry)
    entries = entries[:10]  # FIFO

    ctx_text = f"# Активный контекст: {project}\n\nПоследние действия:\n" + "\n".join(entries) + "\n"
    ctx_path.write_text(ctx_text, encoding="utf-8")


# ─── Contradiction detection ─────────────────────────────────────────────────

_FACT_PATTERNS = [
    (r'\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b', "IP"),
    (r'\bv?(\d+\.\d+\.\d+)\b', "версия"),
    (r'(https?://[^\s\)]+)', "URL"),
    (r':(\d{2,5})\b', "порт"),
]


def detect_contradictions(new_content: str, project: str, exclude_path: Optional[str] = None) -> list[str]:
    """Найти возможные противоречия с существующими статьями."""
    warnings = []
    # Извлечь факты из нового контента
    new_facts: dict[str, set[str]] = {}
    for pattern, label in _FACT_PATTERNS:
        found = set(re.findall(pattern, new_content))
        if found:
            new_facts[label] = found

    if not new_facts:
        return []

    # Проверить против существующих статей проекта
    proj_path = project_dir(project)
    for md in proj_path.glob("*.md"):
        if md.name.startswith("_"):
            continue
        rel_path = f"{project}/{md.name}"
        if exclude_path and rel_path == exclude_path:
            continue
        text = md.read_text(encoding="utf-8")
        for pattern, label in _FACT_PATTERNS:
            existing = set(re.findall(pattern, text))
            new_vals = new_facts.get(label, set())
            if not new_vals or not existing:
                continue
            # Если и в старом и в новом есть факты одного типа, но разные
            overlap = new_vals & existing
            diff = new_vals - existing
            if diff and existing:
                # Есть новые значения которых нет в старой статье — возможное обновление
                for d in list(diff)[:2]:
                    for e in list(existing)[:2]:
                        if d != e:
                            warnings.append(f"В {md.name} {label}: {e}, а в новой записи: {d}")
                            break

    return warnings[:5]  # макс 5 предупреждений


# ─── Auto-tagging ────────────────────────────────────────────────────────────

_AUTO_TAG_RULES = [
    (r'\b(?:docker|dockerfile|docker-compose|контейнер)\b', 'docker'),
    (r'\b(?:nginx|reverse.proxy|ssl|https)\b', 'nginx'),
    (r'\b(?:1[cсС]|1С|bsl|epf|обработк[аи]|конфигурац)\b', '1c'),
    (r'\b(?:postgres|postgresql|pgdump|pg_dump|миграци[яи]|alembic)\b', 'postgres'),
    (r'\b(?:ssh|paramiko|scp|sftp)\b', 'ssh'),
    (r'\b(?:react|typescript|tsx|vite|shadcn)\b', 'frontend'),
    (r'\b(?:fastapi|uvicorn|pydantic|sqlalchemy)\b', 'backend'),
    (r'\b(?:redis|celery|celery.beat)\b', 'redis'),
    (r'\b(?:mikrotik|firewall|фаервол|маршрут)\b', 'mikrotik'),
    (r'\b(?:nas|synology|dsm)\b', 'nas'),
    (r'\b(?:git|commit|merge|branch|rebase)\b', 'git'),
    (r'\b(?:mcp|claude|anthropic)\b', 'mcp'),
    (r'\b(?:деплой|deploy|прод|production)\b', 'deploy'),
    (r'\b(?:bug|баг|fix|исправлен|ошибк[аи])\b', 'bugfix'),
]


def auto_tags(content: str, topic: str) -> list[str]:
    """Извлечь теги из контента автоматически."""
    text = f"{topic} {content}".lower()
    found = set()
    for pattern, tag in _AUTO_TAG_RULES:
        if re.search(pattern, text, re.IGNORECASE):
            found.add(tag)
    return sorted(found)


# ─── Git-linking ─────────────────────────────────────────────────────────────

_GIT_REF_PATTERNS = [
    (r'(?:^|\s)([a-f0-9]{7,40})(?:\s|$|[,.\)])', "commit"),       # abc1234 or full SHA
    (r'(?:[\w.-]+/[\w.-]+)?#(\d+)', "issue"),                       # #123 or org/repo#123
    (r'\b(v\d+\.\d+(?:\.\d+)?)\b', "tag"),                          # v1.3.47
    (r'\b(?:branch|ветк[аи])\s+["\']?([a-zA-Z][\w/.-]+)', "branch"), # branch feature/xxx
]


def extract_git_refs(content: str, topic: str) -> dict[str, list[str]]:
    """Извлечь упоминания git-объектов из контента."""
    text = f"{topic}\n{content}"
    refs: dict[str, set[str]] = {}
    for pattern, ref_type in _GIT_REF_PATTERNS:
        found = re.findall(pattern, text)
        if found:
            refs.setdefault(ref_type, set()).update(found)
    # Отфильтровать ложные срабатывания для коммитов (исключить даты и т.п.)
    if "commit" in refs:
        refs["commit"] = {c for c in refs["commit"] if not c.isdigit() and len(c) >= 7}
    return {k: sorted(v) for k, v in refs.items() if v}


def format_git_refs(refs: dict[str, list[str]]) -> str:
    """Форматировать git-ссылки для вставки в статью."""
    if not refs:
        return ""
    parts = []
    labels = {"commit": "Коммиты", "issue": "Issues/PR", "tag": "Теги", "branch": "Ветки"}
    for ref_type, values in refs.items():
        label = labels.get(ref_type, ref_type)
        parts.append(f"**{label}:** {', '.join(values)}")
    return "\n".join(parts)


# ─── Cross-references ────────────────────────────────────────────────────────


def update_cross_references(topic: str, project: str, saved_path: str):
    """Добавить ссылки в связанные статьи."""
    from memory_compiler.search import _embeddings, get_embed_model
    import numpy as np

    if not _embeddings:
        return
    model = get_embed_model()
    q_vec = model.encode([topic], normalize_embeddings=True)[0]
    now = datetime.now().strftime("%Y-%m-%d")

    for key, vec in _embeddings.items():
        if key == saved_path or key.startswith("daily/") or "#chunk" in key:
            continue
        sim = float(np.dot(q_vec, vec))
        if sim < 0.55 or sim > 0.85:
            continue  # слишком далеко или слишком близко (дубль)

        fpath = KNOWLEDGE_DIR / key
        if not fpath.exists() or fpath.name.startswith("_"):
            continue

        text = fpath.read_text(encoding="utf-8")
        ref_line = f"- [{topic}](../{saved_path}) ({now})"

        if "## См. также" in text:
            # Не добавлять дубли
            if saved_path in text:
                continue
            text = text.rstrip() + f"\n{ref_line}\n"
        else:
            text = text.rstrip() + f"\n\n## См. также\n{ref_line}\n"

        fpath.write_text(text, encoding="utf-8")
