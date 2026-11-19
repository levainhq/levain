"""The derive loop: re-derive a panel when its inputs move, publish the result to the snapshot writer.

One loop runs every provider warm and publishes each result through a snapshot writer (see snapshots.py for the
writer API). It decides when a panel is due from declared inputs (change detection, DESIGN D5), a clock bound
(valid_until and legacy timers, D6) and dirty marks from admitted writes (read-your-writes, D7). The dueness rules
are pure functions over plain state (`due_panels`) so a test drives them with a fake clock; the threads only
carry out what those functions decide.

Stdlib only. This module does not import the engine: the writer is passed in, and the canonical body encoding is
snapshots.encode_body (the one encoder), so equal values give equal bytes and an equal body sha.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
from collections import namedtuple
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from levain.cockpit.snapshots import encode_body

# Concurrent derives (one panel is never derived twice at once, whatever this is).
DERIVE_THREADS = 3
# A Dir input with more entries than this is not fingerprinted (answers None, the stream goes degraded).
DIR_ENTRIES_MAX = 5000
# The refs walk of a GitRefs input is bounded the same way.
GIT_REFS_MAX = 5000
# The fingerprint answer for a repo whose index.lock exists and is younger than LOCK_HOLD_MAX_S.
LOCKED = "locked"
ABSENT = "absent"
# Prefix of the fingerprint of a repo whose index.lock is older than LOCK_HOLD_MAX_S (the lock is ignored, named).
STALE_LOCK = "stale-lock"
# A live index.lock holds a GitRefs panel at most this long (seconds); past it the panel derives anyway.
LOCK_HOLD_MAX_S = 180.0
# A derive running longer than this publishes status "timeout" (PanelPlan.timeout_s overrides, seconds).
DERIVE_TIMEOUT_S = 120.0

_UNSET = object()


# --------------------------------------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class File:
    path: str


@dataclass(frozen=True)
class Dir:
    path: str


@dataclass(frozen=True)
class Sqlite:
    path: str


@dataclass(frozen=True)
class GitRefs:
    repo: str


@dataclass(frozen=True)
class Poll:
    every_s: float


def _stat_sig(path: str) -> str:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return ABSENT
    return f"{st.st_mtime_ns}:{st.st_size}:{st.st_ino}"


def _gitdir(repo: str) -> tuple[str, str] | None:
    """(gitdir, commondir) for a repo or a linked worktree, None when `repo` is not a git checkout."""
    dotgit = os.path.join(repo, ".git")
    if os.path.isdir(dotgit):
        gitdir = dotgit
    elif os.path.isfile(dotgit):
        with open(dotgit, "r", encoding="utf-8") as fh:
            first = fh.readline().strip()
        if not first.startswith("gitdir:"):
            return None
        gitdir = first[len("gitdir:"):].strip()
        if not os.path.isabs(gitdir):
            gitdir = os.path.normpath(os.path.join(repo, gitdir))
    else:
        return None
    common = gitdir
    try:
        with open(os.path.join(gitdir, "commondir"), "r", encoding="utf-8") as fh:
            rel = fh.read().strip()
        common = rel if os.path.isabs(rel) else os.path.normpath(os.path.join(gitdir, rel))
    except FileNotFoundError:
        pass
    return gitdir, common


def _refs_walk(refs_dir: str) -> list[str] | None:
    out: list[str] = []
    stack = [refs_dir]
    while stack:
        cur = stack.pop()
        try:
            it = os.scandir(cur)
        except FileNotFoundError:
            continue
        with it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                out.append(f"{os.path.relpath(entry.path, refs_dir)}:{st.st_mtime_ns}:{st.st_size}")
                if len(out) > GIT_REFS_MAX:
                    return None
    out.sort()
    return out


class _Fingerprinter:
    """Fingerprints inputs; holds one read-only sqlite connection per Sqlite path (closed by `close`)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conns: dict[str, tuple[sqlite3.Connection, tuple[int, int]]] = {}
        self.why: dict[Any, str] = {}  # the last failure type of an input that answered None

    def close(self) -> None:
        with self._lock:
            conns, self._conns = [c for c, _ in self._conns.values()], {}
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def fingerprint(self, inp: Any) -> str | None:
        self.why.pop(inp, None)
        try:
            if isinstance(inp, File):
                return _stat_sig(inp.path)
            if isinstance(inp, Dir):
                return self._dir(inp.path)
            if isinstance(inp, Sqlite):
                return self._sqlite(inp.path)
            if isinstance(inp, GitRefs):
                return self._git(inp.repo)
        except Exception as exc:
            self.why[inp] = type(exc).__name__
            return None
        return None  # Poll, or a kind this module does not know: cannot tell.

    @staticmethod
    def _dir(path: str) -> str | None:
        names: list[str] = []
        try:
            it = os.scandir(path)
        except FileNotFoundError:
            return ABSENT
        with it:
            for entry in it:
                try:
                    est = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                names.append(f"{entry.name}:{est.st_mtime_ns}:{est.st_size}")
                if len(names) > DIR_ENTRIES_MAX:
                    return None
        names.sort()
        return hashlib.sha256("\n".join(names).encode("utf-8", "surrogateescape")).hexdigest()

    def _sqlite(self, path: str) -> str | None:
        db = _stat_sig(path)
        if db == ABSENT:
            with self._lock:
                held = self._conns.pop(path, None)
            if held is not None:
                held[0].close()
            return ABSENT
        st = os.stat(path)
        ident = (st.st_dev, st.st_ino)
        with self._lock:
            held = self._conns.get(path)
            if held is not None and held[1] != ident:
                held[0].close()  # the file was replaced: the held connection reads the old inode
                held = None
            if held is None:
                conn = sqlite3.connect(
                    Path(path).resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False, timeout=1.0
                )
                held = self._conns[path] = (conn, ident)
            version = held[0].execute("PRAGMA data_version").fetchone()[0]
        return f"{db}|{_stat_sig(path + '-wal')}|dv={version}"

    @staticmethod
    def _git(repo: str) -> str | None:
        dirs = _gitdir(repo)
        if dirs is None:
            return ABSENT
        gitdir, common = dirs
        lock = os.path.join(gitdir, "index.lock")
        stale = ""
        try:
            lock_mtime = os.stat(lock).st_mtime
        except FileNotFoundError:
            pass
        else:
            if time.time() - lock_mtime < LOCK_HOLD_MAX_S:
                return LOCKED
            stale = f"{STALE_LOCK}:{lock}|"
        refs = _refs_walk(os.path.join(common, "refs"))
        if refs is None:
            return None
        parts = [
            _stat_sig(os.path.join(gitdir, "HEAD")),
            _stat_sig(os.path.join(common, "packed-refs")),
            _stat_sig(os.path.join(gitdir, "index")),
            *refs,
        ]
        return stale + hashlib.sha256("\n".join(parts).encode("utf-8", "surrogateescape")).hexdigest()


