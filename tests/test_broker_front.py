"""The gateway front's egress proxy (S1 session 3a, CONTRACT §4.1 / §4.4): every refusal happens before any upstream
connection, and a tunnel carries only a TLS session whose SNI is the CONNECT host. Real sockets on both sides; the
resolver and the upstream's address are fixtures (never the internet)."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import ssl
import tempfile
from pathlib import Path

import pytest

from levain.broker.front import (Front, PolicyError, Refused, connect_target, load_policy, parse_head,
                                 refused_address, system_resolve)

PUBLIC = "93.184.215.14"
PUBLIC6 = "2606:2800:21f:cb07:6820:80da:af6b:8b2c"
DNS = {"ok.example": [PUBLIC], "loop.example": ["127.0.0.1"], "ten.example": ["10.0.0.5"],
       "meta.example": ["169.254.169.254"], "six.example": ["::1"], "mapped.example": ["::ffff:127.0.0.1"],
       "mixed.example": [PUBLIC, "192.168.0.1"]}
POLICY = {"format": "levain-front-session/1", "kind": "interactive", "session": "s-test", "routes": [
    {"route": "egress-test", "type": "egress", "hosts": sorted(DNS) + ["rebind.example"]},
    {"route": "pass", "type": "subscription_passthrough", "hosts": ["api.anthropic.com"]}]}


def client_hello(name: str | None) -> bytes:
    """A real OpenSSL ClientHello for SNI `name`: the first bytes a TLS client sends through the tunnel."""
    ctx = ssl.create_default_context()
    inc, out = ssl.MemoryBIO(), ssl.MemoryBIO()
    obj = ctx.wrap_bio(inc, out, server_hostname=name)
    try:
        obj.do_handshake()
    except ssl.SSLWantReadError:
        pass
    return out.read()


async def _with_front(fn) -> None:
    calls: list[str] = []
    up_bytes = [0]
    rebind = {"n": 0}

    async def resolve(host: str, port: int):
        if host == "rebind.example":
            rebind["n"] += 1
            return [PUBLIC] if rebind["n"] == 1 else ["127.0.0.1"]
        return DNS.get(host, [PUBLIC])

    async def echo(r, w):
        while data := await r.read(65536):
            up_bytes[0] += len(data)
            w.write(data)
            await w.drain()
        w.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    up_port = upstream.sockets[0].getsockname()[1]

    async def connect(addr: str, port: int):
        calls.append(addr)
        assert addr == PUBLIC, f"the front asked for {addr}"
        return await asyncio.open_connection("127.0.0.1", up_port)

    with tempfile.TemporaryDirectory() as tmp:
        front = Front(load_policy(json.dumps(POLICY).encode()), log_path=Path(tmp) / "front.jsonl",
                      slot_uid=os.getuid(), resolve=resolve, connect=connect)
        path = str(Path(tmp) / "gw.sock")
        server = await front.serve_unix(path)

        async def ask(raw: bytes, sni: str | None = "", hello: bytes | None = None) -> tuple[bytes, int, bytes]:
            """Send `raw`; on a 200 send a ClientHello (SNI = the CONNECT host unless `sni` names another; `hello`
            replaces it whole) and read what comes back. Returns (status, upstream connections, bytes echoed back
            through the tunnel)."""
            before = len(calls)
            r, w = await asyncio.open_unix_connection(path)
            w.write(raw)
            await w.drain()
            head = await r.readuntil(b"\r\n\r\n")
            back = b""
            if head.startswith(b"HTTP/1.1 200"):
                if hello is None:
                    hello = client_hello(raw.split(b" ")[1].rsplit(b":", 1)[0].decode() if sni == "" else sni)
                w.write(hello)
                await w.drain()
                while len(back) < len(hello) and (chunk := await r.read(65536)):
                    back += chunk
            w.close()
            return head.split(b" ", 2)[1], len(calls) - before, back

        try:
            await fn(ask, Path(tmp) / "front.jsonl", front, up_bytes)
        finally:
            server.close()
            front.close_live()
            front.unbind()
            upstream.close()


def _connect(target: str) -> bytes:
    return f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode()


def test_connect_refuses_unsafe_targets():
    async def body(ask, log: Path, front: Front, up_bytes) -> None:
        assert (await ask(_connect("ok.example:443")))[:2] == (b"200", 1)          # positive control
        assert (await ask(_connect("api.anthropic.com:443")))[:2] == (b"200", 1)
        for target in ("evil.example:443", "ok.example:22", "93.184.215.14:443", "[::1]:443",
                       "loop.example:443", "ten.example:443", "meta.example:443", "six.example:443",
                       "mapped.example:443", "mixed.example:443"):
            assert (await ask(_connect(target)))[:2] == (b"403", 0), target
        # DNS rebinding: the first CONNECT uses the address it validated; the second answer (loopback) is refused
        assert (await ask(_connect("rebind.example:443")))[:2] == (b"200", 1)
        assert (await ask(_connect("rebind.example:443")))[:2] == (b"403", 0)
        for _ in range(200):   # a tunnel's record is written before its handler leaves `live`
            if not front.live:
                break
            await asyncio.sleep(0.01)
        tunnels = [json.loads(ln) for ln in log.read_text().splitlines() if '"tunnel"' in ln]
        assert {(t["host"], t["passthrough"]) for t in tunnels} == {
            ("ok.example", False), ("api.anthropic.com", True), ("rebind.example", False)}

    asyncio.run(_with_front(body))


def test_malformed_corpus_refused():
    corpus = [
        b"POST /r/x/v1/messages HTTP/1.1\r\nHost: a\r\nContent-Length: 5\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
        b"CONNECT ok.example:443 HTTP/1.1\r\nHost: ok.example:443\r\nHost: evil.example:443\r\n\r\n",
        b"POST http://127.0.0.1:47100/r/x/v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 2\r\n\r\n{}",
        b"GET http://ok.example/ HTTP/1.1\r\nHost: ok.example\r\n\r\n",
        b"CONNECT ok.example:443 HTTP/1.1\r\nHost: ok.example:443\r\nContent-Length: 4\r\n\r\nabcd",
        b"CONNECT ok.example:443 HTTP/1.1\r\nHost: ok.example:443\r\nX-A: a\r\n b\r\n\r\n",
        b"CONNECT ok.example:443 HTTP/1.1\nHost: ok.example:443\r\n\r\n",
    ]

    async def body(ask, log: Path, front: Front, up_bytes) -> None:
        for raw in corpus:
            assert (await ask(raw))[:2] == (b"400", 0), raw

    asyncio.run(_with_front(body))


def test_sni_other_than_connect_host_reaches_no_upstream_byte():
    """Reproduced (r1, domain fronting): a CONNECT to an allowed host followed by a ClientHello naming another host
    was relayed, the upstream receiving the whole hello. The upstream connection is opened before the 200 (so an
    unreachable upstream is still answered 502/504); the refused hello leaves it with no byte."""
    async def body(ask, log: Path, front: Front, up_bytes) -> None:
        status, conns, back = await ask(_connect("ok.example:443"), sni="evil.example")
        assert (status, conns, back, up_bytes[0]) == (b"200", 1, b"", 0)
        status, conns, back = await ask(_connect("ok.example:443"))           # positive control, same front
        assert (status, conns) == (b"200", 1) and back and up_bytes[0] == len(back)
        refused = [json.loads(ln) for ln in log.read_text().splitlines() if '"refused"' in ln]
        assert [(r["refused"], r["detail"], r["host"]) for r in refused] == [
            ("host-not-allowed", "sni-mismatch", "ok.example")]

    asyncio.run(_with_front(body))


def test_brokered_upstream_host_trailing_dot_refused():
    """Reproduced (r1): a brokered route whose upstream.host was 'ok.example.' loaded beside a CONNECT route for
    ok.example, so the brokered-host collision check never saw the match."""
    doc = {"format": "levain-front-session/1", "kind": "interactive", "session": "s", "routes": [
        {"route": "eg", "type": "egress", "hosts": ["ok.example"]},
        {"route": "br", "type": "brokered", "upstream": {"scheme": "https", "host": "ok.example.", "base_path": "/"}}]}
    with pytest.raises(PolicyError, match="not a lowercase A-label host name"):
        load_policy(json.dumps(doc).encode())


def test_system_resolve_queries_the_absolute_name(monkeypatch):
    """Reproduced (L3 r1, codex HIGH 1): with `search attacker.example` + `ndots:5` a dotless query for the allowed
    api.vendor.com resolved as api.vendor.com.attacker.example and the front used that host's address."""
    queries: list[str] = []

    def search_list(host, port, family=0, type=0, proto=0, flags=0):
        queries.append(host)
        if not host.endswith(".") and host.count(".") < 5:   # the search suffix is tried first
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.99", port))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, port))]

    monkeypatch.setattr(socket, "getaddrinfo", search_list)
    assert asyncio.run(system_resolve("api.vendor.com", 443)) == [PUBLIC]
    assert queries == ["api.vendor.com."]


