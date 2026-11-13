"""READY_SCHEMA v2: the states, columns, audit kinds and rule order in the schema document and in ``ready_v2.json`` are
exactly the sets the function can emit. The function's constants are the one list; this test only compares."""
from __future__ import annotations

import json
import re
from pathlib import Path

from levain.desk import board_state as bs
from tests._board_state_cases import load_cases, run_case

REPO = Path(__file__).resolve().parents[1]
DOC = (REPO / "docs" / "ready-schema-v2.md").read_text(encoding="utf-8")
V2 = json.loads((REPO / "tests" / "fixtures" / "ready_schema" / "ready_v2.json").read_text(encoding="utf-8"))
V1 = REPO / "tests" / "fixtures" / "ready_schema" / "ready_v1.json"


def _section(title: str) -> str:
    m = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", DOC, re.S | re.M)
    assert m, title
    return m.group(1)


def test_the_document_lists_exactly_the_emittable_states_and_columns():
    rows = re.findall(r"^\| `(\w+)` \| `(\w+)` \|", _section("2. States and columns"), re.M)
    assert dict(rows) == bs.STATE_COLUMN and [s for s, _ in rows] == list(bs.STATES)


def test_the_document_lists_the_rule_order_of_the_table():
    rows = re.findall(r"^\| (\d+) \| (\w+) \|", _section("3. Precedence"), re.M)
    assert [int(n) for n, _ in rows] == list(range(1, len(rows) + 1))
    assert [name for _, name in rows] == [n for n, _ in bs._RULES]


def test_the_document_names_every_audit_kind_and_only_those():
    text = _section("7. The board and the audit")
    named = set(re.findall(r"`(unknown|landed_not_closed|gate_expired|unattributed_work|unknown_id_in_trailer)`", text))
    assert named == set(bs.AUDIT_KINDS)


def test_the_v2_fixture_states_columns_and_kinds_are_the_functions():
    assert V2["schema"] == 2 and V2["board_schema"] == bs.SCHEMA
    assert {r["state"]: r["column"] for r in V2["states"]} == bs.STATE_COLUMN
    assert [r["state"] for r in V2["states"]] == list(bs.STATES)
    assert tuple(V2["columns"]) == bs.COLUMNS
    assert tuple(V2["audit_kinds"]) == bs.AUDIT_KINDS
    assert tuple(V2["precedence"]) == tuple(n for n, _ in bs._RULES)
    assert V2["max_evidence_age_s"] == bs.MAX_EVIDENCE_AGE_S


def test_the_board_and_state_shapes_are_the_fixtures():
    case = load_cases()[0]
    _folded, board = run_case(bs, case)
    d = board.to_dict()
    assert list(d) == V2["board_keys"] and list(d["items"][0]) == V2["state_keys"]
    probe = bs.AuditEntry("unknown", None, "x").to_dict()
    assert list(probe) == V2["audit_entry_keys"]


def test_every_emitted_state_and_column_comes_from_the_declared_set():
    emitted_states, emitted_columns, emitted_kinds = set(), set(), set()
    for case in load_cases():
        _folded, board = run_case(bs, case)
        emitted_states |= {s.state for s in board.items}
        emitted_columns |= {s.column for s in board.items}
        emitted_kinds |= {a.kind for a in board.audit}
    assert emitted_states == set(bs.STATES) and emitted_columns == set(bs.COLUMNS)
    assert emitted_kinds == set(bs.AUDIT_KINDS)


def test_v1_stays_untouched_beside_v2():
    v1 = json.loads(V1.read_text(encoding="utf-8"))
    assert v1["schema"] == 1 and v1["cases"]
