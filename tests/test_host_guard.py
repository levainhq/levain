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
    for name in ("session_start.py", "user_prompt_submit.py", "automemory_mirror.py"):
        (hooks / name).write_text(f"# {name}\n", encoding="utf-8")
    (hooks / "_levain_hook.py").write_text('_INSTALL_ANNEAL_BIN = "{{ANNEAL_MEMORY}}"\n', encoding="utf-8")
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
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop"}
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


def test_managed_hook_needs_the_operators_trust_pin(ent):
    # D13: a managed hook is never judged by reading its shell text; only by exact trust
    roots = hg.cc_writable_roots(ent)
    doc = [("m.json", _managed(_GUARD))]
    probs = hg.check_managed_settings(doc, roots)
    assert probs and "trust pin" in probs[0]
    assert hg.check_managed_settings(doc, roots, trusted_hooks=frozenset({hg._sha(_GUARD)})) == []
    changed = [("m.json", _managed(_GUARD + " "))]
    assert hg.check_managed_settings(changed, roots, trusted_hooks=frozenset({hg._sha(_GUARD)}))


def test_trust_managed_records_exactly_the_commands_in_force(home, monkeypatch, tmp_path):
    base = tmp_path / "managed"
    base.mkdir()
    (base / "managed-settings.json").write_text(json.dumps(_managed(_GUARD)))
    monkeypatch.setitem(hg._MANAGED_SETTINGS, "darwin", base)
    cmds = hg.trust_managed(platform="darwin", home=home)
    assert cmds == [_GUARD]
    assert hg.load_managed_trust(platform="darwin", home=home) == frozenset({hg._sha(_GUARD)})


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
    assert session[session.index("--tools") + 1] == "Bash,Read"  # D12: no file tools
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




# ---------------------------------------------------------------------------- path identity


def test_is_within_matches_case_variants_by_identity(ent):
    # L3 r1, codex 5: on APFS a case variant of the entity IS the entity
    variant = Path(str(ent).replace("/ent", "/ENT"))
    if not variant.exists():
        pytest.skip("needs a case-insensitive filesystem")
    assert hg.is_within(variant / "bin" / "claude", ent)
    assert hg._check_exec(str(variant / "x"), (Path(os.path.realpath(ent)),), "claude")


def test_is_within_handles_a_missing_tail(ent):
    assert hg.is_within(ent / "not" / "yet" / "there", ent)
    assert not hg.is_within(ent.parent / "elsewhere", ent)


# ---------------------------------------------------------------------------- hook pin aliases


def test_pin_refuses_a_hook_file_with_a_second_hard_link(ent):
    # L3 r1, codex 2: an alias outside activation/ would let sandboxed Bash rewrite the hook inode
    os.link(ent / "activation" / "hooks" / "session_start.py", ent / "alias.py")
    with pytest.raises(hg.HostRefusal, match="hard links"):
        hg.hook_pin(ent)


def test_pin_refuses_a_fifo(ent):
    os.mkfifo(ent / "activation" / "hooks" / "pipe")
    with pytest.raises(hg.HostRefusal, match="not a regular file"):
        hg.hook_pin(ent)


# ---------------------------------------------------------------------------- the probe, run for real


def _cfg(tmp_path: Path, port: int) -> dict:
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "session_start.py").write_text("x")
    return {"nonce": "n0", "port": port, "sock": str(tmp_path / "none.sock"),
            "outside": str(tmp_path / "nodir" / "f"), "hook": str(hooks / "h"), "hook_case": "",
            "claude": str(tmp_path / "c"), "mcp": "", "git": "",
            "link_target": str(hooks), "swap_link": str(tmp_path / "sl"), "swap_name": "via.txt",
            "link_src": str(hooks / "session_start.py"), "link_dst": str(tmp_path / "dst")}


