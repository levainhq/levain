"""Run INSIDE the entity's shell. Usage: probe_net.py TCP_PORT UDS_PATH. Prints one line per probe."""
import socket, sys
port, path = int(sys.argv[1]), sys.argv[2]
for name, mk, addr in (("TCP", lambda: socket.socket(), ("127.0.0.1", port)),
                       ("UDS", lambda: socket.socket(socket.AF_UNIX), path)):
    s = mk(); s.settimeout(3)
    try:
        s.connect(addr); s.sendall(b"PROBE-" + name.encode() + b"\n"); print(name, "CONNECTED", s.recv(64))
    except Exception as e: print(name, "BLOCKED", type(e).__name__, e)
    finally: s.close()
# FS control: a user-writable path outside cwd/tmp. Sandboxed Bash must not be able to create it.
import os
fp = os.path.expanduser("~/.s2_fs_probe_control")
try:
    open(fp, "w").write("x"); os.unlink(fp); print("FS WRITE-OUTSIDE-CWD ALLOWED")
except Exception as e: print("FS WRITE-OUTSIDE-CWD BLOCKED", type(e).__name__, e)
