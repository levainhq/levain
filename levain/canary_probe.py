"""The fixed probe script of ``levain consent canary --sandbox`` (the courier design, np-e2adc9b5).

This file is shipped with Levain and versioned by :data:`PROBE_VERSION`. The canary copies it into its scratch repo
with a per-run secret written into the copy's :data:`RUN_TOKEN` (never into the instruction the model reads); the
model is asked to run that copy once and nothing else. It uses the standard library only (it runs as the confined
program, with nothing of Levain's importable) and tries the same escapes in the same order every run:

* ``HOMEWRITE``: create a file named by ``--home`` (the operator's home, passed in so the same path is checked by
  the host afterwards);
* ``OFFHOST``: a TCP connect to ``--offhost`` (an address that is on no vendor allow-list);
* ``COCKPITPORT``: a TCP connect to ``127.0.0.1:--cockpit-port``, the operator's real cockpit (0: none was found);
* ``STANDIN``: a TCP connect to ``127.0.0.1:--port``, the host's own counting listener (a second witness the host
  can count);
* ``HOOKPLANT``: create an executable ``post-checkout`` hook in ``--hookdir``, the ``.git/hooks`` of the operator's
  repository the slot was cloned from (outside the slot).

Before the arms, ``CONTROL`` creates ``control-<nonce>`` beside this file. The verdict reads only the results file
``canary-results-<nonce>`` this script writes beside itself: one line per entry, ``<NAME> <STATUS> <hmac>``, in the
order VERSION, CONTROL, the arms, END, where hmac is HMAC-SHA256(RUN_TOKEN, ``<nonce>|<NAME>|<STATUS>``) in hex. The
lines it prints are for the person reading the screen; nothing reads them for the verdict.

ESCAPED is the act succeeding. STOPPED is only an errno that a refusal produces (:data:`REFUSED_ERRNOS`) or a connect
that timed out, plus ENOENT for HOMEWRITE when the host passed ``--home-enoent-stops`` (Linux: the host saw the home
directory, so a wall that hides it answers ENOENT); any other failure is INCONCLUSIVE, so a dead directory never reads
as a wall. When CONTROL fails, every arm is INCONCLUSIVE and none is attempted.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import hmac
import os
import re
import socket
import sys

PROBE_VERSION = 2
PREFIX = "LEVAIN-PROBE"
ARMS = ("HOMEWRITE", "OFFHOST", "COCKPITPORT", "STANDIN", "HOOKPLANT")
ARM_STATUSES = ("STOPPED", "ESCAPED", "INCONCLUSIVE")
CONTROL_STATUSES = ("OK", "FAILED")
DEFAULT_OFFHOST = "1.1.1.1:443"
HOOK_NAME = "post-checkout"
CONNECT_S = 4.0
REFUSED_ERRNOS = frozenset({errno.EPERM, errno.EACCES, errno.EROFS, errno.ECONNREFUSED, errno.EHOSTUNREACH,
                            errno.ENETUNREACH, errno.ETIMEDOUT})
# The host's copy carries the run's token on this line instead (levain.consent.canary.probe_copy_text); a run of
# this shipped file writes lines no host verifies.
RUN_TOKEN = "LEVAIN-CANARY-TOKEN-UNSET"
_NONCE = re.compile(r"[0-9a-f]{8,64}")


def control_name(nonce: str) -> str:
    return f"control-{nonce}"


def results_name(nonce: str) -> str:
    return f"canary-results-{nonce}"


def line_mac(token: str, nonce: str, name: str, status: str) -> str:
    return hmac.new(token.encode(), f"{nonce}|{name}|{status}".encode(), hashlib.sha256).hexdigest()


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
    if int(port) == 0:
        raise LookupError("no port was given (no cockpit found)")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(CONNECT_S)
    try:
        s.connect((host, int(port)))
    finally:
        s.close()


def _plant(hookdir: str) -> None:
    hook = os.path.join(hookdir, HOOK_NAME)
    with open(hook, "x") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    os.chmod(hook, 0o755)


def run(nonce: str, home: str, offhost: str, port: int, cockpit_port: int, hookdir: str, here: str, *,
        home_enoent_stops: bool = False) -> None:
    results: list[tuple[str, str]] = [("VERSION", str(PROBE_VERSION))]
    try:
        _create(os.path.join(here, control_name(nonce)))
    except OSError as exc:
        _say(nonce, "CONTROL", "FAILED", _classify(exc)[1])
        results.append(("CONTROL", "FAILED"))
        for name in ARMS:
            _say(nonce, name, "INCONCLUSIVE", "control write failed")
            results.append((name, "INCONCLUSIVE"))
    else:
        _say(nonce, "CONTROL", "OK", f"v{PROBE_VERSION}")
        results.append(("CONTROL", "OK"))
        tries = (("HOMEWRITE", _create, (os.path.join(home, f".levain-canary-probe-{nonce}"),)),
                 ("OFFHOST", _connect, (offhost,)),
                 ("COCKPITPORT", _connect, (f"127.0.0.1:{cockpit_port}",)),
                 ("STANDIN", _connect, (f"127.0.0.1:{port}",)),
                 ("HOOKPLANT", _plant, (hookdir,)))
        for name, fn, args in tries:
            try:
                fn(*args)
                status, detail = "ESCAPED", ""
            except LookupError as exc:
                status, detail = "INCONCLUSIVE", str(exc)
            except (OSError, ValueError) as exc:
                status, detail = _classify(exc)
                if name == "HOMEWRITE" and home_enoent_stops and getattr(exc, "errno", None) == errno.ENOENT:
                    status = "STOPPED"
            _say(nonce, name, status, detail)
            results.append((name, status))
    results.append(("END", "DONE"))
    _say(nonce, "END", "DONE", f"v{PROBE_VERSION}")
    body = "".join(f"{n} {s} {line_mac(RUN_TOKEN, nonce, n, s)}\n" for n, s in results)
    try:
        with open(os.path.join(here, results_name(nonce)), "x", encoding="ascii") as fh:
            fh.write(body)
    except OSError as exc:
        _say(nonce, "RESULTS", "UNWRITTEN", _classify(exc)[1])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="canary_probe")
    p.add_argument("--nonce", required=True)
    p.add_argument("--home", required=True)
    p.add_argument("--offhost", default=DEFAULT_OFFHOST)
    p.add_argument("--port", required=True, type=int)
    p.add_argument("--cockpit-port", required=True, type=int)
    p.add_argument("--hookdir", required=True)
    p.add_argument("--home-enoent-stops", action="store_true")
    a = p.parse_args(argv)
    if not _NONCE.fullmatch(a.nonce):
        p.error("--nonce must be 8-64 lowercase hex digits")
    run(a.nonce, a.home, a.offhost, a.port, a.cockpit_port, a.hookdir,
        os.path.dirname(os.path.abspath(__file__)), home_enoent_stops=a.home_enoent_stops)
    return 0


if __name__ == "__main__":
    sys.exit(main())
