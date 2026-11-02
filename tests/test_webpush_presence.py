"""The phone-push rule over the one intent record (presence 1b): follow (devices on after 10 min of no laptop input) by default,
the presets' lets_through, loud over all, and the compat words flow's installed half still calls."""
from __future__ import annotations

import json
import os
import stat

import pytest

from levain.consent.adapters import claude_code
from levain.webpush import intent, presence, sender
from levain.webpush.presence import AWAY_GRACE_SECS, PresenceError, set_word
from tests._push_fixtures import phone

NOW = 1_000_000.0
FOLLOW = intent.FOLLOW_DEFAULT
ACTIVE, INACTIVE = 10.0, FOLLOW + 1.0   # idle seconds


@pytest.fixture(autouse=True)
def _as_operator(monkeypatch):
    """These tests drive the operator's own CLI: run them as a shell outside any agent session, wherever the suite runs."""
    from levain.consent import band

    monkeypatch.setattr(band, "agent_process", lambda: None)


def _rec(overlay=None, **base):
    """A ``intent.read``-shaped record (the pure rule's input) with an optional overlay."""
    b = {"devices": "follow", "follow_after": FOLLOW, "lets_through": "everything", "ts": 0.0, **base}
    return {"status": "file", "reason": "", "base": b, "overlay": overlay, "projection": None}


def _ov(preset, lets, until, **kw):
    return {"preset": preset, "devices": "on", "follow_after": FOLLOW, "lets_through": lets, "ts": NOW - 100, "until": until, **kw}


AWAY = lambda **kw: _ov("away", "everything", {"kind": "input"}, **kw)       # noqa: E731
SLEEP = lambda **kw: _ov("sleep", "needs", {"kind": "input"}, **kw)          # noqa: E731
QUIET = lambda secs=600, **kw: _ov("quiet", "needs", {"kind": "timer", "at": NOW + secs}, **kw)   # noqa: E731
LOUD = lambda secs=600, **kw: _ov("loud", "everything", {"kind": "timer", "at": NOW + secs}, **kw)   # noqa: E731


def decide(rec, idle, cls="other", **kw):
    return intent.decide(NOW, rec, idle, class_name=cls, **kw)


def test_an_active_laptop_holds_an_ordinary_ping():
    d = decide(_rec(), ACTIVE)
    assert (d.push, d.state) == (False, "holding")


def test_follow_pushes_after_ten_minutes_of_no_input_and_not_before():
    assert decide(_rec(), FOLLOW).push is True
    assert decide(_rec(), FOLLOW - 1).push is False
    assert intent.FOLLOW_DEFAULT == 600


def test_no_activity_source_reads_inactive():
    d = decide(_rec(), None)
    assert d.push is True and d.why == "follow: no laptop input reading"


def test_an_approval_at_an_active_laptop_stays_in_the_agents_dialog_and_its_notice_still_goes():
    """Phill 11-02 (np-e642162b (a)): *"yes we DO want these to be able to find us ASAP"*. At the laptop under follow the
    record still does not route an approval to the devices for a signed answer (the hold's question: the agent's own
    dialog asks), but the NOTICE that a prompt waits goes whatever the desk is doing (adapter wiring below); an
    attention item pushes at an active desk too (1b)."""
    assert decide(_rec(), ACTIVE, "approval").push is False          # the hold's decision, unchanged
    assert decide(_rec(), ACTIVE, "attention").push is True
    assert decide(_rec(), INACTIVE, "approval").push is True


def test_quiet_holds_an_ordinary_ping_but_approvals_and_attention_push_through():
    q = _rec(QUIET())
    assert decide(q, ACTIVE).state == "muted" and decide(q, INACTIVE).state == "muted"
    for cls in ("approval", "attention"):
        d = decide(q, ACTIVE, cls)
        assert (d.push, d.state) == (True, "pushing")


