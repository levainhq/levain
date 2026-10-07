"""The tip-only ledger's one judgement and its acceptance (the quotepath named fix, 2026-10-07).

Every test here was a failure RUN on the redesign's last reviewed tip (seat/1005-3-qp2 @6ef300e) before the fix: one
side-effect-free ``judge`` for every reader, writer and sync; the pin advance as the read's acceptance transaction;
every git failure while judging a hook deny.
"""
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from levain.team import entry as E
from levain.team.transport import GitLedger, LedgerReadError, Repo, TeamError

from tests.test_team_git import _own_file, _pinned, _pins_file, _push_wt, clone, edit, git, hook, ledger, record_ruling, team, two  # noqa: F401


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
    assert _pinned(ben) == {}
    ledger(ben)
    assert _pinned(ben)


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
    rel = next(k for k in _pinned(ben) if k.startswith("ana/"))
    _pins_file(ben).write_text(json.dumps({rel: ["an old pinned line"]}))
    assert not ledger(ben).tamper
    assert set(_pinned(ben)[rel]) == {"sha256", "length"}


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
    (gl.wt / "ledger" / "ana").mkdir(parents=True, exist_ok=True)
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
    before = _pinned(ben)
    assert gb.fetch_only(interval=0, timeout=30) is None
    gb.ledger(rev=gb.remote_ref())                                           # a newer, non-local rev
    gb.judge_remote(gb.remote_ref())
    assert _pinned(ben) == before


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
    git("add", "--", ".gitattributes", cwd=ga.wt)          # only this file: re-adding a ledger file under UTF-16 fails
    git("commit", "-qm", "attributes", cwd=ga.wt)
    git("push", "-q", "origin", "HEAD:levain-ledger", cwd=ga.wt)
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


def test_a_join_seeded_with_a_teammates_pins_refuses_a_ledger_rewritten_before_it_joined(two, capsys):
    # The would-be known limit "TOFU per clone" (head ruling: build the ssh-style seed). RUN before: a clone that
    # joined after a rewrite trusted the rewritten file silently.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    assert record_ruling(ana, "src/b.py", "second") == 0
    assert team("sync", repo=ben) == 0
    ledger(ben)
    seed = tmp / "ben_pins.json"
    seed.write_bytes(_pins_file(ben).read_bytes())
    ga = _gl(ana)
    git("reset", "-q", "--hard", "HEAD~1", cwd=ga.wt)
    git("push", "-qf", "origin", "HEAD:levain-ledger", cwd=ga.wt)
    cat = clone(tmp, "cat", "ben@ex.com")
    capsys.readouterr()
    assert team("join", "--pins-from", str(seed), "--no-install", repo=cat) == 2
    err = capsys.readouterr().err
    assert str(seed) in err and "rewritten or removed" in err
    assert not _gl(cat).joined()


def test_a_seeded_join_is_verified_and_an_unseeded_one_says_it_trusted_first_sight(two, capsys):
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    assert team("sync", repo=ben) == 0
    ledger(ben)
    seed = tmp / "ben_pins.json"
    seed.write_bytes(_pins_file(ben).read_bytes())
    cat = clone(tmp, "cat", "ben@ex.com")
    capsys.readouterr()
    assert team("join", "--pins-from", str(seed), "--no-install", repo=cat) == 0
    out = capsys.readouterr().out
    assert "first sight" not in out and "Block force pushes" in out
    assert team("status", repo=cat) == 0 and "first sight" not in capsys.readouterr().out
    dan = clone(tmp, "dan", "ben@ex.com")
    assert team("join", "--no-install", repo=dan) == 0
    assert "first sight trusted" in capsys.readouterr().out
    assert team("status", repo=dan) == 0 and "first sight trusted" in capsys.readouterr().out
    bad = tmp / "not_pins.json"
    bad.write_text('{"x": 1}')
    eve = clone(tmp, "eve", "ben@ex.com")
    assert team("join", "--pins-from", str(bad), "--no-install", repo=eve) == 2
    assert "not a levain pins file" in capsys.readouterr().err


def test_a_team_toml_that_is_not_utf8_is_a_team_error_not_a_crash(two):
    # E review (complement LOW a), RUN: `member add` on a non-UTF-8 team.toml raised a raw UnicodeDecodeError.
    from levain.team.transport import TeamError
    tmp, ana, ben = two
    ga = _gl(ana)
    (ga.wt / "team.toml").write_bytes(b'project = "p\xff"\n' + (ga.wt / "team.toml").read_bytes())
    git("add", "-A", ".", cwd=ga.wt)
    git("commit", "-qm", "not utf-8", cwd=ga.wt)
    with pytest.raises(TeamError, match="not valid UTF-8"):
        ga.update_team(lambda t: None, "x", push=False)


def test_read_plain_closes_its_descriptor_when_fdopen_fails(two, monkeypatch):
    # E review (gemini LOW c), RUN: 20 failed reads leaked 20 descriptors.
    tmp, ana, ben = two
    ga = _gl(ana)
    before = len(os.listdir("/dev/fd"))

    def boom(*a, **k):
        raise ValueError("injected")
    monkeypatch.setattr(os, "fdopen", boom)
    for _ in range(20):
        with pytest.raises(ValueError):
            ga._read_plain("team.toml")
    monkeypatch.undo()
    assert len(os.listdir("/dev/fd")) - before <= 1


def test_a_rewritten_top_level_file_gets_the_mode_git_checkout_would_give_it(two, monkeypatch):
    # E review (gemini LOW d), RUN: under umask 002 _replace_plain forced 0o644 where git's checkout gives 0o664.
    tmp, ana, ben = two
    ga = _gl(ana)
    from levain.team import transport as T
    monkeypatch.setattr(T, "_UMASK", 0o002)                  # read once at import, before any thread exists
    ga._replace_plain("PROJECT.md", "x\n")
    assert os.stat(ga.wt / "PROJECT.md").st_mode & 0o777 == 0o664


def test_a_warning_reaches_the_hook_output_once(two):
    # E review (complement LOW f), RUN: one team.toml fallback was printed twice at SessionStart, three times at an
    # edit (each team() read appends it again).
    tmp, ana, ben = two
    ga = _gl(ana)
    (ga.wt / "team.toml").write_text("not = [valid")
    git("add", "-A", ".", cwd=ga.wt)
    git("commit", "-qm", "bad toml", cwd=ga.wt)
    ctx = hook("sessionstart", {"session_id": "s", "cwd": str(ana), "hook_event_name": "SessionStart",
                                "source": "startup"})["hookSpecificOutput"]["additionalContext"]
    assert ctx.count("team.toml at the tip is unusable") == 1, ctx
    ctx = edit(ana, "src/billing.py")["hookSpecificOutput"]["additionalContext"]
    assert ctx.count("team.toml at the tip is unusable") == 1, ctx


@pytest.mark.parametrize("args", [["--recheck-days", "-1"], ["--ack-flag", "0"]])
def test_team_view_refuses_a_negative_recheck_or_a_zero_ack_flag(two, monkeypatch, args):
    # E review (codex LOW h), RUN: both were accepted and passed to the view.
    import levain.team.view as V
    tmp, ana, ben = two
    called = []
    monkeypatch.setattr(V, "serve", lambda *a, **k: called.append(k) or 0)
    with pytest.raises(SystemExit):
        team("view", *args, repo=ben)
    assert called == []


def test_an_ack_with_no_later_write_reaches_the_remote_at_the_next_session_start(two, capsys):
    # E review (codex MED e), RUN: acks are committed push=False, so an ack with no later ledger write never left the
    # clone and the lead's view said no agent had acknowledged a ruling.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "a stays") == 0
    assert team("sync", repo=ben) == 0
    assert edit(ben, "src/a.py", session="k1")["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "permissionDecision" not in edit(ben, "src/a.py", session="k1")["hookSpecificOutput"]   # proceeds: an ack
    acked = lambda: subprocess.run(["git", "grep", "-q", '"type": "ack"', "levain-ledger"],  # noqa: E731
                                   cwd=tmp / "origin.git").returncode == 0
    assert not acked()
    hook("sessionstart", {"session_id": "s2", "cwd": str(ben), "hook_event_name": "SessionStart", "source": "startup"})
    assert acked()


def test_acks_that_cannot_be_sent_are_said_at_session_start_and_in_status(two, capsys):
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "a stays") == 0
    assert team("sync", repo=ben) == 0
    edit(ben, "src/a.py", session="k1")
    edit(ben, "src/a.py", session="k1")                                    # the ack, committed without a push
    git("remote", "set-url", "origin", str(tmp / "gone.git"), cwd=ben)
    ctx = hook("sessionstart", {"session_id": "s2", "cwd": str(ben), "hook_event_name": "SessionStart",
                                "source": "startup"})["hookSpecificOutput"]["additionalContext"]
    assert "not pushed yet" in ctx
    capsys.readouterr()
    assert team("status", repo=ben) == 0
    assert "not pushed yet" in capsys.readouterr().out


def test_a_users_own_git_fetch_cannot_move_the_floor_under_a_truncation(two):
    # L1 r1 #1 / L2 r1 #1 / E-team L3 codex HIGH, RUN: the floor was refs/remotes/origin/levain-ledger, which a plain
    # `git fetch` (default refspec) moves with no quarantine; a truncation of this clone's own pushed file then passed,
    # and `repin` adopted it.
    tmp, ana, ben = two
    assert record_ruling(ben, "src/f1.py", "F1") == 0
    assert record_ruling(ben, "src/f2.py", "F2") == 0
    assert team("sync", repo=ana) == 0
    ga = _gl(ana)
    f = next(p for p in (ga.wt / "ledger" / "ben").glob("*.jsonl") if p.name == f"{_gl(ben).device}.jsonl")
    f.write_text(f.read_text().splitlines()[0] + "\n")
    _push_wt(ga, "truncate ben's own file")
    git("fetch", "-q", "origin", cwd=ben)                                    # the developer's own, ordinary fetch
    assert team("sync", repo=ben) == 2
    assert team("repin", repo=ben) == 0
    assert team("sync", repo=ben) == 2                                       # no repin adopts it
    assert "F2" in git("grep", "-h", "F2", "levain-ledger", cwd=ben)


