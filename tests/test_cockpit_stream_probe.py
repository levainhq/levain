"""The Watcher on a change probe: ``change_seq()`` is looked at every token_s and ``snapshot()`` is taken only when
it moved or the backstop passed. Same short real timers as tests/test_cockpit_stream.py, so none of it waits on
production cadence."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from levain.cockpit import stream

from tests.test_cockpit_k1 import _install
from tests.test_cockpit_stream import _Feed, _drain, _until


class _Probe:
    """A change probe the test mutates by hand. ``snapshots`` counts every ``snapshot()``; ``seq`` is the
    counter, which the test moves only when it means the content changed."""

    def __init__(self, etags: dict[str, str], set_version: str = "v1") -> None:
        self.etags = dict(etags)
        self.set_version = set_version
        self.seq = 0
        self.seq_looks = 0
        self.snapshots = 0
        self.fail_seq = False
        self.fail_snapshot = False
        self._lock = threading.Lock()

    def change_seq(self) -> int:
        with self._lock:
            self.seq_looks += 1
            if self.fail_seq:
                raise OSError("counter unavailable")
            return self.seq

    def snapshot(self) -> tuple[dict[str, str], str]:
        with self._lock:
            self.snapshots += 1
            if self.fail_snapshot:
                raise RuntimeError("snapshot unavailable")
            return dict(self.etags), self.set_version

    def move(self, **etags: str) -> None:
        """Change content the way a publish does: the etags and the counter together."""
        with self._lock:
            self.etags.update(etags)
            self.seq += 1


def _watch(probe: _Probe, **kw: Any) -> tuple[stream.Watcher, _Feed]:
    feed = _Feed()      # a manifest build the probe path must never reach
    kw.setdefault("watch_s", 3600)
    kw.setdefault("token_s", 0.01)
    kw.setdefault("backstop_s", 3600)
    return stream.Watcher(feed, epoch="E", probe=probe, **kw), feed


def _looks(probe: _Probe, n: int) -> bool:
    """True once the loop has looked at the counter ``n`` more times."""
    target = probe.seq_looks + n
    return _until(lambda: probe.seq_looks >= target)


class TestProbeWatcher:
    def test_a_counter_that_never_moves_announces_nothing_and_snapshots_once(self) -> None:
        probe = _Probe({"a": "1", "b": "1"})
        w, feed = _watch(probe)
        sub = w.subscribe(None)
        try:
            assert probe.snapshots == 1         # the baseline, on the subscribing thread
            probe.etags["a"] = "2"              # the db moved underneath (a heartbeat): the counter did not
            assert _looks(probe, 30)
            assert probe.snapshots == 1 and _drain(sub) == []
            assert feed.reads == 0              # no manifest build, ever
        finally:
            w.stop()

    def test_one_moved_etag_is_one_panel_changed_and_one_snapshot(self) -> None:
        probe = _Probe({"a": "1", "b": "1"})
        w, feed = _watch(probe)
        sub = w.subscribe(None)
        try:
            probe.move(a="2")
            assert _until(lambda: not sub.q.empty())
            assert _looks(probe, 10)
            (ev, data, eid), = _drain(sub)
            assert (ev, data) == ("panel-changed", {"id": "a", "etag": "2"}) and eid.endswith(":1")
            assert probe.snapshots == 2         # the baseline and the one after the move
            assert feed.reads == 0
        finally:
            w.stop()

    def test_an_added_or_removed_panel_is_manifest_changed(self) -> None:
        probe = _Probe({"a": "1"})
        w, _feed = _watch(probe)
        sub = w.subscribe(None)
        try:
            probe.move(b="1")
            assert _until(lambda: probe.snapshots == 2)
            with probe._lock:
                del probe.etags["a"]
                probe.seq += 1
            assert _until(lambda: probe.snapshots == 3)
            assert _until(lambda: sub.q.qsize() == 2)
            assert [e[0] for e in _drain(sub)] == ["manifest-changed", "manifest-changed"]
        finally:
            w.stop()

    def test_the_same_ids_under_a_new_set_version_is_manifest_changed(self) -> None:
        probe = _Probe({"a": "1"})
        w, _feed = _watch(probe)
        sub = w.subscribe(None)
        try:
            with probe._lock:
                probe.set_version = "v2"        # a day turned, a verb revision moved
                probe.seq += 1
            assert _until(lambda: not sub.q.empty())
            (ev, data, _eid), = _drain(sub)
            assert (ev, data) == ("manifest-changed", {})
            assert _looks(probe, 10)
            assert _drain(sub) == []            # and settled
        finally:
            w.stop()

    def test_the_backstop_takes_a_snapshot_with_no_move_and_announces_what_differs(self) -> None:
        probe = _Probe({"a": "1"})
        w, _feed = _watch(probe, backstop_s=0.1)
        sub = w.subscribe(None)
        try:
            assert _until(lambda: probe.snapshots >= 3)     # snapshots with the counter never moved
            assert _drain(sub) == []                        # nothing differed: nothing announced
            probe.etags["a"] = "2"                          # a change the counter cannot see
            assert _until(lambda: not sub.q.empty())
            (ev, data, _eid), = _drain(sub)
            assert (ev, data) == ("panel-changed", {"id": "a", "etag": "2"})
        finally:
            w.stop()

    def test_one_backstop_window_is_one_snapshot(self) -> None:
        probe = _Probe({"a": "1"})
        w, _feed = _watch(probe, backstop_s=0.3)
        w.subscribe(None)
        try:
            assert probe.snapshots == 1
            assert _looks(probe, 10)            # many looks well inside the window
            assert probe.snapshots == 1
            assert _until(lambda: probe.snapshots == 2)
        finally:
            w.stop()

    def test_a_probe_that_raises_does_not_kill_the_thread_and_it_recovers(self, caplog: Any) -> None:
        probe = _Probe({"a": "1"})
        w, _feed = _watch(probe, watch_s=0.05)
        sub = w.subscribe(None)
        try:
            with caplog.at_level("ERROR", logger="levain.cockpit.stream"):
                probe.fail_seq = True
                assert _until(lambda: probe.snapshots >= 3)      # an unknown counter: the slow tick snapshots
                probe.fail_snapshot = True
                assert _until(lambda: probe.snapshots >= 5)      # a failing snapshot is retried, not fatal
                probe.fail_snapshot = False
                probe.fail_seq = False
                probe.move(a="2")
                assert _until(lambda: not sub.q.empty())
            assert w.running
            (ev, data, _eid), = _drain(sub)
            assert (ev, data) == ("panel-changed", {"id": "a", "etag": "2"})
            assert len([r for r in caplog.records if "counter could not be read" in r.getMessage()]) == 1
        finally:
            w.stop()

    def test_a_failed_baseline_is_adopted_by_the_first_good_snapshot_and_pages_reload(self) -> None:
        probe = _Probe({"a": "1"})
        probe.fail_snapshot = True
        w, _feed = _watch(probe, watch_s=3600)
        sub = w.subscribe(None)
        try:
            probe.fail_snapshot = False
            probe.move(a="2")
            assert _until(lambda: not sub.q.empty())
            assert [e[0] for e in _drain(sub)] == ["manifest-changed"]
        finally:
            w.stop()

    def test_a_tick_driven_by_hand_reads_the_probe_not_the_manifest(self) -> None:
        probe = _Probe({"a": "1"})
        w, feed = _watch(probe, token_s=3600)
        sub = w.subscribe(None)
        try:
            probe.etags["a"] = "2"
            assert w.tick() is True
            (ev, data, _eid), = _drain(sub)
            assert (ev, data["id"]) == ("panel-changed", "a") and feed.reads == 0
        finally:
            w.stop()

    def test_set_probe_installs_one_while_stopped_and_refuses_while_running(self) -> None:
        probe = _Probe({"a": "1"})
        feed = _Feed(a="1")
        w = stream.Watcher(feed, epoch="E", watch_s=3600, token_s=0.01, backstop_s=3600)
        w.set_probe(probe)
        w.subscribe(None)
        try:
            assert probe.snapshots == 1 and feed.reads == 0
            with pytest.raises(RuntimeError):
                w.set_probe(None)
        finally:
            w.stop()


class _Churn(_Probe):
    """A counter that moves on every look, over a snapshot that takes ``cost_s`` to make."""

    cost_s = 0.15

    def change_seq(self) -> int:
        with self._lock:
            self.seq_looks += 1
            self.seq += 1
            return self.seq

    def snapshot(self) -> tuple[dict[str, str], str]:
        time.sleep(self.cost_s)
        return super().snapshot()


class TestThrottle:
    def test_a_counter_that_never_settles_cannot_run_snapshots_back_to_back(self) -> None:
        probe = _Churn({"a": "1"})
        w, _feed = _watch(probe)
        w.subscribe(None)
        began = time.monotonic()
        try:
            time.sleep(1.5)
        finally:
            w.stop()
        window = time.monotonic() - began
        # each snapshot is followed by at least twice its own duration before the next may start
        most = window / (3 * probe.cost_s) + 1
        assert 2 <= probe.snapshots - 1 <= most, (probe.snapshots, most)


class TestNoProbeIsUnchanged:
    def test_a_watcher_without_a_probe_builds_the_manifest_as_before(self) -> None:
        feed = _Feed(a="1")
        w = stream.Watcher(feed, epoch="E", watch_s=0.02)
        sub = w.subscribe(None)
        try:
            base = feed.reads
            feed.etags["a"] = "2"
            assert _until(lambda: not sub.q.empty())
            assert feed.reads > base
            (ev, data, _eid), = _drain(sub)
            assert (ev, data) == ("panel-changed", {"id": "a", "etag": "2"})
        finally:
            w.stop()

    def test_the_server_hands_its_change_probe_to_the_watcher_and_has_none_by_default(self, tmp_path: Path, monkeypatch) -> None:
        from levain.web_server import _LevainHTTPServer, make_server

        _root, src = _install(tmp_path)
        assert _LevainHTTPServer.change_probe is None
        httpd = make_server(src, host="127.0.0.1", port=0)
        try:
            assert httpd.stream_watcher._probe is None
        finally:
            httpd.server_close()
        probe = _Probe({"a": "1"})
        monkeypatch.setattr(_LevainHTTPServer, "change_probe", probe)
        httpd = make_server(src, host="127.0.0.1", port=0)
        try:
            assert httpd.stream_watcher._probe is probe
        finally:
            httpd.server_close()
