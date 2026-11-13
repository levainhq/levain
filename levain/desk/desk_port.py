"""levain.desk.desk_port: the Desk tab's port (levain#2 lane A): a mailbox and a doorbell, never a conversation.

The Desk tab files one letter the desk model never touched, then watches for the postmark a script writes. This module
holds the two Protocols the tab and the hosts speak, the frozen results they return, the host's commands, and two
implementations of :class:`DeskPort`:

* :class:`HostCommandDeskPort`: runs the argvs a HOST supplies (:class:`DeskCommands`) through
  :func:`levain.desk.room_host.run`. Levain names no script and opens none of the host's files.
* :class:`ScriptedDeskPort`: an in-memory double with scriptable states, for the lanes that build the routes and the page.

Rules the contract tests hold:

1. ``post()`` is idempotent on ``outbox_id``. The claim is written (and committed) BEFORE the host command runs, so a
   crash, a timeout or a kill leaves a claim and a replay answers ``ambiguous``: the command is never run twice for one id.
2. His words go to the host command's STDIN, never argv (``ps`` reads argv). Words over the cap are REFUSED, never cut.
3. A :class:`WakeAdapter` rings with an entry id; ``ring`` has no ``words`` parameter at all.
4. Results are evidence, never a verdict: "filed" is the host's word, "took" is a script-written receipt, and nothing here
   says "fenced", "read" or "acted on".
5. Capabilities are asked, not assumed (:class:`DeskCaps`): a port without ``receipts`` or ``lease`` draws no such slot.

:class:`DeskCommands` is a frozen dataclass SEPARATE from :class:`levain.desk.room_host.HostCommands`: everything in
``HostCommands`` is a command the Room's MODEL can have run, and the Desk tab is model-free. The two have disjoint key
sets and neither ``from_env`` accepts the other's keys (a test holds both); this module hands the Desk argvs to
:func:`levain.desk.room_host.run` and to nothing else.

Host output shapes (the host's side of the contract; anything else degrades to ``unknown`` / empty, never a guess):

* ``post``: exit 0 filed (one stdout line, its ``(thread <id>)`` is the thread to read receipts by), exit 2 the host's
  refusal (its first line is the reason), any other code, a timeout or a kill: the command started and may have
  delivered. ``post`` appends nothing to argv; the words are STDIN.
* ``lease``: no extra argv; stdout is one JSON object ``{"state": "held"|"none"|"unknown", "holder": str|null,
  "age_s": int|null}``; only those three fields are read.
* ``events``: argv gets the thread id, then ``--after <cursor>`` when a cursor is given; stdout is one JSON object
  ``{"events": [{"by": "script"|"desk", "kind": "took"|"held"|"reply", "text": str, "at": str}], "cursor": str|null}``.
  The door's output cap (:data:`levain.desk.room_host.OUTPUT_MAX`) bounds one batch; a batch cut mid-JSON is unreadable.

Stdlib only."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Literal, Optional, Protocol, Sequence, runtime_checkable

from levain.desk import room_host
from levain.desk.chat_lease import _CLIENT_ID
from levain.desk.room_host import HostCommandError, THREAD_RE

__all__ = ["ARMED_STATES", "Armed", "DELIVERY_OUTCOMES", "DESKCMD_ENV", "DeskCaps", "DeskCommandError", "DeskCommands",
           "DeskEvent", "DeskPort", "DeskPortError", "Delivery", "EVENT_BY", "EVENT_KINDS", "EventBatch",
           "HostCommandDeskPort", "LEASE_STATES", "LeaseView", "OutboxConflict", "POST_OUTCOMES", "PostResult",
           "ScriptedDeskPort", "WORDS_MAX", "WakeAdapter", "check_words"]

DESKCMD_ENV = "LEVAIN_DESK_PORT_COMMANDS"
# [seat] characters of words one post may carry by default; a host sets its own with ``words_max``
WORDS_MAX = 2000
POST_OUTCOMES = ("accepted", "ambiguous", "not_delivered")
LEASE_STATES = ("held", "none", "unknown")
EVENT_BY = ("script", "desk")
EVENT_KINDS = ("took", "held", "reply")
DELIVERY_OUTCOMES = ("delivered", "no_listener", "failed")
ARMED_STATES = ("armed", "expired", "unknown")
_WAKES = ("event", "poll")
_MODES = ("pull", "push")
# the client's own outbox id (the Room's own shape; a test holds them equal) and the owner a credential names
_OUTBOX_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,127}")
_ORIGIN = re.compile(rf"(?:cockpit|phone):{_CLIENT_ID}")     # the turn lease's own client id: a name it takes, this takes
_CURSOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,63}")      # never starts with "-": it is an argv word, not a flag
_THREAD_LINE = re.compile(r"\(thread (" + THREAD_RE.pattern + r")\)")
# [seat] characters kept of a host's one-line reference / reason / holder / event text / event time
_LINE_MAX = 300
_DETAIL_MAX = 150
_HOLDER_MAX = 80
_TEXT_MAX = 4000
_AT_MAX = 64
_AGE_MAX = 10 ** 9


class DeskPortError(ValueError):
    """The caller gave the port something it must not (a bad outbox id or origin); the route validates first, so this is
    a caller fault, never an outcome of a post."""


class OutboxConflict(DeskPortError):
    """The same ``outbox_id`` came back with DIFFERENT words: a stored id is answered from the record, never rewritten."""


class DeskCommandError(HostCommandError):
    """The host's desk-port command configuration is not usable."""


