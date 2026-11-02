"""levain.agents.accounting: the launch ceiling under contention, a reservation open at a crash, a breach, StopResult."""
from __future__ import annotations

import dataclasses
import sqlite3
import threading

import pytest

from levain.agents.accounting import Executor, Limits, Refused, Reserved, StopResult, Usage, reserve, settle
from levain.agents.accounting import open_reservations
from levain.agents.store import open_agent_store

WINDOW = "d:2026-11-02"
LIMITS = Limits(run_ceiling=1, budget_window_usd_micros=10_000, scheduled_window_key=WINDOW,
                span_start="2026-11-02T00:00:00Z", span_end="2026-11-03T00:00:00Z")


class _Exec:
    def __init__(self, exposure: int) -> None:
        self.max_exposure_usd_micros = exposure


def _admit(conn, n: int, kind: str = "scheduled", window_key: str = WINDOW,
           admitted_at: str = "2026-11-02T09:00:00Z") -> dict:
    row = {"occurrence_id": f"oc-{n}", "agent_id": "ag-1", "kind": kind, "key": f"k{n}", "state": "admitted",
           "window_key": window_key, "spec_hash": "sh", "binding_id": "b", "run_id": f"run-{n}",
           "admitted_at": admitted_at}
    conn.execute("INSERT INTO agent_occurrences (occurrence_id,agent_id,kind,key,state,window_key,spec_hash,"
                 "binding_id,run_id,admitted_at) VALUES (:occurrence_id,:agent_id,:kind,:key,:state,:window_key,"
                 ":spec_hash,:binding_id,:run_id,:admitted_at)", row)
    return row


def _launch(conn, oc: str) -> None:
    conn.execute("UPDATE agent_occurrences SET state='launched', launch_epoch=0, link_id='al-x', link_incarnation='i',"
                 " launched_at='2026-11-02T10:00:00Z' WHERE occurrence_id=?", (oc,))


@pytest.fixture()
def store(tmp_path):
    return open_agent_store(tmp_path)


def test_a_ceiling_of_one_admits_exactly_one_of_two_contending_launches(store):
    with store.write() as conn:
        rows = [_admit(conn, 1), _admit(conn, 2)]
    start = threading.Barrier(2)
    results: dict[str, object] = {}

    def launch(row: dict) -> None:
        start.wait()
        with store.write() as conn:    # its own connection and its own BEGIN IMMEDIATE
            out = reserve(conn, row, _Exec(1000), limits=LIMITS)
            if isinstance(out, Reserved):
                _launch(conn, row["occurrence_id"])
            results[row["occurrence_id"]] = out

    threads = [threading.Thread(target=launch, args=(r,)) for r in rows]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(type(v).__name__ for v in results.values()) == ["Refused", "Reserved"]
    refused = next(v for v in results.values() if isinstance(v, Refused))
    assert (refused.reason, refused.state) == ("ceiling", "skipped-budget")
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_reservations").fetchone()[0] == 1
    # an executor stating no exposure is refused, and nothing is written for it
    with store.write() as conn:
        third = _admit(conn, 3)
        assert reserve(conn, third, _Exec(None), limits=Limits(5, 10**9, WINDOW, "a", "z")) == Refused("no_exposure")
        assert reserve(conn, third, _Exec(1000), limits=Limits(5, 1500, WINDOW, "a", "z")) == Refused("budget_window")


def test_an_open_reservation_left_by_a_crash_settles_whole_on_recovery(store):
    with store.write() as conn:
        row = _admit(conn, 1)
        assert reserve(conn, row, _Exec(4000), limits=LIMITS) == Reserved(4000)
        _launch(conn, "oc-1")
    # the process died here: a later connection finds it open and settles it with no readable usage
    with store.write() as conn:
        assert open_reservations(conn) == ["oc-1"]
        out = settle(conn, row, Usage(None, None, None, "recovery"))
        assert (out.debited_usd_micros, out.breach, out.basis) == (4000, False, "unreadable")
        assert settle(conn, row, Usage(1, None, None, "again")).basis == "already_settled"
        assert open_reservations(conn) == []
        assert conn.execute("SELECT debited_usd_micros FROM agent_reservations").fetchone()[0] == 4000


