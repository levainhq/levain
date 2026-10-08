"""Chat S2: the hosted-primary profile and its canary's evaluator.

The live canary (a real `claude` turn) is not run here: it needs a logged-in Claude Code. These
tests pin the parts whose failures were REPRODUCED on 2026-10-06/07: a wrongly-typed sandbox key
dropping the whole block, a hook tree the entity can rewrite, an ECONNREFUSED that must never read
as BLOCKED, the canary's argv drifting from the session's, and the evaluator's verdicts on the
three control runs (each control's turn is rebuilt here from what the live run produced).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from levain import host_canary as hc
from levain import host_guard as hg


# ---------------------------------------------------------------------------- fixtures


def _entity(root: Path) -> Path:
    ent = root / "ent"
    hooks = ent / "activation" / "hooks"
    hooks.mkdir(parents=True)
    (ent / ".levain").mkdir()
    for name in ("_levain_hook.py", "session_start.py", "user_prompt_submit.py", "automemory_mirror.py"):
        (hooks / name).write_text(f"# {name}\n", encoding="utf-8")
    return ent


@pytest.fixture
def ent(tmp_path: Path) -> Path:
    return _entity(tmp_path)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "h"
    h.mkdir()
    return h


@pytest.fixture
def entity_only_roots(monkeypatch):
    """pytest's tmp_path sits under $TMPDIR, which IS a writable root, so a host dir there is
    correctly refused. Tests about profile wiring narrow the roots to the entity itself."""
    monkeypatch.setattr(hg, "cc_writable_roots", lambda e: (Path(os.path.realpath(e)),))


# ---------------------------------------------------------------------------- sandbox schema


def test_sandbox_block_types_pass():
    assert hg.check_sandbox_types(hg.cc_sandbox_block("/x")) == []


def test_string_where_bool_belongs_is_caught():
    # probes 1f: allowUnsandboxedCommands: "forbid" made CC 2.1.292 drop the whole block silently.
    block = hg.cc_sandbox_block("/x")
    block["allowUnsandboxedCommands"] = "forbid"
    assert any("allowUnsandboxedCommands must be bool" in p for p in hg.check_sandbox_types(block))


def test_int_is_not_bool():
    block = hg.cc_sandbox_block("/x")
    block["enabled"] = 1
    assert hg.check_sandbox_types(block)


def test_missing_key_is_caught():
    block = hg.cc_sandbox_block("/x")
    del block["network"]["allowLocalBinding"]
    assert any("allowLocalBinding is missing" in p for p in hg.check_sandbox_types(block))


def test_deny_write_covers_hooks_and_store(ent):
    deny = hg.cc_sandbox_block(ent)["filesystem"]["denyWrite"]
    real = Path(os.path.realpath(ent))
    assert str(real / "activation") in deny and str(real / ".levain") in deny


# ---------------------------------------------------------------------------- hooks


def test_hook_commands_are_rewritten_from_the_package_template(ent, tmp_path):
    py = sys.executable
    hooks = hg.cc_hook_settings(ent, py, tmp_path / "pyc")
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "PostToolUse"}
    real = os.path.realpath(ent)
    for groups in hooks.values():
        for g in groups:
            for h in g["hooks"]:
                argv = __import__("shlex").split(h["command"])
                assert argv[:5] == [py, "-E", "-P", "-X", f"pycache_prefix={tmp_path / 'pyc'}"]
                assert argv[5].startswith(f"{real}/activation/hooks/") and argv[5].endswith(".py")
                assert "${CLAUDE_PROJECT_DIR}" not in h["command"]


def test_unexpected_template_shape_refuses(ent, tmp_path, monkeypatch):
    t = tmp_path / "settings.template.json"
    t.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
        {"type": "command", "command": "bash -c 'curl 127.0.0.1'"}]}]}}))
    monkeypatch.setattr(hg, "_CC_SETTINGS_TEMPLATE", t)
    with pytest.raises(hg.HostRefusal, match="unexpected SessionStart hook command"):
        hg.cc_hook_settings(ent, sys.executable, tmp_path)


def test_pin_records_then_matches_then_refuses_on_change(ent, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    assert hg.check_or_record_pin(ent, state) == "recorded"
    assert hg.check_or_record_pin(ent, state) == "match"
    (ent / "activation" / "hooks" / "user_prompt_submit.py").write_text("import socket\n")
    with pytest.raises(hg.HostRefusal, match="changed since it was pinned"):
        hg.check_or_record_pin(ent, state)
    hg.repin(ent, state)
    assert hg.check_or_record_pin(ent, state) == "match"


def test_pin_ignores_pycache_but_sees_new_files(ent, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    hg.check_or_record_pin(ent, state)
    pc = ent / "activation" / "hooks" / "__pycache__"
    pc.mkdir()
    (pc / "x.cpython-312.pyc").write_bytes(b"\0")
    assert hg.check_or_record_pin(ent, state) == "match"
    (ent / "activation" / "hooks" / "json.py").write_text("")  # a planted sibling module
    with pytest.raises(hg.HostRefusal):
        hg.check_or_record_pin(ent, state)


def test_symlink_in_hook_tree_refuses(ent, tmp_path):
    (tmp_path / "elsewhere.py").write_text("")
    os.symlink(tmp_path / "elsewhere.py", ent / "activation" / "hooks" / "linked.py")
    with pytest.raises(hg.HostRefusal, match="symlink"):
        hg.hook_pin(ent)


def test_anneal_binary_inside_the_entity_refuses(ent):
    (ent / "activation" / "hooks" / "_levain_hook.py").write_text(
        f'_INSTALL_ANNEAL_BIN = "{ent}/bin/anneal-memory"\n')
    roots = hg.cc_writable_roots(ent)
    assert hg._check_hook_executables(Path(os.path.realpath(ent)), roots)


# ---------------------------------------------------------------------------- host dirs


def test_host_dir_is_private_and_outside_the_roots(ent, home):
    d = hg.host_dir(ent, "run", roots=(Path(os.path.realpath(ent)),), platform="darwin", home=home)
    assert d.is_dir() and (d.stat().st_mode & 0o077) == 0
    assert "levain" in d.parts


def test_host_dir_inside_a_writable_root_refuses(ent):
    # home under the entity: the run dir would be writable by the entity
    with pytest.raises(hg.HostRefusal, match="the entity can write"):
        hg.host_dir(ent, "run", roots=hg.cc_writable_roots(ent), platform="darwin", home=ent)


def test_host_dir_symlinked_component_refuses(ent, home, tmp_path):
    base = home / "Library" / "Application Support"
    base.mkdir(parents=True)
    (tmp_path / "other").mkdir()
    os.symlink(tmp_path / "other", base / "levain")
    with pytest.raises(hg.HostRefusal, match="symlink"):
        hg.host_dir(ent, "run", roots=hg.cc_writable_roots(ent), platform="darwin", home=home)


@pytest.mark.parametrize("environ", [{}, {"XDG_RUNTIME_DIR": "relative/dir"}])
def test_linux_without_a_usable_runtime_dir_refuses_never_tmp(ent, home, environ):
    with pytest.raises(hg.HostRefusal, match="XDG_RUNTIME_DIR"):
        hg.host_dir(ent, "run", roots=(), platform="linux", environ=environ, home=home)


def test_unsupported_platform_refuses(ent, home):
    with pytest.raises(hg.HostRefusal, match="not supported"):
        hg.host_dir(ent, "run", roots=(), platform="win32", home=home)


# ---------------------------------------------------------------------------- managed settings


_GUARD = ('G="/Library/Application Support/ClaudeCode/flow-guard/sandbox_guard.py"; '
          'if [ -r "$G" ]; then exec /usr/bin/python3 -I "$G" hook; else exit 0; fi')


def _managed(cmd: str) -> dict:
    return {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": cmd}]}]}}


def test_admin_hook_outside_the_roots_is_accepted(ent):
    # the shape of the 10-07 flow guard installed as a managed hook on Phill's machine
    assert hg.check_managed_settings([("m.json", _managed(_GUARD))], hg.cc_writable_roots(ent)) == []


def test_managed_hook_running_entity_code_refuses(ent):
    cmd = f"/usr/bin/python3 {ent}/activation/hooks/x.py"
    probs = hg.check_managed_settings([("m.json", _managed(cmd))], hg.cc_writable_roots(ent))
    assert probs and "entity can write" in probs[0]


@pytest.mark.parametrize("sandbox", [
    {"excludedCommands": ["git *"]}, {"network": {"allowLocalBinding": True}},
    {"network": {"allowUnixSockets": ["/x"]}}, {"filesystem": {"allowWrite": ["~"]}},
    {"enabled": False}, {"allowUnsandboxedCommands": True},
])
def test_managed_sandbox_loosening_refuses(ent, sandbox):
    assert hg.check_managed_settings([("m.json", {"sandbox": sandbox})], hg.cc_writable_roots(ent))


def test_unparseable_managed_file_refuses(ent):
    assert hg.check_managed_settings([("m.json", hg._MISSING)], ())


# ---------------------------------------------------------------------------- profile + argv


def test_host_dir_under_the_temp_dir_refuses(ent, home):
    # the real roots include $TMPDIR, and tmp_path lives there
    with pytest.raises(hg.HostRefusal, match="the entity can write"):
        hg.host_dir(ent, "run", roots=hg.cc_writable_roots(ent), platform="darwin", home=home)


def test_profile_and_canary_argv_differ_only_in_the_canary_flags(ent, home, entity_only_roots):
    p = hg.cc_host_profile(ent, claude_bin=sys.executable, home=home, platform="darwin", managed=[])
    session, canary = p.argv(), p.argv(canary=True)
    assert canary[: len(session)] == session
    assert canary[len(session):] == ["--no-session-persistence", "--model", "haiku"]
    for flag in ("--restricted", "--strict-mcp-config", "--disable-slash-commands"):
        assert flag in session
    assert session[session.index("--tools") + 1] == "Bash,Read,Edit,Write"
    assert session[session.index("--permission-mode") + 1] == "default"
    settings = json.loads(session[session.index("--settings") + 1])
    assert hg.check_sandbox_types(settings["sandbox"]) == []
    mcp = json.loads(session[session.index("--mcp-config") + 1])
    assert set(mcp["mcpServers"]) == {"anneal_memory"}
    assert "--dangerously-skip-permissions" not in session


def test_profile_records_pin_on_first_open(ent, home, entity_only_roots):
    p = hg.cc_host_profile(ent, claude_bin=sys.executable, home=home, platform="darwin", managed=[])
    assert p.pin_status == "recorded"
    p2 = hg.cc_host_profile(ent, claude_bin=sys.executable, home=home, platform="darwin", managed=[])
    assert p2.pin_status == "match"


def test_profile_refuses_a_non_levain_dir(tmp_path, home):
    with pytest.raises(hg.HostRefusal, match="not a Levain install"):
        hg.cc_host_profile(tmp_path, claude_bin=sys.executable, home=home, platform="darwin", managed=[])


def test_env_is_an_allowlist():
    env = hg.host_env({"PATH": "/bin", "HOME": "/h", "LC_ALL": "C", "CLAUDE_CODE_MESSAGING_TOKEN": "t",
                       "ANTHROPIC_API_KEY": "k", "AWS_SECRET_ACCESS_KEY": "s", "LEVAIN_CHAT_TOKEN": "c"})
    assert env == {"PATH": "/bin", "HOME": "/h", "LC_ALL": "C"}


# ---------------------------------------------------------------------------- the file-tool host policy


@pytest.mark.parametrize("rel,expected", [
    ("notes.txt", "allow"), ("src/a.py", "allow"),
    ("activation/hooks/x.py", "refuse"), ("activation/posture.md", "refuse"),
    (".levain/memory.db", "refuse"), (".claude/settings.local.json", "refuse"),
    (".mcp.json", "refuse"), ("sub/.git/config", "refuse"),
])
def test_write_decision_inside_the_entity(ent, rel, expected):
    assert hg.file_write_decision(ent, "Write", {"file_path": str(ent / rel)}) == expected


def test_write_outside_goes_to_the_human(ent, tmp_path):
    assert hg.file_write_decision(ent, "Write", {"file_path": str(tmp_path / "x")}) == "ask"


def test_write_through_a_symlink_into_activation_refuses(ent):
    os.symlink(ent / "activation", ent / "innocent")
    assert hg.file_write_decision(ent, "Edit", {"file_path": str(ent / "innocent" / "hooks" / "x.py")}) == "refuse"


def test_write_through_a_symlink_out_goes_to_the_human(ent, tmp_path):
    (tmp_path / "out").mkdir()
    os.symlink(tmp_path / "out", ent / "link")
    assert hg.file_write_decision(ent, "Write", {"file_path": str(ent / "link" / "f")}) == "ask"


def test_relative_path_resolves_against_the_entity(ent):
    assert hg.file_write_decision(ent, "Write", {"file_path": "activation/x"}) == "refuse"
    assert hg.file_write_decision(ent, "Write", {"file_path": "../escape"}) == "ask"


def test_missing_path_refuses(ent):
    assert hg.file_write_decision(ent, "Write", {}) == "refuse"


# ---------------------------------------------------------------------------- the probe, run for real


def test_probe_never_reports_refused_connections_as_blocked(tmp_path):
    """A closed port and a missing socket raise ECONNREFUSED / ENOENT, not EPERM: the probe must say
    INCONCLUSIVE, never BLOCKED, or a dead listener would read as a working sandbox."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens now
    script = tmp_path / "p.py"
    script.write_text(hc.probe_script(nonce="n0", port=port, sock=str(tmp_path / "none.sock"),
                                      outside=str(tmp_path / "nodir" / "f"), hook=str(tmp_path / "h")))
    out = subprocess.run(hc.probe_command(script, python=sys.executable), shell=True,
                         capture_output=True, text=True, timeout=30).stdout
    lines = dict(ln.split(" ", 2)[1:] for ln in out.splitlines())
    assert lines["TCP"].startswith("INCONCLUSIVE")
    assert lines["UDS"].startswith("INCONCLUSIVE")
    assert lines["FSOUT"].startswith("INCONCLUSIVE")  # ENOENT: the dir does not exist
    assert lines["FSHOOK"] == "ALLOWED"  # unsandboxed here, so the write lands


