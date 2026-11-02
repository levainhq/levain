"""P7-A3: a run's own caps at GATE (max_calls, deadline) and the read-only RUN-STATE verb."""
from __future__ import annotations

from datetime import timedelta

import pytest

from levain.contracts.canon import canonical_bytes
from levain.qbroker import broker as qbroker, operator
from levain.qbroker.broker import Resolved, resolved_from
import tests.test_qbroker as base
from tests.test_qbroker import NOW, RUN, make_broker
from tests.test_qbroker_gate import gate
from tests.test_qbroker_limits import BIG, digests, latches

L, clock, keys, resolver = base.L, base.clock, base.keys, base.resolver


def capped(resolver, **caps):
    run = dict(RUN) | caps
    resolver.add(2, "cap-two", Resolved(session="sess-2", kind="dispatch", run=run))
    return run


def go(b, d, cap="cap-two"):
    return gate(b, slot=2, cap=cap, input_digest=next(d))


def test_the_run_calls_cap_denies_past_max_calls_and_latches(L, resolver, clock):
    capped(resolver, max_calls=3)
    b = make_broker(L, resolver, clock, limits=lambda a, s: dict(BIG))
    d = digests()
    got = [go(b, d) for _ in range(6)]
    assert [g["gate"] for g in got] == ["go"] * 3 + ["over-limit"] * 3
    assert {g["kind"] for g in got[3:]} == {"run-calls"}
    assert ("calls", 4.0) in latches(L)


def test_the_count_survives_a_broker_restart(L, resolver, clock):
    capped(resolver, max_calls=3)
    d = digests()
    b = make_broker(L, resolver, clock, limits=lambda a, s: dict(BIG))
    assert [go(b, d)["gate"] for _ in range(2)] == ["go", "go"]
    again = make_broker(L, resolver, clock, limits=lambda a, s: dict(BIG))
    assert [go(again, d)["gate"] for _ in range(2)] == ["go", "over-limit"]


def test_the_cap_is_held_with_no_global_limits_configured(L, resolver, clock):
    capped(resolver, max_calls=1)
    b = make_broker(L, resolver, clock)       # limits=None: Q13 counts nothing, the run's cap still binds
    d = digests()
    assert [go(b, d)["gate"] for _ in range(2)] == ["go", "over-limit"]


def test_the_counter_key_is_the_run_not_the_session_or_the_cap(L, resolver, clock):
    capped(resolver, max_calls=2)
    resolver.add(2, "cap-two-b", Resolved(session="sess-2b", kind="dispatch", run=dict(RUN) | {"max_calls": 2}))
    b = make_broker(L, resolver, clock, limits=lambda a, s: dict(BIG))
    d = digests()
    assert go(b, d)["gate"] == "go" and go(b, d, "cap-two-b")["gate"] == "go"
    assert go(b, d)["gate"] == "over-limit"            # the same run id under another session shares the count


def test_either_crossing_denies_and_the_global_kind_is_unchanged(L, resolver, clock):
    capped(resolver, max_calls=50)
    b = make_broker(L, resolver, clock, limits=lambda a, s: BIG | {"calls": 2})
    d = digests()
    got = [go(b, d) for _ in range(3)]
    assert [g["gate"] for g in got] == ["go", "go", "over-limit"] and got[2]["kind"] == "calls"


def test_past_the_deadline_is_over_limit_and_counts_nothing(L, resolver, clock):
    capped(resolver, deadline=int(NOW.timestamp()) + 60)
    b = make_broker(L, resolver, clock, limits=lambda a, s: dict(BIG))
    d = digests()
    assert go(b, d)["gate"] == "go"
    clock["now"] = NOW + timedelta(seconds=61)
    got = go(b, d)
    assert got["gate"] == "over-limit" and got["kind"] == "run-deadline"
    assert operator.run_state(L / "queue", "run-1", now=int(NOW.timestamp()) + 61) == {
        "calls": 1, "cap": None, "tripped": True, "kind": "run-deadline", "known": True}


def test_a_run_without_caps_and_a_session_without_a_run_are_untouched(L, resolver, clock):
    b = make_broker(L, resolver, clock, limits=lambda a, s: dict(BIG))
    d = digests()
    assert go(b, d)["gate"] == "go"                                       # RUN carries no caps
    assert gate(b, slot=1, cap="cap-a", input_digest=next(d))["gate"] == "go"
    assert operator.run_state(L / "queue", "run-1")["known"] is False


@pytest.mark.parametrize("bad", [{"max_calls": 0}, {"max_calls": True}, {"max_calls": 1.5}, {"deadline": 0},
                                 {"deadline": 1.5e9}, {"deadline": "soon"}])
def test_a_malformed_cap_is_a_malformed_resolve(bad):
    answer = {"session": "s", "kind": "dispatch", "run": dict(RUN) | bad}
    assert resolved_from(answer) is None
    assert resolved_from({**answer, "run": dict(RUN) | {"max_calls": 5, "deadline": 10**9}}) is not None


def test_run_state_reads_calls_cap_tripped_and_fails_closed(L, resolver, clock):
    capped(resolver, max_calls=2)
    b = make_broker(L, resolver, clock, limits=lambda a, s: dict(BIG))
    d = digests()
    go(b, d)
    q = L / "queue"
    assert operator.run_state(q, "run-1") == {"calls": 1, "cap": 2, "tripped": False, "kind": None, "known": True}
    go(b, d), go(b, d)
    assert operator.run_state(q, "run-1") == {"calls": 2, "cap": 2, "tripped": True, "kind": "run-calls",
                                              "known": True}
    qbroker.run_state_path(q / "requests", "run-1").write_bytes(b"not json")
    with pytest.raises(operator.OperatorError):
        operator.run_state(q, "run-1")
    with pytest.raises(operator.OperatorError):
        operator.run_state(q, "")


