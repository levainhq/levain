"""The hands-uid chat: a Claude or Codex chat started AS the entity's own user, reading only the registry's MCP servers.

Not the cockpit room. The room runs as the operator with one door and no hands (Phill 11-01, "Chat A: full mind, no
hands, one door"); nothing here is wired into it or into any other launch path. This is the one function that starts a
hands vendor chat, and it keeps this order:

1. the entity's session lock (SHARED, as :class:`levain.session.EntitySession` takes it) and its one-session lock
   (EXCLUSIVE, :func:`levain.firing.ws_git.hold_hands_session`). ``prepare_launch(held=True)`` skips prepare's own
   exclusive lock, and the shared lock alone would let two launchers read the registry and commit in either order;
   the one-session lock serialises them.
2. every process the hands account left running is stopped, so nothing of that account can rewrite the launch files
   between the prepare and the start (the files are the hands account's own).
3. :func:`levain.firing.hands_mcp_login.prepare_launch` with ``held=True``: both launch files rewritten from the LIVE
   registry, so a server revoked since the last prepare is absent from both.
4. only when that returned, and a re-read of the authority log still holds every entry the files carry (a revoke is
   a log append that takes no hands lock, :func:`~levain.firing.hands_mcp_login.launch_still_live`): the vendor, from :func:`~levain.firing.hands_mcp_login.claude_chat_argv` (which passes
   ``--strict-mcp-config``) or :func:`~levain.firing.hands_mcp_login.codex_chat_argv`.

A refusal or a failure at any step before the start (a lock, the stop, the prepare, the spawn itself) starts nothing,
releases both locks and reports. There is no fallback to the files an earlier prepare left: a launch on those is the
L3 HIGH this slice closes (a revoked server stayed usable until a launcher refreshed the files).

Three bounds the 1107-26 L3 round 1 added: the launch is macOS only in this slice (the Linux start program execs the
bare vendor name with no PATH search, from the hands workspace as cwd), so any other host is refused before a lock is
taken; a project layer in the hands workspace that the launched vendor may read (a ``.mcp.json``; for Codex also a
``.codex/`` config file or ``.agents/skills``; for Claude a ``.claude/`` holding agents, plugins or settings; names
compared case-folded, :func:`project_layer`) refuses the launch, since the vendor would read servers from it
that the registry does not hold; and from the moment the locks are
taken, a SIGTERM or SIGHUP ends in an orderly stop (the child terminated, the hands account swept, both locks released,
then re-raised as an exit with ``128 + signal``), with processes left after the chat making the command exit non-zero.

The vendor never holds the operator's terminal (leg 6 fix, L2 HIGH 2): it runs on a pty the launcher owns, in a
session of its own, and the launcher relays the bytes (:class:`_PtyChat`); the operator's terminal modes are put back
and its input flushed after the final sweep, so the hands uid cannot queue keystrokes for the operator's shell.

A hands-MCP revoke ends a chat that is starting or running (the 457 (c) ruling). The wait is a watched wait: every
``WATCH_S`` it re-reads the authority log, and an entry the launch files carry that is gone ends the chat now (the
vendor stopped, the hands account swept, both locks released), with one line on the terminal, a cockpit note, and
``HANDS_REVOKED_EXIT``. Each read has a deadline (``READ_DEADLINE_S``), and a log that cannot be read ends the chat
within 30 s by the monotonic clock of the first read that could not confirm it (``UNREADABLE_S``, :func:`_wait`). When
processes of the hands account are left after that stop, the line and the note say so rather than that the chat
ended. There is no grace and the agent is not told: a grace in which it was told would leave the revoked hands usable.
"""
from __future__ import annotations

import fcntl
import json
import os
import platform
import re
import select
import signal
import subprocess
import termios
import threading
import time
import tty
from pathlib import Path
from typing import Callable, Protocol

from levain import hands_live
from levain.consent import notes as notes_mod
from levain.firing import hands_login as hl
from levain.firing import hands_mcp_login as hml
from levain.firing.confinement import HandsIdentity
from levain.firing.ws_git import WsGitError, hold_hands_session, hold_session_lock
from levain.launch import child_env

VENDORS = ("claude", "codex")


class HandsChatRefused(RuntimeError):
    """The chat was not started; the message says why."""


class HandsChatInterrupted(HandsChatRefused):
    """A Ctrl-C before the start: nothing was started, both locks released; the command exits 130."""


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

#: The settle window before the operator's terminal input is flushed at the chat's end (:meth:`_PtyChat._settle`):
#: the input must be quiet this long, and the window lasts at most ``SETTLE_MAX_S``. A local terminal answers a query
#: in a few ms; the window is for a terminal across ssh. A reply later than the window is a known edge.
SETTLE_S = 0.3
SETTLE_MAX_S = 1.5

#: Seconds between the watch's reads of the authority log while the chat runs.
WATCH_S = 5
#: How long one read of the authority log may take before it counts as unreadable (it runs in a worker thread; a
#: hung read no longer holds the wait, leg 6 fix, L2 MED 6).
READ_DEADLINE_S = 5.0
#: Fail closed on consent that stays unknown: the chat ends at the first unconfirmed read that ends this long (by
#: ``time.monotonic``) after the start of the first read that could not confirm the grants. With the read's own
#: deadline the chat ends within ``UNREADABLE_S + READ_DEADLINE_S`` = 30 s of that start. A confirmed read starts
#: the window again: it re-read the whole log, so a revoke cannot hide behind a flapping log (the read that confirms
#: is the read that would have seen the revoke).
UNREADABLE_S = 25.0
#: The clock the window and the watch run on; tests pass a fake one.
_now: Callable[[], float] = time.monotonic
#: The exit code of a chat a revoke ended (EX_NOPERM).
HANDS_REVOKED_EXIT = 77


