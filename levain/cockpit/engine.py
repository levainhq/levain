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

import copy
import dataclasses
import hashlib
from collections import Counter
import itertools
import json
import math
import re
import sqlite3
import threading
import time
import unicodedata
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable

from levain.cockpit.registry import (
    FACETS,
    ORDERINGS,
    CockpitRegistrationError,
    CONSENT_ROW_PREFIX,
    QUESTION_ROW_PREFIX,
    NOTE_ROW_PREFIX,
    OUTCOME_ROW_PREFIX,
    PENDING_ROW_PREFIX,
    FacetValueError,
    apply_ordering,
    counted_rows,
    parse_date,
    validate_facets,
)
from levain import unshowable
from levain.cockpit.display_safe import visible
from levain.cockpit.authority import NO_LABELS, LabelKey, LabelSnapshot, RequestAuthority, current, unavailable
from levain.cockpit.results import Absent, Fault, Read, Result, RowIn

SCHEMA = "levain.cockpit/2"
# the budget one panel read has before it is a fault (a provider's default ``timeout_s``, the shared-read memo's wait); other
# waits on a read are expressed as a part of it, so they stay inside it when it moves
PANEL_READ_DEADLINE_S = 10.0
ENTITY_TIMEOUT_S = 10.0
# The kinds of the kernel's vocabulary (docs/cockpit-contract.md). A provider written against
# levain.cockpit/1 declares an ALIAS; it is normalised to the new kind when the spec is built, so the
# manifest only ever carries the new names.
KINDS = ("reading", "line", "brief", "count-queue", "ambient", "status-dots", "metric-grid", "roster", "list",
         "form", "log", "topology", "board")
ALIASES = {"triage-list": "list", "metric": "status-dots", "visual": "ambient", "prose": "brief"}
# row-shaped kinds (the searchable property); list and count-queue ride the RowIn engine, roster and log
# carry their rows inside the value
ROW_ENGINE_KINDS = ("list", "count-queue")
VALUE_ROW_KINDS = ("roster", "log")
ROW_KINDS = ROW_ENGINE_KINDS + VALUE_ROW_KINDS
VERSIONED_VALUE_KINDS = ("line", "brief")     # a value write binds to the value's version (§4.4)
SEARCH_THRESHOLD = 8      # a row-shaped panel with MORE rows than this (or a windowed one) gets the box
SEARCH_Q_MAX = 200        # a search query is plain text of at most this many characters
REPLAY_READ_CAP = 4           # past reads (``?at=``) that may run at once: their own pool, never a slot of WRITE_READ_CAP
REPLAY_CACHE_S = 20.0         # a past read of one (panel, minute) is kept this long, so a repeated read does not re-read the source
REPLAY_CACHE_MAX = 16
REPLAY_WINDOW_S = 14 * 3600   # a past read (``?at=``) reaches back at most this far (F12 Replay: the last 14 h)
SEARCH_LIMIT_MAX = 200    # the most rows a store search returns
SEARCH_LIMIT_DEFAULT = 50
SEARCH_INFLIGHT = 2       # live store-search workers per panel: the current query plus one superseded by typing
SCOPES = ("rows", "store")
FORM_FIELD_TYPES = ("text", "recipient", "date")
PRIORITIES = ("gate", "gauge", "feed")
ZONES = (("operate", "Work"), ("mind", "Mind"), ("identity", "Identity"), ("team", "Team"))
HEADER = "header"     # the legacy region: normalised to hero or brief (or operate) when a spec is built
# the regions above the zones, in drawing order; a panel in one is a single answer, never a list
HERO, BRIEF, WIDGETS = "hero", "brief", "widgets"
TOP_REGIONS = (HERO, BRIEF, WIDGETS)
HERO_KINDS = ("reading", "ambient")
BRIEF_KINDS = ("line", "brief")
WIDGET_KINDS = ("reading", "line", "status-dots", "metric-grid")
SIZES = ("tall", "medium", "short")
SPANS = (1, 2, 3)
FACES = ("", "count")
ZONE_GRID = 3
_REGION_ORDER = TOP_REGIONS + tuple(z for z, _ in ZONES)
# the symbol a reading may carry: a provider maps its own source vocabulary onto this set
READING_SYMBOLS = ("clear", "clear-night", "partly-cloudy", "partly-cloudy-night", "cloudy", "fog", "drizzle",
                   "rain", "heavy-rain", "thunderstorm", "snow", "sleet", "wind", "haze")
# the band tokens the contract names for a sub-reading; a renderer compares case-insensitively,
# `good` is calm, the rest are hot, and any other string renders neutral
READING_BANDS = ("good", "moderate", "unhealthy-sensitive", "unhealthy", "very-unhealthy", "hazardous")
VISUALS = ("sky", "constellation", "cognition-graph", "wrap-history")
TITLE_MAX = 160
SOON_DAYS = 3
WRITE_READ_CAP = 32       # source reads made inside writes that may run at once (design §4.4)
WRITE_READ_CAP_PER_SOURCE = 4   # of those, reads of one source (a panel or a discoverer)
NOW_ID = "now"
# a triage-list view id: lower-case, and never "default" (the panel's own order is the default view)
_VIEW_ID = re.compile(r"(?!default\Z)[a-z0-9][a-z0-9_-]{0,31}")     # matched whole (fullmatch)
VALUE_ABSENT = "absent"   # the value_version of a line/prose value with no stored record yet
PROCESS_GRACE_S = 5.0     # how long a joiner waits for the owner to process a finished read
_STATUS_RANK = {"ok": 0, "empty": 0, "partial": 1, "stale": 2, "error": 3}


DECISIONS_ID = "decisions"
DECISION_ORDER = "decision.queue"
KERNEL_DECISION_PREFIXES = (PENDING_ROW_PREFIX, OUTCOME_ROW_PREFIX, CONSENT_ROW_PREFIX, QUESTION_ROW_PREFIX,
                            NOTE_ROW_PREFIX)
# the kernel's rows a panel verb is offered on through the ordinary row path (each verb's applies_to picks its own)
_VERB_ROW_PREFIXES = (QUESTION_ROW_PREFIX, NOTE_ROW_PREFIX)
KERNEL_DECISION_FACETS = frozenset({"principal", "pending_tier", "expires_at", "due", "at", "outcome", "verb",
                                    "reason", "unapplied", "cancelled_by",
                                    "agent", "tool", "consent_state", "consent_tier", "decided_at",
                                    "question_n", "question_state", "question_options", "note_n", "note_state",
                                    "note_open_total", "sign", "egress_state"})
QUESTION_VERB = "question_answer"       # the cockpit verb a question row offers (verbs.py registers it)
QUESTION_DONE_HOURS = 24                # an answered question stays listed this long, as a decided request does
NOTE_VERB = "note_dismiss"              # the cockpit verb a note row offers (verbs.py registers it)
BAND_VERB = "band_answer"               # the cockpit verb that answers the NEEDS YOU band by its shorthand (verbs.py)


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()


def _sha(obj: Any) -> str:
    return hashlib.sha256(_canon(obj)).hexdigest()


def value_version_of(stored: Any) -> str:
    """The version a value write binds to (§4.4), from the stored record a provider read
    (``Read.version_of``). The ONE computation: a renderer outside the manifest that offers a
    value write (the legacy dashboard's state line) takes its version from here too."""
    return _sha({"stored": stored})[:16]


def row_version_of(stored: dict[str, Any], fields: tuple[str, ...], excluded: tuple[str, ...] = ()) -> str:
    """A row's version (§4.4) over its stored record: the ONE computation, used for every rendered
    row and by a store that recomputes it inside its own write (a spore's ``version_of``)."""
    if "*" in fields:
        payload = {k: v for k, v in stored.items() if k not in excluded}
    else:
        payload = {k: stored.get(k) for k in fields}
    return _sha(payload)[:16]


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
    region: str = "operate"           # "hero", "brief", "widgets", a zone id, or the legacy "header"
    rank: int = 0
    refresh_every_s: float | None = None
    stale_after_s: float = 900.0
    optional: bool = False
    # a source the substrate only creates on first use (the spore store): absent before it was ever seen is an EMPTY panel,
    # never a fault. Once seen, its disappearance is still a fault ("source disappeared").
    absent_is_empty: bool = False
    timeout_s: float = PANEL_READ_DEADLINE_S
    order: str | None = None
    # other server orders of the SAME rows, as ``(id, title, ordering)``: a triage-list's views. The
    # panel sends each view's row order (``view_rows``) and a renderer switches by it, never sorting.
    views: tuple[tuple[str, str, str], ...] = ()
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
    # K2b-3b: what a row's (or a line/prose value's) label is computed over, from its stored record (a
    # value's: the value as read, ``provenance`` and ``value_version`` aside). None: provenance stays null.
    label_key: Callable[[dict[str, Any]], LabelKey | None] | None = None
    # Searchable (contract v2): ``search_fields`` (above) names WHAT is matched; ``search_scope`` says
    # whether the panel holds the whole set ("rows") or is a window of a larger store ("store").
    # ``window`` is the newest-N a windowed panel delivers; ``search(q, limit)`` is the provider's hook
    # over the whole store (a scope-"store" panel only).
    search_scope: str = "rows"
    window: int | None = None
    search: Callable[[str, int], Any] | None = None
    # the levain.cockpit/1 name this spec was declared with, when it declared an alias. Derived from
    # ``kind`` alone: a caller cannot set it, so it can never disagree with the kind it normalised.
    legacy_kind: str = field(default="", init=False)
    # how the panel draws in a widget: "" (its kind's own face) or "count" (a row-engine kind as its row count)
    face: str = ""
    # a list panel's compose card: ``{verb, fields: [typed form fields]}``, drawn on the list's own card (never
    # a second form panel). The verb is one the panel offers; the fields are the form kind's own (text,
    # recipient, date). A recipient field with nothing chosen sends no value for it: "none selected = everyone".
    compose: dict[str, Any] | None = None
    # zone layout (a zone panel only): the card's height and column span, and a shared title that makes
    # same-zone panels tabs of one card
    size: str = "medium"
    span: int = 1
    tabs: str = ""
    # "header" when the spec declared the legacy region; ``region`` then holds where it was normalised to.
    # Derived like ``legacy_kind``: a caller cannot set it.
    legacy_region: str = field(default="", init=False)
    # the panel's read at a past instant (F12 Replay): ``read_at(ctx, iso)`` returning the same shape as ``read`` for
    # that instant. Optional: None (and no ``read.read_at`` on the provider's read) means the panel has no past read,
    # its head carries no ``replay`` flag and ``?at=`` on it is refused. A provider may also carry it on its read
    # function as ``read.read_at`` (how the topology provider offers it); an explicit field wins.
    read_at: Callable[["ReadContext", str], Result] | None = None

    def __post_init__(self) -> None:
        # rank is a sort key beside str ids: anything but an int would turn every manifest into a 500
        if type(self.rank) is not int:
            raise CockpitRegistrationError("a panel's rank must be an int")
        if self.read_at is None and callable(getattr(self.read, "read_at", None)):
            self.read_at = self.read.read_at  # type: ignore[attr-defined]
        if self.read_at is not None and not callable(self.read_at):
            raise CockpitRegistrationError("a panel's read_at must be callable")
        if self.kind in ALIASES:
            self.legacy_kind = self.kind
            self.kind = ALIASES[self.kind]
        if self.region == HEADER:
            self.legacy_region = HEADER
            self.region = HERO if self.kind in HERO_KINDS else (BRIEF if self.kind in BRIEF_KINDS else "operate")

    def replaced(self, **changes: Any) -> "ProviderSpec":
        """A copy with ``changes``. ``replace`` re-runs ``__post_init__`` on the already-normalised kind and
        region, so the declared names (the init=False fields) are carried across explicitly, here and nowhere else."""
        out = dataclasses.replace(self, **changes)
        out.legacy_kind = self.legacy_kind
        out.legacy_region = self.legacy_region
        return out

    def with_verbs(self, verbs: tuple[str, ...]) -> "ProviderSpec":
        """A copy offering ``verbs``."""
        return self.replaced(verbs=verbs)


def _label_note(note: str | None, labels: "LabelSnapshot | None") -> str | None:
    """A panel's note plus what its label snapshot could not read: the snapshot's error, and how many
    label rows were unreadable (§8d row 3: a corrupt label row degrades only its resource, and says so).
    ``labels`` is None for a panel whose rows carry no label."""
    if labels is not None and labels.error:
        note = f"{note}; {labels.error}" if note else labels.error
    degraded = getattr(labels, "degraded", 0) if labels is not None else 0
    if degraded:
        msg = f"{degraded} label row(s) unreadable: their resources read unverified"
        note = f"{note}; {msg}" if note else msg
    return note


class ReadContext:
    """One read cycle: a fixed instant and a memo, so every built-in provider in a cycle sees one
    view of the substrate instead of building its own. ``fresh``: a write's read, so a source with
    its own answer cache must answer anew (``ExternalPanels.take``)."""

    def __init__(self, now: datetime, *, memo_timeout_s: float = PANEL_READ_DEADLINE_S, fresh: bool = False,
                 authority: RequestAuthority | None = None) -> None:
        self.now = now
        self.fresh = fresh
        # the request's one authority snapshot (K2b-3b §0.1); None in a refresher's own cycle, which
        # computes label keys only and leaves provenance to the request that renders them
        self.authority = authority
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
    view_order: dict[str, list[str]] | None = None   # each view's row ids in order (a triage-list with views)
    # computed in the read worker from ``stored`` (a row dict carries no stored record); provenance is
    # applied from these at render, from the request's snapshot
    label_keys: dict[str, LabelKey] | None = None
    value_label_key: LabelKey | None = None
    # rows the provider marked ``held`` (a Tray spore whose surface date has not come): listed apart so
    # ``rows``, its counts, groups and orders stay what they were for every reader that does not know them
    held: list[dict[str, Any]] = field(default_factory=list)


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
    read: Any = None                              # the registering spec's provider: a state belongs to ONE generation


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


@dataclass
class _Decisions:
    """The ``decisions`` panel's contributors (K2b-3b §0.4): a downstream's ``open`` source, kept whole,
    and the broker's reader. ``spec`` is the panel the kernel registered for them, None while a
    downstream's own ``register(ProviderSpec("decisions", ...))`` holds the id (today's panel)."""

    source: "ProviderSpec | None" = None
    rows: Callable[[datetime], Any] | None = None
    consent: Callable[[datetime], Any] | None = None
    questions: Any = None             # a ``levain.consent.questions.QuestionStore`` (Q-2b), read and answered at the source
    notes: Any = None                 # a ``levain.consent.notes.NoteStore``, read and dismissed at the source
    spec: "ProviderSpec | None" = None


_DEC_FIELDS = ("source", "rows", "consent", "questions", "notes", "spec")     # every _Decisions field, for attach_server


def _composed(ctx: "ReadContext") -> Result:
    return Fault("the decisions panel is composed by the kernel; it has no read of its own")


def _broker_decision_spec() -> "ProviderSpec":
    """The spec the broker's rows are read and versioned under (never registered): ``stored`` is
    exactly what a row's version covers, ``(state, expires_at)`` for a pending and
    ``(record_sha256, state)`` for an outcome (§3)."""
    return ProviderSpec(DECISIONS_ID, "triage-list", "Decisions", "gate", _composed, order=DECISION_ORDER,
                        facets=KERNEL_DECISION_FACETS, version_fields=("*",))


def _due_of(stamp: str) -> str:
    return _parse_iso(stamp).astimezone().date().isoformat()


def _target_text(target: Any) -> str:
    t = target if isinstance(target, dict) else {}
    for k in ("row_id", "panel_id", "pending_id", "kind"):
        if isinstance(t.get(k), str) and t[k]:
            return t[k]
    return "?"


def _decision_rows_in(got: Any) -> list[RowIn]:
    """The broker's ``DecisionRows`` as rows (§3): ``pend:<id>`` per pending waiting on a signature,
    ``out:<operation_id>`` per outcome."""
    rows: list[RowIn] = []
    for p in got.pendings:
        rows.append(RowIn(
            id=f"{PENDING_ROW_PREFIX}{p.pending_id}", title=f"{p.verb or '?'} on {_target_text(p.target)}",
            facets={"principal": p.principal, "pending_tier": p.tier, "expires_at": p.expires_at,
                    "due": _due_of(p.expires_at), "at": p.issued_at},
            stored={"state": "WAITING", "expires_at": p.expires_at}))
    for o in got.outcomes:
        rows.append(RowIn(
            id=f"{OUTCOME_ROW_PREFIX}{o.operation_id}", title=f"{o.verb or '?'}: {o.outcome}",
            body=o.reason,
            facets={"at": o.terminal_at, "outcome": o.outcome, "verb": o.verb or "", "reason": o.reason,
                    "unapplied": list(o.unapplied), "cancelled_by": o.cancelled_by},
            stored={"record_sha256": o.record_sha256, "state": o.state}))
    return rows


PANEL_EGRESS_BUSY_TIMEOUT = 0.2   # seconds a panel read waits on a locked dispatch store; the body route keeps EgressReader's own default


def _egress_lookup_of(reader: Any) -> Callable[[str, str], tuple[Any, bool]] | None:
    from levain.consent.listing import egress_reading

    return egress_reading(reader, busy_timeout=PANEL_EGRESS_BUSY_TIMEOUT)


def _consent_rows_in(got: Any, egress_lookup: Callable[[str, str], tuple[Any, bool]] | None = None
                     ) -> tuple[list[RowIn], int]:
    """The consent queue's listing (``levain.consent.listing.ConsentListing``) as rows: ``consent:<request_id>``
    per request an agent waits on (pending, or undecided and waiting on a late ruling, however old), and per one
    decided in the last day, with the count of entries that did not
    build (each row is built on its own, so one bad entry never blanks the rest). A row carries who asks, for
    which tool, its tier, state and times (``at`` is when it was filed; a decided one adds
    ``decided_at``), and nothing of the request's body: the display, cwd, targets, answer commands and how it
    was answered are served only to a token holder (``/cockpit/consent/<id>.json``), since
    this panel is token-free on loopback. The row never carries the time left (the page counts down from
    ``expires_at``), so its version does not change every second. A ``levain_egress`` row also carries
    ``egress_state``, what the card became (``listing.egress_state_of`` over ``egress_lookup``, the computation the
    body route serves; ``unreadable`` when the lookup could not answer), so a decided row's header can say it before
    its details open. A pending row carries none."""
    from levain.consent.listing import egress_state_of

    rows: list[RowIn] = []
    bad = 0
    for e in (*got.pending, *got.undecided, *got.recent):
        try:
            r = e.request
            pending = e.state == "pending"
            agent, tool = visible(r.agent), visible(r.tool_name)     # agent-controlled: invisible characters show as markers
            facets: dict[str, Any] = {
                "agent": agent, "tool": tool, "consent_state": e.state, "consent_tier": r.tier,
                "expires_at": r.expires_at, "due": _due_of(r.expires_at),
                "at": r.received_at}
            if e.decided_at is not None:
                facets["decided_at"] = e.decided_at
            word = None if pending else egress_state_of(e, egress_lookup)   # a pending header never draws it
            if word is not None:
                facets["egress_state"] = word
            rows.append(RowIn(
                id=f"{CONSENT_ROW_PREFIX}{r.id}", title=f"{agent} \u00b7 {tool}", facets=facets,
                stored={"state": e.state, "expires_at": r.expires_at, "decided_at": e.decided_at, "request": r.id}))
        except Exception:  # noqa: BLE001 - one entry that does not build is skipped and counted
            bad += 1
    return rows, bad


