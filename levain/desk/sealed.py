"""levain.desk.sealed: the sealed reader's and drafter's data side (ND-4, board levain#14; night desk design 2.7, 7).

TRUST-BOUNDARY CODE. Raw email never reaches the desk. Two roles, with no tool, launched by
:func:`levain.desk.claude_session.sealed_reader_argv` and :func:`levain.desk.claude_session.sealed_drafter_argv`, sit
between a raw message and the desk's effects. This module is what they read and what they may say:

* :class:`BodyStore`: the raw-body store. A message is stored by its digest (``sha256:<64 hex>``, the ``body_ref`` an
  event row carries). Only the sealed reader's launcher (not built in this commit) holds the store; the ref alone
  opens nothing. A body is written atomically (mode 0600 in a 0700 directory), is at most :data:`BODY_MAX` bytes, is
  read back only if it is a regular file with one link and its bytes still hash to the ref, and is deleted by
  :meth:`BodyStore.purge` once it is older than :data:`RETENTION`. :meth:`BodyStore.raw_source` is the call shape
  ``Effects.raw_source`` takes. KNOWN EDGE: re-storing the same bytes keeps the first write's mtime, so a re-delivery
  on day 29 is purged on day 30 (rare, and the event acts within hours).
* AN OVERSIZE MESSAGE. The mail door takes up to :data:`levain.desk.events.MAIL_RAW_MAX` bytes, more than
  :data:`BODY_MAX`. :meth:`BodyStore.put` of a body over :data:`BODY_MAX` raises :class:`BodyOversize` (its one fixed
  text, :data:`R_BODY_OVERSIZE`) and writes nothing. The caller turns that into an event with trust ``untrusted`` and
  NO ``body_ref``: an alert only. No reader runs on it, and :meth:`levain.desk.effects.Effects.send_ack` refuses an
  event with no ``body_ref``.
* :func:`parse_reader_output`: the reader's whole vocabulary. Every field that can influence an effect is an enum;
  the one string is ``untrusted_text``, length-capped and never an argument, a recipient, a path, a host, an order id
  or a ping title. ``instructions_present`` is ADVISORY ONLY: an attacker's body can make the reader say ``no``, so
  nothing may be relaxed on a ``no``. Output that is not exactly that schema is the marker :data:`UNTRUSTED` (an alert
  only), never repaired and never coerced.
* :func:`parse_drafter_output`: one short text and a VOICE from :data:`levain.desk.effects.VOICES`. The send tool
  (:meth:`levain.desk.effects.Effects.send_ack`) checks the voice and the text itself.

THE LAUNCHER (not built in this commit) MUST, for either role: run the argv of
:func:`levain.desk.claude_session.sealed_reader_argv` / :func:`levain.desk.claude_session.sealed_drafter_argv` as
given (it loads no settings source and runs in safe mode, so no hook, plugin, CLAUDE.md or memory of the operator
reaches a model that reads attacker text); run on a host with NO machine-level managed-policy settings or hooks
(the live probe covered the operator's own hooks; an admin-managed policy hook was not probed and may still apply);
give the child an EMPTY working directory of its own (no CLAUDE.md, no
``.claude/``, no repository above it) and an explicit scrubbed environment it names (never ``os.environ`` passed
through); hand the body or the message on stdin, never in the argv; and unwrap the ``result`` string from the
``--output-format json`` envelope BEFORE :func:`parse_reader_output` / :func:`parse_drafter_output`. The parsers take
the bare object; a wrapped output fails closed as :data:`UNTRUSTED`. Nothing schedules :meth:`BodyStore.purge` yet:
that is the launcher's caller too.

This module holds no mail credential, runs no process and sends nothing.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from levain.desk.effects import VOICES, Draft
from levain.desk.events import UNTRUSTED_TEXT_MAX

__all__ = ["BODY_MAX", "BodyMissing", "BodyOversize", "BodyRefused", "BodyStore", "CATEGORIES", "DRAFT_TEXT_MAX",
           "INSTRUCTIONS", "OUTPUT_MAX", "R_BODY_OVERSIZE", "RETENTION", "SENDER_VIEWS", "UNTRUSTED", "ReaderResult", "default_store", "parse_drafter_output",
           "parse_reader_output"]

#: the most bytes of one raw message the store keeps (one over is refused, never cut: a cut body is a different body)
BODY_MAX = 1024 * 1024
#: a stored body lives this long after it was written; :meth:`BodyStore.purge` deletes what is older
RETENTION = timedelta(days=30)
STORE_DIR_NAME = "sealed_bodies"
_BODY_REF = re.compile(r"sha256:([0-9a-f]{64})")
_BODY_FILE = re.compile(r"[0-9a-f]{64}\.body")
_TMP_FILE = re.compile(r"\.tmp-[0-9a-f]{32}")


class BodyRefused(Exception):
    """The store refused: a ref of the wrong shape, an over-size body, a store that is not private, a body that is not
    the bytes its ref names. The text is fixed and never holds a path or any of the body."""


class BodyMissing(BodyRefused):
    """No body is stored under the ref (or no store exists yet): :meth:`BodyStore.raw_source` answers None for this
    class only, never by reading the text."""


#: the one fixed text of :class:`BodyOversize`
R_BODY_OVERSIZE = "the body is over the store's size bound"


class BodyOversize(BodyRefused):
    """:meth:`BodyStore.put` of a body over :data:`BODY_MAX`; nothing was written. The caller posts the event with trust
    ``untrusted`` and NO ``body_ref`` (an alert only); see the module docstring."""


def _refuse(why: str) -> BodyRefused:
    return BodyRefused(why)


#: characters ``untrusted_text`` (and a draft's text) may not hold: C0 controls other than tab and newline, DEL, the
#: C1 controls (CSI among them), a lone surrogate, the bidi embedding, override and isolate controls, the bidi marks
#: (LRM, RLM, ALM), the zero-width space, joiners and word joiner, the invisible operators, the line and paragraph
#: separators, the variation selectors, the tag block, and the byte-order mark. Each would be a terminal escape, a
#: reordering, a hidden character, or a string that cannot be encoded. Every code point is written as an escape, so
#: the source holds no invisible character (a test pins this file as printable ASCII)
_UNSAFE_CHARS = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff"
                           "\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c"
                           "\u200b-\u200d\u2060-\u2064\u2028\u2029\ufeff"
                           "\ufe00-\ufe0f\U000e0000-\U000e007f]")


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _private_single_file(st: os.stat_result) -> bool:
    """A regular file of this user with one link, no group or other bit, and within :data:`BODY_MAX`."""
    return (stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_uid == os.geteuid()
            and not stat.S_IMODE(st.st_mode) & 0o077 and st.st_size <= BODY_MAX)


class BodyStore:
    """The raw-body store under ``root`` (a directory this process owns: made 0700, refused if it is a symlink or
    open to the group or others). The ref is capability-scoped: it is handed to the sealed reader's launcher (not
    built in this commit) only, and an event row, the desk and the drafter hold the ref (or nothing) but never the
    store."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    # --- the directory ---------------------------------------------------------------------------------------------
    def _dir(self, *, create: bool) -> bool:
        """True when the store directory exists and is private; False when it is absent and not created. Any other
        fault examining or making it (no permission, a parent that is a file) is one fixed :class:`BodyRefused`."""
        try:
            return self._dir_checked(create=create)
        except OSError:
            raise _refuse("the body store cannot be examined or made") from None

    def _dir_checked(self, *, create: bool) -> bool:
        try:
            st = os.lstat(self.root)
        except FileNotFoundError:
            if not create:
                return False
            self.root.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.mkdir(self.root, 0o700)
            except FileExistsError:
                pass                      # exist_ok: a concurrent writer made it; the checks below still apply
            else:
                os.chmod(self.root, 0o700)
            st = os.lstat(self.root)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) & 0o077:
            raise _refuse("the body store is not a private directory of this user")
        return True

    @staticmethod
    def _name(body_ref: Any) -> str:
        if type(body_ref) is not str:
            raise _refuse("a body ref is not a digest ref")
        m = _BODY_REF.fullmatch(body_ref)
        if m is None:
            raise _refuse("a body ref is not a digest ref")
        return m.group(1) + ".body"

    # --- write -----------------------------------------------------------------------------------------------------
    def put(self, raw: bytes) -> str:
        """Store ``raw`` and return its ``body_ref``. Atomic (a temporary file, fsync, rename) and 0600. Storing the same
        bytes again changes nothing: when a stored body already reads back as those bytes, no file is written and its
        mtime (so its retention) stays as it was. Over :data:`BODY_MAX` raises :class:`BodyOversize`."""
        if type(raw) is not bytes:
            raise _refuse("a body is bytes")
        if len(raw) > BODY_MAX:
            raise BodyOversize(R_BODY_OVERSIZE)
        digest = _digest(raw)
        self._dir(create=True)
        try:
            self.read_raw("sha256:" + digest)
        except BodyRefused:
            pass                          # absent, or present but not those bytes: write it
        else:
            return "sha256:" + digest
        final = self.root / (digest + ".body")
        tmp = self.root / (".tmp-" + os.urandom(16).hex())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fh = os.fdopen(fd, "wb")
            except BaseException:
                try:
                    os.close(fd)                  # fdopen never took the descriptor: it is still ours to close
                except OSError:
                    pass
                raise
            with fh:
                os.fchmod(fh.fileno(), 0o600)
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, final)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return "sha256:" + digest

    # --- read ------------------------------------------------------------------------------------------------------
    def read_raw(self, body_ref: Any) -> bytes:
        """The bytes ``body_ref`` names. Refuses a ref that is not exactly ``sha256:`` and 64 lowercase hex (so no path,
        dot or separator can ride in it), a file that is a symlink, not a regular file (a FIFO is refused by ``lstat``
        before any open, so it cannot block the read), has more than one link, is not 0600 and ours, is over the size
        bound, or whose bytes do not hash to the ref. A missing body raises :class:`BodyMissing` (:meth:`raw_source`
        answers None for it)."""
        name = self._name(body_ref)
        if not self._dir(create=False):
            raise BodyMissing("no such body")
        path = self.root / name
        try:
            before = os.lstat(path)
        except FileNotFoundError:
            raise BodyMissing("no such body") from None
        except OSError:
            raise _refuse("the body cannot be opened") from None
        if not _private_single_file(before):
            raise _refuse("the body file is not a private regular file within the bound")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise BodyMissing("no such body") from None
        except OSError:
            raise _refuse("the body cannot be opened") from None
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if (not _private_single_file(st) or (st.st_dev, st.st_ino) != (before.st_dev, before.st_ino)):
                raise _refuse("the body file is not a private regular file within the bound")
            data = fh.read(BODY_MAX + 1)
        if len(data) > BODY_MAX or not hmac.compare_digest("sha256:" + _digest(data), body_ref):
            raise _refuse("the body is not the bytes its ref names")
        return data

    def raw_source(self, body_ref: str) -> bytes | None:
        """The shape ``Effects.raw_source`` takes: the bytes for a ``body_ref``, None when no such body is stored. A
        body that is present but tampered, or a store that is not private, refuses (the verb answers its fixed fault)."""
        try:
            return self.read_raw(body_ref)
        except BodyMissing:
            return None

    # --- retention -------------------------------------------------------------------------------------------------
    def purge(self, now: datetime) -> int:
        """Delete every stored body written more than :data:`RETENTION` before ``now`` (an aware datetime), and every
        leftover temporary file that old; returns how many files went. Only files this store names are touched; a
        symlink is unlinked as a link and never followed. A fault on one entry skips that entry and the sweep goes on; a
        time too early to subtract the retention from, or a directory that cannot be scanned, is one fixed
        :class:`BodyRefused`. The caller runs this on a schedule."""
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise _refuse("purge needs an aware time")
        try:
            cutoff = (now - RETENTION).timestamp()
        except (OverflowError, ValueError, OSError):
            raise _refuse("purge needs a time later than the retention") from None
        if not self._dir(create=False):
            return 0
        gone = 0
        try:
            with os.scandir(self.root) as entries:
                for entry in entries:     # a fault reading the directory itself (open or next) is the refusal below
                    if _BODY_FILE.fullmatch(entry.name) is None and _TMP_FILE.fullmatch(entry.name) is None:
                        continue
                    try:
                        if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                            os.unlink(entry.path)
                            gone += 1
                    except OSError:
                        continue          # gone already, a directory so named, or no permission: skip this one
        except OSError:
            raise _refuse("the body store cannot be scanned") from None
        return gone


