"""S1 3c-8 slice 2, the L3 r1 fix round (1025-23/3c8s2/RULINGS_R1.md): CONTROL's event journal and stream.

Each test was RED on af2256d6 (the slice-2 tip) and names the ruling item it holds. The rig is test_slot_control_events'
(CONTROL through `Control.handle`, the event journal on disk beside the records)."""
from __future__ import annotations

import socket
import time
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import Refused
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


# ---- item 4: the replay is pinned when the stream opens --------------------------------------------------------------

def test_a_prune_after_the_hello_never_drops_an_advertised_event(served):
    """The opening's replay is read under the lock the hello is read under: a prune that runs between the answer and
    the stream's first read (here: forced on the stream's first `after`) cannot drop events the hello advertised."""
    rig = served.rig
    bound_session(rig)
    to_retained(rig)
    cas(rig, R.RETAINED, R.TRASH)
    cas(rig, R.TRASH, R.PURGED)                                         # its events are prunable from here on
    kept = rig.kept()
    real = rig.events.after
    pruned = {"n": None}

    def after(seq, **kw):
        if pruned["n"] is None:
            pruned["n"] = rig.events.prune()
        return real(seq, **kw)

    rig.events.after = after
    c, hello = served.open(0)
    assert (hello["oldest_seq"], hello["newest_seq"]) == (kept[0]["seq"], kept[-1]["seq"])
    got = [_frame(c) for _ in kept]
    assert got == kept
    end = time.monotonic() + 5
    while pruned["n"] is None and time.monotonic() < end:           # the stream's first `after` follows the replay
        time.sleep(0.01)
    assert pruned["n"], "the prune ran and dropped events"
    c.close()


# ---- item 5: the data file lost, the meta kept -------------------------------------------------------------------------

@pytest.mark.parametrize("loss", ["unlinked", "emptied"])
def test_a_lost_journal_under_a_kept_meta_is_a_new_journal(erig, loss):
    """`events.jsonl` absent (or holding no complete line) while `events.meta` survives: the start makes a new
    `journal_id`, so a client holding a cursor into the lost journal sees the id change and resets, instead of
    filtering out the new journal's seq 1..cursor. A journal that is whole keeps its id."""
    bound_session(erig)
    jid = erig.events.journal_id
    erig.restart()
    assert erig.events.journal_id == jid
    path = erig.sdir / EV.JOURNAL
    if loss == "unlinked":
        path.unlink()
    else:
        path.write_bytes(b"")
    erig.control.stop()
    erig.restart()
    assert erig.events.journal_id != jid
    assert (erig.sdir / EV.META).read_bytes() == canon.canonical_bytes({"journal_id": erig.events.journal_id})
    jid2 = erig.events.journal_id
    erig.restart()
    assert erig.events.journal_id == jid2                              # the new journal holds lines now: kept


# ---- item 6: a slow reader -------------------------------------------------------------------------------------------

def _a_large_backlog(rig, n: int = 4000) -> None:
    """``n`` pool events (fsync off: the test needs bytes on the wire, not durability), far past what the socket
    buffers hold, so a peer that stops reading leaves the server's send waiting."""
    rig.events._fsync = lambda _fd: None
    for i in range(n):
        rig.events.append({"kind": "pool", "egress_free": i, "none_free": i})


