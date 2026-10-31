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
carries. Each label divides by its OWN denominator (``DENOMINATORS``): the runs that could have carried it. A run still
awaiting a human is not a miss against the adopted rate, and a run that never reached a verifier is not one against the
verifier rate. A rate is shown only where that label's N is at least ``min_n``; below that the cell reports the N and the
caller shows the coarser line (:func:`finest_reporting`). Rates are exact fractions, never floats. Stdlib only.

What a run's labels may say together is fixed at the door: a run carries at most one of each exclusive pair (a verifier
pass or fail, a clean or divergent contrast, an adoption or a close without one, a planner recommendation or re-file), and a
retry is a new run index. Verifier rates are kept per verifier kind, never pooled. Rates compare cells on different
template mixes; they are not model quality.

Known edges, by design here: a gated call is counted per RUN (one row per job, run and label), so the gated words say "runs
with a gated call" and per-call counts wait on the gateway's lineage (wave 3); a run with mixed gated calls can carry both
``gated_allowed`` and ``gated_denied``, so those two can sum past 1. A numerator row outside its denominator (a
``reverted_14d`` on an adoption not yet old enough or never scanned) is dropped, not counted.
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

from levain import unshowable
from levain.autonomic.durable_sqlite import open_durable, write_transaction
from levain.autonomic.durable_sqlite import rollback as _rollback
from levain.cockpit.consent import parse_stamp
from levain.dispatch import DispatchError

__all__ = ["Cell", "DB_NAME", "DENOMINATORS", "FORMAT", "LABELS", "MODEL_QUALITY_CAVEAT", "OutcomeRow", "OutcomeStore",
           "Panel", "PanelEntry", "PanelRow", "REVERTED_CAVEAT", "REVERT_WINDOW", "SOURCES", "STATS", "VERIFIER_KINDS",
           "VERIFIER_LABELS", "cell_line",
           "default_directory", "finest_reporting", "panel", "rollup"]

DB_NAME = "outcomes.db"
FORMAT = "levain-dispatch-outcomes/3"
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
    # the job store's terminal state for a run that did not end adopted (failed or cancelled): the fact that makes a
    # run's decision FINAL without an adoption, so the adopted and authorised rates can leave out runs still waiting
    "closed_without_adoption": "machine",
    # the revert scan ran over this adoption: without it an old adoption with no revert label is unscanned, not clean
    "revert_scanned": "machine",
}
LABELS: tuple[str, ...] = tuple(SOURCES)
VERIFIER_LABELS: frozenset[str] = frozenset({"verifier_pass", "verifier_fail"})
# an in-process verifier runs inside the executor's reach and is forgeable; the ledger records which kind spoke
VERIFIER_KINDS: tuple[str, ...] = ("black-box", "in-process")

# labels one run may never carry together: the second is refused, a retry is a new run index
_EXCLUSIVE: tuple[tuple[str, str], ...] = (("verifier_pass", "verifier_fail"), ("contrast_clean", "contrast_divergent"),
                                           ("adopted", "closed_without_adoption"),
                                           ("planner_recommends", "planner_refiles"))
_RIVALS: Mapping[str, tuple[str, ...]] = {a: (b,) for a, b in _EXCLUSIVE} | {b: (a,) for a, b in _EXCLUSIVE}

# how long an adoption must have stood before its revert label can exist
REVERT_WINDOW = timedelta(days=14)

MODEL_QUALITY_CAVEAT = ("Rates compare models on different template mixes, so they are not model quality: a model given the "
                        "harder templates will rate lower for that alone.")

REVERTED_CAVEAT = ("reverted_14d is survivor-biased: only a commit someone looked at again within 14 days can be "
                   "reverted, so a low rate is not proof of quality.")


def default_directory() -> Path:
    """the consent directory (:func:`levain.consent.queue.default_queue_dir`), beside ``questions.db``: dispatch
    reads no ``$LEVAIN_HOME`` of its own, so the confinement floor that keeps every agent out of that directory
    covers dispatch control state too, with no second name for it to learn."""
    from levain.consent.queue import default_queue_dir

    return default_queue_dir()


