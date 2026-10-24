"""The run-ticket store (S1 3c-5; CONTRACT §3.3 dispatch row, §3.5 Ceiling + IDEMPOTENT MINT, §13; 3c design r3 §2.1).

A run ticket is what `_levain_dispatch` holds between `RUN-TICKET-MINT` and the lease that binds it. CONTROL keeps every
ticket it mints in ONE append-only journal, `L/sessions/tickets.jsonl` (CONTROL only, 0600, one canonical JSON object per
line, fsynced per append), and rebuilds everything it counts from that journal at its start: the open tickets, the UTC
day's mints, and the attempt index `RUN-TICKET-STATUS` answers from.

Lines (`op`): `mint` (the ticket, its `attempt_id`, the `run_id` CONTROL minted with it, the request digest, the facts,
the mint time and UTC day), `bind` (an unbound ticket bound to the session an `ATTACH` created), `unbind` (that
`ATTACH`'s session record was never written, so the ticket is unbound again: L2 4a, 3c-5 fix 1), `cancel`, `expire` (an
unbound ticket past V16), `tombstone` (`{attempt_id, ticket, request_digest, state, session?}`, written only by the
compaction). A tombstone is authoritative over every other line of its attempt. One `attempt_id` naming two
tickets or two request digests, one ticket named by two attempts, or a whole line that does not parse, refuses the start
(`journal-inconsistent`): CONTROL never serves a ticket journal it cannot read whole. A last line with no newline is a
torn append (a crash inside the write) and is dropped; the compaction then rewrites the journal without it.

The compaction runs only at the start, before any append is admitted: the open tickets' mints, the current UTC day's
mints (the day count) and one tombstone per closed attempt, written to `tickets.jsonl.new`, fsynced, renamed over the
journal (one atomic replace) and the directory fsynced. A leftover `.new` (a crash before the rename) is removed at the
start, never adopted.

A failed append POISONS the store (3c-5 fix 1, L1 W1 + L2 2a-2c): ANY exception while a line is written or fsynced
leaves the journal ambiguous (the line may be on disk, whole or torn) while memory never took it, so the appender is
closed and every ticket verb answers `journal-unavailable` until CONTROL restarts; nothing is appended after the failed
line. The restart then reads that line as the truth (a complete last line: consistent, since nothing follows it) or
drops it (a torn last line, never answered). `bound` and `referenced_generations` keep answering from memory, which
holds only what was journaled.

Idempotent mint: a mint whose `attempt_id` the index already names is compared on its request digest FIRST (any
difference: `attempt-conflict`, whatever the ticket's state); an equal one answers the same ticket while it is unbound
and counts nothing again, else `attempt-closed`.

`run_id` (head ruling H1, 1024+8): CONTROL's own opaque id for the run (`r-` + 16 lower-case hex), the ledger scope
`("run", run_id)` and `TOKEN-RESOLVE`'s `run.run_id`; a replay of the same attempt answers the same one. The ticket is a
bearer handle for `LEASE-OPEN` and never leaves CONTROL past dispatch: the run is named by `run_id` everywhere else.

ONE CONTROL (3c-5 fix 2, L3 r1 codex 1): the store holds an exclusive `flock` on `tickets.jsonl.lock` beside the
journal from :meth:`TicketStore.acquire` (CONTROL's first start act) until :meth:`TicketStore.close`; a second CONTROL
that cannot take it refuses to start `control-running` before it compacts, appends or binds its socket. The lock is on
its own file, never the journal's fd (the compaction replaces the journal's inode) and the file is never removed.

Every mint line is checked whole at the rebuild (fix 2, codex 2): exactly its fields, its facts re-checked by
:func:`check_facts` and equal to what that returns, and :func:`request_digest` of them equal to the stored digest; any
difference is `journal-inconsistent`. A line whose facts were altered under its old digest is never served.

Expiry is derived (fix 2, complement 3): an unbound ticket past V16 READS as `expired` on every verb, but the `expire`
line is journaled only by a verb that writes anyway (mint, bind, cancel) or by the start's compaction, so a read-only
verb (STATUS, a replay, LEASE-OPEN's check) never appends and so can never poison the store.

The reader refuses any op it does not hold (a slot releasing a ticket is journaled by the code that builds it, 3c-7,
with the reader that holds it).

Runs on the root interpreter (3.9)."""
from __future__ import annotations

import fcntl
import hashlib
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional

