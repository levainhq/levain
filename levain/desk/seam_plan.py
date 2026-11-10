"""The operator-walk checklist (C15 S5): ``levain desk close --plan``.

What a desk walks with the operator before it writes the operator handoff, derived from ONE read snapshot of the
desk store (and the consent queue beside it): each line is a count and the command or id that settles it. A
section is its count, ``0``, or ``cannot-derive (why)``: a read that failed is never printed as an empty answer. Nothing
is written. Stdlib plus levain's own readers.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from levain.autonomic.durable_sqlite import read_transaction
from levain.cockpit.consent import utc_stamp
from levain.desk import drop_reply, morning, ready, seam_open
from levain.desk import lease as _leases
from levain.desk.seam import _cap, _clean
from levain.desk.seam_open import open_tables_present
from levain.desk.store import CoordStore
from levain.desk.legacy import ROLE_DESK

__all__ = ["MAX_IDS", "close_plan"]

MAX_IDS = 6                    # ids named on one line; the rest are a count


def _ids(names: list[str]) -> str:
    shown = ", ".join(names[:MAX_IDS])
    return shown + (f", +{len(names) - MAX_IDS} more" if len(names) > MAX_IDS else "")


def _guard(name: str, fn: Callable[[], str]) -> str:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - named on its line; the other lines stand
        return f"{name}: cannot-derive ({type(exc).__name__}: {_cap(exc, 160)})"


def _active_launch(conn: sqlite3.Connection, now: str) -> Any:
    """The active desk launch holding the live lease (the one an acknowledgement would come from), or ``None``."""
    held = _leases.lease(conn, now)
    if held is None or not held.live or held.holder == "operator":
        return None
    ln = _leases._launch_by_key(conn, held.holder)
    return ln if ln is not None and ln.role == ROLE_DESK and ln.state == "active" else None


def close_plan(store: CoordStore, queue_dir: Path | str | None, now: datetime, lv: str) -> list[str]:
    """The walk's checklist at ``now`` (an aware datetime), one line per section, in the order the walk goes. ``lv``:
    how the commands in it spell levain. Raises when the desk store itself cannot be opened (the verb says so)."""
    qdir = Path(queue_dir) if queue_dir is not None else Path(store.directory)
    read_at = utc_stamp(now)
    with read_transaction(store.open_readonly) as conn:
        band_items: list[Any] | None = None
        try:
            from levain import band_view
            from levain.consent.questions import QuestionStore

            band_items = band_view.band_snapshot_readonly(QuestionStore(qdir), now=now, coord_conn=conn)["items"]
        except Exception as exc:  # noqa: BLE001
            band_why = f"{type(exc).__name__}: {_cap(exc, 160)}"
        card: dict[str, Any] | None = None
        card_why = "no morning card could be built"
        if band_items is not None:                  # an unreadable queue directory reads as empty: with the band unread,
            try:                                    # the queue parts are unknown, and are said so rather than counted 0
                card = morning.card(store, queue_dir=qdir, conn=conn, now=now)
            except Exception as exc:  # noqa: BLE001
                card_why = f"{type(exc).__name__}: {_cap(exc, 160)}"
        queue_why = f"the queue directory could not be read ({band_why})" if band_items is None else None

        def band() -> str:
            if band_items is None:
                raise RuntimeError(queue_why)
            if not band_items:
                return "band: 0 open"
            names = [f"#{it['n']}" if it.get("n") else "new" for it in band_items]
            return (f"band: {len(band_items)} open ({_ids(names)}): each is answered by him, or carried in in_flight "
                    "by its number")

        def card_part(name: str, count: Callable[[dict[str, Any]], str]) -> Callable[[], str]:
            def run() -> str:
                if band_items is None:
                    raise RuntimeError(queue_why)
                if card is None:
                    raise RuntimeError(card_why)
                return count(card)
            return lambda: _guard(name, run)

        def undecided(c: dict[str, Any]) -> str:
            sign = c["undecided"]
            crossed = [b for b in c["budgets"] if b.get("refusal")]
            if not sign and not crossed:
                return "undecided SIGNs and crossed budgets: 0"
            bits = []
            if sign:
                bits.append(f"{len(sign)} undecided SIGN(s) ({_ids([str(u['request_id']) for u in sign])})")
            if crossed:
                bits.append(f"{len(crossed)} crossed budget(s) ({_ids([str(b['thread_id']) for b in crossed])})")
            return ("undecided SIGNs and crossed budgets: " + "; ".join(bits) +
                    f": they wait for his morning card ({lv} coord morning)")

        def drops() -> str:
            got = drop_reply.unreplied(conn, read_at, every=True)      # the walk counts every one, not the panel's window
            if not got:
                return "drops with no reply: 0"
            open_ = [u for u in got if not u.closed]
            shut = [u for u in got if u.closed]
            parts = []
            if open_:
                parts.append(f"{len(open_)} ({_ids([u.drop_id for u in open_])}): answer each with "
                             f"{lv} coord reply DROP_ID --text-file PATH")
            if shut:
                parts.append(f"{len(shut)} past the reply window ({_ids([u.drop_id for u in shut])}), which can no "
                             "longer be answered: name each id in the handoff's in_flight or notes")
            return "drops with no reply: " + "; ".join(parts if open_ else ["0 to answer", *parts])

        def board() -> str:
            out = ready.list_ready(store, all=True, generated=read_at, conn=conn)
            by: dict[str, list[str]] = {}
            for it in out["items"]:
                if it["status_category"] in ("running", "waiting", "needs-you"):
                    by.setdefault(it["status_category"], []).append(str(it["id"]))
            if out["unreadable"]:
                raise RuntimeError("the board could not be read: " + ", ".join(map(str, out["unreadable"])))
            if not by:
                return "board: nothing running, waiting or needs-you"
            parts = [f"{k} {len(by[k])} ({_ids(by[k])})" for k in ("running", "waiting", "needs-you") if k in by]
            return "board: " + "; ".join(parts) + ": each goes into in_flight with its owner and why, or is done"

        def unacked() -> str:
            if not open_tables_present(conn):
                return "unacknowledged opens: 0 (this store has no open tables: nothing was ever built)"
            rows = conn.execute("SELECT b.open_id, b.seam_kind, b.handoff_id FROM open_builds b WHERE NOT EXISTS (SELECT 1 "
                                "FROM open_acks a WHERE a.open_id = b.open_id) ORDER BY b.open_id").fetchall()
            if not rows:
                return "unacknowledged opens: 0"
            active = _active_launch(conn, read_at)
            mine: list[str] = []
            others: list[str] = []
            for open_id, seam, handoff_id in rows:
                handed = active is not None and conn.execute(
                    "SELECT 1 FROM open_deliveries WHERE open_id = ? AND recipient_launch_id = ?",
                    (open_id, active.launch_id)).fetchone() is not None
                own = (handed and seam in seam_open.PREDECESSOR_HANDOFF_SEAMS and handoff_id is not None
                       and (conn.execute("SELECT writer FROM seam_handoffs WHERE handoff_id = ?",
                                         (handoff_id,)).fetchone() or ("",))[0] == active.session_key)
                if handed and not own:      # the verb's own rules (seam_open.acknowledge), read, not repeated as a guess
                    need = " --read-back-file PATH" if seam in seam_open.PREDECESSOR_HANDOFF_SEAMS else ""
                    mine.append(f"{lv} coord open --ack {open_id}{need} --token N")
                else:
                    others.append(f"#{open_id}")
            tail = []
            if mine:
                tail.append("acknowledge with " + "; ".join(mine[:MAX_IDS]) + (f"; +{len(mine) - MAX_IDS} more"
                                                                             if len(mine) > MAX_IDS else "") +
                            (" (PATH holds your restatement of the handoff)" if any("PATH" in m for m in mine) else ""))
            if others:
                tail.append(f"not acknowledgeable from this launch ({_ids(others)}; handed to an earlier launch, to none, "
                            "or built from your own handoff): name each in the handoff's notes, or leave it")
            return f"unacknowledged opens: {len(rows)} ({_ids([f'#{r[0]}' for r in rows])}): " + "; ".join(tail)

        def away(c: dict[str, Any]) -> str:
            states = c["away"]
            if any(s.get("away") is None for s in states.values()):
                raise RuntimeError("an agent's away state is unknown")
            on = [a for a, s in states.items() if s.get("away")]
            if on:
                return f"away: on ({', '.join(on)})"
            return (f"away: off: when he is leaving, the drop that says so goes first: {lv} coord away DROP_ID "
                    "--token N")

        def body() -> str:
            latest = conn.execute("SELECT handoff_id FROM seam_handoffs ORDER BY handoff_id DESC LIMIT 1").fetchone() \
                if "seam_handoffs" in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} \
                else None
            prev = f" --prev-handoff-id {latest[0]}" if latest else " (no earlier handoff: omit --prev-handoff-id)"
            return ("body: state_line, in_flight, next and contingencies (the night plan) and notes; attested_walked "
                    "is true only after this list was walked with him present; then "
                    f"{lv} coord close --kind operator --body-file PATH --request-id ID{prev} --token N")

        lines = [
            _guard("band", band),
            card_part("undecided SIGNs and crossed budgets", undecided)(),
            _guard("drops with no reply", drops),
            _guard("board", board),
            _guard("unacknowledged opens", unacked),
            card_part("away", away)(),
            _guard("body", body),
        ]
    return [_clean(ln) for ln in lines]
