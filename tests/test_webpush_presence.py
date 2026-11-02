"""The phone-push rule: push = not muted AND (urgent OR not_home OR desk inactive >= 5 min), loud over all."""
from __future__ import annotations

import json
import os
import stat

import pytest

from levain.consent.adapters import claude_code
from levain.webpush import presence, sender
from levain.webpush.presence import ACTIVE_WINDOW_SECS, AWAY_GRACE_SECS, DEFAULT_CELLS, PresenceError, decide, set_word
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


def test_sleep_pushes_urgent_by_default_and_holds_it_when_his_cell_says_hold():
    # re-baselined (sleep default 10-25: approvals and attention push through sleep)
    d = decide(NOW, _w("sleep"), INACTIVE, urgent=True)   # no input since the word, said 100 s ago
    assert (d.push, d.state, d.by) == (True, "pushing", "")
    holding = {"quiet": DEFAULT_CELLS["quiet"], "sleep": {"approval": "hold", "attention": "hold", "other": "hold"}}
    d = decide(NOW, _w("sleep"), INACTIVE, urgent=True, policy=holding)
    assert (d.push, d.state, d.by) == (False, "muted", "sleep")


def test_loud_overrides_quiet_and_sleep_and_an_active_desk():
    assert decide(NOW, _w("loud", until=NOW + 600), ACTIVE).push is True
    # loud replaces the word on file, so "over a mute" is the one word: a loud stamped after a quiet wins by being the file
    assert decide(NOW, _w("loud", until=NOW + 600), ACTIVE, urgent=False).push is True


def test_sleep_mutes_until_input_after_the_word():
    s = _w("sleep")   # said 100 s ago
    assert decide(NOW, s, INACTIVE).push is False        # re-baselined: an urgent ping pushes through sleep now
    assert decide(NOW, s, INACTIVE, urgent=True).push is True
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


def test_remote_is_no_longer_a_presence_word(tmp_path):
    """Dropped 11-01: it is not a word to set, and a `remote` an older version left on file reads as NO word."""
    assert "remote" not in presence.WORDS and "remote" not in presence.TIMED and "remote" not in presence.SEAT_WORDS
    with pytest.raises(presence.PresenceError, match="`remote` word was dropped"):
        set_word(tmp_path, "remote", duration="1h", now=NOW)
    assert presence.read_word(tmp_path) is None
    (tmp_path / "webpush").mkdir(mode=0o700, exist_ok=True)
    (tmp_path / "webpush" / presence.FILE_NAME).write_text(json.dumps({"word": "remote", "ts": NOW, "until": NOW + 3600}))
    assert presence.read_word(tmp_path) is None
    # the rule then follows the measurement: an active desk holds
    assert presence.should_push(tmp_path, "ping", now=NOW + 1, idle=lambda: ACTIVE).push is False


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


def test_wiring_quiet_and_the_sleep_default_push_a_consent_and_a_held_sleep_cell_holds_it(tmp_path, monkeypatch):
    _phone(tmp_path)
    calls = _spy(monkeypatch, ACTIVE)
    set_word(tmp_path, "quiet", duration="30")
    assert _notify(tmp_path, "consent") is True
    set_word(tmp_path, "sleep")
    assert _notify(tmp_path, "consent") is True      # re-baselined: the sleep default pushes an approval
    presence.set_policy(tmp_path, "sleep", "approval", "hold")
    assert _notify(tmp_path, "consent") is False
    assert [c["push"] for c in calls] == [True, True, False]


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
    for w in ("sleep", "away", "back"):
        assert _cli(tmp_path, w) == 0
    assert _cli(tmp_path, "status") == 0


def test_a_short_write_does_not_truncate_the_word(tmp_path, monkeypatch):
    """The word is replaced whole from a temp file; a write that takes only part of the bytes must be continued, not
    renamed into place as a half-written (unreadable) word."""
    real = os.write

    def short(fd, data):
        return real(fd, bytes(data)[:3])

    monkeypatch.setattr(presence.os, "write", short)
    presence._write(tmp_path, {"word": "away", "ts": NOW})
    monkeypatch.undo()
    assert presence.read_word(tmp_path) is not None
    assert presence.read_word(tmp_path)["word"] == "away"


