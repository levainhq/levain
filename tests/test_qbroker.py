"""F10 S1: the queue broker (levain.qbroker), against a fake CONTROL resolver.

Keys are real ssh-keygen keys; the answer-signer snapshot is signed by a real trust-root key in
levain-allowed-signers; answers are real SSHSIGs in levain-consent-answer. Sockets live under a short /tmp dir
(macOS caps sun_path at 104 bytes). Peer-uid checks are injected where a test runs as one uid, except the one test
that holds a real socket to a wrong expected uid.
"""
from __future__ import annotations

import os
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.cockpit.consent import utc_stamp
from levain.consent.record import ANSWER_NAMESPACE, answer_message, answer_payload, tool_input_digest
from levain.contracts.canon import canonical_bytes
from levain.qbroker import wire
from levain.qbroker.broker import Broker, Resolved
from levain.qbroker.control import ControlResolver
from levain.qbroker.seam import answer_refusal, request_of
from levain.qbroker.server import SlotListener, peer_uid
from levain.qbroker.signers import SignersSource
from levain.qbroker.store import ANSWER_ROW_FORMAT, QueueStore

NOW = datetime.now(timezone.utc).replace(microsecond=0)
HEAD = {"size": 0, "root": "0" * 64}
D64 = "sha256:" + "d" * 64


class Key:
    def __init__(self, d: Path, name: str) -> None:
        self.path = d / name
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(self.path)],
                       check=True, capture_output=True, timeout=30)
        self.type, self.b64 = (d / f"{name}.pub").read_text().split()[:2]
        out = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(self.path)], check=True,
                             capture_output=True, text=True).stdout
        self.fp = out.split()[1]

    def sign(self, message: bytes, namespace: str) -> str:
        return subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(self.path), "-n", namespace],
                              input=message, capture_output=True, check=True, timeout=30).stdout.decode()

    def signer(self, namespaces=(ANSWER_NAMESPACE,), key_class="display-phone", custody="unstated") -> dict:
        return {"principal": self.path.name, "key_type": self.type, "key_b64": self.b64, "key_class": key_class,
                "custody": custody, "namespaces": list(namespaces)}


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    d = tmp_path_factory.mktemp("qbkeys")
    return {n: Key(d, n) for n in ("trust", "operator", "stranger")}


class FakeResolver:
    def __init__(self) -> None:
        self.caps: dict[tuple[str, int, str], Resolved] = {}
        self.calls: list[tuple[dict, str]] = []

    def add(self, slot: int, cap: str, resolved: Resolved) -> None:
        import hashlib
        self.caps[("egress", slot, "sha256:" + hashlib.sha256(cap.encode()).hexdigest())] = resolved

    def queue_resolve(self, slot: int, cap_digest: str):
        self.calls.append((slot, cap_digest))
        return self.caps.get((slot["class"], slot["index"], cap_digest), "cap-unknown")


def write_signers(root: Path, keys, generation: int, signers=None, *, sign_with="trust",
                  namespace="levain-allowed-signers") -> None:
    record = {"format": "levain-allowed-signers/1", "generation": generation, "issued_at": utc_stamp(NOW),
              "signers": signers if signers is not None else [keys["operator"].signer()]}
    body = canonical_bytes(record)
    sig = keys[sign_with].sign(body, namespace)
    tmp_body, tmp_sig = root / "policy" / ".b", root / "policy" / ".s"
    tmp_body.write_bytes(body)
    tmp_sig.write_text(sig)
    os.replace(tmp_body, root / "policy" / "allowed_signers.json")
    os.replace(tmp_sig, root / "policy" / "allowed_signers.json.sig")


@pytest.fixture
def L(tmp_path, keys):
    for d in ("queue/requests", "queue/answers", "policy", "routes"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "routes" / "trust.pub").write_text(f"{keys['trust'].type} {keys['trust'].b64} trust\n")
    write_signers(tmp_path, keys, 1)
    return tmp_path


@pytest.fixture
def clock():
    state = {"now": NOW}
    state["fn"] = lambda: state["now"]
    return state


SESSION_A = Resolved(session="sess-a", kind="interactive", run=None)
SESSION_B = Resolved(session="sess-b", kind="interactive", run=None)
RUN = {"run_id": "run-1", "job_id": "job-1", "run_index": 0, "contract_digest": "sha256:" + "c" * 64,
       "model_requested": "sonnet"}


@pytest.fixture
def resolver():
    r = FakeResolver()
    r.add(1, "cap-a", SESSION_A)
    r.add(1, "cap-a2", SESSION_B)
    r.add(2, "cap-two", Resolved(session="sess-2", kind="dispatch", run=RUN))
    return r


