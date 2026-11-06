"""The terminal seat profile builder (Z1b): unit tests, nothing is launched."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from levain import host_guard as hg
from levain import seat_profile as sp


@pytest.fixture
def rig(tmp_path, monkeypatch):
    tmp = Path(os.path.realpath(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp))
    base = Path(tempfile_prefix(tmp)) / "n1"
    (base / "lane" / ".git").mkdir(parents=True)
    (base / "ret").mkdir()
    keys = tmp / "keys"
    keys.mkdir()
    claude = tmp / "bin" / "claude"
    claude.parent.mkdir()
    claude.write_text("#!/bin/sh\n")
    return base, keys, claude


def tempfile_prefix(tmp: Path) -> Path:
    return tmp / sp.CANARY_DIRNAME


def build(rig, **kw):
    base, keys, claude = rig
    args = dict(claude_bin=str(claude), python=sys.executable, lease=None, key_dir=keys, canary_root=base,
                managed=[], environ={**os.environ, "TMPDIR": os.environ["TMPDIR"]})
    args.update(kw)
    return sp.build_seat_profile(base / "lane", base / "ret", **args)


def test_builds_and_shapes(rig):
    p = build(rig)
    base, keys, _ = rig
    s = p.settings
    assert p.tools == hg.CC_SEAT_TOOLS == ("Bash", "Read", "Edit", "Write")
    sb = s["sandbox"]
    assert sb["network"]["allowLocalBinding"] is False and sb["network"]["strictAllowlist"] is True
    assert sb["network"]["allowedDomains"] == []
    assert sb["filesystem"]["allowWrite"] == [str(base / "lane"), str(base / "ret")]
    assert sb["filesystem"]["denyRead"] == [str(keys)]
    assert s["permissions"]["defaultMode"] == "acceptEdits"
    assert s["includeGitInstructions"] is False and s["autoMemoryEnabled"] is False
    assert hg.check_sandbox_types(sb) == []


def test_deny_write_keeps_git_commit_possible(rig):
    base, _, _ = rig
    dw = sp.seat_deny_write(base / "lane", git=True)
    assert str(base / "lane" / ".git") not in dw
    for sub in ("hooks", "config", "info"):
        assert str(base / "lane" / ".git" / sub) in dw
    assert str(base / "lane" / ".claude") in dw
    assert not any(".git" in d for d in sp.seat_deny_write(base / "ret", git=False))


def test_permission_deny_names_all_four_file_tools(rig):
    base, keys, _ = rig
    deny = sp.seat_permission_deny((base / "lane", base / "ret"), keys)
    for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        assert any(r.startswith(f"{tool}(") and ".claude/**" in r for r in deny)
        assert any(r.startswith(f"{tool}(") and r.endswith("/.mcp.json)") for r in deny)
        assert f"{tool}(**/.git)" in deny
    assert any(r.startswith("Read(") and str(keys).lstrip("/") in r for r in deny)


def test_hooks_ride_the_settings(rig):
    p = build(rig)
    hooks = p.settings["hooks"]
    assert set(hooks) == {"PermissionRequest", "PreToolUse", "PostToolUse", "PostToolUseFailure"}
    cmd = lambda ev: hooks[ev][0]["hooks"][0]["command"]
    assert "levain.observe hook" in cmd("PermissionRequest")
    assert "levain.seat_record pre" in cmd("PreToolUse")
    assert "levain.seat_record post" in cmd("PostToolUse") and cmd("PostToolUse") == cmd("PostToolUseFailure")
    assert "onFailure" not in hooks["PreToolUse"][0]["hooks"][0]


def test_argv_and_digest(rig):
    p = build(rig)
    a = p.argv()
    assert "-p" not in a
    assert a[1:3] == list(hg.SEAT_SOURCE_ARGS) and a[3] == "--restricted"   # L3 z1 r1 codex 2: file tools confined
    assert a[a.index("--tools") + 1] == "Bash,Read,Edit,Write"
    assert "--strict-mcp-config" in a and a[a.index("--permission-mode") + 1] == "acceptEdits"
    h = p.argv(headless=True, model="haiku")
    assert "-p" in h and h[-2:] == ["--model", "haiku"]
    assert json.loads(a[a.index("--settings") + 1]) == p.settings
    assert p.digest() == build(rig).digest()
    assert p.env["CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS"] == "1"


@pytest.mark.parametrize("name", [".claude", ".mcp.json", ".codex", ".levain", "activation"])
@pytest.mark.parametrize("where", ["lane", "ret"])
def test_refuses_root_with_agent_config(rig, name, where):
    base = rig[0]
    target = base / where / name
    if name in (".mcp.json",):
        target.write_text("{}")
    else:
        target.mkdir()
    with pytest.raises(sp.SeatRefusal, match="agent-config target"):
        build(rig)


def test_refuses_nested_git_and_gitfile(rig):
    base = rig[0]
    (base / "lane" / "sub").mkdir()
    (base / "lane" / "sub" / ".git").write_text("gitdir: x")
    with pytest.raises(sp.SeatRefusal, match="agent-config target"):
        build(rig)


def test_refuses_gitfile_clone(rig):
    base = rig[0]
    os.rmdir(base / "lane" / ".git")
    (base / "lane" / ".git").write_text("gitdir: elsewhere")
    with pytest.raises(sp.SeatRefusal):
        build(rig)


def test_lease_none_only_under_canary_prefix(rig, tmp_path):
    base, keys, claude = rig
    with pytest.raises(sp.SeatRefusal, match="no lease"):
        build(rig, canary_root=None)
    other = Path(os.path.realpath(tmp_path)) / "elsewhere"
    (other / "lane" / ".git").mkdir(parents=True)
    (other / "ret").mkdir()
    with pytest.raises(sp.SeatRefusal, match="not under"):
        sp.build_seat_profile(other / "lane", other / "ret", claude_bin=str(claude), python=sys.executable,
                              lease=None, key_dir=keys, canary_root=other, managed=[])


def test_lease_paths_checked(rig, tmp_path):
    base, keys, _ = rig
    lease = {"kind": sp.SEAT_LEASE_KIND, "agent": "claude_code", "lanes_parent": str(base),
             "returns_parent": str(base)}
    assert build(rig, lease=lease, canary_root=None).clone == base / "lane"
    bad = {**lease, "lanes_parent": str(tmp_path / "nope")}
    with pytest.raises(sp.SeatRefusal, match="lanes_parent"):
        build(rig, lease=bad, canary_root=None)


def test_refuses_writable_executable(rig):
    base, keys, claude = rig
    mine = base / "ret" / "claude"
    mine.write_text("x")
    with pytest.raises(sp.SeatRefusal, match="claude binary"):
        build(rig, claude_bin=str(mine))


def test_refuses_key_dir_inside_root(rig):
    base = rig[0]
    with pytest.raises(sp.SeatRefusal, match="key directory"):
        build(rig, key_dir=base / "ret" / "k")  # does not exist yet but overlaps


def test_hosted_sandbox_block_still_valid():
    assert hg.check_sandbox_types(hg.cc_sandbox_block("/x")) == []
    assert hg.CC_HOSTED_TOOLS == ("Bash", "Read")


def test_the_canary_prefix_is_refused_unless_private_to_this_user(tmp_path):
    from levain import seat_profile as sp
    env = {"TMPDIR": str(tmp_path)}
    prefix = sp.ensure_canary_prefix(env)
    assert prefix.is_dir() and (prefix.stat().st_mode & 0o777) == 0o700
    prefix.chmod(0o777)                                   # another user could swap a canary root inside it
    with pytest.raises(sp.SeatRefusal, match="not a private directory"):
        sp.ensure_canary_prefix(env)
