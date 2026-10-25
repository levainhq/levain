"""S1 3c-7: the pool and the binding (CONTRACT §3.3 root + dispatch rows, §3.5 Pool; 3c design r3 row 3c-7).

CONTROL's half runs through `Control.handle` on test_tickets' rig (a real signed route map, a real ticket journal on
disk, keys' attachment recorded). The reaper's half runs the REAL pass (:meth:`levain.slot.reaper.Reaper.run_pass`) on
the start tests' world (a tmp store, the real CONTROL, the fake process table), so the SLOT-SYNC a test reads is the
one the reaper read under the pool lock. The ticketed `prepare` runs the real runtime helper `main` on lane F2's world
with a real ticket store. What only a VM run can show (root's real uids, the installed units) is 3c-10's."""
from __future__ import annotations

import io
import json
import os

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import control as C
from levain.slot import records as R
from levain.slot import runtime_helper as H
from levain.slot import tickets as T
from levain.slot.ids import SlotId
from tests.test_routemaps import st  # noqa: F401 - the signed route-map store fixture
from tests.test_tickets import (DISPATCH_UID, SESSION, Clock, Rig, d_attach_claims, lease, mint_args, refused, root,
                                ticket_of)

TS = "2026-10-20T00:00:00Z"


def entry(cls: str, index: int, rev: int, seq: int, state: str = "reusable", session: str | None = None,
          eligible: bool = True) -> dict:
    obj = {"format": R.SLOT_STATE_FORMAT, "class": cls, "index": index, "rev": rev, "lease_seq": seq, "state": state,
           "dispatch_eligible": eligible, "changed_at": TS}
    if session is not None:
        obj["session"] = session
    return obj


def sync(rig, *entries: dict) -> dict:
    return root(rig, "SLOT-SYNC", slots=list(entries))


def pool(rig) -> dict:
    ans = rig.ask("POOL")
    assert ans["ok"], ans
    return {k: ans[k] for k in ("egress_free", "none_free")}


def attach(rig, ticket: str, session: str = SESSION, *, index: int = 1, seq: int = 1, rev: int = 1) -> dict:
    _conn, lo = lease(rig, ticket)
    return root(rig, "ATTACH", lease_nonce=lo["lease_nonce"], sudo_uid=DISPATCH_UID, session=session,
                claims=d_attach_claims(ticket, slot={"class": "egress", "index": index}, lease_seq=seq,
                                       slot_rev=rev))


@pytest.fixture
def rig(st):  # noqa: F811
    return Rig(st, Clock())


# ---- CONTROL: the mirror, the mint refusal, the release ------------------------------------------------------------

def test_mint_refuses_no_free_slot(rig):
    """§3.5: RUN-TICKET-MINT refuses `no-free-slot:egress` when (reusable AND dispatch_eligible egress slots in the
    mirror) - (open unbound egress tickets) - 1 (the operator's) <= 0; POOL.egress_free is that same count."""
    # e0..e2 reusable and eligible; every other slot the rig synced is absent from this snapshot = not reusable
    assert sync(rig, entry("egress", 0, 1, 0), entry("egress", 1, 1, 0), entry("egress", 2, 1, 0))["ok"]
    assert pool(rig) == {"egress_free": 2, "none_free": 0}
    one = ticket_of(rig.mint("a-1"))
    assert pool(rig) == {"egress_free": 1, "none_free": 0}
    ticket_of(rig.mint("a-2"))
    assert pool(rig) == {"egress_free": 0, "none_free": 0}
    before = rig.lines()
    assert refused(rig.mint("a-3")) == "no-free-slot:egress"
    assert rig.lines() == before                                          # a refused mint journals nothing
    assert refused(rig.ask("RUN-TICKET-STATUS", attempt_id="a-3")) == "attempt-unknown"
    assert rig.ask("RUN-TICKET-CANCEL", ticket=one)["ok"]                 # a cancel gives the place back
    assert pool(rig)["egress_free"] == 1
    ticket_of(rig.mint("a-3"))
    # a slot that is not dispatch_eligible (a credential in keep/, a baseline) never counts
    assert sync(rig, entry("egress", 0, 2, 0, eligible=False), entry("egress", 1, 1, 0), entry("egress", 2, 1, 0))["ok"]
    assert pool(rig)["egress_free"] == -1                                 # the two open tickets now overcommit by one
    assert refused(rig.mint("a-4")) == "no-free-slot:egress"
    # an unbound ticket past V16 holds no place
    rig.clock.t += T.TICKET_UNUSED_S
    assert pool(rig)["egress_free"] == 1
    ticket_of(rig.mint("a-4"))


