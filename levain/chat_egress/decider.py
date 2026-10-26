"""The chat-egress decider service: srt 0.0.81's decider protocol, answered for one chat launch (design §3.8.5(o)
item 2, C-3, R-1 .. R-10, *At close*; CH-D2 S5b, the composer).

srt (sandbox-runtime, through Levain's glue `node/glue.mjs`) asks about every request on one socket and reports what
became of each allowed one on a second (the observation channel, `wire.parse_obs_line`). This module answers the
first and drains the second, and composes, per request, in this order (the first step that decides, decides):
  1. `parse.parse` (R-1): the one parse. A frame it refuses gets a 400 deny (no record row can carry it: no request
     id survives the refusal; it is logged and counted).
  2. the audit value (item 2): no one well-formed `x-levain-audit` -> deny `audit-missing`; a value already
     spent this launch, or a new one once `max_audit_ids` are spent (503 `audit_cap`) -> denied, no row.
  3. the ONE route lookup (`keys.lookup`, R15-18-L1) and the launch's mode, read live (`LiveSetting`, C1) and held as
     a ratchet from the mode the launch started with (`Capped`, Q-R15-9: a tightening applies live and stays for the
     launch; a loosening, undoing a tightening included, never applies to it; a failed or timed-out read gates
     that request only and does not move the hold).
  4. `parse.walls` (R-2 .. R-4), then R-5's header strip (`strip.strip`; too many credential names -> deny).
  5. C-3: `resolver.Resolver` (bounded, timed out) and `guard.address_wall`: any non-public answer walls
     `non_public_address` BEFORE any card (R15-10-A4); no answer denies.
  6. the body, when the request needs one seen: an upload-pack exchange whose advertisement is drained (item 6) or a
     request the classifier cards that carries a body. One `need_body` per request, max CARD_BODY_CAP decoded bytes;
     the body is hashed as spooled; srt sends nothing upstream before the verdict (it holds the body for it). Past
     the cap -> 503 `body_cap`; no body frame inside BODY_TIMEOUT_S -> 408 `body_timeout`; past MAX_BODY_READERS
     concurrent spools -> 503 `body_readers` (R15-10-A7, R9-A4). An upload-pack body over
     `advert.UPLOAD_PACK_BODY_CAP` decoded bytes is `cut` for the predicate (R15-19-2 / R15-19-3): a card.
  7. `classify.decide`, with the observation channel DRAINED under the same lock, so no observation already in the
     socket is missing from the advertisement set at the decision and no drain runs between the drain and the
     read (R15-19-1).
  8. silent -> `keys.decide_key` (R-6) and the allow frame, BUILT (and validated by `wire`) before the ALLOW event
     fires, so every deny check has run before an allow exists; card -> at most MAX_CARDS pending per launch
     (past it 503 `card_cap`, never queued), `cards.card_input` + the desk, held EGRESS_CARD_HOLD_S, the key decided
     at the answer; wall, or any other kind -> deny.
Every request the decider is asked about runs on its own thread (a held card never stalls another request), at most
MAX_DECISIONS at once (past it the request is denied 503 `decider_busy` on the link thread, with its row).

The per-request machine is `lifecycle.RequestLifecycle` (C0); its effects are performed here, the rows through
`records.EgressRecords`. The rules the 1024-20 handoff carries for this composer:
  * a stray ALLOW (an allow for a request whose state cannot take one) is logged and DENIED, never dropped (srt would
    hold the request until its own timer);
  * CLOSE fires only after the observation channel is drained to its end (the glue closes it at its clean end) or
    the stop bound passes, so an `ended` or `resp` already written is recorded as itself, never as a close;
  * the verdict frame for a decision is a deny unless every deny check has passed (step 8);
  * RELEASE (a pending request whose client left) is a deny frame for srt's still-waiting id;
  * after FAILED the caller owes srt an answer: when no verdict or release is among the effects done, a deny is sent
    here. When SEND_ALLOW / SEND_DENY / RELEASE itself raised, the bytes of the frame that LEFT the process are
    counted (`SendFailed.sent`): none -> the deny is owed and sent; the whole frame -> it left (an allow reads
    AMBIGUOUS in the records); part of it -> the link is cut (srt then refuses every request, fail closed).

Totality (R15-12-B8): every exception in a decision ends in a deny for that one request; the link keeps serving.
A protocol violation from srt (a bad frame) cuts the link: srt's own rule then refuses every request (fail closed).

Nothing here logs a path, a query value, a header value or a credential (R15-17-8): ids, reasons and counts only.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import queue as queue_mod
import select
import socket
import struct
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from functools import partial
from typing import Any, Callable, Protocol

from levain.chat_egress import advert, cards, classify, guard, keys, parse, shape, strip, wire
from levain.chat_egress import records as rec
from levain.chat_egress.lifecycle import EGRESS_CARD_HOLD_S, Effect, Event, RequestLifecycle, State, Step, PENDING
from levain.chat_egress.resolver import Resolver, ResolveError

log = logging.getLogger("levain.chat_egress.decider")

#: the card step's reason when an upload-pack body was past advert.UPLOAD_PACK_BODY_CAP (K1 counts it as a cap card)
UPLOAD_PACK_OVER_CAP = "upload_pack_over_cap"
THREAD_DECIDE = "levain-egress-decide"
THREAD_LINK = "levain-egress-link"
THREAD_OBS = "levain-egress-obs"


@dataclass(frozen=True)
class Limits:
    """The decider's caps. [seat] values, each named in RETURN for the head: the design names the caps, not these
    numbers, except the hold (lifecycle's EGRESS_CARD_HOLD_S) and srt's protocol maxima (`wire`)."""
    max_decisions: int = 64              # decision threads at once
    max_cards: int = 8                   # cards pending per launch (R9-A4)
    max_body_readers: int = 8            # concurrent spools (R15-10-A7)
    card_body_cap: int = 4 << 20         # one spool's decoded bytes; MAX_BODY_READERS x this <= srt's 64 MiB budget
    body_timeout_s: float = 30.0         # a spool's whole deadline (the only one srt's protocol lets the decider see)
    card_hold_s: float = EGRESS_CARD_HOLD_S
    card_poll_s: float = 0.5
    stop_drain_s: float = 3.0            # close: how long the obs channel may take to reach its end
    early_ended_max: int = 4096          # `ended` lines seen before their request frame
    # DECISION (L3 r2 FIX 2): audit values kept for the launch (bound, never evict: an evicted value could be
    # reused). One entry is a 36-character v4 UUID str in a set: ~118 bytes measured (tracemalloc, CPython 3.12,
    # 2**17 entries), so this cap holds ~15 MiB. Past it every new request is denied 503 `audit_cap`.
    max_audit_ids: int = 1 << 17


#: A body frame is at most srt's 32 MiB as base64 plus its JSON; a frame past this is a protocol violation.
MAX_IN_FRAME = (wire.MAX_BODY * 4) // 3 + (64 << 10)
_OBS_LINE_MAX = 64 << 10
_FORM = "application/x-www-form-urlencoded"


# --- the link (srt's framing) --------------------------------------------------------------------------------------

class LinkDead(Exception):
    """The decider link is gone (srt closed it, or this end cut it)."""


class SendFailed(LinkDead):
    """A frame's send raised after `sent` of its `total` bytes left this process (were taken by the kernel)."""

    def __init__(self, sent: int, total: int, why: str) -> None:
        super().__init__(f"send failed after {sent} of {total} bytes: {why}")
        self.sent, self.total = sent, total


class ProtocolViolation(Exception):
    """srt sent something its protocol does not have."""


def _strict_object(raw: bytes) -> dict:
    def hook(pairs):
        d: dict = {}
        for k, v in pairs:
            if k in d:
                raise ProtocolViolation(f"duplicate key {k!r}")
            d[k] = v
        return d

    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=hook)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolViolation(f"not one JSON object: {type(exc).__name__}") from None
    if not isinstance(obj, dict):
        raise ProtocolViolation("not one JSON object")
    return obj


