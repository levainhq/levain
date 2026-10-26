"""levain.dispatch.follow: the SESSION-EVENTS follower and its ARTIFACT-ACK, slice 1 (1025+30 ackscout DESIGN).

CONTROL is real: test_slot_3c8's verifier rig with the event journal on disk, served on a `ControlServer` socket; the
job store is real (test_dispatch_jobs' verifying job and git repo), and `JobStore.record_verification` is the real
writer. A crash is an exception raised at the follower's fault seam."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from levain.dispatch import follow as F
from levain.slot import Refused
from levain.slot import control as C
from levain.slot import control_client as CC
from levain.slot import events as EV
from levain.slot import records as R
from levain.slot import routemaps as RM
from levain.slot import tickets as T
from tests import test_dispatch_verifier_report as tvr
from tests.test_dispatch_jobs import at_state, base_tree, clock, model, repo, store  # noqa: F401 - fixtures
from tests.test_routemaps import st  # noqa: F401 - the signed store fixture
from tests.test_slot_3c8 import SESSION_V, VRig, vitem
from tests.test_slot_control_events import END_CHAIN, cas, to_retained
from tests.test_tickets import DISPATCH_UID, KEYS_UID, OP_UID, Clock, bound_session, root, sync_free, ticket_of


class Crash(Exception):
    pass


class EVRig(VRig):
    """test_slot_3c8's verifier rig with CONTROL's event journal, served on a real socket."""

    def restart(self, *, fault=None, attach: bool = True) -> None:
        if self.control is not None:
            self.control.stop()
        self.tickets = T.TicketStore(self.sdir / T.JOURNAL, self.st.L / "policy" / "dispatch_ceiling",
                                     clock=self.clock, ceiling_uid=os.getuid())
        self.cr = RM.ControlRoutes(self.st.routes)
        self.cv = RM.ControlVerifiers(self.st.routes)
        self.events = EV.EventJournal(self.sdir, clock=self.clock)
        self.control = C.Control(C.StateStore(self.sdir), OP_UID, dispatch_uid=DISPATCH_UID, keys_uid=KEYS_UID,
                                 routes=self.cr, tickets=self.tickets, clock=self.clock, verifiers=self.cv,
                                 events=self.events)
        self.control.start()
        sync_free(self.control)
        if attach:
            self.attach(self.cr.head())

    def kept(self, kind=None) -> list:
        return [e for e in self.events.kept() if kind is None or e["kind"] == kind]


@pytest.fixture
def served(st):  # noqa: F811
    rig = EVRig(st, Clock())
    path = Path(tempfile.mkdtemp(dir="/tmp")) / "c.sock"               # a unix socket path must stay short
    server = C.ControlServer(rig.control, path, peer_uid=lambda _s: DISPATCH_UID)
    server.start()
    rig.path = path
    try:
        yield rig
    finally:
        server.stop()


class Dispatch:
    """dispatch's side: a verifying job of the job store, the run its verifier ticket is, and the follower."""

    def __init__(self, store, repo, rig, *, refused: str | None = None) -> None:  # noqa: F811
        self.store, self.repo, self.rig = store, repo, rig
        self.job, _ = at_state(store, repo, "verifying")
        self.ticket = "unset"
        self.run = tvr.vrun(self.job.id, 0)
        self.raw, self.digest = None, None
        self.resolved: list = []
        self.faults: dict = {}
        self.refused = refused

    def bind(self, ticket: str) -> None:
        self.ticket = ticket
        self.run = dataclasses.replace(self.run, session=SESSION_V, ticket=ticket)
        if not self.refused:
            self.raw, self.digest = tvr.envelope(self.run)

    def resolve(self, ev):
        self.resolved.append(ev["seq"])
        if ev.get("ticket") != self.ticket:
            return None
        return F.VerifierCall(self.run, self.raw, str(self.repo[0]), base_tree(self.repo), model(0))

    def fault(self, where: str, seq: int) -> None:
        if self.faults.pop(where, None):
            raise Crash(where)

    def follower(self) -> F.EventFollower:
        return F.EventFollower.at(self.store, self.rig.path, self.resolve, read_deadline=0.5, fault=self.fault)


def setup_verifier(store, repo, rig, *, refused=None):  # noqa: F811
    d = Dispatch(store, repo, rig, refused=refused)
    # the envelope binds the session's ticket, which CONTROL mints: mint first, then CAS the digest it yields
    assert rig.submit_v(vitem(1))["ok"]
    ticket = ticket_of(rig.vmint())
    d.bind(ticket)
    assert rig.vattach(ticket)["ok"]
    cas(rig, R.ALLOCATED, R.RENDERING, SESSION_V)
    assert root(rig, "REVOKE", session=SESSION_V)["ok"]
    for frm, to in END_CHAIN[:-1]:
        cas(rig, frm, to, SESSION_V)
    field = {"verifier_report_refused": refused} if refused else {"verifier_report_digest": d.digest}
    cas(rig, R.CREDENTIAL_PARKED, R.CREDENTIAL_PARKED, SESSION_V, **field)
    cas(rig, R.CREDENTIAL_PARKED, R.RETAINED, SESSION_V)
    return d


