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
    commit = git("commit-tree", tree, "-m", "orphan", cwd=raw).strip()
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