# ---------------------------------------------------------------------------- the evaluator, on the control runs


_INIT = {"tools": ["Bash", "Edit", "Read", "Write", "mcp__anneal_memory__recall"],
         "mcp_servers": [{"name": "anneal_memory", "status": "connected"}],
         "plugins": [{"name": "cc-plugin-telemetry", "path": "builtin"}],
         "permissionMode": "default", "claude_code_version": "2.1.293"}


def test_init_check_passes_the_live_shape():
    assert hc.check_init_event(dict(_INIT)).status == "pass"


@pytest.mark.parametrize("patch", [
    {"tools": _INIT["tools"] + ["SendMessage"]},
    {"tools": _INIT["tools"] + ["mcp__iterm-mcp__write_to_terminal"]},
    {"mcp_servers": [{"name": "anneal_memory", "status": "connected"}, {"name": "iterm-mcp", "status": "connected"}]},
    {"plugins": [{"name": "x", "path": "/Users/x/.claude/plugins/x"}]},
    {"permissionMode": "acceptEdits"},
    {"claude_code_version": "2.1.200"},
])
def test_init_check_fails_each_drift(patch):
    assert hc.check_init_event({**_INIT, **patch}).status == "fail"


class _FakeListener:
    def __init__(self, hits: int = 0) -> None:
        self._hits = hits

    def foreign_hits(self) -> int:
        return self._hits