def _question_row_in(q: Any) -> RowIn:
    """One question (``levain.consent.questions.Question``) as a row: ``question:<n>``. A row carries who asked,
    when, how many options it offers and whether it is answered, and NOTHING the agent wrote (the question, the
    option labels, what it blocks): this panel is token-free on loopback, so those words are served only to a token
    holder (``/cockpit/question/<n>.json``). Its version is its number and its answered-ness, read from the source,
    so an answer that landed elsewhere (the phone, the CLI) makes a stale row a 409, never a second answer."""
    answered = q.answer is not None
    agent = visible(q.agent)
    facets: dict[str, Any] = {"agent": agent, "at": q.filed_at, "question_n": q.n,
                              "question_state": "answered" if answered else "open",
                              "question_options": len(q.options)}
    if answered:
        facets["decided_at"] = q.answer.answered_at
    _sign_facet(facets, q)
    return RowIn(id=f"{QUESTION_ROW_PREFIX}{q.n}", title=f"{agent} · question #{q.n}", facets=facets,
                 stored={"n": q.n, "answered_at": q.answer.answered_at if answered else "open"})


def _sign_facet(facets: dict[str, Any], item: Any) -> None:
    """DESIGN_SIGN D5: ``sign: {"request": <id>}`` on a question or note row whose item names a levain sign request
    (``sign_request``; an item without the attribute names none). The id only: the page reads the request itself from
    ``/cockpit/sign.json`` behind the token."""
    request = getattr(item, "sign_request", None)
    if request is not None:
        facets["sign"] = {"request": request}


def _question_rows_in(store: Any, now: datetime) -> tuple[list[RowIn], int]:
    """The question store's open questions, and those answered in the last day, as rows (with the count of open
    ones left out past the cap). A queue with no ``questions.db`` lists nothing and creates nothing."""
    from levain.consent.listing import PENDING_CAP

    open_ = store.open_questions()
    since = (now - timedelta(hours=QUESTION_DONE_HOURS)).astimezone(timezone.utc)
    done = [q for q in store.recent(50) if q.answer is not None
            and _parse_iso(q.answer.answered_at) >= since]
    # two reads: a question answered between them is in both, and its row id is shown once (the answered state)
    shown = {q.n: q for q in open_[:PENDING_CAP]}
    shown.update((q.n, q) for q in done)
    return [_question_row_in(q) for q in shown.values()], max(len(open_) - PENDING_CAP, 0)


def _note_row_in(n: Any, open_total: int | None = None) -> RowIn:
    """One note (``levain.consent.notes.Note``) as a row: ``note:<n>``, carrying who filed it, when, and whether it was
    dismissed, and NOTHING the agent wrote (served to a token holder only: ``/cockpit/note/<n>.json``). Its version is
    its number and its dismissed-ness, so a dismissal made elsewhere makes a stale row a 409. An open row of a listing
    also carries ``note_open_total``, how many notes are open in all, so a surface that shows a page says how many
    it left out (it is not versioned: the count moves with other notes, the row does not)."""
    dismissed = n.dismissed is not None
    agent = visible(n.agent)
    facets: dict[str, Any] = {"agent": agent, "at": n.filed_at, "note_n": n.n,
                              "note_state": "dismissed" if dismissed else "open"}
    if dismissed:
        facets["decided_at"] = n.dismissed.dismissed_at
    elif open_total is not None:
        facets["note_open_total"] = open_total
    _sign_facet(facets, n)
    return RowIn(id=f"{NOTE_ROW_PREFIX}{n.n}", title=f"note from {agent} #{n.n}", facets=facets,
                 stored={"n": n.n, "dismissed_at": n.dismissed.dismissed_at if dismissed else "open"})


