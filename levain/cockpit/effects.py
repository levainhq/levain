"""levain.cockpit.effects: a consented effect, its apply units, the one classifier that settles
them, and the intention adapter (K2b design §0.4-0.5, §4.0-§4.1).

An ``Effect`` is one object's entry in a consent record's ``effects`` list. Execution groups
effects into APPLY UNITS, the effects one store primitive applies in one transaction (§4.0). Each
store sits behind the ``EffectStore`` protocol: a lock-free ``snapshot`` for issue and decide, and
``apply_unit``, which checks the postcondition, then the precondition, mutates, checks the
postcondition again and saves, all inside the store's own transaction (§4.1.1). ``settle`` is the
one classifier, called the same way live and in recovery (§0.5): it turns ``apply_unit``'s TYPED
result into APPLIED / UNAPPLIED, and every exception it cannot name into ``TransientApplyFault``,
so a repaired store can still take the consented write (§4.1.2); the retry policy is §4.1.2's
``record_fault``.

The intention adapter maps each intention effect to anneal's ``IntentionStore.apply(IntentionApply(...))``,
addressed by ``origin_key``, with the postcondition leaves anneal requires (§4.1.2, the anneal
mapping). Every effect it reads is an intention whose ``resource_key`` is ``intention:<origin_key>``."""

from __future__ import annotations

import copy
import enum
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from levain.intention_legacy import (  # noqa: F401  (the readers of history are re-exported: every name still resolves here)
    INTENTION_KEY_PREFIX,
    INTENTIONS,
    read_recorded_fields,
    read_recorded_key,
    read_recorded_store,
    read_recorded_target,
    read_recorded_verb,
    recorded_key_forms,
)

# --- the effect ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Effect:
    """One object a fire writes (flow design §4.5 consent record). ``expect`` is the precondition:
    ``{"version": v}``, ``{"absent": True}`` for a create, or ``{"fields": {...}}`` for a
    ``cockpit.db`` object (§4.1.4). ``changes`` is the canonical post-value of every field the fire
    changes; a value may be the symbolic ``{"from_effect": i, "field": "native_id"}``.
    ``projection`` is the authority projection, ``writes`` the authority fields this fire changes,
    ``binds`` the ones it was signed against without changing."""

    resource_key: str
    store: str
    expect: Mapping[str, Any]
    changes: Mapping[str, Any]
    projection: Mapping[str, Any] | None = None
    writes: tuple[str, ...] = ()
    binds: tuple[str, ...] = ()

    @property
    def is_create(self) -> bool:
        return self.expect.get("absent") is True

    @property
    def is_delete(self) -> bool:
        return dict(self.changes) == {"deleted": True}

    def to_record(self) -> dict[str, Any]:
        return {"resource_key": self.resource_key, "store": self.store, "expect": copy.deepcopy(dict(self.expect)),
                "changes": copy.deepcopy(dict(self.changes)),
                "projection": None if self.projection is None else copy.deepcopy(dict(self.projection)),
                "writes": list(self.writes), "binds": list(self.binds)}

    @classmethod
    def from_record(cls, rec: Mapping[str, Any]) -> "Effect":
        """The effect as the signed record holds it, read as history: a store or resource key the release that said
        "spore" signed comes back under its new name (:func:`read_recorded_store`, :func:`read_recorded_key`), so an op
        in flight across the update settles on the intention store. The record's own bytes are never touched. A malformed
        entry raises ``ValueError``: the journal treats that as a corrupt record and fails closed (§2 integrity
        framing)."""
        try:
            expect, changes, proj = rec["expect"], rec["changes"], rec.get("projection")
            if not isinstance(expect, Mapping) or not isinstance(changes, Mapping):
                raise TypeError("expect and changes must be objects")
            if proj is not None and not isinstance(proj, Mapping):
                raise TypeError("projection must be an object or null")
            if not all(isinstance(rec.get(k, []), list) for k in ("writes", "binds")):
                raise TypeError("writes and binds must be lists")
            store = read_recorded_store(str(rec["store"]))
            as_read = read_recorded_fields if store == INTENTIONS else dict
            return cls(resource_key=read_recorded_key(str(rec["resource_key"])), store=store,
                       expect=copy.deepcopy(dict(expect)), changes=copy.deepcopy(as_read(changes)),
                       projection=None if proj is None else copy.deepcopy(as_read(proj)),
                       writes=tuple(str(f) for f in rec.get("writes", ())),
                       binds=tuple(str(f) for f in rec.get("binds", ())))
        except (KeyError, TypeError) as exc:
            raise ValueError(f"malformed effect record: {exc}") from exc


@dataclass(frozen=True)
class ApplyUnit:
    """The effects one store primitive applies in one transaction (§4.0). ``indices`` are their
    positions in the record's ``effects``; a unit never spans two stores."""

    store: str
    indices: tuple[int, ...]
    effects: tuple[Effect, ...]


# --- symbolic references -------------------------------------------------------------------------


def _is_ref(value: Any) -> bool:
    return (isinstance(value, Mapping) and set(value) == {"from_effect", "field"}
            and isinstance(value["from_effect"], int) and not isinstance(value["from_effect"], bool))


def refs_of(effect: Effect) -> tuple[int, ...]:
    """The effect indices this effect's ``changes`` and ``projection`` refer to."""
    found: list[int] = []

    def walk(v: Any) -> None:
        if _is_ref(v):
            found.append(v["from_effect"])
        elif isinstance(v, Mapping):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)

    walk(effect.changes)
    walk(effect.projection)
    return tuple(sorted(set(found)))


class EffectStatus(str, enum.Enum):
    PLANNED = "PLANNED"
    APPLIED = "APPLIED"
    UNAPPLIED = "UNAPPLIED"


@dataclass(frozen=True)
class EffectState:
    """What the journal holds for an earlier effect: the input a symbolic ref resolves against."""

    status: EffectStatus
    native_id: str | None = None


class _Dependency(Exception):
    pass


class _Wait(Exception):
    pass


