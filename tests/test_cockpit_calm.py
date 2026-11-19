"""Calm: one page control sets ``html.calm`` and the page reads as if the OS asked for reduced motion.

The class is the one predicate: dashboard.css, topology_view.js's particle loop, sky.js's ambient meteors and the age timer all test it. The
choice is kept per browser (absent storage = full). The page tests run the real cockpit in headless Chrome (the agents graph with live edges,
as test_cockpit_k10_agents does); the age-timer test uses the page's fake DOM."""
from __future__ import annotations

import json

from tests._ke_dom import NODE, run_node
from tests.test_cockpit_f12b_topology_view import TV, _paint, _run
from tests.test_cockpit_flip_parity import needs_browser
from tests.test_cockpit_k10_agents import PRIME, _graph_value, _probe
from tests.test_cockpit_k7_build import _sessions_fixture

import pytest

# The phone first-row geometry this measures (ROW1_AT_13C4D885, with the Modes bar's `.seg` as the second row) is gone with the Modes bar, and Home
# hides the band's own Calm button (#calmphone): Calm's phone reach is a boarded row (Calm at 480 px and under). The test comes back with that row,
# measured again on the places page.
WAIT = {"eval": "new Promise(r => setTimeout(r, 300))"}
HTML = "document.documentElement.classList.contains('calm')"
BTN = "(() => { const b = document.getElementById('calmbtn'); return JSON.stringify([b.getAttribute('aria-pressed'), b.textContent]); })()"
CLICK = {"eval": "document.getElementById('calmbtn').click(); 1"}
# a probe element that asks for an infinite animation: under Calm the page's one rule must win over it
ANIM = ("(() => { const d = document.createElement('div'); d.style.animation = 'spin 1s linear infinite'; document.body.appendChild(d); "
        "const n = getComputedStyle(d).animationName; d.remove(); return n; })()")


@needs_browser
def test_absent_storage_is_full_motion_with_the_control_off(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_graph_value(), "agents")}, WAIT, {"eval": HTML}, {"eval": BTN}, {"eval": ANIM}, {"eval": _probe()}])
    assert out[-4] is False and json.loads(out[-3]) == ["false", "Calm off"], out
    assert out[-2] == "spin", out
    assert json.loads(out[-1])["stage"]["motion"] == "on", out


@needs_browser
def test_the_control_stills_the_graph_the_sky_rule_and_every_animation_at_once(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_graph_value(), "agents")}, WAIT, {"eval": _probe()},
                               CLICK, WAIT, {"eval": HTML}, {"eval": BTN}, {"eval": ANIM}, {"eval": _probe()}])
    before, after = json.loads(out[-7])["stage"], json.loads(out[-1])["stage"]
    assert before["motion"] == "on" and int(before["particles"]) > 0, before
    assert out[-4] is True and json.loads(out[-3]) == ["true", "Calm on"], out
    assert out[-2] == "none", out                                  # the CSS reader
    assert after["motion"] == "off" and after["particles"] == "0" and after["streams"] == "2", after   # the graph reader: running loop stops, edges still count


@needs_browser
def test_turning_calm_off_again_resumes_the_stream(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_graph_value(), "agents")}, WAIT, CLICK, WAIT, CLICK, WAIT, {"eval": HTML}, {"eval": _probe()}])
    got = json.loads(out[-1])["stage"]
    assert out[-2] is False and got["motion"] == "on" and int(got["particles"]) > 0, out


@needs_browser
def test_the_choice_is_remembered_across_a_reload_and_a_page_painted_under_it_draws_no_stream(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [CLICK, {"eval": "localStorage.getItem('levain_cockpit_calm')"},
                               {"eval": "location.reload(); 1"}, {"until": HTML + " && document.getElementById('calmbtn') !== null"},
                               *PRIME, {"eval": _paint(_graph_value(), "agents")}, WAIT, {"eval": BTN}, {"eval": _probe()}])
    assert out[2] == "1", out
    assert json.loads(out[-2]) == ["true", "Calm on"], out
    assert json.loads(out[-1])["stage"]["motion"] == "off", out


