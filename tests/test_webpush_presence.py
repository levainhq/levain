"""The phone-push rule over the one intent record (presence 1b): follow (devices on after 10 min of no laptop input) by default,
the presets' lets_through, loud over all, and the compat words flow's installed half still calls."""
from __future__ import annotations

import json
import os
import stat

import pytest

from levain.consent.adapters import claude_code
from levain.webpush import intent, presence, sender
from levain.webpush.presence import PresenceError, set_word
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


def test_sleep_stands_against_sensed_input_fresh_or_stale_or_missing():
    """np-a93deaf7 "2a": a set sleep ends on a word of his or a typed turn, never on sensed laptop input."""
    old = _rec(SLEEP(ts=NOW - 1000))     # said 1000 s ago, so any input reading below is past the old 180 s grace
    for reading in (50.0, 400.0, 5000.0, None):      # a fresh reading, a stale one, one older than the word, no reading at all
        d = decide(old, reading)
        assert (d.push, d.state, d.by) == (False, "muted", "sleep"), reading
    stale_word = _rec(SLEEP(ts=NOW - 10 * 3600))      # even hours later, with no reading, it stands (no blind bound)
    assert decide(stale_word, None).state == "muted"


def test_away_stands_against_sensed_input_the_shape_of_the_1752_row():
    a = _rec(AWAY(ts=NOW - 600))
    for reading in (10.0, 99.0, 590.0, 5000.0, None):   # input past the old grace, fresh and stale, and none
        assert decide(a, reading).push is True, reading


def test_a_desk_message_ends_a_set_away_and_a_set_sleep():
    for ov in (AWAY(ts=NOW - 600), SLEEP(ts=NOW - 600)):
        rec = _rec(ov)
        assert intent.overlay_ended(rec, NOW, 5.0, NOW - 100) == "desk message"
        assert intent.overlay_ended(rec, NOW, 5.0, NOW - 700) is None      # a message before the word does not
        assert intent.overlay_ended(rec, NOW, 5.0, None) is None           # sensed input alone does not


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


def test_wiring_a_filed_consent_at_an_active_laptop_pushes(tmp_path, monkeypatch):
    """notify() with an approval-class kind is always a ping about a FILED request held for a signed answer (no
    dialog): Phill 11-02 (np-e642162b (a)) it goes to his devices at an active laptop too. Was push False before."""
    _phone(tmp_path)
    calls = _spy(monkeypatch, ACTIVE)
    assert _notify(tmp_path, "consent") is True
    assert [c["push"] for c in calls] == [True]


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


_AWAY_HOLD = 0.3


def _away(marked=True):
    return claude_code.AdapterSettings(away_mode=True, away_marked=marked, away_hold_seconds=_AWAY_HOLD)


def _cx_decide(q, settings, **kw):
    from levain.consent.adapters import codex
    from tests._h4 import h4_env, h4_home

    e = {**_pr_event(), "session_id": "s-codex"}
    return codex.decide(e, q, settings, environ=h4_env(), home=h4_home(), **kw)


def _held_ping(calls, q, agent):
    """The one push an away hold sends for the request it filed (the gate's own ping shape), the request withdrawn."""
    assert [(c["kind"], c["push"], c["hold"], c["url"]) for c in calls] == [("consent", True, None, "/?view=needs")]
    assert calls[0]["ref"].startswith("request:")
    rid = calls[0]["ref"].split(":", 1)[1]
    assert calls[0]["body"].startswith(f"{agent} waits on you up to ") and f"(request {rid})." in calls[0]["body"]
    assert q.get(rid)[1] == "withdrawn"                    # the hold ran out and left the call to the dialog


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_an_away_hold_pings_the_request_to_his_devices_once_and_sends_no_notice(tmp_path, monkeypatch, agent):
    """THE REPRODUCTION (fails on 71992bce: nothing pushes at all, codex HIGH 1). Away on (a signed mark), a paired
    phone, a key that can sign: a plain PermissionRequest is filed and held for a signed answer, and ONE push for that
    request goes to his devices when the hold starts, as the gate pings its filed request. The dialog that asks after
    the hold gets no second notice."""
    from levain.consent.adapters import codex

    q = _consent_queue(tmp_path)
    _phone(q.directory)
    calls = _spy(monkeypatch, ACTIVE)
    mod = codex if agent == "codex" else claude_code
    monkeypatch.setattr(mod, "nothing_can_decide", lambda s, **kw: False)
    max_hold = _AWAY_HOLD + claude_code.CLEANUP_RESERVE
    if agent == "codex":
        assert _cx_decide(q, codex.AdapterSettings(away_mode=True, away_marked=True, away_hold_seconds=_AWAY_HOLD),
                          max_hold=max_hold) is None
    else:
        assert _cc_decide(q, _away(), max_hold=max_hold) is None
    _held_ping(calls, q, agent)


