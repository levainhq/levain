"""levain.dispatch.follow: the SESSION-EVENTS follower and its ARTIFACT-ACK, slice 1 (1025+30 ackscout DESIGN).

CONTROL is real: test_slot_3c8's verifier rig with the event journal on disk, served on a `ControlServer` socket; the
job store is real (test_dispatch_jobs' verifying job and git repo), and `JobStore.record_verification` is the real
writer. A crash is an exception raised at the follower's fault seam."""
from __future__ import annotations

import dataclasses
import logging
import os
import tempfile
from pathlib import Path

import pytest

from levain.dispatch import follow as F
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
    assert f.step() == CC.DEADLINE
    assert [r.run for r in store.run_records(d.job.id)] == [0]
    assert acked(served)
    newest = served.kept()[-1]["seq"]
    assert store.event_cursor() == (served.events.journal_id, newest)
    # a second connection resumes past the cursor: nothing is re-delivered
    d.resolved.clear()
    assert f.step() == CC.DEADLINE and d.resolved == []
    f.close()


def test_a_live_event_reaches_the_follower(store, repo, served):  # noqa: F811
    """The follower connects before the verifier's terminal move; the event arrives live on the open stream."""
    d = Dispatch(store, repo, served)
    assert served.submit_v(vitem(1))["ok"]
    ticket = ticket_of(served.vmint())
    d.bind(ticket)
    assert served.vattach(ticket)["ok"]
    f = d.follower()
    assert f.step() == CC.DEADLINE                                     # the replay so far: nothing terminal
    cas(served, R.ALLOCATED, R.RENDERING, SESSION_V)
    assert root(served, "REVOKE", session=SESSION_V)["ok"]
    for frm, to in END_CHAIN[:-1]:
        cas(served, frm, to, SESSION_V)
    cas(served, R.CREDENTIAL_PARKED, R.CREDENTIAL_PARKED, SESSION_V, verifier_report_digest=d.digest)
    cas(served, R.CREDENTIAL_PARKED, R.RETAINED, SESSION_V)
    assert f.step() == CC.DEADLINE
    assert [r.run for r in store.run_records(d.job.id)] == [0] and acked(served)
    f.close()


def test_a_refused_report_fails_the_job_and_acks_with_no_digest(store, repo, served):  # noqa: F811
    d = setup_verifier(store, repo, served, refused="verifier-failed:exit")
    f = d.follower()
    assert f.step() == CC.DEADLINE
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
    assert f.step() == CC.DEADLINE
    assert [r.run for r in store.run_records(d.job.id)] == [0] and acked(served)
    assert store.event_cursor() == (served.events.journal_id, served.kept()[-1]["seq"])
    f.close()


def test_another_journal_restarts_from_zero_and_converges(store, repo, served):  # noqa: F811
    d = setup_verifier(store, repo, served)
    store.set_event_cursor("j-another", served.kept()[-1]["seq"])
    f = d.follower()
    assert f.step() == "journal-changed" and store.event_cursor() is None
    assert f.step() == CC.DEADLINE
    assert [r.run for r in store.run_records(d.job.id)] == [0] and acked(served)
    f.close()


def test_a_reset_restarts_from_zero_and_converges(store, repo, served):  # noqa: F811
    d = setup_verifier(store, repo, served)
    store.set_event_cursor(served.events.journal_id, served.kept()[-1]["seq"] + 50)
    f = d.follower()
    assert f.step() == "reset" and store.event_cursor() is None
    assert f.step() == CC.DEADLINE
    assert [r.run for r in store.run_records(d.job.id)] == [0] and acked(served)
    f.close()


def test_a_gap_is_logged_and_the_follower_continues(store, repo, served, caplog):  # noqa: F811
    d = Dispatch(store, repo, served)
    first = served.kept("pool")
    served.clock.t += EV.POOL_KEEP_S + 10
    sync_free(served.control, egress=5)
    assert served.events.prune() == len(first)
    f = d.follower()
    with caplog.at_level(logging.WARNING, logger="levain.dispatch.follow"):
        assert f.step() == CC.DEADLINE
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
    assert f.step() == CC.DEADLINE
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
    """A run another verification outcome already failed: dispatch holds nothing from this event, so no ACK lets the
    reaper trash the retained work; the follower moves past it."""
    d = setup_verifier(store, repo, served)
    other = {"kind": "state", "session": SESSION_V, "ticket": d.ticket, "state": "RETAINED",
             "at": "2026-10-25T00:00:00.000000Z", "verifier_report_refused": "verifier-failed:exit"}
    store.record_verification(d.run, envelope=None, event=other, repo=str(repo[0]), tree=base_tree(repo),
                              model=model(0))
    f = d.follower()
    assert f.step() == CC.DEADLINE
    assert store.get(d.job.id).history[-1].evidence == "verifier-failed:exit"
    assert not acked(served)
    assert store.event_cursor() == (served.events.journal_id, served.kept()[-1]["seq"])
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