def test_empty_mirror_admits_no_mint(rig):
    """CONTROL's mirror is memory: a restarted CONTROL admits no ticket until root's first sync."""
    rig.control.stop()
    control = C.Control(C.StateStore(rig.sdir), rig.control.operator_uid, dispatch_uid=DISPATCH_UID,
                        keys_uid=rig.control.keys_uid, routes=rig.cr, tickets=T.TicketStore(
                            rig.sdir / T.JOURNAL, rig.st.L / "policy" / "dispatch_ceiling", clock=rig.clock,
                            ceiling_uid=os.getuid()), clock=rig.clock)
    control.start()
    rig.control, rig.tickets = control, control.tickets
    rig.attach(rig.cr.head())
    assert pool(rig) == {"egress_free": -1, "none_free": 0}
    assert refused(rig.mint("a-1")) == "no-free-slot:egress"


def test_stale_sync_after_attach_ignored(rig):
    """§3.5: a snapshot read before `prepare` and delivered after its `ATTACH` changes nothing: ATTACH set the mirror
    entry `leased` at the claims' `slot_rev`, and an entry replaces the mirror's only with a GREATER `rev`."""
    stale = [entry("egress", i, 0, 0) for i in range(8)]                 # what the reaper read before the prepare
    one = ticket_of(rig.mint())
    assert pool(rig)["egress_free"] == 8 - 1 - 1                          # the unbound ticket holds its place
    att = attach(rig, one, index=1, seq=1, rev=1)
    assert att["ok"], att
    # the bind moved the ticket out of the unbound count and ATTACH's mark moved e1 out of the reusable one, together
    assert pool(rig)["egress_free"] == 8 - 1 - 1
    assert sync(rig, *stale)["ok"]
    e1 = rig.control.pool.entry("egress", 1)
    assert (e1.state, e1.session, e1.rev, e1.lease_seq) == ("leased", SESSION, 1, 1)
    assert pool(rig)["egress_free"] == 8 - 1 - 1
    assert rig.ask("RUN-TICKET-STATUS", attempt_id="a-1")["state"] == "bound"   # an older lease_seq releases nothing
    # the next real snapshot (prepare's lease at rev 1) is equal, so still nothing moves
    assert sync(rig, *stale[:1], entry("egress", 1, 1, 1, "leased", SESSION), *stale[2:])["ok"]
    assert pool(rig)["egress_free"] == 8 - 1 - 1


def test_release_survives_skipped_reusable(rig):
    """§3.5 RELEASE: END then a new lease between two syncs (the sync never saw e1 scrubbed or reusable): the first
    sync whose e1 entry has a GREATER lease_seq releases the old ticket, journaled, and the restart counts it."""
    one = ticket_of(rig.mint("a-1"))
    assert attach(rig, one, index=1, seq=1, rev=1)["ok"]
    assert rig.lines()[-1] == {"op": "bind", "ticket": one, "session": SESSION, "slot": {"class": "egress", "index": 1},
                               "lease_seq": 1}
    assert rig.tickets.counts()["open"] == 1
    others = [entry("egress", i, 0, 0) for i in range(8) if i != 1]
    # the same lease, scrubbed: not over yet
    assert sync(rig, entry("egress", 1, 2, 1, "scrubbed"), *others)["ok"]
    assert rig.ask("RUN-TICKET-STATUS", attempt_id="a-1")["state"] == "bound"
    # rev 3 (reusable) and rev 4 (the next lease) both happened before the next sync read e1
    assert sync(rig, entry("egress", 1, 4, 2, "leased", "s-00000000000000b2"), *others)["ok"]
    assert rig.ask("RUN-TICKET-STATUS", attempt_id="a-1") == {"id": 1, "ok": True, "ticket": one, "state": "released",
                                                               "session": SESSION}
    assert rig.lines()[-1] == {"op": "release", "ticket": one, "session": SESSION}
    assert rig.tickets.counts()["open"] == 0
    # the same lease_seq at reusable (or retired) releases too
    two = ticket_of(rig.mint("a-2"))
    s2 = "s-00000000000000a2"
    assert attach(rig, two, s2, index=2, seq=1, rev=1)["ok"]
    assert sync(rig, entry("egress", 2, 3, 1, "reusable"), *[e for e in others if e["index"] != 2])["ok"]
    assert rig.ask("RUN-TICKET-STATUS", attempt_id="a-2")["state"] == "released"
    rig.restart()                                                          # the rebuild reads the release lines
    for attempt, session in (("a-1", SESSION), ("a-2", s2)):
        assert rig.ask("RUN-TICKET-STATUS", attempt_id=attempt)["state"] == "released"
        assert rig.ask("RUN-TICKET-STATUS", attempt_id=attempt)["session"] == session
    assert rig.tickets.counts()["open"] == 0
    assert [x["op"] for x in rig.lines()].count("tombstone") == 2         # the compaction closed both attempts