def _one_line(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise DispatchError(f"{name} must be a string")
    hidden = unshowable.describe(value, selectors=False)
    if hidden is not None:
        raise DispatchError(f"{name} must be one line of plain text that shows as itself: it holds {hidden}")
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


@contextmanager
def _not_a_store(path: Path) -> Iterator[None]:
    """A file that is not a SQLite database (or a damaged one) as :class:`DispatchError`. Only the bare
    ``sqlite3.DatabaseError`` SQLite raises for NOTADB and CORRUPT is turned: its subclasses (a lock wait, an
    append-only trigger's IntegrityError) keep their own type."""
    try:
        yield
    except sqlite3.DatabaseError as exc:
        if type(exc) is not sqlite3.DatabaseError:
            raise
        raise DispatchError(f"{path} is not an outcome store: {str(exc)[:200]}") from None


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
        with _not_a_store(self.path), write_transaction(self._connect) as conn:
            yield conn

    def append(self, row: OutcomeRow) -> bool:
        """Append ``row``. ``True`` when it was written, ``False`` when the same (job, run, label) is already
        there with the same facts (a retry: idempotent, the first ``at`` stands). Refused with
        :class:`DispatchError` when the run's identity facts disagree with what the run already carries, or when the
        same (job, run, label) is there with other facts or another source: a conflicting retry is a bug to see,
        not a correction to accept, since nothing here is ever updated. A label whose exclusive partner the run already
        carries is refused the same way."""
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
            for rival in _RIVALS.get(row.label, ()):
                if conn.execute("SELECT 1 FROM outcomes WHERE job_id = ? AND run_index = ? AND label = ?",
                                (row.job_id, row.run_index, rival)).fetchone():
                    raise DispatchError(f"run {row.job_id}#{row.run_index} already carries {rival}; {row.label} would "
                                        f"contradict it (a retry is a new run_index)")
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
        with _not_a_store(self.path):
            conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0,
                                   isolation_level=None)
            try:
                conn.execute("BEGIN")
                have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                fmt = (conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
                       if "meta" in have else None)
                if fmt is None or fmt[0] != FORMAT or "outcomes" not in have:
                    raise DispatchError(f"{self.path} is not an outcome store of format {FORMAT!r}")
                got = conn.execute(f"SELECT {', '.join(_COLUMNS)} FROM outcomes ORDER BY n").fetchall()
                conn.execute("COMMIT")
            finally:
                conn.close()
        return [OutcomeRow(**dict(zip(_COLUMNS, r))) for r in got]


# --- the rollup ------------------------------------------------------------------------------------------------

_LEVELS = ("model", "route", "harness", "template")

def _stat(label: str, kind: str | None) -> str:
    return f"{label}:{kind}" if label in VERIFIER_LABELS else label


# The rates a cell can report. A verifier label is a stat per verifier kind (``verifier_pass:black-box``), never pooled:
# an in-process verifier is forgeable by the code under test, and pooling it would lend that to the black-box rate.
# ``revert_scanned`` is a fact the revert denominator needs, not a rate.
STATS: tuple[str, ...] = tuple(
    _stat(label, kind) for label in LABELS if label != "revert_scanned"
    for kind in (VERIFIER_KINDS if label in VERIFIER_LABELS else (None,)))

# stat -> the denominator group it divides by. A run is in a group's N when it carries one of the group's qualifying
# labels (``_QUALIFIES``); the group's stats are counted among those runs only, so no rate can pass 1.
_GROUP: Mapping[str, str] = {
    **{_stat(l, k): f"verifier:{k}" for l in VERIFIER_LABELS for k in VERIFIER_KINDS},
    "contrast_clean": "contrast", "contrast_divergent": "contrast",
    "planner_recommends": "planner", "planner_refiles": "planner",
    "authorised": "decision", "adopted": "decision", "closed_without_adoption": "decision",
    "reverted_14d": "revert",
    "gated_filed": "gated", "gated_undecided": "gated",
    "gated_allowed": "gated_decided", "gated_denied": "gated_decided",
}
_GROUP_WORDS: Mapping[str, str] = {
    "verifier:black-box": "with a final verifier label (black-box)",
    "verifier:in-process": "with a final verifier label (in-process: forgeable by the code under test)",
    "contrast": "with a contrast label",
    "planner": "with a planner label",
    "decision": "with a final decision",
    "revert": "adopted 14 or more days ago and revert-scanned",
    "gated": "runs with a gated call",
    "gated_decided": "runs with a decided gated call",
}
DENOMINATORS: Mapping[str, str] = {stat: _GROUP_WORDS[_GROUP[stat]] for stat in STATS}
# the labels whose presence puts a run in the group's N; the revert group is decided by an adoption's age and scan
_QUALIFIES: Mapping[str, frozenset[str]] = {
    "verifier:black-box": VERIFIER_LABELS, "verifier:in-process": VERIFIER_LABELS,
    "contrast": frozenset({"contrast_clean", "contrast_divergent"}),
    "planner": frozenset({"planner_recommends", "planner_refiles"}),
    "decision": frozenset({"adopted", "closed_without_adoption"}),
    "gated": frozenset({"gated_filed"}),
    "gated_decided": frozenset({"gated_allowed", "gated_denied"}),
}
_DECIDED_GATED = _QUALIFIES["gated_decided"]


