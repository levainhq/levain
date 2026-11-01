"""levain.webpush.presence — whether a ping is pushed to the phone now. One rule, no state precedence:

    push = not muted AND (urgent OR not_home OR desk inactive >= ACTIVE_WINDOW_SECS)        ... unless loud

That is the shipped default. While ``quiet`` or ``sleep`` is active the operator's policy cell for the ping's class
(``approval``, ``attention``, ``other``: :data:`KIND_CLASSES`) decides push or hold, kept in
``<queue_dir>/webpush/policy.json`` and set with ``levain ping policy`` (operator only); a ``push`` cell pushes while
that word is active even when the desk is active, a ``hold`` cell holds. With no policy file, or a malformed one, the
cells are :data:`DEFAULT_CELLS`, which are the rule above.

(Phill 10-25: *"++ yes that's the shape 100%"*.) A ping that is not pushed is still written to the notification record
(:func:`levain.webpush.sender.send` with ``push=False``); only the push to the phone is held, and a held ping waits in
the push store (:mod:`levain.webpush.held`) until the rule next says push.

Words, kept in ``<queue_dir>/webpush/presence.json`` (one word on file; the newest replaces the one before):

- ``quiet [duration]`` and ``sleep`` are the only holding words. ``quiet`` mutes for its duration (default
  :data:`DEFAULT_SECS`, at most :data:`MAX_SECS`); by default an urgent ping still pushes through it (the policy). ``sleep`` mutes until the
  operator's next input at this machine after saying it (input is read from the macOS HID idle time: he has typed since
  when ``now - idle`` is later than the word), urgent pings included by default (the policy). With no idle reading (not macOS, or the read
  failed) it cannot see his input, so it ends :data:`SLEEP_BLIND_SECS` after the word and ``levain ping status`` says the
  idle source is unavailable.
- ``loud [duration|off]`` turns pings on for its duration, over every other parameter, a mute included; only its timer,
  ``loud off`` or any other word written over it (``set_word`` keeps one word on file) ends it, never ``back``.
- ``away`` pushes at once until input at this machine more than :data:`AWAY_GRACE_SECS` after it (a line typed just
  after saying it is the parting instruction). ``back`` clears ``away``, ``quiet`` and ``sleep``, sets the route to ``desk``
  (:mod:`levain.webpush.route`) and leaves the measurement to decide. All optional overrides. (There was a ``remote`` word;
  it was dropped 11-01: the route, ``devices``, is how pings go to the phone, and a measured remote session is desk activity.
  A ``remote`` left on file by an older version reads as no word.)

Desk activity is the macOS HID idle time (:func:`levain.consent.auto_away.input_idle_seconds`): measured, never
predicted. With no such source, or no readable word, nothing is muted and the desk reads inactive: a ping he did not
need costs one notification, a ping he needed and never got costs the day. This host has no location signal, so
``not_home`` is the ``away`` word alone.

A word may carry who set it. A seat the operator told ("heading out") may set only :data:`SEAT_WORDS`, and the
coordinator only :data:`COORDINATOR_WORDS`; every other word, and a standing ``loud`` (no setter replaces it), stays his.
A setter's write is refused unless it names an agent and quotes his words, and the record keeps the setter and the quote
beside the word. A setter replaces a word the operator set himself only when its quote is found in what he typed
(:mod:`levain.webpush.quote`) after that word; any other quote sets over no word or another agent's word, marked
unverified. The setter's role comes from the caller's session
identity, which is a label under one uid and not a boundary: it records intent, it does not stop a process that hides
its environment.

The consent hook's own away mode (:mod:`levain.consent.away`) is a different setting (whether the hook holds a request
for a signed answer) and is not read or changed here.

Every call to :func:`set_word`, a refusal included, leaves one row in ``<queue_dir>/webpush/presence.log.jsonl``
(append-only, rotated to ``.1`` past 1 MiB; ``levain ping log``): who, in which role, the word, the quote, the
word it found, and the outcome.

Stdlib only.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import stat
import sys
import time
from contextlib import contextmanager
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

__all__ = ["ACTIVE_WINDOW_SECS", "AWAY_GRACE_SECS", "DEFAULT_SECS", "MAX_SECS", "SLEEP_BLIND_SECS", "URGENT_KINDS", "WORDS", "Decision", "KIND_CLASSES", "DEFAULT_CELLS", "kind_class",
           "read_policy", "set_policy", "reset_policy", "policy_sentences", "policy_view", "policy_is_default",
           "PresenceError", "SEAT_WORDS", "COORDINATOR_WORDS", "decide", "describe", "parse_duration", "is_expired", "log_line", "read_log", "read_word", "set_word", "should_push", "effective"]

ACTIVE_WINDOW_SECS = 5 * 60
AWAY_GRACE_SECS = 3 * 60
DEFAULT_SECS = 60 * 60
MAX_SECS = 24 * 3600
MIN_SECS = 60
SLEEP_BLIND_SECS = DEFAULT_SECS   # with no idle reading, `sleep` cannot see his next input: it ends this long after the word
WORDS = ("away", "back", "sleep", "quiet", "loud")
TIMED = ("quiet", "loud")
SEAT_WORDS = ("away",)
DROPPED_WORDS = {"remote": "the `remote` word was dropped: use the route, `devices`, to push pings to your phone"}
COORDINATOR_WORDS = SEAT_WORDS + ("quiet", "sleep")
ROLES = ("seat", "coordinator")
_SAID_MAX = 300
_FIELD_MAX = 200
_WHY_MAX = 300
LOG_NAME = "presence.log.jsonl"
LOCK_NAME = ".presence.lock"
LOG_MAX_BYTES = 1 << 20   # past this the log is rotated to ``LOG_NAME + ".1"``
# THE one kind -> class map (never configurable). ``approval``: a ping about a request that holds an agent until the
# operator answers: the hook's wait (``consent``, the default kind: the permission hook, the publish gate, the limits),
# the push gate's wait (``push``) and the asking-again of a request still pending (``reping``). ``attention``: the rows
# the coordinator raises to him (``attention``, C11 Q1 (c)) and a dated item (``dated``). ``other``: every other kind,
# unknown ones included (``question``, ``ruling``, ``summary``, the self-test ping the senders file as ``test``, and the
# kind ``describe`` probes the rule with, ``ping``). A posted notice (:mod:`levain.webpush.notify`) files as ``notify``
# (``other``) or, from a token the operator marked urgent, ``notify_urgent``, which SHARES the ``attention`` cell: the
# operator's table stays the one authority and no sender rings through a hold he set.
CLASSES = ("approval", "attention", "other")
KIND_CLASSES = {"consent": "approval", "push": "approval", "reping": "approval",
                "attention": "attention", "dated": "attention",
                "notify": "other", "notify_urgent": "attention"}
# urgent = approval or attention: derived from the map, not a second copy.
URGENT_KINDS = frozenset(k for k, c in KIND_CLASSES.items() if c in ("approval", "attention"))
MUTE_WORDS = ("quiet", "sleep")
# The shipped default policy is today's rule for every kind that existed before the policy: quiet lets approval and
# attention through, sleep holds all. ``dated`` is the one deliberate change: a kind unknown before is now class
# ``attention``, so it pushes at an active desk and through quiet, which it did not before.
DEFAULT_CELLS = {"quiet": {"approval": "push", "attention": "push", "other": "hold"},
                 "sleep": {"approval": "hold", "attention": "hold", "other": "hold"}}
POLICY_NAME = "policy.json"
POLICY_VERSION = 1
FILE_NAME = "presence.json"
_MAX_BYTES = 4096
_DURATION = re.compile(r"^(\d{1,4})\s*(m|min|mins|minutes?|h|hr|hrs|hours?)?$", re.I)


class PresenceError(ValueError):
    """A word or duration that cannot be applied, or a presence file that cannot be written safely."""


class PresenceBusy(PresenceError):
    """The presence lock was not free within the wait a caller allowed (``_locked(..., wait=...)``)."""


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
    if not agent:
        return {}
    # a record from before the quote check carries no ``verified``: it was never checked
    return {"by": {"agent": agent, "session": session, "role": by["role"]}, "said": said,
            "verified": got.get("verified") is True}


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


def kind_class(kind: object) -> str:
    """The class (``approval``, ``attention`` or ``other``) of a ping kind; an unknown kind is ``other``."""
    return KIND_CLASSES.get(kind, "other") if isinstance(kind, str) else "other"


_CLASS_WORDS = {"approval": "approvals", "attention": "attention items", "other": "other pings"}


def decide(now: float, word: dict | None, idle: float | None, *, urgent: bool = False, class_name: str | None = None,
           policy: dict | None = None, route: str = "desk") -> Decision:
    """:func:`_decide` without the source: whether a ping is pushed at ``now`` (the full docstring is there). Pure."""
    return _decide(now, word, idle, urgent=urgent, class_name=class_name, policy=policy, route=route)[0]


def _decide(now: float, word: dict | None, idle: float | None, *, urgent: bool = False, class_name: str | None = None,
            policy: dict | None = None, route: str = "desk") -> tuple[Decision, str]:
    """Whether a ping is pushed at ``now``. ``idle``: seconds since the operator's last input at this machine, ``None``
    when unknown. ``class_name`` is the ping's class (:func:`kind_class`); left unset, ``urgent`` stands for
    ``approval`` and not urgent for ``other``. ``policy`` is the cells (:func:`read_policy`), the shipped default when
    unset: while ``quiet`` or ``sleep`` is active the cell for (word, class) decides push or hold. ``route``
    (:mod:`levain.webpush.route`) is ``desk`` or ``devices``: ``devices`` pushes an ordinary ping even while the desk is
    active, after every mute and word above it. Returns ``(decision, source)``: ``word`` when a word on file (loud, a
    mute, away) decided, ``measured`` when the desk reading, the route or the ping's class decided. Pure."""
    if _active(word, "loud", now):
        return Decision(True, "pushing", "loud: pings on until its timer ends, over every other parameter"), "word"
    cls = class_name if class_name in CLASSES else ("approval" if urgent else "other")
    cells = policy if policy is not None else DEFAULT_CELLS
    seen = None if idle is None else now - max(0.0, idle)   # when he last did something here
    mute = None
    if word and word["word"] == "sleep" and (now < word["ts"] + SLEEP_BLIND_SECS if seen is None
                                             else not seen > word["ts"]):
        mute = "sleep"
    elif _active(word, "quiet", now):
        mute = "quiet"
    if mute is not None:
        value = cells[mute][cls]
        custom = value != DEFAULT_CELLS[mute][cls]
        if value == "push":
            return Decision(True, "pushing", f"{mute}: {_CLASS_WORDS[cls]} still reach you (your policy)" if custom else "urgent"), "word"
        if mute == "sleep":
            why = (f"sleep: {_CLASS_WORDS[cls]} are held (your policy)" if custom else
                   "sleep: nothing is pushed until his next input here, urgent pings included")
        else:
            why = (f"quiet: {_CLASS_WORDS[cls]} are held (your policy)" if custom else
                   "quiet: nothing but an urgent ping is pushed until it ends")
        return Decision(False, "muted", why, mute), "word"
    if cls != "other":
        return Decision(True, "pushing", "urgent"), "measured"
    if word and word["word"] == "away" and not (seen is not None and seen > word["ts"] + AWAY_GRACE_SECS):
        return Decision(True, "pushing", "away: his word"), "word"
    if route == "devices":
        return Decision(True, "pushing", "route: devices (his phone shows the band)"), "measured"
    if seen is None:
        return Decision(True, "pushing", "no desk activity source: read as inactive"), "measured"
    if idle is not None and idle >= ACTIVE_WINDOW_SECS:
        return Decision(True, "pushing", f"desk inactive {int(idle // 60)} min"), "measured"
    return Decision(False, "holding", f"desk active {int((idle or 0.0) // 60)} min ago"), "measured"


