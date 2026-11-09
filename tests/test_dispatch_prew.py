"""PRE-W: the production wiring of F9a's supervisor readers and the brokered setting (ON unless it opts out).

* follow_run's ``ticket_for`` and ``routemap`` come from CONTROL's OWN records over its socket (``RUN-TICKET-FACTS``,
  ``ROUTEMAP-ACTIVE``, :class:`levain.dispatch.follow_run.SupervisorFacts`), never from anything ``resolve`` carries;
* both are read BEFORE any store transaction opens (head ruling, levain 72), and a reader that raises or a CONTROL that
  cannot be reached is UNRESOLVED, never raised into the follower;
* a PASSTHROUGH executor ticket's run records (``passthrough:<vendor>``) and the landing contrast escalates it, instead
  of failing every passthrough verifier run closed;
* ``L/policy/dispatch_auth``: root's file. Absent, or exactly ``brokered``, is ON; exactly ``passthrough`` opts out;
  anything else, or a file that is not root's alone, is OFF with its reason."""
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
    assert "only a human can vouch" in world.contrast(job)


def test_the_contrast_names_a_passthrough_record_as_passthrough_not_as_from_before_f9a(world):
    """L1 N1: the gate escalates every non-routemap record; the operator-facing reason must say why for THIS record. A
    passthrough run's backend is the vendor's, so only a human can vouch; it is not a record from before F9a."""
    job, _ = world.file()
    job = world.verify_routed(job.id, [(_pt, says("label-a")), (_pt, says("label-b"))], routemap=None)
    why = world.contrast(job)
    assert why is not None and "passthrough" in why and "before F9a" not in why, why
    assert "only a human can vouch" in why


@pytest.mark.parametrize("over",[{"job_id": "dj-other"}, {"run_index": 5}, {"kind": "planner"}, {"vendor": ""},
                                  {"model_requested": "other-model"}])
def test_a_passthrough_ticket_that_is_not_this_runs_is_unresolved(world, over):
    job, _ = world.file()
    job = world.verify_routed(job.id, [(lambda j, r: _pt(j, r, **over), says("x"))], routemap=None)
    assert (job.state, job.history[-1].evidence) == ("failed", J.BACKEND_UNRESOLVED)


def _pt_unnamed(jid, run):
    """A passthrough executor ticket that names no model (``model_requested`` is optional on one)."""
    t = _pt(jid, run)
    del t["model_requested"]
    return t


@pytest.mark.parametrize("caller", [says("x", requested="caller-model"), {"answered": "x", "backend_identity": "x"}])
def test_a_passthrough_ticket_naming_no_model_records_no_requested_from_the_caller(world, caller):
    """L2 LOW-2: ``requested`` is the ticket's or nothing; the caller's report never supplies it. The record's shape
    holds exactly ``requested``, ``answered``, ``backend_identity``, so a run whose ticket names no model records
    nothing and is unresolved (fails closed), whatever the caller said."""
    job, _ = world.file()
    job = world.verify_routed(job.id, [(_pt_unnamed, caller)], routemap=None)
    assert (job.state, job.history[-1].evidence) == ("failed", J.BACKEND_UNRESOLVED)
    assert world.jstore.run_records(job.id) == []


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


class _Wired(Exception):
    """Raised by the captured ``EventFollower.at``: what ``follow_run.main`` handed it, and the follower never runs."""


def _follow_main_wiring(tmp_path, monkeypatch, **kw):
    """Run ``follow_run.main`` up to the follower's construction; answer the ``routemap``/``ticket_for`` it passed."""
    seen = {}

    def at(jobs, socket, resolve, **fields):
        seen.update(fields, socket=socket)
        raise _Wired()
    monkeypatch.setattr(FR.EventFollower, "at", staticmethod(at))
    sock = str(tmp_path / "control.sock")
    with pytest.raises(_Wired):
        FR.main(["--store-dir", str(tmp_path / "store"), "--socket", sock, "--export-dir", str(tmp_path / "export")],
                **kw)
    return seen, sock


