"""P6-2a: the supervisor heartbeat and fenced takeover."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from levain.agents import controller as ctl
from levain.agents.store import open_agent_store
from tests.test_agents_claim import T0, _store

LEASE = 60


def _launched(tmp_path):
    """A store with one occurrence launched at T0 under a lease of LEASE seconds."""
    store = _store(tmp_path)
    from tests import test_agents_claim as tc
    with store.write() as c:
        oid, _ = ctl.claim(c, agent_id=tc.AG, kind="scheduled", key="k1", spec_hash="h1", binding_id="b1",
                           window_key="d:2026-11-02", now=T0)
        c.execute("UPDATE agent_occurrences SET state='launched', launch_epoch=epoch, link_id='l1',"
                  " link_incarnation='i1', launched_at=?, lease_until=? WHERE occurrence_id=?",
                  ("2026-11-02T12:00:00Z", "2026-11-02T12:01:00Z", oid))
    return store, oid


def _row(store, oid):
    with store.read() as c:
        return c.execute("SELECT state, reason, epoch, lease_until FROM agent_occurrences WHERE occurrence_id=?",
                         (oid,)).fetchone()


def _take(store, oid, at):
    with store.write() as c:
        return ctl.takeover(c, oid, at)


def test_lapsed_heartbeat_takeover_bumps_epoch_once(tmp_path):
    store, oid = _launched(tmp_path)
    late = T0 + timedelta(seconds=LEASE + 1)
    assert _take(store, oid, late) is True
    assert _row(store, oid)[:3] == ("stopped", "crash_recovered", 1)
    assert _take(store, oid, late + timedelta(hours=1)) is False  # the row is terminal: no second bump
    assert _row(store, oid)[2] == 1


def test_live_heartbeat_is_not_taken_over(tmp_path):
    store, oid = _launched(tmp_path)
    assert _take(store, oid, T0 + timedelta(seconds=LEASE)) is False  # at the lease's edge it is still live
    with store.write() as c:
        assert ctl.heartbeat(c, oid, 0, T0 + timedelta(seconds=50), LEASE) is True
    assert _take(store, oid, T0 + timedelta(seconds=LEASE + 40)) is False  # renewed: live until now+LEASE
    assert _row(store, oid)[:3] == ("launched", None, 0)


def test_two_racing_takeovers_bump_epoch_once(tmp_path):
    store, oid = _launched(tmp_path)
    late = T0 + timedelta(seconds=LEASE + 1)
    barrier = threading.Barrier(2)
    results = []

    def go():
        s = open_agent_store(tmp_path / "s")
        barrier.wait()
        with s.write() as c:
            results.append(ctl.takeover(c, oid, late))

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False, True]
    assert _row(store, oid)[2] == 1


def test_old_holders_heartbeat_is_refused_by_the_fence(tmp_path):
    store, oid = _launched(tmp_path)
    assert _take(store, oid, T0 + timedelta(seconds=LEASE + 1)) is True
    before = _row(store, oid)
    with store.write() as c:  # the woken holder, still believing epoch 0
        assert ctl.heartbeat(c, oid, 0, T0 + timedelta(seconds=LEASE + 2), LEASE) is False
    with store.write() as c:  # and one that somehow knew epoch 1 is refused by the row's state
        assert ctl.heartbeat(c, oid, 1, T0 + timedelta(seconds=LEASE + 2), LEASE) is False
    assert _row(store, oid) == before
