#!/usr/bin/env python3
"""SPIKE (throwaway): drive a Claude Code entity the way the cockpit chat would.

One turn over `claude -p --input-format stream-json --output-format stream-json`, with
`--permission-prompt-tool stdio` so CC sends every permission prompt to this host as a
`control_request` (subtype `can_use_tool`), the same wire the Claude Agent SDK uses. The host
prints the raw call (what the cockpit's consent row would show) and answers allow/deny by the
--decide policy. Every line of the stream is written to --log for the parity checks.

    cc_drive.py --cwd ENTITY --prompt "..." [--resume ID] [--decide allow|deny|ask]
                [--model M] [--effort E] [--log FILE]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--resume")
    ap.add_argument("--session-id")
    ap.add_argument("--decide", default="ask", choices=["allow", "deny", "ask"])
    ap.add_argument("--model")
    ap.add_argument("--effort")
    ap.add_argument("--log", default="cc_drive.jsonl")
    a = ap.parse_args()

    cmd = [
        os.path.expanduser("~/.local/bin/claude"), "-p",
        "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
        "--include-hook-events",
        "--permission-prompt-tool", "stdio",
        "--permission-mode", "default",
    ]
    if a.model:
        cmd += ["--model", a.model]
    if a.effort:
        cmd += ["--effort", a.effort]
    if a.resume:
        cmd += ["--resume", a.resume]
    elif a.session_id:
        cmd += ["--session-id", a.session_id]

    # Same env scrub as flowConnect's chat: no API key, so the turn rides the CLI's own login.
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "CLAUDECODE") and not k.startswith("CLAUDE_CODE_")}
    p = subprocess.Popen(cmd, cwd=a.cwd, env=env, text=True, bufsize=1,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr)
    log = open(a.log, "a")

    def send(obj: dict) -> None:
        log.write(json.dumps({"_dir": "host->cc", **obj}) + "\n")
        p.stdin.write(json.dumps(obj) + "\n")
        p.stdin.flush()

    send({"type": "control_request", "request_id": f"init-{uuid.uuid4().hex[:8]}",
          "request": {"subtype": "initialize", "hooks": None}})
    send({"type": "user", "session_id": "",
          "message": {"role": "user", "content": a.prompt}, "parent_tool_use_id": None})

    session_id = None
    for line in p.stdout:
        line = line.strip()
        if not line:
            continue
        log.write(line + "\n")
        log.flush()
        try:
            m = json.loads(line)
        except ValueError:
            print("RAW", line)
            continue
        t = m.get("type")
        session_id = m.get("session_id") or session_id
        if t == "control_request" and m["request"].get("subtype") == "can_use_tool":
            r = m["request"]
            print("\n=== CONSENT ROW (raw call) ===")
            print(json.dumps({"tool_name": r["tool_name"], "input": r["input"]}, indent=1))
            decision = a.decide
            if decision == "ask":
                with open("/dev/tty") as tty:
                    print("approve? [y/N] ", end="", flush=True)
                    decision = "allow" if tty.readline().strip().lower() == "y" else "deny"
            resp = ({"behavior": "allow", "updatedInput": r["input"]} if decision == "allow"
                    else {"behavior": "deny", "message": "operator rejected in cockpit"})
            print("->", decision)
            send({"type": "control_response", "response": {
                "subtype": "success", "request_id": m["request_id"], "response": resp}})
        elif t == "system":
            st = m.get("subtype")
            if st == "init":
                print(f"[init] session={m.get('session_id')} model={m.get('model')} "
                      f"tools={len(m.get('tools', []))} mcp={m.get('mcp_servers')}")
            elif st and st.startswith("hook"):
                print(f"[{st}] {m.get('hook_event') or m.get('hook_event_name')} {m.get('hook_name', '')}")
        elif t == "assistant":
            for c in m["message"].get("content", []):
                if c.get("type") == "text":
                    print("ASSISTANT:", c["text"][:400])
                elif c.get("type") == "tool_use":
                    print("TOOL_USE:", c["name"], json.dumps(c["input"])[:200])
        elif t == "result":
            print(f"[result] {m.get('subtype')} turns={m.get('num_turns')} "
                  f"cost={m.get('total_cost_usd')} session={m.get('session_id')}")
            p.stdin.close()
    p.wait()
    print("SESSION", session_id)
    return p.returncode


if __name__ == "__main__":
    sys.exit(main())
