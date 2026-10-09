"""levain.cockpit.providers: the built-in providers over ``SubstrateView`` (design §6.2).

``SubstrateView`` degrades quietly: a missing spore file, a missing crystal file, a missing edit
ledger and an unreadable context file all arrive as an EMPTY list, which a naive provider would
render as "nothing waiting". So each provider here checks its own source first and returns
``Absent`` / ``Fault`` where the view would have hidden it (the typed-result rule, §3.4), and the
edit ledger is read directly because ``recent_edits`` swallows every fault."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from levain.cockpit.engine import Cockpit, ProviderSpec, ReadContext
from levain.cockpit.registry import parse_date
from levain.cockpit.results import Absent, Fault, Read, Result, RowIn
from levain.dashboard import FOCUS_STALE_AFTER_HOURS, SubstrateSource, SubstrateView

# The view caps each spore bucket at ``max_spores``. The cockpit asks for far more than any store
# holds and DISCLOSES a cap that is still hit, so a panel never shows a prefix as the whole set.
SPORE_CAP = 5000
EDITS_LIMIT = 20
# An undated handoff stops leading after this many days untouched: it says "pick up here next
# session", and a calendar that has refuted that must not keep it first (flow, 2026-08-27, where
# one led the queue for 26 days). A dated handoff never expires.
HANDOFF_FRESH_DAYS = 7

SPORE_STORED = ("id", "text", "disposition", "tier", "type", "domain", "next", "pointer", "salience", "seen")
SPORE_FACETS = frozenset({
    "disposition", "domain", "tier", "salience", "spore_type", "due", "overdue_days",
    "handoff_expired", "last_seen",
})


def _view(source: SubstrateSource, ctx: ReadContext) -> SubstrateView:
    return ctx.memo("view", lambda: source.build(max_spores=SPORE_CAP + 1, now=ctx.now))


def _view_fault(view: SubstrateView, *keys: str) -> Fault | None:
    for k in (*keys, "store"):
        if k in view.errors:
            return Fault(f"{k}: {view.errors[k]}")
    return None


def _spore_row(s: Any, today: date) -> RowIn:
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
    if due:
        facets["overdue_days"] = max(0, (today - due).days)
    return RowIn(
        id=f"spore:{s.id}", title=s.text, body=s.text, facets=facets,
        stored={"id": s.id, "text": s.text, "disposition": s.disposition, "tier": s.tier,
                "type": s.type, "domain": s.domain, "next": s.next, "pointer": s.pointer,
                "salience": s.salience, "seen": s.seen},
    )


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
        capped = len(items) > SPORE_CAP
        items = items[:SPORE_CAP]
        filtered = 0
        rows = []
        for s in items:
            if apply_hold and bucket == "tray" and (d := parse_date(s.next)) and d > ctx.today:
                filtered += 1           # surface date not reached: held on purpose, not unreadable
                continue
            rows.append(_spore_row(s, ctx.today))
        skipped = ((1, f"capped at {SPORE_CAP}; at least this many more exist"),) if capped else ()
        return Read(rows=tuple(rows), filtered=((filtered, "surface date not reached"),) if filtered else (),
                    skipped=skipped)
    return read


def _spore_read_one(source: SubstrateSource, bucket: str) -> Callable[[ReadContext, str], Result]:
    def read_one(ctx: ReadContext, row_id: str) -> Result:
        # a fresh read of the SOURCE (its own context, never the cycle's memoised view)
        fresh = ReadContext(ctx.now)
        # the row by its id, NOT through the list's visibility rules: a row rescheduled into the
        # future since the render still exists, and a write must find it
        res = _spore_provider(source, bucket, apply_hold=False)(fresh)
        if not isinstance(res, Read):
            return res
        for r in res.rows or ():
            if r.id == row_id:
                return Read(rows=(r,))
        return Absent(f"row {row_id!r} is gone")
    return read_one


def _ts(v: Any) -> str:
    return str(v) if v is not None else ""


def _episodes(source: SubstrateSource) -> Callable[[ReadContext], Result]:
    def read(ctx: ReadContext) -> Result:
        view = _view(source, ctx)
        bad = _view_fault(view, "episodes")
        if bad:
            return bad
        rows = tuple(
            RowIn(
                id=f"episode:{e.id}", title=e.content, body=e.content,
                facets={"episode_type": e.type, "source": e.source, "at": _ts(e.timestamp), "tags": list(e.tags)},
                stored={"id": e.id, "timestamp": e.timestamp, "type": e.type, "source": e.source,
                        "content": e.content, "tags": list(e.tags)},
            )
            for e in view.episodes
        )
        return Read(rows=rows)
    return read


def _ledger_path(source: SubstrateSource) -> Path | None:
    if source.write_scope is not None:
        return source.write_scope.ledger_root / "edits.jsonl"
    if source.install_root is not None:
        return source.install_root / ".levain" / "edits.jsonl"
    return None


def _edits(source: SubstrateSource) -> Callable[[ReadContext], Result]:
    def read(ctx: ReadContext) -> Result:
        path = _ledger_path(source)
        if path is None:
            return Absent("no edit ledger is configured for this source")
        if not path.exists():
            return Absent(f"no edit ledger at {path}")
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
                facets={"edit_kind": str(r.get("kind", "")), "at": _ts(r.get("ts")), "undoable": bool(r.get("undoable"))},
                stored=r,
            )
            for rid, r in reversed(recs[-EDITS_LIMIT:])
        )
        return Read(rows=rows, skipped=((bad, "unreadable ledger line"),) if bad else ())
    return read


def _crystals(source: SubstrateSource) -> Callable[[ReadContext], Result]:
    def read(ctx: ReadContext) -> Result:
        path = source.anneal.crystal_json
        if not path.exists():
            return Absent(f"no crystal store at {path}")
        view = _view(source, ctx)
        if "crystal_index" in view.errors:
            return Fault(f"crystal_index: {view.errors['crystal_index']}")
        if not path.exists():
            return Absent(f"no crystal store at {path}")
        try:
            from anneal_memory.crystal import CrystalStore

            raw = len(CrystalStore(path).active())
        except Exception as exc:  # noqa: BLE001 - the view read it; a second read failing is a fault too
            return Fault(f"crystal store unreadable: {type(exc).__name__}: {exc}")
        dropped = max(0, raw - len(view.crystal_index))   # the view skips a malformed row without a trace
        rows = tuple(
            RowIn(
                id=f"crystal:{c.name}", title=c.name, body=c.one_clause,
                facets={"crystal_level": c.level, "permanence": c.permanence,
                        "last_activated_on": c.last_activated_on or None, "tags": list(c.tags)},
                stored={"name": c.name, "level": c.level, "one_clause": c.one_clause,
                        "permanence": c.permanence, "activation_mode": c.activation_mode,
                        "last_activated_on": c.last_activated_on, "tags": list(c.tags)},
            )
            for c in view.crystal_index
        )
        return Read(rows=rows, skipped=((dropped, "malformed crystal row"),) if dropped else ())
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
        for label, value in (
            ("links", h.total_links), ("avg strength", h.avg_strength), ("density", h.density),
            ("episodes", h.total_episodes), ("episodes since wrap", h.episodes_since_wrap),
            ("tombstones", h.tombstones), ("wraps", h.total_wraps),
            ("graduations validated", h.graduations_validated_total),
            ("graduations demoted", h.graduations_demoted_total),
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


def _context_state(source: SubstrateSource) -> tuple[Path | None, Fault | Absent | None]:
    cj = source.context_json
    if cj is None:
        return None, Absent("no live-context source is configured")
    if not cj.exists():
        return cj, Absent(f"no context file at {cj}")
    try:
        data = json.loads(cj.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return cj, Fault(f"context file unreadable: {type(exc).__name__}: {exc}")
    if not isinstance(data, dict):
        return cj, Fault(f"context file is a {type(data).__name__}, not an object")
    return cj, None


def _context_line(source: SubstrateSource, label: str) -> Callable[[ReadContext], Result]:
    def read(ctx: ReadContext) -> Result:
        _cj, problem = _context_state(source)
        if problem is not None:
            return problem
        view = _view(source, ctx)
        item = view.focus if label == "focus" else view.state
        if item is None or not item.text:
            return Read(value={"lines": []})
        stale = False
        if label == "focus":
            # an unknown age is never rendered as current (unknown != fresh)
            stale = bool(item.stale) or item.freshness != "fresh"
        return Read(value={"lines": [{"label": label, "text": item.text, "at": item.set_at,
                                      "source": item.source}]}, stale=stale)
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


def _slug(heading: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-") or "section"


def build_default_cockpit(
    source: SubstrateSource, *, job_store: Any = None, clock: Callable[[], datetime] | None = None
) -> Cockpit:
    """The kernel's own cockpit over one substrate: every panel the dashboard already shows,
    as the five kinds (design §6.2). A downstream registers its own providers on the result."""

    def entity(ctx: ReadContext) -> dict[str, Any]:
        v = _view(source, ctx)
        return {"name": v.entity_name, "governance": v.scope,
                "brand": {"wordmark": v.brand_wordmark, "model": v.brand_model}}

    ck = Cockpit(entity=entity, clock=clock)
    spore_common = dict(
        facets=SPORE_FACETS,
        version_fields=tuple(f for f in SPORE_STORED if f != "seen"), version_excluded=("seen",),
        search_fields=("title", "body", "domain"),
    )
    ck.register(ProviderSpec("focus", "line", "Focus", "gauge", _context_line(source, "focus"),
                             region="header", rank=0, optional=True,
                             stale_after_s=FOCUS_STALE_AFTER_HOURS * 3600, empty="No focus set."))
    ck.register(ProviderSpec("state", "line", "State", "gauge", _context_line(source, "state"),
                             region="header", rank=1, optional=True, empty="No state line."))
    for pid, title, bucket, prio, order, rank in (
        ("tray", "Tray", "tray", "gate", "spore.tray", 0),
        ("loops", "Open loops", "loops", "feed", "spore.loops", 1),
        ("keep", "Keep", "keep", "feed", "spore.keep", 2),
    ):
        ck.register(ProviderSpec(
            pid, "triage-list", title, prio, _spore_provider(source, bucket), region="operate", rank=rank,
            order=order, rowset=f"spore:{bucket}", read_one=_spore_read_one(source, bucket),
            empty="Nothing waiting.", **spore_common))
    ck.register(ProviderSpec(
        "episodes", "triage-list", "Recent episodes", "feed", _episodes(source), region="operate", rank=3,
        order="time.desc", rowset="episodes", facets=frozenset({"episode_type", "source", "at", "tags"}),
        version_fields=("id", "timestamp", "type", "source", "content", "tags"),
        search_fields=("title", "body", "source"), empty="No recent episodes."))
    ck.register(ProviderSpec(
        "edits", "triage-list", "Recent edits", "feed", _edits(source), region="operate", rank=4,
        optional=True, order="time.desc", rowset="edits",
        facets=frozenset({"edit_kind", "at", "undoable"}), version_fields=("*",), empty="No edits yet."))
    if job_store is not None:
        ck.register(ProviderSpec(
            "jobs", "triage-list", "Recent jobs", "feed", _jobs(job_store), region="operate", rank=5,
            optional=True, order="time.desc", rowset="jobs",
            facets=frozenset({"job_status", "verb", "at"}), version_fields=("*",), empty="No jobs yet."))
    ck.register(ProviderSpec("health", "metric", "Health", "gauge", _health(source), region="mind", rank=0))
    ck.register(ProviderSpec("graph", "visual", "Cognition trace", "feed", _graph(source), region="mind", rank=1))
    ck.register(ProviderSpec(
        "crystals", "triage-list", "Crystallized patterns", "feed", _crystals(source), region="mind", rank=2,
        optional=True, order="crystal.level", rowset="crystals",
        facets=frozenset({"crystal_level", "permanence", "last_activated_on", "tags"}),
        version_fields=("name", "level", "one_clause", "permanence", "activation_mode", "tags"),
        version_excluded=("last_activated_on",), search_fields=("title", "body"), empty="No crystals yet."))
    ck.register(ProviderSpec("wraps", "visual", "Projection history", "feed", _wraps(source), region="mind", rank=9))
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
        for i, s_ in enumerate(v.sections):
            pid = f"section:{_slug(s_.heading)}"
            while pid in used:
                pid += "-2"
            used.add(pid)
            specs.append(ProviderSpec(pid, "prose", s_.heading, "feed",
                                      _prose(source, "section", s_.heading), region="mind", rank=20 + i))
        for i, d in enumerate(v.config_docs):
            pid = f"config:{d.key}"
            if pid not in used:
                used.add(pid)
                specs.append(ProviderSpec(pid, "prose", d.title, "feed",
                                          _prose(source, "config", d.key), region="identity", rank=i))
        return specs

    ck.discover(discover)
    return ck
