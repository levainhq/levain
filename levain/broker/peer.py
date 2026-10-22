"""keys' peer check (S1 session 3b, design 1.2 / 1.5): who is at the other end of an accepted AF_UNIX connection, read
from the kernel after accept and before the first frame, as `(uid, pid, start)`.

`start` is the process start time in the unit MINT's `front.start` carries, so a registration and a peer compare as
plain integers: Linux, clock ticks since boot (`/proc/<pid>/stat` field 22); macOS, microseconds since the epoch
(`sysctl {CTL_KERN, KERN_PROC, KERN_PROC_PID, pid}`'s `p_starttime`). macOS reads it through sysctl, not libproc:
libproc answers nothing for a process of another uid, and keys (`_levain_keys`) reads `_levain_gw<n>` fronts.

The macOS socket options are raw constants (`SOL_LOCAL = 0`, `LOCAL_PEERCRED = 1`, `LOCAL_PEERPID = 2`): the root
interpreter's `socket` module names neither `SOL_LOCAL` nor `LOCAL_PEERPID`.

Anything that cannot be read is `PeerUnknown`, and keys closes the connection with no frame (fail closed: an outage,
never a pass). The pid is read at accept and the start time after it; a peer that exited in between, its pid reused,
shows the new process's start, which names no registration.

Runs on the root interpreter (3.9).
"""
from __future__ import annotations

import socket
import struct
import sys
from typing import Optional, Tuple

_SOL_LOCAL = 0
_LOCAL_PEERCRED = 1
_LOCAL_PEERPID = 2
_XUCRED_VERSION = 0
#: macOS `struct xucred`: cr_version, cr_uid, cr_ngroups, cr_groups[16].
_XUCRED = struct.Struct("@IIh16I")
#: Linux `struct ucred`: pid, uid, gid.
_UCRED = struct.Struct("3i")
_CTL_KERN, _KERN_PROC, _KERN_PROC_PID = 1, 14, 1
#: `struct kinfo_proc` opens with `struct extern_proc`, whose first member is `p_un`, a union whose first member is
#: `p_starttime`, a `struct timeval` (64-bit seconds, 32-bit microseconds).
_TIMEVAL = struct.Struct("@qi")
_KINFO_PROC_SIZE = 648


class PeerUnknown(Exception):
    """The peer's uid, pid or start time could not be read."""


def peer_of(sock: socket.socket) -> Tuple[int, int, int]:
    """`(uid, pid, start)` of the process that connected `sock`, or `PeerUnknown`."""
    if sock.family != socket.AF_UNIX:
        raise PeerUnknown("not a unix socket")
    try:
        if sys.platform.startswith("linux"):
            pid, uid, _gid = _UCRED.unpack(sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size))
        elif sys.platform == "darwin":
            raw = sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERCRED, _XUCRED.size)
            version, uid = _XUCRED.unpack(raw[:_XUCRED.size])[:2]
            if version != _XUCRED_VERSION:
                raise PeerUnknown(f"xucred version {version}")
            pid = struct.unpack("i", sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID, struct.calcsize("i")))[0]
        else:
            raise PeerUnknown(f"no peer credentials on {sys.platform}")
    except (OSError, struct.error) as e:
        raise PeerUnknown(f"{type(e).__name__}: {e}") from None
    if pid <= 0:
        raise PeerUnknown(f"pid {pid}")
    start = start_of(pid)
    if start is None:
        raise PeerUnknown(f"no start time for pid {pid}")
    return uid, pid, start


def start_of(pid: int) -> Optional[int]:
    """The start time of process `pid` (see the module docstring for its unit), or None when it cannot be read."""
    if sys.platform.startswith("linux"):
        return _start_linux(pid)
    if sys.platform == "darwin":
        return _start_darwin(pid)
    return None


def _start_linux(pid: int) -> Optional[int]:
    try:
        with open(f"/proc/{int(pid)}/stat", "rb") as fh:
            data = fh.read()
    except OSError:   # gone, or hidepid: fail closed
        return None
    # the command name (field 2) may hold spaces and parentheses; every field after its LAST ')' is plain
    fields = data.rpartition(b")")[2].split()
    try:
        return int(fields[19])   # field 22: fields[0] is field 3
    except (IndexError, ValueError):
        return None


_libc = None


def _start_darwin(pid: int) -> Optional[int]:
    global _libc
    import ctypes
    if _libc is None:
        lib = ctypes.CDLL(None, use_errno=True)
        lib.sysctl.argtypes = (ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                               ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t)
        lib.sysctl.restype = ctypes.c_int
        _libc = lib
    mib = (ctypes.c_int * 4)(_CTL_KERN, _KERN_PROC, _KERN_PROC_PID, int(pid))
    buf = ctypes.create_string_buffer(_KINFO_PROC_SIZE)
    size = ctypes.c_size_t(_KINFO_PROC_SIZE)
    if _libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0:
        return None
    if size.value < _TIMEVAL.size:   # no such process: the kernel answers with an empty record
        return None
    sec, usec = _TIMEVAL.unpack_from(buf.raw, 0)
    if sec <= 0:
        return None
    return sec * 1000000 + usec