def _hello(record_ver: int, suites: bytes, comp: bytes, sni: str) -> bytes:
    name = sni.encode()
    lst = b"\x00" + len(name).to_bytes(2, "big") + name
    ext = b"\x00\x00" + (len(lst) + 2).to_bytes(2, "big") + len(lst).to_bytes(2, "big") + lst
    body = (b"\x03\x03" + b"\x11" * 32 + b"\x00" + len(suites).to_bytes(2, "big") + suites
            + len(comp).to_bytes(1, "big") + comp + len(ext).to_bytes(2, "big") + ext)
    hs = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16" + record_ver.to_bytes(2, "big") + len(hs).to_bytes(2, "big") + hs


def test_invalid_hello_with_matching_sni_reaches_no_upstream_byte():
    """Reproduced (L3 r1, codex MED 4 / complement MED 1): a hello with record version 0x03ff and empty cipher-suite
    and compression vectors, naming the CONNECT host, was accepted and forwarded, opening the unread relay."""
    async def body(ask, log: Path, front: Front, up_bytes) -> None:
        status, conns, back = await ask(_connect("ok.example:443"), hello=_hello(0x03FF, b"", b"", "ok.example"))
        assert (status, conns, back, up_bytes[0]) == (b"200", 1, b"", 0)
        valid = _hello(0x0301, b"\x13\x01", b"\x00", "ok.example")             # the same hello made valid
        status, conns, back = await ask(_connect("ok.example:443"), hello=valid)
        assert (status, back, up_bytes[0]) == (b"200", valid, len(valid))

    asyncio.run(_with_front(body))


