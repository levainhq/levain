"""S1 3c-5 session 1: run tickets (CONTRACT §3.3 dispatch row, §3.5 Ceiling + IDEMPOTENT MINT, §13; 3c design r3
§2.1 and row 3c-5; R4 §2 "Keys' generation at attach"). CONTROL's `RUN-TICKET-MINT` / `-CANCEL` / `-STATUS` through
`Control.handle` over a real signed route map (`tests/test_routemaps.py`'s ssh-keygen fixtures) and a real journal
on disk; `test_mint_waits_for_keys_reload_ack` also runs CONTROL through `control.serve` with keys as a child process.

Binding (`LEASE-OPEN {ticket}`, `ATTACH`), `TOKEN-RESOLVE` `run` and keys' run side are session 2's; pool, release and
`no-free-slot` are 3c-7's; verifier generations 3c-8's."""
from __future__ import annotations

import asyncio
import json
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
                                  _crash)

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
                                     clock=self.clock, **kw)
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
    assert ans.get("ok") is True and set(ans) == {"id", "ok", "ticket"}, ans
    return ans["ticket"]


def refused(ans: dict) -> str:
    assert ans.get("ok") is False, ans
    return ans["refused"]


@pytest.fixture
def rig(st):
    return Rig(st, Clock())


# ---- idempotent mint ------------------------------------------------------------------------------------------------

def test_mint_replay_same_attempt_same_ticket(rig):
    one = ticket_of(rig.mint())
    assert ticket_of(rig.mint()) == one                                   # a replay in the same process
    assert rig.tickets.counts() == {"open": 1, "today": 1}
    rig.restart()                                                         # CONTROL restarted between
    assert ticket_of(rig.mint()) == one
    assert rig.tickets.counts() == {"open": 1, "today": 1}                # the open and day counts +1 once
    assert [x["op"] for x in rig.lines()] == ["mint"]


def test_mint_attempt_conflict_refused(rig):
    one = ticket_of(rig.mint())
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
    one = ticket_of(rig.mint())
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
    assert [x["op"] for x in rig.lines()] == ["mint", "tombstone"]
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
    (rig.st.L / "policy" / "dispatch_ceiling").unlink()
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
    one = ticket_of(rig.mint())
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
