"""Render the analytics panel with actual UI code and representative API data."""
import json
import shutil
import subprocess

import pytest

from memory_compiler.ui import WEB_HTML


@pytest.mark.parametrize("lang,error_label,miss_label,action_label", [
    ("ru", "Ошибок поиска", "Впустую (от успешных поисков)", "Чтение/запись в том же чате"),
    ("en", "Search errors", "Empty (of successful searches)", "Read or write in the same chat"),
])
@pytest.mark.parametrize("successful,attributed,miss_display,action_display", [
    (0, 0, "0 (—)", "—"),
    (4, 0, "1 (25%)", "—"),
    (4, 2, "1 (25%)", "1 (50%)"),
])
def test_analytics_panel_displays_rates_with_their_sample(
        lang, error_label, miss_label, action_label, successful, attributed, miss_display, action_display):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to run UI behavior")
    i18n_start = WEB_HTML.index("var I18N=")
    i18n = WEB_HTML[i18n_start:WEB_HTML.index("};", i18n_start) + 2]
    start = WEB_HTML.index("async function loadAnalytics()")
    function = WEB_HTML[start:WEB_HTML.index("async function loadAudit()", start)]
    data = {"total_articles": 0, "total_tracked": 0, "never_accessed": [], "top_accessed": [],
            "quality": {"searches": 2 + successful, "search_errors": 2, "successful_searches": successful,
                        "misses": int(bool(successful)), "miss_rate": 0.25 if successful else 0,
                        "acted": int(bool(attributed)), "act_rate": 0.5 if attributed else 0,
                        "attributed_searches": attributed,
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
    assert miss_label + ": " + miss_display in run.stdout
    assert action_label + ": " + action_display in run.stdout
    assert "NaN" not in run.stdout and "undefined" not in run.stdout
