"""P6-5a: the launch transaction (A0 r8 §2.4, §2.5) and the session key the per-call fence joins on (P7-0 §3.1).

Every store is the real agent DDL; every authority log is real (ssh-keygen keys, links verified through the replay:
the ``World`` of the agent-link tests). Each case was first run as a script against this rig (P65A_DESIGN.md).
"""
from __future__ import annotations

import copy
import json
import shutil
import sqlite3
from datetime import timedelta
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
    assert ctl.SESSION_KEY_RE.pattern == SESSION_RE.pattern
    oid, epoch = _launched(rig)
    seen = []

    def spawn(key):
        seen.append(_fence_rows(rig, key))
        return "child"

    started = ctl.start_run(rig.store, oid, epoch, KEY, spawn)
    assert started == ctl.Started(True, None, "child")
    assert seen == [[(oid,)]]


def test_a_session_key_that_cannot_be_recorded_never_reaches_the_spawn_seam(rig, tmp_path):
    # behaviour: every refused compare-and-set (a key another row names, a second key, an epoch a bar raised, a row
    # that is not launched) returns Started(False) and a failing write raises; spawn is never called, so no claude
    # starts for a session the fence cannot see
    calls = []
    spawn = calls.append
    oid, epoch = _launched(rig)
    assert ctl.start_run(rig.store, oid, epoch, KEY, spawn).spawned is True
    assert ctl.start_run(rig.store, oid, epoch, "s-" + "1" * 16, spawn).spawned is False      # a second key
    rig.finish(oid)
    other = rig.claim("k2")
    result = rig.launch(other)
    assert result.outcome == "launched"
    assert ctl.start_run(rig.store, other, result.launch_epoch, KEY, spawn).spawned is False  # names another row
    with rig.store.write() as c:                                                              # a revoke's bar step
        c.execute("UPDATE agent_occurrences SET epoch = epoch + 1 WHERE occurrence_id = ?", (other,))
    assert ctl.start_run(rig.store, other, result.launch_epoch, "s-" + "2" * 16, spawn).spawned is False
    with pytest.raises(ValueError):
        ctl.start_run(rig.store, other, result.launch_epoch, "s-NOTHEX", spawn)
    # a write that fails at the store: a trigger standing for a full disk
    db = sqlite3.connect(tmp_path / "s" / "autonomic.db", isolation_level=None)
    db.execute("CREATE TRIGGER t_fail BEFORE UPDATE OF session_key ON agent_occurrences"
               " BEGIN SELECT RAISE(ABORT, 'disk full'); END")
    db.close()
    rig.finish(other)
    third = rig.claim("k4")
    r3 = rig.launch(third)
    with pytest.raises(sqlite3.Error, match="disk full"):
        ctl.start_run(rig.store, third, r3.launch_epoch, "s-" + "3" * 16, spawn)
    assert rig.row(third)["session_key"] is None
    assert calls == [KEY]


def test_a_crash_between_the_key_and_the_spawn_is_ended_by_lapse_recovery(rig):
    # behaviour: the key commits, the process dies before spawn; nobody renews the launch's lease, so takeover
    # (lapse recovery, A0 §2.3) ends the row stopped/crash_recovered with epoch + 1, and the session the key names
    # is then STALE at the fence (state and epoch moved), so it can make no call
    oid, epoch = _launched(rig)
    with rig.store.write() as c:
        assert ctl.record_session_key(c, oid, epoch, KEY) is True
    row = rig.row(oid)
    fence, fence_now = agent_fence(row, authority=rig.authority, store_id=rig.sid, now=rig.now)
    lapse = rig.now(timedelta(seconds=ctl.LEASE_S + 1))
    with rig.store.write() as c:
        assert ctl.takeover(c, oid, rig.now(timedelta(seconds=ctl.LEASE_S - 1))) is False   # the lease still holds
        assert ctl.takeover(c, oid, lapse) is True
    ended = rig.row(oid)
    assert (ended["state"], ended["reason"], ended["epoch"]) == ("stopped", "crash_recovered", epoch + 1)
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
