"""Q11: every signed answer covers the consent queue's receipt log (an RFC 6962 head, computed from the rows), and
`levain consent receipts verify` checks every stored signed head against the log."""
from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

import pytest

import levain.authority as auth
from levain.authority import ReplayCache, anchor_path
from levain.cli import main
from levain.consent.queue import AnswerRefused
from levain.consent.receipt_log import head
from levain.consent.record import ANSWER_FORMAT, ANSWER_NAMESPACE, answer_message
from tests.test_consent_answer_cli import _file, _key
from tests.test_consent_answer_cli import scratch  # noqa: F401 - the shared fixture
from tests._h4 import h4_home


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


# --- L3 r1 (1019+24 triage F1-F5) -------------------------------------------------------------------------------

def _verify_cli(queue, root, capsys):
    capsys.readouterr()
    rc = main(["consent", "receipts", "verify", "--queue-dir", str(queue.directory), "--data-root", str(root)])
    got = capsys.readouterr()
    return rc, got.out, got.err


def _raw_sql(queue, *statements):
    conn = sqlite3.connect(queue.path, isolation_level=None)
    try:
        for sql in statements:
            conn.execute(sql)
    finally:
        conn.close()


def test_verify_refuses_a_prior_format_store_and_names_the_command_that_migrates_it(scratch, tmp_path, capsys):
    # F1. Seen red on a9a66aea: rc 0, "receipts: 0", a /3 store with receipts read as an empty, clean log
    from levain.authority import anchor_path as _anchor
    from levain.consent.queue import ConsentQueue, authority_source
    from tests.test_consent_queue_v4 import _v3_module

    queue, _, _, root = scratch
    v3 = _v3_module()
    old = v3.ConsentQueue(queue.directory, authority=v3.authority_source(root / "cockpit.db", anchor=_anchor(root)))
    gone = old.file(agent="claude_code", agent_session_id="s", tool_name="Write",
                    tool_input={"file_path": "/w/a.txt", "content": "hi"}, cwd="/w", hold=timedelta(minutes=5))
    assert old.withdraw(gone.id)
    rc, out, err = _verify_cli(queue, root, capsys)
    assert rc == 1, out
    assert f"UNREADABLE: {queue.path} is in format {v3.STORE_FORMAT!r}" in out and "levain consent pending" in out
    assert "receipts:" not in out
    conn = sqlite3.connect(queue.path)
    try:                                                             # verify never migrates or writes
        assert conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()[0] == v3.STORE_FORMAT
    finally:
        conn.close()
    # the advice is true: `levain consent pending` migrates it, and verify then reads the log
    assert main(["consent", "pending", "--queue-dir", str(queue.directory)]) == 0
    rc, out, _ = _verify_cli(ConsentQueue(queue.directory, authority=authority_source(root / "cockpit.db",
                                                                                      anchor=_anchor(root)), home=h4_home()),
                             root, capsys)
    assert rc == 0, out
    assert "receipts: 1" in out and "UNREADABLE" not in out


def test_verify_refuses_a_zero_byte_store(scratch, capsys):
    # F1. Seen red on a9a66aea: rc 0, "receipts: 0"
    queue, _, _, root = scratch
    queue.path.touch(mode=0o600)
    assert queue.path.stat().st_size == 0
    rc, out, _ = _verify_cli(queue, root, capsys)
    assert rc == 1, out
    assert "UNREADABLE:" in out and "an empty database" in out
    assert queue.path.stat().st_size == 0                           # never set up by verify


@pytest.mark.parametrize("dropped", [("receipts", "requests"), ("receipts",)])
def test_verify_refuses_a_store_whose_log_tables_were_dropped(scratch, capsys, dropped):
    # F1. Seen red on a9a66aea: both dropped -> rc 0, "receipts: 0"; one dropped -> a stderr refusal, not UNREADABLE
    queue, laptop, _, root = scratch
    _signed_over_one_receipt(queue, laptop, root, capsys)
    _raw_sql(queue, *(f"DROP TABLE {t}" for t in dropped))
    rc, out, _ = _verify_cli(queue, root, capsys)
    assert rc == 1, out
    assert "UNREADABLE:" in out and "lacks receipts" in out


def test_verify_reads_a_fresh_empty_current_store_as_an_empty_clean_log(scratch, capsys):
    # F1's control: a current-format store with its tables and no rows is a readable (empty) log
    queue, _, _, root = scratch
    assert queue.pending() == []
    rc, out, _ = _verify_cli(queue, root, capsys)
    assert rc == 0, out
    assert "receipts: 0" in out and "UNREADABLE" not in out