def test_an_actual_above_the_reservation_is_debited_in_full_and_pauses_the_agent(store):
    with store.write() as conn:
        conn.execute("INSERT INTO agents (agent_id,name,current_version,registration,operation,created_at)"
                     " VALUES ('ag-1','a',1,'registered','active','2026-11-02T08:00:00Z')")
        row = _admit(conn, 1)
        reserve(conn, row, _Exec(1000), limits=LIMITS)
        out = settle(conn, row, Usage(2500, 1, 1, "stub"))
        assert (out.debited_usd_micros, out.breach, out.basis) == (2500, True, "breach")
        assert conn.execute("SELECT operation FROM agents").fetchone()[0] == "paused"
        assert conn.execute("SELECT debited_usd_micros, settled_reason FROM agent_reservations").fetchone() == \
            (2500, "breach")


def test_reserve_refuses_a_connection_with_no_open_transaction_and_writes_nothing(store):
    with store.write() as conn:
        row = _admit(conn, 1)
    conn = sqlite3.connect(store.path, isolation_level=None)    # autocommit: each statement its own transaction
    try:
        with pytest.raises(RuntimeError, match="BEGIN IMMEDIATE"):
            reserve(conn, row, _Exec(1000), limits=LIMITS)
        assert conn.execute("SELECT COUNT(*) FROM agent_reservations").fetchone()[0] == 0
    finally:
        conn.close()


def test_amounts_past_the_sqlite_integer_range_refuse_or_saturate_and_the_window_still_refuses(store):
    limits = dataclasses.replace(LIMITS, run_ceiling=5)
    with store.write() as conn:
        conn.execute("INSERT INTO agents (agent_id,name,current_version,registration,operation,created_at)"
                     " VALUES ('ag-1','a',1,'registered','active','2026-11-02T08:00:00Z')")
        rows = [_admit(conn, 1), _admit(conn, 2)]
        assert reserve(conn, _admit(conn, 3), _Exec(2**63), limits=limits) == Refused("no_exposure")
        assert [reserve(conn, r, _Exec(1000), limits=limits) for r in rows] == [Reserved(1000)] * 2
        outs = [settle(conn, r, Usage(2**70, 1, 1, "stub")) for r in rows]    # two breaches past 2**63
        assert [(o.debited_usd_micros, o.breach) for o in outs] == [(2**63 - 1, True)] * 2
        assert conn.execute("SELECT operation FROM agents").fetchone()[0] == "paused"
        assert reserve(conn, _admit(conn, 4), _Exec(1), limits=limits) == Refused("budget_window")


def test_a_manual_run_stamped_with_an_offset_counts_at_its_instant(store):
    # 20:00-05:00 on the 2nd is 01:00Z on the 3rd: inside the 3rd's span although its text sorts before it
    day3 = Limits(run_ceiling=5, budget_window_usd_micros=10_000, scheduled_window_key=None,
                  span_start="2026-11-03T00:00:00Z", span_end="2026-11-04T00:00:00Z")
    with store.write() as conn:
        first = _admit(conn, 1, "manual", "m:2026-11-03", "2026-11-02T20:00:00-05:00")
        second = _admit(conn, 2, "manual", "m:2026-11-03", "2026-11-03T02:00:00Z")
        assert reserve(conn, first, _Exec(9000), limits=day3) == Reserved(9000)
        assert reserve(conn, second, _Exec(9000), limits=day3) == Refused("budget_window")


def test_stop_result_is_frozen_and_a_stub_that_cannot_observe_returns_terminated_false():
    class Blind:
        max_exposure_usd_micros = 1

        def usage(self) -> Usage:
            return Usage(None, None, None, "blind")

        def stop(self, deadline_s: float) -> StopResult:
            return StopResult(False, "cannot observe the process")

    result = Blind().stop(1.0)
    assert isinstance(Blind(), Executor) and result.terminated is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.terminated = True   # type: ignore[misc]
