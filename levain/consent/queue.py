"""levain.consent.queue — the consent queue: external agents' gates, their answers, their receipts.

A hook files an :class:`~levain.consent.record.ExternalGateRequest`; the request is ``pending`` until exactly
one of four things ends it:

  - ``allowed`` / ``denied``: an answer that VERIFIES (:meth:`ConsentQueue.answer`): a record binding this
    queue, the request's id, its digest, its expiry and the decision, signed in
    :data:`~levain.consent.record.ANSWER_NAMESPACE` by a key the replayed authority log holds unrevoked, of a
    class that covers the request's tier for an allow. A delivered "yes" that does not verify is no
    decision, never an allow (the 10-08 rule): the request stays pending;
  - ``expired``: its ``expires_at`` passed with no verified answer. Silence is NO decision: the hook outputs
    nothing and the agent's native dialog stands;
  - ``withdrawn``: the requester gave up (the human answered the native dialog, the hook stopped holding).

A terminal request never changes again: SQL triggers refuse any update of a terminal row, any change of a
request's fields, and any delete, so the rule does not rest on this module's discipline. Every terminal
transition writes its receipt (built by :mod:`levain.autonomic.receipt`) in the same transaction, so a
transition and its receipt can never disagree.

The store is ``<levain home>/consent/queue.db``, opened through :mod:`levain.autonomic.durable_sqlite` (WAL,
synchronous FULL); every write is one ``BEGIN IMMEDIATE``, serialized by SQLite. The directory is the
operator's alone (0700, refused otherwise).

Policy answers (standing grants, criteria) are Q4: :func:`decide_by_policy` is their seam and decides nothing.

Stdlib only.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from levain.authority import AuthoritySnapshot, VerifierUnavailable, verify_link_sig
from levain.autonomic.authority import AuthorityScope
from levain.autonomic.durable_sqlite import open_durable, read_transaction, write_transaction
from levain.autonomic.durable_sqlite import rollback as _rollback
from levain.autonomic.receipt import build_action_face, build_gate_face, build_gate_verdict
from levain.cockpit.consent import ConsentError, utc_stamp
from levain.cockpit.firejournal import _CLASS_RANK, _TIER_NEEDS
from levain.consent.record import (
    ANSWER_FORMAT, ANSWER_NAMESPACE, DECISIONS, ExternalGateRequest, answer_message, redact_display,
    tool_input_digest,
)

__all__ = ["DB_NAME", "STATES", "STORE_FORMAT", "TERMINAL", "AnswerRefused", "ConsentQueue", "Decision",
           "QueueStoreError", "decide_by_policy", "default_queue_dir"]

DB_NAME = "queue.db"
STORE_FORMAT = "levain-consent-queue/1"
STATES: tuple[str, ...] = ("pending", "allowed", "denied", "expired", "withdrawn")
TERMINAL: frozenset[str] = frozenset(STATES[1:])
_ANSWER_KEYS = frozenset({"format", "queue_id", "request_id", "request_digest", "decision", "expires_at",
                          "signer_fp"})
_FIELDS = ("id", "agent", "agent_session_id", "tool_name", "tool_input_digest", "display", "cwd", "tier",
           "received_at", "expires_at")
_ACCESS_BY_OTHERS = 0o077

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, agent TEXT NOT NULL, agent_session_id TEXT NOT NULL, "
    "tool_name TEXT NOT NULL, tool_input_digest TEXT NOT NULL, display TEXT NOT NULL, cwd TEXT NOT NULL, "
    "tier TEXT NOT NULL, received_at TEXT NOT NULL, expires_at TEXT NOT NULL, "
    "state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','allowed','denied','expired','withdrawn')), "
    "decided_at TEXT, answer TEXT, signature TEXT, signer_fp TEXT, note TEXT)",
    "CREATE INDEX IF NOT EXISTS requests_pending ON requests (state, expires_at)",
    "CREATE TABLE IF NOT EXISTS receipts (seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL UNIQUE "
    "REFERENCES requests(id), state TEXT NOT NULL, created_at TEXT NOT NULL, face TEXT NOT NULL)",
    # terminal is terminal; a request's fields never change; nothing is deleted; receipts are append-only
    "CREATE TRIGGER IF NOT EXISTS requests_frozen BEFORE UPDATE ON requests WHEN OLD.state != 'pending' "
    "OR NEW.state = 'pending' OR NEW.id IS NOT OLD.id OR NEW.agent IS NOT OLD.agent "
    "OR NEW.agent_session_id IS NOT OLD.agent_session_id OR NEW.tool_name IS NOT OLD.tool_name "
    "OR NEW.tool_input_digest IS NOT OLD.tool_input_digest OR NEW.display IS NOT OLD.display "
    "OR NEW.cwd IS NOT OLD.cwd OR NEW.tier IS NOT OLD.tier OR NEW.received_at IS NOT OLD.received_at "
    "OR NEW.expires_at IS NOT OLD.expires_at "
    "BEGIN SELECT RAISE(ABORT, 'a consent request changes only once, from pending to a terminal state'); END",
    "CREATE TRIGGER IF NOT EXISTS requests_kept BEFORE DELETE ON requests "
    "BEGIN SELECT RAISE(ABORT, 'consent requests are never deleted'); END",
    "CREATE TRIGGER IF NOT EXISTS receipts_frozen BEFORE UPDATE ON receipts "
    "BEGIN SELECT RAISE(ABORT, 'receipts are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS receipts_kept BEFORE DELETE ON receipts "
    "BEGIN SELECT RAISE(ABORT, 'receipts are append-only'); END",
)


class QueueStoreError(RuntimeError):
    """The store cannot be trusted: another format, a directory others can reach, one that vanished."""


class AnswerRefused(Exception):
    """An answer that is not authority. The request is unchanged (or, for a late answer, now expired)."""


@dataclass(frozen=True)
class Decision:
    """A verified answer: what the hook delivers. ``decision`` is ``allow`` or ``deny``."""
    request_id: str
    decision: str
    signer_fp: str
    decided_at: str


def default_queue_dir() -> Path:
    """``<levain home>/consent``: ``$LEVAIN_HOME/consent`` if set, else ``~/.levain/consent``, made absolute
    (as ``levain.autonomic.db.default_store_dir`` does for the autonomic store)."""
    home = os.environ.get("LEVAIN_HOME")
    return ((Path(home).expanduser() if home else Path.home() / ".levain") / "consent").resolve()


def decide_by_policy(request: ExternalGateRequest) -> Decision | None:
    """The seam for Q4's policy and criteria (signed standing grants, tool identity, workspace). Decides
    nothing yet: every request goes to a signed answer or to the human."""
    return None


def _now_utc(clock: Callable[[], datetime]) -> datetime:
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ConsentError("the queue's clock returned a naive datetime")
    return now


class ConsentQueue:
    """The queue in ``directory`` (default :func:`default_queue_dir`). Every operation opens its own
    connection, so one object is safe across threads and several processes may share the store."""

    def __init__(self, directory: Path | str | None = None, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 ssh_keygen: Path | str | None = None) -> None:
        self.directory = Path(directory) if directory is not None else default_queue_dir()
        self.path = self.directory / DB_NAME
        self._clock = clock
        self._ssh_keygen = ssh_keygen
        self._ready = False

    # --- store ---------------------------------------------------------------------------------------
    def _check_directory(self) -> None:
        if not self.directory.exists():
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)   # the umask can only narrow it
        st = os.lstat(self.directory)
        if not os.path.isdir(self.directory) or os.path.islink(self.directory):
            raise QueueStoreError(f"consent queue directory {self.directory} is not a directory")
        if st.st_uid != os.getuid():
            raise QueueStoreError(f"consent queue directory {self.directory} is not owned by uid {os.getuid()}")
        if st.st_mode & _ACCESS_BY_OTHERS:
            raise QueueStoreError(f"consent queue directory {self.directory} is reachable by group or others "
                                  f"(mode {st.st_mode & 0o777:o}); it must be 0700")

    def _connect(self) -> sqlite3.Connection:
        if self._ready:
            if not self.path.exists():
                raise QueueStoreError(f"consent queue {self.path} has disappeared")
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
                raise QueueStoreError("the database holds tables but no consent-queue format marker")
            for stmt in _SCHEMA:
                conn.execute(stmt)
            row = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
            if row is None:
                conn.execute("INSERT INTO meta (key, value) VALUES ('format', ?)", (STORE_FORMAT,))
            elif row[0] != STORE_FORMAT:
                raise QueueStoreError(f"store format {row[0]!r} is not {STORE_FORMAT!r}")
            conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('queue_id', ?)",
                         ("cq-" + os.urandom(16).hex(),))
            conn.execute("COMMIT")
        except BaseException:
            _rollback(conn)
            raise

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with write_transaction(self._connect) as conn:
            yield conn

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with read_transaction(self._connect) as conn:
            yield conn

    @property
    def queue_id(self) -> str:
        with self._read() as conn:
            return conn.execute("SELECT value FROM meta WHERE key = 'queue_id'").fetchone()[0]

    # --- filing --------------------------------------------------------------------------------------
    def file(self, *, agent: str, agent_session_id: str, tool_name: str, tool_input: Any, cwd: str,
             hold: timedelta, tier: str = "T2") -> ExternalGateRequest:
        """File one gate an agent is waiting on; it expires ``hold`` after now. ``tier`` must be one an
        allow can be signed at (``T2`` or ``T3``)."""
        if tier not in _TIER_NEEDS:
            raise ConsentError(f"tier must be one of {tuple(_TIER_NEEDS)}: {tier!r}")
        if not isinstance(hold, timedelta) or hold <= timedelta(0):
            raise ConsentError("hold must be a positive timedelta")
        now = _now_utc(self._clock)
        request = ExternalGateRequest.create(
            agent=agent, agent_session_id=agent_session_id, tool_name=tool_name,
            tool_input_digest=tool_input_digest(tool_input), display=redact_display(tool_name, tool_input),
            cwd=cwd, tier=tier, received_at=utc_stamp(now), expires_at=utc_stamp(now + hold))
        with self._write() as conn:
            conn.execute(f"INSERT OR IGNORE INTO requests ({', '.join(_FIELDS)}) VALUES "
                         f"({', '.join('?' for _ in _FIELDS)})", tuple(request.fields()[f] for f in _FIELDS))
        return request

    # --- reading -------------------------------------------------------------------------------------
    @staticmethod
    def _request_of(row: Mapping[str, Any]) -> ExternalGateRequest | None:
        request = ExternalGateRequest(**{f: row[f] for f in _FIELDS})
        return request if request.seal_matches() else None

    def _row(self, conn: sqlite3.Connection, request_id: str) -> dict[str, Any] | None:
        cur = conn.execute(f"SELECT {', '.join(_FIELDS)}, state, decided_at, answer, signer_fp FROM requests "
                           "WHERE id = ?", (request_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip([d[0] for d in cur.description], row))

    def get(self, request_id: str) -> tuple[ExternalGateRequest, str] | None:
        """The request and its state, after expiring it if its time is up. ``None`` if unknown or its seal
        does not match (a row someone edited is never answered)."""
        self._expire_one(request_id)
        with self._read() as conn:
            row = self._row(conn, request_id)
        if row is None:
            return None
        request = self._request_of(row)
        return None if request is None else (request, row["state"])

    def pending(self) -> list[ExternalGateRequest]:
        """The open requests, oldest first, after a sweep (sealed rows only)."""
        self.sweep()
        with self._read() as conn:
            cur = conn.execute(f"SELECT {', '.join(_FIELDS)} FROM requests WHERE state = 'pending' "
                               "ORDER BY received_at, id")
            names = [d[0] for d in cur.description]
            rows = [dict(zip(names, r)) for r in cur.fetchall()]
        return [r for r in (self._request_of(row) for row in rows) if r is not None]

    def receipt(self, request_id: str) -> dict[str, Any] | None:
        with self._read() as conn:
            row = conn.execute("SELECT state, created_at, face FROM receipts WHERE request_id = ?",
                               (request_id,)).fetchone()
        return None if row is None else {"state": row[0], "created_at": row[1], "face": json.loads(row[2])}

    # --- transitions ---------------------------------------------------------------------------------
    @staticmethod
    def _face(request: ExternalGateRequest, state: str, signer_fp: str | None) -> dict[str, Any]:
        common: dict[str, Any] = dict(context_id=request.id, query_text=request.display, query_date=request.received_at[:10],
                      producers=[request.agent])
        if state in ("allowed", "denied"):
            return build_gate_face(
                **common,
                gate=build_gate_verdict(verdict="approved" if state == "allowed" else "denied", by="human"),
                authority=AuthorityScope(grantor="human", grant=f"signed-answer:{signer_fp}"),
                terminal_state="fired" if state == "allowed" else "refused", downstream_id=request.id)
        face = build_action_face(**common)          # no decision: the gate slice stays null
        if state == "expired":
            face["terminal_state"] = "timed_out"
        return face

    def _transition(self, conn: sqlite3.Connection, request: ExternalGateRequest, state: str, *, now: datetime,
                    answer: str | None = None, signature: str | None = None, signer_fp: str | None = None,
                    note: str | None = None, before_expiry: bool = False) -> bool:
        """One ``pending -> state`` move plus its receipt, inside the caller's write transaction. False when
        the request is no longer pending (or, with ``before_expiry``, its time is up): nothing is written."""
        stamp = utc_stamp(now)
        sql = ("UPDATE requests SET state = ?, decided_at = ?, answer = ?, signature = ?, signer_fp = ?, note = ? "
               "WHERE id = ? AND state = 'pending'")
        args: tuple[Any, ...] = (state, stamp, answer, signature, signer_fp, note, request.id)
        if before_expiry:
            sql += " AND expires_at > ?"
            args += (stamp,)
        if conn.execute(sql, args).rowcount != 1:
            return False
        conn.execute("INSERT INTO receipts (request_id, state, created_at, face) VALUES (?, ?, ?, ?)",
                     (request.id, state, stamp, json.dumps(self._face(request, state, signer_fp),
                                                           ensure_ascii=False, sort_keys=True)))
        return True

    def _expire_one(self, request_id: str) -> None:
        now = _now_utc(self._clock)
        stamp = utc_stamp(now)
        with self._write() as conn:
            row = self._row(conn, request_id)
            if row is None or row["state"] != "pending" or row["expires_at"] > stamp:
                return
            request = self._request_of(row)
            if request is not None:
                self._transition(conn, request, "expired", now=now)

    def sweep(self) -> int:
        """Expire every pending request whose time is up; returns how many."""
        now = _now_utc(self._clock)
        stamp = utc_stamp(now)
        done = 0
        with self._write() as conn:
            cur = conn.execute(f"SELECT {', '.join(_FIELDS)} FROM requests WHERE state = 'pending' "
                               "AND expires_at <= ?", (stamp,))
            names = [d[0] for d in cur.description]
            for raw in cur.fetchall():
                request = self._request_of(dict(zip(names, raw)))
                if request is not None and self._transition(conn, request, "expired", now=now):
                    done += 1
        return done

    def withdraw(self, request_id: str, *, note: str = "") -> bool:
        """The requester stops waiting (the human answered the native dialog; the hook gave up). Needs no
        signature: it decides nothing, it leaves the agent's own dialog. False if the request is not pending."""
        now = _now_utc(self._clock)
        with self._write() as conn:
            row = self._row(conn, request_id)
            request = None if row is None else self._request_of(row)
            if request is None:
                return False
            return self._transition(conn, request, "withdrawn", now=now, note=note or None)

    # --- answers -------------------------------------------------------------------------------------
    def answer(self, request_id: str, payload: Mapping[str, Any], signature: str, *,
               authority: AuthoritySnapshot) -> Decision:
        """Apply a signed answer to ``request_id``, or raise :class:`AnswerRefused` (the request stays as it
        was, or becomes ``expired`` when the answer came too late). ``authority`` must be a verified replay
        of the authority log (:func:`levain.authority.snapshot`)."""
        got = self.get(request_id)                         # expires it first if its time is up
        if got is None:
            raise AnswerRefused(f"no sealed request {request_id!r}")
        request, state = got
        if state != "pending":
            raise AnswerRefused(f"request {request_id} is already {state}")
        if not isinstance(payload, Mapping) or set(payload) != _ANSWER_KEYS:
            raise AnswerRefused("the answer is not an answer record")
        if payload["format"] != ANSWER_FORMAT:
            raise AnswerRefused(f"answer format {payload['format']!r} is not {ANSWER_FORMAT!r}")
        if payload["queue_id"] != self.queue_id:
            raise AnswerRefused("the answer names another queue")
        if payload["request_id"] != request.id:
            raise AnswerRefused("the answer is for another request")
        if payload["request_digest"] != request.digest:
            raise AnswerRefused("the answer signs another version of this request")
        if payload["expires_at"] != request.expires_at:
            raise AnswerRefused("the answer names another expiry")
        decision = payload["decision"]
        if decision not in DECISIONS:
            raise AnswerRefused(f"decision {decision!r} is not one of {DECISIONS}")
        signer_fp = payload["signer_fp"]
        key = authority.key(signer_fp) if isinstance(signer_fp, str) else None
        if key is None:
            raise AnswerRefused("the signer is not an enrolled key")
        if not key.active:
            raise AnswerRefused("the signer's key is revoked")
        if decision == "allow":
            need, have = _TIER_NEEDS[request.tier], _CLASS_RANK.get(key.key_class)
            if have is None or have < need:
                raise AnswerRefused(f"key class {key.key_class} does not cover {request.tier}")
        try:
            message = answer_message(dict(payload))
        except ConsentError as exc:
            raise AnswerRefused(f"the answer cannot be encoded: {exc}") from None
        kwargs: dict[str, Any] = {} if self._ssh_keygen is None else {"ssh_keygen": self._ssh_keygen}
        try:
            ok = verify_link_sig(message, signature, public_key=key.public_key, fingerprint=key.fingerprint,
                                 namespace=ANSWER_NAMESPACE, **kwargs)
        except VerifierUnavailable as exc:
            raise AnswerRefused(f"the answer could not be verified: {exc}") from None
        if not ok:
            raise AnswerRefused("the answer's signature does not verify")
        now = _now_utc(self._clock)
        state = "allowed" if decision == "allow" else "denied"
        with self._write() as conn:
            moved = self._transition(conn, request, state, now=now, answer=message.decode("utf-8"),
                                     signature=signature, signer_fp=signer_fp, before_expiry=True)
        if not moved:
            self._expire_one(request_id)
            got = self.get(request_id)
            raise AnswerRefused(f"request {request_id} is already {got[1] if got else 'gone'}")
        return Decision(request_id=request.id, decision=decision, signer_fp=signer_fp, decided_at=utc_stamp(now))

    def decision(self, request_id: str) -> Decision | None:
        """The verified decision of a terminal request; ``None`` while pending and for expired/withdrawn."""
        with self._read() as conn:
            row = self._row(conn, request_id)
        if row is None or row["state"] not in ("allowed", "denied") or self._request_of(row) is None:
            return None
        return Decision(request_id=request_id, decision="allow" if row["state"] == "allowed" else "deny",
                        signer_fp=row["signer_fp"], decided_at=row["decided_at"])

    def await_decision(self, request_id: str, timeout: float, *, poll: float = 0.1,
                       sleep: Callable[[float], None] = time.sleep) -> Decision | None:
        """Wait up to ``timeout`` seconds for a verified decision. ``None`` is NO decision: the request
        expired, was withdrawn, is unknown, or is still pending when the wait ends (the adapter then
        outputs nothing, so the native dialog stands, and withdraws or lets it expire)."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            got = self.get(request_id)
            if got is None:
                return None
            if got[1] != "pending":
                return self.decision(request_id)
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            sleep(min(poll, left))