class Link:
    """One end of srt's decider socket. Reads are the link thread's alone; sends are serialised under the write lock.
    `kill` takes only the dead-state lock, never the write lock, so a send blocked on a full buffer (srt stopped
    reading) is interrupted by the kill's shutdown instead of holding the kill off. A send that fails after part of
    its frame left cuts the link at once: the stream is misframed, and nothing may follow on it. `fault` is a test
    seam: `fault(frame_len) -> int | None`; an int N makes the send raise after N bytes left (S8 passes none)."""

    def __init__(self, sock: socket.socket, *, fault: Callable[[int], int | None] | None = None) -> None:
        self._sock = sock
        self._wlock = threading.Lock()
        self._slock = threading.Lock()       # `_dead` only
        self._fault = fault
        self._dead: str | None = None
        self._buf = b""

    @property
    def dead(self) -> str | None:
        with self._slock:
            return self._dead

    def _exact(self, n: int) -> bytes | None:
        have = len(self._buf)
        if have < n:
            parts = [self._buf]
            while have < n:
                try:
                    chunk = self._sock.recv(max(65536, n - have))
                except OSError:
                    return None
                if not chunk:
                    return None
                parts.append(chunk)
                have += len(chunk)
            self._buf = b"".join(parts)
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def read(self) -> dict | None:
        """The next frame, or None at the end of the stream. ProtocolViolation for a frame srt's protocol lacks."""
        head = self._exact(4)
        if head is None:
            return None
        (n,) = struct.unpack(">I", head)
        if n > MAX_IN_FRAME:
            raise ProtocolViolation(f"a {n}-byte frame")
        body = self._exact(n)
        if body is None:
            return None
        return _strict_object(body)

    def send_bytes(self, frame: bytes) -> None:
        with self._wlock:
            dead = self.dead
            if dead is not None:
                raise SendFailed(0, len(frame), f"the link is dead ({dead})")
            limit = self._fault(len(frame)) if self._fault is not None else None
            view = memoryview(frame)
            sent = 0
            try:
                while sent < len(frame):
                    if limit is not None and sent >= limit:
                        raise OSError("test seam: the send fails here")
                    end = len(frame) if limit is None else max(sent + 1, min(len(frame), limit))
                    sent += self._sock.send(view[sent:end])
            except OSError as exc:
                if 0 < sent < len(frame):
                    self.kill("a frame was cut mid-send")
                raise SendFailed(sent, len(frame), type(exc).__name__) from None

    def send(self, msg: dict) -> None:
        """Encode (a WireError leaves before any byte), then send."""
        self.send_bytes(wire.encode_frame(msg))

    def kill(self, why: str) -> None:
        with self._slock:
            first = self._dead is None
            if first:
                self._dead = why
        if first:
            log.error("decider link cut: %s", why)
        for fn in (lambda: self._sock.shutdown(socket.SHUT_RDWR), self._sock.close):
            try:
                fn()
            except OSError:
                pass


# --- C1: the standing default, read live --------------------------------------------------------------------------

