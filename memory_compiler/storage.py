"""
Storage module: article management, git versioning, and helper utilities.
"""
import base64
import hashlib
import ipaddress
import json
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


def normalize_project(project: str) -> str:
    """Normalize project name to canonical form (Jira pattern: lowercase + trim).

    All project lookups go through this. Eliminates MyProj vs myproj duplication.
    Whitespace-only or empty names default to 'general'.
    """
    if not project:
        return "general"
    return project.strip().lower()


def safe_project_dir(project: str) -> Path:
    """Return KNOWLEDGE_DIR/<project> only if project name is safe.

    Rejects empty, '.', '..', anything with path separators or traversal — these
    would resolve OUT of any project directory (back to KNOWLEDGE_DIR root or
    higher). Same defense-in-depth as safe_article_path, for handlers that need
    just the project directory (save_lesson, save_session, etc).
    """
    if not project or not project.strip():
        raise ValueError(f"empty project: {project!r}")
    if project in (".", "..") or "/" in project or "\\" in project or ".." in project:
        raise ValueError(f"unsafe project: {project!r}")
    proj = project_dir(project)
    kd = KNOWLEDGE_DIR.resolve()
    proj_r = proj.resolve()
    # Project must be a strict subdir of KNOWLEDGE_DIR (not KNOWLEDGE_DIR itself).
    if proj_r == kd or kd not in proj_r.parents:
        raise ValueError(f"project escapes KNOWLEDGE_DIR: {project!r}")
    return proj


def safe_article_path(project: str, filename: str) -> Path:
    """Return KNOWLEDGE_DIR/<project>/<filename> only if it stays inside the project dir.

    Raises ValueError on traversal attempts (../, absolute paths, project names
    that escape KNOWLEDGE_DIR or resolve to KNOWLEDGE_DIR itself). Defense-in-
    depth: even though MC_API_KEY guards the HTTP/MCP endpoints, we still must
    not let a crafted filename read or overwrite arbitrary host files.
    """
    if not filename:
        raise ValueError(f"empty filename: {filename!r}")
    # Filenames must be flat — no subdirs, no traversal, no absolute paths.
    if "/" in filename or "\\" in filename or ".." in filename:
        raise ValueError(f"unsafe filename: {filename!r}")
    proj = safe_project_dir(project)  # delegates project validation
    kd = KNOWLEDGE_DIR.resolve()
    candidate = (proj / filename).resolve()
    if kd not in candidate.parents:
        raise ValueError(f"path escapes KNOWLEDGE_DIR: {project}/{filename}")
    return proj / filename


def project_dir(project: str) -> Path:
    """Get project directory, normalizing the name first.

    If a directory with the original (non-normalized) case exists alongside a normalized one,
    prefer the normalized version. Migration of legacy mixed-case dirs is handled by
    a one-time merge_case_duplicates() call at startup.
    """
    import memory_compiler.config as _cfg
    norm = normalize_project(project)
    p = KNOWLEDGE_DIR / norm
    p.mkdir(parents=True, exist_ok=True)
    if norm not in _cfg.PROJECTS:
        _cfg.PROJECTS = _discover_projects()
    return p


def merge_case_duplicates() -> list[dict]:
    """One-time migration: merge case-variant project dirs into normalized lowercase form.

    Example: MyProj/ → myproj/, files moved, source dir removed.
    Returns list of merges performed: [{from, to, files_moved}].
    Safe to call multiple times — does nothing if no duplicates exist.
    """
    import shutil
    if not KNOWLEDGE_DIR.exists():
        return []
    merges = []
    # Group dirs by lowercase name
    seen: dict[str, list[Path]] = {}
    for p in KNOWLEDGE_DIR.iterdir():
        if not p.is_dir() or p.name.startswith(".") or p.name == "daily":
            continue
        seen.setdefault(p.name.lower(), []).append(p)

    for norm_name, paths in seen.items():
        # Single dir with non-canonical name (e.g. UPPERPROJ, no upperproj) — rename
        if len(paths) == 1:
            single = paths[0]
            if single.name == norm_name:
                continue  # already canonical
            target = KNOWLEDGE_DIR / norm_name
            try:
                single.rename(target)
                merges.append({"from": single.name, "to": norm_name, "files_moved": -1})
            except OSError:
                pass
            continue

        # Multiple case variants — merge non-canonical into canonical (lowercase)
        canonical = next((p for p in paths if p.name == norm_name), None)
        if canonical is None:
            # No existing lowercase — pick first variant and rename it
            canonical = KNOWLEDGE_DIR / norm_name
            paths[0].rename(canonical)
            merges.append({"from": paths[0].name, "to": norm_name, "files_moved": -1})
            paths = paths[1:]
        for src in paths:
            if src == canonical or not src.exists():
                continue
            moved = 0
            for f in src.iterdir():
                dst = canonical / f.name
                if dst.exists():
                    # Conflict: keep newer file
                    if f.stat().st_mtime > dst.stat().st_mtime:
                        dst.unlink()
                        shutil.move(str(f), str(dst))
                    else:
                        f.unlink()
                else:
                    shutil.move(str(f), str(dst))
                moved += 1
            try:
                src.rmdir()
            except OSError:
                pass
            merges.append({"from": src.name, "to": canonical.name, "files_moved": moved})
    return merges


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

# Порядок важен: сначала извлекаем структурные факты (URL, IP, порты),
# потом из остатка — версии. Это предотвращает ложные срабатывания
# когда regex версии ловит кусок IP (192.168.1.20 → "168.1.20")
# или порт-подобное ":80" внутри URL.
_FACT_PATTERNS_PRIMARY = [
    (r'(https?://[^\s\)]+)', "URL"),
    # IP — только одиночные адреса. CIDR (1.2.3.0/24) НЕ считается IP-фактом:
    # это описание подсети, а не хоста, и сравнивать его с host-IP бессмысленно.
    (r'\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?!/\d)\b', "IP"),
]
_FACT_PATTERNS_SECONDARY = [
    # Версия: должна быть префикс v/V или слово "верси"/"version" рядом,
    # чтобы не ловить случайные X.Y.Z (даты, числа).
    (r'(?:(?<=[vV])|(?<=верси[ияю] )|(?<=version )|(?<=release )|(?<=v\.))(\d+\.\d+\.\d+)\b', "версия"),
    (r'(?<!:)(?<!\d)\b(?:port|порт)\s*[:=]?\s*(\d{2,5})\b', "порт"),
]


def _extract_facts(text: str) -> dict[str, set[str]]:
    """Извлечь факты из текста, избегая пересечений между паттернами."""
    facts: dict[str, set[str]] = {}
    remaining = text
    # 1. Primary: URL, IP — удаляем найденное из текста
    for pattern, label in _FACT_PATTERNS_PRIMARY:
        found = set(re.findall(pattern, remaining))
        if found:
            if label == "URL":
                # regex [^\s\)]+ тянет URL до пробела/скобки, поэтому из markdown/JSON
                # («"url",», «url.», «<url>») прилипает хвост. Обрезаем хвостовые
                # кавычки/запятые/точки — иначе один и тот же адрес сравнивается как
                # разные строки (FP детектора) и попадает «грязным» в факты/tracking.
                found = {u.rstrip('"\'.,;:>]}') for u in found}
            facts[label] = found
            remaining = re.sub(pattern, " ", remaining)
    # 2. Secondary: версии, порты — ищем в остатке
    for pattern, label in _FACT_PATTERNS_SECONDARY:
        found = set(re.findall(pattern, remaining, re.IGNORECASE))
        if found:
            facts[label] = found
    return facts


def _ip_subnet(ip: str) -> str:
    """Вернуть /24 подсеть IP: 192.168.25.55 → 192.168.25"""
    parts = ip.split(".")
    if len(parts) == 4:
        return ".".join(parts[:3])
    return ip


