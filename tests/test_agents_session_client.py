"""P7-1 S9 + S10a: the operator-side client of the dispatch run server (levain.agents.session_client) and its wire codec.

The server here is an IN-PROCESS stand-in for S10b: a thread per socketpair connection that decodes with the codec's own
functions and drives the REAL :class:`levain.agents.session_launcher.RealLauncher` over the S4 fakes (control, runtime,
usage sources). So the client is proven against the launcher's real answers, and against nothing the stand-in invents.

Held by these tests: (i) ``run_session`` through the client ends one occurrence ``done``; (ii) ``RowSessionExecutor.stop``
through the client is ``terminated=True`` (finding A: the bare ``RealLauncher`` has no ``end_session``, reproduced first);
(iii) every transport fault reads ``GATE_UNPROVEN`` / the caps / ``StopResult(False)``; (iv) the ticket and the run id
appear in no frame either way (a sentinel over every byte of the wire, with a positive control that the sentinel did
reach the server side).

What no test here holds (UNVERIFIED until the VM rows): real AF_UNIX peer credentials across two uids, the real run
server (S10b), the real server-side signature verification (S10b), a real session.
"""
from __future__ import annotations

import ast
import json
import shutil
import socket
import subprocess
import threading
from pathlib import Path

import pytest

from levain import authority as A
from levain.agents import controller as ctl
from levain.agents import session as S
from levain.agents import session_client as C
from levain.agents import session_launcher as L
from levain.agents import session_wire as W
from levain.agents.accounting import StopResult, Usage
from levain.authority_sig import verify_link_sig
from tests.test_agents_launch import Rig
from tests.test_agents_session_launch import EXP, _occurrence, _reservation, _run
from tests.test_agents_session_launcher import FakeControl, FakeRuntime, FakeSources
from tests.test_agents_session_reaper_stop import DEAD, _executor, _session_row

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

SENT_TICKET = "SENTINELticket" + "0123456789abcdefghijklmnopqrstu"[:29]          # 43 chars: a ticket's length
SENT_RUN = "r-5e17e11e5e17e11e"


@pytest.fixture
def rig(tmp_path) -> Rig:
    return Rig(tmp_path)


# --- the in-process stand-in for the run server ---------------------------------------------------------------------

class Tap:
    """A socket that records every byte it sends and receives (the whole of what the client can see)."""

    def __init__(self, sock, wire):
        self._s, self._wire = sock, wire

    def sendall(self, data):
        self._wire.append(bytes(data))
        return self._s.sendall(data)

    def recv(self, n):
        data = self._s.recv(n)
        self._wire.append(bytes(data))
        return data

    def __getattr__(self, name):
        return getattr(self._s, name)