@needs_browser
def test_a_graph_drawn_under_calm_starts_flowing_when_the_control_turns_it_off(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [CLICK, {"eval": "location.reload(); 1"}, {"until": HTML + " && document.getElementById('calmbtn') !== null"},
                               *PRIME, {"eval": _paint(_graph_value(), "agents")}, WAIT, {"eval": _probe()}, CLICK, WAIT, {"eval": _probe()}])
    still, flowing = json.loads(out[-4])["stage"], json.loads(out[-1])["stage"]
    assert still["motion"] == "off" and still["particles"] == "0", still
    assert flowing["motion"] == "on" and int(flowing["particles"]) > 0, flowing


@needs_browser
def test_calm_off_removes_the_stored_choice(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [CLICK, CLICK, {"eval": "String(localStorage.getItem('levain_cockpit_calm'))"}, {"eval": HTML}])
    assert out[-2] == "null" and out[-1] is False, out


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_age_timer_slows_under_calm_and_is_never_stopped(tmp_path):
    out = run_node(r"""
const timers = [], cleared = [];
globalThis.setInterval = (fn, ms) => { timers.push(ms); return timers.length; };
globalThis.clearInterval = (id) => cleared.push(id);
document.documentElement = new N("html");
N.prototype.isConnected = true;
const regions = { hero: [], brief: [], widgets: ["health"], header: [], zones: [{ id: "operate", title: "Work", grid: 3, panels: [], cards: [] }] };
const panel = { metrics: [{ label: "write path", value: "live", status: "ok" }, { label: "last save at", value: "2026-10-12T11:55:00+00:00", status: "unknown" }] };
const m = fixture({ health: head("health", "status-dots", "Health") }, { health: head("health", "status-dots", "Health", { value: panel }) }, { regions });
K.fill(new N("div"), V.fromManifest(m).extra_panels.health, { entry: { title: "health" } });
out.full = timers.slice(); out.calm0 = K.isCalm();
K.setCalm(true, false);
out.calm1 = K.isCalm(); out.after = timers.slice(); out.armed = K.ageTimerArmed(); out.cleared = cleared.length;
out.tick = [K.AGE_TICK_MS, K.CALM_AGE_TICK_MS];
""", tmp_path)
    assert out["full"] == [out["tick"][0]] and out["calm0"] is False
    assert out["calm1"] is True and out["after"] == [out["tick"][0], out["tick"][1]] and out["armed"] is True and out["cleared"] == 1
    assert out["tick"][1] > out["tick"][0]


# the first row's element rects at 13c4d885 (before any Calm control), measured in a scratch checkout of that commit with this test's emulation
# (fixture of test_cockpit_k7_build, 800 px tall, dpr 2): [left, top, right, bottom]
ROW1_AT_13C4D885 = {
    320: {"brand": [28, 38, 50, 54], "live": [54, 43, 61, 50], "sync": [65, 24, 120, 68], "jump": [124, 24, 168, 68], "theme": [172, 24, 292, 68], "tabs": [28, 76, 305, 124]},
    375: {"brand": [28, 38, 105, 54], "live": [109, 43, 116, 50], "sync": [120, 24, 175, 68], "jump": [179, 24, 223, 68], "theme": [227, 24, 347, 68], "tabs": [28, 76, 347, 124]},
    430: {"brand": [28, 38, 144, 54], "live": [152, 43, 159, 50], "sync": [167, 24, 222, 68], "jump": [230, 24, 274, 68], "theme": [282, 24, 402, 68], "tabs": [28, 76, 402, 124]},
}


ROW1_ORDER = ("brand", "live", "sync", "jump", "theme")   # the controls that share the first row, left to right; the tabs are the second row


