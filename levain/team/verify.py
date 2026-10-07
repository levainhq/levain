"""The ledger's integrity problems, as ``levain team verify`` reports them: one function, so every surface that says
"integrity problem" counts the same things (the CLI, the team view).

Two sources: the ledger's judgement (``GitLedger.judge``: a broken hash chain, a line not filed under its author, a
link that is not honoured, an entry naming an id the ledger does not hold, an owner team.toml does not allow), which
every reader of the ledger already carries in ``ledger.problems``; and a ``team.toml`` or ``PROJECT.md`` change not
made by the owner, which needs the branch history.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import index as I
    from . import roles as R
    from .transport import GitLedger


def problems(gl: "GitLedger", sha: str, team: "R.Team", ledger: "I.Ledger") -> list[str]:
    """Every integrity problem in the ledger at commit ``sha``, one line each, in a stable order: the judgement's own
    (``GitLedger.judge``, which every reader runs: chains, misfiled lines, links, an entry naming a missing id, an owner
    team.toml does not allow) and the team.toml/PROJECT.md history check."""
    return list(ledger.problems) + gl.team_history_problems(team, sha)
