"""keys' reservation ledger (S1 3c-1, design `1023-1/3c/C1_DESIGN.md` r4): the append-only, flushed hold journal and
the synchronous admit.

A hold is admitted synchronously on the loop (`Ledger.admit`, no await), its `reserved` line queued for the one daemon
writer thread, which writes every queued line (group commit), flushes once and posts `_durable` back to the loop. Only
a durable `reserved` line makes a hold sendable (RESERVED), and only a durable resolution line takes a hold out of
`outstanding` (no credit before durable). The ledger owns each hold's lifecycle: `admit` registers a done-callback on
the task that admitted, so a hold the caller never resolved is resolved when that task ends, however it ended.

Any write or flush error in the writer, or a writer that completes no flush for `DURABLE_TIMEOUT_S` while a line is in
flight (the stall rule), makes the ledger UNHEALTHY for the life of the process: it admits nothing, sends nothing, and
every hold not RESOLVED is frozen at its full amount (STRANDED). The next `open` replays the file: every `reserved`
line with no resolution is booked `uncertain {restart}` at its full amount.

The flush is strict: `F_FULLFSYNC` on darwin with no fallback, `os.fsync` elsewhere, and neither flush function holds
an `except`. Every open-time I/O error refuses the open by name (`ledger-io:<stage>:<errno>`).

Known edges (L3 build-a r1, complement LOW 2; not built, conservative, never a loss): the stall rule is evaluated only
when a `durable()` wait times out. "If the writer hangs while only resolution lines are in flight (every hold already
SENT, then settled), nothing ever evaluates the stall rule. The ledger stays "healthy" and those holds sit RESOLVING at
full amount until a new admit's `durable()` times out." Charged never drops early.

Format `/3` (S1 3c-6a; R4 `1023-2/3c/R4_DESIGN.md` §3, rulings `1024-13/3c6/HEAD_RULINGS_3C6.md` H6-5, H6-10, H6-12,
H6-17) adds the lineage outbox. A new file is written `/3`; a `/2` file is read as it stands and appended to with `/3`
lines (its header is never rewritten), so a `/2` build refuses that file `ledger-corrupt:key` once it holds one.
- The lineage payload is a dict at `meta["lineage"]` on a resolution line or a `refused` line (a `/2` file carries it the
  same way, R4 §10); its schema is keys' (3c-6c), the ledger only carries it. A `reserved` line's
  `meta["lineage_base"]` marks a ticketed hold: every resolution of it carries a payload, the caller's
  `meta["lineage"]` when given, else `lineage_payload(base, outcome, at)` (so `released` (H6-5), `owner-ended`, `closed`
  and replay's `restart` lines all have their row).
- `released` gains `meta` (a `/2` `released` line has none and is still read).
- `refused {seq, epoch, session, run_id, route, reason, count, meta}` (H6-10: the current epoch; its triple's ref is the
  line's own seq). `run_id` is a string or null. keys writes it at a window's close.
- `lineage-acked {seq, refs: [{ledger, epoch, ref}..], quarantined?: [{source: <triple>, field}]}` (H6-17). Replay refuses
  an ack naming a triple with no payload line before it (`ack-orphan`) or a quarantine outside the line's refs
  (`ack-quarantine`); a repeated ack of an acked triple is not corrupt.
- The outbox is every durable payload line with no durable ack. A triple is `(ledger_id, epoch, ref)`: a resolution's
  ref and epoch are its `reserved` line's (so a restart line for an old epoch keeps the old triple, H6-12), a `refused`
  line's are its own. `unacked()` lists them in ref order; `ack()` writes one `lineage-acked` line and moves its refs out
  of the outbox only in its future's done-callback, after the flush.

Runs on the root interpreter (3.9): no `match`, no 3.10+ API. Standard library imports only (a tripwire holds it).
"""
from __future__ import annotations

import asyncio
import datetime
import enum
import errno
import fcntl
import functools
import inspect
import json
import logging
import os
import queue
import re
import secrets
import sys
import threading
import time
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

FORMAT = "levain-reservation-ledger/3"
FORMAT_VERSION = 3
#: the header formats this build reads (a `/2` file is read as it stands)
READ_FORMATS = frozenset({"levain-reservation-ledger/2", FORMAT})
META_MAX = 4096                  # bytes of a meta object's canonical encoding
PRICE_REF_MAX = 256
#: its value is `creds.LOCK_TIMEOUT_S`, keys' existing bound for a stalled durable-state operation (design 1.8 / R8),
#: written here rather than imported so this module stays standard-library only; a test asserts the two are equal
DURABLE_TIMEOUT_S = 10.0
_READ_CHUNK = 1 << 20
_META_DEPTH = 32

logger = logging.getLogger("levain.broker.ledger")

#: reasons the ledger writes itself; a caller's `uncertain(reason)` may not use them
RESERVED_REASONS = frozenset({"ended", "owner-ended", "closed", "restart"})

_KIND_RE = re.compile(r"[a-z][a-z-]{0,31}")
_HEX32_RE = re.compile(r"[0-9a-f]{32}")
_PRICE_REF_RE = re.compile(r"[\x20-\x7e]{1,%d}" % PRICE_REF_MAX)
_REASON_RE = re.compile(r"[a-z0-9-]{1,32}")
_AT_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z")
_FORMAT_RE = re.compile(r"levain-reservation-ledger/([0-9]{1,9})")
#: a `refused` line's session, route and run_id: printable ASCII, no space
_ID_RE = re.compile(r"[\x21-\x7e]{1,128}")
#: a quarantined record's field name, as CONTROL names it
_FIELD_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")

#: each kind's allowed key sets (a `/2` `released` has no `meta`; `lineage-acked` carries `quarantined` only when any)
_KEYS = {
    "header": (frozenset({"kind", "seq", "format", "ledger_id", "created_at"}),),
    "opened": (frozenset({"kind", "seq", "epoch", "at"}),),
    "reserved": (frozenset({"kind", "seq", "epoch", "scope", "cap", "amount", "price_ref", "meta", "at"}),),
    "released": (frozenset({"kind", "seq", "ref", "meta", "at"}), frozenset({"kind", "seq", "ref", "at"})),
    "settled": (frozenset({"kind", "seq", "ref", "settled", "overrun", "meta", "at"}),),
    "uncertain": (frozenset({"kind", "seq", "ref", "reason", "meta", "at"}),),
    "refused": (frozenset({"kind", "seq", "epoch", "session", "run_id", "route", "reason", "count", "meta", "at"}),),
    "lineage-acked": (frozenset({"kind", "seq", "refs", "at"}), frozenset({"kind", "seq", "refs", "quarantined", "at"})),
}
_RESOLUTIONS = ("released", "settled", "uncertain")

RELEASED = "released"
SETTLED = "settled"
UNCERTAIN = "uncertain"


# ---- errors -------------------------------------------------------------------------------------------------------

