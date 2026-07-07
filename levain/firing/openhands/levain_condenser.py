"""levain.firing.openhands — the ``LevainCondenser``: the full flow-presence adapter.

Requires the ``openhands`` extra (``pip install 'levain[openhands]'`` → openhands-sdk; the firing
adapter is self-contained in ``levain.firing``, no vagus dependency). The harness-neutral seams
(``levain.firing`` — the firing contract + the presence seam) carry NO OpenHands import; this
subpackage is the OpenHands binding.

``LevainCondenser`` **subclasses ``VagusCondenser``** and inherits its whole codex-hardened
lifecycle (compression-first inner, per-turn ``role=system`` recall + rotating directive at
recency, serialization-safe ``firing_kind`` rebuild-on-fork, capability delegation, the
direct-``View`` construction that preserves an inner's ``unhandled_condensation_request``). It
adds the ONE presence job the firing adapter structurally can NOT do — the **compaction-reinject
fold**:

  inner returns a ``Condensation`` (a compaction turn) → remember it (``pending_reanchor``) and
  pass it through unchanged (a ``Condensation`` is a forget+summary instruction, not an event
  list — there is nothing to inject into) → on the RECOVERY turn (the first ``View`` after the
  compaction) run the inherited firing inject (recall + directive) AND append a SECOND
  ``role=system`` event: the behavioral RE-ANCHOR (Claude Code's ``compaction_reinject`` — the
  compressed protocol re-assertion + current State + Top of Mind the compaction trimmed away).

The re-anchor fires **once per recovery turn following one-or-more compactions** — a burst of
consecutive ``Condensation``s (no ``View`` between) coalesces to a single re-anchor on the first
recovery ``View`` (you recover context once). Empirically, OpenHands re-invokes the condenser
WITHIN one ``run()`` after a ``Condensation`` (verified: ``call1→Condensation``, ``call2→View``),
so a compaction and its recovery happen in the same run — exactly the real deployment, where a
turn overflowing ``max_size`` compacts and recovers in place.

HONEST CAVEAT (mirrors ``VagusCondenser._inject_into`` M3): the compaction turn ITSELF still
carries no inject — that is structural, not a gap this closes. What this closes is the RECOVERY
turn running without the behavioral re-anchor, which is exactly where Claude Code's SessionStart
(compact) hook fires. Per-turn recall + directive already self-heal on that same recovery turn.

Afferent-only: it injects into the agent's OWN context (ungated). The re-anchor CONTENT comes
from a ``PresenceSource`` (READ-ONLY by contract). It never acts outward, never consolidates.
"""
from __future__ import annotations

import logging
import warnings

from openhands.sdk import LLM, Message, MessageEvent, TextContent
from openhands.sdk.context.condenser import CondenserBase
from openhands.sdk.context.view.view import View
from openhands.sdk.event.condenser import Condensation
from pydantic import PrivateAttr, model_validator

from levain.firing.contract import FiringContract, StubFiring
from levain.firing.openhands.condenser import VagusCondenser
from levain.firing.presence import PresenceSource, ReanchorRequest, StubPresence, build_presence

_log = logging.getLogger("levain.firing.openhands")


def _warn_live_override_wont_survive_fork(obj: object, kind: str, stub_type: type, label: str) -> None:
    """Mirror of ``VagusCondenser.build``'s fork-downgrade warning, generalized over the two live
    handles this adapter carries (``firing`` + ``presence``). A live NON-stub handle passed with
    the DEFAULT kind (``"stub"``) would SILENTLY downgrade a forked agent to the stub (the child
    rebuilds from the serialized kind, not the live handle) — warn loudly. Production uses a
    non-stub KIND (serialization-safe), never a live override with the default kind."""
    if kind == "stub" and not isinstance(obj, stub_type):
        warnings.warn(
            f"LevainCondenser.build({label}=<non-stub>) with the default {label}_kind='stub': "
            f"this live {label} will NOT survive fork()/reload (the child rebuilds the stub → no "
            f"real {label}). For production, pass {label}_kind=... instead of a live {label}.",
            stacklevel=3,
        )


