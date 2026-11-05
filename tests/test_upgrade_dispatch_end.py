"""C1 (1104-31): does an operator ``REVOKE-OWN`` of a DISPATCH session end it through the run server?

RELEASE_NOTES_S10_LINES said: "a dispatch (agent) session's lease is closed only by the run server's own end of that
session, and whether an external revoke leads it there is not proven." This file is the in-process observation.

What is REAL here: CONTROL (``Control.handle`` over the tickets rig: a real ticket, a real ticket-bound LEASE-OPEN as the
dispatch uid, root's ATTACH, ``REVOKE-OWN`` as the operator, root's END edges as CAS, the lease's EOF), the run server
(:class:`levain.slot.services.dispatch_run.RunServer`) driven by the REAL operator-side client
(:mod:`levain.agents.session_client`) over a socketpair, the REAL ``RealLauncher`` and the REAL
:class:`levain.slot.dispatch_runtime.HelperSlotRuntime`, which holds the lease and closes it only on a proven END. The
helper is a scripted subprocess (the S10c pattern), its ``start`` under the real ``GatedChild`` gate.

What is a stand-in, and why: root's prepare ATTACHes the session in production; here the lease wrapper does root's
ATTACH right after LEASE-OPEN (no root process in a test). The launcher mints through the run-server tests'
``FakeControl``; the wrapper mints the REAL ticket the lease is bound to (one ticket per lease either way). The root
reaper's END is its effect on the two things the run server can see: CONTROL's END edges (REVOKED -> FROZEN -> DEAD ->
CREDENTIAL_PARKED -> RETAINED, sent as root) and the slot's processes gone (the helper's ``start`` returns, as root's
kill makes it). Root's END internals are the reaper's own tests.

NOT held here (the VM's, the V rows): two uids over real AF_UNIX, a real root reaper killing a real Claude process, and
whether the controller in a real fire is WAITing at that moment (here a thread WAITs, as the controller's ``wait`` does).
"""
from __future__ import annotations

import json
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

from levain import upgrade as U
from levain.agents import session as S
from levain.slot import control as C
from levain.slot import dispatch_runtime as DR
from tests.test_agents_launch import Rig
from tests.test_agents_session_launch import EXP, _occurrence
from tests.test_routemaps import DISPATCH_UID, OP_UID, st  # noqa: F401 - st is a fixture
from tests.test_slot_dispatch_run_server import Env
from tests.test_slot_dispatch_runtime import Helper
from tests.test_tickets import Clock, Rig as TicketRig, d_attach_claims, ticket_of

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

SESSION = "s-00000000000000a1"          # the slot session the scripted prepare answers (tests.test_slot_dispatch_runtime)
KEY = "s-00000000000000e1"              # the launch's session key (the run server's holder key)

#: the S10c scripted helper, with a ``start`` that runs until root's END kills the slot's processes (the ``.killed``
#: file), as a real vendor under ``start`` does; a ``start`` never killed gives up after 20 s (a hung test, not a pass).
BLOCKING_HELPER = '''\
import json, os, sys, time
here = sys.argv[0]
cfg = json.load(open(here + ".cfg"))
verb = sys.argv[1]
stdin = sys.stdin.read() if verb in ("prepare", "render") else ""
with open(here + ".log", "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:]}) + "\\n")
if verb == "start":
    end = time.monotonic() + 20
    while time.monotonic() < end and not os.path.exists(here + ".killed"):
        time.sleep(0.02)
    sys.exit(137 if os.path.exists(here + ".killed") else 3)
sys.stdout.write(cfg[verb]["line"] + "\\n")
sys.exit(cfg[verb].get("rc", 0))
'''