def test_release_needs_the_bound_lease(rig):
    """Release keys on the slot the ticket is BOUND to: another slot's later lease, or an absent entry, releases
    nothing; a release line naming no bound ticket refuses the start."""
    one = ticket_of(rig.mint("a-1"))
    assert attach(rig, one, index=1, seq=5, rev=9)["ok"]
    assert sync(rig, entry("egress", 3, 20, 9, "leased", "s-00000000000000b3"))["ok"]   # e1 not in the snapshot
    assert rig.ask("RUN-TICKET-STATUS", attempt_id="a-1")["state"] == "bound"
    assert sync(rig, entry("egress", 1, 10, 4, "reusable"))["ok"]          # an older lease_seq
    assert rig.ask("RUN-TICKET-STATUS", attempt_id="a-1")["state"] == "bound"
    rig.control.stop()
    with open(rig.sdir / T.JOURNAL, "ab") as f:
        f.write(canon.canonical_bytes({"op": "release", "ticket": one, "session": "s-00000000000000ff"}) + b"\n")
    with pytest.raises(Refused) as e:
        rig.tickets = T.TicketStore(rig.sdir / T.JOURNAL, rig.st.L / "policy" / "dispatch_ceiling", clock=rig.clock,
                                    ceiling_uid=os.getuid())
        rig.tickets.start()
    assert e.value.name == "journal-inconsistent"


def test_slot_sync_refuses_a_bad_snapshot_whole(rig):
    """A malformed entry, a slot listed twice or a sync past SLOT_SYNC_MAX is refused and changes nothing; only root
    may send one."""
    good = entry("egress", 0, 5, 0)
    for bad, name in (([good, dict(good, state="busy")], "slot-state-malformed"),
                      ([good, dict(good, rev=6)], "bad-value"),
                      ([entry("egress", 0, 5, 0, "leased")], "slot-state-malformed"),       # leased with no session
                      ([good] * (C.SLOT_SYNC_MAX + 1), "bad-value"),
                      (["not-an-object"], "slot-state-malformed")):
        assert refused(root(rig, "SLOT-SYNC", slots=bad)) == name, bad
    assert rig.control.pool.entry("egress", 0).rev == 0                   # the rig's own sync still stands
    assert refused(rig.control.handle(C.Conn(DISPATCH_UID), {"v": 1, "op": "SLOT-SYNC", "id": 1, "slots": []})) \
        == "verb-not-allowed-for-peer"
    assert refused(root(rig, "POOL")) == "verb-not-allowed-for-peer"


def test_revoked_set_reads_the_mirror(rig):
    """§3.3 REVOKED-SET: a RETAINED record stays in the set while the mirror shows its slot still leased to it (or has
    no entry for it), and leaves it once the mirror shows the slot past that session."""
    s = "s-00000000000000c7"
    claims = {"operator_uid": rig.control.operator_uid, "supervisor_uid": rig.control.operator_uid,
              "slot": {"class": "egress", "index": 9}, "class": "egress", "kind": "interactive", "vendor": "claude",
              "auth": "brokered", "lease_seq": 3, "slot_rev": 7}
    rig.control.store.put(R.SessionState(session=s, generation=4, state=R.RETAINED, claims=claims, lease_live=False,
                                         event_kind="state", updated_at=TS))
    assert s in root(rig, "REVOKED-SET")["sessions"]                     # e9: no entry
    rig.control.pool.mark_leased("egress", 9, s, 3, 7)
    assert s in root(rig, "REVOKED-SET")["sessions"]                     # still leased to it
    assert sync(rig, entry("egress", 9, 8, 3, "reusable"))["ok"]
    assert s not in root(rig, "REVOKED-SET")["sessions"]


