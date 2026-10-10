"""levain.cockpit.engine: the kernel half of the shared cockpit's READ side (design §3, K1).

Providers return typed results; this module turns them into the manifest and panel payloads:
status, freshness, ETag, ordering and groups, row versions, bands, badges, profiles, search, the
``now`` view, background refreshers with liveness, and per-call timeouts. It writes nothing and
enforces no authority: K1 is the read half, served under the existing read gate (routes.py).

TRUST ASSUMPTION (ruled for K1, 10-09): providers are operator-registered, in-process code and are
trusted not to be adversarial. The flight deadline bounds provider hangs and worker-side derivation
of their output; output objects are NOT deep-materialised, so a provider that returns a hostile
object (a container subclass whose methods block, a hostile ``__hash__``) is outside the model. If
any later slice lets an UNTRUSTED party register a provider (a team server, a plugin), this
assumption becomes a must-close for that slice.

INTERRUPT CONTRACT (K1 L3 r20-r22). Python delivers KeyboardInterrupt only to the main thread,
between any two bytecodes, so no placement of ``try`` makes a critical section interrupt-safe (the
general answer, deferring SIGINT through a process-wide handler, is the host's to install, not a
library's). SAFETY, under any interrupt: two provider reads of one state never overlap, because a
worker invokes the provider only after the owner's ``go``, which follows a clean thread start, and
only the worker ends a flight whose provider started. LIVENESS after an interrupt is NOT promised:
once a KeyboardInterrupt has escaped a read, that Cockpit object may answer errors (or keep a stale
refresher mark) for its remaining life, and its waiting joiners get an error at their own deadline.
The caller rebuilds it. [judged by 1010+13 levain-seat, 2026-10-10, against the callers on
seat/1009-k1-cockpit-read and seat/1009-r1r2-render3: the web server's main thread runs only
``serve_forever`` and closes the server on an interrupt, its reads run on request and refresher
threads, and a Cockpit whose ``start()`` raised is stopped and never published; the one main-thread
reader, ``levain tui --manifest``, ends and stops the Cockpit on an interrupt.]"""

from __future__ import annotations

import dataclasses
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
WRITE_READ_CAP = 32       # source reads made inside writes that may run at once (design §4.4)
WRITE_READ_CAP_PER_SOURCE = 4   # of those, reads of one source (a panel or a discoverer)
POLICY_REVISION = 1  # hashed into the manifest etag; bumped when a tier or gesture policy changes
NOW_ID = "now"
VALUE_ABSENT = "absent"   # the value_version of a line/prose value with no stored record yet
PROCESS_GRACE_S = 5.0     # how long a joiner waits for the owner to process a finished read
_STATUS_RANK = {"ok": 0, "empty": 0, "partial": 1, "stale": 2, "error": 3}


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()


def _sha(obj: Any) -> str:
    return hashlib.sha256(_canon(obj)).hexdigest()


def value_version_of(stored: Any) -> str:
    """The version a value write binds to (§4.4), from the stored record a provider read
    (``Read.version_of``). The ONE computation: a renderer outside the manifest that offers a
    value write (the legacy dashboard's state line) takes its version from here too."""
    return _sha({"stored": stored})[:16]


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
    edit_class: str = ""              # the dashboard's A/B/C edit class chip; "" = none (read-side label only)
    verbs: tuple[str, ...] = ()       # the verbs this panel offers (K2a, design §4.1): only these may target it


class ReadContext:
    """One read cycle: a fixed instant and a memo, so every built-in provider in a cycle sees one
    view of the substrate instead of building its own. ``fresh``: a write's read, so a source with
    its own answer cache must answer anew (``ExternalPanels.take``)."""

    def __init__(self, now: datetime, *, memo_timeout_s: float = 10.0, fresh: bool = False) -> None:
        self.now = now
        self.fresh = fresh
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
    empty: str | None = None
    value_version: str | None = None    # a line/prose value's write version; VALUE_ABSENT if no record


@dataclass
class _State:
    lock: threading.Lock = field(default_factory=threading.Lock)
    mu: threading.Lock = field(default_factory=threading.Lock)   # guards the fields _book writes
    snap: _Snap | None = None
    ever_present: bool = False
    last_good_as_of: str | None = None
    failing_since: str | None = None
    last_completion: datetime | None = None
    pflight: "_Flight | None" = None          # the read in flight: ONE owner commits it, joiners share its result
    refresh_started: datetime | None = None   # set while a refresher cycle is inside its read
    started: datetime | None = None
    fresh: tuple[str | None, str] | None = None   # (as_of, status) of the last read committed (on-demand panels)


