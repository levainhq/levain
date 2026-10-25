"""Round 4 local ORIGIN on 127.0.0.1 only: plain and TLS listeners; git smart HTTP through `git http-backend` (CGI) for
/<name>.git/... paths under <repo-root>; everything else answers 200 text. Every REQUEST is logged to <log> (JSONL):
port, tls, sni, the raw request head (repr, capped 6000), body framing (content-length / chunked, chunk count, WIRE
bytes of the body section, decoded length), and the response status/content-type. "Upstream bytes observed".

A non-git path containing `fakeadv` and ending /info/refs answers 200 with the advertisement Content-Type and a bogus
body: the R15-17-1 residue ("a hostile server can advertise").

usage: git_origin.py <pki-dir> <repo-root> <log> <seconds> plain:9080 tls:9443 ...
"""
import asyncio
import json
import os
import re
import ssl
import sys
import time
from pathlib import Path

PKI, ROOT, LOG = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]).open("a")
# CHD2-S1 PATCH: git-http-backend is found through `git --exec-path`, not an Xcode path.
import subprocess
BACKEND = os.path.join(subprocess.check_output(["git", "--exec-path"], text=True).strip(), "git-http-backend")
ADV_CT = b"application/x-git-upload-pack-advertisement"

ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(PKI / "good-leaf.pem", PKI / "good-leaf.key")


async def read_head(r):
    head = b""
    while b"\r\n\r\n" not in head:
        c = await r.read(1)
        if not c:
            return None
        head += c
        if len(head) > 1 << 20:
            return None
    return head


async def read_body(r, hdrs):
    """(decoded body, wire bytes, chunked?, chunk count)."""
    te = hdrs.get(b"transfer-encoding", [b""])[-1].lower()
    if b"chunked" in te:
        body, wire, n = b"", 0, 0
        while True:
            line = await r.readline()
            wire += len(line)
            size = int(line.split(b";")[0].strip() or b"0", 16)
            if size == 0:
                while True:                      # trailers until the blank line
                    t = await r.readline()
                    wire += len(t)
                    if t in (b"\r\n", b"\n", b""):
                        break
                return body, wire, True, n
            data = await r.readexactly(size + 2)
            wire += len(data)
            body += data[:-2]
            n += 1
    cl = hdrs.get(b"content-length")
    if cl:
        n = int(cl[-1])
        data = await r.readexactly(n)
        return data, n, False, 0
    return b"", 0, False, 0


async def cgi(method, path, query, hdrs, body):
    env = {"PATH": "/usr/bin:/bin", "GIT_PROJECT_ROOT": str(ROOT), "GIT_HTTP_EXPORT_ALL": "1", "HOME": str(ROOT),
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "REQUEST_METHOD": method,
           "PATH_INFO": path, "QUERY_STRING": query, "REMOTE_ADDR": "127.0.0.1", "CONTENT_LENGTH": str(len(body)),
           "GATEWAY_INTERFACE": "CGI/1.1", "SERVER_PROTOCOL": "HTTP/1.1"}
    if b"content-type" in hdrs:
        env["CONTENT_TYPE"] = hdrs[b"content-type"][-1].decode()
    for name in (b"content-encoding", b"git-protocol"):
        if name in hdrs:
            env["HTTP_" + name.decode().upper().replace("-", "_")] = hdrs[name][-1].decode()
    p = await asyncio.create_subprocess_exec(BACKEND, env=env, stdin=asyncio.subprocess.PIPE,
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await p.communicate(body)
    a, b2 = out.find(b"\r\n\r\n"), out.find(b"\n\n")
    sep = b"\r\n\r\n" if a != -1 and (b2 == -1 or a < b2) else b"\n\n"
    h, _, b = out.partition(sep)
    status, rh = b"200 OK", []
    for line in h.replace(b"\r\n", b"\n").split(b"\n"):
        if not line:
            continue
        k, _, v = line.partition(b":")
        if k.lower() == b"status":
            status = v.strip()
        else:
            rh.append((k.strip(), v.strip()))
    return status, rh, b, err[-500:]


def handler(port, tls):
    async def h(r, w):
        sslobj = w.get_extra_info("ssl_object")
        sni = sslobj.server_hostname if sslobj else None
        conn_bytes = 0
        try:
            while True:
                head = await asyncio.wait_for(read_head(r), timeout=30)
                if head is None:
                    break
                conn_bytes += len(head)
                lines = head.split(b"\r\n")
                method, target, _v = lines[0].split(b" ", 2)
                hdrs: dict = {}
                for ln in lines[1:]:
                    if ln:
                        k, _, v = ln.partition(b":")
                        hdrs.setdefault(k.strip().lower(), []).append(v.strip())
                body, wire, chunked, nchunks = await read_body(r, hdrs)
                conn_bytes += wire
                t = target.decode("latin1")
                path, _, query = t.partition("?")
                m = re.match(r"^(/[A-Za-z0-9_-]+\.git)(/.*)$", path)
                err = b""
                if m and (ROOT / m.group(1).lstrip("/")).is_dir():
                    status, rh, rb, err = await cgi(method.decode(), path, query, hdrs, body)
                elif "fakeadv" in path and path.endswith("/info/refs"):
                    status, rh, rb = b"200 OK", [(b"Content-Type", ADV_CT)], b"001e# service=git-upload-pack\n0000"
                else:
                    msg = f"origin-ok port={port} tls={tls}\n".encode()
                    status, rh, rb = b"200 OK", [(b"Content-Type", b"text/plain")], msg
                resp = b"HTTP/1.1 " + status + b"\r\n" + b"".join(k + b": " + v + b"\r\n" for k, v in rh) + \
                    b"Content-Length: " + str(len(rb)).encode() + b"\r\n\r\n" + rb
                w.write(resp)
                await w.drain()
                ct = dict((k.lower(), v) for k, v in rh).get(b"content-type", b"").decode()
                LOG.write(json.dumps({"at": round(time.time(), 3), "port": port, "tls": tls, "sni": sni,
                                      "method": method.decode(), "target": t[:300], "head_bytes": len(head),
                                      "body_wire_bytes": wire, "chunked": chunked, "chunks": nchunks,
                                      "body_decoded_len": len(body), "status": status.decode(), "resp_ct": ct,
                                      "resp_len": len(rb), "cgi_err": err.decode("latin1")[-300:],
                                      "raw_head": repr(head[:6000])}) + "\n")
                LOG.flush()
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionResetError, ValueError) as e:
            LOG.write(json.dumps({"at": round(time.time(), 3), "port": port, "tls": tls, "sni": sni,
                                  "conn_end": f"{type(e).__name__}", "conn_bytes": conn_bytes}) + "\n")
            LOG.flush()
        finally:
            w.close()
    return h


async def main():
    for spec in sys.argv[5:]:
        kind, port = spec.split(":")
        await asyncio.start_server(handler(int(port), kind == "tls"), "127.0.0.1", int(port),
                                   ssl=ctx if kind == "tls" else None, limit=1 << 22)
    print("GIT ORIGIN READY " + " ".join(sys.argv[5:]) + f" pid {os.getpid()}", flush=True)
    await asyncio.sleep(float(sys.argv[4]))


asyncio.run(main())
