"""The phone-push rule: push = not muted AND (urgent OR not_home OR desk inactive >= 5 min), loud over all."""
from __future__ import annotations

import json
import os
import stat

import pytest

from levain.consent.adapters import claude_code
from levain.webpush import presence, sender
from levain.webpush.presence import ACTIVE_WINDOW_SECS, AWAY_GRACE_SECS, PresenceError, decide, set_word
from tests._push_fixtures import phone

NOW = 1_000_000.0
ACTIVE, INACTIVE = 10.0, ACTIVE_WINDOW_SECS + 1.0   # idle seconds


@pytest.fixture(autouse=True)
def _as_operator(monkeypatch):
    """These tests drive the operator's own CLI: run them as a shell outside any agent session, wherever the suite runs."""
    from levain.consent import band

    monkeypatch.setattr(band, "agent_process", lambda: None)


def _w(word, **kw):
    rec = {"word": word, "ts": NOW - 100}
    rec.update(kw)
    return rec


def test_active_desk_holds_an_ordinary_ping():
    d = decide(NOW, None, ACTIVE)
    assert (d.push, d.state) == (False, "holding")


def test_inactive_five_minutes_pushes():
    assert decide(NOW, None, ACTIVE_WINDOW_SECS).push is True
    assert decide(NOW, None, ACTIVE_WINDOW_SECS - 1).push is False


def test_no_activity_source_reads_inactive():
    assert decide(NOW, None, None).push is True


def test_urgent_pushes_while_home_and_active():
    assert decide(NOW, None, ACTIVE, urgent=True).push is True


def test_quiet_holds_an_ordinary_ping_but_urgent_pushes_through():
    q = _w("quiet", until=NOW + 600)
    assert decide(NOW, q, INACTIVE).push is False
    assert decide(NOW, q, ACTIVE).state == "muted"
    d = decide(NOW, q, ACTIVE, urgent=True)
    assert (d.push, d.state) == (True, "pushing")


def test_sleep_holds_urgent_too():
    d = decide(NOW, _w("sleep"), INACTIVE, urgent=True)   # no input since the word, said 100 s ago
    assert (d.push, d.state, d.by) == (False, "muted", "sleep")


def test_loud_overrides_quiet_and_sleep_and_an_active_desk():
    assert decide(NOW, _w("loud", until=NOW + 600), ACTIVE).push is True
    # loud replaces the word on file, so "over a mute" is the one word: a loud stamped after a quiet wins by being the file
    assert decide(NOW, _w("loud", until=NOW + 600), ACTIVE, urgent=False).push is True


def test_sleep_mutes_until_input_after_the_word():
    s = _w("sleep")   # said 100 s ago
    assert decide(NOW, s, INACTIVE, urgent=True).push is False
    assert decide(NOW, s, None).push is False
    # input 50 s ago is after the word (said 100 s ago): no longer muted, and the desk is active
    assert decide(NOW, s, 50.0).state == "holding"


def test_away_pushes_at_once_until_input_past_the_grace():
    a = _w("away", ts=NOW - 600)
    # input 10 s ago is 590 s after the word: past the 180 s grace, so the word is spent
    assert decide(NOW, a, ACTIVE).push is False
    # input 1 s after the word is the parting instruction: still away
    assert decide(NOW, _w("away", ts=NOW - 100), 99.0).push is True
    assert AWAY_GRACE_SECS == 180


def test_remote_pushes_for_its_duration_only():
    assert decide(NOW, _w("remote", until=NOW + 60), ACTIVE).push is True
    assert decide(NOW, _w("remote", until=NOW - 1), ACTIVE).push is False


def test_expired_quiet_stops_holding_and_expired_loud_stops_overriding():
    assert decide(NOW, _w("quiet", until=NOW - 1), INACTIVE).push is True
    assert decide(NOW, _w("quiet", until=NOW - 1), ACTIVE).state == "holding"
    assert decide(NOW, _w("loud", until=NOW - 1), ACTIVE).push is False
    # a quiet that ended while a loud had replaced it never mutes
    assert decide(NOW + 700, _w("quiet", until=NOW + 600), INACTIVE).push is True


