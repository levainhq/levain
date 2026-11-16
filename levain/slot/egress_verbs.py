"""Root's host-grant verbs (board levain#66 slice S2, plan hgs2_plan_v2): ``egress-grant``, ``egress-grant-revoke``,
``egress-refusals``, ``egress-grants-list``. The handlers are thin: they bind the caller to the session, then compose
:mod:`levain.slot.egress_verify` (the signature and host policy), :mod:`levain.slot.egress_store` (the grants file in
the root-owned ``R/grants/slot<n>/``) and :mod:`levain.slot.egress_ledger` (single use, revocation). Imported lazily by
:mod:`levain.slot.runtime_helper`'s wrappers, as ``start`` is.

Root's ``/usr/bin/python3`` (3.9) runs this: no match statement, no ``X | Y`` outside annotations.
"""
from __future__ import annotations

from typing import Dict

from levain.slot import Refused
from levain.slot import records as R
from levain.slot import runtime_helper as H
from levain.slot.runtime_grammar import OPERATOR, EgressGrantInput, RuntimeRequest


def bind(ctx: H.Ctx, session: str) -> R.Alloc:
    """The alloc of ``session`` for an OPERATOR caller that owns it, on an egress slot; ``no-session`` for every other
    pairing (a caller learns nothing of another's sessions: not a dispatch caller, not another operator's session)."""
    data = ctx.store.read(("alloc",), f"{session}.json")
    if data is None:
        raise Refused("no-session", session)
    try:
        a = R.parse_alloc(data)
    except (Refused, ValueError):
        raise Refused("no-session", session) from None
    if a.session != session or ctx.caller != OPERATOR or a.operator_uid != ctx.sudo_uid:
        raise Refused("no-session", session)
    if a.slot.slot_class != "egress":
        raise Refused("egress-grant-class", session)
    return a


def egress_grant(ctx: H.Ctx, req: RuntimeRequest, blob: object) -> Dict[str, object]:
    if not isinstance(blob, EgressGrantInput):
        raise Refused("missing-stdin", "egress-grant")
    bind(ctx, str(req.session))
    raise Refused("verb-unbuilt:egress-grant", "the writer is built in a later step")


def egress_grant_revoke(ctx: H.Ctx, req: RuntimeRequest, blob: object) -> Dict[str, object]:
    bind(ctx, str(req.session))
    raise Refused("verb-unbuilt:egress-grant-revoke", "built in a later step")


def egress_refusals(ctx: H.Ctx, req: RuntimeRequest, blob: object) -> Dict[str, object]:
    bind(ctx, str(req.session))
    raise Refused("verb-unbuilt:egress-refusals", "built in a later step")


def egress_grants_list(ctx: H.Ctx, req: RuntimeRequest, blob: object) -> Dict[str, object]:
    bind(ctx, str(req.session))
    raise Refused("verb-unbuilt:egress-grants-list", "built in a later step")
