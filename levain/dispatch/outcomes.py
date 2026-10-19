"""levain.dispatch.outcomes — the per-model outcome ledger's data layer (design §6, F7 slice a).

A run of an executor ends in LABELS: a verifier passed or failed, a contrast compare came out clean or divergent, the
planner recommended or re-filed, a human authorised or adopted, a commit was reverted, a gated call was filed, left
undecided, allowed or denied. Each label is one append-only row. The ledger exists so a small shop can say "this model, on
this route and template, was adopted 41 of 48 times" with the N beside the rate; it measures, it never authorises.

Two rules keep it honest:

* Every label has exactly ONE fixed source (``SOURCES``): human or machine. A row naming the other source is refused, so a
  machine's verdict can never be booked as a human's and a rollup can say which of its numbers a person stood behind.
* A run's identity facts (the model asked for and the one that answered, the route, the harness, the template) are
  fixed once. A later row for the same run that disagrees is refused rather than split across two cells, since a split
  would count one run twice and let a harness bug read as a model verdict.

The store is its own file (``outcomes.db`` in the dispatch directory, the operator's alone, 0700), append-only: SQL
triggers refuse an update, a delete and a replace. Nothing in it is authority; it is a record to read.

The rollup is hierarchical and counts RUNS: a run is a (job, run index) pair, counted once per cell however many labels it
carries. A rate is shown only where a cell holds at least ``min_n`` runs; below that the cell reports its N and the caller
shows the coarser line (:func:`finest_reporting`). Rates are exact fractions, never floats. Stdlib only.
"""
from __future__ import annotations

import os
import sqlite3
import unicodedata
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from levain.autonomic.durable_sqlite import open_durable, write_transaction
from levain.autonomic.durable_sqlite import rollback as _rollback
from levain.cockpit.consent import parse_stamp
from levain.dispatch import DispatchError

__all__ = ["Cell", "DB_NAME", "FORMAT", "LABELS", "OutcomeRow", "OutcomeStore", "REVERTED_CAVEAT", "SOURCES",
           "VERIFIER_KINDS", "VERIFIER_LABELS", "cell_line", "default_directory", "finest_reporting", "rollup"]

DB_NAME = "outcomes.db"
FORMAT = "levain-dispatch-outcomes/1"
UNKNOWN = "unknown"
_ACCESS_BY_OTHERS = 0o077
_FIELD_MAX = 200
_RUN_INDEX_MAX = 2 ** 63 - 1            # SQLite's INTEGER: past it a bind raises OverflowError, never a refusal

# label -> the one source it may carry (design §6's table). A verifier or a contrast compare is a machine on a
# human-owned criterion; the planner is a model, so a machine; a signature is a human; a revert is a human act
# found in git; a gated call filed or left undecided is the queue's clock, a machine; allowed or denied is a signed
# answer or a human-signed grant, a human.
SOURCES: Mapping[str, str] = {
    "verifier_pass": "machine",
    "verifier_fail": "machine",
    "contrast_clean": "machine",
    "contrast_divergent": "machine",
    "planner_recommends": "machine",
    "planner_refiles": "machine",
    "authorised": "human",
    "adopted": "human",
    "reverted_14d": "human",
    "gated_filed": "machine",
    "gated_undecided": "machine",
    "gated_allowed": "human",
    "gated_denied": "human",
}
LABELS: tuple[str, ...] = tuple(SOURCES)
VERIFIER_LABELS: frozenset[str] = frozenset({"verifier_pass", "verifier_fail"})
# an in-process verifier runs inside the executor's reach and is forgeable; the ledger records which kind spoke
VERIFIER_KINDS: tuple[str, ...] = ("black-box", "in-process")

REVERTED_CAVEAT = ("reverted_14d is survivor-biased: only a commit someone looked at again within 14 days can be "
                   "reverted, so a low rate is not proof of quality.")


def default_directory() -> Path:
    """``<levain home>/dispatch``: ``$LEVAIN_HOME/dispatch`` if set, else ``~/.levain/dispatch``, made absolute."""
    home = os.environ.get("LEVAIN_HOME")
    return ((Path(home).expanduser() if home else Path.home() / ".levain") / "dispatch").resolve()


