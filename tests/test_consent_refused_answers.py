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
import time
import warnings
from datetime import timedelta
from pathlib import Path

import pytest

from levain.authority import sshsig_public_key
from levain.cockpit.consent import utc_stamp
from levain.consent import AnswerRefused, ConsentQueue, answer_message, answer_payload
from levain.consent.queue import (
    REFUSED_BUSY_MS, REFUSED_NOT_AUTHORIZED, REFUSED_PER_REQUEST, REFUSED_UNENROLLED_PER_REQUEST,
    REFUSED_WINDOW_ELAPSED, STORE_FORMAT, UNDECIDED_GRACE,
)
from levain.consent.receipt_log import head as receipts_head
from levain.consent.receipt_log import verify as verify_receipts
from levain.consent.record import RULING_NAMESPACE, ruling_message
from tests.test_consent_queue import NOW, Clock, FakeAgent, Key, Log, _raw, signed
from tests.test_consent_queue import authority, clock, keys, queue  # noqa: F401 - the shared fixtures
from tests.test_consent_queue_v4 import _v3_module, ruled, undecided
from tests._h4 import h4_home


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

@pytest.mark.parametrize("name, decision, tier, why, subreason, enrolled", [
    ("stranger", "allow", "T2", "the signer is not an enrolled key", "not-enrolled", False),
    ("stranger", "deny", "T2", "the signer is not an enrolled key", "not-enrolled", False),
    ("revoked", "allow", "T2", "the signer's key is revoked", "revoked", True),
    ("revoked", "deny", "T2", "the signer's key is revoked", "revoked", True),
    ("presence", "allow", "T2", "key class presence does not cover T2", "class", True),
    ("phone", "allow", "T3", "key class display-phone does not cover T3", "class", True),
])
def test_an_unauthorised_signer_is_kept_and_the_request_never_moves(queue, keys, name, decision, tier, why,
                                                                    subreason, enrolled):
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
    assert (row["kind"], row["subreason"], row["enrolled"]) == ("answer", subreason, enrolled)


def test_an_authorised_answer_after_the_hold_is_kept_as_window_elapsed(queue, clock, keys):
    request = FakeAgent(queue).ask(hold=timedelta(seconds=30))
    payload, sig = signed(queue, request, keys["phone"])             # signed in time, delivered late
    clock.now = NOW + timedelta(seconds=30)
    why = f"request {request.id} is already expired: its hold ended {request.expires_at}"
    assert _refused(queue, request.id, payload, sig) == why
    assert queue.get(request.id)[1] == "pending"                     # still in its grace: the hook marks it
    [row] = queue.refused_answers(request.id)
    assert (row["reason_code"], row["detail"], row["signer_fp"]) == (REFUSED_WINDOW_ELAPSED, why, keys["phone"].fp)
    assert (row["kind"], row["subreason"], row["enrolled"]) == ("answer", "late", True)
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


@pytest.mark.parametrize("name, why, subreason", [
    ("stranger", "the signer is not an enrolled key", "not-enrolled"),
    ("revoked", "the signer's key is revoked", "revoked"),
])
def test_an_unauthorised_answer_after_the_hold_keeps_the_key_s_refusal(queue, clock, keys, name, why, subreason):
    """W1: the caller is told the request expired; the row keeps the verdict on the KEY, never that text."""
    agent = FakeAgent(queue)
    request, other = agent.ask("a", hold=timedelta(seconds=30)), agent.ask("b", hold=timedelta(seconds=30))
    payload, sig = signed(queue, request, keys[name], "deny")
    p, s = signed(queue, other, keys[name], "deny")
    clock.now = NOW + timedelta(seconds=30)
    told = _refused(queue, request.id, payload, sig)
    assert told == f"request {request.id} is already expired: its hold ended {request.expires_at}"
    [row] = queue.refused_answers(request.id)
    assert (row["reason_code"], row["subreason"], row["detail"]) == (REFUSED_NOT_AUTHORIZED, subreason, why)
    clock.now = NOW + timedelta(seconds=30) + UNDECIDED_GRACE           # and once expired: the same verdict kept
    assert _refused(queue, other.id, p, s) == f"request {other.id} is already expired"
    assert [(r["subreason"], r["detail"]) for r in queue.refused_answers(other.id)] == [(subreason, why)]


