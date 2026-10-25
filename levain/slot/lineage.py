"""CONTROL's lineage sink (S1 3c-6b; CONTRACT §2.1 `L/lineage/` row and §3.3 keys row; SCHEMAS §2.1
`levain-dispatch-lineage/3`; 3c design r3 §2.4 read through R4 §3 and the 3c-6 head rulings H6-2, H6-4, H6-9,
H6-11, H6-14..H6-17).

keys builds one `/3` record per upstream exchange of a TICKETED session and sends them to CONTROL in a
`LINEAGE-APPEND {envelope}` on its `KEYS-ATTACH` connection; CONTROL's verb checks the envelope and the records
(:func:`check_envelope`, :func:`check_record`) and hands this module the rows to keep. This module is the writer of
`L/lineage/lineage.jsonl` and nothing else: one canonical JSON object per line, `seq` assigned here (contiguous per
sink, from 1), opened `O_APPEND`, never truncated except a torn last line at the start, and no verb reads it back to
a peer (`_levain_dispatch` reads it by group, H6-4).

The idempotency key is the TRIPLE `(ledger_id, epoch, ref)` (R4 §3): :class:`LineageSink` keeps the set of triples
its sink holds (``membership``), rebuilt at :meth:`LineageSink.start` from the sink itself and never pruned. A row
whose triple is a member is acked without appending; order of arrival does not matter (no high-water).

**Start** (:meth:`LineageSink.start`): the sink is opened `O_RDWR | O_APPEND | O_CREAT | O_NOFOLLOW` (0640: the
directory's setgid gives it `levain-lineage`), `flock`ed exclusively (one CONTROL per sink, ``control-running``), read
whole. A last line with no newline is a torn append (a crash inside the write): it was never acked, so it is cut off
(`ftruncate` to the last newline, then a full fsync) before anything is appended after it. Any other line that does
not parse, is not its own canonical encoding, is not a lineage row (a full row must pass :func:`check_record`, as a
fresh record must), breaks `seq`'s contiguity, or repeats a member's triple refuses the start `lineage-corrupt`:
CONTROL never serves a sink it cannot read whole.

**Append** (:meth:`LineageSink.append`): the rows of ONE envelope (at most :data:`ENVELOPE_MAX`) are written as one
`write` and made durable with ONE full fsync (`F_FULLFSYNC` on darwin, no fallback, as the ledger's flush; `fsync`
elsewhere): group commit, H6-11. Membership and the next `seq` move only after that fsync returns. A failed write or
fsync POISONS the sink as `tickets.py` does (the sink is ambiguous: the rows may be on disk, whole or torn, while
membership never took them): the fd is closed and every append answers ``lineage-unavailable`` until CONTROL restarts;
the restart then reads those rows as the truth (complete lines: their resend is acked by membership) or drops them (a
torn tail: their resend is appended). Either way each triple ends with exactly one row.

**The fault seam** (H6-9): ``fault(point)`` is called at ``before_write``, ``before_fsync`` (the rows written, not yet
durable) and ``after_fsync`` (durable, membership not yet moved, nothing acked). A test passes one that raises at a
point to stand for CONTROL dying there; the poison rule makes the sink behave as a dead CONTROL's would until a new
:class:`LineageSink` starts on the same path.

Runs on the root interpreter (3.9)."""
from __future__ import annotations

import fcntl
import os
import re
import sys
import threading
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from levain.contracts import ContractError, canon
from levain.contracts.grammar import JSON_INT_MAX
from levain.slot import Refused
from levain.slot import records as R
from levain.slot import tickets as T

SINK = "lineage.jsonl"
FORMAT = "levain-dispatch-lineage/3"
#: H6-11 / CONTRACT §3.3: the most records one `LINEAGE-APPEND` envelope carries (one group commit).
ENVELOPE_MAX = 64
#: H6-19: the most bytes one envelope's canonical encoding holds (the control frame cap, 64 KiB, less headroom for
#: the frame's own keys). keys starts a new envelope at either bound; CONTROL refuses one over it.
ENVELOPE_BYTES_MAX = 48 * 1024
#: the sink file's mode at creation (CONTRACT §2.1 row: `_levain_control:levain-lineage` 0640).
SINK_MODE = 0o640

