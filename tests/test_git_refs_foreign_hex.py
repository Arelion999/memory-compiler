"""Чужой hex не попадает в «Коммиты» (v1.92.8).

До фикса коммитом считался любой hex 7–12 или 40 символов между пробелами, а фильтр
смотрел только на форму. Живой случай 25.09.2026: save_lesson и finish_task ответили
«🔗 Git: commit: 7ff1e2f6», а это sha1 содержимого файла трекера («sha1 7ff1e2f6 тот
же»), и в статью лёг раздел «Коммиты: 7ff1e2f6».

Замер на боевой базе в тот же день: 1190 записей «Коммиты» в 1586 статьях, эталон —
git cat-file по 38 репозиториям плюс ручная разметка ненайденного. Ложных 142
(11,9%): ревизии alembic 97 (все ровно 12 hex), ID сессий и scratchpad 12, GUID и
ID кластера 1С 11, sha1/md5/sha256 файлов 8, ed25519 7, ID docker 6, blob 1.
Правило этого релиза отсекает 135 из них и не теряет ни одного из 1045 коммитов.
Вариант «требовать рядом слово-признак коммита» отвергнут: он терял 154 коммита.

Фикстуры — формы фраз из живых статей, значения hex заменены.
"""
import pytest

from memory_compiler.storage import extract_git_refs


def commits(text: str) -> list[str]:
    return extract_git_refs(text, "").get("commit", [])


# Позитивный контроль: настоящие коммиты в тех формах, в каких они стоят в базе.
# Без него тесты «ложного коммита нет» прошли бы и на правиле, режущем всё подряд.
REAL_COMMITS = [
    ("Коммит 3c8f5a1, тег v1.92.5, push в 16:59.", ["3c8f5a1"]),
    ("КОММИТЫ: 1f8c66e спека, 34f7914 модель/сервис/миграция, 04d8e02 API/отчёт, "
     "75a9054 витрина.", ["04d8e02", "1f8c66e", "34f7914", "75a9054"]),
    ("**Коммиты (за сессию F9):** 0a4ae88 service / 47e7a81 handler", ["0a4ae88", "47e7a81"]),
    ("в теле написано «изменения в контейнер не попадают» (в 06e26ed и f9ebd3a этой "
     "строки не было).", ["06e26ed", "f9ebd3a"]),
    ("- Миграции работают (после фикса 500c9e89 путей в Alembic)", ["500c9e89"]),
    ("sha1 содержимого тот же, коммит 1a2b3c4 уже в master", ["1a2b3c4"]),
    ("хеш коммита 5e6f7a8 совпал с тегом", ["5e6f7a8"]),
    ("reverted in commit hash 6d2e9f1 after review", ["6d2e9f1"]),
    ("9f4c1d2 fix: v1.92.3 — имя файла трекера", ["9f4c1d2"]),
    ("фикс в Chromium: commit 963206a961a7992b048d07f756f87f11fa2e5420",
     ["963206a961a7992b048d07f756f87f11fa2e5420"]),
    ("выгрузка из большого репозитория: c52b6d468c", ["c52b6d468c"]),
]


@pytest.mark.parametrize("text,expected", REAL_COMMITS)
def test_real_commits_stay(text, expected):
    assert commits(text) == expected


# Хэш содержимого: маркер стоит перед hex, между ними нет слова-признака коммита.
FILE_HASHES = [
    "На NAS файл один, sha1 7ff1e2f6 тот же, владелец root.",
    "файл в зеркале в 16:34:39 с тем же sha1 7ff1e2f6.",
    "python3 с гардами sha1 (35abdbf9 → 2ea47807, бэкап /tmp/x.bak).",
    "sha1 после записи 0d160bb2 совпал с ожидаемым",
    "sha1 с нормализацией CRLF на обеих сторонах: локально 009f7cc0 / 76236 байт",
    "Боевые копии совпадают с шаблоном по md5: 103 = 3cdbf6a7, 105 = 3b67e0f8.",
    "healthz 200, agent-info sha256 bdecb075 — это прод.",
    "Резервный MSIX (266 539 079 байт, SHA256 f3925248..., подпись Valid)",
    "контрольная сумма архива 4e1f0a9 не изменилась",
]


