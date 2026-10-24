"""S1 3c-5 session 1: run tickets (CONTRACT §3.3 dispatch row, §3.5 Ceiling + IDEMPOTENT MINT, §13; 3c design r3
§2.1 and row 3c-5; R4 §2 "Keys' generation at attach"). CONTROL's `RUN-TICKET-MINT` / `-CANCEL` / `-STATUS` through
`Control.handle` over a real signed route map (`tests/test_routemaps.py`'s ssh-keygen fixtures) and a real journal
on disk; `test_mint_waits_for_keys_reload_ack` also runs CONTROL through `control.serve` with keys as a child process.

Binding (`LEASE-OPEN {ticket}`, `ATTACH`), `TOKEN-RESOLVE` `run` and keys' run side are session 2's; pool, release and
`no-free-slot` are 3c-7's; verifier generations 3c-8's."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import control as C
from levain.slot import routemaps as RM
from levain.slot import tickets as T
from tests.test_routemaps import (DISPATCH_UID, OP_UID, Store, item, keys_py, route, sha, st, submit,  # noqa: F401
                                  _crash, _resolved, keys_of, reload)

KEYS_UID = 7002
DAY = 86400.0
PASSTHROUGH = "claude-sub"
GLM_DIGEST = RM.policy_digest(route()["policy"])


class Clock:
    def __init__(self) -> None:
        self.t = 1_790_000_000.0

    def __call__(self) -> float:
        return self.t


def write_ceiling(L: Path, per_run: int = 5_000_000, open_: int = 4, per_day: int = 50) -> None:
    (L / "policy").mkdir(exist_ok=True)
    (L / "policy" / "dispatch_ceiling").write_bytes(canon.canonical_bytes(
        {"format": T.CEILING_FORMAT, "max_reserved_microdollars_per_run": per_run, "max_open_tickets": open_,
         "max_runs_per_day": per_day}))


class Rig:
    """CONTROL over `st`'s lib root with a ticket store, generation 1 active, and keys attached stating it (no keys
    process: the attachment is CONTROL's record of what keys stated)."""

    def __init__(self, st: Store, clock: Clock, *, fault=None) -> None:
        self.st, self.clock = st, clock
        self.sdir = st.L / "sessions"
        self.sdir.mkdir(mode=0o700, exist_ok=True)
        base = canon.parse((st.routes / "base.json").read_bytes())
        if not any(r["route"] == PASSTHROUGH for r in base["routes"]):
            base["routes"].append({"route": PASSTHROUGH, "type": "subscription_passthrough",
                                   "hosts": ["api.anthropic.com"]})
            (st.routes / "base.json").write_bytes(canon.canonical_bytes(base))
        if not (st.L / "policy" / "dispatch_ceiling").exists():
            write_ceiling(st.L)
        self.cr = RM.ControlRoutes(st.routes)
        self.one, sig = st.signed(item(1))
        submit(self.cr, self.one, sig)
        self.control = None
        self.restart(fault=fault)

    def restart(self, *, fault=None, attach: bool = True) -> None:
        if self.control is not None:
            self.control.stop()                                           # a stop is a restart to every caller
        kw = {} if fault is None else {"fault": fault}
        self.tickets = T.TicketStore(self.sdir / T.JOURNAL, self.st.L / "policy" / "dispatch_ceiling",
                                     clock=self.clock, ceiling_uid=os.getuid(), **kw)
        self.cr = RM.ControlRoutes(self.st.routes)
        self.control = C.Control(C.StateStore(self.sdir), OP_UID, dispatch_uid=DISPATCH_UID, keys_uid=KEYS_UID,
                                 routes=self.cr, tickets=self.tickets, clock=self.clock)
        self.control.start()
        if attach:
            self.attach(self.cr.head())

    def attach(self, serves) -> None:
        gen, s = (None, None) if serves is None else (serves["generation"], serves["item_sha256"])
        ans = self.control.handle(C.Conn(KEYS_UID), {"v": 1, "op": "KEYS-ATTACH", "id": 1, "generation": gen,
                                                      "item_sha256": s})
        assert ans["ok"], ans

    def ask(self, op: str, **kw) -> dict:
        return self.control.handle(C.Conn(DISPATCH_UID), {"v": 1, "op": op, "id": 1, **kw})

    def mint(self, attempt: str = "a-1", **kw) -> dict:
        return self.ask("RUN-TICKET-MINT", **mint_args(attempt, **kw))

    def lines(self) -> list:
        return [json.loads(x) for x in (self.sdir / T.JOURNAL).read_text().splitlines()]


def mint_args(attempt: str = "a-1", **kw) -> dict:
    args = {"attempt_id": attempt, "job_id": "job-1", "run_index": 0, "contract_digest": "sha256:" + "1" * 64,
            "kind": "executor", "route": "glm", "routemap_generation": 1, "route_policy_digest": GLM_DIGEST,
            "model_requested": "z-ai/glm-5.2", "egress_routes": ["pypi"], "reserved_microdollars": 1_000_000}
    args.update(kw)
    return {k: v for k, v in args.items() if v is not None}


def ticket_of(ans: dict) -> str:
    assert ans.get("ok") is True and set(ans) == {"id", "ok", "ticket", "run_id"}, ans
    assert T.RUN_ID_RE.fullmatch(ans["run_id"]), ans
    return ans["ticket"]


def refused(ans: dict) -> str:
    assert ans.get("ok") is False, ans
    return ans["refused"]


@pytest.fixture
def rig(st):
    return Rig(st, Clock())


# ---- idempotent mint ------------------------------------------------------------------------------------------------

def test_mint_replay_same_attempt_same_ticket(rig):
    first = rig.mint()
    one = ticket_of(first)
    assert rig.mint() == first                                            # a replay in the same process: same run_id
    assert rig.tickets.counts() == {"open": 1, "today": 1}
    rig.restart()                                                         # CONTROL restarted between
    assert rig.mint() == first                                            # the same ticket and run_id
    assert rig.tickets.counts() == {"open": 1, "today": 1}                # the open and day counts +1 once
    assert [x["op"] for x in rig.lines()] == ["mint"]


