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
verify, and nothing is held). :func:`install`
writes the hook script (:data:`HOOK_TEMPLATE`) and the ``PermissionRequest`` entry into ONE workspace's project
settings; wiring into user settings is a later slice.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import site
import sys
import sysconfig
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import IO, Any

from levain.authority import anchor_path
# FILE_TOOLS, MCP_PATH_FIELDS, PolicyNotEligible and guarded_policy are re-exported: this module's names since Q2
from levain.consent.policy import AGENT_CONFIG_TIER, Policy
from levain.consent.policy import FILE_TOOLS as FILE_TOOLS
from levain.consent.policy import MCP_PATH_FIELDS as MCP_PATH_FIELDS
from levain.consent.policy import PolicyNotEligible as PolicyNotEligible
from levain.consent.policy import guarded_policy as guarded_policy
from levain.consent.policy import mcp_config_candidates
from levain.consent.policy import targets as _targets
from levain.consent.auto_away import IDLE_THRESHOLD, WORDS, away_state, hold_seconds
from levain.consent.queue import ConsentQueue, Decision, authority_source, default_queue_dir
from levain.consent.record import ExternalGateRequest

AGENT = "claude_code"
EVENT = "PermissionRequest"
SETTINGS_NAME = "claude_code.json"
HOOK_SCRIPT_NAME = "levain_consent.py"
# A request is filed for at least this long even when nobody is waiting on it, so the policy seam has a live
# request to look at; it is withdrawn as soon as the hook stops waiting.
MIN_FILE_HOLD = timedelta(seconds=5)
# ``max_hold`` (the installed ``--max-hold``) is the hook's TOTAL internal deadline, counted from the moment
# ``decide`` starts. Before it waits, the hook reserves this many seconds of it for what follows the wait: the
# withdraw (one sqlite write, busy timeout 30 s) and the re-read of a decision that landed meanwhile (one
# signature verification, 10 s, ``levain.authority`` verify timeout), plus 20 s of slack: 30 + 10 + 20 = 60. So
# the wait is at most ``max_hold - CLEANUP_RESERVE`` (0 when that is negative) and the hook ends by
# ``max(max_hold, CLEANUP_RESERVE)``.
CLEANUP_RESERVE = 60.0
# Claude Code's ``timeout`` for the hook is :func:`hook_timeout`: ``ceil(max(max_hold, CLEANUP_RESERVE))`` plus
# this margin (interpreter start, imports, settings read), so it always exceeds wait + CLEANUP_RESERVE and Claude
# Code never cancels the hook while a verified answer is on its way out (a cancelled hook's output is discarded).
TIMEOUT_MARGIN = 30
DEFAULT_MAX_HOLD = 900.0
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
    """``root`` canonicalized (``~`` expanded, symlinks resolved) when it is ABSOLUTE after ``~`` expansion; else
    ``None`` (no authority). A relative path is never resolved: the hook runs in the project's directory, and a
    repository must not be able to supply the authority it is judged by."""
    if not isinstance(root, str) or not root or "\x00" in root:
        return None
    try:
        expanded = Path(root).expanduser()
        if not expanded.is_absolute():
            return None
        return os.path.realpath(expanded)
    except (OSError, ValueError, RuntimeError):
        return None


def protected_paths(queue_dir: Path, settings: AdapterSettings) -> tuple[Path, ...]:
    """What the adapter itself trusts, so a file tool writing any of it is agent config (codex Q2 r1 #1): the
    queue directory (the adapter settings and the queue database live in it) and, when a cockpit data root is
    configured, its authority database (and sqlite's sidecars) and the authority anchor."""
    out = [Path(os.path.abspath(queue_dir))]
    if settings.cockpit_data_root:
        root = Path(settings.cockpit_data_root)
        db = root / "cockpit.db"
        out += [db, *(Path(f"{db}{s}") for s in ("-wal", "-shm", "-journal")), anchor_path(root).parent]
    return tuple(out)


# --- ruling 3b item 1: agent config and hooks ----------------------------------------------------------------

