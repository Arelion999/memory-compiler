"""Сторож двуязычной документации.

Переводы расходятся молча: правят русский файл, английский остаётся со старой
структурой, и читатель на другом языке видит другой документ. Тест держит пары
структурно идентичными и ловит битые ссылки, пока они ещё не уехали в паблик.

Раскладка B, правило одно: **базовое имя занимает тот язык, который платформа
открывает САМА**. README.md — английский, потому что GitHub рендерит его на морде.
docs/security.md — английский, потому что GitHub подхватывает его как Security policy
и показывает по кнопке Security. А docs/claude-desktop-setup.md остаётся русским:
туда приходят только по ссылке, дефолтного входа у него нет. Второй язык получает
суффикс (`.ru.md` или `.en.md`) — какой именно, диктует то же правило.

Второй сторож — соответствие ЯЗЫКА. Ссылка бывает рабочей и при этом ведёт в чужой
текст: файл существует, битой ссылки нет, а русский читатель попадает в английский
документ. Проверяется и между доками, и в указателях из комментариев кода.
"""
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# (русский файл, английский файл)
PAIRS = [
    ("README.ru.md", "README.md"),
    ("docs/claude-desktop-setup.md", "docs/claude-desktop-setup.en.md"),
    ("docs/security.ru.md", "docs/security.md"),
]

# Язык файла и его напарник выводятся из PAIRS, отдельного списка нет:
# заводишь новую пару — обе языковые проверки подхватывают её сами.
LANG = {}
PAIR_OF = {}
for _ru, _en in PAIRS:
    LANG[_ru], LANG[_en] = "ru", "en"
    PAIR_OF[_ru], PAIR_OF[_en] = _en, _ru

CYRILLIC = re.compile(r"[а-яёА-ЯЁ]")
CODE_GLOBS = ("*.py", "*.sh", "*.ps1")

# Указатель «иди читать туда» — только он и проверяется на язык. Комментарий, который
# РАССКАЗЫВАЕТ про файл («docs/security.md GitHub рендерит по двум адресам»), говорит
# о нём, а не отсылает в него, и на языке цели быть не обязан. Без этого разделения
# проверка падала на собственных комментариях этого файла.
REFERRAL = re.compile(r"(?:см\.|смотри|подробнее|see|details)\s*:?\s*", re.IGNORECASE)

# Переключатель обычно ссылается на пару относительно — но docs/security.md GitHub
# рендерит по ДВУМ адресам: /blob/master/docs/security.md (база — docs/) и
# /security/policy (база — КОРЕНЬ репо). Относительная `security.ru.md` во втором случае
# разрешается в /blob/master/security.ru.md и даёт 404 — проверено вживую 2026-07-20.
# Относительной ссылки, работающей в обеих, не существует, поэтому только абсолютный URL.
# Ключ — файл, СОДЕРЖАЩИЙ ссылку.
ABSOLUTE_SWITCHER = {
    "docs/security.md": "https://github.com/Arelion999/memory-compiler/blob/master/docs/security.ru.md",
}

BLOB_URL = re.compile(r"^https://github\.com/[^/]+/[^/]+/blob/[^/]+/(.+)$")

FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
MD_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def headings(path: Path):
    """Заголовки файла как [(уровень, текст)], блоки кода пропускаются.

    Пропуск обязателен: в доках есть ```bash с комментариями `# ...` и блок
    ```markdown с настоящими заголовками внутри шаблона CLAUDE.md — без
    фильтра они бы считались заголовками документа.
    """
    out, in_fence = [], False
    for line in path.read_text(encoding="utf-8").splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = HEADING.match(line)
        if m:
            out.append((len(m.group(1)), m.group(2)))
    return out


def slug(text: str) -> str:
    """Якорь в стиле GitHub: нижний регистр, пунктуация долой, пробелы в дефисы."""
    s = text.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    return re.sub(r"\s+", "-", s)


