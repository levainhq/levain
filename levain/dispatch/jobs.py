"""levain.dispatch.jobs — the dispatch job: a durable state machine whose every transition names its cause.

A planner files a contract SUBMISSION (:meth:`JobStore.file`); the store resolves it itself, through
:func:`levain.dispatch.contract.resolve_contract` with the template resolver and the provider policy it is handed,
so no caller can file a contract object it built by hand. The planner gets back the job and, once, a JOB
CAPABILITY: a random token only the filing session holds, of which the store keeps the SHA-256 alone. The operator
signs the contract's digest (:meth:`JobStore.sign`); from there the job is queued for a slot, run, verified, perhaps
RECOMMENDED by the planner (:meth:`JobStore.recommend`, advisory: it confers no authority), authorised by a human
signature or a human-signed adoption rule, and adopted. It fails on what the service itself observes (a lapsed
lease, a budget cap, a floor refusal, a failed verifier, a landing branch that no longer reads the base the
verifier ran on) and is cancelled by the filing planner (its capability) at
any state before authorised, or by the human at any non-terminal state. Adopted, failed and cancelled are terminal.

:data:`EDGES` is the one table of allowed transitions: (from, to) -> the causes that may take it. Every transition is
checked against it inside its write transaction, against the current state read in that same transaction, so two
writers can never both move a job from one state. Runs and the verifier have no public method yet (later
slices); authorisation is :meth:`JobStore.authorise`, which :func:`levain.dispatch.adopt.land` calls. The table names
every cause.

Every filed job holds a LEASE: an unsigned contract lapses :data:`UNSIGNED_LEASE` after filing (or at its expiry, if
sooner), a signed one at the expiry its digest binds. Every read and every write first sweeps (:meth:`JobStore.sweep`)
at the store's own clock, read only once the transaction's write lock is held (a lock wait never dates a check in
the past): a job past its lease that is not yet authorised moves to ``failed`` with cause
``lease_lapsed``. The sweep commits in its own write transaction before the operation's, so an operation that is then
refused does not roll the lapse back; the operation's transaction sweeps again, so what it checks is current. One
live contract per write file: filing is refused while a live job (not terminal, lease not lapsed) in the same repo
writes the same place, compared without case (a case-insensitive disk holds ``A.py`` and ``a.py`` as one file): the
same path, or one an ancestor of the other (``src/sub`` and ``src/sub/deep.py`` cannot both land), so an abandoned
or runaway planner cannot hold a file past its lease.

By intent, an AUTHORISED job is never swept, even past its expiry, and so keeps holding its write files: a human
signed for that result, and only adoption, the human cancel (:meth:`JobStore.cancel_by_human`, whose signed item is
kept append-only beside the contract signature) or a moved base releases it: a land that finds its branch no longer at
the base the verifier ran on fails the job (``base_moved``), because the authorised result was judged on that base. The lease protects against a planner that
abandons a job, and an authorised job is no longer the planner's. A human cancel signed before the job's latest
human-signed transition (the contract signature, a later authorisation) is refused: a stale cancel never kills a job
the operator signed for after it.

The store is ``<directory>/dispatch.db``, the consent directory by default (:func:`default_dispatch_dir`), in a
directory that is the operator's alone (0700, refused otherwise). Its tables are append-only (SQL triggers refuse
UPDATE, DELETE and an INSERT OR REPLACE through every key a table has): a job, its transitions, its one signature, its
one human cancel, its one human authorisation, the agent that filed it and the store's own metadata are rows written once, and a job's state is its
latest transition. Every connection the store opens turns foreign keys on. At creation the store draws a random
``store_id``, which every contract signature binds, so a signature made for a job in one store is refused by another.

Signature checks tell a refusal from a fault by class: a signature or cancel that is not the operator's is a
:class:`SignatureRefused`; an authority log that cannot be read (a database or file error) or a verifier that cannot
run (:class:`levain.authority.VerifierUnavailable`) is raised as itself, and nothing is written. An authority log
that is readable but refused (not initialised, its anchor refuses it) is a :class:`SignatureRefused`.

Known edge: until S1 places the control principal (design §8), the store is an operator-uid file, so any process of
that uid can write it; no executor exists in this slice, so no run rests on it. The filing agent is the name the
filing door was given (a flag or the agent's own environment), kept for the signer to read, never proven. Stdlib
only.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from levain import unshowable
from levain.authority import AuthorityError, VerifierUnavailable
from levain.autonomic.durable_sqlite import open_durable, write_transaction
from levain.autonomic.durable_sqlite import rollback as _rollback
from levain.cockpit.consent import ConsentError, parse_stamp, utc_stamp
from levain.consent.queue import AuthoritySource
from levain.dispatch import ContractRefused, DispatchError
from levain.dispatch import sign as dsign
from levain.dispatch.contract import ResolvedContract, ResolvedTemplate, parse_contract, resolve_contract
from levain.dispatch.policy import ProviderPolicy

__all__ = [
    "CAUSES", "DB_NAME", "EDGES", "FORMAT", "STATES", "TERMINAL", "UNSIGNED_LEASE", "CapabilityRefused", "Job",
    "JobStore", "JobStoreError", "SignatureRefused", "Transition", "TransitionRefused", "default_dispatch_dir",
]

DB_NAME = "dispatch.db"
FORMAT = "levain-dispatch-jobs/2"
# the formats a store is migrated from IN PLACE when this code opens it, each by tables and triggers added beside
# the old ones (CREATE ... IF NOT EXISTS) and its marker rewritten in the same transaction; no row is rewritten.
# /2 added the human authorise item and the base_moved edges (F11), so a Levain that knows only /1 refuses the
# store instead of misreading a job it cannot transition
_MIGRATES_FROM = frozenset({"levain-dispatch-jobs/1"})
# how long an unsigned contract holds its write files before it lapses
UNSIGNED_LEASE = timedelta(minutes=30)
# how far a signature's signed_at may run ahead of the store's clock (the signer's clock is its own)
_SIGN_SKEW = timedelta(minutes=2)
# the causes a human signed for: a cancel signed before the latest of these is stale. A rule firing is not one: it is
# dated when it fires, not when the human signed the rule, so it never makes a fresher human cancel look stale
_HUMAN_SIGNED = frozenset({"signature", "human_authorises"})
_AGENT_MAX = 64
_ACCESS_BY_OTHERS = 0o077
_SESSION_MAX = 256
_IDEM_MAX = 128
_EVIDENCE_MAX = 4000
_JOB_ID = re.compile(r"dj-[0-9a-f]{32}")
_STORE_ID = re.compile(r"[0-9a-f]{32}")

STATES = ("filed", "signed", "queued", "running", "verifying", "verified", "recommended", "authorised", "adopted",
          "failed", "cancelled")
TERMINAL = frozenset({"adopted", "failed", "cancelled"})
# the states a lease can lapse in, a service failure can end, and the filing planner can cancel
BEFORE_AUTHORISED = ("filed", "signed", "queued", "running", "verifying", "verified", "recommended")
_SERVICE_FAILURES = frozenset({"lease_lapsed", "budget_cap", "floor_refused"})
_FILED = "filed"


def _edges() -> dict[tuple[str, str], frozenset[str]]:
    edges: dict[tuple[str, str], set[str]] = {
        ("filed", "signed"): {"signature"},
        ("signed", "queued"): {"slot_wait"},
        ("signed", "running"): {"slot_taken"},
        ("queued", "running"): {"slot_taken"},
        ("running", "verifying"): {"runs_exited"},
        ("verifying", "verified"): {"verifier_pass"},
        ("verifying", "failed"): {"verifier_fail"},
        ("verified", "recommended"): {"planner_recommends"},
        ("verified", "authorised"): {"human_authorises", "human_adoption_rule"},
        ("recommended", "authorised"): {"human_authorises", "human_adoption_rule"},
        ("authorised", "adopted"): {"adopted"},
        ("authorised", "cancelled"): {"human_cancel"},
        # the verifier ran on the contract's base; a target that no longer reads it fails the result (F11 §6)
        ("verified", "failed"): {"base_moved"},
        ("recommended", "failed"): {"base_moved"},
        ("authorised", "failed"): {"base_moved"},
    }
    for state in BEFORE_AUTHORISED:
        edges.setdefault((state, "failed"), set()).update(_SERVICE_FAILURES)
        edges.setdefault((state, "cancelled"), set()).update({"planner_cancel", "human_cancel"})
    return {k: frozenset(v) for k, v in edges.items()}


# (from, to) -> the causes allowed to take that edge; nothing leaves a terminal state
EDGES: dict[tuple[str, str], frozenset[str]] = _edges()
CAUSES = frozenset().union(*EDGES.values())


class JobStoreError(DispatchError):
    """The store cannot be trusted: another format, a directory others can reach, one that vanished."""


class TransitionRefused(DispatchError):
    """A transition the edge table does not allow from the job's current state, or a job that does not exist."""


