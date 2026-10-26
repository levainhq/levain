"""levain.autonomic.graduation — the §2.6 meta-loop DECISION (Slice 4c).

The binding layer (3a-4b) compiles, seals, fires, and chains a standing grant, and the fire-path
ACCRUES a track record (``Graduation.fire_count`` / ``clean_count`` — bumped by ``record_fire`` on
every clean fire, in-band AND out-of-band). This module closes the loop: it reads that track record
and PROPOSES a posture loosening — "fired N× clean → promote one rung toward autonomy?".

This is **Memory-Is-Governance at the action layer** (``agent_authority_model.md`` §2.6): the operator
governs the GRADIENT (ratifies the looser posture against the evidence), the agent earns its way DOWN it
with evidence, and the constitutional risk FLOOR is the limit graduation can never punch. The decision
NEVER promotes autonomously — it returns a :class:`GraduationProposal` the operator ratifies
(govern-not-trust; the §2.6 "approve POLICY changes, not individual actions").

**The mechanism (why a re-ratification, not an in-place edit).** A binding's posture is SEALED. A
posture promotion is a DIFFERENT governance core → a different id → a re-ratification: mint a NEW sealed
grant at the looser posture and ``replace_atomic`` it for the old. The fire-path's seam #2 (Slice 4c)
fires a binding at ``max(risk_floor(risk), ratified_posture)`` — the operator-ratified posture down to
the unbuyable floor — so a promoted grant ACTUALLY fires looser (under the 4a/4b ``max(policy, ratified)``
it would have been re-floored back up to the static ``earned_posture`` → silent theater).

**The three gates (all must hold to propose).**
  (a) EVIDENCE — ``clean_count >= min_clean_fires`` AND ``clean_count == fire_count`` (ALL-clean). Today
      ``clean == fire`` always (no post-hoc demotion channel — that is 4d); the equality is checked so
      4c is forward-correct for when 4d's demotion makes the two diverge.
  (b) NOT DEPTH-BOUNDED — no EXTERNAL link at hops >= :data:`DEEP_OUTBOUND_MIN_HOPS`. A deep-outbound
      chain's confirm does real EPISTEMIC work ("the operator approving IS the grounding the chain lost",
      ``awareness_bus_governance_design.md`` § "deep-chain confirm is epistemically necessary, not
      trust-tax"), so it canNOT be graduated away by track record the way a shallow lane can. DERIVED at
      decision time from the SEALED goal (per-link ``external`` from the sealed tools, hops = link_index)
      — NEVER a stored flippable bool (the 3a ``Graduation`` comment: storing it would let a tamper
      re-enable autonomy escalation on the most dangerous chains, seal-blind).
  (c) ROOM TO LOOSEN — the current posture is STRICTLY above the unbuyable risk floor, so one rung looser
      is still >= the floor (graduation earns DOWN toward the floor, never below — §1.4).

Stdlib-only; corpus-agnostic; PURE (the per-binding + per-link risk are INJECTED resolvers, structurally
bound to the sealed tools — the core never imports the embedder's manifest). The decision is deterministic.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from levain.autonomic.binding import Binding, BindingStatus, BindingStore, binding_family
from levain.autonomic.coverage import (COVER_NONE, Cover, CoverageSource, CoverageView, Effect, GraduationPolicy,
                                       _is_cover, authority_keys, covered, live)
from levain.autonomic.policy import risk_floor
from levain.autonomic.posture import Posture
from levain.autonomic.risk import ActionManifest, ActionRisk, UnknownAction

__all__ = [
    "DEEP_OUTBOUND_MIN_HOPS",
    "DEFAULT_MIN_CLEAN_FIRES",
    "RiskResolver",
    "LinkRiskResolver",
    "GraduationProposal",
    "one_rung_looser",
    "is_depth_bounded",
    "propose_graduation",
    "LinkActionResolver",
    "LinkCover",
    "GraduationStep",
    "graduate_due",
]

_log = logging.getLogger("levain.autonomic.graduation")

# The §2.6 chain-depth bound: a chain is "deep-outbound" (does NOT graduate) iff it has an EXTERNAL link
# at hops >= this — an outbound link whose content is grounded in >= 1 hop of upstream AGENT OUTPUT (link 0
# fires directly on the authenticated trigger; link 1+ is grounded in agent processing the human never
# saw). 1 is the principled minimum (any agent-output-grounded outbound). A single-link / hops-0 outbound
# is NOT depth-bounded here — its external risk floor (>= CONFIRM, §1.4) already bars graduation below
# confirm, so the floor handles it; the depth-bound is the EXPLICIT governance refusal on top, for the
# DEEP case where the confirm does epistemic work the floor alone wouldn't name.
DEEP_OUTBOUND_MIN_HOPS = 1

# The §2.6 worked-example evidence bar ("fired 20× at confirm, all clean → promote"). The flow adapter's
# ``graduate`` CLI exposes a ``--min-clean`` override (e.g. for a demo); production defaults to this.
DEFAULT_MIN_CLEAN_FIRES = 20

# The injected risk seams (structurally bound to the SEALED tools — the core stays manifest-agnostic).
#   RiskResolver:     (binding) -> ActionRisk     — the WHOLE-binding aggregate risk, for the floor (the
#                                                   SAME ``aggregate_risk`` the binding was ratified at).
#   LinkRiskResolver: (binding, link_index) -> ActionRisk — link i's risk from its sealed tools (the SAME
#                                                   per-link resolver the chain executor uses), for the
#                                                   depth-bound's per-link ``external`` check.
RiskResolver = Callable[[Binding], ActionRisk]
LinkRiskResolver = Callable[[Binding, int], ActionRisk]


def one_rung_looser(posture: Posture) -> Posture | None:
    """The next rung TOWARD autonomy (one less operator involvement), or ``None`` if already at the
    most-autonomous rung (``ABOVE_LOOP``). The :class:`Posture` IntEnum is ordered by INCREASING
    involvement, so 'looser' = ``value - 1``."""
    if posture.value <= Posture.ABOVE_LOOP.value:
        return None
    return Posture(posture.value - 1)


def is_depth_bounded(binding: Binding, link_risk_resolver: LinkRiskResolver) -> bool:
    """True iff ``binding`` is a DEEP-OUTBOUND chain that must NOT graduate (§2.6 bounded by chain-depth).
    It is depth-bounded iff it has an EXTERNAL (outbound) link at hops >= :data:`DEEP_OUTBOUND_MIN_HOPS`
    — an outbound link grounded in >= 1 hop of upstream AGENT OUTPUT, where the confirm does real
    epistemic work track record cannot retire. DERIVED from the SEALED goal (per-link ``external`` from
    the sealed tools, hops = link_index). FAIL-CLOSED: a per-link risk-resolution fault (an undeclared
    tool — e.g. a manifest that dropped one post-ratification) is treated as DEPTH-BOUNDED (an
    unclassifiable link must never be assumed shallow-and-graduatable).

    ⚠ This keys on ``external`` ALONE — narrower than its own "agentic distance degrades grounding"
    rationale (an IRREVERSIBLE-but-internal link at hops>=1 also does epistemic work the confirm earns).
    It is CORRECT today only as a COMPOSITION with the unbuyable floor: ``risk_floor`` independently lifts
    ``reversible=False`` / ``financial`` / ``CRITICAL`` to >= CONFIRM (policy.py), and graduation's
    room-check (``current <= floor``) blocks graduating below it — so the floor covers the irreversible-
    internal case the depth-bound misses. The current manifest has NO irreversible-internal tool
    (``mail.send`` is the only irreversible one, and it is external), so the gap is inert. **If an
    irreversible-internal tool is ever added, extend this to ``external OR not reversible at hops>=1``**
    (L2 L3) — don't let the depth-bound silently lean entirely on the floor for that case."""
    for i in range(len(binding.goal)):
        if i < DEEP_OUTBOUND_MIN_HOPS:
            continue  # link 0 (hops 0) fires on the authenticated trigger — not "deep" agent output
        try:
            risk = link_risk_resolver(binding, i)
        except Exception:  # noqa: BLE001 — an unclassifiable link bars graduation, fail-closed
            _log.warning("graduation: binding %s link %d risk is unclassifiable — treating as "
                         "DEPTH-BOUNDED (fail-closed, no graduation)", binding.binding_id, i)
            return True
        if risk.external:
            return True
    return False


