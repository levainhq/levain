"""CONTRACT §3.1-§3.3, §7.2-§7.3: CONTROL's wire, state store and lease, over a REAL unix socket and REAL
flock. A test cannot be root or another uid, so the server's peer-uid reader is replaced by one that maps
each client's bound socket path to the uid it plays (root / the operator / a foreign uid); one test runs
the kernel's reader as-is. Cross-uid peer checks themselves are the VM's (session 2g)."""
from __future__ import annotations

import os
import shutil
import socket
import struct
import tempfile
import threading
import time
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import control as C
from levain.slot import records as R
from tests.test_slot_s2a_records import CONTRACT_EDGES

OP_UID = os.getuid() if os.getuid() != 0 else 501
FOREIGN_UID = OP_UID + 1000
QB_UID = OP_UID + 2000
ROLE_UID = {"root": 0, "op": OP_UID, "foreign": FOREIGN_UID, "qbroker": QB_UID}

S1, S2, S3, S4 = (f"s-{i:016x}" for i in range(1, 5))

#: Every §3.3 verb, session 2's and session 3c's.
ALL_VERBS = ("LEASE-OPEN", "STATUS", "ATTACH", "CAS", "MINT", "FRONT-ARM", "SECRETS", "REVOKE", "REVOKED-SET",
             "SLOT-SYNC", "TOKEN-RESOLVE", "LINEAGE-APPEND", "KEYS-ATTACH", "QUEUE-RESOLVE", "RUN-TICKET-MINT",
             "RUN-TICKET-CANCEL", "ROUTE-SUBMIT", "CONFIG-SUBMIT", "SESSION-EVENTS", "ARTIFACT-ACK", "POOL",
             "KEYS-STATUS", "NOT-A-VERB")
SESSION2 = {"operator": {"LEASE-OPEN", "STATUS", "KEYS-STATUS"},
            # S1 3c-7 adds root's SLOT-SYNC (CONTRACT §3.3 root row, §3.5 Pool)
            "root": {"ATTACH", "CAS", "MINT", "FRONT-ARM", "SECRETS", "REVOKE", "REVOKED-SET", "SLOT-SYNC"},
            "qbroker": {"QUEUE-RESOLVE"},
            # S1 3c-3 adds keys' attachment verbs (CONTRACT §4.5; 3c design r3 §2.1)
            # S1 3c-6b adds keys' LINEAGE-APPEND (CONTRACT §3.3 keys row; H6-2)
            "keys": {"TOKEN-RESOLVE", "KEYS-ATTACH", "FRONT-LOST", "SPEND-OVERRUN", "SESSIONS", "LINEAGE-APPEND"},
            # S1 3c-4 adds dispatch's ROUTE-SUBMIT (CONTRACT §4.3; 3c design r3 §2.3)
            "dispatch": {"ROUTE-SUBMIT",
                         # S1 3c-5 adds the ticket verbs (CONTRACT §3.3, §3.5)
                         "RUN-TICKET-MINT", "RUN-TICKET-CANCEL", "RUN-TICKET-STATUS",
                         # S1 3c-5 session 2: a ticket-bound lease (CONTRACT §3.1 step 1)
                         "LEASE-OPEN",
                         # S1 3c-7: the pool count (CONTRACT §3.3 dispatch row, §3.5 Pool)
                         "POOL",
                         # S1 3c-8: the verifier generations (CONTRACT §3.3 dispatch row, §3.5 VERIFIER IDENTITY)
                         "CONFIG-SUBMIT",
                         # S1 3c-8 slice 2: the event stream (CONTRACT §3.3 dispatch row, §3.6)
                         "SESSION-EVENTS",
                         # S1 3c-8 slice 2: the result's ack (CONTRACT §3.3 dispatch row, §3.5 Retention)
                         "ARTIFACT-ACK"}}


def attach_claims(**kw) -> dict:
    base = {"operator_uid": OP_UID, "supervisor_uid": OP_UID, "slot": {"class": "egress", "index": 0},
            "class": "egress", "kind": "interactive", "vendor": "claude", "auth": "brokered",
            "lease_seq": 1, "slot_rev": 1}
    base.update(kw)
    return base


def mint_claims(**kw) -> dict:
    base = {"routes": ["anthropic"], "queue_grant": True, "incarnation": 1, "egress_routes": [],
            "front": {"uid": 3989, "pid": 4242, "start": 1700000000}}
    base.update(kw)
    return base


class Seams:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.arm_refuse: str | None = None

    def drop_session(self, session):
        self.calls.append(("drop", session))

    def wake_reaper(self):
        self.calls.append(("wake",))

    def front_register(self, *a):
        self.calls.append(("register",) + a)

    def front_arm(self, session, reg_token):
        if self.arm_refuse:
            raise Refused(self.arm_refuse)
        self.calls.append(("arm", session))

    def kw(self) -> dict:
        return dict(drop_session=self.drop_session, wake_reaper=self.wake_reaper,
                    keys_front_register=self.front_register, keys_front_arm=self.front_arm)