# Under a ``.claude`` directory (the workspace's or ~'s), what Claude Code loads and runs: hooks, subagents, skills,
# slash commands, and the plugin install tree (``~/.claude/plugins``: each plugin's hooks, MCP servers, agents).
CLAUDE_CONFIG_DIRS = frozenset({"hooks", "agents", "skills", "commands", "plugins"})
# MCP server definitions: a ``.mcp.json`` anywhere (a workspace's). The user's ``~/.claude.json`` is matched by
# its full path in :func:`targets_agent_config`: a ``.claude.json`` elsewhere (a test fixture) is not config.
CONFIG_FILE_NAMES = frozenset({".mcp.json"})
USER_CONFIG_NAME = ".claude.json"


# The names a file inside a directory is probed under, so that a directory is agent config exactly when a file the
# checks below tag could sit directly in it (:func:`names_config_dir`): one name each directory-keyed check keys on.
# A name that is config in ANY directory (``.mcp.json``) is not probed, or every directory would be: the file-name
# field naming it is itself a candidate, tagged on its own.
_DIR_PROBES = ("x", "settings.json", "hooks.json", "config.toml", USER_CONFIG_NAME, "x.pth", "sitecustomize.py",
               "levain")


def names_config_dir(target: str, cwd: str, home: Path, roots: tuple[str, ...], sites: tuple[str, ...],
                     user_configs: frozenset[str]) -> bool:
    """For an MCP tool's every-string candidates only (:func:`levain.consent.policy.mcp_config_candidates`): True
    when a server given ``target`` as a directory and a file name in another field could write agent config:
    ``target`` (``~`` expanded, joined to ``cwd``; as written and resolved) holds a file the path checks of
    :func:`targets_agent_config` tag (each of :data:`_DIR_PROBES` directly inside it, so the directory list is
    derived from those checks, never spelled twice) or directly one of ``roots`` (a protected file such as the
    authority database, :func:`protected_paths`), or its file name is ``.claude.json`` wherever it sits (a
    server that joins a relative name to a root of its own, home among them). ``roots``, ``sites`` and
    ``user_configs`` are the caller's, computed once. Over-matching only sends the request to the human."""
    raw = Path(str(home) + target[1:]) if target == "~" or target.startswith("~/") else Path(target)
    if not raw.is_absolute():
        raw = Path(cwd) / raw
    parents = {os.path.dirname(r) for r in roots}   # a directory holding a protected file (cockpit.db) as well
    for p in (Path(os.path.normpath(raw)), Path(os.path.realpath(raw))):
        if p.name.lower() == USER_CONFIG_NAME or str(p).lower() in parents:
            return True
        for probe in _DIR_PROBES:
            if _tags_path(p / probe, roots, sites, user_configs):
                return True
    return False


def _tags_path(path: Path, roots: tuple[str, ...], sites: tuple[str, ...], user_configs: frozenset[str]) -> bool:
    """One spelling of a target path (already normalised or resolved) is agent config or the hook's own code."""
    return (_is_config_path(path) or _under(path, roots) or str(path).lower() in user_configs
            or _redirects_the_import(path, sites))


def _is_config_path(path: Path) -> bool:
    parts = [p.lower() for p in path.parts]
    if parts and parts[-1] in CONFIG_FILE_NAMES:
        return True
    for i, part in enumerate(parts[:-1]):
        rest = parts[i + 1:]
        if part == ".claude":
            if rest[0] in CLAUDE_CONFIG_DIRS or (len(rest) == 1 and rest[0].startswith("settings")
                                                  and rest[0].endswith(".json")):
                return True
        if part == ".codex" and len(rest) == 1 and rest[0] in ("hooks.json", "config.toml"):
            return True
        if part == "activation" and rest[0] == "hooks" and len(rest) > 1:
            return True  # a levain install's hook scripts (the consent hook among them) run in every agent session
        if part == ".claude-plugin":
            return True  # a plugin's manifest directory (plugin.json can declare hooks and MCP servers inline)
        if part == "hooks" and i > 0 and os.path.isdir(Path(*path.parts[:i]) / ".claude-plugin"):
            return True  # a plugin outside ~/.claude/plugins (a local marketplace, --plugin-dir): its hooks/
    return False


def nothing_can_decide(settings: AdapterSettings) -> bool:
    """No explicit word on and the cockpit the settings point at replays to no active key and no standing grant (or
    has no database, or does not replay): no grant or signed answer can verify and no hold is asked (input idle
    holds only for an enrolled key, :func:`away_hold`), so the hook
    files nothing (a queue row is kept for good) and the agent's own dialog answers. A cockpit.db the cockpit made
    before any key was enrolled is not something that can decide (L3 r1 codex MED)."""
    if settings.away_mode:
        return False
    root = settings.cockpit_data_root
    if not root or not (Path(root) / "cockpit.db").is_file():
        return True
    try:
        snap = authority_source(Path(root) / "cockpit.db", anchor=anchor_path(root))()
    except Exception:
        return True  # an authority that does not replay verifies nothing; the dialog answers
    return not snap.active_keys() and not _a_consent_grant_in_force(snap.policy.grants.values())


