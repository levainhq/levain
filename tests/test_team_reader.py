"""The team reader returns a named state for every ledger a clone can hold, and never raises or draws an error as an
empty team. Real git ledgers (the fixtures of test_team_git), plus a ledger whose snapshot fails, for the state a healthy clone cannot reach."""
from __future__ import annotations

from pathlib import Path

from levain.team.reader import read_team
from levain.team.transport import LEGACY_BRANCH, GitLedger, Repo
from tests.test_team_git import BRANCH, _keys, git, record_ruling, sign_as, signed_commit, team, two  # noqa: F401,F811  (fixtures: pytest finds them by name in this module)


def _gl(repo: Path) -> GitLedger:
    return GitLedger(Repo.discover(repo))


def test_a_clone_with_no_ledger_is_no_team_not_an_error(tmp_path):
    git("init", "-q", "--initial-branch=main", "solo", cwd=tmp_path)
    r = read_team(_gl(tmp_path / "solo"))
    assert (r.state, r.model) == ("no_team", None) and r.why


def test_a_joined_clone_reads_ok_and_shows_the_teammates_ruling(two):
    tmp, ana, ben = two
    assert record_ruling(ana) == 0 and team("sync", repo=ben) == 0
    r = read_team(_gl(ben))
    assert r.state == "ok" and r.why == "", r
    assert "Per-line rounding stays." in str(r.model)
    assert r.model is not None and r.model["in_force"]


def test_a_ledger_that_cannot_be_judged_is_named_not_empty(two, monkeypatch):
    tmp, ana, ben = two
    gl = _gl(ben)
    secret = "/Users/someone/private/path.sqlite"

    def boom(*a, **k):
        raise OSError(f"[Errno 13] Permission denied: {secret}")

    monkeypatch.setattr(GitLedger, "derivation", boom)
    r = read_team(gl)
    assert r.state == "unreadable" and r.model is None
    assert secret not in r.why and r.why                       # the screen text never carries the OS error
    assert secret in r.detail                                  # a log does


def test_an_unaccepted_merge_reads_judged_partial_with_the_judged_part_and_a_reason(two):
    tmp, ana, ben = two
    assert record_ruling(ana) == 0
    gl = _gl(ana)
    git("checkout", "-q", "-b", "side", cwd=gl.wt)
    (gl.wt / "ledger" / "x.txt").write_text("x")
    git("add", ".", cwd=gl.wt)
    signed_commit(gl.wt, "side", "ana")
    git("checkout", "-q", BRANCH, cwd=gl.wt)
    (gl.wt / "ledger" / "y.txt").write_text("y")
    git("add", ".", cwd=gl.wt)
    signed_commit(gl.wt, "main", "ana")
    git(*sign_as("ana"), "merge", "-q", "--no-edit", "side", cwd=gl.wt)
    r = read_team(_gl(ana))
    assert r.state == "judged_partial", r
    assert r.model is not None and "Per-line rounding stays." in str(r.model)     # what WAS judged is still shown
    assert "could not be fully judged" in r.why and "not accepted" in r.detail     # and the screen says it is held back
    assert r.why.endswith(".") and "1 newer lines" not in r.why


def test_a_legacy_only_clone_is_unreadable_with_the_legacy_message_not_no_team(tmp_path):
    git("init", "-q", "--initial-branch=main", "old", cwd=tmp_path)
    old = tmp_path / "old"
    git("config", "user.email", "o@x.io", cwd=old)
    git("config", "user.name", "o", cwd=old)
    (old / "a.txt").write_text("a")
    git("add", ".", cwd=old)
    git("commit", "-qm", "init", cwd=old)
    git("branch", LEGACY_BRANCH, cwd=old)
    r = read_team(_gl(old))
    assert r.state == "unreadable" and r.model is None, r
    assert "predates strict mode" in r.why                      # a TeamError's own message reaches the screen


def test_one_read_uses_one_derivation_for_the_team_the_judgement_and_the_handle(two, monkeypatch):
    tmp, ana, ben = two
    gl = _gl(ben)
    calls = []
    real = GitLedger.derivation

    def counting(self, rev=None):
        calls.append(rev)
        return real(self, rev)

    monkeypatch.setattr(GitLedger, "derivation", counting)
    r = read_team(gl)
    # one derivation for the whole read: the team, the judgement, the handle and the canon text all come from it. The
    # canon read re-derived at the current trust state before, so an accept-merge between the two reads mixed judgements
    assert r.state == "ok" and calls == [None], calls
