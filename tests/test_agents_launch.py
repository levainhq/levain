"""P6-5a: the launch transaction (A0 r8 §2.4, §2.5) and the session key the per-call fence joins on (P7-0 §3.1).

Every store is the real agent DDL; every authority log is real (ssh-keygen keys, links verified through the replay:
the ``World`` of the agent-link tests). Each case was first run as a script against this rig (P65A_DESIGN.md).
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import levain.authority as auth
from levain.agent_authority import AgentAuthority, agent_fence
from levain.agents import controller as ctl
from levain.agents import slots
from levain.agents.accounting import StopResult, Usage
from levain.agents.role import role_hash
from levain.agents.spec import parse_spec, register_spec
from levain.agents.store import open_agent_store
from levain.coordinator.lease import LEASE_SCHEMA

pytest.importorskip("cryptography")
from tests.test_authority_agent_link import link as link_record, revoke_agent  # noqa: E402
from tests.test_authority_graduation_kind import World  # noqa: E402
from tests.test_consent_queue import Key  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

FIX = Path(__file__).parent / "fixtures" / "agents_p2"
SPECS = json.loads((FIX / "specs.json").read_text())
ROLES = json.loads((FIX / "roles.json").read_text())
BINDING = "bind-0123456789abcdef"
WINDOW = ctl.Window("2026-10-25T00:00:00Z", "2026-10-26T00:00:00Z")
KEY = "s-0123456789abcdef"


class Exec:
    """An executor stating ``micros`` of exposure (API-key route)."""

    def __init__(self, micros=100_000):
        self.max_exposure_usd_micros = micros
        self.action_exposure = None

    def usage(self):
        return Usage(None, None, None, "test")

    def stop(self, deadline_s):
        return StopResult(False, "stub")


def _spec(cron="*/30 * * * *"):
    d = copy.deepcopy(SPECS["job_push_check"])
    d["role"]["role_hash"] = role_hash(ROLES[d["role"]["name"]])
    d["trigger"]["cron"] = cron
    return parse_spec(d)


class Rig:
    def __init__(self, tmp: Path) -> None:
        self.world = World(tmp)
        self.store = open_agent_store(tmp / "s")
        self.agent, self.spec_hash, *_ = register_spec(self.store, _spec(), "America/New_York")
        with self.store.read() as c:
            self.sid = c.execute("SELECT value FROM meta WHERE key = 'store_id'").fetchone()[0]
        self.authority = AgentAuthority(self.world.root)
        self.n = 0

    def now(self, later=timedelta(0)):
        return self.world.clock.now + timedelta(seconds=30) + later

    def link(self, signer="laptop", **over):
        self.n += 1
        self.world.link({"op": "create_grant", "grant": link_record(
            self.world, self.n, agent=self.agent, store=self.sid, spec_hash=self.spec_hash, binding_id=BINDING,
            **over)}, signer)
        return f"al-{self.n:012x}"

    def claim(self, key):
        with self.store.write() as c:
            oid, outcome = ctl.claim(c, agent_id=self.agent, kind="manual", key=key, spec_hash=self.spec_hash,
                                     binding_id=BINDING, window_key="m:2026-10-25", now=self.now())
        assert outcome == ctl.CLAIMED
        return oid

    def launch(self, oid, executor=None, later=timedelta(0)):
        return ctl.launch(self.store, oid, authority=self.authority, executor=executor or Exec(), window=WINDOW,
                          now=self.now(later))

    def row(self, oid):
        with self.store.read() as c:
            c.row_factory = sqlite3.Row
            return dict(c.execute("SELECT * FROM agent_occurrences WHERE occurrence_id = ?", (oid,)).fetchone())

    def taken(self, oid):
        """What ``oid`` holds: (its slot rows, its reservation rows)."""
        with self.store.read() as c:
            return (c.execute("SELECT slot FROM agent_slots WHERE owner_id = ?", (oid,)).fetchall(),
                    c.execute("SELECT state FROM agent_reservations WHERE occurrence_id = ?", (oid,)).fetchall())

    def finish(self, oid):
        """End a launched run by hand (the terminal transaction is P6-5b's)."""
        with self.store.write() as c:
            c.execute("UPDATE agent_occurrences SET state='done', reason='completed', ended_at='x'"
                      " WHERE occurrence_id = ?", (oid,))
            slots.release(c, oid)
            c.execute("UPDATE agent_reservations SET state='settled', debited_usd_micros=reserved_usd_micros"
                      " WHERE occurrence_id = ?", (oid,))


@pytest.fixture
def rig(tmp_path) -> Rig:
    return Rig(tmp_path)


def _stopped(rig: Rig, oid: str, reason: str, result) -> None:
    assert (result.outcome, result.reason) == ("stopped", reason)
    row = rig.row(oid)
    assert (row["state"], row["reason"]) == ("stopped", reason) and row["ended_at"]
    assert row["launched_at"] is None and row["launch_epoch"] is None and row["link_id"] is None
    assert rig.taken(oid) == ([], [])


# --- the DONE line: every refusal writes its A0 §2.5 state with its reason and takes nothing; launched_at is set ------

@pytest.mark.parametrize("case", ["no_link", "retired", "paused", "spec_moved", "barred", "expired", "revoked",
                                  "signer_revoked"])
def test_every_predicate_refusal_ends_stopped_with_its_reason_and_takes_nothing(rig, case):
    # behaviour: admitted -> stopped (A0 §2.5 "the predicate refuses at launch"), its reason named, no slot and no
    # reservation taken (§2.4 "refused at launch (predicate, bar): never taken")
    later = timedelta(0)
    signer = "laptop"
    if case == "signer_revoked":                    # a second laptop key signs the link, then is revoked
        second = Key(rig.world.tmp / "keys", "laptop2")
        rig.world.link({"op": "enrol", "fingerprint": second.fp, "public_key": second.public_key,
                        "class": "laptop-presence", "custody": "passphrase"})
        rig.world.keys["laptop2"] = (second.fp, lambda m: second.sign(m, auth.NAMESPACE))
        signer = "laptop2"
    if case != "no_link":
        rig.link(signer=signer)
    oid = rig.claim("k")
    if case == "retired":
        with rig.store.write() as c:
            c.execute("UPDATE agents SET registration = 'retired' WHERE agent_id = ?", (rig.agent,))
    elif case == "paused":
        with rig.store.write() as c:
            c.execute("UPDATE agents SET operation = 'paused' WHERE agent_id = ?", (rig.agent,))
    elif case == "spec_moved":
        register_spec(rig.store, _spec("*/20 * * * *"), "America/New_York", revise=True)
    elif case == "barred":
        with rig.store.write() as c:
            c.execute("INSERT INTO agent_bars (bar_id, agent_id, barred_at, head_seq_at_bar) VALUES"
                      " ('ab-0000000000000001', ?, 't', 1)", (rig.agent,))
    elif case == "expired":
        later = timedelta(days=40)
    elif case == "revoked":
        rig.world.link(revoke_agent(rig.agent, rig.sid))
    elif case == "signer_revoked":
        rig.world.link({"op": "revoke_key", "fingerprint": rig.world.fp("laptop2")})
    _stopped(rig, oid, case, rig.launch(oid, later=later))


@pytest.mark.parametrize("case", ["budget_window", "no_exposure", "ceiling"])
def test_a_budget_or_ceiling_refusal_ends_skipped_budget_and_takes_nothing(rig, case):
    # behaviour: admitted -> skipped-budget with accounting.reserve's reason; the limits are the admitted link's signed
    # terms (window 5_000_000 micros, run_ceiling 1 here), and the refused launch holds no slot and no reservation
    rig.link(run_ceiling=1)
    if case == "ceiling":
        first = rig.claim("k0")
        assert rig.launch(first).outcome == "launched"
        rig.finish(first)
    oid = rig.claim("k")
    executor = {"budget_window": Exec(10_000_000), "no_exposure": Exec(None), "ceiling": Exec()}[case]
    result = rig.launch(oid, executor)
    assert (result.outcome, result.reason) == ("skipped-budget", case)
    row = rig.row(oid)
    assert (row["state"], row["reason"]) == ("skipped-budget", case) and row["ended_at"] and row["launched_at"] is None
    assert rig.taken(oid) == ([], [])


def test_a_full_cap_or_an_unreadable_cover_writes_nothing_and_leaves_the_row_admitted(rig):
    # behaviour: no free slot is not a refusal (A0 §2.4: retried until grace), and the reservation the transaction
    # took before the slot check is rolled back with it; a log that cannot be replayed never ends a fire
    rig.link()
    oid = rig.claim("k")
    with rig.store.write() as c:
        assert slots.take(c, slots.LAUNCH, "ln-a", rig.now()) == 1
        assert slots.take(c, slots.LAUNCH, "ln-b", rig.now()) == 2
    before = rig.row(oid)
    assert rig.launch(oid).outcome == "no-slot"
    assert rig.row(oid) == before and rig.taken(oid) == ([], [])
    with rig.store.write() as c:
        slots.release(c, "ln-a")
    rig.world.db.write_bytes(b"not a database")
    result = rig.launch(oid)
    assert result.outcome == "unclassified" and "DatabaseError" in result.reason
    assert rig.row(oid) == before and rig.taken(oid) == ([], [])


def test_a_launch_sets_launched_at_and_the_columns_the_fence_trusts(rig):
    # behaviour: admitted -> launched writes launched_at, launch_epoch, the admitted link, its incarnation and a lease,
    # takes one slot and an open reservation; the fence built from that row equals its fence_now (the launch wrote
    # exactly what the per-call predicate compares); a second launch of the row writes nothing
    rig.link()
    newer = rig.link()                       # the highest-born live link of the spec is the one admitted
    oid = rig.claim("k")
    result = rig.launch(oid)
    assert (result.outcome, result.link_id, result.launch_epoch, result.slot) == ("launched", newer, 0, 1)
    row = rig.row(oid)
    at = ctl._stamp(rig.now())
    assert row["state"] == "launched" and row["launched_at"] == at and row["launch_epoch"] == 0
    assert row["link_id"] == newer and row["link_incarnation"] == rig.authority.incarnation(newer)
    assert row["lease_until"] == ctl._stamp(rig.now(timedelta(seconds=ctl.LEASE_S)))
    assert row["session_key"] is None
    assert rig.taken(oid) == ([(1,)], [("open",)])
    fence, fence_now = agent_fence(row, authority=rig.authority, store_id=rig.sid, now=rig.now)
    with rig.store.read() as c:
        assert fence_now(c) == fence
    again = rig.launch(oid)
    assert again.outcome == "not-admitted" and rig.row(oid) == row


# --- the session key: written before the spawn seam, fail closed, and a crash in between is lapse recovery's ---------

def _launched(rig: Rig) -> tuple[str, int]:
    rig.link()
    oid = rig.claim("k")
    result = rig.launch(oid)
    assert result.outcome == "launched"
    return oid, result.launch_epoch


class Gate:
    """A gated child that records what start_run did with it (spawn, release, abort), in ``log``."""

    def __init__(self, key, log, value=None):
        self.key, self.log, self.value = key, log, value
        log.append(("spawn", key))

    def release(self):
        self.log.append(("release", self.key))

    def abort(self):
        self.log.append(("abort", self.key))
        return ctl.GATE_REFUSED


def _fence_rows(rig: Rig, key: str) -> list:
    # levain.qbroker.fence.AgentFences' join, as it reads it (seat/1102-7-p72fix); a fresh read transaction, so only
    # what is committed is seen
    with rig.store.read() as c:
        return c.execute("SELECT occurrence_id FROM agent_occurrences WHERE session_key = ? LIMIT 2",
                         (key,)).fetchall()


def test_the_session_key_is_committed_before_the_spawn_seam_runs(rig):
    # behaviour: start_run records the S1 session id and calls spawn only after that transaction committed: a
    # separate reader inside spawn already finds the one row the fence joins the session to
    from levain.slot.grammar import SESSION_RE
    assert (ctl.SESSION_KEY_RE.pattern, ctl.SESSION_KEY_RE.flags) == (SESSION_RE.pattern, SESSION_RE.flags)
    # every S1 consumer applies it with fullmatch (export, doctor, readset); the two agree on every edge
    for probe in (KEY, KEY + "\n", KEY + "-v2", "S" + KEY[1:], KEY.upper(), "s-" + "a" * 15, "s-" + "a" * 17, ""):
        assert bool(ctl.SESSION_KEY_RE.fullmatch(probe)) == bool(SESSION_RE.fullmatch(probe)), probe
    oid, epoch = _launched(rig)
    seen, log = [], []

    def spawn(key):
        seen.append(_fence_rows(rig, key))
        return Gate(key, log)

    started = ctl.start_run(rig.store, oid, epoch, KEY, spawn, clock=rig.now)
    assert started.spawned is True and started.value.key == KEY
    assert seen == [[(oid,)]] and log == [("spawn", KEY), ("release", KEY)]


def test_a_session_key_that_cannot_be_recorded_never_reaches_the_spawn_seam(rig, tmp_path):
    # behaviour: every refused compare-and-set (a key another row names, a second key, an epoch a bar raised, a row
    # that is not launched) returns Started(False) and a failing write raises; spawn is never called, so no claude
    # starts for a session the fence cannot see
    log = []
    spawn = lambda key: Gate(key, log)  # noqa: E731
    oid, epoch = _launched(rig)
    run = lambda oid, epoch, key: ctl.start_run(rig.store, oid, epoch, key, spawn, clock=rig.now)  # noqa: E731
    assert run(oid, epoch, KEY).spawned is True
    assert run(oid, epoch, "s-" + "1" * 16).spawned is False                                  # a second key
    rig.finish(oid)
    other = rig.claim("k2")
    result = rig.launch(other)
    assert result.outcome == "launched"
    assert run(other, result.launch_epoch, KEY).spawned is False                              # names another row
    with rig.store.write() as c:                                                              # a revoke's bar step
        c.execute("UPDATE agent_occurrences SET epoch = epoch + 1 WHERE occurrence_id = ?", (other,))
    assert run(other, result.launch_epoch, "s-" + "2" * 16).spawned is False
    with pytest.raises(ValueError):
        run(other, result.launch_epoch, "s-NOTHEX")
    # a write that fails at the store: a trigger standing for a full disk
    db = sqlite3.connect(tmp_path / "s" / "autonomic.db", isolation_level=None)
    db.execute("CREATE TRIGGER t_fail BEFORE UPDATE OF session_key ON agent_occurrences"
               " BEGIN SELECT RAISE(ABORT, 'disk full'); END")
    db.close()
    rig.finish(other)
    third = rig.claim("k4")
    r3 = rig.launch(third)
    with pytest.raises(sqlite3.Error, match="disk full"):
        run(third, r3.launch_epoch, "s-" + "3" * 16)
    assert rig.row(third)["session_key"] is None
    assert log == [("spawn", KEY), ("release", KEY)]
    with rig.store.write() as c:
        c.execute("DROP TRIGGER t_fail")


def test_a_crash_between_the_key_and_the_spawn_is_ended_by_lapse_recovery(rig):
    # behaviour: the key commits, the process dies before spawn; nobody renews the launch's lease, so takeover
    # (lapse recovery, A0 §2.3) ends the row stopped/crash_recovered with epoch + 1, and the session the key names
    # is then STALE at the fence (state and epoch moved), so it can make no call
    oid, epoch = _launched(rig)
    with rig.store.write() as c:
        assert ctl.record_session_key(c, oid, epoch, KEY, rig.now()) is True
    row = rig.row(oid)
    fence, fence_now = agent_fence(row, authority=rig.authority, store_id=rig.sid, now=rig.now)
    lapse = rig.now(timedelta(seconds=ctl.LEASE_S + 1))
    with rig.store.write() as c:
        assert ctl.takeover(c, oid, rig.now(timedelta(seconds=ctl.LEASE_S - 1))) is False   # the lease still holds
        assert ctl.takeover(c, oid, lapse) is True
    ended = rig.row(oid)
    assert (ended["state"], ended["reason"], ended["epoch"]) == ("stopped", "crash_recovered", epoch + 1)
    assert rig.taken(oid) == ([], [("settled",)])                     # P6-5b: takeover settles and frees
    assert _fence_rows(rig, KEY) == [(oid,)]
    with rig.store.read() as c:
        assert fence_now(c) != fence


# --- the coordinator-lease probes over dispatch.db ------------------------------------------------------------------

def test_dispatch_probes_read_the_coordinator_store(tmp_path):
    # behaviour: launch_over is True for a gone or ended launch, False while pending or active, None with no event;
    # session_alive is False only for a swept launch; a missing dispatch.db raises (probe_over: cannot tell)
    db = sqlite3.connect(tmp_path / "dispatch.db")
    for ddl in LEASE_SCHEMA:
        db.execute(ddl)
    g = iter(range(1, 100))
    for lid, kinds in (("ln-p", ["pending"]), ("ln-a", ["pending", "active"]), ("ln-e", ["pending", "active", "ended"]),
                       ("ln-f", ["pending", "failed"]), ("ln-x", ["pending", "expired"]), ("ln-0", [])):
        db.execute("INSERT INTO launches (launch_id, gseq, session_key, harness, role, threads, token, placement, at)"
                   " VALUES (?, ?, ?, 'claude', 'coordinator', '[]', 1, 'p', 't')", (lid, next(g), "s-" + lid))
        for seq, kind in enumerate(kinds, 1):
            db.execute("INSERT INTO launch_events (launch_id, seq, gseq, kind, cause, at) VALUES (?, ?, ?, ?, 'x', 't')",
                       (lid, seq, next(g), kind))
    db.execute("INSERT INTO launch_sweeps (launch_id, gseq, cause, at) VALUES ('ln-e', ?, 'not running', 't')",
               (next(g),))
    db.commit()
    db.close()
    over, alive = ctl.dispatch_probes(tmp_path)
    assert [over(x) for x in ("ln-p", "ln-a", "ln-e", "ln-f", "ln-x", "ln-0", "ln-gone")] == \
        [False, False, True, True, True, None, True]
    assert [alive(x) for x in ("ln-e", "ln-a")] == [False, None]
    assert slots.probe_over(["ln-a", "ln-e", "ln-0"], over, alive) == frozenset({"ln-e"})
    nowhere_over, _ = ctl.dispatch_probes(tmp_path / "absent")
    with pytest.raises(sqlite3.Error):
        nowhere_over("ln-a")
    assert slots.probe_over(["ln-a"], nowhere_over, None) == frozenset()


# --- L3 r1 fixes: each test below was first run against the pre-fix code and failed there ---------------------------

class Racing:
    """The shared AgentAuthority, with another thread's refresh landing between the launch's refresh and its peek:
    ``refresh`` returns the snapshot it replayed (S1), then ``between()`` changes the log and the inner authority is
    refreshed again, so every later peek validates S2."""

    def __init__(self, inner: AgentAuthority, between) -> None:
        self.inner, self.between = inner, between

    def refresh(self):
        s1 = self.inner.refresh()
        self.between()
        self.inner.refresh()
        return s1

    def peek(self, *a, **kw):
        return self.inner.peek(*a, **kw)


def test_a_link_recreated_between_the_launch_refresh_and_its_peek_is_not_launched_under_the_old_terms(rig):
    # behaviour (codex HIGH r1 #1): the link id the launch picked from its snapshot S1 was revoked and created again for
    # another Binding before the peek; the peek answers from S2 with S2's incarnation. The launch must not admit S1's
    # terms under S2's incarnation: it writes nothing and stays admitted (retried next tick from one snapshot)
    gid = rig.link()
    oid = rig.claim("k")

    def recreate():
        rig.world.link({"op": "revoke_grant", "grant_id": gid})
        rig.world.link({"op": "create_grant", "grant": link_record(
            rig.world, 1, agent=rig.agent, store=rig.sid, spec_hash=rig.spec_hash, binding_id="bind-fedcba9876543210")})

    before = rig.row(oid)
    result = ctl.launch(rig.store, oid, authority=Racing(rig.authority, recreate), executor=Exec(), window=WINDOW,
                        now=rig.now(timedelta(hours=1)))         # after the recreate's own (clock-stepped) signing
    assert result.outcome == "unclassified" and "moved" in result.reason
    assert rig.row(oid) == before and rig.taken(oid) == ([], [])


class Peeks:
    """An authority whose peek answers ``answer`` for every link (the replay is the real one)."""

    def __init__(self, inner: AgentAuthority, answer) -> None:
        self.inner, self.answer = inner, answer

    def refresh(self):
        return self.inner.refresh()

    def peek(self, *a, **kw):
        return self.answer


@pytest.mark.parametrize("answer", [(True, ""), (True, 0), (True, None)])
def test_a_live_peek_with_an_incarnation_that_is_not_the_snapshots_is_retried_not_ended(rig, answer):
    # behaviour (deepseek MED r1 #1): a live answer whose incarnation is falsy or not the snapshot's never ends the
    # occurrence (stopped is terminal); it writes nothing and the row stays admitted
    rig.link()
    oid = rig.claim("k")
    before = rig.row(oid)
    result = ctl.launch(rig.store, oid, authority=Peeks(rig.authority, answer), executor=Exec(), window=WINDOW,
                        now=rig.now())
    assert result.outcome == "unclassified"
    assert rig.row(oid) == before and rig.taken(oid) == ([], [])


def test_a_link_signed_for_another_store_never_admits_and_the_store_id_is_not_the_callers(rig):
    # behaviour (codex HIGH + complement LOW r1 #3): the store id the links are matched on is the store's own meta row;
    # a link for the same agent/spec/Binding signed for another store ends the occurrence no_link, and no caller can
    # name the other store's id
    import inspect
    assert "store_id" not in inspect.signature(ctl.launch).parameters
    other = "store-" + "e" * 32
    rig.world.link({"op": "create_grant", "grant": link_record(
        rig.world, 1, agent=rig.agent, store=other, spec_hash=rig.spec_hash, binding_id=BINDING)})
    oid = rig.claim("k")
    _stopped(rig, oid, "no_link", rig.launch(oid))


def test_a_session_key_is_not_recorded_on_a_lapsed_lease(rig):
    # behaviour (codex HIGH r1 #2): a launch whose lease has lapsed belongs to lapse recovery even before takeover runs;
    # its key is refused and spawn is never called. The same row inside its lease records it.
    calls = []
    spawn = lambda key: Gate(key, calls)  # noqa: E731
    oid, epoch = _launched(rig)
    lapsed = lambda: rig.now(timedelta(seconds=ctl.LEASE_S + 1))  # noqa: E731
    assert ctl.start_run(rig.store, oid, epoch, KEY, spawn, clock=lapsed).spawned is False
    with rig.store.write() as c:
        assert ctl.record_session_key(c, oid, epoch, KEY, rig.now(timedelta(seconds=ctl.LEASE_S))) is False
    assert rig.row(oid)["session_key"] is None and calls == []
    live = lambda: rig.now(timedelta(seconds=ctl.LEASE_S - 1))  # noqa: E731
    assert ctl.start_run(rig.store, oid, epoch, KEY, spawn, clock=live).spawned is True
    assert calls == [("spawn", KEY), ("release", KEY)]


def test_a_takeover_between_the_key_commit_and_the_release_never_lets_the_child_run(rig, tmp_path):
    # behaviour (P6-5b ruling (ii), closing the residue of codex HIGH r1 #2): takeover commits after the key and after
    # the gated child exists, before the release; start_run's lease-checked transaction finds the row stopped and
    # aborts the child, which exits GATE_REFUSED without exec (its marker is never written). The row ends settled with
    # its slot freed, and the session the key names is still STALE at the fence. The same gate on a live row runs.
    oid, epoch = _launched(rig)
    marker = tmp_path / "ran"
    argv = [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('ran')"]

    def spawn(key):
        child = ctl.GatedChild(argv)
        with rig.store.write() as c:                                     # recovery, in another transaction
            assert ctl.takeover(c, oid, rig.now(timedelta(seconds=ctl.LEASE_S + 1))) is True
        return child

    started = ctl.start_run(rig.store, oid, epoch, KEY, spawn, clock=rig.now)
    assert started.spawned is False and started.value.process.returncode == ctl.GATE_REFUSED
    assert not marker.exists()
    assert (rig.row(oid)["state"], rig.row(oid)["epoch"]) == ("stopped", epoch + 1)
    assert rig.taken(oid) == ([], [("settled",)])
    fence, fence_now = agent_fence(rig.row(oid) | {"launch_epoch": epoch}, authority=rig.authority, store_id=rig.sid,
                                   now=rig.now)
    with rig.store.read() as c:
        assert fence_now(c) != fence
    live = rig.claim("k2")
    result = rig.launch(live)
    ran = ctl.start_run(rig.store, live, result.launch_epoch, "s-" + "4" * 16, lambda key: ctl.GatedChild(argv),
                        clock=rig.now)
    assert ran.spawned is True and ran.value.process.wait(30) == 0 and marker.read_text() == "ran"


def test_a_transaction_sqlite_already_rolled_back_raises_its_own_error_not_the_savepoints(rig, tmp_path):
    # behaviour (complement MED r1 #5): a reservation write that SQLite answers by rolling the whole transaction back
    # (RAISE(ROLLBACK), as SQLITE_FULL does) surfaces that error, not "no such savepoint"; nothing is written
    rig.link()
    oid = rig.claim("k")
    db = sqlite3.connect(tmp_path / "s" / "autonomic.db", isolation_level=None)
    db.execute("CREATE TRIGGER t_full BEFORE INSERT ON agent_reservations BEGIN SELECT RAISE(ROLLBACK, 'disk full'); END")
    db.close()
    before = rig.row(oid)
    with pytest.raises(sqlite3.IntegrityError, match="disk full"):
        rig.launch(oid)
    assert rig.row(oid) == before and rig.taken(oid) == ([], [])


def test_a_coordinator_slot_with_no_launch_row_is_probed_only_after_its_own_lease(rig, tmp_path):
    # behaviour (complement MED r1 #6): launch_over answers True for a launch row that is gone, but only launch slots
    # whose own lease (slots.LEASE_S from the take, renewed by the watcher) has expired are probed at all
    # (slots.expired_launches), so a coordinator that took a slot and has not committed its launches row yet keeps it
    db = sqlite3.connect(tmp_path / "dispatch.db")
    for ddl in LEASE_SCHEMA:
        db.execute(ddl)
    db.commit()
    db.close()
    over, alive = ctl.dispatch_probes(tmp_path)
    rig.link()
    oid = rig.claim("k")
    with rig.store.write() as c:
        slots.take(c, slots.LAUNCH, "ln-young", rig.now())
        slots.take(c, slots.LAUNCH, "ln-other", rig.now())
    young = ctl.launch(rig.store, oid, authority=rig.authority, executor=Exec(), window=WINDOW,
                       now=rig.now(timedelta(seconds=slots.LEASE_S - 1)), launch_over=over, session_alive=alive)
    assert young.outcome == "no-slot" and rig.row(oid)["state"] == "admitted"
    late = ctl.launch(rig.store, oid, authority=rig.authority, executor=Exec(), window=WINDOW,
                      now=rig.now(timedelta(seconds=slots.LEASE_S)), launch_over=over, session_alive=alive)
    assert late.outcome == "launched"


# --- P6-5b: the fire process and c9 (a crash between the launch commit and the first effect) -------------------------

ROOT = str(Path(ctl.__file__).resolve().parents[2])   # the fire imports the levain under test, not an installed one
ENV = dict(os.environ, PYTHONPATH=ROOT)
# a fire whose run body (P6-4a's) marks that it began and then blocks: the moment between the claim and the first
# effect, held open so the test can kill the real process there
BLOCKING_FIRE = ("import sys, time; from datetime import datetime; from levain.agents import controller as ctl; "
                 "oid, store, mark, at = sys.argv[1:5]; now = datetime.fromisoformat(at); "
                 "sys.exit(ctl.fire(store, oid, clock=lambda: now, "
                 "run=lambda s, o, e: (open(mark, 'w').write(str(e)), time.sleep(120), 9)[2]))")


def _settlement(rig: Rig, oid: str) -> tuple:
    with rig.store.read() as c:
        return c.execute("SELECT state, debited_usd_micros, settled_reason FROM agent_reservations"
                         " WHERE occurrence_id = ?", (oid,)).fetchone()


def _wait_for(path: Path, proc: subprocess.Popen, seconds: float = 30.0) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists():
        assert proc.poll() is None, f"the fire exited {proc.returncode} before {path.name}"
        assert time.monotonic() < deadline, f"no {path.name} in {seconds}s"
        time.sleep(0.05)


def test_c9_the_launcher_dies_before_the_fire_exists_and_lapse_recovery_settles_it(rig):
    # behaviour (c9, first cut): the launch commits (slot 1, an open reservation of 100_000) and no fire is ever
    # spawned. Nothing renews the lease; takeover ends the row stopped/crash_recovered at epoch + 1, debits all of the
    # reservation (unreadable usage, A0 §2.4's lapse row) and frees the slot, which the next launch takes
    oid, epoch = _launched(rig)
    assert rig.taken(oid) == ([(1,)], [("open",)]) and rig.row(oid)["pid"] is None
    with rig.store.write() as c:
        assert ctl.takeover(c, oid, rig.now(timedelta(seconds=ctl.LEASE_S + 1))) is True
    row = rig.row(oid)
    assert (row["state"], row["reason"], row["epoch"]) == ("stopped", "crash_recovered", epoch + 1)
    assert rig.taken(oid) == ([], [("settled",)]) and _settlement(rig, oid) == ("settled", 100_000, "unreadable")
    with rig.store.write() as c:                                     # a second recovery changes nothing
        assert ctl.takeover(c, oid, rig.now(timedelta(seconds=ctl.LEASE_S + 2))) is False
    nxt = rig.claim("k2")
    assert rig.launch(nxt, later=timedelta(seconds=ctl.LEASE_S + 2)).slot == 1


def test_c9_crash_after_launch_commit(rig, tmp_path):
    # behaviour (c9, the BUILD_PLAN P6-5 Done line): the REAL fire process writes its identity record (matching
    # process_identity of the live fire), claims its row (pid), and is killed before its first effect; lapse recovery
    # ends the row stopped/crash_recovered with its slot freed and its reservation settled. A late fire on the settled
    # row claims nothing and exits 0 having run nothing
    from levain.consent.delivery import process_identity
    oid, epoch = _launched(rig)
    began = tmp_path / "began"
    argv = [sys.executable, "-c", BLOCKING_FIRE, oid, str(rig.store.directory), str(began), rig.now().isoformat()]
    proc = ctl.spawn_fire(rig.store.directory, oid, argv=argv, env=ENV)
    try:
        _wait_for(began, proc)
        assert os.getsid(proc.pid) == proc.pid                     # a session leader: no signal of its reaches us
        record = json.loads(ctl.fire_record_path(rig.store.directory, oid).read_text())
        assert record["pid"] == proc.pid == rig.row(oid)["pid"] and began.read_text() == str(epoch)
        assert process_identity(proc.pid) == (record["started"], record["args"]) and oid in record["args"].split()
        os.killpg(proc.pid, signal.SIGKILL)                         # our own unreaped Popen (BUILD_PLAN §2, rule a)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(30)
    assert rig.row(oid)["state"] == "launched" and rig.taken(oid) == ([(1,)], [("open",)])
    with rig.store.write() as c:
        assert ctl.takeover(c, oid, rig.now(timedelta(seconds=ctl.LEASE_S + 1))) is True
    row = rig.row(oid)
    assert (row["state"], row["reason"], row["epoch"]) == ("stopped", "crash_recovered", epoch + 1)
    assert rig.taken(oid) == ([], [("settled",)]) and _settlement(rig, oid) == ("settled", 100_000, "unreadable")
    ctl.fire_record_path(rig.store.directory, oid).unlink()          # so the late fire reaches its claim
    late = tmp_path / "late"
    argv[5] = str(late)
    assert ctl.spawn_fire(rig.store.directory, oid, argv=argv, env=ENV).wait(60) == 0
    assert not late.exists() and rig.row(oid)["pid"] == record["pid"]


def test_the_fire_entry_claims_once_and_a_second_fire_does_nothing(tmp_path):
    # behaviour: spawn_fire's default argv (the module entry) runs in its own session, writes its identity record,
    # claims the row with its pid, and exits FIRE_UNWIRED (no run body until P6-4a). A second fire finds the record,
    # claims nothing and exits 0; the first fire's identity and pid stay. The rig's clock is set near the wall clock,
    # because the entry judges the lease on the real clock.
    rig = Rig(tmp_path)
    rig.world.clock.now = datetime.now(timezone.utc) - timedelta(minutes=5)
    oid, _ = _launched(rig)
    first = ctl.spawn_fire(rig.store.directory, oid, env=ENV)
    assert first.wait(60) == ctl.FIRE_UNWIRED
    assert first.args == ctl.fire_argv(rig.store.directory, oid)
    record = json.loads(ctl.fire_record_path(rig.store.directory, oid).read_text())
    assert record["pid"] == first.pid == rig.row(oid)["pid"] and record["args"].split()[-1] == oid
    assert "no run body is wired" in (Path(rig.store.directory) / "fires" / f"{oid}.log").read_text()
    second = ctl.spawn_fire(rig.store.directory, oid, env=ENV)
    assert second.wait(60) == 0
    assert json.loads(ctl.fire_record_path(rig.store.directory, oid).read_text()) == record
    assert rig.row(oid)["pid"] == first.pid
