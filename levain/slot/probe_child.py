"""V13's connector (DESIGN_V13_REV2 §4, §5; head rulings R3 N2, N3): one non-blocking TCP connect to port
:data:`PORT` per address, all in parallel, inside one bound.

Two callers, one routine (:func:`connect_all`): the probe CHILD runs this module as ``__main__`` as the slot uid inside
its wall (``wall.launch_spec(host, "probe", ...)``), and root's CONTROL calls :func:`connect_all` in the host's own
namespace with no wall. The module imports the standard library only, so the process the slot runs executes this
file's code and nothing else of Levain's (N3; ``tests/test_slot_v13_probe.py`` reads the import list by AST).

The child, in this order: ``signal.alarm(PROBE_SELF_S)`` first; every socket made before any connect starts, so a
socket that cannot be made is never read as the wall holding (N2); the connects; then one line per address, in argv
order, on ``--result-fd``:

* ``c <ip>``: connected.
* ``b <ip> <errno-name|timeout>``: blocked, only for an errno in :data:`BLOCKED_ERRNOS` or the bound passing (a pf
  ``block drop`` is a timeout).
* ``u <ip> <errno-name>``: anything else. Root reads a result holding any ``u`` line as unknown, never as the wall.

then ``end``. A child that dies, is killed by its alarm, or writes anything else gives root no ``end`` line: unknown.
"""
from __future__ import annotations

import errno
import os
import selectors
import signal
import socket
import sys
import time
from typing import Callable, Dict, Sequence, Tuple

#: Every vendor host the probe targets is HTTPS (§8: port 443 always).
PORT = 443
#: The bound on one round of connects, CONTROL's and PROBE's alike (§4).
CONNECT_S = 3.0
#: The child's own alarm, in whole seconds (``signal.alarm`` takes an int). It must fire before root's reap bound
#: (``probe.PROBE_REAP_S``); a test holds the inequality.
PROBE_SELF_S = 7
RESULT_FLAG = "--result-fd"
#: At most this many addresses per probe (root refuses more as ``firewall-probe-inconclusive:too-many``).
MAX_ADDRS = 64
#: Root reads at most this many bytes of the child's result (§4).
RESULT_MAX = 4096

#: N2: the only connect failures that count as the wall. Any other errno is unknown.
BLOCKED_ERRNOS = frozenset({errno.ECONNREFUSED, errno.ETIMEDOUT, errno.EHOSTUNREACH, errno.ENETUNREACH,
                            errno.EACCES, errno.EPERM})
_IN_PROGRESS = frozenset({errno.EINPROGRESS, errno.EALREADY, errno.EWOULDBLOCK, errno.EAGAIN})

CONNECTED, BLOCKED, UNKNOWN = "c", "b", "u"
TIMEOUT = "timeout"

Outcome = Tuple[str, str]     # (CONNECTED | BLOCKED | UNKNOWN, detail: "" | an errno name | TIMEOUT)


def errno_name(code: int) -> str:
    return errno.errorcode.get(code, "errno%d" % code)


def classify(code: int) -> Outcome:
    """A connect's errno as an outcome: N2's blocked set, else unknown."""
    return (BLOCKED if code in BLOCKED_ERRNOS else UNKNOWN), errno_name(code)


def family(ip: str) -> int:
    """The address family of a numeric address; ``ValueError`` for anything that is not one."""
    for fam in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(fam, ip)
            return fam
        except (OSError, ValueError):
            continue
    raise ValueError("not a numeric address")


