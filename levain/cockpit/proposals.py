"""levain.cockpit.proposals — standing-rule proposals for the Decisions view (STATUS_1_0 rows 13 and 17).

When the operator has answered the same request by hand, ``allow``, again and again (same agent, same tool, same
working directory), the page offers the one command that would make that a standing grant. The page never signs:
a proposal is text, the ``levain consent grant create`` line the operator runs in a terminal with their own signing
key. Nothing here reads the authority log or signs anything; the evidence is the consent queue's decided requests
over the last :data:`levain.consent.listing.HISTORY`, counted per key in the database, read-only.

What is never proposed is whatever the log would refuse at append, taken from the log's own constants and checks
(:mod:`levain.authority`), never listed again here: a tool the log's allowlist does not name (a shell tool, an MCP
tool off the read-only list), a request at the agent-config tier, a workspace that is not an existing directory, or is ``/`` or the home directory once resolved.

Stdlib only (plus the record and listing helpers).
"""
from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from levain.authority import (CONSENT_AGENTS, CONSENT_POSTURES, consent_resolved_workspace, consent_root_problem,
                              consent_tool_problem, consent_tool_shape_problem)
from levain.consent.listing import KeyEvidence

__all__ = ["MIN_HAND_ALLOWS", "PROPOSED_LIFE", "PROPOSED_POSTURE", "PROPOSAL_CAP", "Proposal", "propose"]

# [judged by the 1017+4 lane D seat, 10-17, against the queue's shape (a request is filed per prompt, a decided one is
# kept 14 days here)] Three is the smallest count that is a habit rather than a pair: two hand-allows can be one
# task's two prompts, and every extra hand-allow costs the operator a terminal answer, so a higher bar makes the card
# arrive after the nuisance has passed. One denial of the same request in the window withdraws the proposal outright.
MIN_HAND_ALLOWS = 3
# a week: long enough to span the project the habit belongs to, a quarter of the 30-day ceiling the log enforces; a
# renewal is one more command, an over-long grant is a standing hole
PROPOSED_LIFE = timedelta(days=7)
# a request carries no permission mode, and ``default`` is the one both agents report (CONSENT_POSTURES): the narrowest
# posture, so the grant answers only prompts the operator would have seen under the default mode
PROPOSED_POSTURE = "default"
# the page draws every card the backend returns, so this is the number of cards the page can show
PROPOSAL_CAP = 3


@dataclass(frozen=True)
class Proposal:
    """One card: who, what, where, how many hand answers back it, and the command."""
    agent: str
    tool: str
    workspace: str
    count: int
    last_at: str
    expires: str
    posture: str
    command: str
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {"agent": self.agent, "tool": self.tool, "workspace": self.workspace, "count": self.count,
                "last_at": self.last_at, "expires": self.expires, "posture": self.posture,
                "command": self.command, "note": self.note}


def _proposable(agent: str, tool: str, workspace: str) -> str | None:
    """The workspace the grant would store (:func:`~levain.authority.consent_resolved_workspace`: an existing
    directory, symlinks followed) when the log would take a grant naming this agent, tool and workspace, else None. It is
    the log's judgement, asked through its own checks: never a tool name of a shape the log refuses
    (:func:`~levain.authority.consent_tool_shape_problem`) or a tool :func:`~levain.authority.consent_tool_problem`
    refuses (a shell tool, a tool off the agent's allowlist, an MCP tool off the read-only list)."""
    if agent not in CONSENT_AGENTS or PROPOSED_POSTURE not in CONSENT_POSTURES[agent]:
        return None
    if consent_tool_shape_problem(tool) is not None or consent_tool_problem(agent, tool) is not None:
        return None
    if tool.startswith("mcp__"):     # mcp__<server>__<tool>, both parts named: the log's read-only check lets "mcp____x" by
        server, sep, name = tool[len("mcp__"):].partition("__")
        if not (server and sep and name):
            return None
    if not isinstance(workspace, str) or not os.path.isabs(workspace):   # a hook's cwd is absolute; nothing else is proposed
        return None
    resolved, _ = consent_resolved_workspace(workspace)
    return resolved if resolved is not None and consent_root_problem(resolved, home=os.path.expanduser("~")) is None \
        else None


def _duration(life: timedelta) -> str:
    days, rest = divmod(int(life.total_seconds()), 86400)
    return f"{days}d" if days and not rest else f"{int(life.total_seconds() // 3600)}h"


def propose(evidence: list[KeyEvidence]) -> list[Proposal]:
    """The proposals ``evidence`` (a :func:`~levain.consent.listing.read_evidence`, taken with ``min_hand`` of
    :data:`MIN_HAND_ALLOWS`) supports, most hand answers first, then most recent, at most :data:`PROPOSAL_CAP`. A key
    (agent, tool, workspace) is proposed when it has :data:`MIN_HAND_ALLOWS` hand-answered allows below the
    agent-config tier, no denial in the window, and its latest decided request was not answered by a standing grant
    (one is already doing the work)."""
    out: list[Proposal] = []
    for k in evidence:
        if k.hand_allows < MIN_HAND_ALLOWS or k.denied or k.latest_via_grant:
            continue
        ws = _proposable(k.agent, k.tool, k.cwd)
        if ws is None:
            continue
        expires = _duration(PROPOSED_LIFE)
        command = " ".join(["levain consent grant create", "--agent", k.agent, "--tool", shlex.quote(k.tool),
                            "--workspace", shlex.quote(ws), "--posture", PROPOSED_POSTURE,
                            "--expires", expires, "--signing-key <key>"])
        out.append(Proposal(k.agent, k.tool, ws, k.hand_allows, k.last_hand_at, expires, PROPOSED_POSTURE, command,
                            f"covers {k.tool} prompts under the {PROPOSED_POSTURE} permission mode inside that directory; "
                            "other modes need their own --posture"))
    out.sort(key=lambda p: (p.count, p.last_at), reverse=True)
    return out[:PROPOSAL_CAP]