class _Revoked(Exception):
    """Raised out of the watched wait to take the chat's normal stop-and-sweep path; ``reason`` is for the operator."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def host_refusal() -> str | None:
    """Why this host cannot launch a hands chat, or None. On Linux the start program ``os.execv``s the bare vendor
    name with the hands workspace as cwd (no PATH search), so a file planted there would run as the vendor."""
    return None if platform.system() == "Darwin" else MACOS_ONLY


def vendor_missing(vendor: str) -> str | None:
    """Why ``vendor`` cannot start as the hands account, or None (leg 6 fix, L2 MED 4): the hands prefix runs it as
    a bare name under ``env -i PATH=``:data:`~levain.firing.confinement.HANDS_PATH`, so one installed only in the
    operator's home (the Claude native installer's ``~/.local/bin``) ended the launch in a bare ``env: claude: No
    such file or directory`` after the locks, the sweep and the prepare. Looked up as the operator, on that PATH and
    nothing else: a binary the operator cannot see there the hands account cannot either."""
    import shutil
    from levain.firing import confinement
    if shutil.which(vendor, path=confinement.HANDS_PATH) is not None:
        return None
    return (f"{vendor} is not on the hands account's PATH ({confinement.HANDS_PATH}); install {vendor} where the "
            "hands account can run it, under /usr/local/bin or /opt/homebrew/bin (the VM harness's stage-vendors "
            "puts claude and codex there)")


def project_layer(workspace: Path, vendor: str) -> Path | None:
    """A file in the hands workspace that ``vendor`` may read as a project layer, or None.

    Both vendors: a ``.mcp.json``. Claude's project file (``--strict-mcp-config`` ignores it; refused anyway), and
    Codex's binary names ``.mcp.json`` in its plugin discovery, unmeasured whether a workspace can be a plugin root,
    so it stays refused for Codex too rather than assumed unread.
    Codex only: a ``.codex/`` that is a link or holds a config file (the L3 r1 codex finding: Codex loads
    ``.codex/config.toml``, its ``[mcp_servers]`` included, for a workspace the hands home marks trusted). A ``.codex``
    link is refused as such, without following it. Names compare case-folded: APFS is case-insensitive by default, so
    a ``.codex/CONFIG.TOML`` is what Codex opens as ``config.toml`` (L3 r2 N1). Claude does not read ``.codex/``, so a
    repository carrying Codex project config does not refuse a Claude chat (L3 r2).

    Leg 6 fix (L1 MED 2, L2 LOW 8): each vendor's own project layer beside that. Claude only: a ``.claude/`` that is
    a link or holds ``agents`` (an agent's frontmatter can carry ``mcpServers``), ``plugins``, or a ``settings*``
    file (``enabledPlugins``, ``extraKnownMarketplaces``, ``enableAllProjectMcpServers``). Measured on claude 2.1.325
    (scratch home, unauthenticated ``-p --debug``): ``--strict-mcp-config`` does drop a ``.mcp.json`` the project
    settings auto-approve, and a plugin enabled only by repository settings was not loaded with or without it; an
    agent's servers start only when it runs (unmeasured). Strict is not relied on alone: these are refused as
    ``.mcp.json`` is. Codex only: a ``.agents`` that is a link or holds ``skills`` (a skill can declare an MCP
    dependency; ``skill_mcp_dependency_install`` is also off, :data:`~levain.firing.hands_mcp_login.CODEX_DISABLED_FEATURES`).
    Only the workspace itself is checked, not its parents (operator- or root-owned, L2 LOW 9).

    Leg 6 L3 r1 (codex HIGH, "Codex loads a workspace ``config.toml`` for trusted dirs"), measured on codex-cli
    0.162.1 with a scratch ``CODEX_HOME`` trusting the workspace, the chat's ``--disable`` flags, marker-writing
    server commands: ``codex exec`` (no login) started the CODEX_HOME server and the one in ``.codex/config.toml``
    (refused above) and NOT one in the workspace's own ``config.toml``; ``codex mcp list`` agrees, and lists none
    from ``codex.toml``, ``.codex.toml`` or ``managed_config.toml`` either. So the workspace root's ``config.toml``
    is not a project layer on this version and is not refused. From a subdirectory under a ``.git`` root Codex
    reads the root's ``.codex/``; the chat's cwd is the workspace itself, so that is the path checked here."""
    mcp = workspace / ".mcp.json"
    if os.path.lexists(mcp):
        return mcp
    if vendor == "claude":
        return _layer_dir(workspace / ".claude", lambda f: f in ("agents", "plugins") or f.startswith("settings"))
    if vendor != "codex":
        return None
    found = _layer_dir(workspace / ".codex", lambda f: f.startswith("config") or f.endswith((".toml", ".json")))
    return found if found is not None else _layer_dir(workspace / ".agents", lambda f: f == "skills")


#: Paths in the hands account's Claude config home (``CLAUDE_CONFIG_DIR``) that can bring MCP servers the registry does
#: not hold (leg 6 L3 r1, complement MED). Measured on claude 2.1.325 with a scratch home, no login (a fake key to a
#: dead local URL), marker-writing server commands: ``--strict-mcp-config`` drops a user-scope ``mcpServers`` in
#: ``.claude.json`` (started with no flag, not with strict); a plugin agent's ``mcpServers`` is ignored by claude
#: itself ("ignored for plugin agents"); a user-installed plugin's ``.mcp.json`` server is listed by ``claude mcp
#: list`` but was not started by a ``-p`` or a TUI chat even with no flag on this host, so what strict does to it is
#: unmeasured; a user agent's ``mcpServers`` start only when that agent runs, which needs a model (unmeasured). The
#: unmeasured ones are refused, not left to strict. A plain claude run makes ``plugins/store`` only, so that is not one.
#:
#: ``plugins/marketplaces`` is what Claude Code's official-marketplace autoinstall writes. The chat's environment now asks
#: Claude Code not to run that autoinstall (:data:`levain.firing.confinement.HANDS_NO_MARKETPLACE_AUTOINSTALL`), so this
#: refusal is the second line, not the first; a home an earlier build's chat already wrote still holds the tree, is still
#: refused by name, and needs the operator to remove it.
CLAUDE_USER_LAYER = ("agents", "plugins/installed_plugins.json", "plugins/marketplaces")
_CLAUDE_SETTINGS_KEYS = ("enabledPlugins", "extraKnownMarketplaces", "mcpServers")


def claude_user_layer(hands: HandsIdentity, entity_dir: Path | str) -> str | None:
    """A user-level agent, plugin or plugin setting in the hands account's Claude config home, or None. Read as the
    hands user (:func:`levain.firing.hands_login._lstat`, :func:`~levain.firing.hands_login.conf_read`), under the
    launch's locks; a read that fails raises, never reads as absent.

    ``settings.json`` is parsed, and only its top-level keys count (leg 6 L3 r2, complement MED, codex): a substring
    test missed a key written with JSON escapes, which Claude's parser reads as that key, and refused a value that
    merely held the text. A file that does not parse as one JSON object is refused by name, never read as no keys."""
    d = hl.config_home(hands, "claude")
    for rel in CLAUDE_USER_LAYER:
        if hl._lstat(hands, "claude", f"{d}/{rel}", entity_dir=entity_dir, held=True) is not None:
            return f"{d}/{rel}"
    got = hl.conf_read(hands, "claude", d, "settings.json", entity_dir=entity_dir)
    if got is None:
        return None
    try:
        settings = json.loads(got[2])
    except (ValueError, RecursionError):
        settings = None
    if not isinstance(settings, dict):
        raise HandsChatRefused(f"the hands account's Claude settings file {d}/settings.json is not one JSON object, "
                               "so whether it enables a plugin or names an MCP server cannot be checked; fix or "
                               "remove it")
    if any(k in settings for k in _CLAUDE_SETTINGS_KEYS):
        return f"{d}/settings.json"
    return None


def _layer_dir(d: Path, read: Callable[[str], bool]) -> Path | None:
    """``d`` when it is a link (refused as such, never followed), else its first entry whose case-folded name the
    vendor reads (``read``), else None (absent, not a directory, or nothing read there)."""
    if d.is_symlink():
        return d
    if not d.is_dir():
        return None
    for name in sorted(os.listdir(d)):
        if read(name.casefold()):
            return d / name
    return None


def _take_ctty() -> None:
    """In the child, after ``start_new_session``'s ``setsid``: its stdin (the pty slave) becomes the new session's
    controlling terminal, so the vendor's TUI has a ``/dev/tty`` and a Ctrl-C reaches its foreground group."""
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


class _PtyChat:
    """The vendor on a pty the launcher owns, in a session of its own (leg 6 fix, L2 HIGH 2).

    With the operator's terminal as its stdio and ``!use_pty`` in the sudoers rule, the vendor and every tool it ran
    would sit in the operator's session with the operator's tty as their controlling terminal. Measured on macOS
    27.0.1 with a pty pair the probe owned: such a process can ``ioctl(TIOCSTI)`` bytes into the tty's input queue,
    which the operator's shell reads after the chat; a process in another session that holds an fd to the tty gets
    ``EACCES``. So the hands uid never holds an fd of the operator's terminal: the child gets the slave of a new pty
    as its stdio and controlling terminal in a new session, and a thread relays bytes between the operator's
    terminal (put in raw mode, so its keys, Ctrl-C included, go to the vendor's pty) and the pty master.
    :meth:`close` stops the relay, waits out a settle window (``SETTLE_S``) discarding input, puts the operator's
    terminal modes back with ``TCSAFLUSH`` and flushes its input queue (through stdout's fd when stdin is not the
    terminal), so a terminal reply the chat provoked is not left for the shell. Closing the master hangs up the pty's
    session. ``wait``, ``terminate`` and ``kill`` are the ``Popen``'s."""

    def __init__(self, argv: list[str], cwd: str, env: dict[str, str] | None = None, *,
                 tty_in: int = 0, tty_out: int = 1) -> None:
        self._in, self._out = tty_in, tty_out
        self._tty = os.isatty(tty_in)
        self._saved: list | None = None
        # Leg 6 L3 r3 (deepseek LOW): two flags. ``_restore``: setraw was attempted, so close() puts the saved modes
        # back (harmless when nothing changed). ``_raw``: setraw returned, so the chat ran raw and close() also settles
        # and flushes the terminal's input; an interrupt inside setraw never discards what the operator typed ahead.
        self._restore = False
        self._raw = False
        self._winch_before: object = None
        self._closed = False
        self._master = self._wake_r = self._wake_w = -1
        self._thread: threading.Thread | None = None
        self.proc: subprocess.Popen | None = None
        # Leg 6 L3 r1 (four lineages): the construction is one transaction. Any failure from here on (an error, an
        # interrupt, Thread.start or the SIGWINCH install failing) stops the child it started, puts the operator's
        # terminal modes back, restores SIGWINCH and closes every fd it opened before the exception goes on: the
        # caller holds no handle until this returns, so nothing else could undo it.
        try:
            self._master, slave = os.openpty()
            try:
                self._winsize(slave)
                self.proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave,
                                             start_new_session=True, preexec_fn=_take_ctty)
            finally:
                os.close(slave)     # held by the child alone, so the master reads EOF once every holder ended
            self._wake_r, self._wake_w = os.pipe()
            if self._tty:
                try:
                    self._saved = termios.tcgetattr(tty_in)
                    # set before the call: an interrupt that lands once setraw changed the modes still restores them
                    self._restore = True
                    tty.setraw(tty_in, termios.TCSANOW)
                    # set after it returns: before this line no chat byte was relayed (the relay starts below)
                    self._raw = True
                except termios.error:
                    pass                # a failed setraw: the saved modes are put back, typed-ahead input kept
            if hasattr(signal, "SIGWINCH"):
                try:
                    self._winch_before = signal.signal(signal.SIGWINCH, lambda s, f: self._winsize(self._master))
                except ValueError:  # not the main thread: the size is set once, at the start
                    self._winch_before = None
            self._thread = threading.Thread(target=self._relay, name="hands-chat-pty", daemon=True)
            self._thread.start()
        except BaseException:
            self._undo()
            raise

    def _undo(self) -> None:
        """A construction that failed part way: the child stopped (as :func:`_stop` does), then :meth:`close`, which
        runs even when the stop raised (leg 6 L3 r2, complement LOW, deepseek MED); the stop's error then goes on."""
        if self._thread is not None and not self._thread.is_alive() and self._thread.ident is None:
            self._thread = None     # created, never started: nothing to wake or join
        try:
            if self.proc is not None:
                _stop(self.proc)
        finally:
            self.close()

    def _out_tty(self) -> bool:
        try:
            return self._out >= 0 and os.isatty(self._out)
        except OSError:
            return False

    @staticmethod
    def _settle(fd: int) -> None:
        """Leg 6 L3 r1 (complement MED, deepseek MED): the chat's last output can be a terminal query, and the
        terminal's reply can land after the relay stopped and after an immediate flush, where the shell reads it as
        typed input. Read and discard ``fd``'s input until it has been quiet for ``SETTLE_S`` (at most
        ``SETTLE_MAX_S`` in all), while the terminal is still raw; the flush follows. Never raises."""
        end = time.monotonic() + SETTLE_MAX_S
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            try:
                ready, _, _ = select.select([fd], [], [], min(SETTLE_S, left))
                if not ready or not os.read(fd, 4096):
                    return
            except (OSError, ValueError):
                return

    def _winsize(self, fd: int) -> None:
        if not self._tty:
            return
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, fcntl.ioctl(self._in, termios.TIOCGWINSZ, b"\0" * 8))
        except OSError:
            pass

    def _relay(self) -> None:
        reading = [self._master, self._wake_r]
        try:
            os.fstat(self._in)
            reading.append(self._in)
        except OSError:                     # no stdin at all: the screen is still relayed
            pass
        out_ok = True
        while True:
            try:
                ready, _, _ = select.select(reading, [], [])
            except (OSError, ValueError):
                return
            if self._wake_r in ready:
                return
            if self._master in ready:
                try:
                    data = os.read(self._master, 65536)
                except OSError:             # EIO: every holder of the slave ended
                    data = b""
                if not data:
                    return
                if out_ok:
                    try:
                        _write_all(self._out, data)
                    except OSError:         # a closed stdout: the chat's screen is dropped, the chat is not stopped
                        out_ok = False
            if self._in in ready:
                try:
                    data = os.read(self._in, 65536)
                except OSError:
                    data = b""
                if not data:
                    reading.remove(self._in)
                    continue
                try:
                    _write_all(self._master, data)
                except OSError:
                    return

    def wait(self, timeout: float | None = None) -> int:
        rc = self.proc.wait(timeout)
        self._thread.join(timeout=1.0)      # the last screen bytes, once every holder of the slave ended
        return rc

    def terminate(self) -> None:
        self.proc.terminate()

    def kill(self) -> None:
        self.proc.kill()

    def close(self) -> None:
        """Stop the relay, put the operator's terminal back and flush its input; idempotent. Raises nothing of its
        own; an interrupt (``KeyboardInterrupt``, ``SystemExit``) during the relay join or the settle window goes on,
        but only after the terminal modes are put back and every fd is closed, in ``finally`` blocks no earlier
        step can skip (leg 6 L3 r2, complement LOW). The settle and the flush of stdin run only when ``tty.setraw``
        returned (leg 6 L3 r2 complement/gemini/codex LOW, r3 deepseek LOW): a setraw that failed or was interrupted
        gets its saved modes back with nothing flushed. A redirected stdin with the terminal on stdout still waits
        ``SETTLE_S`` and flushes that terminal's input."""
        if self._closed:
            return
        self._closed = True
        try:
            try:
                if self._thread is not None:
                    try:
                        os.write(self._wake_w, b"x")
                    except OSError:
                        pass
                    self._thread.join(timeout=2.0)
            finally:
                if self._winch_before is not None:
                    try:
                        signal.signal(signal.SIGWINCH, self._winch_before)  # type: ignore[arg-type]
                    except (ValueError, TypeError):
                        pass
            if self._raw:
                self._settle(self._in)
            elif not self._tty and self._out_tty():
                # stdin redirected, the screen still the operator's terminal: a reply to a query the chat wrote lands
                # on that terminal's input all the same (gemini LOW); it is flushed through stdout's fd after the window
                time.sleep(SETTLE_S)
                try:
                    termios.tcflush(self._out, termios.TCIFLUSH)
                except termios.error:
                    pass
        finally:
            try:
                if self._raw:
                    try:
                        if self._saved is not None:
                            termios.tcsetattr(self._in, termios.TCSAFLUSH, self._saved)
                        termios.tcflush(self._in, termios.TCIFLUSH)
                    except termios.error:
                        pass
                elif self._restore and self._saved is not None:
                    try:    # setraw attempted, never returned: the modes back, no flush (TCSANOW discards nothing)
                        termios.tcsetattr(self._in, termios.TCSANOW, self._saved)
                    except termios.error:
                        pass
            finally:
                for fd in (self._master, self._wake_r, self._wake_w):
                    if fd < 0:
                        continue
                    try:
                        os.close(fd)
                    except OSError:
                        pass


