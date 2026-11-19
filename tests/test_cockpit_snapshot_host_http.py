"""The snapshot host behind a real HTTP server: attached mode is opt-in (an environment variable, off by default), a
request in it runs no provider, the change stream announces what a derive changed and what the serve itself composes,
and closing the server ends the derive threads. The cockpit, broker and store are real; one provider is a small
recording one added to the built-in panels."""

from __future__ import annotations

import http.client
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

import pytest

from levain.cockpit import providers
from levain.cockpit.engine import Cockpit, Read, RowIn
from levain.cockpit.snapshot_host import DERIVE_THREAD_PREFIX, ServerProbe, SnapshotHost
from levain.cockpit.snapshots import SnapshotWriter
from levain.web_server import COCKPIT_SNAPSHOTS_ENV, make_server

from tests.test_cockpit_k1 import _install, _simple
from tests.test_cockpit_stream import _await_events, _events_for, _open, _pin_minute, _read_frame, _serving

SERVE_COMPOSED = {"decisions", "edits"}


def _row(n: int) -> RowIn:
    return RowIn(f"r{n}", f"row {n}", {"at": "2026-10-01T00:00:00+00:00"}, stored={"id": f"r{n}"})


class Reads:
    """Every provider read a cockpit makes, by panel id, and the threads that asked it to derive. The engine runs a
    provider on a pool thread of its own, so the thread a provider runs on does not say who wanted the read."""

    def __init__(self) -> None:
        self.provider_reads: list[str] = []
        self.deriving_threads: list[str] = []
        self.rows = [_row(1)]
        self._lock = threading.Lock()

    def spy_on(self, ck: Cockpit) -> None:
        read_panel, derive_panel = ck._read_panel, ck.derive_panel

        def spy_read(spec, st, ctx):
            with self._lock:
                self.provider_reads.append(spec.id)
            return read_panel(spec, st, ctx)

        def spy_derive(pid):
            with self._lock:
                self.deriving_threads.append(threading.current_thread().name)
            return derive_panel(pid)

        ck._read_panel = spy_read           # type: ignore[method-assign]
        ck.derive_panel = spy_derive        # type: ignore[method-assign]

    def mark(self) -> int:
        with self._lock:
            return len(self.provider_reads)

    def since(self, mark: int) -> list[str]:
        with self._lock:
            return self.provider_reads[mark:]

    def derives(self) -> int:
        with self._lock:
            return len(self.deriving_threads)

    def add_panel(self, ck: Cockpit) -> None:
        ck.register(_simple("od", rows=(), read=lambda ctx: Read(rows=tuple(self.rows))))


def _env(monkeypatch, value: "str | None") -> None:
    if value is None:
        monkeypatch.delenv(COCKPIT_SNAPSHOTS_ENV, raising=False)
    else:
        monkeypatch.setenv(COCKPIT_SNAPSHOTS_ENV, value)


def _get(port: int, path: str) -> "tuple[int, Any]":
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        return r.status, (json.loads(body) if body else None)
    finally:
        c.close()


def _until(pred: Callable[[], Any], timeout: float = 60.0) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        got = pred()
        if got:
            return got
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.05)


def _derive_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith(DERIVE_THREAD_PREFIX)]


def _settle(resp: Any, quiet_s: float = 1.0, timeout: float = 60.0) -> None:
    """Read the stream until it has been quiet for ``quiet_s``: the host's first derives of every panel are announced."""
    deadline = time.monotonic() + timeout
    while _events_for(resp, quiet_s):
        assert time.monotonic() < deadline, "the stream never went quiet"


def _collect(resp: Any, done: Callable[[list], bool], timeout: float = 30.0) -> "list[tuple[str, Any]]":
    """The (event, panel id) pairs read until ``done`` accepts them, or ``timeout``."""
    got: list[tuple[str, Any]] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not done(got):
        got += [(e[0], e[2].get("id")) for e in _events_for(resp, 0.25)]
    return got


def _every_plan_has_a_row(host: SnapshotHost) -> bool:
    return all(host.reader.read(p.id) is not None for p in host._plans())


def _derived_and_idle(host: SnapshotHost) -> bool:
    """Every plan has a row, discovery has added the panels it will, and no derive is queued or running."""
    plans = {p.id for p in host._plans()}
    if not _every_plan_has_a_row(host) or host.loop.deriving():
        return False
    time.sleep(0.3)
    return {p.id for p in host._plans()} == plans and not host.loop.deriving()


