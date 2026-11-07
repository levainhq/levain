"""A protected-name directory git tracks at the launch base (Phill 11-07, np-4f771671 (a)): levain's own repo ships
``levain/templates/activation/``, so a seat on levain refused at launch. Exempt only an ``activation`` directory below
the clone's top level whose working tree is exactly the base's tree, and launch it read-only. Unit tests on real git
repos; nothing is launched."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from levain import seat_profile as sp

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

ACT = "pkg/templates/activation"
CODEX_ACT = "pkg/templates/adapters/codex/activation"


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    tmp = Path(os.path.realpath(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp))
    base = tmp / sp.CANARY_DIRNAME / "n1"
    lane = base / "lane"
    lane.mkdir(parents=True)
    (base / "ret").mkdir()
    keys = tmp / "keys"
    keys.mkdir()
    claude = tmp / "bin" / "claude"
    claude.parent.mkdir()
    claude.write_text("#!/bin/sh\n")
    return base, keys, claude


def commit(lane: Path, files: dict[str, str], *, exe: tuple[str, ...] = (), links: dict[str, str] | None = None) -> str:
    """Make ``lane`` a repo whose single commit tracks ``files`` (and ``links`` as symlinks); return the sha."""
    if not (lane / ".git" / "HEAD").exists():
        _git(lane, "init", "-q", "--template=")
    for rel, text in files.items():
        (lane / rel).parent.mkdir(parents=True, exist_ok=True)
        (lane / rel).write_text(text)
        if rel in exe:
            (lane / rel).chmod(0o755)
    for rel, target in (links or {}).items():
        (lane / rel).parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, lane / rel)
    _git(lane, "add", "-f", "-A")
    _git(lane, "commit", "-q", "-m", "base")
    return _git(lane, "rev-parse", "HEAD")


def build(rig, **kw):
    base, keys, claude = rig
    args = dict(claude_bin=str(claude), python=sys.executable, lease=None, key_dir=keys, canary_root=base,
                managed=[], environ={**os.environ})
    args.update(kw)
    return sp.build_seat_profile(base / "lane", base / "ret", **args)


TREE = {"README.md": "hi\n", f"{ACT}/posture.md": "p\n", f"{ACT}/hooks/stop.py": "print(1)\n",
        f"{CODEX_ACT}/hooks/stop.py": "print(2)\n", "pkg/__init__.py": ""}


def test_a_tracked_activation_at_base_builds(rig):
    lane = rig[0] / "lane"
    sha = commit(lane, TREE, exe=(f"{ACT}/hooks/stop.py",))
    p = build(rig, base_sha=sha)
    assert p.clone == lane


def test_every_exempt_path_is_in_deny_write_and_in_the_file_tool_deny_rules(rig):
    lane = rig[0] / "lane"
    sha = commit(lane, TREE)
    p = build(rig, base_sha=sha)
    dw = p.settings["sandbox"]["filesystem"]["denyWrite"]
    deny = p.settings["permissions"]["deny"]
    for rel in (ACT, CODEX_ACT):
        assert str(lane / rel) in dw
        for tool in sp.FILE_TOOLS:
            assert f"{tool}(//{str(lane / rel).lstrip('/')}/**)" in deny
            assert f"{tool}({rel}/**)" in deny
    # the clone stays writable: the exemption adds deny rules and widens nothing
    assert p.settings["sandbox"]["filesystem"]["allowWrite"] == [str(lane), str(rig[0] / "ret")]


def test_without_a_base_a_tracked_activation_still_refuses(rig):
    lane = rig[0] / "lane"
    commit(lane, TREE)
    with pytest.raises(sp.SeatRefusal, match="agent-config target"):
        build(rig)


def test_an_untracked_activation_dir_refuses(rig):
    lane = rig[0] / "lane"
    sha = commit(lane, TREE)
    (lane / "pkg" / "other" / "activation").mkdir(parents=True)
    (lane / "pkg" / "other" / "activation" / "x.py").write_text("x\n")
    with pytest.raises(sp.SeatRefusal, match=r"pkg/other/activation \(not exempt: git does not track it"):
        build(rig, base_sha=sha)


def test_an_extra_untracked_file_inside_a_tracked_activation_refuses(rig):
    lane = rig[0] / "lane"
    sha = commit(lane, TREE)
    (lane / ACT / "hooks" / "extra.py").write_text("x\n")
    with pytest.raises(sp.SeatRefusal, match="hooks/extra.py is not tracked at the base"):
        build(rig, base_sha=sha)


def test_an_extra_directory_inside_a_tracked_activation_refuses(rig):
    lane = rig[0] / "lane"
    sha = commit(lane, TREE)
    (lane / ACT / "empty").mkdir()
    with pytest.raises(sp.SeatRefusal, match="directory git does not track"):
        build(rig, base_sha=sha)


def test_a_tracked_file_modified_in_the_working_tree_refuses(rig):
    lane = rig[0] / "lane"
    sha = commit(lane, TREE)
    (lane / ACT / "posture.md").write_text("changed\n")
    with pytest.raises(sp.SeatRefusal, match="posture.md differs from the base"):
        build(rig, base_sha=sha)


def test_a_tracked_file_whose_executable_bit_changed_refuses(rig):
    lane = rig[0] / "lane"
    sha = commit(lane, TREE)
    (lane / ACT / "hooks" / "stop.py").chmod(0o755)
    with pytest.raises(sp.SeatRefusal, match="executable bit"):
        build(rig, base_sha=sha)


def test_a_tracked_file_missing_from_the_working_tree_refuses(rig):
    lane = rig[0] / "lane"
    sha = commit(lane, TREE)
    os.remove(lane / ACT / "posture.md")
    with pytest.raises(sp.SeatRefusal, match="posture.md is tracked at the base but missing"):
        build(rig, base_sha=sha)


def test_a_symlink_inside_activation_refuses_tracked_or_planted(rig):
    lane = rig[0] / "lane"
    sha = commit(lane, TREE, links={f"{ACT}/link.md": "posture.md"})     # tracked at base as mode 120000
    with pytest.raises(sp.SeatRefusal, match="link.md is tracked as mode 120000"):
        build(rig, base_sha=sha)


def test_a_symlink_planted_in_place_of_a_tracked_file_refuses(rig, tmp_path):
    lane = rig[0] / "lane"
    sha = commit(lane, TREE)
    os.remove(lane / ACT / "posture.md")
    os.symlink(lane / "README.md", lane / ACT / "posture.md")
    with pytest.raises(sp.SeatRefusal, match="posture.md is a symlink"):
        build(rig, base_sha=sha)


def test_an_activation_that_is_itself_a_symlink_refuses(rig):
    lane = rig[0] / "lane"
    sha = commit(lane, TREE)
    os.symlink(lane / "pkg" / "templates", lane / "pkg" / "activation")
    with pytest.raises(sp.SeatRefusal, match=r"pkg/activation \(not exempt: it is a symlink"):
        build(rig, base_sha=sha)


def test_a_nested_claude_still_refuses_while_activation_is_exempt(rig):
    lane = rig[0] / "lane"
    sha = commit(lane, TREE)
    (lane / "pkg" / ".claude").mkdir()
    with pytest.raises(sp.SeatRefusal) as exc:
        build(rig, base_sha=sha)
    assert f"{lane / 'pkg' / '.claude'} is an agent-config target" in str(exc.value)
    assert "activation" not in str(exc.value)                 # the tracked activation dirs passed


def test_a_tracked_nested_claude_is_never_exempt(rig):
    lane = rig[0] / "lane"
    sha = commit(lane, {**TREE, "pkg/.claude/settings.json": "{}"})
    with pytest.raises(sp.SeatRefusal, match=r"pkg/\.claude is an agent-config target"):
        build(rig, base_sha=sha)


def test_a_protected_name_inside_a_tracked_activation_refuses(rig):
    lane = rig[0] / "lane"
    sha = commit(lane, {**TREE, f"{ACT}/.claude/settings.json": "{}"})
    with pytest.raises(sp.SeatRefusal, match="is a protected name inside it"):
        build(rig, base_sha=sha)


def test_a_tracked_activation_at_the_clone_root_still_refuses(rig):
    lane = rig[0] / "lane"
    sha = commit(lane, {**TREE, "activation/posture.md": "p\n"})
    with pytest.raises(sp.SeatRefusal, match=r"lane/activation is an agent-config target"):
        build(rig, base_sha=sha)


def test_a_base_the_clone_does_not_hold_refuses(rig):
    lane = rig[0] / "lane"
    commit(lane, TREE)
    with pytest.raises(sp.SeatRefusal, match="cannot be read from the clone"):
        build(rig, base_sha="0" * 40)
    with pytest.raises(sp.SeatRefusal, match="full lowercase hex"):
        build(rig, base_sha="HEAD")


def test_the_exemption_reads_the_base_not_the_index_or_a_later_commit(rig):
    """A commit made after the base that adds an activation file does not widen what the base launch trusts."""
    lane = rig[0] / "lane"
    sha = commit(lane, TREE)
    (lane / ACT / "later.md").write_text("later\n")
    _git(lane, "add", "-A")
    _git(lane, "commit", "-q", "-m", "later")
    with pytest.raises(sp.SeatRefusal, match="later.md is not tracked at the base"):
        build(rig, base_sha=sha)
    assert build(rig, base_sha=_git(lane, "rev-parse", "HEAD"))   # at the new commit it is tracked


def test_the_real_levain_repo_at_head_builds_a_profile(rig):
    """The fixture is this checkout's own HEAD, cloned shallow: levain/templates/activation and the codex adapter's."""
    here = Path(__file__).resolve().parents[1]
    try:
        head = _git(here, "rev-parse", "HEAD")
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("not a git checkout")
    lane = rig[0] / "lane"
    lane.rmdir()
    _git(rig[0], "clone", "-q", "--no-checkout", "--depth", "1", "--template=", f"file://{here}", str(lane))
    _git(lane, "checkout", "-q", "--detach", head)
    p = build(rig, base_sha=head)
    dw = p.settings["sandbox"]["filesystem"]["denyWrite"]
    for rel in ("levain/templates/activation", "levain/templates/adapters/codex/activation"):
        assert str(lane / rel) in dw
        assert f"Edit({rel}/**)" in p.settings["permissions"]["deny"]
