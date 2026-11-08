"""PRE-W: the production wiring of F9a's supervisor readers and the default-off brokered setting.

* follow_run's ``ticket_for`` and ``routemap`` come from CONTROL's OWN records over its socket (``RUN-TICKET-FACTS``,
  ``ROUTEMAP-ACTIVE``, :class:`levain.dispatch.follow_run.SupervisorFacts`), never from anything ``resolve`` carries;
* both are read BEFORE any store transaction opens (head ruling, levain 72), and a reader that raises or a CONTROL that
  cannot be reached is UNRESOLVED, never raised into the follower;
* a PASSTHROUGH executor ticket's run records (``passthrough:<vendor>``) and the landing contrast escalates it, instead
  of failing every passthrough verifier run closed;
* ``L/policy/dispatch_auth``: root's file, absent = OFF; only exactly ``brokered`` from root turns it ON."""
from __future__ import annotations

import os
import sqlite3

import pytest

from levain.dispatch import follow_run as FR
from levain.dispatch import jobs as J
from levain.slot import Refused
from levain.slot.services import dispatch_run as DR
from tests.test_dispatch_backend_identity import World, _map, on, says
from tests.test_routemaps import st as signed_store  # noqa: F401 - the signed store fixture (renamed: no F811 on the parameter)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def _answer(m):
    """``ROUTEMAP-ACTIVE``'s answer for the Checked map ``m``."""
    return {"generation": m.generation, "routes": {rid: {"backends": dict(e.backends)} for rid, e in m.routes.items()},
            "digests": dict(m.digests)}


class Port:
    """A CONTROL port: ``RUN-TICKET-FACTS`` answers from ``tickets`` (keyed by (job_id, run_index)), ``ROUTEMAP-ACTIVE``
    from ``m``; ``fail`` is raised for every call when set."""

    def __init__(self, tickets=None, m=None, fail=None):
        self.tickets, self.m, self.fail, self.calls = tickets or {}, m, fail, []

    def call(self, op, **fields):
        self.calls.append((op, fields))
        if self.fail is not None:
            raise self.fail
        if op == "RUN-TICKET-FACTS":
            key = (fields["job_id"], fields["run_index"])
            if key not in self.tickets:
                raise Refused("ticket-unknown")
            return {"facts": self.tickets[key]}
        if op == "ROUTEMAP-ACTIVE":
            if self.m is None:
                raise Refused("no-routemap")
            return _answer(self.m)
        raise Refused("op-not-allowed")


def _verify(world, job, runs, sup):
    return world.verify_routed(job.id, runs, routemap=sup.routemap, ticket_for=sup.ticket_for)


# --- (ii) ON: the supervisor's ticket and route resolve the identity ------------------------------------------------

def test_the_supervisors_ticket_and_active_map_resolve_backend_identity(world):
    job, _ = world.file()
    tickets = {(job.id, 0): on("rt-a")(job.id, 0), (job.id, 1): on("rt-b")(job.id, 1)}
    port = Port(tickets, _map())
    job = _verify(world, job, [(None, says("x")), (None, says("y"))], FR.SupervisorFacts(port))
    recs = world.jstore.run_records(job.id)
    assert job.state == "verified"
    assert [r.model["backend_identity"] for r in recs] == ["backend-alpha", "backend-gamma"]
    assert {r.model["identity_source"] for r in recs} == {J.IDENTITY_SOURCE}
    assert [c[0] for c in port.calls] == ["RUN-TICKET-FACTS", "ROUTEMAP-ACTIVE"] * 2
    assert world.contrast(job) is None


# --- (iii) a resolve-supplied label never wins ----------------------------------------------------------------------

def test_a_callers_backend_label_never_wins_over_the_supervisors_route(world):
    job, _ = world.file()
    tickets = {(job.id, i): on("rt-a")(job.id, i) for i in (0, 1)}
    job = _verify(world, job, [(None, says("backend-forged-1")), (None, says("backend-forged-2"))],
                  FR.SupervisorFacts(Port(tickets, _map())))
    recs = world.jstore.run_records(job.id)
    assert [r.model["backend_identity"] for r in recs] == ["backend-alpha"] * 2
    assert [r.model["answered"] for r in recs] == ["backend-forged-1", "backend-forged-2"]
    assert "same backend identity" in world.contrast(job)


