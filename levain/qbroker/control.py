"""levain.qbroker.control — the broker's CONTROL client: ``QUEUE-RESOLVE`` and nothing else.

One connection per resolve to ``R/control/control.sock`` (CONTRACT c3 §2.2), one frame each way
(:mod:`levain.qbroker.wire`, CONTRACT c3 §3.3). CONTROL answers ``{session, kind, run?}`` for the cap minted for
that session on that slot, or a refusal name, which the broker passes to the hook as it came. Stdlib only.
"""
from __future__ import annotations

import itertools
import re
import socket
import threading
from pathlib import Path

from levain.qbroker import wire
from levain.qbroker.broker import Resolved, resolved_from

__all__ = ["ControlResolver"]

_NAME = re.compile(r"[a-z][a-z0-9-]{0,63}")


class ControlResolver:
    def __init__(self, path: Path | str, *, timeout: float = 5.0) -> None:
        self._path = str(path)
        self._timeout = timeout
        self._ids = itertools.count(1)
        self._ids_mutex = threading.Lock()

    def queue_resolve(self, slot: int, cap_digest: str) -> Resolved | str:
        with self._ids_mutex:
            rid = next(self._ids)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(self._timeout)
            sock.connect(self._path)
            wire.write_frame(sock, {"v": wire.VERSION, "op": "QUEUE-RESOLVE", "id": rid, "slot": slot,
                                    "cap_digest": cap_digest})
            got = wire.read_frame(sock)
        if not isinstance(got, dict) or got.get("id") != rid or got.get("ok") not in (True, False):
            return "resolve-malformed"
        if got["ok"] is False:
            name = got.get("refused")
            return name if isinstance(name, str) and _NAME.fullmatch(name) else "resolve-malformed"
        resolved = resolved_from({k: got.get(k) for k in ("session", "kind", "run")})
        return resolved if resolved is not None else "resolve-malformed"