_DEFAULT_FINGERPRINTER = _Fingerprinter()


def fingerprint(inp: Any) -> str | None:
    """The fingerprint of one input, None when it cannot be told (treated as "changed" by the loop)."""
    return _DEFAULT_FINGERPRINTER.fingerprint(inp)


# --------------------------------------------------------------------------------------------------- plans


@dataclass(frozen=True)
class PanelPlan:
    id: str
    inputs: tuple = ()
    every_s: float | None = None
    on_token: bool = False
    heavy: bool = False
    backstop_s: float | None = None  # this panel's backstop; None means the loop's default
    timeout_s: float | None = None  # this panel's derive deadline; None means DERIVE_TIMEOUT_S


@dataclass(frozen=True)
class DeriveResult:
    status: str
    body: Any
    valid_until: float | None = None
    error: str | None = None


# What the poll learned about one panel's declared inputs: the joined fingerprint (sha), whether any non-Poll
# input answered None, whether a git input is locked, the names of the unknown inputs, and the stale lock paths.
InputsView = namedtuple("InputsView", "fp unknown locked unknown_names stale_locks", defaults=((),))
NO_INPUTS = InputsView("", False, False, ())


@dataclass
class PanelState:
    """What the loop remembers about one panel. Mutated under the loop's lock only."""

    derived_at: float | None = None  # when the last derive finished
    started_at: float | None = None  # when the last derive started
    valid_until: float | None = None
    inputs_fp: str | None = None  # the joined fingerprint the last derive started from
    token: Any = _UNSET
    dirty_gen: int | None = None  # highest write_gen dirtied and not yet taken by a derive
    busy: bool = False  # queued or running
    last_body: bytes | None = None  # the last good body of this process, republished under an error status
    running_since: float | None = None  # when the derive now running started; None when none is running
    run_fp: str = ""  # the inputs fingerprint the running derive started from
    timed_out: bool = False  # the running derive passed its deadline and a timeout was published