def test_follow_run_main_hands_the_follower_the_supervisor_readers_on_its_socket(tmp_path, monkeypatch):
    """L1 W2 (a): the behaviour, not the source text: by default the follower gets ``SupervisorFacts`` bound methods on
    the ``--socket`` CONTROL serves, limited to the two read verbs; ``supervisor=None`` wires none; an explicit reader
    replaces only its own default."""
    seen, sock = _follow_main_wiring(tmp_path, monkeypatch)
    for name in ("routemap", "ticket_for"):
        reader = seen[name]
        assert isinstance(reader.__self__, FR.SupervisorFacts) and reader.__func__ is getattr(FR.SupervisorFacts, name)
        assert reader.__self__._port._path == sock and reader.__self__._port._ops == FR.SUPERVISOR_OPS
    assert seen["routemap"].__self__ is seen["ticket_for"].__self__ and seen["socket"] == sock
    assert FR.SUPERVISOR_OPS == frozenset({"RUN-TICKET-FACTS", "ROUTEMAP-ACTIVE"})
    seen, _ = _follow_main_wiring(tmp_path, monkeypatch, supervisor=None)
    assert seen["routemap"] is None and seen["ticket_for"] is None
    mine = object()
    seen, _ = _follow_main_wiring(tmp_path, monkeypatch, routemap=mine)
    assert seen["routemap"] is mine and isinstance(seen["ticket_for"].__self__, FR.SupervisorFacts)
    seen, _ = _follow_main_wiring(tmp_path, monkeypatch, ticket_for=mine)
    assert seen["ticket_for"] is mine and isinstance(seen["routemap"].__self__, FR.SupervisorFacts)


# --- the setting: ON unless it opts out ---------------------------------------------------------------------------------

ME = os.getuid()


def test_an_absent_setting_is_on(tmp_path):
    assert DR.read_dispatch_auth(tmp_path / "dispatch_auth", owner=ME) == ("brokered", None)


def test_exactly_passthrough_from_the_owner_is_the_opt_out_with_no_reason(tmp_path):
    p = tmp_path / "dispatch_auth"
    p.write_bytes(b"passthrough\n")
    p.chmod(0o644)
    assert DR.read_dispatch_auth(p, owner=ME) == ("passthrough", None)
    assert DR.read_dispatch_auth(p, owner=ME + 1)[0] == "passthrough" and DR.read_dispatch_auth(p, owner=ME + 1)[1]
    p.chmod(0o666)
    assert DR.read_dispatch_auth(p, owner=ME)[1] is not None                   # others can write it: not the opt-out


def test_only_exactly_brokered_or_passthrough_from_the_owner_is_read(tmp_path):
    p = tmp_path / "dispatch_auth"
    p.write_bytes(DR.DISPATCH_AUTH_ON)
    p.chmod(0o644)
    assert DR.read_dispatch_auth(p, owner=ME) == ("brokered", None)
    assert DR.read_dispatch_auth(p, owner=ME + 1)[0] == "passthrough"          # another owner
    p.chmod(0o666)
    assert DR.read_dispatch_auth(p, owner=ME)[0] == "passthrough"              # others can write it
    p.chmod(0o644)
    for content in (b"brokered", b"BROKERED\n", b"brokered\n\n", b"passthrough", b"passthrough\n\n", b""):
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
    """``brokered=False`` (what ``main`` passes for an opt-out) is one passthrough launcher, no brokered sibling, no spend reader."""
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


class _MainRig:
    """``dispatch_run.main`` with the process edges stubbed (account check, log bound, signals) and its collaborators
    recorded: ``read_dispatch_auth`` answers ``auth``, ``build_server`` returns fakes, ``_bind`` hands out fake
    listeners (``bind_fail`` maps a socket name to the exception its bind raises), ``serve`` records and returns."""

    class Server:
        def __init__(self, name, brokered=None):
            import types
            self.name, self.brokered, self.shut = name, brokered, []
            self.launcher = types.SimpleNamespace(runtime=types.SimpleNamespace(close=lambda: None))

        def shutdown(self, budget):
            self.shut.append(budget)

    class Listener:
        def __init__(self, path):
            self.path, self.closed = path, False

        def close(self):
            self.closed = True

    def __init__(self, tmp_path, monkeypatch, auth, bind_fail=None, *, real_read=False):
        import threading
        import types
        self.events, self.served, self.bound = [], [], []
        self.sock = tmp_path / "run" / "run.sock"
        self.lock = threading.Lock()
        monkeypatch.setattr(DR.pwd, "getpwnam", lambda name: types.SimpleNamespace(pw_uid=os.geteuid()))
        monkeypatch.setattr(DR, "_bound_log", lambda *a, **k: None)
        monkeypatch.setattr(DR.signal, "signal", lambda *a: None)

        def read(path, owner=0):
            self.events.append("read")
            return (auth, None if auth == "brokered" else "absent")

        def build(host, store, control, op_uid, *, brokered=False):
            self.events.append(f"build:{brokered}")
            self.server = self.Server("passthrough", self.Server("brokered") if brokered else None)
            return self.server

        def bind(path):
            self.events.append(f"bind:{path.name}")
            if bind_fail and path.name in bind_fail:
                raise bind_fail[path.name]
            lis = self.Listener(path)
            self.bound.append(lis)
            return lis

        def serve(server, listener, stop):
            with self.lock:
                self.served.append((server.name, listener.path.name))
        if not real_read:
            monkeypatch.setattr(DR, "read_dispatch_auth", read)
        monkeypatch.setattr(DR, "build_server", build)
        monkeypatch.setattr(DR, "_bind", bind)
        monkeypatch.setattr(DR, "serve", serve)

    def run(self, setting="/nonexistent-setting"):
        return DR.main(["--socket", str(self.sock), "--operator-uid", str(ME), "--store-dir", "/nonexistent-store",
                        "--auth-setting", str(setting)])


