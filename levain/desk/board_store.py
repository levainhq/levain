"""levain.desk.board_store — the board's item store: where it lives, the one lock over it, and the writer.

The writer is a port of flow's ``scripts/board.py``: flow's records (compact separators, ASCII only) plus three keys of
its own, ``author``, ``grade`` and ``prev`` (see :func:`build_record`, :func:`chain_prev`), the same refusals, the same id
allocation under the lock. Item STATE is never stored; this module writes
records and reads ids only. The fold and every state rule live in :mod:`levain.desk.board_state`, which this module does
not import.

Where the store is (one pointer, one identity):

* ``<levain home>/board`` is :func:`levain.desk.work_attach.board_store_dir`. Its ``STORE`` file is the pointer: one
  line, an absolute path, written LAST by ``levain board use <dir>`` (atomic replace).
* With a pointer, the store directory is the one it names, and it must hold ``store.id`` (a dangling pointer refuses every
  reader and writer; only ``board use`` creates a store). With none, readers use an explicit ``--items`` /
  ``$LEVAIN_BOARD_ITEMS`` (a directory, or a file named exactly ``items.jsonl``); writers refuse until ``board use`` has run.
* ``store.id`` is a random id written once, when a store directory is first set up, and never rewritten.
* An explicit path that resolves to a different directory than the pointer's refuses every reader and writer
  (:data:`TWO_STORES`).

The lock: ONE ``<store dir>/store.lock``, :func:`store_lock`: exclusive for a write, shared for a read, held across
reading or writing ``store.id`` and ``items.jsonl``, and any further store file. It is NOT held across git evidence
reads. The items file is also flocked on its own descriptor, so a writer that locks only that file excludes this one.

Known limit: git's own writes (checkout, stash, merge) can REPLACE a tracked file by a new inode and take no lock. The
protocol cannot exclude them; what it does is open every file under the lock and re-check that
``(st_dev, st_ino)`` of ``fstat(fd)`` and ``stat(path)`` match (retried a bounded number of times, then refused), so a reader is never left judging a snapshot that
was replaced under it.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import random
import re
import secrets
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from levain.desk import board_chain, work_attach

__all__ = [
    "BoardError", "ITEMS_ENV", "ITEMS_NAME", "LOCK_NAME", "NO_STORE", "POINTER_NAME", "STORE_ID_NAME", "Target",
    "TWO_STORES", "add", "amend", "build_record", "canonical_bytes", "chain_prev", "decide", "next_id", "read_item_lines", "read_pointer",
    "resolve_store", "is_store", "store_lock", "use", "CONFIG_NAME", "read_config", "write_config",
]

ITEMS_ENV = "LEVAIN_BOARD_ITEMS"
ITEMS_NAME = "items.jsonl"
APPEND_FILES = ("items.jsonl", "repos.jsonl")
LOCK_NAME = "store.lock"
POINTER_NAME = "STORE"
STORE_ID_NAME = "store.id"
CONFIG_NAME = "board.json"
NO_STORE = "no board store: run `levain board use`"
TWO_STORES = "two stores: --items names {a}, this install's store is {b} (levain board use)"
OPEN_RETRIES = 5

DONE_KINDS = ("merged", "document", "band", "run")
TIERS = ("sonnet", "opus", "haiku")
DECISIONS = ("cut", "parked", "out-of-1.0", "reopen")
CLEARABLE = ("done", "tier", "depends")
ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]*#[1-9][0-9]*")      # no leading zero: `#007` and `#7` must never be two ids
REPO_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")                # lowercase only: `flow` and `Flow` must never be two counters
NP_RE = re.compile(r"np-[0-9a-f]{8}")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
HASH_RE = re.compile(r"[0-9a-f]{64}")
AUTHOR_KINDS = ("operator", "agent")
GRADE_ASSERTED = "asserted"
SIGNED_CHAIN_MOVED = "the board changed while you signed: sign again"
GRADE_RELAYED = "relayed"


class BoardError(RuntimeError):
    """A read or write the store refuses. Nothing was written."""


# ----------------------------------------------------------------------------------------------------------- location


@dataclass(frozen=True)
class Target:
    """Where one command reads or writes: the store directory, its items file, and whether a pointer named it."""

    store_dir: Path
    items_path: Path
    pointered: bool


def _home_dir(environ: Mapping[str, str] | None) -> Path | None:
    from levain.home_guard import RealHomeReached
    try:
        return work_attach.board_store_dir(environ)
    except RealHomeReached:             # a test or scratch run with no levain home has no pointer, and never reads the real one
        return None


def read_pointer(environ: Mapping[str, str] | None = None) -> Path | None:
    """The store directory the pointer names (resolved), or None when there is no pointer."""
    home = _home_dir(environ)
    if home is None:
        return None
    try:
        raw = (home / POINTER_NAME).read_text("utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise BoardError(f"the store pointer {home / POINTER_NAME} cannot be read ({type(exc).__name__}: {exc})") from None
    line = raw[:-1] if raw.endswith("\n") else raw          # exactly one terminator: a path's own edges are the path
    if not line or "\n" in line or not os.path.isabs(line):
        raise BoardError(f"the store pointer {home / POINTER_NAME} must hold one absolute path; run `levain board use <dir>`")
    return Path(os.path.realpath(line))


def _explicit_dir_and_file(given: Path) -> tuple[Path, Path]:
    """``--items`` names the store DIRECTORY or its ``items.jsonl``. A directory compares as itself; a file must be called
    exactly ``items.jsonl`` and compares by the real path of its parent (so a symlinked items file is the store its parent is)."""
    expanded = Path(given).expanduser()
    p = Path(os.path.realpath(expanded))
    if p.is_dir():
        return p, p / ITEMS_NAME
    if expanded.name != ITEMS_NAME:
        if not os.path.lexists(expanded):
            raise BoardError(f"--items {given}: no such file or directory")
        raise BoardError(f"--items must name a store directory or its {ITEMS_NAME}, not {str(given)!r}")
    parent = Path(os.path.realpath(expanded.parent))
    return parent, parent / ITEMS_NAME


def _require_store_exists(pointed: Path) -> None:
    """A pointer is only good while the directory it names holds a store: never recreate one implicitly."""
    if not pointed.is_dir() or not (pointed / STORE_ID_NAME).exists():
        raise BoardError(f"pointer names {pointed}, which holds no store ({STORE_ID_NAME} missing); run levain board use")


def resolve_store(items: Path | str | None = None, environ: Mapping[str, str] | None = None) -> Target:
    """The target a reader or writer uses. ``items`` (else ``$LEVAIN_BOARD_ITEMS``) is an explicit path; a pointer exists
    -> the explicit path must agree with it, or this raises the two-stores refusal. With no pointer, an explicit path is
    the store as given; with neither, the store is the levain home's board directory."""
    env = os.environ if environ is None else environ
    given = items if items is not None and str(items) != "" else (env.get(ITEMS_ENV) or None)
    pointed = read_pointer(environ)
    if pointed is not None:
        _require_store_exists(pointed)
        if given:
            edir, efile = _explicit_dir_and_file(Path(given))
            if edir != pointed:
                raise BoardError(TWO_STORES.format(a=edir, b=pointed))
            return Target(pointed, efile, True)
        return Target(pointed, pointed / ITEMS_NAME, True)
    if given:
        edir, efile = _explicit_dir_and_file(Path(given))
        return Target(edir, efile, False)
    home = _home_dir(environ)
    if home is None:
        raise BoardError("no items store: pass --items PATH or set " + ITEMS_ENV)
    return Target(home, home / ITEMS_NAME, False)