def due_reason(
    plan: PanelPlan, st: PanelState, view: InputsView, token: Any, now: float, backstop_s: float
) -> str | None:
    """Why a panel is due, or None. Pure: all inputs are arguments.

    An input that cannot be fingerprinted adds nothing here: such a panel derives by the timers and the backstop only.
    """
    if st.busy or view.locked:
        return None
    if st.derived_at is None:
        return "first"
    if st.dirty_gen is not None:
        return "dirty"
    if view.fp != "" and view.fp != st.inputs_fp:
        return "input"
    started = st.started_at if st.started_at is not None else st.derived_at
    periods = [inp.every_s for inp in plan.inputs if isinstance(inp, Poll)]
    if plan.every_s is not None:
        periods.append(plan.every_s)
    if any(now - started >= p for p in periods):
        return "every_s"
    if st.valid_until is not None and now >= st.valid_until:
        return "valid_until"
    if plan.on_token and token is not _UNSET and (st.token is _UNSET or token != st.token):
        return "token"
    if now - st.derived_at >= (plan.backstop_s if plan.backstop_s is not None else backstop_s):
        return "backstop"
    return None


def due_panels(
    plans: Mapping[str, PanelPlan],
    states: Mapping[str, PanelState],
    views: Mapping[str, InputsView],
    token: Any,
    now: float,
    backstop_s: float,
) -> list[tuple[str, str]]:
    """[(panel_id, reason)] of the due panels: dirty first, then the oldest derived_at (never derived first)."""
    due = []
    for pid, plan in plans.items():
        st = states[pid]
        reason = due_reason(plan, st, views.get(pid, NO_INPUTS), token, now, backstop_s)
        if reason is not None:
            due.append((pid, reason))

    def key(item: tuple[str, str]) -> tuple[int, float, str]:
        st = states[item[0]]
        return (0 if st.dirty_gen is not None else 1, st.derived_at if st.derived_at is not None else -1.0, item[0])

    due.sort(key=key)
    return due


@dataclass
class _Job:
    pid: str
    reason: str
    fp: str
    token: Any


# --------------------------------------------------------------------------------------------------- the loop


