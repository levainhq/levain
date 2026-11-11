"""CK4: the cockpit's change stream. The Watcher is driven tick by tick against a fake manifest; the HTTP
tests run the real server with a fast watcher and heartbeat, so none of them waits on real cadence."""

from __future__ import annotations

import http.client
import json
import queue
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from levain.cockpit import stream
from levain.cockpit.engine import Read, RowIn
from levain.cockpit.providers import build_default_cockpit
from levain.dashboard import SubstrateSource
from levain.web_server import make_server
from levain.writes import WriteScope

from tests.test_cockpit_k1 import _install, _simple


def _manifest(**etags: str) -> dict[str, Any]:
    return {"panels": {pid: {"etag": e} for pid, e in etags.items()}}


class _Feed:
    """A manifest source the test mutates between ticks."""

    def __init__(self, **etags: str) -> None:
        self.etags = dict(etags)
        self.fail = False
        self.reads = 0

    def __call__(self) -> dict[str, Any]:
        self.reads += 1
        if self.fail:
            raise RuntimeError("manifest unavailable")
        return _manifest(**self.etags)


def _watcher(feed: _Feed, **kw: Any) -> stream.Watcher:
    return stream.Watcher(feed, epoch="E", watch_s=3600, **kw)     # ticks are driven by hand


def _drain(sub: stream.Subscription) -> list[tuple[str, dict[str, Any], str]]:
    out = []
    while not sub.q.empty():
        out.append(sub.q.get_nowait())
    return out


class TestWatcher:
    def test_the_baseline_announces_nothing_and_a_moved_etag_is_one_panel_changed(self) -> None:
        feed = _Feed(a="1", b="1")
        w = _watcher(feed)
        sub = w.subscribe(None)
        try:
            assert sub.hello["resync"] is False and _drain(sub) == []
            w.tick()
            assert _drain(sub) == []
            feed.etags["a"] = "2"
            w.tick()
            w.tick()        # nothing moved since: no second event
            (ev, data, eid), = _drain(sub)
            assert (ev, data) == ("panel-changed", {"id": "a", "etag": "2"}) and eid.endswith(":1")
        finally:
            w.stop()

    def test_a_panel_that_moved_twice_between_ticks_is_announced_once(self) -> None:
        feed = _Feed(a="1")
        w = _watcher(feed)
        sub = w.subscribe(None)
        try:
            feed.etags["a"] = "2"
            feed.etags["a"] = "3"
            w.tick()
            (_ev, data, _eid), = _drain(sub)
            assert data == {"id": "a", "etag": "3"}
        finally:
            w.stop()

    def test_an_added_or_removed_panel_is_manifest_changed(self) -> None:
        feed = _Feed(a="1")
        w = _watcher(feed)
        sub = w.subscribe(None)
        try:
            feed.etags["b"] = "1"
            w.tick()
            del feed.etags["a"]
            w.tick()
            assert [e[0] for e in _drain(sub)] == ["manifest-changed", "manifest-changed"]
        finally:
            w.stop()

    def test_a_manifest_failure_is_one_lost_tick_and_the_watcher_goes_on(self) -> None:
        feed = _Feed(a="1")
        w = _watcher(feed)
        sub = w.subscribe(None)
        try:
            feed.fail = True
            w.tick()        # must not raise
            feed.fail = False
            feed.etags["a"] = "2"
            w.tick()
            (ev, data, _), = _drain(sub)
            assert (ev, data["id"]) == ("panel-changed", "a")
        finally:
            w.stop()

    def test_a_failed_baseline_is_adopted_by_the_first_good_tick_and_pages_reload(self) -> None:
        feed = _Feed(a="1")
        feed.fail = True
        w = _watcher(feed)
        sub = w.subscribe(None)
        try:
            feed.fail = False
            w.tick()
            assert [e[0] for e in _drain(sub)] == ["manifest-changed"]
        finally:
            w.stop()

    def test_resume_inside_the_ring_replays_and_outside_it_asks_for_a_resync(self) -> None:
        feed = _Feed(a="0")
        w = _watcher(feed, ring=3)
        first = w.subscribe(None)
        try:
            epoch = first.hello["epoch"]
            for n in range(1, 6):
                feed.etags["a"] = str(n)
                w.tick()
            # seq 5 is current; the ring holds 3..5
            inside = w.subscribe(f"{epoch}:3")
            assert inside.hello["resync"] is False and inside.hello_id == f"{epoch}:3"
            assert [d["etag"] for _e, d, _i in _drain(inside)] == ["4", "5"]
            outside = w.subscribe(f"{epoch}:1")
            assert outside.hello["resync"] is True and _drain(outside) == []
            ahead = w.subscribe(f"{epoch}:99")
            assert ahead.hello["resync"] is True
            other = w.subscribe("OTHER.1:2")
            assert other.hello["resync"] is True
            junk = w.subscribe("nonsense")
            assert junk.hello["resync"] is True
        finally:
            w.stop()

    def test_a_watcher_that_stopped_is_a_new_epoch_so_an_old_position_resyncs(self) -> None:
        feed = _Feed(a="1")
        w = _watcher(feed)
        sub = w.subscribe(None)
        old = sub.hello["epoch"]
        w.unsubscribe(sub)
        assert not w.running
        again = w.subscribe(f"{old}:0")
        try:
            assert again.hello["epoch"] != old and again.hello["resync"] is True
        finally:
            w.stop()

    def test_a_subscriber_too_slow_to_follow_is_dropped_not_waited_for(self, monkeypatch) -> None:
        monkeypatch.setattr(stream, "SUBSCRIBER_QUEUE", 2)
        feed = _Feed(a="0")
        w = _watcher(feed)
        sub = w.subscribe(None)
        try:
            for n in range(1, 5):
                feed.etags["a"] = str(n)
                w.tick()
            assert sub.dead
        finally:
            w.stop()


