"""Cross-platform daemon recipe for the always-on ``levain serve`` cockpit.

The DAILY-DRIVER autostart (spore-205): make ``levain serve --write`` start on login and
survive a crash, so the cockpit "just works" for a non-flow operator — no ad-hoc ``nohup``,
no manual restart after a reboot.

Architecture = canonical-object + replaceable-surfaces (the same shape as Levain's firing
adapters): ONE OS-agnostic :class:`DaemonSpec` (what to run + how it should behave) behind a
:class:`DaemonProvider` interface (``render_unit`` / ``install`` / ``uninstall`` / ``status`` /
``restart``), with one thin provider per OS. macOS (launchd *user* agent) ships first; Linux
(systemd ``--user``) and Windows (Task Scheduler ``/SC ONLOGON``) slot in as PURE ADDITIONS
against this contract — no refactor.

LOAD-BEARING INVARIANT — per-user, NO admin/root. A launchd *user* agent
(``~/Library/LaunchAgents`` + ``launchctl bootstrap gui/$UID``), a systemd ``--user`` unit, a
``schtasks`` task WITHOUT ``/RU SYSTEM`` — never a system LaunchDaemon / service. This keeps the
install sovereign + sudo-free, and is exactly what rejects the Windows-*Service* path.

THREAT-MODEL (M2): always-on means a 24/7 loopback-LOCAL cockpit behind a per-launch token. The
daemon-run server never prints the token (its stdout is a log file); it leaves the unlocked link in
``~/.levain-runtime/<port>.json`` (0600, in a 0700 directory an entity's floor denies), and the
operator opens the page with ``levain serve --open-running``. Every restart mints a new token.
Browser/cross-origin attacks stay blocked (Host allowlist + CSRF + the loopback bind).
Off-box (``--host <mesh>``) is deliberately NOT daemonized here — an install-bearing serve is
loopback-only by construction (its seed/config is operator-private).
"""

from __future__ import annotations

import os
import platform
import stat
import re
import shlex
import shutil
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LABEL = "com.levainhq.levain"
DEFAULT_PORT = 7420

# --- scheduled governed seat (K4a) ---
# A distinct label PREFIX from the cockpit's: a seat and the cockpit are different units with
# different lifecycles, and colliding on one label would have `daemon install` silently replace
# one with the other.
DEFAULT_SEAT_LABEL = "com.levainhq.levain.seat"
# Hourly. A cadence, not a claim about the right cadence — the useful interval is a property of
# the seat's job, and the first real seat is what teaches it.
DEFAULT_SEAT_INTERVAL = 3600
# A STARTING step bound for an unattended turn (see build_seat_spec). Deliberately finite: the
# alternative is the SDK's own limit, which nobody chose for this purpose. Calibrate against a
# seat that has actually run rather than reasoning about it (`derive_dont_invent`).
DEFAULT_SEAT_MAX_ITERATIONS = 40
# A STARTING wall-clock bound for an unattended turn (K4a ⑥, `spore-434`). Finite for a REASON that
# is stronger than the step bound's: a turn hung inside ONE step is unbounded by `max_iterations`,
# and because launchd COALESCES per label the seat then never runs again at all — behind a unit
# still reporting installed and loaded. So this is not belt-and-braces on the step bound; it is the
# only thing that makes "restartable" true.
#
# 1800s = half the default hourly cadence. Chosen to sit comfortably ABOVE a legitimate 40-step turn
# on a cloud open model (tens of seconds per step) and comfortably BELOW the interval, so a bounded
# turn never starves its own next run. Like the step bound, a STARTING number to calibrate against a
# seat that has actually run — not a derived one (`derive_dont_invent`).
DEFAULT_SEAT_MAX_SECONDS = 1800.0
# A STARTING wall-clock bound for the seat's SELF-CONSOLIDATE (K4a [6]), separate from the turn's.
#
# SEPARATE, not shared, and the separation is the design: the consolidate runs as a second bounded
# phase AFTER the turn, so a turn that spends its whole budget cannot starve the maintenance that
# keeps the seat's recall from degrading. The cost is that a seat's worst-case PROCESS lifetime is
# `max_seconds + consolidate_max_seconds`, which is why `install-seat` warns against the SUM rather
# than against either bound alone.
#
# MEASURED, not guessed (L4-live, 2026-07-29, `glm-5.2:cloud` via Ollama Cloud): a consolidate of a
# 12-episode window — the default wrap threshold, i.e. the size at which a seat actually wraps —
# producing a 4020-char neocortex took **22s**; a 3-episode window took 7s. So 900s is ~40x a
# measured compose at the threshold, which is the headroom a WALL-CLOCK bound wants: it must never
# cut off legitimate work, only a stall. `derive_dont_invent` — this slice's handoff specifically
# called out re-deriving this against a measured compose instead of reasoning about it.
#
# It also has to fit the cadence, and it does exactly: 1800s turn + 900s consolidate = 2700s, which
# is under the 3600s default interval with room to spare, so a default seat never coalesces itself
# out of a run. Changing either bound should be re-checked against that sum (`install-seat` warns).
#
# ⚠ WHAT THE MEASUREMENT DOES NOT COVER, said rather than implied: a long-lived seat's window grows
# (more episodes, a larger existing memory to re-read), and cloud latency varies by day. 22s is the
# shape of a healthy compose, NOT the worst case — the bound is sized for the tail, not the median.
DEFAULT_SEAT_CONSOLIDATE_MAX_SECONDS = 900.0
# launchd's default minimum respawn spacing (`man launchd.plist` → ThrottleInterval). A
# StartInterval below this is silently throttled up to it unless ThrottleInterval is lowered too.
_LAUNCHD_DEFAULT_THROTTLE = 10

# The base PATH the daemon needs: a login-launched supervisor (launchd/systemd) hands the
# process a MINIMAL PATH, so a thin process dies silently when it shells out (the flowbridge
# launchd lesson). We prepend the dir holding the resolved levain/python bin + the user-local
# bin, then these standard dirs.
_BASE_PATH_DIRS = ("/usr/local/bin", "/opt/homebrew/bin", "/opt/homebrew/sbin",
                   "/usr/bin", "/bin", "/usr/sbin", "/sbin")


class DaemonError(RuntimeError):
    """A service-manager command (launchctl/systemctl/schtasks) failed."""


_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _valid_label(label: str) -> str:
    """A label becomes a FILE NAME on both platforms (``~/Library/LaunchAgents/<label>.plist``,
    ``~/.config/systemd/user/<label>.service``) and a unit name. ``../x`` would write, or for
    ``uninstall`` delete, outside those directories; ``%`` and ``@`` change what systemd reads the
    name as (codex, L3 2026-09-30). Reverse-DNS characters only, as every label Levain ships uses."""
    if not isinstance(label, str) or not _LABEL_RE.fullmatch(label) or ".." in label:
        raise DaemonError(
            f"refusing the label {label!r}: use letters, digits, '.', '_' and '-' only, starting with "
            "a letter or digit, no '..' and at most 128 characters (e.g. com.example.seat)."
        )
    return label


@dataclass(frozen=True)
class DaemonSpec:
    """OS-agnostic description of a supervised Levain process. A :class:`DaemonProvider` renders
    this into the platform's native unit (plist / systemd unit / scheduled task). Construct it with
    :func:`build_spec` (the always-on cockpit) or :func:`build_seat_spec` (a scheduled governed
    seat), never by hand — the path/bin/env resolution is shared across OSes.

    **TWO SHAPES, and they are not interchangeable (K4a).** A *resident* service (the cockpit)
    runs forever and must be relaunched when it dies — ``keep_alive=True``, no interval. A
    *periodic* seat runs ONE bounded turn and EXITS — ``start_interval=<seconds>``, and it must
    NOT keep-alive. That exclusion is enforced in :meth:`__post_init__` rather than documented,
    because the failure is silent and severe: launchd relaunches a ``KeepAlive`` job immediately
    on exit, so a periodic job that also keeps alive runs CONTINUOUSLY — the unit would render
    cleanly, install cleanly, and lie about its own cadence, burning tokens on a hot loop with
    nothing in the output saying so (``absence_of_signal_rendered_as_health``)."""

    label: str
    argv: list[str]            # full exec: [levain_bin, "serve", "--write", "--no-open", ...]
    working_dir: Path
    env: dict[str, str]        # PATH (minimal-login gotcha) / HOME / PYTHONUNBUFFERED
    stdout_log: Path
    stderr_log: Path
    run_at_login: bool = True
    keep_alive: bool = True
    # Periodic cadence in SECONDS for a scheduled seat; None = a resident service. Mutually
    # exclusive with keep_alive (see the class docstring).
    start_interval: int | None = None

    def __post_init__(self) -> None:
        _valid_label(self.label)
        if self.start_interval is None:
            return
        if self.start_interval <= 0:
            raise ValueError(
                f"start_interval must be a positive number of seconds, got {self.start_interval!r}"
            )
        if self.keep_alive:
            raise ValueError(
                "a periodic spec (start_interval set) must have keep_alive=False — a KeepAlive job "
                "is relaunched the instant it exits, so the interval would be ignored and the seat "
                "would run continuously instead of on its schedule. Use build_seat_spec()."
            )


