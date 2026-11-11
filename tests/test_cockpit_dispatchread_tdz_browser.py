"""Row 784: `dispatchRead` is assigned inside render() (dashboard_core.js) but declared `let` ~8000 lines later in the
same IIFE. A TDZ ReferenceError would fire only if render() ran before the declaration line executed. This drives the
real page in headless Chrome, with an error collector installed before any page script, through the first draw and
three redraws (the last with a write token, so the dispatch read runs), and asserts no ReferenceError reached the page."""
from __future__ import annotations

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

pytestmark = [needs_browser]

TOK = "T"
COLLECT = ("window.__errs = []; window.addEventListener('error', e => window.__errs.push(String(e.message)));"
           "window.addEventListener('unhandledrejection', e => window.__errs.push('rej:' + String(e.reason)));")


def test_render_never_touches_dispatch_read_before_its_declaration(tmp_path):
    _root, src, _ids = _install(tmp_path)
    steps = [
        {"init": COLLECT},
        {"until": "document.getElementById('board') && document.getElementById('board').children.length > 0", "ms": 20000},
        {"eval": "new Promise(r => setTimeout(r, 800)).then(() => 1)"},
        {"eval": "(async () => { for (let i = 0; i < 3; i++) { const v = await (await fetch('/substrate.json')).json();"
                 " window.LevainDashboard.render(v, {}); await new Promise(r => setTimeout(r, 300)); } return 1; })()"},
        {"eval": "JSON.stringify(window.__errs)"},
    ]
    with _serve(src, surface_tokens={"browser": TOK}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token={TOK}", steps)
    errs = out["values"][-1]
    assert "dispatchRead" not in errs and "before initialization" not in errs, errs
