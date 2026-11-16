"""levain.desk.would_have_acted: the night desk's fallback (ND-3c, board levain#13; night desk design 3.2, 4.10, 8.1).

An effect no live effect line covers is not run and not lost: it becomes a WOULD-HAVE-ACTED row (what verb and enum
arguments, why it was uncovered, the event, the order if any, the read-only diagnosis) and an OPEN PING, so the morning
list says what the order's vocabulary missed and he widens the order with one signature, or does not ("the gaps are
measured, not guessed"; 4.10: a vocabulary gap that is never shown re-cripples the orders).

WHAT COUNTS AS UNCOVERED (:data:`REASONS`, the only reasons a row carries): no order in force answers (revoked,
expired, stopped, never signed: ``no_order``); the order has no line whose enums cover the call (``no_line``); the line's
per-incident or per-night budget is spent (``budget_incident``, ``budget_night``); the mail's sender is not on any
covering line's ``known_from`` (``sender_not_listed``). :mod:`levain.desk.effects` decides which refusals those are and
calls :meth:`WouldHaveActed.record` for them alone, and only once the event the call names has been looked up and
passed the verb's own event checks (a made-up or stale event id writes nothing, whichever refusal came first).
Argument-shape refusals (an argument outside its enum or id shape), an unauthenticated or malformed event, a stale
event, a version mismatch, an unwired executor and every text check on a draft are NOT recorded: they are the noise an
attacker or a fault makes, and no widening of an order would cover them. Shape refusals never reach this module at all,
because the verbs validate every argument before the gate runs.

THE ROW IS IDEMPOTENT per (night, event, verb, reason, arguments), never per order: a repeat on the same night is one
row (``times`` counts it); the same gap on a later night is that night's own row, on that night's list. Rows are bounded
PER NIGHT (:data:`ROWS_PER_NIGHT_MAX`); past the bound a call that would need a new row is not written but COUNTED for
its night, every such call (repeats too) (:meth:`WouldHaveActed.overflow`, a line in
:meth:`WouldHaveActed.morning_lines`), so a full night is never silent and a flood on one night never hides the next
night's gaps. No row is ever deleted.

THE DIAGNOSIS is read-only text from an injected ``diagnose(verb, event_id)`` (ND-8's read-only forced-command words are
its real source; nothing here runs a process). It is fetched OUTSIDE any write transaction, and only when the row is not
yet held and the night is under its row bound (a full night runs no diagnosis), so a slow source never holds the
file's lock. It is UNTRUSTED TEXT (a server's output): stripped of every unshowable character, bounded to
:data:`DIAGNOSIS_MAX`, stored as such, returned under an ``untrusted`` key, and never an argument, recipient, path,
host, order id or title.

THE PING goes through the existing push path (:func:`levain.webpush.sender.send`, kind ``notify_urgent``, the same path
:mod:`levain.desk.events` pings by), the title fixed from enums, the body the fixed words, the enum fields and the
bounded diagnosis, and ``ref`` the row's stable ``notice_ref``. A push is CLAIMED in the same transaction that records
the call: only while the ping has reached no phone, fewer than :data:`PUSH_ATTEMPTS_MAX` pushes were tried, the row's
``next_due_at`` has come (:data:`PUSH_RETRY_AFTER` after the last push, so a caller racing a push still in flight sends
nothing), and the night has pushed fewer than :data:`PUSHES_PER_NIGHT_MAX` pings across all its rows. A row past the
night's push ceiling is still written and stays ``open``; the morning list shows it.

KNOWN EDGE: ND-6 (notices as state) owns the open ping: re-pushing until a tap, escalation, the ack. The row carries the
fields it will take over (``notice_ref``, ``acked_at``, ``last_push_at``, ``next_due_at``, ``ping_state``) and
:meth:`WouldHaveActed.open_pings` reads them; nothing here writes an ack or closes a ping, and a retry happens only when
the same call is refused again. Stdlib plus the repo's durable sqlite helper.
"""
from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from levain import unshowable
from levain.autonomic.durable_sqlite import open_durable, read_transaction, write_transaction
from levain.cockpit.consent import parse_stamp, utc_stamp

__all__ = ["DIAGNOSIS_MAX", "PING_TITLE", "PUSHES_PER_NIGHT_MAX", "PUSH_ATTEMPTS_MAX", "PUSH_RETRY_AFTER", "REASONS",
           "ROWS_PER_NIGHT_MAX", "WouldHaveActed", "bounded_text"]

