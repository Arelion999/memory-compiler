"""Тяжёлые синхронные вызовы не должны выполняться в event loop.

Хендлеры MCP асинхронные. Если async-функция зовёт тяжёлую синхронную операцию
напрямую, event loop встаёт целиком: сервер перестаёт отвечать НА ВСЁ — и на другие
вызовы, и на health. У проекта это уже случалось: search падал в -32001, потому что
дёргал cross-encoder прямо в loop (лечилось asyncio.to_thread, 2026-07-03).

Замер 2026-07-20: `git add -A` на 1815 статьях — 5.5 с локально на SSD, на NAS
заметно дольше. Тринадцать хендлеров звали git_commit синхронно.

Проверка статическая, по AST: вызов внутри async-функции должен быть либо обёрнут
в to_thread, либо не быть в списке тяжёлых. `to_thread(git_commit, msg)` передаёт
функцию как аргумент — это ast.Name, а не ast.Call, поэтому под проверку не попадает.

Второй сторож — не по именам, а по графу вызовов: async-функция не вправе синхронно
дойти до захвата замка, который фон держит минутами (см. LONG_LOCKS ниже).
"""
import ast
from functools import lru_cache
from pathlib import Path

import pytest

MC = Path(__file__).resolve().parent.parent / "memory_compiler"
PKG = "memory_compiler"

# Функции, которые нельзя звать напрямую из event loop.
# git_commit — subprocess `git add -A` по всей базе знаний (тысячи файлов).
# regenerate_index / rebuild_* — полный обход и перезапись индекса.
# _build_graph — чтение ВСЕХ статей + матрица близостей по всей базе; в async-хендлере
# это вешало сервер на десятки секунд (v1.55.1), и список его тогда не знал: он
# ИМЕННОЙ, то есть новую тяжёлую функцию ловит только после внесения сюда. Заводишь
# такую — дописывай строку, иначе гейт про неё не знает.
HEAVY = {
    "git_commit",
    "regenerate_index",
    "rebuild_index",
    "rebuild_embeddings",
    "whoosh_search",
    "rerank",
    "_build_graph",
    "quality",
    # daily() читает тот же мегабайтный лог ДВАЖДЫ (свой проход плюс quality).
    # Пока её зовут из cron-обёртки, синхронно; список именной, поэтому вносим
    # заранее — если завтра её позовут из хендлера, сторож обязан поймать.
    "daily",
    "_scan_stale",
    # reflexes (v1.78.0): обход всей базы при пересборке индекса триггеров.
    "refresh_index",
    "find_memos",
    # search_by_tag (v1.91.0): обход всей базы ради тега — раньше шёл прямо на loop.
    "_scan_tag",
}


