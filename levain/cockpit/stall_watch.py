"""A stall watchdog for the cockpit process: it reports when this thread's wake came late, and what the threads were in.

One daemon thread sleeps :data:`INTERVAL_S` and measures how late each wake is against a monotonic clock. A wake later
than :data:`LATE_S` is reported as "watch late": every thread in the process waits on the same interpreter, so a late
wake usually means the whole process stalled (a GIL convoy, a long collection, a blocked call that holds the
interpreter), but it can also be only this thread starved. The report is one stderr line and one dump file.

The dump is the Python stack of every thread taken after the wake. That mostly misses the holder, which has released the
interpreter by then; it shows what the other threads were waiting in.

There is no live capture: an earlier version armed ``faulthandler.dump_traceback_later`` every tick so a C thread could
write the stacks during the stall. That dump reads every thread's frames without the interpreter lock while they run.
On 2026-11-21, in a process of about 100 threads, flow's bridge (which starts this watch) sat on one core for hours
with that thread spinning in the interpreter's thread-state check (``tstate_is_freed``) after a frame it printed as
``<invalid frame>``, and the ``cancel_dump_traceback_later`` the next tick makes waits for that thread (levain#238).
Nothing in this module calls faulthandler's timed dump or cancels it.

The loop does nothing but sleep, compare and, on a late wake, report. Below the threshold it walks no stack.
The module still keeps a dead process's scratch file as a dump (an earlier version's), so a process that was killed
during a stall under that version leaves its stacks.

The dump is plain file writes, written before anything is printed (a stderr write can block on a lock a stalled thread
holds); it does not use ``logging``, whose handler locks a stalled thread could hold. Its thread names come from a copy
of ``threading._active`` rather than ``threading.enumerate``, which takes the lock that guards thread start. A failed
write (a full disk, a missing directory) is one stderr line and never raises. The directory is made, and the files
opened, by the watch thread itself, not by the code that starts it.
"""
from __future__ import annotations

import codecs
import errno
import os
import stat
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TextIO

INTERVAL_S = 0.1      # how long each sleep asks for; nothing in the loop runs faster than this
LATE_S = 1.0          # a wake later than this (past the INTERVAL_S it asked for) is a stall
MIN_DUMP_GAP_S = 30.0  # at most one dump file per this span; a late wake inside it still prints its stderr line
KEEP_DUMPS = 20       # the newest this-many dump files stay; older ones are deleted after each new dump
JOIN_S = 2.0          # how long stop() (and a start() that finds a stopping thread) waits before giving up
MAX_THREADS = 64      # the dump lists at most this many threads
MAX_FRAMES = 40       # the dump keeps at most this many innermost frames per thread
MAX_DUMP_BYTES = 262144  # no dump file grows past this many bytes of body; the rest is cut with a one-line note
MAX_NAME_TRIES = 8    # a kept dead process's dump asks this many names for a free one before it gives up

DIR_MODE = 0o700      # the stalls directory, when this module makes it
FILE_MODE = 0o600     # every file this module makes in it

# An open of a dead process's scratch that fails with one of these says nothing is left to keep: the file is gone
# (ENOENT), or a symlink we refuse to follow (O_NOFOLLOW gives ELOOP, and EMLINK on some BSDs). A fifo is not here: it
# opens under O_NONBLOCK and the S_ISREG check refuses it. Any other error, an unreadable file (EACCES, EPERM) included,
# says nothing about whether the stacks are worth keeping: the scratch stays and is named on stderr.
_SCRATCH_GONE_ERRNOS = frozenset({errno.ENOENT, errno.ELOOP, errno.EMLINK})

DUMP_PREFIX = "stall-"
DUMP_SUFFIX = ".txt"
SCRATCH_PREFIX = "live-stacks."        # an earlier version wrote SCRATCH_PREFIX + pid + SCRATCH_SUFFIX, one file per process;
SCRATCH_SUFFIX = ".scratch"            # outside DUMP_PREFIX on purpose; only the harvest of such leftovers reads the name


