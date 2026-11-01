"""One coherent presence snapshot (the status view reads the word and the idle time once) and one projected route (a status
read says what a delivery would do on the route axis, without writing)."""
from __future__ import annotations

import json
import os

import pytest

from levain import web_server
from levain.webpush import presence, route

REMOTE = "u ttys001 Nov 1 10:00 (203.0.113.9)"
LOCAL = "u console Nov 1 09:00"


@pytest.fixture(autouse=True)
def _quiet_who(monkeypatch):
    monkeypatch.setattr(route, "_run_who", lambda: "")   # a test never reads the machine's logins


def _tree(q):
    out = {}
    for dirpath, _d, files in os.walk(q):
        for f in files:
            p = os.path.join(dirpath, f)
            out[p] = open(p, "rb").read()
    return out


class _Fake:
    def _credential_is_full(self):
        return False


def test_presence_view_reads_the_word_once_and_samples_idle_once(tmp_path, monkeypatch):
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    presence.set_word(q, "quiet", duration="1h")
    quiet_rec = presence.read_word(q)
    reads = []
    real = presence.read_word

    def racing(queue):
        reads.append(1)   # the first read sees quiet; any later read would see a concurrent POST's loud
        return quiet_rec if len(reads) == 1 else {**quiet_rec, "word": "loud"}

    idles = []

    def idle():
        idles.append(1)
        return 5.0

    monkeypatch.setattr(presence, "read_word", racing)
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", idle)
    v = web_server._Handler._presence_view(_Fake(), q)
    assert len(reads) == 1 and len(idles) == 1
    assert v["word"] == "quiet" and v["effective"]["source"] == "word" and v["effective"]["word"] == "quiet"
    assert v["effective"]["state"] == "muted" and "quiet" in v["text"]
    monkeypatch.setattr(presence, "read_word", real)


def test_effective_word_names_the_deciding_word_only(tmp_path):
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    assert presence.effective(q, idle=lambda: 5.0)["word"] is None
    presence.set_word(q, "loud", duration="1h")
    e = presence.effective(q, idle=lambda: 5.0)
    assert e["source"] == "word" and e["word"] == "loud"


def test_effective_works_from_a_given_snapshot(tmp_path):
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    called = []
    e = presence.effective(q, word=None, reading=900.0, idle=lambda: called.append(1) or 5.0)
    assert not called and e["state"] == "pushing" and "inactive" in e["reason"]


def _no_route_remote_idle(q):
    """no route standing, a remote login, the desk idle past the threshold: the next ping's auto_suggest picks devices"""
    return route.AUTO_IDLE_SECS + 10.0


def test_projected_route_matches_delivery_when_a_remote_login_would_suggest_devices(tmp_path, monkeypatch):
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    idle = _no_route_remote_idle(q)
    monkeypatch.setattr(route, "_run_who", lambda: REMOTE)
    # a desk that is active would hold; the devices route pushes
    assert route.projected_route(q, idle=idle, now=1000.0, who=REMOTE) == "devices"
    got = presence.effective(q, 1000.0, idle=lambda: idle)
    # idle past AUTO_IDLE_SECS but under ACTIVE_WINDOW_SECS, so only the route decides push
    assert idle < presence.ACTIVE_WINDOW_SECS
    want = presence.should_push(q, "ping", now=1000.0, idle=lambda: idle, suggest=True)
    assert got["pushing"] is want.push is True


def test_projected_route_matches_delivery_when_a_standing_auto_route_ends(tmp_path, monkeypatch):
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    route.set_route(q, "devices", source="auto", now=1000.0)
    monkeypatch.setattr(route, "_run_who", lambda: REMOTE)
    idle = 5.0
    assert route.projected_route(q, idle=idle, now=1001.0) == "desk"
    got = presence.effective(q, 1001.0, idle=lambda: idle)
    want = presence.should_push(q, "ping", now=1001.0, idle=lambda: idle, suggest=True)
    assert got["pushing"] is want.push is False and got["state"] == want.state == "holding"