def test_mint_attempt_conflict_refused(rig):
    first = rig.mint()
    one = ticket_of(first)
    assert refused(rig.mint(reserved_microdollars=2_000_000)) == "attempt-conflict"
    assert rig.ask("RUN-TICKET-CANCEL", ticket=one)["ok"]
    # facts are compared FIRST: a differing request is a conflict whatever the state; a matching one is closed
    assert refused(rig.mint(reserved_microdollars=2_000_000)) == "attempt-conflict"
    assert refused(rig.mint()) == "attempt-closed"


def test_mint_status_unknown_attempt(rig):
    assert refused(rig.ask("RUN-TICKET-STATUS", attempt_id="a-never")) == "attempt-unknown"
    one = ticket_of(rig.mint("a-2"))
    assert rig.ask("RUN-TICKET-STATUS", attempt_id="a-2") == {"id": 1, "ok": True, "ticket": one, "state": "unbound"}


@pytest.mark.parametrize("bad", ["A-1", "a/1", "", "-a", "a" * 65, "a.1", "a-1\n", 7])
def test_mint_attempt_id_malformed_refused(rig, bad):
    assert refused(rig.mint(bad)) == "ticket-field:attempt_id"
    assert refused(rig.ask("RUN-TICKET-STATUS", attempt_id=bad)) == "ticket-field:attempt_id"


def test_mint_closed_attempt_after_restart_and_rollover(rig):
    first = rig.mint()
    one = ticket_of(first)
    assert rig.ask("RUN-TICKET-CANCEL", ticket=one) == {"id": 1, "ok": True}
    rig.clock.t += DAY                                                    # the UTC day rolled over
    rig.restart()                                                         # and the compaction ran
    assert rig.lines() == [{"op": "tombstone", "attempt_id": "a-1", "ticket": one,
                            "request_digest": T.request_digest(T.check_facts(mint_args())), "state": "cancelled"}]
    assert refused(rig.mint()) == "attempt-closed"
    assert rig.ask("RUN-TICKET-STATUS", attempt_id="a-1")["state"] == "cancelled"
    assert refused(rig.ask("RUN-TICKET-CANCEL", ticket=one)) == "attempt-closed"


# ---- the journal's crash safety --------------------------------------------------------------------------------------

class Crash(BaseException):
    """The compaction's process ending at the fault point: nothing after it runs (a SIGKILL leaves the same disk)."""


def _fault(point: str) -> None:
    if point == "before_rename":
        raise Crash(point)


def test_compaction_crash_atomic_and_journal_inconsistent(rig):
    one = ticket_of(rig.mint("a-1"))
    two = ticket_of(rig.mint("a-2"))
    assert rig.ask("RUN-TICKET-CANCEL", ticket=two)["ok"]
    rig.clock.t += DAY
    old = (rig.sdir / T.JOURNAL).read_bytes()
    with pytest.raises(Crash):
        rig.restart(fault=_fault)                                         # killed after the new file, before rename
    new = rig.sdir / (T.JOURNAL + ".new")
    assert (rig.sdir / T.JOURNAL).read_bytes() == old and new.exists()    # the old journal is whole
    # the leftover is removed at the next start, never adopted: a different ticket planted in it is never seen
    new.write_bytes(new.read_bytes().replace(one.encode(), b"x" * len(one)))
    rig.restart()
    assert not new.exists()
    assert [x["op"] for x in rig.lines()] == ["tombstone", "tombstone"]   # a-1 is past V16 a day on: expired here
    assert rig.ask("RUN-TICKET-STATUS", attempt_id="a-1")["ticket"] == one
    assert rig.ask("RUN-TICKET-STATUS", attempt_id="a-2")["state"] == "cancelled"
    # a journal holding one attempt with two tickets refuses the start
    forged = dict(rig.lines()[0], ticket="y" * len(one))
    with open(rig.sdir / T.JOURNAL, "ab") as f:
        f.write(canon.canonical_bytes(forged) + b"\n")
    with pytest.raises(Refused) as e:
        rig.restart()
    assert e.value.name == "journal-inconsistent"


# ---- the ceiling ---------------------------------------------------------------------------------------------------

def test_ticket_ceiling_refused(rig):
    write_ceiling(rig.st.L, per_run=5_000_000, open_=2, per_day=3)
    assert refused(rig.mint("a-0", reserved_microdollars=5_000_001)) == \
        "ticket-ceiling:max_reserved_microdollars_per_run"
    a = ticket_of(rig.mint("a-1"))
    ticket_of(rig.mint("a-2", reserved_microdollars=5_000_000))
    assert refused(rig.mint("a-3")) == "ticket-ceiling:max_open_tickets"
    assert rig.ask("RUN-TICKET-CANCEL", ticket=a)["ok"]
    ticket_of(rig.mint("a-3"))                                            # an open place again: the day's third mint
    assert rig.ask("RUN-TICKET-CANCEL", ticket=a)["refused"] == "attempt-closed"
    a2 = rig.ask("RUN-TICKET-STATUS", attempt_id="a-2")
    assert rig.ask("RUN-TICKET-CANCEL", ticket=a2["ticket"])["ok"]
    assert refused(rig.mint("a-4")) == "ticket-ceiling:max_runs_per_day"
    ceiling = rig.st.L / "policy" / "dispatch_ceiling"
    ceiling.chmod(0o664)                                                  # L1 note 8: group-writable is no bound
    assert refused(rig.mint("a-5")) == "ticket-ceiling:file"
    ceiling.chmod(0o644)
    rig.tickets.ceiling_uid = os.getuid() + 1                             # not the required owner (root, in use)
    assert refused(rig.mint("a-5")) == "ticket-ceiling:file"
    rig.tickets.ceiling_uid = os.getuid()
    ceiling.unlink()
    assert refused(rig.mint("a-5")) == "ticket-ceiling:file"


