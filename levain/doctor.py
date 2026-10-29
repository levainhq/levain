"""`levain doctor` — loud, in-environment health check of a Levain install.

What it checks:
  - Install layout: `seed/`, and (for a HOOKED adapter) `activation/` + hook
    scripts present and parse. A HOOKLESS adapter (openhands — detected by the
    `.levain/config.json` `adapter` marker + the absence of hosted-harness files)
    has no activation tree; that check is skipped for it, since its activation is
    the runtime condenser, not installed files.
  - Runtime: Python interpreter; `anneal-memory` available as CLI or module.
  - Store: `.levain/memory.db` opens cleanly via sqlite3.
  - Continuity headroom: `.levain/memory.continuity.md` against anneal's hard maximum for the store's schema.
  - Per detected adapter (Claude Code if `CLAUDE.md` + `.claude/`; Codex if
    `AGENTS.md`; openhands via the marker with no residue):
      * Config files parse.
      * Hooks wired (and, for Codex, wired to THIS install).
      * MCP server registered under `anneal_memory` with the install's store
        path in its arguments.
      * For Claude Code: the Python interpreter referenced in hook commands
        is resolvable.

Exits nonzero on any failure. Counter to fail-open hooks — a silent dead
install is otherwise indistinguishable from a working one.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from levain.slot import SUDO


def _supports_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


_COLOR = _supports_color()
_OK = "\033[32m✓\033[0m" if _COLOR else "[OK]"
_FAIL = "\033[31m✗\033[0m" if _COLOR else "[FAIL]"
_WARN = "\033[33m!\033[0m" if _COLOR else "[WARN]"


EXIT_HEALTHY = 0
"""Every check passed."""

EXIT_BROKEN = 1
"""At least one check failed for a reason that is NOT a pending post-upgrade step."""

EXIT_UPGRADE_PENDING = 6
"""Every failing check is a routine post-upgrade step that has not been applied yet
(``spore-840``, ruled 2026-09-13).

Its own code because the two states want opposite responses: ``1`` means something is
wrong, ``6`` means run the remedy the output names. ⛔ It is NOT a success. Until the step
is applied the PREVIOUS release's activation layer is what runs — that is why these checks
fail at all (a shipped hook fix reaches nobody who merely upgraded). The value is chosen to
stay clear of the ``levain run`` codes in ``levain.session``, asserted by
``test_the_pending_code_does_not_collide_with_levain_run_codes``.