def is_store(target: Target) -> bool:
    """Whether ``target`` is a REAL store: a pointer names it, or ``store.id`` sits beside it (however it was reached: the pointer,
    ``--items`` or the environment). A bare legacy items file with no ``store.id`` is not one: it has no board config. The ONE
    predicate every reader asks (the state read, the cockpit's board)."""
    return bool(target.pointered or os.path.lexists(target.store_dir / STORE_ID_NAME))


# --------------------------------------------------------------------------------------------------------------- lock


LOCK_WAITING = "the board store is locked by a writer; try again"
LOCK_POLL = 0.005               # seconds between tries of the non-blocking lock (a hold is milliseconds), plus up to as much jitter


class LockWaiting(BoardError):
    """The lock was not free before the caller's deadline. Callers match this TYPE, never its text."""


def _lock_bounded(fd: int, flock_op: int, deadline: float) -> None:
    """``flock`` with a deadline (a ``time.monotonic()`` instant): poll the non-blocking form, raise :class:`LockWaiting`
    once the deadline passes. The poll is not fair to a writer queued in the kernel; the wait is bounded, never silent."""
    while True:
        try:
            fcntl.flock(fd, flock_op | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            left = deadline - time.monotonic()
            if left <= 0:
                raise LockWaiting(LOCK_WAITING) from None
            time.sleep(min(left, LOCK_POLL * (1 + random.random())))


def _deadline_of(wait: float | None) -> float | None:
    return None if wait is None else time.monotonic() + wait


def _open_checked(path: Path, flags: int, flock_op: int | None = None, missing_ok: bool = False,
                  deadline: float | None = None) -> int:
    """Open ``path`` (taking ``flock_op`` when given), then check it is still the file at ``path``: a git operation replaces a
    tracked file by a new inode, and a lock on the unlinked one excludes nobody. Retried a bounded number of times.
    ``missing_ok``: a file that is not there returns -1 instead of refusing. ``deadline`` (a ``time.monotonic()`` instant):
    give up on the lock then (:class:`LockWaiting`), once for every retry pass together; None waits as long as it takes."""
    for _ in range(OPEN_RETRIES):
        try:
            fd = os.open(path, flags, 0o644)
        except FileNotFoundError:
            if missing_ok:
                return -1
            raise BoardError(f"cannot open {path}: no such file") from None
        except OSError as exc:
            raise BoardError(f"cannot open {path}: {exc}") from None
        if flock_op is not None:
            try:
                if deadline is None:
                    fcntl.flock(fd, flock_op)
                else:
                    _lock_bounded(fd, flock_op, deadline)
            except OSError as exc:
                os.close(fd)
                raise BoardError(f"cannot lock {path}: {exc}") from None
            except BaseException:               # LockWaiting, KeyboardInterrupt, anything: the fd never leaks
                os.close(fd)
                raise
        if _same_file(fd, path):
            return fd
        os.close(fd)
    raise BoardError(f"{path} keeps being replaced under the lock (a git operation?); retry")


def _same_file(fd: int, path: Path) -> bool:
    try:
        a, b = os.fstat(fd), os.stat(path)
        return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)
    except OSError:
        return False


def _fsync_dir(path: Path) -> None:
    """Make a created or renamed entry durable: fsync the directory that holds it."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir_or_warn(path: Path, what: str) -> None:
    """The bytes are already durable: a directory fsync that fails is a warning, never a failure of the write."""
    try:
        _fsync_dir(path)
    except OSError as exc:
        print(f"board: {what} was written, but the directory {path} could not be fsynced ({exc})", file=sys.stderr)


class StoreHandle:
    """What :func:`store_lock` yields: opens the store's files while the lock is held."""

    def __init__(self, store_dir: Path, exclusive: bool):
        self.dir = store_dir
        self.exclusive = exclusive

    def open(self, name: str, flags: int, flock_op: int | None = None) -> int:
        """Open ``<store dir>/<name>`` with the inode re-check. The caller closes it."""
        return _open_checked(self.dir / name, flags, flock_op)


@contextmanager
def store_lock(store_dir: Path, *, exclusive: bool, setup: bool = False, wait: float | None = None,
               deadline: float | None = None) -> Iterator[StoreHandle]:
    """Hold ``<store dir>/store.lock``: LOCK_EX when ``exclusive``, else LOCK_SH. Every store file's reader and writer
    uses it. Not held across git evidence reads. A writer never creates the store: under the exclusive lock the directory
    and ``store.id`` must exist, or it refuses (only ``use``, with ``setup``, makes a store). A reader never creates the
    lock: a missing lock means no writer, and it reads without one. ``wait`` (seconds) or ``deadline`` (a ``time.monotonic()``
    instant, so one bound can span several locks) bounds the wait for the lock: past it the call raises :class:`LockWaiting`;
    with neither, the call waits as long as it takes."""
    store_dir = Path(store_dir)
    if deadline is None:
        deadline = _deadline_of(wait)
    if exclusive:
        if not setup:
            _require_store_exists(store_dir)
        try:
            fd = _open_checked(store_dir / LOCK_NAME, os.O_RDWR | os.O_CREAT, fcntl.LOCK_EX, deadline=deadline)
        except BoardError:
            if not setup:
                _require_store_exists(store_dir)            # the store vanished while we opened: say so, not "cannot open"
            raise
        try:
            if not setup:
                _require_store_exists(store_dir)            # re-checked under the lock
            yield StoreHandle(store_dir, True)
        finally:
            os.close(fd)                # closing releases the flock
        return
    fd = _open_checked(store_dir / LOCK_NAME, os.O_RDONLY, fcntl.LOCK_SH, missing_ok=True, deadline=deadline)
    if fd < 0:
        _require_store_exists(store_dir)            # no lock in a store that is gone is a dangling store, not an empty one
        yield StoreHandle(store_dir, False)         # no writer ever ran here (``use`` creates the lock)
        return
    try:
        yield StoreHandle(store_dir, False)
    finally:
        os.close(fd)


# ------------------------------------------------------------------------------------------------------------- reading


def _read_fd(fd: int) -> bytes:
    try:
        size = os.fstat(fd).st_size
        raw = b""
        while len(raw) < size:          # a short pread is legal: read to the size fstat gave
            chunk = os.pread(fd, size - len(raw), len(raw))
            if not chunk:
                raise BoardError("the store shrank while it was read; retry")
            raw += chunk
    except OSError as exc:
        raise BoardError(f"the board store could not be read ({exc})") from None
    return raw


