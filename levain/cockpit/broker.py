"""levain.cockpit.broker: the one process-wide owner of ``cockpit.db`` that turns a T2/T3 cockpit
write into a consented, journalled fire (K2b design §3, §4.2, §4.3, §6).

A T2/T3 write never fires from the POST handler: it is ISSUED as a pending holding the JCS consent
record (§3.1), DECIDED in one transaction, the commit point (§3.2), then APPLIED unit by unit
through each store's ``EffectStore`` (§3.3) and FINISHED (§3.4). ``intent: "sign"`` returns the
pending for a signature (``submit``); ``intent: "reported"`` decides in the issuing request, only in
a reported install, only T2, only on a surface token.

**One broker per data root, as one lifecycle state** (§4.2): ``STANDBY -> ACTIVATING -> ACTIVE ->
DRAINING -> CLOSED``, plus ``INSTALL_DISABLED`` (never healed). The start sequence, run by the
first start and by every heal: local-FS check -> exclusive non-blocking ``flock`` on
``<data_root>/cockpit.lock`` -> open ``cockpit.db`` -> install checks -> ``expire_due`` -> recovery ->
the re-drive thread -> write routes open. In STANDBY no ``cockpit.db`` is opened and every
broker-backed read is a typed ``Fault``. Stop drains: write admission closes, admitted settlers are
waited for (up to the drain deadline), and only then is the lock released; a settler still inside a
store call keeps the lock held until process exit.

The settlers are §4.2's: start recovery, the request that decided the op (once, right after its
commit point), and the re-drive thread. A read of an applying op wakes the thread and never settles.
One settler per op: an in-process mutex per ``operation_id``, which the thread skips while held.

``_kp(name)`` is a kill point (§7): a no-op unless the constructor's ``kill_at`` argument names it,
then ``os._exit(137)``. The environment is never read for it.
"""
from __future__ import annotations

import contextlib
import enum
import fcntl
import logging
import os
import sqlite3
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from levain.autonomic.confirm import (
    COCKPIT_NAMESPACE,
    DEFAULT_SSH_KEYGEN,
    UNAVAILABLE,
    VERIFIED,
    verify_cockpit_signature,
)
from levain.autonomic.journal import JournalCorruptError
from levain.cockpit.anneal_effects import (
    EPISODE_CANONICAL,
    EPISODES,
    SECTION_CANONICAL,
    SECTION_VERB,
    SECTIONS,
    TOMBSTONE_VERB,
    section_effects,
    tombstone_effects,
)
from levain.cockpit.consent import ConsentError, build_record, encode, parse_stamp, utc_stamp, value_sha256
from levain.cockpit.effects import (
    SPORE_AUTHORITY_FIELDS,
    SPORE_CANONICAL,
    SPORES,
    ApplyOutcome,
    ApplyUnit,
    DownstreamEffect,
    Effect,
    EffectState,
    EffectStatus,
    EffectStore,
    MemberApply,
    PermanentApplyRefusal,
    Snapshot,
    TransientApplyFault,
    UnitApplyResult,
    _stored_authority,
    canonical_effect,
    create_origin_key,
    plan_units,
    registration_refusals,
    resolve_effect,
    settle,
    spore_resource_key,
)
from levain.cockpit.firejournal import (
    CockpitDB,
    CockpitFormatError,
    DecisionRows,
    EditsPage,
    IdempotencyConflict,
    PendingCapExceeded,
    UnitResult,
    UnknownPending,
    new_pending_id,
    request_shape,
)
from levain.cockpit.install import REPORTED, decide_install, network_fs_problem
from levain.cockpit.labels import BrokerLabeller, LabelSnapshot, label_class
from levain.authority import AnchorError, advance_anchor, anchor_path
from levain.cockpit.authority import RequestAuthority, bound, current, inactive
from levain.cockpit.results import Absent, Fault
from levain.cockpit.verbs import KERNEL_VERBS

if TYPE_CHECKING:
    from levain.authority import AuthoritySnapshot

__all__ = ["Broker", "BrokerError", "COCKPIT_STORE", "INTERNAL_VERBS", "LOCK_NAME", "Lifecycle",
           "PENDING_TTL", "STANDBY_FAULT", "label_class"]

_log = logging.getLogger(__name__)

LOCK_NAME = "cockpit.lock"
PENDING_TTL = timedelta(minutes=15)      # an unsigned cockpit pending's life (flow design §4.3)
COCKPIT_STORE = "cockpit"                # cockpit.db's own objects as a store (§4.1.4)
STANDBY_FAULT = "write-disabled: another broker holds this data root"
INTERNAL_VERBS = ("pending_cancel", "token_rotate")
_TIERS = ("C1", "T2", "T3")


class Lifecycle(str, enum.Enum):
    STANDBY = "STANDBY"
    ACTIVATING = "ACTIVATING"
    ACTIVE = "ACTIVE"
    DRAINING = "DRAINING"
    CLOSED = "CLOSED"
    INSTALL_DISABLED = "INSTALL_DISABLED"


