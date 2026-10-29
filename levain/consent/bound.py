"""BOUND: what the vendor's sandbox refused INSIDE a call that ran (the S1 record beside STAMP and OUTCOME).

A call can exit 0 while one step of it was refused by the agent's own sandbox, so the exit status and the stamp say
nothing about it. This module reads the tool's OUTPUT (and, for WebFetch, the INPUT url as a gate only) and returns a
:class:`BoundRecord`: a bound, a count and a set of classes. It INFORMS and never authorises; it moves no stamp and no
outcome. It stores a class and a count, never a url, host, path, stderr line or any echo of the command.

The four bounds: ``none`` (no bound was in force over this call), ``touched`` (a measured refusal string was seen),
``clean`` (only a WebFetch whose result is the measured success object), ``unknown`` (the evidence is silent or not
recognised; silence is never ``clean`` for a shell or patch call, because ``curl -s`` and a write sent to
``/dev/null`` leave no text).

Only strings measured in a real run are matched; every other denial text reads ``unknown``. The class whitelist is
wider than what is emitted: ``agent_config`` and ``process_info`` are reserved for a later, measured matcher.

Known edges (one line each): Codex's ``tool_response`` is one string, stdout and stderr undistinguished, so a command
that prints the phrase on stdout reads ``touched`` there (Claude Code separates stderr, so the same command reads
``unknown``); WebFetch is outside the Claude Code Bash sandbox, so its refusal rule fires only where a proxy refuses
the fetch; a Codex patch the sandbox rejected may fire no post event at all, so it has no row to classify; an
``Operation not permitted`` the OS raises with no sandbox involved (``chown``, ``kill`` as a non-owner) inside a
bounded call reads ``touched``, since the text is the same.

Pure: no I/O, no clock, no environment. ``classify`` and ``classify_payload`` never raise, since a hook must get a
record, not a crash.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from levain.consent.contained import SANDBOX_ESCAPE_KEY

__all__ = ["AGENT_SENTENCE", "BOUNDS", "CLASSES", "MAX_DENIALS", "MAX_SCAN_CHARS", "BoundRecord",
           "agent_sentence", "classify", "classify_payload"]

BOUNDS: tuple[str, ...] = ("none", "clean", "touched", "unknown")
CLASSES: tuple[str, ...] = ("write_outside_roots", "agent_config", "network", "process_info", "other")
MAX_DENIALS = 999
MAX_SCAN_CHARS = 1_000_000
# the tools whose `command` is a shell line
SHELL_TOOLS = frozenset({"Bash", "shell", "exec_command", "shell_command", "local_shell"})
PATCH_TOOL = "apply_patch"     # Codex only
FETCH_TOOL = "WebFetch"        # Claude Code only
_TOOL_EVENTS = ("PostToolUse", "PostToolUseFailure")
_CODEX_ESCALATION_KEY = "sandbox_permissions"
_CODEX_ESCALATED = "require_escalated"
_MAX_URL = 2048
_MAX_HOST = 253
# case-insensitive: an OS strerror, spelled differently by Go, Python and Node
_EPERM = re.compile(r"operation not permitted", re.IGNORECASE)
_EROFS = re.compile(r"read-only file system", re.IGNORECASE)
# fixed vendor texts: case-sensitive
_PROXY_403 = re.compile(r"CONNECT tunnel failed, response 403\b")
_PATCH_OUTSIDE = re.compile(r"patch rejected: writing outside of the project")
_SHELL_PATTERNS = ((_EPERM, "other"), (_EROFS, "other"), (_PROXY_403, "network"))
_PATCH_PATTERNS = ((_PATCH_OUTSIDE, "write_outside_roots"),)
_FETCH_REFUSED = frozenset({"proxy refused the connection", "Error: proxy refused the connection"})
AGENT_SENTENCE = ("levain: the sandbox refused part of this command ({n} operation(s): {classes}). An exit status of 0 "
                  "does not mean every step ran; check that what you meant to happen did. If the step must run outside "
                  "the sandbox, ask for it; it goes to the operator.")


@dataclass(frozen=True, slots=True)
class BoundRecord:
    """One call's bound: ``touched`` carries a positive count and the classes seen; every other bound carries 0 and
    none."""
    bound: str
    denials: int
    classes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.bound not in BOUNDS:
            raise ValueError(f"bound {self.bound!r} is not one of {BOUNDS}")
        if type(self.denials) is not int or not 0 <= self.denials <= MAX_DENIALS:
            raise ValueError(f"denials must be an int in 0..{MAX_DENIALS} (got {self.denials!r})")
        if not isinstance(self.classes, tuple) or any(c not in CLASSES for c in self.classes):
            raise ValueError(f"classes must be a tuple of members of {CLASSES}")
        if list(self.classes) != [c for c in CLASSES if c in self.classes]:
            raise ValueError("classes must be distinct and in CLASSES order")
        if not ((self.bound == "touched") == (self.denials > 0) == (len(self.classes) > 0)):
            raise ValueError("a touched record carries denials and classes; no other record does")

    def as_dict(self) -> dict[str, Any]:
        return {"bound": self.bound, "denials": self.denials, "classes": list(self.classes)}

    def classes_text(self) -> str | None:
        return ",".join(self.classes) if self.classes else None

    @classmethod
    def from_columns(cls, bound: str | None, denials: int | None, classes_text: str | None) -> BoundRecord | None:
        """The record a ledger row's three columns hold; None when the row recorded no bound."""
        if bound is None:
            return None
        return cls(bound, 0 if denials is None else denials, tuple(classes_text.split(",")) if classes_text else ())


def _none() -> BoundRecord:
    return BoundRecord("none", 0, ())


