"""levain.desk.board_store — the board's item store: where it lives, the one lock over it, and the writer.

The writer is a port of flow's ``scripts/board.py``: the same records (``json.dumps(rec, separators=(",", ":"))``,
ASCII only), the same refusals, the same id allocation under the lock. Item STATE is never stored; this module writes
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
protocol cannot exclude them; what it does is open every file under the lock and re-check ``fstat(fd).st_ino ==
stat(path).st_ino`` (retried a bounded number of times, then refused), so a reader is never left judging a snapshot that
was replaced under it.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Mapping

from levain.desk import work_attach

__all__ = [
    "BoardError", "ITEMS_ENV", "ITEMS_NAME", "LOCK_NAME", "NO_STORE", "POINTER_NAME", "STORE_ID_NAME", "Target",
    "TWO_STORES", "add", "amend", "build_record", "decide", "next_id", "read_item_lines", "read_pointer",
    "read_store_id", "resolve_store", "store_lock", "use",
]

ITEMS_ENV = "LEVAIN_BOARD_ITEMS"
ITEMS_NAME = "items.jsonl"
LOCK_NAME = "store.lock"
POINTER_NAME = "STORE"
STORE_ID_NAME = "store.id"
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


# --------------------------------------------------------------------------------------------------------------- lock


def _open_checked(path: Path, flags: int, flock_op: int | None = None) -> int:
    """Open ``path`` (taking ``flock_op`` when given), then check it is still the file at ``path``: a git operation replaces a
    tracked file by a new inode, and a lock on the unlinked one excludes nobody. Retried a bounded number of times."""
    for _ in range(OPEN_RETRIES):
        try:
            fd = os.open(path, flags, 0o644)
        except OSError as exc:
            raise BoardError(f"cannot open {path}: {exc}") from None
        if flock_op is not None:
            try:
                fcntl.flock(fd, flock_op)
            except OSError as exc:
                os.close(fd)
                raise BoardError(f"cannot lock {path}: {exc}") from None
        try:
            same = os.fstat(fd).st_ino == os.stat(path).st_ino
        except OSError:
            same = False
        if same:
            return fd
        os.close(fd)
    raise BoardError(f"{path} keeps being replaced under the lock (a git operation?); retry")


def _same_file(fd: int, path: Path) -> bool:
    try:
        return os.fstat(fd).st_ino == os.stat(path).st_ino
    except OSError:
        return False


def _fsync_dir(path: Path) -> None:
    """Make a created or renamed entry durable: fsync the directory that holds it."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class StoreHandle:
    """What :func:`store_lock` yields: opens the store's files while the lock is held."""

    def __init__(self, store_dir: Path, exclusive: bool):
        self.dir = store_dir
        self.exclusive = exclusive

    def open(self, name: str, flags: int, flock_op: int | None = None) -> int:
        """Open ``<store dir>/<name>`` with the inode re-check. The caller closes it."""
        return _open_checked(self.dir / name, flags, flock_op)


