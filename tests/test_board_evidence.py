"""The board's git evidence readers (slice B2) against REAL temporary git repositories.

Every test here is a world made with real git (a bare origin, a clone, commits at fixed dates): the readers are git
parsers, so a mock of git would test the mock. The contract under test is B1's: every record carries every required field
(an omitted ``items`` on an empty branch, seat or commit makes the WHOLE source unreadable), ``ahead`` is counted by the
reader by work and never by tip sha, and a source git cannot read is named, never read as empty."""
from __future__ import annotations

import os
import types
from pathlib import Path

import pytest

from levain.desk import board_evidence as be
from levain.desk.board_state import Evidence
from tests._board_git_world import DAY, OLD, World, git

SINCE = "2026-11-12T00:00:00Z"


@pytest.fixture
def world(tmp_path: Path) -> World:
    w = World(tmp_path)
    w.commit("root", date=OLD)
    w.push_main()
    return w


def read(w: World, **kw):
    return be.read_git_evidence(w.w, repo_name="levain", since=SINCE, **kw)


def parsed(ev: dict) -> Evidence:
    """The evidence through B1's own reader: the proof that every required field is there."""
    return Evidence.from_dict({**ev, "seats": [], "band": {"open": {}, "resolved": {}}, "waiters": [], "documents": {}})


def branch(ev: dict, name: str) -> dict:
    return next(b for b in ev["branches"] if b["name"] == name)


# ------------------------------------------------------------------------------------------------ the empty world
def test_an_empty_world_emits_every_source_as_an_empty_list_and_B1_reads_all_of_it(world):
    ev, summary = read(world)
    for source in ("main", "chain", "branches", "unattributed", "refs", "merged"):
        assert ev[source] == [] or source in ("refs", "merged"), source
    assert ev["unreadable"] == {}
    assert parsed(ev).unreadable == {}                  # not one source reads malformed or "not supplied" beyond flow's own
    assert summary["main_ref"] == "origin/main"


def test_only_flows_sources_and_the_stat_d_documents_are_left_to_the_cli_and_the_extra(world):
    ev, _ = read(world)
    p = Evidence.from_dict(ev)
    assert set(p.unreadable) == {"seats", "band", "waiters", "documents"}      # the readers supply the rest


# ------------------------------------------------------------------------------------------------ main
def test_main_records_carry_both_item_lists_even_when_one_is_empty(world):
    sha_i = world.commit("work", trailers=("Board-Item: levain#1",))
    sha_c = world.commit("close", trailers=("Board-Closes: levain#2",))
    sha_b = world.commit("both", trailers=("Board-Item: levain#3", "Board-Item: levain#4", "Board-Closes: levain#3"))
    world.commit("plain")
    world.push_main()
    ev, _ = read(world)
    rows = {r["sha"]: r for r in ev["main"]}
    assert rows[sha_i]["items"] == ["levain#1"] and rows[sha_i]["closes"] == []
    assert rows[sha_c]["items"] == [] and rows[sha_c]["closes"] == ["levain#2"]
    assert rows[sha_b]["items"] == ["levain#3", "levain#4"] and rows[sha_b]["closes"] == ["levain#3"]
    assert len(rows) == 3                                # the plain commit is no record
    assert all(r["date"].endswith("Z") for r in rows.values())
    assert parsed(ev).unreadable == {}


def test_a_note_on_refs_notes_board_is_read_like_a_trailer(world):
    sha = world.commit("noted")
    git(world.w, "notes", "--ref=board", "add", "-m", "Board-Item: levain#9\nBoard-Closes: levain#9", sha)
    world.push_main()
    ev, _ = read(world)
    assert [(r["sha"], r["items"], r["closes"]) for r in ev["main"]] == [(sha, ["levain#9"], ["levain#9"])]


def test_none_is_attributed_but_names_no_item(world):
    world.commit("tooling", trailers=("Board-Item: none - a release chore",))
    world.push_main()
    ev, _ = read(world)
    assert ev["main"] == [] and ev["unattributed"] == []


def test_a_trailer_in_the_body_is_not_a_trailer(world):
    world.commit("prose", body="x\n")
    git(world.w, "commit", "-q", "--amend", "-m", "prose\n\nBoard-Item: levain#5 appears first in this line of prose\n\nmore words here", date=DAY)
    world.push_main()
    ev, _ = read(world)
    assert ev["main"] == []                              # not the last paragraph: git's parser does not call it a trailer


# ------------------------------------------------------------------------------------------------ fetch time
def test_fetched_at_is_the_fetch_head_time_when_the_fetch_carried_main(world):
    ev, _ = read(world)
    assert ev["fetched_at"].endswith("Z")
    stamp = os.stat(world.w / ".git" / "FETCH_HEAD").st_mtime
    from datetime import datetime
    assert abs(datetime.fromisoformat(ev["fetched_at"].replace("Z", "+00:00")).timestamp() - stamp) < 2


def test_a_fetch_of_another_branch_does_not_refresh_main(world):
    git(world.w, "push", "-q", "origin", "main:other")
    os.remove(world.w / ".git" / "logs" / "refs" / "remotes" / "origin" / "main")     # the other witness gone
    git(world.w, "fetch", "-q", "origin", "other")                                       # FETCH_HEAD rewritten, without main
    assert "branch 'main' of" not in (world.w / ".git" / "FETCH_HEAD").read_text()
    ev, _ = read(world)
    assert "fetched_at" not in ev                       # a fresh FETCH_HEAD that never carried main says nothing about main


def test_the_reflog_of_the_remote_main_is_the_fallback_witness_and_it_is_the_entry_time_not_the_commit_time(tmp_path):
    w = World(tmp_path)
    w.commit("root", date=OLD)
    git(w.w, "push", "-q", "origin", "main")
    git(w.w, "update-ref", "refs/remotes/origin/main", git(w.w, "rev-parse", "HEAD"))
    if (w.w / ".git" / "FETCH_HEAD").exists():
        os.remove(w.w / ".git" / "FETCH_HEAD")
    ev, _ = read(w)
    assert ev["fetched_at"].startswith("20") and ev["fetched_at"] > "2026-11-01"      # the commit is dated 2026-10-01


def test_no_witness_means_no_fetch_time(tmp_path):
    w = World(tmp_path)
    w.commit("root", date=OLD)
    git(w.w, "push", "-q", "origin", "main")
    git(w.w, "-c", "core.logAllRefUpdates=false", "update-ref", "refs/remotes/origin/main", git(w.w, "rev-parse", "HEAD"))
    for stale in (w.w / ".git" / "FETCH_HEAD", w.w / ".git" / "logs" / "refs" / "remotes" / "origin" / "main"):
        if stale.exists():
            os.remove(stale)
    ev, _ = read(w)
    assert "fetched_at" not in ev                       # derive reads "no fetch time" as unknown


# ------------------------------------------------------------------------------------------------ chain and refs
def test_a_live_queue_ref_is_a_chain_row_with_every_commit_and_its_items(world):
    world.branch("queue/a/one")
    s1 = world.commit("c1", trailers=("Board-Item: levain#1",))
    s2 = world.commit("c2")                                              # a chain commit naming nothing
    world.push("queue/a/one")
    ev, _ = read(world)
    (row,) = ev["chain"]
    assert row["ref"] == "queue/a/one" and row["tip"] == s2
    assert row["commits"] == [{"sha": s2, "items": []}, {"sha": s1, "items": ["levain#1"]}]
    assert "queue/a/one" in ev["refs"] and "queue/a/one" not in ev["merged"]
    assert parsed(ev).unreadable == {}


def test_a_queue_ref_already_on_main_is_merged_not_chain(world):
    world.branch("queue/a/done")
    world.commit("c1", trailers=("Board-Item: levain#1",))
    world.push("queue/a/done")
    git(world.w, "checkout", "-q", "main")
    git(world.w, "merge", "-q", "--ff-only", "queue/a/done")
    world.push_main()
    ev, _ = read(world)
    assert ev["chain"] == [] and "queue/a/done" in ev["merged"]


def test_a_chain_ref_whose_work_main_holds_by_patch_id_is_not_live(world):
    world.branch("queue/a/old")
    s = world.commit("c1", trailers=("Board-Item: levain#1",))
    world.push("queue/a/old")
    git(world.w, "checkout", "-q", "main")
    world.cherry_pick(s)                                                 # same work, a new sha
    world.push_main()
    ev, _ = read(world)
    assert ev["chain"] == []                                             # beyond main by sha, on main by work


def test_a_queue_ref_older_than_the_epoch_is_not_read(world):
    world.branch("queue/a/ancient")
    world.commit("c1", date=OLD)
    world.push("queue/a/ancient")
    ev, _ = read(world)
    assert ev["chain"] == []


# ------------------------------------------------------------------------------------------------ branches
def test_an_empty_token_branch_is_emitted_with_items_and_ahead_zero(world):
    world.branch("seat/1112-40-i7-slug")
    world.push("seat/1112-40-i7-slug")
    ev, _ = read(world)
    # an old tip would be filtered; the branch tip IS main's tip (dated OLD), so it is filtered by the epoch
    assert ev["branches"] == []
    git(world.w, "checkout", "-q", "main")
    world.commit("w", date=DAY)
    world.push_main()
    world.branch("seat/1112-40-i7-slug", "main")
    git(world.w, "push", "-q", "-f", "origin", "main:seat/1112-40-i7-slug")
    git(world.w, "fetch", "-q", "origin")
    ev, _ = read(world)
    b = branch(ev, "seat/1112-40-i7-slug")
    assert b == {"name": "seat/1112-40-i7-slug", "tip": b["tip"], "date": b["date"], "ahead": 0, "items": ["levain#7"]}
    assert parsed(ev).unreadable == {}                   # the empty-branch shape B1 requires


def test_a_branch_with_own_commits_counts_them_and_unions_token_and_trailer_items(world):
    world.branch("seat/1112-40-i7-a")
    world.commit("a1", trailers=("Board-Item: levain#7",))
    world.commit("a2", trailers=("Board-Item: levain#8",))
    world.commit("a3")
    ev, _ = read(world)
    b = branch(ev, "seat/1112-40-i7-a")
    assert b["ahead"] == 3 and b["items"] == ["levain#7", "levain#8"]
    assert ev["unattributed"] == []                      # a token branch is attributed whole


def test_ahead_is_by_work_not_by_tip_sha_a_cherry_picked_branch_is_not_an_orphan(world):
    world.branch("seat/1112-40-i7-a")
    s1 = world.commit("a1", trailers=("Board-Item: levain#7",), file="a.txt", body="alpha\n")
    s2 = world.commit("a2", trailers=("Board-Item: levain#7",), file="b.txt", body="beta\n")
    git(world.w, "checkout", "-q", "main")
    world.cherry_pick(s1)
    world.cherry_pick(s2)
    world.push_main()
    ev, _ = read(world)
    b = branch(ev, "seat/1112-40-i7-a")
    assert b["ahead"] == 0                               # B1 L2 MED-4: a replayed branch has ahead 0, not 2
    assert b["tip"] == s2 and b["tip"] != git(world.w, "rev-parse", "origin/main")