def _policy_path(queue_dir: Path | str) -> Path:
    return Path(queue_dir) / "webpush" / POLICY_NAME


def _copy_cells(cells: dict) -> dict:
    return {m: dict(c) for m, c in cells.items()}


def _read_policy(queue_dir: Path | str | None) -> tuple[dict, str, list[str]]:
    """``(cells, kind, reasons)``: ``kind`` is ``default``, ``loaded`` or ``malformed``; ``reasons`` is empty unless
    malformed. Any malformed reason returns the whole shipped default, never a partial file."""
    if queue_dir is None:
        return _copy_cells(DEFAULT_CELLS), "default", []

    def bad(*why: str) -> tuple[dict, str, list[str]]:
        return _copy_cells(DEFAULT_CELLS), "malformed", list(why)

    try:
        fd = os.open(_policy_path(queue_dir), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return _copy_cells(DEFAULT_CELLS), "default", []
    except OSError as e:
        return bad(f"unreadable ({type(e).__name__})")
    why = None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            why = "not a regular file"
        else:
            raw = os.read(fd, _MAX_BYTES + 1)
    except OSError as e:
        why = f"unreadable ({type(e).__name__})"
    try:
        os.close(fd)
    except OSError as e:  # a failed close is the malformed result, never an error out of a read that already happened
        why = why or f"unreadable ({type(e).__name__})"
    if why:
        return bad(why)
    if len(raw) > _MAX_BYTES:
        return bad("file too large")
    try:
        got = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return bad("not valid JSON")
    if not isinstance(got, dict) or got.get("version") != POLICY_VERSION or isinstance(got.get("version"), bool):
        return bad(f"version is not {POLICY_VERSION}")
    given = got.get("cells")
    if not isinstance(given, dict):
        return bad("no cells")
    cells: dict = {}
    invalid = []
    for m in MUTE_WORDS:
        row = given.get(m)
        cells[m] = {}
        for c in CLASSES:
            v = row.get(c) if isinstance(row, dict) else None
            if v in ("push", "hold"):
                cells[m][c] = v
            else:
                invalid.append(f"missing or invalid cell {m}.{c}")
    if invalid:
        return bad(*invalid)
    return cells, "loaded", []


def read_policy(queue_dir: Path | str | None) -> tuple[dict, str]:
    """``(cells, status)``: the six cells in effect and ``default`` (no file), ``loaded`` or ``malformed: <reasons>``.
    A file that is unreadable, a symlink, not regular, oversized, unparseable, of another version, without cells, or with
    any missing or invalid cell is malformed, and a malformed file is the WHOLE shipped default
    (:data:`DEFAULT_CELLS`), never a partial file: it never holds more than the default does."""
    cells, kind, reasons = _read_policy(queue_dir)
    if kind == "malformed":
        return cells, "malformed: " + "; ".join(reasons) + " (the whole default is in effect)"
    return cells, kind


def policy_is_default(cells: dict, status: str) -> bool:
    return status == "default" or (status == "loaded" and cells == DEFAULT_CELLS)


def policy_sentences(cells: dict) -> dict:
    """One plain sentence per mute word, from its cells."""
    out = {}
    for m in MUTE_WORDS:
        end = "until you are back" if m == "sleep" else "until it ends"
        through = [_CLASS_WORDS[c] for c in CLASSES if cells[m][c] == "push"]
        waits = [_CLASS_WORDS[c] for c in CLASSES if cells[m][c] == "hold"]
        if not through:
            out[m] = f"In {m}: nothing reaches you {end}"
        elif not waits:
            out[m] = f"In {m}: everything reaches you"
        else:
            out[m] = f"In {m}: {' and '.join(through)} reach you; {' and '.join(waits)} wait {end}"
    return out


def policy_view(queue_dir: Path | str | None) -> dict:
    """The stable dict ``levain ping policy show --json`` prints."""
    cells, kind, reasons = _read_policy(queue_dir)  # ONE read: cells, status, kind and reasons agree
    status = "malformed: " + "; ".join(reasons) + " (the whole default is in effect)" if kind == "malformed" else kind
    # `held_by_default`: an APPROVAL cell that holds only because the shipped default says so (no table of the operator's own
    # is in effect); the block says so in one plain line, where a hold of his own choosing gets the warning
    held = {f"{m}.approval": True for m in MUTE_WORDS
            if kind != "loaded" and cells[m]["approval"] == "hold" and DEFAULT_CELLS[m]["approval"] == "hold"}
    return {"version": POLICY_VERSION, "cells": cells, "classes": dict(sorted(KIND_CLASSES.items())),
            "default_class": "other", "status": status, "status_kind": kind, "reasons": reasons,
            "sentences": policy_sentences(cells), "default_cells": _copy_cells(DEFAULT_CELLS), "held_by_default": held}


def _policy_log(queue_dir: Path | str, ts: float, word: str, by: object, outcome: str, why: str, strict: bool,
                prior: dict | None = None, via: str | None = None) -> None:
    row: dict = {"ts": ts, "word": _clean(word, 60), "prior": dict(prior) if prior else {}, "outcome": outcome}
    if via:     # the credential's name for a write through the cockpit (never the secret); identity stays ``by``
        row["via"] = _clean(via, _FIELD_MAX)
    if by is not None:
        who = by if isinstance(by, dict) else {}
        row["by"] = {"agent": _clean(who.get("agent"), _FIELD_MAX), "session": _clean(who.get("session"), _FIELD_MAX),
                     "role": _clean(who.get("role"), 20)}
    if why:
        row["why"] = _clean(why, _WHY_MAX)
    (_log if strict else _log_best_effort)(queue_dir, row)


def _write_policy(queue_dir: Path | str, cells: dict | None) -> None:
    path = _policy_path(queue_dir)
    d = path.parent
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise PresenceError(f"{d} is not a directory of this user's")
        if cells is None:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            return
        tmp = d / f".{POLICY_NAME}.{os.getpid()}.{time.time_ns()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            try:
                view = memoryview(json.dumps({"version": POLICY_VERSION, "cells": cells}).encode("utf-8"))
                while view:
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
        raise PresenceError(f"policy not written: {e}") from e


def _refuse_setter(queue_dir: Path | str, ts: float, word: str, by: dict | None) -> None:
    """The policy is the operator's alone, like his own presence words: a caller carrying an agent identity (a seat or
    the coordinator) is refused, and the refusal is one row in the presence log. The role is a label under one uid, not
    a boundary: it records intent, it does not stop a process that hides its environment."""
    if by is None:
        return
    who = "the coordinator" if isinstance(by, dict) and by.get("role") == "coordinator" else "a seat"
    msg = (f"the notification policy is the operator's own: {who} may not change it "
           "(the operator sets it himself, from a terminal outside any agent session)")
    with _locked(queue_dir, strict=False):
        _policy_log(queue_dir, ts, word, by, "refused", msg, strict=False)
    raise PresenceError(msg)


def set_policy(queue_dir: Path | str, mute: str, cls: str, value: str, *, by: dict | None = None,
               now: float | None = None, via: str | None = None, wait: float | None = None) -> dict:
    """Set one cell and return the cells after. Operator only (``by`` must be ``None``, see :func:`_refuse_setter`).
    Atomic write under the presence lock; one presence-log row. Raises :class:`PresenceError`; ``wait`` (seconds) bounds the
    lock wait and raises :class:`PresenceBusy` past it (default: wait as long as it takes, as the CLI does)."""
    ts = time.time() if now is None else now
    word = f"policy set {mute} {cls} {value}"
    _refuse_setter(queue_dir, ts, word, by)
    if mute not in MUTE_WORDS or cls not in CLASSES or value not in ("push", "hold"):
        raise PresenceError(f"policy set takes <{'|'.join(MUTE_WORDS)}> <{'|'.join(CLASSES)}> <push|hold>")
    with _locked(queue_dir, strict=True, wait=wait):  # a policy mutation never runs unlocked: two sets could lose one
        cells, _status = read_policy(queue_dir)
        prior = _status if _status.startswith("malformed") else ""
        was = cells[mute][cls]
        cells[mute][cls] = value
        try:
            _write_policy(queue_dir, cells)
        except PresenceError as e:
            _policy_log(queue_dir, ts, word, None, "refused", str(e), strict=False, via=via)
            raise
        _policy_log(queue_dir, ts, word, None, "set" if was != value else "unchanged",
                    f"prior file {prior.replace(' (the whole default is in effect)', '')}; rewritten from the default" if prior else "",
                    strict=False, prior={f"{mute}.{cls}": was}, via=via)
    return cells


def reset_policy(queue_dir: Path | str, *, by: dict | None = None, now: float | None = None, via: str | None = None,
                 wait: float | None = None) -> dict:
    """Remove the policy file (back to the shipped default) and return the default cells. Operator only."""
    ts = time.time() if now is None else now
    _refuse_setter(queue_dir, ts, "policy reset", by)
    with _locked(queue_dir, strict=True, wait=wait):
        before, _st = read_policy(queue_dir)
        try:
            _write_policy(queue_dir, None)
        except PresenceError as e:
            _policy_log(queue_dir, ts, "policy reset", None, "refused", str(e), strict=False, via=via)
            raise
        _policy_log(queue_dir, ts, "policy reset", None, "set", "", strict=False,
                    prior={f"{m}.{c}": before[m][c] for m in MUTE_WORDS for c in CLASSES}, via=via)
    return _copy_cells(DEFAULT_CELLS)


_UNSET = object()   # "read it yourself", distinct from None (no word on file / no idle reading)


def _core(queue_dir: Path | str | None, kind: str, now: float | None, idle: Callable[[], float | None] | None,
          suggest: bool, word: object = _UNSET, reading: object = _UNSET) -> tuple[Decision, str, dict | None]:
    """The one place the rule is read: ``(decision, source, word)`` for a ``kind`` of ping at ``now``. Both
    :func:`should_push` and :func:`effective` call it, so the push and the chip cannot diverge. Raises on a fault; the
    callers read that as push. ``word`` and ``reading`` let a caller that already sampled the word and the idle time have
    this rule work from that same snapshot (one read of each per status), not a second code path."""
    from levain.webpush import route as _route

    at = time.time() if now is None else now
    if reading is _UNSET:
        if idle is None:
            from levain.consent.auto_away import input_idle_seconds as idle
        reading = idle()
    if suggest:   # delivery drives the suggestion (throttled there); a status read (``suggest=False``) never does
        try:
            _route.auto_suggest(queue_dir, idle=reading, now=at)
        except Exception:  # noqa: BLE001 - its fault never changes the rule
            pass
    if word is _UNSET:
        word = read_word(queue_dir)
    # a delivery (suggest) reads the route after auto_suggest; a status read projects the same route without writing
    r = (_route.read_route(queue_dir, at)[0] if suggest else _route.projected_route(queue_dir, idle=reading, now=at))
    d, source = _decide(at, word, reading, class_name=kind_class(kind), policy=read_policy(queue_dir)[0], route=r)
    return d, source, word


_FAULT = "the presence rule could not be read: pushing"


def should_push(queue_dir: Path | str | None, kind: str, *, now: float | None = None,
                idle: Callable[[], float | None] | None = None, suggest: bool = True,
                word: object = _UNSET) -> Decision:
    """:func:`decide` for a ``kind`` of ping against the word on file and the input idle time (``idle`` defaults to
    :func:`levain.consent.auto_away.input_idle_seconds`). Any fault reads as push: a missed ping is the worse error."""
    try:
        return _core(queue_dir, kind, now, idle, suggest, word)[0]
    except Exception:  # noqa: BLE001 - the rule failing must not silence the phone
        return Decision(True, "pushing", _FAULT)


def effective(queue_dir: Path | str | None, now: float | None = None, *,
              idle: Callable[[], float | None] | None = None, suggest: bool = False,
              word: object = _UNSET, reading: object = _UNSET) -> dict:
    """What the rule does now with an ordinary ping, for the cockpit chip and any status read:
    ``{state: pushing|holding|muted, pushing, reason, source: word|measured, since}``. It is the same call
    :func:`should_push` makes (:func:`_core`, kind ``ping``), so the chip and the push decision for the same instant are
    one answer; the route axis is folded in as there. ``source`` is ``word`` when a word on file (loud, away, quiet, sleep)
    decided, ``measured`` when the desk reading or the route decided; ``since`` is that word's time, else ``None``.
    ``suggest`` defaults off: a status read never drives the route suggestion, and projects the route a delivery would see
    (:func:`levain.webpush.route.projected_route`). ``word`` and ``reading`` (the word record and the idle seconds) let a caller
    that sampled them once get an answer from that same snapshot. ``word`` in the result names the deciding word, else ``None``.
    A fault reads as pushing."""
    try:
        d, source, wrec = _core(queue_dir, "ping", now, idle, suggest, word, reading)
    except Exception:  # noqa: BLE001 - the same fault rule as should_push
        return {"state": "pushing", "pushing": True, "reason": _FAULT, "source": "measured", "since": None,
                "word": None}
    decided = wrec if source == "word" and wrec is not None else None
    return {"state": d.state, "pushing": d.push, "reason": d.why, "source": source,
            "since": decided["ts"] if decided else None, "word": decided["word"] if decided else None}


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
             by: dict | None = None, said: str | None = None, match: object = None,
             via: str | None = None, wait: float | None = None,
             precheck: "Callable[[], None] | None" = None, after: "Callable[[], None] | None" = None) -> dict | None:
    """Write ``word`` (``duration`` for quiet and loud; ``loud off`` clears a loud). Returns what is on file
    after, ``None`` when no word stands. ``back`` leaves a standing ``loud`` alone and returns it. Raises
    :class:`PresenceError` for an unknown word, a bad duration, or a directory or file that is not safe to write.
    ``by`` (a seat's or the coordinator's identity, see :func:`check_setter`) and ``said`` (his words) are for an agent
    setting a word for him: both are checked before anything is read or written, and recorded with the word. With
    ``by`` unset the call is the operator's own and records nothing more. ``match`` (a
    :class:`levain.webpush.quote.Match`) says whether the quote is in what he typed; with none, it is unverified.
    A setter replaces a word he set himself only on a verified quote he typed after that word (:func:`_plan`).

    Every call leaves one row in the presence log (:func:`read_log`), a refusal included. A setter's row is written
    before the word and a row that cannot be written refuses the write; the operator's own row is written after, and a
    failure to write it is reported on stderr and never stops his word.

    The whole call (read the word on file, plan, log row, write, outcome row) runs under one interprocess lock, so a
    word set meanwhile is read, never overwritten. A setter that cannot take the lock is refused; the operator's own
    word goes through without it.

    ``via`` names the credential an HTTP write came in on (never its secret); it is recorded on the operator's row.
    ``wait`` (seconds) is for a caller that must not park a thread behind a stalled lock holder: the lock is then
    required (a failure to take it refuses the write, :class:`PresenceBusy` past the wait) instead of skipped.

    ``precheck`` runs once the lock is held and before the word on file is read: a :class:`PresenceError` it raises
    refuses the call (logged as a refusal) with nothing written, so a credential it re-checks and a revoke that takes
    the same lock have one order. Passing one requires the lock. ``after`` runs under the same hold once the word is
    written; it must not take the presence lock itself."""
    strict = by is not None or wait is not None or precheck is not None
    with _locked(queue_dir, strict=strict, wait=wait):
        if precheck is not None:
            try:
                precheck()
            except PresenceError as e:
                ts = time.time() if now is None else now
                row = _log_row(ts, word, duration, by, said, read_word(queue_dir))
                if via:
                    row["via"] = _clean(via, _FIELD_MAX)
                _log_best_effort(queue_dir, {**row, "outcome": "refused", "why": _clean(str(e), _WHY_MAX)})
                raise
        out = _set_word(queue_dir, word, duration, now, by, said, match, via)
        if after is not None:
            after()
        return out


