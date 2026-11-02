"""levain.webpush.presence — whether a ping is pushed to the phone now, and the words that set it.

Since presence 1b (Phill 11-02) the rule lives in :mod:`levain.webpush.intent`: ONE record (``intent.json``) with a standing
``base`` and an ``overlay`` (a preset or a setting for a while), each carrying ``devices`` (on / off / follow), ``follow_after``
(seconds; default 600) and ``lets_through`` (everything / needs / approvals / nothing). Sensing (laptop idle, remote input, a
desk message, desk liveness) is shown and feeds only ``follow`` and ``until: input``; it never rewrites the record. This module
keeps what the record shares with its callers:

- the vocabulary the record is written with: the preset words (:data:`WORDS`), who may set which (:data:`SEAT_WORDS`,
  :data:`COORDINATOR_WORDS`, :func:`check_setter`), durations (:func:`parse_duration`), the kind -> class map
  (:data:`KIND_CLASSES`), and the lock (:func:`_locked`), the log (:func:`log_line`, :func:`read_log`) and the setter checks
  every write goes through;
- the remote-activity record (:func:`note_remote_activity`): input over ssh or mosh, or a turn in the Claude app's remote
  control, reported BY A CALLER and kept as a high-water mark in ``remote_activity.json``. It is NOT desk presence (Phill
  11-01: *"don't we want device pings ON when remote desk is measured and NOT be counted at the physical desk?"*): while
  fresh (under :data:`ACTIVE_WINDOW_SECS`) it turns ``follow`` to devices on, and it never counts as laptop input;
- the wrappers flow's installed half and older callers use for one release: :func:`should_push` (the one decider, signature
  unchanged), :func:`effective`, :func:`describe`, :func:`read_word`, :func:`set_word` and the deprecated :func:`decide`;
- the pre-1b readers the migration uses (:func:`read_legacy_word`, :func:`read_policy`), and the retired policy
  (:func:`policy_view`, :func:`set_policy`: the policy cells are replaced by the presets' ``lets_through``).

A word may carry who set it. A seat the operator told ("heading out") may set only :data:`SEAT_WORDS`, and the coordinator only
:data:`COORDINATOR_WORDS`; every other word, and a standing ``loud`` (no setter replaces it), stays his. A setter's write is
refused unless it names an agent and quotes his words, and the record keeps the setter and the quote beside the overlay. The
setter's role comes from the caller's session identity, which is a label under one uid and not a boundary.

Every write, a refusal included, leaves one row in ``<queue_dir>/webpush/presence.log.jsonl`` (append-only, rotated to ``.1``
past 1 MiB; ``levain ping log``). Any fault reads as push: a ping he did not need costs one notification, one he needed and
never got costs the day.

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
           "REMOTE_SOURCES", "REMOTE_FAULT", "note_remote_activity", "read_remote_activity", "PresenceError", "PresenceBusy", "SEAT_WORDS", "COORDINATOR_WORDS", "decide", "describe", "parse_duration", "is_expired", "log_line", "read_log", "read_word", "read_legacy_word", "set_word", "should_push", "effective"]


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
# attention through, and sleep lets them through too (Phill 10-25: in sleep "you CAN ping me for approval prompt blocks
# or anything else high priority like that"); only ``other`` is held. ``dated`` is the one deliberate change: a kind unknown before is now class
# ``attention``, so it pushes at an active desk and through quiet, which it did not before.
DEFAULT_CELLS = {"quiet": {"approval": "push", "attention": "push", "other": "hold"},
                 "sleep": {"approval": "push", "attention": "push", "other": "hold"}}
POLICY_NAME = "policy.json"
POLICY_VERSION = 1
FILE_NAME = "presence.json"
REMOTE_SOURCES = ("remote-terminal", "remote-control")
REMOTE_NAME = "remote_activity.json"
_REMOTE_MAX_BYTES = 1024
_REMOTE_SKEW_SECS = 60.0   # an `at` further ahead than this is clamped to now when written, and a record that far ahead is no record
_REMOTE_LOCK_WAIT_SECS = 0.5
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


def read_legacy_word(queue_dir: Path | str | None) -> dict | None:
    """The PRE-1b word on file (``presence.json``), or ``None``; the migration reads it. A file that is missing, a symlink, not regular, oversized, unparseable or naming
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
           policy: dict | None = None, route: str = "desk", remote: dict | None = None) -> Decision:
    """DEPRECATED, one release (use :func:`levain.webpush.intent.decide`): the pre-1b arguments mapped onto the record. ``word``
    is a pre-1b word record and ``route`` ``desk`` or ``devices``; ``policy`` is ignored (the presets' ``lets_through``
    replaced the cells). ``idle`` is the laptop input idle seconds. Pure."""
    from levain.webpush import intent

    cls = class_name if class_name in CLASSES else ("approval" if urgent else "other")
    return intent.decide(now, intent.from_legacy(word, route, now), idle, class_name=cls, remote=remote)


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
    """The stable dict ``levain ping policy show --json`` prints. The notification policy is RETIRED (presence 1b): what each
    preset lets through is part of the preset (:data:`levain.webpush.intent.PRESET_TABLE`) and the operator changes it for a
    given stretch with ``levain ping lets``. ``cells`` is derived from the presets so an older reader still paints something true."""
    from levain.webpush import intent

    lets = {m: intent.PRESET_TABLE[m]["lets_through"] for m in MUTE_WORDS}
    cells = {m: {c: ("push" if c in intent.LETS_CLASSES[lets[m]] else "hold") for c in CLASSES} for m in MUTE_WORDS}
    return {"version": POLICY_VERSION, "retired": True, "message": POLICY_RETIRED, "cells": cells,
            "classes": dict(sorted(KIND_CLASSES.items())), "default_class": "other", "status": "retired",
            "status_kind": "retired", "reasons": [], "sentences": policy_sentences(cells),
            "default_cells": _copy_cells(DEFAULT_CELLS), "held_by_default": {},
            "presets": {p: {"devices": r["devices"], "lets_through": r["lets_through"], "until": r["until"]}
                        for p, r in intent.PRESET_TABLE.items()}}