def connect_all(addrs: Sequence[str], *, bound_s: float = CONNECT_S,
                make_socket: Callable[[int, int], socket.socket] = socket.socket,
                monotonic: Callable[[], float] = time.monotonic,
                selector: Callable[[], selectors.BaseSelector] = selectors.DefaultSelector) -> Dict[str, Outcome]:
    """One outcome per address of ``addrs``: every socket made first, then every connect started, then one select
    loop inside ``bound_s``; an address still pending at the bound is ``(BLOCKED, TIMEOUT)``. Every socket is closed
    before this returns."""
    out: Dict[str, Outcome] = {}
    socks: Dict[str, socket.socket] = {}
    try:
        for ip in addrs:
            try:
                fam = family(ip)
            except ValueError:
                out[ip] = (UNKNOWN, "bad-address")
                continue
            try:
                s = make_socket(fam, socket.SOCK_STREAM)
            except OSError as e:
                out[ip] = (UNKNOWN, errno_name(e.errno or 0))
                continue
            socks[ip] = s
            try:
                s.setblocking(False)
            except OSError as e:
                out[ip] = (UNKNOWN, errno_name(e.errno or 0))
        pending: Dict[str, socket.socket] = {}
        for ip, s in socks.items():
            if ip in out:
                continue
            try:
                code = s.connect_ex((ip, PORT))
            except OSError as e:          # connect_ex raises for what is not an errno (a bad address tuple)
                code = e.errno or 0
                out[ip] = (UNKNOWN, errno_name(code))
                continue
            if code == 0:
                out[ip] = (CONNECTED, "")
            elif code in _IN_PROGRESS:
                pending[ip] = s
            else:
                out[ip] = classify(code)
        if pending:
            sel = selector()
            try:
                for ip, s in pending.items():
                    sel.register(s, selectors.EVENT_WRITE, ip)
                end = monotonic() + bound_s
                while pending:
                    left = end - monotonic()
                    if left <= 0:
                        break
                    for key, _ in sel.select(left):
                        ip = key.data
                        s = pending.pop(ip)
                        sel.unregister(s)
                        try:
                            code = s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                        except OSError as e:
                            out[ip] = (UNKNOWN, errno_name(e.errno or 0))
                            continue
                        out[ip] = (CONNECTED, "") if code == 0 else classify(code)
            finally:
                sel.close()
            for ip in pending:
                out[ip] = (BLOCKED, TIMEOUT)
    finally:
        for s in socks.values():
            try:
                s.close()
            except OSError:
                pass
    return out


def result_text(addrs: Sequence[str], outcomes: Dict[str, Outcome]) -> bytes:
    """The child's result: one line per address in ``addrs`` order, then ``end``."""
    lines = []
    for ip in addrs:
        kind, detail = outcomes[ip]
        lines.append("c %s" % ip if kind == CONNECTED else "%s %s %s" % (kind, ip, detail))
    lines.append("end")
    return ("\n".join(lines) + "\n").encode("ascii")


def parse_argv(argv: Sequence[str]) -> Tuple[int, Tuple[str, ...]]:
    """``--result-fd <fd> <ip> [<ip> ...]``: the fd (above 2) and 1 to :data:`MAX_ADDRS` distinct numeric
    addresses; ``ValueError`` otherwise."""
    if len(argv) < 3 or argv[0] != RESULT_FLAG or not argv[1].isdigit() or int(argv[1]) <= 2:
        raise ValueError("usage: --result-fd <fd> <ip>...")
    addrs = tuple(argv[2:])
    if len(addrs) > MAX_ADDRS or len(set(addrs)) != len(addrs):
        raise ValueError("addresses: too many or repeated")
    for ip in addrs:
        family(ip)
    return int(argv[1]), addrs


def main(argv: Sequence[str], *, alarm: Callable[[int], object] = signal.alarm,
         write: Callable[[int, bytes], int] = os.write, close: Callable[[int], None] = os.close,
         connect: Callable[..., Dict[str, Outcome]] = connect_all) -> int:
    alarm(PROBE_SELF_S)
    try:
        fd, addrs = parse_argv(argv)
    except ValueError:
        return 2
    data = result_text(addrs, connect(addrs))
    view = memoryview(data)
    while view:
        view = view[write(fd, view):]
    close(fd)
    return 0


if __name__ == "__main__":   # pragma: no cover - the slot-side child (a VM row)
    sys.exit(main(sys.argv[1:]))
