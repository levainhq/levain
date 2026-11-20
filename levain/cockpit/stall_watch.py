"""A stall watchdog for the cockpit process: it names who held the interpreter when this thread's wake came late.

One daemon thread sleeps :data:`INTERVAL_S` and measures how late each wake is against a monotonic clock. A wake later
than :data:`LATE_S` is reported as "watch late": every thread in the process waits on the same interpreter, so a late
wake usually means the whole process stalled (a GIL convoy, a long collection, a blocked call that holds the
interpreter), but it can also be only this thread starved. The report is one stderr line and one dump file.

Two captures, each saying what it holds:

* The live capture. Each tick arms ``faulthandler.dump_traceback_later`` for :data:`LATE_S` into a scratch file; the
  next tick cancels and re-arms it. If a tick does not come back in time, faulthandler's own C thread writes the Python
  stack of every thread into the scratch file while the stall is still going, with no help from the interpreter lock.
  On a late wake the scratch's contents are the dump's body: they name the holder, because they were taken before it let
  go. The timer is process-wide (one at a time), so arming it replaces any other ``dump_traceback_later`` timer.
* The fallback. When the scratch is empty or was never opened (a directory that could not be made, an arm that
  failed), the dump is the Python stack of every thread taken after the wake. That mostly misses the holder, which has
  released the interpreter by then; it shows what the other threads were waiting in.

The loop does nothing but arm, sleep, compare and, on a late wake, report. Below the threshold it walks no stack.

The dump is plain file writes, written before anything is printed (a stderr write can block on a lock a stalled thread
holds); it does not use ``logging``, whose handler locks a stalled thread could hold. Its thread names come from a copy
of ``threading._active`` rather than ``threading.enumerate``, which takes the lock that guards thread start. A failed
write (a full disk, a missing directory) is one stderr line and never raises. The directory is made, and the files
opened, by the watch thread itself, not by the code that starts it.
"""
from __future__ import annotations

import codecs
import faulthandler
import os
import stat
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

INTERVAL_S = 0.1      # how long each sleep asks for; nothing in the loop runs faster than this
LATE_S = 1.0          # a wake later than this (past the INTERVAL_S it asked for) is a stall; also the live timer's span
MIN_DUMP_GAP_S = 30.0  # at most one dump file per this span; a late wake inside it still prints its stderr line
KEEP_DUMPS = 20       # the newest this-many dump files stay; older ones are deleted after each new dump
JOIN_S = 2.0          # how long stop() (and a start() that finds a stopping thread) waits before giving up
MAX_THREADS = 64      # the fallback dump lists at most this many threads (the live capture has faulthandler's own cap)
MAX_FRAMES = 40       # the fallback dump keeps at most this many innermost frames per thread
MAX_DUMP_BYTES = 262144  # no dump file grows past this many bytes of body; the rest is cut with a one-line note
MAX_NAME_TRIES = 8    # a kept dead process's dump asks this many names for a free one before it gives up
MAX_ARM_FAILURES = 3 # this many arms of the live timer failing in a row turn the live capture off; a success resets it

DIR_MODE = 0o700      # the stalls directory, when this module makes it
FILE_MODE = 0o600     # every file this module makes in it

