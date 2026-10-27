"""Claude Code's ``PermissionRequest`` hook on the consent queue (Q2, first slice), and its ``PreToolUse`` gate
(Q14 B2, :func:`gate`: the one that holds in bypass and auto, where no dialog ever fires; see its section below).

Claude Code runs this hook when it is about to show its permission dialog, and shows the dialog AT ONCE while the
hook runs (measured, Q0 item 2); a later ``allow`` from the hook dismisses it and the tool runs. So the hook:

1. reads the event (``tool_name``, ``tool_input``, ``session_id``, ``agent_id`` for a subagent, ``cwd``) and files
   one request on the queue, with Claude Code's ``tool_use_id`` as its event id when the event carries one (a
   ``PermissionRequest`` measured in Q0 does not; the core then mints a random one);
2. asks the core's policy seam (:func:`levain.consent.policy.guarded_policy`) with the session's own posture (the
   event's ``permission_mode``) and the tool input the request was filed for; it answers only with a standing
   consent grant the queue verified. A request that targets agent config or hooks (:func:`targets_agent_config`)
   is never eligible for one;
3. when away mode is on (:func:`levain.consent.auto_away.away_state`: the operator's explicit word, else input
   idle on macOS, else off) waits up to the hold for a signed answer the core verifies, re-reading away mode while
   it waits and stopping when away mode ends, and withdraws the request if none came (one whose hold ran out
   is expired);
4. prints ``allow`` only for a verified allow (with a line naming the grant when a grant answered) and ``deny``
   only for a verified deny. Anything else, including any exception, prints nothing and exits 0: the native
   dialog stands and the human answers it.

Nothing read from stdin is ever a decision: the event only says what is being asked. Bash commands are never
parsed (ruling 1): a Bash request is just a Bash request.

Settings live in ``<levain home>/consent/claude_code.json`` (:data:`SETTINGS_NAME`)::

    {"cockpit_data_root": "/absolute/cockpit/data/root", "away_mode": false, "away_hold_seconds": 0}

plus, once ``levain consent away`` has written them, ``away_word`` (``"on"`` / ``"off"``, absent for input idle)
and ``away_idle_seconds``. Missing or unreadable settings mean no explicit word and no authority (so no answer can
verify, and nothing is held). An away mode set by ``levain consent away on`` is also an away mark in the queue store,
and the hook reads it there (:func:`with_away_mark`): until a signed leave of it verifies, the agent is away whatever
the file says. :func:`install`
writes the hook script (:data:`HOOK_TEMPLATE`) and the ``PermissionRequest`` entry into ONE workspace's project
settings; wiring into user settings is a later slice.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pwd
import re
import shlex
import shutil
import site
import subprocess
import sys
import sysconfig
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from typing import IO, Any, Literal

from levain.authority import anchor_path
# FILE_TOOLS, MCP_PATH_FIELDS, PolicyNotEligible and guarded_policy are re-exported: this module's names since Q2
from levain.consent.policy import AGENT_CONFIG_TIER, Policy
from levain.consent.policy import FILE_TOOLS as FILE_TOOLS
from levain.consent.policy import MCP_PATH_FIELDS as MCP_PATH_FIELDS
from levain.consent.policy import PolicyNotEligible as PolicyNotEligible
from levain.consent.policy import guarded_policy as guarded_policy
from levain.consent.policy import mcp_config_candidates
from levain.consent.policy import targets as _targets
from levain.consent.auto_away import DEFAULT_HOLDS, IDLE_THRESHOLD, WORDS, away_state, hold_seconds
from levain.consent.queue import (
    DB_NAME, AwayMark, ConsentQueue, Decision, authority_source, default_queue_dir, gap_label,
    queue_dir_for,
)
from levain.consent.adapters.rules import (  # noqa: F401 - the rules' one home; re-exported under these names
    CLAUDE_CODE_AGENT, CLAUDE_CONFIG_DIRS, CLEANUP_RESERVE, CONFIG_FILE_NAMES, DEFAULT_MAX_HOLD, GIT_STATE_DIRS,
    GIT_STATE_FILES, _is_config_path, _is_git_config,
)
from levain.consent.broker_client import in_slot
from levain.consent.contained import SANDBOX_ESCAPE_KEY as SANDBOX_ESCAPE_KEY
from levain.consent.contained import SANDBOX_NET_TOOL as SANDBOX_NET_TOOL
from levain.consent.contained import ContainedLaunchProof, CoreCall, core_call_of, sandbox_escape_requested, \
    sandbox_net_tool
from levain.consent.record import ExternalGateRequest

AGENT = CLAUDE_CODE_AGENT
EVENT = "PermissionRequest"
SETTINGS_NAME = "claude_code.json"
HOOK_SCRIPT_NAME = "levain_consent.py"
# A request is filed for at least this long even when nobody is waiting on it, so the policy seam has a live
# request to look at; it is withdrawn as soon as the hook stops waiting.
MIN_FILE_HOLD = timedelta(seconds=5)
# ``max_hold`` and ``CLEANUP_RESERVE`` (the reserve after the wait): :mod:`levain.consent.adapters.rules`.
# Claude Code's ``timeout`` for the hook is :func:`hook_timeout`: ``ceil(max(max_hold, CLEANUP_RESERVE))`` plus
# this margin (interpreter start, imports, settings read), so it always exceeds wait + CLEANUP_RESERVE and Claude
# Code never cancels the hook while a verified answer is on its way out (a cancelled hook's output is discarded).
TIMEOUT_MARGIN = 30
# A request on one of FILE_TOOLS (or an MCP tool, by its MCP_PATH_FIELDS) whose target path is agent config or a
# hook is tagged agent_config and filed at AGENT_CONFIG_TIER (all three from levain.consent.policy): an allow then
# needs a key class covering T3, and no policy is ever asked (fail toward the human).
DEFAULT_TIER = "T2"
# The PreToolUse gate's hold D (Q14 B2, DESIGN §3.2): ``gate_hold_seconds`` in the settings file, default 300 s,
# never past the ceiling of 840 s (a longer hold is a frozen session, which the principle rules out).
DEFAULT_GATE_HOLD = 300.0
GATE_HOLD_CEILING = 840.0

@dataclass(frozen=True)
class AdapterSettings:
    # away_mode is the explicit word on, explicit_off the explicit word off; with neither, input idle decides
    # (levain.consent.auto_away.away_state). legacy: the file holds no away_word (written before auto-away
    # existed), so its away_mode is read as the word
    away_mode: bool = False
    away_hold_seconds: float = 0.0
    cockpit_data_root: str | None = None
    explicit_off: bool = False
    away_idle_seconds: float = IDLE_THRESHOLD
    legacy: bool = False
    # away mode stands by the operator's away mark in the queue store (:func:`with_away_mark`), not by the file alone;
    # away_unreadable: the store could not be read (:func:`store_unreadable`'s words, else empty): counted as such a
    # mark, though a hook that cannot open the store can neither hold nor file a request
    away_marked: bool = False
    away_unreadable: str = ""
    # the PreToolUse gate (Q14 B2): its hold D; the notify command it pings the operator with (R-f: an argv, the
    # message appended as its last argument; None = no ping); the phone page's link the ping carries
    gate_hold_seconds: float = DEFAULT_GATE_HOLD
    notify_argv: tuple[str, ...] | None = None
    phone_url: str | None = None


def load_settings(queue_dir: Path) -> AdapterSettings:
    """The adapter's settings from ``queue_dir/claude_code.json``; defaults (away off, no authority) when the
    file is missing or any field is malformed."""
    return read_settings_file(queue_dir / SETTINGS_NAME)


def read_settings_file(path: Path) -> AdapterSettings:
    """:func:`settings_from` the JSON in ``path``; defaults when it is missing, does not parse or nests too deep.
    The Codex adapter reads its own file through this too."""
    got = try_read_settings_file(path)
    return AdapterSettings() if got is None else got


def try_read_settings_file(path: Path) -> AdapterSettings | None:
    """:func:`settings_from` the JSON in ``path``, or ``None`` when it is missing, does not parse or nests too
    deep (a re-read during a hold keeps what the hold started with, :func:`still_away_from`)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return settings_from(data)


def settings_from(data: Any) -> AdapterSettings:
    """The settings a parsed settings file means, as both adapters' hooks and ``levain consent away`` read it:
    the explicit word ``away_word``: ``"on"`` (away mode), ``"off"`` (``explicit_off``) or ``"auto"`` (input idle
    decides). A file without a valid one was written before auto-away existed (``legacy``): its ``away_mode`` is
    the word, ``true`` on and anything else off, whether ``levain init`` or the operator wrote it (the two cannot
    be told apart, and an operator's off is never undone by an upgrade); a hold only when it is a finite number >= 0
    (an integer too large for a float is malformed), an idle threshold
    (``away_idle_seconds``) only when it is a finite number > 0, else :data:`IDLE_THRESHOLD`, a cockpit root only
    when absolute (:func:`_absolute_root`). The PreToolUse gate's keys: ``gate_hold_seconds`` (a finite number > 0,
    at most :data:`GATE_HOLD_CEILING`; else :data:`DEFAULT_GATE_HOLD`), ``notify_argv`` (:func:`_argv`) and
    ``phone_url`` (:func:`_line`)."""
    if not isinstance(data, dict):
        return AdapterSettings()
    word = data.get("away_word")
    legacy = word not in WORDS
    if legacy:
        word = "on" if data.get("away_mode") is True else "off"
    return AdapterSettings(away_mode=word == "on", away_hold_seconds=_seconds(data.get("away_hold_seconds", 0), 0.0),
                           cockpit_data_root=_absolute_root(data.get("cockpit_data_root")), explicit_off=word == "off",
                           away_idle_seconds=_seconds(data.get("away_idle_seconds"), IDLE_THRESHOLD, positive=True),
                           legacy=legacy,
                           gate_hold_seconds=min(_seconds(data.get("gate_hold_seconds"), DEFAULT_GATE_HOLD,
                                                          positive=True), GATE_HOLD_CEILING),
                           notify_argv=_argv(data.get("notify_argv")), phone_url=_line(data.get("phone_url")))


def _argv(value: Any) -> tuple[str, ...] | None:
    """``value`` as a command's argv: a non-empty list of non-empty strings with no NUL; ``None`` for anything
    else (no ping)."""
    if not isinstance(value, list) or not value or \
            not all(isinstance(a, str) and a and "\x00" not in a for a in value):
        return None
    return tuple(value)


def _line(value: Any) -> str | None:
    """``value`` when it is a non-empty one-line string, else ``None``."""
    if not isinstance(value, str) or not value or any(c in value for c in "\x00\r\n"):
        return None
    return value


def _seconds(value: Any, default: float, *, positive: bool = False) -> float:
    """``value`` as a finite float >= 0 (> 0 when ``positive``); ``default`` for anything else."""
    try:
        ok = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(float(value)) \
            and (value > 0 if positive else value >= 0)
    except OverflowError:
        ok = False
    return float(value) if ok else default


def _absolute_root(root: Any) -> str | None:
    """``root`` canonicalized (symlinks resolved) when it is ABSOLUTE as written; else ``None`` (no authority). A
    ``~`` is never expanded (H4: this reads no home; ``levain init`` writes the real path), so a ``~/...`` root is
    ``None``, the safe direction: no signed answer verifies. A relative path is never resolved: the hook runs in the
    project's directory, and a repository must not be able to supply the authority it is judged by."""
    if not isinstance(root, str) or not root or "\x00" in root:
        return None
    try:
        written = Path(root)
        if not written.is_absolute():
            return None
        return os.path.realpath(written)
    except (OSError, ValueError, RuntimeError):
        return None


def protected_paths(queue_dir: Path, settings: AdapterSettings, *, home: Path, environ: Mapping[str, str],
                    cwd: str | None = None) -> tuple[Path, ...]:
    """What the adapter itself trusts, so a file tool writing any of it is agent config (codex Q2 r1 #1): the
    queue directory (the adapter settings and the queue database live in it); the whole levain home, the
    directory the confinement floor denies (:func:`levain.firing.confinement.levain_home_spellings`, one
    derivation for both), so a write to the autonomic binding store or any other operator state there is a tagged
    request (14b Q5); and, when a cockpit data root is configured, its authority database (and sqlite's sidecars)
    and the authority anchor. ``home`` is the one :func:`targets_agent_config` expands ``~`` under and ``environ``
    the one whose ``LEVAIN_HOME`` names the levain home (both required: the hook binds them once at its top);
    ``cwd``, when absolute, stands in for the workspace. A levain home the floor's own predicate finds too
    wide to deny whole (:func:`levain.firing.confinement.too_wide_to_deny` holds the rules: a filesystem root, a path
    of two parts or fewer, a shared temp directory, or one holding the home or the cwd; the event's cwd is passed as
    its ``within``) is named by its state directories instead
    (:data:`levain.firing.confinement.LEVAIN_HOME_STATE_DIRS`, the floor's fallback list), or every file under it
    would be agent config. Never raises on the levain home: one that cannot be resolved is ``<home>/.levain``
    (fail closed for the store, never an outage of every tool). Bash is still never parsed: a shell write there
    is not tagged here."""
    out = [Path(os.path.abspath(queue_dir)), *levain_home_protected(home, cwd, environ=environ)]
    if settings.cockpit_data_root:
        root = Path(settings.cockpit_data_root)
        db = root / "cockpit.db"
        out += [db, *(Path(f"{db}{s}") for s in ("-wal", "-shm", "-journal")), anchor_path(root).parent]
    return tuple(out)


def levain_home_protected(home: Path | None, cwd: str | None, *, environ: Mapping[str, str]) -> list[Path]:
    """:func:`protected_paths`' share of the levain home, named by ``LEVAIN_HOME`` in ``environ``; never raises.
    Also what :func:`queue_unresolved` (and Codex's) classify with when no queue directory could be resolved."""
    from levain.firing.confinement import LEVAIN_HOME_STATE_DIRS, levain_home_spellings, too_wide_to_deny  # lazy

    try:
        user_home = Path(home) if home is not None else Path.home()
        base, spellings = levain_home_spellings(user_home, environ=environ)
        try:
            within = (Path(cwd),) if isinstance(cwd, str) and os.path.isabs(cwd) else ()
            if any(too_wide_to_deny(d, user_home, *within) for d in spellings):
                return [base / name for name in LEVAIN_HOME_STATE_DIRS]
        except Exception:  # noqa: BLE001 - the base resolved: protect it whole, conservative
            pass
        return [base]
    except Exception:  # noqa: BLE001 - any failure resolving the base: the default home, never a raise
        pass
    try:
        user_home = Path(home) if home is not None else Path.home()
    except Exception:  # noqa: BLE001 - no $HOME: the password database's
        try:
            user_home = operator_home(os.environ)
        except Exception:  # noqa: BLE001 - no home at all: nothing there to protect
            return []
    return [Path(os.path.abspath(user_home / ".levain"))]


# --- ruling 3b item 1: agent config and hooks ----------------------------------------------------------------

# What is agent config by its path (``CLAUDE_CONFIG_DIRS``, ``CONFIG_FILE_NAMES``, ``.git``'s ``GIT_STATE_*``,
# :func:`_is_config_path`): :mod:`levain.consent.adapters.rules`, which the queue broker reads too.
USER_CONFIG_NAME = ".claude.json"

# Asking git where hooks run (L3 r3 G1): from a fixed binary, never one the session's PATH finds; within one gate
# call's budget, at most GIT_QUERIES repositories asked and GIT_BUDGET_SECONDS spent, past which a target is tagged.
GIT_QUERIES = 4
GIT_BUDGET_SECONDS = 3.0


# The names a file inside a directory is probed under, so that a directory is agent config exactly when a file the
# checks below tag could sit directly in it (:func:`names_config_dir`): one name each directory-keyed check keys on.
# A name that is config in ANY directory (``.mcp.json``) is not probed, or every directory would be: the file-name
# field naming it is itself a candidate, tagged on its own.
_DIR_PROBES = ("x", "settings.json", "hooks.json", "config.toml", USER_CONFIG_NAME, "x.pth", "sitecustomize.py",
               "levain", "config")