def resolve_effect(effect: Effect, idx: int, prior: Mapping[int, EffectState]) -> Effect:
    """``effect`` with every symbolic ref replaced by the native id its effect's APPLIED write
    assigned (§4.1.2). A ref to an UNAPPLIED effect, or one APPLIED with no native id, raises
    ``_Dependency``; a ref to a PLANNED effect raises ``_Wait``. A ref to itself, a later effect or
    a field other than ``native_id`` is a malformed plan: ``PermanentApplyRefusal``."""

    def sub(v: Any) -> Any:
        if _is_ref(v):
            j = v["from_effect"]
            if v["field"] != "native_id" or j < 0 or j >= idx:
                raise PermanentApplyRefusal(f"effect {idx}: malformed reference {dict(v)!r}")
            state = prior.get(j)
            if state is None:
                raise PermanentApplyRefusal(f"effect {idx}: reference to unknown effect {j}")
            if state.status is EffectStatus.PLANNED:
                raise _Wait(j)
            if state.status is EffectStatus.UNAPPLIED or not state.native_id:
                raise _Dependency(j)
            return state.native_id
        if isinstance(v, Mapping):
            return {k: sub(x) for k, x in v.items()}
        if isinstance(v, list):
            return [sub(x) for x in v]
        return v

    if not refs_of(effect):
        return effect
    proj = None if effect.projection is None else sub(effect.projection)
    return Effect(effect.resource_key, effect.store, effect.expect, sub(effect.changes), proj,
                  effect.writes, effect.binds)


# --- typed apply results and faults (§4.1.2) -----------------------------------------------------


class ApplyOutcome(str, enum.Enum):
    ALREADY = "ALREADY"
    APPLIED = "APPLIED"
    PRECONDITION_LOST = "PRECONDITION_LOST"


@dataclass(frozen=True)
class MemberApply:
    """One unit member as the store left it: ``native_id`` the store's id for the object, and
    ``stored`` the object after the call (None when it is absent), for the label compare."""

    native_id: str | None
    stored: Mapping[str, Any] | None


@dataclass(frozen=True)
class UnitApplyResult:
    outcome: ApplyOutcome
    members: tuple[MemberApply, ...]


class PermanentApplyRefusal(Exception):
    """The store refused the effect after its precondition held (or refused the effect itself): a
    retry fails the same way, so the unit ends UNAPPLIED ``refused``."""


class StoreGone(Exception):
    """The store's path is definitely absent: UNAPPLIED ``store gone``."""


class TransientApplyFault(Exception):
    """Anything ``settle`` cannot name. The unit stays PLANNED: a repaired store can still take the
    consented write (§4.1.2). ``fault_class`` and ``message`` are what ``record_fault`` stores."""

    def __init__(self, fault_class: str, message: str) -> None:
        super().__init__(f"{fault_class}: {message}")
        self.fault_class = fault_class
        self.message = message

    @classmethod
    def of(cls, exc: BaseException) -> "TransientApplyFault":
        return cls(type(exc).__name__, str(exc))


# --- the store protocol --------------------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    """A lock-free read of one object (§3.1.1): ``row`` None when it does not exist."""

    row: Mapping[str, Any] | None
    version: str | None


@runtime_checkable
class EffectStore(Protocol):
    """A store the broker can apply consented effects to. ``version_fields`` are the fields a
    version covers, ``authority_fields`` those a label can name (registration refuses one outside
    ``version_fields``), ``store_assigned_fields`` those the store fills at write and that never sit
    in ``changes``."""

    name: str
    version_fields: tuple[str, ...]
    authority_fields: tuple[str, ...]
    store_assigned_fields: tuple[str, ...]

    def snapshot(self, resource_key: str) -> Snapshot: ...

    def units(self, effects: Sequence[tuple[int, Effect]]) -> list[ApplyUnit]: ...

    def apply_unit(self, unit: ApplyUnit) -> UnitApplyResult: ...

    def label_fields(self, effect: Effect, stored: Mapping[str, Any] | None) -> tuple[str, ...]: ...


def registration_refusals(store: Any, canonical: Mapping[str, Callable[..., Any]] | None) -> list[str]:
    """Why a store may not back a T2/T3 verb (flow design §4.5, K2b §13 registration row); empty
    when it may."""
    out: list[str] = []
    if not isinstance(store, EffectStore):
        return ["no EffectStore adapter"]
    if not store.authority_fields:
        out.append("no authority_fields declared")
    stray = [f for f in store.authority_fields if f not in store.version_fields]
    if stray:
        out.append(f"authority fields outside version_fields: {stray}")
    if not canonical:
        out.append("no canonical_effect")
    return out


@dataclass(frozen=True)
class DownstreamEffect:
    """A downstream T2/T3 verb's broker effect (flow design §4.6, K2b §13): the ``EffectStore`` it
    applies through and its ``canonical_effect``. The broker registers ``store`` under ``store.name``
    and builds the verb's one effect as ``canonical(params, origin_key=...)``: a create on ``store``
    keyed by ``create_origin_key(pending_id, 0)``, idempotent there on that key. The build refuses a key
    already in use, so the object exists only after the apply, and the build never runs again once the
    pending is decided: recovery settles the consented effect, it does not rebuild it. ``ValueError``
    or ``ConsentError`` from ``canonical`` refuses the params; any other failure of ``canonical``, and
    any failure of ``store.snapshot``, is a transient read fault.

    The store's create is not rolled back (an HTTP insert), so recovery may find it already made:
    ``apply_unit`` answers ``ALREADY`` with the object as it is stored now, and the effect is APPLIED.
    A stored value that differs from ``changes`` is APPLIED without that field's label, never a
    refusal. A key the store reports used and since deleted is ``ALREADY`` with ``stored=None``: APPLIED,
    with no label, and nothing is created again.

    ``canonical`` is a pure function of its arguments: the decision rebuilds the effect and compares
    it with the consented one. A downstream verb binds to its panel, so an effect on an existing object
    is not built here."""

    store: EffectStore
    canonical: Callable[..., Effect]