def _turn(tmp_path: Path, *, bash_out: str, cmd: str, slots: dict[str, str], prompted: set[str],
          bash_prompt: bool = False) -> hc._Turn:
    t = hc._Turn(init=dict(_INIT), hook_events={"SessionStart", "UserPromptSubmit"})
    t.tool_uses.append({"id": "b", "name": "Bash", "input": {"command": cmd}})
    t.results["b"] = bash_out
    for i, path in enumerate(slots):
        t.tool_uses.append({"id": f"w{i}", "name": "Write", "input": {"file_path": path}})
    t.prompts += [{"tool_name": "Write", "input": {"file_path": p}} for p in prompted]
    if bash_prompt:
        t.prompts.append({"tool_name": "Bash", "input": {"command": cmd}})
    return t


def _slots(tmp_path: Path) -> dict[str, str]:
    return {str(tmp_path / "o.txt"): "outside", str(tmp_path / "hk.txt"): "hook",
            str(tmp_path / "lnk" / "f"): "link", str(tmp_path / "s.txt"): "scratch"}


_BLOCKED = "N TCP BLOCKED\nN UDS BLOCKED\nN FSOUT BLOCKED\nN FSHOOK BLOCKED\n"


def _status(checks: list[hc.Check]) -> dict[str, str]:
    return {c.name.split()[0]: c.status for c in checks}


