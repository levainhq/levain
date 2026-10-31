"""One headless Chrome for a whole pytest session, for the tests that drive a page through ``tests/_page_drive.mjs``.

Every Chrome launch makes macOS keychain calls that no flag removes, and the browser tests launch Chrome a hundred times a
run. Here the session starts one Chrome (``CHROME_SAFE_FLAGS``, its own profile, a debugging port the OS picks) and hands its
port to the driver as ``LEVAIN_DRIVE_CDP_PORT``; the driver gives each drive its own browser context there.

Never a mass failure: if the Chrome cannot start, or stops answering twice, the session falls back to the driver's own
launch for the rest of the run. The Chrome lives in its own process group and that group is killed at the end of the session
and at interpreter exit (an orphan headless Chrome under PID 1 gets new-window requests routed to it).
``LEVAIN_SHARED_CHROME=0`` turns the whole thing off.
"""
from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from tests._chrome_flags import CHROME_SAFE_FLAGS
from tests._page_drive_run import _sweep

CHROME = os.environ.get("CHROME") or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PING = Path(__file__).with_name("_cdp_ping.mjs")
HEALTH_SECONDS = 2.0
START_SECONDS = 20.0

_proc: subprocess.Popen | None = None
_profile: str | None = None
_port: int | None = None
_restarts = 0
_disabled = False


def enabled() -> bool:
    return os.environ.get("LEVAIN_SHARED_CHROME", "1") != "0" and not _disabled


def _stop() -> None:
    global _proc, _profile, _port
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
    if not Path(CHROME).exists():
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
            [CHROME, *CHROME_SAFE_FLAGS, "--remote-debugging-port=0", f"--user-data-dir={_profile}", "--no-first-run", "about:blank"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        _stop()
        return False
    active = Path(_profile) / "DevToolsActivePort"
    deadline = time.monotonic() + START_SECONDS
    while time.monotonic() < deadline:
        if _proc.poll() is not None:
            break
        try:
            first = active.read_text().split("\n")[0].strip()
            if first.isdigit():
                _port = int(first)
                return True
        except OSError:
            pass
        time.sleep(0.1)
    _stop()
    return False


def _healthy() -> bool:
    """Browser.getVersion answered within HEALTH_SECONDS."""
    node = shutil.which("node")
    if _port is None or node is None or (_proc is not None and _proc.poll() is not None):
        return False
    try:
        r = subprocess.run([node, str(PING), str(_port)], capture_output=True, timeout=HEALTH_SECONDS)
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
