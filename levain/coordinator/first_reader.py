"""levain.coordinator.first_reader: the coordinator reads the seats' questions first (C5; BUILD_PLAN gap 7).

RULED (gap 7, *"all recs"*): *"the coordinator is FIRST READER of seat questions (``levain ask``), its answers are KNOWs
labelled as its own (an answer never authorises)"*; and the C5 row: it *"answers inside the envelope as KNOWs labelled as
its own; escalates the rest"*.

What that means here:

- **Reading** (:func:`read_band`): every open question in the band (Q17, ``questions.db``) the coordinator has not read
  yet, each with the one mechanical verdict below.
- **Its answer is a KNOW, never the operator's answer** (:func:`answer`). It is written in the coordinator's own store
  (``dispatch.db``): a reply of kind ``know`` pointing at the question, its review item (feed ``know``) and a
  ``first_reads`` row, in ONE fenced transaction of ``dispatch.db``. Nothing is written to ``questions.db``: the
  operator's one answer slot stays his, the question stays OPEN, and an answer he records later reaches the seat too
  and is the one that counts.
  C5's first run showed why: the only path a coordinator
  had was ``levain question answer``, which recorded its pick under the operator's channel (``via cli``) and refused
  the operator's own answer after it.
- **The seat gets it labelled** (:func:`claim_knows`, read by the band's hook path,
  :func:`levain.consent.band.question_context`): once, as the coordinator's own reading, while the question is still
  open (re-read inside the delivery transaction; a KNOW is formatted before it is marked delivered).
- **Escalating** (:func:`escalate`): an ANSWER record (the grammar's push, C0 §3.3) pointing at the question, which then
  shows in "waiting on you"; the question stays open for the operator.
- **The held ping** (C5-1, Phill 10-22 *"4a"*: hold ``levain ask``'s ping while a coordinator holds a live lease; any
  question it has not read within 10 min pings him and lands in his review queue). :func:`hold_ping`, at filing:
  while a coordinator (an ``active`` coordinator launch, never the operator) holds the lease and it is live, the ping
  is held (a ``held_pings`` row) instead of sent; no store, no lease, a lapsed or expired lease, or any fault: the
  ping goes as before. :func:`claim_held` releases a held ping once (a ``held_ping_releases`` row): ``escalated`` when
  the coordinator escalated it, ``unread`` when it has no first read :data:`UNREAD_AFTER` after filing (that one also
  files the escalation's ANSWER record, so it shows in "waiting on you", and its first read as ``escalated``, so the
  coordinator's later ``coord answer`` or ``coord escalate`` on it is refused by name), ``answered`` (no ping) when the operator
  answered it meanwhile. A KNOW holds it for good: the KNOW is the review item. Nothing runs on a clock: the release is
  checked by :func:`levain.consent.band.release_held_pings`, which the gate hook (every tool call of every session
  in the store's directory), ``levain coord watch``'s pass, ``levain ask`` and ``levain coord escalate`` call.

**The verdict** (:func:`verdict`) is necessary, never sufficient. The coordinator may answer only when ALL hold, read
again inside the fenced ``dispatch.db`` transaction that writes the KNOW (the question re-read from ``questions.db``,
the launch and envelope chain read on that transaction's own connection). The authority log is replayed and verified
just BEFORE that transaction, never inside it (one ``ssh-keygen`` per link this process has not verified, and every
other ``dispatch.db`` writer would wait on it); inside it only the log's stored head is compared with that snapshot's,
and a head that moved abandons the transaction and replays again outside it:

1. the question has no answer yet;
2. no option of it is tied to a consent request (``levain ask --would-push``): that one is the operator's ruling;
3. the asking session is an ``active`` worker launch Levain started (matched by the harness's own session id), of the
   question's harness, holding exactly one thread, with a pinned envelope (C0 §3.4);
4. that envelope (and the head of its supersede chain) is live for the thread, its signature re-verified against the
   authority log (C0 §4.5); with no authority snapshot nothing is live;
5. the question was asked from inside a repository the envelope's narrowed ``where`` names.

Whether the question's SUBJECT is inside the envelope is the coordinator's judgement and no code here reads it: the KNOW
is filed ``authority_relevant``, so it is cleared only by you or a fresh session you asked, never by the coordinator
reviewing itself (C0 §3.6). [judged by c5 lane, 10-22, against the C4-4 ruling "a parser informs the human, never
authorises".]

NOTHING HERE IS AUTHORITY. A KNOW is information; this module never imports the consent queue's ruling path, never
writes ``questions.db`` and never rules a request. Before S1 any process of your uid can hold the lease token and write
this file (C0 §2), so ``by = coordinator`` is a label. Stdlib only.

Known edges:

- the question and the authority log are other files, so their re-reads inside the ``dispatch.db`` transaction are
  not atomic with it: an operator answer or a revocation landing between that re-read and the commit still files a
  KNOW. Accepted, not locked across databases: a KNOW grants nothing, and every KNOW waits in the operator's review
  queue;
- delivery re-reads the question's openness inside its own transaction, which narrows but does not close the same
  window: an operator answer landing between that re-read and the delivery's commit still delivers the KNOW once,
  beside his answer (his is the one that counts);
- the in-lock authority check compares the stored head row only: a row rewritten BELOW an unchanged head row between
  the replay and the commit is not seen there (the next full replay and the anchor refuse it);
- a question's ``cwd`` and session are the asking process's own word (``levain ask`` runs as the agent), so "asked
  from inside the envelope" is a label, never proof;
- a KNOW whose line cannot be formatted stays undelivered (tried again at each hook) and waits in the review queue;
- a KNOW reaches a live session at its next tool call only (an ENDED session is not resumed for it);
- a question filed while no coordinator held the live lease was pinged to you at filing (as before C5-1), and a
  coordinator's later KNOW to it does not unsend that ping;
- a session Levain did not start (yours, and today every seat: :mod:`levain.coordinator.launch` starts coordinator
  launches only) always escalates;
- while a coordinator store exists, each hook reads ``questions.db`` once more for this session's open questions;
- a held ping is released only when something checks (above): with every session idle, no watch running and no
  ``levain`` command run, a question unread past 10 minutes waits, silent, until the next check;
- a released ping is claimed before it is sent: a notify that fails after the claim is not sent again (an ``unread``
  one still shows in "waiting on you");
- a question tied to a consent request (``--would-push``) is never held: only the operator rules it.
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from levain.autonomic.durable_sqlite import read_transaction, write_transaction
from levain.coordinator.review import _REF_STORE
from levain.coordinator.store import CoordError, CoordStore, _append_only

__all__ = ["FIRST_READER_SCHEMA", "UNREAD_AFTER", "Reading", "answer", "claim_held", "claim_knows", "escalate",
           "hold_ping", "is_coordinator_session", "read_band", "verdict"]

# the question band names the agent; a launch names the harness
_HARNESS_OF = {"claude_code": "claude", "codex": "codex"}
_TEXT_MAX = 4000
_WHY_MAX = 1000
# ruled (Phill 10-22, "4a"): a held question the coordinator has not read within 10 min pings him
UNREAD_AFTER = timedelta(minutes=10)
RELEASE_CAUSES = ("unread", "escalated", "answered")

FIRST_READER_SCHEMA: tuple[str, ...] = (
    # one reading per question: the coordinator's KNOW or its escalation, written once
    "CREATE TABLE IF NOT EXISTS first_reads (question_id TEXT PRIMARY KEY, "
    "outcome TEXT NOT NULL CHECK (outcome IN ('know', 'escalated')), "
    "reply_id TEXT NOT NULL REFERENCES replies(reply_id), at TEXT NOT NULL)",
    *_append_only("first_reads", "a first read is written once"),
    "CREATE TRIGGER IF NOT EXISTS first_reads_no_replace BEFORE INSERT ON first_reads WHEN EXISTS (SELECT 1 FROM "
    "first_reads WHERE question_id = NEW.question_id OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a question is read first once'); END",
    # a KNOW answer delivered to its asking session: once
    "CREATE TABLE IF NOT EXISTS know_deliveries (reply_id TEXT PRIMARY KEY REFERENCES replies(reply_id), "
    "agent_session_id TEXT NOT NULL, at TEXT NOT NULL)",
    *_append_only("know_deliveries", "a KNOW is delivered once"),
    "CREATE TRIGGER IF NOT EXISTS know_deliveries_no_replace BEFORE INSERT ON know_deliveries WHEN EXISTS (SELECT 1 "
    "FROM know_deliveries WHERE reply_id = NEW.reply_id OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a KNOW is delivered once'); END",
    # C5-1: a question whose filing ping was held because a coordinator held the live lease (its token): once
    "CREATE TABLE IF NOT EXISTS held_pings (question_id TEXT PRIMARY KEY, lease_token INTEGER NOT NULL, "
    "at TEXT NOT NULL)",
    *_append_only("held_pings", "a held ping is written once"),
    "CREATE TRIGGER IF NOT EXISTS held_pings_no_replace BEFORE INSERT ON held_pings WHEN EXISTS (SELECT 1 FROM "
    "held_pings WHERE question_id = NEW.question_id OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a ping is held once'); END",
    # ... and its release: once
    "CREATE TABLE IF NOT EXISTS held_ping_releases (question_id TEXT PRIMARY KEY REFERENCES held_pings(question_id), "
    f"cause TEXT NOT NULL CHECK (cause IN ({', '.join(repr(c) for c in RELEASE_CAUSES)})), at TEXT NOT NULL)",
    *_append_only("held_ping_releases", "a held ping is released once"),
    "CREATE TRIGGER IF NOT EXISTS held_ping_releases_no_replace BEFORE INSERT ON held_ping_releases WHEN EXISTS "
    "(SELECT 1 FROM held_ping_releases WHERE question_id = NEW.question_id OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a held ping is released once'); END",
)


@dataclass(frozen=True)
class Reading:
    question: Any                 # levain.consent.questions.Question
    may_answer: bool
    why: str                      # inside which envelope, or why it escalates
    launch_id: str | None = None
    thread_id: str | None = None
    envelope_id: str | None = None


def _questions(directory: Path) -> Any:
    from levain.consent.questions import QuestionStore

    return QuestionStore(directory)


def _inside(cwd: str, where: Sequence[dict[str, Any]]) -> bool:
    """Whether ``cwd`` is, or is under, a repo ``where`` names; both sides by real path (a repo spelled through a link,
    or macOS's ``/var`` for ``/private/var``, still contains its own real path)."""
    try:
        real = os.path.realpath(cwd)
        repos = [os.path.realpath(w["repo"]) for w in where]
    except (OSError, ValueError):
        return False
    return any(real == r or real.startswith(r.rstrip("/") + "/") for r in repos)


def _verdict(store: Any, conn: sqlite3.Connection, now: str, q: Any, authority: Any) -> Reading:
    """The mechanical verdict for question ``q`` read inside ``conn`` (one transaction, one clock)."""
    from levain.coordinator import lease

    def no(why: str, **kw: Any) -> Reading:
        return Reading(q, False, why, **kw)

    if q.answer is not None:
        return no("the operator has answered it")
    if q.link is not None:
        return no(f"option {q.link.option} is tied to consent request {q.link.request_id}: that is the operator's "
                  "ruling, never the coordinator's")
    if q.open_ended:
        return no("an open question has no options to know: only the operator answers it, in words, so it is "
                  "escalated, never answered by the coordinator")
    ln = lease.by_harness_session(conn, q.agent_session_id)
    if ln is None:
        return no("the asking session is not one Levain started, so no envelope covers it")
    ids = {"launch_id": ln.launch_id}
    if ln.harness != _HARNESS_OF.get(q.agent):
        return no(f"launch {ln.launch_id} is a {ln.harness} session and the question names {q.agent}", **ids)
    if ln.state != "active":
        return no(f"launch {ln.launch_id} is {ln.state}, not active", **ids)
    if ln.role != "worker" or len(ln.threads) != 1:
        return no(f"launch {ln.launch_id} holds no single thread (role {ln.role}, {len(ln.threads)} threads)", **ids)
    ids["thread_id"] = ln.threads[0]
    if ln.pinned_envelope_id is None:
        return no(f"launch {ln.launch_id} was started with no envelope", **ids)
    ids["envelope_id"] = ln.pinned_envelope_id
    if not hasattr(store, "_chain_live_refusal"):
        return no("this store was opened without its envelopes, so nothing is live", **ids)
    if authority is None:
        return no("no authority log was named, so the envelope's signature is unchecked and nothing is live", **ids)
    refused = store._chain_live_refusal(conn, now, ln.pinned_envelope_id, ln.threads[0], authority)
    if refused is not None:
        return no(refused, **ids)
    where = store._effective(conn, ln.pinned_envelope_id, ln.threads[0])["where"]
    if not _inside(q.cwd, where):
        return no(f"the question was asked from {q.cwd[:200]}, outside every repo envelope "
                  f"{ln.pinned_envelope_id} names", **ids)
    return Reading(q, True, f"inside envelope {ln.pinned_envelope_id} for thread {ln.threads[0]}", **ids)


def _read(conn: sqlite3.Connection, question_id: str) -> tuple[str, str] | None:
    row = conn.execute("SELECT outcome, reply_id FROM first_reads WHERE question_id = ?", (question_id,)).fetchone()
    return None if row is None else (row[0], row[1])


def _already_read(q: Any, outcome: str) -> CoordError:
    """The refusal for a question read first already: an escalation (the coordinator's, or the held ping's 10-minute
    release) is named, since the question is the operator's from then on."""
    if outcome == "escalated":
        return CoordError(f"question #{q.n} was escalated to the operator (by the coordinator, or unread "
                          f"{int(UNREAD_AFTER.total_seconds() // 60)} minutes after it was filed): it was already read first, "
                          "and it is his now")
    return CoordError(f"question #{q.n} was already read first ({outcome})")


def verdict(store: Any, ref: Any, *, authority: Any = None) -> Reading:
    """The verdict for one question (its number or id), read now. Raises CoordError when there is no such question."""
    q = _questions(store.directory).get(ref)
    if q is None:
        raise CoordError(f"no question {str(ref)[:40]}")
    with read_transaction(store._connect) as conn:
        return _verdict(store, conn, store._stamp(), q, authority)


def read_band(store: Any, *, authority: Any = None) -> list[Reading]:
    """Every open question the coordinator has not read first yet, oldest first, each with its verdict."""
    qs = _questions(store.directory)
    if not qs.path.exists():
        return []
    open_qs = qs.open_questions()
    with read_transaction(store._connect) as conn:
        now = store._stamp()
        return [_verdict(store, conn, now, q, authority) for q in open_qs if _read(conn, q.id) is None]


def _text(name: str, value: Any, high: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > high:
        raise CoordError(f"{name} must be 1 to {high} characters of text")
    return value


class _HeadMoved(Exception):
    """The authority log's head moved between the snapshot and the lock: abandon the transaction, replay outside it."""


_HEAD_TRIES = 3


def answer(store: Any, ref: Any, *, option: str | None = None, text: str | None = None, token: object,
           authority: Any = None, authority_source: Callable[[], Any] | None = None,
           authority_head: Callable[[], tuple[int, str] | None] | None = None) -> tuple[Any, str]:
    """The coordinator's own answer to question ``ref``: a KNOW (reply + review item + first read) in ONE transaction
    fenced by the lease ``token``, refused unless :func:`_verdict` holds inside it, on the question re-read there.

    The authority: ``authority`` (a snapshot the caller took), or ``authority_source`` (a callable returning a verified
    snapshot, or None for none) called just BEFORE the transaction, never inside it. With ``authority_head`` too (a
    callable returning the log's stored ``(seq, hash)``, no replay), the head is compared inside the transaction; one
    that moved, or cannot be read, rolls the transaction back and the source is called again outside it, at most
    ``_HEAD_TRIES`` times, then the answer is refused. Returns ``(item, reply_id)``. ``questions.db`` is never written:
    the question stays open for the operator. The re-reads narrow the window, they do not close it (the module's known
    edges)."""
    if authority is not None and authority_source is not None:
        raise CoordError("give an authority snapshot or its source, not both")
    if authority_head is not None and authority_source is None:
        raise CoordError("a head check compares a snapshot its source took: give the source too")
    for _try in range(_HEAD_TRIES):
        snap = authority_source() if authority_source is not None else authority
        try:
            return _answer_once(store, ref, option, text, token, snap,
                                authority_head if snap is not None else None)
        except _HeadMoved:
            continue
    raise CoordError(f"the authority log kept moving while the coordinator answered ({_HEAD_TRIES} tries); nothing "
                     "was filed, answer again")


def _answer_once(store: Any, ref: Any, option: str | None, text: str | None, token: object, authority: Any,
                 authority_head: Callable[[], tuple[int, str] | None] | None) -> tuple[Any, str]:
    from levain.coordinator.review import _insert

    if (option is None) == (text is None):
        raise CoordError("the coordinator answers with exactly one of an option key or its own words")
    q = _questions(store.directory).get(ref)
    if q is None:
        raise CoordError(f"no question {str(ref)[:40]}")
    if option is not None and option not in dict(q.options):
        raise CoordError(f"question #{q.n} has no option {str(option)[:20]!r}")
    if text is not None:
        _text("the coordinator's answer", text, _TEXT_MAX)
    with store._write("coordinator", token) as (conn, now):
        done = _read(conn, q.id)
        if done is not None:
            raise _already_read(q, done[0])
        # read again, inside the fenced transaction: the question (its answer and consent link)
        fresh = _questions(store.directory).get(q.id)
        if fresh is None:
            raise CoordError(f"no question {q.id[:40]}")
        q = fresh
        if authority_head is not None:
            # the head only (no replay, no signature): the snapshot was verified before this lock was taken
            try:
                head = authority_head()
            except (sqlite3.Error, OSError):
                head = None
            if head != (authority.seq, authority.head_hash):
                raise _HeadMoved
        got = _verdict(store, conn, now, q, authority)
        if not got.may_answer:
            raise CoordError(f"escalate question #{q.n}: {got.why}")
        label = dict(q.options).get(option, "") if option is not None else None
        summary = (f"the coordinator answered question #{q.n} on its own reading: " +
                   (f"option {option} ({label})" if option is not None else f"in its own words: {text}"))
        body = {"by": "coordinator", "question_id": q.id, "question_n": q.n, "what": q.what,
                "kind": "option" if option is not None else "text", "value": option if option is not None else text,
                "launch_id": got.launch_id, "thread_id": got.thread_id, "envelope_id": got.envelope_id,
                "text": summary[:_TEXT_MAX]}
        reply_id = store._reply(conn, got.thread_id, "know", _REF_STORE["answer"], q.id, body, now)
        item, _new = _insert(conn, now, "know", {"text": body["text"], "reply_id": reply_id, "question_id": q.id,
                                                 "what": q.what, "envelope_id": got.envelope_id},
                             dedupe_key=f"know:question:{q.id}", thread_id=got.thread_id, authority_relevant=True,
                             by="coordinator")
        conn.execute("INSERT INTO first_reads (question_id, outcome, reply_id, at) VALUES (?, 'know', ?, ?)",
                     (q.id, reply_id, now))
        return item, reply_id


def escalate(store: Any, ref: Any, why: str, *, token: object) -> str:
    """Leave question ``ref`` to the operator: an ANSWER record pointing at it (the grammar's push, C0 §3.3), and its
    first read, in one fenced transaction, refused when the question (re-read inside it) is answered already. Returns
    the reply id. The question is not touched."""
    from levain.coordinator import lease

    _text("why", why, _WHY_MAX)
    q = _questions(store.directory).get(ref)
    if q is None:
        raise CoordError(f"no question {str(ref)[:40]}")
    with store._write("coordinator", token) as (conn, now):
        done = _read(conn, q.id)
        if done is not None:
            raise _already_read(q, done[0])
        # read again inside the fenced transaction: an answer the operator gave since needs no escalation
        fresh = _questions(store.directory).get(q.id)
        if fresh is None:
            raise CoordError(f"no question {q.id[:40]}")
        if fresh.answer is not None:
            raise CoordError(f"question #{q.n} was answered by the operator; there is nothing to escalate")
        ln = lease.by_harness_session(conn, q.agent_session_id)
        thread_id = ln.threads[0] if ln is not None and len(ln.threads) == 1 else None
        same = conn.execute("SELECT reply_id FROM replies WHERE kind = 'answer' AND ref_store = ? AND ref_id = ?",
                            (_REF_STORE["answer"], q.id)).fetchone()
        reply_id = same[0] if same is not None else store._reply(
            conn, thread_id, "answer", _REF_STORE["answer"], q.id,
            {"text": f"question #{q.n}: {q.what}"[:_TEXT_MAX], "why": why, "by": "coordinator"}, now)
        conn.execute("INSERT INTO first_reads (question_id, outcome, reply_id, at) VALUES (?, 'escalated', ?, ?)",
                     (q.id, reply_id, now))
        return reply_id


# --- the seat's side (the band's hook path) ------------------------------------------------------------------------
def claim_knows(directory: Path | str, agent: str, session: str,
                render: Callable[[Any, dict[str, Any]], str]) -> list[str]:
    """The coordinator's KNOW answers to this session's still-OPEN questions not yet delivered, each formatted by
    ``render(question, know body)`` and claimed (each reaches the session once): the lines, oldest first. Inside the
    delivery transaction the session's open questions are read again, and a KNOW is marked delivered only after
    ``render`` returned its line: one whose question was answered meanwhile, or that fails to format, is left
    unmarked. Cheap when there is nothing: no ``dispatch.db``, or no open question of this session, opens nothing for
    writing."""
    d = Path(directory)
    db = d / "dispatch.db"
    if not db.exists():
        return []
    qs = _questions(d)
    if not qs.path.exists():
        return []
    mine = {q.id: q for q in qs.open_questions(agent) if q.agent_session_id == session}
    if not mine:
        return []
    marks = ",".join("?" * len(mine))
    sql = ("SELECT r.reply_id, r.ref_id, r.body FROM first_reads f JOIN replies r ON r.reply_id = f.reply_id "
           f"WHERE f.outcome = 'know' AND r.kind = 'know' AND f.question_id IN ({marks}) AND NOT EXISTS "
           "(SELECT 1 FROM know_deliveries k WHERE k.reply_id = r.reply_id) ORDER BY r.rowid")
    try:
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
        try:
            if not conn.execute(sql, tuple(mine)).fetchall():
                return []
        finally:
            conn.close()
    except sqlite3.Error:        # no first_reads table yet (no coordinator has read anything): nothing to deliver
        return []
    store = CoordStore(d)
    out: list[str] = []
    # a plain write: a delivery is the service's record, and it moves no thread (no awake reconcile, no lease fence)
    with write_transaction(store._connect) as conn:
        now = store._stamp()
        still = {q.id: q for q in qs.open_questions(agent) if q.agent_session_id == session}
        for reply_id, question_id, body in conn.execute(sql, tuple(mine)).fetchall():
            if question_id not in still:          # answered since the read above: the operator's answer counts
                continue
            try:
                line = render(still[question_id], json.loads(body))
            except Exception:  # noqa: BLE001 - left unmarked, so it is tried again at the next hook, never lost unseen
                continue
            conn.execute("INSERT INTO know_deliveries (reply_id, agent_session_id, at) VALUES (?, ?, ?)",
                         (reply_id, session, now))
            out.append(line)
    return out


# --- the held ping (C5-1) --------------------------------------------------------------------------------------------
def _coord_store(directory: Path) -> CoordStore:
    """The store a held ping is written in (a seam: the tests' store runs on a fixed clock)."""
    return CoordStore(directory)


def _coordinator_token(conn: sqlite3.Connection, now: str) -> int | None:
    """The live lease's token when a coordinator holds it: live (``now`` before its until), not the operator's, its
    holder an ``active`` coordinator launch (the same holder rule as :func:`levain.coordinator.lease.fence`)."""
    from levain.coordinator import lease

    held = lease.lease(conn, now)
    if held is None or not held.live or held.holder == "operator":
        return None
    ln = lease._launch_by_key(conn, held.holder)
    return held.token if ln is not None and ln.state == "active" and ln.role == "coordinator" else None


def hold_ping(directory: Path | str, question_id: str) -> bool:
    """Hold question ``question_id``'s filing ping when a coordinator holds the live lease (checked inside the write
    that records the hold): True when held, so the caller does not ping. False, and the caller pings as before, when
    there is no coordinator store, no live coordinator lease, the question's ANSWER record already exists (the delivery
    pass won the race), or anything fails (a ping is never lost to a fault)."""
    d = Path(directory)
    if not (d / "dispatch.db").exists():
        return False
    try:
        store = _coord_store(d)
        with write_transaction(store._connect) as conn:
            now = store._stamp()
            token = _coordinator_token(conn, now)
            if token is None:
                return False
            if conn.execute("SELECT 1 FROM replies WHERE kind = 'answer' AND ref_store = ? AND ref_id = ?",
                            (_REF_STORE["answer"], question_id)).fetchone() is not None:
                return False        # the question's ANSWER is already filed: it waits on the operator, so his ping goes
            conn.execute("INSERT INTO held_pings (question_id, lease_token, at) VALUES (?, ?, ?)",
                         (question_id, token, now))
            return True
    except Exception:  # noqa: BLE001 - see the docstring: a fault pings
        return False


_DUE_SQL = ("SELECT h.question_id, h.at, f.outcome FROM held_pings h LEFT JOIN first_reads f ON "
            "f.question_id = h.question_id WHERE NOT EXISTS (SELECT 1 FROM held_ping_releases r WHERE "
            "r.question_id = h.question_id) AND (f.outcome IS NULL OR f.outcome = 'escalated') ORDER BY h.rowid")


def claim_held(directory: Path | str, *, after: timedelta | None = None) -> list[tuple[Any, str]]:
    """Release every held ping now due, once: ``[(question, cause)]`` for the caller to ping, cause ``escalated`` or
    ``unread`` (module docstring). An ``unread`` release also files the escalation's ANSWER record (once per question,
    as :func:`escalate` does) and its ``first_reads`` row (``escalated``), in the same transaction as the release. A question the operator answered meanwhile is
    released as ``answered`` and returned to nobody. Cheap when nothing waits: one read-only query."""
    from levain.cockpit.consent import parse_stamp
    from levain.coordinator import lease

    after = UNREAD_AFTER if after is None else after
    d = Path(directory)
    db = d / "dispatch.db"
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
        try:
            if not conn.execute(_DUE_SQL).fetchall():
                return []
        finally:
            conn.close()
    except sqlite3.Error:        # no held_pings table yet: nothing was ever held
        return []
    qs = _questions(d)
    store = _coord_store(d)
    out: list[tuple[Any, str]] = []
    with write_transaction(store._connect) as conn:
        now = store._stamp()
        for question_id, at, outcome in conn.execute(_DUE_SQL).fetchall():
            q = qs.get(question_id)
            if q is None or q.answer is not None:
                cause = "answered"
            elif outcome == "escalated":
                cause = "escalated"
            elif parse_stamp(now) - parse_stamp(at) >= after:
                cause = "unread"
            else:
                continue
            conn.execute("INSERT INTO held_ping_releases (question_id, cause, at) VALUES (?, ?, ?)",
                         (question_id, cause, now))
            if cause == "unread":
                # the release IS the question's first read (an escalation), so a later ``coord answer`` or
                # ``coord escalate`` on it is refused by name (L3 r3): the KNOW would contradict the push to him
                same = conn.execute("SELECT reply_id FROM replies WHERE kind = 'answer' AND ref_store = ? "
                                    "AND ref_id = ?", (_REF_STORE["answer"], q.id)).fetchone()
                if same is not None:
                    reply_id = same[0]
                else:
                    ln = lease.by_harness_session(conn, q.agent_session_id)
                    reply_id = store._reply(
                        conn, ln.threads[0] if ln is not None and len(ln.threads) == 1 else None, "answer",
                        _REF_STORE["answer"], q.id,
                        {"text": f"question #{q.n}: {q.what}"[:_TEXT_MAX], "by": "service",
                         "why": f"the coordinator had not read it {int(after.total_seconds() // 60)} minutes "
                                "after it was filed"}, now)
                conn.execute("INSERT INTO first_reads (question_id, outcome, reply_id, at) "
                             "VALUES (?, 'escalated', ?, ?)", (q.id, reply_id, now))
            if cause != "answered":
                out.append((q, cause))
    return out


def is_coordinator_session(directory: Path | str, session: str | None) -> bool:
    """True when ``session`` (a harness session id) is one Levain started as the coordinator. Read-only; False when
    there is no store or it cannot be read. A label check (before S1 nothing is a credential): it keeps the
    coordinator off the operator's answer path, ``levain question answer``."""
    if not session:
        return False
    db = Path(directory) / "dispatch.db"
    if not db.exists():
        return False
    try:
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute("SELECT 1 FROM launch_events e JOIN launches l ON l.launch_id = e.launch_id WHERE "
                               "e.kind = 'active' AND e.harness_session_id = ? AND l.role = 'coordinator' LIMIT 1",
                               (session,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return row is not None