def test_an_away_hold_is_held_only_when_it_ran_and_pinged(tmp_path, monkeypatch):
    """decide_ex's ``held``: True for a hold that ran with its ping started; the hook's own answer is unchanged."""
    from tests._h4 import h4_env, h4_home

    q = _consent_queue(tmp_path)
    _phone(q.directory)
    _spy(monkeypatch, ACTIVE)
    monkeypatch.setattr(claude_code, "nothing_can_decide", lambda s, **kw: False)
    out = claude_code.decide_ex(_pr_event(), q, _away(), environ=h4_env(), home=h4_home(), launch_nonce="",
                                contained_launch=None, max_hold=_AWAY_HOLD + claude_code.CLEANUP_RESERVE)
    assert isinstance(out, claude_code.Abstained) and out.reason == "hold_expired" and out.held is True


def _no_wait_clock():
    # the deadline and the hold read 0; every later read is past the deadline, so the wait after queue.file is 0
    ticks = iter([0.0, 0.0])
    return lambda: next(ticks, 1e9)


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_a_hold_the_deadline_left_no_wait_for_sends_the_dialog_notice(tmp_path, monkeypatch, agent):
    """codex MED 2 (fails on 71992bce: held=True with no hold run, so neither a ping nor a notice). The hold was above
    zero when the request was filed, but the deadline left no wait after it: no hold ran and nothing pinged, so the
    dialog's prompt notice goes, once, for the filed request."""
    from levain.consent.adapters import codex

    q = _consent_queue(tmp_path)
    _phone(q.directory)
    calls = _spy(monkeypatch, ACTIVE)
    held = []
    monkeypatch.setattr(claude_code, "hold_for_answer", lambda *a, **kw: held.append(a) or (None, None))
    monkeypatch.setattr(codex, "hold_for_answer", lambda *a, **kw: held.append(a) or (None, None))
    mod = codex if agent == "codex" else claude_code
    monkeypatch.setattr(mod, "nothing_can_decide", lambda s, **kw: False)
    max_hold = _AWAY_HOLD + claude_code.CLEANUP_RESERVE
    if agent == "codex":
        assert _cx_decide(q, codex.AdapterSettings(away_mode=True, away_marked=True, away_hold_seconds=_AWAY_HOLD),
                          max_hold=max_hold, clock=_no_wait_clock()) is None
    else:
        from tests._h4 import h4_env, h4_home

        out = claude_code.decide_ex(_pr_event(), q, _away(), environ=h4_env(), home=h4_home(), launch_nonce="",
                                    contained_launch=None, max_hold=max_hold, clock=_no_wait_clock())
        assert isinstance(out, claude_code.Abstained) and out.held is False and calls == []   # decide_ex sends no notice
        assert _cc_decide(q, _away(), max_hold=max_hold, clock=_no_wait_clock()) is None
    assert held == []                                                    # no hold ran
    assert [(c["kind"], c["push"]) for c in calls] == [("consent", True)]
    assert calls[0]["body"].startswith(f"{agent} asks in its own dialog: ")
    assert calls[0]["ref"].startswith("request:")


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_an_away_hold_whose_ping_cannot_start_sends_the_dialog_notice(tmp_path, monkeypatch, agent):
    """A hold ran but its ping could not start: ``held`` is false, so the dialog's notice goes (never neither)."""
    from levain.consent.adapters import codex

    q = _consent_queue(tmp_path)
    _phone(q.directory)
    calls = _spy(monkeypatch, ACTIVE)
    monkeypatch.setattr(claude_code, "_notify_ex", lambda *a, **kw: (False, False, False))
    mod = codex if agent == "codex" else claude_code
    monkeypatch.setattr(mod, "nothing_can_decide", lambda s, **kw: False)
    max_hold = _AWAY_HOLD + claude_code.CLEANUP_RESERVE
    if agent == "codex":
        assert _cx_decide(q, codex.AdapterSettings(away_mode=True, away_marked=True, away_hold_seconds=_AWAY_HOLD),
                          max_hold=max_hold) is None
    else:
        assert _cc_decide(q, _away(), max_hold=max_hold) is None
    assert [(c["kind"], c["push"]) for c in calls] == [("consent", True)]
    assert calls[0]["body"].startswith(f"{agent} asks in its own dialog: ")


