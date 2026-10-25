"""The open decision mark breathes (Phill 10-25): the badge (and the desk's leader line) carry .tv-needpulse while a decision is open, the class is
on nothing once needs_open is 0 or absent, and a reduced-motion phone gets today's still mark. Real page in headless Chrome."""
from __future__ import annotations

import json
import re
from pathlib import Path

from tests.test_cockpit_f12b_topology_view import TV, _paint, _value
from tests.test_cockpit_flip_parity import needs_browser
from tests.test_cockpit_needsbadge import _needy
from tests.test_cockpit_seatmsg import PRIME, _run

PULSING = "JSON.stringify({badges: [...%s.querySelectorAll('.tv-needbadge')].map(b => b.classList.contains('tv-needpulse')), lead: %s.querySelectorAll('.tv-bz-lead.tv-needpulse').length, all: %s.querySelectorAll('.tv-needpulse').length})" % (TV, TV, TV)
DELAY = "JSON.stringify([...%s.querySelectorAll('.tv-needbadge, .tv-bz-lead')].map(e => getComputedStyle(e).animationDelay))" % TV
WEB = Path(__file__).resolve().parents[1] / "levain" / "templates" / "web"
ANIM = "JSON.stringify([...%s.querySelectorAll('.tv-needbadge, .tv-bz-lead')].map(e => getComputedStyle(e).animationName))" % TV


@needs_browser
def test_the_pulse_class_is_on_the_open_marks_only_and_leaves_with_the_decision(tmp_path, monkeypatch):
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_needy(), "agents")}, {"eval": PULSING}, {"eval": _paint(_value(), "agents")}, {"eval": PULSING}])
    open_, answered = json.loads(out[-3]), json.loads(out[-1])
    assert open_["badges"] == [True, True] and open_["lead"] == 1 and open_["all"] == 3, open_       # the desk's and the seat's badge, plus the desk's leader line
    assert answered == {"badges": [], "lead": 0, "all": 0}, answered


@needs_browser
def test_the_pulse_runs_unless_the_phone_asks_for_reduced_motion(tmp_path, monkeypatch):
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_needy(), "agents")}, {"motion": "no-preference"}, {"eval": ANIM}, {"motion": "reduce"}, {"eval": ANIM}])
    moving, still = json.loads(out[-2]), json.loads(out[-1])
    assert sorted(moving) == ["tv-needlead", "tv-needpulse", "tv-needpulse"], moving
    assert still == ["none", "none", "none"], still


@needs_browser
def test_the_delay_is_the_time_already_played_so_a_repaint_does_not_restart_the_breath(tmp_path, monkeypatch):
    got = _probe(tmp_path, monkeypatch, [
        (None, {"eval": CLOCK}), (None, _drawn(_needy())), (None, {"eval": SKEW % 10_000}), (None, _drawn(_needy())), ("d", {"eval": DELAY})])
    delays = [float(d.rstrip("s")) for d in json.loads(got["d"])]
    css = (WEB / "dashboard.css").read_text()
    period = float(re.search(r"tv-needpulse\s+([\d.]+)s", css).group(1))
    cycles = int(re.search(r"tv-needpulse [\d.]+s ease-in-out (\d+)", css).group(1))
    assert len(delays) == 3 and all(-period * cycles < d < -9.5 and d > -11 for d in delays), (delays, period, cycles)       # 10 s of the breath already played: a restart-from-zero repaint would read 0


@needs_browser
def test_a_frame_from_the_ledger_is_still_nothing_breathes(tmp_path, monkeypatch):
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    replayed = dict(_needy(), replay=True)
    out = _run(tmp_path, src, [*PRIME, {"motion": "no-preference"}, {"eval": _paint(replayed, "agents")}, {"eval": "%s.classList.contains('tv-replaying')" % TV}, {"eval": ANIM}])
    assert out[-2] is True, out
    assert json.loads(out[-1]) == ["none", "none", "none"], out[-1]               # the two badges and the desk's leader line


def test_the_period_has_one_home_in_the_css_and_the_script_agrees():
    css = (WEB / "dashboard.css").read_text()
    js = (WEB / "topology_view.js").read_text()
    seconds, cycles = re.search(r"animation: tv-needpulse ([\d.]+)s ease-in-out (\d+)", css).groups()
    millis, count = re.search(r"const PULSE_MS = (\d+), PULSE_CYCLES = (\d+)", js).groups()
    assert (int(millis), int(count)) == (round(float(seconds) * 1000), int(cycles)), (millis, count, seconds, cycles)


