"""CH-D1b slice 1, L3 round 1: the send path's edges (F1-F11 of the round's findings), through the real store.

The concurrency tests force their race: the driver stub blocks inside ``send`` on an :class:`threading.Event` the test
holds, so a second operation provably runs while the first is inside the driver; every wait is bounded."""
from __future__ import annotations

# ruff: noqa: F811 - the fixtures are the send suite's, imported by name and taken as test parameters

import sqlite3
import threading
from contextlib import contextmanager

import pytest

from levain.coordinator import chat
from levain.coordinator.chat import ChatService
from levain.coordinator.driver import ReadBatch, ScriptedSession, SendResult, event
from levain.coordinator.store import CoordError
from tests.test_coordinator_chat_send import (LIVE, ME, check, clock, cs, qdir, ready, send, states, store,  # noqa: F401
                                              world)

WAIT = 5.0          # seconds a test waits for a thread it expects to move
STILL = 0.5         # seconds a test waits to see that a thread did NOT move


def batch(*events, through, gap=False, cursor=None):
    return ReadBatch(tuple(events), cursor or through, through, gap)


def anchor_of(cs, launch):
    with cs._write() as (conn, _now):
        return chat._anchor(conn, launch)


def coord_send(svc, cs, world, body, outbox):
    return send(svc, cs, world, body, outbox=outbox, author_kind="coordinator")


