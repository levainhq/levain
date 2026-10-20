"""K10 S8b: the Replay strip (topology_view.js, the page half of S8a's `?at=` read) and the conflict chip that never covers a label.

Through the real server and the real page (headless Chrome): the strip draws only when the topology panel's head says `replay: true`;
a scrub asks `?at=` with a whole-minute T taken from the live read's `as_of` (never the browser's clock), pauses the topology's live
refetch and says so; Live resumes; Esc and the arrow keys; what the past read could not rebuild is drawn as unknown; a refusal (400)
is shown plainly and the page stays live; below 1440 there is no topology and no replay. A literal value handed to the renderer
carries the shapes the server does not send yet (no `replay` option, an unreadable source) and the chip's bounding boxes."""
from __future__ import annotations

import json

from tests.test_cockpit_f12b_topology_view import TV, _paint, _run, _value
from tests.test_cockpit_flip_parity import needs_browser
from tests.test_cockpit_k10_agents import PRIME
from tests.test_cockpit_k7_build import _sessions_fixture

STRIP = "document.querySelector('#k7-sessions .tv-replay')"
SPY = ("(() => { window.__urls = []; const f = window.fetch.bind(window); window.fetch = (u, o) => { const s = String(u); window.__urls.push(s); "
       "return f(window.__rewrite ? window.__rewrite(s) : u, o); }; return 1; })()")


def _scrub(frac: float) -> str:
    """A press, a move and a release on the track at `frac` of its width (what a click or a short drag does)."""
    return ("(() => { const t = document.querySelector('#k7-sessions .tv-rtrack'); const r = t.getBoundingClientRect(); const x = r.left + r.width * %s; "
            "const ev = (n) => new PointerEvent(n, {bubbles: true, clientX: x, clientY: r.top + 5, pointerId: 1}); "
            "t.dispatchEvent(ev('pointerdown')); t.dispatchEvent(ev('pointermove')); t.dispatchEvent(ev('pointerup')); return 1; })()") % frac


def _key(key: str, on: str = "document.querySelector('#k7-sessions .tv-rtrack')") -> str:
    return "(() => { %s.dispatchEvent(new KeyboardEvent('keydown', {key: %s, bubbles: true})); return 1; })()" % (on, json.dumps(key))


PROBE = ("JSON.stringify({strip: !!%s, label: (%s || {querySelector: () => null}).querySelector('.tv-rcap b') && %s.querySelector('.tv-rcap b').textContent, "
         "sub: %s && %s.querySelector('.tv-rcap span').textContent, badge: (document.querySelector('#k7-sessions .tv-rbadge') || {}).textContent || null, "
         "pressed: %s && document.querySelector('#k7-sessions .tv-live').getAttribute('aria-pressed'), "
         "err: (document.querySelector('#k7-sessions .tv-rerr') || {}).textContent || null, "
         "unk: [...document.querySelectorAll('#k7-sessions .tv-unk')].map(n => n.textContent), "
         "urls: (window.__urls || []).filter(u => u.includes('topology.json')), "
         "bars: document.querySelectorAll('#k7-sessions .tv-rbars i').length, hours: [...document.querySelectorAll('#k7-sessions .tv-rhr')].map(n => n.textContent)})") % (
    STRIP, STRIP, STRIP, STRIP, STRIP, STRIP)
AT = "u => decodeURIComponent((u.match(/[?&]at=([^&]+)/) || [])[1] || '')"


def _ats(got: dict) -> list[str]:
    import re
    from urllib.parse import unquote
    return [unquote(m.group(1)) for u in got["urls"] for m in [re.search(r"[?&]at=([^&]+)", u)] if m]


# --- the strip is drawn from the head's flag -------------------------------------------------------------


@needs_browser
def test_the_strip_is_drawn_under_the_topology_with_its_label_sub_bars_hours_and_a_live_pill(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [{"until": STRIP}, {"until": "document.querySelectorAll('#k7-sessions .tv-rbars i').length > 10"}, {"eval": PROBE}])
    got = json.loads(out[-1])
    assert got["strip"] is True and got["label"] == "Replay" and got["sub"] == "last 14 h, from the ledger", got
    assert got["pressed"] == "true" and got["badge"] is None and got["err"] is None, got      # Live is the default
    assert got["bars"] >= 100 and len(got["hours"]) >= 6, got                                  # a bar per bucket, an hour label every two hours


