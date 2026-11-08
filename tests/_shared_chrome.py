"""One headless Chrome for a whole pytest session, for the tests that drive a page through ``tests/_page_drive.mjs``.

Every Chrome launch makes macOS keychain calls that no flag removes, and the browser tests launch Chrome a hundred times a
run. Here the session starts one Chrome (``CHROME_SAFE_FLAGS``, its own profile, a debugging port the OS picks) and hands its
port to the driver as ``LEVAIN_DRIVE_CDP_PORT``; the driver gives each drive its own browser context there.

Never a mass failure: if the Chrome cannot start, or stops answering twice, the session falls back to the driver's own
launch for the rest of the run. The Chrome lives in its own process group and that group is killed at the end of the session
and at interpreter exit (an orphan headless Chrome under PID 1 gets new-window requests routed to it). SIGTERM and SIGHUP
clean up in-process; a SIGKILLed pytest is covered by a small watchdog process (``_shared_chrome_watch.py``) that kills the
Chrome's group and removes the profile once the pytest pid is gone.
``LEVAIN_SHARED_CHROME=0`` turns the whole thing off.
"""
from __future__ import annotations

import atexit
import json
import os
import platform
import pwd
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from tests._chrome_flags import CHROME_FLAGS_FILE, CHROME_SAFE_FLAGS
from tests._page_drive_run import _sweep



DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
_warned: set[str] = set()


def _version_key(name: str) -> tuple[int, ...]:
    """The numeric version at the end of a cache directory name ("mac_arm-156.0.8078.80"); a name without one sorts last."""
    tail = name.rsplit("-", 1)[-1]
    try:
        return tuple(int(part) for part in tail.split("."))
    except ValueError:
        return ()


def _real_home() -> Path:
    """The account's real home, not $HOME (the tests point $HOME at a scratch dir); $HOME when the account is unknown."""
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError):
        return Path(os.environ.get("HOME") or "/nonexistent")


def _warn_once(msg: str) -> None:
    if msg not in _warned:
        _warned.add(msg)
        print(f"_shared_chrome: {msg}", file=sys.stderr)


def _pick_chrome(given: str, *, explicit: bool = False, flags_file: Path = CHROME_FLAGS_FILE, home: Path | None = None,
                 system: str | None = None, machine: str | None = None) -> str:
    """chrome-headless-shell when installed under the flags file's ``headless_shell_root``: full Chrome mints a Secure
    Enclave key in the operator's keychain on every fresh profile and no flag stops it; the shell never does.

    A shell counts only if it is a regular, executable file in a ``-mac-<this host's arch>`` directory (versions compared
    numerically, a stray file skipped); anything else, and any unreadable layout, gives the given Chrome. The given Chrome
    is kept when ``explicit`` (the caller named a Chrome on purpose: ``CHROME`` is set) or ``LEVAIN_FULL_CHROME=1``."""
    if explicit or os.environ.get("LEVAIN_FULL_CHROME") == "1":
        return given
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}.get((machine or platform.machine()).lower())
    if (system or platform.system()) != "Darwin" or arch is None:
        return given
    try:
        raw = json.loads(flags_file.read_text(encoding="utf-8")).get("headless_shell_root")
    except (OSError, ValueError, AttributeError):
        return given
    if raw in (None, ""):
        return given
    if not isinstance(raw, str):
        _warn_once("headless_shell_root is not a string; using the given Chrome")
        return given
    root = (home or _real_home()) / raw[2:] if raw.startswith("~/") else Path(raw)
    try:
        versions = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: _version_key(d.name), reverse=True)
    except OSError:
        return given
    for ver in versions:
        try:
            subs = sorted(d for d in ver.iterdir() if d.is_dir() and d.name.endswith(f"-mac-{arch}"))
        except OSError:
            continue
        for sub in subs:
            binary = sub / "chrome-headless-shell"
            if binary.is_file() and os.access(binary, os.X_OK):
                return str(binary)
    return given


def _session_chrome() -> str:
    """The Chrome the session starts: ``CHROME`` when set (explicit), else the shell if installed, else the default Chrome."""
    named = os.environ.get("CHROME")
    if named:
        named = shutil.which(named) or named   # a command name resolves; a path that resolves to nothing stays as typed and FAILS at launch
    return _pick_chrome(named or DEFAULT_CHROME, explicit=bool(named))


