"""levain.webpush.desk — presence that follows the operator, and an honest account of the desk.

Three records beside the presence word (all under ``<queue_dir>/webpush/``), plus the readings the chip paints from them:

- **A desk message** (:func:`note_desk_message`, ``desk_message.json``): the operator typed a message to a desk at the laptop.
  It ends an ``away`` or ``sleep`` word at once (never ``quiet`` or ``loud``: those are his own standing choices) and counts as
  activity for :data:`~levain.webpush.presence.ACTIVE_WINDOW_SECS`, so a long message that is being composed holds pings the
  way keyboard input does. A turn in the Claude app's remote control is NOT a desk message: the caller decides, and it reports
  that as :func:`~levain.webpush.presence.note_remote_activity`. Every desk message writes a row to the presence log
  (``levain ping log``), which is the stream a coordinator wakes on.
- **Desk liveness** (:func:`publish_desk` / :func:`end_desk` / :func:`read_desk`, ``desk.json``): the desk's name and when it
  began, refreshed by whoever runs the desk. A record not refreshed for :data:`DESK_LIVE_TTL_SECS` reads ``stale``, never
  ``live``: a publisher that died must not leave "live" standing.
- **The Here / Away reading of the operator** (:func:`you_view`), derived from the laptop's input idle time (and a desk
  message): ``here`` while the last input is under :data:`~levain.webpush.presence.ACTIVE_WINDOW_SECS` old, ``away``
  after it, cleared by input. :func:`observe` writes one presence-log row at each change (``auto-away`` / ``auto-here``).
  The word "desk" is never used for this reading: it names the desk, and the operator is "you".

:func:`record_decision` keeps one row per delivery decision (``push_decisions.jsonl``: kind, push, reason, idle, word) so the
window can be fitted from what was measured. Stdlib only.
"""
from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

from levain.webpush import presence
from levain.webpush.presence import ACTIVE_WINDOW_SECS, PresenceError

__all__ = ["DESK_LIVE_TTL_SECS", "note_desk_message", "read_desk_message", "adjust_reading", "publish_desk", "end_desk",
           "read_desk", "you_view", "observe", "record_decision"]

DESK_LIVE_TTL_SECS = 10 * 60
DESK_MESSAGE_NAME = "desk_message.json"
DESK_NAME = "desk.json"
AUTO_NAME = "presence_auto.json"
DECISIONS_NAME = "push_decisions.jsonl"
DECISIONS_MAX_BYTES = 1 << 20
_MAX_BYTES = 2048
_SKEW_SECS = 60.0
_LOCK_WAIT_SECS = 0.5
_NAME_MAX = 80


