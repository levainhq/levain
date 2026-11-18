"""P4 (levain#103): the author of a close, derived from git. Every close gets a verdict and none is refused for its author.

Real git, real ssh-keygen keys (throwaway, under tmp), a real signed authority log in a scratch cockpit (the consent queue
tests' ``Key`` and ``Log``). Nothing touches the operator's keys, agent or authority log."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
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
OUTSIDE_TEXT = ba.OUTSIDE


class Scratch:
    def __init__(self, root: Path) -> None:
        kd = root / "keys"
        kd.mkdir()
        self.keys = {n: Key(kd, n) for n in ("laptop", "old", "stranger", "phone", "late")}
        self.cockpit = root / "cockpit"
        self.cockpit.mkdir()
        Log(self.cockpit, self.keys)                       # the cockpit journal; the links below are anchored, as a real one is
        self._link({"op": "enrol", "fingerprint": self.keys["laptop"].fp, "public_key": self.keys["laptop"].public_key,
                    "class": "laptop-presence", "custody": "passphrase"}, T0)
        self._link({"op": "enrol", "fingerprint": self.keys["old"].fp, "public_key": self.keys["old"].public_key,
                    "class": "laptop-presence", "custody": "passphrase"}, T0)
        self._link({"op": "enrol", "fingerprint": self.keys["phone"].fp, "public_key": self.keys["phone"].public_key,
                    "class": "display-phone", "custody": "unstated"}, T0)
        self._link({"op": "revoke_key", "fingerprint": self.keys["old"].fp}, T2)
        self._link({"op": "enrol", "fingerprint": self.keys["late"].fp, "public_key": self.keys["late"].public_key,
                    "class": "laptop-presence", "custody": "passphrase"}, T2)
        self.repo = root / "repo"
        self.repo.mkdir()
        self.outside = root / "elsewhere.md"
        self.outside.write_text("not in git")
        self.git("init", "-q", "-b", "main")

    def _link(self, op, at) -> None:
        import sqlite3

        from levain import authority as auth
        from levain.authority import ReplayCache, anchor_path

        signer = self.keys["laptop"]
        conn = sqlite3.connect(self.cockpit / "cockpit.db", isolation_level=None)
        try:
            auth.sign_and_append(conn, op, signer_fp=signer.fp, sign=lambda m: signer.sign(m, auth.NAMESPACE),
                                 clock=lambda: at, cache=ReplayCache(), anchor=anchor_path(self.cockpit))
        finally:
            conn.close()

    def git(self, *args, env=None):
        e = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, **(env or {}))
        cp = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True, env=e)
        assert cp.returncode == 0, cp.stderr
        return cp.stdout.strip()

    def commit(self, msg, who, when, key=None, files=None, *, author_date=None, committer=None) -> str:
        """``when`` is the COMMITTER date; the author date defaults to an hour before it, so a reader that took the author
        date for the committer date would be graded wrong (``author_date`` sets it). ``committer`` (name, email) differs
        from the author when given (a rebase by another person)."""
        for name, text in (files or {}).items():
            p = self.repo / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        self.git("add", "-A")
        adate = author_date or (datetime.fromisoformat(when) - timedelta(hours=1)).isoformat()
        cname, cmail = committer or (who, "x@x")
        env = {"GIT_AUTHOR_NAME": who, "GIT_AUTHOR_EMAIL": "x@x", "GIT_COMMITTER_NAME": cname, "GIT_COMMITTER_EMAIL": cmail,
               "GIT_AUTHOR_DATE": adate, "GIT_COMMITTER_DATE": when}
        pre = ["-c", "gpg.format=ssh", "-c", f"user.signingkey={key.path}", "-c", "commit.gpgsign=true"] if key else []
        self.git(*pre, "commit", "--allow-empty", "-q", "-m", msg, env=env)
        return self.git("rev-parse", "HEAD")

    def authority(self) -> ba.CockpitAuthority:
        return ba.CockpitAuthority(self.cockpit)


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
    s.c["phone"] = s.commit("phone\n\nBoard-Closes: scratch#40", "Phone Person", T1, key=s.keys["phone"], files={"m": "1"})
    s.c["late"] = s.commit("late key\n\nBoard-Closes: scratch#41", "Late Person", T1, key=s.keys["late"], files={"n": "1"})
    # author date BEFORE the laptop key's enrolment (T0), committer date after it: the committer date is the one read
    s.c["adate_early"] = s.commit("author date early\n\nBoard-Closes: scratch#42", "Op Person", T1, key=s.keys["laptop"],
                                  author_date="2026-10-20T12:00:00+00:00", files={"o": "1"})
    # author date AFTER the late key's enrolment (T2), committer date before it: not tier 1
    s.c["adate_late"] = s.commit("author date late\n\nBoard-Closes: scratch#43", "Late Person", T1, key=s.keys["late"],
                                 author_date=T3, files={"p": "1"})
    s.c["rebased"] = s.commit("rebased\n\nBoard-Closes: scratch#44", "Agent Author", T1, committer=("Re Baser", "r@r"),
                              files={"q": "1"})
    s.c["noted2"] = s.commit("annotated twice, no trailer", "Annotated Author", T1, files={"r": "1"})
    s.git("update-ref", "refs/remotes/origin/main", "HEAD")
    # the note on c["noted"], written LATER by another commit on refs/notes/board by another person
    note_env = {"GIT_AUTHOR_NAME": "Note Writer", "GIT_AUTHOR_EMAIL": "n@n", "GIT_COMMITTER_NAME": "Note Writer",
                "GIT_COMMITTER_EMAIL": "n@n"}
    s.git("notes", "--ref=refs/notes/board", "add", "-m", "Board-Closes: scratch#5", s.c["noted"], env=note_env)
    # the note on c["noted2"]: written by one person, removed by another, written again by a third (latest addition wins)
    for who, text in (("First Writer", "Board-Closes: scratch#30"), ("Remover", "Board-Item: scratch#30"),
                      ("Second Writer", "Board-Closes: scratch#30")):
        env = {"GIT_AUTHOR_NAME": who, "GIT_AUTHOR_EMAIL": "w@w", "GIT_COMMITTER_NAME": who, "GIT_COMMITTER_EMAIL": "w@w"}
        s.git("notes", "--ref=refs/notes/board", "add", "-f", "-m", text, s.c["noted2"], env=env)
    return s


def seats(sc, items=("scratch#3", "scratch#11", "scratch#12", "scratch#13", "scratch#14"), cwd=None):
    """A stand-in launch lookup: slot-key-77 is launch L-77, which claimed ``items`` in ``cwd`` (default: the scratch repo)."""
    table = {"slot-key-77": ba.Seat("worker L-77", None if items is None else tuple(items), str(cwd or sc.repo))}
    return table.get


FULL = lambda sc: ba.Resolvers(sc.authority(), seats(sc))          # noqa: E731


def item(n: int, kind: str = "merged", done: str | None = None):
    return SimpleNamespace(id=f"scratch#{n}", repo="scratch", done_kind=kind, done=done)


def verdict(sc, it, ref, res=None):
    return ba.closer_of_item(sc.repo, it, ref, res or FULL(sc))


# ------------------------------------------------------------------------------------------------ tier 1
def test_tier1_a_valid_signature_by_a_key_in_the_log_is_the_operator(sc):
    c = verdict(sc, item(1), sc.c["signed"])
    assert (c.tier, c.how, c.commit) == ("signed", "signed by an operator key", sc.c["signed"])
    assert c.who.startswith("k") and " " not in c.who and c.note == ""
    assert ba.describe(c) == f"signed by an operator key {c.who}"
    assert set(c.to_dict()) == {"tier", "who", "how", "commit"}


def test_tier1_the_same_key_not_in_the_log_falls_to_the_git_author_and_says_why(sc):
    c = verdict(sc, item(2), sc.c["stranger"])
    assert (c.tier, c.who) == ("git", "Stranger Person") and "is not a key in the authority log" in c.note


def test_tier1_a_signed_commit_with_no_authority_named_is_not_tier1(sc):
    c = verdict(sc, item(1), sc.c["signed"], ba.Resolvers(None, seats(sc)))
    assert (c.tier, c.who) == ("git", "Op Person") and "no authority log is consulted" in c.note


def test_tier1_beats_a_copied_trailer(sc):
    c = verdict(sc, item(12), sc.c["signed_seat"])
    assert c.tier == "signed"


def test_tier1_a_revoked_key_is_never_credited_the_snapshot_carries_no_revocation_time(sc):
    """P2's reader gives the revoked seq and no time, so a commit by a now-revoked key cannot be placed before or after it:
    not tier 1 either way, and the note names the key's revocation (a verdict never stronger than the evidence)."""
    c = verdict(sc, item(9), sc.c["old_before"])
    assert (c.tier, c.who) == ("git", "Old Person")
    fp = sc.keys["old"].fp
    assert f"signed by {fp}, revoked at seq 4" in c.note and "no revocation time" in c.note


