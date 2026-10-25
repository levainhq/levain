"""S1 3c-8 slice 2: SESSION-EVENTS (CONTRACT §3.6): CONTROL's event journal (`levain.slot.events`), the event every
ticket-bearing generation owes, the owed-event fence, the start pass's re-emit, pruning, pool events, and the stream.

CONTROL runs through `Control.handle` on test_tickets' rig (a real signed route map, the ticket journal on disk), with
the event journal on disk beside the records; the stream runs through a real `ControlServer` socket, and once through
`control.serve` in a child process with a real socket client (`test_served_control_streams_and_reemits`)."""
from __future__ import annotations

import os
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import control as C
from levain.slot import events as EV
from levain.slot import records as R
from levain.slot import routemaps as RM
from levain.slot import tickets as T
from tests.test_routemaps import st  # noqa: F401 - the signed store fixture
from tests.test_tickets import (DISPATCH_UID, KEYS_UID, OP_UID, SESSION, Clock, Rig, bound_session, d_attach_claims,
                                lease, refused, root, sync_free, ticket_of)

DIG = "sha256:" + "a" * 64
ROOT = Path(__file__).resolve().parents[1]


class ERig(Rig):
    """test_tickets' rig with CONTROL's event journal in the session store; ``efault`` is the journal's crash seam."""

    efault = None

    def restart(self, *, fault=None, attach: bool = True) -> None:
        if self.control is not None:
            self.control.stop()
        kw = {} if fault is None else {"fault": fault}
        self.tickets = T.TicketStore(self.sdir / T.JOURNAL, self.st.L / "policy" / "dispatch_ceiling",
                                     clock=self.clock, ceiling_uid=os.getuid(), **kw)
        self.cr = RM.ControlRoutes(self.st.routes)
        ekw = {} if self.efault is None else {"fault": self.efault}
        self.events = EV.EventJournal(self.sdir, clock=self.clock, **ekw)
        self.control = C.Control(C.StateStore(self.sdir), OP_UID, dispatch_uid=DISPATCH_UID, keys_uid=KEYS_UID,
                                 routes=self.cr, tickets=self.tickets, clock=self.clock, events=self.events)
        self.control.start()
        sync_free(self.control)
        if attach:
            self.attach(self.cr.head())

    def kept(self, kind: str | None = None) -> list:
        return [e for e in self.events.kept() if kind is None or e["kind"] == kind]

    def session_events(self, session: str = SESSION) -> list:
        return [e for e in self.kept() if e.get("session") == session]


@pytest.fixture
def erig(st):  # noqa: F811
    return ERig(st, Clock())


def cas(rig, frm: str, to: str, session: str = SESSION, **fields) -> int:
    gen = rig.control.store.get(session).generation
    ans = root(rig, "CAS", session=session, generation=gen, fields=fields, **{"from": frm, "to": to})
    assert ans["ok"], ans
    return ans["generation"]


#: END's chain from REVOKED, as root's CAS walks it (§7.3)
END_CHAIN = [(R.REVOKED, R.FROZEN), (R.FROZEN, R.DEAD), (R.DEAD, R.CREDENTIAL_PARKED),
             (R.CREDENTIAL_PARKED, R.RETAINED)]


def to_retained(rig, session: str = SESSION) -> None:
    """A bound session ATTACHed (rig) walked ALLOCATED -> RENDERING -> REVOKED -> ... -> RETAINED."""
    cas(rig, R.ALLOCATED, R.RENDERING, session)
    assert root(rig, "REVOKE", session=session)["ok"]
    for frm, to in END_CHAIN:
        cas(rig, frm, to, session)


# ---- the events a session's generations owe ---------------------------------------------------------------------