#: `(ledger_id, epoch, ref)` (R4 §3: 3c-1's hold ref, or a `refused` line's own seq, H6-10).
Triple = Tuple[str, str, int]

_HEX32 = re.compile(r"[0-9a-f]{32}")
#: DECISION: a quarantined field's name as the row records it; a name outside it (an unknown key keys invented) is
#: recorded as ``record``, so nothing keys sends reaches the sink unchecked.
_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
#: DECISION: `model_answered` / `provider_answered` are upstream response metadata (H6-6), not names CONTROL issued:
#: printable ASCII, 1..128 characters (an upstream provider name may hold a space).
_ANSWERED = re.compile(r"[\x20-\x7e]{1,128}")
#: DECISION: a refusal reason as CONTROL and keys name them (`keys-unavailable`, `lineage-record:envelope`, ...).
_REASON = re.compile(r"[a-z][a-z0-9:_-]{0,63}")
_ROUTE = re.compile(r"[a-z0-9-]{1,48}")                       # tickets.py's route id
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}")    # tickets.py's job id / model name
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
SETTLED = ("yes", "no", "uncertain")
#: DECISION: an HTTP status is three digits; 0 is a local refusal (SCHEMAS §2.1).
STATUS_MAX = 999


def _nothing(_point: str) -> None:
    return None


def full_fsync(fd: int) -> None:
    """The ledger's flush rule: `F_FULLFSYNC` on darwin with no fallback (a plain fsync there does not reach the
    medium), `os.fsync` elsewhere. A failure raises, and the append that called it poisons the sink."""
    if sys.platform == "darwin":
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
    else:
        os.fsync(fd)


def _corrupt(detail: str) -> Refused:
    return Refused("lineage-corrupt", detail)


def _envelope(detail: str) -> Refused:
    return Refused("lineage-record:envelope", detail)


# --- the record check (SCHEMAS §2.1, H6-15: every field present, one type per field) --------------------------------

def _str(pattern: "re.Pattern[str]") -> Callable[[object], bool]:
    return lambda v: type(v) is str and pattern.fullmatch(v) is not None


def _ts(v: object) -> bool:
    try:
        R.check_timestamp(v, "at")           # type: ignore[arg-type]
    except (Refused, TypeError):
        return False
    return True


def _uint(low: int = 0) -> Callable[[object], bool]:
    return lambda v: type(v) is int and low <= v <= JSON_INT_MAX


def _or_null(check: Callable[[object], bool]) -> Callable[[object], bool]:
    return lambda v: v is None or check(v)


#: every `/3` field keys sends, in the order the check reports the first bad one (SCHEMAS §2.1's block order; `seq`
#: is CONTROL's and `source` is checked with the envelope, H6-14). Nullable exactly where SCHEMAS §2.1 writes `|null`,
#: plus `model_answered` / `provider_answered` (H6-21: `string | "unknown" | null`; `null` = no upstream call, a
#: refused or released row; `"unknown"` = the call happened and the response did not say).
RECORD_CHECKS: Dict[str, Callable[[object], bool]] = {
    "format": lambda v: v == FORMAT and type(v) is str,
    "at": _ts,
    "ended_at": _ts,
    "run_id": _str(T.RUN_ID_RE),
    "job_id": _str(_NAME),
    "run_index": _uint(),
    "contract_digest": _str(_DIGEST),
    "route": _str(_ROUTE),
    "routemap_generation": _uint(1),
    "route_policy_digest": _str(_DIGEST),
    "model_requested": _str(_NAME),
    "model_answered": _or_null(_str(_ANSWERED)),
    "provider_answered": _or_null(_str(_ANSWERED)),
    "tokens_in": _or_null(_uint()),
    "tokens_out": _or_null(_uint()),
    "cost_microdollars": _or_null(_uint()),
    "cost_reported": _or_null(_uint()),
    "hold_microdollars": _uint(),
    "run_cap_microdollars": _or_null(_uint()),
    "settled": lambda v: type(v) is str and v in SETTLED,
    "overrun": _or_null(_uint(1)),
    "unproven": lambda v: type(v) is bool,
    "status": lambda v: type(v) is int and 0 <= v <= STATUS_MAX,
    "refused": lambda v: v == "" or _str(_REASON)(v),
    "count": _or_null(_uint(1)),
}
#: a full record's keys as the sink holds it
RECORD_KEYS = frozenset(RECORD_CHECKS) | {"seq", "source"}
#: a quarantine row's keys (R4 §3 read through H6-20: no `session`, which no source carries)
QUARANTINE_KEYS = frozenset({"format", "seq", "source", "quarantined"})


