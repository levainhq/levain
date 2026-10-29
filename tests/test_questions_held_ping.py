"""C5-1 (Phill 10-22, "4a"): hold ``levain ask``'s ping while a coordinator holds a LIVE lease; any question it has not
read within 10 minutes pings him and lands in what waits on him. The lane's run (project_memory 1022-19/c5/
c5_1_run.txt) drove the real CLI with a real notify command; these pin it on a store clock the test moves. The ping
is observed at the agent's notify seam. Synthetic words."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from levain.consent import band
from levain.consent.adapters import claude_code as cc
from levain.consent.questions import QuestionStore
from levain.coordinator import first_reader as fr
from levain.coordinator import lease as lease_mod
from levain.coordinator import review
from levain.coordinator.store import CoordStore

NOW = datetime(2026, 10, 22, 21, 0, tzinfo=timezone.utc)
SEAT = "sess-worker-1"


class Desk:
    """A consent directory whose store runs on a clock the test moves, and the pings sent from it."""

    def __init__(self, tmp_path, monkeypatch):
        self.dir = tmp_path / "consent"
        self.dir.mkdir(mode=0o700)
        self.now = [NOW]
        self.store = CoordStore(self.dir, clock=lambda: self.now[0])
        monkeypatch.setattr(fr, "_coord_store", lambda d: CoordStore(d, clock=lambda: self.now[0]), raising=False)
        self.sent: list[str] = []
        monkeypatch.setattr(cc, "notify", lambda settings, text, cwd=None, **_kw: self.sent.append(text) or True)
        self.tmp = tmp_path

    def coordinator(self) -> int:
        """A coordinator launch, active, holding the lease (the real path: a drop wakes it, it activates, it claims)."""
        ws = self.tmp / "coordws"
        ws.mkdir(exist_ok=True)
        self.store.coord_set_agent("claude", cwd=str(ws))
        self.store.accept_drop("a scratch drop", surface="cli")
        ln = self.store.coord_status()["launches"][0]
        self.store.launch_activate(ln["launch_id"], harness_session_id="sess-coord")
        return self.store.coord_claim(ln["launch_id"], self.store.launch(ln["launch_id"]).session_key)

    def ask(self, what="rename helper to _x?"):
        q, text = band.file_question("claude_code", SEAT, what, [("a", "rename it"), ("b", "keep it")], "a",
                                     "the rename", self.dir)
        assert q is not None, text
        return q, text

    def rows(self, sql):
        conn = sqlite3.connect(self.dir / "dispatch.db")
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def later(self, **kw):
        self.now[0] = self.now[0] + timedelta(**kw)


@pytest.fixture
def desk(tmp_path, monkeypatch):
    return Desk(tmp_path, monkeypatch)


def test_a_question_asked_while_a_coordinator_holds_the_live_lease_is_not_pinged(desk):
    """Before C5-1 ``levain ask`` pinged at filing whoever held the lease (red on 198f0b11: one ping sent)."""
    desk.coordinator()
    q, text = desk.ask()
    assert desk.sent == [] and "not pinged yet: a coordinator holds the live lease" in text
    assert desk.rows("SELECT question_id FROM held_pings") == [(q.id,)]


def test_an_expired_coordinator_lease_does_not_hold_the_ping(desk):
    """A coordinator lease past its until (its holder silent) holds nothing: the ping goes at filing."""
    desk.coordinator()
    desk.later(minutes=lease_mod.LEASE_TTL.total_seconds() / 60 + 1)
    _q, text = desk.ask()
    assert len(desk.sent) == 1 and "the operator was pinged" in text
    assert desk.rows("SELECT COUNT(*) FROM held_pings") == [(0,)]


def test_a_lease_the_operator_holds_does_not_hold_the_ping(desk):
    desk.store.coord_take()
    desk.ask()
    assert len(desk.sent) == 1


def test_with_no_coordinator_store_the_ping_goes_and_no_store_is_made(desk):
    desk.ask()
    assert len(desk.sent) == 1 and not (desk.dir / "dispatch.db").exists()


def test_an_unread_question_pings_after_ten_minutes_once_and_waits_on_you(desk):
    desk.coordinator()
    q, _ = desk.ask()
    desk.later(minutes=9, seconds=59)
    assert band.release_held_pings(desk.dir) == [] and desk.sent == []
    desk.later(seconds=1)
    assert band.release_held_pings(desk.dir) == [f"question #{q.n} (unread): the operator was pinged"]
    assert len(desk.sent) == 1 and desk.sent[0].splitlines()[0] == f"levain: claude_code asks (band 1, question #{q.n}): " \
                                                                      f"{q.what}"
    assert "has not read it in 10 minutes" in desk.sent[0]
    waiting = review.waiting_on_you(desk.store, sign_open=lambda _r: None,
                                    answer_open=review.question_open(desk.dir))
    assert [r["ref_id"] for r in waiting["loose"]] == [q.id]
    assert band.release_held_pings(desk.dir) == [] and len(desk.sent) == 1          # once


def test_the_gate_hook_is_what_checks(desk):
    """No watcher needed: any session's hook in the directory releases a due ping."""
    desk.coordinator()
    desk.ask()
    desk.later(minutes=10)
    assert band.question_context(desk.dir, "claude_code", "sess-someone-else") is None
    assert len(desk.sent) == 1


