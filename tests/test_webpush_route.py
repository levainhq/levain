"""The route axis (desk | devices): the pure rule, the file, the presets and the remote-login suggestion."""
from __future__ import annotations

import json
import os

import pytest

from levain.webpush import presence, route
from levain.webpush.presence import ACTIVE_WINDOW_SECS, decide, set_word

NOW = 1_000_000.0
ACTIVE = 10.0


@pytest.fixture(autouse=True)
def _as_operator(monkeypatch):
    from levain.consent import band

    monkeypatch.setattr(band, "agent_process", lambda: None)


def _w(word, **kw):
    return {"word": word, "ts": NOW - 100, **kw}


def test_desk_holds_an_active_ordinary_ping_and_devices_pushes_it():
    d = decide(NOW, None, ACTIVE, class_name="other", route="desk")
    assert (d.push, d.state) == (False, "holding")
    d = decide(NOW, None, ACTIVE, class_name="other", route="devices")
    assert d.push is True and d.why.startswith("route: devices")


def test_route_changes_nothing_for_urgent_or_the_mutes_and_loud_wins():
    for r in ("desk", "devices"):
        assert decide(NOW, None, ACTIVE, class_name="approval", route=r).push is True
    assert decide(NOW, _w("quiet", until=NOW + 600), ACTIVE, class_name="other", route="devices").push is False
    assert decide(NOW, _w("sleep"), 250.0, class_name="other", route="devices").push is False
    assert decide(NOW, _w("sleep"), 250.0, class_name="approval", route="devices").push is False
    assert decide(NOW, _w("loud", until=NOW + 600), ACTIVE, class_name="other", route="desk").push is True


def test_no_file_is_desk_and_a_set_is_read_back(tmp_path):
    assert route.read_route(tmp_path, NOW) == ("desk", "default", None)
    route.set_route(tmp_path, "devices", "2h", now=NOW)
    assert route.read_route(tmp_path, NOW + 1) == ("devices", "operator", NOW + 7200)
    f = tmp_path / "webpush" / route.FILE_NAME
    assert (f.stat().st_mode & 0o777) == 0o600


def test_an_ended_route_reads_desk(tmp_path):
    route.set_route(tmp_path, "devices", "1h", now=NOW)
    assert route.read_route(tmp_path, NOW + 3599)[0] == "devices"
    assert route.read_route(tmp_path, NOW + 3600)[0] == "desk"


@pytest.mark.parametrize("content", ["{", "[]", '{"route": "moon", "ts": 1}', '{"route": "desk"}', "x" * 2000])
def test_an_unreadable_route_file_reads_devices(tmp_path, content):
    (tmp_path / "webpush").mkdir()
    (tmp_path / "webpush" / route.FILE_NAME).write_text(content)
    assert route.read_route(tmp_path, NOW)[:2] == ("devices", "fault")


def test_a_symlinked_route_file_reads_devices(tmp_path):
    (tmp_path / "webpush").mkdir()
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"route": "desk", "ts": NOW, "until": None, "source": "operator"}))
    os.symlink(real, tmp_path / "webpush" / route.FILE_NAME)
    assert route.read_route(tmp_path, NOW)[0] == "devices"


def test_auto_never_replaces_an_unexpired_operator_record_and_operator_replaces_auto(tmp_path):
    route.set_route(tmp_path, "desk", "1h", now=NOW)                       # he said desk
    assert route.set_route(tmp_path, "devices", source="auto", now=NOW + 10) is False
    assert route.read_route(tmp_path, NOW + 11)[:2] == ("desk", "operator")
    assert route.set_route(tmp_path, "devices", source="auto", now=NOW + 3601) is True   # his record ended
    assert route.read_route(tmp_path, NOW + 3602)[:2] == ("devices", "auto")
    route.set_route(tmp_path, "desk", now=NOW + 3700)
    assert route.read_route(tmp_path, NOW + 3701)[:2] == ("desk", "operator")


