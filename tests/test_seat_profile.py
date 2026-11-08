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
    assert p.tools == hg.CC_SEAT_TOOLS == ("Bash", "Read", "Edit", "Write", "Glob", "Grep")
    sb = s["sandbox"]
    assert sb["network"]["allowLocalBinding"] is False and sb["network"]["strictAllowlist"] is True
    assert sb["network"]["allowedDomains"] == []
    assert sb["filesystem"]["allowWrite"] == [str(base / "lane"), str(base / "ret")]
    gh_dir = str(Path(os.path.realpath(os.environ["TMPDIR"])) / sp.CANARY_DIRNAME / "levain-home" / "seat_gh")
    real_gh = str(Path(os.path.realpath(sp.real_levain_home(os.environ))) / "seat_gh")
    # the read-token directory, as the key directory is: the canary server's own AND the real one (L3 z2b r1 HIGH)
    assert sb["filesystem"]["denyRead"] == [str(keys), gh_dir, real_gh]
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
        for sub in sp._GIT_DENY:
            assert f"{tool}({sp._git_rule_path('.git', sub)})" in deny
            assert f"{tool}(//{sp._git_rule_path(str(base / 'lane') + '/.git', sub).lstrip('/')})" in deny
        assert "Edit(.git/config)" in deny and "Edit(.git/config/**)" not in deny   # config is a FILE: /** does not match it
        # a nested .git (a gitfile below the top level) is hard-denied; the shape never matches the root's own .git
        assert f"{tool}(*/**/.git)" in deny and f"{tool}(//{str(base / 'lane').lstrip('/')}/*/**/.git)" in deny
        # git commit needs .git writable (measured, z1e/RETURN_gitdeny.md): no rule may deny .git whole
        whole = [r for r in deny if r.startswith(f"{tool}(") and (r.endswith("/.git/**)") or r.endswith(".git)"))
                 and not r.endswith("/*/**/.git)") and r != f"{tool}(*/**/.git)"]
        assert whole == []
    assert any(r.startswith("Read(") and str(keys).lstrip("/") in r for r in deny)


def test_nested_git_is_one_bare_rule_that_covers_a_nested_git_directory(rig):
    """The nested ``.git`` shape is the bare ``*/**/.git`` and nothing else, per tool and spelling. Measured on the host
    (2.1.319, the real profile, sonnet, 1106-16/z1e/RETURN_nested_git.md, arms A2/A3): with a real nested repo
    ``sub/.git/`` planted after the build, a Write to ``sub/.git/{hooks/pre-commit,info/zz,modules/m/zz,worktrees/w/zz}``
    and an Edit of ``sub/.git/config`` were each "denied by your permission settings" with no prompt, Bash writes to
    ``sub/.git/hooks`` and ``sub/.git/config`` were "operation not permitted", and ``git commit`` in the lane landed.
    Arm B1 (these rules removed) prompted on all five file-tool writes, so this rule, not a vendor default, closes
    them: a bare pattern also denies the CONTENTS of a directory it names (the same fact that made ``**/.git`` break
    the lane's commit, RETURN_gitdeny.md). Adding ``*/**/.git/**`` or per-path ``*/**/.git/hooks/**`` would be
    redundant; dropping or narrowing the bare rule reopens the prompt. Measured consequence, not a defect: ``git
    commit`` INSIDE a nested repo fails (``sub/.git/index.lock: Operation not permitted``)."""
    base, keys, _ = rig
    lane, ret = base / "lane", base / "ret"
    deny = sp.seat_permission_deny((lane, ret), keys)
    nested = [r for r in deny if "*/**/.git" in r]
    want = []
    for i, root in enumerate((lane, ret)):
        for tool in sp.FILE_TOOLS:
            want.append(f"{tool}(//{str(root).lstrip('/')}/*/**/.git)")
            if i == 0:
                want.append(f"{tool}(*/**/.git)")
    assert sorted(nested) == sorted(want)   # exactly these: no /** variant, no per-path rule below a nested .git


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
    assert a[a.index("--tools") + 1] == "Bash,Read,Edit,Write,Glob,Grep"
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
    home = tmp_path / "lhome"
    assert build(rig, lease=lease, canary_root=None, levain_home=home).clone == base / "lane"
    bad = {**lease, "lanes_parent": str(tmp_path / "nope")}
    with pytest.raises(sp.SeatRefusal, match="lanes_parent"):
        build(rig, lease=bad, canary_root=None, levain_home=home)


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


# --- PATHZERO Z2b: the one MCP server, levain_gh, and the read-token directory -------------------------------------------

def test_the_profile_loads_exactly_levain_gh_and_allows_its_two_tools(rig):
    """Revert: ``gh_mcp_config`` returning another/extra server, or ``"allow"`` dropped from the permissions."""
    p = build(rig)
    servers = p.mcp_config["mcpServers"]
    assert list(servers) == [sp.GH_SERVER_NAME] == ["levain_gh"]
    entry = servers["levain_gh"]
    assert entry["type"] == "stdio" and entry["command"] == sys.executable
    assert entry["args"][:4] == ["-EP", "-B", "-m", "levain.seat_gh_mcp"] and entry["args"][4] == "--home"
    assert not any("token" in a.lower() or "ghp_" in a for a in entry["args"])
    assert p.settings["permissions"]["allow"] == ["mcp__levain_gh__gh_read", "mcp__levain_gh__git_fetch"]
    a = p.argv()
    assert "--strict-mcp-config" in a and json.loads(a[a.index("--mcp-config") + 1]) == dict(p.mcp_config)


def test_the_token_directory_is_denied_to_bash_and_to_read_like_the_key_directory(rig, tmp_path):
    """Revert: the ``str(gh_dir)`` member of ``denyRead`` or the ``Read`` rule appended after the key directory's."""
    home = tmp_path / "lhome"
    home.mkdir()
    p = build(rig, levain_home=home)
    gh = str(Path(os.path.realpath(home)) / "seat_gh")
    assert gh in p.settings["sandbox"]["filesystem"]["denyRead"]
    assert f"Read(//{gh.lstrip('/')}/**)" in p.settings["permissions"]["deny"]
    assert p.mcp_config["mcpServers"]["levain_gh"]["args"][-1] == str(Path(os.path.realpath(home)))
    # the mirror directory is NOT under the denied one: the seat must read it
    from levain import seat_gh
    assert not seat_gh.MIRROR_DIR.startswith(seat_gh.TOKEN_DIR)


def test_a_root_that_overlaps_the_token_directory_is_refused(rig, tmp_path):
    """Revert: the ``gh_dir`` clause of ``_roots_problems``."""
    base, keys, claude = rig
    with pytest.raises(sp.SeatRefusal, match="read-token directory"):
        build(rig, levain_home=base / "lane")