def chrome_skip_reason() -> str | None:
    """None when a browser test should run, else why it skips. An explicit ``CHROME`` never skips: one that names nothing
    must FAIL the test at launch, not fall back to another browser or pass by skipping (CI sets CHROME on purpose)."""
    if os.environ.get("CHROME"):
        return None
    for cand in (DEFAULT_CHROME, shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chromium-browser")):
        if cand and Path(cand).exists():
            return None
    return "Chrome is not installed"


PING = Path(__file__).with_name("_cdp_ping.mjs")
WATCH = Path(__file__).with_name("_shared_chrome_watch.py")
PING_CDP_MS = 2000       # the Browser.getVersion exchange itself, timed inside the ping from after node has started
HEALTH_SECONDS = 10.0    # the whole ping process: node's cold start (seconds on a loaded machine) plus the exchange
START_SECONDS = 20.0

_proc: subprocess.Popen | None = None
_watch: subprocess.Popen | None = None
_handlers_set = False
_profile: str | None = None
_port: int | None = None
_restarts = 0
_disabled = False


def enabled() -> bool:
    return os.environ.get("LEVAIN_SHARED_CHROME", "1") != "0" and not _disabled


def _stop() -> None:
    global _proc, _profile, _port, _watch
    if _watch is not None:   # first: it would remove the profile itself the moment the Chrome group is gone
        try:
            _watch.kill()
            _watch.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        _watch = None
    if _proc is not None:
        _sweep(_proc.pid)
        try:
            _proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    if _profile:
        shutil.rmtree(_profile, ignore_errors=True)
    _proc = _profile = _port = None


def _start() -> bool:
    """Launch the session Chrome; True once DevToolsActivePort names its port."""
    global _proc, _profile, _port
    chrome = _session_chrome()   # read when the session starts: LEVAIN_FULL_CHROME=1 set before the first drive is honoured
    if not Path(chrome).exists():
        return False
    _profile = tempfile.mkdtemp(prefix="levain-shared-chrome-")
    log = os.environ.get("LEVAIN_DRIVE_LAUNCH_LOG")   # the driver's launch counter: this launch is one too
    if log:
        try:
            with open(log, "a") as fh:
                fh.write("shared\n")
        except OSError:
            pass
    try:
        _proc = subprocess.Popen(
            [chrome, *CHROME_SAFE_FLAGS, "--remote-debugging-port=0", f"--user-data-dir={_profile}", "--no-first-run", "--window-size=1440,1000", "about:blank"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        _stop()
        return False
    _guard()
    active = Path(_profile) / "DevToolsActivePort"
    deadline = time.monotonic() + START_SECONDS
    while time.monotonic() < deadline:
        if _proc.poll() is not None:
            break
        try:
            lines = active.read_text().split("\n")
        except OSError:
            lines = []
        # a complete file only: the port line, then the browser path line (a half-written first line is a truncated port)
        if len(lines) >= 2 and lines[0].strip().isdigit() and lines[1].strip():
            _port = int(lines[0].strip())
            return True
        time.sleep(0.1)
    _stop()
    return False


def _guard() -> None:
    """Start the parent-death watchdog for the Chrome just launched, and the in-process SIGTERM/SIGHUP cleanup."""
    global _watch, _handlers_set
    try:
        _watch = subprocess.Popen([sys.executable, str(WATCH), str(os.getpid()), str(_proc.pid), str(_profile)],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        _watch = None
    if _handlers_set:
        return
    _handlers_set = True
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name)
        try:
            previous = signal.getsignal(sig)
            if previous in (signal.SIG_IGN, None):   # ignored by the host (nohup): stay ignored
                continue

            def handler(signum, frame, previous=previous):
                shutdown()
                if callable(previous):
                    previous(signum, frame)
                    return
                signal.signal(signum, signal.SIG_DFL)   # no handler of the host's own: end as the signal would have
                os.kill(os.getpid(), signum)

            signal.signal(sig, handler)
        except (ValueError, OSError):   # not the main thread
            pass


def _healthy() -> bool:
    """Browser.getVersion answered: PING_CDP_MS for the exchange (timed in the ping), HEALTH_SECONDS for the whole process."""
    node = shutil.which("node")
    if _port is None or node is None or (_proc is not None and _proc.poll() is not None):
        return False
    try:
        r = subprocess.run([node, str(PING), str(_port), str(PING_CDP_MS)], capture_output=True, timeout=HEALTH_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    return r.returncode == 0


def port() -> int | None:
    """The port of a Chrome that just answered Browser.getVersion, or None (spawn path) for this and every later call
    once the Chrome could not be started, or failed its check a second time."""
    global _restarts, _disabled
    if not enabled():
        return None
    if _proc is None and not _start():
        _disabled = True
        return None
    if _healthy():
        return _port
    if _restarts == 0:
        _restarts = 1
        _stop()
        if _start() and _healthy():
            return _port
    _stop()
    _disabled = True
    return None


def shutdown() -> None:
    _stop()


atexit.register(shutdown)
