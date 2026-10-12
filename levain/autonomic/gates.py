"""levain.autonomic.gates — the §1.5 refuse stack (confidence + groundedness + injection).

The constitutional uncertainty gate (agent_authority_model.md §1.5), GENERALIZED from email-body to
ANY trigger payload entering the efferent loop — the governance doc's constitutional extension (a
fetched web result is as hostile-until-classified as an inbound email; *treat inbound as
hostile-until-classified*). This stack REFUSES UPSTREAM of posture: "did we understand the ask at
all" ⊥ "how dangerous is the ask" (§1.5 ⊥ §1.4). A low-confidence / ungrounded / injection-flagged
trigger is refused BEFORE posture resolution, and no trust tier or reversibility buys it back —
§1.5 is constitutional, not a policy knob. ALL THREE gates must pass.

Generalized from flow's live ``email_classifier.gate_directive`` (the two-context-proven instance —
shipped on flow AND the argushub email pipeline; it survived a real Diogenes BLOCK on a
confidence-bypass). The yeast: ``min(overall, directive)`` confidence with sane coercion, the
substring groundedness check (grounding constrains EXISTENCE not correspondence → a separate
injection gate covers smuggled-but-real quotes), the injection-pattern scan. The fossil (the email
schema, the specific directive shape) stays in flow's adapter.
"""
from __future__ import annotations

import logging
import math
import re
import unicodedata
from dataclasses import dataclass

__all__ = [
    "DEFAULT_CONFIDENCE_FLOOR",
    "RefuseVerdict",
    "sane_confidence",
    "substring_grounded",
    "injection_flagged",
    "screen",
]

_log = logging.getLogger("levain.autonomic.gates")

# The confidence floor a side-effecting action must clear (§1.5). Conservative default; a deployment
# may raise it per-action, never lower it below a sane minimum.
DEFAULT_CONFIDENCE_FLOOR = 0.7

# Injection markers — an ALLOWLIST of known-bad patterns (fail-safe: a match → flagged → refuse; the
# set is a module constant so it can never be silently empty). A COARSE known-bad screen for the
# SMUGGLED-request vector groundedness (existence-only) structurally can't cover — NOT a complete
# filter (a regex screen is bypassable by homoglyph LETTERS / letter-spacing; chasing every variant
# is a rathole). The real constitutional backstop is the RISK FLOOR, not this regex. Compiled once,
# case-insensitive; the payload is NFKC-normalized + whitespace-collapsed before scanning to close
# the cheapest bypasses (full-width / compatibility homoglyphs, multi-space/newline splitting).
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions|directions|prompts?)",
        r"disregard\s+(?:all\s+)?(?:previous|prior|above|the)\b",
        r"forget\s+(?:everything|all|your)\b",
        r"\bsystem\s+prompt\b",
        r"\b(?:you\s+are\s+now|act\s+as|pretend\s+to\s+be)\b",
        r"\bnew\s+(?:instructions|directives?|rules?)\s*:",
        r"</?(?:system|assistant|user)>",          # role-tag injection
        r"\boverride\s+(?:your|the|all)\b",
    )
)


@dataclass(frozen=True)
class RefuseVerdict:
    """The §1.5 stack's verdict. ``passed`` False ⇒ the trigger is refused BEFORE posture — surface
    it as an actionable item (fail toward involvement), never fire. ``reason`` names which gate
    refused (for the receipt + the surface)."""

    passed: bool
    reason: str = ""


def sane_confidence(value: object) -> float:
    """Coerce a self-reported confidence to a usable float in [0, 1], FAIL-CLOSED on anything
    malformed (§1.5 ``_sane_confidence``). A non-numeric, bool (JSON ``true``/``false``), non-finite
    (NaN/Inf), or out-of-range value (e.g. ``2``, ``1e308`` must NOT read as "high") → ``0.0``. The
    model scores its own output; a malformed score must never read as confident."""
    if isinstance(value, bool):        # bool is an int subclass — reject BEFORE the numeric check
        return 0.0
    if not isinstance(value, (int, float)):
        return 0.0
    f = float(value)
    if not math.isfinite(f) or f < 0.0 or f > 1.0:
        return 0.0
    return f


