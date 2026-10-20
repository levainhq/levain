"""Authority stamps: pure readers of the agents' own records (Q15, DESIGN section 3.5). No writes, no network.

A ledger row says a tool call happened; its STAMP says on whose authority. Neither agent's hook payload carries the
decision, so it is read back from the agent's own record of the call:

* Claude Code: the transcript (``transcript_path`` in every hook payload). The user entry holding the call's
  ``tool_result`` carries a top-level ``permissionDecision`` ``{decision, source, reasonType}``. It is NOT on disk at
  the same call's PostToolUse; it is by the next PreToolUse (measured, runs3-cc RUN 3). A missing file or result is
  UNKNOWN, never allow or deny (a claude started from inside a claude session with ``CLAUDE_CODE_CHILD_SESSION=1``
  writes no transcript, runs3-tx); the ledger waits on it until its give-up age before saying so.
* Codex: the rollout (``transcript_path`` in the hook payload IS the rollout, with a ``turn_id``). Policy is per turn,
  in the rollout's ``turn_context``; the payload's ``permission_mode`` is NOT the sandbox (it read ``bypassPermissions``
  under a read-only sandbox, 10-18 measurement), so it is never used for Codex.

Both readers stream the file line by line (a line over the per-line cap is skipped without being held, a non-UTF8 line
is skipped with its own count), stop at the match, tolerate a half-written last line, and cap the bytes they read. The mappings to a stamp are the head's (RUN.md); a case they do not cover maps to ``unknown`` rather than
to an authority nobody exercised.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["CLAUDE_STATES", "OUTCOMES", "SKIP_REASONS", "STAMPS", "ClaudeScan", "claude_decision", "claude_outcome",
           "claude_result_state", "claude_scan", "claude_stamp", "codex_stamp", "codex_turn_policies",
           "codex_turn_policy"]

# who authorised a call. ``pending`` is a ledger row's state before its source was readable, never a final stamp.
STAMPS: tuple[str, ...] = ("grant", "answer", "ruling", "own_rules", "classifier", "bypass", "dialog_unsigned",
                           "hook", "sandbox", "unknown", "pending")
# what the call's authority did: let it run, refused it, or the record does not say. A refusal never carries an
# authority stamp (it authorised nothing), so a ``refused`` row's stamp is ``unknown`` (or ``pending``).
OUTCOMES: tuple[str, ...] = ("accepted", "refused", "unknown")
CLAUDE_STATES: tuple[str, ...] = ("no_source", "no_tool_use_id", "missing_file", "unreadable", "no_result",
                                  "result_oversized", "no_decision", "decided")
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
_BYPASS_MODE = "bypassPermissions"
_USER_SOURCES = frozenset({"user_permanent", "user_temporary"})


DEFAULT_MAX_LINE_BYTES = 16 * 1024 * 1024
SKIP_REASONS = ("too_long", "not_utf8", "not_json")


def _lines(path: str | os.PathLike[str], max_bytes: int, max_line_bytes: int, skipped: dict[str, int],
           needles: Mapping[str, bytes] | None = None, oversized: set[str] | None = None):
    """Yield a JSONL file's raw lines, never holding more than ``max_line_bytes`` of one line: a longer line is
    read past in bounded chunks and skipped (counted in ``skipped['too_long']``). Stops after ``max_bytes`` bytes
    in all. With ``needles`` (name -> bytes), the names whose bytes occur inside a skipped line go into ``oversized``
    (a bounded tail is kept across chunk edges). The callers parse the few lines they need and skip one that does not parse (a half-written last line)."""
    read = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.readline(max_line_bytes + 1)
            if not chunk:
                return
            read += len(chunk)
            if read > max_bytes:
                return
            if len(chunk) > max_line_bytes and not chunk.endswith(b"\n"):
                keep = max((len(n) for n in needles.values()), default=1) - 1 if needles else 0
                window = chunk
                while True:                                    # discard the rest of this line, a chunk at a time
                    if needles and oversized is not None:
                        oversized.update(k for k, n in needles.items() if n in window)
                    rest = fh.readline(1 << 20)
                    read += len(rest)
                    window = (window[-keep:] if keep else b"") + rest
                    if needles and oversized is not None and rest:
                        oversized.update(k for k, n in needles.items() if n in window)
                    if not rest or rest.endswith(b"\n"):
                        break
                    if read > max_bytes:
                        return
                skipped["too_long"] += 1
                continue
            yield chunk


def _parse(raw: bytes, skipped: dict[str, int]) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        skipped["not_utf8"] += 1
    except ValueError:
        skipped["not_json"] += 1                              # a partly written line: not there yet
    return None


@dataclass(frozen=True, slots=True)
class ClaudeScan:
    """One pass over a transcript for several tool calls. ``states`` maps each wanted ``tool_use_id`` to
    ``(state, decision)``; ``skipped`` counts the lines not read (``too_long``, ``not_utf8``, ``not_json``)."""
    states: Mapping[str, tuple[str, Mapping[str, Any] | None]]
    skipped: Mapping[str, int]


def claude_scan(transcript_path: str | os.PathLike[str] | None, tool_use_ids: Iterable[str], *,
                max_bytes: int = DEFAULT_MAX_BYTES, max_line_bytes: int = DEFAULT_MAX_LINE_BYTES) -> ClaudeScan:
    """Find every call of ``tool_use_ids`` in a Claude Code transcript in ONE pass.

    States: ``no_source`` (no path: it will never be there); ``missing_file`` (no such file, it may appear later);
    ``unreadable`` (another OSError, may pass); ``no_result`` (no ``tool_result`` for the call yet); ``result_oversized`` (a line over the cap holds the id, so it
    could not be read); ``no_decision``
    (the result is there with no ``permissionDecision``); ``decided`` (the decision mapping is returned). A call
    that appears twice takes the copy that carries a decision. The scan stops once every call is decided."""
    wanted = {i: i.encode("utf-8") for i in tool_use_ids if i}
    skipped = dict.fromkeys(SKIP_REASONS, 0)
    states: dict[str, tuple[str, Mapping[str, Any] | None]] = {i: ("no_result", None) for i in wanted}
    if not transcript_path:
        return ClaudeScan({i: ("no_source", None) for i in wanted}, skipped)
    open_ids = set(wanted)
    oversized: set[str] = set()
    try:
        for raw in _lines(transcript_path, max_bytes, max_line_bytes, skipped, wanted, oversized):
            hits = [i for i in open_ids if wanted[i] in raw]
            if not hits:
                continue
            entry = _parse(raw, skipped)
            if not isinstance(entry, dict) or entry.get("type") != "user":
                continue
            message = entry.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                    continue
                call = block.get("tool_use_id")
                if call not in open_ids:
                    continue
                decision = entry.get("permissionDecision")
                if isinstance(decision, dict) and decision:
                    states[call] = ("decided", dict(decision))
                    open_ids.discard(call)                     # the first copy that carries a decision wins
                else:
                    states[call] = ("no_decision", None)       # keep looking for a copy that has one
            if not open_ids:
                break
    except FileNotFoundError:
        return ClaudeScan({i: ("missing_file", None) for i in wanted}, skipped)
    except OSError:
        return ClaudeScan({i: ("unreadable", None) for i in wanted}, skipped)
    for i in oversized:                                        # a skipped line held the id: not "no result", unreadable
        if states[i][0] == "no_result":
            states[i] = ("result_oversized", None)
    return ClaudeScan(states, skipped)


def claude_result_state(transcript_path: str | os.PathLike[str] | None, tool_use_id: str | None, *,
                        max_bytes: int = DEFAULT_MAX_BYTES, max_line_bytes: int = DEFAULT_MAX_LINE_BYTES
                        ) -> tuple[str, Mapping[str, Any] | None]:
    """``(state, decision)`` for one call (see :func:`claude_scan` for the states). No ``tool_use_id`` is
    ``no_tool_use_id``: the join can never be made."""
    if not tool_use_id:
        return "no_tool_use_id", None
    return claude_scan(transcript_path, [tool_use_id], max_bytes=max_bytes,
                       max_line_bytes=max_line_bytes).states[tool_use_id]


def claude_decision(transcript_path: str | os.PathLike[str] | None, tool_use_id: str | None, *,
                    max_bytes: int = DEFAULT_MAX_BYTES) -> Mapping[str, Any] | None:
    """The ``permissionDecision`` of the user entry whose ``tool_result`` answers ``tool_use_id`` (keys as the agent
    spells them: ``decision``, ``source``, ``reasonType``), or None when the file or the entry is missing. None is
    UNKNOWN, never allow or deny."""
    return claude_result_state(transcript_path, tool_use_id, max_bytes=max_bytes)[1]


def claude_outcome(decision: Mapping[str, Any] | None) -> str:
    """``accepted`` / ``refused`` / ``unknown`` from a Claude Code decision's own ``decision`` key."""
    verdict = decision.get("decision") if isinstance(decision, Mapping) else None
    return {"accept": "accepted", "reject": "refused"}.get(verdict, "unknown")  # type: ignore[arg-type]


