"""levain.cockpit.stream: the cockpit's change stream (``GET /cockpit/events``, server-sent events).

The stream carries no panel data. It tells a page WHICH panels moved; the page reads them through the
same panel route as any read.

Where a change comes from. An on-demand panel moves only when something reads it, so a store edit
nobody reads never shows in the engine's freshness metadata, and a read advances that metadata by
itself. Neither is a change signal. The signal is the panel head's content ``etag`` in the manifest
(``as_of`` is outside it by design): one server-side Watcher builds the manifest on a slow tick,
diffs the etags and the panel id set between its own ticks, and broadcasts. A client's GET of a panel
never reaches that diff, so reading what was announced cannot announce it again.

The Watcher lives only while a stream is open. Each start is a new epoch, because nothing observed
the interval it was off; a client that comes back from an older epoch is told to resync. Inside one
epoch a reconnecting client is caught up from a bounded ring of recent events.

The full manifest build is the expensive step, so it does not run on every look. When the server gives the
Watcher a change token (a cheap fingerprint of the stores the panels read), the Watcher looks at the token
every ``token_s`` and builds the manifest only when the token moved, or when ``backstop_s`` has passed since the
last build (what the token cannot see: the clock, an in-memory state, a store it does not name). A token of
None means "not known": the build then runs every ``watch_s``, exactly as without a token. After a build the
next token-driven build waits ``2 x`` how long that build took, so a token that never settles cannot run the
build faster than it can finish.

A server that reads the panels through a snapshot reader can give the Watcher a change probe instead (``probe=``;
the token and the manifest build are then not used). The probe has two methods: ``change_seq()``, a cheap counter
that moves only when a panel's content or the panel set changed, and ``snapshot()``, which returns
``({panel_id: etag}, set_version)``. The Watcher looks at ``change_seq()`` every ``token_s`` and takes a snapshot
only when it moved (and ``2 x`` the last snapshot's duration has passed), or when ``backstop_s`` has passed since the
last one. ``set_version`` is opaque: a change of it
with the same panel ids announces ``manifest-changed``, like a change of the id set. A counter that cannot be read
is an unknown one: the snapshot then runs every ``watch_s``."""

from __future__ import annotations

import collections
import json
import logging
import queue
import threading
import time
from typing import Any, Callable

log = logging.getLogger("levain.cockpit.stream")

EVENTS_PATH = "/cockpit/events"
CONTENT_TYPE = "text/event-stream; charset=utf-8"
DEFAULT_WATCH_S = 5.0
DEFAULT_TOKEN_S = 1.0       # how often the cheap change token is looked at
DEFAULT_BACKSTOP_S = 30.0   # the longest the manifest goes unbuilt while a token is in use
DEFAULT_HEARTBEAT_S = 15.0
DEFAULT_MAX_STREAMS = 8
DEFAULT_RING = 256
SUBSCRIBER_QUEUE = 512      # a client this far behind is dropped; it reconnects and resyncs or replays


def frame(event: str, data: Any, event_id: str | None = None) -> bytes:
    head = (f"id: {event_id}\n" if event_id else "") + f"event: {event}\n"
    return (head + "data: " + json.dumps(data, separators=(",", ":")) + "\n\n").encode("utf-8")


def parse_event_id(last_event_id: str | None) -> tuple[str, int] | None:
    """``"<epoch>:<seq>"`` -> ``(epoch, seq)``; anything else is no usable position."""
    epoch, _, seq = (last_event_id or "").rpartition(":")
    if not epoch or not seq.isdigit():
        return None
    return epoch, int(seq)


class Subscription:
    """One open stream's mailbox. ``hello`` is what the stream opens with; the queue is primed with any
    replayed events before the subscription is visible to a broadcast, so nothing slips between."""

    def __init__(self, hello: dict[str, Any], hello_id: str) -> None:
        self.hello = hello
        self.hello_id = hello_id
        self.q: "queue.Queue[tuple[str, dict[str, Any], str]]" = queue.Queue(SUBSCRIBER_QUEUE)
        self.dead = False


