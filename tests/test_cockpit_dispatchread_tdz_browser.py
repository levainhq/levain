"""Row 784: `dispatchRead` is assigned inside render() (dashboard_core.js) but declared `let` ~8000 lines later in the
same IIFE. A TDZ ReferenceError would fire only if render() ran before the declaration line executed. This drives the
real page in headless Chrome, with an error collector installed before any page script, through the first draw and
three redraws (the last with a write token, so the dispatch read runs), and asserts no ReferenceError reached the page.

The first draw runs inside boot's try/catch (dashboard_boot.js `load`), so an error thrown there never reaches the window
error collector: it is written to the page's `#stamp` line ("read failed: ...") and the page retries. The first-draw step
therefore also stops on that line and the test reads it, so a first-draw error goes red at once, with its message, rather
than as a 20 s timeout on an empty board."""
from __future__ import annotations

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

pytestmark = [needs_browser]

TOK = "T"
STAMP = "((document.getElementById('stamp') || {}).textContent || '')"
COLLECT = ("window.__errs = []; window.addEventListener('error', e => window.__errs.push(String(e.message)));"
           "window.addEventListener('unhandledrejection', e => window.__errs.push('rej:' + String(e.reason)));")


def test_render_never_touches_dispatch_read_before_its_declaration(tmp_path):
    _root, src, _ids = _install(tmp_path)
    steps = [
        {"init": COLLECT},
        {"until": "(document.getElementById('board') && document.getElementById('board').children.length > 0)"
                  f" || {STAMP}.startsWith('read failed')", "ms": 20000},
        {"eval": STAMP},
        {"eval": "new Promise(r => setTimeout(r, 800)).then(() => 1)"},
        {"eval": "(async () => { for (let i = 0; i < 3; i++) { const v = await (await fetch('/substrate.json')).json();"
                 " window.LevainDashboard.render(v, {}); await new Promise(r => setTimeout(r, 300)); } return 1; })()"},
        {"eval": "JSON.stringify(window.__errs)"},
    ]
    with _serve(src, surface_tokens={"browser": TOK}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token={TOK}", steps)
    first_draw, errs = out["values"][1], out["values"][-1]     # [0] is the until step, [1] the stamp line
    assert not first_draw.startswith("read failed"), first_draw      # a first-draw error is caught by boot, not collected
    assert "dispatchRead" not in errs and "before initialization" not in errs, errs