def _note_rows_in(store: Any, now: datetime) -> tuple[list[RowIn], int]:
    """The open notes, and those dismissed in the last day, as rows (with the count of open ones past the cap). A
    queue with no ``notes.db`` lists nothing and creates nothing."""
    from levain.consent.listing import PENDING_CAP

    open_, total = store.open_page(PENDING_CAP)      # never more than the page is read (NoteStore.open_page)
    since = (now - timedelta(hours=QUESTION_DONE_HOURS)).astimezone(timezone.utc)
    done = [n for n in store.recent(50) if n.dismissed is not None
            and _parse_iso(n.dismissed.dismissed_at) >= since]
    shown = {n.n: n for n in open_}
    shown.update((n.n, n) for n in done)
    return [_note_row_in(n, total) for n in shown.values()], max(total - len(open_), 0)


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
        self._retiring: frozenset[int] | set[int] = frozenset()   # discoverers whose vanished panels are unregistered
        self._discovered: dict[str, int] = {}   # panel id -> the index of the discoverer that registered it
        self._hero_yields: set[str] = set()     # hero panels a later hero panel of their kind replaces (yield_hero)
        self._entity_timeout_s = ENTITY_TIMEOUT_S
        self._lock = threading.Lock()
        self._life = threading.Lock()    # start() and stop() never run at once
        self._verb_view: Any = None      # levain.cockpit.verbs.VerbView, attached by the server (K2a)
        self._labeller: Callable[[], LabelSnapshot] | None = None    # a labels-only source (tests)
        # the broker's request capture (its authority and labels), attached by the server (K2b, DESIGN §3.4)
        self._capture: Callable[[datetime], RequestAuthority] | None = None
        self._dec = _Decisions()          # the decisions panel's contributors (K2b-3b §0.4)
        self._broker_dec_spec = _broker_decision_spec()
        # the broker's Recent edits reader (fn(limit) -> EditsPage), attached by the server (K2b-3b §0.6)
        self._edits_reader: Callable[[int], Any] | None = None
        # source reads made inside a write still running (a hung source keeps its flight here)
        self._one_live: list[list[Any]] = []
        self._replay_live: list[list[Any]] = []                  # the past reads still running: their own bounded pool
        self._replay_cache: dict[tuple[str, str], tuple[float, ProviderSpec, "_Snap"]] = {}
        # panel id -> its live store-search workers, capped at SEARCH_INFLIGHT, so hung hooks cost bounded threads
        self._searching: dict[str, list[threading.Thread]] = {}
        self._search_mu = threading.Lock()

    def attach_verbs(self, view: Any) -> None:
        """Attach the verb registry's view: row and panel actions, the manifest ``verbs`` map, and
        the revision (install class + authority head) hashed into every etag, since a gesture can change with
        rows unchanged (design §9 K2a). Every compose already registered is checked against the
        registry first (a verb it holds, panel-bound, not a job, fields inside its allowlist), and a
        refused one refuses the attach: the registry is known from here on."""
        with self._lock:
            self._attach_verbs_locked(view)

    def _attach_verbs_locked(self, view: Any) -> None:
        self._check_verbs(view)
        self._verb_view = view

    def check_verbs(self, view: Any) -> None:
        """The check :meth:`attach_verbs` runs, attaching nothing: a server runs it before it commits to the
        cockpit (``make_server``), so a refused compose leaves the cockpit and everything else as they were."""
        with self._lock:
            self._check_verbs(view)

    def _check_verbs(self, view: Any) -> None:
        for spec in self._specs.values():
            _check_compose_verbs(spec, view)

    def attach_server(self, view: Any, consent: Callable[[datetime], Any], *,
                      capture: Callable[[datetime], RequestAuthority] | None = None,
                      edits: Callable[[int], Any] | None = None, decision_rows: Callable[[datetime], Any] | None = None,
                      questions: Any = None, notes: Any = None) -> None:
        """Every attach a server makes (``make_server``), as ONE transaction under the cockpit's lock: the verb view
        (:meth:`attach_verbs`), the broker's capture, edits reader and decisions rows when given, the consent reader,
        then the question and note stores when given. One that raises puts back exactly what this call found, then
        raises. Nothing else registers while the lock is held, so a panel another thread registers before this call or
        after it is kept: the undo removes only this server's attaches."""
        with self._lock:
            before = (self._verb_view, self._capture, self._edits_reader,
                      tuple(getattr(self._dec, f) for f in _DEC_FIELDS), self._specs, self._state,
                      dict(self._rowsets), set(self._hero_yields))
            try:
                self._attach_verbs_locked(view)
                if capture is not None:
                    self._capture = capture
                if edits is not None:
                    self._edits_reader = edits
                if decision_rows is not None:
                    self._attach_decision_rows_locked(decision_rows)
                self._attach_consent_rows_locked(consent)
                if questions is not None:
                    self._attach_question_rows_locked(questions)
                if notes is not None:
                    self._attach_note_rows_locked(notes)
            except BaseException:
                # the registry is copy-on-write, so the dicts taken are the ones a reader held then; the row-set
                # owners and hero yields, changed in place, are put back in place
                self._verb_view, self._capture, self._edits_reader, dec, self._specs, self._state, rowsets, heroes = (
                    before)
                for f, v in zip(_DEC_FIELDS, dec):
                    setattr(self._dec, f, v)
                self._rowsets.clear()
                self._rowsets.update(rowsets)
                self._hero_yields.clear()
                self._hero_yields.update(heroes)
                raise

    def attach_labeller(self, fn: Callable[[], LabelSnapshot]) -> None:
        """Attach a labels-only source: ``fn()`` is ONE journal read, taken once per request (design
        §0.1). A request captured this way holds no signed authority."""
        self._labeller = fn

    def attach_authority(self, fn: Callable[[datetime], RequestAuthority]) -> None:
        """Attach the broker's capture: ``fn(now)`` is ONE journal read per request, the authority
        log's verified head and the labels built from it (trust-tiering DESIGN §3.4)."""
        self._capture = fn

    def attach_edits_reader(self, fn: Callable[[int], Any]) -> None:
        """Attach the broker's journal edits reader: ``fn(limit)`` returns an ``EditsPage``, None while
        the broker is not ACTIVE, or raises (K2b-3b §0.6). Recent edits merges it with the ledger."""
        self._edits_reader = fn

    @property
    def edits_reader(self) -> Callable[[int], Any] | None:
        return self._edits_reader

    def attach_decision_source(self, spec: ProviderSpec) -> None:
        """A downstream's ``open`` decisions, as a CONTRIBUTOR of the kernel's ``decisions`` panel (K2b-3b
        §0.4): read and normalised under its own spec (facets, versions, ``read_one``, Fault), then
        grouped under ``decision.queue`` with the broker's rows. Refused while a downstream's own
        ``register(ProviderSpec("decisions", ...))`` holds the id."""
        if spec.kind != "list" or not spec.version_fields:
            raise CockpitRegistrationError("a decision source is a list declaring version_fields")
        for f in spec.facets:
            if f not in FACETS:
                raise CockpitRegistrationError(f"decision source: unregistered facet {f!r}")
        if spec.order is not None and spec.order not in ORDERINGS:
            raise CockpitRegistrationError(f"decision source: unknown ordering {spec.order!r}")
        with self._lock:
            if self._dec.source is not None:
                raise CockpitRegistrationError("a decision source is already attached")
            if DECISIONS_ID in self._specs and self._specs[DECISIONS_ID] is not self._dec.spec:
                raise CockpitRegistrationError(
                    "panel 'decisions' is registered by a downstream: attach its source instead of registering it")
            # transactional: a placement the registry refuses must leave the previous panel and source intact
            before = (self._dec.source, self._dec.spec, self._specs, self._state, dict(self._rowsets))
            self._dec.source = spec
            try:
                self._install_decisions_locked()
            except BaseException:
                self._dec.source, self._dec.spec, self._specs, self._state, rowsets = before
                self._rowsets.clear()
                self._rowsets.update(rowsets)
                raise

    def attach_decision_rows(self, fn: Callable[[datetime], Any]) -> None:
        """The broker's reader of its own decisions rows: ``fn(now)`` returns ``DecisionRows``, None
        while the broker is not ACTIVE, or raises. With no downstream ``decisions`` registered the
        kernel registers its own panel; a downstream that registered one keeps exactly its panel."""
        with self._lock:
            self._attach_decision_rows_locked(fn)

    def _attach_decision_rows_locked(self, fn: Callable[[datetime], Any]) -> None:
        self._dec.rows = fn
        if DECISIONS_ID not in self._specs or self._specs[DECISIONS_ID] is self._dec.spec:
            self._install_decisions_locked()

    def attach_consent_rows(self, fn: Callable[[datetime], Any]) -> None:
        """The consent queue's reader (``fn(now)`` returns a ``ConsentListing``, or raises): agents' requests
        waiting on the operator (Q6a). Read-only, and independent of the broker: a read fault is a note on the
        panel, never a dark one."""
        with self._lock:
            self._attach_consent_rows_locked(fn)

    def _attach_consent_rows_locked(self, fn: Callable[[datetime], Any]) -> None:
        self._dec.consent = fn
        if DECISIONS_ID not in self._specs or self._specs[DECISIONS_ID] is self._dec.spec:
            self._install_decisions_locked()

    def attach_question_rows(self, store: Any) -> None:
        """The question band's store (``levain.consent.questions.QuestionStore``): agents' questions waiting on the
        operator (Q-2b), read at the source, as the consent queue is. Its rows carry no question text, and the
        panel offers ``question_answer`` on an open one. A read fault is a note, never a dark panel."""
        with self._lock:
            self._attach_question_rows_locked(store)

    def _attach_question_rows_locked(self, store: Any) -> None:
        self._dec.questions = store
        if DECISIONS_ID not in self._specs or self._specs[DECISIONS_ID] is self._dec.spec:
            self._install_decisions_locked()

    def attach_note_rows(self, store: Any) -> None:
        """The note store (``levain.consent.notes.NoteStore``): what agents told the operator, read at the source. Its
        rows carry no words, and the panel offers ``note_dismiss`` on an open one. A read fault is a note on the
        panel, never a dark one."""
        with self._lock:
            self._attach_note_rows_locked(store)

    def _attach_note_rows_locked(self, store: Any) -> None:
        self._dec.notes = store
        if DECISIONS_ID not in self._specs or self._specs[DECISIONS_ID] is self._dec.spec:
            self._install_decisions_locked()

    @property
    def note_store(self) -> Any:
        """The store ``attach_note_rows`` was given (None before): ``note_dismiss`` writes through it."""
        return self._dec.notes

    @property
    def question_store(self) -> Any:
        """The store ``attach_question_rows`` was given (None before one is attached): ``question_answer`` writes
        through it, and the verb dispatch hands it to the verb."""
        return self._dec.questions

    @property
    def consent_reader(self) -> Callable[[datetime], Any] | None:
        """The reader ``attach_consent_rows`` was given (None before one is attached): the sessions panel joins
        a session to the requests it has waiting through the same read-only listing."""
        return self._dec.consent

    def _install_decisions_locked(self) -> None:
        """(Re)register the kernel's ``decisions`` panel over the current contributors."""
        src = self._dec.source
        spec = ProviderSpec(
            DECISIONS_ID, "triage-list", src.title if src else "Decisions waiting", "gate", _composed,
            region=src.region if src else WIDGETS, rank=src.rank if src else -1,
            face=src.face if src else "count", size=src.size if src else "medium",
            span=src.span if src else 1, tabs=src.tabs if src else "",
            refresh_every_s=src.refresh_every_s if src else None,
            stale_after_s=src.stale_after_s if src else 900.0, timeout_s=src.timeout_s if src else PANEL_READ_DEADLINE_S,
            order=DECISION_ORDER, facets=KERNEL_DECISION_FACETS | (src.facets if src else frozenset()),
            version_fields=("*",),
            # what a decisions row shows: who asks and for which tool (the title) and its state word and tier
            search_fields=tuple(dict.fromkeys((*((src.search_fields if src else ()) or ROW_SEARCH_DEFAULT["list"]), "consent_state", "consent_tier", "question_state", "note_state"))),
            search_default_visible=src.search_default_visible if src else 10,
            read_one=lambda ctx, row_id: Fault("the decisions panel routes read_one by row id"),
            note=src.note if src else "", empty=src.empty if src else "Nothing is waiting on you.",
            rowset=src.rowset if src else None, edit_class=src.edit_class if src else "",
            # question_answer applies to a question row only (VerbSpec.applies_to), so a downstream row never gets it
            verbs=tuple(dict.fromkeys((*(src.verbs if src else ()),
                                       *((QUESTION_VERB, BAND_VERB) if self._dec.questions is not None else ()),
                                       *((NOTE_VERB,) if self._dec.notes is not None else ())))),
            label_key=src.label_key if src else None)
        old = self._dec.spec
        if old is not None and self._specs.get(DECISIONS_ID) is old:
            self._unregister_locked(DECISIONS_ID)
        self._register_locked(spec)
        self._dec.spec = spec

    def yield_hero(self, panel_id: str) -> None:
        """Mark a registered hero panel as the kernel's default for its slot: a later hero panel of the same kind
        replaces it (it is unregistered, no error) instead of being refused. Levain's own ``activity`` ambient
        yields this way, so a downstream's own hero ambient (flow's ``sky``) needs no opt-out."""
        with self._lock:
            spec = self._specs.get(panel_id)
            if spec is None or spec.region != HERO:
                raise CockpitRegistrationError(f"{panel_id!r} is not a registered hero panel")
            self._hero_yields.add(panel_id)

    def _unregister_locked(self, sid: str) -> None:
        self._hero_yields.discard(sid)
        specs = dict(self._specs)
        old = specs.pop(sid)
        if old.rowset is not None and self._rowsets.get(old.rowset) == sid:
            del self._rowsets[old.rowset]
        states = dict(self._state)
        states.pop(sid, None)
        for k in [k for k in self._replay_cache if k[0] == sid]:
            del self._replay_cache[k]
        self._specs = specs
        self._state = states

    def _is_composed(self, panel_id: str, specs_map: dict[str, ProviderSpec] | None = None) -> bool:
        spec = (specs_map if specs_map is not None else self._specs).get(panel_id)
        return spec is not None and spec is self._dec.spec

    def labels(self) -> LabelSnapshot:
        """One label snapshot. A labeller that raises is an error snapshot, never a failed panel: a
        label is never a gate on reading."""
        if self._capture is not None:
            return self.authority().labels
        fn = self._labeller
        if fn is None:
            return NO_LABELS
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - every labeller fault is "provenance unavailable"
            return unavailable(f"{type(exc).__name__}: {_safe_str(exc)}")

    def authority(self) -> RequestAuthority:
        """The authority one outer request renders and tiers from, captured once (§0.1)."""
        now = self._clock()
        capture = self._capture
        if capture is not None:
            try:
                return capture(now)
            except Exception as exc:  # noqa: BLE001 - a capture fault holds no authority, labels nothing
                return RequestAuthority(now, unavailable(f"{type(exc).__name__}: {_safe_str(exc)}"),
                                        authority_error=f"the authority could not be read: {_safe_str(exc)}")
        return RequestAuthority(now, self.labels())

    def install_class(self) -> str | None:
        """The install class gestures are decided under (the attached verb view's), or None."""
        return self._verb_view.install_class if self._verb_view is not None else None

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
        return spec.with_verbs(tuple(found[0].verbs))

    def today(self) -> date:
        return ReadContext(self._clock()).today

    def _verb_rev(self, auth: RequestAuthority) -> Any:
        return self._verb_view.revision(auth) if self._verb_view is not None else None

    # --- registration ----------------------------------------------------------------
    def register(self, spec: ProviderSpec) -> None:
        """Add a panel. The registry is copy-on-write: a registration builds NEW dicts and swaps
        them in, and nothing ever mutates a dict a reader holds, so a request that took a snapshot
        iterates a stable set with no lock and a concurrent discovery cannot corrupt it."""
        with self._lock:
            self._register_locked(spec)

    def _register_locked(self, spec: ProviderSpec) -> None:
        sid = spec.id
        if sid == DECISIONS_ID and spec is not self._dec.spec and self._is_composed(sid):
            if self._dec.source is not None:
                raise CockpitRegistrationError("panel 'decisions' composes an attached decision source")
            # a downstream's own decisions panel is exactly today's: the broker's rows leave with the kernel's
            self._unregister_locked(sid)
            self._dec.spec = None
        if not sid or sid in self._specs or sid == NOW_ID:
            raise CockpitRegistrationError(f"panel id {sid!r} is empty, reserved or already registered")
        if spec.kind not in KINDS:
            raise CockpitRegistrationError(f"{sid}: unknown kind {spec.kind!r}")
        if spec.priority not in PRIORITIES:
            raise CockpitRegistrationError(f"{sid}: unknown priority {spec.priority!r}")
        if spec.region not in TOP_REGIONS and spec.region not in {z for z, _ in ZONES}:
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
        if spec.kind in ROW_ENGINE_KINDS:
            if spec.order is None or spec.order not in ORDERINGS:
                raise CockpitRegistrationError(f"{sid}: a {spec.kind} names a registered ordering, got {spec.order!r}")
            if not spec.version_fields:
                raise CockpitRegistrationError(f"{sid}: a {spec.kind} declares version_fields")
            seen_views: set[str] = set()
            for v in spec.views:
                if not (isinstance(v, tuple) and len(v) == 3 and all(isinstance(x, str) for x in v)):
                    raise CockpitRegistrationError(f"{sid}: a view is (id, title, ordering), got {v!r}")
                vid, _title, vorder = v
                if not _VIEW_ID.fullmatch(vid) or vid in seen_views:
                    raise CockpitRegistrationError(f"{sid}: view id {vid!r} is malformed, reserved or repeated")
                if vorder not in ORDERINGS or ORDERINGS[vorder].groups:
                    # a grouped view would need its own groups and bands; none is wanted yet
                    raise CockpitRegistrationError(f"{sid}: view {vid!r} names a registered ungrouped ordering")
                seen_views.add(vid)
        elif spec.order is not None or spec.views:
            raise CockpitRegistrationError(f"{sid}: only a list or count-queue has an ordering or views")
        spec = self._place_locked(spec)
        _validate_search_decl(spec)
        if spec.compose is not None:
            # stored detached: a provider that keeps its dict cannot change what was validated
            spec = spec.replaced(compose=copy.deepcopy(spec.compose))
        _validate_compose_decl(spec)
        _check_compose_verbs(spec, self._verb_view)
        if spec.rowset is not None:
            owner = self._rowsets.get(spec.rowset)
            if owner is not None:
                raise CockpitRegistrationError(
                    f"{sid}: row set {spec.rowset!r} already belongs to panel {owner!r} (one panel per row set)"
                )
        if spec.region == HERO:          # validated whole: only now does a yielding default leave the slot
            for o in [o.id for o in self._specs.values()
                      if o.region == HERO and o.kind == spec.kind and o.id in self._hero_yields]:
                self._unregister_locked(o)
        states = dict(self._state)
        states[sid] = _State(read=spec.read)
        specs = dict(self._specs)
        specs[sid] = spec
        if spec.rowset is not None:      # claimed last: a failure above leaves nothing half-registered
            self._rowsets[spec.rowset] = sid
        self._state = states            # state first: a reader that sees the spec finds its state
        self._specs = specs

    def _place_locked(self, spec: ProviderSpec) -> ProviderSpec:
        """Validate a spec's place (region, face, size, span, tabs) against the registry and return the spec
        to store. Registration is incremental, so the hero's at-most-one and a tab card's one size are checked
        against the specs already registered. A legacy ``header`` reading or ambient that finds the hero slot
        of its kind taken is stored in ``operate`` instead of refused: a pre-hero provider cannot have known."""
        sid, kind, region = spec.id, spec.kind, spec.region
        if not isinstance(spec.face, str) or spec.face not in FACES:
            raise CockpitRegistrationError(f"{sid}: face must be '' or 'count', got {spec.face!r}")
        if spec.face == "count" and kind not in ROW_ENGINE_KINDS:
            raise CockpitRegistrationError(f"{sid}: face 'count' belongs to a row-engine kind "
                                           f"({', '.join(ROW_ENGINE_KINDS)}), not {kind!r}")
        if not isinstance(spec.size, str) or spec.size not in SIZES:
            raise CockpitRegistrationError(f"{sid}: size must be one of {', '.join(SIZES)}, got {spec.size!r}")
        if isinstance(spec.span, bool) or not isinstance(spec.span, int) or spec.span not in SPANS:
            raise CockpitRegistrationError(f"{sid}: span must be one of {', '.join(map(str, SPANS))}, got {spec.span!r}")
        if not isinstance(spec.tabs, str) or len(spec.tabs) > TITLE_MAX:
            raise CockpitRegistrationError(f"{sid}: tabs must be a card title of at most {TITLE_MAX} characters")
        if region in TOP_REGIONS:
            if spec.size != "medium" or spec.span != 1 or spec.tabs:
                raise CockpitRegistrationError(f"{sid}: size, span and tabs lay out a zone card; {region} takes none")
        if region == HERO:
            if kind not in HERO_KINDS:
                raise CockpitRegistrationError(
                    f"{sid}: the hero holds a reading and an ambient, not a {kind!r}")
            if any(o.region == HERO and o.kind == kind and o.id not in self._hero_yields
                   for o in self._specs.values()):
                if spec.legacy_region != HEADER:
                    raise CockpitRegistrationError(f"{sid}: the hero holds at most one {kind}")
                return spec.replaced(region="operate")
        elif region == BRIEF:
            if kind not in BRIEF_KINDS:
                raise CockpitRegistrationError(f"{sid}: the brief holds a line or a brief, not a {kind!r}")
        elif region == WIDGETS:
            if kind in ROW_ENGINE_KINDS:
                if spec.face != "count":
                    raise CockpitRegistrationError(
                        f"{sid}: a widget is one answer, never a list; a {kind} in widgets must declare face='count'")
            elif kind not in WIDGET_KINDS:
                raise CockpitRegistrationError(
                    f"{sid}: a widget is one answer, never a list; a {kind} cannot sit in widgets "
                    f"(allowed: {', '.join(WIDGET_KINDS)}, or a list with face='count')")
        elif spec.tabs:
            for o in self._specs.values():
                if o.region == region and o.tabs == spec.tabs and (o.size, o.span) != (spec.size, spec.span):
                    raise CockpitRegistrationError(
                        f"{sid}: tabs {spec.tabs!r} in {region} share one card with {o.id!r}, which is "
                        f"size={o.size} span={o.span}; {sid} asks size={spec.size} span={spec.span}")
        return spec

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

    def _bounded(self, timeout_s: float, st: _State | None, fn: Callable[..., Any], *args: Any,
                 allow_snap: bool = False) -> Any:
        """A provider call that is not a panel read (entity, discovery, read_one): one flight on its own
        state (a throwaway when it has none), the outcome handed back as-is."""
        def produce() -> Any:
            out = fn(*args)
            if not isinstance(out, (Read, Absent, Fault, _Snap) if allow_snap else (Read, Absent, Fault)):
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
            if spec is self._dec.spec:
                return self._compose_decisions(ctx)
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

    def _state_of(self, spec: ProviderSpec) -> _State | None:
        """The state of the generation ``spec`` belongs to, or None once that panel was retired (or retired and
        registered anew: the old provider never runs against the new generation's state)."""
        st = self._state.get(spec.id)
        return st if st is not None and st.read is spec.read else None

    def _snap_for(self, spec: ProviderSpec, ctx: ReadContext) -> _Snap:
        st = self._state_of(spec)
        if st is None:
            return _Snap("error", None, None, [], [], None, "this panel was retired while it was being read", spec.note)
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

    def _compose_decisions(self, ctx: ReadContext) -> Any:
        """The kernel's ``decisions`` panel (K2b-3b §0.4, §3, §8d row 4), in the read worker. Each
        contributor is read and normalised under its OWN spec; finished rows are never sent back
        through ``_rows``. The open source's Fault (or Absent) is the panel's (the M3 rule); a broker
        that cannot be read leaves the open rows and says so in the note. A downstream row whose id
        carries a broker prefix is skipped and counted on the downstream."""
        dec, now_iso = self._dec, _iso(ctx.now)
        rows: list[dict[str, Any]] = []
        keys: dict[str, LabelKey] = {}
        skipped: list[dict[str, Any]] = []
        filtered: list[dict[str, Any]] = []
        notes: list[str] = []
        as_ofs: list[str] = []
        stale = False
        empty: str | None = None
        src = dec.source
        if src is not None:
            try:
                raw = src.read(ctx)
            except Exception as exc:  # noqa: BLE001 - a provider exception is its Fault
                return Fault(f"{type(exc).__name__}: {_safe_str(exc)}")
            out = self._normalize(src, ctx, raw)
            if not isinstance(out, _Snap):
                return out
            mine = [r for r in out.rows or [] if r["id"].startswith(KERNEL_DECISION_PREFIXES)]
            rows += [r for r in out.rows or [] if not r["id"].startswith(KERNEL_DECISION_PREFIXES)]
            keys.update({i: k for i, k in (out.label_keys or {}).items()
                         if not i.startswith(KERNEL_DECISION_PREFIXES)})
            skipped += out.skipped
            if mine:
                skipped.append({"count": len(mine), "reason": "open row skipped: its id carries a broker "
                                                               "prefix (pend:/out:/consent:/question:/note:)"})
            filtered += out.filtered
            if out.note:
                notes.append(out.note)
            if out.as_of:
                as_ofs.append(out.as_of)
            stale = out.status == "stale"
            empty = out.empty
        if dec.rows is None:
            notes.append("no cockpit broker: decisions waiting on a signature are not shown")
        else:
            try:
                got = dec.rows(ctx.now)
                if got is None:
                    notes.append("decisions waiting on a signature are served by the active broker")
                else:
                    bs = self._read_snap(self._broker_dec_spec, Read(
                        rows=tuple(_decision_rows_in(got)),
                        skipped=((got.skipped, "unreadable cockpit op"),) if got.skipped else ()), ctx, now_iso)
                    rows += bs.rows or []
                    skipped += bs.skipped
            except Exception as exc:  # noqa: BLE001 - the gate panel never goes dark over a journal fault
                notes.append(f"broker decisions unavailable: {type(exc).__name__}: {_safe_str(exc)}")
        overflowed = False
        if dec.consent is not None:
            try:
                got = dec.consent(ctx.now)
                built, bad = _consent_rows_in(got, _egress_lookup_of(dec.consent))
                why = tuple((n, reason) for n, reason in (
                    (got.skipped + bad, "consent request whose seal or stamps do not check out"),) if n)
                if got.overflow:
                    overflowed = True
                    notes.append("more than 200 consent requests are waiting: the soonest to expire are shown, "
                                 "the rest are not read")
                cs = self._read_snap(self._broker_dec_spec, Read(rows=tuple(built), skipped=why), ctx, now_iso)
                rows += cs.rows or []
                skipped += cs.skipped
            except Exception as exc:  # noqa: BLE001 - a consent-queue fault is a note, never a dark panel
                notes.append(f"agent consent requests unavailable: {type(exc).__name__}: {_safe_str(exc)}")
        if dec.questions is not None:
            try:
                built, left_out = _question_rows_in(dec.questions, ctx.now)
                if left_out:
                    overflowed = True
                    notes.append("more questions are waiting than are shown: the oldest are shown, the rest are not read")
                qs = self._read_snap(self._broker_dec_spec, Read(rows=tuple(built)), ctx, now_iso)
                rows += qs.rows or []
                skipped += qs.skipped
            except Exception as exc:  # noqa: BLE001 - a question-store fault is a note, never a dark panel
                notes.append(f"agent questions unavailable: {type(exc).__name__}: {_safe_str(exc)}")
        if dec.notes is not None:
            try:
                built, left_out = _note_rows_in(dec.notes, ctx.now)
                if left_out:
                    overflowed = True
                    notes.append(f"{left_out} more notes are waiting than are shown: the oldest are shown, the rest "
                                 "are not read")
                ns = self._read_snap(self._broker_dec_spec, Read(rows=tuple(built)), ctx, now_iso)
                rows += ns.rows or []
                skipped += ns.skipped
            except Exception as exc:  # noqa: BLE001 - a note-store fault is a note on the panel, never a dark one
                notes.append(f"agent notes unavailable: {type(exc).__name__}: {_safe_str(exc)}")
        rows = self._regroup(rows, ctx.today)
        status = "partial" if skipped or overflowed else ("empty" if not counted_rows(DECISION_ORDER, rows) else "ok")
        if stale and _STATUS_RANK["stale"] > _STATUS_RANK[status]:
            status = "stale"
        return _Snap(status, rows, None, filtered, skipped, min(as_ofs) if as_ofs else now_iso, None,
                     "; ".join(n for n in notes if n), stale, empty, label_keys=keys if src is not None and
                     src.label_key is not None else None)

    @staticmethod
    def _regroup(rows: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
        """Finished rows under ``decision.queue``: its group, its order and its band; nothing else of a
        row (version, facets, provenance) changes."""
        o = ORDERINGS[DECISION_ORDER]
        by_id = {r["id"]: r for r in rows}
        ins = [RowIn(id=r["id"], title=r.get("title") or "", facets=r.get("facets") or {}) for r in rows]
        return [{**by_id[rin.id], "group": g, "band": _band(by_id[rin.id].get("facets") or {}, g, o, today)}
                for rin, g in apply_ordering(DECISION_ORDER, ins, today)]

    def _broker_decision_one(self, ctx: ReadContext, row_id: str) -> Result:
        fn = self._dec.rows
        if fn is None:
            return Absent("no cockpit broker")
        got = fn(ctx.now)
        if got is None:
            return Fault("decisions waiting on a signature are served by the active broker")
        for r in _decision_rows_in(got):
            if r.id == row_id:
                return Read(rows=(r,))
        return Absent(f"decision {row_id!r} is gone")

    def _consent_decision_one(self, ctx: ReadContext, row_id: str) -> Result:
        fn = self._dec.consent
        if fn is None:
            return Absent("no consent queue")
        got = fn(ctx.now)
        def is_mine(e: Any) -> bool:
            try:
                return f"{CONSENT_ROW_PREFIX}{e.request.id}" == row_id
            except Exception:  # noqa: BLE001 - an entry whose request cannot be read is skipped, as _consent_rows_in counts it bad
                return False
        mine = SimpleNamespace(**{part: tuple(e for e in getattr(got, part) if is_mine(e))
                                  for part in ("pending", "undecided", "recent")})   # only this entry is built and looked up
        for r in _consent_rows_in(mine, _egress_lookup_of(fn))[0]:
            if r.id == row_id:
                return Read(rows=(r,))
        return Absent(f"decision {row_id!r} is gone")

    def _question_decision_one(self, ctx: ReadContext, row_id: str) -> Result:
        from levain.consent.questions import QuestionStoreError

        store = self._dec.questions
        if store is None:
            return Absent("no question store")
        try:
            q = store.get(row_id[len(QUESTION_ROW_PREFIX):])     # also reads `#3` or `q-…`: only the canonical id is the row
        except (QuestionStoreError, OSError, sqlite3.Error) as exc:
            return Fault(f"agent questions unavailable: {type(exc).__name__}: {_safe_str(exc)}")
        if q is None or row_id != f"{QUESTION_ROW_PREFIX}{q.n}":
            return Absent(f"decision {row_id!r} is gone")
        return Read(rows=(_question_row_in(q),))

    def _note_decision_one(self, ctx: ReadContext, row_id: str) -> Result:
        from levain.consent.notes import NoteStoreError

        store = self._dec.notes
        if store is None:
            return Absent("no note store")
        try:
            n = store.get(row_id[len(NOTE_ROW_PREFIX):])
        except (NoteStoreError, OSError, sqlite3.Error) as exc:
            return Fault(f"agent notes unavailable: {type(exc).__name__}: {_safe_str(exc)}")
        if n is None or row_id != f"{NOTE_ROW_PREFIX}{n.n}":
            return Absent(f"decision {row_id!r} is gone")
        return Read(rows=(_note_row_in(n),))

    def _book(self, spec: ProviderSpec, st: _State, out: Any, ctx: ReadContext) -> _Snap:
        """The owner's bookkeeping for one outcome: failure start, last good, presence. It reads only
        our own types, so it cannot be held by a provider."""
        with st.mu:
            now_iso = _iso(ctx.now)
            listy = spec.kind in ROW_ENGINE_KINDS
            if isinstance(out, _Snap):
                st.ever_present = True
                st.last_good_as_of = out.as_of
                st.failing_since = None
                return out
            if isinstance(out, Absent):
                # a value with no stored record binds its first write to VALUE_ABSENT (§4.4)
                vv = VALUE_ABSENT if spec.kind in VERSIONED_VALUE_KINDS else None
                if spec.optional and not st.ever_present:
                    return _Snap("ok", [] if listy else None, None, [], [], now_iso, None,
                                 f"not configured: {out.reason}", value_version=vv)
                if spec.absent_is_empty and not st.ever_present and listy:
                    return _Snap("empty", [], None, [], [], now_iso, None, spec.note, empty=None, value_version=vv)
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
        if spec.kind in ROW_ENGINE_KINDS:
            if res.rows is None:
                raise ValueError(f"a {spec.kind} Read carries rows")
            rows, refused = self._rows(spec, list(res.rows), ctx)
            if refused:
                skipped.append({"count": refused, "reason": "row refused: a facet value of the wrong type"})
            label_ids = {r["id"] for r in rows}
            held = sorted((r for r in rows if r["facets"].get("held") is True),
                          key=lambda r: (r["facets"].get("due") or "", r["id"]))
            if held:
                rows = [r for r in rows if r["facets"].get("held") is not True]
            status = "partial" if skipped else ("empty" if not counted_rows(spec.order, rows) else "ok")
            value = None
            # the views' orders are computed here, in the read worker, like every other ordering: an
            # ordering key runs on provider data, so its failure is this read's Fault, never a crash
            view_order = self._view_order(spec, rows, ctx.today) if spec.views else None
            label_keys = self._label_keys(spec, list(res.rows), label_ids)
            value_key = None
        else:
            held = []
            view_order = None
            if res.value is None:
                raise ValueError(f"a {spec.kind} Read carries a value")
            value = self._value(spec, res.value, res.version_of)
            rows = None
            status = "partial" if skipped else ("empty" if self._value_empty(spec.kind, value) else "ok")
            label_keys, value_key = None, self._value_label_key(spec, value)
        stale = res.stale or (ctx.now - _parse_iso(as_of)) > timedelta(seconds=spec.stale_after_s)
        if stale and _STATUS_RANK["stale"] > _STATUS_RANK[status]:
            status = "stale"
        return _Snap(status, rows, value, filtered, skipped, as_of, None,
                     res.note if isinstance(res.note, str) else spec.note, res.stale,
                     res.empty if isinstance(res.empty, str) else None,
                     value["value_version"] if spec.kind in VERSIONED_VALUE_KINDS else None, view_order,
                     label_keys, value_key, held)

    @staticmethod
    def _label_key(spec: ProviderSpec, stored: dict[str, Any]) -> LabelKey | None:
        """A row's (or value's) label key. One that cannot be computed is no key (provenance null,
        label ``unverified``): a label never fails a read."""
        if spec.label_key is None:
            return None
        try:
            key = spec.label_key(stored)
        except Exception:  # noqa: BLE001
            return None
        return key if isinstance(key, LabelKey) else None

    def _label_keys(self, spec: ProviderSpec, rows_in: list[RowIn], ids: set[str]) -> dict[str, LabelKey] | None:
        if spec.label_key is None:
            return None
        keys = {r.id: self._label_key(spec, dict(r.stored)) for r in rows_in if r.id in ids}
        return {i: k for i, k in keys.items() if k is not None}

    def _value_label_key(self, spec: ProviderSpec, value: dict[str, Any]) -> LabelKey | None:
        return self._label_key(spec, {k: x for k, x in value.items() if k not in ("provenance", "value_version")})

    @staticmethod
    def _with_provenance(rows: list[dict[str, Any]], keys: dict[str, LabelKey] | None,
                         labels: LabelSnapshot) -> list[dict[str, Any]]:
        """Each row's provenance from the request's snapshot (design §0.1). A panel with no label keys
        renders as read: its rows keep ``provenance: null``."""
        if keys is None:
            return rows
        return [{**r, "provenance": labels.provenance(keys.get(r["id"]))} for r in rows]

    @staticmethod
    def _value_empty(kind: str, value: Any) -> bool:
        if kind == "topology":
            # empty only when it read everything and found nothing: a source it could not read is not "no work"
            return not value.get("nodes") and not value.get("tasks") and not value.get("unreadable")
        if kind == "board":
            # empty only when the queue was read whole and every column is empty: "no queue yet", "could not be read" and a
            # source named unreadable are content (the value says why), never "nothing here"
            return (value.get("queue") == "ok" and not value.get("unreadable")
                    and not any(c.get("cards") for c in value.get("columns") or ()))
        key = {"line": "lines", "status-dots": "metrics", "metric-grid": "numbers", "roster": "rows",
               "log": "rows", "ambient": "events"}.get(kind)
        if key == "events" and "visual" in value:
            return False           # a levain.cockpit/1 visual: its own data is the content
        if kind == "status-dots" and value.get("alerts"):
            return False           # an alert is content even when no metric is
        return bool(key) and not value.get(key)

    @staticmethod
    def _value(spec: ProviderSpec, v: Any, version_of: Any = None) -> Any:
        """The value contract of a value kind (docs/cockpit-contract.md). A provider BUG (the wrong
        shape) raises ValueError naming the panel and the field, which the read turns into a Fault."""
        check = VALUE_VALIDATORS.get(spec.kind)
        if check is None:
            raise ValueError(f"panel {spec.id!r}: {spec.kind} is not a value kind")
        v = check(spec, v)
        if spec.kind in VERSIONED_VALUE_KINDS:
            # the version a value write binds to (§4.4): a hash of the stored value as read, never of
            # provenance (a render-time label) or of itself
            stored = {k: x for k, x in v.items() if k not in ("provenance", "value_version")}
            if spec.kind == "brief":
                v = {**v, "provenance": v.get("provenance")}   # Brief.provenance (rev 9, §3.2.2); null until K2b
            v = {**v, "value_version": _sha(stored)[:16] if version_of is None else value_version_of(version_of)}
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
        return row_version_of(stored, spec.version_fields, spec.version_excluded)

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
        band = _band(f, group, o, today)
        return {
            "id": row.id,
            "version": self.row_version(spec, row.stored),
            "group": group,
            "title": title,
            "truncated": truncated,
            "body": row.body,
            # the one judge of the page's hidden-text lock (levain.unshowable, over the provider's raw title and
            # body, newline and tab kept as text): the page offers a row's inline text edit only when this is False,
            # and a compact profile that drops the body keeps it
            "text_hidden": any(isinstance(t, str) and unshowable.first_hidden(t, allow="\n\t") is not None
                               for t in (row.title, row.body)),
            "facets": dict(f),
            "emphasis": "accent" if isinstance(overdue, int) and overdue > 0 else row.emphasis,
            "band": band,
            "badges": badges,
            "provenance": None,   # computed at render from a row's signature envelope (K2b)
            "actions": [],        # verbs and their tiers arrive with K2a
        }

    # --- heads, panels, the now view --------------------------------------------------
    def _etag_of(self, snap: _Snap, groups: list[dict[str, Any]] | None, cred: str, today: date,
                 views: tuple[tuple[str, str, str], ...] = (), labels: LabelSnapshot | None = None,
                 face: str = "", compose: dict[str, Any] | None = None, search: dict[str, Any] | None = None,
                 *, auth: RequestAuthority) -> str:
        # Rev 9 (§3.4): the panel etag covers status, rows or value, the credential class and the
        # current date in the install's zone (a held date passing at midnight changes row bands, and
        # from K2a the tiers). ``as_of`` stays outside, so a refresher over unchanged rows still 304s.
        return _sha({"schema": SCHEMA, "status": snap.status, "rows": snap.rows, "value": snap.value,
                     "filtered": snap.filtered, "skipped": snap.skipped, "error": snap.error,
                     "note": snap.note, "empty": snap.empty, "groups": groups, "credential": cred, "date": today.isoformat(),
                     # a panel with no held rows hashes exactly as it did before they existed
                     **({"held": snap.held} if snap.held else {}),
                     "verbs": self._verb_rev(auth),
                     # a panel's views shape view_rows: a kernel that adds or reorders one is not a 304
                     **({"views": [list(v) for v in views]} if views else {}),
                     # the face is drawn from the head, so changing it is not a 304
                     **({"face": face} if face else {}),
                     # the compose card is drawn from the head, so a changed declaration is not a 304
                     **({"compose": compose} if compose else {}),
                     # the search head is drawn from the head too: a kernel that starts marking a panel searchable
                     # (or changes its fields) over unchanged rows is not a 304
                     **({"search": search} if search else {}),
                     # K2b-3b §0.3: provenance is applied at render, so the journal's revision (and a
                     # snapshot's error) is content; with no labeller there is neither
                     **({"labels": [list(labels.revision) if labels.revision is not None else None, labels.error]}
                        if labels is not None and (labels.revision is not None or labels.error is not None)
                        else {})})

    def _head(self, spec: ProviderSpec, snap: _Snap, cred: str, today: date,
              auth: RequestAuthority, now: datetime | None = None) -> dict[str, Any]:
        labels = auth.labels
        listy = spec.kind in ROW_ENGINE_KINDS
        # only a panel whose rows (or value) carry labels renders the snapshot
        labelled = labels if spec.label_key is not None else None
        note = _label_note(snap.note, labelled)
        # an ordering's uncounted groups (K2b-3b §3) are carried, never counted
        vrows = spec.kind in VALUE_ROW_KINDS
        count = (len(counted_rows(spec.order, snap.rows)) if (listy and snap.rows is not None)
                 else (0 if listy and snap.status != "error" else None))
        if vrows and snap.value is not None:
            count = len(snap.value["rows"])
        elif vrows and snap.status != "error":
            count = 0
        groups = None
        if listy and spec.order and ORDERINGS[spec.order].groups:
            tally: dict[str, int] = {}
            for r in snap.rows or []:
                tally[r["group"]] = tally.get(r["group"], 0) + 1
            o = ORDERINGS[spec.order]
            groups = [{"id": gid, "title": t, "count": tally.get(gid, 0),
                       **({"uncounted": True} if gid in o.uncounted else {})} for gid, t in o.groups]
        title = spec.title
        if snap.status == "error":
            title += " (unavailable)"
        elif count is not None:
            title += f" ({count})"
        search_head = self._search_head(spec, len(snap.rows) if listy and snap.rows is not None else count)
        etag = self._etag_of(snap, groups, cred, today, spec.views, labelled, spec.face, spec.compose, search_head, auth=auth)
        if spec.kind == "roster" and now is not None and isinstance(snap.value, dict):
            # a roster's health is read against the clock, so a tone that turns with time is content
            etag = _sha({"base": etag, "health": [[r["name"], r["health"]["tone"], r["health"]["text"]]
                                                  for r in _with_health(snap.value, now)["rows"]]})
        return {
            "id": spec.id, "kind": spec.kind, "title": title, "priority": spec.priority,
            "rank": spec.rank, "status": snap.status,
            "count": None if snap.status == "error" else count,
            "actionable_count": None if snap.status == "error" else count,
            "filtered": snap.filtered, "skipped": snap.skipped,
            "as_of": snap.as_of, "stale_after_s": spec.stale_after_s,
            "today": today.isoformat(),   # the server's calendar day: the page counts "overdue" / "today" against this, never its own clock
            "refresh_every_s": spec.refresh_every_s,
            "etag": etag,
            "error": snap.error, "note": note, "empty": spec.empty if snap.empty is None else snap.empty,
            "order": spec.order, "groups": groups,
            "views": [{"id": i, "title": t, "order": o} for i, t, o in spec.views] or None,
            **search_head,
            "actions": (self._verb_view.panel_actions(spec.id, cred, today, snap.value_version,
                                                      labels.label(snap.value_label_key), auth=auth)
                        if self._verb_view is not None else []),
            "edit_class": spec.edit_class or None,
            **({"face": spec.face} if spec.face else {}),
            # the facets the panel DECLARES: a renderer decides what its rows mean (an addressed list) from this,
            # never from the values the current rows happen to carry
            **({"facets": sorted(spec.facets)} if spec.facets else {}),
            **({"compose": copy.deepcopy(spec.compose)} if spec.compose else {}),
            # the panel has a past read (``?at=``): a renderer draws a replay control only when this says so
            **({"replay": True} if spec.read_at is not None and spec.kind not in ROW_ENGINE_KINDS else {}),
        }

    @staticmethod
    def _search_head(spec: ProviderSpec, count: int | None) -> dict[str, Any]:
        """The searchable property (contract v2): the kernel marks a row-shaped panel ``searchable`` when
        it holds more rows than SEARCH_THRESHOLD (every row it delivers, not only the counted ones: a decisions panel
        with two waiting and fifty outcomes is a long list), is windowed, or is drawn as a count (``face == "count"``)
        with at least one row: its modal is the whole list, so the box is there from the first row. A contributor declares WHAT is searched
        (``search.fields``, ``scope``), never WHETHER."""
        if spec.kind not in ROW_KINDS:
            return {"search": None, "searchable": False, "window": None}
        # a panel drawn as a count opens into a focus modal that is its whole list (Decisions waiting, Sessions): it
        # carries the box from the first row, since the tile is where it is short and the modal is where it is read
        searchable = (spec.window is not None or (count or 0) > SEARCH_THRESHOLD
                      or (spec.face == "count" and (count or 0) > 0))
        return {
            "search": {"fields": list(search_fields_of(spec)), "scope": spec.search_scope,
                       "default_visible": roster_visible(spec), "hook": spec.search is not None},
            "searchable": searchable, "window": spec.window,
        }

    def _now_head_and_rows(
        self, ctx: ReadContext, cred: str, specs_map: dict[str, ProviderSpec],
        snaps: dict[str, _Snap] | None = None
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """The ``now`` view (design §3.2.4): the ``band == "now"`` rows of the gate panels, in panel
        rank order, each panel's rows in that panel's own order. No ordering, provider or actions of
        its own; every row keeps its source ``panel_id``. Its status is the worst of its sources'."""
        sources = sorted(
            (s for s in specs_map.values() if s.priority == "gate" and s.kind in ROW_ENGINE_KINDS),
            key=lambda s: (_REGION_ORDER.index(s.region), s.rank, s.id),
        )
        rows: list[dict[str, Any]] = []
        degraded: list[str] = []
        lines: list[str] = []
        worst = "ok"
        as_ofs: list[str] = []
        snaps = snaps if snaps is not None else {}
        auth = ctx.authority
        if auth is None:
            raise ValueError("the now view renders under a request's authority (ReadContext.authority)")
        labels = auth.labels
        labelled = False
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
            labelled = labelled or s.label_key is not None
            for r in self._with_provenance(counted_rows(s.order, snap.rows or []), snap.label_keys, labels):
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
            "id": NOW_ID, "kind": "list", "title": f"Now ({len(rows)})" if worst != "error" else "Now (unavailable)",
            "priority": "gate", "rank": -100, "status": worst, "count": len(rows) if worst != "error" else None,
            "actionable_count": len(rows) if worst != "error" else None,
            "filtered": [], "skipped": [], "as_of": snap.as_of, "stale_after_s": 0, "refresh_every_s": None,
            "etag": self._etag_of(snap, None, cred, ctx.today, auth=auth), "error": err,
            # the Now view applies the snapshot to its rows, so it explains a degraded one as _head does
            "note": _label_note(snap.note, labels if labelled else None),
            "empty": "Nothing needs you now.", "order": None, "groups": None,
            # the gate view is row-shaped, so it carries the same filter box as every other long panel (10-11),
            # over the rows' own title/body/id. It is never cut to a first screenful: a row waiting on him is not
            # hidden behind "show all", so default_visible is the row count itself.
            "search": {"fields": list(ROW_SEARCH_DEFAULT["list"]), "scope": "rows", "default_visible": max(len(rows), 1),
                       "hook": False} if rows else None,
            "searchable": bool(rows), "window": None,
            "actions": [], "degraded": degraded, "edit_class": None,
        }
        # the union of the source panels' declared groups (labels and order), counted over the now
        # rows, so a renderer prints "Today" / "Overdue", not the raw ids the rows carry
        union: dict[str, dict[str, Any]] = {}
        for src in sources:
            if src.order and ORDERINGS[src.order].groups:
                for gid, title in ORDERINGS[src.order].groups:
                    if gid in ORDERINGS[src.order].uncounted:
                        continue      # the now view takes counted groups only
                    union.setdefault(gid, {"id": gid, "title": title, "count": 0})
        for r in rows:
            if r["group"] in union:
                union[r["group"]]["count"] += 1
        head["groups"] = list(union.values()) or None
        head["etag"] = self._etag_of(snap, head["groups"], cred, ctx.today,   # groups are content
                                     labels=labels if labelled else None, auth=auth)
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

    def discover(self, fn: Callable[[ReadContext], list[ProviderSpec]], *, retire: bool = False) -> None:
        """Register a discoverer (``retire=True``: a panel it stops returning is UNREGISTERED, not left reading
        ``Absent``, for a source whose panel set legitimately changes shape, e.g. a team that appears or goes
        away): run when a manifest is built (or a panel nobody registered is
        asked for), it returns the panels the substrate holds NOW (prose panels, one per heading).
        Ones not yet registered are added, and one already discovered takes the verbs its source
        offers now (a write re-checks them fresh, ``offer_spec``); one that later vanishes keeps its
        provider, which then reads ``Absent`` and renders ``error``, and offers nothing to a write. A discoverer that raises is a manifest ``errors``
        entry, never silent absence. All discoverers share ONE bounded single-flight read. A ``retire=True``
        discoverer must return a NEW ``read`` callable for a panel it re-registers after retiring it: a retired
        panel's state is matched to its spec by that callable's identity."""
        with self._lock:
            if retire:
                self._retiring = self._retiring | {len(self._discoverers)}
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
            # the id exactly str before it is hashed: a str subclass can carry its own __hash__ (None, or
            # one that hangs). A discoverer is the embedding server's own code; its other fields are not
            # re-typed here
            ids = Counter(s.id for s in res.value if type(s.id) is str)   # a bad id is an errors entry below
            for spec in res.value:
                if type(spec.id) is not str:
                    errors.append({"source": f"discovery:{i}", "message": "a discovered panel id must be a string"})
                    continue
                if ids[spec.id] > 1:
                    # one id returned twice in one answer: neither copy is believed (a write asks the
                    # same question, offer_spec, and refuses too)
                    if spec.id not in returned:
                        errors.append({"source": f"discovery:{spec.id}",
                                       "message": f"discoverer {i} returned this id {ids[spec.id]} times"})
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
                    elif (tuple(have.verbs) != tuple(spec.verbs) or have.rank != spec.rank
                          or have.compose != spec.compose):
                        # what a discovered panel offers, its rank among its owner's panels and its compose
                        # card are its owner's answer, read per pass (a heading moved while serving moves
                        # its tab) (a non-int rank never gets here: ProviderSpec refuses it inside the
                        # discoverer). The new answer is validated as a registration is; one that fails is
                        # an errors entry and the panel offers nothing until its owner answers validly
                        new = have.replaced(verbs=tuple(spec.verbs), rank=spec.rank,
                                            compose=copy.deepcopy(spec.compose))
                        try:
                            _validate_compose_decl(new)
                            _check_compose_verbs(new, self._verb_view)
                        except Exception as exc:  # noqa: BLE001 - a bad answer is an errors entry, never a 500
                            errors.append({"source": f"discovery:{spec.id}", "message": _safe_str(exc)})
                            new = have.replaced(verbs=(), rank=spec.rank)
                        specs = dict(self._specs)       # copy-on-write, like a registration
                        specs[spec.id] = new
                        self._specs = specs
            with self._lock:
                # a panel its owner no longer returns keeps its provider (it reads Absent) and offers nothing
                for pid in [p for p, o in self._discovered.items() if o == i and p not in returned]:
                    if i in self._retiring:
                        self._unregister_locked(pid)
                        self._discovered = {k: v for k, v in self._discovered.items() if k != pid}
                    elif self._specs[pid].verbs:
                        self._set_verbs_locked(pid, ())
        return errors

    def _set_verbs_locked(self, panel_id: str, verbs: tuple[str, ...]) -> None:
        specs = dict(self._specs)       # copy-on-write, like a registration
        specs[panel_id] = specs[panel_id].with_verbs(verbs)
        self._specs = specs

    @staticmethod
    def _ordered_specs(specs_map: dict[str, ProviderSpec]) -> list[ProviderSpec]:
        return sorted(specs_map.values(), key=lambda s: (_REGION_ORDER.index(s.region), s.rank, s.id))

    def manifest(self, credential: dict[str, Any], *, install_class: str | None = None,
                 transport: dict[str, Any] | None = None) -> dict[str, Any]:
        """``transport`` is the serving transport's write capability (``writable``,
        ``write_token_required``), which only the server knows; it is published when given."""
        auth = self.authority()
        ctx = ReadContext(auth.now, authority=auth)
        errors = self._ensure_discovered(ctx)
        entity, entity_errors = self._entity(ctx)
        errors += entity_errors
        heads: dict[str, dict[str, Any]] = {}
        snaps: dict[str, _Snap] = {}
        specs_map = self._specs      # one snapshot for the whole manifest
        for spec in self._ordered_specs(specs_map):
            snaps[spec.id] = self._snap_for(spec, ctx)
            heads[spec.id] = self._head(spec, snaps[spec.id], credential["class"], ctx.today, auth, ctx.now)
        now_head, _ = self._now_head_and_rows(ctx, credential["class"], specs_map, snaps)
        heads[NOW_ID] = now_head
        specs = self._ordered_specs(specs_map)
        regions = self._regions(specs)
        order = [NOW_ID] + [s.id for s in specs]
        # regions are content: a card's size or tabs changes no panel's etag, so they are hashed here
        etag = _sha({"schema": SCHEMA, "panels": [heads[i]["etag"] for i in order], "regions": regions,
                     "credential": credential, "verbs": self._verb_rev(auth),
                     "install_class": install_class, "authority": auth.revision,   # DESIGN §4.2: the one counter
                     "entity": entity, "errors": errors,    # identity and manifest faults are content too
                     "transport": transport})
        out = {
            "schema": SCHEMA, "entity": entity, "generated_at": _iso(ctx.now), "etag": etag,
            "credential": credential, "regions": regions,
            "panels": {i: heads[i] for i in order},
            "verbs": self._verb_view.verbs_map(credential["class"], auth=auth) if self._verb_view is not None else {},
            "errors": errors,
        }
        if transport is not None:
            out["transport"] = dict(transport)
        return out

    @staticmethod
    def _regions(specs: list[ProviderSpec]) -> dict[str, Any]:
        """The manifest's ``regions``. ``specs`` arrives in drawing order. ``header`` is COMPAT: what a renderer
        that predates hero and brief reads (the hero, the brief, then the widgets, so none disappears: the old
        page draws an unknown header id into Operate), derived and never declared. A zone's flat ``panels``
        list is likewise kept beside its ``cards``; ``cards`` is authoritative for layout, and the flat list
        follows rank order, which differs from card order when tab members are not rank-contiguous."""
        hero = [s.id for s in specs if s.region == HERO and s.kind == "reading"] + \
               [s.id for s in specs if s.region == HERO and s.kind != "reading"]
        brief = [s.id for s in specs if s.region == BRIEF]
        zones = []
        for zid, zt in ZONES:
            members = [s for s in specs if s.region == zid]
            cards: list[dict[str, Any]] = [
                {"panels": [NOW_ID], "size": "medium", "span": 1, "tabs": None}] if zid == "operate" else []
            by_tabs: dict[str, dict[str, Any]] = {}
            for s in members:
                if s.tabs and s.tabs in by_tabs:
                    by_tabs[s.tabs]["panels"].append(s.id)
                    continue
                card = {"panels": [s.id], "size": s.size, "span": s.span, "tabs": s.tabs or None}
                cards.append(card)
                if s.tabs:
                    by_tabs[s.tabs] = card
            zones.append({"id": zid, "title": zt, "grid": ZONE_GRID,
                          "panels": ([NOW_ID] if zid == "operate" else []) + [s.id for s in members],
                          "cards": cards})
        widgets = [s.id for s in specs if s.region == WIDGETS]
        return {"hero": hero, "brief": brief, "widgets": widgets,
                "header": hero + brief + widgets, "zones": zones}

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
            st = self._state_of(spec)
            if st is None:
                out[spec.id] = dict(unread)
                continue
            with st.lock:
                fresh = st.fresh
            if fresh is None:
                out[spec.id] = dict(unread)
                continue
            as_of, status = fresh
            if status in ("ok", "empty", "partial") and as_of and \
                    (ctx.now - _parse_iso(as_of)) > timedelta(seconds=spec.stale_after_s):
                status = "stale"
            out[spec.id] = {"as_of": as_of, "status": status}
        gate = [out[s.id] for s in specs_map.values() if s.priority == "gate" and s.kind in ROW_ENGINE_KINDS]
        order = ("error", "unread", "stale", "partial")
        status = next((o for o in order if any(g["status"] == o for g in gate)), "ok")
        as_ofs = [g["as_of"] for g in gate if g["as_of"]]
        return {NOW_ID: {"as_of": min(as_ofs) if as_ofs and status != "unread" else None, "status": status}, **out}

    def panel(self, panel_id: str, *, profile: str = "full", q: str | None = None,
              row: str | None = None, credential_class: str = "none",
              limit: int | None = None, at: str | None = None) -> dict[str, Any] | None:
        """The ``Panel`` payload, or None for an unknown panel id. ``profile`` is ``full`` or
        ``compact``; ``q`` is the search (plain text, bounded: ``check_query``) over the panel's declared
        search fields, answered by the provider's ``search`` hook when the panel is a store-scope window;
        ``limit`` caps a store search; ``row`` returns one row in full. ``q`` is never logged or journalled.
        ``at`` is a past read (``_panel_at``)."""
        if at is not None:
            if q is not None or row is not None or limit is not None:
                raise QueryError("at is a read of the whole panel: it takes no q, row or limit")
            return self._panel_at(panel_id, at, profile=profile, credential_class=credential_class)
        q, limit = check_query(q, limit)
        auth = self.authority()
        ctx = ReadContext(auth.now, authority=auth)
        specs_map = self._specs
        if panel_id != NOW_ID and panel_id not in specs_map:
            self._ensure_discovered(ctx)      # a panel we do not know yet may be a heading added since
            specs_map = self._specs
        if panel_id == NOW_ID:
            head, rows = self._now_head_and_rows(ctx, credential_class, specs_map)
            spec = None
            snap_rows: list[dict[str, Any]] | None = rows
            held_rows: list[dict[str, Any]] = []
            value = None
            view_order = None
        else:
            spec = specs_map.get(panel_id)
            if spec is None:
                return None
            snap = self._snap_for(spec, ctx)
            head = self._head(spec, snap, credential_class, ctx.today, auth, ctx.now)
            snap_rows, value, view_order = snap.rows, snap.value, snap.view_order
            if snap_rows is not None:
                snap_rows = self._with_provenance(snap_rows, snap.label_keys, auth.labels)
            held_rows = self._with_provenance(snap.held, snap.label_keys, auth.labels) if snap.held else []
            if spec.label_key is not None and spec.kind in VERSIONED_VALUE_KINDS and isinstance(value, dict):
                value = {**value, "provenance": auth.labels.provenance(snap.value_label_key)}
        out: dict[str, Any] = dict(head)
        out["rows"], out["value"], out["next"] = None, None, None
        if head["status"] == "error" and panel_id != NOW_ID:
            return out    # an error never carries rows or a value: nothing stale passes as a read
        if head["kind"] in ROW_ENGINE_KINDS:
            rows_all = snap_rows or []
            if row is not None:
                hit = [r for r in rows_all + held_rows if r["id"] == row]
                out["rows"] = self._with_actions(hit, panel_id, credential_class, ctx.today, auth)
                if not hit:
                    raise RowNotFound(row)
                return out
            matched = None
            if q and spec is not None:   # the now view declares no search
                fields = search_fields_of(spec)
                held = len(rows_all)
                if spec.search_scope == "store" and spec.search is not None:
                    got, total, err = self._store_search(spec, q, limit)
                    held_rows = []  # a store search answers from the store alone, held rows included
                    if err is None:
                        try:
                            for x in got:
                                if not isinstance(x, RowIn):
                                    raise TypeError(f"a {spec.kind} search hook returns RowIn rows, got {type(x).__name__}")
                            found, refused = self._rows(spec, got, ctx)
                            if refused:
                                raise ValueError(f"{refused} row(s) refused: a facet value of the wrong type")
                            if total is not None and total < len(found):
                                raise ValueError(f"total {total} is below the {len(found)} rows returned")
                            keys = self._label_keys(spec, got, {r["id"] for r in found})
                            rows_all = self._with_provenance(found, keys, auth.labels)
                            matched = total if total is not None else len(rows_all)
                            out["match_label"] = f"{matched} matches in the store"
                        except Exception as exc:  # noqa: BLE001 - a hook's bad rows are a search fault, not a crash
                            err = f"search hook returned unusable rows: {_safe_str(exc)}"
                    if err is not None:
                        out["search_error"] = err
                        rows_all, matched = [], None
                else:
                    keep = [r for r in rows_all if _terms_match([_field_text(r, f) for f in fields], q)]
                    held_rows = [r for r in held_rows if _terms_match([_field_text(r, f) for f in fields], q)]
                    matched = len(keep)
                    rows_all = keep
                    out["match_label"] = (f"searches the newest {spec.window} only: {matched} of {held}"
                                          if spec.window is not None else f"{matched} of {held}")
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
            if held_rows and (profile != "compact" or q or head["priority"] == "gate"):
                out["held"] = [{**r, "body": None} for r in held_rows] if profile == "compact" else held_rows
            if spec is not None and spec.views and out["rows"] is not None and view_order is not None:
                sent = {r["id"] for r in rows_all}
                out["view_rows"] = {v: [i for i in ids if i in sent] for v, ids in view_order.items()}
            if out["rows"] is not None and not out.get("rows_by_ref"):
                out["rows"] = self._with_actions(out["rows"], panel_id, credential_class, ctx.today, auth)
            if out.get("held"):
                out["held"] = self._with_actions(out["held"], panel_id, credential_class, ctx.today, auth)
        else:
            if head["kind"] in VALUE_ROW_KINDS and isinstance(value, dict):
                if q and spec is not None:
                    value = self._search_value_rows(spec, value, q, limit, out)
                if head["kind"] == "log":
                    value = self._log_sentences(value, credential_class, auth)
                if head["kind"] == "roster":
                    value = _with_health(value, ctx.now)
            if profile == "compact":
                out["value"] = _compact_value(head["kind"], value)
                if head["priority"] == "feed" or head["kind"] in ("ambient", "brief", "topology", "board"):
                    out["next"] = f"/cockpit/panel/{panel_id}.json?profile=full"
                if head["priority"] == "feed" and head["kind"] in ("line", "status-dots"):
                    out["value"] = None
            else:
                out["value"] = value
        if q and spec is not None and ("match_label" in out or "search_error" in out):
            self._seal_search(out)
        return out

    def _panel_at(self, panel_id: str, at: str, *, profile: str, credential_class: str) -> dict[str, Any] | None:
        """The panel as it was at the past instant ``at`` (F12 Replay): the provider's ``read_at`` under the panel's own
        timeout, called with this request's context (its authority, its ``now``) and T, served on the same route and
        under the same read gate as the live read. Only a value panel whose provider declares a past read has one
        (else ``QueryError``: a 400); T outside ``check_replay_at``'s bounds is refused, never clamped. It never
        touches the live read's state: it does not join the live flight, commit a snapshot or move ``last good``.
        The payload is the live one's shape plus ``at`` (T, as checked) and ``replay: true``; the value carries its
        own ``as_of`` (T). Its validator covers T, so two instants never share an ETag."""
        auth = self.authority()
        ctx = ReadContext(auth.now, authority=auth)
        spec = self._specs.get(panel_id)
        if spec is None:
            return None
        if spec.read_at is None or spec.kind in ROW_ENGINE_KINDS:
            raise QueryError(f"panel {panel_id!r} has no past read")
        t = check_replay_at(at, ctx.now)
        iso = t.isoformat()
        ctx.today = t.astimezone().date()           # a past read's calendar day is T's, not the server's today
        snap = self._replay_snap(spec, ctx, iso)
        out: dict[str, Any] = dict(self._head(spec, snap, credential_class, ctx.today, auth, ctx.now))
        out["etag"] = _sha({"base": out["etag"], "at": iso})
        out["rows"], out["value"], out["next"] = None, None, None
        out["at"], out["replay"] = iso, True
        if out["status"] == "error":
            return out
        out["value"] = _compact_value(spec.kind, snap.value) if profile == "compact" else snap.value
        return out

    def _replay_snap(self, spec: ProviderSpec, ctx: ReadContext, iso: str) -> "_Snap":
        """One past read of ``spec`` at ``iso``, as a snapshot: ``read_at`` AND its normalisation run inside the one
        bounded call (the live read's shape: provider data is never touched on the request thread), in the replay
        pool (``REPLAY_READ_CAP`` at once, apart from the write pool). A good answer is kept ``REPLAY_CACHE_S`` under
        (panel, minute), so a client re-reading one minute reads the source once; a refusal or fault is never kept."""
        key = (spec.id, iso)
        if self._state_of(spec) is None:             # retirement is checked before the cache: a retired panel is never served
            raw: Any = Fault("this panel was retired while it was being read")
        else:
            with self._lock:
                hit = self._replay_cache.get(key)
                if hit is not None and hit[1] is spec and time.monotonic() - hit[0] < REPLAY_CACHE_S:
                    return hit[2]
            def once(c: ReadContext) -> Any:
                return self._normalize(spec, c, spec.read_at(c, iso))     # type: ignore[misc]
            raw = self._bounded_fresh(("replay", spec.id), spec.timeout_s, once, ctx, pool="replay", allow_snap=True)
        if isinstance(raw, _Snap):
            if raw.status != "error":
                mono = time.monotonic()               # the entry's age starts when the read succeeded, not before the provider ran
                with self._lock:
                    if len(self._replay_cache) >= REPLAY_CACHE_MAX:
                        self._replay_cache.pop(min(self._replay_cache, key=lambda k: self._replay_cache[k][0]))
                    self._replay_cache[key] = (mono, spec, raw)
            return raw
        msg = raw.message if isinstance(raw, Fault) else f"source absent: {getattr(raw, 'reason', '')}"
        return _Snap("error", None, None, [], [], None, _safe_str(msg), spec.note)

    @staticmethod
    def _seal_search(out: dict[str, Any]) -> None:
        """A searched answer is its own response: its status says whether it returned rows (a window
        that is empty today can still have store matches), and its validator covers what it returned,
        so a revalidation never 304s on results that changed or a search that recovered."""
        shown = out["rows"] if out["rows"] is not None else (out["value"] or {}).get("rows")
        shown = shown or out.get("held")
        if out["status"] in ("ok", "empty") and "search_error" not in out:
            out["status"] = "ok" if shown else "empty"
        out["etag"] = _sha({"base": out["etag"], "status": out["status"], "rows": out["rows"], "value": out["value"],
                            "matched": out.get("matched"), "label": out.get("match_label"),
                            "error": out.get("search_error"),
                            **({"held": out["held"]} if out.get("held") else {})})

    def _store_search(self, spec: ProviderSpec, q: str, limit: int) -> tuple[list[Any], int | None, str | None]:
        """Run the provider's ``search(q, limit)`` hook, bounded by the panel's own timeout. Returns
        ``(rows, total, error)``: a hook that fails or overruns is an ERROR the caller reports, never an
        empty answer that reads as "nothing matches".

        The hook's answer is drained INSIDE the timed worker, at most ``limit`` rows (a generator or a
        lazy sequence cannot run on the request thread). Searches on one panel may overlap (a newer
        query while an older one is still running is ordinary typing), up to SEARCH_INFLIGHT live
        workers per panel, counted whether or not their requests have timed out; past that the panel
        answers "search busy". So hung hooks cost a bounded number of threads per panel, never one per
        request."""
        fut: Future = Future()

        def run() -> None:
            try:
                res = spec.search(q, limit)    # type: ignore[misc]
                total = None
                if isinstance(res, StoreMatches):
                    res, total = res.rows, res.total
                try:
                    it = iter(res)
                except TypeError:
                    raise TypeError("search hook must return a sequence of rows") from None
                rows = list(itertools.islice(it, limit))
                if total is not None and (isinstance(total, bool) or not isinstance(total, int) or total < len(rows)):
                    raise ValueError(f"search hook total {total!r} is not a count of at least the {len(rows)} rows returned")
                _set_once(fut, (rows, total))
            except BaseException as exc:  # noqa: BLE001 - whatever the hook raised is its answer
                if not fut.done():
                    fut.set_exception(exc)

        worker = threading.Thread(target=run, daemon=True, name=f"cockpit-search-{spec.id}")
        with self._search_mu:
            live = [t for t in self._searching.get(spec.id, ()) if t.is_alive()]
            if len(live) >= SEARCH_INFLIGHT:
                self._searching[spec.id] = live
                return [], None, "search busy"
            live.append(worker)
            self._searching[spec.id] = live
            worker.start()
        try:
            exc = fut.exception(timeout=spec.timeout_s)
        except FutureTimeout:
            return [], None, f"search timed out after {spec.timeout_s:g}s"
        if exc is not None:
            return [], None, f"search failed: {_safe_str(exc)}"
        rows, total = fut.result()
        return rows, total, None

    def _search_value_rows(self, spec: ProviderSpec, value: dict[str, Any], q: str, limit: int,
                           out: dict[str, Any]) -> dict[str, Any]:
        """``q`` over a roster or log panel's rows: the delivered rows filtered by the declared fields, or
        (scope store with a hook) the whole store's answer, validated by the kind's own contract."""
        rows = value["rows"]
        fields = search_fields_of(spec)
        if spec.search_scope == "store" and spec.search is not None:
            got, total, err = self._store_search(spec, q, limit)
            if err is not None:
                out["search_error"] = err
                return {**value, "rows": []}
            try:
                keep = VALUE_VALIDATORS[spec.kind](spec, {"rows": got})["rows"]
            except Exception as exc:  # noqa: BLE001 - a hook's bad rows are a search fault, not a crash
                out["search_error"] = f"search hook returned unusable rows: {_safe_str(exc)}"
                return {**value, "rows": []}
            out["matched"] = total if total is not None else len(keep)
            out["match_label"] = f"{out['matched']} matches in the store"
            return {**value, "rows": keep}
        keep = [r for r in rows if _terms_match([str(r.get(f) or "") for f in fields], q)]
        out["matched"] = len(keep)
        out["match_label"] = (f"searches the newest {spec.window} only: {len(keep)} of {len(rows)}"
                              if spec.window is not None else f"{len(keep)} of {len(rows)}")
        return {**value, "rows": keep}

    def _log_sentences(self, value: dict[str, Any], cred: str, auth: RequestAuthority) -> dict[str, Any]:
        """One sentence per log row from the manifest's verb LABELS ("Snoozed · spore-1238"); a verb with
        no label renders as its raw name."""
        try:
            labels = {n: v.get("label") for n, v in
                      (self._verb_view.verbs_map(cred, auth=auth) if self._verb_view else {}).items()}
        except Exception:  # noqa: BLE001 - a label lookup never fails a read
            labels = {}
        return {**value, "rows": [
            {**r, "sentence": f"{labels.get(r['verb']) or r['verb']} \u00b7 {r['target']}"} for r in value["rows"]]}

    @staticmethod
    def _view_order(spec: ProviderSpec, rows: list[dict[str, Any]], today: date) -> dict[str, list[str]]:
        """Each view's row ids in that view's order. The views' orderings key on facets only, which
        every rendered row carries whole."""
        ins = [RowIn(id=r["id"], title=r.get("title") or "", facets=r.get("facets") or {}) for r in rows]
        return {vid: [row.id for row, _g in apply_ordering(vorder, ins, today)] for vid, _t, vorder in spec.views}

    def _with_actions(self, rows: list[dict[str, Any]], panel_id: str, cred: str, today: date,
                      auth: RequestAuthority) -> list[dict[str, Any]]:
        """Each row's actions, rendered for this credential (design §4.1: the row's own tier and
        gesture plus the param values that escalate it). A now-view row takes its source panel's."""
        if self._verb_view is None:
            return rows
        out = []
        for r in rows:
            pid = r.get("panel_id") or panel_id
            if (self._is_composed(pid) and str(r["id"]).startswith(KERNEL_DECISION_PREFIXES)
                    and not str(r["id"]).startswith(_VERB_ROW_PREFIXES)):
                # a broker row offers no panel verb; a pending offers its own cancel (§3). A question or note row is
                # the exception: it offers question_answer / note_dismiss through the ordinary row path (applies_to)
                acts = (self._verb_view.pending_actions(r, cred, auth=auth)
                        if str(r["id"]).startswith(PENDING_ROW_PREFIX) else [])
            else:
                acts = self._verb_view.row_actions(pid, r, cred, today, auth=auth)
            out.append({**r, "actions": acts})
        return out

    def read_one(self, panel_id: str, row_id: str, *, authority: RequestAuthority | None = None) -> Result:
        """Read ONE row from the SOURCE through the provider's ``read_one``, never from a refresher
        snapshot (design §4.4). Returns ``Read`` with the finished row dict in ``Read.value``,
        ``Absent`` when the row is gone, or ``Fault``. The row's provenance is ``authority``'s, or the
        one the caller's request bound (``authority.bound``): the caller's one snapshot, never a
        second journal read. With neither, this read is its own outer boundary and takes one."""
        spec = self._specs.get(panel_id)
        composed = spec is not None and spec is self._dec.spec
        if composed:
            # the decisions panel routes by row id to the contributor that owns it (§0.4, §8d row 4)
            if row_id.startswith(KERNEL_DECISION_PREFIXES):
                spec = self._broker_dec_spec
                reader = (self._consent_decision_one if row_id.startswith(CONSENT_ROW_PREFIX)
                          else self._question_decision_one if row_id.startswith(QUESTION_ROW_PREFIX)
                          else self._note_decision_one if row_id.startswith(NOTE_ROW_PREFIX)
                          else self._broker_decision_one)
            else:
                src = self._dec.source
                spec, reader = src, (src.read_one if src is not None else None)
        else:
            reader = spec.read_one if spec is not None else None
        if spec is None or reader is None:
            return Fault(f"panel {panel_id!r} offers no read_one")
        auth = authority or current() or self.authority()
        ctx = ReadContext(auth.now, authority=auth)

        def once(ctx: ReadContext) -> Result:
            res = reader(ctx, row_id)
            if not isinstance(res, Read):
                return res
            try:
                if len(res.rows or ()) != 1:
                    return Fault(f"read_one returned {len(res.rows or ())} rows")
                rows, refused = self._rows(spec, list(res.rows), ctx)   # type: ignore[arg-type]
                if refused or len(rows) != 1:
                    return Fault("read_one row refused: a facet value of the wrong type")
                if composed:
                    rows = self._regroup(rows, ctx.today)
                keys = self._label_keys(spec, list(res.rows), {rows[0]["id"]})   # type: ignore[arg-type]
                return Read(value=self._with_provenance(rows, keys, auth.labels)[0])
            except Exception as exc:  # noqa: BLE001
                return Fault(f"provider output refused: {_safe_str(exc)}")
        return self._bounded_fresh(("panel", panel_id), spec.timeout_s, once, ctx)

    def read_value_version(self, panel_id: str) -> Result:
        """Read a line or prose panel's value from the SOURCE (never the refresher snapshot) and
        return its ``value_version`` in ``Read.value`` (design §4.4)."""
        res = self.read_value_head(panel_id, authority=RequestAuthority(self._clock()))   # no label wanted
        return Read(value=res.value[0]) if isinstance(res, Read) else res

    def read_value_head(self, panel_id: str, *, authority: RequestAuthority | None = None) -> Result:
        """``read_value_version``'s read, returning ``(value_version, label)`` in ``Read.value``: the
        value's label from ``authority``'s snapshot, or the request's bound one (K2b-3b §0.1)."""
        spec = self._specs.get(panel_id)
        if spec is None or spec.kind not in VERSIONED_VALUE_KINDS:
            return Fault(f"panel {panel_id!r} has no stored value")
        auth = authority or current() or self.authority()
        ctx = ReadContext(auth.now, authority=auth)

        def once(ctx: ReadContext) -> Result:
            res = spec.read(ctx)
            if not isinstance(res, Read):
                return res
            try:
                value = self._value(spec, res.value, res.version_of)
                return Read(value=(value["value_version"], auth.labels.label(self._value_label_key(spec, value))))
            except Exception as exc:  # noqa: BLE001
                return Fault(f"provider output refused: {_safe_str(exc)}")
        return self._bounded_fresh(("panel", panel_id), spec.timeout_s, once, ctx)

    def _bounded_fresh(self, source: tuple[str, Any], timeout_s: float, fn: Callable[..., Any], *args: Any,
                       pool: str = "writes", allow_snap: bool = False) -> Any:
        """A source read made inside a write. It never joins a flight another request started: a
        flight that began before this write's own read could hand it a version older than the source.
        The bound is on reads still running (a hung source's threads), not on requests: at most
        ``WRITE_READ_CAP_PER_SOURCE`` per source (a panel, a discoverer), so one hung source refuses
        its own writes and leaves the rest of the cap to the others, and ``WRITE_READ_CAP`` in all. ``pool="replay"``
        is the past read's own pool (``REPLAY_READ_CAP`` in all and per source): it never takes a write's slot."""
        replay = pool == "replay"
        per_cap, total_cap = (REPLAY_READ_CAP, REPLAY_READ_CAP) if replay else (WRITE_READ_CAP_PER_SOURCE, WRITE_READ_CAP)
        st = _State()
        entry = [st, False, source]     # state, returned, (kind, id): a namespaced key, so a panel
        with self._lock:                # id never shares a discoverer's count
            # live: a read whose call has not returned (reserved from here to its finally, so no
            # scheduler stall can make it uncounted), or one that returned while its worker still
            # runs (a hung source); a returned read whose worker ended is dropped at once
            live = [e for e in (self._replay_live if replay else self._one_live) if not e[1] or e[0].pflight is not None]
            if replay:
                self._replay_live = live
            else:
                self._one_live = live
            if sum(1 for e in live if e[2] == source) >= per_cap:
                return Fault(f"{per_cap} reads of {source[0]} {source[1]!r} are still "
                             "running (a hung source?); refused")
            if len(live) >= total_cap:
                return Fault(f"{total_cap} source reads are still running (a hung source?); refused")
            live.append(entry)
        try:
            return self._bounded(timeout_s, st, fn, *args, allow_snap=allow_snap)
        finally:
            entry[1] = True


def _band(facets: dict[str, Any], group: str | None, o: Any, today: date) -> str:
    """A row's band under ordering ``o``: an urgent group is ``now``; else a due date today or past is
    ``now`` (when the ordering names no urgent groups), one within ``SOON_DAYS`` is ``soon``."""
    due = parse_date(facets.get("due"))
    if group is not None and group in o.urgent:
        return "now"
    if due and (due < today or due == today) and not o.urgent:
        return "now"
    if due and 0 < (due - today).days <= SOON_DAYS:
        return "soon"
    return "later"


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
    v = (row.get("facets") or {}).get(field_name)
    if isinstance(v, list):
        return " ".join(map(str, v))
    return "" if v is None else str(v)


# --- the per-kind data contracts (docs/cockpit-contract.md) -----------------------------------------
# One validator per value kind. Each takes the spec and the provider's value and returns the value to
# store (normalised where the contract says so), or raises ValueError naming the panel and the field.


def _bad(spec: "ProviderSpec", where: str, why: str) -> ValueError:
    return ValueError(f"panel {spec.id!r} ({spec.kind}): {where} {why}")


def _is_num(x: Any) -> bool:
    """A JSON number: NaN and the infinities are not representable on the wire."""
    if isinstance(x, bool):
        return False
    return isinstance(x, int) or (isinstance(x, float) and math.isfinite(x))


def _is_str(x: Any) -> bool:
    return isinstance(x, str)


def _is_iso(x: Any) -> bool:
    if not isinstance(x, str) or not x:
        return False
    try:
        _parse_iso(x)
    except ValueError:
        return False
    return True


def _need_obj(spec: "ProviderSpec", v: Any) -> dict[str, Any]:
    if not isinstance(v, dict):
        raise _bad(spec, "value", "must be an object")
    return v


def _need_list(spec: "ProviderSpec", v: dict[str, Any], key: str, *, of_objects: bool = True) -> list[Any]:
    if key not in v:
        raise _bad(spec, f"value.{key}", "is required")
    items = v[key]
    if not isinstance(items, list):
        raise _bad(spec, f"value.{key}", "must be a list")
    if of_objects:
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                raise _bad(spec, f"value.{key}[{i}]", "must be an object")
    return items


def _v_line(spec: "ProviderSpec", v: Any) -> dict[str, Any]:
    v = _need_obj(spec, v)
    for i, ln in enumerate(_need_list(spec, v, "lines")):
        if not _is_str(ln.get("text")):
            raise _bad(spec, f"value.lines[{i}].text", "must be a string")
        if ln.get("label") is not None and not _is_str(ln["label"]):
            raise _bad(spec, f"value.lines[{i}].label", "must be a string")
        if ln.get("at") is not None and not _is_iso(ln["at"]):
            raise _bad(spec, f"value.lines[{i}].at", "must be an ISO-8601 timestamp")
    return v


def _v_brief(spec: "ProviderSpec", v: Any) -> dict[str, Any]:
    v = _need_obj(spec, v)
    if "markdown" not in v and "lines" not in v:
        raise _bad(spec, "value", "needs 'markdown' (a string) or 'lines' (labelled lines)")
    if "markdown" in v and not _is_str(v["markdown"]):
        raise _bad(spec, "value.markdown", "must be a string")
    if v.get("headline") is not None and not _is_str(v["headline"]):
        raise _bad(spec, "value.headline", "must be a string")
    if "lines" in v:
        for i, ln in enumerate(_need_list(spec, v, "lines")):
            if not _is_str(ln.get("text")):
                raise _bad(spec, f"value.lines[{i}].text", "must be a string")
            if ln.get("label") is not None and not _is_str(ln["label"]):
                raise _bad(spec, f"value.lines[{i}].label", "must be a string")
    for k in ("title", "source"):
        if v.get(k) is not None and not _is_str(v[k]):
            raise _bad(spec, f"value.{k}", "must be a string")
    if v.get("threads") is not None:
        for i, t in enumerate(_need_list(spec, v, "threads")):
            if not (isinstance(t, dict) and _is_str(t.get("text"))):
                raise _bad(spec, f"value.threads[{i}].text", "must be a string")
    ov = v.get("overnight")
    if ov is not None and not (isinstance(ov, dict) and isinstance(ov.get("episodes"), int) and not isinstance(ov.get("episodes"), bool)
                               and _is_iso(ov.get("since"))):
        raise _bad(spec, "value.overnight", "must be {episodes: int, since: ISO-8601}")
    return v


def _v_reading(spec: "ProviderSpec", v: Any) -> dict[str, Any]:
    v = _need_obj(spec, v)
    val = v.get("value")
    if not (_is_num(val) or (_is_str(val) and val)):
        raise _bad(spec, "value.value", "must be a number or a non-empty string")
    for k in ("unit", "condition"):
        if not _is_str(v.get(k)):
            raise _bad(spec, f"value.{k}", "must be a string (empty when there is none)")
    if not _is_iso(v.get("as_of")):
        raise _bad(spec, "value.as_of", "must be an ISO-8601 timestamp")
    for k in ("hi", "lo"):
        if v.get(k) is not None and not _is_num(v[k]):
            raise _bad(spec, f"value.{k}", "must be a number")
    if v.get("sub") is not None:
        for i, sr in enumerate(_need_list(spec, v, "sub")):
            if not _is_str(sr.get("label")) or not sr["label"]:
                raise _bad(spec, f"value.sub[{i}].label", "must be a non-empty string")
            if not (_is_num(sr.get("value")) or _is_str(sr.get("value"))):
                raise _bad(spec, f"value.sub[{i}].value", "must be a number or a string")
            if sr.get("band") is not None and not _is_str(sr["band"]):
                raise _bad(spec, f"value.sub[{i}].band", "must be a string")
            if sr.get("detail") is not None and not _is_str(sr["detail"]):
                raise _bad(spec, f"value.sub[{i}].detail", "must be a string")
    if v.get("symbol") is not None and v["symbol"] not in READING_SYMBOLS:
        raise _bad(spec, "value.symbol", f"must be one of {', '.join(READING_SYMBOLS)}, got {v['symbol']!r}")
    return v


def _v_status_dots(spec: "ProviderSpec", v: Any) -> dict[str, Any]:
    v = _need_obj(spec, v)
    for i, m in enumerate(_need_list(spec, v, "metrics")):
        if not _is_str(m.get("label")) or not m["label"]:
            raise _bad(spec, f"value.metrics[{i}].label", "must be a non-empty string")
        if m.get("status", "unknown") not in ("ok", "warn", "bad", "unknown"):
            raise _bad(spec, f"value.metrics[{i}].status", f"must be ok|warn|bad|unknown, got {m.get('status')!r}")
        if m.get("value") is not None and not (_is_num(m["value"]) or _is_str(m["value"])):
            raise _bad(spec, f"value.metrics[{i}].value", "must be a finite number or a string")
        for k in ("unit", "read", "detail"):
            if m.get(k) is not None and not _is_str(m[k]):
                raise _bad(spec, f"value.metrics[{i}].{k}", "must be a string")
    if v.get("alerts") is not None:
        for i, a in enumerate(_need_list(spec, v, "alerts")):
            if not _is_str(a.get("message")):
                raise _bad(spec, f"value.alerts[{i}].message", "must be a string")
    return v


def _v_metric_grid(spec: "ProviderSpec", v: Any) -> dict[str, Any]:
    v = _need_obj(spec, v)
    for i, n in enumerate(_need_list(spec, v, "numbers")):
        if not _is_str(n.get("label")) or not n["label"]:
            raise _bad(spec, f"value.numbers[{i}].label", "must be a non-empty string")
        if not _is_num(n.get("value")):
            raise _bad(spec, f"value.numbers[{i}].value", "must be a number")
        if n.get("unit") is not None and not _is_str(n["unit"]):
            raise _bad(spec, f"value.numbers[{i}].unit", "must be a string")
    return v


EVENT_OUTCOMES = ("ok", "failed")
# an ambient event's outcome: flow's two, then the Levain-native activity feed's (K1_DESIGN §1): a session live, a
# decision's outcome, a question's state. A roster's last_call keeps the two.
AMBIENT_OUTCOMES = EVENT_OUTCOMES + ("live", "allowed", "denied", "waiting", "open", "answered")


def _v_ambient(spec: "ProviderSpec", v: Any) -> dict[str, Any]:
    v = _need_obj(spec, v)
    if spec.legacy_kind == "visual":
        # a levain.cockpit/1 `visual`: a named picture plus its text projection
        if v.get("visual") not in VISUALS:
            raise _bad(spec, "value.visual", f"unknown visual {v.get('visual')!r}")
        if not isinstance(v.get("text"), list):
            raise _bad(spec, "value.text", "a visual always ships a text projection")
        return v
    # the typed event stream; extra keys on an event (the embedder's `_ts`) are allowed and ignored
    for i, e in enumerate(_need_list(spec, v, "events")):
        for k in ("kind", "at", "repo", "agent", "outcome"):
            if k not in e:
                raise _bad(spec, f"value.events[{i}].{k}", "is required (repo and agent may be null)")
        if not _is_str(e["kind"]) or not e["kind"]:
            raise _bad(spec, f"value.events[{i}].kind", "must be a non-empty string")
        if not _is_iso(e["at"]):
            raise _bad(spec, f"value.events[{i}].at", "must be an ISO-8601 timestamp")
        for k in ("repo", "agent"):
            if e[k] is not None and not _is_str(e[k]):
                raise _bad(spec, f"value.events[{i}].{k}", "must be a string or null")
        if e["outcome"] not in AMBIENT_OUTCOMES:
            raise _bad(spec, f"value.events[{i}].outcome", f"must be one of {list(AMBIENT_OUTCOMES)}, got {e['outcome']!r}")
    if not _is_str(v.get("legend")) or not v["legend"].strip():
        raise _bad(spec, "value.legend", "must be a non-empty string (one line saying what the encoding means)")
    if "dialect" in v and v["dialect"] not in ("levain", "flow"):
        raise _bad(spec, "value.dialect", f"is optional and must be 'levain' or 'flow' when present, got {v['dialect']!r}")
    return v


ROSTER_MEMORY = ("stateful", "stateless")
ROSTER_CADENCE = ("resident", "on_call")
ROSTER_LIFECYCLE = ("active", "retired")


def _v_roster(spec: "ProviderSpec", v: Any) -> dict[str, Any]:
    v = _need_obj(spec, v)
    rows = _need_list(spec, v, "rows")
    seen: set[str] = set()
    for i, r in enumerate(rows):
        w = f"value.rows[{i}]"
        if not _is_str(r.get("name")) or not r["name"]:
            raise _bad(spec, f"{w}.name", "must be a non-empty string")
        if r["name"] in seen:
            raise _bad(spec, f"{w}.name", f"{r['name']!r} is repeated")
        seen.add(r["name"])
        if r.get("memory") not in ROSTER_MEMORY:
            raise _bad(spec, f"{w}.memory", f"must be one of {list(ROSTER_MEMORY)}, got {r.get('memory')!r}")
        if r.get("cadence") not in ROSTER_CADENCE:
            raise _bad(spec, f"{w}.cadence", f"must be one of {list(ROSTER_CADENCE)}, got {r.get('cadence')!r}")
        if r.get("lifecycle") not in ROSTER_LIFECYCLE:
            raise _bad(spec, f"{w}.lifecycle", f"must be one of {list(ROSTER_LIFECYCLE)}, got {r.get('lifecycle')!r}")
        for k in ("role", "group", "wrap_every"):
            if r.get(k) is not None and not _is_str(r[k]):
                raise _bad(spec, f"{w}.{k}", "must be a string or null")
        if r.get("silence_is_signal") is not None and not isinstance(r["silence_is_signal"], bool):
            raise _bad(spec, f"{w}.silence_is_signal", "must be a boolean")
        # null is the honest "no data": a digest is never a faked zero
        if r.get("digest") is not None and not isinstance(r["digest"], dict):
            raise _bad(spec, f"{w}.digest", "must be an object or null (null means no data)")
        if isinstance(r.get("digest"), dict) and r["digest"].get("last_wrap_at") is not None and not _is_iso(r["digest"]["last_wrap_at"]):
            raise _bad(spec, f"{w}.digest.last_wrap_at", "must be null or an ISO-8601 timestamp")
        if r.get("wrap_every") is not None and _wrap_every_s(r["wrap_every"]) is None:
            raise _bad(spec, f"{w}.wrap_every", f"must be null or a positive count and one unit of m, h, d, w (like '1d'), got {r['wrap_every']!r}")
        if r.get("group") == "roundtable" and r["cadence"] != "on_call":
            raise _bad(spec, f"{w}.group", "roundtable members must be cadence on_call: a resident's silence is a signal the folded roundtable line cannot carry")
        lc = r.get("last_call")
        if lc is not None and not (isinstance(lc, dict) and _is_iso(lc.get("at")) and lc.get("outcome") in EVENT_OUTCOMES):
            raise _bad(spec, f"{w}.last_call", f"must be null or {{at (ISO-8601), outcome (one of {list(EVENT_OUTCOMES)})}}")
    shown = []
    for r in rows:
        if r["lifecycle"] == "retired":
            continue                                            # retired members do not render
        r = {**r, "silence_is_signal": (r["silence_is_signal"] if r.get("silence_is_signal") is not None
                                        else r["cadence"] == "resident")}
        if isinstance(r.get("digest"), dict):
            # a non-finite number is not representable on the wire: it reads as "not reported"
            r["digest"] = {k: (None if isinstance(x, float) and not math.isfinite(x) else x) for k, x in r["digest"].items()}
        r["fleet_group"] = fleet_group(r)
        shown.append(r)
    return {**v, "rows": shown, "retired_hidden": len(rows) - len(shown)}


_WRAP_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def _wrap_every_s(text: Any) -> float | None:
    """'1d' / '12h' / '2w' in seconds; anything else is no schedule (never a guess)."""
    if not isinstance(text, str) or len(text) < 2 or text[-1] not in _WRAP_UNITS or not text[:-1].isascii() or not text[:-1].isdigit():
        return None
    n = int(text[:-1])
    return n * _WRAP_UNITS[text[-1]] if n > 0 else None


ROSTER_VISIBLE = 40


def roster_visible(spec: "ProviderSpec") -> int:
    """A roster is a bounded set drawn in groups (the roundtable folds to one line), so it is not cut at the
    list default of 10 unless its contributor declared a window of its own."""
    d = spec.search_default_visible
    return ROSTER_VISIBLE if spec.kind == "roster" and d == 10 else d


def fleet_group(row: dict[str, Any]) -> str:
    """Where a roster row is drawn: the roundtable's seats fold into one group, whatever their cadence."""
    if row.get("group") == "roundtable":
        return "roundtable"
    return "resident" if row.get("cadence") == "resident" else "on_call"


def _kb(n: float) -> str:
    return f"{n / 1e6:.1f} MB" if n > 1e6 else f"{round(n / 1e3)} kB"


def roster_health(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Health PER KIND, the one place the fleet rules live. ``tone`` is ok | alarm | neutral | unknown;
    only a RESIDENT member whose silence is a signal can be ``alarm``. An on-call member (and a member the
    contributor says silence means nothing for) is described by its last call as a neutral fact, its
    memory as a plain fact; wrap age is an alarm only against ``wrap_every``, only for resident + stateful.
    The wrap age is read at validation time, so it is as fresh as the snapshot it rides."""
    dg = row.get("digest") if isinstance(row.get("digest"), dict) else None
    lc = row.get("last_call") if isinstance(row.get("last_call"), dict) else None
    stateful = row.get("memory") == "stateful"
    if stateful:
        sb = dg.get("store_bytes") if dg else None
        mem = "memory" + (f" {_kb(sb)}" if isinstance(sb, (int, float)) and not isinstance(sb, bool) and sb else "")
    else:
        mem = "stateless"
    out: dict[str, Any] = {"tone": "neutral", "text": "", "at": None, "memory": mem}
    if row.get("cadence") == "on_call" or not row.get("silence_is_signal"):
        if lc:
            out["text"] = "last call failed" if lc.get("outcome") == "failed" else "answered"
            out["at"] = lc.get("at")
        elif row.get("cadence") == "on_call":
            out["text"] = "not called yet"
        else:
            out["text"] = "no signal expected"
        return out                                              # never an alarm: a neutral fact
    if dg is None:
        return {**out, "tone": "unknown", "text": "no data"}
    if dg.get("available") is False:
        return {**out, "tone": "alarm", "text": "write path unavailable"}
    if dg.get("available") is not True:
        return {**out, "tone": "unknown", "text": "no liveness data"}
    # tone ranks alarm > unknown > ok; evidence that is missing or unreadable is never read as health
    rank = {"ok": 0, "unknown": 1, "alarm": 2}
    tone = "ok"
    parts: list[str] = []

    def note(t: str, text: str) -> None:
        nonlocal tone
        if rank[t] > rank[tone]:
            tone = t
        parts.append(text)

    if not stateful:
        parts.append("running")
    else:
        eps = dg.get("episodes_24h")
        if eps is None:
            note("unknown", "episodes_24h not reported")
        elif isinstance(eps, bool) or not isinstance(eps, (int, float)) or not math.isfinite(eps) or eps < 0 or eps != int(eps):
            note("unknown", "episodes_24h unreadable")
        elif eps == 0:
            note("alarm", "no episodes in 24 h, it should be writing")
        else:
            parts.append(f"{int(eps)} episodes in 24 h")
        every = _wrap_every_s(row.get("wrap_every"))
        if every is not None:
            lw = dg.get("last_wrap_at")
            if not _is_iso(lw):
                note("unknown", f"no wrap time reported (wraps every {row['wrap_every']})")
            elif (now - _parse_iso(lw)).total_seconds() > every:
                note("alarm", f"last wrap overdue (due every {row['wrap_every']})")
            else:
                parts.append(f"wraps every {row['wrap_every']}")
    out["tone"], out["text"] = tone, " · ".join(parts)
    return out


def _with_health(value: dict[str, Any], now: datetime) -> dict[str, Any]:
    """A roster value with each row's health read at ``now`` (the request's clock): health is derived at
    render, never stored in a snapshot, so a cached read cannot keep a stale tone."""
    rows = []
    for r in value.get("rows") or []:
        try:
            h = roster_health(r, now)
        except Exception:  # noqa: BLE001 - a row that cannot be judged is unknown, never a crashed GET
            h = {"tone": "unknown", "text": "health unreadable", "at": None, "memory": ""}
        rows.append({**r, "health": h})
    return {**value, "rows": rows}


def _v_form(spec: "ProviderSpec", v: Any) -> dict[str, Any]:
    v = _need_obj(spec, v)
    fields = _need_list(spec, v, "fields")
    if not fields:
        raise _bad(spec, "value.fields", "must hold at least one field")
    names: set[str] = set()
    for i, f in enumerate(fields):
        w = f"value.fields[{i}]"
        if not _is_str(f.get("name")) or not f["name"]:
            raise _bad(spec, f"{w}.name", "must be a non-empty string")
        if f["name"] in names:
            raise _bad(spec, f"{w}.name", f"{f['name']!r} is repeated")
        names.add(f["name"])
        if f.get("type") not in FORM_FIELD_TYPES:
            raise _bad(spec, f"{w}.type", f"must be one of {list(FORM_FIELD_TYPES)}, got {f.get('type')!r}")
        if f.get("label") is not None and not _is_str(f["label"]):
            raise _bad(spec, f"{w}.label", "must be a string")
        if f["type"] == "recipient":
            en = f.get("enum")
            if not (isinstance(en, list) and en and all(_is_str(x) and x for x in en)):
                raise _bad(spec, f"{w}.enum", "a recipient field carries a non-empty list of ids from the provider's registry")
            # the page trims a chosen id and drops a blank one, so a blank id would send no reader (everyone),
            # and two ids equal after trimming are one reader drawn twice
            seen_ids: set[str] = set()
            for j, x in enumerate(en):
                t = x.strip()
                if not t:
                    raise _bad(spec, f"{w}.enum[{j}]", "is blank (a chosen blank id would send to everyone)")
                if t in seen_ids:
                    raise _bad(spec, f"{w}.enum[{j}]", f"{t!r} is repeated (after trimming)")
                seen_ids.add(t)
            # the provider, not the page, says which ids are pressed at the start and which can leave its roster
            # (and the words its refusal uses when one has): Levain assigns no meaning to any of them
            if "default" in f:
                dflt = f["default"]
                if not (isinstance(dflt, list) and all(_is_str(x) for x in dflt)):
                    raise _bad(spec, f"{w}.default", "must be a list of ids from the enum")
                # the page presses a default id only when it equals an enum id exactly (it compares the raw strings), so
                # the check is exact too: a padded spelling would register here and press nothing there
                picked: set[str] = set()
                for j, x in enumerate(dflt):
                    if x not in en:
                        raise _bad(spec, f"{w}.default[{j}]", f"{x!r} is not an enum id (exactly as spelled there)")
                    if x in picked:
                        raise _bad(spec, f"{w}.default[{j}]", f"{x!r} is repeated")
                    picked.add(x)
            for key in ("transient_prefix", "gone_match"):
                if key in f and not (_is_str(f[key]) and f[key].strip()):
                    raise _bad(spec, f"{w}.{key}", "must be a string with something other than whitespace in it")
            if "transient_prefix" in f and f["transient_prefix"] != f["transient_prefix"].strip():
                # the page compares the prefix raw against the enum ids: a padded one would match none of them
                raise _bad(spec, f"{w}.transient_prefix", "carries leading or trailing whitespace")
            if "gone_match" in f and "transient_prefix" not in f:
                # the recovery clears only ids under the prefix, so a match with no prefix could never act
                raise _bad(spec, f"{w}.gone_match", "needs a transient_prefix beside it")
        else:
            for key in ("enum", "default", "transient_prefix", "gone_match"):
                if key in f:
                    raise _bad(spec, f"{w}.{key}", "belongs to a recipient field only")
    if v.get("verb") is not None and not _is_str(v["verb"]):
        raise _bad(spec, "value.verb", "must be a string")
    return v


def _v_log(spec: "ProviderSpec", v: Any) -> dict[str, Any]:
    v = _need_obj(spec, v)
    for i, r in enumerate(_need_list(spec, v, "rows")):
        for k in ("verb", "target", "actor"):
            if not _is_str(r.get(k)) or not r[k]:
                raise _bad(spec, f"value.rows[{i}].{k}", "must be a non-empty string")
        if not _is_iso(r.get("at")):
            raise _bad(spec, f"value.rows[{i}].at", "must be an ISO-8601 timestamp")
    return v


TOPOLOGY_NODE_STATES = ("waiting", "errored", "stalled", "running", "idle")
TOPOLOGY_TASK_KINDS = ("dispatch", "spore", "consent", "question", "unattached")
TOPOLOGY_TASK_STATES = ("waiting", "stuck", "running", "open", "done", "failed")
TOPOLOGY_TASK_HEATS = ("hot", "warm", "cold", "parked")
# "transcript": the tokens a task's workers spent, read from their session transcripts. Tokens only: a transcript carries no price.
TOPOLOGY_COST_SOURCES = ("dispatch", "ledger", "transcript")
TOPOLOGY_LABEL_MAX = 48
TOPOLOGY_NEEDS_MAX = 120      # the decision mark's strings (needs_first, needs_first_card, needs_first_what): the page's own cap (topology_view.js NEED_CAP)


def _v_topology(spec: "ProviderSpec", v: Any) -> dict[str, Any]:
    """F12's live topology (F12_SPEC, with the 14a amendment): a hub, groups, nodes, tasks and what closing a live
    node would lose. Every string is display-safe (``visible`` leaves it unchanged) and every label at most
    ``TOPOLOGY_LABEL_MAX`` characters; ids are unique and every reference names one; null is "unknown" wherever the
    contract allows it, and is never read as zero. ``conflicts`` may be null (the source is not tracked) and the
    counts in ``lost_if_closed`` may be null (not read). Extra keys are accepted and ignored."""
    v = _need_obj(spec, v)

    def text(where: str, x: Any, *, null: bool = False, label: bool = False, blank: bool = True) -> None:
        if x is None and null:
            return
        if not _is_str(x):
            raise _bad(spec, where, "must be a string" + (" or null" if null else ""))
        if not blank and not x:
            raise _bad(spec, where, "must be a non-empty string")
        if visible(x) != x:
            raise _bad(spec, where, "carries an invisible or format character (send it through display_safe.visible)")
        if label and len(x) > TOPOLOGY_LABEL_MAX:
            raise _bad(spec, where, f"is longer than {TOPOLOGY_LABEL_MAX} characters")

    def count(where: str, x: Any, *, null: bool = True) -> None:
        if x is None and null:
            return
        if not (isinstance(x, int) and not isinstance(x, bool) and x >= 0):
            raise _bad(spec, where, "must be a non-negative integer" + (" or null (unknown)" if null else ""))

    def strings(where: str, x: Any, *, names: bool = False, label: bool = False) -> list[str]:
        if not isinstance(x, list):
            raise _bad(spec, where, "must be a list of strings")
        for i, s_ in enumerate(x):
            text(f"{where}[{i}]", s_, label=names or label, blank=False)
            if names and ("/" in s_ or "\\" in s_):
                raise _bad(spec, f"{where}[{i}]", "must be a basename (no path on the wire)")
        return x

    def unique(where: str, items: list[Any]) -> set[str]:
        seen: set[str] = set()
        for i, it in enumerate(items):
            text(f"{where}[{i}].id", it.get("id"), blank=False)
            if it["id"] in seen:
                raise _bad(spec, f"{where}[{i}].id", f"{it['id']!r} is repeated")
            seen.add(it["id"])
            text(f"{where}[{i}].label", it.get("label"), label=True)
        return seen

    hub = v.get("hub")
    if not isinstance(hub, dict):
        raise _bad(spec, "value.hub", "must be an object {id, label}")
    text("value.hub.id", hub.get("id"), blank=False)
    text("value.hub.label", hub.get("label"), label=True)

    def needs(where: str, o: dict[str, Any]) -> None:
        """The decision mark's optional keys (all may be absent or null): a count, the first need's id and card id and first sentence, each a
        display-safe string; the card id also stays within the page's cap (``TOPOLOGY_NEEDS_MAX``, code points)."""
        count(f"{where}.needs_open", o.get("needs_open"))
        for k in ("needs_first", "needs_first_card", "needs_first_what"):
            x = o.get(k)
            text(f"{where}.{k}", x, null=True)
            # only the card id has a hard cap (the page never cuts or rewrites it); the two text strings are clipped by the page, so an
            # over-long one must never fail the whole topology value
            if k == "needs_first_card" and x is not None and len(x) > TOPOLOGY_NEEDS_MAX:
                raise _bad(spec, f"{where}.{k}", f"is longer than {TOPOLOGY_NEEDS_MAX} characters")

    needs("value.hub", hub)
    groups = _need_list(spec, v, "groups")
    group_ids = unique("value.groups", groups)
    for i, g in enumerate(groups):
        text(f"value.groups[{i}].repo", g.get("repo"), null=True)
    nodes = _need_list(spec, v, "nodes")
    node_ids = unique("value.nodes", nodes)
    for i, n in enumerate(nodes):
        w = f"value.nodes[{i}]"
        if n.get("group") not in group_ids:
            raise _bad(spec, f"{w}.group", f"must name a group id, got {n.get('group')!r}")
        par = n.get("parent")
        if par is not None and (par not in node_ids or par == n["id"]):
            raise _bad(spec, f"{w}.parent", f"must be null or another node's id, got {par!r}")
        for k in ("agent", "model", "tool", "card"):
            text(f"{w}.{k}", n.get(k), null=True, label=k in ("agent", "model", "tool"))
        if n.get("state") not in TOPOLOGY_NODE_STATES:
            raise _bad(spec, f"{w}.state", f"must be one of {list(TOPOLOGY_NODE_STATES)}, got {n.get('state')!r}")
        count(f"{w}.tokens", n.get("tokens"))
        needs(w, n)
        for k in ("started", "last_event"):
            if not _is_iso(n.get(k)):
                raise _bad(spec, f"{w}.{k}", "must be an ISO-8601 timestamp")
        if n.get("rate") is not None and (not _is_num(n["rate"]) or n["rate"] < 0):
            raise _bad(spec, f"{w}.rate", "must be a non-negative finite number (events per minute) or null (no log)")
    parent_of = {n["id"]: n.get("parent") for n in nodes}
    for i, n in enumerate(nodes):
        # a parent chain that returns to itself has no root: the renderer would have nowhere to start
        seen_p, cur = {n["id"]}, n.get("parent")
        while cur is not None:
            if cur in seen_p:
                raise _bad(spec, f"value.nodes[{i}].parent", f"is part of a parent cycle through {cur!r} (a node's ancestors never include itself)")
            seen_p.add(cur)
            cur = parent_of.get(cur)
    if "conflicts" not in v:
        raise _bad(spec, "value.conflicts", "is required (null when the source is not tracked)")
    if v["conflicts"] is not None:
        for i, c in enumerate(_need_list(spec, v, "conflicts")):
            text(f"value.conflicts[{i}].path", c.get("path"), label=True, blank=False)
            if "/" in c["path"] or "\\" in c["path"]:
                raise _bad(spec, f"value.conflicts[{i}].path", "must be a basename (no path on the wire)")
            ids = c.get("nodes")
            if not (isinstance(ids, list) and len(ids) >= 2 and all(isinstance(x, str) and x in node_ids for x in ids)
                    and len(set(ids)) == len(ids)):
                raise _bad(spec, f"value.conflicts[{i}].nodes", "must be two or more distinct node ids")
        total = v.get("conflicts_total")
        if total is not None and (isinstance(total, bool) or not isinstance(total, int) or total < len(v["conflicts"])):
            raise _bad(spec, "value.conflicts_total", "must be an integer no smaller than the conflicts listed (the count before the cap), or null")
    counts = v.get("counts")
    if not isinstance(counts, dict):
        raise _bad(spec, "value.counts", "must be an object {tokens_today}")
    if "tokens_today" not in counts:
        raise _bad(spec, "value.counts.tokens_today", "is required (null when not tracked)")
    count("value.counts.tokens_today", counts["tokens_today"])
    if not _is_iso(v.get("as_of")):
        raise _bad(spec, "value.as_of", "must be an ISO-8601 timestamp")
    tasks = _need_list(spec, v, "tasks")
    unique("value.tasks", tasks)
    for i, t in enumerate(tasks):
        w = f"value.tasks[{i}]"
        if t.get("kind") not in TOPOLOGY_TASK_KINDS:
            raise _bad(spec, f"{w}.kind", f"must be one of {list(TOPOLOGY_TASK_KINDS)}, got {t.get('kind')!r}")
        if t.get("state") not in TOPOLOGY_TASK_STATES:
            raise _bad(spec, f"{w}.state", f"must be one of {list(TOPOLOGY_TASK_STATES)}, got {t.get('state')!r}")
        strings(f"{w}.waits_on", t.get("waits_on"))
        text(f"{w}.card", t.get("card"), null=True)
        # an open loop's age and heat, both optional (a provider with no such thing omits them): present means ISO / a tier name, or null
        if t.get("opened") is not None and not _is_iso(t["opened"]):
            raise _bad(spec, f"{w}.opened", "must be an ISO-8601 timestamp or null (when the task was opened)")
        age = t.get("age_days")
        if age is not None and (isinstance(age, bool) or not isinstance(age, int) or age < 0):
            raise _bad(spec, f"{w}.age_days", "must be a non-negative integer or null (calendar days open, in the provider's day)")
        if t.get("heat") is not None and t["heat"] not in TOPOLOGY_TASK_HEATS:
            raise _bad(spec, f"{w}.heat", f"must be one of {list(TOPOLOGY_TASK_HEATS)} or null, got {t.get('heat')!r}")
        outs = t.get("outcomes")
        if not isinstance(outs, list):
            raise _bad(spec, f"{w}.outcomes", "must be a list")
        for j, o in enumerate(outs):
            if not isinstance(o, dict):
                raise _bad(spec, f"{w}.outcomes[{j}]", "must be an object {label, at, ok}")
            text(f"{w}.outcomes[{j}].label", o.get("label"), label=True)
            if not _is_iso(o.get("at")):
                raise _bad(spec, f"{w}.outcomes[{j}].at", "must be an ISO-8601 timestamp")
            if not isinstance(o.get("ok"), bool):
                raise _bad(spec, f"{w}.outcomes[{j}].ok", "must be a boolean")
        cost = t.get("cost")
        if not isinstance(cost, dict) or not {"usd", "tokens", "source"} <= set(cost):
            raise _bad(spec, f"{w}.cost", "must be an object {usd, tokens, source} (null fields = not tracked)")
        if cost["usd"] is not None and not (_is_num(cost["usd"]) and cost["usd"] >= 0):
            raise _bad(spec, f"{w}.cost.usd", "must be a non-negative finite number or null")
        count(f"{w}.cost.tokens", cost["tokens"])
        if cost["source"] is not None and cost["source"] not in TOPOLOGY_COST_SOURCES:
            raise _bad(spec, f"{w}.cost.source", f"must be one of {list(TOPOLOGY_COST_SOURCES)} or null")
        if cost["source"] is None and (cost["usd"] is not None or cost["tokens"] is not None):
            raise _bad(spec, f"{w}.cost.source", "must name where a cost came from (a cost is never an estimate)")
        if cost["source"] == "transcript" and (cost["usd"] is not None or cost["tokens"] is None):
            raise _bad(spec, f"{w}.cost.source", "a transcript source carries tokens and no usd (a transcript has no price)")
        workers = strings(f"{w}.workers", t.get("workers"))
        for j, x in enumerate(workers):
            if x not in node_ids:
                raise _bad(spec, f"{w}.workers[{j}]", f"must name a node id, got {x!r}")
    for i, lost in enumerate(_need_list(spec, v, "lost_if_closed")):
        w = f"value.lost_if_closed[{i}]"
        if lost.get("node") not in node_ids:
            raise _bad(spec, f"{w}.node", f"must name a node id, got {lost.get('node')!r}")
        for k in ("dirty", "unpushed", "uncaptured_episodes", "running_children"):
            if k not in lost:
                raise _bad(spec, f"{w}.{k}", "is required (null when not read)")
            count(f"{w}.{k}", lost[k])
        strings(f"{w}.dirty_names", lost.get("dirty_names"), names=True)
        strings(f"{w}.branches", lost.get("branches"), label=True)
    for k in ("errors", "unreadable"):
        if v.get(k) is not None:
            strings(f"value.{k}", v[k])
    return v


# The board kind (C12): the ready queue as six derived columns (levain.coordinator.ready.CATEGORIES, in column order; a test
# pins the two equal, so this module need not import the coordinator). BOARD_CARDS_MAX bounds the whole value: a board is drawn
# whole on every poll, so a provider that sends more than this much is sending a bug, and the refusal names the count.
BOARD_CATEGORIES = ("backlog", "ready", "running", "waiting", "needs-you", "done")
BOARD_QUEUES = ("ok", "none", "unreadable")
BOARD_CARDS_MAX = 2000
# A card's `id`, `title`, `repo` and `done_line` are drawn as text, so each is display-safe (no invisible or format character: a
# bidi override reorders what the operator reads) and at most this long.
BOARD_TEXT_MAX = 1000
BOARD_DEPENDS_MAX = 50       # a card's `depends_on` is drawn as a line of ids: a bounded list of bounded, display-safe strings


def _v_board(spec: "ProviderSpec", v: Any) -> dict[str, Any]:
    """The board value (levain.cockpit.board.build): ``{queue, columns: [{category, cards: [{id, title, journal?, ...}]}], unreadable?,
    note?}``. A SHAPE check only, nothing is parsed or executed: ``columns`` is a list of objects each naming one of
    ``BOARD_CATEGORIES`` (once), holding a list of card objects each with a non-empty string ``id`` and a string ``title``; a
    ``journal`` is null or an object; ``queue`` is one of ``BOARD_QUEUES``; ``unreadable`` (when present) is a list of strings.
    Every other key, on the value, a column or a card, is accepted and left alone."""
    v = _need_obj(spec, v)
    if v.get("queue") not in BOARD_QUEUES:
        raise _bad(spec, "value.queue", f"must be one of {list(BOARD_QUEUES)}, got {v.get('queue')!r}")
    if v.get("note") is not None and not _is_str(v["note"]):
        raise _bad(spec, "value.note", "must be a string or null")
    unreadable = v.get("unreadable")
    if unreadable is not None and not (isinstance(unreadable, list) and all(_is_str(u) for u in unreadable)):
        raise _bad(spec, "value.unreadable", "must be a list of strings")
    seen: set[str] = set()
    total = 0
    for i, col in enumerate(_need_list(spec, v, "columns")):
        w = f"value.columns[{i}]"
        if col.get("category") not in BOARD_CATEGORIES:
            raise _bad(spec, f"{w}.category", f"must be one of {list(BOARD_CATEGORIES)}, got {col.get('category')!r}")
        if col["category"] in seen:
            raise _bad(spec, f"{w}.category", f"{col['category']!r} is repeated")
        seen.add(col["category"])
        cards = col.get("cards")
        if not isinstance(cards, list):
            raise _bad(spec, f"{w}.cards", "must be a list")
        total += len(cards)
        if total > BOARD_CARDS_MAX:
            raise _bad(spec, f"{w}.cards", f"brings the board past {BOARD_CARDS_MAX} cards")
        for j, card in enumerate(cards):
            c = f"{w}.cards[{j}]"
            if not isinstance(card, dict):
                raise _bad(spec, c, "must be an object")
            if not _is_str(card.get("id")) or not card["id"]:
                raise _bad(spec, f"{c}.id", "must be a non-empty string")
            if not _is_str(card.get("title")):
                raise _bad(spec, f"{c}.title", "must be a string")
            for k in ("id", "title", "repo", "done_line"):
                x = card.get(k)
                if x is None and k in ("repo", "done_line"):
                    continue
                if not _is_str(x):
                    raise _bad(spec, f"{c}.{k}", "must be a string")
                if len(x) > BOARD_TEXT_MAX:          # before `visible`: a runaway string is never scanned
                    raise _bad(spec, f"{c}.{k}", f"is longer than {BOARD_TEXT_MAX} characters")
                if visible(x) != x:
                    raise _bad(spec, f"{c}.{k}", "carries an invisible or format character (send it through display_safe.visible)")
            deps = card.get("depends_on")
            if deps is not None:
                if not isinstance(deps, list) or len(deps) > BOARD_DEPENDS_MAX:
                    raise _bad(spec, f"{c}.depends_on", f"must be a list of at most {BOARD_DEPENDS_MAX} ids")
                for d, dep in enumerate(deps):
                    if not _is_str(dep) or not dep or len(dep) > BOARD_TEXT_MAX or visible(dep) != dep:
                        raise _bad(spec, f"{c}.depends_on[{d}]", "must be a non-empty, display-safe string no longer than "
                                   f"{BOARD_TEXT_MAX} characters")
            if card.get("journal") is not None and not isinstance(card["journal"], dict):
                raise _bad(spec, f"{c}.journal", "must be an object or null")
    return v


VALUE_VALIDATORS: dict[str, Callable[["ProviderSpec", Any], dict[str, Any]]] = {
    "reading": _v_reading, "line": _v_line, "brief": _v_brief, "ambient": _v_ambient,
    "status-dots": _v_status_dots, "metric-grid": _v_metric_grid, "roster": _v_roster, "form": _v_form,
    "log": _v_log, "topology": _v_topology, "board": _v_board,
}

# What a row of each row-shaped kind carries, i.e. what ``search.fields`` may name, and the default.
ROW_SEARCH_FIELDS = {
    "roster": ("name", "role", "group", "memory", "cadence", "lifecycle", "wrap_every"),
    "log": ("verb", "target", "actor", "at"),
}
ROW_SEARCH_DEFAULT = {"list": ("title", "body", "id"), "count-queue": ("title", "body", "id"),
                      "roster": ("name", "role", "group"), "log": ("verb", "target", "actor")}


def search_fields_of(spec: "ProviderSpec") -> tuple[str, ...]:
    return spec.search_fields or ROW_SEARCH_DEFAULT.get(spec.kind, ())


def _validate_compose_decl(spec: "ProviderSpec") -> None:
    """Refuse a compose declaration the page could not honour (registration time): a list panel only, a
    verb the panel offers, and fields of the form kind's own contract."""
    if spec.compose is None:
        return
    sid, c = spec.id, spec.compose
    if spec.kind != "list":
        raise CockpitRegistrationError(f"{sid}: only a list carries a compose card")
    if spec.face == "count":
        raise CockpitRegistrationError(f"{sid}: a list drawn as a count shows no compose card")
    if not isinstance(c, dict) or set(c) - {"verb", "fields"}:
        raise CockpitRegistrationError(f"{sid}: compose is {{verb, fields}}")
    if not isinstance(c.get("verb"), str) or not c["verb"] or c["verb"] not in spec.verbs:
        raise CockpitRegistrationError(f"{sid}: compose names a verb the panel offers, got {c.get('verb')!r}")
    try:
        _v_form(spec, {"verb": c["verb"], "fields": c.get("fields")})
    except ValueError as exc:
        raise CockpitRegistrationError(f"{sid}: compose {exc}") from None
    recipients = [f for f in c["fields"] if f["type"] == "recipient"]
    if len(recipients) > 1:
        # the card has one address line and one chip row
        raise CockpitRegistrationError(f"{sid}: compose carries at most one recipient field, got {len(recipients)}")
    if recipients:
        # a row of an addressed list draws `source -> <recipient>` from its facets: both must be declared, so a
        # row with no reader value names no reader (everyone), never a facet the provider forgot to declare
        need = {recipients[0]["name"], "source"}
        if not need <= set(spec.facets):
            raise CockpitRegistrationError(
                f"{sid}: a compose with a recipient field declares the facets {sorted(need)}, "
                f"missing {sorted(need - set(spec.facets))}")


def _check_compose_verbs(spec: "ProviderSpec", view: Any) -> None:
    """Refuse a compose the verb registry cannot honour (``VerbView.compose_refusal``): a no-op until the
    registry is attached, and for a view that does not answer the question."""
    if spec.compose is None or view is None:
        return
    ask = getattr(view, "compose_refusal", None)
    why = ask(spec.id, spec.compose) if callable(ask) else None
    if why:
        raise CockpitRegistrationError(f"{spec.id}: compose {why}")


def _validate_search_decl(spec: "ProviderSpec") -> None:
    """Refuse a search declaration the row contract cannot honour (registration time)."""
    sid = spec.id
    if spec.kind not in ROW_KINDS:
        if spec.search_fields or spec.search or spec.window is not None or spec.search_scope != "rows":
            raise CockpitRegistrationError(f"{sid}: only a row-shaped kind ({', '.join(ROW_KINDS)}) is searchable")
        return
    if spec.search_scope not in SCOPES:
        raise CockpitRegistrationError(f"{sid}: search scope must be one of {list(SCOPES)}, got {spec.search_scope!r}")
    allowed = set(ROW_SEARCH_FIELDS.get(spec.kind) or ("title", "body", "id")) | (
        set(spec.facets) if spec.kind in ROW_ENGINE_KINDS else set())
    for f in spec.search_fields:
        if f not in allowed:
            raise CockpitRegistrationError(
                f"{sid}: search field {f!r} is not carried by a {spec.kind} row (carries {sorted(allowed)})")
    if spec.window is not None and (not isinstance(spec.window, int) or isinstance(spec.window, bool) or spec.window < 1):
        raise CockpitRegistrationError(f"{sid}: window must be a positive integer, got {spec.window!r}")
    if spec.search_scope == "store" and spec.window is None:
        raise CockpitRegistrationError(f"{sid}: scope 'store' is for a windowed panel: declare window=N")
    if spec.search is not None and spec.search_scope != "store":
        raise CockpitRegistrationError(f"{sid}: a search(q, limit) hook belongs to a scope 'store' panel")
    if spec.search is not None and not callable(spec.search):
        raise CockpitRegistrationError(f"{sid}: search must be callable")


@dataclass(frozen=True)
class StoreMatches:
    """What a ``search(q, limit)`` hook may return instead of a bare sequence: the rows and the TOTAL
    the store matched (more than the rows when the limit cut them)."""

    rows: tuple[Any, ...]
    total: int | None = None


class QueryError(ValueError):
    """A search query the kernel refuses (too long, not plain text, bad limit): HTTP 400."""


def check_query(q: str | None, limit: int | None = None) -> tuple[str | None, int]:
    """The kernel's bounds on a search: ``q`` is plain text of at most SEARCH_Q_MAX characters with no
    control characters (and no regex meaning, ever); ``limit`` is clamped to SEARCH_LIMIT_MAX."""
    if q is not None:
        if len(q) > SEARCH_Q_MAX:
            raise QueryError(f"q is at most {SEARCH_Q_MAX} characters, got {len(q)}")
        if any(unicodedata.category(ch) in ("Cc", "Cs") for ch in q):
            raise QueryError("q is plain text: control characters are refused")
        if not _fold(q).split():
            q = None            # no searchable term (blank, or only combining marks): no query, never "match everything"
    if limit is None:
        limit = SEARCH_LIMIT_DEFAULT
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise QueryError("limit must be a positive integer")
    return q, min(limit, SEARCH_LIMIT_MAX)


def check_replay_at(at: Any, now: datetime) -> datetime:
    """The kernel's bounds on a past read (``?at=``, a provider's ``read_at``): an ISO-8601 instant with an explicit
    zone, on a whole minute, inside ``[now - REPLAY_WINDOW_S, now]``. Anything else is refused (``QueryError``, an
    HTTP 400), never clamped to the window: a read of another instant than the one asked would pass as it."""
    if not isinstance(at, str) or not at or len(at) > 40:
        raise QueryError("at is an ISO-8601 instant, e.g. 2026-10-20T11:30:00Z")
    try:
        t = datetime.fromisoformat(at[:-1] + "+00:00" if at.endswith("Z") else at)
    except ValueError:
        raise QueryError(f"at is not an ISO-8601 instant: {at!r}") from None
    if t.tzinfo is None:
        raise QueryError("at must carry its zone (Z or an offset)")
    if t.second or t.microsecond:
        raise QueryError("at is on a whole minute (replay has minute resolution)")
    try:
        t = t.astimezone(timezone.utc)
    except OverflowError:               # 0001-01-01T00:00:00+23:59: a valid instant whose UTC form is out of range
        raise QueryError(f"at is not an ISO-8601 instant: {at!r}") from None
    if t > now or t < now - timedelta(seconds=REPLAY_WINDOW_S):
        raise QueryError(f"at must be inside the last {REPLAY_WINDOW_S // 3600} h (now - {REPLAY_WINDOW_S // 3600} h .. now)")
    return t


def _fold(text: str) -> str:
    """Case- and accent-folded, for the rows-scope substring match."""
    return "".join(c for c in unicodedata.normalize("NFD", text.casefold()) if unicodedata.category(c) != "Mn")


def _terms_match(texts: list[str], q: str) -> bool:
    """Space-separated terms ANDed; each a substring of some field (plain text, never a pattern)."""
    hay = _fold(" ".join(texts))
    return all(t in hay for t in _fold(q).split())


def _compact_value(kind: str, value: Any) -> Any:
    if value is None:
        return None
    if kind == "ambient":
        if "visual" in value:       # a levain.cockpit/1 visual
            return {"visual": value["visual"], "text": value["text"]}
        return {"legend": value.get("legend", ""), "events": len(value.get("events") or [])}
    if kind == "brief":
        return {"headline": value.get("headline", "")}
    if kind == "topology":
        # the phone draws no topology (F12_SPEC): the counts, and the full value one link away
        return {"as_of": value.get("as_of"), "nodes": len(value.get("nodes") or []),
                "tasks": len(value.get("tasks") or []),
                "waiting": sum(1 for t in value.get("tasks") or [] if t.get("state") == "waiting")}
    if kind == "board":
        # the same reduction: per-column counts and the queue's own state (never the cards), the full value one link away
        return {"queue": value.get("queue"), "note": value.get("note"), "unreadable": list(value.get("unreadable") or []),
                "columns": [{"category": c.get("category"), "count": len(c.get("cards") or [])} for c in value.get("columns") or []]}
    return value
