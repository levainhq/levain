"""Dispatch's SESSION-EVENTS follower (CONTRACT §3.6) and its ARTIFACT-ACK sender (§3.5 Retention), slice 1.

:class:`EventFollower` reads CONTROL's event stream from the cursor the job store persists
(:meth:`levain.dispatch.jobs.JobStore.event_cursor`) and, per event, in this order: (1) dispatch's durable write,
(2) ``ARTIFACT-ACK``, (3) the cursor moved past the event in its own transaction. A crash between any two re-delivers
the event on the next connection; the write and the ACK are both idempotent, so the repeat converges. The cursor never
moves past an event whose write or ACK failed: :class:`FollowStopped` names why, and the follower stops there.

Slice 1 handles:

- a ``state`` event into a terminal session state (``jobs._SESSION_TERMINAL``) that carries a verifier report digest or
  refusal: :meth:`JobStore.record_verification` with the inputs the caller's ``resolve`` gives for it, then
  ``ARTIFACT-ACK {ticket, verifier_report_digest}`` (no digest for ``verifier_report_refused``). The ACK goes only
  when the store holds THIS event's outcome (written now, or :class:`levain.dispatch.jobs.AlreadyRecorded`); a run
  holding another report's record stops the follower (``record-conflict``); a job ended by another cause is passed
  WITHOUT an ACK, retention falling back to ``dispatch_retain_days``;
- an ``artifact`` event: passed WITHOUT an ACK. Nothing reads the exported artifact yet, and an ACK before that read
  would let the reaper trash work dispatch never took; retention falls back to ``dispatch_retain_days``;
- every other event (other states, ``pool``, a kind this module does not know): passed.

A hello naming another journal than the cursor's, or a ``reset``, drops the cursor: the next :meth:`step` replays from
0, and every event it re-delivers converges (the write and the ACK are idempotent). A ``gap`` is logged and returned
in the step's :class:`Stepped`, with the count of events it skipped: those were pruned before they were read, and are
lost. CONTROL answers ``SESSION-EVENTS`` and ``ARTIFACT-ACK`` to its dispatch peer
only (``levain.slot.control.PEER_VERBS``), so the follower runs as the ``_levain_dispatch`` uid.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from levain.dispatch import DispatchError
from levain.dispatch.jobs import (_SESSION_TERMINAL, AlreadyRecorded, JobStore, RecordConflict, TransitionRefused,
                                  report_of)
from levain.dispatch.verifier_report import VerifierRun
from levain.slot import Refused
from levain.slot import control_client as CC

log = logging.getLogger("levain.dispatch.follow")

_REPORT_FIELDS = ("verifier_report_digest", "verifier_report_refused")


class FollowStopped(DispatchError):
    """The follower stopped at an event it could not finish; ``name`` says why, the cursor is left before it."""

    def __init__(self, name: str, detail: str = "") -> None:
        super().__init__(f"{name}: {detail}" if detail else name)
        self.name = name


@dataclass(frozen=True)
class VerifierCall:
    """What :meth:`JobStore.record_verification` takes beside the event, for one verifier ticket."""
    run: VerifierRun
    envelope: bytes | None
    repo: str
    tree: str
    model: Mapping[str, str]


@dataclass(frozen=True)
class Stepped:
    """How one :meth:`EventFollower.step` ended: ``outcome`` is an :class:`levain.slot.control_client.EventStream`
    outcome, or ``"journal-changed"`` / ``"reset"`` after dropping the cursor; ``gaps`` the gap frames the step read,
    ``lost`` the events they skipped unread (pruned before the follower reached them)."""
    outcome: str
    gaps: tuple[CC.Gap, ...] = ()
    lost: int = 0


#: the terminal state event of a verifier ticket -> its inputs; None when dispatch minted no such verifier ticket
Resolve = Callable[[Mapping[str, Any]], "VerifierCall | None"]


class EventFollower:
    """``jobs`` holds the cursor and the records; ``stream(after_seq, journal_id)`` opens SESSION-EVENTS and
    ``connect()`` a ControlClient for the ACK; ``resolve`` maps a verifier's terminal event to its inputs. ``fault``
    is the crash seam: called with ``("written", seq)`` after (1) and ``("acked", seq)`` after (2)."""

    def __init__(self, jobs: JobStore, stream: Callable[[int, str | None], CC.EventStream],
                 connect: Callable[[], CC.ControlClient], resolve: Resolve, *,
                 fault: Callable[[str, int], None] | None = None) -> None:
        self.jobs = jobs
        self._stream = stream
        self._connect = connect
        self.resolve = resolve
        self._fault = fault
        self._ack_client: CC.ControlClient | None = None
        # the stream a step is reading, so close() from another thread ends it; once closed, no stream opens
        self._lock = threading.Lock()
        self._active: CC.EventStream | None = None
        self._closed = False

    @classmethod
    def at(cls, jobs: JobStore, socket_path: Path | str, resolve: Resolve, *, read_deadline: float | None = None,
           **kw: Any) -> "EventFollower":
        """A follower of the CONTROL socket at ``socket_path``."""
        return cls(jobs, lambda after, jid: CC.follow_events(socket_path, after, jid, read_deadline=read_deadline),
                   lambda: CC.ControlClient(socket_path), resolve, **kw)

    def close(self) -> None:
        """End the stream a step is reading (that step ends ``CANCELLED``) and the ACK connection; a step after
        close opens no stream. Idempotent."""
        with self._lock:
            self._closed = True
            stream, self._active = self._active, None
            client, self._ack_client = self._ack_client, None
        if stream is not None:
            stream.close()
        if client is not None:
            client.close()

    def step(self) -> Stepped:
        """One connection: replay and follow from the persisted cursor until the stream ends. Returns how it ended
        and the gaps it read (:class:`Stepped`); raises :class:`FollowStopped` at an event it could not finish."""
        cur = self.jobs.event_cursor()
        jid, after = cur if cur is not None else (None, 0)
        stream = self._stream(after, jid)
        with self._lock:
            if self._closed:
                stream.close()
                return Stepped(CC.CANCELLED)
            self._active = stream
        try:
            return self._read(stream, jid, after)
        finally:
            with self._lock:
                if self._active is stream:
                    self._active = None
            stream.close()

    def _read(self, stream: CC.EventStream, jid: str | None, after: int) -> Stepped:
        gaps: list[CC.Gap] = []
        lost, last = 0, after                          # last: the newest seq read or skipped so far
        for frame in stream:
            if isinstance(frame, CC.Hello):
                if frame.same_journal is False:
                    log.warning("CONTROL's event journal is %s, the cursor's %s: replaying from 0",
                                frame.journal_id, jid)
                    self.jobs.drop_event_cursor()
                    return Stepped("journal-changed", tuple(gaps), lost)
                jid = frame.journal_id
            elif isinstance(frame, CC.Gap):
                log.warning("events before seq %d were pruned unread; they are lost", frame.oldest_seq)
                gaps.append(frame)
                lost += max(0, frame.oldest_seq - last - 1)
                last = max(last, frame.oldest_seq - 1)
            elif isinstance(frame, CC.Reset):
                log.warning("the cursor %d is past the journal's newest seq %d: replaying from 0", after,
                            frame.newest_seq)
                self.jobs.drop_event_cursor()
                return Stepped("reset", tuple(gaps), lost)
            elif isinstance(frame, CC.StreamError):
                log.warning("CONTROL ended the event stream: %s", frame.error)
            elif isinstance(frame, CC.Event):
                assert jid is not None
                self._handle(frame)
                self.jobs.set_event_cursor(jid, frame.seq)
                last = frame.seq
        return Stepped(stream.outcome or CC.CLOSED, tuple(gaps), lost)

    # -- one event ------------------------------------------------------------------------------------------------

    def _handle(self, ev: CC.Event) -> None:
        body = ev.body
        if ev.kind == "state" and body.get("state") in _SESSION_TERMINAL and any(k in body for k in _REPORT_FIELDS):
            self._verifier_done(ev)
        elif ev.kind == "artifact":
            # slice 1: nothing reads the export yet, so nothing may be acked; the stream must still move on
            log.info("artifact event %d (session %s) passed unacked: no artifact reader", ev.seq, body.get("session"))

    def _verifier_done(self, ev: CC.Event) -> None:
        body = ev.body
        try:
            report_of(body)
        except DispatchError as e:
            raise FollowStopped("event-shape", f"event {ev.seq}: {e}") from None
        call = self.resolve(body)
        if call is None:
            raise FollowStopped("ticket-unresolved", f"event {ev.seq}: no verifier run dispatch minted holds this "
                                                     "ticket")
        try:
            self.jobs.record_verification(call.run, envelope=call.envelope, event=body, repo=call.repo,
                                          tree=call.tree, model=call.model)
        except AlreadyRecorded:
            pass                                       # an idempotent repeat: dispatch holds this outcome, so ACK
        except RecordConflict as e:
            raise FollowStopped("record-conflict", f"event {ev.seq}: {e}") from None
        except TransitionRefused as e:
            if self.jobs.get(call.run.job_id) is None:
                raise FollowStopped("job-unknown", f"event {ev.seq}: {e}") from None
            # the job ended by another cause: dispatch took nothing from this event, and an ACK would let the reaper
            # trash the retained work, so it is passed unacked (retention falls back to dispatch_retain_days)
            log.info("verifier event %d (ticket %s) passed unacked: %s", ev.seq, body.get("ticket"), e)
            return
        except DispatchError as e:
            raise FollowStopped("record-refused", f"event {ev.seq}: {e}") from None
        self._hit("written", ev.seq)
        args: dict[str, object] = {"ticket": body["ticket"]}
        if "verifier_report_digest" in body:
            args["verifier_report_digest"] = body["verifier_report_digest"]
        self._ack(ev.seq, args)
        self._hit("acked", ev.seq)

    def _ack(self, seq: int, args: Mapping[str, object]) -> None:
        try:
            if self._ack_client is None:
                self._ack_client = self._connect()
            self._ack_client.call("ARTIFACT-ACK", **args)
        except Refused as e:
            if e.name == "control-down":
                self._drop_ack()
            raise FollowStopped(e.name, f"ARTIFACT-ACK of event {seq}") from None

    def _drop_ack(self) -> None:
        """Close the ACK connection alone, so the next ACK connects again; the follower stays open (close() is the
        caller's permanent shutdown, never a transient control-down's)."""
        with self._lock:
            client, self._ack_client = self._ack_client, None
        if client is not None:
            client.close()

    def _hit(self, where: str, seq: int) -> None:
        if self._fault is not None:
            self._fault(where, seq)


__all__ = ["EventFollower", "FollowStopped", "Resolve", "Stepped", "VerifierCall"]