def _check_stat(stat: str) -> None:
    if stat in STATS:
        return
    if stat in VERIFIER_LABELS:
        raise DispatchError(f"{stat} is reported per verifier kind: use {' or '.join(_stat(stat, k) for k in VERIFIER_KINDS)}")
    raise DispatchError(f"unknown label {stat!r}")


def _shown(stat: str) -> str:
    base, _, kind = stat.partition(":")
    name = base.replace("_", " ")
    return f"{name} ({kind})" if kind else name


@dataclass(frozen=True)
class Cell:
    """One rollup cell. ``key`` is the path down the hierarchy, 1 to 4 parts: ``(model,)``, then route, harness,
    template. The model part is ``(name, basis)`` with basis ``"answered"`` or ``"requested"``: a model the gateway
    never saw answer is keyed by what was asked for and marked, never merged with an answered one. ``n`` is the
    distinct runs counted; ``counts`` the distinct runs carrying each stat among that stat's denominator runs;
    ``denoms`` each stat's N (``DENOMINATORS`` names what it counts); ``min_n`` the floor below which no rate is
    shown for a stat. ``unscanned`` the adoptions old enough for a revert label that no revert scan has covered:
    they are in no ``reverted_14d`` denominator. The gated stats count RUNS: ``gated_allowed`` and ``gated_denied``
    divide by the runs with a decided gated call and can overlap for a run with mixed calls, so they can sum past 1;
    ``gated_undecided`` counts a run only when it carries no decided gated label."""
    key: tuple
    n: int
    counts: Mapping[str, int]
    min_n: int
    denoms: Mapping[str, int] = field(default_factory=dict)
    unscanned: int = 0

    @property
    def level(self) -> int:
        return len(self.key)

    @property
    def reporting(self) -> bool:
        return self.n >= self.min_n

    @property
    def awaiting(self) -> int:
        """Runs in the cell with no final decision yet (neither adopted nor closed without adoption): in no
        decision denominator."""
        return self.n - self.denominator("adopted")

    def denominator(self, label: str) -> int:
        """``label``'s own N in this cell."""
        _check_stat(label)
        return self.denoms.get(label, 0)

    def reporting_for(self, label: str) -> bool:
        """Whether ``label``'s own N reaches ``min_n``."""
        return self.denominator(label) >= self.min_n

    def rate(self, label: str) -> Fraction | None:
        """``count / N`` over the label's own denominator as an exact fraction, or ``None`` below ``min_n`` (the
        cell then reports only that N)."""
        n = self.denominator(label)
        return Fraction(self.counts.get(label, 0), n) if n and n >= self.min_n else None


def _model_key(row: OutcomeRow) -> tuple[str, str]:
    if row.model_answered != UNKNOWN:
        return (row.model_answered, "answered")
    return (row.model_requested, "requested")


