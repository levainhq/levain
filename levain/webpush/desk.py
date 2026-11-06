"""levain.webpush.desk — presence that follows the operator, and an honest account of the desk.

Three records beside the presence word (all under ``<queue_dir>/webpush/``), plus the readings the chip paints from them:

- **A desk message** (:func:`note_desk_message`, ``desk_message.json``): the operator typed a message to a desk at the laptop.
  It is laptop input: it ends an ``until: input`` overlay (``away``, ``sleep``) at once, by READING (the intent record is never
  written here, :mod:`levain.webpush.intent`), and ``follow`` reads it: devices stay off until the newest one is ``follow_after``
  old (sensed keyboard or mouse input does not count). A turn in the Claude app's remote control is NOT a desk message: the caller decides, and it reports
  that as :func:`~levain.webpush.presence.note_remote_activity`. Every desk message writes a row to the presence log
  (``levain ping log``), which is the stream a coordinator wakes on.
- **Desk liveness** (:func:`publish_desk` / :func:`end_desk` / :func:`read_desk`, ``desk.json``): the desk's name and when it
  began, refreshed by whoever runs the desk. A record not refreshed for :data:`DESK_LIVE_TTL_SECS` reads ``stale``, never
  ``live``: a publisher that died must not leave "live" standing.
- **The Here / Away reading of the operator** (:func:`you_view`), derived from the laptop's input idle time (and a desk
  message): ``here`` while the last input is under ``follow_after`` old (the one threshold, ten minutes by default), ``away``
  after it, cleared by input. It is a display and an event stream: ``follow`` itself reads the typed desk message alone
  (:func:`levain.webpush.intent.devices_on_now`). :func:`observe` writes one presence-log row at each change (``auto-away`` / ``auto-here``) and
  one when it first sees an overlay ended (``ended away`` / ``ended sleep`` / ``ended quiet``): event rows, never a record write.
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


def adjust_reading(queue_dir: Path | str | None, reading: float | None, now: float, window: float | None = None) -> float | None:
    """The laptop's input idle seconds with a recent desk message counted as activity: the smaller of the two, when the
    message is under ``window`` seconds old (default :data:`ACTIVE_WINDOW_SECS`; the intent rule passes ``follow_after``).
    Older messages change nothing (so ``None``, no source, stays ``None``)."""
    msg = read_desk_message(queue_dir, now)
    if msg is None:
        return reading
    age = max(0.0, now - msg["at"])
    if age >= (ACTIVE_WINDOW_SECS if window is None else window):
        return reading
    return age if reading is None else min(reading, age)


def note_desk_message(queue_dir: Path | str, source: str, *, desk: str | None = None, now: float | None = None) -> dict:
    """The operator sent a message to a desk from the laptop. It is laptop input: it records the message as activity
    (:func:`adjust_reading`), ends an ``until: input`` overlay (``away``, ``sleep``) by READING and never by writing the intent
    record, and leaves one presence-log row. ``source`` names the caller (required). Returns ``{at, ended, cleared}``
    (``ended``: the preset it ended, ``"setting"`` for a setting made until his next typed message, else ``None``; ``cleared`` is the
    same value, for callers that read the old key). An older message than the one on file never replaces it. Raises
    :class:`PresenceError` for a missing source or a lock/file fault: the caller decides whether that matters."""
    src = str(source or "").strip()
    if not src:
        raise PresenceError("a desk message names its source")
    from levain.webpush import intent

    t = time.time() if now is None else now
    with presence._locked(queue_dir, strict=True, wait=_LOCK_WAIT_SECS):
        cur = read_desk_message(queue_dir, t)
        rec = intent.read(queue_dir, t)
        ov = rec["overlay"]
        newer = cur is None or t >= cur["at"]   # an older message than the one on file ends nothing
        seen = _read(queue_dir, AUTO_NAME) or {}
        ended = None
        if (newer and ov is not None and ov["until"]["kind"] == "input" and ov["ts"] < t and rec["status"] != "fault"
                and seen.get("ended") != _ended_key(ov)):
            ended = ov["preset"] or "setting"
        # the message record first: a fault writing it leaves the overlay standing, never one ended with no input recorded
        if newer:
            _write(queue_dir, DESK_MESSAGE_NAME, {"at": t, "source": src[:_NAME_MAX], "desk": str(desk or "")[:_NAME_MAX]})
        if ended:   # the end is noted once per overlay; the record itself stays exactly as it was
            _write(queue_dir, AUTO_NAME, {**seen, "ended": _ended_key(ov)})
        row = {"ts": t, "word": "desk-message", "prior": {"word": ov["preset"] or "set for a while"} if ended else {},
               "outcome": "set", "via": src[:_NAME_MAX],
               "by": {"agent": "operator", "session": str(desk or "")[:_NAME_MAX], "role": "desk"}}
        if ended:
            row["why"] = f"ended {ended}"
        presence._log_best_effort(queue_dir, row)
    if ended:   # pings that the ended overlay held are due now (a flush that cannot start is retried at the next call)
        _flush(queue_dir)
    return {"at": t, "ended": ended, "cleared": ended}


def _ended_key(ov: dict) -> str:
    """Which overlay an ``ended ...`` row was already written for (so each end is noted once)."""
    return f"{ov['preset'] or 'setting'}@{ov['ts']}"


def _flush(queue_dir: Path | str) -> None:
    try:
        from levain.webpush import held

        held.flush_if_due(queue_dir, throttle=False)
    except Exception:  # noqa: BLE001
        pass


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
def you_view(now: float, reading: float | None, word: dict | None = None, *, window: float | None = None) -> dict:
    """The operator's own reading, from the laptop's input idle seconds (already counting a desk message): ``{state, idle_secs,
    text, short}`` with ``state`` ``here`` (input under ``window`` ago: ``follow_after``, default :data:`ACTIVE_WINDOW_SECS`),
    ``away`` (longer), or ``unknown`` (no input source). ``away`` here is the MEASUREMENT; a standing overlay is shown by the
    chip as its preset."""
    limit = ACTIVE_WINDOW_SECS if window is None else window
    if reading is None:
        return {"state": "unknown", "idle_secs": None, "text": "Laptop: no input reading", "short": "no reading"}
    idle = max(0.0, reading)
    mins = int(idle // 60)
    if idle < limit:
        short = "active just now" if mins < 1 else f"active {mins} min ago"
        return {"state": "here", "idle_secs": idle, "text": f"Laptop: {short}", "short": short}
    return {"state": "away", "idle_secs": idle, "text": f"Laptop: no input for {mins} min", "short": f"idle {mins} min"}


def _mins_text(secs: float) -> str:
    m = secs / 60.0
    return f"{int(m)} min" if float(m).is_integer() else f"{m:.1f} min"


def observe(queue_dir: Path | str | None, now: float, reading: float | None, *, msg_at: float | None = None) -> str | None:
    """Write event rows for what sensing saw change, and nothing else (the intent record is never written here): one presence-log
    row when the measured reading crosses ``follow_after`` between ``here`` and ``away`` (``auto-away`` / ``auto-here``), and one
    ``ended away`` / ``ended sleep`` / ``ended quiet`` the first time it sees an overlay on file has ended, so a coordinator can
    wake on them. The first reading seen only seeds the state. An ``auto-away`` crossing, or an overlay just seen ended, starts
    the flush of held pings that are due. Best effort: a lock or file fault writes nothing and never raises. Returns the state
    read (``None`` for ``unknown``)."""
    if queue_dir is None:
        return None
    from levain.webpush import intent

    try:
        rec = intent.read(queue_dir, now)
        if msg_at is None:
            m = read_desk_message(queue_dir, now)
            msg_at = m["at"] if m else None
        eff, _ov, why = intent._eff(rec, now, reading, msg_at)
        window = eff["follow_after"]
        gone = rec["overlay"] if (why is not None and rec["overlay"] is not None) else None
    except Exception:  # noqa: BLE001 - an event row never blocks a read
        window, gone, why = ACTIVE_WINDOW_SECS, None, None
    state = you_view(now, reading, window=window)["state"]
    ret = None if state == "unknown" else state
    seen = _read(queue_dir, AUTO_NAME) or {}
    if (state == "unknown" or seen.get("state") == state) and (gone is None or seen.get("ended") == _ended_key(gone)):
        return ret   # nothing changed: no lock, no write (a delivery reads this on every ping)
    wake = False
    try:
        with presence._locked(queue_dir, strict=True, wait=_LOCK_WAIT_SECS):
            cur = _read(queue_dir, AUTO_NAME) or {}
            prev = cur.get("state")
            new = dict(cur)
            if state != "unknown" and prev != state:
                new.update({"state": state, "since": now})
                if prev in ("here", "away"):
                    presence._log_best_effort(queue_dir, {
                        "ts": now, "word": f"auto-{state}", "prior": {"word": f"auto-{prev}"}, "outcome": "set", "via": "auto",
                        "by": {"agent": "levain", "session": "", "role": "auto"},
                        "why": f"laptop input idle {int((reading or 0) // 60)} min against a {_mins_text(window)} follow threshold"})
                    wake = state == "away"
            if gone is not None and cur.get("ended") != _ended_key(gone):
                new["ended"] = _ended_key(gone)
                name = gone["preset"] or "setting"
                presence._log_best_effort(queue_dir, {
                    "ts": now, "word": f"ended {name}", "prior": {"word": name}, "outcome": "set", "via": "sensing",
                    "by": {"agent": "levain", "session": "", "role": "auto"}, "why": f"ended by {why}"})
                wake = True
            if new != cur:
                _write(queue_dir, AUTO_NAME, new)
    except (PresenceError, OSError):
        return ret
    if wake:
        _flush(queue_dir)
    return ret


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