class Blocking(ScriptedSession):
    """A session whose ``send`` signals it is inside the driver, then waits for the test to let it go."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.entered = [threading.Event(), threading.Event()]
        self.release = threading.Event()
        self.inside = 0
        self.most_inside = 0
        self._count = threading.Lock()
        self._calls = 0

    def send(self, command):
        with self._count:
            n = self._calls
            self._calls += 1
            self.inside += 1
            self.most_inside = max(self.most_inside, self.inside)
        self.entered[min(n, 1)].set()
        try:
            assert self.release.wait(WAIT), "the test never released the driver"
            return super().send(command)
        finally:
            with self._count:
                self.inside -= 1


# --- F1: absence reads only inside the complete frontier, never across a gap ---------------------------------------
def test_absence_never_reads_past_the_complete_frontier(cs, world):
    s = ScriptedSession(sends=("lose",))
    svc = ready(cs, world, s)
    r = send(svc, cs, world)                                               # anchored at 00000000
    far = event("user_item", "far", "00000200", body="something else")
    svc.ingest(world["launch"], batch(far, through="00000001", gap=True))
    assert states(cs, r.message_id)[-1] == "ambiguous"                     # 200 lies past what was read completely


def test_absence_is_never_inferred_across_an_unresolved_gap(cs, world):
    s = ScriptedSession(sends=("lose",))
    svc = ready(cs, world, s)
    r = send(svc, cs, world)
    svc.ingest(world["launch"], batch(through="00000000", gap=True))       # events after the anchor were lost
    other = event("user_item", "other", "00000005", body="something else")
    svc.ingest(world["launch"], batch(other, through="00000005"))
    assert states(cs, r.message_id)[-1] == "ambiguous"


def test_a_gap_before_the_anchor_does_not_block_absence(cs, world):
    s = ScriptedSession(sends=("lose",))
    check(cs, world["launch"], "live")
    svc = ChatService(cs, **LIVE)
    svc.attach(world["launch"], s)
    svc.ingest(world["launch"], batch(through="00000000"))
    svc.ingest(world["launch"], batch(through="00000002", gap=True))
    svc.ingest(world["launch"], batch(through="00000003"))
    r = send(svc, cs, world)                                               # anchored at 00000003
    other = event("user_item", "other", "00000004", body="something else")
    svc.ingest(world["launch"], batch(other, through="00000004"))
    assert states(cs, r.message_id)[-1] == "not_delivered"


# --- F2: only an unmatched user item inside the send's own window counts ------------------------------------------
def test_a_turn_start_alone_never_proves_absence(cs, world):
    # the turn the send itself caused: a vendor that echoes the line in another form, or not at all
    s = ScriptedSession(sends=("lose",))
    svc = ready(cs, world, s)
    r = send(svc, cs, world)
    s.show("turn_started", turn_id="t-2")
    s.show("agent_text", body="on it", turn_id="t-2")
    svc.ingest(world["launch"], s.read("00000000"))
    assert states(cs, r.message_id)[-1] == "ambiguous"


def test_absence_counts_only_items_inside_the_sends_window(cs, world):
    s = ScriptedSession(sends=("lose",))
    svc = ready(cs, world, s)
    r1 = send(svc, cs, world, "first")                                     # anchored at 00000000
    s.show("agent_text", body="x")
    svc.ingest(world["launch"], s.read("00000000"))
    r2 = send(svc, cs, world, "second", outbox="ob-00000002")              # anchored at 00000001
    s.show("user_item", body="typed in the app")                            # 00000002: r2's window, not r1's
    svc.ingest(world["launch"], s.read("00000001"))
    assert states(cs, r1.message_id)[-1] == "ambiguous"
    assert states(cs, r2.message_id)[-1] == "not_delivered"


def test_an_item_at_the_next_anchor_is_inside_the_window(cs, world):
    s = ScriptedSession(sends=("lose",))
    svc = ready(cs, world, s)
    r1 = send(svc, cs, world, "first")                                     # anchored at 00000000
    s.show("user_item", body="typed in the app")                            # 00000001
    svc.ingest(world["launch"], s.read("00000000"))
    assert states(cs, r1.message_id)[-1] == "not_delivered"


# --- F11 (and F2's "not resolved to another message"): equal anchors ----------------------------------------------
def test_equal_anchors_show_each_send_its_own_echo_and_neither_resolves_the_other(cs, world):
    s = ScriptedSession(sends=("lose",))
    svc = ready(cs, world, s)
    r1 = send(svc, cs, world, "alpha")
    r2 = send(svc, cs, world, "beta", outbox="ob-00000002")               # no read between: the same anchor
    s.show("user_item", body="alpha")                                       # r1 consumed; r2 may still be queued
    svc.ingest(world["launch"], s.read("00000000"))
    assert states(cs, r1.message_id)[-1] == "ambiguous"
    assert states(cs, r2.message_id)[-1] == "ambiguous"                    # r1's echo says nothing about r2
    assert svc.candidates(r1.message_id) == ["user_item:v00000001"]
    assert svc.candidates(r2.message_id) == []


# --- F3: one launch's sends never overlap inside the driver -------------------------------------------------------
def test_two_sends_to_one_launch_never_overlap_in_the_driver(cs, world):
    s = Blocking(sends=("accept",))
    svc = ready(cs, world, s)
    got = {}
    a = threading.Thread(target=lambda: got.setdefault("a", coord_send(svc, cs, world, "one", "ob-0000000a")))
    b = threading.Thread(target=lambda: got.setdefault("b", coord_send(svc, cs, world, "two", "ob-0000000b")))
    a.start()
    assert s.entered[0].wait(WAIT)
    b.start()
    assert not s.entered[1].wait(STILL)                                    # b waits outside the driver
    s.release.set()
    a.join(WAIT)
    b.join(WAIT)
    assert not a.is_alive() and not b.is_alive()
    assert s.most_inside == 1
    assert got["a"].state == got["b"].state == "accepted"
    assert [w["text"] for w in s.written] == ["one", "two"]


# --- F4: no driver code inside a write transaction ----------------------------------------------------------------
class Watched(ScriptedSession):
    """Records every driver method called while the store's write transaction is open."""

    def __init__(self, inside, **kw):
        super().__init__(**kw)
        self._inside = inside
        self.violations = []

    def _seen(self, name):
        if self._inside():
            self.violations.append(name)

    def capabilities(self):
        self._seen("capabilities")
        return super().capabilities()

    def normalize(self, text):
        self._seen("normalize")
        return super().normalize(text)

    def pos_lt(self, a, b):
        self._seen("pos_lt")
        return super().pos_lt(a, b)

    def send(self, command):
        self._seen("send")
        return super().send(command)


def test_no_driver_code_runs_inside_a_write_transaction(cs, world):
    depth = threading.local()
    real = cs._write

    @contextmanager
    def watched_write():
        depth.n = getattr(depth, "n", 0) + 1
        try:
            with real() as got:
                yield got
        finally:
            depth.n -= 1

    cs._write = watched_write
    s = Watched(lambda: getattr(depth, "n", 0) > 0, sends=("lose", "ambiguous"))
    svc = ready(cs, world, s)
    r1 = send(svc, cs, world, "one")
    s.show("agent_text", body="x")
    svc.ingest(world["launch"], s.read("00000000"))
    r2 = send(svc, cs, world, "two", outbox="ob-00000002")
    svc.ingest(world["launch"], s.read("00000001"))
    svc.candidates(r2.message_id)
    assert s.violations == []
    assert states(cs, r1.message_id)[-1] == "ambiguous" and states(cs, r2.message_id)[-1] == "ambiguous"


