"""keys' credentials (S1 session 3b, design 1.8): the credential file, its per-exchange observation, the host binding,
the injected header, and the credential-lifecycle seam keys' shutdown calls.

A credential file `L/keys/cred/<name>.json` is OBSERVED on every exchange that needs it: opened `O_NOFOLLOW |
O_NONBLOCK` under the credential directory's fd, refused unless it is a regular file, read to at most FILE_CAP, its
`rev` validated by the parse. Bytes that do not parse, whatever the decode or a check raises on them, refuse the
exchange `credential-unavailable` and evict the cached parse. The cached parse is used only when the file's bytes
equal the bytes it was parsed from, else the file is parsed and the cache replaced, so a change under the same `rev`
is never served from an earlier parse; ENOENT evicts at once, so a removed credential stops working on the next
exchange. `rev` is compared for equality only, never ordered.

The one write discipline (design 1.8) is `commit` / `remove`, used by both writers: keys' `CredWriter` (on its one
writer thread, the `flock` non-blocking and bounded) and the install helper's `set-credential` / `remove-credential`
child (the `flock` waits). `CredWriter` is also the real `CredLifecycle`; its `_refresh_tasks` set stays empty until the
refresh exists. The refresh (kind `oauth2-refresh`) is not built at this tip: an upstream 401 reaches the credential
layer through `on_upstream_401`, a no-op for the static kind.
"""
from __future__ import annotations

import asyncio
import errno
import fcntl
import functools
import json
import logging
import os
import re
import stat
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Protocol, Set, Tuple

from levain.broker.norm import Refused
from levain.broker.redact import unsafe_with_marker
from levain.broker.upstream import _note

FORMAT = "levain-keys-credential/1"
FILE_CAP = 64 * 1024
VALUE_CAP = 8 * 1024
MAX_HOSTS = 8
NAME_RE = re.compile(r"[a-z0-9-]{1,48}")   # every use is a fullmatch: a `$` would admit a trailing newline
REV_RE = re.compile(r"[0-9a-f]{32}")
_LABEL_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
INJECT_HEADERS = ("x-api-key", "authorization")
INJECT_SCHEMES = ("", "Bearer")
_COMMON = {"format", "name", "rev", "hosts", "inject", "kind"}
_KEYS = {"static": _COMMON | {"secret"}, "oauth2-refresh": _COMMON | {"access", "expires_at", "refresh"}}
_REFRESH_KEYS = {"token_url", "client_id", "refresh_token"}


# --- the credential-lifecycle seam -----------------------------------------------------------------------------------

class CredLifecycle(Protocol):
    """What keys' shutdown calls on the credential layer, and nothing else. `begin_closing()` at the shutdown latch (a
    refresh wanted after it is refused by whoever owns the refresh); `cancel_detached()` at shutdown's step (3), which
    cancels the credential layer's detached tasks. The refresh-wanted entry point is NOT on this seam."""

    def begin_closing(self) -> None: ...

    def cancel_detached(self) -> None: ...


class FakeCredLifecycle:
    """The stand-in keys is built with until the real credential writer exists: it implements the seam and holds a
    detached set. `refresh_wanted()` is a test hook of this stand-in only, never part of the seam."""

    def __init__(self) -> None:
        self.closing = False
        self.detached: Set = set()
        self.calls: List[str] = []

    def begin_closing(self) -> None:
        self.calls.append("begin_closing")
        self.closing = True

    def cancel_detached(self) -> None:
        self.calls.append("cancel_detached")
        for t in list(self.detached):
            t.cancel()

    def refresh_wanted(self) -> bool:
        """TEST HOOK: records the want; refused (False) once closing."""
        self.calls.append("refresh_wanted")
        return not self.closing


# --- the credential --------------------------------------------------------------------------------------------------