def _non_text_receipt(queue, kind):
    """A writer with the uid stores a receipt column that is not text: a BLOB face (TEXT affinity keeps a BLOB), or
    an integer state (the table rebuilt with no type on ``state``, since TEXT affinity would store '7')."""
    if kind == "blob face":
        _raw_sql(queue, "DROP TRIGGER receipts_frozen",
                 "UPDATE receipts SET face = x'00ff' WHERE seq = (SELECT MIN(seq) FROM receipts)")
    else:
        _raw_sql(queue, "DROP TRIGGER receipts_frozen", "DROP TRIGGER receipts_kept",
                 "ALTER TABLE receipts RENAME TO receipts_old",
                 "CREATE TABLE receipts (seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, state, "
                 "created_at TEXT NOT NULL, face TEXT NOT NULL)",
                 "INSERT INTO receipts SELECT * FROM receipts_old", "DROP TABLE receipts_old",
                 "UPDATE receipts SET state = 7 WHERE seq = (SELECT MIN(seq) FROM receipts)")
    conn = sqlite3.connect(queue.path)
    try:
        got = conn.execute("SELECT typeof(face), typeof(state) FROM receipts ORDER BY seq LIMIT 1").fetchone()
    finally:
        conn.close()
    assert got == (("blob", "text") if kind == "blob face" else ("text", "integer"))


@pytest.mark.parametrize("kind", ["blob face", "int state"])
def test_a_non_text_receipt_refuses_the_apply_and_the_payload_and_verify_reports_it(scratch, capsys, kind):
    # F2. Seen red on a9a66aea: TypeError (Object of type bytes / the int is not ... ) out of queue.answer,
    # queue.answer_payload and the verify CLI, a crash in each, never a refusal
    queue, laptop, fp, root = scratch
    covered = _signed_over_one_receipt(queue, laptop, root, capsys)
    later = _file(queue, "/w/b.txt", "e-b")
    payload = queue.answer_payload(later.id, "deny", fp)
    signature = _sign(laptop, answer_message(payload))
    _non_text_receipt(queue, kind)
    with pytest.raises(AnswerRefused, match="the receipts cannot be read as a log"):
        queue.answer(later.id, payload, signature)
    assert queue.get(later.id)[1] == "pending"
    with pytest.raises(AnswerRefused, match="the receipts cannot be read as a log"):
        queue.answer_payload(later.id, "allow", fp)
    rc, out, err = _verify_cli(queue, root, capsys)
    assert rc == 1, out
    assert "UNREADABLE: the receipts cannot be read as a log" in out and "receipts: 2" in out
    assert f"MISMATCH {covered.id}: the receipts cannot be read as a log" in out


def test_ruling_payload_refuses_on_a_non_text_receipt(scratch, capsys):
    # F2 (the ruling's payload, the same head read). Seen red on a9a66aea: TypeError
    queue, laptop, fp, root = scratch
    request = _file(queue, "/w/u.txt", "e-u")
    queue.mark_undecided(request.id)
    _non_text_receipt(queue, "blob face")
    with pytest.raises(AnswerRefused, match="the receipts cannot be read as a log"):
        queue.ruling_payload(request.id, "allow", fp)


def test_a_stored_answer_whose_format_is_not_text_is_a_mismatch_not_a_crash(scratch, capsys):
    # F3. Seen red on a9a66aea: TypeError: unhashable type: 'list' out of the verify CLI
    from levain.cockpit.consent import encode

    queue, laptop, _, root = scratch
    request = _signed_over_one_receipt(queue, laptop, root, capsys)
    stored = _stored(queue, request.id)
    stored["format"] = []
    conn = sqlite3.connect(queue.path, isolation_level=None)
    try:
        conn.execute("DROP TRIGGER requests_frozen")
        conn.execute("UPDATE requests SET answer = ? WHERE id = ?", (encode(stored).decode("utf-8"), request.id))
    finally:
        conn.close()
    rc, out, _ = _verify_cli(queue, root, capsys)
    assert rc == 1, out
    assert f"MISMATCH {request.id}: answer format [] is not" in out


