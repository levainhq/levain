"""P6-5b: ``settle_terminal`` (A0 r8 §2.4's release table, §2.5 ``launched -> done/failed/stopped``), lapse recovery
routed through it, and the heartbeat's bound on a run that never records its session.

Every row is launched by the real launch transaction on the real agent DDL (the ``Rig`` of the launch tests); each case
was first run as a script against that rig (P65B_DESIGN.md).
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from levain.agents import controller as ctl
from levain.agents.accounting import Usage
from tests.test_agents_launch import Rig, _launched  # noqa: F401  (the rig, its skips and its fixtures)
from tests.test_agents_launch import pytestmark  # noqa: F401


@pytest.fixture
def rig(tmp_path) -> Rig:
    return Rig(tmp_path)


def _reservation(rig: Rig, oid: str) -> tuple:
    with rig.store.read() as c:
        return c.execute("SELECT state, debited_usd_micros, settled_reason, used_actions, used_tunnel_bytes"
                         " FROM agent_reservations WHERE occurrence_id = ?", (oid,)).fetchone()


def _end(rig: Rig, oid: str, epoch: int, state: str, reason: str, usage: Usage) -> ctl.Ended:
    with rig.store.write() as c:
        return ctl.settle_terminal(c, oid, epoch, state, reason, usage, rig.now())


def _operation(rig: Rig) -> str:
    with rig.store.read() as c:
        return c.execute("SELECT operation FROM agents WHERE agent_id = ?", (rig.agent,)).fetchone()[0]


@pytest.mark.parametrize("usage, debited, basis", [
    (Usage(40_000, 10, 20, "t"), 40_000, "actual"),             # §2.4: readable -> actual debited, rest released
    (Usage(None, None, None, "t"), 100_000, "unreadable"),      # §2.4: unreadable -> all of reserved
])
def test_an_end_writes_its_state_settles_the_reservation_and_frees_the_slot(rig, usage, debited, basis):
    # behaviour: launched -> done in one transaction: state, reason and ended_at written, the reservation settled
    # against the executor's usage, the slot freed; a second call (or one at a fenced epoch) writes nothing at all
    oid, epoch = _launched(rig)
    ended = _end(rig, oid, epoch, "done", "completed", usage)
    assert (ended.ended, ended.state, ended.reason, ended.released) == (True, "done", "completed", True)
    assert (ended.settled.debited_usd_micros, ended.settled.basis) == (debited, basis)
    row = rig.row(oid)
    assert (row["state"], row["reason"], row["epoch"]) == ("done", "completed", epoch)
    assert row["ended_at"] == ctl._stamp(rig.now())
    assert rig.taken(oid) == ([], [("settled",)]) and _reservation(rig, oid)[:3] == ("settled", debited, basis)
    before = (rig.row(oid), _reservation(rig, oid))
    assert _end(rig, oid, epoch, "failed", "stop_at", Usage(1, 1, 1, "t")) == ctl.Ended(False)
    assert (rig.row(oid), _reservation(rig, oid)) == before


def test_a_fenced_holder_ends_nothing_and_the_slot_stays_with_the_row(rig):
    # behaviour (the epoch guard): a revoke's bar raised the epoch after launch; the old holder's settle at its launch
    # epoch finds no row, so the reservation stays open and the slot held; the current epoch ends it
    oid, epoch = _launched(rig)
    with rig.store.write() as c:
        c.execute("UPDATE agent_occurrences SET epoch = epoch + 1 WHERE occurrence_id = ?", (oid,))
    assert _end(rig, oid, epoch, "stopped", "revoked", Usage(None, None, None, "t")) == ctl.Ended(False)
    assert rig.row(oid)["state"] == "launched" and rig.taken(oid) == ([(1,)], [("open",)])
    assert _end(rig, oid, epoch + 1, "stopped", "revoked", Usage(None, None, None, "t")).ended is True
    assert rig.row(oid)["state"] == "stopped" and rig.taken(oid) == ([], [("settled",)])


def test_a_breach_ends_failed_budget_breach_and_pauses_the_agent_whatever_was_asked(rig):
    # behaviour (§2.4 row 4): actual above the reservation is debited in full, the row ends failed/budget_breach, and
    # the agent is paused in the same transaction; the slot is freed
    oid, epoch = _launched(rig)
    ended = _end(rig, oid, epoch, "done", "completed", Usage(250_000, 1, 1, "t"))
    assert (ended.state, ended.reason, ended.settled.breach) == ("failed", "budget_breach", True)
    assert (rig.row(oid)["state"], rig.row(oid)["reason"]) == ("failed", "budget_breach")
    assert _reservation(rig, oid)[:3] == ("settled", 250_000, "breach") and _operation(rig) == "paused"
    assert rig.taken(oid)[0] == []


def test_a_subscription_run_records_the_counts_its_executor_read(rig):
    # behaviour (§2.4 r8): on a subscription reservation the end records the executor's calls and bytes (its RUN-STATE
    # count and the front's total, read by the executor) and debits no dollars; a count above its cap is a breach
    oid, epoch = _launched(rig)
    with rig.store.write() as c:
        c.execute("UPDATE agent_reservations SET billing = 'subscription', reserved_usd_micros = 0, cap_actions = 20,"
                  " cap_tunnel_bytes = 1000000, cap_seconds = 600 WHERE occurrence_id = ?", (oid,))
    ended = _end(rig, oid, epoch, "done", "completed", Usage(None, None, None, "t", actions=7, tunnel_bytes=4096,
                                                             seconds=30))
    assert (ended.state, ended.settled.basis) == ("done", "actual")
    assert _reservation(rig, oid) == ("settled", 0, "actual", 7, 4096) and rig.taken(oid)[0] == []


def test_lapse_recovery_routes_through_the_same_end(rig):
    # behaviour: takeover is settle_terminal's end with epoch + 1 and crash_recovered; a usage the caller could read by
    # then is settled as actual (§2.4's lapse row "unless the executor's usage record is readable by then")
    oid, epoch = _launched(rig)
    with rig.store.write() as c:
        assert ctl.takeover(c, oid, rig.now(timedelta(seconds=ctl.LEASE_S + 1)), Usage(30_000, 1, 1, "t")) is True
    row = rig.row(oid)
    assert (row["state"], row["reason"], row["epoch"]) == ("stopped", "crash_recovered", epoch + 1)
    assert _reservation(rig, oid)[:3] == ("settled", 30_000, "actual") and rig.taken(oid)[0] == []
    assert _end(rig, oid, epoch + 1, "stopped", "stop_at", Usage(None, None, None, "t")) == ctl.Ended(False)


@pytest.mark.parametrize("state, reason, epoch", [("done", "stop_at", 0), ("failed", "completed", 0),
                                                  ("missed", "no_slot", 0), ("stopped", "", 0),
                                                  ("stopped", "revoked", "0")])
def test_a_bad_end_is_refused_before_anything_is_read(rig, state, reason, epoch):
    oid, _ = _launched(rig)
    with pytest.raises(ValueError):
        _end(rig, oid, epoch, state, reason, Usage(None, None, None, "t"))
    assert rig.row(oid)["state"] == "launched" and rig.taken(oid) == ([(1,)], [("open",)])


def test_a_launch_that_never_records_a_session_is_not_renewed_past_its_grace(rig):
    # behaviour (P6-5a residue, the seam P6-4a's session supervisor passes): with session_grace_s, a launched row with
    # no session_key is renewed only until launched_at + grace; then the renew is refused and the lease lapses to
    # takeover. With a key it renews; without the bound (a sessionless run, the dream) it renews as before
    oid, epoch = _launched(rig)
    grace = ctl.SESSION_GRACE_S
    with rig.store.write() as c:
        assert ctl.heartbeat(c, oid, epoch, rig.now(timedelta(seconds=grace - 1)), session_grace_s=grace) is True
        assert ctl.heartbeat(c, oid, epoch, rig.now(timedelta(seconds=grace)), session_grace_s=grace) is False
        assert ctl.heartbeat(c, oid, epoch, rig.now(timedelta(seconds=grace)), ctl.LEASE_S) is True
        with pytest.raises(ValueError):
            ctl.heartbeat(c, oid, epoch, rig.now(), session_grace_s=-1)
        assert ctl.record_session_key(c, oid, epoch, "s-" + "5" * 16, rig.now(timedelta(seconds=grace))) is True
        assert ctl.heartbeat(c, oid, epoch, rig.now(timedelta(seconds=grace + 1)), session_grace_s=grace) is True