def is_a_label(host: object) -> bool:
    """A lowercase IDNA A-label host name (ASCII letters, digits and hyphens, dot-separated)."""
    if not isinstance(host, str) or not host or len(host) > 253 or host != host.lower():
        return False
    return all(_LABEL_RE.fullmatch(lab) for lab in host.split("."))


class Credential:
    __slots__ = ("name", "rev", "hosts", "header", "scheme", "kind", "secret")

    def __init__(self, name: str, rev: str, hosts: frozenset, header: str, scheme: str, kind: str,
                 secret: str) -> None:
        self.name, self.rev, self.hosts = name, rev, hosts
        self.header, self.scheme, self.kind, self.secret = header, scheme, kind, secret

    @property
    def generation(self) -> str:
        return self.rev

    def __repr__(self) -> str:   # never a byte of the secret
        return f"Credential(name={self.name!r}, rev={self.rev!r}, kind={self.kind!r})"

    def header_pair(self) -> tuple:
        """The ONE injected header: name and scheme from the file."""
        value = f"{self.scheme} {self.secret}" if self.scheme else self.secret
        return (self.header.encode("ascii"), value.encode("latin-1"))


class Unparseable(ValueError):
    pass


def _value(v: object, what: str) -> str:
    if not isinstance(v, str) or not v or len(v.encode("utf-8")) > VALUE_CAP:
        raise Unparseable(what)
    return v


#: what a file's bytes can raise out of the decode and the checks besides `Unparseable`: nesting past the recursion
#: limit, a value of the wrong type meeting an operation (an unhashable `kind`), a lone surrogate meeting an encode.
_INPUT_ERRORS = (ValueError, RecursionError, TypeError, UnicodeError)


def parse(raw: bytes, expect_name: str) -> Credential:
    """Validate one credential file's bytes; raise `Unparseable` naming the field (never echoing a value). This is the
    parse boundary: anything the bytes can make the decode or a check raise leaves as `Unparseable`."""
    try:
        return _parse(raw, expect_name)
    except Unparseable:
        raise
    except _INPUT_ERRORS as e:
        raise Unparseable(type(e).__name__) from None


def _parse(raw: bytes, expect_name: str) -> Credential:
    try:
        d = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_dup)
    except (UnicodeDecodeError, ValueError) as e:
        raise Unparseable("json") from e
    if not isinstance(d, dict) or d.get("format") != FORMAT:
        raise Unparseable("format")
    kind = d.get("kind")
    if not isinstance(kind, str) or kind not in _KEYS:
        raise Unparseable("kind")
    if set(d) != _KEYS[kind]:
        raise Unparseable("keys")
    if d["name"] != expect_name or not isinstance(d["name"], str) or not NAME_RE.fullmatch(d["name"]):
        raise Unparseable("name")
    if not isinstance(d["rev"], str) or not REV_RE.fullmatch(d["rev"]):
        raise Unparseable("rev")
    hosts = d["hosts"]
    if (not isinstance(hosts, list) or not 1 <= len(hosts) <= MAX_HOSTS or not all(is_a_label(h) for h in hosts)
            or len(set(hosts)) != len(hosts)):
        raise Unparseable("hosts")
    inj = d["inject"]
    if (not isinstance(inj, dict) or set(inj) != {"header", "scheme"} or inj["header"] not in INJECT_HEADERS
            or inj["scheme"] not in INJECT_SCHEMES):
        raise Unparseable("inject")
    if kind == "static":
        secret = _value(d["secret"], "secret")
    else:
        secret = _value(d["access"], "access")
        if not isinstance(d["expires_at"], int) or isinstance(d["expires_at"], bool):
            raise Unparseable("expires_at")
        r = d["refresh"]
        if not isinstance(r, dict) or set(r) != _REFRESH_KEYS:
            raise Unparseable("refresh")
        for k in _REFRESH_KEYS:
            _value(r[k], "refresh." + k)
        url = r["token_url"]
        if not url.startswith("https://"):
            raise Unparseable("refresh.token_url")
        host = url[len("https://"):].split("/", 1)[0]
        if host not in hosts:
            raise Unparseable("refresh.token_url host")
    if not all(0x20 <= ord(c) < 0x7f for c in secret):   # printable ASCII: the value goes into a header
        raise Unparseable("secret charset")
    # the scanner's secret set (design 1.7): a member the marker could hold or rebuild would survive the redaction
    for v in [secret] + ([d["refresh"]["refresh_token"]] if kind == "oauth2-refresh" else []):
        if unsafe_with_marker(v.encode("utf-8")):
            raise Unparseable("secret marker")
    return Credential(d["name"], d["rev"], frozenset(hosts), inj["header"], inj["scheme"], kind, secret)


