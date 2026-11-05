"""levain.slot.dispatch_usage: the production :class:`levain.agents.session_launcher.UsageSources` (S10d).

The three figures a subscription run settles on, read as ``_levain_dispatch`` by the run server (port item 7):

* ``calls(run_id)``: the broker's per-run call count, from the ``RUN-STATE`` answer. The reader is a callable returning
  that dict (the run server binds :func:`levain.qbroker.operator.run_state` to the queue root; S10b owns the binding).
  ``known`` False means GATE never judged the run: "NOT evidence of health, only of no call so far", so it reads None
  (the caps), never a quiet zero.
* ``tunnel_bytes(session)``: the front's FINAL passthrough total from its tunnel log
  (that session's own record in the shared per-slot log, see :func:`_default_read_total`), None until it exists.
* ``seconds(session)``: the session's seconds from the retained record, through a reader the run server supplies.

Every figure is an int >= 0 (bool refused) or None. Every reader fault, empty key or odd shape is None: the launcher
settles a None at the caps, the safe side. Nothing here raises. The readers are injected so the module runs in pytest
without a broker, a front or a root-owned store (the real ones are the S10d residue, U-A1)."""
from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from typing import Any, Callable, List, Optional

__all__ = ["DispatchUsageSources"]


def _count(v: object) -> Optional[int]:
    return v if type(v) is int and v >= 0 else None


def _default_read_total(path: object, session: str) -> Optional[int]:
    """The session's OWN final ``passthrough_total`` in a slot tunnel log. The log (``L/gw/slot<n>/``) is per SLOT and
    append-only, so a reused slot holds several sessions' records; every front record carries its ``session``, and
    :func:`read_passthrough_total` (which takes the last tunnel line of any session) would hand back another session's
    total. Same file discipline and record format as that helper (last 1 MiB of a regular non-symlink file, first
    partial line dropped, ``event`` ``tunnel``, int ``passthrough_total`` in 0..2**63-1), searched BACKWARD for the
    last record whose ``session`` equals ``session``. None when absent."""
    from levain.slot.runtime_helper import _INT64_MAX, _PASSTHROUGH_TAIL_BYTES
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)  # type: ignore[call-overload]
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                return None
            off = max(0, st.st_size - _PASSTHROUGH_TAIL_BYTES)
            if off:
                os.lseek(fd, off, os.SEEK_SET)
            chunks: List[bytes] = []
            left = _PASSTHROUGH_TAIL_BYTES + 1
            while left > 0:
                c = os.read(fd, min(left, 1 << 16))
                if not c:
                    break
                chunks.append(c)
                left -= len(c)
        finally:
            os.close(fd)
    except (OSError, TypeError, ValueError):
        return None
    lines = b"".join(chunks).split(b"\n")
    if off:
        lines = lines[1:]
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(rec, dict) or rec.get("event") != "tunnel" or rec.get("session") != session:
            continue
        v = rec.get("passthrough_total")
        if type(v) is int and 0 <= v <= _INT64_MAX:
            return v
    return None


class DispatchUsageSources:
    def __init__(self, *, run_state: Callable[[str], Any], tunnel_log: Callable[[str], Any],
                 seconds_of: Callable[[str], Any],
                 read_total: Optional[Callable[[Any, str], Any]] = None) -> None:
        self._run_state, self._tunnel_log, self._seconds_of = run_state, tunnel_log, seconds_of
        self._read_total = _default_read_total if read_total is None else read_total

    @staticmethod
    def _key_ok(key: object) -> bool:
        return type(key) is str and bool(key)

    def calls(self, run_id: str) -> Optional[int]:
        if not self._key_ok(run_id):
            return None
        try:
            doc = self._run_state(run_id)
            if not isinstance(doc, Mapping) or doc.get("known") is not True:
                return None
            return _count(doc.get("calls"))
        except Exception:  # noqa: BLE001
            return None

    def tunnel_bytes(self, session: str) -> Optional[int]:
        if not self._key_ok(session):
            return None
        try:
            return _count(self._read_total(self._tunnel_log(session), session))
        except Exception:  # noqa: BLE001
            return None

    def seconds(self, session: str) -> Optional[int]:
        if not self._key_ok(session):
            return None
        try:
            return _count(self._seconds_of(session))
        except Exception:  # noqa: BLE001
            return None