def test_a_head_size_at_the_ceiling_is_not_well_formed_and_never_reaches_sqlite(scratch):
    # F4. Seen red on a9a66aea: head_problem passed 2**63 and head_refusal raised OverflowError (LIMIT ?).
    # Through queue.answer the record's own integer domain (encode, I-JSON safe range) refuses it first.
    from levain.consent.receipt_log import HEAD_SIZE_CEILING, head_problem, head_refusal

    queue, _, _, _ = scratch
    root_hex = "0" * 64
    for size in (HEAD_SIZE_CEILING, 2 ** 63):
        assert head_problem({"size": size, "root": root_hex}) == f"receipts head size {size!r} is not a count"
    assert head_problem({"size": HEAD_SIZE_CEILING - 1, "root": root_hex}) is None
    with queue._read() as conn:
        assert head_refusal(conn, {"size": HEAD_SIZE_CEILING - 1, "root": root_hex}, noun="answer") == \
            "the answer names more receipts than the queue holds"


class _Flipping(Mapping):
    """A payload whose ``decision`` reads ``first`` for its first ``after`` reads, then ``then``."""

    def __init__(self, payload, first, then, after):
        self.payload, self.first, self.then, self.after, self.reads = payload, first, then, after, 0

    def __getitem__(self, key):
        if key != "decision":
            return self.payload[key]
        self.reads += 1
        return self.first if self.reads <= self.after else self.then

    def __iter__(self):
        return iter(self.payload)

    def __len__(self):
        return len(self.payload)


def test_a_payload_changed_after_answer_starts_cannot_change_what_is_applied(scratch):
    # F5. A deny signed; the mapping shows "deny" through every read the checks make (on a9a66aea: three, the
    # last the signature's), then "allow". Seen red on a9a66aea: the request was ALLOWED (answer() re-read
    # payload["decision"] after the signature check)
    queue, laptop, fp, _ = scratch
    request = _file(queue, "/w/a.txt", "e-a")
    deny = queue.answer_payload(request.id, "deny", fp)
    signature = _sign(laptop, answer_message(deny))
    flipping = _Flipping(deny, "deny", "allow", after=3)
    got = queue.answer(request.id, flipping, signature)
    assert got.decision == "deny" and queue.get(request.id)[1] == "denied"
    assert _stored(queue, request.id)["decision"] == "deny"


def test_a_receipt_sqlite_cannot_decode_refuses_the_apply_and_the_payload_and_verify_reports_it(scratch, capsys):
    # L3 r2 (complement + codex): text that is not UTF-8 fails in sqlite3's own decode, at fetchall, before leaf()
    # sees the row. Seen red on 6892aed7: sqlite3.OperationalError out of queue.answer and queue.answer_payload
    queue, laptop, fp, root = scratch
    _signed_over_one_receipt(queue, laptop, root, capsys)
    later = _file(queue, "/w/b.txt", "e-b")
    payload = queue.answer_payload(later.id, "deny", fp)
    signature = _sign(laptop, answer_message(payload))
    _raw_sql(queue, "DROP TRIGGER receipts_frozen",
             "UPDATE receipts SET face = CAST(x'80ff' AS TEXT) WHERE seq = (SELECT MIN(seq) FROM receipts)")
    with pytest.raises(AnswerRefused, match="the receipts cannot be read as a log"):
        queue.answer(later.id, payload, signature)
    assert queue.get(later.id)[1] == "pending"
    with pytest.raises(AnswerRefused, match="the receipts cannot be read as a log"):
        queue.answer_payload(later.id, "allow", fp)
    rc, out, err = _verify_cli(queue, root, capsys)
    assert rc == 1, out
    assert "UNREADABLE:" in out and "not UTF-8" in out


def test_a_record_nested_past_any_record_is_refused_not_a_recursion_crash(scratch):
    # L3 r2 (complement LOW): the phone page posts the payload; a deeply nested one must be a refusal (the phone
    # handler answers AnswerRefused with a refusal), never a RecursionError. Seen red on 6892aed7: RecursionError
    queue, laptop, fp, _ = scratch
    request = _file(queue, "/w/a.txt", "e-a")
    deny = queue.answer_payload(request.id, "deny", fp)
    signature = _sign(laptop, answer_message(deny))
    deep: object = {}
    for _ in range(5000):
        deep = {"x": deep}
    with pytest.raises(AnswerRefused, match="nests deeper"):
        queue.answer(request.id, {**deny, "x": deep}, signature)
    assert queue.get(request.id)[1] == "pending"