def test_no_state_precedence_urgent_beats_quiet_and_an_active_desk_and_quiet_beats_away():
    assert decide(NOW, _w("quiet", until=NOW + 600), ACTIVE, urgent=True).push is True
    assert decide(NOW, _w("quiet", until=NOW + 600), ACTIVE).push is False
    assert decide(NOW, _w("away", ts=NOW - 100), ACTIVE).push is True


# --- words, files ----------------------------------------------------------------------------------------------------

def test_set_word_roundtrip_and_durations(tmp_path):
    rec = set_word(tmp_path, "quiet", duration="30m", now=NOW)
    assert rec["until"] == NOW + 1800
    assert presence.read_word(tmp_path) == {"word": "quiet", "ts": NOW, "until": NOW + 1800}
    assert set_word(tmp_path, "quiet", now=NOW)["until"] == NOW + 3600
    assert set_word(tmp_path, "loud", duration="2h", now=NOW)["until"] == NOW + 7200
    assert set_word(tmp_path, "quiet", duration="9999h", now=NOW)["until"] == NOW + presence.MAX_SECS
    with pytest.raises(PresenceError):
        set_word(tmp_path, "quiet", duration="soon")
    with pytest.raises(PresenceError):
        set_word(tmp_path, "dance")


def test_file_modes_and_replacement(tmp_path):
    set_word(tmp_path, "sleep", now=NOW)
    p = tmp_path / "webpush" / presence.FILE_NAME
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(p.parent).st_mode) == 0o700
    set_word(tmp_path, "quiet", now=NOW + 1)   # the newest replaces the one before
    assert presence.read_word(tmp_path)["word"] == "quiet"


def test_back_clears_but_never_a_standing_loud(tmp_path):
    set_word(tmp_path, "quiet", now=NOW)
    assert set_word(tmp_path, "back", now=NOW + 1) is None
    assert presence.read_word(tmp_path) is None
    set_word(tmp_path, "loud", duration="30", now=NOW)
    assert set_word(tmp_path, "back", now=NOW + 1)["word"] == "loud"
    assert presence.read_word(tmp_path)["word"] == "loud"
    assert set_word(tmp_path, "back", now=NOW + 5000) is None   # the loud has run out: back works again


def test_loud_off_ends_loud_only(tmp_path):
    set_word(tmp_path, "loud", duration="30", now=NOW)
    assert set_word(tmp_path, "loud", duration="off", now=NOW + 1) is None
    assert presence.read_word(tmp_path) is None
    set_word(tmp_path, "quiet", now=NOW)
    assert set_word(tmp_path, "loud", duration="off", now=NOW + 1)["word"] == "quiet"


def test_a_loud_after_a_quiet_replaces_it(tmp_path):
    set_word(tmp_path, "quiet", duration="60", now=NOW)
    set_word(tmp_path, "loud", duration="10", now=NOW + 1)
    assert presence.should_push(tmp_path, "ping", now=NOW + 2, idle=lambda: ACTIVE).push is True


def test_unreadable_or_foreign_word_files_are_no_word(tmp_path):
    d = tmp_path / "webpush"
    d.mkdir(mode=0o700)
    f = d / presence.FILE_NAME
    for body in ("{not json", json.dumps({"word": "focus", "ts": 1}), json.dumps(["quiet"]),
                 json.dumps({"word": "quiet", "ts": "x"}), "x" * 9000):
        f.write_text(body)
        assert presence.read_word(tmp_path) is None
    f.unlink()
    f.symlink_to(tmp_path / "elsewhere")
    assert presence.read_word(tmp_path) is None