def test_an_away_hold_ping_is_never_silenced_by_a_faulting_dedupe_read(tmp_path, monkeypatch):
    """L3 round c (complement MED 1): the request id was minted for this hook call, so nothing can have pinged it; a
    `pinged` read that faults reads as "already pinged" and used to silence both this ping and the dialog's notice."""
    import levain.webpush as wp

    qdir = tmp_path / "q"
    qdir.mkdir()
    _phone(qdir)
    calls = _spy(monkeypatch, ACTIVE)
    monkeypatch.setattr(wp, "pinged", lambda *a, **kw: True)   # what a locked pings DB reads as
    assert claude_code.hold_ping(_away(), qdir, "claude_code", "Bash: touch x", "r7", 300.0) is True
    assert [c["ref"] for c in calls] == ["request:r7"]
    assert calls[0]["body"] == "claude_code waits on you up to 300 s: Bash: touch x (request r7)."


def test_an_away_hold_ping_never_raises(tmp_path, monkeypatch):
    qdir = tmp_path / "q"
    qdir.mkdir()
    _phone(qdir)

    def boom(*a, **kw):
        raise RuntimeError("notify")
    monkeypatch.setattr(claude_code, "_notify_ex", boom)
    assert claude_code.hold_ping(_away(), qdir, "claude_code", "x", "r1", 1.0) is False


def test_an_away_hold_ping_under_a_mute_is_queued_not_pushed(tmp_path, monkeypatch):
    """A mute still holds the ping (notify's existing hold path queues it under its word), and a queued ping counts as
    the request's one ping (``hold_ping`` true), so no dialog notice follows it."""
    qdir = tmp_path / "q"
    qdir.mkdir()
    _phone(qdir)
    calls = _spy(monkeypatch, ACTIVE)
    intent.apply_settings(qdir, lets_through="nothing", idle=lambda: ACTIVE)
    assert claude_code.hold_ping(_away(marked=False), qdir, "claude_code", "Bash: touch x", "r3", 300.0) is True
    assert [(c["kind"], c["push"], bool(c["hold"]), c["ref"]) for c in calls] == [("consent", False, True, "request:r3")]


def test_a_mute_without_a_mark_starts_no_hold_and_says_nothing(tmp_path, monkeypatch):
    """The hold is unchanged: under the explicit word with no signed mark the presence record decides, and a mute keeps
    approvals off his devices, so no hold runs and nothing is filed for one; the dialog asks and the mute says nothing
    (the prompt notice's own rule)."""
    q = _consent_queue(tmp_path)
    _phone(q.directory)
    calls = _spy(monkeypatch, ACTIVE)
    held = []
    monkeypatch.setattr(claude_code, "hold_for_answer", lambda *a, **kw: held.append(a) or (None, None))
    monkeypatch.setattr(claude_code, "nothing_can_decide", lambda s, **kw: False)
    intent.apply_settings(q.directory, lets_through="nothing", idle=lambda: ACTIVE)
    assert _cc_decide(q, _away(marked=False), max_hold=_AWAY_HOLD + claude_code.CLEANUP_RESERVE) is None
    assert held == [] and calls == []


def test_a_signed_mark_under_a_mute_still_holds_and_pushes(tmp_path, monkeypatch):
    """The mark rule is unchanged: a standing signed mark holds the request and pushes its approval whatever the record
    says (auto_away.marked_push), so the held request's one ping goes."""
    q = _consent_queue(tmp_path)
    _phone(q.directory)
    calls = _spy(monkeypatch, ACTIVE)
    monkeypatch.setattr(claude_code, "nothing_can_decide", lambda s, **kw: False)
    intent.apply_settings(q.directory, lets_through="nothing", idle=lambda: ACTIVE)
    assert _cc_decide(q, _away(), max_hold=_AWAY_HOLD + claude_code.CLEANUP_RESERVE) is None
    _held_ping(calls, q, "claude_code")


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


# --- a request FILED and held for a signed answer, no dialog (Phill 11-02, np-e642162b (a), the same ruling) ------------