def test_ceiling_survives_control_restart(rig):
    write_ceiling(rig.st.L, open_=2, per_day=3)
    t1 = ticket_of(rig.mint("a-1"))
    t2 = ticket_of(rig.mint("a-2"))
    rig.restart()
    assert refused(rig.mint("a-3")) == "ticket-ceiling:max_open_tickets"   # the open count rebuilt
    for t in (t1, t2):
        assert rig.ask("RUN-TICKET-CANCEL", ticket=t)["ok"]
    ticket_of(rig.mint("a-3"))
    rig.restart()
    assert refused(rig.mint("a-4")) == "ticket-ceiling:max_runs_per_day"   # the day count rebuilt (compacted too)
    rig.clock.t += DAY
    rig.restart()
    ticket_of(rig.mint("a-4"))                                             # a new UTC day


def test_ticket_expires(rig):
    first = rig.mint()
    one = ticket_of(first)
    rig.clock.t += T.TICKET_UNUSED_S - 1
    assert rig.ask("RUN-TICKET-STATUS", attempt_id="a-1")["state"] == "unbound"
    rig.clock.t += 1                                                       # V16 unused
    assert rig.ask("RUN-TICKET-STATUS", attempt_id="a-1")["state"] == "expired"
    assert refused(rig.mint()) == "attempt-closed"
    assert refused(rig.ask("RUN-TICKET-CANCEL", ticket=one)) == "attempt-closed"
    assert rig.tickets.counts()["open"] == 0
    rig.restart()
    assert rig.ask("RUN-TICKET-STATUS", attempt_id="a-1")["state"] == "expired"


# ---- the route facts -----------------------------------------------------------------------------------------------

def test_run_ticket_passthrough_route_refused(rig):
    # CONTROL's half of "a dispatch run key on subscription_passthrough = REFUSED": the route is no dispatch-map id
    assert refused(rig.mint("a-1", route=PASSTHROUGH)) == "ticket-field:route"
    assert refused(rig.mint("a-2", egress_routes=[PASSTHROUGH])) == "ticket-field:egress_routes"
    assert refused(rig.mint("a-3", route="pypi")) == "ticket-field:route"
    assert refused(rig.mint("a-4", model_requested="z-ai/other")) == "ticket-field:model_requested"
    assert refused(rig.mint("a-5", route_policy_digest="sha256:" + "0" * 64)) == "ticket-field:route_policy_digest"
    assert refused(rig.mint("a-6", kind="verifier")) == "ticket-field:route"   # forbidden fields for a verifier
    assert rig.tickets.counts()["open"] == 0 and rig.lines() == []


# ---- mint only for what keys serves (R4 §2; head ruling 1023-26 Q2) -------------------------------------------------

def test_mint_waits_for_keys_reload_ack(rig, keys_py, monkeypatch):
    # in-process: CONTROL's head is 2 while keys states 1 on the current attachment
    two, sig = rig.st.signed(item(2, [route(host="api.z.ai")]))
    submit(rig.cr, two, sig)
    rig.attach({"generation": 1, "item_sha256": sha(rig.one)})
    d2 = RM.policy_digest(route(host="api.z.ai")["policy"])
    monkeypatch.setattr(C, "MINT_KEYS_WAIT_S", 0.2)
    assert refused(rig.mint("a-1", routemap_generation=2, route_policy_digest=d2)) == "keys-unavailable"
    assert rig.lines() == []
    # a mint in flight waits; keys' statement of the head on a new attachment lets it through
    monkeypatch.setattr(C, "MINT_KEYS_WAIT_S", 10.0)
    out = []
    th = threading.Thread(target=lambda: out.append(rig.mint("a-1", routemap_generation=2, route_policy_digest=d2)))
    th.start()
    time.sleep(0.2)
    assert not out                                                        # still waiting
    rig.attach({"generation": 2, "item_sha256": sha(two)})
    th.join(5)
    assert ticket_of(out[0])
    assert refused(rig.mint("a-2", routemap_generation=1)) == "ticket-field:routemap_generation"

    # the process half: a crash after `current`'s rename, before RELOAD's ack; CONTROL restarts and keys acks the head
    # through the attach-time RELOAD; then keys restarts with CONTROL up
    from tests import b2_keys_rig as B2
    from tests import c3_rig as R3
    three, sig3 = rig.st.signed(item(3, [route()]))
    assert _crash(rig.st, three, sig3, "after_current") == -9
    rig.control.stop()                          # one CONTROL per journal (fix 2): the in-process one stops first
    args = mint_args("a-9", routemap_generation=3)
    req = {"v": 1, "op": "RUN-TICKET-MINT", "id": 1, **args}
    seen: dict = {}

    async def run():
        loop = asyncio.get_running_loop()
        ct = R3.ControlThread(str(rig.st.L), routes_dir=rig.st.routes, dispatch_uid=DISPATCH_UID).start()
        try:
            monkeypatch.setattr(C, "MINT_KEYS_WAIT_S", 0.2)
            seen["before_keys"] = ct.control.handle(C.Conn(DISPATCH_UID), req)
            monkeypatch.setattr(C, "MINT_KEYS_WAIT_S", 10.0)
            keys = B2.KeysProc(str(rig.st.L), keys_py, 1, driver=R3.DRIVER)
            await keys.start()
            seen["with_keys"] = await loop.run_in_executor(None, ct.control.handle, C.Conn(DISPATCH_UID), req)
            seen["serves"] = ct.control.keys_serves()
            assert keys.stop() == 0
            keys.cleanup()
            await R3.aio_until(lambda: ct.control.keys_serves() is None, what="the attachment ended")
            monkeypatch.setattr(C, "MINT_KEYS_WAIT_S", 0.2)
            req2 = {**req, "attempt_id": "a-10"}
            seen["keys_down"] = ct.control.handle(C.Conn(DISPATCH_UID), req2)
            monkeypatch.setattr(C, "MINT_KEYS_WAIT_S", 10.0)
            keys = B2.KeysProc(str(rig.st.L), keys_py, 1, driver=R3.DRIVER)
            await keys.start()
            seen["keys_back"] = await loop.run_in_executor(None, ct.control.handle, C.Conn(DISPATCH_UID), req2)
            assert keys.stop() == 0
            keys.cleanup()
        finally:
            ct.stop()
    asyncio.run(run())
    assert refused(seen["before_keys"]) == "keys-unavailable", seen
    assert ticket_of(seen["with_keys"]) and seen["serves"] == {"generation": 3, "item_sha256": sha(three)}
    assert refused(seen["keys_down"]) == "keys-unavailable"
    assert ticket_of(seen["keys_back"])


