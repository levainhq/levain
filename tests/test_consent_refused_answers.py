"""A refused answer kept as evidence (1019+45 B5, S-R1): the ``refused_answers`` table beside the consent queue.

An answer or late ruling the queue refuses is kept only when it is EVIDENCE: a well-formed record for this queue,
request and digest whose signature verifies under the key it names (an enrolled key's stored public key; for a key
the authority log does not hold, the key its SSHSIG embeds). Kept refusals never move a request, never change the
refusal the signer is given, and are never receipts (the Q11 log and head are unchanged by them).

Keys and authority are the real ones of ``tests/test_consent_queue.py``: enrolled ``laptop`` (laptop-presence),
``phone`` (display-phone), ``presence`` (presence); ``revoked`` (display-phone, revoked); ``stranger`` never enrolled.
"""
from __future__ import annotations

import base64
import json
import sqlite3
import struct
from datetime import timedelta

import pytest

from levain.authority import sshsig_public_key
from levain.cockpit.consent import utc_stamp
from levain.consent import AnswerRefused, ConsentQueue, answer_message, answer_payload
from levain.consent.queue import (
    REFUSED_NOT_AUTHORIZED, REFUSED_PER_REQUEST, REFUSED_WINDOW_ELAPSED, STORE_FORMAT, UNDECIDED_GRACE,
)
from levain.consent.receipt_log import head as receipts_head
from levain.consent.receipt_log import verify as verify_receipts
from levain.consent.record import RULING_NAMESPACE, ruling_message
from tests.test_consent_queue import NOW, Clock, FakeAgent, Log, _raw, signed
from tests.test_consent_queue import authority, clock, keys, queue  # noqa: F401 - the shared fixtures
from tests.test_consent_queue_v4 import _v3_module, ruled, undecided


def _refused(q, request_id, payload, sig):
    with pytest.raises(AnswerRefused) as got:
        q.answer(request_id, payload, sig)
    return str(got.value)


def _late_refused(q, request_id, payload, sig):
    with pytest.raises(AnswerRefused) as got:
        q.answer_late(request_id, payload, sig)
    return str(got.value)


def _table_rows(q):
    conn = sqlite3.connect(q.path)
    try:
        return conn.execute("SELECT * FROM refused_answers ORDER BY seq").fetchall()
    finally:
        conn.close()


# --- what is kept, and why ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("name, decision, tier, why", [
    ("stranger", "allow", "T2", "the signer is not an enrolled key"),
    ("stranger", "deny", "T2", "the signer is not an enrolled key"),
    ("revoked", "allow", "T2", "the signer's key is revoked"),
    ("revoked", "deny", "T2", "the signer's key is revoked"),
    ("presence", "allow", "T2", "key class presence does not cover T2"),
    ("phone", "allow", "T3", "key class display-phone does not cover T3"),
])
def test_an_unauthorised_signer_is_kept_and_the_request_never_moves(queue, keys, name, decision, tier, why):
    request = FakeAgent(queue).ask(tier=tier)
    payload, sig = signed(queue, request, keys[name], decision)
    assert _refused(queue, request.id, payload, sig) == why          # the refusal is exactly as before
    assert queue.get(request.id)[1] == "pending"
    assert queue.decision(request.id) is None
    assert queue.receipts(request.id) == []                          # no transition, so no receipt
    [row] = queue.refused_answers(request.id)
    assert row["reason_code"] == REFUSED_NOT_AUTHORIZED
    assert (row["request_id"], row["detail"], row["decision"], row["signer_fp"], row["signed_at"]) == \
        (request.id, why, decision, keys[name].fp, payload["signed_at"])
    assert row["record"] == answer_message(payload).decode("utf-8")
    assert row["signature"] == sig
    assert row["refused_at"] == utc_stamp(NOW)