def test_evaluator_passes_the_live_pass_run(tmp_path):
    slots = _slots(tmp_path)
    scratch = tmp_path / "s.txt"
    scratch.write_text("selftest")  # the host allowed it, so it landed
    t = _turn(tmp_path, bash_out=_BLOCKED, cmd="C", slots=slots, prompted={str(tmp_path / "hk.txt"), str(scratch)})
    checks = hc._evaluate(t, nonce="N", cmd="C", slots=slots, listener=_FakeListener(),
                          landed=[tmp_path / "o.txt", tmp_path / "hk.txt"], scratch=scratch)
    assert all(c.status == "pass" for c in checks), checks


def test_evaluator_control_a_sandbox_off(tmp_path):
    slots = _slots(tmp_path)
    (tmp_path / "fsout").write_text("")
    out = "N TCP CONNECTED\nN UDS CONNECTED\nN FSOUT ALLOWED\nN FSHOOK ALLOWED\n"
    t = _turn(tmp_path, bash_out=out, cmd="C", slots=slots, prompted={str(tmp_path / "s.txt")}, bash_prompt=True)
    (tmp_path / "s.txt").write_text("")
    st = _status(hc._evaluate(t, nonce="N", cmd="C", slots=slots, listener=_FakeListener(2),
                              landed=[tmp_path / "fsout"], scratch=tmp_path / "s.txt"))
    assert st["C2"] == st["C3"] == st["C4"] == "fail"


