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

Three bounds the 1107-26 L3 round 1 added: the launch is macOS only in this slice (the Linux start program execs the
bare vendor name with no PATH search, from the hands workspace as cwd), so any other host is refused before a lock is
taken; a project layer in the hands workspace that the launched vendor may read (a ``.mcp.json``; for Codex also a
``.codex/`` config file, names compared case-folded) refuses the launch, since the vendor would read servers from it
that the registry does not hold; and from the moment the locks are
taken, a SIGTERM or SIGHUP ends in an orderly stop (the child terminated, the hands account swept, both locks released,
then re-raised as an exit with ``128 + signal``), with processes left after the chat making the command exit non-zero.

What this does not close: a revoke that lands while a chat is running does not reach it (the vendor read its file at
start, and a stdio server is already running). See the frame's last section and its Known edges.
"""
from __future__ import annotations

import os
import platform
import re
import signal
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
    def wait(self, timeout: float | None = None) -> int: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...


#: ``spawn(argv, cwd)`` starts the vendor and returns something with ``wait()``, ``terminate()`` and ``kill()``
#: (a ``Popen``); tests pass a fake vendor.
Spawn = Callable[[list[str], str], _Proc]

MACOS_ONLY = "the hands chat launch is macOS only in this slice"

#: The signals that end the chat in an orderly stop rather than killing the launcher past its ``finally``. Built by
#: name so the module imports where one is absent (Windows has no SIGHUP); the launch itself is macOS only.
_STOP_SIGNALS = tuple(getattr(signal, n) for n in ("SIGTERM", "SIGHUP") if hasattr(signal, n))

#: A terminal type the operator's ``TERM`` may pass on as: a terminfo name, never a path (terminfo looks the name up
#: as a file under its directories, so a ``/`` or a leading ``.`` is refused and the default stands).
_TERM_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")

#: How long the child gets to end after ``terminate`` before ``kill``; the sweep after it is the backstop.
_STOP_TIMEOUT = 5.0


def host_refusal() -> str | None:
    """Why this host cannot launch a hands chat, or None. On Linux the start program ``os.execv``s the bare vendor
    name with the hands workspace as cwd (no PATH search), so a file planted there would run as the vendor."""
    return None if platform.system() == "Darwin" else MACOS_ONLY


def project_layer(workspace: Path, vendor: str) -> Path | None:
    """A file in the hands workspace that ``vendor`` may read as a project layer, or None.

    Both vendors: a ``.mcp.json``. Claude's project file (``--strict-mcp-config`` ignores it; refused anyway), and
    Codex's binary names ``.mcp.json`` in its plugin discovery, unmeasured whether a workspace can be a plugin root,
    so it stays refused for Codex too rather than assumed unread.
    Codex only: a ``.codex/`` that is a link or holds a config file (the L3 r1 codex finding: Codex loads
    ``.codex/config.toml``, its ``[mcp_servers]`` included, for a workspace the hands home marks trusted). A ``.codex``
    link is refused as such, without following it. Names compare case-folded: APFS is case-insensitive by default, so
    a ``.codex/CONFIG.TOML`` is what Codex opens as ``config.toml`` (L3 r2 N1). Claude does not read ``.codex/``, so a
    repository carrying Codex project config does not refuse a Claude chat (L3 r2)."""
    mcp = workspace / ".mcp.json"
    if os.path.lexists(mcp):
        return mcp
    if vendor != "codex":
        return None
    codex = workspace / ".codex"
    if codex.is_symlink():
        return codex
    if not codex.is_dir():
        return None
    for name in sorted(os.listdir(codex)):
        folded = name.casefold()
        if folded.startswith("config") or folded.endswith((".toml", ".json")):
            return codex / name
    return None


def _spawn(argv: list[str], cwd: str) -> _Proc:
    """The real start: the operator's terminal handed to the hands chat (stdin, stdout and stderr inherited)."""
    return subprocess.Popen(argv, cwd=cwd, env=child_env())


def chat_argv(hands: HandsIdentity, vendor: str) -> list[str]:
    """The vendor's chat argv with no argument added: an added ``--mcp-config`` (Claude) or ``-c mcp_servers...``
    (Codex) would bring in a server the registry does not hold, so none is passed through."""
    if vendor == "claude":
        return interactive_term(hml.claude_chat_argv(hands))
    if vendor == "codex":
        return interactive_term(hml.codex_chat_argv(hands))
    raise HandsChatRefused(f"unknown vendor {vendor!r}; one of {', '.join(VENDORS)}")


def interactive_term(argv: list[str], term: str | None = None) -> list[str]:
    """``argv`` with the hands prefix's ``TERM=dumb`` (set for non-interactive bash) replaced by the operator's terminal
    type, since this chat is the operator's terminal handed over: under ``TERM=dumb`` Codex stops at "Continue anyway?
    [y/N]" before its TUI (its binary's own text, codex-cli 0.162.1). Only a terminfo name passes (``_TERM_NAME``);
    anything else, or no ``TERM``, leaves ``dumb``. Only the ``env -i`` assignments are touched, never an argument."""
    term = os.environ.get("TERM", "") if term is None else term
    if not _TERM_NAME.fullmatch(term) or term == "dumb":
        return argv
    out = list(argv)
    try:
        i = next(k for k in range(len(out) - 1) if out[k] == "/usr/bin/env" and out[k + 1] == "-i") + 2
    except StopIteration:
        return out
    while i < len(out) and "=" in out[i]:
        if out[i] == "TERM=dumb":
            out[i] = f"TERM={term}"
            break
        i += 1
    return out


def _wait(proc: _Proc) -> int:
    """The chat's exit code. A Ctrl-C at the terminal reaches the vendor too, which owns it (it clears a prompt, it
    does not end the chat), so it does not end the wait here."""
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            continue