def plan_units(effects: Sequence[Effect], stores: Mapping[str, EffectStore]) -> list[ApplyUnit]:
    """The record's effects grouped into apply units in record order: each maximal run of effects on
    one store is handed to that store's grouping (§4.0). An effect on a store no longer registered
    is its own unit, which the settler records ``store gone``."""
    out: list[ApplyUnit] = []
    run: list[tuple[int, Effect]] = []

    def flush() -> None:
        store = stores.get(run[0][1].store)
        if store is None:
            out.extend(ApplyUnit(e.store, (i,), (e,)) for i, e in run)
        else:
            out.extend(store.units(list(run)))

    for i, e in enumerate(effects):
        if run and run[-1][1].store != e.store:
            flush()
            run = []
        run.append((i, e))
    if run:
        flush()
    return out


# --- settle: the one classifier (§4.1) -----------------------------------------------------------


@dataclass(frozen=True)
class MemberSettle:
    idx: int
    native_id: str | None = None
    label_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class SettleResult:
    """``status`` is the whole unit's. PLANNED means the unit waits on a still-PLANNED effect it
    refers to; nothing was attempted."""

    status: EffectStatus
    reason: str | None
    members: tuple[MemberSettle, ...]


def _all(unit: ApplyUnit, status: EffectStatus, reason: str | None) -> SettleResult:
    return SettleResult(status, reason, tuple(MemberSettle(i) for i in unit.indices))


def settle(unit: ApplyUnit, store: EffectStore, resolved_refs: Mapping[int, EffectState]) -> SettleResult:
    """Classify one apply unit, live and in recovery the same way (§0.5, §4.1). Raises
    ``TransientApplyFault`` for every exception it cannot name; the unit then stays PLANNED."""
    try:
        effects = tuple(resolve_effect(e, i, resolved_refs) for i, e in zip(unit.indices, unit.effects))
    except _Wait:
        return _all(unit, EffectStatus.PLANNED, None)
    except _Dependency:
        return _all(unit, EffectStatus.UNAPPLIED, "dependency")
    except PermanentApplyRefusal:
        return _all(unit, EffectStatus.UNAPPLIED, "refused")
    concrete = ApplyUnit(unit.store, unit.indices, effects)
    try:
        result = store.apply_unit(concrete)
        if not isinstance(result, UnitApplyResult) or len(result.members) != len(effects):
            raise TypeError(f"apply_unit returned {result!r}")
    except TransientApplyFault:
        raise
    except PermanentApplyRefusal:
        return _all(unit, EffectStatus.UNAPPLIED, "refused")
    except StoreGone:
        return _all(unit, EffectStatus.UNAPPLIED, "store gone")
    except Exception as exc:  # noqa: BLE001 - an unnamed failure is transient by design (§4.1.2)
        if _is_anneal_refusal(exc):
            return _all(unit, EffectStatus.UNAPPLIED, "refused")
        raise TransientApplyFault.of(exc) from exc
    if result.outcome is ApplyOutcome.PRECONDITION_LOST:
        return _all(unit, EffectStatus.UNAPPLIED, "changed")
    if result.outcome not in (ApplyOutcome.ALREADY, ApplyOutcome.APPLIED):
        raise TransientApplyFault("TypeError", f"apply_unit outcome {result.outcome!r}")
    try:
        members = tuple(
            MemberSettle(i, m.native_id if e.is_create else None, store.label_fields(e, m.stored))
            for i, e, m in zip(unit.indices, effects, result.members))
    except Exception as exc:  # noqa: BLE001 - the write stands; the unit re-settles as ALREADY
        raise TransientApplyFault.of(exc) from exc
    return SettleResult(EffectStatus.APPLIED, None, members)


def _is_anneal_refusal(exc: BaseException) -> bool:
    """anneal's two refusal classes, when an adapter lets one through untranslated."""
    try:
        from anneal_memory.spores import ApplyRefused, PostconditionFailed
    except ImportError:          # pragma: no cover - anneal is a hard dependency
        return False
    return isinstance(exc, (ApplyRefused, PostconditionFailed))


# --- the intention adapter ---------------------------------------------------------------------------

# INTENTIONS and INTENTION_KEY_PREFIX are defined in ``levain.intention_legacy`` (imported above): its history readers need them
INTENTION_AUTHORITY_FIELDS = ("text", "domain", "disposition")
# anneal fills these at write; none sits in an intention effect's ``changes``
INTENTION_STORE_ASSIGNED = ("id", "seen", "created", "origin_key", "notes", "resolution.on", "resolution.at")
# the fields an update-shaped intention effect may change (anneal's update args, ``add_note`` excluded); ``target`` and
# ``attention`` reach an effect only through intention_retarget's builder (the edit verb's builder refuses both)
INTENTION_UPDATE_FIELDS = ("type", "tier", "next", "text", "salience", "domain", "pointer", "disposition", "target",
                       "attention")


def create_origin_key(pending_id: str, idx: int) -> str:
    """The ``origin_key`` of a create, from its pending's hex and its index (§2)."""
    hexpart = pending_id[2:] if pending_id.startswith("cp") else pending_id
    return f"ck.{hexpart}.{idx}"


def intention_resource_key(origin_key: str) -> str:
    return INTENTION_KEY_PREFIX + origin_key


# --- readers of history ----------------------------------------------------------------------------
# A signed record, an effects row and a label row are append-only: what the release that said "spore" wrote keeps its
# spelling, and the journal holds ops across the update. The readers (``read_recorded_store`` / ``_key`` / ``_verb`` /
# ``_fields`` / ``_target`` and ``recorded_key_forms``) are the one place a recorded old spelling becomes the new one, on
# READ. They live in ``levain.intention_legacy``, because the fire journal that reads them is root-run code and this module
# is operator-side; they are imported here so every name keeps resolving from ``levain.cockpit.effects``.