class Server:
    """One RealLauncher behind the wire. ``faults`` maps an op to ``drop`` (close, no answer), ``garbage`` (bytes that
    are not a frame), ``extra`` (a valid answer with a key outside the op's set), ``wrongid`` or ``refuse:<name>``."""

    def __init__(self, tmp_path, *, control=None, runtime=None, sources=None):
        self.control = control or FakeControl()
        self.runtime = runtime or FakeRuntime()
        self.launcher = L.RealLauncher(self.control, self.runtime, sources or FakeSources(),
                                       L.SessionIndex(tmp_path / "idx"))
        self.faults: dict[str, str] = {}
        self.wire: list[bytes] = []
        self.ops: list[str] = []
        self.proofs: list[C.LinkProof] = []
        self.launches: list[S.SessionLaunch] = []
        self.eof = threading.Event()
        self.child = None
        self.threads: list[threading.Thread] = []

    def connect(self):
        a, b = socket.socketpair()
        t = threading.Thread(target=self._serve, args=(b,), daemon=True)
        t.start()
        self.threads.append(t)
        return Tap(a, self.wire)

    def _serve(self, sock):
        sock.settimeout(20)
        mine = None
        try:
            while True:
                try:
                    req = C.read_run_frame(sock)
                except (C.WireError, OSError):
                    return
                if req is None:
                    self.eof.set()
                    return
                rid, op = C.check_run_request(req)
                self.ops.append(op)
                fault = self.faults.get(op)
                if fault == "drop":
                    return
                if fault == "garbage":
                    sock.sendall(b"\x00\x00\x00\x05zzzzz")
                    return
                if fault and fault.startswith("refuse:"):
                    sock.sendall(C.encode_run_frame(C.refused_answer(rid, fault.split(":", 1)[1], "no", "transient")))
                    continue
                try:
                    ans, mine = self._op(op, req, mine)
                except L.LauncherRefused as exc:
                    sock.sendall(C.encode_run_frame(C.refused_answer(rid, exc.name, exc.detail, exc.reason_class)))
                    continue
                if fault == "extra":
                    ans = {**ans, "ticket": SENT_TICKET}
                sock.sendall(C.encode_run_frame(C.ok_answer(-1 if fault == "wrongid" else rid, **ans)))
        finally:
            if mine is not None and not getattr(mine, "released", False):
                mine.abort()
            elif mine is not None:
                mine.end_once()
            sock.close()

    def _op(self, op, req, mine):
        if op == "LAUNCH":
            launch, proof = C.launch_from_wire(req)
            self.launches.append(launch)
            self.proofs.append(proof)
            mine = self.launcher.spawn(launch)
            self.child = mine
            return {"session": mine.session}, mine
        if op == "RELEASE":
            mine.release()
            return {}, mine
        if op == "TERM":
            mine.process.terminate()
            return {}, mine
        if op == "ABORT":
            return {"returncode": mine.abort(req["timeout_ms"] / 1000)}, mine
        if op == "WAIT":
            try:
                rc = mine.process.wait(req["timeout_ms"] / 1000)
            except subprocess.TimeoutExpired:
                return {"exited": False, "returncode": None}, mine
            return {"exited": True, "returncode": rc}, mine
        if op == "USAGE":
            return {"usage": C.usage_to_wire(self.launcher.usage(req["session_key"]))}, mine
        if op == "END":
            got = self.launcher.stop(req["session_key"], req["deadline_ms"] / 1000)
            return {"terminated": got.terminated, "detail": got.detail}, mine
        raise AssertionError(op)

    def client(self, rig):
        return C.DispatchSessionLauncher(self.connect, lambda occ: C.link_proof(rig.store, rig.world.root, occ, now=rig.now()))

    def frames(self):
        """Every frame that crossed, parsed, in wire order of arrival (both directions)."""
        raw = b"".join(self.wire)
        out, i = [], 0
        while i + 4 <= len(raw):
            n = int.from_bytes(raw[i:i + 4], "big")
            if n == 0 or i + 4 + n > len(raw):
                break
            out.append(json.loads(raw[i + 4:i + 4 + n]))
            i += 4 + n
        return out


@pytest.fixture
def srv(tmp_path):
    return Server(tmp_path)


def _sub_occurrence(rig):
    return _occurrence(rig, billing="subscription", exposure=EXP)


# --- finding A, reproduced first --------------------------------------------------------------------------------------

def test_finding_a_a_real_launcher_cannot_satisfy_the_reapers_seam(rig, srv):
    """At the base the only real launcher has ``stop`` and no ``end_session``, so every dead-fire row answers
    "cannot stop". This is the failure S9 closes; it stays as a pin that ``RealLauncher.stop`` is server-internal."""
    oid, key = _session_row(rig)
    assert not hasattr(srv.launcher, "end_session") and not isinstance(srv.launcher, S.SessionEnder)
    got = _executor(rig, oid, srv.launcher, DEAD).stop(10)
    assert got.terminated is False and "no end_session" in got.detail


# --- (ii) the reaper's stop through the client ------------------------------------------------------------------------------

def test_a_row_stop_through_the_client_is_terminated_true(rig, srv):
    oid, key = _session_row(rig)
    srv.launcher.spawn(S.SessionLaunch(oid, key, "claude", "sonnet", "the brief", "passthrough", EXP))   # the dead fire's
    client = srv.client(rig)
    assert isinstance(client, S.SessionEnder) and isinstance(client, S.SessionLauncher)
    got = _executor(rig, oid, client, DEAD).stop(10)
    assert got.terminated is True and "S1 END proved" in got.detail
    assert srv.runtime.ended == ["sess-1"] and "END" in srv.ops


