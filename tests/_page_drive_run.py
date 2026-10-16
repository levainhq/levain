"""Run a headless-Chrome page driver so that no Chrome outlives it.

``subprocess.run(..., timeout=...)`` kills only the process it started, with SIGKILL, so a node driver killed
that way never runs its own cleanup and its Chrome lives on under PID 1, each with a fresh profile that asks
the macOS keychain for Chrome's storage key (a dialog on the operator's screen). Here the driver runs in its
own session, and a timeout kills that whole process group, Chrome included.
"""
from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Sequence


def run(argv: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """``subprocess.run(argv, capture_output=True, text=True, timeout=timeout)``, except that a timeout (or any
    interruption) kills the driver's whole process group before it propagates."""
    proc = subprocess.Popen(list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except BaseException:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.communicate()
        raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
