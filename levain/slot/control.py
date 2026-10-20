"""CONTROL: the ``levain-control/1`` wire, the session state store and the lease (CONTROL §1, §3.1-§3.3, §7.2).

CONTROL is the authority boundary of the slot system: it is the sole writer of every state record
(``L/sessions/<session>.json``, §7.2), so it alone decides who may move a session between states. It
cannot read alloc (§2.1, by design), so everything it answers with is what root REGISTERED into the
record's ``claims`` at ``ATTACH`` and ``MINT`` (§3.3, §12 D10).

Three layers, each testable on its own:

- **framing** (:func:`encode_frame`, :func:`read_frame`): a 4-byte big-endian length (≤ 64 KiB) and a
  canonical JSON object ``{"v":1,"op":…,"id":<int>,…}``. A length over the cap is answered with a
  refusal and the connection closed (the stream cannot be resynchronised without reading the payload); a
  frame that is not a canonical request object closes the connection with no answer.
- **the store** (:class:`StateStore`): ``<session>.json`` + ``<session>.lock`` in one directory. The lock is
  only ever TRY-locked (``flock LOCK_NB``): busy = ``refused:"locked"``, which the caller retries (§3.3).
  Every write is :func:`levain.slot.records.write_durably` and bumps ``generation`` by one.
- **CONTROL** (:class:`Control`) over a store, and :class:`ControlServer`, which serves it on a unix
  socket, one thread per connection. The CONNECTION is the lease (§3.1): ``LEASE-OPEN`` answers a nonce
  bound to that connection; its EOF revokes the session ``ATTACH`` bound to it.

Session 2 builds the operator verbs (``LEASE-OPEN``, ``STATUS``) and root's (``ATTACH``, ``CAS``,
``MINT``, ``FRONT-ARM``, ``SECRETS``, ``REVOKE``, ``REVOKED-SET``). Root's ``SLOT-SYNC`` and every
dispatch / keys / qbroker verb (§3.3, §3.5, §3.6) are session 3c's: until then every peer but root and the
operator is refused ``verb-not-allowed-for-peer`` (see :data:`PEER_VERBS`). Every act on keys (``DROP-SESSION``,
``FRONT-REGISTER``, ``FRONT-ARM`` on §4.9's wire) and the reaper wake are injected seams.
"""
from __future__ import annotations

import base64
import collections
import os
import socket
import struct
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterator, Mapping

import fcntl

from levain.broker.stub import peer_uid as kernel_peer_uid
from levain.contracts import ContractError, canon
from levain.slot import Refused
from levain.slot import records as R
from levain.slot.runtime_grammar import HANDLE_BYTES, _handle, _keys

CONTROL_FORMAT = "levain-control/1"
FRAME_MAX = 64 * 1024                      # §3.3: the frame cap
_LEN = struct.Struct(">I")

ROOT_UID = 0

#: §3.3's verbs per peer, as built in session 2. Session 3c adds root's ``SLOT-SYNC`` and the
#: ``_levain_keys`` / ``_levain_qbroker`` / ``_levain_dispatch`` rows (each a peer kind that
#: :meth:`Control.peer_kind` learns from its uid); until then those peers are ``foreign``.
PEER_VERBS: Mapping[str, frozenset[str]] = {
    "operator": frozenset({"LEASE-OPEN", "STATUS"}),
    "root": frozenset({"ATTACH", "CAS", "MINT", "FRONT-ARM", "SECRETS", "REVOKE", "REVOKED-SET"}),
    "foreign": frozenset(),
}

#: Each verb's request keys beside ``v``, ``op``, ``id``: (allowed, required).
_ARGS: Mapping[str, tuple[frozenset[str], frozenset[str]]] = {
    "LEASE-OPEN": (frozenset(), frozenset()),
    "STATUS": (frozenset({"session"}), frozenset()),
    "ATTACH": (frozenset({"lease_nonce", "sudo_uid", "session", "claims"}),) * 2,
    "CAS": (frozenset({"session", "generation", "from", "to", "fields"}),) * 2,
    "MINT": (frozenset({"session", "generation", "claims"}),) * 2,
    "FRONT-ARM": (frozenset({"session", "generation"}),) * 2,
    "SECRETS": (frozenset({"session"}),) * 2,
    "REVOKE": (frozenset({"session"}),) * 2,
    "REVOKED-SET": (frozenset(), frozenset()),
}

#: DECISION: the backoff of the lease-EOF revoke's lock retry (§3.1 step 3 fixes only "with backoff
#: until it is taken, never dropped").
EOF_RETRY_FIRST_S = 0.01
EOF_RETRY_MAX_S = 1.0

#: DECISION: a frame's remaining bytes must arrive within this once its first byte has (a lease connection
#: idles legitimately BETWEEN frames, so only a frame already begun has a deadline).
FRAME_READ_S = 10.0
#: DECISION: the connections CONTROL serves at once (one thread each); one over is closed at accept. A
#: session's lease connection lives as long as the session, and every other caller's is one exchange.
MAX_CONNECTIONS = 256
#: DECISION: dead leases kept to answer ``lease-dead`` (§3.3) once nothing else needs them; past this many the
#: oldest is forgotten and its nonce answers ``nonce-unknown``, which refuses a replay just as surely.
RETIRED_LEASES_MAX = 1024


# --- framing --------------------------------------------------------------------------------------------

class FrameTooLarge(Exception):
    """A frame length over :data:`FRAME_MAX`: answered ``frame-too-large``, then the connection closes."""