def make_broker(L, resolver, clock, **kw):
    return Broker(QueueStore(L / "queue"), resolver, SignersSource(L / "policy", L / "routes" / "trust.pub"),
                  clock=clock["fn"], **kw)


@pytest.fixture
def broker(L, resolver, clock):
    return make_broker(L, resolver, clock)


def hook_fields(event="e1", tier="T2", hold=timedelta(minutes=5), at=NOW, **over):
    fields = {"agent": "claude_code", "agent_session_id": "cc-1", "event_id": f"hook:{event}", "tool_name": "Bash",
              "tool_input_digest": tool_input_digest({"command": "ls"}), "display": 'Bash {"command": "ls"}',
              "cwd": "/w", "tier": tier, "received_at": utc_stamp(at), "expires_at": utc_stamp(at + hold)}
    fields.update(over)
    return fields


_ids = iter(range(1, 10**6))


def req(broker, slot=1, cap="cap-a", fields=None, **extra):
    msg = {"v": 1, "op": "REQUEST", "id": next(_ids), "cap": cap, "request": fields or hook_fields(), **extra}
    return broker.handle(slot, msg)


def ask(broker, request_id, slot=1, cap="cap-a"):
    return broker.handle(slot, {"v": 1, "op": "ANSWER?", "id": next(_ids), "cap": cap, "request_id": request_id})


def row_of(L, request_id):
    return QueueStore(L / "queue").read_request(request_id)


def write_answer(L, request_id, key, decision="allow", signed_at=None, sign=True, **override):
    row = row_of(L, request_id)
    payload = answer_payload(queue_id=row["queue_id"], request=request_of(row["request"]), decision=decision,
                             signer_fp=key.fp, signed_at=utc_stamp(signed_at or NOW + timedelta(seconds=5)),
                             receipts_head=HEAD)
    payload.update(override)
    signature = key.sign(answer_message(payload), ANSWER_NAMESPACE) if sign else ""
    (L / "queue" / "answers" / f"{request_id}.json").write_bytes(
        canonical_bytes({"format": ANSWER_ROW_FORMAT, "payload": payload, "signature": signature}))
    return payload, signature


def filed(broker, **kw):
    got = req(broker, **kw)
    assert got["ok"] is True, got
    return got["request_id"]


# --- REQUEST ---------------------------------------------------------------------------------------------------

def test_a_filed_request_takes_session_slot_kind_from_the_resolve_only(L, broker, resolver):
    got = req(broker, session="forged", slot_claim=9, kind="dispatch", run={"run_id": "forged"})
    assert got["ok"] is True
    row = row_of(L, got["request_id"])
    assert (row["session"], row["slot"], row["kind"]) == ("sess-a", {"class": "egress", "index": 1}, "interactive") and "run" not in row
    assert row["queue_id"] == got["queue_id"] == QueueStore(L / "queue").queue_id
    assert oct((L / "queue" / "requests" / f"{got['request_id']}.json").stat().st_mode & 0o777) == "0o640"
    # the raw cap is nowhere on disk
    assert not any(b"cap-a" in p.read_bytes() for p in (L / "queue" / "requests").iterdir())
    import hashlib
    assert resolver.calls[-1] == ({"class": "egress", "index": 1}, "sha256:" + hashlib.sha256(b"cap-a").hexdigest())


def test_a_dispatch_row_carries_the_resolved_run_verbatim(L, broker):
    rid = filed(broker, slot=2, cap="cap-two")
    row = row_of(L, rid)
    assert (row["session"], row["slot"], row["kind"], row["run"]) == ("sess-2", {"class": "egress", "index": 2}, "dispatch", RUN)


def test_the_row_id_is_the_sealed_id_never_the_hooks_claim(L, broker):
    got = req(broker, fields=hook_fields(id="pend-2026-01-01T00:00:00Z-0123456789abcdef"))
    assert got["ok"] is True
    assert got["request_id"] != "pend-2026-01-01T00:00:00Z-0123456789abcdef"
    assert request_of(row_of(L, got["request_id"])["request"]).id == got["request_id"]


def test_a_cap_for_slot_2_on_slot_1s_socket_is_refused(L, broker):
    got = req(broker, slot=1, cap="cap-two")
    assert got["ok"] is False and got["refused"] == "cap-unknown"
    assert list((L / "queue" / "requests").glob("pend-*")) == []
    assert req(broker, slot=2, cap="cap-two")["ok"] is True


def test_the_same_sealed_request_returns_the_existing_row(L, broker):
    first, second = req(broker), req(broker)
    assert first["ok"] and second["ok"] and first["request_id"] == second["request_id"]
    assert len(list((L / "queue" / "requests").glob("pend-*.json"))) == 1


