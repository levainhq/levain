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


def test_unreadable_given_as_a_list_of_names_fails_closed_and_still_names_the_sources():
    ev = bs.Evidence.from_dict({"evidence_v": 1, "unreadable": ["seats"]})
    assert ev.unreadable["seats"] == "unreadable" and "malformed unreadable" in ev.unreadable["evidence"]


@pytest.mark.parametrize("bad", ["git exploded", 7, True, {"git": 5}, {"git": None}, [1, 2], {1: "x"}])
def test_a_malformed_unreadable_value_fails_closed(bad):
    ev = bs.Evidence.from_dict({"evidence_v": 1, "unreadable": bad})
    assert "malformed unreadable" in ev.unreadable["evidence"]


def test_the_latest_closing_commit_is_named_whatever_the_list_order():
    f = bs.fold([_line()])
    sha = {1: "1" * 40, 2: "2" * 40}                  # the first nine characters differ, so the reason can tell them apart
    mc = lambda n, d: {"sha": sha[n], "date": d, "items": [], "closes": ["levain#1"]}       # noqa: E731
    base = {"evidence_v": 1, "fetched_at": "2026-11-12T19:30:00Z", "unreadable": {}, "chain": [], "refs": [], "merged": [],
            "branches": [], "seats": [], "band": {"open": {}, "resolved": {}}, "waiters": [], "documents": {}, "unattributed": []}
    for order in ([mc(1, "2026-11-10T00:00:00Z"), mc(2, "2026-11-11T00:00:00Z")],
                  [mc(2, "2026-11-11T00:00:00Z"), mc(1, "2026-11-10T00:00:00Z")]):
        s = bs.derive_all(f, bs.Evidence.from_dict({**base, "main": order}), now=NOW).items[0]
        assert s.state == "done" and sha[2][:9] in s.reason and sha[1][:9] not in s.reason


def test_naive_evidence_times_are_read_as_utc():
    f = bs.fold([_line()])
    base = {"evidence_v": 1, "fetched_at": "2026-11-12T19:30:00", "unreadable": {}, "main": [], "chain": [], "refs": [],
            "merged": [], "branches": [], "seats": [], "band": {"open": {}, "resolved": {}}, "waiters": [],
            "documents": {}, "unattributed": []}
    got = bs.derive_all(f, bs.Evidence.from_dict(base), now=NOW).items[0]
    assert got.state != "unknown"


def test_a_naive_now_is_refused_with_a_reason_not_guessed_at():
    f = bs.fold([_line()])
    ev = bs.Evidence.from_dict({"evidence_v": 1, "fetched_at": "2026-11-12T19:30:00Z", "unreadable": {}, "main": [],
                                "chain": [], "refs": [], "merged": [], "branches": [], "seats": [],
                                "band": {"open": {}, "resolved": {}}, "waiters": [], "documents": {}, "unattributed": []})
    naive = datetime(2026, 11, 12, 20, 0)
    with pytest.raises(TypeError, match="timezone-aware"):
        bs.derive_all(f, ev, now=naive)
    with pytest.raises(TypeError, match="timezone-aware"):
        bs.derive_state(f.items["levain#1"], ev, f.items, now=naive)


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


def _chain_world(n: int, low_waits_on_high: bool):
    """n items in one wait chain; the root (the one that waits on nothing) is held by a live seat."""
    root = n if low_waits_on_high else 1
    lines = []
    for i in range(1, n + 1):
        nxt = i + 1 if low_waits_on_high else i - 1
        deps = [] if i == root else [{"kind": "item", "ref": f"levain#{nxt}"}]
        lines.append(_line(iid=f"levain#{i}", done="d", tier="sonnet", depends_on=deps))
    ev = bs.Evidence.from_dict({"evidence_v": 1, "fetched_at": "2026-11-12T19:30:00Z", "unreadable": {}, "main": [],
                                "chain": [], "refs": [], "merged": [], "branches": [],
                                "seats": [{"name": "s", "items": [f"levain#{root}"], "since": None, "branch": None}],
                                "band": {"open": {}, "resolved": {}}, "waiters": [], "documents": {}, "unattributed": []})
    return lines, ev, root


