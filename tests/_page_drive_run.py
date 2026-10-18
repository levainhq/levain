"""Run a headless-Chrome page driver so that no Chrome, and no profile directory, outlives it.

``subprocess.run(..., timeout=...)`` kills only the process it started, with SIGKILL, so a node driver killed
that way never runs its own cleanup and its Chrome lives on under PID 1, each with a fresh profile that asks
the macOS keychain for Chrome's storage key (a dialog on the operator's screen). Here the driver runs in its
own session and its own temporary directory: on EVERY return (a normal exit with a surviving child, a timeout,
an interrupt) the whole process group is killed and the directory, which holds the Chrome profile, is removed.
The node drivers also exit when their parent dies, which covers a pytest killed outright.

THIS IS THE WAY to take a cockpit screenshot (or drive any page): ``python tests/_page_drive_run.py <chrome> <url>
<steps.json> [--timeout N]`` with a ``{"shot": "<abs path.png>"}`` step. For app content that loads asynchronously (the driver only waits for the document's load event), put an
``until`` step before the ``shot``. Never write an ad-hoc Chrome launcher for a
shot: one that skips this reaping leaves a headless Chrome under PID 1 that macOS then routes new-window requests to.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
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
        # bounded: a helper outside the group may still hold the pipes, so never wait on them unboundedly
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired as late:
            out, err = late.stdout, late.stderr
            for pipe in (proc.stdout, proc.stderr):
                try:
                    if pipe:
                        pipe.close()
                except OSError:
                    pass
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        _keep_partial(sys.exc_info()[1], out, err)
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


def _keep_partial(exc: BaseException | None, out: object, err: object) -> None:
    """Keep what the driver printed before it died on the exception that is re-raised (a timeout carries it already)."""
    if isinstance(exc, subprocess.TimeoutExpired):
        exc.stdout, exc.stderr = out, err


def main(args: Sequence[str]) -> int:
    """CLI: run ``_page_drive.mjs`` under :func:`run`; its stdout and stderr pass through, its exit code is returned."""
    import argparse
    ap = argparse.ArgumentParser(description="drive a page in headless Chrome with the reaping runner")
    ap.add_argument("chrome"); ap.add_argument("url"); ap.add_argument("steps")
    ap.add_argument("--timeout", type=float, default=120)
    ns = ap.parse_args(args)
    node = shutil.which("node")
    if not node:
        print("node not found on PATH", file=sys.stderr)
        return 127
    driver = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_page_drive.mjs")
    try:
        r = run([node, driver, ns.chrome, ns.url, ns.steps], timeout=ns.timeout)
    except subprocess.TimeoutExpired as e:
        sys.stdout.write(e.stdout if isinstance(e.stdout, str) else "")
        sys.stderr.write(e.stderr if isinstance(e.stderr, str) else "")
        print(f"timed out after {ns.timeout}s", file=sys.stderr)
        return 124
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
