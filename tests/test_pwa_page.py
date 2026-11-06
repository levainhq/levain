"""PWA package, page half (levain 27): resume after a background/foreground, and the update prompt.

RESUME: iOS freezes a backgrounded page and kills its sockets; a read that was in flight then never settles. The first
board read held `inflight` forever, so every later read was dropped and the cold-start "connecting…" line never left.
The drive hangs the first manifest read (as a killed socket does), foregrounds the page, and expects the board drawn.
UPDATE: a stub registration with a waiting worker shows the bar; the tap posts levain-skip-waiting; one reload."""

from __future__ import annotations

from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_scroll_keep_page import _serve_plain

# the first manifest read never settles (the socket was killed under the frozen page); every later one goes through
HANG_FIRST = ("(() => { const f = window.fetch.bind(window); let n = 0;"
              " window.fetch = function (u, o) { if (String(u).indexOf('/cockpit/manifest.json') === 0 && n++ === 0)"
              " return new Promise(() => {}); return f(u, o); }; })()")
BACKGROUND = ("(() => { Object.defineProperty(document, 'hidden', { value: true, configurable: true });"
              " document.dispatchEvent(new Event('visibilitychange')); return 1; })()")
FOREGROUND = ("(() => { Object.defineProperty(document, 'hidden', { value: false, configurable: true });"
              " document.dispatchEvent(new Event('visibilitychange'));"
              " window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: false })); return 1; })()")
DRAWN = "document.querySelector('#board') && document.querySelector('#board').children.length > 0 && 1"


@needs_browser
def test_a_foreground_after_a_dead_first_read_repaints_the_board(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve_plain(src) as url:
        out = _drive(tmp_path, f"{url}/", [
            {"init": HANG_FIRST},
            {"eval": "new Promise(r => setTimeout(() => r([!!document.querySelector('#store[data-cold]'),"
                     " document.querySelector('#board').children.length]), 1500))"},
            {"eval": BACKGROUND},
            {"eval": FOREGROUND},
            {"until": DRAWN, "ms": 10000},
            {"until": "!document.querySelector('#store[data-cold]') && 1", "ms": 10000},
        ])
    cold, board = out["values"][0]
    assert cold and board == 0, f"the dead read must leave the page cold, or this proves nothing: {out['values']}"


# the first manifest read FAILS (a radio still waking: the fetch rejects at once), later ones go through
FAIL_FIRST = ("(() => { const f = window.fetch.bind(window); let n = 0;"
              " window.fetch = function (u, o) { if (String(u).indexOf('/cockpit/manifest.json') === 0 && n++ === 0)"
              " return Promise.reject(new TypeError('Failed to fetch')); return f(u, o); }; })()")


@needs_browser
def test_a_failed_first_read_says_so_and_retries_by_itself_without_a_foreground(tmp_path: Path) -> None:
    """Phill 11-06: both phones sat on the cold "connecting…" line with no panels. A hung read was fixed (above); a FAILED one left
    the line and nothing to try again. It now names the failure in the line and re-reads on a backoff until a board is drawn."""
    _root, src, _ids = _install(tmp_path)
    with _serve_plain(src) as url:
        out = _drive(tmp_path, f"{url}/", [
            {"init": FAIL_FIRST},
            {"until": "document.querySelector('#store') && /trying again/.test(document.querySelector('#store').textContent) && 1", "ms": 8000},
            {"until": DRAWN, "ms": 12000},
        ])
    assert out["values"][-1], out["values"]


# a registration whose worker is waiting, with a controller already in charge (an update, not a first install)
STUB = ("(() => { const posts = []; const l = {}; const w = { postMessage: (m) => posts.push(m) };"
        " const reg = { waiting: w, addEventListener() {}, update: () => Promise.resolve() };"
        " const sw = { controller: {}, register: () => Promise.resolve(reg),"
        "   addEventListener: (e, f) => { l[e] = f; } };"
        " Object.defineProperty(navigator, 'serviceWorker', { value: sw, configurable: true });"
        " window.__posts = posts; window.__reloads = 0; window.__sw = l; })()")


@needs_browser
def test_a_waiting_worker_shows_the_update_bar_and_the_tap_asks_it_to_take_over(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve_plain(src) as url:
        out = _drive(tmp_path, f"{url}/", [
            {"init": STUB},
            {"until": "document.querySelector('.pwa-update-bar') && document.querySelector('.pwa-update-bar').textContent", "ms": 10000},
            {"eval": "(() => { const r = document.querySelector('.pwa-update-bar').getBoundingClientRect();"
                     " return [r.bottom <= innerHeight, r.width > 0, document.querySelectorAll('.pwa-update-bar').length]; })()"},
            {"eval": "window.__sw.controllerchange && window.__sw.controllerchange(), window.__posts.length"},
            {"eval": "document.querySelector('.pwa-update-bar').click(); JSON.stringify(window.__posts)"},
        ])
    v = out["values"]
    assert v[0] == "New version available. Tap to reload.", v
    assert v[1] == [True, True, 1], v
    assert v[2] == 0, f"a controller change nobody asked for must not act: {v}"
    assert v[3] == '[{"type":"levain-skip-waiting"}]', v


@needs_browser
def test_the_worker_is_registered_at_load_without_a_tap(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve_plain(src) as url:
        out = _drive(tmp_path, f"{url}/", [
            {"until": "navigator.serviceWorker.getRegistration().then(r => !!r && r.scope.endsWith('/'))", "ms": 10000},
        ])
    assert out["values"][0] is True, out["values"]


def test_the_standalone_page_pads_every_edge_by_its_safe_area_inset() -> None:
    """black-translucent + viewport-fit=cover draws the page under the clock, notch and home indicator: the top container,
    the side gutters, the fixed update bar and the full-screen overlays must each add env(safe-area-inset-*)."""
    css = (Path(__file__).resolve().parent.parent / "levain" / "templates" / "web" / "dashboard.css").read_text()
    for needle in ("  .deck { padding-top: env(safe-area-inset-top, 0px);", "max(var(--gutter), env(safe-area-inset-left, 0px))",
                   "max(var(--gutter), env(safe-area-inset-right, 0px))", "left: calc(var(--sp-5) + env(safe-area-inset-left, 0px))",
                   "height: calc(100dvh - env(safe-area-inset-top, 0px) - env(safe-area-inset-bottom, 0px))"):
        assert needle in css, needle


@needs_browser
def test_a_status_bar_inset_pushes_the_masthead_below_the_clock(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve_plain(src) as url:
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 430, "h": 932, "dpr": 3, "mobile": True, "safe": {"top": 59, "bottom": 34, "left": 0, "right": 0}}},
            {"until": "document.querySelector('.masthead') && 1", "ms": 10000},
            {"eval": "document.querySelector('.masthead').getBoundingClientRect().top >= 59"},
        ])
    assert out["values"][-1] is True


