"""levain.webpush.presence — whether a ping is pushed to the phone now. One rule, no state precedence:

    push = not muted AND (urgent OR not_home OR desk inactive >= ACTIVE_WINDOW_SECS)        ... unless loud

(Phill 10-25: *"++ yes that's the shape 100%"*.) A ping that is not pushed is still written to the notification record
(:func:`levain.webpush.sender.send` with ``push=False``); only the push to the phone is held, and a held ping waits in
the push store (:mod:`levain.webpush.held`) until the rule next says push.

Words, kept in ``<queue_dir>/webpush/presence.json`` (one word on file; the newest replaces the one before):

- ``quiet [duration]`` and ``sleep`` are the only holding words. ``quiet`` mutes for its duration (default
  :data:`DEFAULT_SECS`, at most :data:`MAX_SECS`); an urgent ping still pushes through it. ``sleep`` mutes until the
  operator's next input at this machine after saying it (input is read from the macOS HID idle time: he has typed since
  when ``now - idle`` is later than the word), urgent pings included. With no idle reading (not macOS, or the read
  failed) it cannot see his input, so it ends :data:`SLEEP_BLIND_SECS` after the word and ``levain ping status`` says the
  idle source is unavailable.
- ``loud [duration|off]`` turns pings on for its duration, over every other parameter, a mute included; only its timer,
  ``loud off`` or any other word written over it (``set_word`` keeps one word on file) ends it, never ``back``.
- ``away`` pushes at once until input at this machine more than :data:`AWAY_GRACE_SECS` after it (a line typed just
  after saying it is the parting instruction). ``remote [duration]`` pushes for its duration. ``back`` clears
  ``away``, ``remote``, ``quiet`` and ``sleep``, and leaves the measurement to decide. All optional overrides.

Desk activity is the macOS HID idle time (:func:`levain.consent.auto_away.input_idle_seconds`): measured, never
predicted. With no such source, or no readable word, nothing is muted and the desk reads inactive: a ping he did not
need costs one notification, a ping he needed and never got costs the day. This host has no location signal, so
``not_home`` is the ``away`` word alone.

A word may carry who set it. A seat the operator told ("heading out") may set only :data:`SEAT_WORDS`, and the
coordinator only :data:`COORDINATOR_WORDS`; every other word, and a standing ``loud`` (no setter replaces it), stays his.
A setter's write is refused unless it names an agent and quotes his words, and the record keeps the setter and the quote
beside the word. The setter's role comes from the caller's session
identity, which is a label under one uid and not a boundary: it records intent, it does not stop a process that hides
its environment.

The consent hook's own away mode (:mod:`levain.consent.away`) is a different setting (whether the hook holds a request
for a signed answer) and is not read or changed here.

Stdlib only.
"""
from __future__ import annotations

import json
import math
import os
import re
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

__all__ = ["ACTIVE_WINDOW_SECS", "AWAY_GRACE_SECS", "DEFAULT_SECS", "MAX_SECS", "SLEEP_BLIND_SECS", "URGENT_KINDS", "WORDS", "Decision",
           "PresenceError", "SEAT_WORDS", "COORDINATOR_WORDS", "decide", "describe", "parse_duration", "read_word", "set_word", "should_push"]

ACTIVE_WINDOW_SECS = 5 * 60
AWAY_GRACE_SECS = 3 * 60
DEFAULT_SECS = 60 * 60
MAX_SECS = 24 * 3600
MIN_SECS = 60
SLEEP_BLIND_SECS = DEFAULT_SECS   # with no idle reading, `sleep` cannot see his next input: it ends this long after the word
WORDS = ("away", "back", "sleep", "quiet", "loud", "remote")
TIMED = ("quiet", "loud", "remote")
SEAT_WORDS = ("away", "remote")
COORDINATOR_WORDS = SEAT_WORDS + ("quiet", "sleep")
ROLES = ("seat", "coordinator")
_SAID_MAX = 300
_FIELD_MAX = 200
# urgent = a ping about a request that holds an agent until the operator answers: the hook's wait (``consent``, the
# default kind: the permission hook, the publish gate, the limits), the push gate's wait (``push``) and the asking-again
# of a request still pending (``reping``). Every other kind (``question``, ``ruling``, ``summary``) waits for the rule.
URGENT_KINDS = frozenset({"consent", "push", "reping"})
FILE_NAME = "presence.json"
_MAX_BYTES = 4096
_DURATION = re.compile(r"^(\d{1,4})\s*(m|min|mins|minutes?|h|hr|hrs|hours?)?$", re.I)


class PresenceError(ValueError):
    """A word or duration that cannot be applied, or a presence file that cannot be written safely."""


@dataclass(frozen=True)
class Decision:
    push: bool
    state: str   # "muted", "pushing" or "holding"
    why: str
    by: str = ""   # the word that muted it ("quiet" or "sleep"), when state is "muted"


