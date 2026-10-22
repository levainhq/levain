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
child (the `flock` waits). `CredWriter` is also the real `CredLifecycle`.

The refresh (kind `oauth2-refresh`, design 1.8) is `CredWriter`'s: it is wanted by `fresh` (the access token expires
within REFRESH_AHEAD_S, before the exchange goes upstream) or by `on_upstream_401` (on the generation the 401'd
exchange used, if it is still the file's). `CredWriter.want` passes the one gate keyed by `(name, rev)` and the
single-flight table, and runs the refresh as a DETACHED task (`_refresh`, the one `upstream.refresh_post` caller) that
nothing an exchange does can cancel; every waiter awaits `asyncio.shield` of its future.
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
from urllib.parse import urlencode

from levain.broker import upstream
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
    __slots__ = ("name", "rev", "hosts", "header", "scheme", "kind", "secret", "expires_at", "fields")

    def __init__(self, name: str, rev: str, hosts: frozenset, header: str, scheme: str, kind: str,
                 secret: str, expires_at: Optional[int] = None, fields: Optional[dict] = None) -> None:
        self.name, self.rev, self.hosts = name, rev, hosts
        self.header, self.scheme, self.kind, self.secret = header, scheme, kind, secret
        #: the refresh kind only: the access token's expiry (epoch s), and the file's object without `rev`, which the
        #: refresh rewrites with the new tokens
        self.expires_at, self.fields = expires_at, fields

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
    if kind == "static":
        return Credential(d["name"], d["rev"], frozenset(hosts), inj["header"], inj["scheme"], kind, secret)
    fields = {k: v for k, v in d.items() if k != "rev"}
    return Credential(d["name"], d["rev"], frozenset(hosts), inj["header"], inj["scheme"], kind, secret,
                      d["expires_at"], fields)


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


def _now(lifecycle: object) -> float:
    return lifecycle.seams.clock() if isinstance(lifecycle, CredWriter) else time.time()


async def fresh(lifecycle: object, store: CredStore, name: str) -> Credential:
    """Before an exchange goes upstream: the credential `name`, observed. A refresh-kind credential whose access token
    expires within REFRESH_AHEAD_S wants a refresh (proactive); when `lifecycle` is the real writer and the want
    starts or joins one, the exchange waits for it SHIELDED (cancelling the exchange never cancels the refresh), then
    observes the file again. An access token that has expired by then is `credential-unavailable`, never sent; a
    still-valid one is used while the gate is shut or the refresh failed. Any other `lifecycle` (the in-process
    stand-in) refreshes nothing."""
    cred = store.observe(name)
    if cred.kind != "oauth2-refresh" or cred.expires_at - _now(lifecycle) >= REFRESH_AHEAD_S:
        return cred
    if isinstance(lifecycle, CredWriter):
        fut = lifecycle.want(cred)
        if fut is not None:
            await asyncio.shield(fut)
            cred = store.observe(name)
    unexpired(lifecycle, cred)
    return cred


def unexpired(lifecycle: object, cred: Credential) -> None:
    """`credential-unavailable` when `cred` is a refresh-kind credential whose access token has expired by
    `lifecycle`'s clock. Synchronous: keys calls it again immediately before the request head's write, with no await
    between the two, so a token that expired during the resolve, connect or TLS is never sent."""
    if cred.kind == "oauth2-refresh" and cred.expires_at <= _now(lifecycle):
        raise Refused("credential-unavailable", "expired")


def on_upstream_401(lifecycle: object, store: Optional[CredStore], credential: Credential, generation: str) -> None:
    """An upstream 401 on an exchange that sent `credential` at `generation` (that exchange answers 503 and is never
    replayed, keys' side). The static kind relays the 401 as is: nothing here. For the refresh kind the want runs
    only if `generation` is still the file's (a newer one means someone already refreshed); it never waits."""
    if credential.kind != "oauth2-refresh" or store is None or not isinstance(lifecycle, CredWriter):
        return None
    try:
        cur = store.observe(credential.name)
    except Refused:
        return None
    if cur.rev == generation:
        lifecycle.want(cur)
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


def new_rev() -> str:
    """A fresh random `rev`: 16 bytes of `os.urandom`, hex."""
    return os.urandom(16).hex()


def render(fields: dict, name: str, rev: Optional[str] = None) -> Tuple[bytes, str]:
    """A credential object WITHOUT `rev` -> the file's bytes with `rev` (a fresh `new_rev()` when None: a caller
    passes one only when it must know the `rev` before the write lands), and that `rev`. The bytes are parsed by
    `parse` before they are returned, so a writer only ever writes what the reader accepts; `Unparseable` names the
    field otherwise."""
    if not isinstance(fields, dict) or "rev" in fields:
        raise Unparseable("rev")
    if rev is None:
        rev = new_rev()
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
           lock_timeout_s: float = LOCK_TIMEOUT_S, seams: Optional[WriteSeams] = None,
           rev: Optional[str] = None) -> Outcome:
    """The commit, step by step, synchronous (keys runs it on its writer thread, the install helper's child inline):
    (1) the `flock` (a timeout = `CommitFailed`, nothing written); the stale-temp sweep; (2) when `expect_rev` is
    given, read the file's `rev` and compare: a different `rev`, an unparseable file or no file = DISCARDED (the
    operator's act wins; a removed credential stays removed); (3) a unique temp (`O_CREAT|O_EXCL|O_NOFOLLOW`, 0600),
    written and fsynced, a failure unlinks it = `CommitFailed`; (4) the rename over `<name>.json`, a failure unlinks
    the temp = `CommitFailed`; (5) the directory fsync, a failure = COMMITTED with `durable` False and the warning
    `credential-commit-not-durable`; (6) unlock. `expect_rev` None is the operator's write: no compare.
    `Unparseable` (from `render`, before the lock) means `fields` is not a credential: nothing is touched. `rev` is
    `render`'s: the new file's `rev` when given (the refresh's, made before the commit is submitted)."""
    s = seams or WriteSeams()
    raw, rev = render(fields, name, rev)
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


