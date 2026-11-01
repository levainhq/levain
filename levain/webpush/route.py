"""levain.webpush.route — where an ordinary ping goes while the operator is active at the laptop: ``desk`` or ``devices``.

The presence word says whether he is reachable; the route says which surface shows him the band. ``desk`` holds a
non-urgent ping for the terminal band while he is active here (the shipped rule). ``devices`` pushes it to the phone and
cockpit even while he is active, because the surface that shows him the band is his phone. :func:`levain.webpush.presence.decide`
takes the route as a pure input; this module keeps it on file and suggests it.

``<queue>/webpush/route.json`` is ``{"route", "ts", "until", "source"}``; ``source`` is ``operator`` (he said it) or ``auto``
(a remote login was seen). No file reads as ``desk``. An ended ``until`` reads as ``desk``. A file that cannot be read
safely (a symlink, not regular, oversized, unparseable, naming a route this version does not know) reads as ``devices``:
the presence rule's own bias, any fault reads as push, since a ping he did not need costs one notification and a ping
he needed and never got costs the day.

An explicit operator set always beats an auto one: ``desk`` said by him is itself a record (source ``operator``, with an
end), so the suggestion below cannot flip it back while it stands. An auto set never replaces an unexpired operator record.

Detection is deterministic and reads no model: a login session from another host (:func:`remote_login_present`) while the
laptop's own keyboard and mouse have been idle (:func:`auto_suggest`).

Stdlib only.
"""
from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

from levain.launch import child_env
from levain.webpush.presence import (MAX_SECS, PresenceError, _FIELD_MAX, _clean, _locked, _log_best_effort,
                                     parse_duration)

__all__ = ["ROUTES", "ROUTE_WORDS", "AUTO_IDLE_SECS", "read_route", "set_route", "apply_route", "remote_login_present",
           "auto_suggest"]

ROUTES = ("desk", "devices")
ROUTE_WORDS = ROUTES   # the words the HTTP set takes for the route alone
FILE_NAME = "route.json"
AUTO_IDLE_SECS = 120.0   # the laptop's own input idle at least this long: the keyboard in use is not the one he is at
_MAX_BYTES = 1024
_NUM_LIMIT = 1e15
AUTO_THROTTLE_SECS = 30.0   # the live path runs `who` at most this often; every ping would otherwise shell out
STAMP_NAME = "route.auto.stamp"
_WHO = "/usr/bin/who"
_WHO_TIMEOUT_SECS = 2.0
_HOSTED = re.compile(r"\(([^()\s]+)\)\s*$")


def _path(queue_dir: Path | str) -> Path:
    return Path(queue_dir) / "webpush" / FILE_NAME


def _stamp_path(queue_dir: Path | str) -> Path:
    return Path(queue_dir) / "webpush" / STAMP_NAME


def _throttled(queue_dir: Path | str, now: float) -> bool:
    """Whether the last `who` run was under the window ago; then it is left alone, else the stamp is rewritten (before the
    `who` call, so a slow or failed run still counts). The stamp is a file because the caller is a fresh process per ping.
    A stamp that cannot be read safely, or lies in the future, means run."""
    path = _stamp_path(queue_dir)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("not regular")
            last = float(os.read(fd, 64).decode("ascii").strip())
        finally:
            os.close(fd)
        if _num(last) and 0 <= now - last < AUTO_THROTTLE_SECS:
            return True
    except (OSError, ValueError):
        pass
    try:
        d = path.parent
        os.makedirs(d, mode=0o700, exist_ok=True)
        tmp = d / f".{STAMP_NAME}.{os.getpid()}.{time.time_ns()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, repr(float(now)).encode("ascii"))
        finally:
            os.close(fd)
        try:
            os.replace(tmp, path)
        except OSError:
            os.unlink(tmp)
    except OSError:
        pass   # no stamp is no throttle; the next call runs
    return False


def _num(v: object) -> bool:
    # compared, never passed to isfinite: an int of hundreds of digits overflows that call; nan and inf fail the compare
    return not isinstance(v, bool) and isinstance(v, (int, float)) and abs(v) < _NUM_LIMIT


