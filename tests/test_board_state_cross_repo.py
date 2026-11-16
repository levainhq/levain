"""Two live failures of ``levain board state`` found on the real board (levain#72, levain#73).

levain#72: a dependency in another repo (flow#5 -> levain#1) was judged under the dependent's repo's git, where it has
no work, so it read ``ready`` and held its dependent. levain#73: a row closed by a ``Board-Closes`` on main read
``unknown`` because an orphan seat branch sat beside its landed chain (levain#1 and seat/1114-44-storeresolver)."""
from __future__ import annotations

import json

from levain.desk import board_state as bs

NOW = bs.parse_timestamp("2026-11-12T20:00:00Z")
SHA = "c" * 40


def _add(iid, **extra):
    rec = {"v": 1, "op": "add", "id": iid, "at": "2026-11-12T10:00:00Z", "by": "desk 17", "repo": iid.split("#")[0],
           "title": "a row", "words": "", "origin": "desk 17", "done": "merged and closed", "done_kind": "merged",
           "tier": "sonnet", "depends_on": []}
    rec.update(extra)
    return json.dumps(rec)


def _evidence(**over):
    ev = {"evidence_v": 1, "fetched_at": "2026-11-12T19:30:00Z", "unreadable": {}, "main": [], "chain": [],
          "refs": [], "merged": [], "branches": [], "seats": [], "band": {"open": {}, "resolved": {}},
          "waiters": [], "documents": {}, "unattributed": []}
    ev.update(over)
    return bs.Evidence.from_dict(ev)


def _states(lines, ev, **kw):
    return {s.id: s for s in bs.derive_all(bs.fold(lines), ev, now=NOW, **kw).items}


def _closed(iid):
    return {"sha": SHA, "date": "2026-11-12T12:00:00Z", "items": [iid], "closes": [iid]}


# ------------------------------------------------------------------------------------------------ levain#72
def _two_repos():
    lines = [_add("levain#1"), _add("flow#5", depends_on=[{"kind": "item", "ref": "levain#1", "label": "the resolver"}])]
    flow_git = _evidence()                                  # flow's git has no work for levain#1
    levain_git = _evidence(main=[_closed("levain#1")])      # levain's git closed it
    return lines, flow_git, levain_git


def test_a_landed_dependency_in_the_other_repo_releases_its_dependent():
    lines, flow_git, levain_git = _two_repos()
    got = _states(lines, flow_git, repo="flow", other_evidence={"levain": levain_git})
    assert got["levain#1"].state == "done", got["levain#1"]
    assert got["flow#5"].state == "ready", got["flow#5"]


def test_the_same_board_under_one_repos_git_is_the_measured_wrong_answer():
    lines, flow_git, _ = _two_repos()
    got = _states(lines, flow_git)                          # no repo: flow's git judges levain#1 too
    assert got["levain#1"].state == "ready" and got["flow#5"].state == "blocked", got


def test_a_dependency_whose_repo_evidence_is_not_supplied_is_unknown_never_judged_under_the_wrong_git():
    lines, flow_git, _ = _two_repos()
    got = _states(lines, flow_git, repo="flow")
    assert got["levain#1"].state == "unknown" and "no git evidence for repo levain" in got["levain#1"].reason
    assert got["flow#5"].state == "blocked" and "levain#1" in got["flow#5"].reason


def test_the_other_repos_git_is_not_used_for_this_repos_own_rows():
    lines, flow_git, levain_git = _two_repos()
    lines.append(_add("flow#6"))
    wrong = _evidence(main=[_closed("flow#6")])             # only levain's git names flow#6: it is not flow's work
    got = _states(lines, flow_git, repo="flow", other_evidence={"levain": wrong})
    assert got["flow#6"].state == "ready", got["flow#6"]


