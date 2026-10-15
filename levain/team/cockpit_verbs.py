"""levain.team.cockpit_verbs: the Team zone's two verbs (TEAM_VIEW_PLAN T3; head design DESIGN_T3_VERBS.md).

``team_fetch`` (C1, offered by Sync while the ledger has a remote) does now what the team hook does on its own every
interval: one fetch and rebase, never a push, on bounded waits (a person's request is open). It exercises no
authority the hook does not, so it is C1 in every install; a standing policy may still raise it.

``team_join_accept`` (T3, offered by Joins pending while a join waits on THIS machine) brings this machine's proposed
key into force for a handle, or takes the ownership offered to this machine's member: the team's trust-root acts. It
is T3 because the cockpit server holds the member's signing key outside any agent's sandbox: at T2 a holder of the
surface token could make the server accept ownership as the operator (a confused deputy). A T3 fires only on a
``laptop-presence`` signature over the broker's stored record, which the token cannot make.

What the operator signs is ``(kind, handle)``. The fingerprint is never a param: the store reads this machine's own,
so a caller cannot name another machine's key. At apply the store decides again, under the team op lock, from ONE
derivation: the act is done (``GitLedger.confirm_own_key`` / ``GitLedger.accept_offer``, the same acts the laptop
commands run), found already done (recorded, nothing committed again), or refused naming what moved since the
signature. Its records live in the clone's own state file, so an ``ALREADY`` survives a restart.

``team_verbs(start)`` returns the two specs and the accept's broker effect for ``make_server`` to merge. Nothing here
reads the ledger at construction: ``start`` is resolved on each fire and each apply.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from levain.cockpit.effects import (
    ApplyOutcome,
    ApplyUnit,
    DownstreamEffect,
    Effect,
    MemberApply,
    PermanentApplyRefusal,
    Snapshot,
    TransientApplyFault,
    UnitApplyResult,
)
from levain.cockpit.team_panels import ACCEPT_VERB, FETCH_VERB, invalidate
from levain.cockpit.verbs import VerbSpec, _const
from levain.writes import EditError

from . import tenure as T
from .ledger_api import LedgerTransport, open_ledger
from .reader import person_text
from .roles import handle_problem
from .transport import Repo, TeamBusy, TeamError

__all__ = ["team_verbs", "TeamJoinStore", "join_effect", "STORE", "KINDS", "TEAM_VERB_NAMES"]

TEAM_VERB_NAMES = (FETCH_VERB, ACCEPT_VERB)

STORE = "team_join"
KINDS = ("key", "owner-offer")
FIELDS = ("kind", "handle")
_PREFIX = "team-join:"
_RECORDS = "cockpit_joins"      # the clone state file's key for this store's records: origin_key -> record
_BUSY = "another levain team operation on this clone is running"
# a person's request is open: the op is waited for briefly and each git network call is bounded (the CLI waits minutes)
OP_WAIT_S = 5.0
NET_TIMEOUT_S = 20.0


def _ledger(start: Path) -> LedgerTransport | None:
    repo = Repo.discover(start)
    return None if repo is None else open_ledger(repo)


# --- team_fetch (C1) ---------------------------------------------------------------------------------------------


def _fetch_fire(start: Path):
    def fire(_scope: Any, _params: dict[str, Any], _target: dict[str, Any], _confirm: bool) -> dict[str, Any]:
        """One bounded fetch. Every refusal is raised before anything changed or names a fetch that changed nothing
        (an ``EditError`` releases the idempotency key, so the same click may try again)."""
        try:
            gl = _ledger(start)
            if gl is None:
                raise EditError("refused", 409, "this install has no team ledger to fetch")
            return {"summary": gl.sync_now(op_wait=OP_WAIT_S, timeout=NET_TIMEOUT_S)}
        except TeamBusy:
            raise EditError("busy", 409, _BUSY) from None
        except TeamError as exc:
            raise EditError("fetch_failed", 502, person_text(
                exc, said="git could not fetch it.", fallback="the remote could not be fetched.")) from None
        finally:
            invalidate()       # whatever happened, the Sync pane's next read shows what the transport recorded
    return fire


# --- team_join_accept (T3, through the broker) -------------------------------------------------------------------


def join_effect(params: Mapping[str, Any], *, origin_key: str) -> Effect:
    """The accept's one effect: a create on ``team_join`` keyed by the pending. Pure. ``ValueError`` refuses params
    (the broker answers it at issue): a kind outside ``KINDS``, a handle the team's own handle rule refuses, or any
    param but ``kind`` and ``handle``."""
    extra = sorted(set(params) - set(FIELDS))
    if extra:
        raise ValueError(f"{ACCEPT_VERB} takes only {list(FIELDS)}, not {extra}")
    kind, handle = params.get("kind"), params.get("handle")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {list(KINDS)}, not {kind!r}")
    problem = handle_problem(handle)
    if problem:
        raise ValueError(problem)
    changes = {"kind": kind, "handle": handle}
    return Effect(_PREFIX + origin_key, STORE, {"absent": True}, changes, dict(changes), FIELDS, ())


def _done(d, kind: str, handle: str, fp: str) -> bool:
    """The act this join names is already in force (a crash between the ledger commit and the record lands here)."""
    if kind == "key":
        return fp in T.key_fps(d.tenure, handle)
    return d.team.owner == handle and not d.tenure.offer and fp in T.key_fps(d.tenure, handle)


def _moved(d, kind: str, handle: str, fp: str) -> str | None:
    """Why the act signed for ``(kind, handle)`` cannot be done on this derivation, or None when it can."""
    if kind == "key":
        holder = next((h for h, lines in d.tenure.pending_keys.items()
                       if fp in {T._fp_or_none(x) for x in lines}), None)
        if holder is None:
            return f"this machine's key is no longer proposed for {handle}; nothing was changed"
        if holder != handle:
            return f"this machine's key is now proposed for {holder}, not {handle}; nothing was changed"
        return None
    off = d.tenure.offer
    if not off:
        return f"the ownership offer to {handle} was withdrawn; nothing was changed"
    if off.get("handle") != handle:
        return f"ownership is now offered to {off.get('handle')}, not {handle}; nothing was changed"
    if fp not in T.key_fps(d.tenure, handle):
        return f"this machine's key is not in force for {handle}; nothing was changed"
    return None


class TeamJoinStore:
    """The broker's store for ``team_join_accept`` (an ``EffectStore``): one record per consented accept, kept in the
    clone's state file under the team op, applied by the team's own acts."""

    name = STORE
    version_fields = FIELDS
    authority_fields = FIELDS
    store_assigned_fields = ("id", "origin_key")

    def __init__(self, start: Path | str) -> None:
        self.start = Path(start)

    @staticmethod
    def _key(resource_key: str) -> str:
        if not resource_key.startswith(_PREFIX) or len(resource_key) == len(_PREFIX):
            raise PermanentApplyRefusal(f"{resource_key!r} is not a team-join resource key")
        return resource_key[len(_PREFIX):]

    @staticmethod
    def _records(gl: LedgerTransport) -> dict[str, dict[str, Any]]:
        recs = gl.state().get(_RECORDS)
        return dict(recs) if isinstance(recs, dict) else {}

    def snapshot(self, resource_key: str) -> Snapshot:
        key = self._key(resource_key)
        gl = _ledger(self.start)
        rec = None if gl is None else self._records(gl).get(key)
        return Snapshot(None, None) if not isinstance(rec, dict) else Snapshot(dict(rec), str(rec.get("sha") or ""))

    def units(self, effects: Sequence[tuple[int, Effect]]) -> list[ApplyUnit]:
        return [ApplyUnit(STORE, (i,), (e,)) for i, e in effects]

    def label_fields(self, effect: Effect, stored: Mapping[str, Any] | None) -> tuple[str, ...]:
        if stored is None or effect.projection is None:
            return ()
        return tuple(f for f in effect.writes if stored.get(f) == effect.projection.get(f))

    def apply_unit(self, unit: ApplyUnit) -> UnitApplyResult:
        (effect,) = unit.effects
        if not effect.is_create:
            raise PermanentApplyRefusal("the team-join store takes creates only")
        key = self._key(effect.resource_key)
        kind, handle = effect.changes.get("kind"), effect.changes.get("handle")
        if kind not in KINDS or handle_problem(handle):
            raise PermanentApplyRefusal(f"not a team join: {kind!r} {handle!r}")
        gl = _ledger(self.start)
        if gl is None:
            raise PermanentApplyRefusal("this install has no team ledger")
        try:
            with gl.op(timeout=OP_WAIT_S):
                return self._apply_in_op(gl, key, str(kind), str(handle))
        except TeamBusy as exc:
            raise TransientApplyFault("TeamBusy", str(exc)) from exc
        finally:
            invalidate()

    def _apply_in_op(self, gl: LedgerTransport, key: str, kind: str, handle: str) -> UnitApplyResult:
        rec = self._records(gl).get(key)
        if isinstance(rec, dict):
            return UnitApplyResult(ApplyOutcome.ALREADY, (MemberApply(str(rec.get("id")), dict(rec)),))
        try:
            if gl.remote:
                # the act is decided on the team as it is now, as the laptop commands fetch first: a key removed or
                # an offer withdrawn since the signature refuses here, not after a commit the remote then undoes
                gl.sync_now(push=False, op_wait=OP_WAIT_S, timeout=NET_TIMEOUT_S)
            gl.forget_derivation()
            d = gl.derivation()
            fp = gl.own_fingerprint()
        except TeamBusy:
            raise
        except TeamError as exc:      # an unreachable remote, an unreadable ledger or key: the consented act waits
            raise TransientApplyFault("TeamError", person_text(exc)) from exc
        if not _done(d, kind, handle, fp):
            why = _moved(d, kind, handle, fp)
            if why is not None:
                raise PermanentApplyRefusal(why)
            try:
                # committed here only; the push below is bounded, and one that fails leaves the act for the next sync
                if kind == "key":
                    gl.confirm_own_key(d, push=False)
                else:
                    gl.accept_offer(d, push=False)
            except TeamError as exc:
                gl.forget_derivation()
                if not _done(gl.derivation(), kind, handle, fp):
                    raise PermanentApplyRefusal(person_text(exc)) from exc
            if gl.remote:
                try:
                    gl.sync_now(push=True, op_wait=OP_WAIT_S, timeout=NET_TIMEOUT_S)
                except TeamError:
                    pass      # the act is in force on this clone; the next sync publishes it
                gl.forget_derivation()
                if not _done(gl.derivation(), kind, handle, fp):
                    # the sync re-landed this clone's change on a newer remote, and a change there undid it
                    raise PermanentApplyRefusal(f"a newer team change landed first; the {kind} for {handle} is not "
                                                "in force")
        rec = {"id": gl.head(), "origin_key": key, "kind": kind, "handle": handle, "sha": gl.head(),
               "at": time.time()}
        gl.save_state(_mutate=lambda st: st.__setitem__(_RECORDS, {**dict(st.get(_RECORDS) or {}), key: rec}))
        return UnitApplyResult(ApplyOutcome.APPLIED, (MemberApply(rec["id"], rec),))


def _accept_fire(*_a: Any) -> dict[str, Any]:
    raise EditError("needs_broker", 403, f"{ACCEPT_VERB!r} is T3: it fires only through the broker, on a "
                                         "laptop-presence signature")


def team_verbs(start: Path | str) -> tuple[dict[str, VerbSpec], dict[str, DownstreamEffect]]:
    """The two downstream specs, and the accept's broker effect, over the team ledger found from ``start``."""
    start = Path(start)
    fetch = VerbSpec(FETCH_VERB, "fetch now", "fetch the team ledger from its remote now and rebase onto it "
                     "(never a push)", "panel", fields=(), floor="C1", tier_fn=_const("C1"), idempotent=True,
                     fire=_fetch_fire(start))
    accept = VerbSpec(ACCEPT_VERB, "accept", "bring this machine's proposed key into force for a member, or take "
                      "the ownership offered to this machine's member", "panel", fields=FIELDS, floor="T3",
                      tier_fn=_const("T3"), reversible=False, fire=_accept_fire)
    return ({FETCH_VERB: fetch, ACCEPT_VERB: accept},
            {ACCEPT_VERB: DownstreamEffect(TeamJoinStore(start), join_effect)})
