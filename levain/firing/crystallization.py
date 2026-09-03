"""levain.firing.crystallization — AN UNATTENDED CONSOLIDATE MAY METABOLIZE, BUT MAY NEVER
CRYSTALLIZE (K4a [6], ``spore-373`` [6]; the ``spore-325`` slice).

**Two different things are called "graduation" around this code, and blocking the wrong one would
break the feature this module exists to enable. The distinction is the first thing to get right:**

- **Pattern graduation (1x → 2x → 3x → 4x, onward, no ceiling) inside the working neocortex.** Citation-validated, performed
  by ``validated_save_continuity`` on every wrap. This is ORDINARY METABOLIZING — it is the whole
  point of letting a seat consolidate at all, and this module does **NOT** touch it.
- **CRYSTALLIZATION — a pattern leaving the working set for the always-loaded bedrock tier**
  (:class:`anneal_memory.CrystalStore`). This is the durable identity layer re-injected into every
  future turn. **THIS is what an unattended wrap may never write**, and it is what this module
  refuses.

WHY THE BOUND EXISTS
====================

K3 built one of the membrane's two efferent edges: action-on-**world** fans in to the human. The
other edge is action-on-**self** — memory mutation — and it has been ungated only because nothing
autonomous ever called it. K4a [6] is the moment that stops being true: a scheduled seat that
consolidates itself is an unsupervised write path into its own cognitive substrate, which is exactly
``spore-325``'s memory-poisoning surface (MPBench ASR 50.46%). Stated plainly: **anything that can
get text into an episode can, one wrap later, be speaking in the entity's constitution** — with no
human anywhere in that path.

So the bound is K3's own move applied one layer in. The seat composes its working neocortex (recall
stops degrading — the entire point of [6]) and **may not promote anything into the always-loaded
tier.** Crystal decisions an unattended composer emits stay proposals; only a human-present
``levain wrap`` ratifies them.

WHY A REFUSING PROXY RATHER THAN A TEST
=======================================

The bound is **already the de facto state**, and that is precisely the problem. Crystallization is
``propose-not-auto``: anneal *surfaces* CONSTITUTION / CRYSTALLIZE / COMPOST candidates in the wrap
package, the composer emits decisions as text, and a consumer must call
:func:`anneal_memory.crystal.parse_crystal_decisions` to execute them — **which Levain does nowhere**
(verified across ``levain/``). Left there, the invariant is an ACCIDENT OF OMISSION: the first commit
that wires up crystal decisions silently makes an unattended seat able to rewrite its own bedrock,
and every existing test still passes. ``wiring_checked_content_unchecked_passes_green`` is the exact
failure mode, so a test asserting "we do not call it today" would document the accident rather than
close it.

A proxy that REFUSES makes it structural: the consolidate path cannot perform the write through the
handle it was given, and an attempt is loud instead of green. ``structural_invariants_beat_discipline``.

⚠ **WHAT THIS IS NOT — the claim above originally read "the write cannot happen, whoever later calls
it", and that was an OVERCLAIM (glm, L3).** This is not an in-process security boundary, and it
cannot be one. Two concrete routes around it: an allow-listed read returns a BOUND METHOD whose
``__self__`` is the raw store, and — decisively — ``CrystalStore`` is constructible from a path that
any code in this process can derive, exactly as :func:`levain.wrap._consolidate` derives it. **No
wrapper can defend against in-process code that goes looking for the underlying object**, so
pretending otherwise would be a check that cannot name the world it fails in.

What it DOES deliver, which is the thing actually at risk: the consolidate hands anneal a handle
that cannot be written through, so a future commit wiring up ``parse_crystal_decisions`` — the
realistic way this invariant dies — fails LOUDLY at the boundary instead of silently promoting into
bedrock with a green suite. Accident-of-omission becomes refusal. Adversarial in-process code is a
different threat model, answered by the crown-jewels floor (which denies the entity's own hands
write access to these files) rather than by this object.

**DENY BY DEFAULT — the allow-list holds the READS, and the polarity is the point.** The consolidate
path touches exactly two instance methods on the crystal store (derived from anneal's call sites,
not guessed: ``_crystal_active_safe`` → ``active()``, and ``prepare_wrap``'s re-warm block →
``surface_rewarm_candidates()``), both pure reads. Everything else — ``crystallize``, ``touch``,
``update``, ``retire``, and anything anneal adds later — is refused. Same polarity as the K3 gate's
file-editor allow-list, for the same reason: **a new upstream method must arrive REFUSED, not
silently permitted.** An allow-list of reads fails safe as anneal evolves; a deny-list of the four
writes known today would not.

:meth:`CrystalStore._safe_level` is reached on the CLASS, never through an instance, so it is
unaffected — noted because it is the one access that looks like it should need allow-listing.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "CRYSTAL_READS",
    "CrystallizationRefused",
    "NoCrystallizeStore",
    "refuse_crystallization",
]


CRYSTAL_READS = frozenset({"active", "surface_rewarm_candidates"})
"""The crystal-store methods a consolidate legitimately needs, and nothing else.

DERIVED FROM ANNEAL'S CALL SITES rather than from the class's public surface: these are the only two
instance attributes ``anneal_memory.continuity`` reaches for on a crystal store across the whole
``prepare_wrap`` → ``validated_save_continuity`` path. Both are pure reads (``active`` loads and
filters; ``surface_rewarm_candidates`` loads, filters and ranks) — neither opens a transaction.

