"""RM-E seam 2: a resumed room conversation reports the session id it was resumed from (RM0 run D: same session_id,
history kept), so when a pending room launch ASKED to resume that id (``resumed_from``), an ended room launch of the
same harness sharing its room thread is no claim on it. Any other launch, a room launch still running, and an id the
launch did not ask for, still is."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from levain.coordinator import lease, room
from levain.coordinator.envelopes import EnvelopeStore
from levain.coordinator.store import Part, next_gseq

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


def _open(store, thread, resumed_from=None, harness="claude"):
    kw = {} if resumed_from is None else {"resumed_from": resumed_from}
    with store._write() as (conn, now):
        return lease.open_room_launch(conn, now, harness=harness, model=None, cwd=str(store.path.parent),
                                      room_thread=thread, pid=4242, **kw).launch_id


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
    second = _open(store, thread, resumed_from="sid-1")
    got, why = _activate(store, second, "sid-1")
    assert why is None and got.state == "active"
    with store._write() as (conn, _now):
        assert lease.by_harness_session(conn, "sid-1", room=True).launch_id == second
    # a chain: a third launch resuming the same conversation is not held by either ended predecessor
    _end(store, second)
    third = _open(store, thread, resumed_from="sid-1")
    assert _activate(store, third, "sid-1")[1] is None
    # the pending row's ask is not read as the launch's own session id
    assert store.launch(third).harness_session_id == "sid-1"


def test_a_room_launch_that_did_not_ask_to_resume_cannot_take_the_id(store):
    """FAILS on the RM-E tip (L3_rme_r1 codex MED 6): any ended same-thread room launch exempted its id."""
    thread = _thread(store)
    first = _open(store, thread)
    assert _activate(store, first, "sid-1")[1] is None
    _end(store, first)
    for asked in (None, "sid-other"):
        second = _open(store, thread, resumed_from=asked)
        with pytest.raises(lease.LeaseRefused, match="already recorded"):
            _activate(store, second, "sid-1")
        _end(store, second, "failed")


def test_another_harness_room_on_the_same_thread_cannot_take_the_id(store):
    """FAILS on the RM-E tip (L3_rme_r1 codex MED 6): an ended Claude room's id was open to a Codex room."""
    thread = _thread(store)
    first = _open(store, thread)
    assert _activate(store, first, "sid-1")[1] is None
    _end(store, first)
    other = _open(store, thread, resumed_from="sid-1", harness="codex")
    with pytest.raises(lease.LeaseRefused, match="already recorded"):
        _activate(store, other, "sid-1")


def test_thread_membership_not_the_verbatim_array_decides_the_same_room(store):
    """L3_rme_r1 deepseek LOW-MED 2: a predecessor whose threads array differs in text (an extra member) but holds the
    room thread is the same room. open_room_launch writes one thread and a launch row is append-only, so the
    predecessor is written by hand here."""
    thread, extra = _thread(store), _thread(store, "extra")
    first = "ln-" + "1" * 32
    with store._write() as (conn, now):
        conn.execute("INSERT INTO launches (launch_id, gseq, session_key, harness, role, threads, model, cwd, token, "
                     "placement, at, user_scope, role_ref) VALUES (?, ?, 's-0000000000000001', 'claude', 'room', ?, "
                     "NULL, NULL, ?, 'operator-uid', ?, 0, NULL)",
                     (first, next_gseq(conn, now), json.dumps([extra, thread]), lease.ROOM_TOKEN, now))
        lease._launch_event(conn, now, first, "pending", "room opened", until=now)
        lease._launch_event(conn, now, first, "active", "spawned", harness_session_id="sid-1")
        lease._launch_event(conn, now, first, "ended", "test")
    second = _open(store, thread, resumed_from="sid-1")
    assert _activate(store, second, "sid-1")[1] is None


def test_a_room_launch_still_running_keeps_its_id(store):
    thread = _thread(store)
    first = _open(store, thread)
    assert _activate(store, first, "sid-1")[1] is None
    second = _open(store, thread, resumed_from="sid-1")
    with pytest.raises(lease.LeaseRefused, match="already recorded"):
        _activate(store, second, "sid-1")
    assert store.launch(second).state == "pending"


def test_another_rooms_ended_launch_keeps_its_id(store):
    a, b = _thread(store, "a"), _thread(store, "b")
    first = _open(store, a)
    assert _activate(store, first, "sid-1")[1] is None
    _end(store, first)
    other = _open(store, b, resumed_from="sid-1")
    with pytest.raises(lease.LeaseRefused, match="already recorded"):
        _activate(store, other, "sid-1")


def test_rme_fix2_r2_no_reader_takes_a_pending_rows_ask_for_the_launchs_own_session_id(store):
    """L3_rme_r2 complement UNSETTLED (codex 6): the pending row carries the id the launch ASKED to resume. In the
    pending window every reader of a launch's session id resolves the older launch that recorded it on its ``active``
    row, or nothing, never the pending one. A pin: it passed on the rme-fix1 tip (each reader filters kind = 'active'
    or takes the id from the active row)."""
    thread = _thread(store)
    with store._write() as (conn, _now):
        room.open_room_thread(conn, thread)
    # an ask with no launch behind it resolves nothing
    lonely = _open(store, thread, resumed_from="sid-none")
    with store._write() as (conn, _now):
        assert lease.by_harness_session(conn, "sid-none", room=True) is None
        assert lease.by_harness_session(conn, "sid-none") is None
    assert store.launch(lonely).harness_session_id is None and store.launch_by_harness_session("sid-none") is None
    _end(store, lonely, "failed")
    first = _open(store, thread)
    assert _activate(store, first, "sid-1")[1] is None
    _end(store, first)
    second = _open(store, thread, resumed_from="sid-1")               # pending, asking for sid-1
    with store._write() as (conn, _now):
        assert lease.by_harness_session(conn, "sid-1", room=True).launch_id == first
        assert lease.by_harness_session(conn, "sid-1") is None
        assert lease.launch(conn, second).harness_session_id is None
        assert [ln.harness_session_id for ln in lease.launches(conn) if ln.launch_id == second] == [None]
    assert store.launch(second).harness_session_id is None and store.launch(second).state == "pending"
    assert store.launch_by_harness_session("sid-1") is None
    # a chain: the newest ACTIVE holder answers while a third launch's ask is pending
    assert _activate(store, second, "sid-1")[1] is None
    _end(store, second)
    third = _open(store, thread, resumed_from="sid-1")
    with store._write() as (conn, _now):
        assert lease.by_harness_session(conn, "sid-1", room=True).launch_id == second
    assert store.launch(third).harness_session_id is None
