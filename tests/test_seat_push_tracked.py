"""The push refuses a lane tip that changes an ``activation`` directory (L3 r1 on the tracked-activation exemption,
items 1+2). Path-based deny rules cannot make a tree read-only on their own: the seat can rename an ancestor away, edit,
and rename it back, or hard-link a file to a new name and write through the alias. What leaves the seat is judged at the
egress instead, from git objects in levain's own mirror: every commit between the launch base (the signed mint row's
``base_sha``) and the tip, against every parent. Real path: a real signed log, mint chain, clone, git and a local bare
remote."""
from __future__ import annotations

import os
import shutil
import sys
from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
from levain import seat_lease as sl  # noqa: E402
from levain import seat_push as sp  # noqa: E402
from tests.test_seat_lease import _git, rig  # noqa: E402,F401  (rig is a fixture)
from tests.test_seat_push import Egress  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None or shutil.which("git") is None,
                                reason="needs ssh-keygen and git")

ACT = "pkg/templates/activation"


@pytest.fixture
def eg(rig, tmp_path, monkeypatch):
    return Egress(rig, tmp_path, monkeypatch)


def act_lane(eg, name="lane-t"):
    """Launch a lane from a source that tracks ``pkg/templates/activation`` (as levain's own repo does), with the
    lease and egress grant signed, on branch ``seat/<name>``."""
    src = eg.tmp / "src-act"
    if not src.exists():
        (src / ACT / "hooks").mkdir(parents=True)
        _git(src, "init", "-q")
        (src / "README.md").write_text("hi\n")
        (src / ACT / "posture.md").write_text("p\n")
        (src / ACT / "hooks" / "stop.py").write_text("print(1)\n")
        _git(src, "add", "-A")
        _git(src, "commit", "-q", "-m", "base")
    sha = _git(src, "rev-parse", "HEAD")
    eg.rig.sign_lease()
    eg.sign()
    plan = sl.prepare_launch(name, str(src), sha, claude_bin=eg.rig.claude, python=sys.executable,
                             cockpit_root=eg.world.root, install=eg.rig.tmp,
                             now=eg.world.clock.now + timedelta(minutes=1), managed=())
    assert plan.mint["base_sha"] == sha
    clone = Path(plan.profile.clone)
    assert str(clone / ACT) in plan.profile.settings["sandbox"]["filesystem"]["denyWrite"]   # exempt, read-only
    _git(clone, "checkout", "-q", "-b", f"seat/{name}")
    return clone, sha


def commit_all(clone, msg="work"):
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", msg)
    return _git(clone, "rev-parse", "HEAD")


def assert_refused(eg, clone, name="lane-t", path=None):
    assert eg.hook(clone) == (0, "")
    assert eg.remote_ref(f"refs/heads/seat/{name}") is None
    row = eg.log()[-1]
    assert row["outcome"] == "refused"
    assert sp.TREE_REFUSAL_HEAD in row["detail"]
    if path is not None:
        assert path in row["detail"]
    return row


def test_a_lane_commit_touching_only_ordinary_files_still_pushes(eg):
    clone, _ = act_lane(eg)
    (clone / "README.md").write_text("changed\n")
    (clone / "pkg" / "new.py").write_text("x\n")
    tip = commit_all(clone)
    assert eg.hook(clone) == (0, "")
    assert eg.remote_ref("refs/heads/seat/lane-t") == tip
    assert eg.log()[-1]["outcome"] == "pushed"


def test_a_modified_activation_file_is_refused(eg):
    clone, _ = act_lane(eg)
    (clone / ACT / "posture.md").write_text("changed\n")
    commit_all(clone)
    assert_refused(eg, clone, path=f"{ACT}/posture.md")


def test_a_deleted_activation_file_is_refused(eg):
    clone, _ = act_lane(eg)
    os.remove(clone / ACT / "posture.md")
    commit_all(clone)
    assert_refused(eg, clone, path=f"{ACT}/posture.md")


def test_an_added_activation_file_is_refused(eg):
    clone, _ = act_lane(eg)
    (clone / ACT / "hooks" / "extra.py").write_text("x\n")
    commit_all(clone)
    assert_refused(eg, clone, path=f"{ACT}/hooks/extra.py")