class LiveSetting:
    """A launch setting read live per request (C1: the standing `open network` default). `read()` returns the
    current value. Single flight: one read at a time; a request is answered only by a read that STARTED at or after
    it arrived (a revoke that landed before the request is seen by it); no answer inside `timeout_s`, an error, or
    a value outside `allowed` -> `fail_closed`. After `close()` every answer is `fail_closed` and no read starts:
    a read finishing after the close arms nothing, and a `get()` waiting on a read is woken by the close.
    `get_ex()` answers the same value paired with whether it came from a real read (an in-`allowed` value the read
    returned) rather than `fail_closed` standing in for one (timeout, error, out-of-`allowed` value, closed).
    `reader` is the read thread while it lives (the decider's close reports it: a read cannot be cancelled).
    `watch(fn)`: `fn` gets every ACCEPTED real read's value on the read thread before any `get()` waiting on that
    read is woken; reads run one at a time, so `fn` sees them in the order they started (`Capped` ratchets there).
    A read is accepted when it finishes before the deadline of a `get()` it answers, and exactly the `get()`s whose
    deadline it beat answer it as real; a read that finishes after every such deadline reached no request and is
    passed to no watcher (L3 verify r2, codex: a late gated answer must not gate the rest of the launch)."""

    def __init__(self, read: Callable[[], str], *, allowed: tuple[str, ...] = classify.MODES,
                 fail_closed: str = classify.GATED, timeout_s: float = 2.0) -> None:
        self._read, self._allowed, self._closed_value, self._timeout = read, allowed, fail_closed, timeout_s
        self._lock = threading.Lock()
        self._flight: tuple[float, threading.Event, dict] | None = None
        self._thread: threading.Thread | None = None
        self._closed = False
        self._watchers: list[Callable[[str], None]] = []

    def watch(self, fn: Callable[[str], None]) -> None:
        """Register before the first read. `fn` runs on the read thread while this setting's lock is held, so it must
        not call back into this setting (`get`, `get_ex`, `close`): the lock is not reentrant and that call deadlocks."""
        self._watchers.append(fn)

    @property
    def reader(self) -> threading.Thread | None:
        t = self._thread
        return t if t is not None and t.is_alive() else None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._flight is not None:
                self._flight[1].set()        # wakes every waiting get(); each then reads `_closed`

    def _start(self, now: float, until: float) -> tuple[float, threading.Event, dict]:
        # box["until"]: the latest deadline among the get()s this read answers (raised as they join, under _lock).
        done, box = threading.Event(), {"until": until}

        def work() -> None:
            try:
                box["v"] = self._read()
            except BaseException as exc:     # noqa: BLE001
                box["e"] = exc
            finally:
                # Accept, watch and wake under _lock: a get() decides its own timeout under it too, so a read is
                # accepted only while some get() can still answer it.
                with self._lock:
                    try:
                        v, at = box.get("v"), time.monotonic()
                        if (not self._closed and "e" not in box and v in self._allowed and at < box["until"]):
                            box["at"] = at
                            for fn in list(self._watchers):
                                fn(v)
                    finally:
                        done.set()

        self._flight = (now, done, box)
        self._thread = threading.Thread(target=work, name="levain-egress-setting", daemon=True)
        self._thread.start()
        return self._flight

    def get(self) -> str:
        return self.get_ex()[0]

    def get_ex(self) -> tuple[str, bool]:
        closed = (self._closed_value, False)
        arrived = time.monotonic()
        deadline = arrived + self._timeout
        while True:
            with self._lock:
                if self._closed:
                    return closed
                f = self._flight
                if f is None or f[1].is_set():
                    f, mine = self._start(time.monotonic(), deadline), True
                else:
                    mine = f[0] >= arrived
                    if mine:
                        f[2]["until"] = max(f[2]["until"], deadline)
            waited = f[1].wait(max(0.0, deadline - time.monotonic()))
            with self._lock:
                if self._closed:
                    return closed
                if mine:
                    at = f[2].get("at")
                    return (f[2]["v"], True) if at is not None and at < deadline else closed
            if not waited or time.monotonic() >= deadline:
                return closed


STRICTEST_FIRST = (classify.GATED, classify.OPEN_NETWORK)
"""The modes, strictest first: `Capped` answers whichever of two values comes first here."""


def _stricter(a: str, b: str) -> str:
    rank = {m: i for i, m in enumerate(STRICTEST_FIRST)}
    return min(a, b, key=lambda m: rank.get(m, -1)) if a in rank and b in rank else STRICTEST_FIRST[0]


class Capped:
    """The decider's mode per request (Q-R15-9: "tightening the standing default applies live; loosening applies from
    the next launch"), a ratchet: it holds the strictest mode seen for the launch's life, starting at ``launched``
    (the mode the launch started with), and each `get()` answers the stricter of the live setting's answer and that
    held mode, and holds the answer. A tightening applies live; undoing it is a loosening and never applies to this
    launch. Only a REAL read moves the hold (`get_ex()`'s flag): an answer that stands in for a failed read (a
    timeout, an error, a value outside the allowed modes) answers the strictest mode for that `get()` alone. A
    setting with no `get_ex` has every read counted real. A value outside `STRICTEST_FIRST` on either side answers
    the strictest mode. `get()` is called from concurrent decision threads: the hold is updated under a lock (the
    live read happens outside it). A setting with `watch` (`LiveSetting`) also moves the hold as each accepted real
    read finishes, before that read answers anyone and before the next read can start, so no `get()` answers looser
    than a read that started before its own (L3 r1, codex 1: a later read reaching the lock first). A read that
    finished after every request waiting on it timed out answered no one and moves nothing (L3 verify r2)."""

    def __init__(self, live: Any, launched: str) -> None:
        self._live = live
        self.launched = launched if launched in STRICTEST_FIRST else STRICTEST_FIRST[0]
        self._held = self.launched
        self._lock = threading.Lock()
        watch = getattr(live, "watch", None)
        if watch is not None:
            watch(self._ratchet)

    def _ratchet(self, live: str) -> None:
        with self._lock:
            self._held = _stricter(live, self._held)

    @property
    def reader(self) -> threading.Thread | None:
        return getattr(self._live, "reader", None)

    def get(self) -> str:
        get_ex = getattr(self._live, "get_ex", None)
        live, real = get_ex() if get_ex is not None else (self._live.get(), True)
        with self._lock:
            if not real:
                return STRICTEST_FIRST[0]
            self._held = _stricter(live, self._held)
            return self._held

    def close(self) -> None:
        self._live.close()


