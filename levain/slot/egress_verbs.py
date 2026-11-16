"""Root's host-grant verbs (board levain#66 slice S2, plan hgs2_plan_v2): ``egress-grant``, ``egress-grant-revoke``,
``egress-refusals``, ``egress-grants-list``. The handlers are thin: they bind the caller to the session, then compose
:mod:`levain.slot.egress_verify` (the signature and host policy), :mod:`levain.slot.egress_store` (the grants file in
the root-owned ``R/grants/slot<n>/``) and :mod:`levain.slot.egress_ledger` (single use, revocation). Imported lazily by
:mod:`levain.slot.runtime_helper`'s wrappers, as ``start`` is.

``egress-grant`` (plan section 2), in this order: bind the caller; parse the signed bytes; freshness; the chosen expiry
is after root's ``now``; the message's session, slot and store are this session's, this slot's and root's; the host is
not already routed; the signature under the install-wide verify lock (taken and RELEASED before any other lock); then
under the session lock: the session is ACTIVE, the slot dir passes its walk, the old file is read (a bad one is an
empty rebuild), expired entries pruned, one entry per host with the LATEST ISSUED winning, the cap checked; the new
file is staged in a unique temp, the ledger row is committed (the commit point of single use) under the global
ledger lock, and only then is the temp renamed to ``grants.json``.

Failure semantics: before the ledger commit, any failure unlinks only the temp, the old file stays in force and the tap
is NOT burned (``egress-grant-write-failed``); after it and before the rename, the same but the tap IS burned (a new
card); a failure to sync the directory after the rename is ``egress-grant-unsure`` (the new file is in force and may not
survive a power loss; the tap is burned). "Append before rename" orders a PROCESS crash only; power-loss ordering rests
on the full sync of the ledger and its directory completing before the rename is issued.

Root's ``/usr/bin/python3`` (3.9) runs this: no match statement, no ``X | Y`` outside annotations.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Optional

from levain.contracts import ContractError
from levain.slot import Refused
from levain.slot import egress_grants as eg
from levain.slot import egress_ledger as EL
from levain.slot import egress_store as ES
from levain.slot import egress_verify as EV
from levain.slot import records as R
from levain.slot import runtime_helper as H
from levain.slot.runtime_grammar import OPERATOR, EgressGrantInput, RuntimeRequest

ERR_CLASS = "egress-grant-class"
ERR_SESSION = "egress-grant-session"
ERR_STORE = "egress-grant-store"
ERR_EXPIRED = "egress-grant-expired"
ERR_NOT_ACTIVE = "egress-grant-not-active"
ERR_SUPERSEDED = "egress-grant-superseded"
ERR_WRITE = "egress-grant-write-failed"
ERR_UNSURE = "egress-grant-unsure"
ERR_TOO_MANY = "too-many"


def bind(ctx: H.Ctx, session: str) -> R.Alloc:
    """The alloc of ``session`` for an OPERATOR caller that owns it, on an egress slot; ``no-session`` for every other
    pairing (a caller learns nothing of another's sessions: not a dispatch caller, not another operator's session)."""
    data = ctx.store.read(("alloc",), f"{session}.json")
    if data is None:
        raise Refused("no-session", session)
    try:
        a = R.parse_alloc(data)
    except (Refused, ContractError, ValueError):
        raise Refused("no-session", session) from None
    if a.session != session or ctx.caller != OPERATOR or a.operator_uid != ctx.sudo_uid:
        raise Refused("no-session", session)
    if a.slot.slot_class != "egress":
        raise Refused(ERR_CLASS, session)
    return a


def _now(ctx: H.Ctx) -> datetime:
    """Root's clock as an aware UTC datetime (never the message's)."""
    return datetime.fromtimestamp(float(ctx.seams.clock()), tz=timezone.utc)


def _active(ctx: H.Ctx, session: str) -> bool:
    """CONTROL's state record, read as a hint (the caller holds the session lock END also takes): ACTIVE and no other
    state admits a grant (a BRAKED session is paused: no tap lands while it is)."""
    data = ctx.store.read(("sessions",), f"{session}.json", trusted=False)
    if data is None:
        return False
    try:
        return R.parse_state(data).state == R.ACTIVE
    except (Refused, ContractError, ValueError):
        return False


def _entry_dict(e: eg.Entry) -> Dict[str, object]:
    return {"grant": e.grant, "host": e.host, "expires": eg.stamp(e.expires), "choice": e.choice}


def egress_grant(ctx: H.Ctx, req: RuntimeRequest, blob: object) -> Dict[str, object]:
    if not isinstance(blob, EgressGrantInput):
        raise Refused("missing-stdin", "egress-grant")
    session = str(req.session)
    a = bind(ctx, session)
    now = _now(ctx)
    # A. before any lock, no write
    try:
        signed = eg.signed_bytes(blob.message, blob.choice)
        grant = eg.parse_signed(signed, writable_at_root=True)
        eg.check_fresh(grant, now)
    except eg.GrantRefused as e:
        raise EV.refusal_of(e) from None
    if grant.expires <= now:
        raise Refused(ERR_EXPIRED, "the chosen grant is already over")
    if grant.session != session or grant.slot != a.slot.index:
        raise Refused(ERR_SESSION, "the message is for another session or slot")
    with EL.ledger_dir(ctx) as dfd:
        if grant.store != EL.read_store_id(ctx, dfd):
            raise Refused(ERR_STORE, "the message names another store")
        EV.host_class(ctx, grant.host)
        with EL.verify_lock(ctx, dfd):
            EV.check_signature(ctx, signed, blob.sig, blob.signer_fp)
        # B. under the session lock
        with H.session_lock(ctx, session):
            if not _active(ctx, session):
                raise Refused(ERR_NOT_ACTIVE, "the session is not ACTIVE")
            with ES.open_slot_dir(ctx, a.slot) as sd:
                _publish(ctx, a, session, grant, sd, dfd, now)
                ES.sweep_temps(sd)
    return {"grant": grant.grant, "host": grant.host, "expires": eg.stamp(grant.expires), "choice": grant.choice}


def _publish(ctx: H.Ctx, a: R.Alloc, session: str, grant: eg.HostGrant, sd: ES.SlotDir, dfd: int,
             now: datetime) -> None:
    at = ctx.seams.at
    live = [e for e in ES.read_current(ctx, a.slot, session, sd) if e.expires > now]
    used = EL.read_used(ctx, dfd)
    # An id already used or revoked is named so before the replace-by-host rule can call it superseded; the commit under
    # the ledger lock (EL.record_used) is the authoritative check, this one only chooses the clearer refusal.
    if EL.used_for(used, grant.grant) is not None:
        raise Refused("egress-grant-used", "that grant was already published")
    if any(r.grant == grant.grant and r.keep_until > now for r in EL.read_revoked(ctx, dfd)):
        raise Refused("egress-grant-revoked", "that grant was revoked before it arrived")
    same = [e for e in live if e.host == grant.host]
    if same:
        row = EL.used_for(used, same[0].grant)
        if row is None:
            raise Refused(EL.ERR_LEDGER, "a live entry has no used row")
        if grant.issued <= row.issued:
            raise Refused(ERR_SUPERSEDED, "a grant issued later already holds that host")
        live.remove(same[0])
    live.append(eg.entry_from_grant(grant))
    if len(live) > eg.GRANTS_MAX:
        raise Refused(ERR_TOO_MANY, "a session holds no more host grants than this")
    try:
        raw = eg.dump_grants(session, a.slot.index, live)
    except eg.GrantRefused:
        raise Refused(ERR_WRITE, "the grants file cannot be built") from None
    tmp = None  # type: Optional[str]
    try:
        tmp = ES.stage(ctx, sd, raw, at)
        at("egress-grant:ledger")
        with EL.ledger_lock(ctx, dfd):
            EL.record_used(ctx, dfd, grant, now)
        at("egress-grant:committed")
        ES.commit(ctx, sd, tmp, at)
    except ES.RenamedNotDurable:
        raise Refused(ERR_UNSURE, "the new file is in force and may not survive a power loss") from None
    except Refused:
        if tmp is not None:
            ES.discard(sd, tmp)
        raise
    except OSError as e:
        if tmp is not None:
            ES.discard(sd, tmp)
        raise Refused(ERR_WRITE, "the grant could not be written; the old file is in force (%s)"
                      % (e.strerror or type(e).__name__)) from None
    except BaseException:
        if tmp is not None:
            ES.discard(sd, tmp)
        raise


def _removal(ctx: H.Ctx, a: R.Alloc, session: str, target: str, sd: Optional[ES.SlotDir]):
    """``(remaining entries, removed ids, unknown ids)`` for revoking ``target`` (``all`` or one id) from the file."""
    current = ES.read_current(ctx, a.slot, session, sd) if sd is not None else []
    if target == "all":
        return [], [e.grant for e in current], []
    if any(e.grant == target for e in current):
        return [e for e in current if e.grant != target], [target], []
    return current, [], [target]


def egress_grant_revoke(ctx: H.Ctx, req: RuntimeRequest, blob: object) -> Dict[str, object]:
    session, target = str(req.session), str(req.target)
    a = bind(ctx, session)
    now = _now(ctx)
    with EL.ledger_dir(ctx) as dfd:
        with H.session_lock(ctx, session):
            try:
                sd = ES.open_slot_dir(ctx, a.slot)    # type: Optional[ES.SlotDir]
            except Refused as e:
                if e.name != ES.NOT_READY:
                    raise
                sd = None                             # nothing was ever published here; an unknown id is still recorded
            try:
                remaining, removed, unknown = _removal(ctx, a, session, target, sd)
                if removed and sd is not None:
                    try:
                        raw = eg.dump_grants(session, a.slot.index, remaining)
                        ES.publish(ctx, sd, raw, ctx.seams.at)
                    except ES.RenamedNotDurable:
                        raise Refused(ERR_UNSURE, "the narrowed file is in force and may not survive a power loss") \
                            from None
                    except (eg.GrantRefused, OSError):
                        raise Refused(ERR_WRITE, "the narrowed file could not be written; the old one is in force") \
                            from None
                if unknown:
                    with EL.ledger_lock(ctx, dfd):
                        try:
                            EL.record_revoked(ctx, dfd, unknown, now)
                        except EL.LedgerWriteFailed:
                            raise Refused(ERR_WRITE, "the revocation could not be recorded") from None
            finally:
                if sd is not None:
                    sd.close()
    return {"removed": removed, "recorded": unknown}


def egress_refusals(ctx: H.Ctx, req: RuntimeRequest, blob: object) -> Dict[str, object]:
    bind(ctx, str(req.session))
    raise Refused("verb-unbuilt:egress-refusals", "built in a later step")


def egress_grants_list(ctx: H.Ctx, req: RuntimeRequest, blob: object) -> Dict[str, object]:
    """``{store, entries}``: root's grant-store id (what a card's ``store`` must be) and the session's live entries."""
    session = str(req.session)
    a = bind(ctx, session)
    now = _now(ctx)
    with EL.ledger_dir(ctx) as dfd:
        store_id = EL.read_store_id(ctx, dfd)
    try:
        sd = ES.open_slot_dir(ctx, a.slot)   # type: Optional[ES.SlotDir]
    except Refused as e:
        if e.name != ES.NOT_READY:
            raise
        sd = None
    try:
        live = [e for e in (ES.read_current(ctx, a.slot, session, sd) if sd is not None else []) if e.expires > now]
    finally:
        if sd is not None:
            sd.close()
    return {"store": store_id, "entries": [_entry_dict(e) for e in live]}


__all__ = ["bind", "egress_grant", "egress_grant_revoke", "egress_grants_list", "egress_refusals"]