@dataclass(frozen=True)
class GraduationProposal:
    """A §2.6 graduation proposal: ``source`` has earned a one-rung-looser posture (``to_posture``). NOT
    applied — a proposal the operator RATIFIES (govern-not-trust). :meth:`build_promoted` mints the
    re-ratified grant; the adapter applies it via ``BindingStore.replace_atomic`` (atomic: persist the new
    grant ACTIVE + revoke the old)."""

    source: Binding
    to_posture: Posture

    @property
    def old_binding_id(self) -> str:
        return self.source.binding_id

    @property
    def from_posture(self) -> Posture:
        return self.source.posture

    @property
    def clean_count(self) -> int:
        return self.source.graduation.clean_count

    @property
    def fire_count(self) -> int:
        return self.source.graduation.fire_count

    def build_promoted(self, *, status: BindingStatus = BindingStatus.PAUSED,
                       created_at: str | None = None) -> Binding:
        """Mint the re-ratified (promoted) binding: SAME trigger / goal / tightness / one_shot + the
        SAME sealed-floor ``guard`` AND ``guard_additions`` (a graduation must NEVER drop a safety kill),
        the looser ``to_posture``, and a FRESH ``Graduation()`` (each rung earns its OWN track record —
        seam 5: clean fires earned at a stricter posture don't prove clean fires at a looser one, and it
        blocks graduation-laundering). A DIFFERENT sealed core (the looser posture) → a DIFFERENT id →
        ``replace_atomic`` supersedes the old grant.

        Defaults to ``PAUSED`` (codex L3 / L2 — FAIL-CLOSED, matching ``Binding.create``'s deliberate
        PAUSED default: a caller that forgets the lifecycle gets a NON-firing grant, never a live looser
        autonomous one — the graduation human-gate must be STRUCTURE, not discipline). The graduation
        APPLY path passes ``status=ACTIVE`` EXPLICITLY — the operator's ``graduate --yes`` IS the
        policy-change ratification (§2.6), and the adapter asserts ``is_fireable`` before ``replace_atomic``
        persists it ACTIVE. ``created_at`` defaults to the source's (PURE — no clock in the core; the id
        still differs by posture); the adapter passes a fresh timestamp to stamp the re-ratification time."""
        promoted = Binding.create(
            created_by=self.source.created_by,
            created_at=created_at if created_at is not None else self.source.created_at,
            trigger=self.source.trigger,
            goal=self.source.goal,
            tightness=self.source.tightness,
            posture=self.to_posture,
            one_shot=self.source.one_shot,
            status=status,
            guard=self.source.guard,
            graduation=None,  # FRESH (seam 5) — Binding.create defaults to Graduation()
        )
        # carry the UNSEALED tightening additions forward (extra kills the operator hardened in — a
        # graduation that loosens the posture must not also DROP safety). Additions are outside the seal,
        # so ``replace`` leaves the new id + floor-seal intact; the store re-validates each kill on persist.
        if self.source.guard_additions:
            promoted = replace(promoted, guard_additions=self.source.guard_additions)
        return promoted


