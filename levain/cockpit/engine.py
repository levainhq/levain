"""levain.cockpit.engine: the kernel half of the shared cockpit's READ side (design §3, K1).

Providers return typed results; this module turns them into the manifest and panel payloads:
status, freshness, ETag, ordering and groups, row versions, bands, badges, profiles, search, the
``now`` view, background refreshers with liveness, and per-call timeouts. It writes nothing and
enforces no authority: K1 is the read half, served under the existing read gate (routes.py)."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from levain.cockpit.registry import (
    FACETS,
    ORDERINGS,
    CockpitRegistrationError,
    FacetValueError,
    apply_ordering,
    parse_date,
    validate_facets,
)
from levain.cockpit.results import Absent, Fault, Read, Result, RowIn

SCHEMA = "levain.cockpit/1"
ENTITY_TIMEOUT_S = 10.0
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

    def __init__(self, now: datetime, *, memo_timeout_s: float = 10.0) -> None:
        self.now = now
        self.today: date = now.astimezone().date()
        self._memo: dict[str, Future] = {}
        self._dead: set[str] = set()
        self._memo_timeout_s = memo_timeout_s
        self._lock = threading.Lock()

    def abandon(self) -> None:
        """A bounded call that wraps this cycle's reads timed out: every shared read still running
        is dead for the rest of the cycle, so the next reader fails at once."""
        with self._lock:
            self._dead |= {k for k, f in self._memo.items() if not f.done()}

    def memo(self, key: str, fn: Callable[[], Any]) -> Any:
        """Run ``fn`` once per cycle and share its result. A waiter gives up after the memo
        timeout, and once one has, the key is dead for the rest of the cycle: every later reader
        fails at once instead of each paying its own timeout behind one hung read."""
        with self._lock:
            if key in self._dead:
                raise TimeoutError(f"shared read {key!r} did not finish")
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
        try:
            return fut.result(timeout=self._memo_timeout_s)
        except FutureTimeout:
            with self._lock:
                self._dead.add(key)
            raise TimeoutError(f"shared read {key!r} did not finish in {self._memo_timeout_s:g}s") from None


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
    flight: "Future | None" = None            # the read currently running, shared by concurrent callers
    flight_started: float = 0.0               # monotonic start of that read
    refresh_started: datetime | None = None   # set while a refresher cycle is inside its read
    started: datetime | None = None


class _Refresher:
    def __init__(self, cockpit: "Cockpit", spec: ProviderSpec) -> None:
        self.cockpit, self.spec = cockpit, spec
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name=f"cockpit-refresh-{spec.id}", daemon=True)

    def _run(self) -> None:
        every = float(self.spec.refresh_every_s or 0)
        while not self.stop_event.wait(every):
            try:
                self.cockpit.refresh(self.spec.id)
            except Exception:  # noqa: BLE001 - the loop outlives one bad cycle; the panel reads error
                continue

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
        self._discoverers: list[Callable[[ReadContext], list[ProviderSpec]]] = []
        self._entity_state = _State()
        self._discovery_states: list[_State] = []
        self._entity_timeout_s = ENTITY_TIMEOUT_S
        self._lock = threading.Lock()

    # --- registration ----------------------------------------------------------------
    def register(self, spec: ProviderSpec) -> None:
        """Add a panel. The registry is copy-on-write: a registration builds NEW dicts and swaps
        them in, and nothing ever mutates a dict a reader holds, so a request that took a snapshot
        iterates a stable set with no lock and a concurrent discovery cannot corrupt it."""
        with self._lock:
            self._register_locked(spec)

    def _register_locked(self, spec: ProviderSpec) -> None:
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
        if spec.rowset is not None:
            self._rowsets[spec.rowset] = sid
        states = dict(self._state)
        states[sid] = _State()
        self._state = states            # state first: a reader that sees the spec finds its state
        specs = dict(self._specs)
        specs[sid] = spec
        self._specs = specs

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
            r.thread.start()
            self._refreshers[spec.id] = r

    def stop(self) -> list[str]:
        """Halt every refresher and wait out any read it is inside (bounded by that provider's
        timeout). A refresher still alive after that stays registered, so a later ``start()`` cannot
        overlap it; the result says which."""
        for r in self._refreshers.values():
            r.halt()
        stuck = []
        for pid, r in list(self._refreshers.items()):
            if r.thread.ident is None:      # never started: nothing to join
                del self._refreshers[pid]
                continue
            r.thread.join(timeout=r.spec.timeout_s + 1)
            if r.thread.is_alive():
                stuck.append(pid)
            else:
                del self._refreshers[pid]
        return stuck

    def refresher(self, panel_id: str) -> _Refresher | None:
        return self._refreshers.get(panel_id)

    # --- reading ---------------------------------------------------------------------
    def _call(self, spec: ProviderSpec, st: _State | None, fn: Callable[..., Result], *args: Any) -> Result:
        """Run a provider call with a timeout, SINGLE-FLIGHT per panel: a request that arrives while
        a read is running waits on that same read (up to the timeout) instead of starting another or
        failing, so concurrent requests never turn a healthy panel into a false error, and a hung
        source costs one thread, not one per request. Any exception is a Fault."""
        return self._bounded(spec.timeout_s, st, fn, *args)

    def _bounded(self, timeout_s: float, st: _State | None, fn: Callable[..., Any], *args: Any) -> Any:
        fut: Future
        wait = timeout_s
        if st is not None:
            with st.lock:
                if st.flight is not None and not st.flight.done():
                    # Join a read only while it is still inside its own budget (the singleflight
                    # pattern). One already past it is hung: fail at once, so a hung source costs
                    # neither a thread per request nor a full timeout per request.
                    age = time.monotonic() - st.flight_started
                    if age >= timeout_s:
                        return Fault("previous read still running past its timeout (source hung?)")
                    fut, mine, wait = st.flight, False, timeout_s - age
                else:
                    fut = st.flight = Future()
                    st.flight_started = time.monotonic()
                    mine = True
        else:
            fut, mine = Future(), True
        if mine:
            def work() -> None:
                try:
                    out: Any = fn(*args)
                except BaseException as exc:  # noqa: BLE001 - an escaping exception IS a Fault
                    out = Fault(f"{type(exc).__name__}: {exc}")
                fut.set_result(out)

            try:
                threading.Thread(target=work, name="cockpit-read", daemon=True).start()
            except BaseException as exc:  # noqa: BLE001 - thread exhaustion: fail THIS read, poison nothing
                out = Fault(f"could not start a read: {type(exc).__name__}: {exc}")
                fut.set_result(out)
                if st is not None:
                    with st.lock:
                        if st.flight is fut:
                            st.flight = None
                return out
        try:
            res = fut.result(timeout=wait)
        except FutureTimeout:
            for a in args:
                if isinstance(a, ReadContext):
                    a.abandon()
            return Fault(f"timed out after {timeout_s:g}s")
        if not isinstance(res, (Read, Absent, Fault)):
            return Fault(f"provider returned {type(res).__name__}, not Read/Absent/Fault")
        return res

    def refresh(self, panel_id: str) -> None:
        """One refresh cycle for a refresher panel (the thread's body; also the test step)."""
        spec, st = self._specs[panel_id], self._state[panel_id]
        ctx = ReadContext(self._clock())
        with st.lock:
            st.refresh_started = ctx.now
        try:
            snap = self._process(spec, st, self._call(spec, st, spec.read, ctx), ctx)
        except Exception as exc:  # noqa: BLE001 - a refresh that cannot even be processed is an error read
            snap = self._fault(spec, st, f"{type(exc).__name__}: {exc}", _iso(ctx.now))
        with st.lock:
            st.snap = snap
            st.last_completion = self._clock()
            st.refresh_started = None

    def _snap_for(self, spec: ProviderSpec, ctx: ReadContext) -> _Snap:
        st = self._state[spec.id]
        if spec.refresh_every_s is None:
            return self._process(spec, st, self._call(spec, st, spec.read, ctx), ctx)
        with st.lock:
            snap, last, started, running = st.snap, st.last_completion, st.started, st.refresh_started
        if snap is None:
            return _Snap("error", None, None, [], [], st.last_good_as_of, "no read yet (refresher not started)", spec.note)
        ref = last or started or ctx.now
        # A cycle still inside its own read, within its timeout, is alive; only silence beyond that
        # (or beyond twice the interval with nothing running) means the refresher is dead.
        busy = running is not None and (ctx.now - running) <= timedelta(seconds=spec.timeout_s)
        if not busy and (ctx.now - ref) > timedelta(seconds=2 * spec.refresh_every_s):
            return _Snap(
                "error", None, None, [], [], st.last_good_as_of,
                f"refresher not reporting since {_iso(ref)}", spec.note,
            )
        # a cached snapshot ages: re-evaluate its freshness against THIS request's clock
        if snap.status in ("ok", "empty", "partial") and snap.as_of and \
                (ctx.now - _parse_iso(snap.as_of)) > timedelta(seconds=spec.stale_after_s):
            return _Snap("stale", snap.rows, snap.value, snap.filtered, snap.skipped, snap.as_of,
                         snap.error, snap.note, snap.stale_hint)
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
        except Exception as exc:  # noqa: BLE001 - malformed provider output of ANY shape is a Fault, never a crash
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
            rows, refused = self._rows(spec, list(res.rows), ctx)
            if refused:
                skipped.append({"count": refused, "reason": "row refused: a facet value of the wrong type"})
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

    def _rows(self, spec: ProviderSpec, rows_in: list[RowIn], ctx: ReadContext) -> tuple[list[dict[str, Any]], int]:
        """Validate, order, group and finish rows. A provider BUG (an undeclared or unregistered
        facet, a stored field outside the version policy, a duplicate id) refuses the whole read; bad
        DATA in one row (a facet value of the wrong type) skips that row and returns the count, so one
        corrupt row makes the panel ``partial`` instead of blanking a gate panel."""
        seen: set[str] = set()
        allowed_stored = set(spec.version_fields) | set(spec.version_excluded)
        good: list[RowIn] = []
        refused = 0
        for r in rows_in:
            if not isinstance(r, RowIn):
                raise TypeError(f"a row must be a RowIn, got {type(r).__name__}")
            if r.id in seen:
                raise ValueError(f"duplicate row id {r.id!r}")
            seen.add(r.id)
            undeclared = set(r.facets) - spec.facets
            if undeclared:
                raise CockpitRegistrationError(f"facets {sorted(undeclared)} not declared by panel {spec.id!r}")
            if "*" not in spec.version_fields:
                stray = set(r.stored) - allowed_stored
                if stray:
                    raise ValueError(
                        f"stored fields {sorted(stray)} are neither versioned nor named as excluded"
                    )
            try:
                validate_facets(r.facets)
            except FacetValueError:
                refused += 1
                continue
            good.append(r)
        o = ORDERINGS[spec.order]  # type: ignore[index]
        out = [self._row(spec, o, row, group, ctx.today)
               for row, group in apply_ordering(spec.order, good, ctx.today)]  # type: ignore[arg-type]
        return out, refused

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

    def _now_head_and_rows(
        self, ctx: ReadContext, cred: str, specs_map: dict[str, ProviderSpec],
        snaps: dict[str, _Snap] | None = None
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """The ``now`` view (design §3.2.4): the ``band == "now"`` rows of the gate panels, in panel
        rank order, each panel's rows in that panel's own order. No ordering, provider or actions of
        its own; every row keeps its source ``panel_id``. Its status is the worst of its sources'."""
        sources = sorted(
            (s for s in specs_map.values() if s.priority == "gate" and s.kind == "triage-list"),
            key=lambda s: (0 if s.region == HEADER else 1 + [z for z, _ in ZONES].index(s.region), s.rank, s.id),
        )
        rows: list[dict[str, Any]] = []
        degraded: list[str] = []
        lines: list[str] = []
        worst = "ok"
        as_ofs: list[str] = []
        snaps = snaps if snaps is not None else {}
        for s in sources:
            if s.id not in snaps:   # one read per source per cycle: the heads and this view agree
                snaps[s.id] = self._snap_for(s, ctx)
            snap = snaps[s.id]
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
        # the union of the source panels' declared groups (labels and order), counted over the now
        # rows, so a renderer prints "Today" / "Overdue", not the raw ids the rows carry
        union: dict[str, dict[str, Any]] = {}
        for src in sources:
            if src.order and ORDERINGS[src.order].groups:
                for gid, title in ORDERINGS[src.order].groups:
                    union.setdefault(gid, {"id": gid, "title": title, "count": 0})
        for r in rows:
            if r["group"] in union:
                union[r["group"]]["count"] += 1
        head["groups"] = list(union.values()) or None
        return head, rows

    def _entity(self, ctx: ReadContext) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        base = {"name": None, "governance": None, "brand": {"wordmark": None, "model": None}}
        if self._entity_fn is None:
            return base, []
        res = self._bounded(self._entity_timeout_s, self._entity_state, self._entity_call, ctx)
        if isinstance(res, Fault):
            return base, [{"source": "entity", "message": res.message}]
        return {**base, **res.value}, []

    def _entity_call(self, ctx: ReadContext) -> Result:
        got = self._entity_fn(ctx)  # type: ignore[misc]
        if not isinstance(got, dict):
            raise TypeError(f"entity must return an object, got {type(got).__name__}")
        return Read(value=got)

    @staticmethod
    def _discover_one(fn: Callable[[ReadContext], list[ProviderSpec]], ctx: ReadContext) -> Result:
        got = fn(ctx)
        if not isinstance(got, list) or not all(isinstance(x, ProviderSpec) for x in got):
            raise TypeError("a discoverer returns a list of ProviderSpec")
        return Read(value=got)

    def discover(self, fn: Callable[[ReadContext], list[ProviderSpec]]) -> None:
        """Register a discoverer: run when a manifest is built (or a panel nobody registered is
        asked for), it returns the panels the substrate holds NOW (prose panels, one per heading).
        Ones not yet registered are added; one that later vanishes keeps its provider, which then
        reads ``Absent`` and renders ``error``. A discoverer that raises is a manifest ``errors``
        entry, never silent absence. All discoverers share ONE bounded single-flight read."""
        self._discoverers.append(fn)

    def _ensure_discovered(self, ctx: ReadContext) -> list[dict[str, Any]]:
        """Each discoverer runs on its own bounded single-flight, so one that fails or hangs costs
        only its own panels and one error entry."""
        errors: list[dict[str, Any]] = []
        while len(self._discovery_states) < len(self._discoverers):
            self._discovery_states.append(_State())
        for i, fn in enumerate(self._discoverers):
            res = self._bounded(self._entity_timeout_s, self._discovery_states[i], self._discover_one, fn, ctx)
            if isinstance(res, Fault):
                errors.append({"source": f"discovery:{i}", "message": res.message})
                continue
            for spec in res.value:
                with self._lock:
                    have = self._specs.get(spec.id)
                    if have is None:
                        try:
                            if spec.refresh_every_s is not None:
                                raise CockpitRegistrationError("a discovered panel cannot carry a refresher")
                            self._register_locked(spec)
                        except Exception as exc:  # noqa: BLE001 - a bad spec is an errors entry, never a 500
                            errors.append({"source": f"discovery:{spec.id}", "message": str(exc)})
                    elif have.title != spec.title:
                        errors.append({"source": f"discovery:{spec.id}",
                                       "message": f"id collision: {spec.title!r} maps to the id of {have.title!r}"})
        return errors

    @staticmethod
    def _ordered_specs(specs_map: dict[str, ProviderSpec]) -> list[ProviderSpec]:
        zi = [z for z, _ in ZONES]
        return sorted(specs_map.values(),
                      key=lambda s: (-1 if s.region == HEADER else zi.index(s.region), s.rank, s.id))

    def manifest(self, credential: dict[str, Any], *, install_class: str | None = None) -> dict[str, Any]:
        ctx = ReadContext(self._clock())
        errors = self._ensure_discovered(ctx)
        entity, entity_errors = self._entity(ctx)
        errors += entity_errors
        heads: dict[str, dict[str, Any]] = {}
        snaps: dict[str, _Snap] = {}
        specs_map = self._specs      # one snapshot for the whole manifest
        for spec in self._ordered_specs(specs_map):
            snaps[spec.id] = self._snap_for(spec, ctx)
            heads[spec.id] = self._head(spec, snaps[spec.id], credential["class"], ctx.today)
        now_head, _ = self._now_head_and_rows(ctx, credential["class"], specs_map, snaps)
        heads[NOW_ID] = now_head
        specs = self._ordered_specs(specs_map)
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
                     "install_class": install_class, "policy_revision": POLICY_REVISION,
                     "entity": entity, "errors": errors})   # identity and manifest faults are content too
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
        specs_map = self._specs
        if panel_id != NOW_ID and panel_id not in specs_map:
            self._ensure_discovered(ctx)      # a panel we do not know yet may be a heading added since
            specs_map = self._specs
        if panel_id == NOW_ID:
            head, rows = self._now_head_and_rows(ctx, credential_class, specs_map)
            spec = None
            snap_rows: list[dict[str, Any]] | None = rows
            value = None
        else:
            spec = specs_map.get(panel_id)
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
            if q and spec is not None:   # the now view declares no search
                fields = spec.search_fields
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
            if len(res.rows or ()) != 1:
                return Fault(f"read_one returned {len(res.rows or ())} rows")
            rows, refused = self._rows(spec, list(res.rows), ctx)   # type: ignore[arg-type]
            if refused or len(rows) != 1:
                return Fault("read_one row refused: a facet value of the wrong type")
            return Read(value=rows[0])
        except Exception as exc:  # noqa: BLE001
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
