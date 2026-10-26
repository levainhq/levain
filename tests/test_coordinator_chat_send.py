"""CH-D1b slice 1: the driver contract, ``ScriptedSession``, the §4.2 latched fold, and the send path (§6) with its
crash matrix and the ambiguous reconciliation (§7.1-§7.4), all through the real store on real sqlite files.

The crash matrix runs twice: in-process (a BaseException at each fault point, so nothing below it runs) and as a
process run, where a child process sends and SIGKILLs itself at the fault point; the parent restarts the service on
the same file and reads the bytes the child's session wrote from the session's fsynced log. Run as a script, this file
is that child (see ``_child``).

Several tests are "broken fakes" (§12): a check function runs against the real service and against a subclass broken
in one named way, and must pass on the first and fail on the second."""
from __future__ import annotations

import ast
import inspect
import json
import os
import signal
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.coordinator import chat, driver
from levain.coordinator.chat import ChatService, ChatStore, Liveness, OutboxRefused, liveness, record_check
from levain.coordinator.driver import (DriverSession, LivenessEvidence, ScriptedSession, SendResult, event)
from levain.coordinator.envelopes import EnvelopeStore
from levain.coordinator.store import CoordError, Part

# CH-D1b-3: a service's verdict comes from its probe; these worlds are Claude launches, whose every send is a boundary
# (R9), so their services carry a probe that reads live and a retry wait that does not sleep
LIVE = {"probe": lambda _launch, _evidence: chat.Verdict(True, "scripted probe: live"), "sleep": lambda _s: None}
T0 = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
ME = "cockpit:tab-a"
PHONE = "phone:iphone-1"
NONCE = "0123456789abcdef0123456789abcdef"


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


def _world(store: EnvelopeStore) -> dict:
    store.coord_set_agent("none")
    token = store.coord_take()
    d = store.accept_drop("fix the build", surface="cli")[0]
    thread = store.split(d.drop_id, [Part("work", "fix the build")])[0]
    ln = store.launch_request(token, harness="claude", role="worker", threads=[thread.thread_id])
    got, why = store.launch_activate(ln.launch_id, harness_session_id="sess-worker-1")
    assert why is None and got.state == "active"
    return {"thread": thread.thread_id, "launch": ln.launch_id}


@pytest.fixture
def store(qdir, clock):
    return EnvelopeStore(qdir, clock=clock)


@pytest.fixture
def world(store):
    return _world(store)


@pytest.fixture
def cs(store):
    s = ChatStore(store)
    s._connect().close()
    return s


def raw(qdir):
    c = sqlite3.connect(qdir / "dispatch.db", isolation_level=None)
    c.execute("PRAGMA foreign_keys = ON")
    return c


def check(cs, launch, kind, source="activation", **kw):
    with cs._write() as (conn, now):
        return record_check(conn, now, launch, kind, source, kw.pop("detail", "test"), **kw)


def ready(cs, world, session, *, svc_cls=ChatService, **kw):
    """A launch the service may send to: a live check, the vendor's origin read, an attached session."""
    check(cs, world["launch"], "live")
    svc = svc_cls(cs, **{**LIVE, **kw})
    svc.attach(world["launch"], session)
    svc.ingest(world["launch"], session.read(None))
    return svc


def send(svc, cs, world, body="go", *, outbox="ob-00000001", epoch=None, owner=ME, **kw):
    if epoch is None and kw.get("author_kind", "operator") == "operator":
        epoch = cs.turn_view(world["launch"], owner).epoch or cs.turn_claim(world["launch"], owner)
    surface, client = owner.split(":", 1)
    kw.setdefault("author_kind", "operator")
    if kw["author_kind"] == "coordinator":
        surface, client = "coordinator", "coord-1"
    return svc.send(world["launch"], author_surface=surface, client_id=client, outbox_id=outbox, body=body,
                    expected_epoch=epoch, **kw)


def states(cs, message_id):
    with cs._write() as (conn, _now):
        return [r[0] for r in conn.execute("SELECT state FROM chat_outbox_events WHERE message_id = ? ORDER BY seq",
                                           (message_id,))]