def test_an_end_the_server_cannot_prove_is_not_a_stop(rig, srv):
    oid, key = _session_row(rig)
    srv.runtime.end_ok = False
    srv.launcher.spawn(S.SessionLaunch(oid, key, "claude", "sonnet", "the brief", "passthrough", EXP))
    got = _executor(rig, oid, srv.client(rig), DEAD).stop(10)
    assert got.terminated is False and "not proven" in got.detail


# --- (i) run_session through the client ----------------------------------------------------------------------------------------

def test_run_session_through_the_client_ends_one_occurrence_done(rig, srv):
    oid = _sub_occurrence(rig)
    client = srv.client(rig)
    sup, report = _run(rig, oid, client, exposure=EXP)
    assert (report.outcome, report.state, report.code) == ("exited", "done", 0), report
    assert rig.row(oid)["state"] == "done"
    assert srv.ops[:3] == ["LAUNCH", "RELEASE", "WAIT"] and srv.ops.count("LAUNCH") == 1
    (start,) = srv.runtime.starts
    assert start.session_key == rig.row(oid)["session_key"] and start.harness == "claude"
    res = _reservation(rig, oid)
    assert res[0] == "settled" and res[2] == 7 and res[3] == 1000 and res[4] == 30     # the sources' figures, via USAGE


def test_the_launch_frame_carries_the_proof_of_his_signature_for_the_server_to_check(rig, srv):
    """Q1 = (b): the client carries the link's own signed rows. Here the test plays S10b: the signature verifies under the
    enrolled key over ``prev_hash + newline + record``, and the signed record names this occurrence's spec hash."""
    oid = _sub_occurrence(rig)
    _run(rig, oid, srv.client(rig), exposure=EXP)
    (proof,) = srv.proofs
    row = rig.row(oid)
    assert (proof.occurrence_id, proof.spec_hash, proof.link_id) == (oid, row["spec_hash"], row["link_id"])
    state = rig.authority.replay()
    for r in {proof.born, proof.current}:
        key = state.keys[r.signer_fp]
        assert verify_link_sig(A._message(r.prev_hash, r.record), r.signature, public_key=key.public_key,
                               fingerprint=r.signer_fp)
    rec = json.loads(proof.born.record)
    assert rec["op"] == "create_grant" and rec["grant"]["spec_hash"] == row["spec_hash"]
    assert proof.born.seq == state.grant_born[proof.link_id] and proof.store_id == rig.sid


def test_a_renewed_link_carries_both_its_born_and_its_current_row(rig, srv):
    oid = _sub_occurrence(rig)
    link_id = rig.row(oid)["link_id"]
    grant = rig.authority.replay().policy.grants[link_id]
    from datetime import timedelta
    from levain.cockpit.consent import utc_stamp
    at = rig.world.clock.now + timedelta(minutes=1)
    later = A._parse_stamp(grant["expires_at"], "expires_at") + timedelta(hours=1)
    rig.world.link({"op": "renew", "grant_id": link_id, "expires_at": utc_stamp(later), "signed_at": utc_stamp(at)},
                   "laptop")
    proof = C.link_proof(rig.store, rig.world.root, oid, now=rig.now())
    assert proof.born.seq < proof.current.seq and json.loads(proof.current.record)["op"] == "renew"
    assert grant["spec_hash"] == proof.spec_hash


def test_a_link_lapsed_by_the_clock_is_refused_by_the_client_too(rig):
    from datetime import timedelta
    oid = _sub_occurrence(rig)
    C.link_proof(rig.store, rig.world.root, oid, now=rig.now())
    with pytest.raises(C.ProofUnavailable, match="expired"):
        C.link_proof(rig.store, rig.world.root, oid, now=rig.now(timedelta(days=400)))


def test_a_long_end_detail_and_a_nonstring_key_do_not_undo_a_proven_stop(rig, srv):
    oid, key = _session_row(rig)
    srv.launcher.spawn(S.SessionLaunch(oid, key, "claude", "sonnet", "the brief", "passthrough", EXP))
    real = srv.launcher.stop
    srv.launcher.stop = lambda k, d: StopResult(real(k, d).terminated, "x" * 5000)
    client = srv.client(rig)
    got = client.end_session(key, 3)
    assert got.terminated is True and len(got.detail) <= 400
    assert client.end_session(None, 3).terminated is False and client.usage(None).actions is None