@pytest.mark.parametrize("text", FILE_HASHES)
def test_file_hash_is_not_commit(text):
    assert commits(text) == []


def test_marker_before_commit_word_does_not_hide_hash_after_it():
    """Слово-признак коммита действует, только если стоит ПОСЛЕ маркера хэша:
    «хеш коммита X» — коммит, а в «коммит A (sha1 файла X)» X остаётся хэшем файла."""
    assert commits("коммит 1a2b3c4 (sha1 файла 7ff1e2f6 тот же)") == ["1a2b3c4"]


def test_sha1sum_output_line_is_not_commit():
    """Вывод sha1sum: полные 40 hex — ровно калибр sha1 коммита, отличает только форма
    строки «<hex>  путь» (в Git Bash — «<hex> *путь»)."""
    text = ("Сверка после деплоя:\n"
            "4d7a0c2e9b1f3a5c6e8d0b2f4a6c8e0d2f4b6a8c  memory_compiler/storage.py\n"
            "8e2b4d6f0a1c3e5b7d9f1a3c5e7b9d1f3a5c7e9b *memory_compiler/handlers.py\n")
    assert commits(text) == []


def test_git_log_oneline_is_commit():
    """Строка git log --oneline похожа на вывод sha1sum, но отделена ОДНИМ пробелом."""
    assert commits("Последние коммиты:\n9a1b2c3 fix: v1.92.4 — план\n") == ["9a1b2c3"]


# 12 hex — не калибр git этой базы: так git сокращает только в репозиториях от
# ~4 млн объектов. Зато это длина ревизии alembic и короткого ID docker.
TWELVE_HEX = [
    "Миграции 2d17a312bf9f (customers) и 52e95c466d23 (nullable).",
    "бэкап → checkout тега → build → alembic c12ea887d245 → d4e8a1f07b3c → up -d",
    "контейнер снова сменился (был 6b55c55a16a0, стал cdcec7a796d2)",
    "Последняя HEAD = a4b5c6d7e8f9 (add_client_legal_fields).",
]


@pytest.mark.parametrize("text", TWELVE_HEX)
def test_twelve_hex_is_not_commit(text):
    assert commits(text) == []


def test_commit_next_to_migration_survives_twelve_hex_rule():
    assert commits("Коммит 925d648 (feat: причина списания), миграция c9e5a1f4b6d2.") == ["925d648"]


def test_ed25519_is_not_commit():
    """Тип ключа — hex-подобное слово с цифрами, правило «без цифр» его не ловит."""
    assert commits("Генерировать ключи так: ssh-keygen -t ed25519 -N '' -f ~/.ssh/имя") == []
    assert commits("SSH-доступ к хосту настроен по ed25519 ключу") == []


# ID чужих систем: признак — слово ВПЛОТНУЮ перед hex. Окно в несколько слов резало
# настоящие коммиты: «модель/сервис/миграция, 04d8e02», «(за сессию F9): 0a4ae88».
FOREIGN_IDS = [
    "Сессия 42402cd6 от 15.09: 29 запусков, тоже по одному.",
    "отказ и удачный повтор — в транскрипте сессии 012a4eeb",
    "написан gate.py (scratchpad 9d2635ac) — сравнивает классы находок",
    "preflight.sh (скратчпад c1cb8c48): pf-start = main",
    "spawn_task fd02b6bb создан.",
    "платформа 8.5.1.1343, кластер 8168ddd9, ib 133cf21b, rphost 372bd296).",
    "раздел «Кроссовки» (XML_ID 942f6dc7), бренд 19188",
    "АРМ с клиентом (hostname-GUID 65cc3170) → HTTP:8080",
    "память контейнера (tobyxdd/hysteria:latest, image-id 10904bb).",
]


@pytest.mark.parametrize("text", FOREIGN_IDS)
def test_foreign_id_is_not_commit(text):
    assert commits(text) == []


def test_same_hash_as_commit_elsewhere_in_text_stays():
    """Хэш остаётся коммитом, если хоть одно его упоминание — коммит."""
    assert commits("sha1 7ff1e2f6 тот же.\nКоммит 7ff1e2f6 откатывает это.") == ["7ff1e2f6"]