def rollup(rows: Iterable[OutcomeRow], *, min_n: int, now: datetime | None = None) -> dict[tuple, Cell]:
    """Roll ``rows`` up model -> model×route -> model×route×harness -> model×route×harness×template, one
    :class:`Cell` per distinct key at each level. A run's facts come from its first row in ``rows``; the store
    refuses a row that disagrees, so a list read from it never holds two versions of a run. ``now`` (aware; the
    present when omitted) fixes which adoptions are old enough for the ``reverted_14d`` denominator."""
    if type(min_n) is not int or min_n < 1:
        raise DispatchError("min_n must be a positive integer")
    if now is None:
        now = datetime.now(timezone.utc)
    elif not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise DispatchError("rollup needs an aware now")
    cutoff = now - REVERT_WINDOW
    runs: dict[tuple[str, int], tuple[tuple, dict[str, str], list[str | None]]] = {}
    for row in rows:
        entry = runs.get(row.run)
        if entry is None:
            path = (_model_key(row), row.route, row.harness, row.template_id)
            entry = runs[row.run] = (path, {}, [None])
        entry[1].setdefault(row.label, row.at)
        if row.verifier_kind is not None:
            # a run is bucketed by ONE verifier kind; two kinds on one run would put it under whichever came first
            if entry[2][0] not in (None, row.verifier_kind):
                raise DispatchError(f"run {row.run} carries verifier labels of two kinds")
            entry[2][0] = row.verifier_kind
    counts: dict[tuple, dict[str, int]] = {}
    denoms: dict[tuple, dict[str, int]] = {}
    totals: dict[tuple, int] = {}
    unscanned: dict[tuple, int] = {}
    for path, labels, (kind,) in runs.values():
        in_group = {g for g, qual in _QUALIFIES.items() if qual & labels.keys() and g.partition(":")[2] in ("", kind)}
        old_adoption = "adopted" in labels and parse_stamp(labels["adopted"]) <= cutoff
        if old_adoption and "revert_scanned" in labels:
            in_group.add("revert")
        decided = bool(_DECIDED_GATED & labels.keys())
        for depth in range(1, len(_LEVELS) + 1):
            key = path[:depth]
            totals[key] = totals.get(key, 0) + 1
            per, den = counts.setdefault(key, {}), denoms.setdefault(key, {})
            if old_adoption and "revert_scanned" not in labels:
                unscanned[key] = unscanned.get(key, 0) + 1
            for stat in STATS:
                if _GROUP[stat] in in_group:
                    den[stat] = den.get(stat, 0) + 1
                    if stat.partition(":")[0] in labels and not (stat == "gated_undecided" and decided):
                        per[stat] = per.get(stat, 0) + 1
    return {key: Cell(key, totals[key], dict(counts[key]), min_n, dict(denoms[key]), unscanned.get(key, 0))
            for key in totals}


def finest_reporting(cells: Mapping[tuple, Cell], key: tuple, label: str | None = None) -> Cell | None:
    """The finest cell on ``key``'s path (``key`` itself, then each coarser prefix) that has at least ``min_n``
    runs, or ``None`` when even the model's own cell is below it. With ``label`` the test is that label's own N,
    since one cell can report a verifier rate and not an adopted one. This is the coarser line a thin cell falls
    back to."""
    for depth in range(len(key), 0, -1):
        cell = cells.get(key[:depth])
        if cell is not None and (cell.reporting if label is None else cell.reporting_for(label)):
            return cell
    return None


def _head(cell: Cell) -> str:
    name, basis = cell.key[0]
    parts = [name if basis == "answered" else f"{name} (requested; answering model unknown)"]
    if cell.level >= 2:
        parts.append(cell.key[1])
    if cell.level >= 3:
        parts.append(cell.key[2])
    head = ", ".join(parts)
    if cell.level >= 4:
        head += f" `{cell.key[3]}`"
    return head


def cell_line(cell: Cell, label: str = "adopted") -> str:
    """The panel line for ``cell`` and ``label``: ``glm-5.2, R0 `fix-lint`: 41/48 adopted (N=48 with a final
    decision)``, the harness between route and template when the cell is that deep. The N is the label's own
    denominator and is named. A label below ``min_n`` shows only its N. A ``reverted_14d`` line carries the
    survivor-bias caveat on a line of its own."""
    _check_stat(label)
    head = _head(cell)
    n, words = cell.denominator(label), DENOMINATORS[label]
    if cell.reporting_for(label):
        line = f"{head}: {cell.counts.get(label, 0)}/{n} {_shown(label)} (N={n} {words})"
    else:
        line = f"{head}: N={n} {words}, below min_n={cell.min_n}: no rate shown"
    return f"{line}\n{REVERTED_CAVEAT}" if label == "reverted_14d" else line