def _provider_reads_during(reads: Reads, run: Callable[[], None], tries: int = 5) -> list[str]:
    """The provider reads made while ``run`` did its requests, in a window in which the host derived nothing (a read made
    in such a window can only have been asked for by a request). A window the host's own timer broke into is run again."""
    for _ in range(tries):
        mark, derived = reads.mark(), reads.derives()
        run()
        if reads.derives() == derived:
            return reads.since(mark)
    raise AssertionError("the host kept deriving through every window")


@contextmanager
def _default_server(tmp_path: Path, monkeypatch, value: "str | None", reads: Reads, **kw: Any):
    """The server that builds its own cockpit (the kernel's built-in panels plus ``od``), spied on."""
    _env(monkeypatch, value)
    _root, src = _install(tmp_path)
    real_build = providers.build_default_cockpit

    def build(*a: Any, **k: Any) -> Cockpit:
        ck = real_build(*a, **k)
        _pin_minute(ck)
        reads.add_panel(ck)
        reads.spy_on(ck)
        return ck

    monkeypatch.setattr(providers, "build_default_cockpit", build)
    with _serving(src, **kw) as (port, httpd):
        yield port, httpd


# ---------------------------------------------------------------------------------------------- (h) the request path


class TestAttachedRequests:
    def test_concurrent_manifest_and_panel_reads_run_no_provider_for_a_snapshot_panel(self, tmp_path: Path, monkeypatch) -> None:
        reads = Reads()
        with _default_server(tmp_path, monkeypatch, "1", reads) as (port, httpd):
            assert _get(port, "/cockpit/manifest.json")[0] == 200       # builds the cockpit, attaches and starts its host
            host = httpd._snapshot_host
            assert host is not None and httpd.get_cockpit().attached
            _until(lambda: _derived_and_idle(host))
            assert reads.deriving_threads and all(n.startswith(DERIVE_THREAD_PREFIX) for n in reads.deriving_threads)
            panel_ids = [p for p in httpd.get_cockpit().panel_ids if p not in SERVE_COMPOSED]
            assert "od" in panel_ids and any(p.startswith("config:") for p in panel_ids)     # discovered panels too
            outcomes: list = []

            def request(i: int) -> "tuple[int, list[int]]":
                m, _ = _get(port, "/cockpit/manifest.json")
                return m, [_get(port, f"/cockpit/panel/{pid}.json")[0] for pid in panel_ids]

            def burst() -> None:
                with ThreadPoolExecutor(8) as pool:
                    outcomes[:] = [f.result(timeout=120) for f in [pool.submit(request, i) for i in range(16)]]

            ran = [pid for pid in _provider_reads_during(reads, burst) if pid not in SERVE_COMPOSED]
            assert all(m == 200 and set(codes) == {200} for m, codes in outcomes) and len(outcomes) == 16
            assert ran == [], ran
            assert _get(port, "/cockpit/panel/od.json")[1]["rows"][0]["id"] == "r1"

    def test_a_downstream_cockpit_passed_to_the_server_is_attached_too(self, tmp_path: Path, monkeypatch) -> None:
        _env(monkeypatch, "1")
        _root, src = _install(tmp_path)
        reads = Reads()
        ck = providers.build_default_cockpit(src)
        _pin_minute(ck)
        reads.add_panel(ck)
        reads.spy_on(ck)
        with _serving(src, cockpit=ck) as (port, httpd):
            host = httpd._snapshot_host
            assert host is not None and ck.attached       # make_server attached it and started the host
            _until(lambda: _derived_and_idle(host))
            assert reads.deriving_threads and all(n.startswith(DERIVE_THREAD_PREFIX) for n in reads.deriving_threads)
            got: list = []

            def burst() -> None:
                with ThreadPoolExecutor(4) as pool:
                    got[:] = [f.result(timeout=60) for f in [pool.submit(_get, port, "/cockpit/panel/od.json") for _ in range(8)]]

            assert _provider_reads_during(reads, burst) == []
            assert all(s == 200 and body["rows"][0]["id"] == "r1" for s, body in got) and len(got) == 8


# ---------------------------------------------------------------------------------------------- (i) off is the legacy server