class Fixed:
    """A setting that does not change for the launch."""

    reader = None

    def __init__(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def get_ex(self) -> tuple[str, bool]:
        return self.value, True

    def close(self) -> None:
        pass


# --- the card desk --------------------------------------------------------------------------------------------------

class CardDesk(Protocol):
    def file(self, card: dict) -> str: ...            # -> the queue's request id
    def state(self, card_id: str) -> str | None: ...  # the queue state ("pending", "allowed", "denied", ...)
    def end(self, card_id: str) -> None: ...          # the request ended: the card can no longer be answered


class QueueCardDesk:
    """The production desk: `cards.file_card` into the launch's consent queue; the answer read from the queue."""

    def __init__(self, queue: Any, *, agent_session_id: str, cwd: str) -> None:
        self._queue, self._session, self._cwd = queue, agent_session_id, cwd

    def file(self, card: dict) -> str:
        return cards.file_card(self._queue, card, agent_session_id=self._session, cwd=self._cwd).id

    def state(self, card_id: str) -> str | None:
        got = self._queue.get(card_id)
        return None if got is None else got[1]

    def end(self, card_id: str) -> None:
        self._queue.withdraw(card_id, note="the request ended before an answer")


# --- one request ----------------------------------------------------------------------------------------------------

class _Req:
    __slots__ = ("srt_id", "frame", "lc", "parsed", "rid", "route", "strip", "verdict", "key", "step_name",
                 "deny", "allow", "body", "body_cut", "body_sha", "wake", "card_lock", "card_id", "card_ended",
                 "obs", "ct_class", "mode", "thread")

    def __init__(self, srt_id: int, frame: dict) -> None:
        self.srt_id, self.frame = srt_id, frame
        self.lc = RequestLifecycle()
        self.parsed: parse.ParsedRequest | None = None
        self.rid: str | None = None
        self.route: keys.RouteSnapshot | None = None
        self.strip: strip.StripResult | None = None
        self.verdict: classify.Verdict | None = None
        self.key: keys.KeyDecision | None = None
        self.step_name = "undecided"
        self.deny: tuple[int, str] = (403, "denied")
        self.allow: bytes | None = None
        self.body: bytes | None = None
        self.body_cut: bool | None = None
        self.body_sha: str | None = None
        self.wake = threading.Event()
        self.card_lock = threading.Lock()
        self.card_id: str | None = None
        self.card_ended = False
        self.obs: wire.ObsResponse | None = None
        self.ct_class: str | None = None
        self.mode = classify.GATED
        self.thread: threading.Thread | None = None


_ANSWERS = (Effect.SEND_ALLOW, Effect.SEND_DENY, Effect.RELEASE)


class Decider:
    """One launch's decider service over srt's decider socket and the glue's observation socket.

    launch_id    the chat launch (the rows' launch id; a `launches` row of the dispatch store)
    records      `records.EgressRecords` over that dispatch store
    routes       the launch's `keys.RouteSet` (built once, at launch)
    mode         a `LiveSetting` / `Fixed` of `classify.GATED` or `classify.OPEN_NETWORK`, held as `Capped` (a
                 ratchet, Q-R15-9) starting at `launch_mode` (required): the mode the launch started with, as
                 passed by the caller (`launch.start_egress` passes the read its header and digest came from); the
                 constructor never reads `mode`
    liveness     `keys.AuthorityLiveness` (or any `keys.Liveness`); credentials: `keys.LaunchCredentials`
    cards        a `CardDesk`; resolver: a `resolver.Resolver`
    """

    def __init__(self, link: socket.socket, obs: socket.socket, *, launch_id: str, records: rec.EgressRecords,
                 routes: keys.RouteSet, mode: Any, liveness: Any, credentials: keys.LaunchCredentials,
                 cards: CardDesk, resolver: Resolver, limits: Limits = Limits(),
                 link_fault: Callable[[int], int | None] | None = None, launch_mode: str) -> None:
        self.link = Link(link, fault=link_fault)
        self._obs = obs
        self.launch_id, self.records, self.routes = launch_id, records, routes
        self.mode = Capped(mode, launch_mode)
        self.liveness, self.credentials, self.cards, self.resolver = liveness, credentials, cards, resolver
        self.limits = limits
        self.adverts = advert.AdvertisementSet()
        self._obs_lock = threading.Lock()
        self._obs_buf = b""
        self._obs_eof = threading.Event()
        self._live: dict[str, _Req] = {}
        self._live_lock = threading.Lock()
        self._early_ended: OrderedDict[str, None] = OrderedDict()
        self._bodies: dict[int, queue_mod.Queue] = {}
        self._decisions = threading.BoundedSemaphore(limits.max_decisions)
        self._card_slots = threading.BoundedSemaphore(limits.max_cards)
        self._readers = threading.BoundedSemaphore(limits.max_body_readers)
        # `_live_lock` also covers `_closing`'s set, admission (`_threads`, `_active`) and `_spent`: close() stops
        # admission under it, so its snapshot holds every decision thread there will be. Admission takes
        # `_decisions` with a non-blocking acquire under it; a blocking acquire there would hold close() off.
        self._threads: set[threading.Thread] = set()
        self._active: set[_Req] = set()          # every request with a decision thread, live or not
        self._spent: set[str] = set()            # every audit value this launch registered (one request each)
        self._closing = threading.Event()
        self._link_thread: threading.Thread | None = None
        self._obs_thread: threading.Thread | None = None
        self.counters: dict[str, int] = {"unparsed_frames": 0, "stray_obs": 0, "stray_allow": 0, "late_bodies": 0,
                                         "audit_cap": 0}

    # --- service ----------------------------------------------------------------------------------------------
    def start(self) -> None:
        self._link_thread = threading.Thread(target=self._link_loop, name=THREAD_LINK, daemon=True)
        self._obs_thread = threading.Thread(target=self._obs_loop, name=THREAD_OBS, daemon=True)
        self._link_thread.start()
        self._obs_thread.start()

    def close(self, timeout_s: float | None = None) -> bool:
        """The launch end. New frames are denied; the obs channel is drained to its end (or `stop_drain_s`); the
        link is cut (a verdict send blocked on srt's full buffer then fails, releasing its request's transition lock);
        THEN every request still open gets CLOSE; card waits are woken; every thread of this service is joined. True
        when every thread ended inside the bound."""
        bound = time.monotonic() + (timeout_s if timeout_s is not None else self.limits.stop_drain_s + 5.0)
        with self._live_lock:
            self._closing.set()                  # admission stops here: no decision thread starts after it
        self._obs_eof.wait(max(0.0, min(self.limits.stop_drain_s, bound - time.monotonic())))
        self.drain()
        with self._live_lock:
            open_reqs = list(self._live.values())
            waiting = list(self._active)
            threads = list(self._threads)
            capped = self.counters["audit_cap"]
        self.link.kill("the launch closed")
        for r in open_reqs:
            self._fire(r, Event.CLOSE)
        for r in open_reqs + waiting:
            r.wake.set()
        self.mode.close()
        if capped:
            log.error("launch %s: %d requests denied audit_cap (max_audit_ids %d)", self.launch_id, capped,
                      self.limits.max_audit_ids)
        for s in (self._obs,):
            try:
                s.close()
            except OSError:
                pass
        everything = [self._link_thread, self._obs_thread, *threads, getattr(self.mode, "reader", None)]
        for t in everything:
            if t is not None:
                t.join(max(0.0, bound - time.monotonic()))
        return not any(t is not None and t.is_alive() for t in everything)

    # --- the link thread ----------------------------------------------------------------------------------------
    def _link_loop(self) -> None:
        try:
            hello = self.link.read()
            if hello is None:
                return
            if hello != {"t": "hello", "proto": 1}:
                raise ProtocolViolation("the first frame is not srt's hello")
            self.link.send({"t": "hello", "proto": 1, "allowedDomains": [], "deniedDomains": []})
            while True:
                msg = self.link.read()
                if msg is None:
                    return
                t = msg.get("t")
                if t == "req":
                    self._on_req(msg)
                elif t == "body":
                    self._on_body(msg)
                else:
                    raise ProtocolViolation(f"a frame of kind {str(t)[:20]!r}")
        except ProtocolViolation as exc:
            self.link.kill(f"protocol violation from srt: {exc}")
        except LinkDead:
            pass
        except BaseException:                 # noqa: BLE001: the link thread never dies silently
            log.exception("decider link thread failed")
            self.link.kill("the link thread failed")

    def _on_req(self, frame: dict) -> None:
        srt_id = frame.get("id")
        if type(srt_id) is not int or not 1 <= srt_id <= 2 ** 53 - 1:
            raise ProtocolViolation("a req frame without srt's id")
        r = _Req(srt_id, frame)
        with self._live_lock:
            if self._closing.is_set():
                refused = (503, "closing", "decider: the launch is closing")
            elif not self._decisions.acquire(blocking=False):
                refused = (503, "decider_busy", "wall: decider_busy")
            else:
                t = threading.Thread(target=self._decide, args=(r,), name=THREAD_DECIDE, daemon=True)
                r.thread = t
                self._threads = {x for x in self._threads if x.is_alive()}   # close joins every one still alive
                self._threads.add(t)
                self._active.add(r)
                try:
                    t.start()
                except BaseException:
                    self._threads.discard(t)
                    self._active.discard(r)
                    self._decisions.release()
                    raise
                return
        self._inline(r, *refused)

    def _inline(self, r: _Req, status: int, step_name: str, reason: str) -> None:
        """A deny decided on the link thread (no decision thread): parsed for its id, recorded, sent."""
        try:
            self._parse(r)
        except _Done:
            return
        r.step_name, r.deny = ("audit-missing" if r.rid is None else step_name), (status, reason)
        try:
            r.route = keys.lookup(self.routes, r.parsed.canonical_host)
        except Exception:                      # noqa: BLE001: the row's path shape only; the deny stands
            r.route = None
        try:
            self._register(r, closing_ok=True)
        except _Duplicate:
            return self._owe_deny(r, 403, "decider: a second request with one audit value")
        except _AuditCap:
            return self._owe_deny(r, 503, "wall: audit_cap")
        if r.lc.state in PENDING:
            self._fire(r, Event.DENY)

    def _on_body(self, msg: dict) -> None:
        if set(msg) - {"t", "id", "data", "cut"} or not {"t", "id", "data"} <= set(msg) \
                or type(msg["id"]) is not int or not isinstance(msg["data"], str) \
                or ("cut" in msg and msg["cut"] is not True):
            raise ProtocolViolation("a body frame's fields")
        try:
            data = base64.b64decode(msg["data"], validate=True)
        except (binascii.Error, ValueError):
            raise ProtocolViolation("a body frame's base64") from None
        cut = bool(msg.get("cut"))
        if len(data) > self.limits.card_body_cap:   # past the need_body max: srt's own cut, whatever it said
            data, cut = b"", True
        q = self._bodies.get(msg["id"])
        if q is None:
            # a body for a request this end already denied (body_timeout raced srt's send): dropped
            self.counters["late_bodies"] += 1
            return
        try:
            q.put_nowait((data, cut))
        except queue_mod.Full:
            raise ProtocolViolation("a second body frame for one request") from None

    # --- the observation channel -----------------------------------------------------------------------------
    def _obs_loop(self) -> None:
        try:
            while not self._obs_eof.is_set():
                try:
                    ready, _, _ = select.select([self._obs], [], [], 0.2)
                except (OSError, ValueError):
                    break
                if ready:
                    self.drain()
        except BaseException:                 # noqa: BLE001
            log.exception("decider observation thread failed")
            self.link.kill("the observation thread failed")

    def drain(self) -> None:
        """Read every observation line already in the socket and apply it. Takes the observation lock; the
        decisions call it with the lock taken (`_drained`)."""
        with self._obs_lock:
            self._drain_locked()

    def _drain_locked(self) -> None:
        if self._obs_eof.is_set():
            return
        s = self._obs
        try:
            s.setblocking(False)
        except OSError:
            self._obs_eof.set()
            return
        try:
            while True:
                try:
                    chunk = s.recv(65536)
                except (BlockingIOError, InterruptedError):
                    break
                except OSError:
                    chunk = b""
                if not chunk:
                    self._obs_eof.set()
                    break
                self._obs_buf += chunk
                if len(self._obs_buf) > _OBS_LINE_MAX and b"\n" not in self._obs_buf:
                    self.link.kill("an observation line past its bound")
                    self._obs_eof.set()
                    self._obs_buf = b""
                    return
        finally:
            try:
                s.setblocking(True)
            except OSError:
                pass
        *lines, self._obs_buf = self._obs_buf.split(b"\n")
        for line in lines:
            try:
                o = wire.parse_obs_line(line + b"\n")
            except wire.WireError as exc:
                self.link.kill(f"the observation channel sent a line that is not wire's: {exc}")
                self._obs_eof.set()
                return
            self._on_obs(o)

    def _on_obs(self, o: wire.ObsResponse | wire.ObsEnded) -> None:
        with self._live_lock:
            r = self._live.get(o.request_id)
            if r is None and isinstance(o, wire.ObsEnded):
                self._early_ended[o.request_id] = None
                while len(self._early_ended) > self.limits.early_ended_max:
                    self._early_ended.popitem(last=False)
                return
        if r is None:
            self.counters["stray_obs"] += 1
            log.warning("an observation for a request not live here: %s", o.request_id)
            return
        if isinstance(o, wire.ObsResponse):
            r.obs = o
            v = r.verdict
            r.ct_class = (self.adverts.observe(r.parsed, o.status, o.content_type, verdict=v) if v is not None
                          else advert.content_type_class(o.content_type))
            self._fire(r, Event.RESPONSE)
        else:
            self._fire(r, Event.ENDED)        # before the wake: a woken wait reads the ended state, never its own
            r.wake.set()

    # --- one decision (its own thread) -------------------------------------------------------------------------
    def _decide(self, r: _Req) -> None:
        try:
            try:
                self._decide_inner(r)
            except _Done:
                pass
            except BaseException as exc:      # noqa: BLE001: totality (R15-12-B8)
                log.error("decision for srt id %s raised %s: denied", r.srt_id, type(exc).__name__)
                if r.lc.state in PENDING:
                    self._deny(r, "internal", 500, "decider: internal error")
                elif r.parsed is None:
                    self._owe_deny(r, 500, "decider: internal error")
        finally:
            with self._live_lock:
                self._active.discard(r)
            self._decisions.release()

    def _parse(self, r: _Req) -> None:
        try:
            r.parsed = parse.parse(r.frame)
        except parse.ParseError as exc:
            # no request id survives the refusal, so no row can be written for it: logged, counted, denied 400
            self.counters["unparsed_frames"] += 1
            log.warning("srt id %s: frame refused by the parse (%s)", r.srt_id, exc.reason)
            self._owe_deny(r, 400, f"decider: the request could not be parsed ({exc.reason})")
            raise _Done
        r.rid = r.parsed.request_id

    def _register(self, r: _Req, *, closing_ok: bool = False) -> None:
        """Make the request reachable by its observation lines; an `ended` that came first is applied now. An audit
        value is spent for the whole launch (`_Duplicate` for a second request with it, live or long done); with
        `max_audit_ids` spent, a new value is `_AuditCap` (no row: its value cannot be spent). After close() began,
        `_Closing` (close's snapshot did not see this request: its caller CLOSEs it)."""
        if r.rid is None:
            return
        with self._live_lock:
            if self._closing.is_set() and not closing_ok:
                raise _Closing
            if r.rid in self._spent:
                raise _Duplicate
            if len(self._spent) >= self.limits.max_audit_ids:
                self.counters["audit_cap"] += 1
                if self.counters["audit_cap"] == 1:
                    log.error("launch %s: %d audit values spent: every new request is denied audit_cap",
                              self.launch_id, len(self._spent))
                raise _AuditCap
            self._spent.add(r.rid)
            self._live[r.rid] = r
            early = self._early_ended.pop(r.rid, "absent") is None
        if early:
            self._fire(r, Event.ENDED)
            r.wake.set()

    def _decide_inner(self, r: _Req) -> None:
        self._parse(r)
        p = r.parsed
        if r.rid is None:
            return self._deny(r, "audit-missing", 403, "decider: the request carries no audit value")
        try:                                   # before _register: an early ENDED row is shaped by the route
            r.route, lookup_failed = keys.lookup(self.routes, p.canonical_host), False
        except Exception as exc:               # noqa: BLE001: denied below, once the audit value is spent
            log.error("srt id %s: the route lookup raised %s", r.srt_id, type(exc).__name__)
            r.route, lookup_failed = None, True
        try:
            self._register(r)
        except _Duplicate:
            self._owe_deny(r, 403, "decider: a second request with one audit value")
            return
        except _AuditCap:
            self._owe_deny(r, 503, "wall: audit_cap")
            return
        except _Closing:
            self._fire(r, Event.CLOSE)
            return
        if r.lc.state not in PENDING:
            return
        if lookup_failed:
            return self._deny(r, "internal", 500, "decider: internal error")
        r.mode = self.mode.get()
        w = parse.walls(p) or (_CONTROL if _has_control(p) else None)
        if w is not None:
            return self._deny(r, f"wall:{w}", 403, f"wall: {w}")
        try:
            r.strip = strip.strip(p)
        except strip.TooManyCredentialHeaders as exc:
            return self._deny(r, f"wall:{exc.reason}", 403, f"wall: {exc.reason}")
        try:
            answers = self.resolver.resolve(p.canonical_host, p.port)
        except ResolveError as exc:
            return self._deny(r, f"wall:{exc.reason}", 502, f"wall: {exc.reason}")
        aw = guard.address_wall(answers)
        if aw is not None:
            return self._deny(r, f"wall:{aw}", 403, f"wall: {aw}")
        with self._obs_lock:
            self._drain_locked()
            spool_upload_pack = advert.needs_body(p, self.adverts)
        if spool_upload_pack:
            if not self._spool(r):
                return
            r.body_cut = bool(r.body_cut) or len(r.body or b"") > advert.UPLOAD_PACK_BODY_CAP
        if not self._acted(r, self._classify(r)):
            self._card(r)

    def _classify(self, r: _Req, form: tuple[parse.QueryParam, ...] | None = None) -> classify.Verdict:
        with self._obs_lock:
            self._drain_locked()
            v = classify.decide(r.parsed, r.route, r.mode, form_params=form,
                                read_shaped=partial(advert.read_shaped, adverts=self.adverts, body_cut=r.body_cut))
        r.verdict = v
        return v

    def _acted(self, r: _Req, v: classify.Verdict) -> bool:
        """silent -> allowed, wall -> denied, card -> False (the caller files it); any other kind -> denied."""
        if v.kind == "card":
            return False
        if v.kind == "silent":
            r.step_name = "silent"
            self._allow(r)
        elif v.kind == "wall":
            self._deny(r, f"wall:{v.why}", 403, f"wall: {v.why}")
        else:
            self._deny(r, "wall:verdict_kind", 403, "wall: verdict_kind")
        return True

    # --- the body -------------------------------------------------------------------------------------------
    def _spool(self, r: _Req) -> bool:
        """One need_body; True with `r.body` set, False when the request was ended or denied here."""
        if r.body is not None:
            return True
        if not self._readers.acquire(blocking=False):
            self._deny(r, "wall:body_readers", 503, "wall: body_readers")
            return False
        q: queue_mod.Queue = queue_mod.Queue(1)
        self._bodies[r.srt_id] = q
        try:
            if self._fire(r, Event.NEED_BODY) is None or r.lc.state is not State.AWAITING_BODY:
                return False
            self.link.send(wire.need_body_frame(r.srt_id, self.limits.card_body_cap))
            deadline = time.monotonic() + self.limits.body_timeout_s
            item = None
            while item is None:
                left = deadline - time.monotonic()
                if left <= 0 or r.wake.is_set() or r.lc.state is not State.AWAITING_BODY:
                    break
                try:
                    item = q.get(timeout=min(0.2, left))
                except queue_mod.Empty:
                    continue
            if item is None:
                # still awaiting the body at the deadline: denied here. Every wake fires its event first, so a woken
                # spool is in a terminal state (which would absorb this DENY in any case)
                if r.lc.state is State.AWAITING_BODY:
                    self._deny(r, "wall:body_timeout", 408, "wall: body_timeout")
                return False
            data, cut = item
            if self._fire(r, Event.BODY) is None or r.lc.state is not State.DECIDING:
                return False
            if cut:
                self._deny(r, "wall:body_cap", 503, "wall: body_cap")
                return False
            r.body, r.body_cut, r.body_sha = data, False, hashlib.sha256(data).hexdigest()
            return True
        finally:
            self._bodies.pop(r.srt_id, None)
            self._readers.release()

    # --- verdicts ---------------------------------------------------------------------------------------------
    def _deny(self, r: _Req, step_name: str, status: int, reason: str, *, withdraw: bool = True) -> None:
        """DENY, then the request's filed card withdrawn (`withdraw=False`: the deny IS the queue's answer)."""
        r.step_name, r.deny = step_name, (status, reason)
        self._fire(r, Event.DENY)
        if withdraw:
            with r.card_lock:
                if r.card_id is None or r.card_ended:
                    return
                r.card_ended = True
                try:
                    self.cards.end(r.card_id)
                except Exception as exc:       # noqa: BLE001: the deny stands; the queue's own hold bounds the card
                    log.error("request %s: its card could not be withdrawn (%s)", r.rid, type(exc).__name__)

    def _allow(self, r: _Req) -> None:
        """R-6, then the allow frame built (every deny check is behind us), then ALLOW."""
        p = r.parsed
        r.key = keys.decide_key(r.route, p, liveness=self.liveness, credentials=self.credentials)
        remove = list(r.strip.remove_headers) + [n for n in p.routing_headers if n not in r.strip.remove_headers]
        try:
            frame = wire.allow_frame(r.srt_id, remove, cred=r.key.cred, credential=r.key.credential)
            r.allow = wire.encode_frame(frame)
        except wire.WireError:
            return self._deny(r, "wall:verdict", 403, "wall: the allow could not be built")
        step = self._fire(r, Event.ALLOW)
        if step is not None and step.stray:
            self.counters["stray_allow"] += 1
            log.error("a stray ALLOW for request %s in %s: denied", r.rid, r.lc.state)
            r.allow = None
            self._deny(r, "stray-allow", 403, "decider: an allow arrived where none may")

    def _card(self, r: _Req) -> None:
        if not self._card_slots.acquire(blocking=False):
            return self._deny(r, "wall:card_cap", 503, "wall: card_cap")
        try:
            v = r.verdict
            if "body" in v.why_all:
                if not self._spool(r):
                    return
                cts = r.parsed.header("content-type")
                if len(cts) == 1 and advert.media_type(cts[0]) == _FORM:   # one form type, or the wire method
                    try:
                        form = parse.parse_query(r.body.decode("utf-8"))
                    except (UnicodeDecodeError, parse.ParseError):
                        form = None
                    if form is not None:
                        v = self._classify(r, form)
                        if self._acted(r, v):
                            return
            card = cards.card_input(r.parsed, v, r.strip.stripped, body_len=len(r.body or b""),
                                    body_sha256=r.body_sha, launch_id=self.launch_id)
            if self._fire(r, Event.CARD) is None or r.lc.state is not State.CARD_HELD:
                return
            with r.card_lock:
                if r.card_ended:
                    return
                r.card_id = self.cards.file(card)
            got = self._await_card(r)
            if got is None:
                return
            answer, from_queue = got
            # the row's reason; the card itself shows v.why. Only the body clause of classify's R-9 card ("body" is the
            # first why of a pack POST that is neither marked nor overridden) is the cap card when the body was cut;
            # any other reason (a credential mark, `_method`, an override) keeps its own.
            why = UPLOAD_PACK_OVER_CAP if r.body_cut and v.why == "body" else v.why
            if answer == "allowed":
                r.step_name = f"card:{why}:allowed"
                return self._allow(r)
            return self._deny(r, f"card:{why}:{answer}", 403, f"card: {answer}", withdraw=not from_queue)
        finally:
            self._card_slots.release()

    def _await_card(self, r: _Req) -> tuple[str, bool] | None:
        """(the answer, whether the queue gave it), or None when the request ended or closed meanwhile."""
        deadline = time.monotonic() + self.limits.card_hold_s
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return "expired", False
            if r.wake.wait(min(self.limits.card_poll_s, left)):
                return None if r.lc.state is not State.CARD_HELD else ("expired", False)
            state = self.cards.state(r.card_id)
            if state == "allowed":
                return "allowed", True
            if state != "pending":
                return str(state or "unknown"), True

    # --- lifecycle --------------------------------------------------------------------------------------------
    def _attempt(self, r: _Req) -> rec.Attempt:
        p = r.parsed
        k = r.key
        attach = rec.Attach(k.attach) if k is not None else (
            rec.Attach.NOT_EVALUATED if r.rid is None else rec.Attach.NONE)
        marked = tuple(q for q in p.params if r.verdict is not None and q.folded in r.verdict.query_names)
        names = list(r.strip.stripped if r.strip is not None else ()) + list(shape.query_name_shapes(marked))
        if r.verdict is not None and r.verdict.exemption in ("signed_url", "azure_sas"):
            # an exemption lifted the mark, so no name was marked: keep the lifted NAMES (never a value) so K1 can count
            # them. One rule for the signed-URL set, Azure SAS's `sig`, and both together (classify labels the combined read
            # `azure_sas`); any other label lifted no name this code knows.
            azure = r.verdict.exemption == "azure_sas"
            names += [q.folded for q in p.params
                      if q.folded in classify._SIGNED_URL_READ_EXEMPT or (azure and q.folded == "sig")]
        return rec.Attempt(
            host=p.canonical_host or wire.escape_text(p.host, wire.HOST_LIMIT),
            method=r.verdict.method if r.verdict is not None else p.method,
            path=shape.request_path_shape(p, r.route), names=tuple(names),
            setup_record_id=k.cred if attach is rec.Attach.ATTACHED else None, attach=attach,
            deciding_step=r.step_name, open_network=r.mode == classify.OPEN_NETWORK)

    def _perform(self, r: _Req, effect: Effect, step: Step) -> None:
        if effect is Effect.CARD_ENDED:
            with r.card_lock:
                r.card_ended = True
                if r.card_id is not None:
                    self.cards.end(r.card_id)
        elif effect is Effect.WRITE_ATTEMPT:
            if not self.records.write_attempt(self.launch_id, r.rid, step.verdict, self._attempt(r)):
                raise RuntimeError("the attempt row was refused (one is stored for this request already)")
        elif effect is Effect.SEND_ALLOW:
            if r.allow is None:
                raise RuntimeError("an ALLOW with no allow frame built")
            self.link.send_bytes(r.allow)
        elif effect is Effect.SEND_DENY:
            self.link.send(wire.deny_frame(r.srt_id, *r.deny))
        elif effect is Effect.WRITE_OUTCOME:
            self.records.write_outcome(self.launch_id, r.obs, r.ct_class)
        elif effect is Effect.WRITE_ENDED:
            self.records.write_ended(self.launch_id, r.rid)
        elif effect is Effect.RELEASE:
            self.link.send(wire.deny_frame(r.srt_id, 403, "decider: the client left before a verdict"))

    def _fire(self, r: _Req, event: Event) -> Step | None:
        """Fire one event with this composer's effects. None when an effect raised (the request is FAILED and its
        owed answer, if any, has been dealt with)."""
        try:
            step = r.lc.fire(event, lambda e, s: self._perform(r, e, s))
        except BaseException as exc:          # noqa: BLE001: FAILED; handled below
            self._failed(r, exc)
            step = None
        if r.lc.state not in PENDING and r.lc.state is not State.ALLOWED and r.rid is not None:
            with self._live_lock:
                if self._live.get(r.rid) is r:
                    del self._live[r.rid]
        return step

    def _failed(self, r: _Req, exc: BaseException) -> None:
        f = r.lc.failure
        log.error("request %s FAILED at %s: %s", r.rid, f.effect if f else "?", type(exc).__name__)
        if f is None:
            return
        if r.rid is not None:
            try:
                self.records.record_failure(self.launch_id, r.rid, f)
            except Exception as e2:            # noqa: BLE001: the rows that landed read as they are
                log.error("request %s: the FAILED mark could not be written (%s)", r.rid, type(e2).__name__)
        answered = any(e in f.done for e in _ANSWERS)
        if f.effect in _ANSWERS and isinstance(exc, SendFailed):
            log.error("request %s: %s raised after %d of %d frame bytes left this process (measured)",
                      r.rid, f.effect, exc.sent, exc.total)
            if exc.sent == exc.total:
                answered = True
            elif exc.sent > 0:
                self.link.kill("a verdict frame was cut mid-send")
                return
        if not answered:
            self._owe_deny(r, 503, "decider: this request failed")

    def _owe_deny(self, r: _Req, status: int, reason: str) -> None:
        """A deny srt is owed outside the machine (no transition sends it). Any send that did not deliver the whole
        frame, none of it included, cuts the link (srt then refuses every request: fail closed)."""
        try:
            self.link.send(wire.deny_frame(r.srt_id, status, reason))
        except SendFailed as exc:
            if exc.sent < exc.total:
                self.link.kill("an owed deny was not sent whole")
        except Exception:                      # noqa: BLE001
            self.link.kill("an owed deny could not be sent")


#: R15-12-B8's red names a NUL in the target, the query and the header names: a control character in any of the three
#: walls the request (parse accepts one in a query key; the wall is here, beside the parse's walls).
_CONTROL = "control_character"


def _has_control(p: parse.ParsedRequest) -> bool:
    return any(ord(c) < 0x20 or ord(c) == 0x7F for s in (p.path, p.query, *(h.name for h in p.headers)) for c in s)


class _Done(Exception):
    """The decision already answered srt; nothing more to do."""


class _Duplicate(Exception):
    pass


class _AuditCap(Exception):
    """`max_audit_ids` audit values are spent for this launch."""


class _Closing(Exception):
    """close() began before this request registered."""


def service_threads() -> list[threading.Thread]:
    """Every live thread a decider service started (for the close census)."""
    return [t for t in threading.enumerate()
            if t.name in (THREAD_DECIDE, THREAD_LINK, THREAD_OBS, "levain-egress-setting")]
