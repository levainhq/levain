"""levain.cockpit.coordinator_panels: the cockpit's coordinator panels (C4-b review queue, C6-b morning card; the status
panel is :mod:`levain.cockpit.coordinator_status`, registered here by the same discoverer).

All are READ-ONLY views over the coordinator store (``dispatch.db`` in the dispatch directory) and draw what
``levain.coordinator`` already computes: the queue is :func:`levain.coordinator.review.list_items`, the "waiting on
you" line is :func:`levain.coordinator.review.waiting_on_you` and the morning card is
:func:`levain.coordinator.morning.card`, the data ``levain coord morning`` prints. Nothing here recomputes any of
them, and nothing here decides anything: every row names the command that does, which is yours.

The panels exist only while the coordinator's tables are in ``dispatch.db`` (a discoverer registered with ``retire``;
the file alone proves nothing, the job store shares it): an install that never used the coordinator draws neither,
never an empty card.

The queue's rows are ``review:<item_id>``, the open items and the most recently cleared ones (:data:`CLEARED_SHOWN`).
A row offers the kernel's ``review_clear`` (an open item not marked "yours to clear"), ``review_clear_signed`` (an open
one that is, cleared on the operator's passkey signature) or ``review_reopen`` (a cleared one); the verbs and their
credential rule live in :mod:`levain.cockpit.verbs` (head ruling 1025+25, c4b RULINGS_OBS item 1). A row's
version is its item, its state and its latest event, never its age line, so a Clear is refused as stale only
when the item changed.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from levain.cockpit.engine import ProviderSpec, ReadContext, row_version_of
from levain.cockpit.results import Absent, Read, Result, RowIn
from levain.cockpit.consent import ConsentError, parse_stamp
from levain.dispatch.jobs import JobStoreError

__all__ = ["CLEARED_SHOWN", "REVIEW_ID", "REVIEW_ROW_PREFIX", "MORNING_ID", "coordinator_discoverer", "review_version"]

REVIEW_ID = "review-queue"
REVIEW_ROW_PREFIX = "review:"
REVIEW_FACETS = frozenset({"review_state", "review_authority"})
# the cleared items the queue panel lists after the open ones, newest clear first (each is a reopen away)
CLEARED_SHOWN = 10
MORNING_ID = "coord-morning"
# the kernel's verbs on a queue row (levain.cockpit.verbs registers them)
REVIEW_VERBS = ("review_clear", "review_reopen", "review_clear_signed")
# what a queue row's version is taken over: every stored field (``_review_stored``), as the panel's spec declares it
_REVIEW_VERSION_FIELDS = ("*",)
_ROW_MAX = 400
_LEAD_MIN = 40
_SHORTENED = " … [command shortened: not runnable as shown]"


def _one_line(text: object, limit: int = _ROW_MAX) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[:limit - 1].rstrip() + "…"


def _row(row_id: str, title: str, body: str, tail: str = "") -> RowIn:
    """A list row draws its BODY as the line (the title is not drawn), so the label leads the body. ``tail`` is a
    command the row names (what the operator runs): the clip falls on the text before it, never inside it, unless the
    command alone is longer than the row may be: then the leading text keeps a stub, the command is cut and the cut is
    marked, so what is left cannot be copied as if it were whole."""
    title = _one_line(title)
    if len(tail) > _ROW_MAX - _LEAD_MIN:
        tail = tail[:_ROW_MAX - _LEAD_MIN - len(_SHORTENED)].rstrip() + _SHORTENED
    body = _one_line(_one_line(f"{title}: {body}", _ROW_MAX - len(tail)) + tail)
    return RowIn(id=row_id, title=title, body=body, stored={"title": title, "body": body})


def _age(stamp: str, now: datetime) -> str:
    """How long ago a coordinator stamp was, in the words the team panels use; an unreadable stamp says so."""
    try:
        d = now - parse_stamp(stamp)
    except ConsentError:
        return "age unknown"
    if d.total_seconds() < 60:          # includes a stamp ahead of this clock: skew reads as "just now", never "-1d"
        return "just now"
    if d.days >= 1:
        return f"{d.days}d ago"
    h = d.seconds // 3600
    return f"{h}h ago" if h else f"{d.seconds // 60}m ago"


def _waiting_line(waiting: dict[str, Any], ran_at: str | None = None) -> str:
    from levain.coordinator import deliver

    recs = [r for v in waiting["threads"].values() for r in v] + list(waiting["loose"])
    if not recs and not waiting["unresolved"]:
        if ran_at is None:
            # the delivery pass files what waits; before it has run, an empty list is not "nothing waits"
            return "Not known yet: " + deliver.pass_line(None)
        return f"Nothing is waiting on you ({deliver.pass_line(ran_at)})."
    kinds = [k for k in ("sign", "answer") if any(r["kind"] == k for r in recs)]
    parts = [f"{sum(1 for r in recs if r['kind'] == k)} {k.upper()}" for k in kinds]
    line = f"Waiting on you: {len(recs)}" + (f" ({', '.join(parts)})" if parts else "")
    if waiting["unresolved"]:
        line += f"; {len(waiting['unresolved'])} could not be read"
    return line


def _review_stored(i: Any) -> dict[str, Any]:
    """What a queue row's version is taken over: the item, its state and its latest event (the age in the line moves
    with the clock, so it is drawn and never versioned)."""
    return {"item_id": i.item_id, "state": i.state, "last": list(i.last) if i.last is not None else None}


def review_version(i: Any) -> str:
    """The version the queue panel draws for item ``i`` (a :class:`levain.coordinator.review.Item`): the ONE
    computation, which the review verbs recompute inside the store's write to refuse a Clear or Reopen of an item that
    moved after the actor drew it, and which the phone's queue rows carry."""
    return row_version_of(_review_stored(i), _REVIEW_VERSION_FIELDS)