class RootSideLease:
    """The runtime's lease connection into the REAL CONTROL as the dispatch uid, one ``Conn`` for its life. Its
    LEASE-OPEN opens a real ticket-bound lease and then does root's ATTACH (prepare's, in production) and the RENDERING
    edge, so CONTROL holds a live dispatch record of the operator bound to this lease. ``close`` is the lease's EOF."""

    def __init__(self, trig: TicketRig) -> None:
        self.trig, self.conn, self.closed, self.ticket = trig, C.Conn(DISPATCH_UID), 0, None

    def call(self, op, **_fields):
        assert op == "LEASE-OPEN"
        self.ticket = ticket_of(self.trig.mint())
        lo = self.trig.control.handle(self.conn, {"v": 1, "op": "LEASE-OPEN", "id": 1, "ticket": self.ticket})
        assert lo["ok"], lo
        root = C.Conn(0)
        att = self.trig.control.handle(root, {"v": 1, "op": "ATTACH", "id": 2, "lease_nonce": lo["lease_nonce"],
                                              "sudo_uid": DISPATCH_UID, "session": SESSION,
                                              "claims": d_attach_claims(self.ticket)})
        assert att["ok"], att
        ren = self.trig.control.handle(root, {"v": 1, "op": "CAS", "id": 3, "session": SESSION, "generation": 0,
                                              "from": "ALLOCATED", "to": "RENDERING", "fields": {}})
        assert ren["ok"], ren
        return {"lease_nonce": lo["lease_nonce"]}

    def close(self):
        self.closed += 1
        if self.conn.lease is not None:
            self.trig.control.lease_eof(self.conn.lease)


def _status(trig) -> list:
    ans = trig.control.handle(C.Conn(OP_UID), {"v": 1, "op": "STATUS", "id": 9})
    assert ans["ok"], ans
    return ans["records"]


def _record(trig) -> dict:
    (rec,) = [r for r in _status(trig) if r["session"] == SESSION]
    return rec


def _root_end(trig, killed: Path) -> None:
    """The root reaper's END of a REVOKED session, as the run server and CONTROL see it: the slot's processes die (the
    helper's ``start`` returns), then root's END edges land in CONTROL. The lease is not touched: only its holder
    closes it."""
    killed.write_text("x")
    root = C.Conn(0)
    for frm, to in (("REVOKED", "FROZEN"), ("FROZEN", "DEAD"), ("DEAD", "CREDENTIAL_PARKED"),
                    ("CREDENTIAL_PARKED", "RETAINED")):
        gen = _record(trig)["generation"]
        ans = trig.control.handle(root, {"v": 1, "op": "CAS", "id": 4, "session": SESSION, "generation": gen,
                                         "from": frm, "to": to, "fields": {}})
        assert ans["ok"], (frm, to, ans)


@pytest.fixture
def world(st, tmp_path):  # noqa: F811
    (tmp_path / "auth").mkdir()
    rig = Rig(tmp_path / "auth")
    trig = TicketRig(st, Clock())
    h = Helper(tmp_path, tmp_path / "store")
    h.script.write_text(BLOCKING_HELPER)
    leases: list[RootSideLease] = []

    def factory(_path):
        leases.append(RootSideLease(trig))
        return leases[-1]

    runtime = DR.HelperSlotRuntime(host="darwin", helper=h.prefix, store=tmp_path / "store", routes=("pass-1",),
                                   client_factory=factory, kill_grace_s=0.5)
    env = Env(rig, tmp_path, runtime=runtime)
    yield rig, trig, h, leases, env
    Path(str(h.script) + ".killed").write_text("x")      # no helper outlives the test
    trig.control.stop()


def test_an_operator_revoke_own_of_a_dispatch_session_ends_it_through_the_run_server(world):
    """The premise, observed: REVOKE-OWN -> root's END -> the slot's processes gone -> the holder's WAIT reads the
    exit, which makes the run server's own END (``_SlotProc`` settles through ``end_once`` -> the runtime's teardown)
    -> proven -> the runtime closes its lease -> CONTROL's record is RETAINED with ``lease_live`` false, and the
    upgrade's own predicate sees nothing still ending."""
    rig, trig, h, leases, env = world
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    child = env.client().spawn(S.SessionLaunch(oid, KEY, "claude", "sonnet", "b", "passthrough", EXP))
    child.release()
    rc: list = []
    waiter = threading.Thread(target=lambda: rc.append(child.process.wait()), daemon=True)   # the controller's wait
    waiter.start()

    (lease,) = leases
    rec = _record(trig)
    assert rec["state"] == "RENDERING" and rec["lease_live"] is True and rec["claims"]["kind"] == "dispatch"
    assert [label for label, _r in U._ending(_status(trig))] == [SESSION]          # the upgrade would wait for it

    ans = trig.control.handle(C.Conn(OP_UID), {"v": 1, "op": "REVOKE-OWN", "id": 5, "session": SESSION})
    assert ans["ok"], ans
    rec = _record(trig)
    assert rec["state"] == "REVOKED" and rec["lease_live"] is True                 # CONTROL never closes the lease
    waiter.join(0.5)
    assert waiter.is_alive() and lease.closed == 0          # the revoke alone ends nothing: root's END is what does
    assert [label for label, _r in U._ending(_status(trig))] == [SESSION]

    _root_end(trig, Path(str(h.script) + ".killed"))
    assert _record(trig)["state"] == "RETAINED"

    waiter.join(15)
    assert not waiter.is_alive(), "the holder's WAIT never returned: nothing ended the session on the run server"
    trig.control.join_eof(5)

    # (a) the dispatch lease: closed by its holder (the runtime) on its own proof, exactly once
    assert lease.closed == 1 and lease.conn.lease is not None and not lease.conn.lease.live
    rec = _record(trig)
    # (c) the work kept
    assert rec["state"] == "RETAINED" and rec["lease_live"] is False
    assert U._ending(_status(trig)) == []                                          # the upgrade's watch is done
    # (b) the run server's holder: the END proved, recorded ``ended``; one teardown
    assert env.launcher.index.get(KEY)["state"] == "ended"
    assert h.verbs().count("teardown") == 1
    assert rc and rc[0] == 137                                                     # the slot's exit, proven
    child._conn.close()                                                            # the fire ends: EOF on the holder
    env.drain()
    assert KEY not in env.server._live and h.verbs().count("teardown") == 1        # the EOF END joins the proof