def test_main_with_no_setting_file_serves_both_sockets(tmp_path, monkeypatch):
    """The real reader, an absent setting: the brokered server is built and served on its own socket."""
    rig = _MainRig(tmp_path, monkeypatch, "brokered", real_read=True)
    assert rig.run(tmp_path / "no-such-setting") == 0
    assert rig.events == ["build:True", "bind:run.sock", f"bind:{DR.BROKERED_SOCKET_NAME}"]
    assert sorted(rig.served) == sorted([("passthrough", "run.sock"), ("brokered", DR.BROKERED_SOCKET_NAME)])


def test_main_with_the_opt_out_setting_serves_the_passthrough_socket_alone(tmp_path, monkeypatch):
    rig = _MainRig(tmp_path, monkeypatch, "passthrough", real_read=True)
    p = tmp_path / "dispatch_auth"
    p.write_bytes(DR.DISPATCH_AUTH_OFF)
    p.chmod(0o644)
    monkeypatch.setattr(DR.os, "fstat", lambda fd, _f=os.fstat: type("S", (), {
        "st_mode": _f(fd).st_mode, "st_uid": 0})())     # the file is root's for this read
    assert rig.run(p) == 0
    assert rig.events == ["build:False", "bind:run.sock"] and rig.served == [("passthrough", "run.sock")]


def test_main_with_a_malformed_setting_serves_passthrough_alone_and_says_why(tmp_path, monkeypatch, capsys):
    rig = _MainRig(tmp_path, monkeypatch, "passthrough", real_read=True)
    p = tmp_path / "dispatch_auth"
    p.write_bytes(b"on\n")
    p.chmod(0o644)
    monkeypatch.setattr(DR.os, "fstat", lambda fd, _f=os.fstat: type("S", (), {
        "st_mode": _f(fd).st_mode, "st_uid": 0})())
    assert rig.run(p) == 0
    assert rig.events == ["build:False", "bind:run.sock"]
    assert "brokered launcher off: the setting's content is neither" in capsys.readouterr().err


def test_main_off_serves_the_passthrough_socket_alone(tmp_path, monkeypatch):
    """L1 W2 (b), OFF: the setting is read before the build, nothing brokered is built, bound or served."""
    rig = _MainRig(tmp_path, monkeypatch, "passthrough")
    assert rig.run() == 0
    assert rig.events == ["read", "build:False", "bind:run.sock"]
    assert rig.served == [("passthrough", "run.sock")]
    assert rig.server.shut and all(lis.closed for lis in rig.bound)


def test_main_on_serves_the_brokered_server_on_its_own_socket(tmp_path, monkeypatch):
    """L1 W2 (b), ON: the brokered server is built, bound beside run.sock, SERVED (its accept loop runs), shut down
    and its listener closed. Deleting the side thread's start, or binding it on the wrong path, turns this red."""
    rig = _MainRig(tmp_path, monkeypatch, "brokered")
    assert rig.run() == 0
    assert rig.events == ["read", "build:True", "bind:run.sock", f"bind:{DR.BROKERED_SOCKET_NAME}"]
    assert sorted(rig.served) == sorted([("passthrough", "run.sock"), ("brokered", DR.BROKERED_SOCKET_NAME)])
    assert rig.bound[1].path == rig.sock.parent / DR.BROKERED_SOCKET_NAME
    assert rig.server.shut and rig.server.brokered.shut and all(lis.closed for lis in rig.bound)


