"""TERMSIGN L4: a late ruling ALLOW carried inside a verified ``levain-terminal-answer/1`` (DESIGN section 8).

The queue gains ONE accept branch (:meth:`ConsentQueue.answer_terminal_ruling`). Keys and authority are the real
ones of ``tests/test_consent_queue.py`` (``laptop`` laptop-presence, ``phone`` display-phone, ``presence``,
``revoked``, ``stranger``), signed by real ``ssh-keygen`` in the terminal namespace; the live Secure Enclave check is
always an injected fake, never ``sc_auth``.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

import pytest

from levain.consent import AnswerRefused
from levain.consent import terminal_sign as ts
from levain.consent.record import ruling_message
from tests.test_consent_queue import NOW
from tests.test_consent_queue import authority, clock, keys, queue  # noqa: F401 - the shared fixtures
from tests.test_consent_queue_v4 import undecided

SCOPE = {"store": "levain-questions:s1", "install": "s1", "session": "sess-1", "view": "7"}
ITEM, VERSION = "q-1", "v1"


def fake_ctk(_public_key):
    return None, "test"


def carried(q, request_id, key, *, ruling_edit=None, kind="authority", at=None, sign_key=None, scope=None,
            payload_edit=None, item=ITEM, version=VERSION, extra=()):
    """The message and signature a seat would produce: one authority entry carrying the queue's own ruling."""
    payload = q.ruling_payload(request_id, "allow", key.fp)
    payload.update(payload_edit or {})
    text = ruling_message(payload).decode("utf-8")
    if ruling_edit is not None:
        text = ruling_edit(text)
    entry = ts.Entry(item, version, "push it?", kind, answer="(a) push", ruling=text if kind == "authority" else None)
    message = ts.build_message(store=(scope or SCOPE)["store"], install=(scope or SCOPE)["install"],
                               session=(scope or SCOPE)["session"], view=(scope or SCOPE)["view"], typed="1a",
                               entries=[entry, *extra], at=at or NOW)
    return message, (sign_key or key).sign(message, ts.NAMESPACE)


def apply(q, request_id, message, sig, **kw):
    kw.setdefault("item", ITEM)
    kw.setdefault("row_version", VERSION)
    kw.setdefault("scope", SCOPE)
    kw.setdefault("ctk", fake_ctk)
    return q.answer_terminal_ruling(request_id, message, sig, **kw)


def refused(q, request_id, message, sig, **kw):
    with pytest.raises(AnswerRefused) as got:
        apply(q, request_id, message, sig, **kw)
    assert q.get(request_id)[1] == "undecided"           # a refusal moves nothing
    return str(got.value)


def test_a_valid_carried_ruling_is_applied_once_and_delivers_as_any_ruling(queue, keys):
    request, gap = undecided(queue)
    message, sig = carried(queue, request.id, keys["laptop"])
    ruled = apply(queue, request.id, message, sig)
    assert (ruled.ruling, ruled.signer_fp, ruled.gap, ruled.request_digest) == \
        ("allow", keys["laptop"].fp, gap, request.digest)
    assert queue.get(request.id)[1] == "allowed"
    # the stored ruling re-verifies on every read: it is still a ruling, and the one take of its allow is given
    taken = queue.take_ruled_allow("claude_code", "sess-1", "Edit", {"command": "touch /tmp/x"}, cwd="/work/proj")
    assert taken is not None and taken.decision == "allow" and taken.signer_fp == keys["laptop"].fp
    assert queue.take_ruled_allow("claude_code", "sess-1", "Edit", {"command": "touch /tmp/x"},
                                  cwd="/work/proj") is None
    [rs] = queue.claim_rulings_for("claude_code", "sess-1")
    assert (rs.ruling, rs.request_id) == ("allow", request.id)


def test_a_second_apply_is_refused_and_records_nothing_more(queue, keys):
    request, _gap = undecided(queue)
    message, sig = carried(queue, request.id, keys["laptop"])
    apply(queue, request.id, message, sig)
    with pytest.raises(AnswerRefused, match="not undecided"):
        apply(queue, request.id, message, sig)
    assert [r["state"] for r in queue.receipts(request.id)] == ["undecided", "allowed"]


def test_a_signature_over_other_bytes_is_a_forgery(queue, keys):
    request, _ = undecided(queue)
    message, _sig = carried(queue, request.id, keys["laptop"])
    _other, other_sig = carried(queue, request.id, keys["laptop"], at=NOW - timedelta(seconds=5))
    assert "does not verify" in refused(queue, request.id, message, other_sig)