@contextmanager
def _locked(queue_dir: Path | str, *, strict: bool, wait: float | None = None):
    """Hold the presence lock (a ``flock`` on a lock file beside ``presence.json``). ``strict``: failing to take it
    raises :class:`PresenceError`; else it is reported on stderr and the body runs unlocked. ``wait`` (seconds, with ``strict``):
    take the lock by non-blocking attempts up to that deadline, then raise :class:`PresenceBusy`; ``None`` blocks."""
    d = Path(queue_dir) / "webpush"
    fd = None
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise PresenceError(f"{d} is not a directory of this user's")
        fd = os.open(d / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise PresenceError(f"{d / LOCK_NAME} is not a file of this user's")
        if wait is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise PresenceBusy("the presence lock is held by another process") from None
                    time.sleep(0.05)
    except (OSError, PresenceError) as e:
        if fd is not None:
            os.close(fd)
        fd = None
        if strict and isinstance(e, PresenceBusy):
            raise
        if strict:
            raise PresenceError(f"presence lock not taken: {e}") from e
        print(f"levain ping: presence lock not taken: {e}", file=sys.stderr)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)


def _set_word(queue_dir: Path | str, word: str, duration: str | None, now: float | None, by: dict | None,
              said: str | None, match: object, via: str | None = None) -> dict | None:
    ts = time.time() if now is None else now
    prior = read_word(queue_dir)
    row = _log_row(ts, word, duration, by, said, prior)
    if via:
        row["via"] = _clean(via, _FIELD_MAX)
    ev = _evidence(match) if by is not None else None
    if ev is not None:
        row["verified"] = ev[0]
        if ev[1] is not None:
            row["said_at"] = ev[1]
    try:
        action, rec, why = _plan(word, duration, ts, prior, by, said, ev)
    except PresenceError as e:
        _log_best_effort(queue_dir, {**row, "outcome": "refused", "why": _clean(str(e), _WHY_MAX)})
        raise
    row["outcome"] = "set" if action != "keep" else "unchanged"
    if rec is not None and "until" in rec:
        row["until"] = rec["until"]
    if why:
        row["why"] = why
    if by is not None:
        try:
            _log(queue_dir, row)
        except PresenceError as e:
            raise PresenceError(f"{e}: a setter's write with no record is not made") from e
    try:
        if action == "write":
            _write(queue_dir, rec)
        elif action == "clear":
            _write(queue_dir, None)
    except PresenceError as e:
        _log_best_effort(queue_dir, {**row, "outcome": "refused", "why": _clean(str(e), _WHY_MAX)})
        raise
    # The word has landed; the route follows it. There is no rollback of the word: a route that cannot be written is
    # reported to the caller, who sees the word set and the route unchanged, and taps again.
    try:
        _follow_route(queue_dir, word, duration, action, ts)
    except PresenceError as e:
        done = "cleared" if action == "clear" else "set"
        _log_best_effort(queue_dir, {**row, "outcome": done, "why": _clean(f"route not changed: {e}", _WHY_MAX)})
        raise PresenceError(f"{word} is {done}, but the route was not changed ({e}); tap it again") from e
    if by is None:
        _log_best_effort(queue_dir, row)
    return rec if action == "write" else (None if action == "clear" else prior)