def propose_graduation(
    binding: Binding,
    *,
    risk_resolver: RiskResolver,
    link_risk_resolver: LinkRiskResolver,
    min_clean_fires: int = DEFAULT_MIN_CLEAN_FIRES,
) -> GraduationProposal | None:
    """The §2.6 graduation DECISION (pure, deterministic, corpus-agnostic): does this binding's CLEAN
    track record earn a one-rung-looser posture? Returns a :class:`GraduationProposal` (the operator
    ratifies it — NEVER an autonomous promotion) or ``None``. Proposes iff ALL of:
      (a) EVIDENCE — ``clean_count >= min_clean_fires`` AND ``clean_count == fire_count`` (all-clean);
      (b) the binding is currently ACTIVE + valid-seal (``is_active``) — a paused/revoked/tampered grant
          is not earning a looser posture;
      (c) NOT depth-bounded (no external link at hops >= DEEP_OUTBOUND_MIN_HOPS — deep-outbound chains
          don't graduate);
      (d) ROOM — the current posture is STRICTLY above the unbuyable risk FLOOR.
    FAIL-CLOSED: an unclassifiable binding (the whole-binding ``risk_resolver`` raises — a manifest that
    dropped a tool post-ratification) does NOT graduate. ``to_posture`` = one rung looser (guaranteed
    >= the floor by (d))."""
    if min_clean_fires < 1:
        raise ValueError(f"min_clean_fires must be >= 1, got {min_clean_fires}")
    grad = binding.graduation
    # (a) evidence: enough clean fires AND all-clean (clean == fire; forward-correct for 4d's demotion).
    if grad.clean_count < min_clean_fires or grad.clean_count != grad.fire_count:
        return None
    # (b) only a currently-FIREABLE, untampered, repeating grant graduates. Use the canonical FIRE-VIEW
    # predicate (``is_fireable``), NOT just ``is_active`` (codex L3 MED): ``is_active`` is status+seal
    # only, so a confirm-class ACTIVE binding with NO sealed-floor kill — BARRED from firing by
    # ``list_active`` — would otherwise graduate on (unsealed, injectable) evidence into a kill-mandate-
    # free ON_LOOP grant that DOES fire, converting a barred grant into a fireable one. And a ONE-SHOT
    # never graduates (it fires once then revokes; promoting it on injected/stale evidence is meaningless).
    if binding.one_shot or not BindingStore.is_fireable(binding):
        return None
    # (c) the §2.6 chain-depth bound FIRST — a CATEGORICAL refusal (this kind of chain never graduates,
    # the confirm does real epistemic work), checked before the mechanical floor-room limit so a
    # deep-outbound chain surfaces the GOVERNANCE reason even when it also happens to sit at its floor.
    if is_depth_bounded(binding, link_risk_resolver):
        return None
    # (d) ROOM — the unbuyable floor from the SEALED tools (fail-closed on an unclassifiable grant).
    try:
        floor = risk_floor(risk_resolver(binding))
    except Exception:  # noqa: BLE001 — a grant we can no longer classify does NOT earn looser
        _log.warning("graduation: binding %s risk is unclassifiable — NOT graduating (fail-closed)",
                     binding.binding_id)
        return None
    current = binding.posture
    if current.value <= floor.value:
        return None  # already AT (or below) the floor — nothing left to earn
    looser = one_rung_looser(current)
    # current > floor >= ABOVE_LOOP, so one_rung_looser is never None here; assert for the type-narrower.
    assert looser is not None and looser.value >= floor.value
    return GraduationProposal(source=binding, to_posture=looser)


