"""levain.autonomic.kill — the DIVERSE, fail-safe kill-evaluation substrate (Slice 3a.5).

A guard's ``kill_predicate`` is the deterministic "stop if X" the human PRODUCES at ratification (the
known-danger backstop). 3c proved a kill is PRESENT + deterministic + drill-bearing; this module is
the 3a.5 hardening that makes a kill load-bearing once a binding GRADUATES to on-loop (where no human
is in the room at fire-time):

  - **kill-purity** (``assert_kill_pure``): the kill reduces to a pure, read-only, deterministic
    predicate over event fields — no effector node, no fire-time judgment, no op this substrate can't
    evaluate. Corpus-AGNOSTIC + vagus-core-OWNED: the diverse substrate validates its OWN input rather
    than trusting that flow's ``events.validate_predicate`` did (substrate independence at the
    validation layer too).

  - **a DIVERSE substrate** (``kill_outcome`` / ``kill_trips``): the kill is evaluated by an
    INDEPENDENT evaluator — re-implemented here, NOT flow's ``events.py`` (the anti-cycle rule
    forbids the import anyway). The diversity is LOAD-BEARING, not incidental. Today (3c) a kill and
    its trigger were COMMON-MODE: same ``events.py`` implementation, same ``_eval_leaf`` absent-field
    semantics. A blind spot shared by both (the H2 footgun: ``events._eval_leaf`` returns FALSE on an
    absent field, so a positive-depth ``dmarc != pass`` kill silently FAILS TO TRIP on no-DMARC mail —
    the action fires on exactly the worst, unauthenticated case) disables the trigger AND the kill at
    once. ``cross_substrate_review_codex_nonreplaceable`` applied to the gate itself: the kill must be
    on a substrate whose failure modes DIFFER from the trigger's, or it is not a real backstop.

The diversity is realized as **three-valued (Kleene) logic with FAIL-SAFE-TRIP**: each node evaluates
to ``TRUE`` / ``FALSE`` / ``UNKNOWN``; a kill TRIPS unless the predicate is CONFIDENTLY ``FALSE``
(``kill_trips = outcome != FALSE``). The inversion of ``events.py``'s absent→False is the whole point:

    kill ``dmarc != pass``,  no-dmarc event:
      events.py    →  dmarc absent → ``!=`` returns False → kill does NOT trip → action FIRES (the bug)
      this module  →  dmarc absent → ``!=`` is UNKNOWN     → kill TRIPS (action does not fire) (fixed)

So a common-mode absent-field event that the trigger fires on TRIPS this kill regardless of how the
author spelled it (``!=`` or the fail-safe ``not(== )`` both go non-FALSE on absence). The absent-
handling footgun, which 3c could only SURFACE as a warning the human had to heed, is now handled
STRUCTURALLY by the evaluator (``structural_invariants_beat_discipline``).

The comparison semantics on a PRESENT, evaluable field MIRROR ``events.py`` (casefold string eq,
numeric coercion, bool-strict) — the diversity is purely in the FAILURE mode (absent / type-mismatch /
unknown op → UNKNOWN → trip), never in the confident-case logic. A kill authored against ``events.py``
semantics evaluates identically here on the cases it can confidently decide, and SAFER on the rest.

Two evaluation contracts, deliberately different:
  - ``assert_kill_pure`` (COMPILE-time, STRICT): RAISES ``KillImpurityError`` on any unsupported op /
    malformed node / depth-exceed / non-canonical leaf — a bad kill is REFUSED at compile, never
    minted.
  - ``_kleene_eval`` (RUN-time, FAIL-SAFE): NEVER raises — any anomaly (even one that slipped past the
    compile gate) degrades to ``UNKNOWN`` → trip. At fire-time a fail-safe trip beats a crashed gate.

Stdlib-only; corpus-agnostic; pure. Imports NOTHING from flow (the anti-cycle rule).
"""
from __future__ import annotations

import math
from enum import Enum
from typing import Any