def _read_stable(path: Path, flock_op: int | None = None, deadline: float | None = None) -> bytes:
    """The bytes of ``path``, read from a file that is still the one at ``path`` afterwards; one retry if it was replaced."""
    for _ in range(2):
        fd = _open_checked(path, os.O_RDONLY, flock_op, deadline=deadline)
        try:
            raw = _read_fd(fd)
            if _same_file(fd, path):
                return raw
        finally:
            os.close(fd)
    raise BoardError(f"{path} was replaced while it was read (a git operation?); retry")


def read_item_lines(target: Target, deadline: float | None = None) -> list[str]:
    """The items file's lines, read under the shared lock. The fold decides what they mean (a torn line is a torn board).
    A pointered store with no items file yet reads as empty; an explicit file that is missing raises BoardError. ``deadline``
    (a ``time.monotonic()`` instant) bounds every lock this read takes (:class:`LockWaiting`)."""
    if not target.pointered:
        raw = _read_stable(target.items_path, fcntl.LOCK_SH, deadline)          # no store.lock in a store nobody set up
    else:
        with store_lock(target.store_dir, exclusive=False, deadline=deadline):
            if not target.items_path.exists():
                return []
            raw = _read_stable(target.store_dir / target.items_path.name, fcntl.LOCK_SH, deadline)
    lines = raw.decode("utf-8").split("\n")          # NOT splitlines(): the line numbers and the chain's hashes are the file's own
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _read_records(fd: int) -> list[dict]:
    """Every record of the store, or BoardError on a torn one. Reads the id and op only."""
    return _records_from(_read_fd(fd))


def ack_hint(file: str, n: int) -> str:
    return f'to acknowledge it: levain board ack-torn --file {file} --line {n} --why "..."'


def _records_from(raw: bytes) -> list[dict]:
    """The items file's records (id and op are all the writer reads), or BoardError on a torn line: not JSON, no valid id, a
    shape the fold refuses, or a broken chain. It takes what the item FOLD (:func:`levain.desk.board_state.fold`) judged and
    re-parses nothing: a torn line an ``ack-torn`` drops is skipped, a chain-broken one the ack names is KEPT (a chain ack clears
    only the chain break; a line malformed in itself stays torn until a torn-style ack of it). An ``ack-torn`` record itself
    carries no id and is not returned."""
    if raw and not raw.endswith(b"\n"):
        raise BoardError("the store does not end in a newline (a torn append); repair it before writing")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BoardError(f"the store holds invalid UTF-8 at byte {exc.start}; repair it before writing") from None
    from levain.desk.board_state import fold
    lines = text.split("\n")[:-1] if raw else []          # NOT splitlines(): a record is one \n-terminated line
    folded = fold(lines)
    if folded.torn:
        n = next((ln for ln, _ in folded.torn_lines if ln not in folded.acked), None)
        why = folded.torn_why
        if n is not None and why.startswith(f"line {n}: "):
            why = why[len(f"line {n}: "):]
        where = f" ({n})" if n is not None else ""
        raise BoardError(f"the store holds a torn line{where}: {why}; repair it before writing"
                         + (f", or {ack_hint(ITEMS_NAME, n)}" if n else ""))
    return [dict(r) for r in folded.records]


# ------------------------------------------------------------------------------------------------------------- writing


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _one_line(value, what: str, required: bool = True, verbatim: bool = False) -> str:
    """One line with no control character. ``verbatim`` keeps the edges exactly (his words), else the value is stripped."""
    if value is None or not str(value).strip():
        if required:
            raise BoardError(f"{what} must not be empty")
        return ""
    v = str(value)
    if re.search(r"[\x00-\x1f\x7f\x85  ]", v):       # \x85 and the Unicode separators are line breaks to str.splitlines()
        raise BoardError(f"{what} must be one line with no control characters")
    return v if verbatim else v.strip()


def _real_date(value: str, what: str) -> str:
    if not DATE_RE.fullmatch(value or ""):
        raise BoardError(f"{what} must be YYYY-MM-DD, not {value!r}")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise BoardError(f"{what} {value!r} is not a calendar date") from None
    return value


def _require_path_done(done_kind, done) -> None:
    """A ``document`` or ``run`` row's ``done`` is the PATH of its evidence: one whitespace-free token, since the reader
    keys the whole stripped string into the documents it checked. A missing done stays a capture."""
    if done_kind in ("document", "run") and done is not None and re.search(r"\s", done):
        raise BoardError(f"a {done_kind} row's done must be the path of its evidence (one token, no spaces), not prose: {done!r}; "
                         f"pass --done <path>, or use --done-kind merged|band if the done is a sentence")


def parse_link(spec: str) -> dict:
    """A typed link object from ``item:<id>`` | ``band:np-xxxxxxxx`` | ``link:<ref>`` | ``gate:<YYYY-MM-DD>:<words>``."""
    kind, _, rest = spec.partition(":")
    if kind == "item":
        if not ID_RE.fullmatch(rest):
            raise BoardError(f"item link {rest!r} is not <repo>#<n>")
        return {"kind": "item", "ref": rest}
    if kind == "band":
        if not NP_RE.fullmatch(rest):
            raise BoardError(f"band link {rest!r} is not np-xxxxxxxx")
        return {"kind": "band", "ref": rest}
    if kind == "link":
        if not rest or re.search(r"\s", rest):
            raise BoardError("link ref must be a non-empty ref name with no whitespace")
        return {"kind": "link", "ref": rest}
    if kind == "gate":
        when, _, words = rest.partition(":")
        return {"kind": "gate", "words": _one_line(words, "gate words"), "recheck": _real_date(when, "gate recheck")}
    raise BoardError(f"link {spec!r}: the type must be item, band, link or gate")


def next_id(recs: list[dict], repo: str) -> str:
    top = max((int(r["id"].rpartition("#")[2]) for r in recs if r["id"].rpartition("#")[0] == repo), default=0)
    return f"{repo}#{top + 1}"


def _author(author: Mapping[str, object] | None) -> dict:
    """The record's ``author``: ``{"kind": "operator"|"agent", "name": str[, "launch_key": str]}``. A launch key belongs to an
    agent only. Refuses a missing author, an empty name, another kind, or a launch key on an operator."""
    if not isinstance(author, Mapping):
        raise BoardError("a record needs an author: {kind: operator|agent, name, launch_key?}")
    extra = sorted(str(k) for k in author if k not in ("kind", "name", "launch_key"))
    if extra:
        raise BoardError(f"author carries {', '.join(extra)}, which is not one of kind, name, launch_key")
    for field in ("kind", "name", "launch_key"):
        if field in author and not isinstance(author[field], str):          # never coerced: a number is not a name
            raise BoardError(f"author {field} must be a string, not {type(author[field]).__name__}")
    kind = author.get("kind")
    if kind not in AUTHOR_KINDS:
        raise BoardError(f"author kind {kind!r} is not one of {AUTHOR_KINDS}")
    out = {"kind": kind, "name": _one_line(author.get("name"), "author name")}
    key = author.get("launch_key")
    if key is not None:
        if kind != "agent":
            raise BoardError("a launch key belongs to an agent; an operator carries none")
        out["launch_key"] = _one_line(key, "launch key")
    return out