def test_a_branch_half_landed_counts_only_the_unlanded_commit(world):
    world.branch("seat/1112-40-i7-a")
    s1 = world.commit("a1", trailers=("Board-Item: levain#7",), file="a.txt", body="alpha\n")
    world.commit("a2", trailers=("Board-Item: levain#7",), file="b.txt", body="beta\n")
    git(world.w, "checkout", "-q", "main")
    world.cherry_pick(s1)
    world.push_main()
    assert branch(read(world)[0], "seat/1112-40-i7-a")["ahead"] == 1


def test_a_cherry_pick_is_landed_by_patch_id_alone_with_no_trailer_to_fall_back_on(world):
    world.branch("seat/1112-40-i7-a")
    s = world.commit("untrailed work", file="a.txt", body="alpha\n")          # the token attributes it; no trailer, so no key either
    git(world.w, "checkout", "-q", "main")
    world.cherry_pick(s)
    world.push_main()
    assert branch(read(world)[0], "seat/1112-40-i7-a")["ahead"] == 0


def test_the_same_trailer_and_subject_with_a_different_diff_is_NOT_landed(world):
    """B2 L1 F2 / L2 MED-1: item id + subject alone is no proof of landed work (a seat that reuses a subject for a
    follow-up, or an amended commit, would hide an orphan, and the board would say 'close it')."""
    world.branch("seat/1112-40-i7-a")
    world.commit("the work", trailers=("Board-Item: levain#7",), file="a.txt", body="alpha\n")
    git(world.w, "checkout", "-q", "main")
    world.commit("the work", trailers=("Board-Item: levain#7",), file="z.txt", body="different diff\n")   # patch-id differs
    world.push_main()
    assert branch(read(world)[0], "seat/1112-40-i7-a")["ahead"] == 1


def test_a_second_round_that_reuses_the_subject_is_ahead_not_absorbed_by_the_first_landing(world):
    """L1 E2: main holds the first round (cherry-picked); round two reuses the subject on the same item with a new file."""
    world.branch("seat/1112-40-i7-a")
    r1 = world.commit("address review findings", trailers=("Board-Item: levain#7",), file="a.txt", body="one\n")
    world.commit("address review findings", trailers=("Board-Item: levain#7",), file="b.txt", body="two\n")
    git(world.w, "checkout", "-q", "main")
    world.cherry_pick(r1)
    world.push_main()
    assert branch(read(world)[0], "seat/1112-40-i7-a")["ahead"] == 1


def test_an_empty_diff_commit_has_no_patch_id_so_it_matches_by_trailer_ids_and_subject(world):
    """The one place the key still decides: a commit with NO diff has no patch-id to compare; its landing is recorded."""
    world.branch("seat/1112-40-i7-a")
    world.empty_commit("marker", trailers=("Board-Item: levain#7",))
    git(world.w, "checkout", "-q", "main")
    world.empty_commit("marker", trailers=("Board-Item: levain#7",), date="2026-11-12T13:00:00Z")
    world.push_main()
    ev, summary = read(world)
    assert branch(ev, "seat/1112-40-i7-a")["ahead"] == 0
    assert summary["matched_by_key"] == 1               # said out loud, not silent


def test_each_main_commit_accounts_for_at_most_one_branch_commit_by_key_too(world):
    world.branch("seat/1112-40-i7-a")
    world.empty_commit("marker", trailers=("Board-Item: levain#7",))
    world.empty_commit("marker", trailers=("Board-Item: levain#7",))
    git(world.w, "checkout", "-q", "main")
    world.empty_commit("marker", trailers=("Board-Item: levain#7",), date="2026-11-12T13:00:00Z")
    world.push_main()
    assert branch(read(world)[0], "seat/1112-40-i7-a")["ahead"] == 1


def test_each_main_commit_accounts_for_at_most_one_branch_commit_by_patch_id(world):
    """c1 adds f.txt, c2 reverts it, c3 adds the identical f.txt again (same patch-id as c1). Main holds c1 once."""
    world.branch("seat/1112-40-i7-a")
    c1 = world.commit("add", trailers=("Board-Item: levain#7",), file="f.txt", body="alpha\n")
    git(world.w, "revert", "--no-edit", "HEAD", date=DAY)
    world.commit("add again", trailers=("Board-Item: levain#7",), file="f.txt", body="alpha\n")
    git(world.w, "checkout", "-q", "main")
    world.cherry_pick(c1)
    world.push_main()
    assert branch(read(world)[0], "seat/1112-40-i7-a")["ahead"] == 2    # the revert, and the second add: main paid for one


def test_a_revert_then_a_reland_of_the_identical_patch_is_ahead(world):
    """L2 MED-2 (its revert.py): X lands on main, is reverted, and the branch re-forked from main re-applies X."""
    world.branch("seat/1112-40-i7-a")
    x = world.commit("feature", trailers=("Board-Item: levain#7",), file="feature.txt", body="feature\n")
    git(world.w, "checkout", "-q", "main")
    world.cherry_pick(x)
    git(world.w, "revert", "--no-edit", "HEAD", date="2026-11-12T14:00:00Z")
    world.push_main()
    world.branch("seat/1112-40-i7-a", "origin/main")
    git(world.w, "cherry-pick", x, date="2026-11-12T15:00:00Z")
    git(world.w, "push", "-q", "-f", "origin", "seat/1112-40-i7-a:seat/1112-40-i7-a")
    git(world.w, "fetch", "-q", "origin")
    with pytest.raises(AssertionError):                  # the premise: the work is NOT on origin/main (the revert took it out)
        git(world.w, "cat-file", "-e", "origin/main:feature.txt")
    assert branch(read(world)[0], "seat/1112-40-i7-a")["ahead"] == 1


def test_a_branch_forked_before_the_revert_does_not_count_the_reverted_landing(world):
    world.branch("seat/1112-40-i7-a")
    x = world.commit("feature", trailers=("Board-Item: levain#7",), file="feature.txt", body="feature\n")
    world.push("seat/1112-40-i7-a")
    git(world.w, "checkout", "-q", "main")
    world.cherry_pick(x)
    git(world.w, "revert", "--no-edit", "HEAD", date="2026-11-12T14:00:00Z")
    world.push_main()
    assert branch(read(world)[0], "seat/1112-40-i7-a")["ahead"] == 1       # the landing is in main's history but not in effect


def test_a_revert_of_the_revert_puts_the_landing_back_in_effect(world):
    world.branch("seat/1112-40-i7-a")
    x = world.commit("feature", trailers=("Board-Item: levain#7",), file="feature.txt", body="feature\n")
    world.push("seat/1112-40-i7-a")
    git(world.w, "checkout", "-q", "main")
    world.cherry_pick(x)
    git(world.w, "revert", "--no-edit", "HEAD", date="2026-11-12T14:00:00Z")
    git(world.w, "revert", "--no-edit", "HEAD", date="2026-11-12T15:00:00Z")
    world.push_main()
    assert branch(read(world)[0], "seat/1112-40-i7-a")["ahead"] == 0


def test_a_real_squash_merge_of_two_commits_leaves_both_ahead_like_git_cherry(world):
    """Known edge, pinned: a squash has ONE combined diff, which no single branch commit matches. git cherry says 2 as well."""
    world.branch("seat/1112-40-i7-a")
    world.commit("a1", trailers=("Board-Item: levain#7",), file="a.txt", body="alpha\n")
    world.commit("a2", trailers=("Board-Item: levain#7",), file="b.txt", body="beta\n")
    world.push("seat/1112-40-i7-a")
    git(world.w, "checkout", "-q", "main")
    git(world.w, "merge", "-q", "--squash", "origin/seat/1112-40-i7-a")
    git(world.w, "commit", "-q", "-m", "squashed\n\nBoard-Item: levain#7", date=DAY)
    world.push_main()
    cherry = git(world.w, "cherry", "origin/main", "origin/seat/1112-40-i7-a").splitlines()
    assert [line[0] for line in cherry] == ["+", "+"]
    assert branch(read(world)[0], "seat/1112-40-i7-a")["ahead"] == 2


def test_commits_already_in_the_chain_are_not_ahead_by_sha_or_by_work(world):
    world.branch("queue/a/one")
    s1 = world.commit("c1", trailers=("Board-Item: levain#7",), file="a.txt", body="alpha\n")
    world.push("queue/a/one")
    world.branch("seat/1112-40-i7-a", "queue/a/one")          # a lane stacked on the chain tip
    world.commit("own", trailers=("Board-Item: levain#7",), file="b.txt", body="beta\n")
    world.branch("seat/1112-40-i7-b")
    git(world.w, "cherry-pick", s1, date=DAY)                 # a re-chain of the same work on a lane
    ev, _ = read(world)
    assert branch(ev, "seat/1112-40-i7-a")["ahead"] == 1
    assert branch(ev, "seat/1112-40-i7-b")["ahead"] == 0
    assert [r["ref"] for r in ev["chain"]] == ["queue/a/one"]


def test_a_branch_is_attributed_by_its_trailers_without_a_token(world):
    world.branch("seat/1112-40-plainname")
    world.commit("a1", trailers=("Board-Item: levain#12",))
    world.commit("a2")                                         # no trailer, no token: unattributed work
    ev, _ = read(world)
    assert branch(ev, "seat/1112-40-plainname")["items"] == ["levain#12"]
    assert len(ev["unattributed"]) == 1 and ev["unattributed"][0]["subject"] == "a2"


def test_a_branch_naming_nothing_is_no_record_and_its_commits_are_unattributed(world):
    world.branch("seat/1112-40-nothing")
    s = world.commit("lone")
    ev, _ = read(world)
    assert ev["branches"] == []
    assert ev["unattributed"] == [{"sha": s, "subject": "lone"}]


def test_a_branch_whose_tip_is_older_than_the_epoch_is_not_read(world):
    world.branch("seat/1112-40-i3-old")
    world.commit("o", trailers=("Board-Item: levain#3",), date=OLD)
    assert read(world)[0]["branches"] == []


def test_a_local_head_wins_over_its_remote_copy(world):
    world.branch("seat/1112-40-i7-a")
    world.commit("a1", trailers=("Board-Item: levain#7",))
    world.push("seat/1112-40-i7-a")
    local = world.commit("a2", trailers=("Board-Item: levain#7",))     # unpushed
    b = branch(read(world)[0], "seat/1112-40-i7-a")
    assert b["tip"] == local and b["ahead"] == 2


def test_the_token_parses_the_fixed_i_n_token_only(world):
    for name in ("seat/1112-40-i12-slug", "seat/1112-40-i0007-slug", "seat/1112-40-ui-slug", "seat/1112-40-i18n-slug",
                 "seat/1112-40-board-b2"):
        world.branch(name)
        world.commit(f"c {name}", trailers=("Board-Item: none - probe",))
    ev, _ = read(world)
    names = {b["name"]: b["items"] for b in ev["branches"]}
    assert names == {"seat/1112-40-i12-slug": ["levain#12"], "seat/1112-40-i0007-slug": ["levain#7"]}


# ------------------------------------------------------------------------------------------------ unattributed
def test_unattributed_is_main_since_the_epoch_without_trailer_or_none_and_never_a_merge(world):
    old = world.commit("ancient", date=OLD)
    margin = world.commit("inside the one-day margin, before the epoch", date="2026-11-11T12:00:00Z")
    bad = world.commit("untrailed")
    world.commit("chore", trailers=("Board-Item: none - cleanup",))
    world.commit("named", trailers=("Board-Item: levain#1",))
    git(world.w, "checkout", "-q", "-b", "side", "HEAD~1")
    world.commit("side")
    git(world.w, "checkout", "-q", "main")
    git(world.w, "merge", "-q", "--no-ff", "-m", "merge side", "side", date=DAY)
    world.push_main()
    ev, _ = read(world)
    subjects = sorted(u["subject"] for u in ev["unattributed"])
    assert subjects == ["side", "untrailed"]
    assert {old, margin}.isdisjoint({u["sha"] for u in ev["unattributed"]}) and bad in {u["sha"] for u in ev["unattributed"]}


