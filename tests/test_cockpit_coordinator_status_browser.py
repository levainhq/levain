"""C10 S2: the Coordinator status panel in a real headless Chrome, one fixture store per wake_state.

Each of the seven wake_state states is built with the store's own API (times relative to the real clock, because the served
page reads with the real clock), the cockpit page is served over it and the panel's rendered text is read. Set
``LEVAIN_PANEL_SHOTS`` to a folder to also save a screenshot per state (a review look; the test does not need it).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.coordinator import lease
from levain.coordinator.store import CoordStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_coordinator_status import Clock, _boot, _claimed, _drop

PANEL = "document.querySelector('#board .panel[data-panel-id=\"coord-status\"]')"
STATES = ("wake", "launching", "held", "idle", "unset", "backoff", "stopped")


def _build(state: str, qdir: Path) -> None:
    """The store for ``state``, written on a clock set so the real clock reads it as that state NOW."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    clock = Clock()
    s = CoordStore(qdir, clock=clock)
    if state == "unset":
        clock.now = now
        _drop(s)
        return
    clock.now = now - timedelta(minutes=25)
    s.coord_set_agent("claude", cwd=str(qdir.parent))
    if state == "wake":
        clock.now = now
        s.coord_set_agent("claude", cwd=str(qdir.parent))
    elif state == "launching":
        clock.now = now
        _drop(s)
    elif state == "held":
        clock.now = now - timedelta(minutes=1)
        _boot(s)
    elif state == "idle":
        clock.now = now - lease.LEASE_TTL - timedelta(minutes=1)      # its lease lapsed a minute ago; the grace still runs
        _boot(s)
    elif state == "backoff":
        clock.now = now - timedelta(minutes=12)                        # two deaths; the next is allowed minutes from now
        for n, wait in enumerate((None, lease.BACKOFF[0])):
            if wait is not None:
                _drop(s, f"too soon {n}")
                clock.advance(seconds=wait.total_seconds())
            _drop(s, f"drop {n}")
            _claimed(s)
            clock.advance(seconds=lease.LAUNCH_TTL.total_seconds())
        _drop(s, "the second death is written by this drop")
    elif state == "stopped":
        for n, wait in enumerate((None, lease.BACKOFF[0], lease.BACKOFF[1])):
            if wait is not None:
                _drop(s, f"too soon {n}")
                clock.advance(seconds=wait.total_seconds())
            _drop(s, f"drop {n}")
            _claimed(s)
            clock.advance(seconds=lease.LAUNCH_TTL.total_seconds())
        _drop(s, "after three deaths")
    else:
        raise AssertionError(state)


READ = ("(() => { const p = " + PANEL + "; if (!p) return null;"
        " const txt = p.innerText; if (!/Coordinator:/.test(txt)) return null;"
        " const r = p.getBoundingClientRect();"
        " const rows = [...p.querySelectorAll('li, .row, .ck-row, tr')].map(x => x.innerText.trim()).filter(Boolean);"
        " return {text: txt, rows, clipped: p.scrollWidth > p.clientWidth + 1, w: Math.round(r.width), h: Math.round(r.height)}; })()")


@needs_browser
@pytest.mark.parametrize("state", STATES)
def test_the_panel_draws_each_wake_state_in_a_real_browser_and_posts_nothing(state, tmp_path, monkeypatch) -> None:
    home = tmp_path / "levain_home"
    (home / "consent").mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    _build(state, home / "consent")
    _root, src, _ids = _install(tmp_path)
    steps = [{"until": READ, "ms": 30000}, {"eval": READ}]
    shots = os.environ.get("LEVAIN_PANEL_SHOTS")
    if shots:
        Path(shots).mkdir(parents=True, exist_ok=True)
        steps += [{"eval": PANEL + ".scrollIntoView({block: 'center'}); 1"}, {"shot": str(Path(shots) / f"{state}.png")}]
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", steps)
    seen = out["values"][1]
    text = seen["text"]
    if shots:
        (Path(shots) / f"{state}.txt").write_text(text + "\n\n--- rows ---\n" + "\n".join(seen["rows"]) + f"\n\n{seen['w']}x{seen['h']} posts={out['posts']}\n")
    assert text.count("Coordinator: " + state + ": ") == 1, text      # the wake word, then the store's own why
    for bad in ("undefined", "None", "null", "[object", "{", "Traceback"):
        assert bad not in text, (bad, text)
    assert not seen["clipped"], seen
    assert "Setting:" in text and "Lease:" in text
    if state == "unset":
        assert "(levain coord agent claude sets one)" in text
        assert "Setting: none set (levain coord agent claude sets one)" in text
    if state == "stopped":
        assert "levain coord take, or levain coord release" in text and text.count("Launch · expired") == 3
    assert out["posts"] == [], out["posts"]                           # a read-only panel: the page sent no POST
