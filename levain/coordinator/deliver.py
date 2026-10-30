"""levain.coordinator.deliver: the delivery pass and the feed runner (C4-a; C0 §3.3 and §3.6).

The grammar's SIGN and ANSWER are records in the coordinator's store that point into the consent band (a request in
``queue.db``, a question in ``questions.db``). Nothing in the band files them, so a RECONCILE PASS does: it walks what is
open in the band now and files the pointer for each one that belongs to a coordinator thread. It reads the band through
its read-only readers (:func:`levain.consent.listing.read_listing`, :meth:`QuestionStore.open_questions`) and writes only
``dispatch.db``.

**Belongs to a thread** is the rule :func:`levain.coordinator.first_reader.verdict` uses to find a question's thread: the
asking session is a launch Levain started (matched by the harness's own session id), of the request's harness, holding
exactly one thread. A session Levain did not start, and a launch holding no single thread, file nothing here.

**Idempotent by id**: :func:`levain.coordinator.review.push_sign` and ``push_answer`` return the first record for a
request or question id and create nothing on a repeat, so a second pass files nothing. A record is never withdrawn:
whether it still WAITS is read from the band's own state by :func:`levain.coordinator.review.waiting_on_you`.

**An ANSWER waits for the first reader.** A question whose filing ping the coordinator holds (``held_pings`` with no
release) is the coordinator's to read first, and one it answered as a KNOW is not waiting on you: neither is pushed. The
held ping's own release files the ANSWER when the coordinator escalates or leaves it unread, so the pass never races it.

**The runner** (:func:`run`) is one call: every mechanical feed, then the delivery pass. HEAD RULING (1025+25): no
daemon for 1.0, so it runs on read: ``levain coord review`` runs it, and a panel's read path calls the same function.
``levain daemon install-feeds`` also runs it on a schedule, through :func:`run_exclusive`.

NOTHING HERE IS AUTHORITY: a record says something waits, never what he decides.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from levain.autonomic.durable_sqlite import read_transaction
from levain.cockpit.consent import parse_stamp
from levain.coordinator import review
from levain.coordinator.first_reader import _HARNESS_OF
from levain.coordinator.store import CoordError, CoordStore

__all__ = ["Delivery", "FEEDS_LOCK", "deliver", "faults", "pass_line", "run", "run_exclusive", "waiting_lines"]

_TEXT = 500


@dataclass
class Delivery:
    """What one pass did. ``signs`` and ``answers`` are ``(ref_id, reply_id, created)`` for each pointer the pass
    reached (``created`` False when the record already existed); ``errors`` one line per band or row it could not read
    or file (the pass goes on with the rest)."""
    signs: list[tuple[str, str, bool]] = field(default_factory=list)
    answers: list[tuple[str, str, bool]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def created(self) -> int:
        return sum(1 for *_x, new in (*self.signs, *self.answers) if new)

    def as_dict(self) -> dict[str, Any]:
        return {"signs": [list(s) for s in self.signs], "answers": [list(a) for a in self.answers],
                "created": self.created, "errors": list(self.errors)}


def _one_thread(conn: sqlite3.Connection, agent: str, session: str) -> str | None:
    """The thread of the launch Levain started as ``session`` (the harness's own id), or None."""
    from levain.coordinator import lease

    ln = lease.by_harness_session(conn, session)
    if ln is None or ln.harness != _HARNESS_OF.get(agent) or len(ln.threads) != 1:
        return None
    return ln.threads[0]


_ASKER_TAIL = " (the asker's rec; the coordinator filed none)"


def _asker_rec(q: Any) -> str:
    """The rec an ANSWER record the delivery pass files carries: the ASKER's, as the band shows it (``a: <label>``,
    the display letter and its label, never the stored key), marked as the asker's. Always a valid rec: never empty,
    never past :data:`review._REC_MAX` (a long label is cut, never the record refused), ``none`` when the question
    names no rec it has."""
    keys = [k for k, _ in q.options]
    if q.open_ended:
        head = "none: an open question, answered in words"
    elif q.rec in keys and keys.index(q.rec) < 6:
        i = keys.index(q.rec)
        head = f"{'abcdef'[i]}: {q.options[i][1]}"
    else:
        head = str(q.rec or "").strip() or "none"
    head = " ".join(head.split())
    room = review._REC_MAX - len(_ASKER_TAIL)
    if len(head) > room:
        head = head[:room - 1] + "…"
    return head + _ASKER_TAIL


def _has_store(store: CoordStore) -> bool:
    """The one check that a coordinator store exists here: the stamp opens (and so would make) it, so a pass on none
    reads nothing and records nothing."""
    return store.path.exists()


def deliver(store: CoordStore, *, queue_dir: Path | str | None = None) -> Delivery:
    """File the SIGN for each open consent request (pending or undecided) and the ANSWER for each open question that
    belongs to a coordinator thread. ``queue_dir`` defaults to the store's own directory, where ``queue.db`` and
    ``questions.db`` sit."""
    from levain.consent.listing import read_listing
    from levain.consent.questions import QuestionStore

    qdir = Path(queue_dir) if queue_dir is not None else store.directory
    out = Delivery()
    if not _has_store(store):
        out.errors.append("store: no coordinator store here; nothing was read or made")
        return out

    requests: list[Any] = []
    try:
        now: datetime | None = parse_stamp(store._stamp())
    except Exception as exc:  # noqa: BLE001 - named; the requests cannot be dated, the questions are still read
        now = None
        out.errors.append(f"stamp: {type(exc).__name__}: {exc}"[:300])
    if now is not None:
        try:
            listing = read_listing(qdir, now=now)
            requests = [e.request for e in (*listing.pending, *listing.undecided)]
            if listing.overflow:
                out.errors.append("requests: more are open than the listing reads, not all are pointed at")
            if listing.skipped:
                out.errors.append(f"requests: {listing.skipped} row(s) do not check out and were skipped")
        except Exception as exc:  # noqa: BLE001 - named under the band, the questions are still read
            out.errors.append(f"requests: {type(exc).__name__}: {exc}"[:300])
    questions: list[Any] = []
    try:
        qs = QuestionStore(qdir)
        questions = qs.open_questions() if qs.path.exists() else []
    except Exception as exc:  # noqa: BLE001
        out.errors.append(f"questions: {type(exc).__name__}: {exc}"[:300])

    todo: list[tuple[str, Any, str]] = []
    with read_transaction(store._connect) as conn:
        for r in requests:
            try:
                thread = _one_thread(conn, r.agent, r.agent_session_id)
            except Exception as exc:  # noqa: BLE001 - one row's failure never hides the others
                out.errors.append(f"sign {str(r.id)[:40]}: {type(exc).__name__}: {exc}"[:300])
                continue
            if thread is not None:
                todo.append(("sign", r, thread))
        for q in questions:
            try:
                thread = _one_thread(conn, q.agent, q.agent_session_id)
                if thread is not None and not review.first_reader_holds(conn, q.id):
                    todo.append(("answer", q, thread))
            except Exception as exc:  # noqa: BLE001
                out.errors.append(f"answer {str(q.id)[:40]}: {type(exc).__name__}: {exc}"[:300])

    for kind, row, thread in todo:
        try:
            if kind == "sign":
                text = f"{row.agent} asks to run {row.tool_name} in {row.cwd}: {row.display}"[:_TEXT]
                reply_id, new = review.push_sign(store, thread, row.id, text, rec=review.NO_REC)
                out.signs.append((row.id, reply_id, new))
            else:
                text = f"question #{row.n}: {row.what}"[:_TEXT]
                got = review.push_answer_unless_held(store, thread, row.id, text, rec=_asker_rec(row))
                if got is not None:             # None: the coordinator's hold landed first, which decides
                    out.answers.append((row.id, got[0], got[1]))
        except Exception as exc:  # noqa: BLE001 - one row's failure never hides the others
            out.errors.append(f"{kind} {str(row.id)[:40]}: {type(exc).__name__}: {exc}"[:300])
    return out


def run(store: CoordStore, *, queue_dir: Path | str | None = None) -> dict[str, Any]:
    """The feed runner: every mechanical feed (:func:`levain.coordinator.review.collect_feeds`), then the delivery pass.
    ``{"feeds": ..., "delivery": ...}``. Run again with nothing changed it files nothing: the feeds dedupe by key and
    the pointers by id. A run with no fault, on a store that existed, leaves one mark
    (:func:`levain.coordinator.review.record_pass`), so a display of when the pass last ran reads a healthy idle
    scheduler as running. The mark is append-only: a scheduled run adds one row each time (a known edge; pruning old
    marks is a later item if the growth is ever measured to matter)."""
    existed = _has_store(store)
    ran = {"feeds": review.collect_feeds(store, queue_dir=queue_dir),
           "delivery": deliver(store, queue_dir=queue_dir).as_dict()}
    if existed and not faults(ran):      # a pass that read nothing, or not all, is not "the last time"
        review.record_pass(store)
    return ran


FEEDS_LOCK = "feeds.lock"


def run_exclusive(store: CoordStore, *, queue_dir: Path | str | None = None) -> dict[str, Any] | None:
    """:func:`run` under a non-blocking exclusive lock on ``<store directory>/feeds.lock``, so a scheduled run and a
    manual one (or two schedules) never overlap. ``None`` when another run holds it: the caller skips, it does not wait.
    Only contention is a skip (EAGAIN, EWOULDBLOCK, EACCES); any other failure to open or lock raises
    :class:`CoordError` with its message, so a broken lock never reads as a quiet skip. The store directory is checked
    by the store's own rule (a real directory, this uid's, closed to others, never a symlink) BEFORE the lock file is
    made in it. The lock is the kernel's (``flock``) and dies with the process, so a killed run leaves nothing to
    clear. A host without ``fcntl`` runs unlocked: its feeds are idempotent by key, so an overlap files nothing twice."""
    try:
        import fcntl
    except ImportError:
        return run(store, queue_dir=queue_dir)
    import errno

    from levain.dispatch.jobs import DispatchError

    lock = Path(store.directory) / FEEDS_LOCK
    try:
        store._jobs._check_directory()
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except (OSError, DispatchError) as exc:
        raise CoordError(f"the feeds lock {lock} cannot be opened: {exc}") from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                return None
            raise CoordError(f"the feeds lock {lock} cannot be taken: {exc}") from exc
        return run(store, queue_dir=queue_dir)
    finally:
        os.close(fd)


def faults(ran: dict[str, Any]) -> list[str]:
    """One line per thing :func:`run` could not read: a feed that raised (a string under its name), a row a feed could
    not file (``errors``: ``{feed: [lines]}``), and each delivery error. Tolerant of the shape: a string under
    ``errors`` is one line, never iterated as a mapping."""
    feeds = ran.get("feeds") or {}
    out = [f"{k}: {v}" for k, v in feeds.items() if isinstance(v, str)]
    rows = feeds.get("errors")
    if isinstance(rows, dict):
        for feed, lines in rows.items():
            out += [f"{feed}: {line}" for line in ([lines] if isinstance(lines, str) else lines)]
    out += [f"delivery: {e}" for e in (ran.get("delivery") or {}).get("errors", [])]
    return out


def pass_line(ran_at: str | None) -> str:
    """When the delivery pass last ran: what "nothing waits" is worth is what a display says beside it."""
    return ("the delivery pass has not run yet here (levain coord review list runs it): what waits on you is not known"
            if ran_at is None else f"the delivery pass last ran {ran_at}")


def waiting_lines(waiting: dict[str, Any]) -> list[str]:
    """The "waiting on you" lines for the dict :func:`levain.coordinator.review.waiting_on_you` returns: the count, then
    each open record. Words only; the set is that function's, never recomputed here."""
    n = sum(len(v) for v in waiting["threads"].values()) + len(waiting["loose"])
    lines = [f"waiting on you: {n}" + (f" ({len(waiting['unresolved'])} unreadable)" if waiting["unresolved"] else "")]
    for t, recs in waiting["threads"].items():
        lines += [f"  [{t}] {r['kind'].upper()}: {str(r['text'])[:160]}" for r in recs]
    lines += [f"  {r['kind'].upper()}: {str(r['text'])[:160]}" for r in waiting["loose"]]
    return lines