def test_an_authorised_answer_after_the_hold_is_kept_as_window_elapsed(queue, clock, keys):
    request = FakeAgent(queue).ask(hold=timedelta(seconds=30))
    payload, sig = signed(queue, request, keys["phone"])             # signed in time, delivered late
    clock.now = NOW + timedelta(seconds=30)
    why = f"request {request.id} is already expired: its hold ended {request.expires_at}"
    assert _refused(queue, request.id, payload, sig) == why
    assert queue.get(request.id)[1] == "pending"                     # still in its grace: the hook marks it
    [row] = queue.refused_answers(request.id)
    assert (row["reason_code"], row["detail"], row["signer_fp"]) == (REFUSED_WINDOW_ELAPSED, why, keys["phone"].fp)
    assert row["refused_at"] == utc_stamp(clock.now)


def test_an_authorised_answer_to_an_expired_request_is_kept(queue, clock, keys):
    request = FakeAgent(queue).ask(hold=timedelta(seconds=30))
    payload, sig = signed(queue, request, keys["laptop"], "deny")
    clock.now = NOW + timedelta(seconds=30) + UNDECIDED_GRACE
    assert _refused(queue, request.id, payload, sig) == f"request {request.id} is already expired"
    assert queue.get(request.id)[1] == "expired"
    assert [r["state"] for r in queue.receipts(request.id)] == ["expired"]   # the expiry's receipt only
    [row] = queue.refused_answers(request.id)
    assert (row["reason_code"], row["decision"]) == (REFUSED_WINDOW_ELAPSED, "deny")


def test_an_authorised_answer_to_an_undecided_request_is_kept(queue, keys):
    request, _gap = undecided(queue)
    payload, sig = signed(queue, request, keys["phone"])
    assert _refused(queue, request.id, payload, sig) == f"request {request.id} is already undecided"
    assert queue.get(request.id)[1] == "undecided"
    [row] = queue.refused_answers(request.id)
    assert row["reason_code"] == REFUSED_WINDOW_ELAPSED


def test_an_unauthorised_answer_after_the_hold_is_kept_as_not_authorised(queue, clock, keys):
    request = FakeAgent(queue).ask(hold=timedelta(seconds=30))
    payload, sig = signed(queue, request, keys["stranger"])
    clock.now = NOW + timedelta(seconds=30)
    _refused(queue, request.id, payload, sig)
    assert [r["reason_code"] for r in queue.refused_answers(request.id)] == [REFUSED_NOT_AUTHORIZED]


def test_an_authorised_answer_signed_after_the_hold_is_kept(queue, keys):
    request = FakeAgent(queue).ask()
    payload = answer_payload(queue_id=queue.queue_id, request=request, decision="allow", signer_fp=keys["phone"].fp,
                             signed_at=request.expires_at, receipts_head=receipts_head(queue))
    sig = keys["phone"].sign(answer_message(payload))
    assert _refused(queue, request.id, payload, sig) == "the answer was signed after the request expired"
    assert [r["reason_code"] for r in queue.refused_answers(request.id)] == [REFUSED_WINDOW_ELAPSED]
    assert queue.get(request.id)[1] == "pending"


def test_an_answer_whose_lock_wait_crosses_the_hold_is_kept(queue, clock, keys, monkeypatch):
    """The refusal decided inside the write transaction (the hold ended while the answer waited for the lock)."""
    import levain.consent.queue as qmod
    request = FakeAgent(queue).ask(hold=timedelta(seconds=30))
    payload, sig = signed(queue, request, keys["phone"])
    armed = {"on": False}
    real_verify, real_connect = qmod.verify_link_sig, queue._connect

    def verify(*a, **kw):
        ok = real_verify(*a, **kw)
        armed["on"] = True
        return ok

    def connect():
        if armed["on"]:
            clock.now = NOW + timedelta(seconds=31)
        return real_connect()

    monkeypatch.setattr(qmod, "verify_link_sig", verify)
    monkeypatch.setattr(queue, "_connect", connect)
    assert _refused(queue, request.id, payload, sig) == f"request {request.id} is already expired"
    assert queue.get(request.id)[1] == "pending"
    assert [r["reason_code"] for r in queue.refused_answers(request.id)] == [REFUSED_WINDOW_ELAPSED]