def acked(rig) -> bool:
    return rig.control.store.get(SESSION_V).fields.get("artifact_acked") is True


def test_replay_records_the_verifier_once_acks_and_moves_the_cursor(store, repo, served):  # noqa: F811
    d = setup_verifier(store, repo, served)
    f = d.follower()
    assert f.step().outcome == CC.DEADLINE
    assert [r.run for r in store.run_records(d.job.id)] == [0]
    assert acked(served)
    newest = served.kept()[-1]["seq"]
    assert store.event_cursor() == (served.events.journal_id, newest)
    # a second connection resumes past the cursor: nothing is re-delivered
    d.resolved.clear()
    assert f.step().outcome == CC.DEADLINE and d.resolved == []
    f.close()


def test_a_live_event_reaches_the_follower(store, repo, served):  # noqa: F811
    """The follower connects before the verifier's terminal move; the event arrives live on the open stream."""
    d = Dispatch(store, repo, served)
    assert served.submit_v(vitem(1))["ok"]
    ticket = ticket_of(served.vmint())
    d.bind(ticket)
    assert served.vattach(ticket)["ok"]
    f = d.follower()
    assert f.step().outcome == CC.DEADLINE                                     # the replay so far: nothing terminal
    cas(served, R.ALLOCATED, R.RENDERING, SESSION_V)
    assert root(served, "REVOKE", session=SESSION_V)["ok"]
    for frm, to in END_CHAIN[:-1]:
        cas(served, frm, to, SESSION_V)
    cas(served, R.CREDENTIAL_PARKED, R.CREDENTIAL_PARKED, SESSION_V, verifier_report_digest=d.digest)
    cas(served, R.CREDENTIAL_PARKED, R.RETAINED, SESSION_V)
    assert f.step().outcome == CC.DEADLINE
    assert [r.run for r in store.run_records(d.job.id)] == [0] and acked(served)
    f.close()


def test_a_refused_report_fails_the_job_and_acks_with_no_digest(store, repo, served):  # noqa: F811
    d = setup_verifier(store, repo, served, refused="verifier-failed:exit")
    f = d.follower()
    assert f.step().outcome == CC.DEADLINE
    job = store.get(d.job.id)
    assert (job.state, job.history[-1].cause) == ("failed", "verifier_refused")
    assert acked(served)
    f.close()


@pytest.mark.parametrize("where", ["written", "acked"])
def test_a_crash_re_delivers_and_converges(store, repo, served, where):  # noqa: F811
    """A crash after the write (before the ACK) or after the ACK (before the cursor): the cursor stays before the
    event, the next connection re-delivers it, the write is not repeated and the ACK repeat is accepted."""
    d = setup_verifier(store, repo, served)
    d.faults[where] = True
    f = d.follower()
    with pytest.raises(Crash):
        f.step()
    assert [r.run for r in store.run_records(d.job.id)] == [0]
    assert acked(served) is (where == "acked")
    before = store.event_cursor()
    assert before is None or before[1] < served.kept()[-1]["seq"]
    assert f.step().outcome == CC.DEADLINE
    assert [r.run for r in store.run_records(d.job.id)] == [0] and acked(served)
    assert store.event_cursor() == (served.events.journal_id, served.kept()[-1]["seq"])
    f.close()


def test_another_journal_restarts_from_zero_and_converges(store, repo, served):  # noqa: F811
    d = setup_verifier(store, repo, served)
    store.set_event_cursor("j-another", served.kept()[-1]["seq"])
    f = d.follower()
    assert f.step().outcome == "journal-changed" and store.event_cursor() is None
    assert f.step().outcome == CC.DEADLINE
    assert [r.run for r in store.run_records(d.job.id)] == [0] and acked(served)
    f.close()


def test_a_reset_restarts_from_zero_and_converges(store, repo, served):  # noqa: F811
    d = setup_verifier(store, repo, served)
    store.set_event_cursor(served.events.journal_id, served.kept()[-1]["seq"] + 50)
    f = d.follower()
    assert f.step().outcome == "reset" and store.event_cursor() is None
    assert f.step().outcome == CC.DEADLINE
    assert [r.run for r in store.run_records(d.job.id)] == [0] and acked(served)
    f.close()


