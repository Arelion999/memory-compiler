"""Git-обслуживание knowledge-репо: путь записи не должен порождать сирот.

Регрессия 2026-07-28 (NAS): контейнер копил ~100 `[git] <defunct>` в сутки — ровно
по одному на коммит. git 2.47 запускает из `git commit` фоновое `git maintenance
run --auto` СРАЗУ отсоединённым (fork+setsid; maintenance.autoDetach по умолчанию
true), не проверяя заранее, нужна ли работа. Отсоединённый процесс терял родителя
и переусыновлялся к PID 1 контейнера — то есть к самому серверу, который сирот не
реапит. Лечение двустороннее: `init: true` в compose (PID 1 = tini, реапит любых
сирот) и gc.auto=0 на коммите — форка нет вовсе. Упаковку взамен git делает
git_gc() из фонового цикла (api.auto_gc_loop), и ей autoDetach=false обязателен:
иначе отсоединение вернётся тем же путём.
"""
import memory_compiler.storage as st


def _spy(monkeypatch):
    """Перехват subprocess.run в storage. returncode=1 — «есть staged-изменения»,
    без этого git_commit не дойдёт до самого commit."""
    calls = []

    class Result:
        returncode = 1

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return Result()

    monkeypatch.setattr(st.subprocess, "run", fake_run)
    return calls


def test_commit_disables_auto_maintenance(monkeypatch):
    """`git commit` идёт с gc.auto=0 — иначе git форкает отсоединённое обслуживание."""
    calls = _spy(monkeypatch)
    st.git_commit("save: тема")

    commits = [cmd for cmd, _ in calls if "commit" in cmd]
    assert commits, "commit не вызван при наличии staged-изменений"
    assert commits[0][:3] == ["git", "-c", "gc.auto=0"], commits[0]
    # -c обязан стоять ДО подкоманды: `git commit -c ...` — это совсем другое
    # (переиспользование сообщения коммита), git молча сделает не то.
    assert commits[0][3] == "commit", commits[0]


def test_gc_packs_without_detaching(monkeypatch):
    """git_gc на лёгком репозитории отдаёт решение git и НЕ отсоединяется.

    Команду ищем среди вызовов, а не берём первую: с v1.59.1 перед gc идёт
    `git count-objects -v` — вес рыхлых объектов решает, звать ли полную упаковку
    (порог `--auto` считает штуки и на крупных блобах не срабатывал).
    """
    calls = _spy(monkeypatch)
    st.git_gc()

    gc_calls = [(cmd, kw) for cmd, kw in calls if "gc" in cmd]
    assert gc_calls, "gc не вызван вовсе: %s" % [c for c, _ in calls]
    cmd, kwargs = gc_calls[-1]
    assert "--auto" in cmd, cmd          # спай не отдаёт вес → репозиторий считаем лёгким
    assert "gc.autoDetach=false" in cmd, cmd
    assert kwargs.get("timeout"), "нужен таймаут: полная упаковка идёт минутами"