def _origin_key_of(resource_key: str) -> str:
    if not resource_key.startswith(INTENTION_KEY_PREFIX) or len(resource_key) == len(INTENTION_KEY_PREFIX):
        raise PermanentApplyRefusal(f"{resource_key!r} is not an intention resource key")
    return resource_key[len(INTENTION_KEY_PREFIX):]


def _stored_authority(row: Mapping[str, Any], f: str) -> Any:
    """An authority field as anneal stores it: a cleared domain is ``""``, a plain loop's
    disposition an absent key (None here)."""
    if f == "domain":
        return row.get("domain") or ""
    if f == "disposition":
        return row.get("disposition") or None
    return row.get(f)


_NO_CAS = object()


def _keeps_disposition(effect: Effect) -> bool:
    """An update that names the disposition the row already holds (an echo: not in ``writes``)."""
    return "disposition" in effect.changes and "disposition" not in effect.writes


class IntentionEffectStore:
    """The ``EffectStore`` over an anneal ``IntentionStore`` (§4.1.2's anneal mapping). Versions are the
    cockpit's own row version (``providers.intention_row_version``), which anneal recomputes under its
    lock as ``version_of``. Every effect is its own unit: one ``IntentionApply`` is one transaction."""

    name = INTENTIONS
    authority_fields = INTENTION_AUTHORITY_FIELDS
    store_assigned_fields = INTENTION_STORE_ASSIGNED

    def __init__(self, path: str | os.PathLike[str],
                 version_of: Callable[[Mapping[str, Any]], str] | None = None) -> None:
        from levain.cockpit.providers import INTENTION_VERSION_FIELDS, intention_row_version

        self.path = Path(path)
        self.version_fields = tuple(INTENTION_VERSION_FIELDS)
        # resolved here, before any store lock: the callback anneal runs under its lock imports nothing
        self.version_of = version_of or intention_row_version
        from levain.intentions import guarded_intention_store

        self._store = guarded_intention_store(self.path)

    # -- reads --

    def snapshot(self, resource_key: str) -> Snapshot:
        from anneal_memory.spores import IntentionError

        key = _origin_key_of(resource_key)
        try:
            row = self._store.get_by_origin_key(key) if self.path.exists() else None
            return Snapshot(row, None if row is None else self.version_of(copy.deepcopy(row)))
        except (IntentionError, OSError, ValueError, TypeError) as exc:
            raise TransientApplyFault.of(exc) from exc

    def waiting_on(self, anchor_id: str) -> list[str]:
        """The ids of the open second parts (``on_event: done:<anchor_id>``) that wait on ``anchor_id`` finishing: a read, never
        a write (``levain.split.followers_of`` over the store's open intentions)."""
        from levain.intentions import list_open_readonly
        from levain.split import followers_of

        try:
            return [str(s.get("id")) for s in followers_of(list_open_readonly(self.path), anchor_id)] if self.path.exists() else []
        except (OSError, ValueError, TypeError, RuntimeError, KeyError, AttributeError) as exc:
            raise TransientApplyFault.of(exc) from exc

    def units(self, effects: Sequence[tuple[int, Effect]]) -> list[ApplyUnit]:
        return [ApplyUnit(INTENTIONS, (i,), (e,)) for i, e in effects]

    def label_fields(self, effect: Effect, stored: Mapping[str, Any] | None) -> tuple[str, ...]:
        if stored is None or effect.projection is None:
            return ()
        return tuple(f for f in (*effect.writes, *effect.binds)
                     if f in effect.projection and _stored_authority(stored, f) == effect.projection[f])

    # -- apply --

    def intention_apply(self, effect: Effect) -> Any:
        """The ``IntentionApply`` for one concrete effect: the op from its shape, ``args`` from its
        ``changes``, and the postcondition leaves anneal requires (add/update: the fields in
        ``args``; descend: ``status`` + ``resolution.kind``; ascend: those + ``resolution.ref``;
        delete: none). An update does not write a disposition it does not change: it carries it as
        ``expect_disposition`` (anneal's CAS), out of the postcondition. ``None`` when that leaves
        nothing to write (only ``apply_unit`` calls this; it answers that case itself)."""
        from anneal_memory.spores import IntentionApply

        key = _origin_key_of(effect.resource_key)
        changes = dict(effect.changes)
        version = effect.expect.get("version")
        if effect.is_create:
            if set(effect.expect) != {"absent"}:
                raise PermanentApplyRefusal(f"a create expects only absence (got {dict(effect.expect)!r})")
            return IntentionApply(op="add", origin_key=key, args=dict(changes), postcondition=dict(changes))
        if set(effect.expect) != {"version"} or not isinstance(version, str) or not version:
            raise PermanentApplyRefusal(f"an intention effect expects a version (got {dict(effect.expect)!r})")
        guard = {"expected_version": version, "version_of": self.version_of}
        if effect.is_delete:
            return IntentionApply(op="delete", origin_key=key, **guard)
        if changes.get("status") == "resolved":
            res = changes.get("resolution")
            if set(changes) != {"status", "resolution"} or not isinstance(res, Mapping):
                raise PermanentApplyRefusal(f"a resolve changes status and resolution only (got {sorted(changes)})")
            op = res.get("direction")
            want = {"direction", "kind"} | ({"ref"} if op == "ascend" else set())
            if op not in ("ascend", "descend") or set(res) != want:
                raise PermanentApplyRefusal(f"a resolution must be {sorted(want)} (got {dict(res)!r})")
            args = {k: res[k] for k in want - {"direction"}}
            leaves: dict[Any, Any] = {"status": "resolved", ("resolution", "kind"): res["kind"]}
            if op == "ascend":
                leaves[("resolution", "ref")] = res["ref"]
            return IntentionApply(op=op, origin_key=key, args=args, postcondition=leaves, **guard)
        stray = sorted(set(changes) - set(INTENTION_UPDATE_FIELDS))
        if not changes or stray:
            raise PermanentApplyRefusal(f"an update changes {list(INTENTION_UPDATE_FIELDS)} only (got {sorted(changes)})")
        args: dict[str, Any] = {}
        if _keeps_disposition(effect):
            # the disposition the row already holds is not WRITTEN (the guarded store refuses a legacy
            # handoff/note it is handed, unchanged or not); it rides as anneal's own CAS, a compare on
            # the row inside the transaction that writes nothing. It is the stored form (an absent key
            # for a plain loop), and it is out of the postcondition (anneal: no leaf names a CAS).
            args["expect_disposition"] = changes.pop("disposition")
            if not changes:
                return None   # writes nothing: apply_unit answers it, version and CAS both checked
        return IntentionApply(op="update", origin_key=key, args={**changes, **args}, postcondition=dict(changes), **guard)

    def apply_unit(self, unit: ApplyUnit) -> UnitApplyResult:
        from anneal_memory.spores import ApplyRefused, PostconditionFailed, IntentionError

        if len(unit.effects) != 1 or unit.store != INTENTIONS:
            raise PermanentApplyRefusal(f"an intention unit is one intention effect (got {len(unit.effects)} on {unit.store!r})")
        effect = unit.effects[0]
        intention_apply = self.intention_apply(effect)
        # anneal loads a missing file as an empty store, so only a create may meet one (anneal
        # makes the file); for any other effect a missing file is a definite absence
        if not effect.is_create and (not self.path.parent.is_dir() or not self.path.exists()):
            raise StoreGone(f"{self.path} is absent")
        if intention_apply is None:
            # an update whose one field, the disposition, is the row's own: nothing to write, so nothing
            # anneal can be handed. Both guards are checked here, on one read of the store: ALREADY only
            # while the row is open, at the signed version, and still holds that disposition (the CAS,
            # compared raw as anneal compares it); anything else means the row moved.
            row = self.snapshot(effect.resource_key).row
            if (row is not None and row.get("status") != "resolved"
                    and self.version_of(copy.deepcopy(row)) == effect.expect.get("version")
                    and row.get("disposition") == effect.changes.get("disposition")):
                return UnitApplyResult(ApplyOutcome.ALREADY, (MemberApply(row.get("id"), row),))
            return UnitApplyResult(ApplyOutcome.PRECONDITION_LOST, (MemberApply(None if row is None else row.get("id"), row),))
        try:
            res = self._store.apply(intention_apply)
        except (PostconditionFailed, ApplyRefused) as exc:
            raise PermanentApplyRefusal(f"{type(exc).__name__}: {exc}") from exc
        except (IntentionError, OSError) as exc:
            raise TransientApplyFault.of(exc) from exc
        outcome = {"already": ApplyOutcome.ALREADY, "applied": ApplyOutcome.APPLIED,
                   "precondition_lost": ApplyOutcome.PRECONDITION_LOST}.get(res.outcome)
        if outcome is None:
            raise TransientApplyFault("ValueError", f"anneal apply outcome {res.outcome!r}")
        cas = dict(intention_apply.args).get("expect_disposition", _NO_CAS)
        if (outcome is ApplyOutcome.ALREADY and cas is not _NO_CAS
                and (res.spore is None or res.spore.get("disposition") != cas)):
            # anneal answers ALREADY from the written leaves before it reads its CAS, so a kept
            # disposition that is no longer the row's is checked here, on the row anneal read under
            # its lock (``res.spore``): the leaves match, the row the effect was signed on does not
            outcome = ApplyOutcome.PRECONDITION_LOST
        return UnitApplyResult(outcome, (MemberApply(res.spore_id, res.spore),))