def _enum(name: str, value: Any, allowed: Sequence[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{name} is one of {', '.join(allowed)}")
    return value


def _opt_str(name: str, value: Any, limit: int) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > limit or "\x00" in value:
        raise ValueError(f"{name} is 1 to {limit} characters of text, or None")
    return value


# --- the results (frozen; field names are pinned: lanes B and C build against them) ------------------------------------
@dataclass(frozen=True)
class DeskCaps:
    """What the port can do, asked and never assumed. ``wake``: how the doorbell behind ``post`` reaches the desk
    ("event" pushes, "poll" is pulled by the desk). ``receipts``: ``events`` can carry took/held rows. ``lease``:
    ``lease()`` can say who holds it. ``replies``: ``events`` can carry desk replies."""
    adapter: str
    wake: str
    receipts: bool
    lease: bool
    replies: bool
    words_max: int

    def __post_init__(self) -> None:
        if not isinstance(self.adapter, str) or not self.adapter:
            raise ValueError("adapter is a non-empty string")
        _enum("wake", self.wake, _WAKES)
        for n in ("receipts", "lease", "replies"):
            if not isinstance(getattr(self, n), bool):
                raise ValueError(f"{n} is a bool")
        if isinstance(self.words_max, bool) or not isinstance(self.words_max, int) or self.words_max < 1:
            raise ValueError("words_max is a positive int")


@dataclass(frozen=True)
class LeaseView:
    """Allowlisted lease fields ONLY: the state, who holds it (a short label) and how long ago it was renewed. Evidence
    from the host's lease fold. ``none`` and ``unknown`` carry no holder and no age."""
    state: str
    holder: Optional[str] = None
    age_s: Optional[int] = None

    def __post_init__(self) -> None:
        _enum("state", self.state, LEASE_STATES)
        _opt_str("holder", self.holder, _HOLDER_MAX)
        if self.age_s is not None and (isinstance(self.age_s, bool) or not isinstance(self.age_s, int)
                                       or not 0 <= self.age_s <= _AGE_MAX):
            raise ValueError("age_s is a non-negative int, or None")
        if self.state != "held" and (self.holder is not None or self.age_s is not None):
            raise ValueError("only a held lease carries a holder and an age")


@dataclass(frozen=True)
class PostResult:
    """``accepted``: the host command exited 0 (the INBOX's word, never "fenced" or "read"); ``ref`` is its one line and
    ``thread_id`` the thread it printed, if any. ``ambiguous``: it may have been delivered and is never sent again.
    ``not_delivered``: it was refused or never started (``reason`` is one line). ``replayed``: this answer came from the
    stored claim of an earlier post of the same ``outbox_id``. ``ref`` and ``reason`` are the host's text: draw them
    as text only."""
    outcome: str
    ref: Optional[str] = None
    thread_id: Optional[str] = None
    reason: Optional[str] = None
    replayed: bool = False

    def __post_init__(self) -> None:
        _enum("outcome", self.outcome, POST_OUTCOMES)
        _opt_str("ref", self.ref, _LINE_MAX)
        _opt_str("reason", self.reason, _LINE_MAX)
        if self.thread_id is not None and (not isinstance(self.thread_id, str) or THREAD_RE.fullmatch(self.thread_id) is None):
            raise ValueError("thread_id is a thread id")
        if not isinstance(self.replayed, bool):
            raise ValueError("replayed is a bool")


@dataclass(frozen=True)
class DeskEvent:
    """One receipt: ``by`` who wrote it (a script row, or the desk itself), ``kind`` took | held | reply, ``text`` the
    host's words (plain text, never HTML), ``at`` the host's time string."""
    by: str
    kind: str
    text: str
    at: str

    def __post_init__(self) -> None:
        _enum("by", self.by, EVENT_BY)
        _enum("kind", self.kind, EVENT_KINDS)
        if not isinstance(self.text, str) or len(self.text) > _TEXT_MAX or "\x00" in self.text:
            raise ValueError(f"text is at most {_TEXT_MAX} characters")
        if not isinstance(self.at, str) or not self.at or len(self.at) > _AT_MAX or "\x00" in self.at:
            raise ValueError(f"at is 1 to {_AT_MAX} characters")


@dataclass(frozen=True)
class EventBatch:
    """``events`` after the cursor given, oldest first; ``cursor`` the opaque value to pass back as ``after``.
    ``readable`` is False when the host's answer could not be read (no events command, a failure, an unknown shape): an
    empty unreadable batch is NOT "no events yet"."""
    events: tuple[DeskEvent, ...] = ()
    cursor: Optional[str] = None
    readable: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(self.events))
        if not all(isinstance(e, DeskEvent) for e in self.events):
            raise ValueError("events are DeskEvents")
        if self.cursor is not None and (not isinstance(self.cursor, str) or _CURSOR.fullmatch(self.cursor) is None):
            raise ValueError("cursor is 1 to 64 of A-Z a-z 0-9 : . _ - (not starting with -), or None")
        if not isinstance(self.readable, bool):
            raise ValueError("readable is a bool")
        if not self.readable and (self.events or self.cursor is not None):
            raise ValueError("an unreadable batch is empty")


@dataclass(frozen=True)
class Delivery:
    """What a doorbell ring did: ``delivered``, ``no_listener`` (nothing holds the bell's read end) or ``failed``."""
    outcome: str
    detail: Optional[str] = None

    def __post_init__(self) -> None:
        _enum("outcome", self.outcome, DELIVERY_OUTCOMES)
        _opt_str("detail", self.detail, _LINE_MAX)


@dataclass(frozen=True)
class Armed:
    """Whether the desk's watcher is armed: ``armed`` | ``expired`` | ``unknown`` (the honest default today); ``until`` the
    host's time string when it says."""
    state: str
    until: Optional[str] = None

    def __post_init__(self) -> None:
        _enum("state", self.state, ARMED_STATES)
        _opt_str("until", self.until, _AT_MAX)


# --- the Protocols ------------------------------------------------------------------------------------------------------
@runtime_checkable
class DeskPort(Protocol):
    """What the Desk TAB calls. Harness-free; never holds authority; returns evidence, never a verdict."""

    def capabilities(self) -> DeskCaps: ...

    def lease(self) -> LeaseView: ...

    def post(self, words: str, *, outbox_id: str, origin: str) -> PostResult: ...

    def events(self, thread_id: str, after: Optional[str] = None) -> EventBatch: ...


@runtime_checkable
class WakeAdapter(Protocol):
    """The doorbell; one per harness; HOST side, behind ``post``. ``ring`` takes an entry id and NEVER words."""
    harness: ClassVar[str]
    mode: ClassVar[Literal["pull", "push"]]

    def ring(self, entry_id: str) -> Delivery: ...

    def armed(self) -> Armed: ...


# --- the host's commands ------------------------------------------------------------------------------------------------
_DESK_KEYS = ("post", "lease", "events", "words_max")


@dataclass(frozen=True)
class DeskCommands:
    """The argvs a host gives the Desk tab: ``post`` (words on STDIN), ``lease`` (reads the lease), ``events`` (reads a
    thread's receipts); each absolute-program, run ``shell=False``; any may be None (the capability is then absent).
    ``words_max`` is the host's cap. Deliberately NOT part of :class:`levain.desk.room_host.HostCommands`."""
    post: Optional[tuple[str, ...]] = None
    lease: Optional[tuple[str, ...]] = None
    events: Optional[tuple[str, ...]] = None
    words_max: int = WORDS_MAX

    def __post_init__(self) -> None:
        for name in ("post", "lease", "events"):
            v = getattr(self, name)
            if v is not None:
                try:
                    object.__setattr__(self, name, room_host._check_argv(name, v))
                except HostCommandError as exc:
                    raise DeskCommandError(str(exc)) from None
        if isinstance(self.words_max, bool) or not isinstance(self.words_max, int) or not 1 <= self.words_max <= WORDS_MAX * 10:
            raise DeskCommandError(f"words_max is an int from 1 to {WORDS_MAX * 10}")

    def to_json(self) -> str:
        return json.dumps({"post": list(self.post) if self.post else None, "lease": list(self.lease) if self.lease else None,
                           "events": list(self.events) if self.events else None, "words_max": self.words_max}, sort_keys=True)

    @classmethod
    def from_env(cls, value: Optional[str]) -> Optional["DeskCommands"]:
        """Commands from an environment value (:data:`DESKCMD_ENV`); None when unset or empty. A malformed value, or ANY
        key outside the desk's own (a Room key included), raises :class:`DeskCommandError`."""
        if not value:
            return None
        try:
            doc = json.loads(value)
        except ValueError:
            raise DeskCommandError("the desk commands are not JSON") from None
        if not isinstance(doc, dict) or set(doc) - set(_DESK_KEYS):
            raise DeskCommandError("the desk commands are an object of " + ", ".join(_DESK_KEYS))
        return cls(doc.get("post"), doc.get("lease"), doc.get("events"), doc.get("words_max", WORDS_MAX))


# --- shared checks ------------------------------------------------------------------------------------------------------
def check_words(words: Any, words_max: int) -> Optional[str]:
    """None when ``words`` may be posted, else the one-line refusal. Over the cap is REFUSED, never cut: a cut
    instruction is a different instruction."""
    if not isinstance(words, str) or not words.strip():
        return "There is nothing to send."
    if "\x00" in words:
        return "That message holds a character that cannot be sent."
    try:
        words.encode("utf-8")
    except UnicodeEncodeError:
        return "That message holds text that cannot be sent as UTF-8."
    if len(words) > words_max:
        return f"That message is {len(words)} characters; the most one can hold is {words_max}. It was not sent or cut."
    return None


def _check_ids(outbox_id: Any, origin: Any) -> None:
    if not isinstance(outbox_id, str) or _OUTBOX_ID.fullmatch(outbox_id) is None:
        raise DeskPortError("outbox_id is the client's own id: 8 to 128 of A-Z a-z 0-9 . _ -")
    if not isinstance(origin, str) or _ORIGIN.fullmatch(origin) is None:
        raise DeskPortError("origin is cockpit:<name> or phone:<id>, from the credential")


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:_LINE_MAX]
    return ""


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _sha(words: str) -> str:
    return hashlib.sha256(words.encode("utf-8", "replace")).hexdigest()