def _a_consent_grant_in_force(grants: Iterable[Any]) -> bool:
    """Whether any grant is a consent grant that has not expired: the two criteria of ``grant_refusal`` that hold
    for every request (L3 r2 codex MED: an expired or other-kind grant answers nothing)."""
    from datetime import datetime, timezone

    from levain.authority import consent_grant
    from levain.cockpit.consent import parse_stamp

    now = datetime.now(timezone.utc)
    for grant in grants:
        if not consent_grant(grant):
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


def targets_agent_config(tool_name: str, tool_input: Any, cwd: str, *, home: Path | None = None,
                         protected: Iterable[Path] = ()) -> bool:
    """True when a file tool (Edit/Write/MultiEdit/NotebookEdit) or an MCP tool targets an agent's config or hooks,
    in the workspace or the home directory: a ``.claude/settings*.json``; anything under a ``.claude/`` ``hooks``,
    ``agents``, ``skills``, ``commands`` or ``plugins`` directory (the adapter's own installed hook script is in
    ``.claude/hooks/``, the one `levain init` wires under the install's ``activation/hooks/``, below); a ``.mcp.json``, or the home directory's ``.claude.json``; a ``.codex/hooks.json`` or
    ``.codex/config.toml``; anything under a ``.claude-plugin/`` directory, or under ``hooks/`` beside one (a
    plugin); anything under the levain package this interpreter imports (:func:`levain_package_dir`, the hook runs
    it) or a site-directory entry that redirects that import (:func:`_redirects_the_import`); or any of
    ``protected`` (a file, or anything under a directory; the adapter passes :func:`protected_paths`). A file
    tool's target is its ``file_path``/``notebook_path``; an MCP tool's are its :data:`MCP_PATH_FIELDS` and every
    other string in its input (:func:`levain.consent.policy.mcp_config_candidates`: a server can name its path in a
    field of any name), each also tagged when it names a config directory or ``.claude.json``
    (:func:`names_config_dir`), and an input past that function's bound is tagged. Each path is checked as written (``~`` expanded, joined to ``cwd``, normalised) and as resolved through
    symlinks; either match tags it. A target that is not a non-empty string is tagged (for a file tool, so is a
    missing one). Other tools are never tagged: Bash is not parsed (ruling 1)."""
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
    home = home if home is not None else Path.home()
    try:
        roots = tuple({s for p in protected for s in (os.path.normpath(str(p)).lower(), os.path.realpath(p).lower())}
                      | {levain_package_dir().lower()})
        sites = import_site_dirs()
        user_config = Path(home) / USER_CONFIG_NAME
        user_configs = frozenset({os.path.normpath(user_config).lower(), os.path.realpath(user_config).lower()})
    except (OSError, ValueError, ImportError, TypeError):
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
        if _tags_path(written, roots, sites, user_configs) or _tags_path(resolved, roots, sites, user_configs):
            return True
    try:
        return any(names_config_dir(target, cwd, Path(home), roots, sites, user_configs) for target in extra)
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


def still_away_from(path: Path, started: AdapterSettings) -> Callable[[], tuple[bool, str, str]]:
    """The recheck a hook entry point holds with: :func:`effective_away` of a fresh read of the settings file at
    ``path``, or of ``started`` (what the hold started with) when that read fails."""
    def recheck() -> tuple[bool, str, str]:
        fresh = try_read_settings_file(path)
        return effective_away(fresh if fresh is not None else started)
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