# --- the pure functions, row by row: the rows the s3a DONE harness ran through a front process ----------------------

_HARNESS_A = {"format": "levain-front-session/1", "kind": "interactive", "session": "s-fixture-pass", "routes": [
    {"route": "egress-fixture", "type": "egress", "hosts": ["allowed.example", "github.com"]},
    {"route": "claude-pass", "type": "subscription_passthrough", "hosts": ["api.anthropic.com", "claude.ai"]}]}
_HARNESS_B = {"format": "levain-front-session/1", "kind": "interactive", "session": "s-fixture-brokered", "routes": [
    {"route": "egress-fixture", "type": "egress", "hosts": ["allowed.example", "github.com"]},
    {"route": "anthropic", "type": "brokered", "hosts": ["api.anthropic.com"],
     "upstream": {"scheme": "https", "host": "api.anthropic.com", "base_path": ""},
     "requests": {"templates": [{"method": "POST", "path": "/v1/messages?*"}], "headers": {},
                  "body_cap": 1048576, "body_codec": "anthropic-messages"},
     "max_call_microdollars": 2000000}]}
_POL = {"A": load_policy(json.dumps(_HARNESS_A).encode()), "B": load_policy(json.dumps(_HARNESS_B).encode())}


def _call(fn: str, arg):
    """-> ("ok", value) or ("refused", name) or ("policy-error", None)."""
    try:
        if fn == "refused_address":
            return ("ok", refused_address(arg) is not None)
        if fn == "connect_target":
            pol, target = arg
            return ("ok", connect_target(_POL[pol], target)[0].route)
        if fn == "parse_head":
            return ("ok", parse_head(arg).method.decode())
        if fn == "load_policy":
            return ("ok", sorted(load_policy(json.dumps(arg).encode()).connect))
    except Refused as r:
        return ("refused", r.name)
    except PolicyError:
        return ("policy-error", None)
    raise AssertionError(fn)