class _Flight:
    """One provider read. The OWNER (the caller that started it) alone commits the outcome; joiners
    wait for ``out`` and receive that same outcome. At most one is in flight per state, so commits
    happen in start order by construction. One deadline, ``started + timeout``, governs everyone."""

    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s       # the budget THIS flight was started with: owner, joiners and messages use it
        self.raw: Future = Future()      # the provider's (normalised) outcome, set by the read thread
        self.out: Future = Future()      # what the owner committed, set once the owner is done
        self.started = time.monotonic()
        self.finished: float | None = None   # when the read thread produced ``raw``
        self.committed = False
        self.go = threading.Event()      # the owner's word that the read may invoke the provider
        self.cancelled = False           # set before ``go`` when the owner left without launching cleanly


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
        self._discovered: dict[str, int] = {}   # panel id -> the index of the discoverer that registered it
        self._entity_timeout_s = ENTITY_TIMEOUT_S
        self._lock = threading.Lock()
        self._life = threading.Lock()    # start() and stop() never run at once
        self._verb_view: Any = None      # levain.cockpit.verbs.VerbView, attached by the server (K2a)
        # source reads made inside a write still running (a hung source keeps its flight here)
        self._one_live: list[list[Any]] = []

    def attach_verbs(self, view: Any) -> None:
        """Attach the verb registry's view: row and panel actions, the manifest ``verbs`` map, and
        the revision (install class + policy) hashed into every etag, since a gesture can change with
        rows unchanged (design §9 K2a)."""
        self._verb_view = view

    def spec(self, panel_id: str, *, discover: bool = False) -> "ProviderSpec | None":
        """A panel's registration. ``discover`` runs discovery first when the id is unknown, as
        ``panel()`` does, so a write naming a heading added since the last read finds its panel."""
        if discover and panel_id not in self._specs and panel_id != NOW_ID:
            self._ensure_discovered(ReadContext(self._clock()))
        return self._specs.get(panel_id)

    def offer_spec(self, panel_id: str) -> "ProviderSpec | Fault | None":
        """The registration a WRITE checks its verb against. A panel registered in code is its
        registration. A DISCOVERED panel's verbs are what its OWNER (the discoverer that registered
        it) offers now: the owner is run again here on a fresh read that never joins an earlier
        flight (``ReadContext.fresh``: a source with its own answer cache answers anew), and the
        write is bound to that answer: the spec with the verbs just discovered, ``None`` when the
        owner no longer returns the panel, ``Fault`` when the owner failed (fail closed: an
        unconfirmed offer is no offer)."""
        spec = self.spec(panel_id, discover=True)
        owner = self._discovered.get(panel_id)
        if spec is None or owner is None:
            return spec
        ctx = ReadContext(self._clock(), fresh=True)
        with self._lock:
            fn = self._discoverers[owner]
        res = self._bounded_fresh(("discovery", owner), self._entity_timeout_s, self._discover_one, fn, ctx)
        if isinstance(res, Fault):
            return res
        found = [got for got in res.value if got.id == panel_id]
        if len(found) > 1:
            return Fault(f"discoverer {owner} returned {panel_id!r} {len(found)} times")
        if not found:
            return None
        if found[0].title != spec.title:
            return Fault(f"id collision: {found[0].title!r} maps to the id of {spec.title!r}")
        return dataclasses.replace(spec, verbs=tuple(found[0].verbs))

    def today(self) -> date:
        return ReadContext(self._clock()).today

    def _verb_rev(self) -> Any:
        return self._verb_view.revision() if self._verb_view is not None else None

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
        if spec.edit_class not in ("", "A", "B", "C"):
            raise CockpitRegistrationError(f"{sid}: edit_class must be '', 'A', 'B' or 'C', got {spec.edit_class!r}")
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
        states = dict(self._state)
        states[sid] = _State()
        specs = dict(self._specs)
        specs[sid] = spec
        if spec.rowset is not None:      # claimed last: a failure above leaves nothing half-registered
            self._rowsets[spec.rowset] = sid
        self._state = states            # state first: a reader that sees the spec finds its state
        self._specs = specs

    @property
    def panel_ids(self) -> list[str]:
        return list(self._specs)

    # --- lifecycle -------------------------------------------------------------------
    def start(self) -> None:
        """Take the first read of every refresher panel, then start its refresher thread."""
        with self._life:
            for pid in [p for p, r in self._refreshers.items() if not r.thread.is_alive()]:
                del self._refreshers[pid]    # a halted refresher that has since died is restartable
            for spec in list(self._specs.values()):
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
        with self._life:
            for r in list(self._refreshers.values()):
                r.halt()
            stuck = []
            for pid, r in list(self._refreshers.items()):
                r.thread.join(timeout=r.spec.timeout_s + 1)
                if r.thread.is_alive():
                    stuck.append(pid)
                else:
                    del self._refreshers[pid]
            return stuck

    def refresher(self, panel_id: str) -> _Refresher | None:
        return self._refreshers.get(panel_id)

    # --- reading ---------------------------------------------------------------------
    def _single_flight(
        self, st: _State, timeout_s: float, produce: Callable[[], Any], commit: Callable[[Any], Any],
        refused: Callable[[str], Any], *, on_start: Callable[[], None] | None = None,
        on_timeout: Callable[[], None] | None = None, on_end: Callable[[], None] | None = None,
    ) -> Any:
        """THE ONLY place a provider is invoked (panel reads, entity, discovery, read_one). It owns
        thread start, the once-only resolution of every future, exception text (``_safe_str``), the plain
        text of a provider-made Fault/Absent and the deadline, so none of that is copied per call site.
        The worker invokes the provider only after the owner sets ``go``; an owner that leaves before a
        clean start cancels the flight, so a launched worker can never overlap the next one.

        One read runs at a time per state. The caller that finds none OWNS it: ``produce()`` runs in a
        worker thread (provider code and everything derived from provider output lives there, so a hung
        or hostile provider cannot hold the owner), the owner waits until ``started + timeout_s``,
        then ``commit(outcome)`` (the owner's own code) records it and its return value is what every
        joiner receives. A joiner waits on that result until the same deadline plus a processing
        grace. A caller that arrives after the owner timed out while the source thread is still hung
        fails fast through ``refused`` and commits nothing. A result that finished after the deadline
        counts as a timeout, so an owner and its joiners can never disagree about the same read."""
        result: Any = None
        committed_ok = False
        with st.lock:
            fl = st.pflight
            if fl is None:
                fl = st.pflight = _Flight(timeout_s)
                if on_start:
                    on_start()
                owner = True
            elif not fl.committed:
                owner = False
                wait = max(0.0, fl.started + fl.timeout_s + PROCESS_GRACE_S - time.monotonic())
            else:
                return refused("previous read still running past its timeout (source hung?)")
        if not owner:
            try:
                return fl.out.result(timeout=wait)
            except FutureTimeout:
                return refused(f"timed out waiting on the read in flight ({fl.timeout_s:g}s budget)")

        try:                               # the owner's flight ends in the finally below from here on
            def work() -> None:
                out: Any = Fault("the read ended without a result")
                try:
                    fl.go.wait()
                    if fl.cancelled:
                        out = Fault("the read was cancelled before it started")
                    else:
                        out = produce()
                        if isinstance(out, Fault):            # engine-owned plain text: nothing provider-made reaches the owner
                            out = Fault(_safe_str(out.message))
                        elif isinstance(out, Absent):
                            out = Absent(_safe_str(out.reason))
                except BaseException as exc:  # noqa: BLE001 - an escaping exception IS a Fault
                    out = Fault(f"{type(exc).__name__}: {_safe_str(exc)}")
                finally:                      # raw ALWAYS reaches a terminal state, even for a hostile exception
                    # RACE (accepted): ``finished`` is stamped just before ``raw`` resolves, so a read that
                    # finished in time can lose the deadline race by microseconds and be reported as a
                    # timeout. The error only ever goes in the safe direction: a late result is never
                    # reported healthy.
                    fl.finished = time.monotonic()
                    _set_once(fl.raw, out)
                    with st.lock:
                        if fl.committed and st.pflight is fl:
                            st.pflight = None    # the owner already committed and left a hung thread behind

            deadline = fl.started + fl.timeout_s
            try:
                threading.Thread(target=work, name="cockpit-read", daemon=True).start()
            except BaseException as exc:  # noqa: BLE001 - a failed start fails THIS read and poisons nothing
                fl.cancelled = True        # a thread that did launch before the failure exits without the provider
                fl.finished = time.monotonic()
                _set_once(fl.raw, Fault(f"could not start a read: {type(exc).__name__}: {_safe_str(exc)}"))
                fl.go.set()
                if not isinstance(exc, Exception):
                    raise                  # an interrupt is not swallowed; the finally below still ends the flight
            else:                          # only a clean start lets the worker read; an interrupt from here on is a hung read
                fl.go.set()
            try:
                raw: Any = fl.raw.result(timeout=max(0.0, deadline - time.monotonic()))
                if fl.finished is not None and fl.finished > deadline:
                    raise FutureTimeout()
            except FutureTimeout:
                if on_timeout:
                    on_timeout()
                raw = Fault(f"timed out after {fl.timeout_s:g}s")
            try:
                result = commit(raw)
            except Exception as exc:  # noqa: BLE001 - an outcome that cannot be committed is an error outcome
                try:
                    result = commit(Fault(f"{type(exc).__name__}: {_safe_str(exc)}"))
                except Exception as exc2:  # noqa: BLE001 - nothing committed; the caller still gets an error answer
                    result = refused(f"the read could not be committed: {type(exc2).__name__}: {_safe_str(exc2)}")
            committed_ok = True
            return result
        finally:
            if not fl.go.is_set():         # left before ``go`` (an interrupt): no provider runs, so the flight can end here
                fl.cancelled = True
                _set_once(fl.raw, Fault("the read was cancelled before it started"))
                fl.go.set()
            with st.lock:
                fl.committed = True
                if on_end:
                    on_end()
                if fl.raw.done() and st.pflight is fl:
                    st.pflight = None
            # joiners get the outcome only if it was fully committed; otherwise an error
            _set_once(fl.out, result if committed_ok else refused("the read could not be committed"))

    def _bounded(self, timeout_s: float, st: _State | None, fn: Callable[..., Any], *args: Any) -> Any:
        """A provider call that is not a panel read (entity, discovery, read_one): one flight on its own
        state (a throwaway when it has none), the outcome handed back as-is."""
        def produce() -> Any:
            out = fn(*args)
            if not isinstance(out, (Read, Absent, Fault)):
                return Fault(f"provider returned {type(out).__name__}, not Read/Absent/Fault")
            return out

        def abandon() -> None:
            for a in args:
                if isinstance(a, ReadContext):
                    a.abandon()
        return self._single_flight(st or _State(), timeout_s, produce, lambda raw: raw, Fault, on_timeout=abandon)

    def refresh(self, panel_id: str) -> None:
        """One refresh cycle for a refresher panel (the thread's body; also the test step). It goes
        through the same one-flight read as a request: if a read is already running it joins it."""
        spec, st = self._specs[panel_id], self._state[panel_id]
        self._read_panel(spec, st, ReadContext(self._clock()))

    def _plain_error(self, spec: ProviderSpec, st: _State, message: str) -> _Snap:
        """An error answer for a caller that does not own the read: it commits nothing."""
        return _Snap("error", None, None, [], [], st.last_good_as_of, message, spec.note)

    def _read_panel(self, spec: ProviderSpec, st: _State, ctx: ReadContext) -> _Snap:
        """Read a panel's source through the one flight (design §3.4). ``produce`` runs the provider and
        turns its output into a snapshot (or a Fault) in the worker; ``commit`` is the owner's
        bookkeeping and publication of ``snap``, ``last_completion`` and ``fresh``."""
        def produce() -> Any:
            return self._normalize(spec, ctx, spec.read(ctx))

        def commit(raw: Any) -> _Snap:
            snap = self._book(spec, st, raw, ctx)
            try:
                done_at: datetime | None = self._clock()      # before the lock: a failing clock cannot strand the flight
            except Exception:  # noqa: BLE001
                done_at = None
            with st.lock:
                st.snap = snap
                st.last_completion = done_at or ctx.now
                st.fresh = (snap.as_of, snap.status)
            return snap

        def start() -> None:
            st.refresh_started = ctx.now

        def end() -> None:
            st.refresh_started = None
        return self._single_flight(
            st, spec.timeout_s, produce, commit, lambda m: self._plain_error(spec, st, m),
            on_start=start, on_timeout=ctx.abandon, on_end=end)

    def _snap_for(self, spec: ProviderSpec, ctx: ReadContext) -> _Snap:
        st = self._state[spec.id]
        if spec.refresh_every_s is None:
            return self._read_panel(spec, st, ctx)
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
            return dataclasses.replace(snap, status="stale")    # every other field, the read's empty included
        return snap

    # --- result -> snapshot ----------------------------------------------------------
    def _normalize(self, spec: ProviderSpec, ctx: ReadContext, res: Any) -> Any:
        """Provider output -> a snapshot, or the Fault/Absent it already is. Runs in the read worker:
        everything that touches provider-controlled data (rows, values) happens here, under the
        flight's deadline, never on the owner."""
        if isinstance(res, (Fault, Absent)):
            return res                                # its text is rebuilt in ``_single_flight``'s worker
        if not isinstance(res, Read):
            return Fault(f"provider returned {type(res).__name__}, not Read/Absent/Fault")
        try:
            return self._read_snap(spec, res, ctx, _iso(ctx.now))
        except Exception as exc:  # noqa: BLE001 - malformed provider output of ANY shape is a Fault, never a crash
            return Fault(f"provider output refused: {_safe_str(exc)}")

    def _book(self, spec: ProviderSpec, st: _State, out: Any, ctx: ReadContext) -> _Snap:
        """The owner's bookkeeping for one outcome: failure start, last good, presence. It reads only
        our own types, so it cannot be held by a provider."""
        with st.mu:
            now_iso = _iso(ctx.now)
            listy = spec.kind == "triage-list"
            if isinstance(out, _Snap):
                st.ever_present = True
                st.last_good_as_of = out.as_of
                st.failing_since = None
                return out
            if isinstance(out, Absent):
                # a value with no stored record binds its first write to VALUE_ABSENT (§4.4)
                vv = VALUE_ABSENT if spec.kind in ("line", "prose") else None
                if spec.optional and not st.ever_present:
                    return _Snap("ok", [] if listy else None, None, [], [], now_iso, None,
                                 f"not configured: {out.reason}", value_version=vv)
                why = "source disappeared" if st.ever_present else "source absent"
                return dataclasses.replace(self._fault(spec, st, f"{why}: {out.reason}", now_iso), value_version=vv)
            message = out.message if isinstance(out, Fault) else f"unexpected outcome {type(out).__name__}"
            return self._fault(spec, st, message, now_iso)

    def _fault(self, spec: ProviderSpec, st: _State, message: str, now_iso: str) -> _Snap:
        if st.failing_since is None:
            st.failing_since = now_iso
        detail = f"{message}"
        if st.failing_since:
            detail += f" (failing since {st.failing_since}"
            detail += f", last good {st.last_good_as_of})" if st.last_good_as_of else ")"
        elif st.last_good_as_of:
            detail += f" (last good {st.last_good_as_of})"
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
            value = self._value(spec, res.value, res.version_of)
            rows = None
            status = "partial" if skipped else ("empty" if self._value_empty(spec.kind, value) else "ok")
        stale = res.stale or (ctx.now - _parse_iso(as_of)) > timedelta(seconds=spec.stale_after_s)
        if stale and _STATUS_RANK["stale"] > _STATUS_RANK[status]:
            status = "stale"
        return _Snap(status, rows, value, filtered, skipped, as_of, None,
                     res.note if isinstance(res.note, str) else spec.note, res.stale,
                     res.empty if isinstance(res.empty, str) else None,
                     value["value_version"] if spec.kind in ("line", "prose") else None)

    @staticmethod
    def _value_empty(kind: str, value: Any) -> bool:
        key = {"line": "lines", "metric": "metrics"}.get(kind)
        return bool(key) and not value.get(key)

    @staticmethod
    def _value(spec: ProviderSpec, v: Any, version_of: Any = None) -> Any:
        if not isinstance(v, dict):
            raise ValueError(f"{spec.kind} value must be an object")
        need = {"line": "lines", "metric": "metrics", "prose": "markdown", "visual": "visual"}[spec.kind]
        if need not in v:
            raise ValueError(f"{spec.kind} value lacks {need!r}")
        if spec.kind == "prose":
            v = {**v, "provenance": v.get("provenance")}   # Prose.provenance (rev 9, §3.2.2); null until K2b
        if spec.kind in ("line", "prose"):
            # the version a value write binds to (§4.4): a hash of the stored value as read, never of
            # provenance (a render-time label) or of itself
            stored = {k: x for k, x in v.items() if k not in ("provenance", "value_version")}
            v = {**v, "value_version": _sha(stored)[:16] if version_of is None else value_version_of(version_of)}
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
                     "note": snap.note, "empty": snap.empty, "groups": groups, "credential": cred, "date": today.isoformat(),
                     "verbs": self._verb_rev()})

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
            "error": snap.error, "note": snap.note, "empty": spec.empty if snap.empty is None else snap.empty,
            "order": spec.order, "groups": groups,
            "search": ({"fields": list(spec.search_fields), "default_visible": spec.search_default_visible}
                       if spec.search_fields else None),
            "actions": (self._verb_view.panel_actions(spec.id, cred, today, snap.value_version)
                        if self._verb_view is not None else []),
            "edit_class": spec.edit_class or None,
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
            "actions": [], "degraded": degraded, "edit_class": None,
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
        head["etag"] = self._etag_of(snap, head["groups"], cred, ctx.today)   # groups are content
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
        Ones not yet registered are added, and one already discovered takes the verbs its source
        offers now (a write re-checks them fresh, ``offer_spec``); one that later vanishes keeps its
        provider, which then reads ``Absent`` and renders ``error``, and offers nothing to a write. A discoverer that raises is a manifest ``errors``
        entry, never silent absence. All discoverers share ONE bounded single-flight read."""
        with self._lock:
            self._discoverers.append(fn)

    def _ensure_discovered(self, ctx: ReadContext) -> list[dict[str, Any]]:
        """Each discoverer runs on its own bounded single-flight, so one that fails or hangs costs
        only its own panels and one error entry."""
        errors: list[dict[str, Any]] = []
        with self._lock:     # one snapshot of the discoverers and their states for this pass
            discoverers = list(self._discoverers)
            while len(self._discovery_states) < len(discoverers):
                self._discovery_states.append(_State())
            dstates = list(self._discovery_states)
        for i, fn in enumerate(discoverers):
            res = self._bounded(self._entity_timeout_s, dstates[i], self._discover_one, fn, ctx)
            if isinstance(res, Fault):
                errors.append({"source": f"discovery:{i}", "message": res.message})
                continue
            returned: set[str] = set()
            ids = [s.id for s in res.value]
            for spec in res.value:
                if not isinstance(spec.id, str):
                    errors.append({"source": f"discovery:{i}", "message": "a discovered panel id must be a string"})
                    continue
                if ids.count(spec.id) > 1:
                    # one id returned twice in one answer: neither copy is believed (a write asks the
                    # same question, offer_spec, and refuses too)
                    if spec.id not in returned:
                        errors.append({"source": f"discovery:{spec.id}",
                                       "message": f"discoverer {i} returned this id {ids.count(spec.id)} times"})
                    returned.add(spec.id)
                    with self._lock:
                        if self._discovered.get(spec.id) == i and self._specs[spec.id].verbs:
                            self._set_verbs_locked(spec.id, ())
                    continue
                returned.add(spec.id)
                with self._lock:
                    have = self._specs.get(spec.id)
                    if have is None:
                        try:
                            if spec.refresh_every_s is not None:
                                raise CockpitRegistrationError("a discovered panel cannot carry a refresher")
                            self._register_locked(spec)
                            self._discovered = {**self._discovered, spec.id: i}
                        except Exception as exc:  # noqa: BLE001 - a bad spec is an errors entry, never a 500
                            errors.append({"source": f"discovery:{spec.id}", "message": _safe_str(exc)})
                    elif self._discovered.get(spec.id) != i or have.title != spec.title:
                        # one id, one owner: another source never takes over or relabels a panel
                        errors.append({"source": f"discovery:{spec.id}",
                                       "message": f"id collision: {spec.title!r} maps to the id of {have.title!r}"})
                    elif tuple(have.verbs) != tuple(spec.verbs):
                        # what a discovered panel offers is its owner's answer, read per pass
                        self._set_verbs_locked(spec.id, tuple(spec.verbs))
            with self._lock:
                # a panel its owner no longer returns keeps its provider (it reads Absent) and offers nothing
                for pid in [p for p, o in self._discovered.items() if o == i and p not in returned]:
                    if self._specs[pid].verbs:
                        self._set_verbs_locked(pid, ())
        return errors

    def _set_verbs_locked(self, panel_id: str, verbs: tuple[str, ...]) -> None:
        specs = dict(self._specs)       # copy-on-write, like a registration
        specs[panel_id] = dataclasses.replace(specs[panel_id], verbs=verbs)
        self._specs = specs

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
        etag = _sha({"panels": [heads[i]["etag"] for i in order], "credential": credential, "verbs": self._verb_rev(),
                     "install_class": install_class, "policy_revision": POLICY_REVISION,
                     "entity": entity, "errors": errors})   # identity and manifest faults are content too
        return {
            "schema": SCHEMA, "entity": entity, "generated_at": _iso(ctx.now), "etag": etag,
            "credential": credential, "regions": regions,
            "panels": {i: heads[i] for i in order},
            "verbs": self._verb_view.verbs_map(credential["class"]) if self._verb_view is not None else {},
            "errors": errors,
        }

    def freshness(self) -> dict[str, dict[str, Any]]:
        """``{panel_id: {as_of, status}}`` for every registered panel, ``now`` included (design §3.4).
        A client that got a 304 on a panel reads the panel's current age here. SEMANTICS: a panel's entry
        is its last COMMITTED read; reads of one panel run one at a time and commit in start order, so
        a failure after a success reads ``error``. A client that sees
        ``error`` or ``unread`` re-fetches the panel. It is metadata only:
        it runs NO provider and computes no status of its own. A refresher panel reports its cached
        snapshot (liveness and aging rules applied); an on-demand panel reports its last served read,
        aged against ``stale_after_s``; ``now`` is rolled up from the gate triage-lists' entries in
        this same call (error over unread over stale over partial over ok, oldest ``as_of``), so it
        is as current as its sources; it has no ``empty`` (that needs rows) and reports ``ok``. A refresher
        panel that was never started reads ``error``, as in its head; an on-demand panel not yet served is
        ``status: "unread"`` with ``as_of: null``. Panels found by discovery appear
        after the first manifest read. No rows, no etag, never 304."""
        ctx = ReadContext(self._clock())
        unread = {"as_of": None, "status": "unread"}
        out: dict[str, dict[str, Any]] = {}
        specs_map = self._specs          # ONE registry snapshot for the whole call
        for spec in self._ordered_specs(specs_map):
            if spec.refresh_every_s is not None:
                snap = self._snap_for(spec, ctx)
                out[spec.id] = {"as_of": snap.as_of, "status": snap.status}
                continue
            with self._state[spec.id].lock:
                fresh = self._state[spec.id].fresh
            if fresh is None:
                out[spec.id] = dict(unread)
                continue
            as_of, status = fresh
            if status in ("ok", "empty", "partial") and as_of and \
                    (ctx.now - _parse_iso(as_of)) > timedelta(seconds=spec.stale_after_s):
                status = "stale"
            out[spec.id] = {"as_of": as_of, "status": status}
        gate = [out[s.id] for s in specs_map.values() if s.priority == "gate" and s.kind == "triage-list"]
        order = ("error", "unread", "stale", "partial")
        status = next((o for o in order if any(g["status"] == o for g in gate)), "ok")
        as_ofs = [g["as_of"] for g in gate if g["as_of"]]
        return {NOW_ID: {"as_of": min(as_ofs) if as_ofs and status != "unread" else None, "status": status}, **out}

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
                out["rows"] = self._with_actions(hit, panel_id, credential_class, ctx.today)
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
            if profile == "compact" and panel_id == NOW_ID:
                # The now view repeats rows its source gate panels already send whole in this same
                # profile, so it sends each one's identity and placement and nothing else; the
                # renderer joins on (panel_id, id) to the source panel's rows.
                out["rows"] = [{**{k: r[k] for k in ("id", "panel_id", "version", "group")}, "body": None}
                               for r in rows_all]
                out["rows_by_ref"] = True
            elif profile == "compact":
                if q or head["priority"] == "gate":
                    out["rows"] = [{**r, "body": None} for r in rows_all]
                else:
                    out["rows"] = None
                    out["next"] = f"/cockpit/panel/{panel_id}.json?profile=full"
            else:
                out["rows"] = rows_all
            if matched is not None:
                out["matched"] = matched
            if out["rows"] is not None and not out.get("rows_by_ref"):
                out["rows"] = self._with_actions(out["rows"], panel_id, credential_class, ctx.today)
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

    def _with_actions(self, rows: list[dict[str, Any]], panel_id: str, cred: str, today: date) -> list[dict[str, Any]]:
        """Each row's actions, rendered for this credential (design §4.1: the row's own tier and
        gesture plus the param values that escalate it). A now-view row takes its source panel's."""
        if self._verb_view is None:
            return rows
        return [{**r, "actions": self._verb_view.row_actions(r.get("panel_id") or panel_id, r, cred, today)}
                for r in rows]

    def read_one(self, panel_id: str, row_id: str) -> Result:
        """Read ONE row from the SOURCE through the provider's ``read_one``, never from a refresher
        snapshot (design §4.4). Returns ``Read`` with the finished row dict in ``Read.value``,
        ``Absent`` when the row is gone, or ``Fault``."""
        spec = self._specs.get(panel_id)
        if spec is None or spec.read_one is None:
            return Fault(f"panel {panel_id!r} offers no read_one")
        ctx = ReadContext(self._clock())

        def once(ctx: ReadContext) -> Result:
            res = spec.read_one(ctx, row_id)       # type: ignore[misc]
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
                return Fault(f"provider output refused: {_safe_str(exc)}")
        return self._bounded_fresh(("panel", panel_id), spec.timeout_s, once, ctx)

    def read_value_version(self, panel_id: str) -> Result:
        """Read a line or prose panel's value from the SOURCE (never the refresher snapshot) and
        return its ``value_version`` in ``Read.value`` (design §4.4)."""
        spec = self._specs.get(panel_id)
        if spec is None or spec.kind not in ("line", "prose"):
            return Fault(f"panel {panel_id!r} has no stored value")
        ctx = ReadContext(self._clock())

        def once(ctx: ReadContext) -> Result:
            res = spec.read(ctx)
            if not isinstance(res, Read):
                return res
            try:
                return Read(value=self._value(spec, res.value, res.version_of)["value_version"])
            except Exception as exc:  # noqa: BLE001
                return Fault(f"provider output refused: {_safe_str(exc)}")
        return self._bounded_fresh(("panel", panel_id), spec.timeout_s, once, ctx)

    def _bounded_fresh(self, source: tuple[str, Any], timeout_s: float, fn: Callable[..., Any], *args: Any) -> Any:
        """A source read made inside a write. It never joins a flight another request started: a
        flight that began before this write's own read could hand it a version older than the source.
        The bound is on reads still running (a hung source's threads), not on requests: at most
        ``WRITE_READ_CAP_PER_SOURCE`` per source (a panel, a discoverer), so one hung source refuses
        its own writes and leaves the rest of the cap to the others, and ``WRITE_READ_CAP`` in all."""
        st = _State()
        entry = [st, False, source]     # state, returned, (kind, id): a namespaced key, so a panel
        with self._lock:                # id never shares a discoverer's count
            # live: a read whose call has not returned (reserved from here to its finally, so no
            # scheduler stall can make it uncounted), or one that returned while its worker still
            # runs (a hung source); a returned read whose worker ended is dropped at once
            self._one_live = [e for e in self._one_live if not e[1] or e[0].pflight is not None]
            if sum(1 for e in self._one_live if e[2] == source) >= WRITE_READ_CAP_PER_SOURCE:
                return Fault(f"{WRITE_READ_CAP_PER_SOURCE} reads of {source[0]} {source[1]!r} are still "
                             "running (a hung source?); refused")
            if len(self._one_live) >= WRITE_READ_CAP:
                return Fault(f"{WRITE_READ_CAP} source reads are still running (a hung source?); refused")
            self._one_live.append(entry)
        try:
            return self._bounded(timeout_s, st, fn, *args)
        finally:
            entry[1] = True


def _safe_str(exc: object) -> str:
    try:
        return str(exc)
    except BaseException:  # noqa: BLE001 - an exception whose own text raises must not abort the caller
        return "(unprintable exception)"


def _set_once(fut: Future, value: Any) -> None:
    """Resolve a future exactly once; a second resolution is a no-op, never an error."""
    try:
        fut.set_result(value)
    except Exception:  # noqa: BLE001 - InvalidStateError: already resolved
        pass


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