def test_a_normalizer_that_writes_the_store_does_not_deadlock(cs, world):
    # codex's re-entrant case: driver code that takes the store's write lock itself
    def normalize(text):
        with cs._write() as (_conn, _now):
            pass
        return text

    s = ScriptedSession(sends=("accept",), normalize=normalize)
    svc = ready(cs, world, s)
    assert send(svc, cs, world).state == "accepted"


# --- F5: the driver boundary is exception-safe --------------------------------------------------------------------
class _Interrupted(ScriptedSession):
    def send(self, command):
        self._write(command)
        raise KeyboardInterrupt


class _HostileResult(SendResult):
    @property
    def detail(self):
        raise RuntimeError("hostile")


class _Hostile(ScriptedSession):
    def send(self, command):
        self._write(command)
        return _HostileResult("accepted", "t-1", "i-1", "x")


class _Unprintable(Exception):
    def __str__(self):
        raise RuntimeError("no text")


class _RaisesUnprintable(ScriptedSession):
    def send(self, command):
        self._write(command)
        raise _Unprintable


def test_a_shutdown_signal_from_the_driver_is_recorded_ambiguous_then_propagates(cs, world):
    s = _Interrupted()
    svc = ready(cs, world, s)
    with pytest.raises(KeyboardInterrupt):
        send(svc, cs, world)
    with cs._write() as (conn, _now):
        m = conn.execute("SELECT message_id FROM chat_messages").fetchone()[0]
    assert states(cs, m)[-1] == "ambiguous" and len(s.written) == 1


@pytest.mark.parametrize("cls", [_Hostile, _RaisesUnprintable])
def test_a_hostile_driver_result_is_ambiguous(cs, world, cls):
    s = cls()
    svc = ready(cs, world, s)
    r = send(svc, cs, world)
    assert r.state == "ambiguous" and states(cs, r.message_id)[-1] == "ambiguous"


# --- F6: one session per launch; detach and close close it --------------------------------------------------------
def test_attach_refuses_a_second_session_while_one_is_attached(cs, world):
    s1 = ScriptedSession()
    svc = ready(cs, world, s1)
    with pytest.raises(CoordError, match="already"):
        svc.attach(world["launch"], ScriptedSession())
    svc.detach(world["launch"])
    assert s1.closed
    s2 = ScriptedSession()
    svc.attach(world["launch"], s2)
    svc.close()
    assert s2.closed


def test_detach_waits_for_an_in_flight_send(cs, world):
    s = Blocking(sends=("accept",))
    svc = ready(cs, world, s)
    got = {}
    a = threading.Thread(target=lambda: got.setdefault("a", coord_send(svc, cs, world, "one", "ob-0000000a")))
    a.start()
    assert s.entered[0].wait(WAIT)
    with pytest.raises(CoordError, match="already"):
        svc.attach(world["launch"], ScriptedSession())                     # no silent replacement mid-send
    d = threading.Thread(target=svc.detach, args=(world["launch"],))
    d.start()
    d.join(STILL)
    assert d.is_alive() and not s.closed                                   # the send still owns the session
    s.release.set()
    a.join(WAIT)
    d.join(WAIT)
    assert got["a"].state == "accepted" and s.closed


# --- F7: a declared correlation field waits for its proof; a late proof is never dropped -------------------------
def test_with_a_correlation_field_absence_is_never_inferred(cs, world):
    s = ScriptedSession(sends=("lose",), correlation_field="uuid")
    svc = ready(cs, world, s)
    r = send(svc, cs, world)
    s.show("user_item", body="typed in the app")
    svc.ingest(world["launch"], s.read("00000000"))
    assert states(cs, r.message_id)[-1] == "ambiguous"
    s.show("user_item", body="go", correlation=s.written[0]["op_id"])     # the proof, a batch later
    svc.ingest(world["launch"], s.read("00000001"))
    assert states(cs, r.message_id)[-1] == "accepted"


def test_a_proof_after_a_terminal_state_is_recorded_as_an_anomaly(cs, world):
    s = ScriptedSession(sends=("lose",), correlation_field="uuid")
    svc = ready(cs, world, s)
    r = send(svc, cs, world)
    svc.mark(r.message_id, False, ME)
    e = s.show("user_item", body="go", correlation=s.written[0]["op_id"])
    svc.ingest(world["launch"], s.read("00000000"))
    assert states(cs, r.message_id)[-1] == "not_delivered"                 # the record stays as it was written
    got = svc.anomalies(world["launch"])
    assert [(a.message_id, a.vendor_event_key, a.kind) for a in got] == [
        (r.message_id, e.key, "proof_after_terminal")]


