"""levain.cockpit.engine: the kernel half of the shared cockpit's READ side (design §3, K1).

Providers return typed results; this module turns them into the manifest and panel payloads:
status, freshness, ETag, ordering and groups, row versions, bands, badges, profiles, search, the
``now`` view, background refreshers with liveness, and per-call timeouts. It writes nothing and
enforces no authority: K1 is the read half, served under the existing read gate (routes.py)."""

from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from levain.cockpit.registry import (
    FACETS,
    ORDERINGS,
    CockpitRegistrationError,
    apply_ordering,
    parse_date,
    validate_facets,
)
from levain.cockpit.results import Absent, Fault, Read, Result, RowIn

SCHEMA = "levain.cockpit/1"
KINDS = ("triage-list", "line", "metric", "visual", "prose")
PRIORITIES = ("gate", "gauge", "feed")
ZONES = (("operate", "Operate"), ("mind", "Mind"), ("identity", "Identity"))
HEADER = "header"
VISUALS = ("sky", "constellation", "cognition-graph", "wrap-history")
TITLE_MAX = 160
SOON_DAYS = 3
POLICY_REVISION = 1  # hashed into the manifest etag; bumped when a tier or gesture policy changes
NOW_ID = "now"
_STATUS_RANK = {"ok": 0, "empty": 0, "partial": 1, "stale": 2, "error": 3}


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()


def _sha(obj: Any) -> str:
    return hashlib.sha256(_canon(obj)).hexdigest()


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class RowNotFound(LookupError):
    """``?row=<id>`` named a row the panel does not currently hold."""


@dataclass
class ProviderSpec:
    """A panel's registration. ``read(ctx)`` returns a typed result. ``read_one(ctx, row_id)``
    reads the SOURCE (never the refresher snapshot) for one row; every provider that will offer a
    verb implements it (K2a), and K1 carries it in the protocol and tests it."""

    id: str
    kind: str
    title: str
    priority: str
    read: Callable[["ReadContext"], Result]
    region: str = "operate"           # "header" or a zone id
    rank: int = 0
    refresh_every_s: float | None = None
    stale_after_s: float = 900.0
    optional: bool = False
    timeout_s: float = 10.0
    order: str | None = None
    facets: frozenset[str] = frozenset()
    version_fields: tuple[str, ...] = ()      # ("*",) = every stored field is versioned
    version_excluded: tuple[str, ...] = ()    # stored fields named as NOT versioned (clock/derived)
    search_fields: tuple[str, ...] = ()
    search_default_visible: int = 10
    read_one: Callable[["ReadContext", str], Result] | None = None
    note: str = ""
    empty: str = ""
    rowset: str | None = None


class ReadContext:
    """One read cycle: a fixed instant and a memo, so every built-in provider in a cycle sees one
    view of the substrate instead of building its own."""

    def __init__(self, now: datetime) -> None:
        self.now = now
        self.today: date = now.astimezone().date()
        self._memo: dict[str, Future] = {}
        self._lock = threading.Lock()

    def memo(self, key: str, fn: Callable[[], Any]) -> Any:
        with self._lock:
            fut = self._memo.get(key)
            mine = fut is None
            if mine:
                fut = self._memo[key] = Future()
        if mine:
            try:
                fut.set_result(fn())
            except BaseException as exc:  # noqa: BLE001 - re-raised to every waiter
                fut.set_exception(exc)
        return fut.result()


@dataclass
class _Snap:
    status: str
    rows: list[dict[str, Any]] | None
    value: Any
    filtered: list[dict[str, Any]]
    skipped: list[dict[str, Any]]
    as_of: str | None
    error: str | None
    note: str
    stale_hint: bool = False


@dataclass
class _State:
    lock: threading.Lock = field(default_factory=threading.Lock)
    mu: threading.Lock = field(default_factory=threading.Lock)   # guards the fields _process writes
    snap: _Snap | None = None
    ever_present: bool = False
    last_good_as_of: str | None = None
    failing_since: str | None = None
    last_completion: datetime | None = None
    inflight: bool = False
    started: datetime | None = None


