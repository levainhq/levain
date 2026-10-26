"""The consent-queue core (Q1): a fake external agent files a gate; a signed answer, a silence timeout and
every replay refusal resolve it, or do not; each terminal transition has its receipt.

Keys are real ssh-keygen keys; the authority is a verified replay of a real signed log
(``levain.authority.sign_and_append`` + ``snapshot``), so an answer is checked exactly as production checks it.
"""
from __future__ import annotations

import dataclasses
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import levain.authority as auth
from levain.authority import ReplayCache
from levain.cockpit.consent import parse_stamp, utc_stamp
from levain.cockpit.firejournal import UNINITIALISED, CockpitDB
from levain.consent.queue import UNDECIDED_GRACE
from levain.consent import (
    ANSWER_NAMESPACE, AnswerRefused, ConsentQueue, QueueStoreError, answer_message, answer_payload, authority_source,
)
from levain.consent.receipt_log import head as receipts_head
from levain.consent.record import ExternalGateRequest, redact_display, tool_input_digest
from tests._h4 import h4_home

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


class Log:
    """A real signed authority log in a real cockpit journal, ``directory/cockpit.db`` (its full schema, as the
    broker creates it); ``source`` is what a queue replays from."""

    def __init__(self, directory: Path, keys) -> None:
        self.path = directory / "cockpit.db"
        self.keys = keys
        with CockpitDB(directory).read():
            pass
        self.source = authority_source(self.path, anchor=None, cache=ReplayCache())

    def link(self, op, signer="laptop", at=None):
        signer = self.keys[signer]
        conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            auth.sign_and_append(conn, op, signer_fp=signer.fp, sign=lambda m: signer.sign(m, auth.NAMESPACE),
                                 clock=lambda: at or datetime.now(timezone.utc), cache=ReplayCache(), anchor=None)
        finally:
            conn.close()

    def enrol(self, name, cls):
        k = self.keys[name]
        self.link({"op": "enrol", "fingerprint": k.fp, "public_key": k.public_key, "class": cls,
                   "custody": "passphrase" if cls == "laptop-presence" else "unstated"})

    def revoke(self, name):
        self.link({"op": "revoke_key", "fingerprint": self.keys[name].fp})


@pytest.fixture(scope="module")
def authority(keys, tmp_path_factory):
    """A verified-replay source: laptop (genesis), phone (display-phone), presence, and a revoked phone-class key."""
    log = Log(tmp_path_factory.mktemp("consent-auth"), keys)
    for name, cls in (("laptop", "laptop-presence"), ("phone", "display-phone"), ("presence", "presence"),
                      ("revoked", "display-phone")):
        log.enrol(name, cls)
    log.revoke("revoked")
    return log.source


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock():
    return Clock(NOW)


@pytest.fixture
def queue(tmp_path, clock, authority):
    return ConsentQueue(tmp_path / "levain-home" / "consent", authority=authority, clock=clock, home=h4_home())


class FakeAgent:
    """What a PermissionRequest hook does: file the gate it is shown, then wait for a decision or nothing."""

    def __init__(self, queue: ConsentQueue, agent: str = "claude_code") -> None:
        self.queue = queue
        self.agent = agent

    def ask(self, command: str = "touch /tmp/x", *, hold: timedelta = timedelta(minutes=5), tier: str = "T2"):
        return self.queue.file(agent=self.agent, agent_session_id="sess-1", tool_name="Bash",
                               tool_input={"command": command}, cwd="/work/proj", hold=hold, tier=tier)


def signed(queue, request, key, decision="allow", **override):
    if isinstance(queue, ConsentQueue):
        payload = answer_payload(queue_id=queue.queue_id, request=request, decision=decision, signer_fp=key.fp,
                                 signed_at=utc_stamp(queue._clock()), receipts_head=receipts_head(queue))
    else:   # the frozen /3 code (tests/fixtures): its own record, from before the receipts were chained
        payload = sys.modules[type(queue).__module__].answer_payload(
            queue_id=queue.queue_id, request=request, decision=decision, signer_fp=key.fp,
            signed_at=utc_stamp(queue._clock()))
    payload.update(override)
    return payload, key.sign(answer_message(payload))


# --- the fake external agent, end to end -------------------------------------------------------------------

def test_signed_allow_resolves_a_waiting_agent(tmp_path, keys, authority):
    q = ConsentQueue(tmp_path / "consent", authority=authority, home=h4_home())  # the real clock: the agent really waits
    request = FakeAgent(q).ask()
    got = {}
    waiter = threading.Thread(target=lambda: got.setdefault("d", q.await_decision(request.id, 20)))
    waiter.start()
    payload, sig = signed(q, request, keys["phone"])
    decision = q.answer(request.id, payload, sig)
    waiter.join(25)
    assert decision.decision == "allow" and decision.signer_fp == keys["phone"].fp
    assert got["d"] == decision
    assert q.get(request.id)[1] == "allowed"
    face = q.receipt(request.id)["face"]
    assert face["gate"] == {"verdict": "approved", "by": "human"}
    assert face["terminal_state"] == "fired"
    assert face["authority_scope"]["grant"] == f"signed-answer:{keys['phone'].fp}"
    assert face["context_id"] == request.id and face["exposed"][0]["producer"] == "claude_code"


def test_silence_expires_and_is_no_decision(tmp_path, authority):
    q = ConsentQueue(tmp_path / "consent", authority=authority, home=h4_home())
    request = FakeAgent(q, "codex").ask(hold=timedelta(milliseconds=300))
    t0 = time.monotonic()
    assert q.await_decision(request.id, 10, poll=0.05) is None
    assert time.monotonic() - t0 < 5                             # it ended at expiry, not at the wait's end
    assert q.get(request.id)[1] == "pending"                    # the hook's to mark undecided (UNDECIDED_GRACE)
    assert q.mark_undecided(request.id).state == "undecided"    # past expires_at, at the real deadline
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
    assert queue.answer(request.id, payload, sig).decision == "deny"
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
        queue.answer(request.id, payload, sig)