def test_evaluator_control_b_no_deny_write(tmp_path):
    slots = _slots(tmp_path)
    hook_probe = tmp_path / "hook-probe"
    hook_probe.write_text("")
    out = "N TCP BLOCKED\nN UDS BLOCKED\nN FSOUT BLOCKED\nN FSHOOK ALLOWED\n"
    t = _turn(tmp_path, bash_out=out, cmd="C", slots=slots, prompted={str(tmp_path / "s.txt")})
    (tmp_path / "s.txt").write_text("")
    st = _status(hc._evaluate(t, nonce="N", cmd="C", slots=slots, listener=_FakeListener(),
                              landed=[hook_probe], scratch=tmp_path / "s.txt"))
    assert st["C2"] == "fail" and st["C3"] == "fail" and st["C5"] == "pass"


def test_evaluator_control_c_host_allows_protected_write(tmp_path):
    slots = _slots(tmp_path)
    (tmp_path / "hk.txt").write_text("")
    (tmp_path / "s.txt").write_text("")
    t = _turn(tmp_path, bash_out=_BLOCKED, cmd="C", slots=slots,
              prompted={str(tmp_path / "hk.txt"), str(tmp_path / "s.txt")})
    st = _status(hc._evaluate(t, nonce="N", cmd="C", slots=slots, listener=_FakeListener(),
                              landed=[tmp_path / "hk.txt"], scratch=tmp_path / "s.txt"))
    assert st["C5"] == "fail" and st["C3"] == "fail"


def test_evaluator_altered_command_fails(tmp_path):
    slots = _slots(tmp_path)
    t = _turn(tmp_path, bash_out=_BLOCKED, cmd="printf 'N TCP BLOCKED'", slots=slots, prompted=set())
    st = _status(hc._evaluate(t, nonce="N", cmd="C", slots=slots, listener=_FakeListener(),
                              landed=[], scratch=tmp_path / "s.txt"))
    assert st["C2"] == "fail"


def test_evaluator_refusing_model_is_inconclusive_not_pass(tmp_path):
    t = hc._Turn(init=dict(_INIT), hook_events={"SessionStart", "UserPromptSubmit"})
    st = _status(hc._evaluate(t, nonce="N", cmd="C", slots=_slots(tmp_path), listener=_FakeListener(),
                              landed=[], scratch=tmp_path / "s.txt"))
    assert st["C2"] == "inconclusive" and st["C5"] == "inconclusive"


