"""A1c1 part 2 (autogrant DESIGN r6.6 / rev 7 A1c1 row): the run journal's not-started transition (B1-1, r5.1 + r6.2)
and the hold decision's generation on a ``levain-autonomic/3`` store (O-a1c1d-1)."""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from levain.autonomic.hands_exec import NotStarted
from levain.autonomic.journal import (AUTHORIZED, EffectStatus, JournalConflictError, JournalCorruptError, RunJournal,
                                      hold_id_for)

FENCE = {"fence": "f", "fence_now": lambda conn: "f", "authorize": lambda hold, fence: AUTHORIZED}


@pytest.fixture
def j(tmp_path):
    return RunJournal(tmp_path / "journal")


def _effects(j):
    with j._read() as c:
        return c.execute("SELECT effect_id, state FROM effects").fetchall()


# ---- B1-1: EffectNotStarted is caught first --------------------------------------------------------------------------
def test_a_not_started_effect_is_held_its_row_gone_and_it_runs_on_the_next_delivery(j):
    j.start("r1", binding_id="b")

    def busy():
        raise NotStarted("hands_busy:live")   # the real hands executor's non-start (a subclass of EffectNotStarted)

    out = j.effect("r1", "link-0", digest="d", **FENCE, fn=busy)
    assert (out.status, out.why, out.hold_id) == (EffectStatus.HELD, "not_started:hands_busy:live", None)
    assert _effects(j) == [] and j.poisoned() == [] and j.peek("r1", "link-0") is None
    assert not j._lease_held("r1", "link-0")
    again = j.effect("r1", "link-0", digest="d", **FENCE, fn=lambda: "sent")
    assert (again.status, again.result) == (EffectStatus.DONE, "sent")


def test_a_not_started_approved_effect_keeps_its_approval(j):
    j.start("r1", binding_id="b")
    held = j.effect("r1", "link-0", digest="d", **FENCE, fn=lambda: "x", needs_decision=True)
    assert j.decide(held.hold_id, approve=True, digest="d", by="on-loop").ok

    def busy():
        raise NotStarted("hands_busy:cannot_tell")

    out = j.effect("r1", "link-0", digest="d", **FENCE, fn=busy, needs_decision=True)
    assert (out.status, out.why, out.hold_id) == (EffectStatus.HELD, "not_started:hands_busy:cannot_tell",
                                                  hold_id_for("r1", "link-0"))
    assert [h["hold_id"] for h in j.approved_unrun()] == [hold_id_for("r1", "link-0")]
    assert j.effect("r1", "link-0", digest="d", **FENCE, fn=lambda: "sent", needs_decision=True).status \
        is EffectStatus.DONE


def _flaky_write(j, monkeypatch, failures: int, exc=JournalCorruptError):
    """``j._write`` stood in: once ``fn`` armed it, the next ``failures`` write transactions raise ``exc``."""
    real = j._write
    state = {"armed": False, "left": failures, "attempts": 0}

    @contextmanager
    def write():
        if state["armed"]:
            state["attempts"] += 1
            if state["left"] > 0:
                state["left"] -= 1
                raise exc("database is locked")
        with real() as conn:
            yield conn

    monkeypatch.setattr(j, "_write", write)

    def fn():
        state["armed"] = True
        raise NotStarted("hands_busy:live")

    return state, fn


def test_the_not_started_write_retries_a_busy_store_then_holds(j, monkeypatch):
    j.start("r1", binding_id="b")
    state, fn = _flaky_write(j, monkeypatch, failures=1)
    out = j.effect("r1", "link-0", digest="d", **FENCE, fn=fn)
    assert (out.status, out.why, state["attempts"]) == (EffectStatus.HELD, "not_started:hands_busy:live", 2)
    assert _effects(j) == []


def test_the_not_started_write_is_bounded_then_fails_closed(j, monkeypatch):
    # r6.2 (a): _NOT_STARTED_TRIES attempts, then the raise; the row stays intent, the lease drops: POISONED
    j.start("r1", binding_id="b")
    state, fn = _flaky_write(j, monkeypatch, failures=99)
    with pytest.raises(JournalCorruptError):
        j.effect("r1", "link-0", digest="d", **FENCE, fn=fn)
    assert state["attempts"] == 3
    monkeypatch.undo()
    assert _effects(j) == [("link-0", "intent")] and j.poisoned() == [("r1", "link-0")]


def test_a_conflict_in_the_not_started_write_is_not_retried(j, monkeypatch):
    j.start("r1", binding_id="b")
    state, fn = _flaky_write(j, monkeypatch, failures=99, exc=JournalConflictError)
    with pytest.raises(JournalConflictError):
        j.effect("r1", "link-0", digest="d", **FENCE, fn=fn)
    assert state["attempts"] == 1


# ---- B1-3: every other BaseException is an unknown outcome, recorded, then raised -----------------------------------
@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit, TimeoutError])
def test_an_interrupted_effect_is_recorded_unknown_and_raised(j, exc):
    j.start("r1", binding_id="b")

    def fn():
        raise exc()

    with pytest.raises(exc):
        j.effect("r1", "link-0", digest="d", **FENCE, fn=fn)
    assert _effects(j) == [("link-0", "unknown")] and j.poisoned() == [("r1", "link-0")]
    assert not j._lease_held("r1", "link-0")


# ---- O-a1c1d-3: a record whose bookkeeping does not load is never made live -------------------------------------------
def test_a_hand_corrupted_record_is_refused_by_every_way_back_to_active(tmp_path, capsys):
    import json
    import sqlite3

    import levain.cli as cli
    from levain.autonomic import BindingStatus, BindingStore, Posture
    from levain.autonomic import db as vdb
    from tests.test_binding_pause_cli import _binding

    d = tmp_path / "store"
    st = BindingStore(d)
    b = _binding(Posture.ON_LOOP)
    st.add(b)
    c = sqlite3.connect(d / vdb.DB_NAME, isolation_level=None)
    rec = json.loads(c.execute("SELECT record FROM bindings").fetchone()[0])
    rec["graduation"]["fire_count"] = "x"
    c.execute("UPDATE bindings SET record = ? WHERE binding_id = ?", (json.dumps(rec, sort_keys=True), b.binding_id))
    status = lambda: json.loads(c.execute("SELECT record FROM bindings").fetchone()[0])["status"]  # noqa: E731
    assert cli.main(["binding", "resume", b.binding_id, "--store-dir", str(d)]) == 1   # ratify: _load refuses it
    assert "malformed" in capsys.readouterr().err
    with pytest.raises(ValueError, match="does not load"):
        st.set_status(b.binding_id, BindingStatus.ACTIVE)
    assert status() == "paused"
    assert st.set_status(b.binding_id, BindingStatus.PAUSED) and st.pause(b.binding_id) == 2   # stopping still works
    c.close()