@pytest.mark.parametrize("how", ["no-link", "revoked", "no-occurrence"])
def test_a_link_that_cannot_be_proven_refuses_before_any_connection(rig, srv, how):
    from tests.test_authority_agent_link import revoke_agent
    oid = _sub_occurrence(rig)
    asked = oid
    if how == "no-link":
        asked = rig.claim("another")                                               # claimed, never launched: no link
    elif how == "revoked":
        rig.world.link(revoke_agent(rig.row(oid)["agent_id"], rig.sid), "laptop")   # the link no longer stands
    else:
        asked = "oc-nothing"
    with pytest.raises(L.LauncherRefused) as e:
        srv.client(rig).spawn(S.SessionLaunch(asked, "s-0123456789abcdef", "claude", "sonnet", "b", "passthrough", EXP))
    assert e.value.name == "link-proof-unavailable" and e.value.reason_class == "standing"
    assert srv.ops == [] and srv.wire == []


# --- (iii) every transport fault is unproven -----------------------------------------------------------------------------------

def test_no_server_at_all_raises_a_transient_refusal_with_nothing_allocated(rig, tmp_path):
    def down():
        raise ConnectionRefusedError(2, "no socket")
    oid = _sub_occurrence(rig)
    client = C.DispatchSessionLauncher(down, lambda occ: C.link_proof(rig.store, rig.world.root, occ, now=rig.now()))
    with pytest.raises(L.LauncherRefused) as e:
        client.spawn(S.SessionLaunch(oid, "s-0123456789abcdef", "claude", "sonnet", "b", "passthrough", EXP))
    assert (e.value.name, e.value.reason_class) == ("dispatch-unreachable", "transient")
    _, report = _run(rig, oid, client, exposure=EXP)
    assert report.state == "failed" and rig.row(oid)["reason"].startswith("session_spawn_failed")
    assert client.usage("s-0123456789abcdef") == Usage(None, None, None, "session:dispatch-unreachable")
    assert client.end_session("s-0123456789abcdef", 3).terminated is False


@pytest.mark.parametrize("fault", ["drop", "garbage", "extra", "wrongid"])
def test_a_launch_whose_answer_is_lost_returns_a_down_child_that_proves_nothing(rig, srv, fault):
    """The request may have been delivered, so spawn must not raise (a raising spawn promises nothing is live)."""
    srv.faults["LAUNCH"] = fault
    oid = _sub_occurrence(rig)
    child = srv.client(rig).spawn(S.SessionLaunch(oid, "s-0123456789abcdef", "claude", "sonnet", "b", "passthrough", EXP))
    assert child.process.wait(1) == ctl.GATE_UNPROVEN and child.process.poll() == ctl.GATE_UNPROVEN
    assert not ctl.gate_proves_termination(child.process.wait())
    assert child.abort() == ctl.GATE_UNPROVEN
    child.process.terminate()                                                    # silent
    with pytest.raises(RuntimeError):
        child.release()


def test_a_run_whose_launch_answer_is_lost_is_never_done_and_keeps_its_slot(rig, srv):
    srv.faults["LAUNCH"] = "drop"
    oid = _sub_occurrence(rig)
    with pytest.raises(RuntimeError, match="did not answer LAUNCH"):             # start_run aborts the child and propagates
        _run(rig, oid, srv.client(rig), exposure=EXP)
    assert rig.row(oid)["state"] == "launched" and rig.taken(oid)[0] != []       # the slot is held for the reaper


@pytest.mark.parametrize("fault", ["drop", "garbage", "extra", "wrongid"])
def test_a_wait_that_breaks_reads_gate_unproven_and_the_run_is_not_done(rig, srv, fault):
    srv.faults["WAIT"] = fault
    oid = _sub_occurrence(rig)
    _, report = _run(rig, oid, srv.client(rig), exposure=EXP)
    assert report.outcome == "exited" and report.state == "launched", report
    assert "does not prove" in report.note and rig.row(oid)["state"] == "launched"


