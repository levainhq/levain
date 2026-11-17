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
def rig(tmp_path, monkeypatch, levain_home_outside_temp):
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
    if args["lease"] is None:   # a lease-free build's own home is under $TMPDIR and refuses (L3 wire2 r2 codex H1)
        args.setdefault("budget_home", Path(os.environ["LEVAIN_HOME"]) / "canary-budget")
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


def _lease_home() -> Path:
    """A levain home OUTSIDE every implicit root of the rig's environ, on Linux too (where tmp_path is under /tmp): the
    rig's ``levain_home_outside_temp`` ``$LEVAIN_HOME``, under the account home's test scratch parent."""
    home = Path(os.path.realpath(os.environ["LEVAIN_HOME"])) / "lhome"
    assert not any(hg.is_within(home, t) for t in sp.implicit_write_roots(os.environ))
    return home


def _lease(base):
    return {"kind": sp.SEAT_LEASE_KIND, "agent": "claude_code", "lanes_parent": str(base), "returns_parent": str(base)}


@pytest.mark.parametrize("root", [*sp.IMPLICIT_TMP_ROOTS, "$TMPDIR"])
def test_a_lease_launch_refuses_a_read_budget_home_under_an_implicit_temp_root(rig, root):
    """L3 wire2 r1 codex HIGH. The sandbox lets the seat's Bash write /tmp, /private/tmp, /var/tmp, /dev/shm and its
    $TMPDIR without an allowWrite entry: a read budget under one is a hook script the seat could rewrite and an
    unsandboxed hook would exec. Refused before anything is written, naming the read budget and LEVAIN_HOME. Revert:
    the ``implicit_write_roots`` clause of ``build_seat_profile`` (the build then passes and installs the script)."""
    import secrets
    base = rig[0]
    parent = Path(os.environ["TMPDIR"]) if root == "$TMPDIR" else Path(root)
    home = parent / f"levain-h1-{secrets.token_hex(6)}" / "home"
    for dry in (True, False):
        with pytest.raises(sp.SeatRefusal, match=r"read budget directory .* temp directory .*LEVAIN_HOME"):
            build(rig, lease=_lease(base), canary_root=None, levain_home=home, dry_run=dry)
    assert not os.path.lexists(home.parent)
    # the same home under a non-temp parent builds (the refusal is narrow)
    assert build(rig, lease=_lease(base), canary_root=None, levain_home=_lease_home()).env["LEVAIN_READ_BUDGET_DIR"]


def test_an_explicit_budget_home_is_held_to_the_temp_roots_too(rig):
    """The canary's per-run home is checked as a lease's is: a ``budget_home`` under $TMPDIR refuses."""
    with pytest.raises(sp.SeatRefusal, match="temp directory"):
        build(rig, budget_home=Path(os.environ["TMPDIR"]) / "bh")


@pytest.mark.parametrize("dry", [True, False])
def test_a_lease_free_build_with_no_budget_home_is_held_to_the_temp_roots(rig, dry):
    """L3 wire2 r2 codex H1. With neither a lease nor a ``budget_home`` the budget falls back to the canary prefix's
    own home, under $TMPDIR: refused, nothing written. Revert: ``if lease is not None or budget_home is not None:``
    around the implicit-root check (the build then passes and installs the script under $TMPDIR)."""
    base = rig[0]
    with pytest.raises(sp.SeatRefusal, match="read budget directory .* temp directory"):
        sp.build_seat_profile(base / "lane", base / "ret", claude_bin=str(rig[2]), python=sys.executable, lease=None,
                              key_dir=rig[1], canary_root=base, managed=[], environ=dict(os.environ), dry_run=dry)
    assert not list(Path(os.environ["TMPDIR"]).rglob("read_budget"))


def test_a_home_under_tmpdir_names_tmpdir_as_the_cause(rig, monkeypatch):
    """L3 wire2 r2 complement LOW 4. ``TMPDIR=$HOME``: the default home is the one in use, so the message names the
    matched root and says TMPDIR is the cause, never "set LEVAIN_HOME to the default". Revert: the TMPDIR branch of
    ``_temp_root_problem``."""
    user = Path(os.path.realpath(os.environ["LEVAIN_HOME"])) / "user"
    (user / ".levain").mkdir(parents=True)
    env = {**os.environ, "HOME": str(user), "TMPDIR": str(user)}
    msg = sp._temp_root_problem(user / ".levain" / "read_budget", user, env)
    assert f"is inside {user}," in msg and f"TMPDIR ({user}) is the cause" in msg and "the default" not in msg
    base = rig[0]
    monkeypatch.setenv("TMPDIR", str(user))
    with pytest.raises(sp.SeatRefusal, match=r"TMPDIR \(.*\) is the cause") as exc:
        build(rig, lease=_lease(base), canary_root=None, levain_home=user / ".levain", environ=env)
    assert "the default" not in str(exc.value)