# --- canonical_effect per intention verb (flow design §4.5) -----------------------------------------


def _norm(value: Any) -> str:
    from anneal_memory.spores import normalize_intention_field

    if not isinstance(value, str):
        raise ValueError(f"expected a string (got {value!r})")
    return normalize_intention_field(value)


def _norm_disposition(value: Any, current: Any = None) -> str | None:
    """``current``: the stored disposition. A retired value is refused only as a CHANGE: an echo of a
    legacy row's own retired value (in any spelling anneal persists as it) is accepted, and
    ``IntentionEffectStore.intention_apply`` leaves it out of the write. Any other invalid value (None
    included) is refused as not one of the write set (``WRITE_DISPOSITIONS``)."""
    from levain.intentions import (
        LOOP_DISPOSITION,
        WRITE_DISPOSITIONS,
        RetiredDisposition,
        refuse_retired_disposition,
        retired_message,
        stored_disposition,
    )

    try:
        refuse_retired_disposition(value, current)
    except RetiredDisposition as exc:
        raise ValueError(str(exc)) from exc
    if value not in WRITE_DISPOSITIONS and not (stored_disposition(value) == current and retired_message(value) is not None):
        raise ValueError(f"disposition must be one of {list(WRITE_DISPOSITIONS)} (got {value!r})")
    return None if value == LOOP_DISPOSITION else _norm(value)


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_ISO_TIMED = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")   # anneal 1.0.4: local, no zone, minute precision


def _norm_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, str) and _ISO_DATE.fullmatch(value):
        datetime.strptime(value, "%Y-%m-%d")
    elif isinstance(value, str) and _ISO_TIMED.fullmatch(value):
        datetime.strptime(value, "%Y-%m-%dT%H:%M")
    else:
        raise ValueError(f"a date must be YYYY-MM-DD, YYYY-MM-DDTHH:MM or null (got {value!r})")
    return value