def test_every_ticket_generation_appends_its_event_before_the_verb_answers(erig):
    """ATTACH's generation 0 and every state move append a `state` event naming the ticket; the 3b CAS an `artifact`
    event; a field-only CAS and MINT's claims none (`event_kind: none`). Each event is on disk when the verb answers."""
    ticket, _run, att = bound_session(erig)
    assert att["ok"], att
    assert [(e["kind"], e["generation"], e.get("state")) for e in erig.session_events()] == [("state", 0, "ALLOCATED")]
    to_retained(erig)
    cas(erig, R.RETAINED, R.RETAINED, held_attempts=1)                 # field-only: owes nothing
    cas(erig, R.RETAINED, R.RETAINED, artifact_digest=DIG)
    evs = erig.session_events()
    assert [(e["kind"], e.get("state")) for e in evs] == [
        ("state", "ALLOCATED"), ("state", "RENDERING"), ("state", "REVOKED"), ("state", "FROZEN"), ("state", "DEAD"),
        ("state", "CREDENTIAL_PARKED"), ("state", "RETAINED"), ("artifact", None)]
    assert all(e["ticket"] == ticket for e in evs)
    assert evs[-1]["artifact_digest"] == DIG and "artifact_failed" not in evs[-1]
    rec = erig.control.store.get(SESSION)
    assert evs[-1]["generation"] == rec.generation and rec.event_kind == "artifact"
    # the journal on disk holds the same lines (each fsynced before its verb answered)
    on_disk = [canon.parse(x) for x in (erig.sdir / EV.JOURNAL).read_bytes().splitlines()]
    assert [e["seq"] for e in on_disk if e["kind"] != "pool"] == [e["seq"] for e in evs]
    assert oct((erig.sdir / EV.JOURNAL).stat().st_mode & 0o777) == "0o600"
    assert oct((erig.sdir / EV.META).stat().st_mode & 0o777) == "0o600"


def test_a_verifier_report_rides_the_state_event(erig):
    """§3.6: the verifier report digest (or refusal) CAS'd before the terminal move rides that `state` event."""
    bound_session(erig)
    cas(erig, R.ALLOCATED, R.RENDERING)
    assert root(erig, "REVOKE", session=SESSION)["ok"]
    for frm, to in END_CHAIN[:-1]:
        cas(erig, frm, to)
    cas(erig, R.CREDENTIAL_PARKED, R.CREDENTIAL_PARKED, verifier_report_digest=DIG)
    cas(erig, R.CREDENTIAL_PARKED, R.RETAINED)
    last = erig.session_events()[-1]
    assert last["state"] == R.RETAINED and last["verifier_report_digest"] == DIG


def test_no_event_for_a_session_without_a_ticket(erig):
    """An operator session (and a REVOKE-created record, which has no claims) bears no ticket: no event."""
    assert root(erig, "REVOKE", session="s-00000000000000b2")["ok"]
    assert [e for e in erig.kept() if e["kind"] != "pool"] == []


def test_pool_event_after_a_sync_that_changed_the_counts(erig):
    """§3.6 `pool`: after root's SLOT-SYNC, with POOL's counts, once per change (a sync that changed nothing owes none)."""
    pools = erig.kept("pool")
    want = erig.ask("POOL")
    assert len(pools) == 1 and (pools[0]["egress_free"], pools[0]["none_free"]) == (want["egress_free"],
                                                                                    want["none_free"])
    first = len(pools)
    sync_free(erig.control)                                            # the same slots: no change
    assert len(erig.kept("pool")) == first
    sync_free(erig.control, egress=3)
    last = erig.kept("pool")[-1]
    want = erig.ask("POOL")
    assert len(erig.kept("pool")) == first + 1
    assert (last["egress_free"], last["none_free"]) == (want["egress_free"], want["none_free"])


# ---- replay, hello, gap (T:3c/test_events_replay_after_seq) -----------------------------------------------------

def _stream(rig, after: int) -> tuple:
    conn = C.Conn(DISPATCH_UID)
    hello = rig.control.handle(conn, {"v": 1, "op": "SESSION-EVENTS", "id": 7, "after_seq": after})
    assert hello["ok"], hello
    gap, replay, cursor = conn.stream
    got = replay + rig.events.after(cursor)
    return hello, gap, [canon.parse(b) for _s, b in got]


