"""levain.qbroker.peercred — the connected peer's uid, from the kernel, for both ends of a broker socket.

Its own module so the consent hook's client can authenticate the listener without importing the broker
(:mod:`levain.qbroker.server` imports it too): stdlib only.
"""
from __future__ import annotations

import socket
import struct
import sys

__all__ = ["peer_uid"]

_SOL_LOCAL = 0            # macOS <sys/un.h>
_LOCAL_PEERCRED = 0x001   # macOS <sys/un.h>: struct xucred
_XUCRED = struct.Struct("@IIh16I")   # cr_version, cr_uid, cr_ngroups, cr_groups[NGROUPS]
_UCRED = struct.Struct("3i")         # Linux struct ucred: pid, uid, gid


def peer_uid(sock: socket.socket) -> int:
    """The connected peer's effective uid, from the kernel."""
    if sys.platform == "darwin":
        raw = sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERCRED, _XUCRED.size)
        version, uid = _XUCRED.unpack(raw[:_XUCRED.size])[:2]
        if version != 0:
            raise OSError(f"xucred version {version}")
        return uid
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size)
    return _UCRED.unpack(raw)[1]
