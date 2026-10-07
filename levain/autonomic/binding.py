"""levain.autonomic.binding — the compiled binding + the delegated-authority registry (Slice 3a).

A binding is a delegated-authority grant made concrete: *"I, Phill, authorize flow to do G when P,
at posture R."* (`agent_authority_model.md` / `awareness_bus_binding_language.md`). It is the
structure that makes the efferent gate AUTONOMOUS — Slices 1-2 fire the gate on a MANUAL,
human-present invocation (``manual_invocation()``); a binding fires the SAME gate on a trigger, with
no human in the room. That is the whole point and the whole danger, so a binding is governance, not
convenience.

This module is the schema + the registry (Slice 3a). It is NOT the compiler (Slice 3c — NL →
compiled binding, schema-first inversion put it LAST), NOT the cockpit (Slice 3b — the would-fire
replay UI over ``events.replay``), and NOT the chain executor / graduation / fire-path (Slice 4). It
is the foundation everything else hangs off: the data model + the standing-governance store.

The load-bearing cuts (each one an apparatus finding made structural):

1. **Two orthogonal axes, split (NOT one ``status`` enum).** A binding has a CARDINALITY (does it
   fire once or repeatedly — ``one_shot``) and a LIFECYCLE state (active / paused / revoked / expired
   — ``status``). Cardinality is GRANT CONTENT (changing fires-once→fires-forever is an autonomy
   change) → it is SEALED. Lifecycle is mutable state (pause/revoke/expire) → it is unsealed and
   moved only by governed verbs. Conflating them (the original ``STANDING|ONE_SHOT|PAUSED`` enum) made
   ``one_shot → standing`` a silent autonomy increase through an unsealed field; the split closes that
   structurally (the seal now catches a cardinality flip).

2. **The predicate ``P`` is OPAQUE here (corpus-agnostic core).** ``TriggerSpec.pattern`` is stored as
   an opaque JSON object — the vagus core NEVER imports flow's ``events.py`` (the dependency arrow
   stays down; the anti-cycle rule). The deterministic-predicate INVARIANT ("``P`` must reduce to a
   deterministic predicate, never a fire-time LLM judgment") is enforced where the corpus is KNOWN:
   the compiler/cockpit (3b/3c) and the fire-path (Slice 4) validate ``pattern`` via the injected
   :class:`PredicateValidator` (flow wires ``events.validate_predicate``). The store offers that
   validator as a SECOND structural layer (injected at construction), but the store is corpus-agnostic
   and is NOT the primary enforcement home — the fire-path, which has the corpus, must re-validate
   before it matches (it cannot trust that a stored predicate was ever validated).

3. **A binding is SEALED (a content fingerprint over its immutable governance core).** A standing
   grant fires REPEATEDLY and autonomously, so silently widening its ``tools``, flipping its
   ``posture`` ``confirm→on_loop``, or its ``one_shot`` ``True→False`` is exactly the tamper a standing
   grant must resist. The seal covers what determines WHAT fires and AT WHAT AUTONOMY (trigger / goal
   / tools / output / tightness / posture / one_shot / creator). It deliberately does NOT cover the
   mutable bookkeeping (``status``, ``graduation``) — pausing, revoking, or bumping a fire counter is a
   GOVERNED verb that must not require re-minting the grant. A LEGITIMATE posture promotion
   (graduation, §2.6) is a re-ratification: mint a NEW sealed binding and revoke the old (see
   :meth:`BindingStore.replace_atomic`) — never an in-place posture edit. A seal mismatch ⇒ the
   binding is structurally BARRED from the active-fire set (``list_active`` excludes it, fail-closed).
   KEYLESS (like ``pending``'s seal) — it does not defend against a malicious local process that can
   also recompute it (full local compromise out of scope); it defends against accidental corruption,
   schema drift, and a buggy writer. An unsealed field (``status``) tampered DIRECTLY on disk is the
   keyless boundary; the governed verbs (``set_status`` transitions, ``add`` bookkeeping-preserve) are
   what close the realistic in-scope (buggy-writer) resurrection paths.

4. **The registry is MUTABLE standing state, not an append-only trace.** Bindings are created, paused,
   graduated, demoted, revoked — so the store is the ``PendingActionStore`` shape (a JSON list,
   sidecar-flock-serialized, atomic tmp+replace), NOT the append-only JSONL the receipt/proposal
   stores use. Revocation prefers ``set_status(REVOKED)`` (audit-preserving) over a hard ``remove``.

Stdlib-only; ``fcntl`` is POSIX (the stack's macOS/Linux targets).
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import logging
import math
import os
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Protocol, runtime_checkable

from levain.autonomic.authority import AuthorityScope
from levain.autonomic.kill import Kleene, assert_kill_pure, kill_outcome
from levain.autonomic.posture import Posture

__all__ = [
    "BindingStatus",
    "TriggerSpec",
    "SubGoal",
    "TightnessVector",
    "Graduation",
    "Guard",
    "PredicateValidator",
    "Binding",
    "binding_invocation",
    "seal_binding_id",
    "BindingStore",
]

_log = logging.getLogger("levain.autonomic.binding")


class BindingStatus(str, Enum):
    """The LIFECYCLE state of a grant (NOT its cardinality — that is the sealed ``Binding.one_shot``).
    A ``str`` enum so it serializes to its value and round-trips by value. NOT part of the seal — a
    pause/revoke/expire is a governed state change, not a re-ratification of the grant's content.

    ``ACTIVE`` is the only firing state. ``PAUSED`` is a resumable suspension. ``REVOKED`` is the
    permanent audit tombstone (never fires, never transitions out — revive = re-ratify = a NEW
    binding). ``EXPIRED`` is the staleness-lifecycle demotion (the adaptivity vertex of the
    autonomy/safety/adaptivity triangle — bindings AGE; the WHEN-to-expire logic is a later slice, the
    STATE + its only-cleanup-to-REVOKED transition exist now so the model is complete and a stale
    grant cannot silently re-activate)."""

    ACTIVE = "active"          # the only firing state
    PAUSED = "paused"          # resumable suspension
    REVOKED = "revoked"        # permanent tombstone (kept for audit, never fires, terminal)
    EXPIRED = "expired"        # staleness demotion (re-ratify to revive; only cleans up to REVOKED)

    @property
    def is_active(self) -> bool:
        """True iff a binding in this lifecycle state may fire (``ACTIVE`` only)."""
        return self is BindingStatus.ACTIVE

    @property
    def is_inert(self) -> bool:
        """True iff this state can never fire again without re-ratification (``REVOKED``/``EXPIRED``)."""
        return self in (BindingStatus.REVOKED, BindingStatus.EXPIRED)


# The governed lifecycle transition policy (the structural close on the silent-reactivation hole): a
# requested transition is legal only if it is idempotent OR the target is in this set. REVOKED is
# terminal (empty set); EXPIRED only cleans up to REVOKED; nothing INERT re-activates.
_ALLOWED_TRANSITIONS: dict[BindingStatus, frozenset[BindingStatus]] = {
    BindingStatus.ACTIVE: frozenset({BindingStatus.PAUSED, BindingStatus.REVOKED, BindingStatus.EXPIRED}),
    BindingStatus.PAUSED: frozenset({BindingStatus.ACTIVE, BindingStatus.REVOKED, BindingStatus.EXPIRED}),
    BindingStatus.EXPIRED: frozenset({BindingStatus.REVOKED}),
    BindingStatus.REVOKED: frozenset(),
}
# (→EXPIRED from ACTIVE/PAUSED is a manual fail-CLOSED demotion — it only de-activates, never
# resurrects; the Slice-4 staleness daemon is the primary expirer, a manual demotion is also valid.)

# Restrictiveness rank for "most-restrictive lifecycle wins" when ``add``/``replace_atomic`` collide
# with existing same-id record(s): a REVOKED tombstone can NEVER be overridden by a duplicate ACTIVE
# (the resurrection-via-duplicate vector, L3 codex/nemotron). Higher = harder to fire.
_STATUS_RESTRICTIVENESS: dict[BindingStatus, int] = {
    BindingStatus.ACTIVE: 0,
    BindingStatus.PAUSED: 1,
    BindingStatus.EXPIRED: 2,
    BindingStatus.REVOKED: 3,
}


def _assert_json_canonical(obj: Any, path: str) -> None:
    """Recursively assert ``obj`` is canonical-JSON-UNAMBIGUOUS: every dict key is a ``str``, every
    float is finite, every leaf is a JSON scalar (None/bool/int/float/str), and the only containers
    are dict/list. This is the corpus-AGNOSTIC structural floor under the seal (L3 codex HIGH): the
    predicate ``pattern`` is opaque, but ``json.dumps`` SILENTLY COERCES a non-string dict key
    (``{1: "x"}`` and ``{"1": "x"}`` both encode to ``{"1":"x"}``), so two DIFFERENT cores would seal
    to the SAME id WITHOUT a hash collision — defeating the seal's tamper-detection. And
    ``json.dumps`` accepts ``NaN``/``Infinity`` by default (not strict JSON), which would also break
    round-trip determinism. Rejecting both at construction (not silently normalizing — a coerced key
    hides a caller bug) makes the seal genuinely unambiguous. Raises ``TypeError``/``ValueError``;
    the store read catches them and skips the record."""
    if obj is None or isinstance(obj, (bool, int, str)):
        return
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError(f"{path}: non-finite float {obj!r} (NaN/Infinity is not canonical JSON)")
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise TypeError(f"{path}: dict key {k!r} must be a string (got {type(k).__name__}) — "
                                "non-string keys make the seal ambiguous")
            _assert_json_canonical(v, f"{path}.{k}")
        return
    if isinstance(obj, list):
        for i, v in enumerate(obj):
            _assert_json_canonical(v, f"{path}[{i}]")
        return
    raise TypeError(f"{path}: value of type {type(obj).__name__} is not JSON-canonical")


# --- the immutable governance core (the sealed parts) ------------------------------------


@dataclass(frozen=True)
class TriggerSpec:
    """What fires the binding: a trigger ``type`` (``"email"`` / ``"location.fix"`` / ``"sensor.<n>"``
    — the ``events.db`` event ``type`` axis) + an OPAQUE deterministic predicate ``pattern`` (the
    ``events.py`` JSON-AST ``P``). The pattern is opaque BY DESIGN (cut 2); its INTERNAL validity is
    the adapter's :class:`PredicateValidator` job, not this dataclass's."""

    type: str
    pattern: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.type, str) or not self.type:
            raise ValueError("TriggerSpec.type must be a non-empty string")
        if not isinstance(self.pattern, dict):
            raise TypeError(f"TriggerSpec.pattern must be a dict, got {type(self.pattern).__name__}")
        # the opaque predicate must still be canonical-JSON-unambiguous, or the seal is not sound
        _assert_json_canonical(self.pattern, "pattern")

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "pattern": dict(self.pattern)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TriggerSpec":
        pattern = d["pattern"]
        # copy a dict so a later caller mutation can't reach into the frozen spec; non-dicts pass
        # through to __post_init__ which raises the clear TypeError (the store read catches it).
        return cls(type=d["type"], pattern=dict(pattern) if isinstance(pattern, dict) else pattern)


@dataclass(frozen=True)
class SubGoal:
    """One link in the binding's goal chain: a bounded ``goal`` (the single transformation), the
    explicit minimal ``tools`` it may use, and the bounded ``output`` destination. A single-action
    binding is a chain of length 1. The chain's EXECUTION (per-link posture, pause-at-confirm,
    hops-attenuation) is Slice 4 — this is only the stored shape the executor reads."""

    goal: str
    tools: tuple[str, ...]
    output: str

    def to_dict(self) -> dict[str, Any]:
        return {"goal": self.goal, "tools": list(self.tools), "output": self.output}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SubGoal":
        goal = d["goal"]
        output = d["output"]
        tools = d["tools"]
        if not isinstance(goal, str) or not goal:
            raise ValueError("SubGoal.goal must be a non-empty string")
        if not isinstance(output, str) or not output:
            raise ValueError("SubGoal.output must be a non-empty string")
        if not isinstance(tools, (list, tuple)) or not all(isinstance(t, str) for t in tools):
            raise TypeError("SubGoal.tools must be a list of strings")
        return cls(goal=goal, tools=tuple(tools), output=output)


def _coerce_unit(value: Any, name: str) -> float:
    """Validate + NORMALIZE a tightness score: a real number in ``[0, 1]``, returned as a ``float``.
    ``bool`` is rejected even though it is an ``int`` subtype (a stored ``true`` is not a score). A
    value outside the unit interval — or ``NaN`` (``0 <= nan <= 1`` is ``False``) — raises
    ``ValueError``. The NORMALIZE half is load-bearing for the seal: an integer score (``1`` for
    tightest, ``0`` for loosest — exactly what a compiler emits) seals over ``json.dumps(1)=="1"`` at
    ``create`` but reloads through ``float()`` to ``1.0`` (``"1.0"``) → a DIFFERENT hash → a false
    SEAL MISMATCH that silently bars a clean grant (L1-H1). Normalizing to ``float`` AT CONSTRUCTION
    makes create-time and reload-time encodings identical, closing the asymmetry."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"tightness.{name} must be a number, got {type(value).__name__}")
    f = float(value)
    if not (0.0 <= f <= 1.0):
        raise ValueError(f"tightness.{name} must be in [0,1], got {f!r}")
    return f


