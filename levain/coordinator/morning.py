"""levain.coordinator.morning — away = UNDECIDED (T3, Q14), and the morning card's DATA (C6).

RULED T3: *"away = that thread waits UNDECIDED (Q14) and the morning card shows it"*; C0 §1: the coordinator may read
your word (*"closing my eyes"*) as away and set Q14 away ON, and *"it can never set away OFF; that is your surface's"*.

- **Away ON from your word** (:func:`away_from_drop`): the coordinator, holding the lease, names the drop it read as
  away; Levain sets away on through the operator's own path (:func:`levain.consent.away.set_away`, what
  ``levain consent away on`` runs), so the hook holds every gated call and a SIGN nobody answers goes UNDECIDED in the
  consent queue. A KNOW in the review queue says the coordinator did it and on which words.
- **Away OFF is never here.** Leaving a marked away takes a leave signed by an enrolled key (Q14,
  :meth:`levain.consent.queue.ConsentQueue.leave_away`). This module has no off, and no path that rules on an
  UNDECIDED request: a late ruling is ``levain consent rule``, signed by you.
- **The morning card** (:func:`card`): what waits for you after a night away, as data: each agent's away state, every
  UNDECIDED request with its gap and the thread it belongs to, every thread budget (crossed ones first) with any open
  SIGN to extend, what "waits on you" (C4), and the open review items. ``levain coord morning`` prints it. The card's
  VIEW on the cockpit and the phone is a later K lane: this is its data and a CLI view only.

Known edge: the lease is checked before away is set and again when the KNOW is filed; a lease lost in between leaves
away ON (your word, and it only raises holds) with the KNOW refused, and the call raises saying so.

THE CARD IS DISPLAY: it reads ``queue.db`` raw and read-only (accepted for display only, head ruling C4-4: a parser
informs the human, never authorises). Nothing here decides anything; every row names the command that does, which is
yours. Stdlib only.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from levain.autonomic.durable_sqlite import read_transaction
from levain.coordinator import budget, deliver, review
from levain.coordinator.store import CoordError, CoordStore

__all__ = ["AGENTS", "away_from_drop", "card", "render", "skipped_lines"]

AGENTS = ("claude_code", "codex")
_FIELD = 300


def away_from_drop(store: CoordStore, drop_id: str, *, token: object, agents: Sequence[str] = AGENTS) -> dict[str, Any]:
    """The coordinator read drop ``drop_id`` as your word that you are away: set Q14 away ON for each of ``agents``
    through the operator's own path, then file a KNOW saying so. Lease-fenced (``token``): refused with nothing written
    unless the caller holds the live lease. An agent whose consent settings are missing, or whose held calls nobody
    could answer (``set_away`` refuses it exactly as it refuses ``levain consent away on``), or whose settings cannot
    be read or written (an ``OSError``), is skipped and named with why; the KNOW says which agents were set away and
    which were not, never "every" when not every (L3 r1 A1). When every agent is skipped nothing is filed and
    :class:`CoordError` says why. Returns ``{"marked", "skipped", "item_id"}``. There is no away OFF here: leaving
    takes your signature."""
    from levain.consent import away as consent_away

    with store._write("coordinator", token) as (conn, _now):
        drop = store._must_drop(conn, drop_id)
        threads = store._threads_of(conn, drop_id)
    marked: list[str] = []
    skipped: dict[str, str] = {}
    for agent in agents:
        if agent not in AGENTS:
            raise CoordError(f"an agent is one of {', '.join(AGENTS)}")
        try:
            consent_away.set_away(Path(store.directory), agent, True)
            marked.append(agent)
        except (ValueError, OSError) as exc:
            skipped[agent] = f"{type(exc).__name__}: {exc}"[:_FIELD] if isinstance(exc, OSError) else str(exc)[:_FIELD]
    if not marked:
        raise CoordError("away was not set for any agent: " + "; ".join(f"{a}: {w}" for a, w in skipped.items()))
    words = drop.words if len(drop.words) <= 200 else drop.words[:200] + "..."
    text = (f"the coordinator read your drop {drop_id} ({words!r}) as away and set away ON for "
            f"{', '.join(marked)}: a SIGN from {'that agent' if len(marked) == 1 else 'those agents'} now waits "
            "UNDECIDED for your morning card. Only you can leave away (`levain consent away off --signing-key KEY`)."
            + skipped_lines(skipped))
    try:
        item, _reply = review.file_know(store, threads[0] if threads else None, text, by="coordinator", token=token)
    except CoordError as exc:
        raise CoordError(f"away was set ON for {', '.join(marked)}, but the KNOW telling him was not filed: {exc}") \
            from exc
    return {"marked": marked, "skipped": skipped, "item_id": item.item_id}


def skipped_lines(skipped: dict[str, str]) -> str:
    """What the KNOW and the CLI say about agents away was NOT set for: their SIGNs are not held as away."""
    return "".join(f" Away was NOT set for {a} ({w}): its SIGNs are not held as away." for a, w in skipped.items())


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)


def _away(qdir: Path) -> dict[str, Any]:
    """Each agent's latest away mark and whether a leave is STORED for it (display: the hook re-verifies a leave on
    every read, so a stored leave by a key revoked since does not end away for it)."""
    out: dict[str, Any] = {a: {"away": False, "mark": None, "since": None, "left_at": None} for a in AGENTS}
    path = qdir / "queue.db"
    if not path.exists():
        return out
    conn = _ro(path)
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'away_marks'").fetchone() is None:
            return out
        for agent in AGENTS:
            row = conn.execute("SELECT m.id, m.set_at, l.left_at FROM away_marks m LEFT JOIN away_leaves l ON "
                               "l.mark_id = m.id WHERE m.agent = ? ORDER BY m.rowid DESC LIMIT 1", (agent,)).fetchone()
            if row is not None:
                out[agent] = {"away": row[2] is None, "mark": row[0], "since": row[1], "left_at": row[2]}
        return out
    finally:
        conn.close()


def _undecided(qdir: Path, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Every UNDECIDED request with its gap, oldest first. ``conn``: a read-only ``queue.db`` connection the caller
    holds (its snapshot and its busy wait), else one of its own."""
    path = qdir / "queue.db"
    if conn is None and not path.exists():
        return []
    own = conn is None
    c = _ro(path) if conn is None else conn
    try:
        rows = c.execute("SELECT r.id, r.agent, r.agent_session_id, r.tool_name, r.display, r.cwd, r.tier, g.gap, "
                            "g.created_at FROM gaps g JOIN requests r ON r.id = g.request_id WHERE r.state = "
                            "'undecided' ORDER BY g.created_at, g.gap").fetchall()
    finally:
        if own:
            c.close()
    return [{"request_id": r[0], "agent": r[1], "session": r[2], "tool": r[3], "what": str(r[4])[:_FIELD],
             "cwd": str(r[5])[:_FIELD], "tier": r[6], "gap": r[7], "undecided_at": r[8], "rule": _rule_line(r[7], r[1], r[2])}
            for r in rows]