def _no_dup(pairs: list) -> dict:
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key")
        out[k] = v
    return out


def read_file(dir_fd: int, name: str) -> Optional[bytes]:
    """`<name>.json` below `dir_fd`, at most FILE_CAP bytes, or None when absent; `Unparseable` for a file that is not
    a regular one or is over the cap, `OSError` for any other failure. The one read: the per-exchange observation and
    the writers' `rev` compare both use it."""
    # O_NONBLOCK: opening a FIFO with no writer would otherwise block keys' one loop; the fstat then refuses
    # anything but a regular file before a byte is read (O_NONBLOCK changes nothing for a regular file's read).
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(name + ".json", flags, dir_fd=dir_fd)
    except OSError as e:
        if e.errno == errno.ENOENT:
            return None
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Unparseable("not a regular file")
        chunks, got = [], 0
        while got <= FILE_CAP:
            b = os.read(fd, FILE_CAP + 1 - got)
            if not b:
                break
            chunks.append(b)
            got += len(b)
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    if len(raw) > FILE_CAP:
        raise Unparseable("size")
    return raw


class CredStore:
    """Credentials observed under one directory fd. `observe(name)` is called once per exchange that needs `name`."""

    def __init__(self, dir_fd: int) -> None:
        self.dir_fd = dir_fd
        #: name -> (the bytes last parsed, their parse)
        self.cache: Dict[str, Tuple[bytes, Credential]] = {}

    def _read(self, name: str) -> Optional[bytes]:
        return read_file(self.dir_fd, name)

    def observe(self, name: str) -> Credential:
        """The credential `name` as the file holds it NOW, or `Refused("credential-unavailable")`."""
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise Refused("credential-unavailable")
        try:
            raw = self._read(name)
        except (OSError, Unparseable) as e:
            self.cache.pop(name, None)
            _note(logging.WARNING, "levain gw-keys: credential %s unreadable (%s)", name, type(e).__name__)
            raise Refused("credential-unavailable") from None
        if raw is None:
            self.cache.pop(name, None)   # ENOENT evicts at once
            raise Refused("credential-unavailable")
        cur = self.cache.get(name)
        if cur is not None and cur[0] == raw:
            return cur[1]   # the very bytes it parsed: a change under the same `rev` is parsed again
        try:
            cred = parse(raw, name)
        except Unparseable as e:
            self.cache.pop(name, None)
            _note(logging.WARNING, "levain gw-keys: credential %s unparseable (%s)", name, e)
            raise Refused("credential-unavailable") from None
        self.cache[name] = (raw, cred)
        return cred


def check_host(cred: Credential, host: str, route: str) -> None:
    """The host binding, every exchange, after the credential is loaded and before the upstream opens."""
    if host not in cred.hosts:
        _note(logging.WARNING, "levain gw-keys: credential-host-mismatch route=%s credential=%s host=%s",
              route, cred.name, host)
        raise Refused("credential-host-mismatch")


def on_upstream_401(credential: Credential, generation: str) -> None:
    """An upstream 401 on an exchange that sent `credential` at `generation`. The static kind relays the 401 as is,
    so this is a no-op for it; the refresh kind's handling is not built at this tip."""
    return None


# --- the one write discipline (design 1.8) ---------------------------------------------------------------------------