# ---- session 2: the binding (§3.1, §3.3, §3.5 "Ticket facts reach render"; head ruling H1) ----------------------------

SESSION = "s-00000000000000a1"
FRONT = {"uid": 3989, "pid": 4242, "start": 1700000000}


def d_attach_claims(ticket: str, **kw) -> dict:
    base = {"operator_uid": OP_UID, "supervisor_uid": DISPATCH_UID, "slot": {"class": "egress", "index": 1},
            "class": "egress", "kind": "dispatch", "vendor": "claude", "auth": "brokered", "lease_seq": 1,
            "slot_rev": 1, "ticket": ticket}
    base.update(kw)
    return base


def d_mint_claims(**kw) -> dict:
    base = {"routes": ["glm", "pypi"], "egress_routes": ["pypi"], "model": "z-ai/glm-5.2", "queue_grant": True,
            "incarnation": 1, "front": dict(FRONT)}
    base.update(kw)
    return base


def root(rig, op: str, **kw) -> dict:
    return rig.control.handle(C.Conn(0), {"v": 1, "op": op, "id": 1, **kw})


def lease(rig, ticket) -> tuple:
    conn = C.Conn(DISPATCH_UID)
    args = {} if ticket is None else {"ticket": ticket}
    return conn, rig.control.handle(conn, {"v": 1, "op": "LEASE-OPEN", "id": 1, **args})


def bound_session(rig, session: str = SESSION) -> tuple:
    """A minted ticket, a dispatch lease on it, and root's ATTACH: (ticket, run_id, ATTACH's answer)."""
    ans = rig.mint()
    ticket = ticket_of(ans)
    _conn, lo = lease(rig, ticket)
    att = root(rig, "ATTACH", lease_nonce=lo["lease_nonce"], sudo_uid=DISPATCH_UID, session=session,
               claims=d_attach_claims(ticket))
    return ticket, ans["run_id"], att


def to_rendering(rig, session: str = SESSION) -> int:
    ans = root(rig, "CAS", session=session, generation=0, **{"from": "ALLOCATED", "to": "RENDERING"}, fields={})
    assert ans["ok"], ans
    return ans["generation"]


def test_lease_open_takes_a_live_unbound_ticket(rig):
    assert refused(lease(rig, None)[1]) == "ticket-required"
    assert refused(lease(rig, "A" * 43)[1]) == "ticket-unknown"
    one = ticket_of(rig.mint())
    first, lo = lease(rig, one)
    assert lo["ok"] and len(lo["lease_nonce"]) == 43
    assert refused(lease(rig, one)[1]) == "ticket-bound"                 # one ticket, one live lease
    rig.control.lease_eof(first.lease)                                   # the lease dies before its ATTACH
    _c, again = lease(rig, one)
    assert again["ok"], again                                            # the ticket is still unbound: held again
    assert rig.ask("RUN-TICKET-STATUS", attempt_id="a-1")["state"] == "unbound"
    two = ticket_of(rig.mint("a-2"))
    assert rig.ask("RUN-TICKET-CANCEL", ticket=two)["ok"]
    assert refused(lease(rig, two)[1]) == "attempt-closed"


def test_attach_binds_and_answers_ticket_facts(rig):
    one = ticket_of(rig.mint())
    other = ticket_of(rig.mint("a-2"))
    for claims in (d_attach_claims(other), d_attach_claims(one, kind="planner"),
                   d_attach_claims(one, kind="interactive"), {k: v for k, v in d_attach_claims(one).items()
                                                              if k != "ticket"}):
        _c, lo = lease(rig, one)
        ans = root(rig, "ATTACH", lease_nonce=lo["lease_nonce"], sudo_uid=DISPATCH_UID, session=SESSION,
                   claims=claims)
        assert refused(ans) == "claims-mismatch", claims
        rig.control.lease_eof(_c.lease)                                   # leave no live lease holding the ticket
    _c, lo = lease(rig, one)
    att = root(rig, "ATTACH", lease_nonce=lo["lease_nonce"], sudo_uid=DISPATCH_UID, session=SESSION,
               claims=d_attach_claims(one))
    facts = T.check_facts(mint_args())
    assert att == {"id": 1, "ok": True, "generation": 0, "ticket_facts": facts}
    assert "run_id" not in att["ticket_facts"] and one not in json.dumps(att)
    assert rig.lines()[-1] == {"op": "bind", "ticket": one, "session": SESSION}
    assert refused(rig.ask("RUN-TICKET-CANCEL", ticket=one)) == "ticket-bound"
    assert refused(rig.mint()) == "attempt-closed"                        # a bound attempt is no longer replayable
    assert refused(lease(rig, one)[1]) == "ticket-bound"


def test_status_keeps_session(rig):
    # the bound half (CONTRACT §3.5): `released` is 3c-7's SLOT-SYNC
    one, _run, att = bound_session(rig)
    assert att["ok"], att
    want = {"id": 1, "ok": True, "ticket": one, "state": "bound", "session": SESSION}
    assert rig.ask("RUN-TICKET-STATUS", attempt_id="a-1") == want
    rig.clock.t += DAY                                                    # the compaction keeps a bound ticket whole
    rig.restart()
    assert [x["op"] for x in rig.lines()] == ["mint", "bind"]
    assert rig.ask("RUN-TICKET-STATUS", attempt_id="a-1") == want
    assert rig.tickets.counts()["open"] == 1                              # bound is open (H4: until 3c-7's release)
    assert refused(rig.ask("RUN-TICKET-CANCEL", ticket=one)) == "ticket-bound"