def _author_for(author: Mapping[str, object] | None, by: str) -> dict:
    """The validated author, checked BEFORE any lock or file is touched, and agreeing with ``by`` (the two never differ)."""
    out = _author(author)
    if out["name"] != by:
        raise BoardError(f"author name {out['name']!r} must equal by {by!r}")
    return out


def build_record(op: str, item_id: str, by: str, body: Mapping[str, object], now: str | None = None, *,
                 author: Mapping[str, object], source: Mapping[str, object] | None = None) -> dict:
    """THE one place a record is built: every verb's record comes out of here, in the key order flow's writer uses, then
    ``author`` and ``grade``. The grade is the writer's, never a caller's: ``asserted`` until a later grade is earned.
    ``prev`` is not here: :func:`_append` stamps it, under the lock, right after ``at``. A body carrying ``author``,
    ``grade``, ``prev``, ``sig`` or ``source`` is refused: those keys are the writer's, never a caller's. ``source`` here
    is the writer's own, from :func:`levain.desk.board_relay.relay_source`: it makes the grade ``relayed`` and is refused on
    an operator (a relay is the desk's act)."""
    taken = sorted(k for k in ("author", "grade", "prev", "sig", "source") if k in body)
    if taken:
        raise BoardError(f"a record body may not carry {', '.join(taken)}: the writer sets those")
    checked = _author_for(author, by)
    rec = {"v": 1, "op": op, "id": item_id, "at": now or _utc_now(), "by": by, **body,
           "author": checked, "grade": GRADE_ASSERTED}
    if source is not None:
        if checked["kind"] != "agent":
            raise BoardError("--source makes a relayed record, and a relay is the desk's act: the author kind must be agent, not operator")
        rec["grade"] = GRADE_RELAYED
        rec["source"] = dict(source)
    return rec


def _read_relay(relay, words) -> dict | None:
    """The ``source`` for a record relaying ``relay`` = ``(np id, band source)``, read NOW (at write time), or None for no
    relay. Refuses an unknown or unanswered row and words that are not a verbatim excerpt of the answer."""
    if relay is None:
        return None
    from levain.desk import board_relay
    np_id, band = relay
    return board_relay.relay_source(np_id, words, band)


def canonical_bytes(rec: Mapping[str, object]) -> bytes:
    """The one serialisation a record is written (and later signed) in: compact separators, ASCII only, the dict's key order.
    No trailing newline."""
    return json.dumps(rec, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def chain_prev(raw: bytes) -> str:
    """The ``prev`` the NEXT record of a file whose bytes are ``raw`` carries: the lowercase hex sha256 of the last line's
    bytes as written (its trailing newline excluded; a torn line's bytes included). A file in which no line carries ``prev``
    yet (empty, or a legacy prefix) gives the sha256 of ALL its bytes: for an empty file that is the sha256 of nothing, and
    for a legacy prefix it is the cutover hash, so the first chained record vouches for every byte before it.

    Every line between newlines is hashed AS WRITTEN: a blank line hashes as empty bytes, a CRLF line hashes its ``\\r``.
    Lines split on ``\\n`` only. A verifier must do the same (not ``splitlines()``, not text mode, no skipping blanks)."""
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    if not lines:
        return hashlib.sha256(raw).hexdigest()
    if _carries_prev(lines[-1]):                # the normal case: one parse, however long the file
        return hashlib.sha256(lines[-1]).hexdigest()
    if any(_carries_prev(line) for line in lines[:-1]):
        return hashlib.sha256(lines[-1]).hexdigest()
    if not raw.endswith(b"\n"):                 # an unterminated legacy tail: ``_append`` completes it with a newline, and the
        raw += b"\n"                            # checker hashes the prefix with each line's newline, so hash the bytes as they will be
    return hashlib.sha256(raw).hexdigest()


def _carries_prev(line: bytes) -> bool:
    """True when ``line`` is a JSON object with a ``prev`` key. A line that cannot be parsed (not JSON, or nested too deep for
    the parser) carries none."""
    rec = board_chain.record_of(line)           # the checker's own parse: a BOM line is judged the same here and there
    return rec is not None and "prev" in rec


def _with_prev(rec: dict, prev: str) -> dict:
    """``rec`` with ``prev`` placed right after ``at``. A record without ``at`` is refused: it is never written unchained."""
    if "at" not in rec:
        raise BoardError("a record needs an at key: it cannot be chained without one")
    out: dict = {}
    for k, v in rec.items():
        if k == "prev":
            continue
        out[k] = v
        if k == "at":
            out["prev"] = prev
    return out


def _write_all(fd: int, payload: bytes) -> None:
    """Write every byte or truncate the store back to where it was and raise: a short write must never leave a torn record."""
    try:
        start = os.fstat(fd).st_size
    except OSError as exc:
        raise BoardError(f"the board store could not be measured before the write ({exc})") from None
    done = 0
    try:
        while done < len(payload):
            n = os.write(fd, payload[done:])
            if n <= 0:
                raise OSError("write returned 0")
            done += n
        os.fsync(fd)
    except OSError as exc:
        try:
            os.ftruncate(fd, start)
            os.fsync(fd)
        except OSError as cut:
            raise BoardError(f"the board store write failed ({exc}) and could not be cut back ({cut}): the store may end in a torn record, repair it") from None
        raise BoardError(f"the board store write failed ({exc}); the store was cut back to its last whole record") from None


def _write_bytes(fd: int, payload: bytes) -> None:
    """Write every byte (a short write is legal), then fsync. Raises OSError."""
    done = 0
    while done < len(payload):
        n = os.write(fd, payload[done:])
        if n <= 0:
            raise OSError("write returned 0")
        done += n
    os.fsync(fd)


def _require_pointered(target: Target) -> None:
    if not target.pointered:
        raise BoardError(NO_STORE)
def _store_seen(store_dir: Path) -> tuple:
    """What a write's checks were judged against, beyond the bytes of the file it appends to: ``(name, dev, inode, size,
    sha256 of the bytes)`` of each store file (``(name,)`` when absent). A build reads BOTH files (the registry for I1, the
    items for ``items_last``, both for a cause), so a change to either between the build and the write sends the write back.
    The bytes, not the mtime: a file only touched (a sync client, ``touch``, a checkout that rewrites the same bytes in place)
    is unchanged, while a replace (another inode), a resize or any other bytes still count (complement 23, fix 4)."""
    out = []
    for name in APPEND_FILES:
        path = store_dir / name
        try:
            fd = os.open(path, os.O_RDONLY)
        except FileNotFoundError:
            out.append((name,))
            continue
        except OSError as exc:
            raise BoardError(f"the board store could not be checked ({exc})") from None
        try:
            st = os.fstat(fd)
            h = hashlib.sha256()
            while chunk := os.read(fd, 1 << 20):
                h.update(chunk)
            digest = h.hexdigest()
        except OSError as exc:
            raise BoardError(f"the board store could not be checked ({exc})") from None
        finally:
            os.close(fd)
        out.append((name, st.st_dev, st.st_ino, st.st_size, digest))
    return tuple(out)


def _seen_changed(before: tuple, after: tuple) -> str:
    """The store files whose identity, size or bytes differ between two :func:`_store_seen` readings, named."""
    names = [b[0] for b, a in zip(before, after) if b != a] if len(before) == len(after) else []
    return " and ".join(names) or "the store"


def _open_existing(path: Path) -> int:
    """The store file at ``path`` opened for append under its own LOCK_EX with the inode re-check, WITHOUT ``O_CREAT``; -1
    when it is not there. A write never creates a store file except by publishing its first record whole
    (:func:`_publish_first`), so a write that ends without one (refused, cancelled, nothing to write, ``prepare``'s capture)
    leaves nothing behind (codex 116, fix 6: the file's existence activates the registry)."""
    return _open_checked(path, os.O_RDWR | os.O_APPEND, fcntl.LOCK_EX, missing_ok=True)


FIRST_TEMP_PREFIX = ".first."          # a dot name that is never one of APPEND_FILES: no store reader opens it


def _seen_entry(seen: tuple, name: str) -> tuple:
    """``name``'s entry in a :func:`_store_seen` reading: ``(name,)`` when the file was absent."""
    return next(e for e in seen if e[0] == name)


def _entry_of(name: str, fd: int, raw: bytes) -> tuple:
    """The :func:`_store_seen` entry for the file held at ``fd`` whose bytes ``raw`` were just read from it."""
    try:
        st = os.fstat(fd)
    except OSError as exc:
        raise BoardError(f"the board store could not be checked ({exc})") from None
    return (name, st.st_dev, st.st_ino, len(raw), hashlib.sha256(raw).hexdigest())


def _publish_first(target: Target, path: Path, rec: dict) -> bool:
    """Make ``rec`` the first record of a store file that does not exist: written and fsynced into a unique temp name in the
    store directory, published at ``path`` by ``link`` (atomic, and never replaces a file there), the temp name unlinked,
    the directory fsynced. A failure before the link (write, fsync, an interrupt) leaves NO file at ``path``: an empty store
    file is never published (an empty ``repos.jsonl`` would activate the registry), and a published path is never cleaned
    up by this writer. Returns False when a file is at ``path`` now (another writer published first; nothing of this write
    is there). The caller holds the exclusive store lock and found the file absent when the record was built (fix 7)."""
    label = rec.get("id") or rec.get("repo") or rec.get("op")
    tmp = target.store_dir / f"{FIRST_TEMP_PREFIX}{path.name}.{os.getpid()}.{secrets.token_hex(8)}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except OSError as exc:
        raise BoardError(f"cannot create the first record of {path} ({exc}); nothing was written") from None
    try:
        try:
            _write_bytes(fd, canonical_bytes(rec) + b"\n")
        except OSError as exc:
            raise BoardError(f"the first record of {path} could not be written ({exc}); nothing was written") from None
        finally:
            os.close(fd)
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        except OSError as exc:
            raise BoardError(f"the first record of {path} could not be published ({exc}); nothing was written") from None
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"board: the temp file {tmp} could not be removed ({exc}); no store reader reads it, delete it", file=sys.stderr)
    _fsync_dir_or_warn(target.store_dir, label)
    return True