def _until(cond, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def _slow_reader(served) -> socket.socket:
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    c.connect(str(served.d / "c.sock"))
    c.sendall(C.encode_frame({"v": 1, "op": "SESSION-EVENTS", "id": 3, "after_seq": 0}))
    return c


def _drain_to_eof(c: socket.socket) -> None:
    c.settimeout(5)
    while c.recv(1 << 16):
        pass


def test_a_reader_that_stops_reading_is_dropped(erig):
    """A stream peer that stops reading is dropped once no byte has gone out for `stream_stall_s`: its thread and its
    connection slot are freed (here 1 s; CONTROL's own default is STREAM_STALL_S)."""
    served = Served(erig, stream_stall_s=1.0)
    try:
        _a_large_backlog(erig)
        c = _slow_reader(served)
        assert _until(lambda: served.conns() == 1, 5)
        assert _until(lambda: served.conns() == 0, 10), "the slow reader is still served"
        _drain_to_eof(c)                                               # the server closed it: EOF after the buffer
        c.close()
    finally:
        served.stop()


def test_a_cancel_byte_ends_a_stream_blocked_on_a_full_buffer(erig):
    """The peer's byte (its cancellation) is read between bounded sends: a stream whose replay is waiting on a full
    socket buffer ends promptly when the peer sends it, long before any stall bound."""
    served = Served(erig, stream_stall_s=600.0)
    try:
        _a_large_backlog(erig)
        c = _slow_reader(served)
        assert _until(lambda: served.conns() == 1, 5)
        time.sleep(0.5)                                                # the server is now waiting to send
        c.sendall(b"x")
        assert _until(lambda: served.conns() == 0, 5), "the cancelled stream is still served"
        c.close()
    finally:
        served.stop()


# ---- item 7: an owed event carried over an event-less write ------------------------------------------------------------

def test_an_eof_write_over_an_owed_generation_carries_its_event(erig):
    """The RETAINED generation's append fails (owed); the lease then ends, and its `lease_live:false` write (the revoke
    path, never fenced, `event_kind: none` on its own) lands over it. That write now carries the owed event's kind, so
    the RETAINED event is owed by the new generation and the restart's repair appends it: it is never forgotten."""
    from tests.test_tickets import d_attach_claims, lease, ticket_of

    ticket = ticket_of(erig.mint())
    conn, lo = lease(erig, ticket)
    att = root(erig, "ATTACH", lease_nonce=lo["lease_nonce"], sudo_uid=DISPATCH_UID, session=SESSION,
               claims=d_attach_claims(ticket))
    assert att["ok"], att
    cas(erig, R.ALLOCATED, R.RENDERING)
    assert root(erig, "REVOKE", session=SESSION)["ok"]
    for frm, to in [(R.REVOKED, R.FROZEN), (R.FROZEN, R.DEAD), (R.DEAD, R.CREDENTIAL_PARKED)]:
        cas(erig, frm, to)
    real = erig.events.append
    armed = {"on": True}

    def failing(ev):
        if armed["on"] and ev.get("kind") != "pool":
            raise Refused("events-unavailable", "injected")
        return real(ev)

    erig.events.append = failing
    cas(erig, R.CREDENTIAL_PARKED, R.RETAINED)
    rec = erig.control.store.get(SESSION)
    assert rec.lease_live and erig.control.owed_events() == {SESSION: rec.generation}
    erig.control._revoke_after_eof(SESSION)                             # the lease's EOF: lease_live:false
    after = erig.control.store.get(SESSION)
    assert not after.lease_live and after.generation == rec.generation + 1
    assert erig.control.owed_events() == {SESSION: after.generation}
    armed["on"] = False
    erig.restart()
    last = erig.session_events()[-1]
    assert (last["kind"], last["state"], last["generation"]) == ("state", R.RETAINED, after.generation)
    assert erig.control.owed_events() == {}


# ---- item 9: the LOWs ------------------------------------------------------------------------------------------------

GOOD_STATE = {"seq": 1, "at": "2026-10-25T00:00:00Z", "kind": "state", "session": SESSION, "ticket": "A" * 43,
              "generation": 0, "state": "ALLOCATED"}


@pytest.mark.parametrize("bad", [{"kind": []}, {"kind": {}}, {"state": []}, {"session": []}, {"at": []},
                                 {"ticket": "x"}, {"ticket": "A" * 42 + "B"}, {"ticket": 7}])
def test_every_bad_journal_line_refuses_the_start_events_corrupt(erig, bad):
    """codex 7 + 8: a canonical line whose kind (or any other value) is of the wrong type, or whose ticket is not the
    canonical 43-character handle, refuses the start `events-corrupt`: never a raw exception, never served."""
    assert EV.check_event(GOOD_STATE) is None
    obj = {**GOOD_STATE, **bad}
    assert EV.check_event(obj) is not None
    erig.control.stop()
    path = erig.sdir / EV.JOURNAL
    path.write_bytes(canon.canonical_bytes(obj) + b"\n")
    with pytest.raises(Refused) as e:
        erig.restart()
    assert e.value.name == "events-corrupt"


def test_after_reads_from_the_first_seq_past_the_cursor(erig):
    """complement 4: `after` finds its start by bisection over the kept seqs (holes included) and answers the same
    events a scan would."""
    erig.events._fsync = lambda _fd: None
    for i in range(50):
        erig.events.append({"kind": "pool", "egress_free": i, "none_free": i})
    seqs = [e["seq"] for e in erig.kept()]
    for cur in [0, seqs[0], seqs[10] - 1, seqs[10], seqs[-2], seqs[-1], seqs[-1] + 3]:
        assert [s for s, _d in erig.events.after(cur)] == [s for s in seqs if s > cur]
    import inspect
    assert "bisect" in inspect.getsource(EV.EventJournal._first_after)


def test_a_cursor_ahead_of_the_journal_gets_a_reset_frame(served):
    """complement 5: `after_seq` above `newest_seq` on this journal (a cursor from a journal that was lost and made
    again under the same id, or a client's bug) is never a silent skip: the stream sends `{kind:"reset", newest_seq}`
    after the hello and closes."""
    rig = served.rig
    bound_session(rig)
    newest = rig.kept()[-1]["seq"]
    c, hello = served.open(newest + 5)
    assert hello["ok"] and hello["newest_seq"] == newest
    assert _frame(c) == {"kind": "reset", "newest_seq": newest}
    c.settimeout(5)
    assert c.recv(1) == b""
    c.close()
    c, hello = served.open(newest)                                     # at the newest: a live stream, no reset
    cas(rig, R.ALLOCATED, R.RENDERING)
    assert _frame(c)["state"] == R.RENDERING
    c.close()