POLICY_RETIRED = ("the notification policy is retired: each preset says what it lets through (quiet and sleep: approvals "
                  "and attention items), and `levain ping lets everything|needs|approvals|nothing [--for 1h]` changes it")


def set_policy(queue_dir: Path | str, mute: str, cls: str, value: str, *, by: dict | None = None,
               now: float | None = None, via: str | None = None, wait: float | None = None) -> dict:
    """Retired: always raises :class:`PresenceError` with the pointer (presence 1b)."""
    raise PresenceError(POLICY_RETIRED)


def reset_policy(queue_dir: Path | str, *, by: dict | None = None, now: float | None = None, via: str | None = None,
                 wait: float | None = None) -> dict:
    """Retired: always raises :class:`PresenceError` with the pointer (presence 1b)."""
    raise PresenceError(POLICY_RETIRED)


def _remote_path(queue_dir: Path | str) -> Path:
    return Path(queue_dir) / "webpush" / REMOTE_NAME


def _remote_at(at: object) -> float | None:
    """THE validator for a remote record's ``at``, used by the write and the read: a number (not a bool) with
    ``0 < at < 1e11``, else ``None``. The bounds are compared BEFORE any float() or isfinite(), so a 500-digit int, nan,
    inf, a negative and zero are all invalid and none of them raises."""
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    try:
        if not 0 < at < 1e11:
            return None
        return float(at)
    except (OverflowError, ValueError, TypeError):
        return None


def _remote_valid(got: object) -> dict | None:
    if not isinstance(got, dict) or got.get("source") not in REMOTE_SOURCES:
        return None
    at = _remote_at(got.get("at"))
    if at is None:
        return None
    return {"at": at, "source": got["source"]}


