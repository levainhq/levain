"""levain.intention_migrate: the stored disposition ``seed`` becomes ``someday`` (ruling H8).

An older release wrote ``seed`` as the disposition of an operator-inbox item. The word is retired: no write sets it
(``levain.intentions.RETIRED_DISPOSITIONS``), it still READS (a resolved record keeps it for good, because anneal's
``update`` cannot edit a resolved record), and ``levain update`` calls :func:`migrate_seed_to_someday` so an OPEN record
stops carrying it.

What the migration does, in order, and what it refuses:

1. It reads the store file WITHOUT writing it (the raw document, no anneal load: anneal's keyed read backfills origin
   keys and saves, which would change bytes). No open ``seed`` record means it stops here: no backup, the store is not
   touched, a second run is a no-op.
2. It copies the store file to ``<store dir>/backups/intentions/<store file name>.<UTC time>`` while holding the store's
   own lock (the sibling ``<store>.lock`` anneal serialises every write on), and checks the copy equals the bytes it read.
   A backup that cannot be written REFUSES the migration: nothing is rewritten.
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

from levain.intention_legacy import STORE_DOC_KEY_OPEN

__all__ = ["BACKUP_SUBDIR", "MigrationReport", "migrate_seed_to_someday"]

BACKUP_SUBDIR = ("backups", "intentions")        # <store dir>/backups/intentions/ (an install: <install>/.levain/backups/intentions/)
_OLD = "seed"
_NEW = "someday"

LABEL_NOTE = ("the provenance label of each rewritten record's disposition reads unverified: a migration is not a "
              "signed edit (its text and domain labels are unchanged)")


@dataclass
class MigrationReport:
    """What one run did. ``migrated`` are the ids whose disposition is now ``someday``; ``skipped`` are
    ``(id, why)`` for a record that moved since it was read; ``refused`` is a reason when the run changed nothing
    because it could not do so safely (an unwritable backup, an unreadable store); ``backup`` is the copy taken."""
    migrated: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    backup: Path | None = None
    refused: str | None = None

    @property
    def count(self) -> int:
        return len(self.migrated)

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
    when = now or datetime.now(timezone.utc)
    dest_dir = Path(backup_dir) if backup_dir is not None else store_path.parent.joinpath(*BACKUP_SUBDIR)
    try:
        report.backup = _backup(store_path, dest_dir, when)
    except OSError as exc:
        report.refused = f"the backup could not be written ({exc}); nothing was rewritten"
        return report

    from anneal_memory.spores import IntentionError
    from levain.intentions import guarded_intention_store

    store = guarded_intention_store(store_path)
    for rid in ids:
        try:
            store.update(rid, disposition=_NEW, expect_disposition=_OLD)
        except IntentionError as exc:            # resolved, re-routed or ambiguous since the read: its writer wins
            report.skipped.append((rid, str(exc)))
        except OSError as exc:
            report.skipped.append((rid, f"the store was unavailable: {exc}"))
        else:
            report.migrated.append(rid)
    return report


def _open_seed_ids(store_path: Path) -> list[str]:
    """The ids of the open records whose stored disposition is ``seed``, read raw. A missing store has none."""
    try:
        data = json.loads(store_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(data, dict):
        raise ValueError("the document is not a JSON object")
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