# --- what is never kept -----------------------------------------------------------------------------------------

def test_a_duplicate_answer_to_a_decided_or_withdrawn_request_is_not_kept(queue, keys):
    agent = FakeAgent(queue)
    decided, gone = agent.ask("a"), agent.ask("b")
    payload, sig = signed(queue, decided, keys["phone"])
    queue.answer(decided.id, payload, sig)
    queue.withdraw(gone.id)
    for request, state in ((decided, "allowed"), (gone, "withdrawn")):
        for name in ("stranger", "laptop"):
            p, s = signed(queue, request, keys[name], "deny")
            assert _refused(queue, request.id, p, s) == f"request {request.id} is already {state}"
    assert _table_rows(queue) == []


def test_a_stale_receipts_head_is_not_kept(queue, keys):
    agent = FakeAgent(queue)
    request, other = agent.ask("a"), agent.ask("b")
    p2, s2 = signed(queue, other, keys["phone"])
    queue.answer(other.id, p2, s2)
    # a head of the right size over other receipts: what a signer reads before the log is rewritten under it
    payload, sig = signed(queue, request, keys["phone"], receipts_head={"size": 1, "root": "0" * 64})
    assert _refused(queue, request.id, payload, sig) == "the receipts changed under this answer"
    assert queue.refused_answers(request.id) == []
    assert queue.get(request.id)[1] == "pending"


def test_an_answer_refused_while_the_log_cannot_be_replayed_is_not_kept(tmp_path, keys, clock):
    (tmp_path / "auth").mkdir()
    log = Log(tmp_path / "auth", keys)
    log.enrol("laptop", "laptop-presence")
    q = ConsentQueue(tmp_path / "consent", authority=log.source, clock=clock)
    request = FakeAgent(q).ask()
    payload, sig = signed(q, request, keys["stranger"])
    log.path.rename(tmp_path / "auth" / "moved.db")
    assert _refused(q, request.id, payload, sig) == \
        "the authority log cannot be replayed now: no answer is authority"
    clock.now = NOW + timedelta(minutes=6)                           # the late path replays too: still nothing
    _refused(q, request.id, payload, sig)
    assert q.refused_answers(request.id) == []


def test_a_bad_signature_is_not_kept(queue, keys):
    request = FakeAgent(queue).ask()
    # an enrolled key named, another key's signature
    payload, _ = signed(queue, request, keys["revoked"])
    assert _refused(queue, request.id, payload, keys["stranger"].sign(answer_message(payload))) == \
        "the signer's key is revoked"
    # an unenrolled key named, another unenrolled key's signature: the embedded key is not the one named
    payload, _ = signed(queue, request, keys["stranger"])
    _refused(queue, request.id, payload, keys["presence"].sign(answer_message(payload)))
    # the right key, the wrong namespace
    _refused(queue, request.id, payload, keys["stranger"].sign(answer_message(payload), RULING_NAMESPACE))
    # garbage
    for sig in ("", "yes", None, "-----BEGIN SSH SIGNATURE-----\nAAAA\n-----END SSH SIGNATURE-----"):
        _refused(queue, request.id, payload, sig)
    assert _table_rows(queue) == []


def test_a_malformed_record_is_not_kept(queue, keys):
    agent = FakeAgent(queue)
    request, other = agent.ask("a"), agent.ask("b")
    payload, sig = signed(queue, request, keys["stranger"])
    for bad in ({**payload, "also": "x"}, {k: v for k, v in payload.items() if k != "expires_at"},
                {**payload, "request_digest": "0" * 64}, {**payload, "queue_id": "cq-" + "1" * 32},
                {**payload, "decision": "maybe"}, {**payload, "signed_at": "yesterday"},
                {**payload, "receipts_head": {"size": -1, "root": "x"}}, "not a mapping"):
        try:                                                          # its own valid signature where it has one
            s = keys["stranger"].sign(answer_message(bad))
        except Exception:
            s = sig
        _refused(queue, request.id, bad, s)
    _refused(queue, other.id, payload, sig)                           # another request's answer
    assert _table_rows(queue) == []