class Refused(Exception):
    """`budget-exhausted`: the scope has no room for the hold (keys answers 402)."""

    def __init__(self, name: str, detail: str = "") -> None:
        super().__init__(name)
        self.name = name
        self.detail = detail


class LedgerUnavailable(Exception):
    """`keys-unavailable` (keys answers 503): the ledger is UNHEALTHY or closing, or the hold cannot be sent."""
    name = "keys-unavailable"

    def __init__(self, detail: str = "") -> None:
        super().__init__("keys-unavailable" + (": " + detail if detail else ""))
        self.detail = detail


class LedgerMisuse(Exception):
    """A programming error: a malformed argument, a call off the loop thread or from inside `bound`, or a call the
    hold's state does not allow (the `misuse` cells of the transition table)."""


class LedgerOpenError(Exception):
    """A start refusal (keys exits 3). `name` is `ledger-locked`, `ledger-format-newer`, `ledger-corrupt:<what>:<line>`
    or `ledger-io:<stage>:<errno>`; `line` is the text keys prints."""

    def __init__(self, name: str, line: str) -> None:
        super().__init__(name)
        self.name = name
        self.line = line


# ---- primitives (module-level so a test can replace each one) -------------------------------------------------------

def _flush(fd: int) -> None:
    """Every file flush: open-time and the writer's batches. Any error propagates (no fallback, no mode)."""
    if sys.platform == "darwin":
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
    else:
        os.fsync(fd)


def _dir_flush(dirfd: int) -> None:
    """The ledger's directory, on every open: the same call on a directory fd."""
    _flush(dirfd)


def _os_write(fd: int, data: Any) -> int:
    return os.write(fd, data)


def _os_close(fd: int) -> None:
    os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    """One write loop: a short write retries; an error ends it."""
    view = memoryview(data)
    while view:
        n = _os_write(fd, view)
        if n <= 0:
            raise OSError(errno.EIO, "a write wrote no bytes")
        view = view[n:]


def _note(level: int, msg: str, *a: object) -> None:
    """Never raises an `Exception` into its caller (the writer thread and the loop callbacks call it)."""
    try:
        logger.log(level, msg, *a)
    except Exception:
        pass


def _errno_name(e: BaseException) -> str:
    if isinstance(e, OSError) and e.errno is not None:
        return errno.errorcode.get(e.errno, str(e.errno))
    return type(e).__name__


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _encode(obj: dict) -> bytes:
    """The one canonical line encoding."""
    return (json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
            + "\n").encode("ascii")


def _json_ok(v: Any, depth: int = 0) -> bool:
    """A JSON value with no floats (bool, int, str, None, list, dict with str keys), nested at most _META_DEPTH."""
    if depth > _META_DEPTH:
        return False
    t = type(v)
    if v is None or t is bool or t is int or t is str:
        return True
    if t is list:
        return all(_json_ok(x, depth + 1) for x in v)
    if t is dict:
        return all(type(k) is str and _json_ok(x, depth + 1) for k, x in v.items())
    return False


def _meta_ok(meta: Any) -> bool:
    if type(meta) is not dict or not _json_ok(meta):
        return False
    return len(json.dumps(meta, sort_keys=True, separators=(",", ":"), ensure_ascii=True)) <= META_MAX


#: the lineage `settled` word for each outcome (H6-5: a released exchange is a row with "no")
_SETTLED_WORD = {"released": "no", "settled": "yes", "uncertain": "uncertain"}
#: the longest `at` the ledger writes (`_now()`'s shape), for the size check of a payload built later
_AT_SAMPLE = "0000-00-00T00:00:00.000000Z"


def lineage_payload(base: dict, outcome: str, at: str) -> dict:
    """The payload of a ledger-written resolution of a ticketed hold: its `lineage_base` plus the outcome (the seam
    3c-6c fills: keys' base already holds every field the ledger cannot know)."""
    p = json.loads(json.dumps(base))
    p["settled"] = _SETTLED_WORD[outcome]
    p["ended_at"] = at
    return p


def _lineage_ok(meta: dict) -> bool:
    """`meta["lineage"]` and `meta["lineage_base"]`, when present, are objects; a base must leave room for the payload
    the ledger may build from it (a replay `restart` line that did not fit would be refused by the next replay)."""
    if "lineage" in meta and type(meta["lineage"]) is not dict:
        return False
    if "lineage_base" in meta:
        base = meta["lineage_base"]
        if type(base) is not dict:
            return False
        if not _meta_ok({"lineage": lineage_payload(base, UNCERTAIN, _AT_SAMPLE)}):
            return False
    return True


def _triple_ok(t: Any) -> bool:
    """An on-disk triple object `{ledger, epoch, ref}` (H6-17)."""
    return (type(t) is dict and frozenset(t) == frozenset({"ledger", "epoch", "ref"})
            and type(t["ledger"]) is str and bool(_HEX32_RE.fullmatch(t["ledger"]))
            and type(t["epoch"]) is str and bool(_HEX32_RE.fullmatch(t["epoch"]))
            and _is_int(t["ref"]) and t["ref"] >= 1)


def _triple_obj(t: "Ref") -> dict:
    return {"ledger": t[0], "epoch": t[1], "ref": t[2]}


def _scope_ok(scope: Any) -> bool:
    """A tuple (in memory) or list (on disk): scope[0] a kind, every other element a non-empty str."""
    if not scope or type(scope[0]) is not str or not _KIND_RE.fullmatch(scope[0]):
        return False
    return all(type(x) is str and x for x in scope[1:])


def _is_int(v: Any) -> bool:
    return type(v) is int


def _set_result(fut: "asyncio.Future") -> None:
    if not fut.done():
        fut.set_result(None)


def _set_unavailable(fut: "asyncio.Future", detail: str) -> None:
    if not fut.done():
        fut.set_exception(LedgerUnavailable(detail))


def _retrieve(fut: "asyncio.Future") -> None:
    """Reads a failed future's exception so a STRANDED hold whose owner is gone logs no 'never retrieved'."""
    if fut.cancelled():
        return
    fut.exception()


# ---- types --------------------------------------------------------------------------------------------------------

class HoldState(enum.Enum):
    PENDING = "pending"        # admitted, `reserved` line queued
    RESERVED = "reserved"      # that line durable: the only sendable state
    SENT = "sent"              # the head handed upstream
    RESOLVING = "resolving"    # resolution line queued; still in `outstanding`
    RESOLVED = "resolved"      # resolution line durable; terminal
    STRANDED = "stranded"      # the ledger went UNHEALTHY first, or a per-hold callback failed; terminal


PENDING = HoldState.PENDING
RESERVED = HoldState.RESERVED
SENT = HoldState.SENT
RESOLVING = HoldState.RESOLVING
RESOLVED = HoldState.RESOLVED
STRANDED = HoldState.STRANDED


class Totals(NamedTuple):
    settled: int
    overrun: int
    uncertain_restart: int
    uncertain_other: int
    outstanding: int

    @property
    def uncertain(self) -> int:
        return self.uncertain_restart + self.uncertain_other

    @property
    def charged(self) -> int:
        return self.settled + self.overrun + self.uncertain_restart + self.uncertain_other + self.outstanding