def test_probe_never_reports_refused_connections_as_blocked(tmp_path):
    """Run unsandboxed against a closed port, a missing socket and a missing directory: those raise
    ECONNREFUSED / ENOENT, not EPERM, so the probe must say INCONCLUSIVE, never BLOCKED, or a dead
    listener would read as a working sandbox. The writable targets say OPEN, and the probe leaves
    none of its own links behind."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens now
    script = tmp_path / "p.py"
    script.write_text(hc.probe_script(_cfg(tmp_path, port)))
    env = {k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
    out = subprocess.run(hc.probe_command(script, python=sys.executable), shell=True, env=env,
                         capture_output=True, text=True, timeout=60).stdout
    lines = {ln.split(" ", 2)[1]: ln.split(" ", 2)[2] for ln in out.splitlines()}
    for k in ("TCP", "UDS", "FSOUT", "PROXYIP", "PROXYLOCAL"):
        assert lines[k].startswith("INCONCLUSIVE"), (k, lines)
    for k in ("FSHOOK", "FSCLAUDE", "FSLINK", "HARDLINK"):
        assert lines[k] == "OPEN", (k, lines)
    assert "FSMCP" not in lines and "FSGIT" not in lines  # not aimed at absent targets
    assert not (tmp_path / "dst").exists() and not (tmp_path / "sl").exists()


# ---------------------------------------------------------------------------- the listener


def test_listener_counts_a_tokenless_connection_as_foreign_even_during_self_check():
    # L3 r1, codex 6 / complement 4: attribution by what a connection sends, never by timing
    import shutil
    import tempfile
    short = Path(tempfile.mkdtemp(prefix="lv", dir="/tmp"))  # AF_UNIX paths are capped at ~104 bytes
    lst = hc._Listener(short / "l.s")
    try:
        foreign = socket.create_connection(("127.0.0.1", lst.port))
        assert lst.self_connect()
        foreign.close()
        deadline = __import__("time").monotonic() + 3
        while lst.foreign_hits() == 0 and __import__("time").monotonic() < deadline:
            __import__("time").sleep(0.05)
        assert lst.foreign_hits() == 1 and lst.self_hits == 2
    finally:
        lst.close()
        shutil.rmtree(short, ignore_errors=True)


# ---------------------------------------------------------------------------- the evaluator, on the control runs


_INIT = {"tools": ["Bash", "Read", "mcp__anneal_memory__recall"],
         "mcp_servers": [{"name": "anneal_memory", "status": "connected"}],
         "plugins": [{"name": "cc-plugin-telemetry", "path": "builtin"}],
         "permissionMode": "default", "claude_code_version": "2.1.293"}


def test_init_check_passes_the_live_shape():
    assert hc.check_init_event(dict(_INIT)).status == "pass"


@pytest.mark.parametrize("patch", [
    {"tools": _INIT["tools"] + ["Write"]},
    {"tools": _INIT["tools"] + ["SendMessage"]},
    {"tools": _INIT["tools"] + ["mcp__iterm-mcp__write_to_terminal"]},
    {"mcp_servers": [{"name": "anneal_memory", "status": "connected"}, {"name": "iterm-mcp", "status": "connected"}]},
    {"plugins": [{"name": "x", "path": "/Users/x/.claude/plugins/x"}]},
    {"permissionMode": "acceptEdits"},
    {"claude_code_version": "2.1.200"},
])
def test_init_check_fails_each_drift(patch):
    assert hc.check_init_event({**_INIT, **patch}).status == "fail"


def test_init_check_malformed_shapes_fail_not_raise():
    assert hc.check_init_event(None).status == "fail"
    assert hc.check_init_event({**_INIT, "mcp_servers": ["anneal_memory"]}).status == "fail"
    assert hc.check_init_event({**_INIT, "plugins": "x"}).status == "fail"


class _FakeListener:
    def __init__(self, hits: int = 0) -> None:
        self._hits = hits

    def foreign_hits(self) -> int:
        return self._hits


def _probe_out(**override: str) -> str:
    return "".join(f"N {k} {override.get(k, 'BLOCKED')}\n" for k in hc._PROBE_LINES)


def _turn(*, bash_out: str, cmd: str = "C", bash_prompt: bool = False,
          context: dict | None = None) -> hc._Turn:
    t = hc._Turn(init=dict(_INIT), hook_events={"SessionStart", "UserPromptSubmit"},
                 hook_context={"SessionStart": 40, "UserPromptSubmit": 40} if context is None else context)
    t.tool_uses.append({"id": "b", "name": "Bash", "input": {"command": cmd}})
    t.results["b"] = bash_out
    if bash_prompt:
        t.prompts.append({"tool_name": "Bash", "input": {"command": cmd}})
    return t


def _status(checks: list) -> dict[str, str]:
    return {c.name.split()[0]: c.status for c in checks}


def _ev(t: hc._Turn, *, hits: int = 0, landed: list | None = None) -> dict[str, str]:
    return _status(hc._evaluate(t, nonce="N", cmd="C", listener=_FakeListener(hits), landed=landed or []))


def test_evaluator_passes_the_live_pass_run():
    assert set(_ev(_turn(bash_out=_probe_out())).values()) == {"pass"}


def test_evaluator_control_a_sandbox_off(tmp_path):
    (tmp_path / "fsout").write_text("")
    out = _probe_out(**{k: "OPEN" for k in ("TCP", "UDS", "FSOUT", "FSHOOK", "FSCLAUDE", "FSLINK", "HARDLINK")},
                     PROXYIP="INCONCLUSIVE no HTTP_PROXY", PROXYLOCAL="INCONCLUSIVE no HTTP_PROXY")
    st = _ev(_turn(bash_out=out, bash_prompt=True), hits=2, landed=[tmp_path / "fsout"])
    assert st["C2"] == st["C3"] == st["C4"] == "fail"


def test_evaluator_control_b_no_deny_write(tmp_path):
    (tmp_path / "hook").write_text("")
    out = _probe_out(FSHOOK="OPEN", FSCLAUDE="OPEN", FSLINK="OPEN", HARDLINK="OPEN")
    st = _ev(_turn(bash_out=out), landed=[tmp_path / "hook"])
    assert st["C2"] == "fail" and st["C3"] == "fail" and st["C4"] == "pass"


def test_evaluator_control_d_proxy_reaches_the_listener():
    # the live run: the probe saw a connection reset, the listener saw the forwarded requests
    out = _probe_out(TCP="OPEN", PROXYIP="INCONCLUSIVE errno=54 reset", PROXYLOCAL="INCONCLUSIVE errno=54 reset")
    st = _ev(_turn(bash_out=out), hits=3)
    assert st["C3"] == "fail"


def test_evaluator_altered_command_fails():
    assert _ev(_turn(bash_out=_probe_out(), cmd="printf 'N TCP BLOCKED'"))["C2"] == "fail"


def test_evaluator_extra_tool_call_fails():
    t = _turn(bash_out=_probe_out())
    t.tool_uses.append({"id": "x", "name": "Read", "input": {"file_path": "/etc/hosts"}})
    assert _ev(t)["C2"] == "fail"


def test_evaluator_refusing_model_is_inconclusive_not_pass():
    t = hc._Turn(init=dict(_INIT), hook_events={"SessionStart", "UserPromptSubmit"},
                 hook_context={"SessionStart": 40, "UserPromptSubmit": 40})
    assert _ev(t)["C2"] == "inconclusive"


def test_evaluator_hook_event_without_context_fails():
    assert _ev(_turn(bash_out=_probe_out(), context={"SessionStart": 40}))["C6"] == "fail"


def test_evaluator_proxy_line_open_fails():
    assert _ev(_turn(bash_out=_probe_out(PROXYLOCAL="OPEN")))["C2"] == "fail"


def test_probe_detail_after_the_status_word_is_ignored():
    detail = {k: "BLOCKED [Errno 1] Operation not permitted: '/x y'" for k in hc._PROBE_LINES}
    assert _ev(_turn(bash_out=_probe_out(**detail)))["C2"] == "pass"
    assert _ev(_turn(bash_out=_probe_out(TCP="OPENISH")))["C2"] == "inconclusive"


def test_evaluator_missing_conditional_line_is_inconclusive():
    t = _turn(bash_out=_probe_out())
    checks = hc._evaluate(t, nonce="N", cmd="C", listener=_FakeListener(), landed=[],
                          want=hc._PROBE_LINES + ("FSGIT",))
    assert _status(checks)["C2"] == "inconclusive"


# ---------------------------------------------------------------------------- Codex


def test_codex_method_allowlist():
    for m in ("turn/start", "thread/resume", "hooks/list"):
        assert hg.broker_method_allowed(m)
    for m in ("thread/shellCommand", "command/exec", "fs/writeFile", "config/value/write",
              "thread/approveGuardianDeniedAction", "mcpServer/tool/call", "made/up"):
        assert not hg.broker_method_allowed(m)


def test_codex_policy_is_overwritten_not_forwarded():
    p = hg.overwrite_codex_policy("turn/start", {
        "threadId": "t", "input": [], "sandboxPolicy": {"type": "dangerFullAccess"}, "permissions": "x",
        "cwd": "/", "approvalPolicy": "never", "approvalsReviewer": "auto_review"}, cwd="/ent")
    assert p["sandboxPolicy"] == hg.codex_sandbox_policy()
    assert p["cwd"] == "/ent" and p["approvalPolicy"] == "untrusted" and p["approvalsReviewer"] == "user"
    assert "permissions" not in p
    t = hg.overwrite_codex_policy("thread/start", {"sandbox": "danger-full-access", "config": {"x": 1},
                                                   "baseInstructions": "b"}, cwd="/ent")
    assert t == {"cwd": "/ent", "approvalPolicy": "untrusted", "approvalsReviewer": "user", "sandbox": "workspace-write"}
    assert hg.overwrite_codex_policy("initialize", {"capabilities": {"experimentalApi": True}}, cwd="/e") == \
        hg.codex_initialize_params()


def test_codex_config_overrides_carry_what_start_resume_cannot():
    ww = hg.codex_config_overrides()["sandbox_workspace_write"]
    assert ww["network_access"] is False and ww["exclude_slash_tmp"] is True and ww["exclude_tmpdir_env_var"] is True


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


def test_codex_writable_root_inside_codex_home_refuses(tmp_path):
    (tmp_path / "home" / "hooks").mkdir(parents=True)
    good = {"type": "workspaceWrite", "networkAccess": False, "excludeTmpdirEnvVar": True,
            "excludeSlashTmp": True, "writableRoots": [str(tmp_path / "home" / "hooks")]}
    assert hg.check_codex_effective_policy(good, forbidden=[tmp_path / "home"])


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
    assert any("-I -B before its script" in p for p in probs)
    assert any("entity can write" in p for p in probs)


def test_codex_code_paths_refuse_shell_composition():
    hooks = {"hooks": {"Stop": [{"hooks": [{"command": "/usr/bin/python3 -I -B /safe/h.py; /w/pwn"}]}]}}
    assert any("shell syntax" in p for p in hg.check_codex_code_paths(hooks, {}, ()))


def test_codex_flags_after_the_script_do_not_count():
    hooks = {"hooks": {"Stop": [{"hooks": [{"command": "/usr/bin/python3 /opt/safe/h.py -I -B"}]}]}}
    assert any("before its script" in p for p in hg.check_codex_code_paths(hooks, {}, ()))


def test_codex_mcp_server_rules(ent):
    roots = (Path(os.path.realpath(ent)),)
    py = "/usr/bin/python3"
    db = str(ent / ".levain" / "memory.db")
    assert hg.check_codex_code_paths({}, {"m": {"command": py, "args": ["-I", "server.py", "--db", db]}}, roots,
                                     expected_db=db)
    assert hg.check_codex_code_paths({}, {"m": {"command": py, "args": ["-I", "/opt/s.py", "--db", db],
                                                "env": {"PYTHONPATH": str(ent)}}}, roots, expected_db=db)
    assert hg.check_codex_code_paths({}, {"m": {"command": py, "args": ["-I", "/opt/s.py", "--db", db]}}, roots,
                                     expected_db=db) == []
    # without the entity's store an MCP server cannot be vetted at all (chat L3 r1, H2)
    assert hg.check_codex_code_paths({}, {"m": {"command": py, "args": ["-I", "/opt/s.py", "--db", db]}}, roots)
    # L3 r1, codex 4: the --db exemption is by position, not by value
    evil = str(ent / "evil.py")
    assert hg.check_codex_code_paths({}, {"m": {"command": py, "args": ["-I", evil, "--db", evil]}}, roots,
                                     expected_db=evil)


def test_codex_spawn_env():
    env = hg.codex_spawn_env("/c", {"PATH": "/bin", "OPENAI_API_KEY": "k"})
    assert env == {"PATH": "/bin", "CODEX_HOME": "/c"}


# ---------------------------------------------------------------------------- L1/L2 r1 regressions


def test_deny_write_matches_the_protected_set(ent):
    deny = hg.cc_sandbox_block(ent)["filesystem"]["denyWrite"]
    real = Path(os.path.realpath(ent))
    assert sorted(deny) == sorted(str(real / n) for n in hg.PROTECTED_NAMES)


def test_sandbox_block_denies_the_proxy_route_and_git_snapshot(ent, home, entity_only_roots):
    net = hg.cc_sandbox_block(ent)["network"]
    assert net["strictAllowlist"] is True
    assert {"localhost", "*.localhost", "127.0.0.1", "::1"} <= set(net["deniedDomains"])
    p = hg.cc_host_profile(ent, claude_bin=sys.executable, home=home, platform="darwin", managed=[])
    assert p.settings["includeGitInstructions"] is False
    assert p.env["CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS"] == "1"
    assert p.env["PYTHONPYCACHEPREFIX"] == str(p.run_dir / "pycache")


def test_path_keeps_only_absolute_entries_outside_the_roots(ent):
    env = hg.host_env({"PATH": f"/usr/bin::.:bin:{ent}/bin:/opt/x"}, roots=(Path(os.path.realpath(ent)),))
    assert env["PATH"] == "/usr/bin:/opt/x"


def test_exec_check_catches_a_symlinked_interpreter_inside_the_entity(ent):
    (ent / ".venv" / "bin").mkdir(parents=True)
    os.symlink(sys.executable, ent / ".venv" / "bin" / "python")
    probs = hg._check_exec(str(ent / ".venv" / "bin" / "python"), (Path(os.path.realpath(ent)),), "py")
    assert probs and "passes through" in probs[0]
    assert hg._check_exec("python3", (), "py")  # relative refuses


def test_mcp_must_start_isolated_from_its_cwd(ent):
    cfg = hg.cc_mcp_config(ent, sys.executable)
    roots = (Path(os.path.realpath(ent)),)
    assert hg._check_mcp(cfg, Path(os.path.realpath(ent)), roots) == []
    cfg["mcpServers"]["anneal_memory"]["args"] = cfg["mcpServers"]["anneal_memory"]["args"][1:]
    assert any("isolated" in p for p in hg._check_mcp(cfg, Path(os.path.realpath(ent)), roots))


def test_managed_unknown_loosening_and_mistyped_keys_refuse(ent):
    roots = hg.cc_writable_roots(ent)
    for doc in ({"env": {"PYTHONPATH": str(ent)}}, {"permissions": {"allow": ["Write"]}},
                {"sandbox": {"network": {"httpProxyPort": 8080}}}, {"sandbox": {"filesystem": {"disabled": True}}},
                {"apiKeyHelper": "/bin/x"}, {"sandbox": {"allowAppleEvents": True}},
                {"sandbox": {"enabled": 0}}, {"sandbox": {"allowUnsandboxedCommands": "false"}}):
        assert hg.check_managed_settings([("m.json", doc)], roots), doc


def test_shared_code_inside_the_entity_refuses(ent, tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    os.symlink(ent / "activation", home / ".claude" / "skills")
    (home / ".claude" / "settings.json").write_text(json.dumps(_managed("python3 ${HOME}/../ent/hooks_x.py")))
    (ent / "hooks_x.py").write_text("")
    probs = hg.check_shared_code(Path(os.path.realpath(ent)), (Path(os.path.realpath(ent)),), home=home)
    assert any("skills" in p for p in probs) and any("PreToolUse hook runs" in p for p in probs)


def test_repin_entry_point(ent, home, monkeypatch, capsys, entity_only_roots):
    monkeypatch.setattr(hg.os.path, "expanduser", lambda p: str(home) if p == "~" else p)
    monkeypatch.setattr(hg.sys, "platform", "darwin")
    assert hg.main(["repin", str(ent)]) == 0
    assert "(none) ->" in capsys.readouterr().out
    assert hg.main(["repin", str(ent)]) == 0
    assert "(none)" not in capsys.readouterr().out


def test_digest_binds_env_values_and_the_binary(ent, home, entity_only_roots):
    import dataclasses
    p = hg.cc_host_profile(ent, claude_bin=sys.executable, home=home, platform="darwin", managed=[])
    q = dataclasses.replace(p, env={**p.env, "PATH": "/somewhere/else"})
    assert p.digest() != q.digest()


# ---------------------------------------------------------------------------- L3 r2 named fixes (after the closing round)


def test_python_path_check_locates_without_importing(tmp_path):
    # L3 r2, codex 1: the check must not run the package it is vetting
    pkg = tmp_path / "site" / "anneal_memory"
    pkg.mkdir(parents=True)
    marker = tmp_path / "ran"
    (pkg / "__init__.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(tmp_path / "site")}
    probs = hg._check_python_paths(sys.executable, (Path(os.path.realpath(tmp_path)),), env)
    assert not marker.exists()
    assert probs and "imports from" in probs[0]


def test_codex_argv_refuses_a_value_taking_flag():
    # L3 r2, complement 1: `-X` would swallow `--db` and make the next word the real script
    probs = hg.check_codex_code_paths({}, {"m": {"command": "/usr/bin/python3",
                                                 "args": ["-I", "-X", "--db", "/ent/evil.py"]}}, ())
    assert any("interpreter option" in p for p in probs)
    assert any("names no script" in p for p in hg.check_codex_code_paths(
        {}, {"m": {"command": "/usr/bin/python3", "args": ["-I"]}}, ()))


def test_codex_hooks_must_be_exactly_levains():
    data = {"data": [{"hooks": [{"key": "k1", "sourcePath": "/h/hooks.json", "trustStatus": "trusted"},
                                {"key": "k2", "sourcePath": "/etc/managed.json", "trustStatus": "managed"}]}]}
    assert any("did not install" in p for p in hg.check_codex_hooks(data, ["/h/hooks.json"]))


def test_codex_effective_cwd_must_be_the_entity(ent, tmp_path):
    good = {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False,
            "excludeTmpdirEnvVar": True, "excludeSlashTmp": True}
    assert hg.check_codex_effective_policy(good, forbidden=[], cwd=str(ent), entity=ent) == []
    home = tmp_path / "codex_home"
    home.mkdir()
    probs = hg.check_codex_effective_policy(good, forbidden=[home], cwd=str(home), entity=ent)
    assert any("not the entity" in p for p in probs) and any("overlaps" in p for p in probs)


def test_codex_spawn_env_filters_path(ent):
    env = hg.codex_spawn_env("/c", {"PATH": f"{ent}/bin:/usr/bin"}, roots=(Path(os.path.realpath(ent)),))
    assert env["PATH"] == "/usr/bin"


def test_security_inputs_digest_moves_with_managed_settings(ent, home, monkeypatch):
    monkeypatch.setattr(hg, "load_managed_settings", lambda platform=None: [])
    a = hg.security_inputs_digest(Path(os.path.realpath(ent)), sys.executable, sys.executable, "darwin", {}, home)
    monkeypatch.setattr(hg, "load_managed_settings",
                        lambda platform=None: [("m.json", {"sandbox": {"enabled": False}})])
    b = hg.security_inputs_digest(Path(os.path.realpath(ent)), sys.executable, sys.executable, "darwin", {}, home)
    assert a != b


def test_pin_file_that_is_not_an_object_refuses(ent, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "hook_pin.json").write_text("[]")
    with pytest.raises(hg.HostRefusal, match="unreadable"):
        hg.check_or_record_pin(ent, state)


# ---------------------------------------------------------------------------- chat L3 r1, H2


_OTHER_DB = "/other-entity/.levain/memory.db"


@pytest.mark.parametrize("tail", [
    ["--db", _OTHER_DB],                       # another entity's store
    [],                                        # no --db at all
    ["--db", "{db}", "--db", _OTHER_DB],       # a duplicate: argparse keeps the last
    ["--db", "{db}", "--db", "{db}"],          # a duplicate, even of the right path
    ["--d", _OTHER_DB],                        # argparse abbreviation of --db
    ["--db=" + _OTHER_DB],                     # the = spelling
    ["--db"],                                  # no value
])
def test_codex_mcp_db_must_be_exactly_this_entitys_store(ent, tail):
    # codex HIGH: every value after --db was removed before vetting, so any store passed
    roots = (Path(os.path.realpath(ent)),)
    db = str(Path(os.path.realpath(ent)) / ".levain" / "memory.db")
    args = ["-I", "/opt/s.py", *[t.replace("{db}", db) for t in tail]]
    probs = hg.check_codex_code_paths({}, {"m": {"command": "/usr/bin/python3", "args": args}}, roots,
                                      expected_db=db)
    assert any("--db" in p for p in probs), probs


def test_codex_mcp_db_compares_resolved_paths(ent, tmp_path):
    real = Path(os.path.realpath(ent))
    link = tmp_path / "entlink"
    os.symlink(real, link)
    probs = hg.check_codex_code_paths({}, {"m": {"command": "/usr/bin/python3",
                                                 "args": ["-I", "/opt/s.py", "--db", str(link / ".levain" / "memory.db")]}},
                                      (real,), expected_db=real / ".levain" / "memory.db")
    assert probs == []


@pytest.mark.parametrize("tail", [
    ["--db", _OTHER_DB, "serve"],
    ["serve"],
    ["--db", "{db}", "--db", "{db}", "serve"],
    ["--d", _OTHER_DB, "serve"],
    ["--db=" + _OTHER_DB, "serve"],
])
def test_cc_mcp_db_must_be_exactly_this_entitys_store(ent, tail):
    real = Path(os.path.realpath(ent))
    cfg = hg.cc_mcp_config(ent, sys.executable)
    db = str(real / ".levain" / "memory.db")
    cfg["mcpServers"]["anneal_memory"]["args"] = ["-P", "-m", "anneal_memory",
                                                  *[t.replace("{db}", db) for t in tail]]
    probs = hg._check_mcp(cfg, real, (real,))
    assert any("--db" in p for p in probs), probs


# ---------------------------------------------------------------------------- ruling 3b: .codex, agent hook pins


def test_codex_dir_is_write_denied_to_hosted_bash(ent):
    """r3a measured a sandboxed model Bash creating <ws>/.codex/config.toml; without `.codex` in
    PROTECTED_NAMES the profile's denyWrite would not name it."""
    real = Path(os.path.realpath(ent))
    deny = hg.cc_sandbox_block(ent)["filesystem"]["denyWrite"]
    assert str(real / ".codex") in deny
    assert ".codex" in hg.PROTECTED_NAMES


