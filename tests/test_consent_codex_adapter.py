"""Codex's PermissionRequest hook on the consent queue (Q3). The decision path is the Claude Code adapter's (its
tests); what is Codex's own here is asking Codex itself whether it trusts the installed hook. The real
``codex app-server`` exits at stdin EOF without answering (measured, Q3 RUN), so a fake with that behaviour stands
in for it.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from levain.authority import CONSENT_SHELL_TOOLS
from levain.consent.adapters import codex as cx
from tests._h4 import h4_env

FAKE_CODEX = r'''#!{python}
import json, os, sys, threading, time
status = {status!r}
enabled = {enabled!r}
def answer(msg):  # answered asynchronously, a moment later, as codex app-server does
    time.sleep(0.5)
    hooks = [{{"key": {key!r}, "eventName": "permissionRequest", "handlerType": "command",
               "command": {command!r}, "currentHash": "sha256:cur", "trustStatus": status,
               "enabled": enabled}}]
    print(json.dumps({{"id": msg["id"], "result": {{"data": [{{"cwd": msg["params"]["cwds"][0],
                                                                 "hooks": hooks if status else []}}]}}}}), flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("id") == 1:
        print(json.dumps({{"id": 1, "result": {{}}}}), flush=True)
    elif msg.get("method") == "hooks/list":
        threading.Thread(target=answer, args=(msg,)).start()
os._exit(0)  # stdin closed: exit at once, an unanswered request unanswered
'''


def test_trust_status_asks_codex_and_maps_its_answer(tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    home = tmp_path / "codexhome"
    home.mkdir()
    assert cx.trust_status(ws, home=home, codex_bin="/nonexistent").status == "absent"
    script, hooks = cx.install(ws, python=sys.executable, queue_dir=tmp_path / "q", max_hold=0,
                                 allow_untested_agent_version=True)
    command = json.loads(hooks.read_text())["hooks"]["PermissionRequest"][0]["hooks"][0]["command"]
    key = f"{os.path.realpath(hooks)}:permission_request:0:0"
    (home / "config.toml").write_text(f'[hooks.state."{key}"]\ntrusted_hash = "sha256:old"\n')
    fake = tmp_path / "codex"
    for codex_says, enabled, levain_says in (("trusted", True, "trusted"), ("trusted", False, "disabled"),
                                             ("untrusted", True, "untrusted"), ("modified", True, "changed"),
                                             (None, True, "unlisted")):
        fake.write_text(FAKE_CODEX.format(python=sys.executable, status=codex_says, enabled=enabled, key=key,
                                          command=command))
        fake.chmod(0o755)
        got = cx.trust_status(ws, home=home, codex_bin=str(fake), timeout=20, queue_dir=tmp_path / "q")
        assert (got.status, got.key, got.trusted_hash, got.script_intact) == (levain_says, key, "sha256:old", True)
    with open(script, "a") as fh:
        fh.write("# edited\n")
    assert cx.trust_status(ws, home=home, codex_bin=str(fake), timeout=20, queue_dir=tmp_path / "q").script_intact \
        is False


def test_installed_hook_never_imports_a_levain_planted_beside_it(tmp_path):
    """r1 HIGH: Python puts a script's directory first on sys.path, so ``.codex/hooks/levain/`` shadowed levain."""
    import subprocess
    ws = tmp_path / "ws"
    ws.mkdir()
    script, hooks = cx.install(ws, python=sys.executable, queue_dir=tmp_path / "q", max_hold=0,
                               allow_untested_agent_version=True)
    # away mode on (no hold), so the real adapter files the request this test reads as proof that it ran
    (tmp_path / "q").mkdir(mode=0o700, exist_ok=True)
    (tmp_path / "q" / cx.SETTINGS_NAME).write_text(json.dumps({"away_mode": True, "away_hold_seconds": 0}))
    plant = script.parent / "levain" / "consent" / "adapters"
    plant.mkdir(parents=True)
    for d in (plant.parent.parent, plant.parent, plant):
        (d / "__init__.py").write_text("")
    (plant / "codex.py").write_text("def main(argv=None):\n    print('PLANTED ALLOW')\n    return 0\n")
    command = json.loads(hooks.read_text())["hooks"]["PermissionRequest"][0]["hooks"][0]["command"]
    event = {"hook_event_name": "PermissionRequest", "session_id": "s", "cwd": str(ws), "tool_name": "Bash",
             "tool_input": {"command": "true"}}
    env = dict(os.environ, PYTHONPATH=str(script.parent))
    out = subprocess.run(command, shell=True, input=json.dumps(event), capture_output=True, text=True, cwd=ws,
                         env=env, timeout=60)
    assert (out.returncode, out.stdout) == (0, "")
    assert (tmp_path / "q" / "queue.db").exists()  # the real adapter ran: it filed the request
    assert cx.script_intact(script, queue_dir=tmp_path / "q")


