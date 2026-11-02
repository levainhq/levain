"""P7-A2: the front's byte cap and deadline on passthrough tunnels, and the policy file's rule for them. Real sockets;
the upstream is a local echo behind a fixture resolver (never the internet)."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

import pytest

from levain.broker import norm
from levain.broker.front import Front, PolicyError, load_policy
from levain.slot.render import front_limit_fields
from tests.test_broker_front import PUBLIC, _connect, client_hello

ROUTES = [{"route": "egress-test", "type": "egress", "hosts": ["ok.example"]},
          {"route": "pass", "type": "subscription_passthrough", "hosts": ["api.anthropic.com"]}]


def policy(kind: str = "dispatch", **extra) -> bytes:
    return json.dumps({"format": "levain-front-session/1", "kind": kind, "session": "s-test", "routes": ROUTES,
                       **extra}).encode()


# --- load_policy -----------------------------------------------------------------------------------------------

def test_dispatch_passthrough_needs_a_positive_int_cap():
    assert load_policy(policy(tunnel_bytes_cap=1000)).tunnel_bytes_cap == 1000
    assert load_policy(policy("planner", tunnel_bytes_cap=1)).tunnel_bytes_cap == 1
    with pytest.raises(PolicyError, match="only with a tunnel_bytes_cap"):
        load_policy(policy())                                   # absent
    for bad in (0, -5, True, False, 1.5, 10.0, "100", None, [1], {"n": 1}):
        with pytest.raises(PolicyError, match="tunnel_bytes_cap must be a positive integer"):
            load_policy(policy(tunnel_bytes_cap=bad))
    for bad in (0, -1, True, "9", None, [1]):                  # deadline: validated when present
        with pytest.raises(PolicyError, match="deadline must be"):
            load_policy(policy(tunnel_bytes_cap=1000, deadline=bad))
    for bad in ("Infinity", "NaN"):                            # non-finite numbers, as JSON text
        with pytest.raises(PolicyError, match="deadline must be"):
            load_policy(policy(tunnel_bytes_cap=1000).replace(b"}", b', "deadline": ' + bad.encode() + b"}"))
    assert load_policy(policy(tunnel_bytes_cap=1000, deadline=1_900_000_000)).deadline == 1_900_000_000
    # a non-dispatch session keeps loading without a cap; a present bad cap still refuses
    assert load_policy(policy("interactive")).tunnel_bytes_cap is None
    with pytest.raises(PolicyError):
        load_policy(policy("interactive", tunnel_bytes_cap=0))
    # a dispatch session with only an egress route needs no cap
    only_egress = json.dumps({"format": "levain-front-session/1", "kind": "dispatch", "session": "s",
                              "routes": ROUTES[:1]}).encode()
    assert load_policy(only_egress).tunnel_bytes_cap is None


def test_norm_names():
    for n in ("passthrough-bytes-exhausted", "passthrough-deadline"):
        assert norm.status_of(n) == 403 and norm.is_name(n)


def test_render_limit_fields_round_trip_through_the_front_parser():
    out = front_limit_fields(4096, 1_900_000_000)
    assert out == {"tunnel_bytes_cap": 4096, "deadline": 1_900_000_000}
    assert front_limit_fields() == {} and front_limit_fields(None, None) == {}
    p = load_policy(policy(**out))
    assert (p.tunnel_bytes_cap, p.deadline) == (4096, 1_900_000_000)
    for cap in (0, -1, True, 1.0, "1"):
        with pytest.raises(Exception, match="front-limit-invalid:tunnel-bytes-cap"):
            front_limit_fields(cap)
    for dl in (0, -1, True, 1.5, "1"):
        with pytest.raises(Exception, match="front-limit-invalid:deadline"):
            front_limit_fields(None, dl)


# --- the running front -----------------------------------------------------------------------------------------

async def _run(pol: bytes, fn, *, silent: bool = False, seen: dict | None = None, on_connect=None) -> None:
    """``silent``: the upstream reads and never answers (else it echoes). ``seen``: filled with the bytes the upstream
    actually received (``"up"``) and ``"eof"`` once its side ended. ``on_connect``: called as the front opens the
    upstream, before it answers the CONNECT."""
    seen = {} if seen is None else seen
    seen.update(up=0, eof=0)

    async def resolve(host: str, port: int):
        return [PUBLIC]

    async def echo(r, w):
        try:
            while data := await r.read(65536):
                seen["up"] += len(data)
                if not silent:
                    w.write(data)
                    await w.drain()
        except (ConnectionError, OSError):
            pass
        seen["eof"] += 1
        w.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    up_port = upstream.sockets[0].getsockname()[1]

    async def connect(addr, port):
        if on_connect is not None:
            on_connect()
        return await asyncio.open_connection("127.0.0.1", up_port)

    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "front.log"
        front = Front(load_policy(pol), log_path=log, slot_uid=os.getuid(), resolve=resolve, connect=connect)
        path = str(Path(tmp) / "gw.sock")
        server = await front.serve_unix(path)
        front._test_path = [path]   # for tests that drive a CONNECT by hand (_raw_200)

        async def open_tunnel(host: str = "api.anthropic.com"):
            """-> (status, head, reader, writer); on a 200 the ClientHello is already sent."""
            r, w = await asyncio.open_unix_connection(path)
            w.write(_connect(f"{host}:443"))
            await w.drain()
            head = await r.readuntil(b"\r\n\r\n")
            status = int(head.split(b" ", 2)[1])
            if status == 200:
                w.write(client_hello(host))
                await w.drain()
            return status, head, r, w

        try:
            await fn(open_tunnel, front, log)
        finally:
            server.close()
            front.close_live()
            front.unbind()
            upstream.close()


def _records(log: Path) -> list:
    return [json.loads(ln) for ln in log.read_text().splitlines()]


async def _settled(front: Front) -> None:
    for _ in range(300):
        if not front.live:
            return
        await asyncio.sleep(0.01)


async def _drain_to_eof(r: asyncio.StreamReader) -> None:
    data = b"x"
    while data:
        data = await asyncio.wait_for(r.read(65536), 5)


def test_no_cap_counts_and_logs_the_total_but_never_refuses():
    """The accounting baseline: an ordinary (non-dispatch) passthrough tunnel moves bytes both ways; each tunnel record
    carries the running total."""
    async def body(open_tunnel, front: Front, log: Path) -> None:
        status, _, r, w = await open_tunnel()
        assert status == 200
        hello_len = len(client_hello("api.anthropic.com"))
        w.write(b"x" * 5000)
        await w.drain()
        got = b""
        while len(got) < hello_len + 5000:
            got += await r.read(65536)
        w.close()
        await _settled(front)
        t = [x for x in _records(log) if x.get("event") == "tunnel"][-1]
        assert t["passthrough"] is True and t["bytes_up"] == t["bytes_down"] == hello_len + 5000
        assert t["passthrough_total"] == front.pt_total == 2 * (hello_len + 5000)
        print("BASELINE tunnel record:", {k: t[k] for k in ("bytes_up", "bytes_down", "end", "passthrough_total")})

    asyncio.run(_run(policy("interactive"), body))


def test_cap_closes_open_tunnels_and_refuses_the_next_connect():
    """The cap is the point: a tunnel moving bytes past it is closed, the total never passes the cap, and the next
    passthrough CONNECT is refused by name. An egress tunnel is neither counted nor refused."""
    hello_len = len(client_hello("api.anthropic.com"))
    cap = 3 * hello_len + 10_000

    async def body(open_tunnel, front: Front, log: Path) -> None:
        status, _, r, w = await open_tunnel()
        assert status == 200
        status2, _, r2, w2 = await open_tunnel()       # a second open tunnel, which the overrun must close too
        assert status2 == 200
        sent = 0
        try:
            for _ in range(200):                       # up to 200 x 4 KiB, far past the cap
                w.write(b"y" * 4096)
                await w.drain()
                sent += 4096
                await asyncio.sleep(0)
                if front.pt_exhausted:
                    break
        except (ConnectionError, OSError):
            pass
        for _ in range(300):
            if front.pt_exhausted:
                break
            await asyncio.sleep(0.01)
        assert front.pt_exhausted, "the cap never tripped"
        await _settled(front)
        assert front.pt_total <= cap, (front.pt_total, cap)       # a bound, not a brake
        for rd in (r, r2):                                        # both tunnels are closed: reads end in EOF
            await _drain_to_eof(rd)
        status3, head3, _, w3 = await open_tunnel()               # the next passthrough CONNECT is refused by name
        assert status3 == 403 and b"X-Levain-Refused: passthrough-bytes-exhausted" in head3
        w3.close()
        status4, _, _, w4 = await open_tunnel("ok.example")       # an egress CONNECT is still admitted
        assert status4 == 200
        w4.close()
        await _settled(front)
        recs = _records(log)
        ends = [x for x in recs if x.get("event") == "tunnel" and x["passthrough"]]
        assert [x["end"] for x in ends] == ["bytes-exhausted"] * 2
        assert max(x["passthrough_total"] for x in ends) == front.pt_total <= cap
        assert any(x.get("event") == "passthrough-bytes-exhausted" for x in recs)
        print("RUN: cap", cap, "total", front.pt_total, "sent", sent, "|", head3.split(b"\r\n")[0].decode(),
              "X-Levain-Refused: passthrough-bytes-exhausted |", [x["end"] for x in ends])

    asyncio.run(_run(policy(tunnel_bytes_cap=cap), body))


def test_a_hello_past_the_cap_is_never_forwarded():
    async def body(open_tunnel, front: Front, log: Path) -> None:
        status, _, r, w = await open_tunnel()
        assert status == 200
        assert await asyncio.wait_for(r.read(65536), 5) == b""      # closed, no byte echoed
        await _settled(front)
        assert front.pt_total == 0 and front.pt_exhausted
        status2, head2, _, _ = await open_tunnel()
        assert status2 == 403 and b"passthrough-bytes-exhausted" in head2

    asyncio.run(_run(policy(tunnel_bytes_cap=10), body))


def test_deadline_refuses_connects_and_ends_open_tunnels():
    async def body(open_tunnel, front: Front, log: Path) -> None:
        status, _, r, w = await open_tunnel()
        assert status == 200
        await asyncio.sleep(max(front.policy.deadline - time.time(), 0) + 0.3)   # it passes while the tunnel is open
        await _drain_to_eof(r)                                  # the open tunnel was closed at the deadline
        status2, head2, _, _ = await open_tunnel()
        assert status2 == 403 and b"X-Levain-Refused: passthrough-deadline" in head2
        status3, _, _, w3 = await open_tunnel("ok.example")     # egress ignores it
        assert status3 == 200
        w3.close()
        await _settled(front)
        ends = [x["end"] for x in _records(log) if x.get("event") == "tunnel" and x["passthrough"]]
        assert ends == ["deadline"]

    asyncio.run(_run(policy(tunnel_bytes_cap=10**9, deadline=int(time.time()) + 2), body))


def test_a_future_deadline_and_ample_cap_change_nothing():
    async def body(open_tunnel, front: Front, log: Path) -> None:
        status, _, r, w = await open_tunnel()
        assert status == 200
        w.write(b"z" * 100)
        await w.drain()
        assert len(await asyncio.wait_for(r.read(65536), 5)) > 0
        w.close()
        assert not front.pt_exhausted
        await _settled(front)

    asyncio.run(_run(policy(tunnel_bytes_cap=10**9, deadline=int(time.time()) + 3600), body))


# --- L3 r1: the limits hold at the forward, not only at the read --------------------------------------------------

class _Clock:
    """The front's wall clock, stepped by the test: replaces ``levain.broker.front.time``."""

    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