LOCK = ".lock"
LOCK_TIMEOUT_S = 10.0      # keys' flock: a stopped holder fails the commit after this (DECISION, design 1.8 / R8)
LOCK_RETRY_S = 0.05
STALE_TMP_S = 60.0         # whoever holds the lock removes temps older than this
#: exactly `_tmp_name`'s namespace: the name, a canonical positive pid, 8 lowercase hex, and nothing else
_TMP_RE = re.compile(r"\.(?:" + NAME_RE.pattern + r")\.[1-9][0-9]*\.[0-9a-f]{8}\.tmp")
_O_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
#: the fds a commit or remove holds beyond keys' counted fixed ones, at its peak: the directory (when keys started
#: before `cred/` existed, the first commit opens it), the lock, and ONE more at a time (the sweep's listing, which
#: dups the dirfd; the compare's read; the temp). keys' NOFILE transient term is built from it.
COMMIT_FDS = 3
COMMITTED, DISCARDED = "committed", "discarded"
REMOVED, ABSENT = "removed", "absent"


def _tmp_name(name: str) -> str:
    return f".{name}.{os.getpid()}.{os.urandom(4).hex()}.tmp"


class CommitFailed(Exception):
    """Nothing was committed and the old file is intact: `credential-lock-timeout` (the flock was not had within the
    bound) or `credential-commit-failed` (an OS failure at the lock, the read, the temp or the rename)."""

    def __init__(self, name: str, detail: str = "") -> None:
        super().__init__(f"{name}: {detail}" if detail else name)
        self.name = name


class Outcome:
    """`COMMITTED` with the new `rev` (`durable` False when the directory fsync failed after the rename: the new file
    is in place, and the caller uses it), or `DISCARDED` (the `rev` compare failed: the file changed or is gone).
    A remove's: `REMOVED` or `ABSENT` (no file was there), each with `durable` False when the directory fsync failed
    (a crash may bring the file back)."""
    __slots__ = ("result", "rev", "durable")

    def __init__(self, result: str, rev: Optional[str] = None, durable: bool = True) -> None:
        self.result, self.rev, self.durable = result, rev, durable

    def __repr__(self) -> str:
        return f"Outcome({self.result}, rev={self.rev!r}, durable={self.durable})"


def full_fsync(fd: int) -> None:
    """`F_FULLFSYNC` on macOS (a plain fsync there does not reach the medium), and `os.fsync` if that raises (some
    filesystems refuse it: SMB, NFS, some FUSE mounts); `os.fsync` elsewhere. One rule for a file and a directory."""
    if sys.platform == "darwin":
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except OSError:
            pass
    os.fsync(fd)


def dir_fsync(fd: int) -> None:
    """The directory's fsync, by `full_fsync`'s rule."""
    full_fsync(fd)


def _renameat(src: str, dst: str, dir_fd: int) -> None:
    os.rename(src, dst, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


class WriteSeams:
    """The commit's OS acts a test injects a failure into, each with its real default."""

    def __init__(self, *, fsync_file: Callable[[int], None] = full_fsync, fsync_dir: Callable[[int], None] = dir_fsync,
                 rename: Callable[[str, str, int], None] = _renameat,
                 monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time) -> None:
        self.fsync_file, self.fsync_dir, self.rename = fsync_file, fsync_dir, rename
        self.monotonic, self.sleep, self.clock = monotonic, sleep, clock


def render(fields: dict, name: str) -> Tuple[bytes, str]:
    """A credential object WITHOUT `rev` -> the file's bytes with a FRESH random `rev` (16 bytes of `os.urandom`, hex),
    and that `rev`. The bytes are parsed by `parse` before they are returned, so a writer only ever writes what the
    reader accepts; `Unparseable` names the field otherwise."""
    if not isinstance(fields, dict) or "rev" in fields:
        raise Unparseable("rev")
    rev = os.urandom(16).hex()
    d = dict(fields)
    d["rev"] = rev
    try:
        raw = json.dumps(d, sort_keys=True, separators=(",", ":")).encode("ascii")
    except (TypeError, ValueError, RecursionError) as e:
        raise Unparseable(type(e).__name__) from None
    if len(raw) > FILE_CAP:
        raise Unparseable("size")
    parse(raw, name)
    return raw, rev


def _open_lock(dir_fd: int) -> int:
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0), 0o600,
                 dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "the lock is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _take_lock(fd: int, wait: bool, timeout_s: float, seams: WriteSeams) -> bool:
    """`flock(LOCK_EX)`: waiting (the install helper: an operator verb may wait), or `LOCK_NB` retried every
    LOCK_RETRY_S for at most `timeout_s` (keys' writer thread: a stopped holder never holds it forever). After the
    first attempt no attempt is made at or past the deadline (a thread descheduled in its sleep must not take the lock
    late), and no sleep runs past it."""
    if wait:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return True
    end = seams.monotonic() + timeout_s
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as e:
            if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise
        remaining = end - seams.monotonic()
        if remaining <= 0:
            return False
        seams.sleep(min(LOCK_RETRY_S, remaining))
        if seams.monotonic() >= end:
            return False


