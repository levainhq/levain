"""gw-keys (S1 session 3b): the credential side of the gateway, run as `_levain_keys`. This tip holds WHO MAY CONNECT
(design 1.5, session 3b-3a): keys' `R/keys/front.sock` server, the registration table, the two admission pools, the
`F` path, the NOFILE floor, the process entry and its one shutdown trigger; and WHO MAY ASK (session 3b-3b): the admin
server on `R/keys/admin.sock`; and the `Q` handler: its frame, token states and request path (the route against the
token's claims, keys' own policy load, the re-checks, the 3c seams at their refusing defaults, the credential, and the
request through `Upstream.write`); and the UPSTREAM half (session 3b-4): the address check, TLS through `upstream.py`,
the h11 request and response, the relay `R` `b`* `e` through the redaction scanner, and keys' log `gw.jsonl` (one
record per exchange, plus the `levain.broker` notes).

The admin server (design 1.5 "The admin connection", "Fencing", "Order inside one connection"):
- a connection is accepted only from the `_levain_control` uid, read from the kernel; any other peer is closed unread.
- framing and answer shape are `levain-control/1`'s: a 4-byte big-endian length and one canonical JSON object,
  `{"v":1,"op","id",...}` in, `{"id","ok":true,...}` or `{"id","ok":false,"refused",...}` out. A frame that is not
  one closes the connection, which is its EOF.
- each request runs as its own task carrying the admin epoch of its connection, so answers go out in any order; two
  requests naming one session run in frame order, the order assigned in the read loop with no `await` between the
  read and the append to that session's chain.
- FRONT-REGISTER / FRONT-ARM / DROP-SESSION are the fenced table calls; RELOAD is refused until a dispatch map exists.
- a newer connection PREEMPTS in five steps: bump the epoch, fence every live exchange (marked detached, so its next
  upstream write is refused) and stop reading the old one; cancel its request tasks and
  await them, bounded; the fail-closed clear in two phases (`reg_high` kept); close the old connection; adopt.
- the adopted connection's EOF fails closed: the epoch is bumped (its in-flight tasks are stale from that instant) and
  every registration is detached, `K` sent on each STATUS connection.

Who may connect, decided at accept, after `peer.peer_of` and before the first frame:
1. the peer's uid must be a `_levain_gw<n>` uid and its `(uid, pid, start)` must name a registration, else the
   connection is closed with no frame (an unregistered peer learns nothing).
2. the STATUS pool: a registration that is ARMED and holds no STATUS candidate makes the connection its candidate; its
   first frame must be `H` naming the registration's session and incarnation, and keys answers `A` (CLAIMED). Never
   counted against anything else, so a registered front's STATUS connection is never refused for room.
3. the exchange pool: a CLAIMED registration makes the connection an exchange candidate; its first frame must be `Q`.
   At most PENDING_PER_PEER candidates per peer that have sent nothing yet and MAX_EXCHANGE_CONNS exchange
   connections in all (both closed at accept), and EXCHANGES_PER_SESSION live exchanges per session (`F`).
4. FIRST_FRAME_S from accept for the first frame, both pools.
Any other first frame, or any frame that does not decode exactly, closes the connection with no frame.

Registrations are FENCED (design 1.5 "Fencing"): one per `front_uid`, replaced only by a greater `reg_token`; an
equal token with every field equal is an idempotent retry; anything else is `registration-stale` carrying `reg_high`.
Every mutation of the table runs under `state_lock` with no `await` inside, and an admin request's checks the admin
epoch first (the one exception: a closing connection leaving its sets, idempotent discards with no `await`). Every
transition is two-phase: phase 1, under the lock, detaches the victims from every table; phase 2, a synchronous loop
right after it, sends `K` on a claimed STATUS connection and closes each victim (aborted ABORT_AFTER_S later).

Runs on the root interpreter (3.9): every lock and event is made inside the running loop.
"""
from __future__ import annotations

import asyncio
import datetime
import errno
import functools
import json
import logging
import os
import pwd
import re
import resource
import signal
import socket
import stat
import sys
import time
from collections import OrderedDict
from typing import Awaitable, Dict, List, Optional, Sequence, Set, Tuple, TypeVar, Union

import h11

from levain.broker import gwwire, norm, upstream
from levain.broker.creds import CredLifecycle, CredStore, FakeCredLifecycle, check_host, on_upstream_401
from levain.broker.gwwire import FrameReader, KeysSide, WireError
from levain.broker.peer import PeerUnknown, peer_of, peer_uid
from levain.broker.policy import PolicyError, RoutePolicy, parse_route
from levain.broker.redact import Scanner
from levain.broker.upstream import (GRACE, StaleExchange, Upstream, _note, h11_bytes, open_exchange,
                                    pick_address)
from levain.contracts import ContractError, canon
from levain.contracts.grammar import JSON_INT_MAX

T = TypeVar("T")

# --- the admission values (DECISION, judged against the 8-slot default; one constant each) ------------------------
FIRST_FRAME_S = 5.0
PENDING_PER_PEER = 8
EXCHANGES_PER_SESSION = 32
MAX_EXCHANGE_CONNS = 512
#: the `_levain_gw<n>` index space: at most one STATUS connection per registered front.
STATUS_CONNS = 64
RESOLVE_CONNS = 32
REFRESH_CONNS = 8
DNS_CONCURRENCY = 32
#: fds one in-flight `getaddrinfo` of an uncached name holds [measured 2026-10-22, macOS 26.6, 32 concurrent lookups
#: of distinct uncached names: 36 extra fds under 3.9.6, 35 under 3.12.13, so 2 per lookup rounded up]. Linux is
#: unmeasured: its VM run prints its own.
DNS_FDS_PER_LOOKUP = 2
#: the second admin connection during a preemption, the commit's temp file, the refresh's response, the credential read.
TRANSIENT_NAMED = 4
#: passed to listen() and to every start_unix_server, never asyncio's default of 100: asyncio accepts up to this many
#: connections per wakeup before admission closes any of them.
ACCEPT_BACKLOG = 8
ADMIN_CONNS = 1
NOFILE_TARGET = 4096

#: The fixed terms of the NOFILE floor: each is the value its own cap or semaphore is built from.
NOFILE_TERMS = {
    "exchange": MAX_EXCHANGE_CONNS * 2,   # front side + upstream
    "status": STATUS_CONNS,
    "resolve": RESOLVE_CONNS,
    "refresh": REFRESH_CONNS,
    "dns": DNS_CONCURRENCY * DNS_FDS_PER_LOOKUP,
    "transient": TRANSIENT_NAMED + 2 * ACCEPT_BACKLOG,
    "admin": ADMIN_CONNS,
}

#: the `F` path's drain: every `F` is followed by SHUT_WR, then the front's frames are read and discarded until `E`,
#: `X`, EOF, DRAIN_S, or this many body bytes read AFTER the `F` (counted from zero there).
DRAIN_S = 5.0
#: the drain cap of a refusal made before the route is trusted (no route policy, hence no `body_cap`, is known yet).
#: Policy bounds no `body_cap` today; DRAIN_S bounds the drain whatever this is.
UNTRUSTED_DRAIN_CAP = 64 * 1024 * 1024
#: phase 2: a victim closed now is aborted this long after, whether or not the front read its `K`.
ABORT_AFTER_S = 2.0
#: shutdown's awaits (the listeners' closure and the detached exchange tasks) share ONE deadline.
SHUTDOWN_WAIT_S = 3.0

#: resolves one registration may have in flight inside the global RESOLVE_CONNS (DECISION, judged: a session's
#: legitimate first burst is a handful of parallel requests, and the per-digest single-flight folds repeats).
RESOLVE_PER_SESSION = 4
#: ONE deadline per TOKEN-RESOLVE, from the `Q` that wanted it: both permits, the connect and the answer.
RESOLVE_S = 5.0
#: digests keys itself saw dropped, answered `token-revoked` without asking (oldest evicted first).
DROPPED_MAX = 64 * 1024
#: a session token as the front sends it: 43 base64url characters (the `Bearer` value).
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
#: CONTROL's TOKEN-RESOLVE refusals keys maps, and the name each maps to.
RESOLVE_REFUSALS = {"token-unknown": "token-invalid", "token-revoked": "token-revoked"}

#: preemption's step (2): the old connection's request tasks are awaited this long after their cancel (DECISION,
#: judged: a task's work is memory operations plus closing sockets); one still running is left to its epoch check.
PREEMPT_WAIT_S = 2.0
#: `levain-control/1`'s frame cap, which the admin wire shares.
ADMIN_FRAME_MAX = 64 * 1024
#: once an admin frame's first byte is in, the rest of its length and its body must arrive within this, and an
#: answer's write must drain within it (the connection idles legitimately only BETWEEN frames).
ADMIN_FRAME_S = 10.0
#: request tasks one admin connection may hold at once; one more closes the connection, which is its EOF (DECISION,
#: judged: well above a legitimate CONTROL's concurrency, its retransmits included).
ADMIN_INFLIGHT_MAX = 1024
#: the admin verbs and their argument keys, exactly; every argument but `session` is an unsigned integer.
ADMIN_VERBS = {
    "FRONT-REGISTER": frozenset({"session", "front_uid", "pid", "start", "incarnation", "reg_token"}),
    "FRONT-ARM": frozenset({"session", "reg_token"}),
    "DROP-SESSION": frozenset({"session"}),
    "RELOAD": frozenset({"generation"}),
}
#: RELOAD's answer until keys serves a dispatch map: there is no generation it could now serve.
RELOAD_REFUSED = "reload-unsupported"

GW_ACCOUNT = "_levain_gw{}"
KEYS_ACCOUNT = "_levain_keys"
CONTROL_ACCOUNT = "_levain_control"
FRONTS_GROUP = "levain-gwfronts"
#: admin.sock's group: a shared group whose only members are keys and CONTROL, so no other account can connect (and
#: the peer uid check admits CONTROL alone). keys binds the socket as itself, so it must be a member for the chown to
#: succeed; a primary group stays its own account's only, hence a shared group and not CONTROL's.
ADMIN_GROUP = "levain-keysadmin"

_K = gwwire.encode(b"K")
_A = gwwire.encode(b"A")

#: a brokered route's upstream is `https` (policy refuses any other scheme) and names no port
UPSTREAM_PORT = 443
#: CONTRACT §4.3's base route map, `L/routes/base.json`: its format, and the size keys reads at most (DECISION: far
#: above a real map; a larger file is refused unread past the cap)
BASE_MAP_FORMAT = "levain-routes-base/1"
BASE_MAP_CAP = 1024 * 1024