def _gated_edit(ws, posture, **extra):
    (ws / ".claude").mkdir(parents=True, exist_ok=True)
    return {"hook_event_name": "PreToolUse", "session_id": "s-filed", "cwd": str(ws), "permission_mode": posture,
            "tool_name": "Edit", "tool_use_id": f"tu-{posture}",
            "tool_input": {"file_path": str(ws / ".claude" / "settings.json"), "old_string": "a", "new_string": "b"},
            **extra}


@pytest.mark.parametrize("posture", ["bypassPermissions", "acceptEdits", "auto"])
def test_a_filed_and_held_request_at_an_active_laptop_pushes_its_ping(tmp_path, monkeypatch, posture):
    """THE REPRODUCTION (fails on 6c5236c2: the ping is recorded held "active", push False). A gated T3 call in a
    non-default posture, him active at the laptop (follow reading 5 s), a key could sign: the request is filed and held
    for a signed answer (no dialog will ask), and its ping goes to his devices. The hold's own question is unchanged."""
    from tests._h4 import h4_env, h4_home

    q = _consent_queue(tmp_path)
    _phone(q.directory)
    calls = _spy(monkeypatch, ACTIVE)
    monkeypatch.setattr(claude_code, "nothing_can_decide", lambda s, **kw: False)   # a key could sign
    assert intent.approvals_to_devices(q.directory, idle=lambda: ACTIVE)[0] is False   # the hold's question: unchanged
    settings = claude_code.AdapterSettings(gate_hold_seconds=0.2)
    out = claude_code.gate(_gated_edit(tmp_path / "ws", posture), q, settings, home=h4_home(), environ=h4_env(),
                           sleep=lambda s: None)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"          # held to D, never passed
    assert [(c["kind"], c["push"], c["hold"]) for c in calls] == [("consent", True, None)]
    assert calls[0]["ref"].startswith("request:")
    assert q.get(calls[0]["ref"].split(":", 1)[1]) is not None                # the request was filed


def test_a_filed_codex_request_at_an_active_laptop_pushes_its_ping(tmp_path, monkeypatch):
    from levain.consent.adapters import codex
    from tests._h4 import h4_env, h4_home

    q = _consent_queue(tmp_path)
    _phone(q.directory)
    calls = _spy(monkeypatch, ACTIVE)
    monkeypatch.setattr(codex, "nothing_can_decide", lambda s, **kw: False)
    ws = tmp_path / "ws"
    (ws / ".codex").mkdir(parents=True)
    event = {"hook_event_name": "PreToolUse", "session_id": "s-cx", "turn_id": "t1", "transcript_path": None,
             "cwd": str(ws), "model": "m", "permission_mode": "bypassPermissions", "tool_name": "apply_patch",
             "tool_input": {"command": "*** Begin Patch\n*** Add File: .codex/hooks.json\n+{}\n*** End Patch"},
             "tool_use_id": "call-cx"}
    out = codex.gate(event, q, codex.AdapterSettings(gate_hold_seconds=0.2), home=h4_home(), environ=h4_env(),
                     codex_dir=ws / ".codex", sleep=lambda s: None)
    assert out is not None                                                    # held to D, never passed
    assert [(c["kind"], c["push"], c["hold"]) for c in calls] == [("consent", True, None)]
    assert calls[0]["ref"].startswith("request:")


def test_a_filed_request_ping_is_still_held_by_a_mute(tmp_path, monkeypatch):
    """Only a mute (lets_through not admitting approvals) holds the ping about a filed request, under its word."""
    _phone(tmp_path)
    calls = _spy(monkeypatch, ACTIVE)
    intent.apply_settings(tmp_path, lets_through="nothing", idle=lambda: ACTIVE)
    for kind in ("consent", "push", "reping"):
        assert _notify(tmp_path, kind) is False
    assert [c["push"] for c in calls] == [False] * 3 and all(c["hold"] for c in calls)


def test_a_filed_request_ping_goes_with_devices_off_and_other_pings_still_hold(tmp_path, monkeypatch):
    _phone(tmp_path)
    calls = _spy(monkeypatch, INACTIVE)
    intent.apply_settings(tmp_path, devices="off", idle=lambda: INACTIVE)
    for kind in ("consent", "push", "reping"):
        assert _notify(tmp_path, kind) is True
    assert _notify(tmp_path, "ruling") is False            # an other-class ping: held as before
    assert [c["push"] for c in calls] == [True, True, True, False]
    assert intent.approvals_to_devices(tmp_path, idle=lambda: INACTIVE)[0] is False   # the hold's question: unchanged