⚠ A consumer that enumerates codes (``case $? in 1) repair ;; esac``) no longer matches this
state. That is the intended de-alert for alerting pipelines and a behaviour change for
repair scripts; the CHANGELOG says so."""


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    hint: str | None = None
    # Meaningful only when ``ok`` is False: the failure is a post-upgrade step still
    # pending, not a broken install. It changes the exit code, never the badge.
    upgrade_pending: bool = False
    # Meaningful only when ``ok`` is True: a working install with an exposure the operator should
    # act on. It changes the badge and prints the hint, never the exit code.
    warn: bool = False
    # A fact that is not there to read (no isolation install on this machine): said in words, counted as neither
    # a pass nor a failure nor a warning.
    neutral: bool = False


def _emit(r: CheckResult) -> None:
    badge = "[--]" if r.neutral else _FAIL if not r.ok else (_WARN if r.warn else _OK)
    print(f"  {badge} {r.name}: {r.detail}")
    if (not r.ok or r.warn) and r.hint:
        print(f"      → {r.hint}")


def run_doctor(path: Path, invoke: bool = False, repin_hooks: bool = False) -> int:
    # one `claude --version` per run, however many rows ask for the version
    from levain.consent.canary import versions_read_once

    with versions_read_once():
        return _run_doctor(path, invoke=invoke, repin_hooks=repin_hooks)


def _run_doctor(path: Path, invoke: bool = False, repin_hooks: bool = False) -> int:
    install = Path(str(path)).expanduser().resolve()
    print(f"Levain doctor — checking {install}\n")

    # Detect the adapter via `effective_adapter` — the SHARED classifier (doctor/verify/run
    # all use it, so they can't diverge) where hosted files DOMINATE a possibly-stale config
    # marker. So an install is HOOKLESS only when it is a clean openhands entity (marker +
    # no hosted residue): a `--force` adapter switch is read by its files, not the stale
    # marker — a healthy claude reinstall is never false-FAILed as "hookless", and an
    # openhands marker on top of a hook tree is never green-lit (its activation IS checked).
    # (Lazy import to match this module's no-top-level-levain-imports discipline.)
    from levain.install import effective_adapter

    hookless = effective_adapter(install) == "openhands"

    core: list[CheckResult] = []
    core.extend(_check_install_layout(install, expect_hooks=not hookless))
    if not hookless:
        core.extend(_check_hook_freshness(install))
        core.extend(_check_agent_hook_pins(install, repin=repin_hooks))
        core.extend(_check_cc_sandbox(install))
        core.extend(_check_cc_messaging(install))
    if not hookless:
        # Additive scope report — a hookless (openhands) entity has no hooks and
        # therefore no activation gate to report on.
        core.extend(_check_activation_scope(install))
    core.extend(_check_seed_content(install))
    core.extend(_check_recorded_answers(install))
    core.extend(_check_runtime(install))
    core.extend(_check_confinement(install))
    if hookless:
        # Both rows describe `levain run`'s bash, which runs only a clean openhands entity: on a Claude
        # Code or Codex install the Bash is the host agent's, and these rows would describe a shell that
        # install never gets.
        core.extend(_check_unix_sockets(install))
        core.extend(_check_hands_isolation(install))
    core.extend(_check_floor_placeholders())
    core.extend(_check_store(install))
    core.extend(_check_autonomic_store())
    core.extend(_check_baton(install))
    core.extend(_check_host_awake())
    core.extend(_check_cockpit_authority(install))
    core.extend(_check_authority_signers(install))
    core.extend(_check_continuity_headroom(install))
    core.extend(_check_compat_set(install))
    core.extend(_check_consent_hooks(install))
    core.extend(_check_consent_limits())
    core.extend(_check_chat_network())
    core.extend(_check_dispatch_adoption_rules())
    core.extend(_check_dispatch_verifiers())
    core.extend(_check_slot_isolation())
    core.extend(_check_vendor_copies())
    core.extend(_check_slot_limits())
    core.extend(_check_slot_keys())
    core.extend(_check_slot_services())
    for r in core:
        _emit(r)
    print(spend_cap_note())

    # Adapter detection. A hookless install (openhands) is identified by its config
    # marker (it lays down no CLAUDE.md/AGENTS.md tag-file); a hosted-harness install by
    # its tag-file. Wiring is checked downstream — a tagged install with missing wiring
    # surfaces as a wiring FAIL, not "no adapter."
    adapter_results: dict[str, list[CheckResult]] = {}
    if hookless:
        adapter_results["openhands"] = _check_openhands(install)
    else:
        if (install / "CLAUDE.md").is_file():
            adapter_results["claude-code"] = _check_claude_code(install)
        if (install / "AGENTS.md").is_file():
            adapter_results["codex"] = _check_codex(install)

    if not adapter_results:
        no_adapter = CheckResult(
            "adapter",
            False,
            "no adapter detected (no CLAUDE.md or AGENTS.md at install root)",
            "Run `levain init` in this directory to install one.",
        )
        print()
        _emit(no_adapter)
        core.append(no_adapter)

    for adapter, rs in adapter_results.items():
        print(f"\n  Adapter: {adapter}")
        for r in rs:
            _emit(r)

    all_results = core + [r for rs in adapter_results.values() for r in rs]
    failed = [r for r in all_results if not r.ok]

    # --invoke: layer the dynamic verify-hooks check on top of static checks.
    # Run UNCONDITIONALLY when --invoke is set — the failure class --invoke
    # was built to surface (silent dead hooks) is most common precisely when
    # static is partial-fail. Gating dynamic on full-static-green defeats the
    # composition. The operator chose --invoke; trust the choice.
    verify_rc = 0
    if invoke:
        print("\n  Live-fire (--invoke): running verify-hooks dynamic check...")
        from levain.verify import run_verify_hooks
        verify_rc = run_verify_hooks(install)

    broken = [r for r in failed if not r.upgrade_pending]
    pending = [r for r in failed if r.upgrade_pending]

    print()
    if failed and verify_rc != 0:
        print(f"{len(failed)} static check(s) FAILED + live-fire verify-hooks FAILED.")
    elif failed:
        print(f"{len(failed)} check(s) FAILED.")
    elif verify_rc != 0:
        print("Static checks passed but live-fire verify-hooks FAILED.")
    else:
        warned = [r for r in all_results if r.ok and r.warn]
        print("All checks passed." + (f" ({len(warned)} warning(s) above.)" if warned else ""))
    if pending and not broken and verify_rc == 0:
        print(
            f"  Every failure above is a post-upgrade step not yet applied "
            f"(exit {EXIT_UPGRADE_PENDING}, not {EXIT_BROKEN}).\n"
            "  None of them is classed as broken, but until you apply them this install\n"
            "  is still running what an earlier levain set up (its activation layer,\n"
            "  adapter carrier or memory-server registration), not what the installed\n"
            "  levain ships."
        )
    elif pending and broken:
        print(
            f"  {len(pending)} of the failures are post-upgrade steps not yet applied; "
            f"the rest are not, so doctor exits {EXIT_BROKEN}."
        )
    elif pending:
        print(
            f"  Every static failure is a post-upgrade step not yet applied, but the "
            f"live-fire check failed, so doctor exits {EXIT_BROKEN}."
        )
    if not invoke and not hookless:
        # A hookless install has no activation hooks — the verify-hooks nudge would mislead.
        print(
            "  Note: doctor is static — it does NOT invoke the activation hooks.\n"
            "        For the actual firing test, run `levain verify-hooks`\n"
            "        or `levain doctor --invoke`.\n"
            "        Also: hooks no-op when LEVAIN_HOOK_SUPPRESS=1 is in the env."
        )
    if broken or verify_rc != 0:
        return EXIT_BROKEN
    if pending:
        return EXIT_UPGRADE_PENDING
    return EXIT_HEALTHY


# The base-install REQUIRED-minimum seed set (a static check on an installed
# dir). NOT the full seed taxonomy: the canonical seed classification — which
# files load as harness context (the roster-driven adapter @import list), which
# are reached on demand through a carrier pointer, and which are not entity
# context at all (continuity.md / README.md) — lives in `levain.packs`
# (NON_IMPORT_SEED / seed_classes / import_entries / on_demand_entries). A PACK
# extends the seed set, so this fixed list checks only the base minimum; if doctor
# ever needs to validate a pack-layered install's full import list it must read the
# installed adapter file or a recorded roster, not grow this constant (Slice 3
# deferral).
#
# ⚠ `spore_instructions.md` JOINED THIS LIST 2026-08-01, WITH THE MOVE THAT MADE IT
# ON-DEMAND. It had never been here: eagerly imported, its absence surfaced anyway
# (a broken `@seed/` line in the carrier). Behind a POINTER it would not — the
# carrier would instruct the entity to read a file that is not on disk, with doctor
# reporting green. That is `absence_of_signal_rendered_as_health`, and the move to
# on-demand is what would have created it. An on-demand seed needs its existence
# checked MORE than an eager one, not less.
_SEED_REQUIRED = (
    "origin.md",
    "partnership.md",
    "world.md",
    "memory.md",
    "memory_workflow.md",
    "spore_instructions.md",
)
_SEED_EXPECTED = _SEED_REQUIRED + ("continuity.md", "README.md")
_HOOK_REQUIRED = ("session_start.py", "user_prompt_submit.py", "_levain_hook.py")
_ACTIVATION_FILES = ("posture.md", "recency_directives.md")

# Base-pack seed files the interview RENDERS (fills). A surviving BARE `{{SLOT}}`
# in one of these means the file was copied-not-rendered, or a slot was never
# asked — the interview-desync class the wiring/layout checks stay green through
# (Chip's Jul-1 rehearsal: the entity ends up not knowing its operator while
# `doctor` reports healthy). VERBATIM seeds (README.md/continuity.md) legitimately
# carry `{{...}}` documentation, so they are NOT content-checked. A pack that
# adds render targets isn't covered here (no recorded roster at doctor time) —
# these two base files carry the identity slots (OPERATOR_NAME/ENTITY_NAME) that
# matter most, so it is the right minimum.
_RENDER_TARGET_SEEDS = ("world.md", "origin.md")


def _check_consent_hooks(install: Path) -> list[CheckResult]:
    """Consent hooks installed here (Q5): a WARN when the agent is not a tested version, and for Codex when it
    does not trust the hook (`levain init`/`update` write the machine-wide hooks' trust; a workspace hook's is the
    user's) or the script is not what levain installed."""
    from levain.consent.canary import doctor_findings

    return [CheckResult(f.name, True, f.detail, f.hint, warn=f.warn) for f in doctor_findings(install)]


#: The ruled words for the authorized-spend cap (R4 DESIGN §1.1, "3a") and the sizing line (§10, 3c-2a). One home:
#: the CHANGELOG's [Unreleased] entry carries the same strings and a test holds the two together.
SPEND_CAP_WORDS = (
    "Levain enforces an authorized-spend cap: a route's per-session cap on what Levain lets an agent spend. "
    "It is a budget, not a ceiling on your bill. Calls already running when the cap is reached can finish past it. "
    "To put a hard ceiling on what a provider can bill, set a spending limit on the API key itself."
)
SPEND_CAP_SIZING_LINE = (
    "a route's per-session cap should be at least the concurrent calls × their output ceiling × the model's output "
    "rate, or calls are refused 402 before they start."
)


def spend_cap_note() -> str:
    """What the authorized-spend cap is and how to size it, printed as a NOTE after the checks: it checks nothing,
    so it carries no OK / WARN / FAIL badge and never touches the exit code. The per-credential KEY-LIMIT rows are
    not here."""
    return f"  note: {SPEND_CAP_WORDS} Sizing: {SPEND_CAP_SIZING_LINE}"


def _check_chat_network() -> list[CheckResult]:
    """Chat network's Node dependency (P-5): the Node major and the srt version, a hash mismatch refused. Not set up
    is said in words (only chat needs Node), never a failure."""
    import shutil

    from levain.chat_egress import srt_pin
    from levain.chat_egress.launch import EgressRefused, check_node

    name = "chat network"
    fix = "Run `levain chat setup` again (with Node 26 on PATH)."
    try:
        prefix = srt_pin.default_prefix()
        set_up = (prefix / srt_pin.STAMP).exists()
    except (OSError, RuntimeError) as exc:              # RuntimeError: a symlink loop under Python 3.12's resolve
        return [CheckResult(name, False, f"the chat network install could not be read: {type(exc).__name__}: {exc}",
                            fix)]
    if not set_up:
        return [CheckResult(name, True, f"not set up at {prefix} (only chat needs Node 26; `levain chat setup` "
                                        "installs its pinned dependency)", neutral=True)]
    try:
        node = check_node(shutil.which("node") or "node")
        st = srt_pin.check(prefix)
    except (EgressRefused, srt_pin.SrtProblem) as exc:
        return [CheckResult(name, False, str(exc), fix)]
    return [CheckResult(name, True, f"node {node}, srt {st.version} at {st.prefix}")]


def _check_consent_limits() -> list[CheckResult]:
    """The session budgets (Q13): the limits in force per agent and where they came from, the sessions counted, how
    many a limit halted, and a WARN when any root's counter is failing to count (its calls proceed uncounted)."""
    from levain.consent import limits
    from levain.install import _consent_queue_dir

    name = "consent limits"
    try:
        qdir = _consent_queue_dir()
        forces, not_counting, halted, counted = [], [], 0, 0
        for agent in limits.AGENTS:
            lim, src = limits.load_limits(qdir, agent)
            shown = ", ".join(f"{k} {limits._num_text(k, v)}" for k, v in lim.items())
            forces.append(f"{agent}: {shown} [{src}]")
        for row in limits.snapshot(qdir):
            counted += 1
            halted += 1 if row["halt"] else 0
            if limits._fail_read(limits._fail_path(qdir, row["agent"], row["root"])):
                not_counting.append(row["root"][:12])
    except Exception as exc:  # noqa: BLE001 - a row that cannot be built is said, never a crash
        return [CheckResult(name, True, f"could not be read ({type(exc).__name__}: {exc})",
                            "Check the consent directory's permissions.", warn=True)]
    detail = f"{'; '.join(forces)}; {counted} session(s) counted, {halted} halted"
    if not_counting:
        return [CheckResult(name, True, f"NOT COUNTING: {', '.join(not_counting)} (the limits store is failing "
                                        f"to take their calls, which go on uncounted); {detail}",
                            f"Look at {qdir / limits.DB_NAME}: a full disk, a store of another levain version or a "
                            "corrupt file. The next call is counted once it can be written.", warn=True)]
    return [CheckResult(name, True, detail)]


# --- agent-session isolation: rows over facts root already writes (S1 session 6a) ---------------------------------

_SLOT_USABLE = ("leased", "scrubbed", "reusable")
_SLOT_RECORD_CAP = 1 << 16
#: The pool's configured class -> the slot class it counts (L/policy/pool: ``slots`` egress, ``verifier_slots`` none).
_POOL_CLASSES = (("egress", "e", "egress"), ("verifier", "v", "none"))


def _slot_why(exc: BaseException) -> str:
    if isinstance(exc, PermissionError):
        return "permission denied for your account"
    name = getattr(exc, "name", None)
    return f"{name}: {getattr(exc, 'detail', '')}".rstrip(": ") if name else f"{type(exc).__name__}: {exc}"


def _read_slot_dirs(slots: Path) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """(tag -> (kind, why), ignored names): kind is the state of an in-service slot, ``retired`` (a tombstone) or
    ``bad`` (not usable, ``why`` says why; an absent state file is ``bad`` with "state missing", as the runtime
    reads an absent state as not reusable). ``ignored`` are entries the runtime does not count as slots
    (non-canonical names, links). Names and links are judged as the runtime helper judges them: a canonical tag
    naming a real directory, a state file that is a regular file naming this slot."""
    from levain.slot import Refused
    from levain.slot import records as R
    from levain.slot.ids import SlotId
    from levain.slot.layout import POOL_LOCK_NAME

    out: dict[str, tuple[str, str]] = {}
    ignored: list[str] = []
    for name in sorted(os.listdir(slots)):
        if name == POOL_LOCK_NAME:      # install's own pool lock: infrastructure, not an entry to report
            continue
        if len(name) < 2 or name[0] not in "ev" or not name[1:].isdigit() or str(int(name[1:])) != name[1:] \
                or int(name[1:]) >= 64:
            ignored.append(name)
            continue
        d = slots / name
        if not stat.S_ISDIR(os.lstat(d).st_mode):
            ignored.append(name)      # a link or a file: the runtime does not count it as a slot either
            continue
        want = SlotId("egress" if name[0] == "e" else "none", int(name[1:]))
        try:
            data = R.read_record(d / "state", max_bytes=_SLOT_RECORD_CAP)
        except (Refused, OSError) as exc:
            out[name] = ("bad", f"state unreadable: {_slot_why(exc)}")
            continue
        if data is None:
            out[name] = ("bad", "state missing")
            continue
        try:
            rec = R.parse_slot_state(data)
        except Exception as exc:  # noqa: BLE001 - a state file that does not parse is a slot that is not usable
            out[name] = ("bad", f"state does not parse ({type(exc).__name__})")
            continue
        if rec.slot != want:
            out[name] = ("bad", f"state names {rec.slot.tag}")
        else:
            out[name] = (rec.state, "")
    return out, ignored


def _check_slot_isolation(*, host: str | None = None, store: Path | None = None,
                          free_bytes: Any = None, now: float | None = None,
                          seams: Any = None) -> list[CheckResult]:
    """Read-only rows over the isolation install's facts the OPERATOR can read (doctor runs as the operator): the
    pool's configured counts (``policy/pool``), the slots' state files (root-owned 0644 in 0755 dirs; the group is
    ``wheel`` on macOS) and the store volume's free space. Nothing is leased, probed or run as a slot user. A fact that
    is not there is said as not there, one that cannot be read as unreadable, never as healthy; no read here can
    abort the doctor. Retired slot directories are tombstones (replenish never removes them): they are never counted."""
    from levain.slot import Refused, layout
    from levain.slot import records as R
    from levain.slot.end import V5_FLOOR_BYTES

    if host is None:
        host = "darwin" if sys.platform == "darwin" else "linux"
    store = store if store is not None else layout.STORE.get(host)
    try:
        if store is None:
            return [CheckResult("agent sessions", True, "not installed", neutral=True)]
        try:
            store_mode = os.lstat(store).st_mode      # lstat, not is_dir(): is_dir() follows a link to nothing
        except FileNotFoundError:
            return [CheckResult("agent sessions", True, "not installed", neutral=True)]
        if not stat.S_ISLNK(store_mode) and not stat.S_ISDIR(store_mode):
            return [CheckResult("agent sessions", False, f"{store} is not a directory; the runtime refuses it, so "
                                "no session can be started",
                                "Remove it and let the install helper create the real directory.")]
        for part in (store, store / "slots", store / "policy"):   # the runtime opens each O_NOFOLLOW: a link is not it
            try:
                is_link = stat.S_ISLNK(os.lstat(part).st_mode)
            except FileNotFoundError:
                continue
            if is_link:
                return [CheckResult("agent sessions", False, f"{part} is a symbolic link; the runtime refuses a "
                                    "linked store, so no session can be started",
                                    "Replace it with the real directory (the install helper creates it).")]
        if not (store / "slots").is_dir():
            return [CheckResult("agent sessions", False, f"partially installed: no slots directory at {store}",
                                "The isolation install is incomplete; its install helper repairs it.")]
        rows = _slot_rows(store, layout, R, Refused)
        rows += _front_log_rows(store, host, layout, R, seams)
    except OSError as exc:
        rows = [CheckResult("agent slots", True, f"could not be read ({_slot_why(exc)})", warn=True)]
    else:
        try:
            rows += _probe_rows(store, R, Refused, time.time() if now is None else now)
        except OSError as exc:
            rows.append(CheckResult("agent firewall probe", True, f"could not be read ({_slot_why(exc)})",
                                    warn=True))

    try:
        free = (free_bytes if free_bytes is not None
                else (lambda p: (lambda v: v.f_bavail * v.f_frsize)(os.statvfs(p))))(store)
    except OSError as exc:
        rows.append(CheckResult("agent store free space", True, f"could not be read ({exc})", warn=True))
    else:
        gib = free / (1 << 30)
        if free < V5_FLOOR_BYTES:
            rows.append(CheckResult("agent store free space", False,
                                    f"{gib:.1f} GiB free, below the {V5_FLOOR_BYTES >> 30} GiB floor; new sessions "
                                    "are refused and running ones are braked",
                                    f"Free space on the volume that holds {store}."))
        else:
            rows.append(CheckResult("agent store free space", True, f"{gib:.1f} GiB free"))
    return rows


def _operator_home(*, env: Any = None, getuid: Any = None, getpwuid: Any = None) -> str:
    """The home the vendor search starts from, resolved the way the install helper's repair resolves its operator:
    under sudo, the account ``SUDO_UID`` names (never root's ``~``); else the account running doctor."""
    import pwd

    env = os.environ if env is None else env
    uid = (getuid or os.getuid)()
    sudo = env.get("SUDO_UID", "")
    if uid == 0 and sudo.isdigit():
        uid = int(sudo)
    return (getpwuid or pwd.getpwuid)(uid).pw_dir


def _check_vendor_copies(*, host: str | None = None, store: Path | None = None, home: str | None = None,
                         state: Any = None) -> list[CheckResult]:
    """The ``agent vendor CLIs`` row: each vendor CLI a session runs is root's copy of its install unit in
    ``VENDOR_PREFIX`` (setup and repair make it, head ruling 1026+8), checked with the predicate start's check reads
    and compared byte for byte with the vendor it is copied from (:func:`levain.slot.root_tree.vendor_state`, the same
    search repair runs, from the operator's home, :func:`_operator_home`). A copy that differs is a vendor updated
    since the last repair: sessions keep running the old one until repair re-copies it. No row when there is no
    isolation install."""
    from levain.slot import layout
    from levain.slot import root_tree as RT

    if host is None:
        host = "darwin" if sys.platform == "darwin" else "linux"
    store = store if store is not None else layout.STORE.get(host)
    if store is None or not os.path.lexists(store):
        return []
    if home is None and state is None:
        try:
            home = _operator_home()
        except (KeyError, OSError) as exc:
            return [CheckResult("agent vendor CLIs", True, f"the operator's home could not be read ({exc})",
                                warn=True)]
    read = state if state is not None else (lambda name: RT.vendor_state(host, name, home))
    parts, fix, orphan = [], [], []
    for name in RT.VENDORS:
        try:
            st, detail = read(name)
        except Exception as exc:     # V13: a probe that crashes is a row, never doctor's crash
            st, detail = "unreadable", f"could not be read ({_slot_why(exc)})"
        if st == "current":
            parts.append(f"{name} current")
        elif st == "absent":
            parts.append(f"{name} not on this machine")
        elif st == "stale":
            fix.append(name)
            parts.append(f"{name} STALE: {detail}")
        elif st == "not-copied":
            fix.append(name)
            parts.append(f"{name} NOT COPIED: {detail}; its sessions are refused vendor-not-installed:{name}")
        elif st == "bad-copy":
            fix.append(name)
            parts.append(f"{name} BAD COPY: {detail}; its sessions are refused vendor-not-installed:{name}")
        elif st == "unreadable":
            fix.append(name)
            parts.append(f"{name} UNREADABLE: {detail}")
        else:   # orphan
            orphan.append(name)
            parts.append(f"{name}: {detail}")
    repair = f"sudo {layout.install_helper_path(host)} repair"
    if fix:
        return [CheckResult("agent vendor CLIs", True, "; ".join(parts), warn=True,
                            hint=f"Run `{repair}`: it copies {', '.join(fix)} into {RT.VENDOR_PREFIX[host]} again, "
                                 "where sessions run it.")]
    if orphan:
        return [CheckResult("agent vendor CLIs", True, "; ".join(parts), warn=True,
                            hint="A copy with no vendor to compare it with keeps running as it is; repair leaves it.")]
    return [CheckResult("agent vendor CLIs", True, "; ".join(parts))]


def _slot_rows(store: Path, layout: Any, R: Any, Refused: Any) -> list[CheckResult]:
    """The ``agent slots`` row: in-service usable slots per class against ``policy/pool``'s counts."""
    try:
        raw = R.read_record(store / "policy" / "pool", max_bytes=_SLOT_RECORD_CAP)
        want = layout.parse_pool_config(raw) if raw is not None else None
    except (Refused, OSError) as exc:
        return [CheckResult("agent slots", False, f"the pool configuration cannot be read ({_slot_why(exc)})",
                            f"Look at {store / 'policy' / 'pool'}.")]
    found, ignored = _read_slot_dirs(store / "slots")
    live = {t: v for t, v in found.items() if v[0] not in R.OUT_OF_SERVICE}
    tombs = sorted(t for t, v in found.items() if v[0] in R.OUT_OF_SERVICE)
    usable = {t for t, v in live.items() if v[0] in _SLOT_USABLE}
    bad = [f"{t} ({v[1] or v[0]})" for t, v in live.items() if t not in usable]
    note = ((f"; {len(tombs)} retired, not counted" if tombs else "")
            + (f"; ignored (not slots): {', '.join(ignored)}" if ignored else ""))
    if want is None:
        return [CheckResult("agent slots", False,
                            "the pool configuration is missing, so capacity cannot be judged and replenishment "
                            f"refuses; {len(usable)} usable"
                            + (f"; not usable: {', '.join(bad)}" if bad else "") + note,
                            f"Look at {store / 'policy' / 'pool'}; the install helper writes it.")]
    short = []
    for (_label, prefix, _cls), need in zip(_POOL_CLASSES, want):
        have = sum(1 for t in usable if t[0] == prefix)
        if have < need:
            short.append(f"{_label} {have} of {need}")
    total = sum(want)
    detail = f"{len(usable)} usable of {total} required"
    if short:
        return [CheckResult("agent slots", False, f"{detail}; short: {', '.join(short)}"
                            + (f"; not usable: {', '.join(bad)}" if bad else "") + note,
                            "The pool is below its configured counts; its install helper replenishes it.")]
    return [CheckResult("agent slots", True, detail + (f"; not usable (extra): {', '.join(bad)}" if bad else "")
                        + note)]


def _front_log_rows(store: Path, host: str, layout: Any, R: Any, seams: Any = None) -> list[CheckResult]:
    """The ``agent front logs`` row: for each in-service egress slot, what a session start would refuse about its
    front log dirs (``L/gw/``, ``L/gw/slot<n>/``, S1 3b-8a), asked of the runtime itself
    (``runtime_helper.check_front_log_dirs``: render's own front-account lookup and ``prepare_front_log``'s own walk
    up to its final open, read-only), never re-derived here. Every slot is gathered into the one row, each named
    under its own clause only. A ``store-missing`` there is a dir that is genuinely absent (the walk raises it only on
    ``FileNotFoundError``): an install made before the dir existed, said with the repair line. Any other refusal
    (``bad-root``: a link, a non-directory, a wrong owner or mode; ``account-missing``, ``account-out-of-range``) is
    said refused. What the walk cannot read is said not judged, never refused. The row's verdict is the worst real
    one: any refusal fails it, absences alone make it upgrade-pending, unread alone warns. No egress slot: no row.
    ``seams``: the runtime's, for tests (its ``store`` is set to ``store``); the default reads the real accounts."""
    import dataclasses

    from levain.slot import Refused
    from levain.slot import runtime_helper as H
    from levain.slot.ids import SlotId

    found, _ignored = _read_slot_dirs(store / "slots")
    tags = sorted((t for t, v in found.items() if t[0] == "e" and v[0] not in R.OUT_OF_SERVICE),
                  key=lambda t: int(t[1:]))
    if not tags:
        return []
    name = "agent front logs"
    seams = H.Seams(host=host, store=store) if seams is None else dataclasses.replace(seams, store=store)
    try:
        st = H.Store(seams)
    except Refused as exc:
        if exc.name == "bad-root":
            return [CheckResult(name, False, f"{store} is not root's alone; the runtime refuses it (bad-root), so no "
                                "session can be started", f"Look at its owner and mode; `{layout.repair_line(host)}` "
                                "sets them back to setup's.")]
        # Store maps every open error of the root to store-missing; the caller saw the root there, so it is unreadable
        return [CheckResult(name, True, f"could not be read ({_slot_why(exc)})", warn=True)]
    absent: dict[str, list[str]] = {}       # refusal text -> tags, in the order first met
    failed: dict[str, list[str]] = {}
    unread: list[str] = []
    try:
        for t in tags:
            try:
                refusals = H.check_front_log_dirs(st, SlotId("egress", int(t[1:])))
            except (OSError, LookupError) as exc:   # unreadable is said as unreadable, never as present or absent
                unread.append(f"{t} ({_slot_why(exc)})")
                continue
            for r in refusals:
                (absent if r.name == "store-missing" else failed).setdefault(f"{r.detail} ({r.name})", []).append(t)
    finally:
        st.close()

    def said(header: str, groups: dict[str, list[str]]) -> list[str]:
        return [header + "; ".join(f"{', '.join(ts)}: {why}" for why, ts in groups.items())] if groups else []

    # each slot under its own verdict only: a slot never judged is never said refused (codex L3 r5)
    clauses = (said("refused, so no session on them starts: ", failed)
               + said("refused until the missing dir is made: ", absent)
               + (["could not be checked, so not judged: " + ", ".join(unread)] if unread else []))
    detail = ". ".join(clauses)
    unjudged = (" What could not be checked is not judged; run the doctor again once it can read them."
                if unread else "")
    if failed:
        return [CheckResult(name, False, detail,
                            f"Look at what is named. `{layout.repair_line(host)}` sets each dir's owner and mode back "
                            "to setup's; it does not replace a link or a file standing where a dir belongs, and it "
                            "never makes a missing account (undo, then setup, does)." + unjudged)]
    if absent:
        return [CheckResult(name, False, detail,
                            f"Run `{layout.repair_line(host)}` once (an install made before the front log moved to the "
                            "store has none)." + unjudged, upgrade_pending=True)]
    if unread:
        return [CheckResult(name, True, detail, unjudged.strip(), warn=True)]
    return [CheckResult(name, True, f"{len(tags)} egress slot(s), each with its front log directory")]


#: V13 (O4, N10): the record root writes after each firewall probe, beside the slot's state (levain.slot.probe's
#: RECORD_NAME / RECORD_FORMAT; a test holds the two equal). Doctor reads it and never probes.
_PROBE_RECORD_NAME = "probe"
_PROBE_RECORD_FORMAT = "levain-probe-result/1"
_PROBE_KEYS = frozenset({"format", "at", "session", "outcome", "cause"})
_PROBE_OUTCOMES = ("closed", "open", "inconclusive", "unknown")
#: DECISION (V13 §7): a closed result older than this is said stale; a gauge says its own age.
_PROBE_STALE_S = 24 * 3600
_PROBE_SKEW_S = 300           # DECISION: a stamp this far ahead of the clock is not read as fresh


def _age_text(s: float) -> str:
    s = max(0, int(s))
    if s < 120:
        return f"{s} s"
    if s < 120 * 60:
        return f"{s // 60} min"
    if s < 48 * 3600:
        return f"{s // 3600} h"
    return f"{s // 86400} days"


def _probe_one(path: Path, now: float, R: Any, Refused: Any) -> tuple[int, str]:
    """(level, words) for one slot's record: level 0 ok, 1 not probed yet, 2 warn, 3 bad. The stamp is parsed with
    the writer's own format (``levain.slot.probe.RECORD_AT_FORMAT``) and must spell back to itself; an ``open``
    record is a failure whatever its stamp says (a stamp anomaly is appended); the record's text is marked before
    it is shown."""
    import calendar

    from levain import unshowable
    from levain.contracts import ContractError, canon
    from levain.slot import probe as P

    try:
        raw = R.read_record(path, max_bytes=_SLOT_RECORD_CAP)
    except (Refused, OSError) as exc:
        return 2, f"record unreadable ({_slot_why(exc)})"
    if raw is None:
        return 1, "not probed yet"
    try:
        rec = canon.parse(raw)
        if type(rec) is not dict or set(rec) != _PROBE_KEYS or rec["format"] != _PROBE_RECORD_FORMAT \
                or rec["outcome"] not in _PROBE_OUTCOMES or not all(type(rec[k]) is str for k in _PROBE_KEYS):
            raise ValueError("shape")
    except (ContractError, ValueError):
        return 2, "record unreadable (not a levain-probe-result/1 record)"
    outcome = rec["outcome"]
    sess, cause = unshowable.mark(rec["session"][:64], allow=""), unshowable.mark(rec["cause"][:96], allow="")
    try:
        parsed = time.strptime(rec["at"], P.RECORD_AT_FORMAT)
        if time.strftime(P.RECORD_AT_FORMAT, parsed) != rec["at"]:
            raise ValueError("at")
        at = calendar.timegm(parsed)
    except (ValueError, OverflowError):
        if outcome == "open":
            return 3, f"OPEN at its last probe (stamp unreadable): the slot reached a vendor address ({cause}; session {sess})"
        return 2, "record unreadable (not a levain-probe-result/1 record)"
    age = now - at
    future = age < -_PROBE_SKEW_S
    if outcome == "open":
        when = f"stamped in the future ({rec['at']})" if future else f"{_age_text(age)} ago"
        return 3, f"OPEN at its last probe, {when}: the slot reached a vendor address (session {sess})"
    if future:
        return 2, f"stamped in the future ({rec['at']}; session {sess})"
    when = f"{_age_text(age)} ago"
    if outcome != "closed":
        return 2, f"{outcome} ({cause}) {when} (session {sess})"
    if age > _PROBE_STALE_S:
        return 2, f"closed {when}, stale: over {_PROBE_STALE_S // 3600} h (session {sess})"
    return 0, f"closed {when} (session {sess})"


def _probe_rows(store: Path, R: Any, Refused: Any, now: float) -> list[CheckResult]:
    """The ``agent firewall probe`` row (V13 §7, O4): each in-service egress slot's LAST RECORDED probe, as a gauge
    naming its age. Doctor leases nothing and probes nothing: the record is what the last session launch (and, on
    macOS, its re-probes) found. OPEN is a failure; unknown, inconclusive, unreadable and stale results warn; a slot
    never probed is said so and never read as healthy. No egress slot in service: no row."""
    found, _ignored = _read_slot_dirs(store / "slots")
    tags = [t for t, v in sorted(found.items()) if t.startswith("e") and v[0] not in R.OUT_OF_SERVICE]
    if not tags:
        return []
    per = [(t, *_probe_one(store / "slots" / t / _PROBE_RECORD_NAME, now, R, Refused)) for t in tags]
    level = max(lv for _t, lv, _w in per)
    detail = "; ".join(f"{t}: {w}" for t, _lv, w in per)
    name = "agent firewall probe"
    if level == 3:
        return [CheckResult(name, False, detail,
                            "A session on that slot reached the vendor directly, outside the slot's network wall "
                            "(pf on macOS, the slot's network namespace on Linux): the wall did not hold. Every "
                            "launch probes again and refuses while it is open.")]
    if level == 2:
        return [CheckResult(name, True, detail,
                            "The last probe could not show the wall holding, or its result is old. The next session "
                            "launch on that slot probes again, and refuses to start unless the wall holds.", warn=True)]
    if level == 1:
        return [CheckResult(name, True, detail + " (each session launch probes its slot)", neutral=True)]
    return [CheckResult(name, True, detail)]


def _qbroker_running(host: str) -> bool | None:
    """Whether the queue broker's unit runs, as the operator can see it: ``systemctl is-active`` (Linux) or
    ``launchctl print system/<label>``'s ``state = running`` (macOS). ``None`` when it cannot be told."""
    import subprocess

    from levain.launch import child_env
    from levain.slot import units

    svc = units.by_name("qbroker")
    argv = (["systemctl", "is-active", "--quiet", units.systemd_name(svc)] if host == "linux"
            else ["/bin/launchctl", "print", f"system/{units.launchd_label(svc)}"])
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=10, env=child_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    if host == "linux":
        return r.returncode == 0 if r.returncode in (0, 3) else None    # 3: inactive or failed; others: unknown
    if r.returncode != 0:
        return False if r.returncode == 113 else None                    # 113: no such service loaded
    return any(line.strip() == "state = running" for line in r.stdout.splitlines())


def _check_slot_limits(*, host: str | None = None, store: Path | None = None, owner: int = 0,
                       running: Any = None) -> list[CheckResult]:
    """The ``slot limits`` row: the limits each agent's slot sessions run under, read by the queue broker's own
    reader (:class:`levain.qbroker.limits.PolicyLimits`) from ``L/policy/limits`` (root 0644, readable by the
    operator), so the row and the broker cannot disagree: per agent, and whether they come from the file or the
    defaults. FAIL when the file is present but unusable (the broker then refuses every limited slot call), with the
    broker's own reason. WARN when the host has a slot pool and the queue broker is not running (stated, not
    enforced). No row before the isolation install (the ``agent sessions`` row says so). The counts are not read:
    they live in the broker's own 0700 directory. ``owner``/``running``: a test's seams."""
    import logging

    from levain.consent.limits import _num_text
    from levain.qbroker.limits import LimitsUnavailable, PolicyLimits
    from levain.slot import layout
    from levain.slot import limits_doc as LD

    name = "slot limits"
    if host is None:
        host = "darwin" if sys.platform == "darwin" else "linux"
    store = store if store is not None else layout.STORE.get(host)
    if store is None or not (store / "policy").is_dir():
        return []
    path = store / "policy" / layout.policy_limits_path(host).name
    reader = PolicyLimits(store / "policy", owner=owner)
    broker_log = logging.getLogger("levain.qbroker")
    quiet = broker_log.disabled
    broker_log.disabled = True        # the reader logs an unusable file for the broker's log; this row says it instead
    try:
        shown = []
        for agent in LD.AGENTS:
            lim = reader(agent, "doctor")
            shown.append(f"{agent}: " + ", ".join(f"{k.replace('_', ' ')} {_num_text(k, lim[k])}"
                                                  for k in LD.SLOT_KINDS))
    except LimitsUnavailable as exc:
        return [CheckResult(name, False, f"{exc.reason}. Until it is, the queue broker refuses every slot tool call "
                                         "it gates (limits-failed)",
                            "Write the limits again with `levain limits set --slots <agent|default> <kind> <value>` "
                            "(the install helper replaces the file whole).")]
    except Exception as exc:  # noqa: BLE001 - a row that cannot be built is said, never a crash
        return [CheckResult(name, True, f"could not be read ({type(exc).__name__}: {exc})",
                            f"Look at {path}.", warn=True)]
    finally:
        broker_log.disabled = quiet
    src = str(path) if os.path.lexists(path) else "defaults (no file)"
    detail = f"{'; '.join(shown)} [{src}]"
    if (store / "policy" / "pool").exists():
        up = (running or _qbroker_running)(host)
        if up is not True:
            why = "is not running" if up is False else "could not be checked"
            return [CheckResult(name, True, f"{detail}; the queue broker {why}, so these limits are not enforced",
                                "Start the Levain queue broker unit (levain-qbroker on Linux, com.levain.qbroker on macOS).",
                                warn=True)]
    return [CheckResult(name, True, detail)]


# --- 3c-9: the brokered-spend rows (route trust, keys, the spend ledger, the invoice ceiling) --------------------

#: OpenRouter's key-status endpoint: ``limit`` and ``limit_remaining`` for the key that asks (US dollars).
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
#: The most one limit read may take in total (name lookup through the last body byte): doctor never hangs on a provider.
_LIMIT_FETCH_TIMEOUT_S = 5.0
#: The most wall clock all the provider reads of one doctor run may use together.
_LIMIT_TOTAL_S = 10.0
_LIMIT_ANSWER_CAP = 64 * 1024
#: The clock every deadline in these rows is read on (a test's seam: patching it never reaches ``time`` itself).
_clock = time.monotonic
#: DECISION: the wait for CONTROL's ``KEYS-STATUS`` answer (keys' own report of what the operator cannot read), asked
#: at most once per run and only when a row meets keys' 0700 directory.
_KEYS_STATUS_S = 3.0
#: Provider hosts whose spend limit is an operator setting Levain cannot read: host -> (what the limit is called).
_PROVIDER_LIMIT_SETTINGS = {"api.anthropic.com": "workspace spend limit", "api.openai.com": "project spend limit"}


def _unit_running(host: str, name: str) -> bool | None:
    """Whether the named slot service's unit runs, as the operator can see it (``systemctl is-active`` on Linux,
    ``launchctl print``'s ``state = running`` on macOS); ``None`` when it cannot be told. A unit the host does not know
    (``is-active`` exit 4, ``launchctl`` exit 113) is not running."""
    import subprocess

    from levain.launch import child_env
    from levain.slot import units

    svc = units.by_name(name)
    argv = (["systemctl", "is-active", "--quiet", units.systemd_name(svc)] if host == "linux"
            else ["/bin/launchctl", "print", f"system/{units.launchd_label(svc)}"])
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=10, env=child_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    if host == "linux":
        return r.returncode == 0 if r.returncode in (0, 3, 4) else None
    if r.returncode != 0:
        return False if r.returncode == 113 else None
    return any(line.strip() == "state = running" for line in r.stdout.splitlines())


def _by_deadline(build: Any, deadline: float, what: str) -> Any:
    """``build()``'s value, run on a daemon thread joined to ``deadline`` (on :data:`_clock`): its exception is raised
    here, and ``TimeoutError`` when it is not over by then (the thread is left to finish on its own)."""
    import threading

    box: dict = {}

    def run() -> None:
        try:
            box["ok"] = build()
        except BaseException as exc:  # noqa: BLE001 - handed to the caller's thread
            box["err"] = exc

    t = threading.Thread(target=run, name="levain-doctor-deadline", daemon=True)
    t.start()
    t.join(max(0.0, deadline - _clock()))
    if "ok" in box:
        return box["ok"]
    if "err" in box:
        raise box["err"]
    raise TimeoutError(f"{what} passed its deadline")


def _fetch_key_limit(url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
    """One GET of ``url`` with ``headers`` that is over within ``timeout`` seconds in TOTAL (name lookup, connect,
    headers and the body, which is read in chunks): ``(status, at most _LIMIT_ANSWER_CAP bytes)``. A redirect is never
    followed (the credential goes to the host it is bound to and nowhere else). Raises on any transport failure,
    ``TimeoutError`` past the deadline."""
    deadline = _clock() + timeout
    return _by_deadline(lambda: _fetch_once(url, headers, deadline), deadline, "the key-limit read")


def _fetch_once(url: str, headers: dict, deadline: float) -> tuple[int, bytes]:
    import urllib.error
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a: Any, **k: Any) -> None:
            return None

    def body(resp: Any) -> bytes:
        out = b""
        while len(out) < _LIMIT_ANSWER_CAP:
            if _clock() > deadline:
                raise TimeoutError("the key-limit answer passed its deadline")
            part = resp.read(min(4096, _LIMIT_ANSWER_CAP - len(out)))
            if not part:
                break
            out += part
        return out

    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with opener.open(req, timeout=max(0.1, deadline - _clock())) as resp:
            return resp.status, body(resp)
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, body(exc)


def _check_route_trust(store: Path) -> CheckResult:
    from levain import authority_sig
    from levain.slot import Refused
    from levain.slot import routemaps

    name = "route trust"
    routes = store / "routes"
    present = [os.path.lexists(routes / f) for f in (routemaps.TRUST_PUB, routemaps.TRUST_JSON)]
    if not any(present):
        return CheckResult(name, True, "not pinned: dispatch route maps cannot be verified, so dispatch routes "
                           "are refused", "Run `levain dispatch config init` (or `levain dispatch config trust` "
                           "if the store exists) to pin it.", warn=True)
    try:
        t = routemaps.read_trust(routes)
    except Refused as exc:
        return CheckResult(name, False, f"{_slot_why(exc)}; every dispatch route is refused",
                           "Run `levain dispatch config trust` to write the pair again.")
    try:
        actual = authority_sig.public_key_fingerprint(t.public_key)
    except authority_sig.VerifierUnavailable as exc:
        return CheckResult(name, True, f"trust.pub was not checked: {exc}; keys cannot verify a dispatch route map "
                           "without ssh-keygen, so every dispatch route is refused", warn=True)
    if actual is None:
        return CheckResult(name, False, "trust.pub is not a public key; every dispatch route is refused",
                           "Run `levain dispatch config trust` to write the pair again.")
    if actual != t.fingerprint:
        return CheckResult(name, False, f"trust.pub's fingerprint is {actual} but trust.json pins {t.fingerprint}; "
                           "every dispatch signature is refused",
                           "Run `levain dispatch config trust` to write the pair again.")
    return CheckResult(name, True, f"pinned to {t.fingerprint} for store {t.store_id}")


def _keys_status_asker(run_dir: Path, budget_s: float) -> Any:
    """A callable answering CONTROL's ``KEYS-STATUS`` (3c-9 session D: keys' report of its ledger size and the
    brokered routes it serves, which the operator's account cannot read), asked on its first call only and answered
    within ``budget_s`` of that call; ``None`` for any failure (CONTROL or keys unreachable, a refusal, an answer off its shape), which a
    row says as keys having given none to this run."""
    memo: list = []

    def ask() -> Any:
        if not memo:
            memo.append(_ask_keys_status(run_dir, _clock() + budget_s))
        return memo[0]
    return ask


class _ByDeadline:
    """A socket whose every blocking operation waits only what is left of ONE ``deadline`` (on :data:`_clock`), so
    a peer answering a byte at a time cannot stretch it (a socket timeout alone restarts at each operation)."""

    def __init__(self, sock: Any, deadline: float) -> None:
        self._sock, self._deadline = sock, deadline

    def _left(self) -> float:
        left = self._deadline - _clock()
        if left <= 0:
            raise TimeoutError("the deadline passed")
        self._sock.settimeout(left)
        return left

    def connect(self, path: str) -> None:
        self._left()
        self._sock.connect(path)

    def sendall(self, data: bytes) -> None:
        self._left()
        self._sock.sendall(data)   # one bounded call: since 3.5 the timeout is the whole send's

    def recv(self, n: int) -> bytes:
        self._left()
        return self._sock.recv(n)

    def gettimeout(self) -> float | None:
        return self._sock.gettimeout()

    def settimeout(self, _t: float | None) -> None:
        pass   # every operation sets its own from the deadline


def _ask_keys_status(run_dir: Path, deadline: float) -> dict | None:
    """One ``KEYS-STATUS`` on CONTROL's socket, framed by CONTROL's own ``encode_frame``/``read_frame`` and inside
    ``deadline`` as a whole: connect, send and each receive wait only what is left of it."""
    import socket

    try:
        from levain.slot import control

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as raw:
            s = _ByDeadline(raw, deadline)
            s.connect(str(run_dir / "control" / "control.sock"))
            s.sendall(control.encode_frame({"v": 1, "op": "KEYS-STATUS", "id": 1}))
            ans = control.read_frame(s)  # type: ignore[arg-type]
        if ans is None or ans.get("ok") is not True or ans.get("id") != 1:
            return None
        return control._keys_status_answer({k: v for k, v in ans.items() if k not in ("id", "ok")})
    except Exception:  # noqa: BLE001 - any failure is "not told", said as held by keys
        return None


def _check_spend_ledger(store: Path, keys_status: Any = lambda: None) -> CheckResult:
    name = "spend ledger"
    path = store / "keys" / "ledger.jsonl"
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return CheckResult(name, True, "not created yet (keys opens it when it first starts)")
    except PermissionError:
        told = keys_status()
        if told is None:
            return CheckResult(name, True, "held by keys: its size is read once a session has started, and keys gave "
                               "none to this run (with no session yet, or with keys not answering)", neutral=True)
        if told["ledger_bytes"] is None:
            return CheckResult(name, True, "keys could not tell its size (it has no ledger open, or cannot stat it)",
                               neutral=True)
        return CheckResult(name, True, f"{told['ledger_bytes']:,} bytes (one line per call), as keys reports it")
    except OSError as exc:
        return CheckResult(name, True, f"could not be read ({exc})", warn=True)
    if not stat.S_ISREG(st.st_mode):
        return CheckResult(name, True, "is not a regular file; keys will not append to it", warn=True)
    return CheckResult(name, True, f"{st.st_size:,} bytes (one line per call)")


def _check_keys_service(host: str, running: Any, egress: int | None) -> CheckResult:
    name = "keys service"
    if egress == 0:
        return CheckResult(name, True, "no egress slot, so keys waits for one (idle on macOS, stopped on Linux "
                                       "after exit status 78); this is not a failure", neutral=True)
    up = running(host, "keys")
    if up is True:
        return CheckResult(name, True, "running")
    if up is False and egress is None:
        return CheckResult(name, True, "not running, and the egress slots could not be read to tell whether "
                           "it should be", warn=True)
    if up is False:
        return CheckResult(name, False, "not running: no model call can be brokered",
                           "Run `sudo levain setup-isolation` (a repair run starts it).")
    return CheckResult(name, True, "could not be checked", warn=True)


def _egress_in_service(store: Path) -> int | None:
    """Egress slots not out of service, or ``None`` when the slots cannot be read."""
    try:
        found, _ignored = _read_slot_dirs(store / "slots")
    except OSError:
        return None
    from levain.slot import records as R
    return sum(1 for tag, v in found.items() if tag[0] == "e" and v[0] not in R.OUT_OF_SERVICE)


def _base_pairs(store: Path, shadow: frozenset = frozenset()) -> tuple[set, str | None]:
    """``({(upstream host, credential name)}, problem)`` of the brokered routes in ``L/routes/base.json``, read by
    keys' own reader (``routemaps.read_base_map``), leaving out each route whose id is in ``shadow`` (a dispatch id:
    keys refuses that base route at use). ``problem`` is a sentence when the map cannot be read; an absent map is no
    routes."""
    from levain.broker.policy import PolicyError
    from levain.contracts import ContractError
    from levain.slot import routemaps

    try:
        _key, routes = routemaps.read_base_map(str(store))
    except FileNotFoundError:
        return set(), None
    except OSError as exc:
        return set(), f"base.json could not be read ({_slot_why(exc)}); keys refuses it"
    except PolicyError as exc:
        return set(), f"base.json is refused by keys ({exc})"
    except (ContractError, ValueError, RecursionError) as exc:
        return set(), f"base.json does not parse as keys parses it ({type(exc).__name__}); keys refuses it"
    return _brokered_pairs(p for rid, p in routes.items() if rid not in shadow), None


def _brokered_pairs(policies: Any) -> set:
    return {(p.upstream.host, p.credential) for p in policies
            if p.type == "brokered" and p.upstream is not None and p.upstream.host and p.credential}


def _dispatch_pairs(store: Path, deadline: float, keys_status: Any = lambda: None) -> tuple[set, CheckResult | None]:
    """The brokered ``(host, credential)`` pairs of the dispatch routes keys serves, and a row when they cannot be
    told (:func:`_served_pairs` with its third part dropped)."""
    pairs, row, _shadow = _served_pairs(store, deadline, keys_status)
    return pairs, row


def _served_pairs(store: Path, deadline: float, keys_status: Any) -> tuple[set, CheckResult | None, frozenset | None]:
    """``(pairs, row, shadow)``: the brokered ``(host, credential)`` pairs of the dispatch routes keys serves, a row
    when they cannot be told, and which base routes keys refuses at use because their id is a dispatch id
    (``_route_policy``'s collision rule): ``shadow`` is those ids, or ``None`` when the base map adds no pair, because
    ``pairs`` is already keys' own WHOLE served set (its STATUS, base map included: L3 r1 K2) or because keys
    refuses every base route (the dispatch ids are unknown). Read as keys starts (``KeysRoutes.load``, inside the run's ``deadline``); a load ``problem`` means keys
    refuses every dispatch route, so it is the warning and no pairs. Otherwise a route counts in each generation where
    keys' own pin rule (``KeysRoutes.policy``) would serve it, so a route the head generation dropped does not count.
    keys' head sits in keys' own 0700 directory, so the operator's account often cannot read it, which is said as such
    (neutral) unless keys' own report (``keys_status``, CONTROL's ``KEYS-STATUS``) gives the pairs it serves. Any
    failure of the read is this row's warning, never the loss of the base map's rows."""
    from levain import trust_anchor
    from levain.slot import Refused
    from levain.slot import routemaps

    name = "invoice ceiling (dispatch routes)"
    head = store / "keys" / "routemap.head"
    try:
        os.lstat(head)
    except FileNotFoundError:
        return set(), None, frozenset()
    except PermissionError:
        told = keys_status()
        if told is not None:
            pairs = {(h, c) for h, c in told["routes"]}
            said = []
            if told["routes_problem"] is not None:
                said.append(f"keys reports: {told['routes_problem']}")
            if told["routes_truncated"]:
                said.append(f"keys' answer had room for {len(pairs)} routes and left {told['routes_truncated']} more "
                            "out, so their ceilings are not shown")
            if said:
                return pairs, CheckResult(name, True, "; ".join(said), warn=True), None
            return pairs, None, None
        return set(), CheckResult(name, True, "held by keys: this account's own read of the route maps was refused "
                                  "and keys gave none to this run, so the ceilings of their credentials are not "
                                  "shown (keys gives them once a session has started)",
                                  "Run `sudo levain doctor` to read them as root.", neutral=True), frozenset()
    except OSError as exc:
        return set(), CheckResult(name, True, f"dispatch route maps could not be read ({_slot_why(exc)})",
                                  warn=True), frozenset()
    problem, _keygen = trust_anchor.trust_anchor_problem(trust_anchor.DEFAULT_SSH_KEYGEN, what="ssh-keygen")
    if problem is not None:
        return set(), CheckResult(name, True, f"the dispatch route maps cannot be verified ({problem}); keys refuses "
                                  "every dispatch route while ssh-keygen cannot run", warn=True), frozenset()
    kr = routemaps.KeysRoutes(str(store))
    try:
        kr_head, maps, problem = _by_deadline(kr.load, deadline, "the dispatch route map read")
    except TimeoutError:
        return set(), CheckResult(name, True, "the dispatch route maps were not read: the time allowed for the reads "
                                  "in this run was used up", warn=True), frozenset()
    except Exception as exc:  # noqa: BLE001 - this row's failure, never the base map's rows
        return set(), CheckResult(name, True, f"the dispatch route maps could not be read ({type(exc).__name__}: "
                                  f"{exc})", warn=True), frozenset()
    if problem is not None:
        return set(), CheckResult(name, True, f"keys refuses every dispatch route ({problem}), so no dispatch "
                                  "credential is in use", warn=True), frozenset()
    pairs: set = set()
    try:
        kr.install(kr_head, maps, problem)
        for gen, m in maps.items():
            for rid in m.routes:
                try:
                    served = kr.policy(rid, routemaps.Pin(gen, m.digests[rid]))
                except Refused:
                    continue
                pairs |= _brokered_pairs([served])
        shadow = kr.route_ids()
    except Exception as exc:  # noqa: BLE001 - this row's failure, never the base map's rows
        return set(), CheckResult(name, True, f"the dispatch route maps could not be read ({type(exc).__name__}: "
                                  f"{exc})", warn=True), frozenset()
    return pairs, None, shadow


def _usd(v: float) -> str:
    return ("-" if v < 0 else "") + f"${abs(v):,.2f}"


_RESET_PERIOD = {"daily": "day", "weekly": "week", "monthly": "month"}


#: keys' limit-read failure tokens (``levain.broker.keys.LIMIT_FAILURES``), each as the row says it
_LIMIT_FAILURE_WORDS = {
    "no-credential": "keys has no usable credential file for it",
    "host-mismatch": "the credential is not bound to openrouter.ai",
    "expired": "the credential's access token has expired",
    "bad-credential": "the credential's header cannot be sent",
    "address-refused": "openrouter.ai resolved to an address keys refuses",
    "unreachable": "OpenRouter could not be reached",
    "timeout": "OpenRouter did not answer within keys' deadline",
    "http-2xx": "OpenRouter answered with a status other than 200",
    "http-3xx": "OpenRouter answered with a redirect, which keys does not follow",
    "http-4xx": "OpenRouter refused the request (a 4xx status)",
    "http-5xx": "OpenRouter failed the request (a 5xx status)",
    "http-other": "OpenRouter answered with an unexpected status",
    "bad-body": "OpenRouter answered with something that was not a key status",
    "bad-value": "OpenRouter's key status had a value keys does not carry",
    "internal": "keys hit an internal error reading it (keys' log names it)",
}


def _limit_failure_words(token: str) -> str:
    return _LIMIT_FAILURE_WORDS.get(token, "keys reported a failure this doctor does not know")


def _limit_age_words(age_s: int) -> str:
    if age_s < 60:
        return "less than a minute ago"
    if age_s < 2 * 3600:
        return f"{age_s // 60} min ago"
    return f"{age_s // 3600} h ago"


_LIMIT_HINT_SET = ("Set a per-key credit limit with no reset period, counting BYOK usage, at "
                   "openrouter.ai/settings/keys.")


def _unusable_numbers(name: str, said: str = "") -> CheckResult:
    return CheckResult(name, True, "OpenRouter's key status had no usable numbers; the key limit was not read" + said,
                       warn=True)


def _limit_verdict(name: str, lim: float | None, rem: float | None, reset: object, byok: object,
                   said: str = "") -> CheckResult:
    """THE interpretation of an OpenRouter key status (US dollars), shared by the root read and keys' report so both
    give the same verdict: no limit, unusable numbers, exhausted, a per-period cap, BYOK not counted, or a total
    limit. ``said`` (keys' "as keys read it ..." words) ends every text."""
    if lim is None:
        return CheckResult(name, True, "no credit limit is set on this key: nothing at the provider bounds the "
                           "invoice" + said, _LIMIT_HINT_SET, warn=True)
    if rem is None:
        return _unusable_numbers(name, said)
    problems = []
    if rem <= 0:
        problems.append("the key is exhausted: OpenRouter will refuse its calls")
    if reset is not None:
        period = _RESET_PERIOD.get(reset, reset) if isinstance(reset, str) else "period"
        problems.append(f"cap {_usd(lim)} per {period}, not a total: spend can pass it across periods")
    if byok is False:
        problems.append("BYOK usage is not counted against this limit")
    if problems:
        return CheckResult(name, True, f"key limit {_usd(lim)}, {_usd(rem)} remaining; " + "; ".join(problems) + said,
                           _LIMIT_HINT_SET, warn=True)
    return CheckResult(name, True, f"key limit {_usd(lim)} total, {_usd(rem)} remaining" + said)


def _keys_limit_row(cred: str, name: str, held: tuple, keys_status: Any) -> CheckResult:
    """The ceiling row from keys' own report (D1: keys' refresher reads each served OpenRouter key's limit and
    ``KEYS-STATUS`` carries its last read, in microdollars, with its age), for an operator whose account cannot open
    the credential; the held words when keys gave no report, and its own words when keys answered without this credential."""
    told = keys_status()
    entry = None if told is None else next((e for e in told["limits"] if e["credential"] == cred), None)
    if entry is None:
        if told is None:
            return CheckResult(name, True, held[0], held[1], warn=True)
        # keys answered: what it left out is said as what it left out, never as keys not answering
        said = (f"the key limit was not read: keys answered and reports no limit for {cred}"
                + ("; keys' answer had no room for it" if told["limits_truncated"] else ""))
        return CheckResult(name, True, said, held[1], warn=True)
    failed = entry["failed"]
    if entry["age_s"] is None:
        if failed is None:
            return CheckResult(name, True, "keys has not read this key's limit yet (it reads at start, then every "
                               "few minutes)", held[1], warn=True)
        return CheckResult(name, True, "the key limit was not read: keys' read failed "
                           f"({_limit_failure_words(failed)})", held[1], warn=True)
    said = f" (as keys read it {_limit_age_words(entry['age_s'])}"
    said += f"; its latest read failed: {_limit_failure_words(failed)})" if failed is not None else ")"
    lim, rem = entry["limit_microdollars"], entry["remaining_microdollars"]
    return _limit_verdict(name, None if lim is None else lim / 1_000_000, None if rem is None else rem / 1_000_000,
                          entry["reset"], entry["byok"], said)


def _openrouter_limit_row(store: Path, cred: str, name: str, fetch: Any, timeout: float,
                          keys_status: Any = lambda: None) -> CheckResult:
    """The ceiling row for one OpenRouter credential: its key status read with that credential, or, when the
    operator's account cannot open it, keys' own last read of it (``keys_status``), or why not. Never hangs and every
    outcome is its own row text. GET /api/v1/key answers ``data`` with (OpenRouter's API reference, "Get current API
    key") ``limit`` (USD, nullable), ``limit_remaining`` (nullable), ``limit_reset`` (nullable string: the period the
    limit resets over) and ``include_byok_in_limit`` (boolean)."""
    from levain.broker import creds, policy

    held = (f"the key limit was not read: keys holds {cred}; its limit is read once a session has started, and keys "
            "gave none to this run (with no session yet, or with keys not answering: see the keys service row)",
            "Set a per-key credit limit at openrouter.ai/settings/keys; Levain's authorized cap does not bound "
            "the provider's invoice by itself.")
    try:
        dir_fd = os.open(store / "keys" / "cred", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except PermissionError:
        return _keys_limit_row(cred, name, held, keys_status)
    except FileNotFoundError:
        return CheckResult(name, True, f"no credential file for {cred} (the credential directory does not exist); "
                           "keys refuses calls on this route until it is set; the key limit was not read", warn=True)
    except OSError as exc:
        return CheckResult(name, True, f"the credential directory could not be read ({_slot_why(exc)})", warn=True)
    try:
        raw = creds.read_file(dir_fd, cred)
    except PermissionError:
        return _keys_limit_row(cred, name, held, keys_status)
    except creds.Unparseable as exc:
        return CheckResult(name, True, f"the credential file for {cred} is not usable ({exc}); the key limit was "
                           "not read", warn=True)
    except OSError as exc:
        return CheckResult(name, True, f"the credential file for {cred} could not be read ({_slot_why(exc)}); "
                           "the key limit was not read", warn=True)
    finally:
        os.close(dir_fd)
    if raw is None:
        return CheckResult(name, True, f"no credential file for {cred}; keys refuses calls on this route until it "
                           "is set; the key limit was not read", warn=True)
    try:
        c = creds.parse(raw, cred)
    except creds.Unparseable as exc:
        return CheckResult(name, True, f"the credential file does not parse ({exc}); the limit was not read",
                           warn=True)
    if policy.OPENROUTER_HOST not in c.hosts:
        return CheckResult(name, True, f"{cred} is not bound to {policy.OPENROUTER_HOST}, so it is not sent there; "
                           "the key limit was not read", warn=True)
    hname, hvalue = c.header_pair()
    try:
        status, body = (fetch or _fetch_key_limit)(OPENROUTER_KEY_URL, {hname.decode(): hvalue.decode("latin-1")},
                                                   timeout)
    except Exception as exc:  # noqa: BLE001 - any transport failure is a row, never a doctor failure
        return CheckResult(name, True, f"OpenRouter was unreachable ({type(exc).__name__}); the key limit was not read",
                           warn=True)
    if status != 200:
        return CheckResult(name, True, f"OpenRouter answered {status}; the key limit was not read", warn=True)
    from levain.broker import keys

    got = keys.key_status_values(body)   # keys' own validation, so both paths give one verdict (L3 r1 E3)
    if got == "bad-body":
        return CheckResult(name, True, "OpenRouter answered with something that was not a key status; "
                                       "the key limit was not read", warn=True)
    if isinstance(got, str):
        return _unusable_numbers(name)
    lim, rem, reset, byok = got
    return _limit_verdict(name, None if lim is None else lim / 1_000_000, None if rem is None else rem / 1_000_000,
                          reset, byok)


def _check_invoice_ceiling(store: Path, fetch: Any, keys_status: Any = lambda: None) -> list[CheckResult]:
    """The provider-side limit on each brokered credential in the routes keys serves (the base map and the dispatch
    route maps; design §2.2, the invoice ceiling): OpenRouter's is read within one total deadline; Anthropic's and
    OpenAI's are operator settings Levain cannot read, said as that; any other provider's is said to be unknown."""
    from levain.broker import policy

    deadline = _clock() + _LIMIT_TOTAL_S
    rows: list[CheckResult] = []
    more, dispatch_row, shadow = _served_pairs(store, deadline, keys_status)
    pairs, problem = (set(), None) if shadow is None else _base_pairs(store, shadow)
    if problem is not None:
        rows.append(CheckResult("invoice ceiling", True, problem, warn=True))
    pairs |= more
    if dispatch_row is not None:
        rows.append(dispatch_row)
    uses: dict = {}
    for host, cred in pairs:
        uses[cred] = uses.get(cred, 0) + 1
    if pairs and not _cred_dir_exists(store):
        rows.append(CheckResult("invoice ceiling (credentials)", True, "no credential is set for any route (the "
                                "credential directory does not exist), so keys refuses every brokered call; no key "
                                "limit was read", warn=True))
        pairs = {(h, c) for h, c in pairs if h != policy.OPENROUTER_HOST}
    for host, cred in sorted(pairs):
        name = f"invoice ceiling ({cred})" if uses[cred] == 1 else f"invoice ceiling ({cred} at {host})"
        rows.extend(_guarded(name, lambda host=host, cred=cred, name=name: _ceiling_row(
            store, host, cred, name, fetch, deadline, keys_status)))
    return rows


def _cred_dir_exists(store: Path) -> bool:
    """False only when keys' credential directory is known to be absent (an unreadable parent is not absence)."""
    try:
        os.lstat(store / "keys" / "cred")
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _ceiling_row(store: Path, host: str, cred: str, name: str, fetch: Any, deadline: float,
                 keys_status: Any = lambda: None) -> CheckResult:
    from levain.broker import policy

    if host == policy.OPENROUTER_HOST:
        left = deadline - _clock()
        if left <= 0:
            return CheckResult(name, True, "the key limit was not read: the time allowed for the provider reads "
                               "in this run was used up", warn=True)
        return _openrouter_limit_row(store, cred, name, fetch, min(_LIMIT_FETCH_TIMEOUT_S, left), keys_status)
    if host in _PROVIDER_LIMIT_SETTINGS:
        return CheckResult(name, True, f"not readable; set a {_PROVIDER_LIMIT_SETTINGS[host]} in the provider "
                           "console, since Levain's authorized cap does not bound the provider's invoice",
                           neutral=True)
    return CheckResult(name, True, f"the provider-side ceiling for {host} is unknown: Levain cannot read it, and its "
                       "invoice is not bounded by Levain's authorized cap", "Set a spend limit in that provider's "
                       "console.", warn=True)


def _guarded(name: str, build: Any) -> list[CheckResult]:
    """``build()``'s row(s); a builder that raises is its OWN failure row, so one defect never discards the rows built
    beside it nor turns into a pass."""
    try:
        out = build()
    except Exception as exc:  # noqa: BLE001 - this is the isolation
        return [CheckResult(name, False, f"could not be built ({type(exc).__name__}: {exc})",
                            "This is a defect in `levain doctor`; report it.")]
    return out if isinstance(out, list) else [out]


def _check_slot_keys(*, host: str | None = None, store: Path | None = None, running: Any = None,
                     fetch: Any = None, keys_status: Any = None) -> list[CheckResult]:
    """The 3c-9 rows over the brokered-spend install: ``route trust``, ``keys service``, ``spend ledger`` and one
    ``invoice ceiling`` row per brokered credential. Read-only and read as the OPERATOR, so a fact in keys' own
    0700 directory is said as held by keys, never as healthy or failed. No rows before the isolation install. Each
    row is built on its own (:func:`_guarded`). A row that meets keys' directory asks keys through CONTROL instead
    (``keys_status``; the default asks the installed CONTROL, and with a test's ``store`` it asks nothing).
    ``running``/``fetch``: a test's seams."""
    from levain.slot import layout

    if host is None:
        host = "darwin" if sys.platform == "darwin" else "linux"
    if keys_status is None:
        run = layout.RUN.get(host)
        keys_status = (_keys_status_asker(run, _KEYS_STATUS_S) if store is None and run is not None
                       else (lambda: None))
    store = store if store is not None else layout.STORE.get(host)
    if store is None or not (store / "policy").is_dir():
        return []
    rows = _guarded("route trust", lambda: _check_route_trust(store))
    rows += _guarded("keys service", lambda: _check_keys_service(host, running or _unit_running,
                                                                 _egress_in_service(store)))
    rows += _guarded("spend ledger", lambda: _check_spend_ledger(store, keys_status))
    rows += _guarded("invoice ceiling", lambda: _check_invoice_ceiling(store, fetch, keys_status))
    return rows


#: DECISION (S1 6b): a heartbeat whose pass ended longer ago than this is a reaper with no pass for that long; ten
#: minutes is well past the pass worker's tick (``levain.slot.reaper.V11_S``), room for a long pass. A pass that old
#: is a WARN while the unit runs (one END may legally take this long), and no longer evidence of anything else.
_REAPER_STALE_S = 600
_REAPER_SKEW_S = 300           # DECISION: an `ended_at` this far ahead of the clock is not read as fresh
_REAPER_BEAT_KEYS = frozenset({"boot_id", "pass_seq", "started_at", "ended_at", "control", "held", "skipped"})
_CONTROL_ASK_S = 3.0           # DECISION: one deadline for CONTROL's STATUS answer
_HELD_NAMED = 5                # DECISION: held sessions named in a row before "and N more"
_SKIP_SHOWN = 3                # DECISION: failed reaper steps named in a row before "and N more"
_AS_ROOT = "as root"           # the `why` of a STATUS that was not asked: CONTROL does not serve root this verb
_FAILED_STEP = re.compile(r"step\d+[a-z]*:")


def _read_beat(run: Path) -> tuple[str, Any]:
    """(kind, value) for the reaper's heartbeat (``R/reaper/pass.json``, written by ``Reaper.write_heartbeat``): ``ok``
    with the parsed record; ``absent``; ``closed`` when this account cannot open the directory (it is ``root`` and
    group ``levain-wake``, 0750, so an operator outside that group cannot); ``bad`` with why. A fact that cannot be
    read is never read as a healthy one."""
    from levain.contracts import ContractError, canon
    from levain.slot import Refused
    from levain.slot import records as R
    from levain.slot.grammar import SESSION_RE
    from levain.slot.reaper import HEARTBEAT

    path = run / "reaper" / HEARTBEAT
    try:
        raw = R.read_record(path, max_bytes=_SLOT_RECORD_CAP)
    except PermissionError:
        return "closed", None
    except (Refused, OSError) as exc:
        return "bad", _slot_why(exc)
    if raw is None:
        return "absent", None
    try:
        rec = canon.parse(raw)
        if (type(rec) is not dict or not {"pass_seq", "ended_at", "control", "held", "skipped"} <= set(rec)
                or not set(rec) <= _REAPER_BEAT_KEYS or type(rec["pass_seq"]) is not int
                or rec["control"] not in ("up", "down") or type(rec["ended_at"]) is not str
                or type(rec["held"]) is not list or type(rec["skipped"]) is not list
                or not all(type(s) is str and SESSION_RE.fullmatch(s) for s in rec["held"])
                or not all(type(s) is str for s in rec["skipped"])
                or ("boot_id" in rec and type(rec["boot_id"]) is not str)
                or ("started_at" in rec and type(rec["started_at"]) is not str)):
            raise ValueError("shape")
        R.epoch_of(rec["ended_at"])
        if "started_at" in rec:
            R.epoch_of(rec["started_at"])
    except (ContractError, ValueError):
        return "bad", "not a reaper pass record"
    return "ok", rec


def _beat_age(rec: dict, now: float) -> float:
    from levain.slot import records as R

    return now - R.epoch_of(rec["ended_at"])


def _fresh_beat(beat: tuple[str, Any], now: float) -> dict | None:
    """The pass record when it can speak for NOW (ended no more than ``_REAPER_STALE_S`` ago, nor stamped ahead of the
    clock by more than ``_REAPER_SKEW_S``); ``None`` otherwise. A stale or future record is history."""
    if beat[0] != "ok":
        return None
    age = _beat_age(beat[1], now)
    return beat[1] if -_REAPER_SKEW_S <= age <= _REAPER_STALE_S else None


def _failed_steps(rec: dict) -> list[str]:
    """The pass's own failures: a whole step that raised (``step<N>:<Error>``). The per-item ``skipped`` names (a slot
    or session busy this pass) are the reaper's normal deferrals, and ``:seam-unbuilt`` names a step the reaper does
    not carry yet; neither is a fault."""
    return [s for s in rec["skipped"] if _FAILED_STEP.match(s) and not s.endswith(":seam-unbuilt")]


def _check_reaper(host: str, beat: tuple[str, Any], running: Any, now: float,
                  status: tuple[bool, Any] = (False, "not asked")) -> CheckResult:
    from levain import unshowable
    from levain.slot.reaper import V11_S

    name = "agent reaper"
    up = running(host, "reaper")
    if up is not None:
        up = bool(up)
    if up is False:
        return CheckResult(name, False, "not running: ended sessions are not cleaned up, held ones are not retried, "
                           "and the space brake does not run",
                           "Run `sudo levain setup-isolation` (a repair run starts it).")
    kind, rec = beat
    lead = "running" if up is True else "could not be checked"
    soft = up is not True
    if kind == "closed":
        return CheckResult(name, True, f"{lead}; its pass record is readable only by root and the levain-wake group, "
                           "so this account cannot see its last pass", neutral=not soft, warn=soft)
    if kind == "absent":
        return CheckResult(name, True, f"{lead}, and has written no pass record yet", warn=True,
                           hint=f"It passes about every {int(V11_S)} s; if this stays, look at its service log.")
    if kind == "bad":
        return CheckResult(name, True, f"{lead}; its pass record cannot be read ({rec})", warn=True)
    age = _beat_age(rec, now)
    if age < -_REAPER_SKEW_S:
        return CheckResult(name, True, f"{lead}; its last pass is stamped in the future ({rec['ended_at']})", warn=True)
    if age > _REAPER_STALE_S:
        return CheckResult(name, True, f"{lead}, but no pass in {_age_text(age)} (pass {rec['pass_seq']}); one long END "
                           f"can take that long, and it otherwise passes about every {int(V11_S)} s", warn=True,
                           hint="If this stays, look at the reaper's service log; a repair run restarts it: "
                           "`sudo levain setup-isolation`.")
    failed = _failed_steps(rec)
    detail = f"{lead}; pass {rec['pass_seq']} ended {_age_text(age)} ago"
    if rec["control"] == "down" and status[0]:
        return CheckResult(name, True, f"{detail}; that pass could not reach CONTROL, which answers this account now",
                           warn=True, hint="Likely a restart of CONTROL between its passes; if the next pass says it "
                           "again, restart both with `sudo levain setup-isolation`.")
    if failed:
        shown = ", ".join(unshowable.mark(s[:64], allow="") for s in failed[:_SKIP_SHOWN])
        more = f" and {len(failed) - _SKIP_SHOWN} more" if len(failed) > _SKIP_SHOWN else ""
        return CheckResult(name, True, f"{detail}; a step failed in that pass: {shown}{more}", warn=True,
                           hint="The pass goes on past a failed step; the reaper's service log names the fault.")
    return CheckResult(name, True, detail, warn=soft)


def _ask_control_status(run: Path, deadline: float) -> tuple[bool, Any]:
    """(True, records) when CONTROL answers the operator's ``STATUS`` (the operator's own session records, read-only,
    the verb ``PEER_VERBS['operator']`` carries); (True, name) when CONTROL answered with a refusal (it is alive; the
    name is the refusal's); (False, why) when it did not answer. CONTROL's own frame codec is used, and ``deadline``
    bounds the whole exchange. Nothing here raises."""
    import socket

    try:
        from levain.slot import control

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as raw:
            s = _ByDeadline(raw, deadline)
            s.connect(str(run / "control" / "control.sock"))
            s.sendall(control.encode_frame({"v": 1, "op": "STATUS", "id": 1}))
            ans = control.read_frame(s)  # type: ignore[arg-type]
    except FileNotFoundError:
        return False, "no control socket"
    except PermissionError:
        return False, "this account may not open the control socket"
    except TimeoutError:
        return False, "no answer in time"
    except Exception as exc:  # noqa: BLE001 - any other failure is "did not answer"
        return False, f"{type(exc).__name__}"
    if type(ans) is not dict or ans.get("id") != 1 or type(ans.get("ok")) is not bool:
        return False, "closed without an answer" if ans is None else "an answer off its shape"
    if ans["ok"] is not True:
        from levain import unshowable

        why = ans.get("refused")
        return True, unshowable.mark(why[:40], allow="") if type(why) is str and why else "an unnamed refusal"
    recs = ans.get("records")
    if type(recs) is not list or not all(type(r) is dict for r in recs):
        return False, "an answer off its shape"
    return True, recs


def _check_control(host: str, running: Any, status: tuple[bool, Any]) -> CheckResult:
    name = "agent control"
    up = running(host, "control")
    if up is not None:
        up = bool(up)
    if up is False:
        return CheckResult(name, False, "not running: no session can start, and ending ones wait",
                           "Run `sudo levain setup-isolation` (a repair run starts it).")
    answered, why = status
    if not answered and why is _AS_ROOT:
        return CheckResult(name, True, f"{'running' if up else 'unit state unknown'}; run as root, so CONTROL's answer "
                           "was not asked (it serves STATUS to the operator)", "Run `levain doctor` as the operator "
                           "to see CONTROL answer.", neutral=up is True, warn=up is None)
    if not answered:
        return CheckResult(name, False, f"{'running' if up else 'unit state unknown'}, but does not answer ({why})",
                           "Run `sudo levain setup-isolation`; if it stays, look at CONTROL's service log.")
    return CheckResult(name, True, "answers" if up is True else "answers (its unit state could not be read)",
                       warn=up is None)


def _check_held(status: tuple[bool, Any], beat: tuple[str, Any], now: float) -> CheckResult:
    from levain import unshowable
    from levain.slot import records as R
    from levain.slot.grammar import SESSION_RE

    name = "agent held sessions"
    held: dict[str, str] = {}
    listed = status[0] and type(status[1]) is list
    shown_by_control: set[str] = set()
    if listed:
        for rec in status[1]:
            sess = rec.get("session")
            if type(sess) is not str or not SESSION_RE.fullmatch(sess):
                continue
            shown_by_control.add(sess)
            if rec.get("state") == R.HELD:
                step = rec.get("failed_step")
                tries = rec.get("held_attempts")
                held[sess] = (f"{unshowable.mark(step[:32], allow='')}" if type(step) is str else "?") + (
                    f" ({tries} tries)" if type(tries) is int else "")
    fresh = _fresh_beat(beat, now)
    if fresh is not None:
        for s in fresh["held"]:
            if s not in shown_by_control:       # CONTROL's word on a session it reports is the live one
                held.setdefault(s, "")
    if not held:
        if status[0] and not listed:
            return CheckResult(name, True, f"could not list: CONTROL refused ({status[1]})", warn=True)
        if not status[0]:
            if status[1] is _AS_ROOT:
                return CheckResult(name, True, "not asked: run as root, CONTROL does not serve this account its "
                                   "sessions", "Run `levain doctor` as the operator to see them.", neutral=True)
            seen = {"closed": "closed to this account", "absent": "not written yet", "bad": "unreadable"}.get(beat[0])
            if seen:
                state = seen
            elif fresh is not None:     # current, but the reaper skips end_and_scrub while CONTROL is down: empty proves nothing
                state = "current but names no held session, which proves nothing while CONTROL is down"
            else:
                state = "out of date"
            why = f"CONTROL did not answer ({status[1]}) and the reaper's pass record is {state}"
            return CheckResult(name, True, f"unknown: {why}; a session stuck mid-end would not show here", warn=True)
        if beat[0] == "closed":
            return CheckResult(name, True, "none of your sessions (CONTROL shows only yours)")
        return CheckResult(name, True, "none")
    shown = [f"{s} at {w}" if w else s for s, w in sorted(held.items())[:_HELD_NAMED]]
    more = f" and {len(held) - _HELD_NAMED} more" if len(held) > _HELD_NAMED else ""
    return CheckResult(name, False, f"{len(held)} stuck mid-end: {'; '.join(shown)}{more}; each keeps its slot leased "
                       "and its tree untouched while the reaper retries",
                       "The reaper retries with a backoff; if the named step keeps failing, its service log says why. "
                       "If `repair` refuses `live-lease` for this session (it was stranded by an older release's END), "
                       "release it from this install's package, in order:\n" + _held_commands())


def reap_held_lines(host: HostOS, source: str) -> tuple[str, ...]:
    """The commands that release a session stuck HELD by an OLDER release's END, run as the operator in this order
    (Phill 10-28, np-5da552a2 (b): no ``reap-held`` verb; an installed helper cannot learn a verb it was built
    without, so ``repair`` stays the only door and refuses ``live-lease`` while the session is stuck). It stops the
    reaper, copies the NEW package (``source``) to a root-owned directory, runs ONE reaper pass from that copy,
    never from the operator-writable tree, starts the reaper again and removes the copy. ``repair`` follows."""
    from levain.slot import layout, root_tree, units

    svc = next(sv for sv in units.SERVICES if sv.name == "reaper")
    stage = "/var/root/levain-pass" if host == "darwin" else "/root/levain-pass"
    if host == "darwin":
        stop = f"sudo launchctl bootout system/{units.launchd_label(svc)}"
        start = f"sudo launchctl bootstrap system {units.launchd_path(svc)}"
    else:
        unit = units.systemd_name(svc)
        stop, start = f"sudo systemctl stop {unit}", f"sudo systemctl start {unit}"
    prog = ("import sys; sys.path.insert(0, '" + stage + "'); "
            "from levain.slot import reaper as RP, runtime_helper as H; print(RP.Reaper(H.Seams()).run_pass())")
    return (stop,
            f"sudo rm -rf {stage} && sudo mkdir -m 700 {stage} && sudo cp -R {shlex.quote(source)} {stage}/levain",
            f"sudo {root_tree.ROOT_PYTHON[host]} {root_tree.PY_FLAGS} -c \"{prog}\"",
            start,
            f"sudo rm -rf {stage}",
            layout.repair_line(host) + f" --source {shlex.quote(source)}")


def _held_commands() -> str:
    """The numbered commands of :func:`reap_held_lines`, for the package this doctor runs from."""
    from levain.slot import layout

    host = "darwin" if sys.platform == "darwin" else "linux"
    src = os.path.dirname(os.path.abspath(__file__))
    steps = "\n".join(f"        {i}. {c}" for i, c in enumerate(reap_held_lines(host, src), 1))
    return steps + ("\n        If 2 fails, stop and look at why: steps 3-5 need the copy. If 3 fails, still run 4 and 5."
                      "\n        If 4 fails, run it again before 5. Step 6 asks for your password.")


def _check_slot_services(*, host: str | None = None, store: Path | None = None, run: Path | None = None,
                         running: Any = None, status: Any = None, now: float | None = None,
                         uid: int | None = None) -> list[CheckResult]:
    """The 6b rows over the isolation install's two root-side services, read as the OPERATOR: ``agent reaper`` (its
    unit and its last pass record), ``agent control`` (its unit and an answered ``STATUS``) and ``agent held
    sessions`` (the operator's sessions CONTROL reports stuck in HELD, plus any a FRESH pass record names that
    CONTROL does not show). The pass record is root-and-``levain-wake``-only, so an account outside that group gets
    CONTROL's word alone and the reaper row says what it cannot see. Run as root, CONTROL is not asked (it serves
    ``STATUS`` to the operator). A fact doctor cannot establish now is a warning or neutral, never green. No rows
    before the isolation install (``agent sessions`` says that). Each row is built on its own (:func:`_guarded`).
    ``running``/``status``/``uid``: a test's seams (``status`` returns what :func:`_ask_control_status` does)."""
    from levain.slot import layout

    if host is None:
        host = "darwin" if sys.platform == "darwin" else "linux"
    store = store if store is not None else layout.STORE.get(host)
    run = run if run is not None else layout.RUN.get(host)
    if store is None or run is None or not (store / "policy").is_dir():
        return []
    when = time.time() if now is None else now
    up = running or _unit_running
    uid = os.getuid() if uid is None and hasattr(os, "getuid") else uid
    try:
        if uid == 0 and status is None:
            answer: tuple[bool, Any] = (False, _AS_ROOT)
        else:
            answer = (status if status is not None else lambda: _ask_control_status(run, _clock() + _CONTROL_ASK_S))()
        if type(answer) is not tuple or len(answer) != 2 or type(answer[0]) is not bool:
            raise ValueError("shape")
    except Exception as exc:  # noqa: BLE001 - a STATUS that raises is "did not answer", the rows below still build
        answer = (False, type(exc).__name__)
    try:
        beat = _read_beat(run)
    except Exception:  # noqa: BLE001 - a heartbeat read that raises is a row below, not a lost doctor
        beat = ("bad", "could not be read")
    rows = _guarded("agent reaper", lambda: _check_reaper(host, beat, up, when, answer))
    rows += _guarded("agent control", lambda: _check_control(host, up, answer))
    rows += _guarded("agent held sessions", lambda: _check_held(answer, beat, when))
    return rows


def _check_host_awake() -> list[CheckResult]:
    """Why this host will or will not sleep (C7): whether the idle-sleep assertion is held for a working work thread.
    Advisory: no row until a coordinator store exists (read only, never created here), a warn row when one exists and
    cannot be read; never a failure, because sleeping with nothing working is the machine's own setting."""
    from levain.install import _consent_queue_dir

    name = "host sleep"
    qdir = _consent_queue_dir()
    try:
        from levain.coordinator import awake
        from levain.coordinator.door import readonly_store

        store = readonly_store(qdir)          # a doctor reads: it never creates the store or its tables
        if store is None:
            return []
        n = len(awake.working(store))
        ok, line = awake.describe(qdir, working_count=n)
    except Exception as exc:      # noqa: BLE001 - a doctor row never crashes the run
        return [CheckResult(name, True, f"not read: {exc}", warn=True)]
    return [CheckResult(name, True, line, warn=not ok)]


def _check_dispatch_adoption_rules() -> list[CheckResult]:
    """The dispatch adoption rules (F11 (B)), read the one way a land reads them (re-judged against the active
    configuration): no row without a dispatch control store; a WARN naming the rule and the way out when they do not
    hold, the case a rule signed with Levain 0.11.10's free-text criteria meets after an upgrade. Advisory only: a
    rule that does not hold adopts nothing."""
    from levain.dispatch import DispatchError
    from levain.dispatch import control as dctl
    from levain.dispatch.registry import RESIGN_RULES, active_adoption_rules
    from levain.install import _consent_queue_dir

    name = "dispatch adoption rules"
    qdir = _consent_queue_dir()
    if not (qdir / dctl.DB_NAME).is_file():
        return []
    try:
        from levain.dispatch.cli import _authority_source

        rules = active_adoption_rules(dctl.ControlStore(qdir, authority=_authority_source(qdir, None)))
    except dctl.ControlStateUnavailable as exc:
        return [CheckResult(name, True, f"could not be read ({str(exc)[:300]})",
                            "`levain dispatch config show` says what the control store holds.", warn=True)]
    except DispatchError as exc:
        hint = None if RESIGN_RULES in str(exc) else ("Show the active configuration with `levain dispatch config "
                                                      f"show`; {RESIGN_RULES}.")
        return [CheckResult(name, True, f"{str(exc)[:600]} (nothing is adopted by rule until they hold)", hint,
                            warn=True)]
    except Exception as exc:  # noqa: BLE001 - a row that cannot be built is said, never a crash
        return [CheckResult(name, True, f"could not be read ({type(exc).__name__}: {str(exc)[:300]})",
                            "`levain dispatch config show` says what the control store holds.", warn=True)]
    return [CheckResult(name, True, f"{len(rules)} active rule(s), each with typed criteria")]


def _check_dispatch_verifiers() -> list[CheckResult]:
    """The active dispatch verifier item (CONTRACT §3.5): every entry's ``driver_template_digest`` against the
    template the installed levain ships, so an upgrade that changed a template is red BEFORE a run refuses it
    (re-sign the item). No row without a dispatch control store; a WARN when the store holds only Levain 0.11's
    ``verifiers`` item, which is no longer read."""
    from levain.dispatch import control as dctl
    from levain.dispatch.registry import (
        RESIGN_VERIFIERS, active_verifier_entries, driver_template_digest, legacy_verifiers_problem,
    )
    from levain.install import _consent_queue_dir

    name = "dispatch verifiers"
    qdir = _consent_queue_dir()
    if not (qdir / dctl.DB_NAME).is_file():
        return []
    try:
        from levain.dispatch.cli import _authority_source

        active = dctl.ControlStore(qdir, authority=_authority_source(qdir, None)).active()
        gen, entries = active_verifier_entries(active)
        legacy = legacy_verifiers_problem(active)
        stale, unreadable = [], []
        for e in entries:
            try:
                shipped = driver_template_digest(e["driver_template"])
            except OSError as exc:
                # an installed template that cannot be read: its row is red and names it, the doctor goes on
                unreadable.append(f"{e['verifier_id']} (driver {e['driver_template']}: the installed template "
                                  f"cannot be read, {type(exc).__name__})")
                continue
            if shipped != e["driver_template_digest"]:
                stale.append(f"{e['verifier_id']} (driver {e['driver_template']}: "
                             + ("not shipped by this levain)" if shipped is None
                                else "the installed template differs)"))
    except dctl.ControlStateUnavailable as exc:
        return [CheckResult(name, True, f"could not be read ({str(exc)[:300]})",
                            "`levain dispatch config show` says what the control store holds.", warn=True)]
    except Exception as exc:  # noqa: BLE001 - a row that cannot be built is said, never a crash
        return [CheckResult(name, True, f"could not be read ({type(exc).__name__}: {str(exc)[:300]})",
                            "`levain dispatch config show` says what the control store holds.", warn=True)]
    if not gen:
        if legacy is not None:
            return [CheckResult(name, True, f"{legacy[:600]} (no verifier runs until one is signed)", None,
                                warn=True)]
        return [CheckResult(name, True, "no verifier item is accepted")]
    if unreadable:
        return [CheckResult(name, False, f"generation {gen}: {', '.join(unreadable + stale)[:600]}; every run of "
                            "these verifiers is refused", "Reinstall levain so its driver templates are readable; "
                            f"if a template changed, {RESIGN_VERIFIERS}.")]
    if stale:
        return [CheckResult(name, False, f"generation {gen}: {', '.join(stale)[:600]}; every run of these verifiers "
                            "is refused", f"The installed levain changed a driver template; {RESIGN_VERIFIERS}.")]
    in_process = [e["verifier_id"] for e in entries if e["declared_kind"] == "in-process"]
    detail = f"generation {gen}: {len(entries)} verifier(s), every driver template matches the installed levain"
    if in_process:
        detail += f"; in-process, forgeable by the code under test: {', '.join(in_process)[:300]}"
    if legacy is not None:
        detail += "; the 0.11 verifiers item is no longer read"
    return [CheckResult(name, True, detail)]


def _check_seed_content(install: Path) -> list[CheckResult]:
    """Content sanity: a rendered base seed carries NO unfilled `{{SLOT}}`.
    Reuses the interview's code-aware slot scan, so a code-formatted `` `{{X}}` ``
    documentation reference is not mistaken for an unfilled slot."""
    from levain.interview import _unique_slots

    results: list[CheckResult] = []
    seed = install / "seed"
    for name in _RENDER_TARGET_SEEDS:
        f = seed / name
        if not f.is_file():
            continue  # a missing required seed is the layout check's job
        try:
            content = f.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as e:
            # A binary / invalid-UTF-8 seed is a corrupt install, not a crash —
            # `read_text` raises UnicodeDecodeError (a UnicodeError, NOT OSError).
            results.append(
                CheckResult(f"seed content ({name})", False, f"unreadable: {e}")
            )
            continue
        unfilled = _unique_slots(content)
        if unfilled:
            markers = ", ".join("{{" + s + "}}" for s in unfilled)
            results.append(
                CheckResult(
                    f"seed content ({name})",
                    False,
                    f"unfilled placeholder(s): {markers}",
                    "The interview did not fill this file — the operator's "
                    "identity was not captured. Re-run `levain init` and answer "
                    "every prompt.",
                )
            )
        else:
            results.append(
                CheckResult(f"seed content ({name})", True, "no unfilled placeholders")
            )
    return results


def _check_recorded_answers(install: Path) -> list[CheckResult]:
    """CONTENT, as distinct from WIRING — the check `doctor` did not have.

    The gap this closes, stated exactly: `_check_seed_content` above proves no
    `{{SLOT}}` SURVIVED, which proves the render RAN. It cannot see whether the
    render ran over the right values. A seed whose answers were shifted by one slot
    has no surviving placeholder, no missing file, no broken hook — every wiring
    check passes and `doctor` prints green over an entity that does not know who its
    operator is. That is the one failure class Levain's own pitch says cannot happen
    here, so it is the one `doctor` must be able to see.

    Three checks over the recorded interview (`.levain/answers.json`, the same map
    `levain update` re-renders from):

      1. THE RECORD EXISTS. Absent, it fails — it does not pass quietly. An install
         whose content cannot be verified is not an install verified as good, and
         rendering "unable to check" as a green tick is the precise habit this
         function exists to break.
      2. REQUIRED ANSWERS ARE NON-EMPTY. An empty required slot renders to nothing
         and leaves no placeholder behind, so it is invisible to every other check.
      3. THE SEED ON DISK STILL MATCHES THE RECORD — REPORTED, NOT FAILED.
         `render_template` substitutes values VERBATIM, so a recorded answer that is
         missing from its seed file means the seed was edited after init. That is an
         invited action, not a defect (see the note at the check itself), so it is
         surfaced as information about a future re-render, never as a failure.

    Plus `shape_violations`, which is where a slot-shift actually gets CAUGHT rather
    than merely being possible to catch.

    KNOWN, DELIBERATE LIMITS — none of these are bugs, and pretending otherwise
    would make this check a worse liar than the one it replaces:
      - A semantically WRONG answer that is well-formed is undetectable, here or
        anywhere. No mechanism can know whether "Chris" is your name.
      - Check 3 is a SUBSTRING match over the whole file, not a slot-anchored one,
        so a short or common value ("a", "CEO") can match incidentally elsewhere in
        the seed. It is reliable for the long, distinctive values (names, bios) and
        weak for terse ones — acceptable precisely because it only ever INFORMS.
      - Only slots in BOTH the base field plan and the record are checked. A `--pack`
        layer may add or replace render templates and no roster is recorded at
        doctor time (the same Slice-3 deferral `_SEED_REQUIRED` notes), so pack
        slots are passed over rather than guessed at — a false FAIL on a healthy
        packed install would teach operators to ignore this check, which costs more
        than the coverage gained.
    """
    from levain.answers import (
        SHAPE_IMPOSSIBLE,
        is_required,
        shape_violations,
    )
    from levain.install import _templates_root, read_answers

    answers = read_answers(install)
    if not answers:
        return [
            CheckResult(
                "seed content (recorded answers)",
                False,
                "no recorded interview at .levain/answers.json — seed CONTENT "
                "cannot be verified",
                "Wiring checks alone cannot tell a filled seed from a scrambled "
                "one. This means the install predates answer recording (levain "
                "0.3.13, 2026-07-07) — anything installed since records it "
                "automatically, so a NEW install never sees this. To fix an older "
                "one, re-run `levain init --force --path <install>` and answer the "
                "interview again: your anneal store, and everything the entity has "
                "learned, is preserved — only the seed is re-rendered.",
            )
        ]

    try:
        from levain.interview import build_field_plan, parse_template

        with _templates_root() as troot:
            specs = [
                parse_template(troot / "seed" / name)
                for name in _RENDER_TARGET_SEEDS
                if (troot / "seed" / name).is_file()
            ]
            # TWO lists, and keeping them apart IS the fix. `plan` is what this install's
            # interview would ASK; `fields` is what the record actually HOLDS. Narrowing to
            # `fields` before looking is what made a MISSING slot invisible — filtering by
            # `f.slot in answers` deletes the absent slot from the evidence, and the check
            # then asks whether any absent slot is blank. Nothing absent is ever blank.
            plan = build_field_plan(specs)
            fields = [f for f in plan if f.slot in answers]
    except Exception as e:  # noqa: BLE001 — an unreadable template must not crash doctor
        return [
            CheckResult(
                "seed content (recorded answers)",
                False,
                f"could not read the base interview templates to check content: {e}",
                "Reinstall the package: `pip install --force-reinstall levain`.",
            )
        ]

    if not fields:
        # A record that shares NOT ONE slot with the base seed is not a record of
        # this install's interview. The previous shape of this branch returned a
        # cheerful OK here and called it "pack-layered" — which meant a record
        # holding only `{"OPERATOR_NMAE": "Chris"}` (one typo, nothing else) passed
        # doctor green, reopening the exact "the record proves nothing" hole this
        # function exists to close. Caught at L3 by codex.
        #
        # A pack CAN legitimately reach here by replacing BOTH base render templates
        # with its own slots, so the message names that possibility instead of
        # accusing — but it still reports UNVERIFIED, because "we could not check"
        # and "we checked and it is fine" must never print the same badge.
        return [
            CheckResult(
                "seed content (recorded answers)",
                False,
                f"the recorded interview ({len(answers)} answer(s)) shares no fields "
                f"with the base seed, so its content cannot be verified",
                "Either a pack replaced both base seed templates (expected — there "
                "is nothing here to check, and doctor records no roster to tell), or "
                "this .levain/answers.json does not belong to this install. Compare "
                "it against `levain init --answers-template`.",
            )
        ]

    results: list[CheckResult] = []

    # TWO TIERS, because a blank is not one kind of problem.
    #
    # The terminal interview and the web form BOTH accept a blank answer for a
    # non-optional field (blank = skip). Failing every such install would condemn
    # people for using the product exactly as its own surfaces let them — so a blank
    # elsewhere is REPORTED, and only IDENTITY is fatal. That line is not arbitrary:
    # a thin seed still supports a working partnership, while an entity with no name
    # for itself or its operator is the failure the pitch says cannot happen.
    # `levain.answers.IDENTITY_SLOTS` is the same constant the `--answers` gate uses,
    # so the two surfaces cannot drift into two different rules.
    from levain.answers import IDENTITY_SLOTS

    empty_identity = sorted(
        f.slot for f in fields if f.slot in IDENTITY_SLOTS and not answers[f.slot].strip()
    )
    # ABSENT is a DISTINCT failure from BLANK, and collapsing the two was the hole: a
    # record is scanned for identity slots that are EMPTY, so an identity slot that was
    # never written at all — one typo'd key among otherwise-correct ones — was filtered
    # out of the evidence before the scan and printed GREEN over a seed with no operator.
    # The PARTIAL case of the same hole codex closed at L3 for the TOTAL case (the
    # `fields`-empty branch that returned a cheerful "pack-layered" OK), and the likelier
    # one by far: one mistyped key beats every key being wrong. Second
    # `absence_of_signal_rendered_as_health` in this one function, which is the argument
    # for reading the WHOLE function whenever one branch of it turns out to lie.
    missing_identity = sorted(
        f.slot for f in plan if f.slot in IDENTITY_SLOTS and f.slot not in answers
    )
    empty_other = sorted(
        f.slot
        for f in fields
        if f.slot not in IDENTITY_SLOTS and is_required(f) and not answers[f.slot].strip()
    )
    if missing_identity or empty_identity:
        # Name WHICH failure it is. "never recorded" and "recorded blank" send the
        # operator to different places — the first says the answer file is wrong (a
        # typo'd or dropped key), the second says the interview was skipped.
        parts = []
        if missing_identity:
            parts.append(f"NEVER RECORDED: {', '.join(missing_identity)}")
        if empty_identity:
            parts.append(f"recorded EMPTY: {', '.join(empty_identity)}")
        results.append(
            CheckResult(
                "seed content (answers present)",
                False,
                f"identity field(s) {' · '.join(parts)}",
                "The seed renders with nothing here and leaves no placeholder "
                "behind, so the entity does not know who it is partnering with "
                "while every other check passes. A slot that is missing entirely is "
                "usually a mistyped key in .levain/answers.json — compare it against "
                "`levain init --answers-template`. (If a pack replaced the base "
                "origin seed, these slots may not apply to this install; doctor "
                "records no roster and cannot tell, so it reports rather than "
                "assumes.) Otherwise re-run `levain init --force`, or supply them "
                "via --answers.",
            )
        )
    elif empty_other:
        results.append(
            CheckResult(
                "seed content (answers present)",
                True,
                f"{len(fields)} recorded field(s); identity present; left blank: "
                f"{', '.join(empty_other)} (a thinner seed, not a broken one)",
            )
        )
    else:
        results.append(
            CheckResult(
                "seed content (answers present)",
                True,
                f"{len(fields)} recorded field(s), none empty",
            )
        )

    # Every recorded answer must appear VERBATIM in the seed file its slot fills.
    # Two value shapes are skipped rather than mis-reported: one containing `<!--`
    # (render strips template comments AFTER substitution, so such a value can be
    # legitimately altered on its way to disk) and one containing `{{` (it would be
    # indistinguishable from an unfilled slot, which is check 1's job anyway).
    seed_cache: dict[str, str | None] = {}

    def _seed_text(name: str) -> str | None:
        if name not in seed_cache:
            f = install / "seed" / name
            try:
                seed_cache[name] = f.read_text(encoding="utf-8") if f.is_file() else None
            except (OSError, UnicodeError):
                seed_cache[name] = None
        return seed_cache[name]

    diverged: list[str] = []
    skipped: list[str] = []
    for f in fields:
        value = answers[f.slot].strip()
        if not value:
            continue
        if "<!--" in value or "{{" in value:
            # Not checkable: render strips template comments AFTER substitution, and
            # a `{{` in a value is indistinguishable from an unfilled slot. COUNTED,
            # not silently dropped — reporting "carries every recorded answer" while
            # having quietly declined to look at some of them is the same
            # unverified-as-verified move this whole function objects to.
            skipped.append(f.slot)
            continue
        text = _seed_text(f.spec_name)
        if text is None:
            continue  # a missing/unreadable seed is the layout check's job
        if value not in text:
            diverged.append(f"{f.slot} (in {f.spec_name})")
    if diverged:
        # REPORTED, NEVER FAILED — and the reason is a correction, not a softening.
        # The install banner explicitly invites this: "Files created (you can
        # hand-edit any of these)" (`install.py` `_print_manifest`) lists the seed.
        # Failing here would condemn an install for doing exactly what the product
        # told the operator to do, with no way back — `levain update` "fixes" the
        # FAIL by re-rendering from the record, i.e. by destroying the edit.
        #
        # It is also not a corruption in the first place: THE ENTITY READS THE SEED.
        # A hand-edited seed is the operator's intent, correctly delivered; the
        # record is only the re-render INPUT. So the real and only consequence is
        # forward-looking — a later re-render would overwrite these edits — and that
        # is a thing to be TOLD, not condemned for.
        results.append(
            CheckResult(
                "seed content (seed matches record)",
                True,
                f"seed edited since init for: {', '.join(sorted(diverged))} — the "
                f"seed is what the entity reads, so this is fine; note only that "
                f"a re-render (`levain update`) would restore the recorded answers "
                f"over these edits",
            )
        )
    else:
        detail = "rendered seed carries every recorded answer"
        if skipped:
            detail += (
                f" (not checkable, contains template syntax: {', '.join(sorted(skipped))})"
            )
        results.append(CheckResult("seed content (seed matches record)", True, detail))

    shapes = shape_violations(fields, answers)
    impossible = [m for sev, m in shapes if sev == SHAPE_IMPOSSIBLE]
    unusual = [m for sev, m in shapes if sev != SHAPE_IMPOSSIBLE]
    if impossible:
        results.append(
            CheckResult(
                "seed content (answer shapes)",
                False,
                "; ".join(impossible),
                "A single-line field cannot hold a multi-line value if it was "
                "answered at its own prompt — this is the signature of answers "
                "landing in the wrong slots. Check the seed reads correctly, then "
                "re-run init with a slot-keyed --answers file.",
            )
        )
    elif unusual:
        results.append(
            CheckResult(
                "seed content (answer shapes)",
                True,
                f"plausible; {len(unusual)} note(s): " + "; ".join(unusual),
            )
        )
    else:
        results.append(
            CheckResult("seed content (answer shapes)", True, "consistent with each field")
        )

    return results


def _check_install_layout(install: Path, expect_hooks: bool = True) -> list[CheckResult]:
    """Static layout check. ``expect_hooks=False`` (a hookless adapter, e.g. openhands)
    checks the seed but SKIPS the activation-tree + hook-script requirement entirely —
    those files do not exist for an adapter whose activation is the runtime condenser, so
    demanding them would FAIL a perfectly healthy sovereign entity."""
    results: list[CheckResult] = []

    if not install.is_dir():
        return [
            CheckResult(
                "install root",
                False,
                f"not a directory: {install}",
                "Pass --path pointing to a Levain install.",
            )
        ]
    results.append(CheckResult("install root", True, str(install)))

    seed = install / "seed"
    if not seed.is_dir():
        results.append(
            CheckResult(
                "seed/",
                False,
                "missing",
                # DERIVED from _SEED_REQUIRED, never hand-listed: the hand-written
                # form read "{origin,partnership,world,memory}.md" and silently went
                # stale the moment the required set changed.
                "Required: " + ", ".join(f"seed/{f}" for f in _SEED_REQUIRED),
            )
        )
    else:
        missing = [f for f in _SEED_REQUIRED if not (seed / f).is_file()]
        if missing:
            # `levain update` writes a base seed this package ships verbatim (the rendered ones
            # are the interview's) when levain has no record of writing it: a package upgrade
            # that added the file. A recorded file that is gone was deleted, and update keeps
            # it deleted, so that case still needs init.
            from levain.install import _read_adapter_receipt

            recorded = _read_adapter_receipt(install) or {}
            update_writes = all(f not in _RENDER_TARGET_SEEDS and _ships_base_seed(f)
                                and f"seed/{f}" not in recorded for f in missing)
            results.append(
                CheckResult(
                    "seed/",
                    False,
                    f"missing required files: {', '.join(missing)}",
                    "Run `levain update`; this levain ships the file(s)."
                    if update_writes else
                    "Re-run `levain init` or restore from adapter source.",
                )
            )
        else:
            present = [f for f in _SEED_EXPECTED if (seed / f).is_file()]
            results.append(
                CheckResult(
                    "seed/",
                    True,
                    f"{len(present)} files present ({', '.join(present)})",
                )
            )

    if not expect_hooks:
        # Hookless adapter (openhands): no activation tree — the condenser is the runtime
        # activation. Report it as an informational PASS, not a missing-tree FAIL.
        results.append(
            CheckResult(
                "activation/",
                True,
                "n/a — hookless adapter (activation is the runtime condenser)",
            )
        )
        return results

    activation = install / "activation"
    if not activation.is_dir():
        results.append(
            CheckResult(
                "activation/",
                False,
                "missing",
                "Required: activation/{posture.md, recency_directives.md, hooks/}",
            )
        )
        return results

    missing_files = [f for f in _ACTIVATION_FILES if not (activation / f).is_file()]
    if missing_files:
        results.append(
            CheckResult(
                "activation/",
                False,
                f"missing: {', '.join(missing_files)}",
                "Re-run `levain init` or restore from adapter source.",
            )
        )
    else:
        results.append(
            CheckResult(
                "activation/",
                True,
                f"{len(_ACTIVATION_FILES)} activation files present",
            )
        )

    hooks = activation / "hooks"
    if not hooks.is_dir():
        results.append(
            CheckResult(
                "activation/hooks/",
                False,
                "missing",
                "Required: activation/hooks/{session_start,user_prompt_submit,_levain_hook}.py",
            )
        )
        return results

    missing_hooks = [f for f in _HOOK_REQUIRED if not (hooks / f).is_file()]
    if missing_hooks:
        results.append(
            CheckResult(
                "activation/hooks/",
                False,
                f"missing scripts: {', '.join(missing_hooks)}",
                "Re-run `levain init` or restore from adapter source.",
            )
        )
        return results

    syntax_errors = []
    for f in _HOOK_REQUIRED:
        script = hooks / f
        try:
            compile(script.read_text(), str(script), "exec")
        except SyntaxError as e:
            syntax_errors.append(f"{f}: {e.msg} line {e.lineno}")
    if syntax_errors:
        results.append(
            CheckResult(
                "hook scripts",
                False,
                "; ".join(syntax_errors),
                "Hook scripts are corrupt. Re-run `levain init`.",
            )
        )
    else:
        results.append(
            CheckResult(
                "hook scripts",
                True,
                f"{len(_HOOK_REQUIRED)} scripts present and parse OK",
            )
        )

    return results


def _check_runtime(install: Path) -> list[CheckResult]:
    results: list[CheckResult] = []

    py = Path(sys.executable)
    py_version = ".".join(map(str, sys.version_info[:3]))
    results.append(
        CheckResult("python interpreter", True, f"{py} ({py_version})")
    )

    from levain.manifest import resolve_anneal_bin

    # spore-751: report the ONE anneal levain's interpreter resolves, which is the one init
    # wires, instead of whatever PATH finds first.
    am_cli = resolve_anneal_bin()
    script_failure = ""
    if os.path.isfile(am_cli):
        ok, out = _probe([am_cli, "--version"])
        if ok:
            version = (out.strip().splitlines() or ["present"])[0]
            results.append(
                CheckResult("anneal-memory CLI", True, f"{am_cli} ({version})")
            )
            return results
        # A script that exists but cannot run (a moved venv's stale shebang) is not the
        # end of the question: every consumer falls back to the module form, so doctor
        # does too before declaring anneal unavailable (codex L3 LOW).
        script_failure = f"; the script {am_cli} failed: {out}"

    ok, out = _probe([sys.executable, "-P", "-m", "anneal_memory", "--version"])
    if ok:
        version = (out.strip().splitlines() or ["present"])[0]
        results.append(
            CheckResult(
                "anneal-memory module",
                True,
                f"importable via {sys.executable} ({version}){script_failure}",
            )
        )
    else:
        results.append(
            CheckResult(
                "anneal-memory",
                False,
                f"not installed for levain's interpreter ({sys.executable})",
                f"Install with: {sys.executable} -m pip install anneal-memory",
            )
        )
    return results


def _anneal_invocation(*args: str) -> str:
    from levain.manifest import anneal_invocation

    return anneal_invocation(*args)


def _probe(cmd: list[str], timeout: float = 5.0) -> tuple[bool, str]:
    """Run a command; return (ok, stdout-or-truncated-stderr).

    Strips LEVAIN_HOOK_SUPPRESS from the child environment so a probe that
    happens to invoke a Levain-aware tool isn't silenced by a parent shell
    that set the var for an unrelated reason.
    """
    from levain.launch import child_env

    env = child_env()
    env.pop("LEVAIN_HOOK_SUPPRESS", None)
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env
        )
        if r.returncode == 0:
            return True, r.stdout
        return False, (r.stderr or r.stdout).strip()[:500]
    except FileNotFoundError:
        return False, f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"


def _check_confinement(install: Path) -> list[CheckResult]:
    """Report whether ``levain run`` will get its bash hand on THIS host, and if not, how to fix it.

    ⚖ A MISSING HAND IS REPORTED, NEVER FAILED — a deliberate call, not an oversight. (The one FAIL
    is a ``confinement.json`` that ``levain run`` refuses: that entity cannot start at all.) An entity with only its
    file-editor hand is a WORKING, SUPPORTED configuration: the in-process crown-jewels floor is
    fully cross-platform and enforces the same denylist. Turning a healthy install red because it
    lacks an optional hand is how an operator learns to ignore doctor, and it would contradict what
    the code actually does (``levain run`` fails CLOSED and carries on without bash, by design).
    The remedy is rendered in the SAME ``→`` shape ``_emit`` uses for a hint, so a report that needs
    action still looks like one.

    ⚡ WHY THIS CHECK EXISTS AT ALL. Before K4c the answer was uninteresting — macOS had a floor,
    everything else did not. Now Linux ships one, and the common Ubuntu case is a SUPPORTED platform
    whose kernel refuses to let the sandbox start. That is one command away from working, and
    without a surface saying so the operator's only signal is a banner line and a missing tool.

    The diagnosis itself is NOT computed here: :func:`~levain.firing.confinement.diagnose_confinement`
    is the single source for the host's answer, shared with the ``levain run`` banner; the entity's
    localhost opt-out is read from the same config ``levain run`` reads."""
    # Lazy import — this module keeps levain imports out of its top level (see `run_doctor`).
    try:
        from levain.firing.confinement import (
            OFFLINE_RESIDUAL,
            diagnose_confinement,
            load_confinement_config,
            resolve_localhost_deny,
        )
    except Exception as exc:  # noqa: BLE001 — never let a diagnosis break the whole doctor run
        return [CheckResult("confinement floor", True, f"not determinable ({exc})")]

    # The entity's own config first, on EVERY host: `levain run` refuses a broken one whether or not
    # bash is possible, so doctor must not report a working configuration (codex, L3 r2). This is
    # the one outcome here that FAILS — not a missing optional hand, an entity that cannot start.
    # It runs BEFORE the host diagnosis, so a probe that raises cannot turn it into "not
    # determinable" (complement + codex, L3 2026-10-01, reproduced).
    try:
        cfg = load_confinement_config(install)
    except Exception as exc:  # noqa: BLE001
        return [CheckResult("confinement floor", False,
                            f"`levain run` will refuse to start this entity: {exc}")]

    try:
        d = diagnose_confinement()
    except Exception as exc:  # noqa: BLE001
        return [CheckResult("confinement floor", True, f"not determinable ({exc})")]
    if d.supported:
        # The host can sandbox bash, but the entity's localhost deny can still drop it or take its
        # network. `levain run` asks the SAME resolver with the same config, so the two agree.
        bash_ok, refusal, offline = resolve_localhost_deny(cfg.allow_localhost_outbound)
        # The line `levain run` prints when $PYTHONUSERBASE puts the user site outside the floor.
        try:
            from levain.firing.confinement import python_userbase_note

            note = python_userbase_note(Path.home())   # build_policy's home
        except Exception:  # noqa: BLE001 — never let a diagnosis break the whole doctor run
            note = None
        tail = f"\n      {note}" if note else ""
        if not bash_ok:
            return [CheckResult("confinement floor", True,
                                f"{d.provider} available, but `levain run` gets file_editor only: "
                                f"{refusal}{tail}")]
        if offline:
            return [CheckResult("confinement floor", True,
                                f"{d.provider} — `levain run` gets file_editor + bash, and bash has "
                                f"no IP network: connections back to this host are blocked by "
                                f"removing it; {OFFLINE_RESIDUAL}. \"allow_localhost_outbound\": "
                                f"true restores the network, with the spore-755 exposure{tail}")]
        return [CheckResult("confinement floor", True,
                            f"{d.provider} — `levain run` gets file_editor + bash{tail}")]
    detail = f"{d.reason} — `levain run` gets file_editor only (this is a supported configuration)"
    if d.remedy:
        detail += f"\n      → to enable the bash hand: {d.remedy}"
    return [CheckResult("confinement floor", True, detail)]


def _check_unix_sockets(install: Path) -> list[CheckResult]:
    """The unix-socket lines ``levain run`` prints (one generator, :func:`~levain.firing.confinement.
    unix_socket_lines`): the tier claim, each operator grant as a named waiver, each default grant, the
    ssh agent, and a legacy ``runtime_sockets`` key as ignored. A grant file that cannot be read, or
    whose state cannot be determined at all, FAILS: ``levain run`` refuses bash with it. A hands user's
    entity reads no grant file, and none is read where grants are not applied (one line says so). A
    broken entity config is ``_check_confinement``'s to report."""
    try:
        from levain.firing.confinement import ConfinementError, load_confinement_config, unix_socket_lines
    except Exception as exc:  # noqa: BLE001 — never let a diagnosis break the whole doctor run
        return [CheckResult("unix sockets", False, f"not determinable ({exc})")]
    try:
        cfg = load_confinement_config(install)
    except Exception:  # noqa: BLE001 — _check_confinement already FAILs a broken config
        cfg = None
    try:
        lines = unix_socket_lines(cfg)
    except ConfinementError as exc:
        return [CheckResult("unix sockets", False, f"`levain run` will refuse bash: {exc}")]
    except Exception as exc:  # noqa: BLE001 — an undeterminable grant file is a FAIL, never a pass
        return [CheckResult("unix sockets", False, f"not determinable, so `levain run` may refuse bash ({exc})")]
    return [CheckResult("unix sockets", True, "\n      ".join(lines))]


def _check_hands_isolation(install: Path) -> list[CheckResult]:
    """Is this entity's bash run as its own unprivileged user outside the interactive REPL (M2)?

    Not set up is a WARNING, loud but not a failure: the entity works, but its bash runs as the
    operator, so it can read the argv and environment of every process the operator runs. A
    recorded user that cannot be started (deleted, or its sudoers rule gone) is a FAILURE."""
    name = "hands isolation"
    try:
        from levain.firing.confinement import load_confinement_config
        from levain.firing.hands import host_os
    except Exception as exc:  # noqa: BLE001
        return [CheckResult(name, True, f"not determinable ({exc})")]
    try:
        host_os()
    except Exception:  # noqa: BLE001 — no setup exists for this OS; nothing to warn about
        return []
    try:
        cfg = load_confinement_config(install)
    except Exception:  # noqa: BLE001 — _check_confinement already FAILs a broken config
        return []
    if cfg.hands_user is None:
        return [CheckResult(
            name, True,
            "NOT SET UP. Headless chat and unattended runs execute bash as YOU, so the entity can "
            "read the environment and command line of every process you run (tokens included).",
            hint=f"sudo levain setup-isolation --path {shlex.quote(str(install))}",
            warn=True,
        )]
    import pwd

    redo = f"sudo levain setup-isolation --undo --path {shlex.quote(str(install))}, then set it up again"
    try:
        entry = pwd.getpwnam(cfg.hands_user)
    except KeyError:
        return [CheckResult(name, False, f"hands user {cfg.hands_user} does not exist", hint=redo)]
    if entry.pw_uid != cfg.hands_uid:
        # an OS update can take an id for a system account (macOS 15 did, for 301-304)
        return [CheckResult(name, False, f"hands user {cfg.hands_user} now has id {entry.pw_uid}, not the "
                            f"recorded {cfg.hands_uid}", hint=redo)]
    # No stat of the sudoers drop-in: /etc/sudoers.d is root-only on Linux (measured in CI), so the
    # operator cannot see it. Writing to the workspace AS the hands user tests the rule, the account
    # and its ownership of the workspace in one step.
    ok, out = _probe([SUDO, "-n", "-u", cfg.hands_user, "/bin/test", "-w", str(cfg.hands_workspace)])
    if not ok and not os.access(SUDO, os.X_OK):
        return [CheckResult(name, False, f"sudo is not at {SUDO}", hint=redo)]
    if not ok:
        return [CheckResult(name, False, f"{cfg.hands_user} cannot write its workspace "
                            f"{cfg.hands_workspace} ({out or 'refused'})", hint=redo)]
    from levain.firing.hands import egress_boundary_problem, egress_drift_problem, hands_net_group, net_group_problem

    if host_os() == "linux" and (
            problem := net_group_problem(cfg.hands_user)
            or egress_drift_problem(cfg.hands_user, cfg.hands_uid, cfg.hands_egress_ports)
            or egress_boundary_problem(cfg.hands_user, cfg.hands_egress_ports,
                                       net_group=hands_net_group(cfg.hands_user))):
        # A behaviour probe, not a ruleset read: listing nftables needs root (P-1 (a)).
        return [CheckResult(name, False, f"network boundary: {problem}",
                            hint=f"sudo levain setup-isolation --path {shlex.quote(str(install))} (a repair run "
                                 "loads it again)")]
    from levain.firing.confinement import HANDS_PYTHON, HANDS_ZSH

    if host_os() == "linux" and not os.access(HANDS_PYTHON, os.X_OK):
        return [CheckResult(name, False, f"{HANDS_PYTHON} is missing: every process run as {cfg.hands_user} "
                            "starts through it, so bash and the file editor refuse",
                            hint="install python3")]
    from levain.firing.hands import WORKSPACE_ROOT
    from levain.firing.ws_git import bare_repository_explicit, foreign_entries, wildcard_safe_directory

    extra: list[CheckResult] = []
    if host_os() == "linux" and not os.access(HANDS_ZSH, os.X_OK):
        extra.append(CheckResult("hands file editor", True,
                                 f"{HANDS_ZSH} is missing: the file editor refuses as {cfg.hands_user}",
                                 hint="install zsh", warn=True))
    wildcard = wildcard_safe_directory(tuple(WORKSPACE_ROOT.values()))
    if wildcard:
        extra.append(CheckResult(
            "git trust", True,
            "your git trusts EVERY repository, or every one in the entity's workspace (safe.directory in "
            + ", ".join(wildcard) + "). That switches off the check that keeps your git from running code "
            "the entity writes into a repository in its workspace.",
            hint="remove that safe.directory line, or narrow it to paths outside the entity's workspace",
            warn=True,
        ))
    if not bare_repository_explicit():
        extra.append(CheckResult(
            "git bare repositories", True,
            "your git will use a bare repository it finds in a folder by itself (safe.bareRepository is not "
            "explicit). Nothing in the entity's workspace is yours, so there is no folder of yours it could "
            "plant one in, but with this setting your git never picks one up unasked.",
            hint="git config --global safe.bareRepository explicit",
            warn=True,
        ))
    from levain.firing.ws_git import Hands, WsGitError

    try:
        foreign = foreign_entries(Hands(cfg.hands_user, cfg.hands_uid, entry.pw_dir, cfg.hands_workspace))
    except (WsGitError, OSError) as exc:
        return [CheckResult(name, False, str(exc), hint=redo), *extra]
    if foreign:
        return [CheckResult(
            name, False,
            f"{len(foreign)} entr(y/ies) in the workspace do not belong to the entity's user, or are writable by "
            "you: " + ", ".join(str(g) for g in foreign[:3]) + ". Everything there must "
            "be the entity's and read-only to you: a folder of yours that the entity can write is one it can "
            "turn into a repository your git trusts and runs code from, as you.",
            hint="remove what is yours with `sudo rm -r`, take write away from you (`sudo chmod o-w`, or the "
                 "ACL entry that grants it), or set the entity up again; import a repository with `levain ws-adopt`",
        ), *extra]
    from levain.firing.confinement import hands_for

    try:
        separated = hands_for(cfg, "headless") is not None
    except Exception:  # noqa: BLE001 — the account check above already passed
        separated = False
    if not separated:
        return [CheckResult(
            name, True,
            f"hands user {cfg.hands_user} is set up (workspace {cfg.hands_workspace}), but on this OS "
            "levain still runs the entity's bash as you",
            hint="running bash as the entity's own user is built for macOS and Linux",
            warn=True,
        ), *extra]
    return [CheckResult(
        name, True,
        f"hands user {cfg.hands_user} is set up: headless chat, --task and seats run the entity's bash "
        f"as it, in {cfg.hands_workspace}; the interactive REPL runs bash as you, since you read every turn",
    ), *extra]
def _check_floor_placeholders() -> list[CheckResult]:
    """Sweep the Linux floor's session placeholders (an absent ``~/.netrc`` the bash floor masks over
    an empty 0444 file while a session runs): remove the ones whose session is gone, which a crash
    or SIGKILL leaves behind, and name the ones a live session still holds. Silent where there are
    none, which is every macOS host."""
    try:
        from levain.firing.confinement import (
            ledger_problem,
            live_floor_placeholders,
            sweep_floor_placeholders,
        )
        problem = ledger_problem()
        if problem is not None:
            return [CheckResult(
                "floor placeholders", False,
                f"{problem}: levain cannot tell which placeholders are its own, so it removes none",
                hint="move the file or link aside; levain makes a fresh ledger at the next bash start",
            )]
        removed = sweep_floor_placeholders()
        live = live_floor_placeholders()
    except Exception as exc:  # noqa: BLE001 — never let the sweep break the whole doctor run
        return [CheckResult("floor placeholders", True, f"not determinable ({exc})")]
    if not removed and not live:
        return []
    parts = []
    if removed:
        parts.append("removed, their session gone: " + ", ".join(removed))
    if live:
        parts.append("held by a running session (removed when it ends): "
                     + ", ".join(str(p) for p in live))
    return [CheckResult("floor placeholders", True, "; ".join(parts))]


def _store_requires_baton(install: Path) -> bool:
    """Whether this install's store reports `consolidate_requires_baton` (anneal 1.0). False when
    the store is absent or `status --json` cannot be read: the compat rows report that."""
    from levain import manifest

    store = install / ".levain" / "memory.db"
    if not store.is_file():
        return False
    status = manifest._run_anneal_json(store, manifest.resolve_anneal_bin(), ["status", "--json"])
    return isinstance(status, dict) and status.get("consolidate_requires_baton") is True


def _check_baton(install: Path) -> list[CheckResult]:
    """anneal 1.0's consolidate baton (KL-19): on a store that requires it, the MCP launcher's
    session (``ANNEAL_SESSION_ID``) must hold it, or every wrap over MCP is downgraded to capture
    and the entity records memory it can never consolidate. No row for a store without the policy
    (every pre-1.0 store) or when `status --json` cannot be read: the compat rows report that."""
    from levain import manifest

    store = install / ".levain" / "memory.db"
    if not store.is_file():
        return []
    status = manifest._run_anneal_json(store, manifest.resolve_anneal_bin(), ["status", "--json"])
    if not isinstance(status, dict) or status.get("consolidate_requires_baton") is not True:
        return []
    holder = status.get("baton_holder")
    if holder == manifest.ANNEAL_SESSION_ID:
        return [CheckResult("consolidate baton", True, f"held by '{holder}', the session the MCP server runs as")]
    if status.get("baton_readable") is False:
        what = "unreadable"
    else:
        what = f"held by '{holder}'" if holder else "not claimed"
    claim = _anneal_invocation("--db", str(store), "baton", "claim", manifest.ANNEAL_SESSION_ID)
    return [CheckResult(
        "consolidate baton", False,
        f"{what}; this store requires it for '{manifest.ANNEAL_SESSION_ID}', so a wrap is downgraded to capture-only",
        f"Claim it: {claim}" + (" --take" if holder or what == "unreadable" else ""),
    )]


def _check_store(install: Path) -> list[CheckResult]:
    store = install / ".levain" / "memory.db"

    if not store.is_file():
        return [
            CheckResult(
                ".levain/memory.db",
                False,
                "missing",
                f"Initialize: {_anneal_invocation('--db', str(store), 'init', '--schema', 'partnership')}",
            )
        ]

    try:
        with sqlite3.connect(f"file:{store}?mode=ro", uri=True, timeout=2) as con:
            cur = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "ORDER BY name LIMIT 5"
            )
            tables = [row[0] for row in cur.fetchall()]
    except sqlite3.Error as e:
        return [
            CheckResult(
                ".levain/memory.db",
                False,
                f"sqlite open failed: {e}",
                f"Check perms; try `{_anneal_invocation('--db', str(store), 'status')}`.",
            )
        ]

    if not tables:
        return [
            CheckResult(
                ".levain/memory.db",
                False,
                "empty (no tables)",
                f"Initialize: {_anneal_invocation('--db', str(store), 'init', '--schema', 'partnership')}",
            )
        ]
    label = f"reachable ({len(tables)} table(s): {', '.join(tables[:3])}{'…' if len(tables) > 3 else ''})"
    return [CheckResult(".levain/memory.db", True, label)]


