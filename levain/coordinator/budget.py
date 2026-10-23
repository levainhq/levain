"""levain.coordinator.budget — the thread budget GAUGE (C6; C0 §5 and T6).

RULED T6: *"Q13 per session stays the GATE, the thread budget is a GAUGE"*. C0 §5: crossing the budget stops NEW legs and
asks for a SIGN to extend; the stop time is the hard edge, the budget is not.

- **The budget** is the signed ``budget`` of the thread's CURRENT head envelope (the latest bound to it). An attenuation
  never touches it (C0 §4.4 narrows effects and the stop only). Raising it takes a new envelope that supersedes the
  head, signed by you through the one signer path (``levain envelope extend`` writes the body, ``levain envelope sign``
  signs it, :meth:`levain.coordinator.envelopes.EnvelopeStore.accept_envelope` verifies it). Nothing here writes a
  budget, an envelope or a count: there is no coordinator-side extension to refuse, because there is no such path.
- **The meter** is DERIVED, never a second counter: Q13 already counts every tool call of every session at its gate
  (:mod:`levain.consent.limits`, ``limits.db``). A thread's usage is the sum of Q13's counts of every worker session
  Levain activated PINNED to an envelope bound to that thread (C0 §3.4: a pin needs a one-thread worker, and a worker
  holding several threads is refused when asked for and at its spawn claim and activation, so no worker runs
  unmetered: L3 r1 B1, :data:`levain.coordinator.lease.SEVERAL_THREADS`). Read with
  ``mode=ro``: this module never writes ``limits.db``, so the gate is exactly what it was. Mapping, envelope key -> Q13
  count: ``tool_calls`` -> ``calls``; ``tokens`` -> ``fresh_tokens``; ``hours`` -> ``active_hours`` (Q13's active time,
  not wall clock). ``dollars_cents`` is NOT METERED: Q13 counts no dollars (its F1d), so it never crosses and every view
  says so. [judged by c6 lane, 10-22, against limits.py's own KINDS; question C6-1 in the lane's RETURN]
- **Usage is the thread's total**, across its whole chain of envelopes: an extension states a new total, it does not
  refill. [judged by c6 lane; question C6-2]
- **Crossed** when a metered count is AT OR PAST its budget (a budget of 0 is crossed at once: the echo says "0
  allowed (none named: new steps stop at once)").
- **A leg** today is a worker launch for the thread. The gauge is read at its spawn claim, before anything starts
  (:meth:`levain.coordinator.store.CoordStore.launch_claim_spawn`), and again at activation, the pin's transaction and
  the authoritative check (:meth:`levain.coordinator.store.CoordStore.launch_activate`); at either, a crossed gauge
  writes the launch ``failed`` and files the SIGN record in that same transaction, so no new worker starts. A dispatch job's ``-> running`` is the
  other leg the design names; its caller (the run supervisor, S1 + C-sessions) does not exist yet, and the envelope
  object's ``precondition`` there must call :func:`leg_refusal` (an interface owed, not built here).
- **The SIGN record** is a ``replies`` row of kind ``sign`` with ``ref_store`` :data:`SIGN_REF` and ``ref_id``
  ``<thread>/<head envelope>``, one per head (a second crossing of the same head returns the first). It is a record
  that something waits, never authority. It is open while that envelope is still the thread's head, not killed, its
  thread not done and its stop ahead (:func:`sign_open`); a superseding envelope closes it.

Known edges (one line each):
- a session whose agent never ran Q13's hook (no row in ``limits.db``) reads as 0 used: the view lists it as "not
  counted", never as within budget silently.
- legs started at the same moment each pass the gauge, so the budget can be overshot by one leg each (C0 §10).
- a running session is never stopped by the gauge: a crossing stops NEW legs; the session's own Q13 limits stop it.
- Q13 counts per session root, so a worker session reused for two threads would count twice (a pinned worker holds
  one thread, so Levain never does this).
- a session whose harness id differs from the root Q13 keys it under is not matched: it lands in ``uncounted`` (shown,
  not refusing), so its usage is not summed.
- ``limits.db`` that cannot be read (corrupt, or no ``sessions`` table yet) makes the gauge UNREADABLE: it refuses new
  legs and every view says why, never "crossed" with zero usage.

Stdlib only.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from levain.cockpit.consent import parse_stamp

__all__ = ["METERS", "NOT_METERED", "SIGN_REF", "Gauge", "file_sign", "gauge", "leg_refusal", "sign_open"]

# envelope budget key -> the Q13 count it reads (levain.consent.limits.KINDS)
METERS = {"tool_calls": "calls", "tokens": "fresh_tokens", "hours": "active_hours"}
NOT_METERED = ("dollars_cents",)
SIGN_REF = "budget"
# a launch's harness -> the agent name Q13 counts it under
_AGENT = {"claude": "claude_code", "codex": "codex"}
_SUBAGENT_MARK = "#agent:"      # levain.consent.limits.SUBAGENT_MARK: a session root drops a subagent's suffix


@dataclass(frozen=True)
class Gauge:
    """A thread's budget read against its usage now. ``envelope_id`` is the head whose budget is read; ``used`` holds
    the metered keys only; ``sessions`` the (agent, root) pairs summed and ``uncounted`` those Q13 holds no row for;
    ``crossed`` the metered keys at or past their budget; ``unreadable`` why ``limits.db`` could not be read (the
    gauge then refuses: a meter that cannot be read is never read as within budget)."""
    thread_id: str
    envelope_id: str
    budget: dict[str, int]
    used: dict[str, float]
    sessions: tuple[tuple[str, str], ...]
    uncounted: tuple[tuple[str, str], ...]
    crossed: tuple[str, ...]
    unreadable: str | None = None

    @property
    def refusal(self) -> str | None:
        """Why a NEW leg may not start under this gauge; None when it may."""
        if self.unreadable is not None:
            return (f"thread {self.thread_id}'s budget cannot be metered ({self.unreadable}); new steps stop until "
                    "the Q13 store reads")
        if not self.crossed:
            return None
        parts = ", ".join(f"{k} {_num(k, self.used[k])} of {self.budget[k]}" for k in self.crossed)
        return (f"thread {self.thread_id} has used its budget ({parts}): new steps stop; a SIGN extends it "
                f"(levain envelope extend {self.thread_id} --budget ..., then levain envelope sign)")

    def as_dict(self) -> dict[str, Any]:
        return {"thread_id": self.thread_id, "envelope_id": self.envelope_id, "budget": dict(self.budget),
                "used": dict(self.used), "not_metered": list(NOT_METERED),
                "sessions": [list(s) for s in self.sessions], "uncounted": [list(s) for s in self.uncounted],
                "crossed": list(self.crossed), "unreadable": self.unreadable, "refusal": self.refusal}


def _num(key: str, value: float) -> str:
    return f"{value:.2f}" if key == "hours" else str(int(value))


def _has(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None


def _head(conn: sqlite3.Connection, thread_id: str) -> tuple[str, dict[str, Any]] | None:
    row = conn.execute("SELECT e.envelope_id, e.body FROM envelope_binds b JOIN envelopes e ON e.envelope_id = "
                       "b.envelope_id WHERE b.thread_id = ? ORDER BY b.gseq DESC LIMIT 1", (thread_id,)).fetchone()
    return None if row is None else (row[0], json.loads(row[1]))


def _sessions(conn: sqlite3.Connection, thread_id: str) -> list[tuple[str, str]]:
    """Every (agent, session root) Levain activated as a one-thread worker pinned to an envelope bound to the thread."""
    rows = conn.execute(
        "SELECT l.harness, l.threads, e.harness_session_id FROM launches l JOIN launch_events e ON e.launch_id = "
        "l.launch_id WHERE e.kind = 'active' AND e.pinned_envelope_id IN (SELECT envelope_id FROM envelope_binds "
        "WHERE thread_id = ?) ORDER BY e.gseq", (thread_id,)).fetchall()
    out: list[tuple[str, str]] = []
    for harness, threads, hsid in rows:
        if json.loads(threads) != [thread_id] or not hsid or harness not in _AGENT:
            continue
        key = (_AGENT[harness], str(hsid).split(_SUBAGENT_MARK, 1)[0])
        if key not in out:
            out.append(key)
    return out


def _q13(queue_dir: Path, sessions: list[tuple[str, str]]) -> tuple[dict[str, float], list[tuple[str, str]]]:
    """Q13's counts summed over ``sessions`` (read-only), and the sessions it holds no row for. Raises
    ``sqlite3.Error`` / ``OSError`` when an existing store cannot be read."""
    used = {k: 0.0 for k in METERS}
    path = Path(queue_dir) / "limits.db"
    if not sessions:
        return used, []
    if not path.exists():
        return used, list(sessions)
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
    try:
        if not _has(conn, "sessions"):
            raise sqlite3.OperationalError(f"{path} has no sessions table (Q13 has not counted a call there yet)")
        uncounted = []
        for agent, root in sessions:
            row = conn.execute("SELECT calls, active_s, fresh FROM sessions WHERE agent = ? AND root = ?",
                               (agent, root)).fetchone()
            if row is None:
                uncounted.append((agent, root))
                continue
            used["tool_calls"] += int(row[0] or 0)
            used["hours"] += float(row[1] or 0.0) / 3600.0
            used["tokens"] += int(row[2] or 0)
        return used, uncounted
    finally:
        conn.close()


def gauge(conn: sqlite3.Connection, queue_dir: Path | str, thread_id: str) -> Gauge | None:
    """The thread's gauge read on ``conn`` (the caller's transaction on ``dispatch.db``), or None when no envelope is
    bound to it (a thread with no envelope has no budget: its sessions hold no envelope authority)."""
    if not _has(conn, "envelope_binds"):
        return None
    head = _head(conn, thread_id)
    if head is None:
        return None
    envelope_id, body = head
    budget = {k: int(v) for k, v in body["budget"].items()}
    sessions = _sessions(conn, thread_id)
    try:
        used, uncounted = _q13(Path(queue_dir), sessions)
    except (sqlite3.Error, OSError) as exc:
        return Gauge(thread_id, envelope_id, budget, {k: 0.0 for k in METERS}, tuple(sessions), (), (),
                     f"{type(exc).__name__}: {exc}"[:300])
    crossed = tuple(k for k in METERS if used[k] >= budget[k])
    return Gauge(thread_id, envelope_id, budget, used, tuple(sessions), tuple(uncounted), crossed)


def leg_refusal(conn: sqlite3.Connection, queue_dir: Path | str, thread_id: str) -> str | None:
    """Why a NEW leg for ``thread_id`` may not start, read in the caller's transaction; None when it may (or when the
    thread has no envelope, whose legs carry no envelope authority and so are not this gauge's)."""
    g = gauge(conn, queue_dir, thread_id)
    return None if g is None else g.refusal


