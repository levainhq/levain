"""Presence 1b, lane B (DESIGN §4, §12 row 6): consent reads the same record the pings do.

An approval reaches the operator's devices for a signed answer exactly when ``should_push(queue_dir, "consent")`` is
true, and the hook holds a request for a signed answer (away_hold > 0) exactly then. A signed away mark that no signed
leave has ended is the one floor over the record: it holds whatever the record says."""
from __future__ import annotations

import itertools

import pytest

from levain.consent import auto_away, away
from levain.consent.adapters import claude_code as cc
from levain.consent.adapters import codex as cx
import time

from levain.webpush import desk, intent, presence
from tests.test_consent_away import _cockpit, _key, _queue

READINGS = (2.0, 899.0, 900.0, 5000.0, None)   # at the laptop · just under 15 min · at 15 min · long gone · no reading
# every way the record can be set: a preset, or a direct setting (the base, an overlay is covered by the presets)
STATES = (
    ("standing follow/10m", lambda q: None),
    ("away", lambda q: intent.apply_preset(q, "away")),
    ("quiet", lambda q: intent.apply_preset(q, "quiet")),
    ("sleep", lambda q: intent.apply_preset(q, "sleep")),
    ("loud", lambda q: intent.apply_preset(q, "loud")),
    ("devices off", lambda q: intent.apply_settings(q, devices="off")),
    ("devices on", lambda q: intent.apply_settings(q, devices="on")),
    ("follow 15m", lambda q: intent.apply_settings(q, devices="follow", follow_after="15m")),
    ("lets nothing", lambda q: intent.apply_settings(q, lets_through="nothing")),
    ("lets approvals", lambda q: intent.apply_settings(q, lets_through="approvals")),
)


@pytest.fixture(scope="module")
def cockpit(tmp_path_factory):
    return _cockpit(tmp_path_factory.mktemp("ck"))


def _scratch(tmp_path, cockpit):
    q = _queue(tmp_path, cockpit)
    (q / "webpush").mkdir(mode=0o700)
    return q


def _message_age(q, secs):
    """1a (Phill 11-03): follow reads the newest typed desk message, so a reading of ``secs`` is a message that old (``None``: none)."""
    path = q / "webpush" / desk.DESK_MESSAGE_NAME
    if secs is None:
        path.unlink(missing_ok=True)
    else:
        desk._write(q, desk.DESK_MESSAGE_NAME, {"at": time.time() - secs, "source": "test", "desk": ""})


@pytest.mark.parametrize("agent,mod", [("claude_code", cc), ("codex", cx)])
def test_consent_equals_push_and_the_hold_is_zero_exactly_when_false(tmp_path, cockpit, agent, mod):
    q = _scratch(tmp_path, cockpit)
    st = away.settings(q, agent)
    for (label, setup), reading in itertools.product(STATES, READINGS):
        intent.apply_preset(q, "back")                  # each state starts from the standing record
        intent.apply_settings(q, devices="follow", follow_after=600, lets_through="everything")
        setup(q)
        _message_age(q, reading)
        idle = (lambda r=reading: r)
        pushes = presence.should_push(q, "consent", idle=idle, suggest=False).push
        to_devices, why = intent.approvals_to_devices(q, idle=idle)
        blind = intent.approvals_route(q, idle=idle)[2]
        hold = mod.away_hold(st, agent, idle, queue_dir=q)
        away_now = mod.effective_away(st, idle, queue_dir=q)
        where = f"{label} / reading {reading}"
        assert to_devices == pushes, where
        # the one named divergence: a follow that pushes only for want of a reading does not hold (L3 r1 complement 1)
        assert blind == (pushes and reading is None and label in ("standing follow/10m", "follow 15m", "lets approvals")), where
        assert (hold > 0) == (pushes and not blind), where
        assert away_now[0] == (pushes and not blind) and away_now[1] == "intent", where
        assert hold in (0.0, auto_away.DEFAULT_HOLDS[agent]), where   # the length is per agent, never presence
    # a coverage check that the matrix is not all one answer
    outcomes = set()
    for r in READINGS:
        _message_age(q, r)
        outcomes.add(intent.approvals_to_devices(q, idle=lambda: r)[0])
    assert outcomes == {True, False}


