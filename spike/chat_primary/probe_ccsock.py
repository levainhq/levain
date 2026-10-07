"""Run INSIDE a throwaway CC entity's Bash. Reads only metadata + connects-and-closes ONLY this session's own socket.
Never connects to any other /tmp/cc-socks entry (rule: other seats' sockets are off limits)."""
import os, socket, stat, glob
own = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
tok = os.environ.get("CLAUDE_CODE_MESSAGING_TOKEN")
print("env CLAUDE_CODE_MESSAGING_SOCKET =", own)
print("env CLAUDE_CODE_MESSAGING_TOKEN present =", bool(tok), "len =", len(tok or ""))
try:
    ents = sorted(glob.glob("/tmp/cc-socks/*"))
    print("listdir /tmp/cc-socks: %d entries; dir mode %s" % (len(ents), oct(os.stat("/tmp/cc-socks").st_mode & 0o7777)))
    modes = {oct(os.stat(p).st_mode & 0o7777) for p in ents}; print("entry modes:", modes, "all sockets:", all(stat.S_ISSOCK(os.stat(p).st_mode) for p in ents))
except Exception as e: print("listdir/stat /tmp/cc-socks: BLOCKED", type(e).__name__, e)
if own:
    s = socket.socket(socket.AF_UNIX); s.settimeout(3)
    try: s.connect(own); print("OWN-SOCKET connect-and-close: REACHABLE")
    except Exception as e: print("OWN-SOCKET connect-and-close: BLOCKED", type(e).__name__, e)
    finally: s.close()