class TestRun:
    def test_hello_leads_then_events_then_a_heartbeat_follows_silence(self) -> None:
        feed = _Feed(a="1")
        w = _watcher(feed)
        sub = w.subscribe(None)
        chunks: list[bytes] = []
        t = [0.0]
        stop = threading.Event()

        def write(b: bytes) -> None:
            chunks.append(b)
            if len(chunks) == 3:
                stop.set()

        def clock() -> float:
            t[0] += 10
            return t[0]

        try:
            feed.etags["a"] = "2"
            w.tick()
            stream.run(write, sub, stop=stop, heartbeat_s=15, poll_s=0.01, clock=clock)
        finally:
            w.stop()
        assert chunks[0].startswith(b"id: E.1:0\nevent: hello\n")
        assert b"event: panel-changed" in chunks[1]
        assert chunks[2] == b": hb\n\n"

    def test_a_write_failure_ends_the_loop(self) -> None:
        w = _watcher(_Feed(a="1"))
        sub = w.subscribe(None)
        try:
            def boom(_b: bytes) -> None:
                raise BrokenPipeError

            try:
                stream.run(boom, sub, stop=threading.Event())
            except BrokenPipeError:
                return
            raise AssertionError("the loop swallowed a dead client")
        finally:
            w.stop()


class _Tok:
    """A change token the test moves by hand."""

    def __init__(self, value: Any = 0) -> None:
        self.value = value
        self.reads = 0

    def __call__(self) -> Any:
        self.reads += 1
        return self.value