def claude_stamp(decision: Mapping[str, Any] | None, permission_mode: str | None) -> str:
    """The authority stamp of a Claude Code decision (RUN.md's mapping). A REFUSAL authorised nothing, so it never
    gets an authority stamp: it is ``unknown`` here, and the row's outcome (:func:`claude_outcome`) and its detail
    (``source``, ``reasonType``) say who refused. In particular a ``reject`` whose ``reasonType`` is ``mode`` under a
    bypass session is not bypass authority."""
    if not isinstance(decision, Mapping) or not decision:
        return "unknown"
    if claude_outcome(decision) != "accepted":
        return "unknown"
    source, reason = decision.get("source"), decision.get("reasonType")
    if source in _USER_SOURCES or reason in _USER_SOURCES:
        return "dialog_unsigned"
    if source == "hook" or reason == "hook":
        return "hook"
    if reason == "classifier":
        return "classifier"
    if reason in (None, "mode"):
        if permission_mode == _BYPASS_MODE:
            return "bypass"
        return "own_rules" if source == "config" else "unknown"
    if source == "config" or reason in ("rule", "config", "subcommandResults"):
        return "own_rules"
    return "unknown"


def codex_turn_policies(rollout_path: str | os.PathLike[str] | None, *, max_bytes: int = DEFAULT_MAX_BYTES,
                        max_line_bytes: int = DEFAULT_MAX_LINE_BYTES) -> dict[str, Mapping[str, Any]] | None:
    """Every turn's policy in one pass over a Codex rollout, ``{turn_id: policy}`` in file order (a turn's FIRST
    ``turn_context`` counts). None when the file cannot be read; raises nothing for a missing one. The policy keeps
    only ``approval_policy`` (a string; ``granular`` for a mapping), ``approvals_reviewer``, and the ``type`` of
    ``sandbox_policy`` and ``permission_profile``: nothing else of the rollout (paths, roots) is carried."""
    if not rollout_path:
        return None
    out: dict[str, Mapping[str, Any]] = {}
    skipped = dict.fromkeys(SKIP_REASONS, 0)
    try:
        for raw in _lines(rollout_path, max_bytes, max_line_bytes, skipped):
            if b'"turn_context"' not in raw:
                continue
            entry = _parse(raw, skipped)
            payload = entry.get("payload") if isinstance(entry, dict) else None
            if not isinstance(payload, dict) or entry.get("type") != "turn_context":
                continue
            turn = payload.get("turn_id")
            if isinstance(turn, str) and turn not in out:
                out[turn] = _policy_of(payload)
    except OSError:
        return None
    return out


