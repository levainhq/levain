"""CH-D1 spike ROUND 4 decider. Copy of spike3/tools/decider_spike3.py (strict mode only), round-4 changes marked R4.

usage: decider_spike4.py <out-dir> <seconds> <cap-bytes> -- <glue args (lib_proxy4.mjs)...>

R4-1 (R15-17-1) advertisement gating. A second socket (the glue's --obs-fd) carries one JSON line per allowed
     response. When a `GET <prefix>/info/refs?service=git-upload-pack` was answered 200 with
     `Content-Type: application/x-git-upload-pack-advertisement`, (scheme, host, port, prefix) is recorded in
     ADVERTISED. A `POST <prefix>/git-upload-pack` is read-shaped ONLY when its (scheme, host, port, prefix) is in
     ADVERTISED; otherwise it is a card. NO body parse decides it (the R15-15 deletion stands).
     KEYS: a keyed git route carries a DECLARED REPO SET; the key attaches to the advertisement GET and the advertised
     POST only for a declared prefix. An undeclared prefix on the keyed host is forwarded keyless.
R4-2 (R15-17-2) the cap: an advertised upload-pack POST gets need_body max=<cap-bytes>; srt reads at most that many
     body bytes BEFORE any verdict (nothing is dialed until the verdict); `cut` (more than the cap) -> card.
     With <cap-bytes> = 33554432 (srt's maximum) the run is a DUMP: every body's size, framing and pkt-line summary
     goes to <out>/bodies.jsonl.
R4-3 (R15-17-8) frames.jsonl logs the opaque id, method, host, port, path shape, query NAMES and header NAMES; never
     a path value that is not plain, a query value or a header value. `--log raw` in the glue args = the round-3 log.
Kept from round 3: scheme from `sni`, port walls, strip on every allow, http never keyed, the credential-in-query card,
upgrade / override / receive-pack walls, non-read method -> card. "Card" = deny 403 with reason "card: ...".
"""
from __future__ import annotations

import base64
import datetime
import gzip
import ipaddress
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import zlib
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

LIB = Path(__file__).resolve().parent / "lib_proxy4.mjs"
# CHD2-S1 PATCH: the node binary comes from SPIKE_NODE (the fixture resolves it); no machine path is baked in.
NODE = os.environ.get("SPIKE_NODE", "node")
# R4-1: keyed routes. (scheme, host, port) -> key, declared repo prefixes (None = not a git route: key on every request,
# the round-3 shape for api.rb.test). 9443 = the 443 stand-in.
KEYED = {
    ("https", "api.rb.test", 9443): ("SPIKE-KEY-B1", None),
    ("https", "git.rb.test", 9443): ("SPIKE-KEY-GIT", {"/declared.git"}),
}
ADVERTISED: set = set()
ALOCK = threading.Lock()
ADV_CT = "application/x-git-upload-pack-advertisement"
TLS_PORTS, HTTP_PORTS = [443], [80]
OUT = Path("/tmp")
CAP = 1 << 20
LOGMODE = "safe"
STRIP = ["authorization", "proxy-authorization", "cookie", "x-api-key", "api-key", "x-auth-token"]
SAFE = {"GET", "HEAD", "OPTIONS"}
CRED_Q = re.compile(r"(?<![a-z0-9])(access_token|api_key|apikey|token|key|secret|signature|password)(?![a-z0-9])")
OVERRIDES = {"x-http-method-override", "x-method-override", "x-http-method"}
DUMP_MAX = 32 << 20
GUNZIP_LIMIT = 256 << 20


def fold_name(raw: str) -> str:
    return re.sub(r"[-.]", "_", urllib.parse.unquote_plus(raw).lower())


def nonpublic_literal(host: str) -> bool:
    try:
        return not ipaddress.ip_address(host.strip("[]")).is_global
    except ValueError:
        return False


def seg_shape(s: str) -> str:
    return s if re.fullmatch(r"[A-Za-z0-9._~-]{1,24}", s) and not re.search(r"[0-9a-fA-F]{16,}", s) else f"<seg:{len(s)}>"


def path_shape(p: str) -> str:
    return "/".join(seg_shape(s) if s else "" for s in p.split("/"))


def query_names(q: str) -> list[str]:
    return [seg_shape(urllib.parse.unquote_plus(part.split("=", 1)[0])) for part in q.lstrip("?").split("&") if part]


