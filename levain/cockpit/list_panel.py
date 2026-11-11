"""levain.cockpit.list_panel: THE LIST (flow ``task_surfaces_1107.md`` THE LIST; Phill 11-07, np-7c5c8752 (a)).

The Tray becomes the List in every operator-facing word. The List is a VIEW over three stores and keeps none of its own:

* **band**  the open needs-you items (``levain.cockpit.board`` reads them through ``band_view``), read-only, each labelled with
  where to answer it. The List never holds a second needs-you list: it points at Decisions.
* **board** the desk's ``ready`` column (``board.build`` over ``ready.snapshot``), read-only.
* **intention** the operator's seeds, agendas and talk seeds, through the same intention view the Tray read, with ``verbs`` unchanged.

One panel, one ordering (``intention.list``), one row set. Every row carries a ``surface`` facet in {band, board, intention, talk,
drop}. A row's id says whose it is: ``band:<ref>``, ``board:<item>``, ``intention:<id>``. Only a ``intention:`` row is ever offered a
intention verb (each ``intention_*`` row verb is ``applies_to`` a ``intention:`` id), so a band or board row has no write by construction.

The panel's WIRE ID stays ``tray`` (``LIST_PANEL_ID``): the id is a code name like ``TRAY_*`` and the ``intention_*`` verbs, and the
tab map, row-verb routes and ~40 tests name it; the operator-facing words are the title and the group names. Flipping the id is
this one constant plus the places in the PLAN 1d list.

Reading rules: a timed ``next`` is read through ``intentions._parse_when`` only (a date-prefix read would take 09:00 as midnight);
a timed value is overdue when ``now >= that minute``, a date-only one when it is before today; an intention whose date has not come
is LATER, whatever its source (a talk seed too). A source that cannot be read is named in the panel's ``skipped`` line
(status ``partial``) and never shown as an empty list; with nothing else to show it is a Fault."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from levain import band_view, intentions
from levain.cockpit import board as cockpit_board
from levain.cockpit.display_safe import visible
from levain.cockpit.engine import INTENTION_VERBS, Cockpit, ProviderSpec, ReadContext
from levain.cockpit import providers as _prov
from levain.cockpit.providers import (
    INTENTION_FACETS,
    INTENTION_ROW_VERBS,
    TRAY_VIEWS,
    _configured_entity_name,
    _dispatch_dir,
    _intention_row,
    _view,
    _view_fault,
    next_date,
    next_minute,
    intention_empty,
)
from levain.cockpit.registry import DONE_DAYS, opened_day, parse_date
from levain.cockpit.results import Absent, Fault, Read, Result, RowIn
from levain.dashboard import SubstrateSource

LIST_PANEL_ID = "tray"          # the wire id (a code name); the operator-facing title is "List"
LIST_TITLE = "List"
LIST_ROWSET = "intention:tray"
LIST_VERBS = INTENTION_ROW_VERBS + ("intention_seed", "list_intake", *INTENTION_VERBS)      # a verb the registry does not know yet is simply not offered
LIST_NOTE = "Band rows are what only you can answer: answer them in Decisions. Board rows are the agents' ready queue."
BAND_POINTER = "answer it in Decisions (levain band)"
BOARD_POINTER = "the agents' ready queue (levain board)"
LIST_FACETS = INTENTION_FACETS | frozenset({
    "surface", "fires", "overdue", "target", "attention", "list_pointer", "band_number", "board_rank", "board_tier",
    "source", "repo", "held_until", "intention", "intention_text", "band_section", "band_carried", "band_age", "band_since", "band_later_words",
    "time_state", "wake_day", "wake_bucket", "wake_line", "done_on", "done_kind",
})
SURFACES = ("band", "board", "intention", "talk", "drop")
GERMINATED_REF = "germinated into"   # a resolution ref germinate writes when an agenda woke into a band row
DONE_PREFIX = "done:"             # a Done row's id: never a ``intention:`` id, so no intention verb applies to it


@dataclass
class _Part:
    rows: list[RowIn] = field(default_factory=list)
    later: list[RowIn] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)
    fault: str | None = None
    entity: Any = None


def _steering(source: SubstrateSource, today: date) -> tuple[dict[str, dict[str, Any]], str | None]:
    """``{intention id: raw stored dict}`` for the open intentions, read straight from the store, because ``OpenIntention`` does not carry
    ``attention`` or ``target``. A store that cannot be read returns no map and the reason (rows are then shown as plain intentions)."""
    try:
        from anneal_memory.spores import IntentionStore

        return {str(r.get("id")): r for r in IntentionStore(source.anneal.intentions_json, id_prefix="intention").list_open(today=today)
                if isinstance(r, dict)}, None
    except Exception as exc:  # noqa: BLE001 - a store that does not read leaves the steering unknown, never the panel down
        return {}, f"{type(exc).__name__}: {exc}"


def _timing(value: Any, now: datetime, today: date) -> tuple[str | None, bool]:
    """``(fires, overdue)`` for a ``next``: a valid timed value is its own minute and is overdue when ``now`` has reached it; a
    date-only value has no ``fires`` and is overdue when before today; anything else is neither."""
    if intentions.is_timed(value):
        when = intentions._parse_when(value)
        if when is None:
            return None, False
        return next_minute(value), now >= when
    d = parse_date(value)
    return None, bool(d and d < today)


def _surface(raw: dict[str, Any], disposition: str) -> str:
    if intentions.is_talk_seed(raw):
        return "talk"
    target = raw.get("target")
    if disposition == "agenda" and isinstance(target, str) and intentions.valid_target(target):
        return "drop"
    return "intention"


def _wake(day: date, today: date) -> tuple[str, str]:
    """``(wake_day, wake_bucket)`` for a wake that is after ``today``: the ISO day, and tomorrow / this week / later dates."""
    n = (day - today).days
    return day.isoformat(), "tomorrow" if n <= 1 else "week" if n <= 7 else "later"


def _lands(surface: str, target: Any, disposition: str = "someday") -> str:
    """Where a row lands when it wakes, in the words the List prints (``band``, ``board · levain``, ``talk``). Only a seed comes
    back to the List; an agenda with no usable target is filed into the band when it germinates."""
    if surface == "band" or surface == "talk":
        return surface
    if surface == "drop" and isinstance(target, str):
        t = target.strip()
        low = t.lower()
        if low == "desk":
            return "desk"
        if low.startswith("board:") and t[6:]:
            return "board · " + t[6:]
        return "band"           # band and nudge both file a band row; an unknown word falls back to the band (germinate's rule)
    return "band" if disposition == "agenda" else "list"


def _wake_facets(day: date, today: date, surface: str, target: Any, disposition: str = "someday") -> dict[str, Any]:
    wake_day, bucket = _wake(day, today)
    return {"time_state": "later", "wake_day": wake_day, "wake_bucket": bucket,
            "wake_line": f"wakes {band_view.later_words(wake_day, today.isoformat())} \u2192 {_lands(surface, target, disposition)}"}


def _intention_part(source: SubstrateSource, ctx: ReadContext, seen: list[bool], *, apply_hold: bool = True,
                ringing: frozenset[str] = frozenset()) -> _Part:
    """``ringing`` = the band rows the band read returned: an open agenda whose stamp names one of them is that row's
    intention, drawn once, as the band row, so its intention row is left out. A write still finds it (``list_read_one`` passes none)."""
    out = _Part()
    path = source.anneal.intentions_json
    if not path.exists():
        if seen[0]:
            out.fault = f"source disappeared: no intention store at {path}"
        try:
            out.entity = _view(source, ctx).entity_name
        except Exception:  # noqa: BLE001 - a view that cannot be built still leaves an empty List
            out.entity = _configured_entity_name(source)
        return out
    seen[0] = True
    view = _view(source, ctx)
    out.entity = view.entity_name
    bad = _view_fault(view, "open_intentions")
    if bad:
        out.fault = f"intentions: {bad.message}"
        return out
    if not path.exists():
        out.fault = f"source disappeared: no intention store at {path}"
        return out
    steer, steer_bad = _steering(source, ctx.today)
    if steer_bad:
        out.skipped.append((1, f"which intentions are talks and drops could not be read ({steer_bad})"))
    now = ctx.now.astimezone().replace(tzinfo=None)
    items = view.tray
    rows: list[RowIn] = []
    later: list[RowIn] = []
    for s in items:
        raw = steer.get(s.id, {})
        if ringing and intentions.ringing_ref(raw, ringing):
            continue
        surface = _surface(raw, s.disposition) if raw else "intention"
        d = next_date(s.next)
        target = raw.get("target") if isinstance(raw.get("target"), str) else None
        is_later = bool(d and d > ctx.today)
        base = _intention_row(s, ctx.today)
        fires, overdue = _timing(s.next, now, ctx.today)
        extra: dict[str, Any] = {"surface": surface, "fires": fires, "overdue": overdue, "target": target,
                                 "attention": raw.get("attention") if isinstance(raw.get("attention"), str) else None}
        if is_later:
            extra.update(_wake_facets(d, ctx.today, surface, target, s.disposition))
        elif not d and ((surface == "intention" and s.disposition in ("someday", "seed")) or s.disposition == "agenda"):
            extra["time_state"] = "someday"          # an undated seed or agenda: it is never due mechanically
        else:
            extra["time_state"] = "now"
        row = replace(base, facets={**base.facets, **extra}, emphasis="accent" if overdue else base.emphasis)
        (later if apply_hold and is_later else rows).append(row)
    over = max(0, len(rows) - _prov.INTENTION_CAP) if apply_hold else 0
    over_later = max(0, len(later) - _prov.INTENTION_CAP)
    out.rows = rows[:_prov.INTENTION_CAP] if apply_hold else rows
    out.later = later[:_prov.INTENTION_CAP]
    if over:
        out.skipped.append((over, f"over the display cap of {_prov.INTENTION_CAP}"))
    if over_later:
        out.skipped.append((over_later, f"later rows over the display cap of {_prov.INTENTION_CAP}"))
    if len(items) >= _prov.VIEW_INTENTION_LIMIT:
        out.skipped.append((1, f"the substrate view stopped at {_prov.VIEW_INTENTION_LIMIT} rows; at least this many more exist"))
    return out


def _stored(rid: str, text: str, kind: str, domain: str, nxt: str | None, pointer: str, tier: str | None = None) -> dict[str, Any]:
    """A read-only row's stored record, in the intention tuple's fields (the engine refuses any other), so its version moves with
    what it shows. It has no ``origin_key``: it is never a label target."""
    return {"id": rid, "text": text, "disposition": kind, "type": kind, "domain": domain, "next": nxt, "pointer": pointer,
            "tier": tier}


def _aware_instant(instant: Any) -> datetime | None:
    """``instant`` as an aware datetime: a tz-naive stamp is the host's local time. None when it is unusable."""
    if not isinstance(instant, str) or not instant or len(instant) > 40:
        return None
    try:
        t = datetime.fromisoformat(instant[:-1] + "+00:00" if instant.endswith("Z") else instant)
    except ValueError:
        return None
    return t.astimezone() if t.tzinfo is None else t


