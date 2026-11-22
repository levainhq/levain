"""The page's one-shot guard for a missing cockpit_view.js (levain#243), in real headless Chrome over a served scratch install.

These cases prove the guard's mechanism: when the page's script for the view is answered with an error, the guard asks for it again once,
else reloads the page once with a marker bounding it. They do NOT prove the cause reported in levain#243: the rig has no service
worker, and the injected 503 is a server answer, not the network failure that issue's worker hypothesis names. What the guard does:
- re-requests the script once, from the very `src` the page named it by, and goes on when that lands (the heal case waits for the board to be drawn, so it is red if a late script leaves the page undrawn);
- else reloads the page once, a marker in sessionStorage written first, so a second failure cannot reload again;
- clears the marker when the global is there at boot (a healthy boot), so a later incident may heal again.
The storage cases (storage that throws), the marker's age and the guard's own timer are in test_cockpit_script_guard_243_node.py.

The server here is the real one with its handler wrapped: a GET for the script is answered 503 for the first N requests the PAGE makes
for it, and every other request is the real server's. A request counts as the page's by `Sec-Fetch-Dest` (`script` for a script
element, `document` for a navigation).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

pytestmark = needs_browser

KEY = "levain_cockpit_view_reload"       # the guard's sessionStorage marker
ALWAYS = 10**6

# the global is the view's api object once cockpit_view.js has run; `complete` so the page's own boot has run to its end
_DEFINED = "typeof window.LevainCockpitView === 'object' && document.readyState === 'complete' && 1"
_STATE = ("JSON.stringify({ nav: (performance.getEntriesByType('navigation')[0] || {}).type, marker: sessionStorage.getItem('" + KEY + "'),"
          " view: typeof window.LevainCockpitView })")
# the same facts, and whether the boot drew its whole board (dashboard_boot.js's LevainBootFacts, which the boot's own freshness line reads)
_STATE_DRAWN = ("JSON.stringify({ nav: (performance.getEntriesByType('navigation')[0] || {}).type, marker: sessionStorage.getItem('" + KEY + "'),"
                " view: typeof window.LevainCockpitView, drawn: !!(window.LevainBootFacts && window.LevainBootFacts().wholeDrawn),"
                " whole: document.documentElement.hasAttribute('data-board-whole') })")
# a step is `partial`: the driver does not wait for a whole board first, and a page whose script never loaded draws none
_WAIT_DEFINED = {"until": _DEFINED, "ms": 30000, "partial": True}
# the second document after a reload (its navigation type), the marker still there, and the injected script's own failure seen
# (the driver records every failed script load in window.__loadErrors, in the capture phase, before the guard's own handler)
_WAIT_RELOADED_AND_FAILED = {
    "until": ("(performance.getEntriesByType('navigation')[0] || {}).type === 'reload' && sessionStorage.getItem('" + KEY + "') !== null"
              " && (window.__loadErrors || []).filter((e) => e.indexOf('/cockpit_view.js') >= 0).length >= 2 && 1"),
    "ms": 30000, "partial": True}
# a quiet stretch in that second document, longer than the guard's own 4000 ms timer, so a reload the guard should not make has time to
# happen before anything is read (a document's performance.now() counts from its own navigation start)
_SETTLE = {"until": "performance.now() > 6000 && 1", "ms": 30000, "partial": True}


def _scenario(tmp_path: Path, fail_first: int, steps: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, int]]:
    """Drive the cockpit with the page's first `fail_first` script requests for cockpit_view.js answered 503. Returns the drive's
    result and the server's counts of the page's document requests and script requests."""
    _root, src, _ids = _install(tmp_path)
    counts = {"docs": 0, "scripts": 0}
    lock = threading.Lock()
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, httpd):
        class Flaky(httpd.RequestHandlerClass):  # type: ignore[name-defined,misc]
            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?", 1)[0]
                dest = self.headers.get("Sec-Fetch-Dest", "")
                fail = False
                with lock:
                    if path == "/" and dest == "document":
                        counts["docs"] += 1
                    elif path == "/cockpit_view.js" and dest == "script":
                        counts["scripts"] += 1
                        fail = counts["scripts"] <= fail_first
                if fail:
                    self._send(b"unavailable\n", "text/plain; charset=utf-8", status=503, head=False)
                    return
                super().do_GET()

        httpd.RequestHandlerClass = Flaky
        out = _drive(tmp_path, f"{url}/#write_token=T", steps)
    with lock:
        return out, dict(counts)


def test_a_failed_script_is_asked_for_again_once_and_the_page_goes_on_without_a_reload(tmp_path: Path) -> None:
    """Red if a late cockpit_view.js leaves the page undrawn: the boot's first read can run before the re-request lands, and this case waits
    for the whole board the way the other cockpit cases do (not `partial`), then asks the boot whether it drew one."""
    out, counts = _scenario(tmp_path, 1, [{"until": _DEFINED, "ms": 30000}, {"eval": _STATE_DRAWN}])
    state = json.loads(out["values"][-1])
    assert state["view"] == "object", state
    assert state["drawn"] is True and state["whole"] is True, state   # the page drew its board, not only defined the global
    assert counts["scripts"] == 2, counts            # the head script that failed, and the one re-request
    assert counts["docs"] == 1, counts               # no reload: the one document the drive opened
    assert state["nav"] == "navigate", state         # the document in front of the person is the one that was opened, not a reload of it
    assert state["marker"] is None, state            # nothing to remember: the page healed in place


def test_a_script_that_never_comes_reloads_the_page_once_and_never_again(tmp_path: Path) -> None:
    out, counts = _scenario(tmp_path, ALWAYS, [_WAIT_RELOADED_AND_FAILED, _SETTLE, {"eval": _STATE, "partial": True}])
    state = json.loads(out["values"][-1])
    assert counts["docs"] == 2, counts               # the opened document and the one reload, never a third
    assert state["nav"] == "reload", state
    assert state["marker"] is not None, state        # written before the reload, still there: a second failure cannot reload again
    assert state["view"] == "undefined", state       # nothing was invented in its place
    assert counts["scripts"] == 4, counts            # the head script and the one re-request, in each of the two documents


def test_a_healthy_boot_asks_for_nothing_again_reloads_nothing_and_leaves_no_marker(tmp_path: Path) -> None:
    out, counts = _scenario(tmp_path, 0, [_WAIT_DEFINED, {"eval": _STATE, "partial": True}])
    state = json.loads(out["values"][-1])
    assert counts == {"docs": 1, "scripts": 1}, counts
    assert state["nav"] == "navigate" and state["marker"] is None, state


def test_a_healthy_boot_clears_a_marker_an_earlier_incident_left(tmp_path: Path) -> None:
    left = "try { sessionStorage.setItem('" + KEY + "', '1'); } catch (e) {}"
    out, _counts = _scenario(tmp_path, 0, [{"init": left}, _WAIT_DEFINED, {"eval": _STATE, "partial": True}])
    state = json.loads(out["values"][-1])
    assert state["view"] == "object", state
    assert state["marker"] is None, state            # a later incident may heal again
