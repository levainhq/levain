"""levain.coordinator.first_reader: the coordinator reads the seats' questions first (C5; BUILD_PLAN gap 7).

RULED (gap 7, *"all recs"*): *"the coordinator is FIRST READER of seat questions (``levain ask``), its answers are KNOWs
labelled as its own (an answer never authorises)"*; and the C5 row: it *"answers inside the envelope as KNOWs labelled as
its own; escalates the rest"*.

What that means here:

- **Reading** (:func:`read_band`): every open question in the band (Q17, ``questions.db``) the coordinator has not read
  yet, each with the one mechanical verdict below.
- **Its answer is a KNOW, never the operator's answer** (:func:`answer`). It is written in the coordinator's own store
  (``dispatch.db``): a reply of kind ``know`` pointing at the question, its review item (feed ``know``) and a
  ``first_reads`` row, in ONE fenced transaction. Nothing is written to ``questions.db``: the operator's one answer slot
  stays his, the question stays OPEN, and an answer he records later reaches the seat too and is the one that counts.
  C5's first run showed why: the only path a coordinator
  had was ``levain question answer``, which recorded its pick under the operator's channel (``via cli``) and refused
  the operator's own answer after it.
- **The seat gets it labelled** (:func:`claim_knows`, read by the band's hook path,
  :func:`levain.consent.band.question_context`): once, as the coordinator's own reading, while the question is still
  open.
- **Escalating** (:func:`escalate`): an ANSWER record (the grammar's push, C0 §3.3) pointing at the question, which then
  shows in "waiting on you"; the question stays open for the operator.

**The verdict** (:func:`verdict`) is necessary, never sufficient. The coordinator may answer only when ALL hold, checked
again inside the transaction that writes the KNOW:

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

Known edges (one line each): a KNOW reaches a live session at its next tool call only (an ENDED session is not resumed
for it); a question filed before a coordinator reads it was already pinged to you at filing (``levain ask`` pings, as
before C5); a session Levain did not start (yours, and today every seat: :mod:`levain.coordinator.launch` starts
coordinator launches only) always escalates; while a coordinator store exists, each hook reads ``questions.db`` once
more for this session's open questions.
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from levain.autonomic.durable_sqlite import read_transaction, write_transaction
from levain.coordinator.store import CoordError, CoordStore, _append_only

__all__ = ["FIRST_READER_SCHEMA", "Reading", "answer", "claim_knows", "escalate", "is_coordinator_session",
           "read_band", "verdict"]

# the question band names the agent; a launch names the harness
_HARNESS_OF = {"claude_code": "claude", "codex": "codex"}
_TEXT_MAX = 4000
_WHY_MAX = 1000

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
    try:
        real = os.path.realpath(cwd)
    except (OSError, ValueError):
        return False
    return any(real == w["repo"] or real.startswith(w["repo"].rstrip("/") + "/") for w in where)


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


def answer(store: Any, ref: Any, *, option: str | None = None, text: str | None = None, token: object,
           authority: Any) -> tuple[Any, str]:
    """The coordinator's own answer to question ``ref``: a KNOW (reply + review item + first read) in ONE transaction
    fenced by the lease ``token``, refused unless :func:`_verdict` holds inside it. Returns ``(item, reply_id)``.
    ``questions.db`` is never written: the question stays open for the operator."""
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
            raise CoordError(f"question #{q.n} was already read first ({done[0]})")
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
        reply_id = store._reply(conn, got.thread_id, "know", "questions.db", q.id, body, now)
        item, _new = _insert(conn, now, "know", {"text": body["text"], "reply_id": reply_id, "question_id": q.id,
                                                 "what": q.what, "envelope_id": got.envelope_id},
                             dedupe_key=f"know:question:{q.id}", thread_id=got.thread_id, authority_relevant=True,
                             by="coordinator")
        conn.execute("INSERT INTO first_reads (question_id, outcome, reply_id, at) VALUES (?, 'know', ?, ?)",
                     (q.id, reply_id, now))
        return item, reply_id


def escalate(store: Any, ref: Any, why: str, *, token: object) -> str:
    """Leave question ``ref`` to the operator: an ANSWER record pointing at it (the grammar's push, C0 §3.3), and its
    first read, in one fenced transaction. Returns the reply id. The question is not touched."""
    from levain.coordinator import lease

    _text("why", why, _WHY_MAX)
    q = _questions(store.directory).get(ref)
    if q is None:
        raise CoordError(f"no question {str(ref)[:40]}")
    with store._write("coordinator", token) as (conn, now):
        done = _read(conn, q.id)
        if done is not None:
            raise CoordError(f"question #{q.n} was already read first ({done[0]})")
        ln = lease.by_harness_session(conn, q.agent_session_id)
        thread_id = ln.threads[0] if ln is not None and len(ln.threads) == 1 else None
        same = conn.execute("SELECT reply_id FROM replies WHERE kind = 'answer' AND ref_store = 'questions.db' AND "
                            "ref_id = ?", (q.id,)).fetchone()
        reply_id = same[0] if same is not None else store._reply(
            conn, thread_id, "answer", "questions.db", q.id,
            {"text": f"question #{q.n}: {q.what}"[:_TEXT_MAX], "why": why, "by": "coordinator"}, now)
        conn.execute("INSERT INTO first_reads (question_id, outcome, reply_id, at) VALUES (?, 'escalated', ?, ?)",
                     (q.id, reply_id, now))
        return reply_id


# --- the seat's side (the band's hook path) ------------------------------------------------------------------------
def claim_knows(directory: Path | str, agent: str, session: str) -> list[tuple[Any, dict[str, Any]]]:
    """The coordinator's KNOW answers to this session's still-OPEN questions not yet delivered, claimed (each reaches
    the session once): ``[(question, know body)]``. Cheap when there is nothing: no ``dispatch.db``, or no open question
    of this session, opens nothing for writing."""
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
    out: list[tuple[Any, dict[str, Any]]] = []
    # a plain write: a delivery is the service's record, and it moves no thread (no awake reconcile, no lease fence)
    with write_transaction(store._connect) as conn:
        now = store._stamp()
        for reply_id, question_id, body in conn.execute(sql, tuple(mine)).fetchall():
            conn.execute("INSERT INTO know_deliveries (reply_id, agent_session_id, at) VALUES (?, ?, ?)",
                         (reply_id, session, now))
            out.append((mine[question_id], json.loads(body)))
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