@pytest.mark.parametrize("low_waits_on_high", [False, True], ids=["high-waits-on-low", "low-waits-on-high"])
def test_a_long_chain_of_waits_reads_through_without_recursion_trouble(low_waits_on_high):
    """The second direction is the one that recursed: derive_all visits ids ascending, so a low id waiting on a high one
    is computed top-down with nothing memoised yet."""
    n = 300
    lines, ev, root = _chain_world(n, low_waits_on_high)
    board = bs.derive_all(bs.fold(lines), ev, now=NOW)
    by = {s.id: s for s in board.items}
    assert by[f"levain#{root}"].state == "in_flight"
    assert {s.state for i, s in by.items() if i != f"levain#{root}"} == {"blocked"}
    top = f"levain#{1 if low_waits_on_high else n}"
    assert by[top].reason.count("waits on") == n - 1                    # read through all the way to the root
    folded = bs.fold(lines)
    got = bs.derive_state(folded.items[top], ev, folded.items, now=NOW)
    assert got == by[top]


def test_a_dependency_chain_deeper_than_the_interpreter_can_recurse_still_answers():
    n = 3000
    lines = [_line(iid="levain#%d" % i, done="d", tier="sonnet",
                   depends_on=[] if i == n else [{"kind": "item", "ref": "levain#%d" % (i + 1)}]) for i in range(1, n + 1)]
    ev = bs.Evidence.from_dict({"evidence_v": 1, "fetched_at": "2026-11-12T19:30:00Z", "unreadable": {}, "main": [],
                                "chain": [], "refs": [], "merged": [], "branches": [], "seats": [],
                                "band": {"open": {}, "resolved": {}}, "waiters": [], "documents": {}, "unattributed": []})
    board = bs.derive_all(bs.fold(lines), ev, now=NOW)
    states = {s.id: s.state for s in board.items}
    assert states["levain#%d" % n] == "ready" and states["levain#1"] == "blocked"


# ------------------------------------------------------------------------------------------------ purity
#: the only top-level modules the function may import; anything else is a way to reach the disk, the clock or the net
ALLOWED_IMPORTS = {"__future__", "collections", "dataclasses", "datetime", "functools", "json", "re", "typing"}
CLOCK_ATTRS = {"now", "today", "utcnow", "time", "monotonic", "perf_counter", "fromtimestamp"}


def _impurities(source: str) -> list[str]:
    """What in ``source`` could read the disk, the clock, the environment or the net, in any import form."""
    bad: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            bad += [f"import {a.name}" for a in node.names if a.name.split(".")[0] not in ALLOWED_IMPORTS]
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "").split(".")[0] not in ALLOWED_IMPORTS:
                bad.append(f"from {'.' * node.level}{node.module or ''} import")
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in {"open", "input", "print", "exec", "eval", "compile", "__import__", "breakpoint"}:
                bad.append(f.id + "(")
            elif isinstance(f, ast.Attribute) and f.attr in CLOCK_ATTRS:
                bad.append("." + f.attr + "(")                    # ``ctx.now`` is the parameter; a CALL of ``now`` is the clock
        elif isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv", "read_text", "write_text", "read_bytes", "open"}:
            bad.append("." + node.attr)
        elif isinstance(node, ast.Name) and node.id in {"os", "sys", "subprocess", "__builtins__", "__import__"}:
            bad.append(node.id)
    return bad


