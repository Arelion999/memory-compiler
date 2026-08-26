"""Отчёты по базе: линт, пробелы знаний, сроки, дубли, история статьи.

Вынесено из handlers.py в v1.64.0: файл дорос до 3474 строк и держал почти
четверть кодовой базы. Шов выбран замером связности — константы и хелперы
ссылок (SECRET_POINTER_RE, _REPO_DOC_FILES, _link_targets, _base_link_index,
кэш сроков) используются ТОЛЬКО этими функциями и уезжают вместе с ними.

⚠️ ДВА ХЕЛПЕРА ИМПОРТИРУЮТСЯ ОТЛОЖЕННО, внутри функций: `_whoosh_async` (нужен
ещё восьми функциям handlers) и `_validate_repo_path` (нужен git_capture).
Импорт на уровне модуля дал бы цикл handlers ↔ reports, а отложенный вдобавок
сохраняет тестам возможность патчить их на handlers: имя берётся из модуля в
момент вызова, а не связывается при импорте.
"""

import asyncio
import re
import subprocess
from datetime import datetime, date, timedelta

import numpy as np
from mcp.types import TextContent

from memory_compiler.config import KNOWLEDGE_DIR, PROJECTS, _discover_projects
from memory_compiler.storage import (
    project_dir, safe_project_dir, safe_article_path, regenerate_index,
    article_title_tags, parse_meta_value, _parse_frontmatter, log_event,
    strip_code_blocks,
)
import memory_compiler.search as _search



# Разбор ссылок: нужен и линту здесь, и функциям handlers — те берут эти
# имена реэкспортом. Держим определение тут, где живёт основной потребитель.
_MD_LINK_RE = re.compile(r"\]\(([^)]+)\)")

# Вики-ссылка: ОБЯЗАТЕЛЬНО закрытая, однострочная, без пробелов и не длиннее 120.
# Алиас '[[цель|подпись]]' в базе не встречается ни разу, но разбор дешевле проверки.
# Прежний permissive-вариант '\[\[([^\]|]+)' не требовал закрывающих скобок: на фразе
# «инлайн [[ или # → дропдаун» он проглатывал сотни символов до первой ']' — в
# backlinks безвредно (не разрешается), а линт печатал такую «цель» на двадцать строк.
# Пробелы запрещены как второй рубеж: имя статьи их не содержит (проверено на 1618
# стемах), а весь пойманный мусор — содержит.
_WIKI_LINK_RE = re.compile(r"\[\[([^\[\]|\s]{1,120})\]\]")

def _strip_code(text: str) -> str:
    """Делегат → storage.strip_code_blocks (одно определение на весь код).

    Здесь это нужно потому, что '[[tool.mypy.overrides]]' — синтаксис TOML, а не
    ссылка (11 вхождений на живой базе); в storage тем же помощником отсекаются
    значения-примеры при извлечении фактов. Две копии разошлись бы — как уже
    разошлись два словаря паттернов.
    """
    return strip_code_blocks(text)

# Заглушка, оставшаяся после выноса значения в зашифрованный секрет, опознаётся по
# сигнатуре отсылки: вызов read_article с именем secret_*. Признак узкий — по всей
# базе (2656 статей) под него попадают только сами заглушки, а содержательные статьи,
# упоминающие секрет по имени файла, — нет.
SECRET_POINTER_RE = re.compile(r"read_article\([^)]*secret_[^)]*\)")

# Файлы РЕПОЗИТОРИЯ, а не статьи базы: README-пара, публичные доки, внутренний
# регламент. Check 9 ищет цель в каталоге проекта knowledge/, где их нет и быть не
# должно, поэтому без этого списка две ссылки висели «битыми» вечно — а fix вырезал
# бы из статьи само указание на файл. Сверять с раскладкой доков в CLAUDE.md.
_REPO_DOC_FILES = {
    "readme.md", "readme.ru.md", "changelog.md", "claude.md",
    "security.md", "security.ru.md",
    "claude-desktop-setup.md", "claude-desktop-setup.en.md",
    "release-process.md", "description.md",
}