def _review_row(i: Any, now: datetime) -> RowIn:
    """One queue row, versioned over :func:`_review_stored`."""
    from levain.coordinator import review

    label = review.FEED_LABELS.get(i.feed, i.feed)
    mine = " · yours to clear" if i.authority_relevant else ""
    body = review.body_fields(i)
    text = str(body.get("text") or body.get("summary") or body.get("reason") or "").strip()
    if i.state == "cleared" and i.last is not None:
        head = f"{label} · cleared {_age(i.last[3], now)}{mine}"
    else:
        head = f"{label} · {_age(i.at, now)}{mine}"
    drawn = _row(REVIEW_ROW_PREFIX + i.item_id, head, text or f"{i.feed} item {i.item_id}")
    return RowIn(id=drawn.id, title=drawn.title, body=drawn.body,
                 facets={"review_state": i.state, "review_authority": bool(i.authority_relevant)},
                 stored=_review_stored(i))


def _review_rows(store: Any, now: datetime) -> tuple[tuple[RowIn, ...], str]:
    from levain.autonomic.durable_sqlite import read_transaction
    from levain.coordinator import review

    qdir = Path(store.directory)
    with read_transaction(store.open_readonly) as conn:     # one snapshot for the queue, what waits and the last pass
        items = review.list_items(store, conn=conn)
        # one past what is shown: whether older clears exist, said in the note (they reopen from the laptop)
        cleared = review.queue_page(conn, open_limit=0, cleared_limit=CLEARED_SHOWN + 1).cleared
        waiting = review.waiting_on_you(store, sign_open=review.queue_request_open(qdir),
                                        answer_open=review.question_open(qdir), conn=conn)
        ran_at = review.last_pass(conn)
    older = len(cleared) > CLEARED_SHOWN
    # newest first (list_items is oldest first), then the newest clears
    rows = [_review_row(i, now) for i in reversed(items)] + [_review_row(i, now) for i in cleared[:CLEARED_SHOWN]]
    note = _waiting_line(waiting, ran_at)
    if older:
        note += f" · Cleared shows the {CLEARED_SHOWN} newest clears; older ones: levain coord review list --all"
    return tuple(rows), note