def test_a_signed_at_after_the_hold_alone_is_never_lateness(queue, keys):
    """W8: ``signed_at`` is the signer's claim. An authorised answer refused only for it is kept nowhere; an
    unauthorised one is kept for its key, never as late."""
    request = FakeAgent(queue).ask()
    for name, kept in (("phone", []), ("stranger", [(REFUSED_NOT_AUTHORIZED, "not-enrolled")])):
        payload = answer_payload(queue_id=queue.queue_id, request=request, decision="allow", signer_fp=keys[name].fp,
                                 signed_at=request.expires_at, receipts_head=receipts_head(queue))
        sig = keys[name].sign(answer_message(payload))
        assert _refused(queue, request.id, payload, sig) == "the answer was signed after the request expired"
        assert [(r["reason_code"], r["subreason"]) for r in queue.refused_answers(request.id)] == kept
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
    q = ConsentQueue(tmp_path / "consent", authority=log.source, clock=clock, home=h4_home())
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


# --- bound, dedupe, failure ---------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def strangers(tmp_path_factory):
    """Keys no authority log holds, more than the bound."""
    d = tmp_path_factory.mktemp("consent-strangers")
    return [Key(d, f"s{i}") for i in range(REFUSED_PER_REQUEST + 3)]


def test_at_most_the_bound_is_kept_per_request_first_ones_first(queue, clock, keys, strangers):
    agent = FakeAgent(queue)
    request, other = agent.ask("a", hold=timedelta(hours=1)), agent.ask("b", hold=timedelta(hours=1))
    for i, k in enumerate(strangers):
        clock.now = NOW + timedelta(seconds=i)
        payload, sig = signed(queue, request, k)
        assert _refused(queue, request.id, payload, sig) == "the signer is not an enrolled key"
    kept = queue.refused_answers(request.id)
    assert [r["signer_fp"] for r in kept] == [k.fp for k in strangers[:REFUSED_UNENROLLED_PER_REQUEST]]
    assert [r["seq"] for r in kept] == sorted(r["seq"] for r in kept)
    payload, sig = signed(queue, other, keys["stranger"])           # the bound is per request
    _refused(queue, other.id, payload, sig)
    assert len(queue.refused_answers(other.id)) == 1


def test_a_full_request_is_counted_before_any_verify(queue, clock, strangers, monkeypatch):
    """W4: once the bound is reached, a refusal does no replay and no verify to learn it keeps nothing."""
    import levain.consent.queue as qmod
    request = FakeAgent(queue).ask(hold=timedelta(hours=1))
    for k in strangers[:REFUSED_UNENROLLED_PER_REQUEST]:
        payload, sig = signed(queue, request, k)
        _refused(queue, request.id, payload, sig)
    calls = []
    real = qmod.verify_link_sig
    monkeypatch.setattr(qmod, "verify_link_sig", lambda *a, **kw: calls.append(1) or real(*a, **kw))
    payload, sig = signed(queue, request, strangers[REFUSED_UNENROLLED_PER_REQUEST])
    assert _refused(queue, request.id, payload, sig) == "the signer is not an enrolled key"
    assert calls == []
    assert len(queue.refused_answers(request.id)) == REFUSED_UNENROLLED_PER_REQUEST


