#!/usr/bin/env python3
"""SPIKE (throwaway): drive a Codex entity the way the cockpit chat would, over `codex app-server`.

JSON-RPC over stdio (one JSON object per line). initialize -> initialized -> thread/start or
thread/resume -> turn/start. Server-initiated approval requests (item/commandExecution/
requestApproval, item/fileChange/requestApproval, ...) are the harness's own permission prompt:
the host prints the raw request (the cockpit consent row) and answers accept/decline.
Every line is written to --log. Run with CODEX_HOME set to an isolated home.

    codex_drive.py --cwd ENTITY --prompt "..." [--thread ID] [--decide accept|decline]
                   [--model M] [--effort E] [--approval untrusted|on-request|never] [--log F]
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import subprocess
import sys

APPROVAL_METHODS = {
    "item/commandExecution/requestApproval", "item/fileChange/requestApproval",
    "item/permissions/requestApproval", "execCommandApproval", "applyPatchApproval",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--thread")
    ap.add_argument("--decide", default="accept", choices=["accept", "decline"])
    ap.add_argument("--model")
    ap.add_argument("--effort")
    ap.add_argument("--approval", default="untrusted")
    ap.add_argument("--log", default="codex_drive.jsonl")
    ap.add_argument("--sock", help="join a shared app-server (codex app-server --listen unix://PATH)")
    ap.add_argument("--watch", type=float, help="attach only: resume, start no turn, print events")
    a = ap.parse_args()

    argv = ([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "ws_uds_bridge.py"), a.sock]
            if a.sock else ["codex", "app-server"])
    if a.watch:
        import threading
        threading.Timer(a.watch, lambda: p.terminate()).start()
    p = subprocess.Popen(argv, cwd=a.cwd, text=True, bufsize=1,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr)
    log = open(a.log, "a")
    ids = itertools.count(1)

    def send(obj: dict) -> None:
        log.write(json.dumps({"_dir": "host->codex", **obj}) + "\n")
        p.stdin.write(json.dumps(obj) + "\n")
        p.stdin.flush()

    def request(method: str, params: dict) -> int:
        i = next(ids)
        send({"id": i, "method": method, "params": params})
        return i

    pending: dict[int, str] = {}
    pending[request("initialize", {"clientInfo": {"name": "levain-spike", "version": "0"},
                                   "capabilities": None})] = "initialize"
    thread_id = a.thread
    for line in p.stdout:
        line = line.strip()
        if not line:
            continue
        log.write(line + "\n")
        log.flush()
        m = json.loads(line)
        meth = m.get("method")
        if "id" in m and meth is None:  # a response to one of ours
            what = pending.pop(m["id"], "?")
            if "error" in m:
                print(f"[error:{what}]", m["error"])
                break
            r = m["result"]
            if what == "initialize":
                send({"method": "initialized"})
                common = {"cwd": a.cwd, "approvalPolicy": a.approval, "sandbox": "workspace-write"}
                if a.model:
                    common["model"] = a.model
                if thread_id:
                    pending[request("thread/resume", {"threadId": thread_id, **common})] = "resume"
                else:
                    pending[request("thread/start", common)] = "start"
            elif what in ("start", "resume"):
                thread_id = r["thread"]["id"]
                print(f"[{what}] thread={thread_id} model={r.get('model')} "
                      f"approval={r.get('approvalPolicy')} sandbox={json.dumps(r.get('sandbox'))[:80]}")
                if a.watch:
                    continue
                turn = {"threadId": thread_id, "input": [{"type": "text", "text": a.prompt}]}
                if a.model:
                    turn["model"] = a.model
                if a.effort:
                    turn["effort"] = a.effort
                pending[request("turn/start", turn)] = "turn"
        elif meth in APPROVAL_METHODS and "id" in m:
            print("\n=== CONSENT ROW (raw call) ===")
            print(json.dumps({"method": meth, "params": m["params"]}, indent=1)[:1200])
            print("->", a.decide)
            send({"id": m["id"], "result": {"decision": a.decide}})
        elif meth and "id" in m:
            print("[unhandled server request]", meth)
            send({"id": m["id"], "error": {"code": -32601, "message": "spike: unhandled"}})
        elif meth in ("hook/started", "hook/completed"):
            run = m["params"].get("run") or m["params"]
            print(f"[{meth}] {json.dumps(run)[:220]}")
        elif meth == "item/completed":
            it = m["params"]["item"]
            if it.get("type") == "userMessage":
                print("USER:", json.dumps(it.get("content"))[:300])
            elif it.get("type") == "agentMessage":
                print("ASSISTANT:", it.get("text", "")[:400])
            elif it.get("type") == "commandExecution":
                print("CMD:", it.get("command"), "status=", it.get("status"))
        elif meth == "turn/completed":
            print("[turn/completed]", json.dumps(m["params"].get("turn", {}).get("status")))
            break
        elif meth in ("error", "warning", "configWarning"):
            print(f"[{meth}]", json.dumps(m["params"])[:300])
    p.terminate()
    print("THREAD", thread_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