def make_ca() -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)   # srt (node-forge) refuses an EC CA
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Levain chat egress spike r4")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    return key_pem, cert.public_bytes(serialization.Encoding.PEM)


class Link:
    def __init__(self, sock: socket.socket, log: Path) -> None:
        self.sock, self.log, self.wlock = sock, log.open("a"), threading.Lock()

    def read(self) -> dict | None:
        head = self._exact(4)
        if head is None:
            return None
        (n,) = struct.unpack(">I", head)
        body = self._exact(n)
        msg = json.loads(body)
        self._note("in", msg)
        return msg

    def _exact(self, n: int) -> bytes | None:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def send(self, msg: dict) -> None:
        raw = json.dumps(msg, separators=(",", ":")).encode()
        with self.wlock:
            self.sock.sendall(struct.pack(">I", len(raw)) + raw)
        self._note("out", msg)

    def _note(self, way: str, msg: dict) -> None:
        shown = dict(msg)
        if shown.get("t") == "body":
            shown["data"] = f"<{len(shown.get('data', ''))} b64 chars>"
        if LOGMODE == "safe" and shown.get("t") == "req":      # R4-3
            shown = {"t": "req", "id": msg["id"], "method": msg["method"], "host": msg["host"], "port": msg["port"],
                     "sni": "present" if msg.get("sni") else None, "path": path_shape(msg.get("path", "")),
                     "queryNames": query_names(msg.get("query", "") or ""),
                     "headerNames": sorted((msg.get("headers") or {}).keys())}
        if LOGMODE == "safe" and shown.get("t") == "verdict" and "credential" in shown:
            shown["credential"] = "<held key>"
        self.log.write(json.dumps({"at": round(time.time(), 3), "way": way, "msg": shown}) + "\n")
        self.log.flush()


def pkt_summary(data: bytes) -> dict:
    """Counts of the pkt-lines in a git request body (after any gzip), or why it is not pkt-lines."""
    at, counts, n_lines, maxlen = 0, {}, 0, 0
    while at < len(data):
        if at + 4 > len(data):
            return {"pkt": False, "at": at, **counts}
        try:
            n = int(data[at:at + 4], 16)
        except ValueError:
            return {"pkt": False, "at": at, **counts}
        if n in (0, 1, 2):
            counts[f"special{n}"] = counts.get(f"special{n}", 0) + 1
            at += 4
            continue
        if n < 4 or at + n > len(data):
            return {"pkt": False, "at": at, **counts}
        line = data[at + 4:at + n]
        word = line.split(b" ", 1)[0].split(b"=", 1)[0].rstrip(b"\n").decode("latin1")[:20]
        counts[word] = counts.get(word, 0) + 1
        n_lines += 1
        maxlen = max(maxlen, n)
        at += n
    return {"pkt": True, "lines": n_lines, "max_pkt": maxlen, **counts}


def decoded(data: bytes, enc: list[str]) -> tuple[bytes, str]:
    if enc and enc[0].lower() in ("gzip", "x-gzip"):
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        out = d.decompress(data, GUNZIP_LIMIT)
        return out, "gzip" + ("" if d.eof else "-truncated")
    return data, "identity"


def git_route(scheme: str, host: str, port: int, path: str, query: str) -> tuple[str, str] | None:
    """(kind, prefix) for an upload-pack advertisement GET or exchange POST, else None."""
    if path.endswith("/info/refs") and "service=git-upload-pack" in query.lstrip("?").split("&"):
        return "adv", path[: -len("/info/refs")]
    if path.endswith("/git-upload-pack"):
        return "post", path[: -len("/git-upload-pack")]
    return None


def deny(link: Link, rid: int, reason: str, status: int = 403) -> None:
    link.send({"t": "verdict", "id": rid, "action": "deny", "status": status, "reason": reason})