# ------------------------------------------------------------------------------------------------ levain#73
def _orphaned(**item):
    chain = [{"ref": "queue/x/sr2", "commits": [{"sha": "d" * 40, "items": ["levain#1"]}]}]
    orphan = {"name": "seat/1114-44-storeresolver", "tip": "e" * 40, "date": "2026-11-12T11:00:00Z", "items": ["levain#1"],
              "ahead": 23}
    return _evidence(chain=chain, branches=[orphan], **item)


def test_a_closed_row_with_an_orphan_branch_beside_its_chain_reads_done():
    got = _states([_add("levain#1")], _orphaned(main=[_closed("levain#1")]))["levain#1"]
    assert got.state == "done" and "ccccccccc" in got.reason, got


def test_the_same_chain_and_orphan_without_a_closing_trailer_is_still_the_conflict():
    got = _states([_add("levain#1")], _orphaned())["levain#1"]
    assert got.state == "unknown" and "orphan branch seat/1114-44-storeresolver" in got.reason, got


def test_no_row_with_a_closing_trailer_on_main_reads_unknown_for_an_orphan_or_a_chain():
    lines = [_add(f"levain#{n}") for n in range(1, 4)]
    ev = _evidence(
        main=[{"sha": SHA, "date": "2026-11-12T12:00:00Z", "items": [], "closes": [f"levain#{n}" for n in range(1, 4)]}],
        chain=[{"ref": f"queue/x/{n}", "commits": [{"sha": "d" * 40, "items": [f"levain#{n}"]}]} for n in range(1, 4)],
        branches=[{"name": f"seat/b{n}", "tip": "e" * 40, "date": "2026-11-12T11:00:00Z", "items": [f"levain#{n}"], "ahead": 2}
                  for n in range(1, 4)])
    got = _states(lines, ev)
    assert {i: s.state for i, s in got.items()} == {"levain#1": "done", "levain#2": "done", "levain#3": "done"}


# ------------------------------------------------------------------------------------------------ levain#72 round 1 (L3)
def test_a_foreign_repos_unreadable_source_is_an_audit_line_naming_the_repo():
    lines, flow_git, _ = _two_repos()
    broken = _evidence(main=[_closed("levain#1")], unreadable={"chain": "git log failed"})
    board = bs.derive_all(bs.fold(lines), flow_git, now=NOW, repo="flow", other_evidence={"levain": broken})
    hits = [a for a in board.audit if a.kind == "unknown" and a.id is None and a.why == "levain: chain unreadable: git log failed"]
    assert len(hits) == 1, board.audit
    assert "levain: chain unreadable: git log failed" in board.problems


def test_a_foreign_repos_stale_evidence_is_an_audit_line_naming_the_repo():
    lines, flow_git, _ = _two_repos()
    old = _evidence(main=[_closed("levain#1")], fetched_at="2026-11-01T00:00:00Z")
    board = bs.derive_all(bs.fold(lines), flow_git, now=NOW, repo="flow", other_evidence={"levain": old})
    assert any(a.why.startswith("levain: stale_evidence:") for a in board.audit), board.audit


def test_a_clean_foreign_repo_adds_no_audit_line():
    lines, flow_git, levain_git = _two_repos()
    board = bs.derive_all(bs.fold(lines), flow_git, now=NOW, repo="flow", other_evidence={"levain": levain_git})
    assert board.audit == () and board.problems == (), board.audit


# ------------------------------------------------------------------------------------------------ levain#72 round 2 (L3 verify)
def _board(lines, flow_git, levain_git):
    return bs.derive_all(bs.fold(lines), flow_git, now=NOW, repo="flow", other_evidence={"levain": levain_git})


def test_a_foreign_repos_unattributed_commit_is_on_the_board_though_its_item_reads_done():
    lines, flow_git, _ = _two_repos()
    dirty = _evidence(main=[_closed("levain#1")], unattributed=[{"sha": "f" * 40, "subject": "drive-by fix"}])
    board = _board(lines, flow_git, dirty)
    assert {s.id: s.state for s in board.items}["flow#5"] == "ready"
    hits = [a for a in board.audit if a.kind == "unattributed_work"]
    assert [a.why for a in hits] == ["levain: fffffffff drive-by fix"], board.audit


