"""Code L3 round 16 on init's publish (9d28da7..e23e80a): each test is a RUN that failed on e23e80a (a lost race with
the origin unreachable, a descendant tip, a local-only checkout on another branch), built into a test.

P1 a lost race whose tip probe FAILS is never read as absent, so --pack seeds nothing · P2 a tip that descends from
our genesis is ours, never a lost race · P3 a local-only clone's re-attach advice works when the checkout is on
another branch.
"""
# ruff: noqa: F811  (the keys fixture is imported from tests.test_team_tenure)
import os
import re
import shlex
import subprocess

from levain.team import transport as Tr
from tests.test_team_tenure import clone, gl, keys, sh, team  # noqa: F401

LEDGER = "refs/heads/levain-team-ledger"


def _origin(tmp_path):
    sh("git", "init", "-q", "--bare", "--initial-branch=main", "origin.git", cwd=tmp_path)
    ana = clone(tmp_path, "ana", "ana@ex.com")
    sh("git", "commit", "-q", "--allow-empty", "-m", "init", cwd=ana)
    sh("git", "push", "-q", "origin", "HEAD:main", cwd=ana)
    return ana, clone(tmp_path, "ben", "ben@ex.com")


def _init(repo, keys, owner, *extra):
    return team("init", "--project", "demo", "--owner", owner, "--member", f"{owner}={owner}@ex.com", *extra,
                "--signing-key", str(keys[owner]), "--no-install", repo=repo)


def test_p1_a_failed_tip_probe_after_a_lost_race_seeds_nothing(tmp_path, keys, monkeypatch):
    """codex r16 HIGH (RUN 3/3 on e23e80a): ben published first, then the origin went away, so ana's push AND its
    tip probe failed; the failed probe read as "absent" and --pack seeded a ledger that had lost."""
    ana, ben = _origin(tmp_path)
    origin = tmp_path / "origin.git"
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "judgment.toml").write_text('[pack]\nname = "p"\nversion = "1"\n\n[[rule]]\nid = "r1"\n'
                                        'paths = ["src/a.py"]\nkind = "ruling"\nowner = "ana"\nwords = "small"\n')
    real = Tr.GitLedger._genesis_commit
    fired = []

    def genesis(self, *a, **k):
        c = real(self, *a, **k)
        if not fired:
            fired.append(1)
            assert _init(ben, keys, "ben") == 0          # ben wins the race
            os.rename(origin, tmp_path / "gone.git")      # then the origin is unreachable
        return c
    monkeypatch.setattr(Tr.GitLedger, "_genesis_commit", genesis)
    try:
        assert _init(ana, keys, "ana", "--pack", str(pack)) == 2
    finally:
        os.rename(tmp_path / "gone.git", origin)
    assert sh("git", "rev-list", "--count", LEDGER, cwd=ana).strip() == "1", "a ledger of unknown fate was seeded"


def test_p2_a_tip_that_descends_from_our_genesis_is_published(tmp_path, keys, monkeypatch):
    """codex r16 MED + complement 2 (RUN on e23e80a): ana's push landed but reported failure, and ben joined and
    appended before ana's probe; the tip was not ana's genesis, so the owner was told to DELETE the published ledger."""
    ana, ben = _origin(tmp_path)
    real = Tr.git

    def git(args, cwd, *a, **k):
        if args[:2] == ["push", "--porcelain"] and os.path.samefile(cwd, ana):
            real(args, cwd, *a, **k)
            assert team("join", "--signing-key", str(keys["ben"]), "--no-install", repo=ben) == 0
            assert team("record", "decision", "--kind", "ruling", "--owner", "lead", "--paths", "src/b.py",
                        "--words", "ben", repo=ben) == 0
            return subprocess.CompletedProcess(args, 1, "", "fatal: the remote end hung up unexpectedly\n")
        return real(args, cwd, *a, **k)
    monkeypatch.setattr(Tr, "git", git)
    assert _init(ana, keys, "ana", "--member", f"ben=ben@ex.com={keys['ben']}") == 0
    assert gl(ana).joined()


def test_p3_local_only_reattach_advice_works_with_the_checkout_on_another_branch(tmp_path, keys, capsys):
    """codex r16 MED + complement 1 (RUN on e23e80a): the advice was prune + add, and `worktree add` exits 128 while
    the checkout exists on another branch, so following it left the clone broken."""
    repo = tmp_path / "solo"
    sh("git", "init", "-q", str(repo))
    sh("git", "-c", "user.email=a@x", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "i", cwd=repo)
    assert _init(repo, keys, "ana") == 0
    sh("git", "checkout", "-q", "-b", "elsewhere", cwd=gl(repo).wt)
    capsys.readouterr()
    assert _init(repo, keys, "ana") == 2
    advice = capsys.readouterr().err
    cmds = re.findall(r"`(git -C [^`]*)`", advice)
    assert cmds, advice
    for c in cmds:
        argv = shlex.split(c)       # git -C <top> worktree <verb> ...: remove is "if it is listed", the rest must work
        subprocess.run(argv, capture_output=True, check=argv[4] != "remove")
    assert gl(repo).joined()