def _policy_of(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    approval = payload.get("approval_policy")

    def kind(value: Any) -> Any:
        return {"type": value.get("type")} if isinstance(value, Mapping) else value

    return {"approval_policy": approval if isinstance(approval, str) or approval is None else "granular",
            "sandbox_policy": kind(payload.get("sandbox_policy")),
            "permission_profile": kind(payload.get("permission_profile")),
            "approvals_reviewer": payload.get("approvals_reviewer")}


def codex_turn_policy(rollout_path: str | os.PathLike[str] | None, turn_id: str | None = None, *,
                      max_bytes: int = DEFAULT_MAX_BYTES) -> Mapping[str, Any] | None:
    """The policy of a Codex turn from the rollout's ``turn_context`` (see :func:`codex_turn_policies`). ``turn_id``
    None takes the LAST turn recorded: that is a test and CLI convenience only, since a call whose turn is unknown
    would be stamped with whatever policy the session reached later. None when the file or the turn is missing."""
    turns = codex_turn_policies(rollout_path, max_bytes=max_bytes)
    if not turns:
        return None
    if turn_id is None:
        return list(turns.values())[-1]
    return turns.get(turn_id)


def codex_stamp(policy: Mapping[str, Any] | None) -> str:
    """The authority stamp of a Codex turn's policy. Covered: ``never`` + a read-only, workspace-write or
    external sandbox -> sandbox (nothing can be escalated: the sandbox is the only authority); ``never`` +
    danger-full-access -> bypass. EVERYTHING ELSE is ``unknown``: under any approval policy that can ask
    (on-request, untrusted, on-failure, granular) a call may have been escalated and approved by the user, and the
    rollout records no per-call approval (openai/codex rust-v0.162.1, ``codex-rs/rollout/src/policy.rs``: the
    ``ExecApprovalRequest`` / ``ApplyPatchApprovalRequest`` / ``RequestPermissions`` events sit in the transient,
    non-durable arm, and the user's answer is an ``Op``, not a rollout item), so own_rules would claim an authority
    the source does not show. Also unknown: an unrecognised sandbox type, and a reviewer other than the user (Codex's
    auto-reviewer is a classifier-like authority nobody has measured here)."""
    if not isinstance(policy, Mapping):
        return "unknown"
    approval = policy.get("approval_policy")
    sandbox = policy.get("sandbox_policy")
    kind = sandbox.get("type") if isinstance(sandbox, Mapping) else sandbox
    reviewer = policy.get("approvals_reviewer")
    if approval != "never" or not isinstance(kind, str) or reviewer not in (None, "user"):
        return "unknown"
    if kind in ("read-only", "workspace-write", "external-sandbox"):
        return "sandbox"
    if kind == "danger-full-access":
        return "bypass"
    return "unknown"