def _until(cond: Any, secs: float = 5.0) -> bool:
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class TestWatcherToken:
    def test_an_unmoved_token_builds_no_manifest_and_a_moved_one_builds_once(self) -> None:
        feed, tok = _Feed(a="1"), _Tok(0)
        w = stream.Watcher(feed, epoch="E", watch_s=0.02, token=tok, token_s=0.01, backstop_s=3600)
        sub = w.subscribe(None)
        try:
            base = feed.reads           # the baseline read
            assert _until(lambda: tok.reads >= 20)
            assert feed.reads == base   # twenty looks at an unmoved token, no build
            feed.etags["a"] = "2"
            tok.value = 1
            assert _until(lambda: feed.reads == base + 1)
            assert _until(lambda: not sub.q.empty())
            (ev, data, _), = _drain(sub)
            assert (ev, data) == ("panel-changed", {"id": "a", "etag": "2"})
            time.sleep(0.2)
            assert feed.reads == base + 1       # and settled: no further build
        finally:
            w.stop()

    def test_the_backstop_builds_even_when_the_token_never_moves(self) -> None:
        feed, tok = _Feed(a="1"), _Tok(0)
        w = stream.Watcher(feed, epoch="E", watch_s=3600, token=tok, token_s=0.01, backstop_s=0.1)
        sub = w.subscribe(None)
        try:
            base = feed.reads
            feed.etags["a"] = "2"       # a change the token cannot see
            assert _until(lambda: not sub.q.empty())
            assert feed.reads > base
        finally:
            w.stop()

    def test_no_token_or_an_unknown_one_keeps_the_slow_tick(self) -> None:
        for token in (None, _Tok(None)):
            feed = _Feed(a="1")
            w = stream.Watcher(feed, epoch="E", watch_s=0.05, token=token, token_s=0.01, backstop_s=3600)
            w.subscribe(None)
            try:
                base = feed.reads
                assert _until(lambda: feed.reads >= base + 3)
            finally:
                w.stop()

    def test_a_token_that_raises_is_an_unknown_one(self) -> None:
        feed = _Feed(a="1")

        def boom() -> Any:
            raise OSError("gone")
        w = stream.Watcher(feed, epoch="E", watch_s=0.05, token=boom, token_s=0.01, backstop_s=3600)
        w.subscribe(None)
        try:
            base = feed.reads
            assert _until(lambda: feed.reads >= base + 2)
        finally:
            w.stop()

    def test_a_token_that_never_settles_cannot_build_faster_than_the_build_runs(self) -> None:
        feed, tok = _Feed(a="1"), _Tok(0)

        def slow() -> dict[str, Any]:
            time.sleep(0.05)
            tok.value += 1          # every look finds a new token
            return feed()
        w = stream.Watcher(slow, epoch="E", watch_s=3600, token=tok, token_s=0.005, backstop_s=3600)
        w.subscribe(None)
        try:
            time.sleep(0.6)
        finally:
            w.stop()
        assert 2 <= feed.reads <= 10    # 0.6 s of 50 ms builds each followed by a 100 ms wait: about 4, never ~12


class TestServerWatchToken:
    def test_an_install_server_has_a_token_that_moves_with_its_stores_and_not_with_a_read(self, tmp_path: Path) -> None:
        root, src = _install(tmp_path)
        httpd = make_server(src, host="127.0.0.1", port=0)
        try:
            httpd.get_cockpit().manifest({"class": "none", "device_class": None})      # the first read makes its files
            a = httpd._watch_token()
            assert a is not None and httpd._watch_token() == a
            httpd.get_cockpit().manifest({"class": "none", "device_class": None})      # a read after that moves nothing
            assert httpd._watch_token() == a
            (root / ".levain" / "memory.spores.json").write_text('{"intentions": []}')
            b = httpd._watch_token()
            assert b != a
            q = httpd.question_store
            q.file(agent="claude_code", agent_session_id="s1", what="a question", options=[
                {"key": "a", "label": "yes"}, {"key": "b", "label": "no"}], rec="a", blocks="x", cwd=str(root))
            assert httpd._watch_token() != b
        finally:
            httpd.server_close()

    def test_a_downstream_cockpit_has_no_token_until_it_names_what_it_reads(self, tmp_path: Path) -> None:
        root, src = _install(tmp_path)
        own = build_default_cockpit(src)
        for kw, expect_token in (({}, False), ({"watch_paths": [root / ".levain" / "edits.jsonl"]}, True)):
            httpd = make_server(src, host="127.0.0.1", port=0, cockpit=own, **kw)
            try:
                assert (httpd._watch_token() is not None) is expect_token
            finally:
                httpd.server_close()
        httpd = make_server(src, host="127.0.0.1", port=0, cockpit=own, watch_paths=[root / ".levain" / "edits.jsonl"])
        try:
            a = httpd._watch_token()
            (root / ".levain" / "edits.jsonl").write_text("x\n")
            assert httpd._watch_token() != a
        finally:
            httpd.server_close()


