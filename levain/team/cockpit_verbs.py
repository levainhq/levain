"""levain.team.cockpit_verbs: the Team zone's two verbs (TEAM_VIEW_PLAN T3; head design DESIGN_T3_VERBS.md).

``team_fetch`` (C1, offered by Sync while the ledger has a remote) does now what the team hook does on its own every
interval: one fetch and rebase, never a push, on bounded waits (a person's request is open). It exercises no
authority the hook does not, so it is C1 in every install; a standing policy may still raise it.

``team_join_accept`` (T3, offered by Joins pending while a join waits on THIS machine) brings this machine's proposed
key into force for a handle, or takes the ownership offered to this machine's member: the team's trust-root acts. It
is T3 because the cockpit server holds the member's signing key outside any agent's sandbox: at T2 a holder of the
surface token could make the server accept ownership as the operator (a confused deputy). A T3 fires only on a
``laptop-presence`` signature over the broker's stored record, which the token cannot make.

What the operator signs is ``(kind, handle, about)``: ``about`` is the Joins row's digest of exactly what is accepted
(``levain.team.reader.join_digest``: the pending key LINE, or the current owner and the offer with its email). The
fingerprint is never a param: the store reads this machine's own, so a caller cannot name another machine's key. At
apply the store decides again, under the team op lock, after a fetch, from ONE derivation: the act is done
(``GitLedger.confirm_own_key`` / ``GitLedger.accept_offer``, the same acts the laptop commands run), or refused naming
what moved since the signature (a digest that no longer matches: "what was signed changed"; an act already in force
that this signature did not make: "changed nothing"). An ``acting`` marker saved under the op before the act is what
proves an act found in force is this pending's own (a crash, a failed push or a failed final record re-drives into it).
Only a PUBLISHED act (or one on a clone with no remote) is recorded APPLIED: a push that fails, a busy lock or any
other fault of the environment during the act is transient and the broker re-drives the pending. Its records live
in the clone's own state file, so an ``ALREADY`` survives a restart.

``team_verbs(start)`` returns the two specs and the accept's broker effect for ``make_server`` to merge. Nothing here
reads the ledger at construction: ``start`` is resolved on each fire and each apply.
"""
from __future__ import annotations

import re
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
from .reader import join_digest, person_text
from .roles import handle_problem
from .transport import Repo, TeamBusy, TeamError

__all__ = ["team_verbs", "TeamJoinStore", "join_effect", "STORE", "KINDS", "TEAM_VERB_NAMES"]

TEAM_VERB_NAMES = (FETCH_VERB, ACCEPT_VERB)

STORE = "team_join"
KINDS = ("key", "owner-offer")
FIELDS = ("kind", "handle", "about")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_PREFIX = "team-join:"
_RECORDS = "cockpit_joins"      # the clone state file's key for this store's records: origin_key -> record
_ACTING = "acting"              # a record's ``state`` while its act is begun and not yet recorded (a marker, not a record)
_BUSY = "another levain team operation on this clone is running"
_NO_REMOTE = "this team ledger has no remote to fetch from; nothing was fetched"
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
            if not gl.remote:
                raise EditError("refused", 409, _NO_REMOTE)
            summary = gl.sync_now(op_wait=OP_WAIT_S, timeout=NET_TIMEOUT_S)
            if not gl.remote:          # the remote went between the check and the op: nothing was fetched
                raise EditError("refused", 409, _NO_REMOTE)
            return {"summary": summary}
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
    (the broker answers it at issue): a kind outside ``KINDS``, a handle the team's own handle rule refuses, an
    ``about`` that is not a 64-hex sha256 digest (missing included), or any param but ``kind``, ``handle`` and
    ``about``. All three are the effect's changes, projection and writes, so the signature over the record binds them."""
    extra = sorted(set(params) - set(FIELDS))
    if extra:
        raise ValueError(f"{ACCEPT_VERB} takes only {list(FIELDS)}, not {extra}")
    kind, handle = params.get("kind"), params.get("handle")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {list(KINDS)}, not {kind!r}")
    problem = handle_problem(handle)
    if problem:
        raise ValueError(problem)
    about = params.get("about")
    if not isinstance(about, str) or not _DIGEST.fullmatch(about):
        raise ValueError("about must be the join row's digest (64 lowercase hex), as the Joins pane shows it")
    changes = {"kind": kind, "handle": handle, "about": about}
    return Effect(_PREFIX + origin_key, STORE, {"absent": True}, changes, dict(changes), FIELDS, ())


