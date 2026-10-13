"""The consent-queue core (Q1): a fake external agent files a gate; a signed answer, a silence timeout and
every replay refusal resolve it, or do not; each terminal transition has its receipt.

Keys are real ssh-keygen keys; the authority is a verified replay of a real signed log
(``levain.authority.sign_and_append`` + ``snapshot``), so an answer is checked exactly as production checks it.
"""
from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import levain.authority as auth
from levain.authority import ReplayCache
from levain.cockpit.consent import utc_stamp
from levain.consent import (
    ANSWER_NAMESPACE, AnswerRefused, ConsentQueue, QueueStoreError, answer_message, answer_payload, decide_by_policy,
)
from levain.consent.record import ExternalGateRequest, redact_display, tool_input_digest

NOW = datetime(2026, 10, 13, 20, 0, tzinfo=timezone.utc)


class Key:
    def __init__(self, d: Path, name: str) -> None:
        self.path = d / name
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(self.path)],
                       check=True, capture_output=True, timeout=30)
        self.public_key = " ".join((d / f"{name}.pub").read_text().split()[:2])
        out = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(self.path)], check=True,
                             capture_output=True, text=True).stdout
        self.fp = out.split()[1]

    def sign(self, message: bytes, namespace: str = ANSWER_NAMESPACE) -> str:
        return subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(self.path), "-n", namespace],
                              input=message, capture_output=True, check=True, timeout=30).stdout.decode()


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    d = tmp_path_factory.mktemp("consent-keys")
    return {n: Key(d, n) for n in ("laptop", "phone", "presence", "revoked", "stranger")}


@pytest.fixture(scope="module")
def authority(keys, tmp_path_factory):
    """A verified replay: laptop (genesis), phone (display-phone), presence, and a revoked phone-class key."""
    conn = sqlite3.connect(tmp_path_factory.mktemp("consent-auth") / "cockpit.db", isolation_level=None)
    auth.create_schema(conn)

    def link(op, signer):
        auth.sign_and_append(conn, op, signer_fp=signer.fp, sign=lambda m: signer.sign(m, auth.NAMESPACE),
                             clock=lambda: datetime.now(timezone.utc), cache=ReplayCache(), anchor=None)

    for name, cls in (("laptop", "laptop-presence"), ("phone", "display-phone"), ("presence", "presence"),
                      ("revoked", "display-phone")):
        k = keys[name]
        link({"op": "enrol", "fingerprint": k.fp, "public_key": k.public_key, "class": cls,
              "custody": "passphrase" if cls == "laptop-presence" else "unstated"}, keys["laptop"])
    link({"op": "revoke_key", "fingerprint": keys["revoked"].fp}, keys["laptop"])
    snap = auth.snapshot(conn, cache=ReplayCache())
    conn.close()
    return snap


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock():
    return Clock(NOW)


@pytest.fixture
def queue(tmp_path, clock):
    return ConsentQueue(tmp_path / "levain-home" / "consent", clock=clock)


class FakeAgent:
    """What a PermissionRequest hook does: file the gate it is shown, then wait for a decision or nothing."""

    def __init__(self, queue: ConsentQueue, agent: str = "claude_code") -> None:
        self.queue = queue
        self.agent = agent

    def ask(self, command: str = "touch /tmp/x", *, hold: timedelta = timedelta(minutes=5), tier: str = "T2"):
        return self.queue.file(agent=self.agent, agent_session_id="sess-1", tool_name="Bash",
                               tool_input={"command": command}, cwd="/work/proj", hold=hold, tier=tier)


def signed(queue, request, key, decision="allow", **override):
    payload = answer_payload(queue_id=queue.queue_id, request=request, decision=decision, signer_fp=key.fp)
    payload.update(override)
    return payload, key.sign(answer_message(payload))


# --- the fake external agent, end to end -------------------------------------------------------------------