#: an upstream redirect is never followed: `upstream-redirect` (design 1.6); other 3xx (304) pass
REDIRECTS = frozenset({301, 302, 303, 307, 308})
#: the largest `b` frame keys writes (the wire's cap for `b`)
BODY_FRAME = gwwire.CAPS[b"b"]
#: a header value the wire carries: visible ASCII and tab
_HVALUE = re.compile(r"[\t\x20-\x7e]*\Z")
#: keys' log, under the lib root (design 1.9): one JSON line per exchange and per diagnostic note
GW_LOG = ("keys", "log", "gw.jsonl")


def _at(t: float) -> str:
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def new_record(session: str, q: dict) -> dict:
    """An exchange's gw.jsonl record as admission makes it: names and counts only, never a value of the request."""
    return {"session": session, "slot": None, "route": q["route"], "method": q["method"], "template": None,
            "status": None, "refused": None, "bytes_up": 0, "bytes_down": 0, "upstream_address": None,
            "redactions": 0, "t0": time.monotonic()}


class GwLog:
    """`L/keys/log/gw.jsonl`: created 0600 in a 0700 directory, opened once (append, no symlink followed) before the
    start log counts keys' fds. A write that fails is reported once on stderr and never raises."""

    def __init__(self, lib_root: str) -> None:
        d = os.path.join(lib_root, *GW_LOG[:-1])
        os.makedirs(d, mode=0o700, exist_ok=True)
        self.path = os.path.join(d, GW_LOG[-1])
        self.fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        self._failed = False

    def write(self, obj: dict) -> None:
        try:
            os.write(self.fd, (json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
        except Exception as e:
            if not self._failed:
                self._failed = True
                try:
                    print(f"levain gw-keys: gw.jsonl write failed ({type(e).__name__})", file=sys.stderr, flush=True)
                except Exception:
                    pass


class GwLogHandler(logging.Handler):
    """The `levain.broker` logger's handler: each diagnostic note (every one is written by `upstream._note`, from
    names and counts only) becomes a `note` record in gw.jsonl."""

    def __init__(self, log: GwLog) -> None:
        super().__init__(logging.INFO)
        self.log = log

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.log.write({"at": _at(record.created), "kind": "note", "level": record.levelname,
                            "msg": record.getMessage()})
        except Exception:
            self.handleError(record)


def install_log(lib_root: str) -> GwLog:
    """keys' logging, once, in the process entry: the gw.jsonl handler on `levain.broker`, and stderr kept for
    warnings (the unit's own log), which is where the notes went before a handler existed."""
    log = GwLog(lib_root)
    lg = logging.getLogger("levain.broker")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    lg.addHandler(GwLogHandler(log))
    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.WARNING)
    lg.addHandler(err)
    return log


# --- the seams 3c fills (design 1.11), with their refusing production defaults ------------------------------------

class RefusingBudget:
    """`budget`'s production default until 3c's ledger: every reservation is refused `budget-exhausted`.

    The seam is two calls: `reserve` (the request check's last step; refusing raises `norm.Refused`) and `release`,
    which keys calls exactly once for every reservation `reserve` made, when its exchange ends without a relayed
    response to settle it. `sent` says whether the request head was handed to the upstream connection by then (it
    may have been processed, so a ledger may hold what it would otherwise free)."""

    def reserve(self, session: str, route: str, max_call_microdollars: Optional[int]) -> None:
        raise norm.Refused("budget-exhausted", "no ledger")

    def release(self, session: str, route: str, max_call_microdollars: Optional[int], sent: bool) -> None:
        return None


class UnmeteredBudget:
    """`--unmetered` (3b's fixture and VM runs only; 3c deletes the flag): every reservation is admitted."""

    def reserve(self, session: str, route: str, max_call_microdollars: Optional[int]) -> None:
        return None

    def release(self, session: str, route: str, max_call_microdollars: Optional[int], sent: bool) -> None:
        return None


def no_dispatch_route(route: str) -> Optional[RoutePolicy]:
    """`dispatch_policy`'s production default until 3c: no dispatch map is loaded (RELOAD is refused), so it knows
    no route, and a dispatch-map id is `route-not-allowed`."""
    return None


class NoCodec:
    """`codec`'s production default until 3c: no codec exists, so a body passes through untouched and a ticketed
    session's `Q` (its claims carry a `ticket`, a run) is refused `model-not-allowed`: no run goes out with an
    unchecked model."""

    def check(self, run: object, route: RoutePolicy) -> None:
        if run is not None:
            raise norm.Refused("model-not-allowed", "no codec")


#: what an upstream connection's failure raises, each answered `upstream-error` (`upstream-timeout` for a deadline):
#: a refused or reset connect, a TLS failure (`ssl.SSLError` is an OSError), a write or drain on a lost connection,
#: and a seam that is not built yet. `StaleExchange` is none of them: a detached exchange still ends with no answer.
UPSTREAM_FAILURES = (OSError, EOFError, asyncio.TimeoutError, NotImplementedError)


class UpstreamFailed(Exception):
    """An upstream failure translated to the refusal the front is answered: `name` is `upstream-error` or
    `upstream-timeout`."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


async def upstream_step(aw: Awaitable[T]) -> T:
    """Await one upstream step; an UPSTREAM_FAILURES raise becomes UpstreamFailed (noted), anything else, a
    StaleExchange or a cancel included, propagates unchanged."""
    try:
        return await aw
    except UPSTREAM_FAILURES as e:
        # on 3.11+ asyncio's TimeoutError IS the builtin one, an OSError: the deadline is told apart first
        timeout = isinstance(e, (asyncio.TimeoutError, TimeoutError))
        _note(logging.WARNING, "levain gw-keys: upstream failed (%s%s)", type(e).__name__,
              f": {e.detail}" if isinstance(e, upstream.BadResponse) else "")
        raise UpstreamFailed("upstream-timeout" if timeout else "upstream-error") from None


async def upstream_send(up: Upstream, data: bytes) -> None:
    """`up.write(data)` (the gate) and its drain, as one upstream step."""
    async def send() -> None:
        up.write(data)
        await up.drain()
    await upstream_step(send())


def bounded() -> int:
    return sum(NOFILE_TERMS.values())


# --- the table ------------------------------------------------------------------------------------------------------

class Registration:
    """One front: FRONT-REGISTER's fields, and its admission state REGISTERED -> ARMED -> CLAIMED (-> LOST when its
    STATUS connection ends without keys' `K`)."""

    __slots__ = ("session", "front_uid", "pid", "start", "incarnation", "reg_token", "state", "armed_at",
                 "status", "pending", "exchanges", "resolve_sem", "admin_epoch")

    def __init__(self, session: str, front_uid: int, pid: int, start: int, incarnation: int, reg_token: int) -> None:
        self.session, self.front_uid, self.pid, self.start = session, front_uid, pid, start
        self.incarnation, self.reg_token = incarnation, reg_token
        self.state = "registered"
        self.armed_at: Optional[float] = None
        self.status: Optional[Conn] = None          # the STATUS candidate, then the STATUS connection
        self.pending: Set[Conn] = set()             # exchange candidates that have sent no frame yet
        self.exchanges: Set[Conn] = set()           # connections past `Q`
        #: this registration's share of the resolve slots, made at its first resolve (inside the loop)
        self.resolve_sem: Optional[asyncio.Semaphore] = None
        #: the admin epoch FRONT-REGISTER made it under: an exchange is admitted only while that epoch is current
        self.admin_epoch: Optional[int] = None

    def fields(self) -> tuple:
        return (self.session, self.front_uid, self.pid, self.start, self.incarnation)


class Conn:
    """One accepted connection attributed to a registration. A connection past `Q` IS the exchange (design 1.2's
    `exchange`, 1.5's member of a registration's exchange set): `reg` is the sketch's `exchange.registration`, and
    `upstream` holds the exchange's `Upstream` from `open_exchange`'s store until the release drops it, so a phase 1
    always finds what it must abort."""

    __slots__ = ("writer", "reg", "kind", "task", "detached", "upstream", "admin_epoch", "rec")

    def __init__(self, writer: asyncio.StreamWriter, reg: Registration, kind: str) -> None:
        self.writer, self.reg, self.kind = writer, reg, kind   # kind: candidate | status | pending | exchange
        self.task: Optional[asyncio.Task] = asyncio.current_task()
        self.detached = False
        self.upstream: Optional[Upstream] = None
        #: the admin epoch stamped at `Q`, under state_lock: the exchange's mutations and its resolve key compare it
        self.admin_epoch: Optional[int] = None
        #: the exchange's gw.jsonl record, made at admission and written at its end (None before `Q` is admitted)
        self.rec: Optional[dict] = None


#: one entry of a phase 1's detached-objects list: a connection to close, or a detached exchange's `Upstream` to abort
Victim = Union[Conn, Upstream]


class AdminConn:
    """One accepted admin connection: the epoch it was adopted on, its read loop's task, its request tasks, and per
    session the last request task read for that session (the tail of that session's chain)."""

    __slots__ = ("reader", "writer", "epoch", "task", "tasks", "last", "preempted", "wlock")

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, epoch: int) -> None:
        self.reader, self.writer, self.epoch = reader, writer, epoch
        self.task: Optional[asyncio.Task] = asyncio.current_task()
        self.tasks: Set[asyncio.Task] = set()
        self.last: Dict[str, asyncio.Task] = {}
        #: set by the preemption that replaces this connection, before it cancels the read loop: the EOF path then
        #: does nothing, and the preemption closes the connection itself, after its clear.
        self.preempted = False
        #: one answer is written and drained at a time (a concurrent drain is not allowed on 3.9's StreamWriter).
        self.wlock = asyncio.Lock()


class AdminMalformed(Exception):
    """An admin frame that is not a `levain-control/1` request: the connection is closed, which is its EOF."""