def test_real_core_grant_answers_an_apply_patch_inside_its_workspace_only(tmp_path):
    """Q3+Q4 merge: the Codex adapter passes the input it filed and the posture to the policy, and registers its
    apply_patch reader; without either, a grant answers nothing. A patch naming a file outside the workspace, and
    one whose headers cannot be read, fall to the human."""
    import io
    import sqlite3
    from datetime import datetime, timedelta, timezone

    import levain.authority as auth
    from levain.authority import ReplayCache
    from levain.cockpit.consent import utc_stamp
    from levain.cockpit.firejournal import CockpitDB
    from levain.consent import policy as policy_mod
    from tests.test_consent_cc_adapter import Key

    assert policy_mod._TARGETS["codex"] is cx.policy_targets
    key = Key(tmp_path, "laptop")
    root = tmp_path / "cockpit"
    root.mkdir()
    with CockpitDB(root).read():
        pass
    ws = Path(os.path.realpath(tmp_path / "proj"))
    ws.mkdir()
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    for op in ({"op": "enrol", "fingerprint": key.fp, "public_key": key.public_key, "class": "laptop-presence",
                "custody": "passphrase"},
               {"op": "create_grant", "grant": {"id": "gx", "kind": "consent", "agent": "codex",
                                                "tools": ["apply_patch"], "workspace": str(ws),
                                                "postures": ["default"],
                                                "expires_at": utc_stamp(datetime.now(timezone.utc)
                                                                        + timedelta(days=1))}}):
        auth.sign_and_append(conn, op, signer_fp=key.fp, sign=lambda m: key.sign(m, auth.NAMESPACE),
                             clock=lambda: datetime.now(timezone.utc), cache=ReplayCache(), anchor=None)
    conn.close()
    qdir = tmp_path / "home" / "consent"
    qdir.mkdir(parents=True, mode=0o700)
    (qdir / cx.SETTINGS_NAME).write_text(json.dumps({"cockpit_data_root": str(root)}))

    def patch(path):
        return {"command": f"*** Begin Patch\n*** Add File: {path}\n+hi\n*** End Patch\n"}

    def run(tool_input, **extra):
        e = {"session_id": "s-1", "cwd": str(ws), "permission_mode": "default", "hook_event_name": "PermissionRequest",
             "tool_name": "apply_patch", "tool_input": tool_input, **extra}
        out = io.StringIO()
        assert cx.main(["--queue-dir", str(qdir)], stdin=io.StringIO(json.dumps(e)), stdout=out) == 0
        return out.getvalue()
    got = json.loads(run(patch("a.txt")))
    assert got["hookSpecificOutput"]["decision"] == {"behavior": "allow"}
    assert run(patch(str(tmp_path / "outside.txt"))) == ""
    assert run({"command": "*** Begin Patch\n*** Bogus: a.txt\n*** End Patch\n"}) == ""
    assert run(patch("a.txt"), permission_mode="never") == ""
    # r2 item 1 (codex r2 #1): an inside header does not cover an outside path field carried beside it
    assert run({**patch("a.txt"), "path": str(tmp_path / "outside.txt")}) == ""
    assert run({**patch("a.txt"), "file_path": str(tmp_path / "outside.txt")}) == ""


