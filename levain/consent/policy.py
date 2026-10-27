"""levain.consent.policy — the one seam every agent adapter asks for a policy answer.

An adapter files a request, then asks :func:`guarded_policy`. A policy answer is never the hook's own say-so: it is
a standing consent grant, signed into the authority log (``levain consent grant create``), that the queue finds in a
FRESH replay of the log, checks against the criteria (:func:`grant_refusal`), records as the request's answer, and
re-verifies in another fresh replay every time it is delivered (:meth:`ConsentQueue.decision`). A policy only ever
allows; there are no deny grants. Every request no grant covers goes to a signed answer or to the human.

The criteria, all of which must hold: the request is not agent config (tier :data:`AGENT_CONFIG_TIER`, never); the
grant names the request's agent, its exact tool (one levain knows for that agent and never a shell:
:func:`levain.authority.consent_tool_problem`), and the posture the agent's own event reported (a request with no
posture matches nothing); the grant's workspace is, at decision, the very directory it was signed for (its realpath
is the signed string, checked at append too) and not ``/``, this machine's home or above it; the request's working
directory resolves inside it; every target path the request names (:func:`grant_targets`: a file tool's target and
any tool's :data:`MCP_PATH_FIELDS`, Claude Code's Read, Grep, LS and NotebookRead included) is inside it with no
existing symlink on the way, and a file tool whose target cannot be read, a path field of another shape and a Glob
pattern that can reach outside its root match nothing; an MCP tool is one of the read tools levain knows
(:data:`levain.authority.CONSENT_MCP_READ_ONLY_TOOLS`) and its input is one that tool's schema accepts, or one a
consent-mcp grant declares (:func:`levain.authority.consent_mcp_declaration`) and its input carries only declared
fields, each value of its kind, every declared ``path`` value and ``paths`` item joining the targets checked (beside
the named fields, never instead); and the grant has not expired. Which tools are file tools,
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

from levain.authority import (consent_grant, consent_grant_tool_problem, consent_mcp_declaration, consent_mcp_input_fits,
                              consent_root_problem, contained_shell_grant)
from levain.cockpit.consent import ConsentError, parse_stamp
from levain.consent.contained import ContainedLaunchProof
from levain.consent.record import tool_input_digest

if TYPE_CHECKING:
    from levain.consent.queue import ConsentQueue, Decision
    from levain.consent.record import ExternalGateRequest

__all__ = ["AGENT_CONFIG_TIER", "AGENT_FILE_TOOLS", "FILE_TOOLS", "GLOB_TOOLS", "MCP_PATH_FIELDS", "Policy",
           "PolicyNotEligible", "Targets", "contained_shell_refusal", "decide", "grant_refusal", "grant_targets", "guarded_policy",
           "mcp_config_candidates", "register_targets", "targets"]

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


# The bound on reading an MCP tool's input for agent config (:func:`mcp_config_candidates`): a spelling longer than
# the longest path an OS opens (Linux PATH_MAX), once normalised, names no file; past the string count or the depth,
# the input is tagged.
_CANDIDATE_MAX_LEN = 4096
_CANDIDATE_MAX_COUNT = 4096
_CANDIDATE_MAX_DEPTH = 64
# the characters of every kept spelling together: each is resolved through the filesystem, one lstat per component
_CANDIDATE_MAX_TOTAL = 256 * 1024


# a string longer than this is normalised (linear) before it is decoded; decoding reads at most
# _CANDIDATE_RAW_MAX characters across one input
_CANDIDATE_DECODE_MAX = 64 * 1024
_CANDIDATE_RAW_MAX = 4 * 1024 * 1024
# the most spellings one string yields, and the most JSON nodes (strings, containers, scalars) one input holds
_SPELLINGS_PER_STRING = 16
_CANDIDATE_MAX_NODES = 4 * _CANDIDATE_MAX_COUNT


def _decoded(form: str) -> list[str]:
    """One decoding step of ``form``: trimmed, percent-decoded, and as a ``file:`` URI's path (its authority,
    query and fragment dropped, the path percent-decoded, as Node's ``fileURLToPath`` and Python's ``url2pathname``
    read it)."""
    from urllib.parse import unquote, urlsplit

    out = [form.strip()]
    if "%" in form:
        out.append(unquote(form))
    lead = form.lstrip()
    if lead[:5].lower() == "file:":
        try:
            out.append(unquote(urlsplit(lead).path))
        except ValueError:
            out.append(lead[5:])
    return out


def _spellings(value: str) -> list[str] | None:
    """The paths one input string can reach a server as: cut at its first NUL (a C server stops there); a string over
    :data:`_CANDIDATE_DECODE_MAX` first trimmed and normalised, and ``None`` if what is left is still that long and
    holds an encoded dot or slash or a ``file:`` scheme; then, to a fixed point, trimmed, percent-decoded and read
    as a ``file:`` URI (:func:`_decoded`); each as given and lexically normalised (a server's
    ``path.resolve`` / ``abspath`` / ``filepath.Clean`` collapses ``./``, ``..`` and slash runs before it opens).
    Only the non-empty ones at most :data:`_CANDIDATE_MAX_LEN` long are kept. ``None`` past
    :data:`_SPELLINGS_PER_STRING` forms (the caller tags the request)."""
    head = value.split("\x00", 1)[0]
    if len(head) > _CANDIDATE_DECODE_MAX:
        # collapse first (linear), then decode what that leaves when it is short; a long string a server could
        # still decode into a shorter path (an encoded dot or slash, a file: URI) goes to the human
        short = os.path.normpath(head.strip())
        if len(short) > _CANDIDATE_DECODE_MAX:
            low = short.lower()
            if "%2e" in low or "%2f" in low or "%5c" in low or "file:" in low[:16]:
                return None
            return [short] if len(short) <= _CANDIDATE_MAX_LEN else []
        head = short
    forms = [head]
    todo = [head]
    while todo:
        form = todo.pop()
        for nxt in (_decoded(form) if len(form) <= _CANDIDATE_DECODE_MAX else [form.strip()]):
            nxt = nxt.split("\x00", 1)[0]
            if nxt not in forms:
                if len(forms) >= _SPELLINGS_PER_STRING:
                    return None
                forms.append(nxt)
                todo.append(nxt)
    out: list[str] = []
    for form in forms:
        for spelled in (form, os.path.normpath(form) if form else form):
            if spelled and len(spelled) <= _CANDIDATE_MAX_LEN and spelled not in out:
                out.append(spelled)
    return out


def mcp_config_candidates(tool_input: Any) -> list[str] | None:
    """Every string in an MCP tool's input that could name a file, for agent-config tagging only (ruling 3b item 1):
    an MCP server can take its path in a field of any name (``filepath``, ``file``, a key of a path-to-content map,
    an item of a nested list), so tagging reads every string key and value at any depth, not only
    :data:`MCP_PATH_FIELDS`, in each of its :func:`_spellings`; the input need not be an object. ``None`` (the
    caller tags the request), decided as the walk goes, when the input holds more than :data:`_CANDIDATE_MAX_COUNT`
    strings or :data:`_CANDIDATE_MAX_NODES` nodes, more than :data:`_CANDIDATE_RAW_MAX` characters to decode, nests deeper than :data:`_CANDIDATE_MAX_DEPTH`, a string has too
    many spellings, or the kept spellings total more than :data:`_CANDIDATE_MAX_TOTAL` characters. Over-matching
    only sends a request to the human; a grant's own criteria still read the named fields alone
    (:func:`grant_targets`). This is best effort by construction: MCP declares no path fields (a tool's
    annotations are untrusted, access control is the server's), so a path a server builds from what no string holds
    whole (a root it was configured with, an encoding other than these) is not seen, a known edge in the
    CHANGELOG."""
    out: list[str] = []
    strings = nodes = total = raw = 0
    stack: list[tuple[Any, int]] = [(tool_input, 0)]
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > _CANDIDATE_MAX_NODES:
            return None
        if isinstance(value, str):
            strings += 1
            raw += min(len(value), _CANDIDATE_DECODE_MAX)
            got = _spellings(value) if strings <= _CANDIDATE_MAX_COUNT and raw <= _CANDIDATE_RAW_MAX else None
            if got is None:
                return None
            total += sum(map(len, got))
            if total > _CANDIDATE_MAX_TOTAL:
                return None
            out += got
        elif isinstance(value, (Mapping, list, tuple)):
            if depth >= _CANDIDATE_MAX_DEPTH or len(value) > _CANDIDATE_MAX_NODES:
                return None
            for item in (value.items() if isinstance(value, Mapping) else value):
                if isinstance(value, Mapping):
                    stack.append((item[0], depth + 1))
                    stack.append((item[1], depth + 1))
                else:
                    stack.append((item, depth + 1))
            if len(stack) > _CANDIDATE_MAX_NODES:
                return None
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


def _resolved(target: Any, cwd: str, *, home: str | os.PathLike[str]) -> str | None:
    """``target`` as a path (``~`` expanded under ``home``, joined to ``cwd`` when relative), only when the filesystem
    resolves
    it to exactly its lexical normal form: no existing component on it is a symlink. A ``..`` component in the
    target as given matches nothing, link or no link. ``None`` for anything else, or anything that is not a usable
    path."""
    if not isinstance(target, str) or not target or "\x00" in target or ".." in target.split(os.sep):
        return None
    # os.path.expanduser's own join, with ``home`` for $HOME
    raw = ((os.fspath(home).rstrip(os.sep) + target[1:]) or os.sep) if target == "~" or target.startswith("~/") \
        else target
    joined = raw if os.path.isabs(raw) else os.path.join(cwd, raw)
    try:
        lexical = os.path.normpath(joined)
        return lexical if os.path.realpath(joined) == lexical else None
    except (OSError, ValueError):
        return None


def _declared_targets(declared: Mapping[str, str], tool_input: Any) -> list[Any] | None:
    """The target paths of an input to a tool a consent-mcp grant declares (field -> kind): every ``path`` value and
    each item of every ``paths`` value. ``None`` (matches nothing) unless the input is an object whose every field is
    declared and whose every value fits its kind: ``path`` and ``str`` a string, ``paths`` a non-empty list of
    strings, ``strs`` a list of strings, ``int`` a JSON integer, ``bool`` a JSON boolean. A declared field the input
    leaves out is not required."""
    if not isinstance(tool_input, Mapping):
        return None
    out: list[Any] = []
    for name, value in tool_input.items():
        kind = declared.get(name) if isinstance(name, str) else None
        if kind in ("path", "str"):
            ok = isinstance(value, str)
        elif kind in ("paths", "strs"):
            ok = isinstance(value, list) and all(isinstance(v, str) for v in value) and (kind == "strs" or bool(value))
        elif kind == "int":
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif kind == "bool":
            ok = isinstance(value, bool)
        else:
            ok = False
        if not ok:
            return None
        if kind == "path":
            out.append(value)
        elif kind == "paths":
            out += value
    return out


def grant_refusal(grant: Mapping[str, Any], request: ExternalGateRequest, *, tool_input: Any, posture: Any,
                  now: datetime, home: str | os.PathLike[str]) -> str | None:
    """Why ``grant`` does not answer ``request`` at ``now`` for ``tool_input`` under ``posture``; ``None`` when
    every criterion holds (module docstring). ``home`` is this machine's home directory for the home check and a
    ``~`` target (the queue's, :class:`ConsentQueue`). ``tool_input`` must be the input the request's digest was
    computed
    from: anything else is refused here, so what was checked is what is answered."""
    if not consent_grant(grant):
        return "not a consent grant"   # an egress grant, or one of a kind this levain does not know
    if request.tier == AGENT_CONFIG_TIER:
        return "agent config is never answered by a grant"
    if grant.get("agent") != request.agent:
        return "the grant is for another agent"
    tools = grant.get("tools")
    if not isinstance(tools, (list, tuple)) or request.tool_name not in tools:
        return "the grant does not name this tool"
    if consent_grant_tool_problem(grant, request.tool_name) is not None:   # one signed before the append rule
        return "a grant never answers this tool (a shell, an MCP tool that may write, or a tool levain does not know)"
    declared = consent_mcp_declaration(grant, request.tool_name)
    declared_paths: list[Any] | None = None
    if declared is not None:
        declared_paths = _declared_targets(declared, tool_input)
        if declared_paths is None:
            return "the input is not one the grant declares for this tool"
    elif request.tool_name.startswith("mcp__") and not consent_mcp_input_fits(request.tool_name, tool_input):
        return "the input is not one the read tool levain knows takes"  # another server's read_file, say
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
    if root != workspace or not os.path.isdir(root) or consent_root_problem(root, home=home) is not None:
        return "the workspace is not, here and now, the directory the grant was signed for"
    if not _within(cwd, root):
        return "the working directory is outside the grant's workspace"
    paths = _request_targets(request.agent, request.tool_name, tool_input)
    if declared_paths:      # the declared ones beside the named fields read for every tool, never instead
        paths = (paths or []) + declared_paths
    if paths is not None:
        for target in paths:
            where = _resolved(target, request.cwd, home=home)
            if where is None or not _within(where, root):
                return "a target is outside the grant's workspace"
    return None


def contained_shell_refusal(grant: Mapping[str, Any], request: ExternalGateRequest, *, tool_input: Any,
                            posture: Any, now: datetime, home: str | os.PathLike[str],
                            contained_launch: ContainedLaunchProof | None) -> str | None:
    """Why the contained-shell grant ``grant`` (SH1) does not answer ``request`` from the launch ``contained_launch``
    proves, at ``now``, for ``tool_input`` under ``posture``; ``None`` when every criterion holds. Checked in this
    order, the first failing one named: a proof of the right shape; a contained-shell grant; not agent config; the
    grant's agent; the tool is exactly ``Bash`` (no case fold, no strip, never an MCP tool); the input an object
    without the ``dangerouslyDisableSandbox`` key (any value; the command is never read); the input the request was
    filed for; the posture; unexpired; the grant's ``containment_digest`` is the proof's ``profile_digest``; the
    request's directory is the launch's; the launch's directory inside the grant's workspace, which is, here and
    now, the directory the grant was signed for and not ``/`` or ``home`` or above it."""
    if contained_launch is None or not isinstance(contained_launch, ContainedLaunchProof) \
            or contained_launch.problem() is not None:
        return "no contained launch"
    if not contained_shell_grant(grant):
        return "not a contained-shell grant"
    if request.tier == AGENT_CONFIG_TIER:
        return "agent config is never answered by a grant"
    if grant.get("agent") != request.agent:
        return "the grant is for another agent"
    if request.tool_name != "Bash":
        return "not the Bash tool"
    if not isinstance(tool_input, Mapping):
        return "the input is not an object"
    if "dangerouslyDisableSandbox" in tool_input:
        return "the call asks to leave the sandbox"
    try:
        if tool_input_digest(tool_input) != request.tool_input_digest:
            return "not the input the request was filed for"
        expires = parse_stamp(grant.get("expires_at"))
    except (ConsentError, TypeError, ValueError, RecursionError):
        return "the grant or the tool input cannot be read"
    if posture != "default" or posture not in (grant.get("postures") or ()):
        return "posture"
    if not now < expires:
        return "expired"
    if grant.get("containment_digest") != contained_launch.profile_digest:
        return "another containment"
    workspace = grant.get("workspace")
    if not isinstance(workspace, str) or not os.path.isabs(request.cwd):
        return "no workspace to match"
    try:
        root = os.path.realpath(workspace)
        cwd = os.path.realpath(request.cwd)
    except (OSError, ValueError):
        return "the workspace cannot be resolved"
    if cwd != contained_launch.cwd:
        return "not this launch's directory"
    if not _within(contained_launch.cwd, root):
        return "outside the grant's workspace"
    if root != workspace or not os.path.isdir(root):
        return "the workspace is not, here and now, the directory the grant was signed for"
    problem = consent_root_problem(root, home=home)
    if problem is not None:
        return problem
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