def _one_line(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise DispatchError(f"{name} must be a string")
    if any(unicodedata.category(c)[0] == "C" or unicodedata.category(c) in ("Zl", "Zp") for c in value):
        raise DispatchError(f"{name} must be one line of plain text: no control characters")
    if not 1 <= len(value.strip()) <= _FIELD_MAX or value != value.strip():
        raise DispatchError(f"{name} must be 1 to {_FIELD_MAX} characters with no edge whitespace")
    return value


@dataclass(frozen=True)
class OutcomeRow:
    """One label on one run. ``model_answered`` is what the gateway saw answer (``"unknown"`` when it did not say);
    ``harness`` is the tool and its version as one string. ``verifier_kind`` is required on a verifier label and
    refused on any other. ``at`` is a ledger timestamp (``levain.cockpit.consent.utc_stamp``)."""
    job_id: str
    run_index: int
    model_requested: str
    model_answered: str
    route: str
    harness: str
    template_id: str
    label: str
    source: str
    at: str
    verifier_kind: str | None = None

    def __post_init__(self) -> None:
        for name in ("job_id", "model_requested", "model_answered", "route", "harness", "template_id"):
            _one_line(name, getattr(self, name))
        if type(self.run_index) is not int or not 0 <= self.run_index <= _RUN_INDEX_MAX:
            raise DispatchError(f"run_index must be an integer from 0 to {_RUN_INDEX_MAX} (SQLite's INTEGER)")
        if self.label not in SOURCES:
            raise DispatchError(f"label must be one of {', '.join(LABELS)}: {self.label!r}")
        if self.source != SOURCES[self.label]:
            raise DispatchError(f"{self.label} is a {SOURCES[self.label]} label; a row cannot book it as "
                                f"{self.source!r}")
        if self.label in VERIFIER_LABELS:
            if self.verifier_kind not in VERIFIER_KINDS:
                raise DispatchError(f"a verifier label needs a verifier_kind of {' or '.join(VERIFIER_KINDS)}")
        elif self.verifier_kind is not None:
            raise DispatchError("verifier_kind belongs to a verifier label only")
        try:
            parse_stamp(self.at)
        except Exception as exc:                                 # noqa: BLE001 - any unparsable stamp is refused
            raise DispatchError(f"at must be a ledger timestamp: {exc}") from exc

    @property
    def run(self) -> tuple[str, int]:
        return (self.job_id, self.run_index)


# the identity facts of a run: fixed once, whichever label arrives first
_RUN_FACTS = ("model_requested", "model_answered", "route", "harness", "template_id")
_COLUMNS = ("job_id", "run_index", "model_requested", "model_answered", "route", "harness", "template_id", "label",
            "source", "verifier_kind", "at")


def _append_only(table: str, message: str) -> tuple[str, str]:
    return (f"CREATE TRIGGER IF NOT EXISTS {table}_frozen BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{message}'); END",
            f"CREATE TRIGGER IF NOT EXISTS {table}_kept BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{message}'); END")


def _label_check() -> str:
    return ", ".join(f"'{label}'" for label in LABELS)


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS outcomes (n INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, "
    "run_index INTEGER NOT NULL CHECK (run_index >= 0), model_requested TEXT NOT NULL, model_answered TEXT NOT NULL, "
    "route TEXT NOT NULL, harness TEXT NOT NULL, template_id TEXT NOT NULL, "
    f"label TEXT NOT NULL CHECK (label IN ({_label_check()})), "
    "source TEXT NOT NULL CHECK (source IN ('human','machine')), "
    "verifier_kind TEXT CHECK (verifier_kind IN ('black-box','in-process')), at TEXT NOT NULL, "
    "UNIQUE (job_id, run_index, label))",
    "CREATE INDEX IF NOT EXISTS outcomes_run ON outcomes (job_id, run_index)",
    *_append_only("outcomes", "outcomes are append-only"),
    # INSERT OR REPLACE deletes the old row without firing a delete trigger: refuse a row that reuses a key
    "CREATE TRIGGER IF NOT EXISTS outcomes_no_replace BEFORE INSERT ON outcomes "
    "WHEN EXISTS (SELECT 1 FROM outcomes WHERE (job_id = NEW.job_id AND run_index = NEW.run_index "
    "AND label = NEW.label) OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'an outcome is never replaced'); END",
)


class OutcomeStore:
    """``<directory>/outcomes.db``, ``directory`` the dispatch directory by default (the operator's alone, 0700,
    refused otherwise). Every write is one ``BEGIN IMMEDIATE``; every read is a read-only snapshot that never
    creates the store."""

    def __init__(self, directory: Path | str | None = None) -> None:
        self.directory = Path(directory) if directory is not None else default_directory()
        self.path = self.directory / DB_NAME
        self._ready = False

    def _check_directory(self) -> None:
        if not self.directory.exists():
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)   # the umask can only narrow it
        st = os.lstat(self.directory)
        if os.path.islink(self.directory) or not os.path.isdir(self.directory):
            raise DispatchError(f"outcome store directory {self.directory} is not a directory")
        if st.st_uid != os.getuid():
            raise DispatchError(f"outcome store directory {self.directory} is not owned by uid {os.getuid()}")
        if st.st_mode & _ACCESS_BY_OTHERS:
            raise DispatchError(f"outcome store directory {self.directory} is reachable by group or others "
                                f"(mode {st.st_mode & 0o777:o}); it must be 0700")

    def _connect(self) -> sqlite3.Connection:
        if self._ready:
            if not self.path.exists():
                raise DispatchError(f"outcome store {self.path} has disappeared")
        else:
            self._check_directory()
        conn = open_durable(self.path)
        try:
            if not self._ready:
                os.chmod(self.path, 0o600)
                self._ensure_schema(conn)
                self._ready = True
        except BaseException:
            conn.close()
            raise
        return conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            row = (conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
                   if "meta" in tables else None)
            if tables and row is None:
                raise DispatchError("the database holds tables but no outcome-store format marker")
            if row is not None and row[0] != FORMAT:
                raise DispatchError(f"store format {row[0]!r} is not {FORMAT!r}")
            for stmt in _SCHEMA:
                conn.execute(stmt)
            if row is None:
                conn.execute("INSERT INTO meta (key, value) VALUES ('format', ?)", (FORMAT,))
            conn.execute("COMMIT")
        except BaseException:
            _rollback(conn)
            raise

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with write_transaction(self._connect) as conn:
            yield conn

    def append(self, row: OutcomeRow) -> bool:
        """Append ``row``. ``True`` when it was written, ``False`` when the same (job, run, label) is already
        there with the same facts (a retry: idempotent, the first ``at`` stands). Refused with
        :class:`DispatchError` when the run's identity facts disagree with what the run already carries, or when the
        same (job, run, label) is there with other facts or another source: a conflicting retry is a bug to see,
        not a correction to accept, since nothing here is ever updated."""
        if not isinstance(row, OutcomeRow):
            raise DispatchError("append takes an OutcomeRow")
        with self._write() as conn:
            same = conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM outcomes WHERE job_id = ? AND run_index = ? AND label = ?",
                (row.job_id, row.run_index, row.label)).fetchone()
            if same is not None:
                had = dict(zip(_COLUMNS, same))
                for name in (*_RUN_FACTS, "source", "verifier_kind"):
                    if had[name] != getattr(row, name):
                        raise DispatchError(f"{row.label} for run {row.job_id}#{row.run_index} is already "
                                            f"recorded with another {name}: {had[name]!r}, not {getattr(row, name)!r}")
                return False
            known = conn.execute(
                f"SELECT {', '.join(_RUN_FACTS)} FROM outcomes WHERE job_id = ? AND run_index = ? LIMIT 1",
                (row.job_id, row.run_index)).fetchone()
            if known is not None:
                for name, value in zip(_RUN_FACTS, known):
                    if value != getattr(row, name):
                        raise DispatchError(f"run {row.job_id}#{row.run_index} is already recorded with {name} "
                                            f"{value!r}; a row naming {getattr(row, name)!r} would split the run")
            conn.execute(f"INSERT INTO outcomes ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' for _ in _COLUMNS)})",
                         tuple(getattr(row, c) for c in _COLUMNS))
        return True

    def rows(self) -> list[OutcomeRow]:
        """Every row in the order it was written; empty when there is no store yet."""
        try:
            empty = self.path.stat().st_size == 0
        except FileNotFoundError:
            empty = True
        if empty:
            return []
        self._check_directory()
        conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0,
                               isolation_level=None)
        try:
            conn.execute("BEGIN")
            have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            fmt = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone() if "meta" in have else None
            if fmt is None or fmt[0] != FORMAT or "outcomes" not in have:
                raise DispatchError(f"{self.path} is not an outcome store of format {FORMAT!r}")
            got = conn.execute(f"SELECT {', '.join(_COLUMNS)} FROM outcomes ORDER BY n").fetchall()
            conn.execute("COMMIT")
        finally:
            conn.close()
        return [OutcomeRow(**dict(zip(_COLUMNS, r))) for r in got]