def test_pool_mirror_merge_rules():
    """The mirror alone: GREATER rev replaces, an absent index is not reusable and keeps its rev, the same rev listed
    again restores it, a mark below the mirror's rev changes nothing."""
    m = C.PoolMirror()
    S = lambda d: R.parse_slot_state(canon.canonical_bytes(d))  # noqa: E731
    assert m.free("egress") == 0
    m.merge([S(entry("egress", 0, 3, 1)), S(entry("egress", 1, 3, 1)), S(entry("none", 0, 1, 0))])
    assert (m.free("egress"), m.free("none")) == (2, 1)
    m.merge([S(entry("egress", 0, 3, 1))])                                # e1 and v0 absent
    assert (m.free("egress"), m.free("none")) == (1, 0)
    m.merge([S(entry("egress", 0, 3, 1)), S(entry("egress", 1, 2, 1))])   # an OLDER e1 never comes back
    assert m.free("egress") == 1
    m.merge([S(entry("egress", 0, 3, 1)), S(entry("egress", 1, 3, 1))])   # the same rev restores it
    assert m.free("egress") == 2
    m.mark_leased("egress", 0, SESSION, 2, 3)                             # not greater than rev 3: ignored
    assert m.free("egress") == 2
    m.mark_leased("egress", 0, SESSION, 2, 4)
    assert m.free("egress") == 1 and m.entry("egress", 0).state == "leased"


# ---- the reaper: the sync it sends, a dropped one, the next pass --------------------------------------------------

def test_pool_mirror_rebuilt_from_sync(tmp_path):
    """§3.5 (level-triggered): a dropped SLOT-SYNC costs one pass. A session torn down (END + SCRUB make its slot
    reusable), the next pass's sync lost on the wire: the mirror still shows the slot leased; the pass after that:
    the slot is reusable again. Every snapshot here is the one the REAL reaper pass read under the pool lock."""
    from levain.slot import reaper as RP
    from tests.test_slot_s2f3_start import E0, _started, _world

    w, la, table = _world(tmp_path)
    syncs: list = []

    def on_call(op, fields):
        if op == "SLOT-SYNC":
            syncs.append(fields["slots"])
            if drop:
                raise Refused("control-down", "the sync is lost")
    drop = False
    w.on_call = on_call
    reaper = RP.Reaper(w.seams, tuning=RP.Tuning(reserve_bytes=4096))
    rep = reaper.run_pass()
    assert not [x for x in rep["skipped"] if x.startswith("slot-sync")], rep["skipped"]
    assert [(e["class"], e["index"], e["state"]) for e in syncs[-1]] == [
        ("egress", 0, "reusable"), ("egress", 1, "reusable"), ("egress", 2, "reusable"), ("none", 0, "reusable")]
    assert (w.control.pool.free("egress"), w.control.pool.free("none")) == (3, 1)

    s = _started(w, la, table)                                            # prepare leased e0: ATTACH marked it
    assert w.control.pool.entry("egress", 0).state == "leased"
    assert w.control.pool.free("egress") == 2
    assert w.main(["teardown", s])[1] == {"ok": True, "state": R.RETAINED}   # END, then SCRUB released e0
    assert w.slot_state(E0).state == "reusable"
    drop = True
    rep = reaper.run_pass()
    assert "slot-sync:control-down" in rep["skipped"]
    assert w.control.pool.free("egress") == 2                             # the lost sync: the mirror lags one pass
    drop = False
    reaper.run_pass()
    assert w.control.pool.free("egress") == 3                             # the next pass: e0 reusable again
    assert syncs[-1][0] == canon.parse(R.slot_state_bytes(w.slot_state(E0)))

    # eligibility and absence come from the same pass: a credential in e1's keep/ (the pass recomputes
    # dispatch_eligible under the pool lock before the snapshot), e2's state file gone (absent = not reusable)
    w.give_credential(SlotId("egress", 1))
    os.unlink(SlotId("egress", 2).slot_dir(w.L) / "state")
    reaper.run_pass()
    assert w.slot_state(SlotId("egress", 1)).dispatch_eligible is False
    assert [e["index"] for e in syncs[-1] if e["class"] == "egress"] == [0, 1]
    assert w.control.pool.free("egress") == 1


