"""levain.desk.room_profile — the host-chosen room profile: what a host that names its own repo and accepts the
operator's full reach gives the room INSTEAD of the confined profile.

The confined room (no shell, no writes, read scope = its cwd, ``dontAsk``) is what ``RoomSetting.profile=None`` means,
and it stays byte-for-byte what it was. A :class:`RoomProfile` is the other shape: the vendor's own default tool set
under ``bypassPermissions`` (one operator, one machine, the same reach as the host's other chat surface), the host's
own pre-tool hooks, its own system prompt and dials. Levain names no host path and no host text here: the host
supplies every script, string and number, and this module only validates them and renders them as plain data
(:func:`host_settings_json`, :func:`host_policy`). Nothing here spawns or opens a store; the one read of a script's
bytes is :meth:`HookSpec.check` hashing a PINNED script (``HookSpec.sha256``). A hook script is meant to be copied by
whoever launches the room, once per launch, and the copy's bytes hashed into the containment digest (and, for a pinned
script, compared with the pin), so what ran is what was recorded.

Pure helpers. Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Optional

__all__ = [
    "PROFILE_EFFORTS", "PROFILE_MODES", "PROFILE_SETTING_SOURCES", "HookSpec", "RoomProfile", "host_policy",
    "host_settings_json",
]

PROFILE_MODES = ("bypassPermissions",)
PROFILE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
PROFILE_SETTING_SOURCES = ("user", "project", "local")
_MAX_TURNS_BOUNDS = (1, 200)
_HOOK_TIMEOUT_BOUNDS = (1, 600)
_NAME_MAX = 64
_MATCHER_MAX = 512


def _has_control(text: str) -> bool:
    return any(unicodedata.category(c) == "Cc" for c in text)


def _clean_token(label: str, value: Any, *, limit: int) -> str:
    """A short single-line token (a name, a matcher, a model id): a non-empty string with no control character and no
    surrounding space."""
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > limit or _has_control(value):
        raise ValueError(f"{label} must be a non-empty single-line string of at most {limit} characters")
    return value


def _money(label: str, value: Any) -> float:
    """A positive dollar amount in whole cents: the argv spells it with two decimals, so a finer value would be a
    different number than the one validated."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be a positive number of dollars")
    if round(float(value), 2) != float(value):
        raise ValueError(f"{label} must be a whole number of cents")
    return float(value)


def _int_in(label: str, value: Any, bounds: tuple[int, int]) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not bounds[0] <= value <= bounds[1]:
        raise ValueError(f"{label} must be an integer from {bounds[0]} to {bounds[1]}")
    return value


@dataclass(frozen=True)
class HookSpec:
    """One PreToolUse hook the host gives the room. ``script`` is the absolute path of a standard-library python
    script on the host's side; it is meant to be read once per launch and the COPY run, so what ran is what was
    hashed. A relative or unreadable script is refused here and again by :meth:`check` (the file can vanish later).
    ``sha256``, when set, PINS the script: :meth:`check` refuses a file whose bytes hash to anything else, and a launcher
    compares the bytes it copies with it, so a pinned hook either runs exactly those bytes or the room does not open."""
    matcher: str
    script: str
    timeout: int = 10
    sha256: Optional[str] = None

    def __post_init__(self) -> None:
        _clean_token("hook matcher", self.matcher, limit=_MATCHER_MAX)
        _int_in("hook timeout", self.timeout, _HOOK_TIMEOUT_BOUNDS)
        if self.sha256 is not None and not _is_sha256(self.sha256):
            raise ValueError("a hook's pinned sha256 is 64 lowercase hex characters")
        self.check()

    def check(self) -> None:
        """Raise ValueError unless the script is an absolute path to a readable regular file, holding the pinned bytes
        when the spec has a pin."""
        if not isinstance(self.script, str) or not self.script or _has_control(self.script) or not os.path.isabs(self.script):
            raise ValueError("a hook script must be an absolute path")
        if not os.path.isfile(self.script) or not os.access(self.script, os.R_OK):
            raise ValueError(f"the hook script {self.script[:240]!r} is not a readable file")
        if self.sha256 is not None:
            try:
                with open(self.script, "rb") as f:
                    got = hashlib.sha256(f.read()).hexdigest()
            except OSError as exc:
                raise ValueError(f"the hook script {self.script[:240]!r} could not be read: {exc}") from exc
            self.matches(got)

    def matches(self, sha256: str) -> None:
        """Raise ValueError when this spec is pinned and ``sha256`` (of bytes about to run) is not its pin."""
        if self.sha256 is not None and sha256 != self.sha256:
            raise ValueError(f"the hook script {self.script[:240]!r} does not hold the pinned bytes (its sha256 is "
                             f"{sha256[:16]}..., the pin is {self.sha256[:16]}...)")


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