# --- the contract ---------------------------------------------------------------------------------------------------
def test_session_returns_no_verdict():
    # §10 / B2 (static): liveness evidence carries raw data and no boolean; the fold lives in chat.py
    assert LivenessEvidence._fields == ("source", "raw", "read_at")
    hints = inspect.get_annotations(LivenessEvidence)
    assert all("bool" not in str(h) for h in hints.values())
    assert isinstance(ScriptedSession(), DriverSession)
    assert "liveness" not in {n for n in dir(driver) if callable(getattr(driver, n)) and not n[0].isupper()}
    assert inspect.signature(chat.liveness).return_annotation in ("Liveness", Liveness)


def test_driver_imports_nothing_with_authority():
    # B4: the session module imports nothing from consent, the store, the lease or sqlite
    tree = ast.parse(Path(driver.__file__).read_text())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add(node.module or "")
    assert not {m for m in mods if m.startswith(("levain", "sqlite3"))}, mods


def test_event_key_is_kind_qualified_and_vendor_id_strips_it():
    e = event("user_item", "u-1", "00000001", body="go")
    assert e.key == "user_item:u-1" and e.vendor_id == "u-1" and e.correlation is None


# --- §2.7 / §4.2: the latched fold --------------------------------------------------------------------------------
def test_nothing_enabled_before_the_first_check(cs, world):
    with cs._write() as (conn, _now):
        assert liveness(conn, world["launch"]) == Liveness(False, None, False, "no check has run")


def test_live_after_not_live_stays_off(cs, world, qdir):
    launch = world["launch"]
    check(cs, launch, "live")
    check(cs, launch, "not_live", "turn_start")
    check(cs, launch, "live", "turn_start")
    with cs._write() as (conn, _now):
        fold = liveness(conn, launch)
    assert not fold.enabled and fold.latched and fold.latest == "live"


def test_reenabled_needs_latest_live(cs, world, qdir):
    launch = world["launch"]
    check(cs, launch, "live")
    with pytest.raises(CoordError, match="re-enable"):
        check(cs, launch, "reenabled", "operator", by="operator")           # nothing latched
    check(cs, launch, "not_live", "tripwire:deficit")
    with pytest.raises(CoordError, match="re-enable"):
        check(cs, launch, "reenabled", "operator", by="operator")           # latest is not_live
    c = raw(qdir)
    n = c.execute("SELECT COUNT(*) FROM chat_launch_checks").fetchone()[0]
    g = c.execute("INSERT INTO gseq (at) VALUES ('2026-10-23T12:00:00Z')").lastrowid
    with pytest.raises(sqlite3.IntegrityError, match="re-enable clears a latch"):
        c.execute("INSERT INTO chat_launch_checks (launch_id, seq, gseq, kind, source, detail, by, at) VALUES "
                  "(?, ?, ?, 'reenabled', 'operator', 'x', 'operator', 'x')", (launch, n + 1, g))
    check(cs, launch, "probing", "turn_start")
    check(cs, launch, "live", "turn_start")
    check(cs, launch, "reenabled", "operator", by="operator")
    with cs._write() as (conn, _now):
        assert liveness(conn, launch).enabled
    with pytest.raises(CoordError, match="only you re-enable"):
        check(cs, launch, "reenabled", "operator")                          # by service
    with pytest.raises(CoordError, match="only you re-enable"):
        check(cs, launch, "live", "operator", by="operator")


def test_probing_is_not_enabled(cs, world):
    check(cs, world["launch"], "live")
    check(cs, world["launch"], "probing", "send")
    with cs._write() as (conn, _now):
        assert liveness(conn, world["launch"]) == Liveness(False, "probing", False, "a check is running")


def test_spawn_nonce_refused_on_non_activation_row(cs, world, qdir):
    launch = world["launch"]
    check(cs, launch, "probing", spawn_nonce=NONCE)                       # an activation row takes one
    for source, nonce in (("turn_start", NONCE), ("activation", NONCE.upper()), ("activation", NONCE[:-1])):
        with pytest.raises(CoordError, match="spawn nonce"):
            check(cs, launch, "live", source, spawn_nonce=nonce)
    c = raw(qdir)
    g = c.execute("INSERT INTO gseq (at) VALUES ('2026-10-23T12:00:00Z')").lastrowid
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        c.execute("INSERT INTO chat_launch_checks (launch_id, seq, gseq, kind, source, spawn_nonce, detail, by, at) "
                  "VALUES (?, 2, ?, 'live', 'turn_start', ?, 'x', 'service', 'x')", (launch, g, NONCE))