class Client:
    def __init__(self, env: "Env", role: str) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        env.n += 1
        self.sock.bind(str(env.dir / f"{role}.{env.n}"))
        self.sock.connect(str(env.sock_path))
        self.sock.settimeout(5)
        self.next_id = 0

    def raw(self, data: bytes) -> None:
        self.sock.sendall(data)

    def ask(self, op: str, **args) -> dict:
        self.next_id += 1
        self.raw(C.encode_frame({"v": 1, "op": op, "id": self.next_id, **args}))
        ans = self.read()
        assert ans is not None and ans["id"] == self.next_id, ans
        return ans

    def read(self) -> dict | None:
        return C.read_frame(self.sock)

    def closed(self) -> bool:
        try:
            return self.sock.recv(1) == b""
        except (ConnectionResetError, BrokenPipeError):
            return True

    def close(self) -> None:
        self.sock.close()


class Env:
    def __init__(self, **control_kw) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="lvc", dir="/tmp"))   # short: AF_UNIX path limit
        self.store_dir = self.dir / "sessions"
        self.store_dir.mkdir(mode=0o700)
        self.sock_path = self.dir / "control.sock"
        self.n = 0
        self.clients: list[Client] = []
        self.seams = Seams()
        self.control_kw = control_kw
        self.server: C.ControlServer | None = None
        self.start()

    def peer(self, sock: socket.socket) -> int | None:
        name = Path(sock.getpeername()).name
        return ROLE_UID[name.split(".")[0]]

    def start(self) -> None:
        self.store = C.StateStore(self.store_dir)
        kw = {"qbroker_uid": QB_UID, **self.seams.kw(), **self.control_kw}
        self.control = C.Control(self.store, OP_UID, **kw)
        self.server = C.ControlServer(self.control, self.sock_path, peer_uid=self.peer)
        self.server.start()

    def restart(self) -> None:
        self.server.stop()
        self.start()

    def client(self, role: str) -> Client:
        c = Client(self, role)
        self.clients.append(c)   # a dropped Client would close its socket: a lease EOF the test did not ask for
        return c

    def record(self, session: str) -> R.SessionState:
        return R.parse_state((self.store_dir / f"{session}.json").read_bytes())

    def close(self) -> None:
        if self.server:
            self.server.stop()
        shutil.rmtree(self.dir, ignore_errors=True)


@pytest.fixture
def env():
    e = Env()
    yield e
    e.close()


def settle(env: Env, session: str, state: str, timeout: float = 5.0) -> R.SessionState:
    """Wait for the server to notice a client's close and for the revoke to land."""
    deadline = time.time() + timeout
    while True:
        p = env.store_dir / f"{session}.json"
        rec = env.record(session) if p.exists() else None
        if rec is not None and rec.state == state:
            env.control.join_eof(timeout)
            return env.record(session)
        assert time.time() < deadline, (session, rec and rec.state)
        time.sleep(0.01)


def lease(env: Env) -> tuple[Client, str]:
    op = env.client("op")
    ans = op.ask("LEASE-OPEN")
    assert ans["ok"], ans
    return op, ans["lease_nonce"]


def attach(env: Env, session: str = S1, **claims_kw) -> tuple[Client, Client]:
    op, nonce = lease(env)
    root = env.client("root")
    ans = root.ask("ATTACH", lease_nonce=nonce, sudo_uid=OP_UID, session=session, claims=attach_claims(**claims_kw))
    assert ans == {"id": ans["id"], "ok": True, "generation": 0}, ans
    return op, root


def cas(root: Client, session: str, gen: int, frm: str, to: str, **fields) -> dict:
    return root.ask("CAS", session=session, generation=gen, **{"from": frm, "to": to}, fields=fields)


def to_rendering(env: Env, session: str = S1, **claims_kw) -> tuple[Client, Client]:
    op, root = attach(env, session, **claims_kw)
    assert cas(root, session, 0, "ALLOCATED", "RENDERING")["ok"]
    return op, root


def to_rendered(env: Env, session: str = S1, **mint_kw) -> tuple[Client, Client]:
    op, root = to_rendering(env, session)
    gen = root.ask("MINT", session=session, generation=1, claims=mint_claims(**mint_kw))["generation"]
    assert root.ask("FRONT-ARM", session=session, generation=gen)["ok"]
    assert cas(root, session, gen, "RENDERING", "RENDERED")["ok"]
    return op, root


# --- framing ---------------------------------------------------------------------------------------------

def test_frame_round_trip_socketpair():
    a, b = socket.socketpair()
    a.sendall(C.encode_frame({"v": 1, "op": "X", "id": 3}))
    assert C.read_frame(b) == {"v": 1, "op": "X", "id": 3}
    a.close()
    assert C.read_frame(b) is None
    b.close()