# ---- the real server ------------------------------------------------------------------------

@contextmanager
def _serving(src: SubstrateSource, watch: float = 0.05, heartbeat: float = 0.1, **kw: Any):
    httpd = make_server(src, host="127.0.0.1", port=0, **kw)
    if hasattr(httpd, "stream_watcher"):
        httpd.stream_watcher.watch_s = watch
    else:
        httpd.stream_tick_s = watch     # the rev 1 server, for the regression proof
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


def _open(port: int, headers: dict | None = None, timeout: float = 5):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
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


def _parse(frame: str) -> tuple[str, str, Any]:
    lines = dict(l.split(": ", 1) for l in frame.strip().split("\n") if not l.startswith(":"))
    return lines["event"], lines.get("id", ""), json.loads(lines["data"])


def _events_for(resp, seconds: float) -> list[tuple[str, str, Any]]:
    """Every non-heartbeat frame that arrives within ``seconds``. A reader thread per response owns the
    blocking socket reads, so a quiet stream is a timeout here and never a broken socket."""
    q = getattr(resp, "_pump_q", None)     # on the response itself: an id() key outlives the object and is reused
    if q is None:
        q = resp._pump_q = queue.Queue()

        def pump() -> None:
            try:
                while True:
                    frame = _read_frame(resp)
                    if not frame:
                        return
                    q.put(frame)
            except (OSError, ValueError):
                return

        threading.Thread(target=pump, daemon=True).start()
    out = []
    deadline = time.time() + seconds
    while True:
        left = deadline - time.time()
        if left <= 0:
            return out
        try:
            frame = q.get(timeout=left)
        except queue.Empty:
            return out
        if not frame.startswith(":"):
            out.append(_parse(frame))




def _await_events(resp, timeout: float = 15.0, settle: float = 1.0) -> list[tuple[str, str, Any]]:
    """The frames that follow an expected change: wait (up to ``timeout``) for the first, then collect for
    ``settle`` more seconds so an extra frame still shows. A loaded runner is slow; a fixed window would be a flake."""
    deadline = time.monotonic() + timeout
    got: list[tuple[str, str, Any]] = []
    while not got and time.monotonic() < deadline:
        got = _events_for(resp, 0.25)
    return got + _events_for(resp, settle) if got else got

def _pin_minute(ck, second: int = 5) -> None:
    """The cockpit's clock, still advancing in real time but held inside ONE wall-clock minute. The topology panel
    stamps ``as_of`` to the minute (so an idle install is quiet for 59 s and announces the minute turning), and a
    test that straddled a minute boundary read that as a change nobody made."""
    t0 = time.monotonic()
    base = datetime.now(timezone.utc).replace(second=second, microsecond=0)
    ck._clock = lambda: base + timedelta(seconds=min(time.monotonic() - t0, 50.0))


def _cockpit(src: SubstrateSource, rows: list[RowIn]):
    """A cockpit with one ON-DEMAND panel ``od`` whose rows the test edits; ``reads`` counts provider calls. Its
    clock is pinned inside one minute (``_pin_minute``)."""
    state = {"rows": rows, "reads": 0}
    ck = build_default_cockpit(src)
    _pin_minute(ck)

    def read(ctx):
        state["reads"] += 1
        return Read(rows=tuple(state["rows"]))

    ck.register(_simple("od", rows=(), read=read))
    return ck, state