def test_one_key_and_decision_is_kept_once_whatever_its_signatures(queue, clock, keys):
    """W5: a signature need not be deterministic (ECDSA, a passkey), so one key re-signing must not fill the bound:
    a (request, signer, decision) is kept once; the other decision is its own row."""
    request = FakeAgent(queue).ask(hold=timedelta(hours=1))
    for i in range(REFUSED_PER_REQUEST + 2):
        clock.now = NOW + timedelta(seconds=i)                        # a new signed_at: a new signature
        payload, sig = signed(queue, request, keys["stranger"])
        assert _refused(queue, request.id, payload, sig) == "the signer is not an enrolled key"
    payload, sig = signed(queue, request, keys["stranger"], "deny")
    _refused(queue, request.id, payload, sig)
    kept = queue.refused_answers(request.id)
    assert [(r["decision"], r["signed_at"]) for r in kept] == [("allow", utc_stamp(NOW)),
                                                               ("deny", utc_stamp(clock.now))]


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
    q = ConsentQueue(d, authority=authority, clock=clock, home=h4_home())
    request = FakeAgent(q).ask()
    payload, sig = signed(q, request, keys["phone"])
    q.answer(request.id, payload, sig)
    _raw(q, ("DROP TABLE refused_answers", ()))                        # as a store from before this slice
    fresh = ConsentQueue(d, authority=authority, clock=clock, home=h4_home())
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
    new = ConsentQueue(d, authority=log.source, clock=clock, home=h4_home())
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


# --- r1 (refused_FIX_r1): the key's standing, kind, lock, golden texts, processes ---------------------------------

def test_a_key_revoked_as_compromised_is_kept_as_compromised(tmp_path, keys, clock):
    """W2: a stolen key's attempt is real and kept, named ``compromised``, enrolled."""
    (tmp_path / "auth").mkdir()
    log = Log(tmp_path / "auth", keys)
    log.enrol("laptop", "laptop-presence")
    log.enrol("phone", "display-phone")
    since = log.source().key(keys["phone"].fp).enrolled_seq
    log.link({"op": "revoke_key", "fingerprint": keys["phone"].fp, "compromised_since": since})
    q = ConsentQueue(tmp_path / "consent", authority=log.source, clock=clock, home=h4_home())
    request = FakeAgent(q).ask()
    payload, sig = signed(q, request, keys["phone"])
    assert _refused(q, request.id, payload, sig) == "the signer's key is revoked"
    [row] = q.refused_answers(request.id)
    assert (row["reason_code"], row["subreason"], row["enrolled"], row["detail"]) == \
        (REFUSED_NOT_AUTHORIZED, "compromised", True, "the signer's key is revoked")


def test_the_kind_names_an_answer_or_a_ruling(queue, keys):
    """W7."""
    request, _gap = undecided(queue)
    payload, sig = ruled(queue, request.id, keys["stranger"], "deny")
    _late_refused(queue, request.id, payload, sig)
    other = FakeAgent(queue).ask("b")
    p, s = signed(queue, other, keys["stranger"], "deny")
    _refused(queue, other.id, p, s)
    assert [(r["kind"], r["subreason"], r["enrolled"]) for r in queue.refused_answers(request.id)] == \
        [("ruling", "not-enrolled", False)]
    assert [r["kind"] for r in queue.refused_answers(other.id)] == ["answer"]


def test_the_store_refuses_a_row_whose_reason_and_subreason_disagree(queue, keys):
    request = FakeAgent(queue).ask()
    for kind, reason, sub, enrolled in (("answer", "approval-window-elapsed", "class", 1),
                                        ("answer", "approver-not-authorized", "late", 1),
                                        ("answer", "approver-not-authorized", "bogus", 1),
                                        ("other", "approver-not-authorized", "class", 1),
                                        ("answer", "approver-not-authorized", "class", 2)):
        with pytest.raises(sqlite3.IntegrityError):
            _raw(queue, ("INSERT INTO refused_answers (request_id, kind, refused_at, reason_code, subreason, enrolled, "
                         "detail, decision, signer_fp, signed_at, record, signature) "
                         "VALUES (?, ?, 'x', ?, ?, ?, '', 'allow', 'f', 's', 'r', 'y')",
                         (request.id, kind, reason, sub, enrolled)))


def _armored(blob):
    return "-----BEGIN SSH SIGNATURE-----\n" + base64.b64encode(blob).decode() + "\n-----END SSH SIGNATURE-----"


