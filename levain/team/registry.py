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
* An entry is written under its lock to a hidden temp name and then renamed into place. A rename keeps the inode, so
  a visible entry is always already locked.
* The ``lock1-`` prefix is the liveness-generation marker. A future mechanism must use a different prefix, so an
  old view can never prune a newer view's files and a reader never has to parse a file to decide whether to trust it.
* The cockpit never deletes anything. A starting view prunes entries nobody holds, strictly before it publishes.
  Hidden temp files are never swept: an age rule would be a heuristic, and a publisher paused longer than the age
  (SIGSTOP, a sleeping laptop) would lose its file. A temp file left by a view killed mid-publish is harmless;
  readers ignore it.
* Before publishing, a view checks that ``flock`` really conflicts on this filesystem. On a filesystem that emulates
  it (NFS, some FUSE mounts) the view says why it is not registered and keeps serving.

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

import ipaddress
import json
import os
import re
import secrets
import stat
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
MAX_NAMES = 256                  # entries a listing examines, however many are on disk
LIST_BUDGET = 1.0                # seconds, the whole listing; checked between files
PRUNE_MAX_NAMES = 1024
MAX_ENTRY_BYTES = 4096
_FIELDS = ("repo", "url", "project", "started")
_NAME_RE = re.compile(r"lock1-[0-9a-f]{32}\.json")
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


def _flock_is_real(dir_fd: int) -> bool:
    """Does an exclusive lock on one open file make a shared try-lock on another fail here? False on filesystems
    that emulate flock (NFS, some FUSE mounts), where the whole design would silently list nothing."""
    name = f".selftest-{secrets.token_hex(8)}.tmp"
    fa = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=dir_fd)
    fb = None
    try:
        fb = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
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
        try:
            os.unlink(name, dir_fd=dir_fd)
        except OSError:
            pass


# ---- the publisher ------------------------------------------------------------------------------------------------

class Registration:
    """One published entry and the lock fd that makes it live. Owns the fd for the life of the view: keep this
    object referenced (the view hangs it on its server), because dropping it closes the lock and the entry reads dead.
    ``unpublish`` removes the entry, ``close`` releases the lock; both are idempotent and neither raises on a
    resource that is already gone."""

    def __init__(self, name: str, dir_fd: int, lock_fd: int):
        self.name = name
        self._dir_fd: int | None = dir_fd
        self._lock_fd: int | None = lock_fd

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
    dir_fd = _open_dir(create=True)
    lock_fd = None
    published = None
    tmp = f".{PREFIX}{secrets.token_hex(16)}.tmp"
    try:
        if not _flock_is_real(dir_fd):
            raise RegistryUnavailable("this filesystem does not support the registry's locks")
        name = f"{PREFIX}{secrets.token_hex(16)}.json"
        entry = {"v": VERSION, "repo": str(repo), "url": norm, "project": str(project),
                 "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        data = json.dumps(entry, sort_keys=True).encode("utf-8")
        lock_fd = os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600,
                          dir_fd=dir_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        view = memoryview(data)
        while view:
            view = view[os.write(lock_fd, view):]
        os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        published = name
        return Registration(name, dir_fd, lock_fd)
    except BaseException:
        if published is not None:   # interrupted after the rename: withdraw the entry while still holding its lock
            try:
                os.unlink(published, dir_fd=dir_fd)
            except OSError:
                pass
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                pass
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except OSError:
            pass
        os.close(dir_fd)
        raise


# ---- the pruner (a starting view, before it publishes) --------------------------------------------------------------

def _open_entry(dir_fd: int, name: str) -> tuple[int, os.stat_result] | None:
    """An fd on a regular file of ours, opened so that a FIFO or a symlink cannot block or redirect the open."""
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except OSError:
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
    """Remove entries no publisher holds. A shared try-lock excludes exactly a publisher's exclusive lock, so a live
    entry is never touched and a concurrent reader still sees the entry as dead. Nothing is decided by parsing: a
    file of a newer grammar has a different name and is never looked at."""
    if fcntl is None:
        return
    try:
        dir_fd = _open_dir(create=False)
    except (OSError, RegistryUnavailable):
        return
    try:
        # Filter, then bound: junk names must not use up the examination budget (L2 2026-10-05, RUN).
        for name in [n for n in os.listdir(dir_fd) if _NAME_RE.fullmatch(n)][:PRUNE_MAX_NAMES]:
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
    """The entry if a publisher holds its lock, else None. Acquiring the lock means nobody holds it: dead."""
    opened = _open_entry(dir_fd, name)
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
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)


def live_views(budget: float = LIST_BUDGET) -> list[dict]:
    """The registered views whose publisher holds its lock now. Read-only, no network. At most MAX_NAMES entries are
    examined and the listing stops at ``budget`` seconds between files, so a directory of junk cannot hold the
    cockpit (one ``open`` hung on a dead hard mount cannot be interrupted here; the caller's gate bounds how many
    requests that can trap)."""
    if fcntl is None:
        return []
    try:
        dir_fd = _open_dir(create=False)
    except (OSError, RegistryUnavailable):
        return []
    end = time.monotonic() + budget
    out: list[dict] = []
    try:
        try:
            names = [n for n in os.listdir(dir_fd) if _NAME_RE.fullmatch(n)]
        except OSError:
            return []
        for name in sorted(names)[:MAX_NAMES]:
            if time.monotonic() >= end or len(out) >= MAX_VIEWS:
                break
            try:   # one poisoned file must never abort the loop or hide the real views after it
                e = _read_live(dir_fd, name)
            except Exception:  # noqa: BLE001
                continue
            if e:
                out.append(e)
    finally:
        os.close(dir_fd)
    return sorted(out, key=lambda e: (e["project"].casefold(), e["url"]))