async def read_admin_frame(reader: asyncio.StreamReader) -> Optional[Tuple[int, str, dict]]:
    """The next request as `(id, op, args)`, or None at a clean EOF between frames; anything else that is not a
    canonical `{"v":1,"op":<string>,"id":<uint>,...}` object within ADMIN_FRAME_MAX raises AdminMalformed."""
    try:
        first = await reader.readexactly(1)
    except asyncio.IncompleteReadError:
        return None
    # from the frame's first byte, ONE deadline covers the rest of its length and its body
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ADMIN_FRAME_S
    try:
        head = first + await asyncio.wait_for(reader.readexactly(3), ADMIN_FRAME_S)
        n = int.from_bytes(head, "big")
        if n == 0 or n > ADMIN_FRAME_MAX:
            raise AdminMalformed(f"frame length {n}")
        data = await asyncio.wait_for(reader.readexactly(n), max(0.0, deadline - loop.time()))
    except (asyncio.IncompleteReadError, asyncio.TimeoutError):
        raise AdminMalformed("a frame cut short or stalled") from None
    try:
        obj = canon.parse(data)
    except ContractError as e:
        raise AdminMalformed(str(e)) from None
    if type(obj) is not dict or canon.canonical_bytes(obj) != data:
        raise AdminMalformed("not a canonical JSON object")
    v, op, rid = obj.get("v"), obj.get("op"), obj.get("id")
    if type(v) is not int or v != 1 or type(op) is not str or not op:
        raise AdminMalformed("v is 1 and op is a string")
    if type(rid) is not int or not 0 <= rid <= JSON_INT_MAX:
        raise AdminMalformed("id is an unsigned integer")
    return rid, op, {k: x for k, x in obj.items() if k not in ("v", "op", "id")}


async def read_control_answer(reader: asyncio.StreamReader) -> dict:
    """One `levain-control/1` answer from CONTROL: a 4-byte length within ADMIN_FRAME_MAX and one canonical JSON
    object. Anything else raises (the resolve's outcome is then `failed`)."""
    n = int.from_bytes(await reader.readexactly(4), "big")
    if n == 0 or n > ADMIN_FRAME_MAX:
        raise AdminMalformed(f"answer length {n}")
    data = await reader.readexactly(n)
    obj = canon.parse(data)
    if type(obj) is not dict:
        raise AdminMalformed("not a JSON object")
    return obj


def _resolve_done(t: asyncio.Task) -> None:
    """Reads the resolve task's outcome so none is left unretrieved; `cancelled()` first, since a preemption's
    cancel makes a cancelled resolve common and `exception()` on one raises."""
    if not t.cancelled():
        t.exception()


def _same_uint(v: object, want: int) -> bool:
    return type(v) is int and v == want


def front_binding(answer: dict, reg: "Registration") -> bool:
    """A TOKEN-RESOLVE answer binds to `reg` only when its session is `reg`'s and its claims name `reg`'s front
    exactly: CONTROL's persisted claims carry `front: {uid, pid, start}` and `incarnation`, and each must be present,
    an integer, and equal to the registration's. A missing or mistyped field fails the binding, never defaults to the
    registration's own value."""
    claims = answer.get("claims")
    front = claims.get("front") if type(claims) is dict else None
    return (answer.get("session") == reg.session and type(front) is dict
            and _same_uint(front.get("uid"), reg.front_uid) and _same_uint(front.get("pid"), reg.pid)
            and _same_uint(front.get("start"), reg.start)
            and _same_uint(claims.get("incarnation"), reg.incarnation))


def encode_admin(answer: dict) -> bytes:
    data = canon.canonical_bytes(answer)
    return len(data).to_bytes(4, "big") + data


def check_admin_args(op: str, args: dict) -> Optional[str]:
    """The refusal name for `op`'s arguments, or None when they are exactly its keys with the right types."""
    want = ADMIN_VERBS.get(op)
    if want is None:
        return "verb-not-allowed-for-peer"
    if any(k not in want for k in args):
        return "unknown-key"
    if any(k not in args for k in want):
        return "missing-key"
    for k, x in args.items():
        if k == "session":
            if type(x) is not str or not x:
                return "wrong-type"
        elif type(x) is not int or not 0 <= x <= JSON_INT_MAX:
            return "wrong-type"
    return None


# --- the listeners --------------------------------------------------------------------------------------------------

def bind_listeners(specs: Sequence[Tuple[str, Optional[int]]]) -> List[socket.socket]:
    """Create, bind and listen keys' listening sockets, one per `(path, gid)`, WITHOUT registering them with the loop
    (the NOFILE count comes between this and the first accept). Each is created 0660, then given `gid` when it is
    not None. A path that exists is replaced only when it is a socket that REFUSES a connect (stale: nobody listens);
    any other file, or a socket that may be live, raises FileExistsError. On any failure every socket made here is
    closed and every path bound here is unlinked, so a failed start leaves no listener path behind."""
    out: List[socket.socket] = []
    bound: List[str] = []
    try:
        for path, gid in specs:
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                st = None
            if st is not None:
                if not stat.S_ISSOCK(st.st_mode):
                    raise FileExistsError(f"{path}: not a socket")
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.setblocking(False)   # a live listener's full backlog must not block start
                try:
                    err = probe.connect_ex(path)
                finally:
                    probe.close()
                if err != errno.ECONNREFUSED:
                    why = errno.errorcode.get(err, err) if err else "listening"
                    raise FileExistsError(f"{path}: in use or unprobeable ({why})")
                os.unlink(path)
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            out.append(s)
            old = os.umask(0o117)   # created 0660: no window at a wider mode
            try:
                s.bind(path)
            finally:
                os.umask(old)
            bound.append(path)
            if gid is not None:
                os.chown(path, -1, gid)
            s.listen(ACCEPT_BACKLOG)
    except BaseException:
        for s in out:
            s.close()
        for path in bound:
            try:
                os.unlink(path)
            except OSError:
                pass
        raise
    return out


def count_open_fds() -> int:
    """The fds this process holds now, less the one the listing itself opens."""
    d = "/proc/self/fd" if sys.platform.startswith("linux") else "/dev/fd"
    return len(os.listdir(d)) - 1


