"""levain.consent.policy — the one seam every agent adapter asks for a policy answer.

An adapter files a request, then asks :func:`guarded_policy`. A policy answer is never the hook's own say-so: it is
a decision the queue records against a signed standing grant it re-verifies in a fresh replay of the authority log
(Q4). Until Q4 lands, :func:`decide` decides nothing and every request goes to a signed answer or to the human.

A request filed at :data:`AGENT_CONFIG_TIER` (an edit of agent config or hooks) never reaches a policy (ruling 3b
item 1): :func:`guarded_policy` raises :class:`PolicyNotEligible` instead of asking.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from levain.consent.queue import ConsentQueue, Decision
    from levain.consent.record import ExternalGateRequest

__all__ = ["AGENT_CONFIG_TIER", "Policy", "PolicyNotEligible", "decide", "guarded_policy"]

# The tier an agent_config request is filed at: an allow then needs a key class covering T3.
AGENT_CONFIG_TIER = "T3"

Policy = Callable[..., "Decision | None"]


class PolicyNotEligible(AssertionError):
    """The policy seam was reached for a request that may never have a policy answer (ruling 3b item 1)."""


def decide(request: ExternalGateRequest, queue: ConsentQueue, *, posture: str | None = None) -> Decision | None:
    """The policy answer for ``request`` on ``queue``, or ``None``. ``posture`` is the agent session's own
    permission posture as its event reports it (Claude Code's ``permission_mode``), ``None`` when absent."""
    return None


def guarded_policy(request: ExternalGateRequest, queue: ConsentQueue, *, posture: str | None = None,
                   policy: Policy | None = None) -> Decision | None:
    """``policy`` (default :func:`decide`), called as ``policy(request, queue, posture=posture)``, with ruling 3b
    item 1 asserted first: a request filed at :data:`AGENT_CONFIG_TIER` never reaches it."""
    if request.tier == AGENT_CONFIG_TIER:
        raise PolicyNotEligible(f"request {request.id} targets agent config: never eligible for a policy answer")
    return (policy or decide)(request, queue, posture=posture)