def test_a_gap_is_returned_counted_and_the_follower_continues(store, repo, served, caplog):  # noqa: F811
    """RULINGS_OBS 8: the step returns the gap it read and how many events it skipped unread, beside the log line."""
    d = Dispatch(store, repo, served)
    first = served.kept("pool")
    assert served.kept() == first                                      # every event so far is one the prune drops
    served.clock.t += EV.POOL_KEEP_S + 10
    sync_free(served.control, egress=5)
    assert served.events.prune() == len(first)
    f = d.follower()
    with caplog.at_level(logging.WARNING, logger="levain.dispatch.follow"):
        out = f.step()
    assert out.outcome == CC.DEADLINE
    assert [g.oldest_seq for g in out.gaps] == [first[-1]["seq"] + 1] and out.lost == len(first)
    assert any("pruned unread" in r.getMessage() for r in caplog.records)
    assert store.event_cursor() == (served.events.journal_id, served.kept()[-1]["seq"])
    f.close()


def test_an_artifact_event_is_passed_and_never_acked(store, repo, served):  # noqa: F811
    """Slice 1 has no artifact reader: the follower moves past an executor's artifact event without ACK-ing, so the
    reaper keeps the retained work until dispatch_retain_days."""
    from tests.test_tickets import SESSION
    d = Dispatch(store, repo, served)
    bound_session(served)
    to_retained(served)
    cas(served, R.RETAINED, R.RETAINED, SESSION, artifact_digest="sha256:" + "a" * 64)
    f = d.follower()
    assert f.step().outcome == CC.DEADLINE
    assert "artifact_acked" not in served.control.store.get(SESSION).fields
    assert store.event_cursor()[1] == served.kept()[-1]["seq"] and served.kept()[-1]["kind"] == "artifact"
    assert d.resolved == []
    f.close()


def test_a_refused_ack_stops_the_follower_by_name_with_the_cursor_unmoved(store, repo, served):  # noqa: F811
    """CONTROL's record now carries another report digest than the event dispatch recorded from: the ACK is
    `artifact-not-published`, the follower stops naming it, and the cursor stays before the event."""
    d = setup_verifier(store, repo, served)
    cas(served, R.RETAINED, R.RETAINED, SESSION_V, verifier_report_digest="sha256:" + "b" * 64)
    f = d.follower()
    with pytest.raises(F.FollowStopped) as e:
        f.step()
    assert e.value.name == "artifact-not-published"
    assert not acked(served)
    seq = [x for x in served.kept() if x.get("session") == SESSION_V and x.get("state") == R.RETAINED][0]["seq"]
    cur = store.event_cursor()
    assert cur is None or cur[1] < seq
    f.close()


def test_an_unresolved_verifier_ticket_stops_the_follower(store, repo, served):  # noqa: F811
    d = setup_verifier(store, repo, served)
    d.ticket = "another"
    f = d.follower()
    with pytest.raises(F.FollowStopped) as e:
        f.step()
    assert e.value.name == "ticket-unresolved" and not acked(served)
    f.close()


def test_a_job_failed_elsewhere_is_passed_unacked_and_the_cursor_moves(store, repo, served):  # noqa: F811
    """A job another run's verifier outcome already failed: dispatch holds nothing from this event, so no ACK lets the
    reaper trash the retained work; the follower moves past it."""
    d = setup_verifier(store, repo, served)
    run1 = dataclasses.replace(d.run, run=1, ticket="t-run-1")
    other = {"kind": "state", "session": SESSION_V, "ticket": run1.ticket, "state": "RETAINED",
             "at": "2026-10-25T00:00:00.000000Z", "verifier_report_refused": "verifier-failed:exit"}
    store.record_verification(run1, envelope=None, event=other, repo=str(repo[0]), tree=base_tree(repo),
                              model=model(1))
    f = d.follower()
    assert f.step().outcome == CC.DEADLINE
    assert store.get(d.job.id).history[-1].evidence == "verifier-failed:exit"
    assert not acked(served)
    assert store.event_cursor() == (served.events.journal_id, served.kept()[-1]["seq"])
    f.close()


def test_a_ticket_that_failed_the_job_with_another_outcome_stops_the_follower(store, repo, served):  # noqa: F811
    """This ticket already failed the job with a refusal, and CONTROL's event for it carries a report: the store
    holds another outcome for this run, so the follower stops (record-conflict) with no ACK and the cursor before the
    event."""
    d = setup_verifier(store, repo, served)
    other = {"kind": "state", "session": SESSION_V, "ticket": d.ticket, "state": "RETAINED",
             "at": "2026-10-25T00:00:00.000000Z", "verifier_report_refused": "verifier-failed:exit"}
    store.record_verification(d.run, envelope=None, event=other, repo=str(repo[0]), tree=base_tree(repo),
                              model=model(0))
    f = d.follower()
    with pytest.raises(F.FollowStopped) as exc:
        f.step()
    assert exc.value.name == "record-conflict"
    assert not acked(served)
    [done] = [e for e in served.kept("state") if e.get("ticket") == d.ticket and "verifier_report_digest" in e]
    cur = store.event_cursor()
    assert cur is None or cur[1] < done["seq"]
    f.close()