def test_a_failed_write_keeps_the_word_and_leaves_no_temp_file(tmp_path, monkeypatch):
    """A write that fails part way (a full disk) raises, keeps the word already in place, and removes its temp file."""
    presence._write(tmp_path, {"word": "away", "ts": NOW})
    real = os.write
    calls = []

    def fails_second(fd, data):
        calls.append(1)
        if len(calls) > 1:
            raise OSError(28, "No space left on device")
        return real(fd, bytes(data)[:3])

    monkeypatch.setattr(presence.os, "write", fails_second)
    with pytest.raises(presence.PresenceError):
        presence._write(tmp_path, {"word": "sleep", "ts": NOW})
    monkeypatch.undo()
    assert presence.read_word(tmp_path)["word"] == "away"
    assert [p.name for p in tmp_path.rglob("*.tmp")] == []


def test_effective_is_the_same_answer_as_should_push_for_every_decide_case(tmp_path):
    """The chip and the push decision are one function: over words x idle x route x kind, effective(...).pushing is
    should_push(...).push, and source says whether a word or the measurement decided."""
    from levain.webpush import route

    words = [None, _w("away", ts=NOW - 100), _w("away", ts=NOW - 1000), _w("quiet", until=NOW + 60), _w("quiet", until=NOW - 1),
             _w("sleep", ts=NOW - 100), _w("loud", until=NOW + 60), _w("loud", until=NOW - 1)]
    n = 0
    for wi, w in enumerate(words):
        for idle in (None, 0.0, 120.0, float(ACTIVE_WINDOW_SECS), 900.0):
            for rt, rem in [(r, m) for r in ("desk", "devices") for m in ("absent", "fresh", "stale")]:
                q = tmp_path / f"q{n}"
                n += 1
                (q / "webpush").mkdir(parents=True, mode=0o700)
                if w is not None:
                    (q / "webpush" / presence.FILE_NAME).write_text(json.dumps(w))
                if rt == "devices":
                    route.set_route(q, "devices", now=NOW - 5)
                if rem != "absent":   # fresh: 2 min old; stale: past the window
                    age = 120.0 if rem == "fresh" else ACTIVE_WINDOW_SECS + 1.0
                    (q / "webpush" / presence.REMOTE_NAME).write_text(json.dumps({"at": NOW - age, "source": "remote-terminal"}))
                got = presence.effective(q, NOW, idle=lambda: idle)
                want = presence.should_push(q, "ping", now=NOW, idle=lambda: idle, suggest=False)
                assert got["pushing"] is want.push and got["state"] == want.state and got["reason"] == want.why, (w, idle, rt, rem)
                assert (got["remote"] is not None) is (rem == "fresh")
                assert got["source"] in ("word", "measured") and got["state"] in ("pushing", "holding", "muted")
                if got["source"] == "word":
                    assert got["since"] == w["ts"]
                else:
                    assert got["since"] is None
    # source: a word decides when it is loud, a mute, or a live away; the desk reading decides otherwise
    q = tmp_path / "src"
    (q / "webpush").mkdir(parents=True, mode=0o700)
    assert presence.effective(q, NOW, idle=lambda: 900.0)["source"] == "measured"
    (q / "webpush" / presence.FILE_NAME).write_text(json.dumps(_w("quiet", until=NOW + 60)))
    assert presence.effective(q, NOW, idle=lambda: 900.0)["source"] == "word"
    (q / "webpush" / presence.FILE_NAME).write_text(json.dumps(_w("away", ts=NOW - 1000)))   # spent: he typed after the grace
    assert presence.effective(q, NOW, idle=lambda: 5.0)["source"] == "measured"


def test_effective_never_drives_the_route_suggestion_and_a_fault_reads_as_pushing(tmp_path, monkeypatch):
    from levain.webpush import route

    ran = []
    monkeypatch.setattr(route, "auto_suggest", lambda *a, **k: ran.append(1) or False)
    presence.effective(tmp_path, NOW, idle=lambda: 900.0)
    assert ran == []                                  # a status read (suggest=False) never suggests
    presence.should_push(tmp_path, "ping", now=NOW, idle=lambda: 900.0)
    assert ran == [1]                                 # a delivery does
    def boom():
        raise RuntimeError("no idle")
    got = presence.effective(tmp_path, NOW, idle=boom)
    assert got["pushing"] is True and got["state"] == "pushing" and "could not be read" in got["reason"]