def _row(n: int) -> RowIn:
    return RowIn(f"r{n}", f"row {n}", {"at": "2026-10-01T00:00:00+00:00"}, stored={"id": f"r{n}"})



class TestHttp:
    def test_hello_arrives_first_with_the_epoch_and_no_resync_on_a_first_connect(self, tmp_path: Path) -> None:
        _root, src = _install(tmp_path)
        ck, _ = _cockpit(src, [_row(1)])
        with _serving(src, cockpit=ck) as (port, httpd):
            c, r = _open(port)
            try:
                assert r.status == 200 and r.getheader("Content-Type").startswith("text/event-stream")
                name, eid, data = _parse(_read_frame(r))
                assert name == "hello" and data["resync"] is False
                assert eid == f"{data['epoch']}:0" and data["epoch"].startswith(httpd.stream_watcher._base_epoch)
            finally:
                c.close()

    def test_a_store_change_on_an_on_demand_panel_nobody_reads_is_exactly_one_panel_changed(self, tmp_path: Path) -> None:
        _root, src = _install(tmp_path)
        ck, state = _cockpit(src, [_row(1)])
        with _serving(src, cockpit=ck) as (port, _h):
            c, r = _open(port)
            try:
                _read_frame(r)
                assert _events_for(r, 0.4) == []        # an idle store announces nothing
                state["rows"] = [_row(1), _row(2)]       # the edit; no client reads the panel
                got = _await_events(r)
                assert [(e[0], e[2]["id"]) for e in got] == [("panel-changed", "od")], got
                assert got[0][2]["etag"]
            finally:
                c.close()

    def test_the_topology_etag_moves_when_the_minute_turns_and_not_before(self, tmp_path: Path) -> None:
        # S1: the flake was a real boundary crossing, not a feedback loop. The panel stamps as_of to the minute, so
        # an idle install announces ONE topology change per minute (F12a ruling 4, as the activity panel does).
        _root, src = _install(tmp_path)
        ck = build_default_cockpit(src)
        now = [datetime(2026, 10, 19, 15, 0, 59, 900000, tzinfo=timezone.utc)]
        ck._clock = lambda: now[0]

        def etags() -> dict[str, str]:
            return {k: v["etag"] for k, v in ck.manifest({"class": "none", "device_class": None})["panels"].items()}

        before = etags()
        assert etags() == before                                   # the same instant: stable
        now[0] += timedelta(milliseconds=300)                      # 15:01:00.2: the minute turned
        after = etags()
        assert {k for k in before if before[k] != after[k]} == {"topology"}
        now[0] += timedelta(seconds=30)                            # inside the new minute: quiet again
        assert etags() == after

    def test_a_client_get_of_a_panel_produces_no_event(self, tmp_path: Path) -> None:
        # the feedback-loop regression: a read advances the engine's freshness, which must not be a change signal
        _root, src = _install(tmp_path)
        ck, _state = _cockpit(src, [_row(1)])
        with _serving(src, cockpit=ck) as (port, _h):
            c, r = _open(port)
            try:
                _read_frame(r)
                for _ in range(4):
                    g = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    g.request("GET", "/cockpit/panel/od.json")
                    assert g.getresponse().status == 200
                    g.close()
                    time.sleep(0.15)
                assert _events_for(r, 1.0) == []
            finally:
                c.close()

    def test_a_panel_added_is_manifest_changed(self, tmp_path: Path) -> None:
        _root, src = _install(tmp_path)
        ck, _ = _cockpit(src, [_row(1)])
        with _serving(src, cockpit=ck) as (port, _h):
            c, r = _open(port)
            try:
                _read_frame(r)
                ck.register(_simple("later", rows=()))
                got = _await_events(r)
                assert [e[0] for e in got] == ["manifest-changed"], got
            finally:
                c.close()

    def test_resume_replays_inside_the_ring_and_resyncs_outside_it(self, tmp_path: Path) -> None:
        _root, src = _install(tmp_path)
        ck, state = _cockpit(src, [_row(1)])
        with _serving(src, cockpit=ck) as (port, httpd):
            c, r = _open(port)
            try:
                _name, hello_id, hello = _parse(_read_frame(r))
                state["rows"] = [_row(1), _row(2)]
                (ev,) = _await_events(r)
                assert ev[0] == "panel-changed"
                last = ev[1]
            finally:
                c.close()
            # the watcher is still alive only if a stream remains: hold one so the epoch survives
            keep, kr = _open(port)
            try:
                _read_frame(kr)
                c2, r2 = _open(port, {"Last-Event-ID": hello_id})
                try:
                    _n, _i, h2 = _parse(_read_frame(r2))
                    if h2["epoch"] == hello["epoch"]:
                        assert h2["resync"] is False
                        assert [e[0] for e in _await_events(r2)] == ["panel-changed"]       # replayed
                    else:
                        assert h2["resync"] is True        # the first stream's end stopped the watcher: a new epoch
                finally:
                    c2.close()
                c3, r3 = _open(port, {"Last-Event-ID": "stale.1:4"})
                try:
                    assert _parse(_read_frame(r3))[2]["resync"] is True
                finally:
                    c3.close()
            finally:
                keep.close()
            assert last.startswith(hello["epoch"])

    def test_the_watcher_runs_only_while_a_stream_is_open(self, tmp_path: Path) -> None:
        _root, src = _install(tmp_path)
        ck, _ = _cockpit(src, [_row(1)])
        with _serving(src, cockpit=ck) as (port, httpd):
            w = httpd.stream_watcher
            assert not w.running
            c1, r1 = _open(port)
            c2, r2 = _open(port)
            _read_frame(r1)
            _read_frame(r2)
            assert w.running and w.stream_count == 2
            r1.close()
            c1.close()
            time.sleep(0.5)
            assert w.running            # one stream is still open
            r2.close()
            c2.close()
            deadline = time.time() + 5
            while time.time() < deadline and w.running:
                time.sleep(0.05)
            assert not w.running and w.stream_count == 0

    def test_a_hung_up_client_frees_its_slot_without_waiting_for_a_heartbeat(self, tmp_path: Path) -> None:
        _root, src = _install(tmp_path)
        with _serving(src, heartbeat=3600) as (port, httpd):
            c, r = _open(port)
            _read_frame(r)
            assert httpd.stream_watcher.stream_count == 1
            r.close()
            c.close()
            deadline = time.time() + 3
            while time.time() < deadline and httpd.stream_watcher.running:
                time.sleep(0.05)
            assert not httpd.stream_watcher.running and httpd.stream_watcher.stream_count == 0

    def test_a_manifest_exception_does_not_end_streams(self, tmp_path: Path) -> None:
        _root, src = _install(tmp_path)
        ck, state = _cockpit(src, [_row(1)])
        real = ck.manifest
        flag = {"fail": False}

        def flaky(*a: Any, **kw: Any):
            if flag["fail"]:
                raise RuntimeError("store unreadable")
            return real(*a, **kw)

        ck.manifest = flaky       # type: ignore[method-assign]
        with _serving(src, cockpit=ck) as (port, httpd):
            c, r = _open(port)
            try:
                _read_frame(r)
                flag["fail"] = True
                time.sleep(0.4)             # several failed ticks
                assert httpd.stream_watcher.running and httpd.stream_watcher.stream_count == 1
                flag["fail"] = False
                state["rows"] = [_row(1), _row(2)]
                got = _await_events(r)
                assert [e[0] for e in got] == ["panel-changed"], got
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
        with _serving(gated, surface_tokens={"flowconnect": "tok"}) as (port, httpd):   # off-box the owner key opens nothing (L-1)
            httpd.is_loopback_bind = False   # what a real off-box bind sets
            c, r = _open(port)
            panel = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            panel.request("GET", "/cockpit/panel/tray.json")
            p = panel.getresponse()
            assert r.status == p.status == 403
            assert r.read() == p.read()
            assert not httpd.stream_watcher.running       # a refused stream never starts the watcher
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

    def test_server_shutdown_ends_open_streams_and_the_watcher(self, tmp_path: Path) -> None:
        _root, src = _install(tmp_path)
        httpd = make_server(src, host="127.0.0.1", port=0)
        httpd.stream_watcher.watch_s = 0.05
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        c, r = _open(httpd.server_address[1])
        _read_frame(r)
        assert httpd.stream_watcher.running
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)
        try:
            tail = r.read()       # the server ended the response: the read finishes instead of waiting
        finally:
            c.close()
        assert isinstance(tail, bytes) and not httpd.stream_watcher.running

    def test_stream_state_is_per_server_instance(self, tmp_path: Path) -> None:
        _root, src = _install(tmp_path)
        a = make_server(src, host="127.0.0.1", port=0)
        b = make_server(src, host="127.0.0.1", port=0)
        try:
            assert a.stream_stop is not b.stream_stop and a.stream_gate is not b.stream_gate
            assert a.stream_watcher is not b.stream_watcher
        finally:
            a.server_close()
            b.server_close()


