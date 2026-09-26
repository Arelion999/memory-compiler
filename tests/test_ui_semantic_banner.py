"""Баннер состояния поиска по смыслу в Web UI (v1.95.0).

Свежая установка с пустым кешем моделей жила без него молча: поиск шёл только по словам,
и этого не видел никто. Поведение баннера проверяется исполнением настоящих функций
страницы в node со стабами DOM и t().
"""
import json
import re
import shutil
import subprocess

import pytest

from memory_compiler.ui import WEB_HTML


def _function(name):
    m = re.search(rf"function {name}\(d?\)\{{.*?\n\}}\n", WEB_HTML, re.S)
    assert m, f"в ui.py нет функции {name}"
    return m.group(0)


def test_banner_markup_is_hidden_by_default():
    assert '<div id="semantic-banner" class="sem-banner" style="display:none"></div>' in WEB_HTML


def test_health_handler_renders_banner():
    """F7 (final-fix-findings.md): срез на первом "});" попадает внутрь
    Object.keys(d.projects||{}) и обрубает хендлер до конца — проверка видела лишь
    самое начало тела. Срез до следующего top-level стейтмента "\\nconst $=" отдаёт
    ВЕСЬ callback, и тест реально видит его целиком (в т.ч. хвост с loadTags())."""
    start = WEB_HTML.index('fetch("/api/health").then(function(r){return r.json()})'
                           '.then(function(d){')
    handler = WEB_HTML[start:WEB_HTML.index("\nconst $=", start)]
    assert "renderSemantic(d)" in handler, "обработчик health обязан отрисовать баннер"
    assert "loadTags()" in handler, "срез обязан захватывать хендлер целиком, а не обрубок"


HARNESS = r"""
var el={textContent:"",className:"",style:{display:"none"}};
function $(id){return id==="semantic-banner"?el:null}
var TEXTS={"sem.off":"OFF","sem.offlineNoCache":"NOCACHE","sem.downloadFailed":"DL","sem.loadFailed":"LOAD","sem.firstDownload":"FIRST"};
function t(k){return TEXTS[k]||k}
var timers=[];
function setTimeout(f,ms){timers.push(ms)}
function fetch(){return {then:function(){return {then:function(){return {catch:function(){}}}}}}}
%s
%s
var out=[];
[%s].forEach(function(d){timers.length=0;renderSemantic(d);out.push([el.textContent,el.style.display,el.className,timers.length]);});
console.log(JSON.stringify(out));
"""

CASES = [
    ({"semantic": "off", "semantic_reason": "offline_no_cache"}, ["NOCACHE", "block", "sem-banner", 0]),
    ({"semantic": "off", "semantic_reason": "download_failed"}, ["DL", "block", "sem-banner", 0]),
    ({"semantic": "off", "semantic_reason": "load_failed"}, ["LOAD", "block", "sem-banner", 0]),
    ({"semantic": "off", "semantic_reason": "new_reason"}, ["OFF", "block", "sem-banner", 0]),
    ({"semantic": "loading", "semantic_reason": "first_download"},
     ["FIRST", "block", "sem-banner loading", 1]),
    ({"semantic": "loading", "semantic_reason": None}, ["", "none", "sem-banner", 0]),
    ({"semantic": "on", "semantic_reason": None}, ["", "none", "sem-banner", 0]),
    ({}, ["", "none", "sem-banner", 0]),  # сервер до v1.95.0: полей нет — баннера нет
]


def test_banner_behaviour_in_node(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node не установлен — поведение баннера не проверить")
    js = HARNESS % (_function("renderSemantic"), _function("pollSemantic"),
                    ",".join(json.dumps(d) for d, _ in CASES))
    script = tmp_path / "banner.js"
    script.write_text(js, encoding="utf-8")
    done = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [expected for _, expected in CASES]


# F4 (final-fix-findings.md): раньше pollSemantic.catch(function(){}) молча глотал
# сетевую ошибку и НЕ планировал повтор — баннер первой загрузки замирал навсегда,
# если хоть один health-запрос не удался. .catch должен сам ставить setTimeout.
POLL_FAIL_HARNESS = r"""
function renderSemantic(){}
var timers=[];
function setTimeout(f,ms){timers.push(ms)}
function fetch(){return {then:function(){return {then:function(){
  return {catch:function(cb){cb();return {}}}
}}}}}
%s
pollSemantic();
console.log(JSON.stringify(timers));
"""


def test_poll_semantic_retries_after_failed_fetch(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node не установлен — поведение баннера не проверить")
    js = POLL_FAIL_HARNESS % _function("pollSemantic")
    script = tmp_path / "poll_fail.js"
    script.write_text(js, encoding="utf-8")
    done = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [30000], \
        "неудачный fetch обязан планировать повтор через 30000 мс"