__all__ = [
    "Kleene",
    "KillImpurityError",
    "KILL_LEAF_OPS",
    "KILL_COMPOSITE_OPS",
    "MAX_KILL_DEPTH",
    "assert_kill_pure",
    "kill_purity_error",
    "kill_outcome",
    "kill_trips",
]


class Kleene(Enum):
    """Three-valued (Kleene/Priest) truth for the diverse kill evaluator. ``UNKNOWN`` is the
    fail-safe state: the evaluator could not CONFIDENTLY decide the node (an absent field, a
    type-incompatible comparison, an unknown op). A kill TRIPS on anything that is not confidently
    ``FALSE`` — ``UNKNOWN`` trips, because "I cannot confirm this action is safe" must STOP an
    autonomous fire, not permit it."""

    FALSE = 0
    UNKNOWN = 1
    TRUE = 2

    def __bool__(self) -> bool:  # guard against `if kleene:` — force an explicit comparison
        raise TypeError("Kleene is three-valued; compare explicitly (== Kleene.TRUE), never truthy-test")


class KillImpurityError(ValueError):
    """Raised by :func:`assert_kill_pure` when a kill predicate is not a pure, read-only, deterministic
    predicate this substrate can evaluate. A ``ValueError`` subclass so the compiler's broad
    ``except Exception`` around predicate validation catches it uniformly with the validator's raises."""


# The op vocabulary this substrate evaluates — the SAME deterministic grammar as flow's events.py
# (so a kill authored there is evaluable here), re-declared so the diverse substrate owns its own
# contract rather than importing the corpus's (the anti-cycle rule + substrate independence).
KILL_LEAF_OPS = frozenset({"==", "!=", "contains", "in", ">", ">=", "<", "<=", "exists"})
KILL_COMPOSITE_OPS = frozenset({"and", "or", "not"})
MAX_KILL_DEPTH = 32  # mirror events._MAX_PREDICATE_DEPTH (stack / pathological-nesting guard)

_NUMERIC_OPS = frozenset({">", ">=", "<", "<="})
_ABSENT = object()  # sentinel distinct from a stored JSON null (which also resolves to _ABSENT)


# --- purity (the strict COMPILE-time gate) -----------------------------------------------------