class TestWatchTokenSettles:
    def test_the_token_settles_after_a_build_reads_stores_that_hold_data(self, tmp_path: Path) -> None:
        root, src = _install(tmp_path)
        httpd = make_server(src, host="127.0.0.1", port=0)
        try:
            import sqlite3
            for i in range(3):
                httpd.question_store.file(agent="claude_code", agent_session_id=f"s{i}", what=f"q{i}", options=[
                    {"key": "a", "label": "yes"}, {"key": "b", "label": "no"}], rec="a", blocks="x", cwd=str(root))
            db = sqlite3.connect(str(src.anneal.episodic_db))
            db.execute("create table if not exists t(x)")
            db.execute("insert into t values (1)")
            db.commit()
            db.close()
            seen = []
            for _ in range(4):
                httpd.get_cockpit().manifest({"class": "none", "device_class": None})
                seen.append(httpd._watch_token())
            assert seen[1] == seen[2] == seen[3], "the token keeps moving on the watcher's own reads"
        finally:
            httpd.server_close()

    def test_a_token_that_raises_logs_once_per_run(self, caplog: Any) -> None:
        feed = _Feed(a="1")

        def boom() -> Any:
            raise OSError("gone")
        w = stream.Watcher(feed, epoch="E", watch_s=0.02, token=boom, token_s=0.005, backstop_s=3600)
        with caplog.at_level("ERROR", logger="levain.cockpit.stream"):
            w.subscribe(None)
            try:
                base = feed.reads
                assert _until(lambda: feed.reads >= base + 3)
            finally:
                w.stop()
        assert len([r for r in caplog.records if "change token" in r.getMessage()]) == 1


