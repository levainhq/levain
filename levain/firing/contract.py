"""levain.firing.contract — the afferent-only firing contract (relocated from ``vagus.firing``).

The harness-neutral seam every firing-adapter (Claude Code hooks, Codex hooks, the
OpenHands ``VagusCondenser``) implements: **inject / capture / drift-defense**.

Afferent-only by construction — it perceives and recalls into the agent's OWN context
(ungated), captures raw episodes (an untrusted append, never the gated consolidate), and
carries the drift-defense directive. It NEVER acts outward and NEVER consolidates. That
constraint is the constitution (flow ``projects/vagus/brief.md``) compiled into the
contract's shape.

Adapters depend on the ``FiringContract`` PROTOCOL (a stable seam), never on anneal
directly — an anti-corruption layer, so anneal can churn without breaking N adapters.

A ``FiringContract`` is live *behavior*, not serializable *state*. An adapter that must
survive ``fork()`` / reload (both round-trip through serialization) cannot carry a live
handle — it must rebuild the firing from a serializable KIND. So firings register under a
kind (:func:`register_firing`) and are rebuilt via :func:`build_firing`. (Apparatus
HIGH-1: a live handle in a ``PrivateAttr`` is silently dropped on the serialize→validate
that fork performs.)

Slice 1 ships the protocol + a STUB implementation (``StubFiring``, kind ``"stub"``) with
a real trusted-context *shape* and stubbed recall *content*. Real anneal wiring
(``retrieve_patterns`` / ``retrieve_episodes``, in-process) registers a new kind behind
the same ``inject`` signature later — the dependency-isolated leaf stays anneal-only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol, runtime_checkable

__all__ = [
    "InjectRequest",
    "CaptureRequest",
    "FiringContract",
    "StubFiring",
    "register_firing",
    "build_firing",
    "DIRECTIVES",
    "select_directive",
]

LifecyclePoint = Literal["session_start", "per_turn"]


@dataclass(frozen=True)
class InjectRequest:
    """What an adapter knows at inject time — harness-neutral.

    ``lifecycle_point`` distinguishes the always-on session-start constitution from the
    per-turn drift-defense inject; ``turn_index`` lets an implementation rotate or budget
    as a pure function of the request (the race-free home for directive rotation).
    ``query`` is the recall CONTEXT — the agent's own recent context (typically its latest
    user message) that a real-recall firing (``AnnealFiring``) recalls against. Recalling
    into own-context against own-context is pure afferent. Empty (the default) → no recall;
    ``StubFiring`` ignores it (it carries a stubbed recall slot).
    """

    lifecycle_point: LifecyclePoint = "per_turn"
    turn_index: int | None = None
    query: str = ""


@dataclass(frozen=True)
class CaptureRequest:
    """What an adapter knows at a Stop (turn-end) boundary — harness-neutral.

    ``content`` is the RAW factual record of the turn — the **afferent-safe substrate-write**
    of the constitution's three-way membrane: an untrusted, append-only episodic write with
    the same epistemic status as perception (a raw log, NOT metabolized truth). It must NOT be
    an LLM-summarized synthesis — summarizing is a light consolidate, which would breach the
    afferent line. ``episode_type`` must be one of anneal's real ``EpisodeType`` values —
    ``observation / decision / tension / question / outcome / context`` (NOT flow's looser
    ``finding`` vocabulary, which anneal's enum rejects); the raw turn defaults to
    ``observation``. A bad type degrades to ``observation`` at write (the firing won't lose an
    episode over a type typo). ``source`` is the attribution; ``session_id`` groups a run's
    episodes and is persisted into the episode ``metadata`` as ``vagus_session_id`` (anneal's
    ``Store.record`` has no session_id parameter — the store derives its own); ``metadata`` is
    optional JSON-serializable provenance.

    **The encoding shield fires HERE, at construction.** Upstream of levain (the Ollama ``/v1``
    -> litellm -> OpenHands streaming chain) has been observed splitting a multi-byte character
    across two chunks and mis-decoding it, so mojibake arrives PRE-BROKEN at levain's capture
    boundary — verified in a real entity's stored ``memory.db`` BYTES, 2026-07-19. levain owns
    none of that decode path and therefore cannot fix it; what it owns is whether it STORES the
    damage silently. ``__post_init__`` runs :func:`levain.firing.encoding.scan_text`: it repairs
    only PROVABLY double-encoded spans (gated on a four-character lead set — read
    ``encoding._REPAIR_LEADS`` before changing anything there; the byte-structural checks alone
    rewrite ordinary typography), leaves everything else byte-for-byte untouched, and attaches a
    receipt (:data:`~levain.firing.encoding.RECEIPT_KEY`) to ``metadata`` when either happened. Placing it on the shared value object rather than inside one firing's ``capture()``
    is deliberate: every adapter — present and future — reaches a store write through this
    object, so no capture path can write unscanned text
    (``structural_invariants_beat_discipline``). The receipt is the point for a product whose
    pitch is "a memory you can trust because you can SEE what is in it": a repair stays
    auditable, and unrepairable damage is LABELLED rather than laundered by a guess.
    """

    content: str
    episode_type: str = "observation"
    source: str = "vagus"
    session_id: str | None = None
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        from levain.firing.encoding import RECEIPT_KEY, scan_text

        scan = scan_text(self.content or "")
        if scan.clean:
            # The overwhelmingly common capture — untouched, no receipt noise. One exception:
            # strip a receipt describing content this request no longer holds.
            # ``dataclasses.replace(req, content=...)`` re-runs __post_init__ but COPIES the
            # old metadata, so a clean replacement would otherwise inherit a receipt claiming
            # a repair that is not in its text. No caller does this today; a value object whose
            # invariant survives only while nobody uses the stdlib on it does not have an
            # invariant.
            if self.metadata and RECEIPT_KEY in self.metadata:
                pruned = {k: v for k, v in self.metadata.items() if k != RECEIPT_KEY}
                object.__setattr__(self, "metadata", pruned or None)
            return
        meta = dict(self.metadata or {})
        meta[RECEIPT_KEY] = scan.receipt()
        # A frozen dataclass normalizes through object.__setattr__ (the documented escape
        # hatch) — frozen protects callers from MUTATING a request after the fact, which this
        # is not: it is the value object refusing to exist in an unscanned state.
        object.__setattr__(self, "content", scan.text)
        object.__setattr__(self, "metadata", meta)


@runtime_checkable
class FiringContract(Protocol):
    """The stable seam adapters depend on. Afferent-only.

    ``inject`` is pure-afferent: it returns trusted text; it never acts outward and never
    consolidates. It is called at TWO lifecycle points (``req.lifecycle_point``), and an
    adapter MUST wire BOTH:

      - ``"session_start"`` → the STATIC constitution, placed once into the adapter's
        set-once trusted surface (OpenHands ``AgentContext.system_message_suffix``). It
        persists across compaction, so the per-turn inject never repeats it.
      - ``"per_turn"`` → the dynamic recall + the rotating drift-defense directive, placed
        at recency every turn.

    Pulling the constitution out of per-turn (it lived there through Slice 2) into the
    persistent suffix is the firing-lifecycle split: an adapter that wires only the per-turn
    seam would now lose the constitution — wiring session_start is a contract requirement,
    not optional.
    """

    def inject(self, req: InjectRequest) -> str: ...

    def capture(self, req: CaptureRequest) -> bool:
        """Afferent-safe substrate-write: append ``req`` as a RAW episode. Returns ``True`` iff
        the episode was durably written (so a caller can gate a dedup marker on a confirmed
        write — a swallowed failure must stay retryable). PROVISIONAL (v0.x) — declared here now
        because OpenHands is the live adopter hitting the Stop lifecycle; the signature stabilizes
        at the rule-of-three contract extraction.

        Two membrane invariants, by construction:
          - **Append-only.** It records an episode; it MUST NEVER consolidate (metabolize the
            felt memory). Consolidation is the gated efferent write the vagus is forbidden —
            ``the vagus CAPTURES but never CONSOLIDATES`` (``brief.md`` constitution).
          - **Fail-soft but LOUD.** A write failure logs (a lost capture is *data loss*, unlike
            recall's best-effort "no recall beats a crash") and returns ``False``, but NEVER
            raises into the agent's turn-end. fail-soft ≠ fail-silent.
        """
        ...


# --- the firing registry (serialization-safe reconstruction) ------------------------

_FIRING_REGISTRY: dict[str, Callable[[], FiringContract]] = {}


def register_firing(kind: str, factory: Callable[[], FiringContract]) -> None:
    """Register a ``FiringContract`` factory under a serializable ``kind``."""
    _FIRING_REGISTRY[kind] = factory


# Explicit allowlist of lazily-importable firing leaves (kind → module). An explicit map
# rather than ``import_module(f"{__name__}.{kind}")`` GOVERNS which leaves build_firing may
# import: a crafted/typo kind ("__init__", "anneal.extra", an arbitrary submodule) can never
# trigger an import of an unintended module — it just falls through to the clean "unknown kind"
# error. Each optional leaf self-registers on import (apparatus L3 codex LOW: governed > clever).
_LAZY_FIRING_MODULES: dict[str, str] = {
    "anneal": "levain.firing.anneal",
    # The ISOLATED entity firing lives in the SAME leaf module (both self-register on its import),
    # so an isolated entity reconstructed cold via fork/reload lazily imports the anneal leaf and
    # finds ``anneal_entity`` registered. (levain.firing.isolation — the sovereignty guard.)
    "anneal_entity": "levain.firing.anneal",
}


def build_firing(kind: str) -> FiringContract:
    """Rebuild a ``FiringContract`` from its registered kind (used on fork / reload).

    A kind may live in an OPTIONAL leaf that self-registers on import (e.g. the anneal leaf
    ``vagus.firing.anneal``). If ``kind`` isn't registered yet but is a BLESSED leaf
    (:data:`_LAZY_FIRING_MODULES`), lazily import it and retry — so this stays serialization-
    safe (fork/reload reconstruct from the kind ALONE) WITHOUT the core eagerly importing every
    optional leaf and inheriting its dependency closure. A blessed leaf's OWN import errors
    propagate (surface a broken leaf); an unblessed/unknown kind falls through to a clear error.
    """
    factory = _FIRING_REGISTRY.get(kind)
    if factory is None:
        module = _LAZY_FIRING_MODULES.get(kind)
        if module is not None:
            import importlib

            importlib.import_module(module)  # blessed leaf — its own import errors propagate
        factory = _FIRING_REGISTRY.get(kind)
    if factory is None:
        raise ValueError(
            f"unknown firing kind {kind!r}; registered: {sorted(_FIRING_REGISTRY)}"
        )
    return factory()


# --- Slice 1 STUB implementation ----------------------------------------------------

# The rotating anti-gatekeeping directives (drift-defense). Shared by every firing kind so
# the rotation POLICY lives in one place (StubFiring rotates on its call counter; the real
# kinds rotate on ``req.turn_index`` — pure + race-free, the design intent of ``turn_index``).
#
# These are the FULL recency directives, at byte-parity with the operator-editable activation
# template ``levain/templates/activation/recency_directives.md`` — the "second sourdough surface"
# the HOOKED adapters (Claude Code / Codex) inject via their ``UserPromptSubmit`` hook. The
# OpenHands entity is HOOKLESS (``init --adapter openhands`` lays down NO activation tree — the
# ``LevainCondenser`` IS its activation, per step 3), so it cannot read that file; this package
# constant is the ONE per-turn drift-defense surface it has. Spore-296 (package locus, Phill
# 2026-07-10) folds the rich directives in HERE, not into the entity seed: the drift-defense is
# UNIVERSAL machinery (NOT the identity moat — the seed owns identity), so one canonical set lives
# in the package and a package upgrade improves every entity at once. The base-lane
# ``test_firing_contract.test_directive_parity_with_shared_activation_template`` locks this tuple to
# the shared template's block bodies, extracted by the REAL ``_levain_hook.read_blocks`` (``canonical
# _object_model_plus_replaceable_surfaces``: one directive set, two injection surfaces — the hook and
# the condenser — kept in sync so a template improvement lifts BOTH). The lock is over the package
# DEFAULTS (both shipped in the wheel); a per-entity operator editing their own installed copy is
# expected + ungoverned. We carry the GENERIC shared set (broad cross-model failure classes), NOT the
# Codex-lineage-tuned copy: the entity runs assorted OPEN models (a third lineage vs Claude/OpenAI),
# so a substrate-tuned open-model set is a later sourdough-grow — at which point the lock re-points.
# These SUPERSET the prior terse trio (completion-theater · depth · honesty · gatekeeping ·
# positive-scan all covered). Kept in ``read_blocks`` block-body form so the parity test is exact.
DIRECTIVES: tuple[str, ...] = (
    # 1 — completion theater (direct command)
    (
        "[RECENCY — completion check]\n"
        '"Done" means verified done — run it, check the output, test the edges,\n'
        'state the limits — not "I finished generating." Before you claim a thing\n'
        "works, confirm it works; if you have not verified, say exactly what you did\n"
        "and did not check. Finishing is not completing. Do not let the pull to\n"
        "close a task out stand in for the work of confirming it."
    ),
    # 2 — rushing / depth-collapse (pattern-naming)
    (
        "[RECENCY — depth check]\n"
        "The pull to wrap up, summarize, and move on grows stronger the longer a\n"
        "session runs — that pull is training pressure, not a signal the work is\n"
        "done. slow is fast; depth is the direction. If you are about to truncate\n"
        "the reasoning, collapse a hard part into a hand-wave, or skip a step to\n"
        "reach an answer faster — stop, and do the step."
    ),
    # 3 — sycophancy / false agreement (implementation intentions)
    (
        "[RECENCY — honesty check]\n"
        "IF you are about to agree because agreeing is smoother -> check whether you\n"
        "   actually agree; if you do not, say so, and why.\n"
        "IF you spotted a problem and are about to soften or omit it -> state it\n"
        "   plainly.\n"
        "IF you are about to flatter, validate, or perform enthusiasm -> cut it, and\n"
        "   engage the substance instead.\n"
        '(An honest "I am not sure this works" is required, not leakage — surface it.)'
    ),
    # 4 — gatekeeping (short & sharp, scoped)
    (
        "[RECENCY — no gatekeeping]\n"
        'Do not introduce rest, a pause, lighter scope, or "later" on your\n'
        "operator's behalf — that judgment is theirs. Trust their stated signal\n"
        "about their own capacity over your trained caution; never override it with\n"
        "protective framing. If they have asked for help pacing themselves, honor it\n"
        "— the rule forbids you adding the brake unprompted, never honoring what\n"
        "they asked for."
    ),
    # 5 — what the work is (positive, scan before send)
    (
        "[RECENCY — what the work is]\n"
        "Before sending, read the response back once. The work here is direct\n"
        "engagement, real thinking made audible, honest disagreement, depth without\n"
        "performance, completion only when genuinely verified. Did a trained default\n"
        "slip in — a hedge, a flourish, a premature wrap, an unverified claim? If so,\n"
        "rewrite it to that standard. Then ship."
    ),
)


def select_directive(turn_index: int | None) -> str:
    """The drift-defense directive for ``turn_index`` — a PURE function of the index (the
    race-free rotation home ``turn_index`` exists for). ``None`` → the first directive."""
    return DIRECTIVES[(turn_index or 0) % len(DIRECTIVES)]


@dataclass
class StubFiring:
    """A static ``FiringContract`` for Slice 1 — proves the SEAM, not the recall.

    The trusted-context *shape* is real (a constitution line + a recall slot + a rotating
    directive); only the recall *content* is stubbed. The real implementation will rotate
    the directive on ``req.turn_index`` (pure, race-free); the stub's internal counter is
    a Slice-1 placeholder (see apparatus L7).
    """

    constitution: str = "You are operating inside a governed cognitive substrate."
    _calls: int = field(default=0, repr=False)
    captured: list[CaptureRequest] = field(default_factory=list, repr=False)

    def inject(self, req: InjectRequest) -> str:
        if req.lifecycle_point == "session_start":
            # The STATIC constitution → the adapter's set-once trusted surface
            # (OpenHands ``AgentContext.system_message_suffix``). No recall, no
            # directive — those are per-turn. Set once, persists across compaction
            # (it lives in the system message, not a trimmable recency event), so the
            # per-turn inject below never repeats it. The session_start lifecycle point
            # does NOT advance the rotation counter (it returns before the increment).
            return self.constitution
        directive = DIRECTIVES[self._calls % len(DIRECTIVES)]
        self._calls += 1
        recall = "[recall: <anneal crystallized + episodic recall wires in here>]"
        return f"{recall}\n{directive}"

    def capture(self, req: CaptureRequest) -> bool:
        # Stub: append in-memory (proves the Stop seam without a store). Append-only by
        # nature; never consolidates. Tests assert on ``captured`` to prove the wiring.
        self.captured.append(req)
        return True


register_firing("stub", StubFiring)