# ------------------------------------------------------------------------------------------------ failure
def test_a_path_that_is_not_a_repository_names_every_source_unreadable_and_emits_none(tmp_path):
    ev, _ = be.read_git_evidence(tmp_path / "nowhere", repo_name="levain", since=SINCE)
    assert set(ev["unreadable"]) == {"main", "chain", "branches", "refs", "unattributed"}
    for source in ("main", "chain", "branches", "refs", "unattributed", "merged", "fetched_at"):
        assert source not in ev
    p = Evidence.from_dict(ev)
    assert p.unreadable_reason("main") and "cannot be read" in p.unreadable_reason("main")


def test_a_repo_without_origin_main_is_unreadable_not_empty(tmp_path):
    w = World(tmp_path)
    w.commit("root", date=OLD)                                            # never pushed: no origin/main
    ev, _ = be.read_git_evidence(w.w, repo_name="levain", since=SINCE)
    assert "origin/main cannot be read" in ev["unreadable"]["main"] and "main" not in ev


def test_a_since_that_is_not_a_timestamp_fails_closed(world):
    ev, _ = be.read_git_evidence(world.w, repo_name="levain", since="last week")
    assert set(ev["unreadable"]) == {"main", "chain", "branches", "refs", "unattributed"}


def test_the_chain_failing_takes_branches_and_unattributed_with_it(world, monkeypatch):
    real = be._git

    def boom(repo, *args, **kw):
        if args[:1] == ("diff-tree",):
            raise be.GitFailed("git diff-tree failed: simulated")
        return real(repo, *args, **kw)

    world.branch("queue/a/one")
    world.commit("c1")
    world.push("queue/a/one")
    monkeypatch.setattr(be, "_git", boom)
    ev, _ = read(world)
    assert {"chain", "branches", "unattributed"} <= set(ev["unreadable"]) and "main" not in ev["unreadable"]
    assert "chain" not in ev and "branches" not in ev and "unattributed" not in ev and ev["main"] == []


def test_the_reader_writes_nothing(world):
    world.branch("seat/1112-40-i7-a")
    world.commit("a1", trailers=("Board-Item: levain#7",))
    git(world.w, "notes", "--ref=board", "add", "-m", "Board-Item: levain#7", "HEAD")

    def snapshot() -> list:
        out = []
        for base, _dirs, files in os.walk(world.w / ".git"):
            for f in files:
                p = Path(base) / f
                st = p.stat()
                out.append((str(p), st.st_size, st.st_mtime_ns))
        return sorted(out)

    before = snapshot()
    read(world)
    be.read_documents({}, world.w)
    assert snapshot() == before                          # not a ref, not the index, not FETCH_HEAD, not an object


# ------------------------------------------------------------------------------------------------ documents
class _I:
    def __init__(self, kind, done):
        self.done_kind, self.done = kind, done


def test_read_documents_stats_only_document_and_run_items(tmp_path):
    (tmp_path / "full.md").write_text("x")
    (tmp_path / "empty.md").write_text("")
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "f").write_text("x")
    (tmp_path / "emptydir").mkdir()
    items = {"a": _I("document", "full.md"), "b": _I("document", "empty.md"), "c": _I("run", "missing.md"),
             "d": _I("document", "dir"), "e": _I("document", "emptydir"), "f": _I("merged", "full.md"),
             "g": _I("document", None), "h": _I("band", "np-1"), "i": _I("document", str(tmp_path / "full.md"))}
    assert be.read_documents(items, tmp_path) == {"full.md": True, "empty.md": False, "missing.md": False, "dir": True,
                                                   "emptydir": False, str(tmp_path / "full.md"): True}


def test_read_documents_leaves_out_a_path_it_cannot_examine(tmp_path, monkeypatch):
    (tmp_path / "x.md").write_text("x")

    def deny(self):
        raise PermissionError("no")

    monkeypatch.setattr(Path, "is_dir", deny)
    assert be.read_documents({"a": _I("document", "x.md")}, tmp_path) == {}       # absent key: the item reads unknown, not not-done


# ------------------------------------------------------------------------------------------------ fully qualified refs
def test_a_tag_or_a_branch_named_origin_main_cannot_swap_mains_evidence(world):
    """B2 L1 F1 / L2 LOW-5: `origin/main` by short name resolves a TAG first; the closing commit vanished, silently."""
    root = git(world.w, "rev-parse", "origin/main")
    world.commit("done", trailers=("Board-Item: levain#5", "Board-Closes: levain#5"))
    world.push_main()
    before = read(world)[0]["main"]
    assert [r["closes"] for r in before] == [["levain#5"]]
    git(world.w, "tag", "origin/main", root)
    assert read(world)[0]["main"] == before
    git(world.w, "tag", "-d", "origin/main")
    git(world.w, "branch", "origin/main", root)
    ev, _ = read(world)
    assert ev["main"] == before and ev["unreadable"] == {}


def test_a_ref_that_does_not_resolve_fully_qualified_is_unreadable_not_the_short_name_winner(tmp_path):
    w = World(tmp_path)
    root = w.commit("root", date=OLD)
    git(w.w, "tag", "origin/main", root)                  # a tag by that name, but no refs/remotes/origin/main at all
    ev, _ = be.read_git_evidence(w.w, repo_name="levain", since=SINCE)
    assert "origin/main cannot be read" in ev["unreadable"]["main"] and "main" not in ev


def test_every_revision_the_log_is_given_is_a_sha_resolved_once_at_the_start(world, monkeypatch):
    world.branch("queue/a/one")
    world.commit("c1", trailers=("Board-Item: levain#1",))
    world.push("queue/a/one")
    world.branch("seat/1112-40-i7-a", "queue/a/one")
    world.commit("own", trailers=("Board-Item: levain#7",))
    seen: list[str] = []
    real = be._git

    def spy(repo, *args, **kw):
        if args[:1] == ("log",):
            seen.extend((kw.get("stdin") or b"").decode().split())
        return real(repo, *args, **kw)

    monkeypatch.setattr(be, "_git", spy)
    read(world)
    assert seen and all(len(r.lstrip("^")) == 40 for r in seen), seen        # shas only: refs are resolved once, at the start
    assert f"^{git(world.w, 'rev-parse', 'refs/remotes/origin/main')}" in seen
    assert f"^{git(world.w, 'rev-parse', 'refs/remotes/origin/queue/a/one')}" in seen


# ------------------------------------------------------------------------------------------------ the local head and origin's copy
def _three_commit_branch(world, name="seat/1112-40-i7-a"):
    world.branch(name)
    a1 = world.commit("a1", trailers=("Board-Item: levain#7",), file="a.txt", body="alpha\n")
    world.commit("a2", trailers=("Board-Item: levain#7",), file="b.txt", body="beta\n")
    a3 = world.commit("a3", trailers=("Board-Item: levain#7",), file="c.txt", body="gamma\n")
    world.push(name)
    return a1, a3


def test_a_stale_local_head_does_not_override_the_newer_remote_tip(world):
    """B2 L2 MED-3: the local head was left at the first commit, which was cherry-picked; origin holds two more."""
    name = "seat/1112-40-i7-a"
    a1, a3 = _three_commit_branch(world)
    git(world.w, "checkout", "-q", "-B", name, a1)                       # the local head is behind origin's copy
    git(world.w, "checkout", "-q", "main")
    world.cherry_pick(a1)
    world.push_main()
    ev, summary = read(world)
    b = branch(ev, name)
    assert b["ahead"] == 2 and b["tip"] == a3 and summary["diverged"] == []


def test_a_local_head_and_an_origin_copy_that_diverged_are_both_read_and_named(world):
    name = "seat/1112-40-i7-a"
    a1, _a3 = _three_commit_branch(world)
    git(world.w, "checkout", "-q", "-B", name, a1)
    local = world.commit("local only", trailers=("Board-Item: levain#7",), file="l.txt", body="local\n")
    ev, summary = read(world)
    b = branch(ev, name)
    assert b["ahead"] == 4                                              # a1..a3 on origin, plus the local commit: the union
    assert summary["diverged"] == [name] and b["tip"] in (local, git(world.w, "rev-parse", f"origin/{name}"))


# ------------------------------------------------------------------------------------------------ cut history
def test_a_shallow_clone_names_every_source_unreadable(world, tmp_path):
    """B2 L1 F5 / L2 LOW-3: in a --depth 1 clone a branch fully on main read `ahead` 1 and nothing said so."""
    world.commit("w1", trailers=("Board-Item: levain#1",))
    world.commit("w2", trailers=("Board-Item: levain#1",))
    world.push_main()
    world.branch("seat/1112-40-i7-a", "main~1")
    git(world.w, "push", "-q", "origin", "seat/1112-40-i7-a")
    clone = world.clone(tmp_path / "shallow", "--depth", "1", "--no-single-branch")
    assert git(clone, "rev-parse", "--is-shallow-repository") == "true"
    ev, _ = be.read_git_evidence(clone, repo_name="levain", since=SINCE)
    assert set(ev["unreadable"]) == {"main", "chain", "branches", "refs", "unattributed"}
    assert all("shallow clone" in why for why in ev["unreadable"].values())
    for source in ("main", "chain", "branches", "unattributed"):
        assert source not in ev


def test_a_partial_clone_is_unreadable_and_the_reader_fetches_nothing(world, tmp_path):
    """B2 L2 LOW-4: `diff-tree -p` in a blob:none clone went to the promisor remote and wrote a pack."""
    git(world.origin, "config", "uploadpack.allowFilter", "true")
    git(world.origin, "config", "uploadpack.allowAnySHA1InWant", "true")
    world.commit("w1", trailers=("Board-Item: levain#1",), file="a.txt", body="alpha\n")
    world.push_main()
    clone = world.clone(tmp_path / "partial", "--filter=blob:none", "--no-checkout", "--no-single-branch")
    packs = git(clone, "count-objects", "-v")
    ev, _ = be.read_git_evidence(clone, repo_name="levain", since=SINCE)
    assert all("partial clone" in why for why in ev["unreadable"].values()) and "main" in ev["unreadable"]
    assert git(clone, "count-objects", "-v") == packs                    # nothing came over the wire, nothing was written


def test_the_reader_forbids_lazy_fetches(monkeypatch):
    env = be._env()
    assert env["GIT_NO_LAZY_FETCH"] == "1" and env["GIT_ALLOW_PROTOCOL"] == "" and env["GIT_PROTOCOL_FROM_USER"] == "0"


