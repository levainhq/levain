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
from collections.abc import Mapping
from typing import Any, Callable, Optional

__all__ = ["DispatchUsageSources", "lineage_spend", "lineage_spend_reader"]


def _count(v: object) -> Optional[int]:
    return v if type(v) is int and v >= 0 else None


def _default_read_total(path: object, session: str) -> Optional[int]:
    """The session's OWN final ``passthrough_total`` in a slot tunnel log: the one reader now lives in
    ``levain.slot.runtime_helper`` (:func:`~levain.slot.runtime_helper.read_session_total`); this is its re-import
    under the S10d name, opening ``path`` with ``O_NOFOLLOW`` at the leaf only. Root's END does NOT use this path
    reader: it uses :func:`~levain.slot.runtime_helper.read_session_tunnel_total` (the trusted walk), and a new caller
    resolving a slot log should too. None when absent."""
    from levain.slot.runtime_helper import read_session_total
    return read_session_total(path, session)


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


# --- a brokered run's spend (dispatch wave 3, S-B3) ---------------------------------------------------------------------

#: the lineage sink's format and the longest line read (a `/3` row is under 2 KiB; a longer line is no row)
_LINEAGE_FORMAT = "levain-dispatch-lineage/3"
_LINE_MAX = 65536
_INT_MAX = 2**63 - 1


def _no_constant(name: str) -> None:
    raise ValueError(name)


def _micros(v: object) -> Optional[int]:
    return v if type(v) is int and 0 <= v <= _INT_MAX else None


def lineage_spend(path: object, run_id: str) -> Optional[int]:
    """The micro-dollars a run spent, summed from CONTROL's lineage sink (`L/lineage/lineage.jsonl`, `/3` rows) by
    ``run_id``. The dispatch uid reads it by group (the sink's own docstring, H6-4). Per row of the run: ``settled``
    ``yes`` is its ``cost_microdollars`` (the hold when the cost is unsaid), ``uncertain`` is the larger of the cost and
    the hold (a call that may have been charged), ``no`` is 0 (released or refused). Fail-closed: None, so the caller
    settles at the whole reservation, when the sink cannot be opened or read whole, any line is torn, over-long, not
    JSON or not a row, ANY quarantine row exists (it names no run, so it may be this one's), a row of the run carries a
    figure that is not a micro-dollar count, or the run has NO row (a run that called nothing and a sink that lags the
    run read alike, and the second must not read as zero)."""
    if type(run_id) is not str or not run_id or not isinstance(path, (str, os.PathLike)):
        return None
    total, matched = 0, 0
    try:
        fd = os.open(os.fspath(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            while True:
                raw = fh.readline(_LINE_MAX + 1)
                if not raw:
                    break
                if len(raw) > _LINE_MAX or not raw.endswith(b"\n"):
                    return None            # over-long, or a torn tail (an append in flight, or a crash): never a row
                try:
                    row = json.loads(raw.decode("utf-8"), parse_constant=_no_constant)
                except (ValueError, RecursionError):
                    return None
                if not isinstance(row, dict) or row.get("format") != _LINEAGE_FORMAT:
                    return None
                if "quarantined" in row:
                    return None
                if row.get("run_id") != run_id:
                    continue
                settled, hold, cost = row.get("settled"), _micros(row.get("hold_microdollars")), row.get("cost_microdollars")
                if hold is None or (cost is not None and _micros(cost) is None):
                    return None
                if settled == "yes":
                    figure = hold if cost is None else cost
                elif settled == "uncertain":
                    figure = hold if cost is None else max(hold, cost)
                elif settled == "no":
                    figure = 0
                else:
                    return None
                total += figure
                matched += 1
    except OSError:
        return None
    finally:
        if fd != -1:
            os.close(fd)
    return total if matched and total <= _INT_MAX else None


def lineage_spend_reader(path: object) -> Callable[[str], Optional[int]]:
    """:func:`lineage_spend` bound to the sink at ``path``: the ``spend`` a brokered ``RealLauncher`` is built with."""
    return lambda run_id: lineage_spend(path, run_id)
