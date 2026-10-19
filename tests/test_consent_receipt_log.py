"""Q11: every signed answer covers the consent queue's receipt log (an RFC 6962 head, computed from the rows), and
`levain consent receipts verify` checks every stored signed head against the log."""
from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime, timezone

import pytest

import levain.authority as auth
from levain.authority import ReplayCache, anchor_path
from levain.cli import main
from levain.consent.queue import AnswerRefused
from levain.consent.receipt_log import head
from levain.consent.record import ANSWER_FORMAT, ANSWER_NAMESPACE, answer_message
from tests.test_consent_answer_cli import _file, _key
from tests.test_consent_answer_cli import scratch  # noqa: F401 - the shared fixture


def _sign(key, message: bytes) -> str:
    return subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(key), "-n", ANSWER_NAMESPACE], input=message,
                          capture_output=True, check=True, timeout=30).stdout.decode()


def _stored(queue, request_id):
    conn = sqlite3.connect(queue.path)
    try:
        return json.loads(conn.execute("SELECT answer FROM requests WHERE id = ?", (request_id,)).fetchone()[0])
    finally:
        conn.close()


def _signed_over_one_receipt(queue, laptop, root, capsys):
    """A withdrawn request (receipt 1), then a second one answered through `levain consent answer`."""
    gone = _file(queue, "/w/gone.txt", "e-gone")
    assert queue.withdraw(gone.id)
    request = _file(queue, "/w/a.txt", "e-a")
    rc = main(["consent", "answer", request.id, "allow", "--signing-key", str(laptop),
               "--queue-dir", str(queue.directory), "--data-root", str(root)])
    assert rc == 0, capsys.readouterr().err
    return request


def test_the_cli_signs_the_receipts_head_and_the_queue_applies_it(scratch, capsys):
    queue, laptop, fp, root = scratch
    request = _signed_over_one_receipt(queue, laptop, root, capsys)
    out = capsys.readouterr().out
    stored = _stored(queue, request.id)
    assert stored["format"] == ANSWER_FORMAT
    assert stored["receipts_head"] == head(queue, 1) and stored["receipts_head"]["size"] == 1
    assert f"1 receipt(s), root {stored['receipts_head']['root']}" in out
    got = queue.decision(request.id)
    assert got is not None and got.decision == "allow" and got.signer_fp == fp
    assert main(["consent", "receipts", "verify", "--queue-dir", str(queue.directory), "--data-root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "receipts: 2" in out and f"last signed head: size 1 root {stored['receipts_head']['root']}" in out
    assert f"signed by {fp}" in out and "unsigned tail: 1 receipt(s)" in out and "MISMATCH" not in out


def test_an_edited_receipt_fails_the_verifier_and_refuses_the_next_answer(scratch, capsys):
    queue, laptop, fp, root = scratch
    covered = _signed_over_one_receipt(queue, laptop, root, capsys)
    later = _file(queue, "/w/b.txt", "e-b")
    payload = queue.answer_payload(later.id, "allow", fp)           # built before the edit: covers 2 receipts
    assert payload["receipts_head"]["size"] == 2
    signature = _sign(laptop, answer_message(payload))
    conn = sqlite3.connect(queue.path, isolation_level=None)
    try:                                                             # a writer with the uid: triggers dropped
        conn.execute("DROP TRIGGER receipts_frozen")
        conn.execute("DROP TRIGGER receipts_kept")
        first = conn.execute("SELECT seq, face FROM receipts ORDER BY seq LIMIT 1").fetchone()
        edited = first[1].replace('"query_text": "', '"query_text": "(edited) ', 1)
        assert edited != first[1]
        assert conn.execute("UPDATE receipts SET face = ? WHERE seq = ?", (edited, first[0])).rowcount == 1
    finally:
        conn.close()
    capsys.readouterr()
    assert main(["consent", "receipts", "verify", "--queue-dir", str(queue.directory), "--data-root", str(root)]) == 1
    out = capsys.readouterr().out
    assert f"MISMATCH {covered.id}: the receipts changed under this answer" in out
    assert "last signed head: none" in out
    with pytest.raises(AnswerRefused, match="the receipts changed under this answer"):
        queue.answer(later.id, payload, signature)
    assert queue.get(later.id)[1] == "pending"


def test_an_answer_in_the_unchained_format_is_refused_with_a_reason_to_answer_again(scratch):
    queue, laptop, fp, _ = scratch
    request = _file(queue, "/w/a.txt", "e-old")
    old = {k: v for k, v in queue.answer_payload(request.id, "allow", fp).items() if k != "receipts_head"}
    old["format"] = "levain-consent-answer/1"
    with pytest.raises(AnswerRefused, match="signed before the queue's receipts were chained.*answer the request "
                                            "again"):
        queue.answer(request.id, old, _sign(laptop, answer_message(old)))
    assert queue.get(request.id)[1] == "pending"


def _append(root, record, key, fp):
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    try:
        auth.sign_and_append(
            conn, record, signer_fp=fp,
            sign=lambda m: subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(key), "-n", auth.NAMESPACE],
                                          input=m, capture_output=True, check=True).stdout.decode(),
            clock=lambda: datetime.now(timezone.utc), cache=ReplayCache(), anchor=anchor_path(root))
    finally:
        conn.close()


def test_a_signer_revoked_since_is_reported_not_failed(scratch, capsys, tmp_path):
    # Q11 head call 2: a key revoked SINCE it signed is not a chain failure (exit 0, its own line); delivery still
    # delivers nothing for it. Seen red: exit 1, "MISMATCH ...: the signer's key is revoked"
    queue, laptop, fp, root = scratch
    request = _signed_over_one_receipt(queue, laptop, root, capsys)
    other, other_pub, other_fp = _key(tmp_path / "keys", "laptop2")
    _append(root, {"op": "enrol", "fingerprint": other_fp, "public_key": other_pub, "class": "laptop-presence",
                   "custody": "passphrase"}, laptop, fp)
    _append(root, {"op": "revoke_key", "fingerprint": fp}, other, other_fp)
    assert queue.decision(request.id) is None                       # delivery: a revoked key delivers nothing
    capsys.readouterr()
    rc = main(["consent", "receipts", "verify", "--queue-dir", str(queue.directory), "--data-root", str(root)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert f"signer revoked since: {fp} (request {request.id}; its head matches" in out and "MISMATCH" not in out
    # never the last human-signed head: the whole log is the unsigned tail
    assert "last signed head: none" in out and "unsigned tail: 2 receipt(s)" in out
    # an edit under it still fails: a revoked signer's head is checked all the same
    conn = sqlite3.connect(queue.path, isolation_level=None)
    try:
        conn.execute("DROP TRIGGER receipts_frozen")
        conn.execute("UPDATE receipts SET created_at = '2000-01-01T00:00:00Z' WHERE seq = "
                     "(SELECT MIN(seq) FROM receipts)")
    finally:
        conn.close()
    assert main(["consent", "receipts", "verify", "--queue-dir", str(queue.directory), "--data-root", str(root)]) == 1
    assert f"MISMATCH {request.id}: the receipts changed under this answer" in capsys.readouterr().out
