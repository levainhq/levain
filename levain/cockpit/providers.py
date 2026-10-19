"""levain.cockpit.providers: the built-in providers over ``SubstrateView`` (design §6.2).

``SubstrateView`` degrades quietly: a missing spore file, a missing crystal file, a missing edit
ledger and an unreadable context file all arrive as an EMPTY list, which a naive provider would
render as "nothing waiting". So each provider here checks its own source first and returns
``Absent`` / ``Fault`` where the view would have hidden it (the typed-result rule, §3.4), and the
edit ledger is read directly because ``recent_edits`` swallows every fault."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from levain.cockpit.anneal_effects import section_resource_key
from levain.cockpit.authority import LabelKey
from levain.cockpit.effects import SPORE_AUTHORITY_FIELDS, _stored_authority, spore_resource_key
from levain.cockpit.engine import Cockpit, ProviderSpec, ReadContext, row_version_of
from levain.cockpit.registry import parse_date
from levain.cockpit.results import Absent, Fault, Read, Result, RowIn
from levain.dashboard import (
    CLASS_A,
    CLASS_B,
    CLASS_C,
    LOOP_DISPOSITION,
    SubstrateSource,
    SubstrateView,
    OpenSpore,
    _episode_row,
    _read_levain_config,
    _one_clause,
    open_spore_from,
    state_from_data,
    state_record,
)

# The view caps each spore bucket at ``max_spores``. The cockpit asks for far more than any store
# holds and DISCLOSES a cap that is still hit, so a panel never shows a prefix as the whole set.
SPORE_CAP = 5000
# The view's own per-bucket limit is raised out of the way so the cockpit's cap is applied AFTER the
# hold filter: ranking and holds decide what is visible, then the cap truncates what is visible.
VIEW_SPORE_LIMIT = 100_000
EDITS_LIMIT = 20
# the entity's own episodes the view reads (newest first). Feed rows are not cut to that window: a busy
# entity's newest few minutes would otherwise hide every other agent (measured on flow, 10-10); the
# panel's note names the window instead, and the agent filter shows each agent's own run
EPISODE_LIMIT = 50
# An undated handoff stops leading after this many days untouched: it says "pick up here next
# session", and a calendar that has refuted that must not keep it first (flow, 2026-08-27, where
# one led the queue for 26 days). A dated handoff never expires.
HANDOFF_FRESH_DAYS = 7

# the verbs a spore panel offers (design §4.2); anneal still refuses a verb the spore's type forbids
SPORE_ROW_VERBS = ("spore_touch", "spore_descend", "spore_surface_at", "spore_update",
                   "spore_set_disposition", "spore_ascend")
SPORE_STORED = ("id", "text", "disposition", "tier", "type", "domain", "next", "pointer", "salience", "seen",
                "origin_key")
# seen alone never stales a write (a touch that clears an elapsed next does); origin_key is store-assigned
# once and names the label resource, so it is carried, not versioned
SPORE_VERSION_EXCLUDED = ("seen", "origin_key")
SPORE_VERSION_FIELDS = tuple(f for f in SPORE_STORED if f not in SPORE_VERSION_EXCLUDED)
SPORE_FACETS = frozenset({
    "disposition", "domain", "tier", "salience", "spore_type", "due", "overdue_days",
    "handoff_expired", "last_seen", "age_days", "held",
})
# the Tray's second view (2026-10-10, "yes to all": "Tray by Age" is a view of the Tray box, not
# a second box): the same rows, oldest first
TRAY_VIEWS = (("age", "By age", "spore.age"),)
EPISODE_FACETS = frozenset({"episode_type", "source", "at", "tags", "agent"})
EPISODE_STORED = ("id", "timestamp", "type", "source", "content", "tags")
# a federated episode feed's name: it is part of every row id the feed contributes
FEED_EMPHASIS = ("none", "dim")      # what a feed row may ask for (a quiet agent's rows recede)
FEED_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")      # matched whole (fullmatch)


def _store_label(db: Path) -> str:
    """The store's display label for the masthead: ``~/…`` under the home directory, else the file
    name. The absolute path (which carries the account name) never goes on the wire (design §6.2)."""
    try:
        home = os.path.normpath(os.path.expanduser("~"))
        rel = os.path.relpath(os.path.normpath(os.path.abspath(db.expanduser())), home)
        if rel == ".":
            return "~/"
        if rel == ".." or rel.startswith(".." + os.sep):
            return db.name
        return "~/" + Path(rel).as_posix()
    except (ValueError, RuntimeError, OSError):
        return db.name       # no filesystem resolution, and no exception text, can reach the wire


def _view(source: SubstrateSource, ctx: ReadContext) -> SubstrateView:
    return ctx.memo("view", lambda: source.build(max_spores=VIEW_SPORE_LIMIT, max_episodes=EPISODE_LIMIT,
                                                 now=ctx.now))


def _view_fault(view: SubstrateView, *keys: str) -> Fault | None:
    for k in (*keys, "store"):
        if k in view.errors:
            return Fault(f"{k}: {view.errors[k]}")
    return None


def _spore_row(s: Any, today: date, *, held: bool = False) -> RowIn:
    due = parse_date(s.next)
    seen = parse_date(s.seen)
    expired = bool(
        s.disposition == "handoff" and not due and seen and (today - seen).days > HANDOFF_FRESH_DAYS
    )
    facets: dict[str, Any] = {
        "disposition": s.disposition, "domain": s.domain, "tier": s.tier, "salience": s.salience,
        "spore_type": s.type, "handoff_expired": expired, "last_seen": s.seen or None,
        "due": due.isoformat() if due else None,
    }
    if held:
        # a Tray row whose surface date has not come: the kernel lists it apart from the due rows
        facets["held"] = True
    if due:
        facets["overdue_days"] = max(0, (today - due).days)
    created = parse_date(s.created)
    if created and created <= today:
        facets["age_days"] = (today - created).days
    return RowIn(id=f"spore:{s.id}", title=s.text, body=s.text, facets=facets, stored=spore_stored(s))


def spore_stored(s: OpenSpore) -> dict[str, Any]:
    """A spore row's stored record: what its version covers (``SPORE_VERSION_FIELDS``)."""
    return {f: getattr(s, f) for f in SPORE_STORED}


def spore_label_key(stored: Mapping[str, Any]) -> LabelKey | None:
    """A spore row's label key (K2b-3b §2): its authority fields as stored, under its origin key. A
    spore with no origin_key was never a cockpit target, so it has none."""
    ok = stored.get("origin_key")
    if not isinstance(ok, str) or not ok:
        return None
    values = {f: _stored_authority(stored, f) for f in SPORE_AUTHORITY_FIELDS}
    if values["disposition"] == LOOP_DISPOSITION:
        # the view names a plain loop's absent disposition LOOP_DISPOSITION; the store, and so the label,
        # hold it as absent (None), so the hash is taken over None
        values["disposition"] = None
    return LabelKey(spore_resource_key(ok), values)


def section_label_key(heading: str) -> Callable[[Mapping[str, Any]], LabelKey | None]:
    """A Class-A section's value label key: the text ``read_section`` returned, under its heading. The
    label was written over ``canonical_section_markdown(body)``, which is that text (K2b-3b §0.5)."""
    def key(value: Mapping[str, Any]) -> LabelKey | None:
        text = value.get("markdown")
        return LabelKey(section_resource_key(heading), {"body": text}) if isinstance(text, str) else None
    return key