def _unknown() -> BoundRecord:
    return BoundRecord("unknown", 0, ())


def _touched(n: int, classes: Iterable[str]) -> BoundRecord:
    seen = set(classes)
    return BoundRecord("touched", min(n, MAX_DENIALS), tuple(c for c in CLASSES if c in seen))


def _fetch_host(tool_input: Any) -> str | None:
    """The host WebFetch's input names, used only as a gate and dropped. None when the input has no usable one:
    Claude Code itself refuses a dotless host before fetching, so such a call never reached a bound."""
    if not isinstance(tool_input, Mapping):
        return None
    url = tool_input.get("url")
    if not isinstance(url, str) or len(url) > _MAX_URL:
        return None
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host or len(host) > _MAX_HOST or "." not in host:
        return None
    return host


def _classify_fetch(event: str, tool_input: Any, tool_response: Any, error: Any, bound_active: bool | None
                    ) -> BoundRecord:
    if _fetch_host(tool_input) is None:
        return _unknown()
    text = error if event == "PostToolUseFailure" else tool_response
    # an exact text, never a substring: a fetched page that discusses proxies must not read as a refusal
    if isinstance(text, str) and text.strip() in _FETCH_REFUSED:
        return _touched(1, ("network",))
    if event == "PostToolUse" and bound_active is True and _fetch_succeeded(tool_response):
        return BoundRecord("clean", 0, ())
    return _unknown()


def _fetch_succeeded(tool_response: Any) -> bool:
    """True only for the MEASURED success object (``{"bytes": 577, "code": 200, "codeText": "OK", "result": ...}``):
    an int ``code`` of 200, an int ``bytes`` and a str ``result``. Any other mapping (empty, an error shape, another
    status, a vendor's new shape) is not evidence of success, so it reads ``unknown``, never ``clean``."""
    if not isinstance(tool_response, Mapping):
        return False
    code, size, result = tool_response.get("code"), tool_response.get("bytes"), tool_response.get("result")
    return (type(code) is int and code == 200 and type(size) is int and size >= 0 and isinstance(result, str))


def _scan_text(agent: str, event: str, tool_response: Any, error: Any) -> str | None:
    if agent == "claude_code":
        if event == "PostToolUse":
            stderr = tool_response.get("stderr") if isinstance(tool_response, Mapping) else None
            return stderr if isinstance(stderr, str) else None
        if event == "PostToolUseFailure":
            return error if isinstance(error, str) else None
        return None
    if agent == "codex" and event in _TOOL_EVENTS:
        if isinstance(tool_response, str):
            return tool_response
        return error if isinstance(error, str) else None
    return None


def _scan(text: str, patterns: tuple[tuple[re.Pattern[str], str], ...]) -> BoundRecord:
    n = 0
    classes: set[str] = set()
    for line in text[:MAX_SCAN_CHARS].splitlines():
        hit = {cls for pattern, cls in patterns if pattern.search(line)}
        if hit:
            n += 1
            classes |= hit
    return _touched(n, classes) if n else _unknown()


def classify(agent: str, event: str, tool_name: Any, tool_input: Any, tool_response: Any = None, error: Any = None,
             *, bound_active: bool | None) -> BoundRecord:
    """The bound of one finished call. ``bound_active`` is the caller's word on whether a bound was in force
    (False: none; None: not known, so nothing reads ``clean`` but a refusal still reads ``touched``)."""
    try:
        if bound_active is False:
            return _none()
        shell = isinstance(tool_name, str) and tool_name in SHELL_TOOLS
        patch = tool_name == PATCH_TOOL and agent == "codex"
        fetch = tool_name == FETCH_TOOL and agent == "claude_code"
        if not (shell or patch or fetch):
            return _none()
        # the VALUE decides where the call ran: set true, it ran outside the sandbox
        if shell and isinstance(tool_input, Mapping) and tool_input.get(SANDBOX_ESCAPE_KEY) is True:
            return _none()
        # a Codex call that asked to run outside its sandbox (the field and value Codex 0.162.1 sent on an escalated
        # write_stdin input) may have run outside it, where an EPERM is the OS's and not the bound's: neither touched
        # nor none can be claimed
        if shell and agent == "codex" and isinstance(tool_input, Mapping) \
                and tool_input.get(_CODEX_ESCALATION_KEY) == _CODEX_ESCALATED:
            return _unknown()
        if fetch:
            return _classify_fetch(event, tool_input, tool_response, error, bound_active)
        text = _scan_text(agent, event, tool_response, error)
        if text is None:
            return _unknown()
        return _scan(text, _SHELL_PATTERNS if shell else _PATCH_PATTERNS)
    except Exception:
        return _unknown()


def classify_payload(agent: str, payload: Any, *, bound_active: bool | None) -> BoundRecord | None:
    """:func:`classify` over a hook payload; None when it is not a tool-finished event."""
    try:
        if not isinstance(payload, Mapping) or payload.get("hook_event_name") not in _TOOL_EVENTS:
            return None
        tool_name = payload.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            return None
        return classify(agent, payload["hook_event_name"], tool_name, payload.get("tool_input"),
                        payload.get("tool_response"), payload.get("error"), bound_active=bound_active)
    except Exception:
        return None


def agent_sentence(record: BoundRecord, tool_name: Any) -> str | None:
    """The one fixed sentence for the agent after a masked shell denial; no path, no command, only a count and class
    names. A WebFetch refusal is already visible to the agent as an error, so it gets none."""
    if record.bound != "touched" or not isinstance(tool_name, str) or tool_name not in SHELL_TOOLS:
        return None
    return AGENT_SENTENCE.format(n=record.denials, classes=", ".join(record.classes))