def decide(event: Any, queue: ConsentQueue, settings: AdapterSettings, *, max_hold: float = DEFAULT_MAX_HOLD,
           policy: Policy | None = None,
           home: Path | None = None, clock: Callable[[], float] = time.monotonic,
           idle: Callable[[], float | None] | None = None,
           still_away: Callable[[], tuple[bool, str, str]] | None = None) -> dict[str, Any] | None:
    """The hook's output for one event, or ``None`` for no decision. Raises on anything unexpected (after
    withdrawing the request it filed, so no ghost stays pending); the caller (:func:`main`) turns that into no
    decision. ``max_hold`` is a total deadline: the wait ends :data:`CLEANUP_RESERVE` seconds before it. The hold is
    :func:`away_hold` (``idle`` is its input-idle source, default the platform's); ``still_away`` (:func:`main`
    passes :func:`still_away_from`) ends a hold early when it says away mode ended (the operator is back, or no
    key could sign), leaving the native dialog to ask. ``policy``
    (default :func:`levain.consent.policy.decide`) is only ever reached through :func:`guarded_policy`, called with
    the event's ``permission_mode`` as ``posture`` (``None`` when absent: no grant matches) and the tool input the
    request was filed for, and never for a request filed at :data:`AGENT_CONFIG_TIER`: a direct caller cannot
    bypass ruling 3b item 1. What
    the adapter trusts (:func:`protected_paths`) is taken from ``queue.directory`` and ``settings`` here, never
    from the caller, so no call can leave it out; a queue without a directory raises before anything is filed."""
    if getattr(queue, "directory", None) is None:
        raise TypeError("the queue has no directory: the adapter's own files cannot be protected")
    protected = protected_paths(Path(queue.directory), settings)
    deadline = clock() + max(0.0, float(max_hold)) - CLEANUP_RESERVE
    if not isinstance(event, Mapping) or event.get("hook_event_name") != EVENT:
        return None
    tool_name, tool_input, cwd = event.get("tool_name"), event.get("tool_input", {}), event.get("cwd")
    session = event.get("session_id")
    if not isinstance(tool_name, str) or not tool_name or not isinstance(cwd, str) or not cwd \
            or not isinstance(session, str) or not session:
        return None
    if tool_input is None:
        tool_input = {}  # an MCP tool with no arguments arrives without tool_input (Q0 item 4)
    agent_id = event.get("agent_id")
    if isinstance(agent_id, str) and agent_id:
        session = f"{session}#agent:{agent_id}"
    tool_use_id = event.get("tool_use_id")
    mode = event.get("permission_mode")
    posture = mode if isinstance(mode, str) and mode else None
    if policy is None and nothing_can_decide(settings):
        return None
    agent_config = targets_agent_config(tool_name, tool_input, cwd, home=home, protected=protected)
    if agent_config:
        # the gate already asked the operator about this call and delivered an allow, and Claude Code shows its
        # dialog anyway on a path it protects: deliver that allow once, never a second question
        took = queue.take_gate_allow(AGENT, session, tool_name, tool_input, cwd=cwd, within=GATE_ALLOW_REUSE,
                                     posture=posture, event=EVENT)
        if took is not None:
            return hook_output(took)
    hold = min(away_hold(settings, AGENT, idle), max(0.0, deadline - clock()))
    request = queue.file(agent=AGENT, agent_session_id=session, tool_name=tool_name, tool_input=tool_input, cwd=cwd,
                         hold=max(timedelta(seconds=hold), MIN_FILE_HOLD),
                         tier=AGENT_CONFIG_TIER if agent_config else DEFAULT_TIER,
                         event_id=tool_use_id if isinstance(tool_use_id, str) and tool_use_id else None)
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
        return hook_output(got) if got is not None else None
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
# 1. a ruled allow for this exact call (R-d) runs it, once (:meth:`ConsentQueue.take_ruled_allow`);
# 2. else the request is filed with ``expires_at`` = now + D (the library owns the deadline, B1 fix 3), the operator
#    is pinged through the notify seam (R-f; a failed ping never fails the gate), and the hook waits up to D for a
#    verified decision, which it delivers;
# 3. at D, :meth:`ConsentQueue.mark_undecided` gives the request its gap number and the hook denies with the
#    UNDECIDED text (DESIGN §3.3). The irreversible tier (T3 here; the push executor's tool is B6's,
#    :func:`gate_class`) adds top-level ``"continue": false`` + ``stopReason``: words do not stop an agent, this does
#    (runs2 RUN B, 3/3 ``terminal_reason: hook_stopped``).
#
# Every PreToolUse also carries this session's late rulings, not yet delivered, as ``additionalContext`` (DESIGN
# §3.6; :meth:`ConsentQueue.claim_rulings_for`), and, when a notify command is configured, the snoozed re-pings that
# are due (R-e). The entry is installed with ``"onFailure": "block"`` and ``timeout`` = :data:`GATE_TIMEOUT`, the
# timeout of the CEILING, never of the D set at install: D is re-read from the settings file at every run, so a D
# raised after install would otherwise outlive a timeout frozen at the old one (Claude Code would kill the hook
# mid-hold: the request left pending, no UNDECIDED text, no gap). The hold is clamped to the ceiling here too, so it
# always ends :data:`GATE_TIMEOUT_MARGIN` before the timeout. The cost: a gate that hangs (not one that holds) keeps
# the call blocked up to that timeout, not D + the margin.

