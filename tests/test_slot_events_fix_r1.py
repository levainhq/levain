"""S1 3c-8 slice 2, the L3 r1 fix round (1025-23/3c8s2/RULINGS_R1.md): CONTROL's event journal and stream.

Each test was RED on af2256d6 (the slice-2 tip) and names the ruling item it holds. The rig is test_slot_control_events'
(CONTROL through `Control.handle`, the event journal on disk beside the records)."""
from __future__ import annotations

import socket
from pathlib import Path

import pytest

from levain.slot import control as C
from levain.slot import events as EV
from levain.slot import records as R
from levain.slot import routemaps as RM
from tests.test_routemaps import st  # noqa: F401 - the signed store fixture
from tests.test_slot_control_events import DIG, ERig, cas, erig, to_retained  # noqa: F401 - erig is a fixture
from tests.test_tickets import (DISPATCH_UID, KEYS_UID, OP_UID, SESSION, Clock, bound_session, refused, root,
                                sync_free)


# ---- item 1: a prune failure after a landed append -----------------------------------------------------------------

def test_a_prune_failure_after_the_append_never_fails_the_verb(erig):
    """The append that reaches PRUNE_EVERY is fsynced and kept before the prune runs: a prune that raises `OSError`
    (a full disk writing `events.jsonl.new`) is recorded, never raised, so ATTACH answers ok with its generation-0
    event kept once, and the next write appends no event twice."""
    def boom() -> int:
        raise OSError(28, "No space left on device")

    erig.events._prune_locked = boom
    erig.events._since_prune = EV.PRUNE_EVERY - 1
    _ticket, _run, att = bound_session(erig)
    assert att["ok"], att
    assert [(e["generation"], e["state"]) for e in erig.session_events()] == [(0, R.ALLOCATED)]
    assert erig.control.owed_events() == {}
    assert [s for s in erig.control.seam_errors if s[0] == "event-prune"], erig.control.seam_errors
    cas(erig, R.ALLOCATED, R.RENDERING)
    assert [(e["generation"], e["state"]) for e in erig.session_events()] == [(0, R.ALLOCATED), (1, R.RENDERING)]


# ---- the stream over a real ControlServer socket ---------------------------------------------------------------------

class Served:
    """``erig``'s CONTROL behind a real `ControlServer` on a short socket path (a unix socket path must stay short)."""

    def __init__(self, rig, **server_kw) -> None:
        import tempfile

        self.rig = rig
        self.d = Path(tempfile.mkdtemp(dir="/tmp"))
        rig.control.stop()                                   # the server's start runs the start pass again
        rig.control = C.Control(C.StateStore(rig.sdir), OP_UID, dispatch_uid=DISPATCH_UID, keys_uid=KEYS_UID,
                                routes=RM.ControlRoutes(rig.st.routes), tickets=rig.tickets, clock=rig.clock,
                                events=rig.events)
        self.server = C.ControlServer(rig.control, self.d / "c.sock", peer_uid=lambda s: DISPATCH_UID, **server_kw)
        self.server.start()
        sync_free(rig.control)
        rig.attach(rig.cr.head())

    def open(self, after_seq: int = 0) -> tuple:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(str(self.d / "c.sock"))
        c.sendall(C.encode_frame({"v": 1, "op": "SESSION-EVENTS", "id": 3, "after_seq": after_seq}))
        return c, _frame(c)

    def conns(self) -> int:
        with self.server._mu:
            return len(self.server._conns)

    def stop(self) -> None:
        self.server.stop()


def _frame(sock: socket.socket, timeout: float = 5.0) -> dict:
    sock.settimeout(timeout)
    obj = C.read_frame(sock)
    assert obj is not None
    return obj


@pytest.fixture
def served(erig):
    s = Served(erig)
    yield s
    s.stop()


# ---- item 3: poison is terminal for streams --------------------------------------------------------------------------

def test_a_poisoned_journal_refuses_new_streams(erig):
    bound_session(erig)
    erig.events.fault = lambda p: (_ for _ in ()).throw(OSError(5, "EIO"))
    cas(erig, R.ALLOCATED, R.RENDERING)
    assert erig.events.poisoned
    assert refused(erig.ask("SESSION-EVENTS", after_seq=0)) == "events-unavailable"


def test_a_live_stream_ends_with_an_error_frame_when_the_journal_is_poisoned(served):
    """A stream open when an append poisons the journal gets what was kept past its cursor, then
    `{kind:"error", error:"events-unavailable"}`, then the close: never a stream that waits forever."""
    rig = served.rig
    bound_session(rig)
    c, hello = served.open(0)
    assert hello["ok"]
    kept = rig.kept()
    assert [_frame(c) for _ in kept] == kept
    rig.events.fault = lambda p: (_ for _ in ()).throw(OSError(5, "EIO"))
    cas(rig, R.ALLOCATED, R.RENDERING)
    assert _frame(c) == {"kind": "error", "error": "events-unavailable"}
    c.settimeout(5)
    assert c.recv(1) == b""
    c.close()
