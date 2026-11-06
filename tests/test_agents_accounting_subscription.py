"""levain.agents.accounting on a subscription route (P7-A §2.2, §4): the exposure statement, a zero-dollar reservation,
the action usage and the breach rule. The api_key route's own tests are test_agents_accounting.py, untouched."""
from __future__ import annotations

import pytest

from levain.agents.accounting import (FRONT_READ_BUFFER, ActionExposure, Limits, Refused, Reserved, Usage, reserve,
                                      settle)
from levain.agents.store import open_agent_store
from tests.test_agents_accounting import WINDOW, _admit, _Exec, _launch

SPAN = dict(scheduled_window_key=WINDOW, span_start="2026-11-02T00:00:00Z", span_end="2026-11-03T00:00:00Z")
SUB = Limits(run_ceiling=2, budget_window_usd_micros=0, billing="subscription", budget_run_calls=20,
             budget_run_tunnel_bytes=5_000_000, **SPAN)
EXP = ActionExposure(actions=20, unit="tool_calls", seconds=3600, tunnel_bytes=5_000_000)


class _SubExec:
    def __init__(self, exposure=0, action=EXP) -> None:
        self.max_exposure_usd_micros = exposure
        self.action_exposure = action


@pytest.fixture()
def store(tmp_path):
    return open_agent_store(tmp_path)


def _reserve(store, n=1, executor=None, limits=SUB):
    with store.write() as conn:
        row = _admit(conn, n)
        out = reserve(conn, row, executor or _SubExec(), limits=limits)
        if isinstance(out, Reserved):
            _launch(conn, row["occurrence_id"])
    return row, out


def _row(store, oc="oc-1"):
    with store.read() as conn:
        conn.row_factory = __import__("sqlite3").Row
        return dict(conn.execute("SELECT * FROM agent_reservations WHERE occurrence_id = ?", (oc,)).fetchone())


def test_a_subscription_launch_reserves_zero_dollars_and_records_its_caps(store):
    _, out = _reserve(store)
    assert out == Reserved(0)
    r = _row(store)
    assert (r["reserved_usd_micros"], r["billing"], r["cap_actions"], r["cap_tunnel_bytes"], r["cap_seconds"],
            r["state"]) == (0, "subscription", 20, 5_000_000, 3600, "open")


@pytest.mark.parametrize("executor", [
    _SubExec(action=None),                                                  # no ActionExposure
    _SubExec(exposure=1),                                                   # a non-zero dollar exposure
    _SubExec(exposure=None),                                                # no dollar statement at all
    _SubExec(exposure=False),                                               # a bool is no integer
    _SubExec(action=ActionExposure(0, "tool_calls", 3600, 5_000_000)),      # actions not positive
    _SubExec(action=ActionExposure(21, "tool_calls", 3600, 5_000_000)),     # above the link's per-run cap
    _SubExec(action=ActionExposure(True, "tool_calls", 3600, 5_000_000)),
    _SubExec(action=ActionExposure(20, "tool_calls", 0, 5_000_000)),        # no deadline
    _SubExec(action=ActionExposure(20, "tool_calls", 3600.0, 5_000_000)),
    _SubExec(action=ActionExposure(20, "tool_calls", 3600, 5_000_001)),     # bytes above the cap
    _SubExec(action=ActionExposure(20, "tool_calls", 3600, 0)),
    _SubExec(action=ActionExposure(20, "minutes", 3600, 5_000_000)),        # unknown unit
    _Exec(0),                                                               # an api_key executor on a subscription link
])
def test_a_subscription_launch_refuses_a_bad_statement(store, executor):
    _, out = _reserve(store, executor=executor)
    assert out == Refused("no_exposure")
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_reservations").fetchone()[0] == 0


def test_a_missing_tunnel_figure_is_allowed_for_an_executor_with_no_process_tool(store):
    _, out = _reserve(store, executor=_SubExec(action=ActionExposure(5, "turns", 600, None)))
    assert out == Reserved(0)
    r = _row(store)
    assert (r["cap_actions"], r["cap_tunnel_bytes"], r["cap_seconds"]) == (5, None, 600)


def test_a_subscription_link_with_no_action_cap_or_an_unknown_billing_refuses(store):
    zero = Limits(run_ceiling=2, budget_window_usd_micros=0, billing="subscription", **SPAN)
    assert _reserve(store, 1, limits=zero)[1] == Refused("no_exposure")
    odd = Limits(run_ceiling=2, budget_window_usd_micros=0, billing="oauth", budget_run_calls=20, **SPAN)
    assert _reserve(store, 2, limits=odd)[1] == Refused("no_exposure")


def test_run_ceiling_still_binds_and_the_dollar_window_does_not(store):
    assert _reserve(store, 1)[1] == Reserved(0)
    assert _reserve(store, 2)[1] == Reserved(0)    # a window of 0 dollars refuses nothing on this route
    assert _reserve(store, 3)[1] == Refused("ceiling")


