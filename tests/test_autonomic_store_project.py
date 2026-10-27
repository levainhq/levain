"""A1c1 part 2: the receipt file as the projection of a ``levain-autonomic/3`` store's receipt table (autogrant DESIGN
§r66.5, r7.0): ``gate_record`` with its envelope subject, ``project_rows`` durable before it returns and tolerant of a
torn tail, ``read`` once per id."""
from __future__ import annotations

import json
import os

import pytest

from levain.autonomic import Posture
from levain.autonomic import journal as J
from levain.autonomic import schema3 as s
from levain.autonomic import store as S
from levain.autonomic.store import GateReceiptStore, gate_record

FACE = {"context_id": "c", "terminal_state": "refused", "gate": {"verdict": "denied", "by": "on-loop"}}


def _row(subject, at="2026-10-26T09:00:00+00:00", face=FACE):
    rid, rec = gate_record(created_at=at, action_name="send", proposal_id=None, posture=Posture.ON_LOOP, fired=False,
                           action_face=face, subject=subject)
    return rid, rec


def test_gate_record_is_append_s_record_and_a_subject_enters_envelope_and_id(tmp_path):
    st = GateReceiptStore(tmp_path / "r.jsonl")
    rid = st.append(created_at="2026-10-26T09:00:00+00:00", action_name="send", proposal_id=None,
                    posture=Posture.ON_LOOP, fired=False, action_face=FACE)
    plain_id, plain = gate_record(created_at="2026-10-26T09:00:00+00:00", action_name="send", proposal_id=None,
                                  posture=Posture.ON_LOOP, fired=False, action_face=FACE)
    assert (plain_id, plain) == (rid, json.loads((tmp_path / "r.jsonl").read_text()))
    a, ra = _row("run:r1")
    b, rb = _row("run:r2")
    assert ra["subject"] == "run:r1" and "subject" not in plain
    assert len({a, b, plain_id}) == 3      # one clock reading, one context: still three ids