def spore_row_version(raw: Mapping[str, Any]) -> str:
    """The ``version`` the spore panels render for this stored spore, from anneal's raw dict. A
    spore write passes it to anneal as ``version_of``, so the compare runs inside anneal's write
    transaction against exactly what the operator was shown (§4.4)."""
    return row_version_of(spore_stored(open_spore_from(raw)), SPORE_VERSION_FIELDS, SPORE_VERSION_EXCLUDED)


def _spore_provider(
    source: SubstrateSource, bucket: str, *, apply_hold: bool = True
) -> Callable[[ReadContext], Result]:
    def read(ctx: ReadContext) -> Result:
        path = source.anneal.spores_json
        if not path.exists():
            return Absent(f"no spore store at {path}")
        view = _view(source, ctx)
        bad = _view_fault(view, "open_spores")
        if bad:
            return bad
        if not path.exists():           # removed while the view was being built
            return Absent(f"no spore store at {path}")
        items = {"tray": view.tray, "loops": view.open_spores, "keep": view.keep}[bucket]
        filtered = 0
        rows = []
        held_rows = []
        for s in items:
            if apply_hold and bucket == "tray" and (d := parse_date(s.next)) and d > ctx.today:
                filtered += 1           # surface date not reached: held on purpose, not unreadable
                held_rows.append(_spore_row(s, ctx.today, held=True))
                continue
            rows.append(_spore_row(s, ctx.today))
        view_full = len(items) >= VIEW_SPORE_LIMIT
        over = max(0, len(rows) - SPORE_CAP) if apply_hold else 0
        over_held = max(0, len(held_rows) - SPORE_CAP)
        rows = rows[:SPORE_CAP] if apply_hold else rows
        rows += held_rows[:SPORE_CAP]
        skipped = ()
        if over:
            skipped += ((over, f"over the display cap of {SPORE_CAP}"),)
        if over_held:
            skipped += ((over_held, f"held rows over the display cap of {SPORE_CAP}"),)
        if view_full:
            skipped += ((1, f"the substrate view stopped at {VIEW_SPORE_LIMIT} rows; at least this many more exist"),)
        return Read(rows=tuple(rows), filtered=((filtered, "surface date not reached"),) if filtered else (),
                    skipped=skipped)
    return read


def _spore_read_one(source: SubstrateSource, bucket: str) -> Callable[[ReadContext, str], Result]:
    def read_one(ctx: ReadContext, row_id: str) -> Result:
        # a fresh read of the SOURCE: ``ctx`` is the write's own (``Cockpit.read_one`` makes one per
        # read, never the render cycle's), passed through so it keeps the request's authority (§8d row 6).
        # The row by its id, NOT through the list's visibility rules: a row rescheduled into the
        # future since the render still exists, and a write must find it
        res = _spore_provider(source, bucket, apply_hold=False)(ctx)
        if not isinstance(res, Read):
            return res
        for r in res.rows or ():
            if r.id == row_id:
                return Read(rows=(r,))
        if res.skipped:    # the scan was truncated: not finding the row proves nothing
            return Fault(f"row {row_id!r} not found, but the scan was truncated")
        return Absent(f"row {row_id!r} is gone")
    return read_one


def _ts(v: Any) -> str:
    return str(v) if v is not None else ""