# what each row of a levain-autonomic/3 store's INVARIANT (schema3.INVARIANT) means, in the doctor's words
_INVARIANT_MEANS = {
    "terminal_without_receipt": "a closed run, effect, pending or request whose receipt is missing",
    "receipts_per_subject_gt_1": "more than one receipt for one subject",
    "receipt_without_terminal": "a receipt whose subject never closed (the reaper adopts it when it can)",
    "closing_left": "a close statement's mark left behind (the reaper clears it)",
    "hold_rejected_without_run_close": "a rejected hold whose run was not closed",
    "not_started_uncounted": "an attempt that did not start, with no held count naming it, so its retry is refused",
    "held_at_bound_unclosed": "a held count at its bound with neither its run's close nor its request's close",
}


def _check_autonomic_store(store_dir: Path | None = None) -> list[CheckResult]:
    """The autonomic store's own consistency read (R15-5 S3). The store is this user's, not the install's: the one
    ``default_store_dir()`` names (``$LEVAIN_HOME/autonomic``, else ``~/.levain/autonomic``), the default of every
    ``levain binding`` command's ``--store-dir``; ``store_dir`` reads another.
      - No row without a database, for an empty one, or for a ``levain-autonomic/1`` or ``/2`` store (those are the
        ``levain binding`` commands' to report).
      - A ``levain-autonomic/3`` store: one FAIL row per row of its INVARIANT (``schema3.invariant``), else one row
        naming its legacy rows (``schema3.census``) and the receipts not yet copied to the receipt file.
      - Any other database there (a newer format, tables with no marker, a schema not this levain's): a FAIL row
        worded by ``schema3.refuse_unless_this_v3``'s refusal.
      - A database that cannot be opened or read, or a read that raises: a FAIL row, since an unreadable store cannot
        be told from a corrupt one. Doctor goes on to its next check.
    The store is read through a read-only connection, in one snapshot, and never written."""
    from levain.autonomic import schema3
    from levain.autonomic.db import DB_NAME, STORE_FORMAT_V1, STORE_FORMAT_V2, default_store_dir, store_marker

    db = (store_dir.expanduser() if store_dir is not None else default_store_dir()) / DB_NAME
    name = "autonomic store"
    where = f"{db}" + (" (this user's store ($LEVAIN_HOME/autonomic), not the install's)" if store_dir is None
                       else "")
    conn: sqlite3.Connection | None = None
    try:
        try:
            if not stat.S_ISREG(db.stat().st_mode):
                return []
        except FileNotFoundError:
            return []
        conn = sqlite3.connect(db.absolute().as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=2)
        conn.execute("BEGIN")
        marker = store_marker(conn)
        if marker in (STORE_FORMAT_V1, STORE_FORMAT_V2):
            return []
        if marker is None and conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is None:
            return []
        try:
            schema3.refuse_unless_this_v3(conn)
        except schema3.StoreFormatError as e:
            return [CheckResult(name, False, f"{where}: {e}")]
        rows = schema3.invariant(conn)
        legacy = len(schema3.census(conn))
        unprojected = conn.execute("SELECT COUNT(*) FROM gate_receipts WHERE projected = 0").fetchone()[0]
    except Exception as e:  # noqa: BLE001 -- one store doctor cannot read is a FAIL row, never the end of doctor
        return [CheckResult(name, False, f"{where} cannot be read ({type(e).__name__}: {e})",
                            "Check the file's owner and permissions; a store this user can read that still fails "
                            "to read may be corrupt.")]
    finally:
        if conn is not None:
            conn.close()
    if rows:
        return [CheckResult(f"{name} INVARIANT", False,
                            f"{kind} {subject}: {_INVARIANT_MEANS.get(kind, kind)}, in {where}")
                for kind, subject in rows]
    return [CheckResult(name, True, f"{schema3.STORE_FORMAT_V3} at {where}: invariant holds; {legacy} legacy row(s); "
                                    f"{unprojected} receipt(s) not yet in the receipt file")]