def _norm_update_field(f: str, value: Any, pre: Mapping[str, Any] | None = None) -> Any:
    from anneal_memory.spores import VALID_TIERS, VALID_TYPES

    if f == "text":
        # the request's text is stripped before anneal's normaliser, which keeps leading space
        text = _norm(value.strip() if isinstance(value, str) else value)
        if not text:
            raise ValueError("text cannot be cleared to empty")
        return text
    if f == "domain":
        # None and "" clear it; anything else must be a string (a 0 or [] is refused, never a clear)
        return "" if value is None or value == "" else _norm(value)
    if f == "disposition":
        if value is None:
            return None
        return _norm_disposition(value, (pre or {}).get("disposition"))
    if f == "type":
        if value not in VALID_TYPES:
            raise ValueError(f"type must be one of {list(VALID_TYPES)} (got {value!r})")
        return value
    if f == "tier":
        if value not in VALID_TIERS:
            raise ValueError(f"tier must be one of {list(VALID_TIERS)} (got {value!r})")
        return value
    if f == "next":
        return _norm_date(value)
    if f == "pointer":
        if value is not None and not isinstance(value, str):
            raise ValueError(f"pointer must be a string or null (got {value!r})")
        return value or None
    if f == "salience":
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 3:
            raise ValueError(f"salience must be an int 0-3 (got {value!r})")
        return value
    raise ValueError(f"{f!r} is not a field an intention edit changes")