# --- the host-command port ----------------------------------------------------------------------------------------------
_SCHEMA = ("CREATE TABLE IF NOT EXISTS desk_post_claims (outbox_id TEXT PRIMARY KEY, words_sha256 TEXT NOT NULL, "
           "origin TEXT NOT NULL, at TEXT NOT NULL, state TEXT NOT NULL, ref TEXT, thread_id TEXT, reason TEXT)")


def _map_ran(ran: room_host.Ran) -> PostResult:
    """The host command's result as a post outcome. Exit 0 accepted; exit 2 the host's refusal; a program that never
    started delivered nothing; every other end of a command that DID start (a failure, a timeout, a kill) may have
    delivered, so it is ambiguous and never run again."""
    if not ran.started:
        return PostResult("not_delivered", reason="The host command could not be started; nothing was sent.")
    if ran.code == 0:
        return PostResult("accepted", ref=_first_line(ran.out) or None,
                          thread_id=(m.group(1) if (m := _THREAD_LINE.search(ran.out)) else None))
    if ran.code == 2:
        return PostResult("not_delivered", reason=_first_line(ran.out) or "The desk would not take that message.")
    if ran.code == room_host.TIMED_OUT:
        why = "The host command did not finish in time and was stopped"
    elif ran.code < 0:
        why = "The host command was killed"
    else:
        why = f"The host command failed (exit {ran.code})"
    # the host's own diagnostic (its first line) rides behind the fixed words; it is capped so the whole stays one line
    detail = "" if ran.code in (room_host.TIMED_OUT, 0) or ran.code < 0 else _first_line(ran.out)[:_DETAIL_MAX]
    return PostResult("ambiguous", reason=f"{why} and may have delivered; it is not sent again." + (f" {detail}" if detail else ""))


