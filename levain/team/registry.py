"""The team-view registry: how the cockpit (`levain serve`) finds the running `levain team view` servers.

One small JSON file per running view, in ``<levain home>/team-views/``, named ``lock1-<32 hex>.json``. A view is
listed for exactly as long as it holds an exclusive ``flock`` on its own entry file: the cockpit tries a shared lock
on each entry, and "a publisher holds it" is the only thing that can make that fail. Liveness is therefore a fact
the kernel holds (a lease that dies with the process, however it dies) and not a guess made by talking to whatever
happens to be listening on a port a dead view used to own. That guess is what the earlier network-probe design
kept losing.

The protocol, in one place:

* Only a PUBLISHER ever holds ``LOCK_EX``. Readers and pruners use ``LOCK_SH | LOCK_NB``, so contention always
  means "a publisher holds it" and a stalled pruner can never read as a live view.
* An entry's file is never visible unlocked, not even as a temp. On macOS and the BSDs it is created already locked
  (``open`` with ``O_EXLOCK``: "if creating a file with O_CREAT, the request for the lock will never fail", open(2)),
  under a hidden temp name, written, and renamed into place; a rename keeps the inode and its lock. On Linux it is
  created unnamed (``O_TMPFILE``), locked and written, and only then given its name (``linkat`` through
  ``/proc/self/fd``). Anywhere neither exists, or where the filesystem refuses them, the view does not register.
* The ``lock1-`` prefix is the liveness-generation marker. A future mechanism must use a different prefix, so an
  old view can never prune a newer view's files and a reader never has to parse a file to decide whether to trust it.
* The cockpit never deletes anything. A starting view prunes entries nobody holds, strictly before it publishes.
  It also sweeps the hidden temp files a killed view left behind. A temp is born locked, so one nobody holds is
  dead, whatever its age. The ``x`` in its name marks that grammar. A temp from levain 0.6.9 or older was created
  first and locked a moment later, so an unlocked one may belong to a publisher that is still starting. Those are
  left alone; an older version makes no new ones, so they are a fixed set.
* A listing judges entries in sorted name order until MAX_VIEWS live ones are found, the names run out, or
  LIST_BUDGET is spent; whenever names were left unjudged, or the directory could not be read in full, it reports
  ``truncated`` so the cockpit can say the list may be incomplete.
* A forked child (no exec) closes its copy of every fd a publisher holds at once, so it cannot keep a dead view
  listed. Each such fd (the directory, the lock file, the flock self-test's) is opened and recorded, and later
  forgotten and closed, under the same lock the fork handlers take, so no fork copies one this module is not
  tracking.
* Before publishing, a view checks that ``flock`` really conflicts on this filesystem, creating its test file the
  same way it creates an entry. On a filesystem that emulates flock (NFS, some FUSE mounts) or refuses ``O_EXLOCK``
  or ``O_TMPFILE`` (Linux before 3.11, and filesystems without O_TMPFILE support, such as NFS, CIFS and older FUSE
  and overlayfs), the view says why it is not registered and keeps serving.

SCOPE AND TRUST: ``LEVAIN_HOME`` must be on a local filesystem and belong to one OS user; NFS and a registry shared
across users are unsupported. The registry is a convenience index for one user's own processes, NOT an
authentication mechanism: any process of that user can write a fake entry pointing at another loopback port.
What an entry proves is that some process of this user holds the lock and claims the URL, not that the process is
answering (a wedged view is still listed; the browser shows the hang). POSIX only: without ``fcntl`` a view does
not register and the cockpit lists nothing.

The levain home is ``$LEVAIN_HOME`` if set, else ``~/.levain`` (``Path.home()`` follows ``$HOME``, which is how
tests and the demo point it at a temp directory and never the real one).

PRIVACY: an entry holds only the repo path, the URL, the project name and the start time. No ledger content, no
member names, nothing per person. A URL that is not ``http://`` on a loopback IPv4 host is never written or listed.

Stdlib only; imports nothing from the rest of levain.
"""
from __future__ import annotations

import errno
import ipaddress
import json
import os
import re
import secrets
import stat
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

try:
    import fcntl