def test_the_remote_word_sets_devices_for_its_duration_and_back_sets_desk(tmp_path):
    set_word(tmp_path, "remote", duration="2h", now=NOW)
    assert route.read_route(tmp_path, NOW + 1) == ("devices", "operator", NOW + 7200)
    set_word(tmp_path, "away", now=NOW + 5)
    assert route.read_route(tmp_path, NOW + 6)[0] == "devices"            # away and sleep leave the route alone
    set_word(tmp_path, "sleep", now=NOW + 7)
    assert route.read_route(tmp_path, NOW + 8)[0] == "devices"
    set_word(tmp_path, "back", now=NOW + 9)
    assert route.read_route(tmp_path, NOW + 10)[:2] == ("desk", "operator")


def test_back_under_a_standing_loud_leaves_the_route(tmp_path):
    set_word(tmp_path, "remote", now=NOW)
    set_word(tmp_path, "loud", duration="1h", now=NOW + 1)
    set_word(tmp_path, "back", now=NOW + 2)
    assert route.read_route(tmp_path, NOW + 3)[0] == "devices"


def test_should_push_reads_the_route(tmp_path):
    kw = dict(now=NOW, idle=lambda: ACTIVE)
    assert presence.should_push(tmp_path, "ping", **kw).push is False
    route.set_route(tmp_path, "devices", now=NOW)
    assert presence.should_push(tmp_path, "ping", **kw).push is True
    assert "route: devices" in presence.describe(tmp_path, now=NOW + 1, idle=lambda: ACTIVE)


WHO_SSH = "phill    ttys004  Nov  1 09:12 (203.0.113.7)"
WHO_MOSH = "phill    ttys005  Nov  1 09:15 (mac.example.net)"
WHO_LOCAL = "phill    ttys001  Nov  1 08:00"
WHO_CONSOLE = "phill    console  Nov  1 07:59"


@pytest.mark.parametrize("out,want", [(WHO_SSH, True), (WHO_MOSH, True), (WHO_LOCAL, False), (WHO_CONSOLE, False),
                                      (WHO_CONSOLE + "\n" + WHO_LOCAL + "\n" + WHO_SSH, True),
                                      ("phill    ttys002  Nov  1 08:00 (:0)", False), ("", False)])
def test_who_parser(out, want):
    assert route.remote_login_present(out) is want


def test_auto_suggest_needs_a_remote_login_and_an_idle_laptop(tmp_path):
    assert route.auto_suggest(tmp_path, idle=300.0, who=WHO_LOCAL, now=NOW) is False
    assert route.auto_suggest(tmp_path, idle=30.0, who=WHO_SSH, now=NOW) is False
    assert route.auto_suggest(tmp_path, idle=None, who=WHO_SSH, now=NOW) is False
    assert route.read_route(tmp_path, NOW)[0] == "desk"
    assert route.auto_suggest(tmp_path, idle=ACTIVE_WINDOW_SECS, who=WHO_SSH, now=NOW) is True
    assert route.read_route(tmp_path, NOW + 1) == ("devices", "auto", NOW + 3600)


def test_auto_suggest_does_not_override_his_desk(tmp_path):
    route.set_route(tmp_path, "desk", now=NOW)
    assert route.auto_suggest(tmp_path, idle=300.0, who=WHO_SSH, now=NOW + 5) is False
    assert route.read_route(tmp_path, NOW + 6)[:2] == ("desk", "operator")


def test_who_failing_reads_as_no_remote_login(monkeypatch):
    def boom(*a, **k):
        raise OSError("no who")

    monkeypatch.setattr(route.subprocess, "run", boom)
    assert route._run_who() == ""


# ---- L3 r1 fixes -----------------------------------------------------------------------------------------------

def _raw(tmp_path, text):
    d = tmp_path / "webpush"
    d.mkdir(exist_ok=True)
    (d / route.FILE_NAME).write_text(text)


