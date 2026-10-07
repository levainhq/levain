"""The tip-only ledger's one judgement and its acceptance (the quotepath named fix, 2026-10-07).

Every test here was a failure RUN on the redesign's last reviewed tip (seat/1005-3-qp2 @6ef300e) before the fix: one
side-effect-free ``judge`` for every reader, writer and sync; the pin advance as the read's acceptance transaction;
every git failure while judging a hook deny.
"""
import fcntl
import json
import os
import subprocess

import pytest

from levain.team import entry as E
from levain.team.transport import GitLedger, LedgerReadError, Repo

from tests.test_team_git import _own_file, _pins_file, _push_wt, edit, git, ledger, record_ruling, team, two  # noqa: F401


def _gl(repo):
    return GitLedger(Repo.discover(repo))


def _ids(repo):
    return {e["id"] for e in ledger(repo).entries}


def test_a_read_whose_pins_cannot_be_saved_is_refused_not_served(two):
    # codex (pin round) 1: _advance_pins swallowed a lock timeout and the read still returned the new lines unpinned,
    # so a later rewrite of those lines was accepted.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    assert team("sync", repo=ben) == 0
    ledger(ben)
    assert record_ruling(ana, "src/b.py", "second") == 0
    assert team("sync", repo=ben) == 0
    gb = _gl(ben)
    fd = os.open(gb.base / "pins.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with pytest.raises(LedgerReadError):
            gb.ledger()
    finally:
        os.close(fd)
    assert "second" in [e.get("words") for e in ledger(ben).entries]      # once the pins can be saved, it reads


def test_sync_does_not_push_onto_a_remote_that_rewrote_a_pinned_file(two):
    # codex/gemini/complement (pin round) 2: the remote tip was judged for structure only, so an unpushed entry was
    # replayed onto, and pushed over, a remote that rewrote a pinned file.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    assert record_ruling(ana, "src/b.py", "second") == 0
    assert team("sync", repo=ben) == 0
    ledger(ben)                                                             # ben pins ana's file
    assert record_ruling(ben, "src/c.py", "ben unpushed", "--no-push") == 0
    ga = _gl(ana)
    f = _own_file(ga)
    f.write_text(f.read_text().splitlines()[0] + "\n")                     # drop "second", a canonical path only
    _push_wt(ga, "rewrite")
    before = git("rev-parse", "levain-ledger", cwd=tmp / "origin.git").strip()
    rc = team("sync", repo=ben)
    assert git("rev-parse", "levain-ledger", cwd=tmp / "origin.git").strip() == before
    assert any("rewritten or removed" in t for t in ledger(ben).tamper) and rc == 2


def test_sync_does_not_push_a_locally_refused_tip(two):
    # codex (pin round) 3: sync checked only the remote, so a clone whose own tip held a stray file pushed it.
    tmp, ana, ben = two
    gb = _gl(ben)
    (gb.wt / "ledger" / "ben").mkdir(parents=True, exist_ok=True)
    (gb.wt / "ledger" / "ben" / "notes.txt").write_text("x\n")
    git("add", "-A", ".", cwd=gb.wt)
    git("commit", "-qm", "a stray file, committed locally", cwd=gb.wt)
    before = git("rev-parse", "levain-ledger", cwd=tmp / "origin.git").strip()
    assert team("sync", repo=ben) == 2
    assert git("rev-parse", "levain-ledger", cwd=tmp / "origin.git").strip() == before


@pytest.mark.parametrize("fail", ["ls-tree", "log"])
def test_a_git_failure_while_judging_denies_in_the_hook(two, monkeypatch, capsys, fail):
    # codex (pin round) 4: a failed ls-tree raised a plain TeamError and the hook failed OPEN; so did a timeout while
    # naming a tamper path's author.
    from levain.team import hook as H, transport as T
    tmp, ana, ben = two
    if fail == "log":
        (_gl(ben).wt / "ledger" / "ben").mkdir(parents=True, exist_ok=True)
        (_gl(ben).wt / "ledger" / "ben" / "notes.txt").write_text("x\n")
        git("add", "-A", ".", cwd=_gl(ben).wt)
        git("commit", "-qm", "stray", cwd=_gl(ben).wt)
    real = T.git

    def fake(args, cwd, **kw):
        if args and args[0] == fail and (fail != "log" or "-1" in args):
            raise T.TeamError(f"git {fail} timed out after 30s")
        return real(args, cwd, **kw)
    monkeypatch.setattr(T, "git", fake)
    H.pretooluse({"session_id": "s", "transcript_path": "/x", "cwd": str(ben), "hook_event_name": "PreToolUse",
                  "tool_name": "Edit", "tool_input": {"file_path": str(ben / "src" / "a.py")}, "tool_use_id": "t"})
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "timed out" in out["permissionDecisionReason"]


def test_after_repin_the_next_read_pins_again_and_is_not_served_from_the_cache(two):
    # codex (pin round) 5: the cache key held the pins file's mtime/size, and a first read stored its result under
    # "no pins"; after `repin` the next read hit that entry and never re-created the pins.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    assert team("sync", repo=ben) == 0
    _pins_file(ben).unlink(missing_ok=True)
    ledger(ben)
    assert team("repin", repo=ben) == 0
    assert not _pins_file(ben).exists()
    ledger(ben)
    assert _pins_file(ben).exists()


@pytest.mark.parametrize("text", ["[" * 200000, " " * (9 << 20) + "{}"], ids=["deep", "large"])
def test_a_pins_file_too_deep_or_too_large_refuses_as_unreadable(two, text):
    # codex (pin round) 6: a deeply nested pins.json raised RecursionError out of the read (the hook failed open).
    tmp, ana, ben = two
    ledger(ben)
    _pins_file(ben).write_text(text)
    assert any("pins.json" in t for t in ledger(ben).tamper)


def test_an_old_format_pins_file_restarts_pinning_instead_of_refusing(two):
    # complement (pin round) 5: every pins.json in the earlier list-of-lines format refused the whole ledger.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    assert team("sync", repo=ben) == 0
    ledger(ben)
    rel = next(k for k in json.loads(_pins_file(ben).read_text()) if k.startswith("ana/"))
    _pins_file(ben).write_text(json.dumps({rel: ["an old pinned line"]}))
    assert not ledger(ben).tamper
    assert set(json.loads(_pins_file(ben).read_text())[rel]) == {"sha256", "length"}


def test_doctor_reports_an_unjudgeable_ledger_as_a_fail_row(two, monkeypatch, capsys):
    # complement (pin round) 6: doctor, the command the hook's deny names, raised instead of reporting.
    from levain.team import transport as T
    tmp, ana, ben = two
    real = T.git

    def fake(args, cwd, **kw):
        if args and args[0] == "ls-tree":
            raise T.TeamError("git ls-tree timed out after 30s")
        return real(args, cwd, **kw)
    monkeypatch.setattr(T, "git", fake)
    capsys.readouterr()
    assert team("doctor", repo=ben) == 1
    assert "FAIL  the team ledger could not be read" in capsys.readouterr().out


def test_a_refused_ledger_does_not_commit_an_interrupted_own_write(two):
    # codex (pin round) 7: writers ran _recover_dirty before the tamper check, so on a refused tip it committed the
    # clone's dirty entry file and then refused.
    tmp, ana, ben = two
    assert record_ruling(ben, "src/a.py", "ben's first") == 0
    gb = _gl(ben)
    own = next((gb.wt / "ledger" / "ben").glob("*.jsonl"))
    (gb.wt / "ledger" / "ben" / "notes.txt").write_text("x\n")
    git("add", "--", "ledger/ben/notes.txt", cwd=gb.wt)
    git("commit", "-qm", "a stray file, committed locally", cwd=gb.wt)
    with open(own, "a") as fh:
        fh.write('{"interrupted": true}\n')
    head = git("rev-parse", "HEAD", cwd=gb.wt)
    assert record_ruling(ben, "src/b.py", "must not land") != 0
    assert git("rev-parse", "HEAD", cwd=gb.wt) == head
    assert own.name in git("status", "--porcelain", cwd=gb.wt)


def test_entries_after_a_problem_line_are_pinned_and_their_removal_refuses(two):
    # codex (tip-only r1) 2 + complement (pin round) 4: any problem anywhere stopped every pin from advancing, so a
    # ruling enforced after a bad line could be rewritten away without a refusal.
    tmp, ana, ben = two
    ga = _gl(ana)
    assert record_ruling(ana, "src/a.py", "first") == 0
    f = _own_file(ga)
    with open(f, "a") as fh:
        fh.write("not json\n")
    _push_wt(ga, "a junk line")
    assert record_ruling(ana, "src/b.py", "after the junk") == 0
    assert team("sync", repo=ben) == 0
    assert "after the junk" in [e.get("words") for e in ledger(ben).in_force]
    lines = f.read_text().splitlines()
    f.write_text("\n".join(lines[:-1]) + "\n")                              # the ruling after the junk, removed
    _push_wt(ga, "drop the last ruling")
    rc = team("sync", repo=ben)
    led = ledger(ben)
    assert any("rewritten or removed" in t for t in led.tamper) and not led.entries and rc == 2


def test_an_unterminated_last_line_is_a_problem_and_never_an_entry(two):
    # codex (pin round) 9: a valid entry without a final LF was enforced and pinned; one appended byte then turned it
    # into invalid JSON without changing the pinned prefix, and the ruling silently stopped being enforced.
    tmp, ana, ben = two
    ga = _gl(ana)
    assert record_ruling(ana, "src/a.py", "first") == 0
    f = _own_file(ga)
    last = json.loads(f.read_text().splitlines()[-1])
    torn = E.seal(E.build("ana", "decision", kind="ruling", owner="client:Dana", paths=["src/t.py"], words="torn"),
                  last["hash"])
    with open(f, "a") as fh:
        fh.write(json.dumps(torn, sort_keys=True))                          # no LF
    _push_wt(ga, "an unterminated line")
    assert team("sync", repo=ben) == 0
    led = ledger(ben)
    assert "torn" not in [e.get("words") for e in led.entries]
    assert any("no line break" in p for p in led.problems), led.problems


def test_a_refused_fetch_never_moves_the_remote_tracking_ref(two):
    # L2 (round 0) Q6 quarantine: the remote is fetched into refs/levain/incoming/ and judged first. RUN: without
    # `--refmap=` git's opportunistic update still moved refs/remotes/origin/levain-ledger onto the refused tip.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    assert team("sync", repo=ben) == 0
    gb = _gl(ben)
    before = gb.remote_ref()
    _plant_stray(_gl(ana))
    assert team("sync", repo=ben) == 2
    assert gb.remote_ref() == before
    assert any("notes.txt" in t for t in ledger(ben).tamper)              # the quarantine ref is the record


def test_a_rewrite_of_an_accepted_file_denies_with_the_file_and_the_recovery_until_repin(two):
    # Phill 10-06: EXPLICIT repin. The read side refuses a rewrite of accepted bytes; the deny names the file, says it
    # was rewritten, and gives the recovery. After `levain team repin` the quarantined tip is accepted on read.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    assert record_ruling(ana, "src/b.py", "second") == 0
    assert team("sync", repo=ben) == 0
    ledger(ben)
    ga = _gl(ana)
    git("reset", "-q", "--hard", "HEAD~1", cwd=ga.wt)
    git("push", "-qf", "origin", "HEAD:levain-ledger", cwd=ga.wt)
    assert team("sync", repo=ben) == 2
    reason = edit(ben, "src/unrelated.py", session="rw")["hookSpecificOutput"]["permissionDecisionReason"]
    rel = _own_file(ga).relative_to(ga.wt).as_posix()
    assert rel in reason and "rewritten" in reason and "levain team repin" in reason and "restores" in reason
    assert team("repin", repo=ben) == 0
    assert not ledger(ben).tamper


def _plant_stray(gl):
    (gl.wt / "ledger" / "ana" / "notes.txt").write_text("x\n")
    _push_wt(gl, "plant")


def test_a_parent_directory_swapped_for_a_link_mid_write_is_not_followed(two, monkeypatch):
    # The would-be known limit "parent-dir TOCTOU": the member directory was checked by path, then resolved again by
    # mkdir and open, so a local process swapping it for a symlink in between redirected the append out of the
    # worktree (O_NOFOLLOW guards only the last component).
    tmp, ana, ben = two
    outside = tmp / "outside"
    outside.mkdir()
    gb = _gl(ben)
    member = gb.wt / "ledger" / "ben"
    real_mkdir = os.mkdir

    def racing_mkdir(path, *a, **kw):
        if str(path).endswith("ben") or path == "ben":
            if member.exists() and not member.is_symlink():
                os.rmdir(member)
            if not member.is_symlink():
                os.symlink(outside, member)
        return real_mkdir(path, *a, **kw)
    monkeypatch.setattr(os, "mkdir", racing_mkdir)
    try:
        rc = record_ruling(ben, "src/a.py", "must not land outside")
    except Exception:
        rc = 1
    assert rc != 0
    assert list(outside.iterdir()) == []


def test_an_entry_naming_a_missing_id_is_a_problem_every_reader_reports(two, capsys):
    # E-team / codex: a hash-valid ack naming a missing id failed `levain team verify` while ledger.problems (what the
    # team view and the hook count) was empty. RUN on the merged tree before the fix: ledger.problems == [].
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    ga = _gl(ana)
    f = _own_file(ga)
    last = json.loads(f.read_text().splitlines()[-1])
    dangling = E.seal(E.build("ana", "ack", refs=["ana-20200101000000-deadbeef"], session="s", agent="claude-code",
                              summary="an ack to nothing"), last["hash"])
    with open(f, "a") as fh:
        fh.write(json.dumps(dangling, sort_keys=True) + "\n")
    _push_wt(ga, "a dangling ack")
    assert team("sync", repo=ben) == 0
    assert any("ana-20200101000000-deadbeef" in p for p in ledger(ben).problems)
    capsys.readouterr()
    assert team("verify", repo=ben) == 1
    out = capsys.readouterr().out
    assert out.count("ana-20200101000000-deadbeef") == 1, out


def test_fetch_only_judges_and_never_replays(two):
    # E-team's view: fetch, never rebase on GET. A refused remote is reported and never becomes the remote ref.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    gb = _gl(ben)
    head = gb.head()
    assert gb.fetch_only(interval=0, timeout=30) is None
    assert gb.remote_ref() != head and gb.head() == head                     # fetched and accepted, nothing replayed
    accepted = gb.remote_ref()
    _plant_stray(_gl(ana))
    note = gb.fetch_only(interval=0, timeout=30)
    assert note and "notes.txt" in note
    assert gb.remote_ref() == accepted and gb.head() == head


def test_only_this_clones_own_tip_is_ever_pinned(two):
    # Head ruling: a judge or read of any rev other than the local branch tip cannot advance pins (structural, not a
    # caller's flag).
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    gb = _gl(ben)
    gb.ledger()
    before = _pins_file(ben).read_bytes()
    assert gb.fetch_only(interval=0, timeout=30) is None
    gb.ledger(rev=gb.remote_ref())                                           # a newer, non-local rev
    gb.judge_remote(gb.remote_ref())
    assert _pins_file(ben).read_bytes() == before


@pytest.mark.parametrize("exc", ["RolesError", "TeamError", "LedgerReadError", "RuntimeError", "RecursionError"])
def test_every_failure_to_judge_is_a_deny_at_the_hook(two, monkeypatch, capsys, exc):
    # L1 (round 0) HIGH: a team.toml that does not parse, or a plain TeamError from snapshot, failed OPEN. One boundary:
    # any exception while a joined clone judges an edit is a DENY.
    from levain.team import hook as H, roles as R, transport as T
    tmp, ana, ben = two
    kind = {"RolesError": R.RolesError, "TeamError": T.TeamError, "LedgerReadError": T.LedgerReadError,
            "RuntimeError": RuntimeError, "RecursionError": RecursionError}[exc]

    def boom(self):
        raise kind("injected")
    monkeypatch.setattr(T.GitLedger, "snapshot", boom)
    H.pretooluse({"session_id": "s", "transcript_path": "/x", "cwd": str(ben), "hook_event_name": "PreToolUse",
                  "tool_name": "Edit", "tool_input": {"file_path": str(ben / "src" / "a.py")}, "tool_use_id": "t"})
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "injected" in out["permissionDecisionReason"]


def test_a_team_toml_that_does_not_parse_on_the_remote_is_refused(two):
    # L1 (round 0) HIGH, the route without a monkeypatch: a remote whose team.toml never parses was adopted, and the
    # hook then failed open on the RolesError.
    tmp, ana, ben = two
    ga = _gl(ana)

    def run(*args, stdin=""):
        return subprocess.run(["git", *args], cwd=ga.wt, input=stdin, capture_output=True, text=True,
                              check=True).stdout.strip()
    blob = run("hash-object", "-w", "--stdin", stdin="not = [toml")
    root = run("commit-tree", run("mktree", stdin=f"100644 blob {blob}\tteam.toml\n"), "-m", "orphan")
    git("push", "-qf", "origin", f"{root}:refs/heads/levain-ledger", cwd=ga.wt)
    assert team("sync", repo=ben) == 2
    out = edit(ben, "src/settlement.py", session="tt")["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "team.toml" in out["permissionDecisionReason"]


def test_a_top_level_attributes_file_on_the_ledger_branch_is_tamper(two):
    # L2 (round 0) #5, RUN: a member committed `.gitattributes` (ledger/** working-tree-encoding=UTF-16) at the top of
    # the ledger branch; the judge looked only under ledger/, accepted it, and every later append failed at `git add`.
    tmp, ana, ben = two
    ga = _gl(ana)
    (ga.wt / ".gitattributes").write_text("ledger/** working-tree-encoding=UTF-16\n")
    _push_wt(ga, "attributes")
    assert team("sync", repo=ben) == 2
    assert any(".gitattributes" in t for t in ledger(ben).tamper)
    out = edit(ben, "src/unrelated.py", session="ga")["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"


def test_discovery_reads_the_top_level_and_the_git_dir_from_one_git_process(tmp_path, monkeypatch):
    # The would-be known limit "the top level and the git directory come from two git calls, so a directory swapped
    # between them (a retargeted symlink) can mix two repositories": one process, one discovery, no window.
    from levain.team import transport as T
    for name in ("proj", "proj\nx", "a\n/b"):
        d = tmp_path / name
        d.mkdir(parents=True)
        git("init", "-q", cwd=d)
        calls = []
        real = T.git

        def counting(args, cwd, **kw):
            calls.append(args)
            return real(args, cwd, **kw)
        monkeypatch.setattr(T, "git", counting)
        repo = Repo.discover(d / "sub" / "f.py")
        monkeypatch.setattr(T, "git", real)
        assert len(calls) == 1, calls
        assert os.path.realpath(repo.toplevel) == os.path.realpath(d), name