def test_a_fault_closes_the_holder_so_the_server_sees_eof_and_ends_the_session(rig, srv):
    srv.faults["WAIT"] = "drop"
    oid = _sub_occurrence(rig)
    child = srv.client(rig).spawn(S.SessionLaunch(oid, "s-0123456789abcdef", "claude", "sonnet", "b", "passthrough", EXP))
    child.release()
    assert child.process.wait() == ctl.GATE_UNPROVEN
    for t in srv.threads:
        t.join(5)
    assert srv.runtime.ended == ["sess-1"]                                          # the stand-in's EOF rule: END after RELEASE
    sent = len(srv.ops)
    assert child.process.poll() == ctl.GATE_UNPROVEN and child.abort() == ctl.GATE_UNPROVEN
    child.process.terminate()
    assert len(srv.ops) == sent                                                     # the closed holder sends nothing more


def test_a_release_that_fails_raises_and_the_abort_after_it_is_unproven(rig, srv):
    srv.faults["RELEASE"] = "drop"
    oid = _sub_occurrence(rig)
    child = srv.client(rig).spawn(S.SessionLaunch(oid, "s-0123456789abcdef", "claude", "sonnet", "b", "passthrough", EXP))
    with pytest.raises(RuntimeError):
        child.release()
    assert child.abort() == ctl.GATE_UNPROVEN and child.released is False


@pytest.mark.parametrize("fault", ["drop", "garbage", "extra", "wrongid"])
def test_usage_that_cannot_be_read_is_the_caps(rig, srv, fault):
    oid = _sub_occurrence(rig)
    _run(rig, oid, srv.client(rig), exposure=EXP)
    key = rig.row(oid)["session_key"]
    client = srv.client(rig)
    assert client.usage(key).actions == 7                                          # the control: it reads
    srv.faults["USAGE"] = fault
    got = client.usage(key)
    assert (got.usd_micros, got.actions, got.tunnel_bytes, got.seconds) == (None, None, None, None)
    assert got.source.startswith("session:dispatch-")


def test_a_usage_with_an_off_type_figure_is_the_caps(rig, srv):
    client = srv.client(rig)
    srv.launcher.usage = lambda key: Usage(0, None, None, "x", actions=True)       # a bool is not a count
    assert client.usage("s-0123456789abcdef").actions is None
    srv.launcher.usage = lambda key: Usage(0, None, None, "x", actions=-4)
    assert client.usage("s-0123456789abcdef").actions is None
    assert client.usage("../../etc/passwd") == Usage(None, None, None, "session:dispatch-bad-key")


@pytest.mark.parametrize("fault", ["drop", "garbage", "extra", "wrongid", "refuse:end-busy"])
def test_an_end_that_is_not_answered_exactly_is_not_a_stop(rig, srv, fault):
    oid, key = _session_row(rig)
    srv.launcher.spawn(S.SessionLaunch(oid, key, "claude", "sonnet", "the brief", "passthrough", EXP))
    client = srv.client(rig)
    srv.faults["END"] = fault
    got = client.end_session(key, 3)
    assert type(got) is StopResult and got.terminated is False
    srv.faults.clear()
    assert client.end_session(key, 3).terminated is True                           # the control


def test_an_end_answering_a_non_boolean_terminated_is_not_a_stop(rig, srv):
    oid, key = _session_row(rig)
    srv.launcher.spawn(S.SessionLaunch(oid, key, "claude", "sonnet", "the brief", "passthrough", EXP))
    srv.launcher.stop = lambda k, d: type("R", (), {"terminated": 1, "detail": "yes"})()
    assert srv.client(rig).end_session(key, 3).terminated is False


def test_a_launch_refusal_keeps_its_name_and_class(rig, srv):
    srv.faults["LAUNCH"] = "refuse:no-free-slot"
    oid = _sub_occurrence(rig)
    with pytest.raises(L.LauncherRefused) as e:
        srv.client(rig).spawn(S.SessionLaunch(oid, "s-0123456789abcdef", "claude", "sonnet", "b", "passthrough", EXP))
    assert (e.value.name, e.value.reason_class) == ("no-free-slot", "transient")


def test_the_real_launchers_own_refusal_crosses_with_its_name(rig, srv):
    oid = _sub_occurrence(rig)
    with pytest.raises(L.LauncherRefused) as e:
        srv.client(rig).spawn(S.SessionLaunch(oid, "s-0123456789abcdef", "codex", "sonnet", "b", "passthrough", EXP))
    assert e.value.name == "harness-not-claude" and srv.runtime.starts == []


