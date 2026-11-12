"""levain.intention_migrate: the stored disposition ``seed`` becomes ``someday`` (ruling H8).

An older release wrote ``seed`` as the disposition of an operator-inbox item. The word is retired: no write sets it
(``levain.intentions.RETIRED_DISPOSITIONS``), it still READS (a resolved record keeps it for good, because anneal's
``update`` cannot edit a resolved record), and ``levain update`` calls :func:`migrate_seed_to_someday` so an OPEN record
stops carrying it.

What the migration does, in order, and what it refuses:

1. It reads the store file WITHOUT writing it (the raw document, no anneal load: anneal's keyed read backfills origin
   keys and saves, which would change bytes). No open ``seed`` record means it stops here: no backup, the store is not
   touched, a second run is a no-op. An installed anneal-memory below 1.0.7 (no ``Intention*`` names, no ``id_prefix``)
   REFUSES the migration here, before any copy: nothing is copied or rewritten.
2. It copies the store file to ``<store dir>/backups/intentions/<store file name>.<UTC time>`` while holding the store's
   own lock (the sibling ``<store>.lock`` anneal serialises every write on), and checks the copy equals the bytes it read.
   A backup that cannot be written REFUSES the migration: nothing is rewritten. The default backup place is git-ignored
   first (``backups/`` in the store dir's ``.gitignore``).
3. For each open ``seed`` record it calls anneal's ``update(disposition="someday", expect_disposition="seed")``: one
   transaction under the store lock per record, compare-and-set on the disposition, so a record that moved since step 1
   (resolved, re-routed, edited by a concurrent writer) is skipped and named in the report, never overwritten. Only the
   disposition changes: ``attention: talk`` and every other field ride through untouched. A resolved record is never
   addressed.

Provenance: a record's cockpit label is a claim about a SIGNED edit, held as the sha256 of each authority field's value
(``levain.cockpit.labels``). A migration is not a signed edit, so the rewritten ``disposition`` no longer matches any
signed fire and reads ``unverified``; the record's ``text`` and ``domain`` keep their labels. The report says so.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:                              # pragma: no cover - Windows: guarded writes are refused anyway
    fcntl = None  # type: ignore[assignment]

from levain.intention_legacy import STORE_DOC_KEY_OPEN, STORE_DOC_KEY_OPEN_V1

__all__ = ["BACKUP_SUBDIR", "MigrationReport", "migrate_seed_to_someday", "open_seed_ids"]

BACKUP_SUBDIR = ("backups", "intentions")        # <store dir>/backups/intentions/ (an install: <install>/.levain/backups/intentions/)
_OLD = "seed"
_NEW = "someday"

LABEL_NOTE = ("the provenance label of each rewritten record's disposition reads unverified: a migration is not a "
              "signed edit (its text and domain labels are unchanged)")


@dataclass
class MigrationReport:
    """What one run did. ``migrated`` are the ids whose disposition is now ``someday``; ``skipped`` are
    ``(id, why)`` for a record that moved since it was read; ``refused`` is a reason when the run changed nothing
    because it could not do so safely (an unwritable backup, an unreadable store, an anneal-memory below 1.0.7);
    ``unmigrated`` are the skipped ids that are STILL an open ``seed`` after the run (a store fault, not a record that
    moved: that one is its writer's); ``backup`` is the copy taken."""
    migrated: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    unmigrated: list[str] = field(default_factory=list)
    backup: Path | None = None
    refused: str | None = None

    @property
    def count(self) -> int:
        return len(self.migrated)

    @property
    def incomplete(self) -> bool:
        """True when an open ``seed`` was left behind by a store fault: `levain update` ends non-zero on it."""
        return bool(self.unmigrated)

    @property
    def failed(self) -> bool:
        """True when `levain update` must end non-zero: the run refused, or it left an open ``seed`` behind."""
        return self.refused is not None or self.incomplete

    def lines(self) -> list[str]:
        """The operator-facing lines `levain update` prints; empty when there was nothing to do."""
        out: list[str] = []
        if self.refused:
            out.append(f"intentions: the seed -> someday rewrite was not done: {self.refused}")
        if self.migrated:
            out.append(f"intentions: {len(self.migrated)} open seed record(s) rewritten to someday "
                       f"({', '.join(self.migrated)}); the store was copied to {self.backup} first")
            out.append(f"  {LABEL_NOTE}")
        for rid, why in self.skipped:
            out.append(f"  {rid}: left as it is ({why}); `levain update` retries it")
        return out


def migrate_seed_to_someday(paths: Any, *, now: datetime | None = None,
                            backup_dir: Path | str | None = None) -> MigrationReport:
    """Rewrite every OPEN ``seed`` record of ``paths.intentions_json`` to ``someday`` (see the module docstring).
    ``paths`` is an ``AnnealPaths``. ``now`` stamps the backup name; ``backup_dir`` overrides where it goes."""
    store_path = Path(paths.intentions_json)
    report = MigrationReport()
    try:
        ids = _open_seed_ids(store_path)
    except (OSError, ValueError) as exc:
        report.refused = f"the store {store_path} cannot be read ({exc})"
        return report
    if not ids:
        return report
    if fcntl is None:                            # pragma: no cover - the guarded writes below need the POSIX lock
        report.refused = "this platform has no file locking, which the compare-and-set needs"
        return report
    # The anneal names the rewrite needs come BEFORE the backup (1111-32 L1 MED-1): an anneal-memory below 1.0.7 has no
    # `Intention*` names and no `id_prefix`, and finding that out after the copy left a raw ImportError mid-update and one
    # more backup per retry. Building the store reads and writes nothing (anneal's constructor only keeps the path).
    try:
        from anneal_memory.spores import IntentionError

        from levain.intentions import guarded_intention_store

        store = guarded_intention_store(store_path)
    except (ImportError, RuntimeError) as exc:
        report.refused = (f"the installed anneal-memory cannot do it (levain needs anneal-memory>=1.0.7: "
                          f"pip install -U 'anneal-memory>=1.0.7,<1.1'; {exc}); nothing was copied or rewritten")
        return report
    when = now or datetime.now(timezone.utc)
    dest_dir = Path(backup_dir) if backup_dir is not None else store_path.parent.joinpath(*BACKUP_SUBDIR)
    if backup_dir is None:
        # the copy is the private store's bytes: in a repo-tracked install it must not be committable (1111-32 E2), the
        # same `backups/` line the seed-file backups add to the store dir's .gitignore (best-effort, as theirs)
        from levain.install import _ensure_gitignored

        _ensure_gitignored(store_path.parent, BACKUP_SUBDIR[0] + "/")
    try:
        report.backup = _backup(store_path, dest_dir, when)
    except OSError as exc:
        report.refused = f"the backup could not be written ({exc}); nothing was rewritten"
        return report

    for rid in ids:
        try:
            store.update(rid, disposition=_NEW, expect_disposition=_OLD)
        except IntentionError as exc:            # resolved, re-routed or ambiguous since the read: its writer wins
            report.skipped.append((rid, str(exc)))
        except OSError as exc:
            report.skipped.append((rid, f"the store was unavailable: {exc}"))
        except Exception as exc:                 # never out of `levain update` after the backup is written
            report.skipped.append((rid, f"the store refused it: {type(exc).__name__}: {exc}"))
        else:
            report.migrated.append(rid)
    if report.skipped:
        # A skip is a plain compare-and-set loss (the row moved: resolved, re-routed, edited) or a store fault. The two
        # are told apart by the store itself: a skipped row that is STILL an open `seed` was not rewritten and is left for
        # the next run, so the run is incomplete; one that moved is its writer's and is not.
        try:
            still = set(_scan_open_seeds(store_path))
        except (OSError, ValueError):
            still = {rid for rid, _why in report.skipped}   # cannot tell, so none is assumed done
        report.unmigrated = [rid for rid, _why in report.skipped if rid in still]
    return report


def open_seed_ids(paths: Any) -> list[str]:
    """What :func:`migrate_seed_to_someday` WOULD rewrite, read without writing anything (a dry run's plan). A missing
    store has none. An unreadable or malformed one RAISES (OSError or ValueError): an empty answer there would read as
    "nothing to do" about a store nobody could read, so each caller says it cannot tell."""
    return _open_seed_ids(Path(paths.intentions_json))


def _open_seed_ids(store_path: Path) -> list[str]:
    """The ids of the open records whose stored disposition is ``seed``, read raw. A missing store has none."""
    return _scan_open_seeds(store_path)


def _scan_open_seeds(store_path: Path) -> list[str]:
    try:
        data = json.loads(store_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(data, dict):
        raise ValueError("the document is not a JSON object")
    if STORE_DOC_KEY_OPEN_V1 in data:       # a schema 1 store reads as "no open seeds" if its key is simply absent
        raise ValueError(f"the store is schema 1 (open records under {STORE_DOC_KEY_OPEN_V1!r}); convert it once: "
                         f"anneal-memory intention migrate-key --file {store_path}")
    open_rows = data.get(STORE_DOC_KEY_OPEN, [])
    if not isinstance(open_rows, list):
        raise ValueError("the open list is not a list")
    return [str(r["id"]) for r in open_rows
            if isinstance(r, dict) and r.get("disposition") == _OLD and r.get("status", "open") == "open"
            and isinstance(r.get("id"), str)]


def _backup(store_path: Path, dest_dir: Path, when: datetime) -> Path:
    """Copy the store file to a new name in ``dest_dir``, under the store's own lock, and prove the copy equals the bytes
    read. Raises OSError when it cannot; a partial copy is removed."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{store_path.name}.{when.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"
    lock_fd = os.open(store_path.with_name(store_path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        data = store_path.read_bytes()
        n = 0
        while True:
            dest = dest_dir / (stem if n == 0 else f"{stem}-{n}")
            try:
                fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                n += 1
                continue
            break
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            if dest.read_bytes() != data:
                raise OSError(f"the copy at {dest} does not equal the store")
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
    finally:
        os.close(lock_fd)
    return dest