def _sshsig_fields(sig):
    blob = base64.b64decode("".join(sig.strip().splitlines()[1:-1]))
    pos, fields = 10, []
    for _ in range(5):
        n = struct.unpack(">I", blob[pos:pos + 4])[0]
        fields.append(blob[pos + 4:pos + 4 + n])
        pos += 4 + n
    return blob[:10], fields


def _string(b):
    return struct.pack(">I", len(b)) + b


@pytest.mark.parametrize("key_type", [b"ssh-ed25519\nno-touch-required", b"ssh-ed25519 cert-authority",
                                      b'ssh-ed25519"', b"ssh-ed25519\r", b"ssh-ed\x00"])
def test_sshsig_public_key_refuses_a_key_type_that_would_change_the_signers_line(keys, key_type):
    """W3: the key type is written into ssh-keygen's allowed-signers line; a newline would add a second line."""
    head, fields = _sshsig_fields(keys["stranger"].sign(b"hello"))
    key_blob = fields[0]
    n = struct.unpack(">I", key_blob[:4])[0]
    crafted_key = _string(key_type) + key_blob[4 + n:]
    crafted = head + _string(crafted_key) + b"".join(_string(f) for f in fields[1:])
    with pytest.raises(ValueError, match="key type"):
        sshsig_public_key(_armored(crafted))
    ok_key = _string(b"sk-ssh-ed25519@openssh.com") + key_blob[4 + n:]  # the allowed alphabet still reads
    assert sshsig_public_key(_armored(head + _string(ok_key) + b"".join(_string(f) for f in fields[1:])))[0] \
        .startswith("sk-ssh-ed25519@openssh.com ")


_PASSKEY = json.loads((Path(__file__).parent / "fixtures" / "passkey_assertion_virtual.json").read_text("utf-8"))


def test_an_unenrolled_passkey_assertion_is_not_kept(queue, keys, monkeypatch):
    """A real WebAuthn SSHSIG (the recorded assertion) by a passkey the log does not hold is never evidence, even
    with ssh-keygen's verdict forced to yes; the same forced yes keeps an unenrolled SSH key's answer."""
    import levain.consent.queue as qmod
    from levain.authority import _WEBAUTHN_SIG_TYPE
    from levain.cockpit.keys import fingerprint_of
    from levain.consent.passkey import public_key_line, to_sshsig
    spki = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200") + bytes.fromhex(
        _PASSKEY["pub_point_hex"])
    key = public_key_line(spki, _PASSKEY["rp_id"])
    sig = to_sshsig(key, bytes.fromhex(_PASSKEY["authData_hex"]), _PASSKEY["clientDataJSON"].encode(),
                    bytes.fromhex(_PASSKEY["sig_der_hex"]), namespace=_PASSKEY["namespace"])
    fp = fingerprint_of(key)
    assert sshsig_public_key(sig)[1:] == (fp, _WEBAUTHN_SIG_TYPE)    # it names the very key the record names
    request = FakeAgent(queue).ask()
    payload = answer_payload(queue_id=queue.queue_id, request=request, decision="allow", signer_fp=fp,
                             signed_at=utc_stamp(NOW), receipts_head=receipts_head(queue))
    monkeypatch.setattr(qmod, "verify_link_sig", lambda *a, **kw: True)   # as if it verified over this record
    assert _refused(queue, request.id, payload, sig) == "the signer is not an enrolled key"
    assert queue.refused_answers(request.id) == []
    p, s = signed(queue, request, keys["stranger"])
    _refused(queue, request.id, p, s)
    assert [r["signer_fp"] for r in queue.refused_answers(request.id)] == [keys["stranger"].fp]