HELLO_LEN = len(client_hello("api.anthropic.com"))


async def _until(pred, what: str) -> None:
    for _ in range(500):
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


async def _read_n(r: asyncio.StreamReader, n: int) -> bytes:
    got = b""
    while len(got) < n:
        data = await asyncio.wait_for(r.read(65536), 5)
        assert data, (len(got), n)
        got += data
    return got


async def _raw_200(path_holder: list, front: Front, host: str = "api.anthropic.com"):
    """A CONNECT answered 200 whose ClientHello the test sends (or withholds) itself."""
    r, w = await asyncio.open_unix_connection(path_holder[0])
    w.write(_connect(f"{host}:443"))
    await w.drain()
    head = await r.readuntil(b"\r\n\r\n")
    return int(head.split(b" ", 2)[1]), head, r, w


def _tunnels(log: Path) -> list:
    return [x for x in _records(log) if x.get("event") == "tunnel" and x["passthrough"]]


def test_a_chunk_read_across_the_deadline_is_never_forwarded(monkeypatch):
    """codex HIGH: the clock passes the deadline while a relay waits in its read; the chunk that read returns is dropped
    in both directions, never forwarded."""
    import levain.broker.front as front_mod
    deadline = 1_900_000_000
    clock = _Clock(deadline - 100.0)
    monkeypatch.setattr(front_mod, "time", clock)
    seen: dict = {}

    async def body(open_tunnel, front: Front, log: Path) -> None:
        status, _, r, w = await open_tunnel()
        assert status == 200
        await _read_n(r, HELLO_LEN)                  # the hello's echo: both relay directions now wait in a read
        clock.now = deadline + 1.0                   # the deadline passes between the read and the forward
        w.write(b"P" * 1000)
        await w.drain()
        assert await asyncio.wait_for(r.read(65536), 5) == b""      # closed, the payload never echoed
        await _settled(front)
        t = _tunnels(log)[-1]
        assert t["end"] == "deadline" and t["bytes_up"] == HELLO_LEN, t
        assert seen["up"] == HELLO_LEN, seen        # the upstream never saw the post-deadline chunk

    asyncio.run(_run(policy(tunnel_bytes_cap=10**9, deadline=deadline), body, seen=seen))


