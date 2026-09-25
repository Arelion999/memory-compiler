"""scripts/response_size_usage.py: состав ответов базы по транскриптам Claude Code."""
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "response_size_usage.py"
NOW = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
P = "mcp__memory-compiler__"

READ = ("# Статья\n\n## Записи\n\n### 2026-09-01 10:00\nтекст\n\n"
        "## См. также\n- [Сосед](../p/s.md) (2026-08-01)\n\n"
        "### 2026-09-02 11:00\nзапись после раздела\n\n"
        "## Git-ссылки\n**Коммиты:** abc1234")
START = ("# Контекст для: брокер\n\n"
         "## Найдено (1 релевантных, hybrid)\n### [p] Выбор брокера (hybrid: 90)\n# Выбор брокера\nтело\n\n"
         "## Решения по теме\n- **Выбор брокера** — Mosquitto\n")


def _load():
    spec = importlib.util.spec_from_file_location("response_size_usage", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _use(tid, tool, sidechain=False):
    return {"type": "assistant", "timestamp": NOW, "isSidechain": sidechain,
            "message": {"id": "m" + tid,
                        "content": [{"type": "tool_use", "id": tid, "name": P + tool, "input": {}}]}}


def _res(tid, *texts, sidechain=False):
    return {"type": "user", "timestamp": NOW, "isSidechain": sidechain, "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid,
         "content": [{"type": "text", "text": t} for t in texts]}]}}


def _write(tmp_path, records):
    f = tmp_path / "s.jsonl"
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")
    return [str(f)]


def test_read_article_sections_use_the_double_boundary(tmp_path):
    mod = _load()
    ra = mod.measure(_write(tmp_path, [_use("t1", "read_article"), _res("t1", READ)]))["read_article"]
    assert ra["see_also"] == len("## См. также\n- [Сосед](../p/s.md) (2026-08-01)\n\n"), (
        "запись ### после раздела посчитана как его часть")
    assert ra["git_links"] == len("## Git-ссылки\n**Коммиты:** abc1234\n")


def test_start_task_repeat_and_title_duplicate(tmp_path):
    mod = _load()
    st = mod.measure(_write(tmp_path, [_use("t2", "start_task"), _res("t2", START)]))["start_task"]
    assert st == {"n": 1, "with_repeat": 1, "repeats": 1,
                  "title_dup_chars": len("Выбор брокера") + 3,
                  "with_trace": 0, "traces": 0}


def test_start_task_header_without_score_is_parsed(tmp_path):
    """С v1.91.0 оценки в заголовке нет — замер «после» обязан находить находки."""
    mod = _load()
    new = START.replace(" (hybrid: 90)", "")
    st = mod.measure(_write(tmp_path, [_use("t6", "start_task"), _res("t6", new)]))["start_task"]
    assert st["repeats"] == 1, "без оценки в заголовке находка не распознана"


def test_search_by_tag_counts_lines_and_links(tmp_path):
    mod = _load()
    recs = [_use("t3", "search_by_tag"),
            _res("t3", "# Тег: x\n---\n### [p] A\na.md\n", "[Resource link: p/a.md] memory://p/a.md")]
    bt = mod.measure(_write(tmp_path, recs))["search_by_tag"]
    assert bt["lines"] == 1 and bt["links"] == 1


def test_start_task_traces_are_counted(tmp_path):
    """v1.92.0: повторный start_task в том же чате показывает виденное следом «… ↺»."""
    mod = _load()
    text = ("# Контекст для: брокер\n\n*↺ — уже было в этом чате за последние 3 ч, здесь "
            "только начало. Целиком: `open_questions`, `load_session`, `read_article`.*\n\n"
            "## Открытые вопросы (p)\n- **2026-09-25 10:00** — Какой брокер… ↺\n"
            "- **2026-09-25 11:00** — Новый вопрос\n")
    st = mod.measure(_write(tmp_path, [_use("t7", "start_task"), _res("t7", text)]))["start_task"]
    assert st["with_trace"] == 1 and st["traces"] == 1


def test_duplicates_and_sidechains_are_not_counted(tmp_path):
    mod = _load()
    note = "\n\n📌 **Первое обращение к `p` в этой сессии.** Контекст не загружался:\n- вопрос"
    recs = [_use("t4", "read_article"), _res("t4", "# A" + note), _res("t4", "# A" + note),
            _use("t5", "search", sidechain=True), _res("t5", "{}", sidechain=True)]
    r = mod.measure(_write(tmp_path, recs))
    assert r["first_touch"]["n"] == 1, "дубль tool_use_id посчитан дважды"
    assert "search" not in r["tools"], "реплика сабагента попала в замер"