def _follow_route(queue_dir: Path | str, word: str, duration: str | None, action: str, ts: float) -> None:
    """``back`` is present + desk; the caller holds the presence lock. Runs after the word is written; a route that
    cannot be written raises and the caller says so."""
    if not (word == "back" and action == "clear"):
        return
    from levain.webpush import route

    # present at the desk: the standing route record goes, and a later remote login can still suggest devices
    route._write(queue_dir, None)


def _evidence(match: object) -> tuple[bool, float | None, str]:
    """``(verified, said_at, why)`` from a :class:`levain.webpush.quote.Match`; no match is unverified."""
    verified = getattr(match, "verified", False) is True
    at = getattr(match, "said_at", None)
    at = float(at) if verified and isinstance(at, (int, float)) and not isinstance(at, bool) and math.isfinite(at) else None
    why = _clean(getattr(match, "why", "") or "no transcript check ran", _WHY_MAX)
    return verified and at is not None, at, why


def _plan(word: str, duration: str | None, ts: float, prior: dict | None, by: dict | None,
          said: str | None, ev: tuple[bool, float | None, str] | None = None) -> tuple[str, dict | None, str]:
    """What a call does, with nothing read or written: ``(action, record, note)``, action ``write`` (the record is the
    word), ``clear`` (no word stands after) or ``keep`` (the word on file stays, the note says why). Raises
    :class:`PresenceError` for a refusal."""
    if word not in WORDS:
        hint = f"; {DROPPED_WORDS[word]}" if word in DROPPED_WORDS else ""
        raise PresenceError(f"{word!r} is not a presence word (one of: {', '.join(WORDS)}){hint}")
    quote = check_setter(word, by, said) if by is not None else None
    if by is not None and _active(prior, "loud", ts):
        raise PresenceError("`loud` is the operator's standing word; a seat cannot replace it "
                            "(the operator says `loud off` himself)")
    note = ""
    if by is not None:
        ev = ev or (False, None, "no transcript check ran")
        if prior is not None and "by" not in prior and not is_expired(prior, ts):   # his own standing word
            at = time.strftime("%H:%M", time.localtime(prior["ts"]))
            if not ev[0]:
                raise PresenceError(f"he set `{prior['word']}` himself at {at} and your quote is not in your transcript "
                                    f"({ev[2]}); he ends it himself")
            if ev[1] <= prior["ts"]:
                raise PresenceError(f"he set `{prior['word']}` himself at {at}, after the sentence you quote; "
                                    "he ends it himself")
        elif not ev[0]:
            note = f"unverified: {ev[2]}"
    if word == "loud" and (duration or "").strip().lower() == "off":
        if _active(prior, "loud", ts) or (prior and prior["word"] == "loud"):
            return "clear", None, "loud off"
        return "keep", None, "loud off: no loud was standing"
    if word == "back" and _active(prior, "loud", ts):
        return "keep", None, "back leaves a standing loud"
    if word == "back":
        return "clear", None, ""
    if word not in TIMED and duration:
        raise PresenceError(f"`{word}` takes no duration")
    rec: dict = {"word": word, "ts": ts}
    if word in TIMED:
        rec["until"] = ts + parse_duration(duration)
    if quote is not None:
        rec["by"] = {"agent": _clean(by.get("agent"), _FIELD_MAX), "session": _clean(by.get("session"), _FIELD_MAX),
                     "role": by["role"]}
        rec["said"] = quote
        rec["verified"] = ev[0]
    return "write", rec, note