@dataclass(frozen=True)
class DaemonStatus:
    """The result of :meth:`DaemonProvider.status`."""

    installed: bool            # the unit file is on disk
    running: bool              # a live process exists (a numeric pid) — the cross-version run signal
    detail: str                # a one-line human summary (the state line, when available)
    load_state: str = "unknown"  # running | loaded | not-loaded | unknown — keyed on the service
                                  # manager, NEVER on file presence (file-on-disk ≠ loaded; the
                                  # honesty floor). "unknown" = the domain itself is unreadable (ssh /
                                  # no Aqua), which must never read as a false "not-loaded".


@dataclass(frozen=True)
class DaemonPlan:
    """What :meth:`DaemonProvider.would_install` reports — computed WITHOUT mutating anything. The
    honesty floor (06-29 Daily Sharpening): a unit file on disk is NOT proof the service is
    installed-and-loaded. So the plan diffs the rendered unit against any on-disk unit AND reads the
    TRUE live state — file-present ≠ loaded ≠ running."""

    label: str
    unit_path: Path
    on_disk: bool              # a unit file already exists at unit_path
    would_change: bool         # the rendered unit differs from what's on disk (or nothing's on disk)
    current: DaemonStatus      # the TRUE live state — keyed on the service manager, not file presence
    action: str                # one-line human summary of what install would do


# --- path / bin / env resolution (shared across every provider) ------------------------------

def _levain_invocation() -> list[str]:
    """The argv prefix that runs Levain. Prefer the installed ``levain`` console script
    (pyproject ``[project.scripts]``); fall back to ``<python> -m levain`` when ``levain`` is
    not on PATH (e.g. an unactivated venv). Both forms resolve to ABSOLUTE paths — a
    login-launched unit must never depend on PATH lookup to find the interpreter."""
    found = shutil.which("levain")
    if found:
        # ProgramArguments[0] must be ABSOLUTE (a login unit has no PATH to resolve against);
        # shutil.which can return a relative path if PATH carries relative entries (codex L3 LOW).
        # realpath is safe on a console script — its shebang still points at the right interpreter.
        return [os.path.realpath(found)]
    # Fallback: run via the current interpreter. Do NOT realpath sys.executable — for a venv it is
    # the venv's python (already absolute), and realpath resolves the symlink to the UNDERLYING
    # interpreter, which has NO access to the venv site-packages -> `No module named levain`
    # (L4-live caught this: a `.venv` install ran under the resolved uv python and couldn't import).
    return [sys.executable, "-m", "levain"]