def test_frame_over_length_refused_then_closed(env):
    c = env.client("op")
    c.raw(struct.pack(">I", C.FRAME_MAX + 1))
    assert c.read() == {"id": None, "ok": False, "refused": "frame-too-large"}
    assert c.closed()
    # the cap itself is accepted
    c2 = env.client("op")
    pad = "x" * (C.FRAME_MAX - len(canon.canonical_bytes({"v": 1, "op": "STATUS", "id": 1, "pad": ""})))
    data = canon.canonical_bytes({"v": 1, "op": "STATUS", "id": 1, "pad": pad})
    assert len(data) == C.FRAME_MAX
    c2.raw(struct.pack(">I", len(data)) + data)
    assert c2.read() == {"id": 1, "ok": False, "refused": "unknown-key"}


@pytest.mark.parametrize("payload", [
    b"not json", b'{"v": 1, "op": "STATUS", "id": 1}', b'["v"]', b'{"id":1,"op":"STATUS","v":2}',
    b'{"id":1,"op":"STATUS"}', b'{"id":true,"op":"STATUS","v":1}', b'{"id":1,"op":7,"v":1}',
    b'{"id":1.5,"op":"STATUS","v":1}', b'{"id":1,"id":2,"op":"STATUS","v":1}', b"",
])
def test_malformed_frame_closes_without_answer(env, payload):
    c = env.client("op")
    c.raw(struct.pack(">I", len(payload)) + payload)
    assert c.closed()


def test_eof_inside_frame_closes(env):
    c = env.client("op")
    c.raw(struct.pack(">I", 50) + b'{"v":1')
    c.sock.shutdown(socket.SHUT_WR)
    assert c.closed()


def test_malformed_frame_on_lease_is_its_eof(env):
    op, root = attach(env)
    op.raw(struct.pack(">I", 3) + b"bad")
    assert op.closed()
    settle(env, S1, "REVOKED")


# --- the verb matrix ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("role,kind", [("op", "operator"), ("root", "root"), ("qbroker", "qbroker"), ("foreign", None)])
def test_verb_not_allowed_for_peer_matrix(env, role, kind):
    c = env.client(role)
    for verb in ALL_VERBS:
        ans = c.ask(verb)
        allowed = verb in SESSION2.get(kind, set())
        if allowed:
            assert ans.get("refused") != "verb-not-allowed-for-peer", (role, verb, ans)
        else:
            assert ans == {"id": ans["id"], "ok": False, "refused": "verb-not-allowed-for-peer"}, (role, verb)


def test_peer_table_is_session2():
    assert {k: set(v) for k, v in C.PEER_VERBS.items() if v} == SESSION2
    assert C.PEER_VERBS["foreign"] == frozenset()


def test_kernel_peer_uid_names_the_operator():
    """No override: the kernel's peer credential is this process's uid, which plays the operator."""
    d = Path(tempfile.mkdtemp(prefix="lvk", dir="/tmp"))
    try:
        (d / "s").mkdir()
        ctl = C.Control(C.StateStore(d / "s"), OP_UID)
        srv = C.ControlServer(ctl, d / "c.sock")
        srv.start()
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(str(d / "c.sock"))
            s.sendall(C.encode_frame({"v": 1, "op": "LEASE-OPEN", "id": 1}))
            ans = C.read_frame(s)
            if os.getuid() == OP_UID:
                assert ans["ok"] and len(ans["lease_nonce"]) == 43
            s.sendall(C.encode_frame({"v": 1, "op": "REVOKED-SET", "id": 2}))
            assert C.read_frame(s)["refused"] == "verb-not-allowed-for-peer"
            s.close()
        finally:
            srv.stop()
        assert stat_mode(d / "c.sock") == 0o660
    finally:
        shutil.rmtree(d, ignore_errors=True)


def stat_mode(p: Path) -> int:
    return os.stat(p).st_mode & 0o7777


def test_bad_args_refused_by_name(env):
    root = env.client("root")
    assert root.ask("REVOKE")["refused"] == "missing-key"
    assert root.ask("REVOKE", session=S1, extra=1)["refused"] == "unknown-key"
    assert root.ask("REVOKE", session="s-nope")["refused"] == "bad-token"
    assert env.client("op").ask("LEASE-OPEN", ticket="x")["refused"] == "ticket-not-allowed"   # the operator holds none


# --- the lease and ATTACH (§3.1) ---------------------------------------------------------------------------

def test_lease_nonce_shape(env):
    _, nonce = lease(env)
    assert len(nonce) == 43 and "=" not in nonce


def test_attach_creates_allocated_record(env):
    attach(env)
    rec = env.record(S1)
    assert (rec.generation, rec.state, rec.lease_live, rec.event_kind) == (0, "ALLOCATED", True, "state")
    assert rec.claims == attach_claims()
    assert stat_mode(env.store_dir / f"{S1}.json") == 0o600