def default_store(levain_home: Path | str | None = None) -> BodyStore:
    """The store under the desk's home (:func:`levain.desk.legacy.desk_home`, the one place that resolves the Levain
    home); nothing is created until the first :meth:`BodyStore.put`."""
    from levain.desk.legacy import desk_home
    return BodyStore(desk_home(levain_home) / STORE_DIR_NAME)


# --- the reader's output -------------------------------------------------------------------------------------------
#: what kind of message it is; an enum, never a string the reader chose
CATEGORIES = ("question", "request", "status_update", "complaint", "automated", "other")
#: the reader's view of whether the body's own claimed sender fits the sender the code authenticated: ADVISORY. The
#: recipient is derived from the raw message by code, never from this field
SENDER_VIEWS = ("consistent", "inconsistent", "unclear")
#: ``yes`` or ``no``: ADVISORY ONLY. An attacker's body can force it to ``no``, so a ``no`` relaxes nothing
INSTRUCTIONS = ("yes", "no")
#: the longest raw output (characters) the parsers read at all
OUTPUT_MAX = 8192
DRAFT_TEXT_MAX = 600
_READER_KEYS = frozenset({"category", "sender_as_authenticated", "instructions_present", "untrusted_text"})
_DRAFTER_KEYS = frozenset({"text", "voice"})


