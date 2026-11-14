"""levain.intention_migrate: stored dispositions that no longer exist become ``someday``.

TWO rewrites live here, with one backup helper and one report shape between them:

- ``seed`` -> ``someday`` (ruling H8, :func:`migrate_seed_to_someday`, described first below), and
- an absent / empty / literal ``loop`` disposition -> ``someday`` (the loop fold, board item levain#4, Phill np-10262267 (a),
  :func:`migrate_loop_to_someday`, described at its definition). Q2 (np-ee498a62 (a)): that rewrite changes ONLY the disposition
  and a past ``next`` stays, so the row shows as Overdue; the one function that decides it is :func:`q2_past_next_policy`.

What follows is the seed rewrite's account, and its rules (a read that writes nothing, a verified backup under the store's
lock, one compare-and-set per record, a skipped record named, a second run a no-op) are the loop rewrite's too.

The seed rewrite: the stored disposition ``seed`` becomes ``someday`` (ruling H8).

An older release wrote ``seed`` as the disposition of an operator-inbox item. The word is retired: no write sets it
(``levain.intentions.RETIRED_DISPOSITIONS``), it still READS (a resolved record keeps it for good, because anneal's
``update`` cannot edit a resolved record), and ``levain update`` calls :func:`migrate_seed_to_someday` so an OPEN record
stops carrying it.

What the migration does, in order, and what it refuses:

1. It reads the store file WITHOUT writing it (the raw document, no anneal load: anneal's keyed read backfills origin
   keys and saves, which would change bytes). No open ``seed`` record means it stops here: no backup, the store is not
   touched, a second run is a no-op. An installed anneal-memory older than the pinned one (it keys the open list differently:
   :func:`levain.intentions.guarded_intention_store`) REFUSES the migration here, before any copy: nothing is copied or rewritten.
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
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:                              # pragma: no cover - Windows: guarded writes are refused anyway
    fcntl = None  # type: ignore[assignment]

from levain.intention_legacy import STORE_DOC_KEY_OPEN, STORE_DOC_KEY_OPEN_V1

__all__ = ["BACKUP_SUBDIR", "LEDGER_PREFIX", "LoopMigrationReport", "LoopPlan", "LoopRevertReport", "MigrationReport",
           "migrate_loop_to_someday", "migrate_seed_to_someday", "open_loop_ids", "open_seed_ids", "plan_loop_migration",
           "q2_past_next_policy", "revert_loop_migration"]

BACKUP_SUBDIR = ("backups", "intentions")        # <store dir>/backups/intentions/ (an install: <install>/.levain/backups/intentions/)
_OLD = "seed"
_NEW = "someday"

LABEL_NOTE = ("the provenance label of each rewritten record's disposition reads unverified: a migration is not a "
              "signed edit (its text and domain labels are unchanged)")


@dataclass
class MigrationReport:
    """What one run did. ``migrated`` are the ids whose disposition is now ``someday``; ``skipped`` are
    ``(id, why)`` for a record that moved since it was read; ``refused`` is a reason when the run changed nothing
    because it could not do so safely (an unwritable backup, an unreadable store, an anneal-memory older than the pin);
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
    # The anneal names the rewrite needs come BEFORE the backup (1111-32 L1 MED-1): an anneal-memory older than the pin has
    # no schema-2 open key (the store guard refuses it), and finding that out after the copy left a raw ImportError mid-update and one
    # more backup per retry. Building the store reads and writes nothing (anneal's constructor only keeps the path).
    try:
        from anneal_memory.spores import IntentionError

        from levain.intentions import guarded_intention_store

        store = guarded_intention_store(store_path)
    except (ImportError, RuntimeError) as exc:
        report.refused = (f"the installed anneal-memory cannot do it (levain needs anneal-memory>=2.0.0,<2.1: "
                          f"pip install -U 'anneal-memory>=2.0.0,<2.1'; {exc}); nothing was copied or rewritten")
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


def _read_open_rows(store_path: Path) -> list[Any]:
    """The store document's open list, read raw (no anneal load, nothing written). A missing store has none. A malformed
    document, a schema 1 store (its open records live under another key, so reading it as "none open" would be a lie) or
    an open list that is not a list RAISES ValueError; an unreadable file raises OSError."""
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
    return open_rows


