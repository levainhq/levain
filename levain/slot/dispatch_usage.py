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
import threading
import time
from collections.abc import Mapping
from typing import Any, Callable, Optional

from levain.broker.ledger import triples_digest

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
    """The injected ``read_total`` takes two arguments, ``(path, session)``, and returns the session's final total."""

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


# --- a brokered run's spend (dispatch wave 3, S-B3; the run-final proof, W3-3b) -----------------------------------------

#: the lineage sink's row formats and the longest line read (a row is under 2 KiB; a longer line is no row)
_LINEAGE_FORMAT = "levain-dispatch-lineage/3"
_FINAL_FORMAT = "levain-dispatch-run-final/1"
_LINE_MAX = 65536
_INT_MAX = 2**63 - 1
#: [judged] how long the one USAGE read waits for keys' run final. The settle reads once (a proof that lands after it is
#: worth nothing), and the client's USAGE request gives up at its connect timeout of ten seconds, so the wait sits well
#: under that; the normal path is a handful of fsyncs, tens of milliseconds, and the wait covers a keys that learns of
#: the drop late. At the deadline the answer is None: the whole reservation.
FINAL_WAIT_S = 6.0
_POLL_S = 0.2


_DONE, _RETRY, _BAD = "done", "retry", "bad"


def _no_constant(name: str) -> None:
    raise ValueError(name)


def _micros(v: object) -> Optional[int]:
    return v if type(v) is int and 0 <= v <= _INT_MAX else None


class _SinkRead:
    """An incremental read of the sink for one run. :meth:`step` reads from where the last step stopped and answers
    one of three things: ``(_DONE, total)`` (the proof is whole and holds), ``(_RETRY, None)`` (not yet: no final so
    far, a tail not yet a whole line, or the sink not yet there) or ``(_BAD, None)`` (a proof that cannot become good by
    waiting: a malformed line, an unknown format, a figure that is no count, a final that disagrees with the rows,
    a second final, a quarantine count, a sink that cannot be opened for any reason but absence). A torn tail with
    ``torn_is_final`` is bad: the one-shot reader never waits."""

    def __init__(self, path: object, run_id: str, *, torn_is_final: bool) -> None:
        self._path, self._run, self._torn_final = path, run_id, torn_is_final
        self._offset, self._total, self._matched = 0, 0, 0
        self._triples: list = []
        self._finals: list = []

    def step(self) -> "tuple[str, Optional[int]]":
        try:
            fd = os.open(os.fspath(self._path), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return (_BAD, None) if self._torn_final else (_RETRY, None)
        except OSError:
            return _BAD, None
        try:
            fh = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)                       # fdopen took no ownership: this close is the only one
            raise
        try:
            with fh:                           # from here fh owns the descriptor: nothing closes it a second time
                if os.fstat(fh.fileno()).st_size < self._offset:
                    return _BAD, None          # a sink that shrank is no append-only record
                fh.seek(self._offset)
                while True:
                    raw = fh.readline(_LINE_MAX + 1)
                    if not raw:
                        break
                    if len(raw) > _LINE_MAX:
                        return _BAD, None
                    if not raw.endswith(b"\n"):
                        return (_BAD, None) if self._torn_final else (_RETRY, None)
                    self._offset += len(raw)
                    if not self._take(raw):
                        return _BAD, None
        except OSError:
            return _BAD, None
        return self._verdict()

    def _take(self, raw: bytes) -> bool:
        try:
            row = json.loads(raw.decode("utf-8"), parse_constant=_no_constant)
        except (ValueError, RecursionError):
            return False
        # every decision here keys on `format`, never on a key's presence: a run-final row's `quarantined`
        # is a COUNT, a `/3` row's `quarantined` is the quarantine marker
        fmt = row.get("format") if isinstance(row, dict) else None
        if fmt == _FINAL_FORMAT:
            if row.get("run_id") == self._run:
                self._finals.append(row)
            return len(self._finals) <= 1      # a second final is no proof, whatever follows
        if fmt != _LINEAGE_FORMAT:
            return False
        if "quarantined" in row:
            return True                        # a quarantine row names no run: the final's own count attributes it
        if row.get("run_id") != self._run:
            return True
        settled, hold, cost = row.get("settled"), _micros(row.get("hold_microdollars")), row.get("cost_microdollars")
        if hold is None or (cost is not None and _micros(cost) is None):
            return False
        if settled == "yes":
            figure = hold if cost is None else cost
        elif settled == "uncertain":
            figure = hold if cost is None else max(hold, cost)
        elif settled == "no":
            figure = 0
        else:
            return False
        src = row.get("source")
        if not isinstance(src, dict) or set(src) != {"ledger", "epoch", "ref"}:
            return False
        self._triples.append(src)
        self._total += figure
        self._matched += 1
        return True

    def _verdict(self) -> "tuple[str, Optional[int]]":
        if not self._finals:
            return _BAD if self._torn_final else _RETRY, None
        fin = self._finals[0]
        n, q, digest = fin.get("rows"), fin.get("quarantined"), fin.get("triples_digest")
        if type(n) is not int or type(q) is not int or q != 0 or n != self._matched or type(digest) is not str:
            return _BAD, None
        try:
            if triples_digest(self._triples) != digest:
                return _BAD, None
        except (KeyError, TypeError, IndexError):
            return _BAD, None
        return (_DONE, self._total) if self._total <= _INT_MAX else (_BAD, None)


