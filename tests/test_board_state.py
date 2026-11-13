"""The pure board-state function (READY_SCHEMA v2): the fold, the conformance fixtures, the closed state set, the
module's purity, and a property run. The mutation tests (every precedence rule, every unknown cause) are in
test_board_state_mutations.py; the two failures that came from a measured day are in test_board_state_measured.py."""
from __future__ import annotations

import ast
import json
import random
from datetime import datetime, timezone
from pathlib import Path

import pytest

from levain.desk import board_state as bs
from tests._board_state_cases import case_problems, load_cases

REPO = Path(__file__).resolve().parents[1]
MODULE = REPO / "levain" / "desk" / "board_state.py"
CASES = load_cases()
NOW = datetime(2026, 11, 12, 20, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------------------------------------ conformance
@pytest.mark.parametrize("case", CASES, ids=[c["name"][:70] for c in CASES])
def test_fixture_case(case):
    assert case_problems(bs, case) == []


def test_case_names_are_unique_and_every_state_is_exercised():
    assert len({c["name"] for c in CASES}) == len(CASES)
    seen = {w["state"] for c in CASES for w in c["expect"].values()}
    assert seen == set(bs.STATES)


def test_every_case_reproduces_through_derive_state():
    """derive_state is the same function as one row of derive_all."""
    for case in CASES:
        folded = bs.fold(case["items_lines"])
        ev = bs.Evidence.from_dict(case["evidence"])
        now = bs.parse_timestamp(case["now"])
        board = {s.id: s for s in bs.derive_all(folded, ev, now=now).items}
        if folded.torn:
            continue                # derive_state has no view of the file: tearing is derive_all's
        for iid, item in folded.items.items():
            assert bs.derive_state(item, ev, folded.items, now=now) == board[iid], (case["name"], iid)


# ------------------------------------------------------------------------------------------------ fold
def _line(op="add", iid="levain#1", **kw):
    rec = {"v": 1, "op": op, "id": iid, "at": "2026-11-12T10:00:00Z", "by": "desk 17"}
    if op == "add":
        rec.update(repo="levain", title="t", origin="desk 17")
    rec.update(kw)
    return json.dumps(rec)


def test_fold_applies_records_in_file_order_and_keeps_provenance():
    f = bs.fold([_line(), _line("amend", fields={"title": "t2", "tier": "opus"}, why="retitled"),
                 _line("amend", fields={"title": "t3"}, why="again")])
    it = f.items["levain#1"]
    assert (it.title, it.tier, it.added_by) == ("t3", "opus", "desk 17")
    assert [(a.fields, a.why) for a in it.amendments] == [(("tier", "title"), "retitled"), (("title",), "again")]
    assert f.problems == () and not f.torn


def test_fold_skips_blank_lines_and_reads_a_decision_then_a_reopen():
    f = bs.fold(["", "  ", _line(), _line("decide", decision="cut", words="no", date="2026-11-12"),
                 _line("decide", decision="reopen", words="yes", date="2026-11-13")])
    assert f.items["levain#1"].decision is None and not f.torn


def test_fold_duplicate_add_is_a_problem_and_the_first_stands():
    f = bs.fold([_line(title="first"), _line(title="second")])
    assert f.items["levain#1"].title == "first" and not f.torn
    assert any("added twice" in p for p in f.problems)


@pytest.mark.parametrize("bad", [
    "{", "[]", "null", '"x"', json.dumps({"v": 1, "op": "add"}),
    _line(iid="not-an-id"), _line(done_kind="eventually"), _line(depends_on=[{"kind": "prose", "ref": "x"}]),
    _line(depends_on=[{"kind": "gate", "words": "w"}]), _line(depends_on=[{"kind": "gate", "words": "w", "recheck": "2026-13-45"}]),
    _line(depends_on="levain#2"), _line("amend", fields={"branch": "x"}, why="w"), _line("amend", fields={}, why="w"),
    _line("amend", fields={"title": "x"}), _line("decide", decision="burn", words="w", date="d"),
    _line("decide", decision="cut", words="", date="d"), json.dumps({"v": True, "op": "add", "id": "levain#1"}),
])
def test_a_line_that_cannot_be_trusted_tears_the_file(bad):
    f = bs.fold([_line("add", "levain#2"), bad])
    assert f.torn and f.torn_why


def test_a_torn_file_reads_every_item_unknown_decided_ones_included():
    f = bs.fold([_line(), _line("decide", decision="cut", words="no", date="2026-11-12"), "{"])
    ev = bs.Evidence.from_dict({"evidence_v": 1, "fetched_at": "2026-11-12T19:30:00Z", "unreadable": {}, "main": [],
                                "chain": [], "refs": [], "merged": [], "branches": [], "seats": [],
                                "band": {"open": {}, "resolved": {}}, "waiters": [], "documents": {}, "unattributed": []})
    assert [s.state for s in bs.derive_all(f, ev, now=NOW).items] == ["unknown"]


# ------------------------------------------------------------------------------------------------ evidence
def test_evidence_never_raises_and_names_what_it_could_not_read():
    for junk in (None, 5, "x", [], {}, {"evidence_v": 1, "main": 3, "seats": {"a": 1}, "band": [], "documents": {"p": "yes"}}):
        ev = bs.Evidence.from_dict(junk)
        assert ev.unreadable, junk
    ev = bs.Evidence.from_dict({"evidence_v": 1, "main": [{"sha": ""}], "band": {"open": {}}, "branches": [{"name": "b"}]})
    assert {"main", "band", "branches"} <= set(ev.unreadable)


def test_unreadable_given_as_a_list_of_names_is_read():
    ev = bs.Evidence.from_dict({"evidence_v": 1, "unreadable": ["seats"]})
    assert ev.unreadable["seats"] == "unreadable"


def test_the_latest_closing_commit_is_named_whatever_the_list_order():
    f = bs.fold([_line()])
    mc = lambda n, d: {"sha": f"{n:040x}", "date": d, "items": [], "closes": ["levain#1"]}       # noqa: E731
    base = {"evidence_v": 1, "fetched_at": "2026-11-12T19:30:00Z", "unreadable": {}, "chain": [], "refs": [], "merged": [],
            "branches": [], "seats": [], "band": {"open": {}, "resolved": {}}, "waiters": [], "documents": {}, "unattributed": []}
    for order in ([mc(1, "2026-11-10T00:00:00Z"), mc(2, "2026-11-11T00:00:00Z")],
                  [mc(2, "2026-11-11T00:00:00Z"), mc(1, "2026-11-10T00:00:00Z")]):
        s = bs.derive_all(f, bs.Evidence.from_dict({**base, "main": order}), now=NOW).items[0]
        assert s.state == "done" and f"{2:040x}"[:9] in s.reason


def test_naive_times_are_read_as_utc():
    f = bs.fold([_line()])
    base = {"evidence_v": 1, "fetched_at": "2026-11-12T19:30:00", "unreadable": {}, "main": [], "chain": [], "refs": [],
            "merged": [], "branches": [], "seats": [], "band": {"open": {}, "resolved": {}}, "waiters": [],
            "documents": {}, "unattributed": []}
    got = bs.derive_all(f, bs.Evidence.from_dict(base), now=datetime(2026, 11, 12, 20, 0)).items[0]
    assert got.state != "unknown"


# ------------------------------------------------------------------------------------------------ the closed sets
def test_state_column_audit_sets_are_the_declared_ones():
    assert len(bs.STATES) == 13 and bs.STATES[-1] == "unknown"
    assert set(bs.STATE_COLUMN.values()) == set(bs.COLUMNS)
    assert bs.COLUMNS == ("off", "done", "needs_you", "waiting", "running", "backlog", "ready", "unknown")
    assert bs.AUDIT_KINDS == ("unknown", "landed_not_closed", "gate_expired", "unattributed_work", "unknown_id_in_trailer")
    assert bs.MAX_EVIDENCE_AGE_S == 21600


def test_the_board_serialises_with_the_schema_word():
    folded, board = None, None
    case = CASES[0]
    folded = bs.fold(case["items_lines"])
    board = bs.derive_all(folded, bs.Evidence.from_dict(case["evidence"]), now=NOW)
    d = json.loads(json.dumps(board.to_dict()))
    assert d["schema"] == "ready-v2" and set(d) == {"schema", "as_of", "items", "audit", "unattributed", "problems"}
    assert set(d["items"][0]) == {"id", "repo", "title", "tier", "state", "column", "reason", "refs", "as_of"}


def test_a_long_chain_of_waits_reads_through_without_recursion_trouble():
    n = 150
    lines = [_line(iid="levain#1", done="d", tier="sonnet")]
    for i in range(2, n + 1):
        lines.append(_line(iid=f"levain#{i}", done="d", tier="sonnet", depends_on=[{"kind": "item", "ref": f"levain#{i - 1}"}]))
    ev = bs.Evidence.from_dict({"evidence_v": 1, "fetched_at": "2026-11-12T19:30:00Z", "unreadable": {}, "main": [],
                                "chain": [], "refs": [], "merged": [], "branches": [],
                                "seats": [{"name": "s", "items": ["levain#1"], "since": None, "branch": None}],
                                "band": {"open": {}, "resolved": {}}, "waiters": [], "documents": {}, "unattributed": []})
    board = bs.derive_all(bs.fold(lines), ev, now=NOW)
    assert board.items[0].state == "in_flight" and {s.state for s in board.items[1:]} == {"blocked"}


# ------------------------------------------------------------------------------------------------ purity
def test_the_module_does_no_io_reads_no_clock_and_no_environment():
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    bad: list[str] = []
    banned_modules = {"os", "subprocess", "time", "sys", "pathlib", "socket", "shutil", "tempfile", "sqlite3", "levain"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bad += [a.name for a in node.names if a.name.split(".")[0] in banned_modules]
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in banned_modules:
            bad.append(node.module or "")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"open", "input", "print", "exec", "eval"}:
            bad.append(node.func.id + "(")
        elif isinstance(node, ast.Attribute) and node.attr in {"now", "today", "utcnow"} \
                and isinstance(node.value, ast.Name) and "date" in node.value.id.lower():
            bad.append(f"{node.value.id}.{node.attr}")       # ``ctx.now`` is the parameter, not a clock read
        elif isinstance(node, ast.Attribute) and node.attr in {"time", "environ", "getenv", "read_text", "write_text"}:
            bad.append("." + node.attr)
        elif isinstance(node, ast.Name) and node.id in {"os", "subprocess"}:
            bad.append(node.id)
    assert bad == []


# ------------------------------------------------------------------------------------------------ property run
def _junk(rng: random.Random):
    return rng.choice([None, 5, "x", [], {}, [1], {"a": 1}, True])


def _random_world(rng: random.Random):
    n = rng.randint(0, 10)
    ids = [f"{rng.choice(['levain', 'flow'])}#{i}" for i in range(1, n + 1)]
    pool = ids + ["levain#999", "flow#998"]
    lines = []
    for iid in ids:
        deps = []
        for _ in range(rng.randint(0, 3)):
            k = rng.choice(["item", "item", "band", "link", "gate"])
            deps.append({"kind": "gate", "words": "g", "recheck": rng.choice(["2026-11-01", "2026-11-30"])} if k == "gate"
                        else {"kind": k, "ref": rng.choice(pool + ["np-aaaaaaaa", "np-bbbbbbbb", "queue/a", "queue/b"])})
        lines.append(json.dumps({"v": 1, "op": "add", "id": iid, "at": "t", "by": "b", "repo": iid.split("#")[0], "title": "t",
                                 "origin": "o", "done": rng.choice([None, "d", "p.md", "np-aaaaaaaa"]),
                                 "done_kind": rng.choice(list(bs.DONE_KINDS)), "tier": rng.choice([None, "sonnet", "land"]),
                                 "depends_on": deps}))
        if rng.random() < 0.2:
            lines.append(json.dumps({"v": 1, "op": "decide", "id": iid, "at": "t", "by": "b",
                                     "decision": rng.choice(["cut", "parked", "out-of-1.0", "reopen"]), "words": "w", "date": "d"}))
        if rng.random() < 0.05:
            lines.append(rng.choice(["{", "", "[]", json.dumps({"v": 9})]))
    pick = lambda: rng.sample(pool, rng.randint(0, min(3, len(pool))))                          # noqa: E731
    ev = {"evidence_v": rng.choice([1, 1, 1, 2]),
          "fetched_at": rng.choice(["2026-11-12T19:30:00Z", "2026-11-12T01:00:00Z", "junk", None]),
          "unreadable": rng.choice([{}, {}, {"git": "x"}, {"seats": "y"}, ["band"], _junk(rng)]),
          "main": [{"sha": f"{rng.getrandbits(40):x}", "date": "2026-11-12T10:00:00Z", "items": pick(), "closes": pick()}
                   for _ in range(rng.randint(0, 3))],
          "chain": [{"ref": "queue/a", "tip": "c1", "commits": [{"sha": "c1", "items": pick()}]} for _ in range(rng.randint(0, 2))],
          "refs": rng.choice([["queue/a"], [], _junk(rng)]), "merged": rng.choice([["queue/b"], []]),
          "branches": [{"name": f"seat/{i}", "tip": f"{rng.getrandbits(30):x}", "date": None, "ahead": rng.randint(0, 4), "items": pick()}
                       for i in range(rng.randint(0, 3))],
          "seats": [{"name": f"s{i}", "items": pick(), "since": None, "branch": rng.choice([None, "seat/0", "seat/1"])}
                    for i in range(rng.randint(0, 3))],
          "band": rng.choice([{"open": {"np-aaaaaaaa": pick()}, "resolved": {"np-bbbbbbbb": "phill"}},
                              {"open": {}, "resolved": {}}, _junk(rng)]),
          "waiters": [{"branch": "land/x", "items": pick()} for _ in range(rng.randint(0, 2))],
          "documents": rng.choice([{"p.md": True}, {"p.md": False}, {}, _junk(rng)]),
          "unattributed": rng.choice([[], [{"sha": "abc", "subject": "s"}], _junk(rng)])}
    for key in rng.sample(sorted(ev), rng.randint(0, 2)):
        ev.pop(key)
    return lines, ev


def test_property_derive_all_never_raises_and_stays_inside_the_closed_set():
    rng = random.Random(1112)
    for _ in range(200):
        lines, raw = _random_world(rng)
        board = bs.derive_all(bs.fold(lines), bs.Evidence.from_dict(raw), now=NOW)
        json.dumps(board.to_dict())
        for s in board.items:
            assert s.state in bs.STATES and s.column == bs.STATE_COLUMN[s.state]
            assert s.reason, s
            if s.state == "unknown":
                assert s.reason.strip()
        for a in board.audit:
            assert a.kind in bs.AUDIT_KINDS and a.why
        assert [a.id for a in board.audit if a.kind == "unknown" and a.id] == [s.id for s in board.items if s.state == "unknown"]