class LevainCondenser(VagusCondenser):
    """``VagusCondenser`` (recall + directive, per turn) + the compaction-reinject fold (the
    behavioral re-anchor on the recovery turn). The full flow-presence layer on OpenHands.

    The presence SOURCE is identified by a serializable ``presence_kind`` so ``fork()`` / reload
    rebuild it (a live handle in a ``PrivateAttr`` is dropped on the serialize→validate round-trip
    fork performs — the same reason ``firing_kind`` is serialized, apparatus HIGH-1).

    ``pending_reanchor`` is a SERIALIZED FIELD (not a PrivateAttr): a fork/reload that lands between
    a compaction and its recovery turn MUST still fire the re-anchor — dropping it is a miss at the
    exact boundary this adapter exists to cover (codex L3 HIGH). This differs from the inherited
    ``_turn`` (a PrivateAttr whose reset only repeats a directive — cosmetic); a lost re-anchor is
    functional, so it rides serialization. Non-atomic set/read is fine under OpenHands' one-instance-
    per-conversation-per-loop contract (same basis as the parent's ``_turn`` note).
    """

    presence_kind: str = "stub"
    # SERIALIZED (survives fork) — see the class docstring. True between a compaction and its
    # recovery turn: the re-anchor is due on the next View.
    pending_reanchor: bool = False
    _presence: PresenceSource | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _ensure_presence(self) -> "LevainCondenser":
        # Runs on construction AND on deserialization (fork/reload), alongside the inherited
        # ``_ensure_firing`` — both build their live handle from the serialized kind. (Verified in
        # apparatus L2/L3: pydantic keys model-validators by name, so a differently-named subclass
        # validator does not shadow the inherited one; both fire.)
        if self._presence is None:
            self._presence = build_presence(self.presence_kind)
        return self

    @classmethod
    def build(
        cls,
        inner: CondenserBase,
        firing: FiringContract | None = None,
        firing_kind: str = "stub",
        presence: PresenceSource | None = None,
        presence_kind: str = "stub",
    ) -> "LevainCondenser":
        """Construct with both serializable kinds, then apply any live handles (test doubles /
        in-process handles). A live handle does NOT survive fork — production passes a non-stub
        KIND, never a live override with the default kind (see the fork-downgrade warning).

        Signature keeps ``firing``/``firing_kind`` positional to match ``VagusCondenser.build`` (LSP);
        ``presence``/``presence_kind`` are trailing optionals a subclass may add."""
        obj = cls(inner=inner, firing_kind=firing_kind, presence_kind=presence_kind)
        if firing is not None:
            _warn_live_override_wont_survive_fork(firing, firing_kind, StubFiring, "firing")
            obj._firing = firing
        if presence is not None:
            _warn_live_override_wont_survive_fork(presence, presence_kind, StubPresence, "presence")
            obj._presence = presence
        return obj

    # --- the compaction-reinject fold (override the full lifecycle) --------------------

    def condense(self, view: View, agent_llm: LLM | None = None) -> View | Condensation:
        result = self.inner.condense(view, agent_llm)
        if isinstance(result, Condensation):
            self.pending_reanchor = True  # compaction turn → re-anchor is due on the recovery turn
            return result
        # inherited: compression-first inner already ran; inject recall + directive (advances _turn)
        injected = self._inject_into(result)
        return self._maybe_reanchor(injected)

    async def acondense(self, view: View, agent_llm: LLM | None = None) -> View | Condensation:
        # Mirror condense on the async path — the base acondense would call the inner's SYNC
        # condense (blocks the loop, bypasses async-only inner behavior); VagusCondenser already
        # overrides acondense for the firing inject, and the fold must ride the async path too.
        # (NOTE: PresenceSource.reanchor is sync, so a real I/O-backed source blocks the loop here —
        # the same accepted pattern as the parent's sync FiringContract.inject; Slice 4 decides an
        # async variant when a real content source ships.)
        result = await self.inner.acondense(view, agent_llm)
        if isinstance(result, Condensation):
            self.pending_reanchor = True
            return result
        injected = self._inject_into(result)
        return self._maybe_reanchor(injected)

    def _maybe_reanchor(self, view: View) -> View:
        """Append the behavioral re-anchor at recency iff a compaction is pending. Fires once per
        recovery turn following one-or-more compactions. A raised failure RE-ARMS (retry next
        recovery turn — a transient store read on the heavy recovery turn is exactly when a real
        source flakes); a clean result (text or a legitimate empty) CONSUMES."""
        if not self.pending_reanchor:
            return view
        # Slice 1: StubPresence ignores the query, so it is left empty (real query derivation —
        # reusing the firing's recent-user-text signal — wires in with the content-aware presence
        # source, Slice 4). turn_index is the just-advanced firing turn (any rotation a source wants).
        text, ok = self._safe_reanchor(ReanchorRequest(query="", turn_index=self._turn))
        if not ok:
            # The source raised → keep pending_reanchor True so the next recovery turn retries.
            # _safe_reanchor already logged the loss (fail-soft but LOUD). Do NOT consume.
            return view
        self.pending_reanchor = False  # clean result → consume: at most one re-anchor per recovery
        if not text:
            return view  # nothing to re-anchor (a source with no state to re-assert) → no event
        reanchor_event = MessageEvent(
            source="environment",  # substrate-injected, not agent-authored (excluded from capture)
            sender="levain",  # distinguishes the presence re-anchor from the firing recall (sender="vagus")
            llm_message=Message(role="system", content=[TextContent(text=text)]),
        )
        # Direct View() construction (never View.from_events) — from_events re-runs
        # enforce_properties, which can silently drop the inner's unhandled_condensation_request
        # flag or re-trim (the M5 lesson, inherited from VagusCondenser._inject_into).
        return View(
            events=[*view.events, reanchor_event],
            unhandled_condensation_request=view.unhandled_condensation_request,
        )

    def _safe_reanchor(self, req: ReanchorRequest) -> tuple[str | None, bool]:
        """Call the presence source fail-soft. Returns ``(text, ok)``:
          - ``ok=True``  → a clean result (``text`` is the re-anchor, or ``None`` for "nothing to
            re-assert"); the caller CONSUMES the pending flag.
          - ``ok=False`` → the source RAISED; the caller RE-ARMS (retry next recovery turn), and
            this logs the loss LOUD (a dropped re-anchor is a loss, not a no-op — the ``capture``
            "fail-soft but LOUD" convention; distinct from a legitimate empty, which is silent).

        The self-heal ``build_presence`` AND the ``reanchor`` call are BOTH inside the fail-soft
        boundary: ``build_presence`` raises on an unknown kind, so a bad ``presence_kind`` reaching
        the runtime self-heal path (``model_construct`` bypasses the validator) must degrade to a
        retryable no-re-anchor, NEVER crash the agent's turn (codex + complement L3 HIGH — the
        fail-OPEN the earlier build-before-try left open). Construction/deserialization with a bad
        kind still fails FAST (the validator raises) — the right asymmetry (surface a config error
        at build time; never crash a live turn)."""
        try:
            presence = self._presence
            if presence is None:
                # Self-heal on unsafe construction paths (model_construct bypasses the validator) —
                # a structural guard, not an -O-strippable assert (mirrors VagusCondenser).
                presence = self._presence = build_presence(self.presence_kind)
            text = presence.reanchor(req)
            if text is not None and not isinstance(text, str):
                # A source DEFECT: the contract is str | None, but a buggy source returned something
                # else (e.g. structured chunks). Don't let it crash the turn at TextContent(text=...)
                # downstream (codex L3 round-2 LOW). A type error won't self-heal, so log + drop +
                # CONSUME (return None, ok=True) rather than re-arm-and-retry-spam a permanent bug.
                _log.warning(
                    "levain re-anchor: source returned %s, expected str|None — dropping",
                    type(text).__name__,
                )
                return None, True
            return text, True
        except Exception as e:  # noqa: BLE001 — fail-soft IS the contract at the afferent seam
            _log.warning(
                "levain re-anchor FAILED — dropped this recovery turn, will retry (%s): %s",
                type(e).__name__,
                e,
            )
            return None, False