@dataclass(frozen=True)
class RoomProfile:
    """The host profile. See the module docstring; every field is the host's, none has a flow-specific default.

    ``permission_mode``: only ``bypassPermissions`` is accepted (``dontAsk`` with an allow list is not the same
    reach, and any other mode would stop on a prompt nobody answers). ``setting_sources``: which vendor config
    chains the child reads; the confined profile reads ``user`` only. ``turn_budget_usd`` and ``recycle_at_usd``: the
    vendor's budget flag is cumulative per PROCESS, so the argv carries their sum; ``recycle_at_usd`` is the spend at
    which a child is meant to be replaced between turns, so a long conversation never reaches the cap mid-turn.
    ``required_tools``: the open probe's FLOOR (the vendor's default set drifts by CLI version; a profile that is
    silently not the expected one lacks one of these)."""
    name: str = "host"
    permission_mode: str = "bypassPermissions"
    setting_sources: tuple = ("user",)
    hooks: tuple = ()
    system_prompt: Optional[str] = None
    model: Optional[str] = None
    effort: Optional[str] = None
    max_turns: Optional[int] = None
    turn_budget_usd: Optional[float] = None
    recycle_at_usd: Optional[float] = None
    required_tools: tuple = ("Bash", "Read", "Edit", "Write")

    def __post_init__(self) -> None:
        _clean_token("profile name", self.name, limit=_NAME_MAX)
        if self.permission_mode not in PROFILE_MODES:
            raise ValueError(f"permission_mode must be one of {PROFILE_MODES}, not {str(self.permission_mode)[:40]!r}")
        srcs = self.setting_sources
        if (not isinstance(srcs, tuple) or not srcs or len(set(srcs)) != len(srcs)
                or any(s not in PROFILE_SETTING_SOURCES for s in srcs)):
            raise ValueError(f"setting_sources must be a non-empty tuple drawn from {PROFILE_SETTING_SOURCES}, each once")
        if not isinstance(self.hooks, tuple) or not all(isinstance(h, HookSpec) for h in self.hooks):
            raise ValueError("hooks must be a tuple of HookSpec")
        for h in self.hooks:
            h.check()
        if self.system_prompt is not None and (not isinstance(self.system_prompt, str) or not self.system_prompt.strip()
                                               or "\x00" in self.system_prompt):
            raise ValueError("system_prompt must be a non-empty string without a NUL")
        if self.model is not None:
            _clean_token("model", self.model, limit=_NAME_MAX)
            if any(c.isspace() for c in self.model):
                raise ValueError("model must not contain whitespace")
        if self.effort is not None and self.effort not in PROFILE_EFFORTS:
            raise ValueError(f"effort must be one of {PROFILE_EFFORTS}")
        if self.max_turns is not None:
            _int_in("max_turns", self.max_turns, _MAX_TURNS_BOUNDS)
        if self.turn_budget_usd is not None:
            _money("turn_budget_usd", self.turn_budget_usd)
        if self.recycle_at_usd is not None:
            _money("recycle_at_usd", self.recycle_at_usd)
            if self.turn_budget_usd is None:
                raise ValueError("recycle_at_usd needs a turn_budget_usd to sit below")
            if self.recycle_at_usd >= self.turn_budget_usd:
                raise ValueError("recycle_at_usd must be below turn_budget_usd: the recycle is what keeps a turn off the cap")
        tools = self.required_tools
        if (not isinstance(tools, tuple) or not tools or len(set(tools)) != len(tools)
                or not all(isinstance(t, str) and t and t == t.strip() and not _has_control(t) for t in tools)):
            raise ValueError("required_tools must be a non-empty tuple of distinct tool names")

    @property
    def max_budget_usd(self) -> Optional[float]:
        """The value of the vendor's ``--max-budget-usd``: the per-turn budget plus the recycle threshold (the
        process may spend up to the recycle point before a turn that gets a full budget starts). None without a
        ``turn_budget_usd``."""
        if self.turn_budget_usd is None:
            return None
        return round(self.turn_budget_usd + (self.recycle_at_usd or 0.0), 2)