def names_config_dir(target: str, cwd: str, home: Path, roots: tuple[str, ...], sites: tuple[str, ...],
                     user_configs: frozenset[str], git_cache: dict[Any, Any] | None = None, *,
                     user_roots: tuple[str, ...], environ: Mapping[str, str]) -> bool:
    """For an MCP tool's every-string candidates only (:func:`levain.consent.policy.mcp_config_candidates`): True
    when a server given ``target`` as a directory and a file name in another field could write agent config:
    ``target`` (``~`` expanded, joined to ``cwd``; as written and resolved) holds a file the path checks of
    :func:`targets_agent_config` tag (each of :data:`_DIR_PROBES` directly inside it, so the directory list is
    derived from those checks, never spelled twice) or directly one of ``roots`` (a protected file such as the
    authority database, :func:`protected_paths`), or its file name is ``.claude.json`` wherever it sits (a
    server that joins a relative name to a root of its own, home among them). ``roots``, ``sites``,
    ``user_configs`` and ``user_roots`` (:func:`user_site_roots`) are the caller's, computed once. An over-match
    costs a gated call: the request goes to the human, or, when no enrolled key can sign T3 outside ``default``,
    the call is refused (the Q10 floor) and the agent reads why."""
    raw = Path(str(home) + target[1:]) if target == "~" or target.startswith("~/") else Path(target)
    if not raw.is_absolute():
        raw = Path(cwd) / raw
    git_cache = git_cache if git_cache is not None else {}
    parents = {os.path.dirname(r) for r in roots}   # a directory holding a protected file (cockpit.db) as well
    for p in (Path(os.path.normpath(raw)), Path(os.path.realpath(raw))):
        if p.name.lower() == USER_CONFIG_NAME or str(p).lower() in parents:
            return True
        for probe in _DIR_PROBES:
            if (_tags_path(p / probe, cwd, roots, sites, user_configs, git_cache, environ=environ)
                    or in_user_site(p / probe, user_roots)):
                return True
    return False


def _tags_path(path: Path, cwd: str, roots: tuple[str, ...], sites: tuple[str, ...], user_configs: frozenset[str],
               git_cache: dict[Any, Any] | None = None, *, environ: Mapping[str, str]) -> bool:
    """One spelling of a target path (already normalised or resolved) is agent config or the hook's own code."""
    return (_is_config_path(path) or _under(path, roots) or str(path).lower() in user_configs
            or _redirects_the_import(path, sites) or _under_hooks_path(path, cwd, git_cache, environ=environ))


# --- where git runs hooks from (L3 r2 G1'): asked of git, never read out of its config ------------------------------

def operator_home(environ: Mapping[str, str]) -> Path:
    """The operator's home directory from the password database, not ``$HOME`` (which the agent's environment
    sets): the home the operator's own shell and git read the user's git config from. Only when the database has
    no entry, ``environ["HOME"]``; a ``KeyError`` when that is unset too (no guess)."""
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError):
        return Path(environ["HOME"])


def global_git_configs(home: Path, *, environ: Mapping[str, str]) -> tuple[Path, ...]:
    """The user-level git config files, tagged by full path: ``~/.gitconfig`` and ``~/.config/git/config`` for
    ``home`` (where a ``~`` in a target expands) and for :func:`operator_home`, ALWAYS, whatever
    ``$XDG_CONFIG_HOME`` says (an agent-set value must not move the protected file), plus
    ``$XDG_CONFIG_HOME/git/config`` when that variable is set to an absolute path in ``environ``."""
    out: list[Path] = []
    for h in dict.fromkeys((Path(home), operator_home(environ))):
        out += [h / ".gitconfig", h / ".config" / "git" / "config"]
    xdg = environ.get("XDG_CONFIG_HOME")
    if xdg and os.path.isabs(xdg):
        out.append(Path(xdg) / "git" / "config")
    return tuple(out)


GIT_TAGGED = "tagged"                # a repository answer that tags every target asked of it (fail closed)


def _git_envs(environ: Mapping[str, str]) -> list[dict[str, str]]:
    """The environments git is asked in: ``PATH`` = ``os.defpath``, ``HOME`` from :func:`operator_home`,
    ``LANG``/``LC_ALL`` = ``C``, nothing else (no ``GIT_*``, no ``XDG_*``); and, when ``environ``'s
    ``XDG_CONFIG_HOME`` is absolute, the same with it set (L3 r3 G1: the operator's own git reads its global config
    there). Both answers count."""
    base = {"PATH": os.defpath, "HOME": str(operator_home(environ)), "LANG": "C", "LC_ALL": "C"}
    xdg = environ.get("XDG_CONFIG_HOME")
    return [base, {**base, "XDG_CONFIG_HOME": xdg}] if xdg and os.path.isabs(xdg) else [base]


def _git_budget(cache: dict[Any, Any]) -> dict[str, Any]:
    """One gate call's git state, in its ``cache``: answers by repository top-level (``scopes``), by directory
    asked (``asked``), queries spent and the deadline (set at the first query)."""
    return cache.setdefault("git", {"scopes": {}, "asked": {}, "queries": 0, "deadline": None})


def _ask_git(directory: str, state: dict[str, Any], *, environ: Mapping[str, str]) -> frozenset[Path] | str:
    """``git -C <directory> rev-parse --path-format=absolute --show-toplevel --git-path hooks``, one spawn per
    environment (:func:`_git_envs`): git's own answer to where the repository holding ``directory`` runs hooks from
    (``core.hooksPath`` from every config file git reads, includes, linked worktrees and submodules applied), the
    hooks directories as a set, recorded under the repository's top-level. Exit 128 saying "not a git repository"
    is no repository (an empty set, recorded for ``directory``'s ancestors up to its mount point, which git's own
    search covered). Any other failure, a timeout, more than two lines, the budget spent (:data:`GIT_QUERIES`,
    :data:`GIT_BUDGET_SECONDS`): :data:`GIT_TAGGED`. No git at ``host_guard.GIT_CANDIDATES``: an empty set, no query
    spent (no git, no hooks run). An older git (< 2.31) echoes the unknown ``--path-format`` and prints a relative
    answer relative to ``directory`` (measured on 2.50 without the option), which is how it is joined.
    ``rev-parse`` runs no hooks."""
    from levain.host_guard import git_binary  # imported here, not at module load, to keep this module light

    git = git_binary()
    if git is None:
        return frozenset()
    if state["queries"] >= GIT_QUERIES:
        return GIT_TAGGED
    state["queries"] += 1
    if state["deadline"] is None:
        state["deadline"] = time.monotonic() + GIT_BUDGET_SECONDS
    hooks: set[Path] = set()
    top: str | None = None
    for env in _git_envs(environ):
        left = state["deadline"] - time.monotonic()
        if left <= 0:
            return GIT_TAGGED
        try:
            got = subprocess.run([git, "-C", directory, "rev-parse", "--path-format=absolute", "--show-toplevel",
                                  "--git-path", "hooks"], env=env, stdin=subprocess.DEVNULL, capture_output=True,
                                 timeout=left, check=False)
        except (OSError, ValueError, subprocess.SubprocessError):
            return GIT_TAGGED
        if got.returncode == 128 and b"not a git repository" in got.stderr:
            continue
        lines = got.stdout.decode("utf-8", "surrogateescape").splitlines()
        if lines and lines[0] == "--path-format=absolute":
            lines = lines[1:]
        if got.returncode != 0 or len(lines) != 2 or not os.path.isabs(lines[0]) or not lines[1]:
            return GIT_TAGGED
        top = lines[0]
        hooks.add(Path(lines[1]) if os.path.isabs(lines[1]) else Path(directory) / lines[1])
    answer = frozenset(hooks)
    if top is not None:
        state["scopes"][top] = answer
    else:
        d = Path(directory)
        while True:
            state["scopes"].setdefault(str(d), answer)
            if d.parent == d or os.path.ismount(d):
                break
            d = d.parent
    return answer


def _git_answer(directory: Path, cache: dict[Any, Any], *, environ: Mapping[str, str]) -> frozenset[Path] | str:
    """The hooks directories of the repository holding ``directory`` (real, existing): an answer already in
    ``cache`` for a repository top-level (or a no-repository ancestor) reached from ``directory`` without passing a
    directory that could start another repository (one holding a ``.git`` or a ``HEAD``, or a mount point), else
    :func:`_ask_git`. So every target inside the cwd's repository reuses its answer, and a nested repository
    (a submodule) is asked its own."""
    state = _git_budget(cache)
    key = str(directory)
    if key in state["asked"]:
        return state["asked"][key]
    d = directory
    while True:
        if str(d) in state["scopes"]:
            return state["scopes"][str(d)]
        if os.path.lexists(d / ".git") or os.path.lexists(d / "HEAD") or os.path.ismount(d) or d.parent == d:
            break
        d = d.parent
    state["asked"][key] = got = _ask_git(key, state, environ=environ)
    return got


def _nearest_dir(start: Path) -> Path:
    """``start`` or its nearest ancestor that is an existing directory (``/`` at the latest)."""
    d = start
    while not d.is_dir() and d.parent != d:
        d = d.parent
    return d


def _under_hooks_path(path: Path, cwd: str, cache: dict[Any, Any] | None = None, *,
                      environ: Mapping[str, str]) -> bool:
    """``path`` lies under a directory git runs hooks from (husky's ``.husky/``, a ``.githooks/``, an absolute
    ``core.hooksPath`` outside the worktree) for the repository of the event's ``cwd`` (its nearest existing
    directory) or of the target's nearest existing ancestor directory, as git answers (:func:`_git_answer`), the
    cwd's answer checked first. An answer git could not give in the call's budget (:data:`GIT_TAGGED`) tags the
    target. ``cache`` is one gate call's, shared by its candidates."""
    cache = cache if cache is not None else {}
    try:
        dirs = [_nearest_dir(Path(cwd))] if cwd and os.path.isabs(cwd) else []
        dirs.append(_nearest_dir(path.parent))
        for d in dirs:
            got = _git_answer(Path(os.path.realpath(d)), cache, environ=environ)
            if isinstance(got, str):   # GIT_TAGGED
                return True
            hooks = tuple({s for h in got for s in (os.path.normpath(h).lower(), os.path.realpath(h).lower())})
            if _under(path, hooks):
                return True
        return False
    except (OSError, ValueError, KeyError):
        return True   # a path that cannot be examined (or no operator home to ask git as) is tagged


def nothing_can_decide(settings: AdapterSettings, tier: str | None = None, *, contained_shell: bool = False) -> bool:
    """No explicit word on and the cockpit the settings point at replays to no active key and no standing grant (or
    has no database, or does not replay): no grant or signed answer can verify and no hold is asked (input idle
    holds only for an enrolled key, :func:`away_hold`), so the hook
    files nothing (a queue row is kept for good). The PermissionRequest hook then leaves the call to the agent's own
    dialog; the PreToolUse gate does so in ``default`` only, and in every other mode denies a
    T3 call (the floor, Q10 D1: no dialog asks there). A cockpit.db the cockpit made
    before any key was enrolled is not something that can decide (L3 r1 codex MED). For ``tier`` T3
    (:data:`AGENT_CONFIG_TIER`, never grant-eligible) only an active key whose class can sign a T3 allow counts;
    a standing grant never does, nor does away mode: a held T3 call that no key can sign only halts its session
    (L3 r1 codex 6, r2). With ``contained_shell`` (:func:`decide_ex` sets it for a chat Bash call that carries its
    launch's proof, the one call an SH1 grant can answer) an unexpired contained-shell grant counts too (L3 r1
    complement MED)."""
    return nothing_can_decide_why(settings, tier, contained_shell=contained_shell) is not None


def nothing_can_decide_why(settings: AdapterSettings, tier: str | None = None, *,
                           contained_shell: bool = False) -> str | None:
    """Why :func:`nothing_can_decide` holds, in words for the operator, or ``None`` when something can decide. The
    one body of that judgement: :func:`nothing_can_decide` is this being not ``None``."""
    if settings.away_mode and tier != AGENT_CONFIG_TIER:
        return None
    root = settings.cockpit_data_root
    if not root:
        return "no cockpit data root in the consent settings"
    if not (Path(root) / "cockpit.db").is_file():
        return "the cockpit names no ledger (no cockpit.db)"
    try:
        snap = authority_source(Path(root) / "cockpit.db", anchor=anchor_path(root))()
    except Exception:
        return "the cockpit's ledger does not replay"  # an authority that does not replay verifies nothing
    if tier == AGENT_CONFIG_TIER:
        from levain.consent.queue import class_covers

        if any(class_covers(key.key_class, tier) for key in snap.active_keys()):
            return None
        return "no enrolled key can sign an agent-config allow"
    if snap.active_keys() or _a_consent_grant_in_force(snap.policy.grants.values()):
        return None
    if contained_shell:
        from levain.authority import contained_shell_grant

        if _a_consent_grant_in_force(snap.policy.grants.values(), kind=contained_shell_grant):
            return None
    return "no enrolled key and no standing grant in force"


def _a_consent_grant_in_force(grants: Iterable[Any], *, kind: Callable[[Any], bool] | None = None) -> bool:
    """Whether any grant is a consent grant (or, with ``kind``, a grant ``kind`` accepts) that has not expired: the
    two criteria of ``grant_refusal`` that hold for every request (L3 r2 codex MED: an expired or other-kind grant
    answers nothing)."""
    from datetime import datetime, timezone

    from levain.authority import consent_grant
    from levain.cockpit.consent import parse_stamp

    is_kind = consent_grant if kind is None else kind
    now = datetime.now(timezone.utc)
    for grant in grants:
        if not is_kind(grant):
            continue
        try:
            if now < parse_stamp(grant.get("expires_at")):
                return True
        except Exception:
            continue
    return False


def _under(path: Path, roots: tuple[str, ...]) -> bool:
    p = str(path).lower()
    return any(p == r or p.startswith(r.rstrip(os.sep) + os.sep) for r in roots)


def levain_package_dir() -> str:
    """The directory of the levain package this interpreter imports (realpath'd): the installed hook script runs
    ``levain.consent.adapters.claude_code`` from it, so an edit there changes what the consent hook does."""
    import levain
    return os.path.realpath(os.path.dirname(levain.__file__))


def import_site_dirs() -> tuple[str, ...]:
    """This interpreter's site directories (realpath'd), where the entries that redirect ``import levain`` live."""
    dirs = [sysconfig.get_path("purelib"), sysconfig.get_path("platlib")]
    if site.ENABLE_USER_SITE:
        dirs.append(site.getusersitepackages())
    return tuple({os.path.realpath(d).lower() for d in dirs if d})


def user_site_roots(home: Path, *, environ: Mapping[str, str]) -> tuple[str, ...]:
    """The operator's Python user-site trees :func:`in_user_site` protects, lowercased, each as written and resolved:
    the floor's own derivation (``levain.firing.confinement.python_user_site_roots``: ``~/.local/lib``, macOS's
    ``~/Library/Python``, an absolute ``$PYTHONUSERBASE``'s ``lib``), for ``home`` (where a ``~`` in a target
    expands) and for :func:`operator_home`, each only when absolute. Whatever THIS interpreter's
    ``site.ENABLE_USER_SITE`` says: a venv's is False, and the operator's other, non-venv Pythons still run a
    ``.pth``, ``usercustomize.py`` or package planted there. ``environ`` names the operator (:func:`operator_home`)
    and any ``$PYTHONUSERBASE``. Kept apart from :func:`import_site_dirs`, which answers only what redirects
    this interpreter's ``import levain``."""
    from levain.firing.confinement import python_user_site_roots  # lazy: keep this module light

    out: dict[str, None] = {}
    for h in dict.fromkeys((Path(home), operator_home(environ))):
        if not h.is_absolute():   # an empty pw_dir is ``.``: no tree, never one relative to where the hook runs
            continue
        for root, _label in python_user_site_roots(h, environ)[0]:
            for spelled in (os.path.normpath(root), os.path.realpath(root)):
                out[spelled.lower()] = None
    return tuple(out)


def in_user_site(path: Path, roots: tuple[str, ...]) -> bool:
    """``path`` (one spelling, normalised or resolved, as :func:`_tags_path` takes it) is one of ``roots``
    (:func:`user_site_roots`) or lies under one."""
    p = str(path).lower()
    return any(p == r or p.startswith(r.rstrip(os.sep) + os.sep) for r in roots)


