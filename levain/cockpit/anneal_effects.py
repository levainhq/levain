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

    def _require_db(self) -> None:
        """An absent store at a READ is a fault a retry can outlive (a restore, a move in
        progress), never proof the episode is gone (codex K2b-3a r1). ``apply_unit`` keeps
        ``StoreGone``, the settle mapping."""
        if not self.db.is_file():
            raise TransientApplyFault("FileNotFoundError", f"{self.db} is absent")

    def read(self, origin_key: str) -> EpisodeRead | None:
        """The live episode carrying ``origin_key``, or None: a superseded episode is not live, as
        the episodes panel (anneal's ``recall``) never lists it. ``TransientApplyFault`` for a read
        that faulted, the store's absence included."""
        from levain.cockpit.providers import episode_row_version

        self._require_db()
        try:
            with self._store(read_only=True) as store:
                got = store.read_episode_versioned(origin_key)
                if got is not None and store.superseded_by_map([got[0].id]):
                    got = None    # one definition of live: the panel's (recall's), which hides it
            if got is None:
                return None
            ep, version = got
            return EpisodeRead(_episode_dict(ep), version, episode_row_version(ep))
        except _read_faults() as exc:
            raise TransientApplyFault.of(exc) from exc

    def origin_key_of(self, episode_id: str) -> tuple[str | None, str]:
        """The live episode ``episode_id``'s ``origin_key`` and ``source``; ``KeyError`` when there
        is none."""
        self._require_db()
        try:
            with self._store(read_only=True) as store:
                ep = store.get(episode_id)
                hidden = ep is not None and bool(store.superseded_by_map([episode_id]))
        except _read_faults() as exc:
            raise TransientApplyFault.of(exc) from exc
        if ep is None or hidden:
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


# --- the neocortex section edit (K2b-3a section adapter; design SECTION_ADAPTER_DESIGN.md) ----------
#
# A section edit is ONE effect on ``section:<heading>``: ``expect: {version}`` with anneal's own section
# version (``Store.read_section``, sha256 of the span), ``changes: {body}``, applied by
# ``Store.replace_section(expected_version=...)`` under anneal's continuity lock. The Class-A section
# panel renders from the same ``read_section`` (``providers._anneal_section``), so the version it shows
# is ``value_version_of(anneal's version)``: issue compares that against what the operator was shown
# and binds the effect to anneal's version from the SAME read, so no second read sits between them.

SECTIONS = "sections"
SECTION_KEY_PREFIX = "section:"
SECTION_VERB = "section_edit"
# replace_section refusals a retry can outlive (a writer, a wrap, a wrap's tmp); every other reason
# is the effect's own, and a retry fails the same way
_SECTION_TRANSIENT = frozenset({"store_busy", "wrap_in_progress", "pipeline_tmp_present"})


def section_resource_key(heading: str) -> str:
    return SECTION_KEY_PREFIX + heading


def section_effects(heading: str, version: str, body: str) -> list[Effect]:
    """A section edit's one effect."""
    if not isinstance(version, str) or not version:
        raise ValueError("a section edit needs the section's version")
    if not isinstance(body, str):
        raise ValueError("a section edit's body is a string")
    return [Effect(section_resource_key(heading), SECTIONS, {"version": version}, {"body": body},
                   {"heading": heading}, (), ("heading",))]


SECTION_CANONICAL = {SECTION_VERB: section_effects}