def test_tier1_a_revoked_key_is_not_tier1_whatever_the_commit_date(sc):
    c = verdict(sc, item(10), sc.c["old_after"])
    assert (c.tier, c.who) == ("git", "Old Person") and "revoked at seq 4" in c.note


def test_tier1_a_key_of_the_wrong_class_is_refused_by_the_boards_key_rule(sc):
    """A git commit is signed at a terminal: board_sign's key rule takes a laptop-presence key only (verify_record's)."""
    c = verdict(sc, item(40), sc.c["phone"])
    assert (c.tier, c.who) == ("git", "Phone Person")
    assert f"{sc.keys['phone'].fp} is not a terminal key" in c.note and "key rule refuses" in c.note


def test_tier1_a_quarantined_key_is_refused_by_the_boards_key_rule(sc):
    """The log's laptop key with its stored custody made ``passkey`` (an ed25519 key is not a passkey's material): the
    one quarantine definition holds it, so board_sign refuses it and so does tier 1."""
    import dataclasses

    from levain import authority as auth

    a = sc.authority()
    snap = a._load()
    fp = sc.keys["laptop"].fp
    held = dataclasses.replace(snap.keys[fp], custody=auth.PASSKEY)
    assert auth.quarantined([held])
    a._snap = SimpleNamespace(keys={**snap.keys, fp: held})
    c = verdict(sc, item(1), sc.c["signed"], ba.Resolvers(a, seats(sc)))
    assert (c.tier, c.who) == ("git", "Op Person") and f"{fp} is quarantined" in c.note


def test_tier1_a_key_enrolled_after_the_commit_date_is_not_tier1(sc):
    c = verdict(sc, item(41), sc.c["late"])
    assert (c.tier, c.who) == ("git", "Late Person")
    assert f"signed by {sc.keys['late'].fp}, enrolled 2026-11-10" in c.note and "after the commit's own date" in c.note


def test_tier1_reads_the_committer_date_never_the_author_date(sc):
    """Author date before the key's enrolment, committer date after it: signed. And the reverse: not signed. A switch to
    the author date fails both."""
    assert verdict(sc, item(42), sc.c["adate_early"]).tier == "signed"
    c = verdict(sc, item(43), sc.c["adate_late"])
    assert c.tier == "git" and "after the commit's own date 2026-11-05" in c.note


def test_tier1_a_key_with_no_enrol_time_says_so_never_enrolled_after(sc):
    import dataclasses

    a = sc.authority()
    snap = a._load()
    fp = sc.keys["laptop"].fp
    a._snap = SimpleNamespace(keys={**snap.keys, fp: dataclasses.replace(snap.keys[fp], enrolled_at=None,
                                                                         legacy_enrolled_at=None)})
    c = verdict(sc, item(1), sc.c["signed"], ba.Resolvers(a, seats(sc)))
    assert c.tier == "git" and "carries no readable time" in c.note and "enrolled after" not in c.note