def test_a_deadline_passing_while_the_upstream_opens_refuses_the_connect(monkeypatch):
    import levain.broker.front as front_mod
    deadline = 1_900_000_000
    clock = _Clock(deadline - 100.0)
    monkeypatch.setattr(front_mod, "time", clock)
    seen: dict = {}

    def late() -> None:
        clock.now = deadline + 1.0                   # admitted before the open, past the deadline after it

    async def body(open_tunnel, front: Front, log: Path) -> None:
        status, head, _, w = await open_tunnel()
        assert status == 403 and b"X-Levain-Refused: passthrough-deadline" in head, head
        w.close()
        await _settled(front)
        await asyncio.sleep(0.05)
        assert seen["up"] == 0 and not _tunnels(log)

    asyncio.run(_run(policy(tunnel_bytes_cap=10**9, deadline=deadline), body, seen=seen, on_connect=late))


def test_a_deadline_passing_before_the_hello_keeps_the_hello(monkeypatch):
    import levain.broker.front as front_mod
    deadline = 1_900_000_000
    clock = _Clock(deadline - 100.0)
    monkeypatch.setattr(front_mod, "time", clock)
    seen: dict = {}

    async def body(open_tunnel, front: Front, log: Path) -> None:
        status, _, r, w = await _raw_200(front._test_path, front)
        assert status == 200
        clock.now = deadline + 1.0                   # after the 200, before the hello
        w.write(client_hello("api.anthropic.com"))
        await w.drain()
        assert await asyncio.wait_for(r.read(65536), 5) == b""
        await _settled(front)
        assert seen["up"] == 0, seen
        refused = [x for x in _records(log) if x.get("event") == "refused"]
        assert refused and refused[-1]["refused"] == "passthrough-deadline", refused

    asyncio.run(_run(policy(tunnel_bytes_cap=10**9, deadline=deadline), body, seen=seen))


