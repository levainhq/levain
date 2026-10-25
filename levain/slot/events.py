"""CONTROL's session-event journal (S1 3c-8; CONTRACT §3.6 ``levain-session-event/1``, §2.1 ``L/sessions/`` row).

CONTROL appends an event here BEFORE it pushes it to any ``SESSION-EVENTS`` stream: ``L/sessions/events.jsonl``, one
canonical JSON object per line, ``seq`` assigned here (increasing per journal, from 1), each append written and fsynced
before :meth:`EventJournal.append` returns. The journal's identity is ``L/sessions/events.meta`` = canonical
``{journal_id}`` (a random 32-hex id made when the meta is created, written once: temp + fsync + rename + fsync of the
directory); a stream's ``hello`` carries it, so a client holding a cursor from another journal (restored, recreated)
sees the id change. Both files are CONTROL's, 0600.

The three kinds this module keeps (§3.6's tagged union; ``seq`` and ``at`` on every one):

- ``state``: ``{seq, at, kind, session, ticket, generation, state, verifier_report_digest?, verifier_report_refused?}``
- ``artifact``: ``{seq, at, kind, session, ticket, generation, artifact_digest | artifact_failed}``
- ``pool``: ``{seq, at, kind, egress_free, none_free}``

and the two a stream sends but never stores: ``{kind:"hello", journal_id, oldest_seq, newest_seq}`` (CONTROL's answer
to ``SESSION-EVENTS`` carries it) and ``{kind:"gap", oldest_seq}``, sent first when the cursor is older than the oldest
kept event and an event past the cursor was pruned. ``oldest_seq`` / ``newest_seq`` are 0 while nothing is kept.

**Start** (:meth:`EventJournal.start`): the meta is read (absent: made; one that does not parse refuses
``events-corrupt``); the journal is opened ``O_RDWR | O_APPEND | O_CREAT | O_NOFOLLOW`` and read whole. A last line with
no newline is a torn append that was never acknowledged: cut off (``ftruncate``, fsync) before anything is appended.
Any other line that does not parse, is not its own canonical encoding, is not one of the three kinds, or does not
raise ``seq`` refuses the start ``events-corrupt``: CONTROL never serves a journal it cannot read whole.

**Append**: a failed write or fsync POISONS the journal, as ``tickets.py`` and ``lineage.py`` do (the line may be on
disk, whole or torn, while memory never took it): the appender is closed and every later append refuses
``events-unavailable`` until CONTROL restarts; the restart reads a whole line as written and cuts a torn one. Readers
of what memory holds (a stream, :meth:`last_generation`) keep working.

**Prune** (:meth:`EventJournal.prune`, at CONTROL's start and every :data:`PRUNE_EVERY` appends, where a failure is
handed to :attr:`EventJournal.on_error` and the append that ran it still returns its ``seq``): the ``state`` /
``artifact`` events of every session whose newest ``state`` event names ``PURGED``, and the ``pool`` events older than
:data:`POOL_KEEP_S`. The newest event is always kept, so ``seq`` carries on across a restart. The kept lines are
written to ``events.jsonl.new``, fsynced, renamed over the journal, the directory fsynced, and the appender reopened on
the new file; any failure before the rename leaves the journal as it was.

Runs on the root interpreter (3.9)."""
from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from levain.contracts import ContractError, canon
from levain.contracts.grammar import JSON_INT_MAX
from levain.slot import Refused
from levain.slot import records as R

JOURNAL = "events.jsonl"
META = "events.meta"
FORMAT = "levain-session-event/1"
#: CONTRACT §2.1 ``L/sessions/`` row: both files ``_levain_control`` 0600.
MODE = 0o600
#: §3.6: ``pool`` events older than one day are pruned.
POOL_KEEP_S = 24 * 3600
#: DECISION: a long-running CONTROL prunes after this many appends (its start prunes too).
PRUNE_EVERY = 1024

KINDS = ("state", "artifact", "pool")
_JOURNAL_ID_RE = re.compile(r"[0-9a-f]{32}")

#: The keys each stored kind holds: (required, optional) beside ``seq``, ``at``, ``kind``.
_SESSION_REQUIRED = frozenset({"session", "ticket", "generation"})
_SHAPES: Mapping[str, Tuple[frozenset, frozenset]] = {
    "state": (_SESSION_REQUIRED | {"state"}, frozenset({"verifier_report_digest", "verifier_report_refused"})),
    "artifact": (_SESSION_REQUIRED, frozenset({"artifact_digest", "artifact_failed"})),
    "pool": (frozenset({"egress_free", "none_free"}), frozenset()),
}


def _corrupt(detail: str) -> Refused:
    return Refused("events-corrupt", detail)