# ------------------------------------------------------------------------------------------------ a count window, a deadline
def test_a_commit_written_with_a_wrong_clock_does_not_hide_the_history_behind_it(world):
    """B2 L2 LOW-1: `--since` stops at the first old-dated commit; main's earlier close was dropped, nothing unreadable."""
    world.commit("m1 close", trailers=("Board-Closes: levain#1",), date="2026-11-12T10:00:00Z")
    world.commit("m2 skewed", date="2026-10-01T10:00:00Z")
    world.commit("m3", trailers=("Board-Item: levain#3",), date="2026-11-12T12:00:00Z")
    world.commit("m4", trailers=("Board-Item: levain#4",), date="2026-11-12T13:00:00Z")
    world.push_main()
    ev, _ = read(world)
    assert sorted(i for r in ev["main"] for i in r["closes"] + r["items"]) == ["levain#1", "levain#3", "levain#4"]


def test_a_main_window_that_reaches_its_count_while_still_inside_the_epoch_is_unreadable(world, monkeypatch):
    monkeypatch.setattr(be, "MAX_MAIN_COMMITS", 3)
    for i in range(4):
        world.commit(f"w{i}", trailers=("Board-Item: none - chore",))
    world.push_main()
    ev, _ = read(world)
    assert "more than 3 commits" in ev["unreadable"]["main"] and "main" not in ev
    assert {"chain", "branches", "unattributed"} <= set(ev["unreadable"])


def test_a_history_of_exactly_the_count_is_a_complete_window(world, monkeypatch):
    monkeypatch.setattr(be, "MAX_MAIN_COMMITS", 3)
    world.commit("old2", date=OLD)
    world.commit("new", trailers=("Board-Item: levain#1",))
    world.push_main()                                    # root (OLD), old2 (OLD), new: exactly 3 commits, none cut off
    ev, _ = read(world)
    assert ev["unreadable"] == {} and [r["items"] for r in ev["main"]] == [["levain#1"]]


def test_the_overall_deadline_marks_the_remaining_stages_unreadable_not_partial(world, monkeypatch):
    world.branch("seat/1112-40-i7-a")
    world.commit("a1", trailers=("Board-Item: levain#7",))
    clock = {"t": 0.0}
    monkeypatch.setattr(be, "time", types.SimpleNamespace(monotonic=lambda: clock["t"]))
    real = be._patch_ids

    def late(repo, shas):
        clock["t"] = float(be.READ_DEADLINE_S) + 1.0     # the chain stage runs out of time as it starts its patch-id pass
        return real(repo, shas)

    monkeypatch.setattr(be, "_patch_ids", late)
    ev, _ = read(world)
    assert "deadline" in ev["unreadable"]["chain"] and {"chain", "branches", "unattributed"} <= set(ev["unreadable"])
    assert "main" not in ev["unreadable"] and "main" in ev           # what finished before the deadline is whole and stays


def test_a_deadline_already_past_reads_nothing_not_something(world, monkeypatch):
    monkeypatch.setattr(be, "READ_DEADLINE_S", -1)
    ev, _ = read(world)
    assert set(ev["unreadable"]) == {"main", "chain", "branches", "refs", "unattributed"}
    assert all("deadline" in why for why in ev["unreadable"].values())


# ------------------------------------------------------------------------------------------------ the trailer block
def test_board_item_above_the_attribution_paragraph_is_not_a_trailer(world):
    """B2 L2 MED-4, the contract line (docs/ready-schema-v2.md 6a): git reads only the LAST paragraph as trailers."""
    msg = ("work\n\nsome body\nBoard-Item: levain#18\n\n"
           "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>\nClaude-Session: https://claude.ai/code/session_x")
    sha = world.commit_raw(msg, file="a.txt")
    good = ("work two\n\nsome body\n\nBoard-Item: levain#19\n"
            "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>\nClaude-Session: https://claude.ai/code/session_x")
    ok = world.commit_raw(good, file="b.txt")
    world.push_main()
    ev, _ = read(world)
    assert [r["items"] for r in ev["main"]] == [["levain#19"]]            # in the same final paragraph as the attribution: counted
    assert [u["sha"] for u in ev["unattributed"]] == [sha] and ok not in {u["sha"] for u in ev["unattributed"]}


# ------------------------------------------------------------------------------------------------ the merge-base, and spending a pool once per branch
def test_a_landing_removed_by_hand_then_re_applied_on_a_fresh_fork_is_ahead(world):
    """Only commits AFTER the branch's merge-base are compared (git cherry's upstream side): X' is in this branch's own
    history, so it cannot account for the branch's re-application, even though no `This reverts` line says it was undone."""
    world.branch("seat/1112-40-i7-a")
    x = world.commit("feature", trailers=("Board-Item: levain#7",), file="feature.txt", body="feature\n")
    git(world.w, "checkout", "-q", "main")
    world.cherry_pick(x)
    git(world.w, "rm", "-q", "feature.txt")
    git(world.w, "commit", "-q", "-m", "take the feature out again, by hand", date="2026-11-12T14:00:00Z")
    world.push_main()
    world.branch("seat/1112-40-i7-a", "origin/main")
    git(world.w, "cherry-pick", x, date="2026-11-12T15:00:00Z")
    git(world.w, "push", "-q", "-f", "origin", "seat/1112-40-i7-a:seat/1112-40-i7-a")
    git(world.w, "fetch", "-q", "origin")
    assert branch(read(world)[0], "seat/1112-40-i7-a")["ahead"] == 1


def _same_patch_twice(world, name_a="seat/1112-40-i7-a", name_b="seat/1112-41-i8-b"):
    """Two branches from the root, each with a commit of the byte-identical patch (distinct shas by commit time)."""
    for name, date in ((name_a, DAY), (name_b, "2026-11-12T11:00:00Z")):
        world.branch(name)
        world.commit("same work", trailers=("Board-Item: levain#7",), file="same.txt", body="alpha\n", date=date)
        git(world.w, "checkout", "-q", "main")


def test_two_branches_may_each_spend_the_one_main_commit_that_holds_their_work(world):
    _same_patch_twice(world)
    world.commit("land it", file="same.txt", body="alpha\n", date="2026-11-12T13:00:00Z")
    world.push_main()
    ev, _ = read(world)
    assert branch(ev, "seat/1112-40-i7-a")["ahead"] == 0 and branch(ev, "seat/1112-41-i8-b")["ahead"] == 0


def test_two_branches_may_each_spend_the_one_chain_commit_that_holds_their_work(world):
    _same_patch_twice(world)
    world.branch("queue/a/one")
    world.commit("chained", file="same.txt", body="alpha\n", date="2026-11-12T13:00:00Z")
    world.push("queue/a/one")
    ev, _ = read(world)
    assert branch(ev, "seat/1112-40-i7-a")["ahead"] == 0 and branch(ev, "seat/1112-41-i8-b")["ahead"] == 0


# ================================================================================================ B2 L3 round 1 fixes (1112-44)
from datetime import datetime   # noqa: E402  (this section is the L3 round 1 fixes; the import rides with it)

READER_KEYS = {"repo", "since", "main_ref", "main_commits", "main_records", "chain_refs", "chain_commits", "branches", "refs",
               "merged", "unattributed", "diverged", "matched_by_key"}


def _iso_to_epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _no_reflog_witness(w: World) -> None:
    (w.w / ".git" / "logs" / "refs" / "remotes" / "origin" / "main").unlink(missing_ok=True)


# ------------------------------------------------------------------------------------------------ 1. a reverted close
def _revert_head(world: World, n: int) -> None:
    for i in range(n):
        git(world.w, "revert", "--no-edit", "HEAD", date=f"2026-11-12T{14 + i}:00:00Z")


@pytest.mark.parametrize("reverts, in_effect", [(0, True), (1, False), (2, True), (3, False), (4, True)])
def test_a_closing_commit_closes_only_while_its_last_revert_is_itself_reverted(world, reverts, in_effect):
    """codex 1: C closes levain#1; `git revert C` takes the close back; a revert of that revert puts it back; and so on."""
    c = world.commit("close it", trailers=("Board-Item: levain#1", "Board-Closes: levain#1"))
    _revert_head(world, reverts)
    world.push_main()
    ev, _ = read(world)
    assert [(r["sha"], r["items"], r["closes"]) for r in ev["main"]] == ([(c, ["levain#1"], ["levain#1"])] if in_effect else [])


def test_a_revert_only_takes_back_the_commit_it_names(world):
    keep = world.commit("keep", trailers=("Board-Closes: levain#2",))
    gone = world.commit("gone", trailers=("Board-Closes: levain#1",))
    _revert_head(world, 1)
    world.push_main()
    assert [r["sha"] for r in read(world)[0]["main"]] == [keep] and gone != keep


# ------------------------------------------------------------------------------------------------ 2. one ref snapshot
def _advance_ref_after_log(monkeypatch, nth: int, ref: str, to: str) -> None:
    """The race of a concurrent `git fetch`: after the reader's ``nth`` `git log`, ``ref`` is moved to ``to``."""
    real = be._git
    calls = {"log": 0}

    def hook(repo, *args, **kw):
        out = real(repo, *args, **kw)
        if args[:1] == ("log",):
            calls["log"] += 1
            if calls["log"] == nth:
                git(repo, "update-ref", ref, to)
        return out

    monkeypatch.setattr(be, "_git", hook)


def test_a_fetch_that_moves_origin_main_between_the_reads_cannot_hide_a_branchs_work(world, monkeypatch):
    """complement 2 / codex 3: main was read at M0; a fetch moves it onto the branch's tip; the branch log then excluded it."""
    name = "seat/1112-40-i7-a"
    world.branch(name)
    b = world.commit("own", trailers=("Board-Item: levain#7",), file="own.txt")
    world.push(name)
    _advance_ref_after_log(monkeypatch, 1, "refs/remotes/origin/main", b)
    assert branch(read(world)[0], name)["ahead"] == 1


def test_a_fetch_that_moves_a_queue_ref_between_the_reads_cannot_hide_a_branchs_work(world, monkeypatch):
    name = "seat/1112-40-i7-a"
    world.branch("queue/a/one")
    world.commit("q1", trailers=("Board-Item: levain#1",), file="q.txt")
    world.push("queue/a/one")
    world.branch(name, "queue/a/one")
    b = world.commit("own", trailers=("Board-Item: levain#7",), file="own.txt")
    world.push(name)
    _advance_ref_after_log(monkeypatch, 2, "refs/remotes/origin/queue/a/one", b)       # after the chain log, before the branch log
    assert branch(read(world)[0], name)["ahead"] == 1


def test_a_ref_that_moves_before_the_snapshot_is_simply_the_snapshot(world, monkeypatch):
    """No log has run yet: the move is not a race, it is the state the reader reads (and reads whole)."""
    name = "seat/1112-40-i7-a"
    world.branch(name)
    b = world.commit("own", trailers=("Board-Item: levain#7",), file="own.txt")
    world.push(name)
    git(world.w, "update-ref", "refs/remotes/origin/main", b)
    assert [b["name"] for b in read(world)[0]["branches"]] == [name] and branch(read(world)[0], name)["ahead"] == 0


# ------------------------------------------------------------------------------------------------ 3. the freshness witness
def test_a_fetch_inside_a_linked_worktree_is_the_witness_even_when_the_common_dir_has_none(world, tmp_path):
    """complement 1: git writes FETCH_HEAD to the per-worktree git dir; a quiet main read permanently stale."""
    wt = world.linked_worktree(tmp_path / "wt")
    (world.w / ".git" / "FETCH_HEAD").unlink(missing_ok=True)
    _no_reflog_witness(world)
    git(wt, "fetch", "-q", "origin")
    assert not (world.w / ".git" / "FETCH_HEAD").exists() and (world.w / ".git" / "worktrees" / "wt" / "FETCH_HEAD").is_file()
    ev, _ = be.read_git_evidence(wt, repo_name="levain", since=SINCE)
    assert ev["fetched_at"].endswith("Z") and ev["unreadable"] == {}


