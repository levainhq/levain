"""The hands-uid chat: a Claude or Codex chat started AS the entity's own user, reading only the registry's MCP servers.

Not the cockpit room. The room runs as the operator with one door and no hands (Phill 11-01, "Chat A: full mind, no
hands, one door"); nothing here is wired into it or into any other launch path. This is the one function that starts a
hands vendor chat, and it keeps the order the 1107-26 frame set:

1. the entity's session lock (SHARED, as :class:`levain.session.EntitySession` takes it) and its one-session lock
   (EXCLUSIVE, :func:`levain.firing.ws_git.hold_hands_session`). ``prepare_launch(held=True)`` skips prepare's own
   exclusive lock, and the shared lock alone would let two launchers read the registry and commit in either order;
   the one-session lock serialises them.
2. every process the hands account left running is stopped, so nothing of that account can rewrite the launch files
   between the prepare and the start (the files are the hands account's own).
3. :func:`levain.firing.hands_mcp_login.prepare_launch` with ``held=True``: both launch files rewritten from the LIVE
   registry, so a server revoked since the last prepare is absent from both.
4. only when that returned: the vendor, from :func:`~levain.firing.hands_mcp_login.claude_chat_argv` (which passes
   ``--strict-mcp-config``) or :func:`~levain.firing.hands_mcp_login.codex_chat_argv`.

A refusal or a failure at any step before the start (a lock, the stop, the prepare, the spawn itself) starts nothing,
releases both locks and reports. There is no fallback to the files an earlier prepare left: a launch on those is the
L3 HIGH this slice closes (a revoked server stayed usable until a launcher refreshed the files).

What this does not close: a revoke that lands while a chat is running does not reach it (the vendor read its file at
start, and a stdio server is already running). See the frame's last section.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable, Protocol

from levain import hands_live
from levain.firing import hands_login as hl
from levain.firing import hands_mcp_login as hml
from levain.firing.confinement import HandsIdentity
from levain.firing.ws_git import WsGitError, hold_hands_session, hold_session_lock
from levain.launch import child_env

VENDORS = ("claude", "codex")


class HandsChatRefused(RuntimeError):
    """The chat was not started; the message says why."""


class _Proc(Protocol):
    def wait(self) -> int: ...


#: ``spawn(argv, cwd)`` starts the vendor and returns something with ``wait()``; tests pass a fake vendor.
Spawn = Callable[[list[str], str], _Proc]


def _spawn(argv: list[str], cwd: str) -> _Proc:
    """The real start: the operator's terminal handed to the hands chat (stdin, stdout and stderr inherited)."""
    return subprocess.Popen(argv, cwd=cwd, env=child_env())


def chat_argv(hands: HandsIdentity, vendor: str) -> list[str]:
    """The vendor's chat argv with no argument added: an added ``--mcp-config`` (Claude) or ``-c mcp_servers...``
    (Codex) would bring in a server the registry does not hold, so none is passed through."""
    if vendor == "claude":
        return hml.claude_chat_argv(hands)
    if vendor == "codex":
        return hml.codex_chat_argv(hands)
    raise HandsChatRefused(f"unknown vendor {vendor!r}; one of {', '.join(VENDORS)}")


def _wait(proc: _Proc) -> int:
    """The chat's exit code. A Ctrl-C at the terminal reaches the vendor too, which owns it (it clears a prompt, it
    does not end the chat), so it does not end the wait here."""
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            continue


def launch_hands_chat(entity_dir: Path | str, vendor: str, cockpit_root: Path | str | None = None, *,
                      spawn: Spawn | None = None,
                      emit: Callable[[str], None] = print) -> int:
    """Start the hands-uid ``vendor`` chat for ``entity_dir`` and wait for it; its exit code. Raises
    :class:`HandsChatRefused` (nothing started, both locks released) for any refusal or failure before the start."""
    start = spawn if spawn is not None else _spawn
    if vendor not in VENDORS:
        raise HandsChatRefused(f"unknown vendor {vendor!r}; one of {', '.join(VENDORS)}")
    fds: list[int] = []
    try:
        try:
            hands = hl.load_hands_identity(entity_dir)
            fds.append(hold_session_lock(entity_dir))
            fds.append(hold_hands_session(entity_dir))
            stale = hands_live.sweep_hands_user(hands.user, uid=hands.uid)
            if stale is not None:
                raise HandsChatRefused(f"processes of the entity's user could not be stopped ({stale})")
            res = hml.prepare_launch(hands, entity_dir, cockpit_root, held=True)
            argv = chat_argv(hands, vendor)
            for gid, why in res.skipped:
                emit(f"hands chat: registry entry {gid} not rendered ({why})")
            emit(f"hands chat: {vendor} as {hands.user} with {len(res.names)} registered MCP server(s)"
                 + (f" ({', '.join(res.names)})" if res.names else ""))
            proc = start(argv, str(hands.workspace))
        except HandsChatRefused as exc:
            raise HandsChatRefused(f"{exc}; no chat was started") from None
        except (hml.HandsMcpError, hl.HandsLoginError, WsGitError, OSError, subprocess.SubprocessError) as exc:
            # HandsMcpRefused is a HandsMcpError; a partial prepare (one file replaced) lands here too, and the
            # next launch rewrites both from the registry
            raise HandsChatRefused(f"{exc}; no chat was started") from None
        rc = _wait(proc)
        try:
            left = hands_live.sweep_hands_user(hands.user, uid=hands.uid)
        except Exception as exc:  # noqa: BLE001 - reported; the chat's own exit code is the answer
            left = f"the stop failed ({exc})"
        if left is not None:
            emit(f"hands chat: processes of {hands.user} were left after the chat ({left})")
        return rc
    finally:
        for fd in reversed(fds):
            os.close(fd)


def cmd_hands_chat(entity_dir: Path | str, vendor: str, cockpit_root: Path | str | None = None) -> int:
    try:
        return launch_hands_chat(entity_dir, vendor, cockpit_root)
    except HandsChatRefused as exc:
        print(f"hands chat: {exc}")
        return 1