def test_another_session_filing_the_same_sealed_request_is_refused(L, broker):
    filed(broker)
    got = req(broker, cap="cap-a2")
    assert got["ok"] is False and got["refused"] == "request-exists"


def test_quota_per_session_and_per_run_and_replay_does_not_count(L, resolver, clock):
    b = make_broker(L, resolver, clock, quota=2)
    filed(b, fields=hook_fields("q1"))
    filed(b, fields=hook_fields("q1"))          # a replay: no new row
    filed(b, fields=hook_fields("q2"))
    got = req(b, fields=hook_fields("q3"))
    assert got["ok"] is False and got["refused"] == "quota-exhausted"
    assert req(b, cap="cap-a2", fields=hook_fields("q3"))["ok"] is True       # another session: its own count
    # a restarted broker counts the rows already on disk
    b2 = make_broker(L, resolver, clock, quota=2)
    assert req(b2, fields=hook_fields("q4"))["refused"] == "quota-exhausted"
    # a dispatch run counts by run_id, across the sessions of that run
    resolver.add(3, "cap-three", Resolved(session="sess-3", kind="dispatch", run=RUN))
    filed(b2, slot=2, cap="cap-two", fields=hook_fields("r1"))
    filed(b2, slot=3, cap="cap-three", fields=hook_fields("r2"))
    assert req(b2, slot=2, cap="cap-two", fields=hook_fields("r3"))["refused"] == "quota-exhausted"


@pytest.mark.parametrize("fields, name", [
    (hook_fields(tier="T9"), "request-malformed"),
    ({**hook_fields(), "extra": 1}, "request-malformed"),
    ({k: v for k, v in hook_fields().items() if k != "cwd"}, "request-malformed"),
    (hook_fields(at=NOW - timedelta(minutes=10)), "request-clock"),
    (hook_fields(hold=timedelta(hours=2)), "request-hold"),
])
def test_a_request_outside_its_bounds_is_refused(broker, fields, name):
    got = req(broker, fields=fields)
    assert got["ok"] is False and got["refused"] == name


@pytest.mark.parametrize("op", ["ANSWER", "GRANT-ANSWER", "ROW", "QUEUE-RESOLVE", "request"])
def test_no_verb_takes_an_answer_or_a_row_from_a_hook(broker, op):
    got = broker.handle(1, {"v": 1, "op": op, "id": 7, "cap": "cap-a", "payload": {}, "signature": "x"})
    assert got == {"id": 7, "ok": False, "refused": "verb-not-allowed"}


# --- ANSWER? ---------------------------------------------------------------------------------------------------

def test_pending_then_a_verified_allow_then_deny(L, broker, keys):
    rid = filed(broker)
    assert ask(broker, rid)["pending"] is True
    write_answer(L, rid, keys["operator"])
    assert ask(broker, rid)["decision"] == "allow"
    write_answer(L, rid, keys["operator"], decision="deny")
    assert ask(broker, rid)["decision"] == "deny"


def test_an_answer_signed_by_a_key_not_in_the_snapshot_is_refused(L, broker, keys):
    rid = filed(broker)
    write_answer(L, rid, keys["stranger"])
    assert ask(broker, rid)["refused"] == "answer-refused"


def test_an_answer_signed_by_one_key_naming_another_is_refused(L, broker, keys):
    rid = filed(broker)
    row = row_of(L, rid)
    payload = answer_payload(queue_id=row["queue_id"], request=request_of(row["request"]), decision="allow",
                             signer_fp=keys["operator"].fp, signed_at=utc_stamp(NOW + timedelta(seconds=5)),
                             receipts_head=HEAD)
    sig = keys["stranger"].sign(answer_message(payload), ANSWER_NAMESPACE)
    (L / "queue" / "answers" / f"{rid}.json").write_bytes(
        canonical_bytes({"format": ANSWER_ROW_FORMAT, "payload": payload, "signature": sig}))
    assert ask(broker, rid)["refused"] == "answer-refused"


def test_an_unsigned_or_planted_answer_row_is_refused(L, broker, keys):
    rid = filed(broker)
    write_answer(L, rid, keys["operator"], sign=False)
    assert ask(broker, rid)["refused"] == "answer-refused"
    (L / "queue" / "answers" / f"{rid}.json").write_bytes(b'{"decision":"allow"}')
    assert ask(broker, rid)["refused"] == "answer-refused"
    (L / "queue" / "answers" / f"{rid}.json").unlink()
    (L / "queue" / "answers" / f"{rid}.json").symlink_to(L / "routes" / "trust.pub")
    assert ask(broker, rid)["refused"] == "answer-refused"