def _row1_defects(row1: dict, width: int) -> list:
    """Font-independent geometry of the first row (rects are [left, top, right, bottom]): the defects found, [] when it is sound."""
    rects = [(k, row1[k]) for k in ROW1_ORDER]
    bad = []
    for (ka, a), (kb, b) in zip(rects, rects[1:]):
        if b[0] < a[0]:
            bad.append(f"{kb} is left of {ka}")
        if a[2] > b[0]:
            bad.append(f"{ka} overlaps {kb} horizontally ({a[2]} > {b[0]})")
    for k, r in rects:
        if r[0] < 0 or r[2] > width:
            bad.append(f"{k} leaves the {width}px viewport ({r[0]}..{r[2]})")
    centres = [(k, (r[1] + r[3]) / 2) for k, r in rects]
    bound = max(r[3] - r[1] for _, r in rects) / 2   # centres of one row differ by less than half its tallest control
    if max(c for _, c in centres) - min(c for _, c in centres) >= bound:
        bad.append(f"the controls do not share one row (centres {centres}, bound {bound})")
    tabs = row1["tabs"]   # the second row: no control of the first may reach into it (touching edges is allowed)
    for k, r in rects:
        if r[3] > tabs[1] and r[0] < tabs[2] and tabs[0] < r[2]:
            bad.append(f"{k} overlaps the tabs ({k} bottom {r[3]} > tabs top {tabs[1]})")
    return bad


@pytest.mark.parametrize("width,mutate,expect", [
    (375, lambda r: r.update(sync=[r["sync"][0] + 10, r["sync"][1], r["sync"][2] + 10, r["sync"][3]]), "sync overlaps jump"),
    (375, lambda r: r.update(theme=[r["theme"][0] + 40, r["theme"][1], r["theme"][2] + 40, r["theme"][3]]), "theme leaves the 375px viewport"),
    (375, lambda r: r.update(jump=[r["jump"][0], r["jump"][1] + 30, r["jump"][2], r["jump"][3] + 30]), "do not share one row"),
    (375, lambda r: r.update(live=[10, r["live"][1], 17, r["live"][3]]), "is left of"),
    (375, lambda r: r.update(jump=[r["jump"][0], r["jump"][1] + 21, r["jump"][2], r["jump"][3] + 21]), "jump overlaps the tabs"),
])
def test_the_row_invariants_catch_a_real_defect_and_pass_the_known_good_row(width, mutate, expect):
    good = {k: list(v) for k, v in ROW1_AT_13C4D885[width].items()}
    assert _row1_defects(good, width) == []
    mutate(good)
    assert any(expect in d for d in _row1_defects(good, width)), _row1_defects(good, width)




# ---- C11: Calm suppresses the sky's EFFECTIVE motion and never writes the user's Ambient preference ----------------------
# sky.js runs whole on a fake canvas (the harness of test_cockpit_sky.py, cut before its own mount); `pending` is the count of animation
# frames the sky has asked for, which a launched meteor raises and a still sky leaves at 0.

def _sky_calm_script() -> str:
    from tests.test_cockpit_sky import _FAKE
    head = _FAKE.split("const h = sky.mount")[0]
    return head + r"""
const wl = {};
global.addEventListener = (t, f) => { (wl[t] = wl[t] || []).push(f); };
let calm = !!I.calmAtMount;
document.documentElement.classList = { contains: c => c === 'calm' && calm };
const setCalm = on => { calm = on; (wl['levain-calm'] || []).forEach(f => f({})); };
// the OS reduced-motion query, flippable mid-run; every other media query answers no
const mq = { matches: !!I.reduceAtMount, ls: [], addEventListener(t, f) { this.ls.push(f); }, removeEventListener(t, f) { this.ls = this.ls.filter(g => g !== f); } };
global.matchMedia = q => String(q).includes('prefers-reduced-motion') ? mq : { matches: false, addEventListener() {}, removeEventListener() {} };
const setReduce = on => { mq.matches = on; mq.ls.forEach(f => f({ matches: on })); };
const h = sky.mount(new El('div'), mk(I.base), Object.assign({ now: () => NOW }, I.opts || {}));
const toggle = all.find(e => e.attrs['class'] === 'sky-toggle');
const pressed = () => toggle.attrs['aria-pressed'];
let n = 0;
const commit = () => mk(I.base.concat([{ kind: 'commit', at: new Date(NOW - 60000 * (++n)).toISOString(), repo: 'tally', agent: null, outcome: 'ok' }]));
const out = { log: [] };
for (const s of I.steps) {
  if (s === 'calm_on') setCalm(true);
  else if (s === 'calm_off') setCalm(false);
  else if (s === 'reduce_on') setReduce(true);
  else if (s === 'reduce_off') setReduce(false);
  else if (s === 'click') toggle.listeners.click.forEach(f => f({}));
  else if (s === 'commit') { const before = sky.stats.pending; h.update(commit()); out.log.push(['launch', sky.stats.pending - before]); }
  out.log.push([s, pressed()]);
}
h.destroy();
console.log(JSON.stringify(out));
"""


