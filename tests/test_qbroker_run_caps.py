"""P7-A3: a run's own caps at GATE (max_calls, deadline) and the read-only RUN-STATE verb."""
from __future__ import annotations

from datetime import timedelta

import pytest

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