def test_a_forged_entry_signed_by_a_stranger_is_refused(queue, keys):
    request, _ = undecided(queue)
    message, sig = carried(queue, request.id, keys["laptop"], sign_key=keys["stranger"])
    assert "does not verify" in refused(queue, request.id, message, sig)


def test_a_ruling_naming_a_stranger_as_signer_is_refused(queue, keys):
    request, _ = undecided(queue)
    message, sig = carried(queue, request.id, keys["stranger"])
    assert refused(queue, request.id, message, sig) == "the signer is not an enrolled key"


def test_a_swapped_request_digest_is_refused(queue, keys):
    request, _ = undecided(queue)
    message, sig = carried(queue, request.id, keys["laptop"], payload_edit={"request_digest": "0" * 64})
    assert refused(queue, request.id, message, sig) == "the ruling signs another version of this request"


def test_a_ruling_replayed_to_another_gap_is_refused(queue, keys):
    request, gap = undecided(queue)
    other, other_gap = undecided(queue, "touch /tmp/y")
    assert other_gap != gap
    message, sig = carried(queue, request.id, keys["laptop"])
    assert refused(queue, other.id, message, sig) == "the ruling is for another request"
    message, sig = carried(queue, other.id, keys["laptop"], payload_edit={"request_id": request.id})
    with pytest.raises(AnswerRefused):
        apply(queue, other.id, message, sig)
    wrong = carried(queue, request.id, keys["laptop"], payload_edit={"gap": gap + 5})
    assert "gap" in refused(queue, request.id, *wrong)


def test_a_ruling_for_another_queue_is_refused(queue, keys):
    request, _ = undecided(queue)
    message, sig = carried(queue, request.id, keys["laptop"], payload_edit={"queue_id": "cq-elsewhere"})
    assert refused(queue, request.id, message, sig) == "the ruling names another queue"


def test_a_display_phone_key_cannot_carry_a_ruling(queue, keys):
    request, _ = undecided(queue)
    message, sig = carried(queue, request.id, keys["phone"])
    assert "display-phone" in refused(queue, request.id, message, sig)


def test_a_presence_key_cannot_carry_a_ruling(queue, keys):
    request, _ = undecided(queue)
    message, sig = carried(queue, request.id, keys["presence"])
    assert "cannot" in refused(queue, request.id, message, sig) or "does not cover" in \
        refused(queue, request.id, message, sig)


def test_a_revoked_key_cannot_carry_a_ruling(queue, keys):
    request, _ = undecided(queue)
    message, sig = carried(queue, request.id, keys["revoked"])
    assert refused(queue, request.id, message, sig) == "the signer's key is revoked"


def test_a_stale_receipts_head_is_refused(queue, keys):
    request, _ = undecided(queue)
    head = queue.ruling_payload(request.id, "allow", keys["laptop"].fp)["receipts_head"]
    message, sig = carried(queue, request.id, keys["laptop"],
                           payload_edit={"receipts_head": {"size": head["size"], "root": "ab" * 32}})
    assert "receipts" in refused(queue, request.id, message, sig)
    message, sig = carried(queue, request.id, keys["laptop"],
                           payload_edit={"receipts_head": {"size": head["size"] + 3, "root": head["root"]}})
    assert "receipts" in refused(queue, request.id, message, sig)


def test_a_ruling_that_differs_by_one_byte_is_refused(queue, keys):
    request, _ = undecided(queue)
    for edit in (lambda t: t.replace('"allow"', '"deny"'), lambda t: t + " ", lambda t: t.replace('"gap":', '"gap" :')):
        message, sig = carried(queue, request.id, keys["laptop"], ruling_edit=edit)
        with pytest.raises(AnswerRefused):
            apply(queue, request.id, message, sig)
        assert queue.get(request.id)[1] == "undecided"


def test_a_deny_is_not_carried(queue, keys):
    request, _ = undecided(queue)
    payload = queue.ruling_payload(request.id, "deny", keys["laptop"].fp)
    entry = ts.Entry(ITEM, VERSION, "push it?", "authority", answer="(a) push", ruling=ruling_message(payload).decode())
    message = ts.build_message(**{k: SCOPE[k] for k in SCOPE}, typed="1a", entries=[entry], at=NOW)
    assert "ALLOW only" in refused(queue, request.id, message, keys["laptop"].sign(message, ts.NAMESPACE))


def test_an_information_entry_cannot_carry_a_ruling(queue, keys):
    request, _ = undecided(queue)
    with pytest.raises(ts.MessageError):
        ts.build_message(**SCOPE, typed="1a", at=NOW, entries=[
            ts.Entry(ITEM, VERSION, "q", "information", answer="(a) x", ruling="{}")])
    # and an entry that is not an authority entry names no ruling for the question
    message, sig = carried(queue, request.id, keys["laptop"], kind="information")
    assert "no ruling for this question" in refused(queue, request.id, message, sig)


