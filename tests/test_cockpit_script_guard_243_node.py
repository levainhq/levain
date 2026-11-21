"""The one-shot guard at the top of dashboard_boot.js (levain#243), run under node: the real boot file against a stub page.

These cases hold the guard's own promises, which the browser cases in test_cockpit_script_guard_243.py cannot reach (they need a real
storage that throws, or a marker of a chosen age): the marker is a time and goes stale, a fresh one stops a second reload, storage that
cannot keep a marker never reloads, the marker is written before the reload, and both ways of a good boot clear it.

One node process per case. The stub page names `/cockpit_view.js?v=abc` in a script tag, records the element the guard injects, and
lets the case fire that element's load or error by hand; `Date.now` and `location.reload` are the case's own.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

WEB = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

KEY = "levain_cockpit_view_reload"       # the guard's sessionStorage marker
NOW = 1_800_000_000_000                  # the stub clock, in ms
TTL_MS = 60_000                          # the guard's MARK_TTL_MS

_DRIVER = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const c = JSON.parse(process.argv[3]);
const NOW = c.now, KEY = c.key;
Date.now = () => NOW;
globalThis.window = globalThis;
const store = {}, log = { writes: [], removes: 0, reloads: 0, markerAtReload: [], injected: [] };
if (c.marker !== null) store[KEY] = c.marker;
const ss = {
  getItem: (k) => { if (c.storage === "throws") throw new Error("SecurityError"); return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
  setItem: (k, v) => { if (c.storage === "throws") throw new Error("QuotaExceededError"); log.writes.push(v); if (c.storage !== "noop") store[k] = String(v); },
  removeItem: (k) => { log.removes += 1; delete store[k]; },
};
Object.defineProperty(globalThis, "sessionStorage", { value: ss, configurable: true, writable: true });
globalThis.location = { hash: "", search: "", pathname: "/", reload: () => { log.reloads += 1; log.markerAtReload.push(Object.prototype.hasOwnProperty.call(store, KEY) ? store[KEY] : null); } };
globalThis.history = { replaceState() {} };
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.fetch = () => Promise.reject(new Error("offline"));
const timers = [];
globalThis.setTimeout = (f, ms) => { timers.push({ f, ms }); return timers.length; };
globalThis.clearTimeout = () => {};
if (c.global === "present") globalThis.LevainCockpitView = {};
const node = () => ({ dataset: {}, classList: { add() {}, contains: () => false, toggle() {}, remove() {} }, addEventListener() {}, setAttribute() {}, appendChild() {}, append() {} });
const tag = { getAttribute: (n) => (n === "src" ? "/cockpit_view.js?v=abc" : null) };
globalThis.document = { readyState: "complete", visibilityState: "visible", activeElement: null, body: node(), documentElement: node(),
  head: { appendChild: (el) => { log.injected.push(el.src); globalThis.__el = el; } },
  addEventListener() {}, querySelectorAll: () => [], querySelector: (s) => (s === 'script[src^="/cockpit_view.js"]' ? tag : null),
  getElementById: () => null, createElement: () => ({}), createElementNS: node, createTextNode: () => node() };
new Function(src)();
const el = globalThis.__el;
const fire = (how) => { if (!el) return; if (c.then === "heal") globalThis.LevainCockpitView = {}; el[how](); };
if (c.then === "error" || c.then === "heal") fire("onerror");
if (c.then === "twice") { fire("onerror"); fire("onload"); }
const out = { reloads: log.reloads, markerAtReload: log.markerAtReload, writes: log.writes, removes: log.removes, injected: log.injected,
  marker: Object.prototype.hasOwnProperty.call(store, KEY) ? store[KEY] : null };
console.log(JSON.stringify(out));
process.exit(0);                      // the boot's own timers would keep node alive
"""


def _run(tmp_path: Path, **case: Any) -> dict[str, Any]:
    drv = tmp_path / "drive.js"
    drv.write_text(_DRIVER, encoding="utf-8")
    spec = {"now": NOW, "key": KEY, "marker": None, "storage": "ok", "global": "missing", "then": "error", **case}
    run = subprocess.run([NODE, str(drv), str(WEB / "dashboard_boot.js"), json.dumps(spec)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout.strip().splitlines()[-1])


def test_storage_that_throws_never_reloads(tmp_path: Path) -> None:
    """Red with a guard that reloads when it cannot read the marker (a catch that falls through to reload): reloads would be 1."""
    out = _run(tmp_path, storage="throws")
    assert out["injected"] == ["/cockpit_view.js?v=abc"], out    # it still asks once more for the script
    assert out["reloads"] == 0, out


def test_storage_that_cannot_keep_the_marker_never_reloads(tmp_path: Path) -> None:
    """Red with a guard that reloads after writing without reading the marker back (the write is a no-op here): reloads would be 1."""
    out = _run(tmp_path, storage="noop")
    assert out["writes"], out                                    # it did try to write
    assert out["reloads"] == 0, out


def test_a_fresh_marker_stops_a_second_reload(tmp_path: Path) -> None:
    """Red with a guard that ignores a marker already there (or one that treats a fresh stamp as stale): reloads would be 1."""
    held = str(NOW - 5_000)
    out = _run(tmp_path, marker=held)
    assert out["reloads"] == 0, out
    assert out["writes"] == [] and out["marker"] == held, out    # and it leaves the marker as it found it


def test_the_marker_goes_stale_after_sixty_seconds_and_not_before(tmp_path: Path) -> None:
    """Red on the guard before the marker was a time (a held marker of any age blocked): the 60001 ms, the garbage and the old "1" cases
    reload. Red with a guard that drops the age check the other way (the 60000 ms case then reloads)."""
    edge = _run(tmp_path, marker=str(NOW - TTL_MS))
    assert edge["reloads"] == 0, edge                            # exactly the limit: still the same incident
    for held in (str(NOW - TTL_MS - 1), "1", "not a time", ""):
        out = _run(tmp_path, marker=held)
        assert out["reloads"] == 1, (held, out)                  # an old incident, or no time at all: a later recovery is allowed


def test_the_marker_is_written_before_the_reload_and_a_second_event_does_not_reload_again(tmp_path: Path) -> None:
    """Red with a guard that reloads before it writes the marker (markerAtReload would be null, and the second event would reload
    again). The injected element fires its error and then its load; the marker the first one wrote is what stops the second."""
    out = _run(tmp_path, then="twice")
    assert out["reloads"] == 1, out
    assert out["markerAtReload"] == [str(NOW)], out              # the marker, as a time, was already in storage when the reload went out


def test_a_healthy_boot_clears_the_marker_and_asks_for_nothing(tmp_path: Path) -> None:
    """Red with a guard that does not clear the marker at a healthy boot: the marker would still be there."""
    out = _run(tmp_path, **{"global": "present", "marker": str(NOW - 5_000)})
    assert out["marker"] is None, out
    assert out["injected"] == [] and out["reloads"] == 0, out


def test_a_script_that_lands_on_the_re_request_clears_the_marker_and_does_not_reload(tmp_path: Path) -> None:
    """Red with a guard whose decision clears no marker when the global has arrived by then (the fresh marker would still be there), or
    one that reloads anyway (reloads would be 1)."""
    out = _run(tmp_path, then="heal", marker=str(NOW - 5_000))
    assert out["reloads"] == 0, out
    assert out["marker"] is None, out