@pytest.mark.parametrize("worktree_newer", [True, False])
def test_the_newer_of_the_two_fetch_heads_is_the_witness(world, tmp_path, worktree_newer):
    wt = world.linked_worktree(tmp_path / "wt")
    _no_reflog_witness(world)
    git(wt, "fetch", "-q", "origin")
    common, own = world.w / ".git" / "FETCH_HEAD", world.w / ".git" / "worktrees" / "wt" / "FETCH_HEAD"
    old, new = "2026-11-12T10:00:00Z", "2026-11-12T11:00:00Z"
    for path, iso in ((own, new if worktree_newer else old), (common, old if worktree_newer else new)):
        os.utime(path, (_iso_to_epoch(iso),) * 2)
    ev, _ = be.read_git_evidence(wt, repo_name="levain", since=SINCE)
    assert ev["fetched_at"] == new


def test_a_fetch_from_another_remote_does_not_stamp_origin_main_fresh(world, tmp_path):
    """complement 1 / codex 2: `git fetch upstream main` writes a line that also says `branch 'main' of`, of another url."""
    world.second_remote("upstream", tmp_path / "upstream.git")
    (world.w / ".git" / "FETCH_HEAD").unlink(missing_ok=True)
    _no_reflog_witness(world)
    git(world.w, "fetch", "-q", "upstream", "main")
    text = (world.w / ".git" / "FETCH_HEAD").read_text()
    assert "branch 'main' of" in text and "upstream" in text            # the premise: the old reader would have accepted it
    ev, _ = read(world)
    assert "fetched_at" not in ev


def test_a_fetch_head_that_carries_both_remotes_counts_for_the_origin_line(world, tmp_path):
    world.second_remote("upstream", tmp_path / "upstream.git")
    _no_reflog_witness(world)
    git(world.w, "fetch", "-q", "--all")                                  # one FETCH_HEAD, lines for both urls
    text = (world.w / ".git" / "FETCH_HEAD").read_text()
    assert "origin" in text and "upstream" in text
    assert read(world)[0]["fetched_at"].endswith("Z")


@pytest.mark.parametrize("url_form", ["trailing-slash", "file-scheme"])
def test_the_origin_url_matches_however_git_prints_it(world, tmp_path, url_form):
    origin = str(world.origin)
    git(world.w, "remote", "set-url", "origin", origin + "/" if url_form == "trailing-slash" else f"file://{origin}")
    (world.w / ".git" / "FETCH_HEAD").unlink(missing_ok=True)
    _no_reflog_witness(world)
    git(world.w, "fetch", "-q", "origin")
    assert "branch 'main' of" in (world.w / ".git" / "FETCH_HEAD").read_text()
    assert read(world)[0]["fetched_at"].endswith("Z")


def test_fetch_head_is_read_through_one_open_file_not_by_path_twice(world, monkeypatch):
    """codex 6: text and mtime from two path lookups can come from two different files. Pin the single descriptor."""
    real_stat, real_read = Path.stat, Path.read_text

    def no_stat(self, *a, **kw):
        assert self.name != "FETCH_HEAD", "FETCH_HEAD stat'd by path"
        return real_stat(self, *a, **kw)

    def no_read(self, *a, **kw):
        assert self.name != "FETCH_HEAD", "FETCH_HEAD read by path"
        return real_read(self, *a, **kw)

    _no_reflog_witness(world)
    monkeypatch.setattr(Path, "stat", no_stat)
    monkeypatch.setattr(Path, "read_text", no_read)
    assert read(world)[0]["fetched_at"].endswith("Z")


def test_an_absurd_mtime_is_an_absent_witness_not_a_traceback(world, monkeypatch):
    """deepseek LOW-2: `datetime.fromtimestamp` of a year past 9999 raises OverflowError/ValueError."""
    url = git(world.w, "remote", "get-url", "origin").removesuffix(".git")
    line = f"{'a' * 40}\t\tbranch 'main' of {url}\n"
    _no_reflog_witness(world)
    for absurd in (1e18, 2.0 ** 40, float("inf"), float("nan"), -1e18):
        monkeypatch.setattr(be, "_read_stamped", lambda path, a=absurd: (line, a))
        ev, _ = read(world)
        assert "fetched_at" not in ev and ev["unreadable"] == {}, absurd


def test_a_far_future_mtime_on_disk_never_raises(world):
    _no_reflog_witness(world)
    fh = world.w / ".git" / "FETCH_HEAD"
    try:
        os.utime(fh, (_iso_to_epoch("2900-01-01T00:00:00Z"),) * 2)
    except (OSError, OverflowError, ValueError):
        pytest.skip("this filesystem refuses a far-future mtime")
    ev, _ = read(world)
    assert "unreadable" in ev and ev["unreadable"] == {}                # a stamp (the board calls it future) or none; never a traceback


# ------------------------------------------------------------------------------------------------ 4. a failure board's reader
def _whole(summary: dict) -> None:
    assert set(summary) == READER_KEYS, set(summary) ^ READER_KEYS
    for k in READER_KEYS - {"repo", "since", "main_ref", "diverged"}:
        assert summary[k] == 0, k
    assert summary["diverged"] == []


def test_a_successful_read_carries_every_reader_field(world):
    assert set(read(world)[1]) == READER_KEYS


def test_an_invalid_since_still_carries_every_reader_field(world):
    _, summary = be.read_git_evidence(world.w, repo_name="levain", since="last week")
    _whole(summary)
    assert summary["since"] == "last week" and summary["main_ref"] == "origin/main" and summary["repo"] == "levain"


def test_a_missing_main_ref_still_carries_every_reader_field(tmp_path):
    w = World(tmp_path)
    w.commit("root", date=OLD)
    _, summary = be.read_git_evidence(w.w, repo_name="levain", since=SINCE)
    _whole(summary)


def test_a_path_that_is_not_a_repository_still_carries_every_reader_field(tmp_path):
    _, summary = be.read_git_evidence(tmp_path / "nowhere", repo_name="levain", since=SINCE)
    _whole(summary)


def test_a_shallow_clone_still_carries_every_reader_field(world, tmp_path):
    world.commit("w1")
    world.commit("w2")
    world.push_main()
    clone = world.clone(tmp_path / "shallow", "--depth", "1")
    _, summary = be.read_git_evidence(clone, repo_name="levain", since=SINCE)
    _whole(summary)


def test_a_partial_clone_still_carries_every_reader_field(world, tmp_path):
    git(world.origin, "config", "uploadpack.allowFilter", "true")
    git(world.origin, "config", "uploadpack.allowAnySHA1InWant", "true")
    world.commit("w1", file="a.txt", body="alpha\n")
    world.push_main()
    clone = world.clone(tmp_path / "partial", "--filter=blob:none", "--no-checkout")
    _, summary = be.read_git_evidence(clone, repo_name="levain", since=SINCE)
    _whole(summary)


def test_a_main_that_cannot_be_read_inside_the_run_still_carries_every_reader_field(world, monkeypatch):
    monkeypatch.setattr(be, "MAX_MAIN_COMMITS", 1)
    monkeypatch.setattr(be, "MIN_PREWINDOW_TAIL", 5)
    world.commit("w1")
    world.push_main()
    ev, summary = read(world)
    assert "main" in ev["unreadable"]
    assert set(summary) == READER_KEYS


# ------------------------------------------------------------------------------------------------ 5. a cut main history
def test_a_cut_history_with_a_real_old_tail_is_whole_for_the_window(world, monkeypatch):
    """The board must not go dark when main outgrows the cap: a log cut at the cap is whole for the window when enough of what was read predates it."""
    monkeypatch.setattr(be, "MAX_MAIN_COMMITS", 3)
    monkeypatch.setattr(be, "MIN_PREWINDOW_TAIL", 3)
    world.commit("old2", date=OLD)
    world.commit("old3", date=OLD)
    world.commit("new", trailers=("Board-Item: levain#1",))
    world.push_main()                                    # root, old2, old3 (all OLD), new: 4 commits, the cap is 3, 3 predate the window
    ev, _ = read(world)
    assert ev["unreadable"] == {} and [r["items"] for r in ev["main"]] == [["levain#1"]]


def test_a_cut_history_without_a_real_old_tail_is_unreadable(world, monkeypatch):
    monkeypatch.setattr(be, "MAX_MAIN_COMMITS", 3)
    monkeypatch.setattr(be, "MIN_PREWINDOW_TAIL", 4)
    world.commit("old2", date=OLD)
    world.commit("old3", date=OLD)
    world.commit("new", trailers=("Board-Item: levain#1",))
    world.push_main()                                    # 3 commits predate the window, the tail asked for is 4
    ev, _ = read(world)
    assert "more than 3 commits" in ev["unreadable"]["main"] and "main" not in ev
    assert {"chain", "branches", "unattributed"} <= set(ev["unreadable"])


def test_one_commit_with_a_wrong_clock_cannot_make_a_cut_history_look_whole(world, monkeypatch):
    """codex 5 / deepseek MED-1: the skewed commit is inside the 3 newest, so `min(ts) < window` called the cut log complete."""
    monkeypatch.setattr(be, "MAX_MAIN_COMMITS", 3)
    monkeypatch.setattr(be, "MIN_PREWINDOW_TAIL", 3)
    world.commit("the close, past the cap", trailers=("Board-Closes: levain#1",))
    world.commit("skewed", date=OLD)
    world.commit("w2")
    world.commit("w3")
    world.push_main()                                    # root and the skewed commit predate the window: 2 old commits, the tail asked for is 3
    ev, _ = read(world)
    assert "more than 3 commits" in ev["unreadable"]["main"] and "main" not in ev


def test_a_history_of_exactly_the_cap_is_whole(world, monkeypatch):
    monkeypatch.setattr(be, "MAX_MAIN_COMMITS", 3)
    world.commit("a", trailers=("Board-Item: levain#1",))
    world.commit("b", trailers=("Board-Item: levain#2",))
    world.push_main()                                    # root + 2 = exactly 3: nothing is cut off
    ev, _ = read(world)
    assert ev["unreadable"] == {} and sorted(i for r in ev["main"] for i in r["items"]) == ["levain#1", "levain#2"]


def _toggle_world(world: World):
    """root(t.txt=base) -> M0 (adds X) -> F (removes X) -> branch b (adds X again, the same patch as M0) / M1 on main."""
    (world.w / "t.txt").write_text("base\n")
    git(world.w, "add", "-A")
    git(world.w, "commit", "-q", "-m", "base", date=OLD)
    git(world.w, "push", "-q", "origin", "main")
    world.commit_raw("m0 adds X", file="t.txt", body="base\nX\n", date="2026-11-12T10:00:00Z")
    fork = world.commit_raw("f removes X", file="t.txt", body="base\n", date="2026-11-12T11:00:00Z")
    world.commit("m1", date="2026-11-12T13:00:00Z")
    world.push_main()
    git(world.w, "checkout", "-q", "-B", "seat/1112-40-i7-a", fork)
    world.commit_raw("b adds X again\n\nBoard-Item: levain#7", file="t.txt", body="base\nX\n", date="2026-11-12T12:00:00Z")
    world.push("seat/1112-40-i7-a")
    git(world.w, "checkout", "-q", "main")
    return fork


