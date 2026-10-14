"""levain.cockpit.authority: the one authority snapshot a request renders and tiers from (K2b-3b
design §0.1, build plan "Render (lane B)").

``LabelKey`` names what a row's label is computed over; a ``LabelSnapshot`` (the broker's labeller,
``levain.cockpit.labels``) answers ``provenance(key)`` and ``label(key)`` from ONE journal read.
``RequestAuthority(now, labels)`` is captured once at each outer boundary (a GET, ``dispatch``,
``retier``) and rides on ``ReadContext.authority`` (a write's source reads find it through ``bound``),
so every row, note, tier and etag of one request comes from the same snapshot. ``NO_LABELS`` is the snapshot with no labeller attached: every
provenance is null and every label ``unverified``, which is today's output exactly.

:meth:`RequestAuthority.capture` is the ONE place the signed authority log is read (trust-tiering
DESIGN §3.4): inside one read transaction it takes the verified replay at the log's head and builds the
labels from that same snapshot, so who may sign, the fence a record carries and a label's provenance
always come from one head.

The engine consumes only ``revision``, ``error``, ``provenance(key)`` and ``label(key)``."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator, Mapping, Protocol

if TYPE_CHECKING:
    import sqlite3

    from levain.authority import AuthoritySnapshot, ReplayCache

UNVERIFIED = "unverified"


@dataclass(frozen=True)
class LabelKey:
    resource_key: str
    values: Mapping[str, Any]          # field -> the CURRENT stored value
    operation_id: str | None = None    # set: exactly that op's labels row; None: the latest by content
    gate: str | None = None            # tomb rows only: "match" | "mismatch" | "unavailable"


class LabelSnapshot(Protocol):
    revision: tuple[str, int, int, str] | None
    error: str | None

    def provenance(self, key: LabelKey | None) -> dict[str, Any] | None: ...

    def label(self, key: LabelKey | None) -> str: ...


class _NoLabels:
    """No labeller attached: nothing is labelled, and nothing says provenance is unavailable."""

    revision: tuple[str, int, int, str] | None = None
    error: str | None = None
    degraded = 0

    def provenance(self, key: LabelKey | None) -> dict[str, Any] | None:
        return None

    def label(self, key: LabelKey | None) -> str:
        return UNVERIFIED


NO_LABELS: LabelSnapshot = _NoLabels()


@dataclass(frozen=True)
class _Unavailable:
    """A labeller that raised: every keyed row renders ``unverified`` with nulls, never stronger
    than the journal proves, and the panel says why."""

    error: str
    revision: tuple[str, int, int, str] | None = None
    degraded: int = 0

    def provenance(self, key: LabelKey | None) -> dict[str, Any] | None:
        if key is None:
            return None
        nulls = {"label": UNVERIFIED, "signer": None, "fired_at": None, "operation_id": None}
        return {**nulls, "credential": None, "fields": {f: dict(nulls) for f in key.values}}

    def label(self, key: LabelKey | None) -> str:
        return UNVERIFIED


@dataclass(frozen=True)
class _Inactive:
    """A server whose broker is not ACTIVE (STANDBY, or write-disabled): it holds no journal it may read,
    so every row's provenance is null, as with no labeller, and the panel says where it is served
    (K2b-3b §8d row 1)."""

    error: str
    revision: tuple[str, int, int, str] | None = None
    degraded: int = 0

    def provenance(self, key: LabelKey | None) -> dict[str, Any] | None:
        return None

    def label(self, key: LabelKey | None) -> str:
        return UNVERIFIED


def inactive(note: str) -> LabelSnapshot:
    return _Inactive(note)


def unavailable(reason: str) -> LabelSnapshot:
    return _Unavailable(f"provenance unavailable: {reason}")


class TierPolicy(Protocol):
    """What a verb's tier reads of the standing policy: ``levain.authority.StandingPolicy``, or
    :data:`UNKNOWN_POLICY`."""

    def apply(self, verb: str, tier: str) -> str: ...


class _UnknownPolicy:
    """The policy of a request whose authority log could not be read or was refused: every verb at
    T3, never lower, since a raise it cannot see may stand in the log."""

    def apply(self, verb: str, tier: str) -> str:
        return "T3"


UNKNOWN_POLICY: TierPolicy = _UnknownPolicy()


class Labeller(Protocol):
    def snapshot(self, conn: "sqlite3.Connection", authority: "AuthoritySnapshot | None") -> LabelSnapshot: ...


@dataclass(frozen=True)
class RequestAuthority:
    now: datetime
    labels: LabelSnapshot = NO_LABELS
    # the verified authority log head this request decides under: None when the log is empty, or
    # was refused (its replay, its anchor, or the verifier)
    authority: "AuthoritySnapshot | None" = None
    # why this request may not sign (None when it may); operator-facing
    authority_error: str | None = None
    # the journal it was captured from (its path); None when no journal was read
    source: str | None = None
    # the log has no links (and no anchor names one), or, with ``no_log``, no trace of a log exists on the
    # data root (``Broker._ledger_trace``): nothing signs, a token-reported fire still may
    log_empty: bool = False
    # no log was asked: this server's broker is not ACTIVE, so it holds no journal it may read (K2b-3b
    # §8d row 1). Unless ``log_empty`` too, its policy is UNKNOWN_POLICY: the log it cannot read may hold a raise
    no_log: bool = False
    # journal reads one request shares (K2b-3b §0.6: Recent edits' broker side), so a write's probes
    # through ``_scan_read_one`` read the journal once, never once per probe
    _memo: dict[str, tuple[bool, Any]] = field(default_factory=dict, compare=False, repr=False)
    _memo_lock: threading.Lock = field(default_factory=threading.Lock, compare=False, repr=False)

    def memo(self, key: str, fn: Callable[[], Any]) -> Any:
        """``fn()`` once per request; its exception, too, is the answer every later caller gets."""
        with self._memo_lock:
            if key not in self._memo:
                try:
                    self._memo[key] = (True, fn())
                except Exception as exc:  # noqa: BLE001 - re-raised to every caller
                    self._memo[key] = (False, exc)
            ok, got = self._memo[key]
        if not ok:
            raise got
        return got

    @property
    def policy(self) -> "TierPolicy":
        """The standing policy this request tiers under (trust-tiering DESIGN §4.1): the replay's, from
        the one snapshot this request captured. The genesis policy (the shipped default) only when the
        log is positively established empty, or when the cockpit has no authority system at all (no
        broker attached: a read-only server with no data root). Otherwise what the log raises is
        unknown (a log refused or unreadable, or a broker that is not ACTIVE, ``no_log``, on a data root
        holding any trace of a log: STANDBY, install-disabled, failed at start), so every verb is T3
        (:data:`UNKNOWN_POLICY`) and the inactive broker refuses it: a fire never falls back to the
        shipped default past a signed raise it cannot see."""
        from levain.authority import genesis_policy
        if self.authority is not None:
            return self.authority.policy
        if self._genesis:
            return genesis_policy()
        return UNKNOWN_POLICY

    @property
    def _genesis(self) -> bool:
        return self.log_empty or (self.authority_error is None and not self.no_log)

    @property
    def revision(self) -> list[Any]:
        """The authority head this request's policy was read at, hashed into every etag (DESIGN §4.2:
        one counter): the fence a record issued now would carry, or why there is no head."""
        from levain.cockpit.firejournal import authority_fence
        if self.authority is not None:
            return sorted(authority_fence(self.authority).items())
        return ["genesis"] if self._genesis else ["unknown"]

    @property
    def fence(self) -> dict[str, Any] | None:
        """The fence a record issued in this request carries; None when the authority log was
        refused, so nothing may be issued."""
        from levain.cockpit.firejournal import authority_fence
        if self.authority is None and not self.log_empty:
            return None
        return authority_fence(self.authority)

    @classmethod
    def capture(cls, conn: "sqlite3.Connection", *, now: datetime, anchor: Path | None,
                labeller: Labeller | None = None, source: str | None = None,
                cache: "ReplayCache | None" = None, ssh_keygen: Path | str | None = None) -> "RequestAuthority":
        """The request's authority, read in ``conn``'s open read transaction: the verified replay of
        the log's head (the per-process cache's, replayed only if absent), checked against the
        ``anchor`` file, then the labels built from that same snapshot. ``conn``'s transaction must not
        have read yet: the anchor is read first, so its snapshot is pinned after it. A refused log captures as no
        authority with the reason: no ``AuthorityError`` escapes (a label is never a gate on reading).
        A ``sqlite3.Error`` reading the journal does escape; at broker start (``Broker._advance_anchor``)
        it fails that start attempt (a busy journal is retried, any other error disables the broker)."""
        from levain import authority as auth
        from levain.cockpit.firejournal import INIT_HINT, UNINITIALISED, _meta

        kw: dict[str, Any] = {"cache": cache}
        if ssh_keygen is not None:
            kw["ssh_keygen"] = ssh_keygen
        snap: AuthoritySnapshot | None = None
        error: str | None = None
        empty = False
        try:
            # the anchor is read BEFORE the replay's first SELECT pins the read snapshot: an append
            # committing in between can then only put the log ahead of the anchor, never behind it
            pinned = None if anchor is None else auth.read_anchor(anchor)
            try:
                snap = auth.snapshot(conn, **kw)
            except auth.NotInitialised:
                if pinned is not None:
                    raise auth.AnchorError(f"the authority log is empty but its anchor names store "
                                           f"{pinned.store_id} at seq {pinned.seq}") from None
                empty = True
                error = f"authority not initialised: run {INIT_HINT}"
            if snap is not None and anchor is not None:
                auth.check_anchor(snap, pinned)
        except auth.AuthorityError as exc:
            snap, empty = None, False
            error = f"the authority log is refused: {exc}"
        if snap is not None:
            marker = _meta(conn, UNINITIALISED)
            if marker is not None:
                error = f"authority not initialised ({marker}): run {INIT_HINT}"
        labels = NO_LABELS if labeller is None else labeller.snapshot(conn, snap)
        return cls(now, labels, snap, error, source, empty)


# The outer boundary's authority for the reads it makes through ``Cockpit.read_one`` /
# ``read_value_head`` (verbs' ``dispatch`` and ``retier``): bound for the request, read by the engine
# when no ``authority=`` is passed, so a read inside a write never takes a second snapshot.
_BOUND: ContextVar[RequestAuthority | None] = ContextVar("levain_cockpit_authority", default=None)


@contextmanager
def bound(authority: RequestAuthority) -> Iterator[RequestAuthority]:
    token = _BOUND.set(authority)
    try:
        yield authority
    finally:
        _BOUND.reset(token)


def current() -> RequestAuthority | None:
    return _BOUND.get()