def test_an_unsigned_message_carries_no_authority(queue, keys):
    request, _ = undecided(queue)
    message, _ = carried(queue, request.id, keys["laptop"])
    for sig in ("", "not a signature", None):
        with pytest.raises(AnswerRefused):
            apply(queue, request.id, message, sig)
    assert queue.get(request.id)[1] == "undecided"


def test_a_ruling_for_another_question_or_version_or_scope_is_refused(queue, keys):
    request, _ = undecided(queue)
    message, sig = carried(queue, request.id, keys["laptop"])
    assert "no ruling for this question" in refused(queue, request.id, message, sig, item="q-2")
    assert "no ruling for this question" in refused(queue, request.id, message, sig, row_version="v2")
    for name in SCOPE:
        assert name in refused(queue, request.id, message, sig, scope={**SCOPE, name: "other"})
    assert "view" in refused(queue, request.id, message, sig, scope={k: v for k, v in SCOPE.items() if k != "view"})


def test_a_message_signed_long_ago_is_refused(queue, keys, clock):
    request, _ = undecided(queue)
    message, sig = carried(queue, request.id, keys["laptop"], at=NOW - timedelta(minutes=30))
    assert "ten minutes" in refused(queue, request.id, message, sig)


def test_a_failing_live_enclave_check_refuses(queue, keys):
    request, _ = undecided(queue)
    message, sig = carried(queue, request.id, keys["laptop"])
    assert "biometric" in refused(queue, request.id, message, sig, ctk=lambda pk: (False, "no such identity"))


def test_a_message_carrying_two_entries_judges_each_by_its_own_class(queue, keys):
    request, _ = undecided(queue)
    info = ts.Entry("q-9", "v9", "ok?", "information", answer="(a) fine")
    message, sig = carried(queue, request.id, keys["laptop"], extra=[info])
    ruled = apply(queue, request.id, message, sig)
    assert ruled.ruling == "allow"


def test_the_kept_message_cannot_be_edited_or_deleted(queue, keys):
    request, _ = undecided(queue)
    message, sig = carried(queue, request.id, keys["laptop"])
    apply(queue, request.id, message, sig)
    conn = sqlite3.connect(queue.path)
    try:
        for stmt in ("UPDATE terminal_rulings SET message = 'x'", "DELETE FROM terminal_rulings"):
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(stmt)
    finally:
        conn.close()


def test_a_ruled_row_whose_kept_message_is_gone_no_longer_delivers(queue, keys):
    """A raw writer that drops the kept message (the triggers aside) leaves a signature over bytes nobody holds:
    the row fails closed rather than falling back to a plain ruling check it could never pass."""
    request, _ = undecided(queue)
    message, sig = carried(queue, request.id, keys["laptop"])
    apply(queue, request.id, message, sig)
    conn = sqlite3.connect(queue.path)
    try:
        conn.execute("DROP TRIGGER terminal_rulings_kept")
        conn.execute("DELETE FROM terminal_rulings")
        conn.commit()
    finally:
        conn.close()
    assert queue.take_ruled_allow("claude_code", "sess-1", "Edit", {"command": "touch /tmp/x"},
                                  cwd="/work/proj") is None


def test_a_swapped_kept_message_fails_the_row_closed(queue, keys):
    request, _ = undecided(queue)
    message, sig = carried(queue, request.id, keys["laptop"])
    other, _ = carried(queue, request.id, keys["laptop"], at=NOW - timedelta(seconds=9))
    apply(queue, request.id, message, sig)
    conn = sqlite3.connect(queue.path)
    try:
        conn.execute("DROP TRIGGER terminal_rulings_frozen")
        conn.execute("UPDATE terminal_rulings SET message = ?", (other.decode("utf-8"),))
        conn.commit()
    finally:
        conn.close()
    assert queue.take_ruled_allow("claude_code", "sess-1", "Edit", {"command": "touch /tmp/x"},
                                  cwd="/work/proj") is None


