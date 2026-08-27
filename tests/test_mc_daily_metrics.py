"""Cron-обёртка суточного замера (v1.73.0).

Скрипт исполняется по-настоящему, с подставным `curl` — как и тест ватчера с
подставным `docker`. Проверять bash пересказом на Python значило бы тестировать
пересказ.

⚠️ Замер дёргается у РАБОТАЮЩЕГО сервера. Первая редакция запускала второй
процесс python в контейнере: он переписывал бы pickle эмбеддингов мимо
внутрипроцессного лока, а на выходе падал с кодом 134 при успешно записанном
замере — то есть в логе успех выглядел провалом.

⚠️ REST закрыт `Authorization: Bearer`, и первый живой прогон это показал:
ответ был `{"error":"Unauthorized"}`. Ключ берётся из `.env` базы — в скрипте
его нет, в query он не идёт (утёк бы в access-логи), в лог не пишется.
"""

import os
import shutil
import subprocess

import pytest

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash недоступен")

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "scripts", "mc-daily-metrics.sh")

ENV_WITH_KEY = 'MC_ENCRYPT_KEY=x\nMC_API_KEY="секретный-ключ"\n'
ENV_WITHOUT_KEY = "MC_ENCRYPT_KEY=x\n"


@pytest.fixture
def env(tmp_path):
    calls = tmp_path / "curl-calls.log"

    def run(code=0, out='{"result": "Замер за последние 24 ч."}',
            env_file_content=ENV_WITH_KEY):
        fake = tmp_path / "curl"
        fake.write_text('#!/bin/bash\necho "$@" >> "%s"\necho \'%s\'\nexit %d\n'
                        % (calls.as_posix(), out, code),
                        encoding="utf-8", newline="\n")
        fake.chmod(0o755)

        envfile = tmp_path / ".env"
        envfile.write_text(env_file_content, encoding="utf-8", newline="\n")

        e = dict(os.environ)
        e.pop("MC_API_KEY", None)
        e.update({"CURL": fake.as_posix(),
                  "MC_METRICS_LOG": (tmp_path / "metrics.log").as_posix(),
                  "MC_METRICS_URL": "http://127.0.0.1:8765/api/metrics/daily",
                  "MC_ENV_FILE": envfile.as_posix(),
                  "MC_METRICS_HOURS": "24"})
        done = subprocess.run([BASH, SCRIPT], env=e, capture_output=True, text=True)
        log = tmp_path / "metrics.log"
        return done, (log.read_text(encoding="utf-8") if log.exists() else ""), \
            (calls.read_text(encoding="utf-8") if calls.exists() else "")

    return run


def test_asks_the_running_server_and_logs_the_result(env):
    done, log, calls = env()
    assert done.returncode == 0
    assert "/api/metrics/daily" in calls, "замер обязан считаться внутри сервера"
    assert "hours" in calls, "окно передаётся явно"
    assert "OK" in log and "Замер за последние" in log


def test_unreachable_server_is_reported_not_swallowed(env):
    """Пропуск обязан быть ВИДЕН: иначе в ряду замеров появится тихая дыра."""
    done, log, _ = env(code=7, out="curl: (7) Failed to connect")
    assert done.returncode == 7
    assert "FAIL" in log and "curl 7" in log


def test_answer_without_result_counts_as_failure(env):
    """HTTP-200 с ошибкой в теле — тоже провал: замера не случилось."""
    done, log, _ = env(out='{"error": "hours out of range"}')
    assert done.returncode == 1
    assert "FAIL" in log and "hours out of range" in log


def test_key_is_taken_from_env_file_and_sent_as_bearer(env):
    done, log, calls = env()
    assert done.returncode == 0
    assert "Bearer секретный-ключ" in calls, "ключ должен уехать заголовком"
    assert "key=" not in calls, "в query ключ не отправляем — утечёт в access-логи"


def test_key_never_lands_in_the_log(env):
    """Лог замера читают глазами и грепают — секрету там не место."""
    done, log, _ = env()
    assert "секретный-ключ" not in log


def test_missing_key_fails_loudly_without_calling_the_server(env):
    done, log, calls = env(env_file_content=ENV_WITHOUT_KEY)
    assert done.returncode == 2
    assert "MC_API_KEY" in log, "надо назвать, чего не хватает"
    assert calls.strip() == "", "без ключа сервер не дёргаем"
