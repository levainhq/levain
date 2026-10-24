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
``MINT``, ``FRONT-ARM``, ``SECRETS``, ``REVOKE``, ``REVOKED-SET``); session 3 the queue broker's
``QUEUE-RESOLVE`` (§3.3, §26 S1-c); S1 session 3b the keys peer's ``TOKEN-RESOLVE`` and CONTROL's side of keys'
admin wire (:class:`KeysAdmin`, §4.9: ``FRONT-REGISTER`` fenced by the per-``front_uid`` registration counter in
``L/sessions/front-reg/``, ``FRONT-ARM``, ``DROP-SESSION``); S1 session 3c-3 keys' long-lived ``KEYS-ATTACH``
connection and the verbs it carries (``FRONT-LOST``, ``SPEND-OVERRUN``, ``SESSIONS``: §4.5, 3c design r3 §2.1 (e),
R4 §1.4), whose EOF revokes every egress-class session, and :func:`serve`, the program's one entry; S1 3c-4 the
``_levain_dispatch`` peer's ``ROUTE-SUBMIT`` (§4.3, 3c design r3 §2.3 as R4 §2 amends it: :mod:`levain.slot.routemaps`),
its start pass and the RELOADs to keys, each under the one activation lock; S1 3c-5 its ``RUN-TICKET-MINT`` /
``-CANCEL`` / ``-STATUS`` over the ticket journal (:mod:`levain.slot.tickets`), and the binding of a ticket: its
``LEASE-OPEN {ticket}``, the ``ATTACH`` of a ticket-bound lease (``ticket_facts``), ``MINT``'s claims against the
ticket and ``TOKEN-RESOLVE``'s ``run``, read from the ticket record and never from root's claims; S1 3c-6b keys'
``LINEAGE-APPEND {envelope}`` into the lineage sink (:mod:`levain.slot.lineage`). Root's ``SLOT-SYNC`` and the other dispatch verbs (§3.3, §3.5, §3.6) are later 3c sessions': until then
every other peer, and every other verb of a known one, is refused ``verb-not-allowed-for-peer`` (see
:data:`PEER_VERBS`). Every act on keys and the reaper wake are seams: :class:`KeysAdmin` supplies the keys ones when
CONTROL is given one.
"""
from __future__ import annotations

import base64
import collections
import hashlib
import hmac
import os
import re
import signal
import socket
import struct
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterator, Mapping

import fcntl

from levain.broker.stub import peer_uid as kernel_peer_uid
from levain.contracts import ContractError, canon
from levain.slot import Refused
from levain.slot import records as R
from levain.slot import lineage as LN
from levain.slot import routemaps
from levain.slot import tickets as T
from levain.slot.layout import STORE, session_repo
from levain.slot.runtime_grammar import HANDLE_BYTES, _handle, _keys

CONTROL_FORMAT = "levain-control/1"
FRAME_MAX = 64 * 1024                      # §3.3: the frame cap
_LEN = struct.Struct(">I")

ROOT_UID = 0

#: §3.3's verbs per peer, as built so far (3c-6b adds keys' ``LINEAGE-APPEND``). Later 3c sessions add root's ``SLOT-SYNC`` and
#: the rest of the ``_levain_dispatch`` row (3c-4 builds its ``ROUTE-SUBMIT``, 3c-5 the three ticket verbs and ``LEASE-OPEN {ticket}``; the peer is ``dispatch`` when
#: :meth:`Control.peer_kind` is given its uid, else ``foreign``). keys' ``FRONT-LOST`` / ``SPEND-OVERRUN`` / ``SESSIONS`` / ``LINEAGE-APPEND`` are answered only on the connection that
#: holds the current ``KEYS-ATTACH`` (else ``not-attached``).
PEER_VERBS: Mapping[str, frozenset[str]] = {
    "operator": frozenset({"LEASE-OPEN", "STATUS"}),
    "root": frozenset({"ATTACH", "CAS", "MINT", "FRONT-ARM", "SECRETS", "REVOKE", "REVOKED-SET"}),
    "qbroker": frozenset({"QUEUE-RESOLVE"}),
    "keys": frozenset({"TOKEN-RESOLVE", "KEYS-ATTACH", "FRONT-LOST", "SPEND-OVERRUN", "SESSIONS", "LINEAGE-APPEND"}),
    "dispatch": frozenset({"LEASE-OPEN", "ROUTE-SUBMIT", "RUN-TICKET-MINT", "RUN-TICKET-CANCEL", "RUN-TICKET-STATUS"}),
    "foreign": frozenset(),
}

#: Each verb's request keys beside ``v``, ``op``, ``id``: (allowed, required).
_ARGS: Mapping[str, tuple[frozenset[str], frozenset[str]]] = {
    "LEASE-OPEN": (frozenset({"ticket"}), frozenset()),       # a ticket from dispatch only (§3.1 step 1)
    "STATUS": (frozenset({"session"}), frozenset()),
    "ATTACH": (frozenset({"lease_nonce", "sudo_uid", "session", "claims"}),) * 2,
    "CAS": (frozenset({"session", "generation", "from", "to", "fields"}),) * 2,
    "MINT": (frozenset({"session", "generation", "claims"}),) * 2,
    "FRONT-ARM": (frozenset({"session", "generation"}),) * 2,
    "SECRETS": (frozenset({"session"}),) * 2,
    "REVOKE": (frozenset({"session"}),) * 2,
    "REVOKED-SET": (frozenset(), frozenset()),
    "QUEUE-RESOLVE": (frozenset({"slot", "cap_digest"}),) * 2,
    "TOKEN-RESOLVE": (frozenset({"token_digest"}),) * 2,
    "KEYS-ATTACH": (frozenset({"generation", "item_sha256"}),) * 2,
    "FRONT-LOST": (frozenset({"session"}),) * 2,
    "SPEND-OVERRUN": (frozenset({"session", "route", "model", "ref", "overrun", "unproven"}),) * 2,
    "SESSIONS": (frozenset({"digests"}),) * 2,
    "LINEAGE-APPEND": (frozenset({"envelope"}),) * 2,     # S1 3c-6b (H6-2): CONTRACT §3.3 keys row
    "ROUTE-SUBMIT": (frozenset({"map", "sig"}),) * 2,
    # the per-kind required fields are the §3.5 table's (tickets.check_facts), refused by field name
    "RUN-TICKET-MINT": (frozenset({"attempt_id"}) | T.MINT_FIELDS, frozenset({"attempt_id"})),
    "RUN-TICKET-CANCEL": (frozenset({"ticket"}),) * 2,
    "RUN-TICKET-STATUS": (frozenset({"attempt_id"}),) * 2,
}

#: DECISION: how long a mint waits for keys to serve CONTROL's route-map head on the current attachment (an
#: attach-time or activation RELOAD in flight) before it answers ``keys-unavailable``, which dispatch retries.
MINT_KEYS_WAIT_S = 2.0

#: KEYS-ATTACH's ``item_sha256``: the hex SHA-256 of the route-map item keys loaded (3c design R4 §2).
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")
#: SPEND-OVERRUN's ``ref``: keys' ledger hold ref ``[ledger_id, epoch, seq]``, each id 32 lower-case hex digits.
_HEX32_RE = re.compile(r"[0-9a-f]{32}")
#: a route id or a model name as SPEND-OVERRUN carries it (keys' own policy names; CONTROL records, never acts on them).
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}")
#: DECISION: the SPEND-OVERRUN records CONTROL keeps for the card (R4 §4), oldest forgotten first. Memory only: a
#: CONTROL restart loses them, as R4 §1.4 says (its restart is keys' attachment EOF, which refuses every token).
SPEND_OVERRUNS_MAX = 4096
#: DECISION: the token digests one SESSIONS may carry; keys sends more as several (a test pins its chunk below this).
SESSIONS_DIGESTS_MAX = 512

#: §3.3 / c6: a ``*_digest`` on the wire, the form the broker sends a queue cap's digest in.
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
#: The run fields ``QUEUE-RESOLVE`` copies from the session's bound ticket (§3.3); ``repo_root`` is added by CONTROL.
QUEUE_RUN_KEYS = frozenset({"run_id", "job_id", "run_index", "contract_digest", "model_requested"})
#: ``TOKEN-RESOLVE``'s ``run`` (§3.3) beside ``run_id``: the bound ticket's own facts, never root's claims.
RESOLVE_RUN_FACTS = ("job_id", "run_index", "contract_digest", "route", "routemap_generation", "route_policy_digest",
                     "model_requested", "reserved_microdollars")

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
#: DECISION: the digests of dropped session tokens kept to answer ``TOKEN-RESOLVE`` ``token-revoked``; past this
#: many the oldest is forgotten and answers ``token-unknown``, which keys refuses just as surely (``token-invalid``).
REVOKED_TOKENS_MAX = 4096

#: §4.9: each admin request's answer deadline per attempt; a missed one is sent again on the same connection.
KEYS_ANSWER_S = 2.0
#: DECISION (complement r2 2): after a failed connect to keys' admin socket the next verb that needs keys tries again
#: only once this backoff has passed, doubling from the first value to the cap; until then it is ``keys-unavailable``.
KEYS_RECONNECT_FIRST_S = 0.25
KEYS_RECONNECT_MAX_S = 5.0
#: §3.3 / §4.9 / E23 (a): the per-``front_uid`` registration counters live in this subdirectory of the session store.
FRONT_REG_DIR = "front-reg"


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

@contextmanager
def try_lock(path: Path, what: str) -> Iterator[None]:
    """An exclusive ``flock`` on ``path``, TRY-locked only (§3.3): another holder = ``Refused("locked")``, never a
    wait. The file is opened per section and never cached, so two threads hold two open file descriptions and the
    flock excludes them; it is never removed (unlinking a lock file lets two holders lock two inodes)."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as e:
        raise Refused("bad-root", f"{path}: {e.strerror}") from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused("locked", what) from None
        yield
    finally:
        os.close(fd)   # closing the only descriptor releases the flock


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

    def lock(self, session: str):
        """The session lock, TRY-locked only (§3.3): another holder = ``Refused("locked")``, never a wait."""
        return try_lock(self._path(session, ".lock"), session)

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

    def in_memory(self) -> list[R.SessionState]:
        """Every record as memory holds it (an unsettled one as it was before its failed write), with no disk read and
        no settle: for a path that must not miss a session because one record cannot be read (keys' attachment EOF)."""
        with self._mu:
            return list(self._mem.values())

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
    #: a dispatch lease's run ticket (§3.1 step 1); the operator's leases carry none
    ticket: str | None = None


@dataclass(eq=False)
class Conn:
    """CONTROL's view of one connection: its peer and, once ``LEASE-OPEN`` answered, its lease; once ``KEYS-ATTACH``
    answered, the attachment's number. ``after`` holds what CONTROL does once the current answer is on the wire (the
    attach-time ``RELOAD``, R4 §10); ``sock`` lets CONTROL end a superseded attachment."""

    peer_uid: int | None
    lease: Lease | None = None
    sock: socket.socket | None = field(default=None, repr=False)
    attach_no: int | None = None
    after: list[Callable[[], object]] = field(default_factory=list, repr=False)