def _spawn(argv: list[str], cwd: str) -> _Proc:
    """The real start: the vendor on a pty of the launcher's, in a session of its own (:class:`_PtyChat`); the
    operator's terminal is relayed, never handed over."""
    return _PtyChat(argv, cwd, child_env())


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


def _wait(proc: _Proc, check: Callable[[], tuple[str, str] | None] | None = None) -> int:
    """The chat's exit code. A Ctrl-C belongs to the vendor (it clears a prompt, it does not end the chat): with the
    operator's terminal in raw mode it is relayed as a byte to the vendor's pty (:class:`_PtyChat`) and raises nothing
    here; one that does arrive as a SIGINT (a terminal not in raw mode) does not end the wait, nor the read below.

    With ``check`` (the liveness read, :func:`levain.firing.hands_mcp_login.launch_check`) the wait wakes every
    ``WATCH_S``: a ``gone`` answer raises :class:`_Revoked` at once; an ``unreadable`` one only once the grants have
    gone unconfirmed for ``UNREADABLE_S`` by the clock (:data:`UNREADABLE_S`: within 30 s in all, the read's deadline
    included), and a readable answer starts that window again. The read runs in a worker thread with
    ``READ_DEADLINE_S``: one that does not finish in time is ``unreadable``, and while it still runs no second read
    is started (each later tick is ``unreadable`` too); a result that arrives late is dropped, never taken as live.
    Any exception out of the read counts as ``unreadable`` (its type named in the reason), never as live; a
    ``SystemExit`` from a stop signal goes on to the orderly stop.

    The read is due every ``WATCH_S`` by the clock, not by a wait that ran its full length, so Ctrl-C pressed faster
    than ``WATCH_S`` cannot starve it; a read a Ctrl-C cut short is due again at once (L3 1108-23). Before a revoke
    ends the chat the vendor is polled: one that already exited on its own gives its own code, with no line and no
    note, since the revoke ended nothing (L3 1108-23, codex MED)."""
    if check is None:
        while True:
            try:
                return proc.wait()
            except KeyboardInterrupt:
                continue
    reader = _BoundedRead(check)
    since: float | None = None          # the start of the first read that could not confirm the grants
    due = _now() + WATCH_S
    while True:
        try:
            try:
                return proc.wait(timeout=max(0.0, due - _now()))
            except subprocess.TimeoutExpired:
                pass
            started = _now()
            moved = reader.read()
            reason: str | None = None
            if moved is None:
                since = None
            elif moved[0] == hml.GONE:
                reason = hml.gone_words(moved)
            else:
                since = started if since is None else since
                if _now() - since >= UNREADABLE_S:
                    reason = f"the hands grants could not be confirmed ({moved[1]})"
            if reason is not None:
                try:
                    return proc.wait(timeout=0)     # it exited on its own during the read: its code stands
                except subprocess.TimeoutExpired:
                    raise _Revoked(reason) from None
            due = _now() + WATCH_S
            if since is not None:               # the window's end is a read of its own, never a tick past it
                due = min(due, since + UNREADABLE_S)
        except KeyboardInterrupt:       # the vendor's Ctrl-C: the wait goes on, a cut-short read is due at once
            continue


