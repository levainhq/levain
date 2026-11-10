"""levain.desk.room_host: the commands a HOST gives the room (1106-16 Room D2).

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
room's words-by-id rule, :func:`levain.desk.room.quoted_words`); each of his messages is relayed once per room
thread (claimed before the command runs: :func:`levain.desk.room.claim_relay`); and the door runs only the argv
the host gave, with ``shell=False``, in its own process group (killed whole on the timeout) and a capped output. The argv
reaches the door through the room's own mcp-config file (0700 run directory, written per launch), not through anything
the model can name.

Stdlib only."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, NamedTuple, Optional

from levain.launch import child_env

__all__ = ["HOSTCMD_ENV", "NOT_STARTED", "OUTPUT_MAX", "TIMED_OUT", "TIMEOUT", "HostCommands", "HostCommandError",
           "RELAY_TOOL", "REPLIES_TOOL", "Ran", "THREAD_RE", "door_tools", "from_env", "run"]

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


class Ran(NamedTuple):
    """What :func:`run` saw. ``started`` is False only when the program never began (nothing it could deliver); a
    command that started and then failed or timed out may have delivered."""
    code: int
    out: str
    started: bool


# the exit codes run() reports for its own outcomes
TIMED_OUT = 124
NOT_STARTED = 127
# [seat] bytes of each stream kept while the rest is read and dropped (OUTPUT_MAX characters, at UTF-8's widest)
_RETAIN = OUTPUT_MAX * 4
_CHUNK = 64 * 1024
# [seat] seconds to reap a killed command's group and readers
_REAP = 2.0


def _drain(stream: Any, keep: bytearray) -> None:
    """Read ``stream`` to its end, keeping its first :data:`_RETAIN` bytes and dropping the rest (a noisy command never
    makes the door hold its whole output)."""
    try:
        read = getattr(stream, "read1", stream.read)
        while True:
            chunk = read(_CHUNK)
            if not chunk:
                return
            room = _RETAIN - len(keep)
            if room > 0:
                keep += chunk[:room]
    except (OSError, ValueError):
        return
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _feed(stream: Any, data: bytes) -> None:
    try:
        stream.write(data)
    except (OSError, ValueError):       # a command that exits without reading its stdin: a broken pipe, not a fault
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):     # the group is already gone
        pass


def run(argv: tuple[str, ...], stdin_text: str = "", extra: tuple[str, ...] = (), *, timeout: float = TIMEOUT) -> Ran:
    """Run the host's command. ``stdin_text`` goes to its STDIN; ``extra`` are fixed-shape arguments appended (an
    already-validated thread id), never free text. The command starts in its OWN session (process group): on a timeout
    the whole group is killed, so a helper it spawned cannot deliver after the door gave up. Each stream is read as it
    comes, keeping a bounded head and dropping the rest. Returns :class:`Ran`: code :data:`TIMED_OUT` for a timeout,
    :data:`NOT_STARTED` (``started`` False) when the program could not be started; output is stdout then stderr, capped
    at :data:`OUTPUT_MAX`."""
    try:
        proc = subprocess.Popen([*argv, *extra], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                shell=False, start_new_session=True,
                                env=child_env())    # the allowlist, never the whole parent environment (test_launch's invariant)
    except (OSError, ValueError) as exc:     # ValueError: an argument Popen will not pass (it never ran either way)
        return Ran(NOT_STARTED, f"the host command could not start ({type(exc).__name__})", False)
    out_b, err_b = bytearray(), bytearray()
    workers = [threading.Thread(target=_feed, args=(proc.stdin, stdin_text.encode("utf-8", "replace")), daemon=True),
               threading.Thread(target=_drain, args=(proc.stdout, out_b), daemon=True),
               threading.Thread(target=_drain, args=(proc.stderr, err_b), daemon=True)]
    for w in workers:
        w.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        timed_out = True
    # a command that exited may have left a descendant holding its pipes: its readers wait only until the deadline
    for w in workers[1:]:
        w.join(max(0.0, deadline - time.monotonic()))
    if timed_out or any(w.is_alive() for w in workers):
        _kill_group(proc.pid)
        try:
            proc.wait(timeout=_REAP)
        except subprocess.TimeoutExpired:
            pass
        for w in workers:
            w.join(_REAP)
    if timed_out:
        return Ran(TIMED_OUT, f"the host command did not finish within {timeout:g} s and was stopped", True)
    code = proc.returncode
    out = out_b.decode("utf-8", "replace").strip()
    err = err_b.decode("utf-8", "replace").strip()
    if code and err:        # a failure's diagnostic is usually on stderr: it keeps room even behind a long stdout
        text = (out[:OUTPUT_MAX // 2] + "\n" + err).strip() if out else err
    else:
        text = out or err
    return Ran(code, text[:OUTPUT_MAX], True)