class _Signals:
    """SIGTERM and SIGHUP handlers for the duration of a launch. While the chat runs, the first one raises
    ``SystemExit(128 + signal)`` so the launcher's cleanup runs; once cleanup has begun (:meth:`quiet`) every stop
    signal and SIGINT are recorded and ignored, so a second one cannot cut the sweep short. :meth:`restore` puts back
    what was installed before."""

    def __init__(self) -> None:
        self.stopping = False
        self.before: dict[int, object] = {}
        for s in (*_STOP_SIGNALS, signal.SIGINT):
            self.before[s] = signal.getsignal(s)
        for s in _STOP_SIGNALS:
            signal.signal(s, self._on_stop)

    def _on_stop(self, signum: int, frame: object) -> None:
        if not self.stopping:
            self.stopping = True
            raise SystemExit(128 + signum)

    def _ignore(self, signum: int, frame: object) -> None:
        pass

    def quiet(self) -> None:
        self.stopping = True
        for s in (*_STOP_SIGNALS, signal.SIGINT):
            signal.signal(s, self._ignore)

    def restore(self) -> None:
        for s, h in self.before.items():
            # None: a handler installed outside Python, which cannot be put back from here; the default stands in
            signal.signal(s, signal.SIG_DFL if h is None else h)  # type: ignore[arg-type]


def _stop(proc: _Proc) -> None:
    """Best effort: terminate the child, then kill it if it has not ended. The child is sudo, and signalling it can
    fail (it runs as root); the sweep that follows is what stops the hands account's processes either way."""
    try:
        proc.terminate()
        try:
            proc.wait(timeout=_STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=_STOP_TIMEOUT)
    except Exception:  # noqa: BLE001 - the sweep is the backstop
        pass


def launch_hands_chat(entity_dir: Path | str, vendor: str, cockpit_root: Path | str | None = None, *,
                      spawn: Spawn | None = None,
                      emit: Callable[[str], None] = print) -> int:
    """Start the hands-uid ``vendor`` chat for ``entity_dir`` and wait for it; its exit code, or non-zero when
    processes of the hands account were left after it. Raises :class:`HandsChatRefused` (nothing started, both locks
    released) for any refusal or failure before the start, and ``SystemExit(128 + signal)`` after an orderly stop on
    SIGTERM or SIGHUP."""
    start = spawn if spawn is not None else _spawn
    refusal = host_refusal()
    if refusal is not None:
        raise HandsChatRefused(f"{refusal}; no chat was started")
    if vendor not in VENDORS:
        raise HandsChatRefused(f"unknown vendor {vendor!r}; one of {', '.join(VENDORS)}")
    try:
        sigs = _Signals()
    except ValueError as exc:   # not the main thread: no handler, so no orderly stop; refuse rather than run without
        raise HandsChatRefused(f"the stop signals cannot be handled here ({exc}); no chat was started") from None
    fds: list[int] = []
    hands: HandsIdentity | None = None
    proc: _Proc | None = None
    attempted = False           # set just before the spawn: from then on the hands account may be running a chat
    survivors = False
    try:
        try:
            hands = hl.load_hands_identity(entity_dir)
            fds.append(hold_session_lock(entity_dir))
            fds.append(hold_hands_session(entity_dir))
            stale = hands_live.sweep_hands_user(hands.user, uid=hands.uid)
            if stale is not None:
                raise HandsChatRefused(f"processes of the entity's user could not be stopped ({stale})")
            res = hml.prepare_launch(hands, entity_dir, cockpit_root, held=True)
            layer = project_layer(Path(hands.workspace), vendor)
            if layer is not None:
                raise HandsChatRefused(f"the hands workspace holds a project layer the vendor would read ({layer}), "
                                       "which can name MCP servers the registry does not hold; remove it")
            argv = chat_argv(hands, vendor)
            for gid, why in res.skipped:
                emit(f"hands chat: registry entry {gid} not rendered ({why})")
            emit(f"hands chat: {vendor} as {hands.user} with {len(res.names)} registered MCP server(s)"
                 + (f" ({', '.join(res.names)})" if res.names else ""))
            attempted = True
            proc = start(argv, str(hands.workspace))
        except HandsChatRefused as exc:
            raise HandsChatRefused(f"{exc}; no chat was started") from None
        except (hml.HandsMcpError, hl.HandsLoginError, WsGitError, OSError, subprocess.SubprocessError) as exc:
            # HandsMcpRefused is a HandsMcpError; a partial prepare (one file replaced) lands here too, and the
            # next launch rewrites both from the registry
            raise HandsChatRefused(f"{exc}; no chat was started") from None
        rc = _wait(proc)
    except BaseException:
        sigs.quiet()
        if proc is not None:
            _stop(proc)
        raise
    finally:
        sigs.quiet()
        try:
            if attempted and hands is not None:
                try:
                    left = hands_live.sweep_hands_user(hands.user, uid=hands.uid)
                except Exception as exc:  # noqa: BLE001 - reported, and the command exits non-zero
                    left = f"the stop failed ({exc})"
                if left is not None:
                    survivors = True
                    try:
                        emit(f"hands chat: processes of {hands.user} were left after the chat ({left})")
                    except Exception:  # noqa: BLE001 - a closed stdout must not replace the outcome (L3 r2 N2)
                        pass
        finally:
            sigs.restore()
            for fd in reversed(fds):
                os.close(fd)
    return (rc or 1) if survivors else rc


def cmd_hands_chat(entity_dir: Path | str, vendor: str, cockpit_root: Path | str | None = None) -> int:
    try:
        return launch_hands_chat(entity_dir, vendor, cockpit_root)
    except HandsChatRefused as exc:
        print(f"hands chat: {exc}")
        return 1