GATE_EVENT = "PreToolUse"
# Claude Code's ``timeout`` for the gate entry is D + this margin (interpreter start, the undecided mark)
GATE_TIMEOUT_MARGIN = 30
# how long after the gate's allow the dialog's own PermissionRequest may take it (it follows in milliseconds)
GATE_ALLOW_REUSE = timedelta(seconds=30)
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


def gate_class(tool_name: str, tool_input: Any, cwd: str, *, home: Path | None = None,
               protected: Iterable[Path] = ()) -> str | None:
    """The gate's tier for a call: ``irreversible`` (T3, agent config and hooks, which is Q10's floor and Q14's
    irreversible tier at once, R-h), ``reversible``, or ``None`` (not gated). Bash is never parsed (ruling 1, R-g).
    THE SEAM for B6: the push executor's tool joins here as ``irreversible``."""
    if targets_agent_config(tool_name, tool_input, cwd, home=home, protected=protected):
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


def notify(settings: AdapterSettings, message: str, *, cwd: Path | str | None = None) -> bool:
    """Ping the operator (R-f): start ``notify_argv`` + ``[message]``, detached, never waited on, in ``cwd`` (the
    queue directory from the gate; the home directory when none is given), never the project the agent works in.
    The command is an absolute path or a bare name found on the child's ``PATH`` as an absolute path; a relative
    path (``bin/ping``) is refused, since it would resolve against a directory the agent can write. True when it
    started; False when none is configured, the command is refused, or it could not start. A failed ping never
    fails the gate."""
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


def send_repings(queue: ConsentQueue, settings: AdapterSettings) -> int:
    """R-e: claim the snoozed re-pings that are due and send each (only when a notify command is configured: a
    claimed re-ping is never sent again, so one is never claimed with nowhere to send it). Returns how many
    started."""
    if not settings.notify_argv:
        return 0
    sent = 0
    for reping in queue.claim_repings():
        got = queue.get(reping.request_id)
        if got is None:
            continue
        gap = queue.gap(reping.request_id)
        what = _what(got[0].tool_name, None, got[0].display)
        head = f"levain: asking again, gap #{gap}" if gap is not None else "levain: asking again"
        sent += notify(settings, f"{head}: {what} (request {reping.request_id}).", cwd=queue.directory)
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
    if decision.decision == "allow":
        return _pre("allow", f"levain: allowed by {how}.")
    return _pre("deny", f"levain: denied by {how}. Do not retry it and do not achieve it another way.")


def undecided_output(tier: str | None, what: str, gap: int) -> dict[str, Any]:
    """The deny at D: U-IRR and the hard stop for the irreversible tier, U-REV for the reversible one."""
    text = (U_IRR if tier == "irreversible" else U_REV).format(x=what, n=gap)
    return _pre("deny", text, **_stop(tier, what, f"gap #{gap} is open and waits on the operator's ruling."))


def ruling_context(queue: ConsentQueue, session: str) -> str | None:
    """This session's late rulings not yet delivered, as DESIGN §3.6's text, one per line (claimed: each reaches
    the session once), or ``None``. The ruled allow itself still runs only through ``take_ruled_allow``."""
    lines = []
    for ruling in queue.claim_rulings_for(AGENT, session):
        got = queue.get(ruling.request_id)
        what = _what(got[0].tool_name, None, got[0].display) if got is not None else f"request {ruling.request_id}"
        lines.append(RULING_TEXT[ruling.ruling].format(n=ruling.gap, x=what))
    return "\n".join(lines) or None


def _session_of(event: Mapping[str, Any]) -> str | None:
    session = event.get("session_id")
    if not isinstance(session, str) or not session:
        return None
    agent_id = event.get("agent_id")
    return f"{session}#agent:{agent_id}" if isinstance(agent_id, str) and agent_id else session