def test_mint_claims_checked_against_the_ticket(rig):
    one, run_id, att = bound_session(rig)
    rig.control._keys_front_register = lambda *a: 1
    for bad in (d_mint_claims(model="z-ai/other"), d_mint_claims(routes=["glm"]),
                d_mint_claims(egress_routes=[], routes=["glm"]), d_mint_claims(routes=["glm", "pypi", "pypi2"]),
                {k: v for k, v in d_mint_claims().items() if k != "model"}):
        gen = rig.control.store.get(SESSION).generation
        if rig.control.store.get(SESSION).state == "ALLOCATED":
            gen = to_rendering(rig)
        assert refused(root(rig, "MINT", session=SESSION, generation=gen, claims=bad)) == "claims-mismatch", bad
    gen = rig.control.store.get(SESSION).generation
    ans = root(rig, "MINT", session=SESSION, generation=gen, claims=d_mint_claims())
    assert ans["ok"], ans
    # TOKEN-RESOLVE: the run from the ticket record, and the ticket nowhere in the answer (head ruling H1)
    h = rig.control.handles(SESSION)
    res = rig.control.handle(C.Conn(KEYS_UID), {"v": 1, "op": "TOKEN-RESOLVE", "id": 1,
                                                 "token_digest": h.token_digest})
    assert res["ok"], res
    assert res["run"] == {"run_id": run_id, "job_id": "job-1", "run_index": 0, "contract_digest": "sha256:" + "1" * 64,
                          "route": "glm", "routemap_generation": 1, "route_policy_digest": GLM_DIGEST,
                          "model_requested": "z-ai/glm-5.2", "reserved_microdollars": 1_000_000}
    assert "ticket" not in res["claims"] and one not in json.dumps(res)
    assert rig.control._run_facts(SESSION) == {"run_id": run_id, "job_id": "job-1", "run_index": 0,
                                                "contract_digest": "sha256:" + "1" * 64,
                                                "model_requested": "z-ai/glm-5.2"}


def test_token_resolve_with_no_bound_ticket_refused(rig):
    # a ticketed session whose binding the journal no longer holds is never resolved with a run (fail closed)
    one, _run, att = bound_session(rig)
    rig.control._keys_front_register = lambda *a: 1
    root(rig, "MINT", session=SESSION, generation=to_rendering(rig), claims=d_mint_claims())
    h = rig.control.handles(SESSION)
    rig.tickets._by_ticket[one].state = T.CANCELLED                       # the binding gone from CONTROL's index
    res = rig.control.handle(C.Conn(KEYS_UID), {"v": 1, "op": "TOKEN-RESOLVE", "id": 1,
                                                 "token_digest": h.token_digest})
    assert refused(res) == "run-unknown"


def test_pin_holds_for_session_life(rig):
    # R4 §8 test 6, through a real ticket: CONTROL mints, binds and resolves the session; keys holds what that resolve
    # answers (keys.resolve_outcome) and serves each `Q`'s route by its own pin rule across new generations: N+1
    # CHANGES the route (served from P), N+2 removes it (refused while absent), N+3 re-adds it (served from P again),
    # then REVOKE (CONTROL answers token-revoked, so keys holds no claims to serve with)
    from levain.broker import keys as K
    one, run_id, _att = bound_session(rig)
    rig.control._keys_front_register = lambda *a: 1
    assert root(rig, "MINT", session=SESSION, generation=to_rendering(rig), claims=d_mint_claims())["ok"]
    digest = rig.control.handles(SESSION).token_digest

    def resolve() -> tuple:
        return K.resolve_outcome(rig.control.handle(C.Conn(KEYS_UID), {"v": 1, "op": "TOKEN-RESOLVE", "id": 1,
                                                                         "token_digest": digest}))
    kind, answer = resolve()
    assert kind == "answer" and answer["claims"]["run"]["routemap_generation"] == 1
    claims = answer["claims"]
    k = keys_of(rig.st)
    assert reload(k, 1, sha(rig.one))["generation"] == 1
    assert _resolved(k, claims, "glm") == "openrouter.ai"                     # P
    seen = []
    for g, routes in ((2, [route(host="api.z.ai")]), (3, [route(rid="other")]), (4, [route(host="api.z.ai")])):
        it, sig = rig.st.signed(item(g, routes))
        submit(rig.cr, it, sig)
        assert reload(k, g, sha(it))["generation"] == g
        seen.append(_resolved(k, claims, "glm"))
    assert seen == ["openrouter.ai",                                            # N+1 changed it: still P
                    "route-not-allowed|rule 2",                                 # removed from the head
                    "openrouter.ai"]                                            # re-added: P again, not N+3's entry
    assert root(rig, "REVOKE", session=SESSION)["ok"]
    assert resolve() == ("refused", "token-revoked")


def test_body_policy_refused_at_route_submit(rig):
    # head ruling H2: a body_policy outside the four keys or their value sets, or a non-empty one on a route that is
    # not an OpenRouter route, is refused at ROUTE-SUBMIT (`route-invalid:body_policy`), never at request time
    good = route()
    good["body_policy"] = {"provider_only": ["z-ai"], "allow_fallbacks": "no", "zdr": "yes", "data_collection": "deny"}
    bads = [{"provider_only": "z-ai"}, {"allow_fallbacks": "false"}, {"zdr": "true"}, {"data_collection": "maybe"},
            {"order": ["z-ai"]}, {"provider_only": []},
            {"zdr": True}, {"zdr": 1}]                                    # L1 note 9: a non-text value, H2's name
    gen = 2
    for bp in bads:
        bad = dict(route(), body_policy=bp)
        it, sig = rig.st.signed(item(gen, [bad]))
        assert refused(rig.ask("ROUTE-SUBMIT", map=it, sig=sig)) == "route-invalid:body_policy", bp
    off = dict(route(host="api.z.ai"), body_policy={"zdr": "yes"})
    it, sig = rig.st.signed(item(gen, [off]))
    assert refused(rig.ask("ROUTE-SUBMIT", map=it, sig=sig)) == "route-invalid:body_policy"
    it, sig = rig.st.signed(item(gen, [good]))
    assert rig.ask("ROUTE-SUBMIT", map=it, sig=sig) == {"id": 1, "ok": True, "generation": gen}