def _check_cockpit_authority(install: Path) -> list[CheckResult]:
    """The cockpit's signing keys (trust-tiering DESIGN §3.9), read from ``<install>/.levain/cockpit``.
    A WARNING, never a failure, while exactly one laptop-presence key that can sign is enrolled: losing it
    leaves nothing that can enrol a successor. A FAIL per QUARANTINED key, and a FAIL when unrevoked
    laptop-presence keys remain but every one is quarantined (nothing in this levain can recover that).
    No row at all without a cockpit journal. The journal is
    opened read-only (SQLite may add its WAL side files beside it): a ``levain-cockpit/2`` journal is
    reported, not migrated, and no schema is created; any other format but /3 fails. It reads
    ``<install>/.levain/cockpit`` only: a store at another data root (an embedder's) gets no row."""
    from datetime import datetime, timezone

    from levain.authority import LAPTOP, anchor_path
    from levain.cockpit.authority import RequestAuthority
    from levain.cockpit.firejournal import _MIGRATES_FROM, DB_NAME, STORE_FORMAT

    root = install / ".levain" / "cockpit"
    journal = root / DB_NAME
    name = "cockpit authority"
    if not journal.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{journal}?mode=ro", uri=True, timeout=2, isolation_level=None)
        try:
            fmt = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
            found = None if fmt is None else fmt[0]
            if found == _MIGRATES_FROM:
                return [CheckResult(name, True, f"journal format {found!r}; the next "
                                                "`levain cockpit keys` (or a restarted `levain serve "
                                                "--write`) upgrades it")]
            if found != STORE_FORMAT:                 # missing, unknown or newer: the journal refuses it too
                return [CheckResult(name, False, f"unsupported journal format {found!r}; this levain reads "
                                                 f"{STORE_FORMAT} and migrates {_MIGRATES_FROM}")]
            conn.execute("BEGIN")
            auth = RequestAuthority.capture(conn, now=datetime.now(timezone.utc), anchor=anchor_path(root),
                                            source=str(journal))
            conn.execute("COMMIT")
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return [CheckResult(name, True, f"the journal cannot be read ({exc})", warn=True)]
    if auth.authority is None:
        if auth.log_empty:
            return [CheckResult(name, True, str(auth.authority_error))]
        return [CheckResult(name, True, str(auth.authority_error), warn=True)]
    if auth.authority_error is not None:              # a migrated store, its keys not yet re-enrolled
        return [CheckResult(name, True, auth.authority_error)]
    # a quarantined key signs nothing new, so it is not counted as a laptop-presence key that can sign
    quarantined = auth.authority.quarantined_keys()
    laptops = [k.fingerprint for k in auth.authority.signing_laptop_keys()]
    stuck = [k.fingerprint for k, _why in quarantined if k.key_class == LAPTOP] if not laptops else []
    rows = [_quarantine_row(name, root, key, why, stuck=bool(stuck)) for key, why in quarantined]
    # re-genesis (§3.9) is S3a-2b-2's, not this build's: name it as missing, never as a command to run
    recovery = ("recovery after losing it, `levain cockpit ledger re-genesis` (a new authority log, every grant "
                "void), is not in this build")
    if stuck:
        return rows + [CheckResult(
            name, False, f"no unrevoked {LAPTOP} key can sign: every one is QUARANTINED ({', '.join(stuck)})",
            "There is no in-product recovery. The log still replays: its keys and grants stand, and links a key "
            f"below {LAPTOP} may sign still append, but no key can be enrolled, no {LAPTOP} key revoked and no T3 "
            "act signed. The way out, `levain cockpit ledger re-genesis` (a new authority log, every grant void), "
            "is not in this build.")]
    if len(laptops) == 1:
        return rows + [CheckResult(
            name, True, f"one {LAPTOP} key is enrolled ({laptops[0]})",
            f"Enrol a second {LAPTOP} key now: make one with `levain cockpit key create`, then `levain cockpit "
            "enroll <key.pub> --signing-key <the enrolled key>`; "
            f"{recovery}.",
            warn=True)]
    if not laptops:
        return rows + [CheckResult(name, True, f"no unrevoked {LAPTOP} key: nothing can enrol a key or sign a T3 act",
                                   "Recovery, `levain cockpit ledger re-genesis` (a new authority log, every grant "
                                   "void), is not in this build.",
                                   warn=True)]
    return rows + [CheckResult(name, True,
                               f"{len(laptops)} {LAPTOP} keys enrolled (head at seq {auth.authority.seq})")]