def test_tripwire_source_only_on_not_live(cs, world, qdir):
    with pytest.raises(CoordError, match="tripwire"):
        check(cs, world["launch"], "live", "tripwire:deficit")
    check(cs, world["launch"], "not_live", "tripwire:deficit")
    c = raw(qdir)
    g = c.execute("INSERT INTO gseq (at) VALUES ('2026-10-23T12:00:00Z')").lastrowid
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        c.execute("INSERT INTO chat_launch_checks (launch_id, seq, gseq, kind, source, detail, by, at) VALUES "
                  "(?, 2, ?, 'live', 'tripwire:x', 'x', 'service', 'x')", (world["launch"], g))


def test_launch_checks_are_append_only(cs, world, qdir):
    check(cs, world["launch"], "live")
    c = raw(qdir)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE chat_launch_checks SET kind = 'not_live'")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("DELETE FROM chat_launch_checks")


# --- §6: the send ---------------------------------------------------------------------------------------------------
def test_send_accepted_writes_once(cs, world):
    s = ScriptedSession()
    svc = ready(cs, world, s)
    r = send(svc, cs, world)
    assert (r.state, r.created) == ("accepted", True)
    assert states(cs, r.message_id) == ["pending", "attempted", "accepted"]
    assert len(s.written) == 1 and s.written[0]["text"] == "go" and s.written[0]["op_id"].startswith("op-")
    again = send(svc, cs, world)                                           # same outbox_id: nothing sent again
    assert (again.state, again.created, len(s.written)) == ("accepted", False, 1)


def test_attempt_carries_the_persisted_anchor(cs, world):
    s = ScriptedSession()
    svc = ready(cs, world, s)
    s.show("agent_text", body="hello")
    svc.ingest(world["launch"], s.read("00000000"))
    r = send(svc, cs, world)
    with cs._write() as (conn, _now):
        op, anchor = chat._attempt(conn, r.message_id)
    assert anchor == "00000001" and op == s.written[0]["op_id"]


def test_gate_not_live_refuses_with_no_call(cs, world):
    s = ScriptedSession()
    svc = ready(cs, world, s)
    check(cs, world["launch"], "not_live", "turn_start")
    r = send(svc, cs, world)
    assert r.state == "refused" and "gate is not live" in r.cause and s.written == []


def test_send_refused_without_the_fence(cs, world):
    s = ScriptedSession()
    svc = ready(cs, world, s)
    cs.turn_claim(world["launch"], PHONE)
    e = cs.turn_view(world["launch"], PHONE).epoch
    r = send(svc, cs, world, epoch=e)                                      # the right epoch, the wrong owner
    assert r.state == "refused" and "not yours" in r.cause and s.written == []
    r2 = send(svc, cs, world, outbox="ob-00000002", epoch=e, owner=PHONE)
    assert r2.state == "accepted"


def test_send_refused_on_an_ended_launch(cs, world, store):
    s = ScriptedSession()
    svc = ready(cs, world, s)
    e = cs.turn_claim(world["launch"], ME)
    store.launch_end(world["launch"], "ended", "done")
    r = send(svc, cs, world, epoch=e)
    assert r.state == "refused" and "only a running worker" in r.cause and s.written == []


def test_send_refused_with_no_session(cs, world):
    check(cs, world["launch"], "live")
    svc = ChatService(cs, **LIVE)
    r = send(svc, cs, world)
    assert r.state == "refused" and "no driver session" in r.cause


def test_coordinator_queues_unfenced_and_never_steers(cs, world):
    s = ScriptedSession()
    svc = ready(cs, world, s)
    cs.turn_claim(world["launch"], PHONE)                                  # you hold the turn
    r = send(svc, cs, world, author_kind="coordinator")
    assert r.state == "accepted"
    with pytest.raises(CoordError):
        send(svc, cs, world, outbox="ob-00000002", author_kind="coordinator", mode="steer",
             expected_turn_id="t-1")


