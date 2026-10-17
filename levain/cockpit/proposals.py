"""levain.cockpit.proposals — standing-rule proposals for the Decisions view (STATUS_1_0 rows 13 and 17).

When the operator has answered the same request by hand, ``allow``, again and again (same agent, same tool, same
working directory), the page offers the one command that would make that a standing grant. The page never signs:
a proposal is text, the ``levain consent grant create`` line the operator runs in a terminal with their own signing
key. Nothing here reads the authority log or signs anything; the evidence is the consent queue's decided requests
over the last :data:`levain.consent.listing.HISTORY`, read-only.

What is never proposed is whatever the log would refuse at append, taken from the log's own constants and checks
(:mod:`levain.authority`), never listed again here: a shell tool, an MCP tool off the read-only list, a request at
the agent-config tier, a workspace of ``/`` or the home directory.

Stdlib only (plus the record and listing helpers).
"""
from __future__ import annotations

import shlex
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from levain.authority import (CONSENT_AGENTS, CONSENT_POSTURES, CONSENT_SHELL_TOOLS, _CONSENT_TOOL,
                              consent_mcp_tool_problem, consent_root_problem, consent_workspace_problem)
from levain.consent.listing import ConsentEntry, ConsentListing
from levain.consent.policy import AGENT_CONFIG_TIER

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
PROPOSAL_CAP = 10


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


def _proposable(agent: str, tool: str, workspace: str) -> bool:
    """Whether the log would take a grant naming this agent, tool and workspace (it is the log's judgement, asked
    through its own checks): never a shell tool, never an MCP tool off the read-only list."""
    if agent not in CONSENT_AGENTS or PROPOSED_POSTURE not in CONSENT_POSTURES[agent]:
        return False
    if tool.lower() in CONSENT_SHELL_TOOLS or _CONSENT_TOOL.fullmatch(tool) is None:
        return False
    if consent_mcp_tool_problem(tool) is not None:
        return False
    if tool.startswith("mcp__"):     # mcp__<server>__<tool>, both parts named: the log's read-only check lets "mcp____x" by
        server, sep, name = tool[len("mcp__"):].partition("__")
        if not (server and sep and name):
            return False
    return consent_workspace_problem(workspace) is None and consent_root_problem(workspace) is None


def _duration(life: timedelta) -> str:
    days, rest = divmod(int(life.total_seconds()), 86400)
    return f"{days}d" if days and not rest else f"{int(life.total_seconds() // 3600)}h"


def propose(history: ConsentListing) -> list[Proposal]:
    """The proposals ``history`` (a :func:`~levain.consent.listing.read_history`) supports, most hand answers first,
    then most recent, at most :data:`PROPOSAL_CAP`. A key (agent, tool, workspace) is proposed when it has
    :data:`MIN_HAND_ALLOWS` hand-answered allows below the agent-config tier, no denial in the window, and its latest
    decided request was not answered by a standing grant (one is already doing the work)."""
    by_key: dict[tuple[str, str, str], list[ConsentEntry]] = defaultdict(list)
    for e in history.recent:
        if e.state in ("allowed", "denied"):
            by_key[(e.request.agent, e.request.tool_name, e.request.cwd)].append(e)
    out: list[Proposal] = []
    for (agent, tool, cwd), entries in by_key.items():
        if not _proposable(agent, tool, cwd):
            continue
        entries.sort(key=lambda e: (e.decided_at or "", e.request.id))    # a tie in the stamp: the later id
        if any(e.state == "denied" for e in entries) or entries[-1].via == "grant":
            continue
        hand = [e for e in entries if e.state == "allowed" and e.via == "signature" and e.request.tier != AGENT_CONFIG_TIER]
        if len(hand) < MIN_HAND_ALLOWS:
            continue
        expires = _duration(PROPOSED_LIFE)
        command = " ".join(["levain consent grant create", "--agent", agent, "--tool", shlex.quote(tool),
                            "--workspace", shlex.quote(cwd), "--posture", PROPOSED_POSTURE,
                            "--expires", expires, "--signing-key <key>"])
        out.append(Proposal(agent, tool, cwd, len(hand), hand[-1].decided_at or "", expires, PROPOSED_POSTURE, command,
                            f"covers {tool} prompts under the {PROPOSED_POSTURE} permission mode inside {cwd}; "
                            "other modes need their own --posture"))
    out.sort(key=lambda p: (p.count, p.last_at), reverse=True)
    return out[:PROPOSAL_CAP]