def _dedup_preserve(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _daemon_env(invocation: list[str]) -> dict[str, str]:
    """The minimal env a login-launched serve needs. PATH must include the dir holding the
    resolved bin (else a minimal-PATH supervisor can't find python/levain); HOME for ``~``
    expansion; PYTHONUNBUFFERED so the startup banner + a KeepAlive-crash reach the log live
    (not block-buffered until exit)."""
    bin_dir = str(Path(invocation[0]).resolve().parent)
    home_local = str(Path.home() / ".local" / "bin")
    path = os.pathsep.join(_dedup_preserve([bin_dir, home_local, *_BASE_PATH_DIRS]))
    # PYTHONPATH = the dir CONTAINING the levain package (this module's grandparent). The daemon's
    # cwd is the INSTALL dir (not the repo), so a `-m levain` invocation can't find the package via
    # cwd; this makes `import levain` work regardless of cwd AND whether levain is run from a repo
    # checkout / unactivated venv rather than a site-packages install (L4-live caught this — the
    # serve crashed with `No module named levain` under launchd's install-dir cwd). Harmless for a
    # pip-installed adopter (the path is just site-packages, already importable).
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    return {"PATH": path, "PYTHONPATH": pkg_parent, "HOME": str(Path.home()),
            "PYTHONUNBUFFERED": "1"}


# The umask every unit runs under (launchd ``Umask``, systemd ``UMask=``): files it creates are 0600, dirs 0700.
_PRIVATE_UMASK = 0o077


def _prepare_private_logs(spec: "DaemonSpec") -> None:
    """Create the log directory 0700 and both log files 0600 before the service first writes them (an append
    keeps a file's mode); an existing log file this user owns is narrowed to 0600 too. Levain's own default directory
    on Linux is also chmod-ed back to 0700 if it exists wider; a directory Levain does not own (macOS's
    ~/Library/Logs, a caller's --log-dir) is created private when missing and otherwise left as it is, but it must be
    this user's and writable by no one else: the service manager opens the log by path later, and in a directory
    another user can write, the file checked here could be swapped before then (codex L3)."""
    for log in (spec.stdout_log, spec.stderr_log):
        d = log.parent
        if not d.exists():
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(d, 0o700)   # mkdir's mode is masked by the umask
        elif platform.system() != "Darwin" and d.resolve() == _default_log_dir().expanduser().resolve():
            os.chmod(d, 0o700)
        dst = os.stat(d)
        if not stat.S_ISDIR(dst.st_mode) or dst.st_uid != os.getuid() or dst.st_mode & 0o022:
            raise DaemonError(f"{d} must be a directory this user owns that no other user can write; pass another "
                              "--log-dir.")
        # The service will append to whatever is at this path: it must be a regular file this user owns with one
        # name, never a link (it would be followed), a hardlink to another file, a FIFO or device (it would block), or
        # another user's file (codex L3: in a shared --log-dir someone could pre-create it world-readable). Opened once with O_NOFOLLOW and checked and narrowed
        # through that descriptor, so nothing can be swapped in between the check and the chmod (L1 L3).
        flags = (os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                 | getattr(os, "O_NOCTTY", 0))
        try:
            fd = os.open(log, flags, 0o600)
        except OSError as exc:
            if os.path.islink(log):
                raise DaemonError(
                    f"{log} is a symlink; a unit's log must be a regular file. Remove it and install again.") from exc
            if isinstance(exc, PermissionError):   # another user's file (say, root's from an earlier sudo install)
                raise DaemonError(
                    f"{log} is not a regular file this user owns; remove it or pass another --log-dir.") from exc
            raise DaemonError(f"could not open {log}: {exc}") from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                raise DaemonError(f"{log} is not a regular file this user owns; remove it or pass another --log-dir.")
            if st.st_nlink != 1:   # another name for one of this user's files: the service would append to it (L2)
                raise DaemonError(f"{log} has {st.st_nlink} names on disk (a hardlink); remove it or pass another "
                                  "--log-dir.")
            os.fchmod(fd, 0o600)   # new, or an older unit's log created wider (its old token lines, since dead)
        finally:
            os.close(fd)


def _default_log_dir() -> Path:
    """Where the daemon's stdout/err go. macOS convention is ``~/Library/Logs``; elsewhere an
    XDG-ish ``~/.local/state/levain``. Created (parents) at install time."""
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Logs"
    return Path.home() / ".local" / "state" / "levain"


def build_spec(
    *,
    install_path: Path,
    port: int = DEFAULT_PORT,
    label: str = DEFAULT_LABEL,
    log_dir: Path | None = None,
) -> DaemonSpec:
    """Build the OS-agnostic spec for ``levain serve --write --no-open`` over ``install_path``.

    ``--write`` (the daily-driver cockpit) + ``--no-open`` (a login-launched process must not
    pop a browser tab on every login AND every KeepAlive restart) + loopback-only port. The
    install path is resolved to an absolute path — a login unit has no stable cwd."""
    install_path = install_path.expanduser().resolve()
    invocation = _levain_invocation()
    argv = [
        *invocation, "serve", "--write", "--no-open",
        "--port", str(port), "--path", str(install_path),
    ]
    logs = (log_dir or _default_log_dir()).expanduser().resolve()
    return DaemonSpec(
        label=label,
        argv=argv,
        working_dir=install_path,
        env=_daemon_env(invocation),
        stdout_log=logs / f"{label}.log",
        stderr_log=logs / f"{label}.err",
    )


def build_seat_spec(
    *,
    entity_path: Path,
    task: str,
    interval: int = DEFAULT_SEAT_INTERVAL,
    label: str = DEFAULT_SEAT_LABEL,
    model: str | None = None,
    max_iterations: int | None = DEFAULT_SEAT_MAX_ITERATIONS,
    max_seconds: float | None = DEFAULT_SEAT_MAX_SECONDS,
    consolidate: bool = True,
    consolidate_every: int | None = None,
    consolidate_max_seconds: float | None = DEFAULT_SEAT_CONSOLIDATE_MAX_SECONDS,
    log_dir: Path | None = None,
) -> DaemonSpec:
    """Build the OS-agnostic spec for a **scheduled governed seat** (K4a) — ONE sovereign entity
    that runs a bounded task unattended, on a cadence, and exits.

    This is the PERIODIC sibling of :func:`build_spec`: ``start_interval`` set, ``keep_alive``
    off, and ``run_at_login`` off (installing a *schedule* is not a request to spend a real model
    turn the instant you install it — the first run happens on the cadence, or on demand via
    ``levain daemon restart``).

    Three argv choices that are deliberate, because each looks like an obvious flag to flip:

    - **NO ``--quiet``.** It prints only the final reply and suppresses the tool-activity stream —
      but for an *unattended* seat that stream IS the operator's fan-in surface: the record of what
      the entity actually did, including a K3 gated halt. Quieting it would throw away exactly the
      evidence the human is supposed to review, and leave a log that cannot distinguish "did
      nothing" from "was stopped" (``absence_of_signal_rendered_as_health``).
    - **``--max-iterations`` defaults ON.** "Time-bounded" is part of K4a's definition, and an
      unattended turn with no step bound can spend indefinitely with nobody watching. The default
      is a STARTING bound to be calibrated against a seat that has actually run — not a derived
      number (``derive_dont_invent``). Pass ``None`` to fall back to the SDK's own limit.
    - **``--max-seconds`` defaults ON, and it is the one that makes "restartable" TRUE**
      (K4a ⑥, ``spore-434``). The step bound above counts STEPS, so a turn hung inside ONE of them —
      a stalled model call, a socket with no read timeout — is not bounded by it at all. And because
      launchd **coalesces per label**, launchd will not start the next turn while that one lives:
      the seat stops running FOREVER behind a unit that still reports installed and loaded, with
      nothing in either log saying so. The two bounds are therefore not redundant; they bound
      different failure modes, and only this one bounds the silent-death one.

      Enforced IN-PROCESS rather than by the unit file, which is what lets ``K4c`` inherit it:
      launchd has no ``RuntimeMaxSec`` and macOS has no ``timeout`` binary, so a unit-file bound
      would be Linux-only and leave macOS holed — one requirement, two enforcement models, the trap
      ``K4c`` is already warned about for confinement.
    - **``--consolidate`` defaults ON, and it is the difference between a seat that compounds and
      one that decays** (K4a [6]). ``levain wrap`` is human-gated by invocation, so before this a
      scheduled seat captured episodes forever and never metabolized them — and an agent whose raw
      episodes only ever accumulate is **worse than stateless**, because its recall degrades as it
      runs. Defaulting it OFF would ship a seat that looks healthy for a week and quietly gets worse
      the whole time, which is the failure mode this keystone exists to remove. It runs as a SECOND
      bounded phase after the turn, with its own ``--consolidate-max-seconds`` so a long turn cannot
      starve it — and therefore a seat's worst-case PROCESS lifetime is the SUM of the two bounds,
      which is what the install banner checks the cadence against.

    - **The entity path is POSITIONAL** (``levain run <path>``), not ``--path``. Resolved absolute,
      because a login-launched unit has no stable cwd.

    NOTE: this does not validate that ``entity_path`` is a real OpenHands entity — ``daemon.py`` is
    a stdlib-only leaf and must not import the run/confinement layer. The CLI validates before
    installing; an unvalidated seat would otherwise fail identically every interval, forever, into
    a log nobody is watching.
    """
    entity_path = entity_path.expanduser().resolve()
    invocation = _levain_invocation()
    # `--unattended` is EMITTED, not inferred, so the seat's governance posture is auditable in
    # the unit file itself: anyone reading the plist sees that this drive declares no human in the
    # loop, and therefore that the standard credential stores are denied by default. Inferring it
    # from "is there a StartInterval" would put the security-relevant fact somewhere an auditor of
    # the argv cannot see.
    argv = [*invocation, "run", str(entity_path), "--task", task, "--unattended"]
    if model:
        argv += ["--model", model]
    if max_iterations is not None:
        argv += ["--max-iterations", str(max_iterations)]
    if max_seconds is not None:
        # EMITTED into the argv, like `--unattended`, so the seat's time bound is auditable in the
        # unit file itself. A bound that lived only in a default inside the binary would leave an
        # auditor of the plist unable to tell a bounded seat from an unbounded one — and "is this
        # seat restartable" is exactly the question a plist reader is trying to answer.
        # `%g` so a whole number of seconds renders as `1800`, not `1800.0`.
        argv += ["--max-seconds", f"{max_seconds:g}"]
    if consolidate:
        # EMITTED, like `--unattended` and `--max-seconds`, and for the strongest version of that
        # reason yet: this flag is what permits the entity to REWRITE ITS OWN MEMORY with nobody
        # present. Inferring it from "this is a seat" would bury the single most governance-relevant
        # fact about the unit somewhere an auditor reading the plist cannot see it. Someone asking
        # "does this thing modify its own identity while I sleep?" must be able to answer from the
        # argv alone. (What it may NOT do is crystallize — that bound rides `--unattended`, also in
        # the argv, and is enforced structurally rather than by this flag's absence.)
        argv += ["--consolidate"]
        if consolidate_every is not None:
            argv += ["--consolidate-every", str(consolidate_every)]
        if consolidate_max_seconds is not None:
            argv += ["--consolidate-max-seconds", f"{consolidate_max_seconds:g}"]
    logs = (log_dir or _default_log_dir()).expanduser().resolve()
    return DaemonSpec(
        label=label,
        argv=argv,
        working_dir=entity_path,
        env=_daemon_env(invocation),
        stdout_log=logs / f"{label}.log",
        stderr_log=logs / f"{label}.err",
        run_at_login=False,
        keep_alive=False,
        start_interval=interval,
    )


# --- the provider interface ------------------------------------------------------------------

class DaemonProvider(ABC):
    """One thin provider per OS. ``render_unit`` is PURE (no I/O) so the generated unit is fully
    testable without touching the system; ``install``/``uninstall``/``status``/``restart`` shell
    out to the platform's USER-SCOPE service manager (never a system/root scope)."""

    @abstractmethod
    def render_unit(self, spec: DaemonSpec) -> str:
        """Render ``spec`` into the platform's native unit text (no I/O)."""

    @abstractmethod
    def install(self, spec: DaemonSpec) -> str:
        """Write the unit + register it with the user service manager. Idempotent."""

    @abstractmethod
    def uninstall(self, label: str) -> str:
        """Deregister + remove the unit. A no-op (not an error) if already absent."""

    @abstractmethod
    def status(self, label: str) -> DaemonStatus:
        """Report installed/running state."""

    @abstractmethod
    def would_install(self, spec: DaemonSpec) -> DaemonPlan:
        """DRY-RUN: report what :meth:`install` WOULD do + the TRUE current state, mutating nothing
        (no file write, no service-manager call that changes state). The honesty floor: prove a unit
        file on disk is not proof the service is loaded — read the live state, don't infer it."""

    @abstractmethod
    def restart(self, label: str) -> str:
        """Restart the running service (pick up new code / a crashed instance)."""


def _run(cmd: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise DaemonError(f"`{' '.join(cmd)}` failed (rc={proc.returncode}): {msg}")
    return proc


def _refuse_root() -> None:
    """The per-user / NO-root invariant, enforced STRUCTURALLY (not merely "we don't install a
    system LaunchDaemon"): a daemon op run as root or via sudo would write root-owned files under
    $HOME and target ``gui/0``, breaking the sovereign per-user model. Refuse it up front
    (codex L3 MED). ``geteuid`` is POSIX-only — absent on Windows, where this is a no-op."""
    if getattr(os, "geteuid", lambda: 1)() == 0 or os.environ.get("SUDO_UID"):
        raise DaemonError(
            "refusing to run as root / via sudo — the Levain daemon is a per-user agent and "
            "needs no admin. Re-run as your normal user, without sudo.")


# --- macOS: launchd USER agent ---------------------------------------------------------------

class LaunchdProvider(DaemonProvider):
    """macOS launchd *user* agent (``~/Library/LaunchAgents`` + ``launchctl bootstrap
    gui/$UID``). RunAtLoad=login-start, KeepAlive=crash-survive — the proven choices from
    flow's ``com.claphamdigital.flowbridge`` reference. User-scope only: never a LaunchDaemon,
    never sudo."""

    UNIT_DIR = Path.home() / "Library" / "LaunchAgents"

    def _domain(self) -> str:
        return f"gui/{os.getuid()}"

    def _plist_path(self, label: str) -> Path:
        return self.UNIT_DIR / f"{_valid_label(label)}.plist"

    def render_unit(self, spec: DaemonSpec) -> str:
        # plistlib GENERATES the XML (correct escaping/typing) — never hand-roll plist XML.
        import plistlib

        doc = {
            "Label": spec.label,
            "ProgramArguments": list(spec.argv),
            "WorkingDirectory": str(spec.working_dir),
            "EnvironmentVariables": dict(spec.env),
            "RunAtLoad": spec.run_at_login,
            "KeepAlive": spec.keep_alive,
            "StandardOutPath": str(spec.stdout_log),
            "StandardErrorPath": str(spec.stderr_log),
            # Files the job creates (its logs among them) are this user's alone. Belt and braces: no
            # Levain server prints its launch token to a non-terminal stdout (levain.http_guards).
            "Umask": _PRIVATE_UMASK,
        }
        if spec.start_interval is not None:
            # A PERIODIC seat (K4a): launchd re-runs the job every N seconds and the process is
            # expected to EXIT each time. `KeepAlive` is already False here — DaemonSpec refuses to
            # construct a periodic spec that keeps alive, because the two together silently collapse
            # the interval into a hot loop.
            doc["StartInterval"] = spec.start_interval
            if spec.start_interval < _LAUNCHD_DEFAULT_THROTTLE:
                # launchd will not spawn a job more than once every 10s by DEFAULT
                # (`man launchd.plist`, ThrottleInterval). Without this, a sub-10s interval
                # installs happily and REPORTS its requested cadence while launchd silently
                # enforces ~10s — the unit lying about itself again, one level down. Lower the
                # throttle to match so the reported cadence is TRUE. (codex L3 MEDIUM.)
                doc["ThrottleInterval"] = spec.start_interval
        return plistlib.dumps(doc).decode("utf-8")

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        """Write `data` to `path` atomically — to a same-dir temp then ``os.replace`` (a
        same-filesystem rename), so a crash/partial write can never leave a TRUNCATED unit on disk
        that the next run reads as a valid install (codex+complement+L2)."""
        tmp = path.with_name(f"{path.name}.new.{os.getpid()}")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def _bootstrap_with_retry(self, domain: str, plist_path: Path,
                              attempts: int = 3) -> str | None:
        """bootstrap with a bounded retry — launchd is RACY: a ``bootout``'s teardown can still hold
        the label when the next ``bootstrap`` fires ("Bootstrap failed: 5: Input/output error"), a
        TRANSIENT not a bad def. Retry to ride it. SUCCESS = a bootstrap that RETURNS 0 (the new def
        actually loaded); do NOT treat a visible ``launchctl print`` as success — right after a
        ``bootout`` the OLD registration can still be tearing down, so a visible reg would FALSE-GREEN
        the stale def (codex L3 HIGH). The retry, not a print-probe, rides the race. Returns None on
        success, else the last failure detail."""
        detail = ""
        for i in range(attempts):
            proc = _run(["launchctl", "bootstrap", domain, str(plist_path)], check=False)
            if proc.returncode == 0:
                return None
            detail = (proc.stderr or proc.stdout or f"rc={proc.returncode}").strip()
            if i < attempts - 1:
                time.sleep(1)
        return detail or "bootstrap failed"

    def install(self, spec: DaemonSpec) -> str:
        _refuse_root()
        self.UNIT_DIR.mkdir(parents=True, exist_ok=True)
        _prepare_private_logs(spec)
        plist_path = self._plist_path(spec.label)
        domain = self._domain()
        # TRANSACTIONAL + ATOMIC (the install-honesty floor): a failed bootstrap must neither DESTROY a
        # prior good unit nor leave a half-written one on disk. Back up any prior unit; ATOMIC-swap the
        # new one in (temp + os.replace); bootout; bootstrap WITH RETRY (the "Bootstrap failed: 5"
        # teardown race is transient, not a bad def). On a GENUINE reject (it survives the retries):
        # roll back to the prior unit (bootout first to clear a partial registration); on a FIRST
        # install KEEP the new unit — it's valid (render_unit produced it + plist is well-formed), so
        # macOS RunAtLoad self-heals it at next login, and DELETING a valid unit would regress autostart
        # on a transient/no-domain failure (the regression codex+L2 caught). Never delete a valid unit.
        prior = plist_path.read_bytes() if plist_path.exists() else None
        self._atomic_write(plist_path, self.render_unit(spec).encode("utf-8"))
        # Idempotent: bootout any existing instance first (a stale/loaded unit makes bootstrap
        # fail with "service already loaded"); ignore its failure when nothing is loaded.
        _run(["launchctl", "bootout", domain, str(plist_path)], check=False)
        failure = self._bootstrap_with_retry(domain, plist_path)
        if failure is not None:
            if prior is not None:
                _run(["launchctl", "bootout", domain, str(plist_path)], check=False)  # clear partial reg
                self._atomic_write(plist_path, prior)                                 # roll back the prior def
                self._bootstrap_with_retry(domain, plist_path)                        # best-effort reload
                raise DaemonError(
                    f"bootstrap of the new unit failed: {failure} — rolled back to the prior installed "
                    f"unit at {plist_path}")
            # The self-heal story DIFFERS by shape, and stating the wrong one sends the operator
            # to wait for a run that will never happen. A resident unit has RunAtLoad=True and
            # genuinely retries at next login; a SEAT has RunAtLoad=False (installing a schedule
            # is not a request to run now), so login does nothing for it — it needs the unit
            # loaded, after which the timer fires. (glm L3 MEDIUM.)
            heal = ("macOS RunAtLoad will retry it at next login" if spec.run_at_login else
                    f"NOTE: this seat has RunAtLoad=False, so logging in will NOT start it — "
                    f"re-run the install (or `launchctl bootstrap`) to load it, after which the "
                    f"{spec.start_interval}s timer fires")
            raise DaemonError(
                f"bootstrap failed: {failure} — the unit is KEPT at {plist_path} (it is valid; "
                f"{heal}). A valid unit is not deleted on a transient/no-domain failure.")
        if spec.start_interval is None:
            # RESIDENT service: kickstart so it's running NOW (bootstrap + RunAtLoad would
            # otherwise start it only at next login).
            _run(["launchctl", "kickstart", "-k", f"{domain}/{spec.label}"], check=False)
        # VERIFY-don't-assume: report the ACTUAL run state. A kickstart that silently failed, or a
        # serve that crashed on a bad install dir, must NOT read as a false green (codex L3 MED).
        st = self.status(spec.label)
        if spec.start_interval is not None:
            # A PERIODIC seat is EXPECTED to be not-running between its turns, so the resident
            # service's "NOT yet running → go check the log" line is a FALSE ALARM here — the
            # mirror-image of a false green, and just as much a lying instrument. Report the
            # CADENCE instead, and say plainly that idle is the healthy state. We also do NOT
            # kickstart a seat: installing a schedule is not a request to run a real model turn
            # right now (it would spend tokens, and could halt gated, at install time).
            run_line = (
                f"scheduled every {spec.start_interval}s — idle between turns is NORMAL "
                f"(now: {st.detail}). Force a turn with `levain daemon restart`; "
                f"activity lands in {spec.stdout_log} and DECISIONS (gated halts) in "
                f"{spec.stderr_log}"
            )
        else:
            run_line = (f"running ({st.detail})" if st.running
                        else f"NOT yet running ({st.detail}) — check the log at {spec.stdout_log}")
        return (f"installed {spec.label}\n  unit:   {plist_path}\n"
                f"  domain: {domain} (per-user, no sudo)\n  status: {run_line}")

    def uninstall(self, label: str) -> str:
        _refuse_root()
        domain = self._domain()
        plist_path = self._plist_path(label)
        _run(["launchctl", "bootout", domain, str(plist_path)], check=False)
        existed = plist_path.exists()
        plist_path.unlink(missing_ok=True)
        return (f"uninstalled {label} (removed {plist_path})" if existed
                else f"{label} was not installed (no plist at {plist_path})")

    def status(self, label: str) -> DaemonStatus:
        domain = self._domain()
        installed = self._plist_path(label).exists()
        proc = _run(["launchctl", "print", f"{domain}/{label}"], check=False)
        if proc.returncode != 0:
            # rc != 0 means the service print failed — but that is EITHER genuinely-not-loaded OR an
            # unreadable domain (ssh / no Aqua session). Probe the domain to tell them apart: a false
            # "not loaded" when we simply can't SEE the domain is the no-data≠no-event violation
            # (codex+L2 HIGH). domain readable + service absent = not-loaded; domain unreadable = unknown.
            domain_ok = _run(["launchctl", "print", domain], check=False).returncode == 0
            if domain_ok:
                return DaemonStatus(installed=installed, running=False, detail="not loaded",
                                    load_state="not-loaded")
            return DaemonStatus(installed=installed, running=False,
                                detail=f"unknown (cannot read {domain})", load_state="unknown")
        # rc==0 means LOADED, not running (codex L3 MED). The robust cross-version "actually
        # running" signal is a LIVE PID — launchctl prints `pid = N` only while a process exists.
        # The `state = ` STRING varies by macOS/job-type ("running" / "active" / ...), so don't key
        # on it (L4-live: a live job printed `state = active`, not "running" -> a string check was a
        # false NEGATIVE). A throttled crash-loop between respawns has NO pid -> running=False; a
        # nonzero `last exit code` surfaces a flapping job that has a transient pid.
        state = pid = last_exit = None
        for ln in proc.stdout.splitlines():
            s = ln.strip()
            if s.startswith("state = "):
                state = s.split("=", 1)[1].strip()
            elif s.startswith("pid = "):
                pid = s.split("=", 1)[1].strip()
            elif s.startswith("last exit code = "):
                last_exit = s.split("=", 1)[1].strip()
        running = pid is not None and pid.isdigit()
        detail = f"state = {state}" if state else "loaded"
        if pid:
            detail += f", pid = {pid}"
        if last_exit not in (None, "0"):
            detail += f", last exit = {last_exit}"
        return DaemonStatus(installed=installed, running=running, detail=detail,
                            load_state="running" if running else "loaded")

    def would_install(self, spec: DaemonSpec) -> DaemonPlan:
        # DRY-RUN — render the unit, diff it against any on-disk unit, and read the TRUE live state,
        # WITHOUT writing a file or calling a state-changing launchctl verb (status() only calls
        # `launchctl print`, which is read-only). Proves the honesty floor: an unchanged unit that is
        # NOT loaded still needs a (re-)bootstrap, so "on disk" can never read as "installed + loaded".
        plist_path = self._plist_path(spec.label)
        on_disk = plist_path.exists()
        rendered = self.render_unit(spec)
        try:
            existing = plist_path.read_text(encoding="utf-8") if on_disk else None
        except OSError:
            existing = None        # unreadable on-disk unit → treat as a change (a real reinstall)
        would_change = existing != rendered
        current = self.status(spec.label)
        if not on_disk:
            action = "FRESH INSTALL — write the unit + bootstrap"
        elif would_change:
            action = "REINSTALL — the unit changed; back up, atomic-swap, re-bootstrap"
        elif current.load_state == "running":
            action = "no-op — unit unchanged and the service is running"
        elif current.load_state == "loaded":
            # "idle" is the healthy state for a periodic seat BETWEEN turns — and it is also what
            # a seat that has failed every interval for three days looks like. Reporting a bare
            # "idle" as the one-line verdict turns a chronic failure into a clean bill of health
            # in the exact command an operator uses as a pre-flight check. The last exit code is
            # already in `detail`; surface it in the ACTION too. (glm L3 LOW.)
            failing = ("last exit = " in current.detail)
            action = ("no-op — unit unchanged and loaded, but ⚠ its LAST RUN FAILED "
                      f"({current.detail}) — check the seat's logs"
                      if failing else "no-op — unit unchanged and loaded (idle)")
        elif current.load_state == "unknown":
            # can't read the service-manager domain (ssh / no Aqua) — don't claim a load state we
            # can't see (the honesty floor: no-data ≠ not-loaded).
            action = "UNKNOWN — unit unchanged, but the service-manager state can't be read"
        else:  # not-loaded
            # unit on disk + unchanged BUT not actually loaded — the honesty floor: a file is not a
            # loaded service, so install would still (re-)bootstrap it.
            action = "RE-BOOTSTRAP — unit unchanged but NOT loaded (a file on disk is not a loaded service)"
        return DaemonPlan(label=spec.label, unit_path=plist_path, on_disk=on_disk,
                          would_change=would_change, current=current, action=action)

    def restart(self, label: str) -> str:
        _refuse_root()
        domain = self._domain()
        _run(["launchctl", "kickstart", "-k", f"{domain}/{_valid_label(label)}"], check=True)
        return f"restarted {label}"


# --- Linux: systemd --user units -------------------------------------------------------------

_SYSTEMD_USER_UNIT_DIR = Path.home() / ".config" / "systemd" / "user"


def _systemd_refuse_control(value: str, *, field: str) -> str:
    r"""Refuse a control character in ANY value that reaches a unit directive. Returns it unchanged.

    ⛔ **THIS IS SPLIT OUT OF ``_systemd_escape`` BECAUSE THE REFUSAL AND THE ESCAPING ARE DIFFERENT
    JOBS AND ONLY ONE OF THEM WAS EVERYWHERE IT NEEDED TO BE** (Diogenes HIGH, 2026-09-04). The
    guard was applied to ``Environment=`` — whose values levain GENERATES (``PATH``/``HOME``/
    ``PYTHONUNBUFFERED``, the least attacker-reachable input in the function) — and omitted from
    ``ExecStart``, which carries ``--task``: **free text the OPERATOR types**, ``required=True`` at
    the CLI. The one field a guard was written for was the one field it did not cover.

    ``shlex.quote`` is SHELL quoting, not systemd's, and it does not help: it wraps the argument in
    single quotes and closes them at the END, so an embedded newline leaves every line but the last
    inside an open quote. RUN-VERIFIED against real systemd 255 (``tests/linux/Dockerfile``): an
    ordinary multi-line ``--task`` renders an ``ExecStart`` truncated mid-task, with
    ``--unattended``, ``--max-seconds`` and ``--consolidate`` — **the bounds on an unattended
    agent** — on an orphan line outside every directive.

    ▶ WHAT SYSTEMD ACTUALLY DOES WITH IT, measured rather than assumed, because the original finding
    left this undetermined and both answers were bad: systemd **REFUSES** the unit —
    ``Unbalanced quoting, ignoring: …`` then ``Unit configuration has fatal error, unit will not be
    started``, exit 1. So the agent does not run stripped of its bounds; it does not run at all.
    Three task shapes chosen to try to produce a balanced first line all give the same fatal result.

    ⚠ AND THAT IS *WORSE* THAN IT SOUNDS FOR A PERIODIC SEAT, WHICH IS WHY THIS FAILS AT RENDER TIME
    RATHER THAN AT INSTALL TIME. ``install()`` enables ``<label>.timer``, never the service, and
    ``systemd-analyze verify`` on the TIMER **exits 0** while printing the service's fatal error —
    measured, same host, same units. Nothing in a periodic install's path fails, so the seat installs
    "successfully" and dies at its first fire, in the journal. Refusing here means the unloadable
    unit is never written, which closes that silent path at its only known trigger instead of adding
    a verification step that would have to be remembered.

    macOS is unaffected and that is exactly why this survived: ``LaunchdProvider`` renders the same
    spec into a plist ``<string>`` where the newline is inert — verified side by side on the
    identical spec."""
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise DaemonError(
            f"refusing to render a systemd unit: {field} contains a control character "
            f"({value!r}). A newline or control character ENDS the directive and puts everything "
            f"after it outside every unit setting — for ExecStart that silently strips the bounds "
            f"on an unattended agent, and systemd then refuses to load the unit at all. "
            f"Fail-closed. Use a single-line value."
        )
    return value


def _systemd_escape(value: str) -> str:
    r"""Escape a value for a systemd unit's double-quoted string (``Environment="K=V"``).

    systemd's unit parser treats ``\`` as an escape and ``"`` as a quote delimiter, so both are
    escaped. The control-character REFUSAL is :func:`_systemd_refuse_control` — separate because
    every value reaching a directive needs the refusal, and only the double-quoted ones need this
    escaping (FAIL CLOSED — the seatbelt provider makes the identical choice for the identical
    reason)."""
    return (_systemd_refuse_control(value, field="an Environment value")
            .replace("\\", "\\\\").replace('"', '\\"'))


def _systemd_exec_arg(value: str) -> str:
    r"""One ``ExecStart`` argument. ``shlex.quote`` is not enough, MEASURED 2026-09-30 on systemd 255
    with a unit that printed its own argv: systemd still expanded ``%n`` (a specifier) and
    ``${PATH}`` and turned ``a\nb`` into a real newline inside single quotes. Doubling ``\``, ``%`` and
    ``$`` after quoting delivered all five probe arguments byte-exact."""
    quoted = shlex.quote(_systemd_refuse_control(value, field="an ExecStart argument"))
    return quoted.replace("\\", "\\\\").replace("%", "%%").replace("$", "$$")


def _systemd_specifiers(value: str) -> str:
    """A path or value in a directive systemd runs specifier expansion on: ``%`` is doubled."""
    return value.replace("%", "%%")


class SystemdUserProvider(DaemonProvider):
    """Linux ``systemd --user`` provider (K4c) — the lifecycle half, and it is the half that is
    easy to miss: without it there is no SCHEDULED Linux seat at all, only confinement with nothing
    supervising it.

    Per-user and sudo-free, exactly like the launchd provider: units land in
    ``~/.config/systemd/user`` and every verb is ``systemctl --user``. Never a system unit.

    ⚠ **THE CADENCE LIVES IN A SEPARATE UNIT, AND THAT IS THE RE-DERIVATION TRAP THIS PROVIDER
    EXISTS TO AVOID.** On launchd, ``StartInterval`` is one more key in the same plist, so
    ``DaemonSpec`` naturally reads as "one spec, one unit". systemd has no such key: a periodic job
    is a ``.service`` (what to run) PLUS a ``.timer`` (when), and they are different unit types with
    different lifecycles. So a periodic spec renders TWO files here, ``status`` reports on the TIMER
    (the thing that is supposed to be active, while the service is correctly inactive between turns),
    and ``uninstall`` must remove both or the next install inherits an orphan timer. Rendering the
    interval into the service — the shape a launchd-derived reading suggests — produces a unit that
    installs cleanly and never fires.

    ⚠ **LINGERING IS WHAT MAKES AN ALWAYS-ON AGENT ACTUALLY ALWAYS-ON.** By default a user's systemd
    instance stops when their last session ends, so a headless box's seat would die on logout — the
    precise contradiction ``spore-418`` names ("an always-on agent that dies when the laptop lid
    closes is a contradiction in terms"), reappearing as a session lifetime instead of a lid.
    ``install`` requests ``loginctl enable-linger`` and REPORTS whether it was granted rather than
    assuming it; a refusal is surfaced in the install summary, never swallowed."""

    UNIT_DIR = _SYSTEMD_USER_UNIT_DIR

    # ---- paths -------------------------------------------------------------------------------

    def _service_path(self, label: str) -> Path:
        return self.UNIT_DIR / f"{_valid_label(label)}.service"

    def _timer_path(self, label: str) -> Path:
        return self.UNIT_DIR / f"{_valid_label(label)}.timer"

    def _is_periodic_on_disk(self, label: str) -> bool:
        """A seat is identified by its TIMER existing. ``status``/``uninstall`` receive only a label,
        never the spec, so the on-disk shape is the only thing that can say which kind of unit this
        is — and reporting a seat's *service* state would call a healthy idle seat "inactive"."""
        return self._timer_path(label).exists()

    # ---- rendering (PURE) --------------------------------------------------------------------

    def render_unit(self, spec: DaemonSpec) -> str:
        """The ``.service`` unit. For a periodic spec this deliberately carries NO schedule — see
        :meth:`render_timer`."""
        periodic = spec.start_interval is not None
        # ⛔ EVERY VALUE THAT REACHES A DIRECTIVE, not just the Environment ones (Diogenes HIGH,
        # 2026-09-04). The guard covered the field levain GENERATES and skipped the field the
        # OPERATOR TYPES. These are refusal-only: they are not inside a double-quoted systemd
        # string, so they need no escaping — which is precisely why the two halves were split.
        label = _systemd_refuse_control(spec.label, field="the unit label")
        working_dir = _systemd_refuse_control(str(spec.working_dir), field="WorkingDirectory")
        stdout_log = _systemd_refuse_control(str(spec.stdout_log), field="StandardOutput")
        stderr_log = _systemd_refuse_control(str(spec.stderr_log), field="StandardError")
        argv = " ".join(_systemd_exec_arg(a) for a in spec.argv)
        lines = [
            "[Unit]",
            f"Description=Levain {label}",
            # A user unit that starts at "login" should come up with the user session; for a linger-
            # enabled headless box this is also what boots it.
            "After=default.target",
            "",
            "[Service]",
            # oneshot for a seat (it runs one bounded turn and EXITS, and systemd must not treat that
            # exit as a crash); simple + Restart for the resident cockpit.
            "Type=oneshot" if periodic else "Type=simple",
            f"ExecStart={argv}",
            f"WorkingDirectory={_systemd_specifiers(working_dir)}",
        ]
        for key, value in spec.env.items():
            lines.append(f'Environment="{_systemd_specifiers(_systemd_escape(key))}='
                         f'{_systemd_specifiers(_systemd_escape(value))}"')
        lines += [
            # `append:` matches launchd's StandardOutPath/StandardErrorPath semantics — accumulate
            # rather than truncate, so a crash loop's evidence survives. DOCUMENTED as systemd 240+
            # (systemd.exec(5)); what was actually VERIFIED here is that `systemd-analyze verify`
            # accepts it on systemd 255, which is what Ubuntu 24.04 ships. An older systemd is
            # untested by us — the distinction matters because the failure would be at unit LOAD.
            f"StandardOutput=append:{_systemd_specifiers(stdout_log)}",
            f"StandardError=append:{_systemd_specifiers(stderr_log)}",
            # The launchd Umask analogue: what the unit creates is this user's alone.
            f"UMask={_PRIVATE_UMASK:04o}",
        ]
        if not periodic and spec.keep_alive:
            # The launchd KeepAlive analogue: survive a crash. Deliberately NOT set for a seat —
            # `DaemonSpec` already refuses a periodic spec with keep_alive, and on systemd a Restart
            # on a oneshot is how a seat becomes a hot loop.
            lines += ["Restart=always", "RestartSec=5"]
        if periodic:
            # A seat's service is started by its TIMER only. With an [Install] section it could be
            # enabled on its own (a resident -> periodic reinstall left exactly that) and start at
            # login beside the timer (complement, L3 2026-09-30).
            lines.append("")
        else:
            lines += ["", "[Install]", "WantedBy=default.target", ""]
        return "\n".join(lines)

    def render_timer(self, spec: DaemonSpec) -> str | None:
        """The ``.timer`` unit for a periodic seat, or None for a resident service.

        ``OnUnitActiveSec`` measures from when the service last became ACTIVE — the START of the last
        run, start to start, like launchd's ``StartInterval`` ("every N seconds"). A run longer than
        the interval is followed by the next as soon as it exits, never overlapping it (codex +
        complement, L3 2026-09-30: this text used to say "from the END"). ``OnActiveSec`` gives the
        first run one interval after the timer starts, so installing a schedule does not
        immediately spend a model turn. ``AccuracySec`` is pinned to 1s rather than left to systemd's
        default, which systemd.timer(5) DOCUMENTS as one minute — ⚠ documented, not measured here
        (the man pages were not installed on the box these units were verified on). The reason does
        not depend on the exact default: any accuracy window wider than the interval means a short
        cadence installs happily and silently fires on a different schedule than the one the operator
        asked for, the unit lying about itself exactly as an unthrottled sub-10s launchd job does.
        Pinning it makes the reported cadence TRUE regardless of what the default happens to be. ``Persistent`` is deliberately NOT set: a seat that missed turns while the box was off
        must not stampede them all on boot."""
        if spec.start_interval is None:
            return None
        # The label reaches TWO directives here (Description and Unit=), and `Unit=` is the one that
        # names the service this timer fires. Refused for the same reason as in `render_unit`.
        label = _systemd_refuse_control(spec.label, field="the unit label")
        return "\n".join([
            "[Unit]",
            f"Description=Levain {label} schedule",
            "",
            "[Timer]",
            f"OnActiveSec={spec.start_interval}",
            f"OnUnitActiveSec={spec.start_interval}",
            f"Unit={label}.service",
            "AccuracySec=1s",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ])

    # ---- helpers -----------------------------------------------------------------------------

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        """Same atomicity floor as the launchd provider: temp + ``os.replace`` in the same directory,
        so a crash can never leave a TRUNCATED unit that systemd would parse as a valid one."""
        tmp = path.with_name(f"{path.name}.new.{os.getpid()}")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def _primary_unit(self, label: str) -> str:
        """The unit whose state ANSWERS "is this thing working" — the timer for a seat, the service
        for a resident cockpit."""
        return f"{label}.timer" if self._is_periodic_on_disk(label) else f"{label}.service"

    def _show(self, unit: str, props: str) -> dict[str, str]:
        proc = _run(["systemctl", "--user", "show", unit, "-p", props], check=False)
        if proc.returncode != 0:
            return {}
        out: dict[str, str] = {}
        for ln in proc.stdout.splitlines():
            if "=" in ln:
                k, v = ln.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    PROC = Path("/proc")   # a class attribute so tests can point it at a fake process table

    def _turn_runs_another_argv(self, main_pid: str, spec: DaemonSpec,
                                prior_service: bytes | None) -> bool:
        """Is the running turn executing something other than ``spec.argv``? Read from the turn's
        own ``/proc/<pid>/cmdline``, not inferred from whether this install changed the unit file:
        a second reinstall of the same new definition, or a turn the timer started after the
        reload, both fooled the byte comparison (L3 2026-10-01, complement + codex). Falls back to
        that comparison only when the process cannot be read."""
        try:
            pid = int(main_pid)
            if pid <= 0:
                return False
            running = (self.PROC / str(pid) / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
            decoded = [a.decode("utf-8", "surrogateescape") for a in running]
            # Compare the TAIL. When the argv is the `levain` console script, the kernel execs its
            # shebang, so the cmdline reads `<interpreter> [shebang-arg] <script> <args...>` and
            # never equals the argv itself; an exact match flagged every identical reinstall as
            # "another argv" (Diogenes MEDIUM 2026-10-02, run in the Linux container).
            return decoded[-len(spec.argv):] != list(spec.argv)
        except (ValueError, OSError):
            return prior_service is not None and prior_service != self.render_unit(spec).encode()

    def _enable_linger(self) -> str:
        """Ask for lingering and REPORT the answer. Never assumes it was granted — a polkit-denied
        linger on a locked-down box would otherwise leave an operator with a unit that looks
        installed and dies at logout."""
        proc = _run(["loginctl", "enable-linger"], check=False)
        if proc.returncode == 0:
            return "lingering enabled (the seat survives logout)"
        detail = (proc.stderr or proc.stdout or f"rc={proc.returncode}").strip()
        return (f"⚠ could NOT enable lingering ({detail}) — the unit is installed, but a systemd "
                f"--user instance stops when your last session ends, so this will NOT run while you "
                f"are logged out. Fix with `sudo loginctl enable-linger $USER`.")

    # ---- verbs -------------------------------------------------------------------------------

    @staticmethod
    def _systemd_version() -> int | None:
        proc = _run(["systemctl", "--version"], check=False)
        m = re.match(r"systemd (\d+)", proc.stdout or "")
        return int(m.group(1)) if m else None

    def install(self, spec: DaemonSpec) -> str:
        _refuse_root()
        # `StandardOutput=append:` needs systemd 240+ (systemd.exec(5)); an older one rejects the unit
        # at LOAD, after it is written and enabled (codex, L3 2026-09-30). Refuse up front instead.
        version = self._systemd_version()
        if version is not None and version < 240:
            raise DaemonError(
                f"systemd {version} is too old: levain's units need systemd 240 or newer "
                "(StandardOutput=append:). Nothing was written."
            )
        self.UNIT_DIR.mkdir(parents=True, exist_ok=True)
        _prepare_private_logs(spec)

        service_path = self._service_path(spec.label)
        timer_path = self._timer_path(spec.label)
        timer_text = self.render_timer(spec)

        prior_service = service_path.read_bytes() if service_path.exists() else None
        prior_timer = timer_path.read_bytes() if timer_path.exists() else None

        self._atomic_write(service_path, self.render_unit(spec).encode("utf-8"))
        if timer_text is not None:
            self._atomic_write(timer_path, timer_text.encode("utf-8"))
            if prior_timer is None and prior_service is not None:
                # SHAPE CHANGE, resident -> periodic: the resident service is still enabled at login
                # and still running. Stop and disable it, or it runs beside its own timer.
                _run(["systemctl", "--user", "disable", "--now", f"{spec.label}.service"],
                     check=False)
        elif timer_path.exists():
            # SHAPE CHANGE, periodic -> resident. An orphan timer left here would keep firing the
            # service on the OLD cadence alongside the new resident unit — two schedulers driving one
            # service, which is the kind of thing that reads as "it randomly restarts".
            _run(["systemctl", "--user", "disable", "--now", f"{spec.label}.timer"], check=False)
            timer_path.unlink(missing_ok=True)

        _run(["systemctl", "--user", "daemon-reload"], check=False)
        target = f"{spec.label}.timer" if timer_text is not None else f"{spec.label}.service"
        # `enable --now` STARTS an inactive unit and leaves an ACTIVE one alone, so a reinstall with a
        # new task, bound or cadence kept the old process or the old schedule while the summary said
        # it was installed (codex + complement, L3 2026-09-30). `restart` applies the new definition
        # either way: it starts an inactive unit and restarts an active one (re-arming a timer).
        proc = _run(["systemctl", "--user", "enable", target], check=False)
        if proc.returncode == 0:
            proc = _run(["systemctl", "--user", "restart", target], check=False)
        if proc.returncode != 0:
            failure = (proc.stderr or proc.stdout or f"rc={proc.returncode}").strip()
            if prior_service is not None:
                rollback_target = (f"{spec.label}.timer" if prior_timer is not None
                                   else f"{spec.label}.service")
                if target != rollback_target:
                    # A shape change: disable what the failed install enabled, BEFORE its unit
                    # file is replaced, while the [Install] section `enable` read is still on disk
                    # (complement, L3 2026-10-02). For periodic -> resident that is the service,
                    # whose default.target.wants symlink the restored periodic file (no [Install])
                    # would leave behind, starting it at login beside its restored timer (Diogenes
                    # LOW 2026-10-02). For resident -> periodic it is the timer, disabled again below.
                    # No --now: if `enable` itself failed, a turn the OLD timer started may be
                    # running, and stopping it would lose that work (codex, L3 r2 2026-10-02).
                    _run(["systemctl", "--user", "disable", target], check=False)
                # ROLL BACK to the prior good definition — the same transactional floor as launchd.
                self._atomic_write(service_path, prior_service)
                # ⛔ ROLL THE TIMER BACK TO ITS PRIOR **STATE**, NOT ITS PRIOR **CONTENT**, AND
                # RECOMPUTE THE TARGET FROM THE RESTORED SHAPE (Diogenes MEDIUM, 2026-09-04).
                # The old form restored the timer only `if prior_timer is not None`, so a
                # resident -> periodic upgrade — where there WAS no prior timer — left the NEW
                # timer on disk while the service was rolled back to the resident one, and then
                # re-enabled that timer because `target` was still computed from the FAILED spec.
                # That arrives at precisely the "two schedulers driving one service" state the
                # shape-change branch thirty lines up exists to prevent, while telling the
                # operator the rollback succeeded. The MIRROR case was broken the other way:
                # periodic -> resident restored the timer FILE and never re-enabled it, leaving a
                # disabled timer that still captures `_primary_unit`, and a timer is MainPID=0
                # forever — which this provider's own comments say must not be read as a run signal.
                # ⚠ Both are the same error: the rollback restored FILES and left the systemd
                # ENABLEMENT describing a shape that no longer exists on disk.
                if prior_timer is not None:
                    self._atomic_write(timer_path, prior_timer)
                else:
                    # There was no timer before this install. If we wrote one, it is ours and it
                    # must go — disable first, because unlinking a file does not un-enable the
                    # symlink systemd already created.
                    _run(["systemctl", "--user", "disable", "--now", f"{spec.label}.timer"],
                         check=False)
                    timer_path.unlink(missing_ok=True)
                _run(["systemctl", "--user", "daemon-reload"], check=False)
                back = _run(["systemctl", "--user", "enable", "--now", rollback_target], check=False)
                # Say what the re-enable did; it is not checked anywhere else (L3 codex, 2026-10-03).
                if back.returncode == 0:
                    restored = f"re-enabled {rollback_target}"
                else:
                    why = (back.stderr or back.stdout or f"rc={back.returncode}").strip()
                    restored = (f"re-enabling {rollback_target} FAILED ({why}); the prior unit is on "
                                f"disk but NOT enabled or running")
                raise DaemonError(
                    f"enabling the new unit failed: {failure} — rolled back to the prior installed "
                    f"unit at {service_path} ({restored})")
            raise DaemonError(
                f"enabling failed: {failure} — the unit is KEPT at {service_path}. "
                # ⚠ THIS USED TO SAY "(it is valid)". Nothing here checks that, and systemd will
                # refuse a unit for reasons this code never inspects — an asserted property in the
                # sentence an operator reads while deciding whether to trust the file on disk.
                f"levain RENDERED it and is not deleting it on what may be a transient failure; "
                f"`systemd-analyze verify {service_path}` says whether systemd accepts it. "
                f"Re-run the install once the cause is fixed.")

        linger = self._enable_linger()
        st = self.status(spec.label)
        if timer_text is not None:
            run_line = (
                f"scheduled every {spec.start_interval}s — idle between turns is NORMAL "
                f"(now: {st.detail}). Force a turn with `levain daemon restart`; activity lands in "
                f"{spec.stdout_log} and DECISIONS (gated halts) in {spec.stderr_log}"
            )
            # Restarting the TIMER does not touch a turn already running, and systemd applies a
            # changed unit only at the next start. Measured 2026-10-01 on argushub (systemd 255): a
            # reinstall during a turn left it running the OLD task to completion, while this summary
            # printed that turn's pid as if it were the idle timer. Say so; do not kill the turn.
            svc = self._show(f"{spec.label}.service", "ActiveState,MainPID")
            if (svc.get("ActiveState") in ("activating", "active", "deactivating")
                    and self._turn_runs_another_argv(svc.get("MainPID", ""), spec,
                                                     prior_service)):
                run_line += (
                    f"\n  ⚠ a turn that started BEFORE this install is still running (pid "
                    f"{svc.get('MainPID', '?')}) on the PREVIOUS definition; it finishes on that, and "
                    f"the next turn uses the new one. `levain daemon restart` starts a new-definition "
                    f"turn now, stopping that one."
                )
        else:
            run_line = (f"running ({st.detail})" if st.running
                        else f"NOT yet running ({st.detail}) — check the log at {spec.stdout_log}")
        units = f"{service_path}" + (f"\n          {timer_path}" if timer_text is not None else "")
        return (f"installed {spec.label}\n  unit:   {units}\n"
                f"  domain: systemd --user (per-user, no sudo)\n"
                f"  linger: {linger}\n  status: {run_line}")

    def uninstall(self, label: str) -> str:
        _refuse_root()
        service_path = self._service_path(label)
        timer_path = self._timer_path(label)
        # Disable the TIMER first: disabling only the service would leave the timer armed to start a
        # unit that no longer exists, which systemd reports as a failing timer forever.
        for unit in (f"{label}.timer", f"{label}.service"):
            _run(["systemctl", "--user", "disable", "--now", unit], check=False)
        existed = service_path.exists() or timer_path.exists()
        removed = [str(p) for p in (service_path, timer_path) if p.exists()]
        service_path.unlink(missing_ok=True)
        timer_path.unlink(missing_ok=True)
        _run(["systemctl", "--user", "daemon-reload"], check=False)
        return (f"uninstalled {label} (removed {', '.join(removed)})" if existed
                else f"{label} was not installed (no unit at {service_path})")

    def status(self, label: str) -> DaemonStatus:
        installed = self._service_path(label).exists()
        unit = self._primary_unit(label)
        props = self._show(unit, "LoadState,ActiveState,SubState,MainPID,ExecMainStatus")
        if not props:
            # `systemctl --user show` failed outright — no user manager reachable (no session bus,
            # a container without systemd, ssh without linger). That is NO DATA, and reporting it as
            # "not loaded" is the no-data≠no-event violation the launchd provider guards against for
            # an unreadable Aqua domain. Same honesty floor, same wording.
            return DaemonStatus(installed=installed, running=False,
                                detail="unknown (cannot reach the systemd --user manager)",
                                load_state="unknown")
        load_state = props.get("LoadState", "")
        active = props.get("ActiveState", "")
        sub = props.get("SubState", "")
        pid = props.get("MainPID", "0")
        last_exit = props.get("ExecMainStatus", "0")
        if load_state != "loaded":
            return DaemonStatus(installed=installed, running=False,
                                detail=f"not loaded (LoadState={load_state or 'unknown'})",
                                load_state="not-loaded")
        # A live numeric pid is the run signal, exactly as on launchd — NOT the ActiveState string.
        # A TIMER is `active (waiting)` with MainPID=0 forever, which is its HEALTHY state, so a
        # string check would call a working seat "running" and a resident crash-loop "active" too.
        if unit.endswith(".timer"):
            # A timer has no MainPID or exit status of its own; the TURN is the service. Read both
            # from it, or a seat whose every turn fails reads as healthy (codex + complement, L3).
            svc = self._show(f"{label}.service", "MainPID,ExecMainStatus,Result")
            pid = svc.get("MainPID", "0")
            last_exit = svc.get("ExecMainStatus", "0")
            if svc.get("Result", "success") not in ("", "success") and last_exit in ("", "0"):
                last_exit = svc["Result"]
        running = pid.isdigit() and int(pid) > 0
        detail = f"{active} ({sub})" if sub else active or "loaded"
        if running:
            detail += f", pid = {pid}"
        if last_exit not in ("", "0"):
            detail += f", last exit = {last_exit}"
        return DaemonStatus(installed=installed, running=running, detail=detail,
                            load_state="running" if running else "loaded")

    def would_install(self, spec: DaemonSpec) -> DaemonPlan:
        service_path = self._service_path(spec.label)
        timer_path = self._timer_path(spec.label)
        on_disk = service_path.exists()
        rendered = self.render_unit(spec)
        rendered_timer = self.render_timer(spec)
        try:
            existing = service_path.read_text(encoding="utf-8") if on_disk else None
        except OSError:
            existing = None
        try:
            existing_timer = timer_path.read_text(encoding="utf-8") if timer_path.exists() else None
        except OSError:
            existing_timer = None
        # BOTH files are diffed. Comparing only the service would report "no change" for a pure
        # cadence edit — the one field a seat's operator is most likely to be changing.
        would_change = existing != rendered or existing_timer != rendered_timer
        current = self.status(spec.label)
        if not on_disk:
            action = "FRESH INSTALL — write the unit(s) + daemon-reload + enable --now"
        elif would_change:
            action = "REINSTALL — the unit(s) changed; atomic-swap, daemon-reload, re-enable"
        elif current.load_state == "running":
            action = "no-op — unit unchanged and the service is running"
        elif current.load_state == "loaded":
            failing = ("last exit = " in current.detail)
            action = ("no-op — unit unchanged and loaded, but ⚠ its LAST RUN FAILED "
                      f"({current.detail}) — check the seat's logs"
                      if failing else "no-op — unit unchanged and loaded (idle)")
        elif current.load_state == "unknown":
            action = "UNKNOWN — unit unchanged, but the systemd --user manager can't be read"
        else:
            action = ("RE-ENABLE — unit unchanged but NOT loaded (a file on disk is not a loaded "
                      "service)")
        return DaemonPlan(label=spec.label, unit_path=service_path, on_disk=on_disk,
                          would_change=would_change, current=current, action=action)

    def restart(self, label: str) -> str:
        _refuse_root()
        # Restart the SERVICE even for a seat: "force a turn now" is what an operator means, and
        # restarting the timer would merely reset the countdown.
        # restart builds no unit path, so nothing else validates the label here; systemctl reads
        # `*.service` as a glob over every loaded user unit.
        _run(["systemctl", "--user", "restart", f"{_valid_label(label)}.service"], check=True)
        return f"restarted {label}"


def select_provider(system: str | None = None) -> DaemonProvider:
    """The provider for this OS. macOS (launchd user agent) and Linux (systemd ``--user``, K4c)
    ship; Windows (Task Scheduler) remains a planned pure-addition against the same contract.

    ⚠ A PROVIDER IS NOT A GUARANTEE THAT THE SERVICE MANAGER IS REACHABLE. On Linux the units can be
    written while ``systemctl --user`` has no session bus to talk to (ssh without lingering, a
    container), which :meth:`SystemdUserProvider.status` reports as ``unknown`` rather than as
    not-loaded — no-data is not no-event."""
    system = system or platform.system()
    if system == "Darwin":
        return LaunchdProvider()
    if system == "Linux":
        return SystemdUserProvider()
    if system == "Windows":
        raise NotImplementedError(
            "the Task Scheduler (schtasks /SC ONLOGON) provider is a planned pure-addition "
            # ⚠ THIS SENTENCE IS EXECUTABLE CODE, WHICH IS WHY IT OUTLIVED THE SWEEP. The K4c
            # description pass (3ac52a4, "five descriptions still told the operator Linux had no
            # floor") reached five sites and missed the sixth — the only one that is a STRING
            # LITERAL IN A RAISE rather than a comment or a docstring. It found what a reader greps
            # for in prose and not what an operator is actually SHOWN. It read "macOS ships first"
            # while `select_provider`'s own docstring, fourteen lines up, already said macOS AND
            # Linux ship. The ordering clause is dropped rather than corrected: it carries no
            # information a Windows operator can act on.
            "(spore-205). For now run `levain serve --write --no-open` "
            "under your own supervisor.")
    raise NotImplementedError(f"no daemon provider for platform {system!r}")


THREAT_MODEL_NOTE = (
    "An always-on serve is a 24/7 loopback-LOCAL write window behind a per-launch token. The "
    "token is never written to the log: open the cockpit with `levain serve --open-running "
    "--port <port>`, which reads it from ~/.levain-runtime/ (yours only, denied to entities). "
    "Each restart mints a new token. Cross-origin/browser attacks stay blocked. "
    "Off-box (--host <mesh>) is NOT daemonized — an install-bearing serve is loopback-only."
)