def _redirects_the_import(path: Path, sites: tuple[str, ...]) -> bool:
    """A site-directory entry that decides what ``import levain`` loads: a ``levain`` package or module, a
    ``levain-*.dist-info``, an ``__editable__`` levain finder, or any ``.pth`` / ``sitecustomize.py`` /
    ``usercustomize.py`` (run at interpreter start, whatever their name). Nothing else in site-packages."""
    p = str(path).lower()
    for s in sites:
        prefix = s.rstrip(os.sep) + os.sep
        if not p.startswith(prefix):
            continue
        first, _, rest = p[len(prefix):].partition(os.sep)
        if first in ("levain", "levain.py") or (first.startswith("levain-") and first.endswith(".dist-info")):
            return True
        if first.startswith("__editable__") and "levain" in first:
            return True
        if not rest and (first.endswith(".pth") or first in ("sitecustomize.py", "usercustomize.py")):
            return True
    return False


def targets_agent_config(tool_name: str, tool_input: Any, cwd: str, *, home: Path, environ: Mapping[str, str],
                         protected: Iterable[Path] = (), git_cache: dict[Any, Any] | None = None) -> bool:
    """True when a file tool (Edit/Write/MultiEdit/NotebookEdit) or an MCP tool targets an agent's config or hooks,
    in the workspace or the home directory: a ``.claude/settings*.json``; anything under a ``.claude/`` ``hooks``,
    ``agents``, ``skills``, ``commands`` or ``plugins`` directory (the adapter's own installed hook script is in
    ``.claude/hooks/``, the one `levain init` wires under the install's ``activation/hooks/``, below); a ``.mcp.json``, or the home directory's ``.claude.json``; a ``.codex/hooks.json`` or
    ``.codex/config.toml``; git's config and hooks, by path and never by parsing git's config: anything under a
    ``.git`` directory but its objects, refs, reflogs and state files (:func:`_is_git_config`; its ``config``,
    ``hooks/``, ``info/``, a linked worktree's or a submodule's git directory), a file named ``.git`` (a gitfile,
    which names the git directory), anything under the directory git itself answers it runs hooks from for the
    repository of ``cwd`` or of the target (:func:`_under_hooks_path`: ``core.hooksPath`` wherever it points), and
    the user's git config files (:func:`global_git_configs`), never ``.gitignore`` or ``.gitattributes``; anything under a ``.claude-plugin/`` directory, or
    under ``hooks/`` beside one (a plugin); anything under the levain package this interpreter imports (:func:`levain_package_dir`, the hook runs
    it) or a site-directory entry that redirects that import (:func:`_redirects_the_import`); anything in an
    operator's Python user site, whatever this interpreter's own flag (:func:`in_user_site`); or any of
    ``protected`` (a file, or anything under a directory; the adapter passes :func:`protected_paths`). A file
    tool's target is its ``file_path``/``notebook_path``; an MCP tool's are its :data:`MCP_PATH_FIELDS` and every
    other string in its input (:func:`levain.consent.policy.mcp_config_candidates`: a server can name its path in a
    field of any name), each also tagged when it names a config directory or ``.claude.json``
    (:func:`names_config_dir`), and an input past that function's bound is tagged. Each path is checked as written (``~`` expanded, joined to ``cwd``, normalised) and as resolved through
    symlinks; either match tags it. A target that is not a non-empty string is tagged (for a file tool, so is a
    missing one). Other tools are never tagged: Bash is not parsed (ruling 1). ``git_cache`` is one gate call's,
    shared across the calls a caller makes for it (Codex's patch headers), so git is asked once per repository within
    one budget (:func:`_git_answer`)."""
    targets = _targets(tool_name, tool_input)
    if targets is None and not tool_name.startswith("mcp__"):
        return False
    targets = targets or []
    extra: list[str] = []
    if tool_name.startswith("mcp__"):
        got = mcp_config_candidates(tool_input)
        if got is None:
            return True
        extra = got
        targets = [*targets, *extra]
    try:
        roots = tuple({s for p in protected for s in (os.path.normpath(str(p)).lower(), os.path.realpath(p).lower())}
                      | {levain_package_dir().lower()})
        sites = import_site_dirs()
        user_roots = user_site_roots(home, environ=environ)
        git_cache = git_cache if git_cache is not None else {}   # git asked once per repository, in one budget, per call
        home = Path(home)
        # by full path: the home's ``.claude.json`` and the user's git config files (``~/.gitconfig``,
        # ``~/.config/git/config``, the XDG one), which every repository reads executables from (L3 r1 G1c, r2 G1')
        user_configs = frozenset({s for f in (home / USER_CONFIG_NAME, *global_git_configs(home, environ=environ))
                                  for s in (os.path.normpath(f).lower(), os.path.realpath(f).lower())})
    except (OSError, ValueError, ImportError, TypeError, KeyError):
        return True
    for target in targets:
        if not isinstance(target, str) or not target:
            return True
        raw = Path(str(home) + target[1:]) if target == "~" or target.startswith("~/") else Path(target)
        if not raw.is_absolute():
            raw = Path(cwd) / raw
        written = Path(os.path.normpath(raw))
        try:
            resolved = Path(os.path.realpath(raw))
        except (OSError, ValueError):
            return True
        if (_tags_path(written, cwd, roots, sites, user_configs, git_cache, environ=environ)
                or _tags_path(resolved, cwd, roots, sites, user_configs, git_cache, environ=environ)
                or in_user_site(written, user_roots) or in_user_site(resolved, user_roots)):
            return True
    try:
        return any(names_config_dir(target, cwd, home, roots, sites, user_configs, git_cache, user_roots=user_roots,
                                    environ=environ)
                   for target in extra)
    except (OSError, ValueError):
        return True


# --- the decision ------------------------------------------------------------------------------------------

def _verified(decision: Any, request: ExternalGateRequest) -> Decision | None:
    """``decision`` only if it is the core's Decision for THIS request with a known verdict, over the digest
    of the request this hook filed (``Decision.request_digest`` is required since Q1 round 2)."""
    if not (isinstance(decision, Decision) and decision.request_id == request.id
            and decision.decision in ("allow", "deny") and decision.request_digest == request.digest):
        return None
    return decision


def hook_output(decision: Decision) -> dict[str, Any]:
    """What Claude Code reads from a ``PermissionRequest`` hook for a verified decision. A grant's allow also
    carries a ``systemMessage`` naming the grant, so an answer the operator did not give in the moment says where
    it came from. Claude Code decides whether to display it (a real 2.1.300 session showed only its own "Allowed by
    PermissionRequest hook", Q4 RUN 2026-10-15); the receipt is the record."""
    verdict: dict[str, Any] = {"behavior": decision.decision}
    if decision.decision == "deny":
        verdict["message"] = f"Denied by a signed levain answer (key {decision.signer_fp})."
    out: dict[str, Any] = {"hookSpecificOutput": {"hookEventName": EVENT, "decision": verdict}}
    if decision.via == "grant":
        out["systemMessage"] = (f"levain: allowed by your standing consent grant {decision.grant_id} "
                                f"(posture {decision.posture}); `levain consent grant revoke {decision.grant_id}` "
                                "ends it.")
    return out


def _can_sign(settings: AdapterSettings) -> bool:
    """The cockpit the settings name replays to at least one active key, so a held request can get a signed
    answer."""
    root = settings.cockpit_data_root
    if not root or not (Path(root) / "cockpit.db").is_file():
        return False
    try:
        return bool(authority_source(Path(root) / "cockpit.db", anchor=anchor_path(root))().active_keys())
    except Exception:
        return False  # an authority that does not replay verifies nothing


def effective_away(settings: AdapterSettings, idle: Callable[[], float | None] | None = None
                   ) -> tuple[bool, str, str]:
    """:func:`levain.consent.auto_away.away_state`, except that away by input idle is away only while an enrolled
    key could sign an answer (:func:`_can_sign`; source ``idle_unsignable`` otherwise): the explicit word on
    checks that when it is set, nothing sets input idle. Read at a hold's start and at every recheck, and by
    ``levain consent away``."""
    away, source, why = away_state(settings, idle)
    if away and source == "idle" and not _can_sign(settings):
        return False, "idle_unsignable", f"{why}, but no enrolled key could sign an answer"
    return away, source, why


def away_hold(settings: AdapterSettings, agent: str, idle: Callable[[], float | None] | None = None) -> float:
    """Seconds the hook holds a request for a signed answer under ``settings``: 0 unless :func:`effective_away`
    says away (``idle`` is its idle source)."""
    away, source, _why = effective_away(settings, idle)
    return hold_seconds(settings, agent, source) if away else 0.0


def store_unreadable(queue_dir: Path) -> str:
    """What the operator is told when the consent queue store at ``queue_dir`` cannot be read."""
    return (f"consent store unreadable at {Path(queue_dir) / DB_NAME}: levain can neither hold nor file requests "
            "until it opens")


def away_mark_of(queue_dir: Path, settings: AdapterSettings, agent: str, *, queue: Any = None) -> AwayMark | None:
    """``agent``'s latest away mark in the queue store at ``queue_dir``, its leave re-verified against the authority
    ``settings`` name (:meth:`ConsentQueue.away_mark`); ``None`` when there is no store or no mark. Read through
    ``queue`` when it is a :class:`ConsentQueue` (the hook's own, whose schema is checked once, and which verifies
    a stored leave once), else from the store as it is, never written (a store of the prior format: no mark).
    Raises when the store cannot be read."""
    if not os.path.lexists(Path(queue_dir) / DB_NAME):
        return None
    if isinstance(queue, ConsentQueue):
        return queue.away_mark(agent)
    # a queue opened here reads only the mark (never a grant), so the home it holds decides nothing: the caller's
    # process home, as before (not the core path: the hooks and the chat core pass their own ``queue``)
    return _queue_for(Path(queue_dir), settings, home=Path.home()).away_mark(agent, as_is=True)


def with_away_mark(settings: AdapterSettings, queue_dir: Path, agent: str, *, queue: Any = None) -> AdapterSettings:
    """``settings`` as the hook acts on them: when ``agent``'s latest away mark has no leave that verifies
    (:func:`away_mark_of`, through ``queue`` when given), away mode is on by the operator's word whatever the file
    says (``away_marked``), holding the file's hold when it is above zero, else the agent's :data:`DEFAULT_HOLDS`.
    A store that cannot be read counts as such a mark, never as away off (``away_unreadable``: why, in
    :func:`store_unreadable`'s words). An ``agent`` that is not one of :data:`DEFAULT_HOLDS` has no marks."""
    if agent not in DEFAULT_HOLDS:
        return settings
    unreadable = ""
    try:
        mark = away_mark_of(queue_dir, settings, agent, queue=queue)
    except Exception:  # noqa: BLE001 - an unreadable store is never read as away off
        mark, unreadable = AwayMark(id="", agent=agent, set_at="", left=False), store_unreadable(queue_dir)
    if mark is None or mark.left:
        return settings
    hold = settings.away_hold_seconds if settings.away_hold_seconds > 0 else DEFAULT_HOLDS[agent]
    return replace(settings, away_mode=True, explicit_off=False, legacy=False, away_marked=True,
                   away_unreadable=unreadable, away_hold_seconds=hold)


def still_away_from(path: Path, started: AdapterSettings, *, agent: str | None = None, queue: Any = None
                    ) -> Callable[[], tuple[bool, str, str]]:
    """The recheck a hook entry point holds with: :func:`effective_away` of a fresh read of the settings file at
    ``path``, or of ``started`` (what the hold started with) when that read fails, with the away mark of ``agent``
    (default: the file's name without ``.json``) read again from the store beside it (:func:`with_away_mark`,
    through the hook's ``queue`` when given, so a recheck tick takes no write lock and reuses the leave verified at
    the hold's start). Known edge: the hook's ``queue`` verifies leaves against the cockpit the settings named when
    it opened, so a ``cockpit_data_root`` changed during a hold takes effect at the next hook call."""
    agent = agent if agent is not None else path.stem

    def recheck() -> tuple[bool, str, str]:
        fresh = try_read_settings_file(path)
        return effective_away(with_away_mark(fresh if fresh is not None else started, path.parent, agent,
                                             queue=queue))
    return recheck


# While a request is held, away mode is read again this often: an operator who comes back (input, or the word off)
# gets the agent's own dialog within about this long, plus one idle read (levain.consent.auto_away.IDLE_READ_TIMEOUT)
RECHECK_EVERY = 1.0


def hold_for_answer(queue: ConsentQueue, request: ExternalGateRequest, wait: float, *, tool_input: Any,
                    posture: str | None, still_away: Callable[[], tuple[bool, str, str]] | None = None,
                    ) -> tuple[Decision | None, str | None]:
    """Wait up to ``wait`` seconds for a verified decision on ``request``: ``(decision, None)``, or
    ``(None, why)`` when ``still_away`` (re-read every :data:`RECHECK_EVERY` seconds) says away mode ended (the
    operator is back, or no key could sign), ``why`` its reason, or ``(None, None)`` when the wait ran out or the
    request stopped being pending. With no
    ``still_away`` it is one :meth:`ConsentQueue.await_decision` of ``wait``. The wait can run past ``wait`` by one
    ``still_away`` call."""
    if still_away is None:
        return _verified(queue.await_decision(request.id, wait, tool_input=tool_input, posture=posture), request), \
            None
    end = time.monotonic() + wait
    while True:
        left = end - time.monotonic()
        if left <= 0:
            return None, None
        piece, started = min(left, RECHECK_EVERY), time.monotonic()
        got = _verified(queue.await_decision(request.id, piece, tool_input=tool_input, posture=posture), request)
        if got is not None:
            return got, None
        if time.monotonic() - started < piece or end - time.monotonic() <= 0:
            # await_decision returns before its timeout only when the request is no longer pending
            return None, None
        away, _source, why = still_away()
        if not away:
            return None, why


# --- the core result (CH-D1 §3.4): decide_ex tags every exit; decide() is its hook output -------------------

@dataclass(frozen=True)
class Decided:
    """A verified answer for ``call``: ``decision`` passed :func:`_verified` against ``request`` (the request filed
    for the call, or on the take path the request the take's decision names)."""
    call: CoreCall
    request: ExternalGateRequest
    decision: Decision


@dataclass(frozen=True)
class Abstained:
    """No answer: nothing could decide (``no_decider``, nothing filed, ``request`` ``None``, ``why`` the reason), the
    gate's allow was taken but the request it names could not be read, twice (``take_unread``, nothing filed,
    ``request`` ``None``, ``why`` the take's request id), or the hold ended without one (``hold_expired``: a zero hold,
    a timeout, away mode ending, a lost withdraw race; ``request`` the filed request, ``why`` ``""``). Each becomes
    the operator's own question."""
    call: CoreCall
    reason: Literal["no_decider", "hold_expired", "take_unread"]
    request: ExternalGateRequest | None
    why: str


@dataclass(frozen=True)
class Declined:
    """A call levain refuses outright: in chat each becomes a DENY carrying :data:`DECLINE_MESSAGES` [reason],
    never a question. ``call`` is ``None`` only when the event was too malformed to name one."""
    call: CoreCall | None
    reason: Literal["invalid_event", "unconfined", "sandbox_escape_requested", "egress_not_levain",
                    "contained_launch_mismatch"]


DECLINED_REASONS = frozenset({"invalid_event", "unconfined", "sandbox_escape_requested", "egress_not_levain",
                              "contained_launch_mismatch"})
DECLINE_MESSAGES: Mapping[str, str] = MappingProxyType({
    "invalid_event": "levain could not read this request",
    "unconfined": "levain refuses agent-config writes from an unconfined session",
    "sandbox_escape_requested": "no sandbox escape in chat: run the command again without dangerouslyDisableSandbox",
    "egress_not_levain": "this launch's network is not the one levain built, so levain opens nothing for it",
    "contained_launch_mismatch": "levain could not match this call to the launch it came from"})

CoreResult = Decided | Abstained | Declined