def test_the_toggle_world_reads_ahead_one_when_main_is_whole(world):
    """The control for the next test: M0's patch is the same as the branch's, but M0 is behind the fork."""
    _toggle_world(world)
    ev, _ = read(world)
    assert branch(ev, "seat/1112-40-i7-a")["ahead"] == 1 and ev["unreadable"] == {}


def test_a_fork_point_main_does_not_hold_marks_the_branches_unreadable_not_the_whole_pool(world, monkeypatch):
    """deepseek MED-1: main's log lost the fork commit F (it is truncated, or F is mis-dated), so the boundary is empty and
    the pool would be everything: the older M0, which carries the branch's patch, would read the branch as landed."""
    fork = _toggle_world(world)
    real = be._log
    calls = {"n": 0}

    def cut(repo, revs, **kw):
        out = real(repo, revs, **kw)
        calls["n"] += 1
        if calls["n"] == 1:                              # the main log
            out.pop(fork)
        return out

    monkeypatch.setattr(be, "_log", cut)
    ev, _ = read(world)
    assert {"chain", "branches", "unattributed"} <= set(ev["unreadable"]) and "main" not in ev["unreadable"]
    assert "branches" not in ev


def test_a_branch_with_no_history_in_common_with_main_still_reads(world):
    """The empty boundary that is TRUE (an orphan root): the pool is everything, and that is right. Not an error."""
    git(world.w, "checkout", "-q", "--orphan", "seat/1112-40-i7-a")
    git(world.w, "rm", "-rfq", ".")
    world.commit("own root", trailers=("Board-Item: levain#7",), file="own.txt")
    world.push("seat/1112-40-i7-a")
    ev, _ = read(world)
    assert ev["unreadable"] == {} and branch(ev, "seat/1112-40-i7-a")["ahead"] == 1


def test_a_queue_ref_that_moves_after_the_snapshot_is_not_read_as_merged(world, monkeypatch):
    """The snapshot's sha is the truth: a ref `for-each-ref --merged` finds merged only because a fetch moved it since is
    not merged, and its unlanded commit stays on the chain."""
    world.branch("queue/a/one")
    world.commit("q1", trailers=("Board-Item: levain#1",), file="q.txt")
    world.push("queue/a/one")
    real = be._git

    def hook(repo, *args, **kw):
        if args[:1] == ("for-each-ref",) and any(a.startswith("--merged=") for a in args):
            git(repo, "update-ref", "refs/remotes/origin/queue/a/one", git(repo, "rev-parse", "refs/remotes/origin/main"))
        return real(repo, *args, **kw)

    monkeypatch.setattr(be, "_git", hook)
    ev, _ = read(world)
    assert [r["ref"] for r in ev["chain"]] == ["queue/a/one"] and "queue/a/one" not in ev["merged"]


def test_a_revert_dated_before_its_target_still_takes_it_back(world):
    """`git log` order is not parent-after-child under clock skew: the closing commit can be listed BEFORE the revert that
    takes it back, so the effect must not depend on the order of the log."""
    c = world.commit("close it", trailers=("Board-Item: levain#1", "Board-Closes: levain#1"), date="2026-11-12T12:00:00Z")
    world.commit("x", date="2026-11-12T13:00:00Z")
    git(world.w, "checkout", "-q", "-b", "side", c)
    git(world.w, "revert", "--no-edit", c, date="2026-11-12T11:00:00Z")           # dated BEFORE the commit it reverts
    git(world.w, "checkout", "-q", "main")
    git(world.w, "merge", "-q", "--no-ff", "-m", "merge side", "side", date="2026-11-12T14:00:00Z")
    world.push_main()
    assert read(world)[0]["main"] == []


def test_a_chain_ref_whose_fork_point_main_does_not_hold_marks_the_chain_unreadable(world, monkeypatch):
    """The same guard on a queue ref (deepseek MED-1 names chain/branches/unattributed): main's log lost the fork commit."""
    fork = world.commit("fork point", date="2026-11-12T10:00:00Z")
    world.push_main()
    world.branch("queue/a/one", "origin/main")
    world.commit("c1", trailers=("Board-Item: levain#1",), file="c.txt", date="2026-11-12T11:00:00Z")
    world.push("queue/a/one")
    real = be._log
    calls = {"n": 0}

    def cut(repo, revs, **kw):
        out = real(repo, revs, **kw)
        calls["n"] += 1
        if calls["n"] == 1:                              # the main log
            out.pop(fork)
        return out

    monkeypatch.setattr(be, "_log", cut)
    ev, _ = read(world)
    assert {"chain", "branches", "unattributed"} <= set(ev["unreadable"]) and "chain" not in ev
    assert "main" in ev and "main" not in ev["unreadable"]


# ================================================================================================ B2 L3 round 2 fixes (1113-1)
import threading   # noqa: E402  (this section is the L3 round 2 fixes; the import rides with it)


def _after_nth_log(monkeypatch, nth: int, act) -> None:
    """The race of a concurrent writer: after the reader's ``nth`` `git log`, ``act(repo)`` runs."""
    real = be._git
    calls = {"log": 0}

    def hook(repo, *args, **kw):
        out = real(repo, *args, **kw)
        if args[:1] == ("log",):
            calls["log"] += 1
            if calls["log"] == nth:
                act(repo)
        return out

    monkeypatch.setattr(be, "_git", hook)


# ------------------------------------------------------------------------------------------------ 1. the notes ref, once
def _noted_branch(world, name="seat/1112-40-a"):
    world.branch(name)
    b = world.commit("own", file="own.txt")                        # no trailer: only the note attributes it
    git(world.w, "notes", "--ref=board", "add", "-m", "Board-Item: levain#7", b)
    world.push(name)
    return b


def test_a_notes_ref_that_moves_after_the_snapshot_is_the_same_notes_for_every_log(world, monkeypatch):
    """codex OPEN 2: the notes ref was read by name on every log, so a notes fetch between the main log and the branch log
    gave the two reads different notes."""
    _noted_branch(world)
    _after_nth_log(monkeypatch, 1, lambda repo: git(repo, "update-ref", "-d", "refs/notes/board"))
    ev, _ = read(world)
    assert [b["items"] for b in ev["branches"]] == [["levain#7"]]


def test_a_notes_ref_created_after_the_snapshot_is_not_seen_by_a_later_log(world, monkeypatch):
    """An absent notes ref stays absent for the whole read."""
    name = "seat/1112-40-i7-a"
    world.branch(name)
    b = world.commit("own", trailers=("Board-Item: levain#7",), file="own.txt")
    world.push(name)
    _after_nth_log(monkeypatch, 1, lambda repo: git(repo, "notes", "--ref=board", "add", "-m", "Board-Item: levain#98\nBoard-Closes: levain#99", b))
    assert branch(read(world)[0], name)["items"] == ["levain#7"]


def test_only_the_snapshot_names_the_notes_ref(world, monkeypatch):
    """The invariant 'every ref once, then shas only' as an observable: one call names refs/notes/board, the for-each-ref."""
    _noted_branch(world)
    seen: list[tuple[str, ...]] = []
    real = be._run

    def spy(repo, *args, **kw):
        seen.append(args)
        return real(repo, *args, **kw)

    monkeypatch.setattr(be, "_run", spy)
    ev, _ = read(world)
    assert [a[0] for a in seen if any("refs/notes" in x for x in a)] == ["for-each-ref"]
    assert [b["items"] for b in ev["branches"]] == [["levain#7"]]


def test_a_fanned_out_notes_tree_is_read(world):
    """git fans a big notes tree out into `ab/cdef...` paths; the reader reads the notes tree itself, so it must follow that."""
    name = "seat/1112-40-a"
    world.branch(name)
    b = world.commit("own", file="own.txt")
    world.push(name)
    blob = git(world.w, "hash-object", "-w", "--stdin", stdin="Board-Item: levain#7\n")
    sub = git(world.w, "mktree", stdin=f"100644 blob {blob}\t{b[2:]}\n")
    root = git(world.w, "mktree", stdin=f"040000 tree {sub}\t{b[:2]}\n")
    git(world.w, "update-ref", "refs/notes/board", git(world.w, "commit-tree", root, "-m", "notes"))
    assert [x["items"] for x in read(world)[0]["branches"]] == [["levain#7"]]


# ------------------------------------------------------------------------------------------------ 2. FETCH_HEAD, not through a link
def test_a_symlinked_fetch_head_is_no_witness(world, tmp_path):
    """codex MED: a FETCH_HEAD that is a link to a regular file with a matching line and a recent mtime read as a fresh fetch."""
    _no_reflog_witness(world)
    fh = world.w / ".git" / "FETCH_HEAD"
    real = tmp_path / "real_fetch_head"
    real.write_text(fh.read_text())
    fh.unlink()
    fh.symlink_to(real)
    assert "fetched_at" not in read(world)[0]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no mkfifo on this platform")
def test_a_fifo_named_fetch_head_is_no_witness_and_does_not_block(world):
    """complement LOW: open() on a FIFO blocks before any regular-file check."""
    _no_reflog_witness(world)
    fh = world.w / ".git" / "FETCH_HEAD"
    fh.unlink()
    os.mkfifo(fh)
    out: dict = {}
    t = threading.Thread(target=lambda: out.update(ev=read(world)[0]), daemon=True)
    t.start()
    t.join(10)
    if t.is_alive():
        os.close(os.open(fh, os.O_WRONLY | os.O_NONBLOCK))          # release the blocked open, so the worker thread ends
        t.join(5)
        pytest.fail("the reader blocked on a FIFO named FETCH_HEAD")
    assert "fetched_at" not in out["ev"]


# ------------------------------------------------------------------------------------------------ 3. a reverted merge
def _merged_close(world):
    """root -> side: c (closes levain#1) / main: m1 (closes levain#2) -> M = `git merge --no-ff side`."""
    git(world.w, "checkout", "-q", "-b", "side")
    c = world.commit_raw("close it\n\nBoard-Item: levain#1\nBoard-Closes: levain#1", file="c.txt", body="C\n", date="2026-11-12T11:00:00Z")
    git(world.w, "checkout", "-q", "main")
    m1 = world.commit("main work", trailers=("Board-Closes: levain#2",), date="2026-11-12T12:00:00Z")
    git(world.w, "merge", "-q", "--no-ff", "-m", "merge side", "side", date="2026-11-12T13:00:00Z")
    return c, m1, git(world.w, "rev-parse", "HEAD")


def _closers(world) -> set[str]:
    world.push_main()
    return {r["sha"] for r in read(world)[0]["main"]}


def test_the_revert_of_a_merge_takes_back_the_work_the_merge_brought_in(world):
    """complement MED: `git revert -m 1 M` is the usual way to undo a merge; M carries no trailers, the side commit does."""
    c, m1, merge = _merged_close(world)
    assert _closers(world) == {c, m1}
    git(world.w, "revert", "--no-edit", "-m", "1", merge, date="2026-11-12T14:00:00Z")
    assert _closers(world) == {m1}


def test_the_revert_of_the_revert_of_a_merge_puts_the_work_back(world):
    c, m1, merge = _merged_close(world)
    git(world.w, "revert", "--no-edit", "-m", "1", merge, date="2026-11-12T14:00:00Z")
    git(world.w, "revert", "--no-edit", "HEAD", date="2026-11-12T15:00:00Z")
    assert _closers(world) == {c, m1}