def test_a_home_under_a_fixed_root_names_it_and_offers_the_default_only_when_safe(rig):
    """L3 wire2 r2 complement LOW 4: the matched fixed root is named; the default ``~/.levain`` is offered only when it
    lies outside every temp root and is not the home in use."""
    safe = Path(os.path.realpath(os.environ["LEVAIN_HOME"])) / "user"
    msg = sp._temp_root_problem(Path("/tmp/x/read_budget"), sp._real("/tmp"), {"HOME": str(safe), "TMPDIR": "/nowhere"})
    assert f"is inside {sp._real('/tmp')}," in msg and "set LEVAIN_HOME" in msg and "TMPDIR (" not in msg
    assert f"(the default {sp._real(safe / '.levain')} is)" in msg
    in_tmp = sp._real("/tmp") / "u"     # a HOME under /tmp: its ~/.levain is no advice
    msg = sp._temp_root_problem(in_tmp / ".levain" / "read_budget", sp._real("/tmp"), {"HOME": str(in_tmp)})
    assert "set LEVAIN_HOME" in msg and "the default" not in msg


def test_implicit_write_roots_are_resolved_and_carry_the_seats_tmpdir(tmp_path):
    roots = sp.implicit_write_roots({"TMPDIR": str(tmp_path)})
    assert Path(os.path.realpath(tmp_path)) in roots and Path(os.path.realpath("/tmp")) in roots
    assert all(r == Path(os.path.realpath(r)) for r in roots) and len(set(roots)) == len(roots)
    assert Path(os.path.realpath(tmp_path)) not in sp.implicit_write_roots({})


def test_a_refused_build_installs_no_read_budget(rig, monkeypatch):
    """L3 wire2 r1 deepseek 2. A build refused by the managed-settings check (the LAST refusal) leaves no
    ``read_budget`` behind. Revert: the install above that ``if problems: raise``."""
    home = _lease_home()
    monkeypatch.setattr(sp.hg, "check_managed_settings", lambda *a, **k: ["a managed setting refuses"])
    with pytest.raises(sp.SeatRefusal, match="a managed setting refuses"):
        build(rig, lease=_lease(rig[0]), canary_root=None, levain_home=home)
    assert not (home / "read_budget").exists()


def test_a_budget_hook_event_seat_record_does_not_register_is_added_not_a_crash(rig, monkeypatch):
    """L3 wire2 r1 deepseek 3. Revert: ``hooks[ev]`` for ``hooks.get(ev, [])`` (a KeyError out of the builder)."""
    from levain.desk import read_budget
    real = read_budget.settings_hooks
    group = {"matcher": "Read", "hooks": [{"type": "command", "command": "x read_budget-extra"}]}
    monkeypatch.setattr(read_budget, "settings_hooks", lambda py, s: {**real(py, s), "SessionStart": [group]})
    p = build(rig)
    assert p.settings["hooks"]["SessionStart"] == [group]


def test_lease_paths_checked(rig, tmp_path):
    base, keys, _ = rig
    lease = {"kind": sp.SEAT_LEASE_KIND, "agent": "claude_code", "lanes_parent": str(base),
             "returns_parent": str(base)}
    home = _lease_home()           # a lease launch refuses a home under the rig's $TMPDIR (tmp_path): L3 wire2 r1 H1
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


# --- the read budget rides every seat (levain row 800) ---------------------------------------------------------------

def _budget_entries(p):
    return {ev: [g for g in p.settings["hooks"][ev] if any("read_budget-" in h["command"] for h in g["hooks"])]
            for ev in ("PreToolUse", "PostToolUse")}


def test_the_read_budget_hooks_are_appended_after_seat_record(rig):
    import re
    p = build(rig)
    hooks = p.settings["hooks"]
    assert set(hooks) == {"PermissionRequest", "PreToolUse", "PostToolUse", "PostToolUseFailure"}   # no new event name
    for ev, arg in (("PreToolUse", "pre"), ("PostToolUse", "post")):
        assert "levain.seat_record" in hooks[ev][0]["hooks"][0]["command"]       # seat_record stays at index 0
        assert len(hooks[ev]) == 2
        group = hooks[ev][1]
        assert group["matcher"] == "Read"
        cmd = group["hooks"][0]["command"]
        assert re.search(r"/read_budget/read_budget-[0-9a-f]{16}\.py", cmd) and f" {arg};" in cmd
    assert all("read_budget-" not in h["command"] for g in hooks["PostToolUseFailure"] for h in g["hooks"])
    assert all(len(v) == 1 for v in _budget_entries(p).values())