def _require_open(pre_state: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if pre_state is None:
        raise ValueError("the intention does not exist")
    if pre_state.get("status", "open") != "open":
        raise ValueError("the intention is resolved")
    if not isinstance(pre_state.get("origin_key"), str) or not pre_state.get("origin_key"):
        raise ValueError("the intention carries no origin_key, so no effect can address it")
    return pre_state


def _existing_effect(pre: Mapping[str, Any], changes: dict[str, Any], version_of: Callable[..., str]) -> Effect:
    projection = {f: _stored_authority({**pre, **changes}, f) for f in INTENTION_AUTHORITY_FIELDS}
    writes = tuple(f for f in INTENTION_AUTHORITY_FIELDS
                   if f in changes and _stored_authority(changes, f) != _stored_authority(pre, f))
    return Effect(intention_resource_key(str(pre["origin_key"])), INTENTIONS,
                  {"version": version_of(copy.deepcopy(dict(pre)))}, changes, projection, writes, ())


def _ce_seed(params: Mapping[str, Any], pre: Mapping[str, Any] | None, *, origin_key: str | None,
             version_of: Callable[..., str]) -> Effect:
    from anneal_memory.spores import VALID_TYPES
    from levain.intentions import TRAY_WRITE_DISPOSITIONS

    if pre is not None:
        raise ValueError("a create's origin_key is already in use")
    if not origin_key:
        raise ValueError("a create needs its origin_key (create_origin_key)")
    stype = params.get("type", "thought")
    if stype not in VALID_TYPES:
        raise ValueError(f"type must be one of {list(VALID_TYPES)} (got {stype!r})")
    disposition = params.get("disposition", "someday")
    from levain.intentions import RetiredDisposition, refuse_retired_disposition

    try:
        refuse_retired_disposition(disposition)
    except RetiredDisposition as exc:
        raise ValueError(str(exc)) from exc
    if disposition not in TRAY_WRITE_DISPOSITIONS:
        raise ValueError(f"a captured item's disposition must be one of {list(TRAY_WRITE_DISPOSITIONS)}")
    changes: dict[str, Any] = {"type": stype, "text": _norm_update_field("text", params.get("text")),
                               "disposition": _norm(disposition)}
    if params.get("domain"):
        changes["domain"] = _norm_update_field("domain", params["domain"])
    changes.update(_seed_steering(params, changes["disposition"]))
    projection = {f: _stored_authority(changes, f) for f in INTENTION_AUTHORITY_FIELDS}
    # a create sets EVERY authority field, an empty domain included, so it labels all of them (flow §4.5
    # invariant 5 makes the row's label its weakest field's: an unlabelled empty field would hold a row this
    # fire wrote whole at unverified). label_fields still labels only a field the store holds as projected
    writes = INTENTION_AUTHORITY_FIELDS
    return Effect(intention_resource_key(origin_key), INTENTIONS, {"absent": True}, changes, projection, writes, ())


# THE LIST (task_surfaces_1107.md): the three optional fields a create may carry. ``next`` is stored exactly as
# given, a timed minute never cut to its date; ``target``/``attention`` steer how an agenda germinates (a seed may
# carry ``attention: talk`` and nothing else). Each is refused when it does not read, never coerced
SEED_STEERING_FIELDS = ("next", "target", "attention")


def _seed_steering(params: Mapping[str, Any], disposition: str) -> dict[str, Any]:
    from levain.intentions import check_steering, valid_next

    out: dict[str, Any] = {}
    nxt = params.get("next")
    if nxt is not None:
        if not valid_next(nxt):
            raise ValueError(f"next must be YYYY-MM-DD or YYYY-MM-DDTHH:MM, a real date and clock (got {nxt!r})")
        out["next"] = nxt
    target, attention = params.get("target"), params.get("attention")
    why = check_steering(disposition, target, attention)
    if why is not None:
        raise ValueError(why)
    if target is not None:
        out["target"] = target
    if attention is not None:
        out["attention"] = attention
    return out


# --- THE LIST's one-line intake (task_surfaces_1107.md "intake grammar") ------------------------------------------
# One panel verb, ``{line}``: the server reads the line with ``levain.intake.parse_intake`` (the browser holds no
# grammar). A ``drop`` and a ``talk`` become a ``intention_seed`` effect (below) through the broker; a ``sort`` is a
# desk drop (``verbs._intake_fire``), never an intention.
INTAKE_VERB = "list_intake"
# the tier of each kind. A drop wakes the desk at its minute with the line's words as the operator's (germinate files
# them as a desk drop from surface ``cli``), so it is signed. A talk seed is an intention agents read (the talk
# queue), the exposure every ``intention_seed`` is T2 for, so it is signed too: a
# ``?`` prefix must not be a way around intention_seed's floor. A sort is the room composer's own write (a desk drop
# from the cockpit surface, "your words, kept verbatim, never authority"), C1 as that is. "Signed" is T2's gesture in a
# governed install; a REPORTED install fires a token's T2 as reported (``gesture_for``), as it does every intention_seed,
# which is that install class's declared trade. A standing raise of intention_seed governs a drop and a talk too
# (``verbs._intake_policy_also``). Pinned by the tests
INTAKE_TIERS = {"drop": "T2", "talk": "T2", "sort": "C1"}


def intake_kind_of(line: Any) -> str | None:
    """The kind a line asks for, read from its prefix alone (the tier needs no clock): ``drop`` for ``@``, ``talk``
    for ``?``, ``sort`` otherwise; None for a line that is not a string (refused, at the highest tier)."""
    if not isinstance(line, str):
        return None
    s = line.strip()
    return "drop" if s.startswith("@") else "talk" if s.startswith("?") else "sort"


def intake_seed_params(parsed: Mapping[str, Any]) -> dict[str, Any]:
    """The ``intention_seed`` params a parsed ``drop`` or ``talk`` plants. A drop is an agenda for the desk at the
    resolved minute, stored whole (``YYYY-MM-DDTHH:MM``, never cut to a date); a talk is a seed with ``attention:
    talk`` and no target. The words are the operator's, verbatim (``_ce_seed`` strips the ends only)."""
    kind = parsed.get("kind")
    if kind == "drop":
        return {"type": "task", "text": parsed["text"], "disposition": "agenda", "target": "desk",
                "next": parsed["when"]}
    if kind == "talk":
        return {"type": "thought", "text": parsed["text"], "disposition": "someday", "attention": "talk"}
    raise ValueError(f"a {kind!r} intake plants no intention")


def _no_date_on_event_agenda(pre: Mapping[str, Any], when: Any) -> None:
    """A part waits on an event or a date, never both: refuse a write that sets a (non-empty) date on a row that waits on an
    event. The same rule (``levain.intentions.date_on_event_agenda_refusal``) the legacy writes ask, so no door is a way around it."""
    from levain.intentions import date_on_event_agenda_refusal

    if when in (None, ""):
        return
    why = date_on_event_agenda_refusal(pre)
    if why is not None:
        raise ValueError(why)


def _ce_update(params: Mapping[str, Any], pre: Mapping[str, Any] | None, *, origin_key: str | None,
               version_of: Callable[..., str]) -> Effect:
    pre = _require_open(pre)
    if "next" in params:
        _no_date_on_event_agenda(pre, params["next"])
    allowed = ("text", "domain", "type", "tier", "next")
    stray = sorted(set(params) - set(allowed))
    if stray or not params:
        raise ValueError(f"intention_update changes one or more of {list(allowed)} (got {sorted(params)})")
    if "type" in params:
        from levain.intentions import is_tray

        # the reclassify gate writes._apply_intention_update holds on the legacy path: type is plastic
        # only while an item is forming in the Tray
        if not is_tray(pre):
            raise ValueError("type is only reclassifiable while an item is forming in the Tray")
    return _existing_effect(pre, {f: _norm_update_field(f, params[f], pre) for f in params}, version_of)


def _ce_retarget(params: Mapping[str, Any], pre: Mapping[str, Any] | None, *, origin_key: str | None,
                 version_of: Callable[..., str]) -> Effect:
    """The List's "who does it": set an agenda's germinate ``target``. A seed given a target becomes an agenda in the same
    write, which needs a date (``surface_at``, or the one the row already holds; a part that waits on an event needs none and
    is refused one). Only a value flow's germinate also accepts
    is written (the two halves read one store)."""
    from levain.intentions import check_steering, is_event_agenda, valid_next, valid_retarget

    pre = _require_open(pre)
    stray = sorted(set(params) - {"target", "surface_at"})
    if stray or "target" not in params:
        raise ValueError(f"intention_retarget sets target, and may carry surface_at (got {sorted(params)})")
    target = params["target"]
    if not valid_retarget(target):
        raise ValueError(f"target must be band, nudge, desk or board:<repo> (got {target!r})")
    disposition = str(pre.get("disposition") or "").strip().casefold()
    if disposition not in ("someday", "seed", "agenda"):
        raise ValueError("a target steers a seed or an agenda; this intention is neither")
    stamp = pre.get("germinated")
    if stamp:
        # germinate never reads a stamped agenda's target again: a new one would change the record and nothing else
        ref = stamp.get("ref") if isinstance(stamp, Mapping) else None
        raise ValueError(f"{pre.get('id')} already woke{f' as {ref}' if ref else ''}: who acts on it is settled there, "
                         "so its target is not changed here")
    changes: dict[str, Any] = {"target": target, "disposition": "agenda"}
    if "surface_at" in params:
        if params["surface_at"] in (None, ""):
            raise ValueError("an agenda needs a date: a target cannot clear it (leave surface_at out to keep the date)")
        _no_date_on_event_agenda(pre, params["surface_at"])
        changes["next"] = _norm_date(params["surface_at"])
    elif is_event_agenda(pre):
        # a part that waits on an event has no date and must not get one (an event or a date, never both): handing it to someone
        # else changes who acts when it wakes, and it keeps waiting on its event (germinate reads the target at the wake)
        pass
    elif not valid_next(pre.get("next")):
        raise ValueError("an agenda needs a date: give surface_at with the target")
    attention = pre.get("attention")
    if disposition in ("someday", "seed") and attention == "talk":
        # talk is a seed's steering (the talk queue reads seeds only): the seed that becomes an agenda leaves it behind
        changes["attention"] = None
        attention = None
    why = check_steering("agenda", target, attention)
    if why is not None:
        raise ValueError(why)
    return _existing_effect(pre, changes, version_of)


def retarget_to_machine(params: Mapping[str, Any]) -> bool:
    """Whether ``intention_retarget`` is asked to hand the row to the machine (the desk or a board) with a target the verb allows. Asked
    BEFORE anything is read for the stranding rule, so a retarget to the band or a nudge, or one the verb itself refuses, never
    reads the store for waiting parts."""
    from levain.intentions import executor_of, valid_retarget

    target = params.get("target")
    return valid_retarget(target) and executor_of(target) == "machine"


def retarget_strands_refusal(params: Mapping[str, Any], pre: Mapping[str, Any] | None, waiting: Sequence[str]) -> str | None:
    """Why ``intention_retarget`` may not hand ``pre`` to the desk or a board while ``waiting`` (the ids of the open second parts
    that wait on it finishing) is not empty; ``None`` when it may. The split's one invariant, which ``levain.split.create_then``
    holds at link time: a machine part is closed when it is handed off, so nothing could mark it done and the part waiting on it
    would never wake. The effect sees only the one row, so the caller that holds the store reads ``waiting`` and asks."""
    if not waiting or pre is None or not retarget_to_machine(params):
        return None
    names = ", ".join(waiting[:3]) + (" ..." if len(waiting) > 3 else "")
    return (f"{pre.get('id')} waits to be marked done by you: {names} waits on it. Handed to the desk or a board it would be "
            "closed when handed off, so nothing could mark it done and that part would never wake. Finish or drop what waits "
            "on it first, or keep it yours.")


def _ce_set_disposition(params: Mapping[str, Any], pre: Mapping[str, Any] | None, *,
                        origin_key: str | None, version_of: Callable[..., str]) -> Effect:
    pre = _require_open(pre)
    disposition = params.get("disposition")
    changes: dict[str, Any] = {"disposition": _norm_disposition(disposition, pre.get("disposition"))}
    surface_at = _norm_date(params.get("surface_at")) if "surface_at" in params else None
    # a move into the Keep is refused above (``_norm_disposition``): the Keep is retired
    if "surface_at" in params:
        _no_date_on_event_agenda(pre, surface_at)
        changes["next"] = surface_at
    return _existing_effect(pre, changes, version_of)


def _ce_surface_at(params: Mapping[str, Any], pre: Mapping[str, Any] | None, *, origin_key: str | None,
                   version_of: Callable[..., str]) -> Effect:
    pre = _require_open(pre)
    if "surface_at" not in params:
        raise ValueError("surface_at is required (null clears it)")
    _no_date_on_event_agenda(pre, params["surface_at"])
    return _existing_effect(pre, {"next": _norm_date(params["surface_at"])}, version_of)


def _ce_resolve(direction: str) -> Callable[..., Effect]:
    def build(params: Mapping[str, Any], pre: Mapping[str, Any] | None, *, origin_key: str | None,
              version_of: Callable[..., str]) -> Effect:
        from anneal_memory.spores import ASCEND_BY_TYPE, DESCEND_BY_TYPE
        from levain.intentions import is_note

        pre = _require_open(pre)
        kind = params.get("intention_kind")
        valid = (ASCEND_BY_TYPE if direction == "ascend" else DESCEND_BY_TYPE).get(str(pre.get("type")), frozenset())
        if kind not in valid:
            raise ValueError(f"{direction} kind {kind!r} is invalid for a {pre.get('type')!r} intention")
        resolution: dict[str, Any] = {"direction": direction, "kind": kind}
        if direction == "ascend":
            if is_note(pre):
                raise ValueError("a Keep note is durable reference; it can't be ascended")
            ref = params.get("ref")
            if _is_ref(ref):
                resolution["ref"] = {"from_effect": ref["from_effect"], "field": ref["field"]}
            elif isinstance(ref, str) and ref.strip():
                resolution["ref"] = ref.strip()
            else:
                raise ValueError("ascend requires a ref")
        return _existing_effect(pre, {"status": "resolved", "resolution": resolution}, version_of)
    return build


def _ce_undo_edit(params: Mapping[str, Any], pre: Mapping[str, Any] | None, *, origin_key: str | None,
                  version_of: Callable[..., str]) -> Effect:
    """``params["restore"]``: the field values the reversed edit replaced. Known edge: an undo that
    restores a RETIRED disposition (a legacy handoff/note the edit had re-dispositioned) is refused,
    since the current row no longer holds it, so it is a CHANGE into a retired value. No UI builds
    ``intention_undo_edit`` for a disposition."""
    pre = _require_open(pre)
    restore = params.get("restore")
    if not isinstance(restore, Mapping) or not restore:
        raise ValueError("an undo restores at least one field")
    return _existing_effect(pre, {f: _norm_update_field(f, v, pre) for f, v in restore.items()}, version_of)


def _ce_undo_seed(params: Mapping[str, Any], pre: Mapping[str, Any] | None, *, origin_key: str | None,
                  version_of: Callable[..., str]) -> Effect:
    if pre is None:
        raise ValueError("the intention does not exist")
    if not isinstance(pre.get("origin_key"), str) or not pre.get("origin_key"):
        raise ValueError("the intention carries no origin_key, so no effect can address it")
    return Effect(intention_resource_key(str(pre["origin_key"])), INTENTIONS,
                  {"version": version_of(copy.deepcopy(dict(pre)))}, {"deleted": True}, None, (), ())


INTENTION_CANONICAL: dict[str, Callable[..., Effect]] = {
    "intention_seed": _ce_seed,
    "intention_update": _ce_update,
    "intention_set_disposition": _ce_set_disposition,
    "intention_surface_at": _ce_surface_at,
    "intention_retarget": _ce_retarget,
    "intention_close": _ce_resolve("descend"),
    "intention_promote": _ce_resolve("ascend"),
    "intention_undo_edit": _ce_undo_edit,
    "intention_undo_seed": _ce_undo_seed,
}


def canonical_effect(verb: str, params: Mapping[str, Any], pre_state: Mapping[str, Any] | None, *,
                     origin_key: str | None = None,
                     version_of: Callable[[Mapping[str, Any]], str] | None = None) -> Effect:
    """The effect an intention verb's params make of ``pre_state`` (the snapshot row, None for a create),
    every stored value computed by anneal's exported normaliser. ``origin_key`` names a create.
    ``ValueError`` for params the verb refuses; the broker records it as REFUSED."""
    if verb not in INTENTION_CANONICAL:
        raise ValueError(f"no canonical_effect for {verb!r}")
    if version_of is None:
        from levain.cockpit.providers import intention_row_version
        version_of = intention_row_version
    return INTENTION_CANONICAL[verb](dict(params), pre_state, origin_key=origin_key, version_of=version_of)
