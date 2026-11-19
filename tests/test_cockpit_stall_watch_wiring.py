"""The stall watch inside ``run_web_server`` (levain#157): a ``cockpit-stall-watch`` thread runs while the server
serves, dumps under ``<broker data root>/stalls``, ends with the serve, and a failure to set it up never stops the
serve. ``make_server`` is replaced by a fake whose ``serve_forever`` records what it sees; no socket is opened."""
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


def _serve(tmp_path, monkeypatch, broker, *, interrupt: bool = False):
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
    rc = ws.run_web_server(tmp_path, open_browser=False, write=False)
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
    assert seen["threads"] == [] and _watch_threads() == []           # no watch, no thread left behind
    assert "stall watch not started" in capsys.readouterr().err


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