# Кэш скана сроков: пересчитывать на каждый start_task накладно (400-650 мс
# на проект, 4.5 с на всю базу — замер 2026-08-26), а сроки меняются днями.
_STALE_CACHE: dict = {}

_STALE_TTL = 3600

def _link_targets(body: str) -> tuple[set[str], set[str]]:
    """Цели ссылок из готового тела: (вики-стемы, имена md-файлов).

    Единственное место, где в коде решается, «что считать ссылкой». Раньше определений
    было ТРИ — у backlinks, у проверки сирот и у проверки битых ссылок, — и они
    расходились: сироты не видели вики-ссылок и чужих проектов, битые не видели вики
    вовсе, а backlinks единственный отсекал машинные блоки.
    """
    wiki = {t.strip() for t in _WIKI_LINK_RE.findall(body) if t.strip()}
    md = set()
    for raw in _MD_LINK_RE.findall(body):
        href = raw.split("#", 1)[0].strip()
        if href.endswith(".md") and not href.startswith(("http://", "https://")):
            md.add(href.split("/")[-1])
    return wiki, md

def _link_scan_body(text: str) -> str:
    """Тело для ГИГИЕНЫ ссылок: без кода, но С машинными блоками.

    Для сиротства и битых ссылок авто-блок «См. также» — полноценный указатель:
    вопрос «на статью хоть что-то ссылается» шире, чем «кто сослался осознанно».
    """
    return _strip_code(text)