class _BoundedRead:
    """``check()`` in a worker thread, joined for at most ``READ_DEADLINE_S``. Its answer, or ``(UNREADABLE, why)``
    for an exception, a read past its deadline, or one still running from an earlier tick; a result that arrives
    after its deadline is dropped. A ``KeyboardInterrupt`` or ``SystemExit`` the check raised is raised here, on the
    caller's thread, as if the read had run there.

    A ``KeyboardInterrupt`` in the join itself (leg 6 L3 r1, deepseek LOW, gemini MED) leaves the read, its box and
    its deadline in place: the next call joins that same read for what is left of its deadline (by
    ``time.monotonic``, the clock ``join`` runs on) and takes its answer, rather than calling a read cut short at once
    one past its deadline and dropping what it found."""

    def __init__(self, check: Callable[[], tuple[str, str] | None]) -> None:
        self.check = check
        self.thread: threading.Thread | None = None
        self.box: list = []
        self.deadline = 0.0             # time.monotonic() by which self.thread must have answered
        self.late = False               # self.thread ran past its deadline: its answer is dropped

    def _run(self, box: list) -> None:
        try:
            got: tuple = ("ok", self.check())
        except BaseException as exc:    # noqa: BLE001 - carried to the caller's thread
            got = ("raised", exc)
        box.append((*got, time.monotonic()))     # when it answered: one past its deadline is dropped

    def read(self) -> tuple[str, str] | None:
        if self.thread is not None and self.late:
            if self.thread.is_alive():
                return hml.UNREADABLE, f"the previous read has not finished after {READ_DEADLINE_S:g} s"
            self.thread = None          # a late answer is dropped, never taken as live
        if self.thread is None:
            self.box, self.late = [], False
            self.deadline = time.monotonic() + READ_DEADLINE_S
            self.thread = threading.Thread(target=self._run, args=(self.box,), name="hands-chat-watch",
                                           daemon=True)
            self.thread.start()
        self.thread.join(max(0.0, self.deadline - time.monotonic()))   # an interrupt here keeps all of the above
        if self.thread.is_alive():          # its late answer lands in a box nothing reads again
            self.late = True
            return hml.UNREADABLE, f"the read did not finish within {READ_DEADLINE_S:g} s"
        self.thread = None
        kind, value, at = self.box[0]
        if at > self.deadline:
            # leg 6 L3 r2 (codex MED): after an interrupted join ``late`` was never set, so an answer that came after
            # the deadline, before this retry, was taken as live; the worker's own stamp settles it
            return hml.UNREADABLE, f"the read did not finish within {READ_DEADLINE_S:g} s"
        if kind == "ok":
            return value
        if isinstance(value, (KeyboardInterrupt, SystemExit)):
            raise value
        return hml.UNREADABLE, f"{type(value).__name__}: {value}"