def _sweep(dir_fd: int, now: float) -> None:
    """Under the lock: remove `_tmp_name`-shaped files older than STALE_TMP_S (a killed writer's). Best effort: a
    directory that cannot be listed skips the sweep with a warning and never fails the commit or remove (whatever
    is wrong with it, the commit's own steps fail on their own)."""
    try:
        entries = os.listdir(dir_fd)
    except OSError as e:
        _note(logging.WARNING, "levain gw-keys: credential-temp-sweep-skipped (listdir: %s)", type(e).__name__)
        return
    for entry in entries:
        if not _TMP_RE.fullmatch(entry):
            continue
        try:
            st = os.lstat(entry, dir_fd=dir_fd)
            if stat.S_ISREG(st.st_mode) and now - st.st_mtime > STALE_TMP_S:
                os.unlink(entry, dir_fd=dir_fd)
        except OSError:
            pass


def _unlink_quiet(path: str, dir_fd: int) -> None:
    try:
        os.unlink(path, dir_fd=dir_fd)
    except OSError:
        pass


def _locked(dir_fd: int, wait: bool, timeout_s: float, seams: WriteSeams) -> int:
    """Open the lock and take it (step 1), or raise `CommitFailed`; returns the held lock's fd."""
    try:
        lock_fd = _open_lock(dir_fd)
    except OSError as e:
        _note(logging.WARNING, "levain gw-keys: credential-commit-failed (lock: %s)", type(e).__name__)
        raise CommitFailed("credential-commit-failed", "lock") from None
    try:
        had = _take_lock(lock_fd, wait, timeout_s, seams)
    except OSError as e:
        os.close(lock_fd)
        _note(logging.WARNING, "levain gw-keys: credential-commit-failed (flock: %s)", type(e).__name__)
        raise CommitFailed("credential-commit-failed", "flock") from None
    if not had:
        os.close(lock_fd)
        _note(logging.WARNING, "levain gw-keys: credential-lock-timeout after %.1f s", timeout_s)
        raise CommitFailed("credential-lock-timeout")
    return lock_fd


def _unlock(lock_fd: int) -> None:
    """Release and close. Never raises: closing the fd releases the flock, so a failed `LOCK_UN` (or close) is a
    warning and never replaces the commit's outcome or the failure in flight."""
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except OSError as e:
        _note(logging.WARNING, "levain gw-keys: credential unlock failed (%s); the close releases it", type(e).__name__)
    try:
        os.close(lock_fd)
    except OSError as e:
        _note(logging.WARNING, "levain gw-keys: credential lock close failed (%s)", type(e).__name__)