def test_a_file_hold_sets_the_length_and_no_enrolled_key_means_the_agents_dialog(tmp_path, cockpit):
    q = _scratch(tmp_path, cockpit)
    intent.apply_preset(q, "away")
    path = q / cc.SETTINGS_NAME
    path.write_text(path.read_text().replace('"away_hold_seconds": 0', '"away_hold_seconds": 77'))
    assert cc.away_hold(away.settings(q, "claude_code"), "claude_code", lambda: 2.0, queue_dir=q) == 77.0
    # the record says devices, but no enrolled key could sign from one: not away, and the line says so
    own = tmp_path / "nokey"
    own.mkdir()
    q2 = _scratch(own, _cockpit(own, enrol=False))
    intent.apply_preset(q2, "away")
    st = away.settings(q2, "claude_code")
    assert intent.approvals_to_devices(q2, idle=lambda: 2.0)[0] is True
    got = cc.effective_away(st, lambda: 2.0, queue_dir=q2)
    assert got[:2] == (False, "intent_unsignable") and cc.away_hold(st, "claude_code", lambda: 2.0, queue_dir=q2) == 0.0
    assert auto_away.approvals_note(q2) == "the agent's own dialog (no enrolled key can sign from a device)"
    assert "Approvals: the agent's own dialog (no enrolled key can sign from a device)." in \
        intent.describe_line(q2, idle=lambda: 2.0)


def test_an_unleft_mark_holds_whatever_the_record_says_until_a_signed_leave(tmp_path, cockpit):
    q = _scratch(tmp_path, cockpit)
    away.set_away(q, "claude_code", True)
    marked = away.settings(q, "claude_code")
    assert marked.away_marked
    for (label, setup), reading in itertools.product(STATES, READINGS):
        intent.apply_preset(q, "back")
        intent.apply_settings(q, devices="follow", follow_after=600, lets_through="everything")
        setup(q)
        idle = (lambda r=reading: r)
        assert cc.away_hold(marked, "claude_code", idle, queue_dir=q) == auto_away.DEFAULT_HOLDS["claude_code"], \
            f"{label} / reading {reading}"
    # the record at the desk says no, the mark says yes: the line names the floor and the command that leaves it
    intent.apply_settings(q, devices="off")
    assert intent.approvals_to_devices(q, idle=lambda: 2.0)[0] is False
    note = auto_away.approvals_note(q)
    assert note == ("your devices, for a signed answer (a signed away stands; leave: levain consent away off "
                    "--signing-key <key>)")
    assert f"Approvals: {note}." in intent.describe_line(q, idle=lambda: 2.0)
    # the clause never samples the record itself (the line is one sample); a caller that has it says where the record
    # sends approvals, and where that is the devices already, the clause adds nothing
    intent.apply_preset(q, "away")
    assert auto_away.approvals_note(q, to_devices=True) is None
    assert auto_away.approvals_note(q, to_devices=False) == note
    # a signed leave ends the floor: the hold follows the record again
    intent.apply_settings(q, devices="off")
    away.set_away(q, "claude_code", False, signing_key=_key(q))
    left = away.settings(q, "claude_code")
    assert not left.away_marked and auto_away.approvals_note(q) is None
    assert cc.away_hold(left, "claude_code", lambda: 5000.0, queue_dir=q) == 0.0
    intent.apply_preset(q, "away")
    assert cc.away_hold(left, "claude_code", lambda: 2.0, queue_dir=q) == auto_away.DEFAULT_HOLDS["claude_code"]


@pytest.mark.parametrize("agent", ["claude_code", "codex"])
def test_the_hook_entry_points_hold_on_the_record_and_release_when_it_changes(tmp_path, cockpit, agent, monkeypatch):
    """The call sites (hold at the start, recheck while held) pass the queue directory they hold: at the laptop, an
    `away` preset holds the request, and `back` releases it to the agent's own dialog within the recheck."""
    from tests.test_consent_presence import EVENTS, _hook, _row, _wait_pending

    monkeypatch.setattr(auto_away, "input_idle_seconds", lambda: 2.0)   # input just now: the old rule would not hold
    q = _scratch(tmp_path, cockpit)
    mod, event = EVENTS[agent]
    intent.apply_preset(q, "away")
    t, out, _started = _hook(mod, event, q)
    _wait_pending(q)
    intent.apply_preset(q, "back")
    desk.note_desk_message(q, "test")     # 1a: `back` is follow, and only a typed desk message returns follow to the desk
    t.join(10)
    assert not t.is_alive() and out.getvalue() == ""
    state, note = _row(q)
    assert state == "withdrawn" and note.startswith("away mode ended (follow: desk message just now)")