#: reason code to the fixed sentence a row and a ping carry; the code is the only reason text that is ever stored
REASONS: Mapping[str, str] = {
    "no_order": "no standing order in force covered it",
    "no_line": "the order had no effect line covering it",
    "budget_incident": "the order's budget for the incident was spent",
    "budget_night": "the order's budget for the night was spent",
    "sender_not_listed": "the sender is on no covering line's list",
}
PING_TITLE = "Night desk: an effect had no cover"
DIAGNOSIS_MAX = 1000
_PING_DIAGNOSIS_MAX = 300
#: rows written per night; past it a call that would need a new row is counted in the night's overflow, never written
ROWS_PER_NIGHT_MAX = 500
#: pushes (first pushes and retries, every row together) per night
PUSHES_PER_NIGHT_MAX = 5
#: pushes per row
PUSH_ATTEMPTS_MAX = 3
#: the earliest a row's ping may be pushed again after its last push
PUSH_RETRY_AFTER = timedelta(minutes=10)
_VERBS = ("restart_service", "send_ack")
#: the fixed-shape fields a row may hold of its arguments (enum members only; send_ack has none: its recipient is derived)
_ARG_KEYS = ("host", "service")

SCHEMA: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS would_have_acted (row_id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "dedupe_key TEXT NOT NULL UNIQUE, night TEXT NOT NULL, verb TEXT NOT NULL, args TEXT NOT NULL, reason TEXT NOT NULL, "
    "event_id TEXT NOT NULL, order_id TEXT, diagnosis TEXT NOT NULL, "
    "ping_state TEXT NOT NULL CHECK (ping_state IN ('open', 'closed')), ping_pushed INTEGER NOT NULL, "
    "push_attempts INTEGER NOT NULL, times INTEGER NOT NULL, first_at TEXT NOT NULL, last_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS would_have_acted_night ON would_have_acted (night, row_id)",
    "CREATE TRIGGER IF NOT EXISTS would_have_acted_no_delete BEFORE DELETE ON would_have_acted "
    "BEGIN SELECT RAISE(ABORT, 'a would-have-acted row is never deleted'); END",
    "CREATE TABLE IF NOT EXISTS would_have_acted_nights (night TEXT PRIMARY KEY, pushes INTEGER NOT NULL, "
    "overflow INTEGER NOT NULL)",
)
#: the open-ping columns ND-6 takes over, added to a file made before them
_PING_COLUMNS = (("notice_ref", "TEXT"), ("acked_at", "TEXT"), ("last_push_at", "TEXT"), ("next_due_at", "TEXT"))

Pinger = Callable[..., Any]
Diagnose = Callable[[str, str], "str | None"]


def bounded_text(text: object, limit: int) -> str:
    """Untrusted text made safe to store and show: not a string is empty; every unshowable character (controls, bidi,
    zero-width, private use) is stripped; at most ``limit`` characters."""
    if not isinstance(text, str):
        return ""
    return unshowable.mark(text, fmt=lambda _cp: "", allow="\n")[:limit].strip()


def _key(night: str, event_id: str, verb: str, reason: str, args_text: str) -> str:
    framed = "".join(f"{len(p)}:{p}|" for p in (night, event_id, verb, reason, args_text))
    return hashlib.sha256(framed.encode("utf-8")).hexdigest()


def _args_text(args: Mapping[str, str]) -> str:
    return " ".join(f"{k}={args[k]}" for k in _ARG_KEYS if k in args)