# Публичные DNS-резолверы и аналогичные well-known сервисы. Их IP встречаются
# в десятках статей в разных ролях, попарное сравнение даёт чистый шум.
_WELL_KNOWN_IPS = frozenset({
    "8.8.8.8", "8.8.4.4",                  # Google
    "1.1.1.1", "1.0.0.1",                  # Cloudflare
    "9.9.9.9", "149.112.112.112",          # Quad9
    "208.67.222.222", "208.67.220.220",    # OpenDNS
    "77.88.8.8", "77.88.8.1",              # Yandex
    "94.140.14.14", "94.140.15.15",        # AdGuard
})


def _ip_role(ip: str) -> str:
    """Классификация IP по сетевой роли.

    Возвращает: 'wellknown' | 'private' | 'public' | 'special' | 'invalid'.
    Используется детектором противоречий: IP в РАЗНЫХ ролях не могут
    конфликтовать (LAN-адрес и WAN-адрес — это разные сущности по природе).
    """
    if ip in _WELL_KNOWN_IPS:
        return "wellknown"
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return "invalid"
    if addr.is_loopback or addr.is_unspecified or addr.is_multicast or addr.is_link_local:
        return "special"
    if addr.is_private:
        return "private"
    return "public"


# Известные сущности — слова рядом с IP/URL указывают на конкретный сервер/сервис.
# Если в новой и старой статьях упоминается ОДНА И ТА ЖЕ сущность, и факты разные —
# это реальное противоречие. Если сущности разные — это разные сервера, не конфликт.
# Только generic infra-термины — никаких имён конкретных приложений/клиентов.
_ENTITY_KEYWORDS = [
    'nas', 'synology', 'nginx', 'mikrotik', 'routeros', 'router',
    'postgres', 'mysql', 'redis', 'docker', 'kubernetes', 'k8s',
    'hypervisor', 'esxi', 'proxmox',
    'wireguard', 'vpn', 'ssh', 'ftp', 'sftp',
    'haproxy', 'traefik', 'apache',
    'prod', 'dev', 'stage', 'production', 'staging',
    'cfstorage', 'хранилище',
]


def _entities_in_text(text: str) -> set[str]:
    """Извлечь известные сущности из текста (lowercase)."""
    text_lower = text.lower()
    found = set()
    for kw in _ENTITY_KEYWORDS:
        if kw in text_lower:
            found.add(kw)
    return found


def detect_contradictions(new_content: str, project: str, exclude_path: Optional[str] = None) -> list[str]:
    """Найти возможные противоречия с существующими статьями.

    Логика умного детектора:
    1. IP в разных /24 подсетях — НЕ конфликт (разные сегменты сети = разные сервера)
    2. Если в обеих статьях упоминаются разные сущности (NAS vs nginx) — НЕ конфликт
    3. Конфликт только если: одна сущность ИЛИ одна подсеть И значения разные
    4. Версии НЕ сравниваются (монотонны во времени = эволюция, не конфликт;
       текущую версию ведёт tracking). Порты сравниваются по точному совпадению.
    """
    warnings = []
    new_facts = _extract_facts(new_content)

    if not new_facts:
        return []

    new_entities = _entities_in_text(new_content)

    # Проверить против существующих статей проекта
    proj_path = project_dir(project)
    for md in proj_path.glob("*.md"):
        if md.name.startswith("_"):
            continue
        rel_path = f"{project}/{md.name}"
        if exclude_path and rel_path == exclude_path:
            continue
        text = md.read_text(encoding="utf-8")
        existing_facts = _extract_facts(text)
        existing_entities = _entities_in_text(text)

        for label in new_facts:
            # Версии монотонно растут во времени: одна и та же сущность за месяцы
            # проходит 1.1.0 → 1.7.8, и старые статьи легитимно содержат старые
            # версии. Сравнение версий между статьями давало чистый шум (FP даже
            # при общей сущности). Текущую версию ведёт tracking
            # (save_tracking/get_current), а не детектор противоречий.
            if label == "версия":
                continue
            existing = existing_facts.get(label, set())
            new_vals = new_facts.get(label, set())
            if not new_vals or not existing:
                continue

            diff = new_vals - existing
            if not diff:
                continue

            for d in list(diff)[:2]:
                for e in list(existing)[:2]:
                    if d == e:
                        continue

                    # Умная фильтрация: для IP смотрим на роль, подсеть и сущность
                    if label == "IP":
                        role_d, role_e = _ip_role(d), _ip_role(e)

                        # Случай 0a: well-known публичные сервисы (8.8.8.8 и т.п.) —
                        # фигурируют в десятках статей в разных контекстах, шум.
                        if "wellknown" in (role_d, role_e):
                            continue
                        # Случай 0b: разные «роли» IP (private vs public) — это
                        # заведомо разные сущности (LAN-адрес не конфликтует с WAN).
                        if {role_d, role_e} == {"private", "public"}:
                            continue
                        # Случай 0c: special-адреса (loopback/link-local/multicast)
                        # тоже не сравниваем с обычными адресами.
                        if "special" in (role_d, role_e) and role_d != role_e:
                            continue

                        same_subnet = _ip_subnet(d) == _ip_subnet(e)
                        shared_entities = new_entities & existing_entities

                        # Случай 1: разные подсети + нет общих сущностей → разные сервера
                        if not same_subnet and not shared_entities:
                            continue
                        # Случай 2: одна подсеть + явно разные сущности → разные сервера в одной сети
                        if same_subnet and new_entities and existing_entities and not shared_entities:
                            continue
                        # Иначе: одна подсеть ИЛИ общая сущность — возможный реальный конфликт
                        # (миграция в другую сеть с тем же именем — ВАЖНОЕ предупреждение)

                    warnings.append(f"В {md.name} {label}: {e}, а в новой записи: {d}")
                    break

    return warnings[:5]  # макс 5 предупреждений


# ─── Auto-tagging ────────────────────────────────────────────────────────────