def _review_one(store: Any, row_id: str, now: datetime) -> RowIn | None:
    """The row ``row_id`` read from the store, for a verb's write (open or cleared, listed or not), or None."""
    from levain.autonomic.durable_sqlite import read_transaction
    from levain.coordinator import review

    if not row_id.startswith(REVIEW_ROW_PREFIX):
        return None
    item_id = row_id[len(REVIEW_ROW_PREFIX):]
    with read_transaction(store.open_readonly) as conn:
        found = [i for i in review.list_items(store, include_cleared=True, conn=conn) if i.item_id == item_id]
    return _review_row(found[0], now) if found else None


def _morning_rows(c: dict[str, Any]) -> tuple[RowIn, ...]:
    from levain.coordinator import budget, deliver

    rows: list[RowIn] = []
    for agent, s in c["away"].items():
        if s.get("away") is None:
            rows.append(_row(f"away:{agent}", f"Away · {agent}", "UNKNOWN: the store could not be read"))
        elif s["away"]:
            rows.append(_row(f"away:{agent}", f"Away · {agent}",
                             f"ON since {s.get('since') or '?'}: its SIGNs wait UNDECIDED; leaving takes your "
                             "signature (levain consent away off --signing-key KEY)"))
        else:
            rows.append(_row(f"away:{agent}", f"Away · {agent}", "off"))
    for u in c["undecided"]:
        where = f" · thread {u['thread_id']}" if u.get("thread_id") else ""
        rows.append(_row(f"undecided:{u['request_id']}", f"UNDECIDED · gap #{u['gap']}",
                         f"{u['agent']} {u['tool']}: {u['what']}{where}", tail=f" · rule it: {u['rule']}"))
    for b in c["budgets"]:
        if b["unreadable"]:
            rows.append(_row(f"budget:{b['thread_id']}", f"Budget · {b['thread_id']}",
                             f"UNREADABLE, new steps stop: {b['unreadable']}"))
            continue
        used = ", ".join(f"{k} {budget._num(k, b['used'][k])}/{b['budget'][k]}" for k in budget.METERS)
        uncounted = f" (not counted by Q13 yet: {', '.join(r for _a, r in b['uncounted'])})" if b.get("uncounted") else ""
        state = "CROSSED: new steps stop" if b["refusal"] else "within"
        extend = ""
        if b["sign"] is not None and b["sign"]["open"]:
            extend = (f" · SIGN to extend waits: levain envelope extend {b['thread_id']} --budget K=N,... -o FILE, "
                      "then levain envelope sign FILE")
        rows.append(_row(f"budget:{b['thread_id']}", f"Budget · {b['thread_id']} ({b['envelope_id']})",
                         f"{used}{uncounted}; dollars not metered; {state}", tail=extend))
    w = c["waiting"]
    for t, recs in w["threads"].items():
        rows += [_row(f"waiting:{r['reply_id']}", f"Waiting · {r['kind'].upper()} · {t}", str(r["text"])) for r in recs]
    rows += [_row(f"waiting:{r['reply_id']}", f"Waiting · {r['kind'].upper()}", str(r["text"])) for r in w["loose"]]
    rows += [_row(f"waiting:{r['reply_id']}", f"Waiting · {r['kind'].upper()} · unreadable", str(r["text"]))
             for r in w["unresolved"]]
    rows.append(_row("delivery", "Delivery pass", deliver.pass_line(c.get("delivery_ran"))))
    rows += [_row(f"error:{n}", "Could not read", e) for n, e in enumerate(c["errors"])]
    return tuple(rows)