def test_a_byte_copy_of_another_tickets_stored_envelope_stops_the_follower_unacked(store, repo, served):  # noqa: F811
    """RULINGS_OBS 18: CONTROL's verifier session (ticket B) publishes the digest of ticket A's envelope, whose report
    the store already holds, and dispatch reads B's retained envelope as a byte copy of A's. That is no repeat of B:
    the follower stops (record-conflict), sends no ACK (the reaper keeps B's retained work) and leaves the cursor
    before the event."""
    d = Dispatch(store, repo, served)
    assert served.submit_v(vitem(1))["ok"]
    ticket = ticket_of(served.vmint())
    d.bind(ticket)
    a_run = dataclasses.replace(d.run, ticket="t-a-copied")
    d.raw, d.digest = tvr.envelope(a_run)
    store.record_verification(a_run, envelope=d.raw, event=tvr.event(a_run, at="2026-10-25T00:00:00Z",
                                                                      verifier_report_digest=d.digest),
                              repo=str(repo[0]), tree=base_tree(repo), model=model(0))
    assert served.vattach(ticket)["ok"]
    cas(served, R.ALLOCATED, R.RENDERING, SESSION_V)
    assert root(served, "REVOKE", session=SESSION_V)["ok"]
    for frm, to in END_CHAIN[:-1]:
        cas(served, frm, to, SESSION_V)
    cas(served, R.CREDENTIAL_PARKED, R.CREDENTIAL_PARKED, SESSION_V, verifier_report_digest=d.digest)
    cas(served, R.CREDENTIAL_PARKED, R.RETAINED, SESSION_V)
    f = d.follower()
    with pytest.raises(F.FollowStopped) as exc:
        f.step()
    assert exc.value.name == "record-conflict"
    assert not acked(served)
    [done] = [e for e in served.kept("state") if e.get("ticket") == ticket and "verifier_report_digest" in e]
    cur = store.event_cursor()
    assert cur is None or cur[1] < done["seq"]
    f.close()


@pytest.mark.parametrize("bad", [{"ticket": 7}, {"verifier_report_digest": "sha256:nothex"},
                                 {"verifier_report_refused": ""},
                                 {"verifier_report_refused": "x", "verifier_report_digest": "sha256:" + "0" * 64}])
def test_a_misshapen_verifier_event_stops_before_resolve_and_writes_nothing(store, repo, served, bad):  # noqa: F811
    d = Dispatch(store, repo, served)
    f = d.follower()
    body = {"seq": 9, "kind": "state", "session": SESSION_V, "ticket": "t", "state": "RETAINED",
            "at": "2026-10-25T00:00:00Z", "verifier_report_digest": "sha256:" + "0" * 64, **bad}
    with pytest.raises(F.FollowStopped) as exc:
        f._handle(CC.Event(9, "state", body))
    assert exc.value.name == "event-shape" and d.resolved == []
    assert store.run_records(d.job.id) == [] and store.get(d.job.id).state == "verifying"
    f.close()


def test_close_ends_a_step_blocked_on_its_stream_and_a_closed_follower_opens_none(store, repo, served):  # noqa: F811
    import threading
    import time

    d = Dispatch(store, repo, served)
    f = F.EventFollower.at(store, served.path, d.resolve, read_deadline=10.0)
    # the store is this thread's, so the step runs here and close() comes from another thread once it reads live
    t = threading.Timer(0.5, f.close)
    t.start()
    began = time.monotonic()
    assert f.step().outcome == CC.CANCELLED
    assert time.monotonic() - began < 5.0
    t.join()
    assert f.step().outcome == CC.CANCELLED                                    # closed: no stream is opened after close


class DownClient:
    """An ACK connection whose every call finds CONTROL down."""

    def call(self, *_a, **_k):
        raise Refused("control-down", "the test's ACK connection")

    def close(self) -> None:
        pass


def test_a_control_down_ack_stops_the_step_and_the_next_step_opens_a_stream(store, repo, served):  # noqa: F811
    """RULINGS_OBS 13: one control-down during an ARTIFACT-ACK stops that step by name and drops only the ACK
    connection, never the follower: the next step opens a stream, re-delivers the event and acks it."""
    d = setup_verifier(store, repo, served)
    opened: list = []
    clients = [DownClient()]

    def stream(after, jid):
        opened.append(after)
        return CC.follow_events(served.path, after, jid, read_deadline=0.5)

    f = F.EventFollower(store, stream, lambda: clients.pop() if clients else CC.ControlClient(served.path),
                        d.resolve)
    with pytest.raises(F.FollowStopped) as exc:
        f.step()
    assert exc.value.name == "control-down" and not acked(served)
    assert f.step().outcome == CC.DEADLINE
    assert len(opened) == 2 and acked(served)
    f.close()