def check_triple(v: object) -> Optional[Triple]:
    """A `source` object `{ledger, epoch, ref}` (exactly those keys; ledger and epoch 32 lower-case hex, ref a JSON
    unsigned int) as a triple, or None."""
    if not isinstance(v, dict) or set(v) != {"ledger", "epoch", "ref"}:
        return None
    ledger, epoch, ref = v["ledger"], v["epoch"], v["ref"]
    if type(ledger) is not str or _HEX32.fullmatch(ledger) is None or type(epoch) is not str \
            or _HEX32.fullmatch(epoch) is None or type(ref) is not int or not 0 <= ref <= JSON_INT_MAX:
        return None
    return (ledger, epoch, ref)


def source_of(t: Triple) -> Dict[str, object]:
    return {"ledger": t[0], "epoch": t[1], "ref": t[2]}


def check_envelope(envelope: object) -> Tuple[Triple, ...]:
    """H6-2 / H6-14 / H6-19: `{ledger_id, epoch, records}`, 1..:data:`ENVELOPE_MAX` records and at most
    :data:`ENVELOPE_BYTES_MAX` bytes canonically encoded, each record an object whose `source` is a valid triple naming
    the envelope's `ledger_id` and `epoch`. Anything else refuses the WHOLE envelope ``lineage-record:envelope`` (a
    keys bug: nothing is appended). CONTROL sees only an envelope that fit the frame (an over-cap frame closes the
    connection before this runs); the byte bound refuses one that fit the frame but not H6-19. Returns each element's
    triple, in order."""
    if not isinstance(envelope, dict) or set(envelope) != {"ledger_id", "epoch", "records"}:
        raise _envelope("the envelope is exactly {ledger_id, epoch, records}")
    ledger_id, epoch, records = envelope["ledger_id"], envelope["epoch"], envelope["records"]
    if type(ledger_id) is not str or _HEX32.fullmatch(ledger_id) is None:
        raise _envelope("ledger_id")
    if type(epoch) is not str or _HEX32.fullmatch(epoch) is None:
        raise _envelope("epoch")
    if type(records) is not list or not 1 <= len(records) <= ENVELOPE_MAX:
        raise _envelope(f"records is a list of 1..{ENVELOPE_MAX}")
    try:
        size = len(canon.canonical_bytes(envelope))
    except (ContractError, ValueError, TypeError, RecursionError):
        raise _envelope("the envelope does not encode canonically") from None
    if size > ENVELOPE_BYTES_MAX:
        raise _envelope(f"the envelope encodes to {size} bytes, over {ENVELOPE_BYTES_MAX} (H6-19)")
    triples = []
    for rec in records:
        t = check_triple(rec.get("source")) if isinstance(rec, dict) else None
        if t is None:
            raise _envelope("an element's source is not a {ledger, epoch, ref} triple")
        if t[0] != ledger_id or t[1] != epoch:
            raise _envelope("an element's source names another ledger or epoch than the envelope (H6-14)")
        triples.append(t)
    return tuple(triples)


