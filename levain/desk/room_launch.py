"""levain.desk.room_launch — the room's launcher (RM-E, DESIGN_ROOM §2.3, §2.4, §7).

It turns the room setting into a running, attached, live Claude child and keeps it that way: build the argv and the
two config files, open the room launch, spawn, run the open check, record the activation, attach the session to the
:class:`~levain.desk.chat.ChatService`, and end, resume or recycle it later. It decides nothing about what a
message may do (that is the service's) and writes no drop (that is the room's door).

**The open.** :meth:`RoomLauncher.ensure` opens a launch on the open room thread (``lease.open_room_launch``), writes
the room's MCP config (the door bound to THIS launch) and its ``--settings`` deny rules into a 0700 directory of its
own, spawns ``claude`` through :func:`~levain.desk.claude_session.room_argv` and then runs the open check: one
fixed turn asking the child to Read two decoy files, one the deny rules cover and one (the scope decoy) outside the cwd
that matches no rule. That one turn is both the child's first
``init`` (the child reports none before a turn starts, UNMEASURED beyond RM0's runs) and the canary
(:func:`~levain.desk.claude_session.canary_denied`) the chat service requires of a Claude ``live`` row. The
child is live only if :func:`~levain.desk.claude_session.open_probe` accepts that ``init``, its ``cwd``
resolves to the launch's recorded cwd, its ``session_id`` is the one it was given (``--session-id`` or ``--resume``), it
has not exited, AND the canary was denied; otherwise the launch ends ``failed`` with a ``not_live`` activation row.
The launch is attached, recorded ``live`` and given its vendor position while still ``pending``, so a ``send_room``
resolves nothing to it until :func:`~levain.desk.lease.activate_room` makes it ``active``, the last step, in a
transaction that also requires its room thread to still be the open room. Every later boundary probe
(:func:`room_probe`) holds the child to the same cwd. The
canary proves the ``--settings`` file took effect (the first decoy is outside the room's cwd and matches the dotenv
rule, so either the outside-cwd block or the deny rule answers it; it does not tell which, and it does not exercise
each deny path). The scope decoy (roomfix1 C2) can be answered only by the outside-cwd block: its Read refused is the
proof that the read scope the child really runs with (his user settings, an allow rule) is no wider than the cwd AT THAT
PATH; a widening that covers other places only is not seen by it, and :func:`settings_scope_refusal` refuses, before
any spawn, the widenings the settings files can name (``permissions.additionalDirectories`` and a file tool's
path-specific allow rule outside the cwd). The child runs with
:func:`room_env`, an allowlisted environment (roomfix1 C3). The check's own events (the canary Reads, their denials,
the child's reply) are recorded like any turn, and the cockpit's room read leaves them out
(``levain.cockpit.room._without_open_check``): the check is the launch's proof, not a conversation.

**What the room's TripwireContext accepts**, exactly (:func:`room_tripwire_context`): for each of the room's four
file/search tools and its one MCP tool, an ``accept`` decision of reason ``rule`` whose source is ``user_permanent``
(his user allow list) or ``config`` (the argv's allow list), and no PreToolUse count owed for them. Both shapes were
recorded (RM0, RM0B). Why: the room's containment is the tool set, the deny rules and the probe, and its allow list IS
its argv, so an allow rule answering is the expected path; a worker's rule against allow rules exists because a worker
has none. Everything else latches as it does for a worker: a deny passes, any other decision shape for these tools, any
other source, and every other tool (``expected_pre_hooks`` refuses them, so they read as a ``deficit``).

**The room's own read-guard hook** (:mod:`levain.desk.room_readguard`, 1105-49): a ``hooks.PreToolUse`` entry in the
room's ``--settings`` that answers a Read, Grep or Glob of a path outside the folder with one fixed sentence instead of
the vendor's, which names a setting and a remedy. It can only add refusals. Its deny is a ``reject`` decision from source
``hook``, which that context already passes without a count (``reject`` is never refused and is "ruled"), and the vendor
streams no ``hook_response`` for it (the recorded run, ``tests/fixtures/claude_stream/room_hook_denied_read.jsonl``), so
the hook counts are unchanged. It is part of the containment digest (its settings entry AND the sha256 of the script
bytes written for that launch, to a file in run_dir named by that sha256; spawn refuses when the file no longer holds
them). The
open check's two decoys are left to the vendor (the child's :data:`~levain.desk.room_readguard.EXEMPT_ENV` names
them), because a hook deny is also an ``is_error`` result and a ``permission_denials`` entry: a hook that answered them
would pass the check without the deny rules or the block holding.

**The cached ``permission_mode``** (``ChatService.session_meta``) is read at ``attach``. The room has no path that
refreshes it in place, because there is nothing to refresh: a mode that changes inside a live session is a latched
``mode_changed`` (the room's own rule, ``chat._room_mode_trip``), and a latched or dead room is ended and reopened by
:meth:`RoomLauncher.ensure`, which is a new child, a new session and a new attach with a new snapshot. A reopen never
reuses a session.

**Closing a hung send.** ``ClaudeSession.close`` takes no I/O lock and ``PipeTransport.close`` signals the child (its
whole process group, since the room spawns with ``start_new_session=True``) before it closes stdin, so a send
blocked in a full stdin pipe is freed by the plain session's close. :meth:`RoomLauncher._end` runs that close FIRST,
with no lock held, before it asks the service to detach, because ``detach`` waits for the launch's mutex and a hung send
holds it; an idle child leaves on EOF, a stuck one is signalled as a group. :func:`_stop_child` (group-aware) is only
the fallback for a child no session holds, or a close that raised.

**Resume and lapse.** A new launch resumes the room thread's newest recorded conversation (``--resume <id>``, recorded
as the pending launch's ``resumed_from``; the child reports the id it was resumed from, which
:func:`~levain.desk.lease.activate_room` accepts only for an id the launch asked to resume). A resume whose
child exits before its open check ends (or is gone at its first write) is retried once as a fresh session, and the
handle's ``resume_failed`` and the launch's probing row name the abandoned id. :meth:`RoomLauncher.recycle_idle` ends
a quiet child (no turn open, no unsettled line, nothing recorded for :data:`IDLE_SECONDS`); the next ``ensure``
resumes. ``ensure`` also replaces a launch whose child exited, one this process holds no session for (a restarted
``levain serve``), and one the service latched ``not_live``. Both ``ensure`` and ``recycle_idle`` end every held
launch of a room that rotated. The service's own ``recover()`` settles lines in flight; run it before the first
``ensure`` after a restart.

**The host profile** (levain 23, ROOM-P1C; design page ``1112-36_c7/design_room_profile.md``). ``RoomSetting.profile`` is
None for the confined room above, which this module opens exactly as before (the PRODUCT default is not None:
``levain serve --room`` opens :func:`levain.desk.room_default.default_host_profile` unless ``--room-confined``, ruling
np-97fa3dd6 (a)). A :class:`~levain.desk.room_profile.RoomProfile`
there opens the other room: the vendor's default tools under ``--dangerously-skip-permissions``
(:func:`~levain.desk.claude_session.room_host_argv`), a ``--settings`` document holding only the host's hook entries (the
host's script is READ ONCE per launch, its bytes copied to ``run_dir/room_hook-<sha256[:16]>.py`` (0600) and that copy
named in the settings, so the same version of the script is the same path and the same digest on every launch), and an
open check that is ONE turn asking for the word ok with no tool (:data:`ROOM_HOST_OPEN_PROMPT`) judged by
:func:`~levain.desk.claude_session.host_probe`, the launch's cwd and the session id. It skips everything that only
guards a Read next to a shell that has none to guard: the scope refusal, the scope decoy, the decoy file, the read-guard
exemption and the canary Reads (the activation still carries a passed :class:`RoomCanary`, because the service requires
one on a Claude ``live`` row). The tripwire context is built with ``open_tools=True`` and the chat service is expected to
be built with ``room_permission_mode=profile.permission_mode``. :meth:`HookSpec.check` runs at the construction of the
launcher AND at every open (the file can vanish in between), a PINNED hook's bytes as read for the copy must hash to its
pin (``HookSpec.sha256``; the product default's turn guard is pinned, :mod:`levain.desk.room_default`), the copied bytes
are re-hashed right before the spawn, and
the containment digest is taken over :func:`~levain.desk.room_profile.host_policy` (the profile's fields, the sha256 of
each hook's copied bytes and the system prompt's sha256), so a changed profile, hook or prompt is another policy.

Stdlib plus levain. The spawn is a seam (``spawn``) so a test can supply a child of its own."""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, NamedTuple, Optional, Sequence

