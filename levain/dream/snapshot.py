"""levain.dream.snapshot (D1-1b): the pre-save snapshot, the post-save record, one-step rollback, retention and the drill.

The rules are flow's ``dream_snapshot.py`` rules, not its code (D1_EXECUTOR §3): a rollback refuses per FILE, verifies
every snapshot file before it touches the store, keeps what it replaces, and a rerun finishes an interrupted one.

Two rollbacks, chosen by the public surface of the installed anneal-memory (``callable(Store.revert_wrap)``):

* WITH ``revert_wrap``: the rollback verifies the snapshot, then asks anneal to undo the wrap. The day's episodes are
  offered to the next dream. On a store that keeps its identity in the db anneal restores the identity itself (and
  re-projects the files), so Levain does not write the text; when anneal restored none (``identity_version`` is
  None) Levain restores the text after the revert. A rerun finds one of three states in rollback.json
  (``_rollback_gen3``): no call started, a call started with no receipt, or a receipt.
* WITHOUT it (anneal <= 1.0.2, format 2): the memory TEXT only. The wrap's db rows stay, so the day's episodes still
  count as consolidated and are not offered to the next dream; every rollback's output says so.

Layout, under one occurrence dir ``<root>/<occurrence id>/``:

* ``snap/manifest.json`` and the snapshot files (text tier + ``memory.db.backup``), written by :func:`take`;
* ``reverted-from/`` (what a rollback replaced) and ``rollback.json``, written by :func:`rollback`.

Nothing here touches a store it was not given. ``take`` and ``record_post`` run inside the dream's effect, which
already holds the store lock; ``rollback`` and ``drill`` take that lock themselves.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

SNAP_DIR = "snap"
MANIFEST = "manifest.json"
DB_BACKUP = "memory.db.backup"
UNDO_DIR = "reverted-from"
ROLLBACK_RECORD = "rollback.json"
DRILL_RECORD = "drill.json"
#: Snapshots kept for occurrences that consolidated. Only the newest can be rolled back; the one before it is kept for
#: the drill and for reading by hand.
KEEP_SNAPSHOTS = 2
#: The store format with a proven text-only rollback (text files are the authority; the db is never restored).
#: Format 3 (REVERT_FORMAT) is also taken, but only when anneal has revert_wrap (see ``_has_revert_wrap``).
SUPPORTED_FORMAT = 2
DRILL_CADENCE_S = 7 * 24 * 3600
#: The store format whose identity lives in the db: only anneal's ``revert_wrap`` can undo a save on it.
REVERT_FORMAT = 3

LIMIT = ("The memory text is restored. The day's episodes are NOT re-offered: this anneal-memory has no revert_wrap, "
         "so anneal still counts them as consolidated and the next dream starts from the episodes that arrive after "
         "this.")

#: One line of remedy per ``WrapRevertRefused.code`` (printed with the code; never swallowed).
REVERT_REMEDY = {
    "not_latest": "a later dream has saved since; only the newest can be rolled back.",
    "unknown_wrap": "anneal has no such wrap in this store; the snapshot's wrap id does not match it.",
    "wrap_open": "a consolidation is in progress; cancel it first.",
    "identity_moved": "the memory changed after this dream; a rollback would discard that change.",
    "no_prior_identity": "this dream's save was the store's first identity, so there is no earlier text to restore.",
    "episodes_gone": "some of the day's episodes were pruned or deleted, so reverting would discard the only summary "
                     "of them; rerun with --allow-missing to revert anyway.",
    "no_identity_version": "anneal holds no identity version for this wrap (it was recorded outside a save), so "
                           "reverting it would leave its text in the identity.",
}


class SnapshotRefused(Exception):
    """A snapshot (or its post record) could not be made safely; the effect must not go on to prepare."""


# ------------------------------------------------------------------------------------------------------ small helpers

def _now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="microseconds")


def _taken(m: Mapping[str, Any]) -> _dt.datetime:
    """``taken_at`` as an aware instant. Local-offset strings do not sort as text across a DST change; an unreadable
    stamp sorts first (it is never taken to be later than a readable one)."""
    try:
        t = _dt.datetime.fromisoformat(str(m.get("taken_at")))
    except ValueError:
        return _dt.datetime.min.replace(tzinfo=_dt.timezone.utc)
    return t if t.tzinfo else t.astimezone()


def sha256_file(path: Path) -> str | None:
    """sha256 of a file's bytes, or None when it does not exist."""
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def text_tier(db_path: Path | str) -> dict[str, Path]:
    """The three files a save rewrites beside the db, keyed by a stable name."""
    db = Path(db_path)
    return {"continuity": db.parent / f"{db.stem}.continuity.md",
            "meta": db.parent / f"{db.stem}.continuity.meta.json",
            "crystal": db.parent / f"{db.stem}.crystal.json"}


def tier_hashes(db_path: Path | str) -> dict[str, str | None]:
    return {k: sha256_file(p) for k, p in text_tier(db_path).items()}