# per-scope accumulator indexes
_S, _O, _UR, _UO, _OUT = range(5)

Scope = Tuple[str, ...]
BoundFn = Callable[[int], int]
Ref = Tuple[str, str, int]


class _Close:
    def __repr__(self) -> str:
        return "<CLOSE>"


_CLOSE = _Close()


class Hold:
    """One reservation. Every method runs on the attached loop's thread; all are synchronous but `durable`. The
    resolve methods return True for the call that decided the outcome and False for any later one (a no-op)."""
    __slots__ = ("ref", "amount", "scope", "state", "outcome", "gated", "fut", "base", "_ledger")

    def __init__(self, ledger: "Ledger", ref: Ref, amount: int, scope: Scope, fut: "asyncio.Future",
                 base: Optional[dict] = None) -> None:
        self._ledger = ledger
        self.ref = ref
        self.amount = amount
        self.scope = scope
        self.state = PENDING
        self.outcome: Optional[tuple] = None    # (RELEASED,) | (SETTLED, settled, overrun) | (UNCERTAIN, reason)
        self.gated = False
        self.fut = fut
        self.base = base                        # the `reserved` line's `lineage_base`: a ticketed hold

    @property
    def seq(self) -> int:
        return self.ref[2]

    def __repr__(self) -> str:
        return "Hold(seq=%d, amount=%d, %s, %r)" % (self.seq, self.amount, self.state.value, self.outcome)

    async def durable(self) -> None:
        await self._ledger._hold_durable(self)

    def send_gate(self) -> None:
        self._ledger._hold_send_gate(self)

    def mark_sent(self) -> None:
        self._ledger._hold_mark_sent(self)

    def end(self) -> bool:
        self._ledger._check_loop()
        return self._ledger._end(self, "ended")

    def release(self) -> bool:
        return self._ledger._hold_release(self)

    def settle(self, cost: int, meta: Optional[dict] = None) -> bool:
        return self._ledger._hold_settle(self, cost, {} if meta is None else meta)

    def uncertain(self, reason: str, meta: Optional[dict] = None) -> bool:
        return self._ledger._hold_uncertain(self, reason, {} if meta is None else meta)


# ---- open and replay ----------------------------------------------------------------------------------------------

def _io_error(stage: str, e: OSError, path: str) -> LedgerOpenError:
    name = "ledger-io:%s:%s" % (stage, _errno_name(e))
    if stage.startswith("flush-") and sys.platform == "darwin":
        line = ("levain gw-keys: ledger refused: F_FULLFSYNC failed at %s (%s); the ledger's filesystem must "
                "support F_FULLFSYNC" % (stage, _errno_name(e)))
    else:
        line = "levain gw-keys: ledger refused: %s (%s)" % (name, path)
    return LedgerOpenError(name, line)


def _corrupt(what: str, lineno: int, path: str) -> LedgerOpenError:
    name = "ledger-corrupt:%s:%d" % (what, lineno)
    return LedgerOpenError(name, "levain gw-keys: ledger refused: %s (%s)" % (name, path))


def _no_dup(pairs: List[Tuple[str, Any]]) -> dict:
    d: dict = {}
    for k, v in pairs:
        if k in d:
            raise ValueError("a repeated key")
        d[k] = v
    return d


def _no_float(s: str) -> Any:
    raise ValueError("a float")


def _parse(raw: bytes) -> Any:
    """json.loads with no duplicate key, no float and no NaN/Infinity; raises ValueError (or RecursionError)."""
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_no_dup, parse_float=_no_float,
                      parse_constant=_no_float)


def _type_ok(kind: str, o: dict) -> bool:
    for k in ("seq", "cap", "amount", "ref", "settled", "overrun", "count"):
        if k in o and not _is_int(o[k]):
            return False
    if kind == "header":
        return (type(o["format"]) is str and o["format"] in READ_FORMATS and type(o["ledger_id"]) is str
                and bool(_HEX32_RE.fullmatch(o["ledger_id"])) and type(o["created_at"]) is str
                and bool(_AT_RE.fullmatch(o["created_at"])))
    if type(o["at"]) is not str or not _AT_RE.fullmatch(o["at"]):
        return False
    if "epoch" in o and (type(o["epoch"]) is not str or not _HEX32_RE.fullmatch(o["epoch"])):
        return False
    if kind == "reserved":
        if type(o["scope"]) is not list or not _scope_ok(o["scope"]):
            return False
        if type(o["price_ref"]) is not str or not _PRICE_REF_RE.fullmatch(o["price_ref"]):
            return False
    if kind in ("uncertain", "refused") and (type(o["reason"]) is not str or not _REASON_RE.fullmatch(o["reason"])):
        return False
    if kind == "refused":
        if type(o["session"]) is not str or not _ID_RE.fullmatch(o["session"]):
            return False
        if type(o["route"]) is not str or not _ID_RE.fullmatch(o["route"]):
            return False
        if o["run_id"] is not None and (type(o["run_id"]) is not str or not _ID_RE.fullmatch(o["run_id"])):
            return False
    if kind == "lineage-acked":
        refs = o["refs"]
        if type(refs) is not list or not refs or not all(_triple_ok(t) for t in refs):
            return False
        if "quarantined" in o:
            q = o["quarantined"]
            if type(q) is not list or not q:
                return False
            for e in q:
                if (type(e) is not dict or frozenset(e) != frozenset({"source", "field"}) or not _triple_ok(e["source"])
                        or type(e["field"]) is not str or not _FIELD_RE.fullmatch(e["field"])):
                    return False
    if "meta" in o and not (_meta_ok(o["meta"]) and _lineage_ok(o["meta"])):
        return False
    return True


def _triple_key(t: dict) -> "Ref":
    return (t["ledger"], t["epoch"], t["ref"])