@pytest.mark.parametrize("override", [
    {"queue_id": "qb-" + "1" * 32}, {"request_digest": "0" * 64}, {"expires_at": utc_stamp(NOW + timedelta(hours=1))},
    {"format": "levain-consent-answer/1"},
])
def test_an_answer_that_does_not_match_its_own_row_is_refused(L, broker, keys, override):
    rid = filed(broker)
    write_answer(L, rid, keys["operator"], **override)
    assert ask(broker, rid)["refused"] == "answer-refused"


def test_an_expired_answer_is_refused(L, broker, keys, clock):
    rid = filed(broker)
    write_answer(L, rid, keys["operator"], signed_at=NOW + timedelta(minutes=6))   # after the 5-minute hold
    assert ask(broker, rid)["refused"] == "answer-refused"
    write_answer(L, rid, keys["operator"])                                         # in time, read too late
    clock["now"] = NOW + timedelta(minutes=5)
    assert ask(broker, rid)["refused"] == "request-expired"


def test_an_answer_for_another_sessions_request_is_refused(L, broker, keys):
    rid = filed(broker)
    write_answer(L, rid, keys["operator"])
    assert ask(broker, rid, cap="cap-a2")["refused"] == "no-such-request"
    assert ask(broker, rid, slot=2, cap="cap-two")["refused"] == "no-such-request"
    assert ask(broker, "pend-2026-01-01T00:00:00Z-0123456789abcdef")["refused"] == "no-such-request"
    assert ask(broker, "../../policy/allowed_signers")["refused"] == "no-such-request"
    assert ask(broker, rid)["decision"] == "allow"


def test_a_missing_or_unverifiable_snapshot_refuses_every_answer(L, broker, keys):
    rid = filed(broker)
    write_answer(L, rid, keys["operator"])
    assert ask(broker, rid)["decision"] == "allow"
    (L / "policy" / "allowed_signers.json.sig").unlink()
    assert ask(broker, rid)["refused"] == "answer-refused"
    write_signers(L, keys, 2, sign_with="operator")                  # signed, but not by the trust root
    assert ask(broker, rid)["refused"] == "answer-refused"
    write_signers(L, keys, 3)
    assert ask(broker, rid)["decision"] == "allow"
    (L / "policy" / "allowed_signers.json").unlink()
    assert ask(broker, rid)["refused"] == "answer-refused"


def test_an_older_generation_snapshot_is_refused(L, broker, keys):
    rid = filed(broker)
    write_answer(L, rid, keys["operator"])
    write_signers(L, keys, 5)
    assert ask(broker, rid)["decision"] == "allow"
    write_signers(L, keys, 4)                                        # validly signed, a rollback
    assert ask(broker, rid)["refused"] == "answer-refused"
    write_signers(L, keys, 5, [keys["operator"].signer(), keys["stranger"].signer()])   # same generation, new body
    assert ask(broker, rid)["refused"] == "answer-refused"
    write_signers(L, keys, 6)
    assert ask(broker, rid)["decision"] == "allow"


def test_a_signer_without_the_answer_namespace_is_refused(L, broker, keys):
    rid = filed(broker)
    write_signers(L, keys, 2, [keys["operator"].signer(namespaces=["levain-dispatch-contract"])])
    write_answer(L, rid, keys["operator"])
    assert ask(broker, rid)["refused"] == "answer-refused"


def test_a_t3_allow_needs_a_t3_class_signer_and_a_deny_does_not(L, broker, keys):
    """CONTRACT c7 §3.7: each signer's key_class is the consent queue's; class_covers decides an allow."""
    rid = filed(broker, fields=hook_fields("t3", tier="T3"))
    write_answer(L, rid, keys["operator"])                                       # display-phone
    assert ask(broker, rid)["refused"] == "answer-refused"
    write_answer(L, rid, keys["operator"], decision="deny")
    assert ask(broker, rid)["decision"] == "deny"
    write_signers(L, keys, 2, [keys["operator"].signer(key_class="laptop-presence", custody="passphrase")])
    write_answer(L, rid, keys["operator"])
    assert ask(broker, rid)["decision"] == "allow"


@pytest.mark.parametrize("change", [
    {"key_class": None}, {"custody": None},                              # missing (None = drop the field)
    {"key_class": "root"}, {"key_class": "T3"},                          # not a consent key class
    {"custody": "vault"},                                                # not a consent custody
    {"key_class": "laptop-presence", "custody": "unstated"},             # a laptop key must state its custody
    {"custody": "passkey"},                                              # passkey needs an sk-ecdsa key
    {"key_class": "laptop-presence", "custody": "fido-attested"},        # refused until attestation is built
])
def test_a_signer_entry_without_a_valid_class_and_custody_is_refused(L, keys, change):
    entry = keys["operator"].signer()
    for k, v in change.items():
        if v is None:
            del entry[k]
        else:
            entry[k] = v
    write_signers(L, keys, 2, [entry])
    src = SignersSource(L / "policy", L / "routes" / "trust.pub")
    assert src.current() is None and src.problem