# 14b S4 (DESIGN_REV2 §5, DESIGN_REV3 §6): the engine step that takes a proposal under a signed graduation policy.
#   LinkActionResolver: (binding, link_index) -> the action name link i's request builder emits: the honest engine's
#   record of what it builds, never the fire path's input. The gate judges the action that runs, so this must be the
#   same name the builder puts on the request.
LinkActionResolver = Callable[[Binding, int], str]

_STEP_STAMP = "%Y-%m-%dT%H:%M:%S.%fZ"

# RULINGS_R2 item 4: the replace refusals that mean the store moved after the walk read the binding (a revoke, a pause,
# a fire, or another graduate run that already superseded it, under its id or this run's): the step is ``stale``.
# replace_atomic's own compares (is_fireable, then equality with the grant as read) are the one check; the race
# tests in test_autonomic_graduation_due.py pin them, so no caller-side restatement
_STALE = frozenset({"target_exists", "old_absent", "old_not_fireable", "old_changed"})


@dataclass(frozen=True)
class LinkCover:
    """One link of a proposed step: the action it runs, the manifest's risk for it (``None``: the manifest does not
    declare it, so nothing covers it), what the view said of it, and the live policies of that view that cover it
    (empty when the view names none: a view that is not a snapshot, or one whose policies disagree with its answer).
    """

    link_index: int
    action_name: str
    action_risk: ActionRisk | None
    cover: Cover
    policies: tuple[GraduationPolicy, ...] = ()

    def covers(self, rung: Posture) -> bool:
        """A live anchored policy signs this action at this risk and lets it run at ``rung``: the view's answer keeps
        the gate's own invariant (``coverage._is_cover``, RULINGS_R1 item 6) and names a rung no stricter."""
        return _is_cover(self.cover) and self.cover.posture is not None and self.cover.posture <= rung


