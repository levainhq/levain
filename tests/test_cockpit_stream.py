"""CK4: the cockpit's freshness stream. The loop is driven with an injected clock and stop; the HTTP
tests inject the tick and heartbeat intervals on the server, so none of them waits on real cadence."""

from __future__ import annotations

import http.client
import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from levain.cockpit import stream
from levain.cockpit.providers import build_default_cockpit
from levain.writes import WriteScope
from levain.dashboard import SubstrateSource
from levain.web_server import make_server

from tests.test_cockpit_k1 import _install, _simple


class _Ticks:
    """A stop that lets the loop run ``n`` ticks, then ends it."""

    def __init__(self, n: int) -> None:
        self.n = n

    def wait(self, _timeout: float) -> bool:
        if self.n <= 0:
            return True
        self.n -= 1
        return False


def _events(chunks: list[bytes]) -> list[tuple[str, str, Any]]:
    out = []
    for c in chunks:
        text = c.decode()
        if text.startswith(":"):
            out.append(("comment", "", text.strip()))
            continue
        lines = dict(l.split(": ", 1) for l in text.strip().split("\n"))
        out.append((lines["event"], lines.get("id", ""), json.loads(lines["data"])))
    return out


class TestLoop:
    def test_hello_carries_the_whole_freshness_map_and_the_epoch(self) -> None:
        chunks: list[bytes] = []
        fmap = {"a": {"as_of": "t1", "status": "ok"}, "b": {"as_of": None, "status": "unread"}}
        stream.run(chunks.append, lambda: fmap, epoch="E1", last_event_id=None, stop=_Ticks(0))
        (name, eid, data), = _events(chunks)
        assert name == "hello" and eid == "E1:1"
        assert data == {"epoch": "E1", "resumed": False, "freshness": fmap}

    def test_a_reconnect_with_this_epoch_says_resumed_a_stale_epoch_does_not(self) -> None:
        for last, want in (("E1:9", True), ("OLD:3", False), (None, False), ("", False)):
            chunks: list[bytes] = []
            stream.run(chunks.append, lambda: {}, epoch="E1", last_event_id=last, stop=_Ticks(0))
            assert _events(chunks)[0][2]["resumed"] is want

    def test_a_panel_that_changed_twice_between_ticks_is_one_frame(self) -> None:
        # the loop only ever sees the map at a tick, so two commits inside one tick are one change
        maps = iter([
            {"a": {"as_of": "t1", "status": "ok"}, "b": {"as_of": "t1", "status": "ok"}},   # hello
            {"a": {"as_of": "t3", "status": "ok"}, "b": {"as_of": "t1", "status": "ok"}},   # a moved (t1 -> t2 -> t3)
            {"a": {"as_of": "t3", "status": "ok"}, "b": {"as_of": "t1", "status": "ok"}},   # nothing
        ])
        chunks: list[bytes] = []
        stream.run(chunks.append, lambda: next(maps), epoch="E", last_event_id=None, stop=_Ticks(2))
        ev = _events(chunks)
        assert [e[0] for e in ev] == ["hello", "panel-changed"]
        assert ev[1][2] == {"id": "a", "as_of": "t3", "status": "ok"} and ev[1][1] == "E:2"

    def test_a_status_change_alone_is_announced_and_each_panel_gets_its_own_frame(self) -> None:
        maps = iter([
            {"a": {"as_of": "t1", "status": "ok"}, "b": {"as_of": "t1", "status": "ok"}},
            {"a": {"as_of": "t1", "status": "stale"}, "b": {"as_of": "t2", "status": "ok"}},
        ])
        chunks: list[bytes] = []
        stream.run(chunks.append, lambda: next(maps), epoch="E", last_event_id=None, stop=_Ticks(1))
        ev = _events(chunks)[1:]
        assert sorted(e[2]["id"] for e in ev) == ["a", "b"] and len(ev) == 2

    def test_a_heartbeat_comment_follows_silence_and_not_traffic(self) -> None:
        t = [0.0]
        chunks: list[bytes] = []

        class Stop(_Ticks):
            def wait(self, timeout: float) -> bool:
                t[0] += 10
                return super().wait(timeout)

        stream.run(chunks.append, lambda: {}, epoch="E", last_event_id=None, stop=Stop(3), heartbeat_s=15, clock=lambda: t[0])
        assert [c for c in chunks if c.startswith(b":")] == [b": hb\n\n"]

    def test_a_write_failure_ends_the_loop(self) -> None:
        def boom(_b: bytes) -> None:
            raise BrokenPipeError

        try:
            stream.run(boom, lambda: {}, epoch="E", last_event_id=None, stop=_Ticks(5))
        except BrokenPipeError:
            return
        raise AssertionError("the loop swallowed a dead client")


