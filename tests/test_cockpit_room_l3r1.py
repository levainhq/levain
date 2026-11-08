"""L3 room r1 fixes on the room read: digit-bounded parameters that can neither raise past the parser nor leak a request
slot, an answer in hand never turned into a 503 by the waiter cap, the no-store branch refusing and waiting alike, and
a shutdown that ends a wait with no further read."""
from __future__ import annotations

import threading
import time

import pytest

from levain import web_server
from levain.cockpit import room as cockpit_room
from tests.test_cockpit_room import TOKEN, _call, _serve_room
from tests.test_cockpit_threads_mount import qdir  # noqa: F401
from tests.test_cockpit_room_wait import FAR, GET, _in_thread, _send, quick, w  # noqa: F401

HUGE = "9" * 5000           # past int()'s 4300-digit limit: isdigit() holds, int() raises a plain ValueError


def _free_slots(h) -> int:
    held = 0
    try:
        while h.request_gate.acquire(blocking=False):
            held += 1
    finally:
        for _ in range(held):
            h.request_gate.release()
    return held


@pytest.mark.parametrize("q", [f"after={HUGE}", f"after=1&wait={HUGE}", f"after=1&wait=1&live_seq={HUGE}",
                               "after=" + "1" * 21])
def test_a_huge_number_is_a_400_and_leaks_no_request_slot(tmp_path, w, q):  # noqa: F811
    with _serve_room(tmp_path, w) as (base, h):
        before = _free_slots(h)
        for _ in range(before + 2):          # more than the gate holds: a leak would turn the last ones into 503s
            status, body = _call(base, f"{GET}?{q}", method="GET")
            assert status == 400 and body["error"] == "bad_request"
        assert _free_slots(h) == before
        assert _call(base, GET, method="GET")[0] == 200


def test_the_parsers_bound_digits_and_serve_room_answers_400(tmp_path):
    assert cockpit_room.parse_cursor("after=" + "9" * 20)[0] == 10 ** 20 - 1
    for q in (f"after={HUGE}", "after=" + "9" * 21):
        with pytest.raises(cockpit_room.CursorError):
            cockpit_room.parse_cursor(q)
    with pytest.raises(cockpit_room.CursorError):
        cockpit_room.parse_wait(f"wait={HUGE}", 1)
    with pytest.raises(cockpit_room.CursorError):
        cockpit_room.parse_live_seq(f"live_seq={HUGE}")
    assert cockpit_room.serve_room(GET, directory=tmp_path, query=f"after={HUGE}")[0] == 400


def test_an_answer_in_hand_is_served_even_with_every_waiter_slot_held(tmp_path, w, monkeypatch):  # noqa: F811
    monkeypatch.setattr(cockpit_room, "WAITERS_MAX", 1, raising=False)
    with _serve_room(tmp_path, w) as (base, _h):
        _send(base, w, 2)
        t, out = _in_thread(_call, base, f"{GET}?after={FAR}&wait=3", None, TOKEN, "GET")   # holds the one slot
        time.sleep(0.3)
        assert t.is_alive()
        t0 = time.monotonic()
        status, body = _call(base, f"{GET}?after=0&wait=10", method="GET")                   # news already in hand
        assert status == 200 and len(body["stream"]) == 2
        status, body = _call(base, f"{GET}?after={FAR}&thread=th-{'0' * 32}&wait=10", method="GET")   # a reset in hand
        assert status == 200 and body["reset"] is True
        assert time.monotonic() - t0 < 2
        assert _call(base, f"{GET}?after={FAR}&wait=10", method="GET")[0] == 503            # a real waiter: the cap holds
        t.join(6)
        assert out and out[0][0] == 200


def test_the_no_store_branch_refuses_and_waits_like_the_store_branch(tmp_path, w, monkeypatch):  # noqa: F811
    monkeypatch.setattr(web_server._Handler, "_store_directory", lambda self: None)
    with _serve_room(tmp_path, w) as (base, _h):
        for q in ("wait=1", "after=1&wait=x", f"after=1&wait={cockpit_room.WAIT_MAX_S + 1}", "live_seq=x",
                  f"after={HUGE}"):
            status, body = _call(base, f"{GET}?{q}", method="GET")
            assert status == 400 and body["error"] == "bad_request", q
        t0 = time.monotonic()
        status, body = _call(base, f"{GET}?after=0&wait=1", method="GET")
        assert status == 200 and body["stream"] == [] and body["room_thread"] is None and body["next"] == 0
        assert time.monotonic() - t0 >= 0.8   # the wait was honoured: a long-polling client cannot spin


def test_a_shutdown_ends_the_wait_with_no_further_read():
    reads = []
    stop = threading.Event()

    def read():
        reads.append(1)
        return 200, {"stream": [], "next": 5}

    threading.Timer(0.1, stop.set).start()
    t0 = time.monotonic()
    got = cockpit_room.wait_for_news(read, 20, stop=stop, tick=5)
    assert got == (200, {"stream": [], "next": 5}) and time.monotonic() - t0 < 2
    assert len(reads) == 1