# every manifest read answers 403 with a token message (this device's token is missing or wrong)
REFUSE_ALL = ("(() => { const f = window.fetch.bind(window); window.__manifest_reads = 0;"
              " window.fetch = function (u, o) { if (String(u).indexOf('/cockpit/manifest.json') === 0) { window.__manifest_reads++;"
              " return Promise.resolve(new Response(JSON.stringify({error: 'forbidden', message: 'missing or invalid write token'}),"
              " {status: 403, headers: {'Content-Type': 'application/json'}})); } return f(u, o); }; })()")


@needs_browser
def test_a_refused_device_is_not_retried_in_a_loop(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve_plain(src) as url:
        out = _drive(tmp_path, f"{url}/", [
            {"init": REFUSE_ALL},
            {"eval": "new Promise(r => setTimeout(() => r(window.__manifest_reads), 7000))"},
        ])
    assert out["values"][-1] <= 3, out["values"]      # the boot read (and its token re-read), never a 2/4/8 s retry loop


# the plain-text 403 a Host / cross-site refusal sends (no JSON, no "token" word): refused all the same
PLAIN_403 = REFUSE_ALL.replace("JSON.stringify({error: 'forbidden', message: 'missing or invalid write token'})", "'forbidden\\n'") \
    .replace("'Content-Type': 'application/json'", "'Content-Type': 'text/plain'")


@needs_browser
def test_a_plain_403_and_the_online_event_do_not_re_ask(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve_plain(src) as url:
        out = _drive(tmp_path, f"{url}/", [
            {"init": PLAIN_403},
            {"eval": "new Promise(r => setTimeout(() => r(window.__manifest_reads), 3000))"},
            {"eval": "(() => { window.dispatchEvent(new Event('online')); return 0; })()"},
            {"eval": "new Promise(r => setTimeout(() => r(window.__manifest_reads), 4000))"},
        ])
    first, last = out["values"][-3], out["values"][-1]
    assert 1 <= first <= 3, out["values"]             # the boot read happened (and was refused)
    assert last == first, out["values"]               # neither the 2/4 s backoff nor an online event asked again