def _write_record(target: Target, fd: int, path: Path, raw: bytes, rec: dict, empty: bool) -> None:
    # ensure_ascii: no raw U+2028/2029/0085 reaches the file, so a reader that splits with splitlines() sees the same lines
    label = rec.get("id") or rec.get("repo") or rec.get("op")        # a registry record has no id
    _write_all(fd, (b"\n" if raw and not raw.endswith(b"\n") else b"") + canonical_bytes(rec) + b"\n")
    if not _same_file(fd, path):
        raise BoardError(f"{path} was replaced while {label} was being written (a git operation?): the record may or may not be "
                         f"in the new file; run \"levain board state\" and check before retrying (an add with --key is safe to retry)")
    if empty:
        _fsync_dir_or_warn(target.store_dir, label)


def _append(target: Target, build: Callable[[Any], tuple[dict, bool]], *, filename: str = ITEMS_NAME,
            parse: Callable[[bytes], Any] = _records_from,
            seal: Callable[[dict], dict] | None = None) -> tuple[dict, bool]:
    """Under the exclusive lock, hand ``build(parse(raw))`` the parsed bytes of ``<store dir>/<filename>`` (``parse`` is the
    per-file record reader: ``_records_from`` for the items file, the registry's reader for ``repos.jsonl``; it may raise
    BoardError); ``build`` returns ``(record, write)`` and may raise BoardError. A file that ends in an unterminated line (only
    a ``parse`` that accepts one gets this far: an ``ack-torn`` is written after it) gets a newline first.
    When ``write`` is true the record gets ``prev`` (:func:`chain_prev` of the bytes read in this same locked read) and is
    appended as one line, fsynced. Returns what ``build`` returned, with ``prev`` on it when it was written (a record returned unwritten, an idempotent ``add`` hit, is the stored one and may carry none). Immediately
    before the write (and, for a sealed write, before it leaves for the ceremony) the held fd's size, and the identity, size and
    bytes of BOTH store files (:func:`_store_seen`, what the build's checks read), are compared with what the record was built
    from: a lock-free writer that appended to or replaced either file in between sends this round back to the read (bounded;
    the refusal past the bound names what changed). A race in the remaining window shows up as a chain break, which the
    verifier reports.

    ``seal`` (see :mod:`levain.desk.board_sign`): the record is built and chained under the lock, then the lock is RELEASED for
    the ceremony (``board_sign.ceremony``: a Touch ID prompt or a phone signature is human time, and no reader or other
    writer waits on it), then taken again: the store must be exactly as it was (the file's bytes, both files' identity, size
    and bytes; an mtime alone is no change), else the write is refused (:data:`SIGNED_CHAIN_MOVED`, never a re-read: a second
    prompt is never shown for one write). Under that second lock ``board_sign.earn`` replays the authority log and the signed
    record must verify, or BoardError is raised and nothing is written.

    No open creates a store file: a missing file reads as no bytes (absent is taken from the same :func:`_store_seen`
    reading the write is checked against, fix 7), and a first record is published whole by :func:`_publish_first` (written
    and fsynced under a temp name, then ``link``ed into place, which never replaces a file). A write that ends without a
    record, or fails or is interrupted before the link, leaves no store file and never unlinks a store path (codex 116, fix
    6, fix 7). Absent when built and absent now is unchanged; absent then and present now (or the reverse) is the store
    moving: a file published by another writer first sends an unsigned write back to the read, and refuses a signed one."""
    if filename not in APPEND_FILES:
        raise BoardError(f"{filename!r} is not a store file this writer appends to ({', '.join(APPEND_FILES)})")
    _require_pointered(target)
    path = target.store_dir / filename
    with store_lock(target.store_dir, exclusive=True):
        changed = ""
        for _ in range(OPEN_RETRIES):
            seen = _store_seen(target.store_dir)    # FIRST: absent is this reading's word, and the bytes read must be it
            absent = len(_seen_entry(seen, filename)) == 1      # (gemini, fix 7: never a build on b"" over a file it saw)
            fd = _open_existing(path)               # -1: absent (no O_CREAT: nothing is created before the record)
            try:
                raw = b"" if fd < 0 else _read_fd(fd)
                if (fd < 0) != absent or (not absent and (not _same_file(fd, path)
                                                          or _entry_of(filename, fd, raw) != _seen_entry(seen, filename))):
                    changed = (f"{filename} was created" if absent else f"{filename} was removed" if fd < 0
                               else f"{filename} was replaced (a git operation?)" if not _same_file(fd, path)
                               else f"{filename} changed under a writer that takes no lock")
                    continue                # moved before anything was written: read, open and build again
                rec, write = build(parse(raw))
                if not write:
                    return rec, False
                rec = _with_prev(rec, chain_prev(raw))  # recomputed on every rebuild: it is the bytes of THIS inode
                now_seen = _store_seen(target.store_dir)
                grew = not absent and os.fstat(fd).st_size != len(raw)
                if grew or now_seen != seen:        # seen holds this file too: absent then and present now differs
                    changed = (f"{filename} grew" if grew
                               else f"{_seen_changed(seen, now_seen)} changed") + " under a writer that takes no lock"
                    continue                # a lock-free writer changed a store file since the read: re-read and re-build
                if seal is not None:        # (bounded); checked BEFORE a sealed write leaves for its one prompt (gemini 130)
                    break                   # the ceremony runs with no lock held (below)
                if absent:
                    if not _publish_first(target, path, rec):
                        changed = f"{filename} was created under a writer that takes no lock"
                        continue            # re-read the file that appeared and append after it
                    return rec, True
                _write_record(target, fd, path, raw, rec, not raw)
                return rec, True
            finally:
                if fd >= 0:
                    os.close(fd)
        else:
            raise BoardError(f"the store changed between the read and the write {OPEN_RETRIES} times (the last time: "
                             f"{changed}); nothing was written, retry")

    from levain.desk import board_sign
    sealed = board_sign.ceremony(seal, rec)         # signs the bytes that include prev; raises on any failure; NO lock held
    with store_lock(target.store_dir, exclusive=True):
        fd = _open_existing(path)
        try:
            if (fd < 0) != absent:                  # absent then and present now, or present then and gone now: moved
                raise BoardError(SIGNED_CHAIN_MOVED)
            if (not absent and (not _same_file(fd, path) or _read_fd(fd) != raw)) or _store_seen(target.store_dir) != seen:
                raise BoardError(SIGNED_CHAIN_MOVED)        # moved during the ceremony: never sign twice for one write
            sealed = board_sign.earn(target, seal, sealed)     # the authority log replayed now; the signed record must verify
            if _store_seen(target.store_dir) != seen or (
                    path.exists() if absent else (not _same_file(fd, path) or os.fstat(fd).st_size != len(raw))):
                raise BoardError(SIGNED_CHAIN_MOVED)        # every post-seal mismatch refuses: a re-build would seal again
            if absent:
                if not _publish_first(target, path, sealed):
                    raise BoardError(SIGNED_CHAIN_MOVED)    # a file appeared since the check: built against none, refused
                return sealed, True
            _write_record(target, fd, path, raw, sealed, not raw)
            return sealed, True
        finally:
            if fd >= 0:
                os.close(fd)