def test_a_locked_store_never_holds_the_refusal_or_changes_it(queue, keys):
    """W4/W6/W10, X6: another writer holds the lock; keeping gives up well before the 30 s of every other write
    (it waits REFUSED_BUSY_MS; only a generous upper bound is asserted), the real OperationalError is swallowed with no warning, the refusal is the same text, and
    nothing is left held."""
    request = FakeAgent(queue).ask()
    payload, sig = signed(queue, request, keys["stranger"])
    locker = sqlite3.connect(queue.path, isolation_level=None, timeout=0)
    try:
        locker.execute("BEGIN IMMEDIATE")
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            t0 = time.monotonic()
            assert _refused(queue, request.id, payload, sig) == "the signer is not an enrolled key"
            took = time.monotonic() - t0
        assert [w for w in seen if issubclass(w.category, RuntimeWarning)] == []
        assert took < 20                     # generous: only that it is not the 30 s every other write waits
        locker.execute("ROLLBACK")
    finally:
        locker.close()
    assert queue.refused_answers(request.id) == []
    probe = sqlite3.connect(queue.path, isolation_level=None, timeout=0)   # nothing of the refusal holds a lock
    try:
        probe.execute("BEGIN IMMEDIATE")
        probe.execute("ROLLBACK")
    finally:
        probe.close()
    assert _refused(queue, request.id, payload, sig) == "the signer is not an enrolled key"
    assert len(queue.refused_answers(request.id)) == 1                 # unlocked, it is kept


def test_a_bug_in_keeping_is_a_warning_never_a_changed_refusal(queue, keys, monkeypatch):
    """W6: only store, disk, verifier and record failures are swallowed silently; anything else is a bug, shown as
    a RuntimeWarning, and the signer still gets the same refusal."""
    request = FakeAgent(queue).ask()
    payload, sig = signed(queue, request, keys["stranger"])

    def bug(self, *a, **kw):
        raise KeyError("subreason")

    monkeypatch.setattr(ConsentQueue, "_refused_reason", bug)
    with pytest.warns(RuntimeWarning, match="was not kept: KeyError"):
        assert _refused(queue, request.id, payload, sig) == "the signer is not an enrolled key"
    assert queue.get(request.id)[1] == "pending"


def test_the_refusal_texts_are_those_of_f06ed8ff(queue, clock, keys, authority):
    """W10: every path's refusal, copied from the code before refused answers were kept (f06ed8ff)."""
    from levain.consent.queue import RULING_SKEW
    agent = FakeAgent(queue)
    request, other = agent.ask("a"), agent.ask("b")
    payload, sig = signed(queue, request, keys["phone"])
    assert _refused(queue, "cr-nope", payload, sig) == "no sealed request 'cr-nope'"
    assert _refused(queue, request.id, payload, "") == "the answer carries no signature"
    assert _refused(queue, request.id, payload, keys["stranger"].sign(answer_message(payload))) == \
        "the answer's signature does not verify"
    assert _refused(queue, other.id, payload, sig) == "the answer is for another request"
    p = {**payload, "queue_id": "cq-" + "1" * 32}
    assert _refused(queue, request.id, p, keys["phone"].sign(answer_message(p))) == "the answer names another queue"
    p = {**payload, "decision": "maybe"}
    assert _refused(queue, request.id, p, keys["phone"].sign(answer_message(p))) == \
        "decision 'maybe' is not one of ('allow', 'deny')"
    assert _late_refused(queue, request.id, payload, sig) == \
        f"request {request.id} is pending, not undecided: a ruling answers only an undecided request"
    late, gap = undecided(queue, "c")
    rp, rs = ruled(queue, late.id, keys["phone"])
    assert _late_refused(queue, late.id, rp, keys["stranger"].sign(ruling_message(rp), RULING_NAMESPACE)) == \
        "the ruling's signature does not verify"
    assert _late_refused(queue, late.id, {**rp, "gap": gap + 1}, rs) == \
        f"the ruling names gap {gap + 1!r}, not this request's gap #{gap}"
    early, es = ruled(queue, late.id, keys["phone"], signed_at=utc_stamp(NOW + timedelta(minutes=10)))
    assert _late_refused(queue, late.id, early, es) == \
        (f"the ruling's signed_at {early['signed_at']} is later than the queue's clock {utc_stamp(NOW)} plus "
         f"{int(RULING_SKEW.total_seconds())} s")
    queue.answer_late(late.id, rp, rs)
    assert _late_refused(queue, late.id, rp, rs) == \
        f"request {late.id} is allowed, not undecided: a ruling answers only an undecided request"