class BrokerError(Exception):
    """A refusal the HTTP layer returns as ``{error: code, message, **extra}`` with ``status``."""

    def __init__(self, code: str, status: int, message: str,
                 extra: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.extra = dict(extra or {})


def _transient(exc: BaseException) -> bool:
    """SQLite's busy/locked: another process holds the journal's write lock for now."""
    return isinstance(exc, sqlite3.OperationalError) and any(
        w in str(exc).lower() for w in ("locked", "busy"))


# --- cockpit.db's own objects as an EffectStore (§3.5, §4.1.4, §13) ---------------------------------

class _CockpitObjects:
    """``pend:<id>`` (a pending's ``state``) and ``token:<name>`` (its ``generation``), applied to
    ``cockpit.db`` by its own transactions. ``by`` is the deciding op's principal, recorded as the
    canceller. No label: these fields are not provenance-bearing."""

    name = COCKPIT_STORE
    version_fields = ("state", "generation")
    authority_fields = ("state", "generation")
    store_assigned_fields: tuple[str, ...] = ()

    def __init__(self, db: CockpitDB, *, by: str, now: datetime) -> None:
        self.db, self.by, self.now = db, by, now

    def snapshot(self, resource_key: str) -> Snapshot:
        kind, _, ident = resource_key.partition(":")
        try:
            if kind == "pend":
                row = {"state": self.db.outcome(ident).state}
            elif kind == "token":
                gen = self.db.token_generation(ident)
                row = None if gen is None else {"generation": gen}
            else:
                raise PermanentApplyRefusal(f"{resource_key!r} is not a cockpit object")
        except UnknownPending:
            row = None
        except (OSError, CockpitFormatError, JournalCorruptError) as exc:
            raise TransientApplyFault.of(exc) from exc
        return Snapshot(row, None)

    def units(self, effects: Sequence[tuple[int, Effect]]) -> list[ApplyUnit]:
        return [ApplyUnit(COCKPIT_STORE, (i,), (e,)) for i, e in effects]

    def label_fields(self, effect: Effect, stored: Mapping[str, Any] | None) -> tuple[str, ...]:
        return ()

    def apply_unit(self, unit: ApplyUnit) -> UnitApplyResult:
        if len(unit.effects) != 1:
            raise PermanentApplyRefusal("a cockpit unit is one effect")
        eff = unit.effects[0]
        kind, _, ident = eff.resource_key.partition(":")
        want = dict(eff.expect).get("fields")
        if kind == "pend" and want == {"state": "WAITING"} and dict(eff.changes) == {"state": "CANCELLED"}:
            res = self.db.cancel_as_effect(ident, by=self.by, now=self.now)
        elif kind == "token" and isinstance(want, dict) and set(want) == {"generation"} \
                and set(eff.changes) == {"generation"}:
            res = self.db.rotate_token(ident, expect_generation=want["generation"],
                                       new_generation=eff.changes["generation"], by=self.by,
                                       now=self.now)
        else:
            raise PermanentApplyRefusal(f"no cockpit write for {eff.resource_key!r}")
        outcome = {"already": ApplyOutcome.ALREADY, "applied": ApplyOutcome.APPLIED,
                   "precondition_lost": ApplyOutcome.PRECONDITION_LOST}[res]
        return UnitApplyResult(outcome, (MemberApply(None, dict(eff.changes)),))


@dataclass(frozen=True)
class _Built:
    target: dict[str, Any]
    effects: list[Effect]
    pre_images: list[Any]


class _Unbuildable(Exception):
    """The verb's effect cannot be built from the object as it now stands (it moved since issue)."""


# each store name the broker registers, and the verbs its canonical_effect builds (K2b §13)
_STORE_CANONICAL: dict[str, Mapping[str, Any]] = {SPORES: SPORE_CANONICAL, EPISODES: EPISODE_CANONICAL,
                                                   SECTIONS: SECTION_CANONICAL}


# --- the broker -------------------------------------------------------------------------------------

class Broker:
    """The cockpit broker over ``data_root``. ``stores`` maps a store name to its ``EffectStore``;
    ``install_class`` is the ``--install-class`` declaration (``None`` = none given); ``store_paths``
    are the registered store paths the local-FS check covers; ``tier_of(verb, params, target)``
    recomputes a registry verb's tier from a fresh read for the decision (§3.2.1): ``Absent`` when
    the target is gone, ``None`` or a ``Fault`` when the read faulted. ``kill_at`` names one kill
    point (§7)."""

    def __init__(self, data_root: Path | str, *, stores: Mapping[str, EffectStore],
                 install_class: str | None, store_paths: Sequence[Path | str] = (),
                 tier_of: Callable[[str, Mapping[str, Any], Mapping[str, Any]], str | Absent | Fault | None]
                 | None = None,
                 clock: Callable[[], datetime] | None = None, kill_at: str | None = None,
                 lock_wait: float = 10.0, lock_retry: float = 5.0, lock_poll: float = 0.25,
                 drain_timeout: float = 5.0, min_wait: float = 0.2, max_wait: float = 5.0,
                 ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
                 marker_directory: Path | None = None,
                 downstream: Mapping[str, DownstreamEffect] | None = None) -> None:
        for name, store in stores.items():
            canonical = _STORE_CANONICAL.get(name)
            refusals = registration_refusals(store, canonical)
            if refusals or name == COCKPIT_STORE:
                raise ValueError(f"store {name!r} refused at registration: {refusals or ['reserved name']}")
        self.downstream = self.check_downstream(downstream or {})
        self.data_root = Path(data_root)
        self.stores = {**stores, **{d.store.name: d.store for d in self.downstream.values()}}
        self.declared = install_class
        self.install_class: str | None = None
        self.store_paths = tuple(Path(p) for p in store_paths)
        self.tier_of = tier_of
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._kill_at = kill_at
        self._lock_wait, self._lock_retry, self._lock_poll = lock_wait, lock_retry, lock_poll
        self._drain_timeout = drain_timeout
        self._min_wait, self._max_wait = min_wait, max_wait
        self.ssh_keygen = ssh_keygen
        self._marker_directory = marker_directory

        self._mu = threading.Lock()
        self._wake = threading.Condition(self._mu)      # the re-drive thread's one condition (§4.2)
        self._drained = threading.Condition(self._mu)
        self._state = Lifecycle.STANDBY
        self._started = False
        self.disabled_reason: str | None = None
        self._generation = 0
        self._inflight = 0
        self._op_locks: dict[str, list[Any]] = {}       # pid -> [mutex, users], pruned at zero users
        self._lock_fd: int | None = None
        self.db: CockpitDB | None = None
        self._labeller: BrokerLabeller | None = None
        self._thread: threading.Thread | None = None
        self._healer: threading.Thread | None = None
        self._stop = threading.Event()

    @staticmethod
    def check_downstream(downstream: Mapping[str, Any]) -> dict[str, DownstreamEffect]:
        """The downstream verbs' effects, refused at registration (K2b §13) when one is not a
        ``DownstreamEffect``, names a kernel verb, fails ``registration_refusals``, takes a kernel
        store's name, or names a store another verb registered as a different object."""
        out: dict[str, DownstreamEffect] = {}
        by_name: dict[str, Any] = {}
        for verb, d in downstream.items():
            if not isinstance(d, DownstreamEffect):
                raise ValueError(f"downstream effect {verb!r} must be a DownstreamEffect")
            if verb in SPORE_CANONICAL or verb in (TOMBSTONE_VERB, SECTION_VERB, *INTERNAL_VERBS) \
                    or verb in {v.name for v in KERNEL_VERBS}:
                raise ValueError(f"{verb!r} is a kernel verb; its effect is the kernel's")
            refusals = registration_refusals(d.store, {verb: d.canonical} if callable(d.canonical) else None)
            if refusals:
                raise ValueError(f"downstream effect {verb!r} refused at registration: {refusals}")
            name = d.store.name
            if not isinstance(name, str) or not name or name in _STORE_CANONICAL or name == COCKPIT_STORE:
                raise ValueError(f"downstream effect {verb!r}: store name {name!r} is reserved or empty")
            if by_name.setdefault(name, d.store) is not d.store:
                raise ValueError(f"downstream effect {verb!r}: another store is registered as {name!r}")
            out[verb] = d
        return out

    # -- lifecycle -------------------------------------------------------------------------------

    @property
    def state(self) -> Lifecycle:
        with self._mu:
            return self._state

    @property
    def enabled(self) -> bool:
        return self.state is Lifecycle.ACTIVE

    def capture(self, now: datetime | None = None) -> RequestAuthority:
        """One request's authority (K2b-3b §0.1, trust-tiering DESIGN §3.4): one read of THIS broker's
        journal, the authority log's verified head and the labels built from it, served only while
        ACTIVE, when the broker has opened and initialised the journal. Any other state reads no file,
        labels nothing and holds no authority (§8d row 1: there is no read-only opener, since one
        would create ``-shm``/``-wal`` or miss an uncheckpointed WAL)."""
        now = self._now() if now is None else now
        db = self._active_db()
        if db is None:
            return RequestAuthority(now, inactive("provenance is served by the active broker"),
                                    authority_error="the broker is not active")
        return self._capture(db, now)

    def _capture(self, db: CockpitDB, now: datetime) -> RequestAuthority:
        labeller = self._labeller
        if labeller is None or labeller.db is not db:
            labeller = self._labeller = BrokerLabeller(db)
        with db.read() as conn:
            return RequestAuthority.capture(conn, now=now, anchor=anchor_path(db.data_root), labeller=labeller,
                                            source=str(db.path), ssh_keygen=self.ssh_keygen)

    def _request_authority(self, db: CockpitDB) -> RequestAuthority:
        """The authority this request was captured under: the one bound by its outer boundary when it
        was captured from this journal, else one captured now (a direct broker call is its own
        request)."""
        bound_auth = current()
        if bound_auth is not None and bound_auth.source == str(db.path):
            return bound_auth
        return self._capture(db, self._now())

    def labels(self) -> LabelSnapshot:
        """The labels of a fresh :meth:`capture`."""
        return self.capture().labels

    def _active_db(self) -> CockpitDB | None:
        with self._mu:
            return self.db if self._state is Lifecycle.ACTIVE else None

    def decision_rows(self, now: datetime) -> DecisionRows | None:
        """The ``decisions`` panel's broker rows (K2b-3b §3) from THIS broker's journal, served only
        while ACTIVE (§8d row 1); None otherwise. Raises on a journal fault (the panel notes it)."""
        db = self._active_db()
        return None if db is None else db.decision_rows(now)

    def edits(self, limit: int) -> EditsPage | None:
        """Recent edits' broker rows (K2b-3b §0.6), served only while ACTIVE; None otherwise."""
        db = self._active_db()
        return None if db is None else db.edits(limit)

    def start(self) -> Lifecycle:
        """Run the start sequence once now; on a held lock stay STANDBY and heal in the background:
        retried every ``lock_poll`` for ``lock_wait`` seconds, then every ``lock_retry`` (§4.2)."""
        with self._mu:
            if self._started:
                return self._state
            self._started = True
        decision = decide_install(self.declared, self.data_root, marker_directory=self._marker_directory)
        if decision.install_class is None:
            return self._disable(decision.reason or "install check")
        self.install_class = decision.install_class
        if self._attempt():
            return self.state
        if self.state is Lifecycle.STANDBY:
            self._healer = threading.Thread(target=self._heal, name="cockpit-broker-heal", daemon=True)
            self._healer.start()
        return self.state

    def _disable(self, reason: str) -> Lifecycle:
        with self._mu:
            self._state = Lifecycle.INSTALL_DISABLED
            self.disabled_reason = reason
        _log.warning("cockpit broker disabled: %s", reason)
        return Lifecycle.INSTALL_DISABLED

    def _heal(self) -> None:
        begun = self._clock_monotonic()
        while not self._stop.is_set():
            fast = self._clock_monotonic() - begun < self._lock_wait
            if self._stop.wait(self._lock_poll if fast else self._lock_retry):
                return
            if self.state is not Lifecycle.STANDBY:
                return
            if self._attempt():
                return

    @staticmethod
    def _clock_monotonic() -> float:
        import time
        return time.monotonic()

    def _attempt(self) -> bool:
        """The start sequence, in order (§4.2). True when ACTIVE. False leaves STANDBY (the lock is
        held elsewhere, or the journal was busy: the healer retries), INSTALL_DISABLED (anything
        else, with its reason), or the state a stop that arrived meanwhile set. Every failure after
        the lock is taken closes the lock's fd before returning."""
        problem = network_fs_problem([self.data_root, *self.store_paths])
        if problem is not None:
            self._disable(problem)
            return False
        try:
            self.data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.data_root / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        except OSError as exc:
            self._disable(f"data root {self.data_root}: {exc}")
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        except BaseException:
            os.close(fd)
            raise
        with self._mu:
            if self._stop.is_set():
                os.close(fd)
                return False
            self._lock_fd = fd
            self._state = Lifecycle.ACTIVATING
            # recovery below is a settler, so a stop during it drains like any other
            self._inflight += 1
        failure: Exception | None = None
        try:
            db = CockpitDB(self.data_root)
            stored = db.set_install_class(self.install_class or "")
            if stored != self.install_class:
                raise CockpitFormatError(f"journal was started as {stored!r}, not {self.install_class!r}")
            now = self._clock()
            db.expire_due(now)
            self._advance_anchor(db, now)
            self.db = db
            self._recover(now, db)
        except Exception as exc:  # noqa: BLE001 - each failure is classified below, the fd closed
            failure = exc
        finally:
            with self._mu:
                self._inflight -= 1
                self._drained.notify_all()
                fd_to_close = None
                if failure is not None:
                    # the attempt took the fd, so the attempt closes it, whether or not a stop is
                    # waiting: a stop finds no fd and no settler, and releases nothing twice
                    fd_to_close, self._lock_fd, self.db = self._lock_fd, None, None
                    waiting = self._state in (Lifecycle.ACTIVATING, Lifecycle.DRAINING)
                    if waiting and not _transient(failure):
                        self._state = Lifecycle.INSTALL_DISABLED
                        self.disabled_reason = f"cockpit journal: {type(failure).__name__}: {failure}"
                    elif self._state is Lifecycle.ACTIVATING:
                        self._state = Lifecycle.STANDBY
            if fd_to_close is not None:
                os.close(fd_to_close)
        if failure is not None:
            if _transient(failure):
                _log.warning("cockpit broker: journal busy at start, retrying: %s", failure)
            else:
                _log.warning("cockpit broker disabled: %s", self.disabled_reason)
            return False
        with self._mu:
            if self._state is not Lifecycle.ACTIVATING:
                return False          # a stop arrived during recovery; it owns the lock now
            self._state = Lifecycle.ACTIVE
            self._thread = threading.Thread(target=self._redrive, name="cockpit-broker-redrive", daemon=True)
            self._thread.start()
        return True

    def _advance_anchor(self, db: CockpitDB, now: datetime) -> None:
        """At open, a log at or ahead of its anchor moves the anchor up to its head (DESIGN §3.8). A log
        the anchor refuses is left refused (every request captures that refusal); it never stops the
        broker, whose token-reported reads and writes do not depend on it."""
        auth = self._capture(db, now)
        if auth.authority is None:
            if not auth.log_empty:
                _log.warning("cockpit broker: %s", auth.authority_error)
            return
        try:
            advance_anchor(anchor_path(db.data_root), auth.authority)
        except (AnchorError, OSError) as exc:
            _log.warning("cockpit broker: the authority anchor was not advanced: %s", exc)

    def stop(self) -> Lifecycle:
        """DRAINING, then CLOSED (§4.2): admission closes, admitted settlers are waited for up to the
        drain deadline, then the lock is released. A settler still running keeps it until exit."""
        self._stop.set()
        with self._mu:
            if self._state in (Lifecycle.CLOSED, Lifecycle.INSTALL_DISABLED):
                return self._state
            if self._state is Lifecycle.STANDBY:
                self._state = Lifecycle.CLOSED
                return self._state
            self._state = Lifecycle.DRAINING
            self._wake.notify_all()
            deadline = self._clock_monotonic() + self._drain_timeout
            while self._inflight > 0:
                left = deadline - self._clock_monotonic()
                if left <= 0:
                    break
                self._drained.wait(left)
            drained = self._inflight == 0
            if self._state is Lifecycle.INSTALL_DISABLED:
                # the start attempt this stop waited on failed and disabled the broker; it closes its fd
                return self._state
            self._state = Lifecycle.CLOSED
            fd = self._lock_fd if drained else None
            if drained:
                self._lock_fd = None
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._drain_timeout)
        if fd is not None:
            os.close(fd)
        if not drained:
            _log.warning("cockpit broker: a settler outlived the drain; the lock is held until exit")
        return Lifecycle.CLOSED

    # -- admission, wakes, kill points -----------------------------------------------------------

    def _unavailable(self) -> BrokerError:
        state = self._state
        if state is Lifecycle.INSTALL_DISABLED:
            return BrokerError("install_disabled", 403, f"write-disabled: {self.disabled_reason}")
        if state is Lifecycle.STANDBY:
            return BrokerError("standby", 503, STANDBY_FAULT)
        return BrokerError("broker_unavailable", 503, f"write-disabled: the broker is {state.value}")

    @contextlib.contextmanager
    def _admitted(self) -> Iterator[CockpitDB]:
        with self._mu:
            if self._state is not Lifecycle.ACTIVE or self.db is None:
                raise self._unavailable()
            self._inflight += 1
            db = self.db
        try:
            yield db
        finally:
            with self._mu:
                self._inflight -= 1
                self._drained.notify_all()

    def _bump(self) -> None:
        """A commit created DECIDED work or moved a ``next_attempt_at``: a new deadline (§4.2)."""
        with self._mu:
            self._generation += 1
            self._wake.notify_all()

    def wake(self) -> None:
        """What a read of a DECIDED op does: wake the thread, which still honours due times."""
        with self._mu:
            self._wake.notify_all()

    def _kp(self, name: str) -> None:
        if self._kill_at is not None and self._kill_at == name:
            os._exit(137)

    def _now(self) -> datetime:
        return self._clock()

    # -- principals ------------------------------------------------------------------------------

    def principal_of(self, db: CockpitDB, credential: Mapping[str, Any]) -> str:
        cls = credential.get("class")
        if cls == "token":
            name = credential.get("name")
            if not isinstance(name, str) or not name:
                raise BrokerError("credential_required", 403, "a token credential names its surface")
            return f"token:{name}:{db.ensure_token(name)}"
        if cls == "device":
            fp = credential.get("fingerprint")
            if not isinstance(fp, str) or not fp:
                raise BrokerError("credential_required", 403, "a device credential names its key")
            return f"device:{fp}"
        raise BrokerError("credential_required", 403, "a write needs a credential")

    @staticmethod
    def principal_base_of(credential: Mapping[str, Any]) -> str | None:
        if credential.get("class") == "token" and isinstance(credential.get("name"), str):
            return f"token:{credential['name']}"
        if credential.get("class") == "device" and isinstance(credential.get("fingerprint"), str):
            return f"device:{credential['fingerprint']}"
        return None

    def replay(self, *, verb: str, params: Mapping[str, Any], intent: str | None,
               credential: Mapping[str, Any], idempotency_key: str,
               target: Mapping[str, Any]) -> dict[str, Any] | None:
        """A replayed POST's recorded pending and outcome, found by its idempotency key BEFORE the
        target is read again (the fire it made has moved the target's version), or None when the key
        is new. The same key with other params, or aimed at another target, is a 409 (§2)."""
        with self._admitted() as db:
            out = self._replayed(db, verb=verb, params=params, intent=intent, credential=credential,
                                 idempotency_key=idempotency_key, target=target, harness="cockpit")
        if out is not None and out["state"] == "applying":
            self.wake()
        return out

    def _replayed(self, db: CockpitDB, *, verb: str, params: Mapping[str, Any], intent: str | None,
                  credential: Mapping[str, Any], idempotency_key: str | None,
                  target: Mapping[str, Any], harness: str) -> dict[str, Any] | None:
        """``replay``'s lookup, run inside the caller's admission: every issue runs it before its
        target is built. A key names one request (verb, intent, params, target identity, harness;
        IETF draft-ietf-httpapi-idempotency-key-header §2), and its replay answers from the journal,
        never from the target again: a finished op returns its outcome, and a reported pending its
        request died before deciding resumes there, decided from its stored record (whose decide-time
        recompute ends a moved target STALE), the way a Stripe-style key resumes at its recovery
        point."""
        base = self.principal_base_of(credential)
        if base is None or idempotency_key is None:
            return None
        pid = db.find_idempotent(base, idempotency_key)
        if pid is None:
            return None
        view = self._load(db, pid)
        rec = view.record
        asked = request_shape(verb=verb, intent=intent or "sign", params=dict(params), target=target)
        if asked != request_shape(verb=rec.get("verb"), intent=rec.get("intent"), params=rec.get("params"),
                                  target=rec.get("target")):
            raise BrokerError("idempotency_conflict", 409,
                              f"idempotency key {idempotency_key!r} is bound to another request")
        if view.harness != harness:
            raise BrokerError("idempotency_conflict", 409,
                              f"idempotency key {idempotency_key!r} is bound to another harness's request")
        if rec.get("intent") == "reported" and view.state == "WAITING":
            why = self._reported_refusal(str(rec.get("tier")), credential, harness)
            if why is not None:
                raise BrokerError("refused", 403, why)
            auth = self._request_authority(db)
            with bound(auth):
                return {**self._decide(db, auth, pid, "ALLOWED_REPORTED", None, None), "replay": True}
        return self.result(db, pid, replay=True)

    # -- building a verb's effects (§3.1.1) -------------------------------------------------------

    def _build(self, db: CockpitDB, verb: str, params: Mapping[str, Any], target: Mapping[str, Any],
               pid: str, *, at_issue: bool) -> _Built:
        """The verb's effects from a lock-free snapshot. ``BrokerError`` for a refusal,
        ``TransientApplyFault`` for a read that faulted, ``_Unbuildable`` (at decide) for an object
        that moved so far the effect no longer builds."""
        try:
            if verb in self.downstream:
                return self._build_downstream(verb, params, target, pid)
            if verb in SPORE_CANONICAL:
                return self._build_spore(verb, params, target, pid, at_issue=at_issue)
            if verb == TOMBSTONE_VERB:
                return self._build_tombstone(params, target, pid, at_issue=at_issue)
            if verb == SECTION_VERB:
                return self._build_section(params, target, at_issue=at_issue)
            if verb == "pending_cancel":
                return self._build_cancel(db, target)
            if verb == "token_rotate":
                return self._build_rotate(db, params, target)
        except (ValueError, ConsentError) as exc:
            if not at_issue:
                raise _Unbuildable(str(exc)) from exc
            raise BrokerError("refused", 409, str(exc)) from exc
        raise BrokerError("not_built", 501, f"{verb!r} has no EffectStore adapter; it cannot fire "
                                            "through the broker")

    def _build_downstream(self, verb: str, params: Mapping[str, Any], target: Mapping[str, Any],
                          pid: str) -> _Built:
        """A downstream verb's one create, keyed by the pending as a spore seed is. The store is a
        client of another process, so any failure of its read is transient (a non-JSON body is a
        ``ValueError`` too); ``canonical`` refuses params by ``ValueError`` or ``ConsentError`` and any
        other failure of it is transient."""
        d = self.downstream[verb]
        try:
            eff = d.canonical(dict(params), origin_key=create_origin_key(pid, 0))
        except (ValueError, ConsentError, TransientApplyFault):
            raise
        except Exception as exc:  # noqa: BLE001 - a registrant's failure is not a refusal of the params
            _log.warning("downstream %r canonical failed; treated as transient", verb, exc_info=True)
            raise TransientApplyFault.of(exc) from exc
        if not isinstance(eff, Effect) or eff.store != d.store.name or not eff.is_create:
            raise ValueError(f"{verb!r}'s canonical_effect must build one create on {d.store.name!r}")
        try:
            row = d.store.snapshot(eff.resource_key).row
        except TransientApplyFault:
            raise
        except Exception as exc:  # noqa: BLE001 - every read fault of a downstream store is transient
            raise TransientApplyFault.of(exc) from exc
        if row is not None:
            raise ValueError("a create's origin_key is already in use")
        return _Built({"kind": target.get("kind"), "panel_id": target.get("panel_id")}, [eff], [None])

    def _spores(self) -> Any:
        store = self.stores.get(SPORES)
        if store is None:
            raise BrokerError("not_built", 501, "no spore store is registered with the broker")
        return store

    def _build_spore(self, verb: str, params: Mapping[str, Any], target: Mapping[str, Any], pid: str,
                     *, at_issue: bool) -> _Built:
        es = self._spores()
        if verb == "spore_seed":
            ok = create_origin_key(pid, 0)
            snap = es.snapshot(spore_resource_key(ok))
            eff = canonical_effect(verb, params, snap.row, origin_key=ok, version_of=es.version_of)
            return _Built({"kind": "create", "panel_id": target.get("panel_id")}, [eff], [None])
        if at_issue:
            ok = self._origin_key_of_row(es, target)
        else:
            ok = target.get("origin_key")
        snap = es.snapshot(spore_resource_key(ok))
        if at_issue and target.get("row_version") is not None and snap.version != target["row_version"]:
            raise BrokerError("stale", 409, f"row {target.get('row_id')!r} changed since it was rendered")
        eff = canonical_effect(verb, params, snap.row, version_of=es.version_of)
        pre = None if snap.row is None else {f: _stored_authority(snap.row, f) for f in SPORE_AUTHORITY_FIELDS}
        return _Built({"kind": "row", "panel_id": target.get("panel_id"), "row_id": target.get("row_id"),
                       "origin_key": ok}, [eff], [pre])

    def _build_tombstone(self, params: Mapping[str, Any], target: Mapping[str, Any], pid: str,
                         *, at_issue: bool) -> _Built:
        """A tombstone's two effects (``anneal_effects``) off one anneal snapshot. Its pre-image keeps
        the episode's id and type, never its content: the journal must not hold what the delete
        erases."""
        es: Any = self.stores.get(EPISODES)      # an EpisodeEffectStore: read and origin_key_of
        if es is None:
            raise BrokerError("not_built", 501, "no episode store is registered with the broker")
        if params:
            raise ValueError(f"episode_tombstone takes no params (got {sorted(params)})")
        if at_issue:
            row_id = str(target.get("row_id") or "")
            if not row_id.startswith("episode:"):
                raise BrokerError("bad_target", 400, f"{row_id!r} is not one of the entity's episodes")
            if not isinstance(target.get("row_version"), str) or not target["row_version"]:
                # an irreversible delete binds to the version the operator was shown (complement r1)
                raise BrokerError("bad_target", 400, f"a tombstone of {row_id!r} names the row_version it rendered")
            try:
                ok, source = es.origin_key_of(row_id[len("episode:"):])
            except KeyError:
                raise BrokerError("stale", 409, f"row {row_id!r} is gone") from None
            if ok is None:
                raise BrokerError("refused", 409, f"row {row_id!r} has no origin_key: no label resource yet")
            if source.startswith("team:"):
                # anneal's next v3 team replace brings back an entry its stream enforces unless the
                # delete carries team_operator, which only anneal's CLI sets (L2 r1 C)
                raise BrokerError("refused", 409, f"row {row_id!r} is a team entry: remove it with anneal's CLI")
        else:
            ok = target.get("origin_key")
            if not isinstance(ok, str) or not ok:
                raise ValueError("the record's target names no origin_key")
        got = es.read(ok)
        if got is None:
            if at_issue:
                raise BrokerError("stale", 409, f"row {target.get('row_id')!r} is gone")
            raise ValueError("the episode is gone")
        if at_issue and got.row["id"] != str(target.get("row_id"))[len("episode:"):]:
            raise BrokerError("stale", 409, f"row {target.get('row_id')!r} changed since it was rendered")
        if at_issue and got.rendered != target["row_version"]:
            raise BrokerError("stale", 409, f"row {target.get('row_id')!r} changed since it was rendered")
        effects = tombstone_effects(ok, got.version, effect_id=create_origin_key(pid, 0))
        pre = {"id": got.row["id"], "type": got.row["type"]}
        return _Built({"kind": "row", "panel_id": target.get("panel_id"), "row_id": target.get("row_id"),
                       "origin_key": ok}, effects, [None, pre])

    def _build_section(self, params: Mapping[str, Any], target: Mapping[str, Any], *, at_issue: bool) -> _Built:
        """A section edit's one effect off one anneal read (``anneal_effects.SectionEffectStore``).
        At issue the heading comes from the panel through the store's own schema, never the
        request; the version the operator was shown is compared with that read's rendered version,
        and the effect binds to anneal's version from the same read (SECTION_ADAPTER_DESIGN.md)."""
        from levain.dashboard import CLASS_A, _section_edit_class
        from levain.writes import MAX_BODY_BYTES

        ss: Any = self.stores.get(SECTIONS)     # a SectionEffectStore: heading_for and read
        if ss is None:
            raise BrokerError("not_built", 501, "no section store is registered with the broker")
        if set(params) != {"new_body"} or not isinstance(params["new_body"], str):
            raise ValueError(f"section_edit takes one string param, new_body (got {sorted(params)})")
        body = params["new_body"]
        if len(body.encode("utf-8")) > MAX_BODY_BYTES:
            raise BrokerError("too_large", 413, f"body exceeds {MAX_BODY_BYTES} bytes")
        from anneal_memory.origin import canonical_section_markdown

        # anneal's own header test on the canonical body: a '## ' line would split the section, and
        # replace_section refuses it only AFTER the operator consented (L4 section_drive, L2 r1)
        if any(line.startswith("## ") for line in canonical_section_markdown(body).split("\n")):
            if at_issue:
                raise BrokerError("invalid_body", 400, "a line starting '## ' would start a new section")
            raise ValueError("the body has a '## ' line")
        if at_issue:
            shown = target.get("panel_version")
            if not isinstance(shown, str) or not shown:
                raise BrokerError("bad_request", 400, "'panel_version' is required for a section edit")
            heading = ss.heading_for(str(target.get("panel_id")))
            if heading is None:
                raise BrokerError("stale", 409, f"panel {target.get('panel_id')!r} names no section now")
            if _section_edit_class(heading) != CLASS_A:
                raise BrokerError("not_editable", 403, f"the {heading!r} section is consolidated cognition "
                                                       "(Class C); only the operator's sections are editable")
        else:
            heading = target.get("heading")
            if not isinstance(heading, str) or not heading:
                raise ValueError("the record's target names no heading")
        got = ss.read(heading)
        if got is None:
            if at_issue:
                raise BrokerError("stale", 409, f"the {heading!r} section is gone")
            raise ValueError("the section is gone")
        if at_issue:
            if got.rendered != target["panel_version"]:
                raise BrokerError("stale", 409, f"panel {target.get('panel_id')!r} changed since it was rendered")
            if got.wrap_open:
                # a consented edit would wait out a whole wrap in capped retries and could end refused;
                # nothing is consented while one is open
                raise BrokerError("wrap_in_progress", 409, "a wrap is open on the memory store; edit after it saves")
        elif got.wrap_open:
            # a wrap opened after issue (a pending lives PENDING_TTL): the decision waits, the signature
            # stays unspent, and the operator retries once the wrap saves (L2 r1, L3 r1 complement #3)
            raise TransientApplyFault("WrapOpen", "a wrap is open on the memory store; retry after it saves")
        effects = section_effects(heading, got.version, body)
        return _Built({"kind": "value", "panel_id": target.get("panel_id"), "heading": heading,
                       "panel_version": target.get("panel_version")}, effects,
                      [{"heading": heading, "body": got.text}])

    @staticmethod
    def _origin_key_of_row(es: Any, target: Mapping[str, Any]) -> str:
        """The spore's ``origin_key``, read by its id. A row read with none is REFUSED: no label
        resource can name it, and none is invented (§2)."""
        from anneal_memory.spores import SporeError, SporeStore

        row_id = str(target.get("row_id") or "")
        if not row_id.startswith("spore:"):
            raise BrokerError("bad_target", 400, f"{row_id!r} is not a spore row")
        try:
            row = SporeStore(es.path).get(row_id[len("spore:"):])
        except (SporeError, OSError, ValueError) as exc:
            raise TransientApplyFault.of(exc) from exc
        if row is None:
            raise BrokerError("stale", 409, f"row {row_id!r} is gone")
        ok = row.get("origin_key")
        if not isinstance(ok, str) or not ok:
            raise BrokerError("refused", 409, f"row {row_id!r} has no origin_key: no label resource yet")
        return ok

    def _build_cancel(self, db: CockpitDB, target: Mapping[str, Any]) -> _Built:
        tid = target.get("pending_id")
        if not isinstance(tid, str):
            raise ValueError("a pending_cancel names its pending")
        try:
            state = db.outcome(tid).state
        except UnknownPending as exc:
            raise ValueError(f"no pending {tid!r}") from exc
        if state != "WAITING":
            raise ValueError(f"pending {tid} is {state}, not WAITING")
        eff = Effect(f"pend:{tid}", COCKPIT_STORE, {"fields": {"state": "WAITING"}},
                     {"state": "CANCELLED"})
        return _Built({"kind": "pending", "pending_id": tid}, [eff], [{"state": state}])

    def _build_rotate(self, db: CockpitDB, params: Mapping[str, Any], target: Mapping[str, Any]) -> _Built:
        name = target.get("name")
        if not isinstance(name, str) or params.get("token") != name:
            raise ValueError("a token_rotate names its token in the target and params alike")
        gen = db.token_generation(name)
        if gen is None:
            raise ValueError(f"no token {name!r}")
        eff = Effect(f"token:{name}", COCKPIT_STORE, {"fields": {"generation": gen}},
                     {"generation": gen + 1})
        return _Built({"kind": "token", "name": name}, [eff], [{"generation": gen}])

    def _record(self, *, pid: str, verb: str, intent: str, built: _Built, params: Mapping[str, Any],
                tier: str, fence: Mapping[str, Any], issued_at: str, expires_at: str) -> bytes:
        return encode(build_record(
            operation_id=pid, verb=verb, intent=intent,  # type: ignore[arg-type]
            target=built.target, params=dict(params), effects=[e.to_record() for e in built.effects],
            tier=tier, fence=fence, issued_at=issued_at, expires_at=expires_at))

    # -- ISSUE (§3.1) ------------------------------------------------------------------------------

    def issue(self, *, verb: str, params: Mapping[str, Any], target: Mapping[str, Any], tier: str,
              credential: Mapping[str, Any], intent: str | None = None,
              idempotency_key: str | None = None, harness: str = "cockpit") -> dict[str, Any]:
        """Record a WAITING pending for this write; ``intent`` absent is ``sign``. A ``reported``
        intent decides in this request (§3.1.3)."""
        intent = "sign" if intent is None else intent
        if intent not in ("sign", "reported"):
            raise BrokerError("bad_request", 400, f"'intent' must be sign or reported, not {intent!r}")
        if tier not in ("T2", "T3"):
            raise BrokerError("bad_request", 400, f"the broker takes T2/T3 writes, not {tier!r}")
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key):
            raise BrokerError("bad_request", 400, "'idempotency_key' must be a non-empty string")
        with self._admitted() as db:
            auth = self._request_authority(db)
            with bound(auth):
                return self._issue(db, auth, verb=verb, params=params, target=target, tier=tier,
                                   credential=credential, intent=intent, idempotency_key=idempotency_key,
                                   harness=harness)

    def _issue(self, db: CockpitDB, auth: RequestAuthority, *, verb: str, params: Mapping[str, Any],
               target: Mapping[str, Any], tier: str, credential: Mapping[str, Any], intent: str,
               idempotency_key: str | None, harness: str) -> dict[str, Any]:
        if intent == "reported":
            why = self._reported_refusal(tier, credential, harness)
            if why is not None:
                raise BrokerError("refused", 403, why)
        principal = self.principal_of(db, credential)
        replayed = self._replayed(db, verb=verb, params=params, intent=intent, credential=credential,
                                  idempotency_key=idempotency_key, target=target, harness=harness)
        if replayed is not None:
            if replayed["state"] == "applying":
                self.wake()
            return replayed
        fence = auth.fence
        if fence is None:
            raise self._authority_refusal(auth)
        pid = new_pending_id()
        try:
            built = self._build(db, verb, params, target, pid, at_issue=True)
        except TransientApplyFault as exc:
            raise BrokerError("source_unavailable", 503, f"could not read the target: {exc}") from exc
        now = self._now()
        raw = self._record(pid=pid, verb=verb, intent=intent, built=built, params=params, tier=tier,
                           fence=fence, issued_at=utc_stamp(now),
                           expires_at=utc_stamp(now + PENDING_TTL))
        try:
            res = db.issue(record_bytes=raw, principal=principal,
                           idempotency_key=idempotency_key or ("auto:" + pid), now=now,
                           harness=harness, cap_exempt=intent == "reported")
        except PendingCapExceeded as exc:
            raise BrokerError("pending_cap", 429, str(exc), {"occupying": [
                {"idempotency_key": o.idempotency_key, "verb": o.verb, "issued_at": o.issued_at}
                for o in exc.occupying]}) from exc
        except IdempotencyConflict as exc:
            raise BrokerError("idempotency_conflict", 409, str(exc)) from exc
        self._kp("issued")
        if intent == "reported" and res.outcome.state == "WAITING":
            # a reported pending is decided by the request that issued it, or by its replay
            # when that request died after ``issued``
            return self._decide(db, auth, res.pending_id, "ALLOWED_REPORTED", None, None)
        return self.result(db, res.pending_id, replay=res.replay)

    @staticmethod
    def _authority_refusal(auth: RequestAuthority) -> BrokerError:
        if auth.authority is None and not auth.log_empty:
            return BrokerError("authority_unavailable", 503, str(auth.authority_error))
        return BrokerError("not_initialised", 409, str(auth.authority_error))

    def _reported_refusal(self, tier: str, credential: Mapping[str, Any], harness: str) -> str | None:
        if self.install_class != REPORTED:
            return "a reported fire needs a reported install"
        if harness != "cockpit":
            return "ALLOWED-REPORTED is the cockpit harness's only"
        if credential.get("class") != "token":
            return "a reported fire rides a surface token; a device signs"
        if tier != "T2":
            return f"a {tier} on the token alone is refused: it needs a laptop-presence signature"
        return None

    # -- DECIDE (§3.2) ----------------------------------------------------------------------------

    def submit(self, pending_id: str, signature: Any) -> dict[str, Any]:
        """A signature over a ``sign`` pending's STORED bytes. A signature that does not verify is a
        4xx and spends nothing: the pending stays WAITING (§3.2.1)."""
        with self._admitted() as db:
            db.expire_due(self._now())
            view = self._load(db, pending_id)
            if view.state != "WAITING":
                return self.result(db, pending_id, replay=True)
            if view.record.get("intent") != "sign":
                raise BrokerError("refused", 409, "a reported pending takes no signature")
            if not isinstance(signature, str) or not signature:
                raise BrokerError("bad_request", 400, "'signature' is required")
            auth = self._request_authority(db)
            if auth.authority is None or auth.authority_error is not None:
                # the signature is unspent: the pending stays WAITING
                raise self._authority_refusal(auth)
            with bound(auth):
                fp = self._verify(auth.authority, view.record_bytes, signature)
                return self._decide(db, auth, pending_id, "ALLOWED", fp, signature)

    def _verify(self, authority: AuthoritySnapshot, message: bytes, signature: str) -> str:
        """The verified signer's fingerprint (§6.5), against a signers file generated from the
        request's authority snapshot (its unrevoked keys) for this verification into a fresh
        ``mkstemp`` file in the data root."""
        keys = sorted((k.principal, k.public_key) for k in authority.active_keys())
        fd, path = tempfile.mkstemp(prefix=".signers-", dir=self.data_root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for principal, public_key in keys:
                    fh.write(f'{principal} namespaces="{COCKPIT_NAMESPACE}" {public_key}\n')
            verdict = verify_cockpit_signature(message, signature, path,
                                               owners=frozenset({0, os.getuid()}),
                                               ssh_keygen=self.ssh_keygen)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)
        if verdict.status == UNAVAILABLE:
            raise BrokerError("verifier_unavailable", 503, "the signature could not be checked")
        if verdict.status != VERIFIED or verdict.principal is None:
            raise BrokerError("bad_signature", 400, "the signature does not verify over this pending")
        key = authority.key(verdict.fingerprint) if verdict.fingerprint is not None else None
        if key is None or not key.active or key.principal != verdict.principal:
            raise BrokerError("bad_signature", 400, "the signing key is not the enrolled key")
        assert verdict.fingerprint is not None
        return verdict.fingerprint

    def _recompute(self, db: CockpitDB, view: Any) -> tuple[bytes | None, list[Any]]:
        """§3.2.1: every effect's object read again, tier and canonical effects recomputed, the
        record rebuilt with the stored fence and times. ``(None, [])`` when a read faulted."""
        rec = view.record
        try:
            built = self._build(db, rec["verb"], rec.get("params") or {}, rec.get("target") or {},
                                view.pending_id, at_issue=False)
        except TransientApplyFault:
            return None, []
        except _Unbuildable:
            # the object moved so far the effect no longer builds
            return self._moved(rec)
        tier = rec.get("tier")
        if self.tier_of is not None and rec["verb"] not in INTERNAL_VERBS:
            # a tier that moves with the date between issue and decide differs from the record here,
            # so the op ends and the write is issued and signed again (a known edge, left as is)
            tier = self.tier_of(rec["verb"], rec.get("params") or {}, rec.get("target") or {})
            if isinstance(tier, Absent):
                return self._moved(rec)
            if tier is None or isinstance(tier, Fault):
                return None, []
        elif rec["verb"] == "pending_cancel":
            tier = self._target_tier(db, rec.get("target") or {}) or tier
        raw = self._record(pid=view.pending_id, verb=rec["verb"], intent=rec["intent"], built=built,
                           params=rec.get("params") or {}, tier=str(tier), fence=rec["fence"],
                           issued_at=rec["issued_at"], expires_at=rec["expires_at"])
        return raw, built.pre_images

    @staticmethod
    def _moved(rec: Mapping[str, Any]) -> tuple[bytes, list[Any]]:
        """A record whose every ``expect`` differs from the stored one, so the decision ends STALE
        "changed" through the one comparison (§3.2.2)."""
        fx = [dict(e, expect={"unbuildable": True}) for e in rec["effects"]]
        return encode(dict(rec, effects=fx)), [None] * len(fx)

    @staticmethod
    def _target_tier(db: CockpitDB, target: Mapping[str, Any]) -> str | None:
        try:
            return str(db.load(str(target.get("pending_id"))).record.get("tier"))
        except (UnknownPending, JournalCorruptError):
            return None

    def _decide(self, db: CockpitDB, auth: RequestAuthority, pid: str, kind: str, signer_fp: str | None,
                signature: str | None) -> dict[str, Any]:
        view = self._load(db, pid)
        if view.state != "WAITING":
            return self.result(db, pid, replay=True)
        recomputed, pre_images = self._recompute(db, view)
        self._kp("verified")
        out = db.decide(operation_id=pid, decision_kind=kind, recomputed=recomputed,  # type: ignore[arg-type]
                        now=self._now(), authority=auth.authority, signer_fp=signer_fp, signature=signature,
                        pre_images=pre_images if recomputed is not None else [])
        if out.decided:
            self._bump()
            self._kp("decided")
            # the deciding request is one of the three settlers: its own op, once (§4.2). Past the
            # commit point a failure here is the re-drive's to finish, never this request's error;
            # the thread is woken to take the op's new due time.
            if not self._settle_one(db, pid, now=self._now(), mode="own"):
                self._bump()
        elif kind == "ALLOWED" and out.state == "WAITING":
            raise BrokerError("source_unavailable", 503, "a source read faulted; the signature is unspent")
        return self.result(db, pid)

    # -- CANCEL (§3.5) and rotation (§13) ---------------------------------------------------------

    def cancel(self, pending_id: str, *, credential: Mapping[str, Any], intent: str | None = None,
               idempotency_key: str | None = None) -> dict[str, Any]:
        """C1 on the canceller's own pending; on another principal's, a pending of that pending's
        tier whose one effect cancels it (§3.5)."""
        with self._admitted() as db:
            principal = self.principal_of(db, credential)
            view = self._load(db, pending_id)
            base = principal.rpartition(":")[0] if principal.startswith("token:") else principal
            if view.principal_base == base:
                out = db.cancel(pending_id, principal, self._now())
                if out.state != "CANCELLED":
                    raise BrokerError("refused", 409, f"pending {pending_id} is {out.state}; "
                                                      "only a WAITING pending is cancelled")
                return self.result(db, pending_id)
            tier = str(view.record.get("tier"))
        return self.issue(verb="pending_cancel", params={}, target={"kind": "pending", "pending_id": pending_id},
                          tier=tier, credential=credential, intent=intent, idempotency_key=idempotency_key)

    def rotate_token(self, name: str, *, credential: Mapping[str, Any], intent: str | None = None,
                     idempotency_key: str | None = None) -> dict[str, Any]:
        """Rotation is a T3 act on ``tokens`` (§13): its one effect moves the generation to the
        absolute g+1 and cancels the old generation's WAITING pendings in one transaction."""
        return self.issue(verb="token_rotate", params={"token": name}, target={"kind": "token", "name": name},
                          tier="T3", credential=credential, intent=intent, idempotency_key=idempotency_key)

    # -- reads ------------------------------------------------------------------------------------

    def outcome(self, pending_id: str) -> dict[str, Any] | Fault:
        """A broker-backed read: a typed ``Fault`` unless ACTIVE (§4.2). A DECIDED op only wakes the
        thread; a read never settles."""
        try:
            with self._admitted() as db:
                out = self.result(db, pending_id)
        except BrokerError as exc:
            return Fault(str(exc))
        if out["state"] == "applying":
            self.wake()
        return out

    def result(self, db: CockpitDB, pid: str, *, replay: bool = False) -> dict[str, Any]:
        view = self._load(db, pid)
        state, reason = ("applying" if view.state == "DECIDED" else view.state), view.reason
        if view.state == "WAITING" and parse_stamp(view.record["expires_at"]) <= self._now():
            # expiry is a time, not a write: a read past it answers what the next write would record
            # (the same signed field ``firejournal._expire`` compares), and never hands out a record to sign
            state, reason = "EXPIRED", "expired"
        out: dict[str, Any] = {"ok": True, "pending_id": pid, "state": state, "reason": reason,
                               "verb": view.record.get("verb"), "tier": view.record.get("tier"),
                               "intent": view.record.get("intent"), "decision_kind": view.decision_kind,
                               "replay": replay}
        if view.decision_kind is not None:
            out["label"] = label_class(view.decision_kind, self.install_class)
        if state == "WAITING":
            out["record"] = view.record_bytes.decode("utf-8")
            out["expires_at"] = view.record.get("expires_at")
        return out

    @staticmethod
    def _load(db: CockpitDB, pid: str) -> Any:
        try:
            return db.load(pid)
        except UnknownPending as exc:
            raise BrokerError("not_found", 404, f"no pending {pid!r}") from exc

    # -- APPLY + FINISH: the settle pass (§3.3, §3.4, §4.2) ----------------------------------------

    def recover(self) -> int:
        """Start recovery, on demand (§4.2, §7): every DECIDED op's due units, a never-faulted unit
        due now. Returns the number of ops passed over."""
        with self._admitted() as db:
            return self._recover(self._now(), db)

    def _recover(self, now: datetime, db: CockpitDB | None = None) -> int:
        db = db or self.db
        assert db is not None
        ops = db.decided_ops()
        for pid in ops:
            self._settle_one(db, pid, now=now, mode="start")
        return len(ops)

    def _settle_one(self, db: CockpitDB, pid: str, *, now: datetime, mode: str) -> bool:
        """One op's pass, its failure kept to that op; False when it failed. A corrupt op is marked
        in the journal and leaves the due set; any other failure leaves it DECIDED for the next
        pass."""
        try:
            self._settle_pass(db, pid, now=now, mode=mode)
            return True
        except JournalCorruptError as exc:
            _log.error("cockpit broker: op %s is corrupt; it is set aside, unsettled: %s", pid, exc)
            try:
                db.mark_corrupt(pid, str(exc), now)
            except Exception:  # noqa: BLE001 - unmarked, it is tried again on the next pass
                _log.exception("cockpit broker: op %s could not be marked corrupt", pid)
        except Exception:  # noqa: BLE001 - the op stays DECIDED; the next pass tries it again
            _log.exception("cockpit broker: settling op %s failed", pid)
        return False

    @contextlib.contextmanager
    def _op_mutex(self, pid: str) -> Iterator[bool]:
        """The op's in-process mutex, taken without blocking; yields whether it was taken. The entry
        is dropped when its last user leaves, so the map holds only ops being settled."""
        with self._mu:
            entry = self._op_locks.setdefault(pid, [threading.Lock(), 0])
            entry[1] += 1
        taken = entry[0].acquire(blocking=False)
        try:
            yield taken
        finally:
            if taken:
                entry[0].release()
            with self._mu:
                entry[1] -= 1
                if entry[1] == 0:
                    del self._op_locks[pid]

    def _stores_for(self, db: CockpitDB, principal: str, now: datetime) -> dict[str, Any]:
        return {**self.stores, COCKPIT_STORE: _CockpitObjects(db, by=principal, now=now)}

    def _settle_pass(self, db: CockpitDB, pid: str, *, now: datetime, mode: str) -> None:
        """One pass over an op: every PLANNED unit in order through ``settle`` (``mode`` "own": the
        deciding request's, every unit; "start" and "thread": only units due, a never-faulted unit
        due at once at start and ``decided_at`` + 5 s for the thread), then FINISH once none is
        planned. The op's mutex is held for the pass; a held mutex skips it."""
        with self._op_mutex(pid) as taken:
            if taken:
                self._settle_locked(db, pid, now=now, mode=mode)

    def _settle_locked(self, db: CockpitDB, pid: str, *, now: datetime, mode: str) -> None:
        view = db.load(pid)
        if view.state != "DECIDED":
            return
        try:
            effects = [Effect.from_record(e) for e in view.record["effects"]]
        except ValueError as exc:
            raise JournalCorruptError(f"cockpit op {pid}: {exc}") from exc
        stores = self._stores_for(db, view.principal, now)
        rows = {r.idx: r for r in view.effects}
        for u, unit in enumerate(plan_units(effects, stores)):
            members = [rows[i] for i in unit.indices]
            if all(m.status != "PLANNED" for m in members):
                continue
            if mode != "own" and self._not_due(view, members[0], now, at_start=mode == "start"):
                continue
            if unit.store not in stores:
                db.record_unit(pid, [UnitResult(i, "UNAPPLIED", "store gone") for i in unit.indices], now)
                rows = {r.idx: r for r in db.load(pid).effects}
                continue
            prior = {i: EffectState(EffectStatus(r.status), r.native_id) for i, r in rows.items()}
            try:
                res = settle(unit, stores[unit.store], prior)
            except TransientApplyFault as fault:
                db.record_fault(pid, unit.indices, f"{fault.fault_class}: {fault.message}", now)
                self._bump()
                rows = {r.idx: r for r in db.load(pid).effects}
                continue
            if res.status is EffectStatus.PLANNED:
                continue          # waits on a still-planned unit it refers to (§4.1.2)
            self._kp(f"applied:{u}")
            db.record_unit(pid, self._unit_results(unit, res, prior), now)
            self._kp(f"journaled:{u}")
            rows = {r.idx: r for r in db.load(pid).effects}
        if all(r.status != "PLANNED" for r in rows.values()):
            db.finish(pid, now)
            self._kp("finished")

    @staticmethod
    def _not_due(view: Any, row: Any, now: datetime, *, at_start: bool) -> bool:
        if row.next_attempt_at is None and at_start:
            return False
        due = view.due_at(row)
        return due is not None and due > now

    @staticmethod
    def _unit_results(unit: ApplyUnit, res: Any, prior: Mapping[int, EffectState]) -> list[UnitResult]:
        if res.status is EffectStatus.UNAPPLIED:
            return [UnitResult(i, "UNAPPLIED", res.reason or "refused") for i in unit.indices]
        out = []
        by_idx = {m.idx: m for m in res.members}
        for i, eff in zip(unit.indices, unit.effects):
            m = by_idx[i]
            resolved = resolve_effect(eff, i, prior)
            proj = resolved.projection or {}
            labels = [(f, value_sha256(proj[f])) for f in m.label_fields if f in proj]
            out.append(UnitResult(i, "APPLIED", None, m.native_id, labels))
        return out

    # -- the re-drive thread (§4.2) ---------------------------------------------------------------

    def _redrive(self) -> None:
        while True:
            with self._mu:
                if self._state is not Lifecycle.ACTIVE:
                    return
                seen = self._generation
            due: datetime | None = None
            try:
                with self._admitted() as db:
                    now = self._now()
                    for pid in db.due_ops(now):
                        self._settle_one(db, pid, now=now, mode="thread")
                    due = db.earliest_due()
            except BrokerError:
                return
            except Exception:  # noqa: BLE001 - the thread outlives one bad pass; the backstop is 5 s
                _log.exception("cockpit broker: re-drive pass failed")
            now = self._now()
            wait = self._max_wait if due is None else (due - now).total_seconds()
            wait = min(max(wait, self._min_wait), self._max_wait)
            with self._mu:
                if self._state is not Lifecycle.ACTIVE:
                    return
                if self._generation == seen:
                    self._wake.wait(wait)