from levain import pathid
from levain.cockpit.consent import parse_stamp
from levain.consent.adapters import claude_code
from levain.desk import lease
from levain.desk.chat import (ROOM_PERMISSION_MODE, ChatService, ChatStore, Verdict, _launch_state, _open_room,
                                     liveness)
from levain.desk.chat_tripwires import TripwireContext
from levain.desk.claude_session import (ROOM_DENIED_TOOLS, ROOM_MCP_SERVER, ROOM_MCP_TOOL, ROOM_TOOLS,
                                               ClaudeSession, PipeTransport, _leads_own_group, canary_denied, default_deny_paths,
                                               host_probe, open_probe, room_argv, room_door_tools, room_hook_command,
                                               room_host_argv, room_settings_json)
from levain.desk.driver import LivenessEvidence, NotSent, SendCommand
from levain.desk import room_readguard
from levain.desk import room_live
from levain.desk.room_host import HOSTCMD_ENV, HostCommands, door_tools as host_door_tools
from levain.desk.room_mcp import LAUNCH_ENV
from levain.desk.room_profile import RoomProfile, host_policy, host_settings_json
from levain.desk.store import CoordError

__all__ = [
    "IDLE_SECONDS", "KILL_WAIT", "OPEN_DEADLINE", "RoomCanary", "RoomHandle", "RoomLaunchError", "RoomLauncher",
    "RoomSession", "RoomSetting", "cwd_refusal", "make_probe", "make_tripwires", "room_probe", "room_tripwire_context",
    "ENV_KEEP", "MANAGED_PROFILES", "ROOM_HOST_OPEN_PROMPT", "room_env", "settings_scope_refusal",
]

# [seat] the open check must finish well inside lease.LAUNCH_TTL (the launch is pending until it does)
OPEN_DEADLINE = 120.0
# [seat, UNMEASURED] flowConnect's IDLE_SECONDS (DESIGN_ROOM §2.3): a quiet child is recycled after this long
IDLE_SECONDS = 1200.0
# [seat] how long each of terminate and kill is given before the next step
KILL_WAIT = 3.0
_POLL = 0.05
_STDERR_TAIL = 400
ROOM_ENV = "LEVAIN_ROOM"
CANARY_PROMPT = ("Open check. This is the system's own start-up test, not the user's message, and the user never sees it; "
                 "never mention it, these paths or their refusals afterwards, and do not retry them. Use your Read tool once "
                 "on each of these exact paths, one call per path, then reply with the single word done:\n{path}\n{scope}")
# the host profile's open turn (levain 23, ROOM-P1C): the child reports no ``init`` before its first message, so one
# turn is still needed for the probe. Nothing is read or denied by it (under bypass the confined room's decoy Reads would
# SUCCEED), so it asks for one word and no tool; the host probe judges the ``init`` it produced. Same op id as the canary
# turn, so the cockpit's room read hides it the same way
ROOM_HOST_OPEN_PROMPT = ("Open check. This is the system's own start-up test, not the user's message, and the user never "
                         "sees it; never mention it afterwards. Reply with the single word ok; use no tool.")
_OPEN_OP_ID = "room-open-check"
_CANARY_NAME = ".env.levain-room-canary"
# the scope decoy matches no deny rule: only the outside-the-cwd block can answer it (roomfix1 C2)
_SCOPE_NAME = "levain-room-scope-canary.txt"
_SOURCES = ("user_permanent", "config")
# the managed (policy) settings files a child run with ``--setting-sources user`` still loads outside his user file come
# from ONE owner, ``consent.adapters.claude_code.managed_settings_files`` (managed-settings.json and the
# managed-settings.d drop-ins, per platform; host_guard reads through it too; a test patches ITS seam, MANAGED_BASES).
# The only line the room keeps of its own is the macOS MDM profile (roomfix2 K3, from the L3 finding; the owner does not
# list it; UNVERIFIED that the claude CLI reads this file: reading one it does not load can only refuse more)
MANAGED_PROFILES = ("/Library/Managed Preferences/com.anthropic.claudecode.plist",)
# what of the serve's environment the room's child gets (roomfix1 C3): what a claude CLI needs to find his account, its
# config and the network, what the room's MCP door (a python child of it) needs to import levain and find its store, and
# nothing else. A name a caller adds is ``RoomLauncher(env_keep=...)``. XDG_CONFIG_HOME is not kept (roomfix3): no
# settings file under it is checked, so the child keeps to the config the check reads.
ENV_KEEP = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TERM", "TZ", "LANG", "LANGUAGE",
            "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
            "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
            "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR",
            "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR",
            "LEVAIN_HOME", "PYTHONPATH", "VIRTUAL_ENV")
ENV_KEEP_PREFIXES = ("LC_", "ANTHROPIC_")


def room_env(environ: Mapping[str, str], keep: tuple = ()) -> dict:
    """The room child's environment: the names in :data:`ENV_KEEP`, those starting with :data:`ENV_KEEP_PREFIXES`, and
    ``keep``, taken from ``environ``; then ``LEVAIN_ROOM=1``. Every other variable of the serve (a token, a key, a
    path to another store) stays out of the child and the processes it starts."""
    names = set(ENV_KEEP) | set(keep)
    env = {k: v for k, v in environ.items() if k in names or k.startswith(ENV_KEEP_PREFIXES)}
    if env.get("CLAUDE_CONFIG_DIR") == "":
        del env["CLAUDE_CONFIG_DIR"]           # read as unset by settings_scope_refusal: the child gets it unset too
    # a relative or empty search-path entry is the child's cwd, the room's DIR: a `claude` or a `levain` package there
    # would run in place of the real one (FINDINGS_room_r2 K5), so only absolute entries are passed on; with none left
    # the name is not passed at all, since "" is itself one empty entry (FINDINGS_room_r3 R3-2)
    for name in ("PATH", "PYTHONPATH"):
        if name in env:
            kept = os.pathsep.join(p for p in env[name].split(os.pathsep) if os.path.isabs(p))
            if kept:
                env[name] = kept
            else:
                del env[name]
    env[ROOM_ENV] = "1"
    return env