# --- the rollup ------------------------------------------------------------------------------------------------

_LEVELS = ("model", "route", "harness", "template")


@dataclass(frozen=True)
class Cell:
    """One rollup cell. ``key`` is the path down the hierarchy, 1 to 4 parts: ``(model,)``, then route, harness,
    template. The model part is ``(name, basis)`` with basis ``"answered"`` or ``"requested"``: a model the gateway
    never saw answer is keyed by what was asked for and marked, never merged with an answered one. ``n`` is the
    distinct runs counted; ``counts`` the distinct runs carrying each label; ``min_n`` the floor below which no
    rate is shown."""
    key: tuple
    n: int
    counts: Mapping[str, int]
    min_n: int

    @property
    def level(self) -> int:
        return len(self.key)

    @property
    def reporting(self) -> bool:
        return self.n >= self.min_n

    def rate(self, label: str) -> Fraction | None:
        """``count / n`` as an exact fraction, or ``None`` below ``min_n`` (the cell then reports only its N)."""
        if label not in SOURCES:
            raise DispatchError(f"unknown label {label!r}")
        return Fraction(self.counts.get(label, 0), self.n) if self.reporting and self.n else None


def _model_key(row: OutcomeRow) -> tuple[str, str]:
    if row.model_answered != UNKNOWN:
        return (row.model_answered, "answered")
    return (row.model_requested, "requested")


