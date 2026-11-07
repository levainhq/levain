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
    """A tree refusal is recorded AND told once on the first Stop (L3 r2 #1): exit 2 with the fixed words and the first
    path; the next Stop for the same tip is silent and records nothing new."""
    code, err = eg.hook(clone)
    assert code == 2 and sp.TREE_REFUSAL_HEAD in err and "Drop the commits" in err
    assert eg.remote_ref(f"refs/heads/seat/{name}") is None
    rows = eg.log()
    row = rows[-1]
    assert row["outcome"] == "refused"
    assert sp.TREE_REFUSAL_HEAD in row["detail"]
    if path is not None:
        assert path in row["detail"] and path in err
    assert eg.hook(clone) == (0, "")                     # told once: remembered, never loops
    assert len(eg.log()) == len(rows)
    return row


def test_a_tree_refusal_is_never_told_on_session_end_or_a_second_stop(eg):
    """SessionEnd has no model to tell, and a Stop with stop_hook_active must not loop: both record and exit 0, and
    the refusal nobody heard is told at the next first Stop. Reverting the ``event != "Stop" or active`` guard in
    ``_tell_tree`` turns this red."""
    clone, _ = act_lane(eg)
    (clone / ACT / "posture.md").write_text("changed\n")
    commit_all(clone)
    assert eg.hook(clone, event="SessionEnd") == (0, "")
    assert eg.log()[-1]["outcome"] == "refused"
    assert eg.hook(clone, stdin={"stop_hook_active": True}) == (0, "")
    assert eg.log()[-1]["outcome"] == "refused"
    code, err = eg.hook(clone)
    assert code == 2 and f"{ACT}/posture.md" in err


def test_dropping_the_commits_after_the_tell_pushes(eg):
    clone, base = act_lane(eg)
    (clone / ACT / "posture.md").write_text("changed\n")
    commit_all(clone)
    assert eg.hook(clone)[0] == 2
    _git(clone, "reset", "-q", "--hard", base)
    (clone / "README.md").write_text("ok\n")
    tip = commit_all(clone)
    assert eg.hook(clone) == (0, "")
    assert eg.remote_ref("refs/heads/seat/lane-t") == tip


def test_other_push_refusals_stay_silent(eg):
    """Only the activation refusal is told: a branch that does not descend from the base is recorded, exit 0."""
    clone, _ = act_lane(eg)
    _git(clone, "checkout", "-q", "--orphan", "tmp2")
    (clone / "only2.txt").write_text("x\n")
    commit_all(clone)
    _git(clone, "branch", "-f", "seat/lane-t", "tmp2")
    assert eg.hook(clone) == (0, "")


def test_too_many_changed_paths_refuses_with_fixed_words(eg, monkeypatch):
    """The diff-tree reads are streamed under a path cap; over it, the fixed words and nothing pushed. Reverting the
    ``tally["paths"] > _TREE_MAX_PATHS`` check in ``_mirror_paths`` turns this red (the lane pushes)."""
    clone, _ = act_lane(eg)
    for i in range(3):
        (clone / f"f{i}.txt").write_text("x\n")
    commit_all(clone)
    monkeypatch.setattr(sp, "_TREE_MAX_PATHS", 2)
    assert eg.hook(clone) == (0, "")
    assert eg.remote_ref("refs/heads/seat/lane-t") is None
    assert "the lane branch changes too many paths to judge" in eg.log()[-1]["detail"]


def test_too_many_changed_path_bytes_refuses_with_fixed_words(eg, monkeypatch):
    """The byte cap, the same words. Reverting the ``tally["bytes"] > _TREE_MAX_BYTES`` check turns this red."""
    clone, _ = act_lane(eg)
    (clone / ("long-name-" * 8 + ".txt")).write_text("x\n")
    commit_all(clone)
    monkeypatch.setattr(sp, "_TREE_MAX_BYTES", 40)
    assert eg.hook(clone) == (0, "")
    assert eg.remote_ref("refs/heads/seat/lane-t") is None
    assert "the lane branch changes too many paths to judge" in eg.log()[-1]["detail"]


def test_a_judge_past_its_deadline_refuses_and_still_records(eg, monkeypatch):
    """ONE deadline for the whole judge: with no budget left the check refuses in fixed words, nothing is pushed, and
    seat_egress.jsonl still gets the line. Reverting ``_left``'s expiry raise turns this red."""
    clone, _ = act_lane(eg)
    (clone / "README.md").write_text("changed\n")
    commit_all(clone)
    monkeypatch.setattr(sp, "_JUDGE_BUDGET", 0)
    assert eg.hook(clone) == (0, "")
    assert eg.remote_ref("refs/heads/seat/lane-t") is None
    assert "could not be judged in time" in eg.log()[-1]["detail"]


def test_a_git_call_that_times_out_refuses_as_late(eg, monkeypatch):
    """A git call that hits the shared deadline (TimeoutExpired) is the same fixed-words refusal, never a traceback."""
    import subprocess
    clone, _ = act_lane(eg)
    (clone / "README.md").write_text("changed\n")
    commit_all(clone)
    real_run = subprocess.run

    def slow(argv, *a, **k):
        if "merge-base" in argv:
            raise subprocess.TimeoutExpired(argv, k.get("timeout"))
        return real_run(argv, *a, **k)
    monkeypatch.setattr(subprocess, "run", slow)
    assert eg.hook(clone) == (0, "")
    assert eg.remote_ref("refs/heads/seat/lane-t") is None
    assert "could not be judged in time" in eg.log()[-1]["detail"]


def test_the_hook_deadline_bounds_the_judge(eg, monkeypatch):
    """The judge stops at the hook's own deadline when that comes first (SessionEnd's ceiling minus the reserve)."""
    clone, _ = act_lane(eg)
    (clone / "README.md").write_text("changed\n")
    commit_all(clone)
    monkeypatch.setattr(sp, "_HOOK_RESERVE", sp.SESSION_END_TIMEOUT)    # SessionEnd leaves the judge nothing
    assert eg.hook(clone, event="SessionEnd") == (0, "")
    assert eg.remote_ref("refs/heads/seat/lane-t") is None
    assert "could not be judged in time" in eg.log()[-1]["detail"]


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


def test_the_streamed_read_honours_the_shared_deadline(eg):
    """The streamed diff-tree read checks the one deadline itself (a select with no time left would poll, never stop).
    Reverting ``_left``'s expiry raise turns this red: the read then finishes past its deadline."""
    import time
    clone, base = act_lane(eg)
    (clone / "README.md").write_text("changed\n")
    tip = commit_all(clone)
    tally = {"paths": 0, "bytes": 0, "hits": set(), "base": base[:12]}
    args = ["diff-tree", "-r", "-z", "--no-renames", "--name-only", base, tip]
    assert sp._mirror_paths(clone / ".git", args, deadline=time.monotonic() + 30, stdin=b"", tally=tally) is None
    assert tally["paths"] == 1
    with pytest.raises(sp._Late):
        sp._mirror_paths(clone / ".git", args, deadline=time.monotonic() - 1, stdin=b"", tally=dict(tally, hits=set()))
