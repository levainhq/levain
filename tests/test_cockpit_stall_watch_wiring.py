"""The stall watch inside ``run_web_server`` (levain#157): a ``cockpit-stall-watch`` thread runs while the server
serves, dumps under ``<broker data root>/stalls`` (the thread makes that directory, the wiring only passes the path),
ends with the serve, and a failure to set it up or end it never stops the serve. ``make_server`` is replaced by a fake whose ``serve_forever`` records what it sees; no socket is opened."""
from __future__ import annotations

import threading
from pathlib import Path

import levain.web_server as ws


def _source(tmp_path: Path):
    from levain.dashboard import AnnealPaths, SubstrateSource

    (tmp_path / "memory.db").write_bytes(b"")
    return SubstrateSource(anneal=AnnealPaths.from_db(tmp_path / "memory.db"))


def _watch_threads() -> list:
    return [t for t in threading.enumerate() if t.name == "cockpit-stall-watch" and t.is_alive()]


class _Broker:
    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root


def _serve(tmp_path, monkeypatch, broker, *, interrupt: bool = False, write: bool = False):
    """Run ``run_web_server`` over a fake server; returns (exit code, what serve_forever saw)."""
    seen: dict = {}

    class _Httpd:
        server_address = ("127.0.0.1", 7466)
        write_token = None

        def serve_forever(self):
            seen["threads"] = _watch_threads()
            seen["entered"] = True
            if interrupt:
                raise KeyboardInterrupt

        def server_close(self):
            seen["closed"] = True

    _Httpd.broker = broker
    monkeypatch.setattr(ws, "_resolve_source", lambda p: _source(tmp_path))
    monkeypatch.setattr(ws, "make_server", lambda *a, **k: _Httpd())
    rc = ws.run_web_server(tmp_path, open_browser=False, write=write)
    return rc, seen


def test_the_stall_watch_runs_while_serving_dumps_under_the_broker_data_root_and_ends_with_the_serve(
        tmp_path, monkeypatch):
    from levain.cockpit import stall_watch

    dirs: list = []
    real = stall_watch.StallWatch

    class _Recording(real):
        def __init__(self, dump_dir, **kw):
            dirs.append(Path(dump_dir))
            super().__init__(dump_dir, **kw)

    monkeypatch.setattr(stall_watch, "StallWatch", _Recording)
    data_root = tmp_path / "ledger" / "cockpit"
    data_root.mkdir(parents=True)

    assert _watch_threads() == []
    rc, seen = _serve(tmp_path, monkeypatch, _Broker(data_root))

    assert rc == 0 and seen["entered"] and seen["closed"]
    assert len(seen["threads"]) == 1                      # alive while serve_forever ran
    assert _watch_threads() == []                         # gone once the serve returned
    assert dirs == [data_root / "stalls"] and (data_root / "stalls").is_dir()


def test_the_stall_watch_ends_when_the_serve_is_interrupted(tmp_path, monkeypatch):
    data_root = tmp_path / "cockpit"
    data_root.mkdir()

    rc, seen = _serve(tmp_path, monkeypatch, _Broker(data_root), interrupt=True)

    assert rc == 0 and len(seen["threads"]) == 1
    assert _watch_threads() == []


def test_a_dump_directory_that_cannot_be_made_leaves_the_serve_running(tmp_path, monkeypatch, capsys):
    data_root = tmp_path / "not-a-directory"
    data_root.write_text("a file where the data root should be")      # mkdir under a file always fails

    rc, seen = _serve(tmp_path, monkeypatch, _Broker(data_root))

    assert rc == 0 and seen["entered"] and seen["closed"]             # the serve ran to completion
    assert _watch_threads() == []                                     # the thread ended with the serve
    assert "dump directory not made" in capsys.readouterr().err       # the watch thread said why, once
    assert data_root.read_text() == "a file where the data root should be"


def test_the_wiring_makes_no_directory_before_serve_forever(tmp_path, monkeypatch):
    from levain.cockpit import stall_watch

    class _Inert:
        def __init__(self, dump_dir, **kw):
            self.dir = Path(dump_dir)

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(stall_watch, "StallWatch", _Inert)
    data_root = tmp_path / "cockpit"
    data_root.mkdir()

    rc, seen = _serve(tmp_path, monkeypatch, _Broker(data_root))

    assert rc == 0 and seen["entered"]
    assert not (data_root / "stalls").exists()                        # a start that does nothing makes nothing


def test_a_missing_data_root_is_not_made_by_the_serve(tmp_path, monkeypatch):
    data_root = tmp_path / "ledger" / "cockpit"                       # neither level exists

    rc, seen = _serve(tmp_path, monkeypatch, _Broker(data_root))

    assert rc == 0 and seen["entered"] and seen["closed"]
    assert not (tmp_path / "ledger").exists()


def test_a_stall_watch_that_will_not_start_or_stop_leaves_the_serve_running(tmp_path, monkeypatch, capsys):
    from levain.cockpit import stall_watch

    class _Broken:
        def __init__(self, dump_dir, **kw):
            pass

        def start(self):
            raise RuntimeError("no threads")

        def stop(self):
            raise RuntimeError("cannot stop")

    monkeypatch.setattr(stall_watch, "StallWatch", _Broken)
    data_root = tmp_path / "cockpit"
    data_root.mkdir()

    rc, seen = _serve(tmp_path, monkeypatch, _Broker(data_root))

    err = capsys.readouterr().err
    assert rc == 0 and seen["entered"] and seen["closed"]
    assert "stall watch not started: no threads" in err and "stall watch not stopped: cannot stop" in err


def test_a_serve_with_no_broker_starts_no_stall_watch(tmp_path, monkeypatch):
    rc, seen = _serve(tmp_path, monkeypatch, None)

    assert rc == 0 and seen["entered"] and seen["threads"] == []


def test_a_closed_stderr_does_not_make_the_never_fatal_handler_fatal(tmp_path, monkeypatch):
    import io
    import sys

    from levain.cockpit import stall_watch

    class _Broken:
        def __init__(self, dump_dir, **kw):
            pass

        def start(self):
            raise RuntimeError("no threads")

        def stop(self):
            raise RuntimeError("cannot stop")

    monkeypatch.setattr(stall_watch, "StallWatch", _Broken)
    data_root = tmp_path / "cockpit"
    data_root.mkdir()
    closed = io.StringIO()
    closed.close()
    monkeypatch.setattr(sys, "stderr", closed)

    rc, seen = _serve(tmp_path, monkeypatch, _Broker(data_root))

    assert rc == 0 and seen["entered"] and seen["closed"]             # both messages hit a closed stderr and raised nothing


def test_a_ctrl_c_inside_the_stall_watch_stop_does_not_skip_the_desk_watcher_stop(tmp_path, monkeypatch):
    from levain.cockpit import stall_watch
    import levain.desk.supervise as supervise

    stopped: list = []

    class _Interrupted:
        def __init__(self, dump_dir, **kw):
            pass

        def start(self):
            pass

        def stop(self):
            raise KeyboardInterrupt

    class _Desk:
        def __init__(self, store_for):
            pass

        def start(self):
            return self

        def stop(self):
            stopped.append("desk")

    monkeypatch.setattr(stall_watch, "StallWatch", _Interrupted)
    monkeypatch.setattr(supervise, "Watcher", _Desk)
    data_root = tmp_path / "cockpit"
    data_root.mkdir()

    try:
        _serve(tmp_path, monkeypatch, _Broker(data_root), write=True)
    except KeyboardInterrupt:
        pass                                                           # the Ctrl-C itself may propagate; the stop must not be skipped
    assert stopped == ["desk"]