class Watcher:
    """Diffs the manifest's panel etags and id set on a tick and broadcasts what moved."""

    def __init__(self, read_manifest: Callable[[], dict[str, Any]], *, epoch: str,
                 watch_s: float = DEFAULT_WATCH_S, ring: int = DEFAULT_RING,
                 token: Callable[[], Any] | None = None, token_s: float = DEFAULT_TOKEN_S,
                 backstop_s: float = DEFAULT_BACKSTOP_S, probe: Any = None) -> None:
        self._read_manifest = read_manifest
        self._probe = probe
        self._seq_failed = False
        self._set_version: str | None = None
        self._token = token
        self._token_failed = False
        self.token_s = token_s
        self.backstop_s = backstop_s
        self._base_epoch = epoch
        self.watch_s = watch_s
        self._ring_max = ring
        self._lock = threading.Lock()
        self._subs: set[Subscription] = set()
        self._thread: threading.Thread | None = None
        self._run_stop: threading.Event | None = None
        self._gen = 0
        self._epoch = epoch
        self._seq = 0
        self._ring: "collections.deque[tuple[int, str, dict[str, Any]]]" = collections.deque(maxlen=ring)
        self._etags: dict[str, Any] | None = None

    def set_probe(self, probe: Any) -> None:
        """Give the Watcher a change probe (or None to drop it) after construction, while no stream is open: the
        observation a run began with is not swapped under it."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("the change probe cannot be replaced while the watcher runs")
            self._probe = probe

    # ---- observation -------------------------------------------------------------
    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def stream_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def _read(self) -> dict[str, Any]:
        heads = self._read_manifest()["panels"]
        return {pid: head.get("etag") for pid, head in heads.items()}

    def _observe(self) -> tuple[dict[str, Any], str | None]:
        """What one observation sees: the panel etags and the set version (None without a probe)."""
        if self._probe is None:
            return self._read(), None
        etags, set_version = self._probe.snapshot()
        return dict(etags), set_version

    def _seq_now(self) -> Any:
        """The probe's change counter, or None when it cannot be read (None = unknown: snapshot on the slow tick)."""
        try:
            return self._probe.change_seq()
        except Exception:   # noqa: BLE001 - an unreadable counter is an unknown one, never a missed change
            if not self._seq_failed:        # once per run, not once per look
                self._seq_failed = True
                log.exception("cockpit watcher: the change probe's counter could not be read (the slow tick snapshots instead)")
            return None

    def _token_now(self) -> Any:
        """The change token, or None when there is none or it cannot be read (None = unknown: build on the slow tick)."""
        if self._token is None:
            return None
        try:
            return self._token()
        except Exception:   # noqa: BLE001 - an unreadable token is an unknown one, never a missed change
            if not self._token_failed:      # once per run, not once per look
                self._token_failed = True
                log.exception("cockpit watcher: the change token could not be read (the slow tick builds instead)")
            return None

    # ---- subscribe / unsubscribe -------------------------------------------------
    def subscribe(self, last_event_id: str | None) -> Subscription:
        with self._lock:
            if not self._subs:
                self._start_locked()
            pos = parse_event_id(last_event_id)
            replay: list[tuple[int, str, dict[str, Any]]] = []
            resync = False
            if last_event_id and pos is None:
                resync = True       # a position that cannot be read is no position the ring can honour
            elif pos is not None:
                ep, seq = pos
                first = self._ring[0][0] if self._ring else self._seq + 1
                if ep == self._epoch and first - 1 <= seq <= self._seq:
                    replay = [e for e in self._ring if e[0] > seq]
                else:
                    resync = True
            sub = Subscription({"epoch": self._epoch, "resync": resync}, f"{self._epoch}:{self._seq - len(replay)}")
            for seq, event, data in replay:
                sub.q.put_nowait((event, data, f"{self._epoch}:{seq}"))
            self._subs.add(sub)
            return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            self._subs.discard(sub)
            sub.dead = True
            if not self._subs:
                self._stop_locked()

    def stop(self) -> None:
        """The server is closing: every stream ends and the thread stops."""
        with self._lock:
            for s in self._subs:
                s.dead = True
            self._subs.clear()
            self._stop_locked()

    # ---- the thread --------------------------------------------------------------
    def _start_locked(self) -> None:
        self._gen += 1
        self._epoch = f"{self._base_epoch}.{self._gen}"
        self._seq = 0
        self._ring.clear()
        self._token_failed = False
        self._seq_failed = False
        self._set_version = None
        # before the baseline: a change landing between them is seen as one next look
        tok = self._token_now() if self._probe is None else self._seq_now()
        try:
            self._etags, self._set_version = self._observe()
        except Exception:   # noqa: BLE001 - the first good tick adopts a baseline and asks pages to reload
            log.exception("cockpit watcher: the baseline manifest read failed")
            self._etags = None
        stop = threading.Event()
        self._run_stop = stop
        self._thread = threading.Thread(target=self._loop, args=(stop, tok), name="cockpit-watcher", daemon=True)
        self._thread.start()

    def _stop_locked(self) -> None:
        if self._run_stop is not None:
            self._run_stop.set()
        self._run_stop = None
        self._thread = None

    def _loop_probe(self, stop: threading.Event, seq: Any) -> None:
        last_snap = time.monotonic()
        not_before = 0.0
        gap = 0.0       # twice the last snapshot's duration: a counter that never settles cannot run snapshots faster than they finish
        while not stop.wait(self.token_s):
            now_seq = self._seq_now()
            t = time.monotonic()
            idle = t - last_snap
            if now_seq is None:
                due = idle >= self.watch_s
            else:
                due = (now_seq != seq and idle >= gap) or idle >= self.backstop_s
            if not due or t < not_before:
                continue
            if self.tick(stop):
                seq = now_seq
                last_snap = time.monotonic()
                gap = 2 * (last_snap - t)
            else:
                not_before = time.monotonic() + self.watch_s     # a snapshot that fails is retried on the slow tick

    def _loop(self, stop: threading.Event, tok: Any = None) -> None:
        if self._probe is not None:
            self._loop_probe(stop, tok)
            return
        if self._token is None:
            while not stop.wait(self.watch_s):
                self.tick(stop)
            return
        last_build = time.monotonic()
        gap = 0.0
        while not stop.wait(self.token_s):
            now_tok = self._token_now()
            idle = time.monotonic() - last_build
            if now_tok is None:
                due = idle >= self.watch_s
            else:
                due = (now_tok != tok and idle >= gap) or idle >= self.backstop_s
            if not due:
                continue
            tok = now_tok
            began = time.monotonic()
            self.tick(stop)
            last_build = time.monotonic()
            gap = 2 * (last_build - began)

    def tick(self, run: threading.Event | None = None) -> bool:
        """One observation. Any failure is this tick's alone: it is logged and the next tick tries again.
        False when the observation failed. True otherwise, which includes a tick from a run that has been stopped or
        replaced: that observation is read but discarded (nothing adopted, nothing announced). A True return is "the
        look did not fail", not "this tick's observation is the watcher's current one"."""
        try:
            now, version = self._observe()
        except Exception:   # noqa: BLE001 - a watcher that dies would leave every page reading "live" over stale data
            log.exception("cockpit watcher: a manifest read failed")
            return False
        with self._lock:
            if run is not None and run is not self._run_stop:
                return True     # a tick from a run that has been stopped (or replaced)
            prev, self._etags = self._etags, now
            prev_version, self._set_version = self._set_version, version
            if prev is None or set(prev) != set(now) or prev_version != version:
                self._publish_locked("manifest-changed", {})
                return True
            for pid, etag in now.items():
                if prev[pid] != etag:
                    self._publish_locked("panel-changed", {"id": pid, "etag": etag})
        return True

    def _publish_locked(self, event: str, data: dict[str, Any]) -> None:
        self._seq += 1
        self._ring.append((self._seq, event, data))
        eid = f"{self._epoch}:{self._seq}"
        for s in list(self._subs):
            try:
                s.q.put_nowait((event, data, eid))
            except queue.Full:
                s.dead = True       # too slow to follow: its stream ends and it comes back through replay or resync