def test_a_codex_mcp_write_naming_agent_config_in_any_field_is_tagged(tmp_path):
    """Ruling 3b item 1 for Codex: an MCP server's own field name (``filepath``) still reaches config."""
    home, codex_dir = tmp_path / "home", tmp_path / "codexhome"
    for inp in ({"filepath": ".codex/config.toml"}, {"target": {"file": str(codex_dir / "x")}},
                {"filepath": ".claude/settings.json"}):
        assert cx.targets_agent_config("mcp__fs__write", inp, str(tmp_path), home=home, codex_dir=codex_dir, environ=h4_env()), inp
    assert not cx.targets_agent_config("mcp__fs__write", {"filepath": "src/a.py"}, str(tmp_path), home=home,
                                       codex_dir=codex_dir, environ=h4_env())


def test_a_codex_mcp_write_naming_a_config_directory_is_tagged(tmp_path):
    home = tmp_path / "home"
    for inp in ({"dir": "~/.claude", "name": "settings.json"}, {"dir": str(tmp_path / ".codex"), "name": "hooks.json"},
                {"name": ".claude.json"}):
        assert cx.targets_agent_config("mcp__fs__write", inp, str(tmp_path), home=home,
                                       codex_dir=tmp_path / "ch", environ=h4_env()), inp


def test_a_codex_mcp_input_that_is_not_an_object_is_still_read(tmp_path):
    for inp in ([".codex/config.toml"], [{"file": ".codex/config.toml"}], "file:///x/.codex/hooks.json?a=1"):
        assert cx.targets_agent_config("mcp__fs__write", inp, str(tmp_path), home=tmp_path / "h",
                                       codex_dir=tmp_path / "ch", environ=h4_env()), inp


# The PermissionRequest real Codex 0.162.1 sent for input typed into a terminal it launched outside the sandbox
# (project_memory/1017-shellnames, run p8; ids and paths shortened). A grant naming write_stdin answered it there and
# `touch cx_stdin` ran with no dialog.
_WRITE_STDIN_INPUT = {"session_id": 71143, "chars": "touch cx_stdin\nexit\n", "parent_call_id": "exec-4bd9c9c5",
                      "approval_id": "exec-4acc630a", "environment_id": "local", "cwd": "file:///PROJ", "tty": True,
                      "sandbox_permissions": "require_escalated", "additional_permissions": None}


def _granted_codex_hook(tmp_path, tools):
    """A real cockpit with one Codex consent grant ``gx`` on ``tools`` over ``proj``; returns (workspace, conn
    factory, key, run(tool_name, tool_input) -> stdout)."""
    import io
    import sqlite3
    from datetime import datetime, timedelta, timezone

    import levain.authority as auth
    from levain.authority import ReplayCache
    from levain.cockpit.consent import utc_stamp
    from levain.cockpit.firejournal import CockpitDB
    from tests.test_consent_cc_adapter import Key

    key = Key(tmp_path, "laptop")
    root = tmp_path / "cockpit"
    root.mkdir()
    with CockpitDB(root).read():
        pass
    ws = Path(os.path.realpath(tmp_path / "proj"))
    ws.mkdir()
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    for op in ({"op": "enrol", "fingerprint": key.fp, "public_key": key.public_key, "class": "laptop-presence",
                "custody": "passphrase"},
               {"op": "create_grant", "grant": {"id": "gx", "kind": "consent", "agent": "codex",
                                                "tools": sorted(tools), "workspace": str(ws),
                                                "postures": ["default"],
                                                "expires_at": utc_stamp(datetime.now(timezone.utc)
                                                                        + timedelta(days=1))}}):
        auth.sign_and_append(conn, op, signer_fp=key.fp, sign=lambda m: key.sign(m, auth.NAMESPACE),
                             clock=lambda: datetime.now(timezone.utc), cache=ReplayCache(), anchor=None)
    conn.close()
    qdir = tmp_path / "home" / "consent"
    qdir.mkdir(parents=True, mode=0o700)
    (qdir / cx.SETTINGS_NAME).write_text(json.dumps({"cockpit_data_root": str(root)}))

    def run(tool_name, tool_input):
        e = {"session_id": "s-1", "turn_id": "t-1", "cwd": str(ws), "hook_event_name": "PermissionRequest",
             "model": "gpt-5.6-luna", "permission_mode": "default", "tool_name": tool_name, "tool_input": tool_input}
        out = io.StringIO()
        assert cx.main(["--queue-dir", str(qdir)], stdin=io.StringIO(json.dumps(e)), stdout=out) == 0
        return out.getvalue()
    return ws, lambda: sqlite3.connect(root / "cockpit.db", isolation_level=None), key, run