def test_events_replay_after_seq(erig):
    """`SESSION-EVENTS {after_seq}` answers the hello (journal_id, oldest/newest seq), then replays every kept event
    with `seq > after_seq`, in seq order; a journal whose PURGED session's events were pruned has seq holes, which the
    replay tolerates, and a cursor older than the oldest kept event (with one past it pruned) gets `gap` first."""
    bound_session(erig)
    to_retained(erig)
    all_ = erig.kept()
    seqs = [e["seq"] for e in all_]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    hello, gap, got = _stream(erig, seqs[2])
    assert hello == {"id": 7, "ok": True, "kind": "hello", "journal_id": erig.events.journal_id,
                     "oldest_seq": seqs[0], "newest_seq": seqs[-1]}
    assert gap is None and [e["seq"] for e in got] == seqs[3:]
    assert got == all_[3:]
    # a cursor at the newest: nothing yet; the next generation arrives on the waiting reader
    _h, _g, none = _stream(erig, seqs[-1])
    assert none == []
    cas(erig, R.RETAINED, R.TRASH)
    assert [e["state"] for e in canon_list(erig.events.after(seqs[-1]))] == [R.TRASH]
    # the session PURGED, a second session's events a day later: pruning drops the first's state/artifact events and
    # the day-old pool event
    cas(erig, R.TRASH, R.PURGED)
    other = "s-00000000000000a2"
    erig.clock.t += EV.POOL_KEEP_S + 10
    t2 = ticket_of(erig.mint("a-2"))
    _conn, lo = lease(erig, t2)
    att = root(erig, "ATTACH", lease_nonce=lo["lease_nonce"], sudo_uid=DISPATCH_UID, session=other,
               claims=d_attach_claims(t2))
    assert att["ok"], att
    assert erig.events.prune() > 0
    kept = erig.kept()
    assert not [e for e in kept if e.get("session") == SESSION]
    hello, gap, got = _stream(erig, 0)
    oldest = kept[0]["seq"]
    assert hello["oldest_seq"] == oldest > 1
    assert gap == {"kind": "gap", "oldest_seq": oldest}
    assert got == kept
    # a cursor inside the holes is not older than the oldest kept event: no gap, just what is past it
    _h, gap2, got2 = _stream(erig, oldest)
    assert gap2 is None and got2 == kept[1:]


def canon_list(got) -> list:
    return [canon.parse(b) for _s, b in got]


def test_journal_id_kept_across_restart_and_a_new_meta_is_a_new_journal(erig):
    bound_session(erig)
    jid = erig.events.journal_id
    assert len(jid) == 32
    erig.restart()
    assert erig.events.journal_id == jid
    newest = erig.kept()[-1]["seq"]
    (erig.sdir / EV.META).unlink()
    erig.restart()
    assert erig.events.journal_id != jid
    assert erig.events.next_seq > newest                              # seq still rises: the lines were kept


def test_session_events_from_dispatch_only_and_after_seq_typed(erig):
    for uid in (0, OP_UID, KEYS_UID):
        ans = erig.control.handle(C.Conn(uid), {"v": 1, "op": "SESSION-EVENTS", "id": 1, "after_seq": 0})
        assert refused(ans) == "verb-not-allowed-for-peer"
    assert refused(erig.ask("SESSION-EVENTS", after_seq=-1)) == "out-of-range"
    assert refused(erig.ask("SESSION-EVENTS", after_seq="0")) == "wrong-type"


# ---- the start pass's re-emit (T:3c/test_missing_event_reemitted_at_start) --------------------------------------

class Crash(Exception):
    """CONTROL dying at a journal fault point (the poison rule makes the journal behave as a dead CONTROL's would)."""


@pytest.mark.parametrize("point", ["before_write", "before_fsync"])
@pytest.mark.parametrize("move", ["state", "artifact"])
def test_missing_event_reemitted_at_start(st, point, move):  # noqa: F811
    """The record write landed and the append did not (CONTROL died between the two): the next start compares each
    ticket-bearing record's generation with the last event naming it and appends the missing one, of the kind STORED
    with that generation. A `before_fsync` crash may leave the line on disk whole: then nothing is appended twice."""
    rig = ERig(st, Clock())
    bound_session(rig)
    to_retained(rig)
    before = rig.session_events()
    armed = {"on": True}

    def fault(p: str) -> None:
        if armed["on"] and p == point:
            raise Crash(p)

    rig.events.fault = fault
    if move == "state":
        cas(rig, R.RETAINED, R.TRASH)
    else:
        cas(rig, R.RETAINED, R.RETAINED, artifact_digest=DIG)
    rec = rig.control.store.get(SESSION)
    assert rig.events.poisoned and rig.control.owed_events() == {SESSION: rec.generation}
    assert rig.session_events() == before                              # memory never took it
    armed["on"] = False
    rig.restart()
    evs = rig.session_events()
    new = [e for e in evs if e["generation"] == rec.generation]
    assert len(new) == 1 and new[0]["kind"] == move, evs
    if move == "state":
        assert new[0]["state"] == R.TRASH
    else:
        assert new[0]["artifact_digest"] == DIG
    assert rig.control.owed_events() == {}
    # a second restart re-emits nothing (idempotent by (session, generation))
    n = len(rig.kept())
    rig.restart()
    assert len([e for e in rig.kept() if e["kind"] != "pool"]) == len([e for e in rig.kept()[:n] if e["kind"] != "pool"])