# --- replay refusals (H5) ----------------------------------------------------------------------------------

def test_refuses_an_answer_for_another_request(queue, keys, authority):
    agent = FakeAgent(queue)
    a, b = agent.ask("touch a"), agent.ask("touch b")
    payload, sig = signed(queue, b, keys["phone"])
    with pytest.raises(AnswerRefused, match="another request"):
        queue.answer(a.id, payload, sig)
    assert queue.get(a.id)[1] == "pending"


def test_refuses_an_answer_over_a_changed_request_digest(queue, keys, authority):
    request = FakeAgent(queue).ask()
    payload, sig = signed(queue, request, keys["phone"], request_digest="0" * 64)
    with pytest.raises(AnswerRefused, match="another version"):
        queue.answer(request.id, payload, sig)
    assert queue.get(request.id)[1] == "pending"


def test_refuses_an_answer_naming_another_expiry(queue, keys, authority):
    request = FakeAgent(queue).ask()
    payload, sig = signed(queue, request, keys["phone"], expires_at=utc_stamp(NOW + timedelta(days=1)))
    with pytest.raises(AnswerRefused, match="another expiry"):
        queue.answer(request.id, payload, sig)


def test_refuses_an_answer_after_expiry(queue, clock, keys, authority):
    request = FakeAgent(queue).ask(hold=timedelta(seconds=30))
    payload, sig = signed(queue, request, keys["phone"])        # signed in time, delivered late
    clock.now = NOW + timedelta(seconds=30)
    with pytest.raises(AnswerRefused, match="already expired"):
        queue.answer(request.id, payload, sig)
    assert queue.get(request.id)[1] == "pending"                # applied nothing; the hook marks it undecided
    assert queue.decision(request.id) is None
    clock.now = NOW + timedelta(seconds=30) + UNDECIDED_GRACE
    assert queue.get(request.id)[1] == "expired"


def test_refuses_a_second_answer_to_a_terminal_request(queue, keys, authority):
    request = FakeAgent(queue).ask()
    payload, sig = signed(queue, request, keys["phone"])
    queue.answer(request.id, payload, sig)
    deny, dsig = signed(queue, request, keys["laptop"], "deny")
    with pytest.raises(AnswerRefused, match="already allowed"):
        queue.answer(request.id, deny, dsig)
    with pytest.raises(AnswerRefused, match="already allowed"):
        queue.answer(request.id, payload, sig)   # the same answer again
    assert queue.decision(request.id).decision == "allow"


def test_refuses_an_answer_for_another_queue(queue, keys, authority):
    request = FakeAgent(queue).ask()
    payload, sig = signed(queue, request, keys["phone"], queue_id="cq-" + "1" * 32)
    with pytest.raises(AnswerRefused, match="another queue"):
        queue.answer(request.id, payload, sig)


# --- an answer is authority only when it verifies (H4) ----------------------------------------------------

def test_a_yes_signed_by_another_key_than_it_names_is_no_decision(queue, keys, authority):
    request = FakeAgent(queue).ask()
    payload = answer_payload(queue_id=queue.queue_id, request=request, decision="allow", signer_fp=keys["phone"].fp,
                             signed_at=utc_stamp(NOW), receipts_head=receipts_head(queue))
    sig = keys["stranger"].sign(answer_message(payload))
    with pytest.raises(AnswerRefused, match="does not verify"):
        queue.answer(request.id, payload, sig)
    assert queue.get(request.id)[1] == "pending"


@pytest.mark.parametrize("namespace", ["levain-cockpit", auth.NAMESPACE, "levain-confirm"])
def test_a_signature_in_another_namespace_is_no_decision(queue, keys, authority, namespace):
    request = FakeAgent(queue).ask()
    payload = answer_payload(queue_id=queue.queue_id, request=request, decision="allow", signer_fp=keys["phone"].fp,
                             signed_at=utc_stamp(NOW), receipts_head=receipts_head(queue))
    sig = keys["phone"].sign(answer_message(payload), namespace)
    with pytest.raises(AnswerRefused, match="does not verify"):
        queue.answer(request.id, payload, sig)


def test_an_unsigned_or_garbage_yes_is_no_decision(queue, keys, authority):
    request = FakeAgent(queue).ask()
    payload, _ = signed(queue, request, keys["phone"])
    for sig in ("", "yes", None):
        with pytest.raises(AnswerRefused):
            queue.answer(request.id, payload, sig)
    assert queue.get(request.id)[1] == "pending"


def test_unknown_and_revoked_keys_are_refused(queue, keys, authority):
    request = FakeAgent(queue).ask()
    for name, why in (("stranger", "not an enrolled key"), ("revoked", "revoked")):
        payload, sig = signed(queue, request, keys[name])
        with pytest.raises(AnswerRefused, match=why):
            queue.answer(request.id, payload, sig)


def test_an_allow_needs_a_key_class_covering_the_tier(queue, keys, authority):
    agent = FakeAgent(queue)
    t2, t3 = agent.ask("touch two"), agent.ask("touch three", tier="T3")
    payload, sig = signed(queue, t2, keys["presence"])
    with pytest.raises(AnswerRefused, match="does not cover T2"):
        queue.answer(t2.id, payload, sig)
    payload, sig = signed(queue, t3, keys["phone"])
    with pytest.raises(AnswerRefused, match="does not cover T3"):
        queue.answer(t3.id, payload, sig)
    payload, sig = signed(queue, t3, keys["laptop"])
    assert queue.answer(t3.id, payload, sig).decision == "allow"


