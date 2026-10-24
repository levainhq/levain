"""CH-D2 S4: the fake decider-frame driver for the glue's `node --test` reds (head ruling on S1's gap).

It speaks srt 0.0.81's decider protocol (4-byte big-endian length + one JSON object) through the round-4 spike
decider's `Link` (fixtures/spike_glue/decider_spike4.py, logging raw: this is a test, every frame is recorded whole),
spawns `levain/chat_egress/node/glue.mjs` with a CLOSED env and explicit fds as S8's launch will, and answers by a
fixed script, so a red sees what the GLUE did, not what a decider decided.

usage: fake_decider.py <out-dir> <script.json> -- <glue args...>
  script: {"remove": [names], "sni_rule": bool, "tls_ports": [..], "http_ports": [..], "literal_wall": bool,
           "hang6_port": int | null}
  verdicts by path: /deny... -> deny 403 "card: test"; /hold/<seconds>... -> allow after the wait; otherwise allow
  with removeHeaders = script.remove. sni_rule (the decider's second layer, R15-14-3; S5b owns the real one):
  scheme = https iff the frame has sni; https only on tls_ports, http only on http_ports, else deny "wall: port".
  literal_wall: a non-public IP-literal host -> deny (the decider's own wall; srt's guard never checks a literal).
  hang6_port: a [::1] listener on that port with a full backlog and no accept (a v6 answer that HANGS, for C7).
Out: frames.jsonl (Link's log), obs.raw (the observation channel's bytes, exactly), proxy.stderr, ca.pem.
Prints `READY <port> pid <node pid>`; a line or EOF on stdin is the clean end (the lifeline closes, the glue writes
its final flush); then prints `EXIT <code>`.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "fixtures" / "spike_glue"))
import decider_spike4 as spike  # noqa: E402

spike.LOGMODE = "raw"
GLUE = HERE.parents[2] / "levain" / "chat_egress" / "node" / "glue.mjs"
NODE = os.environ.get("CHD2_NODE", "node")
SCRIPT: dict = {}


def nonpublic_literal(host: str) -> bool:
    try:
        return not ipaddress.ip_address(host.strip("[]")).is_global
    except ValueError:
        return False


def decide(link: spike.Link, req: dict) -> None:
    rid, path = req["id"], req.get("path", "")
    if path.startswith("/hold/"):
        time.sleep(float(path.split("/")[2]))
    if path.startswith("/deny"):
        return link.send({"t": "verdict", "id": rid, "action": "deny", "status": 403, "reason": "card: test"})
    if SCRIPT.get("literal_wall") and nonpublic_literal(req["host"]):
        return link.send({"t": "verdict", "id": rid, "action": "deny", "status": 403, "reason": "wall: literal"})
    if SCRIPT.get("sni_rule"):
        https = bool(req.get("sni"))
        ok = req["port"] in (SCRIPT["tls_ports"] if https else SCRIPT["http_ports"])
        if not ok:
            return link.send({"t": "verdict", "id": rid, "action": "deny", "status": 403, "reason": "wall: port"})
    v = {"t": "verdict", "id": rid, "action": "allow"}
    if SCRIPT.get("remove"):
        v["removeHeaders"] = SCRIPT["remove"]
    link.send(v)


def serve(link: spike.Link) -> None:
    while True:
        msg = link.read()
        if msg is None:
            return
        if msg["t"] == "hello":
            link.send({"t": "hello", "proto": 1, "allowedDomains": [], "deniedDomains": []})
        elif msg["t"] == "req":
            threading.Thread(target=decide, args=(link, msg), daemon=True).start()


def drain(sock: socket.socket, out: Path) -> None:
    with out.open("ab") as f:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                return
            f.write(chunk)
            f.flush()


def hang6(port: int) -> list[socket.socket]:
    """A [::1]:port listener whose backlog is full: a new connect to it hangs (SYNs dropped), never refused."""
    ls = socket.socket(socket.AF_INET6)
    ls.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    ls.bind(("::1", port))
    ls.listen(0)
    held = [ls]
    for _ in range(8):
        c = socket.socket(socket.AF_INET6)
        c.setblocking(False)
        try:
            c.connect(("::1", port))
        except (BlockingIOError, OSError):
            pass
        held.append(c)
    time.sleep(0.2)
    return held


def main() -> None:
    global SCRIPT
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    SCRIPT = json.loads(Path(sys.argv[2]).read_text())
    glue_args = sys.argv[sys.argv.index("--") + 1:]
    held = hang6(SCRIPT["hang6_port"]) if SCRIPT.get("hang6_port") else []
    key_pem, cert_pem = spike.make_ca()
    (out / "ca.pem").write_bytes(cert_pem)
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(64)
    port = lsock.getsockname()[1]
    ours, theirs = socket.socketpair()
    obs_ours, obs_theirs = socket.socketpair()
    ca_r, ca_w = os.pipe()
    life_r, life_w = os.pipe()
    fds = (lsock.fileno(), theirs.fileno(), ca_r, life_r, obs_theirs.fileno())
    for fd in fds:
        os.set_inheritable(fd, True)
    proc = subprocess.Popen([NODE, str(GLUE), "--listen-fd", str(lsock.fileno()), "--decider-fd", str(theirs.fileno()),
                             "--ca-fd", str(ca_r), "--lifeline-fd", str(life_r), "--obs-fd", str(obs_theirs.fileno()),
                             *glue_args],
                            pass_fds=fds, stderr=(out / "proxy.stderr").open("w"), stdout=subprocess.DEVNULL, env={})
    os.write(ca_w, key_pem + cert_pem)
    os.close(ca_w)
    for fd in (ca_r, life_r):
        os.close(fd)
    theirs.close()
    obs_theirs.close()
    lsock.close()
    threading.Thread(target=serve, args=(spike.Link(ours, out / "frames.jsonl"),), daemon=True).start()
    obs_thread = threading.Thread(target=drain, args=(obs_ours, out / "obs.raw"), daemon=True)
    obs_thread.start()
    print(f"READY {port} pid {proc.pid}", flush=True)
    sys.stdin.readline()
    os.close(life_w)
    try:
        code = proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = proc.wait(timeout=5)
    obs_thread.join(timeout=3)
    for s in held:
        s.close()
    print(f"EXIT {code}", flush=True)


if __name__ == "__main__":
    main()