@dataclass(frozen=True)
class GraduationStep:
    """What :func:`graduate_due` did with one proposal.

    ``outcome`` is ``graduated`` (the promoted grant replaced the old one and its audit row committed with it),
    ``preview`` (covered, ``apply=False``: nothing was written), ``uncovered`` (some link's action no live anchored
    policy signs at its manifest risk down to ``to_posture``: the card), ``unresolved`` (a link's action could not be
    named: nothing covers what cannot be named), ``unattributed`` (covered, but a policy covering a link cannot be
    named, or names no ``min_clean_fires``: nothing is written), ``error:<type>`` (refreshing the coverage, building
    the promoted grant, the audit row or the replace raised: this binding's step is not taken; the others are), or the
    :class:`~levain.autonomic.binding.ReplaceResult` reason that refused the replace."""

    outcome: str
    proposal: GraduationProposal
    family: str
    links: tuple[LinkCover, ...]
    new_id: str | None = None
    authority_head: tuple[int, str] | None = None
    # where the engine ran: the binding store's directory and, when the coverage source names it, the cockpit data
    # root whose authority log it reads (the card's line signs a policy for that store, in that log)
    store_dir: str | None = None
    cockpit_root: str | None = None

    @property
    def uncovered(self) -> tuple[LinkCover, ...]:
        return tuple(link for link in self.links if not link.covers(self.proposal.to_posture))


def _absolute(path: object) -> str:
    # RULINGS_R2 item 3: a path the card's line carries names the same directory from any working directory
    return str(Path(str(path)).expanduser().resolve())


def _cockpit_root_of(coverage: CoverageSource | None) -> str | None:
    root = getattr(coverage, "cockpit_root", None)
    return None if root is None else _absolute(root)


def _family_or_blank(binding: Binding) -> str:
    try:
        return binding_family(binding)
    except Exception:  # noqa: BLE001 - an error step names the family when it can, never raises for it
        return ""


def _step_stamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime(_STEP_STAMP)


def _covering(view: CoverageView, effect: Effect, now: datetime, cover: Cover) -> tuple[GraduationPolicy, ...]:
    """The live policies of ``view`` that cover ``effect`` at ``now`` (coverage's own ``covered`` and ``live``, over the
    snapshot's policies, root file and store id), or ``()`` when ``view`` does not carry them or they do not give the
    answer ``view`` gave (its strictest rung and one of their cover values)."""
    policies, signers, store = (getattr(view, a, None) for a in ("policies", "signers", "store_id"))
    if cover.posture is None or not isinstance(policies, tuple) or signers is None:
        return ()
    try:
        keys = authority_keys(signers)
        hits = tuple(p for p in policies if isinstance(p, GraduationPolicy) and p.store_id == store
                     and covered(p, effect) and live(p, now, keys))
        # RULINGS_R2 item 5: a hit whose rung or cover value cannot be read is a policy that cannot be read
        if not hits or max(p.lowest_posture for p in hits) != cover.posture or \
                cover.value not in {p.cover_value for p in hits}:
            return ()
    except Exception:  # noqa: BLE001 - policies that cannot be read name nothing
        return ()
    return hits


def _walk(links: tuple[LinkCover, ...]) -> int | None:
    """The clean fires the covering policies ask for: the strictest (largest) ``min_clean_fires`` of every policy
    covering any link, at least 1; ``None`` when a link names no covering policy or one names no walk."""
    walks: list[int] = []
    for link in links:
        if not link.policies:
            return None
        for p in link.policies:
            n = getattr(p, "min_clean_fires", None)
            if type(n) is not int or n < 0:
                return None
            walks.append(n)
    return max([1, *walks])


