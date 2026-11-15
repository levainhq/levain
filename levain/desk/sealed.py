"""levain.desk.sealed: the sealed reader's and drafter's data side (ND-4, board levain#14; night desk design 2.7, 7).

TRUST-BOUNDARY CODE. Raw email never reaches the desk. Two roles, with no tool, launched by
:func:`levain.desk.claude_session.sealed_reader_argv` and :func:`levain.desk.claude_session.sealed_drafter_argv`, sit
between a raw message and the desk's effects. This module is what they read and what they may say:

* :class:`BodyStore`: the raw-body store. A message is stored by its digest (``sha256:<64 hex>``, the ``body_ref`` an
  event row carries). Only the sealed reader's launcher holds the store; the ref alone opens nothing. A body is
  written atomically (mode 0600 in a 0700 directory), is at most :data:`BODY_MAX` bytes, is read back only if its bytes
  still hash to the ref, and is deleted by :meth:`BodyStore.purge` once it is older than :data:`RETENTION`.
  :meth:`BodyStore.raw_source` is the call shape ``Effects.raw_source`` takes.
* :func:`parse_reader_output`: the reader's whole vocabulary. Every field that can influence an effect is an enum;
  the one string is ``untrusted_text``, length-capped and never an argument, a recipient, a path, a host, an order id
  or a ping title. ``instructions_present`` is ADVISORY ONLY: an attacker's body can make the reader say ``no``, so
  nothing may be relaxed on a ``no``. Output that is not exactly that schema is the marker :data:`UNTRUSTED` (an alert
  only), never repaired and never coerced.
* :func:`parse_drafter_output`: one short text and a VOICE from :data:`levain.desk.effects.VOICES`. The send tool
  (:meth:`levain.desk.effects.Effects.send_ack`) checks the voice and the text itself.

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

__all__ = ["BODY_MAX", "BodyRefused", "BodyStore", "CATEGORIES", "DRAFT_TEXT_MAX", "INSTRUCTIONS", "OUTPUT_MAX",
           "RETENTION", "SENDER_VIEWS", "UNTRUSTED", "ReaderResult", "default_store", "parse_drafter_output",
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


def _refuse(why: str) -> BodyRefused:
    return BodyRefused(why)


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class BodyStore:
    """The raw-body store under ``root`` (a directory this process owns: made 0700, refused if it is a symlink or
    open to the group or others). The ref is capability-scoped: it is handed to the sealed reader's launcher only, and
    an event row, the desk and the drafter hold the ref (or nothing) but never the store."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    # --- the directory ---------------------------------------------------------------------------------------------
    def _dir(self, *, create: bool) -> bool:
        """True when the store directory exists and is private; False when it is absent and not created."""
        try:
            st = os.lstat(self.root)
        except FileNotFoundError:
            if not create:
                return False
            self.root.parent.mkdir(parents=True, exist_ok=True)
            os.mkdir(self.root, 0o700)
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
        bytes again changes nothing."""
        if type(raw) is not bytes:
            raise _refuse("a body is bytes")
        if len(raw) > BODY_MAX:
            raise _refuse("the body is over the store's size bound")
        digest = _digest(raw)
        self._dir(create=True)
        final = self.root / (digest + ".body")
        tmp = self.root / (".tmp-" + os.urandom(16).hex())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
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
        dot or separator can ride in it), a file that is a symlink, not a regular file, not 0600 and ours, over the size
        bound, or whose bytes do not hash to the ref. A missing body refuses too (:meth:`raw_source` answers None)."""
        name = self._name(body_ref)
        if not self._dir(create=False):
            raise _refuse("no such body")
        try:
            fd = os.open(self.root / name, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise _refuse("no such body") from None
        except OSError:
            raise _refuse("the body cannot be opened") from None
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) & 0o077
                    or st.st_size > BODY_MAX):
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
        except BodyRefused as exc:
            if str(exc) == "no such body":
                return None
            raise

    # --- retention -------------------------------------------------------------------------------------------------
    def purge(self, now: datetime) -> int:
        """Delete every stored body written more than :data:`RETENTION` before ``now`` (an aware datetime), and every
        leftover temporary file that old; returns how many files went. Only files this store names are touched; a
        symlink is unlinked as a link and never followed. The caller runs this on a schedule."""
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise _refuse("purge needs an aware time")
        if not self._dir(create=False):
            return 0
        cutoff = (now - RETENTION).timestamp()
        gone = 0
        for entry in os.scandir(self.root):
            if _BODY_FILE.fullmatch(entry.name) is None and _TMP_FILE.fullmatch(entry.name) is None:
                continue
            try:
                if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                    os.unlink(entry.path)
                    gone += 1
            except FileNotFoundError:
                continue
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
    return type(value) is str and len(value) <= cap and "\x00" not in value


def parse_reader_output(raw_json: Any) -> ReaderResult | _Untrusted:
    """The reader's output as a :class:`ReaderResult`, or :data:`UNTRUSTED`. Strict: exactly the four keys, an enum
    member (exact ``str``, no case folding, no trimming) for each enum field, ``untrusted_text`` a ``str`` of at most
    :data:`levain.desk.events.UNTRUSTED_TEXT_MAX` characters; any other key, type, value or size, or text that is not
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
