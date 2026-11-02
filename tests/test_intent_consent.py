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
from levain.webpush import intent, presence
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


@pytest.mark.parametrize("agent,mod", [("claude_code", cc), ("codex", cx)])
def test_consent_equals_push_and_the_hold_is_zero_exactly_when_false(tmp_path, cockpit, agent, mod):
    q = _scratch(tmp_path, cockpit)
    st = away.settings(q, agent)
    for (label, setup), reading in itertools.product(STATES, READINGS):
        intent.apply_preset(q, "back")                  # each state starts from the standing record
        intent.apply_settings(q, devices="follow", follow_after=600, lets_through="everything")
        setup(q)
        idle = (lambda r=reading: r)
        pushes = presence.should_push(q, "consent", idle=idle, suggest=False).push
        to_devices, why = intent.approvals_to_devices(q, idle=idle)
        hold = mod.away_hold(st, agent, idle, queue_dir=q)
        away_now = mod.effective_away(st, idle, queue_dir=q)
        where = f"{label} / reading {reading}"
        assert to_devices == pushes, where
        assert (hold > 0) == pushes, where
        assert away_now[0] == pushes and away_now[1] == "intent", where
        assert hold in (0.0, auto_away.DEFAULT_HOLDS[agent]), where   # the length is per agent, never presence
    # a coverage check that the matrix is not all one answer
    outcomes = {intent.approvals_to_devices(q, idle=lambda: r)[0] for r in READINGS}
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
    t.join(10)
    assert not t.is_alive() and out.getvalue() == ""
    state, note = _row(q)
    assert state == "withdrawn" and note.startswith("away mode ended (follow: input at the laptop just now)")