def _check_repo_mode(store_dir: Path, repo: str, key: str | None) -> None:
    """I1, under the exclusive lock: ``add`` only for a repo the registry holds as tracking (keyed to its migration) or tracked."""
    from levain.desk import board_registry
    board_registry.check_add(store_dir, repo, key)


@dataclass(frozen=True)
class StoreLines:
    """The store as the cause check reads it: each file's lines AS WRITTEN (split on ``\\n`` only, the final empty piece
    dropped, blank lines kept, so a line's index is its place in its own file and the bytes before it are exactly
    ``b"".join(l + b"\\n" for l in lines[:i])``), whether ``repos.jsonl`` ends in an unterminated line, and the store's id.
    The two files are two chains (design 7.4, P1): nothing here is ever indexed across them."""
    items: tuple[bytes, ...]
    repos: tuple[bytes, ...]
    store_id: str
    repos_unterminated: bool = False
    items_unterminated: bool = False          # the items file ends in an unterminated line: the cause check reads it torn


def _split_written(raw: bytes) -> tuple[tuple[bytes, ...], bool]:
    parts = raw.split(b"\n")
    tail = parts[-1] != b""
    if not tail:
        parts.pop()
    return tuple(parts), tail


def read_store_id(store_dir: Path) -> str:
    """The store's ``store.id`` (stripped). A store without one, or with an empty one, refuses."""
    p = Path(store_dir) / STORE_ID_NAME
    if not p.exists():
        raise BoardError(f"{store_dir} holds no {STORE_ID_NAME}; run `levain board use`")
    sid = _read_stable(p).decode("utf-8", "replace").strip()
    if not sid:
        raise BoardError(f"{p} is empty; run `levain board use`")
    return sid


def _store_lines(target: Target) -> StoreLines:
    """Both store files' lines as written, and the store id, for the cause check. Called from a ``build`` (or ``earn``) that
    runs under the exclusive lock, so it takes none of its own. A missing file reads as no lines."""
    got: dict[str, tuple[tuple[bytes, ...], bool]] = {}
    for name in APPEND_FILES:
        try:
            got[name] = _split_written(_read_stable(target.store_dir / name))
        except BoardError:
            if (target.store_dir / name).exists():
                raise
            got[name] = ((), False)
    return StoreLines(got["items.jsonl"][0], got["repos.jsonl"][0], read_store_id(target.store_dir), got["repos.jsonl"][1],
                      got["items.jsonl"][1])


def _with_store(rec: dict, store_id: str) -> dict:
    """``rec`` naming ``store``. A registry record carries it already (after ``v``, from its builder). An item record gets it
    LAST (after ``grade``; ``sig`` follows it), so everything before it stays the bytes flow's writer wrote."""
    return rec if "store" in rec else {**rec, "store": store_id}


def _graded(target: Target, rec: dict, seal, authority=None, *, decision: str = "") -> dict:
    """The writer's word on a built record, before ``prev`` and before any prompt: the grade the seal's ``via`` claims
    (``asserted`` with none; a claimed grade is earned only by ``board_sign.earn`` after the ceremony), a signed record's
    author must be the operator, and the fixed policy (:func:`levain.desk.board_sign.require_policy`) must accept it. A
    SIGNED record names its store: ``store`` (this store's ``store.id``) is put on it, last (a registry record has it
    already), so the signature binds the record to this store and a signed line copied or a phone signature replayed into
    another store is refused there (``earn`` refuses a ``store`` that is not this one). A record carrying ``cause`` has
    it checked here against the authority log read now (the seal's source, else ``authority``): for an unsigned record no
    blocking step follows this check; a sealed one is checked again by ``earn`` against the log replayed after the
    ceremony. ``decision`` is ``require_policy``'s (a state-keyed decision-class write)."""
    from levain.desk import board_sign
    if seal is not None:
        board_sign.check_signable(rec)
        rec = {**_with_store(rec, read_store_id(target.store_dir)), "grade": seal.grade}
    grade = rec["grade"]
    store, snapshot = None, None
    if "cause" in rec and rec["op"] == "decide" and rec.get("decision") == "cut":
        store = _store_lines(target)
        source = seal.authority if seal is not None else authority
        snapshot = board_sign.read_authority(source) if source is not None else None
    board_sign.require_policy(rec["op"], grade, rec, store=store, snapshot=snapshot, decision=decision)
    return rec