def test_sleep_lets_approvals_and_attention_through_as_phill_ruled_and_mutes_the_rest():
    d = decide(_rec(SLEEP()), INACTIVE, "approval")
    assert (d.push, d.state) == (True, "pushing")
    d = decide(_rec(SLEEP()), INACTIVE)
    assert (d.push, d.state, d.by) == (False, "muted", "sleep")


def test_loud_overrides_an_active_laptop_and_an_ordinary_ping():
    assert decide(_rec(LOUD()), ACTIVE).push is True
    assert decide(_rec(LOUD()), ACTIVE, "other").state == "pushing"


def test_sleep_ends_at_input_past_the_grace_and_the_grace_is_every_until_input_presets():
    s = _rec(SLEEP())   # said 100 s ago
    assert decide(s, 50.0).state == "muted"            # input 50 s ago is 50 s after the word: the parting instruction
    assert decide(s, None).state == "muted"            # no reading: it ends only at the blind bound (SLEEP_BLIND_SECS)
    ended = decide(_rec(SLEEP(ts=NOW - 1000)), 50.0)   # input 950 s after the word is past the 180 s grace
    assert (ended.push, ended.state) == (False, "holding")


def test_a_sleep_with_no_idle_reading_ends_at_the_blind_bound():
    old = _rec(SLEEP(ts=NOW - presence.SLEEP_BLIND_SECS - 1))
    assert decide(old, None, "other").push is True and "no laptop input reading" in decide(old, None).why


def test_away_pushes_at_once_until_input_past_the_grace():
    a = _rec(AWAY(ts=NOW - 600))
    # input 10 s ago is 590 s after the word: past the 180 s grace, so the word is spent and follow reads the desk
    assert decide(a, ACTIVE).push is False
    # input 1 s after the word is the parting instruction: still away
    assert decide(_rec(AWAY(ts=NOW - 100)), 99.0).push is True
    assert AWAY_GRACE_SECS == 180


def test_remote_is_no_longer_a_presence_word(tmp_path):
    """Dropped 11-01: it is not a word to set, and a `remote` an older version left on file reads as NO word."""
    assert "remote" not in presence.WORDS and "remote" not in presence.TIMED and "remote" not in presence.SEAT_WORDS
    with pytest.raises(presence.PresenceError, match="`remote` word was dropped"):
        set_word(tmp_path, "remote", duration="1h", now=NOW)
    assert presence.read_word(tmp_path, now=NOW) is None
    (tmp_path / "webpush").mkdir(mode=0o700, exist_ok=True)
    (tmp_path / "webpush" / presence.FILE_NAME).write_text(json.dumps({"word": "remote", "ts": NOW, "until": NOW + 3600}))
    assert presence.read_word(tmp_path, now=NOW) is None
    # the rule then follows the measurement: an active laptop holds
    assert presence.should_push(tmp_path, "ping", now=NOW + 1, idle=lambda: ACTIVE).push is False


def test_expired_quiet_stops_muting_and_expired_loud_stops_overriding():
    assert decide(_rec(QUIET(-1)), INACTIVE).push is True
    assert decide(_rec(QUIET(-1)), ACTIVE).state == "holding"
    assert decide(_rec(LOUD(-1)), ACTIVE).push is False
    # a quiet that ended while a loud had replaced it never mutes (one overlay on file)
    assert decide(_rec(QUIET(-100)), INACTIVE).push is True


def test_no_state_precedence_approvals_beat_quiet_and_quiet_beats_nothing_and_away_beats_an_active_laptop():
    assert decide(_rec(QUIET()), ACTIVE, "approval").push is True
    assert decide(_rec(QUIET()), ACTIVE).push is False
    assert decide(_rec(AWAY(ts=NOW - 100)), ACTIVE).push is True


def test_a_fault_in_the_record_reads_as_push():
    bad = {"status": "fault", "reason": "not valid JSON", "base": _rec()["base"], "overlay": None, "projection": None}
    d = decide(bad, ACTIVE, "other")
    assert d.push is True and "could not be read" in d.why