@needs_browser
def test_no_strip_without_the_replay_flag_and_none_below_1440(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    # a value handed to the renderer with no `replay` option (a head without `replay: true`) draws no strip, in either lens
    out = _run(tmp_path, src, [{"until": STRIP}, {"eval": _paint(_value(), "work")}, {"eval": "!!document.querySelector('#k7-sessions .tv-replay')"},
                               {"eval": _paint(_value(), "agents")}, {"eval": "!!document.querySelector('#k7-sessions .tv-replay')"}])
    assert out[-3] is False and out[-1] is False, out
    # below 1440 the list is the default: no topology, so no replay
    from tests.test_cockpit_f12b_topology_view import MID, NARROW
    for dev in (MID, NARROW):
        o = _run(tmp_path, src, [{"until": "document.querySelector('#k7-sessions .k7-srow')"}, {"eval": "!!document.querySelector('#k7-sessions .tv-replay')"}], first=dev)
        assert o[-1] is False, (dev, o)


# --- a scrub ----------------------------------------------------------------------------------------------


@needs_browser
def test_a_scrub_asks_a_whole_minute_from_the_live_as_of_not_the_browser_clock_pauses_the_topology_and_says_so(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    live_asof = "window.LevainCockpitTransport.readJson('/cockpit/panel/topology.json?profile=full').then(p => p.value.as_of)"
    skew = "(() => { const n = Date.now.bind(Date); Date.now = () => n() + 3 * 86400000; return 1; })()"        # a browser clock three days off
    out = _run(tmp_path, src, [{"until": STRIP}, {"eval": SPY}, {"eval": skew}, {"eval": live_asof}, {"eval": _scrub(0.5)},
                               {"until": "document.querySelector('#k7-sessions .tv-rbadge')"}, {"eval": PROBE}])
    asof, got = out[-4], json.loads(out[-1])
    ats = _ats(got)
    assert len(ats) == 1, got
    from datetime import datetime, timedelta, timezone
    t = datetime.strptime(ats[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    a = datetime.fromisoformat(asof.replace("Z", "+00:00"))
    assert t.second == 0 and a - timedelta(hours=14) <= t <= a, (ats, asof)                                  # the server's window, the server's clock
    assert got["err"] is None, got                                                                           # the server answered it (a browser-clock T would be refused)
    assert got["badge"].startswith("replay ") and got["badge"].endswith(" — live paused"), got
    assert got["pressed"] == "false", got


@needs_browser
def test_scrubbing_pauses_the_topologys_live_refetch_and_live_resumes_it(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    refetch = "window.LevainCockpitView.refetchPanel('topology').then(r => String(r))"
    out = _run(tmp_path, src, [{"until": STRIP}, {"eval": SPY}, {"eval": _scrub(0.4)}, {"until": "document.querySelector('#k7-sessions .tv-rbadge')"},
                               {"eval": "(window.__urls = [], 1)"}, {"eval": refetch}, {"eval": PROBE},
                               {"eval": "document.querySelector('#k7-sessions .tv-live').click(); 1"},
                               {"until": "!document.querySelector('#k7-sessions .tv-rbadge')"}, {"until": "(window.__urls || []).some(u => u.includes('topology.json') && !u.includes('at='))"},
                               {"eval": PROBE}])
    paused, live = json.loads(out[-5]), json.loads(out[-1])
    assert out[-6] == "applied", out                                                                         # answered without a read
    assert paused["urls"] == [], paused                                                                      # no topology request while replaying
    assert live["badge"] is None and live["pressed"] == "true" and any("at=" not in u for u in live["urls"]), live   # Live: resumed, a live read went out


@needs_browser
def test_escape_returns_to_live_and_the_arrows_step_one_bucket(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [{"until": STRIP}, {"eval": SPY}, {"eval": _key("ArrowLeft")}, {"until": "document.querySelector('#k7-sessions .tv-rbadge')"},
                               {"eval": _key("ArrowLeft")}, {"until": "(window.__urls || []).filter(u => u.includes('at=')).length >= 2"}, {"eval": PROBE},
                               {"eval": _key("Escape", "document")}, {"until": "!document.querySelector('#k7-sessions .tv-rbadge')"}, {"eval": PROBE}])
    stepped, back = json.loads(out[-4]), json.loads(out[-1])
    from datetime import datetime
    ats = [datetime.strptime(x, "%Y-%m-%dT%H:%M:%SZ") for x in _ats(stepped)]
    assert len(ats) >= 2 and (ats[0] - ats[1]).total_seconds() == 300, ats                                   # one bucket (5 min) per press
    assert back["badge"] is None and back["pressed"] == "true", back


# --- what the past could not rebuild, and a refusal ------------------------------------------------------


@needs_browser
def test_a_source_the_past_read_could_not_rebuild_is_drawn_as_unknown_never_as_none(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [{"until": STRIP}, {"eval": _scrub(0.4)}, {"until": "document.querySelector('#k7-sessions .tv-rbadge')"},
                               {"until": "document.querySelector('#k7-sessions .tv-unk')"}, {"eval": PROBE}])
    got = json.loads(out[-1])
    assert any(u.startswith("open loops: not kept for the past") for u in got["unk"]), got
    # a literal replay value: every unreadable name is said, in words, with the same reason
    v = _value()
    v["replay"] = True
    v["unreadable"] = ["spores", "lost_if_closed", "session_logs"]
    o = _run(tmp_path, src, [{"until": STRIP}, {"eval": _paint(v, "work")}, {"eval": PROBE}])
    unk = json.loads(o[-1])["unk"]
    assert [u.split(":")[0] for u in unk] == ["open loops", "what closing would lose", "rate and state from the session logs"], unk
    assert all(u.endswith("not kept for the past") for u in unk), unk


@needs_browser
def test_a_refusal_is_said_plainly_and_the_page_stays_live(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    # the page asks a T the server refuses (15 h back): the page never clamps it
    rewrite = ("window.__rewrite = (s) => (s.includes('topology.json') && s.includes('at=') ? s.replace(/at=[^&]+/, 'at=' + encodeURIComponent("
               "new Date(Math.floor((Date.now() - 15 * 3600e3) / 60000) * 60000).toISOString().replace('.000Z', 'Z'))) : s); 1")
    out = _run(tmp_path, src, [{"until": STRIP}, {"eval": SPY}, {"eval": rewrite}, {"eval": _scrub(0.3)},
                               {"until": "document.querySelector('#k7-sessions .tv-rerr')"}, {"eval": PROBE},
                               {"eval": "(window.__urls = [], window.LevainCockpitView.refetchPanel('topology').then(r => String(r)))"}, {"eval": PROBE}])
    got, after = json.loads(out[-3]), json.loads(out[-1])
    assert got["err"] and got["err"].lower().startswith("replay refused"), got
    assert got["badge"] is None and got["pressed"] == "true", got                                            # still live
    assert any("at=" not in u for u in after["urls"]), after                                                  # and live polling is not paused


@needs_browser
def test_the_repaint_signature_carries_the_replay_time(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    sig = "window.LevainTopologyView.signature(%s, 'work')" % json.dumps(_value())
    out = _run(tmp_path, src, [{"until": STRIP}, {"eval": sig}, {"eval": _scrub(0.4)}, {"until": "document.querySelector('#k7-sessions .tv-rbadge')"}, {"eval": sig},
                               {"eval": _scrub(0.6)}, {"until": "document.querySelector('#k7-sessions .tv-rbadge')"}, {"eval": sig}])
    assert len({out[1], out[4], out[7]}) == 3, out[1:]                                                       # live, T1, T2 all repaint


# --- the conflict chip never covers a label --------------------------------------------------------------


CHIP_PROBE = ("JSON.stringify((() => { const c = document.querySelector('#k7-sessions .tv-confchip').getBoundingClientRect(); "
              "const hit = (n) => { const r = n.getBoundingClientRect(); return !(r.right <= c.left || r.left >= c.right || r.bottom <= c.top || r.top >= c.bottom); }; "
              "return {labels: [...document.querySelectorAll('#k7-sessions .tv-lblbg')].filter(hit).length, faces: [...document.querySelectorAll('#k7-sessions .tv-facebg')].filter(hit).length, "
              "w: c.width}; })())")


def _conflict_value(path: str, a: str, b: str) -> dict:
    v = _value()
    v["conflicts"] = [{"path": path, "nodes": [a, b]}]
    return v


@needs_browser
def test_the_conflict_chip_covers_no_group_label_hub_label_or_face(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    for path, a, b in (("money.py", "n1", "n4"), ("src/ledgerline/money_rounding_and_tally.py", "n1", "n5"), ("money.py", "n2", "n4"), ("a.py", "n3", "n5")):
        out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_conflict_value(path, a, b), "agents")}, {"eval": CHIP_PROBE}])
        got = json.loads(out[-1])
        assert got["w"] > 0 and got["labels"] == 0 and got["faces"] == 0, (path, a, b, got)