def _load(queue_dir: Path | str | None) -> dict | None:
    """The record on file, ``None`` for no file. Raises :class:`ValueError` for a file that cannot be read safely."""
    if queue_dir is None:
        return None
    try:
        fd = os.open(_path(queue_dir), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ValueError(f"unreadable ({type(e).__name__})") from e
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("not a regular file")
        raw = os.read(fd, _MAX_BYTES + 1)
    except OSError as e:
        raise ValueError(f"unreadable ({type(e).__name__})") from e
    finally:
        os.close(fd)
    if len(raw) > _MAX_BYTES:
        raise ValueError("file too large")
    try:
        got = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        raise ValueError("not valid JSON") from e
    if not isinstance(got, dict) or got.get("route") not in ROUTES or not _num(got.get("ts")):
        raise ValueError("not a route record")
    until = got.get("until")
    if until is not None and not _num(until):
        raise ValueError("bad until")
    if until is None and got["route"] == "desk":
        raise ValueError("a desk record always ends")   # an endless desk would be an operator override nobody set
    if got.get("source") not in ("operator", "auto"):
        raise ValueError("unknown source")   # never promoted to an operator record
    source = got["source"]
    return {"route": got["route"], "ts": float(got["ts"]), "source": source,
            "until": None if until is None else float(min(until, got["ts"] + MAX_SECS))}


def read_route(queue_dir: Path | str | None, now: float | None = None) -> tuple[str, str, float | None]:
    """``(route, source, until)``. ``source`` is ``operator``, ``auto``, ``default`` (no record, or one that ended) or
    ``fault`` (a file that cannot be read safely, which reads as ``devices``)."""
    t = time.time() if now is None else now
    try:
        rec = _load(queue_dir)
    except (ValueError, ArithmeticError):
        return "devices", "fault", None
    if rec is None or (rec["until"] is not None and t >= rec["until"]):
        return "desk", "default", None
    return rec["route"], rec["source"], rec["until"]


def _write(queue_dir: Path | str, rec: dict | None) -> None:
    path = _path(queue_dir)
    d = path.parent
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise PresenceError(f"{d} is not a directory of this user's")
        if rec is None:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            return
        tmp = d / f".{FILE_NAME}.{os.getpid()}.{time.time_ns()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            try:
                view = memoryview(json.dumps(rec).encode("utf-8"))
                while view:   # a short write would rename a half record into place
                    n = os.write(fd, view)
                    if n <= 0:
                        raise OSError("short write")
                    view = view[n:]
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
        raise PresenceError(f"route not written: {e}") from e


def apply_route(queue_dir: Path | str, route: str, duration: str | None = None, *, source: str = "operator",
                now: float | None = None) -> bool:
    """:func:`set_route` for a caller ALREADY holding the presence lock. Whether the record was written (an auto set that
    an unexpired operator record outranks writes nothing)."""
    if route not in ROUTES:
        raise PresenceError(f"{route!r} is not a route (one of: {', '.join(ROUTES)})")
    if source not in ("operator", "auto"):
        raise PresenceError(f"{source!r} is not a route source")
    ts = time.time() if now is None else now
    # an operator's Desk outlasts the hour an auto suggestion stands for, or it would be silently replaced
    secs = float(MAX_SECS) if (route == "desk" and source == "operator" and not duration) else parse_duration(duration)
    if source == "auto":
        try:
            cur = _load(queue_dir)
        except ValueError:
            cur = None   # a record that cannot be read outranks nothing
        if cur is not None and cur["source"] == "operator" and (cur["until"] is None or ts < cur["until"]):
            return False
    _write(queue_dir, {"route": route, "ts": ts, "until": ts + secs, "source": source})
    return True


def set_route(queue_dir: Path | str, route: str, duration: str | None = None, *, source: str = "operator",
              now: float | None = None, via: str | None = None, wait: float | None = None,
              precheck=None, after=None) -> bool:
    """Set the route under the presence lock; ``duration`` as the timed words take it (default one hour, at most a day).
    ``desk`` said by the operator is a record too, so an auto suggestion cannot undo it before it ends. ``wait``,
    ``precheck`` and ``after`` are :func:`levain.webpush.presence.set_word`'s, with the same meaning: a tap path's
    credential is re-checked and stamped inside one hold of the lock. Raises :class:`PresenceError`."""
    ts = time.time() if now is None else now
    strict = wait is not None or precheck is not None or source == "auto"
    with _locked(queue_dir, strict=strict, wait=wait):
        if precheck is not None:
            precheck()
        wrote = apply_route(queue_dir, route, duration, source=source, now=ts)
        row = {"ts": ts, "word": route, "prior": {}, "outcome": "set" if wrote else "unchanged"}
        if via:
            row["via"] = _clean(via, _FIELD_MAX)
        if source == "auto":
            row["why"] = "auto: a remote login is open and the laptop's input is idle"
        _log_best_effort(queue_dir, row)
        if after is not None:
            after()
    if wrote and route == "devices":   # outside the lock: pings held as "active" are due now
        try:
            from levain.webpush import held

            held.flush_if_due(queue_dir, throttle=False)
        except Exception:  # noqa: BLE001 - a failed flush never fails the set
            pass
    return wrote


def remote_login_present(who_output: str) -> bool:
    """Whether ``who`` output lists a login from another host: a line whose tty is not ``console`` and that ends with a
    parenthesised host (an ssh or mosh session lists as ``user ttysNNN date (host)``). A local Terminal tab has no host and
    does not count; neither does a display name such as ``(:0)`` or a loopback host."""
    for line in (who_output or "").splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[1] == "console":
            continue
        m = _HOSTED.search(line)
        if m and not _local_host(m.group(1)):
            return True
    return False


def _local_host(host: str) -> bool:
    # parsed, never prefix-matched: "127.evil.example" and "fe80:" lookalikes are names, while 127.0.0.2 is loopback too
    if host.startswith(":") and not host.startswith("::"):
        return True   # a display such as :0
    h = host.strip("[]").split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return h.lower() == "localhost"
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_link_local


def _run_who() -> str:
    try:
        out = subprocess.run([_WHO], capture_output=True, text=True, timeout=_WHO_TIMEOUT_SECS, check=False,
                             stdin=subprocess.DEVNULL, env=child_env())
        return out.stdout if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""   # no reading is no remote login


def auto_suggest(queue_dir: Path | str, *, idle: float | None, who: str | None = None, now: float | None = None,
                 clock=time.time) -> bool:
    """Set ``devices`` (source ``auto``, one hour) when a remote login is open, the laptop's own input has been idle at
    least :data:`AUTO_IDLE_SECS` (``idle`` seconds, ``None`` when unknown, which suggests nothing) and no route is
    standing: neither an operator's unexpired record nor a ``devices`` already in force. Returns whether it wrote.
    An ``auto`` record ends as soon as the idle reads below the threshold (he is back at the keyboard). Called from the
    delivery decision, so with ``who`` unset it runs at most once per :data:`AUTO_THROTTLE_SECS` per queue by ``clock``
    (wall clock, injectable; the stamp is a file under the queue); a caller giving ``who`` is not throttled."""
    if idle is None or not _num(idle):
        return False
    ts = time.time() if now is None else now
    if idle < AUTO_IDLE_SECS:
        _end_auto(queue_dir, ts)   # before the throttle: the keys back ends an auto route at once
        return False
    if who is None and _throttled(queue_dir, clock()):
        return False
    if not remote_login_present(_run_who() if who is None else who):
        return False
    try:
        with _locked(queue_dir, strict=True, wait=0.5):
            route, _source, _until = read_route(queue_dir, ts)
            if route == "devices":
                return False
            wrote = apply_route(queue_dir, "devices", None, source="auto", now=ts)
            if wrote:
                _log_best_effort(queue_dir, {"ts": ts, "word": "devices", "prior": {}, "outcome": "set",
                                             "why": "auto: a remote login is open and the laptop's input is idle"})
    except PresenceError as e:
        print(f"levain route: auto suggestion skipped: {e}", file=sys.stderr)
        return False
    if wrote:   # outside the lock: pings held as "active" are due now that the route is devices
        from levain.webpush import held

        held.flush_if_due(queue_dir, throttle=False)
    return wrote


def _end_auto(queue_dir: Path | str, ts: float) -> None:
    if read_route(queue_dir, ts)[1] != "auto":
        return
    try:
        with _locked(queue_dir, strict=True, wait=0.5):
            if read_route(queue_dir, ts)[1] == "auto":
                _write(queue_dir, None)
                _log_best_effort(queue_dir, {"ts": ts, "word": "desk", "prior": {}, "outcome": "set",
                                             "why": "auto route ended: input at the laptop again"})
    except PresenceError as e:
        print(f"levain route: auto route not ended: {e}", file=sys.stderr)