def _refuse_in_a_process(qdir, cockpit_db, request_id, attempts, out):
    from levain.consent.queue import AnswerRefused as Refused, ConsentQueue as Q, authority_source as src
    q = Q(qdir, authority=src(cockpit_db, anchor=None), clock=lambda: NOW + timedelta(minutes=1), home=h4_home())
    told = []
    for payload, sig in attempts:
        try:
            q.answer(request_id, payload, sig)
        except Refused as exc:
            told.append(str(exc))
    out.put(told)


def test_the_bound_and_the_dedupe_hold_across_processes(tmp_path, keys, clock, strangers):
    """W10: three processes refusing the same request at once, each with its own signatures by the same unenrolled
    keys: exactly the bound is kept, one row per (signer, decision), and every caller got the same refusal."""
    import multiprocessing
    (tmp_path / "auth").mkdir()
    log = Log(tmp_path / "auth", keys)
    log.enrol("laptop", "laptop-presence")
    d = tmp_path / "consent"
    q = ConsentQueue(d, authority=log.source, clock=clock, home=h4_home())
    request = FakeAgent(q).ask(hold=timedelta(hours=1))
    batches = []
    for proc in range(3):
        clock.now = NOW + timedelta(seconds=proc)                    # each process its own signatures
        batches.append([signed(q, request, k) for k in strangers])
    ctx = multiprocessing.get_context("spawn")
    out = ctx.Queue()
    procs = [ctx.Process(target=_refuse_in_a_process, args=(d, log.path, request.id, b, out)) for b in batches]
    for p in procs:
        p.start()
    got = [out.get(timeout=300) for _ in procs]
    for p in procs:
        p.join(timeout=120)
    assert all(t == ["the signer is not an enrolled key"] * len(strangers) for t in got)
    kept = q.refused_answers(request.id)
    assert len(kept) == REFUSED_UNENROLLED_PER_REQUEST
    assert len({(r["signer_fp"], r["decision"]) for r in kept}) == REFUSED_UNENROLLED_PER_REQUEST
    assert {r["signer_fp"] for r in kept} <= {k.fp for k in strangers}
    assert q.get(request.id)[1] == "pending"


# --- r2 (refused_FIX_r2): head, caps by class, repeats, warnings as errors, the insert ---------------------------

def test_a_wrong_receipts_head_is_never_kept_on_any_path(queue, clock, keys):
    """X1: a well-formed head that is not the log's head of its size (another root, or more receipts than the queue
    holds) is no evidence: not from an unauthorised signer, not from an authorised one too late. The right head is."""
    agent = FakeAgent(queue)
    first, request, late = agent.ask("a"), agent.ask("b"), agent.ask("c", hold=timedelta(seconds=30))
    p0, s0 = signed(queue, first, keys["phone"])
    queue.answer(first.id, p0, s0)                                    # one receipt: size 1 has one true root
    size = receipts_head(queue)["size"]
    wrong = ({"size": size, "root": "0" * 64}, {"size": size + 5, "root": "0" * 64})
    for head in wrong:
        payload, sig = signed(queue, request, keys["stranger"], receipts_head=head)
        assert _refused(queue, request.id, payload, sig) == "the signer is not an enrolled key"
    lp = [signed(queue, late, keys["phone"], receipts_head=head) for head in wrong]
    clock.now = NOW + timedelta(seconds=30)
    for payload, sig in lp:
        assert _refused(queue, late.id, payload, sig).startswith(f"request {late.id} is already expired")
    assert _table_rows(queue) == []
    clock.now = NOW
    payload, sig = signed(queue, request, keys["stranger"])           # the right head: kept
    _refused(queue, request.id, payload, sig)
    good, gsig = signed(queue, late, keys["phone"])
    clock.now = NOW + timedelta(seconds=30)
    _refused(queue, late.id, good, gsig)
    assert [r["subreason"] for r in queue.refused_answers(request.id)] == ["not-enrolled"]
    assert [r["subreason"] for r in queue.refused_answers(late.id)] == ["late"]