class _Untrusted:
    """The marker for output that is not the schema: an alert, never an effect, never repaired."""
    _instance: _Untrusted | None = None

    def __new__(cls) -> _Untrusted:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "untrusted"

    def __bool__(self) -> bool:
        return False


UNTRUSTED = _Untrusted()


@dataclass(frozen=True)
class ReaderResult:
    """A reader's answer that passed the schema. ``category``, ``sender_as_authenticated`` and ``instructions_present``
    are enum members; ``untrusted_text`` is a bounded string that is only ever shown, quoted and marked, and is never
    an argument to a verb, a recipient, a path, a host, an order id or a ping title."""
    category: str
    sender_as_authenticated: str
    instructions_present: str
    untrusted_text: str

    def enum_fields(self) -> dict[str, str]:
        """The fields that may sit in an event row's typed columns: enums only, never the text."""
        return {"category": self.category, "sender_as_authenticated": self.sender_as_authenticated,
                "instructions_present": self.instructions_present}


def _object(raw_json: Any) -> Mapping[str, Any] | None:
    """The JSON object in ``raw_json``, or None: exact ``str``, bounded, duplicate keys refused, no NaN or Infinity."""
    if type(raw_json) is not str or len(raw_json) > OUTPUT_MAX:
        return None

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in items:
            if k in out:
                raise ValueError("duplicate key")
            out[k] = v
        return out

    def no_constant(_: str) -> Any:
        raise ValueError("not a JSON number")

    try:
        doc = json.loads(raw_json, object_pairs_hook=pairs, parse_constant=no_constant)
    except (ValueError, RecursionError):
        return None
    return doc if type(doc) is dict else None


