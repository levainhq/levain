"""gw-keys (S1 session 3b): the credential side of the gateway, run as `_levain_keys`. This tip holds WHO MAY CONNECT
(design 1.5, session 3b-3a): keys' `R/keys/front.sock` server, the registration table, the two admission pools, the
`F` path, the NOFILE floor, the process entry and its one shutdown trigger; and WHO MAY ASK (session 3b-3b): the admin
server on `R/keys/admin.sock`. The token states, the upstream and the credentials are later parts'; until the real `Q`
handler exists every `Q` is answered `F keys-unavailable`.

The admin server (design 1.5 "The admin connection", "Fencing", "Order inside one connection"):
- a connection is accepted only from the `_levain_control` uid, read from the kernel; any other peer is closed unread.
- framing and answer shape are `levain-control/1`'s: a 4-byte big-endian length and one canonical JSON object,
  `{"v":1,"op","id",...}` in, `{"id","ok":true,...}` or `{"id","ok":false,"refused",...}` out. A frame that is not
  one closes the connection, which is its EOF.
- each request runs as its own task carrying the admin epoch of its connection, so answers go out in any order; two
  requests naming one session run in frame order, the order assigned in the read loop with no `await` between the
  read and the append to that session's chain.
- FRONT-REGISTER / FRONT-ARM / DROP-SESSION are the fenced table calls; RELOAD is refused until a dispatch map exists.
- a newer connection PREEMPTS in five steps: bump the epoch and stop reading the old one; cancel its request tasks and
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
import errno
import functools
import logging
import os
import pwd
import resource
import signal
import socket
import stat
import sys
from typing import Dict, List, Optional, Sequence, Set, Tuple, Union

from levain.broker import gwwire
from levain.broker.creds import CredLifecycle, FakeCredLifecycle
from levain.broker.gwwire import FrameReader, KeysSide, WireError
from levain.broker.peer import PeerUnknown, peer_of, peer_uid
from levain.broker.upstream import Upstream, _note
from levain.contracts import ContractError, canon
from levain.contracts.grammar import JSON_INT_MAX

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
#: shutdown's awaits (the listeners' closure, later the exchange tasks) share ONE deadline.
SHUTDOWN_WAIT_S = 3.0

#: preemption's step (2): the old connection's request tasks are awaited this long after their cancel (DECISION,
#: judged: a task's work is memory operations plus closing sockets); one still running is left to its epoch check.
PREEMPT_WAIT_S = 2.0
#: `levain-control/1`'s frame cap, which the admin wire shares.
ADMIN_FRAME_MAX = 64 * 1024
#: once an admin frame's length is in, its body must arrive within this (the connection idles legitimately only
#: BETWEEN frames).
ADMIN_FRAME_S = 10.0
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

_K = gwwire.encode(b"K")
_A = gwwire.encode(b"A")


def bounded() -> int:
    return sum(NOFILE_TERMS.values())


# --- the table ------------------------------------------------------------------------------------------------------

class Registration:
    """One front: FRONT-REGISTER's fields, and its admission state REGISTERED -> ARMED -> CLAIMED (-> LOST when its
    STATUS connection ends without keys' `K`)."""

    __slots__ = ("session", "front_uid", "pid", "start", "incarnation", "reg_token", "state", "armed_at",
                 "status", "pending", "exchanges")

    def __init__(self, session: str, front_uid: int, pid: int, start: int, incarnation: int, reg_token: int) -> None:
        self.session, self.front_uid, self.pid, self.start = session, front_uid, pid, start
        self.incarnation, self.reg_token = incarnation, reg_token
        self.state = "registered"
        self.armed_at: Optional[float] = None
        self.status: Optional[Conn] = None          # the STATUS candidate, then the STATUS connection
        self.pending: Set[Conn] = set()             # exchange candidates that have sent no frame yet
        self.exchanges: Set[Conn] = set()           # connections past `Q`

    def fields(self) -> tuple:
        return (self.session, self.front_uid, self.pid, self.start, self.incarnation)