# --- the refresh (design 1.8, kind oauth2-refresh) -------------------------------------------------------------------

REFRESH_AHEAD_S = 60.0     # a refresh is wanted when the access token expires within this (proactive)
GATE_MIN_S = 30.0          # at most one attempt per this, from the start of the last attempt (DECISION, r2 W8)
GATE_MAX_S = 900.0         # the failure backoff, 30 s x 2^(failures - 1), is at most this (15 min)
TOKEN_PORT = 443           # `token_url` is `https://<host>/...` with `<host>` an A-label of `hosts`: no port in it
#: refresh connections open at once (the refresh semaphore, design 1.6's NOFILE terms; keys' `refresh` term is this)
REFRESH_CONNS = 8
#: the request target the POST carries: printable ASCII, no space and no fragment
_TARGET_RE = re.compile(r"/[\x21-\x22\x24-\x7e]*")
#: a token endpoint's `expires_in` past this is taken as this (one year), so a huge value cannot stop the proactive
#: refresh for good
EXPIRES_IN_MAX_S = 365 * 86400
_DIGITS_RE = re.compile(r"[0-9]{1,20}")


class RefreshRejected(Exception):
    """The token endpoint's answer is not a refresh keys commits; `detail` is a fixed token (the response body is never
    logged): `status`, `json`, `access_token`, `expires_in`, `refresh_token`, or `token-url` (refused before any byte)."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class _Gate:
    """The one per-credential gate, for the `rev` it holds (a newly observed `rev` gets a new, open gate; the `rev`
    a refresh commits keeps its gate, so one refresh's own success never opens the next attempt early): an attempt is
    admitted at most once per GATE_MIN_S from the last attempt's start, and not while a failure's backoff runs.
    `own` is the `rev` this gate's latest refresh commits, set BEFORE that commit is submitted: the new file can be
    observed before the refresh resumes to call `succeeded`, and that observation must find this gate, not open one."""
    __slots__ = ("rev", "own", "last_start", "failures", "shut_until")

    def __init__(self, rev: str) -> None:
        self.rev = rev
        self.own: Optional[str] = None
        self.last_start: Optional[float] = None
        self.failures = 0
        self.shut_until = float("-inf")

    def admits(self, now: float) -> bool:
        return (self.last_start is None or now - self.last_start >= GATE_MIN_S) and now >= self.shut_until

    def failed(self, now: float) -> None:
        self.failures += 1
        self.shut_until = now + min(GATE_MIN_S * 2 ** min(self.failures - 1, 16), GATE_MAX_S)

    def succeeded(self, rev: str) -> None:
        self.rev, self.failures, self.shut_until = rev, 0, float("-inf")


async def _post(cred: Credential) -> "upstream.RefreshResponse":
    """RFC 6749 §6 for `cred`: a form-encoded `grant_type=refresh_token` POST to its `token_url`, through
    `upstream.refresh_post` (the address checks, TLS, the host as SNI, no redirect, the body cap). The one caller."""
    r = cred.fields["refresh"]
    host, _, path = r["token_url"][len("https://"):].partition("/")
    target = "/" + path
    if not _TARGET_RE.fullmatch(target):
        raise RefreshRejected("token-url")
    addr = await upstream.pick_address(host, TOKEN_PORT)
    body = urlencode({"grant_type": "refresh_token", "refresh_token": r["refresh_token"],
                      "client_id": r["client_id"]}).encode("ascii")
    head = (f"POST {target} HTTP/1.1\r\nHost: {host}\r\nContent-Type: application/x-www-form-urlencoded\r\n"
            f"Content-Length: {len(body)}\r\nAccept: application/json\r\nAccept-Encoding: identity\r\n"
            f"Connection: close\r\n\r\n").encode("ascii")
    return await upstream.refresh_post(upstream.RefreshRequest(addr, TOKEN_PORT, host, head, body))


def _refreshed(cred: Credential, resp: "upstream.RefreshResponse", now: float) -> dict:
    """The credential's object with the endpoint's new tokens, or `RefreshRejected`: a status other than 200, a body
    that is not a JSON object, an `access_token` or `refresh_token` that is not a string of at most VALUE_CAP bytes, an
    `expires_in` that is not a positive whole number (`_expires_in`; absent is a failure: the expiry is what the next
    want reads). A returned `refresh_token` replaces the old one (rotation); none keeps it. `render` parses the result
    before it is written."""
    if resp.status != 200:
        raise RefreshRejected("status")
    try:
        d = json.loads(resp.body.decode("utf-8"), object_pairs_hook=_no_dup)
    except (ValueError, RecursionError):
        raise RefreshRejected("json") from None
    if not isinstance(d, dict):
        raise RefreshRejected("json")
    try:
        access = _value(d.get("access_token"), "access_token")
        rotated = d.get("refresh_token")
        if rotated is not None:
            rotated = _value(rotated, "refresh_token")
    except Unparseable as e:
        raise RefreshRejected(str(e)) from None
    exp = _expires_in(d.get("expires_in"))
    fields = json.loads(json.dumps(cred.fields))
    fields["access"] = access
    fields["expires_at"] = int(now) + exp
    if rotated is not None:
        fields["refresh"]["refresh_token"] = rotated
    return fields


def _expires_in(v: object) -> int:
    """`expires_in` as seconds, at most EXPIRES_IN_MAX_S: a JSON integer, a string of decimal digits (some endpoints
    send `"3600"`), or a float with no fraction (`3600.0`); positive. Anything else (a bool, a fraction, a sign or a
    space in the string, inf or nan) is `RefreshRejected("expires_in")`."""
    if isinstance(v, str) and _DIGITS_RE.fullmatch(v):
        v = int(v)
    elif isinstance(v, float) and v.is_integer():   # False for inf and nan
        v = int(v)
    if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
        raise RefreshRejected("expires_in")
    return min(v, EXPIRES_IN_MAX_S)


def _why(e: BaseException) -> str:
    """A refresh failure's name for the log: a fixed token or a type name, never text from the response."""
    if isinstance(e, RefreshRejected):
        return e.detail
    if isinstance(e, (CommitFailed, Refused)):
        return e.name
    return type(e).__name__


class CredWriter:
    """keys' ONE credential writer (design 1.8, r2 W4) and the real `CredLifecycle`. Every commit runs on one dedicated
    writer thread (`ThreadPoolExecutor(max_workers=1)`, made here at keys' start; its thread starts at the first
    commit), so keys' own commits serialize and nothing on the event loop blocks on the `flock` or an fsync; the loop
    awaits the job's future, and a job runs to its end whatever happens to its awaiter. The credential directory is
    opened (`O_DIRECTORY|O_NOFOLLOW`) by `open_dir()` at keys' start, before keys counts its fixed fds, or, when it
    did not exist then, on the writer thread at the first commit (COMMIT_FDS budgets that case); it is kept.

    The seam (R86): `begin_closing()` sets `_closing`, after which `want` starts no refresh (R61);
    `cancel_detached()` cancels the members of `_refresh_tasks`, the refreshes `want` started. `want`, the
    refresh-wanted entry point, is not on the seam."""

    def __init__(self, cred_dir: str, *, seams: Optional[WriteSeams] = None) -> None:
        self.cred_dir = cred_dir
        self.seams = seams or WriteSeams()
        self.lock_timeout_s = LOCK_TIMEOUT_S
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="levain-cred-writer")
        self._dir_fd: Optional[int] = None    # set by open_dir() before any job, else by the writer thread only
        self._closing = False
        self._refresh_tasks: Set["asyncio.Task"] = set()
        #: the single-flight table: (name, rev) -> the future of the refresh in flight for that rev (a stalled refresh
        #: of one rev never holds the operator's next one)
        self._inflight: Dict[Tuple[str, str], "asyncio.Future"] = {}
        #: the one gate per credential, holding the `rev` it is for
        self._gates: Dict[str, _Gate] = {}
        self._refresh_sem: Optional[asyncio.Semaphore] = None
        #: the shielded commits' tasks, held until done (their outcome is retrieved even when the refresh was cancelled)
        self._commit_jobs: Set["asyncio.Task"] = set()

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

    # -- the refresh: the gate and the single-flight (design 1.8, R4, R5, R61) --

    def want(self, cred: Credential) -> "Optional[asyncio.Future]":
        """The refresh-wanted entry point, on keys' loop, with no await (so the check and the table update are one
        step): None when closing (R61) or when the gate is shut; else the future of the refresh in flight for
        `(name, rev)`, made here on a miss as a DETACHED task held in `_refresh_tasks`. The gate is the one for `name`
        when it holds `rev` (or `rev` is its own refresh's commit); any other `rev` gets a new, open gate. The future
        always resolves (to the outcome's name, never an exception), so no waiter hangs; a waiter awaits it shielded."""
        if self._closing:
            _note(logging.INFO, "levain gw-keys: credential %s refresh refused (closing)", cred.name)
            return None
        key = (cred.name, cred.rev)
        fut = self._inflight.get(key)
        if fut is not None:
            return fut
        g = self._gates.get(cred.name)
        if g is None or cred.rev not in (g.rev, g.own):
            g = self._gates[cred.name] = _Gate(cred.rev)
        now = self.seams.monotonic()
        if not g.admits(now):
            return None
        g.last_start = now
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._inflight[key] = fut
        task = loop.create_task(self._refresh(cred, g, fut))
        self._refresh_tasks.add(task)
        # a task cancelled before its first step never runs `_refresh`'s `finally`; this runs for every task
        task.add_done_callback(functools.partial(self._refresh_ended, key, fut))
        return fut

    def _refresh_ended(self, key: Tuple[str, str], fut: "asyncio.Future", task: "asyncio.Task") -> None:
        """A refresh task's done-callback, and `_refresh`'s `finally`: idempotent. Pops the table entry if it is still
        this refresh's, drops the task, and resolves the future ("cancelled" when nothing resolved it)."""
        if self._inflight.get(key) is fut:
            del self._inflight[key]
        self._refresh_tasks.discard(task)
        if not fut.done():
            fut.set_result("cancelled")

    def _commit_ended(self, job: "asyncio.Task") -> None:
        self._commit_jobs.discard(job)
        if not job.cancelled():
            job.exception()   # retrieved: a failure after the refresh was cancelled is not "never retrieved"

    async def _refresh(self, cred: Credential, gate: _Gate, fut: "asyncio.Future") -> None:
        """The detached refresh: the POST, the new object, the commit on the writer thread awaited SHIELDED (the job
        runs to its end whatever happens to this task; it is held in `_commit_jobs`) with the generation compare, its
        new `rev` made first and set as the gate's `own`. A failure counts on the gate and leaves the old file; a
        DISCARD (the operator's act landed) is neither. The `finally` resolves the future with the outcome; the
        task's done-callback repeats the cleanup for a task cancelled before its first step."""
        result = "cancelled"
        try:
            if self._refresh_sem is None:
                self._refresh_sem = asyncio.Semaphore(REFRESH_CONNS)   # made inside the running loop (3.9 binds it)
            async with self._refresh_sem:
                response = await _post(cred)
            fields = _refreshed(cred, response, self.seams.clock())
            gate.own = rev = new_rev()   # no await between this and the submit below
            job = asyncio.ensure_future(self.commit(cred.name, fields, expect_rev=cred.rev, rev=rev))
            self._commit_jobs.add(job)
            job.add_done_callback(self._commit_ended)
            out = await asyncio.shield(job)
            result = out.result
            if out.result == COMMITTED:
                gate.succeeded(out.rev)
            else:
                _note(logging.INFO, "levain gw-keys: credential %s refresh discarded (changed meanwhile)", cred.name)
        except Exception as e:
            result = "failed"
            gate.failed(self.seams.monotonic())
            _note(logging.WARNING, "levain gw-keys: credential-refresh-failed credential=%s (%s)", cred.name, _why(e))
        finally:
            if not fut.done():
                fut.set_result(result)
            task = asyncio.current_task()
            if task is not None:
                self._refresh_ended((cred.name, cred.rev), fut, task)

    # -- the writer thread --

    async def commit(self, name: str, fields: dict, expect_rev: Optional[str] = None,
                     rev: Optional[str] = None) -> Outcome:
        """`commit` on the writer thread, with keys' bounded `flock`."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor,
                                          functools.partial(self._commit_sync, name, fields, expect_rev, rev))

    def _commit_sync(self, name: str, fields: dict, expect_rev: Optional[str], rev: Optional[str] = None) -> Outcome:
        if self._dir_fd is None:
            try:
                self._dir_fd = os.open(self.cred_dir, _O_DIR)
            except OSError as e:
                _note(logging.WARNING, "levain gw-keys: credential-commit-failed (the credential directory: %s)",
                      type(e).__name__)
                raise CommitFailed("credential-commit-failed", "directory") from None
        return commit(self._dir_fd, name, fields, expect_rev=expect_rev, wait=False,
                      lock_timeout_s=self.lock_timeout_s, seams=self.seams, rev=rev)

    def close(self) -> None:
        """At keys' exit, after the loop: wait for a job in flight (bounded by the lock timeout and the fsyncs), then
        close the directory fd."""
        self._executor.shutdown(wait=True)
        if self._dir_fd is not None:
            os.close(self._dir_fd)
            self._dir_fd = None
