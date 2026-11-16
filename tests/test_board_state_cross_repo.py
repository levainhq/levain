"""A cross-repo dependency is judged under ITS repo's evidence (flow 9 finding, 11-16).

``levain board state`` read ONE repository and applied that evidence to every item, so flow#5 waiting on levain#1 judged
levain#1 against flow's main. An item belongs to the repo its id names (``<repo>#<n>``, the row's ``repo`` field);
``derive_all(..., repo_evidence={repo: Evidence})`` reads each item, and each dependency, from the evidence of its own repo."""
from __future__ import annotations

import json

from levain.desk import board_state as bs

NOW = bs.parse_timestamp("2026-11-12T20:00:00Z")


def _item(repo: str, n: int, depends=()) -> str:
    return json.dumps({"v": 1, "op": "add", "id": f"{repo}#{n}", "at": "2026-11-12T10:00:00Z", "by": "t", "repo": repo,
                       "title": "t", "origin": "o", "done": "lands", "tier": "sonnet", "depends_on": list(depends)})


def _evidence(**kw) -> bs.Evidence:
    base = {"evidence_v": 1, "fetched_at": "2026-11-12T19:00:00Z", "unreadable": {}, "main": [], "chain": [], "refs": [],
            "merged": [], "branches": [], "seats": [], "band": {"open": {}, "resolved": {}}, "waiters": [], "documents": {},
            "unattributed": []}
    base.update(kw)
    return bs.Evidence.from_dict(base)


DEP_LANDED_ON_B = {"sha": "b" * 40, "date": "2026-11-12T12:00:00Z", "items": ["levain#1"], "closes": ["levain#1"]}
LINES = [_item("levain", 1), _item("flow", 5, [{"kind": "item", "ref": "levain#1"}])]


def _by(board):
    return {s.id: s for s in board.items}


def test_a_dependency_done_on_its_own_repo_main_is_met_for_the_dependent_in_another_repo():
    repo_ev = {"flow": _evidence(), "levain": _evidence(main=[DEP_LANDED_ON_B])}
    by = _by(bs.derive_all(bs.fold(LINES), repo_ev["flow"], now=NOW, repo_evidence=repo_ev))
    assert by["levain#1"].state == "done"
    assert by["flow#5"].state == "ready", by["flow#5"].reason


def test_the_same_board_read_from_the_dependents_repo_alone_is_the_old_wrong_answer():
    """Pins what the fix is for: one repo's evidence for every item leaves levain#1 not done (the evidence that closes it
    is in the other repo), so flow#5 reads blocked."""
    by = _by(bs.derive_all(bs.fold(LINES), _evidence(), now=NOW))
    assert by["levain#1"].state != "done" and by["flow#5"].state == "blocked"


def test_a_dependency_that_is_not_done_on_its_own_repo_still_blocks_and_the_reason_names_it():
    repo_ev = {"flow": _evidence(main=[DEP_LANDED_ON_B]), "levain": _evidence()}   # the close sits in the WRONG repo's main
    by = _by(bs.derive_all(bs.fold(LINES), repo_ev["flow"], now=NOW, repo_evidence=repo_ev))
    assert by["levain#1"].state != "done"
    assert by["flow#5"].state == "blocked" and "levain#1" in by["flow#5"].reason


def test_an_item_whose_repo_was_not_read_is_unknown_not_judged_under_another_repo():
    repo_ev = {"flow": _evidence()}                                 # levain was never read
    by = _by(bs.derive_all(bs.fold(LINES), repo_ev["flow"], now=NOW, repo_evidence=repo_ev))
    assert by["levain#1"].state == "unknown" and "levain" in by["levain#1"].reason and "not read" in by["levain#1"].reason
    assert by["flow#5"].state == "blocked"                          # waits on an unknown: errs closed, and says so


def test_each_repos_unreadable_source_is_reported_on_the_board_under_its_repo():
    repo_ev = {"flow": _evidence(), "levain": _evidence(unreadable={"main": "git failed"})}
    board = bs.derive_all(bs.fold(LINES), repo_ev["flow"], now=NOW, repo_evidence=repo_ev)
    assert any("levain" in p and "main unreadable: git failed" in p for p in board.problems)
    assert _by(board)["levain#1"].state == "unknown"


def test_without_repo_evidence_nothing_changes():
    a = bs.derive_all(bs.fold(LINES), _evidence(), now=NOW)
    b = bs.derive_all(bs.fold(LINES), _evidence(), now=NOW, repo_evidence=None)
    assert a == b


