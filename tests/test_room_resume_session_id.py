"""RM-E seam 2: a resumed room conversation reports the session id it was resumed from (RM0 run D: same session_id,
history kept), so the room's own ended launch on the same room thread is no claim on that id. Any other launch, and a
room launch still running, still is."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from levain.coordinator import lease, room
from levain.coordinator.envelopes import EnvelopeStore
from levain.coordinator.store import Part

T0 = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def store(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    s = EnvelopeStore(d, clock=Clock())
    s.coord_set_agent("none")
    return s


def _thread(store, title="the room"):
    d = store.accept_drop(title, surface="cli")[0]
    return store.split(d.drop_id, [Part("note", title)])[0].thread_id


def _open(store, thread):
    with store._write() as (conn, now):
        return lease.open_room_launch(conn, now, harness="claude", model=None, cwd=str(store.path.parent),
                                      room_thread=thread, pid=4242).launch_id


def _activate(store, launch, sid):
    with store._write() as (conn, now):
        return lease.activate_room(conn, now, launch, harness_session_id=sid)


def _end(store, launch, kind="ended"):
    with store._write() as (conn, now):
        lease.end_launch(conn, now, launch, kind, "test")


def test_a_resumed_room_launch_takes_the_id_its_ended_predecessor_recorded(store):
    thread = _thread(store)
    with store._write() as (conn, _now):
        room.open_room_thread(conn, thread)
    first = _open(store, thread)
    assert _activate(store, first, "sid-1")[1] is None
    _end(store, first)
    second = _open(store, thread)
    got, why = _activate(store, second, "sid-1")
    assert why is None and got.state == "active"
    with store._write() as (conn, _now):
        assert lease.by_harness_session(conn, "sid-1", room=True).launch_id == second


def test_a_room_launch_still_running_keeps_its_id(store):
    thread = _thread(store)
    first = _open(store, thread)
    assert _activate(store, first, "sid-1")[1] is None
    second = _open(store, thread)
    with pytest.raises(lease.LeaseRefused, match="already recorded"):
        _activate(store, second, "sid-1")
    assert store.launch(second).state == "pending"


def test_another_rooms_ended_launch_keeps_its_id(store):
    a, b = _thread(store, "a"), _thread(store, "b")
    first = _open(store, a)
    assert _activate(store, first, "sid-1")[1] is None
    _end(store, first)
    other = _open(store, b)
    with pytest.raises(lease.LeaseRefused, match="already recorded"):
        _activate(store, other, "sid-1")