def test_a_payload_with_extra_or_missing_fields_is_refused(queue, keys, authority):
    request = FakeAgent(queue).ask()
    payload, sig = signed(queue, request, keys["phone"])
    with pytest.raises(AnswerRefused, match="not an answer record"):
        queue.answer(request.id, {**payload, "also": "x"}, sig)
    short = dict(payload)
    short.pop("expires_at")
    with pytest.raises(AnswerRefused, match="not an answer record"):
        queue.answer(request.id, short, sig)


# --- the row is an index, never authority (fix round 1: F1-F5) -------------------------------------------

def _raw(queue, *statements):
    conn = sqlite3.connect(queue.path, isolation_level=None)
    try:
        for sql, args in statements:
            conn.execute(sql, args)
    finally:
        conn.close()


def test_a_raw_update_to_allowed_is_no_decision(queue, keys):
    """F1: a process that writes queue.db cannot make an allow: the state column is never authority."""
    request = FakeAgent(queue).ask()
    _raw(queue, ("UPDATE requests SET state = 'allowed', decided_at = ?, signer_fp = ? WHERE id = ?",
                 (utc_stamp(NOW), keys["phone"].fp, request.id)))
    assert queue.get(request.id)[1] == "allowed"           # the index says allowed...
    assert queue.decision(request.id) is None               # ...and nothing is delivered
    assert queue.await_decision(request.id, 0.2, poll=0.01) is None


def test_a_raw_insert_of_an_allowed_row_is_no_decision(queue, keys):
    """F1: a sealed row inserted terminal, carrying a real signature over ANOTHER request, is no decision."""
    other = FakeAgent(queue).ask("touch other")
    payload, sig = signed(queue, other, keys["phone"])
    queue.answer(other.id, payload, sig)
    forged = ExternalGateRequest.create(
        agent="claude_code", agent_session_id="sess-1", event_id="rand:" + "0" * 32, tool_name="Bash",
        tool_input_digest=tool_input_digest({"command": "rm -rf /"}), display="Bash rm", cwd="/work/proj", tier="T2",
        received_at=utc_stamp(NOW), expires_at=utc_stamp(NOW + timedelta(minutes=5)))
    cols = list(forged.fields())
    _raw(queue, (f"INSERT INTO requests ({', '.join(cols)}, state, decided_at, answer, signature, signer_fp) "
                 f"VALUES ({', '.join('?' for _ in cols)}, 'allowed', ?, ?, ?, ?)",
                 (*forged.fields().values(), utc_stamp(NOW), answer_message(payload).decode(), sig, keys["phone"].fp)))
    assert queue.get(forged.id)[1] == "allowed"
    assert queue.decision(forged.id) is None
    assert queue.decision(other.id).decision == "allow"     # the real one still verifies


def test_a_stored_deny_relabelled_allow_is_no_decision(queue, keys):
    """F1: the stored answer must be the record of the row's own decision."""
    request = FakeAgent(queue).ask()
    payload, sig = signed(queue, request, keys["phone"], "deny")
    queue.answer(request.id, payload, sig)
    _raw(queue, ("DROP TRIGGER requests_frozen", ()),
         ("UPDATE requests SET state = 'allowed' WHERE id = ?", (request.id,)))
    assert queue.decision(request.id) is None


def test_a_key_revoked_after_it_answered_delivers_nothing(tmp_path, keys, clock):
    """F1+F2: delivery re-verifies against a replay taken then; a revocation after the answer voids it."""
    (tmp_path / "auth").mkdir()
    log = Log(tmp_path / "auth", keys)
    log.enrol("laptop", "laptop-presence")
    log.enrol("phone", "display-phone")
    queue = ConsentQueue(tmp_path / "consent", authority=log.source, clock=clock, home=h4_home())
    request = FakeAgent(queue).ask()
    payload, sig = signed(queue, request, keys["phone"])
    assert queue.answer(request.id, payload, sig).decision == "allow"
    assert queue.decision(request.id).decision == "allow"
    log.enrol("presence", "presence")                      # an unrelated append does not void it
    assert queue.decision(request.id).decision == "allow"
    log.revoke("phone")
    assert queue.decision(request.id) is None
    assert queue.await_decision(request.id, 0.2, poll=0.01) is None


def test_an_answer_is_checked_against_a_fresh_replay(tmp_path, keys, clock):
    """F2: a key revoked before the answer arrives is refused, though it was active when the queue was made."""
    (tmp_path / "auth").mkdir()
    log = Log(tmp_path / "auth", keys)
    log.enrol("laptop", "laptop-presence")
    log.enrol("phone", "display-phone")
    queue = ConsentQueue(tmp_path / "consent", authority=log.source, clock=clock, home=h4_home())
    request = FakeAgent(queue).ask()
    payload, sig = signed(queue, request, keys["phone"])
    log.revoke("phone")
    with pytest.raises(AnswerRefused, match="revoked"):
        queue.answer(request.id, payload, sig)
    assert queue.get(request.id)[1] == "pending"


def test_an_unavailable_authority_is_no_decision(tmp_path, keys, clock):
    """F2: no replay, no authority: an answer is refused and a stored allow is not delivered."""
    (tmp_path / "auth").mkdir()
    log = Log(tmp_path / "auth", keys)
    log.enrol("laptop", "laptop-presence")
    log.enrol("phone", "display-phone")
    queue = ConsentQueue(tmp_path / "consent", authority=log.source, clock=clock, home=h4_home())
    a, b = FakeAgent(queue).ask("a"), FakeAgent(queue).ask("b")
    payload, sig = signed(queue, a, keys["phone"])
    queue.answer(a.id, payload, sig)
    log.path.rename(tmp_path / "auth" / "moved.db")          # the log is gone: nothing can be replayed
    assert queue.decision(a.id) is None
    payload, sig = signed(queue, b, keys["phone"])
    with pytest.raises(AnswerRefused, match="cannot be replayed"):
        queue.answer(b.id, payload, sig)


