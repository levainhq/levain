"""The VM rig's fake vendor (tests/vm_fakes/fire_fakeclaude.c) sends a VALID TLS ClientHello (SNI api.anthropic.com)
before its payload, so the egress front counts the tunnel's bytes (it used to send 20000 bytes of 0x16, which the front
refuses as ``malformed`` / ``tls-hello`` and counts nothing). Compiles the fake, runs it against a stand-in CONNECT
proxy, and checks the bytes it sent: they parse as a ClientHello with the expected SNI, and a real Front counts them."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from levain.broker.front import client_hello_sni, read_client_hello
from tests.test_broker_front import _connect, _with_front

SRC = Path(__file__).parent / "vm_fakes" / "fire_fakeclaude.c"
PAYLOAD = 20000

pytestmark = pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("cc"),
                                reason="the fake uses a macOS call (confstr DARWIN_USER_DIR) and needs a C compiler")


def _run_fake(tmp_path: Path) -> bytes:
    exe = tmp_path / "fake"
    cmd = ["cc", "-O1", "-DHOLD_S=0", "-o", str(exe), str(SRC)]
    if subprocess.run(cmd, capture_output=True, timeout=120).returncode:
        # a host whose default SDK cannot link (seen with a too-new CLT SDK): retry against the other CLT SDKs
        for sdk in sorted(Path("/Library/Developer/CommandLineTools/SDKs").glob("MacOSX2*.sdk"), reverse=True):
            if subprocess.run(cmd, capture_output=True, timeout=120, env={**os.environ, "SDKROOT": str(sdk)}).returncode == 0:
                break
        else:
            subprocess.run(cmd, check=True, timeout=120)   # nothing links: fail loudly with the compiler's output
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got = bytearray()

    def serve() -> None:
        c, _ = srv.accept()
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += c.recv(4096)
        c.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        c.settimeout(2)
        try:
            while chunk := c.recv(65536):
                got.extend(chunk)
        except (socket.timeout, OSError):
            pass
        c.close()

    t = threading.Thread(target=serve)
    t.start()
    env = {"HTTPS_PROXY": f"http://127.0.0.1:{srv.getsockname()[1]}", "TMPDIR": str(tmp_path), "PATH": os.environ["PATH"]}
    subprocess.run([str(exe), "run"], env=env, check=True, timeout=60, capture_output=True)
    t.join(10)
    srv.close()
    return bytes(got)


def test_fake_vendor_sends_a_valid_clienthello_then_the_payload_and_the_front_counts_it(tmp_path):
    sent = _run_fake(tmp_path)

    async def parse():
        r = asyncio.StreamReader()
        r.feed_data(sent)
        r.feed_eof()
        return await read_client_hello(r)

    raw, body = asyncio.run(parse())
    assert client_hello_sni(body) == "api.anthropic.com"
    assert len(sent) == len(raw) + PAYLOAD and sent[len(raw):] == b"\x16" * PAYLOAD

    async def through_front(ask, log: Path, front, up_bytes) -> None:
        status, _calls, _back = await ask(_connect("api.anthropic.com:443"), hello=sent)
        assert status == b"200"
        for _ in range(200):
            if not front.live:
                break
            await asyncio.sleep(0.01)
        recs = [json.loads(ln) for ln in log.read_text().splitlines()]
        assert not [x for x in recs if x.get("event") == "refused"], recs
        t = [x for x in recs if x.get("event") == "tunnel" and x["passthrough"]]
        assert len(t) == 1 and t[0]["bytes_up"] == len(sent) and t[0]["passthrough_total"] > 0
        assert up_bytes[0] == len(sent)

    asyncio.run(_with_front(through_front))
