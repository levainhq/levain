"""Notify tokens: a credential bound to one purpose, posting a notice (:mod:`levain.webpush.notify`) at the operator.

Sibling of :mod:`levain.webpush.presence_tokens`. The file (``<queue>/webpush/notify_tokens.json``, 0600) holds only the
sha256 of each token, never the token: the secret is shown once, at creation. A token carries its own SOURCE label (a
slug the operator chose at mint, immutable after), a priority ceiling, whether it may be urgent at all, and its rate caps.

One entry point decides a post: :func:`admit`. It resolves the token, applies the ceiling and the sanitiser and the
caps, and stamps the token, all under one lock that :func:`revoke` also takes, so a revoke lands before a post is
admitted or after it, never between. The day counter and the urgent-hour stamps live in the token's record and survive
a restart; the per-minute window is in memory and starts empty after one.
"""
from __future__ import annotations

import fcntl
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
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from levain.webpush import notify
from levain.webpush.notify import NotifyRefused, PRIORITIES, RANK

__all__ = ["Admitted", "NotifyBusy", "NotifyTokenError", "admit", "create", "list_public", "revoke", "verify", "FILE_NAME", "HEADER",
           "TOKEN_PREFIX"]

FILE_NAME = "notify_tokens.json"
LOCK_NAME = ".notify.lock"
HEADER = "X-Levain-Notify-Token"       # ``Authorization: Bearer <token>`` is its alias at the web layer
TOKEN_PREFIX = "lvn_"                  # greppable, so a leaked token is found by a scanner
# every use of these is a fullmatch: a `$` anchor would admit a trailing newline
NAME_RE = re.compile(r"[A-Za-z0-9 ._-]{1,40}")
SOURCE_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,23}")
# the shape :func:`create` mints (the prefix, then ``secrets.token_urlsafe(32)``): anything else is no token, refused
# without reading the file
TOKEN_RE = re.compile(re.escape(TOKEN_PREFIX) + r"[A-Za-z0-9_-]{43}")
FORBIDDEN = ("forbidden", "unknown or revoked token")     # one refusal for unknown, revoked and malformed alike
MAX_TOKENS = 50
_MAX_BYTES = 128 * 1024
RATE_PER_MIN = 6
RATE_PER_DAY = 200
URGENT_PER_HOUR = 3
_DAY = 86400.0


class NotifyTokenError(ValueError):
    """A request about the token list that cannot be applied, or a token file that cannot be read or written safely."""


class NotifyBusy(NotifyTokenError):
    """The notify lock stayed held past the wait: nothing was changed, and a retry may succeed."""