⚠ If a future anneal release makes the consolidate need another READ, this set is the one place to
widen, and widening it is a deliberate, reviewable act. That is the trade being bought: an unknown
new *write* is refused automatically, at the cost of an unknown new *read* needing a one-line
change. For a boundary protecting the always-loaded identity tier, failing toward refusal is the
correct direction.

⚠ **THE ASSUMPTION UNDER THE ALLOW-LIST, named because it is invisible and load-bearing** (codex,
L3, hidden assumptions). Both reads are safe *because* they return DETACHED objects — dicts parsed
out of the JSON store by ``_load()``, not live handles into it — so a caller mutating a returned dict
changes nothing on disk. **If a future anneal returns live mutable state from either method, an
allow-listed READ silently becomes a WRITE SURFACE and this proxy stops meaning what it says.**
⚠ **Verified true in anneal-memory 0.9.6, and the floor has since moved TWICE (0.9.7, 0.9.8)
without this being re-checked** — the sentence carried its own re-check trigger (*"when … bumping
anneal"*) and nothing watched it, which is the deferral-in-prose class: a condition that comes true
quietly with no mechanism to surface it. Treat this as VERIFIED AT 0.9.6 AND UNVERIFIED SINCE, not
as a current guarantee, and re-read `_load()`'s return path against the pinned anneal before relying
on the detachment property."""


class CrystallizationRefused(BaseException):
    """An unattended consolidate attempted to write the crystallized (always-loaded) tier.

    **``BaseException``, NOT ``Exception``, for the reason** :class:`levain.firing.deadline.TurnTimeout`
    **is** — and this is not caution-by-analogy, it is the lesson K4a ⑥ paid for on the device. The
    wall-clock bound's graceful layer was swallowed whole by the agent SDK's broad ``except Exception``
    handlers (five in ``llm.py`` alone), which classified an out-of-band stop as a program error and
    retried into the same dead socket — while the entire unit suite stayed green over it. A boundary
    whose refusal can be caught by code handling ordinary errors is a boundary that reports itself
    armed and does nothing.

    That risk is live here specifically: the consolidate path is dotted with fail-soft handlers by
    DESIGN (a crystal fault must never break a wrap — ``_crystal_active_safe`` and the re-warm block
    both swallow store errors on purpose). Those handlers are correct for their own concern and would
    be exactly wrong applied to this one. The same polarity call the K4a L3 pass already made twice:
    ``reject_turn``'s fail-soft was wrong because its boundary was SECURITY, while ``close()``'s
    ``BaseException`` catch was right because its boundary was resource AVAILABILITY. **This boundary
    is sovereignty — what may rewrite an entity's durable identity with no human present — so it
    fails CLOSED and refuses to be swallowed.**

    ⚠ Consequence for callers: ``except Exception`` will NOT catch this, deliberately.
    :func:`levain.wrap.wrap_entity` names it explicitly so the in-progress wrap is cancelled rather
    than stranded.
    """

    def __init__(self, method: str) -> None:
        super().__init__(
            f"an unattended consolidate tried to write the crystallized tier via "
            f"{method!r}, and was refused. An unattended wrap MAY METABOLIZE (compose the working "
            f"neocortex) but MAY NEVER CRYSTALLIZE (promote into the always-loaded bedrock tier) — "
            f"only a human-present `levain wrap` ratifies that. Reaching this is a Levain defect, "
            f"not an operator error: nothing in the shipped consolidate path should call it."
        )
        self.method = method


class NoCrystallizeStore:
    """A crystal store with its WRITES removed — reads pass through, everything else refuses.

    Composition, not subclassing, and deliberately so: subclassing :class:`CrystalStore` to override
    its writers would inherit every method it grows in future *unrefused*, which is the failure this
    exists to prevent. A wrapper with an explicit read allow-list inverts that — the default for
    anything new is refusal.

    The consolidate still gets everything it legitimately needs, so the shrink gate keeps its
    crystallization credit and the composer still SEES its candidates and can still propose. Only the
    execution of a promotion is unreachable. That is the correct cut: the rejected alternative was
    staging the *whole* consolidate as a proposal, which defeats [6] — a proposal the seat never runs
    on leaves recall degrading exactly as before. Staging the bedrock half is a real bound; staging
    the whole thing is a no-op wearing a safety costume.
    """

    __slots__ = ("_ncs_store",)

    def __init__(self, store: Any) -> None:
        object.__setattr__(self, "_ncs_store", store)

    def __getattr__(self, name: str) -> Any:
        # Reached only when normal lookup fails, which — with `__slots__` and no methods of our own
        # besides the dunders — is every attribute anneal asks for.
        if name in CRYSTAL_READS:
            return getattr(self._ncs_store, name)
        raise CrystallizationRefused(name)

    def __setattr__(self, name: str, value: Any) -> None:
        # Mutating state through the proxy is a write by another route; refuse it the same way
        # rather than letting it reach the wrapped store. (`_store` itself is set via
        # `object.__setattr__` in `__init__`, so it never comes through here.)
        raise CrystallizationRefused(f"set {name}")

    def __repr__(self) -> str:
        return f"NoCrystallizeStore({self._ncs_store!r})"


def refuse_crystallization(store: Any) -> NoCrystallizeStore:
    """Wrap ``store`` so an unattended consolidate can read it but never promote into it.

    Call this at the point the store is handed to the consolidate, NOT at the point the drive mode is
    resolved — ``invariant_must_fire_at_the_point_of_use``. The cred-floor slice paid for the other
    ordering the day before: a policy resolved in one place and consumed in two produced a
    SPLIT-BRAIN FLOOR where the file editor and the bash seatbelt disagreed, caught by mypy rather
    than by review.
    """
    return NoCrystallizeStore(store)