DUMP_PREFIX = "stall-"
DUMP_SUFFIX = ".txt"
SCRATCH_PREFIX = "live-stacks."        # the scratch is SCRATCH_PREFIX + pid + SCRATCH_SUFFIX: one file per process, so
SCRATCH_SUFFIX = ".scratch"            # two cockpits on one data root never share one; outside DUMP_PREFIX on purpose


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

    ``pid`` names this watch's scratch file (it defaults to this process's); a test passes another to stand for a
    second process.

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
        self._scratch = None                  # the open scratch file object the live timer writes into, or None
        self._scratch_path: Path | None = None
        self._dumps_off = False               # set by _prepare when the directory is not one this module will write to
        self._arm_failures = 0                # consecutive failed arms of the live timer

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
        if self._scratch is not None:
            _cancel_live_timer()
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
        """Prepare, then arm, sleep, measure the lateness of each wake, report a late one; return when stopped.

        ``stop`` is the event this run ends on (``start`` passes its thread's own); None is the current one. The
        directory and the scratch file are made here, at the top, so the code that starts the watch never touches disk.
        Nothing here raises."""
        stop = self._stop if stop is None else stop
        sleep = self._sleep_override if self._sleep_override is not None else stop.wait
        self._prepare()
        try:
            while not stop.is_set():
                try:
                    due = self._clock() + INTERVAL_S
                    self._arm()
                    sleep(INTERVAL_S)
                    if self._scratch is not None:
                        _cancel_live_timer()      # the report below must not race the timer
                    if stop.is_set():
                        return
                    now = self._clock()
                    late = now - due
                    if late > LATE_S:
                        self._report(late, now)
                except Exception as exc:     # the watchdog must outlive anything it meets
                    say(f"cockpit stall: watch error: {type(exc).__name__}: {exc}")
        finally:
            self._close_live()

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

    # ---- the directory and the scratch file -----------------------------------------------------------------

    def _prepare(self) -> None:
        """Make the stalls directory (only that one level, only under a root that exists) and open the scratch.

        An existing directory must be a real directory (not a symlink, not a file): otherwise the live capture and the
        dumps are both off, said once. One owned by this user is set to :data:`DIR_MODE`. Scratch files of processes
        that are gone are kept as dumps when they hold text, then removed; one of a live process is never touched. A failure leaves the watch running with no
        live capture; a dump then fails with its own stderr line."""
        self._dumps_off = False
        self._arm_failures = 0
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
                say(f"cockpit stall: live capture and dumps off: {self._dir} is not a plain directory (a symlink or a file)")
                return
            if info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) != DIR_MODE:
                os.chmod(self._dir, DIR_MODE)
        except OSError as exc:
            say(f"cockpit stall: dump directory not checked: {type(exc).__name__}: {exc}")
            return
        self._remove_dead_scratches()
        fd, path = None, self._dir / scratch_name(self._pid)
        try:
            flags = os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags, FILE_MODE)
            try:
                os.fchmod(fd, FILE_MODE)
                handle = os.fdopen(fd, "wb", buffering=0)
            except BaseException:
                os.close(fd)
                try:
                    path.unlink()
                except OSError:
                    pass
                raise
            self._scratch, self._scratch_path = handle, path
        except Exception as exc:
            say(f"cockpit stall: live capture off: {type(exc).__name__}: {exc}")

    def _remove_dead_scratches(self) -> None:
        """Delete the scratch files in the directory that belong to a process that no longer exists. A non-empty one is
        first kept as a dump (see :meth:`_keep_dead_scratch`); one that could not be kept is left in place."""
        try:
            names = [p.name for p in self._dir.iterdir()]
        except OSError:
            return
        for name in names:
            pid = _scratch_pid(name)
            if pid is None or pid == self._pid or not _pid_is_gone(pid):
                continue
            try:
                if not self._keep_dead_scratch(self._dir / name, pid):
                    continue
                (self._dir / name).unlink()
            except OSError:
                pass

    def _arm(self) -> None:
        """Empty the scratch and re-arm the live timer for :data:`LATE_S` from now. Anything in the scratch at this
        point is from a stall too short to report; it is dropped."""
        handle = self._scratch
        if handle is None:
            return
        try:
            _cancel_live_timer()
            os.ftruncate(handle.fileno(), 0)
            faulthandler.dump_traceback_later(LATE_S, repeat=False, file=handle, exit=False)
        except Exception as exc:
            self._arm_failures += 1
            if self._arm_failures >= MAX_ARM_FAILURES:
                say(f"cockpit stall: live capture off: {type(exc).__name__}: {exc}")
                self._close_live()
            else:
                say(f"cockpit stall: live capture arm failed ({self._arm_failures} of {MAX_ARM_FAILURES}): "
                    f"{type(exc).__name__}: {exc}")
        else:
            self._arm_failures = 0

    def _close_live(self) -> None:
        """Cancel the timer, close the scratch and remove it. Safe to call with nothing open."""
        handle, path = self._scratch, self._scratch_path
        self._scratch = self._scratch_path = None
        if handle is None:
            return
        _cancel_live_timer()
        try:
            handle.close()
        except Exception:
            pass
        try:
            if path is not None:
                path.unlink()
        except OSError:
            pass

    # ---- the dump --------------------------------------------------------------------------------------------

    def _open_new_dump(self, path: Path):
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
            budget = _Budget(MAX_DUMP_BYTES)
            if not self._copy_live(handle, budget):
                _write_stacks(handle, budget)
        self._trim_dumps()
        return path

    def _keep_dead_scratch(self, scratch: Path, pid: int) -> bool:
        """Keep a dead process's non-empty scratch as a dump named for its pid and "killed". True when the scratch may
        now be deleted: it was kept, or it is empty, not a plain file or not readable. False when the dump could not
        be written, so the scratch stays for the next start.

        The scratch holds what faulthandler wrote while a stall was going; a process that was killed in the stall never
        reached the late wake that would have written a report from it. The dump takes the same byte budget as any
        other, takes a name no file has, and counts in :data:`KEEP_DUMPS`; the trim never deletes it."""
        try:
            fd = os.open(scratch, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except OSError:
            return True
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
                    handle.write(f"cockpit stall: process {pid} ended with live stacks in its scratch file, last written "
                                 f"{written}; no late-wake report was written from them\n")
                    _copy_fd(fd, handle, _Budget(MAX_DUMP_BYTES), "\n--- live stacks: left by that process ---\n")
            except Exception as exc:
                say(f"cockpit stall: dump of dead process {pid}'s stacks not written: {type(exc).__name__}: {exc}")
                try:
                    path.unlink()
                except OSError:
                    pass
                return False
            self._trim_dumps(keep=path)
            return True
        except Exception:
            return True              # not readable or not statable: the scratch goes as it did before
        finally:
            os.close(fd)

    def _copy_live(self, handle, budget: "_Budget") -> bool:
        """Copy the scratch's contents into the dump. False (nothing written) when there is no scratch or it is empty.

        At most one byte more than :data:`MAX_DUMP_BYTES` is read: the extra byte is what lets the budget see the
        overflow and write its note."""
        scratch = self._scratch
        if scratch is None:
            return False
        return _copy_fd(scratch.fileno(), handle, budget,
                        f"\n--- live stacks: faulthandler wrote these {LATE_S:g} s into the stall, "
                        f"before this thread woke ---\n")


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


def _cancel_live_timer() -> None:
    try:
        faulthandler.cancel_dump_traceback_later()
    except Exception:
        pass


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