CLOCK = "(() => { const o = performance.now.bind(performance); window.__skew = 0; performance.now = () => o() + window.__skew; return 1; })()"
SKEW = "(() => { window.__skew += %d; return 1; })()"
COUNT = "%s.querySelectorAll('.tv-needpulse').length" % TV
FRAMES = "(() => { window.__f = 0; requestAnimationFrame(() => requestAnimationFrame(() => { window.__f = 1; })); return 1; })()"


def _more(v: dict) -> dict:
    """The same value with the desk's count risen by one (a new open decision behind the first)."""
    v = json.loads(json.dumps(v))
    v["hub"]["needs_open"] += 1
    return v


def _drawn(v: dict) -> list[dict]:
    """Paint v, then wait for the resize observer's build at the real width: the build the person sees, the one that commits what it showed."""
    return [{"eval": _paint(v, "agents")}, {"eval": FRAMES}, {"until": "window.__f === 1"}]


def _probe(tmp_path, monkeypatch, plan):
    """plan: a list of (label, step or steps); returns {label: value} for the labelled steps (every step but `motion` collects a value)."""
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    steps, at = [], {}
    for label, st in plan:
        for one in (st if isinstance(st, list) else [st]):
            steps.append(one)
            if label and one is (st[-1] if isinstance(st, list) else st):
                at[label] = len([x for x in steps if "motion" not in x]) - 1
    out = _run(tmp_path, src, [*PRIME, {"motion": "no-preference"}, *steps])
    base = len(out) - len([x for x in steps if "motion" not in x])
    return {k: out[base + i] for k, i in at.items()}


@needs_browser
def test_a_decision_breathes_once_and_a_repaint_after_its_cycles_leaves_the_mark_still(tmp_path, monkeypatch):
    got = _probe(tmp_path, monkeypatch, [
        (None, {"eval": CLOCK}), (None, _drawn(_needy())), ("n0", {"eval": COUNT}), ("d0", {"eval": DELAY}),
        (None, {"eval": SKEW % 10_000}), (None, _drawn(_needy())), ("n10", {"eval": COUNT}), ("d10", {"eval": DELAY}),
        (None, {"eval": SKEW % 30_000}), (None, _drawn(_needy())), ("n40", {"eval": COUNT}), ("p40", {"eval": PULSING})])
    assert got["n0"] == 3, got                                                                          # the desk's badge and leader line, the seat's badge
    assert all(-0.5 < float(d.rstrip("s")) <= 0 for d in json.loads(got["d0"])), got["d0"]              # just armed: nothing played yet
    assert got["n10"] == 3, got                                                                         # a repaint 10 s in: still breathing ...
    assert all(9.5 < -float(d.rstrip("s")) < 11 for d in json.loads(got["d10"])), got["d10"]            # ... from where it was, not from the start
    assert got["n40"] == 0, got                                                                         # a repaint at 40 s: the 32 s are spent, the mark is still
    assert json.loads(got["p40"])["badges"] == [False, False], got["p40"]


@needs_browser
def test_a_new_decision_breathes_again_an_answered_one_that_returns_too_and_an_answer_alone_does_not(tmp_path, monkeypatch):
    spent = (None, {"eval": SKEW % 40_000})
    got = _probe(tmp_path, monkeypatch, [
        (None, {"eval": CLOCK}), (None, _drawn(_needy())), spent,
        (None, _drawn(_needy())), ("still", {"eval": COUNT}),                                              # a repaint of the spent decision: still
        (None, _drawn(_more(_needy()))), ("risen", {"eval": COUNT}),                                       # the desk's count rises: a new decision (the desk's two parts breathe, the seat's does not)
        spent, (None, _drawn(_needy())), ("fell", {"eval": COUNT}),                                        # the count falls back: an answer, not a new decision
        (None, _drawn(_value())), (None, _drawn(_needy())), ("back", {"eval": COUNT})])                    # everything answered, then a decision returns
    assert (got["still"], got["risen"], got["fell"], got["back"]) == (0, 2, 0, 3), got