# ---- L3 round 1 (flow 9 finding, 11-16) ----

def _ctx(repo_ev):
    folded = bs.fold(LINES)
    return bs._Ctx(folded.items, repo_ev["flow"], NOW, None, repo_ev), folded


def test_resolving_a_dependency_under_its_repo_leaves_the_dependent_under_its_own():
    repo_ev = {"flow": _evidence(), "levain": _evidence(main=[DEP_LANDED_ON_B])}
    ctx, folded = _ctx(repo_ev)
    ctx.focus(folded.items["flow#5"])
    ctx.state_of("levain#1")                                        # a dependency reached mid-compute, not primed
    assert ctx.ev is repo_ev["flow"] and ctx.idx is repo_ev["flow"].index


def test_a_trailer_in_another_repos_main_naming_an_item_that_is_not_its_own_is_audited():
    stray = {"sha": "c" * 40, "date": "2026-11-12T12:00:00Z", "items": ["levain#1"], "closes": []}
    repo_ev = {"flow": _evidence(main=[stray]), "levain": _evidence()}
    board = bs.derive_all(bs.fold(LINES), repo_ev["flow"], now=NOW, repo_evidence=repo_ev, primary_repo="flow")
    hits = [a for a in board.audit if a.kind == "unknown_id_in_trailer" and a.id == "levain#1"]
    assert hits and "flow" in hits[0].why and "levain" in hits[0].why


def test_a_seat_naming_another_repos_item_is_not_a_stray_trailer():
    seat = {"name": "s", "items": ["flow#5"]}
    repo_ev = {"flow": _evidence(), "levain": _evidence(seats=[seat])}
    board = bs.derive_all(bs.fold(LINES), repo_ev["flow"], now=NOW, repo_evidence=repo_ev, primary_repo="flow")
    assert not [a for a in board.audit if a.kind == "unknown_id_in_trailer"]


def test_another_repos_unattributed_commits_reach_board_unattributed_with_the_repo():
    repo_ev = {"flow": _evidence(), "levain": _evidence(unattributed=[{"sha": "d" * 40, "subject": "stray"}])}
    board = bs.derive_all(bs.fold(LINES), repo_ev["flow"], now=NOW, repo_evidence=repo_ev, primary_repo="flow")
    assert [u.subject for u in board.unattributed] == ["levain: stray"]


def test_the_primary_is_told_by_its_name_not_by_object_identity():
    twin = _evidence(unreadable={"main": "git failed"})
    other = _evidence(unreadable={"main": "git failed"})
    repo_ev = {"flow": twin, "levain": _evidence()}
    board = bs.derive_all(bs.fold(LINES), other, now=NOW, repo_evidence=repo_ev, primary_repo="flow")
    assert sum("main unreadable" in p for p in board.problems) == 1


def test_one_shared_evidence_object_under_two_names_is_still_read_for_both():
    shared = _evidence(unreadable={"main": "git failed"})
    repo_ev = {"flow": _evidence(), "levain": shared, "other": shared}
    board = bs.derive_all(bs.fold(LINES), repo_ev["flow"], now=NOW, repo_evidence=repo_ev, primary_repo="flow")
    assert any(p.startswith("levain: main") for p in board.problems) and any(p.startswith("other: main") for p in board.problems)


def test_a_source_the_primary_reports_with_the_same_words_is_not_repeated_per_repo():
    """The flow-supplied extra lands in every repo's evidence so each item still fails closed; the board says it once."""
    u = {"seats": "roster down"}
    repo_ev = {"flow": _evidence(unreadable=u), "levain": _evidence(unreadable=u)}
    board = bs.derive_all(bs.fold(LINES), repo_ev["flow"], now=NOW, repo_evidence=repo_ev, primary_repo="flow")
    assert sum("seats unreadable" in p for p in board.problems) == 1


# ---- levain#72 review hardening (L3 rounds on the earlier isolated cut; the cores landed as a8d6182cb and c8b545539) ----

def _decide(iid: str) -> str:
    return json.dumps({"v": 1, "op": "decide", "id": iid, "at": "2026-11-12T11:00:00Z", "by": "t", "decision": "cut",
                       "words": "w", "date": "2026-11-12"})