def test_an_escalated_question_pings_at_once(desk):
    tok = desk.coordinator()
    q, _ = desk.ask()
    fr.escalate(desk.store, q.id, "outside its envelope", token=tok)
    assert band.release_held_pings(desk.dir) == [f"question #{q.n} (escalated): the operator was pinged"]
    assert "The coordinator escalated it to you." in desk.sent[0]
    desk.later(minutes=10)
    assert band.release_held_pings(desk.dir) == [] and len(desk.sent) == 1


def test_a_know_holds_the_ping_for_good(desk):
    """The coordinator answered it as a KNOW (the KNOW is the review item): no ping, not even after 10 minutes."""
    desk.coordinator()
    q, _ = desk.ask()
    with desk.store._write() as (conn, now):
        rid = desk.store._reply(conn, None, "know", "questions.db", q.id, {"text": "k"}, now)
        conn.execute("INSERT INTO first_reads (question_id, outcome, reply_id, at) VALUES (?, 'know', ?, ?)",
                     (q.id, rid, now))
    desk.later(minutes=30)
    assert band.release_held_pings(desk.dir) == [] and desk.sent == []


def test_a_question_the_operator_answered_meanwhile_is_released_without_a_ping(desk):
    desk.coordinator()
    q, _ = desk.ask()
    QuestionStore(desk.dir).answer(q.id, option="b", channel="cli")
    desk.later(minutes=10)
    assert band.release_held_pings(desk.dir) == [] and desk.sent == []
    assert desk.rows("SELECT cause FROM held_ping_releases") == [("answered",)]
    assert desk.rows("SELECT COUNT(*) FROM replies WHERE kind = 'answer'") == [(0,)]


def test_a_store_fault_at_filing_pings_as_before(desk, monkeypatch):
    desk.coordinator()

    def broken(d):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(fr, "_coord_store", broken)
    desk.ask()
    assert len(desk.sent) == 1


def test_an_unread_release_is_the_first_read_and_the_coordinator_cannot_answer_it_after(desk):
    """L3 r3 (codex MED): the 10-minute release filed the escalation's ANSWER record but no ``first_reads`` row, so a
    later ``coord answer`` passed ``_read`` and could file a KNOW contradicting the escalation (red on 3540ae0c: no
    row, and the refusal did not name the escalation)."""
    tok = desk.coordinator()
    q, _ = desk.ask()
    desk.later(minutes=10)
    assert band.release_held_pings(desk.dir) == [f"question #{q.n} (unread): the operator was pinged"]
    reply = desk.rows("SELECT reply_id FROM replies WHERE kind = 'answer'")
    assert desk.rows("SELECT question_id, outcome, reply_id FROM first_reads") == [(q.id, "escalated", reply[0][0])]
    with pytest.raises(fr.CoordError, match=f"question #{q.n} was escalated to the operator"):
        fr.answer(desk.store, q.id, option="a", token=tok)
    with pytest.raises(fr.CoordError, match=f"question #{q.n} was escalated to the operator"):
        fr.escalate(desk.store, q.id, "again", token=tok)
    assert desk.rows("SELECT COUNT(*) FROM replies WHERE kind = 'know'") == [(0,)]
    assert desk.rows("SELECT COUNT(*) FROM replies WHERE kind = 'answer'") == [(1,)]


@pytest.mark.parametrize("claude", ["unavailable", "unreadable"])
def test_a_watch_pass_releases_a_due_ping_whatever_claude_reads(desk, monkeypatch, claude):
    """L3 r3 (codex MED): ``watch_once`` released held pings only at its bottom, after the returns for no ``claude``
    on PATH and an unreadable session list, so in a Codex-only install a due ping stayed silent (red on 3540ae0c)."""
    from levain.coordinator import codex_launch
    from levain.coordinator import launch as cl

    desk.coordinator()
    q, _ = desk.ask()
    desk.later(minutes=10)
    monkeypatch.setattr(codex_launch, "watch_once", lambda store: [])
    monkeypatch.setattr(cl, "_harness", lambda: (None, {}) if claude == "unavailable" else ("/x/claude", {}))
    monkeypatch.setattr(cl, "_observed", lambda exe, env, cwd: None)
    lines = cl.watch_once(desk.store)
    assert f"question #{q.n} (unread): the operator was pinged" in lines
    assert len(desk.sent) == 1