def _done(d, kind: str, handle: str, fp: str) -> bool:
    """The act this join names is in force: this machine's key for ``handle`` (key), or ``handle`` the owner with this
    machine's key in force (owner-offer). The ONE done-definition per kind. It never proves that THIS pending acted:
    only an ``acting`` marker does (``TeamJoinStore._apply_in_op``). No offer clause: once ``handle`` owns the team,
    an offer the new owner then makes to someone else does not undo the accept (T3 L3 r1 glm HIGH)."""
    if kind == "key":
        return fp in T.key_fps(d.tenure, handle)
    return d.team.owner == handle and fp in T.key_fps(d.tenure, handle)


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
        self.start = Path(start).expanduser().resolve(strict=False)   # one absolute meaning, whatever the cwd later is

    @staticmethod
    def _key(resource_key: str) -> str:
        if not resource_key.startswith(_PREFIX) or len(resource_key) == len(_PREFIX):
            raise PermanentApplyRefusal(f"{resource_key!r} is not a team-join resource key")
        return resource_key[len(_PREFIX):]

    @staticmethod
    def _records(gl: LedgerTransport) -> dict[str, dict[str, Any]]:
        recs = gl.state().get(_RECORDS)
        return dict(recs) if isinstance(recs, dict) else {}

    @staticmethod
    def _final(rec: Any, key: str | None = None) -> bool:
        """A record of an applied accept: exactly the shape :meth:`_apply_in_op` writes last, for ``key`` when given
        (an ``acting`` marker, or a malformed entry, is not one, so it never stands in for the act)."""
        return (isinstance(rec, dict) and set(rec) == {"id", "origin_key", "kind", "handle", "about", "sha", "at"}
                and isinstance(rec["id"], str) and rec["id"] == rec["sha"] and rec["kind"] in KINDS
                and isinstance(rec["handle"], str) and isinstance(rec["about"], str)
                and isinstance(rec["at"], (int, float)) and not isinstance(rec["at"], bool)
                and (key is None or rec["origin_key"] == key))

    @staticmethod
    def _save(gl: LedgerTransport, key: str, rec: dict[str, Any]) -> None:
        gl.save_state(_mutate=lambda st: st.__setitem__(_RECORDS, {**dict(st.get(_RECORDS) or {}), key: rec}))

    def snapshot(self, resource_key: str) -> Snapshot:
        key = self._key(resource_key)
        gl = _ledger(self.start)
        rec = None if gl is None else self._records(gl).get(key)
        return Snapshot(dict(rec), str(rec.get("sha") or "")) if self._final(rec, key) else Snapshot(None, None)

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
        kind, handle, about = (effect.changes.get(f) for f in FIELDS)
        if kind not in KINDS or handle_problem(handle) or not isinstance(about, str) or not _DIGEST.fullmatch(about):
            raise PermanentApplyRefusal(f"not a team join: {kind!r} {handle!r}")
        gl = _ledger(self.start)
        if gl is None:
            raise PermanentApplyRefusal("this install has no team ledger")
        try:
            with gl.op(timeout=OP_WAIT_S):
                return self._apply_in_op(gl, key, str(kind), str(handle), about)
        except TeamBusy as exc:
            raise TransientApplyFault("TeamBusy", str(exc)) from exc
        finally:
            invalidate()

    def _apply_in_op(self, gl: LedgerTransport, key: str, kind: str, handle: str, about: str) -> UnitApplyResult:
        """Under the op: ALREADY for a final record; else the team as it is now (fetched), then RECOVERY or FRESH.

        Before the act a durable ``acting`` marker for this pending is saved under the op, and after the act (and its
        publication) the final record replaces it (T3 L3 r1 head ruling). So an act found in force proves this pending
        did it only when the marker is there: RECOVERY (a crash, a failed push, a failed final save) publishes and
        records it with no other check. With no marker, or a marker whose act did not land, the apply is FRESH: an act
        already in force was not done by this signature and is refused, then what moved, then the digest, then the
        act. A fault of the environment (a fetch, a lock, a missing agent key) waits for repair: transient."""
        rec = self._records(gl).get(key)
        if self._final(rec, key):
            return UnitApplyResult(ApplyOutcome.ALREADY, (MemberApply(str(rec.get("id")), dict(rec)),))
        marker = {"state": _ACTING, "origin_key": key, "kind": kind, "handle": handle, "about": about}
        acting = isinstance(rec, dict) and all(rec.get(f) == marker[f] for f in ("state", "kind", "handle", "about"))
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
        if not (acting and _done(d, kind, handle, fp)):
            # FRESH: no marker, or a marker whose act did not land (it is made now, every check included)
            if _done(d, kind, handle, fp):
                raise PermanentApplyRefusal(f"the {kind} for {handle} is already in force; this signature changed "
                                            "nothing")
            why = _moved(d, kind, handle, fp)
            if why is not None:
                raise PermanentApplyRefusal(why)
            if join_digest(d, kind, handle, fp) != about:
                raise PermanentApplyRefusal(f"what was signed changed: the {kind} for {handle} is not the one the "
                                            "signature was given for; nothing was changed")
            try:
                self._save(gl, key, marker)   # durable before the act: a re-drive after it knows the act is its own
            except OSError as exc:
                raise TransientApplyFault("OSError", f"the accept could not be marked as begun: {exc}") from exc
            try:
                # committed here only; published below on a bounded wait
                if kind == "key":
                    if not gl.confirm_own_key(d, push=False):
                        raise PermanentApplyRefusal(f"this machine's key is no longer proposed for {handle}; nothing "
                                                    "was changed")
                else:
                    gl.accept_offer(d, push=False)
            except KeyError as exc:
                raise PermanentApplyRefusal(f"the {kind} for {handle} does not name a member this ledger knows "
                                            f"({exc}); nothing was changed") from exc
            except TeamBusy:
                raise                 # a lock held by another team operation: the pending re-drives
            except TeamError as exc:
                # an environment fault (no key in the agent, a lock timeout): what moved was decided above, so the
                # consented act waits for repair; the re-drive finds the marker and whether the act landed
                raise TransientApplyFault("TeamError", f"the {kind} for {handle} did not complete: "
                                          f"{person_text(exc)}") from exc
            # on every clone, remote or not: the act counts only if the ledger now says it is in force
            gl.forget_derivation()
            if not _done(gl.derivation(), kind, handle, fp):
                raise PermanentApplyRefusal(f"the {kind} for {handle} is not in force after the act; nothing is "
                                            "recorded")
        if gl.remote:
            # only a published act is APPLIED: a push that fails is a transient fault, the pending re-drives, and the
            # re-drive finds the marker and the act done above and pushes it here (the hook's own sync never pushes)
            try:
                # one push round: a person's request waits for one fetch, rebase and push, not the CLI's retries
                gl.sync_now(push=True, op_wait=OP_WAIT_S, timeout=NET_TIMEOUT_S, retries=1)
            except TeamBusy:
                raise
            except TeamError as exc:
                raise TransientApplyFault("TeamError", f"the {kind} for {handle} is in force on this clone but not "
                                          f"yet published: {person_text(exc)}") from exc
            gl.forget_derivation()
            if not _done(gl.derivation(), kind, handle, fp):
                # the sync re-landed this clone's change on a newer remote, and a change there undid it
                raise PermanentApplyRefusal(f"a newer team change landed first; the {kind} for {handle} is not "
                                            "in force")
        try:
            head = gl.head()
            rec = {"id": head, "origin_key": key, "kind": kind, "handle": handle, "about": about, "sha": head,
                   "at": time.time()}
            self._save(gl, key, rec)          # replaces the marker
        except (TeamError, OSError) as exc:
            # the act is in force and published; the re-drive finds the marker and records it
            raise TransientApplyFault(type(exc).__name__, f"the {kind} for {handle} is done but not yet recorded: "
                                      f"{exc}") from exc
        return UnitApplyResult(ApplyOutcome.APPLIED, (MemberApply(rec["id"], rec),))


def _accept_fire(*_a: Any) -> dict[str, Any]:
    raise EditError("needs_broker", 403, f"{ACCEPT_VERB!r} is T3: it fires only through the broker, on a "
                                         "laptop-presence signature")


def team_verbs(start: Path | str) -> tuple[dict[str, VerbSpec], dict[str, DownstreamEffect]]:
    """The two downstream specs, and the accept's broker effect, over the team ledger found from ``start``."""
    start = Path(start).expanduser().resolve(strict=False)
    fetch = VerbSpec(FETCH_VERB, "fetch now", "fetch the team ledger from its remote now and rebase onto it "
                     "(never a push)", "panel", fields=(), floor="C1", tier_fn=_const("C1"), idempotent=True,
                     fire=_fetch_fire(start))
    accept = VerbSpec(ACCEPT_VERB, "accept", "bring this machine's proposed key into force for a member, or take "
                      "the ownership offered to this machine's member", "panel", fields=FIELDS, floor="T3",
                      tier_fn=_const("T3"), reversible=False, fire=_accept_fire)
    return ({FETCH_VERB: fetch, ACCEPT_VERB: accept},
            {ACCEPT_VERB: DownstreamEffect(TeamJoinStore(start), join_effect)})