def test_project_rows_is_durable_before_it_returns(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(S, "durable_fsync", lambda fd: calls.append(os.fstat(fd)))
    path = tmp_path / "d" / "r.jsonl"
    st = GateReceiptStore(path)
    rid, rec = _row("run:r1")
    assert st.project_rows([(rid, json.dumps(rec))]) == [rid]
    file_ino, dir_ino = os.stat(path).st_ino, os.stat(path.parent).st_ino
    root_ino = os.stat(tmp_path).st_ino
    assert [c.st_ino for c in calls] == [file_ino, dir_ino, root_ino]   # the file, its new directory, that one's parent
    calls.clear()
    rid2, rec2 = _row("run:r2")
    assert st.project_rows([(rid2, json.dumps(rec2))]) == [rid2]
    assert [c.st_ino for c in calls] == [file_ino, dir_ino]        # the file existed: its directory still flushed


def test_project_rows_reads_a_torn_multibyte_tail(tmp_path):
    path = tmp_path / "r.jsonl"
    path.write_bytes(b'{"id": "x", "q": "\xe2\x82')
    st = GateReceiptStore(path)
    rid, rec = _row("run:r1")
    assert st.project_rows([(rid, json.dumps(rec))]) == [rid]
    assert path.read_bytes().startswith(b'{"id": "x", "q": "\xe2\x82\n')
    assert [r.id for r in st.read()] == [rid]


def test_a_row_already_in_the_file_is_not_appended_twice_and_read_once(tmp_path):
    path = tmp_path / "r.jsonl"
    st = GateReceiptStore(path)
    rows = [_row("run:r1"), _row("run:r2")]
    texts = [(rid, json.dumps(rec)) for rid, rec in rows]
    assert st.project_rows(texts[:1]) == [texts[0][0]]
    assert st.project_rows(texts) == [texts[0][0], texts[1][0]]    # a re-projection after a stop before the mark
    assert len(path.read_text().splitlines()) == 2
    with path.open("a") as fh:                                     # a repeat written by some earlier writer
        fh.write(texts[0][1] + "\n")
    assert [r.id for r in st.read()] == [texts[1][0], texts[0][0]]  # newest first, by arrival; once per id


def test_a_v3_close_is_projected_then_marked(tmp_path):
    store = s.AutonomicStore3(tmp_path / "store")
    with store.write() as c:
        c.execute("INSERT INTO runs (run_id, binding_id, generation, seal) VALUES ('r1', 'b', 0, ?)",
                  (json.dumps({"context_id": "c", "confirm_class": False, "guard": [], "links": 1}),))
        rid, rec = _row("run:r1")
        assert J.close_run_in(c, "r1", reason="denied:x", receipt=J.Receipt(rid, rec)).terminal is J.Terminal.WON
    st = GateReceiptStore(tmp_path / "receipts.jsonl")
    with store.read() as c:
        rows = J.unprojected_receipts(c)
    done = st.project_rows(rows)
    with store.write() as c:
        assert J.mark_projected(c, done) == 1
    with store.read() as c:
        assert J.unprojected_receipts(c) == [] and s.invariant(c) == []
    [got] = st.read()
    assert (got.id, got.fired, got.action_face["terminal_state"]) == (rid, False, "refused")


@pytest.mark.parametrize("bad", [b"", b"\n", b"[1]\n", b'{"id": 3}\n', b"not json\n"])
def test_a_file_of_odd_lines_never_stops_a_projection(tmp_path, bad):
    path = tmp_path / "r.jsonl"
    path.write_bytes(bad)
    rid, rec = _row("run:r1")
    assert GateReceiptStore(path).project_rows([(rid, json.dumps(rec))]) == [rid]


# ---- L3 r1 (codex 2 / complement 2): the directory is flushed on every projection ------------------------------------
def test_a_projection_into_a_file_it_did_not_create_still_flushes_its_directory(tmp_path, monkeypatch):
    path = tmp_path / "r.jsonl"
    st = GateReceiptStore(path)
    st.append(created_at="2026-10-26T09:00:00+00:00", action_name="send", proposal_id=None,
              posture=Posture.ON_LOOP, fired=False, action_face=FACE)   # append created it, with no directory flush
    calls = []
    monkeypatch.setattr(S, "durable_fsync", lambda fd: calls.append(os.fstat(fd).st_ino))
    rid, rec = _row("run:r1")
    assert st.project_rows([(rid, json.dumps(rec))]) == [rid]
    assert calls == [os.stat(path).st_ino, os.stat(tmp_path).st_ino]


# ---- L3 r1 (codex 3): only a line equal to the db receipt counts as projected ----------------------------------------
@pytest.mark.parametrize("foreign", [{"kind": "future"}, {"kind": "gate"}, {"kind": "gate", "created_at": "x"}])
def test_a_foreign_or_partial_line_with_the_same_id_is_not_the_projection(tmp_path, foreign):
    path = tmp_path / "r.jsonl"
    rid, rec = _row("run:r1")
    path.write_text(json.dumps({"id": rid, **foreign}) + "\n")
    st = GateReceiptStore(path)
    assert st.project_rows([(rid, json.dumps(rec))]) == [rid]
    assert [json.loads(x) for x in path.read_text().splitlines()][-1] == rec   # the real receipt was appended
    [got] = st.read()
    assert (got.id, got.action_face) == (rid, rec["action_face"])


def test_a_complete_gate_line_with_the_same_id_and_other_content_is_never_marked(tmp_path):
    path = tmp_path / "r.jsonl"
    rid, rec = _row("run:r1")
    other = dict(rec, action_name="something-else")
    path.write_text(json.dumps(other) + "\n")
    rid2, rec2 = _row("run:r2")
    st = GateReceiptStore(path)
    assert st.project_rows([(rid, json.dumps(rec)), (rid2, json.dumps(rec2))]) == [rid2]   # the other row goes on
    assert st.project_rows([(rid, json.dumps(rec))]) == []                                 # and stays unmarked


def test_project_rows_flushes_every_directory_the_call_created(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(S, "durable_fsync", lambda fd: calls.append(os.fstat(fd).st_ino))
    path = tmp_path / "a" / "b" / "r.jsonl"
    rid, rec = _row("run:r1")
    assert GateReceiptStore(path).project_rows([(rid, json.dumps(rec))]) == [rid]
    ino = lambda p: os.stat(p).st_ino           # noqa: E731
    # the file; its directory (the file's entry); then the parent of each created directory, up to the first that
    # already existed: b's entry lives in a, a's entry in tmp_path
    assert calls == [ino(path), ino(path.parent), ino(path.parent.parent), ino(tmp_path)]


def test_a_gate_record_that_differs_only_by_a_json_type_is_not_the_same_record(tmp_path):
    # RED (dict ==): ``{"fired": 0} == {"fired": false}`` in Python, so the db receipt was never appended
    path = tmp_path / "r.jsonl"
    rid, rec = _row("run:r1")
    held = dict(rec, fired=0)
    path.write_text(json.dumps(held) + "\n")
    assert GateReceiptStore(path).project_rows([(rid, json.dumps(rec))]) == []
    assert path.read_text() == json.dumps(held) + "\n"          # the foreign-typed line is left, the row not marked
    other = tmp_path / "o.jsonl"
    other.write_text(json.dumps(rec) + "\n")
    assert GateReceiptStore(other).project_rows([(rid, json.dumps(rec))]) == [rid]       # the same record still counts
