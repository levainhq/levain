"""levain.coordinator.room_host: the commands a HOST gives the room (1106-16 Room D2).

A host (flow's bridge is the one today) may hand the room up to two commands as room tools. Levain names no script and
no path: the host passes an argv for each, and the room's MCP door runs it. The commands, by role:

* ``relay``: files the operator's words with the host's desk. The text arrives on STDIN, never in argv. Its output's
  ``(thread <id>)`` is the id the room reads replies by.
* ``replies``: reads the desk's replies to one thread. Read-only. The thread id is the only argument the door appends.

**Degrading.** With no host commands the room is exactly what it was: one door tool, ``propose_drop``, and his tap on
the card commits a drop. The proposed-drop path is retired only when ``relay`` is present AND the host set
``relay_without_tap`` (the consent class: his message in the room IS the ask, no second tap; the host says so, levain
never infers it). ``replies`` alone adds a read-only tool and retires nothing.

**What the model cannot do.** The relay tool takes ids of the operator's own messages in the open room, never words (the
room's words-by-id rule, :func:`levain.coordinator.room.quoted_words`); the same quote set is relayed once per room
thread; and the door runs only the argv the host gave, with ``shell=False``, a timeout and a capped output. The argv
reaches the door through the room's own mcp-config file (0700 run directory, written per launch), not through anything
the model can name.

Stdlib only."""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Optional

__all__ = ["HOSTCMD_ENV", "OUTPUT_MAX", "TIMEOUT", "HostCommands", "HostCommandError", "RELAY_TOOL", "REPLIES_TOOL",
           "THREAD_RE", "door_tools", "from_env", "run"]

HOSTCMD_ENV = "LEVAIN_ROOM_HOST_COMMANDS"
# [seat] seconds a host command may run before the door gives up on it
TIMEOUT = 30.0
# [seat] characters of a command's output the door keeps (the rest is dropped, never passed to the model)
OUTPUT_MAX = 4000
_ARGV_MAX = 32
_ARG_MAX = 1024
RELAY_TOOL = "relay_to_desk"
REPLIES_TOOL = "desk_replies"
# the id a relay prints as "(thread <id>)": a conservative id shape, never the host's own format
THREAD_RE = re.compile(r"th-[0-9a-z]{1,16}(?:-[0-9a-f]{1,16})?")


class HostCommandError(ValueError):
    """The host's command configuration is not usable."""


def _check_argv(name: str, argv: Any) -> tuple[str, ...]:
    if isinstance(argv, (str, bytes)) or not isinstance(argv, (list, tuple)) or not 1 <= len(argv) <= _ARGV_MAX:
        raise HostCommandError(f"{name} is a list of 1 to {_ARGV_MAX} strings")
    for a in argv:
        if not isinstance(a, str) or not a or len(a) > _ARG_MAX or "\x00" in a:
            raise HostCommandError(f"{name} holds a non-string, an empty one, a NUL or one over {_ARG_MAX} characters")
    if not os.path.isabs(argv[0]):
        raise HostCommandError(f"{name}'s program must be an absolute path (the room's PATH is not the host's)")
    return tuple(argv)


@dataclass(frozen=True)
class HostCommands:
    """``relay`` and ``replies`` are argvs (program absolute); either may be None. ``relay_without_tap`` is the host's
    word that a relay on the model's call needs no tap (False: ``relay`` is accepted but not offered, the card stays)."""
    relay: Optional[tuple[str, ...]] = None
    replies: Optional[tuple[str, ...]] = None
    relay_without_tap: bool = False

    def __post_init__(self) -> None:
        for name in ("relay", "replies"):
            v = getattr(self, name)
            if v is not None:
                object.__setattr__(self, name, _check_argv(name, v))
        if not isinstance(self.relay_without_tap, bool):
            raise HostCommandError("relay_without_tap is a bool")

    @property
    def relays(self) -> bool:
        """The relay tool is offered, and the proposed-drop tool is retired."""
        return self.relay is not None and self.relay_without_tap

    def to_json(self) -> str:
        return json.dumps({"relay": list(self.relay) if self.relay else None,
                           "replies": list(self.replies) if self.replies else None,
                           "relay_without_tap": self.relay_without_tap}, sort_keys=True)


def door_tools(host: Optional[HostCommands]) -> tuple[str, ...]:
    """The door's tool names (short, as ``tools/list`` names them) for ``host``: ``propose_drop`` unless the relay
    retires it, then the relay, then the replies reader. No host: ``("propose_drop",)``, as before D2."""
    if host is None:
        return ("propose_drop",)
    out = [] if host.relays else ["propose_drop"]
    if host.relays:
        out.append(RELAY_TOOL)
    if host.replies is not None:
        out.append(REPLIES_TOOL)
    return tuple(out)


def from_env(value: Optional[str]) -> Optional[HostCommands]:
    """The door's host commands from its environment value; None when unset/empty. A malformed value raises
    :class:`HostCommandError` (the door refuses to start rather than silently run without the commands it was told)."""
    if not value:
        return None
    try:
        doc = json.loads(value)
    except ValueError:
        raise HostCommandError("the host commands are not JSON") from None
    if not isinstance(doc, dict) or set(doc) - {"relay", "replies", "relay_without_tap"}:
        raise HostCommandError("the host commands are an object of relay, replies, relay_without_tap")
    return HostCommands(doc.get("relay"), doc.get("replies"), doc.get("relay_without_tap", False))


def run(argv: tuple[str, ...], stdin_text: str = "", extra: tuple[str, ...] = (), *, timeout: float = TIMEOUT
        ) -> tuple[int, str]:
    """Run the host's command. ``stdin_text`` goes to its STDIN; ``extra`` are fixed-shape arguments appended (an
    already-validated thread id), never free text. Returns ``(exit code, output)``: code 124 for a timeout, 127 when
    the program cannot be started; output is stdout then stderr, capped at :data:`OUTPUT_MAX`."""
    try:
        done = subprocess.run([*argv, *extra], input=stdin_text, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, shell=False)
    except subprocess.TimeoutExpired:
        return 124, f"the host command did not finish within {timeout:g} s"
    except OSError as exc:
        return 127, f"the host command could not start ({type(exc).__name__})"
    text = (done.stdout or "").strip()
    if done.returncode and done.stderr:
        text = (text + "\n" + done.stderr.strip()).strip()
    elif not text:
        text = (done.stderr or "").strip()
    return done.returncode, text[:OUTPUT_MAX]