class DeriveLoop:
    """The poll thread plus a small pool of derive threads, publishing through `writer`.

    Locks: `_lock` (states, queue, stop flag) and `_write_lock` (every writer call). Code holding `_lock` never takes
    `_write_lock`; code holding `_write_lock` may take `_lock` for a short state update.
    """

    def __init__(
        self,
        writer: Any,
        plans: Mapping[str, PanelPlan],
        derive_fn: Callable[[str], DeriveResult],
        *,
        threads: int = DERIVE_THREADS,
        poll_s: float = 1.0,
        backstop_s: float = 30.0,
        token_fn: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._writer = writer
        self._plans = dict(plans)
        self._derive_fn = derive_fn
        self._threads_n = max(1, int(threads))
        self._poll_s = poll_s
        self._backstop_s = backstop_s
        self._token_fn = token_fn
        self._clock = clock
        self._fingerprinter = _Fingerprinter()
        self._states = {pid: PanelState() for pid in self._plans}
        self._lock = threading.Lock()  # states, queue, stop flag
        self._cv = threading.Condition(self._lock)  # workers wait here for queued jobs
        self._write_lock = threading.Lock()  # serialises every writer call
        self._wake = threading.Event()
        self._queue: dict[str, _Job] = {}
        self._stopping = False
        self._start_gen = 0  # bumped by start(): threads of an earlier start exit
        self._threads: list[threading.Thread] = []
        self._last_event_at: float | None = None
        self._last_views: dict[Any, str | None] | None = None
        self.last_publish_error: BaseException | None = None

    # ---- control

    def dirty(self, panel_ids, write_gen: int) -> None:
        """Mark panels dirty: they derive first, stamped with the highest write_gen dirtied before they start."""
        with self._lock:
            for pid in panel_ids:
                st = self._states.get(pid)
                if st is None:
                    continue
                st.dirty_gen = write_gen if st.dirty_gen is None else max(st.dirty_gen, write_gen)
        self._wake.set()

    def start(self) -> None:
        with self._lock:
            if self._threads:
                return
            self._stopping = False
            self._start_gen += 1
            gen = self._start_gen
            self._wake.clear()
            self._threads = [threading.Thread(target=self._poll_loop, args=(gen,), name="derive-poll", daemon=True)]
            for i in range(self._threads_n):
                self._threads.append(threading.Thread(target=self._worker, args=(gen,), name=f"derive-{i}", daemon=True))
            threads = list(self._threads)
        for t in threads:
            t.start()

    def stop(self, timeout: float | None = None) -> None:
        with self._cv:
            self._stopping = True
            threads, self._threads = self._threads, []
            self._cv.notify_all()
        self._wake.set()
        for t in threads:
            t.join(timeout)
        self._fingerprinter.close()

    def run_once(self, now: float) -> list[str]:
        """Evaluate dueness once and run the due derives to completion on the calling thread.

        `now` is the derive start time and the loop's clock supplies the finish time, so a test hands one fake clock
        to both.
        """
        jobs = self._evaluate(now)
        ran = []
        for job in jobs:
            with self._lock:
                self._states[job.pid].busy = True
            fatal = self._run_job(job, now)
            ran.append(job.pid)
            if fatal is not None:
                raise fatal
        return ran

    # ---- the poll

    def _views(self) -> tuple[dict[str, InputsView], dict[Any, str | None]]:
        seen: dict[Any, str | None] = {}
        views: dict[str, InputsView] = {}
        for pid, plan in self._plans.items():
            parts, unknown, locked, stale = [], [], False, []
            for inp in plan.inputs:
                if isinstance(inp, Poll):
                    continue
                if inp not in seen:
                    seen[inp] = self._fingerprinter.fingerprint(inp)
                fp = seen[inp]
                if fp is None:
                    why = self._fingerprinter.why.get(inp)
                    where = getattr(inp, "path", None) or getattr(inp, "repo", "")
                    unknown.append(f"{pid}: {type(inp).__name__}({where})" + (f" [{why}]" if why else ""))
                    parts.append("?")
                    continue
                if fp == LOCKED:
                    locked = True
                elif fp.startswith(STALE_LOCK + ":"):
                    stale.append(f"{pid}: stale index.lock {fp[len(STALE_LOCK) + 1:].split('|', 1)[0]}")
                parts.append(fp)
            joined = hashlib.sha256("\x1f".join(parts).encode("utf-8", "surrogateescape")).hexdigest() if parts else ""
            views[pid] = InputsView(joined, bool(unknown), locked, tuple(unknown), tuple(stale))
        return views, seen

    def _publish_timeouts(self, now: float) -> None:
        """Publish status "timeout" (stored body kept) for each running derive past its deadline, once."""
        with self._lock:
            late = [
                (pid, st.running_since)
                for pid, st in self._states.items()
                if st.running_since is not None and not st.timed_out
                and now - st.running_since >= (self._plans[pid].timeout_s or DERIVE_TIMEOUT_S)
            ]
        for pid, since in late:
            with self._write_lock:
                with self._lock:
                    st = self._states[pid]
                    if st.running_since != since or st.timed_out:
                        continue  # it finished (and published) meanwhile
                    st.timed_out = True
                    run_fp = st.run_fp
                try:
                    self._writer.publish(
                        pid, status="timeout", body=None, started_at=since, derived_at=now,
                        duration_ms=max(0, int(round((now - since) * 1000))), inputs_fp=run_fp,
                        error=f"derive still running after {now - since:.0f} s",
                    )
                except Exception as exc:
                    self.last_publish_error = exc

    def _evaluate(self, now: float) -> list[_Job]:
        """One poll: heartbeat, the inputs stream, then the due panels not already busy, as jobs in order."""
        views, seen = self._views()
        token: Any = _UNSET
        if self._token_fn is not None:
            try:
                token = self._token_fn()
            except Exception:
                token = _UNSET
        if self._last_views is not None and self._last_views != seen:
            self._last_event_at = now  # the last change seen; the first poll's fingerprints are not a change
        self._last_views = seen
        self._publish_timeouts(now)
        degraded = [name for v in views.values() for name in (*v.unknown_names, *v.stale_locks)]
        with self._lock:
            degraded += [f"{pid}: derive timeout" for pid, st in self._states.items() if st.timed_out]
        err = self.last_publish_error
        if err is not None:
            degraded.append(f"write error: {type(err).__name__}: {err}")
        with self._write_lock:
            try:
                self._writer.heartbeat(now)
                self._writer.set_stream(
                    "inputs",
                    state="degraded" if degraded else "ok",
                    last_event_at=self._last_event_at,
                    detail="; ".join(degraded),
                )
            except Exception as exc:  # a failing writer must not kill the loop
                self.last_publish_error = exc
            else:
                if err is not None and self.last_publish_error is err:
                    self.last_publish_error = None  # the writer answers again
        with self._lock:
            due = due_panels(self._plans, self._states, views, token, now, self._backstop_s)
            return [_Job(pid, reason, views[pid].fp, token) for pid, reason in due]

    def _poll_loop(self, gen: int) -> None:
        while True:
            with self._cv:
                if self._stopping or self._start_gen != gen:
                    return
            self._wake.clear()
            try:
                jobs = self._evaluate(self._clock())
            except Exception as exc:  # one bad poll must not end the loop
                self.last_publish_error = exc
                jobs = []
            with self._cv:
                if self._stopping or self._start_gen != gen:
                    return
                for job in jobs:
                    st = self._states[job.pid]
                    if not st.busy:
                        st.busy = True
                        self._queue[job.pid] = job
                if jobs:
                    self._cv.notify_all()
            self._wake.wait(self._poll_s)

    def _worker(self, gen: int) -> None:
        while True:
            with self._cv:
                while not self._queue and not self._stopping and self._start_gen == gen:
                    self._cv.wait()
                if self._stopping or self._start_gen != gen:
                    return
                pid = min(self._queue, key=self._queue_key)
                job = self._queue.pop(pid)
            self._run_job(job, self._clock())

    def _queue_key(self, pid: str) -> tuple[int, float, str]:
        st = self._states[pid]
        return (0 if st.dirty_gen is not None else 1, st.derived_at if st.derived_at is not None else -1.0, pid)

    # ---- one derive

    def _run_job(self, job: _Job, started: float) -> BaseException | None:
        """Run one derive and publish it; the panel is not busy afterwards whatever happened.

        Returns a non-Exception BaseException the derive raised (the caller decides whether to re-raise), else None.
        """
        pid = job.pid
        with self._lock:
            st = self._states[pid]
            write_gen = st.dirty_gen if st.dirty_gen is not None else 0
            st.dirty_gen = None  # a dirty arriving from here on is for the next derive
            last_body = st.last_body
            st.running_since, st.run_fp, st.timed_out = started, job.fp, False
        body: bytes | None = None
        status, error, valid_until = "error", None, None
        finished: float | None = None
        published = False
        fatal: BaseException | None = None
        try:
            try:
                result = self._derive_fn(pid)
                body = encode_body(result.body)
                status, error, valid_until = result.status, result.error, result.valid_until
            except BaseException as exc:
                error = f"{type(exc).__name__}: {exc}"
                if not isinstance(exc, Exception):
                    fatal = exc
            finished = self._clock()
            duration_ms = max(0, int(round((finished - started) * 1000)))
            # A valid_until at or before the start would re-derive on every poll: treat it as no expiry, also in the store.
            if valid_until is not None and valid_until <= started:
                valid_until = None
            try:
                with self._write_lock:
                    with self._lock:
                        st.running_since = None  # the timeout publisher checks this under the write lock
                    self._writer.publish(
                        pid,
                        status=status,
                        body=body if body is not None else last_body,  # None keeps the stored body
                        started_at=started,
                        derived_at=finished,
                        duration_ms=duration_ms,
                        valid_until=valid_until,
                        inputs_fp=job.fp,
                        write_gen=write_gen,
                        error=error,
                    )
                published = True
            except Exception as exc:
                self.last_publish_error = exc
            else:
                self.last_publish_error = None
        finally:
            with self._cv:
                st.running_since, st.timed_out = None, False
                late_dirty = st.dirty_gen is not None  # a dirty that arrived during this derive
                if not published and write_gen and (st.dirty_gen is None or st.dirty_gen < write_gen):
                    st.dirty_gen = write_gen  # the write was not published: the next derive still owes it
                if finished is not None:
                    st.started_at = started
                    st.derived_at = finished
                    st.inputs_fp = job.fp
                    st.token = job.token
                    st.valid_until = valid_until
                    if body is not None:
                        st.last_body = body
                st.busy = False
            if late_dirty:
                self._wake.set()
        return fatal