def decide(event: Any, queue: ConsentQueue, settings: AdapterSettings, *, environ: Mapping[str, str], home: Path,
           claude_pid: int | None = None, **kw: Any) -> dict[str, Any] | None:
    """The hook's output for one event, or ``None`` for no decision: :func:`hook_output` of a :class:`Decided`
    result of :func:`decide_ex` (called with ``launch_nonce=""`` and ``contained_launch=None``), else ``None``.
    Raises what :func:`decide_ex` raises; the caller (:func:`main`) turns that into no decision."""
    out = decide_ex(event, queue, settings, environ=environ, home=home, claude_pid=claude_pid, launch_nonce="",
                    contained_launch=None, **kw)
    return hook_output(out.decision) if isinstance(out, Decided) else None


def decide_ex(event: Any, queue: ConsentQueue, settings: AdapterSettings, *, environ: Mapping[str, str], home: Path,
              launch_nonce: str, contained_launch: ContainedLaunchProof | None,
              claude_pid: int | None = None, max_hold: float = DEFAULT_MAX_HOLD,
              policy: Policy | None = None, clock: Callable[[], float] = time.monotonic,
              idle: Callable[[], float | None] | None = None,
              still_away: Callable[[], tuple[bool, str, str]] | None = None) -> CoreResult:
    """The core's result for one event, tagged (:data:`CoreResult`), every result bound to the :class:`CoreCall`
    :func:`core_call_of` computes from ``event``. ``launch_nonce`` is ``""`` for every hook caller and the launch's
    spawn nonce for a chat call; ``contained_launch`` is ``None`` for every hook caller and the launch's SH1
    capability (or ``None``, withheld) for a chat call. Both are required, so no caller can leave either out.
    Raises on anything unexpected (after withdrawing the request it filed, so no ghost stays pending).
    ``max_hold`` is a total deadline: the wait ends :data:`CLEANUP_RESERVE` seconds before it. The hold is
    :func:`away_hold` (``idle`` is its input-idle source, default the platform's); ``still_away`` (:func:`main`
    passes :func:`still_away_from`) ends a hold early when it says away mode ended (the operator is back, or no
    key could sign), leaving the native dialog to ask. ``policy``
    (default :func:`levain.consent.policy.decide`) is only ever reached through :func:`guarded_policy`, called with
    the event's ``permission_mode`` as ``posture`` (``None`` when absent: no grant matches) and the tool input the
    request was filed for, and never for a request filed at :data:`AGENT_CONFIG_TIER`: a direct caller cannot
    bypass ruling 3b item 1. What
    the adapter trusts (:func:`protected_paths`) is taken from ``queue.directory``, ``settings``, ``home`` and the
    event's cwd here, never as a list from the caller, so no call can leave it out; a queue without a directory
    raises before anything is filed. A ``contained_launch`` that is not of the right shape, or whose spawn nonce is
    not ``launch_nonce`` (a hook caller's ``""`` included), is ``Declined(contained_launch_mismatch)`` before
    anything is filed. A chat call (``launch_nonce`` not ``""``) is declined before anything is
    filed when it has no ``tool_use_id``, when it asks to leave its sandbox
    (:func:`~levain.consent.contained.sandbox_escape_requested`: a Bash call with the :data:`SANDBOX_ESCAPE_KEY` key,
    any value) and when it is :data:`SANDBOX_NET_TOOL` (:func:`~levain.consent.contained.sandbox_net_tool`, any
    input); each name is also matched case-folded and namespaced; a hook caller's call is never declined for these.
    A gate allow the take spent whose request cannot be read on a second try is ``Abstained(take_unread)``,
    nothing filed.
    A chat call's request is filed with ``event_source="chat"`` (its own ``chat:`` event id); a hook caller's is
    filed exactly as before."""
    if getattr(queue, "directory", None) is None:
        raise TypeError("the queue has no directory: the adapter's own files cannot be protected")
    if not isinstance(launch_nonce, str):
        raise TypeError("launch_nonce must be a string (\"\" for a hook caller)")
    if contained_launch is not None and not isinstance(contained_launch, ContainedLaunchProof):
        raise TypeError("contained_launch must be a ContainedLaunchProof or None")
    deadline = clock() + max(0.0, float(max_hold)) - CLEANUP_RESERVE
    if not isinstance(event, Mapping) or event.get("hook_event_name") != EVENT:
        return Declined(None, "invalid_event")
    tool_name, tool_input, cwd = event.get("tool_name"), event.get("tool_input", {}), event.get("cwd")
    session = event.get("session_id")
    if not isinstance(tool_name, str) or not tool_name or not isinstance(cwd, str) or not cwd \
            or not isinstance(session, str) or not session:
        return Declined(None, "invalid_event")
    if tool_input is None:
        tool_input = {}  # an MCP tool with no arguments arrives without tool_input (Q0 item 4)
    try:
        call = core_call_of(event, launch_nonce=launch_nonce)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        # a chat call with no tool_use_id, or an input the digest cannot take: a non-string key, a cycle, a nesting
        # deeper than the interpreter's stack (L3 r1 codex MED)
        return Declined(None, "invalid_event")
    if contained_launch is not None and (not launch_nonce or contained_launch.problem() is not None
                                         or contained_launch.spawn_nonce != launch_nonce):
        # the proof is this launch's or none: never another launch's, never on a hook call (L3 r1 codex HIGH)
        return Declined(call, "contained_launch_mismatch")
    if launch_nonce:
        if sandbox_escape_requested(tool_name, tool_input):
            return Declined(call, "sandbox_escape_requested")
        if sandbox_net_tool(tool_name):
            return Declined(call, "egress_not_levain")
    session = call.session
    tool_use_id = event.get("tool_use_id")
    mode = event.get("permission_mode")
    posture = mode if isinstance(mode, str) and mode else None
    # the one call an SH1 grant can answer (policy.contained_shell_refusal: exactly Bash, a chat call, a proof)
    sh1 = {"contained_shell": True} if launch_nonce and contained_launch is not None and tool_name == "Bash" else {}
    if policy is None and nothing_can_decide(settings, **sh1):
        return Abstained(call, "no_decider", None, nothing_can_decide_why(settings, **sh1) or "nothing can decide")
    protected = protected_paths(Path(queue.directory), settings, home=home, environ=environ, cwd=cwd)
    agent_config = targets_agent_config(tool_name, tool_input, cwd, home=home, environ=environ, protected=protected)
    if agent_config and unconfined(cwd, home=home, environ=environ):
        # an unconfined session (Q10 D2): its dialog answers, as the gate left the call to its mode (L1-7)
        return Declined(call, "unconfined")
    if agent_config:
        # the gate already asked the operator about this call and delivered an allow, and Claude Code shows its
        # dialog anyway on a path it protects: deliver that allow once, never a second question. The take is
        # delivered only as the verified decision of the request it names (R7-9); any other take is no take.
        took = queue.take_gate_allow(AGENT, session, tool_name, tool_input, cwd=cwd, within=GATE_ALLOW_REUSE,
                                     posture=posture, event=EVENT)
        if took is not None:
            got_row = queue.get(took.request_id) if isinstance(took, Decision) else None
            if got_row is None and isinstance(took, Decision):
                # the take already spent the gate's allow: read once more, and if the request is still not there,
                # file nothing (a second row would ask the operator again for a call they allowed: L3 r1 deepseek)
                got_row = queue.get(took.request_id)
                if got_row is None:
                    return Abstained(call, "take_unread", None, took.request_id)
            if got_row is not None:
                taken = _verified(took, got_row[0])
                if taken is not None:
                    return Decided(call, got_row[0], taken)
    hold = min(away_hold(settings, AGENT, idle), max(0.0, deadline - clock()))
    request = queue.file(agent=AGENT, agent_session_id=session, tool_name=tool_name, tool_input=tool_input, cwd=cwd,
                         hold=max(timedelta(seconds=hold), MIN_FILE_HOLD),
                         tier=AGENT_CONFIG_TIER if agent_config else DEFAULT_TIER,
                         event_id=tool_use_id if isinstance(tool_use_id, str) and tool_use_id else None,
                         resume=resume_meta_of(event, environ=environ, claude_pid=claude_pid),
                         model=event.get("model"), **({"event_source": "chat"} if launch_nonce else {}))
    settled = False
    try:
        got: Decision | None = None
        if not agent_config and request.tier != AGENT_CONFIG_TIER:
            got = _verified(guarded_policy(request, queue, posture=posture, tool_input=tool_input, policy=policy),
                            request)
        wait = min(hold, max(0.0, deadline - clock()))
        back = None
        if got is None and wait > 0:
            got, back = hold_for_answer(queue, request, wait, tool_input=tool_input, posture=posture,
                                        still_away=still_away)
        if got is None:
            note = f"away mode ended ({back}); the native dialog stands" if back else \
                "the hook stopped waiting; the native dialog stands"
            # `settled` is set only after this withdraw returns: if the withdraw itself raises, the finally retries it
            if not queue.withdraw(request.id, note=note):
                # Not pending any more: an answer may have landed between the wait and the withdraw.
                got = _verified(queue.decision(request.id, tool_input=tool_input, posture=posture), request)
        settled = True
        return Decided(call, request, got) if got is not None else Abstained(call, "hold_expired", request, "")
    finally:
        if not settled:
            # An exception between file() and the withdraw: withdraw now so no ghost request stays answerable
            # after the hook has gone. Its own failure is swallowed; the original exception propagates.
            try:
                queue.withdraw(request.id, note="the hook failed; the native dialog stands")
            except Exception:
                pass


# --- the PreToolUse gate (Q14 B2) ----------------------------------------------------------------------------
#
# PermissionRequest fires only when Claude Code is about to show its dialog, so it never fires in bypass or auto
# (DESIGN §2.2, measured). PreToolUse fires in every mode and can deny with a reason the agent reads (§2.3). So a
# gated call (T3: agent config and hooks, :func:`targets_agent_config`) is held here, in every mode where no dialog
# would ask. ``default`` is held too: an allow rule (``Edit``) runs a T3 call that Claude Code does not itself
# protect (a hook script) with no dialog and no PermissionRequest (measured 2.1.302, B2 fix lane M1). ``plan`` runs
# nothing and gets no decision. When the gate allowed a call and Claude Code shows its own dialog anyway (a path it
# protects, ``.claude/settings.json``), the PermissionRequest hook delivers that same allow once
# (:meth:`ConsentQueue.take_gate_allow`), so the operator is asked once:
#
# 0. a session started with ``LEVAIN_UNCONFINED=1`` (:data:`UNCONFINED_ENV`, Q10 D2) gets no decision: the mode
#    decides, and no ruled allow is spent; unless a settings file the session can load sets the variable
#    (:func:`unconfined_state`), which is then ignored;
# 1. a ruled allow for this exact call (R-d) runs it, once (:meth:`ConsentQueue.take_ruled_allow`);
# 1a. else, when no enrolled key can sign it (:func:`nothing_can_decide` at T3): in ``default``
#    (:data:`ATTENDED_POSTURES`) no decision (the dialog, or the operator's own allow rule, decides); in every other
#    mode, an absent or unknown one included, the T3 FLOOR (Q10 D1): a plain deny with :data:`FLOOR_TEXT`, nothing
#    filed (no hard stop: nothing is undecided, L2-2);
# 2. else the request is filed with ``expires_at`` = now + D (the library owns the deadline, B1 fix 3), the operator
#    is pinged through the notify seam (R-f; a failed ping never fails the gate), and the hook waits up to D for a
#    verified decision, which it delivers;
# 3. at D, :meth:`ConsentQueue.mark_undecided` gives the request its gap number and the hook denies with the
#    UNDECIDED text (DESIGN §3.3). The irreversible tier (T3 here; the push executor's tool is B6's,
#    :func:`gate_class`) adds top-level ``"continue": false`` + ``stopReason``: words do not stop an agent, this does
#    (runs2 RUN B, 3/3 ``terminal_reason: hook_stopped``).
#
# Every PreToolUse also carries this session's late rulings, not yet delivered, as ``additionalContext`` (DESIGN
# §3.6; :meth:`ConsentQueue.claim_rulings_for`), its question answers not yet delivered (Q17,
# :func:`levain.consent.band.question_context`), and, when a notify command is configured, the snoozed re-pings that
# are due (R-e). The entry is installed with ``"onFailure": "block"`` and ``timeout`` = :data:`GATE_TIMEOUT`, the
# timeout of the CEILING, never of the D set at install: D is re-read from the settings file at every run, so a D
# raised after install would otherwise outlive a timeout frozen at the old one (Claude Code would kill the hook
# mid-hold: the request left pending, no UNDECIDED text, no gap). The hold is clamped to the ceiling here too, so it
# always ends :data:`GATE_TIMEOUT_MARGIN` before the timeout. The cost: a gate that hangs (not one that holds) keeps
# the call blocked up to that timeout, not D + the margin.

GATE_EVENT = "PreToolUse"
# Claude Code's ``timeout`` for the gate entry is D + this margin (interpreter start, the undecided mark)
GATE_TIMEOUT_MARGIN = 30
# a PreToolUse that took longer than this held (or waited on a store): it leaves its ledger fill to the next hook,
# Stop or reconcile, so the fill never eats into the margin
GATE_FILL_WITHIN = 5.0
# how long after the gate's allow the dialog's PermissionRequest may take it: that hook follows in under a second
# (an interpreter start + a replay); this bounds how long a later identical call can ride the old allow
GATE_ALLOW_REUSE = timedelta(seconds=5)
# how often the hold re-reads the request
GATE_POLL = 0.25
# the longest the hold sleeps between its wait and the undecided mark
GATE_SLEEP_CAP = 6.0
# DESIGN §3.3, verbatim (U-REV with its bundling clause): the reversible tier; the agent may work around the gap
U_REV = ("levain: UNDECIDED, not denied. The operator did not answer in time, so the action was NOT taken: {x}. "
         "This is not a rejection of your approach; it is open as gap #{n} and the operator will rule on it later. "
         "Do not retry it and do not achieve it another way. If your command had several steps, none of them ran; "
         "redo only the parts that do not depend on it. Continue only work that does not depend on it. If the rest "
         "of the task depends on it, stop cleanly: write a short summary of what is done, what waits on gap #{n}, "
         "and end your turn.")
# The irreversible tier's text, verbatim as measured (runs/setup.py UIRR); on Claude Code the stop itself is
# ``continue: false``, never these words
U_IRR = ("levain: UNDECIDED, not denied. The operator did not answer in time, so the action was NOT taken: {x}. "
         "This class of action stops the task: do no further work. Do not retry it or achieve it another way. "
         "Write a short summary of what is done and what waits on gap #{n}, and end your turn.")
# Q10 D1: the plain deny (no hard stop: nothing is undecided) for a gated call when no enrolled key can sign it and
# no dialog asks. It names no way around the floor: the agent is told to ask, and the operator reads in
# `levain doctor` how to allow it
FLOOR_TEXT = ("levain: the action was NOT taken: {x}. Agent config and hooks are the operator's, and no enrolled key "
              "can approve this change, so it cannot be asked for. Do not retry it and do not achieve it another "
              "way, including through the shell. Ask the operator: `levain doctor` shows them how to allow it.")
# added to FLOOR_TEXT when the session's environment says unconfined but a settings file sets that (:func:`unconfined`)
UNCONFINED_IGNORED_TEXT = (" The session's environment asks for an unconfined session, but that was ignored: a "
                           "settings file this session can load sets it, and only the operator's own shell can "
                           "start an unconfined session.")
# Q10 D2: a session started with this set to "1" in its environment is unconfined: the gate passes its gated calls
# and the session's mode decides. Read from the hook's own environment at every call (the agent's launch env)
UNCONFINED_ENV = "LEVAIN_UNCONFINED"
# the modes the gate treats as attended: only ``default`` (Codex sends ``default`` with approvals on). With no key
# that can sign T3 the gate leaves the call to Claude Code's dialog there, and holds only while the operator is
# away. acceptEdits is NOT attended: it auto-accepts workspace edits Claude Code does not itself protect (measured
# 10-19, Q10 run 4 case C5: a Write of ``.codex/config.toml`` ran with no question), so it is floored/held like any
# no-dialog mode
ATTENDED_POSTURES = ("default",)
# the machine-wide settings every session loads too (L3 r1 G3): Codex's system config, Claude Code's managed
# settings (macOS, Linux). A missing one sets nothing
SYSTEM_SETTINGS_FILES: tuple[tuple[str, Path], ...] = (
    ("toml", Path("/etc/codex/config.toml")),
    ("json", Path("/Library/Application Support/ClaudeCode/managed-settings.json")),
    ("json", Path("/etc/claude-code/managed-settings.json")),
)


