"""levain.consent.contained — the call a core result is bound to, and the contained launch's capability.

Two values the chat core passes beside an event (CH-D1 §3.4):

- :class:`CoreCall` names ONE vendor call (its session, call id, tool, input digest, directory and the launch's
  spawn nonce). ``decide_ex`` returns every result carrying the ``CoreCall`` it computed from ITS event; the service
  computes its own from the approval row it built the event from, and a result whose call digest differs is
  another call's result (a core bug), never delivered. A hook caller's call has ``launch_nonce`` ``""``.
- :class:`ContainedLaunchProof` is the SH1 capability: the launch, its spawn nonce, the containment CLASS digest a
  contained-shell grant binds, the canary verdict and the launch directory. It is a same-uid, in-process consistency
  value, never a signature: the signed fact is the grant's ``containment_digest``. A hook caller passes ``None``.

:func:`plain_dns_host` is the one "is this a plain DNS name" test (rev 9, R9-7): the chat core declines a
``SandboxNetworkAccess`` ask for anything else, and the egress proxy (CH-D2) uses the same function.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from levain.consent.record import tool_input_digest

__all__ = ["ContainedLaunchProof", "CoreCall", "core_call_of", "plain_dns_host"]

_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_LABEL = re.compile(r"[a-z0-9-]{1,63}")


@dataclass(frozen=True)
class ContainedLaunchProof:
    launch_id: str             # dispatch.db launches.launch_id
    spawn_nonce: str           # 32 lower hex: the launch profile's spawn nonce, = the activation row's
    profile_digest: str        # 64 lower hex: the profile's containment_digest (the CLASS a grant binds)
    canary_verdict_id: str     # 64 lower hex: the activation row's canary verdict
    cwd: str                   # realpath of the launch directory

    def problem(self) -> str | None:
        """Why this proof is not of the right SHAPE, or ``None``. Shape only: it reads no store."""
        if not isinstance(self.launch_id, str) or not self.launch_id:
            return "no launch id"
        if not isinstance(self.spawn_nonce, str) or not _HEX32.fullmatch(self.spawn_nonce):
            return "the spawn nonce is not 32 lower hex"
        if not isinstance(self.profile_digest, str) or not _HEX64.fullmatch(self.profile_digest):
            return "the profile digest is not 64 lower hex"
        if not isinstance(self.canary_verdict_id, str) or not _HEX64.fullmatch(self.canary_verdict_id):
            return "the canary verdict id is not 64 lower hex"
        if not isinstance(self.cwd, str) or not os.path.isabs(self.cwd) or "\x00" in self.cwd:
            return "the launch directory is not absolute"
        try:
            if os.path.realpath(self.cwd) != self.cwd:
                return "the launch directory is not its own real path"
        except (OSError, ValueError):
            return "the launch directory cannot be resolved"
        return None


@dataclass(frozen=True)
class CoreCall:
    session: str               # the agent session id exactly as decide derives it (session_id, + "#agent:<id>")
    call_id: str               # the event's tool_use_id; "" only for a hook caller's event that carries none
    tool_name: str
    input_digest: str          # levain.consent.record.tool_input_digest(tool_input)
    cwd: str
    launch_nonce: str          # the spawn nonce for a chat launch; "" for every hook caller

    def digest(self) -> str:
        """sha256 (hex) of the canonical JSON of the six fields: sorted keys, compact separators."""
        text = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


def core_call_of(event: Mapping[str, Any], *, launch_nonce: str) -> CoreCall:
    """The :class:`CoreCall` of a PermissionRequest ``event``. Pure: it reads only ``event``. Raises
    :class:`ValueError` when the event cannot name a call: no session, tool name or cwd (non-empty strings), an
    ``agent_id`` or ``tool_use_id`` that is not a string, or, for a chat call (``launch_nonce`` not ``""``), no
    ``tool_use_id``; an input :func:`tool_input_digest` cannot digest raises what it raises."""
    if not isinstance(launch_nonce, str):
        raise ValueError("the launch nonce is not a string")
    if not isinstance(event, Mapping):
        raise ValueError("the event is not an object")
    session, tool_name, cwd = event.get("session_id"), event.get("tool_name"), event.get("cwd")
    for name, value in (("session_id", session), ("tool_name", tool_name), ("cwd", cwd)):
        if not isinstance(value, str) or not value:
            raise ValueError(f"the event has no {name}")
    agent_id = event.get("agent_id")
    if isinstance(agent_id, str) and agent_id:
        session = f"{session}#agent:{agent_id}"
    elif agent_id is not None and not isinstance(agent_id, str):
        raise ValueError("the event's agent_id is not a string")
    call_id = event.get("tool_use_id")
    if call_id is None:
        call_id = ""
    if not isinstance(call_id, str):
        raise ValueError("the event's tool_use_id is not a string")
    if launch_nonce and not call_id:
        raise ValueError("a chat call has no tool_use_id")
    tool_input = event.get("tool_input", {})
    if tool_input is None:
        tool_input = {}  # an MCP tool with no arguments arrives without tool_input, as decide reads it
    return CoreCall(session=session, call_id=call_id, tool_name=tool_name, input_digest=tool_input_digest(tool_input),
                    cwd=cwd, launch_nonce=launch_nonce)


def plain_dns_host(host: Any) -> str | None:
    """``host`` normalised as a plain DNS name, or ``None`` (= decline). Lower-cased, ONE trailing ``.`` stripped;
    ``None`` when: not a string; empty or longer than 253 characters; any ``[`` ``]`` ``:`` ``%`` ``/`` ``@`` or
    whitespace; any label empty, longer than 63 characters or outside ``[a-z0-9-]``; a label starting or ending with
    ``-``; the final label all digits or starting ``0x``; the name ``localhost`` or under ``.localhost``; fewer than
    two labels. Never resolves anything: where a plain name RESOLVES is the egress proxy's check (CH-D2)."""
    if not isinstance(host, str):
        return None
    name = host.lower()
    if name.endswith("."):
        name = name[:-1]
    if not name or len(name) > 253 or any(c in name for c in "[]:%/@") or any(c.isspace() for c in name):
        return None
    labels = name.split(".")
    if len(labels) < 2:
        return None
    for label in labels:
        if not _LABEL.fullmatch(label) or label.startswith("-") or label.endswith("-"):
            return None
    last = labels[-1]
    if last.isdigit() or last.startswith("0x"):
        return None
    if labels[-1] == "localhost":
        return None
    return name