def test_signed_allow_resolves_a_waiting_agent(tmp_path, keys, authority):
    q = ConsentQueue(tmp_path / "consent")                       # the real clock: the agent really waits
    request = FakeAgent(q).ask()
    got = {}
    waiter = threading.Thread(target=lambda: got.setdefault("d", q.await_decision(request.id, 20)))
    waiter.start()
    payload, sig = signed(q, request, keys["phone"])
    decision = q.answer(request.id, payload, sig, authority=authority)
    waiter.join(25)
    assert decision.decision == "allow" and decision.signer_fp == keys["phone"].fp
    assert got["d"] == decision
    assert q.get(request.id)[1] == "allowed"
    face = q.receipt(request.id)["face"]
    assert face["gate"] == {"verdict": "approved", "by": "human"}
    assert face["terminal_state"] == "fired"
    assert face["authority_scope"]["grant"] == f"signed-answer:{keys['phone'].fp}"
    assert face["context_id"] == request.id and face["exposed"][0]["producer"] == "claude_code"


def test_silence_expires_and_is_no_decision(tmp_path):
    q = ConsentQueue(tmp_path / "consent")
    request = FakeAgent(q, "codex").ask(hold=timedelta(milliseconds=300))
    t0 = time.monotonic()
    assert q.await_decision(request.id, 10, poll=0.05) is None
    assert time.monotonic() - t0 < 5                             # it ended at expiry, not at the wait's end
    assert q.get(request.id)[1] == "expired"
    face = q.receipt(request.id)["face"]
    assert face["gate"] is None and face["terminal_state"] == "timed_out"


def test_a_wait_that_ends_first_leaves_the_request_pending(queue):
    request = FakeAgent(queue).ask()
    assert queue.await_decision(request.id, 0.05, poll=0.01) is None
    assert queue.get(request.id)[1] == "pending"
    assert queue.receipt(request.id) is None


def test_signed_deny(queue, keys, authority):
    request = FakeAgent(queue).ask()
    payload, sig = signed(queue, request, keys["presence"], "deny")   # any active key may deny
    assert queue.answer(request.id, payload, sig, authority=authority).decision == "deny"
    assert queue.await_decision(request.id, 1).decision == "deny"
    face = queue.receipt(request.id)["face"]
    assert face["gate"] == {"verdict": "denied", "by": "human"} and face["terminal_state"] == "refused"


def test_withdraw_is_no_decision_and_terminal(queue, keys, authority):
    request = FakeAgent(queue).ask()
    assert queue.withdraw(request.id, note="answered in the native dialog")
    assert queue.await_decision(request.id, 1) is None
    face = queue.receipt(request.id)["face"]
    assert face["gate"] is None and face["terminal_state"] is None
    payload, sig = signed(queue, request, keys["phone"])
    with pytest.raises(AnswerRefused, match="already withdrawn"):
        queue.answer(request.id, payload, sig, authority=authority)


# --- replay refusals (H5) ----------------------------------------------------------------------------------

def test_refuses_an_answer_for_another_request(queue, keys, authority):
    agent = FakeAgent(queue)
    a, b = agent.ask("touch a"), agent.ask("touch b")
    payload, sig = signed(queue, b, keys["phone"])
    with pytest.raises(AnswerRefused, match="another request"):
        queue.answer(a.id, payload, sig, authority=authority)
    assert queue.get(a.id)[1] == "pending"


def test_refuses_an_answer_over_a_changed_request_digest(queue, keys, authority):
    request = FakeAgent(queue).ask()
    payload, sig = signed(queue, request, keys["phone"], request_digest="0" * 64)
    with pytest.raises(AnswerRefused, match="another version"):
        queue.answer(request.id, payload, sig, authority=authority)
    assert queue.get(request.id)[1] == "pending"


def test_refuses_an_answer_naming_another_expiry(queue, keys, authority):
    request = FakeAgent(queue).ask()
    payload, sig = signed(queue, request, keys["phone"], expires_at=utc_stamp(NOW + timedelta(days=1)))
    with pytest.raises(AnswerRefused, match="another expiry"):
        queue.answer(request.id, payload, sig, authority=authority)


def test_refuses_an_answer_after_expiry(queue, clock, keys, authority):
    request = FakeAgent(queue).ask(hold=timedelta(seconds=30))
    payload, sig = signed(queue, request, keys["phone"])        # signed in time, delivered late
    clock.now = NOW + timedelta(seconds=30)
    with pytest.raises(AnswerRefused, match="already expired"):
        queue.answer(request.id, payload, sig, authority=authority)
    assert queue.get(request.id)[1] == "expired"
    assert queue.decision(request.id) is None


