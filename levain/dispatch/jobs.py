"""levain.dispatch.jobs — the dispatch job: a durable state machine whose every transition names its cause.

A planner files a resolved contract (:meth:`JobStore.file`) and gets back the job and, once, a JOB CAPABILITY: a
random token only the filing session holds, of which the store keeps the SHA-256 alone. The operator signs the
contract's digest (:meth:`JobStore.sign`); from there the job is queued for a slot, run, verified, perhaps
RECOMMENDED by the planner (:meth:`JobStore.recommend`, advisory: it confers no authority), authorised by a human
signature or a human-signed adoption rule, and adopted. It fails on what the service itself observes (a lapsed
lease, a budget cap, a floor refusal, a failed verifier) and is cancelled by the filing planner (its capability) at
any state before authorised, or by the human at any non-terminal state. Adopted, failed and cancelled are terminal.

:data:`EDGES` is the one table of allowed transitions: (from, to) -> the causes that may take it. Every transition is
checked against it inside its write transaction, against the current state read in that same transaction, so two
writers can never both move a job from one state. Most causes have no public method yet (runs, the verifier and
adoption are later slices); the table names them all.

Every filed job holds a LEASE: an unsigned contract lapses :data:`UNSIGNED_LEASE` after filing (or at its expiry, if
sooner), a signed one at the expiry its digest binds. Every read and every write first sweeps (:meth:`JobStore.sweep`):
a job past its lease that is not yet authorised moves to ``failed`` with cause ``lease_lapsed``. One live contract per
write file: filing is refused while a live job (not terminal, lease not lapsed) in the same repo writes a file in
common, so an abandoned or runaway planner cannot hold a file past its lease.

The store is ``<directory>/dispatch.db``, ``<levain home>/dispatch`` by default, in a directory that is the
operator's alone (0700, refused otherwise). Its tables are append-only (SQL triggers refuse UPDATE, DELETE and an
INSERT OR REPLACE): a job, its transitions and its one signature are rows written once, and a job's state is its
latest transition. Known edge: until S1 places the control principal (design §8), the store is an operator-uid file,
so any process of that uid can write it; no executor exists in this slice, so no run rests on it. Stdlib only.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from levain.authority import AuthorityError
from levain.autonomic.durable_sqlite import open_durable, write_transaction
from levain.autonomic.durable_sqlite import rollback as _rollback
from levain.cockpit.consent import ConsentError, utc_stamp
from levain.consent.queue import AuthoritySource
from levain.dispatch import ContractRefused, DispatchError
from levain.dispatch import sign as dsign
from levain.dispatch.contract import ResolvedContract, parse_contract

__all__ = [
    "CAUSES", "DB_NAME", "EDGES", "FORMAT", "STATES", "TERMINAL", "UNSIGNED_LEASE", "CapabilityRefused", "Job",
    "JobStore", "JobStoreError", "SignatureRefused", "Transition", "TransitionRefused", "default_dispatch_dir",
]

DB_NAME = "dispatch.db"
FORMAT = "levain-dispatch-jobs/1"
# how long an unsigned contract holds its write files before it lapses
UNSIGNED_LEASE = timedelta(minutes=30)
# how far a signature's signed_at may run ahead of the store's clock (the signer's clock is its own)
_SIGN_SKEW = timedelta(minutes=2)
_ACCESS_BY_OTHERS = 0o077
_SESSION_MAX = 256
_IDEM_MAX = 128
_EVIDENCE_MAX = 4000
_JOB_ID = re.compile(r"dj-[0-9a-f]{32}")

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
    """``<levain home>/dispatch``: ``$LEVAIN_HOME/dispatch`` if set, else ``~/.levain/dispatch``, made absolute (as
    ``levain.consent.queue.default_queue_dir`` does for the consent store)."""
    home = os.environ.get("LEVAIN_HOME")
    return ((Path(home).expanduser() if home else Path.home() / ".levain") / "dispatch").resolve()


def _append_only(table: str, message: str) -> tuple[str, str]:
    # as levain.consent.questions: UPDATE and DELETE refused on the table
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
    # INSERT OR REPLACE deletes the old row without firing a delete trigger: refuse a new row that reuses a key or
    # an explicit rowid (as levain.consent.questions does)
    "CREATE TRIGGER IF NOT EXISTS jobs_no_replace BEFORE INSERT ON jobs "
    "WHEN EXISTS (SELECT 1 FROM jobs WHERE id = NEW.id OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a job is never replaced'); END",
    "CREATE TRIGGER IF NOT EXISTS signatures_no_replace BEFORE INSERT ON signatures "
    "WHEN EXISTS (SELECT 1 FROM signatures WHERE job_id = NEW.job_id OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a signature is never replaced'); END",
    # a transition is the next seq of its job, never a gap and never a replacement
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


def _capability_sha(capability: str) -> str:
    return hashlib.sha256(capability.encode("utf-8")).hexdigest()


def _label(name: str, value: Any, high: int) -> str:
    if not isinstance(value, str) or not value or len(value) > high:
        raise DispatchError(f"{name} must be 1 to {high} characters")
    if any(unicodedata.category(c)[0] == "C" or unicodedata.category(c) in ("Zl", "Zp") for c in value):
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
        # levain.consent.questions' directory rule: the operator's alone, never through a symlink
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
            if row is not None and row[0] != FORMAT:
                raise JobStoreError(f"store format {row[0]!r} is not {FORMAT!r}")
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

    # --- reading, inside a transaction ---------------------------------------------------------------------------
    @staticmethod
    def _load(conn: sqlite3.Connection, job_id: str) -> Job | None:
        row = conn.execute(f"SELECT {_JOB_COLS} FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        history = tuple(Transition(*r) for r in conn.execute(
            "SELECT seq, to_state, cause, at, evidence FROM transitions WHERE job_id = ? ORDER BY seq", (job_id,)))
        signer = conn.execute("SELECT signer_fp FROM signatures WHERE job_id = ?", (job_id,)).fetchone()
        return Job(id=row[0], digest=row[1], contract_text=row[2], filed_at=row[3], filer_session=row[4],
                   idem_key=row[5], repo=row[6], expires_at=row[7], unsigned_until=row[8], history=history,
                   signer_fp=None if signer is None else signer[0])

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
    def sweep(self, now: datetime | None = None) -> list[str]:
        """Fail every lapsed job not yet authorised (cause ``lease_lapsed``); returns their ids."""
        stamp = utc_stamp(_now_utc(self._clock) if now is None else _now_utc(lambda: now))
        with self._write() as conn:
            return self._sweep(conn, stamp)

    def file(self, contract: ResolvedContract, *, filer_session: str,
             idem_key: str | None = None) -> tuple[Job, str | None]:
        """File ``contract`` for the planner session ``filer_session``; returns the job and its capability.

        A repeat with the same ``(filer_session, idem_key)`` and the same digest returns the SAME job and no
        capability: it was returned once, to that session, and the store keeps only its hash, so a replayed call
        never mints a second token for one job. The same key with another digest is refused. Filing is refused while
        another live job in the same repo writes a file in common."""
        if not isinstance(contract, ResolvedContract):
            raise ContractRefused("file takes a ResolvedContract from resolve_contract")
        filer_session = _label("filer_session", filer_session, _SESSION_MAX)
        if idem_key is not None:
            idem_key = _label("idem_key", idem_key, _IDEM_MAX)
        text = contract.canonical()
        parsed = parse_contract(text)      # what is stored must read back as this contract, digest and all
        digest = parsed.digest
        now = _now_utc(self._clock)
        stamp = utc_stamp(now)
        if parsed.expires_at <= stamp:
            raise ContractRefused("the contract has expired; resolve it again")
        unsigned_until = min(utc_stamp(now + UNSIGNED_LEASE), parsed.expires_at)
        with self._write() as conn:
            self._sweep(conn, stamp)
            if idem_key is not None:
                row = conn.execute("SELECT id, digest FROM jobs WHERE filer_session = ? AND idem_key = ?",
                                   (filer_session, idem_key)).fetchone()
                if row is not None:
                    if row[1] != digest:
                        raise ContractRefused(f"idem_key {idem_key!r} already filed job {row[0]} with another "
                                              "contract digest")
                    job = self._load(conn, row[0])
                    assert job is not None
                    return job, None
            mine = parsed.write_paths
            live = conn.execute(
                f"SELECT j.id, j.contract FROM jobs j JOIN ({_CURRENT}) c ON c.job_id = j.id "
                f"WHERE j.repo = ? AND c.to_state NOT IN ({','.join('?' * len(TERMINAL))}) ORDER BY j.id",
                (parsed.repo, *sorted(TERMINAL))).fetchall()
            for other_id, other_text in live:
                common = sorted(mine & parse_contract(other_text).write_paths)
                if common:
                    raise ContractRefused(f"live job {other_id} already writes {common[0]}; one live contract per "
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
            job = self._load(conn, job_id)
        assert job is not None
        return job, capability

    def sign(self, job_id: str, payload: Any, signature: Any, authority: AuthoritySource) -> Job:
        """Store the operator's signature over a filed job's digest and move it to ``signed``, in one write
        transaction; raises :class:`SignatureRefused` saying why when it does not verify. ``authority`` is the
        trusted source (:func:`levain.consent.queue.authority_source`), replayed afresh here, never a snapshot a
        caller captured earlier."""
        if type(authority) is not AuthoritySource:
            raise SignatureRefused("authority must be the AuthoritySource authority_source() returns")
        try:
            snapshot = authority()
        except AuthorityError as exc:
            raise SignatureRefused(f"the authority log could not be read: {exc}") from None
        stamp = utc_stamp(_now_utc(self._clock))
        with self._write() as conn:
            self._sweep(conn, stamp)
            job = self._must_load(conn, job_id)
            if job.state != _FILED:
                raise SignatureRefused(f"job {job.id} is {job.state}, not filed; a contract is signed once")
            problem = dsign.refusal(payload, signature, snapshot, job, ssh_keygen=self._ssh_keygen)
            if problem is None and payload["signed_at"] > utc_stamp(_now_utc(self._clock) + _SIGN_SKEW):
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
        stamp = utc_stamp(_now_utc(self._clock))
        with self._write() as conn:
            self._sweep(conn, stamp)
            job = self._must_load(conn, job_id)
            stored = conn.execute("SELECT capability_sha256 FROM jobs WHERE id = ?", (job.id,)).fetchone()[0]
            given = _capability_sha(capability) if isinstance(capability, str) else ""
            if not hmac.compare_digest(given, stored):
                raise CapabilityRefused(f"that capability is not job {job.id}'s")
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

    def get(self, job_id: str) -> Job | None:
        """The job ``job_id`` after a sweep, or ``None``."""
        stamp = utc_stamp(_now_utc(self._clock))
        with self._write() as conn:
            self._sweep(conn, stamp)
            if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
                return None
            return self._load(conn, job_id)

    def list(self, *, repo: str | None = None) -> list[Job]:
        """Every job after a sweep, oldest filing first; ``repo`` narrows to one repository."""
        stamp = utc_stamp(_now_utc(self._clock))
        with self._write() as conn:
            self._sweep(conn, stamp)
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM jobs WHERE (? IS NULL OR repo = ?) ORDER BY filed_at, id", (repo, repo))]
            return [job for job in (self._load(conn, i) for i in ids) if job is not None]

    def _transition(self, job_id: str, to_state: str, cause: str, *, evidence: str = "") -> Job:
        """Any edge of :data:`EDGES`, for the slices that own its cause (runs, the verifier, adoption) and for the
        tests that walk the table; no public caller exists in this slice."""
        stamp = utc_stamp(_now_utc(self._clock))
        with self._write() as conn:
            self._sweep(conn, stamp)
            job = self._must_load(conn, job_id)
            self._apply(conn, job.id, to_state, cause, stamp, evidence)
            out = self._load(conn, job.id)
        assert out is not None
        return out