def test_a_sibling_device_under_the_same_handle_is_pinned_like_any_teammate(two):
    # Head precision on the floor rule: "own" is exactly this clone's device file; a sibling device's file under the
    # same handle is pinned, so one device cannot truncate another's history under the floor rule.
    tmp, ana, ben = two
    ben2 = clone(tmp, "ben2", "ben@ex.com")
    assert team("join", "--no-install", repo=ben2) == 0
    assert record_ruling(ben2, "src/s1.py", "S1") == 0
    assert record_ruling(ben2, "src/s2.py", "S2") == 0
    assert team("sync", repo=ben) == 0
    ledger(ben)                                                              # ben pins ben2's file
    gb2 = _gl(ben2)
    f = gb2.wt / "ledger" / "ben" / f"{gb2.device}.jsonl"
    f.write_text(f.read_text().splitlines()[0] + "\n")
    _push_wt(gb2, "truncate a sibling device's file")
    assert team("sync", repo=ben) == 2
    assert any(f.name in t and "rewritten" in t for t in ledger(ben).tamper)


@pytest.mark.parametrize("state", ['{"device": "not-hex", "remote": "origin"}', "{"])
def test_an_unreadable_or_invalid_state_json_in_a_joined_clone_denies(two, state):
    # L1 r1 #4 (a)(b), RUN: a bad device id raised outside the boundary and a corrupt state.json read as "not joined";
    # both failed OPEN at the hook in a clone that has a ledger.
    tmp, ana, ben = two
    (_gl(ben).base / "state.json").write_text(state)
    out = edit(ben, "src/settlement.py", session="st")["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"


def test_a_repository_git_cannot_read_still_denies_when_it_holds_ledger_state(two, monkeypatch, capsys):
    # L1 r1 #4 (c): discover failing (a timeout, safe.directory) failed open.
    from levain.team import hook as H, transport as T

    def broken(cls, start):
        raise T.TeamError("git rev-parse failed: timed out")
    tmp, ana, ben = two
    monkeypatch.setattr(T.Repo, "discover", classmethod(broken))
    H.pretooluse({"session_id": "s", "transcript_path": "/x", "cwd": str(ben), "hook_event_name": "PreToolUse",
                  "tool_name": "Edit", "tool_input": {"file_path": str(ben / "src" / "a.py")}, "tool_use_id": "t"})
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_sync_on_a_refused_tip_sets_an_interrupted_write_aside_instead_of_committing_it(two):
    # L1 r1 #2, RUN: sync's recovery committed this clone's interrupted entry onto a locally refused ledger.
    tmp, ana, ben = two
    assert record_ruling(ben, "src/a.py", "ben's first") == 0
    gb = _gl(ben)
    own = gb.wt / "ledger" / "ben" / f"{gb.device}.jsonl"
    (gb.wt / "ledger" / "ben" / "notes.txt").write_text("x\n")
    git("add", "--", "ledger/ben/notes.txt", cwd=gb.wt)
    git("commit", "-qm", "a stray file, committed locally", cwd=gb.wt)
    with open(own, "a") as fh:
        fh.write('{"interrupted": true}\n')
    head = git("rev-parse", "HEAD", cwd=gb.wt)
    team("sync", repo=ben)
    assert git("rev-parse", "HEAD", cwd=gb.wt) == head
    kept = list((gb.base / "set-aside").iterdir())
    assert kept and b"interrupted" in kept[0].read_bytes()


@pytest.mark.parametrize("plant", ["unreadable", "nested_repo"])
def test_a_git_dir_planted_in_a_subdirectory_does_not_take_an_edit_out_of_the_ledger(two, plant):
    # L3 r1 gemini HIGH (and its wider class), RUN: an unreadable src/.git made discovery fail and the hook fail open;
    # a real nested repository at src/ made the edit belong to a repository with no ledger. The pack governs
    # src/settlement.py, so the edit must be denied either way.
    tmp, ana, ben = two
    if plant == "unreadable":
        (ben / "src" / ".git").write_text("gitdir: /nowhere\n")
        os.chmod(ben / "src" / ".git", 0)
    else:
        git("init", "-q", cwd=ben / "src")
    try:
        out = edit(ben, "src/settlement.py", session="pl")["hookSpecificOutput"]
    finally:
        if plant == "unreadable":
            os.chmod(ben / "src" / ".git", 0o644)
    assert out["permissionDecision"] == "deny"


def test_a_remote_tip_without_team_toml_is_refused(two):
    # L3 r1 codex HIGH 1, RUN: a tip that deleted team.toml was accepted (team() fell back to an older version), and
    # the clone then read as not joined.
    tmp, ana, ben = two
    ga = _gl(ana)
    git("rm", "-q", "team.toml", cwd=ga.wt)
    git("commit", "-qm", "no team", cwd=ga.wt)
    git("push", "-q", "origin", "HEAD:levain-ledger", cwd=ga.wt)
    assert team("sync", repo=ben) == 2
    assert any("team.toml is missing" in t for t in ledger(ben).tamper)


def test_a_ledger_file_past_the_size_limit_is_denied_before_it_is_read(two, monkeypatch):
    # L3 r1 codex HIGH 2: every blob was read into memory before any check; a huge one could OOM-kill the hook.
    from levain.team import transport as T
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    assert team("sync", repo=ben) == 0
    (_gl(ben).base / "history.json").unlink(missing_ok=True)
    monkeypatch.setattr(T, "_MAX_LEDGER_FILE", 100)
    with pytest.raises(T.LedgerReadError, match="past levain's limits"):
        _gl(ben).ledger()


def test_a_hard_link_to_this_clones_file_never_receives_its_write(two):
    # L3 r1 codex HIGH 3: the single-link check could be raced (a link made after fstat got the append). The append is
    # copy-on-write now: the linked inode is never written, whenever the link was made.
    tmp, ana, ben = two
    assert record_ruling(ben, "src/a.py", "first") == 0
    gb = _gl(ben)
    own = gb.wt / "ledger" / "ben" / f"{gb.device}.jsonl"
    victim = tmp / "victim.jsonl"
    os.link(own, victim)
    before = victim.read_bytes()
    assert record_ruling(ben, "src/b.py", "second") == 0
    assert victim.read_bytes() == before and own.read_bytes() != before


def test_pins_that_could_not_be_read_back_are_never_written(two, monkeypatch):
    # L3 r1 codex MED 4, RUN: enough ledger files made a pins.json over the read cap; it was written anyway and every
    # later read refused it, with no way back.
    from levain.team import transport as T
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    assert team("sync", repo=ben) == 0
    _pins_file(ben).unlink(missing_ok=True)
    (_gl(ben).base / "history.json").unlink(missing_ok=True)
    monkeypatch.setattr(T, "_PINS_MAX_BYTES", 50)
    with pytest.raises(T.LedgerReadError, match="too many files"):
        _gl(ben).ledger()
    assert not _pins_file(ben).exists()


def test_recovery_sets_an_interrupted_write_aside_while_the_remote_is_refused(two):
    # L3 r1 codex MED 5, RUN: recovery judged only the local tip and committed onto a ledger refused by its quarantine.
    tmp, ana, ben = two
    assert record_ruling(ben, "src/a.py", "ben's first") == 0
    assert team("sync", repo=ana) == 0
    _plant_stray(_gl(ana))
    assert team("sync", repo=ben) == 2                                       # the remote is quarantined, refused
    gb = _gl(ben)
    own = gb.wt / "ledger" / "ben" / f"{gb.device}.jsonl"
    with open(own, "a") as fh:
        fh.write('{"interrupted": true}\n')
    head = git("rev-parse", "HEAD", cwd=gb.wt)
    team("sync", repo=ben)
    assert git("rev-parse", "HEAD", cwd=gb.wt) == head
    assert any(b"interrupted" in p.read_bytes() for p in (gb.base / "set-aside").iterdir())


def test_a_team_toml_that_is_not_utf8_never_becomes_a_configuration(two):
    # L3 r1 codex MED 7, RUN: git output was decoded with replacement, so project = "p\xff" parsed as "p�".
    tmp, ana, ben = two
    ga = _gl(ana)
    text = (ga.wt / "team.toml").read_bytes()
    (ga.wt / "team.toml").write_bytes(text.replace(b'project = "ledgerline"', b'project = "ledger\xffline"'))
    git("add", "--", "team.toml", cwd=ga.wt)
    git("commit", "-qm", "not utf-8", cwd=ga.wt)
    assert "�" not in ga.team().project


def test_two_set_asides_of_one_file_in_one_second_are_both_kept(two):
    # L3 r1 codex LOW 8: one-second names let the second set-aside overwrite the first.
    tmp, ana, ben = two
    assert record_ruling(ben, "src/a.py", "first") == 0
    gb = _gl(ben)
    rel = f"ledger/ben/{gb.device}.jsonl"
    for n in range(2):
        with open(gb.wt / rel, "a") as fh:
            fh.write(f'{{"try": {n}}}\n')
        gb._set_aside([rel])
    assert len(list((gb.base / "set-aside").iterdir())) == 2


def test_a_refusal_quarantined_before_the_remote_branch_was_deleted_heals_with_a_sync(two):
    # L3 r1 complement MED 2, checked: the quarantine ref outlives a deleted remote branch, but `levain team sync`
    # pushes this clone's accepted tip, the refresh judges it accepted, and the record clears.
    tmp, ana, ben = two
    _plant_stray(_gl(ana))
    assert team("sync", repo=ben) == 2
    git("push", "-q", "origin", ":levain-ledger", cwd=ana)
    assert ledger(ben).tamper
    assert team("sync", repo=ben) == 0
    assert not ledger(ben).tamper


def test_repin_outside_a_joined_clone_says_so(tmp_path, capsys):
    # L3 r1 glm LOW: repin ran in an unjoined clone and printed "no pins to drop".
    git("init", "-q", cwd=tmp_path)
    assert team("repin", repo=tmp_path) == 2
    assert "has not joined" in capsys.readouterr().err


def test_the_size_limit_counts_a_blob_once_per_path_that_holds_it(two, monkeypatch):
    # L3 r2 codex HIGH 2: the total counted unique blobs, so one blob at many paths passed the limit while every path
    # was decoded and held separately.
    from levain.team import transport as T
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    ga = _gl(ana)
    f = _own_file(ga)
    (f.parent / "0123456789abcdef.jsonl").write_bytes(f.read_bytes())          # the same blob at a second path
    _push_wt(ga, "one blob, two paths")
    size = len(f.read_bytes())
    monkeypatch.setattr(T, "_MAX_LEDGER_TOTAL", int(size * 1.5))
    with pytest.raises(T.LedgerReadError, match="past levain's limits"):
        ga.judge(ga.head(), ga.team(), {})
    monkeypatch.setattr(T, "_MAX_LEDGER_TOTAL", 512 << 20)
    monkeypatch.setattr(T, "_MAX_LEDGER_LEAVES", 1)
    with pytest.raises(T.LedgerReadError, match="files, past levain's limit"):
        ga.judge(ga.head(), ga.team(), {})


def test_a_join_whose_remote_team_toml_never_parses_changes_nothing(two, capsys):
    # L3 r2 codex MED 5, RUN: the branch, accepted ref, state and worktree were created before the failure.
    tmp, ana, ben = two
    raw = tmp / "raw.git"
    git("clone", "-q", "--bare", str(tmp / "origin.git"), str(raw), cwd=tmp)
    # make every version unparseable: an orphan commit with only the broken team.toml
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=raw, input="not = [toml", text=True,
                          capture_output=True, check=True).stdout.strip()
    tree = subprocess.run(["git", "mktree"], cwd=raw, input=f"100644 blob {blob}\tteam.toml\n", text=True,
                          capture_output=True, check=True).stdout.strip()
    commit = git("-c", "user.name=ana", "-c", "user.email=ana@ex.com", "commit-tree", tree, "-m", "orphan",
                 cwd=raw).strip()                     # a bare clone has no identity of its own (CI has no global one)
    git("push", "-qf", str(tmp / "origin.git"), f"{commit}:refs/heads/levain-ledger", cwd=raw)
    cat = clone(tmp, "cat", "ben@ex.com")
    assert team("join", "--no-install", repo=cat) == 2
    gc = _gl(cat)
    assert not gc.joined() and not gc._local_branch_exists() and gc.remote_ref() is None