def test_the_module_does_no_io_reads_no_clock_and_no_environment():
    assert _impurities(MODULE.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("snippet", [
    "import os", "import glob\nglob.glob('*')", "import io\nio.open('x')", "import pathlib", "import shutil", "import socket",
    "import subprocess", "import random\nrandom.random()", "import urllib.request", "from os import path",
    "import datetime as dt\ndt.datetime.now()", "from datetime import datetime as D\nD.now()",
    "from datetime import datetime\ndatetime.today()", "import datetime\ndatetime.datetime.utcnow()",
    "__import__('os').getcwd()", "open('f')", "import time\ntime.time()", "from . import x", "import sys\nsys.argv",
    "x.environ", "p.read_text()",
])
def test_the_purity_check_catches_every_way_to_read_the_world(snippet):
    assert _impurities(snippet), f"{snippet!r} passed the purity check"


def test_the_purity_check_lets_the_modules_own_kind_of_code_through():
    assert _impurities("import json, re\nfrom datetime import date, datetime, timezone\nx = ctx.now.date()\ny = d.astimezone(timezone.utc)") == []


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
        unknown_ids = [s.id for s in board.items if s.state == "unknown"]
        listed = [a.id for a in board.audit if a.kind in ("unknown", "gate_expired") and a.id in unknown_ids]
        assert sorted(listed, key=bs._id_key) == unknown_ids                                              # once each


# ------------------------------------------------------------------------------------------------ order independence
def _shuffled(raw, rng: random.Random):
    """The same evidence with every list (and every dict's key order) permuted."""
    if isinstance(raw, list):
        out = [_shuffled(x, rng) for x in raw]
        rng.shuffle(out)
        return out
    if isinstance(raw, dict):
        keys = list(raw)
        rng.shuffle(keys)
        return {k: _shuffled(raw[k], rng) for k in keys}
    return raw


def _board_dict(lines, raw):
    return bs.derive_all(bs.fold(lines), bs.Evidence.from_dict(raw), now=NOW).to_dict()


def test_the_whole_board_does_not_depend_on_the_order_the_evidence_is_listed_in():
    """Ties (equal dates, several seats, several orphans) are broken by sha / name, so reason and refs agree too."""
    rng = random.Random(1136)
    for _ in range(150):
        lines, raw = _random_world(rng)
        want = _board_dict(lines, raw)
        for _ in range(3):
            assert _board_dict(lines, _shuffled(raw, rng)) == want


def test_fixture_boards_do_not_depend_on_evidence_order_either():
    rng = random.Random(7)
    for case in CASES:
        want = _board_dict(case["items_lines"], case["evidence"])
        for _ in range(3):
            assert _board_dict(case["items_lines"], _shuffled(case["evidence"], rng)) == want, case["name"]


def test_equal_dates_are_broken_by_sha_and_by_name_not_by_list_position():
    f = bs.fold([_line()])
    base = {"evidence_v": 1, "fetched_at": "2026-11-12T19:30:00Z", "unreadable": {}, "chain": [], "refs": [], "merged": [],
            "band": {"open": {}, "resolved": {}}, "waiters": [], "documents": {}, "unattributed": [], "main": [],
            "branches": [], "seats": []}
    same = "2026-11-11T00:00:00Z"
    a = {"sha": "a" * 40, "date": same, "items": ["levain#1"], "closes": ["levain#1"]}
    b = {"sha": "b" * 40, "date": same, "items": ["levain#1"], "closes": ["levain#1"]}
    reasons = {bs.derive_all(f, bs.Evidence.from_dict({**base, "main": order}), now=NOW).items[0].reason for order in ([a, b], [b, a])}
    assert len(reasons) == 1
    seat = lambda n: {"name": n, "items": ["levain#1"], "since": None, "branch": None}                    # noqa: E731
    reasons = {bs.derive_all(f, bs.Evidence.from_dict({**base, "seats": order}), now=NOW).items[0].reason
               for order in ([seat("s1"), seat("s2")], [seat("s2"), seat("s1")])}
    assert len(reasons) == 1


def test_item_ids_that_tie_numerically_still_sort_the_same_way_in_any_file_order():
    la, lb = _line(iid="levain#01"), _line(iid="levain#1")
    ev = bs.Evidence.from_dict({"evidence_v": 1, "fetched_at": "2026-11-12T19:30:00Z", "unreadable": {}, "main": [],
                                "chain": [], "refs": [], "merged": [], "branches": [], "seats": [],
                                "band": {"open": {}, "resolved": {}}, "waiters": [], "documents": {}, "unattributed": []})
    ids = {tuple(s.id for s in bs.derive_all(bs.fold(order), ev, now=NOW).items) for order in ([la, lb], [lb, la])}
    assert len(ids) == 1


# ------------------------------------------------------------------------------------------------ fold edges
@pytest.mark.parametrize("raw", [b'{"v": 1}', 5, None, ["x"], {"v": 1}])
def test_a_line_that_is_not_text_tears_the_file_instead_of_vanishing(raw):
    f = bs.fold([_line(), raw])
    assert f.torn and "line 2" in f.torn_why and any("not text" in p for p in f.problems)


def _tie_world(reverse: bool):
    """Every kind of tie in one world: two seats, two chain refs, two waiters, two band rows (one set closed on main),
    two orphan branches with the same date, seats found through branches, two unattributed commits. ``reverse`` lists
    every one of them the other way round."""
    o = (lambda xs: list(reversed(xs))) if reverse else (lambda xs: list(xs))                 # noqa: E731
    lines = [_line(iid=f"levain#{i}", done="d", tier="sonnet") for i in range(1, 8)]
    commit = lambda n, ids: {"sha": f"{n}" * 40, "items": ids}                                 # noqa: E731
    raw = {
        "evidence_v": 1, "fetched_at": "2026-11-12T19:30:00Z", "unreadable": {}, "refs": [], "merged": [],
        "main": [{"sha": "e" * 40, "date": "2026-11-11T00:00:00Z", "items": ["levain#5"], "closes": ["levain#5"]}],
        "chain": o([{"ref": "queue/b", "tip": "1" * 40, "commits": [commit(1, ["levain#2"])]},
                    {"ref": "queue/a", "tip": "2" * 40, "commits": [commit(2, ["levain#2"])]}]),
        "branches": o([{"name": "o-b", "tip": "3" * 40, "date": "2026-11-11T09:00:00Z", "ahead": 1, "items": ["levain#6"]},
                       {"name": "o-a", "tip": "4" * 40, "date": "2026-11-11T09:00:00Z", "ahead": 1, "items": ["levain#6"]},
                       {"name": "b7-y", "tip": "5" * 40, "date": None, "ahead": 1, "items": ["levain#7"]},
                       {"name": "b7-x", "tip": "6" * 40, "date": None, "ahead": 1, "items": ["levain#7"]}]),
        "seats": o([{"name": "s-b", "items": ["levain#1"], "since": None, "branch": None},
                    {"name": "s-a", "items": ["levain#1"], "since": None, "branch": None},
                    {"name": "t-b", "items": [], "since": None, "branch": "b7-y"},
                    {"name": "t-a", "items": [], "since": None, "branch": "b7-x"}]),
        "band": {"open": dict(o([("np-b", ["levain#4"]), ("np-a", ["levain#4"]), ("np-d", ["levain#5"]), ("np-c", ["levain#5"])])),
                 "resolved": {}},
        "waiters": o([{"branch": "land/b", "items": ["levain#3"]}, {"branch": "land/a", "items": ["levain#3"]}]),
        "documents": {},
        "unattributed": o([{"sha": "f" * 40, "subject": "two"}, {"sha": "9" * 40, "subject": "one"}]),
    }
    return lines, raw


def test_what_a_reason_names_among_tied_evidence_does_not_depend_on_the_listing_order():
    first = _board_dict(*_tie_world(False))
    second = _board_dict(*_tie_world(True))
    assert first == second
    by = {s["id"]: s for s in first["items"]}
    assert "seat s-a" in by["levain#1"]["reason"] and by["levain#1"]["refs"] == ["s-a", "s-b"]
    assert by["levain#2"]["state"] == "landing" and "queue/a" in by["levain#2"]["reason"] and by["levain#2"]["refs"] == ["queue/a", "queue/b"]
    assert "land/a" in by["levain#3"]["reason"] and by["levain#3"]["refs"] == ["land/a", "land/b"]
    assert by["levain#4"]["reason"] == "band np-a, np-b"
    assert by["levain#5"]["state"] == "unknown" and "open band row np-c" in by["levain#5"]["reason"]
    assert by["levain#6"]["state"] == "ready_with_start" and "orphan branch o-b" in by["levain#6"]["reason"]   # same date: the name decides
    assert by["levain#7"]["state"] == "in_flight" and "seat t-a" in by["levain#7"]["reason"]
    assert [u["sha"] for u in first["unattributed"]] == ["9" * 40, "f" * 40]
