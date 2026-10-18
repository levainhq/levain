"""Authority stamps: pure readers of the agents' own records (Q15, DESIGN section 3.5). No writes, no network.

A ledger row says a tool call happened; its STAMP says on whose authority. Neither agent's hook payload carries the
decision, so it is read back from the agent's own record of the call:

* Claude Code: the transcript (``transcript_path`` in every hook payload). The user entry holding the call's
  ``tool_result`` carries a top-level ``permissionDecision`` ``{decision, source, reasonType}``. It is NOT on disk at
  the same call's PostToolUse; it is by the next PreToolUse (measured, runs3-cc RUN 3). A missing file is UNKNOWN,
  never allow or deny (a claude started from inside a claude session with ``CLAUDE_CODE_CHILD_SESSION=1`` writes no
  transcript, runs3-tx).
* Codex: the rollout (``transcript_path`` in the hook payload IS the rollout, with a ``turn_id``). Policy is per turn,
  in the rollout's ``turn_context``; the payload's ``permission_mode`` is NOT the sandbox (it read ``bypassPermissions``
  under a read-only sandbox, 10-18 measurement), so it is never used for Codex.

Both readers stream the file line by line, stop at the match, tolerate a half-written last line, and cap the bytes
they read. The mappings to a stamp are the head's (RUN.md); a case they do not cover maps to ``unknown`` rather than
to an authority nobody exercised.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

__all__ = ["CLAUDE_STATES", "STAMPS", "claude_decision", "claude_result_state", "claude_stamp", "codex_stamp",
           "codex_turn_policy"]

# who authorised a call. ``pending`` is a ledger row's state before its source was readable, never a final stamp.
STAMPS: tuple[str, ...] = ("grant", "answer", "ruling", "own_rules", "classifier", "bypass", "dialog_unsigned",
                           "hook", "sandbox", "unknown", "pending")
CLAUDE_STATES: tuple[str, ...] = ("missing_file", "no_result", "no_decision", "decided")
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
_BYPASS_MODE = "bypassPermissions"
_USER_SOURCES = frozenset({"user_permanent", "user_temporary"})


def _lines(path: str | os.PathLike[str], max_bytes: int):
    """Yield a JSONL file's raw lines (the callers parse the few they need, and skip one that does not parse: a
    partly written last line, junk). Stops after ``max_bytes`` bytes."""
    read = 0
    with open(path, "rb") as fh:
        for raw in fh:
            read += len(raw)
            if read > max_bytes:
                return
            yield raw


def claude_result_state(transcript_path: str | os.PathLike[str] | None, tool_use_id: str | None, *,
                        max_bytes: int = DEFAULT_MAX_BYTES) -> tuple[str, Mapping[str, Any] | None]:
    """``(state, decision)`` for the call ``tool_use_id`` in a Claude Code transcript.

    ``missing_file`` (no path, no such file, unreadable): the source will not be there. ``no_result``: the file has no
    ``tool_result`` for the call yet (not written, or an interrupted call). ``no_decision``: the result is there with
    no ``permissionDecision``. ``decided``: the decision mapping is returned."""
    if not transcript_path or not tool_use_id:
        return "missing_file", None
    needle = tool_use_id.encode("utf-8")
    try:
        for raw in _lines(transcript_path, max_bytes):
            if needle not in raw:
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue                                       # a partly written line: not there yet
            if not isinstance(entry, dict) or entry.get("type") != "user":
                continue
            message = entry.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result" \
                        and block.get("tool_use_id") == tool_use_id:
                    decision = entry.get("permissionDecision")
                    if isinstance(decision, dict) and decision:
                        return "decided", dict(decision)
                    return "no_decision", None
    except OSError:
        return "missing_file", None
    return "no_result", None


def claude_decision(transcript_path: str | os.PathLike[str] | None, tool_use_id: str | None, *,
                    max_bytes: int = DEFAULT_MAX_BYTES) -> Mapping[str, Any] | None:
    """The ``permissionDecision`` of the user entry whose ``tool_result`` answers ``tool_use_id`` (keys as the agent
    spells them: ``decision``, ``source``, ``reasonType``), or None when the file or the entry is missing. None is
    UNKNOWN, never allow or deny."""
    return claude_result_state(transcript_path, tool_use_id, max_bytes=max_bytes)[1]


def claude_stamp(decision: Mapping[str, Any] | None, permission_mode: str | None) -> str:
    """The authority stamp of a Claude Code decision (RUN.md's mapping). A refusal keeps its call row and its stamp
    names who refused; the caller records the whole decision (``decision: reject``) as the row's stamp_detail."""
    if not isinstance(decision, Mapping) or not decision:
        return "unknown"
    source, reason = decision.get("source"), decision.get("reasonType")
    if source in _USER_SOURCES or reason in _USER_SOURCES:
        return "dialog_unsigned"
    if source == "hook" or reason == "hook":
        return "hook"
    if reason == "classifier":
        return "classifier"
    if reason in (None, "mode") and decision.get("decision") == "accept":
        if permission_mode == _BYPASS_MODE:
            return "bypass"
        return "own_rules" if source == "config" else "unknown"
    if reason == "mode":
        return "bypass" if permission_mode == _BYPASS_MODE else "unknown"
    if source == "config" or reason in ("rule", "config", "subcommandResults"):
        return "own_rules"
    return "unknown"


def codex_turn_policy(rollout_path: str | os.PathLike[str] | None, turn_id: str | None = None, *,
                      max_bytes: int = DEFAULT_MAX_BYTES) -> Mapping[str, Any] | None:
    """The policy of a Codex turn from the rollout's ``turn_context``: ``approval_policy``, ``sandbox_policy``,
    ``permission_profile``, ``approvals_reviewer``. ``turn_id`` None takes the last turn recorded. None when the
    file or the turn is missing."""
    if not rollout_path:
        return None
    found: Mapping[str, Any] | None = None
    try:
        for raw in _lines(rollout_path, max_bytes):
            if b'"turn_context"' not in raw:
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            payload = entry.get("payload") if isinstance(entry, dict) else None
            if not isinstance(payload, dict) or entry.get("type") != "turn_context":
                continue
            if turn_id is not None and payload.get("turn_id") != turn_id:
                continue
            found = {k: payload.get(k) for k in ("approval_policy", "sandbox_policy", "permission_profile",
                                                 "approvals_reviewer")}
            if turn_id is not None:
                break
    except OSError:
        return None
    return found


def codex_stamp(policy: Mapping[str, Any] | None) -> str:
    """The authority stamp of a Codex turn's policy. Covered: ``never`` + a read-only, workspace-write or
    external sandbox -> sandbox (the sandbox is the only authority; it cannot ask); ``never`` + danger-full-access ->
    bypass; a string approval policy that can ask (on-request, untrusted, on-failure) -> own_rules. NOT covered, so
    unknown: a granular (mapping) approval policy; an unrecognised sandbox type; a reviewer other than the user
    (Codex's auto-reviewer is a classifier-like authority nobody has measured here)."""
    if not isinstance(policy, Mapping):
        return "unknown"
    approval = policy.get("approval_policy")
    sandbox = policy.get("sandbox_policy")
    kind = sandbox.get("type") if isinstance(sandbox, Mapping) else sandbox
    reviewer = policy.get("approvals_reviewer")
    if not isinstance(approval, str) or not isinstance(kind, str):
        return "unknown"
    if reviewer not in (None, "user"):
        return "unknown"
    if approval == "never":
        if kind in ("read-only", "workspace-write", "external-sandbox"):
            return "sandbox"
        if kind == "danger-full-access":
            return "bypass"
        return "unknown"
    if approval in ("on-request", "untrusted", "on-failure"):
        return "own_rules"
    return "unknown"