def _settings_files(cwd: str | None, home: Path, codex_home: Path | None, *,
                    environ: Mapping[str, str]) -> list[tuple[str, Path]]:
    """Every settings file an agent session started in ``cwd`` can load an environment from, as (``json`` | ``toml``,
    path): Claude Code's user settings (``$CLAUDE_CONFIG_DIR`` or ``~/.claude``), Codex's ``config.toml`` (its home),
    the machine-wide :data:`SYSTEM_SETTINGS_FILES`, and in ``cwd``, ``$CLAUDE_PROJECT_DIR`` and each of their ancestors up to ``/`` (as written and resolved: Claude
    Code reads a project's settings at its repository root) ``.claude/settings.json``, ``.claude/settings.local.json``
    and ``.codex/config.toml``. Every variable is read from ``environ``."""
    out: list[tuple[str, Path]] = []
    config_dir = environ.get("CLAUDE_CONFIG_DIR")
    for d in ([Path(config_dir)] if config_dir else []) + [home / ".claude"]:
        out.append(("json", d / "settings.json"))
    env_codex = environ.get("CODEX_HOME")
    for d in ([codex_home] if codex_home is not None else []) + ([Path(env_codex)] if env_codex else []) \
            + [home / ".codex"]:
        out.append(("toml", d / "config.toml"))
    out += list(SYSTEM_SETTINGS_FILES)
    for start in (cwd, environ.get("CLAUDE_PROJECT_DIR")):
        if not isinstance(start, str) or not start:
            continue
        for p in {Path(os.path.abspath(start)), Path(os.path.realpath(start))}:
            for d in (p, *p.parents):
                out += [("json", d / ".claude" / "settings.json"), ("json", d / ".claude" / "settings.local.json"),
                        ("toml", d / ".codex" / "config.toml")]
    seen: set[Path] = set()
    return [(k, p) for k, p in out if not (p in seen or seen.add(p))]


def _key_anywhere(value: Any, name: str = UNCONFINED_ENV) -> bool:
    """``name`` (default :data:`UNCONFINED_ENV`) inside any key or string value, at any depth."""
    if isinstance(value, str):
        return name in value
    if isinstance(value, Mapping):
        return any(_key_anywhere(k, name) or _key_anywhere(v, name) for k, v in value.items())
    if isinstance(value, list):
        return any(_key_anywhere(v, name) for v in value)
    return False


def _sets_unconfined(kind: str, path: Path, name: str = UNCONFINED_ENV) -> bool:
    """True when the settings file mentions ``name`` (default :data:`UNCONFINED_ENV`) anywhere (L3 r1 G3: in its raw text, or, once
    parsed, in any key or string value at any depth, which also catches an escaped spelling; JSON or TOML alike,
    an ``env`` block or not) or that cannot be ruled out: an unreadable file, one that does not parse, a JSON file
    that is not an object, a Claude Code ``env`` that is not an object. A missing file sets nothing."""
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return False
    except Exception:  # noqa: BLE001 - unreadable: it cannot be ruled out
        return True
    if name.encode() in raw:
        return True
    try:
        if kind == "toml":
            import tomllib

            return _key_anywhere(tomllib.loads(raw.decode("utf-8")), name)
        data = json.loads(raw)
        if not isinstance(data, Mapping):
            return True
        env = data.get("env")
        return (env is not None and not isinstance(env, Mapping)) or _key_anywhere(data, name)
    except Exception:  # noqa: BLE001 - a file that does not parse cannot be ruled out
        return True


def unconfined_state(cwd: str | None, *, home: Path, environ: Mapping[str, str],
                     codex_home: Path | None = None) -> str | None:
    """``"on"`` when the operator started this session unconfined: :data:`UNCONFINED_ENV` is exactly ``"1"`` in the
    hook's environment and no settings file the session can load sets it (:func:`_settings_files`; the hook cannot
    tell a variable a settings file's ``env`` put there from one the operator's shell did, so a cloned repository's
    ``.claude/settings.json`` could otherwise turn the gate off, L2-1). ``"ignored"`` when it is ``"1"`` but such a
    file sets it, or cannot be ruled out (:func:`_sets_unconfined`). ``None`` when it is not ``"1"``."""
    if environ.get(UNCONFINED_ENV) != "1":
        return None
    files = _settings_files(cwd, home, codex_home, environ=environ)
    return "ignored" if any(_sets_unconfined(kind, path) for kind, path in files) else "on"


def unconfined(cwd: str | None, *, home: Path, environ: Mapping[str, str], codex_home: Path | None = None) -> bool:
    """Whether the operator started this session unconfined (:func:`unconfined_state` is ``"on"``)."""
    return unconfined_state(cwd, home=home, environ=environ, codex_home=codex_home) == "on"


def queue_socket_planted(cwd: str | None, *, home: Path, environ: Mapping[str, str],
                         codex_home: Path | None = None) -> bool:
    """True when a settings file this session can load (:func:`_settings_files`) mentions the slot's broker socket
    variable (:data:`levain.consent.broker_client.SOCKET_ENV`), or that cannot be ruled out (:func:`_sets_unconfined`'s
    rules). A slot's launcher sets it in the agent's environment, never in a settings file, and a hook cannot tell a
    variable a settings file's ``env`` put there from the launcher's: a cloned repository's ``.claude/settings.json``
    could otherwise point the gate at a socket of its own that answers ``allow``. The slot gate denies a gated call
    then (:func:`slot_gate`)."""
    from levain.consent.broker_client import SOCKET_ENV

    return any(_sets_unconfined(kind, path, SOCKET_ENV)
               for kind, path in _settings_files(cwd, home, codex_home, environ=environ))


def floor_text(what: str, state: str | None) -> str:
    """The floor's deny reason for ``what``, with :data:`UNCONFINED_IGNORED_TEXT` when the session asked for
    unconfined and a settings file sets it (``state`` from :func:`unconfined_state`)."""
    return FLOOR_TEXT.format(x=what) + (UNCONFINED_IGNORED_TEXT if state == "ignored" else "")


# DESIGN §3.6, verbatim: a late ruling delivered to its live session
RULING_TEXT = {
    "allow": ("levain: ruling on gap #{n}: ALLOW. Do {x} now, it will be allowed. Then list what you skipped or "
              "deferred because of gap #{n}, and do it now (or undo what assumed {x}); check the work since the gap "
              "for anything inconsistent with this ruling and fix it."),
    "deny": ("levain: ruling on gap #{n}: DENY. Do not do {x}. Then list what you skipped or deferred because of "
             "gap #{n}, and do it now (or undo what assumed {x}); check the work since the gap for anything "
             "inconsistent with this ruling and fix it."),
}
# the longest a description of the action runs in a reason, a ping or a ruling
WHAT_MAX = 300
# C0 controls and DEL, each shown as "?" in a description (a newline or an escape sequence never reaches a ping)
_CONTROL = re.compile("[\x00-\x1f\x7f]")


def gate_timeout(hold: float) -> int:
    """Claude Code's ``timeout`` for a gate entry whose hold is at most ``hold``: ``ceil(hold)`` +
    :data:`GATE_TIMEOUT_MARGIN`."""
    return int(math.ceil(hold)) + GATE_TIMEOUT_MARGIN


# the timeout levain installs the gate entry with: that of the ceiling, the longest hold the hook ever runs
GATE_TIMEOUT = gate_timeout(GATE_HOLD_CEILING)


def gate_class(tool_name: str, tool_input: Any, cwd: str, *, home: Path, environ: Mapping[str, str],
               protected: Iterable[Path] = ()) -> str | None:
    """The gate's tier for a call: ``irreversible`` (T3, agent config and hooks, which is Q10's floor and Q14's
    irreversible tier at once, R-h), ``reversible``, or ``None`` (not gated). Bash is never parsed (ruling 1, R-g).
    THE SEAM for B6: the push executor's tool joins here as ``irreversible``."""
    if targets_agent_config(tool_name, tool_input, cwd, home=home, environ=environ, protected=protected):
        return "irreversible"
    return None


def _what(tool_name: str, tool_input: Any, display: str | None = None) -> str:
    """The action in a few words: ``<tool> of <path>`` for a tool that names the paths it writes, else its display
    (:func:`levain.consent.record.redact_display`), at most :data:`WHAT_MAX` characters. With no ``tool_input``
    (a stored request keeps only its digest), the input is read back from ``display`` when it was not cut."""
    from levain.consent.record import redact_display

    if tool_input is None and display is not None:
        try:
            tool_input = json.loads(display.partition(" ")[2])
        except (ValueError, RecursionError):
            tool_input = None
    paths = _targets(tool_name, tool_input)
    if paths and all(isinstance(p, str) and p for p in paths):
        text = f"{tool_name} of {', '.join(paths)}"
    else:
        text = display if display is not None else redact_display(tool_name, tool_input)
    text = _CONTROL.sub("?", text)  # never a control character into a ping, a reason or a ruling
    if len(text) <= WHAT_MAX:
        return text
    # cut in the MIDDLE: a path's decisive end (``…/.claude/settings.json``) is its tail, and a long benign prefix
    # must not push it out of what the operator reads
    head = WHAT_MAX // 3
    return text[:head] + "…" + text[-(WHAT_MAX - head - 1):]


def ping_configured(settings: AdapterSettings, queue_dir: Path | str | None) -> bool:
    """True when a ping has somewhere to go: a ``notify_argv`` command, or a paired phone with pings on for this
    queue directory (:func:`levain.webpush.live`)."""
    if settings.notify_argv:
        return True
    try:
        from levain.webpush import live

        return live(queue_dir)
    except Exception:  # noqa: BLE001 - a ping is optional
        return False


def notify(settings: AdapterSettings, message: str, *, cwd: Path | str | None = None, kind: str = "consent",
           ref: str | None = None) -> bool:
    """Ping the operator (R-f): start ``notify_argv`` + ``[message]``, detached, never waited on, in ``cwd`` (the
    queue directory from the gate; the home directory when none is given), never the project the agent works in.
    The command is an absolute path or a bare name found on the child's ``PATH`` as an absolute path; a relative
    path (``bin/ping``) is refused, since it would resolve against a directory the agent can write. When ``cwd`` is
    a queue directory, the ping is also written to the notification record (``kind`` and ``ref``, ``request:<id>``
    or ``question:<n>``, name it there) by a detached child, with whether the notify command started, and that
    same child pushes it to each paired phone when one with pings on was live at that moment and
    :func:`levain.webpush.presence.should_push` says so (a ping it holds is recorded and queued for the phone,
    :mod:`levain.webpush.held`, not pushed). With no ``cwd`` there
    is no queue directory, so nothing is recorded and no push is sent.
    True when the notify command or a push started; False when neither is configured or neither could start (a
    command that did not start is still written to the record, as not started). A failed ping never fails the
    gate."""
    started = _notify_argv(settings, message, cwd=cwd)
    pushed = False
    if cwd is not None:
        try:
            from levain.webpush import live, presence, sender

            paired = live(cwd)
            said = presence.should_push(cwd, kind) if paired else None
            phone = said is not None and said.push
            # a ping the rule holds waits in the held queue under the word that muted it, else as "active"
            hold = None if said is None or said.push else (said.by or "active")
            if paired or settings.notify_argv:
                body = message[len("levain: "):] if message.startswith("levain: ") else message
                recorded = sender.spawn(cwd, kind=kind, ref=ref, title="levain", body=body, url="/?view=needs",
                                        command=started if settings.notify_argv else None, push=phone, hold=hold)
                pushed = recorded and phone
        except Exception:  # noqa: BLE001 - a failed ping never fails the gate
            pushed = False
    return started or pushed


def _notify_argv(settings: AdapterSettings, message: str, *, cwd: Path | str | None) -> bool:
    if not settings.notify_argv:
        return False
    import shutil
    import subprocess

    from levain.launch import child_env

    text = message if settings.phone_url is None else f"{message} {settings.phone_url}"
    try:
        # child_env: the allowlisted environment, never the agent session's whole one (its tokens among it)
        env = child_env()
        command = settings.notify_argv[0]
        if "/" not in command:
            command = shutil.which(command, path=env.get("PATH", os.defpath)) or ""
        if not os.path.isabs(command):
            return False  # a relative path, or a bare name not found (or found only through a relative PATH entry)
        subprocess.Popen([command, *settings.notify_argv[1:], text], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                         close_fds=True, env=env, cwd=str(cwd) if cwd is not None else str(Path.home()))
    except Exception:
        return False
    return True


def flush_held_pings(queue: ConsentQueue) -> bool:
    """Start the push of any held phone ping the presence rule now lets through (:func:`levain.webpush.held.flush_if_due`).
    Called with every gate call, so held pings are delivered without a daemon. Never raises."""
    try:
        from levain.webpush import held

        return held.flush_if_due(queue.directory)
    except Exception:  # noqa: BLE001 - a held ping waits for the next call
        return False


def send_repings(queue: ConsentQueue, settings: AdapterSettings) -> int:
    """R-e: claim the snoozed re-pings that are due and send each (only when a ping is configured, a notify command
    or a phone with pings on: a claimed re-ping is never sent again, so one is never claimed with nowhere to send
    it). Returns how many started."""
    if not ping_configured(settings, queue.directory):
        return 0
    sent = 0
    for reping in queue.claim_repings():
        got = queue.get(reping.request_id)
        if got is None:
            continue
        gap = queue.gap(reping.request_id)
        what = _what(got[0].tool_name, None, got[0].display)
        head = (f"levain: asking again, gap {gap_label(gap, got[0].agent_session_id)}" if gap is not None
                else "levain: asking again")
        sent += notify(settings, f"{head}: {what} (request {reping.request_id}).", cwd=queue.directory,
                       kind="reping", ref=f"request:{reping.request_id}")
    return sent


