"""The ``coordinator_drop`` broker effect: a device-signed plain List line (a *sort*) as ONE coordinator drop.

A plain line typed in the List is words for the desk to sort. On a browser token that is a direct C1 write
(``verbs._intake_fire``); from a device credential it is a signed pending, and what its decision writes is a drop in
the coordinator store, from the ``cockpit`` surface, keyed by the pending's create origin key as its ``outbox_id``
(``effects.create_origin_key(pid, 0)``, ``ck.<hex>.0``, which fits the store's outbox rule). The effect is a create on
the ``coordinator`` store: the signed record carries the words verbatim, the decision rebuilds the same effect and
compares, and a recovery after a crash between the write and its record finds the drop under its key (``ALREADY``)
and writes nothing twice.

Known edge: a coordinator directory or store that is missing is created by the write (``accept_drop`` makes it), as the
browser-token path does; only a read (``snapshot``) never makes one. ``snapshot`` reads the drop through the store's
private ``_drop`` on a read-only connection because the public ``drop_by_outbox`` opens the store for writing and so
would create it.

Known edges (closed as design, not code). The coordinator directory is resolved when the drop is written, not bound
into the signed effect, so a pending signed under one LEVAIN_HOME and decided (or recovered) after a restart under
another, with the same ledger root, writes to the second directory (the directory comes from the process
environment, fixed for a server's life). The broker's local-filesystem check covers the spore and episode stores,
not the coordinator directory (resolved per call).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

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

COORDINATOR = "coordinator"
COORD_DROP_VERB = "coordinator_drop"
DROP_KEY_PREFIX = "drop:"
DROP_FIELDS = ("text",)
# the store fills these at write; none sits in the effect's ``changes``
DROP_STORE_ASSIGNED = ("drop_id", "outbox_id", "surface", "sha")


def drop_resource_key(origin_key: str) -> str:
    return DROP_KEY_PREFIX + origin_key


def _origin_key_of(resource_key: str) -> str:
    if not resource_key.startswith(DROP_KEY_PREFIX) or len(resource_key) == len(DROP_KEY_PREFIX):
        raise PermanentApplyRefusal(f"{resource_key!r} is not a coordinator drop resource key")
    return resource_key[len(DROP_KEY_PREFIX):]


def coordinator_drop_effect(params: Mapping[str, Any], pre: Mapping[str, Any] | None, *, origin_key: str | None,
                            version_of: Callable[..., str] | None = None) -> Effect:
    """The one create a sort line makes: ``params`` are exactly ``{"text": <the parsed sort text, verbatim>}``. Pure:
    the decision rebuilds it from the recorded reading and compares it with the signed one. ``ValueError`` refuses
    params, a ``pre`` (a create has no earlier object) or a missing origin key."""
    if set(params) != {"text"} or not isinstance(params.get("text"), str) or not params["text"]:
        raise ValueError("a coordinator drop is exactly {text}, a non-empty string")
    from levain.coordinator.store import CoordError, check_words

    try:
        check_words(params["text"])        # the store's own word check: refused here, before any signature
    except CoordError as exc:
        raise ValueError(str(exc)) from exc
    if pre is not None:
        raise ValueError("a create's origin_key is already in use")
    if not origin_key:
        raise ValueError("a create needs its origin_key (create_origin_key)")
    changes = {"text": params["text"]}
    return Effect(drop_resource_key(origin_key), COORDINATOR, {"absent": True}, changes, dict(changes), DROP_FIELDS, ())


COORD_CANONICAL: dict[str, Callable[..., Effect]] = {COORD_DROP_VERB: coordinator_drop_effect}


class CoordDropEffectStore:
    """The ``EffectStore`` over the coordinator's drops (``levain.coordinator.store.CoordStore``). ``dir_of`` names the
    coordinator directory on each call (the consent reader is attached after the server is built), None when this
    cockpit names none. Every effect is its own unit: one ``accept_drop`` is one transaction."""

    name = COORDINATOR
    version_fields = DROP_FIELDS
    authority_fields = DROP_FIELDS
    store_assigned_fields = DROP_STORE_ASSIGNED

    def __init__(self, dir_of: Callable[[], Path | str | None]) -> None:
        self.dir_of = dir_of

    def directory(self) -> Path | None:
        d = self.dir_of()
        return None if d is None else Path(d)

    @staticmethod
    def _row(drop: Any) -> dict[str, Any]:
        return {"text": drop.words, "drop_id": drop.drop_id, "outbox_id": drop.outbox_id, "surface": drop.surface,
                "sha": drop.words_sha256}

    # -- reads --

    def snapshot(self, resource_key: str) -> Snapshot:
        """The drop stored under the key's ``outbox_id``, else row None. A store that is not there reads as no row (a
        read never makes it); the apply is what refuses a directory this cockpit no longer names."""
        from levain.coordinator.store import CoordError, CoordStore, StoreAbsent
        from levain.dispatch import DispatchError

        key = _origin_key_of(resource_key)
        d = self.directory()
        if d is None:
            return Snapshot(None, None)
        store = CoordStore(d)
        try:
            conn = store.open_readonly()
        except StoreAbsent:
            return Snapshot(None, None)
        except (CoordError, OSError, sqlite3.Error, DispatchError) as exc:
            raise TransientApplyFault.of(exc) from exc
        try:
            found = conn.execute("SELECT drop_id FROM drops WHERE outbox_id = ?", (key,)).fetchone()
            drop = None if found is None else store._drop(conn, found[0])
        except (CoordError, OSError, sqlite3.Error, DispatchError) as exc:
            raise TransientApplyFault.of(exc) from exc
        finally:
            conn.close()
        return Snapshot(None, None) if drop is None else Snapshot(self._row(drop), drop.words_sha256)

    # -- apply --

    def units(self, effects: Sequence[tuple[int, Effect]]) -> list[ApplyUnit]:
        return [ApplyUnit(COORDINATOR, (i,), (e,)) for i, e in effects]

    def label_fields(self, effect: Effect, stored: Mapping[str, Any] | None) -> tuple[str, ...]:
        if stored is None or effect.projection is None:
            return ()
        return tuple(f for f in effect.writes if stored.get(f) == effect.projection.get(f))

    def apply_unit(self, unit: ApplyUnit) -> UnitApplyResult:
        from levain.coordinator.store import CoordError, CoordStore, DropConflict, StoreAbsent
        from levain.dispatch import DispatchError
        from levain.dispatch.jobs import JobStoreError

        (effect,) = unit.effects
        if not effect.is_create or set(effect.changes) != {"text"} or not isinstance(effect.changes["text"], str):
            raise PermanentApplyRefusal("the coordinator store takes one drop create, {text}, only")
        key = _origin_key_of(effect.resource_key)
        d = self.directory()
        if d is None:
            raise StoreGone("this cockpit names no coordinator store")
        try:
            drop, created = CoordStore(d).accept_drop(effect.changes["text"], surface="cockpit", outbox_id=key)
        except StoreAbsent as exc:
            raise StoreGone(str(exc)) from exc
        except DropConflict as exc:
            # the key is this pending's, so other words under it are no retry of ours: refused, nothing written
            raise PermanentApplyRefusal(str(exc)) from exc
        except CoordError as exc:
            raise PermanentApplyRefusal(str(exc)) from exc
        except JobStoreError as exc:
            # a directory that is not 0700 / not ours does not mend by itself: refused, not retried forever
            raise PermanentApplyRefusal(str(exc)) from exc
        except (OSError, sqlite3.Error, DispatchError) as exc:
            raise TransientApplyFault.of(exc) from exc
        outcome = ApplyOutcome.APPLIED if created else ApplyOutcome.ALREADY
        return UnitApplyResult(outcome, (MemberApply(drop.drop_id, self._row(drop)),))