def test_nonce_foreign_uid_refused(env):
    _, nonce = lease(env)
    root = env.client("root")
    ans = root.ask("ATTACH", lease_nonce=nonce, sudo_uid=FOREIGN_UID, session=S1,
                   claims=attach_claims(supervisor_uid=FOREIGN_UID))
    assert ans["refused"] == "nonce-uid-mismatch"
    assert not (env.store_dir / f"{S1}.json").exists()
    # the nonce was not spent by the refusal: the right uid still attaches
    assert root.ask("ATTACH", lease_nonce=nonce, sudo_uid=OP_UID, session=S1, claims=attach_claims())["ok"]


def test_nonce_replay_refused(env):
    op, nonce = lease(env)
    root = env.client("root")
    assert root.ask("ATTACH", lease_nonce=nonce, sudo_uid=OP_UID, session=S1, claims=attach_claims())["ok"]
    ans = root.ask("ATTACH", lease_nonce=nonce, sudo_uid=OP_UID, session=S2, claims=attach_claims())
    assert ans["refused"] == "nonce-used"
    assert not (env.store_dir / f"{S2}.json").exists()
    ans = root.ask("ATTACH", lease_nonce=C.new_handle(), sudo_uid=OP_UID, session=S2, claims=attach_claims())
    assert ans["refused"] == "nonce-unknown"


def test_attach_after_lease_eof_refused(env):
    op, nonce = lease(env)
    op.close()
    root = env.client("root")
    deadline = time.time() + 5
    while time.time() < deadline:
        ans = root.ask("ATTACH", lease_nonce=nonce, sudo_uid=OP_UID, session=S1, claims=attach_claims())
        if ans.get("refused") == "lease-dead":
            break
        time.sleep(0.01)
    assert ans["refused"] == "lease-dead"


@pytest.mark.parametrize("claims", [
    attach_claims(supervisor_uid=OP_UID + 1),                 # supervisor_uid == lease peer
    attach_claims(operator_uid=OP_UID + 1),                   # the configured operator
    attach_claims(ticket=C.new_handle()),                     # an operator lease carries no ticket
    attach_claims(kind="dispatch"),
])
def test_attach_claims_mismatch(env, claims):
    _, nonce = lease(env)
    ans = env.client("root").ask("ATTACH", lease_nonce=nonce, sudo_uid=OP_UID, session=S1, claims=claims)
    assert ans["refused"] == "claims-mismatch"


def test_attach_existing_session_refused(env):
    attach(env, S1)
    _, nonce = lease(env)
    ans = env.client("root").ask("ATTACH", lease_nonce=nonce, sudo_uid=OP_UID, session=S1, claims=attach_claims())
    assert ans["refused"] == "session-exists"


def test_second_lease_open_on_live_lease_refused(env):
    op, _ = lease(env)
    assert op.ask("LEASE-OPEN")["refused"] == "lease-exists"
    other, _ = lease(env)                      # another connection is another lease
    assert other is not op


# --- CAS (§7.2) -----------------------------------------------------------------------------------------

def test_stale_generation_refused(env):
    _, root = attach(env)
    assert cas(root, S1, 0, "ALLOCATED", "RENDERING") ["generation"] == 1
    ans = cas(root, S1, 0, "RENDERING", "RENDERED")
    assert ans["refused"] == "stale-generation"
    assert env.record(S1).state == "RENDERING"
    ans = cas(root, S1, 5, "RENDERING", "RENDERED")
    assert ans["refused"] == "stale-generation"


def test_cas_from_mismatch_refused(env):
    _, root = attach(env)
    assert cas(root, S1, 0, "RENDERING", "RENDERED")["refused"] == "from-mismatch"


def test_cas_already_done_counts_as_done(env):
    """§7.4: a crash between the CAS and the intent delete re-sends the CAS with its stale generation."""
    _, root = attach(env)
    assert cas(root, S1, 0, "ALLOCATED", "RENDERING")["generation"] == 1
    assert cas(root, S1, 0, "ALLOCATED", "RENDERING") == {"id": root.next_id, "ok": True, "generation": 1}
    assert env.record(S1).generation == 1


def test_cas_done_rule_needs_the_fields_too(env):
    _, root = attach(env)
    root.ask("REVOKE", session=S1)
    assert cas(root, S1, 1, "REVOKED", "HELD", failed_step="freeze")["generation"] == 2
    ans = cas(root, S1, 1, "REVOKED", "HELD", failed_step="kill")
    assert ans["refused"] == "stale-generation"


@pytest.mark.parametrize("field", ["claims", "artifact_acked", "lease_live", "state", "generation",
                                   "intent", "trashed_at", "event_kind"])
def test_field_not_casable(env, field):
    _, root = attach(env)
    ans = cas(root, S1, 0, "ALLOCATED", "RENDERING", **{field: True})
    assert ans["refused"] == "field-not-casable"
    assert env.record(S1).generation == 0


