"""Render the analytics panel with actual UI code and error-only API data."""
import json
import shutil
import subprocess

import pytest

from memory_compiler.ui import WEB_HTML


@pytest.mark.parametrize("lang,error_label,denominator", [
    ("ru", "Ошибок поиска", "от успешных поисков"),
    ("en", "Search errors", "of successful searches"),
])
def test_analytics_panel_displays_only_errors(lang, error_label, denominator):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to run UI behavior")
    i18n_start = WEB_HTML.index("var I18N=")
    i18n = WEB_HTML[i18n_start:WEB_HTML.index("};", i18n_start) + 2]
    start = WEB_HTML.index("async function loadAnalytics()")
    function = WEB_HTML[start:WEB_HTML.index("async function loadAudit()", start)]
    data = {"total_articles": 0, "total_tracked": 0, "never_accessed": [], "top_accessed": [],
            "quality": {"searches": 2, "search_errors": 2, "successful_searches": 0,
                        "misses": 0, "miss_rate": 0, "acted": 0, "act_rate": 0,
                        "chained": 0, "writes": 0}}
    script = i18n + "\n" + function + "\n" + (
        "const target={innerHTML:''}; const $=()=>target; const esc=x=>String(x);"
        "const t=k=>I18N[" + json.dumps(lang) + "][k]||k;"
        "const fetch=async()=>({json:async()=>(" + json.dumps(data) + ")});"
        "loadAnalytics().then(()=>console.log(target.innerHTML));")
    run = subprocess.run([node, "-e", script], capture_output=True, text=True,
                         encoding="utf-8", timeout=30)
    assert run.returncode == 0, run.stderr
    assert error_label + ": 2" in run.stdout
    assert denominator in run.stdout
    assert "NaN" not in run.stdout and "undefined" not in run.stdout