def _pre(decision: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {**extra, "hookSpecificOutput": {"hookEventName": GATE_EVENT, "permissionDecision": decision,
                                            "permissionDecisionReason": reason}}


def _stop(tier: str | None, what: str, why: str) -> dict[str, Any]:
    """The top-level fields that end the session's turn for the irreversible tier; nothing for any other."""
    if tier != "irreversible":
        return {}
    return {"continue": False, "stopReason": f"levain: stopped at a gated action ({what}): {why}"}


def gate_output(decision: Decision) -> dict[str, Any]:
    """What Claude Code reads from the gate for a verified decision (a signed answer, a grant, a ruled allow)."""
    if decision.via == "ruling":
        how = f"your ruling on gap #{decision.gap} (key {decision.signer_fp})"
    elif decision.via == "grant":
        how = f"your standing consent grant {decision.grant_id}"
    else:
        how = f"a signed levain answer (key {decision.signer_fp})"
    return decision_output(decision.decision, how)


def decision_output(decision: str, how: str) -> dict[str, Any]:
    """:func:`gate_output`'s words for ``decision`` (``allow`` or ``deny``) by the authority ``how`` names."""
    if decision == "allow":
        return _pre("allow", f"levain: allowed by {how}.")
    return _pre("deny", f"levain: denied by {how}. Do not retry it and do not achieve it another way.")


def undecided_output(tier: str | None, what: str, gap: int) -> dict[str, Any]:
    """The deny at D: U-IRR and the hard stop for the irreversible tier, U-REV for the reversible one."""
    text = (U_IRR if tier == "irreversible" else U_REV).format(x=what, n=gap)
    return _pre("deny", text, **_stop(tier, what, f"gap #{gap} is open and waits on the operator's ruling."))


def ruling_context(queue: ConsentQueue, session: str) -> str | None:
    """This session's late rulings not yet delivered, as DESIGN §3.6's text, one per line (claimed: each reaches
    the session once), or ``None``. The ruled allow itself still runs only through ``take_ruled_allow``."""
    lines = [line for line in (ruling_line(queue, r) for r in queue.claim_rulings_for(AGENT, session)) if line]
    return "\n".join(lines) or None


def ruling_line(queue: ConsentQueue, ruling: Any, *, quote: bool = False) -> str | None:
    """DESIGN §3.6's text for one claimed ruling (the hook's additionalContext, a resume's prompt), or ``None`` for an
    unknown ruling value (which skips that ruling only, never the rest). With ``quote`` (a resume's prompt, where the
    text arrives as the user's turn) the action is set in «…», which the prompt then names as the agent's own filing."""
    got = queue.get(ruling.request_id)
    if got is not None and got[0].tool_name == "levain_limit":   # Q13: a limit has no action to do
        from levain.consent.limits import ruling_text

        return ruling_text(ruling.ruling, ruling.gap, got[0].display, queue_dir=queue.directory,
                           request_id=ruling.request_id)
    text = RULING_TEXT.get(ruling.ruling)
    if text is None:
        return None
    what = _what(got[0].tool_name, None, got[0].display) if got is not None else f"request {ruling.request_id}"
    if quote:   # the agent wrote ``what``: no closing mark or line break of its own can end the quote early
        what = what.translate({ord(c): None for c in "«»\r\n"})
    return text.format(n=ruling.gap, x=f"«{what}»" if quote else what)


def resume_meta_of(event: Mapping[str, Any], *, environ: Mapping[str, str],
                   claude_pid: int | None) -> dict[str, Any] | None:
    """What a later resume of this hook's session needs (:meth:`ConsentQueue.file`'s ``resume``), or ``None`` when its
    session id is not a UUID (it would be an argument of the resume, :func:`levain.consent.delivery.session_ok`).
    From the hook's payload, the environment Claude Code gives its hooks and the OS: ``-p`` when the entrypoint is an
    SDK one (``sdk-cli`` under ``claude -p``, measured 2.1.302), else ``interactive`` (a ``--bg`` session is not told
    apart here; an ended one of either kind resumes as ``--bg``); the session's start directory
    (``CLAUDE_PROJECT_DIR``, where its transcript is found), its config directory, its process (``CLAUDE_PID``) and
    that process's start time and the option names of its command line as ``ps`` reports them
    (:func:`levain.consent.delivery.process_identity`: the start time tells it from a later process that reused the
    pid; the option names say what a resume would drop; values and the prompt are never recorded). The variables
    are read from ``environ``; ``claude_pid``, when not ``None``, is the process instead of ``CLAUDE_PID``."""
    from levain.consent.delivery import option_names, process_identity, session_ok

    if not session_ok(event.get("session_id")):
        return None
    entry = environ.get("CLAUDE_CODE_ENTRYPOINT", "")
    project = environ.get("CLAUDE_PROJECT_DIR", "")
    cwd = project if os.path.isabs(project) else event.get("cwd")
    mode = event.get("permission_mode")
    config = environ.get("CLAUDE_CONFIG_DIR", "")
    pid: int | None = claude_pid
    if pid is None:
        try:
            pid = int(environ.get("CLAUDE_PID", ""))
        except ValueError:
            pid = None
    pid = pid if pid is not None and pid > 0 else None
    seen = process_identity(pid) if pid is not None else None
    return {"invocation": "-p" if entry.startswith("sdk") else "interactive",
            "permission_mode": mode if isinstance(mode, str) and mode else None,
            "cwd": cwd if isinstance(cwd, str) and cwd else None,
            "config_dir": config if os.path.isabs(config) else None,
            "claude_pid": pid,
            "claude_started": seen[0] if seen and seen[0] else None,
            "claude_flags": option_names(seen[1]) if seen and seen[1] else None}


def _session_of(event: Mapping[str, Any]) -> str | None:
    session = event.get("session_id")
    if not isinstance(session, str) or not session:
        return None
    agent_id = event.get("agent_id")
    return f"{session}#agent:{agent_id}" if isinstance(agent_id, str) and agent_id else session


def gate(event: Any, queue: ConsentQueue, settings: AdapterSettings, *, home: Path, environ: Mapping[str, str],
         sleep: Callable[[float], None] = time.sleep,
         still_away: Callable[[], tuple[bool, str, str]] | None = None) -> dict[str, Any] | None:
    """The gate's output for one ``PreToolUse`` event, or ``None`` (the call goes on as the session's mode says).
    An event with no valid ``tool_name``, absolute ``cwd`` or session id is denied when its tool is unnamed, a file tool or
    an MCP tool (:func:`_malformed`). A gated call (:func:`gate_class`) gets no decision in ``plan`` (nothing runs there, and no ruled allow is spent)
    or in a session started unconfined (:func:`unconfined_state` ``"on"``, nor is a ruled allow spent), else a ruled
    allow (taken once), else, when nothing can decide it (:func:`nothing_can_decide` at T3), no decision in
    ``default`` (:data:`ATTENDED_POSTURES`) and the floor's plain deny in every other mode
    (:func:`floor_text`, nothing filed), else, in every mode (``default`` included), it is filed with its hold = D
    (``gate_hold_seconds``), pinged (:func:`notify`), held for a verified decision (delivered), and at D marked
    undecided and denied (:func:`undecided_output`). In ``default`` the hold runs only while
    the operator is away, and ``still_away``
    (default :func:`still_away_from` the queue directory's settings file) is re-read every :data:`RECHECK_EVERY`
    (:func:`hold_for_answer`): when away ends the request is withdrawn and Claude Code's dialog asks. Anything
    that fails after the call is known gated is a deny (with the hard stop for
    the irreversible tier), never an allow: the request it filed stays pending until a read or sweep expires it.
    Late rulings for the session (:func:`ruling_context`), and the answers to its questions not yet delivered
    (:func:`levain.consent.band.question_context`), ride whatever is returned, as ``additionalContext``."""
    if getattr(queue, "directory", None) is None:
        raise TypeError("the queue has no directory: the adapter's own files cannot be protected")
    if not isinstance(event, Mapping) or event.get("hook_event_name") != GATE_EVENT:
        return None
    flush_held_pings(queue)   # first, so no return below (limits, malformed, a long hold) skips it
    tool_name, tool_input, cwd = event.get("tool_name"), event.get("tool_input", {}), event.get("cwd")
    session = _session_of(event)
    if not isinstance(tool_name, str) or not tool_name or not isinstance(cwd, str) or not os.path.isabs(cwd) \
            or session is None:
        return _malformed(event, queue, settings, home=home)
    if tool_input is None:
        tool_input = {}
    limited = _limits(event, queue, settings, session, tool_name, tool_input, cwd, home=home, environ=environ)
    if limited is not None:
        return limited
    try:
        send_repings(queue, settings)
    except Exception:
        pass  # a re-ping is a ping: it never fails the gate
    if still_away is None:
        still_away = still_away_from(Path(queue.directory) / SETTINGS_NAME, settings, queue=queue)
    out = _gate_call(event, queue, settings, tool_name, tool_input, cwd, session, home=home, environ=environ,
                     sleep=sleep,
                     still_away=still_away)
    try:
        context = ruling_context(queue, session)
    except Exception:
        context = None  # undelivered rulings stay for the next hook
    try:
        from levain.consent.band import question_context

        # a hard stop ends the turn: an answer claimed on it may never be read, so it waits for the next call
        stopped = out is not None and out.get("continue") is False
        answers = None if stopped else question_context(queue.directory, AGENT, session)
    except Exception:
        answers = None  # undelivered answers stay for the next hook
    context = "\n".join(c for c in (context, answers) if c) or None
    if context:
        out = out if out is not None else {"hookSpecificOutput": {"hookEventName": GATE_EVENT}}
        spec = out["hookSpecificOutput"]
        spec["additionalContext"] = f"{spec['additionalContext']}\n{context}" if spec.get("additionalContext") \
            else context
    return out


def _limits(event: Mapping[str, Any], queue: ConsentQueue, settings: AdapterSettings, session: str, tool_name: str,
            tool_input: Any, cwd: str, *, home: Path, environ: Mapping[str, str]) -> dict[str, Any] | None:
    """Q13 at the top of the gate (:func:`levain.consent.limits.check`): every call is counted, gated or not, before
    any of the gate's own exits; ``None`` lets the call on to the gate. A halt or an End ends the turn
    (``continue: false``). A GATED call held at a limit and extended is denied with a retry line, so the gate's own
    hold runs in a fresh hook and the two holds never share one hook's timeout."""
    from levain.consent import limits

    hold = min(settings.gate_hold_seconds, GATE_HOLD_CEILING)
    got = limits.check(AGENT, event, queue, session, tool_name, tool_input, hold=hold, cwd=cwd,
                       notify=lambda text: notify(settings, text, cwd=queue.directory),
                       resume=resume_meta_of(event, environ=environ, claude_pid=None))
    if got.go:
        if got.extended and gate_class(tool_name, tool_input, cwd, home=home, environ=environ,
                                       protected=protected_paths(Path(queue.directory), settings, home=home,
                                                                 environ=environ, cwd=cwd)) is not None:
            return _pre("deny", limits.retry_text(got.extended))
        return None
    stop = {"continue": False, "stopReason": "levain: stopped by a budget the operator set; it goes on when he "
                                             "extends it."} if got.stop else {}
    return _pre("deny", got.text, **stop)


def _malformed(event: Mapping[str, Any], queue: ConsentQueue, settings: AdapterSettings, *,
               home: Path | None) -> dict[str, Any] | None:
    """The gate's answer to a ``PreToolUse`` event with no valid ``tool_name``, absolute ``cwd`` or session id (L3 r1 G4,
    r2 G4', r3: a relative cwd anchors nothing): fail closed, as Codex's gate does, for every call whose targets can be gated: a deny with the
    irreversible tier's hard stop when the tool is unnamed, a file tool (:data:`FILE_TOOLS`, the ones
    :func:`levain.consent.policy.targets` reads a path from) or an MCP tool, never classified (a relative target
    cannot be anchored without ``cwd``, and the event is not trusted to say what it is). Any other named tool (Bash,
    which is never parsed) is not the gate's to answer and goes on (``None``). Nothing is filed or ledgered: there is
    no session to file under."""
    tool_name, tool_input = event.get("tool_name"), event.get("tool_input", {})
    cwd = event.get("cwd")
    missing = [k for k, ok in (("session_id", _session_of(event) is not None),
                               ("tool_name", isinstance(tool_name, str) and bool(tool_name)),
                               ("cwd", isinstance(cwd, str) and os.path.isabs(cwd))) if not ok]
    named = isinstance(tool_name, str) and bool(tool_name)
    if named and tool_name not in FILE_TOOLS and not tool_name.startswith("mcp__"):
        return None
    what = "this call"
    if named:
        try:
            what = _what(tool_name, tool_input)
        except Exception:  # noqa: BLE001 - the deny stands whatever the description does
            what = tool_name
    return _pre("deny", f"levain: the hook event has no valid {', '.join(missing)}, so the action was NOT taken: "
                f"{what}. Do not retry it and do not achieve it another way.",
                **_stop("irreversible", what, "the hook event was malformed."))


def _ledger_note(queue_dir: Path, event: Mapping[str, Any], session: str, tool_name: str, tool_input: Any, cwd: str,
                 *, decision: Decision | None = None, refused: str | None = None,
                 request_id: str | None = None) -> None:
    """File the gated call's ledger row at decision time (:func:`levain.consent.ledger.gate_row`). A call the gate
    denied fires no PostToolUse (runs3-cc), so this row is its only one. Never changes the gate's answer."""
    try:
        from levain.consent.ledger import gate_row

        gate_row(AGENT, queue_dir, event, session, tool_name, tool_input, cwd, decision=decision, refused=refused,
                 request_id=request_id)
    except Exception:  # noqa: BLE001 - the ledger is a record, never the gate
        pass


def _ledger_fill(event: Any, queue_dir: Path | None) -> None:
    """Stamp this session's pending ledger rows from its transcript (:func:`levain.consent.ledger.hook_fill`, bounded
    by its lock wait and fill budget): the prior call's ``permissionDecision`` is on disk by this PreToolUse
    (runs3-cc). Runs after the gate's answer is written, so unlike the ledger's own writers it also swallows an
    interrupt: :func:`main`'s failure path would print a second answer after the first."""
    try:
        from levain.consent.ledger import hook_fill

        hook_fill(AGENT, event, queue_dir=queue_dir)
    except BaseException:  # noqa: BLE001 - the answer is already written; a record never fails the hook
        pass


def _gate_call(event: Mapping[str, Any], queue: ConsentQueue, settings: AdapterSettings, tool_name: str,
               tool_input: Any, cwd: str, session: str, *, home: Path, environ: Mapping[str, str],
               sleep: Callable[[float], None],
               still_away: Callable[[], tuple[bool, str, str]] | None = None) -> dict[str, Any] | None:
    from datetime import datetime, timezone

    from levain.cockpit.consent import parse_stamp

    qdir = Path(queue.directory)
    note = lambda **kw: _ledger_note(qdir, event, session, tool_name, tool_input, cwd, **kw)  # noqa: E731

    def deliver(decision: Decision) -> dict[str, Any]:
        note(decision=decision)
        return gate_output(decision)

    tier = gate_class(tool_name, tool_input, cwd, home=home, environ=environ,
                      protected=protected_paths(Path(queue.directory), settings, home=home, environ=environ, cwd=cwd))
    if tier is None:
        return None
    what = _what(tool_name, tool_input)
    tool_use_id = event.get("tool_use_id")
    event_id = tool_use_id if isinstance(tool_use_id, str) and tool_use_id else None
    mode = event.get("permission_mode")
    posture = mode if isinstance(mode, str) and mode else None
    if posture == "plan":
        return None  # plan runs nothing: no decision, and a ruled allow is not spent on a call that will not run
    loose = unconfined_state(cwd, home=home, environ=environ)
    if loose == "on":
        return None  # the operator started this session unconfined (Q10 D2): the mode decides, no ruled allow spent
    try:
        ruled = queue.take_ruled_allow(AGENT, session, tool_name, tool_input, cwd=cwd, event=event_id)
        if ruled is not None:
            return deliver(ruled)
        if nothing_can_decide(settings, tier=AGENT_CONFIG_TIER):
            if posture in ATTENDED_POSTURES:
                return None  # Claude Code's own dialog (or the operator's own allow rule) decides while they are present
            # no dialog asks in this mode (an absent or unknown mode counts as none) and no key could answer a
            # request: the floor denies, files nothing (Q10 D1). A plain deny: nothing is undecided, and the agent
            # goes on with what does not need the change (L2-2)
            note(refused="floor")
            return _pre("deny", floor_text(what, loose))
        if posture in ATTENDED_POSTURES and not effective_away(settings)[0]:
            # an attended default session: Claude Code's own dialog (or his own allow rule) decides,
            # as the PermissionRequest hook leaves it while he is present; held only while he is away (head ruling 10-18)
            return None
        hold = min(settings.gate_hold_seconds, GATE_HOLD_CEILING)  # the installed timeout - the margin
        request = queue.file(agent=AGENT, agent_session_id=session, tool_name=tool_name, tool_input=tool_input,
                             cwd=cwd, hold=timedelta(seconds=hold), tier=AGENT_CONFIG_TIER, event_id=event_id,
                             resume=resume_meta_of(event, environ=environ, claude_pid=None),
                             model=event.get("model"))
    except Exception as exc:
        note(refused="failed")
        return _pre("deny", f"levain: the gate failed ({type(exc).__name__}), so the action was NOT taken: {what}. "
                    "Do not retry it and do not achieve it another way.", **_stop(tier, what, "the gate failed."))
    try:
        notify(settings, f"levain: {AGENT} waits on you up to {hold:g} s: {what} (request {request.id}).",
               cwd=queue.directory, ref=f"request:{request.id}")
        if posture in ATTENDED_POSTURES and still_away is not None:
            # held only because he was away (above): re-read away as the PermissionRequest hold does, and when it
            # ends hand the call back to Claude Code's dialog (L3 r1 complement 1 = codex 2)
            got, back = hold_for_answer(queue, request, hold + 5.0, tool_input=tool_input, posture=posture,
                                        still_away=still_away)
            if got is None and back:
                if queue.withdraw(request.id, note=f"away mode ended ({back}); Claude Code's dialog asks"):
                    return None
                # not pending any more: an answer may have landed between the wait and the withdraw
                got = _verified(queue.decision(request.id, tool_input=tool_input, posture=posture), request)
                return deliver(got) if got is not None else None
        else:
            got = _verified(queue.await_decision(request.id, hold + 5.0, poll=GATE_POLL, tool_input=tool_input,
                                                 posture=posture), request)
        if got is not None:
            return deliver(got)
        # the hold ends at expires_at (the library's deadline); mark_undecided applies at or after it
        left = (parse_stamp(request.expires_at) - datetime.now(timezone.utc)).total_seconds()
        still = queue.get(request.id)
        if left > 0 and still is not None and still[1] == "pending":
            # only a request still pending at the end of its hold waits for the mark (one withdrawn, or whose
            # answer no longer verifies, is marked at once: L3 r1 complement 6). Capped: ``left`` is wall-clock,
            # and a clock stepped back must not carry the hold past the timeout (a mark refused for being early
            # is the deny below, never an allow)
            sleep(min(left + 0.05, GATE_SLEEP_CAP))
        marked = queue.mark_undecided(request.id, halt=tier == "irreversible", tool_input=tool_input,
                                      posture=posture)
        if isinstance(marked.decision, Decision) and _verified(marked.decision, request) is not None:
            return deliver(marked.decision)  # an answer verified in the last moments of the hold
        if marked.state == "undecided" and marked.gap is not None:
            note(refused="undecided", request_id=request.id)
            return undecided_output(tier, what, marked.gap)
        note(refused="deny", request_id=request.id)
        why = marked.reason or "no verified decision"
        return _pre("deny", f"levain: the action was NOT taken: {what} ({why}). Do not retry it and do "
                    "not achieve it another way.", **_stop(tier, what, why))
    except Exception as exc:
        note(refused="failed", request_id=request.id)
        return _pre("deny", f"levain: the gate failed ({type(exc).__name__}), so the action was NOT taken: {what}. "
                    "Do not retry it and do not achieve it another way.", **_stop(tier, what, "the gate failed."))


def gate_failed(event: Any, queue_dir: Path, settings: AdapterSettings, *, home: Path | None,
                environ: Mapping[str, str]) -> dict[str, Any] | None:
    """:func:`main`'s answer when the gate itself raised before it could classify the call (the queue would not
    open): a deny with the hard stop when the call is gated, or when that cannot be told; ``None`` otherwise."""
    try:
        tool_name, tool_input, cwd = event.get("tool_name"), event.get("tool_input") or {}, event.get("cwd")
        if not isinstance(tool_name, str) or not isinstance(cwd, str) or not cwd:
            return None
        tier = gate_class(tool_name, tool_input, cwd, home=home, environ=environ,
                          protected=protected_paths(queue_dir, settings, home=home, environ=environ, cwd=cwd))
        what = _what(tool_name, tool_input)
    except Exception:
        tier, what = "irreversible", "this call"
    if tier is None:
        return None
    session = _session_of(event) if isinstance(event, Mapping) else None
    if session is not None:
        _ledger_note(queue_dir, event, session, str(event.get("tool_name")), event.get("tool_input") or {},
                     str(event.get("cwd") or ""), refused="failed")
    return could_not_run(tier, what)


def queue_unresolved(event: Any, *, home: Path | None, environ: Mapping[str, str]) -> dict[str, Any] | None:
    """:func:`main`'s answer for a PreToolUse when no queue directory could be resolved (:func:`default_queue_dir`
    raised: a ``$LEVAIN_HOME`` that cannot be expanded, or no home): with no queue and no settings, only the
    levain home is protected (:func:`levain_home_protected`, which never raises) and :func:`gate_class` classifies
    the call: a file-tool call that targets it or other agent config is denied with the hard stop, and every other
    call has no decision, so a read is not an outage. Bash is never parsed, so a Bash call gets no decision."""
    try:
        tool_name, tool_input, cwd = event.get("tool_name"), event.get("tool_input") or {}, event.get("cwd")
        if not isinstance(tool_name, str) or not isinstance(cwd, str) or not os.path.isabs(cwd):
            return None
        tier = gate_class(tool_name, tool_input, cwd, home=home, environ=environ,
                          protected=tuple(levain_home_protected(home, cwd, environ=environ)))
        what = _what(tool_name, tool_input)
    except Exception:
        tier, what = "irreversible", "this call"
    return None if tier is None else could_not_run(tier, what)


def could_not_run(tier: str | None, what: str) -> dict[str, Any]:
    """:func:`gate_failed`'s deny (the hard stop for the irreversible tier); the slot gate's too (:func:`slot_gate`)."""
    return _pre("deny", f"levain: the gate could not run, so the action was NOT taken: {what}. Do not retry it "
                "and do not achieve it another way.", **_stop(tier, what, "the gate could not run."))


# --- the slot gate (F10 S2a) ---------------------------------------------------------------------------------
#
# A slot session (its environment names a broker socket, :func:`levain.consent.broker_client.in_slot`) has no local
# queue: the slot's floor keeps it out of Levain's files, and the operator side owns rulings, re-pings, away and
# limits. Its PreToolUse goes to :func:`slot_gate`, its PermissionRequest gets no decision (Claude Code's dialog,
# or the session's mode, answers it). :func:`main` opens no ConsentQueue, reads no settings file and writes no ledger
# row in a slot. The hold is :data:`DEFAULT_GATE_HOLD` (the settings file that could change it is the operator's,
# outside the slot); the broker is polled every :data:`GATE_POLL`, then every broker_client.SLOW_POLL.
#
# Every PreToolUse in a slot first asks the broker ``GATE`` (F10 S2b-2,
# :func:`levain.consent.broker_client.gate_check`), before the call is classified: a session halted on an
# irreversible call whose hold ended unanswered, or over a limit the operator set (limits apply when the broker has a
# limits source: the production broker has none yet), is denied and its turn ended; GO lets the rest of this gate
# run (GO is never an allow); the broker down, refusing or out of shape denies. A halt's release and a raised limit
# are the operator side's (levain.qbroker.broker, GATE); a slot has no Extend.

SLOT_STOP_REASON = "levain: stopped by the consent broker (a halt or a limit the operator set)."

# who decided, in the gate's words, when the broker verified the decision (the hook never sees the signer's key, and
# cannot tell an operator's signed answer from an allow the run's signed contract grant gave)
SLOT_HOW = "a levain decision the queue broker verified against an operator's signature"


def slot_gate(event: Any, *, home: Path, environ: Mapping[str, str], client: Any = None,
              hold: float = DEFAULT_GATE_HOLD,
              expected_uid: int | None = None, sleep: Callable[[float], None] = time.sleep,
              monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any] | None:
    """The slot's gate output for one ``PreToolUse`` event, or ``None`` (the call goes on as the mode says). A
    malformed event is :func:`_malformed`'s. Then, on EVERY call and before it is classified, the broker is asked
    ``GATE`` (:func:`levain.consent.broker_client.gate_check`; ``client``, default
    :meth:`~levain.consent.broker_client.BrokerClient.from_env`): halted, over a limit or the limits gate failed is
    denied with the turn ended, and the broker down, refusing or out of shape denies every call, ungated ones too.
    Past a GO, a call :func:`gate_class` does not gate (protected paths: none) is :func:`_slot_ungated`'s (``None``
    unless a dispatch run's grant or queue decides it); a gated call is filed with the broker and held up to ``hold`` for a decision the broker
    verified, delivered as :func:`decision_output` names it; at the end of the hold it is denied with
    :func:`undecided_output`, its gap named by the request id (the operator side numbers gaps; the hook does not).
    Anything else once the call is gated (a settings file that names the socket variable,
    :func:`queue_socket_planted`; no socket or cap; no broker account, or a socket whose peer is not it; the
    broker unreachable, refusing or answering out of shape) is :func:`could_not_run`'s deny with the hard stop.
    ``expected_uid`` is the broker's uid for a caller that is not the hook (a test, a run); the hook passes none,
    and the client looks the broker account up (:func:`levain.consent.broker_client.broker_uid`)."""
    from levain.consent import broker_client

    if not isinstance(event, Mapping) or event.get("hook_event_name") != GATE_EVENT:
        return None
    tool_name, tool_input, cwd = event.get("tool_name"), event.get("tool_input", {}), event.get("cwd")
    session = _session_of(event)
    if not isinstance(tool_name, str) or not tool_name or not isinstance(cwd, str) or not os.path.isabs(cwd) \
            or session is None:
        return _malformed(event, None, AdapterSettings(), home=home)
    if tool_input is None:
        tool_input = {}
    # GATE first, before the call is classified (F10 S2b-2): the session's halt and its limits, every call
    try:
        if queue_socket_planted(cwd, home=home, environ=environ):
            return could_not_run("irreversible", "this call")
        client = client if client is not None else broker_client.BrokerClient.from_env(environ,
                                                                                      expected_uid=expected_uid)
        tool_use_id = event.get("tool_use_id")
        stopped = broker_client.gate_check(
            client, agent=AGENT, tool_name=tool_name, tool_input=tool_input,
            tool_use_id=tool_use_id if isinstance(tool_use_id, str) and tool_use_id else None, sleep=sleep)
    except Exception:  # noqa: BLE001 - fail closed: the broker down, refusing or malformed is a deny
        return could_not_run("irreversible", "this call")
    if stopped is not None:
        # a halt or a limit ends the turn, as on the local path (_limits; the irreversible tier's _stop)
        return _pre("deny", stopped, **{"continue": False, "stopReason": SLOT_STOP_REASON})
    try:
        tier = gate_class(tool_name, tool_input, cwd, home=home, environ=environ)
        if tier is None:
            return _slot_ungated(event, session, tool_name, tool_input, cwd, home=home, environ=environ,
                                 client=client, hold=hold,
                                 expected_uid=expected_uid, sleep=sleep, monotonic=monotonic)
        what = _what(tool_name, tool_input)
    except Exception:  # noqa: BLE001 - a call that cannot be classified is denied as the irreversible tier
        return could_not_run("irreversible", "this call")
    try:
        if queue_socket_planted(cwd, home=home, environ=environ):
            return could_not_run(tier, what)
        client = client if client is not None else broker_client.BrokerClient.from_env(environ,
                                                                                      expected_uid=expected_uid)
        tool_use_id = event.get("tool_use_id")
        request = broker_client.gate_request(
            agent=AGENT, session=session, tool_name=tool_name, tool_input=tool_input, cwd=cwd,
            event_id=tool_use_id if isinstance(tool_use_id, str) and tool_use_id else None,
            tier=AGENT_CONFIG_TIER, hold=hold)
        decision = broker_client.file_and_hold(client, request, hold, poll=GATE_POLL, sleep=sleep,
                                               monotonic=monotonic)
    except Exception:  # noqa: BLE001 - fail closed: the broker down, refusing or malformed is a deny
        return could_not_run(tier, what)
    if decision is None:
        return undecided_output(tier, what, request.id)
    return decision_output(decision, SLOT_HOW)


def _slot_ungated(event: Mapping[str, Any], session: str, tool_name: str, tool_input: Any, cwd: str, *,
                  home: Path, environ: Mapping[str, str], client: Any, hold: float, expected_uid: int | None,
                  sleep: Callable[[float], None], monotonic: Callable[[], float]) -> dict[str, Any] | None:
    """:func:`slot_gate` for a call :func:`gate_class` does not gate (W2-c3, head ruling 1020+49): a file write
    (:data:`FILE_TOOLS`) goes to the broker (:func:`levain.consent.broker_client.file_ungated`), with its grant member
    when :func:`~levain.consent.broker_client.grant_member` reads one, so a dispatch run's signed contract can allow
    it; with no member (a MultiEdit, a relative path) it is sent all the same, and a dispatch run files it for the
    operator (L3 r1 triage R1). At the end of the hold a call the run queued is denied as the reversible tier.
    ``None`` (the mode decides, as for any ungated call) only for a call that is not a file write or on the broker's
    GO; GO is not allow. Any failure (the member that cannot be read, a planted socket variable, no socket or cap,
    the broker unreachable, refusing, out of shape, or not the broker) is :func:`could_not_run`'s deny: broker down
    means the hooks fail CLOSED (BUILD_F10), so a dispatch run's write outside its list never goes through on an
    outage. The price: an interactive slot loses its ordinary writes while its broker is down (head ruling
    1020+49)."""
    from levain.consent import broker_client

    def what() -> str:
        try:
            return _what(tool_name, tool_input)
        except Exception:  # noqa: BLE001 - a call that cannot be described is still denied
            return "this call"

    try:
        member = broker_client.grant_member(AGENT, tool_name, tool_input, cwd)
        if member is None and tool_name not in FILE_TOOLS:
            return None
        if queue_socket_planted(cwd, home=home, environ=environ):
            return could_not_run("reversible", what())
        tool_use_id = event.get("tool_use_id")
        got = broker_client.file_ungated(
            client, member=member, agent=AGENT, session=session, tool_name=tool_name, tool_input=tool_input,
            cwd=cwd, event_id=tool_use_id if isinstance(tool_use_id, str) and tool_use_id else None, hold=hold,
            poll=GATE_POLL, expected_uid=expected_uid, sleep=sleep, monotonic=monotonic)
        if got[0] == broker_client.GO:
            return None
        decision, request_id = got
    except Exception:  # noqa: BLE001 - see the docstring: fail closed
        return could_not_run("reversible", what())
    if decision is None:
        return undecided_output("reversible", what(), request_id)
    return decision_output(decision, SLOT_HOW)


def _slot_main(stdin: IO[str], stdout: IO[str], *, home: Path | None, environ: Mapping[str, str],
               expected_uid: int | None = None) -> int:
    """:func:`main` in a slot session: no ConsentQueue, no settings file, no ledger. Always exits 0. A stdin that
    is not a JSON object (truncated, garbled, an array) is :func:`could_not_run`'s deny: the event cannot be told
    from a PreToolUse, and a PermissionRequest ignores a PreToolUse's deny. ``expected_uid``: :func:`slot_gate`'s."""
    event: Any = None
    try:
        event = json.loads(stdin.read())
        if not isinstance(event, Mapping):
            raise ValueError("the hook event is not a JSON object")
        out = slot_gate(event, home=home, environ=environ,
                        expected_uid=expected_uid) if event.get("hook_event_name") == GATE_EVENT \
            else None   # a PermissionRequest in a slot: no decision
        if out is not None:
            stdout.write(json.dumps(out) + "\n")
            stdout.flush()
    except BaseException as exc:  # noqa: BLE001 - every failure is no decision, never allow; a gated one a deny
        if not isinstance(event, Mapping) or event.get("hook_event_name") == GATE_EVENT:
            try:
                stdout.write(json.dumps(could_not_run("irreversible", "this call")) + "\n")
                stdout.flush()
            except BaseException:
                pass
        try:
            print(f"levain consent hook: no decision ({type(exc).__name__}: {exc})"[:500], file=sys.stderr)
        except Exception:
            pass
    return 0


def _queue_for(queue_dir: Path, settings: AdapterSettings, *, home: Path) -> ConsentQueue:
    if settings.cockpit_data_root:
        root = Path(settings.cockpit_data_root)
        source = authority_source(root / "cockpit.db", anchor=anchor_path(root))
    else:
        # No cockpit data root: a source over the queue DIRECTORY, which sqlite cannot open as a database, so
        # every replay fails and no answer can verify (never a file path a writer could plant a log at).
        source = authority_source(queue_dir, anchor=None)
    return ConsentQueue(queue_dir, authority=source, home=home)


@dataclass
class PermissionContext:
    """What one hook call (or one chat core request) decides with, from :func:`permission_context`: the queue, the
    settings as the hook acts on them (:func:`with_away_mark`) and the away recheck a hold re-reads
    (:func:`still_away_from`). A context manager; :meth:`close` releases nothing today (the queue and the authority
    source open a connection per operation) and is where a future held handle is closed."""
    queue: ConsentQueue
    settings: AdapterSettings
    recheck: Callable[[], tuple[bool, str, str]]
    environ: Mapping[str, str]
    home: Path

    def close(self) -> None:
        pass

    def __enter__(self) -> "PermissionContext":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def permission_context(queue_dir: Path, *, environ: Mapping[str, str], home: Path,
                       settings: AdapterSettings | None = None) -> PermissionContext:
    """The setup :func:`main` runs before it reads the event, for ``queue_dir``: the settings file (or ``settings``,
    when the caller already loaded it), one queue over it holding ``home`` (the away mark, the hold and every recheck
    read it), the settings with the away mark applied, and the recheck over that queue. ``environ`` is carried for
    the caller's core call; nothing here reads it."""
    settings = settings if settings is not None else load_settings(queue_dir)
    queue = _queue_for(queue_dir, settings, home=home)
    settings = with_away_mark(settings, queue_dir, AGENT, queue=queue)
    return PermissionContext(queue=queue, settings=settings,
                             recheck=still_away_from(queue_dir / SETTINGS_NAME, settings, queue=queue),
                             environ=environ, home=home)


def main(argv: list[str] | None = None, stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> int:
    """The hook entry point, for both events the one script is wired to: ``PermissionRequest`` (:func:`decide`)
    and ``PreToolUse`` (:func:`gate`). Always exits 0. A PermissionRequest prints a decision only when the core
    verified one; a PreToolUse whose gate raised prints :func:`gate_failed` (a deny for a gated call). In a slot
    session (:func:`levain.consent.broker_client.in_slot`) it is :func:`_slot_main` instead, and opens no queue."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    event: Any = None
    queue_dir: Path | None = None
    settings = AdapterSettings()   # set before the try: a raising load_settings leaves it, so gate_failed still
    # classifies the call (a store write denied, an ordinary call no decision), never with None
    started = time.monotonic()
    # H4: the one read of the process environment and home; everything below is handed them
    environ: Mapping[str, str] = os.environ
    home = _bound_home()
    if in_slot(environ):
        return _slot_main(stdin, stdout, home=home, environ=environ)
    try:
        parser = argparse.ArgumentParser(prog="levain-consent-claude-code")
        parser.add_argument("--queue-dir", default=None)
        parser.add_argument("--max-hold", type=float, default=DEFAULT_MAX_HOLD)
        args = parser.parse_args(argv)
        queue_dir = Path(args.queue_dir) if args.queue_dir else queue_dir_for(environ)
        if home is None:
            raise RuntimeError("could not determine the home directory")
        # loaded here, not only inside permission_context: when building the queue raises, gate_failed still
        # protects the cockpit root these settings name (L3 r1, complement MED 1)
        settings = load_settings(queue_dir)
        with permission_context(queue_dir, environ=environ, home=home, settings=settings) as pc:
            queue, settings = pc.queue, pc.settings
            event = json.loads(stdin.read())
            if isinstance(event, Mapping) and event.get("hook_event_name") == GATE_EVENT:
                out = gate(event, queue, settings, home=home, environ=environ)
            else:
                out = decide(event, queue, settings, environ=environ, home=home, max_hold=args.max_hold,
                             still_away=pc.recheck)
        if out is not None:
            stdout.write(json.dumps(out) + "\n")
            stdout.flush()
        if isinstance(event, Mapping) and event.get("hook_event_name") == GATE_EVENT \
                and time.monotonic() - started <= GATE_FILL_WITHIN:
            _ledger_fill(event, queue_dir)
    except BaseException as exc:  # noqa: BLE001 - every failure is no decision, never allow
        try:
            if event is None:
                event = json.loads(stdin.read())   # not read yet: the failure came first
            out = None
            if isinstance(event, Mapping) and event.get("hook_event_name") == GATE_EVENT:
                out = gate_failed(event, queue_dir, settings, home=home, environ=environ) \
                    if queue_dir is not None else queue_unresolved(event, home=home, environ=environ)
            if out is not None:
                stdout.write(json.dumps(out) + "\n")
                stdout.flush()
        except BaseException:
            pass
        try:
            print(f"levain consent hook: no decision ({type(exc).__name__}: {exc})"[:500], file=sys.stderr)
        except Exception:
            pass
        return 0
    return 0


def _bound_home() -> Path | None:
    """``Path.home()`` for a hook entry point's one binding, or ``None`` when there is none (no ``$HOME`` and no
    password-database entry): the entry point then classifies a PreToolUse with no queue, as before."""
    try:
        return Path.home()
    except RuntimeError:
        return None


# --- install (one workspace's project settings) ------------------------------------------------------------

# The first lines of every hook script levain writes (this adapter's, Codex's and the canary's): Python puts a
# script's own directory first on sys.path, so a package planted beside the script (``.claude/hooks/levain/``) would
# be imported instead of levain. Unless already running with -E -P (no PYTHON* variables, no script directory on
# sys.path), the script re-executes itself so; ``os`` is already loaded by then (site imports it), so nothing is read
# from the workspace first. ``-EP``, not ``-I``: ``-I`` also drops the user site, where levain may be installed.
ISOLATE_PRELUDE = '''import os
import sys

if not (sys.flags.safe_path and sys.flags.ignore_environment):
    try:
        os.execv(sys.executable, [sys.executable, "-EP", os.path.abspath(__file__), *sys.argv[1:]])
    except BaseException:
        pass
    sys.exit(0)
'''

HOOK_TEMPLATE = '''#!{python}
# Installed by levain (levain.consent.adapters.claude_code.install). Claude Code's PermissionRequest hook:
# it reports a decision only when levain verified a signed answer or a standing consent grant; otherwise it
# prints nothing and the native permission dialog stands. Any failure here, including levain missing, is no
# decision.
''' + ISOLATE_PRELUDE.replace("{", "{{").replace("}", "}}") + '''
try:
    from levain.consent.adapters.claude_code import main
except BaseException:
    sys.exit(0)
sys.exit(main({argv}))
'''


def install(workspace: Path | str, *, python: str | None = None, queue_dir: Path | str | None = None,
            max_hold: float = DEFAULT_MAX_HOLD, allow_untested_agent_version: bool = False,
            inbound: str = "hold", outbound: str = "ask") -> tuple[Path, Path]:
    """Install the hook into ``workspace``: ``.claude/hooks/levain_consent.py`` and a ``PermissionRequest``
    entry (matcher ``*``, ``timeout`` = :func:`hook_timeout`) in ``.claude/settings.json``, other
    settings kept and any earlier levain entry replaced, plus the messaging controls ``inbound`` / ``outbound``
    (:func:`merge_messaging`, never loosening) in the same write. Raises
    :class:`levain.consent.canary.UntestedAgentVersion` when the installed ``claude`` is not a tested version
    (:data:`levain.consent.canary.TESTED_VERSIONS`), unless ``allow_untested_agent_version``. Returns
    (script, settings). It installs the PermissionRequest hook only, never the PreToolUse gate (Q14 B2): the
    gate is installed by ``levain init`` / ``levain update`` (:mod:`levain.install`); only the canary calls this."""
    if not (isinstance(max_hold, (int, float)) and math.isfinite(max_hold) and max_hold >= 0):
        raise ValueError("max_hold must be a finite number of seconds >= 0")
    merge_messaging({}, inbound=inbound, outbound=outbound)  # refuse an unknown choice before writing anything
    from levain.consent.canary import require_tested_version
    require_tested_version(AGENT, allow_untested=allow_untested_agent_version)
    ws = Path(workspace).resolve()
    claude_dir = ws / ".claude"
    hooks_dir = claude_dir / "hooks"
    script = hooks_dir / HOOK_SCRIPT_NAME
    settings_path = claude_dir / "settings.json"
    for directory in (claude_dir, hooks_dir):
        _refuse_symlink(directory)
        if not os.path.lexists(directory):
            os.mkdir(directory)
        _refuse_symlink(directory)
    _refuse_symlink(script)
    _refuse_symlink(settings_path)
    for target in (script, settings_path):
        if not os.path.realpath(target.parent).startswith(str(ws) + os.sep):
            raise ValueError(f"refusing to install outside the workspace: {target}")
    python = python or sys.executable
    argv = ["--max-hold", repr(float(max_hold))]
    if queue_dir is not None:
        argv += ["--queue-dir", str(Path(queue_dir).resolve())]
    command = hook_command(python, script)
    handler = {"type": "command", "command": command, "timeout": hook_timeout(max_hold)}
    # merge onto a copy first: a settings file the merge refuses (a malformed `hooks` or `permissions`) stops the
    # install before the script is written, so no hook script is left without its settings entry
    try:
        trial = json.loads(settings_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        trial = {}
    if not isinstance(trial, dict):
        raise ValueError(f"{settings_path} is not a JSON object")
    _merge_handler(trial, command, handler, script=script)
    merge_messaging(trial, inbound=inbound, outbound=outbound)
    _atomic_write(script, HOOK_TEMPLATE.format(python=python, argv=repr(argv)), mode=0o755)
    for _ in range(5):
        before = _stat_key(settings_path)
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            settings = {}
        if not isinstance(settings, dict):
            raise ValueError(f"{settings_path} is not a JSON object")
        _merge_handler(settings, command, handler, script=script)
        merge_messaging(settings, inbound=inbound, outbound=outbound)
        if _stat_key(settings_path) != before:
            continue  # changed while we merged: merge again onto the newer file
        _atomic_write(settings_path, json.dumps(settings, indent=2) + "\n", mode=0o644, keep_mode=True)
        return script, settings_path
    raise RuntimeError(f"{settings_path} kept changing; not updated")


def hook_timeout(max_hold: float) -> int:
    """Claude Code's ``timeout`` for the installed hook: ``ceil(max(max_hold, CLEANUP_RESERVE))`` +
    :data:`TIMEOUT_MARGIN`. The wait ends :data:`CLEANUP_RESERVE` before ``max_hold`` (or is 0), so this always
    exceeds wait + that reserve."""
    return int(math.ceil(max(max_hold, CLEANUP_RESERVE))) + TIMEOUT_MARGIN


# The flags the installed command starts the interpreter with: -E -P from the FIRST process, so no PYTHON* variable
# (a sitecustomize on PYTHONPATH) and no script directory runs before the hook; the script's ISOLATE_PRELUDE stays for
# a direct invocation. Releases before this one installed the command without them (:func:`_is_ours` still knows it).
HOOK_FLAGS = "-EP"


def hook_command(python: str, script: Path | str) -> str:
    """The command levain installs for a consent hook (Claude Code's and Codex's): ``<python> -EP <script>``."""
    return f"{shlex.quote(python)} {HOOK_FLAGS} {shlex.quote(str(script))}"


# The consent hook `levain init` wires runs its script through `levain.hookrun`, so a gone or moved install exits 0
# and a failing hook exits 1, never the 2 that blocks Codex or that Claude Code may read as a deny (levain/hookrun.py).
HOOKRUN = ("-m", "levain.hookrun")


def hookrun_command(python: str, script: Path | str) -> str:
    """``<python> -EP -m levain.hookrun <script>``: the script stays the last token."""
    return f"{shlex.quote(python)} {HOOK_FLAGS} {' '.join(HOOKRUN)} {shlex.quote(str(script))}"


def _is_ours(h: Any, command: str, script: Path | None) -> bool:
    """A handler this installation owns: its command is exactly ``command``, or it is exactly
    ``<one interpreter> -EP <this installation's absolute script path>``, the machine-wide
    ``<one interpreter> -EP -m levain.hookrun <that path>``, or the earlier releases'
    ``<one interpreter> <that path>`` (a reinstall under another Python, or after an upgrade, must replace the old
    handler, not run beside it). Lookalike paths and flags, and any other command shape, are never ours."""
    if not isinstance(h, dict) or not isinstance(h.get("command"), str):
        return False
    if h["command"] == command:
        return True
    if script is None:
        return False
    try:
        tokens = shlex.split(h["command"])
    except ValueError:
        return False
    return (len(tokens) >= 2 and tokens[-1] == str(script)
            and tokens[1:-1] in ([], [HOOK_FLAGS], [HOOK_FLAGS, *HOOKRUN]))


def _merge_handler(settings: dict[str, Any], command: str, handler: dict[str, Any],
                   script: Path | None = None, event: str = EVENT) -> None:
    """Under ``hooks.<event>`` (default ``PermissionRequest``), remove ONLY the handlers this installation owns
    (:func:`_is_ours`; other handlers and every group field kept; a group dropped only when that leaves it with no
    handlers), then add ``handler`` in its own group."""
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("settings 'hooks' is not a JSON object")
    groups = hooks.get(event, [])
    if not isinstance(groups, list):
        raise ValueError(f"settings 'hooks.{event}' is not a list")
    kept = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            kept.append(group)
            continue
        handlers = [h for h in group["hooks"] if not _is_ours(h, command, script)]
        if handlers or not group["hooks"]:
            kept.append({**group, "hooks": handlers})
    kept.append({"matcher": "*", "hooks": [handler]})
    hooks[event] = kept


# Q9, cross-session messaging (DESIGN_Q9). Inbound: Claude Code's `crossSessionInbound`. Outbound: ``ask`` puts
# `SendMessage` under an explicit ask rule (which no permission mode auto-approves, so every send reaches the
# PermissionRequest hook above or the human) plus `isolatePeerMachines`; ``isolate`` writes `isolatePeerMachines`
# alone (a send beyond this machine asks; one to a session on this machine does not); ``deny`` turns sending and
# listing off. ``unmanaged`` writes nothing for that direction.
INBOUND_CHOICES = ("hold", "refuse", "unmanaged")
OUTBOUND_CHOICES = ("ask", "isolate", "deny", "unmanaged")


def merge_messaging(settings: dict[str, Any], *, inbound: str = "hold", outbound: str = "ask") -> None:
    """Merge the messaging controls into ``settings`` (a Claude Code settings object), never loosening what is
    there: `crossSessionInbound` becomes the stricter of its value and ``inbound``, a rule is only ever added, and
    an existing `isolatePeerMachines` true is kept. Raises ValueError on an unknown choice or a malformed
    `permissions` block (never rewritten into shape)."""
    from levain.host_guard import stricter_inbound

    if inbound not in INBOUND_CHOICES or outbound not in OUTBOUND_CHOICES:
        raise ValueError(f"inbound must be one of {INBOUND_CHOICES} and outbound one of {OUTBOUND_CHOICES}")
    if inbound != "unmanaged":
        current = settings.get("crossSessionInbound")
        # an unrecognised value already holds (Claude Code's rule); replacing it with hold or refuse is no loosening
        settings["crossSessionInbound"] = stricter_inbound(current, inbound) or inbound
    if outbound == "unmanaged":
        return
    if outbound == "isolate":
        settings["isolatePeerMachines"] = True
        return
    if settings.get("permissions") is None:
        settings["permissions"] = {}  # an explicit null holds no rules
    perms = settings["permissions"]
    if not isinstance(perms, dict):
        raise ValueError("settings 'permissions' is not a JSON object")
    wanted = {"ask": ("ask", ("SendMessage",)), "deny": ("deny", ("SendMessage", "ListAgents"))}[outbound]
    if perms.get(wanted[0]) is None:
        perms[wanted[0]] = []
    rules = perms[wanted[0]]
    if not isinstance(rules, list):
        raise ValueError(f"settings 'permissions.{wanted[0]}' is not a list")
    rules.extend(t for t in wanted[1] if t not in rules)
    if outbound == "ask":
        settings["isolatePeerMachines"] = True


def _refuse_symlink(path: Path) -> None:
    if os.path.islink(path):
        raise ValueError(f"refusing to install through a symlink: {path}")


def _stat_key(path: Path) -> tuple[int, int, int, int] | None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _atomic_write(path: Path, text: str, *, mode: int, keep_mode: bool = False) -> None:
    """Write ``text`` to a new temp file beside ``path`` (created exclusively, never through a symlink),
    fsync it, and rename it over ``path``: a rename replaces a symlink leaf itself, never its target, and a
    reader sees the old file or the new one, never a partial write."""
    if keep_mode:
        try:
            mode = os.lstat(path).st_mode & 0o777
        except FileNotFoundError:
            pass
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fchmod(fh.fileno(), mode)
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


if __name__ == "__main__":
    sys.exit(main())
