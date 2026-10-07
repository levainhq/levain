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
    j.start("r1", binding_id="b", generation=1)
    # an intent recorded by another, still-running process (our parent stands in for it)
    j._append({"t": "intent", "run_id": "r1", "effect_id": "send", "digest": "d", "pid": os.getppid()})
    assert j.effect("r1", "send", digest="d", fn=Effect()).status is EffectStatus.IN_FLIGHT
    assert j.poisoned() == []


# --- storage -------------------------------------------------------------------------------

def test_a_torn_final_line_is_ignored_and_a_corrupt_middle_fails_closed(j, tmp_path):
    j.start("r1", binding_id="b", generation=1)
    path = tmp_path / "journal.jsonl"
    with open(path, "a") as f:
        f.write('{"t":"result","run_id"')         # a crash mid-append
    assert j.effect("r1", "send", digest="d", fn=Effect()).status is EffectStatus.DONE
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], "not json"] + lines[1:]) + "\n")
    with pytest.raises(JournalCorruptError):
        j.effect("r1", "other", digest="d", fn=Effect())