def _rule_line(gap: Any, agent: Any, session: Any) -> str:
    """The late-ruling command the card prints for the operator to copy, or words when a value the request carries
    (an agent could have written it) is not a plain token: such a value is never put in a line for his shell."""
    from levain.band_sources import UnsafeValue, consent_rule_command

    try:
        return consent_rule_command(gap, "allow|deny", agent, session)
    except UnsafeValue as bad:
        return (f"no command shown (the request's {bad.what} is not a plain token, so it is not put in a line for "
                "your shell): rule it from its card")


def _thread_of(conn: sqlite3.Connection, store: CoordStore, request_id: str, session: str) -> str | None:
    """The thread an UNDECIDED request belongs to: the SIGN record pointing at it, else the one thread of the worker
    launch whose harness session filed it."""
    from levain.coordinator import lease

    row = conn.execute("SELECT thread_id FROM replies WHERE kind = 'sign' AND ref_store = 'queue.db' AND ref_id = ? "
                       "AND thread_id IS NOT NULL ORDER BY rowid LIMIT 1", (request_id,)).fetchone()
    if row is not None:
        return row[0]
    ln = lease.by_harness_session(conn, session)
    return ln.threads[0] if ln is not None and ln.role == "worker" and len(ln.threads) == 1 else None


def _band(qdir: Path, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """The NEEDS YOU band as the card carries it: ``{"text", "shown", "error"}``. Showing the band numbers its items
    (a first show, as ``levain band`` does); a band that cannot be rendered is said, never an empty band. ``conn``:
    the card's own coordinator snapshot, so the band and "waiting on you" are read from the same one."""
    from levain import band_view
    from levain.consent.questions import QuestionStore

    try:
        r = band_view.render_full(QuestionStore(qdir), coord_conn=conn)
    except Exception as exc:  # noqa: BLE001 - named on the card
        return {"text": "", "shown": [], "error": f"{type(exc).__name__}: {exc}"[:_FIELD]}
    return {"text": r.text, "shown": r.shown, "error": None}


PRESENCE_WINDOW_SECS = 24 * 3600


def _presence(qdir: Path) -> dict[str, Any] | None:
    """What an agent did to his presence word: the standing word when a seat or the coordinator set it, and the refusals
    in the last :data:`PRESENCE_WINDOW_SECS` from the presence log. ``None`` when there is neither."""
    import time

    from levain.webpush import presence

    word = presence.read_word(qdir)
    now = time.time()
    setter = word if word is not None and "by" in word and not presence.is_expired(word, now) else None
    cutoff = now - PRESENCE_WINDOW_SECS
    recent = [r for r in presence.read_log(qdir) if r["ts"] >= cutoff and isinstance(r.get("by"), dict)]
    refused = sum(1 for r in recent if r["outcome"] == "refused")
    if setter is None and not refused:
        return None
    out: dict[str, Any] = {"word": None, "by": None, "said": None, "at": None, "agent_rows": len(recent), "refused": refused}
    if setter is not None:
        out.update(word=setter["word"], by=setter["by"], said=setter["said"], at=setter["ts"])
    return out


def card(store: CoordStore, *, queue_dir: Path | str | None = None, items: Sequence[review.Item] | None = None,
         review_total: int | None = None, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """The morning card's data (display only; see the module docstring). Every section of the coordinator's store is
    read inside ONE read transaction on one read-only connection: ``conn`` when the caller holds one (its response is
    then made from its snapshot), else the card's own. ``items`` is a review queue the caller has already read
    (``review.list_items(store, include_cleared=True)`` or the open ones): the card shows its open items and does not
    read the queue again. ``review_total`` is the queue's open count when ``items`` is only a page of it (the card
    then counts the queue, not the page)."""
    if conn is None:
        with read_transaction(store.open_readonly) as own:
            return card(store, queue_dir=queue_dir, items=items, review_total=review_total, conn=own)
    qdir = Path(queue_dir) if queue_dir is not None else Path(store.directory)
    errors: list[str] = []
    try:
        away = _away(qdir)
    except sqlite3.Error as exc:
        # never read as "not away": an unreadable store is shown as unknown
        away = {a: {"away": None} for a in AGENTS}
        errors.append(f"away: {exc}"[:_FIELD])
    # the one place an entry that is not a record becomes unknown: every surface reads the card, none re-checks it
    away = {a: s if isinstance(s, dict) else {"away": None} for a, s in away.items()}
    try:
        undecided = _undecided(qdir)
    except sqlite3.Error as exc:
        undecided = []
        errors.append(f"undecided: {exc}"[:_FIELD])
    budgets: list[dict[str, Any]] = []
    now = store._stamp()
    # one statement: a thread is open unless its latest state event says closed (as Thread.state reads it)
    open_threads = [r[0] for r in conn.execute(
        "SELECT t.thread_id FROM threads t WHERE COALESCE((SELECT e.value FROM thread_events e WHERE e.thread_id = "
        "t.thread_id AND e.field = 'state' ORDER BY e.seq DESC LIMIT 1), '') != 'closed' ORDER BY t.rowid")]
    for u in undecided:
        u["thread_id"] = _thread_of(conn, store, u["request_id"], u["session"])
    for t in open_threads:
        g = budget.gauge(conn, qdir, t)
        if g is None:
            continue
        row = g.as_dict()
        ref = f"{t}/{g.envelope_id}"
        sign = conn.execute("SELECT reply_id FROM replies WHERE kind = 'sign' AND ref_store = ? AND ref_id = ?",
                            (budget.SIGN_REF, ref)).fetchone()
        row["sign"] = None if sign is None else {"reply_id": sign[0], "open": budget.sign_open(conn, ref, now)}
        budgets.append(row)
    budgets.sort(key=lambda b: (not b["refusal"], b["thread_id"]))
    waiting = review.waiting_on_you(store, sign_open=review.queue_request_open(qdir),
                                    answer_open=review.question_open(qdir), conn=conn)
    try:
        queue = review.list_items(store, conn=conn) if items is None else [i for i in items if i.state == "open"]
    except (sqlite3.Error, CoordError) as exc:
        queue = []
        errors.append(f"review: {exc}"[:_FIELD])
    items_out = [{"item_id": i.item_id, "feed": i.feed, "thread_id": i.thread_id,
                  "authority_relevant": i.authority_relevant, "text": str(i.body.get("text", ""))[:_FIELD]}
                 for i in queue]
    band = _band(qdir, conn)
    try:
        pres = _presence(qdir)
    except Exception as exc:  # noqa: BLE001 - named on the card
        pres = None
        errors.append(f"presence: {type(exc).__name__}: {exc}"[:_FIELD])
    return {"at": now, "presence": pres, "away": away, "band": band, "any_away": any(a.get("away") for a in away.values()),
            "undecided": undecided, "budgets": budgets, "waiting": waiting, "delivery_ran": review.last_pass(conn), "review": items_out,
            "review_total": len(items_out) if review_total is None else review_total, "errors": errors}


def render(c: dict[str, Any]) -> str:
    """The card as lines for ``levain coord morning`` (a CLI view of the data; the panels are a K lane's)."""
    L = [f"morning card, {c['at']}"]
    b = c.get("band") or {"text": "", "error": "the card carries no band"}
    L.append(f"NEEDS YOU band: could not render ({b['error']})" if b["error"] else b["text"] or "NEEDS YOU · nothing open")
    pr = c.get("presence")
    if pr:
        import time

        what = "no word of an agent's stands" if pr["word"] is None else (
            f"`{pr['word']}` set by {'the coordinator ' if pr['by']['role'] == 'coordinator' else ''}"
            f"{pr['by']['agent']} {pr['by']['session']} at {time.strftime('%H:%M', time.localtime(pr['at']))} from your words: {pr['said']}")
        L.append(f"presence: {what}; {pr['agent_rows']} agent write(s) in 24 h, {pr['refused']} refused (levain ping log)")
    away = [a for a, s in c["away"].items() if s.get("away")]
    unknown = [a for a, s in c["away"].items() if s.get("away") is None]
    L.append("away: " + (f"ON for {', '.join(away)}: SIGNs wait UNDECIDED; leaving takes your signature "
                         "(levain consent away off --signing-key KEY)" if away else "off")
             + (f"; UNKNOWN for {', '.join(unknown)} (the store could not be read)" if unknown else ""))
    # a section card() could not read is said so, never printed as the empty list it fell back to
    unread = {e.split(":", 1)[0] for e in c["errors"]}
    L.append("undecided: could not read" if "undecided" in unread else f"undecided ({len(c['undecided'])}):")
    for u in c["undecided"]:
        L.append(f"  gap #{u['gap']} {u['agent']} {u['tool']}: {u['what']}"
                 + (f"  [thread {u['thread_id']}]" if u.get("thread_id") else "") + f"\n    rule it: {u['rule']}")
    L.append(f"thread budgets ({len(c['budgets'])}):")
    for b in c["budgets"]:
        if b["unreadable"]:
            # never "crossed" with zero usage: the meter could not be read (L3 r1 U1)
            L.append(f"  {b['thread_id']} ({b['envelope_id']}): UNREADABLE, new steps stop: {b['unreadable']}")
            continue
        used = ", ".join(f"{k} {budget._num(k, b['used'][k])}/{b['budget'][k]}" for k in budget.METERS)
        state = "CROSSED: new steps stop" if b["refusal"] else "within"
        L.append(f"  {b['thread_id']} ({b['envelope_id']}): {used}; dollars not metered; {state}")
        if b["uncounted"]:
            L.append(f"    not counted by Q13 yet: {', '.join(r for _a, r in b['uncounted'])}")
        if b["sign"] is not None and b["sign"]["open"]:
            L.append(f"    SIGN to extend waits: levain envelope extend {b['thread_id']} --budget K=N,... -o FILE, "
                     "then levain envelope sign FILE")
    w = c["waiting"]
    L += deliver.waiting_lines(w)
    L.append("  " + deliver.pass_line(c["delivery_ran"]))
    L.append("review queue: could not read" if "review" in unread
             else f"review queue: {c['review_total']} open item(s)")
    L += [f"  {i['feed']}{' (yours to clear)' if i['authority_relevant'] else ''}: {i['text'][:160]}"
          for i in c["review"]]
    L += [f"could not read: {e}" for e in c["errors"]]
    return "\n".join(L)