def _written_version(body: str) -> str:
    """The version anneal gives ``body`` once written (its canonical form, hashed as read_section
    hashes a span), so a retry after a lost acknowledgement reads ALREADY."""
    import hashlib

    from anneal_memory.origin import canonical_section_markdown

    return hashlib.sha256(canonical_section_markdown(body).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SectionRead:
    """One section read in ONE read-only anneal open: its text, anneal's version (the store's
    precondition), the version the section panel renders for it, and whether a wrap is open."""

    text: str
    version: str
    rendered: str
    wrap_open: bool


class SectionEffectStore:
    """The ``EffectStore`` over the continuity sidecar of the anneal store at ``db``."""

    name = SECTIONS
    version_fields = ("heading", "body")
    authority_fields = ("heading",)
    store_assigned_fields = ()

    def __init__(self, db: str | Path) -> None:
        self.db = Path(db)

    def _store(self, *, read_only: bool) -> Any:
        from anneal_memory import Store

        return Store(str(self.db), read_only=read_only)

    def _require_db(self) -> None:
        if not self.db.is_file():
            raise TransientApplyFault("FileNotFoundError", f"{self.db} is absent")

    # -- reads --

    def heading_for(self, panel_id: str) -> str | None:
        """The schema heading the section panel ``panel_id`` renders (the providers' own slug), so
        the write's address comes from the store, never the request."""
        from levain.cockpit.providers import section_panel_id

        self._require_db()
        try:
            with self._store(read_only=True) as store:
                headings = [s["heading"] for s in store.section_schema]
        except _read_faults() as exc:
            raise TransientApplyFault.of(exc) from exc
        found = [h for h in headings if section_panel_id(h) == panel_id]
        return found[0] if len(found) == 1 else None

    def read(self, heading: str) -> SectionRead | None:
        """The section under ``heading``, or None when the file has no such section. A section
        anneal cannot read as one span (two headers claim it, not UTF-8) is ``ValueError``: no
        version can bind to it, and a retry reads it the same way."""
        from anneal_memory import SectionError

        from levain.cockpit.engine import value_version_of

        self._require_db()
        try:
            with self._store(read_only=True) as store:
                got = store.read_section(heading)
                wrap_open = bool(store.get_wrap_started_at())
        except SectionError as exc:
            raise ValueError(f"the {heading!r} section is unreadable: {exc}") from exc
        except _read_faults() as exc:
            raise TransientApplyFault.of(exc) from exc
        if got is None:
            return None
        text, version = got
        return SectionRead(text, version, value_version_of(version), wrap_open)

    def snapshot(self, resource_key: str) -> Snapshot:
        heading = _key_after(SECTION_KEY_PREFIX, resource_key)
        got = self.read(heading)
        return Snapshot(None, None) if got is None else Snapshot({"heading": heading, "body": got.text},
                                                                 got.version)

    def units(self, effects: Sequence[tuple[int, Effect]]) -> list[ApplyUnit]:
        return [ApplyUnit(SECTIONS, (i,), (e,)) for i, e in effects]

    def label_fields(self, effect: Effect, stored: Mapping[str, Any] | None) -> tuple[str, ...]:
        return ()

    # -- apply --

    @staticmethod
    def _plan(unit: ApplyUnit) -> tuple[str, str, str]:
        if unit.store != SECTIONS or len(unit.effects) != 1:
            raise PermanentApplyRefusal("a section unit is one section effect")
        (e,) = unit.effects
        heading = _key_after(SECTION_KEY_PREFIX, e.resource_key)
        version, body = e.expect.get("version"), e.changes.get("body")
        if set(e.expect) != {"version"} or not isinstance(version, str) or not version:
            raise PermanentApplyRefusal("a section effect expects the section's version")
        if set(e.changes) != {"body"} or not isinstance(body, str):
            raise PermanentApplyRefusal("a section effect changes the body and nothing else")
        return heading, version, body

    def apply_unit(self, unit: ApplyUnit) -> UnitApplyResult:
        """One ``replace_section``. A store without it (anneal below 0.9.43) or without a continuity
        lock refuses permanently: neither appears on a retry."""
        from anneal_memory import AnnealMemoryError, ContinuityLockUnavailable

        heading, version, body = self._plan(unit)
        if not self.db.is_file():
            raise StoreGone(f"{self.db} is absent")
        try:
            with self._store(read_only=False) as store:
                if not hasattr(store, "replace_section"):
                    raise PermanentApplyRefusal("this anneal has no Store.replace_section (0.9.43 or later)")
                res = store.replace_section(heading, body, expected_version=version)
        except ContinuityLockUnavailable as exc:
            raise PermanentApplyRefusal(f"no continuity lock on this filesystem: {exc}") from exc
        except (AnnealMemoryError, OSError) as exc:
            raise TransientApplyFault.of(exc) from exc
        stored = {"heading": heading}
        if res.outcome == "written":
            return UnitApplyResult(ApplyOutcome.APPLIED, (MemberApply(None, stored),))
        if res.outcome == "version_mismatch":
            if res.version == _written_version(body):
                return UnitApplyResult(ApplyOutcome.ALREADY, (MemberApply(None, stored),))
            return UnitApplyResult(ApplyOutcome.PRECONDITION_LOST, (MemberApply(None, None),))
        if res.reason in _SECTION_TRANSIENT:
            raise TransientApplyFault("SectionRefused", f"{res.reason}: {res.message}")
        raise PermanentApplyRefusal(f"anneal refused the section edit: {res.reason}: {res.message}")