# --- levain 34, D2: a SYNCed revoke closes the lease on the run server's own judgement ---------------------------------------
#
# The case above is the CONTROL path. This one is the authority-log path (STATUS_1_0 row 16): the operator signs a revoke,
# a fire SYNCs it into the dispatch replica, and the run server's SYNC re-judge ENDs the running session, with no tick and
# no root END. Same rig as above (real CONTROL lease, scripted helper, ``HelperSlotRuntime``), the production replica-backed
# server and raw frames. NOT held: two uids over real AF_UNIX, a real root END of a real vendor process.

@pytest.fixture
def replica_world(st, tmp_path):  # noqa: F811
    from tests.test_agents_authority_replica import Wire
    (tmp_path / "auth").mkdir()
    rig = Rig(tmp_path / "auth")
    trig = TicketRig(st, Clock())
    h = Helper(tmp_path, tmp_path / "store")
    h.script.write_text(BLOCKING_HELPER)
    leases: list[RootSideLease] = []

    def factory(_path):
        leases.append(RootSideLease(trig))
        return leases[-1]

    runtime = DR.HelperSlotRuntime(host="darwin", helper=h.prefix, store=tmp_path / "store", routes=("pass-1",),
                                   client_factory=factory, kill_grace_s=0.5)
    (tmp_path / "wire").mkdir()
    wire = Wire(rig, tmp_path / "wire", runtime=runtime)
    yield rig, trig, h, leases, wire
    Path(str(h.script) + ".killed").write_text("x")
    trig.control.stop()
    wire.close()


def test_a_revoke_synced_into_the_replica_closes_the_dispatch_lease_once(replica_world):
    from tests.test_agents_authority_replica import NO_BAR, _source
    from tests.test_authority_agent_link import revoke_agent
    rig, trig, h, leases, wire = replica_world
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    assert wire.sync()["ok"] is True
    sock = wire.conn()
    assert wire.launch(sock, oid, key=KEY)["ok"] is True
    assert wire.call(sock, "RELEASE", ledger_head=wire.head(), bar=NO_BAR)["ok"] is True
    (lease,) = leases
    assert _record(trig)["lease_live"] is True and lease.closed == 0 and h.verbs().count("teardown") == 0

    rig.world.link(revoke_agent(rig.row(oid)["agent_id"], rig.sid), "laptop")      # the operator revokes the agent
    assert wire.sync()["ok"] is True                                                # another fire carries it in
    for _ in range(500):                                                            # the worker ENDs it; no tick, no root
        if lease.closed == 1 and wire.launcher_index(KEY).get("state") == "ended":
            break
        time.sleep(0.02)
    trig.control.join_eof(5)
    assert lease.closed == 1 and not lease.conn.lease.live and _record(trig)["lease_live"] is False
    assert wire.launcher_index(KEY)["state"] == "ended" and h.verbs().count("teardown") == 1

    before = _source(rig)
    assert wire.sync(before[-2:])["refused"] == "ledger-stale"                      # a restore to before the revoke
    assert wire.sync([])["ok"] is True                                              # and a head read: nothing runs twice
    sock.close()
    wire.close()
    assert lease.closed == 1 and h.verbs().count("teardown") == 1
