"""levain.dispatch — a planner's task, run by cheaper executors under a contract the operator signs once.

A planner (the operator's own Claude Code or Codex session) files a TASK CONTRACT: a registered template, a goal, the exact
files the executor may read and write, a base commit, a budget and an expiry. Levain resolves it, refuses it whole when it
leaves its template or the provider's data boundary, and content-addresses it; the operator signs that digest once. The job
then moves through a durable state machine whose every transition names its cause. A result is VERIFIED and may be
RECOMMENDED by the planner, but nothing a model says authorises it: only a human signature (or a human-signed adoption rule)
adopts it. Design: the levain-memory page ``1019-dispatch/DESIGN.md``.
"""
from __future__ import annotations

__all__ = ["ContractRefused", "DispatchError"]


class DispatchError(Exception):
    """A dispatch store or contract operation that cannot be carried out."""


class ContractRefused(DispatchError):
    """A task contract refused whole: the message says why, and nothing was filed."""