def kill_purity_error(predicate: Any, *, _depth: int = 0) -> str | None:
    """Return a human-readable reason the ``predicate`` is NOT a pure, read-only, deterministic kill
    this substrate can evaluate, or ``None`` if it is pure. Pure ⇒ a well-formed AST over
    ``KILL_COMPOSITE_OPS`` + ``KILL_LEAF_OPS``, every leaf a non-empty ``field`` string, every leaf
    ``value`` a finite JSON scalar (or a finite-scalar list for ``in``), depth within
    ``MAX_KILL_DEPTH``, dict keys all strings (canonical-JSON-unambiguous). Read-only is STRUCTURAL: a
    predicate AST has no node that can invoke a tool or mutate state — it only READS fields — so
    "read-only" is guaranteed by refusing any node shape that is not this grammar.

    Pure AST-walk; corpus-agnostic; never evaluates against an event. Mirrors (does not import)
    ``events.validate_predicate`` so the diverse substrate validates its OWN input."""
    if _depth > MAX_KILL_DEPTH:
        return f"kill predicate nests past {MAX_KILL_DEPTH} (pathological — refused)"
    if not isinstance(predicate, dict):
        return f"kill predicate node must be a JSON object, got {type(predicate).__name__}"
    op = predicate.get("op")
    if op in ("and", "or"):
        clauses = predicate.get("clauses")
        if not isinstance(clauses, list) or not clauses:
            return f"{op!r} requires a NON-EMPTY 'clauses' list (an empty composite is vacuous)"
        for c in clauses:
            err = kill_purity_error(c, _depth=_depth + 1)
            if err is not None:
                return err
        return None
    if op == "not":
        if "clause" not in predicate:
            return "'not' requires a 'clause'"
        return kill_purity_error(predicate["clause"], _depth=_depth + 1)
    if op not in KILL_LEAF_OPS:
        return (f"unknown/non-deterministic kill op {op!r} — the kill must reduce to the deterministic "
                f"read-only grammar {sorted(KILL_LEAF_OPS | KILL_COMPOSITE_OPS)} (no fire-time judgment)")
    field = predicate.get("field")
    if not isinstance(field, str) or not field:
        return f"leaf op {op!r} requires a non-empty 'field' string"
    if op == "exists":
        return None
    if op == "in":
        vlist = predicate.get("value")
        if not isinstance(vlist, list):
            return "'in' requires a list 'value'"
        if not vlist:
            # a kill `x in []` matches NOTHING → never trips → a vacuous kill that gives false
            # assurance (a fail-open class). Stricter than the trigger validator BECAUSE it is a KILL
            # (complement L3 LOW-2): a never-matching trigger is merely useless, a never-tripping kill
            # is dangerous.
            return "'in' value list must not be empty (a kill that matches nothing is no kill)"
        for item in vlist:
            if item is None:
                return "'in' list must not contain null (use 'not exists' to test absence)"
            if not isinstance(item, (str, int, float, bool)):
                return f"'in' list item must be a JSON scalar, got {type(item).__name__}"
            if isinstance(item, float) and not math.isfinite(item):
                return "'in' list contains a non-finite number (NaN/Inf)"
        return None
    if "value" not in predicate:
        return f"leaf op {op!r} requires a 'value'"
    val = predicate["value"]
    if val is None:
        return f"leaf op {op!r} 'value' must not be null — use the 'exists' op (under 'not' for absence)"
    if not isinstance(val, (str, int, float, bool)):
        return f"leaf op {op!r} 'value' must be a JSON scalar, got {type(val).__name__}"
    if isinstance(val, float) and not math.isfinite(val):
        return f"leaf op {op!r} 'value' must be finite (a non-finite threshold never matches)"
    if op in _NUMERIC_OPS and _as_number(val) is None:
        return f"numeric op {op!r} requires a finite-numeric 'value', got {val!r}"
    return None


def assert_kill_pure(predicate: Any) -> None:
    """Strict COMPILE-time purity gate: raise :class:`KillImpurityError` iff ``predicate`` is not a
    pure, read-only, deterministic kill this substrate can evaluate (see :func:`kill_purity_error`).
    The compiler calls this on every kill-bearing guard before it accepts a binding — a bad kill is
    REFUSED at compile, never sealed into a standing grant."""
    err = kill_purity_error(predicate)
    if err is not None:
        raise KillImpurityError(err)


# --- the diverse evaluator (the fail-safe RUN-time path) ---------------------------------------


def kill_outcome(predicate: Any, event: Any) -> Kleene:
    """Evaluate a kill ``predicate`` against ONE ``event`` dict on the DIVERSE substrate → a
    :class:`Kleene`. NEVER raises (the run-time contract): a malformed node, an unknown op, a depth
    blow-out, or any internal anomaly degrades to ``UNKNOWN`` (fail-safe — a kill that can't be
    confidently evaluated must be treated as POSSIBLY tripping). Use :func:`kill_trips` for the bool.

    ``event`` may be a real event (``{"fields": {...}, "type": ...}``) OR a flat dict (a hand-authored
    ``kill_drill`` like ``{"dmarc": "fail"}``) — field resolution is permissive (fields-namespace dig,
    then top-level) so a drill need not carry the full event envelope."""
    if not isinstance(event, dict):
        return Kleene.UNKNOWN
    return _kleene_eval(predicate, event, 0)


def kill_trips(predicate: Any, event: Any) -> bool:
    """True iff the kill TRIPS on ``event`` — i.e. the outcome is NOT confidently ``FALSE``. The
    fail-safe direction: a confidently-false predicate is the ONLY case the action is allowed to
    proceed; ``TRUE`` (the danger is present) and ``UNKNOWN`` (the danger can't be ruled out) both
    STOP the fire."""
    return kill_outcome(predicate, event) is not Kleene.FALSE