def check_event(obj: object) -> Optional[str]:
    """Why ``obj`` is not a stored event (a short reason), or None. ``seq`` and ``at`` are checked here; each value's
    type and the record grammar's patterns for the fields a state record holds."""
    if type(obj) is not dict:
        return "not an object"
    kind = obj.get("kind")
    if kind not in _SHAPES:
        return "kind"
    required, optional = _SHAPES[kind]
    keys = set(obj) - {"seq", "at", "kind"}
    if not required <= keys or not keys <= required | optional or "seq" not in obj or "at" not in obj:
        return "keys"
    if type(obj["seq"]) is not int or not 1 <= obj["seq"] <= JSON_INT_MAX:
        return "seq"
    try:
        R.check_timestamp(obj["at"], "at")
        if kind == "pool":
            for k in ("egress_free", "none_free"):
                # DECISION: POOL's count is unclamped (tickets.pool_free), so a negative count is a valid value
                if type(obj[k]) is not int or abs(obj[k]) > JSON_INT_MAX:
                    return k
            return None
        R.check_session(obj["session"], "session")
        if type(obj["ticket"]) is not str or not obj["ticket"]:
            return "ticket"
        if type(obj["generation"]) is not int or not 0 <= obj["generation"] <= JSON_INT_MAX:
            return "generation"
        if kind == "state":
            R._enum(obj["state"], "state", R.STATES)
        elif len(keys & {"artifact_digest", "artifact_failed"}) != 1:
            return "artifact"
        for k in sorted(keys & set(R.FIELD_CHECKS)):
            R.FIELD_CHECKS[k](obj[k])
        R.check_exclusive({k: obj[k] for k in keys})
    except Refused as e:
        return e.detail or e.name
    return None


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _fsync_dir(path: Path, fsync: Callable[[int], None]) -> None:
    dfd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        fsync(dfd)
    finally:
        os.close(dfd)


class _Kept:
    """One line memory holds: its seq, its bytes (no newline: a stream frame's payload), and what pruning reads."""

    __slots__ = ("seq", "data", "kind", "session", "generation", "state", "at")

    def __init__(self, obj: Mapping[str, object], data: bytes) -> None:
        self.seq = int(obj["seq"])                                    # type: ignore[arg-type]
        self.data = data
        self.kind = str(obj["kind"])
        self.session = obj.get("session")
        self.generation = obj.get("generation")
        self.state = obj.get("state")
        self.at = R.epoch_of(str(obj["at"]))