def scratch_name(pid: int) -> str:
    return f"{SCRATCH_PREFIX}{pid}{SCRATCH_SUFFIX}"


def _scratch_pid(name: str) -> int | None:
    """The pid in a scratch file name, or None for any other name."""
    if len(name) <= len(SCRATCH_PREFIX) + len(SCRATCH_SUFFIX):
        return None
    if not (name.startswith(SCRATCH_PREFIX) and name.endswith(SCRATCH_SUFFIX)):
        return None
    mid = name[len(SCRATCH_PREFIX):-len(SCRATCH_SUFFIX)]
    return int(mid) if mid.isascii() and mid.isdigit() else None


def _pid_is_gone(pid: int) -> bool:
    """True only when the system says no such process. A process of another user, or any doubt, counts as alive."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except Exception:
        return False
    return False


def say(line: str) -> None:
    """One stderr line that never raises: a closed, broken or odd stderr is not worth a thread or a serve."""
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:
        pass


class StallWatch:
    """Watch for stalls. ``start()`` and ``stop()`` are idempotent; ``dumps()`` lists the dump files.

    ``pid`` is this watch's process (it defaults to this process's): a scratch file left under that pid is an earlier
    version's, and is kept or removed like a dead process's; a test passes another to stand for a second process.

    ``clock`` is the monotonic clock the lateness and the dump gap are read from. ``sleep`` takes a number of seconds;
    left None it is the running thread's own stop event's wait, so ``stop()`` ends that thread at once instead of after
    the current sleep. A test passes both to drive the loop with no real time (``run`` is the loop, callable on the
    calling thread).

    Each thread ``start()`` makes has its own stop event, so a stop that has not finished cannot be undone by the next
    start: ``start()`` waits for a stopping thread (at most :data:`JOIN_S`) and refuses to run a second watcher while the
    old one lives.
    """

    def __init__(self, dump_dir: Path | str, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], object] | None = None, pid: int | None = None) -> None:
        self._dir = Path(dump_dir)
        self._pid = os.getpid() if pid is None else pid
        self._clock = clock
        self._sleep_override = sleep
        self._stop = threading.Event()        # the current thread's event; start() replaces it for each new thread
        self._thread: threading.Thread | None = None
        self._state = threading.Lock()        # guards only start/stop's view of the thread; the loop never takes it
        self._last_dump: float | None = None  # clock reading of the last dump attempt; None before the first
        self._last_path: Path | None = None   # the newest dump file this watch wrote; None before the first
        self._seq = 0                         # tiebreak so two dumps in one timestamp tick keep distinct, ordered names
        self._dumps_off = False               # set by _prepare when the directory is not one this module will write to

    # ---- lifecycle -------------------------------------------------------------------------------------------

    def start(self) -> None:
        """Start the daemon thread; a call while it runs does nothing, and one while it is stopping waits for it."""
        with self._state:
            old = self._thread
            if old is not None and old.is_alive():
                if not self._stop.is_set():
                    return
                old.join(JOIN_S)
                if old.is_alive():
                    say("cockpit stall: watch not restarted: the old thread has not ended")
                    return
            stop = threading.Event()
            self._stop = stop
            thread = threading.Thread(target=self.run, args=(stop,), name="cockpit-stall-watch", daemon=True)
            thread.start()
            self._thread = thread        # kept only once it has started: stop() never joins a thread that never ran

    def stop(self) -> None:
        """Ask the thread to end and wait for it, at most :data:`JOIN_S`. Safe to call twice, or before ``start``.

        The thread is kept (not forgotten) until it has ended, so a ``start`` after a stop that timed out sees it."""
        with self._state:
            thread, stop = self._thread, self._stop
        stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(JOIN_S)

    def dumps(self) -> list[Path]:
        """The dump files in the directory, oldest first. An unreadable or missing directory lists none."""
        try:
            found = [p for p in self._dir.iterdir() if p.name.startswith(DUMP_PREFIX) and p.name.endswith(DUMP_SUFFIX)]
        except OSError:
            return []
        return sorted(found, key=lambda p: p.name)

    # ---- the loop --------------------------------------------------------------------------------------------

    def run(self, stop: threading.Event | None = None) -> None:
        """Prepare, then sleep, measure the lateness of each wake, report a late one; return when stopped.

        ``stop`` is the event this run ends on (``start`` passes its thread's own); None is the current one. The
        directory is made here, at the top, so the code that starts the watch never touches disk. Nothing here raises."""
        stop = self._stop if stop is None else stop
        sleep = self._sleep_override if self._sleep_override is not None else stop.wait
        self._prepare()
        while not stop.is_set():
            try:
                due = self._clock() + INTERVAL_S
                sleep(INTERVAL_S)
                if stop.is_set():
                    return
                now = self._clock()
                late = now - due
                if late > LATE_S:
                    self._report(late, now)
            except Exception as exc:     # the watchdog must outlive anything it meets
                say(f"cockpit stall: watch error: {type(exc).__name__}: {exc}")

    def _report(self, late: float, now: float) -> None:
        """Write the dump file first, then print. A wake inside the dump gap prints its line and says so."""
        if self._dumps_off:
            say(f"cockpit stall: watch late {late:.2f} s (dumps off for this directory)")
            return
        if self._last_dump is not None and now - self._last_dump < MIN_DUMP_GAP_S:
            where = f"last dump {self._last_path}" if self._last_path is not None else "no dump written yet"
            say(f"cockpit stall: watch late {late:.2f} s (dump rate limited, one per {MIN_DUMP_GAP_S:g} s; {where})")
            return
        self._last_dump = now        # set before the write: a broken disk is tried once per gap, not once per wake
        try:
            path = self._write_dump(late)
        except Exception as exc:
            say(f"cockpit stall: watch late {late:.2f} s")
            say(f"cockpit stall: dump not written: {type(exc).__name__}: {exc}")
            return
        self._last_path = path
        say(f"cockpit stall: watch late {late:.2f} s, dump {path}")

    # ---- the directory and the dead processes' scratch files ------------------------------------------------

    def _prepare(self) -> None:
        """Make the stalls directory (only that one level, only under a root that exists).

        An existing directory must be a real directory (not a symlink, not a file): otherwise the dumps are off, said
        once. One owned by this user is set to :data:`DIR_MODE`. Scratch files of processes that are gone (an earlier
        version wrote them) are kept as dumps when each is a non-empty readable regular file, then removed; one that is
        empty, not a regular file or gone is removed unkept, and one that could not be kept (an unreadable one included)
        stays for the next start, named on stderr. One of a live process other than this one is never touched. A failure
        leaves the watch running; a dump then fails with its own stderr line."""
        self._dumps_off = False
        try:
            os.mkdir(self._dir, DIR_MODE)    # one level: a missing root is not made
        except FileExistsError:
            pass
        except OSError as exc:
            say(f"cockpit stall: dump directory not made: {type(exc).__name__}: {exc}")
            return
        try:
            info = os.lstat(self._dir)
            if not stat.S_ISDIR(info.st_mode):
                self._dumps_off = True
                say(f"cockpit stall: dumps off: {self._dir} is not a plain directory (a symlink or a file)")
                return
            if info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) != DIR_MODE:
                os.chmod(self._dir, DIR_MODE)
        except OSError as exc:
            say(f"cockpit stall: dump directory not checked: {type(exc).__name__}: {exc}")
            return
        self._remove_dead_scratches()

    def _remove_dead_scratches(self) -> None:
        """Delete the scratch files in the directory that belong to a process that no longer exists. A non-empty
        readable regular file is first kept as a dump (see :meth:`_keep_dead_scratch`); one that is empty, not a regular
        file or gone is deleted unkept; one that could not be kept, an unreadable one included, stays in place and is named
        on stderr."""
        try:
            names = [p.name for p in self._dir.iterdir()]
        except OSError:
            return
        for name in names:
            pid = _scratch_pid(name)
            # this process's own name is a leftover too: this version never writes a scratch file, so one under this pid is an
            # earlier version's, from a process that died and whose pid was reused
            if pid is None or (pid != self._pid and not _pid_is_gone(pid)):
                continue
            try:
                if not self._keep_dead_scratch(self._dir / name, pid):
                    continue
                (self._dir / name).unlink()
            except OSError:
                pass

    # ---- the dump --------------------------------------------------------------------------------------------

    def _open_new_dump(self, path: Path) -> TextIO:
        """Create ``path`` as a new dump file and return its text handle; an existing file is never opened."""
        # O_EXCL: never overwrite an earlier dump; FILE_MODE: the stacks are the cockpit's own, not the host's to read
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), FILE_MODE)
        try:
            return os.fdopen(fd, "w", encoding="utf-8", errors="replace")
        except BaseException:
            os.close(fd)
            try:
                path.unlink()
            except OSError:
                pass
            raise

    def _trim_dumps(self, keep: Path | None = None) -> None:
        """Delete the oldest dump files past :data:`KEEP_DUMPS`, never ``keep``."""
        found = self.dumps()
        excess = len(found) - KEEP_DUMPS
        if excess <= 0:
            return
        for old in [p for p in found if p != keep][:excess]:
            try:
                old.unlink()
            except OSError:
                pass                 # one file that will not go does not stop the rest, or the next dump

    def _write_dump(self, late: float) -> Path:
        self._seq += 1
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = self._dir / f"{DUMP_PREFIX}{stamp}-{self._seq:06d}{DUMP_SUFFIX}"
        handle = self._open_new_dump(path)
        with handle:
            handle.write(f"cockpit stall: watch late {late:.2f} s (this thread's wake was late: the process stalled, "
                         f"or only this thread was starved)\n")
            _write_stacks(handle, _Budget(MAX_DUMP_BYTES))
        self._trim_dumps(keep=path)
        return path

    def _keep_dead_scratch(self, scratch: Path, pid: int) -> bool:
        """Keep a dead process's non-empty scratch as a dump named for its pid and "killed". True when the scratch may
        now be deleted: it was kept, or it is empty, not a plain file, or gone (an open that fails with one of
        :data:`_SCRATCH_GONE_ERRNOS`). False when the scratch must stay for the next start: the dump could not be
        written, or anything else went wrong (an unreadable scratch, an open failing for EMFILE, EIO and the like, an
        unforeseen error); each says so in one stderr line naming the pid and the error.

        The scratch holds what faulthandler wrote while a stall was going; a process that was killed in the stall never
        reached the late wake that would have written a report from it. The dump takes the same byte budget as any
        other, takes a name no file has, and counts in :data:`KEEP_DUMPS`. The trim in this call keeps it; a later
        dump's trim deletes it once :data:`KEEP_DUMPS` newer dumps exist.

        A kill during the keep leaves a possibly partial dump plus the intact scratch, and the next start keeps the
        scratch again. A write that fails every time leaves the scratch in place and says so on every start."""
        try:
            fd = os.open(scratch, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except OSError as exc:
            if exc.errno in _SCRATCH_GONE_ERRNOS:
                return True
            say(f"cockpit stall: dump of dead process {pid}'s stacks not written: {type(exc).__name__}: {exc}")
            return False
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
                return True
            written = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(info.st_mtime))
            for _ in range(MAX_NAME_TRIES):
                self._seq += 1
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                path = self._dir / f"{DUMP_PREFIX}{stamp}-killed-pid{pid}-{self._seq:06d}{DUMP_SUFFIX}"
                try:
                    handle = self._open_new_dump(path)
                except FileExistsError:
                    continue
                except OSError as exc:
                    say(f"cockpit stall: dump of dead process {pid}'s stacks not written: {type(exc).__name__}: {exc}")
                    return False
                break
            else:
                say(f"cockpit stall: dump of dead process {pid}'s stacks not written: no free name")
                return False
            try:
                with handle:
                    handle.write(f"cockpit stall: process {pid} ended without stopping its watch; its scratch file held "
                                 f"live stacks, last written {written}, size {info.st_size} bytes; a late-wake report "
                                 f"may or may not have been written from them\n")
                    _copy_fd(fd, handle, _Budget(MAX_DUMP_BYTES), "\n--- live stacks: left by that process ---\n")
            except Exception as exc:
                say(f"cockpit stall: dump of dead process {pid}'s stacks not written: {type(exc).__name__}: {exc}")
                try:
                    path.unlink()
                except OSError:
                    pass
                return False
            try:
                self._trim_dumps(keep=path)
            except Exception:
                pass                 # the dump is written: a failed trim is not a reason to keep the scratch
            return True
        except Exception as exc:
            say(f"cockpit stall: dump of dead process {pid}'s stacks not written: {type(exc).__name__}: {exc}")
            return False             # anything unforeseen: the scratch stays, the line above is the signal
        finally:
            os.close(fd)

def _copy_fd(fd: int, handle, budget: "_Budget", banner: str) -> bool:
    """Copy a file's contents into the dump under ``banner``. False (nothing written) when the file is empty.

    At most one byte more than :data:`MAX_DUMP_BYTES` is read: the extra byte is what lets the budget see the
    overflow and write its note."""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    limit = MAX_DUMP_BYTES + 1
    offset = 0
    wrote = False
    while offset < limit:
        chunk = os.pread(fd, min(65536, limit - offset), offset)
        if not chunk:
            break
        if not wrote:
            handle.write(banner)
            wrote = True
        offset += len(chunk)
        budget.write(handle, decoder.decode(chunk))
    tail = decoder.decode(b"", final=True)      # a last write cut mid-character leaves a replacement, not nothing
    if tail:
        budget.write(handle, tail)
    if offset >= limit:
        budget.note_cut(handle)
    return wrote


class _Budget:
    """A byte budget for one dump's body: text past it is dropped and one note says so."""

    def __init__(self, limit: int) -> None:
        self.left = limit
        self.cut = False

    def write(self, handle, text: str) -> None:
        if self.cut:
            return
        data = text.encode("utf-8", "replace")
        if len(data) <= self.left:
            handle.write(text)
            self.left -= len(data)
            return
        # cut by encoded bytes, not characters; a character split by the cut is dropped whole
        handle.write(data[:max(self.left, 0)].decode("utf-8", "ignore"))
        self.left = 0
        self.note_cut(handle)

    def note_cut(self, handle) -> None:
        if self.cut:
            return
        self.cut = True
        handle.write(f"\n[dump cut at {MAX_DUMP_BYTES} bytes]\n")


def _thread_names() -> dict:
    """Thread names by ident, from a copy of ``threading._active``: a plain dict copy, no lock taken."""
    try:
        active = dict(threading._active)
        return {ident: getattr(thread, "name", "unknown") for ident, thread in active.items()}
    except Exception:
        return {}


def _write_stacks(handle, budget: _Budget) -> None:
    """The fallback body: the Python stack of up to :data:`MAX_THREADS` threads, each cut to its innermost
    :data:`MAX_FRAMES` frames, written thread by thread (never joined into one string)."""
    names = _thread_names()
    frames = sys._current_frames()
    ordered, shown, frame = [], [], None
    try:
        ordered = sorted(frames.items(), key=lambda item: str(names.get(item[0], "")))
        shown = ordered[:MAX_THREADS]
        budget.write(handle, f"\n--- fallback stacks: taken after the wake, so the holder has most likely let go; "
                             f"{len(shown)} of {len(ordered)} threads, innermost {MAX_FRAMES} frames each ---\n")
        for ident, frame in shown:
            budget.write(handle, f"\n--- thread {names.get(ident, 'unknown')} (ident {ident}) ---\n")
            for entry in traceback.format_stack(frame, limit=MAX_FRAMES):
                budget.write(handle, entry)
    finally:
        del frames, ordered, shown, frame   # a frame held past the dump keeps its locals alive