#: What each refusal of the install helper's ``enrol-authority-signer`` / ``remove-authority-signer`` means, in the
#: words a row's hint uses. A test greps ``levain/slot/install_helper.py`` for every ``Refused("authority-signer...")``
#: and holds this table to exactly those names.
AUTHORITY_SIGNER_REFUSALS: dict[str, str] = {
    "authority-signer-key": "a key was not one the authority log would enrol (a passkey goes in --passkey)",
    "authority-signer-verifier": "ssh-keygen could not read a key, or named another fingerprint for it",
    "authority-signer-changed": "the key is already listed under another name or with fewer permissions: "
                                "remove-authority-signer it first, then enrol",
    "authority-signers-unsafe": "the signers file is not root's alone (another account can write it or its folder)",
    "authority-signers-unreadable": "the signers file cannot be read whole, is not plain UTF-8, or is too big",
    "authority-signers-too-large": "the file would pass its size limit; nothing was written",
    "authority-signers-write-failed": "the file could not be written; the earlier file stands",
}


def authority_signer_sudo_line(host: str) -> str:
    """The one ``sudo`` line that enrols the signers: the install helper's real path, then the verb with the flags
    :func:`levain.slot.grammar.parse_install_argv` accepts (a test parses this line with the placeholders filled)."""
    from levain.slot import layout

    return (f"sudo {layout.install_helper_path(host)} enrol-authority-signer --principal YOUR_NAME "  # type: ignore[arg-type]
            "--public-key '<type> <base64>' [--passkey '<type> <base64>' ...] [--confirm]")


def _authority_signers_hint(host: str, *, why: str | None = None) -> str:
    from levain.slot import layout

    helper = layout.install_helper_path(host)         # type: ignore[arg-type]
    refusals = "; ".join(f"{n}: {m}" for n, m in AUTHORITY_SIGNER_REFUSALS.items())
    lead = f"{why} " if why else ""
    return (f"{lead}Run `{authority_signer_sudo_line(host)}`: the laptop key is its .pub file's `<type> <base64>`, "
            "each phone passkey (the opt-in names) its own --passkey, and `levain cockpit keys` lists the "
            "fingerprints the helper prints back, to compare. To take a key out again, "
            f"`sudo {helper} remove-authority-signer --key SHA256:...`. If the helper refuses, its answer names "
            f"why. {refusals}.")


def _check_authority_signers(install: Path, *, signers: Path | None = None, now: Any = None) -> list[CheckResult]:
    """Root's authority-signers file and the graduation policies it anchors (14b DESIGN_REV2 S5, rows 1 and 2).

    Only for an install with a cockpit authority log (no journal, or an uninitialised log, gives no row: there is
    nothing yet to graduate). ``authority signers`` is a WARNING, never a failure, while nothing can graduate: the
    file is missing, is not a file :func:`levain.trust_anchor.trust_anchor_problem` trusts, is refused by the install
    helper's own reader (:func:`levain.slot.install_helper._read_signers`: a link, past its size bound, not UTF-8
    throughout, unreadable), or lists no key for ``levain-authority``. ``graduation policies`` is a WARNING naming
    the live policies :func:`levain.autonomic.coverage.live` (the gate's predicate) says root's keys do not anchor,
    each with the signer that is missing. It is read-only: the journal opens read-only and root's file is only read.
    Nothing here can abort doctor: any other failure is a WARNING ``could not check``. ``signers`` and ``now`` are
    test seams."""
    try:
        return _authority_signers_rows(install, signers, now)
    except Exception as exc:  # noqa: BLE001 - a doctor check reports, it never crashes the run
        return [CheckResult("authority signers", True, f"could not check: {type(exc).__name__}: {exc}",
                            "The cockpit authority row says whether the log itself is readable.", warn=True)]


def _authority_signers_rows(install: Path, signers: Path | None, now: Any) -> list[CheckResult]:
    from datetime import datetime, timezone

    from levain import authority as auth
    from levain.autonomic import coverage as cov
    from levain.autonomic.confirm import _SIGNERS_FILE_OWNERS
    from levain.cockpit.firejournal import DB_NAME
    from levain.graduation_policy import AuthorityCoverage, graduation_policies
    from levain.slot import Refused, root_tree
    from levain.slot.install_helper import _read_signers
    from levain.trust_anchor import trust_anchor_problem

    root = install / ".levain" / "cockpit"
    journal = root / DB_NAME
    if not journal.is_file():
        return []
    host = "darwin" if sys.platform == "darwin" else "linux"
    path = signers if signers is not None else root_tree.authority_signers_path(host)   # type: ignore[arg-type]
    name = "authority signers"
    rows: list[CheckResult] = []
    state: Any = None
    log_problem: str | None = None
    try:
        pinned = auth.read_anchor(auth.anchor_path(root))              # before the replay's first SELECT
        # the gate's own read-only replay; the store id is the gate's autonomic store's, which a doctor row never uses
        state = AuthorityCoverage(root, allowed_signers=path, store_id="doctor").replay(check_anchor=False)
        try:
            auth.check_anchor(state, pinned, path=auth.anchor_path(root))
        except auth.NotInitialised as exc:             # the log replayed, so it is initialised: its anchor is the gap
            state = None
            rows.append(CheckResult("authority anchor", True, f"the anchor check could not run: {exc}",
                                    "The cockpit authority row has the log's state.", warn=True))
    except auth.NotInitialised:
        return []
    except Exception as exc:  # noqa: BLE001 - a log that cannot be replayed is a row of its own, never a quiet pass
        state = None
        log_problem = f"{type(exc).__name__}: {exc}"
    keys: frozenset[str] | None = None
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        row = CheckResult(
            name, True, f"nothing can graduate: no enrolled authority signer ({path} does not exist)",
            _authority_signers_hint(host), warn=True)
    except OSError as exc:
        row = CheckResult(name, True, f"{path} cannot be read (authority-signers-unreadable): "
                                      f"{exc.strerror or exc}", _authority_signers_hint(host), warn=True)
    else:
        problem, _resolved = trust_anchor_problem(path, what="authority signers file", owners=_SIGNERS_FILE_OWNERS)
        raw: bytes | None = None
        if problem is not None:
            row = CheckResult(
                name, True, f"nothing can graduate: {problem}",
                _authority_signers_hint(host, why="The helper will not write over it (authority-signers-unsafe), so "
                                                  "remove the file as root first, or fix its owner and mode."),
                warn=True)
        else:
            try:
                # the helper's own bounded reader, with the owner the trust-anchor check above accepted: the same
                # size bound, strict UTF-8 of the whole file and link refusal the enrol and remove verbs apply
                raw = _read_signers(path, owner=st.st_uid)
                row = None   # type: ignore[assignment]   # set below, once the bytes parse
            except Refused as refusal:
                row = CheckResult(name, True, f"{path} is refused by the install helper ({refusal.name}): "
                                              f"{refusal.detail}",
                                  _authority_signers_hint(host, why=f"The helper's answer is {refusal.name}."),
                                  warn=True)
        if raw is None and problem is None and row is None:
            row = CheckResult(
                name, True, f"nothing can graduate: no enrolled authority signer ({path} does not exist)",
                _authority_signers_hint(host), warn=True)
        if raw is not None:
            keys = cov.authority_keys(cov.parse_root_signers(raw))
            if keys:
                row = CheckResult(name, True, f"{len(keys)} key(s) root lists for levain-authority ({path})")
            else:
                keys = frozenset()
                row = CheckResult(name, True, f"nothing can graduate: {path} lists no key for levain-authority",
                                  _authority_signers_hint(host), warn=True)
    rows.append(row)
    if log_problem is not None:
        rows.append(CheckResult("authority log", True, f"could not check the authority log: {log_problem}",
                                "The cockpit authority row says whether the log itself is readable.", warn=True))
    if state is None:
        return rows
    when = now if now is not None else datetime.now(timezone.utc)
    key_of = cov._key_of
    stale: list[tuple[str, tuple[str, ...]]] = []
    for p in graduation_policies(state):
        # live by the gate's own predicate with the policy's own signers listed (time only), not with root's
        if not cov.live(p, when, frozenset(k for k in (key_of(p.born_signer), key_of(p.current_signer)) if k)):
            continue
        if keys is not None and cov.live(p, when, keys):
            continue
        if keys is None:
            stale.append((p.grant_id, ()))
            continue
        born, cur = key_of(p.born_signer), key_of(p.current_signer)
        gaps = tuple(label for label, k in (("born signer", born), ("current signer", cur if cur != born else None))
                     if k is not None and k not in keys)
        if gaps:
            stale.append((p.grant_id, gaps))
    if stale:
        ids = ", ".join(g for g, _ in sorted(stale))
        if keys is None:
            rows.append(CheckResult(
                "graduation policies", True,
                f"{len(stale)} live policy(ies) ({ids}); root's signers file could not be read, so which of them "
                "its keys anchor is not checked",
                "Fix the authority signers row first.", warn=True))
        else:
            why = "; ".join(f"{g}: {' and '.join(w)} unlisted" for g, w in sorted(stale))
            born = any("born signer" in w for _, w in stale)
            cur = any("current signer" in w for _, w in stale)
            parts = []
            if born:
                parts.append("A missing born signer is not repaired by a renewal: enrol that key (the authority "
                             "signers row has the one sudo line), or revoke the policy and create it again with a "
                             "key root lists.")
            if cur:
                parts.append("A missing current signer is repaired by renewing: sign it again with a key root lists.")
            rows.append(CheckResult(
                "graduation policies", True,
                f"{len(stale)} live policy(ies) root's keys do not anchor ({why}); each covers nothing",
                " ".join(parts), warn=True))
    return rows


def _quarantine_row(name: str, root: Path, key: Any, why: str, *, stuck: bool = False) -> CheckResult:
    """A FAIL for one QUARANTINED key (``levain.authority.AuthoritySnapshot.quarantined_keys``: its stored custody is
    not the one its key material shows). Every verify site refuses its new signatures; the remedy
    names commands this levain has, with this cockpit's data root and the key's own fingerprint filled in. With
    ``stuck`` (no laptop-presence key can sign) every remedy needs a signer that does not exist, so none is named."""
    from levain.authority import DISPLAY_PHONE, LAPTOP, is_passkey_material

    dr = f"--data-root {shlex.quote(str(root))}"
    fp = shlex.quote(key.fingerprint)
    revoke = f"levain cockpit revoke {fp} --signing-key YOUR_LAPTOP_PRESENCE_KEY {dr}"
    if stuck:
        hint = (f"No remedy can run: each one is signed by a {LAPTOP} key that can sign, and none is left "
                "(the cockpit authority row says what that means).")
    elif key.key_class == DISPLAY_PHONE:
        hint = (f"Revoke it: `{revoke}`. Then make a fresh passkey on the phone page (its enrol step writes the "
                f"passkey .pub) and enrol it at custody passkey: `levain cockpit enroll <passkey .pub> --class "
                f"{DISPLAY_PHONE} --custody passkey --signing-key YOUR_LAPTOP_PRESENCE_KEY {dr}`. The same key "
                "cannot be enrolled again: a revoked key stays revoked.")
    else:
        hint = (f"Enrol a replacement {key.key_class} key at the custody it really has first, signed by a "
                f"laptop-presence key that is not quarantined: `levain cockpit enroll <key.pub> --class "
                f"{key.key_class} --custody <its custody> --signing-key YOUR_LAPTOP_PRESENCE_KEY {dr}` (`levain "
                f"cockpit key create` makes one and records its custody). Then revoke this one: `{revoke}`. The same "
                "key cannot be enrolled again: a revoked key stays revoked.")
    how = (f"it was enrolled at custody {key.custody} before levain refused WebAuthn material there"
           if is_passkey_material(key.public_key) else "its key material is not a passkey's")
    return CheckResult(name, False, f"key {key.fingerprint} ({key.key_class}) is QUARANTINED: {why}; {how}, so "
                                    "every levain check refuses its new signatures", hint)


def _check_continuity_headroom(install: Path) -> list[CheckResult]:
    """How much room the entity's continuity has left under anneal's hard maximum.

    Reads the file the way the next wrap will, through the store (``Store.load_continuity``, strict UTF-8), and
    counts what anneal counts: its characters minus the durable section (``anneal_memory.durable.section_chars``)
    against ``hard_max_chars`` of the schema persisted in the entity's store. Nothing here re-implements a
    measure or a read. Fails when the file is over the bound (a save is refused unless the wrap recomposes below
    it) or cannot be read or measured, and when the path exists but is not a regular file (it is never opened: a
    read of a FIFO would block the whole run). It says nothing when there is no store (``_check_store`` names
    that) or no continuity file yet.
    """
    store = install / ".levain" / "memory.db"
    continuity = install / ".levain" / "memory.continuity.md"
    name = "continuity headroom"
    if not store.is_file():
        return []
    if continuity.is_symlink() or continuity.exists():
        if not continuity.is_file():
            return [CheckResult(name, False, ".levain/memory.continuity.md exists but is not a regular file",
                                "Replace it with the entity's continuity file (restore it from a backup or git).")]
    else:
        return []
    try:
        from anneal_memory import Store
        from anneal_memory.durable import section_chars
        from anneal_memory.schema import hard_max_chars

        with Store(str(store), read_only=True) as st:
            schema = st.section_schema_for_wrap()
            text = st.load_continuity()
        if text is None:
            return []
        bound = hard_max_chars(schema)
        used = len(text) - section_chars(text, schema)
        cuttable = ", ".join(s["heading"] for s in schema if s["role"] in ("live-state", "narrative"))
    except Exception as e:  # noqa: BLE001 - a doctor check reports, it never crashes the run
        return [CheckResult(name, False, f"could not measure ({type(e).__name__}: {e})",
                            "Either anneal-memory is older than 0.9.33, the store needs a migration, or the "
                            "continuity file is not readable UTF-8; the error names which.")]
    room = bound - used
    if room < 0:
        return [CheckResult(
            name, False,
            f"{used} of {bound} characters: {-room} over anneal's hard maximum, so a wrap's save is refused "
            "unless it recomposes the continuity below it",
            "Cut fetchable detail" + (f" ({cuttable})" if cuttable else "") + " from .levain/memory.continuity.md; "
            "a durable section is outside this count.")]
    tight = " (under 10% left)" if room * 10 < bound else ""
    return [CheckResult(name, True, f"{used} of {bound} characters, {room} left{tight}")]


def _check_compat_set(install: Path) -> list[CheckResult]:
    """Compatibility-manifest drift: is the installed version SET (anneal +
    schema + acked migrations) at the known-good this levain release declares?

    Folds the manifest's verify into doctor (the natural home — doctor already
    checks the anneal CLI + the store). Each drift axis becomes a check; only an
    in_sync axis is green (honesty floor — `unknown` is reported, never passed).
    Gated on the store existing: a missing store is already reported by
    `_check_store`, so we don't double-fail it here."""
    from levain import manifest

    store = install / ".levain" / "memory.db"
    if not store.is_file():
        return []

    anneal_path = manifest.resolve_anneal_bin()
    declared = manifest.declared_set()
    installed = manifest.discover_installed_set(store, anneal_path)
    lock, lock_status = manifest.read_lock_status(install)
    drift = manifest.compute_drift(declared, installed, lock)

    results: list[CheckResult] = []
    # A CORRUPT lock (the file exists but is unreadable) is an UNKNOWN provenance
    # state worth a loud FAIL; an ABSENT lock is benign (a pre-manifest install),
    # so it gets no check line.
    if lock_status == "corrupt":
        results.append(CheckResult(
            "compat: lock", False,
            "the recorded set (.levain/manifest.json) exists but is unreadable/corrupt",
            "Run `levain update` to re-record a verified compose.",
        ))
    # `pending` (unreviewed migration proposals) and `ahead` (anneal NEWER than
    # this release's known-good — pip allowed it, the install works, and `update`
    # won't downgrade it) are ADVISORIES, not version-SET failures. A fresh
    # `levain init` legitimately has pending proposals, and an operator who runs
    # `pip install -U anneal-memory` within the pin is `ahead` — failing doctor on
    # either would false-alarm a healthy install. Report loudly, green.
    advisory = {"pending", "ahead"}
    levain_upgraded = (
        lock is not None
        and bool(lock.levain)
        and manifest._cmp(installed.levain, lock.levain) > 0
    )
    installed_anneal: str | None = getattr(installed, "anneal", None)
    locked_anneal: str | None = getattr(lock, "anneal", None) if lock is not None else None
    for v in drift.verdicts:
        if v.status in advisory:
            results.append(CheckResult(
                f"compat: {v.axis}", True,
                f"{v.detail} — advisory (run `levain update`); not a set failure",
            ))
        else:
            # The routine upgrade state (`levain update` pending): the running levain is
            # NEWER than the set last composed, and the anneal lock moved with that same
            # upgrade (a new levain raises its anneal floor, so pip moves anneal too; L2
            # HIGH, reproduced). A DOWNGRADE is not routine, so neither axis is pending.
            pending_axis = levain_upgraded and v.status == "drift" and (
                v.axis == "levain"
                or (
                    # Only an anneal that moved FORWARD with the upgrade; an anneal downgrade
                    # alongside it is not routine (complement MED).
                    v.axis == "anneal-lock"
                    and installed_anneal is not None
                    and locked_anneal is not None
                    and manifest._cmp(installed_anneal, locked_anneal) > 0
                )
            )
            results.append(
                CheckResult(
                    f"compat: {v.axis}", v.status == "in_sync", v.detail, v.hint,
                    upgrade_pending=pending_axis,
                )
            )
    # Release-gate: the reviewed known-good constant vs the actual pip floor. This
    # is a RELEASE-INTEGRITY check, not an operator-actionable one — a drift
    # (mis-cut wheel: KNOWN_GOOD != the dependency pin) or unknown (unreadable dep
    # metadata) is something the OPERATOR cannot fix, and `doctor`'s exit code
    # composes with their shell pipelines. So surface it loudly but NEVER fail
    # their doctor on it; CI / a release script reads `pip_floor_verdict()` to gate.
    pin = manifest.pip_floor_verdict()
    pin_detail = (
        pin.detail if pin.status == "in_sync"
        else f"{pin.detail} — advisory (release integrity; not operator-actionable)"
    )
    results.append(CheckResult(f"compat: {pin.axis}", True, pin_detail))
    return results


def _python_resolvable(token: str) -> bool:
    """True if `token` names a usable interpreter (absolute file OR on PATH).

    For absolute paths, requires both presence AND executable bit — a
    non-+x interpreter would PermissionError at hook-fire time.
    """
    if not token:
        return False
    p = Path(token)
    if p.is_absolute():
        return p.is_file() and os.access(p, os.X_OK)
    return shutil.which(token) is not None


def _is_this_environment(python: str) -> bool:
    """Whether ``python`` is the interpreter of the environment this levain runs in: its own path, or an
    interpreter inside ``sys.prefix``. Decided from paths only, so doctor never runs an interpreter a hook file
    names (a hook file is what doctor inspects)."""
    p = os.path.abspath(python)
    if p == os.path.abspath(sys.executable):
        return True
    prefix = os.path.abspath(sys.prefix)
    return os.path.dirname(os.path.dirname(p)) == prefix


def _extract_command_python(entries: list[dict]) -> str | None:
    """Extract the python interpreter token from the first hook command."""
    tokens = _first_command_tokens(entries)
    return tokens[0] if tokens else None


def _first_command_tokens(entries: list[dict]) -> list[str] | None:
    """Tokenize the first hook command via shlex; return None on parse error."""
    for entry in entries:
        for hook in entry.get("hooks", []):
            cmd = hook.get("command")
            if not cmd:
                continue
            try:
                return shlex.split(cmd)
            except ValueError:
                return None
    return None


def _hook_command_targets(
    entries: list[dict],
    install: Path,
    expected_script: str,
) -> bool:
    """True iff the first hook command's script-path token resolves to the
    expected script INSIDE this install. Catches substring-prefix false matches
    (`/tmp/levain` vs `/tmp/levain-test`) AND foreign hooks wired under the
    same event but pointing at unrelated scripts.
    """
    tokens = _first_command_tokens(entries)
    if not tokens or len(tokens) < 2:
        return False
    expected = (install / "activation" / "hooks" / expected_script).resolve()
    return any(_hook_token_path(tok, install) == expected for tok in tokens[1:])


def _hook_token_path(tok: str, install: Path) -> Path | None:
    """A hook command token as the resolved path it names, reading the harness placeholder
    ``${CLAUDE_PROJECT_DIR}`` (or ``$CLAUDE_PROJECT_DIR``) as this install; None when it cannot be resolved."""
    candidate = tok.replace("${CLAUDE_PROJECT_DIR}", str(install)).replace("$CLAUDE_PROJECT_DIR", str(install))
    try:
        return Path(candidate).resolve()
    except (OSError, RuntimeError, ValueError):  # RuntimeError: a symlink loop before Python 3.13 (L3 r1)
        return None


# The hook scripts levain ships in activation/hooks/ (the codex consent hook too): what a stale levain entry names.
# Pinned against the package's templates/activation/hooks/ by tests/test_subfolder_hooks.py.
LEVAIN_HOOK_SCRIPTS = frozenset({
    "session_start.py", "user_prompt_submit.py", "post_tool_use.py", "stop.py", "automemory_mirror.py",
    "consent_claude_code.py", "consent_codex.py"})


def stale_levain_handler(h: Any) -> str | None:
    """The missing script a stale levain handler names, else None: a command with an absolute literal token
    ``<anything>/activation/hooks/<one of LEVAIN_HOOK_SCRIPTS>`` that does not exist on disk. What a moved or
    copied workspace leaves behind in a settings file (its commands name scripts by absolute path); `levain update`
    and `init --force` replace it as levain's own. A ``${CLAUDE_PROJECT_DIR}`` token is never stale (it follows the
    start folder)."""
    if not isinstance(h, dict) or not isinstance(h.get("command"), str):
        return None
    try:
        tokens = shlex.split(h["command"])
    except ValueError:
        return None
    for tok in tokens[1:]:
        if "CLAUDE_PROJECT_DIR" in tok or not os.path.isabs(tok):
            continue
        p = Path(tok)
        if (p.name in LEVAIN_HOOK_SCRIPTS and p.parent.name == "hooks" and p.parent.parent.name == "activation"
                and not os.path.lexists(tok)):
            return tok
    return None


def levain_hook_handler(h: Any, install: Path) -> bool:
    """Whether the hook handler ``h`` is one levain wired for this install: a command whose token after the
    interpreter is an absolute path (after :func:`_hook_token_path`'s placeholder reading) to a ``.py`` directly
    in ``<install>/activation/hooks/``. The reading :func:`_hook_command_targets` makes for one named script,
    widened to any of levain's scripts, so the consent hook's ``-m levain.hookrun <script>`` form counts too.
    Anything else (another install's script, a team hook, the user's own) is not levain's."""
    if not isinstance(h, dict) or not isinstance(h.get("command"), str):
        return False
    try:
        tokens = shlex.split(h["command"])
    except ValueError:
        return False
    hooks_dir = (install / "activation" / "hooks").resolve()
    for tok in tokens[1:]:
        if not os.path.isabs(tok.replace("${CLAUDE_PROJECT_DIR}", "/").replace("$CLAUDE_PROJECT_DIR", "/")):
            continue
        p = _hook_token_path(tok, install)
        if p is not None and p.suffix == ".py" and p.parent == hooks_dir:
            return True
    return False


def _hooks_wired_anywhere(hooks: Any, event: str, install: Path, script: str) -> bool:
    """Whether ANY hook in ANY matcherless group under ``event`` runs this install's ``script`` (a group can
    carry another levain hook first, as PostToolUse carries the auto-memory mirror)."""
    entries = hooks.get(event, []) if isinstance(hooks, dict) else []
    for group in (entries if isinstance(entries, list) else []):
        # a group with a tool matcher fires for some tools only, so it does not record every event (L3 r1 codex)
        if not isinstance(group, dict) or group.get("matcher") not in (None, "", "*"):
            continue
        hs = group.get("hooks")
        for h in (hs if isinstance(hs, list) else []):
            if not isinstance(h, dict) or not isinstance(h.get("command"), str):
                continue
            try:  # a hand-edited command (a NUL byte, say) is "not wired", never a doctor crash (L1, run)
                if _hook_command_targets([{"hooks": [h]}], install, script):
                    return True
            except (ValueError, OSError):
                continue
    return False


def _check_transcript_hooks(install: Path, hooks: Any, events: Sequence[str], agent: str) -> list[CheckResult]:
    """The session transcript hooks (Stop, PostToolUse, and Claude Code's PostToolUseFailure). Advisory only:
    they record what a session did for the cockpit's transcript and change nothing the agent does, so an install
    from before they existed works and simply records no transcript lines."""
    script = {"Stop": "stop.py", "PostToolUse": "post_tool_use.py", "PostToolUseFailure": "post_tool_use.py"}
    missing = [e for e in events if not _hooks_wired_anywhere(hooks, e, install, script[e])]
    name = f"{agent} transcript hooks"
    if not missing:
        return [CheckResult(name, True, f"{', '.join(events)} wired to this install")]
    return [CheckResult(name, True, f"not wired: {', '.join(missing)}, so the cockpit's session transcript records "
                        f"nothing for those events", "Run `levain update` to add them.", warn=True)]


# how an operator allows agent config and hook edits when no key can sign them (the Q10 floor), for both agents
UNCONFINED_HOW = ("To allow them, enrol a key that can sign T3 (a laptop-presence key) in this install's cockpit, "
                  "so each one is asked, or launch the session with LEVAIN_UNCONFINED=1 from your own shell "
                  "(a settings file that sets it is ignored).")


def _hooks_off_files(install: Path, adapter: str, *, home: Path | None = None,
                     codex_home: Path | None = None) -> list[Path]:
    """The workspace or user config files that turn the agent's hooks off, every hook levain wires with them, the
    consent gate and its floor included (L3 r1 G5): for Claude Code, ``disableAllHooks: true`` in any settings file
    a session started in the workspace loads (the consent adapter's
    :func:`~levain.consent.adapters.claude_code._settings_files`, its JSON ones: the user's settings, the
    machine-wide ones, and ``.claude/settings.json`` / ``.claude/settings.local.json`` in the workspace and every
    ancestor directory, L3 r2); for Codex, ``[features] hooks = false`` (or the older ``codex_hooks``) in the
    workspace's ``.codex/config.toml`` or ``CODEX_HOME``'s. A file that is missing, unreadable or does not parse is
    not listed here (the other rows report a broken settings file)."""
    home = home if home is not None else Path.home()
    found: list[Path] = []
    if adapter == "claude-code":
        from levain.consent.adapters.claude_code import _settings_files

        files = [p for kind, p in _settings_files(str(install), home, None, environ=os.environ) if kind == "json"]
        for f in dict.fromkeys(files):
            try:
                data = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("disableAllHooks") is True:
                found.append(f)
        return found
    if codex_home is None:
        codex_home = Path(os.environ.get("CODEX_HOME") or home / ".codex")
    for f in dict.fromkeys([install / ".codex" / "config.toml", codex_home / "config.toml"]):
        try:
            data = tomllib.loads(f.read_text())
        except (OSError, ValueError):
            continue
        features = data.get("features")
        if isinstance(features, dict) and any(features.get(k) is False for k in ("hooks", "codex_hooks")):
            found.append(f)
    return found


def _check_hooks_off(install: Path, adapter: str, *, home: Path | None = None,
                     codex_home: Path | None = None) -> list[CheckResult]:
    """A FAIL naming each config file that turns the agent's hooks off (:func:`_hooks_off_files`): no levain hook
    runs there, so the consent gate's floor cannot refuse anything."""
    label = "Claude Code" if adapter == "claude-code" else "Codex"
    files = _hooks_off_files(install, adapter, home=home, codex_home=codex_home)
    if not files:
        return []
    how = ("`disableAllHooks: true`" if adapter == "claude-code" else "`[features] hooks = false`")
    return [CheckResult(f"{label} hooks", False,
                        f"turned off by {how} in {', '.join(str(f) for f in files)}: no levain hook runs, so the "
                        "consent gate cannot refuse an edit of agent config or hooks in this workspace",
                        "Remove that setting (a repository you cloned may have set it).")]


def _check_consent_wiring(install: Path, hooks: Any, adapter: str) -> list[CheckResult]:
    """The consent hook `levain init` wires (S1436): whether the PermissionRequest entry runs this install's
    script, the away mode the hook will read, and which cockpit its grants and signed answers verify against.
    Advisory only (at most a WARN): with no hook, no settings or no cockpit the agent's own dialog answers."""
    from levain.consent.adapters import claude_code, codex
    from levain.consent.adapters.claude_code import CLEANUP_RESERVE, DEFAULT_MAX_HOLD
    from levain.consent.policy import AGENT_CONFIG_TIER
    from levain.install import CONSENT_HOOK_SCRIPTS, PENDING_REL, _consent_queue_dir, consent_cockpit_root

    from levain.install import persisted_consent_hook

    label = "Claude Code" if adapter == "claude-code" else "Codex"
    mod = claude_code if adapter == "claude-code" else codex
    out: list[CheckResult] = []
    if persisted_consent_hook(install) == "off":
        if _hooks_wired_anywhere(hooks, "PermissionRequest", install, CONSENT_HOOK_SCRIPTS[adapter]):
            return [CheckResult(f"{label} consent hook", True,
                                "off in .levain/config.json, but a PermissionRequest entry still runs this "
                                "install's consent script", "Run `levain update` to rewrite the hook file.",
                                warn=True)]
        return [CheckResult(f"{label} consent hook", True,
                            "off (.levain/config.json): the agent's own dialog answers every permission")]
    qdir = _consent_queue_dir()
    settings = mod.load_settings(qdir)
    if _hooks_wired_anywhere(hooks, "PermissionRequest", install, CONSENT_HOOK_SCRIPTS[adapter]):
        if adapter == "claude-code" and claude_code.nothing_can_decide(settings, tier=AGENT_CONFIG_TIER):
            # the Q10 floor: the PreToolUse gate refuses these outright where no dialog asks (L2-7)
            out.append(CheckResult(f"{label} consent hook", True,
                                   "wired to this install, but no active key can sign an agent config change (T3), "
                                   "so an agent config or hook edit is refused outright outside default mode, "
                                   "and in it left to Claude Code's own dialog, which asks for "
                                   "the paths Claude Code protects (.claude/, .git/, .mcp.json)",
                                   UNCONFINED_HOW, warn=True))
        else:
            out.append(CheckResult(f"{label} consent hook", True, "wired to this install"))
    else:
        from levain.host_guard import cc_levain_hooks_file

        staged = install.joinpath(*PENDING_REL, *((".claude", cc_levain_hooks_file(install).name)
                                                  if adapter == "claude-code" else ("codex-home", "hooks.json")))
        hint = (f"`levain update` staged levain's version at {staged}: merge its PermissionRequest entry."
                if staged.is_file() else
                "`levain update` wires it unless the file holds your own edit, which it keeps: then add the entry back, "
                f"or record the choice with `levain init --path {install} --force --consent-hook off`.")
        out.append(CheckResult(f"{label} consent hook", True,
                               "not wired: the agent's own dialog answers every permission; no grant or away "
                               "answer can reach it", hint, warn=True))
    try:
        mark, unread = claude_code.away_mark_of(qdir, settings, mod.AGENT), None
    except Exception as e:  # noqa: BLE001 - a store the hook cannot open: say what that costs
        mark, unread = None, e
    if unread is not None:
        out.append(CheckResult(f"{label} away mode", True,
                               f"{claude_code.store_unreadable(qdir)} ({type(unread).__name__})",
                               "Repair the store, or move it aside: a store moved aside reads as no away mark, "
                               "which ends a standing `levain consent away on` too.", warn=True))
    elif mark is not None and not mark.left:
        detail = ("on (signed to leave): `levain consent away on` set it, and the hook holds until a signed "
                  "`levain consent away off --signing-key <key>` leaves it, whatever the settings file says")
        if mark.leave_refusal is not None:
            detail += (f"; a leave signed by {mark.leave_by} does not verify ({mark.leave_refusal}), so the mark "
                       "stands")
        out.append(CheckResult(f"{label} away mode", True, detail, warn=mark.leave_refusal is not None))
    elif settings.explicit_off:
        when = " (set before auto-away existed)" if settings.legacy else ""
        out.append(CheckResult(f"{label} away mode", True,
                               f"off by your word{when}: an unanswered request goes straight to {label}'s own "
                               "dialog; `levain consent away auto` lets input idle decide"))
    elif not settings.away_mode:
        out.append(CheckResult(f"{label} away mode", True,
                               f"follows input idle: after {settings.away_idle_seconds:g}s of no keyboard, mouse "
                               f"or trackpad input (macOS; no idle source elsewhere) a request is held for a "
                               f"signed answer until you are back, otherwise it goes straight to {label}'s own "
                               "dialog"))
    else:
        hold = int(min(settings.away_hold_seconds, max(0.0, DEFAULT_MAX_HOLD - CLEANUP_RESERVE)))
        if adapter == "claude-code":
            out.append(CheckResult(f"{label} away mode", True,
                                   f"on, hold {hold}s: Claude Code shows its dialog during the hold; an expired "
                                   "hold leaves it standing"))
        else:
            out.append(CheckResult(f"{label} away mode", True,
                                   f"on, hold {hold}s: Codex shows NO dialog during the hold (\"Running hook\", "
                                   f"up to {hold}s), then its own dialog",
                                   f"Away mode is your opt-in, set in {qdir / mod.SETTINGS_NAME}.", warn=True))
    ours = os.path.realpath(consent_cockpit_root(install))
    name = f"{label} consent authority"
    root = settings.cockpit_data_root
    if root is None:
        out.append(CheckResult(name, True, f"{qdir / mod.SETTINGS_NAME} names no cockpit, so no grant or signed "
                                           "answer can answer the hook",
                               f"Add \"cockpit_data_root\": \"{ours}\" to it to verify against this install's.",
                               warn=True))
    elif root != ours and not (Path(root) / "cockpit.db").is_file() and not (Path(ours) / "cockpit.db").is_file():
        out.append(CheckResult(name, True, f"{qdir / mod.SETTINGS_NAME} names another install's cockpit ({root}); "
                                           "neither it nor this install's holds a cockpit database, so nothing is "
                                           "answered by a grant and the agent's own dialog answers"))
    elif root != ours:
        out.append(CheckResult(name, True, f"grants verify against {root} (queue {qdir}), not this install's "
                                           f"{ours}",
                               "One cockpit per agent per machine: that file is kept as it is. To use this "
                               f"install's, set \"cockpit_data_root\" in it to \"{ours}\".", warn=True))
    elif not (Path(root) / "cockpit.db").is_file():
        out.append(CheckResult(name, True, f"this install's cockpit {root} (queue {qdir}); no cockpit.db there yet, "
                                           "so no grant can answer until a cockpit holds enrolled keys"))
    else:
        out.append(CheckResult(name, True, f"this install's cockpit {root} (queue {qdir})"))
    return out


