"""The run journal's four barrier properties, plus the unknown-outcome rule, each as a run.

Every test drives real effects (a counter of how many times ``fn`` ran) through a real journal
file, so a property that does not hold shows up as an effect that ran when it must not have.
The crash case runs in a separate process that dies between the intent and the result.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from levain.autonomic.journal import EffectStatus, JournalCorruptError, RunJournal


class Effect:
    """A side effect that counts its executions."""

    def __init__(self, value="sent"):
        self.calls = 0
        self.value = value

    def __call__(self):
        self.calls += 1
        return self.value


@pytest.fixture
def j(tmp_path):
    return RunJournal(tmp_path / "journal.jsonl")


# --- dedup-on-replay ---------------------------------------------------------------------

def test_dedup_on_replay_a_recorded_effect_never_runs_again(j):
    j.start("r1", binding_id="b", generation=1)
    send = Effect("msg-42")
    first = j.effect("r1", "send", digest="d", fn=send)
    again = j.effect("r1", "send", digest="d", fn=send)
    assert first.status is EffectStatus.DONE and first.result == "msg-42"
    assert again.status is EffectStatus.REPLAYED and again.result == "msg-42"
    assert send.calls == 1


def test_a_resumed_run_replays_done_effects_and_runs_only_the_rest(j, tmp_path):
    j.start("r1", binding_id="b", generation=1)
    send, pay = Effect(), Effect()
    j.effect("r1", "send", digest="d1", fn=send)
    # a new journal object on the same file = a restarted process resuming the run
    resumed = RunJournal(tmp_path / "journal.jsonl")
    resumed.start("r1", binding_id="b", generation=1)
    assert resumed.effect("r1", "send", digest="d1", fn=send).status is EffectStatus.REPLAYED
    assert resumed.effect("r1", "pay", digest="d2", fn=pay).status is EffectStatus.DONE
    assert (send.calls, pay.calls) == (1, 1)


# --- hold-until-decided --------------------------------------------------------------------

def test_hold_suspends_before_the_effect_and_resumes_after_approval(j):
    j.start("r1", binding_id="b", generation=1)
    pay = Effect()
    held = j.effect("r1", "pay", digest="pay-100", fn=pay, needs_decision=True)
    assert held.status is EffectStatus.HELD and pay.calls == 0
    assert j.decide(held.hold_id, approve=True, digest="pay-100").ok
    done = j.effect("r1", "pay", digest="pay-100", fn=pay, needs_decision=True)
    assert done.status is EffectStatus.DONE and pay.calls == 1


def test_hold_until_decided_stops_sibling_runs_of_the_binding(j):
    j.start("r1", binding_id="b", generation=1)
    j.start("r2", binding_id="b", generation=1)
    j.start("other", binding_id="c", generation=1)
    sibling, unrelated = Effect(), Effect()
    held = j.effect("r1", "pay", digest="p", fn=Effect(), needs_decision=True)
    assert j.effect("r2", "send", digest="s", fn=sibling).status is EffectStatus.HELD
    assert sibling.calls == 0                      # the sibling leak is closed
    assert j.effect("other", "send", digest="s", fn=unrelated).status is EffectStatus.DONE
    j.decide(held.hold_id, approve=True, digest="p")
    assert j.effect("r2", "send", digest="s", fn=sibling).status is EffectStatus.DONE


def test_a_decision_must_echo_the_digest_it_was_shown(j):
    j.start("r1", binding_id="b", generation=1)
    held = j.effect("r1", "pay", digest="pay-100", fn=Effect(), needs_decision=True)
    assert j.decide(held.hold_id, approve=True, digest="pay-999").reason == "digest_mismatch"
    assert j.decide(held.hold_id, approve=True, digest="pay-100").ok
    assert j.decide(held.hold_id, approve=True, digest="pay-100").reason == "already_decided"


def test_approved_bytes_that_change_before_running_cancel_the_run(j):
    j.start("r1", binding_id="b", generation=1)
    pay = Effect()
    held = j.effect("r1", "pay", digest="pay-100", fn=pay, needs_decision=True)
    j.decide(held.hold_id, approve=True, digest="pay-100")
    out = j.effect("r1", "pay", digest="pay-999", fn=pay, needs_decision=True)
    assert out.status is EffectStatus.CANCELLED and pay.calls == 0


# --- reject-cancels ------------------------------------------------------------------------

def test_reject_cancels_the_run(j):
    j.start("r1", binding_id="b", generation=1)
    pay, later = Effect(), Effect()
    held = j.effect("r1", "pay", digest="p", fn=pay, needs_decision=True)
    assert j.decide(held.hold_id, approve=False, digest="p").reason == "rejected"
    assert j.effect("r1", "pay", digest="p", fn=pay, needs_decision=True).status is EffectStatus.CANCELLED
    assert j.effect("r1", "later", digest="l", fn=later).status is EffectStatus.CANCELLED
    assert (pay.calls, later.calls) == (0, 0)


# --- fence-on-cancel -----------------------------------------------------------------------

def test_fence_stops_runs_admitted_under_an_older_generation(j):
    j.start("old", binding_id="b", generation=7)
    first, second, fresh = Effect(), Effect(), Effect()
    assert j.effect("old", "one", digest="1", fn=first).status is EffectStatus.DONE
    j.fence("b", generation=8)                     # a demotion between two effects of a run
    assert j.effect("old", "two", digest="2", fn=second).status is EffectStatus.FENCED
    j.start("new", binding_id="b", generation=8)   # re-admitted under the new generation
    assert j.effect("new", "two", digest="2", fn=fresh).status is EffectStatus.DONE
    assert (first.calls, second.calls, fresh.calls) == (1, 0, 1)


# --- unknown outcome -----------------------------------------------------------------------

def test_an_effect_that_raises_is_poisoned_never_retried(j):
    j.start("r1", binding_id="b", generation=1)

    def flaky():
        flaky.calls += 1
        raise TimeoutError("server may have accepted the message")

    flaky.calls = 0
    with pytest.raises(TimeoutError):
        j.effect("r1", "send", digest="d", fn=flaky)
    assert j.effect("r1", "send", digest="d", fn=flaky).status is EffectStatus.POISONED
    assert flaky.calls == 1
    assert ("r1", "send") in j.poisoned()


def test_a_process_that_dies_mid_effect_leaves_it_poisoned(tmp_path):
    path = tmp_path / "journal.jsonl"
    marker = tmp_path / "effect_ran"
    child = textwrap.dedent(f"""
        import os
        from levain.autonomic.journal import RunJournal
        j = RunJournal({str(path)!r})
        j.start("r1", binding_id="b", generation=1)
        def send():
            open({str(marker)!r}, "w").write("sent")
            os._exit(9)                      # dies after the effect, before the result is recorded
        j.effect("r1", "send", digest="d", fn=send)
    """)
    proc = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True)
    assert proc.returncode == 9, proc.stderr
    assert marker.exists()                       # the world changed
    j = RunJournal(path)
    retry = Effect()
    assert j.effect("r1", "send", digest="d", fn=retry).status is EffectStatus.POISONED
    assert retry.calls == 0                      # and nothing sends it a second time
    assert j.poisoned() == [("r1", "send")]


def test_a_live_owner_is_in_flight_not_poisoned(j):
    # the owner holds the effect's lease across its call; a reader sees IN_FLIGHT, never POISONED,
    # whether the owner is another process, a recycled pid, or another thread of this one
    j.start("r1", binding_id="b", generation=1)
    seen = []

    def slow_send():
        seen.append(j.effect("r1", "send", digest="d", fn=Effect()).status)   # a second caller, mid-call
        seen.append(j.poisoned())
        return "sent"

    assert j.effect("r1", "send", digest="d", fn=slow_send).status is EffectStatus.DONE
    assert seen == [EffectStatus.IN_FLIGHT, []]
    assert j.effect("r1", "send", digest="d", fn=Effect()).status is EffectStatus.REPLAYED


def test_a_recycled_pid_does_not_make_a_dead_owner_look_alive(j):
    # an intent whose owner is gone (no lease held) is poisoned even if its recorded pid is alive
    j.start("r1", binding_id="b", generation=1)
    with j.db.write() as conn:
        conn.execute("INSERT INTO effects (run_id, effect_id, digest, pid, state) VALUES ('r1', 'send', 'd', ?, 'intent')",
                     (os.getpid(),))
    assert j.effect("r1", "send", digest="d", fn=Effect()).status is EffectStatus.POISONED
    assert j.poisoned() == [("r1", "send")]


# --- storage -------------------------------------------------------------------------------

def test_a_damaged_store_fails_closed(j):
    # an unreadable store cannot prove an effect has not run: nothing proceeds
    j.start("r1", binding_id="b", generation=1)
    assert j.effect("r1", "send", digest="d", fn=Effect()).status is EffectStatus.DONE
    for side in j.directory.iterdir():
        if side.name.startswith("autonomic.db"):
            side.write_bytes(b"not a database at all" * 100)
    with pytest.raises(JournalCorruptError):
        RunJournal(j.directory).effect("r1", "other", digest="d", fn=Effect())


# --- the API the fire path uses (2026-10-07 wiring) ------------------------------------------

def test_an_effect_with_its_own_approved_hold_runs_while_a_sibling_decision_is_open(j):
    # a person approved exactly these bytes; only UNDECIDED effects wait on a sibling's open decision
    j.start("r1", binding_id="b", generation=1)
    j.start("r2", binding_id="b", generation=1)
    h1 = j.hold("r1", "pay", digest="p1", pending={})
    h2 = j.hold("r2", "pay", digest="p2", pending={})
    assert j.decide(h2.hold_id, approve=True, digest="p2").ok
    pay = Effect()
    assert j.effect("r2", "pay", digest="p2", fn=pay, needs_decision=True).status is EffectStatus.DONE
    assert j.effect("r2", "after", digest="a", fn=Effect()).status is EffectStatus.HELD   # h1 still open
    assert pay.calls == 1 and h1.status is EffectStatus.HELD


def test_proposing_the_same_effect_again_finds_the_same_decision(j):
    j.start("r1", binding_id="b", generation=0)
    first = j.hold("r1", "send", digest="d", pending={})
    again = j.hold("r1", "send", digest="d", pending={})
    assert first.new_hold and not again.new_hold and first.hold_id == again.hold_id
    assert len(j.open_holds()) == 1
    j.decide(first.hold_id, approve=True, digest="d", by="human")
    approved = j.hold("r1", "send", digest="d", pending={})
    assert approved.status is EffectStatus.APPROVED and approved.decided_by == "human"


def test_proposing_different_bytes_under_an_open_decision_cancels_the_run(j):
    j.start("r1", binding_id="b", generation=0)
    j.hold("r1", "send", digest="d1", pending={})
    assert j.hold("r1", "send", digest="d2", pending={}).status is EffectStatus.CANCELLED
    assert j.effect("r1", "send", digest="d1", fn=Effect()).status is EffectStatus.CANCELLED


def test_a_fence_bumps_the_generation_and_stops_older_runs(j):
    assert j.generation("b") == 0
    j.start("old", binding_id="b", generation=0)
    assert j.fence("b") == 1
    j.start("new", binding_id="b", generation=j.generation("b"))
    assert j.effect("old", "x", digest="d", fn=Effect()).status is EffectStatus.FENCED
    assert j.effect("new", "x", digest="d", fn=Effect()).status is EffectStatus.DONE


def test_peek_runs_nothing_and_reports_the_barrier(j):
    j.start("r1", binding_id="b", generation=0)
    assert j.peek("r1", "x") is None
    j.effect("r1", "x", digest="d", fn=Effect("v"))
    seen = j.peek("r1", "x")
    assert seen.status is EffectStatus.REPLAYED and seen.result == "v" and seen.receipt_id is None
    j.note_receipt("r1", "x", "rcpt-1")
    assert j.peek("r1", "x").receipt_id == "rcpt-1"
    j.cancel("r1", reason="killed")
    assert j.peek("r1", "y").status is EffectStatus.CANCELLED
    with pytest.raises(KeyError):
        j.peek("never", "x")


def test_a_run_id_is_a_content_address_over_the_binding_and_the_event():
    from levain.autonomic.journal import run_id_for
    e = {"type": "email", "id": "1", "fields": {"a": 1}}
    assert run_id_for("b", e) == run_id_for("b", dict(e))
    assert run_id_for("b", e) != run_id_for("c", e)
    assert run_id_for("b", e) != run_id_for("b", dict(e, id="2"))
    for bad in ({"x": float("nan")}, {1: "x"}, {"t": (1, 2)}, {"o": object()}):
        with pytest.raises(ValueError):
            run_id_for("b", bad)


def test_a_store_of_another_format_is_refused(j):
    from levain.autonomic.db import StoreFormatError
    j.start("r1", binding_id="b", generation=1)
    with j.db.write() as conn:
        conn.execute("UPDATE meta SET value = 'something-else/9' WHERE key = 'format'")
    with pytest.raises(StoreFormatError):
        RunJournal(j.directory).start("r2", binding_id="b", generation=1)


def test_a_rejection_cancels_its_run_in_the_same_transaction(j):
    j.start("r1", binding_id="b", generation=0)
    h = j.hold("r1", "send", digest="d", pending={"pending_id": "p1"})
    assert j.decide(h.hold_id, approve=False, digest="d").ok
    assert j.effect("r1", "later", digest="x", fn=Effect()).status is EffectStatus.CANCELLED
    assert j.find_pending("p1")["decided"] is False
    # the update and the cancel commit together: a failure after the update leaves neither
    j.start("r2", binding_id="b", generation=0)
    h2 = j.hold("r2", "send", digest="d", pending={"pending_id": "p2"})
    real = j._hold_row
    calls = []

    def fail_after_update(conn, hold_id):
        if calls:
            raise RuntimeError("stopped mid-decision")
        calls.append(1)
        row = real(conn, hold_id)
        conn.execute("CREATE TEMP TRIGGER boom AFTER UPDATE ON holds BEGIN SELECT RAISE(ABORT, 'stop'); END")
        return row

    j._hold_row = fail_after_update
    with pytest.raises(Exception):
        j.decide(h2.hold_id, approve=False, digest="d")
    j._hold_row = real
    assert j.find_pending("p2")["decided"] is None
    assert j.effect("r2", "later", digest="x", fn=Effect()).status is EffectStatus.HELD