def format_version(db_path: Path | str) -> int | None:
    """The store's ``metadata.format_version`` by plain sqlite (read-only, no anneal import). 0 = no table or no row;
    None = the read itself failed (the caller must not guess)."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = con.execute("select value from metadata where key = 'format_version'").fetchone()
        finally:
            con.close()
        return int(str(row[0]).strip()) if row else 0
    except sqlite3.OperationalError as exc:
        return 0 if "no such table" in str(exc) else None
    except (sqlite3.Error, ValueError, TypeError):
        return None


def wraps_after(db_path: Path | str, after: int | None) -> list[tuple[int, str | None, str | None]] | None:
    """``(id, content_hash, pair_id)`` of every completed wrap with an id past ``after``, oldest first; None when
    unreadable.

    ``content_hash`` is the plain sha256 of the continuity text that wrap saved (anneal stores it at the commit, the
    recovery oracle). ``pair_id`` is anneal's commit identity, ``<first 12 of the wrap token>-<random>``: a hash alone
    can match two wraps that saved identical text, the pair id cannot. ``Store.get_wrap_history`` exposes neither, so
    the columns are read directly; a legacy row without one reads ``None``."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute("select id, content_hash, pair_id from wraps where id > ? order by id",
                               (after if after is not None else 0,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return [(int(r[0]), r[1], r[2]) for r in rows]


def _write_json(path: Path, doc: Mapping[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")  # two writers never share a temp name
    try:
        tmp.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _atomic_copy(src: Path, dest: Path) -> None:
    tmp = dest.with_name(f".{dest.name}.rb-{uuid.uuid4().hex[:8]}")
    try:
        shutil.copy2(src, tmp)
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())  # the bytes are on disk before the rename makes them the live file
        os.replace(tmp, dest)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _backup_db(src: Path, dest: Path) -> None:
    """sqlite's online backup (WAL-consistent), built under a temp name and renamed, so the final name exists only once
    the backup is whole."""
    part = dest.with_name(f".{dest.name}.part")
    part.unlink(missing_ok=True)
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(str(part))
    try:
        s.backup(d)
        d.execute("pragma journal_mode=delete")  # one self-contained file, no -wal/-shm
    finally:
        d.close()
        s.close()
    with open(part, "rb") as f:
        os.fsync(f.fileno())
    os.replace(part, dest)


def _store_db(store: Any) -> Path:
    return Path(str(store.path)).resolve()


def read_manifest(occ_dir: Path | str) -> dict[str, Any] | None:
    return _read_json(Path(occ_dir) / SNAP_DIR / MANIFEST)


def _write_manifest(occ_dir: Path, doc: Mapping[str, Any]) -> None:
    _write_json(occ_dir / SNAP_DIR / MANIFEST, doc)


# ---------------------------------------------------------------------------------------------------- take (step 2)

def take(store: Any, occ_dir: Path | None) -> dict[str, Any]:
    """``on_snapshot``: copy the text tier and take a WAL-consistent db backup into ``<occ_dir>/snap/``, with a manifest
    (sha256 per file and of the backup, store id, format version, anneal version, occurrence id). Runs inside the
    effect, under the store lock and before ``prepare``. Raises :class:`SnapshotRefused` on anything wrong, leaving
    no half-written snapshot; the executor then returns ``snapshot-refused`` with nothing prepared."""
    if occ_dir is None:
        raise SnapshotRefused("no occurrence dir to hold a snapshot")
    occ_dir = Path(occ_dir)
    db = _store_db(store)
    fmt = format_version(db)
    # take/rollback symmetry, one test: format 3 is taken exactly when rollback() could undo it (revert_wrap exists).
    if fmt != SUPPORTED_FORMAT and not (fmt == REVERT_FORMAT and _has_revert_wrap()):
        why = (f"format {REVERT_FORMAT} is rollbackable only through anneal's revert_wrap, which this anneal-memory "
               f"lacks; " if fmt == REVERT_FORMAT else "")
        raise SnapshotRefused(f"the store's format_version is {fmt!r}, not {SUPPORTED_FORMAT}: {why}only format "
                              f"{SUPPORTED_FORMAT} (or {REVERT_FORMAT} with revert_wrap) has a proven "
                              "rollback, so the dream will not consolidate it")
    if store.get_wrap_started_at():
        raise SnapshotRefused("a wrap is open on this store; a snapshot now would capture a half-made wrap")
    if not str(getattr(store, "store_id", "") or ""):
        raise SnapshotRefused("the store has no readable store id, so no rollback of it could ever be matched to it; "
                              "the dream will not consolidate it")
    snap = occ_dir / SNAP_DIR
    if (snap / MANIFEST).exists():
        raise SnapshotRefused(f"{snap} already holds a snapshot: one occurrence takes one")
    shutil.rmtree(snap, ignore_errors=True)  # an unmanifested dir is a crashed take's debris
    snap.mkdir(parents=True)
    try:
        files: dict[str, dict[str, Any]] = {}
        for key, src in text_tier(db).items():
            if src.is_file():
                shutil.copy2(src, snap / src.name)
                copied = sha256_file(snap / src.name)
                if copied != sha256_file(src):
                    raise SnapshotRefused(f"{src.name} changed while it was copied")
                files[key] = {"name": src.name, "sha256": copied}
            else:
                files[key] = {"name": src.name, "sha256": None}
        _backup_db(db, snap / DB_BACKUP)
        try:
            import anneal_memory
            anneal_version = str(getattr(anneal_memory, "__version__", "") or "")
        except ImportError:
            anneal_version = ""
        _write_manifest(occ_dir, {
            "occurrence": occ_dir.name, "store_db": str(db), "store_id": str(getattr(store, "store_id", "") or ""),
            "format_version": fmt, "anneal_version": anneal_version, "taken_at": _now(),
            "files": files, "db_backup_sha256": sha256_file(snap / DB_BACKUP),
            "post_files": None, "wrap_id": None, "revert_started_at": None, "reverted_at": None})
    except BaseException:
        shutil.rmtree(snap, ignore_errors=True)
        raise
    return read_manifest(occ_dir) or {}


# ------------------------------------------------------------------------------------- record-post (step 6) and prune

def record_post_for(db_path: Path | str, occ_dir: Path | str, wrap_id: Any, *, composed_sha256: str | None = None,
                    prune: bool = True) -> dict[str, str | None]:
    """Record the text tier the dream's save wrote (``post_files``) and the wrap id in the manifest. Refuses (raises
    :class:`SnapshotRefused`) when there is no snapshot, a post record already exists (a second could bless a later
    wrap's text as the dream's), the tier is unchanged since the snapshot, or ``composed_sha256`` is given and the
    live continuity text is not it (the live text would not be the dream's)."""
    occ_dir, db = Path(occ_dir), Path(db_path)
    m = read_manifest(occ_dir)
    if m is None:
        raise SnapshotRefused(f"no snapshot manifest under {occ_dir / SNAP_DIR}")
    if m.get("post_files"):
        raise SnapshotRefused("this snapshot already has a post-save record")
    post = tier_hashes(db)
    if post == {k: e["sha256"] for k, e in m["files"].items()}:
        raise SnapshotRefused("the text tier is unchanged since the snapshot; there is no save to record")
    if composed_sha256 and post["continuity"] != composed_sha256:
        raise SnapshotRefused("the live continuity text is not the text this dream composed (a save that did not land, "
                              "or a later write); it is not recorded as the dream's")
    m["post_files"] = post
    m["wrap_id"] = wrap_id
    _write_manifest(occ_dir, m)
    if prune:
        with contextlib.suppress(Exception):  # retention is housekeeping: never a reason to lose the record
            prune_snapshots(occ_dir.parent, keep=KEEP_SNAPSHOTS, protect=occ_dir.name)
    return post


def record_post(store: Any, occ_dir: Path | None, wrap_id: Any) -> dict[str, str | None]:
    """``on_saved``: :func:`record_post_for` for the executor's hook, checking the live text against the
    ``composed_sha256`` the executor wrote to ``progress.json``."""
    if occ_dir is None:
        raise SnapshotRefused("no occurrence dir")
    prog = _read_json(Path(occ_dir) / "progress.json") or {}
    return record_post_for(_store_db(store), occ_dir, wrap_id, composed_sha256=prog.get("composed_sha256"))


def hooks() -> tuple[Callable[[Any, Path | None], Any], Callable[[Any, Path | None, Any], Any]]:
    """``(on_snapshot, on_saved)`` for ``consolidate_store``."""
    return take, record_post


def snapshots(root: Path | str) -> list[tuple[str, dict[str, Any]]]:
    """Every occurrence under ``root`` that holds a snapshot manifest, oldest first (``taken_at``, then name)."""
    root = Path(root)
    out: list[tuple[str, dict[str, Any]]] = []
    try:
        names = sorted(p.name for p in root.iterdir() if p.is_dir())
    except OSError:
        return out
    for n in names:
        m = read_manifest(root / n)
        if m is not None:
            out.append((n, m))
    out.sort(key=lambda t: (_taken(t[1]), t[0]))
    return out


def prune_snapshots(root: Path | str, *, keep: int = KEEP_SNAPSHOTS, protect: str | None = None) -> list[str]:
    """Delete ``snap/`` of every occurrence under ``root`` except the newest ``keep`` that consolidated. A snapshot with
    no post record is removed only when it is older than the oldest kept one and its ``progress.json`` is not
    ``saving`` (an unsettled save is recovery's, which needs the manifest; once recovery has recorded its result the
    snapshot is ordinary debris); ``protect`` is never removed. Returns the
    occurrence names pruned."""
    root = Path(root)
    allsnaps = snapshots(root)
    consolidated = [(n, m) for n, m in allsnaps if m.get("post_files")]
    kept = {n for n, _ in consolidated[-keep:]} if keep > 0 else set()
    floor = min((_taken(m) for n, m in consolidated if n in kept), default=None)
    pruned: list[str] = []
    for n, m in allsnaps:
        if n in kept or n == protect:
            continue
        if not m.get("post_files"):
            prog = _read_json(root / n / "progress.json") or {}
            rec = _read_json(root / n / "recovery.json") or {}
            unsettled_save = prog.get("phase") == "saving" and rec.get("ending") not in ("failed", "done", "stopped")
            if unsettled_save or floor is None or _taken(m) >= floor:
                continue
        shutil.rmtree(root / n / SNAP_DIR, ignore_errors=True)
        pruned.append(n)
    return pruned


# ----------------------------------------------------------------------------------------------------------- rollback

@dataclass
class RollbackResult:
    """``outcome`` is ``rolled-back``, ``finished`` (an interrupted rollback completed), ``nothing`` (the live text is
    already the snapshot's), ``already-reverted`` (a rerun after a finished rollback: nothing changed, the card is
    rebuilt from rollback.json) or ``refused``. A refusal names its ``code``."""
    outcome: str
    message: str
    code: str | None = None
    restored: list[str] = field(default_factory=list)
    undo_dir: str | None = None
    card: dict[str, Any] | None = None
    card_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome != "refused"


def _refused(code: str, why: str) -> RollbackResult:
    return RollbackResult("refused", f"REFUSED ({code}): {why} Nothing was changed.", code=code)


def _has_revert_wrap() -> bool:
    """The one capability test: the installed anneal-memory ``Store`` has a callable ``revert_wrap`` (public surface
    only). ``take`` (what may be snapshotted) and ``rollback`` (what may be undone) both ask this."""
    try:
        from anneal_memory import Store
    except ImportError:
        return False
    return callable(getattr(Store, "revert_wrap", None))


class RevertStoreUnavailable(Exception):
    """This anneal-memory has ``revert_wrap`` but the store would not open: the rollback refuses (a text-only fallback
    would claim a limit that is not true here, and would not re-offer the day's episodes)."""


def _revert_store(db: Path) -> Any | None:
    """An open anneal ``Store`` on ``db`` when this anneal-memory has ``revert_wrap`` (public surface only: no table
    names, no version parsing), else None (no ``revert_wrap``). With ``revert_wrap`` a store that will not open raises
    :class:`RevertStoreUnavailable`."""
    if not _has_revert_wrap():
        return None
    try:
        from anneal_memory import Store
        return Store(str(db), section_schema=None)
    except Exception as exc:  # noqa: BLE001
        raise RevertStoreUnavailable(f"{type(exc).__name__}: {exc}") from exc


def _wrap_open(db: Path) -> bool:
    """True when a wrap is open on the store, or that cannot be told (the stricter reading)."""
    try:
        from anneal_memory import Store
        store = Store(str(db), section_schema=None)
    except Exception:  # noqa: BLE001 — a store that will not open cannot be shown idle
        return True
    try:
        return bool(store.get_wrap_started_at())
    except Exception:  # noqa: BLE001
        return True
    finally:
        with contextlib.suppress(Exception):
            store.close()


def _store_id(db: Path) -> str | None:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute("select value from metadata where key = 'store_id'").fetchone()
        finally:
            con.close()
        return str(row[0]) if row else None
    except sqlite3.Error:
        return None


def store_identity_problem(db: Path | str, m: Mapping[str, Any]) -> str | None:
    """Why the snapshot ``m`` cannot be shown to belong to the store at ``db``, or None. A missing or unreadable id on
    EITHER side is a problem, not a pass: identical text hashes in two stores must never let one overwrite the other."""
    want, have = m.get("store_id"), _store_id(Path(db))
    if not want:
        return "the snapshot records no store id, so it cannot be shown to be of this store."
    if not have:
        return "this store's id cannot be read, so the snapshot cannot be shown to be of it."
    if want != have:
        return f"the snapshot is of store {want}, this store is {have}."
    return None


def _later_blocker(occ_dir: Path, m: Mapping[str, Any]) -> str | None:
    """A rollback is one step, and only the newest consolidating dream's. Why a sibling occurrence taken AFTER this one
    forbids it, or None."""
    me = (_taken(m), occ_dir.name)
    for n, other in snapshots(occ_dir.parent):
        if n == occ_dir.name or (_taken(other), n) <= me:
            continue
        if other.get("reverted_at") or other.get("revert_started_at"):
            return (f"a later dream ({n}) was rolled back; a rollback is one step, and undoing this one as well "
                    "would stack on db rows that no longer describe the text")
        if other.get("post_files"):
            return f"a later dream ({n}) consolidated after this one; only the newest dream can be rolled back"
    return None


def rollback(store_path: Path | str, occ_dir: Path | str, *, who: str = "operator",
             file_card: Callable[[dict[str, Any]], Any] | None = None,
             lock: Callable[[Path], Any] | None = None, allow_missing: bool = False,
             lease_token: str | None = None, allow_foreign: bool = False) -> RollbackResult:
    """Restore the memory text the dream in ``occ_dir`` replaced (D1_EXECUTOR §3.2), one step, under the store lock.

    Refuses when a wrap is open, when there is no post record (no proof the live text is the dream's), when a rollback
    already finished, when a later dream exists, when the store is not format 2, when any live text file is neither
    what the dream saved nor what it replaced (a later write would be undone), or when a snapshot file does not match
    its manifest hash. ``memory.db`` is never restored: episodes captured after the dream live there.

    ``file_card`` (D1-1c's card filer) is called with the ``rolled-back`` card spec after a successful restore; its
    failure is recorded on the result, never raised, because the text is already restored.

    When anneal has ``revert_wrap`` the wrap is undone through it (see the module docstring and
    :func:`_rollback_gen3` for the three states a rerun can find); ``allow_missing`` and ``lease_token`` (the dream's
    lease, when it is live and the caller holds it) are passed to it. A refusal from anneal restores nothing and comes
    back as a ``refused`` result carrying anneal's code and a one-line remedy. ``allow_foreign`` matters only to a
    rerun of a revert that was started (``foreign-write-after-revert``)."""
    from levain import wrap as _w

    db, occ_dir = Path(store_path).resolve(), Path(occ_dir)
    handle = (lock or (lambda d: _w._lock_wrap_file(d.parent / "wrap.lock")))(db)
    if handle is _w._ANOTHER_WRAP_RUNNING:
        return _refused("lock-held", "another consolidate holds the store's wrap lock.")
    try:
        return _rollback_locked(db, occ_dir, who, file_card, allow_missing=allow_missing, lease_token=lease_token,
                                allow_foreign=allow_foreign)
    finally:
        _w._unlock_wrap(handle)


def _rollback_locked(db: Path, occ_dir: Path, who: str, file_card: Callable[[dict[str, Any]], Any] | None,
                     *, allow_missing: bool = False, lease_token: str | None = None,
                     allow_foreign: bool = False) -> RollbackResult:
    m = read_manifest(occ_dir)
    if m is None:
        return _refused("no-snapshot", f"there is no snapshot under {occ_dir / SNAP_DIR}.")
    why = store_identity_problem(db, m)
    if why:
        return _refused("store-mismatch", why)
    if m.get("reverted_at"):
        rec = _read_json(occ_dir / ROLLBACK_RECORD)
        if rec and rec.get("at"):
            # the card is rebuilt from the durable record, so a crash after `reverted_at` but before the card reached
            # the operator costs nothing: every rerun shows the same card
            limit = str(rec.get("limit") or LIMIT)
            card = _rollback_card(occ_dir, str(rec.get("who") or who), str(rec["at"]), list(rec.get("files") or []),
                                  limit, rec.get("revert_wrap"))
            return RollbackResult("already-reverted", f"already-reverted: this dream was already rolled back at "
                                  f"{m['reverted_at']}; nothing was changed. {limit}", code="already-reverted",
                                  restored=list(rec.get("files") or []), undo_dir=str(occ_dir / UNDO_DIR), card=card)
        return _refused("already-reverted", f"this dream was already rolled back at {m['reverted_at']}.")
    has_rw = _has_revert_wrap()
    # BEFORE any Store() open: an anneal with revert_wrap migrates a format-2 store to format 3 on open (measured), a
    # change a rollback must never make, refused or not. Plain sqlite reads the format; only the anneal that ran the dream
    # (no revert_wrap) may roll a format-2 store back.
    if format_version(db) == SUPPORTED_FORMAT and has_rw:
        return _refused("format", f"the store is format {SUPPORTED_FORMAT} and this anneal-memory (which has revert_wrap) "
                                  f"would migrate it to format {REVERT_FORMAT} just by opening it. Nothing was opened or "
                                  "changed. Remedy: roll this dream back with the anneal-memory that ran it (one without "
                                  "revert_wrap).")
    if has_rw:
        return _rollback_gen3(db, occ_dir, who, file_card, m, allow_missing, lease_token, allow_foreign)
    if _wrap_open(db):
        return _refused("wrap-open", "a wrap is open on this store (or its state cannot be read); rolling back now "
                                     "would race its save.")
    return _rollback_text_only(db, occ_dir, who, file_card, m)


def _rollback_text_only(db: Path, occ_dir: Path, who: str, file_card: Callable[[dict[str, Any]], Any] | None,
                        m: dict[str, Any]) -> RollbackResult:
    """The anneal <= 1.0.2 (format 2, no ``revert_wrap``) rollback: the memory text only (D1-1b, unchanged)."""
    snap = occ_dir / SNAP_DIR
    pre = {k: e["sha256"] for k, e in m["files"].items()}
    post = m.get("post_files")
    live = tier_hashes(db)
    if live == pre and m.get("revert_started_at"):
        # an earlier rollback restored every file, then died before it finished: mark it finished
        return _finish(occ_dir, m, who, [], file_card, finished=True)
    if live == pre:
        return RollbackResult("nothing", "nothing to roll back: the live text is already the one the snapshot holds.")
    if not post:
        return _refused("no-post", NO_POST_WHY)
    blocker = _later_blocker(occ_dir, m)
    if blocker:
        return _refused("later-dream", blocker + ".")
    fmt = format_version(db)
    if fmt != SUPPORTED_FORMAT:
        return _refused("format", f"the store's format_version is {fmt!r}; only {SUPPORTED_FORMAT} has a proven "
                                  "rollback without revert_wrap (on 3 the db is the identity authority and the files "
                                  "are projections, and only anneal's revert_wrap can undo a save there).")
    # per FILE, not per tier: each live file must be what the dream saved or what it replaced. A rollback that died
    # halfway leaves a mix of the two, and a rerun finishes it; any third value is a later write.
    foreign = [k for k in live if live[k] not in (pre[k], post.get(k))]
    if foreign:
        return _refused("foreign-write", _foreign_why(foreign))
    for key, e in m["files"].items():
        if e["sha256"] is not None and sha256_file(snap / e["name"]) != e["sha256"]:
            return _refused("snapshot-damaged", f"{snap / e['name']} does not match its manifest hash.")
    if not m.get("revert_started_at"):
        _take_undo_copy(db, occ_dir, m)
    return _restore_text(db, occ_dir, who, file_card, m, live, finished=False)


NO_POST_WHY = ("the snapshot has no post-save record (it never ran, or the save did not land), so there is no proof "
               "the live text is the dream's.")


def _foreign_why(foreign: list[str]) -> str:
    return (f"the live {', '.join(sorted(foreign))} is neither what this dream saved nor what it replaced; a later "
            "write would be silently undone.")


def _take_undo_copy(db: Path, occ_dir: Path, m: dict[str, Any]) -> None:
    """Keep the live text in ``reverted-from/`` and mark the rollback started. Only the FIRST attempt's copy holds the
    dream's text; a rerun never overwrites it."""
    undo = occ_dir / UNDO_DIR
    shutil.rmtree(undo, ignore_errors=True)
    undo.mkdir(parents=True)
    for src in text_tier(db).values():
        if src.is_file():
            shutil.copy2(src, undo / src.name)
    m["revert_started_at"] = _now()
    _write_manifest(occ_dir, m)


def _wrap_open_ro(db: Path) -> bool:
    """:func:`_wrap_open` by plain read-only sqlite (no ``Store`` is constructed, so nothing can migrate or re-project):
    True when a wrap is open, or that cannot be told (the stricter reading)."""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute("select value from metadata where key = 'wrap_started_at'").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return True
    return bool(row and str(row[0] if row[0] is not None else "").strip())


def _later_posts(occ_dir: Path, m: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The post-save hashes of every sibling dream taken AFTER this one: text a later dream saved is recorded, not a
    stranger's."""
    me = (_taken(m), occ_dir.name)
    return [other["post_files"] for n, other in snapshots(occ_dir.parent)
            if n != occ_dir.name and (_taken(other), n) > me and other.get("post_files")]


def _gen3_refusal_text(exc: BaseException, code: str) -> tuple[str, str]:
    """(anneal's detail, the one-line remedy) for a refusal ``code``."""
    remedy = REVERT_REMEDY.get(code, "") if code != "lease_fenced" else (
        "the dream's consolidation lease is live and the token given does not match; pass the right "
        "--lease-token or wait for the lease to lapse.")
    return str(getattr(exc, "detail", "") or exc).rstrip("."), remedy


def _revert_state_hint(db: Path, wrap_id: Any) -> str:
    return (f"Check what anneal did with `anneal-memory --db {db} history`: if wrap {wrap_id} is no longer listed, the "
            "revert committed.")


def _open_for_revert(db: Path) -> Any:
    store = _revert_store(db)
    if store is None:
        raise RevertStoreUnavailable("this anneal-memory has no revert_wrap")
    return store


def _rollback_gen3(db: Path, occ_dir: Path, who: str, file_card: Callable[[dict[str, Any]], Any] | None,
                   m: dict[str, Any], allow_missing: bool, lease_token: str | None,
                   allow_foreign: bool) -> RollbackResult:
    """The rollback when anneal has ``revert_wrap``: a three-state machine keyed on the DURABLE record of the rollback
    being resumed (``rollback.json``'s ``revert_wrap``), never on the manifest's ``revert_started_at``.

    * S0, no ``called_at``: a FIRST attempt. Every check reads plain sqlite (read-only) and file hashes; no ``Store`` is
      constructed until all have passed; the undo copy is taken before the first write-capable open; there is no
      file-hash no-op shortcut (anneal is the authority). A refusal from anneal restores the record as it was, so the
      next run is S0 again.
    * S1, ``called_at`` and no ``receipt``: anneal may or may not have committed. The live files are hashed first
      (``foreign-write-after-revert`` if one is a stranger's, unless ``allow_foreign``); anneal is then asked again
      BEFORE any occurrence-based check, and answers ``already_reverted`` or reverts. Marks are kept on a refusal.
    * S2, a ``receipt``: finish without calling anneal again.
    """
    pre = {k: e["sha256"] for k, e in m["files"].items()}
    post = m.get("post_files")
    live = tier_hashes(db)
    rec0 = _read_json(occ_dir / ROLLBACK_RECORD) or {}
    rw0 = dict(rec0.get("revert_wrap") or {})
    if rw0.get("receipt"):
        return _gen3_s2(db, occ_dir, who, file_card, m, pre, post, live, rw0, allow_foreign)
    if rw0.get("called_at"):
        return _gen3_s1(db, occ_dir, who, file_card, m, pre, post, live, rec0, rw0, allow_missing, lease_token,
                        allow_foreign)
    return _gen3_s0(db, occ_dir, who, file_card, m, pre, post, live, rec0, allow_missing, lease_token)


def _gen3_format_wrap_problem(db: Path, m: Mapping[str, Any], *, started: bool) -> RollbackResult | None:
    """The format, wrap-id and wrap-open checks every state that will call anneal shares (read-only)."""
    fmt = format_version(db)
    if fmt != REVERT_FORMAT:
        return _refused("format", f"the store's format_version is {fmt!r}; the rollback through anneal's revert_wrap is "
                                  f"proven on format {REVERT_FORMAT} only (on it the db is the identity authority and "
                                  "the files are projections).")
    wrap_id = m.get("wrap_id")
    if isinstance(wrap_id, bool) or not isinstance(wrap_id, int):
        return _refused("no-wrap-id", f"the post record holds no wrap id ({wrap_id!r}), so anneal cannot be told which "
                                      "wrap to revert.")
    if _wrap_open_ro(db):
        why = ("a wrap is open on this store (or its state cannot be read); rolling back now would race its save.")
        if started:
            return RollbackResult("refused", f"REFUSED (wrap-open): {why} A revert was started earlier and anneal may "
                                  f"have committed it; nothing was changed by this run. {_revert_state_hint(db, wrap_id)}",
                                  code="wrap-open")
        return _refused("wrap-open", why)
    return None


def _gen3_s0(db: Path, occ_dir: Path, who: str, file_card: Callable[[dict[str, Any]], Any] | None,
             m: dict[str, Any], pre: dict[str, Any], post: Any, live: dict[str, str | None], rec0: dict[str, Any],
             allow_missing: bool, lease_token: str | None) -> RollbackResult:
    snap = occ_dir / SNAP_DIR
    if not post:
        return _refused("no-post", NO_POST_WHY)
    blocker = _later_blocker(occ_dir, m)
    if blocker:
        return _refused("later-dream", blocker + ".")
    bad = _gen3_format_wrap_problem(db, m, started=False)
    if bad:
        return bad
    # per FILE, not per tier. No file-hash no-op here: live == pre after a legitimate edit back does not mean there is
    # nothing to undo in the db, and anneal is the authority.
    foreign = [k for k in live if live[k] not in (pre[k], post.get(k))]
    if foreign:
        return _refused("foreign-write", _foreign_why(foreign))
    for key, e in m["files"].items():
        if e["sha256"] is not None and sha256_file(snap / e["name"]) != e["sha256"]:
            return _refused("snapshot-damaged", f"{snap / e['name']} does not match its manifest hash.")
    # every check passed: the first writes. The undo copy comes BEFORE the first write-capable open.
    rec_path = occ_dir / ROLLBACK_RECORD
    prev_started = m.get("revert_started_at")
    _take_undo_copy(db, occ_dir, m)
    rw = {"wrap_id": m["wrap_id"], "called_at": _now(), "reason": f"levain dream rollback {occ_dir.name}",
          "actor": f"levain-dream:{who}", "allow_missing": bool(allow_missing), "leased": bool(lease_token)}
    _write_json(rec_path, {**rec0, "revert_wrap": rw})

    def back_to_s0() -> None:
        """Nothing reached anneal's db: leave the occurrence exactly as this attempt found it."""
        m["revert_started_at"] = prev_started
        _write_manifest(occ_dir, m)
        shutil.rmtree(occ_dir / UNDO_DIR, ignore_errors=True)
        if rec0:
            _write_json(rec_path, rec0)
        else:
            with contextlib.suppress(OSError):
                rec_path.unlink()

    try:
        store = _open_for_revert(db)
    except RevertStoreUnavailable as exc:
        back_to_s0()
        return _refused("revert-store", f"this anneal-memory has revert_wrap but the store would not open ({exc}). "
                                        "Remedy: free the store (close what holds it) and rerun.")
    with contextlib.closing(store):
        try:
            receipt = store.revert_wrap(m["wrap_id"], reason=rw["reason"], actor=rw["actor"],
                                        lease_token=lease_token, allow_missing=bool(allow_missing))
        except Exception as exc:  # noqa: BLE001 — anneal's refusals are named below; anything else is re-raised
            code = _refusal_code(exc)
            if code is None:
                raise      # a crash: the record keeps called_at, so the rerun is S1
            back_to_s0()
            detail, remedy = _gen3_refusal_text(exc, code)
            return _refused(code, f"anneal refused the revert ({detail}). Remedy: {remedy}" if remedy
                            else f"anneal refused the revert ({detail}).")
    return _gen3_receipt(db, occ_dir, who, file_card, m, rw, receipt, base=live, pre=pre, post=post, later=[],
                         fresh=True, allow_foreign=False)


def _allowed(pre: Mapping[str, Any], post: Any, later: list[Mapping[str, Any]]) -> dict[str, set[Any]]:
    """Per file, the hashes that are the dream's own or a recorded later dream's (never a stranger's)."""
    return {k: {pre[k], (post or {}).get(k), *(p.get(k) for p in later)} for k in pre}


def _later_only(later: list[Mapping[str, Any]], keys: Any) -> dict[str, set[Any]]:
    """Per file, the hashes a later dream saved (text this rollback did not write, whoever's it now is)."""
    return {k: {p.get(k) for p in later} for k in keys}


def _gen3_s1(db: Path, occ_dir: Path, who: str, file_card: Callable[[dict[str, Any]], Any] | None,
             m: dict[str, Any], pre: dict[str, Any], post: Any, live: dict[str, str | None], rec0: dict[str, Any],
             rw0: dict[str, Any], allow_missing: bool, lease_token: str | None,
             allow_foreign: bool) -> RollbackResult:
    wrap_id = m.get("wrap_id")
    started = (f"A revert of wrap {wrap_id} was started at {rw0.get('called_at')} and anneal may have committed it")
    later = _later_posts(occ_dir, m)
    allowed = _allowed(pre, post, later)
    foreign = [k for k in live if live[k] not in allowed[k]]
    if foreign and not allow_foreign:
        return RollbackResult(
            "refused", f"REFUSED (foreign-write-after-revert): {started}, and the live {', '.join(sorted(foreign))} "
            "has changed since: it is neither this dream's text, nor what it replaced, nor a later dream's. Nothing was "
            f"overwritten. Remedy: inspect the file; to let anneal re-project the identity over it, rerun with "
            f"--allow-foreign. {_revert_state_hint(db, wrap_id)}", code="foreign-write-after-revert")
    bad = _gen3_format_wrap_problem(db, m, started=True)
    if bad:
        return bad
    try:
        store = _open_for_revert(db)
    except RevertStoreUnavailable as exc:
        return RollbackResult(
            "refused", f"REFUSED (revert-store): {started}; this run could not open the store ({exc}) and changed "
            f"nothing. Remedy: free the store (close what holds it) and rerun. {_revert_state_hint(db, wrap_id)}",
            code="revert-store")
    with contextlib.closing(store):
        try:
            receipt = store.revert_wrap(m["wrap_id"], reason=str(rw0.get("reason") or f"levain dream rollback {occ_dir.name}"),
                                        actor=str(rw0.get("actor") or f"levain-dream:{who}"), lease_token=lease_token,
                                        allow_missing=bool(allow_missing))
        except Exception as exc:  # noqa: BLE001
            code = _refusal_code(exc)
            if code is None:
                raise
            detail, remedy = _gen3_refusal_text(exc, code)
            if code == "not_latest":
                # a real block, found by asking anneal first: a later wrap exists and this one was never reverted
                why = _later_blocker(occ_dir, m) or "a later wrap exists in the store"
                return RollbackResult("refused", f"REFUSED (later-dream): {why}. {started}; anneal says the wrap is not "
                                      f"the newest and was not reverted ({detail}). The marks are kept.",
                                      code="later-dream")
            return RollbackResult(
                "refused", f"REFUSED ({code}): anneal refused the revert on the rerun ({detail}). {started}, so it "
                f"may already have committed; the marks are kept. {('Remedy: ' + remedy + ' ') if remedy else ''}"
                f"{_revert_state_hint(db, wrap_id)}", code=code)
    rw = {**rw0, "reconciled_at": _now(), "allow_missing": bool(allow_missing), "leased": bool(lease_token)}
    return _gen3_receipt(db, occ_dir, who, file_card, m, rw, receipt, base=post or live, pre=pre, post=post,
                         later=later, fresh=False, allow_foreign=allow_foreign)


def _gen3_s2(db: Path, occ_dir: Path, who: str, file_card: Callable[[dict[str, Any]], Any] | None,
             m: dict[str, Any], pre: dict[str, Any], post: Any, live: dict[str, str | None], rw0: dict[str, Any],
             allow_foreign: bool) -> RollbackResult:
    """The receipt is recorded: anneal is never asked again, the rest of the rollback finishes."""
    return _gen3_after_receipt(db, occ_dir, who, file_card, m, rw0, base=post or live, pre=pre, post=post,
                               later=_later_posts(occ_dir, m), fresh=False, allow_foreign=allow_foreign)


def _gen3_receipt(db: Path, occ_dir: Path, who: str, file_card: Callable[[dict[str, Any]], Any] | None,
                  m: dict[str, Any], rw: dict[str, Any], receipt: Any, *, base: Mapping[str, Any],
                  pre: Mapping[str, Any], post: Any, later: list[Mapping[str, Any]], fresh: bool,
                  allow_foreign: bool) -> RollbackResult:
    """Record anneal's answer durably (the FIRST receipt: S1 has none), then finish."""
    rw["receipt"], rw["reverted_at"] = _receipt_dict(receipt), _now()
    rec_path = occ_dir / ROLLBACK_RECORD
    _write_json(rec_path, {**(_read_json(rec_path) or {}), "revert_wrap": rw})
    return _gen3_after_receipt(db, occ_dir, who, file_card, m, rw, base=base, pre=pre, post=post, later=later,
                               fresh=fresh, allow_foreign=allow_foreign)


def _gen3_after_receipt(db: Path, occ_dir: Path, who: str, file_card: Callable[[dict[str, Any]], Any] | None,
                        m: dict[str, Any], rw: dict[str, Any], *, base: Mapping[str, Any], pre: Mapping[str, Any],
                        post: Any, later: list[Mapping[str, Any]], fresh: bool,
                        allow_foreign: bool) -> RollbackResult:
    rec = rw["receipt"]
    reoffer = _reoffer_text(rec)
    after = tier_hashes(db)
    if rec.get("identity_version") is not None:
        # anneal restored the identity itself (appended a version and re-projected the files): Levain writes no text.
        # `restored` is what changed from where this run started (S0: the live text; S1/S2: the dream's post text),
        # leaving out a file that now holds a later dream's text (this rollback did not write that).
        by_later = _later_only(later, after)
        restored = [text_tier(db)[k].name for k in after if after[k] != base.get(k) and after[k] not in by_later[k]]
        extra = ""
        if any(after[k] != e["sha256"] for k, e in m["files"].items() if after[k] not in by_later[k]):
            extra = (" NOTE: the text files after anneal's revert are not byte-identical to the snapshot "
                     "(anneal projects them from its identity); the snapshot copy is in " + str(occ_dir / SNAP_DIR) + ".")
        return _finish(occ_dir, m, who, restored, file_card, finished=not fresh, limit=reoffer + extra, revert=rw,
                       how="anneal restored the identity itself")
    # anneal restored no identity: the text step is Levain's, and it never overwrites a stranger's file
    snap = occ_dir / SNAP_DIR
    for key, e in m["files"].items():
        if e["sha256"] is not None and sha256_file(snap / e["name"]) != e["sha256"]:
            return RollbackResult("refused", f"REFUSED (snapshot-damaged): {snap / e['name']} does not match its "
                                  "manifest hash. anneal's revert is recorded; the text step is still owed. "
                                  "The marks are kept.", code="snapshot-damaged")
    allowed = _allowed(pre, post, later)
    foreign = [k for k in after if after[k] not in allowed[k]]
    if foreign and not allow_foreign:
        return RollbackResult(
            "refused", f"REFUSED (foreign-write-after-revert): anneal's revert is recorded and restored no identity, so "
            f"the text step is Levain's; the live {', '.join(sorted(foreign))} has changed since the dream. Nothing was "
            "overwritten. Remedy: inspect the file; to restore it from the snapshot anyway, rerun with --allow-foreign.",
            code="foreign-write-after-revert")
    return _restore_text(db, occ_dir, who, file_card, m, after, finished=not fresh, limit=reoffer, revert=rw)


def _restore_text(db: Path, occ_dir: Path, who: str, file_card: Callable[[dict[str, Any]], Any] | None,
                  m: dict[str, Any], live: dict[str, str | None], *, finished: bool,
                  limit: str = LIMIT, revert: dict[str, Any] | None = None) -> RollbackResult:
    snap = occ_dir / SNAP_DIR
    restored: list[str] = []
    tier = text_tier(db)
    for key, e in m["files"].items():
        dest = tier[key]
        if e["sha256"] is None:
            if dest.exists():
                dest.unlink()
                restored.append(dest.name)
            continue
        if live[key] != e["sha256"]:
            _atomic_copy(snap / e["name"], dest)
            restored.append(dest.name)
    return _finish(occ_dir, m, who, restored, file_card, finished=finished, limit=limit, revert=revert)


def _receipt_dict(r: Any) -> dict[str, Any]:
    names = ("wrap_id", "revert_id", "episodes_reoffered", "window_after", "last_wrap_id_after", "already_reverted",
             "identity_version", "pattern_state_exact")
    return {n: getattr(r, n, None) for n in names}


def _reoffer_text(rec: Mapping[str, Any] | None) -> str:
    if not rec:
        return LIMIT
    if rec.get("already_reverted"):
        return "anneal had already reverted this wrap; nothing was reopened by this call."
    n = rec.get("episodes_reoffered")
    if n is None:
        return "an unknown number of the day's episodes will be offered to the next dream."
    return f"the day's {n} episodes will be offered to the next dream."


def _refusal_code(exc: Exception) -> str | None:
    """anneal's refusal code when ``exc`` is a revert refusal or a lease fence (by name and ``code``, so no anneal
    import is needed at module scope), else None."""
    names = {c.__name__ for c in type(exc).__mro__}
    if "WrapRevertRefused" in names:
        return str(getattr(exc, "code", "") or "refused")
    if "LeaseFencedError" in names:
        return "lease_fenced"
    return None


def _rollback_card(occ_dir: Path, who: str, at: str, restored: list[str], limit: str = LIMIT,
                   revert: dict[str, Any] | None = None) -> dict[str, Any]:
    card = {"kind": "rolled-back", "dedupe_key": f"dream-rollback:{occ_dir.name}", "occurrence": occ_dir.name,
            "who": who, "at": at, "files": restored, "limit": limit}
    if revert and revert.get("receipt"):
        card["revert_wrap"] = revert["receipt"]
    return card


def _finish(occ_dir: Path, m: dict[str, Any], who: str, restored: list[str],
            file_card: Callable[[dict[str, Any]], Any] | None, *, finished: bool, limit: str = LIMIT,
            revert: dict[str, Any] | None = None, how: str = "") -> RollbackResult:
    # ``reverted_at`` is the LAST durable marker: it says the record and the card are both out. A rerun after a crash
    # (or a card that did not go) finds it unset, reuses the record already written and finishes the rest.
    rec_path = occ_dir / ROLLBACK_RECORD
    prior = _read_json(rec_path)
    if prior and prior.get("at"):
        at, restored = str(prior["at"]), list(prior.get("files") or [])
        who = str(prior.get("who") or who)
        limit = str(prior.get("limit") or limit)
        revert = prior.get("revert_wrap") or revert
    else:
        at = _now()
        if revert is None and prior and prior.get("revert_wrap"):
            revert = prior["revert_wrap"]
        _write_json(rec_path, {**(prior or {}), "who": who, "at": at, "files": restored,
                               "finished_an_interrupted_rollback": finished, "limit": limit,
                               **({"revert_wrap": revert} if revert else {})})
    card = _rollback_card(occ_dir, who, at, restored, limit, revert)
    res = RollbackResult("finished" if finished else "rolled-back", "", restored=restored,
                         undo_dir=str(occ_dir / UNDO_DIR), card=card)
    res.message = (f"{'finished the interrupted rollback of' if finished else 'rolled back'} {occ_dir.name}: "
                   f"{(how + '; ') if how else ''}restored {', '.join(restored) or 'nothing further'}; the text it "
                   f"replaced is kept in {occ_dir / UNDO_DIR}. {limit}")
    if file_card is not None:
        try:
            file_card(card)
        except Exception as exc:  # noqa: BLE001 — the text is already restored; a rerun refiles the card
            res.card_error = f"{type(exc).__name__}: {exc}"
            res.message += f" The card was not filed ({res.card_error}); run the rollback again to file it."
            return res
    m["reverted_at"] = at
    _write_manifest(occ_dir, m)
    return res


# --------------------------------------------------------------------------------------------------------------- drill

@dataclass
class DrillResult:
    """``outcome``: ``ok`` (every applicable step passed), ``failed`` (a step failed: an attention row) or
    ``no-snapshot`` (a fresh install: not a failure) or ``deferred`` (a dream or another writer holds the store, so
    the live store could not be copied consistently; nothing is recorded and the drill stays due). A step is ``ok``, ``failed`` or ``skipped`` (with why)."""
    outcome: str
    snapshot: str | None = None
    steps: list[dict[str, str]] = field(default_factory=list)
    at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"outcome": self.outcome, "snapshot": self.snapshot, "steps": self.steps, "at": self.at}


def _step(res: DrillResult, name: str, status: str, detail: str) -> str:
    res.steps.append({"name": name, "status": status, "detail": detail})
    return status


def drill(store_path: Path | str, root: Path | str, *, write_result: bool = True) -> DrillResult:
    """The restore drill (§3.4): it never touches the live store. On the latest snapshot under ``root``: (1) hash every
    snapshot file and the db backup against the manifest; (2) ``PRAGMA integrity_check`` on the backup; (3) copy the live
    store to a scratch dir and run the real :func:`rollback` against the copy, then check each restored file hashes to
    the manifest's ``pre``; (4) open the restored copy with anneal and read its status. Step 3 is skipped (not failed)
    when the snapshot is not the one the live text matches (a later write, a finished rollback), because the rollback's
    own guard is then doing its job. The result is written to ``<root>/drill.json`` for the gauge."""
    from anneal_memory import Store
    from levain.dream.executor import _copy_store

    from levain import wrap as _w

    db, root = Path(store_path).resolve(), Path(root)
    res = DrillResult("no-snapshot", at=_now())
    scratch: Path | None = None
    try:
        # the scratch dirs exist before the lock is taken, so a setup failure can never leave the lock held
        try:
            scratch = Path(tempfile.mkdtemp(prefix="levain-dream-drill-"))
            sdir = scratch / "store"
            sdir.mkdir()
            bdir = scratch / "backup"
            bdir.mkdir()
        except OSError as exc:
            _step(res, "setup", "failed", f"no scratch directory: {type(exc).__name__}: {exc}")
            return _drill_done(res, root, write_result, failed=True)
        # one lock covers choosing the snapshot, reading its manifest, and copying both the snapshot files and the
        # live store: a dream finishing between any two of those would otherwise leave the drill checking a stale one
        try:
            handle = _w._lock_wrap_file(db.parent / "wrap.lock")
        except OSError as exc:
            _step(res, "setup", "failed", f"the wrap lock could not be opened: {type(exc).__name__}: {exc}")
            return _drill_done(res, root, write_result, failed=True)
        if handle is _w._ANOTHER_WRAP_RUNNING:
            return _deferred(res, "a consolidate holds the store's wrap lock, so the live store cannot be copied "
                                  "consistently")
        held = [handle]

        def release() -> None:
            if held:
                _w._unlock_wrap(held.pop())

        copy_db: Path | None = None
        copy_err: str | None = None
        restore_err: str | None = None
        try:
            snaps = snapshots(root)
            if not snaps:
                return _drill_done(res, root, write_result)
            name, m = snaps[-1]
            occ = root / name
            res.snapshot, res.outcome = name, "ok"
            snap = occ / SNAP_DIR
            # under the lock: copy every snapshot file and the db backup to scratch and hash each COPY against the
            # manifest. A copy that mismatches is compared with its original (still under the lock, so it cannot be
            # pruned): the original matching means the copy is bad, the original mismatching means the snapshot is.
            # Everything after the release reads only these verified copies.
            socc = scratch / "occs" / name
            corrupt: list[str] = []
            bad_copy: list[str] = []

            def copy_and_check(src: Path, dst: Path, want: str | None, label: str) -> None:
                shutil.copy2(src, dst)
                if sha256_file(dst) != want:
                    (bad_copy if sha256_file(src) == want else corrupt).append(label)

            try:
                (socc / SNAP_DIR).mkdir(parents=True)
                for e in m["files"].values():
                    if e["sha256"] is not None:
                        copy_and_check(snap / e["name"], socc / SNAP_DIR / e["name"], e["sha256"], e["name"])
                copy_and_check(snap / DB_BACKUP, bdir / DB_BACKUP, m.get("db_backup_sha256"), DB_BACKUP)
            except OSError as exc:
                copy_err = f"{type(exc).__name__}: {exc}"
            if copy_err is None and not corrupt and not bad_copy and m.get("post_files"):
                try:
                    copy_db = _copy_store(db, sdir)
                except (OSError, sqlite3.Error) as exc:
                    restore_err = f"the live store could not be copied: {type(exc).__name__}: {exc}"
        finally:
            release()
        if _step(res, "hash", "failed" if corrupt else "ok", f"does not match the manifest: {corrupt}" if corrupt
                 else "every snapshot file and the db backup match their manifest hashes") == "failed":
            return _drill_done(res, root, write_result, failed=True)
        if copy_err is not None or bad_copy:
            _step(res, "copy", "failed", f"the scratch copy failed: {copy_err}" if copy_err
                  else f"the scratch copy of {bad_copy} does not match the manifest though the snapshot does")
            return _drill_done(res, root, write_result, failed=True)
        try:
            con = sqlite3.connect(f"file:{bdir / DB_BACKUP}?mode=ro", uri=True)
            try:
                verdict = [r[0] for r in con.execute("pragma integrity_check")]
            finally:
                con.close()
        except sqlite3.Error as exc:
            verdict = [f"{type(exc).__name__}: {exc}"]
        ok = verdict == ["ok"]
        if _step(res, "integrity", "ok" if ok else "failed", "integrity_check: ok" if ok else f"{verdict[:3]}") == "failed":
            return _drill_done(res, root, write_result, failed=True)
        if restore_err is not None:
            _step(res, "restore", "failed", restore_err)
            return _drill_done(res, root, write_result, failed=True)
        if not m.get("post_files") or copy_db is None:
            _step(res, "restore", "skipped", "the snapshot has no post-save record, so a rollback of it is refused by design")
            _step(res, "open", "skipped", "no restored copy")
            return _drill_done(res, root, write_result)
        # the revert markers stay as the real manifest has them: a finished rollback then refuses on the copy as
        # ``already-reverted`` (skipped), instead of the copy reading "nothing to roll back" (a false failure)
        _write_manifest(socc, {**m, "store_db": str(copy_db)})
        rb = rollback(copy_db, socc, who="drill")
        if rb.outcome == "refused" and rb.code in ("wrap-open", "lock-held"):
            return _deferred(res, f"{rb.code}: a dream is running on the store")
        if rb.code in ("foreign-write", "later-dream", "already-reverted"):
            _step(res, "restore", "skipped", f"{rb.code}: the live store is past this dream, and the rollback guard "
                                             f"refused it on the copy (as designed). {rb.message}")
            _step(res, "open", "skipped", "no restored copy")
            return _drill_done(res, root, write_result)
        if not rb.ok or rb.outcome == "nothing":
            _step(res, "restore", "failed", rb.message)
            return _drill_done(res, root, write_result, failed=True)
        now = tier_hashes(copy_db)
        pre = {k: e["sha256"] for k, e in m["files"].items()}
        wrong = sorted(k for k in pre if now[k] != pre[k])
        if _step(res, "restore", "failed" if wrong else "ok",
                 f"restored files do not hash to the manifest's pre: {wrong}" if wrong
                 else f"rolled back on a copy; {len(rb.restored)} file(s) restored, each hashes to pre") == "failed":
            return _drill_done(res, root, write_result, failed=True)
        try:
            with Store(str(copy_db), section_schema=None) as s:
                st = s.status()
            _step(res, "open", "ok", f"anneal opened the restored copy and read its status ({type(st).__name__})")
        except Exception as exc:  # noqa: BLE001 — any failure to open the restored copy is the drill's finding
            _step(res, "open", "failed", f"{type(exc).__name__}: {exc}")
            return _drill_done(res, root, write_result, failed=True)
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
    return _drill_done(res, root, write_result)


def _deferred(res: DrillResult, why: str) -> DrillResult:
    """A drill that could not look at a consistent live store says so and records nothing, so it stays due."""
    res.outcome = "deferred"
    res.steps.append({"name": "restore", "status": "skipped", "detail": f"deferred: {why}"})
    return res


def _drill_done(res: DrillResult, root: Path, write: bool, *, failed: bool = False) -> DrillResult:
    if failed:
        res.outcome = "failed"
    if write:
        with contextlib.suppress(OSError):
            root.mkdir(parents=True, exist_ok=True)
            _write_json(root / DRILL_RECORD, res.as_dict())
    return res


def last_drill(root: Path | str) -> dict[str, Any] | None:
    """The last drill result under ``root``, or None."""
    return _read_json(Path(root) / DRILL_RECORD)


def drill_due(root: Path | str, now: _dt.datetime | None = None, cadence_s: float = DRILL_CADENCE_S) -> bool:
    """True when no drill result exists or the last one is older than the cadence (an unreadable stamp counts as due)."""
    rec = last_drill(root)
    if rec is None:
        return True
    try:
        at = _dt.datetime.fromisoformat(str(rec.get("at")))
    except ValueError:
        return True
    ref = now or _dt.datetime.now().astimezone()
    if at.tzinfo is None:
        at = at.astimezone()
    return (ref - at).total_seconds() >= cadence_s