def test_a_duplicate_id_under_a_non_member_folder_is_still_tamper(two):
    # L3 r2 codex LOW 7: the cross-file duplicate check skipped folders of non-members.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    ga = _gl(ana)
    f = _own_file(ga)
    (ga.wt / "ledger" / "eve").mkdir()
    (ga.wt / "ledger" / "eve" / "0123456789abcdef.jsonl").write_bytes(f.read_bytes())
    _push_wt(ga, "a copy under a non-member")
    assert any("both hold entry id" in t for t in ga.ledger().tamper)


def test_a_remote_tip_is_judged_and_accepted_inside_the_pins_lock(two, monkeypatch):
    # L3 r2 codex HIGH 3: a fetch judged with pins read before a concurrent read advanced them, then advanced the
    # accepted tip anyway. One read-judge-write under pins.lock: while judging, the lock is held.
    from levain.team import transport as T
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    gb = _gl(ben)
    real = T.GitLedger.judge_remote
    held = []

    def probe(self, rev, rec=None):
        try:
            with self.lock(name="pins.lock", timeout=0):
                held.append(False)
        except T.TeamBusy:
            held.append(True)
        return real(self, rev, rec)
    monkeypatch.setattr(T.GitLedger, "judge_remote", probe)
    assert gb.fetch_only(interval=0, timeout=30) is None
    assert held and all(held)


def test_repin_then_a_read_then_a_sync_adopts_the_quarantined_rewrite(two):
    # L3 r2 codex MED 4, RUN: a read between repin and sync re-pinned the old tip, so the sync refused the same rewrite
    # again. While a quarantined tip waits, reads pin nothing.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    assert record_ruling(ana, "src/b.py", "second") == 0
    assert team("sync", repo=ben) == 0
    ledger(ben)
    ga = _gl(ana)
    git("reset", "-q", "--hard", "HEAD~1", cwd=ga.wt)
    git("push", "-qf", "origin", "HEAD:levain-ledger", cwd=ga.wt)
    assert team("sync", repo=ben) == 2
    assert team("repin", repo=ben) == 0
    ledger(ben)                                                              # e.g. a hook or `status` in between
    assert team("sync", repo=ben) in (0, 2)
    assert not _gl(ben).incoming_refusal()                                   # the rewrite is no longer refused


def test_nothing_reads_the_accepted_anchor_ref_to_decide():
    # Head ruling (C): refs/levain/accepted is a gc anchor written from pins.json; trust comes only from the record.
    import inspect
    from levain.team import transport as T
    src = inspect.getsource(T)
    assert "_ref_sha(_ACCEPTED)" not in src and src.count("_ACCEPTED") == 2   # the definition and the one write
    assert "_ref_sha(_REFUSED)" not in src and src.count("_REFUSED") == 3     # the definition, its write, its delete
    # L1 r3 NOTE 14: nor through the literal ref name, in any team module.
    import pathlib
    def code(text):
        return "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    named = [f.name for f in pathlib.Path(T.__file__).parent.glob("*.py") if "levain/accepted" in code(f.read_text())]
    assert named == ["transport.py"] and code(src).count("levain/accepted") == 1


def test_moving_the_anchor_ref_changes_no_judgement(two):
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    assert team("sync", repo=ben) == 0
    gb = _gl(ben)
    acc = gb.accepted_tip()
    git("update-ref", "refs/levain/accepted/levain-ledger", gb.head() + "~1", cwd=ben)
    assert gb.accepted_tip() == acc and gb.remote_ref() == acc
    git("update-ref", "-d", "refs/levain/accepted/levain-ledger", cwd=ben)
    assert gb.accepted_tip() == acc and team("sync", repo=ben) == 0


def test_a_clone_without_an_accepted_tip_refuses_the_remote_until_it_re_joins(two, capsys):
    # Head ruling: a MISSING accepted tip is never a silent empty floor. A clone from before levain kept one (its
    # pins.json in the v1 shape) is told to re-join, and the re-join records it from the shared history.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    assert team("sync", repo=ben) == 0
    ledger(ben)
    _pins_file(ben).write_text(json.dumps(_pinned(ben)))                     # the v1 shape: no accepted tip
    assert record_ruling(ana, "src/b.py", "second") == 0
    capsys.readouterr()
    assert team("sync", repo=ben) == 2
    assert "levain team join" in capsys.readouterr().err
    assert team("join", "--no-install", repo=ben) == 0
    assert team("sync", repo=ben) == 0


_STAGES = [("transport.Repo", "discover"), ("transport.GitLedger", "joined"), ("transport.GitLedger", "fetch_if_due"),
           ("transport.GitLedger", "team"), ("transport.GitLedger", "snapshot"), ("transport.GitLedger", "handle"),
           ("transport.GitLedger", "session_denied"), ("hook", "decide"), ("hook", "_edit_verdict")]


@pytest.mark.parametrize("where", [f"{m}.{n}" for m, n in _STAGES])
def test_any_failure_at_any_stage_of_judging_an_edit_is_a_deny(two, monkeypatch, capsys, where):
    # Head ruling (A) after L3 r2 codex HIGH 1 / complement MED 1: ONE boundary for the whole edit hook.
    import importlib
    from levain.team import hook as H
    tmp, ana, ben = two
    mod, name = where.rsplit(".", 1)
    path = mod.split(".")
    obj = importlib.import_module("levain.team." + path[0])
    for part in path[1:]:
        obj = getattr(obj, part)

    def boom(*a, **k):
        raise RecursionError("injected at " + where)
    monkeypatch.setattr(obj, name, boom)
    H.pretooluse({"session_id": "s", "transcript_path": "/x", "cwd": str(ben), "hook_event_name": "PreToolUse",
                  "tool_name": "Edit", "tool_input": {"file_path": str(ben / "src" / "settlement.py")},
                  "tool_use_id": "t"})
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny", where


@pytest.mark.parametrize("cwd", [123, ["x"], {"a": 1}])
def test_a_malformed_cwd_never_breaks_the_boundary(two, cwd):
    # L0 on 40a838c, RUN: a payload cwd that is not a string raised inside the judgement AND again inside the
    # boundary's own ledger check, so for a target outside any clone the exception reached main(), which printed
    # "ledger unavailable" for a ledger that does not exist. The boundary's own check must not raise.
    tmp, ana, ben = two
    outside = tmp / "elsewhere"
    outside.mkdir()
    base = {"session_id": "cw", "transcript_path": "/x", "cwd": cwd, "hook_event_name": "PreToolUse",
            "tool_name": "Edit", "tool_use_id": "t"}
    assert hook("pretooluse", {**base, "tool_input": {"file_path": str(outside / "a.py")}}) == {}
    out = hook("pretooluse", {**base, "tool_input": {"file_path": str(ben / "src" / "settlement.py")}})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_team_toml_nested_past_the_parser_denies(two):
    # L3 r2 codex HIGH 1, RUN: a deeply nested team.toml raised RecursionError from _interval, outside the old
    # per-call boundary, and the hook failed open.
    tmp, ana, ben = two
    gb = _gl(ben)
    (gb.wt / "team.toml").write_text("x = " + "[" * 100000 + "]" * 100000 + "\n")
    git("add", "--", "team.toml", cwd=gb.wt)
    git("commit", "-qm", "deep", cwd=gb.wt)
    assert edit(ben, "src/settlement.py", session="deep")["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_an_edit_through_a_symlinked_path_still_finds_its_clone(two):
    # L3 r2 complement MED 1: the ledger-roots walk used the unresolved path, so an edit through a symlink outside the
    # clone found no ledger and, with discovery failing (an unreadable .git planted beside it), failed open.
    tmp, ana, ben = two
    link = tmp / "elsewhere"
    link.symlink_to(ben / "src")
    (ben / "src" / ".git").write_text("gitdir: /nowhere\n")
    os.chmod(ben / "src" / ".git", 0)
    try:
        out = hook("pretooluse", {"session_id": "sy", "transcript_path": "/x", "cwd": str(tmp),
                                  "hook_event_name": "PreToolUse", "tool_name": "Edit",
                                  "tool_input": {"file_path": str(link / "settlement.py")}, "tool_use_id": "t"})
    finally:
        os.chmod(ben / "src" / ".git", 0o644)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_the_repositorys_own_attributes_never_reach_a_ledger_write(two):
    # L2 r1 #7 (RUN): `*.jsonl working-tree-encoding=UTF-16` in .git/info/attributes broke every record ("BOM is
    # required") and left the write dirty for good. Ledger bytes are staged with hash-object --no-filters.
    tmp, ana, ben = two
    info = ben / ".git" / "info"
    info.mkdir(exist_ok=True)
    (info / "attributes").write_text("*.jsonl working-tree-encoding=UTF-16\n")
    assert record_ruling(ben, "src/a.py", "attributes do not reach me") == 0
    assert "attributes do not reach me" in [e.get("words") for e in ledger(ben).entries]
    gb = _gl(ben)
    blob = git("cat-file", "blob", f"HEAD:ledger/ben/{gb.device}.jsonl", cwd=gb.wt)
    assert "attributes do not reach me" in blob                               # UTF-8, exactly as written


def test_flush_unpushed_ends_within_its_bound_against_a_slow_remote(two, monkeypatch):
    # Head addendum (L2 r1 #8 / complement r2 LOW 3): the flush bounded each git call, not the whole round.
    from levain.team import transport as T
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "a stays") == 0
    assert team("sync", repo=ben) == 0
    edit(ben, "src/a.py", session="k1")
    edit(ben, "src/a.py", session="k1")                                    # an ack, committed without a push
    gb = _gl(ben)
    assert gb.unpushed()
    real = T.git

    def slow(args, cwd, **kw):
        if args and args[0] in ("fetch", "push") or args[:3] == ["-c", "fetch.fsckObjects=true", "fetch"]:
            time.sleep(min(kw.get("timeout", 60), 0.8))
        return real(args, cwd, **kw)
    monkeypatch.setattr(T, "git", slow)
    t0 = time.monotonic()
    gb.flush_unpushed(timeout=1.0)
    assert time.monotonic() - t0 < 2.0