def test_reverting_a_merge_against_its_second_parent_takes_back_the_other_side(world):
    """`-m 2` keeps the side branch as the mainline: what the merge brought in is then main's own work (m1)."""
    c, m1, merge = _merged_close(world)
    git(world.w, "revert", "--no-edit", "-m", "2", merge, date="2026-11-12T14:00:00Z")
    assert _closers(world) == {c}


def test_a_branch_carrying_the_work_of_a_reverted_merge_is_ahead_not_landed(world):
    """The same class in the matcher: a landing a merge-revert took back is not landed work."""
    name = "seat/1112-40-i7-a"
    root = git(world.w, "rev-parse", "HEAD")
    _c, _m1, merge = _merged_close(world)
    git(world.w, "checkout", "-q", "-B", name, root)
    world.commit_raw("again\n\nBoard-Item: levain#7", file="c.txt", body="C\n", date="2026-11-12T16:00:00Z")      # the same patch as c
    world.push(name)
    git(world.w, "checkout", "-q", "main")
    world.push_main()
    assert branch(read(world)[0], name)["ahead"] == 0                                    # control: while the merge stands, c holds it
    git(world.w, "revert", "--no-edit", "-m", "1", merge, date="2026-11-12T17:00:00Z")
    world.push_main()
    assert branch(read(world)[0], name)["ahead"] == 1


# ------------------------------------------------------------------------------------------------ 4. a canonical revert marker
@pytest.mark.parametrize("message", [
    "docs: an example\n\nThis reverts commit <C>. This is only an example",              # prose after the sha
    'Revert "close it"\n\nThis reverts commit <C>. This is only an example',            # the right subject, prose after the sha
    "tidy up\n\nThis reverts commit <C>.",                                                # the generated line, not a revert subject
    'Revert "close it"\n\nThis reverts commit <C>',                                       # no period
    'Revert "close it"\n\nThis reverts commit <c12>.',                                    # a short sha (known edge: fails open)
    'Revert "close it"\n\nThis reverts commit <CU>.',                                     # an upper-case sha (known edge)
    'Revert "close it"\n\nsee: This reverts commit <C>.',                                  # not at the start of a line
])
def test_a_message_that_merely_says_the_phrase_is_not_a_revert(world, message):
    """codex HIGH 1: only git's own generated line, under a Revert subject, counts."""
    c = world.commit("close it", trailers=("Board-Closes: levain#1",))
    world.commit_raw(message.replace("<C>", c).replace("<c12>", c[:12]).replace("<CU>", c.upper()), file="d.txt")
    world.push_main()
    assert [r["sha"] for r in read(world)[0]["main"]] == [c]


@pytest.mark.parametrize("message", [
    'Revert "close it"\n\nThis reverts commit <C>.',
    'Revert "close it"\n\nThis reverts commit <C>.\n\nThis reverts commit <C>.',      # the same marker twice is one marker
])                                                                   # (the merge form needs a real merge: test_the_merge_form_git_writes...)
def test_the_canonical_forms_are_reverts(world, message):
    c = world.commit("close it", trailers=("Board-Closes: levain#1",))
    world.commit_raw(message.replace("<C>", c), file="d.txt")
    world.push_main()
    assert read(world)[0]["main"] == []


def test_a_commit_carrying_two_revert_markers_reverts_neither(world):
    """codex HIGH 1: a squash of reverts is ambiguous."""
    a = world.commit("a", trailers=("Board-Closes: levain#1",))
    b = world.commit("b", trailers=("Board-Closes: levain#2",))
    world.commit_raw(f'Revert "a"\n\nThis reverts commit {a}.\n\nThis reverts commit {b}.\n', file="sq.txt")
    world.push_main()
    assert {r["sha"] for r in read(world)[0]["main"]} == {a, b}


# ------------------------------------------------------------------------------------------------ 5. the boundary, per tip
def _cut_main(world, monkeypatch):
    """A main of root + old2 + old3 + new1 + new2 read with a cap of 3: the log keeps new2, new1, old3, old2 (a real old tail
    of 2) and OMITS root, the commit a branch may have forked from."""
    monkeypatch.setattr(be, "MAX_MAIN_COMMITS", 3)
    monkeypatch.setattr(be, "MIN_PREWINDOW_TAIL", 2)
    root = git(world.w, "rev-parse", "HEAD")
    world.commit("old2", date=OLD)
    world.commit("old3", date=OLD)
    new1 = world.commit("new1", date="2026-11-12T10:00:00Z")
    world.commit("new2", date="2026-11-12T11:00:00Z")
    world.push_main()
    return root, new1


def _two_tips(world, name, origin_base, local_base):
    """`name` with an origin copy forked from ``origin_base`` and a local head forked from ``local_base``: diverged."""
    world.branch(name, origin_base)
    world.commit("remote own", trailers=("Board-Item: levain#7",), file="r.txt")
    world.push(name)
    git(world.w, "checkout", "-q", "-B", name, local_base)
    world.commit("local own", trailers=("Board-Item: levain#7",), file="l.txt")
    git(world.w, "checkout", "-q", "main")


def test_a_diverged_branch_one_of_whose_tips_forked_from_a_commit_main_does_not_hold_is_unreadable(world, monkeypatch):
    """codex HIGH 2: one tip reaches retained history, the other forked at the omitted root: the COMBINED boundary was
    nonempty, so the second tip was never checked and was classified against the wrong pool."""
    root, new1 = _cut_main(world, monkeypatch)
    _two_tips(world, "seat/1112-40-i7-a", new1, root)
    ev, _ = read(world)
    assert {"chain", "branches", "unattributed"} <= set(ev["unreadable"]) and "branches" not in ev
    assert "main" in ev and "main" not in ev["unreadable"]


def test_a_diverged_branch_both_of_whose_tips_reach_retained_history_reads(world, monkeypatch):
    _root, new1 = _cut_main(world, monkeypatch)
    _two_tips(world, "seat/1112-40-i7-a", new1, new1)
    ev, summary = read(world)
    assert ev["unreadable"] == {} and summary["diverged"] == ["seat/1112-40-i7-a"]


def test_a_diverged_branch_whose_other_tip_is_an_orphan_root_still_reads(world, monkeypatch):
    """An orphan tip (no history in common with main at all) is a TRUE empty boundary, not a cut."""
    name = "seat/1112-40-i7-a"
    _root, new1 = _cut_main(world, monkeypatch)
    world.branch(name, new1)
    world.commit("remote own", trailers=("Board-Item: levain#7",), file="r.txt")
    world.push(name)
    git(world.w, "checkout", "-q", "main")
    world.delete_local(name)
    git(world.w, "checkout", "-q", "--orphan", name)
    git(world.w, "rm", "-rfq", ".")
    world.commit("local orphan", trailers=("Board-Item: levain#7",), file="o.txt")
    git(world.w, "checkout", "-q", "main")
    ev, summary = read(world)
    assert ev["unreadable"] == {} and summary["diverged"] == [name]


def test_git_titles_the_revert_of_a_revert_reapply_and_it_still_counts(world):
    """git >= 2.43 writes `Reapply "..."` for the revert of a revert: the canonical subject set must include it."""
    c = world.commit("close it", trailers=("Board-Closes: levain#1",))
    git(world.w, "revert", "--no-edit", "HEAD", date="2026-11-12T14:00:00Z")
    git(world.w, "revert", "--no-edit", "HEAD", date="2026-11-12T15:00:00Z")
    world.push_main()
    assert [r["sha"] for r in read(world)[0]["main"]] == [c]


# ================================================================================================ B2 L3 round 3 fixes (1113-1)
# ------------------------------------------------------------------------------------------------ 1. a landed tip is readable
def _landed_branch(world, name="seat/1112-40-i7-a"):
    """A branch whose tip is a commit main now holds (the branch was fast-forwarded into main)."""
    world.branch(name)
    tip = world.commit("own", trailers=("Board-Item: levain#7",), file="own.txt", date="2026-11-12T12:00:00Z")
    world.push(name)
    git(world.w, "checkout", "-q", "main")
    git(world.w, "merge", "-q", "--ff-only", name)
    world.push_main()
    return name, tip


def test_a_branch_whose_tip_is_itself_a_main_commit_reads_it_is_its_own_boundary(world):
    """deepseek HIGH (refuted, pinned): a landed branch's tip is a commit main retains, so its boundary is that commit and
    the per-tip guard does not run merge-base on it. Run in a scratch world: the source stays readable."""
    name, _tip = _landed_branch(world)
    ev, summary = read(world)
    assert ev["unreadable"] == {}
    assert branch(ev, name)["ahead"] == 0 and summary["branches"] == 1


def test_a_landed_tip_in_the_middle_of_main_history_reads_too(world):
    name, _tip = _landed_branch(world)
    world.commit("later", date="2026-11-12T14:00:00Z")
    world.push_main()
    ev, _ = read(world)
    assert ev["unreadable"] == {} and branch(ev, name)["ahead"] == 0


def test_a_landed_tip_whose_commit_main_log_omits_is_unreadable_not_a_pass(world, monkeypatch):
    """The guard is still the guard: the same landed tip, with main's log cut below it, is a fork point main does not hold."""
    name, _tip = _landed_branch(world)
    for i in range(4):
        world.commit(f"later{i}", date=f"2026-11-12T{14 + i}:00:00Z")
    world.push_main()
    monkeypatch.setattr(be, "MAX_MAIN_COMMITS", 3)
    monkeypatch.setattr(be, "MIN_PREWINDOW_TAIL", 1)
    ev, _ = read(world)
    assert {"chain", "branches", "unattributed"} <= set(ev["unreadable"])


# ------------------------------------------------------------------------------------------------ 2. a merge revert needs a mainline that is a parent
def test_a_plain_marker_naming_a_merge_is_not_a_revert_of_that_merge(world):
    """codex HIGH 1: `git revert -m` of a merge always writes the merge form; the plain line on a merge is no such revert, so the
    merge's side commits keep their closes."""
    c, m1, merge = _merged_close(world)
    world.commit_raw(f'Revert "merge side"\n\nThis reverts commit {merge}.\n', file="d.txt", date="2026-11-12T14:00:00Z")
    assert _closers(world) == {c, m1}


def test_a_merge_marker_whose_mainline_is_not_a_parent_of_the_merge_is_not_a_revert(world):
    c, m1, merge = _merged_close(world)
    other = "0123456789abcdef0123456789abcdef01234567"
    world.commit_raw(f'Revert "merge side"\n\nThis reverts commit {merge}, reversing\nchanges made to {other}.\n', file="d.txt",
                     date="2026-11-12T14:00:00Z")
    assert _closers(world) == {c, m1}


def test_a_merge_marker_naming_an_ordinary_commit_is_not_a_revert_of_it(world):
    c = world.commit("close it", trailers=("Board-Closes: levain#1",))
    p = git(world.w, "rev-parse", f"{c}^")
    world.commit_raw(f'Revert "close it"\n\nThis reverts commit {c}, reversing\nchanges made to {p}.\n', file="d.txt")
    assert _closers(world) == {c}