def test_tier1_a_failed_authority_load_is_read_once_per_board_read(sc, tmp_path, monkeypatch):
    from levain.desk import board_sign as bsign

    calls = []
    real = bsign.resolve_authority
    monkeypatch.setattr(bsign, "resolve_authority", lambda **kw: (calls.append(1), real(**kw))[1])
    a = ba.CockpitAuthority(tmp_path / "no-cockpit")
    for _ in range(3):
        with pytest.raises(ba.NotChecked):
            a.credit(sc.keys["laptop"].fp, T2)
    assert len(calls) == 1


def test_tier1_a_signature_that_cannot_be_judged_is_said(sc, monkeypatch):
    from levain.team import signing

    monkeypatch.setattr(signing, "verify_commit", lambda repo, sha: signing.Verdict("indeterminate", None, "ssh-keygen gone"))
    c = verdict(sc, item(4), sc.c["plain"])
    assert c.tier == "git" and "signature could not be checked (ssh-keygen gone)" in c.note


def test_tier1_an_unreadable_authority_log_is_said_and_never_refuses(sc, tmp_path):
    res = ba.Resolvers(ba.CockpitAuthority(tmp_path / "no-cockpit"), seats(sc))
    c = verdict(sc, item(1), sc.c["signed"], res)
    assert c.tier == "git" and "authority log was not checked" in c.note
    assert "holds no cockpit.db" in c.note and "nothing was written" not in c.note      # the reason, never board_sign's prefix


def test_tier1_a_refusal_text_with_no_well_formed_parentheses_is_kept_whole(monkeypatch):
    from levain.desk import board_sign as bsign

    def refuse(**kw):
        raise bsign.bs.BoardError("odd) refusal (with no close")
    monkeypatch.setattr(bsign, "resolve_authority", refuse)
    with pytest.raises(ba.NotChecked, match=r"^odd\) refusal \(with no close$"):
        ba.CockpitAuthority("/nonexistent").credit("SHA256:x", T2)


# ------------------------------------------------------------------------------------------------ tier 2
def test_tier2_a_board_seat_trailer_a_launch_record_maps_is_the_agent_asserted(sc):
    c = verdict(sc, item(3), sc.c["seat"])
    assert (c.tier, c.who, c.how) == ("trailer", "agent worker L-77", "asserted (trailer)")
    assert ba.describe(c) == "agent worker L-77, asserted (trailer)"


def test_tier2_a_board_seat_copied_from_another_items_launch_is_not_read(sc):
    """The launch L-77 claimed scratch#99 only: its trailer on scratch#3's close names a seat that never claimed it."""
    c = verdict(sc, item(3), sc.c["seat"], ba.Resolvers(sc.authority(), seats(sc, items=("scratch#99",))))
    assert (c.tier, c.who) == ("git", "Seat Person")
    assert "maps to launch worker L-77, but the launch claimed scratch#99, not scratch#3" in c.note


def test_tier2_a_launch_that_claimed_no_items_is_not_read(sc):
    c = verdict(sc, item(3), sc.c["seat"], ba.Resolvers(sc.authority(), seats(sc, items=None)))
    assert c.tier == "git" and "the launch claimed no board items" in c.note