def _preflight(seal, author: Mapping[str, object] | None = None) -> None:
    """A seal's refusals that need no lock, before it is taken: the author must be the operator (``board_sign.check_signable``,
    up front even for a write that will build nothing: gemini 136, fix 4; a missing author refuses as ``prepare`` does: gemini
    168, fix 6), then the seal's own ``preflight``."""
    if seal is not None:
        from levain.desk import board_sign
        board_sign.check_signable({"author": _author(author)})
        seal.preflight()


def add(target: Target, repo, title, origin, by, words="", done=None, done_kind="merged", tier=None, depends=(),
        key=None, payload_hash=None, now=None, *, author, seal=None, relay=None) -> tuple[str, bool]:
    """Append an ``add``; returns ``(id, created)``. With ``key`` and ``payload_hash``: the same key with the same hash
    returns the existing id and writes nothing; the same key with another hash refuses. A key never expires."""
    repo = _one_line(repo, "repo")
    if not REPO_RE.fullmatch(repo):
        raise BoardError(f"repo {repo!r} must match {REPO_RE.pattern}")
    if done_kind not in DONE_KINDS:
        raise BoardError(f"done_kind {done_kind!r} is not one of {DONE_KINDS}")
    if tier is not None and tier not in TIERS:
        raise BoardError(f"tier {tier!r} is not one of {TIERS}")
    fields = {"title": _one_line(title, "title"), "words": str(words or ""), "origin": _one_line(origin, "origin"),
              "done": _one_line(done, "done", required=False) or None, "done_kind": done_kind, "tier": tier,
              "depends_on": [parse_link(d) for d in depends]}
    _require_path_done(done_kind, fields["done"])
    by = _one_line(by, "by")
    author = _author_for(author, by)
    if (key is None) != (payload_hash is None):
        raise BoardError("--key and --payload-hash go together")
    if key is not None:
        key = _one_line(key, "key")
        if not HASH_RE.fullmatch(payload_hash or ""):
            raise BoardError(f"payload hash {payload_hash!r} is not a lowercase sha256 hex digest")
        fields = {**fields, "key": key, "payload_hash": payload_hash}

    def build(recs):
        _check_repo_mode(target.store_dir, repo, key)
        if key is not None:
            for r in recs:
                if r.get("op") == "add" and r.get("key") == key:
                    if r.get("repo") != repo:
                        raise BoardError(f"key {key!r} was already used for {r['id']} in repo {r.get('repo')!r}; this add is for repo {repo!r}")
                    if r.get("payload_hash") == payload_hash:
                        return r, False
                    raise BoardError(f"key {key!r} was already used for {r['id']} with payload hash {r.get('payload_hash')}; "
                                     f"this add carries {payload_hash}")
        source = _read_relay(relay, fields["words"])
        return _graded(target, build_record("add", next_id(recs, repo), by, {"repo": repo, **fields}, now, author=author, source=source), seal), True

    _preflight(seal, author)
    rec, written = _append(target, build, seal=seal)
    return rec["id"], written


def _require_known(recs: list[dict], item_id: str) -> None:
    if not ID_RE.fullmatch(item_id or ""):
        raise BoardError(f"id {item_id!r} is not <repo>#<n>")
    if not any(r.get("op") == "add" and r["id"] == item_id for r in recs):
        raise BoardError(f"{item_id} was never added to the store; add it first (an amend or decide for it would only be a problem line)")


def _current_done(recs: list[dict], item_id: str) -> tuple:
    """(done_kind, done) as the add and later amends of ``item_id`` leave them: last write wins, two values for the
    write-time path check."""
    kind, done = "merged", None
    for r in recs:
        if r.get("id") != item_id:
            continue
        src = r if r.get("op") == "add" else (r.get("fields") or {}) if r.get("op") == "amend" else {}
        if "done_kind" in src:
            kind = src["done_kind"]
        if "done" in src:
            done = src["done"]
    return kind, done


def amend(target: Target, item_id, why, by, fields: dict, now=None, *, author, seal=None, relay=None) -> str:
    """``fields`` replaces those fields: title, words, done, done_kind, tier, depends_on (a list of link specs; None clears
    done/tier). Any other key is refused, so there is no way to store a status."""
    allowed = {"title", "words", "done", "done_kind", "tier", "depends_on"}
    if not fields or set(fields) - allowed:
        raise BoardError(f"amend needs at least one of {sorted(allowed)}; got {sorted(fields)}")
    out = {}
    for k, v in fields.items():
        if k == "title":
            out[k] = _one_line(v, "title")
        elif k == "words":
            out[k] = str(v or "")
        elif k == "done":
            out[k] = _one_line(v, "done", required=False) or None
        elif k == "done_kind":
            if v not in DONE_KINDS:
                raise BoardError(f"done_kind {v!r} is not one of {DONE_KINDS}")
            out[k] = v
        elif k == "tier":
            if v is not None and v not in TIERS:
                raise BoardError(f"tier {v!r} is not one of {TIERS}")
            out[k] = v
        else:
            if not isinstance(v, list) or not all(isinstance(d, str) for d in v):
                raise BoardError("depends_on takes a list of typed link specs (item:<id>, band:np-xxxxxxxx, link:<ref>, gate:<date>:<words>); [] clears it")
            out[k] = [parse_link(d) for d in v]
    why, by = _one_line(why, "why"), _one_line(by, "by")
    author = _author_for(author, by)
    if relay is not None and "words" not in out:
        raise BoardError("a relayed amend must carry --words: the verbatim excerpt of the answer it relays")

    def build(recs):
        _require_known(recs, item_id)
        if "done" in out or "done_kind" in out:         # judge the RESULTING row, only when this amend touches the pair
            now_kind, now_done = _current_done(recs, item_id)
            _require_path_done(out.get("done_kind", now_kind), out["done"] if "done" in out else now_done)
        source = _read_relay(relay, out.get("words"))
        return _graded(target, build_record("amend", item_id, by, {"fields": out, "why": why}, now, author=author, source=source), seal), True

    _preflight(seal, author)
    return _append(target, build, seal=seal)[0]["id"]


