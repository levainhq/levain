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
def test_the_phase_is_a_negative_delay_inside_one_period_so_a_repaint_does_not_restart_the_breath(tmp_path, monkeypatch):
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_needy(), "agents")}, {"motion": "no-preference"}, {"eval": DELAY}])
    delays = [float(d.rstrip("s")) for d in json.loads(out[-1])]
    period = float(re.search(r"tv-needpulse\s+([\d.]+)s", (WEB / "dashboard.css").read_text()).group(1))
    assert len(delays) == 3 and all(-period < d <= 0 for d in delays), (delays, period)


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
    seconds = float(re.search(r"animation: tv-needpulse ([\d.]+)s", css).group(1))
    millis = int(re.search(r"const PULSE_MS = (\d+);", js).group(1))
    assert millis == round(seconds * 1000), (millis, seconds)