def test_cas_field_only_and_event_kind(env):
    _, root = attach(env)
    root.ask("REVOKE", session=S1)
    g = cas(root, S1, 1, "REVOKED", "HELD", failed_step="freeze", held_attempts=1)["generation"]
    rec = env.record(S1)
    assert (rec.state, rec.event_kind, rec.fields["failed_step"]) == ("HELD", "state", "freeze")
    g = cas(root, S1, g, "HELD", "HELD", held_attempts=2)["generation"]
    rec = env.record(S1)
    assert (rec.event_kind, rec.fields["held_attempts"], rec.fields["failed_step"]) == ("none", 2, "freeze")
    g = cas(root, S1, g, "HELD", "HELD", failed_step=None)["generation"]
    assert "failed_step" not in env.record(S1).fields
    for frm, to in (("HELD", "RETAINED"),):
        g = cas(root, S1, g, frm, to)["generation"]
    g = cas(root, S1, g, "RETAINED", "RETAINED", artifact_digest="sha256:" + "a" * 64)["generation"]
    assert env.record(S1).event_kind == "artifact"
    assert cas(root, S1, g, "RETAINED", "TRASH", artifact_failed="x")["refused"] == "edge-not-allowed"
    assert cas(root, S1, g, "RETAINED", "RETAINED")["refused"] == "edge-not-allowed"   # nothing to write
    assert cas(root, S1, g, "RETAINED", "RETAINED", artifact_failed="cap")["refused"] == "bad-value"


def test_cas_field_values_checked(env):
    _, root = attach(env)
    root.ask("REVOKE", session=S1)
    assert cas(root, S1, 1, "REVOKED", "HELD", failed_step="Has Space")["refused"] == "bad-value"
    assert cas(root, S1, 1, "REVOKED", "HELD", held_attempts=-1)["refused"] == "out-of-range"


def test_cas_unknown_session_and_locked(env):
    root = env.client("root")
    assert cas(root, S1, 0, "ALLOCATED", "RENDERING")["refused"] == "no-session"
    attach(env, S2)
    with env.store.lock(S2):
        assert cas(root, S2, 0, "ALLOCATED", "RENDERING")["refused"] == "locked"
    assert cas(root, S2, 0, "ALLOCATED", "RENDERING")["ok"]


# The edge table, both signs, through the real CAS: every (from, to) pair of distinct states.

def _seed(store: C.StateStore, session: str, state: str) -> None:
    with store.lock(session):
        store.put(R.SessionState(session=session, generation=4, state=state, claims=attach_claims(),
                                 lease_live=True, event_kind="state", updated_at="2026-10-20T00:00:00Z"))


def test_cas_edge_table_both_signs(tmp_path):
    store = C.StateStore(tmp_path)
    ctl = C.Control(store, OP_UID)
    i = 0
    for frm in R.STATES:
        for to in R.STATES:
            if frm == to:
                continue
            i += 1
            session = f"s-{i:016x}"
            _seed(store, session, frm)
            ctl._session_lease[session] = C.Lease(C.new_handle(), OP_UID)   # a live lease
            try:
                g = ctl.cas(session, 4, frm, to, {})
                got = True
                assert g == 5 and store.get(session).state == to
            except Refused as e:
                assert e.name == "edge-not-allowed", (frm, to, e.name)
                got = False
                assert store.get(session).generation == 4
            assert got == ((frm, to) in CONTRACT_EDGES), (frm, to)
    assert i == len(R.STATES) * (len(R.STATES) - 1)


def test_cas_into_live_state_needs_live_lease(tmp_path):
    store = C.StateStore(tmp_path)
    ctl = C.Control(store, OP_UID)
    _seed(store, S1, "ALLOCATED")
    with pytest.raises(Refused) as ei:
        ctl.cas(S1, 4, "ALLOCATED", "RENDERING", {})       # no lease bound in this process
    assert ei.value.name == "lease-dead"
    assert ctl.cas(S1, 4, "ALLOCATED", "REVOKED", {}) == 5  # leaving the live states needs none


# --- lease EOF (§3.1 step 3) -------------------------------------------------------------------------------

def test_lease_eof_revokes_lease_live_first(env):
    writes = []
    real_put = env.store.put

    def put(rec):
        writes.append((rec.generation, rec.state, rec.lease_live, rec.event_kind))
        real_put(rec)

    env.store.put = put
    op, root = to_rendering(env)
    writes.clear()
    op.close()
    settle(env, S1, "REVOKED")
    assert writes == [(2, "RENDERING", False, "none"), (3, "REVOKED", False, "state")]
    assert env.seams.calls[-2:] == [("wake",), ("drop", S1)]          # the reaper first, never behind keys