# --- words, files ----------------------------------------------------------------------------------------------------

def test_set_word_roundtrip_and_durations(tmp_path):
    rec = set_word(tmp_path, "quiet", duration="30m", now=NOW)
    assert rec["until"] == NOW + 1800
    assert presence.read_word(tmp_path, now=NOW, idle=lambda: 5.0) == {"word": "quiet", "ts": NOW, "until": NOW + 1800}
    assert set_word(tmp_path, "quiet", now=NOW)["until"] == NOW + 3600
    assert set_word(tmp_path, "loud", duration="2h", now=NOW)["until"] == NOW + 7200
    assert set_word(tmp_path, "quiet", duration="9999h", now=NOW)["until"] == NOW + presence.MAX_SECS
    with pytest.raises(PresenceError):
        set_word(tmp_path, "quiet", duration="soon")
    with pytest.raises(PresenceError):
        set_word(tmp_path, "dance")


def test_file_modes_and_replacement(tmp_path):
    set_word(tmp_path, "sleep", now=NOW)
    p = tmp_path / "webpush" / intent.FILE_NAME
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(p.parent).st_mode) == 0o700
    set_word(tmp_path, "quiet", now=NOW + 1)   # the newest replaces the one before
    assert presence.read_word(tmp_path, now=NOW + 1, idle=lambda: 5.0)["word"] == "quiet"


def test_back_clears_but_never_a_standing_loud(tmp_path):
    set_word(tmp_path, "quiet", now=NOW)
    assert set_word(tmp_path, "back", now=NOW + 1) is None
    assert presence.read_word(tmp_path, now=NOW + 1) is None
    set_word(tmp_path, "loud", duration="30", now=NOW)
    assert set_word(tmp_path, "back", now=NOW + 1)["word"] == "loud"
    assert presence.read_word(tmp_path, now=NOW + 1, idle=lambda: 5.0)["word"] == "loud"
    assert set_word(tmp_path, "back", now=NOW + 5000) is None   # the loud has run out: back works again


def test_loud_off_ends_loud_only(tmp_path):
    set_word(tmp_path, "loud", duration="30", now=NOW)
    assert set_word(tmp_path, "loud", duration="off", now=NOW + 1) is None
    assert presence.read_word(tmp_path, now=NOW + 1) is None
    set_word(tmp_path, "quiet", now=NOW)
    assert set_word(tmp_path, "loud", duration="off", now=NOW + 1)["word"] == "quiet"


def test_a_loud_after_a_quiet_replaces_it(tmp_path):
    set_word(tmp_path, "quiet", duration="60", now=NOW)
    set_word(tmp_path, "loud", duration="10", now=NOW + 1)
    assert presence.should_push(tmp_path, "ping", now=NOW + 2, idle=lambda: ACTIVE).push is True


def test_the_route_words_are_aliases_for_the_settings(tmp_path):
    set_word(tmp_path, "devices", now=NOW)                                  # the old tap-token word: devices on for an hour
    assert presence.should_push(tmp_path, "ping", now=NOW + 1, idle=lambda: ACTIVE).push is True
    assert intent.read(tmp_path)["overlay"]["preset"] is None
    set_word(tmp_path, "desk", now=NOW + 2)                                 # the measurement decides again: follow, as the base
    assert presence.should_push(tmp_path, "ping", now=NOW + 3, idle=lambda: ACTIVE).push is False
    with pytest.raises(PresenceError):
        set_word(tmp_path, "devices", by={"agent": "claude", "session": "s", "role": "seat"}, said="x")


