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

THREAT-MODEL (M2): always-on means a 24/7 token-free loopback-LOCAL write window — any *local*
process can POST to the cockpit. That is the same posture as any localhost dev server;
browser/cross-origin attacks stay kernel-blocked (Host allowlist + CSRF + the loopback bind).
Off-box (``--host <mesh>``) is deliberately NOT daemonized here — an install-bearing serve is
loopback-only by construction (its seed/config is operator-private).
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LABEL = "com.levainhq.levain"
DEFAULT_PORT = 7420

# The base PATH the daemon needs: a login-launched supervisor (launchd/systemd) hands the
# process a MINIMAL PATH, so a thin process dies silently when it shells out (the flowbridge
# launchd lesson). We prepend the dir holding the resolved levain/python bin + the user-local
# bin, then these standard dirs.
_BASE_PATH_DIRS = ("/usr/local/bin", "/opt/homebrew/bin", "/opt/homebrew/sbin",
                   "/usr/bin", "/bin", "/usr/sbin", "/sbin")


class DaemonError(RuntimeError):
    """A service-manager command (launchctl/systemctl/schtasks) failed."""


@dataclass(frozen=True)
class DaemonSpec:
    """OS-agnostic description of the always-on serve. A :class:`DaemonProvider` renders this
    into the platform's native unit (plist / systemd unit / scheduled task). Construct it with
    :func:`build_spec`, never by hand — the path/bin/env resolution is shared across OSes."""

    label: str
    argv: list[str]            # full exec: [levain_bin, "serve", "--write", "--no-open", ...]
    working_dir: Path
    env: dict[str, str]        # PATH (minimal-login gotcha) / HOME / PYTHONUNBUFFERED
    stdout_log: Path
    stderr_log: Path
    run_at_login: bool = True
    keep_alive: bool = True


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
        return self.UNIT_DIR / f"{label}.plist"

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
        }
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
        spec.stdout_log.parent.mkdir(parents=True, exist_ok=True)
        spec.stderr_log.parent.mkdir(parents=True, exist_ok=True)
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
            raise DaemonError(
                f"bootstrap failed: {failure} — the unit is KEPT at {plist_path} (it is valid; macOS "
                f"RunAtLoad will retry it at next login). A valid unit is not deleted on a transient/"
                f"no-domain failure.")
        # kickstart so it's running NOW (bootstrap + RunAtLoad starts it at next login otherwise).
        _run(["launchctl", "kickstart", "-k", f"{domain}/{spec.label}"], check=False)
        # VERIFY-don't-assume: report the ACTUAL run state. A kickstart that silently failed, or a
        # serve that crashed on a bad install dir, must NOT read as a false green (codex L3 MED).
        st = self.status(spec.label)
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
            action = "no-op — unit unchanged and loaded (idle)"
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
        _run(["launchctl", "kickstart", "-k", f"{domain}/{label}"], check=True)
        return f"restarted {label}"


def select_provider(system: str | None = None) -> DaemonProvider:
    """The provider for this OS. macOS ships now; Linux/Windows are planned pure-additions
    against the same contract (the interface is here, the providers slot in)."""
    system = system or platform.system()
    if system == "Darwin":
        return LaunchdProvider()
    if system == "Linux":
        raise NotImplementedError(
            "the systemd --user provider is a planned pure-addition (spore-205); macOS ships "
            "first. For now run `levain serve --write --no-open` under your own supervisor.")
    if system == "Windows":
        raise NotImplementedError(
            "the Task Scheduler (schtasks /SC ONLOGON) provider is a planned pure-addition "
            "(spore-205); macOS ships first. For now run `levain serve --write --no-open` "
            "under your own supervisor.")
    raise NotImplementedError(f"no daemon provider for platform {system!r}")


THREAT_MODEL_NOTE = (
    "An always-on serve is a 24/7 loopback-LOCAL write window: any LOCAL process on this "
    "machine can write to the cockpit (no token — the localhost bind + Host/CSRF guards are "
    "the auth, same as any localhost dev server). Cross-origin/browser attacks stay blocked. "
    "Off-box (--host <mesh>) is NOT daemonized — an install-bearing serve is loopback-only."
)