def _age_text(instant: Any, now: datetime) -> str | None:
    """How long since ``instant`` as the terminal footer writes it: minutes under an hour, hours under a day, else days and hours.
    None when the instant is unusable or still ahead."""
    t = _aware_instant(instant)
    if t is None:
        return None
    secs = (now - t).total_seconds()
    if secs < 0:
        return None
    h = int(secs // 3600)
    return f"{h // 24}d{h % 24}h" if h >= 24 else f"{h}h" if h else f"{int(secs // 60)}m"


def _band_row(card: dict[str, Any], today: date, now: datetime) -> RowIn:
    """One needs-you card as a read-only band row."""
    ref = str(card.get("id", ""))
    ring = card.get("intention") if isinstance(card.get("intention"), str) else None
    words = card.get("intention_text") if ring is not None else None
    # a row that rings a reminder draws the reminder's own words (what Edit changes), not the words it was filed with
    title = visible(words if isinstance(words, str) and words else str(card.get("title") or "")) or "(no text)"
    facets: dict[str, Any] = {"surface": "band", "list_pointer": BAND_POINTER,
                              "band_number": card.get("number") if isinstance(card.get("number"), int)
                              and not isinstance(card.get("number"), bool) else None}
    src = card.get("source")
    if isinstance(src, str) and src:
        facets["source"] = visible(src)
    filed = opened_day(card.get("filed_at"))
    if filed and filed <= today:
        facets["age_days"] = (today - filed).days
    # the age a band row shows is the clock flow's footer reads (the row's refile instant when the host sends one, else its
    # add instant), in the footer's own form, never a whole-day count
    clock = card.get("since") or card.get("filed_at")
    age = _age_text(clock, now)
    if age:
        facets["band_age"] = age      # the first-paint text; the page recomputes it from band_since
        facets["band_since"] = _aware_instant(clock).isoformat()  # the instant the age counts from, with its offset so the page reads it as the server did; stable, so it is in the etag (band_age is not)
    later = card.get("later")
    if isinstance(later, str) and parse_date(later) and parse_date(later) > today:
        facets["held_until"] = later[:10]
        if card.get("section") != "desk":
            facets.update(_wake_facets(parse_date(later), today, "band", None))
    facets.setdefault("time_state", "now")
    if ring is not None:
        facets["intention"] = ring
        if isinstance(words, str) and words:
            facets["intention_text"] = words        # the Edit field's start: the reminder's own words, exact
    if card.get("section") in ("you", "desk"):
        facets["band_section"] = card["section"]
    if isinstance(card.get("carried"), bool):
        facets["band_carried"] = card["carried"]
    later_words = card.get("later_words")
    if "held_until" in facets and isinstance(later_words, str) and later_words:
        facets["band_later_words"] = later_words
    blocks = card.get("blocks")
    stored = _stored(ref, title, "band", facets.get("source", ""), facets.get("held_until"), BAND_POINTER)
    if ring is not None:     # the reminder it rings is part of its version: a row re-rung since the page read it is stale
        stored["germinated"] = {"intention": ring}
    return RowIn(
        id=ref, title=title, body=visible(blocks) if isinstance(blocks, str) and blocks else None, facets=facets,
        stored=stored)


def _board_part(store_dir: Any, question_store: Any, today: date, now: datetime) -> _Part:
    """The band's open items and the desk's ready column, as read-only rows. ``board.build`` never raises; what it could
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
        if card.get("unreadable"):
            out.skipped.append((1, "a band item that could not be read"))
        out.rows.append(_band_row(card, today, now))
    for card in cols.get("ready", ()):
        rid = f"board:{card.get('id')}"
        title = visible(str(card.get("title") or "")) or "(no title)"
        repo = card.get("repo") if isinstance(card.get("repo"), str) and card.get("repo") else None
        facets = {"surface": "board", "list_pointer": BOARD_POINTER, "time_state": "now",
                  "board_rank": card.get("rank") if isinstance(card.get("rank"), int) and not isinstance(card.get("rank"), bool) else None,
                  "board_tier": card.get("tier") if isinstance(card.get("tier"), str) else None}
        if repo:
            facets["repo"] = repo
        done = card.get("done_line")
        out.rows.append(RowIn(
            id=rid, title=title, body=done if isinstance(done, str) and done else None, facets=facets,
            stored=_stored(rid, title, "board", repo or "", None, BOARD_POINTER, facets["board_tier"])))
    return out


def _done_part(source: SubstrateSource, ctx: ReadContext) -> _Part:
    """The rows resolved or composted in the last ``DONE_DAYS`` days, newest first, read-only (id ``done:<intention id>``). The store
    is read as a document: anneal has no listing of its resolved set, and a read must never write."""
    out = _Part()
    path = source.anneal.intentions_json
    if not path.exists():
        return out
    try:
        data = json.loads(path.read_text("utf-8"))
        resolved = data.get("resolved") if isinstance(data, dict) else None
    except (OSError, ValueError) as exc:
        out.skipped.append((1, f"the resolved rows could not be read ({type(exc).__name__})"))
        return out
    cutoff = ctx.now.astimezone(timezone.utc) - timedelta(days=DONE_DAYS)
    found: list[tuple[datetime, dict[str, Any]]] = []
    for r in resolved if isinstance(resolved, list) else []:
        if not isinstance(r, dict):
            continue
        res = r.get("resolution")
        at = res.get("at") if isinstance(res, dict) else None
        try:
            when = datetime.fromisoformat(at.replace("Z", "+00:00")) if isinstance(at, str) else None
        except ValueError:
            when = None
        if when is None or not isinstance(r.get("id"), str) or not isinstance(r.get("text"), str):
            continue
        if str(res.get("ref") or "").startswith(GERMINATED_REF):
            continue                # it woke into the band: the intention is waiting there, not finished
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when >= cutoff:
            found.append((when, r))
    found.sort(key=lambda p: p[0], reverse=True)
    if len(found) > _prov.INTENTION_CAP:
        out.skipped.append((len(found) - _prov.INTENTION_CAP,
                            f"older Done rows not shown (the newest {_prov.INTENTION_CAP} are)"))
    zone = ctx.now.tzinfo
    for when, r in found[:_prov.INTENTION_CAP]:
        rid = DONE_PREFIX + r["id"]
        text = visible(r["text"]) or "(no text)"
        surface = _surface(r, intentions.disposition_of(r))
        kind = str(r["resolution"].get("kind") or "")
        facets: dict[str, Any] = {"surface": surface, "time_state": "done",
                                  "done_on": when.astimezone(zone).date().isoformat(), "done_kind": visible(kind)}
        out.rows.append(RowIn(id=rid, title=text, body=text, facets=facets,
                              stored=_stored(rid, text, "done", str(r.get("domain") or ""), None, "")))
    return out


def list_reader(source: SubstrateSource, ck: Cockpit) -> Callable[[ReadContext], Result]:
    seen = [False]       # the intention store has existed on a read: a later absence is named, never silent

    def read(ctx: ReadContext) -> Result:
        bd = _board_part(_dispatch_dir(ck.consent_reader), ck.question_store, ctx.today, ctx.now)
        ringing = frozenset(r.id[len("band:"):] for r in bd.rows if r.id.startswith("band:"))
        sp = _intention_part(source, ctx, seen, ringing=ringing)
        dn = _done_part(source, ctx) if not sp.fault else _Part()
        rows = [*bd.rows, *sp.rows]
        skipped = [*sp.skipped, *bd.skipped, *dn.skipped]
        if sp.fault:
            if not rows and not sp.later:
                return Fault(sp.fault)           # nothing else to show: a fault, never an empty look
            skipped.append((1, sp.fault))
        return Read(rows=tuple([*rows, *sp.later, *dn.rows]), skipped=tuple(skipped), note=LIST_NOTE,
                    empty=intention_empty("tray", sp.entity))
    return read


def list_read_one(source: SubstrateSource, ck: Cockpit | None = None) -> Callable[[ReadContext, str], Result]:
    """One row from the SOURCE, for a write. A ``intention:`` row is a write target; so is a ``band:`` row that rings an intention
    (its version is the row's own, read from the host's band now, so an act on a row that has since moved or closed is stale).
    Any other band row and every board row is read-only."""
    seen = [False]

    def read_one(ctx: ReadContext, row_id: str) -> Result:
        if row_id.startswith("band:") and ck is not None and ck.question_store is not None:
            cards, bad = cockpit_board._band_cards(ck.question_store)
            if bad:
                return Fault("the needs-you band could not be read")
            for card in cards:
                if card.get("id") == row_id and isinstance(card.get("intention"), str):
                    return Read(rows=(_band_row(card, ctx.today, ctx.now),))
            return Absent(f"row {row_id!r} is gone or rings no intention: it is a read-only List row")
        if not row_id.startswith("intention:"):
            return Absent(f"row {row_id!r} is a read-only List row; it has no write")
        part = _intention_part(source, ctx, seen, apply_hold=False)
        if part.fault:
            return Fault(part.fault)
        for r in (*part.rows, *part.later):
            if r.id == row_id:
                return Read(rows=(r,))
        if part.skipped:
            return Fault(f"row {row_id!r} not found, but the scan was truncated")
        return Absent(f"row {row_id!r} is gone")
    return read_one


def register_list(ck: Cockpit, source: SubstrateSource, intention_common: dict[str, Any]) -> None:
    """Register THE LIST in the place the Tray held (operate zone, rank 10, tall)."""
    common = {k: v for k, v in intention_common.items() if k != "facets"}
    ck.register(ProviderSpec(
        LIST_PANEL_ID, "triage-list", LIST_TITLE, "gate", list_reader(source, ck), region="operate", rank=10, size="tall",
        order="intention.list", views=TRAY_VIEWS, rowset=LIST_ROWSET, read_one=list_read_one(source, ck),
        label_key=lambda stored: _prov.intention_label_key(stored),   # looked up at call time: a test (or a downstream) may replace it
        note=LIST_NOTE,
        empty=intention_empty("tray", _configured_entity_name(source)), absent_is_empty=True,
        verbs=LIST_VERBS, facets=LIST_FACETS, **common))