def test_a_dollar_window_spent_by_earlier_api_key_runs_does_not_refuse_a_subscription_one(store):
    api = Limits(run_ceiling=5, budget_window_usd_micros=10_000, budget_run_usd_micros=10_000, **SPAN)
    with store.write() as conn:
        row = _admit(conn, 1)
        assert reserve(conn, row, _Exec(9_000), limits=api) == Reserved(9_000)
        _launch(conn, "oc-1")
    sub = Limits(run_ceiling=5, budget_window_usd_micros=0, billing="subscription", budget_run_calls=20,
                 budget_run_tunnel_bytes=5_000_000, **SPAN)
    assert _reserve(store, 2, limits=sub)[1] == Reserved(0)


def test_an_api_key_reservation_is_as_it_was_and_carries_no_action_columns(store):
    with store.write() as conn:
        row = _admit(conn, 1)
        assert reserve(conn, row, _Exec(1000), limits=Limits(5, 10**9, WINDOW, "a", "z", budget_run_usd_micros=10**9)) == Reserved(1000)
    r = _row(store)
    assert (r["reserved_usd_micros"], r["billing"], r["cap_actions"]) == (1000, None, None)
    with store.write() as conn:
        assert settle(conn, {"occurrence_id": "oc-1"}, Usage(400, None, None, "t")).debited_usd_micros == 400


def _settle(store, usage, oc="oc-1"):
    with store.write() as conn:
        return settle(conn, {"occurrence_id": oc}, usage)


def _operation(store):
    with store.read() as conn:
        row = conn.execute("SELECT operation FROM agents WHERE agent_id = 'ag-1'").fetchone()
        return None if row is None else row[0]


def _with_agent(store):
    with store.write() as conn:
        conn.execute("INSERT INTO agents VALUES ('ag-1','a',1,'registered','active',0,'2026-11-02T00:00:00Z')")


def test_a_run_within_its_caps_records_its_usage_and_debits_nothing(store):
    _with_agent(store)
    _reserve(store)
    out = _settle(store, Usage(usd_micros=123_456, input_tokens=None, output_tokens=None, source="t",
                               actions=20, tunnel_bytes=5_000_000 + FRONT_READ_BUFFER, seconds=1800))
    assert (out.debited_usd_micros, out.breach, out.basis) == (0, False, "actual")
    r = _row(store)
    assert (r["state"], r["debited_usd_micros"], r["used_actions"], r["used_tunnel_bytes"], r["used_seconds"]) \
        == ("settled", 0, 20, 5_000_000 + FRONT_READ_BUFFER, 1800)
    assert _operation(store) == "active"


@pytest.mark.parametrize("actions, tunnel", [(21, 100), (3, 5_000_000 + FRONT_READ_BUFFER + 1)])
def test_a_count_above_its_cap_is_a_breach_and_pauses_the_agent(store, actions, tunnel):
    _with_agent(store)
    _reserve(store)
    out = _settle(store, Usage(None, None, None, "t", actions=actions, tunnel_bytes=tunnel, seconds=10))
    assert (out.debited_usd_micros, out.breach, out.basis) == (0, True, "breach")
    r = _row(store)
    assert (r["used_actions"], r["used_tunnel_bytes"]) == (actions, tunnel)
    assert _operation(store) == "paused"


@pytest.mark.parametrize("usage, used", [
    (Usage(None, None, None, "t"), (20, 5_000_000, 3600)),                  # nothing read
    (Usage(None, None, None, "t", actions=None, tunnel_bytes=10, seconds=1), (20, 10, 1)),
    (Usage(None, None, None, "t", actions=-1, tunnel_bytes=10, seconds=1), (20, 10, 1)),
    # a byte cap exists and no total was read: the readable action count is recorded, the byte cap stands in
    (Usage(None, None, None, "t", actions=5, tunnel_bytes=None, seconds=1), (5, 5_000_000, 1)),
    (Usage(None, None, None, "t", actions=True, tunnel_bytes=10, seconds=1), (20, 10, 1)),
])
def test_an_unreadable_count_records_its_own_cap_as_used_and_is_no_breach(store, usage, used):
    _with_agent(store)
    _reserve(store)
    out = _settle(store, usage)
    assert (out.debited_usd_micros, out.breach, out.basis) == (0, False, "unreadable")
    r = _row(store)
    assert (r["used_actions"], r["used_tunnel_bytes"], r["used_seconds"]) == used
    assert _operation(store) == "active"


