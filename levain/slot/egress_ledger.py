"""Root's host-grant LEDGER (board levain#66 slice S2, plan hgs2_plan_v2 sections 1, 2 and Q6): single use and revocation,
in the root-only ``L/routes/grants/`` (root 0700, made by ``enrol-authority-signer --egress-host``, never by a runtime
verb), with its two locks and the install's own ``store_id``.

  - ``used.jsonl``: one row per signature root ever published, kept until ``keep_until`` = ``issued`` + 24 h + 5 min
    (the latest any entry from it can expire), so a clock stepped backwards cannot re-admit a pruned id. Never evicted
    (eviction would reopen replay); past :data:`USED_MAX` rows a new tap is ``egress-grant-ledger-full`` (fail closed).
  - ``revoked.jsonl``: ids a ``revoke`` named that no file held (a signed card not yet delivered). ``keep_until`` = now +
    freshness + skew, the longest the signed bytes could still pass; at most :data:`REVOKED_MAX`, pruned first and then
    the OLDEST evicted: a revoke never refuses for a full ledger.
  - ``ledger.lock`` (a bounded wait) serializes every read-modify-write of both files across ALL sessions;
    ``verify.lock`` (a TRY) lets one ssh-keygen run at a time install-wide. Lock order: verify lock (taken and released
    before any other), then the session lock, then the ledger lock. Nothing takes the ledger lock and then another.

A ledger that does not parse is ``egress-grant-ledger`` and is never read as empty or rewritten. Every write replaces
the file whole by a unique temp and a rename (never an append: a torn tail would brick the ledger), synced as
:func:`levain.slot.egress_store.fsync_of` says.

Root's ``/usr/bin/python3`` (3.9) imports this module: no match statement, no ``X | Y`` outside annotations.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import stat
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Dict, Iterator, List, Optional

from levain.broker.policy import is_host_name
from levain.slot import Refused, write_all
from levain.slot import egress_grants as eg
from levain.slot.egress_store import NOT_READY, fsync_of

USED_NAME, REVOKED_NAME = "used.jsonl", "revoked.jsonl"
LEDGER_LOCK, VERIFY_LOCK, STORE_ID_NAME = "ledger.lock", "verify.lock", "store_id"
USED_MAX = 4096
REVOKED_MAX = 256
FILE_MAX = 2 * 1024 * 1024
LEDGER_LOCK_WAIT_S = 10.0
LEDGER_DIR_PARTS = ("routes", "grants")
ERR_LEDGER, ERR_FULL, ERR_BUSY = "egress-grant-ledger", "egress-grant-ledger-full", "egress-grant-busy"

_STORE_ID = re.compile(r"[0-9a-f]{32}\n?")
_TEMP = re.compile(r"(?:used|revoked)\.jsonl\.[0-9a-f]{16}\.new")
_GRANT = re.compile(r"eh-[0-9a-f]{24}")
_SESSION = re.compile(r"s-[0-9a-f]{16}")
_O_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_O_LOCK = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_USED_KEYS = frozenset({"grant", "session", "host", "issued", "keep_until"})
_REVOKED_KEYS = frozenset({"grant", "keep_until"})


class LedgerWriteFailed(OSError):
    """A ledger rewrite failed BEFORE its rename: the old ledger is in force, nothing was recorded."""


class Used:
    __slots__ = ("grant", "session", "host", "issued", "keep_until")

    def __init__(self, grant: str, session: str, host: str, issued: datetime, keep_until: datetime) -> None:
        self.grant, self.session, self.host, self.issued, self.keep_until = grant, session, host, issued, keep_until


class Revoked:
    __slots__ = ("grant", "keep_until")

    def __init__(self, grant: str, keep_until: datetime) -> None:
        self.grant, self.keep_until = grant, keep_until


# --- the directory, the store id, the locks -----------------------------------------------------------------------------

@contextmanager
def ledger_dir(ctx: object) -> Iterator[int]:
    """An fd on ``L/routes/grants/``: a trusted walk, root's, no group or other access. Absent or not root's is
    ``egress-grant-not-ready`` (the install verb makes it; no runtime verb does)."""
    try:
        fd = ctx.store.open_dir(*LEDGER_DIR_PARTS)  # type: ignore[attr-defined]
    except Refused:
        raise Refused(NOT_READY, "L/routes/grants is not there or not root's") from None
    try:
        st = os.fstat(fd)
        if st.st_mode & 0o077:
            raise Refused(NOT_READY, "L/routes/grants is open to a group or others")
        yield fd
    finally:
        os.close(fd)


def read_store_id(ctx: object, dirfd: int) -> str:
    """The id root drew at install: ``store_id``, a regular file owned by root, exactly 0600, 32 lowercase hex.
    Anything else, or absent, is ``egress-grant-not-ready``. A slot, an operator or the front supplies no part of it."""
    seams = ctx.seams  # type: ignore[attr-defined]
    try:
        fd = os.open(STORE_ID_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dirfd)
    except OSError:
        raise Refused(NOT_READY, "no store id") from None
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != seams.chowned_uid(0) or stat.S_IMODE(st.st_mode) != 0o600
                or st.st_size > 64):
            raise Refused(NOT_READY, "the store id is not a root 0600 file")
        raw = os.read(fd, 65)
    except OSError:
        raise Refused(NOT_READY, "the store id cannot be read") from None
    finally:
        os.close(fd)
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise Refused(NOT_READY, "the store id is not hex") from None
    if _STORE_ID.fullmatch(text) is None:
        raise Refused(NOT_READY, "the store id is not 32 lowercase hex")
    return text.strip()


@contextmanager
def _flock(ctx: object, dirfd: int, name: str, wait_s: Optional[float]) -> Iterator[None]:
    seams = ctx.seams  # type: ignore[attr-defined]
    try:
        fd = os.open(name, _O_LOCK, 0o600, dir_fd=dirfd)
    except OSError:
        raise Refused(NOT_READY, "%s cannot be opened" % name) from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != seams.chowned_uid(0):
            raise Refused(NOT_READY, "%s is not a regular root file" % name)
        deadline = None  # type: Optional[float]
        delay = 0.01
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    raise
                if wait_s is None:
                    raise Refused(ERR_BUSY, "%s is held" % name) from None
                now = seams.monotonic()
                deadline = now + wait_s if deadline is None else deadline
                if now >= deadline:
                    raise Refused(ERR_BUSY, "%s held past %gs" % (name, wait_s)) from None
                seams.sleep(delay)
                delay = min(delay * 2, 0.25)
        yield
    finally:
        os.close(fd)           # closing the only descriptor releases the flock


def verify_lock(ctx: object, dirfd: int):
    """The install-wide TRY lock around one signature verification: held is ``egress-grant-busy``."""
    return _flock(ctx, dirfd, VERIFY_LOCK, None)


def ledger_lock(ctx: object, dirfd: int):
    """The global ledger lock, waited for up to :data:`LEDGER_LOCK_WAIT_S`, else ``egress-grant-busy``."""
    return _flock(ctx, dirfd, LEDGER_LOCK, LEDGER_LOCK_WAIT_S)


# --- rows ---------------------------------------------------------------------------------------------------------------------

def _no_dups(pairs):
    out = {}  # type: Dict[str, object]
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key")
        out[k] = v
    return out


def _no_const(_n):
    raise ValueError("not JSON")


def _stamp(v: object) -> datetime:
    return eg.parse_stamp(v)


def _read_raw(ctx: object, dirfd: int, name: str) -> bytes:
    seams = ctx.seams  # type: ignore[attr-defined]
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dirfd)
    except FileNotFoundError:
        return b""
    except OSError:
        raise Refused(ERR_LEDGER, "%s cannot be opened" % name) from None
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != seams.chowned_uid(0) or st.st_mode & 0o077
                or st.st_size > FILE_MAX):
            raise Refused(ERR_LEDGER, "%s is not a root 0600 file within its bound" % name)
        chunks = []  # type: List[bytes]
        size = 0
        while size <= FILE_MAX:
            chunk = os.read(fd, FILE_MAX + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > FILE_MAX:
            raise Refused(ERR_LEDGER, "%s is past its bound" % name)
        return b"".join(chunks)
    except OSError:
        raise Refused(ERR_LEDGER, "%s cannot be read" % name) from None
    finally:
        os.close(fd)


def _lines(raw: bytes, name: str) -> List[Dict[str, object]]:
    if not raw:
        return []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused(ERR_LEDGER, "%s is not UTF-8" % name) from None
    if not text.endswith("\n"):
        raise Refused(ERR_LEDGER, "%s has a torn last line" % name)
    out = []  # type: List[Dict[str, object]]
    for line in text[:-1].split("\n"):
        try:
            obj = json.loads(line, object_pairs_hook=_no_dups, parse_constant=_no_const)
        except (ValueError, RecursionError):
            raise Refused(ERR_LEDGER, "%s has a row that is not JSON" % name) from None
        if not isinstance(obj, dict):
            raise Refused(ERR_LEDGER, "%s has a row that is not an object" % name)
        out.append(obj)
    return out


def read_used(ctx: object, dirfd: int) -> List[Used]:
    """``used.jsonl`` strictly parsed (exact keys, each in its grammar, no id twice), ``egress-grant-ledger`` otherwise."""
    rows = []  # type: List[Used]
    seen = set()
    for obj in _lines(_read_raw(ctx, dirfd, USED_NAME), USED_NAME):
        try:
            if (set(obj) != _USED_KEYS or not isinstance(obj["grant"], str) or _GRANT.fullmatch(obj["grant"]) is None
                    or not isinstance(obj["session"], str) or _SESSION.fullmatch(obj["session"]) is None
                    or not isinstance(obj["host"], str) or not is_host_name(obj["host"]) or obj["grant"] in seen):
                raise ValueError
            seen.add(obj["grant"])
            rows.append(Used(obj["grant"], obj["session"], obj["host"], _stamp(obj["issued"]),
                             _stamp(obj["keep_until"])))
        except (ValueError, eg.GrantRefused):
            raise Refused(ERR_LEDGER, "%s has a row out of shape" % USED_NAME) from None
    return rows


def read_revoked(ctx: object, dirfd: int) -> List[Revoked]:
    rows = []  # type: List[Revoked]
    seen = set()
    for obj in _lines(_read_raw(ctx, dirfd, REVOKED_NAME), REVOKED_NAME):
        try:
            if (set(obj) != _REVOKED_KEYS or not isinstance(obj["grant"], str) or _GRANT.fullmatch(obj["grant"]) is None
                    or obj["grant"] in seen):
                raise ValueError
            seen.add(obj["grant"])
            rows.append(Revoked(obj["grant"], _stamp(obj["keep_until"])))
        except (ValueError, eg.GrantRefused):
            raise Refused(ERR_LEDGER, "%s has a row out of shape" % REVOKED_NAME) from None
    return rows


def _dump(objs: List[Dict[str, str]]) -> bytes:
    return b"".join((json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")
                    for o in objs)


def _write(ctx: object, dirfd: int, name: str, raw: bytes) -> None:
    """Replace ``name`` whole: a unique temp (``O_EXCL``), written, synced, renamed, the directory synced. A failure
    before the rename leaves the old ledger and raises :class:`LedgerWriteFailed`."""
    sync = fsync_of(ctx.seams)  # type: ignore[attr-defined]
    tmp = "%s.%s.new" % (name, os.urandom(8).hex())
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dirfd)
        try:
            write_all(fd, raw)
            sync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
    except OSError as e:
        try:
            os.unlink(tmp, dir_fd=dirfd)
        except OSError:
            pass
        raise LedgerWriteFailed(e.errno or errno.EIO, "%s: %s" % (name, e)) from e
    try:
        sync(dirfd)
    except OSError as e:
        raise LedgerWriteFailed(e.errno or errno.EIO, "%s: directory sync: %s" % (name, e)) from e
    _sweep(dirfd)


def _sweep(dirfd: int) -> None:
    try:
        for n in sorted(x for x in os.listdir(dirfd) if _TEMP.fullmatch(x))[:16]:
            try:
                os.unlink(n, dir_fd=dirfd)
            except OSError:
                pass
    except OSError:
        pass


def keep_until_for(issued: datetime) -> datetime:
    """The last moment an entry from a grant issued at ``issued`` can still be live (expires at most ``issued`` + 24 h,
    plus the skew): a used row older than this is gone for good."""
    return issued + timedelta(seconds=eg.DAY_TTL_S + eg.SKEW_S)


def used_for(rows: List[Used], grant: str) -> Optional[Used]:
    for r in rows:
        if r.grant == grant:
            return r
    return None


def record_used(ctx: object, dirfd: int, grant: eg.HostGrant, now: datetime) -> None:
    """The commit point of single use, under the ledger lock the caller holds: re-read both files, prune what is past
    its ``keep_until``, refuse an id already used (``egress-grant-used``) or revoked (``egress-grant-revoked``) or a
    full ledger, then replace ``used.jsonl`` with the new row added. A ledger that does not parse is refused untouched."""
    used = [r for r in read_used(ctx, dirfd) if r.keep_until > now]
    revoked = [r for r in read_revoked(ctx, dirfd) if r.keep_until > now]
    if used_for(used, grant.grant) is not None:
        raise Refused("egress-grant-used", "that grant was already published")
    if any(r.grant == grant.grant for r in revoked):
        raise Refused("egress-grant-revoked", "that grant was revoked before it arrived")
    if len(used) >= USED_MAX:
        raise Refused(ERR_FULL, "used.jsonl is full")
    used.append(Used(grant.grant, grant.session, grant.host, grant.issued, keep_until_for(grant.issued)))
    _write(ctx, dirfd, USED_NAME, _dump([{"grant": r.grant, "session": r.session, "host": r.host,
                                          "issued": eg.stamp(r.issued), "keep_until": eg.stamp(r.keep_until)}
                                         for r in used]))


def record_revoked(ctx: object, dirfd: int, ids: List[str], now: datetime) -> None:
    """Record ``ids`` (levain-minted ids no file held), under the ledger lock the caller holds: prune rows past their
    ``keep_until`` FIRST, add, then evict the OLDEST beyond :data:`REVOKED_MAX`. Never refuses for a full ledger."""
    rows = [r for r in read_revoked(ctx, dirfd) if r.keep_until > now]
    have = {r.grant for r in rows}
    keep = now + timedelta(seconds=eg.FRESH_S + eg.SKEW_S)
    for i in ids:
        if i not in have:
            rows.append(Revoked(i, keep))
            have.add(i)
    rows = rows[-REVOKED_MAX:]
    _write(ctx, dirfd, REVOKED_NAME, _dump([{"grant": r.grant, "keep_until": eg.stamp(r.keep_until)} for r in rows]))


__all__ = ["ERR_BUSY", "ERR_FULL", "ERR_LEDGER", "FILE_MAX", "LEDGER_LOCK_WAIT_S", "LedgerWriteFailed", "REVOKED_MAX",
           "Revoked", "USED_MAX", "Used", "keep_until_for", "ledger_dir", "ledger_lock", "read_revoked",
           "read_store_id", "read_used", "record_revoked", "record_used", "used_for", "verify_lock"]
