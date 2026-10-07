"""The ledger's integrity problems, as ``levain team verify`` reports them: one function, so every surface that says
"integrity problem" counts the same things (the CLI, the team view).

Four kinds: what the ledger build itself found (a broken hash chain, a line not written by the member it is filed
under), a ``team.toml`` or ``PROJECT.md`` change not made by the owner, an entry naming an id the ledger does not hold
(``supersedes`` or ``refs``), and an owner team.toml does not allow.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import index as I
    from . import roles as R
    from .transport import GitLedger


def problems(gl: "GitLedger", sha: str, team: "R.Team", ledger: "I.Ledger") -> list[str]:
    """Every integrity problem in the ledger at commit ``sha``, one line each, in a stable order."""
    out = list(ledger.problems) + gl.team_history_problems(team, sha)
    for e in ledger.entries:
        for s in e.get("supersedes", []) + e.get("refs", []):
            if s not in ledger.by_id:
                out.append(f"{e['id']}: names {s}, which is not in the ledger")
        if e.get("owner") and not team.owner_ok(e["owner"]):
            out.append(f"{e['id']}: owner {e['owner']!r} is not allowed by team.toml")
    return out
