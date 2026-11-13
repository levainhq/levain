"""Mutation tests for the board-state function: the rule table and the unknown checks are data, so each mutation is
made on the live module (swap two adjacent rules; remove one check) and the conformance fixtures must catch it. A
mutation that leaves every fixture green would be an unprotected rule, so each one names the case that must go red."""
from __future__ import annotations

import pytest

from levain.desk import board_state as bs
from tests._board_state_cases import case_problems, load_cases

CASES = load_cases()
BY_NAME = {c["name"]: c for c in CASES}


def _red(names_prefix: str) -> list[str]:
    """The names of the cases whose name starts with ``names_prefix`` that no longer conform."""
    return [c["name"] for c in CASES if c["name"].startswith(names_prefix) and case_problems(bs, c)]


def _all_red() -> list[str]:
    return [c["name"] for c in CASES if case_problems(bs, c)]


def test_baseline_every_fixture_conforms_before_any_mutation():
    assert _all_red() == []


#: swap rules i and i+1 of the table: the fixture that names the pair must go red
SWAPS = [
    ("decision", "done", "precedence 1 over 2"),
    ("done", "needs_you", "precedence 2 over 3"),
    ("needs_you", "landing", "precedence 3 over 4"),
    ("landing", "in_flight", "precedence 4 over 5"),
    ("in_flight", "landed_not_closed", "precedence 5 over 6"),
    ("landed_not_closed", "blocked", "precedence 6 over 7"),
    ("blocked", "ready_with_start", "precedence 7 over 8"),
    ("ready_with_start", "needs_grooming", "precedence 8 over 9"),
    ("needs_grooming", "ready", "precedence 9 over 10"),
]


def test_the_swap_table_covers_every_adjacent_pair_of_the_rule_table():
    names = [n for n, _ in bs._RULES]
    assert [(a, b) for a, b, _ in SWAPS] == list(zip(names, names[1:]))


@pytest.mark.parametrize("first,second,case_prefix", SWAPS, ids=[f"{a}<->{b}" for a, b, _ in SWAPS])
def test_swapping_two_adjacent_rules_turns_the_named_fixture_red(monkeypatch, first, second, case_prefix):
    rules = list(bs._RULES)
    i = [n for n, _ in rules].index(first)
    assert rules[i + 1][0] == second
    rules[i], rules[i + 1] = rules[i + 1], rules[i]
    monkeypatch.setattr(bs, "_RULES", tuple(rules))
    assert _red(case_prefix), f"swapping {first}/{second} left {case_prefix!r} green: the order is unprotected"


#: removing one check: the case that exists for that cause must go red
CHECKS = [
    ("torn", "unknown: a torn items file"),
    ("unreadable", "unknown: git unreadable"),
    ("stale", "unknown: stale evidence"),
    ("cycle", "unknown: a two-item cycle"),
    ("dangling_item", "unknown: a dangling item link"),
    ("dangling_band", "unknown: a dangling band link"),
    ("dangling_link", "unknown: a dangling queue link"),
    ("unchecked_document", "unknown: a document path the evidence never checked"),
    ("conflict_closed_seat", "unknown: conflict, closed on main and a live seat"),
    ("conflict_closed_band", "unknown: conflict, closed on main and an open band row"),
    ("conflict_chain_orphan", "unknown: conflict, in the chain and an orphan branch"),
]


def test_the_check_table_covers_every_check():
    assert [n for n, _ in CHECKS] == [n for n, _ in bs._UNKNOWN_CHECKS]


@pytest.mark.parametrize("check,case_prefix", CHECKS, ids=[c for c, _ in CHECKS])
def test_removing_an_unknown_check_turns_its_fixture_red(monkeypatch, check, case_prefix):
    kept = tuple((n, f) for n, f in bs._UNKNOWN_CHECKS if n != check)
    assert len(kept) == len(bs._UNKNOWN_CHECKS) - 1
    monkeypatch.setattr(bs, "_UNKNOWN_CHECKS", kept)
    assert _red(case_prefix), f"removing the {check} check left {case_prefix!r} green: the cause is unprotected"


@pytest.mark.parametrize("source", ["git", "seats", "notes", "band", "waiters"])
def test_each_unreadable_source_is_noticed_by_the_unreadable_check(source):
    ev = dict(BY_NAME["precedence 5 over 6: a live seat beats commits on main"]["evidence"])
    ev["unreadable"] = {source: "gone"}
    folded = bs.fold(BY_NAME["precedence 5 over 6: a live seat beats commits on main"]["items_lines"])
    board = bs.derive_all(folded, bs.Evidence.from_dict(ev), now=bs.parse_timestamp("2026-11-12T20:00:00Z"))
    assert board.items[0].state == "unknown" and f"{'main' if source in ('git', 'notes') else source} unreadable" in board.items[0].reason