class _Signals:
    """SIGTERM and SIGHUP handlers for the duration of a launch. While the chat runs, the first one raises
    ``SystemExit(128 + signal)`` so the launcher's cleanup runs; once cleanup has begun (:meth:`quiet`) every stop
    signal and SIGINT are recorded and ignored, so a second one cannot cut the sweep short. :meth:`restore` puts back
    what was installed before."""

    def __init__(self) -> None:
        self.stopping = False
        self.deferring = False
        self.interrupted = False
        self.pending: int | None = None
        self.before: dict[int, object] = {}
        for s in (*_STOP_SIGNALS, signal.SIGINT):
            self.before[s] = signal.getsignal(s)
        for s in _STOP_SIGNALS:
            signal.signal(s, self._on_stop)

    def _on_stop(self, signum: int, frame: object) -> None:
        if self.stopping:
            return
        if self.deferring:              # inside the spawn: recorded, raised once the child handle is held
            if self.pending is None:
                self.pending = signum
            return
        self.stopping = True
        raise SystemExit(128 + signum)

    def spawn(self, start: Spawn, argv: list[str], cwd: str) -> _Proc:
        """``start(argv, cwd)`` with the stop signals deferred until it returned (L3 r3 G3, codex HIGH): a SIGTERM
        that landed after ``Popen`` forked sudo but before it returned left the cleanup with no child handle, so the
        launcher could exit while sudo went on to start the vendor. The signal is recorded here and the caller raises
        it (:meth:`raise_pending`) only after it has stored the returned handle, so the cleanup can stop the child.
        Deferred in the handler, never by ``pthread_sigmask``: a blocked mask is inherited across fork and exec, so
        sudo and the vendor would start with the stop signals blocked. A spawn that itself fails raises its own error,
        and the cleanup's sweep still runs. On a return the deferral stays on until :meth:`raise_pending`, so a signal
        between this return and the caller's store of the handle is deferred too.

        SIGINT is deferred the same way (leg 6 L3 r1, gemini HIGH): a Ctrl-C landing after the fork and before the
        handle was stored left the forked child unstopped (only the sweep reached it). It is raised as
        ``KeyboardInterrupt`` by :meth:`raise_pending`, once the handle is held, and the cleanup stops the child."""
        self.deferring = True
        self.interrupted = False
        try:
            signal.signal(signal.SIGINT, self._on_int)
        except ValueError:
            pass
        try:
            return start(argv, cwd)
        except BaseException:
            self.deferring = False
            self._int_back()
            raise

    def _on_int(self, signum: int, frame: object) -> None:
        if self.deferring and not self.stopping:
            self.interrupted = True
            return
        raise KeyboardInterrupt

    def _int_back(self) -> None:
        if self.stopping:
            return              # quiet() owns SIGINT now
        try:
            before = self.before[signal.SIGINT]
            signal.signal(signal.SIGINT, signal.SIG_DFL if before is None else before)  # type: ignore[arg-type]
        except (ValueError, TypeError):
            pass

    def raise_pending(self) -> None:
        """End the deferral :meth:`spawn` began and raise the stop signal (or the Ctrl-C) it recorded, if one came."""
        self.deferring = False
        self._int_back()
        if self.pending is not None and not self.stopping:
            self.stopping = True
            raise SystemExit(128 + self.pending)
        if getattr(self, "interrupted", False) and not self.stopping:
            self.interrupted = False
            raise KeyboardInterrupt

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
    processes of the hands account were left after it, or ``HANDS_REVOKED_EXIT`` (returned, not raised) when a
    revoke, or a log that stayed unreadable, ended it. Raises :class:`HandsChatRefused` (nothing started, both locks
    released) for any refusal or failure before the start, and ``SystemExit(128 + signal)`` after an orderly stop on
    SIGTERM or SIGHUP."""
    start = spawn if spawn is not None else _spawn
    refusal = host_refusal()
    if refusal is not None:
        raise HandsChatRefused(f"{refusal}; no chat was started")
    if vendor not in VENDORS:
        raise HandsChatRefused(f"unknown vendor {vendor!r}; one of {', '.join(VENDORS)}")
    missing = vendor_missing(vendor)
    if missing is not None:
        raise HandsChatRefused(f"{missing}; no chat was started")
    try:
        sigs = _Signals()
    except ValueError as exc:   # not the main thread: no handler, so no orderly stop; refuse rather than run without
        raise HandsChatRefused(f"the stop signals cannot be handled here ({exc}); no chat was started") from None
    fds: list[int] = []
    hands: HandsIdentity | None = None
    proc: _Proc | None = None
    attempted = False           # set just before the spawn: from then on the hands account may be running a chat
    survivors = False
    left: str | None = None
    revoked: str | None = None
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
            user = claude_user_layer(hands, entity_dir) if vendor == "claude" else None
            if user is not None:
                raise HandsChatRefused(f"the hands account's Claude config home holds a user-level agent, plugin or "
                                       f"plugin setting ({user}), which can bring MCP servers the registry does not "
                                       "hold; remove it")
            argv = chat_argv(hands, vendor)
            moved = hml.launch_still_live(res)          # L3 r3 G5: a revoke takes no hands lock; the last read
            if moved is not None:
                raise HandsChatRefused(moved)
            for gid, why in res.skipped:
                emit(f"hands chat: registry entry {gid} not rendered ({why})")
            emit(f"hands chat: {vendor} as {hands.user} with {len(res.names)} registered MCP server(s)"
                 + (f" ({', '.join(res.names)})" if res.names else ""))
            attempted = True
            proc = sigs.spawn(start, argv, str(hands.workspace))
            sigs.raise_pending()        # only now: ``proc`` is held, so the cleanup stops the child
        except HandsChatRefused as exc:
            raise HandsChatRefused(f"{exc}; no chat was started") from None
        except KeyboardInterrupt:
            # L1 LOW 7: SIGINT is the vendor's only once the chat runs; one before (the lock, the sweep, the prepare)
            # is the operator stopping the launch. With the vendor already held, the outer cleanup stops it.
            if proc is not None:
                raise
            if attempted:       # inside the spawn: sudo may have forked; the final sweep below stops what it started
                raise HandsChatInterrupted("interrupted (Ctrl-C) during the start; the entity's user is swept "
                                           "before this command exits") from None
            raise HandsChatInterrupted("interrupted (Ctrl-C) before the start; no chat was started") from None
        except (hml.HandsMcpError, hl.HandsLoginError, WsGitError, OSError, subprocess.SubprocessError) as exc:
            # HandsMcpRefused is a HandsMcpError; a partial prepare (one file replaced) lands here too, and the
            # next launch rewrites both from the registry
            raise HandsChatRefused(f"{exc}; no chat was started") from None
        rc = _wait(proc, lambda: hml.launch_check(res))
    except _Revoked as exc:
        sigs.quiet()
        revoked = exc.reason
        # "end now" (457 (c)): the hands account's processes are swept FIRST, so a vendor that traps SIGTERM does not
        # keep the revoked servers through _stop's graceful wait; sudo then exits with its child. Best effort: the
        # sweep in the finally is the one whose result is reported (L3 1108-23).
        if hands is not None:
            try:
                hands_live.sweep_hands_user(hands.user, uid=hands.uid)
            except Exception:  # noqa: BLE001 - the final sweep reports
                pass
        if proc is not None:        # always held here (raised only from the wait); the guard is for the type
            _stop(proc)
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
            if revoked is not None and left is not None and hands is not None:
                left = _retry_revoked_sweep(hands, left)
            _close_terminal(proc)       # after the sweep: nothing of the hands account writes to it any more
            if attempted and hands is not None:
                if left is not None:
                    survivors = True
                    if revoked is None:     # a revoke's one line names them instead (_report_revoked)
                        try:
                            emit(f"hands chat: {_left_after(hands.user, left)}")
                        except Exception:  # noqa: BLE001 - a closed stdout must not replace the outcome (L3 r2 N2)
                            pass
        finally:
            sigs.restore()
            for fd in reversed(fds):
                os.close(fd)
    if revoked is not None:
        _report_revoked(revoked, vendor, emit,
                        None if left is None or hands is None else _still_up(hands, left))
        return HANDS_REVOKED_EXIT
    return (rc or 1) if survivors else rc


def _close_terminal(proc: _Proc | None) -> None:
    """The operator's terminal back from the chat's pty (:meth:`_PtyChat.close`: modes restored, input flushed);
    a fake vendor has no ``close``. Never raises: the outcome stands."""
    close = getattr(proc, "close", None)
    if close is not None:
        try:
            close()
        except Exception:  # noqa: BLE001
            pass


def _left_after(user: str, left: str) -> str:
    """The words for processes of the hands account the final sweep could not stop, on either path."""
    return f"processes of {user} were left after the chat ({left})"


#: After a revoke, a final sweep that left processes is retried this many times, with both locks still held, before
#: the launcher gives up (leg 6 L3 r1, complement MED): the revoked vendor must not be left running the moment the
#: locks go. The locks are flocks, so they end with this process whatever it does; past that, the next launch's own
#: first sweep refuses to start while any process of the hands account is left.
_REVOKE_SWEEP_RETRIES = 3
_REVOKE_SWEEP_BACKOFF_S = 1.0


def _retry_revoked_sweep(hands: HandsIdentity, left: str) -> str | None:
    """Sweep again, ``_REVOKE_SWEEP_RETRIES`` times at most, ``_REVOKE_SWEEP_BACKOFF_S`` apart: None once a sweep
    left nothing, else the last sweep's words."""
    for _ in range(_REVOKE_SWEEP_RETRIES):
        time.sleep(_REVOKE_SWEEP_BACKOFF_S)
        try:
            again = hands_live.sweep_hands_user(hands.user, uid=hands.uid)
        except Exception as exc:  # noqa: BLE001 - reported
            again = f"the stop failed ({exc})"
        if again is None:
            return None
        left = again
    return left