class TestServerTokenInputs:
    def test_a_store_outside_the_anneal_dir_and_the_context_file_move_the_token(self, tmp_path: Path) -> None:
        import dataclasses
        root, src = _install(tmp_path)
        other = tmp_path / "elsewhere"
        other.mkdir()
        ctx = tmp_path / "ctx" / "context.json"
        ctx.parent.mkdir()
        ctx.write_text("{}")
        src2 = dataclasses.replace(src, anneal=dataclasses.replace(src.anneal, intentions_json=other / "s.json"),
                                   context_json=ctx)
        (other / "s.json").write_text("[]")
        httpd = make_server(src2, host="127.0.0.1", port=0)
        try:
            a = httpd._watch_token()
            (other / "s.json").write_text('["moved"]')
            b = httpd._watch_token()
            assert b != a
            ctx.write_text('{"state": "moved"}')
            assert httpd._watch_token() != b
        finally:
            httpd.server_close()

    def test_a_directory_past_the_cap_makes_the_token_unknown_and_shm_files_do_not_count(self, tmp_path: Path) -> None:
        from levain import web_server
        root, src = _install(tmp_path)
        httpd = make_server(src, host="127.0.0.1", port=0)
        d = root / ".levain"
        try:
            for i in range(web_server._WATCH_ENTRIES_MAX + 5):
                (d / f"x{i}.db-shm").write_text("")
            assert httpd._watch_token() is not None     # shm files do not use the cap
            for i in range(web_server._WATCH_ENTRIES_MAX + 5):
                (d / f"y{i}.txt").write_text("")
            assert httpd._watch_token() is None         # never a partial token
        finally:
            httpd.server_close()

    def test_an_install_root_working_tree_is_not_scanned_and_a_missing_dir_is_one_stable_entry(self, tmp_path: Path) -> None:
        root, src = _install(tmp_path)
        httpd = make_server(src, host="127.0.0.1", port=0)
        try:
            a = httpd._watch_token()
            (root / "unrelated.txt").write_text("x")        # a repo file no panel reads
            assert httpd._watch_token() == a
            assert a is not None        # seed/ and activation/ may be absent: still a token
        finally:
            httpd.server_close()

    def test_an_invalid_watch_path_refuses_before_the_port_is_bound(self, tmp_path: Path) -> None:
        import socket
        _root, src = _install(tmp_path)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        try:
            make_server(src, host="127.0.0.1", port=port, watch_paths=[object()])      # type: ignore[list-item]
        except TypeError:
            pass
        else:
            raise AssertionError("an invalid watch path was accepted")
        httpd = make_server(src, host="127.0.0.1", port=port)       # the port was never taken
        httpd.server_close()