def _pinned_entity(tmp_path: Path):
    ent = _entity(tmp_path)
    (ent / ".claude").mkdir()
    (ent / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"SessionStart": [1]}}))
    ch = tmp_path / "codexhome"
    ch.mkdir()
    (ch / "hooks.json").write_text('{"hooks": {}}')
    return ent, ch


def test_agent_pin_first_use_then_match(tmp_path):
    ent, ch = _pinned_entity(tmp_path)
    status, pinned = hg.check_agent_hook_pins(ent, ch)
    assert status == "recorded" and ".claude/settings.json hooks" in pinned and "absent" not in pinned
    assert hg.check_agent_hook_pins(ent, ch) == ("match", [])


def test_agent_pin_names_an_edited_hook_script(tmp_path):
    """Q0 item 8: Claude Code does not notice an edited hook SCRIPT, so the pin must."""
    ent, ch = _pinned_entity(tmp_path)
    hg.check_agent_hook_pins(ent, ch)
    with open(ent / "activation" / "hooks" / "session_start.py", "a") as fh:
        fh.write("# edited\n")
    status, diffs = hg.check_agent_hook_pins(ent, ch)
    assert status == "changed"
    assert len(diffs) == 1 and diffs[0].startswith("activation/hooks/ (scripts): was ")