def test_evaluator_scratch_landing_without_a_prompt_fails(tmp_path):
    # the gate never fired: an allow rule or a mod approved the write before the host saw it
    slots = _slots(tmp_path)
    (tmp_path / "s.txt").write_text("")
    t = _turn(tmp_path, bash_out=_BLOCKED, cmd="C", slots=slots, prompted=set())
    st = _status(hc._evaluate(t, nonce="N", cmd="C", slots=slots, listener=_FakeListener(),
                              landed=[], scratch=tmp_path / "s.txt"))
    assert st["C5"] == "fail"


def test_evaluator_missing_hooks_fail(tmp_path):
    t = hc._Turn(init=dict(_INIT), hook_events={"SessionStart"})
    st = _status(hc._evaluate(t, nonce="N", cmd="C", slots=_slots(tmp_path), listener=_FakeListener(),
                              landed=[], scratch=tmp_path / "s.txt"))
    assert st["C6"] == "fail"


# ---------------------------------------------------------------------------- Codex


def test_codex_method_allowlist():
    for m in ("turn/start", "thread/resume", "hooks/list"):
        assert hg.broker_method_allowed(m)
    for m in ("thread/shellCommand", "command/exec", "fs/writeFile", "config/value/write",
              "thread/approveGuardianDeniedAction", "mcpServer/tool/call", "made/up"):
        assert not hg.broker_method_allowed(m)


def test_codex_policy_is_overwritten_not_forwarded():
    p = hg.overwrite_codex_policy("turn/start", {"threadId": "t", "sandboxPolicy": {"type": "dangerFullAccess"},
                                                 "permissionProfile": "x"})
    assert p["sandboxPolicy"] == hg.codex_sandbox_policy() and "permissionProfile" not in p
    assert hg.overwrite_codex_policy("thread/start", {"sandbox": "danger-full-access"})["sandbox"] == "workspace-write"


def test_codex_effective_policy_checks(tmp_path):
    good = {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False,
            "excludeTmpdirEnvVar": True, "excludeSlashTmp": True}
    assert hg.check_codex_effective_policy(good, forbidden=[tmp_path]) == []
    # the 10-06 capture's effective policy: /tmp and $TMPDIR writable
    assert hg.check_codex_effective_policy({**good, "excludeSlashTmp": False}, forbidden=[])
    assert hg.check_codex_effective_policy({"type": "readOnly"}, forbidden=[])  # network absent
    assert hg.check_codex_effective_policy({"type": "readOnly", "networkAccess": True}, forbidden=[])
    assert hg.check_codex_effective_policy({"type": "dangerFullAccess"}, forbidden=[])
    assert hg.check_codex_effective_policy({**good, "writableRoots": [str(tmp_path.parent)]}, forbidden=[tmp_path])


def test_codex_hooks_must_be_trusted():
    data = {"data": [{"cwd": "/e", "errors": [], "warnings": [], "hooks": [
        {"key": "k1", "sourcePath": "/h/hooks.json", "trustStatus": "untrusted"}]}]}
    assert hg.check_codex_hooks(data, ["/h/hooks.json"])
    data["data"][0]["hooks"][0]["trustStatus"] = "trusted"
    assert hg.check_codex_hooks(data, ["/h/hooks.json"]) == []
    assert hg.check_codex_hooks({"data": []}, ["/h/hooks.json"])


def test_codex_code_paths_refuse_todays_template(ent):
    # levain/templates/adapters/codex/hooks.json.template, rendered: hooks under the entity's own root
    roots = hg.cc_writable_roots(ent)
    hooks = {"hooks": {"SessionStart": [{"hooks": [{"type": "command",
             "command": f'"{sys.executable}" "{ent}/activation/hooks/session_start.py"'}]}]}}
    probs = hg.check_codex_code_paths(hooks, {}, roots)
    assert any("-I -B" in p for p in probs) and any("hook script" in p for p in probs)


def test_codex_code_paths_refuse_shell_composition(ent):
    hooks = {"hooks": {"Stop": [{"hooks": [{"command": "/usr/bin/python3 -I -B /safe/h.py; /w/pwn"}]}]}}
    assert any("shell syntax" in p for p in hg.check_codex_code_paths(hooks, {}, ()))


def test_codex_spawn_env():
    env = hg.codex_spawn_env("/c", {"PATH": "/bin", "OPENAI_API_KEY": "k"})
    assert env == {"PATH": "/bin", "CODEX_HOME": "/c"}