def test_refuses_a_second_answer_to_a_terminal_request(queue, keys, authority):
    request = FakeAgent(queue).ask()
    payload, sig = signed(queue, request, keys["phone"])
    queue.answer(request.id, payload, sig, authority=authority)
    deny, dsig = signed(queue, request, keys["laptop"], "deny")
    with pytest.raises(AnswerRefused, match="already allowed"):
        queue.answer(request.id, deny, dsig, authority=authority)
    with pytest.raises(AnswerRefused, match="already allowed"):
        queue.answer(request.id, payload, sig, authority=authority)   # the same answer again
    assert queue.decision(request.id).decision == "allow"


def test_refuses_an_answer_for_another_queue(queue, keys, authority):
    request = FakeAgent(queue).ask()
    payload, sig = signed(queue, request, keys["phone"], queue_id="cq-" + "1" * 32)
    with pytest.raises(AnswerRefused, match="another queue"):
        queue.answer(request.id, payload, sig, authority=authority)


# --- an answer is authority only when it verifies (H4) ----------------------------------------------------

def test_a_yes_signed_by_another_key_than_it_names_is_no_decision(queue, keys, authority):
    request = FakeAgent(queue).ask()
    payload = answer_payload(queue_id=queue.queue_id, request=request, decision="allow", signer_fp=keys["phone"].fp)
    sig = keys["stranger"].sign(answer_message(payload))
    with pytest.raises(AnswerRefused, match="does not verify"):
        queue.answer(request.id, payload, sig, authority=authority)
    assert queue.get(request.id)[1] == "pending"


@pytest.mark.parametrize("namespace", ["levain-cockpit", auth.NAMESPACE, "levain-confirm"])
def test_a_signature_in_another_namespace_is_no_decision(queue, keys, authority, namespace):
    request = FakeAgent(queue).ask()
    payload = answer_payload(queue_id=queue.queue_id, request=request, decision="allow", signer_fp=keys["phone"].fp)
    sig = keys["phone"].sign(answer_message(payload), namespace)
    with pytest.raises(AnswerRefused, match="does not verify"):
        queue.answer(request.id, payload, sig, authority=authority)


def test_an_unsigned_or_garbage_yes_is_no_decision(queue, keys, authority):
    request = FakeAgent(queue).ask()
    payload, _ = signed(queue, request, keys["phone"])
    for sig in ("", "yes", None):
        with pytest.raises(AnswerRefused):
            queue.answer(request.id, payload, sig, authority=authority)
    assert queue.get(request.id)[1] == "pending"


def test_unknown_and_revoked_keys_are_refused(queue, keys, authority):
    request = FakeAgent(queue).ask()
    for name, why in (("stranger", "not an enrolled key"), ("revoked", "revoked")):
        payload, sig = signed(queue, request, keys[name])
        with pytest.raises(AnswerRefused, match=why):
            queue.answer(request.id, payload, sig, authority=authority)


def test_an_allow_needs_a_key_class_covering_the_tier(queue, keys, authority):
    agent = FakeAgent(queue)
    t2, t3 = agent.ask("touch two"), agent.ask("touch three", tier="T3")
    payload, sig = signed(queue, t2, keys["presence"])
    with pytest.raises(AnswerRefused, match="does not cover T2"):
        queue.answer(t2.id, payload, sig, authority=authority)
    payload, sig = signed(queue, t3, keys["phone"])
    with pytest.raises(AnswerRefused, match="does not cover T3"):
        queue.answer(t3.id, payload, sig, authority=authority)
    payload, sig = signed(queue, t3, keys["laptop"])
    assert queue.answer(t3.id, payload, sig, authority=authority).decision == "allow"


def test_a_payload_with_extra_or_missing_fields_is_refused(queue, keys, authority):
    request = FakeAgent(queue).ask()
    payload, sig = signed(queue, request, keys["phone"])
    with pytest.raises(AnswerRefused, match="not an answer record"):
        queue.answer(request.id, {**payload, "also": "x"}, sig, authority=authority)
    short = dict(payload)
    short.pop("expires_at")
    with pytest.raises(AnswerRefused, match="not an answer record"):
        queue.answer(request.id, short, sig, authority=authority)