def _log_row(ts: float, word: object, duration: str | None, by: object, said: object, prior: dict | None) -> dict:
    """The row's fields known before the call is decided: who, which role, the word given, the quote, the word it found."""
    row: dict = {"ts": ts, "word": _clean(word, 40), "prior": {"word": prior["word"]} if prior else {}}
    if prior and "by" in prior:
        row["prior"]["by"] = prior["by"]
    if _clean(duration, 40):
        row["duration"] = _clean(duration, 40)
    if by is not None:
        who = by if isinstance(by, dict) else {}
        row["by"] = {"agent": _clean(who.get("agent"), _FIELD_MAX), "session": _clean(who.get("session"), _FIELD_MAX),
                     "role": _clean(who.get("role"), 20)}
        row["said"] = _clean(said, _SAID_MAX)
    return row


def _log_best_effort(queue_dir: Path | str, row: dict) -> None:
    try:
        _log(queue_dir, row)
    except Exception as e:  # noqa: BLE001 - the operator's word, and the refusal being raised, are not blocked by a row
        print(f"levain ping: presence log not written: {e}", file=sys.stderr)


def _log(queue_dir: Path | str, row: dict) -> None:
    """Append one row to the presence log (the caller holds the presence lock). Raises :class:`PresenceError` when it
    cannot be written whole. A log that passes :data:`LOG_MAX_BYTES` is rotated: it becomes ``LOG_NAME + ".1"``,
    replacing the one before, and the next row starts a new file."""
    d = Path(queue_dir) / "webpush"
    path = d / LOG_NAME
    line = (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise PresenceError(f"{d} is not a directory of this user's")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                raise PresenceError(f"{path} is not a file of this user's")
            view = memoryview(line)
            while view:   # a short write is not a row
                n = os.write(fd, view)
                if n <= 0:
                    raise OSError("short write")
                view = view[n:]
            size = st.st_size + len(line)
        finally:
            os.close(fd)
        if size > LOG_MAX_BYTES:
            os.replace(path, d / (LOG_NAME + ".1"))
    except OSError as e:
        raise PresenceError(f"presence log not written: {e}") from e


def log_line(r: dict) -> str:
    """One log row as a line: when, outcome, the word, who set it and on what quote, and what it found or why not."""
    by = r.get("by")
    who = "operator" if not isinstance(by, dict) else f"{by.get('role', '')} {by.get('agent', '')} {by.get('session', '')}".strip()
    prior = r.get("prior") if isinstance(r.get("prior"), dict) else {}
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r['ts']))} {str(r['outcome']).upper()} {r.get('word', '')}"
    if r.get("duration"):
        line += f" {r['duration']}"
    line += f" by {who}"
    if r.get("said"):
        line += f' on "{r["said"]}"' + (" (unverified)" if r.get("verified") is False else "")
    line += f" (found: {prior.get('word', 'no word')})"
    return line + (f": {r['why']}" if r.get("why") else "")