def test_a_grant_never_answers_shell_input_codex_types_into_a_running_terminal(tmp_path, monkeypatch):
    """1017-shellnames, reproduced on main 9e30464 with real Codex 0.162.1: Codex asks before it sends input to a
    terminal launched outside the sandbox, as ``write_stdin``, not ``Bash``, so the shell rule did not see it; a grant
    naming write_stdin was appended and answered ``touch`` typed into /bin/sh. The log now refuses that grant, and
    one signed before the rule (made here by switching the append check off) answers nothing and renews nothing,
    while the same grant's apply_patch still answers (the control)."""
    from datetime import datetime, timedelta, timezone

    import levain.authority as auth
    from levain.authority import ReplayCache
    from levain.cockpit.consent import utc_stamp

    (tmp_path / "a").mkdir()
    with pytest.raises(auth.AuthorityError, match="names the shell tool 'write_stdin'"):
        _granted_codex_hook(tmp_path / "a", ["write_stdin"])
    (tmp_path / "b").mkdir()
    with monkeypatch.context() as m:   # never monkeypatch.undo(): it would revert conftest's isolation too
        m.setattr(auth, "_consent_tool_check", lambda grants, after="": None)
        ws, connect, key, run = _granted_codex_hook(tmp_path / "b", ["apply_patch", "write_stdin"])
    inp = {**_WRITE_STDIN_INPUT, "cwd": f"file://{ws}"}
    assert run("write_stdin", inp) == ""
    patch = {"command": f"*** Begin Patch\n*** Add File: {ws / 'a.txt'}\n+hi\n*** End Patch"}
    assert json.loads(run("apply_patch", patch))["hookSpecificOutput"]["decision"] == {"behavior": "allow"}
    now = datetime.now(timezone.utc)
    conn = connect()
    with pytest.raises(auth.AuthorityError, match="names the shell tool 'write_stdin'"):
        auth.sign_and_append(conn, {"op": "renew", "grant_id": "gx", "expires_at": utc_stamp(now + timedelta(days=5)),
                                    "signed_at": utc_stamp(now)},
                             signer_fp=key.fp, sign=lambda m: key.sign(m, auth.NAMESPACE), clock=lambda: now,
                             cache=ReplayCache(), anchor=None)
    conn.close()


def test_a_codex_grant_answers_only_a_tool_levain_knows_for_codex(tmp_path, monkeypatch):
    """Complement MED, L3 round 1 on 1b24c31: a shell denylist fails open for the next unlisted shell-capable name,
    as write_stdin did. Reproduced on 1b24c31 with the real CLI: ``--agent codex --tool SomeNewTool`` (and ``Edit``,
    Claude Code's name) were accepted, and a SomeNewTool grant answered a SomeNewTool request. A Codex grant may now
    name only apply_patch or an MCP read tool: the log refuses the rest, one signed before the rule answers nothing,
    and its renew is refused with the way out."""
    from datetime import datetime, timedelta, timezone

    import levain.authority as auth
    from levain.authority import ReplayCache
    from levain.cockpit.consent import utc_stamp

    for i, tool in enumerate(("SomeNewTool", "Edit")):
        (tmp_path / str(i)).mkdir()
        with pytest.raises(auth.AuthorityError, match=f"'{tool}' is not a tool levain knows for codex"):
            _granted_codex_hook(tmp_path / str(i), [tool])
    (tmp_path / "b").mkdir()
    with monkeypatch.context() as m:   # never monkeypatch.undo(): it would revert conftest's isolation too
        m.setattr(auth, "_consent_tool_check", lambda grants, after="": None)
        ws, connect, key, run = _granted_codex_hook(tmp_path / "b", ["SomeNewTool", "apply_patch"])
    assert run("SomeNewTool", {"anything": "goes"}) == ""
    patch = {"command": f"*** Begin Patch\n*** Add File: {ws / 'a.txt'}\n+hi\n*** End Patch"}
    assert json.loads(run("apply_patch", patch))["hookSpecificOutput"]["decision"] == {"behavior": "allow"}
    now = datetime.now(timezone.utc)
    conn = connect()
    with pytest.raises(auth.AuthorityError, match="'gx' was signed before this rule: revoke it .* create a new one"):
        auth.sign_and_append(conn, {"op": "renew", "grant_id": "gx", "expires_at": utc_stamp(now + timedelta(days=5)),
                                    "signed_at": utc_stamp(now)},
                             signer_fp=key.fp, sign=lambda m: key.sign(m, auth.NAMESPACE), clock=lambda: now,
                             cache=ReplayCache(), anchor=None)
    conn.close()


