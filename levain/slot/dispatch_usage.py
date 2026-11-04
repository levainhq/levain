"""levain.slot.dispatch_usage: the production :class:`levain.agents.session_launcher.UsageSources` (S10d).

The three figures a subscription run settles on, read as ``_levain_dispatch`` by the run server (port item 7):

* ``calls(run_id)``: the broker's per-run call count, from the ``RUN-STATE`` answer. The reader is a callable returning
  that dict (the run server binds :func:`levain.qbroker.operator.run_state` to the queue root; S10b owns the binding).
  ``known`` False means GATE never judged the run: "NOT evidence of health, only of no call so far", so it reads None
  (the caps), never a quiet zero.
* ``tunnel_bytes(session)``: the front's FINAL passthrough total from its tunnel log
  (:func:`levain.slot.runtime_helper.read_passthrough_total`), None until that final line exists.
* ``seconds(session)``: the session's seconds from the retained record, through a reader the run server supplies.

Every figure is an int >= 0 (bool refused) or None. Every reader fault, empty key or odd shape is None: the launcher
settles a None at the caps, the safe side. Nothing here raises. The readers are injected so the module runs in pytest
without a broker, a front or a root-owned store (the real ones are the S10d residue, U-A1)."""
from __future__ import annotations

from typing import Any, Callable, Optional

__all__ = ["DispatchUsageSources"]


def _count(v: object) -> Optional[int]:
    return v if type(v) is int and v >= 0 else None


def _default_read_total(path: object) -> Optional[int]:
    from levain.slot.runtime_helper import read_passthrough_total
    return read_passthrough_total(path)


class DispatchUsageSources:
    def __init__(self, *, run_state: Callable[[str], Any], tunnel_log: Callable[[str], Any],
                 seconds_of: Callable[[str], Any],
                 read_total: Optional[Callable[[Any], Any]] = None) -> None:
        self._run_state, self._tunnel_log, self._seconds_of = run_state, tunnel_log, seconds_of
        self._read_total = read_total or _default_read_total

    @staticmethod
    def _key_ok(key: object) -> bool:
        return type(key) is str and bool(key)

    def calls(self, run_id: str) -> Optional[int]:
        if not self._key_ok(run_id):
            return None
        try:
            doc = self._run_state(run_id)
            if type(doc) is not dict or doc.get("known") is not True:
                return None
            return _count(doc.get("calls"))
        except Exception:  # noqa: BLE001
            return None

    def tunnel_bytes(self, session: str) -> Optional[int]:
        if not self._key_ok(session):
            return None
        try:
            return _count(self._read_total(self._tunnel_log(session)))
        except Exception:  # noqa: BLE001
            return None

    def seconds(self, session: str) -> Optional[int]:
        if not self._key_ok(session):
            return None
        try:
            return _count(self._seconds_of(session))
        except Exception:  # noqa: BLE001
            return None
