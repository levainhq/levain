"""C11 S5 (Q1 ruled (c), np-61747876): the `attention` ping kind. The coordinator's escalation, its stopped row and its own
question push as kind `attention`, which is urgent: it passes presence `quiet`, not `sleep`. Every other seat's question
keeps kind `question` and stays muted under `quiet`. The pings run through the REAL claude_code `notify` and the REAL
presence rule; only the phone's liveness, the sender and the idle reading are stood in for. Synthetic words."""
from __future__ import annotations

import pytest

from levain.consent import band
from levain.consent.adapters import claude_code as cc
from levain.coordinator import first_reader as fr
from levain.webpush import presence, sender
import levain.webpush as webpush
import levain.consent.auto_away as auto_away

from tests.test_questions_held_ping import Desk, SEAT


class Wire:
    """What the real `notify` handed the sender: (kind, push, hold) per ping."""

    def __init__(self, monkeypatch):
        self.spawned: list[tuple[str, bool, str | None]] = []
        monkeypatch.setattr(webpush, "live", lambda cwd: True)
        monkeypatch.setattr(auto_away, "input_idle_seconds", lambda: 3600.0)    # desk inactive
        monkeypatch.setattr(sender, "spawn", lambda cwd, *, kind, push, hold, **_kw:
                            self.spawned.append((kind, bool(push), hold)) or True)


_REAL_NOTIFY = cc.notify


@pytest.fixture
def rig(tmp_path, monkeypatch):
    d = Desk(tmp_path, monkeypatch)          # stubs notify; this file wants the real one
    monkeypatch.setattr(cc, "notify", _REAL_NOTIFY)
    return d, Wire(monkeypatch)


def _say(d, word):
    presence.set_word(d.dir, word, duration="30m" if word in presence.TIMED else None)


def test_the_urgent_kinds_hold_attention():
    assert "attention" in presence.URGENT_KINDS and "question" not in presence.URGENT_KINDS


def test_an_escalated_question_pushes_as_attention_under_quiet(rig):
    """Reproduced first: on 528d60f4 this ping went out as kind `question` and was muted by `quiet`."""
    d, wire = rig
    tok = d.coordinator()
    q, _ = d.ask()
    _say(d, "quiet")
    fr.escalate(d.store, q.id, "outside its envelope", token=tok)
    assert band.release_held_pings(d.dir) == [f"question #{q.n} (escalated): the operator was pinged"]
    assert wire.spawned == [("attention", True, None)]


def test_an_escalated_question_is_muted_under_sleep(rig):
    d, wire = rig
    tok = d.coordinator()
    q, _ = d.ask()
    _say(d, "sleep")
    fr.escalate(d.store, q.id, "outside its envelope", token=tok)
    band.release_held_pings(d.dir)
    assert len(wire.spawned) == 1 and wire.spawned[0][:2] == ("attention", False) and wire.spawned[0][2] == "sleep"


def test_an_unread_release_stays_a_question_and_is_muted_under_quiet(rig):
    d, wire = rig
    d.coordinator()
    d.ask()
    _say(d, "quiet")
    d.later(minutes=10)
    band.release_held_pings(d.dir)
    assert wire.spawned == [("question", False, "quiet")]


def test_a_seats_question_with_no_coordinator_is_muted_under_quiet(rig):
    d, wire = rig
    _say(d, "quiet")
    d.ask()
    assert wire.spawned == [("question", False, "quiet")]


def test_the_stopped_row_pushes_under_quiet_and_not_under_sleep(rig):
    d, wire = rig
    _say(d, "quiet")
    assert band.file_coordinator_stopped(d.dir, "claude_code", "stopped after three deaths", "ep-1") is not None
    _say(d, "sleep")
    assert band.file_coordinator_stopped(d.dir, "claude_code", "stopped after three deaths", "ep-2") is not None
    assert wire.spawned[0] == ("attention", True, None)
    assert wire.spawned[1][:2] == ("attention", False) and wire.spawned[1][2] == "sleep"


def test_the_coordinators_own_question_is_not_held_and_pings_as_attention(rig):
    """S5's one check: `ask`'s held-ping rule held a coordinator's own question behind its own first read."""
    d, wire = rig
    d.coordinator()
    _say(d, "quiet")
    q, text = band.file_question("claude_code", "sess-coord", "which of the two?", [("a", "one"), ("b", "two")], "a",
                                 "the pick", d.dir)
    assert q is not None and "the operator was pinged" in text
    assert wire.spawned == [("attention", True, None)]
    assert d.rows("SELECT COUNT(*) FROM held_pings") == [(0,)]


def test_another_seats_question_is_still_held_while_a_coordinator_holds_the_lease(rig):
    d, wire = rig
    d.coordinator()
    q, text = d.ask()
    assert "not pinged yet" in text and wire.spawned == []
    assert d.rows("SELECT question_id FROM held_pings") == [(q.id,)]


def test_a_faulted_coordinator_lookup_sends_at_once_as_a_plain_question_and_holds_nothing(rig, monkeypatch):
    """L3 r1 (codex MED, deepseek MED): a lookup fault read False, so a coordinator's own ask sat in held_pings and later went
    out muted. A fault is its own answer: no `attention` (nothing is proven) and no hold behind a first read."""
    d, wire = rig
    d.coordinator()

    def boom(*_a, **_kw):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(fr, "is_coordinator_session", boom)
    _say(d, "quiet")
    q, text = band.file_question("claude_code", "sess-coord", "which of the two?", [("a", "one"), ("b", "two")], "a",
                                 "the pick", d.dir)
    assert q is not None and "not pinged yet" not in text
    assert wire.spawned == [("question", False, "quiet")]
    assert d.rows("SELECT COUNT(*) FROM held_pings") == [(0,)]
