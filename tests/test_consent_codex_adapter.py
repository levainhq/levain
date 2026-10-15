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
def answer(msg):  # answered asynchronously, a moment later, as codex app-server does
    time.sleep(0.5)
    hooks = [{{"key": {key!r}, "eventName": "permissionRequest", "handlerType": "command",
               "command": {command!r}, "currentHash": "sha256:cur", "trustStatus": status}}]
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
    script, hooks = cx.install(ws, python=sys.executable, queue_dir=tmp_path / "q", max_hold=0)
    command = json.loads(hooks.read_text())["hooks"]["PermissionRequest"][0]["hooks"][0]["command"]
    key = f"{os.path.realpath(hooks)}:permission_request:0:0"
    (home / "config.toml").write_text(f'[hooks.state."{key}"]\ntrusted_hash = "sha256:old"\n')
    fake = tmp_path / "codex"
    for codex_says, levain_says in (("trusted", "trusted"), ("untrusted", "untrusted"), ("modified", "changed"),
                                    (None, "unlisted")):
        fake.write_text(FAKE_CODEX.format(python=sys.executable, status=codex_says, key=key, command=command))
        fake.chmod(0o755)
        got = cx.trust_status(ws, home=home, codex_bin=str(fake), timeout=20)
        assert (got.status, got.key, got.trusted_hash, got.script_intact) == (levain_says, key, "sha256:old", True)
    with open(script, "a") as fh:
        fh.write("# edited\n")
    assert cx.trust_status(ws, home=home, codex_bin=str(fake), timeout=20).script_intact is False