def test_a_proof_on_a_second_item_of_an_accepted_send_is_an_anomaly(cs, world):
    s = ScriptedSession(sends=("accept",), correlation_field="uuid")
    svc = ready(cs, world, s)
    r = send(svc, cs, world)
    svc.ingest(world["launch"], s.read("00000000"))                        # its own echo: the item it named
    assert svc.anomalies(world["launch"]) == []
    s.show("user_item", body="go", correlation=s.written[0]["op_id"])     # the vendor shows our op twice
    svc.ingest(world["launch"], s.read("00000001"))
    assert states(cs, r.message_id)[-1] == "accepted"
    assert [a.kind for a in svc.anomalies(world["launch"])] == ["proof_after_terminal"]


# --- F8 / F9: no session, no thread ----------------------------------------------------------------------------------
def test_ingest_without_a_session_is_refused_and_candidates_are_empty(cs, world, store):
    s = ScriptedSession(sends=("ambiguous",))
    svc = ready(cs, world, s)
    r = send(svc, cs, world)
    fresh = ChatService(cs, **LIVE)                                                # a restarted service, nothing attached
    with pytest.raises(CoordError, match="no driver session"):
        fresh.ingest(world["launch"], s.read("00000000"))
    assert fresh.candidates(r.message_id) == []


def test_a_worker_launch_with_no_thread_is_refused(cs, world, qdir):
    # the store refuses such a launch ("a worker launch holds at least one thread"); a row written past it is refused
    # here too, with a CoordError and nothing written, never a TypeError
    bare = "ln-" + "e" * 32
    c = sqlite3.connect(qdir / "dispatch.db", isolation_level=None)
    c.execute("INSERT INTO gseq (at) VALUES ('x')")
    c.execute("INSERT INTO launches (launch_id, gseq, session_key, harness, role, threads, model, cwd, token, "
              "placement, at) SELECT ?, (SELECT MAX(gseq) FROM gseq), session_key || '-bare', harness, role, '[]', "
              "model, cwd, token, placement, at FROM launches WHERE launch_id = ?", (bare, world["launch"]))
    c.close()
    svc = ChatService(cs, **LIVE)
    with pytest.raises(CoordError, match="no thread"):
        svc.send(bare, author_surface="coordinator", client_id="coord-1", outbox_id="ob-00000009",
                 body="go", author_kind="coordinator")
    c = sqlite3.connect(qdir / "dispatch.db")
    assert c.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0] == 0
    c.close()


# --- F10: the frontier never moves back -----------------------------------------------------------------------------
def test_an_older_batch_never_moves_the_frontier_back(cs, world):
    s = ScriptedSession()
    svc = ready(cs, world, s)
    s.show("agent_text", body="x")
    older = s.read("00000000")
    s.show("agent_text", body="y")
    newer = s.read("00000000")
    svc.ingest(world["launch"], newer)
    svc.ingest(world["launch"], older)
    assert anchor_of(cs, world["launch"]) == "00000002"


def test_fingerprint_change_under_ingest_writes_nothing(cs, world, qdir):
    # a second writer of this launch's rows between the read and the write (an unsupported shape) is refused whole
    s = ScriptedSession()
    svc = ready(cs, world, s)
    s.show("agent_text", body="x")
    b = s.read("00000000")
    real = cs._read

    def racing_read(fn):
        got = real(fn)
        with cs._write() as (conn, now):
            chat.record_cursor(conn, now, launch_id=world["launch"], vendor_cursor="00000000",
                               complete_through="00000000", gap=False)
        return got

    cs._read = racing_read
    with pytest.raises(CoordError, match="changed"):
        svc.ingest(world["launch"], b)
    cs._read = real
    c = sqlite3.connect(qdir / "dispatch.db")
    assert c.execute("SELECT COUNT(*) FROM chat_events").fetchone()[0] == 0
    c.close()


# --- L3 round 2: bounds and reorders on the round-1 fix -------------------------------------------------------------
class _Touchy:
    """A capabilities object that is not a plain tuple: reading a field is driver code, and it says where it ran."""
    correlation_field = None
    interrupt_by_turn = idempotency_key = catch_up = replays_pending_requests = False
    vendor_version = "touchy"

    def __init__(self, inside, seen):
        self._inside, self._seen = inside, seen

    @property
    def steer(self):
        if self._inside():
            self._seen.append("steer")
        return True

    @property
    def queue(self):
        if self._inside():
            self._seen.append("queue")
        return True