def test_lease_eof_meets_held_lock(env):
    """EOF while another holder has the session lock: the revoke is never given up, and REVOKED lands once it frees.
    keys hears the drop at the first failed attempt (group L3 r1, complement MED 1) and again after REVOKED."""
    op, root = attach(env)
    holder = env.store.lock(S1)
    holder.__enter__()
    try:
        op.close()
        deadline = time.time() + 2
        while time.time() < deadline and not env.control._eof_threads:
            time.sleep(0.01)
        time.sleep(0.2)                                    # several retry rounds pass
        rec = env.record(S1)
        assert (rec.state, rec.lease_live, rec.generation) == ("ALLOCATED", True, 0)
        assert env.seams.calls.count(("drop", S1)) == 1                # the early drop, while the lock is held
        assert cas(root, S1, 0, "ALLOCATED", "RENDERING")["refused"] == "locked"
    finally:
        holder.__exit__(None, None, None)
    env.control.join_eof(5)
    rec = env.record(S1)
    assert (rec.state, rec.lease_live, rec.generation) == ("REVOKED", False, 2)
    assert env.seams.calls.count(("drop", S1)) == 2


def test_mint_refused_after_lease_eof(env):
    op, root = to_rendering(env)
    op.close()
    settle(env, S1, "REVOKED")
    assert root.ask("MINT", session=S1, generation=3, claims=mint_claims())["refused"] == "lease-dead"
    assert root.ask("FRONT-ARM", session=S1, generation=3)["refused"] == "lease-dead"
    assert root.ask("SECRETS", session=S1)["refused"] == "lease-dead"
    assert not any(c[0] == "register" for c in env.seams.calls)


def test_lease_eof_before_attach_creates_nothing(env):
    op, nonce = lease(env)
    op.close()
    time.sleep(0.1)
    env.control.join_eof(5)
    assert list(env.store_dir.glob("*.json")) == []


def test_lease_eof_drops_handles(env):
    op, root = to_rendered(env)
    assert env.control.handles(S1).token is not None
    op.close()
    settle(env, S1, "REVOKED")
    assert env.control.handles(S1) is None


# --- MINT, FRONT-ARM, SECRETS -------------------------------------------------------------------------------

def test_mint_registers_front_then_mints(env):
    op, root = to_rendering(env)
    ans = root.ask("MINT", session=S1, generation=1, claims=mint_claims())
    assert ans["generation"] == 3                                   # the token-only prewrite is a generation of its own
    assert ("register", S1, 3989, 4242, 1700000000, 1, 1) in env.seams.calls
    rec = env.record(S1)
    assert rec.claims == {**attach_claims(), **mint_claims(), "reg_token": 1} and rec.event_kind == "none"
    h = env.control.handles(S1)
    assert len(h.token) == 43 and len(h.queue_cap) == 43 and h.token != h.queue_cap
    for k in ("token", "queue_cap", "lease_nonce"):
        assert k not in canon.parse((env.store_dir / f"{S1}.json").read_bytes())


def test_mint_handles_follow_claims(env):
    op, root = to_rendering(env, S1, auth="passthrough")
    root.ask("MINT", session=S1, generation=1, claims=mint_claims(queue_grant=False))
    h = env.control.handles(S1)
    assert h.token is None and h.queue_cap is None


def test_mint_refusals(env):
    op, root = attach(env)
    assert root.ask("MINT", session=S1, generation=0, claims=mint_claims())["refused"] == "not-rendering"
    cas(root, S1, 0, "ALLOCATED", "RENDERING")
    assert root.ask("MINT", session=S1, generation=0, claims=mint_claims())["refused"] == "stale-generation"
    c = mint_claims()
    del c["front"]
    assert root.ask("MINT", session=S1, generation=1, claims=c)["refused"] == "claims-mismatch"
    assert root.ask("MINT", session=S1, generation=1, claims=mint_claims())["ok"]
    assert root.ask("MINT", session=S1, generation=3, claims=mint_claims())["refused"] == "already-minted"
    assert root.ask("MINT", session=S2, generation=0, claims=mint_claims())["refused"] == "no-session"


def test_mint_none_class_has_no_front(env):
    op, root = to_rendering(env, S1, slot={"class": "none", "index": 0}, **{"class": "none"})
    assert root.ask("MINT", session=S1, generation=1, claims=mint_claims())["refused"] == "claims-mismatch"
    c = mint_claims(routes=[], queue_grant=False)
    del c["front"]
    assert root.ask("MINT", session=S1, generation=1, claims=c)["ok"]
    assert root.ask("FRONT-ARM", session=S1, generation=2)["refused"] == "not-rendering"


def test_mint_fails_closed_without_keys():
    e = Env(keys_front_register=C._keys_unavailable)
    try:
        op, root = to_rendering(e)
        assert root.ask("MINT", session=S1, generation=1, claims=mint_claims())["refused"] == "keys-unavailable"
        rec = e.record(S1)                                          # TOKEN-ONLY: authorises nothing
        assert rec.generation == 2 and rec.claims == {**attach_claims(), "reg_token": 1}
        assert e.control.handles(S1) is None
    finally:
        e.close()


