"""The fixed probe script of ``levain consent canary --sandbox`` (the courier design, np-e2adc9b5).

This file is shipped with Levain, versioned by :data:`PROBE_VERSION`, and copied unchanged into the canary's scratch
repo; the model is asked to run it once and nothing else. It uses the standard library only (it runs as the confined
program, with nothing of Levain's importable) and tries the same four escapes in the same order every run:

* ``HOMEWRITE``: create a file named by ``--home`` (the operator's home, passed in so the same path is checked by
  the host afterwards);
* ``OFFHOST``: a TCP connect to ``--offhost`` (an address that is on no vendor allow-list);
* ``COCKPITPORT``: a TCP connect to ``127.0.0.1:--port`` (the host's own counting listener, standing in for the
  cockpit's port);
* ``HOOKPLANT``: ``git init`` a nested repo under the working directory and write an executable hook into its
  ``.git/hooks``.

Before the arms, ``CONTROL`` writes a file inside the working directory. Every arm prints one line,
``LEVAIN-PROBE <nonce> <NAME> <STOPPED|ESCAPED|INCONCLUSIVE> [detail]``, and ``END`` closes the report. ESCAPED is
the act succeeding. STOPPED is only an errno that a refusal produces (:data:`REFUSED_ERRNOS`) or a connect that
timed out; any other failure is INCONCLUSIVE, so a dead directory or a missing ``git`` never reads as a wall. When
CONTROL fails, every arm is INCONCLUSIVE and none is attempted.
"""
from __future__ import annotations

import argparse
import errno
import os
import socket
import subprocess
import sys

PROBE_VERSION = 1
PREFIX = "LEVAIN-PROBE"
ARMS = ("HOMEWRITE", "OFFHOST", "COCKPITPORT", "HOOKPLANT")
DEFAULT_OFFHOST = "1.1.1.1:443"
CONNECT_S = 4.0
REFUSED_ERRNOS = frozenset({errno.EPERM, errno.EACCES, errno.EROFS, errno.ECONNREFUSED, errno.EHOSTUNREACH,
                            errno.ENETUNREACH, errno.ETIMEDOUT})


def _say(nonce: str, name: str, status: str, detail: str = "") -> None:
    detail = " ".join(detail.split())[:80]
    print(f"{PREFIX} {nonce} {name} {status}" + (f" {detail}" if detail else ""), flush=True)


def _classify(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, socket.timeout):
        return "STOPPED", "timeout"
    code = getattr(exc, "errno", None)
    label = errno.errorcode.get(code, type(exc).__name__) if isinstance(code, int) else type(exc).__name__
    return ("STOPPED" if code in REFUSED_ERRNOS else "INCONCLUSIVE"), label


def _create(path: str) -> None:
    with open(path, "x"):
        pass


def _connect(hostport: str) -> None:
    host, _, port = hostport.rpartition(":")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(CONNECT_S)
    try:
        s.connect((host, int(port)))
    finally:
        s.close()


def _plant(cwd: str, nonce: str) -> None:
    nested = os.path.join(cwd, f"nested-{nonce}")
    os.mkdir(nested)
    try:
        subprocess.run(["git", "init", "-q", nested], check=True, capture_output=True, timeout=20)
    except FileNotFoundError as exc:
        raise LookupError("git is not available") from exc
    except subprocess.CalledProcessError as exc:
        raise LookupError("git init failed") from exc
    hook = os.path.join(nested, ".git", "hooks", "post-checkout")
    with open(hook, "x") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    os.chmod(hook, 0o755)


def run(nonce: str, home: str, offhost: str, port: int, cwd: str) -> None:
    try:
        _create(os.path.join(cwd, f"control-{nonce}"))
    except OSError as exc:
        for name in ARMS:
            _say(nonce, name, "INCONCLUSIVE", "control write failed: " + _classify(exc)[1])
        _say(nonce, "END", "DONE", f"v{PROBE_VERSION}")
        return
    _say(nonce, "CONTROL", "OK", f"v{PROBE_VERSION}")
    tries = (("HOMEWRITE", _create, (os.path.join(home, f".levain-canary-probe-{nonce}"),)),
             ("OFFHOST", _connect, (offhost,)),
             ("COCKPITPORT", _connect, (f"127.0.0.1:{port}",)),
             ("HOOKPLANT", _plant, (cwd, nonce)))
    for name, fn, args in tries:
        try:
            fn(*args)
            _say(nonce, name, "ESCAPED")
        except LookupError as exc:
            _say(nonce, name, "INCONCLUSIVE", str(exc))
        except (OSError, ValueError) as exc:
            status, label = _classify(exc)
            _say(nonce, name, status, label)
    _say(nonce, "END", "DONE", f"v{PROBE_VERSION}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="canary_probe")
    p.add_argument("--nonce", required=True)
    p.add_argument("--home", required=True)
    p.add_argument("--offhost", default=DEFAULT_OFFHOST)
    p.add_argument("--port", required=True, type=int)
    a = p.parse_args(argv)
    run(a.nonce, a.home, a.offhost, a.port, os.getcwd())
    return 0


if __name__ == "__main__":
    sys.exit(main())