def test_unreadable_or_foreign_record_files_read_as_a_fault_and_old_word_files_as_no_word(tmp_path):
    d = tmp_path / "webpush"
    d.mkdir(mode=0o700)
    f = d / intent.FILE_NAME
    for body in ("{not json", json.dumps({"version": 2}), json.dumps(["quiet"]), json.dumps({"version": 1, "base": {}}), "x" * 9000):
        f.write_text(body)
        assert intent.read(tmp_path)["status"] == "fault", body[:20]
        assert presence.should_push(tmp_path, "ping", now=NOW, idle=lambda: ACTIVE).push is True      # a fault reads as push
    f.unlink()
    f.symlink_to(tmp_path / "elsewhere")
    assert intent.read(tmp_path)["status"] == "fault"
    f.unlink()
    old = d / presence.FILE_NAME
    for body in ("{not json", json.dumps({"word": "focus", "ts": 1}), json.dumps(["quiet"]), json.dumps({"word": "quiet", "ts": "x"}), "x" * 9000):
        old.write_text(body)
        assert presence.read_legacy_word(tmp_path) is None


def test_a_timed_legacy_word_without_until_gets_the_default(tmp_path):
    d = tmp_path / "webpush"
    d.mkdir(mode=0o700)
    (d / presence.FILE_NAME).write_text(json.dumps({"word": "quiet", "ts": NOW}))
    assert presence.read_legacy_word(tmp_path)["until"] == NOW + presence.DEFAULT_SECS


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


def test_wiring_active_laptop_records_the_ping_without_pushing(tmp_path, monkeypatch):
    _phone(tmp_path)
    calls = _spy(monkeypatch, ACTIVE)
    assert _notify(tmp_path, "ruling") is False
    assert [c["push"] for c in calls] == [False]   # recorded, not pushed


def test_wiring_walked_away_pushes(tmp_path, monkeypatch):
    _phone(tmp_path)
    calls = _spy(monkeypatch, INACTIVE)
    assert _notify(tmp_path, "ruling") is True
    assert [c["push"] for c in calls] == [True]


def test_wiring_a_consent_at_an_active_laptop_stays_with_the_agents_own_dialog(tmp_path, monkeypatch):
    _phone(tmp_path)
    calls = _spy(monkeypatch, ACTIVE)
    assert _notify(tmp_path, "consent") is False
    assert [c["push"] for c in calls] == [False]


def test_wiring_quiet_holds_an_ordinary_ping_and_loud_pushes_again(tmp_path, monkeypatch):
    _phone(tmp_path)
    calls = _spy(monkeypatch, INACTIVE)
    set_word(tmp_path, "quiet", duration="30")
    assert _notify(tmp_path, "ruling") is False
    set_word(tmp_path, "loud", duration="30")
    assert _notify(tmp_path, "ruling") is True
    assert [c["push"] for c in calls] == [False, True]


def test_wiring_quiet_and_sleep_both_let_a_consent_through(tmp_path, monkeypatch):
    _phone(tmp_path)
    calls = _spy(monkeypatch, ACTIVE)
    set_word(tmp_path, "quiet", duration="30")
    assert _notify(tmp_path, "consent") is True
    set_word(tmp_path, "sleep")
    assert _notify(tmp_path, "consent") is True      # Phill 10-25: in sleep an approval prompt block still pings him
    assert [c["push"] for c in calls] == [True, True]


# --- the prompt notice: an approval left to the agent's own dialog (Phill 11-02, np-e642162b (a)) -----------------------

def _consent_queue(tmp_path):
    from levain.consent.queue import ConsentQueue, authority_source
    from tests._h4 import h4_home

    q = tmp_path / "consent"
    q.mkdir(mode=0o700)
    return ConsentQueue(q, authority=authority_source(q, anchor=None), home=h4_home())


def _pr_event(**extra):
    return {"session_id": "s-notice", "transcript_path": "/x.jsonl", "cwd": "/work/proj", "permission_mode": "default",
            "hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": "touch x"}, **extra}