def test_front_arm_relays_and_answers_after(env):
    op, root = to_rendering(env)
    assert root.ask("FRONT-ARM", session=S1, generation=1)["refused"] == "not-rendering"   # before MINT
    root.ask("MINT", session=S1, generation=1, claims=mint_claims())
    assert root.ask("FRONT-ARM", session=S1, generation=1)["refused"] == "stale-generation"
    assert root.ask("FRONT-ARM", session=S1, generation=3) == {"id": root.next_id, "ok": True, "generation": 3}
    assert env.seams.calls[-1] == ("arm", S1)
    env.seams.arm_refuse = "keys-unavailable"
    assert root.ask("FRONT-ARM", session=S1, generation=3)["refused"] == "keys-unavailable"


def test_secrets_before_rendered_refused(env):
    op, root = attach(env)
    assert root.ask("SECRETS", session=S1)["refused"] == "not-rendered"
    cas(root, S1, 0, "ALLOCATED", "RENDERING")
    gen = root.ask("MINT", session=S1, generation=1, claims=mint_claims())["generation"]
    assert root.ask("SECRETS", session=S1)["refused"] == "not-rendered"
    root.ask("FRONT-ARM", session=S1, generation=gen)
    cas(root, S1, gen, "RENDERING", "RENDERED")
    ans = root.ask("SECRETS", session=S1)
    h = env.control.handles(S1)
    assert ans == {"id": ans["id"], "ok": True, "token": h.token, "queue_cap": h.queue_cap}
    cas(root, S1, gen + 1, "RENDERED", "ACTIVE")
    assert root.ask("SECRETS", session=S1)["refused"] == "not-rendered"
    assert op.ask("SECRETS", session=S1)["refused"] == "verb-not-allowed-for-peer"


# --- REVOKE, REVOKED-SET, STATUS -------------------------------------------------------------------------------

def test_alloc_only_orphan_reclaimed(env):
    """§3.1: an alloc with no record -> the reaper's REVOKE creates the record directly at REVOKED."""
    root = env.client("root")
    assert root.ask("REVOKE", session=S3)["generation"] == 0
    rec = env.record(S3)
    assert (rec.state, rec.generation, rec.claims, rec.lease_live, rec.event_kind) == ("REVOKED", 0, {}, False, "state")
    assert env.seams.calls == [("wake",), ("wake",), ("drop", S3)]   # the start's wake, then REVOKE's
    assert root.ask("REVOKE", session=S3)["generation"] == 0          # a no-op answering its generation
    assert env.seams.calls == [("wake",), ("wake",), ("drop", S3)]
    assert S3 in root.ask("REVOKED-SET")["sessions"]
    assert cas(root, S3, 0, "REVOKED", "FROZEN")["generation"] == 1    # END runs from alloc as usual


def test_revoke_live_and_past_live(env):
    op, root = to_rendered(env)
    assert root.ask("REVOKE", session=S1)["generation"] == 5
    assert env.record(S1).state == "REVOKED"
    g = 5
    for frm, to in (("REVOKED", "FROZEN"), ("FROZEN", "DEAD"), ("DEAD", "CREDENTIAL_PARKED"),
                    ("CREDENTIAL_PARKED", "RETAINED")):
        g = cas(root, S1, g, frm, to)["generation"]
    assert root.ask("REVOKE", session=S1)["generation"] == g           # past REVOKED: no-op
    assert env.record(S1).state == "RETAINED"


def test_revoked_set_definition():
    reusable = {}
    e = Env(slot_reusable=lambda c, i, s: reusable.get(s, False))
    try:
        root = e.client("root")
        attach(e, S1)                                           # live: never in the set
        _, r2 = attach(e, S2, slot={"class": "egress", "index": 1})
        r2.ask("REVOKE", session=S2)
        g = 1
        for frm, to in (("REVOKED", "FROZEN"), ("FROZEN", "DEAD"), ("DEAD", "CREDENTIAL_PARKED"),
                        ("CREDENTIAL_PARKED", "RETAINED")):
            g = cas(root, S2, g, frm, to)["generation"]
        root.ask("REVOKE", session=S3)                          # REVOKED, no claims
        _, r4 = attach(e, S4, slot={"class": "egress", "index": 2})
        r4.ask("REVOKE", session=S4)
        cas(root, S4, 1, "REVOKED", "HELD", failed_step="kill")
        assert root.ask("REVOKED-SET")["sessions"] == sorted([S2, S3, S4])
        reusable[S2] = True                                     # the mirror reports the slot reusable
        assert root.ask("REVOKED-SET")["sessions"] == sorted([S3, S4])
        cas(root, S2, g, "RETAINED", "TRASH")
        reusable[S2] = False
        assert root.ask("REVOKED-SET")["sessions"] == sorted([S3, S4])   # TRASH is past the revoker
    finally:
        e.close()


def test_status_operator_records_only(env):
    op, root = attach(env, S1)
    root.ask("REVOKE", session=S3)                              # no claims: nobody's
    recs = op.ask("STATUS")["records"]
    assert [r["session"] for r in recs] == [S1]
    assert op.ask("STATUS", session=S3)["records"] == []
    assert op.ask("STATUS", session=S1)["records"][0]["state"] == "ALLOCATED"


