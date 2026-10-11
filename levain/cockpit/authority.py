"""levain.cockpit.authority: the one authority snapshot a request renders and tiers from (K2b-3b
design §0.1, build plan "Render (lane B)").

``LabelKey`` names what a row's label is computed over; a ``LabelSnapshot`` (the broker's labeller,
``levain.cockpit.labels``) answers ``provenance(key)`` and ``label(key)`` from ONE journal read.
``RequestAuthority(now, labels)`` is captured once at each outer boundary (a GET, ``dispatch``,
``retier``) and rides on ``ReadContext.authority`` (a write's source reads find it through ``bound``),
so every row, note, tier and etag of one request comes from the same snapshot. ``NO_LABELS`` is the snapshot with no labeller attached: every
provenance is null and every label ``unverified``, which is today's output exactly.

The engine consumes only ``revision``, ``error``, ``provenance(key)`` and ``label(key)``."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator, Mapping, Protocol

UNVERIFIED = "unverified"


@dataclass(frozen=True)
class LabelKey:
    resource_key: str
    values: Mapping[str, Any]          # field -> the CURRENT stored value
    operation_id: str | None = None    # set: exactly that op's labels row; None: the latest by content
    gate: str | None = None            # tomb rows only: "match" | "mismatch" | "unavailable"


class LabelSnapshot(Protocol):
    revision: tuple[str, int, int] | None
    error: str | None

    def provenance(self, key: LabelKey | None) -> dict[str, Any] | None: ...

    def label(self, key: LabelKey | None) -> str: ...


class _NoLabels:
    """No labeller attached: nothing is labelled, and nothing says provenance is unavailable."""

    revision: tuple[str, int, int] | None = None
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
    revision: tuple[str, int, int] | None = None
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
    revision: tuple[str, int, int] | None = None
    degraded: int = 0

    def provenance(self, key: LabelKey | None) -> dict[str, Any] | None:
        return None

    def label(self, key: LabelKey | None) -> str:
        return UNVERIFIED


def inactive(note: str) -> LabelSnapshot:
    return _Inactive(note)


def unavailable(reason: str) -> LabelSnapshot:
    return _Unavailable(f"provenance unavailable: {reason}")


@dataclass(frozen=True)
class RequestAuthority:
    now: datetime
    labels: LabelSnapshot = NO_LABELS


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
