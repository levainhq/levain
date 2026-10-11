"""levain.cockpit.anneal_effects: the ``EffectStore`` adapters over anneal's episodic ``Store`` (K2b
design §4.0, §4.1.2's anneal mapping).

The episode tombstone. A tombstone is a CREATE of ``tomb:<origin_key>`` with a second, precondition-only
effect on the episode (``expect: {version}``, empty ``changes``), applied together as ONE unit by
anneal's ``Store.delete_by_origin_key`` in one transaction (flow manifest design §4.5, K2b §4.0). The
episode's version is anneal's own (``read_episode_versioned``), the one ``delete_by_origin_key``
checks under its lock; the version a panel rendered is levain's (``episode_row_version``) and is
checked at issue against the same snapshot. The tomb's ``changes`` carry the operation's own
``effect_id``, so a retry of this operation reads ``already_applied`` and another operation's delete
reads ``deleted_by_other``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from levain.cockpit.effects import (
    ApplyOutcome,
    ApplyUnit,
    Effect,
    MemberApply,
    PermanentApplyRefusal,
    Snapshot,
    StoreGone,
    TransientApplyFault,
    UnitApplyResult,
)

EPISODES = "episodes"
EPISODE_KEY_PREFIX = "episode:"
TOMB_KEY_PREFIX = "tomb:"
TOMBSTONE_VERB = "episode_tombstone"


def episode_resource_key(origin_key: str) -> str:
    return EPISODE_KEY_PREFIX + origin_key


def tomb_resource_key(origin_key: str) -> str:
    return TOMB_KEY_PREFIX + origin_key


def _key_after(prefix: str, resource_key: str) -> str:
    if not resource_key.startswith(prefix) or len(resource_key) == len(prefix):
        raise PermanentApplyRefusal(f"{resource_key!r} is not a {prefix.rstrip(':')} resource key")
    return resource_key[len(prefix):]


def _read_faults() -> tuple[type[BaseException], ...]:
    """Every way an anneal read fails that a retry can outlive. ``RecursionError``: anneal parses an
    episode's ``metadata`` with ``json.loads``, and a deeply nested one raises it (L1 r1 #1)."""
    from anneal_memory import AnnealMemoryError

    return (AnnealMemoryError, OSError, ValueError, RecursionError)


def _episode_dict(ep: Any) -> dict[str, Any]:
    return {"id": ep.id, "timestamp": ep.timestamp, "type": getattr(ep.type, "value", ep.type),
            "source": ep.source, "content": ep.content, "metadata": ep.metadata,
            "origin_key": ep.origin_key}


@dataclass(frozen=True)
class EpisodeRead:
    """One episode read in ONE anneal snapshot: its row, anneal's version (the store's precondition)
    and the version the episodes panel renders for it."""

    row: dict[str, Any]
    version: str
    rendered: str


def tombstone_effects(origin_key: str, version: str, *, effect_id: str) -> list[Effect]:
    """The two effects of one tombstone: the ``tomb:`` create, then the episode's precondition."""
    if not isinstance(version, str) or not version:
        raise ValueError("a tombstone needs the episode's version")
    tomb = Effect(tomb_resource_key(origin_key), EPISODES, {"absent": True}, {"effect_id": effect_id},
                  {"origin_key": origin_key}, ("origin_key",), ())
    episode = Effect(episode_resource_key(origin_key), EPISODES, {"version": version}, {}, None, (), ())
    return [tomb, episode]


# registration's canonical_effect check (K2b §13): the verb this store backs and its builder
EPISODE_CANONICAL = {TOMBSTONE_VERB: tombstone_effects}


def _is_tomb(e: Effect) -> bool:
    return e.resource_key.startswith(TOMB_KEY_PREFIX)


def _is_episode(e: Effect) -> bool:
    return e.resource_key.startswith(EPISODE_KEY_PREFIX)