def _kleene_eval(node: Any, event: dict[str, Any], depth: int) -> Kleene:
    """The fail-safe three-valued evaluator. Any structural anomaly → ``UNKNOWN`` (never raises)."""
    if depth > MAX_KILL_DEPTH or not isinstance(node, dict):
        return Kleene.UNKNOWN
    op = node.get("op")
    if op == "and":
        clauses = node.get("clauses")
        if not isinstance(clauses, list) or not clauses:
            return Kleene.UNKNOWN
        results = [_kleene_eval(c, event, depth + 1) for c in clauses]
        if any(r is Kleene.FALSE for r in results):
            return Kleene.FALSE       # one false clause makes the conjunction false, even amid unknowns
        if any(r is Kleene.UNKNOWN for r in results):
            return Kleene.UNKNOWN
        return Kleene.TRUE
    if op == "or":
        clauses = node.get("clauses")
        if not isinstance(clauses, list) or not clauses:
            return Kleene.UNKNOWN
        results = [_kleene_eval(c, event, depth + 1) for c in clauses]
        if any(r is Kleene.TRUE for r in results):
            return Kleene.TRUE        # one true clause makes the disjunction true, even amid unknowns
        if any(r is Kleene.UNKNOWN for r in results):
            return Kleene.UNKNOWN
        return Kleene.FALSE
    if op == "not":
        if "clause" not in node:
            return Kleene.UNKNOWN
        inner = _kleene_eval(node["clause"], event, depth + 1)
        if inner is Kleene.TRUE:
            return Kleene.FALSE
        if inner is Kleene.FALSE:
            return Kleene.TRUE
        return Kleene.UNKNOWN          # not(UNKNOWN) is UNKNOWN
    if op not in KILL_LEAF_OPS:
        return Kleene.UNKNOWN          # an unknown op can't be evaluated → fail-safe trip
    return _eval_leaf(node.get("field"), op, node.get("value"), event)


