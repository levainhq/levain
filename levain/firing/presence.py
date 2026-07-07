"""levain.firing — the full flow-PRESENCE layer for a Levain operator on OpenHands.

The ``VagusCondenser`` (``vagus.adapters.openhands``) carries the *firing* half — per-turn
crystallized recall + a rotating drift-defense directive + Stop→capture, all afferent-only.
This package carries the *presence* half that Claude Code splits across three hooks
(``recall_injection`` + ``anti_gatekeeping`` + ``compaction_reinject``) and that a Levain
operator on OpenHands has NO ``CLAUDE.md`` to carry — so **the condenser IS the config
surface**: presence rides injection, not a config file.

Slice 1 lands the piece the firing adapter structurally can NOT do — the **compaction-reinject
fold**. The per-turn recall + directive already self-heal after a compaction (the condenser
re-injects them next turn); the behavioral RE-ANCHOR (Claude Code's ``compaction_reinject``)
is the one presence job that needs the condenser to be *compaction-aware*, and the shipped
``VagusCondenser`` explicitly skips it (its ``_inject_into`` M3 caveat). ``LevainCondenser``
(``levain.firing.openhands``) folds it in.

This module — the harness-neutral **``PresenceSource`` seam** — is a **dependency-isolated
leaf**, exactly like ``vagus.firing``: importing ``levain.firing`` pulls NO OpenHands and NO
flow. The OpenHands ``LevainCondenser`` lives in the sibling ``levain.firing.openhands`` behind
the ``openhands`` extra; the real presence CONTENT (flow's behavioral re-anchor / a Levain
operator's seed) registers as a new kind behind the same ``reanchor`` signature later. Slice 1
ships a STUB (``StubPresence``) with a real re-anchor SHAPE and stubbed CONTENT — proving the
seam, not the content (the same discipline ``vagus.firing.StubFiring`` used for Slice 1 there).

**Afferent-only, by construction.** ``reanchor`` READS presence content and returns text injected
into the agent's OWN context (ungated, safe — the same membrane as ``vagus.firing``). It never
writes, never sends a transport, never consolidates. A ``PresenceSource`` that ever reached the
world would breach the afferent line and would have to split + gate (WATCH-IT seam #1, vagus
``brief.md``). Fail-soft: any failure degrades to ``None`` (no re-anchor) — a missing behavioral
re-anchor is low-stakes (NOT data loss like a swallowed capture), so "no re-anchor beats a crash".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

__all__ = [
    "ReanchorRequest",
    "PresenceSource",
    "StubPresence",
    "register_presence",
    "build_presence",
]


@dataclass(frozen=True)
class ReanchorRequest:
    """What the condenser knows at re-anchor time — harness-neutral.

    A re-anchor fires on the RECOVERY turn (the first normal turn after a compaction), so a
    ``PresenceSource`` can re-assert the behavioral protocol + current state that the compaction
    trimmed out of recency. ``query`` is the recall CONTEXT (the agent's own most-recent message,
    the same signal the firing recalls against) — a content-aware presence source may tailor the
    re-anchor to it; ``StubPresence`` ignores it. ``turn_index`` is the condenser's monotonic
    per-inject counter (for any rotation a source wants), ``None`` when unknown.
    """

    query: str = ""
    turn_index: int | None = None


@runtime_checkable
class PresenceSource(Protocol):
    """The stable seam the ``LevainCondenser`` depends on for the re-anchor CONTENT. Afferent-only.

    ``reanchor`` is pure-afferent: it returns the behavioral re-anchor text to inject at recency
    on the post-compaction recovery turn, or ``None`` when there is nothing to re-anchor (no event
    is then injected). It MUST be READ-ONLY — it never acts outward and never consolidates — and it
    MUST NOT raise into the agent's turn (the condenser wraps it fail-soft, but a source that
    fail-softs at its OWN boundary keeps the "no re-anchor beats a crash" guarantee local and
    testable). The concrete re-anchor content (flow's ``compaction_reinject.md`` + State + Top of
    Mind; a Levain operator's seed) registers as a new kind behind this signature in a later slice.
    """

    def reanchor(self, req: ReanchorRequest) -> str | None: ...


# --- the presence registry (serialization-safe reconstruction) ----------------------
#
# The condenser identifies its presence source by a serializable ``presence_kind`` (not a live
# handle) so ``fork()`` / reload rebuild the right source — the same reason ``vagus.firing`` uses a
# kind registry (a live handle in a ``PrivateAttr`` is silently dropped on the serialize→validate
# round-trip fork performs). Real presence kinds (a future flow/seed source) self-register on
# import; a blessed-lazy-leaf allowlist mirrors ``vagus.firing._LAZY_FIRING_MODULES`` and is added
# WHEN the first optional leaf exists (Slice 4) — Slice 1 has only the stdlib ``StubPresence``, so
# no lazy import is needed yet and none is offered (governed > clever: no arbitrary-module import).

_PRESENCE_REGISTRY: dict[str, Callable[[], PresenceSource]] = {}


def register_presence(kind: str, factory: Callable[[], PresenceSource]) -> None:
    """Register a ``PresenceSource`` factory under a serializable ``kind``."""
    _PRESENCE_REGISTRY[kind] = factory


def build_presence(kind: str) -> PresenceSource:
    """Rebuild a ``PresenceSource`` from its registered kind (used on fork / reload).

    Serialization-safe: fork/reload reconstruct from the kind ALONE. An unknown kind raises a
    clear error rather than silently degrading (a typo'd presence kind should surface, not
    vanish into a no-re-anchor). Real content kinds live in optional leaves that self-register
    on import; when the first such leaf ships (Slice 4) a blessed-lazy-import allowlist joins
    here, mirroring ``vagus.firing.build_firing``.
    """
    factory = _PRESENCE_REGISTRY.get(kind)
    if factory is None:
        raise ValueError(
            f"unknown presence kind {kind!r}; registered: {sorted(_PRESENCE_REGISTRY)}"
        )
    return factory()


# --- Slice 1 STUB implementation ----------------------------------------------------


@dataclass
class StubPresence:
    """A static ``PresenceSource`` for Slice 1 — proves the SEAM, not the content.

    The re-anchor SHAPE is real (a compressed behavioral re-assertion at recency on the recovery
    turn); only the CONTENT is a placeholder. The real kinds render flow's ``compaction_reinject.md``
    + live State + Top of Mind, or a Levain operator's seed, behind this same ``reanchor`` signature.
    """

    reanchor_text: str = (
        "[presence re-anchor — post-compaction] "
        "<flow behavioral re-anchor + current State + Top of Mind wire in here>"
    )

    def reanchor(self, req: ReanchorRequest) -> str | None:
        # Stub: a static re-anchor, ignoring the request. Real kinds read content against
        # req.query; all are READ-ONLY + afferent by contract. Empty text → no re-anchor.
        return self.reanchor_text or None


register_presence("stub", StubPresence)
