"""Code L3 round 6 on the strict signed ledgers (fac2942..1349c09): each test is a run that failed on 1349c09.

r6 found the per-step lock + snapshot-CAS construct failing at a new site each round (a value read outside the lock
that guards its writers). The seat replaced it with git's frame: ONE per-clone op lock held for the whole of every
operation that changes the pin, the trust state or the ledger.

H1 no other operation runs while a sync re-lands · H2 init proves its key before it saves anything · H3 regenesis
--from is judged by membership in the walk this clone judged.
"""
# ruff: noqa: F811  (the keys/two fixtures are imported from tests.test_team_tenure)
import threading

from levain.team.transport import WARNINGS, GitLedger, TeamBusy
from tests.test_team_tenure import clone, gl, keys, sh, team, two  # noqa: F401
from tests.test_team_tenure_rows_a import ruling_np


def test_h1_no_other_operation_runs_while_a_sync_re_lands(two, monkeypatch):
    """codex r6 1 (HIGH): `_reland` decided from the counted state outside every lock and then applied, so an owner's
    change landing between overwrote it. Now another thread (or process) cannot take the clone's op while a sync is
    re-landing: it waits, and here gives up as busy."""
    tmp, ana, ben = two
    seen = []

    def other():
        try:
            with gl(ben).op(timeout=0.2):
                seen.append("acquired")
        except TeamBusy:
            seen.append("busy")
    real = GitLedger._reland

    def reland(self):
        t = threading.Thread(target=other)
        t.start()
        t.join()
        return real(self)
    monkeypatch.setattr(GitLedger, "_reland", reland)
    gl(ben).sync(push=False)
    assert seen == ["busy"], seen


def test_h2_init_proves_its_key_before_it_saves_anything(tmp_path, keys, capsys):
    """codex r6 2/6, complement 5: `init --signing-key /missing/key.pub` saved the path first, then failed reading it
    (join's r5 fix had not reached init)."""
    WARNINGS.clear()
    sh("git", "init", "-q", "--bare", "--initial-branch=main", "origin.git", cwd=tmp_path)
    a = clone(tmp_path, "a", "ana@ex.com")
    (a / "f").write_text("x")
    sh("git", "add", "f", cwd=a)
    sh("git", "commit", "-qm", "i", cwd=a)
    missing = tmp_path / "nope" / "key.pub"
    assert team("init", "--project", "demo", "--owner", "ana", "--member", "ana=ana@ex.com", "--signing-key",
                str(missing), "--no-install", repo=a) == 2
    assert "signing_key" not in gl(a).state(), gl(a).state()


def test_h3_regenesis_from_a_local_only_ledgers_newest_line(tmp_path, keys, capsys):
    """codex r6 4: `--from` was judged as a git ANCESTOR of the anchor or the last counted team change, so on a
    local-only ledger (no anchor) the newest ledger line, after the last team change, was refused. Now it is judged by
    membership in the walk this clone followed and counted."""
    WARNINGS.clear()
    a = tmp_path / "a"
    a.mkdir()
    sh("git", "init", "-q", "--initial-branch=main", cwd=a)
    sh("git", "config", "user.email", "ana@ex.com", cwd=a)
    sh("git", "config", "user.name", "ana", cwd=a)
    (a / "src").mkdir()
    (a / "src" / "a.py").write_text("x = 1\n")
    sh("git", "add", ".", cwd=a)
    sh("git", "commit", "-qm", "init", cwd=a)
    assert team("init", "--project", "demo", "--owner", "ana", "--member", "ana=ana@ex.com", "--signing-key",
                str(keys["ana"]), "--no-install", repo=a) == 0
    assert ruling_np(a, "src/a.py", "ana: a local line") == 0
    head = gl(a).head()
    assert head != gl(a).derivation().counted_head, "precondition: the newest line is after the last team change"
    capsys.readouterr()
    rc = team("regenesis", "--from", head, "--owner", "ana", "--member", "ana=ana@ex.com", "--no-push", repo=a)
    assert rc == 0, capsys.readouterr().err
