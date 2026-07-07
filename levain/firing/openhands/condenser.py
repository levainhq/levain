"""The OpenHands firing-adapter: ``VagusCondenser``.

Owns the trusted per-turn assembly seam on UNMODIFIED OpenHands. The 2026-06-07 spike
mapped the boundary: native OpenHands hooks carry *capture* (full) + *benign user-role
recall*, but the trusted, system-role, per-turn inject — the moat — has no native
surface (``SessionStart`` ``additionalContext`` is discarded; the only trusted surface,
``AgentContext.system_message_suffix``, is set-once/static). A **condenser** is the one
extension point that injects ``role=system`` content *dynamically every turn*.

So this adapter composes, across the FULL condenser lifecycle (sync + async + capability
flags — not just the happy-path ``condense``):

  inner ``CondenserBase`` (compression — called FIRST so the inject is never summarized
  away or counted toward the size threshold) → then inject the afferent
  ``vagus.firing`` context as a ``role=system`` ``MessageEvent`` at recency.

Afferent-only: it injects recall + the drift-defense directive into the agent's OWN
context. It never acts outward and never consolidates.
"""
from __future__ import annotations

from openhands.sdk import LLM, Message, MessageEvent, TextContent
from openhands.sdk.context.condenser import CondenserBase
from openhands.sdk.context.view.view import View
from openhands.sdk.event.condenser import Condensation
from pydantic import PrivateAttr, model_validator

from levain.firing.contract import FiringContract, InjectRequest, StubFiring, build_firing


def _recent_user_text(view: View) -> str:
    """Derive the recall query from the View: the most recent USER-authored message text,
    falling back to the most recent message of ANY role when no user turn carries text (e.g.
    a user turn summarized away by the inner condenser — the post-compaction turn where recall
    matters most). A real-recall firing (``AnnealFiring``) recalls against this; recalling into
    own-context against own-context is pure afferent. Empty only when no message carries text
    (recall then no-ops)."""
    fallback = ""
    for event in reversed(view.events):
        if not isinstance(event, MessageEvent):
            continue
        msg = event.llm_message
        if msg is None:
            continue
        text = " ".join(
            getattr(c, "text", "") for c in (msg.content or []) if getattr(c, "text", None)
        ).strip()
        if not text:
            continue
        if getattr(msg, "role", None) == "user":
            return text  # prefer the most recent USER text (the human's intent)
        fallback = fallback or text  # else remember the most recent any-role text
    return fallback