def test_steer_names_an_open_turn(cs, world):
    s = ScriptedSession(turn_id="t-1")
    svc = ready(cs, world, s)
    r = send(svc, cs, world, mode="steer", expected_turn_id="t-1")
    assert r.state == "refused" and "not running" in r.cause and s.written == []
    s.show("turn_started", "t-1", turn_id="t-1")
    svc.ingest(world["launch"], s.read("00000000"))
    r2 = send(svc, cs, world, outbox="ob-00000002", mode="steer", expected_turn_id="t-1")
    assert r2.state == "accepted"
    s.turn_id = "t-2"                                                      # the vendor moved on: its error decides
    r3 = send(svc, cs, world, outbox="ob-00000003", mode="steer", expected_turn_id="t-1")
    assert r3.state == "not_delivered" and "live turn is t-2" in r3.cause


def test_steer_refused_when_vendor_takes_none(cs, world):
    s = ScriptedSession(steer=False)
    svc = ready(cs, world, s)
    s.show("turn_started", "t-1", turn_id="t-1")
    svc.ingest(world["launch"], s.read("00000000"))
    r = send(svc, cs, world, mode="steer", expected_turn_id="t-1")
    assert r.state == "refused" and "no steer" in r.cause


def test_no_vendor_position_cancels_before_any_call(cs, world):
    s = ScriptedSession()
    check(cs, world["launch"], "live")
    svc = ChatService(cs, **LIVE)
    svc.attach(world["launch"], s)                                         # never read: no cursor persisted
    r = send(svc, cs, world)
    assert (r.state, r.cause) == ("cancelled", "no vendor position yet") and s.written == []
    assert states(cs, r.message_id) == ["pending", "cancelled"]


def test_epoch_taken_between_pending_and_attempt_cancels(cs, world):
    s = ScriptedSession()

    def take(point):
        if point == "pending_committed":
            cs.turn_take(world["launch"], PHONE)

    svc = ready(cs, world, s, fault=take)
    r = send(svc, cs, world)
    assert r.state == "cancelled" and "not this turn's" in r.cause and s.written == []


def test_gate_off_between_pending_and_attempt_cancels(cs, world):
    s = ScriptedSession()
    svc = ready(cs, world, s, fault=lambda p: p == "pending_committed" and check(
        cs, world["launch"], "not_live", "tripwire:mode_changed"))
    r = send(svc, cs, world)
    assert r.state == "cancelled" and "gate is not live" in r.cause and s.written == []


@pytest.mark.parametrize("script, state, written", [
    ("not_sent", "not_delivered", 0), ("raise", "ambiguous", 1), ("ambiguous", "ambiguous", 1),
    ("lose", "ambiguous", 1), ("reject", "not_delivered", 1), ("accept_silent", "accepted", 1)])
def test_driver_outcomes(cs, world, script, state, written):
    s = ScriptedSession(sends=(script,))
    svc = ready(cs, world, s)
    r = send(svc, cs, world)
    assert r.state == state and len(s.written) == written


class _Says(ScriptedSession):
    def __init__(self, result) -> None:
        super().__init__()
        self._result = result

    def send(self, command):
        self._write(command)
        return self._result


@pytest.mark.parametrize("result, why", [
    (SendResult("accepted", None, None, "ok"), "no vendor id"),
    ({"outcome": "accepted"}, "no send result"),
    (SendResult("delivered", "t", "i", "ok"), "no send result"),
    (SendResult("accepted", "t\nforged", "i", "ok"), "unreadable id")])
def test_an_acknowledgement_without_proof_is_ambiguous(cs, world, result, why):
    s = _Says(result)
    svc = ready(cs, world, s)
    r = send(svc, cs, world)
    assert r.state == "ambiguous" and why in r.cause


def test_a_message_whose_epoch_lapses_after_the_attempt_is_delivered(cs, world, clock):
    s = ScriptedSession()
    svc = ready(cs, world, s, fault=lambda p: p == "attempt_committed" and clock.advance(minutes=30))
    r = send(svc, cs, world)
    assert r.state == "accepted"


# --- §6 step 4: the crash matrix, in process ------------------------------------------------------------------------
class Died(BaseException):
    """Stands for the process dying at a fault point: nothing below the point runs."""


@pytest.mark.parametrize("point, after, written", [
    ("pending_committed", "not_delivered", 0), ("attempt_committed", "ambiguous", 0),
    ("call_returned", "ambiguous", 1), ("outcome_committed", "accepted", 1)])