def decide(link: Link, req: dict, pending: dict) -> None:
    rid, method, host = req["id"], req["method"], req["host"]
    headers = req.get("headers", {})
    query = req.get("query", "") or ""
    qparts = query.lstrip("?").split("&")
    path = req.get("path", "")
    for part in qparts:
        if part.startswith("hold="):
            time.sleep(float(part[5:]))
    with ALOCK:
        drain_obs()           # R4-1b: every decision drains, so the obs socket never backs up into node's write queue
    allow: dict = {"t": "verdict", "id": rid, "action": "allow", "removeHeaders": STRIP}
    port, scheme = req["port"], ("https" if req.get("sni") else "http")
    if (scheme == "https" and port not in TLS_PORTS) or (scheme == "http" and port not in HTTP_PORTS):
        return deny(link, rid, f"wall: {scheme} on port {port}")
    if nonpublic_literal(host):
        return deny(link, rid, "wall: non-public IP literal")
    for part in qparts:
        if part and CRED_Q.search(fold_name(part.split("=")[0])):
            return deny(link, rid, "card: credential in query")
    keyed = KEYED.get((scheme, host, port))        # http is never in KEYED (B12)
    route = git_route(scheme, host, port, path, query)
    if keyed:
        key, repos = keyed
        if repos is None or (route and route[1] in repos):
            allow.update(cred="spike", credential=key)
    if "allow=1" in qparts:                       # R7 (round 2): the copy allows this path whatever the method
        return link.send(allow)
    if "upgrade" in headers:
        return deny(link, rid, "wall: protocol upgrade")
    if OVERRIDES & set(headers) or any(p.split("=")[0] == "_method" for p in qparts):
        return deny(link, rid, "card: method override")
    if path.endswith("/git-receive-pack") or "service=git-receive-pack" in qparts:
        return deny(link, rid, "wall: use Levain's push")
    if method == "POST" and route and route[0] == "post":
        with ALOCK:
            drain_obs()                             # R4-1b
            advertised = (scheme, host, port, route[1]) in ADVERTISED
        if not advertised:
            return deny(link, rid, "card: upload-pack POST with no advertisement this session")
        if headers.get("content-type") != ["application/x-git-upload-pack-request"]:
            return deny(link, rid, "card: upload-pack POST without the request content type")
        pending[rid] = (allow, headers, qparts)
        return link.send({"t": "verdict", "id": rid, "action": "need_body", "max": CAP})
    has_body = any(v not in ("", "0") for v in headers.get("content-length", [])) or "transfer-encoding" in headers
    if method not in SAFE or has_body:
        return deny(link, rid, f"card: {method}")
    link.send(allow)


def on_body(link: Link, rid: int, data: bytes, cut: bool, pending: dict) -> None:
    allow, headers, qparts = pending.pop(rid)
    raw_len = len(data)
    plain, enc = decoded(data, headers.get("content-encoding", []))
    with open(OUT / "bodies.jsonl", "a") as f:
        f.write(json.dumps({"at": round(time.time(), 3), "id": rid, "cap": CAP, "received": raw_len, "cut": cut,
                            "content-length": headers.get("content-length"),
                            "transfer-encoding": headers.get("transfer-encoding"),
                            "content-encoding": headers.get("content-encoding"), "decoded_as": enc,
                            "decoded_len": len(plain), "pkt": pkt_summary(plain) if not cut else None}) + "\n")
    if cut:
        return deny(link, rid, f"card: upload-pack body over the cap ({CAP} bytes)")
    hold = [float(p[9:]) for p in qparts if p.startswith("bodyhold=")]   # R4-2: spool-before-allow timing
    if hold:
        threading.Timer(hold[0], link.send, args=(allow,)).start()
        return
    link.send(allow)


OBS: dict = {"sock": None, "buf": b"", "log": None}


def drain_obs() -> None:
    """R4-1b (round-4 fix of a race measured in run D): read EVERY observation line already in the obs socket's buffer
    before a decision uses ADVERTISED. The glue writes the line at onResponse, before srt relays the response head, so
    the advertisement's line is in this socket's kernel buffer before the client can even send the POST. Run D used a
    reader THREAD instead, and the obs line and the POST's decision landed in the same millisecond (P1.h, P1.k, P2):
    a GIL scheduling order away from a fail-closed card on a real clone. Called with ALOCK held."""
    s = OBS["sock"]
    s.setblocking(False)
    try:
        while True:
            try:
                chunk = s.recv(65536)
            except (BlockingIOError, InterruptedError):
                break
            if not chunk:
                break
            OBS["buf"] += chunk
    finally:
        s.setblocking(True)
    *lines, OBS["buf"] = OBS["buf"].split(b"\n")
    for line in lines:
        if line:
            note_obs(json.loads(line))