class EpisodeEffectStore:
    """The ``EffectStore`` over an anneal episodic store at ``db``. Each call opens its own ``Store``
    (the broker settles from request threads and its re-drive thread)."""

    name = EPISODES
    # anneal's episode version covers every row that names the episode; origin_key is immutable
    version_fields = ("id", "timestamp", "type", "source", "content", "metadata", "origin_key")
    authority_fields = ("origin_key",)
    store_assigned_fields = ("id",)

    def __init__(self, db: str | Path) -> None:
        self.db = Path(db)

    def _store(self, *, read_only: bool) -> Any:
        from anneal_memory import Store

        return Store(str(self.db), read_only=read_only)

    # -- reads --

    def read(self, origin_key: str) -> EpisodeRead | None:
        """The live episode carrying ``origin_key``, or None. ``TransientApplyFault`` for a read that
        faulted."""
        from levain.cockpit.providers import episode_row_version

        if not self.db.is_file():
            return None
        try:
            with self._store(read_only=True) as store:
                got = store.read_episode_versioned(origin_key)
            if got is None:
                return None
            ep, version = got
            return EpisodeRead(_episode_dict(ep), version, episode_row_version(ep))
        except _read_faults() as exc:
            raise TransientApplyFault.of(exc) from exc

    def origin_key_of(self, episode_id: str) -> tuple[str | None, str]:
        """The live episode ``episode_id``'s ``origin_key`` and ``source``; ``KeyError`` when there
        is none."""
        if not self.db.is_file():
            raise KeyError(episode_id)
        try:
            with self._store(read_only=True) as store:
                ep = store.get(episode_id)
        except _read_faults() as exc:
            raise TransientApplyFault.of(exc) from exc
        if ep is None:
            raise KeyError(episode_id)
        ok = ep.origin_key
        return (ok if isinstance(ok, str) and ok else None), str(ep.source)

    def snapshot(self, resource_key: str) -> Snapshot:
        """An episode's snapshot. A tomb has none: who retired a key is known only to the
        ``apply_unit`` that compares its own ``effect_id`` (L1 r1 #4, L2 r1 B)."""
        got = self.read(_key_after(EPISODE_KEY_PREFIX, resource_key))
        return Snapshot(None, None) if got is None else Snapshot(got.row, got.version)

    def units(self, effects: Sequence[tuple[int, Effect]]) -> list[ApplyUnit]:
        """Each ``tomb:`` create with the episode effect after it is one unit; anything else is its
        own unit, which ``apply_unit`` refuses."""
        out: list[ApplyUnit] = []
        i = 0
        while i < len(effects):
            if i + 1 < len(effects) and _is_tomb(effects[i][1]) and _is_episode(effects[i + 1][1]):
                (a, ea), (b, eb) = effects[i], effects[i + 1]
                out.append(ApplyUnit(EPISODES, (a, b), (ea, eb)))
                i += 2
            else:
                out.append(ApplyUnit(EPISODES, (effects[i][0],), (effects[i][1],)))
                i += 1
        return out

    def label_fields(self, effect: Effect, stored: Mapping[str, Any] | None) -> tuple[str, ...]:
        if stored is None or effect.projection is None or not _is_tomb(effect):
            return ()
        return tuple(f for f in effect.writes
                     if f in effect.projection and stored.get(f) == effect.projection[f])

    # -- apply --

    @staticmethod
    def _plan(unit: ApplyUnit) -> tuple[str, str, str]:
        """``(origin_key, expected_version, effect_id)`` of a well-formed tombstone unit, else
        ``PermanentApplyRefusal``."""
        if unit.store != EPISODES or len(unit.effects) != 2:
            raise PermanentApplyRefusal("an episode unit is one tomb: create and its episode precondition")
        tomb, ep = unit.effects
        ok = _key_after(TOMB_KEY_PREFIX, tomb.resource_key)
        if _key_after(EPISODE_KEY_PREFIX, ep.resource_key) != ok:
            raise PermanentApplyRefusal("the tomb and its episode name different origin keys")
        if dict(tomb.expect) != {"absent": True} or set(tomb.changes) != {"effect_id"}:
            raise PermanentApplyRefusal(f"a tomb is a create carrying its effect_id (got {dict(tomb.changes)!r})")
        effect_id = tomb.changes["effect_id"]
        version = ep.expect.get("version")
        if set(ep.expect) != {"version"} or not isinstance(version, str) or not version or dict(ep.changes):
            raise PermanentApplyRefusal("the episode effect is a version precondition and writes nothing")
        from anneal_memory.origin import validate_origin_key

        try:
            validate_origin_key(ok)
            validate_origin_key(effect_id, what="effect_id")
        except ValueError as exc:
            raise PermanentApplyRefusal(str(exc)) from exc
        return ok, version, effect_id

    def apply_unit(self, unit: ApplyUnit) -> UnitApplyResult:
        """One ``delete_by_origin_key``, never with ``team_operator``: a team stream that enforces the
        entry may bring it back, as anneal's ``delete`` says of every caller but its CLI."""
        from anneal_memory import AnnealMemoryError

        ok, version, effect_id = self._plan(unit)
        # anneal creates a missing database on a write-capable open, so a missing one is checked first
        if not self.db.is_file():
            raise StoreGone(f"{self.db} is absent")
        try:
            with self._store(read_only=False) as store:
                res = store.delete_by_origin_key(ok, expected_version=version, effect_id=effect_id)
        except (AnnealMemoryError, OSError) as exc:
            raise TransientApplyFault.of(exc) from exc
        if res.outcome in ("deleted", "already_applied"):
            outcome = ApplyOutcome.APPLIED if res.outcome == "deleted" else ApplyOutcome.ALREADY
            return UnitApplyResult(outcome, (MemberApply(res.episode_id, {"origin_key": ok}),
                                             MemberApply(res.episode_id, None)))
        if res.outcome in ("version_mismatch", "deleted_by_other"):
            return UnitApplyResult(ApplyOutcome.PRECONDITION_LOST, (MemberApply(None, None),) * 2)
        if res.outcome == "unknown":
            raise PermanentApplyRefusal(f"no episode ever carried origin_key {ok!r}")
        raise TransientApplyFault("ValueError", f"anneal delete outcome {res.outcome!r}")