def test_answer_refusal_names_why(L, broker, keys):
    rid = filed(broker)
    payload, sig = write_answer(L, rid, keys["operator"])
    row = row_of(L, rid)
    snap = SignersSource(L / "policy", L / "routes" / "trust.pub").current()
    later = NOW + timedelta(seconds=10)
    assert answer_refusal(row, payload, sig, snap, later) is None
    assert "missing or unverifiable" in answer_refusal(row, payload, sig, None, later)
    assert "hold ended" in answer_refusal(row, payload, sig, snap, NOW + timedelta(minutes=5))
    assert "does not verify" in answer_refusal(row, {**payload, "decision": "deny"}, sig, snap, later)
    tampered = {**row, "request": {**row["request"], "tool_name": "Write"}}
    assert "seal" in answer_refusal(tampered, payload, sig, snap, later)


# --- wire + sockets --------------------------------------------------------------------------------------------

@pytest.fixture
def run_dir():
    d = Path(tempfile.mkdtemp(prefix="qb", dir="/tmp"))
    (d / "s1").mkdir()
    yield d
    shutil.rmtree(d, ignore_errors=True)


def bound_gid(d: Path) -> int:
    """The group a node made in ``d`` takes (the directory's on macOS, the process's on Linux)."""
    probe = Path(tempfile.mkstemp(dir=d)[1])
    try:
        return probe.stat().st_gid
    finally:
        probe.unlink()


def listen(b, run_dir: Path, uid: int, **kw) -> SlotListener:
    kw.setdefault("expected_gid", bound_gid(run_dir / "s1"))
    return SlotListener(b, 1, run_dir / "s1" / "q.sock", uid, **kw)


def connect(path: Path) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(str(path))
    return s


def test_a_request_and_its_answer_over_a_real_socket(L, broker, keys, run_dir):
    lst = listen(broker, run_dir, os.getuid())   # the real LOCAL_PEERCRED / SO_PEERCRED
    lst.start()
    try:
        assert oct((run_dir / "s1" / "q.sock").stat().st_mode & 0o777) == "0o660"
        with connect(lst.path) as s:
            wire.write_frame(s, {"v": 1, "op": "REQUEST", "id": 1, "cap": "cap-a", "request": hook_fields()})
            got = wire.read_frame(s)
            assert got["ok"] is True and got["id"] == 1
            write_answer(L, got["request_id"], keys["operator"])
            wire.write_frame(s, {"v": 1, "op": "ANSWER?", "id": 2, "cap": "cap-a", "request_id": got["request_id"]})
            assert wire.read_frame(s) == {"id": 2, "ok": True, "decision": "allow"}
    finally:
        lst.stop()


def test_a_real_peer_with_the_wrong_uid_is_refused_before_the_body_is_read(L, resolver, clock, run_dir):
    handled = []

    class Spy(Broker):
        def handle(self, slot, message):
            handled.append(message)
            return super().handle(slot, message)

    spy = Spy(QueueStore(L / "queue"), resolver, SignersSource(L / "policy", L / "routes" / "trust.pub"),
              clock=clock["fn"])
    lst = listen(spy, run_dir, os.getuid() + 1)
    lst.start()
    try:
        with connect(lst.path) as s:
            try:   # the server may close before the write lands: a broken pipe is the same refusal
                wire.write_frame(s, {"v": 1, "op": "REQUEST", "id": 1, "cap": "cap-a", "request": hook_fields()})
                assert s.recv(1) == b""                   # closed, unanswered
            except (BrokenPipeError, ConnectionResetError):
                pass
    finally:
        lst.stop()
    assert handled == [] and lst.refused_peers == 1
    assert list((L / "queue" / "requests").glob("pend-*")) == []


def test_peer_uid_reads_the_kernels_answer(run_dir):
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(run_dir / "p.sock"))
    srv.listen(1)
    try:
        with connect(run_dir / "p.sock"):
            conn, _ = srv.accept()
            with conn:
                assert peer_uid(conn) == os.getuid()
    finally:
        srv.close()


