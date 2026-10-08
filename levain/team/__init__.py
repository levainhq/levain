"""Team context: one project's decisions, shared across a team of engineers through git.

git is the wire, not the memory: an orphan ``levain-team-ledger`` branch carries append-only, hash-chained
entries (one file per author per clone), ``team.toml`` roles, and the owner's generated ``PROJECT.md``.
A Claude Code PreToolUse hook puts a recorded decision in front of the agent at the edit it governs.
Stdlib only; POSIX (fcntl).
"""