def test_a_timed_word_without_until_gets_the_default(tmp_path):
    d = tmp_path / "webpush"
    d.mkdir(mode=0o700)
    (d / presence.FILE_NAME).write_text(json.dumps({"word": "quiet", "ts": NOW}))
    assert presence.read_word(tmp_path)["until"] == NOW + presence.DEFAULT_SECS


def test_should_push_reads_a_fault_as_push(tmp_path):
    def boom():
        raise RuntimeError("ioreg")
    assert presence.should_push(tmp_path, "ping", idle=boom).push is True


# --- the wiring: claude_code.notify -----------------------------------------------------------------------------------

def _phone(qdir):
    phone(qdir, "https://fcm.googleapis.com/fcm/send/presence-test")


def _spy(monkeypatch, idle_seconds):
    calls = []
    monkeypatch.setattr(sender, "spawn", lambda qdir, **kw: calls.append(kw) or True)
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", lambda: idle_seconds)
    return calls


def _notify(qdir, kind):
    return claude_code.notify(claude_code.AdapterSettings(), "levain: x waits", cwd=qdir, kind=kind, ref="request:r1")


def test_wiring_active_desk_records_the_ping_without_pushing(tmp_path, monkeypatch):
    _phone(tmp_path)
    calls = _spy(monkeypatch, ACTIVE)
    assert _notify(tmp_path, "ruling") is False
    assert [c["push"] for c in calls] == [False]   # recorded, not pushed


def test_wiring_inactive_desk_pushes(tmp_path, monkeypatch):
    _phone(tmp_path)
    calls = _spy(monkeypatch, INACTIVE)
    assert _notify(tmp_path, "ruling") is True
    assert [c["push"] for c in calls] == [True]


def test_wiring_urgent_consent_pushes_with_an_active_desk(tmp_path, monkeypatch):
    _phone(tmp_path)
    calls = _spy(monkeypatch, ACTIVE)
    assert _notify(tmp_path, "consent") is True
    assert [c["push"] for c in calls] == [True]


def test_wiring_quiet_holds_consent_and_loud_pushes_again(tmp_path, monkeypatch):
    _phone(tmp_path)
    calls = _spy(monkeypatch, INACTIVE)
    set_word(tmp_path, "quiet", duration="30")
    assert _notify(tmp_path, "ruling") is False
    set_word(tmp_path, "loud", duration="30")
    assert _notify(tmp_path, "ruling") is True
    assert [c["push"] for c in calls] == [False, True]


def test_wiring_quiet_still_pushes_a_consent_and_sleep_holds_it(tmp_path, monkeypatch):
    _phone(tmp_path)
    calls = _spy(monkeypatch, ACTIVE)
    set_word(tmp_path, "quiet", duration="30")
    assert _notify(tmp_path, "consent") is True
    set_word(tmp_path, "sleep")
    assert _notify(tmp_path, "consent") is False
    assert [c["push"] for c in calls] == [True, False]


# --- the CLI words ----------------------------------------------------------------------------------------------------

def _cli(tmp_path, *argv):
    import argparse

    from levain.webpush import cli

    p = argparse.ArgumentParser()
    cli.register(p.add_subparsers(dest="cmd"))
    a = p.parse_args(["ping", *argv, "--queue-dir", str(tmp_path)])
    return a.func(a)


def test_cli_words_write_the_word_and_status_says_why(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", lambda: ACTIVE)
    assert _cli(tmp_path, "quiet", "20m") == 0
    assert presence.read_word(tmp_path)["word"] == "quiet"
    assert "MUTED" in capsys.readouterr().out
    assert _cli(tmp_path, "loud", "5") == 0
    assert "PUSHING" in capsys.readouterr().out
    assert _cli(tmp_path, "loud", "off") == 0
    assert presence.read_word(tmp_path) is None
    assert "HOLDING" in capsys.readouterr().out
    assert _cli(tmp_path, "quiet", "soon") == 2
    for w in ("sleep", "away", "remote", "back"):
        assert _cli(tmp_path, w) == 0
    assert _cli(tmp_path, "status") == 0