def test_a_failed_fetch_is_saved_where_every_reader_sees_it(two):
    # E-team LOW on dda91e3: a failed fetch raised without saving last_fetch_error, so the hook, doctor and a second
    # view saw no error.
    tmp, ana, ben = two
    gb = _gl(ben)
    git("remote", "set-url", "origin", str(tmp / "gone.git"), cwd=ben)
    note = gb.fetch_only(interval=0, timeout=30)
    assert note and "fetch failed" in note
    assert "fetch failed" in GitLedger(Repo.discover(ben)).state().get("last_fetch_error", "")


def test_a_device_file_pushed_under_another_member_is_shown(two, capsys):
    # Head ruling on L3 r1 complement MED 1: attribution is by folder (authenticated only by the git host), so a new
    # device file under ana's folder, pushed by ben, is not refused but must be SHOWN in status and at session start.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    assert team("sync", repo=ben) == 0
    ledger(ana)                                                              # ana's first sight is behind her
    gb = _gl(ben)
    forged = E.seal(E.build("ana", "decision", kind="ruling", owner="client:Dana", paths=["src/z.py"],
                            words="ana never said this"), "")
    (gb.wt / "ledger" / "ana").mkdir(exist_ok=True)
    (gb.wt / "ledger" / "ana" / "fedcba9876543210.jsonl").write_text(json.dumps(forged, sort_keys=True) + "\n")
    _push_wt(gb, "a device under ana's folder")
    assert team("sync", repo=ana) == 0
    ledger(ana)
    capsys.readouterr()
    assert team("status", repo=ana) == 0
    assert "fedcba9876543210" in capsys.readouterr().out
    ctx = hook("sessionstart", {"session_id": "nd", "cwd": str(ana), "hook_event_name": "SessionStart",
                                "source": "startup"})["hookSpecificOutput"]["additionalContext"]
    assert "new device fedcba9876543210 under ana" in ctx