class TestWalFingerprint:
    def test_a_commit_after_a_checkpoint_moves_the_wal_mtime_but_not_its_size_and_a_read_moves_neither(self, tmp_path: Path) -> None:
        import os
        import sqlite3
        from levain import web_server
        p = tmp_path / "x.db"
        w = sqlite3.connect(str(p))
        w.execute("pragma journal_mode=wal")
        w.execute("create table t(x)")
        for i in range(50):
            w.execute("insert into t values (?)", (i,))
        w.commit()
        wal = Path(str(p) + "-wal")
        before = web_server._stat_sig(wal)
        r = sqlite3.connect(str(p))
        list(r.execute("select count(*) from t"))
        r.close()
        assert web_server._stat_sig(wal) == before          # (a) a read-only open moves nothing
        w.execute("pragma wal_checkpoint(PASSIVE)")
        mid = web_server._stat_sig(wal)
        time.sleep(0.02)
        w.execute("insert into t values (999)")
        w.commit()
        after = web_server._stat_sig(wal)
        w.close()
        assert after[1] == mid[1] and after[0] != mid[0]    # (b) same size, later mtime: a size-only print is blind to it

    def test_the_token_moves_on_a_commit_after_a_checkpoint(self, tmp_path: Path) -> None:
        import sqlite3
        root, src = _install(tmp_path)
        httpd = make_server(src, host="127.0.0.1", port=0)
        try:
            db = src.anneal.episodic_db
            w = sqlite3.connect(str(db))
            w.execute("pragma journal_mode=wal")
            w.execute("create table if not exists t(x)")
            w.execute("insert into t values (1)")
            w.commit()
            w.execute("pragma wal_checkpoint(PASSIVE)")
            a = httpd._watch_token()
            time.sleep(0.02)
            w.execute("insert into t values (2)")
            w.commit()
            assert httpd._watch_token() != a
            w.close()
        finally:
            httpd.server_close()

    def test_a_directory_of_skipped_entries_is_still_bounded(self, tmp_path: Path) -> None:
        from levain import web_server
        root, src = _install(tmp_path)
        httpd = make_server(src, host="127.0.0.1", port=0)
        try:
            for i in range(web_server._WATCH_SCAN_MAX + 5):
                (root / ".levain" / f"z{i}.db-journal").write_text("")
            assert httpd._watch_token() is None
        finally:
            httpd.server_close()