class SignatureRefused(DispatchError):
    """A contract signature that is not the operator's over this job: the message says why."""


class CapabilityRefused(DispatchError):
    """A planner-channel call whose capability is not the one the job was filed with."""


@dataclass(frozen=True)
class Transition:
    seq: int
    to_state: str
    cause: str
    at: str
    evidence: str


@dataclass(frozen=True)
class Job:
    """A job as read: the stored contract and its history; ``state`` is the latest transition's."""
    id: str
    digest: str
    contract_text: str
    filed_at: str
    filer_session: str
    idem_key: str | None
    repo: str
    expires_at: str
    unsigned_until: str
    history: tuple[Transition, ...]
    signer_fp: str | None
    store_id: str
    # the agent the filing door named (``claude_code`` / ``codex``), or None when it named none; never proven
    filer_agent: str | None = None

    @property
    def state(self) -> str:
        return self.history[-1].to_state

    @property
    def contract(self) -> ResolvedContract:
        return parse_contract(self.contract_text)

    @property
    def unsigned_lease_end(self) -> str:
        return self.unsigned_until

    @property
    def lease_end(self) -> str:
        """When the job lapses if it is not authorised by then: the unsigned lease while filed, else the expiry."""
        return self.unsigned_until if self.state == _FILED else self.expires_at