def _link_covers(binding: Binding, view: CoverageView | None, now: datetime, *, manifest: ActionManifest,
                 link_action: LinkActionResolver, link_risk_resolver: LinkRiskResolver,
                 family: str) -> tuple[LinkCover, ...]:
    """Every link's effect as the gate builds it (``Effect(action, manifest risk, tools risk, trigger, family)``) and
    what ``view`` says of it. Raises when a link's action or tools risk cannot be read."""
    out: list[LinkCover] = []
    for i in range(len(binding.goal)):
        action = link_action(binding, i)
        if not isinstance(action, str) or not action:
            raise ValueError(f"link {i} of {binding.binding_id} names no action")
        try:
            action_risk: ActionRisk | None = manifest.risk_of(action)
        except UnknownAction:
            action_risk = None
        effect = Effect(action, action_risk, link_risk_resolver(binding, i), binding.trigger.type, family)
        cover = Cover(None, COVER_NONE) if view is None or action_risk is None else view.cover(effect, now)
        if not _is_cover(cover):
            cover = Cover(None, COVER_NONE)     # what the gate does with the same answer: it covers nothing
        out.append(LinkCover(i, action, action_risk, cover,
                             _covering(view, effect, now, cover) if view is not None else ()))
    return tuple(out)


def _audit_row(step: GraduationStep, at: str, new_id: str, view: CoverageView) -> tuple:
    """The ``graduation_steps`` row of a covered step. ``policy_id`` and ``incarnation`` name every policy that
    covered one of its links (comma-joined when there are several). A link that names no covering policy raises
    ``ValueError``: the row is never written with a policy it cannot name (RULINGS_R1 item 11)."""
    if not step.links or not all(link.policies for link in step.links):
        raise ValueError(f"no covering policy can be named for {step.proposal.old_binding_id}")
    covering = {p for link in step.links for p in link.policies}
    ids = sorted({p.grant_id for p in covering})
    incarnations = sorted({p.incarnation for p in covering})
    head = view.token[0]
    p = step.proposal
    return (at, p.old_binding_id, new_id, step.family, p.from_posture.name, p.to_posture.name, p.clean_count,
            p.fire_count, ",".join(ids), ",".join(incarnations), f"{head[0]}:{head[1]}")