def rollup(rows: Iterable[OutcomeRow], *, min_n: int) -> dict[tuple, Cell]:
    """Roll ``rows`` up model -> model×route -> model×route×harness -> model×route×harness×template, one
    :class:`Cell` per distinct key at each level. A run's facts come from its first row in ``rows``; the store
    refuses a row that disagrees, so a list read from it never holds two versions of a run."""
    if type(min_n) is not int or min_n < 1:
        raise DispatchError("min_n must be a positive integer")
    runs: dict[tuple[str, int], tuple[tuple, set[str]]] = {}
    for row in rows:
        entry = runs.get(row.run)
        if entry is None:
            path = (_model_key(row), row.route, row.harness, row.template_id)
            entry = runs[row.run] = (path, set())
        entry[1].add(row.label)
    counts: dict[tuple, dict[str, int]] = {}
    totals: dict[tuple, int] = {}
    for path, labels in runs.values():
        for depth in range(1, len(_LEVELS) + 1):
            key = path[:depth]
            totals[key] = totals.get(key, 0) + 1
            per = counts.setdefault(key, {})
            for label in labels:
                per[label] = per.get(label, 0) + 1
    return {key: Cell(key, totals[key], dict(counts[key]), min_n) for key in totals}


def finest_reporting(cells: Mapping[tuple, Cell], key: tuple) -> Cell | None:
    """The finest cell on ``key``'s path (``key`` itself, then each coarser prefix) that has at least ``min_n``
    runs, or ``None`` when even the model's own cell is below it. This is the coarser line a thin cell falls back
    to."""
    for depth in range(len(key), 0, -1):
        cell = cells.get(key[:depth])
        if cell is not None and cell.reporting:
            return cell
    return None


def cell_line(cell: Cell, label: str = "adopted") -> str:
    """The panel line for ``cell`` and ``label``: ``glm-5.2, R0 `fix-lint`: 41/48 adopted (N=48)``, the harness
    between route and template when the cell is that deep. A cell below ``min_n`` shows only its N. A
    ``reverted_14d`` line carries the survivor-bias caveat on a line of its own."""
    if label not in SOURCES:
        raise DispatchError(f"unknown label {label!r}")
    name, basis = cell.key[0]
    parts = [name if basis == "answered" else f"{name} (requested; answering model unknown)"]
    if cell.level >= 2:
        parts.append(cell.key[1])
    if cell.level >= 3:
        parts.append(cell.key[2])
    head = ", ".join(parts)
    if cell.level >= 4:
        head += f" `{cell.key[3]}`"
    if cell.reporting:
        line = f"{head}: {cell.counts.get(label, 0)}/{cell.n} {label.replace('_', ' ')} (N={cell.n})"
    else:
        line = f"{head}: N={cell.n}, below min_n={cell.min_n}: no rate shown"
    return f"{line}\n{REVERTED_CAVEAT}" if label == "reverted_14d" else line