class EventJournal:
    """``events.jsonl`` + ``events.meta`` in ``directory`` (CONTROL's ``L/sessions/``) and the kept events in memory.
    :meth:`start` opens and reads; nothing is appended before it. Every public method takes the journal's own lock;
    :attr:`cond` (on that lock) is notified on every append, prune and close (a stream waits on it). ``clock`` is
    wall time (``at``, the pool age); ``fsync`` the durability flush; ``fault(point)`` a test's crash seam
    (``before_write``, ``before_fsync``, ``after_fsync``)."""

    def __init__(self, directory: Path, *, clock: Callable[[], float] = time.time,
                 fsync: Callable[[int], None] = os.fsync,
                 fault: Callable[[str], None] = lambda _p: None) -> None:
        self.dir = Path(directory)
        self.path = self.dir / JOURNAL
        self.meta_path = self.dir / META
        self._clock = clock
        self._fsync = fsync
        self.fault = fault
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self._fd: Optional[int] = None
        self._poisoned = False
        self._closed = False
        self.journal_id = ""
        self._kept: List[_Kept] = []
        self._next_seq = 1
        #: session -> the newest generation a state / artifact event names
        self._last_gen: Dict[str, int] = {}
        self._since_prune = 0
        #: called with a failure the journal records and does not raise (an append's trailing prune)
        self.on_error: Callable[[BaseException], None] = lambda _e: None

    # -- the start -----------------------------------------------------------------------------------------------------

    def start(self) -> None:
        with self.lock:
            self._close()
            self.journal_id = self._meta()
            try:
                fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, MODE)
            except OSError as e:
                raise Refused("events-unavailable", f"the event journal: {e.strerror}") from None
            try:
                if (os.fstat(fd).st_mode & 0o170000) != 0o100000:
                    raise _corrupt("the event journal is not a regular file")
                os.fchmod(fd, MODE)
                kept, keep = self._read(fd)
                if keep != os.fstat(fd).st_size:
                    os.ftruncate(fd, keep)            # the torn last line: never acknowledged, so never kept
                    self._fsync(fd)
                _fsync_dir(self.dir, self._fsync)
            except BaseException:
                os.close(fd)
                raise
            self._fd, self._poisoned, self._closed = fd, False, False
            self._set_kept(kept)
            self._since_prune = 0

    def _meta(self) -> str:
        """The journal id from ``events.meta``, made (written once, durably) when the file is absent."""
        try:
            data = R.read_record(self.meta_path, max_bytes=4096)
        except Refused as e:
            raise _corrupt(f"events.meta: {e.name}") from None
        if data is not None:
            try:
                obj = canon.parse(data)
            except (ContractError, ValueError, RecursionError):
                raise _corrupt("events.meta does not parse") from None
            if (type(obj) is not dict or set(obj) != {"journal_id"} or type(obj["journal_id"]) is not str
                    or _JOURNAL_ID_RE.fullmatch(obj["journal_id"]) is None
                    or canon.canonical_bytes(obj) != data):
                raise _corrupt("events.meta is not a canonical {journal_id}")
            return str(obj["journal_id"])
        jid = os.urandom(16).hex()
        try:
            R.write_durably(self.meta_path, canon.canonical_bytes({"journal_id": jid}), mode=MODE, fsync=self._fsync)
        except OSError as e:
            raise Refused("events-unavailable", f"events.meta: {e.strerror or e}") from None
        return jid

    @staticmethod
    def _read(fd: int) -> Tuple[List[_Kept], int]:
        """(the kept lines, the byte length of the journal's complete lines)."""
        chunks = []
        off = 0
        while True:
            part = os.pread(fd, 1 << 20, off)
            if not part:
                break
            chunks.append(part)
            off += len(part)
        data = b"".join(chunks)
        cut = data.rfind(b"\n") + 1              # 0 when there is no complete line
        kept: List[_Kept] = []
        seq = 0
        for raw in data[:cut].split(b"\n")[:-1]:
            try:
                obj = canon.parse(raw)
            except (ContractError, ValueError, RecursionError):
                raise _corrupt(f"the line after seq {seq} does not parse") from None
            why = check_event(obj)
            if why is not None:
                raise _corrupt(f"the line after seq {seq} is not an event ({why})")
            if canon.canonical_bytes(obj) != raw:
                raise _corrupt(f"the line after seq {seq} is not canonical JSON")
            if obj["seq"] <= seq:
                raise _corrupt(f"seq does not rise after {seq}")
            seq = obj["seq"]
            kept.append(_Kept(obj, raw))
        return kept, cut

    def _set_kept(self, kept: List[_Kept]) -> None:
        """Memory from ``kept`` (caller holds the lock)."""
        self._kept = kept
        if kept:
            self._next_seq = max(self._next_seq, kept[-1].seq + 1)
        self._last_gen = {}
        for k in kept:
            if k.kind != "pool":
                self._last_gen[str(k.session)] = max(self._last_gen.get(str(k.session), -1), int(k.generation))

    # -- the append ----------------------------------------------------------------------------------------------------

    def append(self, event: Mapping[str, object]) -> int:
        """``event`` (without ``seq`` and ``at``) given the next ``seq`` and the clock's ``at``, written and fsynced;
        memory moves and the waiters are woken only after the fsync. Returns its ``seq``. Refuses
        ``events-unavailable`` when the journal is closed or poisoned; a failure here poisons it."""
        with self.lock:
            self._live()
            assert self._fd is not None
            obj = dict(event)
            obj["seq"] = self._next_seq
            obj["at"] = R.timestamp(self._clock())
            why = check_event(obj)
            if why is not None:
                raise Refused("bad-value", f"event: {why}")
            data = canon.canonical_bytes(obj)
            try:
                self.fault("before_write")
                _write_all(self._fd, data + b"\n")
                self.fault("before_fsync")
                self._fsync(self._fd)
                self.fault("after_fsync")
            except BaseException as e:
                self._poisoned = True
                self._close()
                self.cond.notify_all()
                if isinstance(e, Exception):
                    raise Refused("events-unavailable", f"an event append failed ({type(e).__name__})") from None
                raise
            k = _Kept(obj, data)
            self._kept.append(k)
            self._next_seq += 1
            if k.kind != "pool":
                self._last_gen[str(k.session)] = max(self._last_gen.get(str(k.session), -1), int(k.generation))
            self._since_prune += 1
            self.cond.notify_all()
            if self._since_prune >= PRUNE_EVERY:
                try:
                    self._prune_locked()
                except OSError as e:
                    # L3 r1 fix 1: the event is fsynced and kept, so its append stands; the prune's failure goes to
                    # ``on_error`` and the prune runs again PRUNE_EVERY appends later
                    self._note(e)
            return k.seq

    def _note(self, e: BaseException) -> None:
        try:
            self.on_error(e)
        except Exception:   # noqa: BLE001 - a broken recorder never fails an append that landed
            pass

    def _live(self) -> None:
        if self._poisoned:
            raise Refused("events-unavailable", "an event append failed; CONTROL must restart")
        if self._fd is None:
            raise Refused("events-unavailable", "the event journal is not open")

    # -- prune ---------------------------------------------------------------------------------------------------------

    def prune(self) -> int:
        """The module docstring's rule; returns how many events were dropped. A failure is raised (``OSError``) with
        the journal as it was, unless the rename landed: then the appender is reopened on the new file, or the journal
        poisoned when that fails."""
        with self.lock:
            self._live()
            return self._prune_locked()

    def _prune_locked(self) -> int:
        self._since_prune = 0
        newest_state: Dict[str, str] = {}
        for k in self._kept:
            if k.kind == "state":
                newest_state[str(k.session)] = str(k.state)
        purged = {s for s, st in newest_state.items() if st == R.PURGED}
        floor = self._clock() - POOL_KEEP_S
        last = self._kept[-1] if self._kept else None
        keep = [k for k in self._kept
                if k is last or not ((k.kind == "pool" and k.at < floor) or (k.kind != "pool" and k.session in purged))]
        dropped = len(self._kept) - len(keep)
        if not dropped:
            return 0
        new = self.path.with_name(JOURNAL + ".new")
        try:
            new.unlink()
        except FileNotFoundError:
            pass
        fd = os.open(new, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, MODE)
        try:
            os.fchmod(fd, MODE)
            _write_all(fd, b"".join(k.data + b"\n" for k in keep))
            self._fsync(fd)
        except BaseException:
            os.close(fd)
            new.unlink(missing_ok=True)
            raise
        os.close(fd)
        try:
            os.replace(new, self.path)
        except BaseException:
            new.unlink(missing_ok=True)
            raise
        try:
            _fsync_dir(self.dir, self._fsync)
            fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC)
        except BaseException:
            self._poisoned = True                 # the old appender writes to an unlinked inode
            self._close()
            self.cond.notify_all()
            raise
        self._close()
        self._fd = fd
        self._set_kept(keep)
        self.cond.notify_all()
        return dropped

    # -- reads ---------------------------------------------------------------------------------------------------------

    def hello(self) -> Dict[str, object]:
        with self.lock:
            return self._hello()

    def _hello(self) -> Dict[str, object]:
        return {"kind": "hello", "journal_id": self.journal_id,
                "oldest_seq": self._kept[0].seq if self._kept else 0,
                "newest_seq": self._kept[-1].seq if self._kept else 0}

    def open_stream(self, after_seq: int) -> Tuple[Dict[str, object], Optional[Dict[str, object]], int]:
        """``SESSION-EVENTS {after_seq}``'s opening, read at one instant: (the hello, the gap or None, the cursor the
        stream continues from). The gap is due when an event with ``seq > after_seq`` was pruned: the cursor is older
        than the oldest kept event and the journal's first seq (1) is past it."""
        with self.lock:
            self._open_or_raise()
            hello = self._hello()
            oldest = int(hello["oldest_seq"])                          # type: ignore[arg-type]
            gap = None
            if oldest and after_seq < oldest - 1:
                gap = {"kind": "gap", "oldest_seq": oldest}
            return hello, gap, after_seq

    def _open_or_raise(self) -> None:
        if self._closed or (self._fd is None and not self._poisoned):
            raise Refused("events-unavailable", "the event journal is not open")

    def after(self, seq: int, *, wait_s: float = 0.0) -> Optional[List[Tuple[int, bytes]]]:
        """The kept events with ``seq`` greater than ``seq``, oldest first, waiting up to ``wait_s`` for one to be
        appended when there is none; None once the journal is closed (CONTROL stopped)."""
        with self.lock:
            deadline = time.monotonic() + wait_s
            while True:
                if self._closed:
                    return None
                out = [(k.seq, k.data) for k in self._kept if k.seq > seq]
                left = deadline - time.monotonic()
                if out or left <= 0:
                    return out
                self.cond.wait(left)

    def last_generation(self, session: str) -> int:
        """The newest generation a kept ``state`` / ``artifact`` event names for ``session``; -1 for none."""
        with self.lock:
            return self._last_gen.get(session, -1)

    def kept(self) -> List[Dict[str, object]]:
        """Every kept event, oldest first (doctor / tests)."""
        with self.lock:
            return [canon.parse(k.data) for k in self._kept]

    @property
    def poisoned(self) -> bool:
        with self.lock:
            return self._poisoned

    @property
    def next_seq(self) -> int:
        with self.lock:
            return self._next_seq

    def close(self) -> None:
        """The appender closed and every stream woken to end (CONTROL's stop)."""
        with self.lock:
            self._close()
            self._closed = True
            self.cond.notify_all()

    def _close(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            try:
                os.close(fd)
            except OSError:
                pass


__all__ = ["EventJournal", "FORMAT", "JOURNAL", "KINDS", "META", "MODE", "POOL_KEEP_S", "PRUNE_EVERY", "check_event"]