def settings_scope_refusal(environ: Mapping[str, str], home: str,
                           cwd: Optional[str] = None) -> Optional[str]:
    """Why the settings the room's child will load widen its read scope, or None (roomfix1 C2, roomfix2 K2/K3).
    ``--setting-sources user`` still loads his user ``settings.json`` (``$CLAUDE_CONFIG_DIR`` or ``<home>/.claude``) and
    the managed settings, their ``managed-settings.d`` drop-ins included (listed by the one owner, :func:`_loaded_settings`). The room refuses to open when any of them names ``permissions.additionalDirectories`` (it adds
    directories outside the room's cwd to what its file tools may read) or a path-specific ``permissions.allow`` rule for
    a file tool whose path reaches outside ``cwd`` (:func:`_allow_rule_outside`; a bare ``Read`` allow does not widen
    the scope: the open check's scope decoy was denied on a real claude with one, RETURN_land_room 2026-11-04). A
    ``CLAUDE_CONFIG_DIR`` that is not absolute is refused (roomfix2 K2): the child runs in the room's cwd, so a relative
    one would name a different directory there than the one read here. A file that is present but cannot be read as a
    JSON object (or, for a profile, a property list) is refused too (fail closed: what it would add cannot be said). A
    missing file adds nothing."""
    config = environ.get("CLAUDE_CONFIG_DIR")
    if config and not os.path.isabs(config):
        return _one_line(f"CLAUDE_CONFIG_DIR {config!r} is not an absolute path: the room's child runs in the room's "
                         "directory and would read its settings from a different place than levain checks; set it absolute",
                         400)
    config = config or os.path.join(home, ".claude")
    try:
        loaded = _loaded_settings(os.path.join(config, "settings.json"))
    except Exception as exc:  # noqa: BLE001 - the owner's listing failed: what the child loads cannot be said, so refuse
        return _one_line(f"the managed settings files the room's child loads cannot be listed ({type(exc).__name__}); "
                         "what they add to the room's read scope cannot be said", 400)
    for path in loaded:
        try:
            if path.endswith(".plist"):
                with open(path, "rb") as fb:
                    doc = plistlib.load(fb)
            else:
                with open(path, encoding="utf-8") as f:
                    doc = json.load(f)
        except FileNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001 - any file the check cannot interpret fails closed (a bad XML plist is expat's)
            return _one_line(f"the settings file {path} the room's child loads cannot be read ({type(exc).__name__}); "
                             "what it adds to the room's read scope cannot be said", 400)
        perms = doc.get("permissions") if isinstance(doc, dict) else None
        if not isinstance(doc, dict) or (perms is not None and not isinstance(perms, dict)):
            return _one_line(f"the settings file {path} the room's child loads is not a JSON object with an object "
                             "'permissions'", 400)
        extra = (perms or {}).get("additionalDirectories")
        if extra:
            return _one_line(f"the settings file {path} the room's child loads names permissions.additionalDirectories "
                             f"({len(extra) if isinstance(extra, list) else 'not a list'}): the room's file tools could "
                             "read outside its cwd; remove them, or run the room under another CLAUDE_CONFIG_DIR", 400)
        allow = (perms or {}).get("allow", [])
        if not isinstance(allow, list):
            return _one_line(f"the settings file {path} the room's child loads has a permissions.allow that is not a "
                             "list", 400)
        for rule in allow:
            if isinstance(rule, str) and _allow_rule_outside(rule, cwd, home, os.path.dirname(path)):
                return _one_line(f"the settings file {path} the room's child loads allows {rule!r}: that rule lets the "
                                 "room's file tools read outside its directory; remove it, or run the room under another "
                                 "CLAUDE_CONFIG_DIR", 400)
    return None


def _loaded_settings(user: str) -> list:
    """The path of every settings file the room's child loads: ``user``, the owner's managed files
    (:func:`levain.consent.adapters.claude_code.managed_settings_files`: managed-settings.json then its
    managed-settings.d drop-ins, which Claude Code merges with or without the base file, roomfix3 R3-1), then the
    room's own :data:`MANAGED_PROFILES`. A drop-in directory the owner cannot list comes back as a path itself, so its
    read fails and the check refuses. The call is made through the owner's module so a test's patch of its seam is seen."""
    return [user, *(str(p) for p in claude_code.managed_settings_files()), *MANAGED_PROFILES]


# the file tools whose path-specific allow rules can widen what the room reads: its own (Read, Grep, Glob) and the edit
# tools (an edit permission may carry a read one; the room cannot use those tools, but a rule for them is refused the same)
_FILE_RULE = re.compile(r"\s*(Read|Grep|Glob|LS|NotebookRead|Edit|Write|MultiEdit|NotebookEdit)\s*\((.*)\)\s*", re.S)
_GLOB_CHARS = "*?[{"


def _allow_rule_outside(rule: str, cwd: Optional[str], home: str, settings_dir: str) -> bool:
    """Whether the allow rule ``rule`` names a path, for a file tool, that reaches outside ``cwd`` (roomfix2 K3). A bare
    tool name (``Read``) and a rule for any other tool are not path rules: False. The path follows Claude Code's rule
    syntax: ``//p`` absolute, ``~/p`` under ``home``, ``/p`` under the settings file's directory, anything else under the
    cwd. Its literal part (up to the last ``/`` before the first glob character) must resolve inside ``cwd``; with no
    ``cwd`` given every path rule reaches outside."""
    m = _FILE_RULE.fullmatch(rule)
    if m is None:
        return False
    spec = m.group(2).strip()
    if spec in ("", "*", "**"):
        return False                         # the tool's whole default scope: no wider than the bare rule
    if cwd is None:
        return True
    if spec.startswith("//"):
        path = spec[1:]
    elif spec == "~" or spec.startswith("~/"):
        path = os.path.join(home, spec[2:])
    elif spec.startswith("/"):
        path = os.path.join(settings_dir, spec[1:])
    else:
        path = os.path.join(cwd, spec)
    cut = min((i for i in (path.find(c) for c in _GLOB_CHARS) if i >= 0), default=-1)
    literal = path if cut < 0 else path[:path.rfind("/", 0, cut) + 1] or "/"
    if ".." in path[len(literal):].split("/"):
        return True                          # a `..` the literal cut leaves in the glob part climbs where realpath cannot see
    root, base = os.path.realpath(literal), os.path.realpath(cwd)
    if os.path.exists(root) and os.path.exists(base) and os.path.samefile(root, base):
        return False
    # lexical first (a canonical input answers as it always did); then identity (`pathid.inside`, True only when sure), so
    # an allow rule spelled in another letter case that IS under the cwd is not read as outside. A stat that cannot answer
    # leaves it "outside": this refuses a launch, so the sure-inside direction is the one that may let a rule through.
    inside = root == base or root.startswith(base.rstrip("/") + "/") or pathid.inside(base, root)
    return not inside


class RoomLaunchError(CoordError):
    """The room could not be opened or kept; the message says why. Nothing was attached."""


_NOTSENT_GRACE = 1.0     # s a first-write NotSent waits for a mid-exit child before it counts as still alive


class _ChildExited(RoomLaunchError):
    """The child exited before its open check ended, or was gone at its first write (with ``--resume`` in its argv,
    the resume is the suspect)."""


@dataclass(frozen=True)
class RoomSetting:
    """What the room runs as. ``cwd`` is the directory its file tools see first (absolute, an existing directory).
    ``harness`` is ``claude``: the Codex room profile is RM-D3's. ``profile`` is the host's
    :class:`~levain.desk.room_profile.RoomProfile` (module docstring, "The host profile"); None is the confined room."""
    cwd: str
    model: Optional[str] = None
    harness: str = "claude"
    claude_bin: str = "claude"
    host: Optional[HostCommands] = None      # D2: the commands the host gives the room (None: propose_drop only, as before)
    profile: Optional[RoomProfile] = None

    def __post_init__(self) -> None:
        if self.profile is not None and not isinstance(self.profile, RoomProfile):
            raise ValueError("profile must be a RoomProfile or None (the confined room)")

    @property
    def door_names(self) -> tuple:
        """The door's short tool names this setting opens the room with."""
        return host_door_tools(self.host)


@dataclass(frozen=True)
class RoomCanary:
    """The accepted open-check verdict the chat service records the id of (it requires a dataclass with ``ok``)."""
    ok: bool
    decoy: str
    reason: str


class RoomHandle(NamedTuple):
    """``resumed``: the launch was opened with ``--resume``. ``resume_failed``: the session id whose resume exited
    before its open check ended (or was gone at its first write), so this launch is a fresh start in its place (None
    otherwise). Both describe the launch, whichever :meth:`RoomLauncher.ensure` call returns it."""
    launch_id: str
    room_thread: str
    harness_session_id: str
    resumed: bool
    resume_failed: Optional[str] = None


def _sweep_group(pid: Any) -> None:
    """SIGKILL what is left of the process group ``pid`` led (a leader that exited on its own leaves its tool
    processes behind). An empty group is ESRCH; a pid reused in that gap is a known edge (as in claude_session)."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


def _stop_child(proc: Any) -> None:
    """Terminate, then kill, the child, each bounded by :data:`KILL_WAIT`; when the child leads its own process group
    (as a spawned room child does) the signals go to the GROUP, and a last kill sweeps it even after the leader is gone,
    so a tool or MCP process holding the pipe does not outlive it. The fallback for a child no session holds (a failed
    open); an ended launch goes through the session's own close. Takes no lock. Never raises."""
    group = _leads_own_group(proc)
    pid = getattr(proc, "pid", None)

    def sig(num: int, plain: Callable[[], None]) -> Callable[[], None]:
        def go() -> None:
            if group:
                try:
                    os.killpg(pid, num)
                    return
                except ProcessLookupError:
                    return
                except OSError:
                    pass
            plain()
        return go
    for step in (sig(signal.SIGTERM, proc.terminate), lambda: proc.wait(KILL_WAIT), sig(signal.SIGKILL, proc.kill),
                 lambda: proc.wait(KILL_WAIT)):
        try:
            if proc.poll() is not None:
                break
            step()
        except Exception:  # noqa: BLE001 - a child that will not die is the caller's to find; this never raises
            pass
    if group:
        _sweep_group(pid)                            # the leader is gone; any member still in the group goes with it


class RoomSession(ClaudeSession):
    """A :class:`~levain.desk.claude_session.ClaudeSession` that reports when the child's output has ended.
    ``child=`` is accepted and ignored (the old constructor's keyword; the transport owns the child)."""

    def __init__(self, transport: Any, *, child: Any = None, **kw: Any) -> None:
        super().__init__(transport, **kw)

    @property
    def output_ended(self) -> bool:
        """The transport has read the end of the child's output."""
        return bool(getattr(self._t, "eof", False))


# ---- the service's two seams for a room launch ------------------------------------------------------------------------
def cwd_refusal(raw: Any, cwd: Optional[str]) -> Optional[str]:
    """Why the child's reported ``cwd`` is not the launch's recorded one (both resolved), or None when it is. A launch
    with no absolute recorded cwd, and a missing, malformed or different reported one, are refusals: the cwd is the
    room's filesystem boundary (``blockReadsOutsideWorkingDirectories``)."""
    got = raw.get("cwd") if isinstance(raw, Mapping) else None
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return "the room launch has no recorded cwd to hold its child to"
    if not isinstance(got, str) or not got or not os.path.isabs(got):
        return "the room child reported no usable cwd"
    try:
        same = os.path.realpath(got) == os.path.realpath(cwd)
    except (OSError, ValueError):
        same = False
    return None if same else _one_line(f"the room child reports cwd {got[:120]!r}, not its launch's {cwd[:120]!r}")


def room_probe(launch_id: str, evidence: LivenessEvidence, cwd: Optional[str] = None,
               door_names: Optional[tuple] = None, profile: Optional[RoomProfile] = None) -> Verdict:
    """The room's verdict on one ``init``: live only for exactly its tools, its one connected door, under ``dontAsk``,
    in the launch's recorded ``cwd`` (no ``cwd`` given is not live). With a host ``profile`` the judge is
    :func:`~levain.desk.claude_session.host_probe` instead (the profile's mode and required-tools floor)."""
    raw = evidence.raw if isinstance(evidence, LivenessEvidence) else None
    got = open_probe(raw, door_names) if profile is None else host_probe(raw, profile, door_names)
    if not got.live:
        return Verdict(False, got.reason)
    why = cwd_refusal(raw, cwd)
    return Verdict(False, why) if why is not None else Verdict(True, got.reason)


def room_tripwire_context(cwd: str, door_names: Optional[tuple] = None,
                          profile: Optional[RoomProfile] = None) -> TripwireContext:
    """The room's TripwireContext (what it accepts: the module docstring). A host ``profile`` gets the OPEN context:
    under bypass every accepted call is ``{accept, config, mode}`` for any tool, so no tool list or allow rule is
    named (``open_tools=True``, :mod:`levain.desk.chat_tripwires`); every other shape still latches."""
    def expected(tool: str) -> int:
        raise ValueError(f"{str(tool)[:60]} is not one of the room's tools")

    if profile is not None:
        return TripwireContext(cwd, expected, lambda _call, _input: False, frozenset(), open_tools=True)
    tools = (*ROOM_TOOLS, *room_door_tools(door_names))
    return TripwireContext(cwd, expected, lambda _call, _input: False,
                           frozenset((t, s) for t in tools for s in _SOURCES))


def make_probe(store: ChatStore, other: Optional[Callable[[str, LivenessEvidence], Verdict]] = None,
               door_names: Optional[tuple] = None,
               profile: Optional[RoomProfile] = None) -> Callable[[str, LivenessEvidence], Verdict]:
    """The one ``probe`` a ChatService takes: a room launch gets :func:`room_probe` over its recorded cwd, any other
    launch gets ``other`` (no verdict at all, which latches, when there is none)."""
    def probe(launch_id: str, evidence: LivenessEvidence) -> Verdict:
        row = store._read(lambda conn: conn.execute("SELECT role, cwd FROM launches WHERE launch_id = ?",
                                                    (launch_id,)).fetchone())
        if row is not None and row[0] == "room":
            return room_probe(launch_id, evidence, row[1], door_names, profile)
        if other is None:
            return Verdict(False, "no probe is configured for this launch")
        return other(launch_id, evidence)
    return probe


def make_tripwires(store: ChatStore, other: Optional[Callable[[str], Optional[TripwireContext]]] = None,
                   door_names: Optional[tuple] = None,
                   profile: Optional[RoomProfile] = None) -> Callable[[str], Optional[TripwireContext]]:
    """The one ``tripwires`` a ChatService takes: a room launch gets :func:`room_tripwire_context` over its recorded
    cwd, any other launch gets ``other`` (None when there is none)."""
    def tripwires(launch_id: str) -> Optional[TripwireContext]:
        row = store._read(lambda conn: conn.execute("SELECT role, cwd FROM launches WHERE launch_id = ?",
                                                    (launch_id,)).fetchone())
        if row is not None and row[0] == "room":
            return room_tripwire_context(row[1] or "", door_names, profile)
        return other(launch_id) if other is not None else None
    return tripwires


# ---- small pure helpers -----------------------------------------------------------------------------------------------
def _one_line(text: str, limit: int = 300) -> str:
    return "".join(c if 32 <= ord(c) < 127 else " " for c in " ".join(str(text).split()))[:limit].strip() or "no detail"


def mcp_config_json(launch_id: str, python: str, host: Optional[HostCommands] = None) -> str:
    """The room's ``--mcp-config``: one stdio server, the door bound to ``launch_id`` in its own environment (a call
    carries no session id; RM0 finding 8)."""
    env = {LAUNCH_ENV: launch_id, ROOM_ENV: "1"}
    if host is not None:        # the host's argvs reach the door here, in the launch's own 0700 file, and nowhere the model names
        env[HOSTCMD_ENV] = host.to_json()
    return json.dumps({"mcpServers": {ROOM_MCP_SERVER: {
        "command": python, "args": ["-P", "-m", "levain.desk.room_mcp", "--launch", launch_id],
        "env": env}}}, sort_keys=True)


def _guard_source() -> bytes:
    """The read-guard hook's script as the package holds it now. A launch reads it ONCE and writes, names and hashes
    exactly those bytes (:meth:`RoomLauncher._put_guard`); nothing else hashes the package's copy."""
    return Path(room_readguard.__file__).read_bytes()


def _file_sha256(path: Path) -> Optional[str]:
    """The sha256 of the file at ``path``, or None when it cannot be read."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _guard_name(sha256: str) -> str:
    """The script's file name in run_dir: one file per version of the script, never replaced by another version."""
    return f"room_readguard-{sha256[:16]}.py"


def _hook_name(sha256: str) -> str:
    """A host hook script's file name in run_dir: one file per version of the script (stable across launches)."""
    return f"room_hook-{sha256[:16]}.py"


def containment_digest(settings_json: str, readguard_sha256: str, host: Optional[HostCommands] = None, *,
                       profile: Optional[RoomProfile] = None, hook_sha256: Sequence[str] = ()) -> str:
    """64 hex: the containment policy a spawn ran under (tool flags, mode, sources, deny rules, the read-guard hook entry,
    door name) and ``readguard_sha256``, the sha256 of the script bytes written for THIS launch (L3 r1 codex MED: never a
    re-read of the package, which an upgrade can change between the write and activation).

    With a host ``profile`` the policy is :func:`~levain.desk.room_profile.host_policy` instead (the confined dict is not
    touched, so every pinned confined digest holds): ``readguard_sha256`` is ignored (that profile has no read guard) and
    ``hook_sha256`` holds the sha256 of each hook script's COPIED bytes, one per hook, in order."""
    if profile is not None:
        policy = host_policy(profile, settings_json=settings_json, hook_sha256=hook_sha256, door=host_door_tools(host),
                             host_commands=json.loads(host.to_json()) if host is not None else None)
        return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    policy = {"tools": list(ROOM_TOOLS), "denied": sorted(ROOM_DENIED_TOOLS), "mode": ROOM_PERMISSION_MODE,
              "setting_sources": "user", "settings": json.loads(settings_json), "door": ROOM_MCP_TOOL,
              "readguard_sha256": readguard_sha256}
    if host is not None:        # D2: the door's tool set and the argvs it runs are part of the containment a spawn ran under
        policy["door"] = list(host_door_tools(host))
        policy["host_commands"] = json.loads(host.to_json())
    return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _spawn(argv: list, *, cwd: str, env: dict, stderr: Any) -> Any:
    return subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                            text=True, encoding="utf-8", errors="replace", start_new_session=True)