def test_main_on_whose_brokered_bind_fails_logs_it_and_still_serves_the_passthrough_socket(tmp_path, monkeypatch,
                                                                                            capsys):
    """L2 small obs 1: ON must never take OFF's server down. An OSError binding run-brokered.sock (not a Refused) is
    one stderr line and the service serves run.sock alone, as an opt-out does."""
    rig = _MainRig(tmp_path, monkeypatch, "brokered",
                   bind_fail={DR.BROKERED_SOCKET_NAME: OSError(48, "Address already in use")})
    assert rig.run() == 0
    assert rig.served == [("passthrough", "run.sock")]
    assert rig.server.shut and all(lis.closed for lis in rig.bound)
    err = capsys.readouterr().err
    assert "brokered" in err and "OSError" in err and err.count("\n") == 1, err




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



# --- L3 r1 (prewr2): the END job's thread starts outside the shared registry lock (codex MED) -------------------------

class _EndLauncher:
    """A launcher whose ``stop`` proves every END (all an END job calls)."""

    def __init__(self):
        self.stopped = []

    def stop(self, key, deadline):
        from levain.agents.accounting import StopResult
        self.stopped.append(key)
        return StopResult(True, "ended")


def _end_server():
    return DR.RunServer(_EndLauncher(), verify=lambda *a: None, pool_free=lambda: True, peer_ok=lambda s: True,
                        registry=DR.HolderRegistry())


def _patch_end_thread_start(monkeypatch, on_start):
    """Route the start of an END job's thread (target ``_run_end``) through ``on_start(key, real_start)``; every other
    thread starts as usual."""
    import threading
    real = threading.Thread

    class Gated(real):
        def __init__(self, *a, target=None, args=(), **kw):
            super().__init__(*a, target=target, args=args, **kw)
            self._gated_key = args[0] if getattr(target, "__name__", "") == "_run_end" else None

        def start(self):
            if self._gated_key is None:
                return super().start()
            return on_start(self._gated_key, super().start)
    monkeypatch.setattr(threading, "Thread", Gated)


def test_an_end_whose_thread_cannot_start_answers_the_failure_and_the_key_is_not_wedged(monkeypatch):
    """A Thread.start that raises: the END caller gets the unproven failure by name, and a later END on the key starts
    a fresh job that proves."""
    srv = _end_server()

    def boom(key, start):
        raise RuntimeError("can't start new thread")
    _patch_end_thread_start(monkeypatch, boom)
    got = srv._end("s-k1", 2.0)
    assert (got.terminated, got.detail) == (False, "END could not start; nothing assumed ended"), got
    monkeypatch.undo()
    again = srv._end("s-k1", 5.0)
    assert again.terminated is True, again
    assert srv.launcher.stopped == ["s-k1"]


def test_a_slow_end_thread_start_does_not_hold_the_registry_lock(monkeypatch):
    """codex MED: a stalled Thread.start for one key's END must not stall another key's END (or SYNC, or shutdown)
    behind the shared ``mu``."""
    import threading
    gate, entered = threading.Event(), threading.Event()

    def slow(key, start):
        if key == "s-k1":
            entered.set()
            gate.wait(10)
        return start()
    _patch_end_thread_start(monkeypatch, slow)
    srv = _end_server()
    first: list = []
    t = threading.Thread(target=lambda: first.append(srv._end("s-k1", 10.0)), daemon=True)
    t.start()
    try:
        assert entered.wait(5)
        other: list = []
        u = threading.Thread(target=lambda: other.append(srv._end("s-k2", 2.0)), daemon=True)
        u.start()
        u.join(3)
        assert other and other[0].terminated is True, "another key's END waited on a stalled END thread start"
    finally:
        gate.set()
        t.join(10)
    assert first and first[0].terminated is True


# --- L3 r1 (prewr2): an executor ticket that ran outlives the start compaction (codex MED, complement LOW-MED) ----------

def _ran_and_released(rig, attempt, session, index):
    """``attempt``'s executor ticket bound to ``session`` on an egress slot lease, then RELEASED by a SLOT-SYNC that
    reports the lease over: a closed ticket that ran."""
    from tests.test_tickets import ticket_of
    one = ticket_of(rig.mint(attempt))
    rig.tickets.bind(one, session, ("egress", index, 1))
    assert rig.tickets.release_ended({("egress", index): (2, "reusable")}) == [session]
    assert rig.ask("RUN-TICKET-STATUS", attempt_id=attempt)["state"] == "released"
    return one