def _read_tail(path: Path) -> list[dict]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return []
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return []
        start = max(0, st.st_size - 2 * LOG_MAX_BYTES)
        raw = os.pread(fd, st.st_size - start, start)
    finally:
        os.close(fd)
    lines = raw.splitlines()
    if start:
        lines = lines[1:]   # the first line is cut
    rows = []
    for ln in lines:
        try:
            r = json.loads(ln.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            continue
        ts = r.get("ts") if isinstance(r, dict) else None
        if (isinstance(ts, (int, float)) and not isinstance(ts, bool) and math.isfinite(ts) and 0 <= ts < 1e11
                and r.get("outcome") in ("set", "refused", "unchanged")):
            rows.append(r)
    return rows


def read_log(queue_dir: Path | str | None, *, limit: int | None = None) -> list[dict]:
    """The presence log, oldest first: the rotated file, then the live one (the newest ``limit`` rows when given). A
    missing or unreadable file is no rows, and a line that is not a row is skipped."""
    if queue_dir is None:
        return []
    d = Path(queue_dir) / "webpush"
    rows = _read_tail(d / (LOG_NAME + ".1")) + _read_tail(d / LOG_NAME)
    return rows if limit is None else rows[-limit:] if limit > 0 else []


def is_expired(word: dict | None, now: float) -> bool:
    """Whether ``word`` is a timed word (quiet, loud) whose time has run out."""
    return bool(word and word["word"] in TIMED and not _active(word, word["word"], now))


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
                while view:   # a short write would rename a half word into place
                    n = os.write(fd, view)
                    if n <= 0:
                        raise OSError("short write")
                    view = view[n:]
            finally:
                os.close(fd)
            os.replace(tmp, path)
        except BaseException:
            try:   # a failed write leaves no orphan temp file behind; the word in place is untouched
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
    except OSError as e:
        raise PresenceError(f"presence word not written: {e}") from e


def describe(queue_dir: Path | str, *, now: float | None = None,
             idle: Callable[[], float | None] | None = None, word: object = _UNSET, reading: object = _UNSET) -> str:
    """One line for ``levain ping status``: the word on file, what the rule does now with an ordinary ping, and how many
    pings are held for the phone."""
    ts = time.time() if now is None else now
    if word is _UNSET:
        word = read_word(queue_dir)
    if reading is _UNSET:
        try:
            if idle is None:
                from levain.consent.auto_away import input_idle_seconds as idle
            reading = idle()
        except Exception:  # noqa: BLE001 - no reading: the rule reads the desk as inactive
            reading = None
    d = should_push(queue_dir, "ping", now=ts, idle=lambda: reading, suggest=False, word=word)
    if word is not None and word["word"] == "sleep" and reading is None and ts < word["ts"] + SLEEP_BLIND_SECS:
        end = time.strftime("%H:%M", time.localtime(word["ts"] + SLEEP_BLIND_SECS))
        d = Decision(d.push, d.state, f"{d.why}; idle source unavailable, so sleep ends at {end}", d.by)
    on_file = "no word" if word is None else (word["word"] if "until" not in word else
                                              f"{word['word']} until {time.strftime('%H:%M', time.localtime(word['until']))}")
    from levain.webpush import held

    n = held.count(queue_dir)
    line = f"{d.state.upper()}: {d.why} (word: {on_file}; {n} held for the phone)"
    cells, pstatus = read_policy(queue_dir)
    if not policy_is_default(cells, pstatus):
        line += f"; notification policy: {pstatus if pstatus != 'loaded' else 'yours'} ({' / '.join(policy_sentences(cells).values())})"
    if word is not None and "by" in word:
        by = word["by"]
        who = f"{by['agent']} {by['session']}".strip()
        flag = " (unverified)" if word.get("verified") is not True else ""
        line += f"; set by {'the coordinator ' if by['role'] == 'coordinator' else ''}{who} from your words: {word['said']}{flag}"
    return line
