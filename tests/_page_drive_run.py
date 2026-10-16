"""Run a headless-Chrome page driver so that no Chrome, and no profile directory, outlives it.

``subprocess.run(..., timeout=...)`` kills only the process it started, with SIGKILL, so a node driver killed
that way never runs its own cleanup and its Chrome lives on under PID 1, each with a fresh profile that asks
the macOS keychain for Chrome's storage key (a dialog on the operator's screen). Here the driver runs in its
own session and its own temporary directory: on EVERY return (a normal exit with a surviving child, a timeout,
an interrupt) the whole process group is killed and the directory, which holds the Chrome profile, is removed.
The node drivers also exit when their parent dies, which covers a pytest killed outright.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Sequence


def run(argv: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """``subprocess.run(argv, capture_output=True, text=True, timeout=timeout)``, except that the driver's whole
    process group is killed and its scratch directory removed before this returns or raises."""
    scratch = tempfile.mkdtemp(prefix="levain-run-")
    proc = subprocess.Popen(list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True, env={**os.environ, "TMPDIR": scratch})
    try:
        out, err = proc.communicate(timeout=timeout)
    except BaseException:
        _sweep(proc.pid)
        proc.communicate()
        raise
    else:
        _sweep(proc.pid)
        return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _sweep(pgid: int) -> None:
    """SIGKILL the group, then wait (bounded) until it is gone so its files can be removed."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return
    for _ in range(50):
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.1)