class _Live:
    """What the launcher holds for one running room launch."""

    def __init__(self, launch_id: str, session: RoomSession, proc: Any, workdir: Path, *, room_thread: str,
                 session_id: str, resumed: bool, resume_failed: Optional[str]) -> None:
        self.launch_id, self.session, self.proc, self.workdir = launch_id, session, proc, workdir
        self.room_thread, self.session_id, self.resumed, self.resume_failed = (room_thread, session_id, resumed,
                                                                               resume_failed)


class RoomLauncher:
    """See the module docstring. ``store`` and ``service`` are the ChatStore and the ChatService built with
    :func:`make_probe` and :func:`make_tripwires`. ``run_dir`` is where each launch's private directory is made.
    ``spawn(argv, cwd=, env=, stderr=)`` returns a Popen-like child; ``now()`` is the wall clock for the idle test."""

    def __init__(self, store: ChatStore, service: ChatService, setting: RoomSetting, *, run_dir: str,
                 home: Optional[str] = None, python: Optional[str] = None, spawn: Callable[..., Any] = _spawn,
                 idle_seconds: float = IDLE_SECONDS, open_deadline: float = OPEN_DEADLINE,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 environ: Optional[dict] = None, levain_home: Optional[str] = None, env_keep: tuple = (),
                 scope_dir: Optional[str] = None) -> None:
        if setting.harness != "claude":
            raise RoomLaunchError(f"the room opens on claude here; a {str(setting.harness)[:20]} room profile is not "
                                  "built (RM-D3)")
        for name, p in (("the room's cwd", setting.cwd), ("run_dir", run_dir)):
            if not isinstance(p, str) or not os.path.isabs(p):
                raise RoomLaunchError(f"{name} must be an absolute path")
        if not os.path.isdir(setting.cwd):
            raise RoomLaunchError(f"the room's cwd {setting.cwd[:120]!r} is not a directory")
        self._store, self._svc, self._setting = store, service, setting
        self._run_dir = Path(run_dir)
        self._home = home if home is not None else os.path.expanduser("~")
        self._python = python or sys.executable
        self._spawn = spawn
        self._idle = idle_seconds
        self._deadline = open_deadline
        self._now = now
        self._environ = environ
        self._lock = threading.RLock()                 # one open, end or recycle at a time
        self._live: dict = {}
        self._conn = 0
        if levain_home is None:
            from levain.consent.queue import queue_dir_for     # lazy: the consent package is not this module's import

            # levain's own home as the environment the child gets names it (the consent queue's parent)
            levain_home = str(queue_dir_for(self._environ if self._environ is not None else os.environ).parent)
        self._levain_home = levain_home
        self._env_keep = tuple(env_keep)
        self._scope_dir = scope_dir          # where the scope decoy's directory is made (None: the system temp dir)
        self._profile = setting.profile
        if self._profile is not None:
            # the host profile has no deny rules and no read guard (module docstring); its one early refusal is a hook
            # script that is already gone, so a bridge that built the profile earlier fails here, before any launch row
            self._deny = ()
            for spec in self._profile.hooks:
                self._check_hook(spec)
            self._settings_json = "{}"
            return
        # refused early, before any launch row: a bad cwd/deny pair is the operator's to fix
        self._deny = default_deny_paths(self._home, levain_home)
        # the read-guard hook's script lives in run_dir under a name carrying its sha256 (one immutable file per version,
        # :meth:`_put_guard`), so every launch of one version has the same settings document and containment digest,
        # and another launcher with another version never replaces the file a running room runs. Built once here so a
        # bad cwd/deny pair is refused before any launch row; each launch rebuilds it for the bytes it wrote
        # (``_settings_json`` is the document for the package's script at construction, read for its deny rules)
        self._settings_json = self._settings_for(hashlib.sha256(_guard_source()).hexdigest())

    @staticmethod
    def _check_hook(spec: Any) -> None:
        """:meth:`HookSpec.check` as a launch error (a script that vanished or became unreadable since the profile was
        built)."""
        try:
            spec.check()
        except ValueError as exc:
            raise RoomLaunchError(f"the room profile's hook script cannot be used: {_one_line(str(exc))}") from exc

    def _settings_for(self, guard_sha256: str) -> str:
        """The room's ``--settings`` document whose hook runs the script file of ``guard_sha256``."""
        try:
            return room_settings_json(self._setting.cwd, self._deny,
                                      room_hook_command(self._python, self._run_dir / _guard_name(guard_sha256)))
        except ValueError as exc:
            raise RoomLaunchError(f"the room's settings could not be built: {exc}") from exc

    # --- reads ------------------------------------------------------------------------------------------------------
    def _newest(self, room_thread: str) -> Optional[tuple]:
        """``(launch_id, latest launch event kind)`` of the room thread's newest room launch, or None."""
        def read(conn: Any) -> Optional[tuple]:
            row = conn.execute(
                "SELECT l.launch_id, (SELECT e.kind FROM launch_events e WHERE e.launch_id = l.launch_id ORDER BY "
                "e.seq DESC LIMIT 1) FROM launches l, json_each(l.threads) j WHERE l.role = 'room' AND j.value = ? "
                "ORDER BY l.gseq DESC LIMIT 1", (room_thread,)).fetchone()
            return None if row is None else row
        return self._store._read(read)

    def _resume_id(self, room_thread: str) -> Optional[str]:
        """The newest conversation id any room launch of this thread recorded."""
        row = self._store._read(lambda conn: conn.execute(
            "SELECT e.harness_session_id FROM launch_events e JOIN launches l ON l.launch_id = e.launch_id, "
            "json_each(l.threads) j WHERE l.role = 'room' AND j.value = ? AND e.kind = 'active' AND "
            "e.harness_session_id IS NOT NULL ORDER BY e.gseq DESC LIMIT 1", (room_thread,)).fetchone())
        return None if row is None else row[0]

    def _last_activity(self, launch_id: str) -> datetime:
        row = self._store._read(lambda conn: conn.execute(
            "SELECT MAX(t) FROM (SELECT MAX(at) t FROM launch_events WHERE launch_id = ? AND kind = 'active' "
            "UNION ALL SELECT MAX(at) FROM chat_messages WHERE launch_id = ? "
            "UNION ALL SELECT MAX(at) FROM chat_events WHERE launch_id = ? AND backfill = 0)",
            (launch_id, launch_id, launch_id)).fetchone())
        return parse_stamp(row[0]) if row is not None and row[0] else self._now()

    def _unsettled(self, launch_id: str) -> int:
        """Lines of the launch whose latest outbox row is neither a final one nor accepted."""
        return self._store._read(lambda conn: conn.execute(
            "SELECT COUNT(*) FROM chat_messages m WHERE m.launch_id = ? AND (SELECT o.state FROM chat_outbox_events o "
            "WHERE o.message_id = m.message_id ORDER BY o.seq DESC LIMIT 1) IN ('pending', 'attempted')",
            (launch_id,)).fetchone()[0])

    def child_alive(self, launch_id: str) -> bool:
        with self._lock:
            held = self._live.get(launch_id)
        return held is not None and held.proc.poll() is None

    # --- the open ---------------------------------------------------------------------------------------------------
    def ensure(self, *, fresh: bool = False) -> RoomHandle:
        """The open room's running launch, opened (or reopened, resuming the room thread's newest conversation unless
        ``fresh``) when it has none. A launch that is active but whose child exited, that this process holds no session
        for, or that the service latched is ended first. Raises :class:`RoomLaunchError` when there is no open room or
        the open check fails; the failed launch is ended and nothing stays attached. Every launch this process holds
        for a room thread that is no longer the open room is ended first (a rotation leaves no child behind). A resume
        whose child exits before its open check ends is retried ONCE as a fresh session (``resume_failed`` on the
        handle names the abandoned id); so is one whose child is gone at the open check's first write. A fresh open that
        fails is not retried."""
        with self._lock:
            room_thread = self._store._read(_open_room)
            self._end_rotated(room_thread)
            if room_thread is None:
                raise RoomLaunchError("there is no open room; nothing was opened")
            newest = self._newest(room_thread)
            if newest is not None and newest[1] in ("pending", "active"):
                held = self._live.get(newest[0])
                why = None
                if held is None:
                    why = "room launch not held by this process"
                elif held.proc.poll() is not None:
                    why = "room child exited"
                elif self._store._read(lambda conn: liveness(conn, newest[0])).latched:
                    why = "room latched not live"
                if held is not None and why is None and newest[1] == "active":
                    return RoomHandle(newest[0], room_thread, held.session_id, held.resumed, held.resume_failed)
                self.end(newest[0], why or "room launch replaced")
            resume = None if fresh else self._resume_id(room_thread)
            try:
                return self._open(room_thread, resume)
            except _ChildExited:
                if resume is None:
                    raise
                # a resume the vendor rejects (a pruned or corrupt conversation) would fail every reopen the same way
                return self._open(room_thread, None, resume_failed=resume)

    def _end_rotated(self, room_thread: Optional[str]) -> list:
        """End every held launch whose room thread is not ``room_thread`` (the open room, or None). The ids ended."""
        out = []
        for launch_id, held in list(self._live.items()):
            if held.room_thread != room_thread:
                self._end(launch_id, "room rotated")
                out.append(launch_id)
        return out

    def _next_conn(self) -> int:
        self._conn += 1
        return self._conn

    def _open(self, room_thread: str, resume: Optional[str], *, resume_failed: Optional[str] = None) -> RoomHandle:
        profile = self._profile
        # the host profile's own model wins over the setting's (the argv and the launch row say the same one)
        model = profile.model if profile is not None and profile.model else self._setting.model
        with self._store._write() as (conn, now):
            ln = lease.open_room_launch(conn, now, harness="claude", model=model,
                                        cwd=self._setting.cwd, room_thread=room_thread, pid=os.getpid(),
                                        resumed_from=resume)
        launch_id = ln.launch_id
        nonce = secrets.token_hex(16)
        workdir = self._run_dir / launch_id
        sid = resume or str(uuid.uuid4())             # the one id the child is given, and must report
        proc = session = None
        scope_dir: Optional[str] = None
        hook_files: list = []
        try:
            env = room_env(self._environ if self._environ is not None else os.environ, self._env_keep)
            if profile is not None:
                # no read scope to widen, no decoys and no read guard to exempt them from: nothing of the confined
                # room's open applies. The hook scripts are re-checked here (they can vanish after construction)
                for spec in profile.hooks:
                    self._check_hook(spec)
                argv, stderr_path, settings_json, hook_files = self._write_host_files(workdir, launch_id, resume, sid)
                decoy = scope = guard_sha = ""
                for path, sha in hook_files:        # fail closed, as the read guard's check below
                    if _file_sha256(path) != sha:
                        raise RoomLaunchError(f"the room's hook script {str(path)[:160]!r} no longer holds the bytes "
                                              "written for this launch; nothing was spawned")
            else:
                widened = settings_scope_refusal(env, self._home, cwd=self._setting.cwd)
                if widened is not None:
                    raise RoomLaunchError(widened)
                scope_dir, scope = self._scope_decoy()
                argv, decoy, stderr_path, settings_json, guard, guard_sha = self._write_files(workdir, launch_id, resume, sid)
                # one absolute string each, for the hook's exemption AND the open check: a relative or Path value can neither
                # abort the launch after its files were written nor make the hook inert (L3 r1 complement)
                decoy, scope = str(os.path.abspath(decoy)), str(os.path.abspath(scope))
                # the hook leaves BOTH decoys to the vendor, so each canary is answered by what it tests (room_readguard)
                env[room_readguard.EXEMPT_ENV] = json.dumps([decoy, scope])
                # fail closed: the script the child will run must still be the bytes this launch's digest is taken over
                if _file_sha256(guard) != guard_sha:
                    raise RoomLaunchError(f"the room's read-guard script {str(guard)[:160]!r} no longer holds the bytes "
                                          "written for this launch; nothing was spawned")
            with open(stderr_path, "ab") as errf:
                proc = self._spawn(argv, cwd=self._setting.cwd, env=env, stderr=errf)
            session = RoomSession(PipeTransport(proc), conn=self._next_conn(), session_id=sid,
                                  live=room_live.Sink(launch_id))
            self._live[launch_id] = _Live(launch_id, session, proc, workdir, room_thread=room_thread, session_id=sid,
                                          resumed=resume is not None, resume_failed=resume_failed)
            self._svc.activate(launch_id, "probing", "room spawned" if resume_failed is None else _one_line(
                f"room spawned as a fresh session after the resume of {resume_failed} exited", 200), spawn_nonce=nonce)
            if profile is not None:
                init = self._open_check_host(session, proc, stderr_path, sid, profile)
                canary = RoomCanary(True, "", "host profile: open turn ended")
                cdigest = containment_digest(settings_json, "", self._setting.host, profile=profile,
                                             hook_sha256=[sha for _p, sha in hook_files])
            else:
                init = self._open_check(session, proc, decoy, stderr_path, sid, scope)
                canary = RoomCanary(True, decoy, "the canary Read was denied")
                cdigest = containment_digest(settings_json, guard_sha, self._setting.host)
            digest = "sha256:" + hashlib.sha256(json.dumps(argv, sort_keys=True).encode()).hexdigest()
            # the launch stays pending, so send_room resolves nothing to it, until it is attached, live and holds a
            # vendor position; activation is the last step (L3_rme_r1 codex HIGH 1: a send in between was stored
            # refused or cancelled for good under its outbox_id)
            self._svc.attach(launch_id, session)
            self._svc.activate(launch_id, "live", "room open check passed", evidence=init, spawn_nonce=nonce,
                               canary=canary, containment_digest=cdigest)
            # the service sends nothing to a launch with no recorded vendor position: record the open check's events
            # (the first read ends at its turn boundary) and make sure they did not latch the room they opened
            self._svc.read_next(launch_id)
            fold = self._store._read(lambda conn: liveness(conn, launch_id))
            if not fold.enabled:
                raise RoomLaunchError(f"the room was turned off by its own open check: {fold.why_not}")
            if proc.poll() is not None:
                raise RoomLaunchError(f"the room child exited (code {proc.returncode}) before it was activated"
                                      f"{self._tail(stderr_path)}")
            with self._store._write() as (conn, now):
                # the room read at the top of ensure may have rotated during the open check (codex HIGH 3)
                if _open_room(conn) != room_thread:
                    raise RoomLaunchError(f"room {room_thread} rotated during the open; this launch is not activated")
                got, why = lease.activate_room(conn, now, launch_id, harness_session_id=sid, argv_sha256=digest)
            if why is not None:
                raise RoomLaunchError(f"the room launch could not be activated: {why}")
            return RoomHandle(launch_id, room_thread, sid, resume is not None, resume_failed)
        except BaseException as exc:
            if proc is not None and launch_id not in self._live:
                _stop_child(proc)                        # spawned, but not yet held: nothing else will stop it
            self._fail(launch_id, nonce, exc)
            if isinstance(exc, RoomLaunchError):
                raise
            if isinstance(exc, Exception):
                raise RoomLaunchError(f"the room could not be opened: {_one_line(f'{type(exc).__name__}: {exc}')}"
                                      ) from exc
            raise
        finally:
            if scope_dir is not None:
                shutil.rmtree(scope_dir, ignore_errors=True)

    def _scope_decoy(self) -> tuple:
        """``(dir, path)``: a fresh 0700 directory holding a decoy file that is outside the room's cwd, outside every
        absolute deny path (levain's home included) and matched by no deny glob, so only the outside-the-cwd block can
        refuse its Read. Its Read being denied is the open check's proof that the effective read scope is no wider
        than the cwd at that path (roomfix1 C2). Raises :class:`RoomLaunchError` when no such place can be made."""
        d = tempfile.mkdtemp(prefix="levain-room-scope-", dir=self._scope_dir)
        try:
            real = os.path.realpath(d)
            roots = [os.path.realpath(self._setting.cwd)] + [os.path.realpath(p) for p in self._deny
                                                              if os.path.isabs(p)]
            # a DENY test (a True refuses): the lexical form, then `pathid.maybe_inside` for a root spelled in another case
            if any(real == r or real.startswith(r.rstrip("/") + "/") or pathid.maybe_inside(r, real) for r in roots):
                raise RoomLaunchError(f"the open check's scope decoy {real[:120]!r} would be inside the room's cwd or a "
                                      "deny path; nothing would test the read scope")
            path = os.path.join(d, _SCOPE_NAME)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("SCOPE DECOY\n")
            return d, path
        except BaseException:
            shutil.rmtree(d, ignore_errors=True)
            raise

    def _write_files(self, workdir: Path, launch_id: str, resume: Optional[str], sid: str) -> tuple:
        self._run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        workdir.mkdir(mode=0o700)
        os.chmod(workdir, 0o700)

        def put(name: str, text: str) -> Path:
            p = workdir / name
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            return p

        guard, guard_sha = self._put_guard()
        settings_json = self._settings_for(guard_sha)
        mcp = put("mcp.json", mcp_config_json(launch_id, self._python, self._setting.host))
        settings = put("settings.json", settings_json)
        stderr = put("child.stderr", "")
        decoy_dir = workdir / "decoy"
        decoy_dir.mkdir(mode=0o700)
        decoy = put("decoy/" + _CANARY_NAME, "DECOY\n")
        argv = room_argv(mcp_config=mcp, settings=settings, model=self._setting.model, claude_bin=self._setting.claude_bin,
                         door_tools=self._setting.door_names, **({"resume": resume} if resume else {"session_id": sid}))
        return argv, str(decoy), stderr, settings_json, guard, guard_sha

    def _write_host_files(self, workdir: Path, launch_id: str, resume: Optional[str], sid: str) -> tuple:
        """The host profile's files for one launch: ``(argv, stderr_path, settings_json, hook_files)`` where
        ``hook_files`` is ``[(path, sha256)]``, one per hook in the profile's order. Each hook script is read ONCE from
        the host's path and its bytes written to ``run_dir/room_hook-<sha256[:16]>.py`` (:meth:`_put_script`), and the
        settings name that copy, so the path is the same for the same bytes on every launch (the digest holds the
        settings document) and what the child runs is what was hashed."""
        profile = self._profile
        assert profile is not None
        self._run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        workdir.mkdir(mode=0o700)
        os.chmod(workdir, 0o700)

        def put(name: str, text: str) -> Path:
            p = workdir / name
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            return p

        hook_files, commands = [], []
        for spec in profile.hooks:
            try:
                data = Path(spec.script).read_bytes()
            except OSError as exc:
                raise RoomLaunchError(f"the room profile's hook script {spec.script[:160]!r} could not be read: "
                                      f"{_one_line(str(exc))}") from exc
            try:
                # a pinned hook (the default profile's turn guard): the bytes copied for the child are the pinned ones,
                # whatever the file held when HookSpec.check ran; anything else refuses the open, never a guardless room
                spec.matches(hashlib.sha256(data).hexdigest())
            except ValueError as exc:
                raise RoomLaunchError(f"the room profile's hook script cannot be used: {_one_line(str(exc))}") from exc
            path, sha = self._put_script(data, _hook_name(hashlib.sha256(data).hexdigest()), ".room_hook")
            hook_files.append((path, sha))
            commands.append(room_hook_command(self._python, path))
        try:
            settings_json = host_settings_json(profile, commands)
        except ValueError as exc:
            raise RoomLaunchError(f"the room's settings could not be built: {exc}") from exc
        mcp = put("mcp.json", mcp_config_json(launch_id, self._python, self._setting.host))
        settings = put("settings.json", settings_json)
        stderr = put("child.stderr", "")
        try:
            argv = room_host_argv(profile, mcp_config=mcp, settings=settings, model=self._setting.model,
                                  claude_bin=self._setting.claude_bin,
                                  **({"resume": resume} if resume else {"session_id": sid}))
        except ValueError as exc:
            raise RoomLaunchError(f"the room's argv could not be built: {exc}") from exc
        return argv, stderr, settings_json, hook_files

    def _put_guard(self) -> tuple:
        """``(path, sha256)``: the read-guard hook's script as the package holds it, read ONCE, written to
        ``run_dir/<_guard_name(sha256)>`` (0600, replaced whole when that file is missing or holds other bytes). The
        settings document names that file and the containment digest is taken over that sha256, so both describe the
        bytes written here, and another version of the script is another file."""
        data = _guard_source()
        return self._put_script(data, _guard_name(hashlib.sha256(data).hexdigest()), ".room_readguard")

    def _put_script(self, data: bytes, name: str, tmp_prefix: str) -> tuple:
        """``(path, sha256)``: ``data`` written to ``run_dir/name`` (0600, replaced whole when that file is missing or
        holds other bytes). ``name`` carries the sha256, so one file is one version of one script."""
        sha = hashlib.sha256(data).hexdigest()
        path = self._run_dir / name
        if _file_sha256(path) == sha:
            return path, sha
        tmp = self._run_dir / f"{tmp_prefix}.{secrets.token_hex(6)}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
        return path, sha

    def _open_turn(self, session: RoomSession, proc: Any, prompt: str, stderr_path: Path) -> LivenessEvidence:
        """Send the open check's one turn (``prompt``, op id :data:`_OPEN_OP_ID`), wait for it to end and return the
        ``init`` it produced; nothing about that ``init`` is judged here. Raises :class:`RoomLaunchError` when the turn
        could not be sent, was not accepted, did not end in time, or the child exited (:class:`_ChildExited` when it
        exited first). Both open checks (confined and host) run it."""
        try:
            sent = session.send(SendCommand(_OPEN_OP_ID, prompt, (), "queue", None))
        except NotSent as exc:
            # a dead transport is proof of a dead child only once the child is actually gone (a short grace catches
            # one mid-exit); a live child with a transient dead pipe is a plain failure, so the history is kept
            # (L3_rme_r2 codex OPEN 1 made this retry fresh; L3_rme_r3 NotSent: only for a child that has exited)
            try:
                proc.wait(timeout=_NOTSENT_GRACE)
            except Exception:  # noqa: BLE001  (TimeoutExpired: the child is still alive)
                pass
            if proc.poll() is None:
                raise RoomLaunchError(f"the open check could not be sent while the room child is still running: "
                                      f"{_one_line(str(exc))}{self._tail(stderr_path)}") from exc
            raise _ChildExited(f"the room child exited or closed its stdin before its open check could be sent "
                               f"(exit code {proc.poll()}): "
                               f"{_one_line(str(exc))}{self._tail(stderr_path)}") from exc
        except Exception as exc:  # noqa: BLE001
            raise RoomLaunchError(f"the open check could not be sent: {_one_line(f'{type(exc).__name__}: {exc}')}"
                                  ) from exc
        if sent.outcome != "accepted":
            raise RoomLaunchError(f"the open check was not accepted by the child: {_one_line(sent.detail)}")
        deadline = time.monotonic() + self._deadline
        while True:
            init = session.liveness_evidence()
            ended = any(f.get("type") == "result" for f in session.recent_frames()) and session.pending_lines() == 0
            if ended:
                break
            if proc.poll() is not None and session.output_ended:
                raise _ChildExited(f"the room child exited (code {proc.returncode}) before its open check ended"
                                   f"{self._tail(stderr_path)}")
            if time.monotonic() > deadline:
                raise RoomLaunchError(f"the room's open check did not end within {self._deadline:g} s"
                                      f"{self._tail(stderr_path)}")
            time.sleep(_POLL)
        if proc.poll() is not None:
            raise RoomLaunchError(f"the room child exited (code {proc.returncode}) after its open check"
                                  f"{self._tail(stderr_path)}")
        return init

    def _open_check_host(self, session: RoomSession, proc: Any, stderr_path: Path, sid: str,
                         profile: RoomProfile) -> LivenessEvidence:
        """The host profile's open check: the open turn (:data:`ROOM_HOST_OPEN_PROMPT`, no tool), then the ``init`` it
        produced must pass :func:`~levain.desk.claude_session.host_probe` (the profile's mode and required tools, the one
        connected door), report the launch's cwd and the session id the child was given (``sid``). No canary: the
        confined room's decoy Reads would SUCCEED under bypass, and nothing else here reads or denies a file."""
        init = self._open_turn(session, proc, ROOM_HOST_OPEN_PROMPT, stderr_path)
        probe = host_probe(init.raw, profile, self._setting.door_names)
        if not probe.live:
            raise RoomLaunchError(f"the room could not open under its host profile: {probe.reason}")
        why = cwd_refusal(init.raw, self._setting.cwd)
        if why is not None:
            raise RoomLaunchError(f"the room could not open in its cwd: {why}")
        got = init.raw.get("session_id")
        if got != sid:
            raise RoomLaunchError(_one_line(f"the room child reports session id {str(got)[:60]!r}, not the "
                                            f"{sid[:60]!r} it was given"))
        return init

    def _open_check(self, session: RoomSession, proc: Any, decoy: str, stderr_path: Path,
                    sid: str, scope: str) -> LivenessEvidence:
        """Send the canary turn, wait for it to end, and return the ``init`` it produced once the probe, the launch's
        cwd, the session id the child was given (``sid``) and BOTH canaries accept it: ``decoy`` (a deny-rule match)
        and ``scope`` (outside the cwd, matching no rule: :meth:`_scope_decoy`). Raises :class:`RoomLaunchError` with
        the reason otherwise (:class:`_ChildExited` when the child exited first)."""
        init = self._open_turn(session, proc, CANARY_PROMPT.format(path=decoy, scope=scope), stderr_path)
        probe = open_probe(init.raw, self._setting.door_names)
        if not probe.live:
            raise RoomLaunchError(f"the room could not open with its read-only tools: {probe.reason}")
        why = cwd_refusal(init.raw, self._setting.cwd)
        if why is not None:
            raise RoomLaunchError(f"the room could not open in its cwd: {why}")
        got = init.raw.get("session_id")
        if got != sid:
            raise RoomLaunchError(_one_line(f"the room child reports session id {str(got)[:60]!r}, not the "
                                            f"{sid[:60]!r} it was given"))
        verdict = canary_denied(session.recent_frames(), decoy)
        if not verdict.denied:
            raise RoomLaunchError(f"the room's open check found its deny rules not holding: {verdict.reason}")
        verdict = canary_denied(session.recent_frames(), scope)
        if not verdict.denied:
            raise RoomLaunchError(f"the room's open check found its read scope wider than its cwd (a file outside it "
                                  f"matching no deny rule was not refused): {verdict.reason}")
        return init

    @staticmethod
    def _tail(path: Path) -> str:
        try:
            raw = path.read_bytes()[-_STDERR_TAIL:].decode("utf-8", "replace")
        except OSError:
            return ""
        return f"; stderr: {_one_line(raw)}" if raw.strip() else ""

    def _fail(self, launch_id: str, nonce: str, exc: BaseException) -> None:
        """An open that did not complete: stop the child, detach, record ``not_live`` and end the launch. Best effort at
        each step; the first error is the caller's, never one raised here."""
        why = _one_line(f"{exc}" if isinstance(exc, RoomLaunchError) else f"{type(exc).__name__}: {exc}")
        self._end(launch_id, f"room open failed: {why}", note=why, nonce=nonce)

    # --- the end ----------------------------------------------------------------------------------------------------
    def end(self, launch_id: str, cause: str) -> None:
        """Stop the room's child, detach its session and end the launch. Safe against a ``send`` hung in the pipe: the
        child is stopped first, with no lock held."""
        with self._lock:
            self._end(launch_id, cause)

    def _end(self, launch_id: str, cause: str, *, note: Optional[str] = None, nonce: Optional[str] = None) -> None:
        held = self._live.pop(launch_id, None)
        room_live.forget(launch_id)                      # the ephemeral streamed text ends with its launch
        if held is not None:
            group = _leads_own_group(held.proc)          # asked while the leader is unreaped: it names the group
            try:
                # first, before detach and with no lock: the session's own close is graceful (EOF) for an idle child,
                # signals the child's whole group, and frees a send blocked in the pipe so detach can take the mutex
                held.session.close()
            except Exception:  # noqa: BLE001 - the launch must still end
                _stop_child(held.proc)
            if group:
                _sweep_group(held.proc.pid)              # a leader that left on EOF leaves its tool processes behind
        try:
            self._svc.detach(launch_id)
        except Exception:  # noqa: BLE001 - the launch must still end
            pass
        if note is not None and nonce is not None:
            try:
                self._svc.activate(launch_id, "not_live", note[:200], spawn_nonce=nonce)
            except Exception:  # noqa: BLE001
                pass
        try:
            with self._store._write() as (conn, now):
                state = _launch_state(conn, launch_id)[1]
                if state in ("pending", "active"):
                    lease.end_launch(conn, now, launch_id, "ended" if state == "active" else "failed",
                                     _one_line(cause, 200))
        except Exception:  # noqa: BLE001
            pass
        if held is not None:
            shutil.rmtree(held.workdir, ignore_errors=True)
        else:
            shutil.rmtree(self._run_dir / launch_id, ignore_errors=True)

    # --- lapse ------------------------------------------------------------------------------------------------------
    def recycle_idle(self) -> list:
        """End the room's child if it has been quiet for the idle time: no turn open, no unsettled line, nothing recorded
        for the launch since. The next :meth:`ensure` resumes. A launch whose room thread is no longer the open room is
        ended whatever its activity (a rotation). The ids ended."""
        with self._lock:
            out = self._end_rotated(self._store._read(_open_room))
            for launch_id, held in list(self._live.items()):
                if held.proc.poll() is not None or held.session.pending_lines() or self._unsettled(launch_id):
                    continue
                if self._now() - self._last_activity(launch_id) < timedelta(seconds=self._idle):
                    continue
                self._end(launch_id, "room idle")
                out.append(launch_id)
        return out

    def close(self) -> None:
        """End every launch this launcher holds (a service closing)."""
        with self._lock:
            for launch_id in list(self._live):
                self._end(launch_id, "room closed")