def host_settings_json(profile: RoomProfile, commands: Sequence[str]) -> str:
    """The ``--settings`` JSON of a host-profile room: its hook entries and nothing else. There is NO ``permissions``
    key: no deny rule and no read block belong to this profile (the child holds a shell, so a Read-family rule would
    guard nothing). ``commands`` holds one shell command per :attr:`RoomProfile.hooks` entry, in order, naming the
    COPY of the script made for this launch (see :func:`levain.desk.claude_session.room_hook_command`). No hooks gives ``{}``."""
    if not isinstance(commands, (list, tuple)) or len(commands) != len(profile.hooks):
        raise ValueError("one hook command per hook of the profile, in order")
    entries = []
    for spec, command in zip(profile.hooks, commands):
        if not isinstance(command, str) or not command.strip() or _has_control(command):
            raise ValueError("a hook command must be a non-empty single-line string")
        entries.append({"matcher": spec.matcher, "hooks": [
            {"type": "command", "command": command, "timeout": spec.timeout}]})
    doc: dict[str, Any] = {"hooks": {"PreToolUse": entries}} if entries else {}
    return json.dumps(doc, sort_keys=True)


def host_policy(profile: RoomProfile, *, settings_json: str, hook_sha256: Sequence[str], door: Sequence[str],
                host_commands: Optional[Mapping[str, Any]] = None) -> dict:
    """The policy dict the containment digest hashes for a host-profile spawn (the confined profile's dict is a
    different one, and this function does not touch it). It records everything a spawn ran under that the profile chose: the mode, the
    sources, the settings (hook entries), the sha256 of each hook script's bytes (``hook_sha256``, one per hook, in
    order: the bytes written for THIS launch, never a re-read), the system prompt's sha256, the dials, the one MCP
    server and the door's tool names. ``host_commands`` is the host's command table (parsed JSON), when it gave one."""
    if not isinstance(hook_sha256, (list, tuple)) or len(hook_sha256) != len(profile.hooks):
        raise ValueError("one hook sha256 per hook of the profile, in order")
    for sha in hook_sha256:
        if not _is_sha256(sha):
            raise ValueError("a hook sha256 is 64 lowercase hex characters")
    policy: dict[str, Any] = {
        "profile": profile.name,
        "mode": profile.permission_mode,
        "setting_sources": list(profile.setting_sources),
        "settings": json.loads(settings_json),
        "hooks": [{"matcher": h.matcher, "sha256": sha, "timeout": h.timeout}
                  for h, sha in zip(profile.hooks, hook_sha256)],
        "system_prompt_sha256": (hashlib.sha256(profile.system_prompt.encode("utf-8")).hexdigest()
                                 if profile.system_prompt is not None else None),
        "model": profile.model,
        "effort": profile.effort,
        "max_turns": profile.max_turns,
        "max_budget_usd": profile.max_budget_usd,
        "recycle_at_usd": profile.recycle_at_usd,
        "strict_mcp": True,
        "door": list(door),
    }
    if host_commands is not None:
        policy["host_commands"] = dict(host_commands)
    return policy