@dataclass(frozen=True)
class Handles:
    """§3.2: what ``MINT`` minted for a session, held in CONTROL's memory only (they die at REVOKED, and a
    CONTROL restart revokes every live session, so no handle needs to outlive the process). ``queue_cap_digest``
    is computed once, here: ``QUEUE-RESOLVE`` compares digests and never hashes a cap under the lock."""

    token: str | None = None
    queue_cap: str | None = None
    #: the digest the broker resolves the cap by (c6: ``sha256:`` + hex over its UTF-8 bytes).
    queue_cap_digest: str | None = field(default=None, init=False, compare=False, repr=False)
    #: the digest keys resolves the session token by (``TOKEN-RESOLVE {token_digest}``, the same form).
    token_digest: str | None = field(default=None, init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.queue_cap is not None:
            object.__setattr__(self, "queue_cap_digest", digest_of(self.queue_cap))
        if self.token is not None:
            object.__setattr__(self, "token_digest", digest_of(self.token))


def digest_of(handle: str) -> str:
    """A handle's digest on the wire: ``sha256:`` + hex over its UTF-8 bytes (c6; keys' ``token_digest``)."""
    return "sha256:" + hashlib.sha256(handle.encode("utf-8")).hexdigest()


def _default_is_brokered(claims: Mapping[str, object]) -> bool:
    # DECISION: §3.2 mints a token "only if the registered claims carry a `brokered` route"; a route's type
    # lives in the route maps (§4.3), which CONTROL loads in session 3c. Until then the session's
    # registered `auth` stands for it: a brokered session's vendor route is its brokered route.
    return claims.get("auth") == "brokered"


def _run_facts_ok(facts: object) -> bool:
    """The run fields a seam hands ``QUEUE-RESOLVE``, exactly :data:`QUEUE_RUN_KEYS` with their §3.3 types: CONTROL
    is the one author of what the broker is told, so it checks the values too, not only the names."""
    if not isinstance(facts, Mapping) or set(facts) != QUEUE_RUN_KEYS:
        return False
    run_id, job_id, idx = facts["run_id"], facts["job_id"], facts["run_index"]
    model, digest = facts["model_requested"], facts["contract_digest"]
    return (type(run_id) is str and bool(run_id) and type(job_id) is str and bool(job_id)
            and type(idx) is int and 0 <= idx <= R.JSON_INT_MAX
            and type(digest) is str and _DIGEST_RE.fullmatch(digest) is not None
            and (model is None or type(model) is str))


def _keys_unavailable(*_a: object) -> None:
    # The keys admin wire (§4.9) is session 3c's. Fail closed until it is wired.
    raise Refused("keys-unavailable", "the keys admin wire is not wired")


def _nothing(*_a: object) -> None:
    return None


class AdminRefused(Refused):
    """keys answered an admin request ``{"ok":false,"refused":<name>}``; ``answer`` is the whole answer (a
    FRONT-REGISTER ``registration-stale`` carries ``reg_high`` there)."""

    def __init__(self, name: str, answer: Mapping[str, object], conn: int = 0) -> None:
        super().__init__(name, "keys refused an admin request")
        self.answer = dict(answer)
        self.conn = conn                              # the connection (KeysAdmin epoch) the refusal came on


@dataclass(eq=False)
class _Pending:
    conn: int                                     # the connection the request went out on (KeysAdmin._conn_no)
    done: threading.Event = field(default_factory=threading.Event)
    answer: dict | None = None


class KeysAdmin:
    """CONTROL's ONE long-lived connection to keys' admin socket (§4.9, ``R/keys/admin.sock``), multiplexed by ``id``:
    a write lock, an answer table per ``id`` and a reader thread, so MINT's FRONT-REGISTER, root's FRONT-ARM and a
    background DROP-SESSION share it and are answered in any order.

    - Each attempt waits :data:`KEYS_ANSWER_S` for its answer, never past what is left of V23; a missed one is sent
      again, byte for byte, ON THE SAME CONNECTION (an answer to any attempt answers the request) until V23 has passed
      since the first, and then CONTROL closes the connection: that is §4.5's EOF, never a retry. The request refuses
      ``keys-unavailable``, and an answer arriving after V23 is discarded the same way. A send that cannot complete
      within ``answer_s`` is a broken connection.
    - Each connection is an EPOCH (its number, from 1). The verbs that register or arm return the epoch keys acked
      them on, so CONTROL can tell a registration of a lost connection from one of its successor.
    - The connection's EOF (keys went away, or CONTROL closed it at V23, on a malformed answer or by :meth:`abort`)
      wakes every request waiting on it with ``keys-unavailable`` and calls ``on_eof(epoch)`` (CONTROL revokes the
      sessions registered on that epoch); keys, on its side, clears every registration that connection made.
    - There is no connection until a verb needs keys: the next one connects (outside the state lock, so a slow
      connect never stalls :meth:`close`). After a failed connect, verbs refuse ``keys-unavailable`` without trying
      until :data:`KEYS_RECONNECT_FIRST_S` has passed, doubling to :data:`KEYS_RECONNECT_MAX_S`; a connect that
      succeeds resets it.

    ``answer_s``, ``window_s`` (V23) and ``clock`` are parameters so a test can shorten the clocks."""

    def __init__(self, path: Path | str, *, answer_s: float = KEYS_ANSWER_S, window_s: float | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        from levain.slot.control_client import V23_SECONDS   # lazy: a module-level import would be circular
        self.path = Path(path)
        self.answer_s = answer_s
        self.window_s = V23_SECONDS if window_s is None else window_s
        self._clock = clock
        self.on_eof: Callable[[int], None] = lambda epoch: None
        self._mu = threading.Lock()
        self._wmu = threading.Lock()
        self._cmu = threading.Lock()                  # one connect at a time, never under _mu
        self._sock: socket.socket | None = None
        self._conn_no = 0
        self._next_id = 0
        self._pending: dict[int, _Pending] = {}
        self._retry_at = 0.0
        self._backoff = KEYS_RECONNECT_FIRST_S
        self._closed = False
        self._readers: list[threading.Thread] = []

    # -- the connection --------------------------------------------------------------------------------------

    def connected(self) -> bool:
        with self._mu:
            return self._sock is not None

    def current(self, epoch: int) -> bool:
        """Is connection ``epoch`` the live one (not yet ended, not superseded)?"""
        with self._mu:
            return self._sock is not None and self._conn_no == epoch

    def _live(self, connect: bool) -> tuple[socket.socket, int] | None:
        """The live connection, or ``None`` when a connect may be tried now (caller holds ``_mu``)."""
        if self._closed:
            raise Refused("keys-unavailable", "the keys admin client is closed")
        if self._sock is not None:
            return self._sock, self._conn_no
        if not connect:
            raise Refused("keys-unavailable", "no keys admin connection")
        if self._clock() < self._retry_at:
            raise Refused("keys-unavailable", "keys admin connect backing off")
        return None

    def _connection(self, connect: bool) -> tuple[socket.socket, int]:
        with self._mu:
            live = self._live(connect)
        if live is not None:
            return live
        with self._cmu:
            with self._mu:
                live = self._live(connect)              # another thread may have connected while this one waited
            if live is not None:
                return live
            now = self._clock()
            s = None
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(self.answer_s)
                s.connect(str(self.path))
                s.settimeout(None)
                # A send keys does not drain within answer_s fails (SO_SNDTIMEO), so the write lock is never held
                # forever and the V23 rule is always reached; the reader's blocking receive is unaffected.
                secs = int(self.answer_s)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO,
                             struct.pack("ll", secs, int((self.answer_s - secs) * 1_000_000)))
            except OSError as e:
                if s is not None:
                    s.close()
                with self._mu:
                    self._retry_at = now + self._backoff
                    self._backoff = min(self._backoff * 2, KEYS_RECONNECT_MAX_S)
                raise Refused("keys-unavailable", f"{self.path}: {e.strerror or e}") from None
            with self._mu:
                if self._closed:
                    s.close()
                    raise Refused("keys-unavailable", "the keys admin client is closed")
                self._retry_at, self._backoff = 0.0, KEYS_RECONNECT_FIRST_S
                self._conn_no += 1
                self._sock = s
                no = self._conn_no
                t = threading.Thread(target=self._read_loop, args=(s, no), daemon=True,
                                     name=f"control-keys-admin-{no}")
                self._readers = [r for r in self._readers if r.is_alive()] + [t]
                t.start()                                # under _mu: close() never meets an unstarted reader
        return s, no

    def _read_loop(self, sock: socket.socket, no: int) -> None:
        try:
            while True:
                ans = read_frame(sock, frame_timeout=FRAME_READ_S)
                if ans is None:
                    return
                rid = ans.get("id")
                if type(rid) is not int or type(ans.get("ok")) is not bool:
                    return                                   # off the wire's shape: the stream is lost
                if ans["ok"] is False and type(ans.get("refused")) is not str:
                    return
                with self._mu:
                    p = self._pending.get(rid)
                    if p is None or p.conn != no or p.done.is_set():
                        continue                             # a late answer to an attempt already answered
                    p.answer = ans
                p.done.set()
        except (FrameMalformed, FrameTooLarge, OSError):
            return
        finally:
            self._lost(sock, no, notify=True)

    def _lost(self, sock: socket.socket, no: int, *, notify: bool) -> None:
        """Connection ``no`` is over: every request waiting on it is woken (no answer = ``keys-unavailable``) and,
        unless CONTROL itself is closing the client, ``on_eof(no)`` runs once."""
        with self._mu:
            if self._sock is not sock:
                return                                       # already handled
            self._sock = None
            waiting = [p for p in self._pending.values() if p.conn == no]
            closing = self._closed
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()
        for p in waiting:
            p.done.set()
        if notify and not closing:
            self.on_eof(no)

    def abort(self, epoch: int | None = None) -> None:
        """CONTROL ends connection ``epoch`` (``None``: whichever is live) through the EOF path: keys reads the close
        and clears every registration it made, and ``on_eof`` runs. A connection already ended is left as it is."""
        with self._mu:
            sock, no = self._sock, self._conn_no
        if sock is not None and (epoch is None or epoch == no):
            self._lost(sock, no, notify=True)

    def close(self) -> None:
        """Close for good (CONTROL stopping): no ``on_eof``, every waiter answered ``keys-unavailable``."""
        with self._mu:
            self._closed = True
            sock, no = self._sock, self._conn_no
            readers = list(self._readers)
        if sock is not None:
            self._lost(sock, no, notify=False)
        for t in readers:
            if t is not threading.current_thread() and t.ident is not None:
                t.join(2)

    # -- one request -----------------------------------------------------------------------------------------

    def call(self, op: str, *, connect: bool = True, **args: object) -> dict[str, object]:
        """Send ``op`` and return the answer's fields (without ``id`` / ``ok``); a refusal raises
        :class:`AdminRefused`; no connection, an EOF or V23 with no answer raise ``Refused("keys-unavailable")``.
        ``connect=False``: no new connection is made for this request (it refuses instead)."""
        return self._call(op, connect, args)[0]

    def _call(self, op: str, connect: bool, args: Mapping[str, object]) -> tuple[dict[str, object], int]:
        """:meth:`call`, also returning the epoch the answer came on."""
        sock, no = self._connection(connect)
        with self._mu:
            self._next_id += 1
            rid = self._next_id
        frame = encode_frame({"v": 1, "op": op, "id": rid, **args})   # before the entry exists: a raise leaks none
        with self._mu:
            if self._closed or self._sock is not sock:    # the connection ended between the two: no wakeup would come
                raise Refused("keys-unavailable", f"{op}: the keys admin connection ended")
            p = self._pending[rid] = _Pending(no)
        first = self._clock()
        try:
            while True:
                try:
                    with self._wmu:
                        sock.sendall(frame)
                except OSError:
                    self._lost(sock, no, notify=True)
                    raise Refused("keys-unavailable", f"{op}: the keys admin connection broke") from None
                left = first + self.window_s - self._clock()
                answered = left > 0 and p.done.wait(min(self.answer_s, left))
                if self._clock() - first >= self.window_s and p.answer is not None:
                    answered = False                         # it came after V23: discarded, as no answer at all
                if answered:
                    break
                if self._clock() - first >= self.window_s:
                    self._lost(sock, no, notify=True)       # V23 with no ack: CONTROL closes the wire (§4.5)
                    raise Refused("keys-unavailable", f"{op}: no answer from keys within V23")
        finally:
            with self._mu:
                self._pending.pop(rid, None)
        ans = p.answer
        if ans is None:
            raise Refused("keys-unavailable", f"{op}: the keys admin connection ended")
        if ans["ok"] is False:
            raise AdminRefused(ans["refused"], ans, no)
        return {k: v for k, v in ans.items() if k not in ("id", "ok")}, no

    # -- §4.9's verbs, as CONTROL's seams --------------------------------------------------------------------

    def front_register(self, session: str, front_uid: int, pid: int, start: int, incarnation: int,
                       reg_token: int) -> int:
        """FRONT-REGISTER; returns the epoch keys acked it on."""
        return self._call("FRONT-REGISTER", True, {"session": session, "front_uid": front_uid, "pid": pid,
                                                   "start": start, "incarnation": incarnation,
                                                   "reg_token": reg_token})[1]

    def front_arm(self, session: str, reg_token: int) -> int:
        """FRONT-ARM; returns the epoch keys acked it on."""
        return self._call("FRONT-ARM", True, {"session": session, "reg_token": reg_token})[1]

    def drop_session(self, session: str) -> None:
        """DROP-SESSION on the live connection. With none, or once that connection ends unanswered (EOF, or closed at
        V23), there is nothing to drop: keys cleared every registration of a connection when it ended, and a new
        connection starts against an empty table. So a drop is never retried past its connection."""
        try:
            self.call("DROP-SESSION", connect=False, session=session)
        except AdminRefused:
            raise
        except Refused as e:
            if e.name != "keys-unavailable":
                raise


class Control:
    """CONTROL's verbs over a :class:`StateStore`.

    Seams (each a callable; ``keys_admin``, a :class:`KeysAdmin`, supplies the three keys ones and CONTROL learns its
    EOF; production wires the reaper wake socket): ``drop_session(session)`` (§4.9 ``DROP-SESSION``, acked; the
    default does nothing, which is true while no keys process holds anything) · ``wake_reaper()`` ·
    ``keys_front_register(session, front_uid, pid, start, incarnation, reg_token)`` and
    ``keys_front_arm(session, reg_token)`` (default: refused ``keys-unavailable``; keys' ``registration-stale``
    arrives as an :class:`AdminRefused`) · ``keys_uid`` (the ``_levain_keys`` peer, whose verb is
    ``TOKEN-RESOLVE``; ``None``: no peer is keys) ·
    ``slot_reusable(class, index, session) -> bool`` (the §3.5 mirror; default ``False``, which keeps a
    RETAINED session in ``REVOKED-SET``: the revoker's kill is uid + start checked, so over-inclusion is
    safe and under-inclusion is not) · ``is_brokered(claims) -> bool`` · ``clock() -> epoch seconds`` (persisted
    timestamps only) · ``monotonic() -> seconds`` (every deadline and interval: a wall-clock step never moves one) ·
    ``sleep(seconds)`` (the EOF retry's backoff) · ``qbroker_uid`` (the ``_levain_qbroker`` peer; ``None``: no
    peer is the broker) · ``run_facts(session) -> {run_id, job_id, run_index, contract_digest, model_requested} |
    None`` (the run a dispatch session's bound ticket names; the default, ``None``, reads it from ``tickets``, and with
    no ticket store a dispatch session's ``QUEUE-RESOLVE`` is refused ``run-unknown``) · ``host`` (whose store
    ``repo_root`` names) · ``routemap_head() -> {generation, item_sha256} | None`` (CONTROL's active dispatch route-map
    head, R4 §2; ``routes`` supplies it when given, else the default ``None`` sends no attach-time ``RELOAD``) ·
    ``routes`` (a :class:`levain.slot.routemaps.ControlRoutes`: ``ROUTE-SUBMIT``, the start pass, and THE activation
    lock every RELOAD to keys is sent under; ``None``: ``ROUTE-SUBMIT`` refused ``routemap-verifier``) · ``dispatch_uid``
    (the ``_levain_dispatch`` peer; ``None``: no peer is dispatch) · ``tickets`` (a :class:`levain.slot.tickets.TicketStore`:
    the run-ticket journal, compacted by :meth:`start`; ``None``: every ticket verb refused ``journal-unavailable``) ·
    ``lineage`` (a :class:`levain.slot.lineage.LineageSink`: ``L/lineage/lineage.jsonl``, opened by :meth:`start`;
    ``None``: ``LINEAGE-APPEND`` refused ``lineage-unavailable``)."""

    def __init__(self, store: StateStore, operator_uid: int, *,
                 qbroker_uid: int | None = None,
                 keys_uid: int | None = None,
                 keys_admin: KeysAdmin | None = None,
                 run_facts: Callable[[str], Mapping[str, object] | None] | None = None,
                 host: str = "darwin" if sys.platform == "darwin" else "linux",
                 drop_session: Callable[[str], object] = _nothing,
                 wake_reaper: Callable[[], object] = _nothing,
                 keys_front_register: Callable[..., object] = _keys_unavailable,
                 keys_front_arm: Callable[[str], object] = _keys_unavailable,
                 slot_reusable: Callable[[str, int, str], bool] = lambda c, i, s: False,
                 is_brokered: Callable[[Mapping[str, object]], bool] = _default_is_brokered,
                 clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 routemap_head: Callable[[], Mapping[str, object] | None] | None = None,
                 routes: routemaps.ControlRoutes | None = None,
                 dispatch_uid: int | None = None,
                 tickets: T.TicketStore | None = None,
                 lineage: LN.LineageSink | None = None) -> None:
        if type(operator_uid) is not int or operator_uid <= ROOT_UID:
            raise Refused("bad-config", f"operator uid {operator_uid!r}")
        if qbroker_uid is not None and (type(qbroker_uid) is not int or qbroker_uid <= ROOT_UID
                                        or qbroker_uid == operator_uid):
            raise Refused("bad-config", f"qbroker uid {qbroker_uid!r}")
        if keys_uid is not None and (type(keys_uid) is not int or keys_uid <= ROOT_UID
                                     or keys_uid in (operator_uid, qbroker_uid)):
            raise Refused("bad-config", f"keys uid {keys_uid!r}")
        if dispatch_uid is not None and (type(dispatch_uid) is not int or dispatch_uid <= ROOT_UID
                                         or dispatch_uid in (operator_uid, qbroker_uid, keys_uid)):
            raise Refused("bad-config", f"dispatch uid {dispatch_uid!r}")
        if type(host) is not str or host not in STORE:
            raise Refused("bad-config", f"host {host!r}")
        if keys_admin is not None:
            drop_session, keys_front_register, keys_front_arm = (
                keys_admin.drop_session, keys_admin.front_register, keys_admin.front_arm)
            keys_admin.on_eof = self._keys_eof
        self.keys_admin = keys_admin
        self.store = store
        self.qbroker_uid = qbroker_uid
        self.keys_uid = keys_uid
        self._run_facts = run_facts if run_facts is not None else self._ticket_run_facts
        self._host = host
        self.operator_uid = operator_uid
        self._drop_session = drop_session
        self._wake_reaper = wake_reaper
        self._keys_front_register = keys_front_register
        self._keys_front_arm = keys_front_arm
        self._slot_reusable = slot_reusable
        self._is_brokered = is_brokered
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._mu = threading.Lock()
        self._leases: dict[str, Lease] = {}            # nonce -> lease (a dead one kept, bounded: _retire)
        self._session_lease: dict[str, Lease] = {}
        self._handles: dict[str, Handles] = {}
        #: token digests whose handles were dropped (oldest first, bounded): TOKEN-RESOLVE answers them token-revoked.
        self._revoked_tokens: collections.OrderedDict[str, None] = collections.OrderedDict()
        self._drops_owed: set[str] = set()
        #: session -> the keys admin epoch its FRONT-REGISTER was acked on (KeysAdmin only); dropped at REVOKED.
        self._reg_epoch: dict[str, int] = {}
        #: every keys admin epoch at or below this one has ended and had its EOF handled.
        self._keys_lost_upto = 0
        #: sessions whose keys-EOF revoke worker is running; another EOF coalesces into it (one worker per session).
        self._keys_revoking: set[str] = set()
        self._retired: collections.deque[str] = collections.deque()   # dead lease nonces, oldest first
        self._stopping = threading.Event()
        #: The background threads still running (revokes, drop retries); each removes itself as it ends.
        self._eof_threads: list[threading.Thread] = []
        store.on_settle = self._on_settle
        #: Exceptions a seam raised after a revoke landed (the revoke itself is durable by then).
        self.seam_errors: list[tuple[str, str, BaseException]] = []
        self.routes = routes
        self.dispatch_uid = dispatch_uid
        if routemap_head is None:
            routemap_head = routes.head if routes is not None else (lambda: None)
        self._routemap_head = routemap_head
        #: THE activation lock (R4 §2): the routes' own, so ROUTE-SUBMIT, the start pass and every RELOAD to keys (the
        #: attach-time one included) are one order
        self._activation = routes.lock if routes is not None else threading.Lock()
        #: keys' current attachment (the connection whose KEYS-ATTACH was answered last and has not ended), its number,
        #: and what keys stated (and, after an attach-time RELOAD, acked) it serves on THAT attachment (R4 §2).
        self._attach: Conn | None = None
        self._attach_no = 0
        self._keys_serves: dict[str, object] | None = None
        #: notified whenever ``_keys_serves`` changes: a mint waits on it for keys to serve CONTROL's head (R4 §2)
        self._serves_cv = threading.Condition(self._mu)
        self.tickets = tickets
        self.lineage = lineage
        #: (session, ref) -> the SPEND-OVERRUN record, oldest first (R4 §1.4, §4: the card's data; never a revoke).
        self._overruns: collections.OrderedDict[tuple[str, tuple], dict[str, object]] = collections.OrderedDict()
        #: the sessions keys reported FRONT-LOST for, oldest first, bounded (doctor / tests).
        self.front_lost: collections.deque[str] = collections.deque(maxlen=SPEND_OVERRUNS_MAX)

    # -- peers -------------------------------------------------------------------------------------------

    def peer_kind(self, uid: int | None) -> str:
        if uid == ROOT_UID:
            return "root"
        if uid == self.operator_uid:
            return "operator"
        if uid is not None and uid == self.qbroker_uid:
            return "qbroker"
        if uid is not None and uid == self.keys_uid:
            return "keys"
        if uid is not None and uid == self.dispatch_uid:
            return "dispatch"
        return "foreign"

    # -- start pass (§1 CONTROL row, §1.3) ---------------------------------------------------------------

    def start(self) -> None:
        """CONTROL's start pass: load every record, then treat every record whose lease was live as a
        lease EOF (no lease survives a CONTROL restart): ``lease_live:false``, then live state ->
        REVOKED, then the keys drop + reaper wake. Work is retained by END, which the reaper runs.

        Every record already at or past REVOKED gets its ``DROP-SESSION`` again (idempotent, §4.9): a
        CONTROL that died between the REVOKED write and the drop, or whose drop never got an ack, owes it
        still. Then one reaper wake. The split keys off the STATE alone: a record past live that still says
        ``lease_live:true`` (CONTROL died between a CAS into REVOKED and its after-acts) has the flag cleared,
        then its drop and the wake like every other record past live.

        The route maps' start pass runs first (:meth:`levain.slot.routemaps.ControlRoutes.start_pass`: an interrupted
        activation finished, under the activation lock); keys is sent the head by the attach-time ``RELOAD``.

        Before all of it, the ticket journal's single-CONTROL lock (3c-5 fix 2, L3 r1 codex 1:
        :meth:`levain.slot.tickets.TicketStore.acquire`): a second CONTROL refuses ``control-running`` (noted in
        :attr:`seam_errors`) before it finishes an activation, revokes a record, compacts the journal or, in
        :class:`ControlServer`, unlinks the first one's socket. A start that fails after taking it (a route recovery,
        the record load, a journal refused `journal-inconsistent`, the revoke pass) releases it before the failure
        is raised (3c-5 fix 3, L3 r2): a CONTROL that did not start holds nothing, so a replacement in the same process
        is never refused ``control-running``."""
        if self.tickets is not None:
            try:
                self.tickets.acquire()
            except Refused as e:
                self.seam_errors.append(("start", "", e))
                raise
        try:
            self._start_pass()
        except BaseException:
            if self.tickets is not None:
                self.tickets.close()
            if self.lineage is not None:
                self.lineage.close()
            raise

    def _start_pass(self) -> None:
        """:meth:`start` after the journal lock: the route maps' start pass, the records, the ticket journal, the
        revoke pass."""
        if self.routes is not None:
            self.routes.start_pass()
        loaded = self.store.load()
        if self.tickets is not None:
            # the compaction runs before any append is admitted (CONTRACT §3.5); the records are loaded first so a
            # binding whose session record was never written is undone (head ruling H8)
            self.tickets.start(has_session=lambda s: s in loaded)
        if self.lineage is not None:
            # S1 3c-6b: the sink read whole (membership, seq), its torn tail cut, before any LINEAGE-APPEND is served;
            # a corrupt sink refuses the start ``lineage-corrupt``
            self.lineage.start()
        past = False
        for rec in sorted(self.store.all(), key=lambda r: r.session):
            if rec.state in R.LIVE_STATES:
                self._revoke_after_eof(rec.session)
                continue
            if rec.lease_live:
                self._revoke_after_eof(rec.session)      # past live: clears the flag only
            self._drop(rec.session)
            past = True
        if past:
            self._wake()

    def stop(self) -> None:
        """Stop serving WITHOUT revoking: the records stay as they are, and the next :meth:`start` revokes
        them (a stop is a restart, as far as any session can tell). The keys admin connection is closed with no
        revoke: keys reads its EOF and clears what it made."""
        self._stopping.set()
        if self.keys_admin is not None:
            self.keys_admin.close()
        if self.tickets is not None:
            self.tickets.close()
        if self.lineage is not None:
            self.lineage.close()

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
        """§4.5: CONTROL at REVOKED drops the session's handles, wakes the reaper, then sends ``DROP-SESSION`` to
        keys. Called after the REVOKED write is durable and the session lock released."""
        with self._mu:
            self._drop_handles(session)
            epoch = self._reg_epoch.pop(session, None)
        self._wake()                                     # first: the reaper's kill never waits on keys' answer
        self._drop(session, epoch)
        self._prune(session)

    def _drop_handles(self, session: str) -> None:
        """Forget a session's handles (caller holds ``_mu``); its token's digest is remembered, bounded, so
        ``TOKEN-RESOLVE`` answers it ``token-revoked``."""
        h = self._handles.pop(session, None)
        if h is not None and h.token_digest is not None:
            self._revoked_tokens[h.token_digest] = None
            while len(self._revoked_tokens) > REVOKED_TOKENS_MAX:
                self._revoked_tokens.popitem(last=False)

    def _wake(self) -> None:
        try:
            self._wake_reaper()
        except BaseException as e:   # noqa: BLE001 - the reaper also runs on its own pass; record the failure
            self.seam_errors.append(("wake_reaper", "", e))

    def _drop_moot(self, epoch: int | None) -> bool:
        """keys' connection ``epoch`` has ended, so keys already cleared every registration it made: nothing to drop."""
        return self.keys_admin is not None and epoch is not None and not self.keys_admin.current(epoch)

    def _refused_off_epoch(self, epoch: int | None, e: AdminRefused) -> tuple[int | None, bool]:
        """A drop refused on connection ``e.conn``: the epoch the debt is bound to (the first refusal binds a debt that
        had none) and whether the debt is moot. A refusal on another connection than the bound one means the bound
        connection ended, and keys cleared its registrations at that EOF: the debt never moves to the newer one."""
        if self.keys_admin is None:
            return epoch, False
        if epoch is None:
            return e.conn, False
        return epoch, e.conn != epoch

    def _drop(self, session: str, epoch: int | None = None) -> None:
        """``DROP-SESSION`` once now (``epoch``: the keys connection the session registered on, if known; once it has
        ended the drop is moot). A failure is retried with backoff on a background thread, and the debt ends only by
        an ack, by CONTROL stopping (whose next start re-issues it) or by a CONFIRMED teardown: with
        :class:`KeysAdmin`, a drop still refused at V23 since the first try makes CONTROL close the connection the
        debt is bound to, through the EOF path, and keys clears every registration of a connection that ends (§4.5,
        §4.9). The debt is bound to ONE connection for its whole life, a fencing token: the registration's, else the
        one that first refused it; it is never carried onto a newer connection. Another seam has no connection to
        close, so it is retried until acked or stopped, never forgotten."""
        with self._mu:
            if session in self._drops_owed:
                return                                   # a retry is already running
        if self._drop_moot(epoch):
            return
        try:
            self._drop_session(session)
            return
        except AdminRefused as e:
            self.seam_errors.append(("drop_session", session, e))
            epoch, moot = self._refused_off_epoch(epoch, e)
            if moot:
                return
        except BaseException as e:   # noqa: BLE001 - the revoke is durable; the drop is owed
            self.seam_errors.append(("drop_session", session, e))
        with self._mu:
            if session in self._drops_owed:
                return
            self._drops_owed.add(session)
        self._spawn(lambda s: self._drop_until_acked(s, epoch), session, name=f"control-drop-{session}")

    def _drop_until_acked(self, session: str, epoch: int | None) -> None:
        from levain.slot.control_client import V23_SECONDS   # lazy: a module-level import would be circular
        delay = EOF_RETRY_FIRST_S
        until = self._monotonic() + V23_SECONDS
        try:
            while not self._stopping.is_set():
                if self._drop_moot(epoch):
                    return                               # that connection ended: keys cleared what it made
                if self.keys_admin is not None and self._monotonic() >= until:
                    self.seam_errors.append(("drop_session", session, TimeoutError(
                        "no ack within V23: CONTROL closes the keys admin connection the drop is bound to")))
                    self.keys_admin.abort(epoch)
                    return
                self._sleep(delay)
                delay = min(delay * 2, EOF_RETRY_MAX_S)
                if self._drop_moot(epoch):
                    return                               # it ended during the sleep: never sent to its successor
                try:
                    self._drop_session(session)
                    return
                except AdminRefused as e:
                    epoch, moot = self._refused_off_epoch(epoch, e)
                    if moot:
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
                self._drop_handles(new.session)
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
        try:
            rec = self.store.get(session)
        except Refused:
            return                                       # unsettled: the dead lease stays bound (it reads dead)
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
                self._drop_handles(session)
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
        try:
            t.start()
        except BaseException:
            with self._mu:
                if t in self._eof_threads:
                    self._eof_threads.remove(t)
            raise
        return t

    def _revoke_after_eof(self, session: str, *, lease_eof: bool = True) -> None:
        """``lease_live:false`` first (its own generation), then live state -> REVOKED, retrying the lock
        with backoff until it is taken: never dropped on ``locked``. A failed write (``OSError``) is retried
        the same way, and so is any other refusal (recorded once in :attr:`seam_errors`): a revoke owed is
        never given up. Then the keys drop and the reaper wake. A lease EOF whose first attempt fails also sends the
        keys drop at once, so keys' token cache follows the handles already dropped. ``lease_eof=False`` (keys' admin
        EOF, §4.5): the
        lease is not what ended, so the REVOKED write keeps ``lease_live`` as the ``REVOKE`` verb does."""
        delay = EOF_RETRY_FIRST_S
        noted = False
        dropped_early = False
        spawn_noted = False
        while True:
            try:
                revoked = False
                with self.store.lock(session):
                    rec = self.store.get(session)
                    if rec is None:
                        return
                    if lease_eof and rec.lease_live:
                        rec = self._write(rec, lease_live=False, event_kind="none")
                    if rec.state in R.LIVE_STATES:
                        rec = self._write(rec, state=R.REVOKED, event_kind="state",
                                          lease_live=rec.lease_live and self._lease_live(session))
                        revoked = True
                break
            except (Refused, OSError) as e:
                if not noted and not (isinstance(e, Refused) and e.name == "locked"):
                    self.seam_errors.append(("revoke", session, e))
                    noted = True
                if self._stopping.is_set():
                    return
                if lease_eof and not dropped_early:
                    # The handles are gone already; keys must not keep the token cached while the revoke waits, so
                    # it hears the drop now, on its own thread (the reaper wake never waits on keys). Idempotent: the
                    # drop after the durable REVOKED still runs.
                    with self._mu:
                        epoch = self._reg_epoch.get(session)
                    try:
                        self._spawn(lambda s, ep=epoch: self._drop(s, ep), session, name=f"control-eofdrop-{session}")
                        dropped_early = True
                    except RuntimeError as spawn_err:  # no thread to be had: the revoke keeps retrying, and so does this
                        if not spawn_noted:
                            self.seam_errors.append(("eofdrop", session, spawn_err))
                            spawn_noted = True
                self._sleep(delay)
                delay = min(delay * 2, EOF_RETRY_MAX_S)
        if revoked:
            self._after_revoke(session)
        else:
            self._prune(session)

    def _keys_eof(self, epoch: int) -> None:
        """keys' admin connection ``epoch`` ended (§4.5, :class:`KeysAdmin`): keys cleared every registration and cached
        token that connection made, so the sessions registered on it, and on any earlier one, are the victims, read
        from memory (no store read: a record that cannot settle never hides a victim). Under one hold of ``_mu`` the
        epoch is marked lost (a MINT or FRONT-ARM acked on it then refuses rather than publish) and every victim's
        handles are dropped, before any write; then each victim is REVOKED on its own thread, which retries its
        session lock and any refusal until it lands. A session registered on a LATER connection is never a victim."""
        if self._stopping.is_set():
            return
        with self._mu:
            self._keys_lost_upto = max(self._keys_lost_upto, epoch)
            victims = sorted(s for s, e in self._reg_epoch.items() if e <= epoch)
            for session in victims:
                self._drop_handles(session)
        for session in victims:
            self._spawn_keys_revoke(session)

    def _spawn_keys_revoke(self, session: str) -> threading.Thread | None:
        """The session's keys-EOF revoke, at most one in flight: a worker already running for it retries until the
        revoke lands, so a later EOF's request is already covered (``None``: coalesced into that worker)."""
        with self._mu:
            if session in self._keys_revoking:
                return None
            self._keys_revoking.add(session)

        def run(s: str) -> None:
            try:
                self._revoke_after_eof(s, lease_eof=False)
            finally:
                with self._mu:
                    self._keys_revoking.discard(s)

        try:
            return self._spawn(run, session, name=f"control-keys-eof-{session}")
        except BaseException:
            with self._mu:
                self._keys_revoking.discard(session)     # never started: nothing would clear the mark
            raise

    def _keys_epoch_lost(self, epoch: object) -> bool:
        """Has keys connection ``epoch`` ended (caller holds ``_mu``)? Always ``False`` with no :class:`KeysAdmin`."""
        if self.keys_admin is None:
            return False
        return type(epoch) is not int or epoch <= self._keys_lost_upto or not self.keys_admin.current(epoch)

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
        """§3.1 step 1: the connection becomes the lease. A second ``LEASE-OPEN`` on a connection whose lease is live
        is refused (DECISION: ``lease-exists``). The operator's lease carries no ticket (``ticket-not-allowed``);
        dispatch's carries a live UNBOUND ticket (``ticket-required`` without one; the store's ``ticket-unknown`` /
        ``ticket-bound`` / ``attempt-closed``), held by no other live lease (``ticket-bound``: one ticket, one lease).
        The hold is this lease's, in memory; the ``ATTACH`` that binds the ticket is what the journal records, so a
        lease that dies before its ``ATTACH`` leaves the ticket unbound (§3.5 Pool, OPEN)."""
        ticket = args.get("ticket")
        if self.peer_kind(conn.peer_uid) == "dispatch":
            if ticket is None:
                raise Refused("ticket-required")
            ticket = _handle(ticket, "ticket")
            self._ticket_store().holdable(ticket)
        elif ticket is not None:
            raise Refused("ticket-not-allowed")
        with self._mu:
            if conn.lease is not None and conn.lease.live:
                raise Refused("lease-exists")
            if ticket is not None and any(x.live and x.ticket == ticket for x in self._leases.values()):
                raise Refused("ticket-bound")
            lease = Lease(new_handle(), conn.peer_uid, ticket=ticket)
            self._leases[lease.nonce] = lease
            conn.lease = lease
        return {"lease_nonce": lease.nonce}

    def _op_status(self, conn: Conn, args: dict) -> dict[str, object]:
        """§3.3: the records whose ``claims.operator_uid`` is the peer. DECISION: the answer is an object,
        so the list rides ``records``. Head ruling H8 (1024+8): a record's ``claims.ticket`` is left out (the ticket is
        a bearer handle that reaches no record a non-root reader opens, H1)."""
        want = R.check_session(args["session"]) if "session" in args else None
        recs = []
        for r in sorted(self.store.all(), key=lambda r: r.session):
            if r.claims.get("operator_uid") == conn.peer_uid and (want is None or r.session == want):
                rec = R.state_record(r)
                rec["claims"] = {k: v for k, v in rec["claims"].items() if k != "ticket"}
                recs.append(rec)
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
                # fix 2 (codex 4 + complement 1): the retry of an ATTACH whose record landed (whole, or NotDurable)
                # answers what the record says, `session-exists`, never a ticket refusal
                raise Refused("session-exists" if lease.session == session else "nonce-used")
            if lease.peer_uid != sudo_uid:
                raise Refused("nonce-uid-mismatch")
            # §3.3: the claims CONTROL can check against its own records. An operator lease carries no
            # ticket (§3.1) and prepares only the operator kinds (runtime_grammar.KINDS_FOR_CALLER); a dispatch
            # lease's claims name ITS ticket, and the kind and class that ticket renders as (§3.5).
            if claims["supervisor_uid"] != lease.peer_uid or claims["operator_uid"] != self.operator_uid:
                raise Refused("claims-mismatch")
            if lease.ticket is None:
                if "ticket" in claims or claims["kind"] not in ("interactive", "login"):
                    raise Refused("claims-mismatch")
            elif claims.get("ticket") != lease.ticket:
                raise Refused("claims-mismatch")
            lease.used = True
        facts: dict[str, object] | None = None
        landed: R.NotDurable | None = None
        try:
            if lease.ticket is not None:
                held = self._ticket_store().holdable(lease.ticket)
                if (claims["kind"], claims["class"]) != T.RENDERS_AS[str(held.facts["kind"])]:
                    raise Refused("claims-mismatch")
            with self.store.lock(session):
                if self.store.get(session) is not None:
                    raise Refused("session-exists")
                if lease.ticket is not None:
                    # journaled before the record exists: a crash between the two leaves the ticket bound and no
                    # session, which runs nothing; CONTROL's next start unbinds it (head ruling H8)
                    facts = dict(self._ticket_store().bind(lease.ticket, session).facts)
                try:
                    rec = R.SessionState(session=session, generation=0, state=R.ALLOCATED, claims=claims,
                                         lease_live=True, event_kind="state",
                                         updated_at=R.timestamp(self._clock()))
                    self.store.put(rec)
                except R.NotDurable as e:
                    # the record was renamed into place: it exists and the bind stands, so the ATTACH is SETTLED
                    # below as one that landed (fix 2, codex 4 + complement 1), then its failure is answered
                    landed = e
                except BaseException:
                    # 3c-5 fix 1 (L2 4a): no record was written, so the binding is undone and journaled; if that
                    # append fails the store is poisoned and the restart's H8 pass unbinds it instead
                    if facts is not None:
                        try:
                            self._ticket_store().unbind(str(lease.ticket), session)
                        except Refused:
                            pass
                    raise
        except BaseException:
            with self._mu:
                lease.used = False
                if not lease.live and self._leases.get(nonce) is lease:
                    self._retire(lease)                  # its EOF came while ATTACH held it: nothing binds it now
            raise
        # published for a record that landed, NotDurable included: the lease controls it (its EOF revokes it, a CAS
        # reads the lease live) and `used` stays set, so a retry answers `session-exists`
        with self._mu:
            lease.session = session
            self._session_lease[session] = lease
            died = not lease.live
        if died:   # the lease read EOF between the nonce check and the bind: revoke as its EOF would
            self._spawn_revoke(session)
        if landed is not None:
            raise landed
        if facts is not None:
            return {"generation": 0, "ticket_facts": facts}   # §3.3: the ticket's facts, for render's check
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
        before MINT answers (§4.2), fenced by its ``reg_token`` (§4.9, E23):

        1. the ``front_uid``'s counter is bumped (:meth:`_bump_reg_token`, its own lock, synchronous);
        2. the token is written ALONE into the session's claims (the TOKEN-ONLY state, a generation of its own,
           ``event_kind`` none), and the record that write returns is the one every later step builds on;
        3. FRONT-REGISTER carries it; only once keys acks it are the MINT claims merged and written.

        keys' ``registration-stale {reg_high}`` steps down unless CONTROL's counter was behind: then the counter is
        raised to ``reg_high``, bumped, prewritten as in 2, and the register retried ONCE. Every other end past the
        first prewrite refuses (``front-registration-stale``, or ``keys-unavailable`` when keys is unavailable or
        refusing) and leaves the record TOKEN-ONLY, authorising nothing, until the lease's EOF revokes it. A
        ``locked`` uid lock after the first prewrite is ``front-registration-stale``, never ``locked``: the caller
        would retry it at a generation the prewrite made stale."""
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
            if "ticket" in rec.claims:
                self._check_mint_against_ticket(session, rec.claims["ticket"], claims)
            epoch = None
            lost = False
            if "front" in claims:
                rec, epoch = self._register_front(rec, claims)
                if self.keys_admin is not None:
                    with self._mu:
                        self._reg_epoch[session] = epoch    # from here keys' EOF of that epoch finds it a victim
                        # An EOF handled before the line above never saw this session: it is revoked here instead.
                        lost = self._keys_epoch_lost(epoch)
            if not lost:
                merged = {**rec.claims, **claims}
                h = Handles(token=new_handle() if self._is_brokered(merged) else None,
                            queue_cap=new_handle() if claims["queue_grant"] else None)
                new = self._write(rec, claims=merged, event_kind="none")
                # The lease can read EOF during the keys wire or the write (lease_eof takes no session lock): the
                # handles are published only if that same lease is live NOW, under the lock lease_eof drops them
                # under. Likewise keys' connection: published only if the epoch keys acked the register on has not
                # ended since, under the lock _keys_eof marks it lost under; else the session is left to its revoke.
                with self._mu:
                    if self._session_lease.get(session) is not lease or not lease.live:
                        raise Refused("lease-dead")
                    lost = epoch is not None and self._keys_epoch_lost(epoch)
                    if not lost:
                        self._handles[session] = h
        if lost:
            self._spawn_keys_revoke(session)             # runs once this verb releases the session lock
            raise Refused("keys-unavailable", f"{session}: keys' connection ended after FRONT-REGISTER")
        return {"generation": new.generation}

    def _bound_ticket(self, session: str, ticket: object) -> T.Ticket:
        """The ticket BOUND to ``session`` that its ATTACH claims named; ``run-unknown`` when there is none (no ticket
        store, or a journal that no longer holds the binding)."""
        t = self.tickets.bound(session) if self.tickets is not None else None
        if t is None or not isinstance(ticket, str) or not hmac.compare_digest(t.ticket, ticket):
            raise Refused("run-unknown", "no ticket is bound to the session")
        return t

    def _check_mint_against_ticket(self, session: str, ticket: object, claims: Mapping[str, object]) -> None:
        """§3.5 "Ticket facts reach render": CONTROL compares MINT's claims with its own ticket record,
        ``claims-mismatch`` on any difference. The routes are the ticket's ``route`` (none for a verifier) and its
        ``egress_routes``; the egress subset is exactly ``egress_routes``; the model is exactly ``model_requested``
        (none for a verifier)."""
        facts = self._bound_ticket(session, ticket).facts
        egress = set(facts["egress_routes"])                          # type: ignore[arg-type]
        brokered = {facts["route"]} if "route" in facts else set()
        routes, egress_claimed = set(claims["routes"]), set(claims["egress_routes"])   # type: ignore[arg-type]
        if (routes != brokered | egress or egress_claimed != egress
                or claims.get("model") != facts.get("model_requested")):
            raise Refused("claims-mismatch")

    def _ticket_run_facts(self, session: str) -> Mapping[str, object] | None:
        """The default ``run_facts`` seam: the run of the ticket bound to ``session`` (``QUEUE-RESOLVE``'s fields)."""
        t = self.tickets.bound(session) if self.tickets is not None else None
        if t is None:
            return None
        return {"run_id": t.run_id, "job_id": t.facts["job_id"], "run_index": t.facts["run_index"],
                "contract_digest": t.facts["contract_digest"], "model_requested": t.facts.get("model_requested")}

    def _register_front(self, rec: R.SessionState, claims: Mapping[str, object]) -> tuple[R.SessionState, object]:
        """MINT's steps 1-3 for a session with a front (caller holds the session lock); returns the TOKEN-ONLY
        record keys acked the registration of, which MINT's final write builds on, and what the register seam
        returned (with :class:`KeysAdmin`, the epoch it was acked on)."""
        front = claims["front"]
        assert isinstance(front, Mapping)
        uid = front["uid"]
        token = self._bump_reg_token(uid)                      # a `locked` here passes through: nothing written yet
        for retried in (False, True):
            rec = self._write(rec, claims={**rec.claims, R.REG_TOKEN: token}, event_kind="none")
            try:
                epoch = self._keys_front_register(rec.session, uid, front["pid"], front["start"],
                                                  claims["incarnation"], token)
                return rec, epoch
            except AdminRefused as e:
                if e.name != "registration-stale":
                    raise Refused("keys-unavailable", f"keys refused FRONT-REGISTER: {e.name}") from None
                reg_high = e.answer.get("reg_high")
                if retried or type(reg_high) is not int or not 0 <= reg_high < R.JSON_INT_MAX:
                    raise Refused("front-registration-stale", f"front uid {uid}") from None
            except Refused as e:
                raise Refused("keys-unavailable", e.detail or e.name) from None
            try:
                stepped = self._bump_reg_token(uid, floor=reg_high)
            except Refused as e:
                if e.name == "locked":
                    raise Refused("front-registration-stale", f"front uid {uid} counter locked") from None
                raise
            if stepped is None:                                # not behind: a newer registration stands (step down)
                raise Refused("front-registration-stale", f"front uid {uid}")
            token = stepped
        raise AssertionError("unreachable")

    def _front_reg_dir(self) -> Path:
        """``L/sessions/front-reg/`` (E23 (a)). The install makes it; CONTROL, its sole writer, makes it 0700 if absent."""
        d = self.store.dir / FRONT_REG_DIR
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            pass
        except OSError as e:
            raise Refused("bad-root", f"{d}: {e.strerror}") from None
        if os.path.islink(d) or not os.path.isdir(d):
            raise Refused("bad-root", f"{d} is not a directory")
        return d

    def _bump_reg_token(self, front_uid: int, *, floor: int | None = None) -> int | None:
        """The registration counter's section (E23 (a)), under ``<front_uid>.lock`` taken exactly like the session
        lock and with nothing awaited while it is held: read the counter (absent = 0), +1, ``write_durably``, return
        the new token. ``floor`` (keys' ``reg_high``): only a counter BELOW it is raised to it and bumped; a counter at
        or above it is left as it is and ``None`` returned (step down). A record that does not parse refuses
        ``front-registration-corrupt`` and is never reset."""
        d = self._front_reg_dir()
        path = d / f"{front_uid}.json"
        with try_lock(d / f"{front_uid}.lock", f"front uid {front_uid}"):
            data = R.read_record(path)
            current = 0 if data is None else R.parse_front_reg(data, front_uid).reg_token
            if floor is not None:
                if current >= floor:
                    return None
                current = floor
            if current >= R.JSON_INT_MAX:
                raise Refused("front-registration-corrupt", f"front uid {front_uid}: the counter is at its maximum")
            token = current + 1
            R.write_durably(path, R.front_reg_bytes(R.FrontReg(front_uid, token)), fsync=self.store._fsync)
        return token

    def _op_front_arm(self, conn: Conn, args: dict) -> dict[str, object]:
        """§3.3 FRONT-ARM, §5.1 render (5b): relayed to keys as §4.9's ``FRONT-ARM {session, reg_token}``, the token
        read from the session's OWN claims (never the uid's current counter); CONTROL answers only after keys' ack.
        No ``reg_token`` in the claims, or keys' ``registration-stale``, refuses ``front-registration-stale`` with no
        retry (the session's front was replaced). The session lock is not held across the relay."""
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
            token = rec.claims.get(R.REG_TOKEN)
            if type(token) is not int:
                raise Refused("front-registration-stale", f"{session} holds no reg_token")
            with self._mu:
                lease = self._session_lease.get(session)
        try:
            arm_epoch = self._keys_front_arm(session, token)
        except AdminRefused as e:
            if e.name == "registration-stale":
                raise Refused("front-registration-stale", session) from None
            raise Refused("keys-unavailable", f"keys refused FRONT-ARM: {e.name}") from None
        # The lock is not held across the relay, so the session can move under it: re-check before root is told it
        # may write the go byte. The store first; then the lease and keys' connection, and the answer, in ONE hold of
        # the lock lease_eof and _keys_eof drop handles under, with nothing read or awaited after that check. The arm
        # counts only on the connection the session registered on, and only while that connection has not ended.
        now = self.store.get(session)
        if now is None or now.state != R.RENDERING or now.generation != rec.generation:
            raise Refused("not-rendering")
        with self._mu:
            if not (self._session_lease.get(session) is lease and lease is not None and lease.live):
                raise Refused("lease-dead")
            if not (self.keys_admin is None or (arm_epoch == self._reg_epoch.get(session)
                                                and not self._keys_epoch_lost(arm_epoch))):
                raise Refused("keys-unavailable", f"{session}: keys' connection ended around FRONT-ARM")
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

    # -- the queue broker's verb ------------------------------------------------------------------------------

    def _op_queue_resolve(self, conn: Conn, args: dict) -> dict[str, object]:
        """§3.3 ``QUEUE-RESOLVE``: the session whose queue cap has digest ``cap_digest`` and was minted for a
        session on ``slot``. A cap is dropped at the lease's EOF and after a REVOKED write, so a cap found is a
        session that was live when its record was read (a revoke landing during this verb can still be answered
        once; the broker serves any answer up to its RESOLVE_TTL anyway). DECISION: one refusal, ``cap-unknown``,
        for no such cap, a cap minted on another slot, and a session past the live states, so the broker learns
        nothing of another slot's caps; every later refusal is reached only through this slot's own live cap.
        An interactive session answers ``{session, kind}``; a dispatch session adds ``run``: its bound ticket's
        fields (``run_facts``) plus ``repo_root``, which CONTROL derives from the session name, never from the
        caller (§26 S1-c). Any other kind holds no queue (``kind-not-queued``)."""
        slot = R.slot_obj(R._slot(args["slot"], "slot"))
        digest = args["cap_digest"]
        if type(digest) is not str:
            raise Refused("wrong-type", "cap_digest is a string")
        if _DIGEST_RE.fullmatch(digest) is None:
            raise Refused("bad-token", "cap_digest")
        with self._mu:
            found = [(s, h) for s, h in self._handles.items()
                     if h.queue_cap is not None and hmac.compare_digest(h.queue_cap_digest, digest)]
        if len(found) != 1:
            raise Refused("cap-unknown")
        session, handles = found[0]
        rec = self.store.get(session)
        if rec is None or rec.state not in R.LIVE_STATES or rec.claims.get("slot") != slot:
            raise Refused("cap-unknown")
        with self._mu:
            if self._handles.get(session) is not handles:
                raise Refused("cap-unknown")           # its lease ended while the record was read
        kind = rec.claims.get("kind")
        if kind == "interactive":
            return {"session": session, "kind": kind}
        if kind != "dispatch":
            raise Refused("kind-not-queued")
        got = self._run_facts(session)
        if got is None:
            raise Refused("run-unknown")
        facts = dict(got) if isinstance(got, Mapping) else got   # one snapshot: checked and relayed are the same
        if not _run_facts_ok(facts):
            raise Refused("run-malformed")
        run: dict[str, object] = {k: facts[k] for k in sorted(QUEUE_RUN_KEYS)}
        run["repo_root"] = str(session_repo(self._host, session))
        return {"session": session, "kind": kind, "run": run}

    # -- keys' verb -------------------------------------------------------------------------------------------

    def _op_token_resolve(self, conn: Conn, args: dict) -> dict[str, object]:
        """§1.5 / E23 (d) ``TOKEN-RESOLVE {token_digest}``: the session a live session token belongs to and its
        PERSISTED claims WITHOUT ``reg_token`` (keys learns a token only from FRONT-REGISTER). ``token-unknown`` for a
        digest CONTROL never minted (or has forgotten, :data:`REVOKED_TOKENS_MAX`); ``token-revoked`` for a token
        whose handles were dropped (lease EOF, REVOKED, keys' admin EOF) or whose session is past live.
        A ticketed session's answer adds ``run`` (§3.3) from the ticket bound to it (``run-unknown`` when none is), and
        no answer carries the ticket itself."""
        digest = args["token_digest"]
        if type(digest) is not str:
            raise Refused("wrong-type", "token_digest is a string")
        if _DIGEST_RE.fullmatch(digest) is None:
            raise Refused("bad-token", "token_digest")
        with self._mu:
            found = [(s, h) for s, h in self._handles.items()
                     if h.token_digest is not None and hmac.compare_digest(h.token_digest, digest)]
            revoked = digest in self._revoked_tokens
        if len(found) != 1:
            raise Refused("token-revoked" if revoked else "token-unknown")
        session, handles = found[0]
        rec = self.store.get(session)
        if rec is None or rec.state not in R.LIVE_STATES:
            raise Refused("token-revoked")
        with self._mu:
            if self._handles.get(session) is not handles:
                raise Refused("token-revoked")         # dropped while the record was read
        # the ticket is a bearer handle for LEASE-OPEN and stays CONTROL's (head ruling H1): keys is told the run
        out: dict[str, object] = {"session": session,
                                  "claims": {k: v for k, v in rec.claims.items() if k not in (R.REG_TOKEN, "ticket")}}
        if "ticket" in rec.claims:
            t = self._bound_ticket(session, rec.claims["ticket"])
            if "route" not in t.facts:
                raise Refused("run-unknown", "the bound ticket names no route")
            out["run"] = {"run_id": t.run_id, **{k: t.facts[k] for k in RESOLVE_RUN_FACTS}}
        return out

    # -- keys' attachment (§4.5; 3c design r3 §2.1 (e), §2.4; R4 §1.4, §2) ---------------------------------------

    def _op_keys_attach(self, conn: Conn, args: dict) -> dict[str, object]:
        """keys' long-lived connection becomes THE attachment: it states the route-map generation keys serves
        (``generation`` + ``item_sha256``, or both ``null`` for none), recorded against this attachment only. One per
        connection (``attach-exists``). A newer attachment supersedes an older one still open: the older one's EOF rule
        runs (every egress-class session revoked) and its connection is closed. When CONTROL's route-map head differs
        from what keys stated, the attach-time ``RELOAD`` goes on keys' admin wire AFTER this answer is written (R4
        §10: keys waits on this answer, so a RELOAD sent inside the handler would stall it until V23)."""
        gen, sha = args["generation"], args["item_sha256"]
        serves: dict[str, object] | None
        if gen is None and sha is None:
            serves = None
        else:
            gen = R._uint(gen, "generation")
            if type(sha) is not str:
                raise Refused("wrong-type", "item_sha256 is a string")
            if _SHA256_HEX_RE.fullmatch(sha) is None:
                raise Refused("bad-value", "item_sha256")
            serves = {"generation": gen, "item_sha256": sha}
        with self._mu:
            if conn.attach_no is not None:
                raise Refused("attach-exists")
            old = self._attach
            self._attach_no += 1
            no = self._attach_no
            conn.attach_no = no
            self._attach = conn
            self._keys_serves = serves
            self._serves_cv.notify_all()
        if old is not None:
            self._attach_revoke()                        # the superseded attachment's EOF rule
            if old.sock is not None:
                try:
                    old.sock.shutdown(socket.SHUT_RDWR)  # its thread reads EOF; it is no longer the attachment
                except OSError:
                    pass
        head = self._routemap_head()
        if head is not None and dict(head) != serves:
            conn.after.append(lambda: self._spawn(lambda _s: self._attach_reload(no), "",
                                                  name=f"control-attach-reload-{no}"))
        return {"attach": no}

    def _attach_reload(self, no: int) -> None:
        """The attach-time ``RELOAD`` of CONTROL's head (R4 §2), for attachment ``no`` only, under the activation lock:
        the head is read again inside it (a ROUTE-SUBMIT may have moved it since the attach), and nothing is sent when
        keys already serves it."""
        with self._activation:
            with self._mu:
                if self._attach is None or self._attach.attach_no != no:
                    return
                serves = self._keys_serves
            head = self._routemap_head()
            if head is not None and dict(head) != serves:
                self._reload_keys(head)

    def _reload_keys(self, head: Mapping[str, object]) -> None:
        """``RELOAD {generation, item_sha256, keep}`` of ``head`` on keys' admin wire; the caller holds the activation lock.
        keys' ack is recorded as what keys serves on the attachment that was current when it was sent, if that is
        still the current one (a newer attachment states its own); a refusal, or any other failure of the call, is
        noted in :attr:`seam_errors` and never raised (the head is already durable: the activation stands, and keys
        is reached again by the attach-time RELOAD or a resubmission).

        Head ruling H5 (1024+8): the RELOAD carries ``keep`` (:meth:`_keep_generations`), and keys' ack lists what it
        still serves; a generation file is deleted only once that ack no longer lists it
        (:meth:`levain.slot.routemaps.ControlRoutes.delete_unserved`). An ack without a well-formed ``served`` list
        deletes nothing."""
        if self.keys_admin is None:
            return
        want = {"generation": head["generation"], "item_sha256": head["item_sha256"]}
        keep = self._keep_generations(int(head["generation"]))   # type: ignore[call-overload]
        with self._mu:
            no = None if self._attach is None else self._attach.attach_no
        try:
            got = self.keys_admin.call("RELOAD", **want, keep=sorted(keep))
        except Exception as e:
            self.seam_errors.append(("reload", "", e))
            return
        acked = got.get("generation") == want["generation"] and got.get("item_sha256") == want["item_sha256"]
        served = got.get("served")
        prune = (acked and self.routes is not None and type(served) is list
                 and all(type(g) is int for g in served) and want["generation"] in served)
        with self._mu:
            current = no is not None and self._attach is not None and self._attach.attach_no == no
            if current and acked:
                self._keys_serves = want
                self._serves_cv.notify_all()
            # fix 2 (codex 5): an ack deletes files only when it came on the CURRENT attachment, the same check that
            # records `_keys_serves`; a superseded attachment's late ack deletes nothing (the newer keys may still
            # serve a generation that ack no longer lists). Under `_mu`, so no attachment supersedes it mid-delete.
            if current and prune:
                try:
                    self.routes.delete_unserved(frozenset(served), keep)   # type: ignore[union-attr, arg-type]
                except Exception as e:   # noqa: BLE001 - files left behind are deleted after a later ack
                    self.seam_errors.append(("reload-prune", "", e))

    def _keep_generations(self, head_gen: int) -> frozenset:
        """RELOAD's ``keep`` (head ruling H5): the active generation, plus every route-map generation an OPEN ticket
        names, plus that of any ticket bound to a session whose record is still live."""
        live = {r.session for r in self.store.in_memory() if r.state in R.LIVE_STATES}
        refs = self.tickets.referenced_generations(live) if self.tickets is not None else frozenset()
        return frozenset({head_gen, *refs})

    # -- dispatch's route maps (§4.3; 3c design r3 §2.3, R4 §2) ------------------------------------------------

    def _op_route_submit(self, conn: Conn, args: dict) -> dict[str, object]:
        """``ROUTE-SUBMIT {map, sig}`` -> ``{generation}``: :meth:`levain.slot.routemaps.ControlRoutes.submit` under the
        activation lock, whose RELOAD to keys is :meth:`_reload_keys`. The answer is CONTROL's activation (its head and
        ``current`` durable); keys' ack is what a mint checks (3c-5), never this answer. A resubmission of the head is
        the convergent repair, its RELOAD re-sent unless :meth:`keys_serves` already names the head."""
        if self.routes is None:
            raise Refused("routemap-verifier", "CONTROL holds no route store")
        return {"generation": self.routes.submit(args["map"], args["sig"], self._reload_keys, self.keys_serves)}

    # -- dispatch's run tickets (§3.3, §3.5; 3c design r3 §2.1, R4 §2) ----------------------------------------------

    def _ticket_store(self) -> T.TicketStore:
        if self.tickets is None:
            raise Refused("journal-unavailable", "CONTROL holds no ticket journal")
        return self.tickets

    def _op_run_ticket_mint(self, conn: Conn, args: dict) -> dict[str, object]:
        """``RUN-TICKET-MINT`` -> ``{ticket, run_id}`` (``run_id`` CONTROL's own, head ruling H1). In this order: the
        attempt id's grammar and the §3.5 table's shapes (:func:`levain.slot.tickets.check_facts`); a REPLAY of a journaled attempt answers by the idempotent rule
        before anything else is read, so a route or config change between a crash and the replay does not alter it;
        a ``verifier`` ticket is ``verifier-unknown`` while no verifier generation is active (CONTROL activates none
        here: ``CONFIG-SUBMIT`` is not among its verbs);
        then the wait for keys to serve CONTROL's head on the current attachment, and, under the activation lock,
        the route facts checked against the ACTIVE generation (:meth:`_check_route_facts`) and the mint journaled
        with the ceiling (:meth:`levain.slot.tickets.TicketStore.mint`). Holding the activation lock means no
        activation moves the head between the check and the journal line."""
        store = self._ticket_store()
        attempt = T.check_attempt_id(args["attempt_id"])
        facts = T.check_facts(args)
        again = store.replay(attempt, facts)
        if again is not None:
            return {"ticket": again.ticket, "run_id": again.run_id}
        if facts["kind"] == "verifier":
            raise Refused("verifier-unknown", "no verifier generation is active")
        try:
            self._await_keys_serving_head()
        except Refused:
            # fix 2 (codex 3): a same-attempt request that raced a mint in while this one waited answers the first
            # ticket, never the route state's refusal
            again = store.replay(attempt, facts)
            if again is not None:
                return {"ticket": again.ticket, "run_id": again.run_id}
            raise
        with self._activation:
            # fix 2 (codex 3): every mint runs under the activation lock, so this replay is atomic with the mint below:
            # a same-attempt request that lost the race answers the first ticket BEFORE any route-state or ceiling
            # check (an activation or a lowered ceiling between the two never refuses it)
            again = store.replay(attempt, facts)
            if again is not None:
                return {"ticket": again.ticket, "run_id": again.run_id}
            self._check_route_facts(facts)
            t = store.mint(attempt, facts, new_handle)
            return {"ticket": t.ticket, "run_id": t.run_id}

    def _await_keys_serving_head(self) -> None:
        """Waits, at most :data:`MINT_KEYS_WAIT_S`, until what keys stated or acked on the CURRENT attachment is
        CONTROL's head (R4 §2: a ticket is never minted for a generation keys does not serve); else
        ``keys-unavailable``. No route map at all is ``ticket-field:routemap_generation``. Holds no lock but the
        condition's while it waits, so the attach-time RELOAD (under the activation lock) can land meanwhile."""
        deadline = self._monotonic() + MINT_KEYS_WAIT_S
        with self._serves_cv:
            while True:
                head = self._routemap_head()
                if head is None:
                    raise Refused("ticket-field:routemap_generation", "no route-map generation is active")
                if self._keys_serves == dict(head):
                    return
                left = deadline - self._monotonic()
                if left <= 0:
                    raise Refused("keys-unavailable", "keys does not serve CONTROL's route-map head")
                self._serves_cv.wait(left)

    def _check_route_facts(self, facts: Mapping[str, object]) -> None:
        """The §3.5 table's VALUE half for an executor or planner ticket; the caller holds the activation lock.
        ``routemap_generation`` == the ACTIVE generation == CONTROL's head == what keys serves on the current
        attachment (else ``keys-unavailable``, retryable); ``route`` a dispatch-map id of that generation (a base
        map's passthrough or egress id is not one: ``ticket-field:route``); ``route_policy_digest`` == CONTROL's own
        digest of that entry's ``policy``; ``model_requested`` one of that route's models; every ``egress_routes`` id
        a base-map route of type ``egress``."""
        active = self.routes.active() if self.routes is not None else None
        if active is None:
            raise Refused("ticket-field:routemap_generation", "no route-map generation is active")
        if facts["routemap_generation"] != active.generation:
            raise Refused("ticket-field:routemap_generation", f"the active generation is {active.generation}")
        with self._mu:
            serves = self._keys_serves
        if serves != {"generation": active.generation, "item_sha256": active.item_sha256}:
            raise Refused("keys-unavailable", "keys does not serve CONTROL's route-map head")
        entry = active.routes.get(facts["route"])
        if entry is None or entry.policy.type != "brokered":
            raise Refused("ticket-field:route", "not a brokered route of the active dispatch map")
        if not hmac.compare_digest(str(facts["route_policy_digest"]), active.digests[str(facts["route"])]):
            raise Refused("ticket-field:route_policy_digest")
        if facts["model_requested"] not in entry.backends:
            raise Refused("ticket-field:model_requested", "not one of the route's models")
        assert self.routes is not None
        types = routemaps.base_route_policy_types(self.routes.dir)   # the base map parsed whole (fix 2, codex 6)
        for rid in facts["egress_routes"]:            # type: ignore[union-attr]
            if types.get(rid) != "egress":
                raise Refused("ticket-field:egress_routes", f"{rid} is not a base-map egress route")

    def _op_run_ticket_cancel(self, conn: Conn, args: dict) -> dict[str, object]:
        """``RUN-TICKET-CANCEL {ticket}`` -> ``{}``: an UNBOUND ticket only (``ticket-bound`` once a lease bound it)."""
        self._ticket_store().cancel(_handle(args["ticket"], "ticket"))
        return {}

    def _op_run_ticket_status(self, conn: Conn, args: dict) -> dict[str, object]:
        """``RUN-TICKET-STATUS {attempt_id}`` -> ``{ticket, state, session?}`` from the journal's attempt index
        (``ticket-field:attempt_id`` outside the grammar, ``attempt-unknown`` for an attempt never minted)."""
        return self._ticket_store().status(T.check_attempt_id(args["attempt_id"]))

    def keys_attach_eof(self, conn: Conn) -> None:
        """The attachment's connection ended (:class:`ControlServer`): §4.5's "either EOF at CONTROL", unless a newer
        attachment already superseded it (whose KEYS-ATTACH ran the rule then)."""
        with self._mu:
            if self._attach is not conn:
                return
            self._attach = None
            self._keys_serves = None
            self._serves_cv.notify_all()
        self._attach_revoke()

    def _attach_revoke(self) -> None:
        """§4.5: keys' attachment ended, so keys has failed closed (no registration, no cached token); CONTROL revokes
        every egress-class live session (work retained) and closes the other of the two connections, keys' admin wire
        (whose EOF revokes the sessions registered on it, :meth:`_keys_eof`). The victims are read from memory, never
        settled from disk, and their handles dropped before any write; each is then revoked on its own worker."""
        if self._stopping.is_set():
            return
        if self.keys_admin is not None:
            self.keys_admin.abort()
        egress = {r.session for r in self.store.in_memory()
                  if r.state in R.LIVE_STATES and r.claims.get("class") == "egress"}
        with self._mu:
            victims = sorted(egress | set(self._reg_epoch))
            for session in victims:
                self._drop_handles(session)
        for session in victims:
            self._spawn_keys_revoke(session)

    def _require_attached(self, conn: Conn) -> None:
        with self._mu:
            if conn.attach_no is None or self._attach is not conn:
                raise Refused("not-attached")

    def _op_front_lost(self, conn: Conn, args: dict) -> dict[str, object]:
        """§4.5 ``FRONT-LOST {session}``: keys saw the session's front go (its STATUS connection ended without keys'
        ``K``, or its registration stayed unclaimed for V23 after ``FRONT-ARM``). CONTROL REVOKES the session (work
        retained): its handles are dropped now, and the REVOKED write runs on the session's revoke worker, which retries
        until it lands (a record already past live is left as it is). Acked ``{}`` at once; idempotent."""
        self._require_attached(conn)
        session = R.check_session(args["session"])
        with self._mu:
            self._drop_handles(session)
            self.front_lost.append(session)
        self._spawn_keys_revoke(session)
        return {}

    def _op_spend_overrun(self, conn: Conn, args: dict) -> dict[str, object]:
        """R4 §1.4 step 2 ``SPEND-OVERRUN {session, route, model, ref, overrun, unproven}``: a settle cost more than
        its hold, and keys has stopped the session itself. CONTROL RECORDS it, the data of the session's card row (R4
        §4), and does NOT revoke: a revoke aborts streams, which would leave the calls still in flight ``uncertain`` at
        their holds and lose their reported cost. Idempotent by ``(session, ref)``."""
        self._require_attached(conn)
        session = R.check_session(args["session"])
        route, model = args["route"], args["model"]
        for name, v in (("route", route), ("model", model)):
            if type(v) is not str:
                raise Refused("wrong-type", f"{name} is a string")
            if _NAME_RE.fullmatch(v) is None:
                raise Refused("bad-value", name)
        ref = args["ref"]
        if (type(ref) is not list or len(ref) != 3 or type(ref[0]) is not str or type(ref[1]) is not str
                or _HEX32_RE.fullmatch(ref[0]) is None or _HEX32_RE.fullmatch(ref[1]) is None):
            raise Refused("bad-value", "ref")
        R._uint(ref[2], "ref.seq")
        overrun = R._uint(args["overrun"], "overrun")
        if overrun == 0:
            raise Refused("bad-value", "overrun")
        unproven = args["unproven"]
        if type(unproven) is not bool:
            raise Refused("wrong-type", "unproven is a boolean")
        rec: dict[str, object] = {"session": session, "route": route, "model": model, "ref": list(ref),
                                  "overrun": overrun, "unproven": unproven}
        key = (session, (ref[0], ref[1], ref[2]))
        with self._mu:
            if key not in self._overruns:
                self._overruns[key] = rec
                while len(self._overruns) > SPEND_OVERRUNS_MAX:
                    self._overruns.popitem(last=False)
        return {}

    def _op_sessions(self, conn: Conn, args: dict) -> dict[str, object]:
        """3c design r3 §2.1 (e), the reconcile: ``SESSIONS {digests}``, the token digests keys holds a cached resolve
        for. CONTROL answers ``{drop}``: each digest whose session is not LIVE here (no handles for it, which covers a
        revoke whose DROP-SESSION never reached keys and a token CONTROL forgot; a record missing, past live, or that
        cannot be read now). keys treats each exactly as a DROP-SESSION."""
        self._require_attached(conn)
        digests = args["digests"]
        if type(digests) is not list:
            raise Refused("wrong-type", "digests is a list")
        if len(digests) > SESSIONS_DIGESTS_MAX:
            raise Refused("out-of-range", "digests")
        for d in digests:
            if type(d) is not str or _DIGEST_RE.fullmatch(d) is None:
                raise Refused("bad-token", "digests")
        with self._mu:
            by_digest = {h.token_digest: (s, h) for s, h in self._handles.items() if h.token_digest is not None}
        drop = []
        for d in sorted(set(digests)):
            found = by_digest.get(d)
            if found is None:
                drop.append(d)
                continue
            session, handles = found
            try:
                rec = self.store.get(session)
            except Refused:
                rec = None                               # cannot be confirmed live: the safe answer is drop
            with self._mu:
                if rec is None or rec.state not in R.LIVE_STATES or self._handles.get(session) is not handles:
                    drop.append(d)
        return {"drop": drop}

    def _op_lineage_append(self, conn: Conn, args: dict) -> dict[str, object]:
        """S1 3c-6b, CONTRACT §3.3 keys row ``LINEAGE-APPEND {envelope}`` (H6-2, H6-11, H6-14; SCHEMAS §2.1), on the
        current attachment only (``not-attached``). The envelope ``{ledger_id, epoch, records}`` is checked FIRST and
        whole (:func:`levain.slot.lineage.check_envelope`): any fault, including an element whose ``source`` names
        another ledger or epoch than the envelope, refuses it ``lineage-record:envelope`` and nothing is appended (a keys
        bug). Inside a valid envelope each record is checked by field name and type (:func:`~levain.slot.lineage.
        check_record`); one that fails becomes a QUARANTINE row naming its first bad field (R4 §3), so it is acked and
        the outbox drains. Every triple the sink already holds is acked without appending (membership, never a
        high-water); the rest are appended in ONE write with ONE full fsync (group commit) before this answers.

        Answers ``{acked: [{ledger, epoch, ref}..], quarantined: [{source: {ledger, epoch, ref}, field}..]}``: every
        triple of the envelope once, in envelope order, and among them each one the sink holds as a quarantine row,
        a member's included (a resend after a lost ack learns its quarantine again) (DECISION: the reply mirrors the
        ``lineage-acked`` line's H6-17 shape). No sink: ``lineage-unavailable``; a failed append poisons the sink and
        answers ``lineage-unavailable`` until CONTROL restarts.

        DECISION: a quarantine row's ``session`` is ``null``: the envelope and the ``/3`` record carry no session
        (routed to the head as an observation, 3c-6b RETURN)."""
        self._require_attached(conn)
        envelope = args["envelope"]
        triples = LN.check_envelope(envelope)
        if self.lineage is None:
            raise Refused("lineage-unavailable", "CONTROL holds no lineage sink")
        rows: list[tuple[LN.Triple, dict[str, object]]] = []
        for t, rec in zip(triples, envelope["records"]):
            bad = LN.check_record(rec)
            if bad is None:
                rows.append((t, {k: rec[k] for k in LN.RECORD_CHECKS}))
            else:
                rows.append((t, {"format": LN.FORMAT, "quarantined": bad, "session": None}))
        acked: list[dict[str, object]] = []
        quarantined: list[dict[str, object]] = []
        seen: set[LN.Triple] = set()
        for t, bad in self.lineage.append(rows):
            if t in seen:
                continue
            seen.add(t)
            acked.append(LN.source_of(t))
            if bad is not None:
                quarantined.append({"source": LN.source_of(t), "field": bad})
        return {"acked": acked, "quarantined": quarantined}

    # -- test / service views --------------------------------------------------------------------------------

    def handles(self, session: str) -> Handles | None:
        with self._mu:
            return self._handles.get(session)

    def keys_attached(self) -> int | None:
        """The current attachment's number, or ``None`` while keys is not attached."""
        with self._mu:
            return None if self._attach is None else self._attach.attach_no

    def keys_serves(self) -> dict[str, object] | None:
        """The route-map generation keys stated (or acked) on the current attachment (R4 §2), ``None`` for none."""
        with self._mu:
            return None if self._keys_serves is None else dict(self._keys_serves)

    def spend_overruns(self, session: str | None = None) -> list[dict[str, object]]:
        """The SPEND-OVERRUN records CONTROL holds (R4 §4's card data), oldest first; ``session`` filters."""
        with self._mu:
            return [dict(r) for (s, _), r in self._overruns.items() if session is None or s == session]


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
        conn = Conn(uid, sock=sock)
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
                while conn.after:                        # once the answer is on the wire (R4 §10)
                    act = conn.after.pop(0)
                    try:
                        act()
                    except Exception as e:   # noqa: BLE001 - an after-act's failure is noted, never the connection's
                        self.control.seam_errors.append(("after", "", e))
        finally:
            # The lease dies before the socket closes, so a peer that sees the close sees a dead lease; likewise the
            # attachment (§4.5: its EOF revokes every egress-class session).
            if conn.lease is not None and not self._stop.is_set():
                self.control.lease_eof(conn.lease)
            if conn.attach_no is not None and not self._stop.is_set():
                self.control.keys_attach_eof(conn)
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


# --- the program's one entry -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class ServeConfig:
    """What :func:`serve` runs CONTROL from; the CONTROL program (3c-9) fills it from its argv. ``keys_admin_path`` is
    keys' ``R/keys/admin.sock`` (``None``: no keys client, every keys act refuses ``keys-unavailable``). The last three
    are seams for host tests: ``peer_uid`` replaces the kernel's peer read, ``ready(server)`` runs once the start pass
    is done and the socket listens, and ``stop`` ends :func:`serve` when set."""

    store_dir: Path
    socket_path: Path
    operator_uid: int
    keys_admin_path: Path | None = None
    keys_uid: int | None = None
    qbroker_uid: int | None = None
    #: ``L/routes`` (trust, base map, the dispatch route maps) and the ``_levain_dispatch`` peer: S1 3c-4
    routes_dir: Path | None = None
    dispatch_uid: int | None = None
    #: ``L/policy/dispatch_ceiling`` (S1 3c-5); ``None`` with a ``routes_dir``: ``<routes_dir>/../policy/dispatch_ceiling``.
    #: The ticket journal is ``<store_dir>/tickets.jsonl``, kept whenever there is a route store.
    ceiling_path: Path | None = None
    #: the ceiling file's required owner (root; a test rig passes its own uid)
    ceiling_uid: int = 0
    #: ``L/lineage`` (S1 3c-6b): CONTROL's lineage sink is ``<lineage_dir>/lineage.jsonl``; ``None``: no sink, and
    #: ``LINEAGE-APPEND`` is refused ``lineage-unavailable``
    lineage_dir: Path | None = None
    host: str = "darwin" if sys.platform == "darwin" else "linux"
    peer_uid: Callable[[socket.socket], int | None] | None = None
    ready: Callable[[ControlServer], object] | None = None
    stop: threading.Event | None = None


def serve(config: ServeConfig) -> int:
    """CONTROL as one call: the store, keys' admin client, :class:`Control`, its server; the start pass (every live
    record revoked, every owed drop re-sent) before the socket listens; then served until SIGTERM or SIGINT (handled
    only when called on the main thread) or ``config.stop``; then stopped the way a crash looks to every session (no
    revoke: the next start revokes them). Returns 0."""
    store = StateStore(Path(config.store_dir))
    admin = KeysAdmin(config.keys_admin_path) if config.keys_admin_path is not None else None
    routes = routemaps.ControlRoutes(Path(config.routes_dir)) if config.routes_dir is not None else None
    tickets = None
    if config.routes_dir is not None:
        ceiling = config.ceiling_path or Path(config.routes_dir).parent / "policy" / "dispatch_ceiling"
        tickets = T.TicketStore(Path(config.store_dir) / T.JOURNAL, Path(ceiling), ceiling_uid=config.ceiling_uid)
    lineage = LN.LineageSink(Path(config.lineage_dir) / LN.SINK) if config.lineage_dir is not None else None
    control = Control(store, config.operator_uid, qbroker_uid=config.qbroker_uid, keys_uid=config.keys_uid,
                      keys_admin=admin, host=config.host, routes=routes, dispatch_uid=config.dispatch_uid,
                      tickets=tickets, lineage=lineage)
    if config.peer_uid is not None:
        server = ControlServer(control, Path(config.socket_path), peer_uid=config.peer_uid)
    else:
        server = ControlServer(control, Path(config.socket_path))
    stop = config.stop if config.stop is not None else threading.Event()
    restore: list[tuple[int, object]] = []
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            restore.append((sig, signal.signal(sig, lambda *_a: stop.set())))
    try:
        try:
            server.start()
        except Refused as e:
            # 3c-5 fix 2: a refused start (``control-running``: another CONTROL holds the ticket journal) is logged
            # by name; nothing was served and the other CONTROL's socket is untouched
            print(f"levain CONTROL: start refused: {e.name}", file=sys.stderr, flush=True)
            raise
        if config.ready is not None:
            config.ready(server)
        while not stop.wait(0.5):
            pass
    finally:
        server.stop()
        for sig, old in restore:
            signal.signal(sig, old)   # type: ignore[arg-type]
    return 0


__all__ = ["AdminRefused", "CONTROL_FORMAT", "Conn", "Control", "ControlServer", "FRAME_MAX", "FRAME_READ_S",
           "FRONT_REG_DIR", "FrameMalformed", "FrameTooLarge", "Handles", "KEYS_ANSWER_S", "KEYS_RECONNECT_FIRST_S",
           "KEYS_RECONNECT_MAX_S", "KeysAdmin", "Lease", "MAX_CONNECTIONS", "PEER_VERBS", "RETIRED_LEASES_MAX",
           "REVOKED_TOKENS_MAX", "SESSIONS_DIGESTS_MAX", "SPEND_OVERRUNS_MAX", "ServeConfig", "StateStore",
           "check_request", "digest_of", "encode_frame", "new_handle", "ok", "read_frame", "refused", "serve",
           "try_lock"]