def test_a_none_generation_owes_nothing_at_start(erig):
    """A record whose newest generation is `event_kind: none` (a field-only CAS) re-emits nothing: its event-bearing
    generation's event is already kept."""
    bound_session(erig)
    to_retained(erig)
    cas(erig, R.RETAINED, R.RETAINED, held_attempts=2)
    n = len(erig.session_events())
    erig.restart()
    assert len(erig.session_events()) == n


def test_purged_record_is_not_reemitted(erig):
    bound_session(erig)
    to_retained(erig)
    cas(erig, R.RETAINED, R.TRASH)
    erig.events.fault = lambda p: (_ for _ in ()).throw(Crash(p))
    cas(erig, R.TRASH, R.PURGED)
    erig.events.fault = lambda p: None
    erig.restart()
    assert R.PURGED not in [e.get("state") for e in erig.session_events()]


# ---- the owed-event fence ---------------------------------------------------------------------------------------

def test_an_owed_event_fences_later_writes_but_never_the_revoke(erig):
    """A generation whose append failed is owed: a later CAS of that session is refused `events-unavailable` (it would
    overwrite the generation whose event is unwritten); the revoke still lands; a restart repays."""
    ticket, _r, _att = bound_session(erig)
    erig.events.fault = lambda p: (_ for _ in ()).throw(Crash(p))
    cas(erig, R.ALLOCATED, R.RENDERING)                               # lands; its event does not
    gen = erig.control.store.get(SESSION).generation
    assert erig.control.owed_events() == {SESSION: gen}
    ans = root(erig, "CAS", session=SESSION, generation=gen, fields={}, **{"from": "RENDERING", "to": "RENDERED"})
    assert refused(ans) == "events-unavailable"
    assert erig.control.store.get(SESSION).state == R.RENDERING
    assert root(erig, "REVOKE", session=SESSION)["ok"]               # the revoke path is never fenced
    rec = erig.control.store.get(SESSION)
    assert rec.state == R.REVOKED and erig.control.owed_events() == {SESSION: rec.generation}
    erig.events.fault = lambda p: None
    erig.restart()
    assert erig.session_events()[-1]["state"] == R.REVOKED and erig.control.owed_events() == {}


def test_an_owed_event_is_repaid_by_the_next_write_when_the_journal_recovers(erig):
    """An append that failed WITHOUT poisoning (here: a refusal the journal raised before writing) is repaid by the
    session's next write, which then lands."""
    bound_session(erig)
    real = erig.events.append
    calls = {"n": 0}

    def flaky(ev):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Refused("events-unavailable", "injected")
        return real(ev)

    erig.events.append = flaky
    cas(erig, R.ALLOCATED, R.RENDERING)
    assert erig.control.owed_events()
    assert root(erig, "REVOKE", session=SESSION)["ok"]
    assert erig.control.owed_events() == {}
    assert [e["state"] for e in erig.session_events()] == [R.ALLOCATED, R.RENDERING, R.REVOKED]


# ---- the journal file -----------------------------------------------------------------------------------------

def test_torn_tail_cut_and_corrupt_line_refuses_start(erig):
    bound_session(erig)
    path = erig.sdir / EV.JOURNAL
    whole = path.read_bytes()
    path.write_bytes(whole + b'{"at":"2026-')                          # a torn append
    erig.restart()
    after = path.read_bytes()
    assert after.startswith(whole) and after.endswith(b"\n")
    assert all(EV.check_event(canon.parse(x)) is None for x in after.splitlines())   # cut; later appends are whole
    path.write_bytes(whole + b'{"kind":"bogus"}\n')
    with pytest.raises(Refused) as e:
        erig.restart()
    assert e.value.name == "events-corrupt"
    path.write_bytes(whole)
    lines = whole.splitlines()
    path.write_bytes(b"\n".join([lines[-1], lines[0]]) + b"\n")         # seq does not rise
    with pytest.raises(Refused) as e:
        erig.restart()
    assert e.value.name == "events-corrupt"


