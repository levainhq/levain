"""The derive loop: re-derive a panel when its inputs move, publish the result to the snapshot writer.

One loop runs every provider warm and publishes each result through a snapshot writer (see snapshots.py for the
writer API). It decides when a panel is due from declared inputs (change detection, DESIGN D5), a clock bound
(valid_until and legacy timers, D6) and dirty marks from admitted writes (read-your-writes, D7). The dueness rules
are pure functions over plain state (`due_panels`) so a test drives them with a fake clock; the threads only
carry out what those functions decide.

Stdlib only. This module does not import the engine or snapshots.py: the writer is passed in, and the canonical
body encoding is a local function with the same semantics as the writer module's `encode_body` (sorted keys,
compact separators, ensure_ascii off, UTF-8), so equal values give equal bytes and an equal body sha.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from collections import namedtuple
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

# Concurrent derives (one panel is never derived twice at once, whatever this is).
DERIVE_THREADS = 3
# A Dir input with more entries than this is not fingerprinted (answers None, the stream goes degraded).
DIR_ENTRIES_MAX = 5000
# The refs walk of a GitRefs input is bounded the same way.
GIT_REFS_MAX = 5000
# The fingerprint answer for a repo whose index.lock exists.
LOCKED = "locked"
ABSENT = "absent"

_UNSET = object()


def _encode_body(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


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
        self._conns: dict[str, sqlite3.Connection] = {}

    def close(self) -> None:
        with self._lock:
            conns, self._conns = list(self._conns.values()), {}
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def fingerprint(self, inp: Any) -> str | None:
        try:
            if isinstance(inp, File):
                return _stat_sig(inp.path)
            if isinstance(inp, Dir):
                return self._dir(inp.path)
            if isinstance(inp, Sqlite):
                return self._sqlite(inp.path)
            if isinstance(inp, GitRefs):
                return self._git(inp.repo)
        except Exception:
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
                    mt = entry.stat(follow_symlinks=False).st_mtime_ns
                except FileNotFoundError:
                    continue
                names.append(f"{entry.name}:{mt}")
                if len(names) > DIR_ENTRIES_MAX:
                    return None
        names.sort()
        return hashlib.sha256("\n".join(names).encode("utf-8", "surrogateescape")).hexdigest()

    def _sqlite(self, path: str) -> str | None:
        db = _stat_sig(path)
        if db == ABSENT:
            with self._lock:
                conn = self._conns.pop(path, None)
            if conn is not None:
                conn.close()
            return ABSENT
        with self._lock:
            conn = self._conns.get(path)
            if conn is None:
                conn = sqlite3.connect(
                    Path(path).resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False, timeout=1.0
                )
                self._conns[path] = conn
            version = conn.execute("PRAGMA data_version").fetchone()[0]
        return f"{db}|{_stat_sig(path + '-wal')}|dv={version}"

    @staticmethod
    def _git(repo: str) -> str | None:
        dirs = _gitdir(repo)
        if dirs is None:
            return ABSENT
        gitdir, common = dirs
        if os.path.exists(os.path.join(gitdir, "index.lock")):
            return LOCKED
        refs = _refs_walk(os.path.join(common, "refs"))
        if refs is None:
            return None
        parts = [
            _stat_sig(os.path.join(gitdir, "HEAD")),
            _stat_sig(os.path.join(common, "packed-refs")),
            _stat_sig(os.path.join(gitdir, "index")),
            *refs,
        ]
        return hashlib.sha256("\n".join(parts).encode("utf-8", "surrogateescape")).hexdigest()


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


@dataclass(frozen=True)
class DeriveResult:
    status: str
    body: Any
    valid_until: float | None = None
    error: str | None = None


# What the poll learned about one panel's declared inputs: the joined fingerprint (sha), whether any non-Poll
# input answered None, whether a git input is locked, and the names of the unknown inputs.
InputsView = namedtuple("InputsView", "fp unknown locked unknown_names")
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
    last_body: bytes | None = None  # the last good body, republished under an error status


def due_reason(
    plan: PanelPlan, st: PanelState, view: InputsView, token: Any, now: float, backstop_s: float
) -> str | None:
    """Why a panel is due, or None. Pure: all inputs are arguments."""
    if st.busy or view.locked:
        return None
    if st.derived_at is None:
        return "first"
    if st.dirty_gen is not None:
        return "dirty"
    if view.unknown or (view.fp != "" and view.fp != st.inputs_fp):
        return "input"
    started = st.started_at if st.started_at is not None else st.derived_at
    periods = [inp.every_s for inp in plan.inputs if isinstance(inp, Poll)]
    if plan.every_s is not None:
        periods.append(plan.every_s)
    if any(now - started >= p for p in periods):
        return "every_s"
    if st.valid_until is not None and now >= st.valid_until:
        return "valid_until"
    if plan.on_token and token is not _UNSET and st.token is not _UNSET and token != st.token:
        return "token"
    if now - st.derived_at >= backstop_s:
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
        self._threads: list[threading.Thread] = []
        self._last_event_at: float | None = None
        self._last_views: dict[Any, str | None] = {}
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
            self._wake.clear()
            self._threads = [threading.Thread(target=self._poll_loop, name="derive-poll", daemon=True)]
            for i in range(self._threads_n):
                self._threads.append(threading.Thread(target=self._worker, name=f"derive-{i}", daemon=True))
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
        """Evaluate dueness once and run the due derives to completion on the calling thread."""
        jobs = self._evaluate(now)
        ran = []
        for job in jobs:
            with self._lock:
                self._states[job.pid].busy = True
            self._run_job(job, now)
            ran.append(job.pid)
        return ran

    # ---- the poll

    def _views(self) -> tuple[dict[str, InputsView], dict[Any, str | None]]:
        seen: dict[Any, str | None] = {}
        views: dict[str, InputsView] = {}
        for pid, plan in self._plans.items():
            parts, unknown, locked = [], [], False
            for inp in plan.inputs:
                if isinstance(inp, Poll):
                    continue
                if inp not in seen:
                    seen[inp] = self._fingerprinter.fingerprint(inp)
                fp = seen[inp]
                if fp is None:
                    unknown.append(f"{pid}: {type(inp).__name__}({getattr(inp, 'path', None) or getattr(inp, 'repo', '')})")
                    parts.append("?")
                    continue
                if fp == LOCKED:
                    locked = True
                parts.append(fp)
            joined = hashlib.sha256("\x1f".join(parts).encode("utf-8", "surrogateescape")).hexdigest() if parts else ""
            views[pid] = InputsView(joined, bool(unknown), locked, tuple(unknown))
        return views, seen

    def _evaluate(self, now: float) -> list[_Job]:
        """One poll: heartbeat, the inputs stream, then the due panels not already busy, as jobs in order."""
        views, seen = self._views()
        token: Any = _UNSET
        if self._token_fn is not None:
            try:
                token = self._token_fn()
            except Exception:
                token = _UNSET
        if self._last_views != seen:
            self._last_event_at = now
            self._last_views = seen
        unknown = [name for v in views.values() for name in v.unknown_names]
        with self._write_lock:
            try:
                self._writer.heartbeat(now)
                self._writer.set_stream(
                    "inputs",
                    state="degraded" if unknown else "ok",
                    last_event_at=self._last_event_at if self._last_event_at is not None else now,
                    detail="; ".join(unknown),
                )
            except Exception as exc:  # a failing writer must not kill the loop
                self.last_publish_error = exc
        with self._lock:
            due = due_panels(self._plans, self._states, views, token, now, self._backstop_s)
            return [_Job(pid, reason, views[pid].fp, token) for pid, reason in due]

    def _poll_loop(self) -> None:
        while True:
            with self._cv:
                if self._stopping:
                    return
            self._wake.clear()
            jobs = self._evaluate(self._clock())
            with self._cv:
                if self._stopping:
                    return
                for job in jobs:
                    st = self._states[job.pid]
                    if not st.busy:
                        st.busy = True
                        self._queue[job.pid] = job
                if jobs:
                    self._cv.notify_all()
            self._wake.wait(self._poll_s)

    def _worker(self) -> None:
        while True:
            with self._cv:
                while not self._queue and not self._stopping:
                    self._cv.wait()
                if self._stopping:
                    return
                pid = min(self._queue, key=self._queue_key)
                job = self._queue.pop(pid)
            self._run_job(job, self._clock())

    def _queue_key(self, pid: str) -> tuple[int, float, str]:
        st = self._states[pid]
        return (0 if st.dirty_gen is not None else 1, st.derived_at if st.derived_at is not None else -1.0, pid)

    # ---- one derive

    def _run_job(self, job: _Job, started: float) -> None:
        pid = job.pid
        with self._lock:
            st = self._states[pid]
            write_gen = st.dirty_gen if st.dirty_gen is not None else 0
            st.dirty_gen = None  # a dirty arriving from here on is for the next derive
            last_body = st.last_body
        body: bytes | None = None
        status, error, valid_until = "error", None, None
        try:
            result = self._derive_fn(pid)
            body = _encode_body(result.body)
            status, error, valid_until = result.status, result.error, result.valid_until
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finished = self._clock()
        duration_ms = max(0, int(round((finished - started) * 1000)))
        publish_body = body if body is not None else (last_body if last_body is not None else _encode_body(None))
        try:
            with self._write_lock:
                self._writer.publish(
                    pid,
                    status=status,
                    body=publish_body,
                    started_at=started,
                    derived_at=finished,
                    duration_ms=duration_ms,
                    valid_until=valid_until,
                    inputs_fp=job.fp,
                    write_gen=write_gen,
                    error=error,
                )
        except Exception as exc:
            self.last_publish_error = exc
        with self._cv:
            st.started_at = started
            st.derived_at = finished
            st.inputs_fp = job.fp
            st.token = job.token
            # A valid_until at or before the start would re-derive on every poll: treat it as spent.
            st.valid_until = valid_until if valid_until is not None and valid_until > started else None
            if body is not None:
                st.last_body = body
            st.busy = False
        self._wake.set()