def _sky_calm(tmp_path, steps, calm_at_mount=False, reduce_at_mount=False):
    import subprocess
    from tests.test_cockpit_sky import NODE, _LEVAIN_FEED, _NOW, SKY
    script = tmp_path / "sky_calm.cjs"
    script.write_text(_sky_calm_script())
    payload = {"sky": str(SKY), "base": _LEVAIN_FEED, "feeds": [_LEVAIN_FEED], "now": _NOW, "steps": steps, "calmAtMount": calm_at_mount, "reduceAtMount": reduce_at_mount}
    out = subprocess.run([NODE, str(script)], input=json.dumps(payload), capture_output=True, text=True, check=True).stdout
    return json.loads(out.strip().splitlines()[-1])["log"]


def _launched(log):
    return [d for k, d in log if k == "launch"]


def test_ambient_toggled_under_calm_records_the_preference_and_the_sky_stays_still_until_calm_is_off(tmp_path):
    # order 1: Calm on, then Ambient pressed twice (off, then on again), then Calm off
    log = _sky_calm(tmp_path, ["calm_on", "click", "commit", "click", "commit", "calm_off", "commit"])
    states = [d for k, d in log if k in ("calm_on", "click", "calm_off")]
    assert states == ["true", "false", "true", "true"], log           # Calm itself leaves the preference pressed; each click is recorded
    assert _launched(log) == [0, 0, 1], log                            # still under Calm whatever Ambient says; motion back per the preference


def test_calm_off_restores_motion_per_the_preference_set_while_it_was_on(tmp_path):
    # order 2: Calm on, Ambient off while it is on, Calm off -> the preference (off) holds, the sky stays still
    log = _sky_calm(tmp_path, ["calm_on", "click", "calm_off", "commit"])
    assert [d for k, d in log if k in ("click", "calm_off")] == ["false", "false"], log
    assert _launched(log) == [0], log


def test_ambient_off_before_calm_stays_off_after_calm_and_a_page_mounted_under_calm_shows_the_preference_on(tmp_path):
    log = _sky_calm(tmp_path, ["click", "calm_on", "calm_off", "commit"])
    assert [d for k, d in log if k in ("click", "calm_on", "calm_off")] == ["false", "false", "false"] and _launched(log) == [0], log
    mounted = _sky_calm(tmp_path, ["commit", "calm_off", "commit"], calm_at_mount=True)
    assert _launched(mounted) == [0, 1] and [d for k, d in mounted if k == "calm_off"] == ["true"], mounted


# ---- C13: the OS reduced-motion setting suppresses motion the way Calm does and never writes the Ambient preference ---------

def test_os_reduced_motion_on_then_off_restores_the_ambient_preference(tmp_path):
    log = _sky_calm(tmp_path, ["reduce_on", "commit", "reduce_off", "commit"])
    assert [d for k, d in log if k in ("reduce_on", "reduce_off")] == ["true", "true"], log     # the preference is untouched
    assert _launched(log) == [0, 1], log                                                           # still while the OS asks; motion back after


def test_a_page_mounted_under_os_reduced_motion_moves_once_the_os_stops_asking(tmp_path):
    log = _sky_calm(tmp_path, ["commit", "reduce_off", "commit"], reduce_at_mount=True)
    assert _launched(log) == [0, 1] and [d for k, d in log if k == "reduce_off"] == ["true"], log


def test_reduced_motion_and_calm_each_hold_the_sky_still_and_ambient_off_survives_both(tmp_path):
    log = _sky_calm(tmp_path, ["reduce_on", "calm_on", "reduce_off", "commit", "calm_off", "commit"])
    assert _launched(log) == [0, 1], log                       # Calm still holds after the OS lets go; motion only once both are off
    off = _sky_calm(tmp_path, ["click", "reduce_on", "reduce_off", "commit"])
    assert [d for k, d in off if k in ("click", "reduce_off")] == ["false", "false"] and _launched(off) == [0], off