@contextmanager
def _locked(queue_dir: Path | str, wait: float = 3.0):
    d = Path(queue_dir) / "webpush"
    fd = None
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise NotifyTokenError(f"{d} is not a directory of this user's")
        fd = os.open(d / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise NotifyTokenError(f"{d / LOCK_NAME} is not a file of this user's")
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise NotifyBusy("the notify lock is held by another process") from None
                time.sleep(0.02)
    except OSError as e:
        if fd is not None:
            os.close(fd)
        raise NotifyTokenError(f"notify lock not taken: {e}") from e
    except NotifyTokenError:
        if fd is not None:
            os.close(fd)
        raise
    try:
        yield
    finally:
        os.close(fd)


def _path(queue_dir: Path | str) -> Path:
    return Path(queue_dir) / "webpush" / FILE_NAME


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _well_formed(r: object) -> bool:
    """A record every reader can use without a type error: the three strings, the caps and counters as whole numbers,
    the ceiling one of the priorities."""
    if not isinstance(r, dict) or not all(isinstance(r.get(k), str) for k in ("name", "sha256", "source")):
        return False
    for k in ("rate_per_min", "rate_per_day", "urgent_per_hour", "day_count"):
        v = r.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
            return False
    if r.get("max_priority") is not None and r.get("max_priority") not in PRIORITIES:
        return False
    if r.get("origin") not in (None, "token", "flow"):
        return False
    return r.get("urgent_times") is None or isinstance(r.get("urgent_times"), list)


def _read(queue_dir: Path | str, *, strict: bool = True) -> list[dict]:
    """The records on file; a missing file is none. Anything else that is not a readable file of this user's raises
    :class:`NotifyTokenError`: a caller about to write the list back must never take a failed read for an empty list.
    ``strict`` (every writer, and every listing) also refuses a file holding ANY malformed record, so a write never
    rewrites the list without it and a listing is never a partial one; ``strict=False`` is verification's, where a
    malformed record is no token and the rest still verify."""
    path = _path(queue_dir)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return []
    except OSError as e:
        raise NotifyTokenError(f"notify tokens not read: {e}") from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size > _MAX_BYTES:
            raise NotifyTokenError(f"{path} is not a notify-token file of this user's")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            got = json.loads(fh.read(_MAX_BYTES + 1).decode("utf-8"))
    except NotifyTokenError:        # a ValueError too: its own reason, never reported as "malformed"
        raise
    except OSError as e:
        raise NotifyTokenError(f"notify tokens not read: {e}") from e
    except (ValueError, RecursionError) as e:
        raise NotifyTokenError(f"{path} is malformed: remove it to start a new list") from e
    finally:
        os.close(fd)
    recs = got.get("tokens") if isinstance(got, dict) else None
    if not isinstance(recs, list):
        raise NotifyTokenError(f"{path} is malformed: remove it to start a new list")
    good = [r for r in recs if _well_formed(r)]
    if strict and len(good) != len(recs):
        raise NotifyTokenError(f"{path} holds {len(recs) - len(good)} malformed token record(s); nothing was changed: "
                               "repair the file or remove it to start a new list")
    return good


def _read_or_none(queue_dir: Path | str) -> list[dict]:
    try:
        return _read(queue_dir, strict=False)
    except NotifyTokenError:
        return []


def _write(queue_dir: Path | str, recs: list[dict]) -> None:
    path = _path(queue_dir)
    d = path.parent
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise NotifyTokenError(f"{d} is not a directory of this user's")
        try:
            cur = os.lstat(path)
        except FileNotFoundError:
            cur = None
        if cur is not None and (not stat.S_ISREG(cur.st_mode) or cur.st_uid != os.getuid()):
            raise NotifyTokenError(f"{path} is not a file of this user's")
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
                os.fsync(fd)
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
        raise NotifyTokenError(f"notify tokens not written: {e}") from e


def _public(r: dict) -> dict:
    """The record without its hash or its counters' internals."""
    return {"name": r["name"], "source": r["source"], "origin": r.get("origin") or "token", "max_priority": r.get("max_priority", "normal"),
            "urgent": bool(r.get("urgent")), "rate_per_min": r.get("rate_per_min", RATE_PER_MIN),
            "rate_per_day": r.get("rate_per_day", RATE_PER_DAY),
            "urgent_per_hour": r.get("urgent_per_hour", URGENT_PER_HOUR), "created": r.get("created"),
            "last_used": r.get("last_used"), "revoked": bool(r.get("revoked")),
            "sent_today": r.get("day_count", 0) if r.get("day") == int(time.time() // _DAY) else 0}


def _cap(value: object, default: int, what: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100_000:
        raise NotifyTokenError(f"{what} is a whole number from 1 to 100000")
    return value


def create(queue_dir: Path | str, name: str, source: str | None = None, *, max_priority: str = "normal",
           urgent: bool = False, rate_per_min: int | None = None, rate_per_day: int | None = None,
           urgent_per_hour: int | None = None, origin: str = "token", now: float | None = None) -> tuple[dict, str]:
    """Mint a token. Returns the public record and the token, which is kept nowhere and cannot be shown again. ``source``
    (the label every notice from it carries) defaults to the name lowercased when that is a valid slug; it is unique among
    live tokens and cannot change afterwards. ``urgent=True`` is the only way a token may post an urgent notice, and it
    raises the ceiling to ``urgent``; ``max_priority="urgent"`` without it is a refusal. ``origin`` is ``token`` (a service
    the operator gave a key: its links show in full) or ``flow`` (flow's own poster: it may name the notice's source, and
    its links render as Levain's own); the HTTP mint route never takes it, the operator's terminal does."""
    if origin not in ("token", "flow"):
        raise NotifyTokenError("a token's origin is token or flow")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name != name.strip():
        raise NotifyTokenError("a token's name is 1 to 40 letters, digits, spaces, dots, dashes or underscores")
    label = name.lower().replace(" ", "-") if source is None else source
    if not isinstance(label, str) or not SOURCE_RE.fullmatch(label):
        raise NotifyTokenError("a source label is 1 to 24 lowercase letters, digits, dots, dashes or underscores")
    if max_priority not in PRIORITIES:
        raise NotifyTokenError(f"a token's highest priority is one of: {', '.join(PRIORITIES)}")
    if urgent is not True and urgent is not False:
        raise NotifyTokenError("urgent is true or false")
    if max_priority == "urgent" and not urgent:
        raise NotifyTokenError("a token that may post urgent notices must be marked urgent")
    if urgent:
        max_priority = "urgent"
    caps = {"rate_per_min": _cap(rate_per_min, RATE_PER_MIN, "the per-minute cap"),
            "rate_per_day": _cap(rate_per_day, RATE_PER_DAY, "the per-day cap"),
            "urgent_per_hour": _cap(urgent_per_hour, URGENT_PER_HOUR, "the urgent per-hour cap")}
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    with _locked(queue_dir):
        recs = [r for r in _read(queue_dir) if not r.get("revoked")]   # a revoked record is dropped: the file stays small
        if any(r["name"] == name for r in recs):
            raise NotifyTokenError(f"a notify token named {name!r} already exists: revoke it or pick another name")
        if any(r["source"] == label for r in recs):
            raise NotifyTokenError(f"the source label {label!r} is in use by a live token")
        if len(recs) >= MAX_TOKENS:
            raise NotifyTokenError("too many notify tokens: revoke one first")
        rec = {"name": name, "source": label, "sha256": _hash(token), "created": time.time() if now is None else now,
               "last_used": None, "origin": origin, "max_priority": max_priority, "urgent": bool(urgent), **caps, "revoked": False,
               "day": None, "day_count": 0, "urgent_times": []}
        _write(queue_dir, recs + [rec])
    return _public(rec), token


def _find(recs: list[dict], supplied: object) -> dict | None:
    """The live record ``supplied`` is the token of: the hash is compared in constant time against EVERY record."""
    digest = _hash(supplied if isinstance(supplied, str) else "").encode("ascii")
    got = None
    for r in recs:
        same = hmac.compare_digest(digest, r["sha256"].encode("utf-8", "replace"))
        if same and not r.get("revoked") and got is None:
            got = r
    return got


def verify(queue_dir: Path | str, supplied: str) -> dict | None:
    """The public record ``supplied`` is the live token of, or ``None`` (unknown, revoked and malformed read the same).
    Takes no lock and writes nothing, so the web layer asks it from the header alone, before it holds a request slot or
    reads a body; :func:`admit` asks again under the lock."""
    if not isinstance(supplied, str) or not TOKEN_RE.fullmatch(supplied):
        return None
    r = _find(_read_or_none(queue_dir), supplied)
    return None if r is None else _public(r)


def revoke(queue_dir: Path | str, name: str) -> bool:
    """Revoke the live token named ``name``; whether one was. Takes the lock :func:`admit` holds across a post, so the
    very next post with it fails."""
    with _locked(queue_dir):
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
    """The live tokens: never a hash, never a secret. A token file that cannot be read whole raises
    :class:`NotifyTokenError` rather than answer with an empty or partial list."""
    return [_public(r) for r in _read(queue_dir) if not r.get("revoked")]


class _MinuteWindow:
    """A sliding one-minute count per token, in memory (a restart empties it; the day counter is the durable bound)."""

    def __init__(self) -> None:
        self._seen: dict[str, deque] = {}
        self._lock = threading.Lock()

    def wait(self, key: str, cap: int, now: float) -> float:
        with self._lock:
            q = self._seen.setdefault(key, deque())
            while q and now - q[0] >= 60.0:
                q.popleft()
            return max(1.0, 60.0 - (now - q[0])) if len(q) >= cap else 0.0

    def record(self, key: str, now: float) -> None:
        with self._lock:
            self._seen.setdefault(key, deque()).append(now)

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()


MINUTE = _MinuteWindow()


@dataclass(frozen=True)
class Admitted:
    """A post that passed every check: what to record and decide on. ``title`` is the poster's own, sanitised; the
    ``display`` title carries the token's source label."""
    token: str
    source: str
    title: str
    body: str
    priority: str
    kind: str
    origin: str = "token"

    @property
    def display(self) -> str:
        return notify.display_title(self.source, self.title)


def admit(queue_dir: Path | str, supplied: str, title: object, body: object, priority: object = None, *,
          source: object = None, no_body: bool = False, now: float | None = None) -> Admitted:
    """Decide one post. Under the notify lock: the token (unknown and revoked are one refusal, ``forbidden``), the
    priority against the token's ceiling (a request above it is refused with a named reason, never lowered), the
    sanitised text, then the caps (per minute, per day, urgent per hour). Only a post that passes everything counts toward
    a cap and stamps ``last_used``. Raises :class:`levain.webpush.notify.NotifyRefused`; a token file that cannot be read
    or written whole (a malformed record in it included: the stamp would rewrite the list without it) raises
    :class:`NotifyTokenError`, and nothing is admitted. ``source`` names the notice's source (the chip) and only a
    ``flow``-origin token may send one (any other token sending one is a bad request, as for any key it may not use);
    ``no_body`` is a post that sent no body at all, stored with an empty one (a body that is sent must be non-empty)."""
    t = time.time() if now is None else now
    with _locked(queue_dir):
        recs = _read(queue_dir)
        rec = _find(recs, supplied)
        if rec is None:
            raise NotifyRefused(*FORBIDDEN, 403)
        want = "normal" if priority is None else priority
        if not isinstance(want, str) or want not in RANK:
            raise NotifyRefused("bad_priority", f"priority is one of: {', '.join(PRIORITIES)}")
        if want == "urgent" and not rec.get("urgent"):
            raise NotifyRefused("urgent_not_permitted", "this token is not marked urgent", 403)
        if RANK[want] > RANK.get(rec.get("max_priority"), RANK["normal"]):
            raise NotifyRefused("priority_above_token_max", f"this token's highest priority is {rec.get('max_priority')}", 403)
        origin = rec.get("origin") or "token"
        label = rec["source"]
        if source is not None:
            if origin != "flow":
                raise NotifyRefused("bad_request", "only flow's own token names a source: for any other it is the token's label")
            if not isinstance(source, str) or not SOURCE_RE.fullmatch(source):
                raise NotifyRefused("bad_source", "a source is 1 to 24 lowercase letters, digits, dots, dashes or underscores")
            label = source
        clean_title = notify.clean_title(title)
        clean_body = "" if no_body else notify.clean_body(body)
        key = rec["sha256"]
        wait = MINUTE.wait(key, int(rec.get("rate_per_min", RATE_PER_MIN)), t)
        if wait:
            raise NotifyRefused("rate_limited", "too many notices this minute", 429, wait)
        today = int(t // _DAY)
        count = rec.get("day_count", 0) if rec.get("day") == today else 0
        if count >= int(rec.get("rate_per_day", RATE_PER_DAY)):
            raise NotifyRefused("rate_limited_day", "this token's notices for today are used", 429, (today + 1) * _DAY - t)
        hour = [x for x in rec.get("urgent_times", []) if isinstance(x, (int, float)) and t - x < 3600.0]
        if want == "urgent" and len(hour) >= int(rec.get("urgent_per_hour", URGENT_PER_HOUR)):
            raise NotifyRefused("rate_limited_urgent", "too many urgent notices this hour", 429, 3600.0 - (t - min(hour)))
        # the post is admitted: the counters and the stamp go to disk before the caller hears yes
        rec["day"], rec["day_count"], rec["last_used"] = today, count + 1, t
        rec["urgent_times"] = hour + ([t] if want == "urgent" else [])
        _write(queue_dir, recs)
        MINUTE.record(key, t)
        return Admitted(rec["name"], label, clean_title, clean_body, want, notify.kind_for(want), origin)

