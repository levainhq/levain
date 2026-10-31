"""levain.dream.snapshot (D1-1b): the pre-save snapshot, the post-save record, one-step rollback, retention and the drill.

The rules are flow's ``dream_snapshot.py`` rules, not its code (D1_EXECUTOR §3): a rollback refuses per FILE, verifies
every snapshot file before it touches the store, keeps what it replaces, and a rerun finishes an interrupted one.

A rollback restores the memory TEXT only. The wrap's db rows (its ``wraps`` row, pattern history, the episode
watermark) stay, so the day's episodes still count as consolidated and are not offered to the next dream. Whether the
NEXT save is accepted after a rollback was run by hand on a scratch store: anneal accepted it (format 2). The limit is
stated in every rollback's output; closing it needs an anneal ``revert_wrap``.

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
#: The only store format with a proven rollback (text files are the authority; the db is never restored).
SUPPORTED_FORMAT = 2
DRILL_CADENCE_S = 7 * 24 * 3600

LIMIT = ("The memory text is restored. The day's episodes are NOT re-offered: anneal still counts them as "
         "consolidated until it has a revert_wrap, so the next dream starts from the episodes that arrive after this.")


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
    if fmt != SUPPORTED_FORMAT:
        raise SnapshotRefused(f"the store's format_version is {fmt!r}, not {SUPPORTED_FORMAT}: only format "
                              f"{SUPPORTED_FORMAT} has a proven rollback, so the dream will not consolidate it")
    if store.get_wrap_started_at():
        raise SnapshotRefused("a wrap is open on this store; a snapshot now would capture a half-made wrap")
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
    already the snapshot's) or ``refused``. A refusal names its ``code``."""
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
             lock: Callable[[Path], Any] | None = None) -> RollbackResult:
    """Restore the memory text the dream in ``occ_dir`` replaced (D1_EXECUTOR §3.2), one step, under the store lock.

    Refuses when a wrap is open, when there is no post record (no proof the live text is the dream's), when a rollback
    already finished, when a later dream exists, when the store is not format 2, when any live text file is neither
    what the dream saved nor what it replaced (a later write would be undone), or when a snapshot file does not match
    its manifest hash. ``memory.db`` is never restored: episodes captured after the dream live there.

    ``file_card`` (D1-1c's card filer) is called with the ``rolled-back`` card spec after a successful restore; its
    failure is recorded on the result, never raised, because the text is already restored."""
    from levain import wrap as _w

    db, occ_dir = Path(store_path).resolve(), Path(occ_dir)
    handle = (lock or (lambda d: _w._lock_wrap_file(d.parent / "wrap.lock")))(db)
    if handle is _w._ANOTHER_WRAP_RUNNING:
        return _refused("lock-held", "another consolidate holds the store's wrap lock.")
    try:
        return _rollback_locked(db, occ_dir, who, file_card)
    finally:
        _w._unlock_wrap(handle)


def _rollback_locked(db: Path, occ_dir: Path, who: str, file_card: Callable[[dict[str, Any]], Any] | None
                     ) -> RollbackResult:
    m = read_manifest(occ_dir)
    if m is None:
        return _refused("no-snapshot", f"there is no snapshot under {occ_dir / SNAP_DIR}.")
    snap = occ_dir / SNAP_DIR
    why = store_identity_problem(db, m)
    if why:
        return _refused("store-mismatch", why)
    if m.get("reverted_at"):
        return _refused("already-reverted", f"this dream was already rolled back at {m['reverted_at']}.")
    if _wrap_open(db):
        return _refused("wrap-open", "a wrap is open on this store (or its state cannot be read); rolling back now "
                                     "would race its save.")
    pre = {k: e["sha256"] for k, e in m["files"].items()}
    post = m.get("post_files")
    live = tier_hashes(db)
    if live == pre and m.get("revert_started_at"):
        # an earlier rollback restored every file, then died before it finished: mark it finished
        return _finish(occ_dir, m, who, [], file_card, finished=True)
    if live == pre:
        return RollbackResult("nothing", "nothing to roll back: the live text is already the one the snapshot holds.")
    if not post:
        return _refused("no-post", "the snapshot has no post-save record (it never ran, or the save did not land), so "
                                   "there is no proof the live text is the dream's.")
    blocker = _later_blocker(occ_dir, m)
    if blocker:
        return _refused("later-dream", blocker + ".")
    fmt = format_version(db)
    if fmt != SUPPORTED_FORMAT:
        return _refused("format", f"the store's format_version is {fmt!r}; only {SUPPORTED_FORMAT} has a proven "
                                  "rollback (on 3 the db is the identity authority and the files are projections).")
    # per FILE, not per tier: each live file must be what the dream saved or what it replaced. A rollback that died
    # halfway leaves a mix of the two, and a rerun finishes it; any third value is a later write.
    foreign = [k for k in live if live[k] not in (pre[k], post.get(k))]
    if foreign:
        return _refused("foreign-write", f"the live {', '.join(sorted(foreign))} is neither what this dream saved nor "
                                         "what it replaced; a later write would be silently undone.")
    for key, e in m["files"].items():
        if e["sha256"] is not None and sha256_file(snap / e["name"]) != e["sha256"]:
            return _refused("snapshot-damaged", f"{snap / e['name']} does not match its manifest hash.")
    undo = occ_dir / UNDO_DIR
    if not m.get("revert_started_at"):
        # only the FIRST attempt's copy holds the dream's text; a rerun never overwrites it
        shutil.rmtree(undo, ignore_errors=True)
        undo.mkdir(parents=True)
        for src in text_tier(db).values():
            if src.is_file():
                shutil.copy2(src, undo / src.name)
        m["revert_started_at"] = _now()
        _write_manifest(occ_dir, m)
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
    return _finish(occ_dir, m, who, restored, file_card, finished=False)


