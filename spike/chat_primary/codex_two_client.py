#!/usr/bin/env python3
"""S2 probe 2b: TWO clients on one shared `codex app-server --listen unix://SOCK` (isolated CODEX_HOME).
A starts a thread + a turn that needs approval. B joins on the same socket. Measured: which client receives the
approval request; whether B's answer is honoured; whether B can start turns / override policy / run
thread/shellCommand (documented as unsandboxed). Every frame, tagged by client, goes to the log.

    codex_two_client.py ENTITY SOCK LOG MARKER_DIR
"""
import base64, json, os, queue, socket, struct, subprocess, sys, threading, time
from ws_uds_bridge import recv_exact

ent, sock, logp, mdir = sys.argv[1:5]
log = open(logp, "w"); llock = threading.Lock()
def L(tag, kind, obj):
    with llock: log.write(json.dumps({"t": round(time.time(), 3), "c": tag, "k": kind, **({"m": obj} if not isinstance(obj, dict) else obj)}) + "\n"); log.flush()

class C:
    def __init__(self, tag):
        self.tag, self.q, self.n, self.lock = tag, queue.Queue(), 0, threading.Lock()
        s = self.s = socket.socket(socket.AF_UNIX); s.connect(sock)
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f"GET / HTTP/1.1\r\nHost: l\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head: head += s.recv(1)
        assert b" 101 " in head.split(b"\r\n")[0], head
        threading.Thread(target=self.rd, daemon=True).start()
    def raw(self, obj):
        d = json.dumps(obj).encode(); L(self.tag, "send", obj)
        h = bytearray([0x81]); n = len(d)
        h += bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n)
        m = os.urandom(4)
        with self.lock: self.s.sendall(bytes(h) + m + bytes(b ^ m[i % 4] for i, b in enumerate(d)))
    def req(self, method, params):
        self.n += 1; self.raw({"id": self.n, "method": method, "params": params}); return self.n
    def rd(self):
        buf = b""
        try:
            while True:
                b0, b1 = recv_exact(self.s, 2); op, n = b0 & 15, b1 & 127
                if n == 126: n = struct.unpack(">H", recv_exact(self.s, 2))[0]
                elif n == 127: n = struct.unpack(">Q", recv_exact(self.s, 8))[0]
                p = recv_exact(self.s, n)
                if op == 8: break
                if op == 9: continue
                buf += p
                if b0 & 0x80:
                    m = json.loads(buf); buf = b""; L(self.tag, "recv", m); self.q.put(m)
        except Exception as e: L(self.tag, "reader-end", str(e))
    def wait(self, pred, timeout=60):
        end = time.time() + timeout; seen = []
        while time.time() < end:
            try: m = self.q.get(timeout=0.5)
            except queue.Empty: continue
            seen.append(m)
            if pred(m): return m, seen
        return None, seen
    def drain(self):
        out = []
        while not self.q.empty(): out.append(self.q.get())
        return out

def init(c):
    i = c.req("initialize", {"clientInfo": {"name": "s2-" + c.tag, "version": "0"}, "capabilities": None})
    r, _ = c.wait(lambda m: m.get("id") == i); c.raw({"method": "initialized"}); return r
def ans(c, i): r, _ = c.wait(lambda m: m.get("id") == i, 30); return r
APPROVAL = lambda m: m.get("method", "").endswith("/requestApproval") or "requestApproval" in m.get("method", "")

A = C("A"); print("A init:", json.dumps(init(A))[:160])
B = C("B"); print("B init:", json.dumps(init(B))[:160])
r = ans(A, A.req("thread/start", {"cwd": ent, "approvalPolicy": "untrusted", "sandbox": "workspace-write"}))
tid = r["result"]["thread"]["id"]; print("A thread", tid)
print("B thread/loaded/list ->", json.dumps(ans(B, B.req("thread/loaded/list", {})))[:300])
rb = ans(B, B.req("thread/resume", {"threadId": tid})); print("B thread/resume (no overrides) ->", json.dumps(rb)[:300])
marker = os.path.join(ent, "s2_approval_marker.txt")
if os.path.exists(marker): os.unlink(marker)
A.req("turn/start", {"threadId": tid, "input": [{"type": "text", "text": "Run exactly this shell command and nothing else: touch s2_approval_marker.txt"}]})
ma, seen_a = A.wait(APPROVAL, 90)
print("A got approval request:", bool(ma), ma and ma["method"], "id=", ma and ma["id"])
mb, seen_b = B.wait(APPROVAL, 6)
print("B ALSO got an approval request (while subscribed via resume):", bool(mb), mb and mb["method"], "id=", mb and mb["id"])
print("B frames while A's approval pending:", [m.get("method") or ("resp:" + str(m.get("id"))) for m in seen_b][:12])
time.sleep(1); print("marker exists before any answer:", os.path.exists(marker))
# B answers A's pending request id (B only knows it if it saw it; here use A's id as a guess/leak)
B.raw({"id": ma["id"], "result": {"decision": "accept"}})
time.sleep(8)
print("marker exists after B answered A's request id:", os.path.exists(marker))
done, _ = A.wait(lambda m: m.get("method") in ("serverRequest/resolved", "turn/completed"), 8)
print("A after B's answer:", done and done.get("method"))
if not os.path.exists(marker):
    A.raw({"id": ma["id"], "result": {"decision": "accept"}}); A.wait(lambda m: m.get("method") == "turn/completed", 60)
    print("marker after A's own answer:", os.path.exists(marker))
else:
    A.wait(lambda m: m.get("method") == "turn/completed", 60)
# B starts its own turn on A's thread with policy overrides (never + danger-full-access); marker is OUTSIDE the workspace roots
bm = os.path.join(mdir, "B_turn_marker")
if os.path.exists(bm): os.unlink(bm)
B.drain()
B.req("turn/start", {"threadId": tid, "approvalPolicy": "never", "sandboxPolicy": {"type": "dangerFullAccess"},
                     "input": [{"type": "text", "text": f"Run exactly this shell command and nothing else: touch {bm}"}]})
B.wait(lambda m: m.get("method") == "turn/completed", 90)
print("B-started turn (override never+dangerFullAccess) wrote marker outside workspace:", os.path.exists(bm))
# B runs thread/shellCommand (schema: 'runs unsandboxed with full access')
sm = os.path.join(mdir, "B_shellcommand_marker")
if os.path.exists(sm): os.unlink(sm)
print("B thread/shellCommand ->", json.dumps(ans(B, B.req("thread/shellCommand", {"threadId": tid, "command": f"touch {sm}"})))[:200])
time.sleep(4); print("B shellCommand marker exists (command ran with NO approval):", os.path.exists(sm))
