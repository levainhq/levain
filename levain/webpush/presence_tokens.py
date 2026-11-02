"""Presence tokens: a credential bound to one purpose, setting the operator's presence word from a tap (an Apple Shortcut,
an NFC tag) with no location fix and none of the authority a cockpit token carries.

The file (``<queue>/webpush/presence_tokens.json``, 0600) holds only the sha256 of each token, never the token: the
secret is shown once, at creation. The web layer resolves a supplied token here, asks again under the presence lock at
the write, and owns the rate limit (:class:`RateLimit`); this module decides nothing about what a word does (the record, :mod:`levain.webpush.intent`, does). A token may set only the words on its allow-list, and
``sleep`` and ``loud`` (the two that silence or force the phone for a long stretch) only when the operator listed them.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
import time
from collections import deque
from pathlib import Path

from levain.webpush.presence import LOG_NAME as _LOG_NAME  # noqa: F401 - the same directory, one place
from levain.webpush.presence import WORDS as _PRESENCE_WORDS, PresenceError, _locked
from levain.webpush.route import ROUTE_WORDS

WORDS = _PRESENCE_WORDS + ROUTE_WORDS   # what a token may be listed for: the presets, and the two aliases (`desk` -> follow, `devices` -> devices on)

FILE_NAME = "presence_tokens.json"
DEFAULT_WORDS = ("away", "back", "quiet", "desk", "devices")
NAME_RE = re.compile(r"^[A-Za-z0-9 ._-]{1,40}$")
MAX_TOKENS = 50
_MAX_BYTES = 64 * 1024
RATE_PER_MIN = 10
HEADER = "X-Levain-Presence-Token"


def _path(queue_dir: Path | str) -> Path:
    return Path(queue_dir) / "webpush" / FILE_NAME


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenRefused(PresenceError):
    """A tap token refused at the write itself (revoked, or the word no longer on its list). ``error`` is the reply's
    error code."""

    def __init__(self, error: str, message: str) -> None:
        super().__init__(message)
        self.error = error


def _read(queue_dir: Path | str) -> list[dict]:
    """The records on file; a missing file is none. Anything else that is not a readable file of this user's (an I/O
    error, oversize, foreign-owned, malformed) raises :class:`PresenceError`: a caller about to write the list back
    must never take a failed read for an empty list, or it would drop every token. A read-only caller that wants a
    refusal instead uses :func:`_read_or_none`."""
    path = _path(queue_dir)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return []
    except OSError as e:
        raise PresenceError(f"presence tokens not read: {e}") from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size > _MAX_BYTES:
            raise PresenceError(f"{path} is not a tap-token file of this user's")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            got = json.loads(fh.read(_MAX_BYTES + 1).decode("utf-8"))
    except OSError as e:
        raise PresenceError(f"presence tokens not read: {e}") from e
    except (ValueError, RecursionError) as e:      # a deeply nested document is malformed like any other
        raise PresenceError(f"{path} is malformed: remove it to start a new list") from e
    finally:
        os.close(fd)
    recs = got.get("tokens") if isinstance(got, dict) else None
    if not isinstance(recs, list):
        raise PresenceError(f"{path} is malformed: remove it to start a new list")
    out = []
    for r in recs:
        if (isinstance(r, dict) and isinstance(r.get("name"), str) and isinstance(r.get("sha256"), str)
                and isinstance(r.get("words"), list)):
            out.append(r)
    return out


def _read_or_none(queue_dir: Path | str) -> list[dict]:
    """:func:`_read` for a caller that only reads: a file that cannot be read is no tokens (a token is then unknown,
    which is a refusal)."""
    try:
        return _read(queue_dir)
    except PresenceError:
        return []


def _write(queue_dir: Path | str, recs: list[dict]) -> None:
    path = _path(queue_dir)
    d = path.parent
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise PresenceError(f"{d} is not a directory of this user's")
        try:
            cur = os.lstat(path)
        except FileNotFoundError:
            cur = None
        if cur is not None and (not stat.S_ISREG(cur.st_mode) or cur.st_uid != os.getuid()):
            raise PresenceError(f"{path} is not a file of this user's")
        tmp = d / f".{FILE_NAME}.{os.getpid()}.{time.time_ns()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            try:
                view = memoryview(json.dumps({"version": 1, "tokens": recs}).encode("utf-8"))
                while view:
                    n = os.write(fd, view)
                    if n <= 0:
                        raise OSError("short write")
                    view = view[n:]
                os.fsync(fd)        # the bytes are durable before the rename makes them the list
            finally:
                os.close(fd)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
    except OSError as e:
        raise PresenceError(f"presence tokens not written: {e}") from e


def _public(r: dict) -> dict:
    return {"name": r["name"], "words": [w for w in r["words"] if w in WORDS], "created": r.get("created"),
            "last_used": r.get("last_used"), "revoked": bool(r.get("revoked"))}


def create(queue_dir: Path | str, name: str, words: "list[str] | tuple[str, ...] | None" = None, *,
           now: float | None = None) -> tuple[dict, str]:
    """Mint a token named ``name`` allowed to set ``words`` (default :data:`DEFAULT_WORDS`). Returns the public record
    and the token itself, which is not kept anywhere and cannot be shown again. A live token of the same name is a
    refusal (a name says which tap did what, so it stays unique among the live ones)."""
    if not isinstance(name, str) or not NAME_RE.match(name) or name != name.strip():
        raise PresenceError("a token's name is 1 to 40 letters, digits, spaces, dots, dashes or underscores")
    use = list(DEFAULT_WORDS if words is None else words)
    if not use or any(not isinstance(w, str) or w not in WORDS for w in use):
        raise PresenceError(f"a token's words are some of: {', '.join(WORDS)}")
    use = [w for w in WORDS if w in use]
    token = secrets.token_urlsafe(32)
    with _locked(queue_dir, strict=True, wait=3.0):
        recs = [r for r in _read(queue_dir) if not r.get("revoked")]   # a revoked record is dropped here: the file stays small
        if any(r["name"] == name for r in recs):
            raise PresenceError(f"a tap token named {name!r} already exists: revoke it or pick another name")
        if len(recs) >= MAX_TOKENS:
            raise PresenceError("too many tap tokens: revoke one first")
        rec = {"name": name, "sha256": _hash(token), "created": time.time() if now is None else now,
               "last_used": None, "words": use, "revoked": False}
        _write(queue_dir, recs + [rec])
    return _public(rec), token


def verify(queue_dir: Path | str, supplied: str) -> dict | None:
    """The record ``supplied`` is the token of (hash compared in constant time against EVERY record, no early exit),
    or ``None`` for unknown or revoked. The record is the public one, with no hash."""
    got = None
    digest = _hash(supplied if isinstance(supplied, str) else "").encode("ascii")
    for r in _read_or_none(queue_dir):
        same = hmac.compare_digest(digest, r["sha256"].encode("utf-8", "replace"))
        if same and not r.get("revoked") and got is None:
            got = _public(r)
    return got


def revoke(queue_dir: Path | str, name: str) -> bool:
    """Revoke the live token named ``name``; whether one was. The record stays, marked, so its name is on the list."""
    with _locked(queue_dir, strict=True, wait=3.0):
        recs = _read(queue_dir)
        hit = False
        for r in recs:
            if r["name"] == name and not r.get("revoked"):
                r["revoked"] = True
                hit = True
        if hit:
            _write(queue_dir, recs)
    return hit


def list_public(queue_dir: Path | str) -> list[dict]:
    """The live tokens, with no hash."""
    return [_public(r) for r in _read_or_none(queue_dir) if not r.get("revoked")]


def stamp(queue_dir: Path | str, name: str, *, now: float | None = None) -> None:
    """Stamp ``last_used`` on the live token ``name``, for a caller ALREADY holding the presence lock (a second
    ``flock`` from the same process on another descriptor would wait on itself). Best effort: a display stamp never
    fails the write it follows. A file that cannot be read, or no live token of that name, writes nothing."""
    try:
        recs = _read(queue_dir)
        hit = False
        for r in recs:
            if r["name"] == name and not r.get("revoked"):
                r["last_used"] = time.time() if now is None else now
                hit = True
        if hit:
            _write(queue_dir, recs)
    except PresenceError:
        pass


def touch(queue_dir: Path | str, name: str, *, now: float | None = None) -> None:
    """:func:`stamp` under its own hold of the presence lock. Best effort, like it."""
    try:
        with _locked(queue_dir, strict=True, wait=0.5):
            stamp(queue_dir, name, now=now)
    except PresenceError:
        pass


class RateLimit:
    """A sliding one-minute count of writes per key, in memory. :meth:`hit` returns 0.0 when the write may go, else the
    seconds to wait."""

    def __init__(self, per_min: int = RATE_PER_MIN) -> None:
        self._per = per_min
        self._seen: dict[str, deque] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, *, now: float | None = None) -> float:
        t = time.monotonic() if now is None else now
        with self._lock:
            q = self._seen.setdefault(key, deque())
            while q and t - q[0] >= 60.0:
                q.popleft()
            if len(q) >= self._per:
                return max(1.0, 60.0 - (t - q[0]))
            q.append(t)
            return 0.0