def test_a_map_that_moved_since_the_mint_is_unresolved(world):
    job, _ = world.file()
    port = Port({(job.id, 0): on("rt-a")(job.id, 0)}, _map(generation=8))   # tickets minted at 7
    job = _verify(world, job, [(None, says("x"))], FR.SupervisorFacts(port))
    assert (job.state, job.history[-1].evidence) == ("failed", J.BACKEND_UNRESOLVED)


# --- (iv) unreachable or raising: unresolved, nothing raised, no lock held -------------------------------------------

@pytest.mark.parametrize("fail", [OSError("connection refused"), Refused("control-down"), RuntimeError("bad frame")])
def test_an_unreachable_supervisor_is_unresolved_and_raises_nothing(world, fail):
    job, _ = world.file()
    job = _verify(world, job, [(None, says("x"))], FR.SupervisorFacts(Port(fail=fail)))
    assert (job.state, job.history[-1].cause, job.history[-1].evidence) == (
        "failed", "verifier_refused", J.BACKEND_UNRESOLVED)
    assert world.jstore.run_records(job.id) == []


def test_a_ticket_for_that_raises_is_unresolved_never_raised_and_leaves_the_store_writable(world):
    """levain 71's open edge, pinned: a raising ticket_for used to propagate with no record written."""
    def boom(job_id, run):
        raise RuntimeError("the supervisor's reader broke")

    job, _ = world.file()
    job = world.verify_routed(job.id, [(None, says("x"))], routemap=lambda gen: _map(), ticket_for=boom)
    assert (job.state, job.history[-1].evidence) == ("failed", J.BACKEND_UNRESOLVED)
    other, _ = world.file()                       # the store is not poisoned: another job files and moves
    assert world.jstore.get(other.id).state == other.state