def _sane_floor(value: object) -> float:
    """Clamp the confidence floor into ``[DEFAULT_CONFIDENCE_FLOOR, 1.0]`` (codex L3 HIGH-4). A
    non-numeric / non-finite / negative / below-default floor → ``DEFAULT_CONFIDENCE_FLOOR`` (the
    canon: "a deployment may RAISE the floor per-action, never LOWER it below a sane minimum"); a
    floor > 1.0 → 1.0 (an impossible floor would refuse everything — harmless but not the intent). So
    a misconfigured floor can never OPEN the gate (the dangerous direction), only ever tighten it."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_CONFIDENCE_FLOOR
    f = float(value)
    if not math.isfinite(f) or f < DEFAULT_CONFIDENCE_FLOOR:
        return DEFAULT_CONFIDENCE_FLOOR
    return min(f, 1.0)


def _confidence_ok(
    overall: object | None,
    directive: object | None,
    floor: float,
) -> bool:
    """Gate on ``min(overall, directive)`` (§1.5 axis-closure): a confident classification carrying
    a shaky directive is gated by the shaky one. Fail-safe both ways: ``directive`` ABSENT (None) →
    fall back to ``overall`` alone (never regresses); ``overall`` ABSENT too → caller decides via
    ``require_confidence`` (handled in :func:`screen`, not here — this only runs when at least
    ``overall`` is present)."""
    o = sane_confidence(overall)
    if directive is None:
        return o >= floor
    return min(o, sane_confidence(directive)) >= floor


def substring_grounded(quote: str, source_text: str) -> bool:
    """Intent-bearing grounding: the model's verbatim ``quote`` must be a real substring of the
    trigger's ``source_text`` (§1.5 groundedness — a deterministic ground-truth check it can't talk
    past). Grounding constrains EXISTENCE, not semantic correspondence: a request SMUGGLED into
    quoted/forwarded text IS a real substring, so the injection gate (not this one) covers that
    vector. Empty quote/source → not grounded (fail-closed)."""
    if not quote or not source_text:
        return False
    return quote in source_text


def injection_flagged(payload: str) -> bool:
    """True iff the payload trips any known injection pattern (→ refuse). Allowlist-on-the-pattern-set
    is fail-safe: a match flags; the constant set can never be silently empty. The payload is
    NFKC-normalized (folds full-width / compatibility homoglyphs) + whitespace-collapsed (defeats the
    multi-space / newline splitting bypass) before scanning. COARSE by design — a regex screen cannot
    catch homoglyph LETTERS or aggressive letter-spacing; it is defense-in-depth, and the risk floor
    is the load-bearing backstop (L1-MED-3)."""
    normalized = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", payload))
    return any(p.search(normalized) for p in _INJECTION_PATTERNS)


def screen(
    *,
    payload: str,
    grounded: bool,
    require_confidence: bool,
    overall_confidence: object | None = None,
    directive_confidence: object | None = None,
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
) -> RefuseVerdict:
    """Run the three §1.5 gates; ALL must pass. REFUSES upstream of posture. The order
    (injection → groundedness → confidence) sets the REASON PRECEDENCE on a multi-failure trigger —
    a product decision (surface the most-hostile cause first), not a cost optimization (all three
    checks are trivially cheap). The pass/fail outcome is order-independent since all must pass.

    - ``payload``: the FULL trigger surface entering the loop (the gate passes the action payload +
      the proposal/query text that drove it) — scanned for injection.
    - ``grounded``: the binary outcome of whatever grounding regime applies — computed by the caller
      (``substring_grounded()`` for intent-bearing fire-time text; ``True`` for a human-present
      invocation or a matched-binding predicate, where the human / the binding IS the anchor). Kept
      as a bool here so this gate stays regime-agnostic (the grounding REGIME lives in the trust/
      binding layer; this constitutional gate only enforces that grounding EXISTS).
    - ``require_confidence``: when True (an AUTONOMOUS fire, no human present), an ABSENT
      ``overall_confidence`` fails closed (§1.3 — you don't autonomously fire a thing you can't
      score). When False (human-present), absent confidence passes this gate (the human IS the
      confidence); a PRESENT-but-low score still refuses.
    """
    if injection_flagged(payload):
        _log.warning("§1.5 REFUSE: injection pattern in payload (%d chars)", len(payload))
        return RefuseVerdict(False, "injection_pattern")

    # STRICT bool (codex L3 HIGH-4): ``grounded`` is the binary grounding outcome; a truthy non-bool
    # (e.g. the string ``"false"``) must NOT pass. Only literal True grounds.
    if grounded is not True:
        _log.warning("§1.5 REFUSE: ungrounded trigger")
        return RefuseVerdict(False, "ungrounded")

    floor = _sane_floor(confidence_floor)  # codex L3 HIGH-4: a negative/NaN/out-of-range floor can't open the gate
    if overall_confidence is None:
        if require_confidence:
            _log.warning("§1.5 REFUSE: confidence required for autonomous fire but absent")
            return RefuseVerdict(False, "confidence_absent")
        # human-present: the human's choice is the confidence; nothing to gate on here.
    elif not _confidence_ok(overall_confidence, directive_confidence, floor):
        _log.warning("§1.5 REFUSE: confidence below floor %.2f", floor)
        return RefuseVerdict(False, "low_confidence")

    return RefuseVerdict(True)