def file_sign(store: Any, conn: sqlite3.Connection, now: str, g: Gauge) -> tuple[str, bool]:
    """The SIGN record for a crossed gauge, in the caller's write transaction: ``(reply_id, created)``. One per (thread,
    head envelope): the same crossing again returns the first. Written as the service: a record, never authority."""
    ref = f"{g.thread_id}/{g.envelope_id}"
    same = conn.execute("SELECT reply_id FROM replies WHERE kind = 'sign' AND ref_store = ? AND ref_id = ?",
                        (SIGN_REF, ref)).fetchone()
    if same is not None:
        return same[0], False
    body = {"text": (g.refusal or "")[:4000], "thread_id": g.thread_id, "envelope_id": g.envelope_id,
            "crossed": list(g.crossed), "used": {k: g.used[k] for k in METERS}, "budget": dict(g.budget),
            "by": "service"}
    return store._reply(conn, g.thread_id, "sign", SIGN_REF, ref, body, now), True


def sign_open(conn: sqlite3.Connection, ref_id: str, now: str) -> bool | None:
    """Is the SIGN to extend ``ref_id`` (``<thread>/<envelope>``) still waiting? True while that envelope is still the
    thread's head, not killed, its thread not done and its stop ahead; None when ``ref_id`` is not that shape."""
    thread_id, sep, envelope_id = str(ref_id).partition("/")
    if not sep or not _has(conn, "envelope_binds"):
        return None
    head = _head(conn, thread_id)
    if head is None or head[0] != envelope_id:
        return False
    rows = conn.execute("SELECT kind, value FROM envelope_attenuations WHERE envelope_id = ? AND (thread_id IS NULL "
                        "OR thread_id = ?)", (envelope_id, thread_id)).fetchall()
    if any(k in ("kill", "thread_done") for k, _v in rows):
        return False
    stops = [parse_stamp(head[1]["stop_at"])]
    for k, v in rows:
        if k == "stop_earlier":
            try:
                stops.append(parse_stamp(v))
            except Exception:  # noqa: BLE001 - a bad row is not a stop; the envelope's own stop still counts
                pass
    return parse_stamp(now) < min(stops)