def test_the_phone_path_is_unchanged(queue, keys):
    from levain.consent.record import RULING_NAMESPACE
    request, _ = undecided(queue)
    payload = queue.ruling_payload(request.id, "allow", keys["laptop"].fp)
    ruled = queue.answer_late(request.id, payload, keys["laptop"].sign(ruling_message(payload), RULING_NAMESPACE))
    assert ruled.ruling == "allow"
    conn = sqlite3.connect(queue.path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM terminal_rulings").fetchone() == (0,)
    finally:
        conn.close()


def test_a_signer_that_is_not_a_fingerprint_is_refused(queue, keys):
    request, _ = undecided(queue)
    message, sig = carried(queue, request.id, keys["laptop"], payload_edit={"signer_fp": 5})
    assert refused(queue, request.id, message, sig) == "the signer is not an enrolled key"


# --- L4 L3 r1 -----------------------------------------------------------------------------------------

def _verify(queue, source):
    from levain.consent.receipt_log import verify
    return verify(queue.directory, authority_for=lambda request: source)


def test_receipts_verify_reads_a_terminal_ruling_as_the_take_reads_it(queue, keys, authority):
    """T1 (complement + codex): verify sent a terminal-applied ruling through the plain ruling check (another
    namespace, over the record alone) and reported every valid one as a signature mismatch."""
    request, _ = undecided(queue)
    apply(queue, request.id, *carried(queue, request.id, keys["laptop"]))
    report = _verify(queue, authority)
    assert report.mismatches == () and report.ok
    assert report.signed == 1 and report.last_signed.request_id == request.id


def _raw(queue, *statements):
    conn = sqlite3.connect(queue.path)
    try:
        for stmt, args in statements:
            conn.execute(stmt, args)
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("kept", ["other", "not text", "garbage"])
def test_receipts_verify_reports_a_swapped_or_corrupt_kept_message(queue, keys, authority, kept):
    request, _ = undecided(queue)
    message, sig = carried(queue, request.id, keys["laptop"])
    other, _ = carried(queue, request.id, keys["laptop"], at=NOW - timedelta(seconds=9))
    apply(queue, request.id, message, sig)
    value = {"other": other.decode("utf-8"), "not text": b"\xff\xfe" + message, "garbage": "x"}[kept]
    _raw(queue, ("DROP TRIGGER terminal_rulings_frozen", ()), ("UPDATE terminal_rulings SET message = ?", (value,)))
    report = _verify(queue, authority)
    assert [rid for rid, _why in report.mismatches] == [request.id] and not report.ok


def test_receipts_verify_reports_a_terminal_ruling_whose_signer_was_revoked_since(tmp_path, keys, clock):
    from tests.test_consent_queue import Log
    from tests._h4 import h4_home
    from levain.consent import ConsentQueue

    (tmp_path / "auth").mkdir()
    log = Log(tmp_path / "auth", keys)
    log.enrol("laptop", "laptop-presence")
    log.enrol("presence", "laptop-presence")               # a second terminal key, revocable without the genesis
    q = ConsentQueue(tmp_path / "consent", authority=log.source, clock=clock, home=h4_home())
    request, _ = undecided(q)
    apply(q, request.id, *carried(q, request.id, keys["presence"]))
    log.revoke("presence")
    report = _verify(q, log.source)
    assert report.mismatches == () and [r.request_id for r in report.revoked] == [request.id]
    assert report.revoked[0].head_checked


def test_the_queues_own_verifier_checks_a_terminal_ruling(queue, keys, tmp_path, authority, clock):
    """T4 (codex): a queue built with its own ssh-keygen verified answers with it but terminal rulings with the
    default; a verifier levain may not trust (here a stub file in a scratch directory) now refuses both the apply
    and every re-read."""
    from tests._h4 import h4_home
    from levain.consent import ConsentQueue

    stub = tmp_path / "stub-ssh-keygen"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o700)
    own = ConsentQueue(queue.directory, authority=authority, clock=clock, home=h4_home(), ssh_keygen=stub)
    request, _ = undecided(queue)
    message, sig = carried(queue, request.id, keys["laptop"])
    assert "could not be verified" in refused(own, request.id, message, sig)
    apply(queue, request.id, message, sig)                 # the default verifier applies it
    assert own.take_ruled_allow("claude_code", "sess-1", "Edit", {"command": "touch /tmp/x"}, cwd="/work/proj") is None
    taken = queue.take_ruled_allow("claude_code", "sess-1", "Edit", {"command": "touch /tmp/x"}, cwd="/work/proj")
    assert taken is not None


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", '"\\ud800"'])
def test_a_non_standard_or_unencodable_embedded_ruling_is_refused_not_raised(queue, keys, bad):
    """T6 (codex): NaN / Infinity parse under json.loads and an unpaired surrogate decodes, then the record's
    canonical encoding raised ConsentError / UnicodeError past the caller instead of a refusal."""
    request, _ = undecided(queue)
    message, sig = carried(queue, request.id, keys["laptop"], ruling_edit=lambda t: t.replace('"allow"', bad))
    assert "not a record" in refused(queue, request.id, message, sig) or \
        "not the canonical encoding" in refused(queue, request.id, message, sig)