def test_a_held_approval_ping_is_released_at_an_active_laptop_once_the_mute_ends():
    """held.plan reads the rule a new filed-request ping meets: an approval ping held by a mute goes once the mute
    ends, at the laptop too; an other-class ping held the same way waits for his devices."""
    from levain.webpush import held

    rec = intent.read(None, 0.0)
    items = [{"kind": "consent", "reason": "quiet"}, {"kind": "ruling", "reason": "quiet"},
             {"kind": "reping", "reason": "active"}]
    active, muted = held.plan(items, rec, 5.0, 1000.0)
    assert [i["kind"] for i in muted] == ["consent"] and [i["kind"] for i in active] == ["reping"]


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
    assert f"your devices, {intent.lets_label('needs')} (quiet)" in capsys.readouterr().out
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
    from levain.webpush import desk

    intent._write_record(q, {"base": _rec()["base"], "overlay": AWAY(ts=NOW - 1000)})
    assert presence.effective(q, NOW, idle=lambda: 5.0)["source"] == "word"       # sensed input, past the old grace, does not spend it
    desk.note_desk_message(q, "test-hook", now=NOW - 500)                          # a typed turn into a session does
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


def test_a_remote_turn_after_away_still_pushes_and_follow_with_fresh_remote_still_pushes(tmp_path):
    """Desk 7 / Phill np-a93deaf7 "2a": an away word stands against sensed laptop input, and remote work (a remote-terminal
    or remote-control turn) never counts as the desk: with away set the laptop reading cannot hold the ping, and in follow
    mode a fresh remote record still pushes before the laptop reading is consulted."""
    for source in ("remote-terminal", "remote-control"):
        q = tmp_path / f"away-{source}"
        (q / "webpush").mkdir(parents=True, mode=0o700)
        intent._write_record(q, {"base": _rec()["base"], "overlay": AWAY(ts=NOW - 1000)})
        (q / "webpush" / presence.REMOTE_NAME).write_text(json.dumps({"at": NOW - 30.0, "source": source}))
        got = presence.should_push(q, "ping", now=NOW, idle=lambda: 0.0, suggest=False)
        assert got.push is True and got.state == "pushing", (source, got)
        q2 = tmp_path / f"follow-{source}"
        (q2 / "webpush").mkdir(parents=True, mode=0o700)
        intent._write_record(q2, {"base": _rec()["base"], "overlay": None})
        (q2 / "webpush" / presence.REMOTE_NAME).write_text(json.dumps({"at": NOW - 30.0, "source": source}))
        got2 = presence.should_push(q2, "ping", now=NOW, idle=lambda: 0.0, suggest=False)
        assert got2.push is True and got2.state == "pushing" and "remote" in got2.why, (source, got2)


def test_read_sample_is_the_one_public_reading_of_the_rules_inputs(tmp_path):
    """flow's phone bridge reads presence through ONE public function, not intent's private helpers: the record, the idle
    reading (a desk message counted in), the desk message time, the remote record, the overlay's standing."""
    q = tmp_path / "q"
    (q / "webpush").mkdir(parents=True, mode=0o700)
    intent._write_record(q, {"base": _rec()["base"], "overlay": AWAY(ts=NOW - 1000)})
    (q / "webpush" / presence.REMOTE_NAME).write_text(json.dumps({"at": NOW - 30.0, "source": "remote-terminal"}))
    got = intent.read_sample(q, NOW, lambda: 5.0)
    assert set(got) == {"record", "reading", "last_input_at", "desk_message_at", "remote", "overlay_active", "now"}
    assert got["record"]["status"] == "file" and got["reading"] == 5.0 and got["last_input_at"] == NOW - 5.0
    assert got["desk_message_at"] is None and got["remote"]["source"] == "remote-terminal" and got["now"] == NOW
    assert got["overlay_active"] is True   # 2a: sensed laptop input (5 s ago) does not end an away word
    from levain.webpush import desk

    desk.note_desk_message(q, "test-hook", now=NOW + 10)
    after = intent.read_sample(q, NOW + 20, lambda: 500.0)
    assert after["desk_message_at"] == NOW + 10 and after["overlay_active"] is False   # a typed turn ends it
    q2 = tmp_path / "q2"
    (q2 / "webpush").mkdir(parents=True, mode=0o700)
    assert intent.read_sample(q2, NOW, lambda: None)["reading"] is None   # no sensor, no desk message: unknown
