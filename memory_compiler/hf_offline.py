"""Офлайн-режим Hugging Face по состоянию локального кеша моделей (v1.95.0).

Зачем. docker-compose по умолчанию ставил HF_HUB_OFFLINE=1: после первой загрузки модели
библиотеки продолжали ходить на huggingface.co, и на сетях с rate-limit/firewall старт
падал с Errno 99 (v1.7.2). Но на СВЕЖЕЙ установке кеш пуст, модель скачать нельзя, и поиск
по смыслу молча выключался. Теперь при пустых HF_HUB_OFFLINE/TRANSFORMERS_OFFLINE сервер
решает сам: все нужные модели в кеше — офлайн, как раньше; чего-то нет — этот запуск
онлайн и скачивает модель один раз. Явное значение в .env не трогается.

⚠️ РЕШЕНИЕ ПРИНИМАЕТСЯ ДО ИМПОРТА ML-БИБЛИОТЕК (server.py). huggingface_hub читает
HF_HUB_OFFLINE один раз, при импорте (constants.HF_HUB_OFFLINE), а transformers 4.x ещё и
снимал свою копию. Переключать режим на ходу правкой чужих констант нельзя: версии
библиотек в requirements.txt сверху не закреплены.

⚠️ МОДУЛЬ НЕ ИМПОРТИРУЕТ huggingface_hub, transformers, sentence_transformers и torch —
иначе он сам заморозил бы режим до решения. Держит tests/test_first_start_guards.py.
Схема кеша повторена руками (models--org--name/refs/main → snapshots/<sha>/); сверка с
библиотекой — контрактный тест с huggingface_hub.try_to_load_from_cache.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from memory_compiler.config import DEFAULT_EMBED_MODEL, DEFAULT_RERANKER_MODEL, env_flag

# Истинные значения — как у huggingface_hub (constants._is_true) и transformers.
_TRUE_VALUES = {"1", "ON", "YES", "TRUE"}

# Организация по умолчанию для короткого имени без «/» — как у sentence-transformers 5.x
# (default_huggingface_organization у SentenceTransformer и CrossEncoder). Исключения ST
# (ORIGINAL_TRANSFORMER_MODELS: bert-base-uncased и т. п.) не повторяем: такое имя
# проверится под префиксом, будет считаться отсутствующим, и конфиг просто стартует онлайн.
_DEFAULT_ORG = {"embed": "sentence-transformers", "reranker": "cross-encoder"}

# Файлы весов, без которых снимок не считается скачанным: config.json без весов —
# оборванная первая загрузка (веса — больше 95% объёма модели).
_WEIGHT_PATTERNS = ("*.safetensors", "pytorch_model*.bin")

# Импорт любого из них до apply() замораживает офлайн-режим процесса.
_ML_MODULES = ("huggingface_hub", "transformers", "sentence_transformers")

_HINTS = {
    "offline_no_cache": (
        "модели {model} нет в локальном кеше, а офлайн-режим задан явно "
        "(HF_HUB_OFFLINE/TRANSFORMERS_OFFLINE). Поиск идёт только по словам. Чтобы "
        "включить: уберите обе переменные из .env и перезапустите сервер — модель "
        "скачается один раз."),
    "download_failed": (
        "не удалось скачать модель {model} с huggingface.co. Поиск идёт только по словам. "
        "Проверьте доступ к huggingface.co и перезапустите сервер."),
    "load_failed": (
        "модель {model} не загрузилась, подробности в логе сервера. Поиск идёт только по "
        "словам. Если кеш модели неполный после прерванной загрузки — удалите его (в "
        "Docker — том hf_cache, без Docker — каталог модели в ~/.cache/huggingface/hub) "
        "или поставьте HF_HUB_OFFLINE=0 на один запуск."),
    "first_download": (
        "идёт первая загрузка модели {model} с huggingface.co. Пока поиск идёт только по "
        "словам."),
}


@dataclass(frozen=True)
class Decision:
    """Решение процесса об офлайн-режиме HF.

    mode — auto_offline | auto_download | explicit_offline | explicit_online.
    missing — модели, которых нет в кеше (repo id, для локального пути — сам путь);
    заполняется во всех режимах: по нему пишется лог старта и узнаётся первая загрузка.
    """
    mode: str
    offline: bool
    missing: tuple[str, ...]
    cache_dir: str

    def as_dict(self) -> dict:
        return {"mode": self.mode, "offline": self.offline,
                "missing": list(self.missing), "cache_dir": self.cache_dir}


_decision: Decision | None = None


def _env(environ):
    return os.environ if environ is None else environ


def _is_true(value) -> bool:
    return (value or "").upper() in _TRUE_VALUES


def effective_offline(environ=None) -> bool:
    """Офлайн ли процесс по env — формула huggingface_hub 1.x:
    _is_true(HF_HUB_OFFLINE or TRANSFORMERS_OFFLINE)."""
    env = _env(environ)
    return _is_true(env.get("HF_HUB_OFFLINE") or env.get("TRANSFORMERS_OFFLINE"))


def _explicit(environ) -> bool:
    """Задан ли режим явно: хоть одна из двух переменных — непустая строка."""
    return bool(environ.get("HF_HUB_OFFLINE") or environ.get("TRANSFORMERS_OFFLINE"))


def hub_cache_dir(environ=None) -> Path:
    """Каталог кеша, куда sentence-transformers кладёт модели.

    SENTENCE_TRANSFORMERS_HOME ST передаёт библиотеке как cache_dir; иначе — порядок
    huggingface_hub: HF_HUB_CACHE → HUGGINGFACE_HUB_CACHE → HF_HOME/hub →
    XDG_CACHE_HOME/huggingface/hub → ~/.cache/huggingface/hub.
    """
    env = _env(environ)

    def norm(p: str) -> Path:
        return Path(os.path.expandvars(os.path.expanduser(p)))

    for var in ("SENTENCE_TRANSFORMERS_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        if env.get(var):
            return norm(env[var])
    if env.get("HF_HOME"):
        return norm(env["HF_HOME"]) / "hub"
    xdg = env.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return norm(xdg) / "huggingface" / "hub"


def is_local_path(name: str) -> bool:
    """Имя модели — путь на диске, а не repo id.

    Как у sentence-transformers: существующий путь грузится с диска; «\\» или больше
    одного «/» — путь, даже несуществующий. Плюс явные префиксы и буква диска."""
    if not name:
        return False
    if os.path.exists(os.path.expanduser(name)):
        return True
    if name.startswith(("/", "./", "../", "~", ".\\", "..\\")):
        return True
    if "\\" in name or name.count("/") > 1:
        return True
    return len(name) > 1 and name[1] == ":"


def repo_id(name: str, kind: str = "embed") -> str:
    """repo id на huggingface.co: короткое имя получает организацию по умолчанию ST."""
    if "/" in name:
        return name
    return f"{_DEFAULT_ORG.get(kind, _DEFAULT_ORG['embed'])}/{name}"


def _label(name: str, kind: str) -> str:
    """Как модель называется в missing, логе и подсказках."""
    return name if is_local_path(name) else repo_id(name, kind)


def is_cached(name: str, kind: str = "embed", environ=None) -> bool:
    """Лежит ли модель в локальном кеше целиком, то есть можно ли грузить её офлайн."""
    if is_local_path(name):
        return Path(os.path.expanduser(name)).is_dir()
    repo = hub_cache_dir(environ) / ("models--" + repo_id(name, kind).replace("/", "--"))
    try:
        sha = (repo / "refs" / "main").read_text(encoding="utf-8").strip()
    except OSError:
        return False  # без refs/main офлайн-загрузка hub тоже не найдёт ревизию main
    snap = repo / "snapshots" / sha
    if not sha or not (snap / "config.json").is_file():
        return False
    if not any(p.is_file() for pattern in _WEIGHT_PATTERNS for p in snap.rglob(pattern)):
        return False
    blobs = repo / "blobs"
    return not (blobs.is_dir() and any(blobs.glob("*.incomplete")))


def required_models(environ=None) -> list[tuple[str, str]]:
    """Модели этого процесса: embed всегда, reranker — при RERANK_ENABLED.

    Имена читаются так же, как в search.py: env с умолчанием из config."""
    env = _env(environ)
    models = [(env.get("EMBED_MODEL", DEFAULT_EMBED_MODEL), "embed")]
    if env_flag("RERANK_ENABLED", env):
        models.append((env.get("RERANKER_MODEL", DEFAULT_RERANKER_MODEL), "reranker"))
    return models


def decide(environ=None) -> Decision:
    """Решение по env и кешу. Чистая функция: env не меняет."""
    env = _env(environ)
    cache = str(hub_cache_dir(env))
    missing = tuple(_label(n, k) for n, k in required_models(env)
                    if not is_cached(n, k, env))
    if _explicit(env):
        offline = effective_offline(env)
        return Decision("explicit_offline" if offline else "explicit_online",
                        offline, missing, cache)
    if missing:
        return Decision("auto_download", False, missing, cache)
    return Decision("auto_offline", True, missing, cache)


def apply(environ=None) -> Decision | None:
    """Принять решение и применить его к env процесса. Звать ДО импорта ML-библиотек.

    env меняется только в режиме auto_offline: обе переменные становятся «1». Сбой
    проверки кеша старт не роняет: env остаётся как есть (пустые переменные — онлайн,
    как до v1.95.0), решения нет."""
    global _decision
    env = _env(environ)
    try:
        decision = decide(env)
    except Exception as e:  # noqa: BLE001 — старт сервера важнее решения о режиме
        print(f"[hf] ⚠️ не удалось проверить кеш моделей ({e}) — режим HF оставлен как есть")
        return None
    if decision.mode == "auto_offline":
        env["HF_HUB_OFFLINE"] = "1"
        env["TRANSFORMERS_OFFLINE"] = "1"
        late = [m for m in _ML_MODULES if m in sys.modules]
        if late:
            print(f"[hf] ⚠️ решение принято после импорта {', '.join(late)} — "
                  "на этот процесс оно не подействует")
    _decision = decision
    return decision


def current() -> Decision | None:
    """Решение этого процесса; None — apply() не вызывался (тесты, скрипты)."""
    return _decision


def first_download_pending(name: str, kind: str = "embed") -> bool:
    """Этот запуск впервые качает модель: режим auto_download и модель в missing."""
    d = _decision
    return d is not None and d.mode == "auto_download" and _label(name, kind) in d.missing


def failure_reason(name: str, kind: str = "embed", environ=None) -> str:
    """Код причины отказа загрузки — по кешу и режиму, а не по типу исключения: типы
    исключений разные в разных версиях библиотек."""
    env = _env(environ)
    if is_local_path(name) or is_cached(name, kind, env):
        return "load_failed"
    return "offline_no_cache" if effective_offline(env) else "download_failed"


def hint(reason: str, name: str, kind: str = "embed") -> str:
    """Что случилось и что делать — для лога старта и notice в выдаче search."""
    return _HINTS.get(reason, _HINTS["load_failed"]).format(model=_label(name, kind))


def describe(decision: Decision) -> str:
    """Строка лога старта о выбранном режиме."""
    missing = ", ".join(decision.missing)
    if decision.mode == "auto_offline":
        return "HF: модели в локальном кеше — работаю офлайн, без обращений к huggingface.co"
    if decision.mode == "auto_download":
        return (f"HF: первый запуск — в кеше нет {missing}. Скачиваю один раз с "
                "huggingface.co; следующий старт пойдёт офлайн")
    if decision.mode == "explicit_offline":
        if decision.missing:
            return (f"HF: офлайн задан явно, а в кеше нет {missing} — поиск по смыслу будет "
                    "выключен. Уберите HF_HUB_OFFLINE и TRANSFORMERS_OFFLINE из .env — "
                    "сервер скачает модель один раз")
        return "HF: офлайн задан явно, модели в кеше"
    return "HF: онлайн задан явно — модели сверяются с huggingface.co на каждом старте"