def test_send_crash_matrix_in_process(cs, world, store, point, after, written):
    s = ScriptedSession(sends=("accept_silent",))

    def die(p):
        if p == point:
            raise Died(p)

    svc = ready(cs, world, s, fault=die)
    e = cs.turn_claim(world["launch"], ME)
    with pytest.raises(Died):
        send(svc, cs, world, epoch=e)
    restarted = ChatService(ChatStore(store), **LIVE)
    restarted.recover()
    with cs._write() as (conn, _now):
        m = conn.execute("SELECT message_id FROM chat_messages").fetchone()[0]
    assert states(cs, m)[-1] == after and len(s.written) == written
    assert restarted.recover() == {"ambiguous": 0, "not_delivered": 0}   # the rule runs once per row
    # a resend of the same outbox_id after the restart sends nothing again
    restarted.attach(world["launch"], s)
    assert send(restarted, cs, world, epoch=e).created is False and len(s.written) == written


# --- §6 step 4: the crash matrix, as a process run (kill -9) -------------------------------------------------------
def _child(qdir: str, launch_id: str, epoch: str, point: str, log: str) -> None:
    """Run in a child process: send one message and SIGKILL this process at ``point``."""
    st = EnvelopeStore(Path(qdir))
    cs = ChatStore(st)
    s = ScriptedSession(sends=("accept_silent",), correlation_field="uuid", log_path=Path(log))

    def die(p):
        if p == point:
            os.kill(os.getpid(), signal.SIGKILL)

    svc = ChatService(cs, fault=die, **LIVE)
    svc.attach(launch_id, s)
    svc.ingest(launch_id, s.read(None))
    svc.send(launch_id, author_surface="cockpit", client_id="tab-a", outbox_id="ob-00000001", body="go",
             author_kind="operator", expected_epoch=int(epoch))
    os._exit(3)                                                            # the point was never reached


@pytest.mark.parametrize("point, after, written", [
    ("pending_committed", "not_delivered", 0), ("attempt_committed", "ambiguous", 0),
    ("call_returned", "ambiguous", 1), ("outcome_committed", "accepted", 1)])