# --- the panel's data ------------------------------------------------------------------------------------------

BELOW_MIN_N = "below min_n"


@dataclass(frozen=True)
class PanelEntry:
    """One label on one reporting path. ``cell`` is the key of the cell the numbers come from: the leaf's own when
    its N reaches ``min_n``, else the finest coarser cell that does (``fell_back``), else the leaf itself with
    ``rate`` the string ``"below min_n"``. ``count`` and ``n`` are that cell's; ``n`` is the label's own denominator
    (``words`` names it). A fallback entry's ``cell`` is the key of the cell it fell back to, and where no cell
    reaches ``min_n`` the numbers come from the coarsest cell on the path that has any run for the label. ``pending``
    is the runs in that cell with no final decision yet (set on the decision labels only), ``unscanned`` the old
    adoptions no revert scan has covered (set on ``reverted_14d`` only): both are runs outside ``n``."""
    label: str
    count: int
    n: int
    rate: Fraction | str
    words: str
    cell: tuple
    fell_back: bool
    pending: int = 0
    unscanned: int = 0


@dataclass(frozen=True)
class PanelRow:
    """One finest cell (model, route, harness, template) and its labels. ``model_known`` is False for a model the
    gateway never saw answer: such rows are keyed by the model asked for and listed apart, after the answered ones."""
    key: tuple
    model: str
    model_known: bool
    entries: tuple[PanelEntry, ...]


@dataclass(frozen=True)
class Panel:
    """Plain data for a cockpit provider: no markup. ``rows`` answered-model rows first, then the answer-unknown
    group; ``caveats`` the lines a renderer must print with the numbers."""
    min_n: int
    rows: tuple[PanelRow, ...]
    caveats: tuple[str, ...]


def panel(cells: Mapping[tuple, Cell], *, min_n: int) -> Panel:
    """The panel's data from a :func:`rollup`: per finest cell, per label, ``(count, N, rate)`` with the label's own
    N, the rate the string ``"below min_n"`` where no cell on the path reaches it, plus the caveat lines. ``min_n``
    must be the one the rollup used (a mismatch is refused: a panel judging by another floor than its cells were
    built with would print a rate the cell line withholds).

    Rows whose answering model was not recorded are grouped and marked, never merged with an answered model. The
    rows with no ``model_answered`` are the common case, not an error."""
    if type(min_n) is not int or min_n < 1:
        raise DispatchError("min_n must be a positive integer")
    if any(c.min_n != min_n for c in cells.values()):
        raise DispatchError("panel min_n must equal the min_n the cells were rolled up with")
    rows: list[PanelRow] = []
    for key in sorted((k for k in cells if len(k) == len(_LEVELS)), key=repr):
        entries: list[PanelEntry] = []
        for label in STATS:
            cell = finest_reporting(cells, key, label)
            rate: Fraction | str | None = None if cell is None else cell.rate(label)
            if cell is None:
                cell = next((cells[key[:d]] for d in range(1, len(key) + 1)
                             if cells[key[:d]].denominator(label)
                             or (label == "reverted_14d" and cells[key[:d]].unscanned)), None)
                if cell is None:
                    continue                                            # nothing on this path could carry the label
                rate = BELOW_MIN_N
            pending = cell.awaiting if _GROUP[label] == "decision" else 0
            entries.append(PanelEntry(label, cell.counts.get(label, 0), cell.denominator(label), rate,
                                      DENOMINATORS[label], cell.key, cell.key != key, pending,
                                      cell.unscanned if label == "reverted_14d" else 0))
        name, basis = key[0]
        rows.append(PanelRow(key, name, basis == "answered", tuple(entries)))
    rows.sort(key=lambda r: not r.model_known)                          # stable: answered first, then unknown
    caveats: list[str] = [MODEL_QUALITY_CAVEAT]
    if any(e.label == "reverted_14d" for r in rows for e in r.entries):
        caveats.append(REVERTED_CAVEAT)
    if any(not r.model_known for r in rows):
        caveats.append("Rows marked answering-model-unknown are keyed by the model asked for; the model that "
                       "actually answered was not recorded, so they are not merged with answered ones.")
    return Panel(min_n, tuple(rows), tuple(caveats))