class FrameMalformed(Exception):
    """Not a ``levain-control/1`` request: the connection closes with no answer."""


def encode_frame(obj: Mapping[str, object]) -> bytes:
    data = canon.canonical_bytes(dict(obj))
    if len(data) > FRAME_MAX:
        raise FrameTooLarge(len(data))
    return _LEN.pack(len(data)) + data


def _recv_exact(sock: socket.socket, n: int, *, eof_ok: bool) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            if eof_ok and not buf:
                return None
            raise FrameMalformed("EOF inside a frame")
        buf += chunk
    return bytes(buf)


def read_frame(sock: socket.socket, *, frame_timeout: float | None = None) -> dict | None:
    """One frame's object, or ``None`` on a clean EOF between frames. Raises :class:`FrameTooLarge` or
    :class:`FrameMalformed`. ``frame_timeout``: once a frame's first byte is in, the rest must arrive within
    it (else :class:`FrameMalformed`); the wait for that first byte keeps the socket's own timeout."""
    first = _recv_exact(sock, 1, eof_ok=True)
    if first is None:
        return None
    restore = sock.gettimeout()
    if frame_timeout is not None:
        sock.settimeout(frame_timeout)
    try:
        head = first + _recv_exact(sock, _LEN.size - 1, eof_ok=False)
        (n,) = _LEN.unpack(head)
        if n > FRAME_MAX:
            raise FrameTooLarge(n)
        if n == 0:
            raise FrameMalformed("empty frame")
        data = _recv_exact(sock, n, eof_ok=False)
    except socket.timeout:
        raise FrameMalformed("a frame stalled past its read deadline") from None
    finally:
        if frame_timeout is not None:
            sock.settimeout(restore)
    try:
        obj = canon.parse(data)
    except ContractError as e:
        raise FrameMalformed(str(e)) from None
    if type(obj) is not dict or canon.canonical_bytes(obj) != data:
        raise FrameMalformed("not a canonical JSON object")
    return obj


def check_request(obj: dict) -> tuple[int, str]:
    """``{"v":1,"op":<string>,"id":<uint>,…}``, else :class:`FrameMalformed`."""
    if obj.get("v") != 1 or type(obj.get("v")) is not int:
        raise FrameMalformed("v is 1")
    op, rid = obj.get("op"), obj.get("id")
    if type(op) is not str or not op:
        raise FrameMalformed("op is a string")
    if type(rid) is not int or not 0 <= rid <= R.JSON_INT_MAX:
        raise FrameMalformed("id is an integer")
    return rid, op


def ok(rid: int | None, **fields: object) -> dict[str, object]:
    return {"id": rid, "ok": True, **fields}


def refused(rid: int | None, name: str) -> dict[str, object]:
    return {"id": rid, "ok": False, "refused": name}


def new_handle() -> str:
    """Header shared encodings: 32 random bytes, base64url unpadded."""
    return base64.urlsafe_b64encode(os.urandom(HANDLE_BYTES)).rstrip(b"=").decode("ascii")


# --- the store ------------------------------------------------------------------------------------------