def _watch_writes(cs):
    depth = threading.local()
    real = cs._write

    @contextmanager
    def watched_write():
        depth.n = getattr(depth, "n", 0) + 1
        try:
            with real() as got:
                yield got
        finally:
            depth.n -= 1

    cs._write = watched_write
    return lambda: getattr(depth, "n", 0) > 0


def test_a_capabilities_getter_never_runs_inside_a_write_transaction(cs, world):
    inside = _watch_writes(cs)
    seen = []

    class S(ScriptedSession):
        def capabilities(self):
            return _Touchy(inside, seen)

    svc = ready(cs, world, S(sends=("accept",)))
    assert send(svc, cs, world).state == "accepted"
    assert seen == []


class _Sneaky(str):
    """A normalized form whose own methods are driver code; ``encode`` is what the store hashes it with."""
    where = None

    def encode(self, *a, **kw):
        if _Sneaky.where is not None and _Sneaky.where():
            _Sneaky.seen.append("encode")
        return str.encode(self, *a, **kw)

    seen: list = []


def test_a_normalized_form_is_a_plain_string_before_any_transaction(cs, world):
    _Sneaky.where, _Sneaky.seen = _watch_writes(cs), []
    try:
        svc = ready(cs, world, ScriptedSession(sends=("accept",), normalize=lambda t: _Sneaky(t)))
        assert send(svc, cs, world).state == "accepted"
        svc.ingest(world["launch"], batch(event("user_item", "u1", "00000009", body="typed"), through="00000009"))
        assert _Sneaky.seen == []
    finally:
        _Sneaky.where = None


def test_a_resend_of_a_stored_outbox_id_calls_no_driver_method(cs, world):
    class Gone(ScriptedSession):
        broken = False

        def capabilities(self):
            if self.broken:
                raise RuntimeError("the session went away")
            return super().capabilities()

        def normalize(self, text):
            if self.broken:
                raise RuntimeError("the session went away")
            return super().normalize(text)

    s = Gone(sends=("accept",))
    svc = ready(cs, world, s)
    first = send(svc, cs, world)
    s.broken = True
    again = send(svc, cs, world)                                            # the same outbox_id, the same words
    assert again.state == "accepted" and again.message_id == first.message_id and again.created is False


def test_candidates_waits_for_a_send_in_flight_on_its_launch(cs, world):
    s = Blocking(sends=("ambiguous", "accept"))
    svc = ready(cs, world, s)
    s.release.set()
    r1 = coord_send(svc, cs, world, "one", "ob-0000000a")
    s.release.clear()
    a = threading.Thread(target=lambda: coord_send(svc, cs, world, "two", "ob-0000000b"))
    a.start()
    assert s.entered[1].wait(WAIT)
    got = {}
    c = threading.Thread(target=lambda: got.setdefault("c", svc.candidates(r1.message_id)))
    c.start()
    c.join(STILL)
    assert c.is_alive()                                                     # pos_lt would run beside a send, a detach
    s.release.set()
    a.join(WAIT)
    c.join(WAIT)
    assert got["c"] == []


def test_an_older_batch_with_a_gap_is_not_a_gap_at_the_frontier(cs, world):
    s = ScriptedSession(sends=("lose",))
    svc = ready(cs, world, s)
    s.show("agent_text", body="x")
    older = s.read("00000000")
    s.show("agent_text", body="y")
    svc.ingest(world["launch"], s.read("00000000"))                         # the frontier: 00000002
    svc.ingest(world["launch"], ReadBatch(older.events, older.next_cursor, older.complete_through, True))
    assert anchor_of(cs, world["launch"]) == "00000002"
    r = send(svc, cs, world)                                                # anchored at the frontier
    other = event("user_item", "other", "00000003", body="something else")
    svc.ingest(world["launch"], batch(other, through="00000003"))
    assert states(cs, r.message_id)[-1] == "not_delivered"                  # the stale gap was subsumed by the frontier


def test_no_lock_is_allocated_for_a_launch_that_does_not_exist(cs, world):
    svc = ready(cs, world, ScriptedSession())
    held = set(svc._locks)
    ghost = "ln-" + "f" * 32
    with pytest.raises(CoordError):
        svc.send(ghost, author_surface="coordinator", client_id="coord-1", outbox_id="ob-00000009", body="hi",
                 author_kind="coordinator")
    with pytest.raises(CoordError):
        svc.ingest(ghost, ReadBatch((), None, None, False))
    svc.detach(ghost)
    assert set(svc._locks) == held