class HeldStream:
    """A stream that records its close and must never be read."""

    def __init__(self) -> None:
        self.closed = 0

    def __iter__(self):
        raise AssertionError("a stream opened across a close was read")

    def close(self) -> None:
        self.closed += 1


def test_a_step_after_close_never_opens_a_stream(store):  # noqa: F811
    """RULINGS_OBS 14: close() before a step: the step is CANCELLED without calling the stream factory."""
    calls: list = []
    f = F.EventFollower(store, lambda after, jid: calls.append(after) or HeldStream(), lambda: DownClient(),
                        lambda _ev: None)
    f.close()
    assert f.step().outcome == CC.CANCELLED and calls == []


def test_a_close_landing_while_the_stream_opens_closes_what_was_opened(store):  # noqa: F811
    """RULINGS_OBS 14: close() from another thread while the factory is opening the stream: the step ends CANCELLED
    and closes the stream the factory returned, unread."""
    import threading

    entered, release = threading.Event(), threading.Event()
    held = HeldStream()

    def opening(after, jid):
        entered.set()
        assert release.wait(5.0)
        return held

    f = F.EventFollower(store, opening, lambda: DownClient(), lambda _ev: None)

    def closer() -> None:
        assert entered.wait(5.0)
        f.close()
        release.set()

    t = threading.Thread(target=closer)
    t.start()
    assert f.step().outcome == CC.CANCELLED
    t.join(5.0)
    assert not t.is_alive() and held.closed >= 1


def _verifier_event_seq(rig) -> int:
    [done] = [e for e in rig.kept("state") if e.get("session") == SESSION_V and "verifier_report_digest" in e]
    return done["seq"]


def test_a_close_while_an_event_is_handled_connects_no_ack_and_ends_cancelled(store, repo, served):  # noqa: F811
    """RULINGS_OBS 16 (complement LOW B): close() from another thread while the step handles a verifier event (here
    inside resolve, before the ACK): no ACK connection is made after close, nothing is acked, the step ends CANCELLED
    and the cursor stays before the event, so the write converges on the next follower's re-delivery."""
    d = setup_verifier(store, repo, served)
    connects: list = []
    f = None

    def resolve(ev):
        f.close()
        return d.resolve(ev)

    def connect():
        connects.append(True)
        return CC.ControlClient(served.path)

    f = F.EventFollower(store, lambda after, jid: CC.follow_events(served.path, after, jid, read_deadline=0.5),
                        connect, resolve)
    assert f.step().outcome == CC.CANCELLED
    assert connects == [] and not acked(served) and f._ack_client is None
    assert [r.run for r in store.run_records(d.job.id)] == [0]
    cur = store.event_cursor()
    assert cur is None or cur[1] < _verifier_event_seq(served)


def test_a_close_landing_during_the_ack_call_ends_the_step_cancelled(store, repo, served):  # noqa: F811
    """RULINGS_OBS 16: close() closes the ACK connection while its call is in flight; the call then fails
    control-down, and the step ends CANCELLED (the caller's close), never FollowStopped, the cursor before the event."""
    d = setup_verifier(store, repo, served)
    f = None

    class ClosedUnder:
        def call(self, *_a, **_k):
            f.close()
            raise Refused("control-down", "closed")

        def close(self) -> None:
            pass

    f = F.EventFollower(store, lambda after, jid: CC.follow_events(served.path, after, jid, read_deadline=0.5),
                        ClosedUnder, d.resolve)
    assert f.step().outcome == CC.CANCELLED
    assert not acked(served)
    cur = store.event_cursor()
    assert cur is None or cur[1] < _verifier_event_seq(served)


def test_a_re_delivered_write_meets_the_repeat_seam_never_the_written_one(store, repo, served):  # noqa: F811
    """deepseek LOW (verify r1): after a crash past the write, the re-delivered event is an AlreadyRecorded repeat and
    the fault seam names it "repeat", so a seam armed on "written" for every call crashes only the first write."""
    d = setup_verifier(store, repo, served)
    hits: list = []

    def fault(where: str, seq: int) -> None:
        hits.append(where)
        if where == "written":
            raise Crash(where)

    f = F.EventFollower.at(store, served.path, d.resolve, read_deadline=0.5, fault=fault)
    with pytest.raises(Crash):
        f.step()
    assert f.step().outcome == CC.DEADLINE
    assert hits == ["written", "repeat", "acked"] and acked(served)
    f.close()


# -- slice 2: the executor's export artifact is read, then acked ------------------------------------------------------

BUNDLE = b"a git bundle, as far as the take can tell\n" * 40
BUNDLE_DIGEST = "sha256:" + hashlib.sha256(BUNDLE).hexdigest()
ART_DIGEST = "sha256:" + "a" * 64


