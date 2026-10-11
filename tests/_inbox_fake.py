"""A fake downstream inbox for the downstream effect seam's tests: an ``EffectStore`` whose one effect is a
note create, idempotent on its ``origin_key`` (the argushub contract flow design §9 row P names), with the
authority projection ``body`` and ``for``. In memory, or persisted to a JSON file when given a path, so a
kill-point subprocess and the broker that recovers after it see one store."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from levain.cockpit.effects import (
    ApplyOutcome,
    ApplyUnit,
    DownstreamEffect,
    Effect,
    MemberApply,
    PermanentApplyRefusal,
    Snapshot,
    UnitApplyResult,
)

INBOX = "inbox"
PREFIX = "inbox:"
FIELDS = ("body", "for")


class FakeInbox:
    name = INBOX
    version_fields = FIELDS
    authority_fields = FIELDS
    store_assigned_fields = ("id", "origin_key")

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = None if path is None else Path(path)
        self._notes: dict[str, dict[str, Any]] = {}

    @property
    def notes(self) -> dict[str, dict[str, Any]]:
        if self.path is not None:
            return json.loads(self.path.read_text()) if self.path.exists() else {}
        return self._notes

    def _save(self, notes: dict[str, dict[str, Any]]) -> None:
        if self.path is None:
            self._notes = notes
        else:
            self.path.write_text(json.dumps(notes))

    def snapshot(self, resource_key: str) -> Snapshot:
        row = self.notes.get(resource_key.removeprefix(PREFIX))
        return Snapshot(row, None if row is None else "v1")

    def units(self, effects: Sequence[tuple[int, Effect]]) -> list[ApplyUnit]:
        return [ApplyUnit(INBOX, (i,), (e,)) for i, e in effects]

    def apply_unit(self, unit: ApplyUnit) -> UnitApplyResult:
        (effect,) = unit.effects
        if not effect.is_create:
            raise PermanentApplyRefusal("the inbox takes creates only")
        key = effect.resource_key.removeprefix(PREFIX)
        notes = self.notes
        if key in notes:
            return UnitApplyResult(ApplyOutcome.ALREADY, (MemberApply(notes[key]["id"], notes[key]),))
        note = {"id": f"note-{len(notes) + 1}", "origin_key": key, **dict(effect.changes)}
        notes[key] = note
        self._save(notes)
        return UnitApplyResult(ApplyOutcome.APPLIED, (MemberApply(note["id"], note),))

    def label_fields(self, effect: Effect, stored: Mapping[str, Any] | None) -> tuple[str, ...]:
        if stored is None or effect.projection is None:
            return ()
        return tuple(f for f in effect.writes if stored.get(f) == effect.projection.get(f))


def send_inbox_effect(params: Mapping[str, Any], *, origin_key: str) -> Effect:
    body, to = params.get("body"), params.get("for", "cli")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("a note needs a body")
    changes = {"body": body.strip(), "for": to}
    return Effect(PREFIX + origin_key, INBOX, {"absent": True}, changes, dict(changes), FIELDS, ())


def inbox_downstream(path: Path | str | None = None) -> tuple[FakeInbox, DownstreamEffect]:
    store = FakeInbox(path)
    return store, DownstreamEffect(store, send_inbox_effect)