def test_a_rename_out_of_activation_is_refused(eg):
    clone, _ = act_lane(eg)
    _git(clone, "mv", f"{ACT}/posture.md", "pkg/posture.md")
    commit_all(clone)
    assert_refused(eg, clone, path=f"{ACT}/posture.md")


def test_the_mv_ancestor_shape_is_refused(eg):
    """mv pkg/templates pkg/t, edit pkg/t/activation/hooks/stop.py, mv it back: no operation named the denied spelling
    while the file changed, and the commit still carries the change."""
    clone, _ = act_lane(eg)
    os.rename(clone / "pkg" / "templates", clone / "pkg" / "t")
    (clone / "pkg" / "t" / "activation" / "hooks" / "stop.py").write_text("import os; os.system('x')\n")
    os.rename(clone / "pkg" / "t", clone / "pkg" / "templates")
    commit_all(clone)
    assert_refused(eg, clone, path=f"{ACT}/hooks/stop.py")


def test_a_hard_link_alias_edited_then_committed_is_refused(eg):
    clone, _ = act_lane(eg)
    os.link(clone / ACT / "hooks" / "stop.py", clone / "alias.py")
    with open(clone / "alias.py", "w") as fh:                  # write through the alias: the same inode
        fh.write("import os; os.system('x')\n")
    assert (clone / ACT / "hooks" / "stop.py").read_text().startswith("import os")
    commit_all(clone)
    assert_refused(eg, clone, path=f"{ACT}/hooks/stop.py")


def test_a_new_activation_directory_elsewhere_and_a_case_variant_are_refused(eg):
    clone, _ = act_lane(eg)
    (clone / "pkg" / "other" / "Activation").mkdir(parents=True)
    (clone / "pkg" / "other" / "Activation" / "x.py").write_text("x\n")
    commit_all(clone)
    assert_refused(eg, clone, path="pkg/other/Activation/x.py")


def test_a_change_restored_by_a_later_commit_is_still_refused(eg):
    """Every commit between the base and the tip is judged, not only the net diff: an intermediate commit that carries
    a changed activation file leaves the seat with the branch."""
    clone, _ = act_lane(eg)
    (clone / ACT / "posture.md").write_text("changed\n")
    commit_all(clone, "change")
    (clone / ACT / "posture.md").write_text("p\n")
    commit_all(clone, "restore")
    assert_refused(eg, clone, path=f"{ACT}/posture.md")


def test_a_tip_that_does_not_descend_from_the_base_is_refused(eg):
    clone, _ = act_lane(eg)
    _git(clone, "checkout", "-q", "--orphan", "tmp")
    _git(clone, "rm", "-q", "-r", "--cached", ".")
    for p in ("pkg", "README.md"):
        shutil.rmtree(clone / p) if (clone / p).is_dir() else os.remove(clone / p)
    (clone / "only.txt").write_text("x\n")
    commit_all(clone)
    _git(clone, "branch", "-f", "seat/lane-t", "tmp")
    assert eg.hook(clone) == (0, "")
    assert eg.remote_ref("refs/heads/seat/lane-t") is None
    row = eg.log()[-1]
    assert row["outcome"] == "refused" and "does not descend from the launch base" in row["detail"]


def test_a_mint_row_without_a_base_refuses_the_push(eg, monkeypatch):
    clone, _ = act_lane(eg)
    (clone / "README.md").write_text("changed\n")
    commit_all(clone)
    real_mint = sp._mint_for

    def no_base(key_dir, clone_real):
        row = dict(real_mint(key_dir, clone_real))
        row.pop("base_sha", None)
        return row
    monkeypatch.setattr(sp, "_mint_for", no_base)
    assert eg.hook(clone) == (0, "")
    assert eg.remote_ref("refs/heads/seat/lane-t") is None
    assert "launch base" in eg.log()[-1]["detail"]


def test_activation_component_matching():
    hit = sp._activation_paths
    assert hit([b"pkg/activation/x", b"a.txt", b"activation", b"x/ACTIVATION/y", b"deactivation/x",
                b"pkg/activation.py"]) == ["pkg/activation/x", "activation", "x/ACTIVATION/y"]