def _hands_pids(uid: int | None) -> list[str]:
    """The pids running as ``uid`` (``pgrep -U``), best effort and bounded: [] when they cannot be read."""
    if uid is None:
        return []
    try:
        r = hands_live.run_bounded([hands_live._secure("pgrep"), "-U", str(uid)], env=hands_live._child_env(),
                                   timeout=2.0)
        return r.stdout.decode("utf-8", "replace").split()[:20]
    except Exception:  # noqa: BLE001 - the line names what it can
        return []


def _still_up(hands: HandsIdentity, left: str) -> str:
    """The words, after a revoke, for a hands account that survived every sweep: it is still up, with its pids."""
    pids = _hands_pids(hands.uid)
    named = f"pid {', '.join(pids)}" if pids else "pids could not be read"
    return (f"the revoked chat is STILL UP: processes of {hands.user} ({named}) survived "
            f"{1 + _REVOKE_SWEEP_RETRIES} sweeps ({left}); stop them by hand: sudo -u {hands.user} /bin/kill -9 -- -1")


#: The note store's agent name for each vendor. A vendor not here is passed through as itself, so the store refuses
#: it rather than the note being filed under another vendor's name.
_NOTE_AGENT = {"claude": "claude_code", "codex": "codex"}


def _note_text(text: str) -> tuple[str, str | None]:
    """The note's ``what`` and ``detail`` for ``text``. The store refuses a ``what`` that is not one line within
    ``WHAT_MAX``, and a sweep's or a read error's words can be either; such a text keeps its whole form in ``detail``
    and a one-line cut of it as ``what``, so the note still files."""
    one = " ".join(text.split())
    if one == text and len(one) <= notes_mod.WHAT_MAX:
        return text, None
    cut = one if len(one) <= notes_mod.WHAT_MAX else one[:notes_mod.WHAT_MAX - 1] + "…"
    return cut, text[:notes_mod.DETAIL_MAX]


