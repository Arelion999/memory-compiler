"""Служебные файлы сервера вне истории базы (v1.88.1).

15.09.2026: структурный лог logs/app.jsonl отслеживался git'ом базы и попадал в 74%
коммитов (4144 из 5625 с 16.07). Каждый git add -A заново сжимал многомегабайтный
лог, рыхлых объектов копилось 24–59 МБ в сутки. Тот же класс, что _audit.log
(v1.59.1) и .article_meta.json (14.09.2026): .gitignore на проде правили руками, а
git_init писал его только при первой инициализации, двумя строками. Для уже
отслеживаемого файла одного .gitignore мало — нужен git rm --cached.

Тесты гоняют НАСТОЯЩИЙ git: суть в том, как он обходится с отслеживаемыми и
игнорируемыми файлами, а подменённый subprocess это поведение не проверит.
"""

import shutil
import subprocess

import pytest

from memory_compiler import storage

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git не установлен")

SERVICE = ("logs/app.jsonl", ".article_meta.json", "_audit.log")
JOURNAL = "# Project journal: logs\n\nAppend-only log of knowledge-shaping events.\n\n- [2026-07-19 03:00] **lint** — 0 issues, 0 fixed\n"


def _git(kd, *args):
    return subprocess.run(["git", *args], cwd=str(kd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def _tracked(kd):
    return {p for p in _git(kd, "ls-files", "-z").stdout.split("\0") if p}


def _old_base(kd):
    """База, какой её оставила прежняя версия: короткий .gitignore с ручной строкой,
    служебные файлы и журнал-артефакт logs/_log.md в истории."""
    _git(kd, "init", "-q")
    _git(kd, "config", "user.email", "test@example.com")
    _git(kd, "config", "user.name", "test")
    (kd / ".gitignore").write_text(".whoosh_index/\n.embeddings.pkl\n*Conflict*\n", encoding="utf-8")
    (kd / "logs").mkdir(exist_ok=True)
    (kd / "logs" / "app.jsonl").write_text('{"msg": "tool ok"}\n', encoding="utf-8")
    (kd / "logs" / "_log.md").write_text(JOURNAL, encoding="utf-8")
    (kd / ".article_meta.json").write_text("{}", encoding="utf-8")
    (kd / "_audit.log").write_text("{}\n", encoding="utf-8")
    # статья, чьё имя ловит ручной шаблон: в истории она оказалась до появления шаблона
    (kd / "general" / "synologydrive_conflict-copies.md").write_text("# Копии конфликтов\n", encoding="utf-8")
    _git(kd, "add", "-A", "-f")
    _git(kd, "commit", "-q", "-m", "старая база")


def test_fresh_base_keeps_service_files_out_of_history(knowledge_dir):
    kd = knowledge_dir
    storage.git_init()
    (kd / "logs").mkdir(exist_ok=True)
    for path in SERVICE + ("logs/app.jsonl.1",):
        (kd / path).write_text("{}\n", encoding="utf-8")
    storage.git_commit("запись")
    tracked = _tracked(kd)
    assert not set(SERVICE + ("logs/app.jsonl.1",)) & tracked, tracked
    assert "testproj/test_article.md" in tracked, "статья обязана попасть в историю"


def test_old_base_untracks_service_files_and_keeps_them_on_disk(knowledge_dir):
    kd = knowledge_dir
    _old_base(kd)
    storage.git_init()
    tracked = _tracked(kd)
    for path in SERVICE:
        assert path not in tracked, path + " остался в истории"
        assert (kd / path).exists(), path + " пропал с диска, а сервер им пользуется"
    # следующий git add -A их не вернёт
    (kd / "logs" / "app.jsonl").write_text('{"msg": "ещё строка"}\n', encoding="utf-8")
    storage.git_commit("запись")
    assert "logs/app.jsonl" not in _tracked(kd)
    # позитивный контроль: снимаются ТОЧНЫЕ служебные пути, статьи остаются в истории,
    # в том числе та, чьё имя ловит ручной шаблон *Conflict*
    assert {"testproj/test_article.md", "general/synologydrive_conflict-copies.md"} <= _tracked(kd)


def test_gitignore_gets_missing_patterns_once_and_keeps_manual_lines(knowledge_dir):
    kd = knowledge_dir
    _old_base(kd)
    storage.git_init()
    lines = (kd / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines[:3] == [".whoosh_index/", ".embeddings.pkl", "*Conflict*"], "ручные строки тронуты"
    for pattern in ("logs/app.jsonl*", ".article_meta.json", "_audit.log"):
        assert lines.count(pattern) == 1, pattern
    head = _git(kd, "rev-parse", "HEAD").stdout
    storage.git_init()                                       # второй старт сервера
    assert (kd / ".gitignore").read_text(encoding="utf-8").splitlines() == lines, "шаблоны дописаны повторно"
    assert _git(kd, "rev-parse", "HEAD").stdout == head, "второй старт сделал лишний коммит"


def test_stray_logs_journal_is_removed(knowledge_dir):
    kd = knowledge_dir
    _old_base(kd)
    storage.git_init()
    assert not (kd / "logs" / "_log.md").exists(), "журнал-артефакт остался на диске"
    assert "logs/_log.md" not in _tracked(kd), "журнал-артефакт остался в истории"


def test_foreign_file_named_like_the_journal_is_kept(knowledge_dir):
    """Удаляется только журнал с узнаваемой шапкой, чужой файл с тем же именем — нет."""
    kd = knowledge_dir
    _old_base(kd)
    (kd / "logs" / "_log.md").write_text("# Мои заметки\n", encoding="utf-8")
    _git(kd, "commit", "-q", "-a", "-m", "свой файл")
    storage.git_init()
    assert (kd / "logs" / "_log.md").exists()
    assert "logs/_log.md" in _tracked(kd)


def test_startup_reports_what_was_tidied(knowledge_dir, capsys):
    """Уборка меняет git базы на каждом старте прода — результат обязан быть виден в логе
    контейнера, иначе проверить её можно только руками на NAS (ревью 15.09.2026)."""
    _old_base(knowledge_dir)
    storage.git_init()
    out = capsys.readouterr().out
    assert "logs/app.jsonl" in out and "logs/_log.md" in out, out
    storage.git_init()                                       # второй старт: делать нечего — молчим
    assert capsys.readouterr().out == ""


def test_locked_index_is_reported_and_finished_on_next_start(knowledge_dir):
    """Параллельный git держит index.lock: уборка не падает, честно сообщает о сбое,
    а следующий старт доделывает."""
    kd = knowledge_dir
    _old_base(kd)
    lock = kd / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    done = storage.tidy_service_files()
    assert any(item.startswith("!") for item in done), done
    assert "logs/app.jsonl" in _tracked(kd), "при занятом индексе снять с учёта было нельзя"
    lock.unlink()
    done = storage.tidy_service_files()
    assert not any(item.startswith("!") for item in done), done
    assert "logs/app.jsonl" not in _tracked(kd)


def test_tidy_commit_does_not_sweep_unrelated_changes(knowledge_dir):
    """Уборка коммитит только свои изменения: незакоммиченная правка статьи уйдёт
    со следующей записью, а не под сообщением про служебные файлы."""
    kd = knowledge_dir
    _old_base(kd)
    (kd / "testproj" / "test_article.md").write_text("# Test Article\n\nправка без коммита\n", encoding="utf-8")
    storage.git_init()
    changed = _git(kd, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert "logs/app.jsonl" in changed, "уборка не закоммитила снятие с учёта"
    assert "testproj/test_article.md" not in changed, "уборка прихватила чужую правку"