def check_record(rec: Mapping[str, object]) -> Optional[str]:
    """The first field of `rec` (one envelope element, its `source` already checked) that fails SCHEMAS §2.1 by name
    or type, or None for a well-formed record. A missing field is named; a key the schema does not hold (`seq`
    included: it is CONTROL's) is named when it reads as a field name, else ``record``."""
    for name, ok in RECORD_CHECKS.items():
        if name not in rec:
            return name
        if not ok(rec[name]):
            return name
    for k in sorted(set(rec) - set(RECORD_CHECKS) - {"source"}, key=str):
        return k if type(k) is str and _FIELD_NAME.fullmatch(k) else "record"
    return None


def _row_triple(obj: object) -> Optional[Tuple[Triple, Optional[str]]]:
    """A sink line read back: (its triple, its quarantined field or None), or None when it is not a row CONTROL
    writes (a full record of exactly :data:`RECORD_KEYS`, or a quarantine row of exactly :data:`QUARANTINE_KEYS`)."""
    if not isinstance(obj, dict) or obj.get("format") != FORMAT:
        return None
    keys = set(obj)
    if keys == QUARANTINE_KEYS:
        field = obj["quarantined"]
        if type(field) is not str or not (field == "record" or _FIELD_NAME.fullmatch(field)):
            return None
    elif keys == RECORD_KEYS:
        field = None
    else:
        return None
    t = check_triple(obj["source"])
    return None if t is None else (t, field)