def note_obs(m: dict) -> None:
    if OBS["log"] is None:
        OBS["log"] = (OUT / "obs.jsonl").open("a")
    log = OBS["log"]
    if True:
        rec = None
        if m.get("t") == "resp" and m.get("service") == "git-upload-pack" and m["path"].endswith("/info/refs") \
                and m["status"] == 200 and (m.get("ct") or "").split(";")[0].strip().lower() == ADV_CT:
            rec = (m["scheme"], m["host"], m["port"], m["path"][: -len("/info/refs")])
            ADVERTISED.add(rec)                      # ALOCK is held by the caller (drain_obs)
        log.write(json.dumps({"at": round(time.time(), 3), "scheme": m["scheme"], "host": m["host"], "port": m["port"],
                              "path": path_shape(m["path"]), "status": m["status"], "ct": m.get("ct"),
                              "advertised": list(rec) if rec else None}) + "\n")
        log.flush()


def serve(link: Link) -> None:
    pending: dict = {}
    while True:
        msg = link.read()
        if msg is None:
            return
        if msg["t"] == "hello":
            link.send({"t": "hello", "proto": 1, "allowedDomains": [], "deniedDomains": []})
        elif msg["t"] == "req":
            threading.Thread(target=decide, args=(link, msg, pending), daemon=True).start()
        elif msg["t"] == "body":
            on_body(link, msg["id"], base64.b64decode(msg["data"]), bool(msg.get("cut")), pending)


def main() -> None:
    global OUT, TLS_PORTS, HTTP_PORTS, CAP, LOGMODE
    out = OUT = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    CAP = int(sys.argv[3])
    glue_args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    for flag, name in (("--tls-ports", "TLS_PORTS"), ("--http-ports", "HTTP_PORTS")):
        if flag in glue_args:
            globals()[name] = [int(p) for p in glue_args[glue_args.index(flag) + 1].split(",")]
    if "--log" in glue_args:
        LOGMODE = glue_args[glue_args.index("--log") + 1]
    global KEYED
    # CHD2-S1 PATCH: the keyed routes were written for the 9443 stand-in; they follow the TLS stand-in port the run was
    # given (the last of --tls-ports), so a run can use a free port instead of a fixed one.
    KEYED = {(s, h, TLS_PORTS[-1]): v for (s, h, _p), v in KEYED.items()}
    key_pem, cert_pem = make_ca()
    (out / "ca.pem").write_bytes(cert_pem)
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(64)
    port = lsock.getsockname()[1]
    ours, theirs = socket.socketpair()
    obs_ours, obs_theirs = socket.socketpair()
    for s_ in (obs_ours, obs_theirs):                # R4-1b: room for many observation lines between decisions
        s_.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        s_.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    ca_r, ca_w = os.pipe()
    life_r, life_w = os.pipe()
    fds = (lsock.fileno(), theirs.fileno(), ca_r, life_r, obs_theirs.fileno())
    for fd in fds:
        os.set_inheritable(fd, True)
    proc = subprocess.Popen([NODE, str(LIB), "--listen-fd", str(lsock.fileno()), "--decider-fd",
                             str(theirs.fileno()), "--ca-fd", str(ca_r), "--lifeline-fd", str(life_r),
                             "--obs-fd", str(obs_theirs.fileno()), *glue_args],
                            pass_fds=fds, stderr=(out / "proxy.stderr").open("w"),
                            # closed env (B6). CHD2-S1 PATCH: SPIKE_ENV=parent reproduces round 3's C2/C3 runs (node
                            # inherits the parent env), the NEGATIVE CONTROL for B6; unset = the round-4 closed env.
                            env=dict(os.environ) if os.environ.get("SPIKE_ENV") == "parent" else {})
    os.write(ca_w, key_pem + cert_pem)
    os.close(ca_w)
    for fd in (ca_r, life_r):
        os.close(fd)
    theirs.close()
    obs_theirs.close()
    lsock.close()
    threading.Thread(target=serve, args=(Link(ours, out / "frames.jsonl"),), daemon=True).start()
    OBS["sock"] = obs_ours                          # R4-1b: drained at decision time, no reader thread
    print(f"READY {port} pid {proc.pid}", flush=True)
    (out / "port.txt").write_text(str(port))
    time.sleep(float(sys.argv[2]))
    os.close(life_w)
    proc.wait(timeout=10)


if __name__ == "__main__":
    main()
