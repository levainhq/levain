"""K10 fix5: the page-half L3 findings on the Replay strip and the Agents graph (topology_view.js, cockpit_view.js).

Requested T is not committed T (a badge never says "live paused" over a value that is not T's; a stale answer is dropped by a token, with or
without AbortController) · a spawn is bounded per edge per frame · a past frame is still (no streams, no tickers, no breathe) · replay and the
stream loop never outlive their view · a past value is never drawn without its badge · the scrub window follows the server clock · ids that are
`__proto__` are ids · the hour marks work in any zone · Live keeps the frame until the live read lands · Esc is the card's · the histogram reads
only for a strip that is on screen · focus is taken back only for a read that went out."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from tests.test_cockpit_f12b_topology_view import TV, _paint, _run, _value
from tests.test_cockpit_flip_parity import needs_browser
from tests.test_cockpit_k10_agents import PRIME, _graph_value
from tests.test_cockpit_k7_build import _sessions_fixture
from tests.test_cockpit_s8b_replay_page import SPY, STRIP, _ats, _key, _scrub

WAIT_BADGE = {"until": "document.querySelector('#ck-hlens .tv-rbadge')"}
SLEEP = lambda ms: {"eval": "new Promise(r => setTimeout(r, %d))" % ms}  # noqa: E731
INFO = ("JSON.stringify({badge: (document.querySelector('#ck-hlens .tv-rbadge') || {}).textContent || null, "
        "rat: (%s || {dataset: {}}).dataset.rat || null, rshown: (%s || {dataset: {}}).dataset.rshown || null, "
        "replaying: window.LevainTopologyView.replaying(), paused: window.LevainTopologyView.paused(), "
        "err: (document.querySelector('#ck-hlens .tv-rerr') || {}).textContent || null, urls: (window.__urls || []).filter(u => u.includes('topology.json'))})") % (TV, TV)


def _delay(pred: str, ms: int) -> str:
    """Hold every fetch whose url satisfies `pred` (a JS expression over `s`) for `ms` before it goes out."""
    return ("(() => { const f = window.fetch.bind(window); window.fetch = (u, o) => { const s = String(u); "
            "return (%s) ? new Promise(r => setTimeout(r, %d)).then(() => f(u, o)) : f(u, o); }; return 1; })()") % (pred, ms)


# --- P1: requested T is not committed T -------------------------------------------------------------------


@needs_browser
def test_a_scrub_in_flight_says_loading_over_the_live_value_and_live_paused_only_over_the_value_that_is_t(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [{"until": STRIP}, {"eval": SPY}, {"eval": _delay("s.includes('at=')", 1200)}, {"eval": _scrub(0.5)}, {"until": "(document.querySelector('#ck-hlens .tv-rbadge') || {textContent: ''}).textContent.startsWith('loading replay ')"}, {"eval": INFO},
                               {"until": "(document.querySelector('#ck-hlens .tv-rbadge') || {textContent: ''}).textContent.includes('live paused')", "ms": 8000}, {"eval": INFO}])
    mid, done = json.loads(out[-3]), json.loads(out[-1])
    assert mid["badge"] and mid["badge"].startswith("loading replay ") and "live paused" not in mid["badge"] and "showing live" in mid["badge"], mid
    assert mid["rshown"] == "live", mid                                                                       # the live value is what is drawn, and it says so
    assert done["badge"].startswith("replay ") and done["badge"].endswith(" — live paused"), done
    assert done["rshown"] == done["rat"] and done["rat"], done                                                # the drawn value is the one whose as_of is T


@needs_browser
def test_a_second_scrub_keeps_the_first_frame_labelled_while_it_loads(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [{"until": STRIP}, {"eval": SPY}, {"eval": _scrub(0.3)}, {"until": "(document.querySelector('#ck-hlens .tv-rbadge') || {textContent: ''}).textContent.includes('live paused')"},
                               {"eval": INFO}, {"eval": _delay("s.includes('at=')", 1200)}, {"eval": _scrub(0.6)}, {"until": "(document.querySelector('#ck-hlens .tv-rbadge') || {textContent: ''}).textContent.startsWith('loading replay ')"}, {"eval": INFO},
                               {"until": "(document.querySelector('#ck-hlens .tv-rbadge') || {textContent: ''}).textContent.includes('live paused')", "ms": 8000}, {"eval": INFO}])
    first, mid, last = json.loads(out[-7]), json.loads(out[-3]), json.loads(out[-1])
    assert mid["badge"].startswith("loading replay ") and "live paused" not in mid["badge"], mid
    assert mid["rshown"] == first["rat"] and mid["rat"] != first["rat"], (first, mid)                         # T1's frame, labelled as T1, while T2 loads
    assert last["rshown"] == last["rat"] == mid["rat"], last


@needs_browser
def test_a_stale_answer_is_dropped_by_the_request_token_even_without_abort_controller(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    slow_first = ("(() => { window.AbortController = undefined; window.__n = 0; const f = window.fetch.bind(window); window.fetch = (u, o) => { const s = String(u); "
                  "if (!s.includes('at=')) return f(u, o); const slow = window.__n++ === 0; "
                  "return slow ? new Promise(r => setTimeout(r, 1500)).then(() => f(u, o)) : f(u, o); }; return 1; })()")
    out = _run(tmp_path, src, [{"until": STRIP}, {"eval": SPY}, {"eval": slow_first}, {"eval": _scrub(0.3)}, SLEEP(150), {"eval": _scrub(0.7)}, SLEEP(2500), {"eval": INFO}])
    got = json.loads(out[-1])
    ats = _ats(got)
    assert len(ats) == 2 and ats[0] != ats[1], got
    assert got["rat"] == got["rshown"] == ats[0], (ats, got)                                                    # the later scrub's frame, never the slow earlier one


# --- P2: a spawn is bounded -------------------------------------------------------------------------------


@needs_browser
def test_an_absurd_rate_draws_at_the_cap_and_never_loops(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _graph_value()
    v["nodes"][1]["rate"] = 1e308                                                                           # finite on the wire, Infinity once scaled
    v["nodes"][3]["rate"] = 5e6
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(v, "agents")}, {"eval": "%s.querySelector('.tv-stage').dataset.particles" % TV}])
    assert 0 < int(out[-1]) <= 1500, out[-1]


# --- P3: a replay frame is still ---------------------------------------------------------------------------


STILL = ("JSON.stringify({still: !!%s.classList.contains('tv-replaying'), streams: %s.querySelector('.tv-stage') && %s.querySelector('.tv-stage').dataset.streams, "
         "particles: %s.querySelector('.tv-stage') && %s.querySelector('.tv-stage').dataset.particles, iv: window.__iv, "
         "pulse: (%s.querySelector('.tv-pulse') && getComputedStyle(%s.querySelector('.tv-pulse')).animationName) || null, "
         "breathe: (%s.querySelector('.tv-task.waiting, .tv-rec') && getComputedStyle(%s.querySelector('.tv-task.waiting, .tv-rec')).animationName) || null})") % ((TV,) * 9)
SPYI = "(() => { window.__iv = 0; const s = window.setInterval.bind(window); window.setInterval = (f, ms, ...a) => { if (ms === 20000) window.__iv++; return s(f, ms, ...a); }; return 1; })()"


def _streaming() -> dict:
    v = _graph_value()
    v["nodes"][1]["rate"] = 6.0
    return v


@needs_browser
def test_a_past_frame_has_no_streams_no_tickers_and_no_breathing_while_the_live_one_does(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    past = _streaming()
    past["replay"] = True
    # paint and probe in ONE synchronous eval: a poll that repaints the real card between two steps cannot land in the middle of a read
    both = lambda value, lens: {"eval": "(() => { window.__iv = 0; %s; return %s; })()" % (_paint(value, lens), STILL)}  # noqa: E731
    out = _run(tmp_path, src, [*PRIME, {"eval": SPYI}, both(_streaming(), "agents"), both(past, "agents"), both(_value(), "work"), both({**_value(), "replay": True}, "work")])
    live, still, wlive, wstill = (json.loads(out[i]) for i in (-4, -3, -2, -1))
    assert live["still"] is False and int(live["streams"]) > 0 and int(live["particles"]) > 0 and live["iv"] >= 1 and live["pulse"] not in (None, "none"), live       # the control
    assert still["still"] is True and still["streams"] == "0" and still["particles"] == "0" and still["iv"] == 0 and still["pulse"] == "none", still
    assert wlive["breathe"] not in (None, "none"), wlive
    assert wstill["still"] is True and wstill["breathe"] == "none", wstill


# --- P4 / P5: replay and the loop never outlive their view ------------------------------------------------


@needs_browser
def test_leaving_for_the_memory_tab_stops_the_stream_loop_and_leaves_replay(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    raf = "(() => { window.__raf = 0; const r = window.requestAnimationFrame.bind(window); window.requestAnimationFrame = (f) => { window.__raf++; return r(f); }; return 1; })()"
    out = _run(tmp_path, src, [*PRIME, {"eval": raf}, {"eval": _paint(_streaming(), "agents")}, SLEEP(200), {"eval": "(window.__raf = 0, 1)"},
                               {"eval": "document.querySelector('[data-mode-tab=memory]').click(); 1"}, SLEEP(400), {"eval": "window.__raf"}])
    assert out[-1] <= 2, out[-1]                                                                                # one frame already queued at most; the loop is stopped


@needs_browser
def test_a_tab_switch_to_memory_drops_replay_and_the_topology_polls_again(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    # the tab switch starts a live read of its own; a refetch that lands while that one is out settles "superseded" by design, so the
    # test's refetch asks again until a read settles it (at most 5 times) and the assertion is on that settled answer
    refetch = ("(async () => { let r; for (let i = 0; i < 5; i++) { r = String(await window.LevainCockpitView.refetchPanel('topology')); "
               "if (r !== 'superseded') break; } return r; })()")
    out = _run(tmp_path, src, [{"until": STRIP}, {"eval": SPY}, {"eval": _scrub(0.4)}, WAIT_BADGE, {"eval": "(window.__urls = [], 1)"},
                               {"eval": "document.querySelector('[data-mode-tab=memory]').click(); 1"},
                               {"until": "!window.LevainTopologyView.replaying() && !window.LevainTopologyView.paused()"}, {"eval": INFO}, {"eval": refetch}, {"eval": INFO},
                               {"eval": "document.querySelector('[data-mode-tab=build]').click(); 1"}, {"until": STRIP}, SLEEP(300), {"eval": INFO}])
    after_tab, after_refetch, back = json.loads(out[-7]), json.loads(out[-5]), json.loads(out[-1])
    assert after_tab["replaying"] is False and after_tab["paused"] is False, after_tab
    assert out[-6] == "applied" and any("at=" not in u for u in after_refetch["urls"]), (out[-6], after_refetch)   # a live read went out
    assert back["badge"] is None and back["replaying"] is False, back                                           # and Build comes back live, drawn


@needs_browser
def test_replay_ends_when_the_page_loses_its_replay_path_while_scrubbing(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [{"until": STRIP}, {"eval": _scrub(0.4)}, WAIT_BADGE,
                               {"eval": "(() => { window.__tr = window.LevainCockpitTransport; delete window.LevainCockpitTransport; window.LevainCockpitKinds.paintBuildSessions(); return 1; })()"},
                               {"eval": INFO}])
    got = json.loads(out[-1])
    assert got["replaying"] is False and got["paused"] is False, got


@needs_browser
def test_switching_to_the_event_graph_while_replaying_draws_nothing_from_the_past(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    spy = ("(() => { window.__lost = []; const f = window.LevainTopologyView.renderLost; window.LevainTopologyView.renderLost = (v) => { window.__lost.push(v.replay === true); return f(v); }; return 1; })()")
    out = _run(tmp_path, src, [{"until": STRIP}, {"eval": _scrub(0.4)}, {"until": "window.LevainTopologyView.replayValue() !== null"}, {"eval": spy},
                               {"eval": "document.querySelector('#ck-hlens [data-tv=events]').click(); 1"}, {"until": "document.getElementById('ck-skycard').dataset.hlens == 'events'"}, SLEEP(300),
                               {"eval": "JSON.stringify({lost: window.__lost, replaying: window.LevainTopologyView.replaying(), badge: !!document.querySelector('#ck-hlens .tv-rbadge')})"}])
    got = json.loads(out[-1])
    assert got["replaying"] is False and got["badge"] is False and True not in got["lost"], got


# --- P6: the scrub window follows the server clock --------------------------------------------------------


@needs_browser
def test_the_window_floor_moves_with_the_server_clock_the_page_estimates(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    asof = "window.LevainCockpitTransport.readJson('/cockpit/panel/topology.json?profile=full').then(p => p.value.as_of)"
    # the first scrub anchors the window; three hours (of the page's monotonic clock) later a scrub at the far left must not ask for T < est - 14 h
    skew = "(() => { const p = performance.now.bind(performance); performance.now = () => p() + 3 * 3600e3; return 1; })()"
    out = _run(tmp_path, src, [{"until": STRIP}, {"eval": SPY}, {"eval": asof}, {"eval": _scrub(0.5)}, WAIT_BADGE, {"eval": skew}, {"eval": "(window.__urls = [], 1)"},
                               {"eval": _scrub(0.0)}, SLEEP(800), {"eval": INFO}])
    from datetime import datetime, timedelta
    a = datetime.fromisoformat(out[-8].replace("Z", "+00:00")).replace(tzinfo=None)
    ats = [datetime.strptime(x, "%Y-%m-%dT%H:%M:%SZ") for x in _ats(json.loads(out[-1]))]
    assert ats and ats[0] >= a - timedelta(hours=11, minutes=1), (a, ats)


@needs_browser
def test_a_400_that_names_the_window_re_anchors_once_and_retries(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    once = ("(() => { window.__bad = 0; const f = window.fetch.bind(window); window.fetch = (u, o) => { const s = String(u); "
            "if (s.includes('topology.json') && s.includes('at=') && window.__bad++ === 0) return f(s.replace(/at=[^&]+/, 'at=' + encodeURIComponent("
            "new Date(Math.floor((Date.now() - 15 * 3600e3) / 60000) * 60000).toISOString().replace('.000Z', 'Z'))), o); return f(u, o); }; return 1; })()")
    out = _run(tmp_path, src, [{"until": STRIP}, {"eval": SPY}, {"eval": once}, {"eval": _scrub(0.3)},
                               {"until": "(document.querySelector('#ck-hlens .tv-rbadge') || {textContent: ''}).textContent.includes('live paused')", "ms": 8000}, {"eval": INFO}])
    got = json.loads(out[-1])
    assert got["err"] is None and got["badge"].endswith(" — live paused"), got
    assert len(_ats(got)) == 2, got                                                                              # the refused read and exactly one retry


# --- P7: wire ids are ids ---------------------------------------------------------------------------------


@needs_browser
def test_a_group_and_a_node_whose_id_is_proto_are_drawn(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _graph_value()
    v["groups"][0]["id"] = "__proto__"
    for n in v["nodes"]:
        if n["group"] == "g1":
            n["group"] = "__proto__"
    v["nodes"][1]["id"] = "__proto__"
    v["nodes"][2]["parent"] = "__proto__"
    probe = ("JSON.stringify({group: !!%s.querySelector('.tv-ggroup[data-group=\"__proto__\"]'), node: !!%s.querySelector('.tv-gnode[data-node=\"__proto__\"]'), "
             "hub: !!%s.querySelector('.tv-edge[data-edge=\"hub:__proto__\"]'), nodes: %s.querySelectorAll('.tv-gnode').length})") % ((TV,) * 4)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(v, "agents")}, {"eval": probe}])
    assert json.loads(out[-1]) == {"group": True, "node": True, "hub": True, "nodes": 5}, out[-1]


# --- P8: the hour marks are on the local even hours, in any zone ----------------------------------------


def test_hour_marks_are_the_local_even_hours_in_zones_off_utc_by_an_hour_a_half_hour_or_behind():
    node = shutil.which("node")
    if node is None:
        import pytest
        pytest.skip("node is needed")
    js = Path(__file__).resolve().parents[1] / "levain" / "templates" / "web" / "topology_view.js"
    prog = ("const T = require(%s); const hi = Date.parse('2026-10-20T15:07:00Z'), lo = hi - 14 * 3600e3; "
            "const m = T.hourMarks(lo, hi); console.log(JSON.stringify(m.map(x => [x.label, x.ms, new Date(x.ms).getMinutes()])));") % json.dumps(str(js))
    from datetime import datetime, timezone
    hi_ms = datetime(2026, 10, 20, 15, 7, tzinfo=timezone.utc).timestamp() * 1000
    for tz in ("UTC", "Africa/Lagos", "Asia/Kolkata", "America/New_York", "Pacific/Auckland", "Asia/Kathmandu"):
        res = subprocess.run([node, "-e", prog], env={**os.environ, "TZ": tz}, capture_output=True, text=True, timeout=30)
        assert res.returncode == 0, (tz, res.stderr)
        marks = json.loads(res.stdout)
        assert 6 <= len(marks) <= 8, (tz, marks)
        assert all(int(lab[:2]) % 2 == 0 and lab.endswith(":00") and mins == 0 for lab, _ms, mins in marks), (tz, marks)
        assert all(hi_ms - 14 * 3600e3 <= ms <= hi_ms for _l, ms, _m in marks), (tz, marks)
        assert all(b[1] - a[1] == 2 * 3600e3 for a, b in zip(marks, marks[1:])), (tz, marks)


# --- P9: Live keeps the frame until the live read lands ----------------------------------------------------


@needs_browser
def test_live_keeps_the_replay_frame_labelled_until_the_live_read_lands(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [{"until": STRIP}, {"eval": SPY}, {"eval": _scrub(0.4)},
                               {"until": "(document.querySelector('#ck-hlens .tv-rbadge') || {textContent: ''}).textContent.includes('live paused')"},
                               {"eval": _delay("s.includes('topology.json') && !s.includes('at=')", 1200)},
                               {"eval": "document.querySelector('#ck-hlens .tv-live').click(); 1"}, SLEEP(300), {"eval": INFO},
                               {"until": "!document.querySelector('#ck-hlens .tv-rbadge')", "ms": 8000}, {"eval": INFO}])
    mid, done = json.loads(out[-3]), json.loads(out[-1])
    assert mid["badge"] == "returning to live…" and mid["rshown"] not in (None, "live"), mid                     # the frame stays, labelled
    assert done["badge"] is None and done["rshown"] is None and done["replaying"] is False, done


# --- P10: Esc is the card's --------------------------------------------------------------------------------


@needs_browser
def test_escape_with_a_dialog_open_elsewhere_does_not_end_replay_but_inside_the_card_it_does(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    esc = "(() => { %s.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})); return window.LevainTopologyView.replaying(); })()"
    overlay = "(() => { const d = document.createElement('div'); d.className = 'modal-overlay'; document.body.appendChild(d); return 1; })()"
    editable = "(() => { const d = document.createElement('div'); d.contentEditable = 'true'; d.id = 'ce'; document.body.appendChild(d); return 1; })()"
    out = _run(tmp_path, src, [{"until": STRIP}, {"eval": _scrub(0.4)}, WAIT_BADGE, {"eval": overlay}, {"eval": esc % "document.body"},
                               {"eval": editable}, {"eval": esc % "document.getElementById('ce')"},
                               {"eval": esc % "document.querySelector('#ck-hlens .tv-rtrack')"},
                               {"until": "!window.LevainTopologyView.replaying()"}, {"eval": "window.LevainTopologyView.replaying()"}])
    assert out[-6] is True and out[-4] is True, out                                                              # a dialog is open / an editable is typing: replay stays
    assert out[-1] is False, out                                                                                 # Esc inside the card ends it, dialog or not


# --- P11: the histogram reads only for a strip that is on screen --------------------------------------------


@needs_browser
def test_the_histogram_is_not_read_for_a_strip_that_is_not_connected_or_not_visible(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    skew = "(() => { const n = Date.now.bind(Date); Date.now = () => n() + 10 * 60000; return 1; })()"
    opts = "{replay: {readJson: window.LevainCockpitTransport.readJson, resume: () => Promise.resolve()}}"
    detached = "(() => { const d = document.createElement('div'); d.appendChild(window.LevainTopologyView.render(%s, %s)); return 1; })()" % (json.dumps(_value()), opts)
    hidden = ("(() => { const d = document.createElement('div'); d.style.display = 'none'; document.body.appendChild(d); "
              "d.appendChild(window.LevainTopologyView.render(%s, %s)); return 1; })()") % (json.dumps(_value()), opts)
    shown = ("(() => { const d = document.createElement('div'); document.body.appendChild(d); "
             "d.appendChild(window.LevainTopologyView.render(%s, %s)); return 1; })()") % (json.dumps(_value()), opts)
    count = "(window.__urls || []).filter(u => u.includes('activity.json')).length"
    out = _run(tmp_path, src, [{"until": STRIP}, {"until": "document.querySelectorAll('#ck-hlens .tv-rbars i').length > 10"}, SLEEP(500), {"eval": SPY}, {"eval": skew},
                               {"eval": detached}, SLEEP(400), {"eval": count}, {"eval": hidden}, SLEEP(400), {"eval": count}, {"eval": shown}, SLEEP(600), {"eval": count}])
    assert out[-7] == 0 and out[-4] == 0, out                                                                    # nothing for a detached or a display:none strip
    assert out[-1] == 1, out                                                                                     # the one on screen, stale, is read once


# --- P12: focus is taken back only for a read that went out ------------------------------------------------


@needs_browser
def test_a_key_that_ends_live_sets_no_refocus_for_the_next_repaint(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [{"until": STRIP}, {"eval": _key("ArrowRight")}, SLEEP(500),
                               {"eval": "document.querySelector('#ck-hlens [data-tv=agents]').click(); 1"}, {"until": "document.querySelector('#ck-hlens .tv.tv-replaying, #ck-hlens .tv[data-lens=agents]')"},
                               SLEEP(300), {"eval": "document.activeElement && document.activeElement.classList.contains('tv-rtrack')"}])
    assert out[-1] is False, out


# --- FIX6: the return to live (anchor kept, failure kept visible), Esc counts visible overlays, a lens switch leaves replay --------------------


_H = "document.querySelector('#h6 %s')"
# a host the test owns: the real topology module and transport, with a `resume` the test steers (applied / false / reject / hang / superseded)
HOST = ("""(async () => { const V = window.LevainTopologyView; const host = document.createElement('div'); host.id = 'h6'; document.body.appendChild(host);
  window.__mode = 'applied'; window.__resumes = 0; window.__sup = 0;
  const opts = {__h6: true, replay: {readJson: window.LevainCockpitTransport.readJson, resume: () => { window.__resumes++; const m = window.__mode;
    if (m === 'reject') return Promise.reject(new Error('boom')); if (m === 'false') return Promise.resolve(false);
    if (m === 'hang') return new Promise(() => {}); if (m === 'sup') return Promise.resolve('superseded');
    if (m === 'sup-once') return Promise.resolve(window.__sup++ === 0 ? 'superseded' : 'applied'); return Promise.resolve('applied'); }},
    repaint: () => { host.replaceChildren(V.render(V.replayValue() || window.__live, Object.assign({}, opts, {lens: 'agents', lensSeg: V.headerToggle('agents', () => { if (V.replaying()) V.returnToLive(); opts.repaint(); })}))); }};
  const pl = await window.LevainCockpitTransport.readJson('/cockpit/panel/topology.json?profile=full'); window.__live = pl.value; window.__opts = opts; opts.repaint(); return 1; })()""")
H_INFO = ("JSON.stringify({badge: (%s || {}).textContent || null, err: (%s || {}).textContent || null, replaying: window.LevainTopologyView.replaying(), "
          "rshown: (%s || {dataset: {}}).dataset.rshown || null, max: +%s.getAttribute('aria-valuemax'), resumes: window.__resumes, "
          "urls: (window.__urls || []).filter(u => u.includes('at=')).length})") % (
    _H % ".tv-rbadge", _H % ".tv-rerr", _H % ".tv", _H % ".tv-rtrack")
H_SCRUB = _scrub(0.4).replace("#ck-hlens .tv-rtrack", "#h6 .tv-rtrack")
# the host paints first, so the module's one `opts` slot is the host's when the click calls returnToLive: every repaint after that is production's own
H_LIVE = "window.__opts.repaint(); document.querySelector('#h6 .tv-live').click(); 1"
# the page's own card also owns that slot whenever it repaints; for a wait of seconds it is kept out of the way (it draws an empty card) so the slot stays the host's
ISOLATE = {"eval": "(() => { const V = window.LevainTopologyView, o = V.render; V.render = function (v, op) { return op && op.__h6 ? o.apply(this, arguments) : document.createElement('div'); }; return 1; })()"}
H_PAUSED = {"until": "((document.querySelector('#h6 .tv-rbadge') || {textContent: ''}).textContent).includes('live paused')", "ms": 8000}


def _host_run(tmp_path, monkeypatch, steps, tag="a"):
    (tmp_path / tag).mkdir(exist_ok=True)
    _root, src = _sessions_fixture(tmp_path / tag, monkeypatch)
    return _run(tmp_path, src, [{"until": STRIP}, {"eval": HOST}, {"eval": SPY}, *steps])


@needs_browser
def test_a_scrub_while_returning_to_live_keeps_the_live_anchors_window(tmp_path, monkeypatch):
    out = _host_run(tmp_path, monkeypatch, [{"eval": H_SCRUB}, H_PAUSED, {"eval": H_INFO}, {"eval": "window.__mode = 'hang'; 1"}, {"eval": H_LIVE}, SLEEP(200), {"eval": H_INFO}])
    before, mid = json.loads(out[-5]), json.loads(out[-1])
    assert mid["badge"] == "returning to live…", mid
    assert abs(mid["max"] - before["max"]) <= 120000, (before, mid)          # the window is the live clock's, not the historical frame's as_of (hours earlier)


@needs_browser
def test_a_return_to_live_that_resolves_false_or_rejects_keeps_the_frame_names_the_error_and_the_live_pill_retries(tmp_path, monkeypatch):
    for mode in ("false", "reject"):
        out = _host_run(tmp_path, monkeypatch, tag=mode, steps=[{"eval": H_SCRUB}, H_PAUSED, {"eval": "window.__mode = '%s'; 1" % mode}, {"eval": H_LIVE},
                                                {"until": "!!document.querySelector('#h6 .tv-rerr')", "ms": 8000}, {"eval": H_INFO},
                                                {"eval": "window.__mode = 'applied'; 1"}, {"eval": H_LIVE}, {"until": "!window.LevainTopologyView.replaying()", "ms": 8000}, {"eval": H_INFO}])
        failed, done = json.loads(out[-5]), json.loads(out[-1])
        assert failed["err"].startswith("could not return to live: "), failed
        assert failed["replaying"] is True and failed["rshown"] not in (None, "live"), failed                    # the past frame stays, marked; the stale live value is never painted
        assert done["replaying"] is False and done["badge"] is None and done["err"] is None, done                 # the retry lands


@needs_browser
def test_a_superseded_live_read_is_followed_and_only_a_standing_one_is_an_error(tmp_path, monkeypatch):
    out = _host_run(tmp_path, monkeypatch, [{"eval": H_SCRUB}, H_PAUSED, {"eval": "window.__mode = 'sup-once'; 1"}, {"eval": H_LIVE},
                                            {"until": "!window.LevainTopologyView.replaying()", "ms": 8000}, {"eval": H_INFO}])
    done = json.loads(out[-1])
    assert done["resumes"] == 2 and done["replaying"] is False, done                                              # asked again, the second landed
    out = _host_run(tmp_path, monkeypatch, [{"eval": H_SCRUB}, H_PAUSED, {"eval": "window.__mode = 'sup'; 1"}, {"eval": H_LIVE},
                                            {"until": "!!document.querySelector('#h6 .tv-rerr')", "ms": 8000}, {"eval": H_INFO}], tag="b")
    stuck = json.loads(out[-1])
    assert stuck["replaying"] is True and "superseded" in stuck["err"], stuck


@needs_browser
def test_escape_outside_the_card_ignores_a_hidden_dialog_but_not_a_visible_one(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    esc = "(() => { document.body.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})); return window.LevainTopologyView.replaying(); })()"
    hidden = "(() => { const d = document.createElement('div'); d.setAttribute('role', 'dialog'); d.style.display = 'none'; document.body.appendChild(d); return 1; })()"
    shown = ("(() => { const d = document.createElement('div'); d.setAttribute('role', 'dialog'); d.id = 'dlg'; d.style.cssText = 'position:fixed;top:0;left:0;width:50px;height:50px'; "
             "document.body.appendChild(d); return 1; })()")
    out = _run(tmp_path, src, [{"until": STRIP}, {"eval": _scrub(0.4)}, WAIT_BADGE, {"eval": shown}, {"eval": esc}, {"eval": "document.getElementById('dlg').remove(); 1"},
                               {"eval": hidden}, {"eval": esc}, {"until": "!window.LevainTopologyView.replaying()", "ms": 8000}, {"eval": "window.LevainTopologyView.replaying()"}])
    assert out[-6] is True, out                                   # a visible dialog keeps its own Esc
    assert out[-1] is False, out                                  # a hidden one does not swallow the card's (FIX7: the return to live is async, so this waits for it)


@needs_browser
def test_switching_lens_while_replaying_leaves_replay_and_the_strip_is_live_in_the_new_lens(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [{"until": STRIP}, {"eval": _scrub(0.4)}, WAIT_BADGE, {"eval": "document.querySelector('#ck-hlens [data-tv=work]').click(); 1"},
                               {"until": "document.querySelector('#ck-hlens .tv[data-lens=work]')"}, SLEEP(300), {"eval": INFO}])
    got = json.loads(out[-1])
    assert got["replaying"] is False and got["paused"] is False and got["badge"] is None and got["rat"] is None, got


# --- FIX7: ONE return path (returnToLive): the stale pre-replay live value is never painted unmarked --------------------------------------------


@needs_browser
def test_the_live_retry_after_a_failure_issues_a_new_read_and_never_shows_the_unlabelled_stale_value(tmp_path, monkeypatch):
    out = _host_run(tmp_path, monkeypatch, [{"eval": H_SCRUB}, H_PAUSED, {"eval": "window.__mode = 'false'; 1"}, {"eval": H_LIVE},
                                            {"until": "!!document.querySelector('#h6 .tv-rerr')", "ms": 15000}, {"eval": H_INFO},   # only production's own repaint can put the failed label on the host card
                                            {"eval": "window.__mode = 'hang'; 1"}, {"eval": H_LIVE}, SLEEP(200), {"eval": H_INFO}])
    failed, retry = json.loads(out[-5]), json.loads(out[-1])
    assert failed["resumes"] == 1 and "could not return to live" in failed["badge"] and "Live tries again" in failed["badge"], failed
    assert retry["resumes"] == 2, retry                                                                           # the retry asked the page for a new live read
    assert retry["replaying"] is True and retry["badge"] == "returning to live…" and retry["rshown"] not in (None, "live"), retry   # still the labelled frame


@needs_browser
def test_a_lens_switch_keeps_the_labelled_frame_until_the_live_read_applies(tmp_path, monkeypatch):
    out = _host_run(tmp_path, monkeypatch, [{"eval": H_SCRUB}, H_PAUSED, {"eval": "window.__mode = 'hang'; 1"},
                                            {"eval": "document.querySelector('#h6 [data-tv=work]').click(); 1"},
                                            # the precondition: the host card is in replay, badged (waited for on that state, not a fixed sleep)
                                            {"until": "window.LevainTopologyView.replaying() && window.__resumes === 1 && !!document.querySelector('#h6 .tv-rbadge')"}, {"eval": H_INFO},
                                            {"eval": "window.__mode = 'applied'; 1"}, {"eval": H_LIVE},
                                            # nothing here repaints the card: the badge leaves only if production's own repaint (when the live read applies) redraws it
                                            {"until": "!document.querySelector('#h6 .tv-rbadge')", "ms": 8000}, {"eval": H_INFO}])
    mid, done = json.loads(out[-5]), json.loads(out[-1])
    assert mid["replaying"] is True and mid["badge"] == "returning to live…" and mid["rshown"] not in (None, "live") and mid["resumes"] == 1, mid
    assert done["replaying"] is False and done["badge"] is None, done


@needs_browser
def test_a_hung_resume_times_out_to_the_failed_label_and_live_tries_again(tmp_path, monkeypatch):
    out = _host_run(tmp_path, monkeypatch, [{"eval": H_SCRUB}, H_PAUSED, ISOLATE, {"eval": "window.__mode = 'hang'; 1"}, {"eval": H_LIVE},
                                            {"until": "!!document.querySelector('#h6 .tv-rerr')", "ms": 15000}, {"eval": H_INFO},   # only production's own timeout repaint can draw the failed label
                                            {"eval": "window.__mode = 'applied'; 1"}, {"eval": H_LIVE}, {"until": "!document.querySelector('#h6 .tv-rbadge') && !window.LevainTopologyView.replaying()", "ms": 8000}, {"eval": H_INFO}])
    failed, done = json.loads(out[-5]), json.loads(out[-1])
    assert "timed out" in failed["err"] and failed["replaying"] is True and failed["rshown"] not in (None, "live"), failed
    assert "Live tries again" in failed["badge"], failed
    assert done["replaying"] is False and done["resumes"] == 2 and done["badge"] is None, done


@needs_browser
def test_a_visibility_hidden_dialog_does_not_block_escape(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    esc = "(() => { document.body.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})); return window.LevainTopologyView.replaying(); })()"
    hidden = ("(() => { const d = document.createElement('div'); d.setAttribute('role', 'dialog'); d.style.cssText = 'position:fixed;top:0;left:0;width:50px;height:50px;visibility:hidden'; "
              "document.body.appendChild(d); return 1; })()")
    out = _run(tmp_path, src, [{"until": STRIP}, {"eval": _scrub(0.4)}, WAIT_BADGE, {"eval": hidden}, {"eval": esc}, {"until": "!window.LevainTopologyView.replaying()", "ms": 8000},
                               {"eval": "window.LevainTopologyView.replaying()"}])
    assert out[-1] is False, out                                                                                 # the hidden dialog did not swallow the card's Esc


# --- FIX8: Esc in the returning / failed state, the timer of a superseded return, the histogram's one read per minute ---------------------------


@needs_browser
def test_escape_works_in_the_returning_and_the_failed_return_state(tmp_path, monkeypatch):
    esc = "(() => { document.body.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})); return 1; })()"
    out = _host_run(tmp_path, monkeypatch, tag="a", steps=[{"eval": H_SCRUB}, H_PAUSED, {"eval": "window.__mode = 'hang'; 1"}, {"eval": H_LIVE}, SLEEP(100), {"eval": H_INFO},
                                                           {"eval": "window.__mode = 'applied'; 1"}, {"eval": esc}, {"until": "!window.LevainTopologyView.replaying()", "ms": 8000}, {"eval": H_INFO}])
    mid, done = json.loads(out[-5]), json.loads(out[-1])
    assert mid["badge"] == "returning to live…", mid                       # Esc is pressed while at === null, ret === true
    assert done["replaying"] is False and done["resumes"] == 2, done
    out = _host_run(tmp_path, monkeypatch, tag="b", steps=[{"eval": H_SCRUB}, H_PAUSED, {"eval": "window.__mode = 'false'; 1"}, {"eval": H_LIVE},
                                                           {"until": "!!document.querySelector('#h6 .tv-rerr')", "ms": 8000}, {"eval": H_INFO},
                                                           {"eval": "window.__mode = 'applied'; 1"}, {"eval": esc}, {"until": "!window.LevainTopologyView.replaying()", "ms": 8000}, {"eval": H_INFO}])
    failed, done = json.loads(out[-5]), json.loads(out[-1])
    assert failed["err"] and failed["replaying"] is True, failed
    assert done["replaying"] is False and done["resumes"] == 2, done


@needs_browser
def test_a_scrub_that_supersedes_a_return_clears_the_returns_timer(tmp_path, monkeypatch):
    spy = ("(() => { window.__set = []; window.__clr = []; const s = window.setTimeout.bind(window), c = window.clearTimeout.bind(window); "
           "window.setTimeout = (f, ms, ...a) => { const id = s(f, ms, ...a); if (ms === 10000) window.__set.push(id); return id; }; "
           "window.clearTimeout = (id) => { window.__clr.push(id); return c(id); }; return 1; })()")
    scrub2 = H_SCRUB.replace("0.4", "0.7") if "0.4" in H_SCRUB else H_SCRUB
    out = _host_run(tmp_path, monkeypatch, [{"eval": H_SCRUB}, H_PAUSED, {"eval": "window.__mode = 'hang'; 1"}, {"eval": spy}, {"eval": H_LIVE}, SLEEP(100),
                                            {"eval": scrub2},
                                            {"until": "window.__set.length > 0 && window.__clr.includes(window.__set[window.__set.length - 1])"}, {"eval": "JSON.stringify({set: window.__set, clr: window.__clr})"}])
    got = json.loads(out[-1])
    assert got["set"] and got["set"][-1] in got["clr"], got                  # the return's timer was cleared by the scrub, not left to fire into a dead token


@needs_browser
def test_a_visible_replay_strip_reads_the_histogram_at_most_once_a_minute_and_never_per_repaint(tmp_path, monkeypatch):
    skew = "(() => { const n = Date.now.bind(Date); Date.now = () => n() + 61000; return 1; })()"
    repaints = "(async () => { for (let i = 0; i < 6; i++) { window.__opts.repaint(); await new Promise(r => setTimeout(r, 150)); } return 1; })()"
    count = "(window.__urls || []).filter(u => u.includes('activity.json')).length"
    out = _host_run(tmp_path, monkeypatch, [{"eval": "(window.__urls = [], 1)"}, {"eval": repaints}, SLEEP(300), {"eval": count},
                                            {"eval": skew}, {"eval": repaints}, SLEEP(500), {"eval": count}])
    assert out[-5] <= 1, out                                                   # six repaints inside the minute: no read per repaint
    assert out[-1] - out[-5] == 1, out                                         # the minute passed: six more repaints, exactly one read
