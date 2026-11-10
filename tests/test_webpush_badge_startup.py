"""A host-served install records its needs count when the server starts and keeps it fresh while it idles, so a hook's push
from a child process (no question store of its own) never falls back to the native store's different number; a record that
cannot be decoded is host-unknown and never aborts a push."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from levain.webpush import needs
from tests.test_cockpit_grant_revoke import _signer
from tests.test_cockpit_sign_routes import _serving
from tests.test_grant_phone import RP, world  # noqa: F401  (world is a fixture)

serves = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


def _wait_for(pred: Any, secs: float = 5.0) -> bool:
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return bool(pred())


@serves
def test_the_server_records_its_count_before_it_serves(world, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LEVAIN_HOME", str(world.qdir.parent))
    assert not (world.qdir / needs.COUNT_FILE).exists()
    with _serving(tmp_path / "inst", signer=_signer(world), extra_hosts=[RP]) as (_url, httpd):
        doc = json.loads((world.qdir / needs.COUNT_FILE).read_text())    # no request made: the start itself recorded
        assert doc["host"] is True and doc["n"] is not None
        assert needs.resolved_badge(world.qdir) == doc["n"]               # a child's omitted badge is the host's, not native


@serves
def test_an_idle_server_keeps_its_record_fresh(world, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LEVAIN_HOME", str(world.qdir.parent))
    monkeypatch.setattr(needs, "KEEP_EVERY_S", 0.05, raising=False)
    with _serving(tmp_path / "inst", signer=_signer(world), extra_hosts=[RP]) as (_url, _httpd):
        f = world.qdir / needs.COUNT_FILE
        f.write_text(json.dumps({"n": 7, "at": time.time() - needs.COUNT_FRESH_S - 30, "host": True}))
        assert _wait_for(lambda: needs.recorded_count(world.qdir) is not None), "the idle server never refreshed its record"


def test_an_undecodable_record_is_host_unknown_not_a_failure(tmp_path: Path) -> None:
    (tmp_path / needs.COUNT_FILE).write_bytes(b"\xff\xfe\x00{")
    assert needs.resolved_badge(tmp_path) is None
    assert needs.recorded_count(tmp_path) is None


def test_the_keeper_stops_with_its_event(tmp_path: Path, monkeypatch) -> None:
    import threading

    class Host:
        calls = 0

        def open_questions(self, agent=None):
            Host.calls += 1
            return []

    stop = threading.Event()
    t = threading.Thread(target=needs.keep_recorded, args=(tmp_path, Host(), stop), kwargs={"every": 0.02}, daemon=True)
    t.start()
    assert _wait_for(lambda: Host.calls >= 2)
    stop.set()
    t.join(timeout=2)
    assert not t.is_alive()


@serves
def test_a_serve_that_cannot_start_leaves_no_needs_record(world, tmp_path, monkeypatch) -> None:
    from levain.coordinator.room_serve import RoomServeError
    from levain.web_server import make_server
    from tests.test_cockpit_k2a import TOKEN, _install
    from tests.test_cockpit_room_join import _store
    from tests.test_coordinator_room_serve import scripted

    monkeypatch.setenv("LEVAIN_HOME", str(world.qdir.parent))
    _rs_store, rs, launcher = scripted(tmp_path, store=_store(world.qdir))
    _root, src, _ids = _install(tmp_path / "inst")

    def boom() -> None:
        raise RoomServeError("recover failed")

    monkeypatch.setattr(rs, "start", boom)
    with pytest.raises(RoomServeError, match="recover failed"):
        make_server(src, host="127.0.0.1", port=0, write_token=TOKEN, signer=_signer(world), extra_hosts=[RP], room_serve=rs)
    assert not (world.qdir / needs.COUNT_FILE).exists()      # a stale host record would silence every later push's badge


def test_the_keeper_waits_one_interval_before_its_first_record(tmp_path: Path) -> None:
    import threading

    calls: list[float] = []

    class Host:
        def open_questions(self) -> list:
            calls.append(time.monotonic())
            return []

    stop = threading.Event()
    t = threading.Thread(target=needs.keep_recorded, args=(tmp_path, Host(), stop), kwargs={"every": 0.3}, daemon=True)
    t0 = time.monotonic()
    t.start()
    time.sleep(0.1)
    assert calls == []
    assert _wait_for(lambda: len(calls) >= 1) and calls[0] - t0 >= 0.25
    stop.set()
    t.join(timeout=2)


def test_a_keeper_thread_records_a_number_from_the_real_store(tmp_path: Path) -> None:
    import threading

    from levain.consent.questions import QuestionStore

    qdir = tmp_path / "q"
    qdir.mkdir(mode=0o700)
    store = QuestionStore(qdir)
    store.open_questions()                      # opened on THIS thread; the keeper reads it from another
    stop = threading.Event()
    t = threading.Thread(target=needs.keep_recorded, args=(qdir, store, stop), kwargs={"every": 0.02}, daemon=True)
    t.start()
    assert _wait_for(lambda: (qdir / needs.COUNT_FILE).exists())
    stop.set()
    t.join(timeout=2)
    assert json.loads((qdir / needs.COUNT_FILE).read_text())["n"] == 0