class StateStore:
    """``L/sessions/`` (§2.1): ``<session>.json`` + ``<session>.lock``, CONTROL the only writer.

    The records are held in memory as well (loaded by :meth:`load`, updated by :meth:`put`), so
    ``REVOKED-SET`` and ``STATUS`` are served without a disk read (§4.5: "from memory, no fsync"); every
    write goes to disk first and reaches memory only once it is durable.

    A write that failed AFTER its rename (:class:`records.NotDurable`) leaves disk ahead of memory, so the
    session is marked UNSETTLED: no read serves its memory again until :meth:`_settle` has fsynced the
    directory and reloaded the record from disk; until that succeeds every read of it is refused
    ``record-unsettled``. ``on_settle(old, new)`` is told what the reload changed (CONTROL owes a revoke's
    after-acts for a record that a failed write moved past live)."""

    def __init__(self, directory: Path, *, fsync: Callable[[int], None] = os.fsync) -> None:
        self.dir = Path(directory)
        self._fsync = fsync
        self._mem: dict[str, R.SessionState] = {}
        #: session -> the mark of the failed write that unsettled it (a reload publishes only if no later
        #: failed write re-marked the session while it read).
        self._unsettled: dict[str, int] = {}
        self._marks = 0
        self._mu = threading.Lock()
        self.on_settle: Callable[[R.SessionState | None, R.SessionState | None], None] = lambda old, new: None

    def _path(self, session: str, suffix: str) -> Path:
        return self.dir / f"{R.check_session(session)}{suffix}"

    @contextmanager
    def lock(self, session: str) -> Iterator[None]:
        """The session lock, TRY-locked only (§3.3): another holder = ``Refused("locked")``, never a wait.
        The lock file is never removed (unlinking a lock file lets two holders lock two inodes)."""
        path = self._path(session, ".lock")
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError as e:
            raise Refused("bad-root", f"{path}: {e.strerror}") from None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Refused("locked", session) from None
            yield
        finally:
            os.close(fd)   # closing the only descriptor releases the flock

    def load(self) -> dict[str, R.SessionState]:
        """Read every record (CONTROL's start). A record that does not parse is refused loudly: CONTROL
        never serves a store it cannot read whole."""
        out: dict[str, R.SessionState] = {}
        for p in sorted(self.dir.glob("s-*.json")):
            session = p.name[:-len(".json")]
            data = R.read_record(p)
            if data is None:
                continue
            rec = R.parse_state(data)
            if rec.session != session:
                raise Refused("record-malformed", f"{p} names {rec.session}")
            out[session] = rec
        with self._mu:
            self._mem = dict(out)
            self._unsettled.clear()
        return out

    def get(self, session: str) -> R.SessionState | None:
        with self._mu:
            if session not in self._unsettled:
                return self._mem.get(session)
        return self._settle(session)

    def all(self) -> list[R.SessionState]:
        with self._mu:
            unsettled = sorted(self._unsettled)
        for session in unsettled:
            self._settle(session)
        with self._mu:
            return list(self._mem.values())

    def put(self, rec: R.SessionState) -> None:
        """Write ``rec`` durably (the caller holds the session lock), then publish it in memory. A failure
        after the rename marks the session unsettled before it propagates (see the class docstring)."""
        try:
            R.write_durably(self._path(rec.session, ".json"), R.state_bytes(rec), fsync=self._fsync)
        except R.NotDurable:
            with self._mu:
                self._marks += 1
                self._unsettled[rec.session] = self._marks
            raise
        with self._mu:
            self._mem[rec.session] = rec
            self._unsettled.pop(rec.session, None)

    def _settle(self, session: str) -> R.SessionState | None:
        """Make the record at the path durable (fsync the directory) and serve it from disk from here on.
        Any failure leaves the session unsettled and is refused ``record-unsettled``. A reload publishes only
        if the session still carries the mark it started under: a reader that raced a writer's later failed
        write (and so may have read the bytes before it) leaves the session unsettled for the next read."""
        p = self._path(session, ".json")
        with self._mu:
            mark = self._unsettled.get(session)
        try:
            dfd = os.open(self.dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                self._fsync(dfd)
            finally:
                os.close(dfd)
            data = R.read_record(p)
            rec = None if data is None else R.parse_state(data)
        except (OSError, Refused) as e:
            raise Refused("record-unsettled", f"{session}: {e}") from None
        if rec is not None and rec.session != session:
            raise Refused("record-unsettled", f"{p} names {rec.session}")
        with self._mu:
            now = self._unsettled.get(session)
            if now is None:                              # another reader settled it first
                return self._mem.get(session)
            if now != mark:                              # a later failed write: what this read saw may be older
                raise Refused("record-unsettled", f"{session}: written again while it was read")
            old = self._mem.get(session)
            if rec is None:
                self._mem.pop(session, None)
            else:
                self._mem[session] = rec
            del self._unsettled[session]
        self.on_settle(old, rec)
        return rec


def _current(rec: R.SessionState, name: str) -> object:
    """A CAS-writable field's value in ``rec`` as a CAS would send it (absent = ``None``)."""
    if name == "touched":
        return list(rec.touched) or None   # a CAS clears touched with [] or null alike
    return rec.fields.get(name)


# --- CONTROL --------------------------------------------------------------------------------------------

@dataclass(eq=False)
class Lease:
    """One supervisor connection (§3.1). ``used`` once ATTACH has accepted its nonce; ``session`` once
    the record exists."""

    nonce: str
    peer_uid: int
    live: bool = True
    used: bool = False
    session: str | None = None


@dataclass(eq=False)
class Conn:
    """CONTROL's view of one connection: its peer and, once ``LEASE-OPEN`` answered, its lease."""

    peer_uid: int | None
    lease: Lease | None = None


@dataclass
class Handles:
    """§3.2: what ``MINT`` minted for a session, held in CONTROL's memory only (they die at REVOKED, and a
    CONTROL restart revokes every live session, so no handle needs to outlive the process)."""

    token: str | None = None
    queue_cap: str | None = None


def _default_is_brokered(claims: Mapping[str, object]) -> bool:
    # DECISION: §3.2 mints a token "only if the registered claims carry a `brokered` route"; a route's type
    # lives in the route maps (§4.3), which CONTROL loads in session 3c. Until then the session's
    # registered `auth` stands for it: a brokered session's vendor route is its brokered route.
    return claims.get("auth") == "brokered"


def _keys_unavailable(*_a: object) -> None:
    # The keys admin wire (§4.9) is session 3c's. Fail closed until it is wired.
    raise Refused("keys-unavailable", "the keys admin wire is not wired")


def _nothing(*_a: object) -> None:
    return None


class Control:
    """CONTROL's verbs over a :class:`StateStore`.

    Seams (each a callable; production wires session 3c's keys admin client and the reaper wake socket):
    ``drop_session(session)`` (§4.9 ``DROP-SESSION``, acked; the default does nothing, which is true while
    no keys process holds anything) · ``wake_reaper()`` · ``keys_front_register(session, front_uid, pid,
    start, incarnation)`` and ``keys_front_arm(session)`` (default: refused ``keys-unavailable``) ·
    ``slot_reusable(class, index, session) -> bool`` (the §3.5 mirror; default ``False``, which keeps a
    RETAINED session in ``REVOKED-SET``: the revoker's kill is uid + start checked, so over-inclusion is
    safe and under-inclusion is not) · ``is_brokered(claims) -> bool`` · ``clock() -> epoch seconds`` ·
    ``sleep(seconds)`` (the EOF retry's backoff)."""

    def __init__(self, store: StateStore, operator_uid: int, *,
                 drop_session: Callable[[str], object] = _nothing,
                 wake_reaper: Callable[[], object] = _nothing,
                 keys_front_register: Callable[..., object] = _keys_unavailable,
                 keys_front_arm: Callable[[str], object] = _keys_unavailable,
                 slot_reusable: Callable[[str, int, str], bool] = lambda c, i, s: False,
                 is_brokered: Callable[[Mapping[str, object]], bool] = _default_is_brokered,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if type(operator_uid) is not int or operator_uid <= ROOT_UID:
            raise Refused("bad-config", f"operator uid {operator_uid!r}")
        self.store = store
        self.operator_uid = operator_uid
        self._drop_session = drop_session
        self._wake_reaper = wake_reaper
        self._keys_front_register = keys_front_register
        self._keys_front_arm = keys_front_arm
        self._slot_reusable = slot_reusable
        self._is_brokered = is_brokered
        self._clock = clock
        self._sleep = sleep
        self._mu = threading.Lock()
        self._leases: dict[str, Lease] = {}            # nonce -> lease (a dead one kept, bounded: _retire)
        self._session_lease: dict[str, Lease] = {}
        self._handles: dict[str, Handles] = {}
        self._drops_owed: set[str] = set()
        self._retired: collections.deque[str] = collections.deque()   # dead lease nonces, oldest first
        self._stopping = threading.Event()
        #: The background threads still running (revokes, drop retries); each removes itself as it ends.
        self._eof_threads: list[threading.Thread] = []
        store.on_settle = self._on_settle
        #: Exceptions a seam raised after a revoke landed (the revoke itself is durable by then).
        self.seam_errors: list[tuple[str, str, BaseException]] = []

    # -- peers -------------------------------------------------------------------------------------------

    def peer_kind(self, uid: int | None) -> str:
        if uid == ROOT_UID:
            return "root"
        if uid == self.operator_uid:
            return "operator"
        return "foreign"

    # -- start pass (§1 CONTROL row, §1.3) ---------------------------------------------------------------

    def start(self) -> None:
        """CONTROL's start pass: load every record, then treat every record whose lease was live as a
        lease EOF (no lease survives a CONTROL restart): ``lease_live:false``, then live state ->
        REVOKED, then the keys drop + reaper wake. Work is retained by END, which the reaper runs.

        Every record already at or past REVOKED gets its ``DROP-SESSION`` again (idempotent, §4.9): a
        CONTROL that died between the REVOKED write and the drop, or whose drop never got an ack, owes it
        still. Then one reaper wake."""
        self.store.load()
        past = False
        for rec in sorted(self.store.all(), key=lambda r: r.session):
            if rec.lease_live or rec.state in R.LIVE_STATES:
                self._revoke_after_eof(rec.session)
            else:
                self._drop(rec.session)
                past = True
        if past:
            self._wake()

    def stop(self) -> None:
        """Stop serving WITHOUT revoking: the records stay as they are, and the next :meth:`start` revokes
        them (a stop is a restart, as far as any session can tell)."""
        self._stopping.set()

    # -- the write primitive -------------------------------------------------------------------------------

    def _write(self, rec: R.SessionState, **changes: object) -> R.SessionState:
        """``rec`` with ``changes`` at ``generation + 1``, durably (caller holds the session lock)."""
        new = replace(rec, generation=rec.generation + 1, updated_at=R.timestamp(self._clock()), **changes)
        self.store.put(new)
        return new

    def _lease_live(self, session: str) -> bool:
        with self._mu:
            lease = self._session_lease.get(session)
            return lease is not None and lease.live

    def _after_revoke(self, session: str) -> None:
        """§4.5: CONTROL at REVOKED drops the session's handles, sends ``DROP-SESSION`` to keys, wakes the
        reaper. Called after the REVOKED write is durable and the session lock released."""
        with self._mu:
            self._handles.pop(session, None)
        self._drop(session)
        self._wake()
        self._prune(session)

    def _wake(self) -> None:
        try:
            self._wake_reaper()
        except BaseException as e:   # noqa: BLE001 - the reaper also runs on its own pass; record the failure
            self.seam_errors.append(("wake_reaper", "", e))

    def _drop(self, session: str) -> None:
        """``DROP-SESSION`` once now; a failure is never taken as done: it is retried with backoff on a
        background thread until keys acks it (or CONTROL stops, whose next start re-issues it)."""
        with self._mu:
            if session in self._drops_owed:
                return                                   # a retry is already running
        try:
            self._drop_session(session)
            return
        except BaseException as e:   # noqa: BLE001 - the revoke is durable; the drop is owed
            self.seam_errors.append(("drop_session", session, e))
        with self._mu:
            if session in self._drops_owed:
                return
            self._drops_owed.add(session)
        self._spawn(self._drop_until_acked, session, name=f"control-drop-{session}")

    def _drop_until_acked(self, session: str) -> None:
        delay = EOF_RETRY_FIRST_S
        try:
            while not self._stopping.is_set():
                self._sleep(delay)
                delay = min(delay * 2, EOF_RETRY_MAX_S)
                try:
                    self._drop_session(session)
                    return
                except BaseException:   # noqa: BLE001 - noted once by _drop; retried until acked
                    continue
        finally:
            with self._mu:
                self._drops_owed.discard(session)

    def drops_owed(self) -> frozenset[str]:
        """The sessions whose ``DROP-SESSION`` keys has not acked yet (doctor / tests)."""
        with self._mu:
            return frozenset(self._drops_owed)

    def _on_settle(self, old: R.SessionState | None, new: R.SessionState | None) -> None:
        """A record reloaded after a write that failed past its rename (:meth:`StateStore._settle`). If the
        failed write moved it out of live, the revoke's after-acts are owed, since its writer answered an error
        and ran none; if it is live with no live lease bound, it is revoked as a lease EOF would."""
        if new is None:
            return
        if new.state not in R.LIVE_STATES and (old is None or old.state in R.LIVE_STATES):
            with self._mu:
                self._handles.pop(new.session, None)
            self._spawn(self._after_revoke, new.session, name=f"control-settled-{new.session}")
        elif new.state in R.LIVE_STATES and not self._lease_live(new.session):
            self._spawn_revoke(new.session)

    def _prune(self, session: str) -> None:
        """Unbind a dead lease once its session is past live (nothing reads the binding after that) and retire
        it (:meth:`_retire`). A lease still live stays bound: its own EOF prunes it."""
        with self._mu:
            lease = self._session_lease.get(session)
            if lease is None or lease.live:
                return
        rec = self.store.get(session)
        if rec is not None and rec.state in R.LIVE_STATES:
            return
        with self._mu:
            if self._session_lease.get(session) is lease and not lease.live:
                del self._session_lease[session]
                self._retire(lease)

    def _retire(self, lease: Lease) -> None:
        """A dead lease nothing reads any more (caller holds ``_mu``): kept, bounded, so a late ATTACH still reads
        ``lease-dead``; past :data:`RETIRED_LEASES_MAX` the oldest is forgotten (``nonce-unknown``)."""
        self._retired.append(lease.nonce)
        while len(self._retired) > RETIRED_LEASES_MAX:
            old = self._retired.popleft()
            gone = self._leases.get(old)
            if gone is not None and not gone.live:
                del self._leases[old]

    # -- the lease EOF (§3.1 step 3) ------------------------------------------------------------------------

    def lease_eof(self, lease: Lease) -> threading.Thread | None:
        """The lease connection read EOF. The lease is dead at once (every lease-bound verb answers
        ``lease-dead`` from here on) and the session's in-memory handles are dropped at once, BEFORE any durable
        write (L1: a full disk that keeps the revoke retrying must not keep a token resolvable); the session it
        bound, if any, is then revoked on a thread that retries the session lock until it is taken. Returns that
        thread (tests join it)."""
        with self._mu:
            lease.live = False
            session = lease.session
            if session is not None:
                self._handles.pop(session, None)
            elif not lease.used and self._leases.get(lease.nonce) is lease:
                self._retire(lease)                      # bound nothing and never will
        if session is None or self._stopping.is_set():
            return None
        return self._spawn_revoke(session)

    def _spawn_revoke(self, session: str) -> threading.Thread:
        return self._spawn(self._revoke_after_eof, session, name=f"control-eof-{session}")

    def _spawn(self, target: Callable[[str], None], session: str, *, name: str) -> threading.Thread:
        def run() -> None:
            try:
                target(session)
            finally:
                with self._mu:
                    if t in self._eof_threads:
                        self._eof_threads.remove(t)

        t = threading.Thread(target=run, daemon=True, name=name)
        with self._mu:
            self._eof_threads.append(t)
        t.start()
        return t

    def _revoke_after_eof(self, session: str) -> None:
        """``lease_live:false`` first (its own generation), then live state -> REVOKED, retrying the lock
        with backoff until it is taken: never dropped on ``locked``. A failed write (``OSError``) is retried
        the same way, and so is any other refusal (recorded once in :attr:`seam_errors`): a revoke owed is
        never given up. Then the keys drop and the reaper wake."""
        delay = EOF_RETRY_FIRST_S
        noted = False
        while True:
            try:
                revoked = False
                with self.store.lock(session):
                    rec = self.store.get(session)
                    if rec is None:
                        return
                    if rec.lease_live:
                        rec = self._write(rec, lease_live=False, event_kind="none")
                    if rec.state in R.LIVE_STATES:
                        rec = self._write(rec, state=R.REVOKED, event_kind="state")
                        revoked = True
                break
            except (Refused, OSError) as e:
                if not noted and not (isinstance(e, Refused) and e.name == "locked"):
                    self.seam_errors.append(("revoke", session, e))
                    noted = True
                if self._stopping.is_set():
                    return
                self._sleep(delay)
                delay = min(delay * 2, EOF_RETRY_MAX_S)
        if revoked:
            self._after_revoke(session)
        else:
            self._prune(session)

    def join_eof(self, timeout: float | None = None) -> None:
        """Join the background threads, including any started while joining."""
        while True:
            with self._mu:
                threads = [t for t in self._eof_threads if t.is_alive() and t is not threading.current_thread()]
            if not threads:
                return
            for t in threads:
                t.join(timeout)
            if timeout is not None and any(t.is_alive() for t in threads):
                return

    # -- dispatch ----------------------------------------------------------------------------------------

    def handle(self, conn: Conn, req: dict) -> dict[str, object]:
        """Answer one checked request (``check_request`` passed). Never raises for a refusal."""
        rid, op = req["id"], req["op"]
        kind = self.peer_kind(conn.peer_uid)
        if op not in PEER_VERBS[kind]:
            return refused(rid, "verb-not-allowed-for-peer")
        args = {k: v for k, v in req.items() if k not in ("v", "op", "id")}
        try:
            allowed, required = _ARGS[op]
            _keys(args, allowed, required, op)
            fields = getattr(self, "_op_" + op.lower().replace("-", "_"))(conn, args)
        except Refused as e:
            return refused(rid, e.name)
        except Exception as e:   # noqa: BLE001 - DECISION: a seam's own failure answers by name, never kills the connection
            self.seam_errors.append((op, "", e))
            return refused(rid, "internal-error")
        return ok(rid, **fields)

    # -- operator verbs ------------------------------------------------------------------------------------

    def _op_lease_open(self, conn: Conn, args: dict) -> dict[str, object]:
        """§3.1 step 1 (interactive): the connection becomes the lease. A second ``LEASE-OPEN`` on a
        connection whose lease is live is refused (DECISION: ``lease-exists``)."""
        with self._mu:
            if conn.lease is not None and conn.lease.live:
                raise Refused("lease-exists")
            lease = Lease(new_handle(), conn.peer_uid)
            self._leases[lease.nonce] = lease
            conn.lease = lease
        return {"lease_nonce": lease.nonce}

    def _op_status(self, conn: Conn, args: dict) -> dict[str, object]:
        """§3.3: the records whose ``claims.operator_uid`` is the peer. DECISION: the answer is an object,
        so the list rides ``records``."""
        want = R.check_session(args["session"]) if "session" in args else None
        recs = [R.state_record(r) for r in sorted(self.store.all(), key=lambda r: r.session)
                if r.claims.get("operator_uid") == conn.peer_uid and (want is None or r.session == want)]
        return {"records": recs}

    # -- root verbs ----------------------------------------------------------------------------------------

    def _op_attach(self, conn: Conn, args: dict) -> dict[str, object]:
        """§3.1 step 2, §3.3: accepted ONCE, only if the nonce is live on a connection whose peer uid is
        ``sudo_uid``; creates the record at generation 0, ALLOCATED, with the claims."""
        nonce = _handle(args["lease_nonce"], "lease_nonce")
        sudo_uid = R._uint(args["sudo_uid"], "sudo_uid")
        session = R.check_session(args["session"])
        claims = R.parse_attach_claims(args["claims"])
        with self._mu:
            lease = self._leases.get(nonce)
            if lease is None:
                raise Refused("nonce-unknown")
            if not lease.live:
                raise Refused("lease-dead")
            if lease.used:
                raise Refused("nonce-used")
            if lease.peer_uid != sudo_uid:
                raise Refused("nonce-uid-mismatch")
            # §3.3: the claims CONTROL can check against its own records. An operator lease carries no
            # ticket (§3.1) and prepares only the operator kinds (runtime_grammar.KINDS_FOR_CALLER).
            if (claims["supervisor_uid"] != lease.peer_uid or claims["operator_uid"] != self.operator_uid
                    or "ticket" in claims or claims["kind"] not in ("interactive", "login")):
                raise Refused("claims-mismatch")
            lease.used = True
        try:
            with self.store.lock(session):
                if self.store.get(session) is not None:
                    raise Refused("session-exists")
                rec = R.SessionState(session=session, generation=0, state=R.ALLOCATED, claims=claims,
                                     lease_live=True, event_kind="state",
                                     updated_at=R.timestamp(self._clock()))
                self.store.put(rec)
        except BaseException:
            with self._mu:
                lease.used = False
                if not lease.live and self._leases.get(nonce) is lease:
                    self._retire(lease)                  # its EOF came while ATTACH held it: nothing binds it now
            raise
        with self._mu:
            lease.session = session
            self._session_lease[session] = lease
            died = not lease.live
        if died:   # the lease read EOF between the nonce check and the bind: revoke as its EOF would
            self._spawn_revoke(session)
        return {"generation": 0}

    def _op_cas(self, conn: Conn, args: dict) -> dict[str, object]:
        return {"generation": self.cas(args["session"], args["generation"], args["from"], args["to"],
                                       args["fields"])}

    def cas(self, session: object, generation: object, frm: object, to: object,
            fields: object) -> int:
        """§7.2 CAS. Static checks first (no lock): states named, the edge in :data:`records.EDGES` (or
        ``from == to``: a field-only CAS), every field CAS-writable and valid. Then, under the try-lock: a
        CAS whose ``to`` is already the record's state, with every field already holding its value, AND whose
        generation is exactly one behind the record's counts as done (§7.4: the retry of a CAS whose answer was
        lost) and answers the current generation, unless ``to`` is a live state and the lease is gone
        (``lease-dead``, §3.3, checked in the shortcut too); any other caller at an older generation is
        ``stale-generation``, never told it won a move another made. Else the generation must be the record's,
        ``from`` its state, and a move into a live state needs the lease live. ``null`` as a field value removes
        the field (DECISION)."""
        session = R.check_session(session)
        generation = R._uint(generation, "generation")
        frm = R._enum(frm, "from", R.STATES)
        to = R._enum(to, "to", R.STATES)
        if type(fields) is not dict:
            raise Refused("wrong-type", "fields is an object")
        for k in sorted(fields):
            if k not in R.CAS_FIELDS:
                raise Refused("field-not-casable", k[:64])
        checked = {k: (None if v is None else R.FIELD_CHECKS[k](v)) for k, v in sorted(fields.items())}
        if checked.get("touched") == []:
            checked["touched"] = None
        if frm != to and (frm, to) not in R.EDGES:
            raise Refused("edge-not-allowed", f"{frm} -> {to}")
        if frm == to and (to == R.PURGED or not checked):
            raise Refused("edge-not-allowed", f"{frm} -> {to} with no field")   # DECISION
        if frm != to and R.ARTIFACT_FIELDS & set(checked):
            # DECISION: §3.6 stores ONE event_kind per generation; a CAS that moved the state AND set an
            # artifact field would owe two events. The 3b CAS is field-only (RETAINED -> RETAINED).
            raise Refused("edge-not-allowed", "an artifact field moves with no state change")
        revoked = False
        with self.store.lock(session):
            rec = self.store.get(session)
            if rec is None:
                raise Refused("no-session")
            live_to = to in R.LIVE_STATES and to != R.ALLOCATED
            if (frm != to and rec.state == to and rec.generation == generation + 1
                    and all(_current(rec, k) == v for k, v in checked.items())):
                if live_to and not self._lease_live(session):
                    raise Refused("lease-dead")
                return rec.generation                                  # §7.4: already done
            if generation != rec.generation:
                raise Refused("stale-generation")
            if rec.state != frm:
                raise Refused("from-mismatch")
            if live_to and not self._lease_live(session):
                raise Refused("lease-dead")
            merged = {k: v for k, v in rec.fields.items() if k not in checked}
            merged.update({k: v for k, v in checked.items() if v is not None and k != "touched"})
            touched = rec.touched
            if "touched" in checked:
                touched = tuple(checked["touched"] or ())
            R.check_exclusive(merged)
            kind = "state" if frm != to else ("artifact" if R.ARTIFACT_FIELDS & set(checked) else "none")
            new = self._write(rec, state=to, fields=merged, touched=touched, event_kind=kind)
            revoked = to == R.REVOKED and frm != R.REVOKED
        if revoked:
            self._after_revoke(session)
        return new.generation

    def _op_mint(self, conn: Conn, args: dict) -> dict[str, object]:
        """§3.3 MINT, §5.1 render (5): only in RENDERING with the lease live. Merges the MINT claims into
        the record (``event_kind: none``, §3.6) and mints the §3.2 handles: a session token only if the
        claims carry a brokered route, a queue cap only if ``queue_grant``. A front is registered with keys
        before MINT answers (§4.2)."""
        session = R.check_session(args["session"])
        generation = R._uint(args["generation"], "generation")
        claims = R.parse_mint_claims(args["claims"])
        with self.store.lock(session):
            rec = self.store.get(session)
            if rec is None:
                raise Refused("no-session")
            with self._mu:
                lease = self._session_lease.get(session)
            if lease is None or not lease.live:
                raise Refused("lease-dead")
            if rec.state != R.RENDERING:
                raise Refused("not-rendering")
            if generation != rec.generation:
                raise Refused("stale-generation")
            if R.has_mint_claims(rec.claims):
                raise Refused("already-minted")                        # DECISION: MINT once per session
            # DECISION: an egress session has exactly one front (§1 gw-front), a none class none (§5.1 5b).
            if ("front" in claims) != (rec.claims.get("class") == "egress"):
                raise Refused("claims-mismatch")
            merged = {**rec.claims, **claims}
            if "front" in claims:
                f = claims["front"]
                self._keys_front_register(session, f["uid"], f["pid"], f["start"], claims["incarnation"])
            h = Handles(token=new_handle() if self._is_brokered(merged) else None,
                        queue_cap=new_handle() if claims["queue_grant"] else None)
            new = self._write(rec, claims=merged, event_kind="none")
            # The lease can read EOF during the keys wire or the write (lease_eof takes no session lock): the
            # handles are published only if that same lease is live NOW, under the lock lease_eof drops them under.
            with self._mu:
                if self._session_lease.get(session) is not lease or not lease.live:
                    raise Refused("lease-dead")
                self._handles[session] = h
        return {"generation": new.generation}

    def _op_front_arm(self, conn: Conn, args: dict) -> dict[str, object]:
        """§3.3 FRONT-ARM, §5.1 render (5b): relayed to keys as §4.9's ``FRONT-ARM {session}``; CONTROL
        answers only after keys' ack. The session lock is not held across the relay."""
        session = R.check_session(args["session"])
        generation = R._uint(args["generation"], "generation")
        with self.store.lock(session):
            rec = self.store.get(session)
            if rec is None:
                raise Refused("no-session")
            if not self._lease_live(session):
                raise Refused("lease-dead")
            if rec.state != R.RENDERING or "front" not in rec.claims:
                raise Refused("not-rendering")
            if generation != rec.generation:
                raise Refused("stale-generation")
            with self._mu:
                lease = self._session_lease.get(session)
        self._keys_front_arm(session)
        # The lock is not held across the relay, so the session can move under it: re-check both before root is
        # told it may write the go byte.
        with self._mu:
            lease_ok = self._session_lease.get(session) is lease and lease is not None and lease.live
        if not lease_ok:
            raise Refused("lease-dead")
        now = self.store.get(session)
        if now is None or now.state != R.RENDERING or now.generation != rec.generation:
            raise Refused("not-rendering")
        return {"generation": rec.generation}

    def _op_secrets(self, conn: Conn, args: dict) -> dict[str, object]:
        """§3.2: root's ``start`` fetches the handles by session, only while RENDERED."""
        session = R.check_session(args["session"])
        rec = self.store.get(session)
        if rec is None:
            raise Refused("no-session")
        if not self._lease_live(session):
            raise Refused("lease-dead")
        if rec.state != R.RENDERED:
            raise Refused("not-rendered")
        with self._mu:
            h = self._handles.get(session) or Handles()
        out: dict[str, object] = {}
        if h.token is not None:
            out["token"] = h.token
        if h.queue_cap is not None:
            out["queue_cap"] = h.queue_cap
        return out

    def _op_revoke(self, conn: Conn, args: dict) -> dict[str, object]:
        """§3.3 REVOKE: a live record -> REVOKED; no record -> created at REVOKED (§3.1: the alloc-only
        orphan); a record already past live -> a no-op answering its generation (§4.5)."""
        session = R.check_session(args["session"])
        revoked = False
        with self.store.lock(session):
            rec = self.store.get(session)
            if rec is None:
                rec = R.SessionState(session=session, generation=0, state=R.REVOKED, claims={},
                                     lease_live=False, event_kind="state",
                                     updated_at=R.timestamp(self._clock()))
                self.store.put(rec)
                revoked = True
            elif rec.state in R.LIVE_STATES:
                rec = self._write(rec, state=R.REVOKED, event_kind="state",
                                  lease_live=rec.lease_live and self._lease_live(session))
                revoked = True
        if revoked:
            self._after_revoke(session)
        return {"generation": rec.generation}

    def _op_revoked_set(self, conn: Conn, args: dict) -> dict[str, object]:
        """§3.3 REVOKED-SET, served from memory. DECISION: the answer is an object, so the list rides
        ``sessions``; a RETAINED record with no claims (a REVOKE-created one) names no slot, so it stays
        in the set."""
        out = []
        for r in self.store.all():
            if r.state in R.REVOKED_SET_STATES:
                out.append(r.session)
            elif r.state == R.RETAINED:
                slot = r.claims.get("slot")
                if not isinstance(slot, dict) or not self._slot_reusable(slot["class"], slot["index"], r.session):
                    out.append(r.session)
        return {"sessions": sorted(out)}

    # -- test / service views --------------------------------------------------------------------------------

    def handles(self, session: str) -> Handles | None:
        with self._mu:
            return self._handles.get(session)


# --- the server ---------------------------------------------------------------------------------------------

class ControlServer:
    """:class:`Control` on a unix stream socket at ``path``, one thread per connection.

    ``peer_uid(sock) -> int | None`` is the kernel's answer by default (``SO_PEERCRED`` /
    ``LOCAL_PEERCRED``, via the stub's reader); a test that cannot be another uid injects its own. The
    socket path is ``chmod``ed 0660 before ``listen`` (§2.2: ``fchmod`` on a socket fd does not reach the
    path); its owner and group are the service install's."""

    def __init__(self, control: Control, path: Path, *,
                 peer_uid: Callable[[socket.socket], int | None] = kernel_peer_uid,
                 max_connections: int = MAX_CONNECTIONS, frame_read_s: float = FRAME_READ_S) -> None:
        self.control = control
        self.path = Path(path)
        self._peer_uid = peer_uid
        self.max_connections = max_connections
        self.frame_read_s = frame_read_s
        self._listener: socket.socket | None = None
        self._conns: set[socket.socket] = set()
        self._mu = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()

    def start(self) -> None:
        """The start pass first (no request is served before every live record is revoked), then bind."""
        self.control.start()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        s.bind(str(self.path))
        os.chmod(self.path, 0o660)
        s.listen(64)
        s.settimeout(0.1)
        self._listener = s
        t = threading.Thread(target=self._accept_loop, daemon=True, name="control-accept")
        t.start()
        self._threads.append(t)

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                sock, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            sock.settimeout(None)
            with self._mu:
                full = len(self._conns) >= self.max_connections
                if not full:
                    self._conns.add(sock)
            if full:
                sock.close()                             # over the cap: closed at accept, nothing read
                continue
            t = threading.Thread(target=self._serve, args=(sock,), daemon=True, name="control-conn")
            with self._mu:
                self._threads.append(t)
            t.start()

    def _send(self, sock: socket.socket, answer: Mapping[str, object]) -> bool:
        try:
            frame = encode_frame(answer)
        except FrameTooLarge:
            # DECISION: §3.3 caps every frame; an answer that would pass the cap (a STATUS over many
            # records) is refused by name and the caller asks per session.
            frame = encode_frame(refused(answer.get("id"), "answer-too-large"))
        try:
            sock.sendall(frame)
            return True
        except OSError:
            return False

    def _serve(self, sock: socket.socket) -> None:
        try:
            uid = self._peer_uid(sock)
        except OSError:
            uid = None
        conn = Conn(uid)
        try:
            while not self._stop.is_set():
                try:
                    req = read_frame(sock, frame_timeout=self.frame_read_s)
                except FrameTooLarge:
                    self._send(sock, refused(None, "frame-too-large"))
                    return
                except (FrameMalformed, OSError):
                    return
                if req is None:
                    return
                try:
                    check_request(req)
                except FrameMalformed:
                    return
                if not self._send(sock, self.control.handle(conn, req)):
                    return
        finally:
            # The lease dies before the socket closes, so a peer that sees the close sees a dead lease.
            if conn.lease is not None and not self._stop.is_set():
                self.control.lease_eof(conn.lease)
            with self._mu:
                self._conns.discard(sock)
                me = threading.current_thread()
                if me in self._threads:
                    self._threads.remove(me)
            try:
                sock.close()
            except OSError:
                pass

    def stop(self) -> None:
        """Stop serving, as a crash would look to every session: no revoke on the way down."""
        self._stop.set()
        self.control.stop()
        if self._listener is not None:
            self._listener.close()
        with self._mu:
            conns = list(self._conns)
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for t in list(self._threads):
            t.join(2)


__all__ = ["CONTROL_FORMAT", "Conn", "Control", "ControlServer", "FRAME_MAX", "FRAME_READ_S", "FrameMalformed",
           "FrameTooLarge", "Handles", "Lease", "MAX_CONNECTIONS", "PEER_VERBS", "RETIRED_LEASES_MAX", "StateStore",
           "check_request", "encode_frame", "new_handle", "ok", "read_frame", "refused"]