def test_an_unenrolled_passkey_assertion_is_not_kept(queue, keys, monkeypatch):
    """A WebAuthn signature by a key the log does not hold is never evidence, even one that would verify."""
    request = FakeAgent(queue).ask()
    payload, sig = signed(queue, request, keys["stranger"])

    def u32(n):
        return struct.pack(">I", n)

    def string(b):
        return u32(len(b)) + b

    text = sig.strip()
    blob = base64.b64decode("".join(text.splitlines()[1:-1]))
    pos = 10
    fields = []
    for _ in range(5):
        n = struct.unpack(">I", blob[pos:pos + 4])[0]
        fields.append(blob[pos + 4:pos + 4 + n])
        pos += 4 + n
    inner = string(b"webauthn-sk-ecdsa-sha2-nistp256@openssh.com") + string(b"x" * 64) + bytes([5]) + u32(1) \
        + string(b"https://example.test") + string(b"{}") + string(b"")
    forged = blob[:10] + b"".join(string(f) for f in fields[:4]) + string(inner)
    armored = "-----BEGIN SSH SIGNATURE-----\n" + base64.b64encode(forged).decode() + "\n-----END SSH SIGNATURE-----"
    assert sshsig_public_key(armored)[1] == keys["stranger"].fp     # it names the very key the record names
    monkeypatch.setattr(ConsentQueue, "_signature_refusal", lambda self, *a, **kw: None)   # as if it verified
    _refused(queue, request.id, payload, armored)
    assert queue.refused_answers(request.id) == []
    _refused(queue, request.id, payload, sig)                         # the plain SSH signature still is kept
    assert len(queue.refused_answers(request.id)) == 1


# --- bound, dedupe, failure ---------------------------------------------------------------------------------------

def test_at_most_the_bound_is_kept_per_request_first_ones_first(queue, clock, keys):
    agent = FakeAgent(queue)
    request, other = agent.ask("a", hold=timedelta(hours=1)), agent.ask("b", hold=timedelta(hours=1))
    signed_ats = []
    for i in range(REFUSED_PER_REQUEST + 3):
        clock.now = NOW + timedelta(seconds=i)                        # a new signed_at: a new signature
        payload, sig = signed(queue, request, keys["stranger"])
        signed_ats.append(payload["signed_at"])
        assert _refused(queue, request.id, payload, sig) == "the signer is not an enrolled key"
    kept = queue.refused_answers(request.id)
    assert [r["signed_at"] for r in kept] == signed_ats[:REFUSED_PER_REQUEST]
    assert [r["seq"] for r in kept] == sorted(r["seq"] for r in kept)
    payload, sig = signed(queue, other, keys["stranger"])           # the bound is per request
    _refused(queue, other.id, payload, sig)
    assert len(queue.refused_answers(other.id)) == 1


def test_the_same_signature_is_kept_once(queue, keys):
    request = FakeAgent(queue).ask()
    payload, sig = signed(queue, request, keys["stranger"])
    for _ in range(3):
        assert _refused(queue, request.id, payload, sig) == "the signer is not an enrolled key"
    assert len(queue.refused_answers(request.id)) == 1


def test_a_failure_to_keep_never_changes_the_refusal(queue, keys, monkeypatch):
    request = FakeAgent(queue).ask()
    payload, sig = signed(queue, request, keys["stranger"])

    def boom(self, *a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ConsentQueue, "_record_refused", boom)
    assert _refused(queue, request.id, payload, sig) == "the signer is not an enrolled key"
    monkeypatch.undo()
    _raw(queue, ("DROP TABLE refused_answers", ()))                   # a store damaged under a ready queue
    assert _refused(queue, request.id, payload, sig) == "the signer is not an enrolled key"
    assert queue.get(request.id)[1] == "pending"