def test_a_released_executor_ticket_still_resolves_after_a_compaction_past_its_day(rig):
    """A follower down past the UTC midnight after the mint, CONTROL restarted (the start compaction ran): the run's
    executor facts are still CONTROL's to answer, so the terminal event resolves instead of failing
    backend-unresolved."""
    from tests.test_tickets import DAY
    _ran_and_released(rig, "a-1", "s-" + "a" * 16, 0)
    before = rig.ask("RUN-TICKET-FACTS", job_id="job-1", run_index=0)
    assert before["ok"] is True, before
    rig.clock.t += DAY
    rig.restart()                                         # the compaction writes the journal ...
    rig.restart()                                         # ... and this start reads only what it kept
    after = rig.ask("RUN-TICKET-FACTS", job_id="job-1", run_index=0)
    assert after == before, after
    assert rig.ask("RUN-TICKET-STATUS", attempt_id="a-1")["state"] == "released"
    rig.clock.t += 30 * DAY
    rig.restart()                                         # and again: the kept line survives its own compaction
    assert rig.ask("RUN-TICKET-FACTS", job_id="job-1", run_index=0) == before


def test_two_executor_tickets_of_one_run_stay_ambiguous_after_a_compaction(rig):
    from tests.test_tickets import DAY
    _ran_and_released(rig, "a-1", "s-" + "a" * 16, 0)
    _ran_and_released(rig, "a-2", "s-" + "b" * 16, 1)
    rig.clock.t += DAY
    rig.restart()                                         # the compaction writes the journal ...
    rig.restart()                                         # ... and this start reads only what it kept
    assert rig.ask("RUN-TICKET-FACTS", job_id="job-1", run_index=0)["refused"] == "ticket-ambiguous"


def test_a_cancelled_executor_ticket_is_still_compacted_to_its_tombstone(rig):
    """Only a ticket that RAN keeps its mint line; one that never bound is a tombstone past its day, as before."""
    from tests.test_tickets import DAY, ticket_of
    one = ticket_of(rig.mint("a-1"))
    assert rig.ask("RUN-TICKET-CANCEL", ticket=one)["ok"] is True
    rig.clock.t += DAY
    rig.restart()
    assert [x["op"] for x in rig.lines()] == ["tombstone"]
    assert rig.ask("RUN-TICKET-FACTS", job_id="job-1", run_index=0)["refused"] == "ticket-unknown"


# --- L3 r1 (prewr2): a record with no ``auth`` is passthrough's; a tombstone keeps its record's auth (codex OPEN-4) -----

def _launchers(tmp_path):
    from levain.agents import session_launcher as L
    from tests.test_agents_session_launcher import FakeControl, FakeRuntime, FakeSources
    index = L.SessionIndex(tmp_path / "idx")
    pt = L.RealLauncher(FakeControl(), FakeRuntime(), FakeSources(), index)
    br = L.RealLauncher(FakeControl(), FakeRuntime(), FakeSources(), index, auth="brokered", spend=lambda run: 4242)
    return index, pt, br


def test_a_record_with_no_auth_predates_the_split_and_is_passthrough(tmp_path):
    """Every record written before the split socket is a passthrough launch: the brokered launcher answers the caps by
    name for one, never its own reading; the passthrough launcher reads it as before."""
    index, pt, br = _launchers(tmp_path)
    index.put("s-00000000000000d1", "ended", run_id="r-" + "1" * 16, session="sess-9", created_s=1.0)
    assert "auth" not in index.get("s-00000000000000d1")
    got = br.usage("s-00000000000000d1")
    assert (got.usd_micros, got.source) == (None, "session:auth-mismatch"), got
    assert pt.usage("s-00000000000000d1").source != "session:auth-mismatch"
    index.put("s-00000000000000d2", "refused", reason="x", created_s=1.0)
    assert br.usage("s-00000000000000d2").source == "session:auth-mismatch"
    assert pt.usage("s-00000000000000d2").source == "session:not-started"


@pytest.mark.parametrize("auth", ["passthrough", "brokered"])
def test_a_tombstone_keeps_the_auth_of_the_record_it_replaces(tmp_path, auth):
    """A spawn's ``undone``/``refused`` replaces its own ``pending``/``minted`` record: the auth stays, so its own
    launcher reads zero and the other launcher reads the caps by name."""
    index, pt, br = _launchers(tmp_path)
    own, other = (pt, br) if auth == "passthrough" else (br, pt)
    key = "s-00000000000000e1"
    index.create(key, "minted", run_id="r-" + "2" * 16, created_s=1.0, auth=auth)
    own._tombstone(key, "undone", "spawn-failed")
    assert index.get(key)["state"] == "undone" and index.get(key)["auth"] == auth
    assert own.usage(key).source == "session:not-started"
    assert other.usage(key).source == "session:auth-mismatch"