def test_old_pool_events_pruned_newest_kept(erig):
    """`pool` events older than one day are pruned, the newest event always kept (seq carries on)."""
    first = erig.kept("pool")
    erig.clock.t += EV.POOL_KEEP_S + 10
    sync_free(erig.control, egress=5)
    assert erig.events.prune() == len(first)
    erig.clock.t += EV.POOL_KEEP_S + 10
    assert erig.events.prune() == 0                                    # the newest stays even when old
    assert len(erig.kept()) == 1


# ---- the stream over a real socket ------------------------------------------------------------------------------

def _frame(sock: socket.socket, timeout: float = 5.0) -> dict:
    sock.settimeout(timeout)
    obj = C.read_frame(sock)
    assert obj is not None
    return obj


def test_stream_over_a_control_server_socket(erig, tmp_path):
    """The stream on a real `ControlServer`: the hello answer, the replay, then a live event pushed as the CAS that
    makes it answers; a peer that sends anything on the stream is closed."""
    import tempfile

    d = Path(tempfile.mkdtemp(dir="/tmp"))                             # a unix socket path must stay short
    server = C.ControlServer(erig.control, d / "c.sock", peer_uid=lambda s: DISPATCH_UID)
    erig.control.stop()                                                 # the server's start runs the start pass again
    erig.control = C.Control(C.StateStore(erig.sdir), OP_UID, dispatch_uid=DISPATCH_UID, keys_uid=KEYS_UID,
                             routes=RM.ControlRoutes(erig.st.routes), tickets=erig.tickets, clock=erig.clock,
                             events=erig.events)
    server.control = erig.control
    server.start()
    try:
        sync_free(erig.control)
        erig.attach(erig.cr.head())
        bound_session(erig)
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(str(d / "c.sock"))
        c.sendall(C.encode_frame({"v": 1, "op": "SESSION-EVENTS", "id": 3, "after_seq": 0}))
        hello = _frame(c)
        assert hello["kind"] == "hello" and hello["journal_id"] == erig.events.journal_id
        kept = erig.kept()
        got = [_frame(c) for _ in kept]
        assert got == kept
        cas(erig, R.ALLOCATED, R.RENDERING)
        live = _frame(c)
        assert live["state"] == R.RENDERING and live["seq"] == kept[-1]["seq"] + 1
        c.sendall(b"x")                                                 # nothing is a verb on a stream
        c.settimeout(5)
        try:                                     # closed: EOF, or a reset where the server's close found our byte unread (Linux)
            assert c.recv(1) == b""
        except ConnectionResetError:
            pass
        c.close()
    finally:
        server.stop()


# ---- CONTROL's program: serve() in a child process, a real socket client ----------------------------------------

RUNNER = r'''
import sys
from pathlib import Path
from levain.slot import control as C

d = Path(sys.argv[1])

def peer_uid(_sock):
    return int((d / "next_uid").read_text())

C.serve(C.ServeConfig(store_dir=d / "sessions", socket_path=d / "c.sock", operator_uid=int(sys.argv[2]),
                      dispatch_uid=int(sys.argv[3]), peer_uid=peer_uid,
                      ready=lambda _s: (d / "ready").write_text("1")))
'''


def _ticket_record(session: str, state: str, generation: int, kind: str, ticket: str, **fields) -> bytes:
    claims = d_attach_claims(ticket)
    return R.state_bytes(R.SessionState(session=session, generation=generation, state=state, claims=claims,
                                        lease_live=False, event_kind=kind, updated_at="2026-10-25T00:00:00Z",
                                        fields=fields))


def _connect(d: Path, uid: int) -> socket.socket:
    (d / "next_uid").write_text(str(uid))
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.connect(str(d / "c.sock"))
    return c


def _call(d: Path, uid: int, req: dict) -> dict:
    c = _connect(d, uid)
    try:
        c.sendall(C.encode_frame({"v": 1, "id": 1, **req}))
        return _frame(c)
    finally:
        c.close()


def _spawn(d: Path) -> subprocess.Popen:
    (d / "ready").unlink(missing_ok=True)
    (d / "runner.py").write_text(RUNNER)
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    p = subprocess.Popen([sys.executable, str(d / "runner.py"), str(d), str(OP_UID), str(DISPATCH_UID)], env=env,
                         stderr=subprocess.PIPE)
    end = time.monotonic() + 20
    while not (d / "ready").exists():
        assert p.poll() is None, p.stderr.read().decode()
        assert time.monotonic() < end, "CONTROL did not start"
        time.sleep(0.05)
    return p