@pytest.mark.parametrize("usage, used", [
    # L3 r1 (complement MED): 500 calls on a cap of 20 were masked as "unreadable" by a missing byte total
    (Usage(None, None, None, "t", actions=500, tunnel_bytes=None, seconds=1), (500, 5_000_000)),
    (Usage(None, None, None, "t", actions=500, tunnel_bytes="?", seconds=1), (500, 5_000_000)),
    # and the other axis: a byte breach with no readable action count
    (Usage(None, None, None, "t", actions=None, tunnel_bytes=5_000_000 + FRONT_READ_BUFFER + 1, seconds=1),
     (20, 5_000_000 + FRONT_READ_BUFFER + 1)),
])
def test_a_known_breach_on_one_axis_stays_a_breach_whatever_the_other_reads(store, usage, used):
    _with_agent(store)
    _reserve(store)
    out = _settle(store, usage)
    assert (out.debited_usd_micros, out.breach, out.basis) == (0, True, "breach")
    r = _row(store)
    assert (r["used_actions"], r["used_tunnel_bytes"]) == used
    assert _operation(store) == "paused"


def test_a_run_with_no_byte_cap_needs_no_byte_total(store):
    _with_agent(store)
    _reserve(store, executor=_SubExec(action=ActionExposure(5, "turns", 600, None)))
    out = _settle(store, Usage(None, None, None, "t", actions=5))
    assert (out.breach, out.basis) == (False, "actual")
    r = _row(store)
    assert (r["used_actions"], r["used_tunnel_bytes"], r["used_seconds"]) == (5, None, 600)


@pytest.mark.parametrize("nbytes", [FRONT_READ_BUFFER + 1, 10**12, 2**63 + 5])
def test_a_run_with_no_byte_cap_cannot_breach_on_bytes_and_records_what_it_reports(store, nbytes):
    """L3 r1 (codex HIGH, deepseek HIGH): a NULL byte cap was read as a cap of 0, so 65537 bytes paused the agent."""
    _with_agent(store)
    _reserve(store, executor=_SubExec(action=ActionExposure(5, "turns", 600, None)))
    out = _settle(store, Usage(None, None, None, "t", actions=5, tunnel_bytes=nbytes, seconds=9))
    assert (out.breach, out.basis) == (False, "actual")
    assert _row(store)["used_tunnel_bytes"] == min(nbytes, 2**63 - 1)
    assert _operation(store) == "active"


def test_a_run_with_no_byte_cap_still_breaches_on_actions(store):
    _with_agent(store)
    _reserve(store, executor=_SubExec(action=ActionExposure(5, "turns", 600, None)))
    out = _settle(store, Usage(None, None, None, "t", actions=6, tunnel_bytes=10**9, seconds=9))
    assert (out.breach, out.basis) == (True, "breach")
    assert _operation(store) == "paused"


HUGE = Limits(run_ceiling=2, budget_window_usd_micros=0, billing="subscription", budget_run_calls=2**64,
              budget_run_tunnel_bytes=2**64, **SPAN)
HUGE_B = Limits(run_ceiling=2, budget_window_usd_micros=0, billing="subscription", budget_run_calls=20,
                budget_run_tunnel_bytes=2**64, **SPAN)


@pytest.mark.parametrize("action, limits", [
    (ActionExposure(1, "tool_calls", 2**63, None), SUB),                    # L3 r1 (codex, deepseek): OverflowError
    (ActionExposure(1, "tool_calls", 10**30, 5), SUB),
    (ActionExposure(2**63, "tool_calls", 60, None), HUGE),                  # a link cap past INTEGER range admits none
    (ActionExposure(1, "tool_calls", 60, 2**63), HUGE_B),                   # bytes past range under a larger link cap
    (ActionExposure(1, "tool_calls", 60, 5),                                # a link with no readable byte cap
     Limits(run_ceiling=2, budget_window_usd_micros=0, billing="subscription", budget_run_calls=20,
            budget_run_tunnel_bytes=None, **SPAN)),
])
def test_an_exposure_field_past_sqlite_integer_range_refuses_and_does_not_raise(store, action, limits):
    _, out = _reserve(store, executor=_SubExec(action=action), limits=limits)
    assert out == Refused("no_exposure")
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_reservations").fetchone()[0] == 0


def test_the_largest_integer_deadline_is_still_reserved(store):
    _, out = _reserve(store, executor=_SubExec(action=ActionExposure(1, "tool_calls", 2**63 - 1, None)))
    assert out == Reserved(0)
    assert _row(store)["cap_seconds"] == 2**63 - 1


def test_settling_twice_reports_already_settled_and_keeps_the_first_usage(store):
    _reserve(store)
    _settle(store, Usage(None, None, None, "t", actions=4, tunnel_bytes=9, seconds=2))
    again = _settle(store, Usage(None, None, None, "t", actions=99, tunnel_bytes=9, seconds=2))
    assert (again.basis, again.breach) == ("already_settled", False)
    assert _row(store)["used_actions"] == 4