def graduate_due(
    store: BindingStore,
    coverage: CoverageSource | None,
    *,
    clock: Callable[[], datetime],
    manifest: ActionManifest,
    link_action: LinkActionResolver,
    risk_resolver: RiskResolver,
    link_risk_resolver: LinkRiskResolver,
    min_clean_fires: int = DEFAULT_MIN_CLEAN_FIRES,
    apply: bool = True,
) -> list[GraduationStep]:
    """The engine step (DESIGN_REV2 §5, DESIGN_REV3 §6): for each fireable binding :func:`propose_graduation`
    proposes, refresh ``coverage`` (outside any store transaction), mint the promoted grant ACTIVE, then judge each of
    its links' effects against that one view. Covered at the target rung on every link: ``replace_atomic`` it, its
    ``coverage`` callable reading only that view, its risk floor from ``risk_resolver``, and its ``record`` writing the
    ``graduation_steps`` audit row in the replace's own transaction. Otherwise the step is returned uncovered (the
    card), and nothing is written.

    The evidence a covered step needs is the policies' own (RULINGS_R1 item 2): the strictest ``min_clean_fires`` of
    the policies covering its links; a step under it is not returned. ``min_clean_fires`` here is only the walk
    before a step no named policy's walk governs (``uncovered``, ``unresolved``, ``unattributed``) is returned as a
    card.

    ``manifest`` is the gate's (the risk a policy must sign is the manifest's for the action that runs);
    ``link_action`` names the action each link's request builder emits; ``link_risk_resolver`` is the gate's
    ``binding_risk`` (the tools risk of link i). ``coverage`` ``None`` covers nothing. ``apply=False`` writes nothing:
    a covered step is returned as ``preview``. What stops an uncovered binding firing below CONFIRM is the gate's clamp
    at every fire, not this step."""
    out: list[GraduationStep] = []
    where = {"store_dir": _absolute(store.path), "cockpit_root": _cockpit_root_of(coverage)}
    for binding in store.list_active():
        # every clean binding is a candidate: which walk it must have taken is known only once its covers are read.
        # A resolver that raises on this binding leaves it unproposed (logged), never the run: the steps already taken
        # stay as returned (no step is built here, because a step needs the proposal that could not be made)
        try:
            proposal = propose_graduation(binding, risk_resolver=risk_resolver, link_risk_resolver=link_risk_resolver,
                                          min_clean_fires=1)
        except Exception as e:  # noqa: BLE001 - one binding's failure never hides the steps already taken
            _log.warning("graduation: binding %s: no proposal could be made (%s): %s", binding.binding_id,
                         type(e).__name__, e)
            continue
        if proposal is None:
            continue
        # RULINGS_R2 item 1: a refresh or a promoted grant that raises (a locked authority log) is this binding's own
        # error step, as a failed replace is below: the bindings already stepped stay as returned, the rest still step
        try:
            view = coverage.refresh() if coverage is not None else None
            now = clock()
            at = _step_stamp(now)
            # RULINGS_R1 item 4: the step, its card and the replace's own coverage check all judge the PROMOTED
            # grant's links (the grant that will fire), never the old one's
            promoted = proposal.build_promoted(status=BindingStatus.ACTIVE, created_at=at)
            family = binding_family(promoted)
        except Exception as e:  # noqa: BLE001 - one binding's failure never hides the steps already taken
            _log.warning("graduation: binding %s: the step could not be judged (%s): %s", binding.binding_id,
                         type(e).__name__, e)
            out.append(GraduationStep(f"error:{type(e).__name__}", proposal, _family_or_blank(binding), (), **where))
            continue
        try:
            links = _link_covers(promoted, view, now, manifest=manifest, link_action=link_action,
                                 link_risk_resolver=link_risk_resolver, family=family)
        except Exception as e:  # noqa: BLE001 - an effect that cannot be named is covered by nothing
            _log.warning("graduation: binding %s: a link's effect cannot be read (%s): %s", binding.binding_id,
                         type(e).__name__, e)
            if proposal.clean_count >= min_clean_fires:
                out.append(GraduationStep("unresolved", proposal, family, (), **where))
            continue
        head = view.token[0] if view is not None else None
        step = GraduationStep("uncovered", proposal, family, links, authority_head=head, **where)
        if view is None or step.uncovered:
            if proposal.clean_count >= min_clean_fires:
                out.append(step)
            continue
        walk = _walk(links)
        if walk is None:
            # RULINGS_R2 item 2: a walk no policy names is the engine's, as for an uncovered binding's card
            if proposal.clean_count >= min_clean_fires:
                out.append(replace(step, outcome="unattributed"))
            continue
        if proposal.clean_count < walk:
            continue
        if not apply:
            out.append(replace(step, outcome="preview"))
            continue

        def covered_new(new: Binding, rung: Posture, view: CoverageView = view, now: datetime = now) -> bool:
            # reads only the view refreshed above (S3b RULINGS_R2 observation 1); writes nothing
            new_links = _link_covers(new, view, now, manifest=manifest, link_action=link_action,
                                     link_risk_resolver=link_risk_resolver, family=binding_family(new))
            return bool(new_links) and all(link.covers(rung) for link in new_links)

        # RULINGS_R1 item 8: the row is built before anything is written, and a failure of this binding's step is its
        # own error step: the bindings already stepped stay as returned, the rest still step
        try:
            row = _audit_row(step, at, promoted.binding_id, view)

            def record(conn: sqlite3.Connection, row: tuple = row) -> None:
                conn.execute("INSERT INTO graduation_steps (at, old_id, new_id, family, from_posture, to_posture, "
                             "clean_count, fire_count, policy_id, incarnation, authority_head) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)

            result = store.replace_atomic(binding, promoted, coverage=covered_new,
                                          risk_floor_of=lambda b: risk_floor(risk_resolver(b)), record=record)
        except Exception as e:  # noqa: BLE001 - one binding's failure never hides the steps already taken
            _log.warning("graduation: binding %s: the step failed (%s): %s", binding.binding_id, type(e).__name__, e)
            out.append(replace(step, outcome=f"error:{type(e).__name__}"))
            continue
        if not result and result.reason in _STALE:
            _log.warning("graduation: binding %s changed after it was read (%s); nothing written", binding.binding_id,
                         result.reason)
        outcome = "graduated" if result else "stale" if result.reason in _STALE else result.reason
        out.append(replace(step, outcome=outcome, new_id=promoted.binding_id if result else None))
    return out