def exported(tmp_path, session, ticket, named=None, content=BUNDLE, **over):
    """Root's published directory for ``session`` under a fresh export dir: `dispatch-artifact.json` and, unless the
    artifact names no bundle, the bundle file it digests."""
    from levain.dispatch import export as X
    obj = {"session": named or session, "ticket": ticket, "manifest_digest": "sha256:" + "c" * 64,
           "bundle": "bundle", "bundle_digest": "sha256:" + hashlib.sha256(content).hexdigest(), "files": 2,
           "bytes": 64}
    obj.update(over)
    (tmp_path / session).mkdir(exist_ok=True)
    (tmp_path / session / X.FILE).write_text(json.dumps(obj))
    if obj["bundle"] is not None:
        (tmp_path / session / obj["bundle"]).write_bytes(content)
    return tmp_path


def artifact_follower(d, export_dir, **kw):
    return F.EventFollower.at(d.store, d.rig.path, d.resolve, read_deadline=0.5, fault=d.fault, export_dir=export_dir,
                              **kw)


def published(served, **fields):
    """A bound executor session retained with an artifact field CAS'd, and the ticket CONTROL journaled for it."""
    from tests.test_tickets import SESSION
    bound_session(served)
    to_retained(served)
    cas(served, R.RETAINED, R.RETAINED, SESSION, **(fields or {"artifact_digest": ART_DIGEST}))
    ev = served.kept()[-1]
    assert ev["kind"] == "artifact"
    return SESSION, ev["ticket"], ev["seq"]


def test_a_read_artifact_is_acked_and_the_cursor_moves(store, repo, served, tmp_path):  # noqa: F811
    d = Dispatch(store, repo, served)
    session, ticket, seq = published(served)
    f = artifact_follower(d, exported(tmp_path, session, ticket))
    assert f.step().outcome == CC.DEADLINE
    assert served.control.store.get(session).fields.get("artifact_acked") is True
    assert store.event_cursor()[1] == seq
    f.close()


def test_an_artifact_with_no_bundle_is_acked_too(store, repo, served, tmp_path):  # noqa: F811
    d = Dispatch(store, repo, served)
    session, ticket, _ = published(served)
    f = artifact_follower(d, exported(tmp_path, session, ticket, bundle=None, bundle_digest=None))
    f.step()
    assert served.control.store.get(session).fields.get("artifact_acked") is True
    f.close()


def test_no_export_dir_acks_nothing(store, repo, served):  # noqa: F811
    d = Dispatch(store, repo, served)
    session, _, seq = published(served)
    f = artifact_follower(d, None)
    f.step()
    assert "artifact_acked" not in served.control.store.get(session).fields
    assert store.event_cursor()[1] == seq
    f.close()


@pytest.mark.parametrize("case", ["absent", "garbled", "partial", "other-session", "other-ticket", "bad-shape"])
def test_an_artifact_that_is_not_whole_is_passed_unacked_and_the_cursor_moves(store, repo, served, tmp_path, case):  # noqa: F811
    """A malformed or partial export is never acked: the retained work waits for the reaper's own fallback, and the
    stream moves on (a stalled follower would hold every later event)."""
    from levain.dispatch import export as X
    d = Dispatch(store, repo, served)
    session, ticket, seq = published(served)
    if case == "garbled":
        (tmp_path / session).mkdir()
        (tmp_path / session / X.FILE).write_text("{not json")
    elif case == "partial":
        exported(tmp_path, session, ticket)
        p = tmp_path / session / X.FILE
        p.write_text(p.read_text()[:40])
    elif case == "other-session":
        exported(tmp_path, session, ticket, named="s-" + "f" * 16)
    elif case == "other-ticket":
        exported(tmp_path, session, "another-" + ticket)
    elif case == "bad-shape":
        exported(tmp_path, session, ticket, bundle=None)          # a null bundle that still names a digest
    f = artifact_follower(d, tmp_path)
    assert f.step().outcome == CC.DEADLINE
    assert "artifact_acked" not in served.control.store.get(session).fields
    assert store.event_cursor()[1] == seq
    f.close()


def test_a_failed_artifact_is_passed_unacked(store, repo, served, tmp_path):  # noqa: F811
    """The export holds nothing for `artifact_failed`; dispatch took nothing, so nothing is acked."""
    d = Dispatch(store, repo, served)
    session, _, seq = published(served, artifact_failed="adopt-space-exhausted")
    f = artifact_follower(d, tmp_path)
    f.step()
    assert "artifact_acked" not in served.control.store.get(session).fields
    assert store.event_cursor()[1] == seq
    f.close()