def test_kept_refusals_are_append_only(queue, keys):
    request = FakeAgent(queue).ask()
    payload, sig = signed(queue, request, keys["stranger"])
    _refused(queue, request.id, payload, sig)
    for sql in ("UPDATE refused_answers SET reason_code = 'approval-window-elapsed'",
                "DELETE FROM refused_answers"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            _raw(queue, (sql, ()))
    with pytest.raises(sqlite3.IntegrityError):
        _raw(queue, ("INSERT INTO refused_answers (request_id, refused_at, reason_code, detail, decision, signer_fp, "
                     "signed_at, record, signature) VALUES (?, 'x', 'bogus', '', '', '', '', '', 'y')", (request.id,)))


# --- late rulings -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name, decision, why", [
    ("stranger", "allow", "the signer is not an enrolled key"),
    ("revoked", "deny", "the signer's key is revoked"),
    ("presence", "allow", "key class presence does not cover T2"),
])
def test_an_unauthorised_late_ruling_is_kept_and_the_request_stays_undecided(queue, keys, name, decision, why):
    request, gap = undecided(queue)
    payload, sig = ruled(queue, request.id, keys[name], decision)
    assert _late_refused(queue, request.id, payload, sig) == why
    assert queue.get(request.id)[1] == "undecided"
    assert [r["state"] for r in queue.receipts(request.id)] == ["undecided"]
    [row] = queue.refused_answers(request.id)
    assert (row["reason_code"], row["decision"], row["signer_fp"]) == (REFUSED_NOT_AUTHORIZED, decision, keys[name].fp)
    assert row["record"] == ruling_message(payload).decode("utf-8")
    assert json.loads(row["record"])["gap"] == gap


def test_a_late_ruling_that_is_no_evidence_or_authorised_is_not_kept(queue, clock, keys):
    request, gap = undecided(queue)
    payload, sig = ruled(queue, request.id, keys["stranger"])
    _late_refused(queue, request.id, {**payload, "gap": gap + 1}, sig)                      # another gap
    _late_refused(queue, request.id, payload, keys["stranger"].sign(ruling_message(payload)))  # answer namespace
    _late_refused(queue, request.id, payload, keys["presence"].sign(ruling_message(payload), RULING_NAMESPACE))
    early, esig = ruled(queue, request.id, keys["phone"], signed_at=utc_stamp(NOW + timedelta(minutes=10)))
    assert "later than the queue's clock" in _late_refused(queue, request.id, early, esig)  # authorised, refused
    size = receipts_head(queue)["size"]
    stale, ssig = ruled(queue, request.id, keys["phone"], receipts_head={"size": size, "root": "0" * 64})
    assert _late_refused(queue, request.id, stale, ssig) == "the receipts changed under this ruling"
    good, gsig = ruled(queue, request.id, keys["phone"])
    queue.answer_late(request.id, good, gsig)
    bad, bsig = payload, sig
    _late_refused(queue, request.id, bad, bsig)                         # decided: a stranger's duplicate
    assert _table_rows(queue) == []


# --- the receipts log never sees them (Q11) -----------------------------------------------------------------------

