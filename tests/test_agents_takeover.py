"""P6-2a: the supervisor heartbeat and fenced takeover."""
from __future__ import annotations

import sqlite3
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


def test_an_expired_holders_heartbeat_does_not_postpone_recovery(tmp_path):
    # L3 r1 codex 1: the late holder still matched state and epoch and pushed its lapsed lease forward
    store, oid = _launched(tmp_path)
    with store.write() as c:
        assert ctl.heartbeat(c, oid, 0, T0 + timedelta(seconds=LEASE + 1), 900) is False
    assert _row(store, oid) == ("launched", None, 0, "2026-11-02T12:01:00Z")
    assert _take(store, oid, T0 + timedelta(seconds=LEASE + 1)) is True


def test_a_lease_never_moves_backwards(tmp_path):
    # L3 r1 codex 2: a renew computed from an earlier clock reading landed second and shortened the lease
    store, oid = _launched(tmp_path)
    for at in (55, 50):
        with store.write() as c:
            assert ctl.heartbeat(c, oid, 0, T0 + timedelta(seconds=at), LEASE) is True
    assert _row(store, oid)[3] == "2026-11-02T12:01:55Z"
    assert _take(store, oid, T0 + timedelta(seconds=111)) is False


def test_a_sub_second_renew_is_never_shorter_than_its_lease(tmp_path):
    # L3 r1 codex 3: the stamp dropped 0.9 s and takeover compared the full clock against it
    store, oid = _launched(tmp_path)
    with store.write() as c:
        assert ctl.heartbeat(c, oid, 0, T0 + timedelta(microseconds=900_000), LEASE) is True
    assert _row(store, oid)[3] == "2026-11-02T12:01:01Z"
    assert _take(store, oid, T0 + timedelta(seconds=LEASE, microseconds=950_000)) is False
    with store.write() as c:  # the same boundary: still live at the stamped second, for a renew too
        assert ctl.heartbeat(c, oid, 0, T0 + timedelta(seconds=LEASE + 1), LEASE) is True


class _LockProbe:
    """A connection in a deferred BEGIN that asks, before every statement after the first, whether a rival writer
    could still commit (a ``BEGIN IMMEDIATE`` with no wait on a second connection)."""

    def __init__(self, conn, path):
        self.conn, self.path, self.seen, self.rival_could_write = conn, path, 0, []

    @property
    def in_transaction(self):
        return self.conn.in_transaction

    def execute(self, sql, *args):
        if self.seen:
            rival = sqlite3.connect(self.path, isolation_level=None, timeout=0)
            try:
                rival.execute("BEGIN IMMEDIATE")
                rival.execute("ROLLBACK")
                self.rival_could_write.append(True)
            except sqlite3.OperationalError:
                self.rival_could_write.append(False)
            finally:
                rival.close()
        self.seen += 1
        return self.conn.execute(sql, *args)


def test_a_deferred_caller_holds_the_write_lock_from_its_first_statement(tmp_path):
    # L3 r1 deepseek MED: a rival commit between the lease read and the CAS was SQLite's busy error, not an answer
    store, oid = _launched(tmp_path)
    for call in (lambda c: ctl.takeover(c, oid, T0 + timedelta(seconds=30)),
                 lambda c: ctl.heartbeat(c, oid, 0, T0 + timedelta(seconds=30), LEASE)):
        conn = sqlite3.connect(store.path, isolation_level=None, timeout=2)
        conn.execute("BEGIN")
        probe = _LockProbe(conn, store.path)
        try:
            call(probe)
        finally:
            conn.execute("ROLLBACK")
            conn.close()
        assert probe.rival_could_write and not any(probe.rival_could_write)