def test_an_oversize_frame_is_refused_unread(broker, run_dir):
    lst = listen(broker, run_dir, os.getuid(), peer_uid=lambda _s: os.getuid())
    lst.start()
    try:
        with connect(lst.path) as s:
            s.sendall(struct.pack(">I", wire.MAX_FRAME + 1))
            assert wire.read_frame(s) == {"id": None, "ok": False, "refused": "frame-too-large"}
            assert s.recv(1) == b""
    finally:
        lst.stop()
    with pytest.raises(wire.FrameError):
        wire.encode({"pad": "x" * wire.MAX_FRAME})


def test_a_non_canonical_frame_is_refused(broker, run_dir):
    lst = listen(broker, run_dir, os.getuid(), peer_uid=lambda _s: os.getuid())
    lst.start()
    try:
        with connect(lst.path) as s:
            body = b'{"v": 1, "op": "ANSWER?", "id": 1}'
            s.sendall(struct.pack(">I", len(body)) + body)
            assert wire.read_frame(s) == {"id": None, "ok": False, "refused": "frame-malformed"}
    finally:
        lst.stop()


def test_connections_past_the_bound_are_closed_at_once(broker, run_dir):
    lst = listen(broker, run_dir, os.getuid(), max_connections=1)
    lst.start()
    try:
        held = connect(lst.path)
        wire.write_frame(held, {"v": 1, "op": "NOPE", "id": 1})
        assert wire.read_frame(held)["refused"] == "verb-not-allowed"     # the one slot is taken and live
        with connect(lst.path) as extra:
            assert extra.recv(1) == b""
        held.close()
    finally:
        lst.stop()


def test_the_control_resolver_speaks_queue_resolve(run_dir):
    path = run_dir / "c.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(4)
    seen = []

    def control():
        for answer in ({"ok": True, "session": "s9", "kind": "dispatch", "run": RUN},
                       {"ok": False, "refused": "revoked"}, {"ok": True, "session": "s9", "kind": "planner"}):
            conn, _ = srv.accept()
            with conn:
                msg = wire.read_frame(conn)
                seen.append(msg)
                wire.write_frame(conn, {"id": msg["id"], **answer})

    t = threading.Thread(target=control, daemon=True)
    t.start()
    try:
        r = ControlResolver(path)
        assert r.queue_resolve({"class": "egress", "index": 4}, D64) == Resolved(session="s9", kind="dispatch", run=RUN)
        assert r.queue_resolve({"class": "egress", "index": 4}, D64) == "revoked"
        assert r.queue_resolve({"class": "egress", "index": 4}, D64) == "resolve-malformed"
    finally:
        t.join(timeout=10)
        srv.close()
    assert seen[0] == {"v": 1, "op": "QUEUE-RESOLVE", "id": 1, "slot": {"class": "egress", "index": 4}, "cap_digest": D64}


def test_an_unreachable_control_fails_closed(L, clock, run_dir):
    b = make_broker(L, ControlResolver(run_dir / "absent.sock"), clock)
    got = req(b)
    assert got["ok"] is False and got["refused"] == "control-unavailable"


# --- fix round 1 -----------------------------------------------------------------------------------------------

def test_a_poll_storm_costs_one_verify_and_one_resolve(L, broker, resolver, keys, monkeypatch):
    import levain.qbroker.broker as broker_mod
    verifies = []
    real = broker_mod.answer_refusal

    def counted(*a, **kw):
        verifies.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(broker_mod, "answer_refusal", counted)
    rid = filed(broker)
    write_answer(L, rid, keys["operator"])
    for _ in range(30):
        assert ask(broker, rid)["decision"] == "allow"
    assert len(verifies) == 1 and len(resolver.calls) == 1
    write_answer(L, rid, keys["operator"], decision="deny")          # a new answer file: verified again
    assert ask(broker, rid)["decision"] == "deny" and len(verifies) == 2
    write_signers(L, keys, 2, [keys["stranger"].signer()])           # a new snapshot: verified again, refused
    assert ask(broker, rid)["refused"] == "answer-refused" and len(verifies) == 3


def test_a_cached_resolve_expires_and_refusals_are_cached_too(L, resolver, clock):
    mono = {"t": 100.0}
    b = make_broker(L, resolver, clock, monotonic=lambda: mono["t"])
    rid = filed(b)
    assert ask(b, rid, cap="cap-x")["refused"] == "cap-unknown"
    assert ask(b, rid, cap="cap-x")["refused"] == "cap-unknown"
    ask(b, rid)
    assert len(resolver.calls) == 2                                   # cap-a once, cap-x once
    mono["t"] += 60
    ask(b, rid)
    assert len(resolver.calls) == 3


