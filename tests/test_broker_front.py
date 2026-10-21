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
     "upstream": {"scheme": "https", "host": "api.anthropic.com", "base_path": "/"}}]}
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