def _durable(dir_fd: int, seams: WriteSeams, name: str, act: str = "commit") -> bool:
    try:
        seams.fsync_dir(dir_fd)
        return True
    except OSError as e:
        _note(logging.WARNING, "levain gw-keys: credential-%s-not-durable credential=%s (%s)", act, name,
              type(e).__name__)
        return False


def commit(dir_fd: int, name: str, fields: dict, *, expect_rev: Optional[str] = None, wait: bool = False,
           lock_timeout_s: float = LOCK_TIMEOUT_S, seams: Optional[WriteSeams] = None) -> Outcome:
    """The commit, step by step, synchronous (keys runs it on its writer thread, the install helper's child inline):
    (1) the `flock` (a timeout = `CommitFailed`, nothing written); the stale-temp sweep; (2) when `expect_rev` is
    given, read the file's `rev` and compare: a different `rev`, an unparseable file or no file = DISCARDED (the
    operator's act wins; a removed credential stays removed); (3) a unique temp (`O_CREAT|O_EXCL|O_NOFOLLOW`, 0600),
    written and fsynced, a failure unlinks it = `CommitFailed`; (4) the rename over `<name>.json`, a failure unlinks
    the temp = `CommitFailed`; (5) the directory fsync, a failure = COMMITTED with `durable` False and the warning
    `credential-commit-not-durable`; (6) unlock. `expect_rev` None is the operator's write: no compare.
    `Unparseable` (from `render`, before the lock) means `fields` is not a credential: nothing is touched."""
    s = seams or WriteSeams()
    raw, rev = render(fields, name)
    lock_fd = _locked(dir_fd, wait, lock_timeout_s, s)
    try:
        _sweep(dir_fd, s.clock())
        if expect_rev is not None:
            try:
                cur = read_file(dir_fd, name)
                cur_rev = parse(cur, name).rev if cur is not None else None
            except Unparseable:
                cur_rev = None
            except OSError as e:
                _note(logging.WARNING, "levain gw-keys: credential-commit-failed credential=%s (read: %s)", name,
                      type(e).__name__)
                raise CommitFailed("credential-commit-failed", "read") from None
            if cur_rev != expect_rev:
                _note(logging.INFO, "levain gw-keys: credential %s changed or removed meanwhile; result discarded", name)
                return Outcome(DISCARDED)
        tmp = _tmp_name(name)
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                         0o600, dir_fd=dir_fd)
        except OSError as e:
            _note(logging.WARNING, "levain gw-keys: credential-commit-failed credential=%s (temp: %s)", name,
                  type(e).__name__)
            raise CommitFailed("credential-commit-failed", "temp") from None
        try:
            try:
                view = memoryview(raw)
                while view:
                    view = view[os.write(fd, view):]
                s.fsync_file(fd)
            finally:
                os.close(fd)
            s.rename(tmp, name + ".json", dir_fd)
        except OSError as e:
            _unlink_quiet(tmp, dir_fd)
            _note(logging.WARNING, "levain gw-keys: credential-commit-failed credential=%s (%s)", name,
                  type(e).__name__)
            raise CommitFailed("credential-commit-failed", "write") from None
        return Outcome(COMMITTED, rev, _durable(dir_fd, s, name))
    finally:
        _unlock(lock_fd)


def remove(dir_fd: int, name: str, *, wait: bool = True, lock_timeout_s: float = LOCK_TIMEOUT_S,
           seams: Optional[WriteSeams] = None) -> Outcome:
    """`remove-credential`'s act: under the same lock, unlink `<name>.json` and fsync the directory. `REMOVED` when a
    file was removed, `ABSENT` when there was none; the directory is fsynced either way (a retry after a remove whose
    fsync failed makes that unlink durable), and `durable` False is the warning `credential-remove-not-durable`, as
    commit's. A refresh begun before it then finds the file gone and discards."""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise Unparseable("name")
    s = seams or WriteSeams()
    lock_fd = _locked(dir_fd, wait, lock_timeout_s, s)
    try:
        _sweep(dir_fd, s.clock())
        try:
            os.unlink(name + ".json", dir_fd=dir_fd)
            result = REMOVED
        except FileNotFoundError:
            result = ABSENT
        except OSError as e:
            _note(logging.WARNING, "levain gw-keys: credential-commit-failed credential=%s (unlink: %s)", name,
                  type(e).__name__)
            raise CommitFailed("credential-commit-failed", f"unlink: {type(e).__name__}") from None
        return Outcome(result, durable=_durable(dir_fd, s, name, "remove"))
    finally:
        _unlock(lock_fd)