@dataclass(frozen=True)
class TightnessVector:
    """The compiler's grounding score along the four measurable dimensions
    (``awareness_bus_binding_language.md``): how bounded the goal is, how minimal the tools are, how
    precise the predicate is, how bounded the output is — each in ``[0, 1]`` (1 = tightest). The
    score feeds ``invocation_trust`` (looser ⇒ more confirms); it is the dial the compiler PRICES.
    Part of the sealed core: a binding's tightness is what it was ratified at. Validated + NORMALIZED
    to ``float`` in ``__post_init__`` so a direct constructor and a ``from_dict`` reload produce the
    identical (seal-stable) encoding (L1-H1)."""

    goal_spec: float
    tool_min: float
    pattern_precision: float
    output_bound: float

    def __post_init__(self) -> None:
        for name in ("goal_spec", "tool_min", "pattern_precision", "output_bound"):
            object.__setattr__(self, name, _coerce_unit(getattr(self, name), name))

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_spec": self.goal_spec,
            "tool_min": self.tool_min,
            "pattern_precision": self.pattern_precision,
            "output_bound": self.output_bound,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TightnessVector":
        # __post_init__ validates + normalizes; from_dict only needs the keys present.
        return cls(
            goal_spec=d["goal_spec"],
            tool_min=d["tool_min"],
            pattern_precision=d["pattern_precision"],
            output_bound=d["output_bound"],
        )