class LineageSink:
    """The sink at `path` and the membership rebuilt from it. :meth:`start` (CONTROL's start pass) opens, locks, reads,
    cuts a torn tail; nothing is appended before it. Every public method takes the sink's own lock. ``fsync`` is the
    durability flush (one call per envelope, H6-11); ``fault`` the H6-9 seam."""

    def __init__(self, path: Path, *, fsync: Callable[[int], None] = full_fsync,
                 fault: Callable[[str], None] = _nothing) -> None:
        self.path = Path(path)
        self._fsync = fsync
        self.fault = fault
        self.lock = threading.Lock()
        self._fd: Optional[int] = None
        self._poisoned = False
        #: triple -> the field it was quarantined on, or None (a full record). Never pruned.
        self._members: Dict[Triple, Optional[str]] = {}
        self._next_seq = 1

    # -- the start ---------------------------------------------------------------------------------------------------

    def start(self) -> None:
        with self.lock:
            self._close()
            try:
                fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                             SINK_MODE)
            except OSError as e:
                raise Refused("lineage-unavailable", f"the lineage sink: {e.strerror}") from None
            try:
                if not (os.fstat(fd).st_mode & 0o170000) == 0o100000:
                    raise _corrupt("the lineage sink is not a regular file")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise Refused("control-running", f"another CONTROL holds {self.path}") from None
                members, next_seq, keep = self._read(fd)
                size = os.fstat(fd).st_size
                if keep != size:
                    os.ftruncate(fd, keep)        # the torn last line: never acked, so never kept
                    self._fsync(fd)
                # the sink's directory entry is durable before any ack (every start: one that died between the
                # create and this fsync left an entry the next start must still make durable)
                dfd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
                try:
                    self._fsync(dfd)
                finally:
                    os.close(dfd)
            except BaseException:
                os.close(fd)
                raise
            self._fd, self._members, self._next_seq, self._poisoned = fd, members, next_seq, False

    @staticmethod
    def _read(fd: int) -> Tuple[Dict[Triple, Optional[str]], int, int]:
        """(membership, the next seq, the byte length of the sink's complete lines)."""
        chunks = []
        off = 0
        while True:
            part = os.pread(fd, 1 << 20, off)
            if not part:
                break
            chunks.append(part)
            off += len(part)
        data = b"".join(chunks)
        cut = data.rfind(b"\n") + 1             # 0 when there is no complete line
        members: Dict[Triple, Optional[str]] = {}
        seq = 0
        for raw in data[:cut].split(b"\n")[:-1]:
            try:
                obj = canon.parse(raw)
            except (ContractError, ValueError, RecursionError):
                raise _corrupt(f"the line after seq {seq} does not parse") from None
            got = _row_triple(obj)
            if got is None:
                raise _corrupt(f"the line after seq {seq} is not a lineage row")
            # r1 F4: only a row CONTROL writes is taken into membership: its own canonical encoding, and a full row
            # passes the record check a fresh record gets (a quarantine row's one field `_row_triple` checked)
            try:
                canonical = canon.canonical_bytes(obj) == raw
            except (ContractError, ValueError, TypeError, RecursionError):
                canonical = False
            if not canonical:
                raise _corrupt(f"the line after seq {seq} is not canonical JSON")
            if got[1] is None and check_record({k: v for k, v in obj.items() if k != "seq"}) is not None:
                raise _corrupt(f"the line after seq {seq} is not a well-formed record")
            if type(obj["seq"]) is not int or obj["seq"] != seq + 1:
                raise _corrupt(f"seq is not contiguous after {seq}")
            seq += 1
            t, field = got
            if t in members:
                raise _corrupt(f"seq {seq} repeats a triple already in the sink")
            members[t] = field
        return members, seq + 1, cut

    # -- the append --------------------------------------------------------------------------------------------------

    def append(self, rows: Sequence[Tuple[Triple, Mapping[str, object]]]) -> List[Tuple[Triple, Optional[str]]]:
        """One envelope's rows, each `(triple, row-without-seq)`: every triple not yet a member is given the next `seq`
        and appended, all in ONE write and ONE full fsync (H6-11); a member (or a triple earlier in the same call) is
        not. Returns, per row in order, `(triple, quarantined field or None)` as the sink now holds it. Membership moves
        only after the fsync. Any failure poisons the sink (module docstring) and answers ``lineage-unavailable``."""
        with self.lock:
            self._live()
            assert self._fd is not None
            fresh: Dict[Triple, Optional[str]] = {}
            lines: List[bytes] = []
            seq = self._next_seq
            for t, row in rows:
                if t in self._members or t in fresh:
                    continue
                obj = dict(row)
                obj["seq"] = seq
                obj["source"] = source_of(t)
                field = obj.get("quarantined") if "quarantined" in obj else None
                lines.append(canon.canonical_bytes(obj) + b"\n")
                fresh[t] = field             # type: ignore[assignment]
                seq += 1
            if lines:
                data = b"".join(lines)
                try:
                    self.fault("before_write")
                    _write_all(self._fd, data)
                    self.fault("before_fsync")
                    self._fsync(self._fd)
                    self.fault("after_fsync")
                except BaseException as e:
                    self._poison()
                    if isinstance(e, Exception):
                        raise Refused("lineage-unavailable",
                                      f"a lineage append failed ({type(e).__name__})") from None
                    raise
                self._members.update(fresh)
                self._next_seq = seq
            return [(t, self._members[t]) for t, _row in rows]

    def _live(self) -> None:
        if self._poisoned:
            raise Refused("lineage-unavailable", "a lineage append failed; CONTROL must restart")
        if self._fd is None:
            raise Refused("lineage-unavailable", "the lineage sink is not open (CONTROL's start has not run)")

    def _poison(self) -> None:
        self._poisoned = True
        self._close()

    def _close(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            try:
                os.close(fd)                     # closing the only descriptor releases the flock
            except OSError:
                pass

    # -- views -------------------------------------------------------------------------------------------------------

    def is_member(self, t: Triple) -> bool:
        with self.lock:
            return t in self._members

    def members(self) -> int:
        with self.lock:
            return len(self._members)

    @property
    def next_seq(self) -> int:
        with self.lock:
            return self._next_seq

    def close(self) -> None:
        """The appender and its flock released (CONTROL's stop)."""
        with self.lock:
            self._close()


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


__all__ = ["ENVELOPE_BYTES_MAX", "ENVELOPE_MAX", "FORMAT", "LineageSink", "QUARANTINE_KEYS", "RECORD_CHECKS", "RECORD_KEYS", "SINK",
           "SINK_MODE", "Triple", "check_envelope", "check_record", "check_triple", "full_fsync", "source_of"]