except ImportError:                                  # not POSIX: nothing here can register or list
    fcntl = None                                     # type: ignore[assignment]

VERSION = 2
PREFIX = "lock1-"
MAX_VIEWS = 8
LIST_BUDGET = 1.0                # seconds, the whole listing; checked before each directory entry and each name
PRUNE_MAX_NAMES = 1024
MAX_ENTRY_BYTES = 4096
_FIELDS = ("repo", "url", "project", "started")
_NAME_RE = re.compile(r"lock1-[0-9a-f]{32}\.json")
# The born-locked temps register() and the self-test make on macOS and the BSDs (Linux makes none). A temp from an
# older levain has no ``x`` and is never swept (see prune_dead).
_TEMP_RE = re.compile(r"\.(?:lock1-x[0-9a-f]{32}|selftest-x[0-9a-f]{16})\.tmp")
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


class RegistryUnavailable(Exception):
    """This view cannot be registered here; the message says why. The view keeps serving."""


def home() -> Path:
    raw = os.environ.get("LEVAIN_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".levain"


def registry_dir() -> Path:
    return home() / "team-views"


def loopback_http_url(url: object) -> str | None:
    """The normalised URL if it is ``http://`` on 127.x or localhost with an explicit port, else None."""
    if not isinstance(url, str) or len(url) > 200:
        return None
    try:
        u = urlsplit(url)
        host, port = u.hostname, u.port
    except ValueError:
        return None
    if u.scheme != "http" or not host or port is None or u.username or u.password:
        return None
    if host != "localhost":
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return None
        if ip.version != 4 or not ip.is_loopback:
            return None
    return f"http://{host}:{port}/"


# ---- the directory ------------------------------------------------------------------------------------------------

def _open_dir(*, create: bool) -> int:
    """An fd on the registry directory, opened without following a symlink and required to be a directory of ours.
    Every later open, rename and unlink is relative to this fd, so the path cannot be swapped underneath us."""
    d = registry_dir()
    if create:
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(d, _DIR_FLAGS)
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
            raise RegistryUnavailable(f"{d} is not a directory owned by this user")
        if create:
            os.fchmod(fd, 0o700)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _how() -> str:
    """How this platform creates a file nobody can see unlocked: "exlock" (macOS, the BSDs) or "tmpfile" (Linux).
    Neither: the registry is unavailable here, never a weaker way."""
    if hasattr(os, "O_EXLOCK"):
        return "exlock"
    if hasattr(os, "O_TMPFILE"):
        return "tmpfile"
    raise RegistryUnavailable("this platform can create a file neither already locked (O_EXLOCK) nor unnamed "
                              "(O_TMPFILE), so the registry is unavailable")


def _create(dir_fd: int, how: str, name: str) -> int:
    """A new file that no other process can find unlocked: ``name``, created holding LOCK_EX, for "exlock"; an unnamed
    file the caller locks before it links a name to it, for "tmpfile". A filesystem that refuses either is
    RegistryUnavailable."""
    try:
        if how == "exlock":
            return os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_EXLOCK | _CLOEXEC, 0o600, dir_fd=dir_fd)
        return os.open(".", os.O_RDWR | os.O_TMPFILE | _CLOEXEC, 0o600, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in (errno.EOPNOTSUPP, errno.ENOTSUP, errno.EISDIR, errno.EINVAL):
            # EISDIR: a kernel older than O_TMPFILE reads its O_DIRECTORY bit and opens the directory
            flag = "O_EXLOCK" if how == "exlock" else "O_TMPFILE"
            raise RegistryUnavailable(f"this filesystem refuses {flag} ({os.strerror(exc.errno)})") from None
        raise


def _reopen(dir_fd: int, how: str, fd: int, name: str) -> int:
    """A second, independent open of the file ``fd`` is on (its own open file description, so its own flock)."""
    if how == "exlock":
        return os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    try:
        return os.open(f"/proc/self/fd/{fd}", os.O_RDONLY | _CLOEXEC)
    except FileNotFoundError:
        raise RegistryUnavailable("/proc is not mounted, so an unnamed file cannot be given a name") from None


def _flock_is_real(dir_fd: int, how: str) -> bool:
    """Does an exclusive lock on one open file make a shared try-lock on another fail here? False on filesystems
    that emulate flock (NFS, some FUSE mounts), where the whole design would silently list nothing. The test file is
    created exactly as an entry is, so a filesystem that refuses that refuses here first."""
    name = f".selftest-x{secrets.token_hex(8)}.tmp"
    fa = _create(dir_fd, how, name)
    fb = None
    try:
        fb = _reopen(dir_fd, how, fa, name)
        if how == "tmpfile":
            fcntl.flock(fa, fcntl.LOCK_EX)
        try:
            fcntl.flock(fb, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        for fd in (fb, fa):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if how == "exlock":
            try:
                os.unlink(name, dir_fd=dir_fd)
            except OSError:
                pass


# ---- the publisher ------------------------------------------------------------------------------------------------

_LIVE: "set[Registration]" = set()      # strong: a dropped, unclosed Registration still has its fds closed at fork
_PENDING: set[int] = set()              # fds register() has opened that no Registration owns yet
# Held by a fork, and by every change to which lock fds exist and are tracked. Deliberately NOT reentrant: with an
# RLock, a fork from a signal handler that interrupted a holder would proceed and copy half-updated fd sets. As it is,
# such a fork deadlocks instead, so a process that registers a view must not fork from a signal handler (levain's own
# SIGTERM handler only raises KeyboardInterrupt, which the ``with`` blocks release on).
_FORK_LOCK = threading.Lock()


def _before_fork() -> None:
    _FORK_LOCK.acquire()


def _after_fork_in_parent() -> None:
    _FORK_LOCK.release()


def _forget_in_child() -> None:
    """After a bare fork the child holds a copy of every lock fd, and the lock lives as long as any copy does. Close
    the child's copies (never unlink: the entry is the parent's) so only the parent's view keeps its entry live. The
    forking thread took _FORK_LOCK, so the sets are exactly the fds that existed; it is this thread's to release."""
    try:
        pending = set(_PENDING)
        _PENDING.clear()
        for reg in list(_LIVE):
            reg._drop_fds_locked()
        for fd in pending:
            try:
                os.close(fd)
            except OSError:
                pass
    finally:
        _FORK_LOCK.release()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(before=_before_fork, after_in_parent=_after_fork_in_parent,
                        after_in_child=_forget_in_child)


class Registration:
    """One published entry and the lock fd that makes it live. Owns the fd for the life of the view: keep this
    object referenced (the view hangs it on its server). There is no finalizer: dropping it without ``close`` leaves
    the fds open (the module keeps a strong reference so a fork still closes the child's copies), so the entry reads
    live until ``close`` or process exit.
    ``unpublish`` removes the entry, ``close`` releases the lock; both are idempotent and neither raises on a
    resource that is already gone."""

    def __init__(self, name: str, dir_fd: int, lock_fd: int):
        self.name = name
        self._dir_fd: int | None = dir_fd
        self._lock_fd: int | None = lock_fd
        _LIVE.add(self)

    @property
    def path(self) -> Path:
        return registry_dir() / self.name

    def unpublish(self) -> None:
        if self._dir_fd is not None:
            try:
                os.unlink(self.name, dir_fd=self._dir_fd)
            except OSError:
                pass

    def close(self) -> None:
        with _FORK_LOCK:     # forget and close as one step: a fork in between would copy an fd nobody tracks
            self._drop_fds_locked()

    def _drop_fds_locked(self) -> None:
        _LIVE.discard(self)
        lock_fd, dir_fd = self._lock_fd, self._dir_fd
        self._lock_fd = self._dir_fd = None
        for fd in (lock_fd, dir_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


def register(repo: str, url: str, project: str) -> Registration:
    """Publish this view: lock first, then make the entry visible. Raises ValueError for a URL that is not
    loopback http, RegistryUnavailable (with the reason) where the registry cannot work, OSError on a failed write."""
    norm = loopback_http_url(url)
    if norm is None:
        raise ValueError(f"not a loopback http URL: {url!r}")
    if fcntl is None:
        raise RegistryUnavailable("this platform has no flock; the registry is POSIX only")
    how = _how()
    with _FORK_LOCK:         # open and record as one step, so no fork can copy the fd untracked
        dir_fd = _open_dir(create=True)
        _PENDING.add(dir_fd)
    lock_fd = None
    published = None
    tmp = f".{PREFIX}x{secrets.token_hex(16)}.tmp"
    try:
        with _FORK_LOCK:     # the self-test opens, locks and closes its own fds: no fork sees them
            real = _flock_is_real(dir_fd, how)
        if not real:
            raise RegistryUnavailable("this filesystem does not support the registry's locks")
        name = f"{PREFIX}{secrets.token_hex(16)}.json"
        entry = {"v": VERSION, "repo": str(repo)[:500], "url": norm, "project": str(project)[:120],
                 "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}   # the lengths _validated shows
        data = json.dumps(entry, sort_keys=True).encode("utf-8")
        if len(data) > MAX_ENTRY_BYTES:      # a reader skips an oversized file, so it must never be published
            raise ValueError(f"registry entry is {len(data)} bytes, over {MAX_ENTRY_BYTES}")
        # A KeyboardInterrupt (levain's SIGTERM handler raises one) between _create returning and the assignment leaves
        # that fd untracked and open; the process is exiting, and its exit closes the fd and releases the lock.
        with _FORK_LOCK:     # open and record as one step, so no fork can copy the fd untracked
            lock_fd = _create(dir_fd, how, tmp)          # "exlock": the temp exists only locked
            _PENDING.add(lock_fd)
        if how == "tmpfile":
            fcntl.flock(lock_fd, fcntl.LOCK_EX)          # unnamed: no other process can reach it yet
        view = memoryview(data)
        while view:
            view = view[os.write(lock_fd, view):]
        if how == "exlock":
            os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        else:
            os.link(f"/proc/self/fd/{lock_fd}", name, dst_dir_fd=dir_fd, follow_symlinks=True)
        published = name
        with _FORK_LOCK:
            reg = Registration(name, dir_fd, lock_fd)
            _PENDING.discard(lock_fd)
            _PENDING.discard(dir_fd)
        return reg
    except BaseException:
        if published is not None:   # interrupted after the rename: withdraw the entry while still holding its lock
            try:
                os.unlink(published, dir_fd=dir_fd)
            except OSError:
                pass
        if lock_fd is not None:
            with _FORK_LOCK:
                _PENDING.discard(lock_fd)
                try:
                    os.close(lock_fd)
                except OSError:
                    pass
        if how == "exlock":
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except OSError:
                pass
        with _FORK_LOCK:
            _PENDING.discard(dir_fd)
            os.close(dir_fd)
        raise


# ---- the pruner (a starting view, before it publishes) --------------------------------------------------------------

class _Unjudged(Exception):
    """An entry the reader could not judge (an open, lock or read failed for a reason that says nothing about the
    entry): the listing is then incomplete, never complete without it."""


def _open_entry(dir_fd: int, name: str, *, strict: bool = False) -> tuple[int, os.stat_result] | None:
    """An fd on a regular file of ours, opened so that a FIFO or a symlink cannot block or redirect the open. None
    for a name that is gone or is a symlink; with ``strict``, any other failed open raises _Unjudged."""
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        if strict and exc.errno not in (errno.ENOENT, errno.ELOOP, errno.EMLINK):   # EMLINK: FreeBSD's O_NOFOLLOW
            raise _Unjudged(name) from exc
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
            os.close(fd)
            return None
    except BaseException:
        os.close(fd)
        raise
    return fd, st


def prune_dead() -> None:
    """Remove entries and born-locked temps no publisher holds. A shared try-lock excludes exactly a publisher's
    exclusive lock, so a live entry or temp is never touched and a concurrent reader still sees the entry as dead.
    Nothing is decided by parsing: a file of another grammar has a different name and is never looked at."""
    if fcntl is None:
        return
    try:
        dir_fd = _open_dir(create=False)
    except (OSError, RegistryUnavailable):
        return
    try:
        # Filter, then bound: junk names must not use up the examination budget.
        names = os.listdir(dir_fd)
        cand = [n for n in names if _NAME_RE.fullmatch(n)][:PRUNE_MAX_NAMES]
        cand += [n for n in names if _TEMP_RE.fullmatch(n)][:PRUNE_MAX_NAMES]
        for name in cand:
            opened = _open_entry(dir_fd, name)
            if opened is None:
                continue
            fd, _st = opened
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                os.unlink(name, dir_fd=dir_fd)
            except OSError:                          # BlockingIOError (alive) or an entry another pruner removed
                pass
            finally:
                os.close(fd)
    finally:
        os.close(dir_fd)


# ---- the reader (the cockpit; read-only) ----------------------------------------------------------------------------

def _validated(raw: object) -> dict | None:
    if not isinstance(raw, dict) or raw.get("v") != VERSION or not all(k in raw for k in _FIELDS):
        return None
    if not all(isinstance(raw[k], str) for k in _FIELDS):
        return None
    url = loopback_http_url(raw["url"])
    if url is None:
        return None
    return {"repo": raw["repo"][:500], "url": url, "project": raw["project"][:120], "started": raw["started"][:40]}


def _read_live(dir_fd: int, name: str) -> dict | None:
    """The entry if a publisher holds its lock, else None. Acquiring the lock means nobody holds it: dead. Raises
    _Unjudged when the open, the lock attempt or the read fails for another reason."""
    opened = _open_entry(dir_fd, name, strict=True)
    if opened is None:
        return None
    fd, st = opened
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return None
        except BlockingIOError:
            pass
        if os.fstat(fd).st_nlink == 0 or st.st_size > MAX_ENTRY_BYTES:
            return None                              # a publisher mid-unpublish, or not a file we wrote
        raw = os.read(fd, MAX_ENTRY_BYTES + 1)
        if len(raw) > MAX_ENTRY_BYTES:
            return None
        return _validated(json.loads(raw.decode("utf-8")))
    except (ValueError, RecursionError):
        return None                                  # not JSON we wrote
    except OSError as exc:
        raise _Unjudged(name) from exc
    finally:
        os.close(fd)


def live_views_scan(budget: float = LIST_BUDGET) -> tuple[list[dict], bool]:
    """(views, truncated): the registered views whose publisher holds its lock now. Read-only, no network.

    The directory is read in full first (the budget is checked before every name, junk included), the entry names
    are sorted, and entries are judged in that order, so which views show never depends on directory order. The scan
    stops at MAX_VIEWS live views, at the end of the names, or when ``budget`` seconds are spent. ``truncated`` is
    True whenever the result may be incomplete: names were left unexamined (the cap or the budget), an entry could not
    be judged (its open, lock or read failed for a reason other than being gone or a symlink), or reading the
    directory failed part way. One ``open`` hung on a dead hard mount cannot be interrupted here; the caller's gate
    bounds how many requests that can trap."""
    if fcntl is None:
        return [], False
    try:
        dir_fd = _open_dir(create=False)
    except FileNotFoundError:
        return [], False                              # no view has ever registered: complete and empty
    except (OSError, RegistryUnavailable):
        return [], True
    end = time.monotonic() + budget
    out: list[dict] = []
    truncated = False
    try:
        names: list[str] = []
        try:
            with os.scandir(dir_fd) as it:
                for ent in it:
                    if time.monotonic() >= end:
                        truncated = True
                        break
                    if _NAME_RE.fullmatch(ent.name):
                        names.append(ent.name)
        except OSError:
            truncated = True
        names = sorted(set(names))                    # a readdir racing a rename may repeat a name
        for i, name in enumerate(names):
            if len(out) >= MAX_VIEWS or time.monotonic() >= end:
                truncated = True                      # names[i:] were never judged
                break
            try:   # one poisoned file must never abort the loop or hide the real views after it
                e = _read_live(dir_fd, name)
            except Exception:  # noqa: BLE001 - _Unjudged or anything else: this name was not judged
                truncated = True
                continue
            if e:
                out.append(e)
    finally:
        os.close(dir_fd)
    return sorted(out, key=lambda e: (e["project"].casefold(), e["url"])), truncated


def live_views(budget: float = LIST_BUDGET) -> list[dict]:
    return live_views_scan(budget)[0]
