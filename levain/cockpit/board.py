"""The cockpit's read-only board (C12): the ready queue as six DERIVED columns, the audit window over the coordinator's
work. Nothing here writes, stores a column, or keeps a second needs-you list.

Columns are READY_SCHEMA section 2's six categories in column order. Cards are C13's ``list(all=True)`` items, each with
its last journal record (who moved it, when, why). The needs-you column IS the Q17-ALL band's open items, read through
``levain.band_view``'s own read-only snapshot and its allocated numbers (a number not yet allocated reads ``None``: a look
never numbers an item). A source that cannot be read is named in ``unreadable``, never shown as an empty column; a store
never made is ``queue: "none"`` ("no queue yet"). ``build`` never raises."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from levain.coordinator import ready

BOARD_PATH = "/cockpit/board.json"
CATEGORIES = ready.CATEGORIES
BAND_HINT = "levain band"
NO_QUEUE = "no queue yet"
_WHAT_MAX = 300


def _last_records(records: Any) -> dict[str, Mapping[str, Any]]:
    """The newest journal record per item id (store order is the journal's order)."""
    out: dict[str, Mapping[str, Any]] = {}
    for rec in records or ():
        if isinstance(rec, Mapping) and isinstance(rec.get("id"), str):
            out[rec["id"]] = rec
    return out


def _line(rec: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if rec is None:
        return None
    return {k: rec.get(k) for k in ("iso", "who", "why", "source", "from_category", "to_category", "applied_by")}


def _card(item: Mapping[str, Any], rec: Mapping[str, Any] | None) -> dict[str, Any]:
    card = {k: item.get(k) for k in ("id", "repo", "title", "done_line", "tier", "depends_on", "status", "status_category",
                                    "declared_category", "held_reason", "well_formed", "rank", "unknown_category")}
    card["journal"] = _line(rec)
    return card


def _band_cards(question_store: Any) -> tuple[list[dict[str, Any]], str | None]:
    """The band's open items as needs-you cards, and the reason they could not be read (else None)."""
    if question_store is None:
        return [], "band"
    from levain import band_view

    try:
        items, numbers, _closed = band_view._read(question_store)
    except Exception:  # noqa: BLE001 - a band that cannot be read is reported, never an empty column
        return [], "band"
    cards = []
    for ref, q in items:
        card: dict[str, Any] = {"id": f"band:{ref}", "number": numbers.get(ref), "journal": None, "band": True,
                                "repo": None, "status": "needs-you", "status_category": "needs-you"}
        if q is None:
            card.update(title="UNREADABLE: this band item could not be read", unreadable=True)
        else:
            card.update(title=str(q.what)[:_WHAT_MAX], source=band_view._tag(q), blocks=q.blocks, filed_at=q.filed_at)
        cards.append(card)
    return cards, None


def build(store_dir: Path | str | None, question_store: Any = None, *, generated: str | None = None) -> dict[str, Any]:
    """The board JSON: ``{schema, generated, queue, columns: [{category, cards, count}], unreadable, band_hint, note}``."""
    unreadable: list[str] = []
    items: list[Mapping[str, Any]] = []
    journal: dict[str, Mapping[str, Any]] = {}
    queue = "none"
    if store_dir is not None:
        try:
            out = ready.list(store_dir, all=True, generated=generated)
            items = list(out.get("items") or [])
            unreadable.extend(str(u) for u in out.get("unreadable") or ())
            generated = out.get("generated") or generated
            if not (Path(store_dir) / "dispatch.db").exists():
                queue = "none"
            elif "coordinator" in unreadable:
                queue = "unreadable"
            else:
                queue = "ok"
                try:
                    journal = _last_records(ready.journal(store_dir))
                except Exception:  # noqa: BLE001 - cards stay, their journal lines are reported missing
                    unreadable.append("journal")
        except Exception:  # noqa: BLE001 - never a 500: the store is named unreadable
            unreadable.append("coordinator")
            queue = "unreadable"
    by_cat: dict[str, list[dict[str, Any]]] = {c: [] for c in CATEGORIES}
    for item in items:
        cat = item.get("status_category")
        if cat == "needs-you":
            continue          # the band owns this column; never a second needs-you list
        by_cat[cat if cat in by_cat else "backlog"].append(_card(item, journal.get(str(item.get("id")))))
    band, band_bad = _band_cards(question_store)
    if band_bad:
        unreadable.append(band_bad)
    by_cat["needs-you"] = band
    note = NO_QUEUE if queue == "none" else None
    if band_bad:
        note = (note + "; " if note else "") + f"the needs-you band could not be read here; run `{BAND_HINT}`"
    return {"schema": 1, "generated": generated, "queue": queue, "unreadable": unreadable, "note": note,
            "band_hint": BAND_HINT,
            "columns": [{"category": c, "count": len(by_cat[c]), "cards": by_cat[c]} for c in CATEGORIES]}