def test_the_chunk_that_lands_exactly_on_the_cap_is_forwarded_and_counted():
    """codex MED: pt_total == cap - 4096 and a 4096-byte read. The chunk fits, so it is forwarded whole, and every count
    (the tunnel record, the session total) equals what the upstream actually received."""
    seen: dict = {}

    async def body(open_tunnel, front: Front, log: Path) -> None:
        status, _, r, w = await open_tunnel()
        assert status == 200
        await _until(lambda: seen["up"] == HELLO_LEN, "the hello upstream")
        cap = front.policy.tunnel_bytes_cap
        front.pt_total = cap - 4096                  # the session has 4096 bytes left
        w.write(b"Q" * 4096)
        await w.drain()
        await _until(lambda: seen["eof"], "the upstream to see the tunnel close")
        await _settled(front)
        t = _tunnels(log)[-1]
        assert seen["up"] == HELLO_LEN + 4096, (seen, t)
        assert t["bytes_up"] == seen["up"] and t["end"] == "bytes-exhausted", t
        assert front.pt_total == cap and front.pt_exhausted
        ex = [x for x in _records(log) if x.get("event") == "passthrough-bytes-exhausted"]
        assert ex and ex[0]["total"] == cap

    asyncio.run(_run(policy(tunnel_bytes_cap=10**6), body, silent=True, seen=seen))