def _scan_open_seeds(store_path: Path) -> list[str]:
    return [str(r["id"]) for r in _read_open_rows(store_path)
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


# ---------------------------------------------------------------------------------------------------------------------
# The loop fold (board item levain#4): an absent / empty / literal ``loop`` disposition becomes ``someday``
# ---------------------------------------------------------------------------------------------------------------------
#
# There is no loop kind in Levain 1.0 (Phill np-10262267 (a)). The READ default (``levain.intentions.disposition_of``) already
# makes every such row a someday on the List, so nothing depends on this rewrite for correctness; it stores what the rows
# read as, so an older levain (which reads a key-absent row as a loop) sees them on its List, and a raw reader sees one name.
# It is the seed rewrite's sibling and follows its pattern: a raw read, the pin check, a verified backup under the store lock,
# ONE compare-and-set per row (``expect_disposition`` = the raw value read: absent -> None, "" -> "", "loop" -> "loop"), a row
# that moved named and left alone, a second run a no-op. Only the ``disposition`` changes: the tier (``parked`` is kept), the
# ``next``, notes and ``origin_key`` ride through, except for what :func:`q2_past_next_policy` adds for a past ``next``.
# An open row whose disposition this release does not know is left as it is and named in the report (it reads as that name
# on the List). A resolved record is never addressed (anneal cannot edit one).
#
# The run's ledger, ``<store dir>/backups/intentions/loopfold-<UTC>.json``, lists the migrated ids and what each held before
# (``prior``: null for an absent key), the store it was written for and how many rows the run left unmigrated. Once a ledger
# with none unmigrated exists FOR THIS STORE (a run that finds nothing writes one too), a later run leaves a KEY-ABSENT row alone (the entity's own add writes one; it reads as someday
# already) and rewrites only a row that carries a literal ``loop`` or an empty / null disposition key; so the rewrite can be undone row by row (:func:`revert_loop_migration`), or all at once
# by restoring the backup copy, which also discards every write made since.

LEDGER_PREFIX = "loopfold-"
_LOOP_READS = (None, "", "loop")                 # the raw values the fold rewrites (absent reads as None)
_OVERDUE_NOTE = "shows as Overdue on the List"


def q2_past_next_policy(row: Mapping[str, Any], now: datetime) -> tuple[dict[str, Any], str | None]:
    """THE Q2 BRANCH, the one place the loop rewrite decides what happens to a row whose ``next`` has passed.

    Q2 (Phill, np-ee498a62 (a), signed 11-13 20:20): the migration changes ONLY the ``disposition``, and a past ``next`` STAYS,
    so the row shows as Overdue on the List. So this returns no extra ``update`` fields and the words the report puts beside the
    row. ``now`` is local and naive (the List reads ``next`` as local time); a date-only ``next`` has passed when its day is
    before today, a timed one when its minute is reached, and anything else has not.

    Returns ``(extra_update_kwargs, note)``: the kwargs ride the SAME one-row ``store.update`` as the disposition (one transaction,
    one compare-and-set) and ``note`` is the report's words for a past-dated row, or None for a row that is not past-dated.

    To flip to option (b) (a past ``next`` moves into a note line and is cleared, so the row lands in Someday), change THIS
    function only: ``return {"next": None, "add_note": f"was due {row['next']}"}, "moves its date into a note and shows under
    Someday"`` for a past-dated row. Nothing else in the rewrite, the dry run or the report names the policy."""
    if not _has_passed(row.get("next"), now):
        return {}, None
    return {}, _OVERDUE_NOTE


def _has_passed(value: Any, now: datetime) -> bool:
    from levain.intentions import _parse_when, is_timed

    when = _parse_when(value)
    if when is None:
        return False
    now = _local_now(now)                        # one naive local basis, whatever zone the caller's clock carries
    return now >= when if is_timed(value) else when.date() < now.date()


def _local_now(now: datetime | None) -> datetime:
    """``now`` as local naive time (the List's clock); the system's own now when none is given."""
    if now is None:
        return datetime.now()
    return now.astimezone().replace(tzinfo=None) if now.tzinfo is not None else now


def _fold_done(dest_dir: Path, store_path: Path) -> bool:
    """True once a loop rewrite has completed FOR THIS STORE under ``dest_dir``: a valid ledger (:func:`_read_ledger`) whose run
    left nothing unmigrated. From then on a key-absent row is what the entity's own add writes and already reads as someday, so
    it is no longer a loop to rewrite. A ledger of another store, a malformed or a reverted one never closes the window."""
    try:
        names = sorted(dest_dir.glob(f"{LEDGER_PREFIX}*.json"))
    except OSError:
        return False
    for p in names:
        doc = _read_ledger(p, store_path)
        if isinstance(doc, dict) and doc["unmigrated"] == 0:
            return True
    return False


def _default_dest(store_path: Path, backup_dir: Path | str | None) -> Path:
    return Path(backup_dir) if backup_dir is not None else store_path.parent.joinpath(*BACKUP_SUBDIR)


def _classify_open_loops(store_path: Path, *, keyless: bool = True
                         ) -> tuple[list[dict[str, Any]], list[tuple[str, Any]]]:
    """``(rows to rewrite, rows with an unknown disposition)`` among the OPEN rows, read raw. A row to rewrite holds no
    disposition key (only while ``keyless``: the first run, before a completed ledger exists) or a None / "" or exactly ``loop``.
    An unknown one holds anything else that is not a name this release reads (``levain.intentions.VALID_DISPOSITIONS``), the
    value included. Raises like :func:`_read_open_rows`."""
    from levain.intentions import VALID_DISPOSITIONS

    targets: list[dict[str, Any]] = []
    unknown: list[tuple[str, Any]] = []
    for row in _read_open_rows(store_path):
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or row.get("status", "open") != "open":
            continue
        d = row.get("disposition")
        if d in _LOOP_READS:
            if keyless or "disposition" in row:
                targets.append(row)
        elif not isinstance(d, str) or d not in VALID_DISPOSITIONS:
            unknown.append((row["id"], d))
    return targets, unknown


def _unknown_line(unknown: list[tuple[str, Any]]) -> str:
    named = ", ".join(f"{rid} ({d!r})" for rid, d in unknown)
    return (f"intentions: {len(unknown)} open record(s) hold a disposition this release does not know and were left as they "
            f"are: {named}; each reads as that name on the List")


@dataclass
class LoopPlan:
    """What :func:`migrate_loop_to_someday` WOULD do, read without writing anything (the dry run). ``past_dated`` are
    ``(id, next, what the Q2 policy says)`` for the rows to rewrite whose ``next`` has passed; ``unknown`` are
    ``(id, value)`` for the open rows it would leave alone."""
    ids: list[str] = field(default_factory=list)
    past_dated: list[tuple[str, str, str]] = field(default_factory=list)
    unknown: list[tuple[str, Any]] = field(default_factory=list)

    def rewrite_lines(self) -> list[str]:
        """The would-rewrite line, and under it the past-dated rows by name (each with the Q2 policy's words)."""
        if not self.ids:
            return []
        out = [f"intentions: {len(self.ids)} open loop record(s) would be rewritten to someday "
               f"({', '.join(self.ids)}); the store is copied first"]
        if self.past_dated:
            named = ", ".join(f"{rid} (next {nxt}: {note})" for rid, nxt, note in self.past_dated)
            out.append(f"  past-dated, kept as they are: {named}")
        return out

    def unknown_line(self) -> str | None:
        return _unknown_line(self.unknown) if self.unknown else None


def plan_loop_migration(paths: Any, *, now: datetime | None = None, backup_dir: Path | str | None = None) -> LoopPlan:
    """The dry run of :func:`migrate_loop_to_someday`: nothing is written, copied or locked. A missing store plans nothing; an
    unreadable or malformed one RAISES (OSError or ValueError), because an empty answer would read as "nothing to do" about a
    store nobody could read."""
    store_path = Path(paths.intentions_json)
    targets, unknown = _classify_open_loops(store_path, keyless=not _fold_done(_default_dest(store_path, backup_dir), store_path))
    local = _local_now(now)
    plan = LoopPlan(ids=[str(r["id"]) for r in targets], unknown=unknown)
    for r in targets:
        try:
            _extra, note = q2_past_next_policy(r, local)
        except Exception:                        # a row the policy cannot read is not a plan failure (the run skips and names it)
            continue
        if note:
            plan.past_dated.append((str(r["id"]), str(r.get("next")), note))
    return plan


def open_loop_ids(paths: Any, *, backup_dir: Path | str | None = None) -> list[str]:
    """The ids :func:`migrate_loop_to_someday` would rewrite (a held update names them), read without writing anything."""
    store_path = Path(paths.intentions_json)
    keyless = not _fold_done(_default_dest(store_path, backup_dir), store_path)
    return [str(r["id"]) for r in _classify_open_loops(store_path, keyless=keyless)[0]]


@dataclass
class LoopMigrationReport(MigrationReport):
    """What one loop rewrite did, over :class:`MigrationReport`'s fields (``migrated`` ids now ``someday``; ``skipped``
    ``(id, why)`` for a row that moved; ``unmigrated`` the skipped ids still an open loop; ``backup``; ``refused``). Adds
    ``past_dated`` (``(id, next, note)`` for each migrated row whose ``next`` had passed), ``unknown`` (the open rows
    left alone for a disposition this release does not know), ``prior`` (what each migrated row held before) and the run's
    ``ledger`` (``ledger_error`` when it could not be written: the rows are migrated, the backup restores them exactly)."""
    past_dated: list[tuple[str, str, str]] = field(default_factory=list)
    unknown: list[tuple[str, Any]] = field(default_factory=list)
    prior: dict[str, Any] = field(default_factory=dict)
    ledger: Path | None = None
    ledger_error: str | None = None

    def lines(self) -> list[str]:
        out: list[str] = []
        if self.refused:
            out.append(f"intentions: the loop -> someday rewrite was not done: {self.refused}")
        if self.migrated:
            out.append(f"intentions: {len(self.migrated)} open loop record(s) rewritten to someday "
                       f"({', '.join(self.migrated)}); the store was copied to {self.backup} first")
            out.append(f"  {LABEL_NOTE}")
            if self.ledger is not None:
                out.append(f"  the rewritten ids are listed in {self.ledger} (the rollback list)")
            elif self.ledger_error:
                out.append(f"  the rollback list could not be written ({self.ledger_error}); the copy above restores them")
            for rid, nxt, note in self.past_dated:
                out.append(f"  {rid}: its next {nxt} has passed and it is kept, so it {note}")
        for rid, why in self.skipped:
            out.append(f"  {rid}: left as it is ({why}); `levain update` retries it")
        if self.unknown:
            out.append(_unknown_line(self.unknown))
        return out


def migrate_loop_to_someday(paths: Any, *, now: datetime | None = None,
                            backup_dir: Path | str | None = None) -> LoopMigrationReport:
    """Rewrite every OPEN row of ``paths.intentions_json`` whose disposition is absent, empty or ``loop`` to ``someday`` (see the
    section comment above). ``paths`` is an ``AnnealPaths``; ``now`` stamps the backup and the ledger and is the clock of the
    past-``next`` test; ``backup_dir`` overrides where the copy and the ledger go."""
    store_path = Path(paths.intentions_json)
    report = LoopMigrationReport()
    dest_dir = _default_dest(store_path, backup_dir)
    keyless = not _fold_done(dest_dir, store_path)
    try:
        targets, unknown = _classify_open_loops(store_path, keyless=keyless)
    except (OSError, ValueError) as exc:
        report.refused = f"the store {store_path} cannot be read ({exc})"
        return report
    report.unknown = unknown
    if not targets:
        if keyless and store_path.parent.is_dir():   # read fine (a store not yet made reads as empty): close the window before the entity's first add
            try:                                 # the ledger holds ids only, no store bytes: no .gitignore change for it
                report.ledger = _write_ledger(dest_dir, now or datetime.now(timezone.utc), report, store_path)
            except OSError as exc:
                report.ledger_error = str(exc)
        return report                            # no pin check, no copy, no store write (idempotent)
    if fcntl is None:                            # pragma: no cover - the guarded writes below need the POSIX lock
        report.refused = "this platform has no file locking, which the compare-and-set needs"
        return report
    try:                                         # the anneal names and the pin check come BEFORE the copy, as the seed rewrite's do
        from anneal_memory.spores import IntentionError

        from levain.intentions import guarded_intention_store

        store = guarded_intention_store(store_path)
    except (ImportError, RuntimeError) as exc:
        report.refused = (f"the installed anneal-memory cannot do it (levain needs anneal-memory>=2.0.0,<2.1: "
                          f"pip install -U 'anneal-memory>=2.0.0,<2.1'; {exc}); nothing was copied or rewritten")
        return report
    when = now or datetime.now(timezone.utc)
    if backup_dir is None:
        from levain.install import _ensure_gitignored

        _ensure_gitignored(store_path.parent, BACKUP_SUBDIR[0] + "/")      # the copy is the private store's bytes: not committable
    try:
        report.backup = _backup(store_path, dest_dir, when)
    except OSError as exc:
        report.refused = f"the backup could not be written ({exc}); nothing was rewritten"
        return report

    local = _local_now(when)
    for row in targets:
        rid = str(row["id"])
        raw = row.get("disposition")
        try:
            extra, note = q2_past_next_policy(row, local)
            store.update(rid, disposition=_NEW, expect_disposition=raw, **extra)
        except IntentionError as exc:            # resolved, re-routed or ambiguous since the read: its writer wins
            report.skipped.append((rid, str(exc)))
        except OSError as exc:
            report.skipped.append((rid, f"the store was unavailable: {exc}"))
        except Exception as exc:                 # never out of `levain update` after the backup is written
            report.skipped.append((rid, f"the store refused it: {type(exc).__name__}: {exc}"))
        else:
            report.migrated.append(rid)
            report.prior[rid] = raw
            if note:
                report.past_dated.append((rid, str(row.get("next")), note))
    if report.skipped:
        # told apart as the seed rewrite's are: a skipped row still an open loop was not rewritten (a store fault, the run is
        # incomplete); one that moved is its writer's
        try:
            still = {str(r["id"]) for r in _classify_open_loops(store_path, keyless=keyless)[0]}
        except (OSError, ValueError):
            still = {rid for rid, _why in report.skipped}
        report.unmigrated = [rid for rid, _why in report.skipped if rid in still]
    if report.migrated:
        try:
            report.ledger = _write_ledger(dest_dir, when, report, store_path)
        except OSError as exc:
            report.ledger_error = str(exc)
    return report


def _write_ledger(dest_dir: Path, when: datetime, report: LoopMigrationReport, store_path: Path) -> Path:
    """``loopfold-<UTC>.json`` beside the backup: the ids this run migrated and what each held (a new name each run)."""
    stem = f"{LEDGER_PREFIX}{when.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"
    doc = {"migrated": list(report.migrated), "prior": dict(report.prior),
           "backup": None if report.backup is None else str(report.backup),
           "unmigrated": len(report.unmigrated), "store": os.path.realpath(store_path),
           "at": when.astimezone(timezone.utc).isoformat()}
    dest_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    while True:
        dest = dest_dir / (f"{stem}.json" if n == 0 else f"{stem}-{n}.json")
        try:
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            n += 1
            continue
        break
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return dest


@dataclass
class LoopRevertReport:
    """What :func:`revert_loop_migration` did: ``restored`` ids (their disposition key cleared again), ``skipped`` ``(id, why)``
    for a row that moved since (re-routed, resolved: its writer's value stands), ``refused`` a reason when it did nothing."""
    restored: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    refused: str | None = None
    ledger_error: str | None = None             # the rows are restored but the ledger could not be marked reverted


def _read_ledger(path: Path | str, store_path: Path) -> dict[str, Any] | str:
    """THE one reader of a loop-fold ledger, used by the revert and by :func:`_fold_done`: the validated ledger dict, or the
    reason it is not usable (a string). Valid is: a JSON object of the current schema written for THIS store (``store`` equal by
    realpath), ``migrated`` a list of ids, ``prior`` a dict whose keys are exactly those ids and whose values are each what a loop
    row reads as (null, "" or ``loop``), ``unmigrated`` an int, and not marked ``reverted``."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"cannot be read ({exc})"
    if not isinstance(doc, dict):
        return "is not a JSON object"
    migrated, prior = doc.get("migrated"), doc.get("prior")
    if not isinstance(migrated, list) or not all(isinstance(i, str) for i in migrated):
        return "has a migrated list that is not a list of ids"
    if not isinstance(prior, dict) or len(prior) != len(migrated) or set(prior) != set(migrated):
        return "has a prior that does not agree with its migrated ids"
    if not all(v is None or (isinstance(v, str) and v in _LOOP_READS) for v in prior.values()):
        return "has a prior value that is not what a loop row held"
    unmigrated = doc.get("unmigrated")
    if not isinstance(unmigrated, int) or isinstance(unmigrated, bool):
        return "has no unmigrated count"
    named = doc.get("store")
    if not isinstance(named, str) or os.path.realpath(named) != os.path.realpath(store_path):
        return f"was written for another store ({named!r})"
    if "reverted" in doc:
        return "was already reverted"
    return doc


def _mark_reverted(ledger: Path | str, store_path: Path, when: datetime) -> None:
    """Set ``reverted`` in the ledger in place, under the store's lock, by writing a sibling file and renaming it over. Raises
    OSError when it cannot."""
    path = Path(ledger)
    lock_fd = os.open(store_path.with_name(store_path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["reverted"] = when.astimezone(timezone.utc).isoformat()
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f, indent=2, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    finally:
        os.close(lock_fd)


def revert_loop_migration(paths: Any, ledger: Path | str) -> LoopRevertReport:
    """Undo a loop rewrite from its ledger, row by row: each migrated id still holding ``someday`` has its disposition key
    CLEARED (``update(disposition=None, expect_disposition="someday")``: a clear passes the write guard, and the compare-and-set
    leaves a row that moved since to its writer). A row that held the literal ``loop`` comes back key-absent, which reads the
    same; restore the backup copy for the exact bytes (it discards every write made since). A past ``next`` was never touched.
    A row someone set to ``someday`` on purpose after the rewrite is cleared too: it reads the same, and the explicit value is
    not kept (the intent is a rollback to what the rows read as).

    The ledger must be a well-formed one written for THIS store (it records the store's path): another store's ledger, a
    malformed one, one already reverted or an unreadable store REFUSES, and nothing is changed. A moved install directory
    changes the store's realpath, so the old ledger refuses (it fails safe; the backup copy still restores).

    After the rows are restored the ledger is marked ``reverted`` in place (under the store lock), so it no longer closes the
    key-absent window and a second revert of it refuses."""
    report = LoopRevertReport()
    store_path = Path(paths.intentions_json)
    doc = _read_ledger(ledger, store_path)
    if isinstance(doc, str):
        report.refused = f"the ledger {ledger} {doc}; nothing was changed"
        return report
    ids = list(doc["migrated"])
    try:
        _read_open_rows(store_path)              # an unreadable store refuses here, as the forward path does
    except (OSError, ValueError) as exc:
        report.refused = f"the store {store_path} cannot be read ({exc}); nothing was changed"
        return report
    try:
        from anneal_memory.spores import IntentionError

        from levain.intentions import guarded_intention_store

        store = guarded_intention_store(Path(paths.intentions_json))
    except (ImportError, RuntimeError) as exc:
        report.refused = f"the installed anneal-memory cannot do it ({exc}); nothing was changed"
        return report
    for rid in ids:
        try:
            store.update(rid, disposition=None, expect_disposition=_NEW)
        except IntentionError as exc:
            report.skipped.append((rid, str(exc)))
        except OSError as exc:
            report.skipped.append((rid, f"the store was unavailable: {exc}"))
        else:
            report.restored.append(rid)
    if not any("unavailable" in why for _rid, why in report.skipped):      # a store fault leaves the revert open to a retry
        try:
            _mark_reverted(ledger, store_path, datetime.now(timezone.utc))
        except (OSError, ValueError) as exc:
            report.ledger_error = str(exc)
    return report