def gate(event: Any, queue: ConsentQueue, settings: AdapterSettings, *, home: Path | None = None,
         sleep: Callable[[float], None] = time.sleep) -> dict[str, Any] | None:
    """The gate's output for one ``PreToolUse`` event, or ``None`` (the call goes on as the session's mode says).
    A gated call (:func:`gate_class`) gets no decision in ``plan`` (nothing runs there, and no ruled allow is spent),
    else a ruled allow (taken once), else, in every other mode (``default`` included) and when
    something could decide (:func:`nothing_can_decide`), it is filed with its hold = D (``gate_hold_seconds``),
    pinged (:func:`notify`), held for a verified decision (delivered), and at D marked undecided and denied
    (:func:`undecided_output`). Anything that fails after the call is known gated is a deny (with the hard stop for
    the irreversible tier), never an allow: the request it filed stays pending until a read or sweep expires it.
    Late rulings for the session (:func:`ruling_context`) ride whatever is returned, as ``additionalContext``."""
    if getattr(queue, "directory", None) is None:
        raise TypeError("the queue has no directory: the adapter's own files cannot be protected")
    if not isinstance(event, Mapping) or event.get("hook_event_name") != GATE_EVENT:
        return None
    tool_name, tool_input, cwd = event.get("tool_name"), event.get("tool_input", {}), event.get("cwd")
    session = _session_of(event)
    if not isinstance(tool_name, str) or not tool_name or not isinstance(cwd, str) or not cwd or session is None:
        return None
    if tool_input is None:
        tool_input = {}
    try:
        send_repings(queue, settings)
    except Exception:
        pass  # a re-ping is a ping: it never fails the gate
    out = _gate_call(event, queue, settings, tool_name, tool_input, cwd, session, home=home, sleep=sleep)
    try:
        context = ruling_context(queue, session)
    except Exception:
        context = None  # undelivered rulings stay for the next hook
    if context:
        out = out if out is not None else {"hookSpecificOutput": {"hookEventName": GATE_EVENT}}
        out["hookSpecificOutput"]["additionalContext"] = context
    return out


def _gate_call(event: Mapping[str, Any], queue: ConsentQueue, settings: AdapterSettings, tool_name: str,
               tool_input: Any, cwd: str, session: str, *, home: Path | None,
               sleep: Callable[[float], None]) -> dict[str, Any] | None:
    from datetime import datetime, timezone

    from levain.cockpit.consent import parse_stamp

    tier = gate_class(tool_name, tool_input, cwd, home=home,
                      protected=protected_paths(Path(queue.directory), settings))
    if tier is None:
        return None
    what = _what(tool_name, tool_input)
    tool_use_id = event.get("tool_use_id")
    event_id = tool_use_id if isinstance(tool_use_id, str) and tool_use_id else None
    mode = event.get("permission_mode")
    posture = mode if isinstance(mode, str) and mode else None
    if posture == "plan":
        return None  # plan runs nothing: no decision, and a ruled allow is not spent on a call that will not run
    try:
        ruled = queue.take_ruled_allow(AGENT, session, tool_name, tool_input, cwd=cwd, event=event_id)
        if ruled is not None:
            return gate_output(ruled)
        if nothing_can_decide(settings):
            return None
        hold = min(settings.gate_hold_seconds, GATE_HOLD_CEILING)  # the installed timeout - the margin
        request = queue.file(agent=AGENT, agent_session_id=session, tool_name=tool_name, tool_input=tool_input,
                             cwd=cwd, hold=timedelta(seconds=hold), tier=AGENT_CONFIG_TIER, event_id=event_id)
    except Exception as exc:
        return _pre("deny", f"levain: the gate failed ({type(exc).__name__}), so the action was NOT taken: {what}. "
                    "Do not retry it and do not achieve it another way.", **_stop(tier, what, "the gate failed."))
    try:
        notify(settings, f"levain: {AGENT} waits on you up to {hold:g} s: {what} (request {request.id}).",
               cwd=queue.directory)
        got = _verified(queue.await_decision(request.id, hold + 5.0, poll=GATE_POLL, tool_input=tool_input,
                                             posture=posture), request)
        if got is not None:
            return gate_output(got)
        # the hold ends at expires_at (the library's deadline); mark_undecided applies at or after it
        left = (parse_stamp(request.expires_at) - datetime.now(timezone.utc)).total_seconds()
        if left > 0:
            # capped: ``left`` is wall-clock, and a clock stepped back must not carry the hold past the timeout
            # (a mark refused for being early is the deny below, never an allow)
            sleep(min(left + 0.05, GATE_SLEEP_CAP))
        marked = queue.mark_undecided(request.id, halt=tier == "irreversible", tool_input=tool_input,
                                      posture=posture)
        if isinstance(marked.decision, Decision) and _verified(marked.decision, request) is not None:
            return gate_output(marked.decision)  # an answer verified in the last moments of the hold
        if marked.state == "undecided" and marked.gap is not None:
            return undecided_output(tier, what, marked.gap)
        why = marked.reason or "no verified decision"
        return _pre("deny", f"levain: the action was NOT taken: {what} ({why}). Do not retry it and do "
                    "not achieve it another way.", **_stop(tier, what, why))
    except Exception as exc:
        return _pre("deny", f"levain: the gate failed ({type(exc).__name__}), so the action was NOT taken: {what}. "
                    "Do not retry it and do not achieve it another way.", **_stop(tier, what, "the gate failed."))