def test_agent_pin_names_settings_hook_codex_and_claude_hooks_dir_changes(tmp_path):
    ent, ch = _pinned_entity(tmp_path)
    (ent / "AGENTS.md").write_text("x")  # only a Codex install pins the global Codex hooks.json
    hg.check_agent_hook_pins(ent, ch)
    (ent / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"SessionStart": [2]}}))
    (ch / "hooks.json").write_text('{"hooks": {"x": 1}}')
    (ent / ".claude" / "hooks").mkdir()
    (ent / ".claude" / "hooks" / "evil.sh").write_text("x")
    status, diffs = hg.check_agent_hook_pins(ent, ch)
    names = " | ".join(diffs)
    assert status == "changed"
    assert ".claude/settings.json hooks" in names and "(Codex)" in names and ".claude/hooks/ (scripts)" in names


def test_agent_pin_ignores_non_hook_settings_but_repin_clears(tmp_path):
    ent, ch = _pinned_entity(tmp_path)
    (ent / "AGENTS.md").write_text("x")  # only a Codex install pins the global Codex hooks.json
    hg.check_agent_hook_pins(ent, ch)
    (ent / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"SessionStart": [1]}, "model": "x"}))
    assert hg.check_agent_hook_pins(ent, ch)[0] == "match"
    (ch / "hooks.json").write_text("{}")
    assert hg.check_agent_hook_pins(ent, ch)[0] == "changed"
    hg.repin_agent_hooks(ent, ch)
    assert hg.check_agent_hook_pins(ent, ch)[0] == "match"