_C = "CONNECT allowed.example:443 HTTP/1.1\r\nHost: allowed.example:443\r\n"
PURE_ROWS = [
    # refused_address: (address) -> refused?
    *[("refused_address", a, ("ok", True)) for a in (
        "127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "::ffff:127.0.0.1", "fd00::1", "64:ff9b::7f00:1",
        "192.168.1.1", "172.16.0.1")],
    ("refused_address", PUBLIC, ("ok", False)),
    ("refused_address", PUBLIC6, ("ok", False)),
    # connect_target: (policy, target) -> route or refusal
    ("connect_target", ("A", b"allowed.example:443"), ("ok", "egress-fixture")),
    ("connect_target", ("A", b"api.anthropic.com:443"), ("ok", "claude-pass")),
    ("connect_target", ("A", b"evil.example:443"), ("refused", "host-not-allowed")),
    ("connect_target", ("A", b"allowed.example:22"), ("refused", "host-not-allowed")),
    ("connect_target", ("A", b"allowed.example:80"), ("refused", "host-not-allowed")),
    ("connect_target", ("A", b"93.184.215.14:443"), ("refused", "host-not-allowed")),
    ("connect_target", ("A", b"[::1]:443"), ("refused", "host-not-allowed")),
    ("connect_target", ("B", b"api.anthropic.com:443"), ("refused", "host-not-allowed")),
    # parse_head: the malformed corpus, and the two accepted forms
    ("parse_head", b"POST /r/anthropic/v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 5\r\n"
                   b"Transfer-Encoding: chunked\r\n\r\n", ("refused", "malformed")),
    ("parse_head", (_C + "Host: evil.example:443\r\n\r\n").encode(), ("refused", "malformed")),
    ("parse_head", b"GET http://allowed.example/ HTTP/1.1\r\nHost: allowed.example\r\n\r\n", ("refused", "malformed")),
    ("parse_head", b"POST http://127.0.0.1:47100/r/anthropic/v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                   b"Content-Length: 2\r\n\r\n", ("refused", "malformed")),
    ("parse_head", b"OPTIONS * HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n", ("refused", "malformed")),
    ("parse_head", (_C + "Content-Length: 4\r\n\r\n").encode(), ("refused", "malformed")),
    ("parse_head", (_C + "Transfer-Encoding: chunked\r\n\r\n").encode(), ("refused", "malformed")),
    ("parse_head", (_C + "X-A: a\r\n b\r\n\r\n").encode(), ("refused", "malformed")),
    ("parse_head", b"CONNECT allowed.example:443 HTTP/1.1\nHost: allowed.example:443\r\n\r\n", ("refused", "malformed")),
    ("parse_head", (_C + "X-A: a\x00b\r\n\r\n").encode(), ("refused", "malformed")),
    ("parse_head", (_C + "X-Pad: " + "a" * 70000 + "\r\n\r\n").encode(), ("refused", "malformed")),
    ("parse_head", b"CONNECT allowed.example:443 HTTP/1.1\r\n\r\n", ("refused", "malformed")),
    ("parse_head", b"POST /r/anthropic/v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                   b"Transfer-Encoding: gzip, chunked\r\n\r\n", ("refused", "malformed")),
    ("parse_head", (_C + "\r\n").encode(), ("ok", "CONNECT")),
    ("parse_head", b"CONNECT github.com:443 HTTP/1.0\r\n\r\n", ("ok", "CONNECT")),
    ("parse_head", b"POST /r/anthropic/v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 2\r\n\r\n",
     ("ok", "POST")),
    # load_policy: the two harness policies
    ("load_policy", _HARNESS_A, ("ok", ["allowed.example", "api.anthropic.com", "claude.ai", "github.com"])),
    ("load_policy", _HARNESS_B, ("ok", ["allowed.example", "github.com"])),
]


@pytest.mark.parametrize("fn,arg,want", PURE_ROWS, ids=[f"{r[0]}-{i}" for i, r in enumerate(PURE_ROWS)])
def test_pure_function_rows(fn, arg, want):
    assert _call(fn, arg) == want


# --- the R15 gate script, under the root interpreter ----------------------------------------------------------------

_ROOT_PYTHON = "/usr/bin/python3"


def test_refusal_survives_streaming_body_gates(tmp_path):
    """Reproduced (3b-1a, probe (b)'s write-then-read shape, the script's case R): a front whose keys reader resumes
    only after its body writer failed answered 503, never the 401 keys sent, though the `F` had already arrived
    (3b-1a-fix1: the whole frame sat in the reader's buffer). The gate script proves the `F` is kept under both
    gates and in case R; it runs as the root interpreter runs the gateway, isolated, from a launcher."""
    import shutil
    import subprocess
    import sys

    import h11

    if not os.path.exists(_ROOT_PYTHON):
        pytest.skip(f"no {_ROOT_PYTHON} on this host")
    h11_tree = tmp_path / "h11-only"
    shutil.copytree(Path(h11.__file__).parent, h11_tree / "h11", ignore=shutil.ignore_patterns("__pycache__"))
    here = Path(__file__).resolve().parent
    trees = [here, here.parent, h11_tree]
    launcher = tmp_path / "gate_launcher.py"
    launcher.write_text("import sys\n" + "".join(f"sys.path.insert(0, {str(t)!r})\n" for t in trees)
                        + "import runpy\nrunpy.run_module('broker_gate_script', run_name='__main__', alter_sys=True)\n")
    probe = subprocess.run([_ROOT_PYTHON, "-IS", "-c", "import sys; print(sys.version_info >= (3, 9))"],
                           capture_output=True, text=True, timeout=30)
    if probe.stdout.strip() != "True":
        pytest.skip(f"{_ROOT_PYTHON} is older than the gateway's 3.9 floor")
    out = subprocess.run([_ROOT_PYTHON, "-IS", str(launcher)], capture_output=True, text=True, timeout=120)
    sys.stdout.write(out.stdout + out.stderr)
    assert out.returncode == 0 and out.stdout.strip().endswith("GATE PASS"), out.stdout + out.stderr


# --- the brokered exchange against a stand-in keys -------------------------------------------------------------------

_TOKEN = "A" * 43
_EX_POLICY = {"format": "levain-front-session/1", "kind": "interactive", "session": "s-ex", "routes": [
    {"route": "vendor", "type": "brokered", "upstream": {"scheme": "https", "host": "api.vendor.example",
                                                         "base_path": ""},
     "requests": {"templates": [{"method": "POST", "path": "/v1/messages"}], "headers": {"content-type": ".*"},
                  "body_cap": 32 * 1024 * 1024, "body_codec": "none"},
     "max_call_microdollars": 1}]}


async def _answering_keys(path: str, seen: list):
    """keys' front.sock for one exchange: records `Q`, answers `A`, `R 200`, one `b`, `e` at once, then reads
    whatever the front still sends until its `E` / `X` / EOF."""
    from levain.broker import gwwire

    async def frame(r):
        try:
            hdr = await r.readexactly(5)
            return hdr[:1], await r.readexactly(int.from_bytes(hdr[1:], "big"))
        except (asyncio.IncompleteReadError, ConnectionError):
            return None

    async def serve(r, w):
        first = await frame(r)
        seen.append(json.loads(first[1]))
        for f in (gwwire.encode(b"A", {}), gwwire.encode(b"R", {"v": 1, "status": 200, "headers": []}),
                  gwwire.encode(b"b", b"early-ok"), gwwire.encode(b"e")):
            w.write(f)
        await w.drain()
        while (fr := await frame(r)) is not None and fr[0] not in (b"E", b"X"):
            pass
        w.close()

    return await asyncio.start_unix_server(serve, path)


async def _exchange(slot_pair, head: bytes, body: bytes) -> tuple[dict, bytes, list]:
    """One brokered exchange, the front's side driven directly; the slot writes its whole body BEFORE it reads."""
    from levain.broker.front import BrokeredExchange, parse_head_conn

    seen: list = []
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        keys = await _answering_keys(os.path.join(tmp, "k.sock"), seen)
        front = Front(load_policy(json.dumps(_EX_POLICY).encode()), log_path=os.path.join(tmp, "f.log"),
                      keys_path=os.path.join(tmp, "k.sock"))
        fr, fw, sr, sw = await slot_pair()

        async def slot() -> bytes:
            sw.write(head)
            try:
                for i in range(0, len(body), 65536):
                    sw.write(body[i:i + 65536])
                    await sw.drain()
                return await asyncio.wait_for(sr.read(), 20)
            except OSError as e:
                return type(e).__name__.encode()

        s = asyncio.ensure_future(slot())
        conn, req = parse_head_conn(await fr.readuntil(b"\r\n\r\n"))
        ex = BrokeredExchange(front, front.policy.brokered["vendor"], conn, req, req.target[9:], None, fr, fw)
        await asyncio.wait_for(ex.run(), 30)
        fw.close()   # as Front._handle does once the exchange returns
        got = await asyncio.wait_for(s, 30)
        sw.close()
        keys.close()
    return ex.rec, got, seen


async def _tcp_slot_pair():
    accepted: asyncio.Future = asyncio.get_running_loop().create_future()
    server = await asyncio.start_server(lambda r, w: accepted.set_result((r, w)), "127.0.0.1", 0)
    sr, sw = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
    fr, fw = await accepted
    server.close()
    return fr, fw, sr, sw


async def _unix_slot_pair():
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    fr, fw = await asyncio.open_unix_connection(sock=a)
    sr, sw = await asyncio.open_unix_connection(sock=b)
    return fr, fw, sr, sw


def test_early_response_is_not_reset_by_the_unread_body():
    """Reproduced (3b-1a-fix1, codex r1 HIGH 2): keys answers `R 200 ... e` while a 5 MiB body is still arriving; the
    front used to close over the unread body, and a TCP slot that uploads before it reads got a broken pipe and no
    response. Now the front discards the rest of the body after the response, then closes."""
    body = b"x" * (5 * 1024 * 1024)
    head = (b"POST /r/vendor/v1/messages HTTP/1.1\r\nHost: gw\r\nAuthorization: Bearer %s\r\n"
            b"Content-Length: %d\r\n\r\n" % (_TOKEN.encode(), len(body)))
    rec, got, _ = asyncio.run(_exchange(_tcp_slot_pair, head, body))
    assert got.startswith(b"HTTP/1.1 200 OK\r\n") and b"early-ok" in got, got[:200]
    assert rec["end"] == "ok" and rec["discarded"] > 0, rec


def test_connection_keep_alive_is_dropped_not_refused():
    """Reproduced (3b-1a-fix1, complement r1 9): `Connection: keep-alive`, which common clients send, was refused 400
    `malformed`. It is a connection option: dropped, never forwarded, the request served."""
    head = (b"POST /r/vendor/v1/messages HTTP/1.1\r\nHost: gw\r\nAuthorization: Bearer %s\r\n"
            b"Connection: keep-alive\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n" % _TOKEN.encode())
    rec, got, seen = asyncio.run(_exchange(_unix_slot_pair, head, b"{}"))
    assert got.startswith(b"HTTP/1.1 200 OK\r\n"), got[:200]
    assert [q["headers"] for q in seen] == [[["content-type", "application/json"]]], seen
    assert "refused" not in rec and "connection" not in rec["dropped"], rec


def test_slot_reset_mid_body_ends_the_exchange_and_tells_keys():
    """Reproduced (3b-1a-fix2, complement r2 MED): the slot RESETS mid-upload while keys holds the request open
    without answering. The slot's read error was taken for a keys write error and the front waited on keys with no
    bound. Now it is the slot gone: keys gets `X` and the exchange ends `slot-gone`."""
    import struct

    from levain.broker.front import BrokeredExchange, parse_head_conn

    async def go() -> tuple[dict, list]:
        got: list = []

        async def serve(r, w):   # keys that never answers: reads frames until `X` / `E` / EOF
            while True:
                try:
                    hdr = await r.readexactly(5)
                    await r.readexactly(int.from_bytes(hdr[1:], "big"))
                except (asyncio.IncompleteReadError, ConnectionError):
                    break
                got.append(hdr[:1])
                if hdr[:1] in (b"E", b"X"):
                    break
            w.close()

        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            path = os.path.join(tmp, "k.sock")
            keys = await asyncio.start_unix_server(serve, path)
            front = Front(load_policy(json.dumps(_EX_POLICY).encode()), log_path=os.path.join(tmp, "f.log"),
                          keys_path=path)
            fr, fw, sr, sw = await _tcp_slot_pair()
            sw.write(b"POST /r/vendor/v1/messages HTTP/1.1\r\nHost: gw\r\nAuthorization: Bearer %s\r\n"
                     b"Content-Length: %d\r\n\r\n" % (_TOKEN.encode(), 5 * 1024 * 1024) + b"x" * 65536)
            await sw.drain()
            conn, req = parse_head_conn(await fr.readuntil(b"\r\n\r\n"))
            ex = BrokeredExchange(front, front.policy.brokered["vendor"], conn, req, req.target[9:], None, fr, fw)
            run = asyncio.ensure_future(ex.run())
            await asyncio.sleep(0.3)
            sw.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            sw.transport.abort()   # an RST, not a FIN
            await asyncio.wait_for(run, 10)
            fw.close()
            await asyncio.sleep(0.1)
            keys.close()
        return ex.rec, got

    rec, got = asyncio.run(go())
    assert rec["end"] == "slot-gone" and rec.get("slot_error"), rec
    assert got and got[-1] == b"X", got


_WS_POLICY = {"format": "levain-front-session/1", "kind": "interactive", "session": "s-ws", "routes": [
    {"route": "ws", "type": "brokered", "upstream": {"scheme": "https", "host": "ws.vendor.example", "base_path": ""},
     "requests": {"templates": [{"method": "GET", "path": "/v1/live", "upgrade": "websocket"}],
                  "headers": {"sec-websocket-key": ".*", "sec-websocket-version": ".*"},
                  "body_cap": 1024, "body_codec": "none"},
     "max_call_microdollars": 1}]}


@pytest.mark.parametrize("close", ["rst", "fin"])
def test_websocket_slot_close_ends_the_exchange_and_tells_keys(close):
    """Reproduced (3b-1a-fix3, complement r3): on an established websocket the slot RESETS while keys stays silent.
    The pump died on the slot's read error, keys never heard, and the relay waited on keys with no bound. Now keys
    gets `E` and the exchange ends `slot-gone`. Reproduced (3b-1a-fix4, codex r4): the same wait on a CLEAN slot
    close (a FIN): keys got `E` but the relay never ended. Now it ends `slot-eof`, with no `slot_error`."""
    import base64
    import struct

    from levain.broker import gwwire
    from levain.broker.front import BrokeredExchange, parse_head_conn

    async def go() -> tuple[dict, list]:
        got: list = []

        async def serve(r, w):   # keys: `A`, `U`, then silent; records every frame type until EOF
            w.write(gwwire.encode(b"A", {}) + gwwire.encode(b"U", {"v": 1, "headers": []}))
            while True:
                try:
                    hdr = await r.readexactly(5)
                    await r.readexactly(int.from_bytes(hdr[1:], "big"))
                except (asyncio.IncompleteReadError, ConnectionError):
                    break
                got.append(hdr[:1])
            w.close()

        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            path = os.path.join(tmp, "k.sock")
            keys = await asyncio.start_unix_server(serve, path)
            front = Front(load_policy(json.dumps(_WS_POLICY).encode()), log_path=os.path.join(tmp, "f.log"),
                          keys_path=path)
            fr, fw, sr, sw = await _tcp_slot_pair()
            sw.write(b"GET /r/ws/v1/live HTTP/1.1\r\nHost: gw\r\nAuthorization: Bearer %s\r\nConnection: Upgrade\r\n"
                     b"Upgrade: websocket\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
                     % (_TOKEN.encode(), base64.b64encode(b"k" * 16)))
            await sw.drain()
            conn, req = parse_head_conn(await fr.readuntil(b"\r\n\r\n"))
            ex = BrokeredExchange(front, front.policy.brokered["ws"], conn, req, b"/v1/live", None, fr, fw)
            run = asyncio.ensure_future(ex.run())
            assert (await asyncio.wait_for(sr.readuntil(b"\r\n\r\n"), 10)).startswith(b"HTTP/1.1 101 ")
            sw.write(b"\x81\x05hello")
            await sw.drain()
            await asyncio.sleep(0.3)
            if close == "rst":
                sw.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                sw.transport.abort()   # an RST, not a FIN
            else:
                sw.write_eof()         # a FIN
            await asyncio.wait_for(run, 10)
            fw.close()
            sw.close()
            await asyncio.sleep(0.1)
            keys.close()
        return ex.rec, got

    rec, got = asyncio.run(go())
    if close == "rst":
        assert rec["end"] == "slot-gone" and rec.get("slot_error"), rec
    else:
        assert rec["end"] == "slot-eof" and "slot_error" not in rec, rec
    assert b"D" in got and got[got.index(b"D") + 1:][:1] == [b"E"], got


def test_websocket_handshake_over_http_1_0_is_malformed():
    """Reproduced (3b-1a-fix2, codex r2 6): a complete websocket handshake sent as HTTP/1.0 was forwarded as
    `upgrade: websocket`. RFC 6455 §4.1 requires HTTP/1.1 or later: refused `malformed` before keys."""
    import base64

    from levain.broker.front import BrokeredExchange, parse_head_conn

    async def go() -> tuple[dict, bytes]:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            # no keys listens here: a request that reached keys would be refused 503, not 400
            front = Front(load_policy(json.dumps(_WS_POLICY).encode()), log_path=os.path.join(tmp, "f.log"),
                          keys_path=os.path.join(tmp, "absent.sock"))
            fr, fw, sr, sw = await _unix_slot_pair()
            sw.write(b"GET /r/ws/v1/live HTTP/1.0\r\nHost: gw\r\nAuthorization: Bearer %s\r\nConnection: Upgrade\r\n"
                     b"Upgrade: websocket\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
                     % (_TOKEN.encode(), base64.b64encode(b"k" * 16)))
            await sw.drain()
            conn, req = parse_head_conn(await fr.readuntil(b"\r\n\r\n"))
            ex = BrokeredExchange(front, front.policy.brokered["ws"], conn, req, b"/v1/live", None, fr, fw)
            await asyncio.wait_for(ex.run(), 10)
            fw.close()
            got = await asyncio.wait_for(sr.read(), 10)
            sw.close()
        return ex.rec, got

    rec, got = asyncio.run(go())
    assert got.startswith(b"HTTP/1.1 400 "), got[:200]
    assert rec["refused"] == "malformed" and rec["detail"] == "websocket-version", rec