def test_busy_pool_lock_skips_the_sync(tmp_path):
    """The snapshot is read only under the pool lock: a pass that cannot TRY it sends no sync (a lost one costs one
    pass), and names why."""
    import fcntl

    from levain.slot import reaper as RP
    from tests.test_slot_s2f3_start import _world

    w, _la, _table = _world(tmp_path)
    calls: list = []
    w.on_call = lambda op, fields: calls.append(op)
    fd = os.open(w.L / "slots" / "pool.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rep = RP.Reaper(w.seams, tuning=RP.Tuning(reserve_bytes=4096)).run_pass()
    finally:
        os.close(fd)
    assert "pool-health:pool-busy" in rep["skipped"] and "SLOT-SYNC" not in calls


# ---- the ticketed prepare (§3.5 point of use, §5.1) ----------------------------------------------------------------

EXEC_FACTS = {"job_id": "job-1", "run_index": 0, "contract_digest": "sha256:" + "1" * 64, "kind": "executor",
              "route": "glm", "routemap_generation": 1, "route_policy_digest": "sha256:" + "4" * 64,
              "model_requested": "z-ai/glm-5.2", "egress_routes": ["github"], "reserved_microdollars": 1_000_000}


#: lane F2's world names `_levain_dispatch` by its own uid (its ACCOUNTS table), not the route-map rig's
from tests.test_slot_s2f2_runtime_helper import DISPATCH_UID as F2_DISPATCH  # noqa: E402


class Dispatch:
    """Lane F2's world with a ticket store and the `_levain_dispatch` peer: tickets minted by the store's own mint (the
    verb's route-map checks are test_tickets'), leases opened as dispatch, `prepare` run as dispatch."""

    def __init__(self, world, request) -> None:
        self.w = world
        L = world.L
        (L / "policy" / "dispatch_ceiling").write_bytes(canon.canonical_bytes(
            {"format": T.CEILING_FORMAT, "max_reserved_microdollars_per_run": 5_000_000, "max_open_tickets": 4,
             "max_runs_per_day": 50}))
        os.chmod(L / "policy" / "dispatch_ceiling", 0o644)
        self.tickets = T.TicketStore(L / "sessions" / T.JOURNAL, L / "policy" / "dispatch_ceiling",
                                     ceiling_uid=os.getuid())
        world.control = C.Control(C.StateStore(L / "sessions"), world.control.operator_uid, dispatch_uid=F2_DISPATCH,
                                  tickets=self.tickets, keys_front_register=lambda *a: None,
                                  keys_front_arm=lambda s, t: None)
        world.control.start()
        request.addfinalizer(world.control.stop)
        self.grants: list = []
        world.seams.grant_search = lambda host, fd, path, broker, uid: self.grants.append((path.name, broker, uid))
        self.n = 0

    def ticket(self) -> str:
        self.n += 1
        return self.tickets.mint(f"a-{self.n}", T.check_facts(EXEC_FACTS), C.new_handle).ticket

    def prepare(self, ticket: str, *args: str) -> dict:
        conn = C.Conn(F2_DISPATCH)
        lo = self.w.control.handle(conn, {"v": 1, "op": "LEASE-OPEN", "id": 1, "ticket": ticket})
        assert lo["ok"], lo
        self.conn = conn
        _code, line = self.w.main(["prepare", *(args or ("egress", "dispatch", "claude", "brokered"))],
                                  canon.canonical_bytes({"lease_nonce": lo["lease_nonce"], "ticket": ticket}),
                                  uid=F2_DISPATCH)
        return line

    def drop(self) -> None:
        """The dispatch lease's EOF (a refused prepare's caller gives up the lease; the ticket stays unbound)."""
        self.w.control.lease_eof(self.conn.lease)


@pytest.fixture
def dispatch(tmp_path, request, monkeypatch):
    from tests import test_slot_s2f2_runtime_helper as F2

    monkeypatch.setitem(F2.ACCOUNTS, H.QBROKER, 790)
    return Dispatch(F2.World(tmp_path), request)


def test_ticketed_prepare_binds_and_writes_ticket_facts(dispatch):
    """§3.5 BINDING + "Ticket facts reach render": the ticketed prepare's claims name the ticket and its slot lease;
    CONTROL binds the ticket to {class, index, lease_seq} and answers the ticket's facts, which alloc carries."""
    d, w = dispatch, dispatch.w
    ticket = d.ticket()
    line = d.prepare(ticket)
    assert line["ok"], (line, w.last_err)
    s = line["session"]
    assert line["slot"] == {"class": "egress", "index": 0}
    a = w.alloc(s)
    assert a.ticket == ticket and a.supervisor_uid == F2_DISPATCH and a.kind == "dispatch"
    assert a.ticket_facts == T.check_facts(EXEC_FACTS)
    op, fields = w.calls[-1]
    assert op == "ATTACH" and fields["claims"]["ticket"] == ticket
    lines = [json.loads(x) for x in (w.L / "sessions" / T.JOURNAL).read_text().splitlines()]
    assert lines[-1] == {"op": "bind", "ticket": ticket, "session": s, "slot": {"class": "egress", "index": 0},
                         "lease_seq": a.lease_seq}
    assert w.control.pool.entry("egress", 0).session == s                 # ATTACH's mark
    assert d.grants == [(s, H.QBROKER, 790)]                              # the broker's search entry on <S>
    assert w.state(s).state == R.ALLOCATED
    assert w.main(["status", s], uid=F2_DISPATCH)[1]["ok"]               # its own ticketed session


def test_ticketed_prepare_never_takes_last_egress_slot(dispatch):
    """§3.5 point of use: under the pool lock a ticketed prepare refuses `operator-slot-reserved` when taking a slot
    would leave no reusable credential-free egress slot; the refusal leaves every slot as it was."""
    d, w = dispatch, dispatch.w
    assert d.prepare(d.ticket())["ok"]                                    # e0
    assert d.prepare(d.ticket())["ok"]                                    # e1
    before = [w.slot_state(SlotId("egress", i)) for i in range(3)]
    line = d.prepare(d.ticket())                                          # e2 is the last free one
    assert line["refused"] == "operator-slot-reserved", line
    assert [w.slot_state(SlotId("egress", i)) for i in range(3)] == before
    assert [op for op, _ in w.calls].count("ATTACH") == 2                # refused before any ATTACH
    nonce, _conn = w.lease()                                              # the operator still gets e2
    assert w.prepare(nonce=nonce)["slot"] == {"class": "egress", "index": 2}


def test_ticketed_prepare_skips_credential_slots(dispatch):
    """A ticketed prepare never takes a slot whose keep/ holds a credential or that has a baseline (the operator's
    passthrough slots), and takes no `<slot>` argument."""
    d, w = dispatch, dispatch.w
    w.give_credential(SlotId("egress", 0))
    w.give_credential(SlotId("egress", 1), "codex", baseline=False)
    ticket = d.ticket()
    assert d.prepare(ticket, "egress", "dispatch", "claude", "brokered", "2")["refused"] == "ticket-slot-argument"
    d.drop()
    line = d.prepare(ticket)
    assert line["refused"] == "operator-slot-reserved", line              # e2 is the only credential-free one
    d.drop()
    w.give_credential(SlotId("egress", 2), baseline=True)
    assert d.prepare(ticket)["refused"] == "no-free-slot:egress"
    assert [op for op, _ in w.calls].count("ATTACH") == 0


def test_ticketed_prepare_refused_attach_gives_the_slot_back(dispatch):
    """A ticket CONTROL will not bind (cancelled since its LEASE-OPEN) refuses the ATTACH: prepare puts the lease back
    and removes its alloc, and the slot is reusable again."""
    d, w = dispatch, dispatch.w
    ticket = d.ticket()
    w.on_call = lambda op, fields: d.tickets.cancel(ticket) if op == "ATTACH" else None
    line = d.prepare(ticket)
    assert line["refused"] == "attempt-closed", line
    assert w.slot_state(SlotId("egress", 0)).state == "reusable"
    assert os.listdir(w.L / "alloc") == [] or all(not n.endswith(".json") for n in os.listdir(w.L / "alloc"))


def test_operator_cannot_prepare_with_a_ticket(dispatch):
    """The ticket rule both ways (bind_stdin): dispatch without one, the operator with one, refused before any act."""
    d, w = dispatch, dispatch.w
    nonce, _c = w.lease()
    out = w.main(["prepare", "egress", "dispatch", "claude", "brokered"], canon.canonical_bytes({"lease_nonce": nonce}),
                 uid=F2_DISPATCH)[1]
    assert out["refused"] == "ticket-required"
    out = w.main(["prepare", "egress", "interactive", "claude", "brokered"],
                 canon.canonical_bytes({"lease_nonce": nonce, "ticket": C.new_handle()}))[1]
    assert out["refused"] == "ticket-not-allowed"
    assert w.slot_state(SlotId("egress", 0)).state == "reusable"