def parse_duration(text: str | None) -> float:
    """Seconds for ``30``, ``30m``, ``2h`` (bare numbers are minutes); empty is :data:`DEFAULT_SECS`. Cut to
    [:data:`MIN_SECS`, :data:`MAX_SECS`]: a mute that never ends is a silent outage. Raises :class:`PresenceError`."""
    t = (text or "").strip()
    if not t:
        return float(DEFAULT_SECS)
    m = _DURATION.match(t)
    if m is None:
        raise PresenceError(f"{text!r} is not a duration: use 30, 30m or 2h")
    secs = int(m.group(1)) * (3600 if (m.group(2) or "m").lower().startswith("h") else 60)
    return float(max(MIN_SECS, min(secs, MAX_SECS)))


def _path(queue_dir: Path | str) -> Path:
    return Path(queue_dir) / "webpush" / FILE_NAME


def _clean(text: object, limit: int) -> str:
    """``text`` as one printable line of at most ``limit`` characters; ``""`` for anything that is not text."""
    if not isinstance(text, str):
        return ""
    return " ".join("".join(c if c.isprintable() else " " for c in text).split())[:limit]


def _provenance(got: dict) -> dict:
    """The ``by`` and ``said`` a record carries, cleaned, or nothing when either is missing or malformed: a record is
    shown as set by a seat only when it names the seat and quotes his words."""
    by, said = got.get("by"), _clean(got.get("said"), _SAID_MAX)
    if not isinstance(by, dict) or not said or by.get("role") not in ROLES:
        return {}
    agent, session = _clean(by.get("agent"), _FIELD_MAX), _clean(by.get("session"), _FIELD_MAX)
    return {"by": {"agent": agent, "session": session, "role": by["role"]}, "said": said} if agent else {}


def _valid(got: object) -> dict | None:
    if not isinstance(got, dict) or got.get("word") not in WORDS:
        return None
    ts = got.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts):
        return None
    if got["word"] in TIMED:
        until = got.get("until")
        if isinstance(until, bool) or not isinstance(until, (int, float)) or not math.isfinite(until):
            until = ts + DEFAULT_SECS
        return {"word": got["word"], "ts": float(ts), "until": float(min(until, ts + MAX_SECS)), **_provenance(got)}
    return {"word": got["word"], "ts": float(ts), **_provenance(got)}