def _read_remote_raw(queue_dir: Path | str | None) -> dict | None:
    """The remote record on file with no age test, or ``None`` for a file that is missing, a symlink, not regular,
    oversized, unparseable or invalid (the same discipline as :func:`read_word`)."""
    if queue_dir is None:
        return None
    try:
        fd = os.open(_remote_path(queue_dir), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = os.read(fd, _REMOTE_MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(raw) > _REMOTE_MAX_BYTES:
        return None
    try:
        return _remote_valid(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, ValueError, RecursionError, ArithmeticError):
        return None


def read_remote_activity(queue_dir: Path | str | None, now: float | None = None) -> dict | None:
    """``{at, source, age}`` while the remote-activity record is valid and under :data:`ACTIVE_WINDOW_SECS` old at
    ``now``, else ``None`` (missing, stale, unreadable, invalid, a symlink, or dated more than a minute ahead). A fault the read did not anticipate
    raises: the callers that read a fault as push (:func:`should_push`, the status view) take the fail-open decision."""
    rec = _read_remote_raw(queue_dir)   # an unreadable or invalid record is None there; an UNEXPECTED fault propagates, so the callers that read a fault as push (should_push, the status view) do
    if rec is None:
        return None
    t = time.time() if now is None else now
    age = t - rec["at"]
    if age < -_REMOTE_SKEW_SECS or age >= ACTIVE_WINDOW_SECS:
        return None
    return {"at": rec["at"], "source": rec["source"], "age": max(0.0, age)}


def _write_remote(queue_dir: Path | str, rec: dict) -> None:
    d = _remote_path(queue_dir).parent
    os.makedirs(d, mode=0o700, exist_ok=True)
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise PresenceError(f"{d} is not a directory of this user's")
    tmp = d / f".{REMOTE_NAME}.{os.getpid()}.{time.time_ns()}.tmp"
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
        os.replace(tmp, _remote_path(queue_dir))
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def note_remote_activity(queue_dir: Path | str, source: str, at: float | None = None) -> bool:
    """Record input measured away from the laptop (``source`` one of :data:`REMOTE_SOURCES`; anything else raises
    :class:`PresenceError`) at ``at`` (default now; an ``at`` later than now is clamped to now, so a source clock that runs ahead is stamped
    now). An invalid ``at`` (not a number, a bool, nan, inf, zero, negative, absurdly large) returns ``False`` and never
    raises into a poll caller. The record is a monotone high-water mark: an older ``at`` never replaces a newer one, an
    equal-or-newer one does; an existing record dated more than a minute ahead of now (a clock stepped back) counts as
    absent, so a current write overwrites it. Written atomically under the
    presence lock. Returns whether the mark was written; a lock not taken within a short wait, or a file that cannot be
    written, returns ``False`` and never raises into a caller's poll. It never touches the laptop activity reading."""
    if source not in REMOTE_SOURCES:
        raise PresenceError(f"{source!r} is not a remote source (one of: {', '.join(REMOTE_SOURCES)})")
    now = time.time()
    if at is None:
        t = now
    else:
        t = _remote_at(at)
        if t is None:
            return False
        t = min(t, now)
    try:
        with _locked(queue_dir, strict=True, wait=_REMOTE_LOCK_WAIT_SECS):
            cur = _read_remote_raw(queue_dir)
            if cur is not None and cur["at"] > now + _REMOTE_SKEW_SECS:
                cur = None
            if cur is not None and t < cur["at"]:
                return False
            _write_remote(queue_dir, {"at": t, "source": source})
            return True
    except (PresenceError, OSError):
        return False


_UNSET = object()   # "read it yourself", distinct from None (no word on file / no idle reading)
_FAULT = "the presence rule could not be read: pushing"
REMOTE_FAULT = object()   # a caller's "the remote read itself faulted": the rule then takes the standard fail-open decision


def should_push(queue_dir: Path | str | None, kind: str, *, now: float | None = None,
                idle: Callable[[], float | None] | None = None, suggest: bool = True,
                word: object = _UNSET, route: object = _UNSET, remote: object = _UNSET) -> Decision:
    """Whether a ``kind`` of ping is pushed to his devices now: THE decider (:func:`levain.webpush.intent._core`), against the
    one record and the sensors (``idle`` defaults to :func:`levain.consent.auto_away.input_idle_seconds`). ``suggest`` marks a
    delivery (it keeps the decision and writes the event rows sensing saw); a status read leaves it off. ``word`` and ``route``
    are accepted and ignored (the record replaced both); ``remote`` keeps its meaning. Any fault reads as push: a missed ping
    is the worse error."""
    try:
        from levain.webpush import intent

        return intent._core(queue_dir, kind, now, idle, suggest, remote)[0]
    except Exception:  # noqa: BLE001 - the rule failing must not silence the phone
        return Decision(True, "pushing", _FAULT)


def effective(queue_dir: Path | str | None, now: float | None = None, *, idle: Callable[[], float | None] | None = None,
              suggest: bool = False, word: object = _UNSET, reading: object = _UNSET, route: object = _UNSET,
              remote: object = _UNSET) -> dict:
    """What the rule does now with an ordinary ping, for the cockpit chip and any status read: ``{state: pushing|holding|muted,
    pushing, reason, source: word|measured, since, word, remote, you, desk}`` plus ``line`` and ``chip`` and the record's parts
    (:func:`levain.webpush.intent.view`). It is the same sample :func:`should_push` takes, so the chip and a push for the same
    instant are one answer. ``reading`` (the raw laptop idle seconds) and ``remote`` let a caller that sampled them once get an
    answer from that same snapshot; ``word``, ``route`` and ``suggest`` are accepted and ignored. A fault reads as pushing."""
    from levain.webpush import intent

    return intent.view(queue_dir, now=now, idle=idle, remote=remote, reading=reading)


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


def read_word(queue_dir: Path | str | None, now: float | None = None,
              idle: Callable[[], float | None] | None = None) -> dict | None:
    """Compat (one release): the standing overlay as the old ``{word, ts, until?, by?, said?, verified?}`` record, or ``None``
    (no overlay, an ended one, a setting with no preset, or a fault)."""
    if queue_dir is None:
        return None
    try:
        from levain.webpush import intent

        now = time.time() if now is None else now
        s = intent._sample(queue_dir, now, idle)
        return intent.legacy_word(s["rec"], now, intent._last_input(now, s["reading"]), s["msg_at"])
    except Exception:  # noqa: BLE001
        return None


_ROUTE_ALIASES = ("desk", "devices")   # the tap-token words of the retired route: `desk` is follow, `devices` is devices on


def set_word(queue_dir: Path | str, word: str, *, duration: str | None = None, now: float | None = None,
             by: dict | None = None, said: str | None = None, match: object = None,
             via: str | None = None, wait: float | None = None,
             precheck: "Callable[[], None] | None" = None, after: "Callable[[], None] | None" = None,
             tap: bool = False) -> dict | None:
    """Compat (one release): :func:`levain.webpush.intent.apply_preset` for a preset word, ``devices`` (devices on, one hour by
    default) and ``desk`` (follow) for the old route words. Returns what :func:`read_word` reads after, ``None`` when no preset
    stands. All arguments but ``tap`` are :func:`levain.webpush.intent.apply_preset`'s.

    ``tap`` (the caller holds a tap token, not the full credential): an alias writes a bounded overlay only, never the
    base (``desk`` for a day by default, the retired route's own default), and neither alias replaces a standing preset
    (``away``, ``quiet``, ``sleep``, ``loud``): that is refused under the lock, and ``back`` ends it. The operator's own
    path (``tap`` false) writes the standing default: ``desk`` is ``follow`` with no overlay and no end (the retired route
    gave it a day; follow is the default state, so there is nothing to return to), and ``devices`` is one hour unless a
    duration is given."""
    from levain.webpush import intent

    if word in _ROUTE_ALIASES:
        if by is not None:
            raise PresenceError(f"`{word}` is the operator's own word: a seat or the coordinator may not set it")
        if tap:
            if isinstance(duration, str) and duration.strip().lower() == "switch":
                raise PresenceError(f"a tap's `{word}` sets a bounded while only, never your standing default")
            outer = precheck

            def precheck() -> None:   # runs under the presence lock, before anything is read for writing
                if outer is not None:
                    outer()
                t = time.time() if now is None else now
                rec = intent.read(queue_dir, t)
                ov = rec["overlay"]
                if rec["status"] in ("file", "projection") and ov is not None and ov.get("preset"):
                    reading, msg_at = intent._live(queue_dir, t, None, rec)
                    if intent.overlay_active(rec, t, intent._last_input(t, reading), msg_at):
                        raise PresenceError(f"`{ov['preset']}` stands: a tap's `{word}` does not replace it "
                                            "(`back` ends it)")
        if word == "devices":
            intent.apply_settings(queue_dir, devices="on", until=duration or "60m", via=via, wait=wait, now=now,
                                  precheck=precheck, after=after)
        else:   # desk: the measurement decides again, which is follow; from a tap, for a bounded while only
            intent.apply_settings(queue_dir, devices="follow", until=duration or ("24h" if tap else "switch"), via=via,
                                  wait=wait, now=now, precheck=precheck, after=after)
    else:
        intent.apply_preset(queue_dir, word, duration=duration, now=now, by=by, said=said, match=match, via=via, wait=wait,
                            precheck=precheck, after=after)
    ts = time.time() if now is None else now
    try:   # what the write left standing (read as of the write itself: he has just acted, so the overlay stands)
        rec = intent.read(queue_dir, ts)
        return intent.legacy_word(rec, ts, ts, None)
    except Exception:  # noqa: BLE001
        return None


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


def _evidence(match: object) -> tuple[bool, float | None, str]:
    """``(verified, said_at, why)`` from a :class:`levain.webpush.quote.Match`; no match is unverified."""
    verified = getattr(match, "verified", False) is True
    at = getattr(match, "said_at", None)
    at = float(at) if verified and isinstance(at, (int, float)) and not isinstance(at, bool) and math.isfinite(at) else None
    why = _clean(getattr(match, "why", "") or "no transcript check ran", _WHY_MAX)
    return verified and at is not None, at, why


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


def describe(queue_dir: Path | str, *, now: float | None = None,
             idle: Callable[[], float | None] | None = None, word: object = _UNSET, reading: object = _UNSET,
             route: object = _UNSET, remote: object = _UNSET) -> str:
    """The one line for ``levain ping status``: :func:`levain.webpush.intent.describe_line` (``word`` and ``route`` are accepted
    and ignored; ``reading`` and ``remote`` let a caller that sampled them once get an answer from that same snapshot)."""
    from levain.webpush import intent

    return intent.view(queue_dir, now=now, idle=idle, remote=remote, reading=reading)["line"]