# ---- session 3: generation pruning (head ruling H5) and the ticket's reach (H8) --------------------------------------

class KeysShim:
    """CONTROL's keys admin wire over a real `Keys`: each RELOAD runs keys' own handler with the `keep` CONTROL sent."""

    def __init__(self, k) -> None:
        self.k, self.keeps = k, []

    def call(self, op: str, **kw) -> dict:
        assert op == "RELOAD", op
        self.keeps.append(kw["keep"])
        return reload(self.k, kw["generation"], kw["item_sha256"], keep=frozenset(kw["keep"]))

    def close(self) -> None:
        pass


def keys_on_wire(rig):
    """A real keys behind CONTROL's admin wire, loaded with CONTROL's head (generation 1) by CONTROL's own RELOAD."""
    k = keys_of(rig.st)
    rig.control.keys_admin = KeysShim(k)
    with rig.control._activation:
        rig.control._reload_keys(rig.cr.head())
    assert [e["generation"] for e in rig.st.keys_head()["served"]] == [1]
    return k


def submit_gen(rig, g: int, routes) -> dict:
    it, sig = rig.st.signed(item(g, routes))
    assert rig.ask("ROUTE-SUBMIT", map=it, sig=sig)["ok"]
    return it


def served_gens(rig) -> list:
    return [e["generation"] for e in rig.st.keys_head()["served"]]


def test_reload_keeps_a_generation_an_open_ticket_references(rig):
    # H5: generation 1 is named by an open ticket, so the RELOADs of 2 and 3 keep it (2 nobody references: pruned
    # by keys, then its files deleted by CONTROL once keys' ack leaves it out). The ticket cancelled, the RELOAD of 4
    # keeps only what keys' own cache still pins (a cached run on 3).
    k = keys_on_wire(rig)
    one = ticket_of(rig.mint())
    submit_gen(rig, 2, [route(host="api.z.ai")])
    submit_gen(rig, 3, [route()])
    assert rig.control.keys_admin.keeps[-1] == [1, 3]
    assert served_gens(rig) == [1, 3] and sorted(k.routes.maps) == [1, 3]
    assert rig.st.gen_files() == ["1.json", "1.sig", "3.json", "3.sig"]
    assert rig.ask("RUN-TICKET-CANCEL", ticket=one)["ok"]
    k.cache["d" * 64] = {"session": SESSION, "claims": {"run": {"routemap_generation": 3}}}
    submit_gen(rig, 4, [route(host="api.z.ai")])
    assert rig.control.keys_admin.keeps[-1] == [4]
    assert served_gens(rig) == [3, 4] and sorted(k.routes.maps) == [3, 4]
    assert rig.st.gen_files() == ["3.json", "3.sig", "4.json", "4.sig"]   # 3 listed by keys' ack: never deleted


def test_control_never_deletes_a_file_keys_ack_still_lists(rig):
    # H5: CONTROL's own keep names only its head (no ticket), but keys' cache pins a run to generation 1, so keys'
    # ack still lists 1 and CONTROL keeps its files. keys restarts after the prune: every generation its head lists
    # loads, so the served ids are known and a base route still resolves (a missing file would fail them all closed).
    from tests.test_routemaps import _base_with, _resolved
    k = keys_on_wire(rig)
    k.cache["d" * 64] = {"session": SESSION, "claims": {"run": {"routemap_generation": 1}}}
    submit_gen(rig, 2, [route(host="api.z.ai")])
    submit_gen(rig, 3, [route(rid="other")])
    assert rig.control.keys_admin.keeps[-1] == [3]
    assert served_gens(rig) == [1, 3]
    assert rig.st.gen_files() == ["1.json", "1.sig", "3.json", "3.sig"]
    _base_with(rig.st, "zz")
    k2 = keys_of(rig.st)
    assert sorted(k2.routes.maps) == [1, 3] and k2.routes.problem is None
    assert k2.routes.route_ids() == frozenset({"glm", "other"})
    assert _resolved(k2, {"routes": ["zz"]}, "zz") == "api.z.ai"
    pinned = {"routes": ["glm"], "run": {"route": "glm", "routemap_generation": 1, "route_policy_digest": GLM_DIGEST}}
    assert _resolved(k2, pinned, "glm") == "route-not-allowed|rule 2"   # 1 still served (rule 1 holds); 3 lacks glm


def test_status_carries_no_ticket(rig):
    # H8 (H1's reach): the operator's STATUS of a dispatch session never carries the ticket
    one, _run, att = bound_session(rig)
    assert att["ok"], att
    ans = rig.control.handle(C.Conn(OP_UID), {"v": 1, "op": "STATUS", "id": 1, "session": SESSION})
    assert ans["ok"] and len(ans["records"]) == 1, ans
    assert "ticket" not in ans["records"][0]["claims"] and one not in json.dumps(ans)
    assert ans["records"][0]["claims"]["kind"] == "dispatch"