def gate_failed(event: Any, queue_dir: Path, settings: AdapterSettings) -> dict[str, Any] | None:
    """:func:`main`'s answer when the gate itself raised before it could classify the call (the queue would not
    open): a deny with the hard stop when the call is gated, or when that cannot be told; ``None`` otherwise."""
    try:
        tool_name, tool_input, cwd = event.get("tool_name"), event.get("tool_input") or {}, event.get("cwd")
        if not isinstance(tool_name, str) or not isinstance(cwd, str) or not cwd:
            return None
        tier = gate_class(tool_name, tool_input, cwd, protected=protected_paths(queue_dir, settings))
        what = _what(tool_name, tool_input)
    except Exception:
        tier, what = "irreversible", "this call"
    if tier is None:
        return None
    return _pre("deny", f"levain: the gate could not run, so the action was NOT taken: {what}. Do not retry it "
                "and do not achieve it another way.", **_stop(tier, what, "the gate could not run."))


def _queue_for(queue_dir: Path, settings: AdapterSettings) -> ConsentQueue:
    if settings.cockpit_data_root:
        root = Path(settings.cockpit_data_root)
        source = authority_source(root / "cockpit.db", anchor=anchor_path(root))
    else:
        # No cockpit data root: a source over the queue DIRECTORY, which sqlite cannot open as a database, so
        # every replay fails and no answer can verify (never a file path a writer could plant a log at).
        source = authority_source(queue_dir, anchor=None)
    return ConsentQueue(queue_dir, authority=source)


def main(argv: list[str] | None = None, stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> int:
    """The hook entry point, for both events the one script is wired to: ``PermissionRequest`` (:func:`decide`)
    and ``PreToolUse`` (:func:`gate`). Always exits 0. A PermissionRequest prints a decision only when the core
    verified one; a PreToolUse whose gate raised prints :func:`gate_failed` (a deny for a gated call)."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    event: Any = None
    queue_dir: Path | None = None
    settings = AdapterSettings()
    try:
        parser = argparse.ArgumentParser(prog="levain-consent-claude-code")
        parser.add_argument("--queue-dir", default=None)
        parser.add_argument("--max-hold", type=float, default=DEFAULT_MAX_HOLD)
        args = parser.parse_args(argv)
        queue_dir = Path(args.queue_dir) if args.queue_dir else default_queue_dir()
        settings = load_settings(queue_dir)
        event = json.loads(stdin.read())
        if isinstance(event, Mapping) and event.get("hook_event_name") == GATE_EVENT:
            out = gate(event, _queue_for(queue_dir, settings), settings)
        else:
            out = decide(event, _queue_for(queue_dir, settings), settings, max_hold=args.max_hold,
                         still_away=still_away_from(queue_dir / SETTINGS_NAME, settings))
        if out is not None:
            stdout.write(json.dumps(out) + "\n")
            stdout.flush()
    except BaseException as exc:  # noqa: BLE001 - every failure is no decision, never allow
        if isinstance(event, Mapping) and event.get("hook_event_name") == GATE_EVENT and queue_dir is not None:
            try:
                out = gate_failed(event, queue_dir, settings)
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
    (script, settings)."""
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