def test_send_crash_matrix_kill9(tmp_path, point, after, written):
    q = tmp_path / "consent"
    q.mkdir(mode=0o700)
    st = EnvelopeStore(q)                                                  # the real clock: the child's is real too
    w = _world(st)
    cs = ChatStore(st)
    check(cs, w["launch"], "live")
    e = cs.turn_claim(w["launch"], ME)
    log = tmp_path / "written.jsonl"
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
    p = subprocess.run([sys.executable, __file__, str(q), w["launch"], str(e), point, str(log)], env=env,
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == -signal.SIGKILL, (p.returncode, p.stderr[-2000:])
    lines = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
    assert len(lines) == written
    svc = ChatService(ChatStore(EnvelopeStore(q)), **LIVE)
    svc.recover()
    with cs._write() as (conn, _now):
        m = conn.execute("SELECT message_id FROM chat_messages").fetchone()[0]
    assert states(cs, m)[-1] == after
    if after == "ambiguous" and written:
        # the restarted service reads the vendor: its echo carries our op_id, which proves the send
        s = ScriptedSession(correlation_field="uuid")
        svc.attach(w["launch"], s)
        svc.ingest(w["launch"], s.read(None))
        s.show("user_item", body="go", turn_id="t-1", correlation=lines[0]["op_id"])
        svc.ingest(w["launch"], s.read("00000000"))
        assert states(cs, m)[-1] == "accepted"


# --- §7: reconciliation --------------------------------------------------------------------------------------------
def test_proof_echo_resolves_an_ambiguous_send(cs, world):
    s = ScriptedSession(sends=("ambiguous",), correlation_field="uuid")
    svc = ready(cs, world, s)
    r = send(svc, cs, world)
    assert r.state == "ambiguous"
    svc.ingest(world["launch"], s.read("00000000"))
    assert states(cs, r.message_id)[-1] == "accepted"
    with cs._write() as (conn, _now):
        item = conn.execute("SELECT vendor_item_id, cause FROM chat_outbox_events WHERE message_id = ? ORDER BY seq "
                            "DESC LIMIT 1", (r.message_id,)).fetchone()
    assert item == ("v00000001", "the vendor echoed our op_id")


def test_proof_needs_a_declared_correlation_field(cs, world):
    s = ScriptedSession(sends=("lose",))
    svc = ready(cs, world, s)
    r = send(svc, cs, world)
    s.show("user_item", body="go", correlation=s.written[0]["op_id"])     # the field is not declared: no proof
    svc.ingest(world["launch"], s.read("00000000"))
    assert states(cs, r.message_id)[-1] == "ambiguous"


def test_a_digest_never_resolves_a_send(cs, world):
    # B12: the vendor shows our words with no correlation: "probably this one", still ambiguous until you mark it
    s = ScriptedSession(sends=("ambiguous",))
    svc = ready(cs, world, s)
    r = send(svc, cs, world, body="go  now")
    svc.ingest(world["launch"], s.read("00000000"))
    assert states(cs, r.message_id)[-1] == "ambiguous"
    assert svc.candidates(r.message_id) == ["user_item:v00000001"]
    with pytest.raises(CoordError):
        svc.mark(r.message_id, True, "worker:x")
    assert svc.mark(r.message_id, True, ME) == "accepted"
    with pytest.raises(OutboxRefused):
        svc.mark(r.message_id, False, ME)                                  # resolved: terminal
    assert svc.candidates(r.message_id) == []


def test_complete_read_past_a_later_input_is_not_delivered(cs, world):
    s = ScriptedSession(sends=("lose",))
    svc = ready(cs, world, s)
    r = send(svc, cs, world)
    s.show("agent_text", body="still working")
    svc.ingest(world["launch"], s.read("00000000"))
    assert states(cs, r.message_id)[-1] == "ambiguous"                     # output alone proves nothing
    s.show("user_item", body="something else")
    svc.ingest(world["launch"], s.read("00000001"))
    assert states(cs, r.message_id)[-1] == "not_delivered"


def test_the_window_ends_at_the_next_attempt(cs, world):
    s = ScriptedSession(sends=("lose", "ambiguous"))
    svc = ready(cs, world, s)
    r1 = send(svc, cs, world)
    s.show("agent_text", body="x")
    svc.ingest(world["launch"], s.read("00000000"))
    r2 = send(svc, cs, world, outbox="ob-00000002")
    svc.ingest(world["launch"], s.read("00000001"))
    assert svc.candidates(r2.message_id) == ["user_item:v00000002"]
    assert states(cs, r2.message_id)[-1] == "ambiguous"
    assert svc.candidates(r1.message_id) == []                             # the echo lies past r2's anchor
    # and it carries r1's words too, so it is no evidence r1 was passed over (L3 r1 F2): r1 stays yours to mark
    assert states(cs, r1.message_id)[-1] == "ambiguous"


def test_an_item_resolved_to_another_send_is_no_candidate(cs, world):
    s = ScriptedSession(sends=("lose", "accept"))
    svc = ready(cs, world, s)
    r1 = send(svc, cs, world)
    r2 = send(svc, cs, world, outbox="ob-00000002")                        # accepted: its echo is its own item
    svc.ingest(world["launch"], s.read("00000000"))
    assert states(cs, r2.message_id)[-1] == "accepted"
    assert svc.candidates(r1.message_id) == []


def test_reconciliation_reads_no_gseq():
    # §7.1: no Levain gseq is vendor evidence; the window and the match compare positions through pos_lt only
    for fn in (chat._window, chat._absent, chat._next_anchor, chat._inside, chat._untaken, ChatService._reconcile,
               ChatService._prove, ChatService.candidates):
        assert "gseq" not in inspect.getsource(fn)
    # the one read that names it orders the service's own attempts among themselves, never against a vendor position
    assert inspect.getsource(chat._launch_read).count("gseq") == 1 and "ORDER BY o.gseq" in inspect.getsource(
        chat._launch_read)


# --- the read: events + cursor in one transaction; backfill ---------------------------------------------------------
def test_first_read_is_backfill_and_proves_nothing(cs, world):
    s = ScriptedSession(correlation_field="uuid")
    s.show("user_item", body="old", correlation="op-x")
    check(cs, world["launch"], "live")
    svc = ChatService(cs, **LIVE)
    svc.attach(world["launch"], s)
    assert svc.ingest(world["launch"], s.read(None)) == 1
    with cs._write() as (conn, _now):
        assert conn.execute("SELECT backfill FROM chat_events").fetchall() == [(1,)]
    s.show("agent_text", body="new")
    svc.ingest(world["launch"], s.read("00000001"))
    with cs._write() as (conn, _now):
        assert conn.execute("SELECT backfill FROM chat_events ORDER BY vendor_pos").fetchall() == [(1,), (0,)]


def test_a_batch_and_its_cursor_commit_together(cs, world):
    s = ScriptedSession()
    svc = ready(cs, world, s)
    s.show("agent_text", body="x")
    bad = s.read("00000000")._replace(next_cursor="a\nb")                  # the cursor row is refused
    with pytest.raises(CoordError):
        svc.ingest(world["launch"], bad)
    with cs._write() as (conn, _now):
        assert conn.execute("SELECT COUNT(*) FROM chat_events").fetchone()[0] == 0


# --- broken fakes (§12): each check passes on the service and fails on its broken variant ---------------------------
class _ExceptionNotDelivered(ChatService):
    @staticmethod
    def _call(session, command):
        try:
            session.send(command)
        except Exception:  # noqa: BLE001
            return "not_delivered", "broken", None, None, None
        return "accepted", "ok", "t", "i", None


class _DigestAccepts(ChatService):
    def _reconcile(self, launch_id, session, correlated):
        read = self._chat._read(lambda conn: chat._launch_read(conn, launch_id))
        with self._chat._write() as (conn, now):
            for att in read.attempts:
                if att.state == "ambiguous" and chat._window(read, att, session.pos_lt):
                    chat.outbox_step(conn, now, att.message_id, "accepted", cause="digest")


class _NoFenceAtAttempt(ChatService):
    def _attempt_and_call(self, m, session, caps, owner):
        with self._chat._write() as (conn, now):
            chat.outbox_step(conn, now, m.message_id, "attempted", cause="attempt", op_id="op-broken",
                             vendor_anchor=chat._anchor(conn, m.launch_id))
        session.send(chat._command(m, "op-broken"))
        with self._chat._write() as (conn, now):
            chat.outbox_step(conn, now, m.message_id, "accepted", cause="ok", vendor_item_id="i")
        return chat.SendReceipt(m.message_id, m.outbox_id, "accepted", "ok", True)


def _exception_is_ambiguous(cs, world, cls):
    s = ScriptedSession(sends=("raise",))
    r = send(ready(cs, world, s, svc_cls=cls), cs, world)
    assert r.state == "ambiguous"


def _digest_stays_ambiguous(cs, world, cls):
    s = ScriptedSession(sends=("ambiguous",))
    svc = ready(cs, world, s, svc_cls=cls)
    r = send(svc, cs, world)
    svc.ingest(world["launch"], s.read("00000000"))
    assert states(cs, r.message_id)[-1] == "ambiguous"


def _late_owner_never_sends(cs, world, cls):
    s = ScriptedSession()
    svc = ready(cs, world, s, svc_cls=cls, fault=lambda p: p == "pending_committed" and cs.turn_take(
        world["launch"], PHONE))
    send(svc, cs, world)
    assert s.written == []


@pytest.mark.parametrize("check_fn, broken", [
    (_exception_is_ambiguous, _ExceptionNotDelivered), (_digest_stays_ambiguous, _DigestAccepts),
    (_late_owner_never_sends, _NoFenceAtAttempt)])
def test_broken_fakes_for_sends(tmp_path, check_fn, broken):
    def fresh(name):
        q = tmp_path / name
        q.mkdir(mode=0o700)
        st = EnvelopeStore(q, clock=Clock())
        w = _world(st)
        return ChatStore(st), w

    check_fn(*fresh("real"), ChatService)
    with pytest.raises(AssertionError):
        check_fn(*fresh("broken"), broken)


def test_broken_fake_fold_that_lets_live_re_enable(cs, world):
    def broken(conn, launch_id):
        last = conn.execute("SELECT kind FROM chat_launch_checks WHERE launch_id = ? AND kind != 'reenabled' ORDER "
                            "BY seq DESC LIMIT 1", (launch_id,)).fetchone()[0]
        return Liveness(last == "live", last, False, None)

    def stays_off(fold):
        with cs._write() as (conn, _now):
            assert not fold(conn, world["launch"]).enabled

    check(cs, world["launch"], "live")
    check(cs, world["launch"], "not_live", "turn_start")
    check(cs, world["launch"], "live", "turn_start")
    stays_off(liveness)
    with pytest.raises(AssertionError):
        stays_off(broken)


if __name__ == "__main__":
    _child(*sys.argv[1:6])
