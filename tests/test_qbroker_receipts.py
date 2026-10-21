"""F10 S2b-1: the broker's own receipts log (levain.qbroker.receipts) and the operator library (levain.qbroker.operator).

Built from the S2b-1 run (project_memory/1020-f11/f10s2b_run): the log is the consent queue's table and triggers,
read by levain.consent.receipt_log unchanged; an answer is applied only over a head of that log (never size 0);
the operator lists, builds over the broker's head, and writes an answer row only when the broker would accept it.
Keys, signers and the fake resolver are test_qbroker's.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import timedelta

import pytest

from levain.consent import receipt_log
from levain.consent.record import ANSWER_NAMESPACE, answer_message
from levain.contracts.canon import canonical_bytes
from levain.qbroker import operator
from levain.qbroker.receipts import DB_NAME, Receipts
from levain.qbroker.signers import SignersSource
from levain.qbroker.store import ANSWER_ROW_FORMAT, QueueStore
import tests.test_qbroker as base
from tests.test_qbroker import NOW, ask, filed, head_of, hook_fields, make_broker, req, row_of, write_answer

# test_qbroker's fixtures, bound here so pytest finds them in this module
L, broker, clock, keys, resolver = base.L, base.broker, base.clock, base.keys, base.resolver

LATER = NOW + timedelta(seconds=10)
# the RFC 6962 empty tree's root: what every size-0 head holds
ZERO = {"size": 0, "root": hashlib.sha256(b"").hexdigest()}


def states(L, rid):
    conn = sqlite3.connect(L / "queue" / DB_NAME)
    try:
        return [r[0] for r in conn.execute("SELECT state FROM receipts WHERE request_id = ? ORDER BY seq", (rid,))]
    finally:
        conn.close()


def snapshot(L):
    return SignersSource(L / "policy", L / "routes" / "trust.pub").current()


def signed(L, rid, key, decision="allow", head=None):
    payload = operator.answer_payload(L / "queue", rid, decision, key.fp, now=NOW + timedelta(seconds=5))
    if head is not None:
        payload["receipts_head"] = head
    return payload, key.sign(answer_message(payload), ANSWER_NAMESPACE)


def raw(L, rid, payload, sig):
    (L / "queue" / "answers" / f"{rid}.json").write_bytes(
        canonical_bytes({"format": ANSWER_ROW_FORMAT, "payload": payload, "signature": sig}))


# --- the log ---------------------------------------------------------------------------------------------------

def test_a_new_row_files_one_pending_receipt_and_a_replay_none(L, broker):
    fields = hook_fields()
    rid = filed(broker, fields=fields)
    assert states(L, rid) == ["pending"]
    assert req(broker, fields=fields)["request_id"] == rid
    assert states(L, rid) == ["pending"]


def test_the_log_is_append_only_by_the_consent_queues_triggers(L, broker):
    filed(broker)
    conn = sqlite3.connect(L / "queue" / DB_NAME, isolation_level=None)
    for stmt in ("UPDATE receipts SET state = 'allowed'", "DELETE FROM receipts"):
        with pytest.raises(sqlite3.DatabaseError, match="receipts are append-only"):
            conn.execute(stmt)
    conn.close()


def test_an_accepted_answer_records_its_decision_and_the_head_reads_unchanged(L, broker, keys):
    rid = filed(broker)
    before = head_of(L)
    write_answer(L, rid, keys["operator"])
    assert ask(broker, rid)["decision"] == "allow"
    assert states(L, rid) == ["pending", "allowed"]
    conn = sqlite3.connect(L / "queue" / DB_NAME)
    assert receipt_log.head(conn) == head_of(L) and head_of(L)["size"] == before["size"] + 1
    conn.close()
    for _ in range(3):                                   # polls again: recorded once
        assert ask(broker, rid)["decision"] == "allow"
    assert states(L, rid) == ["pending", "allowed"]


def test_a_size_0_head_is_refused(L, broker, keys):
    rid = filed(broker)
    payload, sig = signed(L, rid, keys["operator"], head=ZERO)
    raw(L, rid, payload, sig)
    assert ask(broker, rid)["refused"] == "answer-refused"
    assert states(L, rid) == ["pending"]


def test_a_head_from_another_receipts_db_is_refused(L, broker, keys, tmp_path):
    rid = filed(broker)
    filed(broker, fields=hook_fields("e2"))
    other = tmp_path / "other"
    other.mkdir()
    foreign = Receipts(other)
    with foreign._write() as conn:
        for i in range(head_of(L)["size"]):
            conn.execute("INSERT INTO receipts (request_id, state, created_at, face) VALUES (?, 'pending', ?, '{}')",
                         (f"x-{i}", "2026-01-01T00:00:00.000000Z"))
    assert foreign.head()["size"] == head_of(L)["size"] and foreign.head() != head_of(L)
    payload, sig = signed(L, rid, keys["operator"], head=foreign.head())
    raw(L, rid, payload, sig)
    assert ask(broker, rid)["refused"] == "answer-refused"
    assert states(L, rid) == ["pending"]


def test_a_head_past_the_log_is_refused(L, broker, keys):
    rid = filed(broker)
    head = head_of(L)
    payload, sig = signed(L, rid, keys["operator"], head={"size": head["size"] + 5, "root": head["root"]})
    raw(L, rid, payload, sig)
    assert ask(broker, rid)["refused"] == "answer-refused"


def test_the_first_decision_stands(L, broker, keys):
    rid = filed(broker)
    write_answer(L, rid, keys["operator"], decision="deny")
    assert ask(broker, rid)["decision"] == "deny"
    write_answer(L, rid, keys["operator"])
    assert ask(broker, rid)["refused"] == "answer-refused"
    assert states(L, rid) == ["pending", "denied"]


def test_an_ended_hold_records_expired_once_and_never_after_a_decision(L, broker, keys, clock):
    open_rid = filed(broker)
    done = filed(broker, fields=hook_fields("e2"))
    write_answer(L, done, keys["operator"])
    assert ask(broker, done)["decision"] == "allow"
    clock["now"] = NOW + timedelta(minutes=5)
    for _ in range(2):
        assert ask(broker, open_rid)["refused"] == "request-expired"
        assert ask(broker, done)["refused"] == "request-expired"
    assert states(L, open_rid) == ["pending", "expired"]
    assert states(L, done) == ["pending", "allowed"]


def test_a_failed_receipt_write_is_repaired_by_the_hooks_refile(L, resolver, clock, monkeypatch):
    b = make_broker(L, resolver, clock)
    fields = hook_fields()

    def boom(*a, **kw):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(b._receipts, "filed", boom)
    assert req(b, fields=fields)["refused"] == "broker-error"
    monkeypatch.undo()
    got = req(b, fields=fields)
    assert got["ok"] is True and states(L, got["request_id"]) == ["pending"]


def test_a_receipts_write_failure_on_decide_is_a_broker_error_and_not_cached(L, broker, keys, monkeypatch):
    rid = filed(broker)
    write_answer(L, rid, keys["operator"])
    real = broker._receipts.decided

    def boom(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(broker._receipts, "decided", boom)
    assert ask(broker, rid)["refused"] == "broker-error"
    monkeypatch.setattr(broker._receipts, "decided", real)
    assert ask(broker, rid)["decision"] == "allow"


# --- the operator library --------------------------------------------------------------------------------------

def test_rows_lists_pend_rows_only_labelled_by_control_with_the_hooks_fields_as_claims(L, broker, keys, clock):
    a = filed(broker)
    b = filed(broker, fields=hook_fields("e2"))
    c = filed(broker, fields=hook_fields("e3"))
    req_dir = L / "queue" / "requests"
    (req_dir / "grant-2026-10-20T00:00:00Z-0123456789abcdef.json").write_text("{}")
    (req_dir / "pend-2026-10-20T00:00:00Z-0123456789abcdef.json").write_text("not a row")
    (req_dir / "notes.txt").write_text("x")
    write_answer(L, a, keys["operator"])
    assert ask(broker, a)["decision"] == "allow"
    write_answer(L, b, keys["stranger"])                 # answered, not accepted
    got = {r.request_id: r for r in operator.rows(L / "queue", now=LATER)}
    assert set(got) == {a, b, c}
    assert (got[a].state, got[a].settled) == ("allowed", True)
    assert (got[b].state, got[b].settled, got[b].answered) == ("pending", False, True)
    assert (got[c].state, got[c].answered) == ("pending", False)
    assert got[c].session == "sess-a" and got[c].slot == {"class": "egress", "index": 1} and got[c].kind == "interactive"
    assert dict(got[c].claims) == {"agent": "claude_code", "agent_session_id": "cc-1", "cwd": "/w",
                                   "display": 'Bash {"command": "ls"}'}
    assert operator.rows(L / "queue", now=NOW + timedelta(minutes=5))[-1].state == "expired"


def test_answer_payload_is_over_the_brokers_current_head_and_refuses_a_settled_request(L, broker, keys):
    rid = filed(broker)
    payload = operator.answer_payload(L / "queue", rid, "allow", keys["operator"].fp, now=LATER)
    assert payload["receipts_head"] == head_of(L) and payload["queue_id"] == row_of(L, rid)["queue_id"]
    write_answer(L, rid, keys["operator"])
    assert ask(broker, rid)["decision"] == "allow"
    with pytest.raises(operator.OperatorError, match="already allowed"):
        operator.answer_payload(L / "queue", rid, "allow", keys["operator"].fp, now=LATER)
    with pytest.raises(operator.OperatorError, match="already expired"):
        operator.answer_payload(L / "queue", filed(broker, fields=hook_fields("e2")), "allow", keys["operator"].fp,
                                now=NOW + timedelta(minutes=5))
    with pytest.raises(operator.OperatorError, match="no sealed request"):
        operator.answer_payload(L / "queue", "pend-2026-01-01T00:00:00Z-0123456789abcdef", "allow",
                                keys["operator"].fp, now=LATER)


def test_write_answer_writes_0640_no_replace_and_the_broker_accepts_it(L, broker, keys):
    rid = filed(broker)
    payload, sig = signed(L, rid, keys["operator"])
    path = operator.write_answer(L / "queue", rid, payload, sig, snapshot(L), now=LATER)
    assert path.stat().st_mode & 0o777 == 0o640
    assert ask(broker, rid)["decision"] == "allow"
    with pytest.raises(operator.OperatorError, match="already has an answer row"):
        operator.write_answer(L / "queue", rid, payload, sig, snapshot(L), now=LATER)


@pytest.mark.parametrize("bad", ["size0", "foreign", "stranger", "unsigned", "no-snapshot"])
def test_write_answer_never_writes_a_row_the_broker_would_refuse(L, broker, keys, tmp_path, bad):
    rid = filed(broker)
    head = head_of(L)
    if bad == "size0":
        payload, sig = signed(L, rid, keys["operator"], head=ZERO)
    elif bad == "foreign":
        payload, sig = signed(L, rid, keys["operator"], head={"size": head["size"], "root": "ab" * 32})
    elif bad == "stranger":
        payload, sig = signed(L, rid, keys["stranger"])
    else:
        payload, sig = signed(L, rid, keys["operator"])
        if bad == "unsigned":
            sig = ""
    with pytest.raises(operator.OperatorError):
        operator.write_answer(L / "queue", rid, payload, sig, None if bad == "no-snapshot" else snapshot(L), now=LATER)
    assert not (L / "queue" / "answers" / f"{rid}.json").exists()


def test_the_operator_side_writes_nothing_but_answers(tmp_path):
    q = tmp_path / "queue"
    for d in ("requests", "answers"):
        (q / d).mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        operator.rows(q)
    assert sorted(p.name for p in q.rglob("*")) == ["answers", "requests"]
    QueueStore(q)                                        # the broker makes the queue id; still no receipts log
    with pytest.raises(FileNotFoundError):
        operator.rows(q)
    assert not (q / DB_NAME).exists()