def test_crash_between_bind_and_session_record_unbinds(rig, monkeypatch):
    # H8: the bind line is journaled, then the record's write fails (CONTROL dies before it): at the next start the
    # ticket is unbound again (no session existed), the compaction drops the bind, and the ticket binds anew
    one = ticket_of(rig.mint())
    _c, lo = lease(rig, one)

    class Died(BaseException):
        pass

    def no_record(rec):
        raise OSError("the record's write never happened")

    def died(*a):
        raise Died()                                                      # CONTROL dies: no unbind is journaled
    monkeypatch.setattr(rig.control.store, "put", no_record)
    monkeypatch.setattr(rig.tickets, "unbind", died)
    with pytest.raises(Died):
        root(rig, "ATTACH", lease_nonce=lo["lease_nonce"], sudo_uid=DISPATCH_UID, session=SESSION,
             claims=d_attach_claims(one))
    assert [x["op"] for x in rig.lines()] == ["mint", "bind"]
    monkeypatch.undo()
    rig.restart()
    assert [x["op"] for x in rig.lines()] == ["mint"]
    assert rig.ask("RUN-TICKET-STATUS", attempt_id="a-1") == {"id": 1, "ok": True, "ticket": one, "state": "unbound"}
    _c, lo = lease(rig, one)
    att = root(rig, "ATTACH", lease_nonce=lo["lease_nonce"], sudo_uid=DISPATCH_UID, session=SESSION,
               claims=d_attach_claims(one))
    assert att["ok"] and "ticket_facts" in att, att


# ---- 3c-5 fix 1: a failed append poisons the store; ATTACH's failure paths -----------------------------------------