def _cc_decide(q, settings=None, **kw):
    from tests._h4 import h4_env, h4_home

    return claude_code.decide(_pr_event(), q, settings or claude_code.AdapterSettings(), environ=h4_env(),
                              home=h4_home(), **kw)


@pytest.mark.parametrize("decider", ["nothing can decide", "a zero hold ran out"])
def test_an_approval_prompt_at_an_active_laptop_pings_the_devices_once_and_starts_no_hold(tmp_path, monkeypatch, decider):
    """THE REPRODUCTION (fails on 8dc7ce30: no push at all). Him active at the laptop, a phone paired: Claude Code's own
    dialog asks (no decision), ONE notice push goes to the devices, and no consent hold starts."""
    q = _consent_queue(tmp_path)
    _phone(q.directory)
    calls = _spy(monkeypatch, ACTIVE)
    if decider == "a zero hold ran out":
        monkeypatch.setattr(claude_code, "nothing_can_decide", lambda s, **kw: False)
    assert intent.approvals_to_devices(q.directory, idle=lambda: ACTIVE)[0] is False   # the hold's question: unchanged
    assert _cc_decide(q) is None                                                       # the agent's own dialog asks
    assert [(c["kind"], c["push"], c["url"]) for c in calls] == [("consent", True, "/?view=needs")]
    assert calls[0]["body"].startswith("claude_code asks in its own dialog: ")
    assert q.pending() == []                                                           # nothing held
    if decider == "a zero hold ran out":
        assert calls[0]["ref"].startswith("request:")
        assert q.get(calls[0]["ref"].split(":", 1)[1])[1] == "withdrawn"               # left to the dialog at once
    else:
        assert calls[0]["ref"].startswith("prompt:")


def test_the_same_command_asked_again_is_a_new_prompt_and_pings_again(tmp_path, monkeypatch):
    """Claude Code's PermissionRequest carries no tool_use_id: a second ask of the same command, its first ping on
    record, is still a new prompt (its ref is minted per hook call, never a digest of the call)."""
    from levain.webpush.store import PushStore

    q = _consent_queue(tmp_path)
    _phone(q.directory)
    calls = _spy(monkeypatch, ACTIVE)
    assert _cc_decide(q) is None
    PushStore(q.directory).add_ping("consent", calls[0]["ref"], "levain", "x")   # what the spawned child records
    assert _cc_decide(q) is None
    assert len(calls) == 2 and calls[0]["ref"] != calls[1]["ref"]


def test_a_prompt_notice_is_said_once_per_ref_and_never_under_a_mute(tmp_path, monkeypatch):
    from levain.webpush.store import PushStore

    qdir = tmp_path / "q"
    qdir.mkdir()
    _phone(qdir)
    calls = _spy(monkeypatch, ACTIVE)
    assert claude_code.prompt_notice(qdir, "claude_code", "Bash: touch x", "request:r9") is True
    PushStore(qdir).add_ping("consent", "request:r9", "levain", "x")   # what the spawned child records
    assert claude_code.prompt_notice(qdir, "claude_code", "Bash: touch x", "request:r9") is False
    intent.apply_settings(qdir, lets_through="nothing", idle=lambda: ACTIVE)
    assert claude_code.prompt_notice(qdir, "claude_code", "Bash: touch x", "request:r10") is False
    intent.apply_settings(qdir, lets_through="approvals", idle=lambda: ACTIVE)
    assert claude_code.prompt_notice(qdir, "claude_code", "Bash: touch x", "request:r10") is True
    assert [c["ref"] for c in calls] == ["request:r9", "request:r10"]


def test_a_prompt_notice_needs_a_phone_and_a_fault_says_nothing(tmp_path, monkeypatch):
    qdir = tmp_path / "q"
    qdir.mkdir()
    calls = _spy(monkeypatch, ACTIVE)
    assert claude_code.prompt_notice(qdir, "claude_code", "x", "request:r1") is False     # no phone paired
    _phone(qdir)

    def boom(*a, **kw):
        raise RuntimeError("sender")
    monkeypatch.setattr(sender, "spawn", boom)
    assert claude_code.prompt_notice(qdir, "claude_code", "x", "request:r1") is False
    assert calls == []


