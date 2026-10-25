"""control_client.follow_events: the SESSION-EVENTS stream (CONTRACT §3.6) read by dispatch's side, over a real
`ControlServer` socket on test_slot_control_events' rig (the pattern `test_stream_over_a_control_server_socket` uses)."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from levain.slot import Refused
from levain.slot import control as C
from levain.slot import control_client as CC
from levain.slot import events as EV
from levain.slot import records as R
from levain.slot import routemaps as RM
from tests.test_routemaps import st  # noqa: F401 - the signed store fixture
from tests.test_slot_control_events import ERig, cas
from tests.test_tickets import DISPATCH_UID, KEYS_UID, OP_UID, Clock, bound_session, sync_free


class Served:
    """An ERig whose Control is served on a real socket; ``uid`` is the peer uid the server reports."""

    def __init__(self, rig: ERig) -> None:
        self.rig = rig
        self.uid = DISPATCH_UID
        self.path = Path(tempfile.mkdtemp(dir="/tmp")) / "c.sock"      # a unix socket path must stay short
        rig.control.stop()
        rig.control = C.Control(C.StateStore(rig.sdir), OP_UID, dispatch_uid=DISPATCH_UID, keys_uid=KEYS_UID,
                                routes=RM.ControlRoutes(rig.st.routes), tickets=rig.tickets, clock=rig.clock,
                                events=rig.events)
        self.server = C.ControlServer(rig.control, self.path, peer_uid=lambda _s: self.uid)
        self.server.start()
        sync_free(rig.control)
        rig.attach(rig.cr.head())

    def stop(self) -> None:
        self.server.stop()


@pytest.fixture
def served(st):  # noqa: F811
    s = Served(ERig(st, Clock()))
    try:
        yield s
    finally:
        s.stop()


def test_hello_replay_then_live_events(served):
    rig = served.rig
    bound_session(rig)
    kept = rig.kept()
    with CC.follow_events(served.path, 0, rig.events.journal_id, read_deadline=5) as stream:
        it = iter(stream)
        hello = next(it)
        assert hello == CC.Hello(rig.events.journal_id, kept[0]["seq"], kept[-1]["seq"], True)
        got = [next(it) for _ in kept]
        assert [e.seq for e in got] == [e["seq"] for e in kept] and [e.body for e in got] == kept
        assert all(isinstance(e, CC.Event) for e in got)
        cas(rig, R.ALLOCATED, R.RENDERING)
        live = next(it)
        assert live.kind == "state" and live.body["state"] == R.RENDERING and live.seq == kept[-1]["seq"] + 1
        assert stream.outcome is None
    assert stream.outcome == CC.CANCELLED
    with pytest.raises(StopIteration):
        next(it)


def test_resume_past_a_cursor_and_another_journal(served):
    rig = served.rig
    bound_session(rig)
    kept = rig.kept()
    with CC.follow_events(served.path, kept[-2]["seq"], "j-other", read_deadline=5) as stream:
        it = iter(stream)
        assert next(it).same_journal is False
        assert next(it).seq == kept[-1]["seq"]
    with CC.follow_events(served.path, 0, read_deadline=5) as stream:
        assert next(iter(stream)).same_journal is None


def test_a_cursor_past_the_newest_seq_is_reset_and_ends(served):
    newest = served.rig.kept()[-1]["seq"]
    stream = CC.follow_events(served.path, newest + 10, read_deadline=5)
    assert list(stream)[1:] == [CC.Reset(newest)]
    assert stream.outcome == CC.RESET


def test_a_pruned_cursor_gets_the_gap_first(served):
    rig = served.rig
    first = rig.kept("pool")
    rig.clock.t += EV.POOL_KEEP_S + 10
    sync_free(rig.control, egress=5)
    assert rig.events.prune() == len(first)
    oldest = rig.kept()[0]["seq"]
    assert oldest > 1
    with CC.follow_events(served.path, 0, read_deadline=5) as stream:
        it = iter(stream)
        next(it)
        assert next(it) == CC.Gap(oldest)
        assert next(it).seq == oldest


def test_an_idle_stream_ends_at_the_read_deadline(served):
    newest = served.rig.kept()[-1]["seq"]
    stream = CC.follow_events(served.path, newest, read_deadline=0.3)
    assert len(list(stream)) == 1 and stream.outcome == CC.DEADLINE


def test_control_stop_ends_the_stream_closed(served):
    newest = served.rig.kept()[-1]["seq"]
    stream = CC.follow_events(served.path, newest, read_deadline=10)
    it = iter(stream)
    next(it)
    served.stop()
    assert list(it) == [] and stream.outcome == CC.CLOSED


def test_refusals_by_name(served):
    served.uid = OP_UID
    with pytest.raises(Refused) as e:
        CC.follow_events(served.path, 0)
    assert e.value.name == "verb-not-allowed-for-peer"
    served.uid = DISPATCH_UID
    with pytest.raises(Refused) as e:
        CC.follow_events(served.path, -1)
    assert e.value.name == "out-of-range"
    with pytest.raises(Refused) as e:
        CC.follow_events(served.path.parent / "absent.sock", 0)
    assert e.value.name == "control-down"


@pytest.mark.parametrize("obj", [{"kind": "gap"}, {"kind": "state"}, {"seq": 3}, {"kind": "hello", "seq": 1},
                                 {"kind": "error", "error": 7}, {"kind": "reset", "newest_seq": -1}])
def test_a_frame_off_the_wire_shape_is_none(obj):
    assert CC._stream_frame(obj) is None


@pytest.mark.parametrize("frame", [{"kind": "bogus", "seq": 1, "at": "2026-10-25T00:00:00Z"},
                                   {"kind": "pool", "seq": 0, "at": "2026-10-25T00:00:00Z"},
                                   {"kind": "state", "seq": 3}])
def test_a_stored_event_off_the_session_event_schema_is_no_frame(frame):
    """An event frame is checked against levain-session-event/1 (events.check_event, one home): an unknown kind, a
    seq below 1 or a missing field is off the wire's shape, so the stream ends BROKEN with the cursor unmoved."""
    assert CC._stream_frame(frame) is None


def test_close_wakes_a_reader_blocked_in_recv():
    """close() from another thread shuts the socket down before closing it, so a reader blocked in recv wakes."""
    import socket
    import threading

    a, b = socket.socketpair()
    try:
        s = CC.EventStream(a, CC.Hello("j", 0, 0, None))
        next(s)                                                        # the hello
        done = threading.Event()

        def read():
            list(s)
            done.set()

        t = threading.Thread(target=read, daemon=True)
        t.start()
        t.join(0.2)
        s.close()
        assert done.wait(2.0)
        assert s.outcome in (CC.CANCELLED, CC.BROKEN)
    finally:
        b.close()
