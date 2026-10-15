"""levain.consent.policy — the one seam every agent adapter asks for a policy answer.

An adapter files a request, then asks :func:`guarded_policy`. A policy answer is never the hook's own say-so: it is
a standing consent grant, signed into the authority log (``levain consent grant create``), that the queue finds in a
FRESH replay of the log, checks against the criteria (:func:`grant_refusal`), records as the request's answer, and
re-verifies in another fresh replay every time it is delivered (:meth:`ConsentQueue.decision`). A policy only ever
allows; there are no deny grants. Every request no grant covers goes to a signed answer or to the human.

The criteria, all of which must hold: the request is not agent config (tier :data:`AGENT_CONFIG_TIER`, never); the
grant names the request's agent, its exact tool, and the posture the agent's own event reported (a request with no
posture matches nothing); the grant's workspace is, at decision, the very directory it was signed for (its realpath
is the signed string, checked at append too) and not ``/``, this machine's home or above it; the request's working
directory resolves inside it; every target path the request names (:func:`grant_targets`: a file tool's target and
any tool's :data:`MCP_PATH_FIELDS`, Claude Code's Read, Grep, LS and NotebookRead included) is inside it with no
existing symlink on the way, and a file tool whose target cannot be read, a path field of another shape and a Glob
pattern that can reach outside its root match nothing; and the grant has not expired. Which tools are file tools,
and how their targets are read, is per agent (:data:`AGENT_FILE_TOOLS`, :func:`register_targets`): Claude Code's are
built in; Codex's ``apply_patch`` has its targets read by the Codex adapter's extractor, registered when that adapter
is imported, and with no extractor registered it matches nothing. A tool naming no path (WebFetch, an MCP tool
without path fields) is matched on the working directory alone. The tool input the criteria read is the one the
request's digest was computed from: the adapter passes it in, and a digest mismatch decides nothing. The paths are
checked when levain answers, not when the agent uses them (a known edge in the CHANGELOG).

A request filed at :data:`AGENT_CONFIG_TIER` (an edit of agent config or hooks) never reaches a policy (ruling 3b
item 1): :func:`guarded_policy` raises :class:`PolicyNotEligible` instead of asking.
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

from levain.authority import CONSENT_GRANT_KIND, CONSENT_SHELL_TOOLS, consent_root_problem
from levain.cockpit.consent import ConsentError, parse_stamp
from levain.consent.record import tool_input_digest

if TYPE_CHECKING:
    from levain.consent.queue import ConsentQueue, Decision
    from levain.consent.record import ExternalGateRequest

__all__ = ["AGENT_CONFIG_TIER", "AGENT_FILE_TOOLS", "FILE_TOOLS", "GLOB_TOOLS", "MCP_PATH_FIELDS", "Policy",
           "PolicyNotEligible", "Targets", "decide", "grant_refusal", "grant_targets", "guarded_policy",
           "register_targets", "targets"]

# The tier an agent_config request is filed at: an allow then needs a key class covering T3.
AGENT_CONFIG_TIER = "T3"
# The tools whose one target path is their ``file_path`` (else ``notebook_path``).
FILE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
# The tool_input fields an MCP tool's target paths are read from (the reference filesystem server's names, plus
# Claude Code's own), and that a grant's criteria read for EVERY tool (:func:`grant_targets`): only these, never a
# value parsed out of another field.
MCP_PATH_FIELDS = ("file_path", "path", "paths", "notebook_path", "source", "destination")
# Claude Code's tools whose ``pattern`` is a path glob read from the working directory (or ``path``).
GLOB_TOOLS = frozenset({"Glob"})
# A glob that can reach outside where it is rooted: ``..`` anywhere, or an absolute or ``~`` path at its start or at
# the start of a brace / extglob alternative (``{/etc,x}``). Over-matching only sends a request to the human.
_ESCAPING_GLOB = re.compile(r"\.\.|(?:^|[{,(|])\s*[/~]")

Policy = Callable[..., "Decision | None"]
# (tool_name, tool_input) -> the target paths, or None when the tool names none
Targets = Callable[[str, Any], "list[Any] | None"]
# per agent, the tools whose targets are file paths: one whose targets cannot be read matches no grant
AGENT_FILE_TOOLS: Mapping[str, frozenset[str]] = {"claude_code": FILE_TOOLS, "codex": frozenset({"apply_patch"})}


class PolicyNotEligible(AssertionError):
    """The policy seam was reached for a request that may never have a policy answer (ruling 3b item 1)."""


def targets(tool_name: str, tool_input: Any) -> list[Any] | None:
    """The paths a request WRITES, as agent-config tagging reads them, or ``None`` when it is not a tool that names
    any. A file tool names one (``file_path``, else ``notebook_path``; ``None`` in the list when it has neither);
    an MCP tool names whatever its :data:`MCP_PATH_FIELDS` hold (a ``paths`` list item by item), possibly nothing.
    A grant's criteria read more (:func:`grant_targets`)."""
    if tool_name in FILE_TOOLS:
        return [tool_input.get("file_path", tool_input.get("notebook_path")) if isinstance(tool_input, Mapping)
                else None]
    if not tool_name.startswith("mcp__") or not isinstance(tool_input, Mapping):
        return None
    out: list[Any] = []
    for field in MCP_PATH_FIELDS:
        if field in tool_input:
            value = tool_input[field]
            out += value if isinstance(value, list) else [value]
    return out