def _utc_at(v: Any) -> str:
    """An episode timestamp in one form (UTC, microseconds, ``Z``), for the entity's rows and every
    feed's alike, so ``time.desc``, which compares the strings, compares instants.
    A timestamp with no zone, or one that does not parse, is unknown time: "" (it sorts last)."""
    try:
        d = datetime.fromisoformat(_ts(v))
    except ValueError:
        return ""
    if d.tzinfo is None:
        return ""
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class _FeedRead:
    rows: list[RowIn] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)
    filtered: list[tuple[int, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    stale: bool = False
    as_of: datetime | None = None


def _feed_rows(name: str, res: Result) -> _FeedRead:
    """``_feed_read`` with every failure of the feed's own output, of any shape, as that feed's
    skipped entry: nothing a feed returns can take the entity's rows or another feed down with it."""
    try:
        return _feed_read(name, res)
    except Exception as exc:  # noqa: BLE001 -- a malformed feed result of ANY shape is that feed's fault
        out = _FeedRead()
        out.skipped.append((1, f"{name} unavailable: {type(exc).__name__}: {exc}"))
        return out


def _feed_read(name: str, res: Result) -> _FeedRead:
    """One federated feed's rows, re-keyed under the feed (``feed:<name>:<id>``) so no feed row can be
    taken for one of the entity's own episodes (the tombstone verb applies to ``episode:`` rows only).
    A feed that faults or breaks the episode row contract contributes NO rows and is named in
    ``skipped``, so the panel is ``partial``, never short and quiet. A feed's own ``skipped``,
    ``filtered``, ``note`` and ``stale`` reach the panel, each named by the feed."""
    out = _FeedRead()
    if isinstance(res, Absent):
        out.notes.append(f"{name}: not configured: {res.reason}")
        return out
    if isinstance(res, Fault):
        out.skipped.append((1, f"{name} unavailable: {res.message}"))
        return out
    if not isinstance(res, Read) or res.rows is None:
        out.skipped.append((1, f"{name} unavailable: the feed returned no rows"))
        return out
    rows: list[RowIn] = []
    seen: set[str] = set()
    for r in res.rows:
        if not isinstance(r, RowIn):
            out.skipped.append((1, f"{name} unavailable: a row is a {type(r).__name__}, not a RowIn"))
            return out
        extra = (set(r.facets) - EPISODE_FACETS) | (set(r.stored) - set(EPISODE_STORED))
        if extra:
            out.skipped.append((1, f"{name} unavailable: fields {sorted(extra)} are not episode fields"))
            return out
        if type(r.id) is not str or r.id in seen:
            out.skipped.append((1, f"{name} unavailable: row id {r.id!r} is not a string or repeats"))
            return out
        seen.add(r.id)
        facets = {**r.facets, "agent": r.facets.get("agent") or name}
        if "at" in facets:
            facets["at"] = _utc_at(facets["at"])
        rows.append(RowIn(id=f"feed:{name}:{r.id}", title=r.title, body=r.body, facets=facets,
                          emphasis=r.emphasis if r.emphasis in FEED_EMPHASIS else "none",
                          stored={k: list(v) if isinstance(v, list) else v for k, v in r.stored.items()}))
    skipped = [(int(n), f"{name}: {why}") for n, why in res.skipped]
    filtered = [(int(n), f"{name}: {why}") for n, why in res.filtered]
    as_of = None
    if res.as_of is not None:      # the feed's own confirmation time: old cached data must not read fresh
        as_of = datetime.fromisoformat(res.as_of)
        if as_of.tzinfo is None:
            raise ValueError(f"as_of {res.as_of!r} has no zone")
    out.rows, out.skipped, out.filtered = rows, skipped, filtered
    if isinstance(res.note, str) and res.note:
        out.notes.append(f"{name}: {res.note}")
    out.stale = bool(res.stale)
    out.as_of = as_of
    return out


def episode_stored(e: Any) -> dict[str, Any]:
    """An episode row's stored record, from the dashboard's ``EpisodeRow``: the one shape its
    rendered version is computed over."""
    return {"id": e.id, "timestamp": e.timestamp, "type": e.type, "source": e.source,
            "content": e.content, "tags": list(e.tags)}


def episode_row_version(ep: Any) -> str:
    """The version the episodes panel renders for a raw anneal episode, so a write's issue can check
    what the operator saw against the store's own snapshot."""
    return row_version_of(episode_stored(_episode_row(ep)), EPISODE_STORED)


def _episode_rowin(e: Any, own: str) -> RowIn:
    return RowIn(
        id=f"episode:{e.id}", title=e.content, body=e.content,
        facets={"episode_type": e.type, "source": e.source, "at": _utc_at(e.timestamp),
                "tags": list(e.tags), "agent": own},
        stored=episode_stored(e),
    )


def _entity_label(source: SubstrateSource) -> str:
    """The ``agent`` facet of the entity's own rows, read narrowly (no substrate view): the source's
    name, else the install config's. The panel's view also falls back to origin.md's H1; a facet
    is display only (the row version covers the stored record)."""
    if source.entity_name:
        return source.entity_name
    if source.install_root is not None:
        name = _read_levain_config(source.install_root).get("entity_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return "entity"


def _episode_read_one(source: SubstrateSource) -> Callable[[ReadContext, str], Result]:
    """One of the entity's episodes by id, read from the store, NOT through the panel's newest-N
    list: an episode that newer ones pushed off the list still exists, and a write that names it
    must find it (codex K2b-3a r1). It reads only that episode, so a fault in another row does not
    make it unreadable (codex r2)."""
    def read_one(ctx: ReadContext, row_id: str) -> Result:
        if not row_id.startswith("episode:"):
            return Absent(f"row {row_id!r} is not one of the entity's episodes")
        db = source.anneal.episodic_db
        if not Path(db).is_file():
            return Fault("the episodic store is absent")
        from anneal_memory import AnnealMemoryError, Store

        try:
            eid = row_id[len("episode:"):]
            with Store(str(db), read_only=True) as st:
                ep = st.get(eid)
                # the panel lists what ``recall`` returns, which leaves superseded episodes out
                hidden = ep is not None and eid in st.superseded_by_map([eid])
        except (AnnealMemoryError, OSError, ValueError, RecursionError) as exc:
            return Fault(f"{type(exc).__name__}: {exc}")
        if ep is None or hidden:
            return Absent(f"row {row_id!r} is gone")
        return Read(rows=(_episode_rowin(_episode_row(ep), _entity_label(source)),))
    return read_one


def _episodes(source: SubstrateSource,
              feeds: Sequence[tuple[str, Callable[[ReadContext], Result]]] = ()) -> Callable[[ReadContext], Result]:
    """The entity's own episodes (``agent`` = the entity's name), then each federated feed's. A feed
    is read inside this panel's read, so its time counts against the panel's timeout."""
    def read(ctx: ReadContext) -> Result:
        view = _view(source, ctx)
        bad = _view_fault(view, "episodes")
        if bad:
            return bad
        own = view.entity_name or "entity"
        capped = len(view.episodes) >= EPISODE_LIMIT
        rows = [_episode_rowin(e, own) for e in view.episodes]
        skipped: list[tuple[int, str]] = []
        filtered: list[tuple[int, str]] = []
        notes: list[str] = [f"the entity's newest {EPISODE_LIMIT} episodes"] if capped and feeds else []
        stale = False
        as_ofs: list[datetime] = []
        for name, feed in feeds:
            try:
                # its own context: a feed over another source must not be handed this cycle's memoised view
                res = feed(ReadContext(ctx.now, fresh=ctx.fresh))
            except Exception as exc:   # noqa: BLE001 -- one feed's fault names that feed, never blanks the panel
                res = Fault(f"{type(exc).__name__}: {exc}")
            got = _feed_rows(name, res)
            rows.extend(got.rows)
            skipped += got.skipped
            filtered += got.filtered
            notes += got.notes
            stale = stale or got.stale
            if got.as_of is not None:
                as_ofs.append(got.as_of)
        # the panel is as fresh as its oldest confirmed source; the entity's own read is now
        as_of = min(as_ofs).astimezone(timezone.utc).isoformat() if as_ofs and min(as_ofs) < ctx.now else None
        return Read(rows=tuple(rows), skipped=tuple(skipped), filtered=tuple(filtered),
                    note="; ".join(notes) or None, stale=stale, as_of=as_of)
    return read


def _ledger_path(source: SubstrateSource) -> Path | None:
    if source.write_scope is not None:
        return source.write_scope.ledger_root / "edits.jsonl"
    if source.install_root is not None:
        return source.install_root / ".levain" / "edits.jsonl"
    return None


def _ledger_edits(source: SubstrateSource) -> Result:
    """The ``levain.writes`` ledger's newest ``EDITS_LIMIT`` records as rows (its own best-effort
    contract): ``Read``, ``Absent`` (no ledger yet) or ``Fault``."""
    path = _ledger_path(source)
    if path is None:
        return Absent("no edit ledger is configured for this source")
    if not path.exists():
        # the ledger's one writer (levain.writes' audit append) creates it at the first edit; the path stays
        # off the wire, as the store path does (entity.store_label)
        return Absent("no edit recorded yet (the first edit creates the ledger)")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError) as exc:
        return Fault(f"edit ledger unreadable: {type(exc).__name__}: {exc}")
    recs: list[tuple[str, dict[str, Any]]] = []
    bad = 0
    for n, ln in enumerate(lines):
        if not ln.strip():
            continue
        try:
            rec = json.loads(ln)
        except ValueError:
            bad += 1
            continue
        if not isinstance(rec, dict):
            bad += 1
            continue
        # some audit records (an action's) carry id "": still a readable record, so it keeps a
        # row, named by its line (the ledger is append-only, so a line number is stable).
        rid = rec.get("id")
        recs.append((rid if isinstance(rid, str) and rid else f"line{n}", rec))
    rows = tuple(
        RowIn(
            id=f"edit:{rid}", title=f"{r.get('kind', '')} {r.get('action', '')} {r.get('source', '')}".strip(),
            facets={"edit_kind": str(r.get("kind", "")), "at": _utc_at(r.get("ts")), "undoable": bool(r.get("undoable"))},
            stored=r,
        )
        for rid, r in reversed(recs[-EDITS_LIMIT:])
    )
    return Read(rows=rows, skipped=((bad, "unreadable ledger line"),) if bad else ())


def _tomb_gate(store: Any, origin_key: str, effect_id: Any, episode_id: str) -> str:
    """A ``tomb:`` row's gate (K2b-3b §0.7, §8d row 5): ``match`` only while anneal reads the key
    retired by THIS op's ``effect_id`` and episode; ``unavailable`` when anneal cannot be read."""
    try:
        st = store.origin_key_status(origin_key)
    except Exception:  # noqa: BLE001 - an unreadable store labels nothing
        return "unavailable"
    if getattr(st, "state", None) == "retired" and st.effect_id == effect_id and st.episode_id == episode_id:
        return "match"
    return "mismatch"


# what a broker fire reads as in Recent edits: a past-tense phrase per verb, then the thing it touched, then where it came from
_EDIT_PHRASES = {
    "spore_touch": "marked seen", "spore_descend": "resolved", "spore_surface_at": "rescheduled", "spore_update": "edited",
    "spore_set_disposition": "moved", "spore_ascend": "promoted", "spore_seed": "added", "episode_tombstone": "deleted episode",
    "operator_state": "set the state line", "section_edit": "edited", "config": "edited", "entity_name": "renamed the entity",
    "undo": "undid an edit", "pending_cancel": "cancelled a pending change", "token_rotate": "rotated the surface token"}


def _edit_target(resource_key: str) -> str:
    kind, _, ident = resource_key.partition(":")
    if kind == "spore" and ident:
        return f"item {ident}"
    if kind == "section" and ident:
        return f"the {ident} section"
    if kind == "token":
        return ident          # a surface token: "rotated the surface token browser"
    if kind == "pend":
        return ""             # a pending change is named by its verb alone
    return resource_key       # a shape this table does not know is named as the store names it


def _edit_origin(credential: Any) -> str:
    """Where a write came from, in words: ``token:browser:1`` is the browser (the surface's name, never its generation)."""
    cred = str(credential or "")
    kind, _, rest = cred.partition(":")
    if kind == "token" and rest:
        name, _, gen = rest.rpartition(":")
        return f"from the {name if name and gen.isdigit() else rest}"
    if kind == "device":
        return "from a signed device"
    return ""


def plain_edit_title(verb: str, target: str, credential: Any) -> str:
    """A broker fire as a sentence a person reads: ``resolved item spore-017, from the browser``."""
    verb = verb if isinstance(verb, str) else ""     # a journal row's verb is a string; a malformed one must not take the panel down
    said = " ".join(x for x in (_EDIT_PHRASES.get(verb) or verb.replace("_", " "), target) if x)
    return ", ".join(x for x in (said, _edit_origin(credential)) if x)


def _journal_edits(source: SubstrateSource, page: Any) -> list[RowIn]:
    """The broker's applied effects as Recent edits rows (K2b-3b §0.6, §0.7): ``fire:<op>:<idx>``,
    or ``tomb:<episode_id>:<op>`` for a tombstone's create, its gate read here once."""
    from levain.cockpit.anneal_effects import TOMB_KEY_PREFIX, EpisodeEffectStore

    episodes = None
    rows: list[RowIn] = []
    for e in page.rows:
        proj = e.projection or {}
        stored: dict[str, Any] = {
            "_fire": True, "operation_id": e.operation_id, "idx": e.idx, "state": e.state, "verb": e.verb,
            "resource_key": e.resource_key, "store": e.store, "changes": dict(e.changes),
            "credential": e.credential, "applied_at": e.applied_at}
        facets = {"edit_kind": str(e.verb or ""), "at": _utc_at(e.applied_at), "undoable": False}
        if e.resource_key.startswith(TOMB_KEY_PREFIX):
            ok = e.resource_key[len(TOMB_KEY_PREFIX):]
            row_id = str((e.target or {}).get("row_id") or "")
            episode_id = row_id[len("episode:"):] if row_id.startswith("episode:") else row_id
            if episodes is None:
                ws = source.write_scope
                episodes = EpisodeEffectStore((ws.anneal if ws is not None else source.anneal).episodic_db)
            stored.update(origin_key=ok, episode_id=episode_id, effect_id=e.changes.get("effect_id"),
                          _tomb_gate=_tomb_gate(episodes, ok, e.changes.get("effect_id"), episode_id))
            rows.append(RowIn(id=f"tomb:{episode_id}:{e.operation_id}", title=plain_edit_title("episode_tombstone", episode_id, e.credential),
                              facets=facets, stored=stored))
        else:
            rows.append(RowIn(id=f"fire:{e.operation_id}:{e.idx}", title=plain_edit_title(e.verb or "", _edit_target(e.resource_key), e.credential),
                              facets=facets, stored=stored))
    return rows


def edit_label_key(stored: Mapping[str, Any]) -> LabelKey | None:
    """A Recent edits row's label key: only a ``tomb:`` row has one, its op's labels row under the gate
    read once into ``stored`` (§8d row 5), because a label describes the STORE's state (flow §4.5). A
    ``fire:`` row is history: it shows who made the edit (its credential) and carries no label, since a
    fire's own signed values always match its own labels row whatever the store holds now. A ledger
    row has none."""
    if stored.get("_fire") is not True or "_tomb_gate" not in stored:
        return None
    from levain.cockpit.anneal_effects import tomb_resource_key

    ok = stored["origin_key"]
    return LabelKey(tomb_resource_key(ok), {"origin_key": ok}, stored.get("operation_id"), stored["_tomb_gate"])


def _edits(source: SubstrateSource,
           journal: Callable[[], Callable[[int], Any] | None] = lambda: None) -> Callable[[ReadContext], Result]:
    """Recent edits: the ledger's rows merged with the broker's journal (K2b-3b §0.6), one sort and ONE
    cut at ``EDITS_LIMIT``. Each contributor faults on its own: a failed journal keeps the ledger's
    rows (``partial``), a missing ledger with a healthy journal shows the journal's, and only both
    failing is a Fault. The journal side is read once per request (memoised on its authority)."""
    def read(ctx: ReadContext) -> Result:
        led = _ledger_edits(source)
        reader = journal()
        if reader is None:
            return led
        try:
            def jread() -> tuple[list[RowIn], int] | None:
                page = reader(EDITS_LIMIT)
                return None if page is None else (_journal_edits(source, page), page.skipped)
            got = ctx.authority.memo("edits:journal", jread) if ctx.authority is not None else jread()
        except Exception as exc:  # noqa: BLE001 - a journal fault is this contributor's, never the panel's alone
            jfault: str | None = f"broker edits unreadable: {type(exc).__name__}: {exc}"
            got = None
        else:
            jfault = None
        if got is None:
            if jfault is None:     # the broker is not ACTIVE: the ledger alone, as before the broker
                return led
            if isinstance(led, Read):
                return Read(rows=led.rows, skipped=(*led.skipped, (1, jfault)))
            return Fault(f"{jfault}; ledger: {led.reason if isinstance(led, Absent) else led.message}")
        jrows, jskipped = got
        skipped: list[tuple[int, str]] = [(jskipped, "unreadable cockpit op")] if jskipped else []
        if isinstance(led, Read):
            lrows = list(led.rows or ())
            skipped += list(led.skipped)
        else:
            lrows = []
            if isinstance(led, Fault):
                skipped.append((1, led.message))
        merged = [("ledger", r) for r in lrows] + [("journal", r) for r in jrows]
        merged.sort(key=lambda p: (p[0], p[1].id))
        merged.sort(key=lambda p: p[1].facets.get("at") or "", reverse=True)   # stable: at desc, source, id
        return Read(rows=tuple(r for _s, r in merged[:EDITS_LIMIT]), skipped=tuple(skipped))
    return read


def _tag_list(raw: Any) -> list[str]:
    """A crystal's tags as a real list. The store holds them as one comma-joined string inside a
    one-element list (``["operator,ergonomics"]``), so every element is split on commas, trimmed,
    emptied of blanks and de-duplicated in first-seen order."""
    seen: dict[str, None] = {}
    for t in [raw] if isinstance(raw, str) else (raw or []):
        for part in str(t).split(","):
            if part.strip():
                seen.setdefault(part.strip(), None)
    return list(seen)


def _crystals(source: SubstrateSource) -> Callable[[ReadContext], Result]:
    def read(ctx: ReadContext) -> Result:
        path = source.anneal.crystal_json
        if not path.exists():
            return Absent("no crystal store yet")   # no absolute path in a sentence the page shows
        try:
            from anneal_memory.crystal import CrystalStore

            raw = CrystalStore(path).active()   # the ONE read this panel makes; the view's copy is not used
            if not path.exists():           # it was there when the read began: vanishing now is a fault
                return Fault(f"crystal store vanished during the read: {path}")
        except Exception as exc:  # noqa: BLE001 - any store fault is a Fault, not an empty list
            return Fault(f"crystal store unreadable: {type(exc).__name__}: {exc}")
        rows, dropped = [], 0
        for c in raw:
            try:
                name = str(c.get("name") or "(unnamed)")
                level = int(c.get("level", 0) or 0)
                tags = _tag_list(c.get("tags"))
                clause = _one_clause(str(c.get("explanation", "")))
                act = str(c.get("activation_mode", ""))
                perm = str(c.get("permanence", ""))
                last = str(c.get("last_activated_on", ""))
            except (ValueError, TypeError, AttributeError, OverflowError):
                dropped += 1
                continue
            rows.append(RowIn(
                id=f"crystal:{name}", title=name, body=clause,
                facets={"crystal_level": level, "permanence": perm, "last_activated_on": last or None, "tags": tags},
                stored={"name": name, "level": level, "one_clause": clause, "permanence": perm,
                        "activation_mode": act, "last_activated_on": last, "tags": tags}))
        return Read(rows=tuple(rows), skipped=((dropped, "malformed crystal row"),) if dropped else ())
    return read


def _health(source: SubstrateSource) -> Callable[[ReadContext], Result]:
    def read(ctx: ReadContext) -> Result:
        view = _view(source, ctx)
        bad = _view_fault(view, "health")
        if bad:
            return bad
        h = view.health
        if h is None:
            return Fault("health: no health read")
        metrics: list[dict[str, Any]] = [
            {"label": "write path", "value": "live" if h.write_path_live else "dark",
             "unit": None, "status": "ok" if h.write_path_live else "bad", "read": None},
        ]
        # the widget draws the dot first, then the first four whole-number metrics in order (mock-e: links, wraps,
        # graduated, demoted), so those lead; the rest follow for the full panel
        for label, value in (
            ("links", h.total_links), ("wraps", h.total_wraps),
            ("graduated", h.graduations_validated_total), ("demoted", h.graduations_demoted_total),
            ("avg strength", h.avg_strength), ("max strength", h.max_strength),
            ("density", h.density), ("local density", h.local_density),
            ("episodes", h.total_episodes), ("episodes since wrap", h.episodes_since_wrap),
            ("tombstones", h.tombstones),
        ):
            metrics.append({"label": label, "value": value, "unit": None, "status": "unknown", "read": None})
        alerts = [{"message": "a wrap is in progress; this snapshot may be inconsistent", "severity": "warn"}] \
            if h.wrap_in_progress else []
        return Read(value={"metrics": metrics, "alerts": alerts})
    return read


def _graph(source: SubstrateSource) -> Callable[[ReadContext], Result]:
    def read(ctx: ReadContext) -> Result:
        view = _view(source, ctx)
        bad = _view_fault(view, "graph")
        if bad:
            return bad
        g = view.graph
        if g is None:
            return Fault("graph: no graph read")
        text = [{"label": "association graph",
                 "text": f"{len(g.nodes)} nodes, {len(g.edges)} links" + (" (truncated)" if g.truncated else ""),
                 "at": None, "source": None}]
        return Read(value={"visual": "cognition-graph", "data": g.to_dict(), "text": text})
    return read


def _wraps(source: SubstrateSource) -> Callable[[ReadContext], Result]:
    def read(ctx: ReadContext) -> Result:
        view = _view(source, ctx)
        bad = _view_fault(view, "wraps")
        if bad:
            return bad
        last = view.wraps[0].wrapped_at if view.wraps else None
        text = [{"label": "projection history",
                 "text": f"{len(view.wraps)} wraps" + (f", last {last}" if last else ""), "at": last, "source": None}]
        return Read(value={"visual": "wrap-history", "data": [w.to_dict() for w in view.wraps], "text": text})
    return read


def _context_state(source: SubstrateSource) -> tuple[dict[str, Any] | None, Fault | Absent | None]:
    """The live-context file, parsed ONCE: ``(data, None)`` or ``(None, problem)``."""
    cj = source.context_json
    if cj is None:
        return None, Absent("no live-context source is configured")
    if not cj.exists():
        return None, Absent(f"no context file at {cj}")
    try:
        data = json.loads(cj.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, Fault(f"context file unreadable: {type(exc).__name__}: {exc}")
    if not isinstance(data, dict):
        return None, Fault(f"context file is a {type(data).__name__}, not an object")
    return data, None


def _context_line(source: SubstrateSource) -> Callable[[ReadContext], Result]:
    def read(ctx: ReadContext) -> Result:
        # the line shown and the record its version hashes come from ONE read of the file, so a
        # write bound to this version is bound to the content it displayed (§4.4)
        data, problem = _context_state(source)
        if problem is not None:
            return problem
        assert data is not None
        stored = state_record(data)
        item = state_from_data(data, ctx.now)
        if not item.text:
            return Read(value={"lines": []}, version_of=stored)
        # an expired or age-unknown line is already dropped by the reader, so what arrives is fresh. The stamp
        # goes out in ECMA-262's Date Time String Format (what toISOString emits), the one form every JS engine
        # must parse; any other string is implementation-defined, so the client checks the round trip.
        assert item.set_at is not None
        ts = datetime.fromisoformat(item.set_at)
        at = (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
        at = at.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return Read(value={"lines": [{"label": "state", "text": item.text, "at": at, "source": item.source}]},
                    version_of=stored)
    return read


def _prose(source: SubstrateSource, kind: str, ident: str) -> Callable[[ReadContext], Result]:
    def read(ctx: ReadContext) -> Result:
        view = _view(source, ctx)
        bad = _view_fault(view, "sections" if kind == "section" else "config")
        if bad:
            return bad
        pool = view.sections if kind == "section" else view.config_docs
        for d in pool:
            if (d.heading if kind == "section" else d.key) == ident:
                return Read(value={"markdown": d.body, "headline": d.heading if kind == "section" else d.title})
        return Absent(f"{kind} {ident!r} is no longer in the substrate")
    return read


def _jobs(job_store: Any) -> Callable[[ReadContext], Result]:
    from levain.jobs import JobStoreCorruptError

    def read(ctx: ReadContext) -> Result:
        if not job_store.path.exists():
            return Absent(f"no job store at {job_store.path}")
        try:
            recs = job_store.list_recent(ctx.now.isoformat(), 50)
        except JobStoreCorruptError as exc:
            return Fault(f"job store unreadable: {exc}")
        rows = tuple(
            RowIn(
                id=f"job:{r['job_id']}", title=f"{r['verb']} {r['status']}",
                body=json.dumps(r.get("result")) if r.get("result") is not None else (r.get("error") or None),
                facets={"job_status": r["status"], "verb": r["verb"], "at": r["created_at"]},
                stored={"job_id": r["job_id"], "verb": r["verb"], "status": r["status"],
                        "created_at": r["created_at"], "started_at": r.get("started_at"),
                        "finished_at": r.get("finished_at"), "result": r.get("result"), "error": r.get("error")},
            )
            for r in recs
        )
        return Read(rows=rows)
    return read


def _notifications() -> Callable[[ReadContext], Result]:
    """P4, the notification record (Phill 10-19, c1): every ping Levain sent, where it went, whether it was opened, and
    its outcome read from the consent queue now (:mod:`levain.webpush.record`). A display, never an authority."""
    def read(ctx: ReadContext) -> Result:
        from levain.consent.queue import default_queue_dir
        from levain.webpush import record as rec_mod
        from levain.webpush.store import PushStore

        qdir = default_queue_dir()
        if not (qdir / "webpush" / "webpush.db").exists():
            return Read(rows=(), note="No phone has been paired for pings yet (`levain ping test` once one is).")
        rows = []
        try:
            for p in rec_mod.record(qdir, PushStore(qdir).record(limit=rec_mod.SHOWN)):
                outcome = p.get("outcome") or {}
                sent = ", ".join(str(d["status"]) for d in p.get("deliveries") or ()) or "no phone"
                opened = f"opened {len(p['opens'])}x" if p.get("opens") else "not opened"
                meta = f"{p['at']} · {p['kind']} · push {sent} · {opened} · {outcome.get('words', 'outcome not read')}"
                rows.append(RowIn(id=f"ping:{p['id']}", title=p["title"], body=p.get("body") or None,
                                  facets={"at": p["at"], "legacy_meta": meta},
                                  stored={"at": p["at"], "kind": p["kind"], "ref": p.get("ref"), "sent": sent,
                                          "opened": len(p.get("opens") or ()), "outcome": outcome.get("state", "unread")}))
        except Exception as exc:  # noqa: BLE001 - an unreadable record is said, never a dark panel
            return Fault(f"the notification record cannot be read: {type(exc).__name__}")
        return Read(rows=tuple(rows))
    return read


def _slug(heading: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-") or "section"


def section_panel_id(heading: str) -> str:
    """The panel id a neocortex section renders under (before ``discover``'s collision suffix)."""
    return f"section:{_slug(heading)}"


def _anneal_section(source: SubstrateSource, heading: str) -> Callable[[ReadContext], Result]:
    """A writable (Class-A) section, read by anneal's ``Store.read_section``: the text and the version
    ``replace_section`` checks come from one read, so the panel's ``value_version`` is
    ``value_version_of(anneal's version)`` and the broker binds the write to that same version
    (SECTION_ADAPTER_DESIGN.md)."""
    def read(ctx: ReadContext) -> Result:
        db = source.anneal.episodic_db
        if not Path(db).is_file():
            return Fault("the episodic store is absent")
        from anneal_memory import AnnealMemoryError, Store

        try:
            with Store(str(db), read_only=True) as st:
                got = st.read_section(heading)
        except (AnnealMemoryError, OSError, ValueError) as exc:
            return Fault(f"{type(exc).__name__}: {exc}")
        if got is None:
            return Absent(f"section {heading!r} is no longer in the substrate")
        text, version = got
        return Read(value={"markdown": text, "headline": heading}, version_of=version)
    return read


def _section_writer_shipped() -> bool:
    """anneal's section writer (``Store.replace_section``, 0.9.43): ruling (i) on 2026-10-10, so
    no section_edit is offered on an anneal without it."""
    from anneal_memory import Store

    return hasattr(Store, "replace_section")


def _scan_read_one(read: Callable[[ReadContext], Result]) -> Callable[[ReadContext, str], Result]:
    """``read_one`` for a provider whose source is read whole: a FRESH read (never the memoised
    view of a render: ``ctx`` is the write's own, ``Cockpit.read_one`` makes one per read) and the
    row found by id. The caller's context is passed through, so a journal read memoised on its
    authority is read once per request, never per probe (K2b-3b §8d row 6)."""
    def read_one(ctx: ReadContext, row_id: str) -> Result:
        res = read(ctx)
        if not isinstance(res, Read):
            return res
        for r in res.rows or ():
            if r.id == row_id:
                return Read(rows=(r,))
        if res.skipped:
            return Fault(f"row {row_id!r} not found, but the read skipped rows")
        return Absent(f"row {row_id!r} is gone")
    return read_one


# --- Q7a: the agent sessions this install's hooks have seen ---------------------------------------------------
# A DISPLAY, never an authority (DESIGN_Q7 ruling 1): the files are written by hooks running as the operator's uid,
# so any same-uid process can forge one, and nothing here decides anything from them. The words say what was
# measured (ruling 2): the hook fired this long ago, never "running" and never "ended".
SESSIONS_SHOWN = 50                 # the newest records read, by mtime
SESSIONS_SCAN = 1000                # directory entries looked at before the listing gives up counting
SESSION_FILE_MAX = 16 * 1024        # a longer file is not a record of the contract's shape
SESSION_ACTIVE_S = 15 * 60          # Active: last hook event under 15 minutes ago
SESSION_LISTED_S = 24 * 3600        # Quiet until 24 hours; older is not listed
SESSION_SKEW_S = 300                # a last_seen further ahead than this is not a record of ours
SESSION_AGENTS = ("claude_code", "codex")
SESSION_EVENTS = ("SessionStart", "UserPromptSubmit")
# doctor's own list of what the settings readers cannot see (levain/doctor.py, the sandbox check); a test holds the two together
SANDBOX_UNSEEN = "CLAUDE_CONFIG_DIR relocation, --settings, server-managed settings"
SESSIONS_NOTE = ("Sandbox is what the settings files say for each session's working directory, not proof it is confined; "
                 f"not visible to doctor or here: {SANDBOX_UNSEEN}. A session shows when it last fired a hook; "
                 "nothing says when one ends, so a silent one goes Quiet.")
SESSION_FACETS = frozenset({"agent", "last_seen", "source", "session_activity", "legacy_meta"})


def _session_stamp(v: Any) -> datetime | None:
    if not isinstance(v, str) or len(v) > 40:
        return None
    try:
        t = datetime.fromisoformat(v[:-1] + "+00:00" if v.endswith("Z") else v)
    except ValueError:
        return None
    return t if t.tzinfo is not None else None


def _session_record(path: Path) -> dict[str, Any] | None:
    """One registry file (the contract in DESIGN_Q7.md) as a dict, or None for any other shape: a bounded read,
    and a name that is not the hash of the agent and session id it holds is not a record of ours."""
    import hashlib

    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read(SESSION_FILE_MAX + 1)
        if len(raw) > SESSION_FILE_MAX:
            return None
        d = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(d, dict) or d.get("v") != 1 or d.get("agent") not in SESSION_AGENTS:
        return None
    sid, cwd = d.get("session_id"), d.get("cwd")
    if not (isinstance(sid, str) and sid and isinstance(cwd, str)) or d.get("last_event") not in SESSION_EVENTS:
        return None
    if path.stem != hashlib.sha1(f"{d['agent']}:{sid}".encode("utf-8", "replace")).hexdigest()[:24]:
        return None
    if d.get("context_delivered") not in (True, False, None) or _session_stamp(d.get("first_seen")) is None:
        return None
    seen = _session_stamp(d.get("last_seen"))
    if seen is None:
        return None
    return {**d, "_seen": seen, "_stem": path.stem}


def _ago(secs: float) -> str:
    m = int(max(secs, 0) // 60)
    return "just now" if m < 1 else (f"{m}m ago" if m < 60 else f"{m // 60}h ago")


def _sandbox_layer(source: str) -> str:
    p = Path(source)
    if p == Path(os.path.expanduser("~")) / ".claude" / "settings.json":
        return "user settings"
    if p.parent.name == ".claude" and p.name in ("settings.json", "settings.local.json"):
        return "project settings"
    return "managed settings"


def _sandbox_chip(agent: str, cwd: str, cache: dict[str, str]) -> str:
    """Claude Code's sandbox as CONFIGURED for the session's working directory (doctor's reader); never "confined".
    Codex has no reader here: it says so."""
    if agent != "claude_code" or not os.path.isabs(cwd):
        return "sandbox not read"
    if cwd not in cache:
        try:
            from levain import host_guard

            # the session's own directory: Claude Code reads the shared settings.json there, never a root's above it
            on, source = host_guard.cc_sandbox_state(cwd)
            cache[cwd] = f"sandbox configured on ({_sandbox_layer(source)})" if on else "sandbox configured off"
        except Exception:  # noqa: BLE001 - an unreadable settings layer is "not read", never a dark panel
            cache[cwd] = "sandbox not read"
    return cache[cwd]


def _sessions(source: SubstrateSource, consent: Callable[[], Callable[[datetime], Any] | None]
              ) -> Callable[[ReadContext], Result]:
    def read(ctx: ReadContext) -> Result:
        if source.install_root is None:
            # not Absent: an optional tile would read that as "No agent session active", absence drawn as health
            return Fault("not configured: no install root, the sessions are kept in the install")
        sdir = source.install_root / ".levain" / "sessions"
        found: list[tuple[float, Path]] = []
        lost = 0
        try:
            with os.scandir(sdir) as it:
                for n, e in enumerate(it):
                    if n >= SESSIONS_SCAN:
                        break
                    try:
                        if e.name.endswith(".json") and e.is_file(follow_symlinks=False):
                            found.append((e.stat(follow_symlinks=False).st_mtime, Path(e.path)))
                    except OSError:    # pruned or replaced between the listing and the stat: that one entry, not the read
                        lost += 1
        except FileNotFoundError:
            return Read(rows=(), note=SESSIONS_NOTE)
        except OSError as exc:
            return Fault(f"the sessions directory cannot be read: {type(exc).__name__}")
        found.sort(key=lambda p: p[0], reverse=True)
        waiting: dict[tuple[str, str], int] = {}
        consent_read = True
        reader = consent()
        if reader is not None:
            try:
                got = reader(ctx.now)
                # an undecided request waits on a late ruling: the session waits on the operator as for a pending one
                for e in (*got.pending, *got.undecided):
                    if e.state in ("pending", "undecided"):
                        k = (e.request.agent, e.request.agent_session_id)
                        waiting[k] = waiting.get(k, 0) + 1
            except Exception:  # noqa: BLE001 - an unread queue is said, not shown as zero waiting
                consent_read = False
        listed: list[tuple[dict[str, Any], list[str], str, bool]] = []
        bad = 0
        sandbox: dict[str, str] = {}
        for _mt, path in found[:SESSIONS_SHOWN]:
            rec = _session_record(path)
            if rec is None:
                bad += 1
                continue
            age = (ctx.now - rec["_seen"]).total_seconds()
            if age < -SESSION_SKEW_S:
                bad += 1
                continue
            if age > SESSION_LISTED_S:
                continue
            chips = [f"last active {_ago(age)}", _sandbox_chip(rec["agent"], rec["cwd"], sandbox)]
            if rec["context_delivered"] is True:
                chips.append("memory loaded at start")
            elif rec["context_delivered"] is False:
                chips.append("memory not loaded")
            if not consent_read:
                chips.append("consent not read")
            elif n := waiting.get((rec["agent"], rec["session_id"]), 0):
                chips.append(f"{n} waiting")
            work = os.path.basename(rec["cwd"].rstrip("/\\")) or "/"
            work = "".join(c for c in work if c.isprintable())[:64]
            listed.append((rec, chips, work, age < SESSION_ACTIVE_S))
        # Two listed sessions of one agent in one workspace would carry the same title: each such row says when it
        # started (never the session id, ruling 4). Two whose starts round alike (same minute; past an hour, same
        # hour) still read alike (known edge).
        titles: dict[tuple[str, str], int] = {}
        for rec, _chips, work, _active in listed:
            titles[(rec["agent"], work)] = titles.get((rec["agent"], work), 0) + 1
        rows: list[RowIn] = []
        for rec, chips, work, active in listed:
            stored = {"agent": rec["agent"], "workspace": work, "last_seen": rec["last_seen"],
                      "active": active, "chips": chips[1:]}
            started = _session_stamp(rec["first_seen"]) if titles[(rec["agent"], work)] > 1 else None
            meta = chips
            if started is not None and started <= rec["_seen"]:
                # clock-relative words stay out of `stored` (like "last active"); the raw stamp is what it versions
                meta = [chips[0], f"started {_ago((ctx.now - started).total_seconds())}", *chips[1:]]
                stored["first_seen"] = rec["first_seen"]
            rows.append(RowIn(
                id=f"session:{rec['_stem']}", title=f"{rec['agent']} · {work}",
                facets={"agent": rec["agent"], "last_seen": rec["last_seen"], "source": work,
                        "session_activity": "active" if active else "quiet", "legacy_meta": " · ".join(meta)},
                stored=stored))
        skipped = ((bad, "session record that does not have the registry file's shape"),) if bad else ()
        if lost:
            skipped += ((lost, "session record that changed while it was being listed"),)
        more = len(found) - SESSIONS_SHOWN
        filtered = ((more, f"older session records not read (the newest {SESSIONS_SHOWN} are)"),) if more > 0 else ()
        return Read(rows=tuple(rows), skipped=skipped, filtered=filtered, note=SESSIONS_NOTE)
    return read


def build_default_cockpit(
    source: SubstrateSource, *, job_store: Any = None, clock: Callable[[], datetime] | None = None,
    episode_feeds: Mapping[str, Callable[[ReadContext], Result]] | None = None,
    team_start: Path | str | None = None,
) -> Cockpit:
    """The kernel's own cockpit over one substrate: every panel the dashboard already shows,
    as the five kinds (design §6.2). A downstream registers its own providers on the result.

    ``episode_feeds`` federates other writers' episodes into the Episodes panel (a constellation's
    agents): ``{name: read}``, where ``read(ctx)`` returns a ``Read`` of ``RowIn`` rows using only the
    episode facets and stored fields. Each row is shown under ``feed:<name>:<id>`` with ``agent`` set
    (the feed's name when the row names none); the entity's own rows carry the entity's name."""
    feeds = tuple((episode_feeds or {}).items())
    for name, fn in feeds:
        if not isinstance(name, str) or not FEED_NAME.fullmatch(name) or not callable(fn):
            raise ValueError(f"episode feed {name!r}: a name matching {FEED_NAME.pattern} and a callable")

    def entity(ctx: ReadContext) -> dict[str, Any]:
        v = _view(source, ctx)
        # ``jar`` (today's episode count against the entity's own typical day) and ``store_label``
        # (the store's home-relative display path, never the absolute one) are the masthead's.
        return {"name": v.entity_name, "governance": v.scope,
                "brand": {"wordmark": v.brand_wordmark, "model": v.brand_model},
                "jar": v.jar.to_dict() if v.jar else None,
                "store_label": _store_label(source.anneal.episodic_db)}

    ck = Cockpit(entity=entity, clock=clock)
    edits_read = _edits(source, lambda: ck.edits_reader)
    spore_common = dict(
        facets=SPORE_FACETS,
        version_fields=SPORE_VERSION_FIELDS, version_excluded=SPORE_VERSION_EXCLUDED,
        search_fields=("title", "body", "domain"), edit_class=CLASS_B,
    )
    ck.register(ProviderSpec("state", "line", "State", "gauge", _context_line(source),
                             region="brief", rank=0, optional=True, empty="No state line.",
                             verbs=("operator_state",)))
    # mock-e's default layout (design 1011, "mock-e: what it decides"). The WORK zone (`operate`) is a
    # 3-column grid whose cards flow in rank order, `now` first: Now, Tray, Open loops (row a, tall) / [Inbox, Fleet,
    # Stream: downstream-only] / Recent episodes, Keep, Recent edits. Ranks leave gaps so a downstream's
    # Inbox, Fleet and Stream slot in 16-39, between Open loops and Recent episodes without renumbering the kernel's.
    for pid, title, bucket, prio, order, rank, views, size in (
        ("tray", "Tray", "tray", "gate", "spore.tray", 10, TRAY_VIEWS, "tall"),
        ("loops", "Open loops", "loops", "feed", "spore.loops", 15, (), "tall"),
        ("keep", "Keep", "keep", "feed", "spore.keep", 50, (), "medium"),
    ):
        ck.register(ProviderSpec(
            pid, "triage-list", title, prio, _spore_provider(source, bucket), region="operate", rank=rank, size=size,
            order=order, views=views, rowset=f"spore:{bucket}", read_one=_spore_read_one(source, bucket),
            label_key=spore_label_key,
            empty="Nothing waiting.", verbs=SPORE_ROW_VERBS + (("spore_seed",) if bucket == "tray" else ()),
            **spore_common))
    ck.register(ProviderSpec(
        "episodes", "triage-list", "Recent episodes", "feed", _episodes(source, feeds), region="operate", rank=40,
        # a write's read: the entity's own rows only (a feed row is never a tombstone target)
        read_one=_episode_read_one(source), verbs=("episode_tombstone",),
        order="time.desc", rowset="episodes", facets=EPISODE_FACETS,
        version_fields=EPISODE_STORED,
        search_fields=("title", "body", "source"), edit_class=CLASS_B, empty="No recent episodes."))
    ck.register(ProviderSpec(
        "edits", "triage-list", "Recent edits", "feed", edits_read, region="operate", rank=60,
        read_one=_scan_read_one(edits_read), verbs=("undo",), label_key=edit_label_key,
        optional=True, order="time.desc", rowset="edits",
        facets=frozenset({"edit_kind", "at", "undoable"}), version_fields=("*",), empty="No edits yet."))
    if job_store is not None:
        ck.register(ProviderSpec(
            "jobs", "triage-list", "Recent jobs", "feed", _jobs(job_store), region="operate", rank=70,
            optional=True, order="time.desc", rowset="jobs",
            facets=frozenset({"job_status", "verb", "at"}), version_fields=("*",), empty="No jobs yet."))
    ck.register(ProviderSpec(
        "notifications", "triage-list", "Notification record", "feed", _notifications(), region="operate", rank=72,
        optional=True, order="time.desc", facets=frozenset({"at", "legacy_meta"}), version_fields=("*",),
        empty="No pings sent yet."))
    # Q7a: the sessions tile (a count of the Active ones) opening into the focus modal, like Decisions waiting
    ck.register(ProviderSpec(
        "sessions", "triage-list", "Sessions", "gauge", _sessions(source, lambda: ck.consent_reader), region="widgets",
        rank=20, face="count", optional=True, order="session.recency", rowset="sessions", facets=SESSION_FACETS,
        # a row shows its agent and workspace (the title) and its chips (legacy_meta): never the id, never the full cwd
        search_fields=("title", "legacy_meta"),
        version_fields=("*",), empty="No agent session active in the last 15 minutes."))
    ck.register(ProviderSpec("health", "metric", "Health", "gauge", _health(source), region="widgets", rank=40,
                             edit_class=CLASS_C))
    ck.register(ProviderSpec("graph", "visual", "Cognition trace", "feed", _graph(source), region="mind", rank=200,
                             size="short", tabs="Cognition", edit_class=CLASS_C))
    ck.register(ProviderSpec(
        "crystals", "triage-list", "Crystallized patterns", "feed", _crystals(source), region="mind", rank=100,
        size="tall", optional=True, order="crystal.level", rowset="crystals",
        facets=frozenset({"crystal_level", "permanence", "last_activated_on", "tags"}),
        version_fields=("name", "level", "one_clause", "permanence", "activation_mode", "tags"),
        version_excluded=("last_activated_on",), search_fields=("title", "body"), edit_class=CLASS_C,
        empty="No crystals yet."))
    ck.register(ProviderSpec("wraps", "visual", "Projection history", "feed", _wraps(source), region="mind", rank=210,
                             size="short", tabs="Cognition", edit_class=CLASS_C))
    # prose panels: one per neocortex heading and per seed/config doc, DISCOVERED at every read so a
    # heading added later appears and an unreadable file at first sight is a manifest error, not a
    # permanent silent absence.
    def discover(ctx: ReadContext) -> list[ProviderSpec]:
        v = _view(source, ctx)
        for key in ("sections", "config", "store"):
            if key in v.errors and key != "store":
                raise RuntimeError(f"{key}: {v.errors[key]}")
        specs: list[ProviderSpec] = []
        used: set[str] = set()
        from levain.cockpit.anneal_effects import section_paths_agree

        ws = source.write_scope
        # the panel reads the store the broker writes, and that store's continuity is the one named
        writable = (_section_writer_shipped() and ws is not None
                    and section_paths_agree(source.anneal, ws.anneal))
        for i, s_ in enumerate(v.sections):
            pid = section_panel_id(s_.heading)
            while pid in used:
                pid += "-2"
            used.add(pid)
            # a Class-A section is read by anneal (the write's version space) and offers section_edit;
            # the rest render from the dashboard's parse and are never writable
            edit = writable and s_.edit_class == CLASS_A
            specs.append(ProviderSpec(pid, "prose", s_.heading, "feed",
                                      _anneal_section(source, s_.heading) if edit
                                      else _prose(source, "section", s_.heading),
                                      region="mind", rank=i, edit_class=s_.edit_class,
                                      tabs="Neocortex", span=2, size="tall",
                                      verbs=("section_edit",) if edit else (),
                                      label_key=section_label_key(s_.heading) if edit else None))
        for i, d in enumerate(v.config_docs):
            pid = f"config:{d.key}"
            if pid not in used:
                used.add(pid)
                specs.append(ProviderSpec(pid, "prose", d.title, "feed",
                                          _prose(source, "config", d.key), region="identity", rank=i,
                                          edit_class=d.edit_class,
                                          verbs=("config",) if d.edit_class == CLASS_A else ()))
        return specs

    ck.discover(discover)
    # the Team zone: read-only, over the team ledger found from ``team_start`` or, failing that, from the source's
    # own install root; NEVER from the process's working directory (an unrelated repo's ledger is not this
    # source's). A source with neither draws no Team zone. The panel set follows the ledger's state.
    start = team_start if team_start is not None else source.install_root
    if start is not None:
        from levain.cockpit.team_panels import TeamSource, team_discoverer
        ck.discover(team_discoverer(TeamSource(start)), retire=True)
    return ck