from levain.contracts import ContractError, canon
from levain.contracts.grammar import JSON_INT_MAX
from levain.slot import Refused
from levain.slot.grammar import SESSION_RE

JOURNAL = "tickets.jsonl"
CEILING_FORMAT = "levain-dispatch-ceiling/1"
CEILING_KEYS = frozenset({"format", "max_reserved_microdollars_per_run", "max_open_tickets", "max_runs_per_day"})
#: CONTRACT V16: an unbound ticket's lifetime.
TICKET_UNUSED_S = 15 * 60
#: DECISION: the largest journal CONTROL reads at its start (the compaction keeps it to the open tickets, one day's
#: mints and one small tombstone per lifetime attempt); past it the start is refused, never half-read.
JOURNAL_MAX = 64 * 1024 * 1024
CEILING_MAX = 4096

KINDS = ("executor", "planner", "verifier")
#: the attempt states `RUN-TICKET-STATUS` reports.
UNBOUND, BOUND, RELEASED, CANCELLED, EXPIRED = "unbound", "bound", "released", "cancelled", "expired"
CLOSED = frozenset({RELEASED, CANCELLED, EXPIRED})
OPEN = frozenset({UNBOUND, BOUND})

ATTEMPT_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")      # CONTRACT §3.5, applied with fullmatch only
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_ROUTE = re.compile(r"[a-z0-9-]{1,48}")
#: DECISION: a job id and a model name take the text form CONTROL already uses for names it records.
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
#: head ruling H1: a run id is `r-` + 16 lower-case hex digits, CONTROL's to mint.
RUN_ID_RE = re.compile(r"r-[0-9a-f]{16}")
#: the session kind and class a ticket kind renders as (CONTRACT §3.5 table, "renders as").
RENDERS_AS = {"executor": ("dispatch", "egress"), "planner": ("planner", "egress"), "verifier": ("verifier", "none")}


def new_run_id() -> str:
    return "r-" + secrets.token_hex(8)


#: DECISION: the most egress route ids a ticket may name (a base map's egress allow-list is short).
EGRESS_ROUTES_MAX = 64
#: CONTRACT §3.5 SIZES: `verifier_steps` is at most 32 KiB canonical.
VERIFIER_STEPS_MAX = 32 * 1024

_ROUTE_FIELDS = ("route", "routemap_generation", "route_policy_digest")
_VERIFIER_FIELDS = ("verifier_generation", "verifier_id", "verifier_steps", "input_bundle_digest",
                    "driver_input_digest")
#: §3.3 RUN-TICKET-MINT's fields beside `attempt_id`.
MINT_FIELDS = frozenset({"job_id", "run_index", "contract_digest", "kind", "model_requested", "egress_routes",
                         "reserved_microdollars", *_ROUTE_FIELDS, *_VERIFIER_FIELDS})
#: a journal `mint` line's keys, exactly (fix 2: the rebuild refuses any other set)
MINT_LINE_KEYS = frozenset({"op", "ticket", "attempt_id", "run_id", "request_digest", "facts", "minted_ms", "day"})


def _field(name: str, detail: str = "") -> Refused:
    return Refused("ticket-field:" + name, detail)


def check_attempt_id(value: object) -> str:
    if type(value) is not str or ATTEMPT_RE.fullmatch(value) is None:
        raise _field("attempt_id", "outside the attempt-id grammar")
    return value


def _uint(v: object, name: str, *, low: int = 0) -> int:
    if type(v) is not int or not low <= v <= JSON_INT_MAX:
        raise _field(name)
    return v


def _text(v: object, name: str, pattern: "re.Pattern[str]") -> str:
    if type(v) is not str or pattern.fullmatch(v) is None:
        raise _field(name)
    return v