def grant_targets(tool_name: str, tool_input: Any) -> list[Any]:
    """The paths a grant's criteria check for a request on ANY tool a grant can name (Claude Code's Read, Grep,
    LS, NotebookRead... as much as its file tools): :func:`targets`, plus every :data:`MCP_PATH_FIELDS` field the
    input carries (``paths`` a non-empty list, item by item; every other field a string). ``None`` in the list,
    which matches nothing, for a field of another shape, a :data:`GLOB_TOOLS` pattern that is not a string or can
    reach outside its root (:data:`_ESCAPING_GLOB`), or an input that is not an object. ``[]``: no path named, so
    the request is matched on its working directory alone."""
    if not isinstance(tool_input, Mapping):
        return [None]
    out: list[Any] = list(targets(tool_name, tool_input) or [])
    for field in MCP_PATH_FIELDS:
        if field in tool_input:
            value = tool_input[field]
            if field == "paths":
                out += value if isinstance(value, list) and value else [None]
            else:
                out.append(value if isinstance(value, str) else None)
    if tool_name in GLOB_TOOLS:
        pattern = tool_input.get("pattern")
        if not isinstance(pattern, str) or _ESCAPING_GLOB.search(pattern):
            out.append(None)
    return out


_TARGETS: dict[str, Targets] = {}


def register_targets(agent: str, extract: Targets) -> None:
    """Set how ``agent``'s request targets are read (the Codex adapter registers its ``apply_patch`` reader when it
    is imported). Claude Code's is :func:`grant_targets`. An agent with none registered: its file tools match
    nothing, and any other tool's targets are read by :func:`grant_targets`."""
    _TARGETS[agent] = extract


register_targets("claude_code", grant_targets)


def _request_targets(agent: str, tool_name: str, tool_input: Any) -> list[Any] | None:
    """The target paths of a request, ``[None]`` (matches nothing) for a file tool whose targets cannot be read."""
    file_tool = tool_name in AGENT_FILE_TOOLS.get(agent, frozenset())
    extract = _TARGETS.get(agent)
    if extract is None:
        return [None] if file_tool else grant_targets(tool_name, tool_input)
    try:
        got = extract(tool_name, tool_input)
    except Exception:  # noqa: BLE001 - an extractor that fails reads no target: nothing matches
        return [None]
    if file_tool and (not isinstance(got, list) or not got):
        return [None]
    return got if isinstance(got, list) or got is None else [None]


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _resolved(target: Any, cwd: str) -> str | None:
    """``target`` as a path (``~`` expanded, joined to ``cwd`` when relative), only when the filesystem resolves
    it to exactly its lexical normal form: no existing component on it is a symlink. A ``..`` component in the
    target as given matches nothing, link or no link. ``None`` for anything else, or anything that is not a usable
    path."""
    if not isinstance(target, str) or not target or "\x00" in target or ".." in target.split(os.sep):
        return None
    raw = os.path.expanduser(target) if target == "~" or target.startswith("~/") else target
    joined = raw if os.path.isabs(raw) else os.path.join(cwd, raw)
    try:
        lexical = os.path.normpath(joined)
        return lexical if os.path.realpath(joined) == lexical else None
    except (OSError, ValueError):
        return None