def _both(lines, flow_ev, levain_ev):
    repo_ev = {"flow": flow_ev, "levain": levain_ev}
    return bs.derive_all(bs.fold(lines), flow_ev, now=NOW, repo_evidence=repo_ev, primary_repo="flow")


def test_a_foreign_repos_stale_evidence_is_an_audit_line_naming_the_repo():
    old = _evidence(main=[DEP_LANDED_ON_B], fetched_at="2026-11-01T00:00:00Z")
    board = _both(LINES, _evidence(), old)
    assert any(a.kind == "unknown" and a.id is None and "levain" in a.why and "stale_evidence" in a.why for a in board.audit), board.audit


def test_a_foreign_repos_unreadable_source_is_an_audit_line_not_only_a_problem():
    board = _both(LINES, _evidence(), _evidence(main=[DEP_LANDED_ON_B], unreadable={"chain": "git log failed"}))
    hits = [a for a in board.audit if a.kind == "unknown" and a.id is None]
    assert [a.why for a in hits] == ["levain: chain unreadable: git log failed"], board.audit


def test_a_clean_foreign_repo_adds_no_audit_line():
    assert _both(LINES, _evidence(), _evidence(main=[DEP_LANDED_ON_B])).audit == ()


def test_a_foreign_repos_unknown_trailer_id_is_on_the_board_naming_the_repo():
    typo = {"sha": "a" * 40, "date": "2026-11-12T12:00:00Z", "items": ["levain#99"], "closes": []}
    board = _both(LINES, _evidence(), _evidence(main=[DEP_LANDED_ON_B, typo]))
    hits = [a for a in board.audit if a.kind == "unknown_id_in_trailer"]
    assert [(a.id, a.why) for a in hits] == [("levain#99", "levain: named by main aaaaaaaaa; the store does not hold it")], board.audit


def test_a_foreign_repos_unattributed_commit_is_on_the_board_though_its_item_reads_done():
    dirty = _evidence(main=[DEP_LANDED_ON_B], unattributed=[{"sha": "f" * 40, "subject": "drive-by fix"}])
    board = _both(LINES, _evidence(), dirty)
    assert _by(board)["flow#5"].state == "ready"
    assert [a.why for a in board.audit if a.kind == "unattributed_work"] == ["levain: fffffffff drive-by fix"], board.audit


def test_flows_shared_seats_are_said_once_not_once_per_repo():
    """The flow-supplied extra (seats, waiters) lands in every repo's evidence; an unknown id it names is one finding."""
    seats = {"seats": [{"name": "s1", "items": ["levain#98"]}]}
    board = _both(LINES, _evidence(**seats), _evidence(main=[DEP_LANDED_ON_B], **seats))
    hits = [a for a in board.audit if a.kind == "unknown_id_in_trailer"]
    assert len(hits) == 1 and "seat s1" in hits[0].why, board.audit


def test_a_foreign_git_failure_with_the_same_text_as_the_home_one_is_still_said():
    """Dedup is for the flow-supplied sources, not for identical text: two repos' git can fail with the same words."""
    gap = {"chain": "git log failed"}
    board = _both(LINES, _evidence(unreadable=gap), _evidence(main=[DEP_LANDED_ON_B], unreadable=gap))
    assert "levain: chain unreadable: git log failed" in [a.why for a in board.audit], board.audit


def test_a_decided_home_item_linking_an_undecided_foreign_item_does_not_dirty_the_board():
    lines = [_item("flow", 1), _item("levain", 1), _item("flow", 5, [{"kind": "item", "ref": "levain#1"}]), _decide("flow#5")]
    board = bs.derive_all(bs.fold(lines), _evidence(), now=NOW, repo_evidence={"flow": _evidence()}, primary_repo="flow")
    assert sorted(s.id for s in board.items) == ["flow#1", "flow#5"] and board.audit == (), board


def test_an_item_a_home_item_reaches_still_reads_unknown_without_its_repos_evidence():
    board = bs.derive_all(bs.fold(LINES), _evidence(), now=NOW, repo_evidence={"flow": _evidence()}, primary_repo="flow")
    assert _by(board)["levain#1"].state == "unknown"


def test_a_repo_that_owns_no_item_leaves_every_item_unknown_not_off_the_board():
    board = bs.derive_all(bs.fold(LINES), _evidence(), now=NOW, repo_evidence={"misnamed": _evidence()}, primary_repo="misnamed")
    assert sorted(_by(board)) == ["flow#5", "levain#1"] and _by(board)["levain#1"].state == "unknown"