def read_word(queue_dir: Path | str | None) -> dict | None:
    """The word on file, or ``None``. A file that is missing, a symlink, not regular, oversized, unparseable or naming
    a word this version does not know (an older or newer writer's) is no word: the measurement then decides."""
    if queue_dir is None:
        return None
    try:
        fd = os.open(_path(queue_dir), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = os.read(fd, _MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > _MAX_BYTES:
        return None
    try:
        return _valid(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None


def _active(word: dict | None, name: str, now: float) -> bool:
    return bool(word and word["word"] == name and now < word["until"])


def decide(now: float, word: dict | None, idle: float | None, *, urgent: bool = False) -> Decision:
    """Whether a ping is pushed at ``now``. ``idle``: seconds since the operator's last input at this machine, ``None``
    when unknown. Pure."""
    if _active(word, "loud", now):
        return Decision(True, "pushing", "loud: pings on until its timer ends, over every other parameter")
    seen = None if idle is None else now - max(0.0, idle)   # when he last did something here
    if word and word["word"] == "sleep" and (now < word["ts"] + SLEEP_BLIND_SECS if seen is None
                                             else not seen > word["ts"]):
        return Decision(False, "muted", "sleep: nothing is pushed until his next input here, urgent pings included", "sleep")
    if urgent:
        return Decision(True, "pushing", "urgent")
    if _active(word, "quiet", now):
        return Decision(False, "muted", "quiet: nothing but an urgent ping is pushed until it ends", "quiet")
    if word and word["word"] == "away" and not (seen is not None and seen > word["ts"] + AWAY_GRACE_SECS):
        return Decision(True, "pushing", "away: his word")
    if _active(word, "remote", now):
        return Decision(True, "pushing", "remote: his word")
    if seen is None:
        return Decision(True, "pushing", "no desk activity source: read as inactive")
    if idle is not None and idle >= ACTIVE_WINDOW_SECS:
        return Decision(True, "pushing", f"desk inactive {int(idle // 60)} min")
    return Decision(False, "holding", f"desk active {int((idle or 0.0) // 60)} min ago")


def should_push(queue_dir: Path | str | None, kind: str, *, now: float | None = None,
                idle: Callable[[], float | None] | None = None) -> Decision:
    """:func:`decide` for a ``kind`` of ping against the word on file and the input idle time (``idle`` defaults to
    :func:`levain.consent.auto_away.input_idle_seconds`). Any fault reads as push: a missed ping is the worse error."""
    try:
        if idle is None:
            from levain.consent.auto_away import input_idle_seconds as idle
        return decide(time.time() if now is None else now, read_word(queue_dir), idle(), urgent=kind in URGENT_KINDS)
    except Exception:  # noqa: BLE001 - the rule failing must not silence the phone
        return Decision(True, "pushing", "the presence rule could not be read: pushing")


def check_setter(word: str, by: dict | None, said: str | None) -> str:
    """The quote to record for a setter's write, or a raised :class:`PresenceError` saying why it is refused. ``by``
    is ``{"agent", "session", "role"}`` with role ``seat`` or ``coordinator``. Pure: a refusal writes nothing."""
    if not isinstance(by, dict):
        raise PresenceError("the setter names no agent")
    role = by.get("role")
    if role not in ROLES:
        raise PresenceError("the setter's role is neither seat nor coordinator")
    if not _clean(by.get("agent"), _FIELD_MAX):   # what `_provenance` requires on read, so a word written here is shown as set
        raise PresenceError("the setter names no agent")
    allowed = COORDINATOR_WORDS if role == "coordinator" else SEAT_WORDS
    if word not in allowed:
        who = "the coordinator" if role == "coordinator" else "a seat"
        raise PresenceError(f"`{word}` is the operator's own word: {who} may set only {', '.join(f'`{w}`' for w in allowed)}"
                            " (the operator says it himself; from a terminal outside any agent session the operator sets any word)")
    raw = said if isinstance(said, str) else ""
    if len(raw) > _SAID_MAX:
        raise PresenceError(f"--said is too long: quote the sentence he said, at most {_SAID_MAX} characters")
    quote = _clean(raw, _SAID_MAX)
    if not quote:
        raise PresenceError(f"a {role} sets `{word}` only on his words: pass --said \"<what he told you>\" "
                            "(no words, no change)")
    return quote


def set_word(queue_dir: Path | str, word: str, *, duration: str | None = None, now: float | None = None,
             by: dict | None = None, said: str | None = None) -> dict | None:
    """Write ``word`` (``duration`` for quiet, loud and remote; ``loud off`` clears a loud). Returns what is on file
    after, ``None`` when no word stands. ``back`` leaves a standing ``loud`` alone and returns it. Raises
    :class:`PresenceError` for an unknown word, a bad duration, or a directory or file that is not safe to write.
    ``by`` (a seat's or the coordinator's identity, see :func:`check_setter`) and ``said`` (his words) are for an agent
    setting a word for him: both are checked before anything is read or written, and recorded with the word. With
    ``by`` unset the call is the operator's own and records nothing more."""
    if word not in WORDS:
        raise PresenceError(f"{word!r} is not a presence word (one of: {', '.join(WORDS)})")
    quote = check_setter(word, by, said) if by is not None else None
    ts = time.time() if now is None else now
    prior = read_word(queue_dir)
    if by is not None and _active(prior, "loud", ts):
        raise PresenceError("`loud` is the operator's standing word; a seat cannot replace it "
                            "(the operator says `loud off` himself)")
    if word == "loud" and (duration or "").strip().lower() == "off":
        if _active(prior, "loud", ts) or (prior and prior["word"] == "loud"):
            _write(queue_dir, None)
            return None
        return prior
    if word == "back" and _active(prior, "loud", ts):
        return prior
    if word == "back":
        _write(queue_dir, None)
        return None
    if word not in TIMED and duration:
        raise PresenceError(f"`{word}` takes no duration")
    rec: dict = {"word": word, "ts": ts}
    if word in TIMED:
        rec["until"] = ts + parse_duration(duration)
    if quote is not None:
        rec["by"] = {"agent": _clean(by.get("agent"), _FIELD_MAX), "session": _clean(by.get("session"), _FIELD_MAX),
                     "role": by["role"]}
        rec["said"] = quote
    _write(queue_dir, rec)
    return rec


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
            os.write(fd, json.dumps(rec).encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except OSError as e:
        raise PresenceError(f"presence word not written: {e}") from e


def describe(queue_dir: Path | str, *, now: float | None = None,
             idle: Callable[[], float | None] | None = None) -> str:
    """One line for ``levain ping status``: the word on file, what the rule does now with an ordinary ping, and how many
    pings are held for the phone."""
    ts = time.time() if now is None else now
    word = read_word(queue_dir)
    try:
        if idle is None:
            from levain.consent.auto_away import input_idle_seconds as idle
        reading = idle()
    except Exception:  # noqa: BLE001 - no reading: the rule reads the desk as inactive
        reading = None
    d = should_push(queue_dir, "ping", now=ts, idle=lambda: reading)
    if word is not None and word["word"] == "sleep" and reading is None and ts < word["ts"] + SLEEP_BLIND_SECS:
        end = time.strftime("%H:%M", time.localtime(word["ts"] + SLEEP_BLIND_SECS))
        d = Decision(d.push, d.state, f"{d.why}; idle source unavailable, so sleep ends at {end}", d.by)
    on_file = "no word" if word is None else (word["word"] if "until" not in word else
                                              f"{word['word']} until {time.strftime('%H:%M', time.localtime(word['until']))}")
    from levain.webpush import held

    n = held.count(queue_dir)
    line = f"{d.state.upper()}: {d.why} (word: {on_file}; {n} held for the phone)"
    if word is not None and "by" in word:
        by = word["by"]
        who = f"{by['agent']} {by['session']}".strip()
        line += f"; set by {'the coordinator ' if by['role'] == 'coordinator' else ''}{who} from your words: {word['said']}"
    return line