def _newest_stamp(db: Path, sql: str, args: tuple[Any, ...]) -> str | None:
    """One ``MAX(...)`` read from a consent store opened read-only (never created, never migrated); None when the
    file is absent, unreadable or empty."""
    import sqlite3

    if not db.is_file():
        return None
    try:
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute(sql, args).fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError):
        return None
    return row[0] if row and isinstance(row[0], str) else None


def _check_consent_ledger(install: Path, hooks: Any, adapter: str) -> list[CheckResult]:
    """The authority ledger's writer (Q15): whether this install's PostToolUse hook files ledger rows (its scripts
    call ``consent_ledger``), and the age of the agent's newest row. A silent ledger is not a healthy one: WARN when
    a request the consent hooks answered (allowed, denied or left undecided) was filed after every ledger row of that
    agent, since the gate files a row for each call it answers and a call that ran files one too (a dialog request
    a signed deny answered has no row, so it warns until that session's next call). Advisory only."""
    from levain.consent.ledger import DB_NAME
    from levain.consent.queue import DB_NAME as QUEUE_DB
    from levain.install import CONSENT_AGENTS, _consent_queue_dir

    label = "Claude Code" if adapter == "claude-code" else "Codex"
    name = f"{label} consent ledger"
    agent = CONSENT_AGENTS[adapter]
    scripts = install / "activation" / "hooks"

    def calls_it(script: str) -> bool:
        try:
            return "consent_ledger" in (scripts / script).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False

    if not (_hooks_wired_anywhere(hooks, "PostToolUse", install, "post_tool_use.py")
            and calls_it("post_tool_use.py") and calls_it("_levain_hook.py")):
        return [CheckResult(name, True, "writer not wired: this install's PostToolUse hook files no ledger rows, so "
                            "`levain consent ledger` shows no calls", "Run `levain update` to rewrite the hooks.",
                            warn=True)]
    qdir = _consent_queue_dir()
    last_row = _newest_stamp(qdir / DB_NAME, "SELECT MAX(received_at) FROM calls WHERE agent = ?", (agent,))
    last_answer = _newest_stamp(qdir / QUEUE_DB, "SELECT MAX(received_at) FROM requests WHERE agent = ? AND state IN "
                                "('allowed', 'denied', 'undecided')", (agent,))
    seen = f"last row {_age(last_row)}" if last_row else "no row yet"
    if last_answer is not None and (last_row is None or last_row < last_answer):
        return [CheckResult(name, True, f"writer wired (hooks reference it), {seen}, but a request filed "
                            f"{_age(last_answer)} and answered has no row after it: the writer is failing",
                            "Each failed write prints one `levain ledger:` line on the hook's stderr; "
                            f"check {qdir} is a 0700 directory you own.", warn=True)]
    return [CheckResult(name, True, f"writer wired (hooks reference it), {seen}")]


def _age(stamp: str) -> str:
    """``3m ago`` / ``2h ago`` / ``4d ago`` for a UTC stamp; the stamp itself when it does not parse."""
    from datetime import datetime, timezone

    from levain.consent.ledger import parse_when

    try:
        secs = (datetime.now(timezone.utc) - parse_when(stamp)).total_seconds()
    except ValueError:
        return stamp
    if secs < 120:
        return f"{max(0, int(secs))}s ago"
    if secs < 7200:
        return f"{int(secs // 60)}m ago"
    return f"{int(secs // 3600)}h ago" if secs < 172800 else f"{int(secs // 86400)}d ago"


_MIRROR_OFF = {"off", "0", "false", "no"}


def _check_automemory_mirror(install: Path, hooks: dict) -> list[CheckResult]:
    """The native auto-memory -> anneal mirror (0.5.10). Advisory only: an install that has
    not run `levain update` since 0.5.9 has no PostToolUse entry and still works."""
    name = "auto-memory mirror"
    env = os.environ.get("LEVAIN_AUTOMEMORY_MIRROR", "").strip().lower()
    if env:
        off = env in _MIRROR_OFF
    else:
        try:
            cfg = json.loads((install / ".levain" / "config.json").read_text(encoding="utf-8"))
            value = cfg.get("automemory_mirror", True) if isinstance(cfg, dict) else True
        except (OSError, ValueError):
            value = True
        off = (value is False or (type(value) is int and value == 0)
               or (isinstance(value, str) and value.strip().lower() in _MIRROR_OFF))
    if off:
        return [CheckResult(name, True, "off (LEVAIN_AUTOMEMORY_MIRROR or config.json)")]
    if (install / ".levain" / "automemory_mirror.lost").exists():
        return [CheckResult(name, True,
                            "STOPPED: its state was lost while the store holds mirror episodes "
                            "— advisory (read .levain/automemory_mirror.lost)")]
    last: dict = {}
    try:
        state = json.loads((install / ".levain" / "automemory_mirror.json").read_text(
            encoding="utf-8"))
        meta = state.get("__meta__") if isinstance(state, dict) else None
        if isinstance(meta, dict) and isinstance(meta.get("last_sweep"), dict):
            last = meta["last_sweep"]
    except (OSError, ValueError):
        pass
    problem = ""
    if (install / ".levain" / "automemory_mirror.pending").exists() and not last:
        problem = ("its first sweep has not yet been able to read the anneal store; notes "
                   "written meanwhile are mirrored once it can")
    elif last.get("store_unreadable"):
        problem = "its last sweep could not read the anneal store"
    elif isinstance(last.get("failed"), int) and last["failed"] > 0:
        problem = f"its last sweep left {last['failed']} note(s) unmirrored (retried next sweep)"
    elif isinstance(last.get("unavailable"), int) and last["unavailable"] > 0:
        problem = (f"{last['unavailable']} mirrored note(s) sit in a memory folder it could not "
                   f"list (missing or unmounted); none was retracted")
    entries = hooks.get("PostToolUse", []) if isinstance(hooks, dict) else []
    wired = any(
        _hook_command_targets([{"hooks": [h]}], install, "automemory_mirror.py")
        for group in entries if isinstance(group, dict)
        for h in group.get("hooks", []) if isinstance(h, dict)
    )
    if not wired:
        detail = ("PostToolUse hook not wired, so notes are mirrored only at session start "
                  "— advisory (run `levain update`)")
    else:
        detail = "PostToolUse → automemory_mirror.py wired"
    if problem:
        detail += f"; {problem} — advisory"
    return [CheckResult(name, True, detail)]


# ---------------------------------------------------------------------------
# Activation scope — the gate doctor never reported (Alex De Groodt, 2026-08-04)
#
# ⚠ THE FINDING IN ONE LINE: a healthy-looking doctor was compatible with the
# WHOLE activation layer being off. Every static check passed — hooks present,
# wired to this install, python resolvable, store open — because they all check
# FILES. What decides whether a hook emits anything is a RUNTIME gate,
# `_levain_hook.in_install_session()`, and doctor never mentioned it existed.
# Alex moved Levain into a global tool, worked in his own project directories,
# and lost posture / recency directives / spore surfacing / crystal recall / the
# wrap nudge in every session that mattered, for weeks, with doctor green.
#
# ⛔ DELIBERATELY NARROW, AND IT MUST STAY THAT WAY. This is additive: it reads
# `.levain/config.json` and the user-level harness settings and reports. It does
# NOT touch `_check_hook_freshness`, `_hook_body`, `PackProvenance`, or anything
# else the doctor redesign owns — that redesign is BLOCKED on a design defect
# (an init-time receipt cannot notice the package moved) and this finding is the
# same CLASS, so folding them together would ship the narrow fix behind a
# blocker. Recorded, not merged.
#
# Scope is a GAUGE in the GATE/GAUGE/FEED tiering, with one exception: the
# combination "install-scoped + wired ONLY at user level" is not a soft reading,
# it is a dark install, and it fails.

_SCOPE_CONFIG_REL = (".levain", "config.json")