def test_the_read_budget_dir_rides_the_profile_env_and_the_digest(rig):
    import dataclasses
    p = build(rig)
    state = p.env["LEVAIN_READ_BUDGET_DIR"]
    assert state.endswith("/canary-budget/read_budget/state") and Path(state).is_dir()
    assert "LEVAIN_READ_BUDGET_DIR" not in hg._ENV_ALLOW        # a deliberate addition outside host_env's allowlist
    # the builder has no no-budget mode: strip the env var from a built profile instead
    bare = dataclasses.replace(p, env={k: v for k, v in p.env.items() if k != "LEVAIN_READ_BUDGET_DIR"})
    assert bare.digest() != p.digest()


def test_the_read_budget_dir_inside_a_seat_root_refuses(rig):
    base, _, _ = rig
    with pytest.raises(sp.SeatRefusal, match="read budget directory"):
        build(rig, budget_home=base / "lane" / "home")
    assert not (base / "lane" / "home" / "read_budget").exists()


@pytest.mark.parametrize("exc", [OSError("disk full"), ValueError("relative")])
def test_a_read_budget_install_error_refuses_naming_it(rig, monkeypatch, exc):
    from levain.desk import read_budget_install as rbi

    def boom(home):
        raise exc
    monkeypatch.setattr(rbi, "install", boom)
    with pytest.raises(sp.SeatRefusal, match="read budget could not be installed"):
        build(rig)


def _allow_write_overlaps(profile_settings: dict, budget_dir: Path) -> list[str]:
    """Every sandbox ``allowWrite`` entry that overlaps the read budget directory, in EITHER direction: the budget dir
    inside the entry (an entry that covers it) or the entry inside the budget dir (a child of it). ``hg.is_within`` is
    one-directional and resolves symlinks, so both calls are needed."""
    return [e for e in profile_settings["sandbox"]["filesystem"]["allowWrite"]
            if hg.is_within(budget_dir, e) or hg.is_within(e, budget_dir)]


@pytest.mark.parametrize("lease", [False, True])
def test_no_allow_write_entry_overlaps_the_read_budget_directory(rig, lease):
    """Desk ruling (L2 wire2): the seat's Bash must not be able to write the hook script or the ledger an unsandboxed
    hook execs and reads. For the profile ``build_seat_profile`` produces, no ``allowWrite`` entry covers the budget
    directory and none lies inside it: a lease launch's home and a lease-free build's ``budget_home``."""
    if lease:
        home = _lease_home()
        p = build(rig, lease=_lease(rig[0]), canary_root=None, levain_home=home)
    else:
        home = Path(os.environ["LEVAIN_HOME"]) / "canary-budget"
        p = build(rig)
    budget_dir = Path(os.path.realpath(home)) / "read_budget"
    assert budget_dir.is_dir() and Path(p.env["LEVAIN_READ_BUDGET_DIR"]).parent == budget_dir
    assert p.settings["sandbox"]["filesystem"]["allowWrite"]                  # not vacuous
    assert _allow_write_overlaps(p.settings, budget_dir) == []


def test_the_overlap_check_flags_a_covering_or_child_allow_write_entry(rig):
    """The check above can fail: a profile whose ``allowWrite`` names the budget dir, its parent, or a child of it."""
    p = build(rig)
    budget_dir = Path(p.env["LEVAIN_READ_BUDGET_DIR"]).parent
    for bad in (budget_dir, budget_dir.parent, budget_dir / "state"):
        fake = json.loads(json.dumps(p.settings))
        fake["sandbox"]["filesystem"]["allowWrite"].append(str(bad))
        assert _allow_write_overlaps(fake, budget_dir) == [str(bad)], bad


def test_a_dry_run_builds_the_same_hooks_and_env_and_writes_nothing(rig):
    base, _, _ = rig
    home = Path(os.environ["LEVAIN_HOME"]) / "canary-budget"
    dry = build(rig, dry_run=True)
    assert not (home / "read_budget").exists()
    real = build(rig)
    assert dry.settings["hooks"] == real.settings["hooks"] and dry.env == real.env and (home / "read_budget").is_dir()