def _stop(p: subprocess.Popen) -> None:
    p.send_signal(signal.SIGTERM)
    p.wait(20)


def test_served_control_streams_and_reemits():
    """The residue run: CONTROL's program (`control.serve`) in a child process over a store holding two ticket-bearing
    records whose events were never appended (the crash between record and append). Its start pass re-emits both, of
    their stored kinds; a dispatch client streams them after the hello; root's CAS on the socket pushes the next event
    to the open stream; after a restart, a client resuming at its cursor gets only what is past it, same journal."""
    import tempfile

    d = Path(tempfile.mkdtemp(dir="/tmp"))
    sessions = d / "sessions"
    sessions.mkdir(mode=0o700)
    ta, tb = C.new_handle(), C.new_handle()
    a, b = "s-00000000000000c1", "s-00000000000000c2"
    (sessions / f"{a}.json").write_bytes(_ticket_record(a, R.RETAINED, 9, "state", ta))
    (sessions / f"{b}.json").write_bytes(_ticket_record(b, R.RETAINED, 11, "artifact", tb, artifact_failed="v7-cap"))
    p = _spawn(d)
    try:
        s = _connect(d, DISPATCH_UID)
        s.sendall(C.encode_frame({"v": 1, "op": "SESSION-EVENTS", "id": 2, "after_seq": 0}))
        hello = _frame(s)
        assert hello["ok"] and hello["kind"] == "hello" and hello["oldest_seq"] == 1 and hello["newest_seq"] == 2
        got = [_frame(s), _frame(s)]
        assert [(e["session"], e["kind"], e["generation"], e["ticket"]) for e in got] == \
            [(a, "state", 9, ta), (b, "artifact", 11, tb)]
        assert got[0]["state"] == R.RETAINED and got[1]["artifact_failed"] == "v7-cap"
        ans = _call(d, 0, {"op": "CAS", "session": a, "generation": 9, "from": "RETAINED", "to": "TRASH",
                           "fields": {}})
        assert ans["ok"] and ans["generation"] == 10, ans
        live = _frame(s)
        assert (live["seq"], live["session"], live["state"], live["generation"]) == (3, a, R.TRASH, 10)
        s.close()
    finally:
        _stop(p)
    jid = hello["journal_id"]
    p = _spawn(d)
    try:
        s = _connect(d, DISPATCH_UID)
        s.sendall(C.encode_frame({"v": 1, "op": "SESSION-EVENTS", "id": 2, "after_seq": 2}))
        hello2 = _frame(s)
        assert hello2["journal_id"] == jid and hello2["newest_seq"] == 3
        again = _frame(s)
        assert again["seq"] == 3 and again["state"] == R.TRASH          # nothing re-emitted, nothing before the cursor
        s.settimeout(0.5)
        with pytest.raises(socket.timeout):
            s.recv(4)
        s.close()
    finally:
        _stop(p)


# ---- ARTIFACT-ACK (CONTRACT §3.5 Retention) ------------------------------------------------------------------------

def ack(rig, **kw) -> dict:
    return rig.ask("ARTIFACT-ACK", **kw)


def test_artifact_ack_needs_the_published_digest(erig):
    """An executor ticket acks its artifact: only the digest the record carries (`artifact-not-published` before the
    3b CAS, for another digest, for the verifier form); the ack writes `artifact_acked` (event_kind none, no event) and
    wakes the reaper; a repeat answers `{}` and writes nothing."""
    wakes = []
    erig.control._wake_reaper = lambda: wakes.append(1)
    ticket, _r, _a = bound_session(erig)
    to_retained(erig)
    assert refused(ack(erig, ticket=ticket, artifact_digest=DIG)) == "artifact-not-published"
    assert refused(ack(erig, ticket=ticket)) == "artifact-not-published"
    cas(erig, R.RETAINED, R.RETAINED, artifact_digest=DIG)
    assert refused(ack(erig, ticket=ticket, artifact_digest="sha256:" + "b" * 64)) == "artifact-not-published"
    assert refused(ack(erig, ticket=ticket, verifier_report_digest=DIG)) == "artifact-not-published"
    assert refused(ack(erig, ticket=ticket)) == "artifact-not-published"          # a digest exists: name it
    n_events, n_wakes = len(erig.kept()), len(wakes)
    assert ack(erig, ticket=ticket, artifact_digest=DIG) == {"id": 1, "ok": True}
    rec = erig.control.store.get(SESSION)
    assert rec.fields["artifact_acked"] is True and rec.event_kind == "none" and rec.state == R.RETAINED
    assert len(erig.kept()) == n_events and len(wakes) == n_wakes + 1
    gen = rec.generation
    assert ack(erig, ticket=ticket, artifact_digest=DIG) == {"id": 1, "ok": True}
    assert erig.control.store.get(SESSION).generation == gen