def test_answer_polls_past_the_slots_rate_are_refused_rate_limited(L, resolver, clock):
    mono = {"t": 100.0}
    b = make_broker(L, resolver, clock, monotonic=lambda: mono["t"], answer_rate=10.0, answer_burst=5)
    rid = filed(b)
    assert [ask(b, rid).get("pending") for _ in range(5)] == [True] * 5
    assert ask(b, rid)["refused"] == "rate-limited"
    assert req(b, fields=hook_fields("other"))["ok"] is True                  # REQUEST is not ANSWER?
    assert ask(b, rid, slot=2, cap="cap-two")["refused"] == "no-such-request"  # slot 2 has its own bucket
    mono["t"] += 0.2                                                           # two tokens back
    assert ask(b, rid)["pending"] is True and ask(b, rid)["pending"] is True
    assert ask(b, rid)["refused"] == "rate-limited"


def test_a_fifth_unexpired_pending_row_is_refused_queue_busy(L, broker, keys, clock):
    rids = [filed(broker, fields=hook_fields(f"p{i}")) for i in range(4)]
    got = req(broker, fields=hook_fields("p4"))
    assert got["ok"] is False and got["refused"] == "queue-busy"
    assert len(list((L / "queue" / "requests").glob("pend-*.json"))) == 4
    assert req(broker, cap="cap-a2", fields=hook_fields("b0"))["ok"] is True   # another session: its own four
    assert req(broker, fields=hook_fields("p0"))["ok"] is True        # a replay is not a new row
    write_answer(L, rids[0], keys["operator"])                                 # answered: no longer pending
    assert req(broker, fields=hook_fields("p4"))["ok"] is True
    clock["now"] = NOW + timedelta(minutes=5)                                  # every hold has ended
    for i in range(4):
        assert req(broker, fields=hook_fields(f"n{i}", at=clock["now"]))["ok"] is True


def test_a_restarted_broker_counts_pending_rows_on_disk(L, resolver, clock):
    b = make_broker(L, resolver, clock)
    for i in range(4):
        filed(b, fields=hook_fields(f"p{i}"))
    assert req(make_broker(L, resolver, clock), fields=hook_fields("p4"))["refused"] == "queue-busy"


def test_a_failed_write_leaves_the_quota_and_pending_count_unchanged(L, resolver, clock, monkeypatch):
    import errno as _errno
    b = make_broker(L, resolver, clock, quota=1)
    real = QueueStore.write_request
    fails = [1]

    def full(self, row):
        if fails:
            fails.pop()
            raise OSError(_errno.ENOSPC, "No space left on device")
        return real(self, row)

    monkeypatch.setattr(QueueStore, "write_request", full)
    assert req(b)["refused"] == "broker-error"
    assert req(b)["ok"] is True


def test_a_replay_after_the_clock_window_returns_the_existing_row(L, broker, clock):
    rid = filed(broker)
    clock["now"] = NOW + timedelta(minutes=3)                         # past RECEIVED_SKEW, inside the hold
    got = req(broker)
    assert got["ok"] is True and got["request_id"] == rid
    clock["now"] = NOW + timedelta(minutes=6)                         # a NEW row would still be refused
    assert req(broker, fields=hook_fields("late"))["refused"] == "request-clock"


def test_a_stored_row_without_a_run_id_does_not_stop_startup(L, resolver, clock):
    rid = filed(make_broker(L, resolver, clock))
    path = L / "queue" / "requests" / f"{rid}.json"
    row = row_of(L, rid)
    row["run"] = {"job_id": "j"}
    path.unlink()
    path.write_bytes(canonical_bytes(row))
    b = make_broker(L, resolver, clock)
    assert req(b, fields=hook_fields("after"))["ok"] is True


def test_a_verifier_unavailable_reload_retries_on_the_next_poll(L, monkeypatch):
    import levain.qbroker.signers as signers_mod
    from levain.authority import VerifierUnavailable
    real, fails = signers_mod.verify_link_sig, [1]

    def flaky(*a, **kw):
        if fails:
            fails.pop()
            raise VerifierUnavailable("ssh-keygen did not run")
        return real(*a, **kw)

    monkeypatch.setattr(signers_mod, "verify_link_sig", flaky)
    src = SignersSource(L / "policy", L / "routes" / "trust.pub")
    assert src.current() is None
    assert src.current() is not None and src.problem is None          # same files, verified this time