# --- L3 r1 (P7-A3) ---------------------------------------------------------------------------------------------------

def state_path(L):
    return qbroker.run_state_path(L / "queue" / "requests", "run-1")


def write_state(L, **over):
    doc = {"format": qbroker.RUN_STATE_FORMAT, "run_id": "run-1", "calls": 1, "cap": 2, "deadline": None,
           "tripped": None} | over
    state_path(L).write_bytes(canonical_bytes(doc))


def test_a_deadline_trip_is_terminal_when_the_clock_steps_back(L, resolver, clock):
    # codex MED (L3 r1): the trip was re-judged on the wall clock at every call, so a clock stepped back un-tripped it
    capped(resolver, deadline=int(NOW.timestamp()) + 60)
    b = make_broker(L, resolver, clock, limits=lambda a, s: dict(BIG))
    d = digests()
    assert go(b, d)["gate"] == "go"
    clock["now"] = NOW + timedelta(seconds=61)
    assert go(b, d)["kind"] == "run-deadline"
    clock["now"] = NOW + timedelta(seconds=10)
    got = go(b, d)
    assert got["gate"] == "over-limit" and got["kind"] == "run-deadline"
    state_path(L).unlink()                               # the latch is the store's, not the published view's
    got = go(make_broker(L, resolver, clock, limits=lambda a, s: dict(BIG)), d)
    assert got["gate"] == "over-limit" and got["kind"] == "run-deadline"
    # CONTROL derives deadline at each ATTACH: a later session of the run carries a later one, and the run stays past
    resolver.add(2, "cap-two-b", Resolved(session="sess-2b", kind="dispatch",
                                          run=dict(RUN) | {"deadline": int(NOW.timestamp()) + 3600}))
    assert go(make_broker(L, resolver, clock, limits=lambda a, s: dict(BIG)), d, "cap-two-b")["kind"] == "run-deadline"
    assert operator.run_state(L / "queue", "run-1", now=int(NOW.timestamp())) == {
        "calls": 1, "cap": None, "tripped": True, "kind": "run-deadline", "known": True}


@pytest.mark.parametrize("stale", ["deleted", "behind"])
def test_the_deadline_state_reports_the_counter_not_the_published_file(L, resolver, clock, stale):
    # codex MED (L3 r1): a crash between the count's commit and the publish left the file behind; the deadline path
    # republished the file's count forever
    capped(resolver, deadline=int(NOW.timestamp()) + 60)
    b = make_broker(L, resolver, clock, limits=lambda a, s: dict(BIG))
    d = digests()
    assert [go(b, d)["gate"] for _ in range(2)] == ["go", "go"]
    if stale == "deleted":
        state_path(L).unlink()
    else:
        write_state(L, calls=1, cap=None, deadline=int(NOW.timestamp()) + 60)
    clock["now"] = NOW + timedelta(seconds=61)
    assert go(make_broker(L, resolver, clock, limits=lambda a, s: dict(BIG)), d)["kind"] == "run-deadline"
    assert operator.run_state(L / "queue", "run-1")["calls"] == 2


def test_a_run_naming_two_agents_counts_both_against_one_cap(L, resolver, clock):
    # complement MED (L3 r1): the run's row was keyed by the first call's agent, and a second agent under the same
    # run_id was answered other_agent -> limits-failed for good
    capped(resolver, max_calls=2)
    resolver.add(2, "cap-two-b", Resolved(session="sess-2b", kind="dispatch", run=dict(RUN) | {"max_calls": 2}))
    b = make_broker(L, resolver, clock, limits=lambda a, s: dict(BIG))
    d = digests()
    assert go(b, d)["gate"] == "go"
    assert gate(b, slot=2, cap="cap-two-b", agent="codex", input_digest=next(d))["gate"] == "go"
    got = gate(b, slot=2, cap="cap-two-b", agent="codex", input_digest=next(d))
    assert got["gate"] == "over-limit" and got["kind"] == "run-calls"
    assert operator.run_state(L / "queue", "run-1")["calls"] == 2


def test_run_state_on_a_queue_that_cannot_be_opened_is_an_operator_error(tmp_path):
    with pytest.raises(operator.OperatorError):
        operator.run_state(tmp_path / "nowhere", "run-1")
    (tmp_path / "q" / "requests").mkdir(parents=True)
    (tmp_path / "q" / "answers").mkdir()
    with pytest.raises(operator.OperatorError):           # no queue id: the broker never ran here
        operator.run_state(tmp_path / "q", "run-1")


@pytest.mark.parametrize("bad", [{"cap": 0}, {"cap": -1}, {"cap": True}, {"deadline": 0}, {"deadline": -5},
                                 {"deadline": 1.5}, {"calls": -1}, {"calls": True}, {"tripped": "calls"},
                                 {"tripped": True}, {"run_id": "run-2"}, {"cap": None},
                                 {"tripped": "run-calls", "cap": None, "deadline": 10**9},
                                 {"tripped": "run-deadline", "deadline": None}])
def test_run_state_refuses_an_impossible_state(L, resolver, clock, bad):
    capped(resolver, max_calls=2)
    go(make_broker(L, resolver, clock, limits=lambda a, s: dict(BIG)), digests())      # makes the queue id
    write_state(L, **bad)
    with pytest.raises(operator.OperatorError):
        operator.run_state(L / "queue", "run-1")