@pytest.mark.parametrize("one_line", [False, True])
def test_the_merge_form_git_writes_is_a_revert_of_the_merge(world, one_line):
    """The control for the three cases above: the canonical merge form, with the mainline sha a parent, still reverts it."""
    c, m1, merge = _merged_close(world)
    p1 = git(world.w, "rev-parse", f"{merge}^1")
    sep = " " if one_line else "\n"
    world.commit_raw(f'Revert "merge side"\n\nThis reverts commit {merge}, reversing{sep}changes made to {p1}.\n', file="d.txt",
                     date="2026-11-12T14:00:00Z")
    assert _closers(world) == {m1}


# ------------------------------------------------------------------------------------------------ 3. the matcher's candidates are what is in effect
def _merged_two(world):
    """root -> side: c1 (c.txt), c2 (c2.txt) / main: m1 -> M = `git merge --no-ff side`. Two side commits, so that the
    revert-of-the-revert (one patch of the whole merge) is NOT the patch of either."""
    git(world.w, "checkout", "-q", "-b", "side")
    world.commit_raw("c1\n\nBoard-Closes: levain#1", file="c.txt", body="C\n", date="2026-11-12T11:00:00Z")
    world.commit_raw("c2\n\nBoard-Closes: levain#3", file="c2.txt", body="C2\n", date="2026-11-12T11:30:00Z")
    git(world.w, "checkout", "-q", "main")
    world.commit("main work", trailers=("Board-Closes: levain#2",), date="2026-11-12T12:00:00Z")
    git(world.w, "merge", "-q", "--no-ff", "-m", "merge side", "side", date="2026-11-12T13:00:00Z")
    return git(world.w, "rev-parse", "HEAD")


def test_a_branch_carrying_work_a_merge_revert_took_back_and_a_revert_of_it_restored_is_landed(world):
    """deepseek MED: M, R1 = revert -m 1 M, R2 = revert R1: C is in effect again, and a branch with C's patch is landed."""
    name = "seat/1112-40-i7-a"
    root = git(world.w, "rev-parse", "HEAD")
    merge = _merged_two(world)
    git(world.w, "checkout", "-q", "-B", name, root)
    world.commit_raw("again\n\nBoard-Item: levain#7", file="c.txt", body="C\n", date="2026-11-12T16:00:00Z")      # c1's patch
    world.push(name)
    git(world.w, "checkout", "-q", "main")
    world.push_main()
    assert branch(read(world)[0], name)["ahead"] == 0                                    # M stands: landed
    git(world.w, "revert", "--no-edit", "-m", "1", merge, date="2026-11-12T17:00:00Z")
    world.push_main()
    assert branch(read(world)[0], name)["ahead"] == 1                                    # R1: taken back, so ahead
    git(world.w, "revert", "--no-edit", "HEAD", date="2026-11-12T18:00:00Z")
    world.push_main()
    assert branch(read(world)[0], name)["ahead"] == 0                                    # R2: in effect again, so landed


# ------------------------------------------------------------------------------------------------ 4. the notes, bounded
def _notes_tree(world, real: dict[str, str], filler: int, filler_text: str | None = None) -> None:
    """refs/notes/board = a tree of ``real`` ({commit sha: note text}) plus ``filler`` notes on commits that do not exist.
    The filler blobs are MISSING objects (`mktree --missing`) unless ``filler_text`` is given: a reader that asks git for one
    fails, so reading only what it needs is observable."""
    lines = [f"100644 blob {git(world.w, 'hash-object', '-w', '--stdin', stdin=text)}\t{sha}" for sha, text in real.items()]
    if filler_text is None:
        lines += [f"100644 blob {i + 10**6:040x}\t{i + 1:040x}" for i in range(filler)]
        flags = ("--missing",)
    else:
        blob = git(world.w, "hash-object", "-w", "--stdin", stdin=filler_text)
        lines += [f"100644 blob {blob}\t{i + 1:040x}" for i in range(filler)]
        flags = ()
    tree = git(world.w, "mktree", *flags, stdin="\n".join(lines) + "\n")
    git(world.w, "update-ref", "refs/notes/board", git(world.w, "commit-tree", tree, "-m", "notes"))


def _spy_cat_file(monkeypatch) -> list[str]:
    """Every object spec the reader hands to a `cat-file --batch` (one per line of its stdin)."""
    asked: list[str] = []
    real = be._run

    def spy(repo, *args, **kw):
        if args[:1] == ("cat-file",) and "--batch" in args:
            asked.extend(x for x in (kw.get("stdin") or b"").decode().splitlines() if x)
        return real(repo, *args, **kw)

    monkeypatch.setattr(be, "_run", spy)
    return asked


def test_only_the_notes_of_commits_the_logs_return_are_read(world, monkeypatch):
    """codex MED: thousands of notes on commits no log returns are never requested from git (they are MISSING objects here,
    so asking for one would fail the read), and the one note on a logged commit is read."""
    name = "seat/1112-40-a"
    world.branch(name)
    b = world.commit("own", file="own.txt")
    world.push(name)
    _notes_tree(world, {b: "Board-Item: levain#7\n"}, 3000)
    asked = _spy_cat_file(monkeypatch)
    ev, _ = read(world)
    assert [x["items"] for x in ev["branches"]] == [["levain#7"]] and ev["unreadable"] == {}
    assert len(asked) == 1 and b in asked[0]


def test_a_repo_with_a_notes_ref_and_no_note_on_a_logged_commit_asks_git_for_no_blob(world, monkeypatch):
    _notes_tree(world, {}, 50)
    asked = _spy_cat_file(monkeypatch)
    ev, _ = read(world)
    assert ev["unreadable"] == {} and asked == []


def test_a_notes_tree_over_the_entry_cap_fails_closed_and_names_the_cap(world, monkeypatch):
    _notes_tree(world, {}, 30)
    monkeypatch.setattr(be, "MAX_NOTES_ENTRIES", 10)
    ev, _ = read(world)
    assert set(ev["unreadable"]) == {"main", "chain", "branches", "refs", "unattributed"}
    assert all("MAX_NOTES_ENTRIES" in why for why in ev["unreadable"].values())
    assert not {"main", "chain", "branches", "refs", "unattributed"} & set(ev)


def test_a_tree_exactly_at_the_entry_cap_reads(world, monkeypatch):
    _notes_tree(world, {}, 10)
    monkeypatch.setattr(be, "MAX_NOTES_ENTRIES", 10)
    assert read(world)[0]["unreadable"] == {}


def test_notes_on_logged_commits_over_the_byte_cap_fail_closed_and_name_the_cap(world, monkeypatch):
    """Even the notes that DO matter are bounded: their total size, measured by git before any blob is read."""
    name = "seat/1112-40-a"
    world.branch(name)
    b = world.commit("own", file="own.txt")
    world.push(name)
    _notes_tree(world, {b: "Board-Item: levain#7\n" + "x" * 500 + "\n"}, 0)
    monkeypatch.setattr(be, "MAX_NOTES_BYTES", 100)
    asked = _spy_cat_file(monkeypatch)
    ev, _ = read(world)
    assert set(ev["unreadable"]) == {"main", "chain", "branches", "refs", "unattributed"}
    assert all("MAX_NOTES_BYTES" in why for why in ev["unreadable"].values())
    assert asked == []                                                  # the cap was applied from the sizes: no blob was read


def test_a_notes_ref_that_is_not_a_tree_or_commit_is_the_same_fail_closed_path(world):
    """complement LOW: a notes ref at a blob makes `ls-tree` fail; it names every source unreadable (it is not an exception)."""
    blob = git(world.w, "hash-object", "-w", "--stdin", stdin="not a notes tree\n")
    git(world.w, "update-ref", "refs/notes/board", blob)
    ev, _ = read(world)
    assert set(ev["unreadable"]) == {"main", "chain", "branches", "refs", "unattributed"}
    assert all("refs/notes/board" in why for why in ev["unreadable"].values())


# ------------------------------------------------------------------------------------------------ 5. a CRLF revert body
def test_a_revert_whose_body_has_crlf_line_ends_is_a_revert(world):
    """complement LOW: `\\.$` under re.M does not match before a `\\r`; some web tooling writes CRLF."""
    c = world.commit("close it", trailers=("Board-Closes: levain#1",))
    world.n += 1
    (world.w / "d.txt").write_text("x\n")
    git(world.w, "add", "-A")
    git(world.w, "commit", "-q", "--cleanup=verbatim", "-F", "-", stdin=f'Revert "close it"\r\n\r\nThis reverts commit {c}.\r\n', date=DAY)
    assert _closers(world) == set()


def test_a_merge_revert_whose_body_has_crlf_line_ends_is_a_revert(world):
    """The merge form carries two line ends (`, reversing` and the final period); both may be CRLF."""
    _c, m1, merge = _merged_close(world)
    p1 = git(world.w, "rev-parse", f"{merge}^1")
    world.n += 1
    (world.w / "d.txt").write_text("x\n")
    git(world.w, "add", "-A")
    git(world.w, "commit", "-q", "--cleanup=verbatim", "-F", "-", date="2026-11-12T14:00:00Z",
        stdin=f'Revert "merge side"\r\n\r\nThis reverts commit {merge}, reversing\r\nchanges made to {p1}.\r\n')
    assert _closers(world) == {m1}


# ------------------------------------------------------------------------------------------------ 6. a cut main log
def _skewed_merge(world):
    """main: root -> T(old) -> S(closes 1) -> B(OLD: a clock behind S) -> A(closes 3) -> P; side: S -> X(closes 2); M = merge side.
    S is an ancestor of the mainline P through B; with B omitted from a cut log, S is reachable only from the merge's side."""
    t = world.commit("t", date="2026-10-06T12:00:00Z")
    s = world.commit("s", trailers=("Board-Closes: levain#1",), date="2026-11-12T10:00:00Z")
    git(world.w, "checkout", "-q", "-b", "side")
    x = world.commit("x", trailers=("Board-Closes: levain#2",), date="2026-11-12T11:00:00Z")
    git(world.w, "checkout", "-q", "main")
    world.commit("b", date=OLD)
    a = world.commit("a", trailers=("Board-Closes: levain#3",), date="2026-11-12T11:30:00Z")
    world.commit("p", date="2026-11-12T12:00:00Z")
    git(world.w, "merge", "-q", "--no-ff", "-m", "merge side", "side", date="2026-11-12T13:00:00Z")
    merge = git(world.w, "rev-parse", "HEAD")
    git(world.w, "revert", "--no-edit", "-m", "1", merge, date="2026-11-12T14:00:00Z")
    return t, s, x, a


def test_a_whole_log_takes_back_what_only_the_merge_brought_in(world):
    """The control: S is on the mainline (through B), X only on the side: the revert takes X back and keeps S."""
    _t, s, _x, a = _skewed_merge(world)
    assert _closers(world) == {s, a}


def test_a_cut_log_takes_back_only_the_merge_it_cannot_tell_what_the_merge_brought_in(world, monkeypatch):
    """complement LOW: with B omitted by the cap, S is reachable in the log only from the side, so the ancestry difference
    would take S back (S's close is on the mainline). A cut log takes back the merge itself and nothing else."""
    _t, s, x, a = _skewed_merge(world)
    monkeypatch.setattr(be, "MAX_MAIN_COMMITS", 6)
    monkeypatch.setattr(be, "MIN_PREWINDOW_TAIL", 1)
    world.push_main()
    ev, _ = read(world)
    assert ev["unreadable"] == {}
    assert {r["sha"] for r in ev["main"]} == {s, x, a}