class HostCommandDeskPort:
    """:class:`DeskPort` over a host's argvs (:class:`DeskCommands`). ``claims_path`` is a SQLite file holding the post
    claims (created 0600); it must outlive the process, or idempotency does not survive a restart."""

    def __init__(self, commands: DeskCommands, claims_path: Path | str, *, adapter: str = "host-command",
                 wake: str = "poll", timeout: float = room_host.TIMEOUT) -> None:
        if not isinstance(commands, DeskCommands):
            raise DeskPortError("commands is a DeskCommands")
        self._cmds = commands
        self._path = Path(claims_path)
        self._adapter = adapter
        self._wake = wake
        self._timeout = timeout
        self._lock = threading.Lock()
        self.capabilities()      # the adapter and wake are checked once, here
        self._ensure()

    # the claim store
    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._path), timeout=10.0, isolation_level=None)

    def _ensure(self) -> None:
        fd = os.open(str(self._path), os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        conn = self._connect()
        try:
            conn.execute(_SCHEMA)
        finally:
            conn.close()

    # the port
    @property
    def can_post(self) -> bool:
        """False when no ``post`` command is configured: a port nothing can be filed through is not a desk that is wired
        (the Desk routes read it as unwired, and a send answers 404 as it does with no port at all)."""
        return self._cmds.post is not None

    def capabilities(self) -> DeskCaps:
        c = self._cmds
        return DeskCaps(adapter=self._adapter, wake=self._wake, receipts=c.events is not None, lease=c.lease is not None,
                        replies=c.events is not None, words_max=c.words_max)

    def lease(self) -> LeaseView:
        if self._cmds.lease is None:
            return LeaseView("unknown")
        ran = room_host.run(self._cmds.lease, "", (), timeout=self._timeout)
        if not ran.started or ran.code != 0:
            return LeaseView("unknown")
        return _parse_lease(ran.out)

    def events(self, thread_id: str, after: Optional[str] = None) -> EventBatch:
        if self._cmds.events is None:
            return EventBatch(readable=False)
        if not isinstance(thread_id, str) or THREAD_RE.fullmatch(thread_id) is None:
            return EventBatch(readable=False)
        extra: tuple[str, ...] = (thread_id,)
        if after is not None:
            if not isinstance(after, str) or _CURSOR.fullmatch(after) is None:
                return EventBatch(readable=False)
            extra += ("--after", after)
        ran = room_host.run(self._cmds.events, "", extra, timeout=self._timeout)
        if not ran.started or ran.code != 0:
            return EventBatch(readable=False)
        return _parse_events(ran.out)

    def post(self, words: str, *, outbox_id: str, origin: str) -> PostResult:
        _check_ids(outbox_id, origin)
        refusal = check_words(words, self._cmds.words_max)
        if refusal is not None:
            return PostResult("not_delivered", reason=refusal)       # nothing is claimed for a line that never goes
        if self._cmds.post is None:
            return PostResult("not_delivered", reason="This cockpit has no desk wired.")
        sha = _sha(words)
        with self._lock:                  # one claimer per process; the SQLite write lock holds across processes
            claimed = self._claim(outbox_id, sha, origin)
        if claimed is not None:
            return claimed                # a stored id is answered from the record and never run again
        try:
            result = _map_ran(room_host.run(self._cmds.post, words, (), timeout=self._timeout))
        except Exception:                 # noqa: BLE001 - whatever ran may have delivered: the claim stays, ambiguous
            result = PostResult("ambiguous", reason="The host command failed and may have delivered; it is not sent again.")
        self._settle(outbox_id, result)
        return result

    def _claim(self, outbox_id: str, sha: str, origin: str) -> Optional[PostResult]:
        """None when this call holds a fresh claim (it alone runs the command), else the stored answer."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT words_sha256, state, ref, thread_id, reason FROM desk_post_claims WHERE outbox_id = ?",
                               (outbox_id,)).fetchone()
            if row is None:
                conn.execute("INSERT INTO desk_post_claims (outbox_id, words_sha256, origin, at, state) "
                             "VALUES (?, ?, ?, ?, 'claimed')", (outbox_id, sha, origin, _stamp()))
                conn.execute("COMMIT")
                return None
            conn.execute("ROLLBACK")
        finally:
            conn.close()
        stored_sha, state, ref, thread_id, reason = row
        if stored_sha != sha:
            raise OutboxConflict("that outbox_id was already used for different words")
        if state == "claimed":            # a crash, a kill or a concurrent post: it may have been delivered
            return PostResult("ambiguous", reason="An earlier send of this message did not report back; it is not sent again.",
                              replayed=True)
        return PostResult(state, ref=ref, thread_id=thread_id, reason=reason, replayed=True)

    def _settle(self, outbox_id: str, result: PostResult) -> None:
        try:
            conn = self._connect()
            try:
                conn.execute("UPDATE desk_post_claims SET state = ?, ref = ?, thread_id = ?, reason = ? "
                             "WHERE outbox_id = ? AND state = 'claimed'",
                             (result.outcome, result.ref, result.thread_id, result.reason, outbox_id))
            finally:
                conn.close()
        except sqlite3.Error:
            pass                          # the claim stands as 'claimed': a replay answers ambiguous, never a second run


# --- strict parsers (an unknown shape is unknown / empty, never a guess) --------------------------------------------------
def _load_obj(text: str) -> Optional[dict]:
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def _parse_lease(text: str) -> LeaseView:
    doc = _load_obj(text)
    if doc is None:
        return LeaseView("unknown")
    try:
        state = doc.get("state")
        if state == "held":
            return LeaseView("held", doc.get("holder"), doc.get("age_s"))
        if state in ("none", "unknown"):
            return LeaseView(state)
    except ValueError:
        pass
    return LeaseView("unknown")


def _parse_events(text: str) -> EventBatch:
    doc = _load_obj(text)
    if doc is None or not isinstance(doc.get("events"), list):
        return EventBatch(readable=False)
    rows = doc["events"]
    if not all(isinstance(e, dict) for e in rows):
        return EventBatch(readable=False)
    try:
        return EventBatch(tuple(DeskEvent(e["by"], e["kind"], e["text"], e["at"]) for e in rows), doc.get("cursor"), True)
    except (KeyError, ValueError):
        return EventBatch(readable=False)


# --- the double ---------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class _Posted:
    words: str
    outbox_id: str
    origin: str


@dataclass
class ScriptedDeskPort:
    """An in-memory :class:`DeskPort` for the lanes that build against the port: deterministic and scriptable. ``posts``
    lists the posts that ran (a replayed outbox id is not run again). Scripting: :meth:`script_lease`,
    :meth:`script_post` (a FIFO of outcomes; empty means accepted), :meth:`script_event`, :meth:`script_unreadable`."""
    caps: DeskCaps = field(default_factory=lambda: DeskCaps("scripted", "event", True, True, True, WORDS_MAX))
    posts: list[_Posted] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._lease = LeaseView("unknown")
        self._queue: list[PostResult] = []
        self._claims: dict[str, tuple[str, PostResult]] = {}
        self._events: dict[str, list[DeskEvent]] = {}
        self._unreadable = False
        self._n = 0

    def script_lease(self, state: str, holder: Optional[str] = None, age_s: Optional[int] = None) -> None:
        self._lease = LeaseView(state, holder, age_s)

    def script_post(self, outcome: str, *, ref: Optional[str] = None, thread_id: Optional[str] = None,
                    reason: Optional[str] = None) -> None:
        self._queue.append(PostResult(outcome, ref=ref, thread_id=thread_id, reason=reason))

    def script_event(self, thread_id: str, by: str, kind: str, text: str, at: str) -> None:
        self._events.setdefault(thread_id, []).append(DeskEvent(by, kind, text, at))

    def script_unreadable(self, flag: bool = True) -> None:
        self._unreadable = bool(flag)

    def capabilities(self) -> DeskCaps:
        return self.caps

    def lease(self) -> LeaseView:
        return self._lease if self.caps.lease else LeaseView("unknown")

    def post(self, words: str, *, outbox_id: str, origin: str) -> PostResult:
        _check_ids(outbox_id, origin)
        refusal = check_words(words, self.caps.words_max)
        if refusal is not None:
            return PostResult("not_delivered", reason=refusal)
        sha = _sha(words)
        if outbox_id in self._claims:
            stored_sha, stored = self._claims[outbox_id]
            if stored_sha != sha:
                raise OutboxConflict("that outbox_id was already used for different words")
            return PostResult(stored.outcome, stored.ref, stored.thread_id, stored.reason, replayed=True)
        self._n += 1
        if self._queue:
            result = self._queue.pop(0)
        else:
            result = PostResult("accepted", ref=f"Filed (thread th-sc{self._n}).", thread_id=f"th-sc{self._n}")
        self._claims[outbox_id] = (sha, result)
        self.posts.append(_Posted(words, outbox_id, origin))
        return result

    def events(self, thread_id: str, after: Optional[str] = None) -> EventBatch:
        if self._unreadable or not self.caps.receipts:
            return EventBatch(readable=False)
        start = 0
        if after is not None:
            if not isinstance(after, str) or not after.isdigit():
                return EventBatch(readable=False)
            start = int(after)
        rows = self._events.get(thread_id, [])
        return EventBatch(tuple(rows[start:]), str(len(rows)), True)