def test_the_supervisor_is_read_with_no_store_write_lock_held(world):
    """Head ruling (levain 72): ticket_for and routemap run before any store transaction opens. Each reader takes the
    store's write lock itself (BEGIN IMMEDIATE, no wait): held by the recording call, it would fail at once."""
    db = str(world.jstore.path)
    took = []

    def lock_free(tag):
        conn = sqlite3.connect(db, timeout=0, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
            took.append(tag)
        finally:
            conn.close()

    job, _ = world.file()
    ticket = on("rt-a")(job.id, 0)

    def ticket_for(jid, run):
        lock_free("ticket_for")
        return ticket

    def routemap(gen):
        lock_free("routemap")
        return _map()

    job = world.verify_routed(job.id, [(None, says("x"))], routemap=routemap, ticket_for=ticket_for)
    assert took == ["ticket_for", "routemap"]
    assert world.jstore.run_records(job.id)[0].model["backend_identity"] == "backend-alpha"


# --- (i) passthrough: records as on main (state), provenance from the supervisor, the contrast escalates -----------

def _pt(jid, run, **over):
    t = {"kind": "executor", "job_id": jid, "run_index": run, "auth": "passthrough", "vendor": "claude",
         "model_requested": "glm-5", "max_calls": 20, "max_tunnel_bytes": 5_000_000, "deadline_s": 3600}
    t.update(over)
    return t


def test_a_passthrough_verifier_run_records_and_the_contrast_escalates(world):
    """The hazard: with PRE-C, every passthrough run failed closed. Main 434a9f5e recorded both runs and the job went
    verified; it still does, with the identity read from the supervisor's ticket, and the landing escalates."""
    job, _ = world.file()
    job = world.verify_routed(job.id, [(_pt, says("label-a")), (_pt, says("label-b"))], routemap=None)
    assert job.state == "verified"
    recs = world.jstore.run_records(job.id)
    assert [r.model["backend_identity"] for r in recs] == ["passthrough:claude"] * 2
    assert {r.model["identity_source"] for r in recs} == {J.PASSTHROUGH_SOURCE}
    assert [r.model["answered"] for r in recs] == ["label-a", "label-b"]
    assert "route map" in world.contrast(job)


@pytest.mark.parametrize("over", [{"job_id": "dj-other"}, {"run_index": 5}, {"kind": "planner"}, {"vendor": ""},
                                  {"model_requested": "other-model"}])
def test_a_passthrough_ticket_that_is_not_this_runs_is_unresolved(world, over):
    job, _ = world.file()
    job = world.verify_routed(job.id, [(lambda j, r: _pt(j, r, **over), says("x"))], routemap=None)
    assert (job.state, job.history[-1].evidence) == ("failed", J.BACKEND_UNRESOLVED)


# --- the CONTROL verbs (real handler) -------------------------------------------------------------------------------

@pytest.fixture
def rig(request):
    from tests.test_tickets import Clock, Rig
    return Rig(request.getfixturevalue("signed_store"), Clock())


def _bind(rig, ticket, session="s-" + "a" * 16):
    rig.tickets.bind(ticket, session)


def test_run_ticket_facts_answers_the_one_executor_ticket_that_ran(rig):
    from tests.test_tickets import ticket_of
    one = ticket_of(rig.mint("a-1"))
    assert rig.ask("RUN-TICKET-FACTS", job_id="job-1", run_index=0)["refused"] == "ticket-unknown"   # never bound
    _bind(rig, one)
    ans = rig.ask("RUN-TICKET-FACTS", job_id="job-1", run_index=0)
    assert ans["ok"] is True and ans["facts"]["route"] == "glm" and ans["facts"]["routemap_generation"] == 1
    assert ans["facts"]["job_id"] == "job-1" and ans["facts"]["run_index"] == 0 and ans["facts"]["kind"] == "executor"
    assert rig.ask("RUN-TICKET-FACTS", job_id="job-1", run_index=1)["refused"] == "ticket-unknown"
    assert rig.ask("RUN-TICKET-FACTS", job_id="job-2", run_index=0)["refused"] == "ticket-unknown"


def test_run_ticket_facts_with_two_tickets_that_ran_is_ambiguous_never_chosen(rig):
    from tests.test_tickets import ticket_of
    _bind(rig, ticket_of(rig.mint("a-1")), "s-" + "a" * 16)
    _bind(rig, ticket_of(rig.mint("a-2")), "s-" + "b" * 16)
    assert rig.ask("RUN-TICKET-FACTS", job_id="job-1", run_index=0)["refused"] == "ticket-ambiguous"


def test_run_ticket_facts_refuses_bad_values_and_other_peers(rig):
    from tests.test_routemaps import OP_UID
    from levain.slot import control as C
    assert rig.ask("RUN-TICKET-FACTS", job_id="has space", run_index=0)["refused"] == "ticket-field:job_id"
    assert rig.ask("RUN-TICKET-FACTS", job_id="job-1", run_index=-1)["refused"] == "ticket-field:run_index"
    assert rig.ask("RUN-TICKET-FACTS", job_id="job-1")["ok"] is False
    ans = rig.control.handle(C.Conn(OP_UID), {"v": 1, "op": "RUN-TICKET-FACTS", "id": 1, "job_id": "job-1",
                                              "run_index": 0})
    assert ans["refused"] == "verb-not-allowed-for-peer"


def test_routemap_active_answers_the_active_generation(rig):
    from levain.slot import routemaps as RM
    from tests.test_tickets import GLM_DIGEST
    ans = rig.ask("ROUTEMAP-ACTIVE")
    assert ans["ok"] is True and ans["generation"] == 1
    assert ans["routes"] == {"glm": {"backends": {"z-ai/glm-5.2": "zhipu/glm-5.2"}}}
    assert ans["digests"] == {"glm": GLM_DIGEST} and RM.policy_digest is not None


def test_routemap_active_does_not_wait_on_a_held_activation_lock(rig):
    """L2 LOW-1: ROUTE-SUBMIT holds the activation lock across its keys RELOAD; a verification's read overlapping it
    must not queue behind it (a CONTROL timeout there fails the verified job terminally). The read takes one reference."""
    import threading
    held, release, answers = threading.Event(), threading.Event(), []

    def hold():
        with rig.control._activation:
            held.set()
            release.wait(10)
    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert held.wait(5)
    asker = threading.Thread(target=lambda: answers.append(rig.ask("ROUTEMAP-ACTIVE")), daemon=True)
    asker.start()
    asker.join(2.0)
    waited = asker.is_alive()
    release.set()
    holder.join(5)
    asker.join(5)
    assert not waited, "ROUTEMAP-ACTIVE queued behind the held activation lock"
    assert answers and answers[0]["ok"] is True and answers[0]["generation"] == 1


def test_the_supervisor_readers_over_the_real_handler_resolve_a_bound_ticket(rig):
    """The whole production path but the socket: SupervisorFacts asks the real CONTROL handler as the dispatch peer."""
    from tests.test_tickets import ticket_of

    class ToControl:
        def call(self, op, **fields):
            ans = rig.ask(op, **fields)
            if ans.get("ok") is not True:
                raise Refused(ans["refused"], op)
            return {k: v for k, v in ans.items() if k not in ("id", "ok")}

    _bind(rig, ticket_of(rig.mint("a-1")))
    sup = FR.SupervisorFacts(ToControl())
    ticket = sup.ticket_for("job-1", 0)
    assert J.resolve_backend_identity(sup.routemap(ticket["routemap_generation"]), ticket) == "zhipu/glm-5.2"
    assert sup.routemap(2) is None                                  # another generation: the map moved
    assert sup.ticket_for("job-1", 1) is None                       # refused ticket-unknown: None, never raised


def test_follow_run_wires_the_supervisor_readers_on_its_socket_by_default():
    import inspect
    src = inspect.getsource(FR.main)
    assert 'supervisor: Any = "socket"' in src and "supervisor_facts(a.socket)" in src
    assert FR.SUPERVISOR_OPS == frozenset({"RUN-TICKET-FACTS", "ROUTEMAP-ACTIVE"})


# --- the default-off setting ----------------------------------------------------------------------------------------

ME = os.getuid()


def test_an_absent_setting_is_off(tmp_path):
    assert DR.read_dispatch_auth(tmp_path / "dispatch_auth", owner=ME) == ("passthrough", None)


def test_only_exactly_brokered_from_the_owner_is_on(tmp_path):
    p = tmp_path / "dispatch_auth"
    p.write_bytes(DR.DISPATCH_AUTH_ON)
    p.chmod(0o644)
    assert DR.read_dispatch_auth(p, owner=ME) == ("brokered", None)
    assert DR.read_dispatch_auth(p, owner=ME + 1)[0] == "passthrough"          # another owner
    p.chmod(0o666)
    assert DR.read_dispatch_auth(p, owner=ME)[0] == "passthrough"              # others can write it
    p.chmod(0o644)
    for content in (b"brokered", b"BROKERED\n", b"brokered\n\n", b"passthrough\n", b""):
        p.write_bytes(content)
        auth, why = DR.read_dispatch_auth(p, owner=ME)
        assert auth == "passthrough" and why is not None, content
    link = tmp_path / "link"
    p.write_bytes(DR.DISPATCH_AUTH_ON)
    os.symlink(p, link)
    assert DR.read_dispatch_auth(link, owner=ME)[0] == "passthrough"           # O_NOFOLLOW
    d = tmp_path / "dir"
    d.mkdir()
    assert DR.read_dispatch_auth(d, owner=ME)[0] == "passthrough"


def test_off_builds_the_passthrough_server_alone(tmp_path):
    """OFF (the default) is today's server: one passthrough launcher, no brokered sibling, no spend reader."""
    srv = DR.build_server("linux", tmp_path, tmp_path / "c.sock", ME)
    assert srv.brokered is None and srv.launcher.auth == "passthrough" and srv.launcher.spend is None
    assert srv._replica is not None


def test_on_builds_a_separate_brokered_launcher_sharing_the_replica_index_and_uploads(tmp_path):
    srv = DR.build_server("linux", tmp_path, tmp_path / "c.sock", ME, brokered=True)
    b = srv.brokered
    assert b is not None and b is not srv and b.brokered is None
    assert b.launcher is not srv.launcher and b.launcher.auth == "brokered" and srv.launcher.auth == "passthrough"
    assert b.launcher.index is srv.launcher.index and b.launcher.runtime is srv.launcher.runtime
    assert b._replica is srv._replica and b._replica is not None and b._uploads is srv._uploads
    assert b.launcher.spend is not None and b.launcher.spend("run-x") is None      # no completeness proof: the whole share


def test_main_reads_the_setting_before_it_builds_and_binds_the_brokered_socket_only_when_on():
    import inspect
    src = inspect.getsource(DR.main)
    assert src.index("read_dispatch_auth(") < src.index("build_server(")
    assert 'brokered=auth == "brokered"' in src and "if server.brokered is not None:" in src
    assert src.count("_bind(") == 2 and "BROKERED_SOCKET_NAME" in src




# --- the two run servers share ONE holder registry (L2 HIGH-1, MED-1, MED-2; L1 W1) ---------------------------------------
#
# Passthrough (run.sock) and brokered (run-brokered.sock) share one SessionIndex and one replica, so a key's live holder,
# its END job, its stripe and the SYNC re-judge must be shared too: a revoke SYNCed on either socket ENDs a session of
# either, an END on either marks the real holder ending, and USAGE never reads a brokered record as a passthrough $0.

class Pair:
    """Both servers of one process over socketpairs, built as ``build_server`` builds them (one index, one replica, one
    holder registry), with the S4 fakes behind each launcher. ``shared=False`` gives each server its own registry: the
    shape before the fix, which each test below must fail against."""

    def __init__(self, rig, tmp_path, *, shared=True):
        from tests.test_agents_authority_replica import _replica
        from tests.test_agents_session_launcher import FakeControl, FakeRuntime, FakeSources
        from levain.agents import session_launcher as L
        self.rig = rig
        self.replica = _replica(tmp_path, "srv")
        self.control, self.runtime = FakeControl(), FakeRuntime()
        self.control.script["ROUTE-RESOLVE"] = lambda f: {"route": "glm", "routemap_generation": 1,
                                                          "route_policy_digest": "sha256:" + "a" * 64}
        index = L.SessionIndex(tmp_path / "idx")
        pt = L.RealLauncher(self.control, self.runtime, FakeSources(), index)
        br = L.RealLauncher(self.control, self.runtime, FakeSources(), index, auth="brokered", spend=lambda run: 4242)
        kw = {"pool_free": lambda: True, "peer_ok": lambda s: True, "holder_idle_s": 20, "replica": self.replica}
        reg = {"registry": DR.HolderRegistry()} if shared else {}
        self.pt = DR.RunServer(pt, verify=DR.replica_verifier(self.replica, now=rig.now), **kw, **reg)
        self.br = DR.RunServer(br, verify=DR.replica_verifier(self.replica, now=rig.now, billing="api_key"), **kw, **reg)
        self.index = index
        self.threads, self.n = [], 0

    def conn(self, server):
        import socket
        import threading
        a, b = socket.socketpair()
        a.settimeout(20)
        t = threading.Thread(target=server.handle, args=(b,), daemon=True)
        t.start()
        self.threads.append(t)
        return a

    def call(self, sock, op, **fields):
        from levain.agents import session_client as C
        self.n += 1
        sock.sendall(C.encode_run_frame({"p": C.PROTOCOL, "op": op, "id": self.n, **fields}))
        return C.read_run_frame(sock)

    def head(self):
        src = self.rig.authority.replay()
        return {"seq": src.seq, "hash": src.head_hash}

    def sync(self, server):
        import dataclasses
        from tests.test_agents_authority_replica import _source
        sock = self.conn(server)
        try:
            rows = [r for r in _source(self.rig) if r.seq > self.replica.head()[0]]
            return self.call(sock, "SYNC", rows=[dataclasses.asdict(r) for r in rows])
        finally:
            sock.close()

    def launch_brokered(self, sock, oid, key):
        from levain.agents import session as S
        from levain.agents import session_client as C
        proof = C.link_proof(self.rig.store, self.rig.world.root, oid, now=self.rig.now())
        figure = self.rig.authority.replay().policy.grants[proof.link_id]["budget_run_usd_micros"]
        launch = S.SessionLaunch(oid, key, "claude", "sonnet", "b", "brokered", None, None, figure)
        return self.call(sock, "LAUNCH", **C.launch_to_wire(launch, proof), ledger_head=self.head(),
                         bar={"open": False, "closed_revoke_seq": 0})

    def state(self, key):
        return (self.index.get(key) or {}).get("state")

    def close(self):
        for t in self.threads:
            t.join(10)


@pytest.fixture
def lrig(tmp_path):
    import shutil
    if shutil.which("ssh-keygen") is None:
        pytest.skip("needs ssh-keygen")
    from tests.test_agents_launch import Rig
    return Rig(tmp_path)


@pytest.fixture
def pair(lrig, tmp_path):
    p = Pair(lrig, tmp_path)
    yield p
    p.close()


KEY_B = "s-00000000000000c1"


def _until(pred, seconds=10.0):
    import time
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _brokered_live(pair, *, release=True):
    from tests.test_agents_session_launch import _occurrence
    oid = _occurrence(pair.rig, billing="api_key", micros=1_000_000)
    assert pair.sync(pair.br)["ok"] is True
    sock = pair.conn(pair.br)
    ans = pair.launch_brokered(sock, oid, KEY_B)
    assert ans["ok"] is True, ans
    if release:
        rel = pair.call(sock, "RELEASE", ledger_head=pair.head(), bar={"open": False, "closed_revoke_seq": 0})
        assert rel["ok"] is True, rel
    return oid, sock


def test_a_revoke_synced_on_the_passthrough_socket_ends_a_brokered_session(pair):
    """L2 HIGH-1: the replica is one authority log; the re-judge must cover every live holder of the pair, each judged
    by its own server's verify (the brokered one by the api_key verifier)."""
    from tests.test_authority_agent_link import revoke_agent
    oid, sock = _brokered_live(pair)
    assert pair.runtime.ended == []
    pair.rig.world.link(revoke_agent(pair.rig.row(oid)["agent_id"], pair.rig.sid), "laptop")
    assert pair.sync(pair.pt)["ok"] is True
    assert _until(lambda: pair.state(KEY_B) == "ended"), "a revoke SYNCed on run.sock left the brokered session running"
    assert pair.runtime.ended == ["sess-1"]
    sock.close()


def test_an_end_on_the_passthrough_socket_marks_the_brokered_holder_ending_and_its_release_refuses(pair):
    """L2 MED-2: an END on run.sock for a key the brokered server holds marks that holder ``ending`` before its job
    starts, so a RELEASE racing on the brokered socket refuses instead of starting the child."""
    _oid, sock = _brokered_live(pair, release=False)
    end_sock = pair.conn(pair.pt)
    got = pair.call(end_sock, "END", session_key=KEY_B, deadline_ms=5000)
    end_sock.close()
    assert got["ok"] is True and got["terminated"] is True, got
    rel = pair.call(sock, "RELEASE", ledger_head=pair.head(), bar={"open": False, "closed_revoke_seq": 0})
    assert (rel["ok"], rel.get("refused")) == (False, "session-ended"), rel
    assert pair.runtime.children[0].released is False
    sock.close()


def test_usage_of_a_brokered_key_asked_on_the_passthrough_socket_is_the_caps_never_zero(pair):
    """L2 MED-1 = L1 W1: the record carries the auth it was launched under; the passthrough launcher reading a brokered
    record answers the fail-closed caps by name, not a zero-dollar settle. Its own socket reads its spend."""
    _oid, sock = _brokered_live(pair)
    end_sock = pair.conn(pair.br)
    assert pair.call(end_sock, "END", session_key=KEY_B, deadline_ms=5000)["terminated"] is True
    end_sock.close()
    assert pair.index.get(KEY_B)["auth"] == "brokered"
    ask = pair.conn(pair.pt)
    cross = pair.call(ask, "USAGE", session_key=KEY_B)["usage"]
    ask.close()
    assert cross["usd_micros"] is None and cross["source"] == "session:auth-mismatch", cross
    ask = pair.conn(pair.br)
    own = pair.call(ask, "USAGE", session_key=KEY_B)["usage"]
    ask.close()
    assert (own["usd_micros"], own["source"]) == (4242, "session:brokered"), own
    sock.close()