def test_a_wrong_head_on_a_late_ruling_is_not_kept(queue, keys):
    """X1 for a ruling by an unauthorised key."""
    request, _gap = undecided(queue)
    size = receipts_head(queue)["size"]
    payload, sig = ruled(queue, request.id, keys["stranger"], receipts_head={"size": size, "root": "0" * 64})
    assert _late_refused(queue, request.id, payload, sig) == "the signer is not an enrolled key"
    assert queue.refused_answers(request.id) == []
    payload, sig = ruled(queue, request.id, keys["stranger"])
    _late_refused(queue, request.id, payload, sig)
    assert len(queue.refused_answers(request.id)) == 1


def test_unenrolled_keys_never_crowd_out_enrolled_evidence(tmp_path, keys, clock, strangers):
    """X2: the caps are per (request, enrolled): throwaway keys fill only their own REFUSED_UNENROLLED_PER_REQUEST,
    and enrolled keys refused for their class still get REFUSED_PER_REQUEST, each cap on its own."""
    enrolled_keys = strangers[:REFUSED_PER_REQUEST + 1]
    named = {**keys, **{f"e{i}": k for i, k in enumerate(enrolled_keys)}}
    (tmp_path / "auth").mkdir()
    log = Log(tmp_path / "auth", named)
    log.enrol("laptop", "laptop-presence")
    for i in range(len(enrolled_keys)):
        log.enrol(f"e{i}", "presence")                                # presence does not cover T2: refused
    q = ConsentQueue(tmp_path / "consent", authority=log.source, clock=clock, home=h4_home())
    request = FakeAgent(q).ask(hold=timedelta(hours=1))
    throwaway = strangers[REFUSED_PER_REQUEST + 1:] + [keys[n] for n in ("stranger", "phone", "presence", "revoked")]
    assert len(throwaway) > REFUSED_UNENROLLED_PER_REQUEST
    for k in throwaway:
        payload, sig = signed(q, request, k)
        assert _refused(q, request.id, payload, sig) == "the signer is not an enrolled key"
    assert len(q.refused_answers(request.id)) == REFUSED_UNENROLLED_PER_REQUEST
    for k in enrolled_keys:
        payload, sig = signed(q, request, k)
        assert _refused(q, request.id, payload, sig) == "key class presence does not cover T2"
    kept = q.refused_answers(request.id)
    assert [r["signer_fp"] for r in kept if r["enrolled"]] == [k.fp for k in enrolled_keys[:REFUSED_PER_REQUEST]]
    assert [r["signer_fp"] for r in kept if not r["enrolled"]] == \
        [k.fp for k in throwaway[:REFUSED_UNENROLLED_PER_REQUEST]]
    other = FakeAgent(q).ask("b", hold=timedelta(hours=1))           # and the enrolled cap alone, unenrolled empty
    for k in enrolled_keys:
        payload, sig = signed(q, other, k)
        _refused(q, other.id, payload, sig)
    payload, sig = signed(q, other, keys["stranger"])
    _refused(q, other.id, payload, sig)
    assert [r["enrolled"] for r in q.refused_answers(other.id)] == [True] * REFUSED_PER_REQUEST + [False]