class Conn:
    """One accepted connection attributed to a registration. A connection past `Q` IS the exchange (design 1.2's
    `exchange`, 1.5's member of a registration's exchange set): `reg` is the sketch's `exchange.registration`, and
    `upstream` holds the exchange's `Upstream` from `open_exchange`'s store until the release drops it, so a phase 1
    always finds what it must abort."""

    __slots__ = ("writer", "reg", "kind", "task", "detached", "upstream")

    def __init__(self, writer: asyncio.StreamWriter, reg: Registration, kind: str) -> None:
        self.writer, self.reg, self.kind = writer, reg, kind   # kind: candidate | status | pending | exchange
        self.task: Optional[asyncio.Task] = asyncio.current_task()
        self.detached = False
        self.upstream: Optional[Upstream] = None


#: one entry of a phase 1's detached-objects list: a connection to close, or a detached exchange's `Upstream` to abort
Victim = Union[Conn, Upstream]


class AdminConn:
    """One accepted admin connection: the epoch it was adopted on, its read loop's task, its request tasks, and per
    session the last request task read for that session (the tail of that session's chain)."""

    __slots__ = ("reader", "writer", "epoch", "task", "tasks", "last", "preempted")

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, epoch: int) -> None:
        self.reader, self.writer, self.epoch = reader, writer, epoch
        self.task: Optional[asyncio.Task] = asyncio.current_task()
        self.tasks: Set[asyncio.Task] = set()
        self.last: Dict[str, asyncio.Task] = {}
        #: set by the preemption that replaces this connection, before it cancels the read loop: the EOF path then
        #: does nothing, and the preemption closes the connection itself, after its clear.
        self.preempted = False


class AdminMalformed(Exception):
    """An admin frame that is not a `levain-control/1` request: the connection is closed, which is its EOF."""