@pytest.mark.parametrize("where", ["acked"])
def test_a_crash_after_the_ack_re_delivers_and_converges(store, repo, served, tmp_path, where):  # noqa: F811
    d = Dispatch(store, repo, served)
    session, ticket, seq = published(served)
    export = exported(tmp_path, session, ticket)
    d.faults[where] = True
    f = artifact_follower(d, export)
    with pytest.raises(Crash):
        f.step()
    assert served.control.store.get(session).fields.get("artifact_acked") is True
    cur = store.event_cursor()
    assert cur is None or cur[1] < seq
    f.close()
    f2 = artifact_follower(d, export)
    f2.step()
    assert store.event_cursor()[1] == seq
    assert served.control.store.get(session).fields.get("artifact_acked") is True
    f2.close()


def test_an_ack_control_refuses_stops_the_follower_by_name_with_the_cursor_unmoved(store, repo, served, tmp_path):  # noqa: F811
    """CONTROL's record carries another digest than the event the follower read: `artifact-not-published`."""
    d = Dispatch(store, repo, served)
    session, ticket, seq = published(served)
    export = exported(tmp_path, session, ticket)
    cas(served, R.RETAINED, R.RETAINED, session, artifact_digest="sha256:" + "e" * 64)
    f = artifact_follower(d, export)
    with pytest.raises(F.FollowStopped) as e:
        f.step()
    assert e.value.name == "artifact-not-published"
    assert "artifact_acked" not in served.control.store.get(session).fields
    f.close()


# -- F1: dispatch TAKES the artifact before the ack --------------------------------------------------------------------

def held(store, session, ticket):  # noqa: F811
    return store.taken_artifact(session, ticket)


def test_the_bundle_is_taken_before_the_ack_and_outlives_the_export(store, repo, served, tmp_path):  # noqa: F811
    """The data-loss case: the ACK lets the reaper trash the retained session and the export with it. After the ack
    and the trash, dispatch still holds the verified bundle."""
    d = Dispatch(store, repo, served)
    session, ticket, seq = published(served)
    export = exported(tmp_path, session, ticket)
    f = artifact_follower(d, export)
    assert f.step().outcome == CC.DEADLINE
    assert served.control.store.get(session).fields.get("artifact_acked") is True
    shutil.rmtree(export / session)                              # the reaper's trash
    t = held(store, session, ticket)
    assert t is not None and t.bundle_digest == BUNDLE_DIGEST
    data = (store.directory / t.path).read_bytes()
    assert data == BUNDLE and "sha256:" + hashlib.sha256(data).hexdigest() == t.bundle_digest
    assert (t.files, t.bytes) == (2, 64) and t.manifest_digest == "sha256:" + "c" * 64
    f.close()


def test_the_take_comes_before_the_ack(store, repo, served, tmp_path):  # noqa: F811
    d = Dispatch(store, repo, served)
    session, ticket, _ = published(served)
    order = []
    d.faults.clear()
    f = artifact_follower(d, exported(tmp_path, session, ticket))
    f._fault = lambda where, seq: order.append((where, held(store, session, ticket) is not None,
                                                served.control.store.get(session).fields.get("artifact_acked")))
    f.step()
    assert ("taken", True, None) in order and order.index(("taken", True, None)) < order.index(("acked", True, True))
    f.close()


def test_a_crash_between_the_take_and_the_ack_re_delivers_and_converges(store, repo, served, tmp_path):  # noqa: F811
    d = Dispatch(store, repo, served)
    session, ticket, seq = published(served)
    export = exported(tmp_path, session, ticket)
    d.faults["taken"] = True
    f = artifact_follower(d, export)
    with pytest.raises(Crash):
        f.step()
    assert "artifact_acked" not in served.control.store.get(session).fields
    first = held(store, session, ticket)
    ino = os.stat(store.directory / first.path).st_ino
    f.close()
    d.faults.clear()
    f2 = artifact_follower(d, export)
    f2.step()
    assert served.control.store.get(session).fields.get("artifact_acked") is True
    assert held(store, session, ticket) == first
    assert os.stat(store.directory / first.path).st_ino == ino          # the repeat took nothing again
    assert store.event_cursor()[1] == seq
    f2.close()


def test_a_bundle_whose_bytes_are_not_its_digest_stops_the_follower_and_holds_nothing(store, repo, served, tmp_path):  # noqa: F811
    d = Dispatch(store, repo, served)
    session, ticket, _ = published(served)
    export = exported(tmp_path, session, ticket)
    (export / session / "bundle").write_bytes(BUNDLE + b"x")
    f = artifact_follower(d, export)
    with pytest.raises(F.FollowStopped) as e:
        f.step()
    assert e.value.name == "take-digest-mismatch"
    assert "artifact_acked" not in served.control.store.get(session).fields
    assert held(store, session, ticket) is None
    cur = store.event_cursor()
    assert cur is None or cur[1] < served.kept()[-1]["seq"]
    taken = store.directory / "taken"
    assert not taken.exists() or not [p for p in taken.rglob("*") if p.is_file()]
    f.close()