def _clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _at(v: object) -> float | None:
    """A time in epoch seconds: a number (not a bool) with 0 < v < 1e11, else ``None``; nothing here raises."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        return float(v) if 0 < v < 1e11 else None
    except (OverflowError, ValueError, TypeError):
        return None


def _read(queue_dir: Path | str | None, name: str) -> dict | None:
    """The JSON object on file, or ``None`` for a file that is missing, a symlink, not regular, oversized or unparseable."""
    if queue_dir is None:
        return None
    try:
        fd = os.open(Path(queue_dir) / "webpush" / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = os.read(fd, _MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(raw) > _MAX_BYTES:
        return None
    try:
        got = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError, ArithmeticError):
        return None
    return got if isinstance(got, dict) else None


def _write(queue_dir: Path | str, name: str, rec: dict) -> None:
    d = Path(queue_dir) / "webpush"
    os.makedirs(d, mode=0o700, exist_ok=True)
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise PresenceError(f"{d} is not a directory of this user's")
    tmp = d / f".{name}.{os.getpid()}.{time.time_ns()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        try:
            view = memoryview(json.dumps(rec).encode("utf-8"))
            while view:
                n = os.write(fd, view)
                if n <= 0:
                    raise OSError("short write")
                view = view[n:]
        finally:
            os.close(fd)
        os.replace(tmp, d / name)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


# ------------------------------------------------------------------------------------------------- the desk message
def read_desk_message(queue_dir: Path | str | None, now: float | None = None) -> dict | None:
    """``{at, source, desk}`` for the newest desk message, or ``None`` (none on file, unreadable, or dated more than a
    minute ahead of ``now``)."""
    got = _read(queue_dir, DESK_MESSAGE_NAME)
    at = _at(got.get("at")) if got else None
    if at is None:
        return None
    t = time.time() if now is None else now
    if at - t > _SKEW_SECS:
        return None
    return {"at": at, "source": str(got.get("source") or "")[:_NAME_MAX], "desk": str(got.get("desk") or "")[:_NAME_MAX]}


def adjust_reading(queue_dir: Path | str | None, reading: float | None, now: float) -> float | None:
    """The laptop's input idle seconds with a recent desk message counted as activity: the smaller of the two, when the
    message is under :data:`ACTIVE_WINDOW_SECS` old. Older messages change nothing (so ``None``, no source, stays ``None``)."""
    msg = read_desk_message(queue_dir, now)
    if msg is None:
        return reading
    age = max(0.0, now - msg["at"])
    if age >= ACTIVE_WINDOW_SECS:
        return reading
    return age if reading is None else min(reading, age)


def note_desk_message(queue_dir: Path | str, source: str, *, desk: str | None = None, now: float | None = None) -> dict:
    """The operator sent a message to a desk from the laptop. Ends an ``away`` or ``sleep`` word (never ``quiet`` or
    ``loud``), records the message as activity (:func:`adjust_reading`) and leaves one presence-log row. ``source`` names the
    caller (required). Returns ``{at, cleared}`` (``cleared``: the word it ended, or ``None``). An older message than the one
    on file never replaces it. Raises :class:`PresenceError` for a missing source or a lock/file fault: the caller decides
    whether that matters (a fault changes no word)."""
    src = str(source or "").strip()
    if not src:
        raise PresenceError("a desk message names its source")
    t = time.time() if now is None else now
    with presence._locked(queue_dir, strict=True, wait=_LOCK_WAIT_SECS):
        cur = read_desk_message(queue_dir, t)
        word = presence.read_word(queue_dir)
        cleared = word["word"] if word and word["word"] in ("away", "sleep") else None
        # the message record first: a fault writing it leaves the word standing, never a word gone with no input recorded
        if cur is None or t >= cur["at"]:
            _write(queue_dir, DESK_MESSAGE_NAME, {"at": t, "source": src[:_NAME_MAX], "desk": str(desk or "")[:_NAME_MAX]})
        if cleared and (cur is None or t >= cur["at"]):   # an older message than the one on file ends no word
            presence._write(queue_dir, None)
        else:
            cleared = None
        row = {"ts": t, "word": "desk-message", "prior": {"word": word["word"]} if word else {}, "outcome": "set",
               "via": src[:_NAME_MAX], "by": {"agent": "operator", "session": str(desk or "")[:_NAME_MAX], "role": "desk"}}
        if cleared:
            row["why"] = f"ended {cleared}"
        presence._log_best_effort(queue_dir, row)
    if cleared:   # pings that the ended word held are due now, as after `back` (a flush that cannot start is retried at the next call)
        try:
            from levain.webpush import held

            held.flush_if_due(queue_dir, throttle=False)
        except Exception:  # noqa: BLE001
            pass
    return {"at": t, "cleared": cleared}


# ------------------------------------------------------------------------------------------------- desk liveness
def publish_desk(queue_dir: Path | str, name: str, since: float | None = None, *, now: float | None = None) -> bool:
    """Say that the desk ``name`` is running. Call it again to refresh: a record not refreshed for
    :data:`DESK_LIVE_TTL_SECS` reads ``stale``. ``since`` is when this desk began (a repeat call for the same live desk keeps
    the first one). Returns whether it was written; a bad name or a lock/file fault returns ``False`` and never raises into a poll."""
    nm = str(name or "").strip()[:_NAME_MAX]
    t = time.time() if now is None else now
    if not nm:
        return False
    begun = _at(since) if since is not None else None
    try:
        with presence._locked(queue_dir, strict=True, wait=_LOCK_WAIT_SECS):
            cur = _read(queue_dir, DESK_NAME) or {}
            same_live = cur.get("name") == nm and cur.get("ended") is None and _at(cur.get("since")) is not None
            first = _at(cur.get("since")) if same_live else None
            rec = {"name": nm, "since": first or begun or t, "beat": t, "ended": None}
            if not same_live and cur.get("ended") is not None:
                rec["last_ended"] = cur.get("ended")
            elif same_live and cur.get("last_ended") is not None:
                rec["last_ended"] = cur.get("last_ended")
            _write(queue_dir, DESK_NAME, rec)
        return True
    except (PresenceError, OSError):
        return False


def end_desk(queue_dir: Path | str, name: str | None = None, *, now: float | None = None) -> bool:
    """The desk stopped. With ``name``, only that desk's own record ends (an older desk closing after its successor
    published leaves the successor live). Returns whether a record was ended; never raises into a caller."""
    t = time.time() if now is None else now
    try:
        with presence._locked(queue_dir, strict=True, wait=_LOCK_WAIT_SECS):
            cur = _read(queue_dir, DESK_NAME)
            if not cur or cur.get("ended") is not None or (name and cur.get("name") != name):
                return False
            _write(queue_dir, DESK_NAME, {**cur, "ended": t})
        return True
    except (PresenceError, OSError):
        return False


def read_desk(queue_dir: Path | str | None, now: float | None = None) -> dict:
    """What the desk record says at ``now``: ``{live, stale, name, since, beat, ended, text}``. ``live`` only while the record
    is refreshed (under :data:`DESK_LIVE_TTL_SECS`); an unrefreshed one is ``stale`` and its text says when it was last heard.
    With no record, or one that ended, no desk is running."""
    t = time.time() if now is None else now
    cur = _read(queue_dir, DESK_NAME) or {}
    nm = str(cur.get("name") or "")[:_NAME_MAX] or None
    beat, since, ended = _at(cur.get("beat")), _at(cur.get("since")), _at(cur.get("ended"))
    last = ended or _at(cur.get("last_ended"))
    out = {"live": False, "stale": False, "name": nm, "since": since, "beat": beat, "ended": ended}
    if nm and beat is not None and ended is None:
        if t - beat <= DESK_LIVE_TTL_SECS:
            out["live"] = True
            out["text"] = f"Desk: {nm} live" + (f" since {_clock(since)}" if since else "")
        else:
            out["stale"] = True
            out["text"] = f"Desk: {nm} not heard since {_clock(beat)}"
        return out
    out["text"] = f"No desk running (last ended {_clock(last)})" if last else "No desk running"
    return out


# --------------------------------------------------------------------------------------- the operator: here or away
def you_view(now: float, reading: float | None, word: dict | None = None) -> dict:
    """The operator's own reading, from the laptop's input idle seconds (already counting a desk message): ``{state, idle_secs,
    text, short}`` with ``state`` ``here`` (input under :data:`ACTIVE_WINDOW_SECS` ago), ``away`` (longer), or ``unknown`` (no input
    source). ``away`` here is the MEASUREMENT; a standing word is shown by the chip as the word."""
    if reading is None:
        return {"state": "unknown", "idle_secs": None, "text": "You: no laptop input reading", "short": "no reading"}
    idle = max(0.0, reading)
    mins = int(idle // 60)
    if idle < ACTIVE_WINDOW_SECS:
        short = "active just now" if mins < 1 else f"active {mins} min ago"
        return {"state": "here", "idle_secs": idle, "text": f"You: {short}", "short": short}
    return {"state": "away", "idle_secs": idle, "text": f"You: no input at the laptop for {mins} min", "short": f"idle {mins} min"}


def observe(queue_dir: Path | str | None, now: float, reading: float | None) -> str | None:
    """Write one presence-log row when the measured reading changes between ``here`` and ``away`` (``auto-away`` /
    ``auto-here``), so a coordinator can wake on it; the first reading seen only seeds the state. Best effort: a lock or file
    fault writes nothing and never raises. Returns the state read (``None`` for ``unknown``)."""
    state = you_view(now, reading)["state"]
    if queue_dir is None or state == "unknown":
        return None
    seen = _read(queue_dir, AUTO_NAME)
    if seen and seen.get("state") == state:   # nothing changed: no lock, no write (a delivery reads this on every ping)
        return state
    try:
        with presence._locked(queue_dir, strict=True, wait=_LOCK_WAIT_SECS):
            cur = _read(queue_dir, AUTO_NAME) or {}
            prev = cur.get("state")
            if prev == state:
                return state
            _write(queue_dir, AUTO_NAME, {"state": state, "since": now})
            if prev in ("here", "away"):
                presence._log_best_effort(queue_dir, {
                    "ts": now, "word": f"auto-{state}", "prior": {"word": f"auto-{prev}"}, "outcome": "set", "via": "auto",
                    "by": {"agent": "levain", "session": "", "role": "auto"},
                    "why": f"laptop input idle {int((reading or 0) // 60)} min against a {int(ACTIVE_WINDOW_SECS // 60)} min window"})
    except (PresenceError, OSError):
        return state
    return state


# ---------------------------------------------------------------------------------------- one row per decision
def record_decision(queue_dir: Path | str | None, now: float, kind: str, push: bool, state: str, why: str, source: str,
                    reading: float | None, word: dict | None) -> None:
    """Append what a delivery decision saw and said. Best effort: never raises, never blocks a push."""
    if queue_dir is None:
        return
    row = {"ts": now, "kind": str(kind)[:40], "push": bool(push), "state": state, "why": str(why)[:300], "source": source,
           "idle": None if reading is None else round(reading, 1), "word": word["word"] if word else None}
    try:
        d = Path(queue_dir) / "webpush"
        os.makedirs(d, mode=0o700, exist_ok=True)
        path = d / DECISIONS_NAME
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)   # NONBLOCK: a pipe in its place fails, never hangs a push
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                return
            line = (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8")
            os.write(fd, line)
            size = st.st_size + len(line)
        finally:
            os.close(fd)
        if size > DECISIONS_MAX_BYTES:
            os.replace(path, d / (DECISIONS_NAME + ".1"))
    except OSError:
        pass