def test_a_protocol_the_server_does_not_speak_is_a_wire_error_not_a_guess():
    with pytest.raises(C.WireError, match="protocol-mismatch"):
        C.check_run_request({"p": "levain-run/2", "op": "LAUNCH", "id": 1})
    with pytest.raises(C.WireError):
        C.check_run_request({"op": "LAUNCH", "id": 1})


def test_a_slow_wait_times_out_like_a_popen_and_the_child_keeps_running(rig, srv):
    class Slow:
        def __init__(self):
            self.gate = threading.Event()
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            if not self.gate.wait(timeout):
                raise subprocess.TimeoutExpired("x", timeout)
            self.returncode = 0
            return 0

        def terminate(self):
            self.gate.set()

    child = srv.runtime  # FakeRuntime hands FakeChild; swap its process for the slow one
    slow = Slow()
    real_start = child.start

    def start(req):
        h = real_start(req)
        h.child.process = slow
        return h
    child.start = start
    oid = _sub_occurrence(rig)
    kid = srv.client(rig).spawn(S.SessionLaunch(oid, "s-0123456789abcdef", "claude", "sonnet", "b", "passthrough", EXP))
    kid.release()
    assert kid.process.poll() is None
    with pytest.raises(subprocess.TimeoutExpired):
        kid.process.wait(0.3)
    kid.process.terminate()                                                        # TERM reaches the server's gate
    assert kid.process.wait(5) == 0 and kid.process.poll() == 0


# --- (iv) the ticket and the run id are in no frame -------------------------------------------------------------------------------

def test_the_ticket_and_the_run_id_appear_in_no_frame_in_either_direction(rig, tmp_path):
    control = FakeControl(**{"RUN-TICKET-MINT": lambda f: {"ticket": SENT_TICKET, "run_id": SENT_RUN}})
    srv = Server(tmp_path, control=control)
    oid = _sub_occurrence(rig)
    client = srv.client(rig)
    _, report = _run(rig, oid, client, exposure=EXP)
    key = rig.row(oid)["session_key"]
    client.usage(key)
    client.end_session(key, 3)
    assert report.state == "done"
    # the positive control: the sentinels DID reach the server side, so their absence on the wire is a fact about the wire
    (start,) = srv.runtime.starts
    assert start.ticket == SENT_TICKET and start.run_id == SENT_RUN
    blob = b"".join(srv.wire)
    assert len(blob) > 1000
    for needle in (SENT_TICKET, SENT_RUN, "SENTINEL", "5e17e11e"):
        assert needle.encode() not in blob, needle
    frames = srv.frames()
    assert len(frames) >= 8

    def keys(o):
        if isinstance(o, dict):
            for k, v in o.items():
                yield k
                yield from keys(v)
        elif isinstance(o, list):
            for v in o:
                yield from keys(v)
    assert not {"ticket", "run_id", "attempt_id", "contract_digest", "brief_digest", "lease_nonce"} & set(
        k for f in frames for k in keys(f))


def test_the_codec_refuses_any_field_it_does_not_name(rig, srv):
    oid = _sub_occurrence(rig)
    proof = C.link_proof(rig.store, rig.world.root, oid, now=rig.now())
    launch = S.SessionLaunch(oid, "s-0123456789abcdef", "claude", "sonnet", "b", "passthrough", EXP)
    good = {"p": C.PROTOCOL, "op": "LAUNCH", "id": 1, **C.launch_to_wire(launch, proof)}
    assert C.launch_from_wire(good) == (launch, proof)
    for bad in ({**good, "ticket": SENT_TICKET}, {**good, "run_id": SENT_RUN},
                {**good, "launch": {**good["launch"], "ticket": SENT_TICKET}},
                {**good, "launch": {**good["launch"], "exposure": {**good["launch"]["exposure"], "run_id": 1}}},
                {**good, "proof": {**good["proof"], "occurrence_id": "oc-another"}},
                {**good, "launch": {**good["launch"], "exposure": {**good["launch"]["exposure"], "actions": True}}}):
        with pytest.raises(C.WireError):
            C.launch_from_wire(bad)


def test_no_string_in_the_client_names_a_mint_field():
    """A source pin (the S8b shape): outside docstrings the client holds no ticket, run id or mint verb literal."""
    tree = ast.parse(Path(C.__file__).read_text())
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef))
            and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    literals = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and id(n) not in docs]
    bad = [s for s in literals if any(w in s.lower() for w in ("ticket", "run_id", "run-ticket", "attempt_id", "mint"))]
    assert bad == [], bad