class CredWriter:
    """keys' ONE credential writer (design 1.8, r2 W4) and the real `CredLifecycle`. Every commit runs on one dedicated
    writer thread (`ThreadPoolExecutor(max_workers=1)`, made here at keys' start; its thread starts at the first
    commit), so keys' own commits serialize and nothing on the event loop blocks on the `flock` or an fsync; the loop
    awaits the job's future, and a job runs to its end whatever happens to its awaiter. The credential directory is
    opened (`O_DIRECTORY|O_NOFOLLOW`) by `open_dir()` at keys' start, before keys counts its fixed fds, or, when it
    did not exist then, on the writer thread at the first commit (COMMIT_FDS budgets that case); it is kept.

    The seam's shell (R86): `begin_closing()` sets `_closing`; `cancel_detached()` cancels the members of
    `_refresh_tasks`. The single-flight that fills the set and reads the flag is not built at this tip, so the set
    stays empty."""

    def __init__(self, cred_dir: str, *, seams: Optional[WriteSeams] = None) -> None:
        self.cred_dir = cred_dir
        self.seams = seams or WriteSeams()
        self.lock_timeout_s = LOCK_TIMEOUT_S
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="levain-cred-writer")
        self._dir_fd: Optional[int] = None    # set by open_dir() before any job, else by the writer thread only
        self._closing = False
        self._refresh_tasks: Set["asyncio.Task"] = set()

    # -- CredLifecycle --

    def begin_closing(self) -> None:
        self._closing = True

    def cancel_detached(self) -> None:
        for t in list(self._refresh_tasks):
            t.cancel()

    def open_dir(self) -> bool:
        """At keys' start, before any commit is submitted and before `_serve` counts the fixed fds: open the credential
        directory now so its fd is one of the counted ones. False (nothing opened, no failure) when it cannot be
        opened: `cred/` is made by the first `set-credential`, so it may not exist yet, and the first commit retries."""
        if self._dir_fd is None:
            try:
                self._dir_fd = os.open(self.cred_dir, _O_DIR)
            except OSError as e:
                if e.errno != errno.ENOENT:
                    _note(logging.WARNING, "levain gw-keys: the credential directory is not open at start (%s)",
                          type(e).__name__)
                return False
        return True

    # -- the writer thread --

    async def commit(self, name: str, fields: dict, expect_rev: Optional[str] = None) -> Outcome:
        """`commit` on the writer thread, with keys' bounded `flock`."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor,
                                          functools.partial(self._commit_sync, name, fields, expect_rev))

    def _commit_sync(self, name: str, fields: dict, expect_rev: Optional[str]) -> Outcome:
        if self._dir_fd is None:
            try:
                self._dir_fd = os.open(self.cred_dir, _O_DIR)
            except OSError as e:
                _note(logging.WARNING, "levain gw-keys: credential-commit-failed (the credential directory: %s)",
                      type(e).__name__)
                raise CommitFailed("credential-commit-failed", "directory") from None
        return commit(self._dir_fd, name, fields, expect_rev=expect_rev, wait=False,
                      lock_timeout_s=self.lock_timeout_s, seams=self.seams)

    def close(self) -> None:
        """At keys' exit, after the loop: wait for a job in flight (bounded by the lock timeout and the fsyncs), then
        close the directory fd."""
        self._executor.shutdown(wait=True)
        if self._dir_fd is not None:
            os.close(self._dir_fd)
            self._dir_fd = None