@contextmanager
def _serving(src: SubstrateSource, tick: float = 0.05, heartbeat: float = 0.1, **kw: Any):
    httpd = make_server(src, host="127.0.0.1", port=0, **kw)
    httpd.stream_tick_s = tick
    httpd.stream_heartbeat_s = heartbeat
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    port = httpd.server_address[1]
    try:
        yield port, httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def _open(port: int, headers: dict | None = None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/cockpit/events", headers=headers or {})
    return c, c.getresponse()


def _read_frame(resp) -> str:
    buf = ""
    while not buf.endswith("\n\n"):
        line = resp.fp.readline().decode()
        if not line:
            break
        buf += line
    return buf


class TestHttp:
    def test_hello_arrives_first_and_carries_the_freshness_map(self, tmp_path: Path) -> None:
        _root, src = _install(tmp_path)
        ck = build_default_cockpit(src)
        ck.register(_simple("slow"))
        with _serving(src, cockpit=ck) as (port, _h):
            c, r = _open(port)
            try:
                assert r.status == 200 and r.getheader("Content-Type").startswith("text/event-stream")
                first = _read_frame(r)
                assert first.startswith("id: ") and "event: hello" in first
                data = json.loads(first.split("data: ", 1)[1])
                assert "slow" in data["freshness"] and "now" in data["freshness"]
            finally:
                c.close()

    def test_a_heartbeat_comment_arrives_on_an_idle_stream(self, tmp_path: Path) -> None:
        _root, src = _install(tmp_path)
        with _serving(src) as (port, _h):
            c, r = _open(port)
            try:
                _read_frame(r)
                deadline = time.time() + 3
                seen = ""
                while time.time() < deadline and ": hb" not in seen:
                    seen = _read_frame(r)
                assert ": hb" in seen
            finally:
                c.close()

    def test_an_unauthenticated_stream_is_refused_exactly_as_a_panel_read(self, tmp_path: Path) -> None:
        root, src = _install(tmp_path)
        gated = SubstrateSource(anneal=src.anneal, write_scope=WriteScope(
            anneal=src.anneal, ledger_root=root / ".levain", install_root=None), context_json=src.context_json)
        with _serving(gated, write_token="tok") as (port, httpd):
            httpd.is_loopback_bind = False   # what a real off-box bind sets
            c, r = _open(port)
            panel = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            panel.request("GET", "/cockpit/panel/tray.json")
            p = panel.getresponse()
            assert r.status == p.status == 403
            assert r.read() == p.read()
            c.close()
            panel.close()
            c2, ok = _open(port, {"X-Levain-Write-Token": "tok"})
            try:
                assert ok.status == 200 and "event: hello" in _read_frame(ok)
            finally:
                c2.close()

    def test_past_the_stream_cap_the_answer_is_503(self, tmp_path: Path) -> None:
        _root, src = _install(tmp_path)
        with _serving(src) as (port, httpd):
            httpd.stream_gate = threading.BoundedSemaphore(2)
            held = []
            try:
                for _ in range(2):
                    c, r = _open(port)
                    assert r.status == 200
                    _read_frame(r)
                    held.append(c)
                c3, r3 = _open(port)
                assert r3.status == 503
                c3.close()
                held.pop().close()          # a stream that ends frees its slot
                deadline = time.time() + 3
                status = 503
                while time.time() < deadline and status != 200:
                    c4, r4 = _open(port)
                    status = r4.status
                    if status == 200:
                        held.append(c4)
                    else:
                        c4.close()
                        time.sleep(0.1)
                assert status == 200
            finally:
                for c in held:
                    c.close()

    def test_server_shutdown_ends_open_streams(self, tmp_path: Path) -> None:
        _root, src = _install(tmp_path)
        httpd = make_server(src, host="127.0.0.1", port=0)
        httpd.stream_tick_s = 0.05
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        c, r = _open(httpd.server_address[1])
        _read_frame(r)
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)
        try:
            tail = r.read()       # the server ended the response: the read finishes instead of waiting
        finally:
            c.close()
        assert isinstance(tail, bytes)