def check_facts(args: Mapping[str, object]) -> Dict[str, object]:
    """The §3.5 mint-field table's SHAPE half, per kind: every field present that the kind REQUIRES, none it FORBIDS,
    each of its type. The VALUE half (the active generation, the route's models, the base map's egress ids, the
    ceiling) needs CONTROL's own records and is CONTROL's. Returns the facts as a plain dict (no `attempt_id`)."""
    kind = args.get("kind")
    if kind not in KINDS:
        raise _field("kind")
    facts: Dict[str, object] = {"kind": kind}
    facts["job_id"] = _text(args.get("job_id"), "job_id", _NAME)
    facts["run_index"] = _uint(args.get("run_index"), "run_index")
    facts["contract_digest"] = _text(args.get("contract_digest"), "contract_digest", _DIGEST)
    egress = args.get("egress_routes")
    if type(egress) is not list or len(egress) > EGRESS_ROUTES_MAX or len(set(map(str, egress))) != len(egress):
        raise _field("egress_routes")
    facts["egress_routes"] = [_text(r, "egress_routes", _ROUTE) for r in egress]
    facts["reserved_microdollars"] = _uint(args.get("reserved_microdollars"), "reserved_microdollars")
    present = {k for k in (*_ROUTE_FIELDS, "model_requested", *_VERIFIER_FIELDS) if k in args}
    if kind == "verifier":
        for k in (*_ROUTE_FIELDS, "model_requested"):
            if k in present:
                raise _field(k, "forbidden for a verifier ticket")
        if facts["egress_routes"]:
            raise _field("egress_routes", "a verifier ticket names none")
        if facts["reserved_microdollars"] != 0:
            raise _field("reserved_microdollars", "a verifier ticket reserves 0")
        for k in _VERIFIER_FIELDS:
            if k not in present:
                raise _field(k, "required for a verifier ticket")
        facts["verifier_generation"] = _uint(args["verifier_generation"], "verifier_generation", low=1)
        facts["verifier_id"] = _text(args["verifier_id"], "verifier_id", re.compile(r"[a-z0-9-]{1,48}"))
        steps = args["verifier_steps"]
        try:
            if type(steps) is not list or len(canon.canonical_bytes(steps)) > VERIFIER_STEPS_MAX:
                raise _field("verifier_steps")
        except (ContractError, TypeError, ValueError, RecursionError):
            raise _field("verifier_steps") from None
        facts["verifier_steps"] = steps
        for k in ("input_bundle_digest", "driver_input_digest"):
            facts[k] = _text(args[k], k, _DIGEST)
        return facts
    for k in _VERIFIER_FIELDS:
        if k in present:
            raise _field(k, f"forbidden for an {kind} ticket")
    for k in (*_ROUTE_FIELDS, "model_requested"):
        if k not in present:
            raise _field(k, f"required for an {kind} ticket")
    facts["route"] = _text(args["route"], "route", _ROUTE)
    facts["routemap_generation"] = _uint(args["routemap_generation"], "routemap_generation", low=1)
    facts["route_policy_digest"] = _text(args["route_policy_digest"], "route_policy_digest", _DIGEST)
    facts["model_requested"] = _text(args["model_requested"], "model_requested", _NAME)
    if facts["reserved_microdollars"] <= 0:
        raise _field("reserved_microdollars", "must be > 0")
    return facts


def request_digest(facts: Mapping[str, object]) -> str:
    """The digest a replay is compared on: every mint field but `attempt_id`, as canonical JSON."""
    return "sha256:" + hashlib.sha256(canon.canonical_bytes(dict(facts))).hexdigest()


@dataclass(frozen=True)
class Ceiling:
    max_reserved_microdollars_per_run: int
    max_open_tickets: int
    max_runs_per_day: int


