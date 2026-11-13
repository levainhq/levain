"""The board's git evidence readers (slice B2) against REAL temporary git repositories.

Every test here is a world made with real git (a bare origin, a clone, commits at fixed dates): the readers are git
parsers, so a mock of git would test the mock. The contract under test is B1's: every record carries every required field
(an omitted ``items`` on an empty branch, seat or commit makes the WHOLE source unreadable), ``ahead`` is counted by the
reader by work and never by tip sha, and a source git cannot read is named, never read as empty."""
from __future__ import annotations

import os
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


def test_a_squash_with_the_same_trailer_and_subject_counts_as_landed(world):
    world.branch("seat/1112-40-i7-a")
    world.commit("the work", trailers=("Board-Item: levain#7",), file="a.txt", body="alpha\n")
    git(world.w, "checkout", "-q", "main")
    world.commit("the work", trailers=("Board-Item: levain#7",), file="z.txt", body="different diff\n")   # patch-id differs
    world.push_main()
    assert branch(read(world)[0], "seat/1112-40-i7-a")["ahead"] == 0


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