def links_with_lines(path: Path):
    """Ссылки файла как [(номер строки, url)], блоки кода пропускаются."""
    out, in_fence = [], False
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.extend((lineno, url) for url in MD_LINK.findall(line))
    return out


def links(path: Path):
    """Ссылки файла, блоки кода пропускаются."""
    return [url for _, url in links_with_lines(path)]


@pytest.mark.parametrize("ru,en", PAIRS)
def test_pair_exists(ru, en):
    assert (REPO / ru).is_file(), f"нет русского файла {ru}"
    assert (REPO / en).is_file(), f"нет английского файла {en}"


@pytest.mark.parametrize("ru,en", PAIRS)
def test_heading_structure_matches(ru, en):
    """Уровни заголовков совпадают по порядку и количеству.

    Тексты не сравниваем — они переведены; сравниваем скелет.
    """
    ru_h, en_h = headings(REPO / ru), headings(REPO / en)
    ru_levels = [lvl for lvl, _ in ru_h]
    en_levels = [lvl for lvl, _ in en_h]
    assert len(ru_h) == len(en_h), (
        f"разное число заголовков: {ru} — {len(ru_h)}, {en} — {len(en_h)}\n"
        f"{ru}: {[t for _, t in ru_h]}\n{en}: {[t for _, t in en_h]}"
    )
    assert ru_levels == en_levels, (
        f"разошлась вложенность заголовков {ru} vs {en}:\n"
        f"{list(zip(ru_levels, en_levels))}"
    )


@pytest.mark.parametrize("ru,en", PAIRS)
def test_language_switcher(ru, en):
    """В каждом файле есть переключатель языка, ведущий на пару."""
    ru_body = (REPO / ru).read_text(encoding="utf-8")
    en_body = (REPO / en).read_text(encoding="utf-8")
    same_dir = Path(ru).parent == Path(en).parent
    ru_target = ABSOLUTE_SWITCHER.get(ru, Path(en).name if same_dir else en)
    en_target = ABSOLUTE_SWITCHER.get(en, Path(ru).name if same_dir else ru)
    assert f"[English]({ru_target})" in ru_body, f"{ru}: нет ссылки на [English]({ru_target})"
    assert "**Русский**" in ru_body, f"{ru}: текущий язык не помечен жирным"
    assert f"[Русский]({en_target})" in en_body, f"{en}: нет ссылки на [Русский]({en_target})"
    assert "**English**" in en_body, f"{en}: текущий язык не помечен жирным"


@pytest.mark.parametrize("doc,url", sorted(ABSOLUTE_SWITCHER.items()))
def test_absolute_switcher_points_at_real_file(doc, url):
    """Абсолютный переключатель ведёт на файл, который в репо есть.

    test_relative_md_links_resolve пропускает http(s)-ссылки, поэтому без этой
    проверки абсолютный URL молча обходил бы сторож.
    """
    m = BLOB_URL.match(url)
    assert m, f"{doc}: не разобрал абсолютный URL {url}"
    assert (REPO / m.group(1)).is_file(), f"{doc}: {url} → в репо нет {m.group(1)}"


@pytest.mark.parametrize("doc", [f for pair in PAIRS for f in pair])
def test_anchors_resolve(doc):
    """Каждая внутренняя ссылка `(#...)` попадает в существующий заголовок."""
    path = REPO / doc
    anchors = {slug(text) for _, text in headings(path)}
    broken = [
        link for link in links(path)
        if link.startswith("#") and link[1:] not in anchors
    ]
    assert not broken, f"{doc}: битые якоря {broken}; есть {sorted(anchors)}"


@pytest.mark.parametrize("doc", [f for pair in PAIRS for f in pair])
def test_relative_md_links_resolve(doc):
    """Относительные ссылки на .md ведут на существующие файлы."""
    path = REPO / doc
    broken = [
        link for link in links(path)
        if link.endswith(".md")
        and not link.startswith(("#", "http://", "https://"))
        and not (path.parent / link).is_file()
    ]
    assert not broken, f"{doc}: битые ссылки на файлы {broken}"