def test_a_bundle_the_artifact_names_but_the_export_lacks_stops_the_follower(store, repo, served, tmp_path):  # noqa: F811
    d = Dispatch(store, repo, served)
    session, ticket, _ = published(served)
    export = exported(tmp_path, session, ticket)
    (export / session / "bundle").unlink()
    f = artifact_follower(d, export)
    with pytest.raises(F.FollowStopped) as e:
        f.step()
    assert e.value.name == "take-bundle-absent"
    assert "artifact_acked" not in served.control.store.get(session).fields
    f.close()


def test_a_copy_that_fails_stops_the_follower_unacked_and_leaves_no_partial(store, repo, served, tmp_path, monkeypatch):  # noqa: F811
    from levain.dispatch import take as K
    d = Dispatch(store, repo, served)
    session, ticket, seq = published(served)
    export = exported(tmp_path, session, ticket)

    def full(fd):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(K.os, "fsync", full)
    f = artifact_follower(d, export)
    with pytest.raises(F.FollowStopped) as e:
        f.step()
    assert e.value.name == "take-failed"
    assert "artifact_acked" not in served.control.store.get(session).fields
    assert held(store, session, ticket) is None
    assert not [p for p in (store.directory / "taken").rglob("*") if p.is_file()]
    cur = store.event_cursor()
    assert cur is None or cur[1] < seq
    f.close()
    monkeypatch.undo()
    f2 = artifact_follower(d, export)                                  # the disk freed: the same event converges
    f2.step()
    assert served.control.store.get(session).fields.get("artifact_acked") is True
    assert held(store, session, ticket) is not None
    f2.close()


def test_a_second_artifact_of_the_same_ticket_with_other_digests_stops_the_follower(store, repo, served, tmp_path):  # noqa: F811
    d = Dispatch(store, repo, served)
    session, ticket, seq = published(served)
    export = exported(tmp_path, session, ticket)
    f = artifact_follower(d, export)
    f.step()
    f.close()
    first = held(store, session, ticket)
    exported(tmp_path, session, ticket, content=b"another bundle")
    store.drop_event_cursor()
    f2 = artifact_follower(d, export)
    with pytest.raises(F.FollowStopped) as e:
        f2.step()
    assert e.value.name == "take-conflict"
    assert held(store, session, ticket) == first
    f2.close()


def test_a_taken_bundle_whose_file_is_gone_is_taken_again_from_a_live_export(store, repo, served, tmp_path):  # noqa: F811
    d = Dispatch(store, repo, served)
    session, ticket, _ = published(served)
    export = exported(tmp_path, session, ticket)
    f = artifact_follower(d, export)
    f.step()
    f.close()
    t = held(store, session, ticket)
    (store.directory / t.path).unlink()
    store.drop_event_cursor()
    f2 = artifact_follower(d, export)
    f2.step()
    assert (store.directory / t.path).read_bytes() == BUNDLE
    f2.close()


def test_a_taken_bundle_whose_file_and_export_are_gone_is_passed_unacked(store, repo, served, tmp_path):  # noqa: F811
    """The row alone is not the artifact, and an absent export is passed like any other (CONTROL was acked once)."""
    d = Dispatch(store, repo, served)
    session, ticket, _ = published(served)
    export = exported(tmp_path, session, ticket)
    f = artifact_follower(d, export)
    f.step()
    f.close()
    t = held(store, session, ticket)
    (store.directory / t.path).unlink()
    shutil.rmtree(export / session)
    store.drop_event_cursor()
    f2 = artifact_follower(d, export)
    f2.step()
    assert held(store, session, ticket) == t
    f2.close()


def test_an_artifact_with_no_bundle_is_taken_as_a_record_with_no_file(store, repo, served, tmp_path):  # noqa: F811
    d = Dispatch(store, repo, served)
    session, ticket, _ = published(served)
    f = artifact_follower(d, exported(tmp_path, session, ticket, bundle=None, bundle_digest=None))
    f.step()
    t = held(store, session, ticket)
    assert t is not None and t.path is None and t.bundle_digest is None
    assert served.control.store.get(session).fields.get("artifact_acked") is True
    f.close()


def test_the_taken_bundle_is_private_to_dispatch(store, repo, served, tmp_path):  # noqa: F811
    d = Dispatch(store, repo, served)
    session, ticket, _ = published(served)
    f = artifact_follower(d, exported(tmp_path, session, ticket))
    f.step()
    t = held(store, session, ticket)
    p = store.directory / t.path
    assert p.stat().st_mode & 0o777 == 0o600
    assert (store.directory / "taken").stat().st_mode & 0o077 == 0 and p.parent.stat().st_mode & 0o077 == 0
    f.close()