def _base_link_index() -> tuple[set[str], set[str], set[str]]:
    """Один проход по базе: (известные стемы, куда ссылаются вики, куда ссылаются md).

    Строится РАЗ на вызов линта, а не на проект: входящая ссылка может прийти из
    любого проекта (на живой базе infra → niksdesk), и per-project сбор объявлял такие
    статьи сиротами.
    """
    stems, ref_wiki, ref_md = set(), set(), set()
    for proj in _discover_projects():
        pdir = KNOWLEDGE_DIR / proj
        if not pdir.exists():
            continue
        for md_file in pdir.glob("*.md"):
            stems.add(md_file.stem)
            try:
                body = _link_scan_body(md_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            w, m = _link_targets(body)
            ref_wiki |= {t for t in w if t != md_file.stem}
            ref_md |= {t for t in m if t != md_file.name}
    return stems, ref_wiki, ref_md

async def lint(project: str = "all", fix: bool = False, verbose: bool = False) -> list[TextContent]:
    """Check knowledge base health."""
    issues = []
    fixed = []
    # tag.lower() -> {написание: [файлы]}; коллизии печатаем после обхода
    tag_case: dict = {}
    check_projects = PROJECTS if project == "all" else [project]
    # Индекс ссылок по ВСЕЙ базе, один раз на вызов (~0.5 с): нужен для сиротства
    # (входящая ссылка приходит и из чужого проекта) и для битых вики-ссылок.
    known_stems, referenced_wiki, referenced_md = await asyncio.to_thread(_base_link_index)

    for proj in check_projects:
        proj_path = KNOWLEDGE_DIR / proj
        if not proj_path.exists():
            continue
        articles = list(proj_path.glob("*.md"))
        if not articles:
            continue
        # Статьи-указатели на вынесенный секрет: собираем здесь, исключаем в Check 5.
        secret_pointers: set[str] = set()

        for a in articles:
            # Service files (_*.md, tracking_*.md) lack yaml metadata
            # by design — they are engine-managed. Skip Check 1/2 for them.
            is_service = a.name.startswith("_") or a.name.startswith("tracking_")
            text = a.read_text(encoding="utf-8")
            # lines — от ТЕЛА; text остаётся сырым, в него же пишем при fix.
            # На срезах по сырому файлу lint давал 92 ложных «нет метаданных»,
            # у 79 статей не отрабатывал Check 3 и у 92 — Check 4. Инструмент
            # здоровья базы врал именно на самых новых статьях.
            lines = _parse_frontmatter(text)[1].splitlines()

            # Когда значение выносят в зашифрованный секрет, на месте статьи остаётся
            # заглушка из одного шаблона: «вынесено в секрет, смотреть там-то». Такие
            # заглушки похожи друг на друга ПО ПОСТРОЕНИЮ — ровно как сами секреты,
            # уже исключённые в Check 5. На проде это дало ложный дубль sim=0.92 между
            # «Ключ для WINDOWS» и «Строка подключения к GIT»: статьи о разном, общей
            # была только обвязка.
            if SECRET_POINTER_RE.search(text):
                secret_pointers.add(a.name)

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

            # Check 6: кандидат на ротацию (>180 дней). ТОЛЬКО ОТЧЁТ, без перемещения.
            # Проверка жила внутри цикла сравнения дублей (перебор ключей эмбеддингов) и
            # работала с `a`/`updated`, вытекшими из ЭТОГО цикла: проверялась лишь
            # ПОСЛЕДНЯЯ статья проекта, предупреждение дублировалось по разу на ключ, а
            # при fix=True один и тот же файл переименовывался по кругу — линт падал
            # FileNotFoundError со второй итерации. Поэтому проектных archive/ в базе
            # не появилось ни одного: ротация никогда не работала.
            # Перемещение убрано намеренно: <проект>/archive и daily/archive вне поиска
            # (см. rebuild_index), то есть «архивация» = скрытие статьи из базы. Массовое
            # скрытие — решение человека, а не побочный эффект линта.
            if updated and (datetime.now() - updated).days > 180:
                days = (datetime.now() - updated).days
                issues.append(f"⚠️ [{proj}] {a.name} — кандидат на архивацию ({days} дней)")

            # Check 4: Tag normalization
            for line in lines[:10]:
                if line.startswith("**Теги:**"):
                    # ⚠️ НЕ split(':', 1)[1]: закрывающие звёздочки метки «**Теги:**»
                    # стоят ПОСЛЕ двоеточия и попадали в значение. Пока его только
                    # читали — шум; здесь значение ЗАПИСЫВАЕТСЯ обратно, и в файл
                    # уезжало '**Теги:** ** ftp, mcp'. Так испорчено 126 статей,
                    # 81 из них секреты (их merge_into_article не самолечит — отказывает
                    # секретам), а 15 получили двойную порчу '** ** '. Следствие:
                    # search_by_tag не находил ни одну из 126 по первому тегу.
                    tags_str = parse_meta_value(line)
                    raw_tags = [t.strip() for t in tags_str.split(",") if t.strip() and t.strip() != "\u2014"]
                    # РЕГИСТР НЕ НОРМАЛИЗУЕМ (с v1.54.2). Прежнее условие
                    # `raw_tags != lower_tags` требовало нижнего регистра от ВСЕХ тегов
                    # и ругалось на MAX, ПУЭ, LG, QR-код, DESIGNER — правильные написания
                    # имён и аббревиатур: на живой базе 32 ложных пункта из 41, причём
                    # fix их «нормализовал», то есть портил. Реальна ровно одна беда —
                    # ОДИН тег в разных написаниях: search_by_tag и чипы /api/tags
                    # разводят MAX и max по разным ведёркам. Копим и печатаем ОДНОЙ
                    # строкой на тег после обхода, а не строкой на каждую статью.
                    for _t in raw_tags:
                        tag_case.setdefault(_t.lower(), {}).setdefault(_t, []).append(f"{proj}/{a.name}")
                    if fix and raw_tags:
                        # Чиним РАЗМЕТКУ метки, а не написание тегов: '**Теги:** ** ftp'
                        # (наследие прежнего fix, 126 статей) приводим к канону. Здоровая
                        # строка обязана остаться байт-в-байт прежней.
                        canonical = f"**Теги:** {', '.join(raw_tags)}"
                        if line != canonical:
                            # count=1: replace шёл по ВСЕМУ документу и правил строки
                            # тегов внутри записей тоже (у daily-агрегатов их десятки).
                            text = text.replace(line, canonical, 1)
                            a.write_text(text, encoding="utf-8")
                            fixed.append(f"\U0001f527 [{proj}] {a.name} \u2014 разметка тегов починена")
                    break

        # Check 5: Duplicates (semantic similarity) — compare parent articles only.
        # Снимок актуального _embeddings под локом (см. snapshot_embeddings) — иначе
        # фоновый rebuild может мутировать dict во время comprehension (RuntimeError).
        # Служебные файлы (_session, _log, tracking_*) ведёт движок — сравнивать их
        # как статьи бессмысленно. Без этого фильтра на каждый проект приходило по два
        # «дубля» вида '_session.md ↔ tracking_release.md': оба файла описывают одно и
        # то же состояние по построению. Тот же фильтр уже стоит в Check 1/2.
        # Плюс СЕКРЕТЫ: в индекс у них идёт плейсхолдер (титул + теги + слова-намерения),
        # а не тело — значит все секреты проекта похожи ПО ПОСТРОЕНИЮ. На проде это дало
        # шесть «дублей» подряд со схожестью 0.90–0.96, и все ложные: сравнивались маски.
        proj_embeddings = {k: v for k, v in _search.snapshot_embeddings().items()
                          if k.startswith(f"{proj}/") and "#chunk" not in k
                          and not k.split("/", 1)[-1].startswith(("_", "tracking_", "secret_"))
                          and k.split("/", 1)[-1] not in secret_pointers}
        keys = list(proj_embeddings.keys())
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                sim = float(np.dot(proj_embeddings[keys[i]], proj_embeddings[keys[j]]))
                if sim > 0.85:
                    name_i = keys[i].split("/", 1)[-1]
                    name_j = keys[j].split("/", 1)[-1]
                    issues.append(f"\u26a0\ufe0f [{proj}] Возможный дубль (sim={sim:.2f}): {name_i} \u2194 {name_j}")

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
            # Входящие ссылки берём из ОБЩЕГО индекса базы (_base_link_index): сбор
            # только по своему проекту объявлял сиротой статью, на которую ссылались
            # из другого проекта — на живой базе так выглядела связь infra → niksdesk.
            # Учитываются и вики-ссылки: раньше проверка их не видела вовсе.
            for a in non_service:
                if a.name not in referenced_md and a.stem not in referenced_wiki:
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
            # Битые ВИКИ-ссылки: раньше не проверялись вовсе — считалось, что связи
            # живут в markdown. С появлением backlinks битая вики-цель это потерянная
            # связь, а не косметика. Блоки кода исключены: '[[tool.mypy.overrides]]' —
            # синтаксис TOML, таких «целей» на живой базе 11.
            wiki_targets, _ = _link_targets(_link_scan_body(atext))
            for wt in sorted(wiki_targets - known_stems):
                issues.append(
                    f"⚠️ [{proj}] {a.name} — dead wiki link → [[{wt}]]")
            seen_dead = set()
            # Two passes: first collect dead refs, then optionally strip them when fix=True.
            dead_matches = []  # list of (match, display) for fix pass
            for m in md_link.finditer(atext):
                cross_proj, target = m.group(1), m.group(2)
                # Внешняя цель (файл репозитория или чужой каталог) — не потерянная
                # связь между статьями, а ссылка наружу. Проверять её здесь нечем.
                # Каталог проверяем ФАКТОМ существования, а не списком PROJECTS: он
                # импортирован по значению и в тестах (да и после add_project)
                # отстаёт — на этом сразу упал кросс-проектный сторож.
                if target.lower() in _REPO_DOC_FILES or (
                        cross_proj and not (KNOWLEDGE_DIR / cross_proj).is_dir()):
                    continue
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

    # Check 4 (итог): один тег в разных написаниях = разъехавшийся поиск по тегу.
    for _low, _variants in sorted(tag_case.items()):
        if len(_variants) < 2:
            continue
        _shown = ", ".join(f"{_v} ({len(_files)})" for _v, _files in sorted(_variants.items()))
        _where = sorted({f for _files in _variants.values() for f in _files})[:3]
        issues.append(f"\u2139\ufe0f коллизия регистра тега \u00ab{_low}\u00bb: {_shown} \u2014 напр. {', '.join(_where)}")

    if fix:
        await asyncio.to_thread(regenerate_index)
        fixed.append("\U0001f527 index.md перегенерирован")

    # СВОРАЧИВАНИЕ НОРМЫ. Замер по всей базе 2026-07-21: 1157 записей, из них 70%
    # «сирота» и 25% «устарела» — это описание нормального СОСТОЯНИЯ базы (ручных
    # входящих ссылок 101 на 1612 статей), а не список проблем. Полезный сигнал
    # составлял 4% и тонул в тысяче строк: 40 битых ссылок было не разглядеть.
    # Информация не теряется — остаётся счётчик, а verbose=True печатает как раньше.
    _COLLAPSIBLE = (
        ("устарела", "устарело (>90 дней без обновления)"),
        ("сирота", "сирот (нет входящих ссылок)"),
        ("связанные:", "подсказок о связанных статьях"),
    )
    collapsed_counts: dict[str, int] = {}
    issues_shown = issues
    if not verbose:
        kept = []
        for line in issues:
            for marker, label in _COLLAPSIBLE:
                if marker in line:
                    collapsed_counts[label] = collapsed_counts.get(label, 0) + 1
                    break
            else:
                kept.append(line)
        issues_shown = kept

    out = [f"# Lint \u2014 проверка базы знаний\n"]
    if issues_shown:
        out.append(f"## Проблемы ({len(issues_shown)})\n")
        out.extend(issues_shown)
    if collapsed_counts:
        out.append("\n## Норма базы (свёрнуто; verbose=true развернёт построчно)\n")
        for label, n in sorted(collapsed_counts.items(), key=lambda kv: -kv[1]):
            out.append(f"ℹ️ {n} — {label}")
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
    # Отложенно: держит цикл handlers ↔ reports разорванным и оставляет
    # тестам возможность патчить _whoosh_async на модуле handlers.
    from memory_compiler.handlers import _whoosh_async
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

async def consolidate(project: str = "all", min_sim: float = 0.985) -> list[TextContent]:
    "Дубли: near-exact (точный/containment по тексту, надёжно на коротком RU-корпусе) + похожие темы (эмбеддинги, порог 0.985 — на низком ложняки). Решение 2026-07-18."
    import memory_compiler.search as _smod
    import numpy as np
    from memory_compiler.storage import near_exact_dupes
    _NL = chr(10)
    parts = [f"# Consolidate report ({project}){_NL}"]
    ne = near_exact_dupes(project)
    if ne:
        parts.append(f"## РЕАЛЬНЫЕ дубли (точный/containment матч): {len(ne)}{_NL}")
        parts.append("Слить: edit_article(append=true) в канон + delete_article дубля.")
        for d in ne[:25]:
            parts.append(f"- **{d['kind']}**: `{d['a']}` <-> `{d['b']}`")
        if len(ne) > 25:
            parts.append(f"*...и ещё {len(ne) - 25}.*")
    else:
        parts.append(f"## РЕАЛЬНЫЕ дубли: не найдено (точный/containment матч){_NL}")
    if not _smod._embeddings:
        parts.append(_NL + "*Эмбеддинги не построены — раздел похожих тем пропущен (reindex).*")
        return [TextContent(type="text", text=_NL.join(parts))]
    article_chunks = {}
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
        parts.append(_NL + "*Меньше 2 статей — для похожих тем сравнивать нечего.*")
        return [TextContent(type="text", text=_NL.join(parts))]
    paths = list(article_chunks.keys())
    vectors = np.array([np.mean(article_chunks[p], axis=0) for p in paths])
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1
    vectors = vectors / norms
    sim_matrix = vectors @ vectors.T
    pairs = []
    n = len(paths)
    for i in range(n):
        for j in range(i + 1, n):
            sim = float(sim_matrix[i, j])
            if sim >= min_sim:
                pairs.append({"a": paths[i], "b": paths[j], "sim": sim})
    pairs.sort(key=lambda p: -p["sim"])
    parts.append(_NL + f"## Похожие темы (эмбеддинги, порог {min_sim}) — НЕ обязательно дубли")
    parts.append(f"*Найдено пар: {len(pairs)}*{_NL}")
    if not pairs:
        parts.append("*Нет пар выше порога — по эмбеддингам чисто.*")
    else:
        for p in pairs[:25]:
            title_a = _smod._embed_texts.get(p["a"], p["a"]).split(_NL)[0][:60]
            title_b = _smod._embed_texts.get(p["b"], p["b"]).split(_NL)[0][:60]
            parts.append(_NL + f"**sim {p['sim']:.3f}**")
            parts.append(f"- A: `{p['a']}` — {title_a}")
            parts.append(f"- B: `{p['b']}` — {title_b}")
        if len(pairs) > 25:
            parts.append(_NL + f"*...и ещё {len(pairs) - 25} пар.*")
    return [TextContent(type="text", text=_NL.join(parts))]

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
        # Срез [:15] не «пропускал frontmatter», а надеялся, что тот короче: медиана
        # 13 строк, p90 40, максимум 275. У 12 статей из 12 теги терялись, у 4 из 12
        # заголовком становилось имя файла.
        title, tags = article_title_tags(text, fallback=a.stem)
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

async def article_history(project: str, filename: str) -> list[TextContent]:
    # safe_article_path как в read/delete/edit — иначе traversal-зонд существования
    # файлов вне базы (LOW из аудита 2026-07-03)
    try:
        fpath = safe_article_path(project, filename)
    except ValueError as e:
        return [TextContent(type="text", text=f"❌ Небезопасный путь: {e}")]
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

def _scan_stale(project: str = "all", warn_days: int = 30) -> dict:
    """Синхронный скан сроков. ⚠️ Читает ВСЕ статьи проекта — звать только из
    потока (asyncio.to_thread), иначе блокирует event loop на сотни миллисекунд.
    Возвращает три списка: истёкшее, истекающее, кандидаты на ротацию."""
    from memory_compiler.storage import _parse_frontmatter
    import memory_compiler.config as _cfg

    today = datetime.now().date()
    warn_until = today + timedelta(days=warn_days)
    stale_180 = today - timedelta(days=180)

    # ⚠️ ГОЛОЕ «до» ЛОВИТ НЕ СРОКИ. Замер 2026-08-26 на боевой базе: из 22 записей
    # «уже истекло» настоящими сроками были единицы, остальное — многозначность
    # русского предлога и чужие исторические справки:
    #   «пометки поставлены ДО 04.02.2026»      — здесь «до» = «раньше чем»
    #   «закрытие отложено до 01.07.2026»       — прошедшее событие, не срок
    #   «3.1 мертва (поддержка до 01.03.2023)»  — чужой продукт, справка на 1274 дня назад
    # Настоящие сигналы выглядят иначе: «554 $m со сроком до 09.09.2026»,
    # «мораторий … До 06.09.2026 НЕ удаляется». Поэтому дате обязан
    # предшествовать маркер СРОКА, а не любое «до».
    # Маркер срока обязан стоять ВПЛОТНУЮ к дате. Промежуточный вариант «маркер
    # в окне 80 символов» проверен и отвергнут замером: он поднял выдачу с 22 до
    # 97 записей — слова «срок», «лицензия», «действует» попадаются в тексте
    # часто, и любая дата рядом становилась «сроком». Цена соседства — теряются
    # сигналы вида «мораторий … До 06.09.2026 НЕ удаляется», где между маркером и
    # датой пол-предложения. Выбор осознанный: ложные срабатывания обесценивают
    # проверку целиком (её и так не звали ни разу за 4.5 месяца), а пропущенный
    # сигнал стоит одного просмотра статьи.
    DEADLINE = (r'(?:valid\s*(?:until|to|till)|valid_to|expires?|expiry|'
                r'истека\w+|сгора\w+|срок\w*\s+(?:действия\s+)?до|действ\w+\s+до|'
                r'действителен\s+до|мораторий\s+до|оплач\w+\s+до|продл\w+\s+до|'
                r'не\s+позднее)')
    DATE_PATTERNS = [
        re.compile(DEADLINE + r'\s*[:=]?\s*(\d{4})[-/](\d{1,2})[-/](\d{1,2})', re.IGNORECASE),
        re.compile(DEADLINE + r'\s*[:=]?\s*(\d{1,2})[./](\d{1,2})[./](\d{4})', re.IGNORECASE),
    ]
    # Дата, приехавшая из ССЫЛКИ на другую статью (раздел «См. также»), — чужой
    # факт: он уже посчитан там, где живёт, и дублировать его незачем.
    LINK_LINE = re.compile(r'\]\(\.{0,2}/|\]\(memory://')

    def _is_deadline(text: str, pos: int) -> bool:
        line_start = text.rfind("\n", 0, pos) + 1
        line_end = text.find("\n", pos)
        line = text[line_start:line_end if line_end > 0 else len(text)]
        return not LINK_LINE.search(line)
    # Истёкшее давно — это история проекта, а не задача. Показываем свежепротухшее:
    # без окна в выдачу лезли записи трёхлетней давности и топили полезное.
    EXPIRED_WINDOW_DAYS = 90
    expired_after = today - timedelta(days=EXPIRED_WINDOW_DAYS)

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
                    if not _is_deadline(text, m.start()):
                        continue          # дата есть, а срока нет — см. коммент выше
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
                    if dt < expired_after:
                        continue          # протухло давно — история, а не задача
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

    return {"expired": expired, "expiring": expiring,
            "stale_secrets": stale_secrets, "warn_days": warn_days}

def stale_summary(project: str, warn_days: int = 30, limit: int = 3) -> list:
    """Короткая сводка сроков для стартового контекста, через кэш.

    Инструмент stale_facts за 4.5 месяца не позвали НИ РАЗУ (замер по аудиту).
    Проверка, которую надо вспомнить и вызвать, механизмом актуальности не
    работает — поэтому её вывод показывается сам, при старте задачи.
    """
    import time as _t
    hit = _STALE_CACHE.get(project)
    if hit and _t.time() - hit[0] < _STALE_TTL:
        data = hit[1]
    else:
        data = _scan_stale(project, warn_days)
        _STALE_CACHE[project] = (_t.time(), data)
    rows = [dict(e, kind="истёк") for e in data["expired"]]
    rows += [dict(e, kind="истекает") for e in data["expiring"]]
    rows.sort(key=lambda e: e["days_left"])
    return rows[:limit]

async def stale_facts(project: str = "all", warn_days: int = 30) -> list[TextContent]:
    """Stale fact watcher: сроки из текста статей и tracking-frontmatter."""
    data = await asyncio.to_thread(_scan_stale, project, warn_days)
    expired, expiring = data["expired"], data["expiring"]
    stale_secrets = data["stale_secrets"]
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