def read_ceiling(path: Path, owner: int = 0) -> Ceiling:
    """`L/policy/dispatch_ceiling` (`levain-dispatch-ceiling/1`), read on every mint so a `set-ceiling` holds from the
    next one. DECISION: an absent, unreadable or malformed ceiling refuses every mint `ticket-ceiling:file` (the
    ceiling is the operator's bound on dispatch; no default stands in for it). So does one not owned by `owner` (root;
    a test passes its own uid) or writable by group or other (L1 note 8): a ceiling the dispatch uid could write is no
    bound on it."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise Refused("ticket-ceiling:file", "no dispatch ceiling") from None
    try:
        st = os.fstat(fd)
        if not (st.st_mode & 0o170000) == 0o100000:
            raise Refused("ticket-ceiling:file", "the ceiling is not a regular file")
        if st.st_uid != owner:
            raise Refused("ticket-ceiling:file", f"the ceiling's owner is {st.st_uid}, not {owner}")
        if st.st_mode & 0o022:
            raise Refused("ticket-ceiling:file", "the ceiling is writable by group or other")
        data = os.read(fd, CEILING_MAX + 1)
    finally:
        os.close(fd)
    try:
        obj = canon.parse(data)
    except (ContractError, ValueError, RecursionError):
        raise Refused("ticket-ceiling:file", "the ceiling does not parse") from None
    if not isinstance(obj, dict) or set(obj) != CEILING_KEYS or obj["format"] != CEILING_FORMAT:
        raise Refused("ticket-ceiling:file", f"the ceiling is exactly {sorted(CEILING_KEYS)}")
    vals = [obj[k] for k in ("max_reserved_microdollars_per_run", "max_open_tickets", "max_runs_per_day")]
    if any(type(v) is not int or not 0 <= v <= JSON_INT_MAX for v in vals):
        raise Refused("ticket-ceiling:file", "a ceiling value is not a non-negative integer")
    return Ceiling(*vals)


@dataclass
class Ticket:
    ticket: str
    attempt_id: str
    request_digest: str
    facts: Dict[str, object]
    minted_ms: int
    day: str
    state: str = UNBOUND
    session: Optional[str] = None
    run_id: str = ""


def _inconsistent(detail: str) -> Refused:
    return Refused("journal-inconsistent", detail)


def _day(epoch: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(int(epoch)))


def _nothing(_point: str) -> None:
    return None


class TicketStore:
    """The journal at `path` and what CONTROL rebuilds from it. `start()` (CONTROL's start pass) removes a leftover
    compaction file, reads the journal whole, compacts it and opens the appender; nothing is appended before it.
    Every public method takes the store's own lock. `clock` is wall time (the mint time and the UTC day are
    persisted); `fault(point)` is the compaction crash test's seam (`before_rename`); `ceiling_uid` the ceiling file's
    required owner (root)."""

    def __init__(self, path: Path, ceiling_path: Path, *, fsync: Callable[[int], None] = os.fsync,
                 clock: Callable[[], float] = time.time, fault: Callable[[str], None] = _nothing,
                 ceiling_uid: int = 0) -> None:
        self.path = Path(path)
        self.new_path = self.path.with_name(self.path.name + ".new")
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.ceiling_path = Path(ceiling_path)
        self.ceiling_uid = ceiling_uid
        self._fsync = fsync
        self._clock = clock
        self.fault = fault
        self.lock = threading.Lock()
        self._fd: Optional[int] = None
        #: the held `flock` on :attr:`lock_path` (fix 2): one CONTROL per journal, from :meth:`acquire` to :meth:`close`
        self._lock_fd: Optional[int] = None
        #: set by a failed append (fix 1): every verb refuses `journal-unavailable` until :meth:`start` runs again.
        self._poisoned = False
        self._by_attempt: Dict[str, Ticket] = {}
        self._by_ticket: Dict[str, Ticket] = {}

    # -- the start: lock, read, check, compact -----------------------------------------------------------------------

    def acquire(self) -> None:
        """Take the journal's single-CONTROL lock (module docstring, ONE CONTROL), held until :meth:`close`; idempotent
        while held. CONTROL calls it as its first start act, before any route, record or journal is touched, and
        :meth:`start` takes it too. Another holder: `control-running` (a second CONTROL; it must not start, compact or
        bind the socket). A lock file it cannot open: `journal-unavailable`."""
        with self.lock:
            self._acquire()

    def _acquire(self) -> None:
        if self._lock_fd is not None:
            return
        try:
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError as e:
            raise Refused("journal-unavailable", f"the ticket-journal lock: {e.strerror}") from None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise Refused("control-running", f"another CONTROL holds {self.lock_path}") from None
        except OSError as e:
            os.close(fd)
            raise Refused("journal-unavailable", f"the ticket-journal lock: {e.strerror}") from None
        self._lock_fd = fd

    def _release(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)               # closing the only descriptor releases the flock
            self._lock_fd = None

    def start(self, has_session: Optional[Callable[[str], bool]] = None) -> None:
        """`has_session(session)`: whether CONTROL's state store holds that session's record (CONTROL loads its records
        first). Head ruling H8 (1024+8): a ticket journaled BOUND to a session with no record (CONTROL died between
        the bind line and the record's write) is unbound back to minted here, before the compaction, which then writes
        it without its bind: no session existed, so nothing ran and nothing was charged. With no `has_session`, every
        binding stands. An unbound ticket already past V16 is expired here, so the compaction tombstones it (fix 2).

        The single-CONTROL lock is taken first (:meth:`acquire`); a start that fails after taking it releases it (a
        CONTROL that did not start holds nothing)."""
        with self.lock:
            held = self._lock_fd is not None
            self._acquire()
            try:
                if self._fd is not None:
                    os.close(self._fd)
                    self._fd = None
                try:
                    os.unlink(self.new_path)          # a compaction that died before its rename: never adopted
                except FileNotFoundError:
                    pass
                self._by_attempt, self._by_ticket = self._rebuild(self._read())
                if has_session is not None:
                    for t in self._by_ticket.values():
                        if t.state == BOUND and t.session is not None and not has_session(t.session):
                            t.state, t.session = UNBOUND, None
                now_ms = self._now_ms()
                for t in self._by_ticket.values():
                    if self._due(t, now_ms):
                        t.state = EXPIRED             # journaled by the compaction's tombstone
                self._compact()
                self._fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                                   0o600)
                self._poisoned = False
            except BaseException:
                if not held:
                    self._release()
                raise

    def _read(self) -> List[dict]:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except FileNotFoundError:
            return []
        try:
            if not (os.fstat(fd).st_mode & 0o170000) == 0o100000:
                raise _inconsistent("the journal is not a regular file")
            chunks, size = [], 0
            while True:
                part = os.read(fd, 1 << 20)
                if not part:
                    break
                size += len(part)
                if size > JOURNAL_MAX:
                    raise _inconsistent("the journal is past JOURNAL_MAX")
                chunks.append(part)
        finally:
            os.close(fd)
        data = b"".join(chunks)
        lines = data.split(b"\n")
        lines.pop()                                # the bytes after the last newline: empty, or a torn append
        out = []
        for raw in lines:
            try:
                obj = canon.parse(raw)
            except (ContractError, ValueError, RecursionError):
                raise _inconsistent("a journal line does not parse") from None
            if not isinstance(obj, dict) or type(obj.get("op")) is not str:
                raise _inconsistent("a journal line is not an op object")
            out.append(obj)
        return out

    def _rebuild(self, lines: List[dict]) -> tuple:
        by_attempt: Dict[str, Ticket] = {}
        by_ticket: Dict[str, Ticket] = {}
        tombs: List[dict] = []
        runs: set = set()

        def claim(attempt: str, ticket: str, digest: str) -> Optional[Ticket]:
            t = by_attempt.get(attempt)
            if t is not None and (t.ticket != ticket or t.request_digest != digest):
                raise _inconsistent(f"attempt {attempt} names two tickets or two request digests")
            o = by_ticket.get(ticket)
            if o is not None and o.attempt_id != attempt:
                raise _inconsistent("one ticket named by two attempts")
            return t

        try:
            for obj in lines:
                op = obj["op"]
                if op == "mint":
                    # fix 2 (codex 2): the line whole, its facts re-checked and its digest recomputed, so a line whose
                    # facts were altered under the old digest is never served (the mint's checks would be bypassed)
                    facts = obj["facts"]
                    if set(obj) != MINT_LINE_KEYS or not isinstance(facts, dict) or not set(facts) <= MINT_FIELDS:
                        raise _inconsistent("a mint line's fields are not exactly a mint's")
                    if check_facts(facts) != facts:
                        raise _inconsistent("a mint line's facts are not what check_facts reads")
                    if obj["request_digest"] != request_digest(facts):
                        raise _inconsistent("a mint line's request digest is not its facts' digest")
                    t = Ticket(obj["ticket"], check_attempt_id(obj["attempt_id"]), obj["request_digest"],
                               dict(obj["facts"]), _uint(obj["minted_ms"], "minted_ms"), obj["day"],
                               run_id=obj["run_id"])
                    if type(t.ticket) is not str or not _DIGEST.fullmatch(t.request_digest) \
                            or not _DAY.fullmatch(t.day) or type(t.run_id) is not str \
                            or not RUN_ID_RE.fullmatch(t.run_id):
                        raise _inconsistent("a mint line is malformed")
                    old = claim(t.attempt_id, t.ticket, t.request_digest)
                    if old is None:
                        if t.run_id in runs:
                            raise _inconsistent("one run id named by two attempts")
                        runs.add(t.run_id)
                        by_attempt[t.attempt_id] = by_ticket[t.ticket] = t
                    elif old.run_id != t.run_id:
                        raise _inconsistent(f"attempt {t.attempt_id} names two run ids")
                elif op == "bind":
                    t = by_ticket.get(obj["ticket"])
                    session = obj["session"]
                    if t is None or type(session) is not str or not SESSION_RE.fullmatch(session):
                        raise _inconsistent("a bind line names no minted ticket, or no session")
                    if t.state != UNBOUND:
                        # CONTROL binds only an unbound ticket, under the store's lock, journaled before it answers
                        raise _inconsistent(f"a bind line names a ticket already {t.state}")
                    t.state, t.session = BOUND, session
                elif op == "unbind":
                    t = by_ticket.get(obj["ticket"])
                    if t is None or t.state != BOUND or t.session != obj["session"]:
                        raise _inconsistent("an unbind line names no ticket bound to its session")
                    t.state, t.session = UNBOUND, None
                elif op in ("cancel", "expire"):
                    t = by_ticket.get(obj["ticket"])
                    if t is None:
                        raise _inconsistent(f"a {op} line names a ticket no mint names")
                    if t.state == UNBOUND:
                        t.state = CANCELLED if op == "cancel" else EXPIRED
                elif op == "tombstone":
                    tombs.append(obj)
                else:
                    raise _inconsistent(f"an op the journal does not hold: {op[:32]}")
            for obj in tombs:                     # a tombstone is authoritative over every line of its attempt
                attempt = check_attempt_id(obj["attempt_id"])
                state, session = obj["state"], obj.get("session")
                if state not in CLOSED or not _DIGEST.fullmatch(obj["request_digest"]) \
                        or type(obj["ticket"]) is not str:
                    raise _inconsistent("a tombstone is malformed")
                t = claim(attempt, obj["ticket"], obj["request_digest"])
                if t is None:
                    t = Ticket(obj["ticket"], attempt, obj["request_digest"], {}, 0, "")
                    by_attempt[attempt] = by_ticket[t.ticket] = t
                t.state, t.session = state, session
        except (KeyError, TypeError):
            raise _inconsistent("a journal line lacks a field") from None
        except Refused as e:
            if e.name == "journal-inconsistent":
                raise
            raise _inconsistent(f"a journal line is malformed ({e.name})") from None
        return by_attempt, by_ticket

    def _compacted(self) -> bytes:
        today = _day(self._clock())
        out: List[bytes] = []
        for attempt in sorted(self._by_attempt):
            t = self._by_attempt[attempt]
            if t.state in CLOSED:
                tomb: Dict[str, object] = {"op": "tombstone", "attempt_id": attempt, "ticket": t.ticket,
                                           "request_digest": t.request_digest, "state": t.state}
                if t.session is not None:
                    tomb["session"] = t.session
                out.append(canon.canonical_bytes(tomb))
            if t.state in OPEN or (t.day == today and t.facts):
                out.append(canon.canonical_bytes(self._mint_line(t)))
            if t.state == BOUND:
                out.append(canon.canonical_bytes(self._bind_line(t)))
        return b"".join(line + b"\n" for line in out)

    def _compact(self) -> None:
        data = self._compacted()
        fd = os.open(self.new_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            _write_all(fd, data)
            self._fsync(fd)
        finally:
            os.close(fd)
        self.fault("before_rename")
        os.replace(self.new_path, self.path)
        dfd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            self._fsync(dfd)
        finally:
            os.close(dfd)

    # -- appends ----------------------------------------------------------------------------------------------------

    @staticmethod
    def _mint_line(t: Ticket) -> Dict[str, object]:
        return {"op": "mint", "ticket": t.ticket, "attempt_id": t.attempt_id, "run_id": t.run_id,
                "request_digest": t.request_digest, "facts": t.facts, "minted_ms": t.minted_ms, "day": t.day}

    @staticmethod
    def _bind_line(t: Ticket) -> Dict[str, object]:
        return {"op": "bind", "ticket": t.ticket, "session": t.session}

    def _live(self) -> None:
        """Every verb's first check (caller holds the lock): the journal is open and not poisoned."""
        if self._poisoned:
            raise Refused("journal-unavailable", "a ticket-journal append failed; CONTROL must restart")
        if self._fd is None:
            raise Refused("journal-unavailable", "the ticket journal is not open (CONTROL's start has not run)")

    def _append(self, obj: Mapping[str, object]) -> None:
        """One line, written and fsynced (caller holds the lock and updates memory only after this returns). ANY
        exception poisons the store (module docstring): the appender is closed, the store refuses every verb
        `journal-unavailable` until :meth:`start`, and the failure answers `journal-unavailable`, never
        `internal-error`."""
        self._live()
        assert self._fd is not None
        line = canon.canonical_bytes(dict(obj)) + b"\n"
        try:
            _write_all(self._fd, line)
            self._fsync(self._fd)
        except BaseException as e:
            self._poisoned = True
            fd, self._fd = self._fd, None
            try:
                os.close(fd)
            except OSError:
                pass
            if isinstance(e, Exception):
                raise Refused("journal-unavailable", f"a ticket-journal append failed ({type(e).__name__})") from None
            raise

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    @staticmethod
    def _due(t: Ticket, now_ms: int) -> bool:
        """An unbound ticket past V16 (whether or not its `expire` line is journaled yet)."""
        return t.state == UNBOUND and now_ms - t.minted_ms >= TICKET_UNUSED_S * 1000

    def _state(self, t: Ticket) -> str:
        """`t`'s state as every verb reads it (fix 2, complement 3): derived from `minted_ms`, so a READ never appends
        (caller holds the lock)."""
        return EXPIRED if self._due(t, self._now_ms()) else t.state

    def _expire_due(self) -> None:
        """Every unbound ticket past V16 becomes `expired`, journaled (caller holds the lock). WRITE paths only (mint,
        bind, cancel): a read-only verb derives the state with :meth:`_state` and appends nothing."""
        now_ms = self._now_ms()
        for t in list(self._by_ticket.values()):
            if self._due(t, now_ms):
                self._append({"op": "expire", "ticket": t.ticket})
                t.state = EXPIRED

    # -- the verbs' halves ------------------------------------------------------------------------------------------

    def replay(self, attempt_id: str, facts: Mapping[str, object]) -> Optional[Ticket]:
        """The idempotent half of a mint: None for an attempt the index does not name; its ticket (and so its
        `run_id`) when the request digest matches and the ticket is still unbound; else `attempt-conflict` (any digest
        difference, checked first) or `attempt-closed`."""
        digest = request_digest(facts)
        with self.lock:
            self._live()
            return self._replay(attempt_id, digest)

    def _replay(self, attempt_id: str, digest: str) -> Optional[Ticket]:
        """:meth:`replay`'s rule (caller holds the lock); appends nothing."""
        t = self._by_attempt.get(attempt_id)
        if t is None:
            return None
        if t.request_digest != digest:
            raise Refused("attempt-conflict")
        state = self._state(t)
        if state != UNBOUND:
            raise Refused("attempt-closed", state)
        return t

    def mint(self, attempt_id: str, facts: Mapping[str, object], new_ticket: Callable[[], str]) -> Ticket:
        """A NEW attempt's mint, journaled before it answers: the ceiling read and checked against the rebuilt counts
        (the open tickets, the UTC day's mints), all under the store's lock, so two mints never both take the last
        place. An attempt the index already names answers by :meth:`replay`'s rule BEFORE the ceiling is read (fix 2,
        codex 3: a same-attempt retry that raced the first mint gets the first ticket, whatever the ceiling now says),
        and again under the lock after it."""
        digest = request_digest(facts)
        with self.lock:
            self._live()                          # a poisoned store answers that before the ceiling is read
            again = self._replay(attempt_id, digest)
            if again is not None:
                return again
        ceiling = read_ceiling(self.ceiling_path, self.ceiling_uid)
        if facts["reserved_microdollars"] > ceiling.max_reserved_microdollars_per_run:   # type: ignore[operator]
            raise Refused("ticket-ceiling:max_reserved_microdollars_per_run")
        with self.lock:
            self._live()
            again = self._replay(attempt_id, digest)
            if again is not None:
                return again
            self._expire_due()
            now = self._clock()
            day = _day(now)
            if sum(1 for t in self._by_ticket.values() if t.state in OPEN) >= ceiling.max_open_tickets:
                raise Refused("ticket-ceiling:max_open_tickets")
            if sum(1 for t in self._by_ticket.values() if t.day == day and t.facts) >= ceiling.max_runs_per_day:
                raise Refused("ticket-ceiling:max_runs_per_day")
            runs = {x.run_id for x in self._by_ticket.values()}
            run_id = new_run_id()
            while run_id in runs:
                run_id = new_run_id()
            t = Ticket(new_ticket(), attempt_id, digest, dict(facts), int(now * 1000), day, run_id=run_id)
            self._append(self._mint_line(t))
            self._by_attempt[attempt_id] = self._by_ticket[t.ticket] = t
            return t

    def holdable(self, ticket: str) -> Ticket:
        """`LEASE-OPEN {ticket}`'s check: a minted ticket that is still UNBOUND (`ticket-unknown` for one never minted,
        `ticket-bound` once an `ATTACH` bound it, `attempt-closed` for a cancelled or expired one). A lease holds it
        in CONTROL's memory only; `bind` is what the journal records."""
        with self.lock:
            self._live()
            return self._unbound(ticket)

    def _unbound(self, ticket: str) -> Ticket:
        t = self._by_ticket.get(ticket)
        if t is None:
            raise Refused("ticket-unknown")
        state = self._state(t)
        if state == BOUND:
            raise Refused("ticket-bound")
        if state != UNBOUND:
            raise Refused("attempt-closed", state)
        return t

    def bind(self, ticket: str, session: str) -> Ticket:
        """`ATTACH` of a ticket-bound lease: the UNBOUND ticket bound to `session`, journaled before it answers (the
        refusals are :meth:`holdable`'s: a ticket cancelled or expired since its `LEASE-OPEN` is not bound)."""
        if type(session) is not str or not SESSION_RE.fullmatch(session):
            raise Refused("bad-token", "session")
        with self.lock:
            self._live()
            self._expire_due()
            t = self._unbound(ticket)
            t.session = session
            try:
                self._append(self._bind_line(t))
            except BaseException:
                t.session = None
                raise
            t.state = BOUND
            return t

    def unbind(self, ticket: str, session: str) -> None:
        """The failure path of the `ATTACH` that bound `ticket` to `session` (L2 4a, 3c-5 fix 1): its session record
        was never written, so the binding is undone, journaled (`unbind`) before memory moves; the ticket is UNBOUND
        and holdable again. A ticket not bound to `session` is left as it is. A failed append poisons the store
        (:meth:`_append`): the restart's H8 pass then unbinds it, since no record exists."""
        with self.lock:
            self._live()
            t = self._by_ticket.get(ticket)
            if t is None or t.state != BOUND or t.session != session:
                return
            self._append({"op": "unbind", "ticket": ticket, "session": session})
            t.state, t.session = UNBOUND, None

    def bound(self, session: str) -> Optional[Ticket]:
        """The ticket BOUND to `session`, or None (the run `TOKEN-RESOLVE`, `MINT` and `QUEUE-RESOLVE` answer from)."""
        with self.lock:
            for t in self._by_ticket.values():
                if t.state == BOUND and t.session == session:
                    return t
            return None

    def referenced_generations(self, sessions: "set[str]") -> "frozenset[int]":
        """The route-map generations still referenced (head ruling H5, RELOAD's `keep`): every OPEN ticket's, and every
        ticket's bound to one of `sessions` (the live ones), whatever its state."""
        with self.lock:
            return frozenset(int(t.facts["routemap_generation"]) for t in self._by_ticket.values()   # type: ignore
                             if "routemap_generation" in t.facts and (t.state in OPEN or t.session in sessions))

    def cancel(self, ticket: str) -> None:
        """An UNBOUND ticket only (`ticket-bound` once a lease bound it; `ticket-unknown` for one never minted;
        DECISION: a ticket already closed answers `attempt-closed`)."""
        with self.lock:
            self._live()
            self._expire_due()
            t = self._by_ticket.get(ticket)
            if t is None:
                raise Refused("ticket-unknown")
            if t.state == BOUND:
                raise Refused("ticket-bound")
            if t.state != UNBOUND:
                raise Refused("attempt-closed", t.state)
            self._append({"op": "cancel", "ticket": ticket})
            t.state = CANCELLED

    def status(self, attempt_id: str) -> Dict[str, object]:
        with self.lock:
            self._live()
            t = self._by_attempt.get(attempt_id)
            if t is None:
                raise Refused("attempt-unknown")
            out: Dict[str, object] = {"ticket": t.ticket, "state": self._state(t)}
            if t.session is not None and t.state in (BOUND, RELEASED):
                out["session"] = t.session
            return out

    def counts(self) -> Dict[str, int]:
        """The rebuilt counts (doctor / tests): open tickets and the current UTC day's mints."""
        with self.lock:
            day = _day(self._clock())
            return {"open": sum(1 for t in self._by_ticket.values() if self._state(t) in OPEN),
                    "today": sum(1 for t in self._by_ticket.values() if t.day == day and t.facts)}

    def close(self) -> None:
        """The appender and the single-CONTROL lock both released (CONTROL's stop)."""
        with self.lock:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            self._release()


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


__all__ = ["BOUND", "CANCELLED", "CEILING_FORMAT", "Ceiling", "EXPIRED", "JOURNAL", "KINDS", "MINT_FIELDS", "RENDERS_AS",
           "RUN_ID_RE", "TICKET_UNUSED_S",
           "Ticket", "TicketStore", "UNBOUND", "check_attempt_id", "check_facts", "read_ceiling", "request_digest"]
