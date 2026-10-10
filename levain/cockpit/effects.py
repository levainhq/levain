"""levain.cockpit.effects: a consented effect, its apply units, the one classifier that settles
them, and the spore adapter (K2b design §0.4-0.5, §4.0-§4.1).

An ``Effect`` is one object's entry in a consent record's ``effects`` list. Execution groups
effects into APPLY UNITS, the effects one store primitive applies in one transaction (§4.0). Each
store sits behind the ``EffectStore`` protocol: a lock-free ``snapshot`` for issue and decide, and
``apply_unit``, which checks the postcondition, then the precondition, mutates, checks the
postcondition again and saves, all inside the store's own transaction (§4.1.1). ``settle`` is the
one classifier, called the same way live and in recovery (§0.5): it turns ``apply_unit``'s TYPED
result into APPLIED / UNAPPLIED, and every exception it cannot name into ``TransientApplyFault``,
so a repaired store can still take the consented write (§4.1.2); the retry policy is §4.1.2's
``record_fault``.

The spore adapter maps each spore effect to anneal's ``SporeStore.apply(SporeApply(...))``,
addressed by ``origin_key``, with the postcondition leaves anneal requires (§4.1.2, the anneal
mapping). Every effect it reads is a spore whose ``resource_key`` is ``spore:<origin_key>``."""

from __future__ import annotations