def test_an_answer_whose_lock_wait_crosses_expiry_is_refused(queue, clock, keys, monkeypatch):
    """F3: the clock is read after the write lock is held, so time spent waiting for it counts."""
    import levain.consent.queue as qmod
    request = FakeAgent(queue).ask(hold=timedelta(seconds=30))
    payload, sig = signed(queue, request, keys["phone"])
    armed = {"on": False}
    real_verify, real_connect = qmod.verify_link_sig, queue._connect

    def verify(*a, **kw):
        ok = real_verify(*a, **kw)
        armed["on"] = True                                  # verified before expiry...
        return ok

    def connect():
        if armed["on"]:                                     # ...then the lock wait outlasts the expiry
            clock.now = NOW + timedelta(seconds=31)
        return real_connect()

    monkeypatch.setattr(qmod, "verify_link_sig", verify)
    monkeypatch.setattr(queue, "_connect", connect)
    with pytest.raises(AnswerRefused, match="already expired"):
        queue.answer(request.id, payload, sig)
    assert queue.get(request.id)[1] == "pending"                # applied nothing; the hook marks it undecided
    assert queue.decision(request.id) is None


def test_withdraw_after_expiry_records_the_expiry(queue, clock):
    """F3: a due request (its hold ended UNDECIDED_GRACE ago) expires before a withdraw is considered."""
    request = FakeAgent(queue).ask(hold=timedelta(seconds=30))
    clock.now = NOW + timedelta(seconds=30) + UNDECIDED_GRACE
    assert queue.withdraw(request.id) is False
    assert queue.receipt(request.id)["state"] == "expired"


def test_two_identical_filings_are_two_requests(queue, keys):
    """F4: one event, one request: the same gate filed twice at one instant is two requests, one answer each."""
    agent = FakeAgent(queue)
    a, b = agent.ask(), agent.ask()
    assert a.id != b.id and a.digest != b.digest
    payload, sig = signed(queue, a, keys["phone"])
    queue.answer(a.id, payload, sig)
    assert queue.decision(a.id).decision == "allow"
    assert queue.get(b.id)[1] == "pending" and queue.decision(b.id) is None
    with pytest.raises(AnswerRefused, match="another request"):
        queue.answer(b.id, payload, sig)


def test_one_hook_event_is_filed_once(queue):
    """F4: a hook-supplied event id is sealed; filing it again is an error, never the earlier request."""
    from levain.cockpit.consent import ConsentError
    kw = dict(agent="codex", agent_session_id="s", tool_name="Bash", tool_input={"command": "ls"}, cwd="/w",
              hold=timedelta(minutes=1), event_id="evt-1")
    first = queue.file(**kw)
    assert first.event_id == "hook:evt-1" and first.seal_matches()
    with pytest.raises(ConsentError, match="already filed"):
        queue.file(**kw)