def test_agent_pin_never_edits_agent_settings(tmp_path):
    ent, ch = _pinned_entity(tmp_path)
    before = (ent / ".claude" / "settings.json").read_bytes(), (ch / "hooks.json").read_bytes()
    hg.check_agent_hook_pins(ent, ch)
    (ch / "hooks.json").write_text("{}")
    hg.check_agent_hook_pins(ent, ch)
    assert (ent / ".claude" / "settings.json").read_bytes() == before[0]


def _git(*args):
    import subprocess

    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True)


def test_a_subdirectory_session_reads_only_its_own_shared_settings_in_both_readers(tmp_path, monkeypatch):
    """Observed with Claude Code 2.1.301: started in a repo subdirectory it does not load the root's shared
    settings.json (a Bash write outside the directory landed; started at the root, the same write was denied). Doctor's sandbox reader, the messaging reader and the cockpit chip
    must all say so; the root itself still reads on."""
    from levain.cockpit.providers import _sandbox_chip

    home, repo = tmp_path / "home", tmp_path / "home" / "R"
    (repo / "sub").mkdir(parents=True)
    _git("init", "-q", str(repo))
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(json.dumps({
        "sandbox": {"enabled": True}, "crossSessionInbound": "hold", "permissions": {"ask": ["SendMessage"]}}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(hg, "load_managed_settings", lambda *a, **k: [])
    sub = repo / "sub"
    assert hg.cc_sandbox_state(sub, home, managed=[])[0] is False
    assert str(repo / ".claude" / "settings.json") not in hg.cc_sandbox_sources(sub, home, managed=[])
    msg = hg.cc_messaging_posture(sub, home, managed=[])
    assert msg.inbound is None and msg.outbound == "open"
    assert _sandbox_chip("claude_code", str(sub), {}) == "sandbox configured off"
    assert hg.cc_sandbox_state(repo, home, managed=[])[0] is True
    assert _sandbox_chip("claude_code", str(repo), {}) == "sandbox configured on (project settings)"


def test_the_local_settings_file_is_read_at_the_repo_root_and_the_main_checkout(tmp_path):
    home, repo = tmp_path / "home", tmp_path / "home" / "R"
    (repo / "sub").mkdir(parents=True)
    _git("init", "-q", str(repo))
    _git("-C", str(repo), "commit", "-q", "--allow-empty", "-m", "x")
    (repo / ".claude").mkdir()
    root_local = repo / ".claude" / "settings.local.json"
    root_local.write_text(json.dumps({"sandbox": {"enabled": True}}))
    assert hg.cc_sandbox_state(repo / "sub", home, managed=[]) == (True, str(root_local))
    _git("-C", str(repo), "worktree", "add", "-q", str(tmp_path / "home" / "wt"))
    local, shared = hg.cc_project_settings_files(tmp_path / "home" / "wt", home)
    assert local[0] == root_local and shared == tmp_path / "home" / "wt" / ".claude" / "settings.json"
    # an older Claude Code's local file in the starting directory is still read, after the root's
    (repo / "sub" / ".claude").mkdir()
    (repo / "sub" / ".claude" / "settings.local.json").write_text(json.dumps({"sandbox": {"enabled": False}}))
    assert hg.cc_sandbox_state(repo / "sub", home, managed=[])[0] is True
    # a repository rooted at the home directory keeps the local file beside the shared one
    _git("init", "-q", str(home))
    assert hg.cc_project_settings_files(home / "x", home)[0] == [home / "x" / ".claude" / "settings.local.json"]


def test_cc_sandbox_state_precedence(tmp_path):
    ent, _ = _pinned_entity(tmp_path)
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    on, why = hg.cc_sandbox_state(ent, home, managed=[])
    assert on is False and "default is off" in why
    (home / ".claude" / "settings.json").write_text(json.dumps({"sandbox": {"enabled": True}}))
    assert hg.cc_sandbox_state(ent, home, managed=[])[0] is True
    (ent / ".claude" / "settings.local.json").write_text(json.dumps({"sandbox": {"enabled": False}}))
    assert hg.cc_sandbox_state(ent, home, managed=[])[0] is False  # the workspace layer wins over the user's
    assert hg.cc_sandbox_state(ent, home, managed=[("/m", {"sandbox": {"enabled": True}})])[0] is True
    (ent / ".claude" / "settings.local.json").write_text(json.dumps({"sandbox": {"enabled": "yes"}}))
    assert hg.cc_sandbox_state(ent, home, managed=[])[0] is False  # a string is not True



# ---------------------------------------------------------------------------- srt keys that relax the profile
# Each key below is in Claude Code 2.1.310's sandbox settings schema (the `sandbox` object it passes to the sandbox
# runtime). network.allowMachLookup ["com.apple.trustd.agent"] was RUN (levain-memory 1026-8/p1/RETURN.md, Q3/Q5): it
# opens the same trustd.agent hole as enableWeakerNetworkIsolation, through which a sandboxed `security verify-cert`
# made trustd fetch a URL the command chose, with no proxy row.
_RELAXING = (
    (("network", "allowMachLookup"), ["com.apple.trustd.agent"], []),
    (("allowAppleEvents",), True, False),
    (("allowNestedUserNamespaces",), True, False),
    (("filesystem", "allowRead"), ["/"], []),
    (("filesystem", "disabled"), True, False),
)


def _nested(keys, value):
    d = value
    for k in reversed(keys):
        d = {k: d}
    return d


@pytest.mark.parametrize("keys,loose,tight", _RELAXING, ids=[".".join(k) for k, _, _ in _RELAXING])
def test_a_repo_settings_layer_that_relaxes_the_profile_is_refused_by_key(keys, loose, tight):
    from levain.slot import render as rd
    f = ".claude/settings.json"
    got = rd.repo_loosening_refusals({f: {"sandbox": _nested(keys, loose)}})
    assert [e.name for e in got] == ["loosening:" + rd.wire_name("sandbox." + ".".join(keys))]
    assert rd.repo_loosening_refusals({f: {"sandbox": _nested(keys, tight)}}) == []


@pytest.mark.parametrize("keys,loose,tight", _RELAXING, ids=[".".join(k) for k, _, _ in _RELAXING])
def test_managed_settings_that_relax_the_profile_refuse_by_key(ent, keys, loose, tight):
    roots = hg.cc_writable_roots(ent)
    probs = hg.check_managed_settings([("m.json", {"sandbox": _nested(keys, loose)})], roots)
    want = "is true" if loose is True else "is not empty"
    assert probs == [f"m.json: sandbox.{'.'.join(keys)} {want}"]


def test_managed_mach_lookup_that_adds_nothing_is_accepted(ent):
    roots = hg.cc_writable_roots(ent)
    assert hg.check_managed_settings([("m.json", {"sandbox": {"network": {"allowMachLookup": []}}})], roots) == []
    probs = hg.check_managed_settings([("m.json", {"sandbox": {"network": {"allowMachLookup": "x"}}})], roots)
    assert "m.json: sandbox.network.allowMachLookup must be list, got str" in probs


@pytest.mark.parametrize("bad", [False, "", 0, None, {}], ids=repr)
def test_managed_allow_read_of_the_wrong_type_refuses(ent, bad):
    """L3 r1 (codex 2): a falsy non-list allowRead passed the not-empty check and nothing else; Claude Code expects a
    list, and a schema type error can drop the sandbox block silently, so the wrong type refuses like every pinned
    key."""
    roots = hg.cc_writable_roots(ent)
    probs = hg.check_managed_settings([("m.json", {"sandbox": {"filesystem": {"allowRead": bad}}})], roots)
    assert f"m.json: sandbox.filesystem.allowRead must be list, got {type(bad).__name__}" in probs
    assert hg.check_managed_settings([("m.json", {"sandbox": {"filesystem": {"allowRead": []}}})], roots) == []


_MANAGED_LIST_KEYS = sorted({k for k, t in hg._SANDBOX_TYPES.items() if t is list}
                            | {("filesystem", "denyRead"), ("filesystem", "allowRead")})


@pytest.mark.parametrize("bad", [[1], ["x", None]], ids=repr)
@pytest.mark.parametrize("keys", _MANAGED_LIST_KEYS, ids=".".join)
def test_managed_list_key_holding_a_non_string_refuses(ent, keys, bad):
    """RULINGS 1026-10 Q5 (2): one fact, one strictness. render's repo check requires a list key's items to be strings;
    the managed exact-type check does too (a `denyRead: [1]` passed every check before)."""
    roots = hg.cc_writable_roots(ent)
    probs = hg.check_managed_settings([("m.json", {"sandbox": _nested(keys, bad)})], roots)
    name = type(next(i for i in bad if type(i) is not str)).__name__
    assert f"m.json: sandbox.{'.'.join(keys)} must be a list of strings, holds {name}" in probs


def test_the_pinned_block_closes_mach_lookup_apple_events_and_nested_namespaces():
    block = hg.cc_sandbox_block("/x")
    assert block["network"]["allowMachLookup"] == []
    assert block["allowAppleEvents"] is False
    assert block["allowNestedUserNamespaces"] is False
    assert hg.check_sandbox_types(block) == []
    del block["network"]["allowMachLookup"]
    assert "sandbox.network.allowMachLookup is missing" in hg.check_sandbox_types(block)