def _configured_scope(install: Path) -> str:
    """The activation scope this install declares: ``"global"`` or ``"install"``.

    MUST agree with ``_levain_hook.configured_scope()`` — same key, same
    fail-closed rule (anything that is not exactly "global" means install
    scope). The hook cannot be imported here (it is a template, and it resolves
    its own install root from ``__file__``), so the contract is pinned by test
    instead: ``TestScopeAgreesWithHook``.

    ``LEVAIN_SCOPE`` is NOT read HERE — this function answers "what does the
    install DECLARE", which is a property of the install. The env override is
    handled by :func:`_env_scope_caveat` and reported alongside, rather than
    folded in.

    ⚠ AN EARLIER VERSION IGNORED THE ENV VAR ENTIRELY, on the reasoning that
    doctor's environment is not the session's. Two L3 seats independently
    called that wrong, and they were right: it holds for a per-invocation
    ``LEVAIN_SCOPE=global claude``, and breaks the moment an operator exports it
    from a shell profile — which is the natural way to make "always on" durable.
    Then doctor's env IS every session's env, and staying silent misreports in
    BOTH directions: green while the layer is dark (config says global, profile
    says install), and a hard FAIL on an install that is working fine (config
    says install, profile says global). The second is worse than saying nothing,
    because it sends an operator to fix something that is not broken.

    The fix is not to guess which kind of export it is. It is to stop asserting
    a scope that cannot be verified from here, and say so.
    """
    try:
        data = json.loads(install.joinpath(*_SCOPE_CONFIG_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "install"
    if not isinstance(data, dict):
        return "install"
    value = data.get("scope")
    if isinstance(value, str) and value.strip().lower() == "global":
        return "global"
    return "install"


def _user_level_wiring(install: Path) -> list[str]:
    """Settings files wiring THIS install's hooks from a place `levain init`
    did NOT put them — i.e. wiring that is an operator CHOICE.

    Returns display paths, or [] (absent/unreadable files included — this is a
    report, never a hard failure on someone else's config).

    ⛔ `~/.codex/hooks.json` IS DELIBERATELY NOT CHECKED, AND THIS IS THE WHOLE
    SUBTLETY OF THE CHECK. levain wires Codex through the machine-wide file only, so
    `levain init --adapter codex` writes the GLOBAL `~/.codex/hooks.json`
    itself (`install.py`, `hooks_target`). For a Codex install, user-level
    wiring is not a signal of anything — it is the only deployment there is,
    and every Codex install on earth would match it.

    ⚠ THIS EXACT MISTAKE ALREADY SHIPPED ONCE. levain 0.4.0 turned `doctor`
    PERMANENTLY RED for every Codex operator, because `_check_hook_freshness`
    compared every install against the Claude Code tree — and it went out in
    the release built to carry this same reporter's previous fixes. The first
    draft of THIS function repeated it, and a green suite did not catch that
    either; running `levain doctor` against a real Codex install did. The
    defect is never "does the code run", it is always "which tree is it
    pointed at".

    Claude Code is different in the way that matters: `levain init` writes
    `<install>/.claude/settings.json`, INSIDE the install. So hooks wired from
    `~/.claude/settings.json` are something the operator did on purpose, and
    combined with install scope that is precisely the dark configuration —
    invisible to every check that only reads the install's own settings.
    """
    found: list[str] = []
    # CLAUDE_CONFIG_DIR relocates the whole ~/.claude tree; checking only the
    # home path would read a directory Claude Code is not using and false-green
    # the dark configuration (codex L3).
    cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(cfg_dir).expanduser() if cfg_dir else Path.home() / ".claude"
    # ⚠ THE INSTALL'S OWN SETTINGS FILE IS NOT USER-LEVEL WIRING, EVEN WHEN IT
    # SITS AT THIS PATH. An install rooted at $HOME makes
    # <install>/.claude/settings.json and ~/.claude/settings.json the SAME FILE
    # — the one `levain init` wrote — so reading it as a deliberate operator
    # choice false-FAILs a perfectly correct install and makes `levain doctor`
    # exit nonzero. Same shape as the codex case below: confusing the file the
    # installer wrote with a signal of operator intent. This function's own
    # docstring already names the discriminator ("levain init writes
    # <install>/.claude/settings.json, INSIDE the install"); the code just never
    # checked that identity. Third instance of doctor going red for a whole
    # operator class, so the guard is an explicit identity test, not a path
    # heuristic. Found at review.
    try:
        own_settings = (install / ".claude" / "settings.json").resolve()
    except (OSError, ValueError):
        own_settings = None
    for path in [root / "settings.json"]:
        try:
            if not path.is_file():
                continue
            if own_settings is not None and path.resolve() == own_settings:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            continue
        for event, script in (("SessionStart", "session_start.py"),
                              ("UserPromptSubmit", "user_prompt_submit.py")):
            entries = hooks.get(event)
            if isinstance(entries, list) and _user_entry_targets(entries, install, script):
                found.append(str(path))
                break
    return found


def _user_entry_targets(entries: list, install: Path, expected_script: str) -> bool:
    """Does ANY command in these user-level entries run THIS install's hook?

    Deliberately NOT ``_hook_command_targets``, for two reasons codex found at
    L3, both of which make that function wrong for a user-level file:

    1. It inspects only the FIRST command it finds across all entries. A foreign
       hook registered ahead of Levain's would hide Levain's entirely, and the
       dark configuration would go unreported. Here every entry and every hook
       is scanned.
    2. It substitutes ``${CLAUDE_PROJECT_DIR}`` with this install's path. That
       is right for the install's OWN settings file, where the placeholder does
       resolve to the install — and wrong here, because in a user-level file the
       harness expands it to whatever project is open, so such a command never
       runs this install's hook at all. Treating it as wiring to this install
       would then send the operator to fix a scope that was never the problem.
       User-level wiring must name an absolute path.

    Fail-soft on shape: a foreign settings file may contain anything (``None``
    entries included — an unguarded ``entry.get`` there aborted the whole doctor
    run). Never raise; this is a report about someone else's config.
    """
    expected = (install / "activation" / "hooks" / expected_script).resolve()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks") or []:
            if not isinstance(hook, dict):
                continue
            cmd = hook.get("command")
            if not isinstance(cmd, str) or not cmd:
                continue
            try:
                tokens = shlex.split(cmd)
            except ValueError:
                continue
            for tok in tokens[1:]:
                if "${CLAUDE_PROJECT_DIR}" in tok or "$CLAUDE_PROJECT_DIR" in tok:
                    continue  # resolves elsewhere at user level — not this install
                try:
                    # EXPAND BEFORE RESOLVING. `Path(tok)` does no variable
                    # expansion and no `expanduser`, so `~/lev/.../hook.py` and
                    # `$HOME/lev/.../hook.py` — both of which a shell DOES
                    # expand, and the second of which levain's own
                    # settings.template.json spells inside double quotes —
                    # resolved to `<cwd>/~/lev/...` and `<cwd>/$HOME/lev/...`,
                    # never equalled `expected`, and the scan returned [].
                    # A miss in `_hook_command_targets` produces a wiring FAIL,
                    # which is fail-loud; a miss HERE produces the reassuring
                    # green on the one configuration this function exists to
                    # fail — `absence_of_signal_rendered_as_health` inside the
                    # instrument built to end it. The `${CLAUDE_PROJECT_DIR}`
                    # skip stays AHEAD of this, so a placeholder is still
                    # correctly not-this-install rather than being expanded to
                    # the empty string and matching something. Measured on all
                    # four spellings (Diogenes 2026-09-03).
                    if Path(os.path.expandvars(tok)).expanduser().resolve() == expected:
                        return True
                # ⛔ RuntimeError IS IN THIS CLAUSE BECAUSE OF `expanduser`, AND
                # LEAVING IT OUT REPRODUCED THE CLASS THIS FUNCTION WAS BUILT TO
                # CLOSE. `Path("~someuser/...").expanduser()` raises RuntimeError
                # ("Could not determine home directory") for a user with no passwd
                # entry — measured, not reasoned — and RuntimeError is neither
                # OSError nor ValueError. A foreign entry naming another operator's
                # home is an ordinary thing to find in a shared
                # ~/.claude/settings.json, so without this the expansion added a
                # fresh way to ABORT THE WHOLE DOCTOR RUN on somebody else's
                # config: exactly codex#7, which
                # `test_never_raises_on_a_malformed_foreign_config` exists to
                # prevent, reintroduced by the fix for the blindness right above.
                # The question that caught it, and it is checkable AT WRITE TIME:
                # would the condition this guard covers disable the guard?
                except (OSError, ValueError, RuntimeError):
                    continue
    return False


def _env_scope_caveat() -> str:
    """A note when ``LEVAIN_SCOPE`` is set in doctor's own environment.

    Returns "" when unset. Deliberately does NOT resolve the ambiguity: doctor
    cannot tell a one-off ``LEVAIN_SCOPE=global levain doctor`` from a line in
    the operator's shell profile, and those two mean opposite things about every
    other session. Naming the value and the ambiguity is the honest report.
    """
    raw = os.environ.get("LEVAIN_SCOPE", "").strip()
    if not raw:
        return ""
    effective = "global" if raw.lower() == "global" else "install"
    return (
        f" — NOTE: LEVAIN_SCOPE={raw} is set in this shell, which overrides the "
        f"config and resolves to {effective} scope. If that is exported "
        f"persistently (a shell profile), it applies to your real sessions too "
        f"and the config line above is not the whole story"
    )


def _check_activation_scope(install: Path) -> list[CheckResult]:
    """Report the runtime gate, and fail the one combination that is dark."""
    scope = _configured_scope(install)
    env_note = _env_scope_caveat()
    env_raw = os.environ.get("LEVAIN_SCOPE", "").strip().lower()

    if scope == "global":
        # Careful with this wording. Scope opens the RUNTIME GATE; it does not
        # make a harness INVOKE the hook. A Claude Code install wired only in
        # <install>/.claude/settings.json is still never invoked outside that
        # project, so "fires in every session" would be an overclaim doctor
        # cannot back (codex L3). Report the gate, and name the other half.
        detail = (
            "global — the gate is open, so the hooks no longer suppress "
            "themselves outside the install "
            f"(set in {Path(*_SCOPE_CONFIG_REL)}). They still only run where "
            "your harness actually invokes them, so wire them at the user level "
            "if you want them everywhere"
        ) + env_note
        return [CheckResult("activation scope", True, detail)]

    # Computed HERE, not above: the global branch returns without using it, and
    # scanning a stranger's settings file for a result nobody reads is how the
    # null-entry crash fired on installs that were never going to be flagged.
    user_wiring = _user_level_wiring(install)
    if user_wiring and env_raw != "global":
        # env_raw == "global" means the gate IS open for every session in this
        # environment, so the install is NOT dark and failing it would be a
        # false alarm sending the operator to fix working software.
        where = ", ".join(user_wiring)
        return [
            CheckResult(
                "activation scope",
                False,
                f"install-scoped, but this install's hooks are wired at the user "
                f"level ({where}) — they run for sessions outside the install and "
                f"then SUPPRESS THEMSELVES, so posture, recency directives, spore "
                f"surfacing, crystal recall and the wrap nudge are all silently off "
                f"in those sessions" + env_note,
                # A fresh `levain init` writes NO config.json, so this has to
                # say CREATE, and it has to show the literal JSON — an operator
                # told to "opt in to global scope" with no example has been
                # given a diagnosis, not a fix.
                'Opt in to global scope: create or edit '
                f'{Path(*_SCOPE_CONFIG_REL)} so it contains {{"scope": "global"}} '
                "(it survives `levain update`), or set LEVAIN_SCOPE=global for a "
                "single session. To keep the hooks scoped instead, remove the "
                "user-level wiring and use the install's own .claude/settings.json.",
            )
        ]

    detail = (
        f"install-scoped (the default) — hooks fire only for sessions whose "
        f"working directory is inside {install}"
    )
    from levain.install import effective_adapter
    # NOT `(install / "AGENTS.md").is_file()`: a `levain init --adapter
    # claude-code --force` over a former codex install leaves AGENTS.md behind,
    # and the operator would get a Codex diagnostic about hooks their install no
    # longer uses — a wrong-artifact reading inside the check written to stop
    # wrong-artifact readings (glm L3). `effective_adapter` is the shared
    # classifier doctor/verify/run already agree on.
    if effective_adapter(install) == "codex":
        # Codex wires ~/.codex/hooks.json globally by design, so this operator's
        # hooks DO run in every Codex session on the machine and then no-op
        # outside the install. Saying so is the point of the check: that is the
        # condition that looked like health while the activation layer was off.
        detail += (
            " — your Codex hooks are wired globally, so they run in every Codex "
            'session and stay silent outside it. Set {"scope": "global"} in '
            f"{Path(*_SCOPE_CONFIG_REL)} if you want the entity present everywhere"
        )
    return [CheckResult("activation scope", True, detail + env_note)]


def _check_openhands(install: Path) -> list[CheckResult]:
    """The hookless (openhands) adapter check — reached only when the marker says openhands
    AND no hosted-harness residue is present (the `hookless` gate in `run_doctor` already
    guarantees the latter via `_hosted_artifacts`). So this is a clean sovereign-entity PASS;
    the seed + store + runtime health are covered by the core checks, and a hookless entity
    has no hooks/MCP to verify. (A stray hook tree makes the install non-hookless at the gate,
    routing it to the normal tag-file detection / no-adapter FAIL instead.)"""
    return [
        CheckResult(
            "adapter",
            True,
            "openhands — sovereign entity (no hooks; run it with `levain run`)",
        )
    ]


# The Claude Code carrier's import of the live continuity, exactly as the template writes it.
CONTINUITY_IMPORT = "@.levain/memory.continuity.md"


def _imports_continuity(text: str) -> bool:
    """Whether doctor can CONFIRM the carrier imports the continuity: the import on a line of its
    own (``./`` allowed, fewer than four leading spaces and no tab), and no line before it that
    opens a code fence or leaves an HTML comment open.

    A bound, not a Markdown parser. Tracking fences and comments the way Claude Code does was
    defeated three ways in one round, each RUN against Claude Code (gemini, the 0.5.5 L3): a
    tab-indented import, a ```` fence around a ``` fence, a comment opened mid-line. Each was
    ignored by Claude Code and counted by doctor. So any fence before the import, or any comment
    left open on its line, means doctor cannot confirm it, and says so: for those shapes, a false
    warning rather than a false all-clear. The levain-written carrier has neither before the import."""
    wanted = {CONTINUITY_IMPORT, "@./" + CONTINUITY_IMPORT[1:]}
    for line in text.splitlines():
        stripped = line.strip()
        lead = line[: len(line) - len(line.lstrip())]
        if stripped in wanted and "\t" not in lead and len(lead) < 4:
            return True
        if stripped.startswith(("```", "~~~")):
            return False
        if "<!--" in line and "-->" not in line.rsplit("<!--", 1)[1]:
            return False
    return False


def _carrier_adapter(carrier: Path) -> str:
    return "claude-code" if carrier.name == "CLAUDE.md" else "codex"


def _ships_base_seed(name: str) -> bool:
    """Does the installed package carry ``name`` among its base seed templates?"""
    from levain.install import _templates_root

    try:
        with _templates_root() as root:
            return (root / "seed" / name).is_file()
    except (OSError, ValueError):
        return False


def _check_memory_load(install: Path, carrier: Path) -> list[CheckResult]:
    """One advisory row: how this adapter's carrier loads the memory wrap procedure. Never a failure: both
    modes are the operator's to choose, and the freshness check owns a carrier that disagrees with it."""
    from levain.install import persisted_memory_load

    from levain.install import recorded_memory_load
    from levain.packs import MEMORY_LOAD_CHOICES

    adapter = _carrier_adapter(carrier)
    mode = persisted_memory_load(install, adapter)
    default, by_adapter = recorded_memory_load(install)
    named = by_adapter.get(adapter, default)
    detail = "eager" if mode == "eager" else "on-demand (summary in the carrier)"
    if named is not None and named not in MEMORY_LOAD_CHOICES:
        # still eager and still advisory: eager loses nothing, but a typo should not read as a choice
        detail = f"eager (unreadable value {named!r} in .levain/config.json)"
    return [CheckResult("memory workflow", True, detail)]


def _check_carrier_freshness(install: Path, carrier: Path) -> list[CheckResult]:
    """Does the rendered adapter carrier still match the CURRENT seed classification?

    History: when this check was written (2026-08-01), adapter carriers were written at
    `init` only, so when a seed file changed class — as `spore_instructions.md` did,
    eager -> on-demand — every EXISTING install kept eagerly importing it, gaining none
    of the benefit, while every other check stayed green. Since the install-truth
    work, `levain update` refreshes the carrier (`install.refresh_adapter`), and the
    remedy below says so; an edited or unrecorded carrier is staged, not overwritten.

    That is the same shape as the defect being fixed: the operator cannot see it, and
    nothing tells them. Presence-only carrier checks cannot catch it (the file IS
    present; its CONTENT is stale). So this check reads the carrier and reports the
    drift with the exact remedy.

    Reported as a FAILING check rather than a note, deliberately: it is actionable and
    silently costs the operator the thing they upgraded for. Found by codex at L3 —
    the finding neither the Claude layers nor the open-weight seat produced, because
    it lives in the files this change never touched.
    """
    from levain.install import persisted_memory_load
    from levain.packs import seed_classes

    # The on-demand set is the install's own: memory_workflow.md is on-demand only where this
    # adapter's recorded memory load says so, and eager everywhere else.
    on_demand = seed_classes(persisted_memory_load(install, _carrier_adapter(carrier)))[1]

    try:
        text = carrier.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as e:
        return [CheckResult(f"{carrier.name} freshness", False, f"unreadable: {e}")]

    # An on-demand seed appearing as an EAGER entry — `@seed/<name>` (claude-code) or
    # a numbered "N. `seed/<name>`" read-list row (codex). The on-demand POINTER also
    # names the file, as ``- `seed/<name>` — summary``, so match the eager forms only.
    stale = [
        name
        for name in on_demand
        if f"@seed/{name}" in text or f". `seed/{name}`" in text
    ]
    # A Claude Code carrier written before 0.5.5 does not import the live continuity, so the
    # entity starts each session without its memory unless it thinks to read it (1003+21's
    # demo runs: memory read on 4-5 of 8 days). Same remedy: `levain update` rewrites it.
    problems: list[str] = []
    if stale:
        problems.append("eagerly loads seed file(s) now classified on-demand: " + ", ".join(stale))
    # The other direction: a file this install's mode says is loaded, with no import (eager) or no
    # pointer (on-demand). An operator-kept carrier that predates a file is the reachable case.
    from levain.install import carrier_load_gaps

    gaps = [n for n in carrier_load_gaps(text, _carrier_adapter(carrier),
                                         persisted_memory_load(install, _carrier_adapter(carrier)))
            if n not in stale]
    if gaps:
        problems.append("does not load seed file(s) this install's memory-load mode expects: "
                        + ", ".join(gaps)
                        + (" (the wrap procedure)" if "memory_workflow.md" in gaps else ""))
    if carrier.name == "CLAUDE.md" and not _imports_continuity(text):
        problems.append(f"does not import the living memory ({CONTINUITY_IMPORT}) where "
                        "doctor can confirm Claude Code reads it (a line of its own, above any "
                        "code block or open HTML comment), so a session may start without it")
    if problems:
        return [
            CheckResult(
                f"{carrier.name} freshness",
                False,
                "; ".join(problems),
                f"This install predates the change. Run `levain update --path "
                f"{install}`: it rewrites {carrier.name} if it is still the copy levain "
                f"wrote. If you edited it, or levain has no record of writing it, the "
                f"new version is staged under .levain/pending/ for you to merge.",
                upgrade_pending=True,
            )
        ]
    return [CheckResult(f"{carrier.name} freshness", True, "matches current seed classification")]


# `_hook_body` LIVES IN `install` NOW, next to the substitution it inverts.
# It was here, and its own docstring worried about exactly the coupling that
# separation caused: "the day a second key ships, this function silently
# UNDER-normalises". A normaliser that must stay in lockstep with a substituter
# belongs beside it, and `install`'s activation backup needs the same question
# answered (do these hooks differ in anything install did not itself write?).
# Imported under the old name so every call site and test here is unchanged.
from levain.install import hook_body as _hook_body  # noqa: E402


def _check_agent_hook_pins(install: Path, repin: bool = False) -> list[CheckResult]:
    """Ruling 3b item 2a. Pins the hook entries and hook scripts the agents run (Claude Code's
    settings hooks, its hooks dir, levain's activation scripts, Codex's hooks.json) and names what
    changed since the pin. Claude Code does not notice an edited hook SCRIPT (Q0 item 8), so the
    pin covers script contents. DETECTS only: the operator's settings are never edited; the pin
    file is levain's own `.levain/agent_hook_pins.json`."""
    from levain import host_guard as hg

    name = "agent hook pins"
    try:
        if repin:
            pins, owner = hg.repin_agent_hooks_with_owner(install)
            out = [CheckResult(name, True, f"re-pinned {len(pins)} component(s) as they are now"
                               + (f"; Codex's hooks.json was NOT pinned: it is levain's write for the install at "
                                  f"{owner}" if owner else ""), warn=owner is not None)]
            return out + _unpinned_commands_result(install)
        status, diffs = hg.check_agent_hook_pins(install)
    except hg.AgentPinError as exc:
        return [CheckResult(name, False, "refused to pin: " + "; ".join(exc.items),
                            "Fix or remove what is named (a placeholder is never pinned in its place), "
                            "then re-run doctor with --repin-hooks.")]
    except OSError as exc:
        return [CheckResult(name, False, f"could not read or record the pin ({exc})",
                            "Check that .levain/ is writable, then re-run doctor.")]
    extra = _unpinned_commands_result(install)
    if status == "recorded":
        return [CheckResult(name, True, "first run: pinned the hook entries and scripts as they are now "
                            "(trust on first use): " + (", ".join(diffs) or "nothing present yet"),
                            "If anything here is not yours, fix it before relying on this pin.",
                            warn=True)] + extra
    if status == "match" and diffs:
        # another install owns Codex's hooks.json: a WARN, never a clean OK, because what was not compared
        # rests on a receipt any process running as you can write (L1 on 6087443, reproduced)
        return [CheckResult(name, True, "hook entries and scripts match the pin; " + "; ".join(diffs),
                            "The wiring rows below say which install's hooks Codex runs now.", warn=True)] + extra
    if status == "match":
        return [CheckResult(name, True, "hook entries and scripts match the pin")] + extra
    return [CheckResult(name, False, "changed since pinned: " + "; ".join(diffs),
                        "If you made the change, run `levain doctor --path "
                        f"{shlex.quote(str(install))} --repin-hooks`. If you did not, "
                        "an agent or another program edited code that runs as you: read it first.")]


def _unpinned_commands_result(install: Path) -> list[CheckResult]:
    """A hook command whose executable is not a file doctor can hash is named: its code is not pinned."""
    from levain import host_guard as hg

    toks = hg.unpinned_hook_commands(install)
    if not toks:
        return []
    return [CheckResult("agent hook commands", True,
                        "not pinned (a hook command names code outside the install, or no file under it): " + ", ".join(toks),
                        "Point the hook command at a script under the install by absolute path "
                        "(or ${CLAUDE_PROJECT_DIR}) so its file is pinned.", warn=True)]


def _check_cc_sandbox(install: Path) -> list[CheckResult]:
    """Ruling 3b item 2b: warn when Claude Code's own sandbox is off for this workspace."""
    from levain import host_guard as hg
    from levain.install import effective_adapter

    if effective_adapter(install) == "codex":
        return []  # Claude Code's sandbox setting says nothing about a Codex install
    on, source = hg.cc_sandbox_state(install)
    read = ", ".join(hg.cc_sandbox_sources(install)) or "no settings file"
    seen = (f"[read: {read}; not visible to doctor: CLAUDE_CONFIG_DIR relocation, --settings, "
            "server-managed settings]")
    if on and hg.cc_sandbox_fails_closed(install):
        return [CheckResult("Claude Code sandbox", True,
                            f"configured enabled, failIfUnavailable true ({source}) {seen}")]
    if on:
        return [CheckResult("Claude Code sandbox", True,
                            f"configured enabled ({source}) but `sandbox.failIfUnavailable` is not true: "
                            f"if Claude Code cannot start its sandbox it runs Bash unconfined {seen}",
                            "Set `sandbox.failIfUnavailable: true` next to `sandbox.enabled` "
                            "(doctor never edits your settings).", warn=True)]
    return [CheckResult("Claude Code sandbox", True,
                        f"OFF for this workspace: {source}. Claude Code's own Bash runs unconfined here {seen}",
                        "Set `sandbox.enabled: true` in the workspace's .claude/settings.json "
                        "(doctor never edits your settings), or accept this as your own trust decision.",
                        warn=True)]


def _check_cc_messaging(install: Path) -> list[CheckResult]:
    """The messaging row, with the audit tail `levain messaging set` leaves ("(changed from X <at>)")."""
    import dataclasses

    from levain.install import messaging_change

    rows = _check_cc_messaging_posture(install)
    change = messaging_change(install)
    if change is not None and rows:
        rows[0] = dataclasses.replace(
            rows[0], detail=f"{rows[0].detail} (changed from {change['from']} {change['at']})")
    return rows


def _check_cc_messaging_posture(install: Path) -> list[CheckResult]:
    """Q9: Claude Code's cross-session messaging for this workspace, read with the vendor's precedence
    (:func:`levain.host_guard.cc_messaging_posture`), as the settings files say it. Governed = inbound `hold`/`refuse`
    and outbound `ask`/`deny` on `SendMessage`. Ungoverned under the install's recorded `isolate` (the default) or
    `unmanaged` choice is an OK row that names the gaps; ungoverned otherwise is a WARN, never a FAIL: it is the
    operator's own trust decision. Tamper is the hook pin's job (its `messaging` components)."""
    from levain import host_guard as hg
    from levain.install import effective_adapter

    if effective_adapter(install) == "codex":
        return []  # Codex documents no cross-session messaging
    p = hg.cc_messaging_posture(install)
    seen = (f"[per the settings files read: {', '.join(p.sources) or 'none'}; not visible to doctor: --settings, "
            "--setting-sources, CLAUDE_CONFIG_DIR relocation, settings.local.json, MDM-profile, server-managed and (on WSL) "
            "inherited Windows settings, plugin mods, "
            "the running Claude Code's version]")
    inbound = (f"inbound {p.inbound} ({p.inbound_source})" if p.inbound in ("hold", "refuse")
               else "inbound NOT governed: " + (f"accept ({p.inbound_source})" if p.inbound == "accept"
                                                 else "no value set, so Claude Code decides per message"))
    if p.outbound == "deny":
        outbound = (f"outbound off: SendMessage denied ({p.outbound_source})"
                    + ("" if p.list_denied else "; ListAgents not denied, so listing your sessions stays on"))
    elif p.outbound == "ask":
        outbound = (f"outbound asks: every SendMessage goes to the permission flow (levain's consent hook, any "
                    f"PermissionRequest hook you installed, or your dialog) ({p.outbound_source}), resuming a "
                    "subagent and messaging a teammate included; a standing grant on SendMessage for this workspace "
                    "is the quiet path" + ("; cross-machine sends ask (isolatePeerMachines)" if p.isolate else ""))
    else:
        outbound = "outbound NOT governed: no rule asks for or denies SendMessage"
    detail = f"{inbound}; {outbound} {seen}"
    from levain.install import recorded_messaging

    chosen = recorded_messaging(install)
    if chosen == "off" and not (p.inbound == "refuse" and p.outbound == "deny" and p.list_denied):
        return [CheckResult("Claude Code messaging", True, f"this install's recorded choice is `off`, and the files "
                            f"say less: {detail}",
                            "Run `levain messaging set off`, or record the posture "
                            "you want; doctor never edits your settings.", warn=True)]
    if p.inbound in ("hold", "refuse") and p.outbound in ("ask", "deny"):
        return [CheckResult("Claude Code messaging", True, detail)]
    if chosen == "unmanaged" or (chosen == "isolate" and p.isolate):
        # the install's own recorded choice (the default is `isolate`): not a warning, but the gaps said plainly
        gaps = "levain does not queue or record sends to your other sessions"
        if p.inbound is None:
            gaps += (", and holds no message arriving from one (with no value set, Claude Code decides per "
                     "message from the two sessions' permission modes)")
        elif p.inbound == "accept":
            gaps += ", and messages from your other sessions are delivered (accept)"
        return [CheckResult("Claude Code messaging", True,
                            f"not governed, by this install's messaging choice `{chosen}`: {gaps}. Claude Code never "
                            "counts a message from another session as your consent, so it cannot answer a permission "
                            "prompt; a delivered message can still lead Claude to act with the permissions the "
                            "session already has. To govern messaging, `levain messaging set governed` "
                            f"writes the hold and the ask. {inbound}; {outbound} {seen}")]
    return [CheckResult("Claude Code messaging", True, detail,
                        'Set `crossSessionInbound: "hold"` and `permissions.ask: ["SendMessage"]` in the '
                        "workspace's .claude/settings.json (`levain messaging set governed` writes both; "
                        "doctor never edits your settings), or accept this as your own trust decision.", warn=True)]


def _check_hook_freshness(install: Path) -> list[CheckResult]:
    """Do the installed HOOK SCRIPTS still match the ones this package ships?

    ⚠ A THIRD SURFACE IN THE SAME CLASS, and the class is the finding. When this check
    was written (2026-08-01), Levain copied artifacts into the operator's install at
    `init` and had NO upgrade path that refreshed them, so `pip install -U levain`
    upgraded the package and left the operator running the old copies; a hook fix is
    exactly what a bug report produces. Since the install-truth work, `levain update`
    refreshes hooks (`install.refresh_adapter`), and the pending-upgrade remedy below
    names it; this check is what tells the operator an update is owed.

    Concretely, 2026-08-01: Alex De Groodt reported that `install_root()` accepted
    any ANCESTOR as the install, which silently killed the whole activation layer
    for an install nested under the launch dir. The fix ships in the package — and
    would reach nobody who merely upgraded, because their `activation/hooks/` copy
    is whatever `init` wrote.

    ⚠ SCOPE — and the correction matters, because the first version of this
    docstring claimed a mechanism the code does not use. The operator-owned files
    (`posture.md`, `recency_directives.md` — the ones every operator is explicitly
    TOLD to tune) are excluded by DIRECTORY: they live in `activation/`, while this
    walks `activation/hooks/` only. The `*.py` filter is not what protects them; it
    only skips `__pycache__`. Mutation proved it — widening the glob to `*` changed
    nothing, because the markdown was never in scope to begin with.

    The distinction still governs the check: hooks are MACHINERY and must match the
    package, activation markdown is OPERATOR-OWNED and must never be compared.
    Flagging a tuned posture would fail an operator for doing what the product asks.
    Enforced by the directory boundary — so if this ever grows to walk
    `activation/` itself, the exclusion has to become explicit.

    ⚠ THE SHIPPED TREE IS SELECTED BY ADAPTER, THROUGH `install`'s OWN HELPER — NEVER BY
    SEARCH (0.4.1, after 0.4.0 shipped the opposite). 0.4.0 iterated
    `(templates/activation/hooks, adapters/codex/activation/hooks)` and broke on the
    first present dir, under a comment asserting "the first present tree is this
    install's lineage." That was FALSE: `templates/activation/hooks` ALWAYS ships, so
    the loop always broke on the claude-code tree and the codex branch was unreachable
    dead code. Every codex install had its hooks compared against the claude-code
    hooks — which legitimately differ — so `doctor` reported them stale and exited 1
    PERMANENTLY, and the remedy it printed (`levain init --force`) rewrote the same
    codex hooks and failed again. The check written to carry the `install_root` fix to
    operators destroyed its own signal for an entire adapter lineage.

    The repair is SUBTRACTION, not a corrected branch. The rule for which tree an
    adapter installs from already exists exactly once — `install._base_activation_root`,
    the same helper `init` renders from. (The defect was `the_comment_names_one_
    authority_the_code_leaves_a_second_alive`; the fix is `a_fix_that_deletes_mechanism_
    beats_one_that_adds_it`.)

    ⚠ **AND THIS PARAGRAPH USED TO END WITH A CLAIM THAT WAS FALSE WHEN WRITTEN**
    (corrected 2026-08-03): *"doctor CANNOT disagree with install about lineage, because
    there is no second copy of the rule left to drift."* It could, and it did. `init`
    renders from `order_activation_roots(templates_root, _base_activation_root(...),
    pack_dirs)` — base is only the FIRST layer — while this check called
    `_base_activation_root` alone. **Doctor took half the helper chain**, so an install
    built with a pack that overrides a hook had that hook installed correctly and then
    reported stale against a tree it never came from: EXIT 1 permanently, with
    `init --force` unable to clear it. Byte-for-byte the 0.4.0 shape above, one release
    later, for a different population, shipped INSIDE the fix for it —
    `guard_scoped_by_symptom_misses_the_class`.

    It was latent when found (no shipped pack overrides hooks), and the confidence is
    what made it dangerous: by this very release's own argument about `_base_seed_root`,
    *"the previous docstring's confidence is what kept anyone from looking."* Pack-owned
    hooks are now resolved against `.levain/manifest.json` — see the comment in the body.
    """
    from levain.install import _base_activation_root, _templates_root, effective_adapter

    hooks_dir = install / "activation" / "hooks"
    if not hooks_dir.is_dir():
        return []  # hookless adapter, or a layout failure the layout check owns

    # A hooks/ dir with no coherent adapter identity is an INCOHERENT install, and
    # `run_doctor`'s adapter-detection block owns it — it emits
    # "no adapter detected (no CLAUDE.md or AGENTS.md at install root)" and fails the
    # run. Comparing such an install against an arbitrarily chosen tree is exactly how
    # 0.4.0 manufactured a hook-freshness failure for a defect of a different name.
    #
    # ⚠ This comment said "the LAYOUT check owns that" until 2026-08-03, and the test
    # asserting this path repeated the misattribution in its docstring. It is wrong:
    # `_check_install_layout` never inspects CLAUDE.md or AGENTS.md at all — it checks
    # the install root, seed/, _ACTIVATION_FILES, the hooks dir, _HOOK_REQUIRED presence
    # and hook syntax. Verified by reproduction: delete CLAUDE.md from a healthy install
    # and LAYOUT prints [OK] on both its checks while the FAIL comes from the adapter
    # block. No behavioural bug — the operator IS protected — but it sent the next
    # reader to the wrong function, which is the same "a comment naming a world it is
    # not in" class this release shipped to fix.
    adapter = effective_adapter(install)
    if adapter is None:
        return []

    # ⚠ PACK-OWNED HOOKS ARE NOT BASE HOOKS, AND COMPARING THEM AGAINST BASE WAS THE
    # SAME DEFECT 0.4.1 SHIPPED TO FIX — one release later, for a different population.
    #
    # `init` renders the activation tree from the LAYERED stack (install.py:387-389,
    # `order_activation_roots(templates_root, _base_activation_root(...), pack_dirs)`)
    # and a pack layer WINS per relative path (documented install.py:1620: "a pack's
    # ``hooks/x.py`` replaces base's ``hooks/x.py``"). This check read only
    # `_base_activation_root`, so a hook a pack contributed was installed CORRECTLY and
    # then reported stale against a tree it never came from — doctor EXIT 1 forever,
    # and `init --force`, the remedy this check itself prints, could not clear it.
    #
    # That makes the docstring's central claim above false as written: doctor took HALF
    # the helper chain, so there WAS a second copy of the rule left to drift. install's
    # rule is order_activation_roots(base, pack_dirs); this one was base alone.
    #
    # THE DATA WAS ALREADY ON DISK. `.levain/manifest.json` records every pack layer's
    # contributed files with their source hashes, so a pack-owned hook is checked
    # against the hash the pack ACTUALLY shipped — still real drift detection, just
    # against the right authority. Hooks are copied verbatim (not rendered), so the
    # source hash and the installed bytes are directly comparable.
    #
    # ⚠ THE HASH COMPARISON DESCRIBED ABOVE IS NO LONGER WHAT THIS CHECK DOES, and the
    # paragraph that stood here explained why `_sha256_file` was imported rather than
    # reimplemented. `d7cee34` deleted the one call (`if _sha256_file(installed) !=
    # recorded:`) and replaced the raw-bytes/source-hash comparison with `_hook_body`
    # normalisation on BOTH sides (a pack hook carrying an install-time placeholder
    # hashed differently forever, and the remedy doctor printed could not clear it). The
    # import went dead in that commit and the comment justifying it did not — so the file
    # explained a mechanism it had stopped using. Both removed
    # 2026-09-04; the surrounding paragraph about WHICH authority is consulted (the
    # pack's own recorded source, not base) is still exactly right and stays.
    # ⚠ KEYED BY PATH RELATIVE TO `activation/hooks/`, **NEVER BY BASENAME** — and the loop
    # below iterates the UNION of these keys with base's. Those are two separate defects
    # with two separate fixes, and repairing either alone leaves the other alive.
    #
    # FOURTH OCCURRENCE of the shape 0.4.0 and 0.4.1 each shipped: a check operating in one
    # namespace over a tree composed in another. `install` builds the activation tree in
    # RELATIVE-PATH space as a UNION over layers; this check worked in BASENAME space over
    # BASE's file list alone. That produced two independent failures:
    #
    #   (a) a pack shipping `activation/hooks/sub/session_start.py` collapsed onto the key
    #       `session_start.py` and made doctor report a completely UNTOUCHED base hook
    #       stale — EXIT 1 permanently, which a perfect `init --force` could not clear
    #       (simulated three times: red, red, red). `install.py:1677` documents nested pack
    #       hooks as SUPPORTED, so this is not an invented shape.
    #   (b) a pack hook that base does NOT ship was installed, recorded in the manifest,
    #       and never compared to ANYTHING — its digest was read here and thrown away.
    #       Replacing one wholesale with `os.system(...)` still printed "hook scripts match
    #       the package".
    #
    # THE SUITE COULD NOT SEE EITHER — 299 tests passed patched and unpatched — because
    # every pack test used `hook: str = "session_start.py"`, the one shape in which
    # basename and relative path coincide. The cement laid after the THIRD occurrence
    # parametrised the ADAPTER axis and left the pack-path axis hardcoded, so the fourth
    # occurrence simply arrived on the unparametrised axis.
    pack_hooks: dict[str, str] = {}
    prov_source_by_rel: dict[str, str] = {}
    _PREFIX = "activation/hooks/"
    try:
        from levain.manifest import read_pack_locks_status

        # ⛔ `read_pack_locks_status`, NOT `read_pack_locks` — AND THE DIFFERENCE IS THE WHOLE
        # POINT. The thin wrapper returns `[]` for ABSENT and for CORRUPT alike, so a truncated
        # or partially-synced `.levain/manifest.json` silently turned pack-hook checking OFF
        # while this check went on reporting "hook scripts match the package". Tampering with an
        # installed pack hook was then invisible. `absence_of_signal_rendered_as_health`, inside
        # doctor, again — and the realistic path is not an attacker, it is a half-written file.
        #
        # ⛔⛔ THE JUSTIFICATION THAT STOOD HERE WAS FALSE: "the LOCK check owns lock integrity."
        # NO DOCTOR CHECK READS THE PACK AXIS. Measured: `read_pack_locks_status` had ZERO
        # callers in this file; its consumers are `update.py` and `reconcile.py`. Doctor's lock
        # check reads `manifest.read_lock_status` — a DIFFERENT function, covering the ENGINE
        # compat set, whose corrupt branch reports on `.levain/manifest.json` as a version
        # record and says nothing about pack provenance. The axis was unowned, and this comment
        # is what stopped anyone noticing.
        provs, pack_status = read_pack_locks_status(install)
        if pack_status == "corrupt":
            return [
                CheckResult(
                    "hook freshness",
                    False,
                    "pack provenance in .levain/manifest.json is unreadable, so pack-owned "
                    "hooks CANNOT be checked — a tampered or stale pack hook would not be "
                    "reported. This is a refusal to guess, not a hook failure.",
                    "Restore .levain/manifest.json (a truncated or partially-synced file is "
                    f"the usual cause), or re-onboard with `levain init --force --path {install}`. "
                    "Base-adapter hooks are still compared; only the pack layer is dark.",
                )
            ]
        for prov in provs:
            for rel, digest in prov.files.items():
                if rel.startswith(_PREFIX) and rel.endswith(".py"):
                    hook_rel = rel[len(_PREFIX):]
                    pack_hooks[hook_rel] = digest
                    # Which pack CONTRIBUTED this hook — needed to normalise against its source
                    # rather than against a hash of the un-substituted template.
                    prov_source_by_rel[hook_rel] = prov.source
    except Exception:
        # A genuinely ABSENT lock is the ordinary pack-less install: nothing to compare, and
        # base-only comparison is correct rather than degraded. Only `corrupt` is the silent
        # case, and it is now handled above rather than swallowed here.
        pack_hooks = {}

    stale: list[str] = []
    try:
        with _templates_root() as templates_root:
            shipped_root = _base_activation_root(adapter, templates_root) / "hooks"
            if not shipped_root.is_dir() and not pack_hooks:
                return []  # no base hooks tree and no pack hooks; nothing to compare
            # `rglob`, not `glob`: base is now read in the SAME relative-path space the
            # pack keys use, so a nested hook on either side lands on one key and the
            # union below is well defined.
            base_hooks: dict[str, Path] = {}
            if shipped_root.is_dir():
                base_hooks = {
                    str(p.relative_to(shipped_root)): p
                    for p in shipped_root.rglob("*.py")
                }
            for rel in sorted(set(base_hooks) | set(pack_hooks)):
                installed = hooks_dir / rel
                if not installed.is_file():
                    continue
                recorded = pack_hooks.get(rel)
                if recorded is not None:
                    # ⛔ A PACK HOOK'S INSTALLED COPY NEVER EQUALS ITS SOURCE HASH IF IT CARRIES A
                    # PLACEHOLDER, so the raw comparison that stood here reported a healthy install
                    # as permanently stale — and the `init --force` remedy this check prints could
                    # not clear it, because re-rendering substitutes the placeholder again.
                    # `_substitute_hook_placeholders` runs over the COMPOSED tree RECURSIVELY (its
                    # own docstring says so, naming nested pack hooks), while `files` records the
                    # PRE-substitution source hash. The base branch never had this problem because
                    # it normalises BOTH sides through `_hook_body`; the pack branch skipped that.
                    #
                    # ▶ SO NORMALISE BOTH SIDES HERE TOO, against the pack SOURCE, which is the
                    # same shape the base branch already proves out. `PackProvenance.rendered`
                    # would also solve it and is arguably the tidier home — its docstring names
                    # this exact class ("a render file's install copy never equals its template")
                    # — but it is populated only for seed/ renders, so it would repair FUTURE
                    # installs and leave every existing one red.
                    src_hook = Path(prov_source_by_rel[rel]) / _PREFIX.rstrip("/") / rel \
                        if rel in prov_source_by_rel else None
                    if src_hook is None or not src_hook.is_file():
                        # ⚠ SOURCE GONE: say nothing rather than guess. A pack whose source dir
                        # has moved cannot be compared either way, and REPORTING STALE HERE WOULD
                        # BE THE WRONG-TREE CLASS A FOURTH TIME — doctor going red for a whole
                        # operator class over a condition that is not their install's fault. The
                        # pack-drift surface owns "your pack source vanished"; this check does not.
                        continue
                    try:
                        if _hook_body(installed.read_text(encoding="utf-8")) != _hook_body(
                            src_hook.read_text(encoding="utf-8")
                        ):
                            stale.append(rel)
                    except (OSError, UnicodeError):
                        continue  # unreadable source — same reasoning as above
                    continue
                shipped = base_hooks[rel]
                if _hook_body(installed.read_text(encoding="utf-8")) != _hook_body(
                    shipped.read_text(encoding="utf-8")
                ):
                    stale.append(rel)
    except (OSError, UnicodeError, RuntimeError) as e:
        return [CheckResult("hook freshness", False, f"could not compare: {e}")]

    # ⚖ WHICH KIND OF DIFFERENT (L2 HIGH, reproduced 2026-09-13: a hook with an appended
    # `os.system(...)` exited 6 under "post-upgrade step"). The package comparison above
    # still decides stale-vs-fresh; the install receipt only splits a stale hook into
    # "exactly what init wrote, and the package has since moved" (pending) and "changed
    # since init wrote it" (an edit, never routine). With no receipt entry — every install
    # made before the receipt existed — the hook stays pending and the text says doctor
    # cannot tell which it is.
    #
    # ⚖ C6 AMENDED (2026-09-15): a CORRUPT or EMPTY receipt is not a pre-receipt
    # install and must not read as one. `_write_activation_receipt` (install.py) is the
    # sole writer and runs after the tree is in place; a failed write reads as absent, and
    # rollback restores the prior text — so a legitimate install cannot produce a corrupt
    # or empty receipt. Reading one as "no receipt yet" would downgrade an edited/tampered
    # hook (e.g. an appended `os.system(...)`) from broken (exit 1) to pending (exit 6),
    # exactly like the pack-provenance corrupt refusal ~90 lines up already fails closed.
    edited: list[str] = []
    unproven: list[str] = []
    if stale:
        from levain.install import _sha256_stream, activation_receipt_path, read_activation_receipt

        receipt_files, status = read_activation_receipt(install)
        if status in ("corrupt", "empty"):
            return [
                CheckResult(
                    "hook freshness",
                    False,
                    "installed hook script(s) differ from the package: "
                    + ", ".join(sorted(set(stale)))
                    + f" — the install receipt at {activation_receipt_path(install)} is "
                    f"unreadable ({status}), so doctor cannot tell an outdated hook from an "
                    f"edited one; a damaged receipt is not a pre-receipt install",
                    f"Re-render with `levain init --force --path {install}` — it RE-RUNS THE "
                    f"INTERVIEW and replaces the whole activation/ tree. Your store is kept, "
                    f"and the previous activation/ tree is moved whole into "
                    f".levain/backups/activation/ first, hooks and all (an old copy is removed "
                    f"only when it provably holds no edits).",
                )
            ]
        for rel in sorted(set(stale)):
            entry = (receipt_files or {}).get(f"hooks/{rel}")
            if entry is None:
                unproven.append(rel)
                continue
            try:
                if _sha256_stream(hooks_dir / rel) != entry["installed"]:
                    edited.append(rel)
            except OSError:
                edited.append(rel)
    if edited:
        return [
            CheckResult(
                "hook freshness",
                False,
                "installed hook script(s) changed since `levain init` wrote them: "
                + ", ".join(edited)
                + " — the hooks that run are these edited copies, not the package's"
                + (
                    "; also differing from the package, with no install receipt to tell "
                    "outdated from edited: " + ", ".join(unproven) if unproven else ""
                ),
                f"If you did not make these changes, treat them as tampering. To return to "
                f"the package's hooks run `levain init --force --path {install}` — it RE-RUNS "
                f"THE INTERVIEW and replaces the whole activation/ tree. Your store is kept, "
                f"and the previous activation/ tree is moved whole into "
                f".levain/backups/activation/ first, hooks and all (an old copy is removed "
                f"only when it provably holds no edits).",
            )
        ]
    if stale:
        return [
            CheckResult(
                "hook freshness",
                False,
                "installed hook script(s) differ from the package: "
                + ", ".join(sorted(set(stale)))
                + " — the hooks that run are these installed copies, not the package's"
                + (
                    " (no install receipt covers them, so doctor cannot tell an outdated "
                    "hook from an edited one)" if unproven else ""
                ),
                f"Your hooks predate the installed levain version. Run `levain update "
                f"--path {install}`: it replaces each hook levain wrote and you did not "
                f"edit, and a hook with no install record is replaced too, its previous "
                f"copy kept under .levain/backups/activation/. A hook you edited is "
                f"kept, and the package's version is staged under .levain/pending/ for "
                f"you to merge.",
                upgrade_pending=True,
            )
        ]
    return [CheckResult("hook freshness", True, "hook scripts match the package")]


def _check_context_surface(install: Path, carrier: Path) -> list[CheckResult]:
    """Report the EAGER context surface — the bytes this install loads into every
    single session — broken down by what those bytes ARE.

    ⚠ THIS CHECK EXISTS BECAUSE AN OPERATOR HAD TO GUESS. Alex De Groodt, the first
    installer outside this machine: *"I'm wondering if levain isn't filling too much
    context actually."* He was right — it was 48,451 bytes, 65% of it documentation
    about the machinery — and he could only feel it, because nothing in Levain ever
    reported the number. A harness that will not tell you what it loads makes its own
    operator the instrument. That is the root under F2, and it is the part a
    one-time trim does not fix.

    The health signal is the mechanism SHARE, never the byte total: an operator with
    a long, rich `world.md` has a large surface and a HEALTHY one — that is Levain
    working. An operator whose surface is mostly docs-about-the-substrate has the
    defect. Failing on raw size would punish precisely the right behaviour.

    Reported for the file set the carrier actually imports, read from the CARRIER on
    disk rather than recomputed from the roster, so it measures what this install
    really loads — including an install that predates a classification change, or one
    the operator hand-edited.
    """
    from levain.packs import MECHANISM_SHARE_WARN, seed_role

    try:
        text = carrier.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as e:
        return [CheckResult("context surface", False, f"carrier unreadable: {e}")]

    # Both eager forms: claude-code `@seed/<name>`, codex `N. \`seed/<name>\``.
    names: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("@seed/"):
            names.append(s[len("@seed/"):].strip())
        else:
            m = re.match(r"^\d+\.\s+`seed/([^`]+)`", s)
            if m:
                names.append(m.group(1))

    by_role: dict[str, int] = {}
    total = len(text.encode("utf-8"))  # the carrier itself loads too
    if _imports_continuity(text):
        try:   # the living memory loads every session too; absent until the first wrap
            by_role["living memory"] = (install / CONTINUITY_IMPORT[1:]).stat().st_size
            total += by_role["living memory"]
        except OSError:
            pass
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        f = install / "seed" / name
        try:
            size = f.stat().st_size
        except OSError:
            continue  # a dangling import is the freshness check's job, not this one
        total += size
        by_role[seed_role(name)] = by_role.get(seed_role(name), 0) + size

    if not seen:
        return [CheckResult("context surface", True, "no eager seed imports")]

    mechanism = by_role.get("mechanism", 0)
    share = mechanism / total if total else 0.0
    breakdown = " · ".join(
        f"{role} {by_role[role]:,}B" for role in sorted(by_role) if by_role[role]
    )
    detail = (
        f"{total:,} B loaded every session across {len(seen)} seed file(s) "
        f"+ carrier — {breakdown} ({share:.0%} mechanism)"
    )
    # ⚠ REPORTS, NEVER FAILS — and that split is deliberate, not softness.
    #
    # The first version FAILED above the threshold, which meant a fresh base install
    # failed `doctor` on day one for a ratio the operator cannot act on: `memory.md`
    # is OUR composition choice, and the remedy (moving its wrap procedure on-demand) is
    # a per-install setting an operator can make, but the default is ours. Failing them for our decision
    # points the signal at the wrong party — the same defect class this whole change
    # is about. Enforcement belongs where the actor is, so the BUDGET is enforced
    # repo-side in `scripts/check_seed_budget.py`, which fails OUR build.
    #
    # What the operator needs here is the NUMBER, every time, which is the exact
    # thing Alex did not have. So it always prints, and says plainly whose problem a
    # skewed ratio is.
    if share > MECHANISM_SHARE_WARN:
        detail += " — mostly machinery, which is a Levain composition issue, not yours"
    return [CheckResult("context surface", True, detail)]


def _cc_levain_hooks(install: Path, settings: dict) -> tuple[dict, str, list[CheckResult]]:
    """(hooks object to check, the file it came from, rows about where levain's hooks are). Where they belong is
    :func:`host_guard.cc_levain_hooks_file`'s answer: ``.claude/settings.local.json`` for a workspace that is the
    toplevel of a main checkout (a session started in any subfolder loads it), else ``.claude/settings.json`` with
    one row saying a subfolder start is not covered. The expected file's hooks are checked when they hold a levain
    handler; else the other file's, when they do, with a WARN naming `levain update`. Also a WARN for levain's
    hooks in both files, for a levain command naming a missing script (a moved workspace), for the two files being
    one (a link), and, in the local layout, for a command naming its script through ``${CLAUDE_PROJECT_DIR}``, a
    Claude Code too old to read the local file at the repository root, and a local file git tracks
    (:func:`_cc_local_layout_rows`)."""
    from levain.host_guard import CC_HOOKS_ROOT_ONLY, cc_levain_hooks_file, cc_settings_aliased
    from levain.install import CC_LOCAL_SETTINGS_REL, _split_hooks

    rows: list[CheckResult] = []
    local_path = install.joinpath(*CC_LOCAL_SETTINGS_REL)
    local_label, shared_label = "/".join(CC_LOCAL_SETTINGS_REL), ".claude/settings.json"
    in_local = cc_levain_hooks_file(install).name == CC_LOCAL_SETTINGS_REL[-1]
    if not in_local:
        rows.append(CheckResult("levain hooks coverage", True, CC_HOOKS_ROOT_ONLY))
    shared_hooks = settings.get("hooks", {})
    shared_hooks = shared_hooks if isinstance(shared_hooks, dict) else {}
    if cc_settings_aliased(install):
        rows.append(CheckResult("levain hooks location", True,
                                f"{local_label} and {shared_label} are one file (a link), so levain does not split "
                                "or move its hooks there",
                                f"Make {local_label} a file of its own, then run `levain update`.", warn=True))
        return shared_hooks, shared_label, rows
    local_hooks: Any = {}
    try:
        local = json.loads(local_path.read_text(encoding="utf-8"))
        if not isinstance(local, dict):
            raise ValueError("not a JSON object")
        local_hooks = local.get("hooks", {})
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        rows.append(CheckResult(local_label, not in_local, f"cannot be read: {e}",
                                "Fix the file, then run `levain update`.", warn=not in_local))
    local_hooks = local_hooks if isinstance(local_hooks, dict) else {}
    local_ours = _split_hooks(local_hooks, install, ours=True)
    shared_ours = _split_hooks(shared_hooks, install, ours=True)
    want_hooks, want_ours, want_label = ((local_hooks, local_ours, local_label) if in_local else
                                         (shared_hooks, shared_ours, shared_label))
    other_hooks, other_ours, other_label = ((shared_hooks, shared_ours, shared_label) if in_local else
                                            (local_hooks, local_ours, local_label))
    if want_ours:
        hooks, label = want_hooks, want_label
        if other_ours:
            rows.append(CheckResult("levain hooks location", True,
                                    f"levain's hooks are in {other_label} too, so a session started at the "
                                    "workspace folder may run them twice",
                                    f"Run `levain update` to remove them from {other_label}.", warn=True))
    elif other_ours:
        hooks, label = other_hooks, other_label
        why = ("Claude Code reads settings.json only for a session started in the workspace folder: a session "
               "started in a subfolder runs no levain hook (no consent gate)" if in_local else
               f"for this workspace levain wires them in {want_label} (see the coverage row)")
        rows.append(CheckResult("levain hooks location", True, f"levain's hooks are in {other_label}: {why}",
                                f"Run `levain update` to move them to {want_label}.", warn=True))
    else:
        hooks, label = want_hooks, want_label
    stale_at = {(event, s) for src in (local_ours, shared_ours) for event, groups in src.items() for g in groups
                for h in g.get("hooks", []) if (s := stale_levain_handler(h)) is not None}
    if stale_at:
        events = sorted({e for e, _ in stale_at})
        rows.append(CheckResult("levain hook scripts", True,
                                "a levain hook names a missing script (moved workspace?): "
                                + ", ".join(sorted({s for _, s in stale_at})) + f" (on {', '.join(events)})"
                                + ("; the UserPromptSubmit hook exits 2, which blocks the prompt"
                                   if "UserPromptSubmit" in events else ""),
                                "Run `levain update`.", warn=True))
    # the placeholder is the start folder: wrong only where a subfolder start loads the hooks (the local layout)
    templated = sorted({event for src in (local_ours, shared_ours) for event, groups in src.items() for g in groups
                        for h in g.get("hooks", []) if "CLAUDE_PROJECT_DIR" in h.get("command", "")}
                       ) if in_local else []
    if templated:
        rows.append(CheckResult("levain hook paths", True,
                                f"{', '.join(templated)}: the command names its script through "
                                "${CLAUDE_PROJECT_DIR}, which is the folder a session starts in, so from a "
                                "subfolder the script is not found (the hook exits 2, which for UserPromptSubmit "
                                "blocks the prompt)", "Run `levain update` to write absolute paths.", warn=True))
    if in_local:
        rows += _cc_local_layout_rows(install)
    return hooks, label, rows


# The Claude Code release that reads settings.local.json at the git repository root (code.claude.com/docs/en/settings:
# "Before v2.1.211, Claude Code kept the file in the starting directory"): below it a session started in a subfolder
# does not load the root's file, so the local layout covers nothing beyond the workspace folder.
CC_LOCAL_AT_ROOT = (2, 1, 211)


def _cc_local_layout_rows(install: Path) -> list[CheckResult]:
    """The local layout's two WARNs (L2 r2): the installed Claude Code is below :data:`CC_LOCAL_AT_ROOT` (the version
    read as the consent canary reads it; an unreadable version gives no row), and git tracks
    ``.claude/settings.local.json`` (it carries this machine's absolute paths, and Claude Code holds a tracked
    file's allow rules until the folder is trusted)."""
    from levain.consent.canary import agent_version
    from levain.host_guard import git_binary, git_env, parse_cc_version

    rows: list[CheckResult] = []
    version = parse_cc_version(agent_version("claude_code", timeout=10.0) or "")
    if version is not None and version < CC_LOCAL_AT_ROOT:
        floor = ".".join(map(str, CC_LOCAL_AT_ROOT))
        rows.append(CheckResult("levain hooks: Claude Code version", True,
                                f"Claude Code {'.'.join(map(str, version))} reads .claude/settings.local.json from "
                                f"the folder a session starts in, not the repository root (from {floor} on it reads "
                                "the root's), so a session started in a subfolder runs no levain hook",
                                f"Update Claude Code to {floor} or later.", warn=True))
    rel = ".claude/settings.local.json"
    git = git_binary()
    try:
        r = subprocess.run([git, "-C", str(install), "ls-files", "--", rel], capture_output=True, text=True,
                           timeout=10, env=git_env()) if git is not None else None
        tracked = r is not None and r.returncode == 0 and bool(r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        tracked = False
    if tracked:
        rows.append(CheckResult("levain hooks file tracked", True,
                                f"git tracks {rel}: it holds this machine's absolute paths, and Claude Code holds a "
                                "tracked file's allow rules until the folder is trusted",
                                f"Untrack it (`git rm --cached {rel}`), then run `levain update`, which git-ignores "
                                "it.", warn=True))
    return rows


def _entries_for(entries: Any, install: Path, script: str) -> list:
    """``entries`` (an event's groups) narrowed to the first handler targeting this install's ``script``, wherever
    it sits in the list (the user's own handlers come first in a merged file), as its own group (``matcher`` and
    the group's other keys kept) holding only that handler; ``entries`` unchanged when none does, so the checks
    below report the first command as before. A group whose ``hooks`` is not a list, or a handler that is not an
    object with a string ``command``, is passed over: a malformed group after a valid one is not a crash (L3 r2)."""
    for g in entries if isinstance(entries, list) else ():
        hs = g.get("hooks") if isinstance(g, dict) else None
        for h in hs if isinstance(hs, list) else ():
            if (isinstance(h, dict) and isinstance(h.get("command"), str)
                    and _hook_command_targets([{"hooks": [h]}], install, script)):
                return [{**g, "hooks": [h]}]
    return entries


def _check_claude_code(install: Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    results.append(CheckResult("CLAUDE.md", True, "present"))
    results.extend(_check_carrier_freshness(install, install / "CLAUDE.md"))
    results.extend(_check_memory_load(install, install / "CLAUDE.md"))
    results.extend(_check_context_surface(install, install / "CLAUDE.md"))

    settings_path = install / ".claude" / "settings.json"
    if not settings_path.is_file():
        results.append(
            CheckResult(
                ".claude/settings.json",
                False,
                "missing",
                "Re-run `levain init --adapter claude-code`.",
            )
        )
    else:
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError as e:
            results.append(
                CheckResult(
                    ".claude/settings.json",
                    False,
                    f"invalid JSON: {e}",
                    "Restore from settings.template.json or re-run `levain init`.",
                )
            )
            settings = None

        if settings is not None:
            hooks, label, located = _cc_levain_hooks(install, settings)
            results.extend(located)
            ss = hooks.get("SessionStart", [])
            ups = hooks.get("UserPromptSubmit", [])
            if ss and ups:
                results.append(
                    CheckResult(
                        f"{label} hooks",
                        True,
                        "SessionStart + UserPromptSubmit configured",
                    )
                )
                ev_scripts = (
                    ("SessionStart", ss, "session_start.py"),
                    ("UserPromptSubmit", ups, "user_prompt_submit.py"),
                )
                for ev_name, entries, script_name in ev_scripts:
                    # levain's handler wherever it sits: a merged file puts the user's own handlers first (L3 r1)
                    entries = _entries_for(entries, install, script_name)
                    py = _extract_command_python(entries)
                    if py is None:
                        results.append(
                            CheckResult(
                                f"settings {ev_name} command",
                                False,
                                "could not parse command",
                                "Re-run `levain init`.",
                            )
                        )
                        continue

                    if not _python_resolvable(py):
                        results.append(
                            CheckResult(
                                f"settings {ev_name} python",
                                False,
                                f"unresolvable: {py}",
                                "Re-run `levain init` to re-resolve {{PYTHON}}.",
                            )
                        )
                        continue

                    # New: verify the command actually targets THIS install's
                    # hook script, not just "some script with a working python."
                    if _hook_command_targets(entries, install, script_name):
                        results.append(
                            CheckResult(
                                f"settings {ev_name} → {script_name}",
                                True,
                                f"wired to this install ({py})",
                            )
                        )
                    else:
                        results.append(
                            CheckResult(
                                f"settings {ev_name} → {script_name}",
                                False,
                                "command does not target this install's hook script",
                                "Re-run `levain init` to point hooks at this install.",
                            )
                        )
            else:
                results.append(
                    CheckResult(
                        f"{label} hooks",
                        False,
                        "SessionStart or UserPromptSubmit missing",
                        "Re-run `levain init`.",
                    )
                )

            results.extend(_check_automemory_mirror(install, hooks))
            results.extend(_check_transcript_hooks(
                install, hooks, ("Stop", "PostToolUse", "PostToolUseFailure"), "Claude Code"))
            results.extend(_check_consent_wiring(install, hooks, "claude-code"))
            results.extend(_check_hooks_off(install, "claude-code"))
            results.extend(_check_consent_ledger(install, hooks, "claude-code"))

            allow = settings.get("permissions", {}).get("allow", [])
            # Match the exact server name or `server__tool` form — not any
            # entry that happens to start with the server name as substring.
            if any(
                a == "mcp__anneal_memory" or a.startswith("mcp__anneal_memory__")
                for a in allow
            ):
                results.append(
                    CheckResult(
                        "settings MCP allowlist",
                        True,
                        "mcp__anneal_memory allowed",
                    )
                )
            else:
                results.append(
                    CheckResult(
                        "settings MCP allowlist",
                        False,
                        "mcp__anneal_memory not in permissions.allow",
                        "Add 'mcp__anneal_memory' to permissions.allow, or re-run `levain init`.",
                    )
                )
            results.append(_check_band_allow(allow))

    mcp_path = install / ".mcp.json"
    if not mcp_path.is_file():
        results.append(
            CheckResult(
                ".mcp.json",
                False,
                "missing",
                "Re-run `levain init --adapter claude-code`.",
            )
        )
        return results

    try:
        mcp = json.loads(mcp_path.read_text())
    except json.JSONDecodeError as e:
        results.append(
            CheckResult(
                ".mcp.json",
                False,
                f"invalid JSON: {e}",
                "Restore from mcp.template.json or re-run `levain init`.",
            )
        )
        return results

    server = mcp.get("mcpServers", {}).get("anneal_memory")
    if not server:
        results.append(
            CheckResult(
                ".mcp.json anneal_memory",
                False,
                "server registration missing",
                "Re-run `levain init`.",
            )
        )
    else:
        results.append(_match_store(".mcp.json anneal_memory", install, server.get("args", [])))
        results.append(_check_mcp_command(
            ".mcp.json anneal_memory", server.get("command"), server.get("args", []), install
        ))
    servers = mcp.get("mcpServers") if isinstance(mcp, dict) else None
    results.append(_check_band_server(
        ".mcp.json", servers.get("levain_band") if isinstance(servers, dict) else None, "claude_code"))
    results.append(_check_dispatch_server(
        install, servers.get("levain_dispatch") if isinstance(servers, dict) else None))

    return results


BAND_ROW = "levain_band MCP registered"


def _check_band_server(where: str, server: object, agent: str, codex_home: Path | None = None) -> CheckResult:
    """QMCP R-q22: the question band's MCP server is registered as levain writes it. Advisory only (a WARN, never a
    FAIL): an agent files questions with `levain ask` without it."""
    from levain.install import BAND_SERVER, _is_levain_launcher

    if not isinstance(server, dict):
        return CheckResult(BAND_ROW, True, f"missing from {where}: the agent can still file questions with "
                                           f"`levain ask`, not with the `ask` tool", "levain update", warn=True)
    # `levain update` replaces only a registration levain itself wrote; one carrying anything else is kept (and,
    # for Claude Code, levain's render staged), so the hint names what helps instead (QMCP L1 #9).
    keep_hint = (f"`levain update` keeps {where}'s entry as yours; merge levain's levain_band entry from "
                 f".levain/pending/.mcp.json (staged by `levain update`)" if agent == "claude_code" else
                 f"`levain update` keeps a block it did not write: delete the [mcp_servers.levain_band] table "
                 f"from {where}, then run `levain update`, which writes levain's")
    extra = sorted(str(k) for k in server if k not in (_BAND_KEYS if agent == "claude_code" else _CODEX_BAND_KEYS))
    command = server.get("command")
    if not _is_levain_launcher({"mcp_servers": {BAND_SERVER: server}}, None, server=BAND_SERVER, agent=agent):
        args = server.get("args")
        shown = " ".join(str(a) for a in args) if isinstance(args, list) else repr(args)
        return CheckResult(BAND_ROW, True, f"{where} starts it as `{command} {shown}`, not levain's "
                                           f"launcher (<python> -P -m levain.consent.band_mcp --agent {agent})",
                           keep_hint, warn=True)
    if extra:  # an `env` (PYTHONPATH, say) or any other key changes what the server runs (QMCP L2 #3)
        return CheckResult(BAND_ROW, True, f"{where}'s levain_band entry carries {', '.join(extra)} beside "
                                           f"levain's launcher: it may not run the band levain ships",
                           keep_hint, warn=True)
    if agent == "codex":
        problem = _codex_band_settings(server, codex_home if codex_home is not None else Path("~/.codex"))
        if problem is not None:
            detail, update_fixes = problem
            return CheckResult(BAND_ROW, True, f"{where}'s levain_band entry {detail}",
                               "levain update" if update_fixes else keep_hint, warn=True)
    if not _is_this_python(command):
        return CheckResult(BAND_ROW, True, f"{where} starts it with `{command}`, not this levain's Python "
                                           f"({sys.executable})", "levain update", warn=True)
    return CheckResult(BAND_ROW, True, f"{where}: {command} -P -m levain.consent.band_mcp "
                                       f"--agent {agent}")


DISPATCH_ROW = "levain_dispatch MCP registered"


def _check_dispatch_server(install: Path, server: object) -> CheckResult:
    """The dispatch door's MCP server is registered in the workspace .mcp.json as levain writes it. Advisory only (a
    WARN, never a FAIL): a planner files with `levain dispatch file` without it. `levain update` rewrites a .mcp.json
    only while it is levain's last render; one carrying anything else is kept whole and levain's render is staged in
    .levain/pending/, so the hint names the hand merge when that render is there."""
    from levain.install import DISPATCH_SERVER, PENDING_REL, _is_levain_launcher

    pending = install.joinpath(*PENDING_REL, ".mcp.json")
    merge = (f"`levain update` keeps your .mcp.json whole, since it is not levain's last render; merge the "
             f"levain_dispatch entry from {'/'.join(PENDING_REL)}/.mcp.json (staged by `levain update`) by hand")
    if not isinstance(server, dict):
        return CheckResult(DISPATCH_ROW, True, "missing from .mcp.json: a planner can still file with "
                                               "`levain dispatch file`, not with the dispatch_file tool",
                           merge if pending.is_file() else "levain update", warn=True)
    command = server.get("command")
    if not _is_levain_launcher({"mcp_servers": {DISPATCH_SERVER: server}}, None, server=DISPATCH_SERVER,
                               agent="claude_code"):
        args = server.get("args")
        shown = " ".join(str(a) for a in args) if isinstance(args, list) else repr(args)
        return CheckResult(DISPATCH_ROW, True, f".mcp.json starts it as `{command} {shown}`, not levain's launcher "
                                               "(<python> -P -m levain.dispatch.mcp --agent claude_code)",
                           merge, warn=True)
    extra = sorted(str(k) for k in server if k not in _BAND_KEYS)
    if extra:  # an `env` or any other key changes what the server runs
        return CheckResult(DISPATCH_ROW, True, f".mcp.json's levain_dispatch entry carries {', '.join(extra)} beside "
                                               "levain's launcher: it may not run the door levain ships",
                           merge, warn=True)
    if not _is_this_python(command):
        return CheckResult(DISPATCH_ROW, True, f".mcp.json starts it with `{command}`, not this levain's Python "
                                               f"({sys.executable})", "levain update", warn=True)
    return CheckResult(DISPATCH_ROW, True, f".mcp.json: {command} -P -m levain.dispatch.mcp --agent claude_code "
                                           "(each call needs the operator's approval)")


BAND_ALLOW_ROW = "settings levain_band tools allowed"
_BAND_TOOL_RULES = ("mcp__levain_band__ask", "mcp__levain_band__note")


def _check_band_allow(allow: object) -> CheckResult:
    """Advisory: Claude Code may call the band's ``ask`` and ``note`` tools without a prompt. Without a tool's rule a
    headless session (``-p``, dontAsk) is refused that call and files nothing, though the server is registered. A
    settings.json kept as the operator's on update can lack one."""
    rules = allow if isinstance(allow, list) else []
    missing = [] if "mcp__levain_band" in rules else [r for r in _BAND_TOOL_RULES if r not in rules]
    if not missing:
        return CheckResult(BAND_ALLOW_ROW, True, " and ".join(_BAND_TOOL_RULES) + " allowed")
    tools = " and ".join(f"`{r.rsplit('__', 1)[1]}`" for r in missing)
    return CheckResult(BAND_ALLOW_ROW, True, f"{', '.join(missing)} not in permissions.allow: a headless session "
                                             f"is refused the {tools} tool (an interactive one asks first)",
                       "Add " + " and ".join(f"'{r}'" for r in missing) + " to .claude/settings.json "
                       "permissions.allow", warn=True)


_BAND_KEYS = frozenset({"type", "command", "args"})
# Codex's block also carries the levain home and the approval mode levain writes (QMCP R-q20 amended)
_CODEX_BAND_KEYS = _BAND_KEYS | {"env", "default_tools_approval_mode"}


def _codex_band_settings(server: dict, codex_home: Path) -> tuple[str, bool] | None:
    """What is wrong with the codex band block's ``env`` and approval mode, and whether `levain update` fixes it
    (an older render, or a levain home moved since the block was written: update rewrites a block levain recorded
    writing) rather than it being the operator's edit, which update keeps; or ``None``. Codex gives the server no
    LEVAIN_HOME of its own, so the block's must be this shell's levain home or questions land in another store; and
    `codex exec` refuses an MCP call that is not pre-approved. ``codex_home`` is the directory whose config.toml
    holds the block."""
    from levain.consent.queue import default_queue_dir

    env, mode = server.get("env"), server.get("default_tools_approval_mode")
    if env is None and mode is None:
        return "has no env / default_tools_approval_mode (an older levain render)", True
    if not isinstance(env, dict) or set(env) != {"LEVAIN_HOME", "CODEX_HOME"}:
        return (f"carries env {env!r} where levain writes only LEVAIN_HOME and CODEX_HOME: it may not run the band "
                f"levain ships"), False
    try:
        homes = (("LEVAIN_HOME", str(default_queue_dir().parent), "the cockpit here will not see them"),
                 ("CODEX_HOME", str(codex_home.expanduser().resolve()),
                  "an ended Codex thread is not resumed with its answer"))
    except (OSError, RuntimeError, ValueError) as exc:
        return f"cannot be compared with this shell's homes ({exc})", False
    for key, want, why in homes:
        named = env[key]
        try:
            seen = str(Path(named).expanduser().resolve()) if isinstance(named, str) else None
        except (OSError, RuntimeError, ValueError):     # a NUL, a symlink loop: not a home levain wrote
            seen = None
        if seen != want:
            return f"starts the band with {key} {named!r}, not {want}: {why}", True
    if mode != "approve":
        return (f"has default_tools_approval_mode {mode!r}: `codex exec` refuses an MCP call that is not approved, "
                f"so only an interactive Codex session that asks can use the `ask` tool"), False
    return None


def _is_this_python(command: object) -> bool:
    """``command`` is this levain's interpreter: the same path, or the same binary in the same directory (the
    directory picks the venv), the rule `_check_mcp_command` applies."""
    if not isinstance(command, str) or not command:
        return False
    here = os.path.abspath(sys.executable)
    try:
        cmd_abs = os.path.abspath(command)
        return cmd_abs == here or (os.path.dirname(cmd_abs) == os.path.dirname(here)
                                   and os.path.realpath(cmd_abs) == os.path.realpath(here))
    except (OSError, ValueError):
        return False


def _codex_takeover_hint(install: Path) -> str:
    """The command that wires Codex's one hooks.json back to this install. `init --force` reads neither the
    recorded packs nor the recorded answers by itself, so both are passed when they are on disk; without a
    recorded answers file it says the interview runs again (L3 r1 on the codex pin owner)."""
    from levain import manifest

    packs, status = manifest.read_pack_locks_status(install)
    cmd = f"levain init --path {shlex.quote(str(install))} --adapter codex --force"
    cmd += "".join(f" --pack {shlex.quote(str(p.source))}" for p in packs)
    answers = install / ".levain" / "answers.json"
    if answers.is_file():
        cmd += f" --answers {shlex.quote(str(answers))}"
        note = "your recorded answers, so no interview"
    else:
        note = "no recorded answers here, so it asks the interview again"
    if status == "corrupt":
        note += "; the pack record cannot be read, so add the --pack flags you installed with"
    return (f"To wire this install again: `{cmd}` ({note}; the store is kept and replaced files are "
            "backed up).")


def _check_codex_gate_trust(install: Path, hooks_path: Path, codex_home: Path) -> list[CheckResult]:
    """Codex's PreToolUse consent gate (Q14 B5) and whether Codex will run it, by Codex's own ``hooks/list``
    (:func:`levain.consent.adapters.codex.gate_trust`). GREEN only when the entry is levain's render, its script is
    the package's and Codex reports it trusted, enabled and hashed as levain's formula: Codex skips an untrusted,
    changed or disabled hook with no error or warning, so the gate would silently not run. A WARN when Codex
    cannot be asked (config.toml alone is not Codex's word), or when nothing can decide a gated call now."""
    from levain.consent.adapters import codex
    from levain.consent.adapters.claude_code import nothing_can_decide
    from levain.consent.policy import AGENT_CONFIG_TIER
    from levain.install import CONSENT_HOOK_SCRIPTS, _consent_queue_dir, persisted_consent_hook

    if persisted_consent_hook(install) == "off":
        return []
    name = "Codex consent gate (PreToolUse)"
    got = codex.gate_trust(hooks_path, install / "activation" / "hooks" / CONSENT_HOOK_SCRIPTS["codex"],
                           home=codex_home, timeout=10.0)
    if got.status == "absent":
        return [CheckResult(name, False, f"not wired: {got.detail}; agent config and hook edits in `codex exec` "
                            "are not gated", "Run `levain update` to add it.", upgrade_pending=True)]
    update = "Run `levain update`, which writes Codex's trust for levain's hooks and confirms it with Codex."
    if got.status == "script_changed":
        return [CheckResult(name, False, f"{got.detail}: Codex still runs whatever the script now does, so the gate "
                            "cannot be relied on", "Restore the script: `levain update` lists it for review.")]
    if got.status == "changed" and "differs from levain's render" in got.detail:
        return [CheckResult(name, False, f"{got.detail}: Codex runs the entry as it now stands, if at all, so agent "
                            "config and hook edits may not be gated", update)]
    if got.status == "unconfirmed":
        return [CheckResult(name, True, f"wired to this install; {got.detail}, but Codex's own verdict could not be "
                            "read, so whether Codex runs the gate is unconfirmed",
                            "Run `levain doctor` again where `codex` is on PATH.", warn=True)]
    if got.status != "trusted":
        what = {"untrusted": "NOT trusted", "changed": "trusted once, changed since",
                "disabled": "disabled", "unknown": "trust unreadable", "unlisted": "not listed by Codex",
                "formula_mismatch": "hashed differently by this Codex"}.get(got.status, got.status)
        return [CheckResult(name, False, f"{what} ({got.detail}): Codex skips it silently, so agent config and "
                            "hook edits are not gated", update)]
    settings = codex.load_settings(_consent_queue_dir())
    if nothing_can_decide(settings, tier=AGENT_CONFIG_TIER):
        return [CheckResult(name, True, "wired to this install and trusted by Codex (its own hooks/list), but "
                            "nothing can decide a gated call now: no active key whose class signs T3 (away mode "
                            "alone does not count: no key could answer a held call), so an agent config or hook "
                            "edit is refused outright outside approval mode, and asked by Codex's own approval in it",
                            UNCONFINED_HOW, warn=True)]
    return [CheckResult(name, True, "wired to this install and trusted by Codex (its own hooks/list: trusted, "
                        "enabled, levain's hash)")]


def _check_codex(install: Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    results.append(CheckResult("AGENTS.md", True, "present"))
    results.extend(_check_carrier_freshness(install, install / "AGENTS.md"))
    results.extend(_check_memory_load(install, install / "AGENTS.md"))
    results.extend(_check_context_surface(install, install / "AGENTS.md"))

    codex_home = Path(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))
    hooks_path = codex_home / "hooks.json"
    config_path = codex_home / "config.toml"

    if not hooks_path.is_file():
        results.append(
            CheckResult(
                f"{hooks_path}",
                False,
                "missing",
                "Re-run `levain init --adapter codex`.",
            )
        )
    else:
        try:
            hooks = json.loads(hooks_path.read_text())
        except json.JSONDecodeError as e:
            results.append(
                CheckResult(
                    f"{hooks_path}",
                    False,
                    f"invalid JSON: {e}",
                    "Restore from hooks.json.template or re-run `levain init`.",
                )
            )
            hooks = None

        if hooks is not None:
            hh = hooks.get("hooks", {})
            ss = hh.get("SessionStart", [])
            ups = hh.get("UserPromptSubmit", [])
            ev_scripts = (
                ("SessionStart", ss, "session_start.py"),
                ("UserPromptSubmit", ups, "user_prompt_submit.py"),
            )
            from levain import host_guard as hg

            owner = hg.codex_hooks_owner(install, codex_home)
            takeover = _codex_takeover_hint(install)
            if owner:
                takeover += (f" If you did not run levain init in {owner}, read its activation/hooks/ first: "
                             "those scripts now run in every Codex session.")
            for ev_name, entries, script_name in ev_scripts:
                # Exact-path match — substring-only match would false-positive
                # against ~/levain vs ~/levain-test (prefix overlap).
                if not _hook_command_targets(entries, install, script_name):
                    results.append(
                        CheckResult(
                            f"~/.codex/hooks.json {ev_name} → {script_name}",
                            False,
                            "not wired to this install (Codex is one-install-per-machine at v1)"
                            + (f": levain wired it to the install at {owner}" if owner else ""),
                            ("" if owner else "Another install may own ~/.codex/hooks.json. ") + takeover,
                        )
                    )
                    continue

                py = _extract_command_python(entries)
                if py and not _python_resolvable(py):
                    results.append(
                        CheckResult(
                            f"~/.codex/hooks.json {ev_name} python",
                            False,
                            f"unresolvable: {py}",
                            "Re-run `levain init` to re-resolve {{PYTHON}}.",
                        )
                    )
                elif py and "levain.hookrun" in (_first_command_tokens(entries) or []) and not _is_this_environment(py):
                    results.append(
                        CheckResult(
                            f"~/.codex/hooks.json {ev_name} python",
                            True,
                            f"{py} is not this levain's interpreter ({sys.executable}); the hooks need levain "
                            "importable there, or every one exits 1 and Codex shows \"Hook failed\"",
                            f"Run `levain doctor` with {py} to check it, or `levain update` from the python "
                            "levain is installed in.",
                            warn=True,
                        )
                    )
                else:
                    results.append(
                        CheckResult(
                            f"~/.codex/hooks.json {ev_name} → {script_name}",
                            True,
                            f"wired to this install ({py})" if py else "wired",
                        )
                    )
            # only while the file is this install's: otherwise the rows above already say it is not wired
            if _hook_command_targets(ss, install, "session_start.py"):
                results.extend(_check_transcript_hooks(install, hh, ("Stop", "PostToolUse"), "Codex"))
                results.extend(_check_consent_wiring(install, hh, "codex"))
                results.extend(_check_consent_ledger(install, hh, "codex"))
                results.extend(_check_codex_gate_trust(install, hooks_path, codex_home))
                results.extend(_check_hooks_off(install, "codex", codex_home=codex_home))

    if not config_path.is_file():
        results.append(
            CheckResult(
                f"{config_path}",
                False,
                "missing",
                "Re-run `levain init --adapter codex`.",
            )
        )
        return results

    try:
        config = tomllib.loads(config_path.read_text())
    except tomllib.TOMLDecodeError as e:
        results.append(
            CheckResult(
                f"{config_path}",
                False,
                f"invalid TOML: {e}",
                "Fix the syntax error in ~/.codex/config.toml.",
            )
        )
        return results

    server = config.get("mcp_servers", {}).get("anneal_memory")
    if not server:
        results.append(
            CheckResult(
                "config.toml [mcp_servers.anneal_memory]",
                False,
                "missing",
                "Re-run `levain init --adapter codex`.",
            )
        )
    else:
        results.append(
            _match_store("config.toml [mcp_servers.anneal_memory]", install, server.get("args", []))
        )
        results.append(_check_mcp_command(
            "config.toml [mcp_servers.anneal_memory]", server.get("command"),
            server.get("args", []), install,
        ))
    servers = config.get("mcp_servers")
    results.append(_check_band_server(
        "config.toml", servers.get("levain_band") if isinstance(servers, dict) else None, "codex", codex_home))

    return results


def _check_mcp_command(name: str, command: object, args: list, install: Path) -> CheckResult:
    """Is the memory server registration launched by THIS levain's interpreter?

    `spore-751` (ruled 2026-09-13): `init` registers `<levain python> -P -m anneal_memory`,
    so the anneal that serves memory is the one levain imports, checks and upgrades. A
    registration naming any other command can start a different anneal from the one every
    other doctor line describes. Deferred from item 1 into `spore-840` because every install
    written before that change fails it on upgrade — an upgrade step pending, so it carries
    that exit code; an unresolvable command is broken and does not.
    """
    from levain import manifest

    check = f"{name} command"
    if not isinstance(command, str) or not command:
        return CheckResult(check, False, "no command in registration",
                           f"Re-run `levain init --force --path {install}`.")
    if not _python_resolvable(command):
        return CheckResult(
            check, False, f"unresolvable: {command} — the memory server cannot start",
            f"Re-run `levain init --force --path {install}`.",
        )
    if not isinstance(args, list):
        return CheckResult(check, False, f"args is not a list: {args!r}",
                           f"Re-run `levain init --force --path {install}`.")
    # The shape `init` writes, element for element:
    # `<levain python> -P -m anneal_memory --db <store> serve --session-id <id>` (anneal 1.0's
    # consolidate-baton identity; the 6-element form without it is what every pre-1.0 init wrote,
    # and still starts the server). A looser test passed
    # `-P -c pass -m anneal_memory`, a `status` subcommand (codex HIGH), and a `--version`
    # ahead of `--db` (codex HIGH, reproduced 2026-09-14); none of them starts the server.
    strs = [a for a in args if isinstance(a, str)]
    module_form = (
        len(strs) == len(args) in (6, 8)
        and strs[:4] == ["-P", "-m", "anneal_memory", "--db"] and strs[5] == "serve"
        and (len(strs) == 6 or strs[6:] == ["--session-id", manifest.ANNEAL_SESSION_ID])
    )
    if (len(strs) == len(args) == 8 and strs[:4] == ["-P", "-m", "anneal_memory", "--db"]
            and strs[5:7] == ["serve", "--session-id"] and strs[7] != manifest.ANNEAL_SESSION_ID):
        # The right shape, someone else's session: the baton `init` claims is held for
        # ANNEAL_SESSION_ID, so this server is always downgraded to capture-only (codex HIGH).
        return CheckResult(
            check, False,
            f"launches the memory server as session '{strs[7]}', but the consolidate baton is "
            f"claimed for '{manifest.ANNEAL_SESSION_ID}' — every wrap over MCP is downgraded to capture-only",
            f"Re-run `levain init --force --path {install}` (it RE-RUNS THE INTERVIEW; your store is kept).",
        )
    if module_form and len(strs) == 6 and _store_requires_baton(install):
        # Every install made before the baton: the server starts but with no session id, so on a
        # store that requires the baton it can never consolidate (codex HIGH). `levain update`
        # re-renders this registration (the 6-argument form is still recognised as levain's own).
        return CheckResult(
            check, False,
            "launcher predates the consolidate baton (no `--session-id`) — the memory server "
            "cannot consolidate on this store",
            f"Run `levain update --path {install}` to rewrite it (the store is kept), then restart your harness.",
            upgrade_pending=True,
        )
    if not module_form:
        # Only the registration every pre-spore-751 install carries is a pending upgrade
        # step: a console script named exactly `anneal-memory` with argv exactly
        # `--db <store> serve`. Any other command is not something `init` ever wrote, so
        # doctor cannot vouch for it (codex MED); a name PREFIX let `anneal-memory-<anything>`
        # read as routine (codex HIGH, reproduced 2026-09-14).
        if (Path(command).name in ("anneal-memory", "anneal-memory.exe")
                and len(strs) == len(args) == 3
                and strs[0] == "--db" and strs[2] == "serve"):
            return CheckResult(
                check,
                False,
                f"launches `{command}`, not `{sys.executable} -P -m anneal_memory` — the "
                f"anneal serving your memory may not be the one levain checks and upgrades",
                f"Registrations written before levain pinned the memory server to its own "
                f"interpreter look like this. `levain init --force --path {install}` rewrites "
                f"it (it RE-RUNS THE INTERVIEW; your store is kept).",
                upgrade_pending=True,
            )
        return CheckResult(
            check, False,
            f"`{command}` with args {args!r} is not a registration `levain init` writes",
            f"Re-run `levain init --force --path {install}`.",
        )
    # ⛔ DOCTOR NEVER EXECUTES A COMMAND IT DID NOT CHOOSE (complement + codex HIGH): the
    # command comes from a config file, and a static check that runs it hands code execution
    # to that file. A realpath match alone is not "this interpreter": every venv's
    # `bin/python` can resolve to one base binary, and starting it from another venv's
    # `bin/` runs that venv's `.pth` files even under `-P` (codex HIGH, reproduced
    # 2026-09-14). The directory a binary is started from picks its `pyvenv.cfg`, so the
    # command must be this path, or the same binary in this directory; the probe then runs
    # `sys.executable` itself, never the configured spelling.
    here = os.path.abspath(sys.executable)
    try:
        cmd_abs = os.path.abspath(command)
        same_binary = cmd_abs == here or (
            os.path.dirname(cmd_abs) == os.path.dirname(here)
            and os.path.realpath(cmd_abs) == os.path.realpath(here)
        )
    except (OSError, ValueError):
        same_binary = False
    if not same_binary:
        return CheckResult(
            check, False,
            f"`{command}` is not this levain's Python ({sys.executable}); doctor does not run "
            f"it, so it cannot confirm which anneal serves your memory",
            f"If that is the levain you use, run `levain doctor` with it. Otherwise "
            f"`levain init --force --path {install}` registers this one (it RE-RUNS THE "
            f"INTERVIEW; your store is kept).",
        )
    ran, out = _probe([sys.executable, "-P", "-c", "import sys, anneal_memory; print(sys.prefix)"])
    if not ran:
        return CheckResult(
            check, False, f"`{command} -P -m anneal_memory` cannot start: {out}",
            f"Re-run `levain init --force --path {install}` from the environment levain is "
            f"installed in.",
        )
    prefix = out.strip()
    if os.path.realpath(prefix) == os.path.realpath(sys.prefix):
        return CheckResult(check, True, f"{command} -P -m anneal_memory (this levain's environment)")
    return CheckResult(
        check,
        False,
        f"serves memory from the Python environment at {prefix}, not the one running this "
        f"doctor ({sys.prefix}) — every other line here describes this environment's anneal",
        f"If that environment is the levain you use, run `levain doctor` from it. Otherwise "
        f"`levain init --force --path {install}` registers this one (it RE-RUNS THE "
        f"INTERVIEW; your store is kept).",
    )


def _match_store(name: str, install: Path, args: list) -> CheckResult:
    """Verify the MCP server args point at this install's anneal-memory store.

    Compares resolved paths so symlinked prefixes (e.g. macOS /tmp -> /private/tmp)
    match correctly. The store is read from the `--db <path>` pair wherever it sits in
    the args, so the pre-spore-751 shape and the `-P -m anneal_memory` shape both parse.
    """
    expected = (install / ".levain" / "memory.db").resolve()
    configured: str | None = None
    for i, a in enumerate(args):
        if a == "--db" and i + 1 < len(args):
            configured = args[i + 1]
            break

    if configured is None:
        return CheckResult(
            name,
            False,
            f"no --db arg in registration (args={args})",
            "Re-run `levain init`.",
        )

    try:
        configured_resolved = Path(configured).resolve()
    except OSError:
        configured_resolved = Path(configured)

    if configured_resolved == expected:
        return CheckResult(name, True, f"registered, store={configured}")

    return CheckResult(
        name,
        False,
        f"store path mismatch: {configured} != {expected}",
        f"Re-run `levain init` to point at {expected}.",
    )