def _text(value: Any, cap: int) -> bool:
    """An exact ``str`` within ``cap`` characters holding none of :data:`_UNSAFE_CHARS` (tab and newline pass)."""
    return type(value) is str and len(value) <= cap and _UNSAFE_CHARS.search(value) is None


def parse_reader_output(raw_json: Any) -> ReaderResult | _Untrusted:
    """The reader's output as a :class:`ReaderResult`, or :data:`UNTRUSTED`. Strict: exactly the four keys, an enum
    member (exact ``str``, no case folding, no trimming) for each enum field, ``untrusted_text`` a ``str`` of at most
    :data:`levain.desk.events.UNTRUSTED_TEXT_MAX` characters with no control, bidi, zero-width or lone-surrogate
    character (:data:`_UNSAFE_CHARS`); any other key, type, value or size, or text that is not
    one JSON object, is :data:`UNTRUSTED`. Nothing is coerced or repaired."""
    doc = _object(raw_json)
    if doc is None or set(doc) != _READER_KEYS:
        return UNTRUSTED
    for key, allowed in (("category", CATEGORIES), ("sender_as_authenticated", SENDER_VIEWS),
                         ("instructions_present", INSTRUCTIONS)):
        v = doc[key]
        if type(v) is not str or v not in allowed:
            return UNTRUSTED
    if not _text(doc["untrusted_text"], UNTRUSTED_TEXT_MAX):
        return UNTRUSTED
    return ReaderResult(doc["category"], doc["sender_as_authenticated"], doc["instructions_present"],
                        doc["untrusted_text"])


def parse_drafter_output(raw_json: Any) -> Draft | _Untrusted:
    """The drafter's output as a :class:`levain.desk.effects.Draft` (one text and a voice), or :data:`UNTRUSTED`:
    exactly the keys ``text`` and ``voice``, the voice an exact member of :data:`levain.desk.effects.VOICES`, the text a
    ``str`` of at most :data:`DRAFT_TEXT_MAX` characters. The send tool then checks the voice against the order's and
    runs the lexical check; this parser only refuses a shape that is not the schema."""
    doc = _object(raw_json)
    if doc is None or set(doc) != _DRAFTER_KEYS:
        return UNTRUSTED
    if type(doc["voice"]) is not str or doc["voice"] not in VOICES or not _text(doc["text"], DRAFT_TEXT_MAX):
        return UNTRUSTED
    return Draft(doc["text"], doc["voice"])