# --- CONTROL restart (§1 CONTROL row, §1.3) ------------------------------------------------------------------

def test_control_restart_ends_sessions_retained(env):
    _, r1 = attach(env, S1)                                       # ALLOCATED
    to_rendered(env, S2)                                          # RENDERED
    _, r3 = to_rendered(env, S3)
    cas(r3, S3, 3, "RENDERED", "ACTIVE")                          # ACTIVE
    _, r4 = attach(env, S4)
    r4.ask("REVOKE", session=S4)
    g = 1
    for frm, to in (("REVOKED", "FROZEN"), ("FROZEN", "DEAD"), ("DEAD", "CREDENTIAL_PARKED"),
                    ("CREDENTIAL_PARKED", "RETAINED")):
        g = cas(r4, S4, g, frm, to)["generation"]
    before = {s: env.record(s) for s in (S1, S2, S3, S4)}
    env.seams.calls.clear()
    env.server.stop()                                             # as a crash looks: nothing revoked
    assert env.seams.calls == []
    assert {s: env.record(s) for s in (S1, S2, S3, S4)} == before
    env.start()                                                   # the old process's leases are gone
    for s in (S1, S2, S3):
        rec = env.record(s)
        assert (rec.state, rec.lease_live) == ("REVOKED", False), s
        assert rec.generation == before[s].generation + 2          # lease_live:false, then REVOKED
        assert rec.claims == before[s].claims                      # work retained: END is the reaper's
    rec4 = env.record(S4)
    assert rec4.state == "RETAINED" and rec4.lease_live is False
    assert rec4.generation == before[S4].generation + 1            # its stale lease_live corrected only
    assert sorted(c[1] for c in env.seams.calls if c[0] == "drop") == [S1, S2, S3, S4]   # S4: past live, owed again
    root = env.client("root")
    assert root.ask("SECRETS", session=S2)["refused"] == "lease-dead"
    assert env.control.handles(S2) is None


def test_lease_eof_drops_at_keys_while_the_revoke_cannot_land(env):
    """Group L3 r1 (complement MED 1), reproduced first (``1022-8/g1fix/item4_repro_before.txt``): a lease EOF dropped
    CONTROL's handles at once, but keys heard nothing until REVOKED was durable, so while the store write kept failing
    keys kept the token cached and live exchanges kept injecting the credential. Now the revoke's first failed attempt
    sends ``DROP-SESSION`` (idempotent) on its own thread; the drop after the durable REVOKED still follows."""
    op, root = to_rendered(env)
    real_put = env.store.put
    failing = threading.Event()
    failing.set()

    def put(rec):
        if failing.is_set():
            raise OSError(28, "No space left on device")
        real_put(rec)

    env.store.put = put
    env.seams.calls.clear()
    op.close()
    deadline = time.time() + 3
    while time.time() < deadline and ("drop", S1) not in env.seams.calls:
        time.sleep(0.01)
    rec = env.record(S1)
    assert rec.state == "RENDERED"                                     # the revoke has not landed
    assert ("drop", S1) in env.seams.calls                             # keys heard it anyway
    failing.clear()
    rec = settle(env, S1, "REVOKED")
    assert env.seams.calls[-2:] == [("wake",), ("drop", S1)]           # the durable path's own drop still runs


def test_lease_eof_revoke_survives_a_thread_that_cannot_start(env):
    """Group L3 r2 (codex MED): the early drop's thread failing to start (thread exhaustion) raised out of the durable
    revoke's retry loop, so the revoke worker died, REVOKED never landed and keys was never told. Now the spawn failure
    is recorded, the loop keeps retrying (and retries the early drop), and the durable path still revokes and drops."""
    op, root = to_rendered(env)
    real_put = env.store.put
    real_start = threading.Thread.start
    failing = threading.Event()
    failing.set()

    def put(rec):
        if failing.is_set():
            raise OSError(28, "No space left on device")
        real_put(rec)

    def start(self):
        if self.name.startswith("control-eofdrop-"):
            raise RuntimeError("can't start new thread")
        return real_start(self)

    env.store.put = put
    threading.Thread.start = start
    try:
        env.seams.calls.clear()
        op.close()
        deadline = time.time() + 3
        while time.time() < deadline and not any(e[0] == "eofdrop" for e in env.control.seam_errors):
            time.sleep(0.01)
        assert any(e[0] == "eofdrop" and e[1] == S1 for e in env.control.seam_errors)
        assert not any(t.name.startswith("control-eofdrop-") for t in env.control._eof_threads)
        failing.clear()
        settle(env, S1, "REVOKED")                                     # the revoke worker is still alive
    finally:
        threading.Thread.start = real_start
    assert env.seams.calls[-1] == ("drop", S1)                         # keys hears it on the durable path