def raise_nofile() -> Tuple[int, int]:
    """Raise the soft RLIMIT_NOFILE to min(hard, NOFILE_TARGET) (never lower it); return (soft, hard) after."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = NOFILE_TARGET if hard == resource.RLIM_INFINITY else min(hard, NOFILE_TARGET)
    if soft != resource.RLIM_INFINITY and soft < want:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
        except (ValueError, OSError) as e:
            _note(logging.WARNING, "levain gw-keys: RLIMIT_NOFILE not raised to %s (%s)", want, e)
    return resource.getrlimit(resource.RLIMIT_NOFILE)


# --- the server -----------------------------------------------------------------------------------------------------

class Keys:
    """keys' connection half. The in-process API (`front_register`, `front_arm`, `drop_session`) is what the admin
    server's request tasks call, each passing the admin epoch its connection was adopted on; a call from a stale
    epoch, or one made after the shutdown latch (`closing`) is set, returns None and changes nothing (no answer is
    sent).

    `closing` is the ONE shutdown state: set synchronously by the trigger, never cleared. From that instant admission
    refuses at accept, at the STATUS claim (`H`) and at `Q`, and every in-process API call refuses."""

    def __init__(self, *, gw_uids: frozenset = frozenset(), single_uid: Optional[int] = None,
                 control_path: Optional[str] = None, lib_root: Optional[str] = None,
                 cred_lifecycle: Optional[CredLifecycle] = None, control_uid: Optional[int] = None,
                 unmetered: bool = False, budget: object = None, dispatch_policy: object = None,
                 codec: object = None, creds: Optional[CredStore] = None) -> None:
        #: the seams 3c fills; each defaults to its refusing production default (`--unmetered` admits the budget)
        self.unmetered = unmetered
        self.budget = budget if budget is not None else (UnmeteredBudget() if unmetered else RefusingBudget())
        self.dispatch_policy = dispatch_policy if dispatch_policy is not None else no_dispatch_route
        self.codec = codec if codec is not None else NoCodec()
        #: the credentials under `L/keys/cred`, opened at the first exchange that names one
        self.creds = creds
        #: gw.jsonl (`main()` opens it); None writes no exchange record
        self.gw_log: Optional[GwLog] = None
        #: the upstream deadlines (design 1.6), attributes so a test can scale them down
        self.connect_timeout_s = upstream.CONNECT_TIMEOUT_S
        self.head_timeout_s = upstream.HEAD_TIMEOUT_S
        self.idle_s = upstream.IDLE_S
        #: the one uid admitted on the admin socket (the `_levain_control` account); None admits nobody.
        self.control_uid = control_uid
        #: the adopted admin connection, None between its EOF and the next adoption.
        self.admin: Optional[AdminConn] = None
        self.admin_lock: Optional[asyncio.Lock] = None    # one adoption (and so one preemption) at a time
        #: the credential layer's lifecycle seam, which shutdown calls; the stand-in until the real writer is passed.
        self.cred_lifecycle: CredLifecycle = cred_lifecycle if cred_lifecycle is not None else FakeCredLifecycle()
        #: uids admitted at accept: the `_levain_gw<n>` accounts.
        self.gw_uids = frozenset(gw_uids)
        #: TEST ONLY (the one-uid fixture harness): peers of this uid are matched to a registration by (pid, start)
        #: whatever its `front_uid`. None on a real install; main() refuses it as root or as `_levain_keys`.
        self.single_uid = single_uid
        self.control_path = control_path   # CONTROL's socket: the real TOKEN-RESOLVE client's
        self.lib_root = lib_root
        #: the last base map that parsed whole: its file's (st_dev, st_ino, st_mtime_ns, st_size), and its routes
        self._base: Optional[Tuple[Tuple[int, int, int, int], Dict[str, RoutePolicy]]] = None
        self.first_frame_s = FIRST_FRAME_S
        self.pending_per_peer = PENDING_PER_PEER
        self.exchanges_per_session = EXCHANGES_PER_SESSION
        self.max_exchange_conns = MAX_EXCHANGE_CONNS
        self.admin_epoch = 0
        self.closing = False
        self.regs: Dict[int, Registration] = {}          # front_uid -> registration
        self.by_session: Dict[str, Registration] = {}
        self.reg_high: Dict[int, int] = {}               # front_uid -> highest reg_token accepted (process life)
        self.pool: Set[Conn] = set()                     # every exchange-pool connection still open, detached or not
        #: token_digest -> CONTROL's answer `{"session", "claims"}`; cleared by DROP-SESSION (that session's) and by
        #: every fail-closed clear
        self.cache: Dict[str, dict] = {}
        #: digests keys itself saw dropped (bounded, oldest first): `token-revoked` without asking
        self.dropped: "OrderedDict[str, None]" = OrderedDict()
        #: the per-digest single-flight: (admin_epoch, registration, digest) -> the table-owned resolve task. Keyed by
        #: the asking registration too, so no registration joins a resolve another one started (one started before a
        #: drop can never answer the registration that replaced it), and a detach cancels its own registration's.
        self.resolving: Dict[Tuple[int, "Registration", str], asyncio.Task] = {}
        self.resolve_sem: Optional[asyncio.Semaphore] = None   # the global RESOLVE_CONNS
        self.servers: list = []
        self.state_lock: Optional[asyncio.Lock] = None
        self._done: Optional[asyncio.Event] = None
        self._closer: Optional[asyncio.Future] = None     # the one run of _close, made by the first trigger
        self._starting: Optional[asyncio.Event] = None    # made by start(), set when start() has returned

    # -- start / stop --

    def _in_loop(self) -> None:
        """The locks and the latch's event, made once, inside the running loop (3.9 binds them to the loop)."""
        if self.state_lock is None:
            self.state_lock = asyncio.Lock()
            self.admin_lock = asyncio.Lock()
            self._done = asyncio.Event()
            self.resolve_sem = asyncio.Semaphore(RESOLVE_CONNS)

    async def start(self, socks: Sequence[socket.socket], admin: Optional[socket.socket] = None) -> None:
        """Serve `socks` as front listeners and `admin` as the admin listener (bind_listeners' output). start() alone
        creates servers, and the shutdown's close waits for start() to return before it closes `servers`; so a trigger
        before or during start never leaves a listener. Once `closing` is set no further server is created and the
        rest of the sockets are closed unserved; a server whose creation was in flight at the trigger is in `servers`
        by the time the close runs, and is closed there (until then admission refuses every connection it accepts). A
        server creation that FAILS closes the servers already made (never their raw sockets, which the loop watches)
        and the sockets not yet served, then raises."""
        self._in_loop()
        self._starting = asyncio.Event()
        plan = [(s, self._on_conn) for s in socks] + ([(admin, self._on_admin)] if admin is not None else [])
        made = 0
        try:
            for i, (s, handler) in enumerate(plan):
                if self.closing:
                    for rest, _h in plan[i:]:
                        rest.close()
                    return
                self.servers.append(await asyncio.start_unix_server(handler, sock=s, backlog=ACCEPT_BACKLOG))
                made += 1
        except BaseException:
            for srv in self.servers:
                srv.close()
            for rest, _h in plan[made:]:
                rest.close()
            raise
        finally:
            self._starting.set()

    def arm_triggers(self) -> None:
        """The ONE shutdown trigger: SIGTERM, and EOF on stdin when stdin is a pipe (the launcher's), both setting the
        one latch. A stdin that is not a pipe (launchd's, `</dev/null`) is never read: EOF there means nothing."""
        self._in_loop()
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, self.trigger)
        try:
            fifo = stat.S_ISFIFO(os.fstat(0).st_mode)
        except OSError:
            fifo = False
        if not fifo:
            print("levain gw-keys: stdin is not a pipe; the stdin-EOF shutdown trigger is not armed (SIGTERM only)",
                  file=sys.stderr, flush=True)
            return
        os.set_blocking(0, False)
        loop.add_reader(0, self._on_stdin)

    def _on_stdin(self) -> None:
        try:
            data = os.read(0, 4096)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            asyncio.get_running_loop().remove_reader(0)
            self.trigger()

    def trigger(self) -> None:
        """The latch: `closing` is set here, synchronously, so admission and the admin calls refuse from this instant
        (no table mutation awaits inside its lock, so none can be half-done when it is set). The close runs once, as
        a task; a later trigger is a no-op. The latch's first transition also tells the credential layer
        (`begin_closing`), whose failure is noted and never stops the shutdown."""
        self._in_loop()
        self.closing = True
        if self._closer is None:
            self._closer = asyncio.ensure_future(self._close())
            try:
                self.cred_lifecycle.begin_closing()
            except Exception as e:
                _note(logging.WARNING, "levain gw-keys: cred_lifecycle.begin_closing failed (%s)", type(e).__name__)

    async def shutdown(self) -> None:
        """Trigger, then await the one close's completion (a later call awaits the same completion and returns)."""
        self.trigger()
        assert self._done is not None
        await self._done.wait()

    async def _close(self) -> None:
        """Shutdown is a transition (design 1.5 "Shutdown's order"), run once behind the latch: wait for a start() in
        progress to return (its only awaits are its server creations); phase 1 over every registration; (1) the
        listeners' synchronous close; (2) phase 2; (2b) the adopted admin connection's transport aborted and its
        read loop and request tasks cancelled (the EOF path the read loop runs finds the table empty); (3) the
        credential layer's detached tasks cancelled; (4) ONE bounded wait over the listeners' closure, the detached
        exchange tasks and the admin tasks, nothing cancelled by the wait itself. Steps (1) to (3) have no await between
        them, and a failure of one step never skips the rest."""
        assert self._done is not None and self.state_lock is not None
        try:
            if self._starting is not None:
                await self._starting.wait()
            async with self.state_lock:     # phase 1: keys' own act, so no epoch check
                victims = self._detach_all()
            tasks = {v.task for v in victims if isinstance(v, Conn) and v.kind == "exchange" and v.task is not None
                     and v.task is not asyncio.current_task() and not v.task.done()}
            for s in self.servers:          # (1) the listeners' synchronous close: no accept after this
                s.close()
            self._effects(victims)          # (2) phase 2
            admin_tasks: Set[asyncio.Task] = set()
            adm = self.admin
            if adm is not None:             # (2b) aborted, not closed: a CONTROL that stopped reading holds a close
                try:
                    adm.writer.transport.abort()
                except Exception as e:
                    _note(logging.WARNING, "levain gw-keys: aborting the admin connection failed (%s)", type(e).__name__)
                for t in {adm.task} | set(adm.tasks):   # its read loop (whose EOF path finds the table empty) and requests
                    if t is not None and t is not asyncio.current_task() and not t.done():
                        t.cancel()
                        admin_tasks.add(t)
            try:                            # (3)
                self.cred_lifecycle.cancel_detached()
            except Exception as e:
                _note(logging.WARNING, "levain gw-keys: cred_lifecycle.cancel_detached failed (%s)", type(e).__name__)
            closures = {asyncio.ensure_future(s.wait_closed()) for s in self.servers}
            waitset = closures | tasks | admin_tasks
            if waitset:                     # an empty set is a ValueError; nothing pending past the deadline is cancelled
                await asyncio.wait(waitset, timeout=SHUTDOWN_WAIT_S)
        finally:
            self._done.set()

    async def wait_done(self) -> None:
        self._in_loop()
        assert self._done is not None
        await self._done.wait()

    # -- the in-process API (the admin tasks') --

    async def front_register(self, epoch: int, session: str, front_uid: int, pid: int, start: int, incarnation: int,
                             reg_token: int) -> Optional[dict]:
        assert self.state_lock is not None
        victims: List[Victim] = []
        async with self.state_lock:
            if epoch != self.admin_epoch or self.closing:
                return None
            high = self.reg_high.get(front_uid, 0)
            cur = self.regs.get(front_uid)
            if reg_token > high:
                if cur is not None:
                    victims = self._detach(cur)
                reg = Registration(session, front_uid, pid, start, incarnation, reg_token)
                reg.admin_epoch = epoch
                self.regs[front_uid] = reg
                self.by_session[session] = reg
                self.reg_high[front_uid] = reg_token
                answer: dict = {}
            elif (reg_token == high and cur is not None and cur.reg_token == reg_token
                  and cur.fields() == (session, front_uid, pid, start, incarnation)):
                answer = {}
            else:
                answer = {"refused": "registration-stale", "reg_high": high}
        self._effects(victims)
        return answer

    async def front_arm(self, epoch: int, session: str, reg_token: int) -> Optional[dict]:
        assert self.state_lock is not None
        async with self.state_lock:
            if epoch != self.admin_epoch or self.closing:
                return None
            reg = self.by_session.get(session)
            if reg is None or reg.reg_token != reg_token or self.regs.get(reg.front_uid) is not reg:
                return {"refused": "registration-stale"}
            if reg.armed_at is None:   # the FIRST accepted FRONT-ARM stamps; a duplicate keeps the stamp
                reg.armed_at = asyncio.get_running_loop().time()
                reg.state = "armed"
            return {}

    async def drop_session(self, epoch: int, session: str) -> Optional[dict]:
        assert self.state_lock is not None
        victims: List[Victim] = []
        async with self.state_lock:
            if epoch != self.admin_epoch or self.closing:
                return None
            reg = self.by_session.get(session)
            if reg is not None:
                victims = self._detach(reg, revoke=True)
        self._effects(victims)
        return {"dropped": reg is not None}   # false = nothing held: still success

    # -- transitions --

    def _detach(self, reg: Registration, revoke: bool = False) -> List[Victim]:
        """Phase 1 (caller holds state_lock; no await): take `reg` out of every table and return every connection
        attributed to it, each marked detached, preceded by the `Upstream` OBJECT of each detached exchange that holds
        one (phase 2 aborts it: an abort drops the transport's buffer, where a close would flush credential-bearing
        bytes); an exchange's task is cancelled (delivery only, never the guarantee). The session's cached claims
        leave the token cache; with `revoke` (DROP-SESSION) their digests enter the dropped set."""
        if self.regs.get(reg.front_uid) is reg:
            del self.regs[reg.front_uid]
        if self.by_session.get(reg.session) is reg:
            del self.by_session[reg.session]
        for digest in [d for d, a in self.cache.items() if a["session"] == reg.session]:
            del self.cache[digest]
            if revoke:
                self._mark_dropped(digest)
        for key, t in list(self.resolving.items()):
            if key[1] is reg:
                t.cancel()   # only this registration's exchanges await it: its permits are freed now, not at RESOLVE_S
        reg.state = "dropped"
        victims = ([reg.status] if reg.status is not None else []) + list(reg.pending) + list(reg.exchanges)
        reg.status = None
        reg.pending.clear()
        reg.exchanges.clear()
        ups: List[Victim] = []
        for c in victims:
            c.detached = True
            if c.upstream is not None:
                ups.append(c.upstream)
            if c.kind == "exchange" and c.task is not None:
                c.task.cancel()
        return ups + victims

    def _detach_all(self) -> List[Victim]:
        """Phase 1 of the fail-closed clear (caller holds state_lock; no await): every registration detached, every
        `Upstream` ahead of every connection, the token cache and the dropped set cleared, and every resolve task
        cancelled (the table is empty after it, so no resolve has a registration left to answer, whatever its epoch).
        `reg_high` is kept: a clear never lowers a fence."""
        regs = {id(r): r for r in list(self.regs.values()) + list(self.by_session.values())}
        got: List[Victim] = []
        for reg in regs.values():
            got += self._detach(reg)
        self.cache.clear()
        self.dropped.clear()
        for t in list(self.resolving.values()):
            t.cancel()   # its permits are freed now, not at its deadline; its `finally` pops the entry
        return [v for v in got if isinstance(v, Upstream)] + [v for v in got if not isinstance(v, Upstream)]

    def _fence_exchanges(self) -> None:
        """Preemption's step (1) (caller holds state_lock; no await): every live exchange is marked detached, so
        `Upstream.write` refuses its next byte from the bump on, through step (2)'s bounded await. The tables are
        left to step (3)'s phase 1, which records each exchange's `Upstream` for phase 2's abort; a new exchange is
        refused at admission meanwhile, its registration having been made under an older epoch."""
        for reg in list(self.regs.values()) + list(self.by_session.values()):
            for c in reg.exchanges:
                c.detached = True

    def _mark_dropped(self, digest: str) -> None:
        self.dropped[digest] = None
        self.dropped.move_to_end(digest)
        while len(self.dropped) > DROPPED_MAX:
            self.dropped.popitem(last=False)

    def _effects(self, victims: List[Victim]) -> None:
        """Phase 2: run right after phase 1 released the lock, synchronously. An `Upstream` is aborted (never closed;
        its abort never raises). Each connection in its own try: `K` if it is a claimed STATUS connection, then close,
        then abort ABORT_AFTER_S later; any failure aborts that one and goes on."""
        if not victims:
            return
        loop = asyncio.get_running_loop()
        for c in victims:
            if isinstance(c, Upstream):
                c.abort()   # never raises an Exception, and notes its own failures
                continue
            t = c.writer.transport
            try:
                if c.kind == "status":
                    t.write(_K)
                t.close()
                loop.call_later(ABORT_AFTER_S, t.abort)
            except Exception as e:
                _note(logging.WARNING, "levain gw-keys: closing a detached connection failed (%s)", type(e).__name__)
                try:
                    t.abort()
                except Exception:
                    pass

    # -- the admin server --

    async def _on_admin(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One admin connection: the uid check, the adoption (a preemption when one is adopted already), the read
        loop, and, unless a preemption replaced it, its EOF's fail-closed clear."""
        conn: Optional[AdminConn] = None
        try:
            try:
                uid = peer_uid(writer.get_extra_info("socket"))
            except PeerUnknown:
                return
            if self.control_uid is None or uid != self.control_uid or self.closing:
                return
            conn = await self._adopt(reader, writer)
            if conn is None:
                return
            await self._admin_read(conn)
        except (OSError, EOFError, AdminMalformed):
            pass   # CONTROL went away or sent what the wire does not allow: either way, this connection's EOF
        except asyncio.CancelledError:
            if conn is None or not conn.preempted:
                raise
        except Exception as e:
            _note(logging.WARNING, "levain gw-keys: admin connection handler failed (%s)", type(e).__name__)
        finally:
            if conn is None:
                writer.close()
            elif not conn.preempted:
                try:
                    await self._admin_eof(conn)
                finally:
                    if not conn.preempted:
                        writer.transport.abort()   # the clear has run: no answer still buffered stands

    async def _adopt(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> Optional[AdminConn]:
        """Adopt a new admin connection; with one adopted already, preempt it first, in the design's five steps."""
        assert self.admin_lock is not None and self.state_lock is not None
        async with self.admin_lock:
            if self.closing:
                return None
            old = self.admin
            async with self.state_lock:
                self.admin_epoch += 1                # (1) every task of the old connection is stale from here,
                epoch = self.admin_epoch
                self._fence_exchanges()              # (1) and every exchange it admitted sends nothing more
            if old is not None:
                old.preempted = True
                wait: Set[asyncio.Task] = set(old.tasks)
                if old.task is not None:
                    old.task.cancel()                # (1) stop reading it
                    wait.add(old.task)
                try:
                    for t in old.tasks:              # (2) cancel its request tasks, then await them, bounded
                        t.cancel()
                    if wait:
                        await asyncio.wait(wait, timeout=PREEMPT_WAIT_S)
                finally:
                    # (3)-(4) run however this handler leaves step (2), a cancel of it included: the old connection
                    # no longer runs its own EOF clear, so nothing else would
                    await asyncio.shield(self._preempt_clear(old))
            if self.closing:
                return None
            conn = AdminConn(reader, writer, epoch)  # (5) adopt; it starts against an empty table
            self.admin = conn
            return conn

    async def _preempt_clear(self, old: AdminConn) -> None:
        """Preemption's steps (3) and (4): the fail-closed clear in its two phases, then the old connection closed.
        Until step (5) adopts the new connection, none is adopted. A clear that runs only after a NEWER connection
        was adopted (its preempting handler was cancelled and the shielded clear ran late) clears nothing: the table
        is the newer connection's, and that adoption's own preemption already cleared what `old` held."""
        assert self.state_lock is not None
        victims: List[Victim] = []
        async with self.state_lock:                  # (3) phase 1
            if self.admin is old or self.admin is None:
                self.admin = None
                victims = self._detach_all()
        self._effects(victims)                       # (3) phase 2
        try:
            old.writer.close()                       # (4)
        except Exception as e:
            _note(logging.WARNING, "levain gw-keys: closing a preempted admin connection failed (%s)",
                  type(e).__name__)

    async def _admin_eof(self, conn: AdminConn) -> None:
        """The adopted connection ended: fail closed. The epoch is bumped in the same mutation section as the clear,
        so a request task of this connection still in flight changes nothing after it."""
        assert self.state_lock is not None
        async with self.state_lock:
            if self.admin is not conn or conn.epoch != self.admin_epoch:
                return
            self.admin_epoch += 1
            self.admin = None
            victims = self._detach_all()
        for t in list(conn.tasks):
            t.cancel()
        self._effects(victims)

    async def _admin_read(self, conn: AdminConn) -> None:
        """The read loop. Each request's place in its session's chain is taken HERE, between the read and the spawn,
        with no `await` in between, so two requests naming one session run in frame order."""
        while True:
            got = await read_admin_frame(conn.reader)
            if got is None:
                return
            rid, op, args = got
            if len(conn.tasks) >= ADMIN_INFLIGHT_MAX:
                raise AdminMalformed("too many requests in flight")
            session = args.get("session") if type(args.get("session")) is str else None
            prev = conn.last.get(session) if session is not None else None
            t = asyncio.ensure_future(self._admin_request(conn, rid, op, args, prev))
            conn.tasks.add(t)
            if session is not None:
                conn.last[session] = t
            t.add_done_callback(functools.partial(self._admin_task_done, conn, session))

    def _admin_task_done(self, conn: AdminConn, session: Optional[str], t: asyncio.Task) -> None:
        conn.tasks.discard(t)
        if session is not None and conn.last.get(session) is t:
            del conn.last[session]
        if t.cancelled():
            return
        e = t.exception()
        if e is not None:
            _note(logging.WARNING, "levain gw-keys: admin request failed (%s)", type(e).__name__)

    async def _admin_request(self, conn: AdminConn, rid: int, op: str, args: dict,
                             prev: Optional[asyncio.Task]) -> None:
        """One request: wait for the previous request of its session (its outcome is not this one's), answer it,
        and write the answer only while its connection's epoch is current. A failure inside keys' own call, in the
        answer's encoding or in its write (an answer not drained within ADMIN_FRAME_S included) closes the
        connection: its EOF clears everything, never a table left as the failure left it nor a client left waiting."""
        if prev is not None and not prev.done():
            await asyncio.wait({prev})
        try:
            answer = await self._admin_answer(conn.epoch, rid, op, args)
            if answer is None or conn.epoch != self.admin_epoch:
                return   # stale, or refused by the shutdown latch: no answer
            data = encode_admin(answer)
            async with conn.wlock:
                if conn.epoch != self.admin_epoch:
                    return
                conn.writer.write(data)
                await asyncio.wait_for(conn.writer.drain(), ADMIN_FRAME_S)
        except Exception:
            # abort, not close: a close waits to flush what a client that stopped reading will never read, and
            # the read loop sees EOF only once the transport is gone
            conn.writer.transport.abort()
            raise

    async def _admin_answer(self, epoch: int, rid: int, op: str, args: dict) -> Optional[dict]:
        bad = check_admin_args(op, args)
        if bad is not None:
            return {"id": rid, "ok": False, "refused": bad}
        if op == "FRONT-REGISTER":
            out = await self.front_register(epoch, args["session"], args["front_uid"], args["pid"], args["start"],
                                            args["incarnation"], args["reg_token"])
        elif op == "FRONT-ARM":
            out = await self.front_arm(epoch, args["session"], args["reg_token"])
        elif op == "DROP-SESSION":
            out = await self.drop_session(epoch, args["session"])
        else:
            out = {"refused": RELOAD_REFUSED}
        if out is None:
            return None
        if "refused" in out:
            return {"id": rid, "ok": False, **out}
        return {"id": rid, "ok": True, **out}

    # -- admission --

    def _lookup(self, uid: int, pid: int, start: int) -> Optional[Registration]:
        if self.single_uid is not None and uid == self.single_uid:
            hits = [r for r in self.regs.values() if r.pid == pid and r.start == start]
            return hits[0] if len(hits) == 1 else None
        if uid not in self.gw_uids:
            return None
        reg = self.regs.get(uid)
        if reg is None or reg.pid != pid or reg.start != start:
            return None
        return reg

    def _admit(self, peer: Tuple[int, int, int], writer: asyncio.StreamWriter) -> Optional[Conn]:
        """At accept, under state_lock, no await: which pool this connection is in, or None (close, no frame)."""
        if self.closing:
            return None
        reg = self._lookup(*peer)
        if reg is None:
            return None
        if reg.state == "armed" and reg.status is None:
            conn = Conn(writer, reg, "candidate")
            reg.status = conn
            return conn
        if reg.state == "claimed":
            if len(reg.pending) >= self.pending_per_peer or len(self.pool) >= self.max_exchange_conns:
                return None
            conn = Conn(writer, reg, "pending")
            reg.pending.add(conn)
            self.pool.add(conn)
            return conn
        return None   # REGISTERED and not armed, a STATUS candidate already pending, or LOST

    def _forget(self, conn: Conn) -> None:
        """The connection is closing: out of every set (idempotent; no lock, no await)."""
        reg = conn.reg
        reg.pending.discard(conn)
        reg.exchanges.discard(conn)
        if reg.status is conn:
            reg.status = None
        self.pool.discard(conn)

    async def _on_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.first_frame_s
        conn: Optional[Conn] = None
        try:
            try:
                peer = peer_of(writer.get_extra_info("socket"))
            except PeerUnknown:
                return
            assert self.state_lock is not None
            async with self.state_lock:
                conn = self._admit(peer, writer)
            if conn is None:
                return
            frames = FrameReader(reader)
            side = KeysSide()
            allowed = frozenset({b"H"}) if conn.kind == "candidate" else frozenset({b"Q"})
            got = await asyncio.wait_for(frames.read(allowed), max(0.0, deadline - loop.time()))
            if got is None or conn.detached:
                return
            ftype, payload = got
            side.got(ftype)
            if ftype == b"H":
                await self._status(conn, frames, payload)
            else:
                await self._exchange(conn, frames, side, payload)
        except (OSError, EOFError, WireError, asyncio.TimeoutError):
            pass   # the peer went away, sent what the wire does not allow, or nothing in time: closed with no frame
        except Exception as e:
            _note(logging.WARNING, "levain gw-keys: connection handler failed (%s)", type(e).__name__)
        finally:
            if conn is not None:
                self._forget(conn)
            writer.close()

    async def _status(self, conn: Conn, frames: FrameReader, h: dict) -> None:
        reg = conn.reg
        assert self.state_lock is not None
        async with self.state_lock:
            ok = (not conn.detached and not self.closing and self.regs.get(reg.front_uid) is reg and reg.state == "armed"
                  and reg.status is conn and h["session"] == reg.session and h["incarnation"] == reg.incarnation)
            if ok:
                reg.state = "claimed"
                conn.kind = "status"
                conn.writer.write(_A)
        if not ok:
            return
        try:
            await frames.read(frozenset())   # nothing is valid after `H`: EOF, or anything at all, ends it
        finally:
            if not conn.detached and self.regs.get(reg.front_uid) is reg:
                reg.state = "lost"   # the claim ended without keys' `K`: refused from now on
                _note(logging.WARNING, "levain gw-keys: front-lost session=%s", reg.session)

    async def _exchange(self, conn: Conn, frames: FrameReader, side: KeysSide, q: dict) -> None:
        """THE FRAME (design 1.2): the ONE admission check at `Q` and the epoch stamp, then entry into the
        registration's exchange set as the LAST statement under the lock; the outer `try` follows with no await
        between them, and its `finally` is the release, so phase 1 can always find the exchange until it is released."""
        reg = conn.reg
        assert self.state_lock is not None
        async with self.state_lock:
            reg.pending.discard(conn)
            if conn.detached:
                return
            admitted = (self.regs.get(reg.front_uid) is reg and reg.state == "claimed" and not self.closing
                        and reg.admin_epoch == self.admin_epoch and len(reg.exchanges) < self.exchanges_per_session)
            if admitted:
                conn.kind = "exchange"
                conn.admin_epoch = self.admin_epoch
                conn.rec = new_record(reg.session, q)
                reg.exchanges.add(conn)
        if not admitted:
            await self._refuse(conn, frames, side, "keys-unavailable", UNTRUSTED_DRAIN_CAP)
            return
        try:
            await self._serve_q(conn, frames, side, q)
        except StaleExchange:
            pass   # detached: nothing more went upstream, and no answer (phase 2 closes the front connection)
        finally:
            try:
                await release_exchange(conn)
            finally:
                self._log_exchange(conn)

    def _current(self, conn: Conn) -> bool:
        """Under state_lock: the exchange may still mutate shared state (its epoch current, not detached, its
        registration the current one for both its front and its session, not closing)."""
        reg = conn.reg
        return (not conn.detached and not self.closing and conn.admin_epoch == self.admin_epoch
                and self.regs.get(reg.front_uid) is reg and self.by_session.get(reg.session) is reg)

    async def _serve_q(self, conn: Conn, frames: FrameReader, side: KeysSide, q: dict) -> None:
        """The `Q` handler: the token state, then the route against the token's claims and keys' OWN policy, the
        request re-checked against that policy, the seams and the credential, then the request through the
        exchange's `Upstream.write`. A stale or detached exchange ends with NO answer (its front connection is one of
        phase 1's victims, which phase 2 closes)."""
        out = await self._token_state(conn, q["token"])
        if out is None or conn.detached:
            return
        if "refused" in out:
            await self._refuse(conn, frames, side, out["refused"], UNTRUSTED_DRAIN_CAP)
            return
        claims = out["answer"]["claims"]
        rec = conn.rec if conn.rec is not None else new_record(conn.reg.session, q)
        slot = claims.get("slot")
        rec["slot"] = slot if type(slot) is int else None
        try:
            pol = self._route_policy(claims, q["route"])
        except norm.Refused as r:
            await self._refuse(conn, frames, side, r.name, UNTRUSTED_DRAIN_CAP)
            return
        try:
            head, hconn, cred, tpl = self._check_request(conn.reg, claims, pol, q)   # the budget reservation is last
        except norm.Refused as r:
            await self._refuse(conn, frames, side, r.name, pol.body_cap)
            return
        rec["template"] = tpl.path
        scanner = Scanner([cred.secret.encode("latin-1")] if cred is not None else [])
        # From the reservation on, every exit releases it (no ledger settles one at this tip): nothing that can raise
        # runs between the reservation and the try. Once `R` is sent no `F` may follow (the wire's order), so a
        # failure after it ends the exchange with the front's connection closed mid-body.
        sent = responded = False
        try:
            assert pol.upstream is not None   # a brokered route always names one
            host = pol.upstream.host
            addr = await upstream_step(pick_address(host, UPSTREAM_PORT))
            rec["upstream_address"] = addr
            # the connect and the TLS handshake share one deadline; a result that lands as it expires is still stored
            # on the exchange by open_exchange, so the release aborts it
            up = await upstream_step(asyncio.wait_for(open_exchange(addr, UPSTREAM_PORT, host, conn),
                                                      self.connect_timeout_s))

            async def send_head() -> None:
                nonlocal sent
                up.write(head)   # the gate: a refused or failed write handed nothing upstream
                sent = True      # from here the head may have reached the upstream
                await up.drain()

            await upstream_step(send_head())
            if not conn.detached:   # a detached exchange answers nothing; its next upstream write is refused by the gate
                conn.writer.write(_A)
                await conn.writer.drain()
            ended = await self._forward_body(frames, side, up, hconn, q["body"], pol.body_cap, rec)
            if not ended:
                return   # the front aborted (`X`) or went away before `E`: nothing to answer
            loop = asyncio.get_running_loop()
            resp = await upstream_step(upstream.read_head(up, hconn, loop.time() + self.head_timeout_s))
            r_frame = self._response_frame(resp, pol, cred, scanner)
            if conn.detached:
                return
            conn.writer.write(r_frame)
            responded = True
            rec["status"] = resp.status_code
            await conn.writer.drain()
            await self._relay_body(conn, up, hconn, scanner, rec)
        except (norm.Refused, UpstreamFailed) as r:
            if responded:
                rec["refused"] = r.name   # after `R`: the front's connection is closed with the body cut short
                _note(logging.WARNING, "levain gw-keys: response cut short after its head (%s)", r.name)
            else:
                await self._refuse(conn, frames, side, r.name, pol.body_cap)
        finally:
            rec["redactions"] = scanner.count
            self._release(conn.reg, pol, sent)

    def _response_frame(self, resp: "h11.Response", pol: RoutePolicy, cred: object, scanner: Scanner) -> bytes:
        """The `R` frame for a final response head, or the refusal (design 1.6): a redirect is `upstream-redirect`;
        a `content-encoding` other than identity, or a status past 599, is `upstream-error`; a 401 on a refreshing
        credential asks for a refresh and is `credential-unavailable` (never relayed, never replayed). Only the
        route's `responses.headers` names pass, each value scanned as one block."""
        status = resp.status_code
        if status in REDIRECTS:
            raise norm.Refused("upstream-redirect", str(status))
        if status > 599:
            raise norm.Refused("upstream-error", "status")
        allow = set(pol.response_headers)
        out: List[List[str]] = []
        for raw_name, raw_value in resp.headers:
            name = bytes(raw_name).decode("ascii", "replace").lower()
            if name == "content-encoding" and bytes(raw_value).strip().lower() not in (b"", b"identity"):
                raise norm.Refused("upstream-error", "encoding")
            if name not in allow:
                continue
            value, _ = scanner.value(bytes(raw_value))
            text = value.decode("latin-1")
            if _HVALUE.match(text) is None:
                _note(logging.WARNING, "levain gw-keys: response header dropped (value outside the wire grammar) %s",
                      name)
                continue
            out.append([name, text])
        if status == 401 and cred is not None:
            on_upstream_401(cred, cred.generation)   # type: ignore[attr-defined]
            if getattr(cred, "kind", None) == "oauth2-refresh":
                raise norm.Refused("credential-unavailable", "upstream 401")
        try:
            return gwwire.encode(b"R", {"v": 1, "status": status, "headers": out})
        except WireError:
            raise norm.Refused("upstream-error", "response head over the frame cap") from None

    async def _relay_body(self, conn: Conn, up: Upstream, hconn: "h11.Connection", scanner: Scanner,
                          rec: dict) -> None:
        """`b`* `e`: each body read relayed at once through the scanner (no buffering but the held possible start of a
        secret), each frame drained before the next read, so a slow front pauses the upstream's reader."""
        w = conn.writer
        while True:
            part = await upstream_step(upstream.read_body(up, hconn, self.idle_s))
            out = scanner.feed(part) if part is not None else scanner.flush()
            for i in range(0, len(out), BODY_FRAME):
                piece = out[i:i + BODY_FRAME]
                w.write(gwwire.encode(b"b", piece))
                rec["bytes_down"] += len(piece)
                await w.drain()
            if part is None:
                break
        w.write(gwwire.encode(b"e"))
        await w.drain()

    def _log_exchange(self, conn: Conn) -> None:
        """The exchange's gw.jsonl record (design 1.9), written once at its end; never a header value, a body byte or a
        token. A failing log never touches the exchange's outcome."""
        rec, conn.rec = conn.rec, None
        if rec is None or self.gw_log is None:
            return
        try:
            out = {k: v for k, v in rec.items() if k != "t0"}
            out["ms"] = int((time.monotonic() - rec["t0"]) * 1000)
            out["kind"] = "exchange"
            out["at"] = _at(time.time())
            self.gw_log.write(out)
        except Exception as e:
            _note(logging.WARNING, "levain gw-keys: the exchange record was not written (%s)", type(e).__name__)

    def _release(self, reg: Registration, pol: RoutePolicy, sent: bool) -> None:
        """The budget reservation's release, synchronous so a cancel cannot skip it; a failing seam is noted and
        never replaces the exchange's own outcome."""
        try:
            self.budget.release(reg.session, pol.route, pol.max_call_microdollars, sent)
        except Exception as e:
            _note(logging.WARNING, "levain gw-keys: budget release failed (%s)", type(e).__name__)

    def _route_policy(self, claims: dict, route: str) -> RoutePolicy:
        """keys' OWN policy for `route`: it must be one of the token's `routes`, and the object comes from keys' load
        of the base map (then the dispatch seam), never from the front or session.json. A route that is not brokered
        is no `Q` route."""
        routes = claims.get("routes")
        if type(routes) is not list or route not in routes:
            raise norm.Refused("route-not-allowed", "not in the token's routes")
        pol = self._base_route(route)
        if pol is None:
            pol = self.dispatch_policy(route)
        if pol is None or pol.type != "brokered" or pol.upstream is None:
            raise norm.Refused("route-not-allowed", "no brokered route")
        return pol

    def _base_route(self, route: str) -> Optional[RoutePolicy]:
        """`route`'s object in `L/routes/base.json`, opened at every `Q` and parsed (`policy.parse_route`, source
        `base`) whenever the opened file's (st_dev, st_ino, st_mtime_ns, st_size) differs from the last map that
        parsed whole, or None when the map has none. EVERY entry is parsed before one is selected: a map keys cannot
        read, any entry that does not parse, or any route id named twice anywhere in the map refuses
        `keys-unavailable` (logged), never a pass for the route asked about; a refused map is never cached."""
        if self.lib_root is None:
            raise norm.Refused("keys-unavailable", "no lib root")
        path = os.path.join(self.lib_root, "routes", "base.json")
        try:
            with open(path, "rb") as f:
                st = os.fstat(f.fileno())
                key = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)
                if self._base is not None and self._base[0] == key:
                    return self._base[1].get(route)
                data = f.read(BASE_MAP_CAP + 1)
            if len(data) > BASE_MAP_CAP:
                raise PolicyError("over the size cap")
            doc = canon.parse(data)
            if type(doc) is not dict or doc.get("format") != BASE_MAP_FORMAT or type(doc.get("routes")) is not list:
                raise PolicyError("format or routes")
            parsed: Dict[str, RoutePolicy] = {}
            for entry in doc["routes"]:
                try:
                    pol = parse_route(entry, source="base")
                except Exception as e:   # any entry's parse failure, of whatever type, refuses the map whole
                    raise PolicyError(f"an entry does not parse ({type(e).__name__})") from None
                if pol.route in parsed:
                    raise PolicyError("a route named twice")
                parsed[pol.route] = pol
            self._base = (key, parsed)
            return parsed.get(route)
        except (OSError, ValueError, ContractError, RecursionError) as e:
            _note(logging.WARNING, "levain gw-keys: the base route map is unusable (%s)", type(e).__name__)
            raise norm.Refused("keys-unavailable", "base map") from None

    def _cred_store(self) -> CredStore:
        if self.creds is None:
            if self.lib_root is None:
                raise norm.Refused("credential-unavailable", "no lib root")
            try:
                fd = os.open(os.path.join(self.lib_root, "keys", "cred"), os.O_RDONLY | os.O_DIRECTORY)
            except OSError as e:
                _note(logging.WARNING, "levain gw-keys: the credential directory is unreadable (%s)", type(e).__name__)
                raise norm.Refused("credential-unavailable", "no credential directory") from None
            self.creds = CredStore(fd)
        return self.creds

    def _check_request(self, reg: Registration, claims: dict, pol: RoutePolicy,
                       q: dict) -> Tuple[bytes, "h11.Connection", object, norm.Template]:
        """keys' own re-check of the `Q` against ITS policy (the front's verdict is never taken), then the seams and
        the credential; returns the request head, the h11 client that wrote it (it frames the body and parses the
        response), the credential (or None) and the matched template. Order: target and template, headers (strict: a forbidden, consumed
        or off-list name is `policy-header`), the declared length against `body_cap`, the codec, the credential and
        its host binding, the budget last (a reservation is made only for a request that goes out)."""
        try:
            method, path = q["method"].encode("ascii"), q["path"].encode("ascii")
            query = q["query"].encode("ascii") or None
        except UnicodeEncodeError:
            raise norm.Refused("malformed", "target") from None
        tpl = norm.match_template(pol.templates, method, path, query)
        if q["upgrade"] != "none":
            if tpl.upgrade is None:
                raise norm.Refused("malformed", "upgrade")
            raise norm.Refused("keys-unavailable", "websocket")   # the brokered websocket is a later session's
        fwd = norm.check_headers([(n, v) for n, v in q["headers"]], pol.headers or {}, upgrade_ok=False, strict=True)
        body = q["body"]
        if body.startswith("length:") and int(body[len("length:"):]) > pol.body_cap:
            raise norm.Refused("body-cap", "length")
        self.codec.check(claims.get("ticket"), pol)
        cred = None
        if pol.credential is not None:
            cred = self._cred_store().observe(pol.credential)
            assert pol.upstream is not None
            check_host(cred, pol.upstream.host, pol.route)
            if any(n == cred.header for n, _ in fwd):
                raise norm.Refused("policy-header", cred.header)
        head, hconn = request_head(pol, method, path, query, fwd, body, cred)
        self.budget.reserve(reg.session, pol.route, pol.max_call_microdollars)   # the last step: see `_serve_q`
        return head, hconn, cred, tpl

    async def _forward_body(self, frames: FrameReader, side: KeysSide, up: Upstream, hconn: "h11.Connection",
                            body: str, cap: int, rec: dict) -> bool:
        """The front's `B` frames through `up.write`, framed as the head declared, until `E` (True) or `X` / EOF
        (False); an upstream failure raises UpstreamFailed. keys counts the bytes itself: past `cap` is `body-cap`, a
        `length:n` body of any other size, or any
        body on a `none` request, is `malformed`."""
        want = int(body[len("length:"):]) if body.startswith("length:") else None
        got = 0
        while True:
            fr = await frames.read(side.allowed())
            if fr is None:
                return False
            ftype, payload = fr
            side.got(ftype)
            if ftype == b"X":
                return False
            if ftype == b"E":
                break
            assert isinstance(payload, bytes)
            got += len(payload)
            if body == "none" or (want is not None and got > want):
                raise norm.Refused("malformed", "body length")
            if got > cap:
                raise norm.Refused("body-cap", "body")
            rec["bytes_up"] = got
            if payload:
                await upstream_send(up, h11_bytes(hconn, h11.Data(data=payload)))   # framed as the head declared
        if want is not None and got != want:
            raise norm.Refused("malformed", "body length")
        tail = h11_bytes(hconn, h11.EndOfMessage())   # the last chunk of a chunked body; nothing for the other framings
        if tail:
            await upstream_send(up, tail)
        return True

    async def _token_state(self, conn: Conn, token: str) -> Optional[dict]:
        """`{"answer": {"session", "claims"}}`, `{"refused": name}`, or None for a stale exchange (no answer). A
        cached digest and one in flight take no permit; a dropped one is `token-revoked` without asking; otherwise
        the first `Q` (the owner) starts the table-owned resolve and every `Q` awaits it through a shield, within
        RESOLVE_S of its own arrival. The answer must bind to the asking registration (`front_binding`) before
        anything else; only a bound answer enters the cache, under state_lock, with the asking registration still
        current and the digest not dropped, so no answer of another session, and none that a drop overtook, is ever
        cached."""
        if TOKEN_RE.match(token) is None:
            return {"refused": "token-invalid"}
        digest = norm.digest_of_token(token)
        reg = conn.reg
        loop = asyncio.get_running_loop()
        deadline = loop.time() + RESOLVE_S
        assert self.state_lock is not None and conn.admin_epoch is not None
        async with self.state_lock:
            if not self._current(conn):
                return None
            hit = self.cache.get(digest)
            if hit is None and digest in self.dropped:
                return {"refused": "token-revoked"}
            if hit is None:
                key = (conn.admin_epoch, reg, digest)
                task = self.resolving.get(key)
                if task is None:
                    task = asyncio.ensure_future(self._resolve(key, reg, deadline))
                    self.resolving[key] = task
                    task.add_done_callback(_resolve_done)
        if hit is None:
            try:
                res = await asyncio.wait_for(asyncio.shield(task), max(0.0, deadline - loop.time()))
            except asyncio.TimeoutError:
                return {"refused": "keys-unavailable"}
            if res[0] == "failed":
                return {"refused": "keys-unavailable"}
            if res[0] == "refused":
                return {"refused": RESOLVE_REFUSALS[res[1]]}
            if not front_binding(res[1], reg):
                return {"refused": "front-mismatch"}
            async with self.state_lock:      # the claims enter the cache only as a fenced mutation
                if not self._current(conn):
                    return None
                if digest in self.dropped:
                    return {"refused": "token-revoked"}
                self.cache[digest] = res[1]
            return {"answer": res[1]}
        if not front_binding(hit, reg):
            return {"refused": "front-mismatch"}
        return {"answer": hit}

    async def _resolve(self, key: Tuple[int, Registration, str], reg: Registration, deadline: float) -> tuple:
        """The table-owned resolve task: its TYPED outcome is `("answer", {"session", "claims"})`,
        `("refused", "token-unknown" | "token-revoked")` or `("failed",)` (any Exception, the deadline included); it
        never raises one to its awaiters. It holds the permits itself, per-session FIRST, global second, each
        released on every exit; its `finally` pops its table entry."""
        try:
            if reg.resolve_sem is None:
                reg.resolve_sem = asyncio.Semaphore(RESOLVE_PER_SESSION)
            assert self.resolve_sem is not None
            loop = asyncio.get_running_loop()
            return await asyncio.wait_for(self._resolve_permitted(reg.resolve_sem, key[2]),
                                          max(0.0, deadline - loop.time()))
        except Exception:
            return ("failed",)
        finally:
            if self.resolving.get(key) is asyncio.current_task():
                del self.resolving[key]

    async def _resolve_permitted(self, mine: asyncio.Semaphore, digest: str) -> tuple:
        assert self.resolve_sem is not None
        async with mine:                     # the per-session permit FIRST: no global permit is held while waiting
            async with self.resolve_sem:
                return await self._token_resolve(digest)

    async def _token_resolve(self, digest: str) -> tuple:
        """`TOKEN-RESOLVE {token_digest}` on its own connection to CONTROL's socket: one request, one answer."""
        if self.control_path is None:
            return ("failed",)
        reader, writer = await asyncio.open_unix_connection(self.control_path)
        try:
            writer.write(encode_admin({"v": 1, "op": "TOKEN-RESOLVE", "id": 1, "token_digest": digest}))
            await writer.drain()
            ans = await read_control_answer(reader)
        finally:
            writer.transport.abort()
        if ans.get("id") != 1:
            return ("failed",)
        if ans.get("ok") is True:
            session, claims = ans.get("session"), ans.get("claims")
            if type(session) is str and session and type(claims) is dict:
                return ("answer", {"session": session, "claims": claims})
            return ("failed",)
        if ans.get("ok") is False and ans.get("refused") in RESOLVE_REFUSALS:
            return ("refused", ans["refused"])
        return ("failed",)

    async def _refuse(self, conn: Conn, frames: FrameReader, side: KeysSide, name: str, cap: int) -> None:
        """The `F` path: `F`, SHUT_WR, then read and discard the front's frames until `E`, `X`, EOF, `cap` body bytes
        read after the `F`, or DRAIN_S; the caller closes. So `F` is never cut off by a close with unread bytes. A
        detached exchange is answered nothing (phase 2 closes its connection)."""
        if conn.detached:
            return
        if conn.rec is not None and conn.rec["refused"] is None:
            conn.rec["refused"], conn.rec["status"] = name, norm.STATUS.get(name)
        w = conn.writer
        w.write(gwwire.encode(b"F", {"refused": name}))
        if w.can_write_eof():
            w.write_eof()
        loop = asyncio.get_running_loop()
        deadline, got = loop.time() + DRAIN_S, 0
        while got < cap:
            left = deadline - loop.time()
            if left <= 0:
                return
            fr = await asyncio.wait_for(frames.read(side.allowed()), left)
            if fr is None:
                return
            ftype, payload = fr
            side.got(ftype)
            if ftype in (b"E", b"X"):
                return
            got += len(payload)


def request_head(pol: RoutePolicy, method: bytes, path: bytes, query: Optional[bytes], fwd: Sequence[Tuple[str, str]],
                 body: str, cred: object) -> Tuple[bytes, "h11.Connection"]:
    """The request head keys writes through `Upstream.write`, built from keys' own policy by an h11 client, and that
    client (it frames the body and parses the response): target = `upstream.base_path` + path (+ `?query`), `host`,
    the re-checked headers, `accept-encoding: identity`, the framing the `Q` declared, then the ONE credential header.
    A head h11 refuses to write is `malformed`."""
    assert pol.upstream is not None
    target = pol.upstream.base_path.encode("ascii") + path + (b"?" + query if query is not None else b"")
    headers = [(b"host", pol.upstream.host.encode("ascii"))]
    headers += [(n.encode("ascii"), v.encode("ascii")) for n, v in fwd]
    headers.append((b"accept-encoding", b"identity"))
    if body.startswith("length:"):
        headers.append((b"content-length", body[len("length:"):].encode("ascii")))
    elif body == "chunked":
        headers.append((b"transfer-encoding", b"chunked"))
    if cred is not None:
        headers.append(cred.header_pair())   # type: ignore[attr-defined]
    conn = h11.Connection(our_role=h11.CLIENT)
    try:
        data = h11_bytes(conn, h11.Request(method=method, target=target, headers=headers))
    except h11.LocalProtocolError:
        raise norm.Refused("malformed", "request head") from None
    return data, conn


async def release_exchange(exchange: Conn) -> None:
    """The exchange's release: a graceful close of an undetached exchange's `Upstream` bounded by GRACE, then an
    abort on every exit (a False abort is noted), then the leave from the exchange set and the drop of the
    `Upstream`, which run even if the abort is cut short. A detached exchange's `Upstream` is never closed
    gracefully (a close would flush what phase 2's abort discards)."""
    up = exchange.upstream
    try:
        if up is not None and not exchange.detached:
            up._graceful_close()
            try:
                await asyncio.wait_for(up.wait_closed(), GRACE)
            except Exception:
                _note(logging.DEBUG, "levain gw-keys: graceful close ended early")
    finally:
        try:
            if up is not None:
                if not up.abort():
                    _note(logging.WARNING, "levain gw-keys: dropped an unclosed transport")
        finally:
            exchange.reg.exchanges.discard(exchange)
            exchange.upstream = None


# --- the process ----------------------------------------------------------------------------------------------------

def _gw_uids() -> frozenset:
    out = set()
    for n in range(STATUS_CONNS):
        try:
            out.add(pwd.getpwnam(GW_ACCOUNT.format(n)).pw_uid)
        except KeyError:
            pass
    return frozenset(out)


def _account_uid(name: str) -> Optional[int]:
    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError:
        return None


def _process_uids() -> Tuple[int, ...]:
    """(real, effective, saved) where `getresuid` exists; macOS has none, so (real, effective) there: its saved uid
    is not readable from Python."""
    if hasattr(os, "getresuid"):
        return tuple(os.getresuid())
    return (os.getuid(), os.geteuid())


def single_uid_refusal(keys_uid: Optional[int], ids: Sequence[int]) -> Optional[str]:
    """Why `--test-single-uid` is refused for a process whose uids are `ids` ((real, effective[, saved])), or None.
    Refused when the `_levain_keys` account's uid is ANY of them (a setuid or `sudo -u` launch holds that authority
    whichever slot it sits in), and when the effective uid is 0."""
    if ids[1] == 0:
        return "the effective uid is root"
    if keys_uid is not None and keys_uid in ids:
        return "this process holds a real install's keys account uid"
    return None


async def _serve(keys: Keys, front_path: str, gid: Optional[int], admin_path: str, admin_gid: Optional[int]) -> int:
    socks = bind_listeners([(front_path, gid), (admin_path, admin_gid)])
    started = False
    try:
        keys.arm_triggers()
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        fixed = count_open_fds()
        need = fixed + bounded()
        print(f"levain gw-keys: start pid={os.getpid()} nofile_soft={soft} nofile_hard={hard} "
              f"fixed_measured={fixed} bounded={bounded()} need={need}", file=sys.stderr, flush=True)
        if keys.unmetered:
            print("levain gw-keys: --unmetered is set: every budget reservation is admitted (no ledger)",
                  file=sys.stderr, flush=True)
        if soft != resource.RLIM_INFINITY and soft < need:
            print(f"levain gw-keys: refused nofile-low (soft {soft} < fixed_measured {fixed} + bounded {bounded()})",
                  file=sys.stderr, flush=True)
            return 3
        started = True   # from here start() owns the sockets: it closes what it does not serve, even on a failure
        await keys.start(socks[:1], admin=socks[1])
        if keys.servers and not keys.closing:   # a trigger before or during start: nothing is served
            print(f"levain gw-keys: ready front={front_path} admin={admin_path}", file=sys.stderr, flush=True)
        await keys.wait_done()
        print("levain gw-keys: stopped", file=sys.stderr, flush=True)
        return 0
    finally:
        if not started:
            for s in socks:
                s.close()


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m levain.broker.keys")
    ap.add_argument("--runtime-root", required=True, help="R: keys serves R/keys/front.sock and R/keys/admin.sock")
    ap.add_argument("--lib-root", required=True, help="L: the route policy and the credentials live under it")
    ap.add_argument("--control", required=True, help="CONTROL's socket, which TOKEN-RESOLVE opens")
    ap.add_argument("--test-single-uid", action="store_true",
                    help="TEST ONLY: admit peers of keys' own uid, matched to a registration by (pid, start); "
                         "refused as root or as " + KEYS_ACCOUNT)
    ap.add_argument("--unmetered", action="store_true",
                    help="3b's fixture and VM runs only: admit every budget reservation (no ledger exists yet)")
    a = ap.parse_args(argv)
    for name, v in (("--runtime-root", a.runtime_root), ("--lib-root", a.lib_root), ("--control", a.control)):
        if not os.path.isabs(v):
            ap.error(f"{name} must be an absolute path")
    if os.geteuid() == 0:
        print("levain gw-keys: refusing to run as root (it runs as _levain_keys)", file=sys.stderr)
        return 2
    single_uid: Optional[int] = None
    gid: Optional[int] = None
    admin_gid: Optional[int] = None
    if a.test_single_uid:
        why = single_uid_refusal(_account_uid(KEYS_ACCOUNT), _process_uids())
        if why is not None:
            print(f"levain gw-keys: --test-single-uid refused: {why}", file=sys.stderr)
            return 2
        # the effective uid: the one the kernel reports to this process's peers, and the one its own fronts carry;
        # the fixture's CONTROL runs as it too
        single_uid, gw_uids = os.geteuid(), frozenset()
        control_uid: Optional[int] = single_uid
    else:
        import grp
        gw_uids = _gw_uids()
        if not gw_uids:
            print("levain gw-keys: no _levain_gw<n> account exists; nothing may connect", file=sys.stderr)
            return 2
        control_uid = _account_uid(CONTROL_ACCOUNT)
        if control_uid is None:
            print(f"levain gw-keys: no {CONTROL_ACCOUNT} account exists; nothing may administer keys", file=sys.stderr)
            return 2
        for group in (FRONTS_GROUP, ADMIN_GROUP):
            try:
                g = grp.getgrnam(group).gr_gid
            except KeyError:
                print(f"levain gw-keys: the {group} group does not exist", file=sys.stderr)
                return 2
            if group == FRONTS_GROUP:
                gid = g
            else:
                admin_gid = g
    raise_nofile()
    keys = Keys(gw_uids=gw_uids, single_uid=single_uid, control_path=a.control, lib_root=a.lib_root,
                control_uid=control_uid, unmetered=a.unmetered)
    try:
        keys.gw_log = install_log(a.lib_root)   # before _serve counts the fds: the log's fd is a fixed one
    except OSError as e:
        print(f"levain gw-keys: refused: gw.jsonl cannot be opened ({type(e).__name__})", file=sys.stderr)
        return 3
    front_path = os.path.join(a.runtime_root, "keys", "front.sock")
    admin_path = os.path.join(a.runtime_root, "keys", "admin.sock")
    try:
        return asyncio.run(_serve(keys, front_path, gid, admin_path, admin_gid))
    except FileExistsError as e:
        print(f"levain gw-keys: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