def test_tier2_a_launch_that_ran_in_another_repo_is_not_read(sc, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    c = verdict(sc, item(3), sc.c["seat"], ba.Resolvers(sc.authority(), seats(sc, cwd=other)))
    assert c.tier == "git" and "the launch ran in another repo" in c.note
    gone = verdict(sc, item(3), sc.c["seat"], ba.Resolvers(sc.authority(), seats(sc, cwd=tmp_path / "gone")))
    assert gone.tier == "git" and "is not a git repo this reader can read" in gone.note


def test_tier2_a_signature_by_a_key_outside_the_log_still_reads_the_trailer(sc):
    c = verdict(sc, item(11), sc.c["stranger_seat"])
    assert c.tier == "trailer" and "is not a key in the authority log" in c.note


def test_tier2_a_trailer_no_launch_record_maps_falls_to_git(sc):
    c = verdict(sc, item(14), sc.c["unknown_seat"])
    assert (c.tier, c.who) == ("git", "Unknown Person") and "maps to no launch record" in c.note


def test_tier2_with_no_launch_records_named_the_trailer_is_unresolved(sc):
    c = verdict(sc, item(3), sc.c["seat"], ba.Resolvers(sc.authority(), None))
    assert c.tier == "git" and "no launch records are consulted" in c.note


def test_tier2_two_different_trailers_read_none(sc):
    c = verdict(sc, item(13), sc.c["two_seats"])
    assert c.tier == "git" and "several different Board-Seat trailers" in c.note


# ------------------------------------------------------------------------------------------------ tier 3
def test_tier3_a_plain_commit_is_the_git_author(sc):
    c = verdict(sc, item(4), sc.c["plain"])
    assert (c.tier, c.who, c.how, c.note, c.email, c.committer) == ("git", "Plain Person", "asserted by git author", "",
                                                                    "x@x", None)
    assert ba.describe(c) == "asserted by git author Plain Person <x@x>"


def test_a_commit_rebased_by_another_person_names_the_committer(sc):
    c = verdict(sc, item(44), sc.c["rebased"])
    assert (c.tier, c.who, c.committer) == ("git", "Agent Author", "Re Baser <r@r>")
    assert ba.describe(c) == "asserted by git author Agent Author <x@x>, committed by Re Baser <r@r>"
    assert c.to_dict()["committer"] == "Re Baser <r@r>"


# ------------------------------------------------------------------------------------------------ where the close comes from
def test_a_note_injected_close_takes_the_author_of_the_note_commit_not_the_annotated_commit(sc):
    c = verdict(sc, item(5), sc.c["noted"])
    first_note = sc.git("log", "--reverse", "--format=%H", "refs/notes/board").split()[0]
    assert (c.tier, c.who, c.commit, c.close_commit) == ("git", "Note Writer", first_note, sc.c["noted"])
    assert c.who != "Annotated Author"


def test_a_note_line_written_removed_and_written_again_is_the_latest_writers(sc):
    """The board's close rule takes the latest close; the note's writer is the latest commit that ADDED the line."""
    c = verdict(sc, item(30), sc.c["noted2"])
    assert (c.who, c.close_commit) == ("Second Writer", sc.c["noted2"])
    assert c.commit == sc.git("rev-parse", "refs/notes/board")


def test_a_note_close_whose_writer_cannot_be_found_is_unattributed_never_the_annotated_author(sc):
    c = verdict(sc, item(77), sc.c["plain"])        # scratch#77 is named by no trailer and no note on this commit
    assert c.tier == "unattributed" and c.who is None and "was not found" in c.how


def test_a_document_close_takes_the_first_first_parent_commit_that_added_the_path(sc):
    c = verdict(sc, item(6, "document", "docs/a.md"), "docs/a.md")
    assert (c.tier, c.who, c.commit) == ("git", "Doc Person", sc.c["doc"])      # not "Editor", who edited it later
    assert c.how == "first added docs/a.md, asserted by git author"
    assert ba.describe(c) == "first added docs/a.md, asserted by git author Doc Person <x@x>"


def test_a_directory_close_takes_the_first_commit_that_added_a_file_under_it(sc):
    c = verdict(sc, item(7, "run", "outdir"), "outdir")
    assert (c.who, c.commit) == ("Dir One", sc.c["dir1"])


def test_a_done_path_outside_git_is_unattributed_and_reported_never_refused(sc):
    c = verdict(sc, item(8, "document", str(sc.outside)), str(sc.outside))
    assert (c.tier, c.who, c.how, c.commit) == ("unattributed", None, OUTSIDE_TEXT, None)
    assert ba.describe(c) == OUTSIDE_TEXT


def test_a_path_in_the_tree_that_main_never_added_says_exactly_that(sc):
    (sc.repo / "untracked.md").write_text("x")
    try:
        c = verdict(sc, item(8, "document", "untracked.md"), "untracked.md")
    finally:
        (sc.repo / "untracked.md").unlink()
    assert c.tier == "unattributed" and c.how == ba.NOT_ON_MAIN and "outside version control" not in c.how


def test_a_path_in_another_repos_tree_is_outside_this_repos_work_tree(sc, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    (other / "x.md").write_text("x")
    c = verdict(sc, item(8, "document", str(other / "x.md")), str(other / "x.md"))
    assert (c.tier, c.how) == ("unattributed", OUTSIDE_TEXT)


def test_a_symlink_in_the_tree_is_read_where_read_documents_reads_it(sc, tmp_path):
    """read_documents follows ``done`` as written (no realpath): a tracked symlink in the tree is the tree's path."""
    target = tmp_path / "real.md"
    target.write_text("x")
    link = sc.repo / "linked.md"
    link.symlink_to(target)
    try:
        sha = sc.commit("adds a link", "Link Person", T1)
        sc.git("update-ref", "refs/remotes/origin/main", "HEAD")
        c = verdict(sc, item(8, "document", "linked.md"), "linked.md")
    finally:
        link.unlink()
        sc.commit("removes the link", "Link Person", T1)
        sc.git("update-ref", "refs/remotes/origin/main", "HEAD")
    assert (c.who, c.commit) == ("Link Person", sha)


def _republish(sc):
    sc.git("update-ref", "refs/remotes/origin/main", "HEAD")


def test_a_symlink_to_a_directory_in_the_repo_credits_the_commit_that_added_the_link_not_the_targets_first_adder(sc):
    """The LITERAL tracked path is attributed: the link is probed as a file, never followed into realdir/."""
    target = sc.commit("adds realdir", "Target Person", T1, files={"realdir/f.txt": "1"})
    link = sc.repo / "dirlink"
    link.symlink_to(sc.repo / "realdir")
    try:
        added = sc.commit("adds dirlink", "Link Person", T1)
        _republish(sc)
        c = verdict(sc, item(8, "document", "dirlink"), "dirlink")
    finally:
        link.unlink()
        sc.commit("removes dirlink", "Link Person", T1)
        _republish(sc)
    assert added != target
    assert (c.who, c.commit) == ("Link Person", added)


def test_a_symlink_to_a_directory_outside_the_repo_credits_the_commit_that_added_the_link(sc, tmp_path):
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (outside / "f.txt").write_text("x")
    link = sc.repo / "outlink"
    link.symlink_to(outside)
    try:
        added = sc.commit("adds outlink", "Out Link Person", T1)
        _republish(sc)
        c = verdict(sc, item(8, "document", "outlink"), "outlink")
    finally:
        link.unlink()
        sc.commit("removes outlink", "Out Link Person", T1)
        _republish(sc)
    assert (c.tier, c.who, c.commit) == ("git", "Out Link Person", added)


def test_a_submodule_path_credits_the_superproject_commit_that_added_it_never_enters_the_submodule(sc):
    import shutil
    sub = sc.repo / "subm"
    sub.mkdir()
    for args in (("init", "-q", "-b", "main"), ("-c", "user.name=S", "-c", "user.email=s@s", "commit", "--allow-empty", "-q", "-m", "sub")):
        subprocess.run(["git", "-C", str(sub), *args], check=True, capture_output=True,
                       env=dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull))
    try:
        added = sc.commit("adds subm", "Sub Adder", T1)
        assert sc.git("ls-files", "-s", "subm").startswith("160000")
        _republish(sc)
        c = verdict(sc, item(8, "run", "subm"), "subm")
    finally:
        sc.git("rm", "-q", "--cached", "subm")
        shutil.rmtree(sub)
        sc.commit("removes subm", "Sub Adder", T1)
        _republish(sc)
    assert (c.tier, c.who, c.commit) == ("git", "Sub Adder", added)


def test_an_empty_git_author_email_is_kept_as_empty_not_dropped(sc):
    env = {"GIT_AUTHOR_NAME": "No Mail", "GIT_AUTHOR_EMAIL": "", "GIT_COMMITTER_NAME": "No Mail", "GIT_COMMITTER_EMAIL": "",
           "GIT_AUTHOR_DATE": T1, "GIT_COMMITTER_DATE": T1}
    sc.git("commit", "--allow-empty", "-q", "-m", "nomail\n\nBoard-Closes: scratch#50", env=env)
    sha = sc.git("rev-parse", "HEAD")
    _republish(sc)
    c = verdict(sc, item(50), sha)
    assert (c.tier, c.who, c.email) == ("git", "No Mail", "")
    assert c.to_dict()["email"] == ""
    assert ba.describe(c) == "asserted by git author No Mail <>"


def test_evidence_keeps_an_empty_git_author_email_as_empty_and_the_closer_agrees(sc):
    env = {"GIT_AUTHOR_NAME": "No Mail", "GIT_AUTHOR_EMAIL": "", "GIT_COMMITTER_NAME": "No Mail", "GIT_COMMITTER_EMAIL": "",
           "GIT_AUTHOR_DATE": T1, "GIT_COMMITTER_DATE": T1}
    sc.git("commit", "--allow-empty", "-q", "-m", "nomail\n\nBoard-Closes: scratch#51", env=env)
    sha = sc.git("rev-parse", "HEAD")
    _republish(sc)
    e = evidence(sc, item(51), sha)
    assert e.email == "" and e.closer.email == ""                 # present-but-empty on both, never None


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


def test_annotate_says_any_failure_on_the_item_not_only_a_git_one(sc, monkeypatch):
    items = {"scratch#1": item(1), "scratch#4": item(4)}
    doc = {"items": [{"id": "scratch#1", "repo": "scratch", "state": "done", "refs": [sc.c["signed"]]},
                     {"id": "scratch#4", "repo": "scratch", "state": "done", "refs": [sc.c["plain"]]}]}
    real = ba.closer_of_item

    def flaky(repo, it, *a, **kw):
        if it.id == "scratch#1":
            raise ValueError("odd record")
        return real(repo, it, *a, **kw)
    monkeypatch.setattr(ba, "closer_of_item", flaky)
    assert ba.annotate(doc, items, {"scratch": sc.repo}, FULL(sc)) == 2
    by = {s["id"]: s["closed_by"] for s in doc["items"]}
    assert by["scratch#1"]["tier"] == "unattributed" and "ValueError: odd record" in by["scratch#1"]["how"]
    assert by["scratch#4"]["tier"] == "git"


def test_the_context_line_and_the_state_part(sc):
    st = SimpleNamespace(state="done", refs=(sc.c["plain"],), column="done", reason="closed")
    line = ba.closer_line(sc.repo, item(4), st, FULL(sc))
    assert line == "closed by: asserted by git author Plain Person <x@x>"
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
    assert by["levain#2"]["closed_by"]["how"] == "first added docs/r.md, asserted by git author"
    rc2 = main(["board", "state", *argv])
    table = capsys.readouterr().out
    assert rc == rc2 and "asserted by git author t" in table


def test_board_state_survives_an_attribution_that_raises_and_says_it_per_item(tmp_path, capsys, monkeypatch):
    """M-5: state_doc's call is guarded as board_context's is: the board still reads, each git close says why."""
    w = World(tmp_path)
    w.commit("root", date="2026-10-01T12:00:00Z")
    w.push_main()
    w.commit("land one", trailers=("Board-Item: levain#1", "Board-Closes: levain#1"))
    w.push_main()
    items = _items_file(tmp_path, (1, "merged", "the work lands"), (3, "merged", "not yet"))
    monkeypatch.setattr(ba, "annotate", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    argv = ["--items", str(items), "--evidence-extra", str(_extra(tmp_path)), "--repo", str(w.w), "--since", "2026-11-12T00:00:00Z"]
    main(["board", "state", "--json", *argv])
    by = {s["id"]: s for s in json.loads(capsys.readouterr().out)["items"]}
    assert by["levain#1"]["state"] == "done" and by["levain#1"]["closed_by"]["tier"] == "unattributed"
    assert "RuntimeError: boom" in by["levain#1"]["closed_by"]["how"] and by["levain#3"].get("closed_by") is None


def test_board_state_with_the_two_flags_reads_tier1_and_tier2(sc, tmp_path, monkeypatch):
    """The flags override the defaults: --data-root an authority log, --desk-dir launch records (a stand-in lookup here: the
    launch store has its own tests)."""
    monkeypatch.setattr(ba, "_desk_seat_of", lambda directory: seats(sc))
    args = SimpleNamespace(data_root=sc.cockpit, desk_dir=tmp_path)
    res = ba.resolvers_from_args(args)
    assert res.authority is not None and res.seat_of("slot-key-77").name == "worker L-77"
    assert verdict(sc, item(1), sc.c["signed"], res).tier == "signed"
    assert verdict(sc, item(3), sc.c["seat"], res).tier == "trailer"


def _claimed_launch(store, cwd, item_id):
    """A real launch record in ``store``, run in ``cwd``, that claimed ``item_id`` (the T7 claim, as ``--item`` writes it)."""
    from levain.desk import lease

    store.coord_set_agent("claude", cwd=str(cwd))
    store.accept_drop("fix the build", surface="cli")
    ln = store.coord_status()["launches"][0]
    with store._write() as (conn, now):
        lease.claim_board_items(conn, now, ln["launch_id"], [item_id])
    return ln


def _home(tmp_path, monkeypatch, *, cockpit=None):
    """A scratch LEVAIN_HOME whose consent hooks name ``cockpit`` (when given): the install's own cockpit, as a real one."""
    home = tmp_path / "home"
    (home / "consent").mkdir(parents=True, mode=0o700)
    if cockpit is not None:
        (home / "consent" / "claude_code.json").write_text(json.dumps({"cockpit_data_root": str(cockpit)}))
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    return home


def test_no_flags_the_install_defaults_find_the_launch_record_and_the_key(sc, tmp_path, monkeypatch):
    """(a): with NO flags, under a scratch LEVAIN_HOME, tier 1 reads the cockpit the consent hooks name and tier 2 the desk
    store of the same home."""
    from levain.desk.store import CoordStore

    home = _home(tmp_path, monkeypatch, cockpit=sc.cockpit)
    store = CoordStore(home / "consent")
    ln = _claimed_launch(store, sc.repo, "scratch#20")
    seated = sc.commit(f"by seat\n\nBoard-Closes: scratch#20\nBoard-Seat: {ln['session_key']}", "Seat Person", T1, files={"l": "1"})
    sc.git("update-ref", "refs/remotes/origin/main", "HEAD")
    res = ba.resolvers_from_args(SimpleNamespace(data_root=None, desk_dir=None))
    signed = verdict(sc, item(1), sc.c["signed"], res)
    assert (signed.tier, signed.how) == ("signed", "signed by an operator key") and signed.who.startswith("k")
    seat = verdict(sc, item(20), seated, res)
    assert (seat.tier, seat.who) == ("trailer", f"agent {ln['role']} {ln['launch_id']}")


def test_no_flags_with_the_default_paths_absent_reads_not_checked_with_a_note(sc, tmp_path, monkeypatch):
    """(a): an install with no cockpit named and no desk store: never an error and never a silent tier-3 claim."""
    _home(tmp_path, monkeypatch)
    res = ba.resolvers_from_args(SimpleNamespace(data_root=None, desk_dir=None))
    signed = verdict(sc, item(1), sc.c["signed"], res)
    assert (signed.tier, signed.who) == ("git", "Op Person") and "authority log was not checked" in signed.note
    seat = verdict(sc, item(3), sc.c["seat"], res)
    assert (seat.tier, seat.who) == ("git", "Seat Person") and "launch records were not checked" in seat.note


def test_with_no_levain_home_at_all_the_defaults_read_not_checked_never_the_real_home(sc, monkeypatch):
    monkeypatch.delenv("LEVAIN_HOME", raising=False)
    res = ba.resolvers_from_args(SimpleNamespace(data_root=None, desk_dir=None))
    assert verdict(sc, item(1), sc.c["signed"], res).tier == "git"
    assert verdict(sc, item(3), sc.c["seat"], res).tier == "git"


def test_the_flags_override_the_defaults(sc, tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)                       # defaults absent
    res = ba.resolvers_from_args(SimpleNamespace(data_root=sc.cockpit, desk_dir=None))
    assert verdict(sc, item(1), sc.c["signed"], res).tier == "signed"


def test_one_authority_reader_board_author_reads_no_authority_log_directly():
    """(b): every authority read goes through board_sign's reader, and every key judgement through its key rule: the
    module imports nothing that reads or judges the log itself, and calls the two board_sign functions."""
    import ast

    tree = ast.parse(Path(ba.__file__).read_text(encoding="utf-8"))
    imported = {a.name if isinstance(n, ast.Import) else f"{n.module}.{a.name}"
                for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    assert not {m for m in imported if m.startswith(("levain.authority", "levain.consent"))}, imported
    called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert {"read_authority", "resolve_authority", "key_standing"} <= called


def test_a_real_launch_record_in_a_scratch_desk_store_maps_the_durable_key_to_a_seat(sc, tmp_path):
    from levain.desk.store import CoordStore

    qdir = tmp_path / "desk"
    qdir.mkdir(mode=0o700)
    store = CoordStore(qdir)
    ln = _claimed_launch(store, sc.repo, "scratch#3")
    seat_of = ba._desk_seat_of(qdir)
    key = ln["session_key"]
    got = seat_of(key)
    assert (got.name, got.items) == (f"{ln['role']} {ln['launch_id']}", ("scratch#3",))
    assert seat_of("no-such-key") is None
    with pytest.raises(ba.NotChecked):
        ba._desk_seat_of(tmp_path / "absent")(key)                  # a store that cannot be read is "not checked", said
    c = verdict(sc, item(3), sc.c["seat"], ba.Resolvers(None, ba._desk_seat_of(qdir)))     # Board-Seat slot-key-77: no such launch
    assert c.tier == "git" and "maps to no launch record" in c.note


def test_a_path_through_an_ancestor_directory_symlink_is_the_literal_path_no_commit_added_it(sc):
    """``dirlink/f.txt`` with ``dirlink -> realdir``: git tracks ``dirlink`` as a link, never ``dirlink/f.txt``. The literal
    path is computed lexically; the disk's target is not followed (levain#103, L3 p4 r2)."""
    sc.commit("adds realdir2", "Target Person", T1, files={"realdir2/f.txt": "1"})
    link = sc.repo / "dirlink2"
    link.symlink_to(sc.repo / "realdir2")
    try:
        sc.commit("adds dirlink2", "Link Person", T1)
        _republish(sc)
        c = verdict(sc, item(8, "document", "dirlink2/f.txt"), "dirlink2/f.txt")
        direct = verdict(sc, item(8, "document", "dirlink2"), "dirlink2")
    finally:
        link.unlink()
        sc.commit("removes dirlink2", "Link Person", T1)
        _republish(sc)
    assert (c.tier, c.who, c.how) == ("unattributed", None, ba.NOT_ON_MAIN)
    assert direct.who == "Link Person"


def test_a_file_under_a_submodule_has_no_superproject_add_but_the_gitlink_does(sc):
    import shutil
    sub = sc.repo / "subm2"
    sub.mkdir()
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    subprocess.run(["git", "-C", str(sub), "init", "-q", "-b", "main"], check=True, capture_output=True, env=env)
    (sub / "inner.txt").write_text("i")
    subprocess.run(["git", "-C", str(sub), "add", "-A"], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(sub), "-c", "user.name=S", "-c", "user.email=s@s", "commit", "-q", "-m", "sub"],
                   check=True, capture_output=True, env=env)
    try:
        added = sc.commit("adds subm2", "Sub Adder", T1)
        _republish(sc)
        inner = verdict(sc, item(8, "document", "subm2/inner.txt"), "subm2/inner.txt")
        link = verdict(sc, item(8, "document", "subm2"), "subm2")
    finally:
        sc.git("rm", "-q", "--cached", "subm2")
        shutil.rmtree(sub)
        sc.commit("removes subm2", "Sub Adder", T1)
        _republish(sc)
    assert (inner.tier, inner.how) == ("unattributed", ba.NOT_ON_MAIN)
    assert (link.who, link.commit) == ("Sub Adder", added)


def test_a_key_ending_in_dotdot_is_normalised_lexically_not_a_bad_pathspec(sc):
    c = verdict(sc, item(6, "document", "docs/sub2/.."), "docs/sub2/..")
    assert (c.who, c.commit) == ("Doc Person", sc.c["doc"])           # docs/sub2/.. is docs (the directory a.md is in)
    assert verdict(sc, item(6, "document", "docs/a.md/.."), "x").who == "Doc Person"
    whole = verdict(sc, item(6, "document", "docs/.."), "x")
    assert (whole.tier, whole.how) == ("unattributed", "unattributed: the path is the repo's whole work tree")
    assert verdict(sc, item(6, "document", "../outside_it"), "x").how == OUTSIDE_TEXT


def test_a_repo_reached_through_a_symlinked_path_with_key_dot_is_the_whole_work_tree_not_outside(sc, tmp_path):
    via = tmp_path / "via_link"
    via.symlink_to(sc.repo)
    c = ba.closer_of_item(via, item(8, "document", "."), "x", FULL(sc))
    assert (c.tier, c.how) == ("unattributed", "unattributed: the path is the repo's whole work tree")
    d = ba.closer_of_item(via, item(6, "document", "docs/a.md"), "x", FULL(sc))
    assert (d.who, d.commit) == ("Doc Person", sc.c["doc"])


def test_a_relative_repo_with_the_cwd_inside_it_reads_the_document_it_is_not_outside(sc, monkeypatch):
    """``levain board state`` with no ``--repo`` passes ``Path(".")``: the path and the repo are both made absolute from the
    cwd, lexically, so neither base misses (levain#103, L3 p4 r3)."""
    monkeypatch.chdir(sc.repo)
    d = ba.closer_of_item(Path("."), item(6, "document", "docs/a.md"), "x", FULL(sc))
    assert (d.who, d.commit) == ("Doc Person", sc.c["doc"])
    r = ba.closer_of_item(Path("."), item(7, "run", "outdir"), "x", FULL(sc))
    assert (r.who, r.commit) == ("Dir One", sc.c["dir1"])
    whole = ba.closer_of_item(Path("."), item(8, "document", "."), "x", FULL(sc))
    assert whole.how == "unattributed: the path is the repo's whole work tree"
    assert ba.closer_of_item(Path("."), item(8, "document", "../outside_it"), "x", FULL(sc)).how == OUTSIDE_TEXT


def test_a_tracked_link_to_dot_given_as_the_repo_with_key_dot_is_the_whole_work_tree_not_the_commit_that_added_the_link(sc):
    """``/r/via`` (``via`` a tracked symlink to ``.``) given as the repo, key ``.``: the repo as given is tried before the
    toplevel, so the answer is the whole work tree, not "first added via" (levain#103, L3 p4 r3)."""
    link = sc.repo / "via"
    link.symlink_to(".")
    try:
        sc.commit("adds via", "Link Person", T1)
        _republish(sc)
        c = ba.closer_of_item(link, item(8, "document", "."), "x", FULL(sc))
    finally:
        link.unlink()
        sc.commit("removes via", "Link Person", T1)
        _republish(sc)
    assert (c.tier, c.who, c.how) == ("unattributed", None, "unattributed: the path is the repo's whole work tree")


def test_a_file_added_then_removed_on_main_is_still_credited_to_the_commit_that_added_it(sc):
    """Pins the fix-3 behaviour (``--diff-filter=A`` over the first-parent log, no ``--follow``-style presence test)."""
    added = sc.commit("adds gone", "Gone Adder", T1, files={"docs/gone.md": "g"})
    sc.git("rm", "-q", "docs/gone.md")
    sc.commit("removes gone", "Gone Remover", T1)
    _republish(sc)
    c = verdict(sc, item(8, "document", "docs/gone.md"), "docs/gone.md")
    assert (c.who, c.commit) == ("Gone Adder", added)
    assert c.how == "first added docs/gone.md, asserted by git author"


# ------------------------------------------------------------------------------------------------ EVIDENCE (P6 fix 6)
def evidence(sc, it, ref, res=None):
    res = res or FULL(sc)
    rd = ba._Reader(sc.repo, res)
    return rd.evidence(ba.closer_of_item(sc.repo, it, ref, res, reader=rd), it)


def test_evidence_a_revoked_key_s_signature_is_valid_its_standing_revoked_and_closed_by_carries_none_of_it(sc):
    from levain.cockpit.keys import principal_of
    e = evidence(sc, item(9), sc.c["old_before"])
    fp = sc.keys["old"].fp
    s = e.signature
    assert (s.state, s.fingerprint, s.principal, s.standing, s.revoked_seq) == ("valid", fp, principal_of(fp), "revoked", 4)
    assert e.closer.tier == "git" and f"signed by {fp}, revoked at seq 4" in e.closer.note        # describe() is unchanged
    assert set(e.closer.to_dict()) == {"tier", "who", "how", "commit", "email", "note"}             # no degraded, no evidence


def test_evidence_a_valid_signature_and_its_principal_need_no_authority_log(sc, tmp_path, monkeypatch):
    from levain.cockpit.keys import principal_of
    fp = sc.keys["laptop"].fp
    for res in (ba.Resolvers(None, seats(sc)), ba.Resolvers(ba.CockpitAuthority(tmp_path / "no-cockpit"), seats(sc))):
        s = evidence(sc, item(1), sc.c["signed"], res).signature
        assert (s.state, s.principal, s.standing) == ("valid", principal_of(fp), "not-checked") and "not checked" in s.standing_why
    s = evidence(sc, item(1), sc.c["signed"]).signature
    assert (s.state, s.standing, s.in_log) == ("valid", "active", True)
    assert evidence(sc, item(4), sc.c["plain"]).signature.state == "none"
    s = evidence(sc, item(2), sc.c["stranger"]).signature
    assert (s.state, s.standing, s.in_log) == ("valid", "refused", False)          # not in this install's log
    s = evidence(sc, item(40), sc.c["phone"]).signature
    assert (s.state, s.standing, s.in_log) == ("valid", "refused", True) and "not a terminal key" in s.standing_why
    s = evidence(sc, item(41), sc.c["late"]).signature
    assert (s.state, s.standing, s.in_log) == ("valid", "refused", True) and "after the commit's own date" in s.standing_why
    from levain.team import signing
    monkeypatch.setattr(signing, "verify_commit", lambda repo, sha: signing.Verdict("indeterminate", None, "ssh-keygen gone"))
    s = evidence(sc, item(4), sc.c["plain"]).signature
    assert s.state == "unjudged" and "ssh-keygen gone" in s.why


def test_evidence_the_trailer_is_judged_on_its_own_even_when_tier1_holds(sc):
    e = evidence(sc, item(12), sc.c["signed_seat"])
    assert e.closer.tier == "signed" and (e.trailer.claim, e.trailer.seat) == ("holds", "worker L-77")
    assert evidence(sc, item(3), sc.c["seat"]).trailer == ba.SeatClaim(("slot-key-77",), "holds", "", "worker L-77")
    t = evidence(sc, item(3), sc.c["seat"], ba.Resolvers(sc.authority(), None)).trailer
    assert (t.claim, t.why) == ("undetermined", "a Board-Seat trailer is present but no launch records are consulted")

    def unreadable(key):
        raise ba.NotChecked("the desk store is absent")
    t = evidence(sc, item(3), sc.c["seat"], ba.Resolvers(sc.authority(), unreadable)).trailer
    assert t.claim == "undetermined" and "the desk store is absent" in t.why
    t = evidence(sc, item(14), sc.c["unknown_seat"]).trailer          # launch records are install-scoped (head ruling 7)
    assert (t.claim, t.seat) == ("undetermined", None) and "maps to no launch record in this install's desk store" in t.why
    t = evidence(sc, item(3), sc.c["seat"], ba.Resolvers(sc.authority(), seats(sc, items=("scratch#99",)))).trailer
    assert (t.claim, t.seat) == ("refuted", "worker L-77") and "claimed scratch#99, not scratch#3" in t.why
    t = evidence(sc, item(3), sc.c["seat"], ba.Resolvers(sc.authority(), seats(sc, cwd=sc.repo.parent / "worktree-removed"))).trailer
    assert t.claim == "undetermined" and "is not a git repo this reader can read" in t.why
    assert evidence(sc, item(13), sc.c["two_seats"]).trailer.claim == "refuted"
    assert evidence(sc, item(4), sc.c["plain"]).trailer == ba.SeatClaim((), "", "the commit carries no Board-Seat trailer")
    # the verdict's words are what they were: a trailer that maps to no launch record reads as before
    assert "its Board-Seat trailer maps to no launch record" in evidence(sc, item(14), sc.c["unknown_seat"]).closer.note


def test_evidence_carries_the_git_facts_and_the_how_prefix(sc):
    e = evidence(sc, item(44), sc.c["rebased"])
    assert (e.author, e.email, e.committer, e.how_prefix) == ("Agent Author", "x@x", "Re Baser <r@r>", "")
    e = evidence(sc, item(6, "document", "docs/a.md"), "docs/a.md")
    assert e.how_prefix == "first added docs/a.md, " and e.closer.how == "first added docs/a.md, asserted by git author"
    assert e.author == "Doc Person"


def test_annotate_fills_evidence_and_an_unread_close_keeps_the_board_state_s_commit(sc, monkeypatch):
    items = {"scratch#1": item(1), "scratch#4": item(4), "scratch#6": item(6, "document", "docs/a.md")}
    doc = {"items": [{"id": "scratch#1", "repo": "scratch", "state": "done", "refs": [sc.c["signed"]]},
                     {"id": "scratch#4", "repo": "scratch", "state": "done", "refs": [sc.c["plain"]]},
                     {"id": "scratch#6", "repo": "scratch", "state": "done", "refs": ["docs/a.md"]}]}
    real = ba.closer_of_item

    def flaky(repo, it, *a, **kw):
        if it.id in ("scratch#1", "scratch#6"):
            raise ba.be.GitFailed("git went away")
        return real(repo, it, *a, **kw)
    monkeypatch.setattr(ba, "closer_of_item", flaky)
    got: dict = {}
    ba.annotate(doc, items, {"scratch": sc.repo}, FULL(sc), evidence=got)
    assert set(got) == {"scratch#1", "scratch#4", "scratch#6"}
    assert (got["scratch#1"].unread, got["scratch#1"].anchor) == ("the author could not be read (git went away)", sc.c["signed"])
    assert (got["scratch#6"].anchor, got["scratch#6"].how_prefix) == (None, "first added docs/a.md, ")
    assert got["scratch#4"].unread is None and got["scratch#4"].author == "Plain Person"
    by = {s["id"]: s["closed_by"] for s in doc["items"]}
    assert set(by["scratch#1"]) == {"tier", "who", "how", "commit"}                 # closed_by: what P4 shipped, no marker


def test_mark_unread_fills_evidence_only_for_the_closes_annotate_left(sc):
    items = {"scratch#1": item(1), "scratch#4": item(4)}
    doc = {"items": [{"id": "scratch#1", "repo": "scratch", "state": "done", "refs": [sc.c["signed"]],
                      "closed_by": {"tier": "signed", "who": "k", "how": "signed by an operator key", "commit": sc.c["signed"]}},
                     {"id": "scratch#4", "repo": "scratch", "state": "done", "refs": [sc.c["plain"]]}]}
    got: dict = {}
    ba.mark_unread(doc, items, RuntimeError("boom"), evidence=got)
    assert set(got) == {"scratch#4"} and got["scratch#4"].anchor == sc.c["plain"]
    assert doc["items"][0]["closed_by"]["who"] == "k" and "RuntimeError: boom" in doc["items"][1]["closed_by"]["how"]
