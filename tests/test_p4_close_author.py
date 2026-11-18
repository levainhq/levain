"""P4 (levain#103): the author of a close, derived from git. Every close gets a verdict and none is refused for its author.

Real git, real ssh-keygen keys (throwaway, under tmp), a real signed authority log in a scratch cockpit (the consent queue
tests' ``Key`` and ``Log``). Nothing touches the operator's keys, agent or authority log."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from levain.cli import main
from levain.desk import board_author as ba
from levain.desk import board_context as bc
from levain.desk import board_producers
from levain.desk import board_store as bs
from tests._board_git_world import World
from tests._board_git_world import git as world_git
from tests.test_consent_queue import Key, Log

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
T1 = "2026-11-05T12:00:00+00:00"
T2 = datetime(2026, 11, 10, tzinfo=timezone.utc)
T3 = "2026-11-12T12:00:00+00:00"
OUTSIDE_TEXT = "unattributed: the path exists outside version control"


class Scratch:
    def __init__(self, root: Path) -> None:
        kd = root / "keys"
        kd.mkdir()
        self.keys = {n: Key(kd, n) for n in ("laptop", "old", "stranger")}
        self.cockpit = root / "cockpit"
        self.cockpit.mkdir()
        log = Log(self.cockpit, self.keys)
        log.link({"op": "enrol", "fingerprint": self.keys["laptop"].fp, "public_key": self.keys["laptop"].public_key,
                  "class": "laptop-presence", "custody": "passphrase"}, at=T0)
        log.link({"op": "enrol", "fingerprint": self.keys["old"].fp, "public_key": self.keys["old"].public_key,
                  "class": "display-phone", "custody": "unstated"}, at=T0)
        log.link({"op": "revoke_key", "fingerprint": self.keys["old"].fp}, at=T2)
        self.repo = root / "repo"
        self.repo.mkdir()
        self.outside = root / "elsewhere.md"
        self.outside.write_text("not in git")
        self.git("init", "-q", "-b", "main")

    def git(self, *args, env=None):
        e = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, **(env or {}))
        cp = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True, env=e)
        assert cp.returncode == 0, cp.stderr
        return cp.stdout.strip()

    def commit(self, msg, who, when, key=None, files=None) -> str:
        for name, text in (files or {}).items():
            p = self.repo / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        self.git("add", "-A")
        env = {"GIT_AUTHOR_NAME": who, "GIT_AUTHOR_EMAIL": "x@x", "GIT_COMMITTER_NAME": who, "GIT_COMMITTER_EMAIL": "x@x",
               "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
        pre = ["-c", "gpg.format=ssh", "-c", f"user.signingkey={key.path}", "-c", "commit.gpgsign=true"] if key else []
        self.git(*pre, "commit", "--allow-empty", "-q", "-m", msg, env=env)
        return self.git("rev-parse", "HEAD")

    def authority(self) -> ba.CockpitAuthority:
        return ba.CockpitAuthority(self.cockpit, anchor=None)


@pytest.fixture(scope="module")
def sc(tmp_path_factory):
    s = Scratch(tmp_path_factory.mktemp("p4"))
    s.commit("base", "Plain Person", T1, files={"README": "x"})
    s.c = {}
    s.c["signed"] = s.commit("signed\n\nBoard-Closes: scratch#1", "Op Person", T1, key=s.keys["laptop"], files={"a": "1"})
    s.c["stranger"] = s.commit("stranger\n\nBoard-Closes: scratch#2", "Stranger Person", T1, key=s.keys["stranger"], files={"b": "1"})
    s.c["seat"] = s.commit("seat\n\nBoard-Closes: scratch#3\nBoard-Seat: slot-key-77", "Seat Person", T1, files={"c": "1"})
    s.c["plain"] = s.commit("plain\n\nBoard-Closes: scratch#4", "Plain Person", T1, files={"d": "1"})
    s.c["noted"] = s.commit("annotated, no trailer", "Annotated Author", T1, files={"e": "1"})
    s.c["old_before"] = s.commit("old before\n\nBoard-Closes: scratch#9", "Old Person", T1, key=s.keys["old"], files={"f": "1"})
    s.c["old_after"] = s.commit("old after\n\nBoard-Closes: scratch#10", "Old Person", T3, key=s.keys["old"], files={"g": "1"})
    s.c["stranger_seat"] = s.commit("stranger with seat\n\nBoard-Closes: scratch#11\nBoard-Seat: slot-key-77", "Stranger Person", T1,
                                    key=s.keys["stranger"], files={"h": "1"})
    s.c["signed_seat"] = s.commit("signed with seat\n\nBoard-Closes: scratch#12\nBoard-Seat: slot-key-77", "Op Person", T1,
                                  key=s.keys["laptop"], files={"i": "1"})
    s.c["two_seats"] = s.commit("two seats\n\nBoard-Closes: scratch#13\nBoard-Seat: slot-key-77\nBoard-Seat: slot-key-78", "Two Person", T1,
                                files={"j": "1"})
    s.c["unknown_seat"] = s.commit("unknown seat\n\nBoard-Closes: scratch#14\nBoard-Seat: nobody", "Unknown Person", T1,
                                   files={"k": "1"})
    s.c["doc"] = s.commit("adds the doc", "Doc Person", T1, files={"docs/a.md": "doc"})
    s.c["dir1"] = s.commit("adds dir file 1", "Dir One", T1, files={"outdir/one.txt": "1"})
    s.c["dir2"] = s.commit("adds dir file 2", "Dir Two", T1, files={"outdir/two.txt": "2"})
    s.c["doc_edit"] = s.commit("edits the doc", "Editor", T1, files={"docs/a.md": "doc v2"})
    s.git("update-ref", "refs/remotes/origin/main", "HEAD")
    # the note on c["noted"], written LATER by another commit on refs/notes/board by another person
    note_env = {"GIT_AUTHOR_NAME": "Note Writer", "GIT_AUTHOR_EMAIL": "n@n", "GIT_COMMITTER_NAME": "Note Writer",
                "GIT_COMMITTER_EMAIL": "n@n"}
    s.git("notes", "--ref=refs/notes/board", "add", "-m", "Board-Closes: scratch#5", s.c["noted"], env=note_env)
    return s


SEATS = {"slot-key-77": "worker L-77"}
FULL = lambda sc: ba.Resolvers(sc.authority(), SEATS.get)          # noqa: E731


def item(n: int, kind: str = "merged", done: str | None = None):
    return SimpleNamespace(id=f"scratch#{n}", done_kind=kind, done=done)


def verdict(sc, it, ref, res=None):
    return ba.closer_of_item(sc.repo, it, ref, res or FULL(sc))


# ------------------------------------------------------------------------------------------------ tier 1
def test_tier1_a_valid_signature_by_a_key_in_the_log_is_the_operator(sc):
    c = verdict(sc, item(1), sc.c["signed"])
    assert (c.tier, c.how, c.commit) == ("signed", "signed commit", sc.c["signed"])
    assert c.who.startswith("operator k") and c.note == ""
    assert ba.describe(c) == f"{c.who}, signed commit"


def test_tier1_the_same_key_not_in_the_log_falls_to_the_git_author_and_says_why(sc):
    c = verdict(sc, item(2), sc.c["stranger"])
    assert (c.tier, c.who) == ("git", "Stranger Person") and "not valid in the authority log" in c.note


def test_tier1_a_signed_commit_with_no_authority_named_is_not_tier1(sc):
    c = verdict(sc, item(1), sc.c["signed"], ba.Resolvers(None, SEATS.get))
    assert (c.tier, c.who) == ("git", "Op Person") and "no authority log was named" in c.note


def test_tier1_beats_a_copied_trailer(sc):
    c = verdict(sc, item(12), sc.c["signed_seat"])
    assert c.tier == "signed"


def test_tier1_a_key_revoked_after_the_commit_date_reads_signed_with_the_caveat(sc):
    c = verdict(sc, item(9), sc.c["old_before"])
    assert c.tier == "signed"
    assert "revoked at 2026-11-10T00:00:00Z" in c.note and "signer's claim" in c.note


def test_tier1_a_key_revoked_before_the_commit_date_is_not_tier1(sc):
    c = verdict(sc, item(10), sc.c["old_after"])
    assert (c.tier, c.who) == ("git", "Old Person")


def test_tier1_an_unreadable_authority_log_is_said_and_never_refuses(sc, tmp_path):
    res = ba.Resolvers(ba.CockpitAuthority(tmp_path / "no-cockpit", anchor=None), SEATS.get)
    c = verdict(sc, item(1), sc.c["signed"], res)
    assert c.tier == "git" and "authority log cannot be read" in c.note


# ------------------------------------------------------------------------------------------------ tier 2
def test_tier2_a_board_seat_trailer_a_launch_record_maps_is_the_agent_asserted(sc):
    c = verdict(sc, item(3), sc.c["seat"])
    assert (c.tier, c.who, c.how) == ("trailer", "agent worker L-77", "asserted (trailer)")
    assert ba.describe(c) == "agent worker L-77, asserted (trailer)"


def test_tier2_a_signature_by_a_key_outside_the_log_still_reads_the_trailer(sc):
    c = verdict(sc, item(11), sc.c["stranger_seat"])
    assert c.tier == "trailer" and "not valid in the authority log" in c.note


def test_tier2_a_trailer_no_launch_record_maps_falls_to_git(sc):
    c = verdict(sc, item(14), sc.c["unknown_seat"])
    assert (c.tier, c.who) == ("git", "Unknown Person") and "maps to no launch record" in c.note


def test_tier2_with_no_launch_records_named_the_trailer_is_unresolved(sc):
    c = verdict(sc, item(3), sc.c["seat"], ba.Resolvers(sc.authority(), None))
    assert c.tier == "git" and "--desk-dir" in c.note


def test_tier2_two_different_trailers_read_none(sc):
    c = verdict(sc, item(13), sc.c["two_seats"])
    assert c.tier == "git" and "several different Board-Seat trailers" in c.note


# ------------------------------------------------------------------------------------------------ tier 3
def test_tier3_a_plain_commit_is_the_git_author(sc):
    c = verdict(sc, item(4), sc.c["plain"])
    assert (c.tier, c.who, c.how, c.note) == ("git", "Plain Person", "asserted by git author", "")
    assert ba.describe(c) == "asserted by git author Plain Person"


# ------------------------------------------------------------------------------------------------ where the close comes from
def test_a_note_injected_close_takes_the_author_of_the_note_commit_not_the_annotated_commit(sc):
    c = verdict(sc, item(5), sc.c["noted"])
    notes_tip = sc.git("rev-parse", "refs/notes/board")
    assert (c.tier, c.who, c.commit) == ("git", "Note Writer", notes_tip)
    assert c.who != "Annotated Author"


def test_a_note_close_whose_writer_cannot_be_found_is_unattributed_never_the_annotated_author(sc):
    c = verdict(sc, item(77), sc.c["plain"])        # scratch#77 is named by no trailer and no note on this commit
    assert c.tier == "unattributed" and c.who is None and "was not found" in c.how


def test_a_document_close_takes_the_first_first_parent_commit_that_added_the_path(sc):
    c = verdict(sc, item(6, "document", "docs/a.md"), "docs/a.md")
    assert (c.tier, c.who, c.commit) == ("git", "Doc Person", sc.c["doc"])      # not "Editor", who edited it later


def test_a_directory_close_takes_the_first_commit_that_added_a_file_under_it(sc):
    c = verdict(sc, item(7, "run", "outdir"), "outdir")
    assert (c.who, c.commit) == ("Dir One", sc.c["dir1"])


def test_a_done_path_outside_git_is_unattributed_and_reported_never_refused(sc):
    c = verdict(sc, item(8, "document", str(sc.outside)), str(sc.outside))
    assert (c.tier, c.who, c.how, c.commit) == ("unattributed", None, OUTSIDE_TEXT, None)
    assert ba.describe(c) == OUTSIDE_TEXT


def test_a_path_in_the_tree_that_main_never_added_is_outside_version_control(sc):
    (sc.repo / "untracked.md").write_text("x")
    try:
        c = verdict(sc, item(8, "document", "untracked.md"), "untracked.md")
    finally:
        (sc.repo / "untracked.md").unlink()
    assert c.tier == "unattributed" and c.how == OUTSIDE_TEXT


# ------------------------------------------------------------------------------------------------ the surface
def test_annotate_sets_closed_by_on_every_done_git_close_and_nothing_else(sc):
    items = {"scratch#1": item(1), "scratch#6": item(6, "document", "docs/a.md"), "scratch#20": item(20, "band", "np-x"),
             "scratch#21": item(21)}
    doc = {"items": [{"id": "scratch#1", "repo": "scratch", "state": "done", "refs": [sc.c["signed"]]},
                     {"id": "scratch#6", "repo": "scratch", "state": "done", "refs": ["docs/a.md"]},
                     {"id": "scratch#20", "repo": "scratch", "state": "done", "refs": ["np-x"]},
                     {"id": "scratch#21", "repo": "scratch", "state": "ready", "refs": []}]}
    n = ba.annotate(doc, items, {"scratch": sc.repo}, FULL(sc))
    by = {s["id"]: s.get("closed_by") for s in doc["items"]}
    assert n == 2 and by["scratch#1"]["tier"] == "signed" and by["scratch#6"]["who"] == "Doc Person"
    assert by["scratch#20"] is None and by["scratch#21"] is None
    assert set(by["scratch#1"]) == {"tier", "who", "how", "commit"}


def test_annotate_never_raises_for_a_git_trouble_it_says_it_on_the_item(sc, tmp_path):
    items = {"scratch#1": item(1)}
    doc = {"items": [{"id": "scratch#1", "repo": "scratch", "state": "done", "refs": [sc.c["signed"]]}]}
    ba.annotate(doc, items, {"scratch": tmp_path / "not-a-repo"}, FULL(sc))
    c = doc["items"][0]["closed_by"]
    assert c["tier"] == "unattributed" and "could not be read" in c["how"]


def test_the_context_line_and_the_state_part(sc):
    st = SimpleNamespace(state="done", refs=(sc.c["plain"],), column="done", reason="closed")
    line = ba.closer_line(sc.repo, item(4), st, FULL(sc))
    assert line == "closed by: asserted by git author Plain Person"
    assert ba.closer_line(sc.repo, item(4), SimpleNamespace(state="ready", refs=()), FULL(sc)) is None
    derived = bc.Derived({"scratch#4": SimpleNamespace(state="done", column="done", reason="closed by x")}, None)
    part = bc._state_part("scratch#4", derived, "", line)
    assert part.lines[1] == line


# ------------------------------------------------------------------------------------------------ the CLI, end to end
@pytest.fixture(autouse=True)
def native_stub(monkeypatch):
    def stub(*, now, store_id, no_land_waiter=False, **_kw):
        out = {s: board_producers._block(now, store_id, [], None) for s in ("seats", "band")}
        if no_land_waiter:
            out["waiters"] = board_producers._block(now, store_id, [], None)
        return out
    monkeypatch.setattr(board_producers, "native_blocks", stub)
    monkeypatch.setattr(bs, "_check_repo_mode", lambda store_dir, repo, key: None)


def _items_file(tmp: Path, *rows) -> Path:
    lines = [json.dumps({"v": 1, "op": "add", "id": f"levain#{n}", "at": "2026-11-12T10:00:00Z", "by": "t", "repo": "levain",
                         "title": f"item {n}", "words": "", "origin": "desk 1", "done": done, "done_kind": kind, "tier": "sonnet",
                         "depends_on": []}) for n, kind, done in rows]
    p = tmp / "items.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _extra(tmp: Path) -> Path:
    p = tmp / "extra.json"
    p.write_text(json.dumps({"seats": [], "band": {"open": {}, "resolved": {}}, "waiters": []}), encoding="utf-8")
    return p


def test_board_state_json_carries_closed_by_for_a_done_item_and_the_table_shows_it(tmp_path, capsys):
    w = World(tmp_path)
    w.commit("root", date="2026-10-01T12:00:00Z")
    w.push_main()
    w.commit("land one", trailers=("Board-Item: levain#1", "Board-Closes: levain#1"))
    (w.w / "docs").mkdir()
    (w.w / "docs" / "r.md").write_text("report")
    world_git(w.w, "add", "-A")
    world_git(w.w, "commit", "-q", "-m", "adds report\n\nBoard-Item: levain#2", date="2026-11-12T12:30:00Z")
    w.push_main()
    items = _items_file(tmp_path, (1, "merged", "the work lands"), (2, "document", "docs/r.md"), (3, "merged", "not yet"))
    argv = ["--items", str(items), "--evidence-extra", str(_extra(tmp_path)), "--repo", str(w.w), "--since", "2026-11-12T00:00:00Z"]
    rc = main(["board", "state", "--json", *argv])
    doc = json.loads(capsys.readouterr().out)
    by = {s["id"]: s for s in doc["items"]}
    assert by["levain#1"]["state"] == "done" and by["levain#1"]["closed_by"]["who"] == "t"
    assert by["levain#1"]["closed_by"]["tier"] == "git" and by["levain#1"]["closed_by"]["commit"] == by["levain#1"]["refs"][0]
    assert by["levain#2"]["closed_by"]["tier"] == "git" and by["levain#3"].get("closed_by") is None
    rc2 = main(["board", "state", *argv])
    table = capsys.readouterr().out
    assert rc == rc2 and "asserted by git author t" in table


def test_board_state_with_the_two_flags_reads_tier1_and_tier2(sc, tmp_path, capsys, monkeypatch):
    """The flags build the resolvers: --data-root an authority log, --desk-dir launch records (a stand-in lookup here: the
    launch store has its own tests)."""
    monkeypatch.setattr(ba, "_desk_seat_of", lambda directory: SEATS.get)
    scratch_auth = sc.authority()
    monkeypatch.setattr(ba, "CockpitAuthority", lambda root: scratch_auth)      # a scratch log has no anchor file
    args = SimpleNamespace(data_root=sc.cockpit, desk_dir=tmp_path)
    res = ba.resolvers_from_args(args)
    assert res.authority is not None and res.seat_of("slot-key-77") == "worker L-77"
    assert verdict(sc, item(1), sc.c["signed"], res).tier == "signed"
    assert verdict(sc, item(3), sc.c["seat"], res).tier == "trailer"
    none = ba.resolvers_from_args(SimpleNamespace())
    assert none.authority is None and none.seat_of is None


def test_a_real_launch_record_in_a_scratch_desk_store_maps_the_durable_key_to_a_seat(sc, tmp_path):
    from levain.desk.store import CoordStore

    qdir = tmp_path / "desk"
    qdir.mkdir(mode=0o700)
    store = CoordStore(qdir)
    store.coord_set_agent("claude", cwd=str(tmp_path))
    store.accept_drop("fix the build", surface="cli")
    ln = store.coord_status()["launches"][0]
    seat_of = ba._desk_seat_of(qdir)
    key = store.coord_status()["launches"][0]["session_key"]
    assert seat_of(key) == f"{ln['role']} {ln['launch_id']}"
    assert seat_of("no-such-key") is None
    assert ba._desk_seat_of(tmp_path / "absent")(key) is None       # a store that cannot be read resolves nothing, never raises
    c = verdict(sc, item(3), sc.c["seat"], ba.Resolvers(None, ba._desk_seat_of(qdir)))     # Board-Seat slot-key-77: no such launch
    assert c.tier == "git" and "maps to no launch record" in c.note