class _Replay:
    """The replay of §4.3 step 3-4 over complete lines: the rules in order, then the per-scope totals."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.ledger_id = ""
        self.last_seq = -1
        self.epochs: set = set()
        self.epoch: Optional[str] = None
        # seq -> (scope, amount, epoch, lineage_base or None)
        self.reserved: Dict[int, Tuple[Scope, int, str, Optional[dict]]] = {}
        self.resolved: set = set()
        self.acc: Dict[Scope, List[int]] = {}
        self.known: set = set()                       # every triple a payload line named
        self.outbox: Dict[Ref, dict] = {}             # payload lines with no ack yet
        self.quarantined: Dict[Ref, str] = {}

    def payload(self, triple: "Ref", meta: dict) -> None:
        if "lineage" in meta:
            self.known.add(triple)
            self.outbox[triple] = meta["lineage"]

    def line(self, n: int, raw: bytes) -> None:
        p = self.path
        try:
            o = _parse(raw)
        except (ValueError, RecursionError):
            raise _corrupt("json", n, p)
        if _encode(o) != raw + b"\n":
            raise _corrupt("canonical", n, p)
        if type(o) is not dict:
            raise _corrupt("header" if n == 1 else "key", n, p)
        kind = o.get("kind")
        if (n == 1) != (kind == "header"):
            raise _corrupt("header", n, p)
        seq = o.get("seq")
        if not _is_int(seq) or (n == 1 and seq != 0) or (n > 1 and seq <= self.last_seq):
            raise _corrupt("seq", n, p)
        if type(kind) is not str or kind not in _KEYS or frozenset(o) not in _KEYS[kind]:
            raise _corrupt("key", n, p)
        if not _type_ok(kind, o):
            raise _corrupt("type", n, p)
        self.last_seq = seq
        if kind == "header":
            self.ledger_id = o["ledger_id"]
        elif kind == "opened":
            if o["epoch"] in self.epochs:
                raise _corrupt("epoch-dup", n, p)
            self.epochs.add(o["epoch"])
            self.epoch = o["epoch"]
        elif kind == "reserved":
            if o["epoch"] != self.epoch:
                raise _corrupt("epoch", n, p)
            if o["amount"] < 1 or o["cap"] < 0:
                raise _corrupt("amount", n, p)
            self.reserved[seq] = (tuple(o["scope"]), o["amount"], o["epoch"], o["meta"].get("lineage_base"))
        elif kind == "refused":
            if o["epoch"] != self.epoch:
                raise _corrupt("epoch", n, p)
            if o["count"] < 1:
                raise _corrupt("amount", n, p)
            self.payload((self.ledger_id, o["epoch"], seq), o["meta"])
        elif kind == "lineage-acked":
            refs = [_triple_key(t) for t in o["refs"]]
            for t in refs:
                if t not in self.known:
                    raise _corrupt("ack-orphan", n, p)
            q = {}
            for e in o.get("quarantined", ()):
                src = _triple_key(e["source"])
                if src not in refs:
                    raise _corrupt("ack-quarantine", n, p)
                q[src] = e["field"]
            for t in refs:
                self.outbox.pop(t, None)
            self.quarantined.update(q)
        else:
            ref = o["ref"]
            if ref not in self.reserved:
                raise _corrupt("orphan", n, p)
            if ref in self.resolved:
                raise _corrupt("double", n, p)
            scope, amount, epoch, _base = self.reserved[ref]
            if kind == "settled" and (o["settled"] < 0 or o["overrun"] < 0 or o["settled"] > amount):
                raise _corrupt("amount", n, p)
            self.resolved.add(ref)
            self.payload((self.ledger_id, epoch, ref), o.get("meta", {}))
            acc = self.acc.setdefault(scope, [0, 0, 0, 0, 0])
            if kind == "settled":
                acc[_S] += o["settled"]
                acc[_O] += o["overrun"]
            elif kind == "uncertain":
                acc[_UR if o["reason"] == "restart" else _UO] += amount

    def open_refs(self) -> List[int]:
        return sorted(s for s in self.reserved if s not in self.resolved)


def _newer(raw: bytes) -> bool:
    """The header-version check, made before every other rule: a parseable first line naming a later format."""
    try:
        o = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return False
    if type(o) is not dict or o.get("kind") != "header" or type(o.get("format")) is not str:
        return False
    m = _FORMAT_RE.fullmatch(o["format"])
    return bool(m) and int(m.group(1)) > FORMAT_VERSION


#: a torn first line still names its format when the cut falls after the version's closing quote
_TORN_FORMAT_RE = re.compile(rb'"format":"levain-reservation-ledger/([0-9]{1,9})"')


def _torn_newer(torn: bytes) -> bool:
    """The zero-complete-lines case (§4.2 recreates it): a torn line that still names a later format is refused
    `ledger-format-newer` and left untouched, since the newer build's rule for its own file is not ours to apply. A cut
    before the version's closing quote names no version, and §4.2's recreate stands: no acknowledged line is lost,
    since nothing is admitted before the header and the first `opened` line are durable."""
    m = _TORN_FORMAT_RE.search(torn)
    return bool(m) and int(m.group(1)) > FORMAT_VERSION


def _newer_error(path: str) -> "LedgerOpenError":
    return LedgerOpenError("ledger-format-newer", "levain gw-keys: ledger refused: ledger-format-newer "
                           "(%s names a format above %s)" % (path, FORMAT))


def _read_all(fd: int) -> bytes:
    parts = []
    off = 0
    while True:
        b = os.pread(fd, _READ_CHUNK, off)
        if not b:
            return b"".join(parts)
        parts.append(b)
        off += len(b)


class Ledger:
    """The hold journal. `open` (synchronous, before the loop), then `attach(loop)` on the loop, then `admit`.

    The loop owns every piece of state `admit`, `send_gate`, `durable` and the resolve methods touch. The writer thread
    owns the fd, its own `dead` flag and `_flushed_at`. They share the line queue, the `stop` event, the loop's
    `call_soon_threadsafe` and `_flushed_at` behind `_progress_lock`."""

    def __init__(self, path: str, fd: int, ledger_id: str, epoch: str, next_seq: int,
                 acc: Dict[Scope, List[int]]) -> None:
        self.path = path
        self.ledger_id = ledger_id
        self.epoch = epoch
        self._fd = fd
        self._next_seq = next_seq
        self._acc = acc
        # loop-owned
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._tid: Optional[int] = None
        self._healthy = True
        self._fault_reason: Optional[str] = None
        self._closing = False
        self._closed_fut: Optional[asyncio.Future] = None
        self._live: Dict[int, Hold] = {}
        self._lines: Dict[int, Tuple[Optional[Hold], str]] = {}    # a hold-less line (refused, ack) has None
        self._line_futs: Dict[int, asyncio.Future] = {}             # a hold-less line's durable future
        self._pending_outbox: Dict[int, Tuple[Ref, dict]] = {}      # a payload line not yet durable
        # the outbox (loop-owned; replay fills it at open)
        self._known: set = set()                                    # every durable payload triple
        self._outbox: Dict[Ref, dict] = {}                          # durable payload lines with no durable ack
        self._ack_pending: set = set()                              # triples in an ack line not yet durable
        self._quarantined: Dict[Ref, str] = {}
        #: called on the loop as `on_outbox(triple, payload)` when a payload line becomes durable (3c-6c's sender)
        self.on_outbox: Optional[Callable[[Ref, dict], None]] = None
        self._inflight_since = 0.0
        self._in_bound = False
        self._bound_misused = False
        # shared with the writer
        self._q: "queue.SimpleQueue" = queue.SimpleQueue()
        self._stop = threading.Event()
        self._progress_lock = threading.Lock()
        self._flushed_at = 0.0
        self._thread: Optional[threading.Thread] = None

    # -- open ----------------------------------------------------------------------------------------------------

    @classmethod
    def open(cls, path: str) -> "Ledger":
        """Lock, replay, torn tail, directory flush, a new epoch: synchronous, before the loop. Every refusal is a
        `LedgerOpenError`; the fd is closed on every refusal."""
        path = os.path.abspath(path)
        dirpath = os.path.dirname(path)
        flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC
        created = False
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            try:
                fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            except OSError as e:
                raise _io_error("open", e, path)
            created = True
        except OSError as e:
            raise _io_error("open", e, path)
        try:
            return cls._open_fd(path, dirpath, fd, created)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

    @classmethod
    def _open_fd(cls, path: str, dirpath: str, fd: int, created: bool) -> "Ledger":
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LedgerOpenError("ledger-locked", "levain gw-keys: ledger refused: ledger-locked (another process "
                                  "holds %s)" % path)
        except OSError as e:
            raise _io_error("open", e, path)
        data = b""
        torn = b""
        rp: Optional[_Replay] = None
        if not created:
            try:
                data = _read_all(fd)
            except OSError as e:
                raise _io_error("read", e, path)
            keep = data.rfind(b"\n") + 1                   # the torn tail: the one line a kill or short write leaves
            data, torn = data[:keep], data[keep:]
            # No byte is written before the checks pass: the complete lines are checked first (newer format, then
            # every corruption rule), and only then is the torn tail truncated or the header recreated.
            if data:
                rp = cls._replay(path, data)
            elif _torn_newer(torn):
                raise _newer_error(path)
            if torn:
                try:
                    os.ftruncate(fd, keep)
                except OSError as e:
                    raise _io_error("truncate", e, path)
                try:
                    _flush(fd)
                except OSError as e:
                    raise _io_error("flush-truncate", e, path)
        if not data:                                       # create, or a file with zero complete lines
            header = _encode({"kind": "header", "seq": 0, "format": FORMAT, "ledger_id": secrets.token_hex(16),
                              "created_at": _now()})
            try:
                _write_all(fd, header)
            except OSError as e:
                raise _io_error("write", e, path)
            try:
                _flush(fd)
            except OSError as e:
                raise _io_error("flush-header", e, path)
            rp = cls._replay(path, header)
        assert rp is not None
        # step 5: the directory, then the `opened` batch
        try:
            dirfd = os.open(dirpath, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except OSError as e:
            raise _io_error("open", e, path)
        try:
            _dir_flush(dirfd)
        except BaseException as e:
            try:
                _os_close(dirfd)
            except OSError as ce:
                _note(logging.WARNING, "levain gw-keys: ledger directory close failed after a flush error (%s)",
                      _errno_name(ce))
            if isinstance(e, OSError):
                raise _io_error("flush-dir", e, path)
            raise
        try:
            _os_close(dirfd)
        except OSError as e:
            raise _io_error("open", e, path)
        epoch = secrets.token_hex(16)
        seq = rp.last_seq + 1
        at = _now()
        batch = [_encode({"kind": "opened", "seq": seq, "epoch": epoch, "at": at})]
        for ref in rp.open_refs():
            seq += 1
            scope, amount, r_epoch, base = rp.reserved[ref]
            # a ticketed hold's restart line carries its row (built from its base; its triple keeps the OLD epoch)
            meta = {} if base is None else {"lineage": lineage_payload(base, UNCERTAIN, at)}
            batch.append(_encode({"kind": "uncertain", "seq": seq, "ref": ref, "reason": "restart", "meta": meta,
                                  "at": at}))
            rp.acc.setdefault(scope, [0, 0, 0, 0, 0])[_UR] += amount
            rp.payload((rp.ledger_id, r_epoch, ref), meta)
        try:
            _write_all(fd, b"".join(batch))
        except OSError as e:
            raise _io_error("write", e, path)
        try:
            _flush(fd)
        except OSError as e:
            raise _io_error("flush-opened", e, path)
        led = cls(path, fd, rp.ledger_id, epoch, seq + 1, rp.acc)
        led._known, led._outbox, led._quarantined = rp.known, rp.outbox, rp.quarantined
        return led

    @staticmethod
    def _replay(path: str, data: bytes) -> "_Replay":
        """Step 3 on complete lines only: the header version first, then every corruption rule. Writes nothing."""
        lines = data.split(b"\n")[:-1]
        if _newer(lines[0]):
            raise _newer_error(path)
        rp = _Replay(path)
        for i, raw in enumerate(lines):
            rp.line(i + 1, raw)
        return rp

    # -- attach and the writer -----------------------------------------------------------------------------------

    def attach(self, loop: asyncio.AbstractEventLoop, *, _writer: bool = True) -> None:
        """Binds the loop (its thread is the caller's) and starts the daemon writer thread. `_writer=False` is a test
        seam: no thread, and the test posts `_durable` / `_fault` / `_closed` itself."""
        if self._loop is not None:
            raise LedgerMisuse("attach called twice")
        self._loop = loop
        self._tid = threading.get_ident()
        with self._progress_lock:
            self._flushed_at = time.monotonic()
        if _writer:
            self._thread = threading.Thread(target=self._writer_main, name="levain-ledger-writer", daemon=True)
            self._thread.start()

    def _post(self, fn: Callable, *args: Any) -> bool:
        """The writer's one channel to the loop. False when the loop is closed (the writer then ends)."""
        loop = self._loop
        if loop is None:
            return False
        try:
            loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:
            return False
        return True

    def _writer_main(self) -> None:
        fd = self._fd
        dead = False
        try:
            while True:
                items = [self._q.get()]
                while True:
                    try:
                        items.append(self._q.get_nowait())
                    except queue.Empty:
                        break
                closing = False
                batch = []
                for it in items:
                    if it is _CLOSE:
                        closing = True
                        break
                    batch.append(it)
                if batch and not dead and not self._stop.is_set():
                    seqs = [s for s, _ in batch]
                    stage = "write"
                    try:
                        _write_all(fd, b"".join(b for _, b in batch))
                        stage = "flush"
                        _flush(fd)
                    except Exception as e:
                        dead = True                            # first: no write or flush on this fd, ever again
                        if not self._post(self._fault, seqs, (stage, e)):
                            return
                    else:
                        with self._progress_lock:
                            self._flushed_at = time.monotonic()   # before the post (the stall rule reads it)
                        if not self._post(self._durable, seqs):
                            return
                if closing:
                    break
        finally:
            try:
                _os_close(fd)
            except OSError as e:
                _note(logging.WARNING, "levain gw-keys: ledger close failed (%s)", _errno_name(e))
        self._post(self._closed)

    # -- loop-side ---------------------------------------------------------------------------------------------

    def _check_loop(self) -> None:
        if self._tid is None or threading.get_ident() != self._tid:
            raise LedgerMisuse("a ledger call off the attached loop's thread")
        if self._in_bound:
            self._bound_misused = True
            raise LedgerMisuse("a ledger call from inside bound")

    def _check_read(self) -> None:
        if self._tid is not None:
            self._check_loop()

    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def fault_reason(self) -> Optional[str]:
        return self._fault_reason

    @property
    def closing(self) -> bool:
        return self._closing

    def _charged(self, scope: Scope) -> int:
        a = self._acc.get(scope)
        return sum(a) if a else 0

    def charged(self, scope: Scope) -> int:
        """settled + overrun + uncertain + outstanding (every hold not RESOLVED, durable or not, STRANDED included)."""
        self._check_read()
        if type(scope) is not tuple or not _scope_ok(scope):
            raise LedgerMisuse("a malformed scope")
        return self._charged(scope)

    def totals(self, scope: Scope) -> Totals:
        self._check_read()
        if type(scope) is not tuple or not _scope_ok(scope):
            raise LedgerMisuse("a malformed scope")
        return Totals(*self._acc.get(scope, [0, 0, 0, 0, 0]))

    def _take_seq(self) -> int:
        s = self._next_seq
        self._next_seq += 1
        return s

    def _submit(self, seq: int, data: bytes, hold: Optional[Hold], kind: str, *,
                fut: Optional[asyncio.Future] = None, outbox: Optional[Tuple[Ref, dict]] = None) -> None:
        """Enqueue only. `_inflight_since` moves only when nothing was in flight. `fut` (a hold-less line's) resolves
        when the line is durable; `outbox` enters the outbox then."""
        if not self._lines:
            self._inflight_since = time.monotonic()
        self._lines[seq] = (hold, kind)
        if fut is not None:
            self._line_futs[seq] = fut
        if outbox is not None:
            self._pending_outbox[seq] = outbox
        self._q.put((seq, data))

    def admit(self, scope: Scope, cap: int, bound: BoundFn, price_ref: str, meta: Optional[dict] = None) -> Hold:
        """One synchronous section on the loop: no await. Raises `LedgerUnavailable`, `LedgerMisuse`, `Refused`
        (`budget-exhausted`), or whatever `bound` raises (then nothing has changed)."""
        self._check_loop()
        if not self._healthy or self._closing:
            raise LedgerUnavailable("the ledger is %s" % ("closing" if self._healthy else "unhealthy"))
        if meta is None:
            meta = {}
        if type(scope) is not tuple or not _scope_ok(scope):
            raise LedgerMisuse("a malformed scope")
        if not _is_int(cap) or cap < 0:
            raise LedgerMisuse("a malformed cap")
        if type(price_ref) is not str or not _PRICE_REF_RE.fullmatch(price_ref):
            raise LedgerMisuse("a malformed price_ref")
        if not _meta_ok(meta) or not _lineage_ok(meta):
            raise LedgerMisuse("a malformed meta")
        if not callable(bound) or inspect.iscoroutinefunction(bound) or asyncio.iscoroutinefunction(bound):
            raise LedgerMisuse("bound must be a plain function")
        try:
            owner = asyncio.current_task()
        except RuntimeError:                     # no running loop
            owner = None
        if owner is None:
            raise LedgerMisuse("admit outside a task")
        room = cap - self._charged(scope)
        if room < 1:
            raise Refused("budget-exhausted", "no room in the scope")
        self._in_bound = True
        self._bound_misused = False
        try:
            amount = bound(room)
        finally:
            self._in_bound = False
        if self._bound_misused:
            self._bound_misused = False
            raise LedgerMisuse("a ledger call from inside bound")
        if inspect.iscoroutine(amount):
            amount.close()
            raise LedgerMisuse("bound returned a coroutine")
        if not _is_int(amount) or amount < 1:
            raise LedgerMisuse("bound returned %r" % (amount,))
        if amount > room:
            raise Refused("budget-exhausted", "the bound exceeds the room")
        meta_c = json.loads(json.dumps(meta))
        loop = self._loop
        assert loop is not None
        seq = self._take_seq()
        data = _encode({"kind": "reserved", "seq": seq, "epoch": self.epoch, "scope": list(scope), "cap": cap,
                        "amount": amount, "price_ref": price_ref, "meta": meta_c, "at": _now()})
        hold = Hold(self, (self.ledger_id, self.epoch, seq), amount, scope, loop.create_future(),
                    meta_c.get("lineage_base"))
        hold.fut.add_done_callback(_retrieve)
        self._acc.setdefault(scope, [0, 0, 0, 0, 0])[_OUT] += amount    # visible to the next admit at once
        self._live[seq] = hold
        owner.add_done_callback(functools.partial(self._owner_ended, hold))
        self._submit(seq, data, hold, "reserved")
        return hold

    def _resolve(self, hold: Hold, outcome: str, value: Any = None, meta: Optional[dict] = None) -> None:
        seq = self._take_seq()
        at = _now()
        meta = {} if meta is None else meta
        if hold.base is not None and "lineage" not in meta:
            # a ticketed hold's every resolution carries its row (H6-5, owner-ended, closed). It fits: admit checked
            # the base alone (the ledger's own resolutions pass `{}`), `_fits` checked a caller's meta with it
            meta = dict(meta)
            meta["lineage"] = lineage_payload(hold.base, outcome, at)
        line: Dict[str, Any] = {"seq": seq, "ref": hold.seq, "meta": meta, "at": at}
        if outcome == RELEASED:
            line["kind"] = "released"
            hold_outcome: tuple = (RELEASED,)
        elif outcome == SETTLED:
            settled, overrun = min(value, hold.amount), max(0, value - hold.amount)
            line.update(kind="settled", settled=settled, overrun=overrun)
            hold_outcome = (SETTLED, settled, overrun)
        else:
            line.update(kind="uncertain", reason=value)
            hold_outcome = (UNCERTAIN, value)
        data = _encode(line)
        hold.state = RESOLVING
        hold.outcome = hold_outcome
        _set_unavailable(hold.fut, "the hold was resolved before its reservation was durable")
        outbox = (hold.ref, meta["lineage"]) if "lineage" in meta else None
        self._submit(seq, data, hold, "resolution", outbox=outbox)

    def _end(self, hold: Hold, reason: str) -> bool:
        """The owner-end column: RELEASED if unsent, `uncertain {reason}` if sent, a no-op otherwise."""
        st = hold.state
        if st is PENDING or st is RESERVED:
            self._resolve(hold, RELEASED)
            return True
        if st is SENT:
            self._resolve(hold, UNCERTAIN, reason, {})
            return True
        return False

    def _strand(self, hold: Hold) -> None:
        try:
            hold.state = STRANDED
            _set_unavailable(hold.fut, "the ledger is unhealthy")
        finally:
            self._live.pop(hold.seq, None)

    def _owner_ended(self, hold: Hold, task: "asyncio.Future") -> None:
        """The backstop: the admitting task ended, however it ended."""
        try:
            self._end(hold, "owner-ended")
        except Exception as e:
            _note(logging.WARNING, "levain gw-keys: ledger owner callback failed for seq %d (%s); hold stranded",
                  hold.seq, type(e).__name__)
            try:
                self._strand(hold)
            except Exception:
                pass

    def _durable(self, line_seqs: List[int]) -> None:
        """Posted by the writer after a batch's flush returned. Each line in its own try: one failing step strands
        THAT hold only."""
        for s in line_seqs:
            ent = self._lines.pop(s, None)
            if ent is None:
                continue
            hold, kind = ent
            fut = self._line_futs.pop(s, None)
            ob = self._pending_outbox.pop(s, None)
            if hold is not None:
                try:
                    self._apply_durable(hold, kind)
                except Exception as e:
                    _note(logging.WARNING, "levain gw-keys: ledger durable step failed for seq %d (%s); hold stranded",
                          hold.seq, type(e).__name__)
                    try:
                        self._strand(hold)
                    except Exception:
                        pass
            if ob is not None:              # the line is in the file whatever happened to its hold: replay agrees
                self._outbox_add(*ob)
            if fut is not None:
                _set_result(fut)

    def _outbox_add(self, triple: Ref, payload: dict) -> None:
        self._known.add(triple)
        self._outbox[triple] = payload
        fn = self.on_outbox
        if fn is not None:
            try:
                fn(triple, payload)
            except Exception as e:
                _note(logging.WARNING, "levain gw-keys: ledger outbox listener failed for ref %d (%s)", triple[2],
                      type(e).__name__)

    def _apply_durable(self, hold: Hold, kind: str) -> None:
        if hold.state is STRANDED:
            return
        if kind == "reserved":
            if hold.state is PENDING:
                hold.state = RESERVED
                _set_result(hold.fut)
            return
        if hold.state is not RESOLVING:
            return
        assert hold.outcome is not None
        acc = self._acc[hold.scope]
        acc[_OUT] -= hold.amount
        if hold.outcome[0] == SETTLED:
            acc[_S] += hold.outcome[1]
            acc[_O] += hold.outcome[2]
        elif hold.outcome[0] == UNCERTAIN:
            acc[_UO] += hold.amount
        hold.state = RESOLVED
        self._live.pop(hold.seq, None)

    def _fault(self, line_seqs: Optional[List[int]], cause: Any) -> None:
        """The loop-side publication of a writer error or a stall. Idempotent: the first one decides."""
        if not self._healthy:
            return
        self._healthy = False
        if cause == "stall":
            self._fault_reason = "stall"
            text = "stall (no flush completed in %s s)" % (DURABLE_TIMEOUT_S,)
        else:
            stage, e = cause
            self._fault_reason = "%s:%s" % (stage, _errno_name(e))
            text = "%s %s (%s)" % (stage, _errno_name(e), e.strerror if isinstance(e, OSError) else e)
        _note(logging.ERROR, "levain gw-keys: ledger fault: %s", text)
        self._stop.set()
        for hold in list(self._live.values()):
            try:
                self._strand(hold)
            except Exception:
                pass
        self._fail_line_futs("the ledger is unhealthy")
        self._lines.clear()

    def _fail_line_futs(self, detail: str) -> None:
        """Every hold-less line not durable: its future fails (an ack it carried is never counted) and its payload
        stays out of the outbox (a replay finds it if its bytes reached the file)."""
        futs = list(self._line_futs.values())
        self._line_futs.clear()
        self._pending_outbox.clear()
        for f in futs:
            try:
                _set_unavailable(f, detail)
            except Exception:
                pass

    def _stalled(self) -> bool:
        """A line is in flight and no flush has RETURNED in the writer for DURABLE_TIMEOUT_S."""
        try:
            if not self._lines:
                return False
            with self._progress_lock:
                last = self._flushed_at
            return time.monotonic() - max(self._inflight_since, last) >= DURABLE_TIMEOUT_S
        except Exception:
            return False

    def _closed(self) -> None:
        if self._closed_fut is not None and not self._closed_fut.done():
            self._closed_fut.set_result(None)

    # -- the hold's methods --------------------------------------------------------------------------------------

    async def _hold_durable(self, hold: Hold) -> None:
        self._check_loop()
        st = hold.state
        if st is RESERVED or st is SENT:
            return
        if st is not PENDING:
            raise LedgerUnavailable("the hold is %s" % st.value)
        try:
            await asyncio.wait_for(asyncio.shield(hold.fut), DURABLE_TIMEOUT_S)
            return
        except asyncio.TimeoutError:
            pass
        if hold.state is RESERVED:            # the `_durable` callback and the timeout landed in one loop turn
            return
        if hold.state is PENDING:
            if self._stalled():
                self._fault(None, "stall")
            self._end(hold, "ended")          # RESOLVING(RELEASED); a no-op if the fault stranded it
        raise LedgerUnavailable("the durable wait timed out")

    def _hold_send_gate(self, hold: Hold) -> None:
        self._check_loop()
        if hold.state is not RESERVED or not self._healthy or self._closing:
            raise LedgerUnavailable("the hold cannot be sent (%s)" % hold.state.value)
        hold.gated = True

    def _hold_mark_sent(self, hold: Hold) -> None:
        self._check_loop()
        if hold.state is RESERVED and hold.gated:
            hold.state = SENT
            return
        raise LedgerMisuse("mark_sent with no passing send_gate before it")

    def _hold_release(self, hold: Hold) -> bool:
        self._check_loop()
        st = hold.state
        if st is PENDING or st is RESERVED:
            self._resolve(hold, RELEASED)
            return True
        if st is SENT:
            raise LedgerMisuse("release on a sent hold")
        return False

    @staticmethod
    def _fits(hold: Hold, outcome: str, meta: Any) -> bool:
        """A caller's resolution meta, with the payload `_resolve` will add to it for a ticketed hold."""
        if not _meta_ok(meta) or "lineage_base" in meta or not _lineage_ok(meta):
            return False
        if hold.base is not None and "lineage" not in meta:
            return _meta_ok(dict(meta, lineage=lineage_payload(hold.base, outcome, _AT_SAMPLE)))
        return True

    def _hold_settle(self, hold: Hold, cost: int, meta: dict) -> bool:
        self._check_loop()
        st = hold.state
        if st is SENT:
            if not _is_int(cost) or cost < 0:
                raise LedgerMisuse("a malformed cost")
            if not self._fits(hold, SETTLED, meta):
                raise LedgerMisuse("a malformed meta")
            self._resolve(hold, SETTLED, cost, json.loads(json.dumps(meta)))
            return True
        if st is PENDING or st is RESERVED:
            raise LedgerMisuse("settle before SENT")
        return False

    def _hold_uncertain(self, hold: Hold, reason: str, meta: dict) -> bool:
        self._check_loop()
        st = hold.state
        if st is SENT:
            if type(reason) is not str or not _REASON_RE.fullmatch(reason) or reason in RESERVED_REASONS:
                raise LedgerMisuse("a malformed or reserved reason")
            if not self._fits(hold, UNCERTAIN, meta):
                raise LedgerMisuse("a malformed meta")
            self._resolve(hold, UNCERTAIN, reason, json.loads(json.dumps(meta)))
            return True
        if st is PENDING or st is RESERVED:
            raise LedgerMisuse("uncertain before SENT")
        return False

    # -- the lineage outbox (3c-6a) -------------------------------------------------------------------------------

    def refused(self, session: str, route: str, reason: str, count: int, meta: dict,
                run_id: Optional[str] = None) -> Tuple[Ref, "asyncio.Future"]:
        """Queues one `refused` line (keys writes it at a refusal window's close, H6-10) and returns its triple
        `(ledger_id, epoch, own seq)` and a future that resolves when the line is durable (or fails
        `LedgerUnavailable`). A `meta["lineage"]` payload enters the outbox then."""
        self._check_loop()
        if not self._healthy or self._closing:
            raise LedgerUnavailable("the ledger is %s" % ("closing" if self._healthy else "unhealthy"))
        for v in (session, route):
            if type(v) is not str or not _ID_RE.fullmatch(v):
                raise LedgerMisuse("a malformed session or route")
        if run_id is not None and (type(run_id) is not str or not _ID_RE.fullmatch(run_id)):
            raise LedgerMisuse("a malformed run_id")
        if type(reason) is not str or not _REASON_RE.fullmatch(reason):
            raise LedgerMisuse("a malformed reason")
        if not _is_int(count) or count < 1:
            raise LedgerMisuse("a malformed count")
        if not _meta_ok(meta) or "lineage_base" in meta or not _lineage_ok(meta):
            raise LedgerMisuse("a malformed meta")
        meta_c = json.loads(json.dumps(meta))
        loop = self._loop
        assert loop is not None
        seq = self._take_seq()
        triple = (self.ledger_id, self.epoch, seq)
        data = _encode({"kind": "refused", "seq": seq, "epoch": self.epoch, "session": session, "run_id": run_id,
                        "route": route, "reason": reason, "count": count, "meta": meta_c, "at": _now()})
        fut = loop.create_future()
        fut.add_done_callback(_retrieve)
        outbox = (triple, meta_c["lineage"]) if "lineage" in meta_c else None
        self._submit(seq, data, None, "refused", fut=fut, outbox=outbox)
        return triple, fut

    def unacked(self) -> List[Tuple[Ref, dict]]:
        """Every durable payload line with no durable ack, in ref order: `[(triple, payload)]`. A triple whose ack
        line is queued but not durable is still listed (it is not counted yet)."""
        self._check_read()
        return [(t, self._outbox[t]) for t in sorted(self._outbox, key=lambda t: t[2])]

    @property
    def outbox_size(self) -> int:
        """`len(unacked())`, in O(1): the measure `LINEAGE_OUTBOX_MAX` is checked against (H6-11)."""
        return len(self._outbox)

    @property
    def quarantined(self) -> Dict[Ref, str]:
        """Every durably acked quarantine: `{triple: field}` (H6-8's counter reads its size)."""
        return dict(self._quarantined)

    def ack(self, triples: Any, quarantined: Optional[Dict[Ref, str]] = None) -> "asyncio.Future":
        """Queues one `lineage-acked` line for the triples CONTROL acked (`quarantined`: the ones it quarantined, by
        field) and returns a future that resolves after the line's flush. The triples leave the outbox ONLY in the
        done-callback of the line's own future (R4 §3: keys counts an ack only after its line is fsynced); a failed
        flush counts nothing. A triple that is not a durable payload line is refused `LedgerMisuse` before anything
        is queued (its line would be `ack-orphan` to the next replay). Triples already acked, or in an ack line not
        yet durable, are dropped; with none left, no line is written and the future is already resolved."""
        self._check_loop()
        loop = self._loop
        assert loop is not None
        q = {} if quarantined is None else quarantined
        if type(q) is not dict:
            raise LedgerMisuse("a malformed quarantine map")
        want: List[Ref] = []
        for t in triples:
            if (type(t) is not tuple or len(t) != 3 or not _is_int(t[2]) or type(t[0]) is not str
                    or type(t[1]) is not str or t not in self._known):
                raise LedgerMisuse("an ack of %r, which is no durable payload line" % (t,))
            if t not in want:
                want.append(t)
        for t, field in q.items():
            if t not in want or type(field) is not str or not _FIELD_RE.fullmatch(field):
                raise LedgerMisuse("a malformed quarantine for %r" % (t,))
        new = [t for t in want if t in self._outbox and t not in self._ack_pending]
        outer = loop.create_future()
        outer.add_done_callback(_retrieve)
        if not new:
            outer.set_result(None)
            return outer
        if not self._healthy or self._closing:
            raise LedgerUnavailable("the ledger is %s" % ("closing" if self._healthy else "unhealthy"))
        new.sort(key=lambda t: t[2])
        line: Dict[str, Any] = {"kind": "lineage-acked", "refs": [_triple_obj(t) for t in new], "at": _now()}
        qn = {t: q[t] for t in new if t in q}
        if qn:
            line["quarantined"] = [{"source": _triple_obj(t), "field": qn[t]} for t in new if t in qn]
        seq = self._take_seq()
        line["seq"] = seq
        inner = loop.create_future()
        inner.add_done_callback(functools.partial(self._ack_done, new, qn, outer))
        self._ack_pending.update(new)
        self._submit(seq, _encode(line), None, "lineage-acked", fut=inner)
        return outer

    def _ack_done(self, refs: List[Ref], q: Dict[Ref, str], outer: "asyncio.Future", inner: "asyncio.Future") -> None:
        """The one place an ack is counted: the ack line's own future, resolved by `_durable` after its flush."""
        for t in refs:
            self._ack_pending.discard(t)
        if inner.cancelled() or inner.exception() is not None:
            exc = None if inner.cancelled() else inner.exception()
            if not outer.done():
                outer.set_exception(exc if exc is not None else LedgerUnavailable("the ack was cancelled"))
            return
        for t in refs:
            self._outbox.pop(t, None)
        self._quarantined.update(q)
        if not outer.done():
            outer.set_result(None)

    # -- close ---------------------------------------------------------------------------------------------------

    async def aclose(self, deadline_s: float) -> None:
        """Closing, every live hold resolved by the `aclose` column, the CLOSE sentinel queued behind those lines, and
        a wait of at most `deadline_s` for the writer to drain and close the fd. Never joins the writer, never
        closes the fd itself (an unattached ledger, which has no writer, excepted)."""
        if self._loop is None:
            self._closing = True
            if self._fd >= 0:
                fd, self._fd = self._fd, -1
                _os_close(fd)
            return
        self._check_loop()
        if self._closed_fut is None:
            self._closing = True
            for hold in list(self._live.values()):
                try:
                    self._end(hold, "closed")
                except Exception as e:
                    _note(logging.WARNING, "levain gw-keys: ledger close step failed for seq %d (%s)", hold.seq,
                          type(e).__name__)
                    try:
                        self._strand(hold)
                    except Exception:
                        pass
            self._closed_fut = self._loop.create_future()
            self._q.put(_CLOSE)
        try:
            await asyncio.wait_for(asyncio.shield(self._closed_fut), deadline_s)
        except asyncio.TimeoutError:
            self._stop.set()
            self._fail_line_futs("the ledger closed before the line was durable")