def test_a_foreign_repos_unknown_trailer_id_is_on_the_board_naming_the_repo():
    lines, flow_git, _ = _two_repos()
    typo = {"sha": "a" * 40, "date": "2026-11-12T12:00:00Z", "items": ["levain#99"], "closes": []}
    board = _board(lines, flow_git, _evidence(main=[_closed("levain#1"), typo]))
    hits = [a for a in board.audit if a.kind == "unknown_id_in_trailer"]
    assert [(a.id, a.why) for a in hits] == [("levain#99", "named by levain main aaaaaaaaa; the store does not hold it")]


def test_flows_shared_seats_are_not_reported_once_per_repo():
    lines, flow_git, _ = _two_repos()
    seat = {"name": "s1", "items": ["levain#98"]}
    seats = {"seats": [seat]}
    board = _board(lines, _evidence(**seats), _evidence(main=[_closed("levain#1")], **seats))
    hits = [a for a in board.audit if a.kind == "unknown_id_in_trailer"]
    assert [a.why for a in hits] == ["named by seat s1; the store does not hold it"]


def test_a_decided_home_item_linking_an_undecided_foreign_item_leaves_that_item_off_the_board():
    decide = json.dumps({"v": 1, "op": "decide", "id": "flow#5", "at": "2026-11-12T11:00:00Z", "by": "t", "decision": "cut",
                         "words": "w", "date": "2026-11-12"})
    lines = [_add("flow#1"), _add("levain#1"), _add("flow#5", depends_on=[{"kind": "item", "ref": "levain#1", "label": "x"}]), decide]
    board = bs.derive_all(bs.fold(lines), _evidence(), now=NOW, repo="flow", other_evidence={})
    assert sorted(s.id for s in board.items) == ["flow#1", "flow#5"] and not board.audit, board


def test_an_item_a_home_item_reaches_still_reads_unknown_without_its_repos_evidence():
    lines, flow_git, _ = _two_repos()
    board = bs.derive_all(bs.fold(lines), flow_git, now=NOW, repo="flow", other_evidence={})
    assert {s.id: s.state for s in board.items}["levain#1"] == "unknown"


def test_a_foreign_git_failure_with_the_same_text_as_the_home_one_is_still_said():
    lines, flow_git, _ = _two_repos()
    home = _evidence(unreadable={"chain": "git log failed"})
    board = _board(lines, home, _evidence(main=[_closed("levain#1")], unreadable={"chain": "git log failed"}))
    assert "levain: chain unreadable: git log failed" in [a.why for a in board.audit]


def test_flows_own_gap_is_said_once_not_per_repo():
    lines, _, _ = _two_repos()
    gap = {"seats": "flow could not read the roster"}
    board = _board(lines, _evidence(unreadable=gap), _evidence(main=[_closed("levain#1")], unreadable=gap))
    assert [a.why for a in board.audit if a.id is None and "roster" in a.why] == ["seats unreadable: flow could not read the roster"]


def test_a_decided_foreign_item_carries_its_own_repos_fetch_time():
    decide = json.dumps({"v": 1, "op": "decide", "id": "levain#1", "at": "2026-11-12T11:00:00Z", "by": "t", "decision": "cut",
                         "words": "w", "date": "2026-11-12"})
    lines = [_add("levain#1"), _add("flow#5", depends_on=[{"kind": "item", "ref": "levain#1", "label": "x"}]), decide]
    mine = _evidence(fetched_at="2026-11-12T19:00:00Z")
    theirs = _evidence(fetched_at="2026-11-12T19:45:00Z")
    got = _states(lines, mine, repo="flow", other_evidence={"levain": theirs})
    assert got["levain#1"].as_of == "2026-11-12T19:45:00Z"