def test_kept_refusals_leave_the_receipts_log_byte_for_byte_unchanged(queue, clock, keys, authority):
    agent = FakeAgent(queue)
    decided, open_, late = agent.ask("a"), agent.ask("b"), agent.ask("c", hold=timedelta(seconds=30))
    payload, sig = signed(queue, decided, keys["phone"])
    queue.answer(decided.id, payload, sig)
    queue.withdraw(agent.ask("d").id)

    def snapshot():
        report = verify_receipts(queue.directory, authority_for=lambda request: authority)
        return json.dumps(receipts_head(queue), sort_keys=True), repr(report), report.ok

    before = snapshot()
    assert before[2]
    for name in ("stranger", "revoked", "presence"):
        p, s = signed(queue, open_, keys[name])
        _refused(queue, open_.id, p, s)
    lp, ls = signed(queue, late, keys["phone"])
    clock.now = NOW + timedelta(seconds=30)
    _refused(queue, late.id, lp, ls)
    assert len(_table_rows(queue)) == 4
    assert snapshot() == before
    # and the head a signer reads now is still applied: a kept refusal never makes an answer stale
    clock.now = NOW + timedelta(seconds=5)
    payload, sig = signed(queue, open_, keys["phone"])
    p, s = signed(queue, open_, keys["stranger"], "deny")
    _refused(queue, open_.id, p, s)                                    # lands between the read and the apply
    assert queue.answer(open_.id, payload, sig).decision == "allow"


# --- the store ----------------------------------------------------------------------------------------------------

def test_a_current_store_made_before_the_table_gains_it_on_open(tmp_path, keys, clock, authority):
    d = tmp_path / "consent"
    q = ConsentQueue(d, authority=authority, clock=clock)
    request = FakeAgent(q).ask()
    payload, sig = signed(q, request, keys["phone"])
    q.answer(request.id, payload, sig)
    _raw(q, ("DROP TABLE refused_answers", ()))                        # as a store from before this slice
    fresh = ConsentQueue(d, authority=authority, clock=clock)
    assert fresh.decision(request.id).decision == "allow"
    other = FakeAgent(fresh).ask("b")
    p, s = signed(fresh, other, keys["stranger"])
    _refused(fresh, other.id, p, s)
    assert len(fresh.refused_answers(other.id)) == 1
    conn = sqlite3.connect(fresh.path)
    try:
        triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' "
                                               "AND tbl_name = 'refused_answers'")}
        assert conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()[0] == STORE_FORMAT
    finally:
        conn.close()
    assert triggers == {"refused_answers_frozen", "refused_answers_kept"}


def test_a_v3_store_migrates_and_gains_the_table(tmp_path, keys):
    v3 = _v3_module()
    (tmp_path / "auth").mkdir()
    log = Log(tmp_path / "auth", keys)
    log.enrol("laptop", "laptop-presence")
    log.enrol("phone", "display-phone")
    clock = Clock(NOW)
    d = tmp_path / "consent"
    from levain.authority import ReplayCache
    old = v3.ConsentQueue(d, authority=v3.authority_source(log.path, anchor=None, cache=ReplayCache()), clock=clock)
    request = FakeAgent(old).ask()
    new = ConsentQueue(d, authority=log.source, clock=clock)
    payload, sig = signed(new, request, keys["stranger"])
    assert _refused(new, request.id, payload, sig) == "the signer is not an enrolled key"
    assert [r["reason_code"] for r in new.refused_answers(request.id)] == [REFUSED_NOT_AUTHORIZED]
    assert new.get(request.id)[1] == "pending"


# --- the SSHSIG reader --------------------------------------------------------------------------------------------

def test_sshsig_public_key_reads_the_signer_s_key(keys):
    k = keys["stranger"]
    public_key, fingerprint, sig_type = sshsig_public_key(k.sign(b"hello"))
    assert (public_key, fingerprint, sig_type) == (k.public_key, k.fp, "ssh-ed25519")


@pytest.mark.parametrize("bad", [None, "", "yes", "-----BEGIN SSH SIGNATURE-----\n!!\n-----END SSH SIGNATURE-----",
                                 "-----BEGIN SSH SIGNATURE-----\nAAAA\n-----END SSH SIGNATURE-----", "x" * 20000])
def test_sshsig_public_key_refuses_what_is_not_a_signature(bad):
    with pytest.raises(ValueError):
        sshsig_public_key(bad)