class _Refresher:
    def __init__(self, cockpit: "Cockpit", spec: ProviderSpec) -> None:
        self.cockpit, self.spec = cockpit, spec
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name=f"cockpit-refresh-{spec.id}", daemon=True)

    def _run(self) -> None:
        every = float(self.spec.refresh_every_s or 0)
        while not self.stop_event.wait(every):
            self.cockpit.refresh(self.spec.id)

    def halt(self) -> None:
        """Stop looping without recording anything: to a reader this is a dead refresher, which
        the liveness rule must turn into ``error`` (the test hook for "killed")."""
        self.stop_event.set()


class Cockpit:
    def __init__(
        self,
        *,
        entity: Callable[[ReadContext], dict[str, Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._entity_fn = entity
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._specs: dict[str, ProviderSpec] = {}
        self._state: dict[str, _State] = {}
        self._refreshers: dict[str, _Refresher] = {}
        self._rowsets: dict[str, str] = {}
        self._lock = threading.Lock()

    # --- registration ----------------------------------------------------------------
    def register(self, spec: ProviderSpec) -> None:
        sid = spec.id
        if not sid or sid in self._specs or sid == NOW_ID:
            raise CockpitRegistrationError(f"panel id {sid!r} is empty, reserved or already registered")
        if spec.kind not in KINDS:
            raise CockpitRegistrationError(f"{sid}: unknown kind {spec.kind!r}")
        if spec.priority not in PRIORITIES:
            raise CockpitRegistrationError(f"{sid}: unknown priority {spec.priority!r}")
        if spec.region != HEADER and spec.region not in {z for z, _ in ZONES}:
            raise CockpitRegistrationError(f"{sid}: unknown region {spec.region!r}")
        if spec.priority == "gate" and spec.optional:
            raise CockpitRegistrationError(f"{sid}: a gate panel may not register an optional source")
        if spec.refresh_every_s is not None:
            if spec.refresh_every_s <= 0:
                raise CockpitRegistrationError(f"{sid}: refresh_every_s must be positive")
            if spec.stale_after_s > 2 * spec.refresh_every_s:
                raise CockpitRegistrationError(
                    f"{sid}: stale_after_s ({spec.stale_after_s}) must be <= 2 x refresh_every_s "
                    f"({spec.refresh_every_s})"
                )
        for f in spec.facets:
            if f not in FACETS:
                raise CockpitRegistrationError(f"{sid}: unregistered facet {f!r}")
        if spec.kind == "triage-list":
            if spec.order is None or spec.order not in ORDERINGS:
                raise CockpitRegistrationError(f"{sid}: a triage-list names a registered ordering, got {spec.order!r}")
            if not spec.version_fields:
                raise CockpitRegistrationError(f"{sid}: a triage-list declares version_fields")
        elif spec.order is not None:
            raise CockpitRegistrationError(f"{sid}: only a triage-list has an ordering")
        if spec.rowset is not None:
            owner = self._rowsets.get(spec.rowset)
            if owner is not None:
                raise CockpitRegistrationError(
                    f"{sid}: row set {spec.rowset!r} already belongs to panel {owner!r} (one panel per row set)"
                )
            self._rowsets[spec.rowset] = sid
        self._specs[sid] = spec
        self._state[sid] = _State()

    @property
    def panel_ids(self) -> list[str]:
        return list(self._specs)

    # --- lifecycle -------------------------------------------------------------------
    def start(self) -> None:
        """Take the first read of every refresher panel, then start its refresher thread."""
        for spec in self._specs.values():
            if spec.refresh_every_s is None or spec.id in self._refreshers:
                continue
            self._state[spec.id].started = self._clock()
            self.refresh(spec.id)
            r = _Refresher(self, spec)
            self._refreshers[spec.id] = r
            r.thread.start()

    def stop(self) -> None:
        for r in self._refreshers.values():
            r.halt()
        for r in self._refreshers.values():
            r.thread.join(timeout=2)
        self._refreshers.clear()

    def refresher(self, panel_id: str) -> _Refresher | None:
        return self._refreshers.get(panel_id)

    # --- reading ---------------------------------------------------------------------
    def _call(self, spec: ProviderSpec, st: _State | None, fn: Callable[..., Result], *args: Any) -> Result:
        """Run a provider call with a timeout. A call still running from an earlier request is
        not stacked on: it answers ``Fault`` at once, so a hung source costs one thread, not one
        per request. Any exception, including a BaseException, is a Fault."""
        if st is not None:
            with st.lock:
                if st.inflight:
                    return Fault("previous read still running (source hung?)")
                st.inflight = True
        box: list[Result] = []

        def work() -> None:
            try:
                box.append(fn(*args))
            except BaseException as exc:  # noqa: BLE001 - an escaping exception IS a Fault
                box.append(Fault(f"{type(exc).__name__}: {exc}"))
            finally:
                if st is not None:
                    with st.lock:
                        st.inflight = False

        t = threading.Thread(target=work, name=f"cockpit-read-{spec.id}", daemon=True)
        t.start()
        t.join(spec.timeout_s)
        if t.is_alive():
            return Fault(f"timed out after {spec.timeout_s:g}s")
        res = box[0]
        if not isinstance(res, (Read, Absent, Fault)):
            return Fault(f"provider returned {type(res).__name__}, not Read/Absent/Fault")
        return res

    def refresh(self, panel_id: str) -> None:
        """One refresh cycle for a refresher panel (the thread's body; also the test step)."""
        spec, st = self._specs[panel_id], self._state[panel_id]
        ctx = ReadContext(self._clock())
        snap = self._process(spec, st, self._call(spec, st, spec.read, ctx), ctx)
        with st.lock:
            st.snap = snap
            st.last_completion = self._clock()

    def _snap_for(self, spec: ProviderSpec, ctx: ReadContext) -> _Snap:
        st = self._state[spec.id]
        if spec.refresh_every_s is None:
            return self._process(spec, st, self._call(spec, st, spec.read, ctx), ctx)
        with st.lock:
            snap, last, started = st.snap, st.last_completion, st.started
        if snap is None:
            return _Snap("error", None, None, [], [], st.last_good_as_of, "no read yet (refresher not started)", spec.note)
        ref = last or started or ctx.now
        if (ctx.now - ref) > timedelta(seconds=2 * spec.refresh_every_s):
            return _Snap(
                "error", None, None, [], [], st.last_good_as_of,
                f"refresher not reporting since {_iso(ref)}", spec.note,
            )
        return snap

    # --- result -> snapshot ----------------------------------------------------------
    def _process(self, spec: ProviderSpec, st: _State, res: Result, ctx: ReadContext) -> _Snap:
        with st.mu:
            return self._process_locked(spec, st, res, ctx)

    def _process_locked(self, spec: ProviderSpec, st: _State, res: Result, ctx: ReadContext) -> _Snap:
        now_iso = _iso(ctx.now)
        listy = spec.kind == "triage-list"
        if isinstance(res, Fault):
            return self._fault(spec, st, res.message, now_iso)
        if isinstance(res, Absent):
            if spec.optional and not st.ever_present:
                return _Snap("ok", [] if listy else None, None, [], [], now_iso, None,
                             f"not configured: {res.reason}")
            why = "source disappeared" if st.ever_present else "source absent"
            return self._fault(spec, st, f"{why}: {res.reason}", now_iso)
        try:
            snap = self._read_snap(spec, res, ctx, now_iso)
        except (CockpitRegistrationError, ValueError, TypeError, KeyError) as exc:
            return self._fault(spec, st, f"provider output refused: {exc}", now_iso)
        st.ever_present = True
        st.last_good_as_of = snap.as_of
        st.failing_since = None
        return snap

    def _fault(self, spec: ProviderSpec, st: _State, message: str, now_iso: str) -> _Snap:
        if st.failing_since is None:
            st.failing_since = now_iso
        detail = f"{message} (failing since {st.failing_since}"
        detail += f", last good {st.last_good_as_of})" if st.last_good_as_of else ")"
        return _Snap("error", None, None, [], [], st.last_good_as_of, detail, spec.note)

    def _read_snap(self, spec: ProviderSpec, res: Read, ctx: ReadContext, now_iso: str) -> _Snap:
        as_of = res.as_of or now_iso
        filtered = [{"count": c, "reason": r} for c, r in res.filtered if c]
        skipped = [{"count": c, "reason": r} for c, r in res.skipped if c]
        if spec.kind == "triage-list":
            if res.rows is None:
                raise ValueError("a triage-list Read carries rows")
            rows = self._rows(spec, list(res.rows), ctx)
            status = "partial" if skipped else ("empty" if not rows else "ok")
            value = None
        else:
            if res.value is None:
                raise ValueError(f"a {spec.kind} Read carries a value")
            value = self._value(spec, res.value)
            rows = None
            status = "partial" if skipped else ("empty" if self._value_empty(spec.kind, value) else "ok")
        stale = res.stale or (ctx.now - _parse_iso(as_of)) > timedelta(seconds=spec.stale_after_s)
        if stale and _STATUS_RANK["stale"] > _STATUS_RANK[status]:
            status = "stale"
        return _Snap(status, rows, value, filtered, skipped, as_of, None, spec.note, res.stale)

    @staticmethod
    def _value_empty(kind: str, value: Any) -> bool:
        key = {"line": "lines", "metric": "metrics"}.get(kind)
        return bool(key) and not value.get(key)

    @staticmethod
    def _value(spec: ProviderSpec, v: Any) -> Any:
        if not isinstance(v, dict):
            raise ValueError(f"{spec.kind} value must be an object")
        need = {"line": "lines", "metric": "metrics", "prose": "markdown", "visual": "visual"}[spec.kind]
        if need not in v:
            raise ValueError(f"{spec.kind} value lacks {need!r}")
        if spec.kind == "prose":
            v = {**v, "provenance": v.get("provenance")}   # Prose.provenance (rev 9, §3.2.2); null until K2b
        if spec.kind == "visual":
            if v["visual"] not in VISUALS:
                raise ValueError(f"unknown visual {v['visual']!r}")
            if not isinstance(v.get("text"), list):
                raise ValueError("a visual always ships a text projection")
        if spec.kind == "metric":
            for m in v["metrics"]:
                if m.get("status", "unknown") not in ("ok", "warn", "bad", "unknown"):
                    raise ValueError(f"metric status {m.get('status')!r}")
        return v

    def _rows(self, spec: ProviderSpec, rows_in: list[RowIn], ctx: ReadContext) -> list[dict[str, Any]]:
        seen: set[str] = set()
        allowed_stored = set(spec.version_fields) | set(spec.version_excluded)
        for r in rows_in:
            if r.id in seen:
                raise ValueError(f"duplicate row id {r.id!r}")
            seen.add(r.id)
            validate_facets(r.facets)
            undeclared = set(r.facets) - spec.facets
            if undeclared:
                raise CockpitRegistrationError(f"facets {sorted(undeclared)} not declared by panel {spec.id!r}")
            if "*" not in spec.version_fields:
                stray = set(r.stored) - allowed_stored
                if stray:
                    raise ValueError(
                        f"stored fields {sorted(stray)} are neither versioned nor named as excluded"
                    )
        o = ORDERINGS[spec.order]  # type: ignore[index]
        out = []
        for row, group in apply_ordering(spec.order, rows_in, ctx.today):  # type: ignore[arg-type]
            out.append(self._row(spec, o, row, group, ctx.today))
        return out

    @staticmethod
    def row_version(spec: ProviderSpec, stored: dict[str, Any]) -> str:
        if "*" in spec.version_fields:
            payload = {k: v for k, v in stored.items() if k not in spec.version_excluded}
        else:
            payload = {k: stored.get(k) for k in spec.version_fields}
        return _sha(payload)[:16]

    def _row(self, spec: ProviderSpec, o: Any, row: RowIn, group: str | None, today: date) -> dict[str, Any]:
        f = row.facets
        title = " ".join(row.title.split())
        truncated = len(title) > TITLE_MAX
        if truncated:
            title = title[: TITLE_MAX - 1].rstrip() + "…"
        overdue = f.get("overdue_days")
        due = parse_date(f.get("due"))
        badges: list[dict[str, Any]] = []
        if isinstance(overdue, int) and overdue > 0:
            badges.append({"kind": "overdue_days", "value": overdue})
        if isinstance(f.get("age_days"), int):
            badges.append({"kind": "age_days", "value": f["age_days"]})
        if due:
            badges.append({"kind": "due", "value": due.isoformat()})
        if group is not None and group in o.urgent:
            band = "now"
        elif due and (due < today or due == today) and not o.urgent:
            band = "now"
        elif due and 0 < (due - today).days <= SOON_DAYS:
            band = "soon"
        else:
            band = "later"
        return {
            "id": row.id,
            "version": self.row_version(spec, row.stored),
            "group": group,
            "title": title,
            "truncated": truncated,
            "body": row.body,
            "facets": dict(f),
            "emphasis": "accent" if isinstance(overdue, int) and overdue > 0 else row.emphasis,
            "band": band,
            "badges": badges,
            "provenance": None,   # computed at render from a row's signature envelope (K2b)
            "actions": [],        # verbs and their tiers arrive with K2a
        }

    # --- heads, panels, the now view --------------------------------------------------
    def _etag_of(self, snap: _Snap, groups: list[dict[str, Any]] | None, cred: str, today: date) -> str:
        # Rev 9 (§3.4): the panel etag covers status, rows or value, the credential class and the
        # current date in the install's zone (a held date passing at midnight changes row bands, and
        # from K2a the tiers). ``as_of`` stays outside, so a refresher over unchanged rows still 304s.
        return _sha({"status": snap.status, "rows": snap.rows, "value": snap.value,
                     "filtered": snap.filtered, "skipped": snap.skipped, "error": snap.error,
                     "note": snap.note, "groups": groups, "credential": cred, "date": today.isoformat()})

    def _head(self, spec: ProviderSpec, snap: _Snap, cred: str, today: date) -> dict[str, Any]:
        listy = spec.kind == "triage-list"
        count = len(snap.rows) if (listy and snap.rows is not None) else (0 if listy and snap.status != "error" else None)
        groups = None
        if listy and spec.order and ORDERINGS[spec.order].groups:
            tally: dict[str, int] = {}
            for r in snap.rows or []:
                tally[r["group"]] = tally.get(r["group"], 0) + 1
            groups = [{"id": gid, "title": t, "count": tally.get(gid, 0)} for gid, t in ORDERINGS[spec.order].groups]
        title = spec.title
        if snap.status == "error":
            title += " (unavailable)"
        elif count is not None:
            title += f" ({count})"
        return {
            "id": spec.id, "kind": spec.kind, "title": title, "priority": spec.priority,
            "rank": spec.rank, "status": snap.status,
            "count": None if snap.status == "error" else count,
            "filtered": snap.filtered, "skipped": snap.skipped,
            "as_of": snap.as_of, "stale_after_s": spec.stale_after_s,
            "refresh_every_s": spec.refresh_every_s, "etag": self._etag_of(snap, groups, cred, today),
            "error": snap.error, "note": snap.note, "empty": spec.empty,
            "order": spec.order, "groups": groups,
            "search": ({"fields": list(spec.search_fields), "default_visible": spec.search_default_visible}
                       if spec.search_fields else None),
            "actions": [],
        }

    def _now_head_and_rows(self, ctx: ReadContext, cred: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """The ``now`` view (design §3.2.4): the ``band == "now"`` rows of the gate panels, in panel
        rank order, each panel's rows in that panel's own order. No ordering, provider or actions of
        its own; every row keeps its source ``panel_id``. Its status is the worst of its sources'."""
        sources = sorted(
            (s for s in self._specs.values() if s.priority == "gate" and s.kind == "triage-list"),
            key=lambda s: (0 if s.region == HEADER else 1 + [z for z, _ in ZONES].index(s.region), s.rank, s.id),
        )
        rows: list[dict[str, Any]] = []
        degraded: list[str] = []
        lines: list[str] = []
        worst = "ok"
        as_ofs: list[str] = []
        for s in sources:
            snap = self._snap_for(s, ctx)
            if snap.status in ("error", "stale"):
                degraded.append(s.id)
                lines.append(f"{s.id}: {snap.status}, last good {snap.as_of or 'never'}")
            if _STATUS_RANK[snap.status] > _STATUS_RANK[worst]:
                worst = snap.status
            if snap.as_of:
                as_ofs.append(snap.as_of)
            for r in snap.rows or []:
                if r["band"] == "now":
                    rows.append({**r, "panel_id": s.id})
        if worst in ("ok", "empty"):
            worst = "ok" if rows else "empty"
        err = None
        if worst == "error":
            err = "; ".join(lines) or "a source panel is in error"
        snap = _Snap(worst, rows, None, [], [], min(as_ofs) if as_ofs else None, err,
                     "; ".join(lines))
        head = {
            "id": NOW_ID, "kind": "triage-list", "title": f"Now ({len(rows)})" if worst != "error" else "Now (unavailable)",
            "priority": "gate", "rank": -100, "status": worst, "count": len(rows) if worst != "error" else None,
            "filtered": [], "skipped": [], "as_of": snap.as_of, "stale_after_s": 0, "refresh_every_s": None,
            "etag": self._etag_of(snap, None, cred, ctx.today), "error": err, "note": snap.note,
            "empty": "Nothing needs you now.", "order": None, "groups": None, "search": None,
            "actions": [], "degraded": degraded,
        }
        return head, rows

    def _entity(self, ctx: ReadContext) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        base = {"name": None, "governance": None, "brand": {"wordmark": None, "model": None}}
        if self._entity_fn is None:
            return base, []
        try:
            return {**base, **self._entity_fn(ctx)}, []
        except Exception as exc:  # noqa: BLE001 - an entity fault is a manifest error, not a 500
            return base, [{"source": "entity", "message": f"{type(exc).__name__}: {exc}"}]

    def _ordered_specs(self) -> list[ProviderSpec]:
        zi = [z for z, _ in ZONES]
        return sorted(self._specs.values(),
                      key=lambda s: (-1 if s.region == HEADER else zi.index(s.region), s.rank, s.id))

    def manifest(self, credential: dict[str, Any], *, install_class: str | None = None) -> dict[str, Any]:
        ctx = ReadContext(self._clock())
        entity, errors = self._entity(ctx)
        heads: dict[str, dict[str, Any]] = {}
        snaps: dict[str, _Snap] = {}
        for spec in self._ordered_specs():
            snaps[spec.id] = self._snap_for(spec, ctx)
            heads[spec.id] = self._head(spec, snaps[spec.id], credential["class"], ctx.today)
        now_head, _ = self._now_head_and_rows(ctx, credential["class"])
        heads[NOW_ID] = now_head
        specs = self._ordered_specs()
        regions = {
            "header": [s.id for s in specs if s.region == HEADER],
            "zones": [
                {"id": zid, "title": zt,
                 "panels": ([NOW_ID] if zid == "operate" else []) + [s.id for s in specs if s.region == zid]}
                for zid, zt in ZONES
            ],
        }
        order = [NOW_ID] + [s.id for s in specs]
        etag = _sha({"panels": [heads[i]["etag"] for i in order], "credential": credential,
                     "install_class": install_class, "policy_revision": POLICY_REVISION})
        return {
            "schema": SCHEMA, "entity": entity, "generated_at": _iso(ctx.now), "etag": etag,
            "credential": credential, "regions": regions,
            "panels": {i: heads[i] for i in order}, "verbs": {}, "errors": errors,
        }

    def panel(self, panel_id: str, *, profile: str = "full", q: str | None = None,
              row: str | None = None, credential_class: str = "none") -> dict[str, Any] | None:
        """The ``Panel`` payload, or None for an unknown panel id. ``profile`` is ``full`` or
        ``compact``; ``q`` is the server-side search over row bodies; ``row`` returns one row in
        full."""
        ctx = ReadContext(self._clock())
        if panel_id == NOW_ID:
            head, rows = self._now_head_and_rows(ctx, credential_class)
            spec = None
            snap_rows: list[dict[str, Any]] | None = rows
            value = None
        else:
            spec = self._specs.get(panel_id)
            if spec is None:
                return None
            snap = self._snap_for(spec, ctx)
            head = self._head(spec, snap, credential_class, ctx.today)
            snap_rows, value = snap.rows, snap.value
        out: dict[str, Any] = dict(head)
        out["rows"], out["value"], out["next"] = None, None, None
        if head["status"] == "error" and panel_id != NOW_ID:
            return out    # an error never carries rows or a value: nothing stale passes as a read
        if head["kind"] == "triage-list":
            rows_all = snap_rows or []
            if row is not None:
                hit = [r for r in rows_all if r["id"] == row]
                out["rows"] = hit
                if not hit:
                    raise RowNotFound(row)
                return out
            matched = None
            if q:
                fields = spec.search_fields if spec else ()
                needle = q.casefold()
                keep = [r for r in rows_all if any(needle in _field_text(r, f).casefold() for f in fields)]
                matched = len(keep)
                rows_all = keep
            if profile == "compact":
                if q or head["priority"] == "gate":
                    out["rows"] = [{**r, "body": None} for r in rows_all]
                else:
                    out["rows"] = None
                    out["next"] = f"/cockpit/panel/{panel_id}.json?profile=full"
            else:
                out["rows"] = rows_all
            if matched is not None:
                out["matched"] = matched
        else:
            if profile == "compact":
                out["value"] = _compact_value(head["kind"], value)
                if head["priority"] == "feed" or head["kind"] in ("visual", "prose"):
                    out["next"] = f"/cockpit/panel/{panel_id}.json?profile=full"
                if head["priority"] == "feed" and head["kind"] in ("line", "metric"):
                    out["value"] = None
            else:
                out["value"] = value
        return out

    def read_one(self, panel_id: str, row_id: str) -> Result:
        """Read ONE row from the SOURCE through the provider's ``read_one``, never from a refresher
        snapshot (design §4.4). Returns ``Read`` with the finished row dict in ``Read.value``,
        ``Absent`` when the row is gone, or ``Fault``."""
        spec = self._specs.get(panel_id)
        if spec is None or spec.read_one is None:
            return Fault(f"panel {panel_id!r} offers no read_one")
        ctx = ReadContext(self._clock())
        res = self._call(spec, None, spec.read_one, ctx, row_id)
        if not isinstance(res, Read):
            return res
        try:
            rows = list(res.rows or ())
            if len(rows) != 1:
                return Fault(f"read_one returned {len(rows)} rows")
            validate_facets(rows[0].facets)
            return Read(value=self._row(spec, ORDERINGS[spec.order], rows[0], None, ctx.today))  # type: ignore[index]
        except (CockpitRegistrationError, ValueError, TypeError) as exc:
            return Fault(f"provider output refused: {exc}")


def _parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _field_text(row: dict[str, Any], field_name: str) -> str:
    if field_name in ("title", "body", "id"):
        return str(row.get(field_name) or "")
    v = row["facets"].get(field_name)
    if isinstance(v, list):
        return " ".join(map(str, v))
    return "" if v is None else str(v)


def _compact_value(kind: str, value: Any) -> Any:
    if value is None:
        return None
    if kind == "visual":
        return {"visual": value["visual"], "text": value["text"]}
    if kind == "prose":
        return {"headline": value.get("headline", "")}
    return value
