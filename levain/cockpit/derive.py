"""The derive loop: re-derive a panel when its inputs move, publish the result to the snapshot writer.

One loop runs every provider warm and publishes each result through a snapshot writer (see snapshots.py for the
writer API). It decides when a panel is due from declared inputs (change detection, DESIGN D5), a clock bound
(valid_until and legacy timers, D6) and dirty marks from admitted writes (read-your-writes, D7). The dueness rules
are pure functions over plain state (`due_panels`) so a test drives them with a fake clock; the threads only
carry out what those functions decide.

The set of panels is not fixed for the loop's life: a host whose panels appear and vanish (discovered ones) hands the
loop the whole current set with `set_plans`, or drops one with `retire`; a panel that left the set is never published
again, even by a derive that was already running. A host that must wake readers when a row lands passes `on_publish`.
`deriving` and `pending` tell a caller which panels are queued or running without exposing the loop's own state.

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
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from levain.cockpit.snapshots import KEEP_BODY_STATUSES, KNOWN_STATUSES, encode_body

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


def _sig(st: os.stat_result) -> str:
    return f"{st.st_mtime_ns}:{st.st_size}:{st.st_ino}"


def _stat_sig(path: str) -> str:
    try:
        return _sig(os.stat(path))
    except FileNotFoundError:
        return ABSENT


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
                out.append(f"{os.path.relpath(entry.path, refs_dir)}:{_sig(st)}")
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

    def keep_sqlite(self, paths: set[str]) -> None:
        """Close the held connections of Sqlite paths that no input uses any more."""
        with self._lock:
            gone = [path for path in self._conns if path not in paths]
            dropped = [self._conns.pop(path)[0] for path in gone]
        for conn in dropped:
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
                names.append(f"{entry.name}:{_sig(est)}")
                if len(names) > DIR_ENTRIES_MAX:
                    return None
        names.sort()
        return hashlib.sha256("\n".join(names).encode("utf-8", "surrogateescape")).hexdigest()

    def _sqlite(self, path: str) -> str | None:
        try:
            st = os.stat(path)  # the one stat: the signature and the identity come from the same look
        except FileNotFoundError:
            with self._lock:
                held = self._conns.pop(path, None)
            if held is not None:
                held[0].close()
            return ABSENT
        ident = (st.st_dev, st.st_ino)
        with self._lock:
            held = self._conns.pop(path, None)  # out of the map while it is closed or reopened: never a closed one in it
            if held is not None and held[1] != ident:
                held[0].close()  # the file was replaced: the held connection reads the old inode
                held = None
            if held is None:
                conn = sqlite3.connect(
                    Path(path).resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False, timeout=1.0
                )
                held = (conn, ident)
            self._conns[path] = held
            version = held[0].execute("PRAGMA data_version").fetchone()[0]
        return f"{_sig(st)}|{_stat_sig(path + '-wal')}|dv={version}"

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


def fingerprint(inp: Any) -> str | None:
    """The fingerprint of one input, None when it cannot be told (treated as "changed" by the loop).

    One-shot: a Sqlite input is opened, read and closed here. The loop keeps its own connections between polls.
    """
    fp = _Fingerprinter()
    try:
        return fp.fingerprint(inp)
    finally:
        fp.close()


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
    running_since: float | None = None  # when the derive now running started; None when none is running
    run_fp: str = ""  # the inputs fingerprint the running derive started from
    run_write_gen: int = 0  # the write_gen the running derive was taken for
    timed_out: bool = False  # the running derive passed its deadline and a timeout was published
    detached: bool = False  # the panel left the plan set while its derive was running (the result is discarded)
    pub_gen: int | None = None  # the gen the last publish of this loop answered; None before the first
    pub_write_gen: int = 0  # the highest write_gen this loop has published for the panel


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
    st: PanelState  # the state the job was made for: a panel retired and added again is a different state


# --------------------------------------------------------------------------------------------------- the loop


class DeriveLoop:
    """The poll thread plus a small pool of derive threads, publishing through `writer`.

    Single use: start() once, stop() once at the end. A second start(), or one after stop(), raises RuntimeError; a
    restart is a new DeriveLoop.

    Locks: `_lock` (plans, states, queue, stop flag) and `_write_lock` (every writer call). Code holding `_lock` never
    takes `_write_lock`; code holding `_write_lock` may take `_lock` for a short state update. `on_publish` runs with
    neither held.
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
        on_publish: Callable[[str, int, int], None] | None = None,
    ) -> None:
        self._writer = writer
        self._plans = dict(plans)
        self._derive_fn = derive_fn
        self._threads_n = max(1, int(threads))
        self._poll_s = poll_s
        self._backstop_s = backstop_s
        self._token_fn = token_fn
        self._clock = clock
        self._on_publish = on_publish
        self._fingerprinter = _Fingerprinter()
        self._states = {pid: PanelState() for pid in self._plans}
        self._lock = threading.Lock()  # states, queue, stop flag
        self._cv = threading.Condition(self._lock)  # workers wait here for queued jobs
        self._write_lock = threading.Lock()  # serialises every writer call
        self._wake = threading.Event()
        self._queue: dict[str, _Job] = {}
        self._stopping = False
        self._started = False
        self._threads: list[threading.Thread] = []
        self._live = 0  # threads started and not yet ended
        self._fp_closed = False
        # Write failures by source: "heartbeat", "stream", "poll", "timeout:<pid>", "publish:<pid>". A source sets its own key
        # when it fails and pops it when it next succeeds (or, for a panel, when it no longer owes the write); nothing
        # else touches a key. Insertion order is failure order. Guarded by `_lock`.
        self._write_errors: dict[str, BaseException] = {}
        self._last_event_at: float | None = None
        self._last_views: dict[Any, str | None] | None = None
        self._plans_version = 0  # bumped by every change of the plan set
        self._pruned_version = 0  # the plans version the fingerprinter's held connections were last trimmed for
        self._detached_n: dict[str, int] = {}  # per panel id: retired derives still running
        self._hook_error: BaseException | None = None
        self._hook_errors = 0

    @property
    def last_publish_error(self) -> BaseException | None:
        """The most recently failed write source's exception, or None when every source is clean."""
        with self._lock:
            return next(reversed(self._write_errors.values()), None)

    @property
    def last_hook_error(self) -> BaseException | None:
        """The exception of the last `on_publish` call if it raised, None once a later call succeeded."""
        with self._lock:
            return self._hook_error

    @property
    def hook_errors(self) -> int:
        """How many `on_publish` calls have raised (they are swallowed, never stop the loop)."""
        with self._lock:
            return self._hook_errors

    def _fail(self, key: str, exc: BaseException) -> None:
        with self._lock:
            self._write_errors.pop(key, None)  # re-inserted last: the most recent failure reads last
            self._write_errors[key] = exc

    def _succeed(self, key: str) -> None:
        with self._lock:
            self._write_errors.pop(key, None)

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

    def set_plans(self, plans: Iterable[PanelPlan]) -> None:
        """Replace the plan set with `plans`; safe from any thread, before or after start().

        A new id is derived on the next poll. A kept id keeps its derive history (when it last derived, its dirty
        mark, its token); its inputs fingerprint is kept when its inputs are equal and dropped when they changed. An id that is not in `plans` is retired (see `retire`). The swap
        is one step: a poll sees the old set or the new one, never a mixture. Two plans with one id raise ValueError
        before anything changes.
        """
        new: dict[str, PanelPlan] = {}
        for plan in plans:
            if plan.id in new:
                raise ValueError(f"two plans for panel id {plan.id!r}")
            new[plan.id] = plan
        self._swap_plans(lambda _current: new)

    def retire(self, panel_id: str) -> None:
        """Drop one panel from the plan set (no effect when it is not in it); safe from any thread.

        A job queued for it is dropped. A derive already running finishes but its result is discarded, and nothing
        is published for the id once this returns. The id is not derived again until the running one has ended, even
        if it is added back. May wait for the publish in progress, because it takes the writer lock to be sure.
        """
        self._swap_plans(lambda current: {pid: plan for pid, plan in current.items() if pid != panel_id})

    def _swap_plans(self, make: Callable[[dict[str, PanelPlan]], dict[str, PanelPlan]]) -> None:
        """Install the plan set `make(current)` returns. The writer lock is held when any panel leaves: a publish is
        checked against the plan set under that lock, so none can land after the retiring call returns."""
        hold_write = False
        while True:
            with (self._write_lock if hold_write else nullcontext()):
                with self._lock:
                    new = make(self._plans)
                    gone = [pid for pid in self._plans if pid not in new]
                    if gone and not hold_write:
                        hold_write = True
                        continue  # take the writer lock first, then look again
                    old = self._plans
                    for pid in gone:
                        st = self._states.pop(pid)
                        if self._queue.pop(pid, None) is not None:
                            st.busy = False
                        if st.running_since is not None:
                            st.detached = True
                            self._detached_n[pid] = self._detached_n.get(pid, 0) + 1
                        self._write_errors.pop(f"publish:{pid}", None)
                        self._write_errors.pop(f"timeout:{pid}", None)
                    for pid, plan in new.items():
                        st = self._states.get(pid)
                        if st is None:
                            self._states[pid] = PanelState()
                        elif old[pid].inputs != plan.inputs:
                            st.inputs_fp = None
                    self._plans = new
                    self._plans_version += 1
                break
        self._wake.set()

    def deriving(self) -> list[str]:
        """The ids queued or running right now, sorted (a copy)."""
        with self._lock:
            return sorted(pid for pid, st in self._states.items() if st.busy)

    def pending(self, panel_id: str) -> bool:
        """Whether a derive of this panel is queued or running (False for an id the loop does not hold)."""
        with self._lock:
            st = self._states.get(panel_id)
            return st is not None and st.busy

    def start(self) -> None:
        with self._lock:
            if self._started or self._stopping:
                raise RuntimeError("a DeriveLoop starts once and does not restart after stop(): make a new one")
            self._started = True
            self._threads = [threading.Thread(target=self._thread_main, args=(self._poll_loop,), name="derive-poll", daemon=True)]
            for i in range(self._threads_n):
                self._threads.append(
                    threading.Thread(target=self._thread_main, args=(self._worker,), name=f"derive-{i}", daemon=True)
                )
            self._live = len(self._threads)
            for t in self._threads:  # under the lock stop() takes, so stop() never sees a thread not yet started
                t.start()

    def stop(self, timeout: float | None = None) -> list[threading.Thread]:
        """End the loop for good; wait for the threads (`timeout` seconds in all; None waits as long as it takes).

        Jobs queued but not started are dropped. A derive already running finishes and publishes. The fingerprinter's
        connections are closed once every thread has ended: here when they all did, else by the last one to end.
        Returns the threads still alive when the wait ran out (empty after a full wait).
        """
        with self._cv:
            self._stopping = True
            for job in self._queue.values():
                job.st.busy = False
            self._queue.clear()
            threads = list(self._threads)
            self._cv.notify_all()
        self._wake.set()
        deadline = None if timeout is None else time.monotonic() + timeout
        for t in threads:
            t.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
        self._close_fingerprinter_if_idle()
        return [t for t in threads if t.is_alive()]

    def _close_fingerprinter_if_idle(self) -> None:
        with self._lock:
            done = self._stopping and self._live == 0 and not self._fp_closed
            if done:
                self._fp_closed = True
        if done:
            self._fingerprinter.close()

    def _thread_main(self, target: Callable[[], None]) -> None:
        try:
            target()
        finally:
            with self._lock:
                self._live -= 1
            self._close_fingerprinter_if_idle()

    def run_once(self, now: float) -> list[str]:
        """Evaluate dueness once and run the due derives to completion on the calling thread.

        `now` is the derive start time and the loop's clock supplies the finish time, so a test hands one fake clock
        to both.
        """
        jobs = self._evaluate(now)
        ran = []
        for job in jobs:
            with self._lock:
                if self._states.get(job.pid) is not job.st:
                    continue  # retired since the poll
                job.st.busy = True
            fatal = self._run_job(job, now)
            ran.append(job.pid)
            if fatal is not None:
                raise fatal
        return ran

    # ---- the poll

    def _views(self, plans: Mapping[str, PanelPlan]) -> tuple[dict[str, InputsView], dict[Any, str | None]]:
        seen: dict[Any, str | None] = {}
        views: dict[str, InputsView] = {}
        for pid, plan in plans.items():
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
        notices = []
        with self._lock:
            late = [
                (pid, st.running_since)
                for pid, st in self._states.items()
                if st.running_since is not None and not st.timed_out
                and now - st.running_since >= (
                    DERIVE_TIMEOUT_S if self._plans[pid].timeout_s is None else self._plans[pid].timeout_s
                )
            ]
        for pid, since in late:
            with self._write_lock:
                with self._lock:
                    st = self._states.get(pid)
                    if st is None or st.running_since != since or st.timed_out:
                        continue  # it finished (and published) or was retired meanwhile
                    run_fp, run_gen = st.run_fp, st.run_write_gen
                try:
                    gen = self._writer.publish(
                        pid, status="timeout", body=None, started_at=since, derived_at=now,
                        duration_ms=max(0, int(round((now - since) * 1000))), inputs_fp=run_fp, write_gen=run_gen,
                        error=f"derive still running after {now - since:.0f} s",
                    )
                except Exception as exc:
                    self._fail(f"timeout:{pid}", exc)  # timed_out stays False: the next poll tries again
                else:
                    with self._lock:
                        self._write_errors.pop(f"timeout:{pid}", None)
                        if st.running_since == since:
                            st.timed_out = True  # only once the timeout row is in the store
                    notices.append(self._note_publish(pid, st, gen, run_gen))
        for notice in notices:
            self._fire_hook(notice)

    def _evaluate(self, now: float) -> list[_Job]:
        """One poll: heartbeat, the inputs stream, then the due panels not already busy, as jobs in order."""
        with self._lock:
            plans = dict(self._plans)
            version = self._plans_version
        if version != self._pruned_version:
            self._pruned_version = version
            self._fingerprinter.keep_sqlite({inp.path for p in plans.values() for inp in p.inputs if isinstance(inp, Sqlite)})
        views, seen = self._views(plans)
        token: Any = _UNSET
        if self._token_fn is not None:
            try:
                token = self._token_fn()
            except Exception:
                token = _UNSET
        if self._last_views is not None and any(
            self._last_views[inp] != fp for inp, fp in seen.items() if inp in self._last_views
        ):
            self._last_event_at = now  # the last change seen; the first poll's fingerprints are not a change
        self._last_views = seen
        self._publish_timeouts(now)
        writer_ok = False
        with self._write_lock:
            try:
                self._writer.heartbeat(now)
            except Exception as exc:  # a failing writer must not kill the loop
                self._fail("heartbeat", exc)
            else:
                writer_ok = True
                self._succeed("heartbeat")
        with self._lock:
            # Only panels whose plan is still the one the views were read for, and with no retired derive still running.
            current = {
                pid: plan for pid, plan in plans.items()
                if self._plans.get(pid) == plan and not self._detached_n.get(pid)
            }
            due = due_panels(current, self._states, views, token, now, self._backstop_s)
            # A panel that is neither due nor busy owes no publish (its input went back): its failure no longer counts.
            # One left out of `current` is not judged this poll.
            owing = {pid for pid, _ in due} | (set(self._states) - set(current))
            for pid in self._states:
                if pid not in owing and not self._states[pid].busy:
                    self._write_errors.pop(f"publish:{pid}", None)
                if self._states[pid].running_since is None:
                    self._write_errors.pop(f"timeout:{pid}", None)  # the derive ended: no timeout row is owed
            jobs = [_Job(pid, reason, views[pid].fp, token, self._states[pid]) for pid, reason in due]
            degraded = [name for pid, v in views.items() if pid in self._states for name in (*v.unknown_names, *v.stale_locks)]
            degraded += [f"{pid}: derive timeout" for pid, st in self._states.items() if st.timed_out]
            degraded += [f"write error {key}: {type(e).__name__}: {e}" for key, e in self._write_errors.items()]
        if writer_ok:
            with self._write_lock:
                try:
                    self._writer.set_stream(
                        "inputs",
                        state="degraded" if degraded else "ok",
                        last_event_at=self._last_event_at,
                        detail="; ".join(degraded),
                    )
                except Exception as exc:
                    self._fail("stream", exc)
                else:
                    self._succeed("stream")
        return jobs

    def _poll_loop(self) -> None:
        while True:
            with self._cv:
                if self._stopping:
                    return
            self._wake.clear()
            try:
                jobs = self._evaluate(self._clock())
            except Exception as exc:  # one bad poll must not end the loop
                self._fail("poll", exc)
                jobs = []
            else:
                self._succeed("poll")
            with self._cv:
                if self._stopping:
                    return
                for job in jobs:
                    if self._states.get(job.pid) is job.st and not job.st.busy:  # not retired since the poll
                        job.st.busy = True
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

    def _run_job(self, job: _Job, started: float) -> BaseException | None:
        """Run one derive and publish it; the panel is not busy afterwards whatever happened.

        A derive whose publish failed changes none of the panel's scheduling state, so the panel is still due at the next
        poll. A job taken before stop() but not yet begun is dropped. Returns a non-Exception BaseException the
        derive raised (the caller decides whether to re-raise), else None.
        """
        pid = job.pid
        st = job.st
        with self._lock:
            if self._stopping or self._states.get(pid) is not st:  # stopping, or retired after the job was taken
                st.busy = False
                return None
            write_gen = st.dirty_gen if st.dirty_gen is not None else 0
            st.dirty_gen = None  # a dirty arriving from here on is for the next derive
            st.running_since, st.run_fp, st.run_write_gen, st.timed_out = started, job.fp, write_gen, False
        body: bytes | None = None
        status, error, valid_until = "error", None, None
        finished: float | None = None
        published = False
        notice: tuple[str, int, int] | None = None
        wake = False
        fatal: BaseException | None = None
        try:
            try:
                result = self._derive_fn(pid)
                if result.status not in KEEP_BODY_STATUSES:
                    body = encode_body(result.body)  # a failed or timed-out result never carries a body to the store
                status, error, valid_until = result.status, result.error, result.valid_until
                if status not in KNOWN_STATUSES:  # the store's vocabulary is closed: keep the row, say what it was
                    error = error or f"unknown derive status {status!r}"
                    status = "partial"
            except BaseException as exc:
                body = None
                error = f"{type(exc).__name__}: {exc}"
                if not isinstance(exc, Exception):
                    fatal = exc
            finished = self._clock()
            duration_ms = max(0, int(round((finished - started) * 1000)))
            # A valid_until at or before the finish would re-derive on every poll: treat it as no expiry, also in the store.
            if valid_until is not None and valid_until <= finished:
                valid_until = None
            try:
                with self._write_lock:
                    with self._lock:
                        st.running_since = None  # the timeout publisher checks this under the write lock
                        current = self._states.get(pid) is st
                    # Retiring a panel takes this lock too, so a panel still current here stays so until the publish ends.
                    if current:
                        # An error or timeout row carries the write_gen too: a reader waiting for that write is answered
                        # (with that status) instead of waiting out its bound. It is not derived again for it.
                        gen = self._writer.publish(
                            pid,
                            status=status,
                            body=body,
                            started_at=started,
                            derived_at=finished,
                            duration_ms=duration_ms,
                            valid_until=valid_until,
                            inputs_fp=job.fp,
                            write_gen=write_gen,
                            error=error,
                        )
                        published = True
                        self._succeed(f"publish:{pid}")
                        notice = self._note_publish(pid, st, gen, write_gen)
            except Exception as exc:
                self._fail(f"publish:{pid}", exc)
        finally:
            with self._cv:
                st.running_since, st.timed_out = None, False
                late_dirty = st.dirty_gen is not None  # a dirty that arrived during this derive
                if not published and write_gen and (st.dirty_gen is None or st.dirty_gen < write_gen):
                    st.dirty_gen = write_gen  # the write was not published: the next derive still owes it
                if published:
                    st.started_at = started
                    st.derived_at = finished
                    st.inputs_fp = job.fp
                    st.token = job.token
                    st.valid_until = valid_until
                st.busy = False
                if st.detached:  # a retired panel's derive has ended: its id may be derived again
                    st.detached = False
                    left = self._detached_n.get(pid, 0) - 1
                    if left > 0:
                        self._detached_n[pid] = left
                    else:
                        self._detached_n.pop(pid, None)
                    wake = True  # an id added back meanwhile was held for this derive
            if late_dirty or wake:
                self._wake.set()
        hook_fatal = self._fire_hook(notice)
        return fatal if fatal is not None else hook_fatal

    def _note_publish(self, pid: str, st: PanelState, gen: int, write_gen: int) -> tuple[str, int, int] | None:
        """Record a landed publish; the (panel, gen, write_gen) to tell `on_publish` when the gen or the write_gen moved.

        The first publish this loop makes for a panel counts as a move, since what the store held before is not known.
        """
        with self._lock:
            moved = st.pub_gen is None or gen is None or gen != st.pub_gen or write_gen > st.pub_write_gen
            st.pub_gen = gen
            st.pub_write_gen = max(st.pub_write_gen, write_gen)
            seen = st.pub_write_gen
        return (pid, gen, seen) if moved and self._on_publish is not None else None

    def _fire_hook(self, notice: tuple[str, int, int] | None) -> BaseException | None:
        """Call `on_publish` with no lock held. An Exception is counted and swallowed; any other BaseException is
        recorded too and returned, so run_once raises it as it does for a derive's."""
        if notice is None:
            return None
        try:
            self._on_publish(*notice)
        except BaseException as exc:
            with self._lock:
                self._hook_error = exc
                self._hook_errors += 1
            return None if isinstance(exc, Exception) else exc
        with self._lock:
            self._hook_error = None
        return None