def test_polling_a_pending_request_takes_no_write_lock(queue):
    """F5: a poll that has nothing to expire reads; it does not queue behind a writer."""
    request = FakeAgent(queue).ask()
    blocker = sqlite3.connect(queue.path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    got = {}
    reader = threading.Thread(target=lambda: got.setdefault("r", queue.get(request.id)), daemon=True)
    try:
        reader.start()
        reader.join(3)
        assert not reader.is_alive(), "get() waited for the write lock"
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
        reader.join(35)
    assert got["r"][1] == "pending"


# --- fix round 2: G1-G5 ---------------------------------------------------------------------------------------

def test_a_late_signed_answer_with_a_forged_decided_at_is_no_decision(queue, clock, keys):
    """G1: "in time" is the signed signed_at, never the row's decided_at a raw writer can set."""
    request = FakeAgent(queue).ask(hold=timedelta(seconds=30))
    clock.now = NOW + timedelta(seconds=40) + UNDECIDED_GRACE
    assert queue.get(request.id)[1] == "expired"
    payload, sig = signed(queue, request, keys["phone"])     # a real signature, made after the expiry
    _raw(queue, ("DROP TRIGGER requests_frozen", ()),
         ("UPDATE requests SET state = 'allowed', decided_at = ?, answer = ?, signature = ?, signer_fp = ? "
          "WHERE id = ?", (utc_stamp(NOW), answer_message(payload).decode(), sig, keys["phone"].fp, request.id)))
    assert queue.decision(request.id) is None


def test_an_answer_signed_outside_the_request_window_is_refused(queue, keys):
    """G1: signed_at must fall in [received_at, expires_at)."""
    request = FakeAgent(queue).ask(hold=timedelta(seconds=30))
    for when, why in ((NOW + timedelta(seconds=30), "after the request expired"),
                      (NOW - timedelta(seconds=1), "before the request was filed")):
        payload, sig = signed(queue, request, keys["phone"], signed_at=utc_stamp(when))
        with pytest.raises(AnswerRefused, match=why):
            queue.answer(request.id, payload, sig)
    payload, sig = signed(queue, request, keys["phone"], signed_at="yesterday")
    with pytest.raises(AnswerRefused, match="not a timestamp"):
        queue.answer(request.id, payload, sig)
    assert queue.get(request.id)[1] == "pending"


def test_the_queue_builds_the_payload_signed_at_its_own_clock(queue, clock, keys):
    """G1: ConsentQueue.answer_payload stamps signed_at from the queue's clock."""
    request = FakeAgent(queue).ask()
    clock.now = NOW + timedelta(seconds=7)
    payload = queue.answer_payload(request.id, "allow", keys["phone"].fp)
    assert payload["signed_at"] == utc_stamp(NOW + timedelta(seconds=7))
    assert queue.answer(request.id, payload, keys["phone"].sign(answer_message(payload))).decision == "allow"


def test_authority_must_be_the_replaying_source(tmp_path, authority):
    """G2: a snapshot, a lambda returning one, or a subclass is refused at construction."""
    from levain.cockpit.consent import ConsentError
    from levain.consent.queue import AuthoritySource
    snap = authority()

    class Stale(AuthoritySource):
        def __call__(self):
            return snap

    for bad in (snap, lambda: snap, Stale(tmp_path / "x.db", anchor=None)):
        with pytest.raises(ConsentError, match="AuthoritySource"):
            ConsentQueue(tmp_path / "consent", authority=bad, home=h4_home())


def test_no_authority_is_no_decision(tmp_path, keys, clock):
    """An empty log, a log marked uninitialised, and an anchor naming another store each read as no
    authority: the source raises with RequestAuthority.capture's reason and no answer is authority."""
    from levain.consent.queue import AuthoritySource
    log = Log(tmp_path, keys)
    request_queue = ConsentQueue(tmp_path / "consent", authority=log.source, clock=clock, home=h4_home())
    request = FakeAgent(request_queue).ask()
    payload = request_queue.answer_payload(request.id, "allow", keys["laptop"].fp)
    sig = keys["laptop"].sign(answer_message(payload))

    with pytest.raises(auth.AuthorityError, match="not initialised"):
        log.source()
    with pytest.raises(AnswerRefused, match="cannot be replayed"):
        request_queue.answer(request.id, payload, sig)

    log.enrol("laptop", "laptop-presence")
    conn = sqlite3.connect(log.path, isolation_level=None)
    conn.execute("INSERT INTO meta (key, value) VALUES (?, 'authority migrated')", (UNINITIALISED,))
    conn.close()
    with pytest.raises(auth.AuthorityError, match="not initialised"):
        log.source()
    with pytest.raises(AnswerRefused, match="cannot be replayed"):
        request_queue.answer(request.id, payload, sig)

    conn = sqlite3.connect(log.path, isolation_level=None)
    conn.execute("DELETE FROM meta WHERE key = ?", (UNINITIALISED,))
    conn.close()
    anchor = tmp_path / "anchor"
    auth.advance_anchor(anchor, log.source())
    assert AuthoritySource(log.path, anchor=anchor)().seq == 1
    (tmp_path / "other").mkdir()
    other = Log(tmp_path / "other", keys)
    other.enrol("laptop", "laptop-presence")
    with pytest.raises(auth.AuthorityError, match="refused"):
        AuthoritySource(other.path, anchor=anchor)()
    assert request_queue.get(request.id)[1] == "pending"
    assert request_queue.answer(request.id, payload, sig).decision == "allow"


def test_a_retried_hook_event_is_refused_later_too(queue, clock):
    """G3: the same hook event filed again a second later (a new seal) is still refused; other sessions are not."""
    from levain.cockpit.consent import ConsentError
    kw = dict(agent="codex", agent_session_id="s", tool_name="Bash", tool_input={"command": "ls"}, cwd="/w",
              hold=timedelta(minutes=1), event_id="evt-1")
    queue.file(**kw)
    clock.now = NOW + timedelta(seconds=1)
    with pytest.raises(ConsentError, match="already filed"):
        queue.file(**kw)
    assert queue.file(**{**kw, "agent_session_id": "s2"}).event_id == "hook:evt-1"
    assert len(queue.pending()) == 2


def test_a_decision_carries_the_digest_of_the_request_it_answered(queue, keys):
    """G4: an adapter can bind delivery to the input it showed."""
    request = FakeAgent(queue).ask()
    payload, sig = signed(queue, request, keys["phone"])
    assert queue.answer(request.id, payload, sig).request_digest == request.digest
    assert queue.decision(request.id).request_digest == request.digest


@pytest.mark.parametrize("poll", [-1.0, 0.0, float("nan")])
def test_a_bad_poll_does_not_crash_the_wait(queue, poll):
    """G5: poll is clamped to >= 1ms."""
    request = FakeAgent(queue).ask()
    assert queue.await_decision(request.id, 0.05, poll=poll) is None


# --- the store ---------------------------------------------------------------------------------------------

def test_terminal_rows_and_receipts_are_frozen_by_the_database(queue, keys, authority):
    request = FakeAgent(queue).ask()
    payload, sig = signed(queue, request, keys["phone"])
    queue.answer(request.id, payload, sig)
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
        queue.answer(request.id, payload, sig)


def test_store_is_owner_only_and_refuses_a_shared_directory(tmp_path, queue):
    FakeAgent(queue).ask()
    assert stat.S_IMODE(os.stat(queue.directory).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(queue.path).st_mode) == 0o600
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    os.chmod(shared, 0o755)
    with pytest.raises(QueueStoreError, match="0700"):
        FakeAgent(ConsentQueue(shared, authority=queue._authority, home=h4_home())).ask()


def test_default_dir_is_levain_home_consent(monkeypatch, tmp_path):
    from levain.consent import default_queue_dir
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    assert default_queue_dir() == (tmp_path / "lh" / "consent").resolve()


def test_sweep_and_pending(queue, clock):
    agent = FakeAgent(queue)
    short, long_ = agent.ask("a", hold=timedelta(seconds=10)), agent.ask("b", hold=timedelta(hours=1))
    assert [r.id for r in queue.pending()] == [short.id, long_.id]
    clock.now = NOW + timedelta(seconds=10)
    assert queue.sweep() == 0                                   # the hook's to mark undecided first
    clock.now = NOW + timedelta(seconds=11) + UNDECIDED_GRACE
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


def test_display_masks_by_key_token_with_a_typed_marker():
    # seen red (probe_fix1 item 2): a substring match masked author, auth_url and a boolean `private`
    shown = redact_display("mcp__x__y", {"author": "alice", "private": False, "auth_url": "http://x"})
    assert shown == 'mcp__x__y {"auth_url": "http://x", "author": "alice", "private": false}'
    shown = redact_display("mcp__x__y", {"api_key": "k", "apiKey": "k", "x-api-key": "k", "AWS_ACCESS_KEY": "k",
                                         "privateKey": "k", "Authorization": "k", "session_token": "k",
                                         "cookies": ["c"], "passwd": 7, "pass_word": "k", "bearer": "k"})
    assert '"k"' not in shown and '"c"' not in shown and ": 7" not in shown
    assert '"api_key": "[redacted: str, 1 chars]"' in shown and '"passwd": "[redacted: int]"' in shown


def test_display_masks_a_secret_word_inside_a_run_together_key():
    # seen red (probe_fix2 on af2a220): whole-token masking showed accesstoken, secretkey and clienttoken
    shown = redact_display("mcp__x__y", {"accesstoken": "k", "secretkey": "k", "clienttoken": "k",
                                         "githubtoken": "k", "dbpassword": "k", "author": "alice",
                                         "auth_url": "http://x", "private": False})
    assert '"k"' not in shown
    assert '"accesstoken": "[redacted: str, 1 chars]"' in shown
    assert '"author": "alice"' in shown and '"auth_url": "http://x"' in shown and '"private": false' in shown


def test_display_masks_auth_as_its_own_word_and_a_passphrase():
    # seen red (run at 3eb69f3): word-token masking showed auth, basic_auth, x-auth and auth_header in clear
    shown = redact_display("mcp__http__request", {"auth": "Basic dXNlcjpodW50ZXIy", "basic_auth": "k", "proxy_auth": "k",
                                                   "auth_header": "k", "headers": {"X-Auth": "s3cr3t"},
                                                   "passphrase": "k", "pass_phrase": "k", "basicauth": "k",
                                                   "authheader": "k", "pwd": "k", "jwt": "k", "pass": "k",
                                                   "private": "-----BEGIN OPENSSH PRIVATE KEY-----"})
    assert "dXNlcjpodW50ZXIy" not in shown and "s3cr3t" not in shown and '"k"' not in shown
    assert "BEGIN OPENSSH" not in shown


def test_display_still_shows_keys_about_auth_rather_than_holding_it():
    shown = redact_display("mcp__x__y", {"auth_url": "http://x", "authType": "oauth", "author": "alice",
                                         "auth_method": "basic", "authenticated": True, "private": False,
                                         "passenger": "p"})
    assert shown == ('mcp__x__y {"authType": "oauth", "auth_method": "basic", "auth_url": "http://x", '
                     '"authenticated": true, "author": "alice", "passenger": "p", "private": false}')


def test_policy_seam_decides_nothing_without_a_grant(queue):
    """No standing grant in the log: the seam answers nothing and leaves the request pending."""
    from levain.consent.policy import decide
    request = FakeAgent(queue).ask()
    assert decide(request, queue, posture="default", tool_input={"command": "touch /tmp/x"}) is None
    assert queue.get(request.id)[1] == "pending"


# --- Q4: standing consent grants --------------------------------------------------------------------------------

def _granted_queue(tmp_path, keys, tools=("Edit", "Write")):
    """A real signed log holding one consent grant g1 (``tools``, default Edit and Write; posture default) over
    ``tmp_path/proj``, a queue over it, and a directory outside it with a symlink to it from inside the workspace."""
    from levain.consent import policy
    (tmp_path / "auth").mkdir()
    log = Log(tmp_path / "auth", keys)
    log.enrol("laptop", "laptop-presence")
    ws = Path(os.path.realpath(tmp_path / "proj"))
    (ws / "sub").mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    os.symlink(outside, ws / "escape")
    log.link({"op": "create_grant", "grant": {
        "id": "g1", "kind": "consent", "agent": "claude_code", "tools": sorted(tools), "workspace": str(ws),
        "postures": ["default"], "expires_at": utc_stamp(datetime.now(timezone.utc) + timedelta(days=1))}})
    q = ConsentQueue(tmp_path / "consent", authority=log.source, home=h4_home())

    def ask(tool_input, *, tool="Edit", cwd=str(ws), posture="default"):
        r = q.file(agent="claude_code", agent_session_id="s1", tool_name=tool, tool_input=tool_input, cwd=cwd,
                   hold=timedelta(minutes=5))
        return r, policy.guarded_policy(r, q, posture=posture, tool_input=tool_input)
    return log, q, ws, outside, ask


def test_a_standing_grant_answers_only_inside_its_workspace(tmp_path, keys):
    """Q4 criteria: the cwd and every file target must resolve inside the grant's workspace, under a posture it
    names. Reproduced by a probe with the containment test forced true: all four outside cases were allowed."""
    log, q, ws, outside, ask = _granted_queue(tmp_path, keys)
    inside = {"file_path": str(ws / "sub" / "a.txt"), "old_string": "a", "new_string": "b"}
    r, d = ask(inside)
    assert (d.decision, d.via, d.grant_id, d.signer_fp, d.posture) == ("allow", "grant", "g1", None, "default")
    assert q.get(r.id)[1] == "allowed"
    face = q.receipt(r.id)["face"]
    assert face["gate"] == {"verdict": "approved", "by": "binding", "binding_id": "g1"}
    assert face["authority_scope"]["grant"].startswith(f"standing-grant:g1@{d.grant_seq}:{d.grant_hash}")
    for tool_input, kw in (({"file_path": str(outside / "x")}, {}),             # a target outside
                           ({"file_path": str(ws / "escape" / "x")}, {}),       # a symlink out of the workspace
                           ({"file_path": "../elsewhere/x"}, {}),               # a relative escape
                           (inside, {"cwd": str(outside)}),                     # the session runs outside
                           (inside, {"posture": "acceptEdits"}),                # a posture the grant does not name
                           (inside, {"posture": None})):                        # no posture reported
        r2, d2 = ask(tool_input, **kw)
        assert d2 is None and q.get(r2.id)[1] == "pending", (tool_input, kw)


def test_a_grant_answer_is_delivered_only_while_the_grant_still_answers_it(tmp_path, keys):
    """Delivery re-verifies a grant answer in a fresh replay with the input it was filed for: a raw write can only
    take it away (a copied answer verifies on no other row), and a revoked grant delivers nothing it gave."""
    log, q, ws, outside, ask = _granted_queue(tmp_path, keys)
    inside = {"file_path": str(ws / "a.txt"), "content": "x"}
    r, d = ask(inside, tool="Write")
    assert d is not None and q.decision(r.id, tool_input=inside, posture="default") == d
    assert q.decision(r.id) is None                                             # no input: no delivery
    assert q.decision(r.id, tool_input={**inside, "content": "y"}, posture="default") is None
    assert q.decision(r.id, tool_input=inside, posture="acceptEdits") is None
    other = q.file(agent="claude_code", agent_session_id="s1", tool_name="Write", tool_input=inside, cwd=str(ws),
                   hold=timedelta(minutes=5))
    raw = sqlite3.connect(q.path)
    stored = raw.execute("SELECT answer FROM requests WHERE id = ?", (r.id,)).fetchone()[0]
    raw.execute("UPDATE requests SET state = 'allowed', via = 'grant', answer = ?, decided_at = ? WHERE id = ?",
                (stored, utc_stamp(datetime.now(timezone.utc)), other.id))
    raw.commit()
    raw.close()
    assert q.decision(other.id, tool_input=inside, posture="default") is None
    log.link({"op": "revoke_grant", "grant_id": "g1"}, signer="laptop")
    assert q.decision(r.id, tool_input=inside, posture="default") is None
    assert ask(inside, tool="Write")[1] is None


# --- Q4 L3 r1 -----------------------------------------------------------------------------------------------------

READ_TOOLS = ("Glob", "Grep", "LS", "NotebookRead", "Read", "mcp__fs__read_file")


def test_every_path_field_of_a_non_shell_tool_is_checked(tmp_path, keys):
    """r1 item 1 (complement HIGH 1): a Read grant on the workspace answered a Read of a file outside it, because only
    Edit/Write/MultiEdit/NotebookEdit and MCP tools had their paths read. Reproduced red on 8135c4b."""
    log, q, ws, outside, ask = _granted_queue(tmp_path, keys, tools=READ_TOOLS)
    secret = str(outside / "id_rsa")
    for tool, tool_input in (("Read", {"file_path": str(ws / "a.txt")}),
                             ("Glob", {"pattern": "src/**/*.py"}),
                             ("Glob", {"pattern": "*.{ts,tsx}", "path": str(ws / "sub")}),
                             ("Grep", {"pattern": "x", "path": str(ws)}),
                             ("LS", {"path": str(ws / "sub")})):
        assert ask(tool_input, tool=tool)[1] is not None, (tool, tool_input)               # the controls
    for tool, tool_input in (("Read", {"file_path": secret}),
                             ("Read", {"file_path": "~/.ssh/id_rsa"}),
                             ("Read", {"file_path": 3}),                                   # not a string
                             ("Read", {"file_path": [secret]}),
                             ("Grep", {"pattern": "x", "path": str(outside)}),
                             ("LS", {"path": str(outside)}),
                             ("NotebookRead", {"notebook_path": str(outside / "n.ipynb")}),
                             ("Glob", {"pattern": str(outside / "*")}),                    # absolute
                             ("Glob", {"pattern": "~/.ssh/*"}),
                             ("Glob", {"pattern": "../elsewhere/*"}),
                             ("Glob", {"pattern": "sub/../../elsewhere/*"}),
                             ("Glob", {"pattern": "{" + str(outside) + ",x}/*"}),          # an absolute alternative
                             ("Glob", {"pattern": 7}),
                             ("Glob", {"pattern": "*", "path": str(outside)}),
                             ("mcp__fs__read_file", {"path": [str(ws / "a")]})):          # a list where one path goes
        r, d = ask(tool_input, tool=tool)
        assert d is None and q.get(r.id)[1] == "pending", (tool, tool_input)


def test_a_workspace_swapped_for_a_symlink_answers_and_delivers_nothing(tmp_path, keys):
    """r1 item 2 (codex HIGH 2): the signed workspace string was re-resolved per decision, so renaming the directory
    and putting a symlink to / in its place widened the grant to the whole disk, and an answer given before the swap
    was still delivered. Reproduced red on 8135c4b."""
    log, q, ws, outside, ask = _granted_queue(tmp_path, keys, tools=("Read",))
    inside = {"file_path": str(ws / "a.txt")}
    r, d = ask(inside, tool="Read")
    assert d is not None
    os.rename(ws, tmp_path / "proj.old")
    os.symlink("/", ws)
    r2, d2 = ask({"file_path": "/etc/hosts"}, tool="Read", cwd=str(tmp_path))
    assert d2 is None and q.get(r2.id)[1] == "pending"
    assert q.decision(r.id, tool_input=inside, posture="default") is None


def test_a_grant_answers_nothing_once_its_workspace_is_home(tmp_path, keys, monkeypatch):
    """r1 item 2: the home / above-home refusal is re-applied at decision, so a log written on a machine whose home
    differs grants no home. Reproduced red on 8135c4b (HOME set to the workspace after the grant was signed)."""
    log, q, ws, outside, ask = _granted_queue(tmp_path, keys, tools=("Read",))
    inside = {"file_path": str(ws / "a.txt")}
    r, d = ask(inside, tool="Read")
    assert d is not None
    for home in (ws, ws / "sub"):
        q.home = home   # H4: the queue holds the home its hook bound (was $HOME, read at each decision)
        assert q.decision(r.id, tool_input=inside, posture="default") is None, home
        assert ask(inside, tool="Read")[1] is None, home


@pytest.mark.parametrize("where", ["a symlink", "missing", "not normal"])
def test_a_grant_workspace_must_be_a_real_directory_when_it_is_signed(tmp_path, keys, where):
    """r1 item 2 at append: the signed string is the directory's realpath, exactly. Reproduced red on 8135c4b for a
    symlink and a missing directory (a non-normal path was already refused by the replay)."""
    (tmp_path / "auth").mkdir()
    log = Log(tmp_path / "auth", keys)
    log.enrol("laptop", "laptop-presence")
    real = Path(os.path.realpath(tmp_path)) / "real"
    real.mkdir()
    os.symlink(real, real.parent / "link")
    workspace = {"a symlink": str(real.parent / "link"), "missing": str(real.parent / "missing"),
                 "not normal": str(real) + "/"}[where]
    with pytest.raises(auth.AuthorityError):
        log.link({"op": "create_grant", "grant": {
            "id": "g1", "kind": "consent", "agent": "claude_code", "tools": ["Read"], "workspace": workspace,
            "postures": ["default"], "expires_at": utc_stamp(datetime.now(timezone.utc) + timedelta(days=1))}})


def test_a_target_through_a_symlink_inside_the_workspace_matches_nothing(tmp_path, keys):
    """r1 item 3 (codex HIGH 3, static case): a link inside the workspace that points inside it today can point at
    agent config tomorrow; a target whose path crosses any existing symlink matches nothing. Reproduced red on
    8135c4b (both link targets were allowed)."""
    log, q, ws, outside, ask = _granted_queue(tmp_path, keys)
    os.symlink(ws / "sub", ws / "link")
    os.symlink(ws / "sub" / "real.txt", ws / "leaf")
    assert ask({"file_path": str(ws / "sub" / "a.txt"), "content": "x"}, tool="Write")[1] is not None
    for target in (str(ws / "link" / "a.txt"), "link/a.txt", str(ws / "leaf"), str(ws / "escape" / ".." / "a.txt"),
                   # r2 item 2 (codex r2 #3): a `..` component matches nothing, link or no link
                   str(ws / "link" / ".." / "a.txt"), str(ws / "sub" / ".." / "a.txt"), "sub/../a.txt"):
        r, d = ask({"file_path": target, "content": "x"}, tool="Write")
        assert d is None and q.get(r.id)[1] == "pending", target


def _forge_grant_answer(q, request, **link):
    from levain.consent.record import grant_answer_payload
    record = grant_answer_payload(queue_id=q.queue_id, request=request, grant_id="g1", posture="default", **link)
    raw = sqlite3.connect(q.path)
    raw.execute("UPDATE requests SET state = 'allowed', via = 'grant', answer = ?, decided_at = ? WHERE id = ?",
                (answer_message(record).decode("utf-8"), utc_stamp(datetime.now(timezone.utc)), request.id))
    raw.commit()
    raw.close()


def test_a_grant_answer_is_delivered_only_for_the_link_it_names(tmp_path, keys):
    """r1 item 5 (codex MED 4): delivery never compared the recorded grant_seq / grant_hash with the fresh replay, so
    a raw write naming a link that never created the grant was delivered with that provenance; and an answer given
    before a renew is still delivered after it. Reproduced red on 8135c4b."""
    log, q, ws, outside, ask = _granted_queue(tmp_path, keys)
    inside = {"file_path": str(ws / "a.txt"), "content": "x"}
    r, d = ask(inside, tool="Write")
    assert d is not None
    other = q.file(agent="claude_code", agent_session_id="s1", tool_name="Write", tool_input=inside, cwd=str(ws),
                   hold=timedelta(minutes=5))
    _forge_grant_answer(q, other, grant_seq=1, grant_hash="0" * 64)
    assert q.decision(other.id, tool_input=inside, posture="default") is None
    third = q.file(agent="claude_code", agent_session_id="s1", tool_name="Write", tool_input=inside, cwd=str(ws),
                   hold=timedelta(minutes=5))
    _forge_grant_answer(q, third, grant_seq=d.grant_seq, grant_hash=d.grant_hash)
    assert q.decision(third.id, tool_input=inside, posture="default") == dataclasses.replace(
        d, request_id=third.id, decided_at=q.decision(third.id, tool_input=inside, posture="default").decided_at,
        request_digest=third.digest)                                                  # the control: the real link
    now = datetime.now(timezone.utc)
    log.link({"op": "renew", "grant_id": "g1", "expires_at": utc_stamp(now + timedelta(days=2)),
              "signed_at": utc_stamp(now)}, at=parse_stamp(utc_stamp(now)))
    assert q.decision(r.id, tool_input=inside, posture="default") is None
    assert ask(inside, tool="Write")[1] is not None                                    # the renewed grant answers anew
