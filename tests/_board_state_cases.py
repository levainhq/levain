"""The conformance fixtures of the board-state function and the runner that applies one case to it.

A case is ``{name, items_lines, evidence, now, expect: {id: {state, column, reason_contains}}}`` plus the optional
``audit`` (the exact [kind, id] list) and ``problems_contain``; the runner takes the module as an argument so the same
file can be pointed at another installed copy of the function."""
from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType
from typing import Any

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "board_state"


def load_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        cases.extend(json.loads(path.read_text(encoding="utf-8"))["cases"])
    return cases


def run_case(bs: ModuleType, case: dict[str, Any]):
    folded = bs.fold(case["items_lines"])
    board = bs.derive_all(folded, bs.Evidence.from_dict(case["evidence"]), now=bs.parse_timestamp(case["now"]))
    return folded, board


def case_problems(bs: ModuleType, case: dict[str, Any]) -> list[str]:
    """Everything the board gets wrong against the case's expectation (empty when it conforms)."""
    folded, board = run_case(bs, case)
    got = {s.id: s for s in board.items}
    bad: list[str] = []
    for iid, want in case["expect"].items():
        s = got.get(iid)
        if s is None:
            bad.append(f"{iid}: not on the board")
            continue
        if s.state != want["state"]:
            bad.append(f"{iid}: state {s.state!r} != {want['state']!r} ({s.reason})")
        if s.column != want["column"]:
            bad.append(f"{iid}: column {s.column!r} != {want['column']!r}")
        if want["reason_contains"] not in s.reason:
            bad.append(f"{iid}: reason {s.reason!r} lacks {want['reason_contains']!r}")
    if "audit" in case and [[a.kind, a.id] for a in board.audit] != case["audit"]:
        bad.append(f"audit {[[a.kind, a.id] for a in board.audit]} != {case['audit']}")
    for frag in case.get("problems_contain", ()):
        if not any(frag in p for p in board.problems):
            bad.append(f"no problem contains {frag!r}: {list(board.problems)}")
    return bad
