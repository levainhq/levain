"""A fake downstream inbox for the downstream effect seam's tests: an ``EffectStore`` whose one effect is a
note create, idempotent on its ``origin_key`` (the argushub contract flow design §9 row P names), with the
authority projection ``body`` and ``for``. In memory, or persisted to a JSON file when given a path, so a
kill-point subprocess and the broker that recovers after it see one store."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from anneal_memory.spores import normalize_spore_field

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

    def __init__(self, path: Path | str | None = None, *, faults: Sequence[BaseException | None] = ()) -> None:
        self.path = None if path is None else Path(path)
        self._notes: dict[str, dict[str, Any]] = {}
        self._retired: dict[str, str] = {}
        self.faults = list(faults)

    @property
    def notes(self) -> dict[str, dict[str, Any]]:
        if self.path is not None:
            return json.loads(self.path.read_text()) if self.path.exists() else {}
        return self._notes

    @property
    def retired(self) -> dict[str, str]:
        """origin_key -> the id of a note sent on that key and since deleted."""
        if self.path is not None:
            side = self.path.with_suffix(".retired.json")
            return json.loads(side.read_text()) if side.exists() else {}
        return self._retired

    def _save(self, notes: dict[str, dict[str, Any]], retired: dict[str, str] | None = None) -> None:
        if self.path is None:
            self._notes = notes
            if retired is not None:
                self._retired = retired
        else:
            self.path.write_text(json.dumps(notes))
            if retired is not None:
                self.path.with_suffix(".retired.json").write_text(json.dumps(retired))

    def retire(self, key: str) -> None:
        notes = self.notes
        note = notes.pop(key)
        self._save(notes, {**self.retired, key: note["id"]})

    def _get(self, key: str) -> tuple[dict[str, Any] | None, bool]:
        """GET by origin_key: the stored note, and whether the key was ever used."""
        note = self.notes.get(key)
        return note, note is not None or key in self.retired

    def snapshot(self, resource_key: str) -> Snapshot:
        if self.faults:
            fault = self.faults.pop(0)
            if fault is not None:
                raise fault
        note, used = self._get(resource_key.removeprefix(PREFIX))
        if note is None and used:
            return Snapshot({"retired": True}, "retired")
        return Snapshot(note, None if note is None else "v1")

    def units(self, effects: Sequence[tuple[int, Effect]]) -> list[ApplyUnit]:
        return [ApplyUnit(INBOX, (i,), (e,)) for i, e in effects]

    def apply_unit(self, unit: ApplyUnit) -> UnitApplyResult:
        (effect,) = unit.effects
        if not effect.is_create:
            raise PermanentApplyRefusal("the inbox takes creates only")
        key = effect.resource_key.removeprefix(PREFIX)
        stored, used = self._get(key)
        if used:
            # the key was used: ALREADY with the note as a GET by origin_key reads it now, which
            # label_fields compares with the projection, or with nothing when it was since deleted
            if stored is None:
                return UnitApplyResult(ApplyOutcome.ALREADY, (MemberApply(self.retired[key], None),))
            return UnitApplyResult(ApplyOutcome.ALREADY, (MemberApply(stored["id"], dict(stored)),))
        notes = self.notes
        note = {"id": f"note-{len(notes) + len(self.retired) + 1}", "origin_key": key, **dict(effect.changes)}
        notes[key] = note
        self._save(notes)
        return UnitApplyResult(ApplyOutcome.APPLIED, (MemberApply(note["id"], note),))

    def label_fields(self, effect: Effect, stored: Mapping[str, Any] | None) -> tuple[str, ...]:
        if stored is None or effect.projection is None:
            return ()
        return tuple(f for f in effect.writes if stored.get(f) == effect.projection.get(f))


def send_inbox_effect(params: Mapping[str, Any], *, origin_key: str) -> Effect:
    body, to = params.get("body"), params.get("for", "cli")
    if not isinstance(body, str) or not normalize_spore_field(body):
        raise ValueError("a note needs a body")
    changes = {"body": normalize_spore_field(body), "for": to}
    return Effect(PREFIX + origin_key, INBOX, {"absent": True}, changes, dict(changes), FIELDS, ())


def inbox_downstream(path: Path | str | None = None) -> tuple[FakeInbox, DownstreamEffect]:
    store = FakeInbox(path)
    return store, DownstreamEffect(store, send_inbox_effect)