def _report_revoked(reason: str, vendor: str, emit: Callable[[str], None], survivors: str | None = None) -> None:
    """The one terminal line and the cockpit note, with the same words. ``survivors``, the sweep's words for what it
    left running, replaces the claim that the chat was ended. Each is a courtesy that must not replace the outcome: a
    failed note or a closed stdout leaves the exit code as it is."""
    tail = "this chat was ended" if survivors is None else survivors
    line = f"hands revoked: {reason}; {tail}"
    try:
        emit(f"hands chat: {line}")
    except Exception:  # noqa: BLE001
        pass
    what, detail = _note_text(f"hands chat: {line}")
    try:
        notes_mod.NoteStore().file(agent=_NOTE_AGENT.get(vendor, vendor),
                                   agent_session_id=notes_mod.NO_SESSION, what=what, detail=detail,
                                   cwd=os.getcwd())
    except Exception:  # noqa: BLE001 - the line above already told the terminal
        pass


def cmd_hands_chat(entity_dir: Path | str, vendor: str, cockpit_root: Path | str | None = None) -> int:
    try:
        return launch_hands_chat(entity_dir, vendor, cockpit_root)
    except HandsChatInterrupted as exc:
        print(f"hands chat: {exc}")
        return 130
    except HandsChatRefused as exc:
        print(f"hands chat: {exc}")
        return 1
    except KeyboardInterrupt:       # the instant between the spawn and the wait: the cleanup stopped and swept
        print("hands chat: interrupted (Ctrl-C); the chat was stopped")
        return 130