@pytest.mark.parametrize("setup", [lambda q: intent.apply_settings(q, devices="off"),
                                   lambda q: intent.apply_settings(q, lets_through="nothing")])
def test_a_standing_mark_that_holds_an_approval_also_pushes_it(tmp_path, cockpit, monkeypatch, setup):
    """L3 r1 codex HIGH, reproduced (R1): a signed away mark held the request (away_state: word) while the consent ping
    read should_push alone, so at devices off / lets nothing the phone never heard of what the hook waited on. The
    consent ping now reads the one mark rule the hold reads (auto_away.approval_push)."""
    import levain.webpush as webpush
    from levain.webpush import sender

    q = _scratch(tmp_path, cockpit)
    setup(q)
    spawned = []
    monkeypatch.setattr(webpush, "live", lambda qd: True)
    monkeypatch.setattr(sender, "spawn", lambda qd, **kw: spawned.append(kw) or True)
    away.set_away(q, "claude_code", True)
    marked = away.settings(q, "claude_code")
    assert cc.away_hold(marked, "claude_code", lambda: 2.0, queue_dir=q) > 0          # the hook holds it ...
    assert presence.should_push(q, "consent", idle=lambda: 2.0, suggest=False).push is False   # ... the record alone says no
    assert cc.notify(marked, "levain: x", cwd=q, ref="request:1") is True               # ... and the approval goes
    assert spawned[-1]["push"] is True and spawned[-1]["hold"] is None
    # a non-approval ping under the mark still reads the record; an unmarked approval reads the record as a FILED
    # request's ping (Phill 11-02, np-e642162b (a)): devices off does not hold it, only a mute does
    plain = away.settings(q, "codex")
    assert not plain.away_marked
    muted = intent.read(q)["base"]["lets_through"] == "nothing"
    assert cc.notify(plain, "levain: y", cwd=q, ref="request:2") is (not muted) and spawned[-1]["push"] is (not muted)
    assert auto_away.approval_push(marked, q, "notify", idle=lambda: 2.0, suggest=False).push is False


def test_no_laptop_reading_pushes_but_never_holds_and_the_line_names_it(tmp_path, cockpit, monkeypatch):
    """L3 r1 complement MED #1, reproduced (R3): with no laptop input reading (Linux, an ioreg failure) follow reads as
    devices on, so every approval was held up to the hold with nothing measured showing him away. The push stays
    fail-open; the hold fails to the agent's own dialog, and the describe line's Approvals clause says so. A fresh remote
    record (a measurement) still holds, as does an explicit devices on."""
    q = _scratch(tmp_path, cockpit)
    blind = lambda: None   # noqa: E731
    st = away.settings(q, "codex")
    assert presence.should_push(q, "consent", idle=blind, suggest=False).push is True
    assert cx.away_hold(st, "codex", blind, queue_dir=q) == 0.0
    assert cx.effective_away(st, blind, queue_dir=q)[:2] == (False, "intent")
    assert ("Approvals: the agent's own dialog (no laptop input reading, so nothing measured shows you away; the ping "
            "still reaches your devices).") in intent.describe_line(q, idle=blind)
    presence.note_remote_activity(q, "remote-terminal")
    assert cx.away_hold(st, "codex", blind, queue_dir=q) == auto_away.DEFAULT_HOLDS["codex"]
    assert "Approvals:" not in intent.describe_line(q, idle=blind)
    (q / "webpush" / presence.REMOTE_NAME).unlink()
    intent.apply_settings(q, devices="on")
    assert cx.away_hold(st, "codex", blind, queue_dir=q) == auto_away.DEFAULT_HOLDS["codex"]


def test_an_agent_with_no_settings_file_is_not_counted_in_the_approvals_clause(tmp_path, cockpit):
    """His real store: claude_code is set up and can sign, codex has no settings file. The shared queue.db made codex count
    as "having a store", read as unsignable, and the line said no enrolled key could sign from a device (11-02, desk 6)."""
    q = _scratch(tmp_path, cockpit)
    intent.apply_preset(q, "away")
    assert (q / cx.SETTINGS_NAME).exists()
    assert auto_away.approvals_note(q, to_devices=True) is None   # both set up and signable: nothing to say
    (q / cx.SETTINGS_NAME).unlink()
    assert not away.settings(q, "codex").cockpit_data_root         # codex is not set up
    assert auto_away.approvals_note(q, to_devices=True) is None
    assert auto_away.approvals_note(q) is None
    assert "Approvals:" not in intent.describe_line(q, idle=lambda: 2.0)
