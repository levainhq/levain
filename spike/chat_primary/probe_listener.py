"""S2 probe listener: a TCP loopback port and an AF_UNIX pathname socket that log every
connection (server-side ground truth). Usage: probe_listener.py LOG TCP_PORT UDS_PATH"""
import socket, sys, threading, time, os
log, port, path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
def w(m):
    with open(log, "a") as f: f.write(f"{time.time():.3f} {m}\n")
def serve(s, tag):
    s.listen(8)
    while True:
        c, a = s.accept()
        try:
            c.settimeout(1); d = c.recv(256)
        except Exception: d = b""
        w(f"{tag} HIT data={d[:60]!r}")
        try: c.sendall(b"HTTP/1.0 200 OK\r\n\r\nLISTENER-" + tag.encode() + b"\n")
        except Exception: pass
        c.close()
t = socket.socket(); t.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); t.bind(("127.0.0.1", port))
if os.path.exists(path): os.unlink(path)
u = socket.socket(socket.AF_UNIX); u.bind(path)
w(f"listening tcp={port} uds={path}")
threading.Thread(target=serve, args=(u, "UDS"), daemon=True).start()
serve(t, "TCP")