@pytest.mark.parametrize("doc", sorted(LANG))
def test_cross_doc_links_keep_language(doc):
    """Ссылка на ДРУГОЙ док ведёт на версию своего языка.

    Существование ссылки сторожит test_relative_md_links_resolve. Здесь важен язык:
    ссылка бывает рабочей и при этом ведёт в чужой текст — так README.ru.md чуть не
    остался с указателем на английский docs/security.md после перестановки пары.

    Ссылка на СВОЮ пару пропускается: это переключатель языка, ему и положено вести
    в другой язык. Исключение выводится из PAIR_OF, списка имён нет.
    """
    path = REPO / doc
    bad = []
    for lineno, url in links_with_lines(path):
        # Якорь отрезаем: ссылка вида docs/security.md#раздел ведёт в тот же файл,
        # и без обрезки путь просто не разрешился бы, молча пройдя мимо проверки.
        target_url = url.split("#", 1)[0]
        if not target_url:
            continue  # чистый якорь внутри своего файла
        blob = BLOB_URL.match(target_url)
        if blob:
            # Абсолютный переключатель (docs/security.md) — путь уже от корня репо.
            # Без этой ветки https-ссылка отсекалась бы и язык не проверялся вовсе.
            target = blob.group(1)
        elif target_url.startswith(("http://", "https://")):
            continue  # внешняя ссылка, не наш док
        else:
            try:
                target = (path.parent / target_url).resolve().relative_to(REPO).as_posix()
            except ValueError:
                continue
        if target not in LANG or target == PAIR_OF[doc]:
            continue
        if LANG[target] != LANG[doc]:
            bad.append(f"{doc}:{lineno} ({LANG[doc]}) → {target} ({LANG[target]})")
    assert not bad, "ссылка ведёт в чужой язык:\n  " + "\n  ".join(bad)


def code_files():
    """Отслеживаемые файлы кода. git ls-files, а не rglob — чтобы не зацепить
    venv, node_modules и прочее неотслеживаемое."""
    done = subprocess.run(
        ["git", "ls-files", *CODE_GLOBS],
        cwd=REPO, capture_output=True, text=True, check=True,
    )
    return [line for line in done.stdout.splitlines() if line]


def test_code_comments_link_to_own_language():
    """Комментарий в коде ссылается на док своего языка.

    Две отсечки, обе без списка исключений по именам файлов:

    1. Только цельные комментарии (первый непробельный символ — #). Так строка с
       README.md внутри git-лога в tests/test_storage.py остаётся данными, а не ссылкой.
    2. Только после маркера отсылки (REFERRAL). Комментарий, объясняющий устройство
       файла, упоминает его имя, но никуда не отсылает — язык там не при чём.

    Язык комментария — по кириллице во ВСЕЙ строке, а не в хвосте: хвост часто и есть
    голый путь («см. docs/security.ru.md»), в нём кириллицы нет и язык определился бы
    неверно. Язык цели — карта LANG.
    """
    bad = []
    for rel in code_files():
        for lineno, line in enumerate(
            (REPO / rel).read_text(encoding="utf-8").splitlines(), 1
        ):
            if not line.lstrip().startswith("#"):
                continue
            referral = REFERRAL.search(line)
            if not referral:
                continue
            tail = line[referral.end():]
            lang = "ru" if CYRILLIC.search(line) else "en"
            for doc, doc_lang in LANG.items():
                if doc not in tail and doc.rsplit("/", 1)[-1] not in tail:
                    continue
                if doc_lang != lang:
                    bad.append(
                        f"{rel}:{lineno} (комментарий {lang}) → {doc} ({doc_lang})"
                    )
    assert not bad, "комментарий ведёт на док чужого языка:\n  " + "\n  ".join(bad)