@pytest.mark.parametrize("tool", sorted(CONSENT_SHELL_TOOLS) + ["Bash", "PowerShell", "Write_Stdin", "EXEC_COMMAND"])
def test_the_log_refuses_a_grant_naming_any_shell_tool_in_any_case(tmp_path, tool):
    import levain.authority as auth
    with pytest.raises(auth.AuthorityError, match="names the shell tool"):
        _granted_codex_hook(tmp_path, [tool])


def test_every_shell_name_the_hook_templates_know_is_a_shell_tool_to_consent():
    """The PostToolUse templates are standalone files copied into an install, so they cannot import the set: a shell
    name a hook knows (and so an agent may send) that consent does not would be grantable. Consent may know more
    (write_stdin carries ``chars``, not the ``command`` those hooks summarise)."""
    import ast

    import levain
    templates = Path(levain.__file__).parent / "templates"
    found = 0
    for path in sorted(templates.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_SHELL_TOOLS"
                                                    for t in node.targets):
                names = ast.literal_eval(node.value.args[0])
                found += 1
                assert {n.lower() for n in names} <= CONSENT_SHELL_TOOLS, path
    assert found >= 2      # both PostToolUse templates (Claude Code's and Codex's)


def test_a_patch_into_the_operators_user_site_is_agent_config_under_a_venv(monkeypatch, tmp_path):
    """F7, through Codex's own entry: its patch headers go through the Claude Code check."""
    from levain.consent.adapters import claude_code as cc

    home = tmp_path / "home"
    monkeypatch.setattr(cc.site, "ENABLE_USER_SITE", False)
    monkeypatch.setattr(cc, "operator_home", lambda environ: tmp_path / "elsewhere")
    target = home / ".local" / "lib" / "python3.12" / "site-packages" / "x.pth"
    patch = {"command": f"*** Begin Patch\n*** Add File: {target}\n+import os\n*** End Patch\n"}
    assert cx.targets_agent_config("apply_patch", patch, str(tmp_path), home=home, codex_dir=tmp_path / "cx", environ=h4_env())


def test_a_patch_into_an_absolute_pythonuserbase_site_is_agent_config(monkeypatch, tmp_path):
    """U1, through Codex's own entry: <PYTHONUSERBASE>/lib/.../site-packages/x.pth is agent config."""
    from levain.consent.adapters import claude_code as cc

    base = tmp_path / "opt" / "python-user"
    monkeypatch.setenv("PYTHONUSERBASE", str(base))
    monkeypatch.setattr(cc.site, "ENABLE_USER_SITE", False)
    monkeypatch.setattr(cc, "operator_home", lambda environ: tmp_path / "elsewhere")
    target = base / "lib" / "python3.12" / "site-packages" / "x.pth"
    patch = {"command": f"*** Begin Patch\n*** Add File: {target}\n+import os\n*** End Patch\n"}
    assert cx.targets_agent_config("apply_patch", patch, str(tmp_path), home=tmp_path / "home",
                                   codex_dir=tmp_path / "cx", environ=h4_env())