def lineage_spend(path: object, run_id: str) -> Optional[int]:
    """The micro-dollars a run spent, summed from CONTROL's lineage sink (`L/lineage/lineage.jsonl`) by ``run_id``, and
    ONLY on the run's final row: keys writes ``levain-dispatch-run-final/1`` once, after every resolution of the run
    is durably in the sink, and it counts the run's rows and digests their source triples. The sum is returned only
    when the sink holds exactly one final row for the run, its ``quarantined`` is 0, the run's rows number its ``rows``
    and their source triples hash to its ``triples_digest`` (the ledger's one :func:`~levain.broker.ledger.
    triples_digest`). A sink holding the first of two calls (100 of 100 + 900) therefore never settles at 100. Rows and
    quarantine rows of OTHER runs do not matter to this one.

    Per row of the run: ``settled`` ``yes`` is its ``cost_microdollars`` (the hold when the cost is unsaid),
    ``uncertain`` the larger of the cost and the hold (a call that may have been charged), ``no`` 0 (released or
    refused). The dispatch uid reads the sink by group (the sink's own docstring, H6-4). Fail-closed: None, so the
    caller settles at the whole reservation, when there is no final or more than one, the final counts a quarantine, the
    count or the digest differs, the sink cannot be opened or read whole, any line is torn, over-long, not JSON or of an
    unknown format, or a row of the run carries a figure that is not a micro-dollar count.

    Known edge: a run keys never noted (a keys restart between the run's last exchange and its drop leaves a run with no
    hold and no row) gets no final and settles at its reservation: the safe direction."""
    if type(run_id) is not str or not run_id or not isinstance(path, (str, os.PathLike)):
        return None
    try:
        status, got = _SinkRead(path, run_id, torn_is_final=True).step()
    except Exception:  # noqa: BLE001 - an unreadable proof is no proof
        return None
    return got if status == _DONE else None


#: [judged] at most this many USAGE reads wait at once; any further read is one look and no wait, so a burst of drops
#: cannot hold every non-holder connection slot for the whole wait and leave END none (the run server's connection cap
#: leaves sixteen beyond its holder cap)
MAX_WAITERS = 8


def lineage_spend_reader(path: object, *, wait_s: float = FINAL_WAIT_S, poll_s: float = _POLL_S,
                         clock: Callable[[], float] = time.monotonic,
                         sleep: Callable[[float], Any] = time.sleep) -> Callable[[str], Optional[int]]:
    """:func:`lineage_spend` bound to the sink at ``path``: the ``spend`` a brokered ``RealLauncher`` is built with.
    The settle reads once, so the read WAITS for the run final, up to ``wait_s`` (:data:`FINAL_WAIT_S`), polling every
    ``poll_s``, but only while the proof can still arrive: no final yet, a torn tail or an absent sink read again (from
    the offset the last poll reached), and a proof that is bad (see :class:`_SinkRead`) answers None at once. At the
    deadline the answer is None. A reader that raises is None. At most :data:`MAX_WAITERS` reads wait together."""
    waiters = threading.BoundedSemaphore(MAX_WAITERS)

    def read(run_id: str) -> Optional[int]:
        if type(run_id) is not str or not run_id or not isinstance(path, (str, os.PathLike)):
            return None
        waits = waiters.acquire(blocking=False)
        try:
            src = _SinkRead(path, run_id, torn_is_final=False)
            deadline = clock() + (max(0.0, float(wait_s)) if waits else 0.0)
            while True:
                status, got = src.step()
                if status == _DONE:
                    return got
                if status == _BAD or clock() >= deadline:
                    return None
                sleep(poll_s)
        except Exception:  # noqa: BLE001 - an unreadable proof is no proof
            return None
        finally:
            if waits:
                waiters.release()
    return read