class WouldHaveActed:
    """The would-have-acted file at ``path`` (its directory must exist). ``queue_dir`` is the push queue the ping goes
    to (None: no ping is pushed, the row is still ``open``). ``pinger`` defaults to the push sender; ``diagnose`` is the
    read-only diagnosis source (None: no diagnosis)."""

    def __init__(self, path: Path | str, *, queue_dir: Path | str | None = None, pinger: Pinger | None = None,
                 diagnose: Diagnose | None = None) -> None:
        self.path = Path(path)
        self.queue_dir = Path(queue_dir) if queue_dir is not None else None
        self.pinger = pinger
        self.diagnose = diagnose
        with write_transaction(self._connect) as conn:
            for statement in SCHEMA:
                conn.execute(statement)
            have = {r[1] for r in conn.execute("PRAGMA table_info(would_have_acted)")}
            for name, kind in _PING_COLUMNS:
                if name not in have:
                    conn.execute(f"ALTER TABLE would_have_acted ADD COLUMN {name} {kind}")

    def _connect(self) -> sqlite3.Connection:
        return open_durable(self.path, needs="the would-have-acted record")

    # --- writing ----------------------------------------------------------------------------------------------
    def record(self, *, verb: str, reason: str, event_id: str, order_id: str | None, args: Mapping[str, str],
               night: str, now: str) -> bool:
        """Write the row and claim its ping. Returns True when a row is held for (night, event, verb, reason, arguments)
        after the call. Idempotent: a repeat counts (``times``) and pushes again only when the claim (module docstring)
        allows. A verb or reason outside the closed sets is not recorded; past the night's row bound the call is
        counted in the night's overflow and returns False. Never raises: the record never changes the refusal."""
        try:
            if verb not in _VERBS or reason not in REASONS:
                return False
            at = parse_stamp(now)
            args_text = _args_text({k: v for k, v in args.items() if k in _ARG_KEYS and isinstance(v, str)})
            key = _key(night, event_id, verb, reason, args_text)
            with read_transaction(self._connect) as conn:
                held = conn.execute("SELECT 1 FROM would_have_acted WHERE dedupe_key = ?", (key,)).fetchone()
                full = not held and conn.execute("SELECT count(*) FROM would_have_acted WHERE night = ?",
                                                 (night,)).fetchone()[0] >= ROWS_PER_NIGHT_MAX
            if full:                                         # counted, never written: no diagnosis is run for it
                with write_transaction(self._connect) as conn:
                    conn.execute("INSERT OR IGNORE INTO would_have_acted_nights (night, pushes, overflow) "
                                 "VALUES (?, 0, 0)", (night,))
                    conn.execute("UPDATE would_have_acted_nights SET overflow = overflow + 1 WHERE night = ?", (night,))
                return False
            diagnosis = "" if held else self._diagnosis(verb, event_id)      # outside any write transaction
            claim = None
            with write_transaction(self._connect) as conn:
                conn.execute("INSERT OR IGNORE INTO would_have_acted_nights (night, pushes, overflow) VALUES (?, 0, 0)",
                             (night,))
                hit = conn.execute("UPDATE would_have_acted SET times = times + 1, last_at = ? WHERE dedupe_key = ?",
                                   (now, key)).rowcount
                if not hit:
                    if conn.execute("SELECT count(*) FROM would_have_acted WHERE night = ?",
                                    (night,)).fetchone()[0] >= ROWS_PER_NIGHT_MAX:
                        conn.execute("UPDATE would_have_acted_nights SET overflow = overflow + 1 WHERE night = ?",
                                     (night,))
                        return False
                    conn.execute("INSERT INTO would_have_acted (dedupe_key, night, verb, args, reason, event_id, order_id, "
                                 "diagnosis, ping_state, ping_pushed, push_attempts, times, first_at, last_at, notice_ref) "
                                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', 0, 0, 1, ?, ?, ?)",
                                 (key, night, verb, args_text, reason, event_id, order_id, diagnosis, now, now,
                                  "wha-" + key[:32]))
                row = conn.execute("SELECT row_id, ping_pushed, push_attempts, next_due_at, diagnosis, notice_ref "
                                   "FROM would_have_acted WHERE dedupe_key = ?", (key,)).fetchone()
                pushes = conn.execute("SELECT pushes FROM would_have_acted_nights WHERE night = ?",
                                      (night,)).fetchone()[0]
                due = row[3] is None or parse_stamp(row[3]) <= at
                if self.queue_dir is not None and row[1] == 0 and row[2] < PUSH_ATTEMPTS_MAX and due \
                        and pushes < PUSHES_PER_NIGHT_MAX:
                    conn.execute("UPDATE would_have_acted SET push_attempts = push_attempts + 1, last_push_at = ?, "
                                 "next_due_at = ? WHERE row_id = ?", (now, utc_stamp(at + PUSH_RETRY_AFTER), row[0]))
                    conn.execute("UPDATE would_have_acted_nights SET pushes = pushes + 1 WHERE night = ?", (night,))
                    claim = row
            if claim is not None:
                self._push(claim[0], verb, reason, event_id, args_text, claim[4], claim[5])
            return True
        except Exception:                                   # noqa: BLE001 - a fault here never changes the refusal
            return False

    def _diagnosis(self, verb: str, event_id: str) -> str:
        if self.diagnose is None:
            return ""
        try:
            return bounded_text(self.diagnose(verb, event_id), DIAGNOSIS_MAX)
        except Exception:                                   # noqa: BLE001 - no diagnosis is a row without one
            return ""

    def _push(self, row_id: int, verb: str, reason: str, event_id: str, args_text: str, diagnosis: str,
              notice_ref: str | None) -> None:
        """One push of the open ping, already claimed (counted) by :meth:`record`, so a crash never retries without
        bound. Fixed title, fixed words, enum fields and the bounded diagnosis (marked as untrusted, never as an
        instruction); ``ref`` is the row's notice ref."""
        if self.queue_dir is None:                          # no queue: nothing to push to (record() never claims one)
            return
        body = (f"{verb} {args_text}".strip() + f" would have run, but {REASONS[reason]}. Nothing was done. Event {event_id}.")
        if diagnosis:
            body += " Read-only diagnosis (untrusted text): " + diagnosis[:_PING_DIAGNOSIS_MAX].replace("\n", " ")
        pushed = False
        try:
            send = self.pinger
            if send is None:
                from levain.webpush import sender
                send = sender.send
            result = send(self.queue_dir, kind="notify_urgent", ref=notice_ref, title=PING_TITLE, body=body)
            from levain.desk.events import sender_delivered
            pushed = sender_delivered(result)
        except Exception:                                   # noqa: BLE001 - a push that fails leaves the ping open
            pushed = False
        if pushed:
            with write_transaction(self._connect) as conn:
                conn.execute("UPDATE would_have_acted SET ping_pushed = 1 WHERE row_id = ?", (row_id,))

    # --- reading: the morning list ----------------------------------------------------------------------------
    _COLUMNS = ("row_id, night, verb, args, reason, event_id, order_id, diagnosis, ping_state, ping_pushed, times, "
                "first_at, last_at, notice_ref, acked_at, last_push_at, next_due_at")

    @staticmethod
    def _row(r: tuple[Any, ...]) -> dict[str, Any]:
        return {"row_id": r[0], "night": r[1], "verb": r[2], "args": r[3], "reason": r[4], "why": REASONS.get(r[4], r[4]),
                "event_id": r[5], "order_id": r[6], "diagnosis_untrusted": r[7], "ping_state": r[8],
                "ping_pushed": bool(r[9]), "times": r[10], "first_at": r[11], "last_at": r[12], "notice_ref": r[13],
                "acked_at": r[14], "last_push_at": r[15], "next_due_at": r[16]}

    def morning_list(self, night: str | None = None, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """The would-have-acted rows of ``night`` (a night key as :func:`levain.desk.effect_attempts.night_key` makes it;
        None: every night), oldest first, read-only. Each row is a plain dict; ``diagnosis_untrusted`` is untrusted
        text and must be shown as data, never read as an instruction. ``now`` is accepted so a caller can keep its
        surface a pure function of its clock; it changes nothing here."""
        with read_transaction(self._connect) as conn:
            rows = conn.execute(f"SELECT {self._COLUMNS} FROM would_have_acted "
                                "WHERE (? IS NULL OR night = ?) ORDER BY row_id", (night, night)).fetchall()
        return [self._row(r) for r in rows]

    def open_pings(self) -> list[dict[str, Any]]:
        """The rows whose ping is open and unacknowledged, oldest first, read-only, in the same shape as
        :meth:`morning_list` (the state ND-6 will own: ``notice_ref``, ``acked_at``, ``last_push_at``,
        ``next_due_at``)."""
        with read_transaction(self._connect) as conn:
            rows = conn.execute(f"SELECT {self._COLUMNS} FROM would_have_acted "
                                "WHERE ping_state = 'open' AND acked_at IS NULL ORDER BY row_id").fetchall()
        return [self._row(r) for r in rows]

    def overflow(self, night: str) -> int:
        """How many uncovered calls ``night`` did not write past :data:`ROWS_PER_NIGHT_MAX` (calls, not distinct gaps:
        a repeat of one unwritten gap counts again)."""
        with read_transaction(self._connect) as conn:
            row = conn.execute("SELECT overflow FROM would_have_acted_nights WHERE night = ?", (night,)).fetchone()
        return int(row[0]) if row else 0

    def morning_lines(self, night: str | None = None) -> list[str]:
        """The same list as plain lines for a surface that prints (nightclose, the morning card): one line a row, and
        a last line when ``night`` overflowed its row bound."""
        out = []
        for r in self.morning_list(night):
            diag = f" diagnosis (untrusted): {r['diagnosis_untrusted'][:120]!r}" if r["diagnosis_untrusted"] else ""
            call = " ".join(p for p in (r["verb"], r["args"]) if p)      # an ack has no enum arguments
            out.append(f"would have acted: {call} for {r['event_id']} ({r['why']}; x{r['times']}; "
                       f"ping {'reached a phone' if r['ping_pushed'] else 'open'}){diag}")
        if night is not None and (more := self.overflow(night)):
            out.append(f"would have acted: {more} more uncovered calls this night past the bound of "
                       f"{ROWS_PER_NIGHT_MAX} rows (not written)")
        return out
