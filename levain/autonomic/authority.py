"""levain.autonomic.authority — the authorization in force at the gate (the receipt's authority_scope).

A binding is a delegated-authority grant ("I, Phill, authorize flow to do G when P, at posture R" —
agent_authority_model.md / the binding-language doc). ``AuthorityScope`` is the concrete record of
WHICH grant gated a given fire — the receipt's ``authority_scope`` field (the FROZEN
DecisionInfluenceReceipt). For Slice 1 (binding-LESS, manual) the grant is the human's own
invocation; when the binding layer lands (Slice 3) ``binding_id`` names the standing grant. Frozen;
serializes to the contract's ``authority_scope`` slice via :meth:`to_dict`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["AuthorityScope", "manual_invocation"]


@dataclass(frozen=True)
class AuthorityScope:
    """The write/action authorization that gated this fire. ``grantor`` = who authorized
    (``"human"`` for a manual invocation / an in-session confirm; ``"binding"`` for a standing
    grant); ``grant`` = a short descriptor of the authorization; ``binding_id`` names the standing
    grant when one applies (``None`` for manual); ``hops`` = the agentic distance from the human
    grant (carried so the receipt records the grounding distance the posture was resolved at)."""

    grantor: str
    grant: str
    binding_id: str | None = None
    hops: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "grantor": self.grantor,
            "grant": self.grant,
            "binding_id": self.binding_id,
            "hops": self.hops,
        }


def manual_invocation() -> AuthorityScope:
    """The Slice-1 authority: the human is invoking the gate directly (the human IS the grant). The
    binding-less default until the Slice-3 binding compiler produces standing grants."""
    return AuthorityScope(grantor="human", grant="manual-invocation", binding_id=None, hops=0)