def _eval_leaf(field: Any, op: str, value: Any, event: dict[str, Any]) -> Kleene:
    """Evaluate ONE leaf, fail-safe. ``exists`` is well-defined on absence (FALSE); every other op on
    an ABSENT field — or a type-incompatible comparison — is ``UNKNOWN`` (the diverse inversion of
    ``events.py``'s deterministic False-on-absent: here, "can't decide" trips)."""
    v = _resolve_field(field, event)
    if op == "exists":
        return Kleene.TRUE if v is not _ABSENT else Kleene.FALSE
    if v is _ABSENT:
        return Kleene.UNKNOWN          # the fail-safe inversion: absent → can't decide → trip
    if op in ("==", "!="):
        # BOTH equality ops fail-safe SYMMETRICALLY (codex L3): a present-but-type-INCOMPATIBLE compare
        # (a non-numeric string vs a number, a bool vs a non-bool) is UNKNOWN, not a confident verdict
        # — events.py's _scalar_eq would return False, which for a `==`-kill is a fail-OPEN (the danger
        # can't be ruled out, yet the action fires). _scalar_eq_opt returns None on that incompatibility.
        eq = _scalar_eq_opt(v, value)
        if eq is None:
            return Kleene.UNKNOWN
        return _b(eq) if op == "==" else _b(not eq)
    if op == "contains":
        return _contains(v, value)
    if op == "in":
        if not isinstance(value, list):
            return Kleene.UNKNOWN
        return Kleene.TRUE if any(_scalar_eq(v, item) for item in value) else Kleene.FALSE
    # one of >, >=, <, <= — a non-numeric operand on either side is UNKNOWN (can't decide), never a
    # silent False (events.py's choice — the common-mode this substrate exists to diverge from).
    a, b = _as_number(v), _as_number(value)
    if a is None or b is None:
        return Kleene.UNKNOWN
    return _b({">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[op])


def _b(x: bool) -> Kleene:
    return Kleene.TRUE if x else Kleene.FALSE


# --- field resolution + scalar helpers (re-implemented — diverse, NOT events.py) ---------------


def _resolve_field(field: Any, event: dict[str, Any]) -> Any:
    """Resolve a predicate field to its value or ``_ABSENT``. Permissive + diverse: ``fields.x`` /
    ``event.x`` force a namespace; a bare name digs the ``fields`` dict (dotted) FIRST, then the
    top-level event dict — so a real event (``{"fields": {...}}``) AND a flat drill
    (``{"dmarc": "fail"}``) both resolve. A stored JSON null resolves to ``_ABSENT`` (no value to
    assert against), matching ``events.py`` so a kill behaves identically on the confident cases."""
    if not isinstance(field, str) or not field:
        return _ABSENT
    fields = event.get("fields")
    fields = fields if isinstance(fields, dict) else {}
    if field.startswith("fields."):
        return _dig(fields, field[len("fields."):])
    if field.startswith("event."):
        return _dig(event, field[len("event."):])
    v = _dig(fields, field)                      # bare: the fields namespace first ...
    return v if v is not _ABSENT else _dig(event, field)  # ... then the top level (flat drills)


def _dig(container: Any, path: str) -> Any:
    cur = container
    for seg in path.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            return _ABSENT
    return _ABSENT if cur is None else cur


def _as_number(x: Any) -> float | None:
    """Coerce to a FINITE float, or None. Bools are NOT numbers (no True==1). A numeric string
    coerces. Mirrors ``events._as_number`` (confident-case parity)."""
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x) if math.isfinite(x) else None
    if isinstance(x, str):
        try:
            f = float(x)
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def _scalar_eq(a: Any, b: Any) -> bool:
    """Equality consistent with ``events._scalar_eq``: both str → casefold; bool → strict (no True==1);
    else finite-numeric coercion; else strict ==. (On a present field the kill matches events.py.)"""
    if isinstance(a, str) and isinstance(b, str):
        return a.casefold() == b.casefold()
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a is b
    na, nb = _as_number(a), _as_number(b)
    if na is not None and nb is not None:
        return na == nb
    return a == b


def _scalar_eq_opt(a: Any, b: Any) -> bool | None:
    """``==`` for the ``!=`` path, but None when the two are type-incompatible enough that equality is
    not confidently decidable — a present STRING field compared ``!=`` to a numeric threshold, say.
    Returns the bool when both sides are the same comparable kind; None → the leaf is UNKNOWN (the
    fail-safe direction for ``!=``, which would otherwise return a confident True on an apples-to-
    oranges mismatch and quietly let the action through)."""
    if isinstance(a, str) and isinstance(b, str):
        return a.casefold() == b.casefold()
    if isinstance(a, bool) or isinstance(b, bool):
        if isinstance(a, bool) and isinstance(b, bool):
            return a is b
        return None  # bool vs non-bool → not confidently (un)equal → UNKNOWN
    na, nb = _as_number(a), _as_number(b)
    if na is not None and nb is not None:
        return na == nb
    if (na is None) != (nb is None):
        return None  # one numeric, one not → UNKNOWN rather than a confident "unequal"
    return a == b


def _contains(v: Any, value: Any) -> Kleene:
    """``contains``: substring (string field, case-insensitive) or membership (list field). Any
    type-INCOMPATIBLE target is UNKNOWN (fail-safe), NOT a confident False (the diverse inversion of
    ``events._contains``'s deterministic False, codex L3): ``contains`` on a number field, OR a
    non-string ``value`` against a string field (a substring test needs a string), is unevaluable →
    trip. A list field with any scalar ``value`` is a well-defined membership test (TRUE/FALSE)."""
    if isinstance(v, str):
        if not isinstance(value, str):
            return Kleene.UNKNOWN   # a substring test of a non-string target is unevaluable → fail-safe
        return _b(value.casefold() in v.casefold())
    if isinstance(v, (list, tuple)):
        return _b(any(_scalar_eq(item, value) for item in v))
    return Kleene.UNKNOWN