def test_projected_route_keeps_the_standing_route_otherwise(tmp_path):
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    idle = route.AUTO_IDLE_SECS + 10.0
    assert route.projected_route(q, idle=idle, now=1000.0, who=LOCAL) == "desk"      # no remote login
    assert route.projected_route(q, idle=None, now=1000.0, who=REMOTE) == "desk"     # idle unknown
    route.set_route(q, "desk", source="operator", now=1000.0)
    assert route.projected_route(q, idle=idle, now=1001.0, who=REMOTE) == "desk"     # an operator record outranks it
    route.set_route(q, "devices", source="operator", now=1000.0)
    assert route.projected_route(q, idle=5.0, now=1001.0, who=REMOTE) == "devices"   # operator devices is not auto


def test_projected_route_writes_nothing(tmp_path):
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    idle = route.AUTO_IDLE_SECS + 10.0
    assert route.projected_route(q, idle=idle, now=1000.0, who=REMOTE) == "devices"
    assert _tree(q) == {}    # no route.json, no stamp, no log row
    route.set_route(q, "devices", source="auto", now=1000.0)
    before = _tree(q)
    assert route.projected_route(q, idle=5.0, now=1001.0, who=REMOTE) == "desk"
    assert _tree(q) == before
    assert json.loads(open(route._path(q)).read())["route"] == "devices"


# ---- fix round 2 -------------------------------------------------------------------------------------------------

def test_projected_route_honours_the_suggestion_throttle_read_only(tmp_path, monkeypatch):
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    idle = route.AUTO_IDLE_SECS + 10.0
    monkeypatch.setattr(route, "_run_who", lambda: "")
    # a delivery just probed `who` (found nothing): the stamp is fresh
    assert route.auto_suggest(q, idle=idle, now=1000.0, clock=lambda: 1000.0) is False
    stamp = _tree(q)
    # a remote login opens inside the window: the next delivery is throttled and holds on desk, so the projection must too
    monkeypatch.setattr(route, "_run_who", lambda: REMOTE)
    assert route.auto_suggest(q, idle=idle, now=1005.0, clock=lambda: 1005.0) is False
    assert route.projected_route(q, idle=idle, now=1005.0, clock=lambda: 1005.0) == "desk"
    assert _tree(q) == stamp                                                  # reading the throttle wrote nothing
    # past the window both would suggest devices
    assert route.projected_route(q, idle=idle, now=1100.0, clock=lambda: 1100.0) == "devices"
    # an explicit `who` is unthrottled, as in delivery
    assert route.projected_route(q, idle=idle, now=1005.0, who=REMOTE, clock=lambda: 1005.0) == "devices"


def test_presence_view_projects_the_route_and_runs_who_once(tmp_path, monkeypatch):
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    runs = []
    monkeypatch.setattr(route, "_run_who", lambda: runs.append(1) or REMOTE)
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", lambda: route.AUTO_IDLE_SECS + 10.0)
    v = web_server._Handler._presence_view(_Fake(), q)
    assert len(runs) == 1
    assert v["effective"]["pushing"] is True


def test_core_given_a_route_does_not_project(tmp_path, monkeypatch):
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    monkeypatch.setattr(route, "projected_route", lambda *a, **k: pytest.fail("projected_route called"))
    presence.effective(q, word=None, reading=900.0, route="desk")
    presence.describe(q, word=None, reading=900.0, route="desk")


def test_an_operator_desk_record_stays_desk_through_auto_suggest(tmp_path):
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    route.set_route(q, "desk", source="operator", now=1000.0)
    idle = route.AUTO_IDLE_SECS + 10.0
    assert route.auto_suggest(q, idle=idle, who=REMOTE, now=1001.0) is False
    assert route.read_route(q, 1001.0)[:2] == ("desk", "operator")
    # apply_route itself refuses an auto set over the unexpired operator record: the _auto_may_suggest guard adds nothing
    assert route.apply_route(q, "devices", None, source="auto", now=1001.0) is False
    assert route.read_route(q, 1001.0)[:2] == ("desk", "operator")