class TestOffIsLegacy:
    @pytest.mark.parametrize("value", [None, "0", "", "true"])
    def test_without_the_variable_set_to_one_nothing_attaches_and_requests_run_providers(
        self, tmp_path: Path, monkeypatch, value: "str | None"
    ) -> None:
        reads = Reads()
        with _default_server(tmp_path, monkeypatch, value, reads) as (port, httpd):
            assert httpd.stream_watcher._probe is None
            assert _get(port, "/cockpit/manifest.json")[0] == 200
            ck = httpd.get_cockpit()
            assert httpd._snapshot_host is None and ck.attached is False
            assert _derive_threads() == []
            mark = reads.mark()
            assert _get(port, "/cockpit/panel/od.json")[0] == 200
            reads.rows = [_row(1), _row(2)]
            time.sleep(1.7)      # past the shared read cache's window
            assert [r["id"] for r in _get(port, "/cockpit/panel/od.json")[1]["rows"]] == ["r1", "r2"]
            assert "od" in reads.since(mark)
            assert reads.deriving_threads == []

    def test_the_variable_is_read_when_the_server_is_built_not_per_request(self, tmp_path: Path, monkeypatch) -> None:
        reads = Reads()
        with _default_server(tmp_path, monkeypatch, None, reads) as (port, httpd):
            monkeypatch.setenv(COCKPIT_SNAPSHOTS_ENV, "1")      # set after the server was built: no effect on it
            assert _get(port, "/cockpit/manifest.json")[0] == 200
            assert httpd._snapshot_host is None and httpd.get_cockpit().attached is False

    def test_on_without_a_data_root_the_server_stays_on_the_providers_and_the_stream_still_watches(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        reads = Reads()
        with _default_server(tmp_path, monkeypatch, "1", reads) as (port, httpd):
            httpd.broker = None
            assert _get(port, "/cockpit/manifest.json")[0] == 200
            assert httpd._snapshot_host is None and httpd.get_cockpit().attached is False
            c, r = _open(port)
            try:
                _read_frame(r)
                reads.rows = [_row(1), _row(2)]
                httpd.stream_watcher.watch_s = 0.2
                got = _await_events(r)
                assert [(e[0], e[2].get("id")) for e in got] == [("panel-changed", "od")], got
            finally:
                c.close()

    def test_a_store_another_writer_holds_leaves_the_server_on_the_providers_and_says_why(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        reads = Reads()
        _env(monkeypatch, "1")
        _root, src = _install(tmp_path)
        httpd = make_server(src, host="127.0.0.1", port=0)
        holder = SnapshotWriter(httpd.broker.data_root / "snapshots.db")
        try:
            monkeypatch.setattr(providers, "build_default_cockpit", _spied(providers.build_default_cockpit, reads))
            ck = httpd.get_cockpit()
            err = capsys.readouterr().err
            assert "snapshots not attached" in err and "lock" in err.lower() and err.count("\n") == 1
            assert ck.attached is False and httpd._snapshot_host is None
            assert ck.manifest({"class": "none", "device_class": None})["panels"]
        finally:
            holder.close()
            httpd.server_close()


def _spied(real: Callable[..., Cockpit], reads: Reads) -> Callable[..., Cockpit]:
    def build(*a: Any, **k: Any) -> Cockpit:
        ck = real(*a, **k)
        reads.add_panel(ck)
        reads.spy_on(ck)
        return ck

    return build


# ---------------------------------------------------------------------------------------------- (j) the stream


class TestStream:
    def test_a_derive_that_changes_one_panel_is_exactly_one_panel_changed_for_it(self, tmp_path: Path, monkeypatch) -> None:
        reads = Reads()
        with _default_server(tmp_path, monkeypatch, "1", reads) as (port, httpd):
            assert _get(port, "/cockpit/manifest.json")[0] == 200
            host = httpd._snapshot_host
            _until(lambda: _every_plan_has_a_row(host))
            httpd.stream_watcher.token_s = 0.05
            c, r = _open(port)
            try:
                _read_frame(r)
                _settle(r)
                reads.rows = [_row(1), _row(2)]         # the source changed; the next derive of "od" publishes it
                host.loop.dirty(["od"], 1)
                got = _await_events(r)
                assert [(e[0], e[2]["id"]) for e in got] == [("panel-changed", "od")], got
                assert got[0][2]["etag"]
                assert _events_for(r, 0.5) == []        # and nothing after it: the heartbeat and the derives of unchanged panels are quiet
            finally:
                c.close()

    def test_a_change_to_a_store_the_serve_composes_itself_is_announced_through_the_watch_token(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # "mine" stands for a panel that reads a store of the serve's own: no derive publishes it, so only the serve's
        # watch token (here a file the downstream names) tells the stream to look again
        _env(monkeypatch, "1")
        root, src = _install(tmp_path)
        watched = root / ".levain" / "mine.touch"
        watched.write_text("0")
        store = {"rows": [_row(1)], "reads": 0}

        def read(ctx):
            store["reads"] += 1
            return Read(rows=tuple(store["rows"]))

        ck = providers.build_default_cockpit(src)
        _pin_minute(ck)
        ck.register(_simple("mine", rows=(), read=read))
        ck.mark_serve_composed(["mine"])
        with _serving(src, cockpit=ck, watch_paths=[watched]) as (port, httpd):
            host = httpd._snapshot_host
            assert host is not None
            _until(lambda: _every_plan_has_a_row(host))
            httpd.stream_watcher.token_s = 0.05
            c, r = _open(port)
            try:
                _read_frame(r)
                _settle(r)
                store["rows"] = [_row(1), _row(2)]
                before = [(e[0], e[2].get("id")) for e in _events_for(r, 0.8)]
                assert ("panel-changed", "mine") not in before, before   # the store moved but nothing the serve watches did
                watched.write_text("1")
                got = _collect(r, lambda evs: ("panel-changed", "mine") in evs, timeout=15.0)    # well inside the backstop
                assert ("panel-changed", "mine") in got, got
            finally:
                c.close()

    def test_a_subscribe_that_is_the_first_call_builds_the_cockpit_and_the_stream_still_runs(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # the watcher builds the cockpit under its own lock on the first subscribe: attaching must not need that lock
        reads = Reads()
        with _default_server(tmp_path, monkeypatch, "1", reads) as (port, httpd):
            assert httpd.cockpit is None
            c, r = _open(port)
            try:
                name, _eid, _data = _parse_first(r)
                assert name == "hello"
                assert httpd._snapshot_host is not None and httpd.get_cockpit().attached
                _until(lambda: _every_plan_has_a_row(httpd._snapshot_host))
                httpd.stream_watcher.token_s = 0.05
                _settle(r)
                reads.rows = [_row(1), _row(2)]
                httpd._snapshot_host.loop.dirty(["od"], 1)
                got = _collect(r, lambda evs: ("panel-changed", "od") in evs)
                assert ("panel-changed", "od") in got, got
            finally:
                c.close()


def _parse_first(resp: Any) -> "tuple[str, str, Any]":
    from tests.test_cockpit_stream import _parse

    return _parse(_read_frame(resp))


# ---------------------------------------------------------------------------------------------- (k) closing


class TestClose:
    def test_closing_the_server_ends_the_derive_threads_and_twice_is_safe(self, tmp_path: Path, monkeypatch) -> None:
        reads = Reads()
        _env(monkeypatch, "1")
        _root, src = _install(tmp_path)
        monkeypatch.setattr(providers, "build_default_cockpit", _spied(providers.build_default_cockpit, reads))
        httpd = make_server(src, host="127.0.0.1", port=0)
        httpd.get_cockpit()
        assert httpd._snapshot_host is not None and _derive_threads()
        httpd.server_close()
        assert _derive_threads() == [] and httpd._snapshot_host is None
        httpd.server_close()
        assert _derive_threads() == []

    def test_a_server_that_never_attached_closes_as_before(self, tmp_path: Path, monkeypatch) -> None:
        _env(monkeypatch, None)
        _root, src = _install(tmp_path)
        httpd = make_server(src, host="127.0.0.1", port=0)
        httpd.get_cockpit()
        httpd.server_close()
        httpd.server_close()
        assert _derive_threads() == []

    def test_a_downstream_server_closes_its_host(self, tmp_path: Path, monkeypatch) -> None:
        _env(monkeypatch, "1")
        _root, src = _install(tmp_path)
        ck = providers.build_default_cockpit(src)
        httpd = make_server(src, host="127.0.0.1", port=0, cockpit=ck)
        assert httpd._snapshot_host is not None and _derive_threads()
        httpd.server_close()
        assert _derive_threads() == []


class _StubbornHost:
    """A host whose stop() answers with threads still alive until told otherwise, and records the timeout it was given."""

    def __init__(self, real: SnapshotHost) -> None:
        self.real = real
        self.timeouts: list[Any] = []
        self.alive: list[Any] = [threading.current_thread()]

    def stop(self, timeout: Any = None) -> list:
        self.timeouts.append(timeout)
        return list(self.alive)


class TestCloseIsBounded:
    def test_the_host_is_stopped_with_a_bound_and_its_handle_is_kept_until_stop_returned_clean(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _env(monkeypatch, "1")
        _root, src = _install(tmp_path)
        httpd = make_server(src, host="127.0.0.1", port=0)
        httpd.get_cockpit()
        real = httpd._snapshot_host
        assert real is not None
        stub = _StubbornHost(real)
        httpd._snapshot_host = stub        # type: ignore[assignment]
        try:
            httpd.server_close()
            assert len(stub.timeouts) == 1 and isinstance(stub.timeouts[0], (int, float)) and stub.timeouts[0] > 0
            assert httpd._snapshot_host is stub             # threads were still alive: the handle stays for a retry
            stub.alive = []
            httpd.server_close()
            assert len(stub.timeouts) == 2 and httpd._snapshot_host is None
        finally:
            real.stop(timeout=10)


def _watch_data_root(monkeypatch) -> "list[Path]":
    """Every data root a host was opened on, so a test can look at the store after a failed open."""
    roots: list[Path] = []
    real = SnapshotHost.try_open.__func__          # type: ignore[attr-defined]

    def try_open(cls: Any, cockpit: Any, data_root: Any, **kw: Any) -> Any:
        roots.append(Path(data_root))
        return real(cls, cockpit, data_root, **kw)

    monkeypatch.setattr(SnapshotHost, "try_open", classmethod(try_open))
    return roots


def _store_is_free(root: Path) -> bool:
    host, _reason = SnapshotHost.try_open(Cockpit(), root)
    if host is not None:
        host.stop()
    return host is not None


class TestAHostIsNeverStranded:
    def test_an_attach_that_raises_in_get_cockpit_stops_the_host_and_frees_the_store(self, tmp_path: Path, monkeypatch) -> None:
        _env(monkeypatch, "1")
        _root, src = _install(tmp_path)
        roots = _watch_data_root(monkeypatch)
        monkeypatch.setattr(SnapshotHost, "attach", lambda self: (_ for _ in ()).throw(RuntimeError("attach refused")))
        httpd = make_server(src, host="127.0.0.1", port=0)
        try:
            with pytest.raises(RuntimeError, match="attach refused"):
                httpd.get_cockpit()
            assert httpd._snapshot_host is None and _derive_threads() == []
            assert _store_is_free(roots[0])
        finally:
            httpd.server_close()

    def test_an_attach_that_raises_in_make_server_stops_the_host_and_frees_the_store(self, tmp_path: Path, monkeypatch) -> None:
        _env(monkeypatch, "1")
        _root, src = _install(tmp_path)
        roots = _watch_data_root(monkeypatch)
        monkeypatch.setattr(SnapshotHost, "attach", lambda self: (_ for _ in ()).throw(RuntimeError("attach refused")))
        ck = providers.build_default_cockpit(src)
        with pytest.raises(RuntimeError, match="attach refused"):
            make_server(src, host="127.0.0.1", port=0, cockpit=ck)
        assert _derive_threads() == []
        assert _store_is_free(roots[0])

    def test_a_failure_after_the_host_started_in_make_server_leaves_no_thread_and_a_free_store(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _env(monkeypatch, "1")
        _root, src = _install(tmp_path)
        roots = _watch_data_root(monkeypatch)
        real_start = SnapshotHost.start

        def start_then_fail(self: SnapshotHost) -> None:
            real_start(self)
            raise RuntimeError("failed after start")

        monkeypatch.setattr(SnapshotHost, "start", start_then_fail)
        ck = providers.build_default_cockpit(src)
        with pytest.raises(RuntimeError, match="failed after start"):
            make_server(src, host="127.0.0.1", port=0, cockpit=ck)
        assert _derive_threads() == []
        assert _store_is_free(roots[0])

    def test_a_failure_after_the_host_started_in_get_cockpit_leaves_no_thread_and_a_free_store(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _env(monkeypatch, "1")
        _root, src = _install(tmp_path)
        roots = _watch_data_root(monkeypatch)
        real_start = SnapshotHost.start

        def start_then_fail(self: SnapshotHost) -> None:
            real_start(self)
            raise RuntimeError("failed after start")

        monkeypatch.setattr(SnapshotHost, "start", start_then_fail)
        httpd = make_server(src, host="127.0.0.1", port=0)
        try:
            with pytest.raises(RuntimeError, match="failed after start"):
                httpd.get_cockpit()
            assert httpd._snapshot_host is None and _derive_threads() == []
            assert _store_is_free(roots[0])
        finally:
            httpd.server_close()


class TestOpenSaysTheTrueThing:
    def test_an_already_attached_cockpit_is_said_to_be_attached_not_served_from_the_providers(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        _env(monkeypatch, "1")
        _root, src = _install(tmp_path)
        ck = providers.build_default_cockpit(src)
        (tmp_path / "other").mkdir()
        first = SnapshotHost(ck, tmp_path / "other")
        first.attach()
        capsys.readouterr()
        httpd = make_server(src, host="127.0.0.1", port=0, cockpit=ck)
        try:
            err = capsys.readouterr().err
            assert "already attached" in err and "serving from the providers" not in err and err.count("\n") == 1
            assert httpd._snapshot_host is None and ck.attached is True
        finally:
            httpd.server_close()
            first.stop(timeout=10)

    def test_the_flag_on_with_no_data_root_says_so_once_on_stderr(self, tmp_path: Path, monkeypatch, capsys) -> None:
        _env(monkeypatch, "1")
        _root, src = _install(tmp_path)
        httpd = make_server(src, host="127.0.0.1", port=0)
        try:
            httpd.broker = None
            capsys.readouterr()
            httpd.get_cockpit()
            err = capsys.readouterr().err
            assert "snapshots not attached" in err and "data root" in err and err.count("\n") == 1
            assert httpd._snapshot_host is None
        finally:
            httpd.server_close()


# ---------------------------------------------------------------------------------------------- the probe, on the real server


class TestServerProbeWiring:
    def test_the_server_builds_a_probe_only_when_attached_mode_is_on(self, tmp_path: Path, monkeypatch) -> None:
        _root, src = _install(tmp_path)
        _env(monkeypatch, "1")
        on = make_server(src, host="127.0.0.1", port=0)
        _env(monkeypatch, None)
        off = make_server(src, host="127.0.0.1", port=0)
        try:
            assert isinstance(on.stream_watcher._probe, ServerProbe)
            assert off.stream_watcher._probe is None
        finally:
            on.server_close()
            off.server_close()


class TestTheServerDiscoversOnceBeforeItAttaches:
    def _serve(self, tmp_path: Path, monkeypatch, gate: "threading.Event | None" = None):
        _env(monkeypatch, "1")
        _root, src = _install(tmp_path)
        httpd = make_server(src, host="127.0.0.1", port=0)
        ck = Cockpit()

        def discoverer(ctx: Any) -> list:
            if gate is not None:
                gate.wait(30)
            return [_simple("found", rows=[_row(1)])]

        ck.discover(discoverer)
        return httpd, ck

    def test_a_discovered_panel_is_known_when_the_host_is_opened_not_after_its_first_cycle(self, tmp_path: Path, monkeypatch) -> None:
        httpd, ck = self._serve(tmp_path, monkeypatch)
        try:
            httpd._open_snapshot_host(ck)
            assert httpd._snapshot_host is not None and ck.attached
            assert "found" in ck.panel_ids
        finally:
            httpd.server_close()

    def test_a_slow_discoverer_does_not_hold_the_open_past_the_bound(self, tmp_path: Path, monkeypatch) -> None:
        from levain.cockpit import snapshot_host as sh

        monkeypatch.setattr(sh, "FIRST_DISCOVERY_BOUND_S", 0.2)
        gate = threading.Event()
        httpd, ck = self._serve(tmp_path, monkeypatch, gate)
        try:
            began = time.monotonic()
            httpd._open_snapshot_host(ck)
            assert time.monotonic() - began < 5.0
            assert httpd._snapshot_host is not None and ck.attached and "found" not in ck.panel_ids
        finally:
            gate.set()
            httpd.server_close()