_AUTO_TAG_RULES = [
    # Containerization
    (r'\b(?:docker|dockerfile|docker-compose|контейнер|kubernetes|k8s|helm|podman)\b', 'docker'),
    # Web servers / SSL
    (r'\b(?:nginx|reverse.proxy|ssl|https|tls|certbot|letsencrypt|cert)\b', 'nginx'),
    # 1C
    (r'\b(?:1[cсС]|1С|bsl|epf|erf|обработк[аи]|конфигурац|расширен)\b', '1c'),
    # Databases
    (r'\b(?:postgres|postgresql|pgdump|pg_dump|миграци[яи]|alembic|psql|pgadmin)\b', 'postgres'),
    (r'\b(?:mysql|mariadb|mysqldump)\b', 'mysql'),
    (r'\b(?:mssql|sqlserver|sql.server|t-sql)\b', 'mssql'),
    (r'\b(?:mongodb|mongo|nosql)\b', 'mongodb'),
    # Network
    (r'\b(?:ssh|paramiko|scp|sftp|sshfs|openssh)\b', 'ssh'),
    (r'\b(?:vpn|wireguard|openvpn|ipsec|l2tp|туннел[ьие])\b', 'vpn'),
    (r'\b(?:dns|hosts|named|bind|cloudflare|регистратор)\b', 'dns'),
    # Frontend
    (r'\b(?:react|typescript|tsx|vite|shadcn|tailwind|next\.?js|vue|svelte)\b', 'frontend'),
    # Backend
    (r'\b(?:fastapi|uvicorn|pydantic|sqlalchemy|django|flask|express|asyncio)\b', 'backend'),
    # Caching / queue
    (r'\b(?:redis|celery|celery.beat|rabbitmq|kafka|memcached)\b', 'redis'),
    # Network equipment
    (r'\b(?:mikrotik|routeros|cisco|firewall|фаервол|маршрут|роутер|router)\b', 'mikrotik'),
    # Storage
    (r'\b(?:nas|synology|dsm|truenas|raid|zfs|btrfs)\b', 'nas'),
    # Git
    (r'\b(?:git|commit|merge|branch|rebase|pull.request|github|gitlab)\b', 'git'),
    # AI / MCP
    (r'\b(?:mcp|claude|anthropic|openai|gpt|llm)\b', 'mcp'),
    # Deployment
    (r'\b(?:деплой|deploy|deploy[a-z]*|прод|production|stage|staging|релиз|release)\b', 'deploy'),
    # Bugfix
    (r'\b(?:bug|баг|fix|исправлен|ошибк[аи]|exception|traceback|crash|stacktrace)\b', 'bugfix'),
    # Performance
    (r'\b(?:performance|производительн|оптимизац|медленн|slow|latency|задержк|profiler)\b', 'performance'),
    # Security
    (r'\b(?:security|безопасн|уязвим|vulnerability|cve|exploit|injection|xss|csrf|auth|авториз|пароль|secret)\b', 'security'),
    # Testing
    (r'\b(?:test|тест|pytest|jest|mocha|unittest|integration.test|e2e|coverage)\b', 'testing'),
    # API / integration
    (r'\b(?:api|rest|graphql|endpoint|webhook|интеграц|integration|swagger|openapi)\b', 'api'),
    # Monitoring / logging
    (r'\b(?:monitoring|мониторинг|grafana|prometheus|zabbix|kibana|elastic|loki|sentry|логирован|logging)\b', 'monitoring'),
    # Backup
    (r'\b(?:backup|бэкап|бекап|восстановлен|восстанов|снапшот|snapshot|rsync|borg)\b', 'backup'),
    # Refactoring
    (r'\b(?:refactor|рефакторинг|cleanup|очист|архитектур|architecture)\b', 'refactor'),
    # Documentation
    (r'\b(?:docs?|документац|readme|changelog|wiki|инструкц)\b', 'docs'),
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


# Meta-статьи (session/health-check/tracking и service-файлы на «_») семантически
# близки почти ко всему и при e5-эмбеддингах засевали базу нерелевантными «См.
# также». Их не кросс-реферим — ни как источник, ни как цель.
_META_REF_SUBSTR = ("health-check", "session", "сессия", "tracking_")


def _is_meta_article(filename: str) -> bool:
    n = filename.lower()
    return n.startswith("_") or any(s in n for s in _META_REF_SUBSTR)


def update_cross_references(topic: str, project: str, saved_path: str,
                           max_refs: int = 5, min_sim: float = 0.65, max_sim: float = 0.85):
    """Добавить ссылки «См. также» в семантически близкие статьи ТОГО ЖЕ проекта.

    Защита от загрязнения (v1.7.13): скоуп по проекту (C), потолок top-N (B),
    пропуск meta-статей (D). Раньше функция при e5-эмбеддингах (косинус сжат
    вверх, порог 0.55 калибровался под старую MiniLM) дописывала сотни
    нерелевантных кросс-ссылок через всю базу, в т.ч. в чужие проекты.
    """
    from memory_compiler.search import _embeddings, encode_query
    import numpy as np

    if not _embeddings:
        return
    # D: не кросс-реферим ОТ meta-статьи.
    if _is_meta_article(saved_path.split("/")[-1]):
        return

    q_vec = encode_query(topic)

    # Кандидаты: тот же проект, не meta, similarity в окне. Затем — top-N.
    cands = []
    for key, vec in _embeddings.items():
        if key == saved_path or "#chunk" in key:
            continue
        if key.split("/", 1)[0] != project:        # C: только тот же проект
            continue
        if _is_meta_article(key.split("/")[-1]):    # D: не линкуем В meta
            continue
        sim = float(np.dot(q_vec, vec))
        if sim < min_sim or sim > max_sim:
            continue  # слишком далеко или слишком близко (дубль)
        cands.append((sim, key))

    cands.sort(reverse=True)
    now = datetime.now().strftime("%Y-%m-%d")
    for _sim, key in cands[:max_refs]:              # B: потолок top-N
        fpath = KNOWLEDGE_DIR / key
        if not fpath.exists():
            continue
        text = fpath.read_text(encoding="utf-8")
        ref_line = f"- [{topic}](../{saved_path}) ({now})"
        if "## См. также" in text:
            if saved_path in text:                  # не дублировать
                continue
            text = text.rstrip() + f"\n{ref_line}\n"
        else:
            text = text.rstrip() + f"\n\n## См. также\n{ref_line}\n"
        fpath.write_text(text, encoding="utf-8")


# ─── Snippet extraction ─────────────────────────────────────────────────────


def extract_snippets(text: str) -> list[dict]:
    """Extract code blocks from markdown text.
    Returns list of {lang: str, code: str, context: str}."""
    snippets = []
    lines = text.splitlines()
    i = 0
    current_context = ""
    while i < len(lines):
        line = lines[i]
        if line.startswith("### ") or line.startswith("## "):
            current_context = line.lstrip("#").strip()
        if line.startswith("```"):
            lang = line[3:].strip().lower() or "text"
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code_lines.append(lines[i])
                i += 1
            if code_lines:
                snippets.append({
                    "lang": lang,
                    "code": "\n".join(code_lines),
                    "context": current_context,
                })
        i += 1
    return snippets


# ─── Error extraction ───────────────────────────────────────────────────────

_ERROR_PATTERNS = [
    (r'(Traceback \(most recent call last\)[\s\S]*?(?:\w+Error|\w+Exception):.+)', "python_traceback"),
    (r'\b((?:HTTP|http)\s*(?:error\s*)?(\d{3}))\b', "http_code"),
    (r'\b((?:Error|Exception|Ошибка|ОШИБКА)\s*[:]\s*.{10,100})', "error_message"),
    (r'\b(errno\s*[:=]\s*\d+)', "errno"),
    (r'\b(SQLSTATE\s*\[\w+\])', "sql_error"),
    (r'(\{[\w.]+\(\d+\)\})', "1c_error"),
]


def extract_errors(text: str) -> list[dict]:
    """Extract error patterns from text.
    Returns list of {type: str, text: str}."""
    errors = []
    for pattern, err_type in _ERROR_PATTERNS:
        for match in re.findall(pattern, text):
            err_text = match if isinstance(match, str) else match[0]
            errors.append({"type": err_type, "text": err_text.strip()[:200]})
    return errors


# ─── Article templates ──────────────────────────────────────────────────────

TEMPLATES = {
    "bug": {
        "description": "Баг-репорт с симптомом, причиной и решением",
        "fields": ["symptom", "cause", "fix"],
        "format": "## Симптом\n{symptom}\n\n## Причина\n{cause}\n\n## Решение\n{fix}",
    },
    "setup": {
        "description": "Настройка сервера/сервиса",
        "fields": ["goal", "steps", "verification"],
        "format": "## Цель\n{goal}\n\n## Шаги\n{steps}\n\n## Проверка\n{verification}",
    },
    "1c": {
        "description": "Доработка 1С (обработка, отчёт, конфигурация)",
        "fields": ["task", "solution", "objects"],
        "format": "## Задача\n{task}\n\n## Решение\n{solution}\n\n## Объекты\n{objects}",
    },
    "deploy": {
        "description": "Деплой/обновление",
        "fields": ["target", "steps", "rollback"],
        "format": "## Цель\n{target}\n\n## Шаги деплоя\n{steps}\n\n## Откат\n{rollback}",
    },
    "integration": {
        "description": "Интеграция между системами",
        "fields": ["systems", "protocol", "implementation"],
        "format": "## Системы\n{systems}\n\n## Протокол\n{protocol}\n\n## Реализация\n{implementation}",
    },
}


# ─── Project dependencies ───────────────────────────────────────────────────


def get_project_deps_file(project: str) -> Path:
    """Get path to project dependencies file."""
    return project_dir(project) / "_deps.json"


def read_project_deps(project: str) -> list[str]:
    """Read project dependencies."""
    deps_file = get_project_deps_file(project)
    if deps_file.exists():
        try:
            data = json.loads(deps_file.read_text(encoding="utf-8"))
            return data.get("depends_on", [])
        except Exception:
            pass
    return []


def write_project_deps(project: str, depends_on: list[str]):
    """Write project dependencies."""
    deps_file = get_project_deps_file(project)
    deps_file.write_text(
        json.dumps({"depends_on": depends_on}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# --- Encryption ---

def _get_cipher():
    """Get Fernet cipher from MC_ENCRYPT_KEY env var."""
    from memory_compiler.config import MC_ENCRYPT_KEY
    if not MC_ENCRYPT_KEY:
        return None
    try:
        from cryptography.fernet import Fernet
        dk = hashlib.pbkdf2_hmac("sha256", MC_ENCRYPT_KEY.encode(), b"memory-compiler-salt", 100000)
        return Fernet(base64.urlsafe_b64encode(dk))
    except ImportError:
        return None


def encrypt_content(text: str) -> str:
    """Encrypt text content. Returns 'ENC:...' string or original if no key."""
    cipher = _get_cipher()
    if not cipher:
        return text
    return "ENC:" + cipher.encrypt(text.encode()).decode()


def decrypt_content(text: str) -> str:
    """Decrypt 'ENC:...' content. Returns original text if not encrypted."""
    if not text.startswith("ENC:"):
        return text
    cipher = _get_cipher()
    if not cipher:
        return "[MC_ENCRYPT_KEY не задан — расшифровка невозможна]"
    try:
        return cipher.decrypt(text[4:].encode()).decode()
    except Exception:
        return "[Ошибка расшифровки]"


def is_encrypted(text: str) -> bool:
    """Check if content is encrypted."""
    return text.strip().startswith("ENC:")


# --- Audit log ---

def _audit_path():
    return KNOWLEDGE_DIR / "_audit.log"


def audit_log(tool_name: str, args: dict, result_size: int):
    """Log tool call to audit file."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    safe_args = {}
    for k, v in args.items():
        if k in ("content", "error_text", "steps"):
            safe_args[k] = f"[{len(str(v))} chars]"
        elif k in ("key", "password"):
            safe_args[k] = "***"
        else:
            safe_args[k] = v
    line = json.dumps({"ts": ts, "tool": tool_name, "args": safe_args, "size": result_size}, ensure_ascii=False)
    try:
        with open(_audit_path(), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ─── Reflective Memory (RMM-lite: prospective reflection on finish_task) ──────
#
# Rule-based atomic-fact extraction from session content. Inspired by Reflective
# Memory Management (arXiv 2503.08026): break a session into reusable units so
# future retrieval can hit specific facts rather than buried prose paragraphs.
# No external LLM — pattern matching on bullets, numbered lists, and Russian/English
# action verbs.

_REFLECTION_ACTION_VERBS = re.compile(
    r'\b(?:настроил|настроили|исправил|исправили|добавил|добавили|обновил|обновили|'
    r'реализовал|реализовали|решил|решили|подключил|подключили|удалил|удалили|'
    r'configured|fixed|added|updated|implemented|resolved|connected|removed|'
    r'deployed|зад\w*плои\w*|сд\w*елал\w*)\b',
    re.IGNORECASE,
)


# Negation markers that disqualify a sentence from being recorded as a fact.
_NEGATION_RE = re.compile(
    r'(?:^|\s)(?:не|never|not|n\'t|didn\'t|did not|hasn\'t|has not|haven\'t)\s',
    re.IGNORECASE,
)


def extract_reflections(content: str) -> list[str]:
    """Extract atomic facts from session content via rules.

    Sources:
      1. Top-level bullet items: '- X' or '* X'
      2. Numbered list items: '1. X'
      3. Sentences containing action verbs (настроил/fixed/added/...) — full sentence
    Sentences with negation markers ('не', 'not', "n't"…) are NOT extracted
    because they describe something that didn't happen.
    Returns deduplicated list of fact strings (trimmed).
    """
    if not content or not content.strip():
        return []

    facts: list[str] = []

    # 1+2. Bullets and numbered lists
    for line in content.splitlines():
        stripped = line.strip()
        # Bullets: - foo, * foo
        m_bullet = re.match(r'^[-*]\s+(.+)$', stripped)
        if m_bullet:
            fact = m_bullet.group(1).strip()
            if len(fact) >= 6 and not _NEGATION_RE.search(" " + fact):
                facts.append(fact)
            continue
        # Numbered: 1. foo, 2) foo
        m_num = re.match(r'^\d+[.)]\s+(.+)$', stripped)
        if m_num:
            fact = m_num.group(1).strip()
            if len(fact) >= 6 and not _NEGATION_RE.search(" " + fact):
                facts.append(fact)

    # 3. Sentences with action verbs (split content into sentences first)
    sentences = re.split(r'(?<=[.!?])\s+', content)
    for sent in sentences:
        sent = sent.strip()
        if not sent or len(sent) < 12:
            continue
        # Skip if already added as a bullet
        if any(sent in f or f in sent for f in facts):
            continue
        if _REFLECTION_ACTION_VERBS.search(sent):
            # Skip negated sentences ("не настроил", "did not configure" etc.)
            if _NEGATION_RE.search(" " + sent):
                continue
            # Cap at 200 chars
            facts.append(sent[:200].rstrip(".!? "))

    # Dedup preserving order
    seen = set()
    deduped = []
    for f in facts:
        key = f.lower().strip()
        if key not in seen:
            seen.add(key)
            deduped.append(f)
    return deduped


def append_reflections(project: str, facts: list[str], cap: int = 20) -> None:
    """Append facts to <project>/_reflections.md, capping at `cap` entries (FIFO).
    No-op if facts is empty.
    """
    if not facts:
        return
    proj = project_dir(project)
    refl_path = proj / "_reflections.md"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    new_lines = [f"- [{ts}] {f}" for f in facts]

    existing: list[str] = []
    if refl_path.exists():
        text = refl_path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("- ["):
                existing.append(line)

    # Newest first, FIFO cap
    combined = new_lines[::-1] + existing  # newest entries on top
    combined = combined[:cap]

    body = (
        f"# Reflections: {normalize_project(project)}\n\n"
        f"Atomic facts extracted from sessions (FIFO {cap}, newest first).\n\n"
        + "\n".join(combined)
        + "\n"
    )
    # Atomic write: write to .tmp then rename (avoids torn writes on crash / concurrent edit)
    tmp_path = refl_path.with_suffix(refl_path.suffix + ".tmp")
    tmp_path.write_text(body, encoding="utf-8")
    tmp_path.replace(refl_path)


def mark_dependents(project: str, filename: str, timestamp: str) -> int:
    """Cascade-mark: when filename is edited, refresh a 🔄 marker on every line
    that links to it from any other article (same or different project).
    Idempotent — re-running replaces the existing marker timestamp.

    Cross-project links recognised:
      - same project: [text](./file.md) or [text](file.md)
      - cross project: [text](../<project>/file.md)

    Returns the number of dependent articles touched.
    """
    target_proj_dir = project_dir(project)
    if not target_proj_dir.exists():
        return 0

    fname_escaped = re.escape(filename)
    same_proj_pattern = re.compile(r'\[([^\]]+)\]\((?:\./)?' + fname_escaped + r'\)')
    cross_proj_pattern = re.compile(
        r'\[([^\]]+)\]\(\.\./' + re.escape(project) + r'/' + fname_escaped + r'\)'
    )
    marker_pattern = re.compile(r'\s*🔄 обновлено: \d{4}-\d{2}-\d{2} \d{2}:\d{2}')
    new_marker = f" 🔄 обновлено: {timestamp}"

    marked = 0
    # Iterate over ALL projects to catch cross-project deps too
    if KNOWLEDGE_DIR.exists():
        for proj_path in KNOWLEDGE_DIR.iterdir():
            if not proj_path.is_dir() or proj_path.name.startswith("."):
                continue
            is_same_project = (proj_path == target_proj_dir)
            for a in proj_path.glob("*.md"):
                # Skip the file itself + service files
                if is_same_project and a.name == filename:
                    continue
                if a.name.startswith("_"):
                    continue
                try:
                    text = a.read_text(encoding="utf-8")
                except Exception:
                    continue
                # Pick the right pattern based on whether the link is intra- or cross-project
                pattern = same_proj_pattern if is_same_project else cross_proj_pattern
                if not pattern.search(text):
                    continue
                new_lines = []
                modified = False
                for line in text.splitlines():
                    if pattern.search(line):
                        stripped = marker_pattern.sub("", line)
                        new_lines.append(stripped + new_marker)
                        modified = True
                    else:
                        new_lines.append(line)
                if modified:
                    tail = "\n" if text.endswith("\n") else ""
                    a.write_text("\n".join(new_lines) + tail, encoding="utf-8")
                    marked += 1
    return marked


# When _log.md exceeds this many bytes, rotate it to _log.archive.md and start fresh.
# Overridable in tests / via env. ~256KB ≈ several thousand lines.
LOG_ROTATE_BYTES = 256 * 1024


def log_event(project: str, action: str, details: str = "") -> None:
    """Append a per-project event line to <project>/_log.md (Karpathy LLM Wiki pattern).

    One human-readable journal per project with ingest / save_* / lint / consolidate
    events. Distinct from _audit.log (global, JSON, every tool call) — _log.md is
    intentionally selective and readable, recording only knowledge-shaping operations.

    Rotates to _log.archive.md when size exceeds LOG_ROTATE_BYTES to keep the active
    log readable.
    """
    proj_dir = project_dir(project)
    log_path = proj_dir / "_log.md"
    archive_path = proj_dir / "_log.archive.md"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    line = f"- [{ts}] **{action}** — {details}".rstrip(" —") + "\n"
    try:
        # Rotation: if current log too big, move to archive (append-only) then start fresh
        if log_path.exists() and log_path.stat().st_size >= LOG_ROTATE_BYTES:
            try:
                old_text = log_path.read_text(encoding="utf-8")
                with open(archive_path, "a", encoding="utf-8") as af:
                    af.write(old_text)
                log_path.unlink()
            except Exception:
                pass
        is_new = not log_path.exists()
        with open(log_path, "a", encoding="utf-8") as f:
            if is_new:
                f.write(f"# Project journal: {normalize_project(project)}\n\n"
                        "Append-only log of knowledge-shaping events "
                        "(ingest, save_*, lint, consolidate, delete). "
                        f"Rotated to _log.archive.md when size exceeds {LOG_ROTATE_BYTES} bytes.\n\n")
            f.write(line)
    except Exception:
        pass  # never break a write path because of journaling


def read_audit_log(limit: int = 100) -> list[dict]:
    """Read last N audit entries."""
    path = _audit_path()
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    entries = []
    for line in lines[-limit:]:
        try:
            entries.append(json.loads(line))
        except Exception:
            pass
    return entries


# ─── Git capture helpers ────────────────────────────────────────────────────


_COMMIT_SEP = "---GIT_CAPTURE_SEP---"
_COMMIT_FORMAT = f"%H|%s|%an|%aI{_COMMIT_SEP}"


def parse_git_log(repo_path: str, since: str = None) -> list[dict]:
    """Parse git log from external repo into structured commits list."""
    cmd = ["git", "log", f"--format={_COMMIT_FORMAT}", "--numstat"]
    if since:
        # Detect if since is a commit hash (hex, 7-40 chars)
        if re.match(r'^[0-9a-f]{7,40}$', since):
            cmd.append(f"{since}..HEAD")
        else:
            cmd.extend(["--since", since])

    result = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True)
    if result.returncode != 0:
        return []

    commits = []
    # numstat lines appear AFTER the separator, at the start of the next block.
    # Block structure: [files_of_prev_commit]\n<header>
    # So we collect (header, files) by pairing: header from block N, files from block N+1.
    raw_blocks = result.stdout.split(_COMMIT_SEP)

    headers = []  # list of (hash, message, author, date)
    file_sections = []  # list of [file_dicts]

    for block in raw_blocks:
        lines = block.strip().splitlines()
        if not lines:
            # Empty block — no files for previous commit
            file_sections.append([])
            continue

        # Parse numstat lines (before the header line)
        files = []
        header_line = None
        for line in lines:
            line_stripped = line.strip()
            if not line_stripped:
                continue
            m = re.match(r'^(\d+|-)\t(\d+|-)\t(.+)$', line_stripped)
            if m:
                ins = int(m.group(1)) if m.group(1) != '-' else 0
                dels = int(m.group(2)) if m.group(2) != '-' else 0
                files.append({"path": m.group(3), "insertions": ins, "deletions": dels})
            elif "|" in line_stripped:
                header_line = line_stripped

        file_sections.append(files)

        if header_line:
            parts = header_line.split("|", 3)
            if len(parts) == 4:
                headers.append(tuple(p.strip() for p in parts))

    # Pair: header[i] gets files from file_sections[i+1]
    for i, (commit_hash, message, author, date_str) in enumerate(headers):
        files = file_sections[i + 1] if i + 1 < len(file_sections) else []
        commits.append({
            "hash": commit_hash,
            "message": message,
            "author": author,
            "date": date_str,
            "files": files,
        })

    return commits


def parse_git_log_raw(raw_text: str) -> list[dict]:
    """Parse raw git log output (--format='%H|%s|%an|%aI' --numstat or --stat).

    Accepts output from:
      git log --format="%H|%s|%an|%aI" --numstat
    """
    commits = []
    current = None

    for line in raw_text.splitlines():
        line_stripped = line.strip()
        if not line_stripped:
            continue

        # Try header: hash|message|author|date
        parts = line_stripped.split("|", 3)
        if len(parts) == 4 and re.match(r'^[0-9a-f]{7,40}$', parts[0].strip()):
            if current:
                commits.append(current)
            current = {
                "hash": parts[0].strip(),
                "message": parts[1].strip(),
                "author": parts[2].strip(),
                "date": parts[3].strip(),
                "files": [],
            }
            continue

        # Try numstat: ins\tdels\tpath
        if current:
            m = re.match(r'^(\d+|-)\t(\d+|-)\t(.+)$', line_stripped)
            if m:
                ins = int(m.group(1)) if m.group(1) != '-' else 0
                dels = int(m.group(2)) if m.group(2) != '-' else 0
                current["files"].append({"path": m.group(3), "insertions": ins, "deletions": dels})

    if current:
        commits.append(current)

    return commits


_PREFIX_RE = re.compile(r'^(fix|feat|refactor|docs|chore|build|test|style|perf|ci)[\(:\s]', re.IGNORECASE)


def group_commits(commits: list[dict], group_by: str = "prefix") -> dict:
    """Group commits by prefix (conventional commits), branch, or file area."""
    groups = {}

    for c in commits:
        if group_by == "prefix":
            m = _PREFIX_RE.match(c["message"])
            key = m.group(1).lower() if m else "other"
        elif group_by == "file":
            # Group by top-level directory of most-changed file
            if c["files"]:
                top_file = max(c["files"], key=lambda f: f["insertions"] + f["deletions"])
                parts = top_file["path"].split("/")
                key = parts[0] if len(parts) > 1 else "(root)"
            else:
                key = "(no files)"
        else:  # branch — fallback to prefix
            key = "other"
            m = _PREFIX_RE.match(c["message"])
            if m:
                key = m.group(1).lower()

        groups.setdefault(key, []).append(c)

    return groups


def format_capture_group(group_name: str, commits: list[dict]) -> str:
    """Format a group of commits into markdown content."""
    dates = [c["date"][:10] for c in commits]
    first_date, last_date = min(dates), max(dates)

    # Collect top changed files across all commits
    file_stats = {}
    for c in commits:
        for f in c["files"]:
            p = f["path"]
            if p not in file_stats:
                file_stats[p] = 0
            file_stats[p] += f["insertions"] + f["deletions"]
    top_files = sorted(file_stats, key=file_stats.get, reverse=True)[:5]

    total_ins = sum(f["insertions"] for c in commits for f in c["files"])
    total_dels = sum(f["deletions"] for c in commits for f in c["files"])

    lines = [
        f"**Коммитов:** {len(commits)}",
        f"**Период:** {first_date} — {last_date}" if first_date != last_date else f"**Дата:** {first_date}",
        f"**Изменения:** +{total_ins} / -{total_dels}",
        f"**Файлы:** {', '.join(top_files)}" if top_files else "",
        "",
        "### Коммиты",
    ]
    for c in commits:
        lines.append(f"- {c['message']} (`{c['hash'][:7]}`)")

    return "\n".join(line for line in lines if line is not None)


def read_last_capture(project: str, repo_path: str) -> Optional[str]:
    """Read last captured commit hash for a repo in this project."""
    cap_path = project_dir(project) / "_last_capture.json"
    if not cap_path.exists():
        return None
    try:
        data = json.loads(cap_path.read_text(encoding="utf-8"))
        return data.get(repo_path, {}).get("last_commit")
    except Exception:
        return None


def write_last_capture(project: str, repo_path: str, commit_hash: str):
    """Save last captured commit hash for a repo."""
    cap_path = project_dir(project) / "_last_capture.json"
    data = {}
    if cap_path.exists():
        try:
            data = json.loads(cap_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    data[repo_path] = {
        "last_commit": commit_hash,
        "last_capture": datetime.now().isoformat(),
    }
    cap_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ─── Ingest helpers ─────────────────────────────────────────────────────


def html_to_markdown(html: str) -> str:
    """Convert HTML to readable markdown using stdlib html.parser."""
    from html.parser import HTMLParser

    class _H2M(HTMLParser):
        def __init__(self):
            super().__init__()
            self.out = []
            self.tag_stack = []
            self.skip = False
            self.li_count = 0

        def handle_starttag(self, tag, attrs):
            self.tag_stack.append(tag)
            if tag in ("script", "style", "nav", "footer", "header", "aside", "noscript"):
                self.skip = True
            elif tag == "h1":
                self.out.append("\n# ")
            elif tag == "h2":
                self.out.append("\n## ")
            elif tag == "h3":
                self.out.append("\n### ")
            elif tag in ("h4", "h5", "h6"):
                self.out.append("\n#### ")
            elif tag == "p":
                self.out.append("\n\n")
            elif tag == "br":
                self.out.append("\n")
            elif tag == "li":
                self.out.append("\n- ")
            elif tag == "strong" or tag == "b":
                self.out.append("**")
            elif tag == "em" or tag == "i":
                self.out.append("*")
            elif tag == "code":
                self.out.append("`")
            elif tag == "pre":
                self.out.append("\n```\n")
            elif tag == "a":
                href = dict(attrs).get("href", "")
                if href and not href.startswith("#") and not href.startswith("javascript"):
                    self.out.append("[")
            elif tag == "blockquote":
                self.out.append("\n> ")

        def handle_endtag(self, tag):
            if tag in self.tag_stack:
                self.tag_stack.remove(tag)
            if tag in ("script", "style", "nav", "footer", "header", "aside", "noscript"):
                self.skip = False
            elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                self.out.append("\n")
            elif tag == "strong" or tag == "b":
                self.out.append("**")
            elif tag == "em" or tag == "i":
                self.out.append("*")
            elif tag == "code":
                self.out.append("`")
            elif tag == "pre":
                self.out.append("\n```\n")

        def handle_data(self, data):
            if self.skip:
                return
            text = data.strip()
            if text:
                self.out.append(text if self.tag_stack and self.tag_stack[-1] == "pre" else " " + text)

    parser = _H2M()
    parser.feed(html)
    text = "".join(parser.out)
    # Clean up
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'  +', ' ', text)
    # Remove stray spaces before inline markers
    text = re.sub(r'\s+(\*\*|\*|`)', r'\1', text)
    text = re.sub(r'(\*\*|\*|`)\s+', r'\1 ', text)
    return text.strip()


def parse_obsidian_note(text: str) -> dict:
    """Parse an Obsidian note: YAML frontmatter, tags, wiki-links, body."""
    result = {"frontmatter": {}, "tags": [], "wiki_links": [], "body": text, "title": None}

    # Parse YAML frontmatter
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end > 0:
            fm_text = text[4:end]
            body = text[end + 5:]
            # Simple YAML parser (no external deps)
            current_key = None
            for line in fm_text.splitlines():
                if not line.strip():
                    continue
                # List continuation
                if line.startswith("  - ") and current_key:
                    if not isinstance(result["frontmatter"].get(current_key), list):
                        result["frontmatter"][current_key] = []
                    result["frontmatter"][current_key].append(line[4:].strip())
                elif ":" in line:
                    key, _, val = line.partition(":")
                    key = key.strip()
                    val = val.strip()
                    current_key = key
                    if val:
                        result["frontmatter"][key] = val
                    else:
                        result["frontmatter"][key] = None  # list to follow
            result["body"] = body
            # Extract tags from frontmatter
            fm_tags = result["frontmatter"].get("tags")
            if isinstance(fm_tags, list):
                result["tags"] = [t.strip().lstrip("#") for t in fm_tags]
            elif isinstance(fm_tags, str):
                result["tags"] = [t.strip().lstrip("#") for t in re.split(r'[,\s]+', fm_tags) if t.strip()]
            # Title
            if "title" in result["frontmatter"]:
                result["title"] = str(result["frontmatter"]["title"])

    # Inline tags (#tag)
    inline_tags = re.findall(r'(?<![\w/])#([а-яА-ЯёЁ\w-]+)', result["body"])
    result["tags"].extend(inline_tags)
    result["tags"] = list(dict.fromkeys(result["tags"]))  # dedup preserve order

    # Wiki links [[Target]] or [[Target|Alias]]
    for m in re.finditer(r'\[\[([^\]|]+)(?:\|([^\]]+))?\]\]', result["body"]):
        target = m.group(1).strip()
        result["wiki_links"].append(target)

    # Convert wiki links to plain text (since we don't have real cross-references yet)
    # [[Target]] → **Target**, [[Target|Alias]] → **Alias**
    def _repl(m):
        target = m.group(1).strip()
        alias = (m.group(2) or target).strip()
        return f"**{alias}**"
    result["body"] = re.sub(r'\[\[([^\]|]+)(?:\|([^\]]+))?\]\]', _repl, result["body"])

    return result


def fetch_url(url: str, timeout: int = 15) -> tuple:
    """Fetch URL content. Returns (text, content_type, title)."""
    import urllib.request
    import urllib.error

    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; memory-compiler/1.0)",
        "Accept": "text/html,application/xhtml+xml,text/plain,*/*",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            data = resp.read(512 * 1024)  # 512KB limit
            charset = "utf-8"
            if "charset=" in content_type:
                charset = content_type.split("charset=")[-1].split(";")[0].strip()
            text = data.decode(charset, errors="replace")

            # Extract title
            title = url
            m = re.search(r'<title[^>]*>([^<]+)</title>', text, re.IGNORECASE)
            if m:
                title = m.group(1).strip()

            # Convert HTML to markdown
            if "html" in content_type.lower():
                text = html_to_markdown(text)

            return text, content_type, title
    except urllib.error.HTTPError as e:
        raise ValueError(f"HTTP {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        raise ValueError(f"URL error: {e.reason}")
    except Exception as e:
        raise ValueError(f"Fetch error: {e}")


# ─── Tracking articles (bi-temporal frontmatter) ───────────────────────────
#
# Tracking statья — снимок текущего состояния сущности (версия, IP, и т.д.).
# Хранит structured facts в YAML frontmatter:
#   ---
#   type: tracking
#   project: myapp
#   entity: release
#   current:
#     version: "1.3.50"
#     since: "2026-04-15"
#   history:
#     - version: "1.3.47"
#       from: "2026-04-11"
#       to: "2026-04-15"
#       changes: "fix #258"
#   ---
# Body — человекочитаемое описание, автогенерируется при update.


try:
    import yaml as _yaml  # type: ignore
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter.

    Prefers PyYAML for correct parsing at any depth (nested lists inside nested dicts, etc).
    Falls back to custom parser if PyYAML unavailable — supports limited subset:
      - top-level scalars
      - nested dicts (1 level deep)
      - lists of dicts / lists of scalars
      - lists nested inside nested dicts (tracks last_nested_key)

    Returns (data, body).
    """
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    fm_text = text[4:end]
    body = text[end + 5:]

    # Primary path: PyYAML (correct at any depth)
    if _HAS_YAML:
        try:
            data = _yaml.safe_load(fm_text) or {}
            if not isinstance(data, dict):
                data = {}
            return data, body
        except Exception:
            # Malformed YAML — fall through to custom parser
            pass

    # Fallback custom parser
    data: dict = {}
    current_key: Optional[str] = None  # top-level key whose block we're filling
    block_type: Optional[str] = None  # "dict" | "list"
    current_list_item: Optional[dict] = None
    last_nested_dict_key: Optional[str] = None  # for lists inside nested dicts

    for raw_line in fm_text.splitlines():
        if not raw_line.strip():
            continue
        stripped = raw_line.lstrip()
        indent = len(raw_line) - len(stripped)

        if indent == 0:
            # Top-level: key: val OR key: (start block)
            if ":" in stripped:
                key, _, val = stripped.partition(":")
                key = key.strip()
                val = val.strip()
                if val:
                    data[key] = _parse_scalar(val)
                    current_key = None
                    block_type = None
                    last_nested_dict_key = None
                else:
                    current_key = key
                    block_type = None  # resolved on next line
                    data[key] = None
                    current_list_item = None
                    last_nested_dict_key = None
        elif current_key is not None:
            # Inside block for current_key
            if stripped.startswith("- "):
                content = stripped[2:].strip()
                # List nested inside a nested dict (indent > 2 and we're in dict mode)
                if indent > 2 and block_type == "dict" and last_nested_dict_key is not None:
                    nested = data[current_key]
                    if not isinstance(nested.get(last_nested_dict_key), list):
                        nested[last_nested_dict_key] = []
                    nested[last_nested_dict_key].append(_parse_scalar(content))
                    continue
                # Top-level list item under current_key
                if block_type != "list":
                    data[current_key] = []
                    block_type = "list"
                if ":" in content and not content.startswith('"'):
                    k, _, v = content.partition(":")
                    item = {k.strip(): _parse_scalar(v.strip())}
                    data[current_key].append(item)
                    current_list_item = item
                else:
                    data[current_key].append(_parse_scalar(content))
                    current_list_item = None
            elif ":" in stripped:
                k, _, v = stripped.partition(":")
                k = k.strip()
                v = v.strip()
                if block_type == "list" and current_list_item is not None:
                    # continuation of list item dict
                    current_list_item[k] = _parse_scalar(v)
                else:
                    # nested dict under current_key
                    if block_type != "dict":
                        data[current_key] = {}
                        block_type = "dict"
                    data[current_key][k] = _parse_scalar(v)
                    # Track key — it might start a nested list on next lines
                    if not v:
                        last_nested_dict_key = k

    return data, body


def _parse_scalar(s: str):
    """Parse YAML scalar: string, number, bool, null."""
    s = s.strip()
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1]
    if s.startswith("'") and s.endswith("'"):
        return s[1:-1]
    if s.lower() in ("null", "none", "~", ""):
        return None
    if s.lower() in ("true", "yes"):
        return True
    if s.lower() in ("false", "no"):
        return False
    # Don't try to parse numbers — keep as string for version consistency
    return s


def _fix_pending_dicts(obj):
    """Convert None values that should be dicts/lists after sub-parsing. Simple pass."""
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if isinstance(v, (dict, list)):
                _fix_pending_dicts(v)


def _write_frontmatter(data: dict) -> str:
    """Serialize dict to YAML-like frontmatter. Simple subset."""
    lines = ["---"]
    _write_yaml_dict(data, lines, 0)
    lines.append("---")
    return "\n".join(lines) + "\n"


def _write_yaml_dict(d: dict, lines: list, indent: int):
    pad = " " * indent
    for k, v in d.items():
        if isinstance(v, dict):
            lines.append(f"{pad}{k}:")
            _write_yaml_dict(v, lines, indent + 2)
        elif isinstance(v, list):
            lines.append(f"{pad}{k}:")
            for item in v:
                if isinstance(item, dict):
                    # First field inline with dash
                    keys = list(item.keys())
                    if not keys:
                        continue
                    lines.append(f"{pad}  - {keys[0]}: {_fmt_scalar(item[keys[0]])}")
                    for ik in keys[1:]:
                        lines.append(f"{pad}    {ik}: {_fmt_scalar(item[ik])}")
                else:
                    lines.append(f"{pad}  - {_fmt_scalar(item)}")
        else:
            lines.append(f"{pad}{k}: {_fmt_scalar(v)}")


def _fmt_scalar(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        # Quote if contains special YAML chars
        if any(c in v for c in ":#[]{}&*!|>'%@`") or v.strip() != v:
            return f'"{v.replace(chr(92), chr(92)+chr(92)).replace(chr(34), chr(92)+chr(34))}"'
        return v
    return str(v)


def load_tracking(project: str, entity: str) -> Optional[dict]:
    """Load tracking article for a project/entity. Returns full parsed frontmatter or None."""
    proj_dir = project_dir(project)
    fname = f"tracking_{entity}.md"
    fpath = proj_dir / fname
    if not fpath.exists():
        return None
    text = fpath.read_text(encoding="utf-8")
    data, _ = _parse_frontmatter(text)
    return data if data.get("type") == "tracking" else None


def save_tracking_article(project: str, entity: str, new_facts: dict, narrative: str = "") -> dict:
    """Create or update tracking article with bi-temporal frontmatter.

    new_facts: dict of fields to set in 'current' (e.g. {"version": "1.3.50"}).
    Existing 'current' moves to 'history[]' with to=now. New 'current.since' = now.
    Returns: {"path": str, "action": "created"|"updated", "old_current": dict, "new_current": dict}
    """
    proj_dir = project_dir(project)
    fname = f"tracking_{entity}.md"
    fpath = proj_dir / fname
    now_iso = datetime.now().date().isoformat()

    if fpath.exists():
        text = fpath.read_text(encoding="utf-8")
        data, _ = _parse_frontmatter(text)
        action = "updated"
    else:
        data = {
            "type": "tracking",
            "project": project,
            "entity": entity,
            "current": {},
            "history": [],
        }
        action = "created"

    # Defensive: if parse corrupted 'current' into non-dict (legacy parser bug),
    # skip old_current rather than crash. Will be regenerated fresh.
    raw_current = data.get("current")
    old_current = dict(raw_current) if isinstance(raw_current, dict) else {}
    if not isinstance(data.get("history"), list):
        data["history"] = []

    # Check if facts actually changed
    if old_current and all(old_current.get(k) == v for k, v in new_facts.items()):
        # No change — don't touch
        return {"path": str(fpath.relative_to(KNOWLEDGE_DIR)), "action": "unchanged",
                "old_current": old_current, "new_current": old_current}

    # Archive old current to history
    if old_current:
        hist_entry = dict(old_current)
        # Ensure 'from' set (fallback to 'since' or today)
        if "from" not in hist_entry:
            hist_entry["from"] = old_current.get("since", now_iso)
        hist_entry["to"] = now_iso
        if "since" in hist_entry:
            del hist_entry["since"]
        if not isinstance(data.get("history"), list):
            data["history"] = []
        data["history"].append(hist_entry)

    # Set new current
    new_current = dict(new_facts)
    new_current["since"] = now_iso
    data["current"] = new_current

    # Render body
    title = f"{project.title()} — current state ({entity})"
    if narrative:
        body = narrative
    else:
        body = _render_tracking_body(data)

    text = _write_frontmatter(data) + f"\n# {title}\n\n{body}\n"
    fpath.write_text(text, encoding="utf-8")

    return {
        "path": str(fpath.relative_to(KNOWLEDGE_DIR)),
        "action": action,
        "old_current": old_current,
        "new_current": new_current,
    }


_FACT_PATTERNS = {
    # Version: \d+.\d+.\d+ but NOT followed by another .\d — that would make it an IP.
    # Previous version w/o this guard captured the first 3 octets of '51.79.124.111' as
    # version '51.79.124', poisoning auto_update_tracking for every key matching version.
    "version": re.compile(
        # Lookbehind: not preceded by digit+dot (would mean we're mid-IP)
        # Lookahead:  not followed by dot+digit  (would mean another octet)
        r'(?<!\d\.)\bv?(\d+\.\d+\.\d+(?:-[a-z0-9.]+)?)(?!\.\d)\b',
        re.IGNORECASE,
    ),
    "ip": re.compile(r'\b((?:\d{1,3}\.){3}\d{1,3})(?::(\d{2,5}))?\b'),
    "port": re.compile(r':(\d{2,5})\b'),
    "url": re.compile(r'(https?://[^\s\)"\']+)'),
}

# Whitelist of EXACT key names (lowercased) eligible for auto_update_tracking per fact type.
# Substring matching is dangerous — keys like 'iptables_policy' contain 'ip' but are not IPs,
# 'hosting' contains 'host' but is a description, 'bitrix_version_date' contains 'version'
# but is a date. Strict allowlist prevents any IP/version in lesson text from overwriting
# unrelated fields.
_AUTOUPDATE_KEY_WHITELIST = {
    "version": {"version", "ver"},
    "ip": {"ip", "host", "server", "address"},
    "port": {"port"},
    "url": {"url", "link"},
}

# Skip patterns that indicate historical context (don't update current)
_HISTORICAL_MARKERS = re.compile(
    r'(было|ранее|раньше|старый|старая|переехал[иа]?\s+с|мигриров|архив|history|was|previously|old)',
    re.IGNORECASE,
)


def extract_facts_from_text(text: str, topic: str = "") -> dict:
    """Extract structural facts from free text. Returns {kind: [values]}.
    Only returns values that appear in non-historical context.
    """
    facts = {}
    combined = f"{topic}\n{text}"

    # Split into sentences (by . followed by space or newline) and lines; filter historical
    parts = re.split(r'(?:\.\s+|\n)', combined)
    relevant_text = " ".join(p for p in parts if not _HISTORICAL_MARKERS.search(p))

    for kind, pattern in _FACT_PATTERNS.items():
        matches = pattern.findall(relevant_text)
        if not matches:
            continue
        # Dedupe while preserving order
        seen = set()
        values = []
        for m in matches:
            v = m if isinstance(m, str) else m[0]
            if v and v not in seen:
                seen.add(v)
                values.append(v)
        if values:
            facts[kind] = values
    return facts


def list_tracking_articles(project: str) -> list[dict]:
    """List all tracking articles in project. Returns [{entity, current, path}, ...]."""
    proj = project_dir(project)
    if not proj.exists():
        return []
    result = []
    for md in proj.glob("tracking_*.md"):
        try:
            data, _ = _parse_frontmatter(md.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("type") != "tracking":
            continue
        # Defensive: skip articles where 'current' is corrupted (not a dict)
        current = data.get("current")
        if current is not None and not isinstance(current, dict):
            continue
        result.append({
            "entity": data.get("entity", md.stem.replace("tracking_", "")),
            "current": current or {},
            "path": str(md.relative_to(KNOWLEDGE_DIR)),
        })
    return result


def auto_update_tracking(project: str, text: str, topic: str = "") -> list[dict]:
    """Scan text for facts and update existing tracking articles safely.
    Rules:
      - Only updates existing tracking (no auto-create to avoid noise)
      - Match by fact type (version, ip, port, url) with existing current keys
      - Skip if new value same as current
    Returns list of updates performed: [{entity, key, old, new, path}]
    """
    facts = extract_facts_from_text(text, topic)
    if not facts:
        return []

    existing = list_tracking_articles(project)
    if not existing:
        return []

    updates = []
    for track in existing:
        current = track["current"] or {}
        entity = track["entity"]
        new_current = dict(current)
        changed = False

        # Match fact types to existing keys via strict whitelist (NOT substring).
        # Substring caused: 'iptables_policy' → ip, 'hosting' → ip, 'bitrix_version_date'
        # → version. Any IP/version mentioned in a lesson would then overwrite all such
        # unrelated fields. Whitelist: only exact key names update.
        for key, value in current.items():
            if key == "since":
                continue
            key_lower = key.lower()
            fact_type = None
            for ft, allowed in _AUTOUPDATE_KEY_WHITELIST.items():
                if key_lower in allowed:
                    fact_type = ft
                    break

            if fact_type and fact_type in facts:
                candidate = facts[fact_type][0]  # first match in text
                if str(candidate) != str(value):
                    new_current[key] = candidate
                    changed = True

        if changed:
            # Remove 'since' — save_tracking_article regenerates it
            new_facts = {k: v for k, v in new_current.items() if k != "since"}
            result = save_tracking_article(project, entity, new_facts)
            if result["action"] == "updated":
                updates.append({
                    "entity": entity,
                    "old": track["current"],
                    "new": result["new_current"],
                    "path": result["path"],
                })
    return updates


def _render_tracking_body(data: dict) -> str:
    """Render human-readable body from tracking frontmatter."""
    lines = []
    current = data.get("current") or {}
    if current:
        lines.append("## Текущее состояние\n")
        for k, v in current.items():
            lines.append(f"- **{k}:** {v}")
        lines.append("")

    history = data.get("history") or []
    if history:
        lines.append("## История")
        for entry in reversed(history):  # newest-first
            keys = list(entry.keys())
            if not keys:
                continue
            # Prefer showing 'version' or first key + from/to
            main_key = "version" if "version" in entry else keys[0]
            val = entry.get(main_key, "?")
            from_d = entry.get("from", "?")
            to_d = entry.get("to", "текущий")
            line = f"- **{val}** ({from_d} → {to_d})"
            extra = [f"{k}: {v}" for k, v in entry.items() if k not in {main_key, "from", "to"}]
            if extra:
                line += " — " + ", ".join(extra)
            lines.append(line)

    return "\n".join(lines)