def test_failed_append_poisons_the_store(rig, monkeypatch):
    # L1 W1 + L2 2a: the mint line is written, its fsync fails. The store is poisoned: the retry of the SAME attempt,
    # STATUS and LEASE-OPEN answer journal-unavailable (never internal-error, never a second mint line). The restart
    # reads the complete last line as the truth: the replay answers that one ticket.
    def eio(fd):
        raise OSError(5, "EIO")
    rig.tickets._fsync = eio
    assert refused(rig.mint()) == "journal-unavailable"
    rig.tickets._fsync = os.fsync                                         # the disk is back; the store stays poisoned
    assert refused(rig.mint()) == "journal-unavailable"
    assert refused(rig.ask("RUN-TICKET-STATUS", attempt_id="a-1")) == "journal-unavailable"
    assert refused(lease(rig, "A" * 43)[1]) == "journal-unavailable"
    [line] = rig.lines()                                                  # nothing appended after the failed line
    rig.restart()
    first = rig.mint()
    assert ticket_of(first) == line["ticket"] and first["run_id"] == line["run_id"]
    assert rig.lines() == [line] and rig.tickets.counts() == {"open": 1, "today": 1}
    # L2 2b: a write that fails part-way (ENOSPC) leaves a torn last line; the restart drops it
    def torn(fd, data):
        os.write(fd, data[:len(data) // 2])
        raise OSError(28, "ENOSPC")
    monkeypatch.setattr(T, "_write_all", torn)
    assert refused(rig.mint("a-2")) == "journal-unavailable"
    monkeypatch.undo()
    assert not (rig.sdir / T.JOURNAL).read_bytes().endswith(b"\n")
    rig.restart()
    assert rig.lines() == [line]
    ticket_of(rig.mint("a-2"))
    assert rig.tickets.counts() == {"open": 2, "today": 2}


def test_attach_record_failure_unbinds_the_ticket(rig, monkeypatch):
    # L2 4a: the bind is journaled, then the session record's write fails (disk full): ATTACH's failure path unbinds
    # the ticket, journaled, so it is not stranded BOUND until a restart; the retried ATTACH binds it
    one = ticket_of(rig.mint())
    _c, lo = lease(rig, one)

    def no_room(rec):
        raise OSError(28, "ENOSPC")
    monkeypatch.setattr(rig.control.store, "put", no_room)
    attach = {"lease_nonce": lo["lease_nonce"], "sudo_uid": DISPATCH_UID, "session": SESSION,
              "claims": d_attach_claims(one)}
    assert root(rig, "ATTACH", **attach)["ok"] is False
    assert [x["op"] for x in rig.lines()] == ["mint", "bind", "unbind"]
    assert rig.ask("RUN-TICKET-STATUS", attempt_id="a-1")["state"] == "unbound"
    monkeypatch.undo()
    att = root(rig, "ATTACH", **attach)
    assert att["ok"] and "ticket_facts" in att, att
    rig.restart()
    assert [x["op"] for x in rig.lines()] == ["mint", "bind"]
    assert rig.ask("RUN-TICKET-STATUS", attempt_id="a-1")["state"] == "bound"
    # fix 2 (codex 4 + complement 1): the record is renamed into place but its directory fsync fails (NotDurable):
    # the record landed, so the bind stands (no unbind), the lease controls the session, and root's retry of the
    # same ATTACH answers `session-exists`, never `ticket-bound`; the lease's EOF revokes the session
    from levain.slot import records as R
    two = ticket_of(rig.mint("a-2"))
    c2, lo2 = lease(rig, two)
    real_put = rig.control.store.put

    def landed(rec):
        real_put(rec)
        raise R.NotDurable("the directory fsync failed after the rename")
    monkeypatch.setattr(rig.control.store, "put", landed)
    again = {"lease_nonce": lo2["lease_nonce"], "sudo_uid": DISPATCH_UID, "session": "s-00000000000000a2",
             "claims": d_attach_claims(two)}
    assert root(rig, "ATTACH", **again)["ok"] is False
    monkeypatch.undo()
    assert [x["op"] for x in rig.lines()][-2:] == ["mint", "bind"]
    assert c2.lease.used and c2.lease.session == "s-00000000000000a2"
    assert refused(root(rig, "ATTACH", **again)) == "session-exists"
    rig.control.lease_eof(c2.lease).join(5)
    assert rig.control.store.get("s-00000000000000a2").state == "REVOKED"


def test_attach_after_the_ticket_closed_refused(rig):
    # L1 gap: the ticket is cancelled, or expires, between LEASE-OPEN and ATTACH; ATTACH binds nothing, writes no
    # record
    one = ticket_of(rig.mint())
    _c, lo = lease(rig, one)
    assert rig.ask("RUN-TICKET-CANCEL", ticket=one)["ok"]
    assert refused(root(rig, "ATTACH", lease_nonce=lo["lease_nonce"], sudo_uid=DISPATCH_UID, session=SESSION,
                        claims=d_attach_claims(one))) == "attempt-closed"
    two = ticket_of(rig.mint("a-2"))
    _c2, lo2 = lease(rig, two)
    rig.clock.t += T.TICKET_UNUSED_S
    assert refused(root(rig, "ATTACH", lease_nonce=lo2["lease_nonce"], sudo_uid=DISPATCH_UID, session=SESSION,
                        claims=d_attach_claims(two))) == "attempt-closed"
    assert rig.control.store.get(SESSION) is None
    assert [x["op"] for x in rig.lines()] == ["mint", "cancel", "mint"]   # fix 2: a read-only check journals no expire


# ---- 3c-5 fix 2 (L3 r1): one CONTROL per journal; mint lines checked whole; atomic idempotency; stale acks --------

def test_second_control_refused_while_the_first_holds_the_journal(rig):
    # codex 1: a second CONTROL on the same journal refuses `control-running` at its start, before it compacts the
    # journal, touches a record or unlinks the first one's socket; the first keeps serving; once it stops, the second
    # starts and serves the same tickets
    first = rig.mint()
    journal = (rig.sdir / T.JOURNAL).read_bytes()
    sock = rig.st.L / "control.sock"
    sock.write_bytes(b"the first CONTROL's socket")                     # a stand-in: it must survive the refusal
    tickets = T.TicketStore(rig.sdir / T.JOURNAL, rig.st.L / "policy" / "dispatch_ceiling", clock=rig.clock,
                            ceiling_uid=os.getuid())
    two = C.Control(C.StateStore(rig.sdir), OP_UID, dispatch_uid=DISPATCH_UID, keys_uid=KEYS_UID,
                    routes=RM.ControlRoutes(rig.st.routes), tickets=tickets, clock=rig.clock)
    with pytest.raises(Refused) as e:
        C.ControlServer(two, sock).start()
    assert e.value.name == "control-running"
    assert [x[0] for x in two.seam_errors] == ["start"]                  # noted by name
    assert sock.read_bytes() == b"the first CONTROL's socket"            # never unlinked
    assert (rig.sdir / T.JOURNAL).read_bytes() == journal and not (rig.sdir / (T.JOURNAL + ".new")).exists()
    assert ticket_of(rig.mint("a-2"))                                    # the first still appends to the live journal
    rig.control.stop()
    two.start()
    assert two.handle(C.Conn(DISPATCH_UID), {"v": 1, "op": "RUN-TICKET-MINT", "id": 1, **mint_args()}) == first
    two.stop()


def test_mint_line_with_altered_facts_refuses_the_start(rig):
    # codex 2: a complete canonical mint line whose facts were altered (a larger reservation) under the original
    # request digest is never served: CONTROL's start refuses `journal-inconsistent`
    ticket_of(rig.mint())
    rig.control.stop()
    [line] = rig.lines()
    line["facts"]["reserved_microdollars"] = 4_000_000
    assert line["request_digest"] == T.request_digest(T.check_facts(mint_args()))   # the old digest kept
    (rig.sdir / T.JOURNAL).write_bytes(canon.canonical_bytes(line) + b"\n")
    with pytest.raises(Refused) as e:
        rig.restart()
    assert e.value.name == "journal-inconsistent"


def test_concurrent_same_attempt_mints_both_get_the_first_ticket(rig, monkeypatch):
    # codex 3: two requests for the same new attempt both pass the first replay; the first mints, the ceiling is
    # lowered below the reservation, then the second goes on: it answers the first ticket (and run_id), never a ceiling
    # refusal and never a second mint line
    real = rig.control._await_keys_serving_head
    go, waiting = threading.Event(), threading.Event()
    calls = []

    def await_head():
        calls.append(1)
        if len(calls) == 1:                                              # the second request, held after its replay
            waiting.set()
            assert go.wait(10)
        real()
    monkeypatch.setattr(rig.control, "_await_keys_serving_head", await_head)
    out = []
    th = threading.Thread(target=lambda: out.append(rig.mint()))
    th.start()
    assert waiting.wait(10)
    first = rig.mint()                                                   # the other request mints meanwhile
    ticket_of(first)
    write_ceiling(rig.st.L, per_run=1)                                   # the ceiling lowered between the two
    go.set()
    th.join(10)
    assert out == [first]
    assert [x["op"] for x in rig.lines()] == ["mint"]


class StaleAckShim(KeysShim):
    """keys' admin wire whose RELOAD ack arrives after a newer attachment superseded the one it was sent on."""

    def __init__(self, k, rig) -> None:
        super().__init__(k)
        self.rig, self.supersede = rig, True

    def call(self, op: str, **kw) -> dict:
        ans = super().call(op, **kw)
        if self.supersede:
            self.rig.attach({"generation": kw["generation"], "item_sha256": kw["item_sha256"]})
        return ans

    def abort(self, epoch=None) -> None:
        pass


def test_stale_attachment_ack_deletes_nothing(rig):
    # codex 5: keys lets generation 1 go in its ack, but the ack came on an attachment a newer one superseded while
    # the RELOAD was in flight: CONTROL deletes no file. The next ack on the current attachment prunes as H5 says.
    k = keys_on_wire(rig)
    rig.control.keys_admin = shim = StaleAckShim(k, rig)
    submit_gen(rig, 2, [route(host="api.z.ai")])
    assert shim.keeps[-1] == [2] and served_gens(rig) == [2]              # keys let 1 go
    assert rig.st.gen_files() == ["1.json", "1.sig", "2.json", "2.sig"]   # the stale ack deleted nothing
    shim.supersede = False
    submit_gen(rig, 3, [route()])
    assert served_gens(rig) == [3] and rig.st.gen_files() == ["3.json", "3.sig"]