def grant_refusal(grant: Mapping[str, Any], request: ExternalGateRequest, *, tool_input: Any, posture: Any,
                  now: datetime) -> str | None:
    """Why ``grant`` does not answer ``request`` at ``now`` for ``tool_input`` under ``posture``; ``None`` when
    every criterion holds (module docstring). ``tool_input`` must be the input the request's digest was computed
    from: anything else is refused here, so what was checked is what is answered."""
    if not isinstance(grant, Mapping) or grant.get("kind") != CONSENT_GRANT_KIND:
        return "not a consent grant"
    if request.tier == AGENT_CONFIG_TIER:
        return "agent config is never answered by a grant"
    if grant.get("agent") != request.agent:
        return "the grant is for another agent"
    tools = grant.get("tools")
    if request.tool_name.lower() in CONSENT_SHELL_TOOLS or not isinstance(tools, (list, tuple)) \
            or request.tool_name not in tools:
        return "the grant does not name this tool"
    postures = grant.get("postures")
    if not isinstance(posture, str) or not isinstance(postures, (list, tuple)) or posture not in postures:
        return "the grant does not cover this posture"
    try:
        if tool_input_digest(tool_input) != request.tool_input_digest:
            return "the tool input is not the one the request was filed for"
        expires = parse_stamp(grant.get("expires_at"))
    except (ConsentError, TypeError, ValueError, RecursionError):
        return "the grant or the tool input cannot be read"
    if not now < expires:
        return "the grant has expired"
    workspace = grant.get("workspace")
    if not isinstance(workspace, str) or not os.path.isabs(request.cwd):
        return "no workspace to match"
    try:
        root = os.path.realpath(workspace)
        cwd = os.path.realpath(request.cwd)
    except (OSError, ValueError):
        return "the workspace cannot be resolved"
    # the signed string is the directory's real path (checked at append); a rename and a symlink in its place, or
    # a log written where home is elsewhere, must not widen it
    if root != workspace or not os.path.isdir(root) or consent_root_problem(root) is not None:
        return "the workspace is not, here and now, the directory the grant was signed for"
    if not _within(cwd, root):
        return "the working directory is outside the grant's workspace"
    paths = _request_targets(request.agent, request.tool_name, tool_input)
    if paths is not None:
        for target in paths:
            where = _resolved(target, request.cwd)
            if where is None or not _within(where, root):
                return "a target is outside the grant's workspace"
    return None


def decide(request: ExternalGateRequest, queue: ConsentQueue, *, posture: str | None = None,
           tool_input: Any = None) -> Decision | None:
    """The policy answer for ``request`` on ``queue``, or ``None``. ``posture`` is the agent session's own
    permission posture as its event reports it (Claude Code's ``permission_mode``), ``None`` when absent;
    ``tool_input`` is the input the request was filed for (``None``: not given, so no answer). The queue finds
    a grant in a fresh replay, records the answer and returns it as delivery re-verifies it."""
    if request.tier == AGENT_CONFIG_TIER or posture is None or tool_input is None:
        return None
    return queue.answer_by_grant(request.id, tool_input=tool_input, posture=posture)


def guarded_policy(request: ExternalGateRequest, queue: ConsentQueue, *, posture: str | None = None,
                   tool_input: Any = None, policy: Policy | None = None) -> Decision | None:
    """``policy`` (default :func:`decide`), called as ``policy(request, queue, posture=posture,
    tool_input=tool_input)``, with ruling 3b item 1 asserted first: a request filed at :data:`AGENT_CONFIG_TIER`
    never reaches it."""
    if request.tier == AGENT_CONFIG_TIER:
        raise PolicyNotEligible(f"request {request.id} targets agent config: never eligible for a policy answer")
    return (policy or decide)(request, queue, posture=posture, tool_input=tool_input)
