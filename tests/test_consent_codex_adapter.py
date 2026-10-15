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

from levain.consent.adapters import codex as cx

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
