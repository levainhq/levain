"""492 R3 x R1/R2: ``live`` rides every room read (never stored), ``live_seq`` wakes a long-poll, the manifest says so."""
from __future__ import annotations

import time

import pytest

from levain.cockpit import room as cockpit_room
from levain.coordinator import room_live
from tests.test_cockpit_room import TOKEN, _call, _serve_room
from tests.test_cockpit_threads_mount import qdir  # noqa: F401
from tests.test_cockpit_room_wait import (FAR, GET, _dcall, _dserve, _in_thread, _pair, _revoke, quick,  # noqa: F401
                                          w)


@pytest.fixture(autouse=True)
def _clean(w):  # noqa: F811
    room_live.forget(w.launch)
    yield
    room_live.forget(w.launch)


def test_live_is_present_while_streaming_and_null_after_the_frame(tmp_path, w):  # noqa: F811
    with _serve_room(tmp_path, w) as (base, _h):
        assert _call(base, GET, method="GET")[1]["live"] is None
        sink = room_live.Sink(w.launch)
        sink.delta("t1-1", "Hel")
        sink.delta("t1-1", "lo")
        for q in ("", f"?after={FAR}"):                      # cold and cursor reads alike
            live = _call(base, GET + q, method="GET")[1]["live"]
            assert live["text"] == "Hello" and live["turn"] == "t1-1" and live["seq"] >= 2
        sink.clear()                                          # the whole frame landed
        assert _call(base, GET, method="GET")[1]["live"] is None
        assert not any("Hello" in (i.get("body") or "") for i in _call(base, GET, method="GET")[1]["stream"])


def test_a_wait_wakes_on_a_live_seq_advance_and_not_without_live_seq(tmp_path, w):  # noqa: F811
    with _serve_room(tmp_path, w) as (base, _h):
        cursor = _call(base, GET, method="GET")[1]["next"] or 0
        sink = room_live.Sink(w.launch)
        sink.delta("t1-1", "a")
        seq = room_live.snapshot(w.launch)["seq"]
        t, out = _in_thread(_call, base, f"{GET}?after={cursor}&live_seq={seq}&wait=10", None, TOKEN, "GET")
        time.sleep(0.3)
        assert t.is_alive()
        t0 = time.monotonic()
        sink.delta("t1-1", "b")
        t.join(5)
        status, body = out[0]
        assert status == 200 and body["live"]["text"] == "ab" and body["live"]["seq"] > seq and body["stream"] == []
        assert time.monotonic() - t0 < 3
        # an old client (no live_seq) does not wake on live changes: it runs to its deadline
        t0 = time.monotonic()
        status, body = _call(base, f"{GET}?after={cursor}&wait=1", method="GET")
        assert status == 200 and time.monotonic() - t0 >= 0.8
        # live gone with the client holding a seq wakes it (turn end, nothing stored)
        t, out = _in_thread(_call, base, f"{GET}?after={cursor}&live_seq={body['live']['seq']}&wait=10", None, TOKEN, "GET")
        time.sleep(0.3)
        sink.clear()
        t.join(5)
        assert out and out[0][1]["live"] is None


@pytest.mark.parametrize("v", ["x", "-1", "", "1.5"])
def test_a_bad_live_seq_is_a_400(tmp_path, w, v):  # noqa: F811
    with _serve_room(tmp_path, w) as (base, _h):
        for q in (f"after=1&wait=1&live_seq={v}", f"live_seq={v}"):
            status, body = _call(base, f"{GET}?{q}", method="GET")
            assert status == 400 and body["error"] == "bad_request"


def test_a_revoked_device_still_gets_403_within_a_tick_while_live_changes(tmp_path, w):  # noqa: F811
    with _dserve(tmp_path, w) as port:
        dev, tok = _pair(port)
        sink = room_live.Sink(w.launch)
        sink.delta("t1-1", "x")
        seq = room_live.snapshot(w.launch)["seq"]
        t, out = _in_thread(_dcall, port, "GET", f"{GET}?after={FAR}&live_seq={seq}&wait=20", None, tok)
        time.sleep(0.4)
        assert t.is_alive()
        _revoke(port, dev)
        t0 = time.monotonic()
        t.join(8)          # the revoke lands first; the next tick's recheck refuses before any read
        assert out and out[0][0] in (403, 200)
        if out[0][0] == 200:   # a live wake raced the revoke: the very next read is refused
            assert _dcall(port, "GET", f"{GET}?after={FAR}", token=tok)[0] == 403