def test_a_repeat_or_a_full_class_costs_no_replay_and_no_verify(queue, clock, keys, strangers, monkeypatch):
    """X3: a refusal already kept for this (request, signer, decision), or one whose class is full, returns from the
    first read: no authority replay (the late path replays only to keep) and no ssh-keygen."""
    import levain.consent.queue as qmod
    request = FakeAgent(queue).ask(hold=timedelta(seconds=30))
    other = FakeAgent(queue).ask("b", hold=timedelta(seconds=30))
    first, fsig = signed(queue, request, keys["stranger"])
    fill = [signed(queue, other, k) for k in strangers[:REFUSED_UNENROLLED_PER_REQUEST + 1]]
    clock.now = NOW + timedelta(seconds=30)                           # the late path: answer() itself never replays
    calls = {"verify": 0, "replay": 0}
    real_verify, real_fresh = qmod.verify_link_sig, ConsentQueue._fresh_authority

    def verify(*a, **kw):
        calls["verify"] += 1
        return real_verify(*a, **kw)

    def fresh(self):
        calls["replay"] += 1
        return real_fresh(self)

    monkeypatch.setattr(qmod, "verify_link_sig", verify)
    monkeypatch.setattr(ConsentQueue, "_fresh_authority", fresh)
    _refused(queue, request.id, first, fsig)
    assert calls == {"verify": 1, "replay": 1} and len(queue.refused_answers(request.id)) == 1
    clock.now = NOW + timedelta(seconds=31)
    again, asig = signed(queue, request, keys["stranger"], signed_at=utc_stamp(NOW + timedelta(seconds=1)))
    for payload, sig in ((first, fsig), (again, asig)):              # the same, and re-signed: one row, no work
        _refused(queue, request.id, payload, sig)
    assert calls == {"verify": 1, "replay": 1}
    for payload, sig in fill[:REFUSED_UNENROLLED_PER_REQUEST]:
        _refused(queue, other.id, payload, sig)
    calls.update(verify=0, replay=0)
    payload, sig = fill[-1]                                           # the unenrolled class is full: no verify
    _refused(queue, other.id, payload, sig)
    assert calls["verify"] == 0
    assert len(queue.refused_answers(other.id)) == REFUSED_UNENROLLED_PER_REQUEST


def test_a_full_class_in_time_costs_no_replay_beyond_the_answer_s_own(queue, strangers, monkeypatch):
    """X3, the path that already holds a replay: a full class returns before any further replay or verify."""
    import levain.consent.queue as qmod
    request = FakeAgent(queue).ask(hold=timedelta(hours=1))
    for k in strangers[:REFUSED_UNENROLLED_PER_REQUEST]:
        payload, sig = signed(queue, request, k)
        _refused(queue, request.id, payload, sig)
    calls = {"verify": 0, "replay": 0}
    real_verify, real_fresh = qmod.verify_link_sig, ConsentQueue._fresh_authority
    monkeypatch.setattr(qmod, "verify_link_sig",
                        lambda *a, **kw: calls.__setitem__("verify", calls["verify"] + 1) or real_verify(*a, **kw))
    monkeypatch.setattr(ConsentQueue, "_fresh_authority",
                        lambda self: calls.__setitem__("replay", calls["replay"] + 1) or real_fresh(self))
    payload, sig = signed(queue, request, strangers[REFUSED_UNENROLLED_PER_REQUEST])
    assert _refused(queue, request.id, payload, sig) == "the signer is not an enrolled key"
    assert calls == {"verify": 0, "replay": 1}                        # the one replay is answer()'s own


def test_warnings_as_errors_never_change_the_refusal(queue, keys, monkeypatch):
    """X4: with warnings as errors the bug's RuntimeWarning is dropped, never raised in place of the refusal."""
    request = FakeAgent(queue).ask()
    payload, sig = signed(queue, request, keys["stranger"])

    def bug(self, *a, **kw):
        raise KeyError("subreason")

    monkeypatch.setattr(ConsentQueue, "_refused_reason", bug)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert _refused(queue, request.id, payload, sig) == "the signer is not an enrolled key"
    assert queue.get(request.id)[1] == "pending"


def test_a_row_the_checks_refuse_is_a_warning_not_a_silent_no_row(queue, keys, monkeypatch):
    """X5: the insert ignores only the (request, signer, decision) dedupe; a CHECK failing (a bug in the keeping)
    raises out of the insert and is shown as a RuntimeWarning, the refusal unchanged."""
    request = FakeAgent(queue).ask()
    payload, sig = signed(queue, request, keys["stranger"])
    monkeypatch.setattr(ConsentQueue, "_refused_reason",
                        lambda self, *a, **kw: (REFUSED_NOT_AUTHORIZED, "late", "x", False))
    with pytest.warns(RuntimeWarning, match="was not kept: IntegrityError"):
        assert _refused(queue, request.id, payload, sig) == "the signer is not an enrolled key"
    assert queue.refused_answers(request.id) == []