def test_artifact_ack_of_a_failed_artifact_names_no_digest(erig):
    ticket, _r, _a = bound_session(erig)
    to_retained(erig)
    cas(erig, R.RETAINED, R.RETAINED, artifact_failed="adopt-path-too-long")
    assert refused(ack(erig, ticket=ticket, artifact_digest=DIG)) == "artifact-not-published"
    assert ack(erig, ticket=ticket)["ok"]
    assert erig.control.store.get(SESSION).fields["artifact_acked"] is True


def test_artifact_ack_refusals_by_name(erig):
    ticket, _r, _a = bound_session(erig)
    assert refused(ack(erig, ticket=C.new_handle())) == "ticket-unknown"
    assert refused(ack(erig, ticket=ticket, artifact_digest=DIG, verifier_report_digest=DIG)) == "bad-value"
    assert refused(ack(erig, ticket=ticket, artifact_digest="sha256:XYZ")) == "bad-token"
    assert refused(ack(erig, ticket=ticket, artifact_digest=7)) == "wrong-type"
    for uid in (0, OP_UID, KEYS_UID):
        ans = erig.control.handle(C.Conn(uid), {"v": 1, "op": "ARTIFACT-ACK", "id": 1, "ticket": ticket})
        assert refused(ans) == "verb-not-allowed-for-peer"


@pytest.mark.parametrize("refusal", [False, True])
def test_verifier_ticket_acks_its_report(st, refusal):  # noqa: F811
    """A VERIFIER ticket (no artifact step) acks its report: the record's `verifier_report_digest`, or no digest when
    `verifier_report_refused` is set; an `artifact_digest` from it is `artifact-not-published`."""
    from tests.test_slot_3c8 import SESSION_V, VRig, vitem

    rig = VRig(st, Clock())
    assert rig.submit_v(vitem(1))["ok"]
    ticket = ticket_of(rig.vmint())
    assert rig.vattach(ticket)["ok"]
    cas(rig, R.ALLOCATED, R.RENDERING, SESSION_V)
    assert root(rig, "REVOKE", session=SESSION_V)["ok"]
    for frm, to in END_CHAIN[:-1]:
        cas(rig, frm, to, SESSION_V)
    field = {"verifier_report_refused": "verifier-failed:exit"} if refusal else {"verifier_report_digest": DIG}
    cas(rig, R.CREDENTIAL_PARKED, R.CREDENTIAL_PARKED, SESSION_V, **field)
    cas(rig, R.CREDENTIAL_PARKED, R.RETAINED, SESSION_V)
    assert refused(ack(rig, ticket=ticket, artifact_digest=DIG)) == "artifact-not-published"
    if refusal:
        assert refused(ack(rig, ticket=ticket, verifier_report_digest=DIG)) == "artifact-not-published"
        assert ack(rig, ticket=ticket)["ok"]
    else:
        assert refused(ack(rig, ticket=ticket)) == "artifact-not-published"
        assert ack(rig, ticket=ticket, verifier_report_digest=DIG)["ok"]
    assert rig.control.store.get(SESSION_V).fields["artifact_acked"] is True


def test_a_repeat_artifact_ack_wakes_the_reaper_too(erig):
    """3c-8 slice 2 fix r3 (b): an ack of a record already acked writes nothing but still wakes the reaper, so a wake
    lost after the first ack is made good by dispatch's retry."""
    wakes = []
    erig.control._wake_reaper = lambda: wakes.append(1)
    ticket, _r, _a = bound_session(erig)
    to_retained(erig)
    cas(erig, R.RETAINED, R.RETAINED, artifact_digest=DIG)
    assert ack(erig, ticket=ticket, artifact_digest=DIG)["ok"]
    gen, n = erig.control.store.get(SESSION).generation, len(wakes)
    assert ack(erig, ticket=ticket, artifact_digest=DIG)["ok"]
    assert erig.control.store.get(SESSION).generation == gen and len(wakes) == n + 1