import copy
import enum
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

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
        """The effect as the signed record holds it. A malformed entry raises ``ValueError``: the
        journal treats that as a corrupt record and fails closed (§2 integrity framing)."""
        try:
            expect, changes, proj = rec["expect"], rec["changes"], rec.get("projection")
            if not isinstance(expect, Mapping) or not isinstance(changes, Mapping):
                raise TypeError("expect and changes must be objects")
            if proj is not None and not isinstance(proj, Mapping):
                raise TypeError("projection must be an object or null")
            if not all(isinstance(rec.get(k, []), list) for k in ("writes", "binds")):
                raise TypeError("writes and binds must be lists")
            return cls(resource_key=str(rec["resource_key"]), store=str(rec["store"]),
                       expect=copy.deepcopy(dict(expect)), changes=copy.deepcopy(dict(changes)),
                       projection=None if proj is None else copy.deepcopy(dict(proj)),
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


def plan_units(effects: Sequence[Effect], stores: Mapping[str, EffectStore]) -> list[ApplyUnit]:
    """The record's effects grouped into apply units in record order: each maximal run of effects on
    one store is handed to that store's grouping (§4.0). An effect on an unregistered store raises
    ``KeyError``."""
    out: list[ApplyUnit] = []
    run: list[tuple[int, Effect]] = []
    for i, e in enumerate(effects):
        if run and run[-1][1].store != e.store:
            out.extend(stores[run[0][1].store].units(run))
            run = []
        run.append((i, e))
    if run:
        out.extend(stores[run[0][1].store].units(run))
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


# --- the spore adapter ---------------------------------------------------------------------------

SPORES = "spores"
SPORE_KEY_PREFIX = "spore:"
SPORE_AUTHORITY_FIELDS = ("text", "domain", "disposition")
# anneal fills these at write; none sits in a spore effect's ``changes``
SPORE_STORE_ASSIGNED = ("id", "seen", "created", "origin_key", "notes", "resolution.on", "resolution.at")
# the fields an update-shaped spore effect may change (anneal's update args, ``add_note`` excluded)
SPORE_UPDATE_FIELDS = ("type", "tier", "next", "text", "salience", "domain", "pointer", "disposition")


def create_origin_key(pending_id: str, idx: int) -> str:
    """The ``origin_key`` of a create, from its pending's hex and its index (§2)."""
    hexpart = pending_id[2:] if pending_id.startswith("cp") else pending_id
    return f"ck.{hexpart}.{idx}"


def spore_resource_key(origin_key: str) -> str:
    return SPORE_KEY_PREFIX + origin_key


def _origin_key_of(resource_key: str) -> str:
    if not resource_key.startswith(SPORE_KEY_PREFIX) or len(resource_key) == len(SPORE_KEY_PREFIX):
        raise PermanentApplyRefusal(f"{resource_key!r} is not a spore resource key")
    return resource_key[len(SPORE_KEY_PREFIX):]


def _stored_authority(row: Mapping[str, Any], f: str) -> Any:
    """An authority field as anneal stores it: a cleared domain is ``""``, a plain loop's
    disposition an absent key (None here)."""
    if f == "domain":
        return row.get("domain") or ""
    if f == "disposition":
        return row.get("disposition") or None
    return row.get(f)


class SporeEffectStore:
    """The ``EffectStore`` over an anneal ``SporeStore`` (§4.1.2's anneal mapping). Versions are the
    cockpit's own row version (``providers.spore_row_version``), which anneal recomputes under its
    lock as ``version_of``. Every effect is its own unit: one ``SporeApply`` is one transaction."""

    name = SPORES
    authority_fields = SPORE_AUTHORITY_FIELDS
    store_assigned_fields = SPORE_STORE_ASSIGNED

    def __init__(self, path: str | os.PathLike[str],
                 version_of: Callable[[Mapping[str, Any]], str] | None = None) -> None:
        from anneal_memory.spores import SporeStore
        from levain.cockpit.providers import SPORE_VERSION_FIELDS, spore_row_version

        self.path = Path(path)
        self.version_fields = tuple(SPORE_VERSION_FIELDS)
        # resolved here, before any store lock: the callback anneal runs under its lock imports nothing
        self.version_of = version_of or spore_row_version
        self._store = SporeStore(self.path)

    # -- reads --

    def snapshot(self, resource_key: str) -> Snapshot:
        from anneal_memory.spores import SporeError

        key = _origin_key_of(resource_key)
        try:
            row = self._store.get_by_origin_key(key) if self.path.exists() else None
            return Snapshot(row, None if row is None else self.version_of(copy.deepcopy(row)))
        except (SporeError, OSError, ValueError, TypeError) as exc:
            raise TransientApplyFault.of(exc) from exc

    def units(self, effects: Sequence[tuple[int, Effect]]) -> list[ApplyUnit]:
        return [ApplyUnit(SPORES, (i,), (e,)) for i, e in effects]

    def label_fields(self, effect: Effect, stored: Mapping[str, Any] | None) -> tuple[str, ...]:
        if stored is None or effect.projection is None:
            return ()
        return tuple(f for f in (*effect.writes, *effect.binds)
                     if f in effect.projection and _stored_authority(stored, f) == effect.projection[f])

    # -- apply --

    def spore_apply(self, effect: Effect) -> Any:
        """The ``SporeApply`` for one concrete effect: the op from its shape, ``args`` from its
        ``changes``, and the postcondition leaves anneal requires (add/update: the fields in
        ``args``; descend: ``status`` + ``resolution.kind``; ascend: those + ``resolution.ref``;
        delete: none)."""
        from anneal_memory.spores import SporeApply

        key = _origin_key_of(effect.resource_key)
        changes = dict(effect.changes)
        version = effect.expect.get("version")
        if effect.is_create:
            if set(effect.expect) != {"absent"}:
                raise PermanentApplyRefusal(f"a create expects only absence (got {dict(effect.expect)!r})")
            return SporeApply(op="add", origin_key=key, args=dict(changes), postcondition=dict(changes))
        if set(effect.expect) != {"version"} or not isinstance(version, str) or not version:
            raise PermanentApplyRefusal(f"a spore effect expects a version (got {dict(effect.expect)!r})")
        guard = {"expected_version": version, "version_of": self.version_of}
        if effect.is_delete:
            return SporeApply(op="delete", origin_key=key, **guard)
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
            return SporeApply(op=op, origin_key=key, args=args, postcondition=leaves, **guard)
        stray = sorted(set(changes) - set(SPORE_UPDATE_FIELDS))
        if not changes or stray:
            raise PermanentApplyRefusal(f"an update changes {list(SPORE_UPDATE_FIELDS)} only (got {sorted(changes)})")
        return SporeApply(op="update", origin_key=key, args=dict(changes), postcondition=dict(changes), **guard)

    def apply_unit(self, unit: ApplyUnit) -> UnitApplyResult:
        from anneal_memory.spores import ApplyRefused, PostconditionFailed, SporeError

        if len(unit.effects) != 1 or unit.store != SPORES:
            raise PermanentApplyRefusal(f"a spore unit is one spore effect (got {len(unit.effects)} on {unit.store!r})")
        effect = unit.effects[0]
        spore_apply = self.spore_apply(effect)
        # anneal loads a missing file as an empty store (and a create would make one), so the
        # definite absence is checked here, before its lock
        if not self.path.parent.is_dir() or not self.path.exists():
            raise StoreGone(f"{self.path} is absent")
        try:
            res = self._store.apply(spore_apply)
        except (PostconditionFailed, ApplyRefused) as exc:
            raise PermanentApplyRefusal(f"{type(exc).__name__}: {exc}") from exc
        except (SporeError, OSError) as exc:
            raise TransientApplyFault.of(exc) from exc
        outcome = {"already": ApplyOutcome.ALREADY, "applied": ApplyOutcome.APPLIED,
                   "precondition_lost": ApplyOutcome.PRECONDITION_LOST}.get(res.outcome)
        if outcome is None:
            raise TransientApplyFault("ValueError", f"anneal apply outcome {res.outcome!r}")
        return UnitApplyResult(outcome, (MemberApply(res.spore_id, res.spore),))


# --- canonical_effect per spore verb (flow design §4.5) -----------------------------------------


def _norm(value: Any) -> str:
    from anneal_memory.spores import normalize_spore_field

    if not isinstance(value, str):
        raise ValueError(f"expected a string (got {value!r})")
    return normalize_spore_field(value)


def _norm_disposition(value: Any) -> str | None:
    from levain.spores import LOOP_DISPOSITION, VALID_DISPOSITIONS

    if value not in VALID_DISPOSITIONS:
        raise ValueError(f"disposition must be one of {list(VALID_DISPOSITIONS)} (got {value!r})")
    return None if value == LOOP_DISPOSITION else _norm(value)


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _norm_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        raise ValueError(f"a date must be YYYY-MM-DD or null (got {value!r})")
    datetime.strptime(value, "%Y-%m-%d")
    return value


def _norm_update_field(f: str, value: Any) -> Any:
    from anneal_memory.spores import VALID_TIERS, VALID_TYPES

    if f == "text":
        # the request's text is stripped before anneal's normaliser, which keeps leading space
        text = _norm(value.strip() if isinstance(value, str) else value)
        if not text:
            raise ValueError("text cannot be cleared to empty")
        return text
    if f == "domain":
        return _norm(value) if value else ""
    if f == "disposition":
        if value is None:
            return None
        return _norm_disposition(value)
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
    raise ValueError(f"{f!r} is not a field a spore edit changes")


def _require_open(pre_state: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if pre_state is None:
        raise ValueError("the spore does not exist")
    if pre_state.get("status", "open") != "open":
        raise ValueError("the spore is resolved")
    if not isinstance(pre_state.get("origin_key"), str) or not pre_state.get("origin_key"):
        raise ValueError("the spore carries no origin_key, so no effect can address it")
    return pre_state


def _existing_effect(pre: Mapping[str, Any], changes: dict[str, Any], version_of: Callable[..., str]) -> Effect:
    projection = {f: _stored_authority({**pre, **changes}, f) for f in SPORE_AUTHORITY_FIELDS}
    writes = tuple(f for f in SPORE_AUTHORITY_FIELDS
                   if f in changes and _stored_authority(changes, f) != _stored_authority(pre, f))
    return Effect(spore_resource_key(str(pre["origin_key"])), SPORES,
                  {"version": version_of(copy.deepcopy(dict(pre)))}, changes, projection, writes, ())


def _ce_seed(params: Mapping[str, Any], pre: Mapping[str, Any] | None, *, origin_key: str | None,
             version_of: Callable[..., str]) -> Effect:
    from anneal_memory.spores import VALID_TYPES
    from levain.spores import NON_COGNITION_DISPOSITIONS

    if pre is not None:
        raise ValueError("a create's origin_key is already in use")
    if not origin_key:
        raise ValueError("a create needs its origin_key (create_origin_key)")
    stype = params.get("type", "thought")
    if stype not in VALID_TYPES:
        raise ValueError(f"type must be one of {list(VALID_TYPES)} (got {stype!r})")
    disposition = params.get("disposition", "seed")
    if disposition not in NON_COGNITION_DISPOSITIONS:
        raise ValueError(f"a captured item's disposition must be one of {list(NON_COGNITION_DISPOSITIONS)}")
    changes: dict[str, Any] = {"type": stype, "text": _norm_update_field("text", params.get("text")),
                               "disposition": _norm(disposition)}
    if params.get("domain"):
        changes["domain"] = _norm_update_field("domain", params["domain"])
    projection = {f: _stored_authority(changes, f) for f in SPORE_AUTHORITY_FIELDS}
    writes = tuple(f for f in SPORE_AUTHORITY_FIELDS if projection[f] not in (None, ""))
    return Effect(spore_resource_key(origin_key), SPORES, {"absent": True}, changes, projection, writes, ())


def _ce_update(params: Mapping[str, Any], pre: Mapping[str, Any] | None, *, origin_key: str | None,
               version_of: Callable[..., str]) -> Effect:
    pre = _require_open(pre)
    allowed = ("text", "domain", "type", "tier", "next")
    stray = sorted(set(params) - set(allowed))
    if stray or not params:
        raise ValueError(f"spore_update changes one or more of {list(allowed)} (got {sorted(params)})")
    return _existing_effect(pre, {f: _norm_update_field(f, params[f]) for f in params}, version_of)


def _ce_set_disposition(params: Mapping[str, Any], pre: Mapping[str, Any] | None, *,
                        origin_key: str | None, version_of: Callable[..., str]) -> Effect:
    pre = _require_open(pre)
    disposition = params.get("disposition")
    changes: dict[str, Any] = {"disposition": _norm_disposition(disposition)}
    surface_at = _norm_date(params.get("surface_at")) if "surface_at" in params else None
    # a Keep note carries no surface date, so a move into Keep clears it
    if disposition == "note":
        if surface_at is not None:
            raise ValueError("a Keep note doesn't resurface on a schedule")
        changes["next"] = None
    elif "surface_at" in params:
        changes["next"] = surface_at
    return _existing_effect(pre, changes, version_of)


def _ce_surface_at(params: Mapping[str, Any], pre: Mapping[str, Any] | None, *, origin_key: str | None,
                   version_of: Callable[..., str]) -> Effect:
    pre = _require_open(pre)
    if "surface_at" not in params:
        raise ValueError("surface_at is required (null clears it)")
    return _existing_effect(pre, {"next": _norm_date(params["surface_at"])}, version_of)


def _ce_resolve(direction: str) -> Callable[..., Effect]:
    def build(params: Mapping[str, Any], pre: Mapping[str, Any] | None, *, origin_key: str | None,
              version_of: Callable[..., str]) -> Effect:
        from anneal_memory.spores import ASCEND_BY_TYPE, DESCEND_BY_TYPE
        from levain.spores import is_note

        pre = _require_open(pre)
        kind = params.get("spore_kind")
        valid = (ASCEND_BY_TYPE if direction == "ascend" else DESCEND_BY_TYPE).get(str(pre.get("type")), frozenset())
        if kind not in valid:
            raise ValueError(f"{direction} kind {kind!r} is invalid for a {pre.get('type')!r} spore")
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
    """``params["restore"]``: the field values the reversed edit replaced."""
    pre = _require_open(pre)
    restore = params.get("restore")
    if not isinstance(restore, Mapping) or not restore:
        raise ValueError("an undo restores at least one field")
    return _existing_effect(pre, {f: _norm_update_field(f, v) for f, v in restore.items()}, version_of)


def _ce_undo_seed(params: Mapping[str, Any], pre: Mapping[str, Any] | None, *, origin_key: str | None,
                  version_of: Callable[..., str]) -> Effect:
    if pre is None:
        raise ValueError("the spore does not exist")
    if not isinstance(pre.get("origin_key"), str) or not pre.get("origin_key"):
        raise ValueError("the spore carries no origin_key, so no effect can address it")
    return Effect(spore_resource_key(str(pre["origin_key"])), SPORES,
                  {"version": version_of(copy.deepcopy(dict(pre)))}, {"deleted": True}, None, (), ())


SPORE_CANONICAL: dict[str, Callable[..., Effect]] = {
    "spore_seed": _ce_seed,
    "spore_update": _ce_update,
    "spore_set_disposition": _ce_set_disposition,
    "spore_surface_at": _ce_surface_at,
    "spore_descend": _ce_resolve("descend"),
    "spore_ascend": _ce_resolve("ascend"),
    "spore_undo_edit": _ce_undo_edit,
    "spore_undo_seed": _ce_undo_seed,
}


def canonical_effect(verb: str, params: Mapping[str, Any], pre_state: Mapping[str, Any] | None, *,
                     origin_key: str | None = None,
                     version_of: Callable[[Mapping[str, Any]], str] | None = None) -> Effect:
    """The effect a spore verb's params make of ``pre_state`` (the snapshot row, None for a create),
    every stored value computed by anneal's exported normaliser. ``origin_key`` names a create.
    ``ValueError`` for params the verb refuses; the broker records it as REFUSED."""
    if verb not in SPORE_CANONICAL:
        raise ValueError(f"no canonical_effect for {verb!r}")
    if version_of is None:
        from levain.cockpit.providers import spore_row_version
        version_of = spore_row_version
    return SPORE_CANONICAL[verb](dict(params), pre_state, origin_key=origin_key, version_of=version_of)