def decide(target: Target, item_id, decision, words, by, when=None, now=None, *, author, cause=None, seal=None,
           authority=None, relay=None) -> str:
    """Append a ``decide``. ``cause`` (a sha256) is the hash of the registry record this decision derives from (an
    ``abandon-track``'s cuts carry it); it is written after ``date`` only when given. A cut's cause (P2-d) is checked against
    ``authority``, an authority SOURCE read under the lock (``board_sign.resolve_authority``); a sealed decide checks it
    against the seal's source instead."""
    if decision not in DECISIONS:
        raise BoardError(f"decision {decision!r} is not one of {DECISIONS}")
    words, by = _one_line(words, "words", verbatim=True), _one_line(by, "by")
    author = _author_for(author, by)
    when = _real_date(when or (now or _utc_now())[:10], "date")
    body = {"decision": decision, "words": words, "date": when}
    if cause is not None:
        if not isinstance(cause, str) or not HASH_RE.fullmatch(cause):
            raise BoardError(f"cause {cause!r} is not a lowercase sha256 hex digest")
        body["cause"] = cause

    def build(recs):
        _require_known(recs, item_id)
        source = _read_relay(relay, words)
        return _graded(target, build_record("decide", item_id, by, body, now, author=author, source=source), seal, authority), True

    _preflight(seal, author)
    return _append(target, build, seal=seal)[0]["id"]


def known_ids(target: Target) -> set[str]:
    """The ids the store has added. Raises BoardError on a torn store."""
    if not target.items_path.exists():
        return set()
    with store_lock(target.store_dir, exclusive=False):
        raw = _read_stable(target.store_dir / target.items_path.name, fcntl.LOCK_SH)
    return {r["id"] for r in _records_from(raw) if r.get("op") == "add"}


# ------------------------------------------------------------------------------------------------------------- use


def _write_store_id_once(held: StoreHandle) -> None:
    path = held.dir / STORE_ID_NAME
    fd = held.open(STORE_ID_NAME, os.O_RDWR | os.O_CREAT)
    try:
        if _read_fd(fd).strip():
            return
        try:
            _write_bytes(fd, (secrets.token_hex(16) + "\n").encode("ascii"))
        except OSError:
            try:
                os.unlink(path)                 # a store with a half-written identity is no store: undo the creation
            except OSError:
                pass
            raise
    finally:
        os.close(fd)
    _fsync_dir_or_warn(held.dir, STORE_ID_NAME)


def _write_pointer(home: Path, store_dir: Path) -> None:
    """Atomic replace: a temp file in the same directory, fully written and fsynced, renamed over the pointer, directory fsynced."""
    home.mkdir(parents=True, exist_ok=True)
    tmp = home / f".{POINTER_NAME}.{os.getpid()}.{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        try:
            _write_bytes(fd, (str(store_dir) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        target = home / POINTER_NAME
        if target.is_dir() and not target.is_symlink():
            target.rmdir()                      # a directory where the pointer belongs: only an empty one is repaired
        os.replace(tmp, target)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir_or_warn(home, "the store pointer")


def _config_doc(producers: Any, no_land_waiter: Any) -> dict[str, Any]:
    from levain.desk import board_producers as bp

    names = list(producers or ())
    if not all(isinstance(n, str) and n and "\n" not in n for n in names):
        raise BoardError("the board config's producers must be non-empty one-line names")
    if bp.NATIVE_PRODUCER in names:      # Levain's own producer is always read: naming it would fail every read of the board, at write and at read alike
        raise BoardError(f"the board config's producers name {bp.NATIVE_PRODUCER!r}, Levain's own producer, which is always read; name an external one")
    return {"config_v": 1, "producers": list(dict.fromkeys(names)), "no_land_waiter": bool(no_land_waiter)}


def read_config(store_dir: Path | str) -> dict[str, Any]:
    """The board config ``levain board use --producer/--no-land-waiter`` wrote in the store dir: ``{producers: [..],
    no_land_waiter: bool}``. No file is the default (no external producer named, a land waiter assumed). A file that cannot be
    read or is not this shape raises ``BoardError``: a config that cannot be read is never silently the default."""
    path = Path(store_dir) / CONFIG_NAME
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {"producers": [], "no_land_waiter": False}
    except OSError as exc:
        raise BoardError(f"the board config {path} cannot be read: {exc}") from None
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):      # a deeply nested file is a refusal, never a traceback
        raise BoardError(f"the board config {path} is not JSON") from None
    if (not isinstance(doc, dict) or doc.get("config_v") != 1 or not isinstance(doc.get("producers"), list)
            or not isinstance(doc.get("no_land_waiter"), bool)):
        raise BoardError(f"the board config {path} is not a v1 config (config_v, producers, no_land_waiter)")
    try:
        clean = _config_doc(doc["producers"], doc["no_land_waiter"])
    except BoardError as exc:
        raise BoardError(f"the board config {path} cannot be used: {exc}") from None
    return {"producers": clean["producers"], "no_land_waiter": clean["no_land_waiter"]}


def write_config(store_dir: Path | str, producers: Any, no_land_waiter: Any) -> bool:
    """Write the board config atomically (a temp file in the store dir, fsynced, renamed, directory fsynced). Returns whether the
    config changed. The store dir is locked while it is written."""
    store_dir = Path(store_dir)
    doc = _config_doc(producers, no_land_waiter)
    payload = (json.dumps(doc, indent=2) + "\n").encode("utf-8")
    try:
        with store_lock(store_dir, exclusive=True, setup=True):
            target = store_dir / CONFIG_NAME
            try:
                if target.read_bytes() == payload:
                    return False
            except OSError:
                pass
            tmp = store_dir / f".{CONFIG_NAME}.{os.getpid()}.{secrets.token_hex(4)}"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            try:
                try:
                    _write_bytes(fd, payload)
                finally:
                    os.close(fd)
                os.replace(tmp, target)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            _fsync_dir_or_warn(store_dir, CONFIG_NAME)
    except OSError as exc:
        raise BoardError(f"cannot write the board config in {store_dir}: {exc}") from None
    return True


def use(directory: Path | str, environ: Mapping[str, str] | None = None,
        before_pointer: Callable[[Path], bool] | None = None) -> tuple[Path, bool]:
    """``levain board use <dir>``: make ``<dir>`` this install's store. Creates it, writes ``store.id`` if absent, and writes
    the pointer LAST. Returns ``(resolved dir, changed)``; the same dir again changes nothing. A pointer that cannot be
    read counts as a different one and is replaced. ``before_pointer(store_dir)`` (the board config write; returns whether it
    changed anything) runs once the store exists and BEFORE the pointer is published: a reader that sees the new pointer sees
    the config with it, and a refusal from it leaves the pointer where it was."""
    home = _home_dir(environ)
    if home is None:
        raise BoardError("cannot place the store pointer: no levain home (set LEVAIN_HOME)")
    store_dir = Path(os.path.realpath(Path(directory).expanduser()))
    if "\n" in str(store_dir):
        raise BoardError(f"the store path {str(store_dir)!r} holds a newline; the one-line pointer cannot name it")
    try:
        store_dir.mkdir(parents=True, exist_ok=True)
        with store_lock(store_dir, exclusive=True, setup=True) as held:
            _write_store_id_once(held)
    except OSError as exc:
        raise BoardError(f"cannot set up the store at {store_dir}: {exc}") from None
    configured = before_pointer(store_dir) if before_pointer is not None else False
    try:
        current = read_pointer(environ)
    except BoardError:
        current = None
    if current == store_dir:
        return store_dir, configured
    try:
        _write_pointer(home, store_dir)
    except OSError as exc:
        raise BoardError(f"cannot write the store pointer in {home}: {exc}") from None
    return store_dir, True