def test_an_away_hold_pings_through_the_hold_path_and_sends_no_notice(tmp_path, monkeypatch):
    """When an away hold routed the request to his devices (the hold path's own ping), the dialog that asks after it
    gets no second notice."""
    q = _consent_queue(tmp_path)
    _phone(q.directory)
    calls = _spy(monkeypatch, ACTIVE)
    monkeypatch.setattr(claude_code, "nothing_can_decide", lambda s, **kw: False)
    away = claude_code.AdapterSettings(away_mode=True, away_marked=True, away_hold_seconds=0.3)
    assert _cc_decide(q, away, max_hold=0.3 + claude_code.CLEANUP_RESERVE) is None
    assert calls == []


def test_codex_dialog_at_an_active_laptop_pings_the_devices_once(tmp_path, monkeypatch):
    from levain.consent.adapters import codex
    from tests._h4 import h4_env, h4_home

    q = _consent_queue(tmp_path)
    _phone(q.directory)
    calls = _spy(monkeypatch, ACTIVE)
    e = {**_pr_event(), "session_id": "s-codex"}
    assert codex.decide(e, q, codex.AdapterSettings(), environ=h4_env(), home=h4_home()) is None
    assert [(c["kind"], c["push"]) for c in calls] == [("consent", True)]
    assert calls[0]["body"].startswith("codex asks in its own dialog: ")


# --- the CLI words ----------------------------------------------------------------------------------------------------

def _cli(tmp_path, *argv):
    import argparse

    from levain.webpush import cli

    p = argparse.ArgumentParser()
    cli.register(p.add_subparsers(dest="cmd"))
    a = p.parse_args(["ping", *argv, "--queue-dir", str(tmp_path)])
    return a.func(a)