class VagusCondenser(CondenserBase):
    """Compression (inner) + afferent injection (firing) on every turn, across the full
    condenser lifecycle.

    The firing SOURCE is identified by a serializable ``firing_kind`` so ``fork()`` /
    reload rebuild the right source — a live handle in a ``PrivateAttr`` is silently
    dropped on the serialize→validate round-trip fork performs (apparatus HIGH-1). The
    live handle is built from the kind by :meth:`_ensure_firing`, which runs on
    construction AND on deserialization.
    """

    inner: CondenserBase
    firing_kind: str = "stub"
    _firing: FiringContract | None = PrivateAttr(default=None)
    # A monotonic per-inject turn counter — the directive-rotation index. NOT View-derived:
    # `len(view.events)` is non-monotonic (the View shrinks on compaction) AND collides under a
    # fixed-window inner (LLMSummarizingCondenser pins the count → the same directive every turn,
    # silently killing rotation — apparatus L1/L2 MED). A private counter is monotonic regardless
    # of View size; it resets to 0 on fork/reload (PrivateAttr isn't serialized), which is fine —
    # rotation CONTINUITY across a fork is not load-bearing, only that consecutive turns differ.
    # Assumes one condenser instance per conversation on one event loop (the OpenHands contract):
    # `self._turn += 1` is not cross-thread atomic, but a lost increment would at worst repeat a
    # directive — tolerable for a rotation index, not worth a lock (apparatus L3 codex LOW).
    _turn: int = PrivateAttr(default=0)

    @model_validator(mode="after")
    def _ensure_firing(self) -> "VagusCondenser":
        if self._firing is None:
            self._firing = build_firing(self.firing_kind)
        return self

    @classmethod
    def build(
        cls,
        inner: CondenserBase,
        firing: FiringContract | None = None,
        firing_kind: str = "stub",
    ) -> "VagusCondenser":
        obj = cls(inner=inner, firing_kind=firing_kind)
        if firing is not None:
            # A live override (test double / in-process handle). It does NOT survive fork()/
            # reload — the child rebuilds from firing_kind. A real (non-stub) firing passed with
            # the DEFAULT firing_kind="stub" therefore SILENTLY downgrades a forked agent to
            # StubFiring (no real recall) — warn loudly (apparatus L3 codex MED). Production real
            # recall MUST pass firing_kind="anneal" (no live override), which is serialization-safe.
            if firing_kind == "stub" and not isinstance(firing, StubFiring):
                import warnings

                warnings.warn(
                    "VagusCondenser.build(firing=<non-stub>) with the default firing_kind='stub': "
                    "this live firing will NOT survive fork()/reload (the child rebuilds StubFiring "
                    "→ no real recall). For production, pass firing_kind=... instead of a live firing.",
                    stacklevel=2,
                )
            obj._firing = firing
        return obj

    # --- the full condenser contract (wrap the whole lifecycle, not just condense) ----

    def handles_condensation_requests(self) -> bool:
        # Delegate the capability flag — else we MASK the inner's condensation-recovery
        # (LLMSummarizingCondenser returns True; the base default is False), so OpenHands
        # would not trigger recovery after context-window / malformed-history errors.
        return self.inner.handles_condensation_requests()

    def condense(self, view: View, agent_llm: LLM | None = None) -> View | Condensation:
        result = self.inner.condense(view, agent_llm)
        return result if isinstance(result, Condensation) else self._inject_into(result)

    async def acondense(
        self, view: View, agent_llm: LLM | None = None
    ) -> View | Condensation:
        # Mirror condense on the async path — else the base acondense calls the inner's
        # SYNC condense (blocks the loop, bypasses async-only inner behavior).
        result = await self.inner.acondense(view, agent_llm)
        return result if isinstance(result, Condensation) else self._inject_into(result)

    # --- injection (shared by sync + async paths) -------------------------------------

    def _inject_into(self, result: View) -> View:
        """Append the trusted afferent context at recency.

        Only the View branch reaches here; a Condensation (compaction turn) is passed
        through unchanged by the callers — it is a forget+summary INSTRUCTION, not an
        event list. The inject is recomputed fresh every turn and never persisted into the
        event log, so it returns on the next normal turn. HONEST CAVEAT (apparatus M3):
        the immediate post-compaction step therefore runs without the directive — the one
        turn where drift is highest. Acceptable for Slice 1; re-asserting on that turn is
        tracked, NOT "free".
        """
        # Self-heal even on unsafe construction paths (model_construct bypasses the
        # validator) — a structural guard, not an -O-strippable assert.
        if self._firing is None:
            self._firing = build_firing(self.firing_kind)
        # Feed the firing the agent's own recent context (the recall query) + the monotonic
        # turn index (for race-free directive rotation that a fixed-window inner can't freeze).
        # A stub firing ignores both; a real-recall firing recalls against the query.
        self._turn += 1
        text = self._firing.inject(InjectRequest(
            lifecycle_point="per_turn",
            query=_recent_user_text(result),
            turn_index=self._turn,
        ))
        inject_event = MessageEvent(
            source="environment",  # substrate-injected context, not agent-authored output
            sender="vagus",
            llm_message=Message(role="system", content=[TextContent(text=text)]),
        )
        # Construct the View DIRECTLY (not via View.from_events): from_events re-runs
        # enforce_properties, which for an arbitrary inner can silently drop a View-level
        # unhandled_condensation_request flag or re-trim against a narrower all_events.
        # result.events is already a flat post-condensation list (no embedded Condensation).
        return View(
            events=[*result.events, inject_event],
            unhandled_condensation_request=result.unhandled_condensation_request,
        )