@contextmanager
def store_lock(store_dir: Path, *, exclusive: bool) -> Iterator[StoreHandle]:
    """Hold ``<store dir>/store.lock``: LOCK_EX when ``exclusive``, else LOCK_SH. Every store file's reader and writer
    uses it. Not held across git evidence reads."""
    store_dir = Path(store_dir)
    if exclusive:
        try:
            store_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BoardError(f"cannot create the board store directory {store_dir}: {exc}") from None
    elif not (store_dir / LOCK_NAME).exists():
        yield StoreHandle(store_dir, False)         # no writer ever ran here (``use`` creates the lock): a read must not create it
        return
    fd = _open_checked(store_dir / LOCK_NAME, os.O_RDWR | os.O_CREAT, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    try:
        yield StoreHandle(store_dir, exclusive)
    finally:
        os.close(fd)                    # closing releases the flock


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


def _read_stable(path: Path, flock_op: int | None = None) -> bytes:
    """The bytes of ``path``, read from a file that is still the one at ``path`` afterwards; one retry if it was replaced."""
    for _ in range(2):
        fd = _open_checked(path, os.O_RDONLY, flock_op)
        try:
            raw = _read_fd(fd)
            if _same_file(fd, path):
                return raw
        finally:
            os.close(fd)
    raise BoardError(f"{path} was replaced while it was read (a git operation?); retry")


def read_item_lines(target: Target) -> list[str]:
    """The items file's lines, read under the shared lock. The fold decides what they mean (a torn line is a torn board).
    A pointered store with no items file yet reads as empty; an explicit file that is missing raises BoardError."""
    if not target.pointered:
        raw = _read_stable(target.items_path, fcntl.LOCK_SH)          # no store.lock in a store nobody set up
    else:
        with store_lock(target.store_dir, exclusive=False):
            if not target.items_path.exists():
                return []
            raw = _read_stable(target.store_dir / target.items_path.name, fcntl.LOCK_SH)
    return raw.decode("utf-8").splitlines()


def read_store_id(target: Target) -> str | None:
    """The store's identity, or None when it has none yet."""
    with store_lock(target.store_dir, exclusive=False):
        if not (target.store_dir / STORE_ID_NAME).exists():
            return None
        return _read_stable(target.store_dir / STORE_ID_NAME).decode("utf-8", "replace").strip() or None


def _read_records(fd: int) -> list[dict]:
    """Every record of the store, or BoardError on a torn one. Reads the id and op only."""
    return _records_from(_read_fd(fd))


def _records_from(raw: bytes) -> list[dict]:
    if raw and not raw.endswith(b"\n"):
        raise BoardError("the store does not end in a newline (a torn append); repair it before writing")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BoardError(f"the store holds invalid UTF-8 at byte {exc.start}; repair it before writing") from None
    recs = []
    for n, line in enumerate(text.split("\n"), 1):          # NOT splitlines(): a record is one \n-terminated line
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            raise BoardError(f"the store holds a torn line ({n}): not JSON; repair it before writing") from None
        if not isinstance(rec, dict) or not isinstance(rec.get("id"), str) or not ID_RE.fullmatch(rec["id"]):
            raise BoardError(f"the store holds a torn line ({n}): no valid id; repair it before writing")
        recs.append(rec)
    return recs


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


def build_record(op: str, item_id: str, by: str, body: Mapping[str, object], now: str | None = None) -> dict:
    """THE one place a record is built: every verb's record comes out of here, in the key order flow's writer uses."""
    return {"v": 1, "op": op, "id": item_id, "at": now or _utc_now(), "by": by, **body}


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


def _append(target: Target, build: Callable[[list[dict]], tuple[dict, bool]]) -> tuple[dict, bool]:
    """Under the exclusive lock, hand ``build(records)`` the store's records; it returns ``(record, write)`` and may raise
    BoardError. When ``write`` is true the record is appended as one line and fsynced. Returns what ``build`` returned."""
    _require_pointered(target)
    path = target.store_dir / target.items_path.name
    with store_lock(target.store_dir, exclusive=True) as held:
        for _ in range(OPEN_RETRIES):
            created = not path.exists()
            fd = held.open(target.items_path.name, os.O_RDWR | os.O_CREAT | os.O_APPEND, fcntl.LOCK_EX)
            try:
                recs = _read_records(fd)
                rec, write = build(recs)
                if write:
                    # ensure_ascii: no raw U+2028/2029/0085 reaches the file, so a reader that splits with splitlines() sees the same lines
                    _write_all(fd, (json.dumps(rec, separators=(",", ":")) + "\n").encode("ascii"))
                if _same_file(fd, path):
                    if write and created:
                        try:
                            _fsync_dir(target.store_dir)
                        except OSError as exc:
                            raise BoardError(f"{rec['id']} was written but the store directory could not be fsynced ({exc})") from None
                    return rec, write
            finally:
                os.close(fd)
            # the file was replaced under the lock (a git operation): the record, if any, is in the unlinked inode.
            # Open the current file, read it again, build again (id and idempotency key judged against the new contents).
        raise BoardError(f"{path} was replaced while a write was in flight (a git operation?) {OPEN_RETRIES} times; nothing was kept, retry")


def _check_repo_mode(repo: str) -> None:
    """Hook point: where a repo-mode check on ``add`` goes. No behaviour."""


def add(target: Target, repo, title, origin, by, words="", done=None, done_kind="merged", tier=None, depends=(),
        key=None, payload_hash=None, now=None) -> tuple[str, bool]:
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
    if (key is None) != (payload_hash is None):
        raise BoardError("--key and --payload-hash go together")
    if key is not None:
        key = _one_line(key, "key")
        if not HASH_RE.fullmatch(payload_hash or ""):
            raise BoardError(f"payload hash {payload_hash!r} is not a lowercase sha256 hex digest")
        fields = {**fields, "key": key, "payload_hash": payload_hash}
    _check_repo_mode(repo)

    def build(recs):
        if key is not None:
            for r in recs:
                if r.get("op") == "add" and r.get("key") == key:
                    if r.get("repo") != repo:
                        raise BoardError(f"key {key!r} was already used for {r['id']} in repo {r.get('repo')!r}; this add is for repo {repo!r}")
                    if r.get("payload_hash") == payload_hash:
                        return r, False
                    raise BoardError(f"key {key!r} was already used for {r['id']} with payload hash {r.get('payload_hash')}; "
                                     f"this add carries {payload_hash}")
        return build_record("add", next_id(recs, repo), by, {"repo": repo, **fields}, now), True

    rec, written = _append(target, build)
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


def amend(target: Target, item_id, why, by, fields: dict, now=None) -> str:
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

    def build(recs):
        _require_known(recs, item_id)
        if "done" in out or "done_kind" in out:         # judge the RESULTING row, only when this amend touches the pair
            now_kind, now_done = _current_done(recs, item_id)
            _require_path_done(out.get("done_kind", now_kind), out["done"] if "done" in out else now_done)
        return build_record("amend", item_id, by, {"fields": out, "why": why}, now), True

    return _append(target, build)[0]["id"]


def decide(target: Target, item_id, decision, words, by, when=None, now=None) -> str:
    if decision not in DECISIONS:
        raise BoardError(f"decision {decision!r} is not one of {DECISIONS}")
    words, by = _one_line(words, "words", verbatim=True), _one_line(by, "by")
    when = _real_date(when or (now or _utc_now())[:10], "date")

    def build(recs):
        _require_known(recs, item_id)
        return build_record("decide", item_id, by, {"decision": decision, "words": words, "date": when}, now), True

    return _append(target, build)[0]["id"]


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
    _fsync_dir(held.dir)


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
    _fsync_dir(home)


def use(directory: Path | str, environ: Mapping[str, str] | None = None) -> tuple[Path, bool]:
    """``levain board use <dir>``: make ``<dir>`` this install's store. Creates it, writes ``store.id`` if absent, and writes
    the pointer LAST. Returns ``(resolved dir, changed)``; the same dir again changes nothing. A pointer that cannot be
    read counts as a different one and is replaced."""
    home = _home_dir(environ)
    if home is None:
        raise BoardError("cannot place the store pointer: no levain home (set LEVAIN_HOME)")
    store_dir = Path(os.path.realpath(Path(directory).expanduser()))
    if "\n" in str(store_dir):
        raise BoardError(f"the store path {str(store_dir)!r} holds a newline; the one-line pointer cannot name it")
    try:
        with store_lock(store_dir, exclusive=True) as held:
            _write_store_id_once(held)
    except OSError as exc:
        raise BoardError(f"cannot set up the store at {store_dir}: {exc}") from None
    try:
        current = read_pointer(environ)
    except BoardError:
        current = None
    if current == store_dir:
        return store_dir, False
    try:
        _write_pointer(home, store_dir)
    except OSError as exc:
        raise BoardError(f"cannot write the store pointer in {home}: {exc}") from None
    return store_dir, True