@dataclass(frozen=True)
class Guard:
    """The bind-time BET-SLIP for ONE uncertainty-spike (``RECEIPT_VERSION=4`` §2.1) — the dissent
    and the kill the human PRODUCES at ratification, so the standing grant carries its own "why this
    might be wrong" + "stop if X". Part of the SEALED governance core: Slice 3c seals it under the
    STRICT whole seal (ANY edit re-ratifies); Slice 3a.5 relaxes that to monotonic-editability
    (tighten-free / loosen-re-ratifies). Sealing the guard ON the binding is the anti-DEFANGED-DISSENT
    invariant — a later strip/weaken of the dissent or the kill is a seal MISMATCH that bars the
    binding from the active-fire set (:meth:`BindingStore.list_active` excludes it), so live-guard ≠
    ratified-guard is detectable.

    dissent + kill are COUPLED. ``rationale`` = the dissent ("why this might be wrong"), authored on a
    DIFFERENT substrate than the compiler (the ``cross_substrate_review_codex_nonreplaceable`` pattern)
    — ``dissent_author`` records WHICH. ``kill_predicate`` = the deterministic "stop if X" (known
    danger): an OPAQUE ``events.py`` JSON-AST validated deterministic by the injected
    :class:`PredicateValidator` (exactly like ``TriggerSpec.pattern``), PRODUCED by the human
    (``kill_authored_by`` — not accepted from a generator; the guard's job is to defeat the operator's
    own bind-time self-trust). ``kill_drill`` = a concrete event that should trip the kill (its
    PRESENCE is a 3c compile gate — "no drill → no compile"; that the drill ACTUALLY trips the kill is
    the 3a.5 *kill-drill compile gate*, which needs kill-purity). ``predicted_trajectory`` = the
    binding's own forward-simulation (the 3a.5 prediction-error monitor diffs actual-vs-predicted).

    STRUCTURAL invariant (HERE, corpus-agnostic): a guard's kill travels as a UNIT — ``kill_predicate``
    + ``kill_drill`` + ``kill_authored_by`` are all-present or all-absent (a kill without its drill or
    its author is malformed). The opaque dicts are canonical-JSON-unambiguous (the seal floor). The
    POLICY invariants — guard MANDATORY at confirm-class, the dissent substrate must DIFFER from the
    compiler, the kill must validate deterministic against the corpus — are the COMPILER's (Slice 3c):
    they need the resolved posture / the corpus, which this corpus-agnostic schema does not have."""

    rationale: str
    dissent_author: str
    spike_id: str | None = None
    kill_predicate: dict[str, Any] | None = None
    kill_drill: dict[str, Any] | None = None
    kill_authored_by: str | None = None
    predicted_trajectory: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        # non-empty checks use .strip() — a whitespace-only id (" ") is semantically empty but would
        # pass a bare truthiness check, and the compiler's substrate-identity comparison would treat
        # it as a real (cross-substrate) author (L2/complement-LOW3).
        if not isinstance(self.rationale, str) or not self.rationale.strip():
            raise ValueError("Guard.rationale must be a non-empty string (the dissent)")
        if not isinstance(self.dissent_author, str) or not self.dissent_author.strip():
            raise ValueError("Guard.dissent_author must be a non-empty string (the adversarial substrate id)")
        if self.spike_id is not None and (not isinstance(self.spike_id, str) or not self.spike_id.strip()):
            raise ValueError("Guard.spike_id must be a non-empty string or None")
        # the kill travels as a unit — predicate + drill + author together, or none of them
        kill_parts = (self.kill_predicate, self.kill_drill, self.kill_authored_by)
        if any(p is not None for p in kill_parts) and not all(p is not None for p in kill_parts):
            raise ValueError(
                "Guard kill is incomplete — kill_predicate, kill_drill and kill_authored_by travel "
                "together (a kill without its drill or its author is malformed)")
        if self.kill_predicate is not None:
            if not isinstance(self.kill_predicate, dict):
                raise TypeError("Guard.kill_predicate must be a dict (an opaque events.py predicate)")
            _assert_json_canonical(self.kill_predicate, "kill_predicate")
            if not isinstance(self.kill_drill, dict):
                raise TypeError("Guard.kill_drill must be a dict (a concrete event)")
            _assert_json_canonical(self.kill_drill, "kill_drill")
            if not isinstance(self.kill_authored_by, str) or not self.kill_authored_by.strip():
                raise ValueError("Guard.kill_authored_by must be a non-empty string (the human who PRODUCED the kill)")
        if self.predicted_trajectory is not None:
            if not isinstance(self.predicted_trajectory, dict):
                raise TypeError("Guard.predicted_trajectory must be a dict")
            _assert_json_canonical(self.predicted_trajectory, "predicted_trajectory")

    @property
    def has_kill(self) -> bool:
        """True iff this guard carries a deterministic kill (predicate+drill+author). A guard WITHOUT
        a kill is calibration-only (the dissent + a flagged spike), valid at on-loop/manual per §2.1;
        the compiler REQUIRES ``has_kill`` for binding-driven confirm-class fires."""
        return self.kill_predicate is not None

    def to_dict(self) -> dict[str, Any]:
        # deepcopy the opaque dicts (not a shallow dict()) — a shallow copy aliases NESTED structures
        # (a predicate's `clauses` list), so a caller mutating the returned dict would reach into the
        # frozen guard and silently change the sealed content (complement-LOW2).
        return {
            "rationale": self.rationale,
            "dissent_author": self.dissent_author,
            "spike_id": self.spike_id,
            "kill_predicate": copy.deepcopy(self.kill_predicate) if self.kill_predicate is not None else None,
            "kill_drill": copy.deepcopy(self.kill_drill) if self.kill_drill is not None else None,
            "kill_authored_by": self.kill_authored_by,
            "predicted_trajectory": (
                copy.deepcopy(self.predicted_trajectory) if self.predicted_trajectory is not None else None),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Guard":
        """Reconstruct a guard from a stored record. A DEEP copy so a later caller mutation of the
        source dict (or its nested AST) can't reach into the frozen guard; a non-dict opaque field
        passes through to ``__post_init__`` which raises the clear error (the store read catches it +
        skips the binding loudly)."""
        def _opt(v: Any) -> Any:
            return copy.deepcopy(v) if isinstance(v, dict) else v
        return cls(
            rationale=d["rationale"],
            dissent_author=d["dissent_author"],
            spike_id=d.get("spike_id"),
            kill_predicate=_opt(d.get("kill_predicate")),
            kill_drill=_opt(d.get("kill_drill")),
            kill_authored_by=d.get("kill_authored_by"),
            predicted_trajectory=_opt(d.get("predicted_trajectory")),
        )


# --- the mutable bookkeeping (NOT sealed) ------------------------------------------------


def _nonneg_int(value: Any, name: str) -> int:
    """Validate a non-negative graduation counter. ``bool`` is rejected (an ``int`` subtype); a
    negative or non-int raises so a corrupt counter can't poison the graduation math."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"graduation.{name} must be an int, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"graduation.{name} must be >= 0, got {value}")
    return value


def _strict_bool(value: Any, name: str) -> bool:
    """A stored governance bool must be a REAL JSON bool — not a truthy string (``bool("false")`` is
    truthy, so a hand-edited/drifted ``"false"`` would flip a fail-closed flag). Mirrors
    ``pending._strict_bool``; reject anything but ``True``/``False`` (the read path skips the record)."""
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a JSON bool, got {type(value).__name__}: {value!r}")
    return value


@dataclass(frozen=True)
class Graduation:
    """The §2.6 meta-loop's track record — how the grant's posture might loosen WITH evidence:
    ``fire_count`` (total fires), ``clean_count`` (fires the human did not have to correct/deny), and
    ``last_fired_at`` (ISO string of the most recent fire, or ``None`` if never fired — the DORMANCY
    sensor the staleness/expiry lifecycle needs; without it a ``fire_count`` 5→5 over 90 days is
    indistinguishable from 90 minutes, L2-M2). MUTABLE bookkeeping, deliberately OUTSIDE the seal:
    ``record_fire`` bumps it without re-minting the grant.

    NOTE — these counters are UNSEALED, so a disk edit does not trip the seal; Slice 4 must treat
    them as untrusted evidence, not a trusted ledger. The structural floor here: ``clean_count`` can
    never exceed ``fire_count`` (a forged "cleaner than it earned" record is rejected on load). The
    chain-depth graduation gate (``deep-outbound chains don't graduate``) is deliberately NOT stored
    here — it is a property of the SEALED goal chain (+ the risk manifest), so Slice 4 DERIVES it from
    sealed inputs at decision time; storing it as an independent flippable bool would let a tamper
    re-enable autonomy escalation on the most dangerous chains, seal-blind (L2-H2)."""

    fire_count: int = 0
    clean_count: int = 0
    last_fired_at: str | None = None

    def __post_init__(self) -> None:
        if self.clean_count > self.fire_count:
            raise ValueError(
                f"graduation.clean_count ({self.clean_count}) cannot exceed fire_count ({self.fire_count})"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "fire_count": self.fire_count,
            "clean_count": self.clean_count,
            "last_fired_at": self.last_fired_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Graduation":
        last = d.get("last_fired_at")
        if last is not None and not isinstance(last, str):
            raise TypeError(f"graduation.last_fired_at must be a string or null, got {type(last).__name__}")
        return cls(
            fire_count=_nonneg_int(d["fire_count"], "fire_count"),
            clean_count=_nonneg_int(d["clean_count"], "clean_count"),
            last_fired_at=last,
        )


# --- the predicate-validator seam (the corpus-agnostic boundary) -------------------------


@runtime_checkable
class PredicateValidator(Protocol):
    """The injected boundary that enforces the deterministic-predicate invariant WITHOUT the vagus
    core importing the corpus (cut 2). The flow/Argus adapter wires ``events.validate_predicate``; a
    compiler/cockpit slice — and, as a second layer, :class:`BindingStore` (when one is injected at
    construction) — calls :meth:`validate` on ``TriggerSpec.pattern`` before a binding is accepted. It
    MUST raise (any exception) if the pattern is not a well-formed deterministic predicate — an empty
    composite, an unknown operator, a non-finite threshold, nesting past the cap, a fire-time-LLM
    escape hatch. Returning normally = the predicate grounds."""

    def validate(self, pattern: dict[str, Any]) -> None:
        ...


# --- the seal ----------------------------------------------------------------------------


def seal_binding_id(
    *,
    created_at: str,
    created_by: str,
    trigger: TriggerSpec,
    goal: tuple[SubGoal, ...],
    tightness: TightnessVector,
    posture: Posture,
    one_shot: bool,
    guard: tuple["Guard", ...] = (),
) -> str:
    """The content-FINGERPRINT binding id: ``bind-<created_at>-<16hex>`` over the binding's IMMUTABLE
    governance core — what determines WHAT fires and AT WHAT AUTONOMY. Recomputing it and comparing
    (:meth:`Binding.seal_matches`) detects ANY post-ratification alteration of the trigger predicate,
    the goal chain, the tool set, the output destinations, the tightness, the posture, the cardinality
    (``one_shot``), or the GUARD (the dissent/kill bet-slip — the anti-defanged-dissent invariant).
    Mirrors ``pending.seal_pending_id``: a STRUCTURALLY-ENCODED canonical JSON basis (not a
    delimiter-join — a delimiter-join lets a field carrying the delimiter byte reshuffle into an equal
    hash for a DIFFERENT field tuple, a verified collision class), so field boundaries + types are
    unambiguous.

    The ``guard`` (Slice 3c, ``RECEIPT_VERSION=4``) is the sealed FLOOR — INSIDE the seal: any edit to
    it re-ratifies (mint a new sealed grant). Slice 3a.5 realizes monotonic-editability (tighten-free /
    loosen-re-ratifies) WITHOUT reworking this basis: the tighten-free half is the UNSEALED, append-only
    ``Binding.guard_additions`` channel (the same sealed-core/unsealed-bookkeeping split that already
    holds for ``graduation``), so the seal here covers ONLY the ratified floor and stays byte-for-byte
    what 3c shipped. TIGHTENING appends to ``guard_additions`` (the seal is untouched — id stable, the
    graduation track record preserved); LOOSENING means removing/weakening a FLOOR guard, which touches
    THIS basis → a seal mismatch → barred → forces a re-ratification (``replace_atomic``). The strong
    anti-defanged-dissent guarantee is "never fires BELOW the ratified floor"; additions are voluntary
    extra protection above it (stripping a non-ratified addition returns to the floor, never below it).
    **BACKWARD-COMPAT (the key is OMITTED when the guard is empty):** a guard-LESS
    binding seals EXACTLY as it did before 3c existed (no ``"guard"`` key in the basis), so a genuine
    pre-3c (3a-basis) record's id still validates — adding the guard field to the schema is NOT a
    silent basis break. A binding that CARRIES a guard adds the key; stripping a ratified guard back to
    empty recomputes WITHOUT the key → a DIFFERENT hash → a seal mismatch (the anti-defanged-dissent
    property holds in both directions). An empty guard and an absent guard are the SAME governance
    content (no bet-slip), so sealing them identically is correct, not a collision. Deliberately
    EXCLUDES ``status`` + ``graduation`` (the mutable bookkeeping): a pause, a revoke, or a fire-counter
    bump is a governed verb, not a re-ratification. KEYLESS (cut 3)."""
    body = {
        "created_at": created_at,
        "created_by": created_by,
        "trigger": trigger.to_dict(),
        "goal": [g.to_dict() for g in goal],
        "tightness": tightness.to_dict(),
        "posture": posture.name,
        "one_shot": bool(one_shot),
    }
    if guard:   # OMIT when empty → a guardless binding seals identically to its pre-3c (3a) basis
        body["guard"] = [g.to_dict() for g in guard]
    # allow_nan=False: a non-finite float anywhere in the basis raises rather than emitting the
    # non-strict-JSON ``NaN``/``Infinity`` token (defense in depth — TriggerSpec/_coerce_unit already
    # reject non-finite at construction, L3 codex).
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"bind-{created_at}-{h}"


@dataclass(frozen=True)
class Binding:
    """A compiled, ratified, standing (or one-shot) authority grant. The ``binding_id`` is the content
    fingerprint over the immutable core (:func:`seal_binding_id`), so any later tamper of the trigger,
    goal, tools, output, tightness, posture, or cardinality is detectable via :meth:`seal_matches` and
    bars the binding from the active-fire set. ``status`` (lifecycle) + ``graduation`` (evidence) are
    the mutable bookkeeping (outside the seal). Build only via :meth:`create` (production + tests both
    go through the sealed constructor, so the seal invariant cannot drift)."""

    binding_id: str
    created_by: str
    created_at: str
    status: BindingStatus
    one_shot: bool
    trigger: TriggerSpec
    goal: tuple[SubGoal, ...]
    tightness: TightnessVector
    posture: Posture
    graduation: Graduation = field(default_factory=Graduation)
    guard: tuple[Guard, ...] = ()
    # The UNSEALED, append-only tightening tier (Slice 3a.5 monotonic-editability). NOT in the seal
    # basis — like ``graduation``, it is mutable bookkeeping a governed verb (``BindingStore.tighten_
    # guard``) appends to without re-minting. Additions can only ADD guards/kills (more "stop if X" =
    # strictly safer); they can NEVER take the binding below its sealed floor. A confirm-class binding's
    # MANDATORY kill must live in the sealed ``guard`` floor (it must be tamper-evident); additions are
    # bonus protection the runtime ALSO evaluates (``effective_guard``).
    guard_additions: tuple[Guard, ...] = ()

    @classmethod
    def create(
        cls,
        *,
        created_by: str,
        created_at: str,
        trigger: TriggerSpec,
        goal: tuple[SubGoal, ...] | list[SubGoal],
        tightness: TightnessVector,
        posture: Posture,
        one_shot: bool = False,
        status: BindingStatus = BindingStatus.PAUSED,
        graduation: Graduation | None = None,
        guard: tuple[Guard, ...] | list[Guard] = (),
    ) -> "Binding":
        """Mint a SEALED binding. A binding with an empty goal chain does nothing → refused here (a
        structural guard, not a discipline note). ``created_by``/``created_at`` must be non-empty
        (governance-identity fields). The ``binding_id`` is the fingerprint over the immutable core
        (incl. ``one_shot`` AND ``guard``); ``status``/``graduation`` are excluded so they change
        without re-minting. ``guard`` defaults to empty (an on-loop / manual / calibration-only grant
        needs none); the COMPILER (Slice 3c) is what requires a well-formed guard at confirm-class.

        ``status`` DEFAULTS to ``PAUSED`` (fail-closed, complement-LOW1 / codex-HIGH2 / L1-MED1): a
        freshly-minted binding is ratification-READY, not live — a caller (seed / test / future seam)
        that forgets to set the lifecycle gets a NON-firing grant, never a live autonomous one.
        Going ACTIVE is the governed PAUSED→ACTIVE ratify-write (the explicit lifecycle transition),
        not a creation default."""
        goal_t = tuple(goal)
        guard_t = tuple(guard)
        if not goal_t:
            raise ValueError("Binding.create: goal chain must have at least one SubGoal")
        if not isinstance(created_by, str) or not created_by:
            raise ValueError("Binding.created_by must be a non-empty string")
        if not isinstance(created_at, str) or not created_at:
            raise ValueError("Binding.created_at must be a non-empty string")
        if not isinstance(posture, Posture):
            raise TypeError(f"Binding.posture must be a Posture, got {type(posture).__name__}")
        if not isinstance(status, BindingStatus):
            raise TypeError(f"Binding.status must be a BindingStatus, got {type(status).__name__}")
        if not isinstance(one_shot, bool):
            raise TypeError(f"Binding.one_shot must be a bool, got {type(one_shot).__name__}")
        if not all(isinstance(g, Guard) for g in guard_t):
            raise TypeError("Binding.guard must be a sequence of Guard")
        bid = seal_binding_id(
            created_at=created_at, created_by=created_by, trigger=trigger,
            goal=goal_t, tightness=tightness, posture=posture, one_shot=one_shot, guard=guard_t,
        )
        return cls(
            binding_id=bid,
            created_by=created_by,
            created_at=created_at,
            status=status,
            one_shot=one_shot,
            trigger=trigger,
            goal=goal_t,
            tightness=tightness,
            posture=posture,
            graduation=graduation if graduation is not None else Graduation(),
            guard=guard_t,
        )

    def seal_matches(self) -> bool:
        """True iff the immutable core is UNALTERED since :meth:`create`. A mismatch ⇒ the grant's
        trigger/goal/tools/output/tightness/posture/cardinality/GUARD was changed out from under its
        ratification ⇒ the binding is barred from firing (``BindingStore.list_active`` excludes it)."""
        return self.binding_id == seal_binding_id(
            created_at=self.created_at, created_by=self.created_by, trigger=self.trigger,
            goal=self.goal, tightness=self.tightness, posture=self.posture, one_shot=self.one_shot,
            guard=self.guard,
        )

    @property
    def is_active(self) -> bool:
        """True iff this binding may fire NOW — active lifecycle state AND a valid seal (the two
        conditions the fire-path requires). ``one_shot`` does not affect this: a one-shot binding is
        ``ACTIVE`` until it fires, after which the fire-path revokes it."""
        return self.status.is_active and self.seal_matches()

    @property
    def effective_guard(self) -> tuple[Guard, ...]:
        """The FULL guard set the RUN-time evaluates: the sealed floor PLUS the unsealed tightening
        additions (Slice 3a.5). The fire-path (Slice 4) and the prediction-error monitor evaluate every
        kill here (floor + additions) — more "stop if X" is strictly safer. NOTE the asymmetry: the
        MANDATORY-kill gate (``BindingStore.list_active`` at confirm-class) checks the SEALED floor
        ``guard`` ONLY — a mandatory kill must be tamper-evident, and an unsealed addition is not. So
        additions ADD kills the runtime honors but can NEVER satisfy the confirm-class mandate."""
        return self.guard + self.guard_additions

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "status": self.status.value,
            "one_shot": self.one_shot,
            "trigger": self.trigger.to_dict(),
            "goal": [g.to_dict() for g in self.goal],
            "tightness": self.tightness.to_dict(),
            "posture": self.posture.name,
            "graduation": self.graduation.to_dict(),
            "guard": [g.to_dict() for g in self.guard],
            "guard_additions": [g.to_dict() for g in self.guard_additions],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Binding":
        """Reconstruct from a stored record WITHOUT re-deriving the id — the stored ``binding_id`` is
        preserved so :meth:`seal_matches` can detect a record whose core was edited while the id was
        left stale (the tamper signature). Raises ``KeyError``/``TypeError``/``ValueError`` on a
        malformed record; the store read CATCHES all three and skips the record loudly.

        ``posture`` parses STRICTLY by ``Posture`` name, ``status`` by ``BindingStatus`` value, and
        ``one_shot`` as a strict bool — an unknown/untrusted governance level raises (a record whose
        level can't be trusted is dropped, the safe direction), never coerced to a default."""
        posture_name = d["posture"]
        if not isinstance(posture_name, str) or posture_name not in Posture.__members__:
            raise ValueError(f"unknown posture {posture_name!r}")
        try:
            status = BindingStatus(d["status"])
        except ValueError as e:
            raise ValueError(f"unknown status {d.get('status')!r}") from e
        created_by = d["created_by"]
        created_at = d["created_at"]
        if not isinstance(created_by, str) or not isinstance(created_at, str):
            raise TypeError("created_by / created_at must be strings")
        if not created_by or not created_at:
            raise ValueError("created_by / created_at must be non-empty (the disk-trust boundary "
                             "enforces what create() does — an empty-identity grant defeats provenance)")
        binding_id = d["binding_id"]
        if not isinstance(binding_id, str) or not binding_id:
            raise ValueError("Binding.binding_id must be a non-empty string")
        goal_raw = d["goal"]
        if not isinstance(goal_raw, list) or not goal_raw:
            raise ValueError("Binding.goal must be a non-empty list")
        guard_raw = d.get("guard", [])   # backward-compat: pre-3c records have no guard → empty
        if not isinstance(guard_raw, list):
            raise TypeError(f"Binding.guard must be a list, got {type(guard_raw).__name__}")
        # backward-compat: pre-3a.5 records have no guard_additions → empty (the UNSEALED tightening
        # tier; absent ⇒ no tightening ⇒ the seal recomputes over the floor exactly as before).
        additions_raw = d.get("guard_additions", [])
        if not isinstance(additions_raw, list):
            raise TypeError(f"Binding.guard_additions must be a list, got {type(additions_raw).__name__}")
        return cls(
            binding_id=binding_id,
            created_by=created_by,
            created_at=created_at,
            status=status,
            one_shot=_strict_bool(d["one_shot"], "one_shot"),
            trigger=TriggerSpec.from_dict(d["trigger"]),
            goal=tuple(SubGoal.from_dict(g) for g in goal_raw),
            tightness=TightnessVector.from_dict(d["tightness"]),
            posture=Posture[posture_name],
            graduation=Graduation.from_dict(d["graduation"]),
            guard=tuple(Guard.from_dict(g) for g in guard_raw),
            guard_additions=tuple(Guard.from_dict(g) for g in additions_raw),
        )


def binding_invocation(binding: Binding, *, hops: int = 0) -> AuthorityScope:
    """The authority in force when a BINDING (not a human) fires the gate — the Slice-3 counterpart to
    ``authority.manual_invocation()``. Produces the receipt's ``authority_scope`` with
    ``grantor="binding"`` and ``binding_id`` naming the standing grant, so the trace records WHICH
    grant authorized the fire and at what grounding distance (``hops``).

    REFUSES to mint authority for a non-active binding (defense in depth, L2-M4): the fire-path fetches
    only active+sealed bindings via ``list_active``, but if a future caller bypasses that, a
    tampered/revoked grant must not be able to produce a valid-looking ``authority_scope`` in a
    receipt. ``hops`` must be non-negative (a negative grounding distance would under-attenuate the
    posture, L2-L1)."""
    if hops < 0:
        raise ValueError(f"hops must be >= 0, got {hops}")
    if not binding.is_active:
        raise ValueError(
            f"binding_invocation refuses a non-active binding {binding.binding_id!r} "
            f"(status={binding.status.value}, seal_ok={binding.seal_matches()})"
        )
    grant = f"binding:{binding.trigger.type}@{binding.posture.name.lower()}"
    return AuthorityScope(grantor="binding", grant=grant, binding_id=binding.binding_id, hops=hops)


# --- the registry ------------------------------------------------------------------------


class BindingStore:
    """The delegated-authority registry — "the third kind of state" (standing governance: not memory
    that accretes, not tasks that complete). A mutable JSON-list store, ``flock``-serialized + atomic
    tmp+replace, the same shape as :class:`~levain.autonomic.pending.PendingActionStore` (bindings are
    created/paused/graduated/revoked, so NOT the append-only JSONL the receipt/proposal traces use).
    Every mutation is a locked read-modify-write; reads need no lock (atomic-replace means a read sees
    a whole old-or-new file). A malformed record is skipped LOUDLY on read.

    An optional :class:`PredicateValidator` injected at construction is the store's SECOND structural
    layer for the deterministic-predicate invariant (the production/flow store wires
    ``events.validate_predicate``; the corpus-agnostic core cannot self-validate — cut 2). It is NOT
    the primary enforcement home: the fire-path (Slice 4), which has the corpus, MUST re-validate a
    predicate before matching — it cannot trust that a stored predicate was ever validated."""

    def __init__(self, path: str | Path, *, validator: PredicateValidator | None = None) -> None:
        self.path = Path(path)
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._validator = validator

    # --- locking -----------------------------------------------------------------------
    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold an exclusive ``flock`` on a sidecar lockfile across a read-modify-write. A sidecar
        (not the data file) so the lock survives the ``os.replace`` that swaps the data inode. Created
        on first use; never removed. Mirrors ``PendingActionStore._locked``."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    # --- raw IO (call under the lock for mutations) ------------------------------------
    def _read_raw(self, *, for_mutation: bool = False) -> list[dict[str, Any]]:
        """Load the JSON list; fail-soft on READS. A missing file → ``[]``; a corrupt file (bad JSON or
        a non-list top level) → ``[]`` with a WARNING. Non-dict elements are pre-filtered (matching the
        ``pending`` idiom); a dict that fails reconstruction is skipped loudly by :meth:`_load`.

        ``for_mutation=True`` (the read that opens a locked read-modify-write) RE-RAISES a transient
        ``OSError`` instead of degrading to ``[]`` (L3 nemotron): a read that fails mid-mutation must
        NOT proceed to ``_write_raw`` on an empty list — that would atomically REPLACE the file and
        silently DELETE every other binding. Fail the mutation loud; the existing file is left intact.
        (A genuinely missing file still reads ``[]`` — that is the first-write case, not a failure.)"""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as e:
            _log.warning("binding store: read failed (%s): %s", type(e).__name__, e)
            if for_mutation:
                raise
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            # On a MUTATION read, a corrupt file must NOT degrade to [] — that would let the
            # read-modify-write atomically REPLACE the registry with a 1-record file, silently DELETING
            # every other grant + revoked tombstone (codex-MED3). Fail the mutation loud; leave the
            # file intact. (A plain READ still degrades to [] — a corrupt registry surfaces as "no
            # bindings" to inspection, never a write.)
            _log.warning("binding store: corrupt JSON (%s)%s", e,
                         " — RE-RAISING (mutation)" if for_mutation else " — returning []")
            if for_mutation:
                raise
            return []
        if not isinstance(data, list):
            _log.warning("binding store: top level is %s, not a list%s", type(data).__name__,
                         " — RE-RAISING (mutation)" if for_mutation else " — returning []")
            if for_mutation:
                raise TypeError(f"binding store top level is {type(data).__name__}, not a list "
                                "(refusing to mutate over a corrupt registry)")
            return []
        return [r for r in data if isinstance(r, dict)]

    def _write_raw(self, records: list[dict[str, Any]]) -> None:
        """Atomically replace the file with ``records`` (tmp + ``os.replace`` — never a torn read).
        Call only under ``_locked``."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    @staticmethod
    def _load(rec: dict[str, Any]) -> Binding | None:
        """Reconstruct a binding from a raw record, or ``None`` (logged) if malformed — the shared
        skip-loudly path for all read methods."""
        try:
            return Binding.from_dict(rec)
        except (KeyError, TypeError, ValueError) as e:
            _log.warning("binding store: skipping malformed record (%s: %s)", type(e).__name__, e)
            return None

    def _validate_kill(self, predicate: dict[str, Any]) -> None:
        """A kill predicate must be PURE — a read-only, deterministic predicate the DIVERSE kill
        substrate can evaluate (:func:`levain.autonomic.kill.assert_kill_pure`) — AND, when a validator
        is injected, accepted by it (the corpus-aware floor). Purity is checked UNCONDITIONALLY (it is
        corpus-agnostic), so even a validator-LESS store refuses an impure kill: the diverse substrate
        validates its own input rather than trusting the compiler / the tighten verb / a seed did
        (Slice 3a.5, ``cross_substrate_review_codex_nonreplaceable`` at the validation layer)."""
        assert_kill_pure(predicate)
        if self._validator is not None:
            self._validator.validate(predicate)

    def _validate(self, binding: Binding) -> None:
        """Pre-persist structural checks shared by ``add``/``replace_atomic``: the binding's OWN seal
        must hold (you cannot persist a self-inconsistent / tampered object), the injected validator
        (if any) must accept the trigger ``pattern``, and every KILL must be valid.

        Every kill — the SEALED floor ``guard`` AND the UNSEALED ``guard_additions`` — is re-validated
        (purity + the injected validator) via :meth:`_validate_kill`: the store does NOT trust that the
        compiler (or the 3a.5 tighten verb / a re-author / a seed) validated it. A "stop if X" that is
        not a pure deterministic predicate is a kill that cannot be evaluated (defense-in-depth parity
        with the trigger; closes the asymmetry where only the trigger was re-validated)."""
        if not binding.seal_matches():
            raise ValueError(f"refusing to persist a seal-broken binding {binding.binding_id!r}")
        if self._validator is not None:
            self._validator.validate(binding.trigger.pattern)
        for g in (*binding.guard, *binding.guard_additions):
            if g.kill_predicate is not None:
                self._validate_kill(g.kill_predicate)

    @staticmethod
    def _preserve_bookkeeping(
        records: list[dict[str, Any]], binding_id: str, record: dict[str, Any]
    ) -> dict[str, Any]:
        """Carry verb-owned bookkeeping (``status`` + ``graduation``) forward onto ``record`` from any
        EXISTING same-id records, so a re-``add`` can neither resurrect a revoked grant nor wipe its
        evidence. Only records that LOAD CLEANLY contribute (L3 codex/complement/nemotron):
          - status: the MOST-RESTRICTIVE valid status wins (a REVOKED/EXPIRED tombstone can never be
            overridden by a duplicate ACTIVE — the resurrection-via-duplicate vector);
          - graduation: the record with the most evidence (max ``fire_count``).
        A MALFORMED same-id record is IGNORED, not entombing-carried (carrying a bad ``status`` forward
        would make the next read drop the binding — a silent data destructor); the caller drops all
        same-id records, so the merged ``record`` is the single deduped survivor. If the collision is
        ONLY with malformed records (no valid prior state), the incoming bookkeeping is used + logged."""
        best_status: str | None = None
        best_rank = -1
        best_grad: dict[str, Any] | None = None
        best_fires = -1
        saw_same_id = False
        saw_clean = False
        for r in records:
            if r.get("binding_id") != binding_id:
                continue
            saw_same_id = True
            loaded = BindingStore._load(r)
            if loaded is None:
                continue
            saw_clean = True
            rank = _STATUS_RESTRICTIVENESS[loaded.status]
            if rank > best_rank:
                best_rank, best_status = rank, loaded.status.value
            if loaded.graduation.fire_count > best_fires:
                best_fires, best_grad = loaded.graduation.fire_count, loaded.graduation.to_dict()
        if best_status is not None:
            record["status"] = best_status
        if best_grad is not None:
            record["graduation"] = best_grad
        if saw_same_id and not saw_clean:
            _log.warning("binding store: re-add of %r collides ONLY with malformed record(s) — using "
                         "the incoming bookkeeping (no valid prior state to preserve)", binding_id)
        return record

    # --- public API --------------------------------------------------------------------
    def add(self, binding: Binding) -> None:
        """Persist a binding. On a same-id collision the record is REPLACED but the on-disk ``status``
        + ``graduation`` are PRESERVED via :meth:`_preserve_bookkeeping` (most-restrictive valid status
        + max evidence) — bookkeeping is owned by its governed verbs (``set_status`` / ``record_fire``),
        never silently reset by ``add``. Closes: a stale re-add wiping graduation (L1-M3); the silent
        un-revoke (re-``create`` of a revoked core → same id → default-ACTIVE → resurrection, L2-H1);
        and a duplicate-ACTIVE overriding a REVOKED tombstone (L3). The binding's own seal must hold +
        the injected validator (if any) must accept the predicate. Locked read-modify-write."""
        self._validate(binding)
        record = binding.to_dict()
        with self._locked():
            records = self._read_raw(for_mutation=True)
            record = self._preserve_bookkeeping(records, binding.binding_id, record)
            kept = [r for r in records if r.get("binding_id") != binding.binding_id]
            kept.append(record)
            self._write_raw(kept)

    def replace_atomic(self, old_binding_id: str, new_binding: Binding,
                       *, precondition: Callable[[Binding], bool] | None = None) -> bool:
        """RE-RATIFICATION as one atomic, ALL-OR-NOTHING step: persist ``new_binding`` (a fresh sealed
        grant — a posture promotion / re-author / tightening produces a DIFFERENT core ⇒ a different
        id) AND set the old grant to ``REVOKED``, under ONE lock. The non-atomic alternative (``add``
        new + ``set_status(old, REVOKED)`` as two ops) has a crash window that leaves BOTH active → the
        same trigger fires at both postures and the looser (newly-promoted) one wins on overlap = a
        weakening on crash (L2-M3; the ``pending.claim`` lesson, one layer up).

        Two edges closed (L3 codex/complement/nemotron): ``old_binding_id == new_binding.binding_id``
        is not a re-ratification (identical core ⇒ identical id) → raises. And if the old grant is
        ABSENT, the call writes NOTHING and returns ``False`` — re-ratification supersedes an EXISTING
        grant; with nothing to supersede the precondition failed, and writing the new (looser) grant
        anyway would be a fail-OPEN (a caller that ignores the bool would get a live autonomous grant
        with no revoke). Returns True iff the old grant was found + revoked.

        ``precondition`` (Slice 4c — codex L3 HIGH-2 / L1 TOCTOU) is a COMPARE-AND-SWAP guard: the caller
        builds ``new_binding`` from a LOCKLESS read of the old grant, so between that read and this locked
        write a concurrent op could REVOKE/pause the old grant (→ resurrecting it looser under a new id is
        a govern-not-trust violation), TIGHTEN it (→ the stale ``new_binding`` would DROP the freshly-added
        kill), or change its EVIDENCE (→ promoting a grant that no longer qualifies). When supplied,
        ``precondition`` is evaluated against the CURRENT old binding UNDER THE LOCK; ``False`` (or an
        old record that no longer loads) ABORTS the whole op (writes nothing, returns ``False``) so the
        caller re-derives from the fresh state. ``replace_atomic`` stays generic — the graduation-specific
        re-validation lives in the caller's closure."""
        if old_binding_id == new_binding.binding_id:
            raise ValueError(
                f"replace_atomic: old and new binding_id are identical ({old_binding_id!r}) — "
                "re-ratification requires a different core (a no-op promotion is not a re-ratification)"
            )
        self._validate(new_binding)
        record = new_binding.to_dict()
        with self._locked():
            records = self._read_raw(for_mutation=True)
            old_rec = next((r for r in records if r.get("binding_id") == old_binding_id), None)
            if old_rec is None:
                return False   # nothing to supersede → abort, write nothing (fail-closed)
            if precondition is not None:
                # CAS: re-validate the CURRENT old grant under the lock before superseding it. A grant
                # that can no longer be loaded (corrupted since the read) fails closed.
                current_old = self._load(old_rec)
                if current_old is None or not precondition(current_old):
                    _log.warning("binding store: replace_atomic precondition FAILED for %r — the old "
                                 "grant changed since the proposal (revoked/tightened/evidence); aborting",
                                 old_binding_id)
                    return False
            record = self._preserve_bookkeeping(records, new_binding.binding_id, record)
            kept: list[dict[str, Any]] = []
            for r in records:
                rid = r.get("binding_id")
                if rid == new_binding.binding_id:
                    continue                                  # drop (merged into ``record``)
                if rid == old_binding_id:
                    r["status"] = BindingStatus.REVOKED.value  # supersede the old grant
                kept.append(r)
            kept.append(record)
            self._write_raw(kept)
            return True

    def get(self, binding_id: str) -> Binding | None:
        """Return the binding by id, or ``None`` (absent or malformed). NOTE: ``get`` returns the
        binding REGARDLESS of seal/status — it is the inspection path (the cockpit shows a tampered or
        revoked grant). The fire-path must use :meth:`list_active` / check ``binding.is_active``."""
        for r in self._read_raw():
            if r.get("binding_id") == binding_id:
                return self._load(r)
        return None

    def list_all(
        self, *, status: BindingStatus | None = None, trigger_type: str | None = None
    ) -> list[Binding]:
        """All bindings (file order), optionally filtered by ``status`` and/or ``trigger_type``.
        Malformed records are skipped loudly. The full inspection view (includes inactive + tampered);
        the fire-path wants :meth:`list_active`. (Named ``list_all`` not ``list`` so the method never
        shadows the builtin ``list`` for ``list[…]`` annotations in this class — mypy catches that.)"""
        out: list[Binding] = []
        for r in self._read_raw():
            b = self._load(r)
            if b is None:
                continue
            if status is not None and b.status != status:
                continue
            if trigger_type is not None and b.trigger.type != trigger_type:
                continue
            out.append(b)
        return out

    @staticmethod
    def is_fireable(b: Binding) -> bool:
        """The single canonical FIRE-VIEW predicate (shared by :meth:`list_active` +
        :meth:`snapshot_if_fireable` + the Slice-4c graduation-apply's in-memory pre-persist check):
        True iff ``b`` may fire NOW — ACTIVE status AND a valid seal AND (not confirm-class OR a
        kill-bearing SEALED-floor guard). A mandatory kill is checked on the sealed ``guard`` ONLY
        (never ``effective_guard``) — an unsealed addition can never satisfy the confirm-class mandate.
        PUBLIC (Slice 4c) so the graduation re-ratification can assert a freshly-minted promoted binding
        is fireable BEFORE ``replace_atomic`` persists it ACTIVE (defense in depth — a promoted binding
        inherits the old's sealed guard so it is fireable by construction, but the assert fails LOUD if a
        loosening ever produced a non-fireable grant). Keep in lockstep with list_active's per-reason
        logging below."""
        return (b.status.is_active and b.seal_matches()
                and (not b.posture.needs_confirm or any(g.has_kill for g in b.guard)))

    def list_active(self, *, trigger_type: str | None = None) -> list[Binding]:
        """The FIRE-PATH view: bindings that may actually fire — active status, a valid seal, AND (at
        confirm-class) a kill-bearing guard. A seal-mismatched binding (its core was tampered) is
        EXCLUDED and logged LOUDLY: a tampered standing grant is structurally barred from autonomous
        firing (fail-closed, ``structural_invariants_beat_discipline``).

        **A confirm-class (``posture.needs_confirm``) binding that lacks a kill-bearing guard is also
        EXCLUDED** (codex-HIGH2 / complement-LOW1 / L1-MED1): the ``RECEIPT_VERSION=4`` mandate — a
        binding-driven confirm-class fire requires a guard — is made STRUCTURAL at the fire view, not
        left to compiler discipline. The compiler is the only path that SHOULD mint such a binding, but
        a direct ``Binding.create`` / seed / future seam could bypass it; the fire-view fails closed
        regardless. (The ADVERSARIAL-authorship half of the mandate stays compiler-only — it needs the
        compiler's own substrate id, which this corpus/substrate-AGNOSTIC core does not have; this
        enforces the structural half — presence of a kill — that the core CAN check.) ``trigger_type``
        narrows to the bindings a given event type could fire."""
        out: list[Binding] = []
        for r in self._read_raw():
            b = self._load(r)
            if b is None:
                continue
            if not b.status.is_active:
                continue
            if trigger_type is not None and b.trigger.type != trigger_type:
                continue
            # the fire-view filter (:meth:`is_fireable`) — active here, so a non-fireable record is a
            # SEAL MISMATCH or the confirm-class kill-mandate gap; log WHICH (the invisible-infrastructure
            # signal) but route both through the one canonical predicate so the fire-view + the per-id
            # snapshot_if_fireable can never drift apart.
            if not self.is_fireable(b):
                if not b.seal_matches():
                    _log.warning(
                        "binding store: SEAL MISMATCH on active binding %r — barred from the fire set "
                        "(its governance core was altered without re-ratification)", b.binding_id,
                    )
                else:
                    # the mandatory kill is checked on the SEALED floor ``guard`` ONLY (an unsealed
                    # ``guard_additions`` kill could be stripped without tripping the seal, so it can ADD
                    # kills the runtime honors but NEVER satisfy the confirm-class mandate — Slice 3a.5).
                    _log.warning(
                        "binding store: confirm-class binding %r has NO kill-bearing guard in its SEALED "
                        "floor — barred from the fire set (RECEIPT_VERSION=4: a binding-driven confirm-class "
                        "fire requires a SEALED guard with a kill; unsealed additions don't count)", b.binding_id,
                    )
                continue
            out.append(b)
        return out

    def snapshot_if_fireable(self, binding_id: str) -> Binding | None:
        """A FRESH re-read returning the binding iff it is currently FIREABLE (the same canonical
        :meth:`is_fireable` filter ``list_active`` applies), else ``None``. The STANDING-binding
        counterpart to :meth:`claim_one_shot` (which is the one-shot's atomic claim): the fire-path
        re-acquires a fresh fireable snapshot IMMEDIATELY before firing so a concurrent
        pause/revoke/seal-break/tighten committed SINCE the (lockless, possibly-stale) ``list_active``
        is caught here — a revoked grant does not fire (the unsafe direction the fire-path must close),
        and a freshly-tightened guard's NEW kill IS evaluated (the dispatcher rebuilds the kill set from
        THIS snapshot). NO mutation (a standing grant fires repeatedly, it is never claimed). Returns
        ``None`` for absent / malformed / not-currently-fireable. The residual window (this read → the
        gate fire) is irreducible — the lock cannot be held across the gate's I/O — but it is the same
        immediately-before-fire re-acquire the ``pending.claim`` precedent uses."""
        b = self.get(binding_id)
        return b if (b is not None and self.is_fireable(b)) else None

    def set_status(self, binding_id: str, status: BindingStatus) -> bool:
        """Change a binding's LIFECYCLE state — a GOVERNED verb with a transition policy
        (``_ALLOWED_TRANSITIONS``): ``REVOKED`` is terminal, ``EXPIRED`` only cleans up to ``REVOKED``,
        and NOTHING inert re-activates (the structural close on the silent-reactivation hole, L2-H1).
        An illegal transition RAISES ``ValueError`` (a governance violation fails loud, never a silent
        no-op); an idempotent ``X→X`` is allowed. Returns True iff the binding was present; False if
        absent. Locked read-modify-write; the seal is unaffected (``status`` is not sealed)."""
        if not isinstance(status, BindingStatus):
            raise TypeError(f"status must be a BindingStatus, got {type(status).__name__}")
        with self._locked():
            records = self._read_raw(for_mutation=True)
            for rec in records:
                if rec.get("binding_id") == binding_id:
                    try:
                        current = BindingStatus(rec.get("status"))
                    except ValueError as e:
                        raise ValueError(
                            f"binding {binding_id!r} has an unparseable status {rec.get('status')!r}"
                        ) from e
                    if status != current and status not in _ALLOWED_TRANSITIONS[current]:
                        raise ValueError(
                            f"illegal lifecycle transition {current.value} → {status.value} "
                            f"for binding {binding_id!r} (revive a revoked/expired grant by re-ratifying)"
                        )
                    rec["status"] = status.value
                    self._write_raw(records)
                    return True
            return False

    def ratify(self, binding_id: str) -> Binding | None:
        """The governed PAUSED→ACTIVE ratify-write (Slice 4 / the last 3b remainder): take a compiled
        candidate LIVE. The STRUCTURAL fireability gate + the lifecycle write — the operator-facing
        HUMAN gate ("this goes live under my eye") is the ADAPTER's, never auto here.

        REFUSES (raises ``ValueError``) to ratify a candidate that could not actually FIRE, so a
        ratification never produces a silently-BARRED 'active' grant — the ``list_active`` fail-closed
        exclusions surfaced EARLY as a clear refusal instead of a confusing active-but-inert state:
          - a SEAL-broken binding (its floor was altered → re-ratify via :meth:`replace_atomic`, never
            flip a tampered grant live);
          - a CONFIRM-class binding with no sealed-floor kill (the ``RECEIPT_VERSION=4`` mandate
            ``list_active`` enforces — a kill-less confirm-class grant can never fire, so it can never
            ratify);
          - an INERT (REVOKED/EXPIRED) target — you re-ratify a dead grant, you do not flip it.
        An already-ACTIVE binding ratifies IDEMPOTENTLY (returns it). Returns the now-ACTIVE binding, or
        ``None`` if absent. Atomic locked read-modify-write — the fireability checks + the flip happen
        under ONE lock so a concurrent mutation can't slip a non-fireable grant live."""
        with self._locked():
            records = self._read_raw(for_mutation=True)
            for rec in records:
                if rec.get("binding_id") != binding_id:
                    continue
                b = self._load(rec)
                if b is None:
                    raise ValueError(f"ratify: binding {binding_id!r} is malformed — cannot ratify")
                # The fireability checks run BEFORE the already-ACTIVE idempotence return (codex L3): a
                # seal-broken / kill-less-confirm-class binding that is ALREADY active is still barred by
                # list_active, so ratify must REFUSE it (a clear error) rather than bless it with a
                # misleading "already ACTIVE" — the ratify fireability claim must hold for ACTIVE too.
                if b.status.is_inert:   # REVOKED / EXPIRED
                    raise ValueError(
                        f"ratify refuses a {b.status.value} binding {binding_id!r} — re-ratify a "
                        "revoked/expired grant via replace_atomic, never flip it live")
                if not b.seal_matches():
                    raise ValueError(
                        f"ratify refuses a seal-broken binding {binding_id!r} — its floor was altered; "
                        "re-ratify (replace_atomic), never flip a tampered grant live (it would be barred)")
                if b.posture.needs_confirm and not any(g.has_kill for g in b.guard):
                    raise ValueError(
                        f"ratify refuses confirm-class binding {binding_id!r} with NO sealed-floor kill "
                        "— it would be barred from the fire set (RECEIPT_VERSION=4: a binding-driven "
                        "confirm-class fire requires a sealed guard with a kill)")
                if b.status is BindingStatus.ACTIVE:
                    return b   # idempotent — already live AND re-verified still fireable
                # status is PAUSED + fireable (the only remaining case — _ALLOWED_TRANSITIONS bars any
                # other live state) → flip to ACTIVE.
                rec["status"] = BindingStatus.ACTIVE.value
                self._write_raw(records)
                return replace(b, status=BindingStatus.ACTIVE)
            return None

    def record_fire(self, binding_id: str, *, clean: bool, fired_at: str) -> Binding | None:
        """Bump the graduation bookkeeping after a fire: ``fire_count += 1`` always, ``clean_count +=
        1`` iff the fire needed no human correction/denial, and ``last_fired_at = fired_at`` (the ISO
        timestamp the caller supplies — there is no ambient clock here; the dormancy sensor). Returns
        the updated binding, or ``None`` if absent/malformed. The seal is unaffected (graduation is
        outside it), so the ``binding_id`` is stable across increments. Pure bookkeeping — the §2.6
        graduation DECISION and any ONE_SHOT auto-revoke are the fire-path's job (Slice 4), NOT this
        method's (and that auto-revoke must be ATOMIC with the fire when built — the ``pending.claim``
        precedent — not a record_fire-then-set_status two-step). ``clean`` must be a real ``bool`` and
        ``fired_at`` a non-empty string (the official bookkeeping verb must not accept a truthy
        ``"false"`` that would inflate the unsealed evidence that later loosens autonomy — L3 codex).
        Locked read-modify-write."""
        if not isinstance(clean, bool):
            raise TypeError(f"record_fire: clean must be a bool, got {type(clean).__name__}")
        if not isinstance(fired_at, str) or not fired_at:
            raise ValueError("record_fire: fired_at must be a non-empty timestamp string")
        with self._locked():
            records = self._read_raw(for_mutation=True)
            for i, rec in enumerate(records):
                if rec.get("binding_id") == binding_id:
                    b = self._load(rec)
                    if b is None:
                        return None
                    grad = Graduation(
                        fire_count=b.graduation.fire_count + 1,
                        clean_count=b.graduation.clean_count + (1 if clean else 0),
                        last_fired_at=fired_at,
                    )
                    # dataclasses.replace (not a positional rebuild) so a future Binding field can't
                    # silently land in the wrong slot on this hot path (L3 nemotron).
                    updated = replace(b, graduation=grad)
                    records[i] = updated.to_dict()
                    self._write_raw(records)
                    return updated
            return None

    def claim_one_shot(self, binding_id: str) -> Binding | None:
        """ATOMICALLY claim a ONE-SHOT binding for a SINGLE fire (the ``pending.claim`` precedent, one
        layer up). Under the lock: iff the binding is currently FIREABLE (``is_active`` = ACTIVE status
        + valid seal) AND ``one_shot``, set it ``REVOKED`` and return the PRE-CLAIM (still-ACTIVE)
        snapshot; else return ``None``.

        At-MOST-once: two concurrent dispatches can never both claim — the first flips it ``REVOKED``
        under the lock, the second sees a non-active record → ``None`` → it does not fire. The CLAIM
        happens BEFORE the fire (the dispatcher fires the returned snapshot), so a crash mid-fire DROPS
        the binding (already revoked, not re-fireable) rather than leaving it re-fireable — fail-CLOSED
        (at-most-once over at-least-once; a failed effect does NOT retry a one-shot). The returned
        snapshot carries its pre-claim ACTIVE status so the caller can still mint authority
        (:func:`binding_invocation` REFUSES a non-active binding) for the fire it was claimed for; the
        STORE record is now ``REVOKED`` (spent). NOT for a standing binding (returns ``None`` — a
        standing grant fires repeatedly, it is never claimed/spent). Locked read-modify-write.

        ATTEMPTED-once, not fired-once (L3 review — the one-shot+gate-outcome semantic): the dispatcher
        claims BEFORE the gate rules, so the grant is spent by the dispatch ATTEMPT — including a gate
        KILL (a known-danger trigger), a §1.5 REFUSE (a tampered/injection payload), or a DEFER. A
        one-shot whose FIRST matching event is dangerous is therefore disarmed without ever performing X
        (an availability/DoS surface: a spoofed kill-tripping event permanently defuses the grant). This
        is the safe direction (it never fires when it should not), and the claim-before-gate ordering is
        load-bearing for at-most-once across the cooling-off PROPOSE path too (else N matching events →
        N pendings → N fires). The "re-arm a one-shot the gate refused to even attempt" refinement is a
        deferred lifecycle decision (Slice 4d), not a 4a behavior."""
        with self._locked():
            records = self._read_raw(for_mutation=True)
            for rec in records:
                if rec.get("binding_id") != binding_id:
                    continue
                b = self._load(rec)
                # is_active = ACTIVE status AND a valid seal → a tampered/paused/spent one-shot is NOT
                # claimable (fail-closed); a standing (non-one_shot) binding is never claimed.
                if b is None or not b.one_shot or not b.is_active:
                    return None
                rec["status"] = BindingStatus.REVOKED.value
                self._write_raw(records)
                return b   # the pre-claim ACTIVE snapshot (the on-disk record is now REVOKED)
            return None

    def tighten_guard(self, binding_id: str, *new_guards: Guard) -> Binding | None:
        """Append guard(s) to a binding's UNSEALED tightening tier (``guard_additions``) — the Slice
        3a.5 monotonic-editability verb. TIGHTEN-FREE: it never re-mints (the seal covers only the
        floor, left untouched), so the ``binding_id`` AND the graduation track record are PRESERVED
        across a safety hardening — the whole point (a re-ratification would reset the grant's identity
        + evidence). Monotonicity is STRUCTURAL, not policed: this verb can ONLY add guards — there is
        no path through it to remove or weaken a floor guard, so it cannot loosen. (To LOOSEN — drop or
        weaken a ratified guard — you must touch the sealed floor, which a seal mismatch bars from
        firing; re-ratify via :meth:`replace_atomic` instead. ``structural_invariants_beat_discipline``:
        loosening is not a rejected tighten, it is simply not expressible here.)

        Returns the updated binding, or ``None`` if absent / malformed. RAISES ``ValueError`` on an
        inert (revoked/expired) target — you re-ratify a dead grant, you do not tighten it — or on a
        seal-broken target (a tampered floor is barred, never edited). Each new guard's kill (if any) is
        held to the FULL compile-grade kill gate BEFORE the lock — purity + the injected validator AND
        the DRILL-TRIP gate (the drill must actually trip the kill on the diverse substrate; complement
        L3) — so the tighten path is NOT a second-class compile citizen: a kill the drill lets through,
        or an impure kill, is refused, never persisted. An empty ``new_guards`` is a no-op. Locked
        read-modify-write; the seal is unaffected (additions are unsealed), so the recomputed floor-seal
        still matches + the id is stable."""
        if not new_guards:
            return self.get(binding_id)
        if not all(isinstance(g, Guard) for g in new_guards):
            raise TypeError("tighten_guard: every new guard must be a Guard")
        for g in new_guards:
            if g.kill_predicate is not None:
                self._validate_kill(g.kill_predicate)
                # the drill-trip gate (parity with compile_binding): a tightening kill's drill must
                # ACTUALLY trip it on the diverse substrate — a kill the drill lets through is no kill.
                # (Guard's kill-travels-as-a-unit invariant guarantees kill_drill is present here.)
                if kill_outcome(g.kill_predicate, g.kill_drill) is Kleene.FALSE:
                    raise ValueError(
                        f"tighten_guard: kill_drill {g.kill_drill!r} does NOT trip its kill — a drill "
                        "must be a concrete event that TRIPS the kill (an unverified kill is no kill)")
        with self._locked():
            records = self._read_raw(for_mutation=True)
            for i, rec in enumerate(records):
                if rec.get("binding_id") != binding_id:
                    continue
                b = self._load(rec)
                if b is None:
                    return None
                if b.status.is_inert:
                    raise ValueError(
                        f"tighten_guard refuses an inert binding {binding_id!r} (status={b.status.value})"
                        " — re-ratify a revoked/expired grant, don't tighten it")
                if not b.seal_matches():
                    raise ValueError(
                        f"tighten_guard refuses a seal-broken binding {binding_id!r} — its floor was "
                        "altered (barred from the fire set), not editable; re-ratify instead")
                # dataclasses.replace (not a positional rebuild) so a future Binding field can't land in
                # the wrong slot; additions are UNSEALED so the id + seal are unchanged by construction.
                updated = replace(b, guard_additions=b.guard_additions + tuple(new_guards))
                records[i] = updated.to_dict()
                self._write_raw(records)
                return updated
            return None

    def remove(self, binding_id: str) -> bool:
        """HARD-delete a binding (no audit record). Returns True iff present. Governance prefers
        ``set_status(REVOKED)`` (the grant stays in the registry for audit); ``remove`` is for genuine
        cleanup (a fired one-shot, a test). Locked read-modify-write."""
        with self._locked():
            records = self._read_raw(for_mutation=True)
            kept = [r for r in records if r.get("binding_id") != binding_id]
            if len(kept) == len(records):
                return False
            self._write_raw(kept)
            return True