def blocking_calls(path: Path):
    """Тяжёлые вызовы внутри async-функций, не обёрнутые в to_thread."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines()
    async_fns = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]
    found = []
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        name = getattr(call.func, "id", None) or getattr(call.func, "attr", "")
        if name not in HEAVY:
            continue
        owner = None
        for fn in async_fns:
            if fn.lineno <= call.lineno <= fn.end_lineno:
                if owner is None or fn.lineno > owner.lineno:
                    owner = fn
        if owner is None:
            continue  # вызов из синхронной функции — там блокировать нечего
        if "to_thread" in lines[call.lineno - 1]:
            continue
        found.append(f"{path.name}:{call.lineno} в async {owner.name}() → {name}()")
    return found


@pytest.mark.parametrize("module", sorted(p.name for p in MC.glob("*.py")))
def test_no_heavy_calls_in_event_loop(module):
    """Тяжёлая синхронная операция в async-функции обязана уходить в to_thread."""
    found = blocking_calls(MC / module)
    assert not found, (
        "блокируют event loop:\n  " + "\n  ".join(found)
        + "\nОберни в await asyncio.to_thread(...)"
    )


# ─── Класс, а не имя: ожидание долгого замка на event loop ───────────────────
# Инцидент 25.09.2026: фоновый reindex держал _index_lock весь дисковый скан (~4 мин
# на NAS), а save_lesson ждал этот замок прямо на loop — через find_existing_article →
# snapshot_embeddings. /api/health молчал три минуты. HEAVY этого не видел: список
# именной и проверяет прямой вызов, а замок брался двумя вызовами глубже.
#
# Здесь проверка идёт по графу вызовов пакета: async-функция не вправе синхронно (не
# через to_thread) вызвать то, что ТРАНЗИТИВНО берёт долгий замок. Новая функция,
# берущая такой замок, и новый путь к ней подхватываются сами, без правки списков.
#
# Каждый замок пакета обязан быть классифицирован — новый без решения валит
# test_every_lock_is_classified. Долгий = его держат дольше, чем вправе стоять loop,
# неважно, как его берут сегодня.
LONG_LOCKS = {
    ("search", "_ix_lock"): "rebuild_index держит всю пересборку Whoosh (~4,5 мин на 4443 документах)",
    ("search", "_emb_lock"): "запись pickle эмбеддингов под замком — секунды на NAS",
    ("search", "_model_load_lock"): "прогрев держит, пока грузит модель (минуты на NAS)",
    ("search", "_reindex_lock"): "занят всё время фонового reindex",
    ("reflexes", "_lock"): "refresh_index держит весь обход базы",
}
SHORT_LOCKS = {
    ("embed_queue", "_lock"): "только операции со словарём очереди",
    ("obs", "_lock"): "только счётчики",
    ("search", "_ix_pending_lock"): "только решение «в очередь или сразу» и забор очереди",
}
# Доходят до долгого замка лишь на холодном старте, который lifespan проходит ДО
# приёма запросов; на работающем сервере это отдача готового объекта.
COLD_START_ONLY = {
    ("search", "get_index"): "строит индекс только при _ix is None, а его открывает "
                             "startup_prepare_index в lifespan",
}
# Лямбда-аргумент этих вызовов исполняется в другом потоке. Прочие аргументы
# вычисляются на loop: to_thread(f, g()) зовёт g() на loop и проверяется как обычно.
OFFLOAD = {"to_thread", "run_in_executor", "Thread", "submit"}
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _call_name(call) -> str:
    return getattr(call.func, "id", None) or getattr(call.func, "attr", "")


def _blocking_acquire(call) -> bool:
    """acquire() ждёт, если явно не сказано blocking=False."""
    flags = [*call.args[:1], *(k.value for k in call.keywords if k.arg == "blocking")]
    return not any(isinstance(a, ast.Constant) and a.value is False for a in flags)


class CallGraph:
    """Граф вызовов пакета: какие функции кто зовёт и какие замки берёт сам.

    Имена разрешаются так же, как их видит Python: вложенный def, модуль, импорт
    (в том числе отложенный внутри функции), атрибут модуля пакета, реэкспорт
    (handlers.save_lesson → handlers_articles.save_lesson). Вызовы через self и
    сторонние объекты не разрешаются — замки пакета так не берутся."""

    def __init__(self, sources: dict):
        trees = {m: ast.parse(src) for m, src in sources.items()}
        self.defs, self.aliases, self.imports, self.locks = {}, {}, {}, set()
        for m, tree in trees.items():
            self._symbols(m, tree)
        self.fns = {}   # (модуль, имя) -> {"async", "locks", "calls"}
        for m, tree in trees.items():
            for node in tree.body:
                if isinstance(node, _DEFS):
                    self._collect(m, node, node.name, [])
                elif isinstance(node, ast.ClassDef):
                    for sub in node.body:
                        if isinstance(sub, _DEFS):
                            self._collect(m, sub, f"{node.name}.{sub.name}", [])

    def _symbols(self, m, tree):
        defs, aliases, imports = {}, {}, {}
        for node in tree.body:
            if isinstance(node, _DEFS):
                defs[node.name] = (m, node.name)
            elif (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                    and _call_name(node.value) in ("Lock", "RLock")):
                self.locks.update((m, t.id) for t in node.targets if isinstance(t, ast.Name))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith(PKG + ".") and a.asname:
                        aliases[a.asname] = a.name.split(".", 1)[1]
            elif isinstance(node, ast.ImportFrom) and node.module == PKG:
                for a in node.names:
                    aliases[a.asname or a.name] = a.name
            elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(PKG + "."):
                for a in node.names:
                    imports[a.asname or a.name] = (node.module.split(".", 1)[1], a.name)
        self.defs[m], self.aliases[m], self.imports[m] = defs, aliases, imports

    def _follow(self, key):
        """Идти по реэкспортам до модуля, где имя определено."""
        for _ in range(10):
            m, name = key
            if (m not in self.defs or name in self.defs[m] or key in self.locks
                    or name not in self.imports[m]):
                return key
            key = self.imports[m][name]
        return key

    def _resolve(self, m, expr, scope):
        """Имя или атрибут модуля пакета → (модуль, имя); None — не наше."""
        if isinstance(expr, ast.Name):
            for frame in scope:
                if expr.id in frame:
                    return frame[expr.id]
            if expr.id in self.defs[m] or (m, expr.id) in self.locks:
                return (m, expr.id)
            return self._follow(self.imports[m][expr.id]) if expr.id in self.imports[m] else None
        if isinstance(expr, ast.Attribute):
            v = expr.value
            if isinstance(v, ast.Name) and v.id in self.aliases[m]:
                return self._follow((self.aliases[m][v.id], expr.attr))
            if isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name) and v.value.id == PKG:
                return self._follow((v.attr, expr.attr))
        return None

    @staticmethod
    def _own(body):
        """Узлы тела функции: без вложенных def/class (у них своё тело) и без
        лямбд, уходящих в поток через OFFLOAD."""
        stack = list(body)
        while stack:
            n = stack.pop()
            yield n
            if isinstance(n, (*_DEFS, ast.ClassDef)):
                continue
            if isinstance(n, ast.Call) and _call_name(n) in OFFLOAD:
                stack.append(n.func)
                stack.extend(a for a in [*n.args, *(k.value for k in n.keywords)]
                             if not isinstance(a, ast.Lambda))
                continue
            stack.extend(ast.iter_child_nodes(n))

    def _collect(self, m, node, qual, scope):
        info = {"async": isinstance(node, ast.AsyncFunctionDef), "locks": [], "calls": [],
                "held": []}
        self.fns[(m, qual)] = info
        own = list(self._own(node.body))
        nested = [n for n in own if isinstance(n, _DEFS)]
        scope = [{n.name: (m, f"{qual}.{n.name}") for n in nested}] + scope
        for n in nested:
            self._collect(m, n, f"{qual}.{n.name}", scope)
        for n in own:
            if isinstance(n, (ast.With, ast.AsyncWith)):
                for i, item in enumerate(n.items):
                    key = self._resolve(m, item.context_expr, scope)
                    if key in self.locks:
                        info["locks"].append((n.lineno, key))
                        info["held"].append((n.lineno, key,
                                             self._held_inside(m, n.items[i + 1:], n.body, scope)))
            elif isinstance(n, ast.Call):
                if (_call_name(n) == "acquire" and isinstance(n.func, ast.Attribute)
                        and _blocking_acquire(n)):
                    key = self._resolve(m, n.func.value, scope)
                    if key in self.locks:
                        info["locks"].append((n.lineno, key))
                key = self._resolve(m, n.func, scope)
                if key is not None:
                    info["calls"].append((n.lineno, key))
        info["calls"].sort()   # в сообщении — путь через первый по тексту вызов
        info["locks"].sort()

    def _held_inside(self, m, later, body, scope):
        """Что делается, пока держится замок: следующие элементы того же with (with a, b:
        берёт b, уже держа a), вызовы и захваты замков в теле блока."""
        exprs = [it.context_expr for it in later]
        inner = [("lock", k) for k in (self._resolve(m, e, scope) for e in exprs)
                 if k in self.locks]
        for sub in self._own(exprs + list(body)):
            if isinstance(sub, (ast.With, ast.AsyncWith)):
                for it in sub.items:
                    k = self._resolve(m, it.context_expr, scope)
                    if k in self.locks:
                        inner.append(("lock", k))
            elif isinstance(sub, ast.Call):
                if (_call_name(sub) == "acquire" and isinstance(sub.func, ast.Attribute)
                        and _blocking_acquire(sub)):
                    k = self._resolve(m, sub.func.value, scope)
                    if k in self.locks:
                        inner.append(("lock", k))
                k = self._resolve(m, sub.func, scope)
                if k is not None:
                    inner.append(("call", k))
        return inner

    def lock_reach(self) -> dict:
        """Функция → замки, которые она берёт сама или через синхронные вызовы."""
        reach = {k: {lk for _ln, lk in info["locks"]} for k, info in self.fns.items()}
        changed = True
        while changed:
            changed = False
            for key, info in self.fns.items():
                for _ln, callee in info["calls"]:
                    extra = reach.get(callee, set()) - reach[key]
                    if extra:
                        reach[key] |= extra
                        changed = True
        return reach

    def nested_holds(self) -> list:
        """(место, внешний замок, внутренний замок): внутренний берётся, пока держится
        внешний, — прямым with в теле блока или через вызов из него."""
        reach = self.lock_reach()
        out = []
        for (m, qual), info in sorted(self.fns.items()):
            for ln, outer, inner in info["held"]:
                for kind, key in inner:
                    locks = {key} if kind == "lock" else reach.get(key, set())
                    for lk in sorted(locks):
                        if lk != outer:
                            out.append((f"{m}.py:{ln} {qual}()", outer, lk))
        return out

    def lock_paths(self, long_locks, cold_start_only) -> dict:
        """sync-функция → цепочка вызовов до захвата долгого замка."""
        paths = {}
        for key, info in self.fns.items():
            held = [(ln, lock) for ln, lock in info["locks"] if lock in long_locks]
            if held and not info["async"] and key not in cold_start_only:
                paths[key] = [f"{key[0]}.{key[1]}:{held[0][0]} [{held[0][1][1]}]"]
        changed = True
        while changed:
            changed = False
            for key, info in self.fns.items():
                if info["async"] or key in paths or key in cold_start_only:
                    continue
                for ln, callee in info["calls"]:
                    if callee in paths:
                        paths[key] = [f"{key[0]}.{key[1]}:{ln}"] + paths[callee]
                        changed = True
                        break
        return paths

    def loop_lock_waits(self, long_locks, cold_start_only) -> list:
        """Места, где async-функция синхронно ждёт долгий замок."""
        paths = self.lock_paths(long_locks, cold_start_only)
        found = []
        for (m, qual), info in sorted(self.fns.items()):
            if not info["async"]:
                continue
            for ln, lock in info["locks"]:
                if lock in long_locks:
                    found.append(f"{m}.py:{ln} async {qual}() берёт {lock[1]} прямо на loop")
            for ln, callee in info["calls"]:
                if callee in paths:
                    found.append(f"{m}.py:{ln} async {qual}() → " + " → ".join(paths[callee]))
        return found


@lru_cache(maxsize=1)
def _package_graph() -> CallGraph:
    return CallGraph({p.stem: p.read_text(encoding="utf-8") for p in MC.glob("*.py")})


@pytest.mark.parametrize("module", sorted(p.stem for p in MC.glob("*.py")))
def test_no_lock_waits_in_event_loop(module):
    """async-функция не ждёт долгий замок на loop — ни прямо, ни через цепочку."""
    found = [f for f in _package_graph().loop_lock_waits(LONG_LOCKS, COLD_START_ONLY)
             if f.startswith(f"{module}.py:")]
    assert not found, (
        "ждут долгий замок прямо на event loop:\n  " + "\n  ".join(found)
        + "\nОберни вызов в await asyncio.to_thread(...): пока фон держит замок, "
          "loop стоит целиком — /api/health и все сессии"
    )


def test_every_lock_is_classified():
    """Новый замок без решения «долгий/короткий» валит тест; устаревшая запись тоже —
    иначе сторож молча охранял бы замок, которого больше нет."""
    graph = _package_graph()
    classified = set(LONG_LOCKS) | set(SHORT_LOCKS)
    assert graph.locks == classified, (
        f"не классифицированы: {sorted(graph.locks - classified)}; "
        f"устарели: {sorted(classified - graph.locks)}")
    stale = [k for k in COLD_START_ONLY if k not in graph.fns]
    assert not stale, f"COLD_START_ONLY ссылается на несуществующее: {stale}"



def _ix_resets(sources: dict) -> set:
    """Функции, обнуляющие глобальный индекс (_ix = None) — после такого get_index
    строит индекс заново и держит _ix_lock весь дисковый скан."""
    found = set()
    for m, src in sources.items():
        for fn in ast.walk(ast.parse(src)):
            if not isinstance(fn, _DEFS):
                continue
            for n in ast.walk(fn):
                if (isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
                        and n.value.value is None
                        and any(isinstance(t, ast.Name) and t.id == "_ix" for t in n.targets)):
                    found.add((m, fn.name))
    return found


def test_cold_start_only_holds():
    """COLD_START_ONLY снимает get_index со сторожа, потому что индекс строится только
    при _ix is None, а обнуляет его один startup_prepare_index (lifespan, до приёма
    запросов). Обнуление в другом месте — и /api/health пересоберёт индекс прямо на
    loop, а сторож промолчит. Позитивный контроль — на синтетическом коде."""
    sources = {p.stem: p.read_text(encoding="utf-8") for p in MC.glob("*.py")}
    assert _ix_resets(sources) == {("search", "startup_prepare_index")}, (
        "_ix = None вне startup_prepare_index: get_index перестал быть «только холодным "
        "стартом», исключение COLD_START_ONLY надо пересмотреть")
    control = {"search": "_ix = None\ndef quarantine():\n    global _ix\n    _ix = None\n"}
    assert _ix_resets(control) == {("search", "quarantine")}


def test_lock_guard_catches_known_shapes():
    """Позитивный контроль: сторож ловит все формы пути к замку и не ловит то,
    что действительно уходит с loop."""
    src = {
        "search": (
            "import threading\n"
            "_index_lock = threading.RLock()\n"
            "def snap():\n"
            "    with _index_lock:\n"
            "        return 1\n"
            "async def held():\n"
            "    with _index_lock:\n"
            "        pass\n"
            "async def try_only():\n"
            "    _index_lock.acquire(blocking=False)\n"
        ),
        "storage": "from memory_compiler.search import snap\ndef find():\n    return snap()\n",
        "handlers": (
            "import asyncio\n"
            "import memory_compiler.storage as st\n"
            "from memory_compiler.storage import find\n"
            "async def direct():\n    find()\n"
            "async def via_module():\n    st.find()\n"
            "async def eager_arg():\n    await asyncio.to_thread(print, find())\n"
            "async def nested_direct():\n"
            "    def inner():\n        return find()\n"
            "    inner()\n"
            "async def offloaded():\n    await asyncio.to_thread(find)\n"
            "async def lam():\n    await asyncio.to_thread(lambda: find())\n"
            "async def nested_offloaded():\n"
            "    def inner():\n        return find()\n"
            "    await asyncio.to_thread(inner)\n"
        ),
        "tools": "from memory_compiler.handlers import find\nasync def reexport():\n    find()\n",
    }
    lock = {("search", "_index_lock"): "тест"}
    found = CallGraph(src).loop_lock_waits(lock, {})
    flagged = {f.split(" async ", 1)[1].split("(", 1)[0] for f in found}
    assert flagged == {"held", "direct", "via_module", "eager_arg", "nested_direct",
                       "reexport"}, found


# ─── Разделение замков индекса (25.09.2026): никто не держит оба ────────────────
# Whoosh (_ix_lock) и эмбеддинги (_emb_lock) разведены по разным замкам, чтобы поиск не
# ждал пересборку Whoosh. Функция, берущая один, пока держит другой, задаёт порядок
# захвата — и взаимоблокировку, если соседний поток когда-нибудь возьмёт их в обратном.
# Последовательные вызовы (сначала один, отпустила, потом другой) — не нарушение.
INDEX_LOCKS = {("search", "_ix_lock"), ("search", "_emb_lock")}


def test_no_function_holds_both_index_locks():
    bad = [f for f in _package_graph().nested_holds() if {f[1], f[2]} == INDEX_LOCKS]
    assert not bad, "держат оба замка индекса разом:\n  " + "\n  ".join(
        f"{where}: держит {outer[1]}, берёт {inner[1]}" for where, outer, inner in bad)


def test_nested_hold_guard_catches_known_shapes():
    """Позитивный контроль: вложенный захват ловится прямым with, через вызов, вторым
    элементом того же with и через acquire(); последовательный — нет."""
    src = {"search": (
        "import threading\n"
        "_ix_lock = threading.RLock()\n"
        "_emb_lock = threading.RLock()\n"
        "def emb_part():\n"
        "    with _emb_lock:\n"
        "        return 1\n"
        "def nested_call():\n"
        "    with _ix_lock:\n"
        "        emb_part()\n"
        "def nested_with():\n"
        "    with _emb_lock:\n"
        "        with _ix_lock:\n"
        "            pass\n"
        "def sequential():\n"
        "    with _ix_lock:\n"
        "        pass\n"
        "    emb_part()\n"
        "def multi_with():\n"
        "    with _ix_lock, _emb_lock:\n"
        "        pass\n"
        "def nested_acquire():\n"
        "    with _emb_lock:\n"
        "        _ix_lock.acquire()\n"
        "        _ix_lock.release()\n"
    )}
    found = {f[0].split()[1] for f in CallGraph(src).nested_holds()
             if {f[1], f[2]} == INDEX_LOCKS}
    assert found == {"nested_call()", "nested_with()", "multi_with()", "nested_acquire()"}, found


def test_ix_pending_lock_is_never_held_while_taking_ix_lock():
    """Порядок захвата — всегда _ix_lock → _ix_pending_lock. Обратный (взять _ix_lock,
    держа короткий замок очереди) — взаимоблокировка с пересборкой, которая держит
    _ix_lock и ждёт _ix_pending_lock. Позитивный контроль — на синтетическом коде."""
    order = (("search", "_ix_pending_lock"), ("search", "_ix_lock"))

    def reversed_order(graph):
        return [f[0] for f in graph.nested_holds() if (f[1], f[2]) == order]

    found = reversed_order(_package_graph())
    assert not found, found
    control = {"search": (
        "import threading\n"
        "_ix_lock = threading.RLock()\n"
        "_ix_pending_lock = threading.Lock()\n"
        "def right():\n"
        "    with _ix_lock:\n"
        "        with _ix_pending_lock:\n"
        "            pass\n"
        "def wrong():\n"
        "    with _ix_pending_lock:\n"
        "        with _ix_lock:\n"
        "            pass\n"
    )}
    assert [w.split()[1] for w in reversed_order(CallGraph(control))] == ["wrong()"]


# ⚠️ ОБРАТНЫЙ СЛУЧАЙ, и он не симметричен списку выше. save_article_meta итерирует
# article_meta в json.dumps, а track_access мутирует тот же словарь из хендлеров: вынос
# записи в поток даёт 'dict changed size during iteration' — редкую порчу аналитики под
# параллельной нагрузкой. Правило записано в handlers._index_embed («НАМЕРЕННО остаются
# на loop»), но жило только комментарием: в v1.79.0 запись сайдкара из новой ручки
# /api/probe уехала в to_thread, и ни один тест этого не заметил — второго хендлера,
# который мутирует словарь, в тестах нет. Цена записи мала (dumps + атомарная запись
# файла), цена гонки — испорченный сайдкар.
# Туда же freshness._save_seen (v1.88.1): dumps по _seen, который мутируют соседние
# вызовы инструментов.
LOOP_ONLY = {"save_article_meta", "_save_seen"}


def thread_offloaded(path: Path):
    """Функции из LOOP_ONLY, уехавшие в to_thread."""
    found = []
    for call in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(call, ast.Call):
            continue
        if (getattr(call.func, "id", None) or getattr(call.func, "attr", "")) != "to_thread":
            continue
        # Первый аргумент to_thread — сама функция: ast.Name (имя) или ast.Attribute
        # (вызов через модуль, _cfg.save_article_meta).
        for arg in call.args[:1]:
            name = getattr(arg, "id", None) or getattr(arg, "attr", "")
            if name in LOOP_ONLY:
                found.append(f"{path.name}:{call.lineno} → to_thread({name})")
    return found


@pytest.mark.parametrize("module", sorted(p.name for p in MC.glob("*.py")))
def test_loop_only_functions_stay_on_loop(module):
    """Запись сайдкара обязана идти на loop: в потоке она гоняется с track_access."""
    found = thread_offloaded(MC / module)
    assert not found, (
        "уехало в поток, хотя обязано остаться на loop:\n  " + "\n  ".join(found)
        + "\nЗвать синхронно (см. handlers._index_embed про гонку с track_access)"
    )
