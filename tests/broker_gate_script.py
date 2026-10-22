"""The R15 gate script (design 1.3 step 5, row 21): the front's `F` survives a streaming body, proven by the two gates
on a brokered exchange, never by a timing profile. Standalone (no pytest): run as `/usr/bin/python3 -IS <launcher>`,
where the launcher puts this directory, the levain tree and an h11-only tree on `sys.path` and runs this module as
`__main__`; `tests/test_broker_front.py` runs it so. It builds a stand-in keys in the SAME event loop, constructs
the front's exchange with its two gates and drives them.

Cases (each a 5 MiB body with `Content-Length`, the slot reading the response while it is still uploading):
  1 `before_keys_read` held until the stand-in keys has written `F` and half-closed, while the body writer streams
    into keys' drain: the slot reads `F`'s status and keys' drain ends at the front's half-close, not at its bound.
  2 `before_body_write` held while the stand-in writes `F` and closes at once: the slot reads `F`'s status.
  R probe (b)'s write-then-read shape: the reader held until the body writer has failed against a keys that wrote
    `F` and closed at once. The `F` arrived before the write error, so the slot still reads its status: the keys-side
    reader returns every frame it received before it raises the error (an earlier front answered 503 here).
Exit 0 and a last line `GATE PASS` when all three read the `F`'s status.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import tempfile
import time

from levain.broker import gwwire
from levain.broker.front import BrokeredExchange, Front, load_policy, parse_head_conn

BODY = 5 * 1024 * 1024
DRAIN_BOUND_S = 5.0
TOKEN = "A" * 43
POLICY = {"format": "levain-front-session/1", "kind": "interactive", "session": "s-gate", "incarnation": 1,
          "routes": [{"route": "vendor", "type": "brokered",
                      "upstream": {"scheme": "https", "host": "api.vendor.example", "base_path": ""},
                      "requests": {"templates": [{"method": "POST", "path": "/v1/messages"}], "headers": {},
                                   "body_cap": 32 * 1024 * 1024, "body_codec": "none"},
                      "max_call_microdollars": 1}]}


class StandInKeys:
    """keys' front.sock for one exchange: reads `Q`, writes `F token-invalid`, then either half-closes and drains
    (keys' correct form) or closes at once with the body unread."""

    def __init__(self, path: str, mode: str) -> None:
        self.path, self.mode = path, mode
        self.f_sent = asyncio.Event()
        self.drain_end: tuple[str, float, int] | None = None

    async def start(self) -> None:
        self.server = await asyncio.start_unix_server(self._serve, self.path)

    async def _frame(self, reader: asyncio.StreamReader) -> tuple[bytes, int] | None:
        try:
            hdr = await reader.readexactly(5)
        except asyncio.IncompleteReadError:
            return None
        n = int.from_bytes(hdr[1:], "big")
        await reader.readexactly(n)
        return hdr[:1], n

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        first = await self._frame(reader)
        assert first is not None and first[0] == b"Q", first
        writer.write(gwwire.encode(b"F", {"refused": "token-invalid"}))
        await writer.drain()
        if self.mode == "close":
            writer.close()
            self.f_sent.set()
            return
        writer.write_eof()
        self.f_sent.set()
        got, why = 0, "deadline"
        loop = asyncio.get_running_loop()
        end = loop.time() + DRAIN_BOUND_S
        try:
            while loop.time() < end:
                fr = await asyncio.wait_for(self._frame(reader), end - loop.time())
                if fr is None:
                    why = "eof"
                    break
                if fr[0] in (b"E", b"X"):
                    why = fr[0].decode()
                    break
                got += fr[1]
        except (asyncio.TimeoutError, ConnectionError):
            pass
        self.drain_end = (why, time.monotonic(), got)
        writer.close()


async def slot_pair() -> tuple[asyncio.StreamReader, asyncio.StreamWriter, asyncio.StreamReader, asyncio.StreamWriter]:
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    fr, fw = await asyncio.open_unix_connection(sock=a)
    sr, sw = await asyncio.open_unix_connection(sock=b)
    return fr, fw, sr, sw


async def run_case(tmp: str, name: str, mode: str) -> dict:
    keys = StandInKeys(os.path.join(tmp, f"keys-{name}.sock"), mode)
    await keys.start()
    front = Front(load_policy(json.dumps(POLICY).encode()), log_path=os.path.join(tmp, f"front-{name}.log"),
                  keys_path=keys.path)
    fr, fw, sr, sw = await slot_pair()
    head = (b"POST /r/vendor/v1/messages HTTP/1.1\r\nHost: gw\r\nAuthorization: Bearer %s\r\n"
            b"Content-Length: %d\r\n\r\n" % (TOKEN.encode(), BODY))

    # Case 1: the slot sends its first MiB, then holds the rest until it has read the answer, so the body is still
    # streaming (keys still draining) when the read gate opens, whatever the scheduling.
    slot_hold = asyncio.Event()
    if name != "1":
        slot_hold.set()

    async def upload() -> str:
        sw.write(head)
        try:
            for i in range(0, BODY, 65536):
                if i == 1024 * 1024:
                    await slot_hold.wait()
                sw.write(b"x" * 65536)
                await sw.drain()
        except OSError as e:
            return type(e).__name__
        return "sent"

    up = asyncio.ensure_future(upload())
    raw = await fr.readuntil(b"\r\n\r\n")
    conn, req = parse_head_conn(raw)
    read_gate, write_gate = asyncio.Event(), asyncio.Event()
    if name in ("1", "R"):
        write_gate.set()
    else:
        read_gate.set()
    ex = BrokeredExchange(front, front.policy.brokered["vendor"], conn, req, b"/v1/messages", None, fr, fw,
                          before_keys_read=read_gate, before_body_write=write_gate)
    run = asyncio.ensure_future(ex.run())
    released_at = None
    if name == "1":
        await asyncio.wait_for(keys.f_sent.wait(), 10)
        await asyncio.sleep(0.2)   # the writer keeps streaming into keys' drain while the reader is held
        released_at = time.monotonic()
        read_gate.set()
    elif name == "R":
        for _ in range(1000):   # until the body writer has failed against the closed keys
            if "write_error" in ex.rec:
                break
            await asyncio.sleep(0.01)
        read_gate.set()
    resp = await asyncio.wait_for(sr.read(), 15)
    slot_hold.set()
    await asyncio.wait_for(run, 15)
    write_gate.set()
    upload_end = await asyncio.wait_for(up, 15)
    for _ in range(600):
        if keys.drain_end is not None or mode == "close":
            break
        await asyncio.sleep(0.01)
    sw.close()
    fw.close()
    keys.server.close()
    status = resp.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    out = {"case": name, "slot_status_line": status, "upload": upload_end,
           "parked_before_first_write": ex.parked_before_first_write, "write_error": ex.rec.get("write_error"),
           "refused": ex.rec.get("refused"), "discarded": ex.rec.get("discarded")}
    if keys.drain_end is not None:
        why, at, got = keys.drain_end
        out["keys_drain"] = {"ended_by": why, "bytes": got,
                             "after_release_s": round(at - released_at, 3) if released_at else None}
    return out


async def main() -> int:
    tmp = tempfile.mkdtemp(prefix="gate-", dir="/tmp")
    print(f"python {sys.version.split()[0]} flags.isolated={sys.flags.isolated} no_site={sys.flags.no_site}")
    r1 = await run_case(tmp, "1", "drain")
    r2 = await run_case(tmp, "2", "close")
    rr = await run_case(tmp, "R", "close")
    for r in (r1, r2, rr):
        print(json.dumps(r, sort_keys=True))
    d = r1.get("keys_drain") or {}
    ok1 = (r1["slot_status_line"].startswith("HTTP/1.1 401") and r1["parked_before_first_write"] is True
           and d.get("ended_by") == "eof" and d.get("after_release_s") is not None
           and d["after_release_s"] < 1.0)
    ok2 = r2["slot_status_line"].startswith("HTTP/1.1 401") and r2["parked_before_first_write"] in (True, None)
    okr = rr["slot_status_line"].startswith("HTTP/1.1 401") and rr["write_error"] is not None
    print(f"case1 {'PASS' if ok1 else 'FAIL'}  case2 {'PASS' if ok2 else 'FAIL'}  caseR {'PASS' if okr else 'FAIL'}")
    if ok1 and ok2 and okr:
        print("GATE PASS")
        return 0
    print("GATE FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
