"""The gateway front's egress proxy (S1 session 3a, CONTRACT §4.1 / §4.4): every refusal happens before any upstream
connection. Real sockets on both sides; the resolver and the upstream's address are fixtures (never the internet)."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

from levain.broker.front import Front, load_policy

PUBLIC = "93.184.215.14"
DNS = {"ok.example": [PUBLIC], "loop.example": ["127.0.0.1"], "ten.example": ["10.0.0.5"],
       "meta.example": ["169.254.169.254"], "six.example": ["::1"], "mapped.example": ["::ffff:127.0.0.1"],
       "mixed.example": [PUBLIC, "192.168.0.1"]}
POLICY = {"format": "levain-front-session/1", "session": "s-test", "routes": [
    {"route": "egress-test", "type": "egress", "hosts": sorted(DNS) + ["rebind.example"]},
    {"route": "pass", "type": "subscription_passthrough", "hosts": ["api.anthropic.com"]}]}


async def _with_front(fn) -> None:
    calls: list[str] = []
    rebind = {"n": 0}

    async def resolve(host: str, port: int):
        if host == "rebind.example":
            rebind["n"] += 1
            return [PUBLIC] if rebind["n"] == 1 else ["127.0.0.1"]
        return DNS.get(host, [PUBLIC])

    async def echo(r, w):
        while data := await r.read(65536):
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

        async def ask(raw: bytes) -> tuple[bytes, int]:
            before = len(calls)
            r, w = await asyncio.open_unix_connection(path)
            w.write(raw)
            await w.drain()
            head = await r.readuntil(b"\r\n\r\n")
            if head.startswith(b"HTTP/1.1 200"):
                w.write(b"ping")
                await w.drain()
                assert await r.readexactly(4) == b"ping"
            w.close()
            return head.split(b" ", 2)[1], len(calls) - before

        try:
            await fn(ask, Path(tmp) / "front.jsonl", front)
        finally:
            server.close()
            front.close_live()
            front.unbind()
            upstream.close()


def _connect(target: str) -> bytes:
    return f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode()


def test_connect_refuses_unsafe_targets():
    async def body(ask, log: Path, front: Front) -> None:
        assert await ask(_connect("ok.example:443")) == (b"200", 1)          # positive control
        assert await ask(_connect("api.anthropic.com:443")) == (b"200", 1)
        for target in ("evil.example:443", "ok.example:22", "93.184.215.14:443", "[::1]:443",
                       "loop.example:443", "ten.example:443", "meta.example:443", "six.example:443",
                       "mapped.example:443", "mixed.example:443"):
            assert await ask(_connect(target)) == (b"403", 0), target
        # DNS rebinding: the first CONNECT uses the address it validated; the second answer (loopback) is refused
        assert await ask(_connect("rebind.example:443")) == (b"200", 1)
        assert await ask(_connect("rebind.example:443")) == (b"403", 0)
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

    async def body(ask, log: Path, front: Front) -> None:
        for raw in corpus:
            assert await ask(raw) == (b"400", 0), raw

    asyncio.run(_with_front(body))