# --- the store ---------------------------------------------------------------------------------------------

def test_terminal_rows_and_receipts_are_frozen_by_the_database(queue, keys, authority):
    request = FakeAgent(queue).ask()
    payload, sig = signed(queue, request, keys["phone"])
    queue.answer(request.id, payload, sig, authority=authority)
    conn = sqlite3.connect(queue.path)
    try:
        for sql in ("UPDATE requests SET state = 'denied' WHERE id = ?",
                    "UPDATE requests SET state = 'pending' WHERE id = ?",
                    "DELETE FROM requests WHERE id = ?",
                    "UPDATE receipts SET state = 'denied' WHERE request_id = ?",
                    "DELETE FROM receipts WHERE request_id = ?"):
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(sql, (request.id,))
    finally:
        conn.close()
    assert queue.decision(request.id).decision == "allow"


def test_a_pending_request_s_fields_cannot_be_changed(queue):
    request = FakeAgent(queue).ask()
    conn = sqlite3.connect(queue.path)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE requests SET display = 'ls' WHERE id = ?", (request.id,))
    finally:
        conn.close()


def test_a_row_whose_seal_does_not_match_is_never_answered(queue, keys, authority):
    request = FakeAgent(queue).ask("rm -rf build")
    payload, sig = signed(queue, request, keys["phone"])
    conn = sqlite3.connect(queue.path)
    try:
        conn.execute("DROP TRIGGER requests_frozen")       # a writer with the file can do anything; the seal sees it
        conn.execute("UPDATE requests SET display = 'Bash {\"command\": \"ls\"}' WHERE id = ?", (request.id,))
        conn.commit()
    finally:
        conn.close()
    assert queue.get(request.id) is None
    assert queue.pending() == []
    with pytest.raises(AnswerRefused, match="no sealed request"):
        queue.answer(request.id, payload, sig, authority=authority)


def test_store_is_owner_only_and_refuses_a_shared_directory(tmp_path, queue):
    FakeAgent(queue).ask()
    assert stat.S_IMODE(os.stat(queue.directory).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(queue.path).st_mode) == 0o600
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    os.chmod(shared, 0o755)
    with pytest.raises(QueueStoreError, match="0700"):
        FakeAgent(ConsentQueue(shared)).ask()


def test_default_dir_is_levain_home_consent(monkeypatch, tmp_path):
    from levain.consent import default_queue_dir
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    assert default_queue_dir() == (tmp_path / "lh" / "consent").resolve()


def test_sweep_and_pending(queue, clock):
    agent = FakeAgent(queue)
    short, long_ = agent.ask("a", hold=timedelta(seconds=10)), agent.ask("b", hold=timedelta(hours=1))
    assert [r.id for r in queue.pending()] == [short.id, long_.id]
    clock.now = NOW + timedelta(seconds=11)
    assert queue.sweep() == 1
    assert [r.id for r in queue.pending()] == [long_.id]
    assert queue.sweep() == 0


# --- the record --------------------------------------------------------------------------------------------

def test_request_id_is_the_pending_seal_and_detects_change(queue):
    request = FakeAgent(queue).ask()
    assert request.id.startswith("pend-") and request.seal_matches()
    fields = request.fields()
    fields["cwd"] = "/elsewhere"
    assert not ExternalGateRequest(**fields).seal_matches()


def test_unknown_agent_and_tier_are_refused(queue):
    from levain.cockpit.consent import ConsentError
    with pytest.raises(ConsentError):
        FakeAgent(queue, "gemini").ask()
    with pytest.raises(ConsentError):
        FakeAgent(queue).ask(tier="C1")


def test_digest_and_display():
    assert tool_input_digest({"a": 1}).startswith("jcs:")
    assert tool_input_digest({"a": 1.5}).startswith("json:")
    assert tool_input_digest({"a": 1}) != tool_input_digest({"a": 2})
    shown = redact_display("mcp__x__y", {"api_key": "sk-123", "nested": {"Token": "t"}, "q": "hi"})
    assert "sk-123" not in shown and '"t"' not in shown and "hi" in shown
    assert len(redact_display("Bash", {"command": "x" * 5000})) == 2000


def test_policy_seam_decides_nothing(queue):
    assert decide_by_policy(FakeAgent(queue).ask()) is None