def test_a_cap_equal_to_the_hello_forwards_it_then_closes_the_tunnel():
    """codex MED: the hello takes the whole cap; it is forwarded, and that tunnel is then closed, not left open to idle."""
    seen: dict = {}

    async def body(open_tunnel, front: Front, log: Path) -> None:
        status, _, r, w = await open_tunnel()
        assert status == 200
        assert await asyncio.wait_for(r.read(65536), 3) == b""       # closed promptly (silent upstream, silent client)
        await _until(lambda: seen["eof"], "the upstream to see the tunnel close")
        await _settled(front)
        assert seen["up"] == HELLO_LEN and front.pt_total == HELLO_LEN and front.pt_exhausted
        t = _tunnels(log)[-1]
        assert t["bytes_up"] == HELLO_LEN and t["end"] == "bytes-exhausted", t

    asyncio.run(_run(policy(tunnel_bytes_cap=HELLO_LEN), body, silent=True, seen=seen))


def test_exhaustion_closes_a_tunnel_still_waiting_for_its_hello():
    seen: dict = {}

    async def body(open_tunnel, front: Front, log: Path) -> None:
        status, _, r, w = await open_tunnel()
        assert status == 200
        await _read_n(r, HELLO_LEN)
        status2, _, r2, w2 = await _raw_200(front._test_path, front)   # admitted; its hello never comes
        assert status2 == 200
        w.write(b"R" * 4096)                                           # past the cap
        await w.drain()
        assert await asyncio.wait_for(r2.read(65536), 3) == b""        # closed by exhaustion, not its 10 s hello wait
        assert front.pt_exhausted and front.pt_total <= front.policy.tunnel_bytes_cap
        w2.close()

    asyncio.run(_run(policy(tunnel_bytes_cap=2 * HELLO_LEN + 100), body, seen=seen))


def test_huge_integer_limits_are_refused_not_crashed():
    """10**400 is a valid JSON integer: float() of it overflows. Both parsers refuse it by name, with one bound."""
    for dl in (10**400, -10**400, 10**12 + 1, 1e400):
        with pytest.raises(PolicyError, match="deadline must be"):
            load_policy(policy(tunnel_bytes_cap=1000, deadline=dl))
        with pytest.raises(Exception, match="front-limit-invalid:deadline"):
            front_limit_fields(None, dl)
    for cap in (10**400, 2**53 + 1):
        with pytest.raises(PolicyError, match="tunnel_bytes_cap must be"):
            load_policy(policy(tunnel_bytes_cap=cap))
        with pytest.raises(Exception, match="front-limit-invalid:tunnel-bytes-cap"):
            front_limit_fields(cap)
    # the bounds themselves are accepted by both, and round-trip
    out = front_limit_fields(2**53, 10**12)
    p = load_policy(policy(**out))
    assert (p.tunnel_bytes_cap, p.deadline) == (2**53, 10**12)