def default_dispatch_dir() -> Path:
    """the consent directory (:func:`levain.consent.queue.default_queue_dir`), beside ``questions.db``: dispatch
    reads no ``$LEVAIN_HOME`` of its own, so the confinement floor that keeps every agent out of that directory
    covers dispatch control state too, with no second name for it to learn."""
    from levain.consent.queue import default_queue_dir

    return default_queue_dir()


def _append_only(table: str, message: str) -> tuple[str, str]:
    # UPDATE and DELETE refused on the table (the consent store's append-only shape, mirrored)
    return (f"CREATE TRIGGER IF NOT EXISTS {table}_frozen BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{message}'); END",
            f"CREATE TRIGGER IF NOT EXISTS {table}_kept BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{message}'); END")


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, digest TEXT NOT NULL, contract TEXT NOT NULL, "
    "filed_at TEXT NOT NULL, filer_session TEXT NOT NULL, capability_sha256 TEXT NOT NULL, idem_key TEXT, "
    "repo TEXT NOT NULL, expires_at TEXT NOT NULL, unsigned_until TEXT NOT NULL, "
    "UNIQUE (filer_session, idem_key))",
    "CREATE INDEX IF NOT EXISTS jobs_repo ON jobs (repo)",
    "CREATE TABLE IF NOT EXISTS transitions (job_id TEXT NOT NULL REFERENCES jobs(id), seq INTEGER NOT NULL, "
    "to_state TEXT NOT NULL, cause TEXT NOT NULL, at TEXT NOT NULL, evidence TEXT NOT NULL, "
    "PRIMARY KEY (job_id, seq))",
    "CREATE TABLE IF NOT EXISTS signatures (job_id TEXT PRIMARY KEY REFERENCES jobs(id), payload TEXT NOT NULL, "
    "signature TEXT NOT NULL, signer_fp TEXT NOT NULL, at TEXT NOT NULL)",
    *_append_only("jobs", "a job is written once"),
    *_append_only("transitions", "transitions are append-only"),
    *_append_only("signatures", "a signature is written once"),
    *_append_only("meta", "store metadata is written once"),
    # the human cancel's signed item, kept beside the contract signature and never in its table: a job has at most
    # one of each, and a cancel is not a signature over the contract
    "CREATE TABLE IF NOT EXISTS cancel_signatures (job_id TEXT PRIMARY KEY REFERENCES jobs(id), payload TEXT NOT NULL, "
    "signature TEXT NOT NULL, signer_fp TEXT NOT NULL, at TEXT NOT NULL)",
    *_append_only("cancel_signatures", "a cancel is written once"),
    # the human authorise item (F11 (A)): the operator's signature over one run's ARTIFACT digest and its landing
    # branch, kept beside the contract signature and the cancel, never in their tables; a job is authorised once
    "CREATE TABLE IF NOT EXISTS authorise_signatures (job_id TEXT PRIMARY KEY REFERENCES jobs(id), "
    "payload TEXT NOT NULL, signature TEXT NOT NULL, signer_fp TEXT NOT NULL, at TEXT NOT NULL)",
    *_append_only("authorise_signatures", "an authorisation is written once"),
    "CREATE TRIGGER IF NOT EXISTS authorise_signatures_no_replace BEFORE INSERT ON authorise_signatures "
    "WHEN EXISTS (SELECT 1 FROM authorise_signatures WHERE job_id = NEW.job_id OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'an authorisation is never replaced'); END",
    # the agent that filed a job, as the filing door named it: beside the jobs table, never a column of it, so a store
    # made before it gains the table and keeps its rows; a job filed with no agent named has no row
    "CREATE TABLE IF NOT EXISTS job_filers (job_id TEXT PRIMARY KEY REFERENCES jobs(id), agent TEXT NOT NULL)",
    *_append_only("job_filers", "a job filer is written once"),
    "CREATE TRIGGER IF NOT EXISTS job_filers_no_replace BEFORE INSERT ON job_filers "
    "WHEN EXISTS (SELECT 1 FROM job_filers WHERE job_id = NEW.job_id OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a job filer is never replaced'); END",
    "CREATE TRIGGER IF NOT EXISTS cancel_signatures_no_replace BEFORE INSERT ON cancel_signatures "
    "WHEN EXISTS (SELECT 1 FROM cancel_signatures WHERE job_id = NEW.job_id OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a cancel is never replaced'); END",
    # INSERT OR REPLACE deletes the old row without firing a delete trigger: refuse a new row that reuses ANY key the
    # table has (its primary key, every UNIQUE, an explicit rowid). jobs: id, (filer_session, idem_key) when the
    # idem_key is not null (a null idem_key never collides under UNIQUE), rowid.
    "CREATE TRIGGER IF NOT EXISTS jobs_no_replace BEFORE INSERT ON jobs "
    "WHEN EXISTS (SELECT 1 FROM jobs WHERE id = NEW.id OR rowid = NEW.rowid OR (NEW.idem_key IS NOT NULL AND "
    "filer_session = NEW.filer_session AND idem_key = NEW.idem_key)) "
    "BEGIN SELECT RAISE(ABORT, 'a job is never replaced'); END",
    "CREATE TRIGGER IF NOT EXISTS meta_no_replace BEFORE INSERT ON meta "
    "WHEN EXISTS (SELECT 1 FROM meta WHERE key = NEW.key OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'store metadata is written once'); END",
    "CREATE TRIGGER IF NOT EXISTS signatures_no_replace BEFORE INSERT ON signatures "
    "WHEN EXISTS (SELECT 1 FROM signatures WHERE job_id = NEW.job_id OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a signature is never replaced'); END",
    # a transition is the next seq of its job, never a gap and never a replacement: an existing (job_id, seq) is at
    # most the max, so it is never max + 1
    "CREATE TRIGGER IF NOT EXISTS transitions_next BEFORE INSERT ON transitions "
    "WHEN NEW.seq != COALESCE((SELECT MAX(seq) FROM transitions WHERE job_id = NEW.job_id), 0) + 1 "
    "OR EXISTS (SELECT 1 FROM transitions WHERE rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a transition is the next seq of its job'); END",
)