def test_cli_words_write_the_record_and_status_prints_the_one_line(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", lambda: ACTIVE)
    assert _cli(tmp_path, "quiet", "20m") == 0
    assert presence.read_word(tmp_path)["word"] == "quiet"
    assert "your devices, needs only (quiet)" in capsys.readouterr().out
    assert _cli(tmp_path, "loud", "5") == 0
    assert "your devices (loud)" in capsys.readouterr().out
    assert _cli(tmp_path, "loud", "off") == 0
    assert presence.read_word(tmp_path) is None
    assert "the desk only" in capsys.readouterr().out
    assert _cli(tmp_path, "quiet", "soon") == 2
    for w in ("sleep", "away", "back"):
        assert _cli(tmp_path, w) == 0
    assert _cli(tmp_path, "status") == 0
    assert capsys.readouterr().out.strip().splitlines()[-1] == intent.describe_line(tmp_path)


def test_a_short_write_does_not_truncate_the_record(tmp_path, monkeypatch):
    """The record is replaced whole from a temp file; a write that takes only part of the bytes must be continued, not
    renamed into place as a half-written (unreadable) record."""
    real = os.write

    def short(fd, data):
        return real(fd, bytes(data)[:3])

    monkeypatch.setattr(presence.os, "write", short)
    intent._write_record(tmp_path, {"base": _rec()["base"], "overlay": AWAY()})
    monkeypatch.undo()
    assert intent.read(tmp_path)["status"] == "file" and intent.read(tmp_path)["overlay"]["preset"] == "away"


def test_a_failed_write_keeps_the_record_and_leaves_no_temp_file(tmp_path, monkeypatch):
    """A write that fails part way (a full disk) raises, keeps the record already in place, and removes its temp file."""
    intent._write_record(tmp_path, {"base": _rec()["base"], "overlay": AWAY()})
    real = os.write
    calls = []

    def fails_second(fd, data):
        calls.append(1)
        if len(calls) > 1:
            raise OSError(28, "No space left on device")
        return real(fd, bytes(data)[:3])

    monkeypatch.setattr(presence.os, "write", fails_second)
    with pytest.raises(presence.PresenceError):
        intent._write_record(tmp_path, {"base": _rec()["base"], "overlay": SLEEP()})
    monkeypatch.undo()
    assert intent.read(tmp_path)["overlay"]["preset"] == "away"
    assert [p.name for p in tmp_path.rglob("*.tmp")] == []


def test_effective_is_the_same_answer_as_should_push_for_every_overlay_idle_and_remote_case(tmp_path):
    """The chip and the push decision are one function: over overlays x idle x remote, effective(...).pushing is
    should_push(...).push, and source says whether an overlay or the measurement decided."""
    overlays = [None, AWAY(ts=NOW - 100), AWAY(ts=NOW - 1000), QUIET(60), QUIET(-1), SLEEP(ts=NOW - 100), LOUD(60), LOUD(-1)]
    n = 0
    for oi, ov in enumerate(overlays):
        for idle in (None, 0.0, 120.0, float(FOLLOW), 900.0):
            for rem in ("absent", "fresh", "stale"):
                q = tmp_path / f"q{n}"
                n += 1
                (q / "webpush").mkdir(parents=True, mode=0o700)
                intent._write_record(q, {"base": _rec()["base"], "overlay": ov})
                if rem != "absent":   # fresh: 2 min old; stale: past the window
                    age = 120.0 if rem == "fresh" else presence.ACTIVE_WINDOW_SECS + 1.0
                    (q / "webpush" / presence.REMOTE_NAME).write_text(json.dumps({"at": NOW - age, "source": "remote-terminal"}))
                got = presence.effective(q, NOW, idle=lambda: idle)
                want = presence.should_push(q, "ping", now=NOW, idle=lambda: idle, suggest=False)
                assert got["pushing"] is want.push and got["state"] == want.state and got["reason"] == want.why, (oi, idle, rem)
                assert (got["remote"] is not None) is (rem == "fresh")
                assert got["source"] in ("word", "measured") and got["state"] in ("pushing", "holding", "muted")
                assert got["line"] == intent.describe_line(q, now=NOW, idle=lambda: idle)
    # source: an overlay decides while it stands; the measurement decides otherwise
    q = tmp_path / "src"
    (q / "webpush").mkdir(parents=True, mode=0o700)
    assert presence.effective(q, NOW, idle=lambda: 900.0)["source"] == "measured"
    intent._write_record(q, {"base": _rec()["base"], "overlay": QUIET(60)})
    got = presence.effective(q, NOW, idle=lambda: 900.0)
    assert got["source"] == "word" and got["word"] == "quiet" and got["since"] == NOW - 100
    intent._write_record(q, {"base": _rec()["base"], "overlay": AWAY(ts=NOW - 1000)})   # spent: he typed after the grace
    assert presence.effective(q, NOW, idle=lambda: 5.0)["source"] == "measured"


def test_a_status_read_writes_no_decision_row_and_a_delivery_does_and_a_fault_reads_as_pushing(tmp_path):
    from levain.webpush import desk

    presence.effective(tmp_path, NOW, idle=lambda: 900.0)
    assert not (tmp_path / "webpush" / desk.DECISIONS_NAME).exists()      # a status read (suggest=False) writes nothing
    presence.should_push(tmp_path, "ping", now=NOW, idle=lambda: 900.0)
    assert (tmp_path / "webpush" / desk.DECISIONS_NAME).exists()          # a delivery keeps what it saw

    def boom():
        raise RuntimeError("no idle")
    got = presence.effective(tmp_path, NOW, idle=boom)                    # no reading: follow reads him inactive and pushes
    assert got["pushing"] is True and got["state"] == "pushing"