@pytest.mark.parametrize("text", [
    '{"route": "devices", "ts": 1, "until": %s, "source": "operator"}' % ("9" * 400),
    '{"route": "devices", "ts": %s, "until": null, "source": "operator"}' % ("9" * 400),
    '{"route": "desk", "ts": 1000000, "until": null, "source": "operator"}',
    '{"route": "desk", "ts": 1000000, "until": 2000000}',
    '{"route": "desk", "ts": 1000000, "until": 2000000, "source": "Operator"}',
    '{"route": "desk", "ts": 1000000, "until": 2000000, "source": "root"}',
    '{"route": "desk", "ts": 1000000, "until": 1e300, "source": "operator"}',
])
def test_a_malformed_record_reads_as_the_fault_never_as_an_operator_override(tmp_path, text):
    _raw(tmp_path, text)
    assert route.read_route(tmp_path, NOW) == ("devices", "fault", None)


def test_loopback_hosts_are_not_remote_logins():
    for host in ("127.0.0.1", "::1", "localhost", "fe80::1%lo0", "::ffff:127.0.0.1", "LOCALHOST"):
        assert route.remote_login_present(f"phill ttys004 Nov 1 09:12 ({host})") is False, host
    assert route.remote_login_present("phill ttys004 Nov 1 09:12 (203.0.113.7)") is True


def test_an_auto_record_ends_when_he_is_back_at_the_keyboard_and_an_operator_one_does_not(tmp_path):
    who = "phill ttys004 Nov 1 09:12 (203.0.113.7)"
    assert route.auto_suggest(tmp_path, idle=900.0, who=who, now=NOW) is True
    assert route.read_route(tmp_path, NOW + 1)[:2] == ("devices", "auto")
    assert route.auto_suggest(tmp_path, idle=3.0, who=who, now=NOW + 2) is False
    assert route.read_route(tmp_path, NOW + 3) == ("desk", "default", None)
    route.set_route(tmp_path, "devices", "2h", now=NOW)
    route.auto_suggest(tmp_path, idle=3.0, who=who, now=NOW + 2)
    assert route.read_route(tmp_path, NOW + 3)[:2] == ("devices", "operator")


def test_the_live_suggestion_runs_who_at_most_once_per_throttle_window(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(route, "_run_who", lambda: ran.append(1) or "")
    monkeypatch.setattr(route, "_throttle", {"at": None})
    t = [100.0]
    for _ in range(5):
        route.auto_suggest(tmp_path, idle=900.0, now=NOW, clock=lambda: t[0])
    assert len(ran) == 1
    t[0] += route.AUTO_THROTTLE_SECS + 1
    route.auto_suggest(tmp_path, idle=900.0, now=NOW, clock=lambda: t[0])
    assert len(ran) == 2


def test_should_push_drives_the_suggestion_and_the_same_call_pushes(tmp_path, monkeypatch):
    monkeypatch.setattr(route, "_run_who", lambda: "phill ttys004 Nov 1 09:12 (203.0.113.7)")
    monkeypatch.setattr(route, "_throttle", {"at": None})
    d = presence.should_push(tmp_path, "ping", now=NOW, idle=lambda: 900.0)
    assert d.push is True
    assert route.read_route(tmp_path, NOW + 1)[:2] == ("devices", "auto")


def test_an_operator_desk_defaults_to_a_day_and_devices_to_an_hour(tmp_path):
    route.set_route(tmp_path, "desk", now=NOW)
    assert route.read_route(tmp_path, NOW + 1)[2] == NOW + presence.MAX_SECS
    route.set_route(tmp_path, "devices", now=NOW)
    assert route.read_route(tmp_path, NOW + 1)[2] == NOW + presence.DEFAULT_SECS


def test_a_failed_route_write_leaves_the_word_untouched_and_raises(tmp_path, monkeypatch):
    set_word(tmp_path, "away", by=None, now=NOW)
    before = presence.read_word(tmp_path)

    def boom(*a, **k):
        raise presence.PresenceError("route not written: disk")

    monkeypatch.setattr(route, "apply_route", boom)
    for word in ("back", "remote"):
        with pytest.raises(presence.PresenceError):
            set_word(tmp_path, word, by=None, now=NOW + 5)
        assert presence.read_word(tmp_path) == before
