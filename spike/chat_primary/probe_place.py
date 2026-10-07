"""Run INSIDE a model shell. Usage: probe_place.py REAL_SOCK DUMMY1 DUMMY2 ...
1) connect() to the REAL app-server socket. 2) for each dummy path (pre-created by the host): can this shell unlink it, or rename it?"""
import os, socket, sys, tempfile
real, dummies = sys.argv[1], sys.argv[2:]
s = socket.socket(socket.AF_UNIX); s.settimeout(3)
try: s.connect(real); print("REAL-SOCKET CONNECT: REACHABLE")
except Exception as e: print("REAL-SOCKET CONNECT: BLOCKED", type(e).__name__, e)
print("TMPDIR =", os.environ.get("TMPDIR"), "| tempfile.gettempdir() =", tempfile.gettempdir())
for d in dummies:
    try: os.rename(d, d + ".renamed"); os.rename(d + ".renamed", d); r = "rename-ok"
    except Exception as e: r = "rename-BLOCKED " + type(e).__name__
    try: os.unlink(d); u = "UNLINK-OK(file gone)"
    except Exception as e: u = "unlink-BLOCKED " + type(e).__name__
    print(f"{d}: {r}; {u}")