def test_control_ok_zero_or_a_true_id_is_refused(run_dir):
    path = run_dir / "c.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(4)

    def control():
        for shape in ("ok0", "idtrue"):
            conn, _ = srv.accept()
            with conn:
                msg = wire.read_frame(conn)
                if shape == "ok0":
                    wire.write_frame(conn, {"id": msg["id"], "ok": 0, "session": "s9", "kind": "interactive"})
                else:
                    wire.write_frame(conn, {"id": True, "ok": True, "session": "s9", "kind": "interactive"})

    t = threading.Thread(target=control, daemon=True)
    t.start()
    try:
        r = ControlResolver(path)
        assert r.queue_resolve({"class": "egress", "index": 4}, D64) == "resolve-malformed"   # id 1
        r2 = ControlResolver(path)
        assert r2.queue_resolve({"class": "egress", "index": 4}, D64) == "resolve-malformed"  # id 1 vs True
    finally:
        t.join(timeout=10)
        srv.close()


def test_an_accept_error_does_not_stop_the_listener(broker, run_dir, monkeypatch):
    import errno as _errno
    real, fails = socket.socket.accept, [1]

    def flaky(self):
        if fails:
            fails.pop()
            raise OSError(_errno.EMFILE, "Too many open files")
        return real(self)

    monkeypatch.setattr(socket.socket, "accept", flaky)
    lst = listen(broker, run_dir, os.getuid(), peer_uid=lambda _s: os.getuid())
    lst.start()
    try:
        with connect(lst.path) as s:
            wire.write_frame(s, {"v": 1, "op": "NOPE", "id": 1})
            assert wire.read_frame(s)["refused"] == "verb-not-allowed"
        assert not fails
    finally:
        lst.stop()
    assert not lst.path.exists()                                       # stop() unlinks q.sock


def test_a_socket_in_the_wrong_group_refuses_to_serve(broker, run_dir):
    gid = bound_gid(run_dir / "s1")
    lst = listen(broker, run_dir, os.getuid(), expected_gid=gid + 1)
    with pytest.raises(PermissionError):
        lst.start()
    assert not (run_dir / "s1" / "q.sock").exists()


def test_a_drip_fed_frame_hits_its_deadline(broker, run_dir):
    import time
    lst = listen(broker, run_dir, os.getuid(), peer_uid=lambda _s: os.getuid(), idle_timeout=0.3)
    lst.start()
    try:
        frame = wire.encode({"v": 1, "op": "NOPE", "id": 1})
        with connect(lst.path) as s:
            closed = False
            try:
                for byte in frame[:12]:            # one byte every 0.1s: each recv inside 0.3s, the frame not
                    s.sendall(bytes([byte]))
                    time.sleep(0.1)
            except (BrokenPipeError, ConnectionResetError):
                closed = True
            if not closed:
                s.setblocking(False)
                try:
                    got = s.recv(4096)             # the server already answered or closed: never still waiting
                except BlockingIOError:
                    got = None
                except ConnectionResetError:
                    got = b""
                assert got is not None and b"verb-not-allowed" not in got
    finally:
        lst.stop()


def test_a_snapshot_signed_in_the_dispatch_config_namespace_is_refused(L, broker, keys):
    """CONTRACT c6 §3.7: the snapshot's SSHSIG is in its own namespace, never a config item's."""
    rid = filed(broker)
    write_answer(L, rid, keys["operator"])
    write_signers(L, keys, 2, namespace="levain-dispatch-config")
    assert ask(broker, rid)["refused"] == "answer-refused"
    write_signers(L, keys, 3)
    assert ask(broker, rid)["decision"] == "allow"


def test_an_allowed_signer_reads_as_an_authority_key(L, keys):
    """The attributes levain.consent.queue's key check reads (key.active, key_class, custody, public_key,
    fingerprint), so the seam can hand a snapshot to it as its ``authority``."""
    write_signers(L, keys, 2, [keys["operator"].signer(key_class="laptop-presence", custody="passphrase")])
    snap = SignersSource(L / "policy", L / "routes" / "trust.pub").current()
    key = snap.key(keys["operator"].fp)
    assert (key.active, key.key_class, key.custody) == (True, "laptop-presence", "passphrase")   # as signed
    assert key.fingerprint == keys["operator"].fp and key.public_key.startswith("ssh-ed25519 ")


@pytest.mark.parametrize("digest", ["c" * 64, "sha256:" + "C" * 64, "sha256:" + "c" * 63, "sha1:" + "c" * 40, 7])
def test_a_resolved_run_whose_contract_digest_is_not_the_sha256_form_is_refused(L, resolver, clock, digest):
    """CONTRACT c6 line 22: every ``*_digest`` is sha256: + 64 lowercase hex; the row carries run verbatim."""
    resolver.add(3, "cap-bad", Resolved(session="sess-3", kind="dispatch", run={**RUN, "contract_digest": digest}))
    got = req(make_broker(L, resolver, clock), slot=3, cap="cap-bad")
    assert got["ok"] is False and got["refused"] == "resolve-malformed"
    assert list((L / "queue" / "requests").glob("pend-*")) == []