_JOB_COLS = "id, digest, contract, filed_at, filer_session, idem_key, repo, expires_at, unsigned_until"
# each job's latest transition
_CURRENT = ("SELECT t.job_id, t.to_state FROM transitions t WHERE t.seq = "
            "(SELECT MAX(seq) FROM transitions l WHERE l.job_id = t.job_id)")


def _now_utc(clock: Callable[[], datetime]) -> datetime:
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ConsentError("the dispatch store's clock returned a naive datetime")
    return now


def _write_clash(mine: Iterable[str], theirs: Iterable[str]) -> tuple[str, str] | None:
    """The first ``(their path, my path)`` pair that names the same place, compared without case: equal, or one an
    ancestor directory of the other (a job creating ``src/sub`` and a job creating ``src/sub/deep.py`` cannot both
    land). ``None`` when the two write sets are disjoint."""
    theirs_l = list(theirs)
    for m in mine:
        fm = m.casefold()
        for t in theirs_l:
            ft = t.casefold()
            if fm == ft or fm.startswith(ft + "/") or ft.startswith(fm + "/"):
                return t, m
    return None


def _relation(mine: str, theirs: str) -> str:
    return "under" if mine.casefold().startswith(theirs.casefold() + "/") else "a directory above"


def _capability_sha(capability: Any) -> str:
    """The SHA-256 of a capability, or ``""`` (which matches no stored hash) for anything that is not text UTF-8
    can encode: a lone surrogate is a mismatch, never a crash."""
    if not isinstance(capability, str):
        return ""
    try:
        return hashlib.sha256(capability.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        return ""


def _replay(authority: AuthoritySource) -> Any:
    """A fresh authority snapshot. A log the replay refuses is the caller's :class:`SignatureRefused`; a verifier that
    cannot run, and a database or file that cannot be read, propagate as the machine faults they are."""
    try:
        return authority()
    except VerifierUnavailable:
        raise                   # an AuthorityError subclass, so it must leave before the refusal clause
    except AuthorityError as exc:
        raise SignatureRefused(f"the authority log is refused: {str(exc)[:200]}") from None


def _label(name: str, value: Any, high: int) -> str:
    if not isinstance(value, str) or not value or len(value) > high:
        raise DispatchError(f"{name} must be 1 to {high} characters")
    if unshowable.first_hidden(value, allow="", selectors=False) is not None:
        raise DispatchError(f"{name} must be one line of plain text")
    return value


class JobStore:
    """The dispatch job store: ``<directory>/dispatch.db``. Every write is one ``BEGIN IMMEDIATE`` through
    :mod:`levain.autonomic.durable_sqlite`; every operation sweeps lapsed leases first."""

    def __init__(self, directory: Path | str | None = None, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 ssh_keygen: Path | str | None = None) -> None:
        self.directory = Path(directory) if directory is not None else default_dispatch_dir()
        self.path = self.directory / DB_NAME
        self._clock = clock
        self._ssh_keygen = ssh_keygen
        self._ready = False

    # --- store ---------------------------------------------------------------------------------------------------
    def _check_directory(self) -> None:
        # the directory is the operator's alone, never through a symlink (the consent store's rule, mirrored)
        if not self.directory.exists():
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)   # the umask can only narrow it
        st = os.lstat(self.directory)
        if not os.path.isdir(self.directory) or os.path.islink(self.directory):
            raise JobStoreError(f"dispatch store directory {self.directory} is not a directory")
        if st.st_uid != os.getuid():
            raise JobStoreError(f"dispatch store directory {self.directory} is not owned by uid {os.getuid()}")
        if st.st_mode & _ACCESS_BY_OTHERS:
            raise JobStoreError(f"dispatch store directory {self.directory} is reachable by group or others "
                                f"(mode {st.st_mode & 0o777:o}); it must be 0700")

    def _connect(self) -> sqlite3.Connection:
        if self._ready:
            if not self.path.exists():
                raise JobStoreError(f"dispatch store {self.path} has disappeared")
        else:
            self._check_directory()
        conn = open_durable(self.path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
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
            if tables and ("meta" not in tables or conn.execute(
                    "SELECT 1 FROM meta WHERE key = 'format'").fetchone() is None):
                raise JobStoreError("the database holds tables but no dispatch-store format marker")
            row = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone() if tables else None
            if row is not None and row[0] != FORMAT and row[0] not in _MIGRATES_FROM:
                raise JobStoreError(f"store format {row[0]!r} is not {FORMAT!r}")
            for stmt in _SCHEMA:
                conn.execute(stmt)
            if row is None:
                conn.execute("INSERT INTO meta (key, value) VALUES ('format', ?)", (FORMAT,))
            elif row[0] != FORMAT:
                # meta is written once by trigger; the migration lifts that one trigger for this one UPDATE and puts
                # it back before COMMIT, so no other connection ever sees meta writable
                conn.execute("DROP TRIGGER meta_frozen")
                cur = conn.execute("UPDATE meta SET value = ? WHERE key = 'format' AND value = ?", (FORMAT, row[0]))
                if cur.rowcount != 1:
                    raise JobStoreError(f"the store's format marker {row[0]!r} could not be moved to {FORMAT!r}")
                conn.execute(_append_only("meta", "store metadata is written once")[0])
            if conn.execute("SELECT 1 FROM meta WHERE key = 'store_id'").fetchone() is None:
                conn.execute("INSERT INTO meta (key, value) VALUES ('store_id', ?)", (secrets.token_hex(16),))
            conn.execute("COMMIT")
        except BaseException:
            _rollback(conn)
            raise

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with write_transaction(self._connect) as conn:
            yield conn

    def _stamp(self) -> str:
        return utc_stamp(_now_utc(self._clock))

    @contextmanager
    def _swept(self) -> Iterator[tuple[sqlite3.Connection, str]]:
        """The sweep committed in its own write transaction, then the operation's write transaction, swept again so
        what it reads is current. Each transaction reads the clock only AFTER its ``BEGIN IMMEDIATE`` holds the
        lock, so a lock wait can never date a check or a transition at a time already past; the operation's stamp is
        the one its sweep used, and is yielded for its transition."""
        with self._write() as conn:
            self._sweep(conn, self._stamp())
        with self._write() as conn:
            stamp = self._stamp()
            self._sweep(conn, stamp)
            yield conn, stamp

    # --- reading, inside a transaction ---------------------------------------------------------------------------
    @staticmethod
    def _store_id(conn: sqlite3.Connection) -> str:
        row = conn.execute("SELECT value FROM meta WHERE key = 'store_id'").fetchone()
        if row is None or not isinstance(row[0], str) or _STORE_ID.fullmatch(row[0]) is None:
            raise JobStoreError("the dispatch store has no valid store_id")
        return row[0]

    @classmethod
    def _load(cls, conn: sqlite3.Connection, job_id: str) -> Job | None:
        row = conn.execute(f"SELECT {_JOB_COLS} FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        history = tuple(Transition(*r) for r in conn.execute(
            "SELECT seq, to_state, cause, at, evidence FROM transitions WHERE job_id = ? ORDER BY seq", (job_id,)))
        signer = conn.execute("SELECT signer_fp FROM signatures WHERE job_id = ?", (job_id,)).fetchone()
        filer = conn.execute("SELECT agent FROM job_filers WHERE job_id = ?", (job_id,)).fetchone()
        return Job(id=row[0], digest=row[1], contract_text=row[2], filed_at=row[3], filer_session=row[4],
                   idem_key=row[5], repo=row[6], expires_at=row[7], unsigned_until=row[8], history=history,
                   signer_fp=None if signer is None else signer[0], store_id=cls._store_id(conn),
                   filer_agent=None if filer is None else filer[0])

    def _must_load(self, conn: sqlite3.Connection, job_id: Any) -> Job:
        job = self._load(conn, job_id) if isinstance(job_id, str) and _JOB_ID.fullmatch(job_id) else None
        if job is None:
            raise TransitionRefused(f"no dispatch job {str(job_id)[:40]!r}")
        return job

    @staticmethod
    def _apply(conn: sqlite3.Connection, job_id: str, to_state: str, cause: str, at: str, evidence: str = "") -> int:
        """Move ``job_id`` to ``to_state`` for ``cause`` if :data:`EDGES` allows it from the state read HERE, in the
        caller's write transaction; returns the new seq."""
        if not isinstance(evidence, str) or len(evidence) > _EVIDENCE_MAX:
            raise TransitionRefused(f"evidence is text of at most {_EVIDENCE_MAX} characters")
        row = conn.execute("SELECT seq, to_state FROM transitions WHERE job_id = ? ORDER BY seq DESC LIMIT 1",
                           (job_id,)).fetchone()
        if row is None:
            raise TransitionRefused(f"no dispatch job {job_id}")
        seq, current = row
        allowed = EDGES.get((current, to_state))
        if allowed is None:
            raise TransitionRefused(f"a job cannot go from {current} to {to_state}")
        if cause not in allowed:
            raise TransitionRefused(f"{cause!r} does not take a job from {current} to {to_state} "
                                    f"(only {sorted(allowed)})")
        conn.execute("INSERT INTO transitions (job_id, seq, to_state, cause, at, evidence) VALUES (?, ?, ?, ?, ?, ?)",
                     (job_id, seq + 1, to_state, cause, at, evidence))
        return seq + 1

    def _sweep(self, conn: sqlite3.Connection, stamp: str) -> list[str]:
        """Fail every job past its lease that is not yet authorised, in the caller's transaction."""
        placeholders = ",".join("?" * len(BEFORE_AUTHORISED))
        rows = conn.execute(
            f"SELECT j.id FROM jobs j JOIN ({_CURRENT}) c ON c.job_id = j.id "
            f"WHERE c.to_state IN ({placeholders}) AND "
            "((c.to_state = 'filed' AND j.unsigned_until <= ?) OR (c.to_state != 'filed' AND j.expires_at <= ?)) "
            "ORDER BY j.id", (*BEFORE_AUTHORISED, stamp, stamp)).fetchall()
        for (job_id,) in rows:
            self._apply(conn, job_id, "failed", "lease_lapsed", stamp, "the lease lapsed before authorisation")
        return [r[0] for r in rows]

    # --- the public operations -----------------------------------------------------------------------------------
    def sweep(self) -> list[str]:
        """Fail every lapsed job not yet authorised (cause ``lease_lapsed``) at the store's own clock; returns their
        ids. It takes no caller time: a time ahead of the clock would fail live jobs, and a failure is final. The
        clock is read once the write lock is held."""
        with self._write() as conn:
            return self._sweep(conn, self._stamp())

    def _idem_row(self, filer_session: str, idem_key: str) -> tuple[str, str, str] | None:
        with self._write() as conn:
            row = conn.execute("SELECT id, digest, filed_at FROM jobs WHERE filer_session = ? AND idem_key = ?",
                               (filer_session, idem_key)).fetchone()
        return None if row is None else (row[0], row[1], row[2])

    def file(self, submission: Any, *, resolve_template: Callable[[str], ResolvedTemplate | None],
             policy: ProviderPolicy, filer_session: str, idem_key: str | None = None,
             filer_agent: str | None = None) -> tuple[Job, str | None]:
        """Resolve the planner's contract ``submission`` (:func:`levain.dispatch.contract.resolve_contract`, with
        ``resolve_template`` and ``policy``, at the store's clock) and file it for the planner session
        ``filer_session``; returns the job and its capability. ``filer_agent`` is the agent the filing door named,
        kept for the signer to read (it proves nothing). Every refusal resolution makes is raised here, and nothing
        is filed.

        A repeat with the same ``(filer_session, idem_key)`` and the same digest returns the SAME job and no
        capability: it was returned once, to that session, and the store keeps only its hash, so a replayed call
        never mints a second token for one job. The key is looked up BEFORE the expiry is judged: a replay is
        resolved at its job's own filing time, so a replay after the contract expired still returns that job (in
        whatever state it is now). The same key with another digest is refused. Filing is refused while another live
        job in the same repo writes the same path or an ancestor or descendant of one, compared without case."""
        filer_session = _label("filer_session", filer_session, _SESSION_MAX)
        if filer_agent is not None:
            filer_agent = _label("filer_agent", filer_agent, _AGENT_MAX)
        if idem_key is not None:
            idem_key = _label("idem_key", idem_key, _IDEM_MAX)
        now = _now_utc(self._clock)
        prior = self._idem_row(filer_session, idem_key) if idem_key is not None else None
        when = now if prior is None else parse_stamp(prior[2])
        resolved = resolve_contract(submission, resolve_template=resolve_template, policy=policy, now=when)
        text = resolved.canonical()
        parsed = parse_contract(text)      # what is stored must read back as this contract, digest and all
        digest = parsed.digest
        with self._swept() as (conn, stamp):
            if idem_key is not None:
                row = conn.execute("SELECT id, digest FROM jobs WHERE filer_session = ? AND idem_key = ?",
                                   (filer_session, idem_key)).fetchone()
                if row is not None:
                    if row[1] != digest:
                        raise ContractRefused(
                            f"idem_key {idem_key!r} already filed job {row[0]} with another contract digest; a "
                            "replay must resend the IDENTICAL submission (the same expires_at included), and a "
                            "lost capability cannot be re-issued")
                    job = self._load(conn, row[0])
                    assert job is not None
                    return job, None
                if prior is not None:
                    # the row read before resolving is gone: the store is append-only, so this is not a replay
                    raise JobStoreError(f"job {prior[0]} vanished from an append-only store")
            if parsed.expires_at <= stamp:
                raise ContractRefused("the contract has expired; resolve it again")
            # the lease runs from the filing stamp (read under the lock), never from the clock read before resolving
            unsigned_until = min(utc_stamp(parse_stamp(stamp) + UNSIGNED_LEASE), parsed.expires_at)
            mine = sorted(parsed.write_paths)
            live = conn.execute(
                f"SELECT j.id, j.contract FROM jobs j JOIN ({_CURRENT}) c ON c.job_id = j.id "
                f"WHERE j.repo = ? AND c.to_state NOT IN ({','.join('?' * len(TERMINAL))}) ORDER BY j.id",
                (parsed.repo, *sorted(TERMINAL))).fetchall()
            for other_id, other_text in live:
                try:
                    other = parse_contract(other_text)
                except DispatchError:
                    # a stored contract is written canonical and never changed: one that no longer parses is a
                    # damaged store, not a refusal of the submission in hand
                    raise JobStoreError(f"live job {other_id} holds a stored contract that no longer parses; the "
                                        "dispatch store is damaged") from None
                clash = _write_clash(mine, sorted(other.write_paths))
                if clash is not None:
                    theirs_p, mine_p = clash
                    if theirs_p.casefold() == mine_p.casefold():
                        raise ContractRefused(f"live job {other_id} already writes {theirs_p}; one live contract "
                                              "per write file")
                    raise ContractRefused(f"live job {other_id} already writes {theirs_p}, and this contract's "
                                          f"{mine_p} is {_relation(mine_p, theirs_p)} it; one live contract per "
                                          "write file")
            job_id = "dj-" + secrets.token_hex(16)
            capability = secrets.token_urlsafe(32)
            conn.execute(f"INSERT INTO jobs ({_JOB_COLS}, capability_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (job_id, digest, text, stamp, filer_session, idem_key, parsed.repo, parsed.expires_at,
                          unsigned_until, _capability_sha(capability)))
            # a job's first transition is written here alone: no edge leads INTO filed
            conn.execute("INSERT INTO transitions (job_id, seq, to_state, cause, at, evidence) "
                         "VALUES (?, 1, 'filed', 'filed', ?, ?)",
                         (job_id, stamp, json.dumps({"filer_session": filer_session}, sort_keys=True)))
            if filer_agent is not None:
                conn.execute("INSERT INTO job_filers (job_id, agent) VALUES (?, ?)", (job_id, filer_agent))
            job = self._load(conn, job_id)
        assert job is not None
        return job, capability

    def sign(self, job_id: str, payload: Any, signature: Any, authority: AuthoritySource) -> Job:
        """Store the operator's signature over a filed job's digest and move it to ``signed``, in one write
        transaction; raises :class:`SignatureRefused` saying why when it does not verify. ``authority`` is the
        trusted source (:func:`levain.consent.queue.authority_source`), replayed afresh INSIDE the write
        transaction, just before the check, so a key revoked while the call waited for the lock is seen as revoked;
        never a snapshot a caller captured earlier. An authority log the replay refuses (not initialised, its anchor
        refuses it) is a :class:`SignatureRefused`; one that cannot be read (a database or file error, a malformed
        value) and a verifier that cannot run (:class:`levain.authority.VerifierUnavailable`) are raised as
        themselves, machine faults, and nothing is written."""
        if type(authority) is not AuthoritySource:
            raise SignatureRefused("authority must be the AuthoritySource authority_source() returns")
        with self._swept() as (conn, stamp):
            job = self._must_load(conn, job_id)
            if job.state != _FILED:
                raise SignatureRefused(f"job {job.id} is {job.state}; only a filed job can be signed")
            snapshot = _replay(authority)
            problem = dsign.refusal(payload, signature, snapshot, job, ssh_keygen=self._ssh_keygen)
            if problem is None and payload["signed_at"] > utc_stamp(parse_stamp(stamp) + _SIGN_SKEW):
                problem = "the signature is dated in the future"
            if problem is not None:
                raise SignatureRefused(problem)
            conn.execute("INSERT INTO signatures (job_id, payload, signature, signer_fp, at) VALUES (?, ?, ?, ?, ?)",
                         (job.id, dsign.sign_message(payload).decode("utf-8"), signature, payload["signer_fp"],
                          stamp))
            self._apply(conn, job.id, "signed", "signature", stamp,
                        json.dumps({"signer_fp": payload["signer_fp"]}, sort_keys=True))
            out = self._load(conn, job.id)
        assert out is not None
        return out

    def _planner(self, job_id: Any, capability: Any, to_state: str, cause: str) -> Job:
        with self._swept() as (conn, stamp):
            job = self._must_load(conn, job_id)
            stored = conn.execute("SELECT capability_sha256 FROM jobs WHERE id = ?", (job.id,)).fetchone()[0]
            if not hmac.compare_digest(_capability_sha(capability), stored):
                raise CapabilityRefused(f"that capability is not job {job.id}'s")
            if to_state == "recommended" and job.state != "verified":
                raise TransitionRefused(f"recommend is only for a verified job (job is {job.state})")
            self._apply(conn, job.id, to_state, cause, stamp)
            out = self._load(conn, job.id)
        assert out is not None
        return out

    def recommend(self, job_id: str, capability: str) -> Job:
        """The planner's RECOMMENDED label on a verified job, on the planner channel (its capability). Advisory: it
        confers no authority; only a human signature or a human-signed adoption rule authorises a result."""
        return self._planner(job_id, capability, "recommended", "planner_recommends")

    def cancel_by_planner(self, job_id: str, capability: str) -> Job:
        """The filing planner cancels its job (its capability), at any state before authorised: cancelling only
        removes authority, so the planner may always do it before a human has authorised."""
        return self._planner(job_id, capability, "cancelled", "planner_cancel")

    def cancel_by_human(self, job_id: str, payload: Any, signature: Any, authority: AuthoritySource) -> Job:
        """The operator's signed cancel (:func:`levain.dispatch.sign.cancel_payload`) of a job in any non-terminal
        state, authorised included: store the signed item and move the job to ``cancelled`` (cause
        ``human_cancel``) in one write transaction. ``authority`` is replayed afresh inside that transaction, as
        :meth:`sign` replays it. A terminal job is a :class:`TransitionRefused`; a cancel that is not the operator's,
        or one signed before the job's latest human-signed transition (a stale cancel), is a
        :class:`SignatureRefused` saying why; an authority log that cannot be read and a verifier that cannot run
        are raised as themselves, as :meth:`sign` raises them. Cancelled is terminal, so an authorised job cancelled
        here stops holding its write files."""
        if type(authority) is not AuthoritySource:
            raise SignatureRefused("authority must be the AuthoritySource authority_source() returns")
        with self._swept() as (conn, stamp):
            job = self._must_load(conn, job_id)
            if job.state in TERMINAL:
                raise TransitionRefused(f"job {job.id} is {job.state}, which is final; only a live job can be "
                                        "cancelled")
            snapshot = _replay(authority)
            problem = dsign.cancel_refusal(payload, signature, snapshot, job, ssh_keygen=self._ssh_keygen)
            if problem is None and payload["signed_at"] > utc_stamp(parse_stamp(stamp) + _SIGN_SKEW):
                problem = "the cancel is dated in the future"
            if problem is None:
                last = max((h for h in job.history if h.cause in _HUMAN_SIGNED), key=lambda h: h.seq, default=None)
                if last is not None and payload["signed_at"] < last.at:
                    problem = (f"the cancel was signed at {payload['signed_at']}, before the job's {last.cause} at "
                               f"{last.at}: a stale cancel never undoes what the operator signed after it; sign a "
                               "new cancel")
            if problem is not None:
                raise SignatureRefused(problem)
            conn.execute("INSERT INTO cancel_signatures (job_id, payload, signature, signer_fp, at) "
                         "VALUES (?, ?, ?, ?, ?)",
                         (job.id, dsign.sign_message(payload).decode("utf-8"), signature, payload["signer_fp"],
                          stamp))
            self._apply(conn, job.id, "cancelled", "human_cancel", stamp,
                        json.dumps({"signer_fp": payload["signer_fp"], "reason": payload["reason"]},
                                   sort_keys=True))
            out = self._load(conn, job.id)
        assert out is not None
        return out

    def authorise(self, job_id: str, *, cause: str, evidence: str,
                  human: tuple[Any, Any, AuthoritySource, Callable[..., str | None]] | None = None) -> Job:
        """``verified``/``recommended`` -> ``authorised`` for ``cause`` with the INTENT ``evidence`` (F11 §6 step 1),
        in one write transaction. ``human`` is ``(payload, signature, authority, refusal)`` for the human authorise
        item: ``authority`` is replayed afresh inside the transaction, ``refusal(payload, signature, snapshot, job,
        ssh_keygen=...)`` judges it (``levain.dispatch.adopt.authorise_refusal``), and the signed item is stored in
        ``authorise_signatures`` with the transition; refusals and machine faults are told apart as :meth:`sign`
        tells them. Without ``human`` the cause must not be ``human_authorises``."""
        if (cause == "human_authorises") != (human is not None):
            raise TransitionRefused("human_authorises is written with the human's signed item, and only it")
        with self._swept() as (conn, stamp):
            job = self._must_load(conn, job_id)
            if job.state not in ("verified", "recommended"):
                raise TransitionRefused(f"job {job.id} is {job.state}; only a verified or recommended job is "
                                        "authorised")
            if human is not None:
                payload, signature, authority, refusal = human
                if type(authority) is not AuthoritySource:
                    raise SignatureRefused("authority must be the AuthoritySource authority_source() returns")
                snapshot = _replay(authority)
                problem = refusal(payload, signature, snapshot, job, ssh_keygen=self._ssh_keygen)
                if problem is None and payload["signed_at"] > utc_stamp(parse_stamp(stamp) + _SIGN_SKEW):
                    problem = "the authorisation is dated in the future"
                if problem is not None:
                    raise SignatureRefused(problem)
                conn.execute("INSERT INTO authorise_signatures (job_id, payload, signature, signer_fp, at) "
                             "VALUES (?, ?, ?, ?, ?)",
                             (job.id, dsign.sign_message(payload).decode("utf-8"), signature, payload["signer_fp"],
                              stamp))
            self._apply(conn, job.id, "authorised", cause, stamp, evidence)
            out = self._load(conn, job.id)
        assert out is not None
        return out

    def get(self, job_id: str) -> Job | None:
        """The job ``job_id`` after a sweep, or ``None``."""
        with self._swept() as (conn, _stamp):
            if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
                return None
            return self._load(conn, job_id)

    def list(self, *, repo: str | None = None) -> list[Job]:
        """Every job after a sweep, oldest filing first; ``repo`` narrows to one repository."""
        with self._swept() as (conn, _stamp):
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM jobs WHERE (? IS NULL OR repo = ?) ORDER BY filed_at, id", (repo, repo))]
            return [job for job in (self._load(conn, i) for i in ids) if job is not None]

    def _transition(self, job_id: str, to_state: str, cause: str, *, evidence: str = "") -> Job:
        """Any edge of :data:`EDGES`, for the slices that own its cause (runs, the verifier, adoption) and for the
        tests that walk the table; no public caller exists in this slice."""
        with self._swept() as (conn, stamp):
            job = self._must_load(conn, job_id)
            self._apply(conn, job.id, to_state, cause, stamp, evidence)
            out = self._load(conn, job.id)
        assert out is not None
        return out
