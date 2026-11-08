"""levain.cockpit.list_panel: THE LIST (flow ``task_surfaces_1107.md`` THE LIST; Phill 11-07, np-7c5c8752 (a)).

The Tray becomes the List in every operator-facing word. The List is a VIEW over three stores and keeps none of its own:

* **band**  the open needs-you items (``levain.cockpit.board`` reads them through ``band_view``), read-only, each labelled with
  where to answer it. The List never holds a second needs-you list: it points at Decisions.
* **board** the coordinator's ``ready`` column (``board.build`` over ``ready.snapshot``), read-only.
* **spore** the operator's seeds, agendas and talk seeds, through the same spore view the Tray read, with ``verbs`` unchanged.

One panel, one ordering (``spore.list``), one row set. Every row carries a ``surface`` facet in {band, board, spore, talk,
drop}. A row's id says whose it is: ``band:<ref>``, ``board:<item>``, ``spore:<id>``. Only a ``spore:`` row is ever offered a
spore verb (each ``spore_*`` row verb is ``applies_to`` a ``spore:`` id), so a band or board row has no write by construction.

The panel's WIRE ID stays ``tray`` (``LIST_PANEL_ID``): the id is a code name like ``TRAY_*`` and the ``spore_*`` verbs, and the
tab map, row-verb routes and ~40 tests name it; the operator-facing words are the title and the group names. Flipping the id is
this one constant plus the places in the PLAN 1d list.

Reading rules: a timed ``next`` is read through ``spores._parse_when`` only (a date-prefix read would take 09:00 as midnight);
a timed value is overdue when ``now >= that minute``, a date-only one when it is before today; a spore whose date has not come
stays HELD exactly as before (a talk seed is never held). A source that cannot be read is named in the panel's ``skipped`` line
(status ``partial``) and never shown as an empty list; with nothing else to show it is a Fault."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any, Callable

from levain import spores
from levain.cockpit import board as cockpit_board
from levain.cockpit.display_safe import visible
from levain.cockpit.engine import Cockpit, ProviderSpec, ReadContext
from levain.cockpit import providers as _prov
from levain.cockpit.providers import (
    SPORE_FACETS,
    SPORE_ROW_VERBS,
    TRAY_VIEWS,
    _configured_entity_name,
    _dispatch_dir,
    _spore_row,
    _view,
    _view_fault,
    next_date,
    spore_empty,
)
from levain.cockpit.registry import opened_day, parse_date
from levain.cockpit.results import Absent, Fault, Read, Result, RowIn
from levain.dashboard import SubstrateSource

LIST_PANEL_ID = "tray"          # the wire id (a code name); the operator-facing title is "List"
LIST_TITLE = "List"
LIST_ROWSET = "spore:tray"
LIST_VERBS = SPORE_ROW_VERBS + ("spore_seed", "list_intake")      # a verb the registry does not know yet is simply not offered
LIST_NOTE = "Band rows are what only you can answer: answer them in Decisions. Board rows are the agents' ready queue."
BAND_POINTER = "answer it in Decisions (levain band)"
BOARD_POINTER = "the agents' ready queue (levain board)"
LIST_FACETS = SPORE_FACETS | frozenset({
    "surface", "fires", "overdue", "target", "attention", "list_pointer", "band_number", "board_rank", "board_tier",
    "source", "repo", "held_until", "band_section", "band_carried", "band_later_words",
})
SURFACES = ("band", "board", "spore", "talk", "drop")


@dataclass
class _Part:
    rows: list[RowIn] = field(default_factory=list)
    held: list[RowIn] = field(default_factory=list)
    held_total: int = 0         # held rows before the display cap: the count the panel names
    skipped: list[tuple[int, str]] = field(default_factory=list)
    fault: str | None = None
    entity: Any = None


def _steering(source: SubstrateSource, today: date) -> tuple[dict[str, dict[str, Any]], str | None]:
    """``{spore id: raw stored dict}`` for the open spores, read straight from the store, because ``OpenSpore`` does not carry
    ``attention`` or ``target``. A store that cannot be read returns no map and the reason (rows are then shown as plain spores)."""
    try:
        from anneal_memory.spores import SporeStore

        return {str(r.get("id")): r for r in SporeStore(source.anneal.spores_json).list_open(today=today)
                if isinstance(r, dict)}, None
    except Exception as exc:  # noqa: BLE001 - a store that does not read leaves the steering unknown, never the panel down
        return {}, f"{type(exc).__name__}: {exc}"


def _timing(value: Any, now: datetime, today: date) -> tuple[str | None, bool]:
    """``(fires, overdue)`` for a ``next``: a valid timed value is its own minute and is overdue when ``now`` has reached it; a
    date-only value has no ``fires`` and is overdue when before today; anything else is neither."""
    if spores.is_timed(value):
        when = spores._parse_when(value)
        if when is None:
            return None, False
        return when.strftime("%Y-%m-%dT%H:%M"), now >= when
    d = parse_date(value)
    return None, bool(d and d < today)


def _surface(raw: dict[str, Any], disposition: str) -> str:
    if spores.is_talk_seed(raw):
        return "talk"
    target = raw.get("target")
    if disposition == "agenda" and isinstance(target, str) and spores.valid_target(target):
        return "drop"
    return "spore"


def _spore_part(source: SubstrateSource, ctx: ReadContext, seen: list[bool], *, apply_hold: bool = True) -> _Part:
    out = _Part()
    path = source.anneal.spores_json
    if not path.exists():
        if seen[0]:
            out.fault = f"source disappeared: no spore store at {path}"
        try:
            out.entity = _view(source, ctx).entity_name
        except Exception:  # noqa: BLE001 - a view that cannot be built still leaves an empty List
            out.entity = _configured_entity_name(source)
        return out
    seen[0] = True
    view = _view(source, ctx)
    out.entity = view.entity_name
    bad = _view_fault(view, "open_spores")
    if bad:
        out.fault = f"spores: {bad.message}"
        return out
    if not path.exists():
        out.fault = f"source disappeared: no spore store at {path}"
        return out
    steer, steer_bad = _steering(source, ctx.today)
    if steer_bad:
        out.skipped.append((1, f"which spores are talks and drops could not be read ({steer_bad})"))
    now = ctx.now.astimezone().replace(tzinfo=None)
    items = view.tray
    rows: list[RowIn] = []
    held: list[RowIn] = []
    for s in items:
        raw = steer.get(s.id, {})
        surface = _surface(raw, s.disposition) if raw else "spore"
        d = next_date(s.next)
        is_held = apply_hold and surface != "talk" and bool(d and d > ctx.today)
        base = _spore_row(s, ctx.today, held=is_held)
        fires, overdue = _timing(s.next, now, ctx.today)
        extra: dict[str, Any] = {"surface": surface, "fires": fires, "overdue": overdue,
                                 "target": raw.get("target") if isinstance(raw.get("target"), str) else None,
                                 "attention": raw.get("attention") if isinstance(raw.get("attention"), str) else None}
        row = replace(base, facets={**base.facets, **extra}, emphasis="accent" if overdue else base.emphasis)
        (held if is_held else rows).append(row)
    over = max(0, len(rows) - _prov.SPORE_CAP) if apply_hold else 0
    over_held = max(0, len(held) - _prov.SPORE_CAP)
    out.rows = rows[:_prov.SPORE_CAP] if apply_hold else rows
    out.held = held[:_prov.SPORE_CAP]
    out.held_total = len(held)
    if over:
        out.skipped.append((over, f"over the display cap of {_prov.SPORE_CAP}"))
    if over_held:
        out.skipped.append((over_held, f"held rows over the display cap of {_prov.SPORE_CAP}"))
    if len(items) >= _prov.VIEW_SPORE_LIMIT:
        out.skipped.append((1, f"the substrate view stopped at {_prov.VIEW_SPORE_LIMIT} rows; at least this many more exist"))
    return out


def _stored(rid: str, text: str, kind: str, domain: str, nxt: str | None, pointer: str, tier: str | None = None) -> dict[str, Any]:
    """A read-only row's stored record, in the spore tuple's fields (the engine refuses any other), so its version moves with
    what it shows. It has no ``origin_key``: it is never a label target."""
    return {"id": rid, "text": text, "disposition": kind, "type": kind, "domain": domain, "next": nxt, "pointer": pointer,
            "tier": tier}


def _board_part(store_dir: Any, question_store: Any, today: date) -> _Part:
    """The band's open items and the coordinator's ready column, as read-only rows. ``board.build`` never raises; what it could
    not read is named."""
    out = _Part()
    value = cockpit_board.build(store_dir, question_store)
    unreadable = [str(u) for u in value.get("unreadable") or ()]
    cols = {c.get("category"): c.get("cards") or [] for c in value.get("columns") or () if isinstance(c, dict)}
    if "band" in unreadable:
        out.skipped.append((1, "the needs-you band could not be read here; run `levain band`"))
    others = sorted({u for u in unreadable if u != "band"})
    if others:
        out.skipped.append((1, f"the agents' board could not be fully read ({', '.join(others)})"))
    for card in cols.get("needs-you", ()):
        ref = str(card.get("id", ""))
        title = visible(str(card.get("title") or "")) or "(no text)"
        facets: dict[str, Any] = {"surface": "band", "list_pointer": BAND_POINTER,
                                  "band_number": card.get("number") if isinstance(card.get("number"), int)
                                  and not isinstance(card.get("number"), bool) else None}
        src = card.get("source")
        if isinstance(src, str) and src:
            facets["source"] = visible(src)
        filed = opened_day(card.get("filed_at"))
        if filed and filed <= today:
            facets["age_days"] = (today - filed).days
        later = card.get("later")
        if isinstance(later, str) and parse_date(later):
            facets["held_until"] = later[:10]
        if card.get("section") in ("you", "desk"):
            facets["band_section"] = card["section"]
        if isinstance(card.get("carried"), bool):
            facets["band_carried"] = card["carried"]
        words = card.get("later_words")
        if "held_until" in facets and isinstance(words, str) and words:
            facets["band_later_words"] = words
        if card.get("unreadable"):
            out.skipped.append((1, "a band item that could not be read"))
        blocks = card.get("blocks")
        out.rows.append(RowIn(
            id=ref, title=title, body=visible(blocks) if isinstance(blocks, str) and blocks else None, facets=facets,
            stored=_stored(ref, title, "band", facets.get("source", ""), facets.get("held_until"), BAND_POINTER)))
    for card in cols.get("ready", ()):
        rid = f"board:{card.get('id')}"
        title = visible(str(card.get("title") or "")) or "(no title)"
        repo = card.get("repo") if isinstance(card.get("repo"), str) and card.get("repo") else None
        facets = {"surface": "board", "list_pointer": BOARD_POINTER,
                  "board_rank": card.get("rank") if isinstance(card.get("rank"), int) and not isinstance(card.get("rank"), bool) else None,
                  "board_tier": card.get("tier") if isinstance(card.get("tier"), str) else None}
        if repo:
            facets["repo"] = repo
        done = card.get("done_line")
        out.rows.append(RowIn(
            id=rid, title=title, body=done if isinstance(done, str) and done else None, facets=facets,
            stored=_stored(rid, title, "board", repo or "", None, BOARD_POINTER, facets["board_tier"])))
    return out


def list_reader(source: SubstrateSource, ck: Cockpit) -> Callable[[ReadContext], Result]:
    seen = [False]       # the spore store has existed on a read: a later absence is named, never silent

    def read(ctx: ReadContext) -> Result:
        sp = _spore_part(source, ctx, seen)
        bd = _board_part(_dispatch_dir(ck.consent_reader), ck.question_store, ctx.today)
        rows = [*bd.rows, *sp.rows]
        skipped = [*sp.skipped, *bd.skipped]
        if sp.fault:
            if not rows and not sp.held:
                return Fault(sp.fault)           # nothing else to show: a fault, never an empty look
            skipped.append((1, sp.fault))
        held = sp.held
        return Read(rows=tuple([*rows, *held]), filtered=((sp.held_total, "surface date not reached"),) if sp.held_total else (),
                    skipped=tuple(skipped), note=LIST_NOTE, empty=spore_empty("tray", sp.entity))
    return read


def list_read_one(source: SubstrateSource) -> Callable[[ReadContext, str], Result]:
    """One row from the SOURCE, for a write. Only a ``spore:`` row is ever a write target; a band or board row is read-only."""
    seen = [False]

    def read_one(ctx: ReadContext, row_id: str) -> Result:
        if not row_id.startswith("spore:"):
            return Absent(f"row {row_id!r} is a read-only List row; it has no write")
        part = _spore_part(source, ctx, seen, apply_hold=False)
        if part.fault:
            return Fault(part.fault)
        for r in (*part.rows, *part.held):
            if r.id == row_id:
                return Read(rows=(r,))
        if part.skipped:
            return Fault(f"row {row_id!r} not found, but the scan was truncated")
        return Absent(f"row {row_id!r} is gone")
    return read_one


def register_list(ck: Cockpit, source: SubstrateSource, spore_common: dict[str, Any]) -> None:
    """Register THE LIST in the place the Tray held (operate zone, rank 10, tall)."""
    common = {k: v for k, v in spore_common.items() if k != "facets"}
    ck.register(ProviderSpec(
        LIST_PANEL_ID, "triage-list", LIST_TITLE, "gate", list_reader(source, ck), region="operate", rank=10, size="tall",
        order="spore.list", views=TRAY_VIEWS, rowset=LIST_ROWSET, read_one=list_read_one(source),
        label_key=lambda stored: _prov.spore_label_key(stored),   # looked up at call time: a test (or a downstream) may replace it
        note=LIST_NOTE,
        empty=spore_empty("tray", _configured_entity_name(source)), absent_is_empty=True,
        verbs=LIST_VERBS, facets=LIST_FACETS, **common))