@pytest.mark.parametrize("auth", ["passthrough", "brokered"])
def test_a_new_tombstone_carries_its_launchers_auth(tmp_path, auth):
    """A refusal before anything was allocated (no record to replace) is the writing launcher's: it reads zero there."""
    index, pt, br = _launchers(tmp_path)
    own, other = (pt, br) if auth == "passthrough" else (br, pt)
    own.refuse_key("s-00000000000000f1", "workspace-refused")
    assert index.get("s-00000000000000f1")["auth"] == auth
    assert own.usage("s-00000000000000f1").source == "session:not-started"
    assert other.usage("s-00000000000000f1").source == "session:auth-mismatch"


def test_a_brokered_spawn_whose_start_fails_reads_zero_on_its_own_socket(tmp_path):
    """End to end through ``spawn``: the undone tombstone of a brokered launch is the brokered launcher's zero."""
    from levain.agents import session_launcher as L
    from tests.test_agents_session_launcher import FakeControl, FakeRuntime, FakeSources, launch
    ctl = FakeControl()
    ctl.script["ROUTE-RESOLVE"] = lambda f: {"route": "glm", "routemap_generation": 1,
                                             "route_policy_digest": "sha256:" + "a" * 64}
    br = L.RealLauncher(ctl, FakeRuntime(fail=OSError("no slot")), FakeSources(), L.SessionIndex(tmp_path / "idx"),
                        auth="brokered", spend=lambda run: 4242)
    lz = launch(auth="brokered", exposure=None)
    import dataclasses
    lz = dataclasses.replace(lz, usd_micros=1_000_000)
    with pytest.raises(OSError):
        br.spawn(lz)
    rec = br.index.get(lz.session_key)
    assert (rec["state"], rec["auth"]) == ("undone", "brokered"), rec
    assert br.usage(lz.session_key).source == "session:not-started"


# --- L3 r1 (prewr2): ROUTEMAP-ACTIVE's generation must be an int, not equal-to-one (codex LOW) ---------------------------

class _OnePort:
    def __init__(self, answer):
        self.answer = answer

    def call(self, op, **kw):
        return self.answer


@pytest.mark.parametrize("gen", [True, 1.0])
def test_a_routemap_reply_whose_generation_only_equals_the_asked_one_fails_closed(gen):
    """``True == 1`` and ``1.0 == 1``: a wrong-shaped reply is None (unresolved), never an active map."""
    ok = {"generation": 1, "routes": {}, "digests": {}}
    assert FR.SupervisorFacts(_OnePort(ok)).routemap(1) is not None
    assert FR.SupervisorFacts(_OnePort({**ok, "generation": gen})).routemap(1) is None


# --- L3 r1 (prewr2): both servers' shutdown fits ONE budget (complement LOW) -----------------------------------------

def test_main_on_shuts_both_servers_down_within_one_budget(tmp_path, monkeypatch):
    """Each server's shutdown takes its whole budget here (sessions that never prove): the two together must still
    return within ONE ``SHUTDOWN_BUDGET_S``, not two back to back (a unit's stop timeout is sized for one)."""
    import time
    budget = 0.6
    monkeypatch.setattr(DR, "SHUTDOWN_BUDGET_S", budget)
    rig = _MainRig(tmp_path, monkeypatch, "brokered")

    def slow(server):
        def shutdown(b):
            server.shut.append(b)
            time.sleep(max(0.0, b))
        return shutdown
    real_build = DR.build_server

    def build(*a, **kw):
        s = real_build(*a, **kw)
        s.shutdown, s.brokered.shutdown = slow(s), slow(s.brokered)
        return s
    monkeypatch.setattr(DR, "build_server", build)
    t0 = time.monotonic()
    assert rig.run() == 0
    took = time.monotonic() - t0
    assert rig.server.shut and rig.server.brokered.shut
    # each had (nearly) the whole budget, never a share of it or more than it ...
    assert all(0.5 * budget < b <= budget for b in rig.server.shut + rig.server.brokered.shut)
    # ... and the two ran at once
    assert took < 1.5 * budget, f"both shutdowns took {took:.2f}s against one budget of {budget}s"