def _finish(occ_dir: Path, m: dict[str, Any], who: str, restored: list[str],
            file_card: Callable[[dict[str, Any]], Any] | None, *, finished: bool) -> RollbackResult:
    # ``reverted_at`` is the LAST durable marker: it says the record and the card are both out. A rerun after a crash
    # (or a card that did not go) finds it unset, reuses the record already written and finishes the rest.
    rec_path = occ_dir / ROLLBACK_RECORD
    prior = _read_json(rec_path)
    if prior and prior.get("at"):
        at, restored = str(prior["at"]), list(prior.get("files") or [])
        who = str(prior.get("who") or who)
    else:
        at = _now()
        _write_json(rec_path, {"who": who, "at": at, "files": restored,
                               "finished_an_interrupted_rollback": finished, "limit": LIMIT})
    card = {"kind": "rolled-back", "dedupe_key": f"dream-rollback:{occ_dir.name}", "occurrence": occ_dir.name,
            "who": who, "at": at, "files": restored, "limit": LIMIT}
    res = RollbackResult("finished" if finished else "rolled-back", "", restored=restored,
                         undo_dir=str(occ_dir / UNDO_DIR), card=card)
    res.message = (f"{'finished the interrupted rollback of' if finished else 'rolled back'} {occ_dir.name}: "
                   f"restored {', '.join(restored) or 'nothing further'}; the text it replaced is kept in "
                   f"{occ_dir / UNDO_DIR}. {LIMIT}")
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
    snaps = snapshots(root)
    if not snaps:
        return _drill_done(res, root, write_result)
    name, m = snaps[-1]
    occ = root / name
    res.snapshot, res.outcome = name, "ok"
    snap = occ / SNAP_DIR
    bad = [e["name"] for e in m["files"].values() if e["sha256"] is not None
           and sha256_file(snap / e["name"]) != e["sha256"]]
    if sha256_file(snap / DB_BACKUP) != m.get("db_backup_sha256"):
        bad.append(DB_BACKUP)
    if _step(res, "hash", "failed" if bad else "ok", f"does not match the manifest: {bad}" if bad
             else "every snapshot file and the db backup match their manifest hashes") == "failed":
        return _drill_done(res, root, write_result, failed=True)
    try:
        con = sqlite3.connect(f"file:{snap / DB_BACKUP}?mode=ro", uri=True)
        try:
            verdict = [r[0] for r in con.execute("pragma integrity_check")]
        finally:
            con.close()
    except sqlite3.Error as exc:
        verdict = [f"{type(exc).__name__}: {exc}"]
    ok = verdict == ["ok"]
    if _step(res, "integrity", "ok" if ok else "failed", "integrity_check: ok" if ok else f"{verdict[:3]}") == "failed":
        return _drill_done(res, root, write_result, failed=True)
    if not m.get("post_files"):
        _step(res, "restore", "skipped", "the snapshot has no post-save record, so a rollback of it is refused by design")
        _step(res, "open", "skipped", "no restored copy")
        return _drill_done(res, root, write_result)
    scratch = Path(tempfile.mkdtemp(prefix="levain-dream-drill-"))
    try:
        sdir = scratch / "store"
        sdir.mkdir()
        handle = _w._lock_wrap_file(db.parent / "wrap.lock")
        if handle is _w._ANOTHER_WRAP_RUNNING:
            return _deferred(res, "a consolidate holds the store's wrap lock, so the live store cannot be copied "
                                  "consistently")
        try:
            copy_db = _copy_store(db, sdir)
        except (OSError, sqlite3.Error) as exc:
            _step(res, "restore", "failed", f"the live store could not be copied: {type(exc).__name__}: {exc}")
            return _drill_done(res, root, write_result, failed=True)
        finally:
            _w._unlock_wrap(handle)
        socc = scratch / "occs" / name
        (socc / SNAP_DIR).mkdir(parents=True)
        for e in m["files"].values():
            if e["sha256"] is not None:
                shutil.copy2(snap / e["name"], socc / SNAP_DIR / e["name"])
        # the revert markers stay as the real manifest has them: a finished rollback then refuses on the copy as
        # ``already-reverted`` (skipped), instead of the copy reading "nothing to roll back" (a false failure)
        _write_manifest(socc, {**m, "store_db": str(copy_db)})
        rb = rollback(copy_db, socc, who="drill")
        if rb.outcome == "refused" and rb.code in ("wrap-open", "lock-held"):
            return _deferred(res, f"{rb.code}: a dream is running on the store")
        if rb.outcome == "refused" and rb.code in ("foreign-write", "later-dream", "already-reverted"):
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