def run(
    write: Callable[[bytes], None], sub: Subscription, *, stop: threading.Event,
    heartbeat_s: float = DEFAULT_HEARTBEAT_S, poll_s: float = 0.5, clock: Callable[[], float] = time.monotonic,
    alive: Callable[[], bool] | None = None, allowed: Callable[[bool], bool] | None = None,
) -> None:
    """Drive one stream until ``stop`` is set, the subscription dies, ``write`` raises (a gone client), or
    ``alive`` says the peer has hung up: a quiet stream would otherwise hold its slot, and keep the watcher
    running, until the next heartbeat write failed.

    ``allowed(recheck)`` (PAIR RU-3) is asked on EVERY loop pass, before any frame (data or heartbeat) is
    written, and the stream ends the moment it answers False: a revoked paired device's open stream writes at most
    the one frame already past its check when the revoke commits (G3: no lock is held around the write).
    ``recheck`` is True on any pass, the data path included, once ``heartbeat_s`` has passed since the last
    heartbeat re-check (G2: its own deadline, so an event flow faster than the heartbeat cannot starve the DB
    liveness re-check, which is the backstop for a revoke that moves no revision); otherwise the callback compares
    the in-process epoch and the journal's revocation revision (levain#231) and runs the device check itself only
    when either moved or the revision cannot be compared. It is asked before the
    hello frame too (F8), so "before any frame" holds for the first one."""
    if allowed is not None and not allowed(False):
        return
    write(frame("hello", sub.hello, sub.hello_id))
    last_write = last_check = clock()

    def check() -> bool:
        nonlocal last_check
        if allowed is None:
            return True
        recheck = clock() - last_check >= heartbeat_s
        if not allowed(recheck):
            return False
        if recheck:
            last_check = clock()
        return True

    while not stop.is_set() and not sub.dead:
        try:
            event, data, eid = sub.q.get(timeout=min(poll_s, heartbeat_s))
        except queue.Empty:
            if alive is not None and not alive():
                return
            if not check():
                return
            if clock() - last_write >= heartbeat_s:
                write(b": hb\n\n")
                last_write = clock()
            continue
        if not check():
            return
        write(frame(event, data, eid))
        last_write = clock()