def _store_at(directory: Callable[[], Path | str | None]) -> Any:
    """The CoordStore for the dispatch directory, only while the coordinator's tables are in its file (None: the
    coordinator was never used here). ``dispatch.db`` is the job store's file too, so the file proves nothing: the
    store's own read-only opener (:meth:`CoordStore.open_readonly`) is the probe, and it creates nothing."""
    from levain.coordinator.store import CoordStore, StoreAbsent

    d = directory()
    if d is None:
        return None
    store = CoordStore(Path(d))
    try:
        store.open_readonly().close()
    except StoreAbsent:
        return None
    return store


def _unreadable(e: Exception) -> RowIn:
    """A store that could not be read says so; any other fault is a defect in the panel and is labelled as one."""
    from levain.dispatch.jobs import JobStoreError

    if isinstance(e, (sqlite3.DatabaseError, OSError, JobStoreError)):
        return _row("error:store", "Could not read", f"the coordinator store: {type(e).__name__}: {e}")
    return _row("error:panel", "Panel fault", f"not the store: {type(e).__name__}: {e}")


def coordinator_discoverer(directory: Callable[[], Path | str | None], *,
                           card: Callable[..., dict[str, Any]] | None = None,
                           clock: Callable[[], datetime] | None = None,
                           ) -> Callable[[ReadContext], list[ProviderSpec]]:
    """The coordinator panels for the install NOW (register with ``Cockpit.discover(..., retire=True)``).
    ``directory`` names the dispatch directory on each read; ``card`` and ``clock`` are seams for tests."""
    now_fn = clock or (lambda: datetime.now(timezone.utc))

    def card_fn(store: Any) -> dict[str, Any]:
        if card is not None:
            return card(store)
        from levain.coordinator import morning

        return morning.card(store)

    def discover(ctx: ReadContext) -> list[ProviderSpec]:
        try:
            if _store_at(directory) is None:
                return []
        except (sqlite3.DatabaseError, OSError, JobStoreError):
            pass                            # the file is there but unreadable: the panels draw and say so
        # fresh read callables per pass: a retired panel's state is matched to its spec by the callable's identity
        # (the contract in ``Cockpit.discover``), as the Team zone's discoverer does
        def review_read(ctx: ReadContext) -> Result:
            try:
                store = _store_at(directory)
                if store is None:
                    return Read(rows=())
                rows, note = _review_rows(store, now_fn())
            except Exception as e:              # a store that cannot be read is this panel's error row, never a raise
                return Read(rows=(_unreadable(e),))
            return Read(rows=rows, note=note)

        def review_read_one(ctx: ReadContext, row_id: str) -> Result:
            store = _store_at(directory)
            row = None if store is None else _review_one(store, row_id, now_fn())
            return Absent("no such review item") if row is None else Read(rows=(row,))

        def morning_read(ctx: ReadContext) -> Result:
            try:
                store = _store_at(directory)
                if store is None:
                    return Read(rows=())
                rows = _morning_rows(card_fn(store))
            except Exception as e:
                return Read(rows=(_unreadable(e),))
            return Read(rows=rows, note=None)

        from levain.cockpit import coordinator_status

        return [
            ProviderSpec(coordinator_status.COORD_STATUS_ID, "list", "Coordinator", "gate",
                         coordinator_status.status_reader(directory, clock), region="operate", rank=20,
                         order="legacy.source", version_fields=("*",), size="medium", span=1,
                         empty="No coordinator store to read."),
            ProviderSpec(MORNING_ID, "list", "Coordinator morning card", "gate", morning_read, region="operate",
                         rank=21, order="legacy.source", version_fields=("*",), size="medium", span=1,
                         empty="Nothing waits after the night."),
            ProviderSpec(REVIEW_ID, "list", "Review queue", "gate", review_read, region="operate", rank=22,
                         order="decision.review", version_fields=_REVIEW_VERSION_FIELDS, size="medium", span=1,
                         empty="Nothing waits in the review queue.", facets=REVIEW_FACETS, read_one=review_read_one,
                         verbs=REVIEW_VERBS),
        ]
    return discover
