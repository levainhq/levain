"""s5 §5b (A6; head ruling 1022+19 Q2): an export target's stage joins a FRESH anonymous session keyring while
still root, before the drop, and proves it.

Why: on Linux a process inherits its parent's session keyring, and a key in it is reachable by anything running
under that keyring whose uid the key's permissions admit. The root child that launches an export target inherits
root's session keyring through bwrap, so the stage replaces it with a new, empty, anonymous one
(``keyctl(KEYCTL_JOIN_SESSION_KEYRING, NULL)``) and then asserts the session keyring the kernel now reports
(``KEYCTL_GET_KEYRING_ID`` of ``KEY_SPEC_SESSION_KEYRING``, no create) is the serial the join returned. Any failure
refuses ``keyring-not-joined``; the stage then execs nothing.

The PERSISTENT keyring is a different object and stays a named edge until S1 session 6's seccomp row (§5b Q5).
macOS has no kernel keyrings: the stage calls this on Linux only.

glibc has no ``keyctl`` wrapper (libkeyutils has one; Levain does not depend on it), so the call goes through
``syscall(2)`` with the architecture's number: x86_64 250, aarch64 219 (the kernel's syscall tables:
``arch/x86/entry/syscalls/syscall_64.tbl`` "keyctl" and ``include/uapi/asm-generic/unistd.h`` ``__NR_keyctl``).
Any other machine refuses rather than guess a number [judged by adopt-cc, 10-22, against PLAN.md session 2's REC].
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from levain.slot import Refused

KEYCTL_GET_KEYRING_ID = 0
KEYCTL_JOIN_SESSION_KEYRING = 1
KEY_SPEC_SESSION_KEYRING = -3
KEYCTL_NR = {"x86_64": 250, "aarch64": 219}
REFUSAL = "keyring-not-joined"


def keyctl_nr(sysname: str, machine: str) -> int:
    """The ``keyctl`` syscall number for this kernel and machine; anything but Linux on x86_64 / aarch64 refuses."""
    if sysname != "Linux" or machine not in KEYCTL_NR:
        raise Refused(REFUSAL, f"no keyctl syscall number for {sysname} {machine}")
    return KEYCTL_NR[machine]


def real_keyctl(op: int, arg2: int, arg3: int = 0) -> int:
    """``syscall(__NR_keyctl, op, arg2, arg3, 0, 0)``; raises ``OSError`` with the errno on -1. The machine is read
    from ``os.uname()`` (the kernel's own answer, never ``sys.platform``, which tests may patch)."""
    import ctypes

    u = os.uname()
    nr = keyctl_nr(u.sysname, u.machine)
    if ctypes.sizeof(ctypes.c_long) != 8:
        raise Refused(REFUSAL, "not a 64-bit process")
    libc = ctypes.CDLL(None, use_errno=True)
    fn = libc.syscall
    fn.restype = ctypes.c_long
    fn.argtypes = [ctypes.c_long] * 6
    r = fn(nr, op, arg2, arg3, 0, 0)
    if r == -1:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))
    return int(r)


def join_session_keyring(keyctl: Optional[Callable[..., int]]) -> int:
    """Join a new anonymous session keyring (``name`` NULL) and prove the kernel now reports it as this process's
    session keyring. Returns its serial. ``keyctl`` None (a caller that supplied no keyctl) refuses: there is no
    path that skips the join."""
    if keyctl is None:
        raise Refused(REFUSAL, "no keyctl call to join with")
    try:
        serial = keyctl(KEYCTL_JOIN_SESSION_KEYRING, 0)
    except OSError as e:
        raise Refused(REFUSAL, f"join: errno {e.errno}") from None
    if not isinstance(serial, int) or isinstance(serial, bool) or serial <= 0:
        raise Refused(REFUSAL, f"join returned {serial!r}")
    try:
        now = keyctl(KEYCTL_GET_KEYRING_ID, KEY_SPEC_SESSION_KEYRING, 0)
    except OSError as e:
        raise Refused(REFUSAL, f"get session keyring: errno {e.errno}") from None
    if now != serial:
        raise Refused(REFUSAL, f"session keyring is {now!r}, the join returned {serial}")
    return serial


__all__ = ["KEYCTL_GET_KEYRING_ID", "KEYCTL_JOIN_SESSION_KEYRING", "KEYCTL_NR", "KEY_SPEC_SESSION_KEYRING", "REFUSAL",
           "join_session_keyring", "keyctl_nr", "real_keyctl"]