def _forge_last_line(gl, words):
    """Rewrite the last entry of ana's own file to say ``words``, resealed so it verifies: a rewrite, not damage."""
    f = _own_file(gl)
    lines = f.read_text().splitlines(keepends=True)
    e = json.loads(lines[-1])
    e["words"] = words
    prev = json.loads(lines[-2])["hash"] if len(lines) > 1 else ""
    e = E.seal({k: v for k, v in e.items() if k not in ("hash", "prev")}, prev)
    f.write_text("".join(lines[:-1]) + json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n")
    git("add", "-A", ".", cwd=gl.wt)
    git("commit", "-qm", "rewrite", cwd=gl.wt)
    git("push", "-q", "-f", "origin", "HEAD:levain-ledger", cwd=gl.wt)


@pytest.mark.parametrize("how", ["sync", "fetch_only"])
def test_bytes_accepted_from_the_remote_are_pinned_without_a_read(two, how):
    # L2 r3 MED 1, RAN (p3.py): a remote tip accepted by a fetch moved the accepted sha but pinned none of its files, so
    # with no read in between, the next remote tip could rewrite them and was accepted silently.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "the real words") == 0
    gb = _gl(ben)
    if how == "sync":
        assert team("sync", repo=ben) == 0
    else:
        assert gb.fetch_only(interval=0, timeout=30) is None
    _forge_last_line(_gl(ana), "FORGED")
    if how == "sync":
        assert team("sync", repo=ben) != 0
    else:
        assert "refused" in (gb.fetch_only(interval=0, timeout=30) or "")
    led = ledger(ben)
    assert "FORGED" not in [e.get("words") for e in led.entries]
    assert led.tamper


def _ss(repo):
    return hook("sessionstart", {"session_id": "nd", "cwd": str(repo), "hook_event_name": "SessionStart",
                                 "source": "startup"})["hookSpecificOutput"]["additionalContext"]


def test_a_seeded_join_announces_no_device_as_new(two):
    # L1 r3 MED 3, RAN: after a join seeded with --pins-from, every teammate device was announced as "new".
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    assert team("sync", repo=ben) == 0
    ledger(ben)
    seed = tmp / "ben_pins.json"
    seed.write_bytes(_pins_file(ben).read_bytes())
    cat = clone(tmp, "cat", "ben@ex.com")
    assert team("join", "--pins-from", str(seed), "--no-install", repo=cat) == 0
    ledger(cat)
    assert "new device" not in _ss(cat)


def test_a_record_migrated_from_v1_announces_no_device_as_new(two):
    # L1 r3 MED 3, RAN: a v1 pins.json (a bare map of file pins, no first-seen record) re-joined announced every device.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    assert team("sync", repo=ben) == 0
    ledger(ben)
    _pins_file(ben).write_text(json.dumps(_pinned(ben)))
    assert team("join", "--no-install", repo=ben) == 0
    ledger(ben)
    assert "new device" not in _ss(ben)


def test_a_device_added_later_is_announced_and_a_pack_file_is_worded_as_one(two):
    tmp, ana, ben = two
    ledger(ben)
    gl = _gl(ana)
    (gl.wt / "ledger" / "ben").mkdir(exist_ok=True)
    (gl.wt / "ledger" / "ben" / "ffffffffffffffff.jsonl").write_text("")
    (gl.wt / "ledger" / "pack-other-1.0").mkdir()
    (gl.wt / "ledger" / "pack-other-1.0" / "eeeeeeeeeeeeeeee.jsonl").write_text("")
    _push_wt(gl, "a device and a pack file nobody here added")
    assert team("sync", repo=ben) == 0
    ctx = _ss(ben)
    assert "new device ffffffffffffffff under ben" in ctx
    assert "new pack file pack-other-1.0/eeeeeeeeeeeeeeee" in ctx and "under pack-other" not in ctx


def test_a_branch_behind_what_a_fetch_accepted_still_reads_and_a_reset_below_a_read_refuses(two):
    # C1: bytes accepted from the remote are pinned before this clone's branch replays them (fetch_only replays
    # nothing), so a read of the branch that is merely BEHIND them is not a rewrite; a branch reset below what a read
    # of it accepted still is.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "one") == 0
    assert team("sync", repo=ben) == 0
    assert "one" in [e.get("words") for e in ledger(ben).entries]
    before = _gl(ben).head()
    assert record_ruling(ana, "src/b.py", "two") == 0
    gb = _gl(ben)
    assert gb.fetch_only(interval=0, timeout=30) is None
    led = ledger(ben)
    assert not led.tamper and "two" not in [e.get("words") for e in led.entries]
    assert team("sync", repo=ben) == 0
    assert "two" in [e.get("words") for e in ledger(ben).entries]
    git("update-ref", "refs/heads/levain-ledger", before, cwd=ben)
    assert ledger(ben).tamper


def test_the_trust_record_is_read_once_and_an_unreadable_one_raises(two):
    tmp, ana, ben = two
    assert team("sync", repo=ben) == 0
    gb = _gl(ben)
    rec = gb.trust_record()
    assert rec.accepted == gb.accepted_tip() and not gb.judge_remote(rec.accepted, rec).ledger.tamper
    _pins_file(ben).write_text("{not json")
    with pytest.raises(LedgerReadError):
        gb.trust_record()


def test_a_case_variant_of_a_members_folder_is_tamper_before_anything_is_written(tmp_path):
    # L2 r3 MED 2, RAN (p5b.py): on a case-insensitive filesystem a member pushed ledger/ben/<Ben's device>.jsonl beside
    # ledger/Ben/; the checkout collided the two, and Ben's next record committed the forged bytes over his own file.
    # The namespace is judged case-folded, so the variant is refused at the read, before recovery can write anything.
    git("init", "-q", "--bare", "--initial-branch=main", "origin.git", cwd=tmp_path)
    ana = clone(tmp_path, "ana", "ana@ex.com")
    (ana / "src").mkdir()
    (ana / "src" / "b.py").write_text("x\n")
    git("add", ".", cwd=ana)
    git("commit", "-qm", "i", cwd=ana)
    git("push", "-q", "origin", "HEAD:main", cwd=ana)
    assert team("init", "--project", "p", "--owner", "ana", "--member", "ana=ana@ex.com", "--member",
                "Ben=ben@ex.com", repo=ana) == 0
    ben = clone(tmp_path, "ben", "ben@ex.com")
    assert team("join", "--no-install", repo=ben) == 0
    assert record_ruling(ben, "src/b.py", "ben real ruling") == 0
    assert team("sync", repo=ana) == 0
    ga, gb = _gl(ana), _gl(ben)
    rel = f"ledger/ben/{gb.device}.jsonl"
    sha = subprocess.run(["git", "hash-object", "-w", "--stdin"], input=b'{"forged": 1}\n', capture_output=True,
                         cwd=ga.wt, check=True).stdout.decode().strip()
    git("update-index", "--add", "--cacheinfo", f"100644,{sha},{rel}", cwd=ga.wt)
    git("commit", "-qm", "case variant", cwd=ga.wt)
    git("push", "-q", "origin", "HEAD:levain-ledger", cwd=ga.wt)
    assert team("sync", repo=ben) != 0
    assert record_ruling(ben, "src/c.py", "ben second") != 0
    own = git("cat-file", "blob", f"levain-ledger:ledger/Ben/{gb.device}.jsonl", cwd=ben)
    assert "forged" not in own and "ben real ruling" in own
    # the owner's own clone refuses the variant too: it is in the same tree as Ben's folder
    led = ledger(ana)
    assert any("ledger/ben" in t and "case" in t for t in led.tamper), led.tamper


def test_a_case_variant_of_a_member_with_no_folder_yet_is_tamper(two):
    tmp, ana, ben = two
    gl = _gl(ana)
    (gl.wt / "ledger" / "BEN").mkdir()
    (gl.wt / "ledger" / "BEN" / "ffffffffffffffff.jsonl").write_text("")
    _push_wt(gl, "a folder ben's clone would write into")
    assert team("sync", repo=ben) != 0
    assert any("case variant" in t for t in ledger(ana).tamper)


def test_a_refusal_of_this_clones_own_file_never_offers_a_repin(two):
    # Head ruling on L2 r3 MED 2: repin would pin whatever replaced this clone's own lines.
    tmp, ana, ben = two
    assert record_ruling(ben, "src/a.py", "mine") == 0
    ledger(ben)
    gb = _gl(ben)
    rel = next((gb.wt / "ledger" / "ben").glob("*.jsonl")).relative_to(gb.wt).as_posix()
    sha = subprocess.run(["git", "hash-object", "-w", "--stdin"], input=b"", capture_output=True, cwd=gb.wt,
                         check=True).stdout.decode().strip()
    git("update-index", "--cacheinfo", f"100644,{sha},{rel}", cwd=gb.wt)
    git("commit", "-qm", "own file emptied", cwd=gb.wt)
    bad = ledger(ben).tamper
    assert bad and all("repin" not in t.replace("do not repin", "") for t in bad), bad
    assert any("this clone's own" in t for t in bad)


def test_recovery_commits_only_an_append_to_this_clones_own_file(two):
    # L2 r3 MED 2 (the second half), RAN: recovery staged whatever the worktree held at any folder's <device>.jsonl.
    tmp, ana, ben = two
    assert record_ruling(ben, "src/a.py", "mine") == 0
    gb = _gl(ben)
    f = next((gb.wt / "ledger" / "ben").glob("*.jsonl"))
    f.write_text('{"not": "an append"}\n')
    assert record_ruling(ben, "src/b.py", "second") == 0
    own = git("cat-file", "blob", f"levain-ledger:{f.relative_to(gb.wt).as_posix()}", cwd=ben)
    assert "an append" not in own and "mine" in own and "second" in own
    assert any((gb.base / "set-aside").iterdir())


def _edit_in_process(repo, rel="src/settlement.py"):
    from levain.team import hook as H
    import io
    import contextlib as _cl
    buf = io.StringIO()
    with _cl.redirect_stdout(buf):
        H.pretooluse({"session_id": "dl", "transcript_path": "/x", "cwd": str(repo), "hook_event_name": "PreToolUse",
                      "tool_name": "Edit", "tool_input": {"file_path": str(repo / rel)}, "tool_use_id": "t"})
    return json.loads(buf.getvalue()) if buf.getvalue().strip() else {}


def test_a_judgement_slowed_by_a_held_lock_is_denied_inside_the_deadline(two, monkeypatch):
    # Head ruling on L1 r3 MED 4: Claude Code lets an edit through when the hook is killed at its timeout, so a slow
    # judgement was a fail-open path. ONE deadline; running out while waiting for a lock is a deny.
    from levain.team import hook as H
    tmp, ana, ben = two
    monkeypatch.setattr(H, "_PRETOOLUSE_BUDGET", 1.5)
    gb = _gl(ben)
    (gb.base / "history.json").unlink(missing_ok=True)       # a read must accept, under pins.lock
    fd = os.open(gb.base / "pins.lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        t0 = time.monotonic()
        out = _edit_in_process(ben, "src/billing.py")
        took = time.monotonic() - t0
    finally:
        os.close(fd)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "took too long" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert took < 4


def test_a_judgement_slowed_by_git_is_denied_inside_the_deadline(two, monkeypatch):
    from levain.team import hook as H
    from levain.team import transport as T
    tmp, ana, ben = two
    monkeypatch.setattr(H, "_PRETOOLUSE_BUDGET", 1.5)
    real = T.subprocess.run

    def slow(cmd, *a, **k):
        if "ls-tree" in cmd:
            cmd = ["sh", "-c", "sleep 5"]
        return real(cmd, *a, **k)
    monkeypatch.setattr(T.subprocess, "run", slow)
    (_gl(ben).base / "history.json").unlink(missing_ok=True)
    t0 = time.monotonic()
    out = _edit_in_process(ben, "src/billing.py")
    assert time.monotonic() - t0 < 4
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "took too long" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_an_edit_outside_every_clone_is_not_judged_by_the_sessions_cwd(two):
    # L1 r3 LOW 11: the session's cwd widens only the boundary's deny condition; it is not a place to judge from.
    tmp, ana, ben = two
    outside = tmp / "elsewhere"
    outside.mkdir()
    (_gl(ben).base / "state.json").write_text('{"device": "not hex"}')
    out = hook("pretooluse", {"session_id": "cw", "transcript_path": "/x", "cwd": str(ben),
                              "hook_event_name": "PreToolUse", "tool_name": "Edit",
                              "tool_input": {"file_path": str(outside / "a.py")}, "tool_use_id": "t"})
    assert out == {}


def test_a_busy_session_record_keeps_the_rulings_words_in_the_deny(two):
    # L1 r3 LOW 9: a TeamBusy from mark_denied reached the boundary, and the deny lost the ruling's text.
    tmp, ana, ben = two
    gb = _gl(ben)
    fd = os.open(gb.base / "sessions.lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        out = edit(ben, "src/settlement.py", session="busy")["hookSpecificOutput"]
    finally:
        os.close(fd)
    assert out["permissionDecision"] == "deny" and "write_batch" in out["permissionDecisionReason"]


def test_a_deleted_process_cwd_does_not_break_the_boundary(tmp_path):
    # L1 r3 LOW 12: with no payload cwd and this process's own cwd deleted, the boundary's own check raised.
    gone = tmp_path / "gone"
    gone.mkdir()
    payload = json.dumps({"session_id": "g", "hook_event_name": "PreToolUse", "tool_name": "Edit",
                          "tool_input": {"file_path": "rel.py"}, "tool_use_id": "t"})
    import sys as _sys
    cp = subprocess.run(["sh", "-c", f'cd "{gone}" && rmdir "{gone}" && exec "$0" -P -m levain.team.hook pretooluse',
                         _sys.executable], input=payload, capture_output=True, text=True, timeout=60,
                        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.strip() == "", cp.stdout


def test_a_ledger_of_many_tiny_lines_is_refused_before_it_is_split(two):
    # L1 r3 HIGH 1, RAN: 2 MiB of "xy\n" (699,050 lines) peaked at 152 MiB, ~76x the byte bound. Lines are counted
    # before anything is split.
    import tracemalloc
    tmp, ana, ben = two
    gl = _gl(ana)
    (gl.wt / "ledger" / "ana").mkdir(exist_ok=True)
    (gl.wt / "ledger" / "ana" / "aaaaaaaaaaaaaaaa.jsonl").write_bytes(b"xy\n" * 699050)
    git("add", "-A", ".", cwd=gl.wt)
    git("commit", "-qm", "many lines", cwd=gl.wt)
    gb = _gl(ben)
    tracemalloc.start()
    try:
        with pytest.raises(LedgerReadError, match="lines, past"):
            gl.judge(gl.head(), gl.team(), {})
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 24 << 20, peak


def test_a_tree_past_the_record_limit_is_refused_before_it_is_split(two, monkeypatch):
    # L1 r3 MED 2: the ls-tree records are counted before the split, and a listing past the byte bound is never read.
    from levain.team import transport as T
    tmp, ana, ben = two
    gl = _gl(ana)
    for i in range(12):
        (gl.wt / f"junk{i}").write_text("x")
    git("add", "-A", ".", cwd=gl.wt)
    git("commit", "-qm", "junk", cwd=gl.wt)
    monkeypatch.setattr(T, "_MAX_TREE_RECORDS", 10)
    with pytest.raises(LedgerReadError, match="entries, past"):
        gl.judge(gl.head(), gl.team(), {})
    monkeypatch.setattr(T, "_MAX_TREE_RECORDS", 10_000)
    monkeypatch.setattr(T, "_MAX_TREE_BYTES", 100)
    with pytest.raises(LedgerReadError, match="past levain's limit of 100"):
        gl.judge(gl.head(), gl.team(), {})
    monkeypatch.setattr(T, "_MAX_TREE_BYTES", 10 << 20)
    monkeypatch.setattr(T, "_MAX_BAD_PATHS", 3)
    bad = gl.judge(gl.head(), gl.team(), {}).ledger.tamper
    assert len(bad) == 4 and bad[-1] == "and 9 more paths levain does not write", bad


def test_a_team_toml_past_its_bound_is_not_read(two, monkeypatch):
    # L2 r3 LOW-MED 5: team.toml and PROJECT.md were read whole with no size check.
    from levain.team import transport as T
    tmp, ana, ben = two
    gl = _gl(ana)
    monkeypatch.setattr(T, "_MAX_TOP_FILE", 10)
    with pytest.raises(LedgerReadError, match="past levain's limit of 10"):
        gl.team()


def test_problems_and_refusals_are_kept_bounded(monkeypatch):
    from levain.team import index as I
    monkeypatch.setattr(I, "MAX_PROBLEMS", 2)
    led = I.build([], None, ["a", "b", "c", "d"], tamper=["w", "x", "y"])
    assert led.file_problems == ["a", "b", "and 2 more problems"]
    assert led.tamper == ["w", "x", "and 1 more reasons"]


def test_a_refusal_that_appears_while_a_read_waits_for_the_pins_lock_stops_the_pin(two, monkeypatch):
    # L1 r3 LOW 10 / L2 r3 LOW 3: the no-pin-while-refused check ran before pins.lock was taken. A fetch that refuses
    # while a read judges writes Trust.refused under the lock; the read's acceptance meets it there and reads again.
    from levain.team import transport as T
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    assert team("sync", repo=ben) == 0
    gb = _gl(ben)
    (gb.base / "history.json").unlink(missing_ok=True)
    before = _pinned(ben)
    older = git("rev-parse", "levain-ledger~1", cwd=ben).strip()
    real = GitLedger.lock

    def lock(self, *a, **k):
        if k.get("name") == "pins.lock":
            rec = self.trust_record()
            if not rec.refused:
                T._atomic_write(self.base / "pins.json", T.dataclasses.replace(rec, refused=older).dump())
        return real(self, *a, **k)
    monkeypatch.setattr(GitLedger, "lock", lock)
    led = gb.ledger()
    assert _pinned(ben) == before and led.tamper


def test_a_refusal_of_the_accepted_tip_is_cleared(two):
    # L2 r3 LOW 7: a refusal naming the very tip the record accepted left reads never pinning.
    from levain.team import transport as T
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    assert team("sync", repo=ben) == 0
    gb = _gl(ben)
    rec = gb.trust_record()
    T._atomic_write(gb.base / "pins.json", T.dataclasses.replace(rec, refused=rec.accepted).dump())
    (gb.base / "history.json").unlink(missing_ok=True)
    gb.ledger()
    assert gb.trust_record().refused is None
    assert any(rel.startswith("ana/") for rel in _pinned(ben))           # the read pinned what it accepted


def test_levains_fetch_brings_no_tags_into_the_code_repository(two):
    # L2 r3 LOW 4, RAN (p2.py): levain's fetch auto-followed tags pushed onto ledger commits into the user's refs/tags.
    tmp, ana, ben = two
    gl = _gl(ana)
    assert record_ruling(ana, "src/a.py", "first") == 0
    git("tag", "-a", "v9.9-evil", "-m", "x", gl.head(), cwd=ana)
    git("push", "-q", "origin", "v9.9-evil", cwd=ana)
    assert record_ruling(ana, "src/b.py", "second") == 0
    assert team("sync", repo=ben) == 0
    dan = clone(tmp, "dan", "ben@ex.com")
    git("tag", "-d", "v9.9-evil", cwd=dan)
    assert team("join", "--no-install", repo=dan) == 0
    assert git("tag", "-l", cwd=ben).split() == [] and git("tag", "-l", cwd=dan).split() == []


def test_with_no_handle_this_clones_own_file_is_still_judged_as_its_own(two):
    # L1 r3 LOW-MED 5: with the handle unknown (the git email no longer maps to a member), this clone's own file was
    # judged by pins like a teammate's, so its own unpushed lines made the remote look rewritten.
    tmp, ana, ben = two
    assert team("record", "decision", "--kind", "ruling", "--owner", "client:Dana", "--paths", "src/x.py", "--words",
                "unpushed", "--no-push", repo=ben) == 0
    ledger(ben)                                              # pins ben's own file with the unpushed line
    git("config", "user.email", "someone-else@ex.com", cwd=ben)
    assert _gl(ben).incoming_refusal() == []
    gb = _gl(ben)
    assert gb.fetch_only(interval=0, timeout=30) is None, gb.state().get("last_fetch_error")


def test_a_repin_of_an_unreadable_record_keeps_the_accepted_tip_it_can_still_read(two, capsys):
    # L1 r3 LOW 8: a repin of an unreadable pins.json deleted the accepted tip with it, and the message then said sync.
    tmp, ana, ben = two
    assert team("sync", repo=ben) == 0
    gb = _gl(ben)
    acc = gb.accepted_tip()
    _pins_file(ben).write_text(json.dumps({"v": 3, "accepted": acc, "files": "not a map"}))
    assert team("repin", repo=ben) == 0
    assert gb.accepted_tip() == acc and team("sync", repo=ben) == 0
    _pins_file(ben).write_text("{not json")
    capsys.readouterr()
    assert team("repin", repo=ben) == 0
    assert "levain team join" in capsys.readouterr().out


def test_a_record_that_cannot_be_written_during_a_fetch_is_saved_as_the_fetch_error(two, monkeypatch):
    # L1 r3 LOW 13: an OSError from writing the trusted record escaped the fetch's error record.
    from levain.team import transport as T
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    gb = _gl(ben)
    real = T._atomic_write

    def full(path, *a, **k):
        if path.name == "pins.json":
            raise OSError(28, "No space left on device")
        return real(path, *a, **k)
    monkeypatch.setattr(T, "_atomic_write", full)
    note = gb.fetch_only(interval=0, timeout=30)
    assert note and "could not be recorded" in note
    assert "could not be recorded" in gb.state().get("last_fetch_error", "")


def test_a_rejoin_with_an_unrelated_local_branch_says_so(two, capsys):
    # L1 r3 LOW 6: the re-join's floor came from a blind merge-base; with no shared history it gets its own message.
    tmp, ana, ben = two
    gb = _gl(ben)
    tree = git("rev-parse", "levain-ledger^{tree}", cwd=ben).strip()
    orphan = git("commit-tree", tree, "-m", "unrelated", cwd=ben).strip()
    git("update-ref", "refs/heads/levain-ledger", orphan, cwd=ben)
    _pins_file(ben).unlink()
    capsys.readouterr()
    assert team("join", "--no-install", repo=ben) == 2
    assert "shares no history" in capsys.readouterr().err


def test_two_fetch_only_calls_at_once_fetch_once(two, monkeypatch):
    # E2 L3 (codex MED on 40a838c): the interval was checked before the net lock and never again, so a second process
    # that waited for the lock fetched again inside the interval.
    import threading
    from levain.team import transport as T
    tmp, ana, ben = two
    gb = _gl(ben)
    gb.save_state(last_fetch_attempt=0)
    real, fetches = T.git, []

    def slow(args, *a, **k):
        if "fetch" in args:
            fetches.append(1)
            time.sleep(0.3)
        return real(args, *a, **k)
    monkeypatch.setattr(T, "git", slow)
    out = []
    ts = [threading.Thread(target=lambda: out.append(_gl(ben).fetch_only(interval=60, timeout=30))) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(fetches) == 1, (fetches, out)


def test_a_last_fetch_in_the_future_is_due(two):
    # E2 residue: after a clock rollback a future last_fetch_attempt made every fetch a silent no-op until then.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    gb = _gl(ben)
    gb.save_state(last_fetch_attempt=time.time() + 3600)
    before = gb.accepted_tip()
    assert gb.fetch_only(interval=60, timeout=30) is None
    assert gb.accepted_tip() != before
    assert gb.state()["last_fetch_attempt"] <= time.time()


def test_a_failed_fetch_says_what_git_said_first_and_never_a_credential(two):
    # E2 residue: _tail reported "git fetch failed: and the repository exists." (git's LAST line) for a missing repo.
    from levain.team import transport as T
    tmp, ana, ben = two
    git("remote", "set-url", "origin", str(tmp / "nope.git"), cwd=ben)
    note = _gl(ben).fetch_only(interval=0, timeout=30)
    assert note and "does not appear to be a git repository" in note and "and the repository exists" not in note
    cp = subprocess.CompletedProcess([], 128, "", "hint: x\nfatal: unable to access 'https://bob:s3cret@host/r.git/': "
                                                  "403\n")
    assert T._tail(cp) == "fatal: unable to access 'https://***@host/r.git/': 403"


def test_a_stale_seed_on_a_joined_clone_merges_and_never_drops_a_stronger_pin(two):
    # L2 r3 LOW 6: --pins-from on a joined clone REPLACED its pins, so a stale seed silently dropped stronger ones.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    assert team("sync", repo=ben) == 0
    ledger(ben)
    stale = tmp / "stale.json"
    stale.write_bytes(_pins_file(ben).read_bytes())
    assert record_ruling(ana, "src/b.py", "second") == 0
    assert team("sync", repo=ben) == 0
    ledger(ben)
    strong = _pinned(ben)
    assert team("join", "--pins-from", str(stale), "--no-install", repo=ben) == 0
    assert all(_pinned(ben)[r]["length"] >= p["length"] for r, p in strong.items())


def _one_doc(raw: str) -> dict:
    """stdout must be exactly one JSON document (or nothing)."""
    if not raw.strip():
        return {}
    doc, end = json.JSONDecoder().raw_decode(raw)
    assert raw[end:].strip() == "", raw
    return doc


def _in_process(payload, capsys) -> tuple[dict, float]:
    from levain.team import hook as H
    t0 = time.monotonic()
    H.pretooluse(payload)
    took = time.monotonic() - t0
    return _one_doc(capsys.readouterr().out), took


def _edit_payload(repo, target):
    return {"session_id": "h", "transcript_path": "/x", "cwd": str(repo), "hook_event_name": "PreToolUse",
            "tool_name": "Edit", "tool_input": {"file_path": str(target)}, "tool_use_id": "t"}


def test_a_scope_scan_that_hangs_is_exactly_one_deny_inside_the_deadline(two, monkeypatch, capsys):
    # Head ruling on L3 r4 (the alarm path deleted): a worker thread judges, the caller waits at most the deadline, and
    # one document is written from one place. gemini + codex HIGH on r3 were a scope re-scan that could hang.
    from levain.team import hook as H
    tmp, ana, ben = two
    git("worktree", "add", "-q", str(tmp / "benwt"), cwd=ben)
    monkeypatch.setattr(H, "_PRETOOLUSE_BUDGET", 1.0)
    monkeypatch.setattr(H, "_JOIN_GRACE", 0.5)
    real = H._small_text

    def hang(path):
        if path.name == "commondir":
            time.sleep(30)
        return real(path)
    monkeypatch.setattr(H, "_small_text", hang)
    out, took = _in_process(_edit_payload(tmp / "benwt", tmp / "benwt" / "src" / "settlement.py"), capsys)
    assert took < 3 and out["hookSpecificOutput"]["permissionDecision"] == "deny", (took, out)
    assert "took too long" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_scope_scan_that_raises_is_a_deny(two, monkeypatch, capsys):
    from levain.team import hook as H
    tmp, ana, ben = two

    def boom(target):
        raise RecursionError("a scan that raises")
    monkeypatch.setattr(H, "_scan", boom)
    out, _took = _in_process(_edit_payload(ben, ben / "src" / "billing.py"), capsys)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_an_unreadable_dot_git_is_an_unknown_scope_and_a_deny(two):
    # codex HIGH on L3 r4: _ledger_roots swallowed the OSError of a mode-000 .git and stored "out of scope" (silence).
    tmp, ana, ben = two
    os.chmod(ben / ".git", 0)
    try:
        out = hook("pretooluse", _edit_payload(ben, ben / "src" / "billing.py"))
    finally:
        os.chmod(ben / ".git", 0o755)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_judgement_stuck_in_python_past_the_deadline_is_one_deny(two, monkeypatch, capsys):
    # complement MED on L3 r3: time spent outside git and locks (here a stuck sync) was unbounded once an alarm was
    # swallowed. There is no alarm now: the caller stops waiting at the deadline.
    from levain.team import hook as H
    from levain.team.transport import GitLedger as G
    tmp, ana, ben = two
    monkeypatch.setattr(H, "_PRETOOLUSE_BUDGET", 1.0)
    monkeypatch.setattr(H, "_JOIN_GRACE", 0.5)
    _gl(ben).save_state(last_fetch_attempt=0)

    def slow_sync(self, **k):
        time.sleep(30)
    monkeypatch.setattr(G, "_sync", slow_sync)
    out, took = _in_process(_edit_payload(ben, ben / "src" / "billing.py"), capsys)
    assert took < 3 and out["hookSpecificOutput"]["permissionDecision"] == "deny", (took, out)


def test_a_long_deny_is_still_exactly_one_document(two):
    # codex HIGH on L3 r4: two write sites could put a second document after the first. One encode, one write site.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/long.py", "a long ruling. " * 100) == 0
    assert team("sync", repo=ben) == 0
    cp = subprocess.run([sys.executable, "-P", "-m", "levain.team.hook", "pretooluse"],
                        input=json.dumps(_edit_payload(ben, ben / "src" / "long.py")), capture_output=True, text=True,
                        timeout=60)
    assert len(cp.stdout) > 512
    out = _one_doc(cp.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "a long ruling" in out["permissionDecisionReason"]
def test_a_branch_behind_the_remote_must_be_a_prefix_of_what_was_accepted(two):
    # codex HIGH on L3 r3: a local file SHORTER than its remote pin was skipped, so a local rewrite to A+C (shorter than
    # the accepted A+B) passed and was pinned. It is compared against that file in the accepted commit.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    assert team("sync", repo=ben) == 0
    ledger(ben)                                                     # pins A
    assert record_ruling(ana, "src/b.py", "a long ruling " * 40) == 0
    gb = _gl(ben)
    assert gb.fetch_only(interval=0, timeout=30) is None            # accepts A+B, branch still at A
    f = gb.wt / "ledger" / "ana" / _own_file(_gl(ana)).name
    lines = f.read_text().splitlines(keepends=True)
    e = json.loads(lines[-1])
    forged = E.seal({**{k: v for k, v in e.items() if k not in ("hash", "prev")}, "id": e["id"][:-8] + "0badf00d",
                     "words": "forged"}, e["hash"])
    f.write_text("".join(lines) + json.dumps(forged, ensure_ascii=False, sort_keys=True) + "\n")
    git("add", "-A", ".", cwd=gb.wt)
    git("commit", "-qm", "a local rewrite", cwd=gb.wt)
    assert len(f.read_bytes()) < gb.trust_record().remote[f"ana/{f.name}"]["length"]
    led = ledger(ben)
    assert led.tamper and "forged" not in [x.get("words") for x in led.entries]


def test_a_fetch_records_its_refusal_only_with_its_judgement(two, monkeypatch):
    # Head ruling (C)2 on L3 r3, codex HIGH: the fetch published the refusal before it took pins.lock and judged.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "x") == 0
    assert team("sync", repo=ben) == 0
    _forge_last_line(_gl(ana), "FORGED")
    gb = _gl(ben)
    seen = []
    real = GitLedger.judge_remote

    def spy(self, rev, rec=None):
        seen.append(self.trust_record().refused)
        return real(self, rev, rec)
    monkeypatch.setattr(GitLedger, "judge_remote", spy)
    assert "refused" in (gb.fetch_only(interval=0, timeout=30) or "")
    assert seen and seen[0] is None
    assert gb.trust_record().refused and gb.incoming_refusal()       # written with the judgement


def test_a_read_served_from_the_cache_still_checks_the_quarantine_under_the_lock(two):
    # Head ruling (C)2: every read, cache hits included, checks the quarantine under pins.lock.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "x") == 0
    assert team("sync", repo=ben) == 0
    gb = _gl(ben)
    ledger(ben)
    ledger(ben)                                                      # the cache is warm
    fd = os.open(gb.base / "pins.lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        from levain.team import transport as T
        with T.deadline(1.0), pytest.raises(T.TeamError):           # waits for the lock; never served unlocked
            gb.ledger()
    finally:
        os.close(fd)


def test_a_push_that_cannot_be_recorded_says_so(two, monkeypatch):
    # Head ruling (C)3 on L3 r3, codex HIGH: _record_pushed failures (and the follow-up fetch's) were suppressed and
    # "pushed" returned, so the pushed bytes went unpinned.
    tmp, ana, ben = two
    gb = _gl(ben)
    assert team("record", "decision", "--kind", "ruling", "--owner", "client:Dana", "--paths", "src/x.py", "--words",
                "w", "--no-push", repo=ben) == 0

    def cannot(self, *a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(GitLedger, "_record_pushed", cannot)
    real, calls = GitLedger._fetch_quarantined, []

    def second_fails(self, remote, timeout):
        calls.append(1)
        if len(calls) > 1:
            raise TeamError("the remote did not answer")
        return real(self, remote, timeout)
    monkeypatch.setattr(GitLedger, "_fetch_quarantined", second_fails)
    out = gb.sync()
    assert out.startswith("pushed, but not recorded") and "levain team sync" in out, out
    assert "not recorded" in gb.state()["last_fetch_error"]
    monkeypatch.setattr(GitLedger, "_fetch_quarantined", real)
    assert team("record", "decision", "--kind", "ruling", "--owner", "client:Dana", "--paths", "src/y.py", "--words",
                "v", "--no-push", repo=ben) == 0
    assert gb.sync() == "pushed" and gb.accepted_tip() == gb.head()      # the follow-up fetch recorded it


def test_a_seed_carries_what_the_teammate_accepted_from_the_remote(two):
    # codex HIGH on L3 r3: seed_pins_from returned only the teammate's read pins, dropping its remote pins.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    assert team("sync", repo=ben) == 0
    ledger(ben)
    assert record_ruling(ana, "src/b.py", "second") == 0
    assert _gl(ben).fetch_only(interval=0, timeout=30) is None      # ben's record: remote holds A+B, files hold A
    seed = tmp / "ben_pins.json"
    seed.write_bytes(_pins_file(ben).read_bytes())
    _forge_last_line(_gl(ana), "FORGED")                            # the remote rewrites B
    cat = clone(tmp, "cat", "ben@ex.com")
    assert team("join", "--pins-from", str(seed), "--no-install", repo=cat) == 2


def test_a_sweep_by_another_call_cannot_lose_a_fetch(two, monkeypatch):
    # RAN on CI (runs 37671919223, 37676002591, test_concurrent_writers_in_both_clones_converge): a concurrent call
    # deleted the one shared quarantine ref between a fetch's write and its read-back. Head ruling (a): each fetch owns
    # a ref of its own, and the sweep of crashed calls' refs skips any whose pid is alive.
    from levain.team import transport as T
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "x") == 0
    gb = _gl(ben)
    real = T.git

    def fetch_then_sweep(args, cwd, **k):
        out = real(args, cwd, **k)
        if "fetch" in args:
            GitLedger(Repo.discover(ben))._sweep_call_refs()          # another call's sweep, mid-fetch
        return out
    monkeypatch.setattr(T, "git", fetch_then_sweep)
    assert gb.sync() in ("fetched", "up to date", "pushed")
    left = git("for-each-ref", "--format=%(refname)", "refs/levain/incoming/", "refs/levain/join/", cwd=ben)
    assert left.strip() == ""                                        # and each call deleted its own


def test_problems_are_counted_past_the_cap_never_held(monkeypatch):
    # codex HIGH on L3 r3: problems were all materialized before the cap (refs x entries), and _links was uncapped.
    from levain.team import index as I
    c = I.Capped((f"p{i}" for i in range(10 ** 6)), limit=3)
    assert list.__len__(c) == 3 and c.more == 10 ** 6 - 3 and c.done()[-1] == f"and {10 ** 6 - 3} more problems"
    monkeypatch.setattr(I, "MAX_PROBLEMS", 2)
    entries = [{"id": f"x-{i}", "type": "decision", "supersedes": [f"gone-{i}"]} for i in range(5)]
    led = I.Ledger(entries, [], [], None, [])
    assert led._links[1][-1] == "and 3 more refused links"


def test_a_tree_the_leaf_cap_allows_can_always_be_pinned():
    # complement LOW on L3 r3: 20,000 leaves at ~450 bytes each overflowed the 8 MiB record cap, so a tree the leaf cap
    # allowed could never be pinned (every read refused). The leaf cap is derived from the record cap.
    from levain.team import entry as E
    from levain.team import transport as T
    n = T._MAX_LEDGER_LEAVES
    rels = [f"{i:064d}/{'f' * 16}.jsonl" for i in range(n)]
    pin = {"sha256": "f" * 64, "length": T._MAX_LEDGER_FILE}
    rec = T.Trust({r: pin for r in rels}, "f" * 64, {r: {"t": E.now_iso(), "first": False} for r in rels},
                  {r: pin for r in rels})
    assert len(rec.dump().encode("utf-8")) <= T._PINS_MAX_BYTES


def test_recovery_never_commits_through_a_link(two):
    # complement LOW on L3 r3: recovery read the worktree file through a symlink, with no size bound.
    tmp, ana, ben = two
    assert record_ruling(ben, "src/a.py", "mine") == 0
    gb = _gl(ben)
    f = next((gb.wt / "ledger" / "ben").glob("*.jsonl"))
    outside = tmp / "outside.txt"
    outside.write_bytes(f.read_bytes() + b'{"secret": "outside the repository"}\n')
    f.unlink()
    f.symlink_to(outside)
    assert record_ruling(ben, "src/b.py", "second") == 0
    own = git("cat-file", "blob", f"levain-ledger:{f.relative_to(gb.wt).as_posix()}", cwd=ben)
    assert "outside the repository" not in own and "second" in own


def test_a_rejoin_works_offline_from_the_clones_own_branch(two, capsys):
    # complement LOW on L3 r3: a re-join of a clone that has the branch needed the network.
    tmp, ana, ben = two
    git("remote", "set-url", "origin", str(tmp / "unreachable.git"), cwd=ben)
    capsys.readouterr()
    assert team("join", "--no-install", repo=ben) == 0
    assert "could not be reached" in capsys.readouterr().out


def test_a_join_that_accepts_the_refused_tip_clears_its_refusal_and_never_anothers(two):
    # complement LOW / codex HIGH on L3 r4: join deleted whatever quarantine existed, a concurrent fetch's included.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "x") == 0
    assert team("sync", repo=ben) == 0
    ledger(ben)
    _forge_last_line(_gl(ana), "FORGED")
    assert team("sync", repo=ben) != 0 and _gl(ben).incoming_refusal()
    refused = _gl(ben).trust_record().refused
    assert team("repin", repo=ben) == 0
    assert team("join", "--no-install", repo=ben) == 0               # the remote's tip IS the refused one: accepted
    assert _gl(ben).trust_record().refused is None and _gl(ben).accepted_tip() == refused


def test_an_unreadable_record_is_said_not_shown_as_no_devices(two):
    # glm LOW on L3 r3: devices() dropped the record's problem and returned [].
    tmp, ana, ben = two
    ledger(ben)
    _pins_file(ben).write_text("{not json")
    with pytest.raises(LedgerReadError):
        _gl(ben).devices()
    assert "REFUSED" in _ss(ben)                                     # session start says it (before any device line)


def test_a_tip_that_moves_while_it_is_read_is_read_again_never_served_unpinned(two, monkeypatch):
    # codex HIGH on L3 r4: a tip that moved before _accept was served (the old tip, unpinned) while the new tip held a
    # new ruling. A read of the tip retries, bounded, like a pin race.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    assert team("sync", repo=ben) == 0
    ledger(ben)
    assert record_ruling(ana, "src/b.py", "the new ruling") == 0
    gb = _gl(ben)
    assert gb.fetch_only(interval=0, timeout=30) is None
    newer = gb.accepted_tip()
    (gb.base / "history.json").unlink(missing_ok=True)
    real, moved = GitLedger.lock, []

    def lock(self, *a, **k):
        if k.get("name") == "pins.lock" and len(moved) == 1:          # the second take: _accept's
            git("update-ref", "refs/heads/levain-ledger", newer, cwd=ben)
        if k.get("name") == "pins.lock":
            moved.append(1)
        return real(self, *a, **k)
    monkeypatch.setattr(GitLedger, "lock", lock)
    led = gb.ledger()
    assert "the new ruling" in [e.get("words") for e in led.entries]


def test_a_capped_count_survives_build(monkeypatch):
    # glm MED + codex LOW + gemini LOW on L3 r4: a Capped summary passed through build() was re-capped ("and 1 more").
    from levain.team import index as I
    monkeypatch.setattr(I, "MAX_PROBLEMS", 2)
    tamper = I.Capped(f"r{i}" for i in range(7))
    problems = I.Capped(f"p{i}" for i in range(5))
    problems += I.Capped(f"x{i}" for i in range(4))
    led = I.build([], None, problems, tamper=tamper)
    assert led.tamper[-1] == "and 5 more reasons" and led.file_problems[-1] == "and 7 more problems"
    assert I.build([], None, problems).file_problems == led.file_problems     # build never changes its input


def test_repin_keeps_the_first_seen_notes_within_what_a_record_holds(monkeypatch):
    # codex MED on L3 r4: repin kept every historical first-seen note, so they outgrew the derived leaf cap.
    from levain.team import transport as T
    monkeypatch.setattr(T, "_MAX_LEDGER_LEAVES", 3)
    seen = {f"m/{i:016x}.jsonl": {"t": f"2026-10-0{i + 1}T00:00:00Z", "first": False} for i in range(6)}
    kept = T._prune_seen(dict(seen), {"m/0000000000000000.jsonl"})
    assert len(kept) == 3 and "m/0000000000000000.jsonl" in kept and "m/0000000000000005.jsonl" in kept


def test_record_says_when_its_push_was_not_recorded(two, monkeypatch, capsys):
    # codex HIGH on L3 r4: append() and init() dropped "pushed, but not recorded".
    tmp, ana, ben = two
    monkeypatch.setattr(GitLedger, "_sync", lambda self, **k: "pushed, but not recorded (x); run `levain team sync`")
    capsys.readouterr()
    assert record_ruling(ben, "src/a.py", "w") == 0
    assert "not recorded" in capsys.readouterr().err


def test_a_push_is_recorded_only_by_a_tip_that_holds_it(two, monkeypatch):
    # complement LOW on L3 r4: any accepted SHA from the follow-up fetch counted the push as recorded.
    tmp, ana, ben = two
    gb = _gl(ben)
    older = gb.accepted_tip()
    assert team("record", "decision", "--kind", "ruling", "--owner", "client:Dana", "--paths", "src/x.py", "--words",
                "w", "--no-push", repo=ben) == 0
    monkeypatch.setattr(GitLedger, "_record_pushed", lambda self, *a: False)
    real, calls = GitLedger._fetch_quarantined, []

    def stale_second(self, remote, timeout):
        calls.append(1)
        return real(self, remote, timeout) if len(calls) == 1 else older
    monkeypatch.setattr(GitLedger, "_fetch_quarantined", stale_second)
    assert gb.sync().startswith("pushed, but not recorded")


def test_an_offline_rejoin_of_a_branch_behind_its_fetch_is_not_refused(two, capsys):
    # codex MED on L3 r4: an offline re-join checked the clone's own remote pins strictly against its lagging branch.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "first") == 0
    assert team("sync", repo=ben) == 0
    ledger(ben)
    assert record_ruling(ana, "src/b.py", "second") == 0
    assert _gl(ben).fetch_only(interval=0, timeout=30) is None       # remote pins A+B; the branch holds A
    git("remote", "set-url", "origin", str(tmp / "unreachable.git"), cwd=ben)
    assert team("join", "--no-install", repo=ben) == 0


def test_a_stale_join_ref_from_a_crashed_call_is_never_used_and_is_swept(two):
    # codex HIGH + gemini LOW on L3 r4: an offline re-join read a stale refs/levain/join left by a crashed join.
    tmp, ana, ben = two
    gb = _gl(ben)
    old = gb.head()
    assert record_ruling(ana, "src/a.py", "x") == 0
    assert team("sync", repo=ben) == 0
    git("update-ref", "refs/levain/join/999999-dead", old, cwd=ben)       # a crashed join's ref (no such pid)
    git("remote", "set-url", "origin", str(tmp / "unreachable.git"), cwd=ben)
    before = gb.accepted_tip()
    assert team("join", "--no-install", repo=ben) == 0
    assert gb.accepted_tip() == before                                   # the stale commit was not accepted
    assert git("for-each-ref", "refs/levain/join/", cwd=ben).strip() == ""


def test_a_join_never_clears_a_refusal_of_another_tip(two, monkeypatch):
    # codex HIGH on L3 r4: a join that judged good tip X erased a refusal of tip Y a concurrent fetch had recorded.
    from levain.team import transport as T
    tmp, ana, ben = two
    gb = _gl(ben)
    other = gb.head()
    assert record_ruling(ana, "src/a.py", "x") == 0                      # the remote moves past `other`
    real = GitLedger._sweep_call_refs

    def fetch_refused_meanwhile(self):
        rec = self.trust_record()
        T._atomic_write(self.base / "pins.json", T.dataclasses.replace(rec, refused=other).dump())
        return real(self)
    monkeypatch.setattr(GitLedger, "_sweep_call_refs", fetch_refused_meanwhile)
    monkeypatch.setattr(GitLedger, "_sync", lambda self, **k: "fetched")   # look at what the join itself wrote
    assert team("join", "--no-install", repo=ben) == 0
    assert gb.trust_record().refused == other


def test_a_deny_exits_2_and_an_allow_exits_0_with_one_document(two):
    # Head ruling on L3 r4: the deny decision is carried by the exit status too (Claude Code: exit 2 blocks a
    # PreToolUse whatever stdout holds), so a torn document can never read as an allow.
    tmp, ana, ben = two

    def run(rel):
        return subprocess.run([sys.executable, "-P", "-m", "levain.team.hook", "pretooluse"],
                              input=json.dumps(_edit_payload(ben, ben / rel)), capture_output=True, text=True,
                              timeout=60)
    denied = run("src/settlement.py")                     # the pack rule denies the first edit
    assert denied.returncode == 2 and "write_batch" in denied.stderr
    assert _one_doc(denied.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    allowed = run("src/billing.py")
    assert allowed.returncode == 0 and denied.stdout.count("hookSpecificOutput") == 1
    assert _one_doc(allowed.stdout).get("hookSpecificOutput", {}).get("permissionDecision") != "deny"


def test_a_refusal_outlives_the_fetchs_ref_and_a_cached_read_still_refuses(two):
    # Head ruling (record): the refusal lives in pins.json, not in any ref; a per-call ref is gone after its call.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "x") == 0
    assert team("sync", repo=ben) == 0
    ledger(ben)
    ledger(ben)                                                      # the cache is warm
    _forge_last_line(_gl(ana), "FORGED")
    assert team("sync", repo=ben) != 0
    assert git("for-each-ref", "refs/levain/incoming/", cwd=ben).strip() == ""
    assert _gl(ben).trust_record().refused
    assert ledger(ben).tamper                                        # a read that would hit the cache refuses


def test_a_repin_then_a_read_accepts_the_refused_tip_once_and_clears_it(two, monkeypatch):
    # codex HIGH + complement HIGH on L3 r4: repin -> read recursed until RecursionError. The read now accepts the
    # refused tip in one read-judge-write.
    tmp, ana, ben = two
    assert record_ruling(ana, "src/a.py", "x") == 0
    assert team("sync", repo=ben) == 0
    ledger(ben)
    _forge_last_line(_gl(ana), "FORGED")
    assert team("sync", repo=ben) != 0
    refused = _gl(ben).trust_record().refused
    assert team("repin", repo=ben) == 0
    calls = []
    real = GitLedger._refusal_locked

    def count(self):
        calls.append(1)
        return real(self)
    monkeypatch.setattr(GitLedger, "_refusal_locked", count)
    _gl(ben).ledger()                    # (its branch still holds the old bytes until a sync replays: it says so)
    rec = _gl(ben).trust_record()
    assert rec.refused is None and rec.accepted == refused and len(calls) <= 2
    monkeypatch.setattr(GitLedger, "_refusal_locked", real)
    assert team("sync", repo=ben) == 0 and not ledger(ben).tamper
