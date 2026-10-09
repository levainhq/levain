"""Code L3 round 9 on the op lock (1f303ec..740d624): each test is a run that failed on 740d624.

K1 pack.seed reads the pack file under the op · K2 init never deletes a ledger ref it did not create.
"""
# ruff: noqa: F811  (the keys/two fixtures are imported from tests.test_team_tenure)
import threading

import pytest

from levain.team import pack as P
from levain.team import roles as R
from levain.team import transport as Tr
from levain.team.transport import WARNINGS, TeamError
from tests.test_team_tenure import gl, keys, sh, two  # noqa: F401


class _Stop(Exception):
    pass


def test_k1_pack_seed_reads_the_pack_file_under_the_op(two, tmp_path, monkeypatch):
    """codex r9 1 (HIGH): seed read the pack file BEFORE waiting for the op, so a v2 written while it waited was
    planned with the stale v1 judgment, superseding v2. Now the file is read once the op is held."""
    _, ana, _ = two
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    jf = pack_dir / R.JUDGMENT_FILE
    jf.write_text("v1")
    read: list[str] = []

    def load(path):
        read.append(path.read_text())
        raise _Stop
    monkeypatch.setattr(R, "load_judgment", load)
    got, release = threading.Event(), threading.Event()

    def holder():
        with gl(ana).op(timeout=5):
            got.set()
            release.wait(10)
    t = threading.Thread(target=holder)
    t.start()
    got.wait(5)
    out: list[BaseException] = []

    def seeder():
        try:
            P.seed(gl(ana), pack_dir, push=False)
        except BaseException as exc:  # noqa: BLE001 - collected for the assertion below
            out.append(exc)
    s = threading.Thread(target=seeder)
    s.start()
    s.join(0.3)          # the seeder is waiting on the op (or, before the fix, has already read v1)
    jf.write_text("v2")
    release.set()
    t.join()
    s.join(10)
    assert out and isinstance(out[0], _Stop), out
    assert read == ["v2"], f"seed planned from the pack file as it was before the op: {read}"


def test_k2_init_never_deletes_a_ref_it_did_not_create(tmp_path, keys, monkeypatch):
    """codex r9 2 (HIGH): when another writer created the ledger ref at the same genesis first, init's create failed and
    its roll-back (keyed on 'genesis computed', not 'ref created') deleted the other writer's ref. The roll-back is
    deleted (join's frame): a failed create deletes nothing and saves nothing."""
    WARNINGS.clear()
    a = tmp_path / "a"
    a.mkdir()
    sh("git", "init", "-q", "--initial-branch=main", cwd=a)
    sh("git", "config", "user.email", "ana@ex.com", cwd=a)
    sh("git", "config", "user.name", "ana", cwd=a)
    (a / "f").write_text("x")
    sh("git", "add", "f", cwd=a)
    sh("git", "commit", "-qm", "i", cwd=a)
    real = Tr.git

    def racing_git(args, cwd, **kw):
        if args[:1] == ["update-ref"] and len(args) == 4 and args[3] == "":
            real(["update-ref", args[1], args[2]], cwd)      # the other writer wins the create, at the same genesis
        return real(args, cwd, **kw)
    monkeypatch.setattr(Tr, "git", racing_git)
    team_ = R.Team(project="demo", owner="ana", members={"ana": "ana@ex.com"})
    with pytest.raises(TeamError):
        gl(a).init(team_, signing_key=str(keys["ana"]), push=False)
    monkeypatch.setattr(Tr, "git", real)
    ref = sh("git", "rev-parse", "-q", "--verify", "refs/heads/" + Tr.BRANCH, cwd=a, check=False).strip()
    assert ref, "init deleted a ledger ref another writer had created"
    assert not gl(a).pinned_root and "signing_key" not in gl(a).state(), gl(a).state()
    # no resume (r13: deleted, spore-813): a retry refuses and names the exact cleanup
    with pytest.raises(TeamError, match="git branch -D"):
        gl(a).init(team_, signing_key=str(keys["ana"]), push=False)