# --- the frame and the registration --------------------------------------------------------------------------------------------

def test_a_brief_at_its_cap_fits_in_one_frame_and_a_larger_frame_is_refused_before_any_connect(rig, srv):
    oid = _sub_occurrence(rig)
    proof = C.link_proof(rig.store, rig.world.root, oid, now=rig.now())
    worst = "\u0001" * S.BRIEF_MAX_BYTES                                           # six bytes each in canonical JSON
    C.encode_run_frame({"p": C.PROTOCOL, "op": "LAUNCH", "id": 1, **C.launch_to_wire(
        S.SessionLaunch(oid, "s-0123456789abcdef", "claude", "sonnet", worst, "passthrough", EXP), proof)})
    with pytest.raises(L.LauncherRefused) as e:
        srv.client(rig).spawn(S.SessionLaunch(oid, "s-0123456789abcdef", "claude", "sonnet", "x" * C.RUN_FRAME_MAX,
                                              "passthrough", EXP))
    assert e.value.name == "launch-frame-refused" and srv.wire == []
    with pytest.raises(C.WireError):
        C.encode_run_frame({"x": 1.5})                                              # no floats on this wire


def test_the_client_is_registered_and_resolves_from_the_install(rig, monkeypatch, tmp_path):
    assert W.LAUNCHERS[C.LAUNCHER_NAME] is C.build_launcher
    monkeypatch.setattr(C, "default_run_socket", lambda: tmp_path / "no-such.sock")
    W.write_install(rig.store.directory, cockpit_root=rig.world.root, launcher=C.LAUNCHER_NAME)
    got = W.resolve_launcher(rig.store, W.read_install(rig.store.directory))
    assert isinstance(got, C.DispatchSessionLauncher) and isinstance(got, S.SessionEnder)
    assert got.route == S.S1_DISPATCH and got.auth == "passthrough"
    oid = _sub_occurrence(rig)
    with pytest.raises(L.LauncherRefused) as e:
        got.spawn(S.SessionLaunch(oid, "s-0123456789abcdef", "claude", "sonnet", "b", "passthrough", EXP))
    assert e.value.name == "dispatch-unreachable"
    reaper = S.session_executor_of(rig.store, got, agent_id=rig.row(oid)["agent_id"])
    assert callable(getattr(reaper({"agent_id": rig.row(oid)["agent_id"], "session_key": "s-0123456789abcdef",
                                    "occurrence_id": oid}).launcher, "end_session"))


def test_a_recorded_exit_is_never_downgraded_by_a_later_abort_or_terminate(rig, srv):
    oid = _sub_occurrence(rig)
    kid = srv.client(rig).spawn(S.SessionLaunch(oid, "s-0123456789abcdef", "claude", "sonnet", "b", "passthrough", EXP))
    kid.release()
    assert kid.process.wait(5) == 0
    assert kid.abort() == 0 and kid.abort(float("nan")) == 0
    kid.process.terminate()
    assert kid.process.poll() == 0 and kid.process.wait() == 0


def test_an_end_never_outlives_its_one_absolute_deadline(rig):
    """A slow connect spends the caller's deadline: the answer wait gets only what remains (no deadline+margin on top)."""
    import time
    seen = {}

    class _Sock:
        def settimeout(self, t):
            seen["timeout"] = t

        def sendall(self, b):
            raise OSError("stop here")

        def shutdown(self, how):
            pass

        def close(self):
            pass

    def slow_connect():
        time.sleep(0.4)
        return _Sock()

    client = C.DispatchSessionLauncher(slow_connect, lambda occ: None)
    t0 = time.monotonic()
    got = client.end_session("s-0123456789abcdef", 1.0)
    assert got.terminated is False
    assert seen["timeout"] <= 0.61, seen
    assert time.monotonic() - t0 < 1.2


def test_an_end_whose_connect_spent_the_whole_deadline_asks_nothing(rig):
    import time
    asked = []

    def slow_connect():
        time.sleep(0.3)
        asked.append("connected")
        raise OSError("down")

    client = C.DispatchSessionLauncher(slow_connect, lambda occ: None)
    assert client.end_session("s-0123456789abcdef", 0.1).terminated is False