async def read_admin_frame(reader: asyncio.StreamReader) -> Optional[Tuple[int, str, dict]]:
    """The next request as `(id, op, args)`, or None at a clean EOF between frames; anything else that is not a
    canonical `{"v":1,"op":<string>,"id":<uint>,...}` object within ADMIN_FRAME_MAX raises AdminMalformed."""
    try:
        head = await reader.readexactly(4)
    except asyncio.IncompleteReadError as e:
        if not e.partial:
            return None
        raise AdminMalformed("EOF inside a frame") from None
    n = int.from_bytes(head, "big")
    if n == 0 or n > ADMIN_FRAME_MAX:
        raise AdminMalformed(f"frame length {n}")
    try:
        data = await asyncio.wait_for(reader.readexactly(n), ADMIN_FRAME_S)
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
                 cred_lifecycle: Optional[CredLifecycle] = None, control_uid: Optional[int] = None) -> None:
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
        a task; a later trigger is a no-op."""
        self._in_loop()
        self.closing = True
        if self._closer is None:
            self._closer = asyncio.ensure_future(self._close())

    async def shutdown(self) -> None:
        """Trigger, then await the one close's completion (a later call awaits the same completion and returns)."""
        self.trigger()
        assert self._done is not None
        await self._done.wait()

    async def _close(self) -> None:
        """Wait for a start() in progress to return (its only awaits are its server creations), then stop accepting
        and close the listeners, then await their closure (bounded)."""
        assert self._done is not None
        try:
            if self._starting is not None:
                await self._starting.wait()
            for s in self.servers:          # (1) the listeners' synchronous close: no accept after this
                s.close()
            closures = {asyncio.ensure_future(s.wait_closed()) for s in self.servers}
            waitset = closures
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
                victims = self._detach(reg)
        self._effects(victims)
        return {}

    # -- transitions --

    def _detach(self, reg: Registration) -> List[Victim]:
        """Phase 1 (caller holds state_lock; no await): take `reg` out of every table and return every connection
        attributed to it, each marked detached, preceded by the `Upstream` OBJECT of each detached exchange that holds
        one (phase 2 aborts it: an abort drops the transport's buffer, where a close would flush credential-bearing
        bytes); an exchange's task is cancelled (delivery only, never the guarantee)."""
        if self.regs.get(reg.front_uid) is reg:
            del self.regs[reg.front_uid]
        if self.by_session.get(reg.session) is reg:
            del self.by_session[reg.session]
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
        `Upstream` ahead of every connection. `reg_high` is kept: a clear never lowers a fence."""
        regs = {id(r): r for r in list(self.regs.values()) + list(self.by_session.values())}
        got: List[Victim] = []
        for reg in regs.values():
            got += self._detach(reg)
        return [v for v in got if isinstance(v, Upstream)] + [v for v in got if not isinstance(v, Upstream)]

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
                        writer.close()

    async def _adopt(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> Optional[AdminConn]:
        """Adopt a new admin connection; with one adopted already, preempt it first, in the design's five steps."""
        assert self.admin_lock is not None and self.state_lock is not None
        async with self.admin_lock:
            if self.closing:
                return None
            old = self.admin
            self.admin_epoch += 1                    # (1) every task of the old connection is stale from here
            epoch = self.admin_epoch
            if old is not None:
                old.preempted = True
                wait: Set[asyncio.Task] = set(old.tasks)
                if old.task is not None:
                    old.task.cancel()                # (1) stop reading it
                    wait.add(old.task)
                for t in old.tasks:                  # (2) cancel its request tasks, then await them, bounded
                    t.cancel()
                if wait:
                    await asyncio.wait(wait, timeout=PREEMPT_WAIT_S)
                async with self.state_lock:          # (3) the fail-closed clear, phase 1
                    victims = self._detach_all()
                self._effects(victims)               # (3) phase 2
                try:
                    old.writer.close()               # (4)
                except Exception as e:
                    _note(logging.WARNING, "levain gw-keys: closing a preempted admin connection failed (%s)",
                          type(e).__name__)
            if self.closing:
                return None
            conn = AdminConn(reader, writer, epoch)  # (5) adopt; it starts against an empty table
            self.admin = conn
            return conn

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
        and write the answer only while its connection's epoch is current. A failure inside keys' own call closes
        the connection: its EOF clears everything, never a table left as the failure left it."""
        if prev is not None and not prev.done():
            await asyncio.wait({prev})
        try:
            answer = await self._admin_answer(conn.epoch, rid, op, args)
        except Exception:
            conn.writer.close()
            raise
        if answer is None or conn.epoch != self.admin_epoch:
            return   # stale, or refused by the shutdown latch: no answer
        conn.writer.write(encode_admin(answer))

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
                await self._exchange(conn, frames, side)
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

    async def _exchange(self, conn: Conn, frames: FrameReader, side: KeysSide) -> None:
        reg = conn.reg
        assert self.state_lock is not None
        async with self.state_lock:
            reg.pending.discard(conn)
            if conn.detached:
                return
            if (self.regs.get(reg.front_uid) is reg and reg.state == "claimed" and not self.closing
                    and len(reg.exchanges) < self.exchanges_per_session):
                conn.kind = "exchange"
                reg.exchanges.add(conn)
        # Until the real `Q` handler exists every `Q` is answered `keys-unavailable`, through the one `F` path.
        await self._refuse(conn, frames, side, "keys-unavailable", UNTRUSTED_DRAIN_CAP)

    async def _refuse(self, conn: Conn, frames: FrameReader, side: KeysSide, name: str, cap: int) -> None:
        """The `F` path: `F`, SHUT_WR, then read and discard the front's frames until `E`, `X`, EOF, `cap` body bytes
        read after the `F`, or DRAIN_S; the caller closes. So `F` is never cut off by a close with unread bytes."""
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
        for group in (FRONTS_GROUP, CONTROL_ACCOUNT):
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
                control_uid=control_uid)
    front_path = os.path.join(a.runtime_root, "keys", "front.sock")
    admin_path = os.path.join(a.runtime_root, "keys", "admin.sock")
    try:
        return asyncio.run(_serve(keys, front_path, gid, admin_path, admin_gid))
    except FileExistsError as e:
        print(f"levain gw-keys: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
