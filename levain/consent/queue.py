"""levain.consent.queue — the consent queue: external agents' gates, their answers, their receipts.

A hook files an :class:`~levain.consent.record.ExternalGateRequest`; the request is ``pending`` until exactly
one of four things ends it:

  - ``allowed`` / ``denied``: an answer that VERIFIES (:meth:`ConsentQueue.answer`): a record binding this
    queue, the request's id, its digest, its expiry, the decision and when it was signed (before the expiry),
    signed in :data:`~levain.consent.record.ANSWER_NAMESPACE` by a key the replayed authority log holds unrevoked, of a
    class that covers the request's tier for an allow. A delivered "yes" that does not verify is no
    decision, never an allow (the 10-08 rule): the request stays pending;
  - ``expired``: its ``expires_at`` passed with no verified answer. Silence is NO decision: the hook outputs
    nothing and the agent's native dialog stands;
  - ``withdrawn``: the requester gave up (the human answered the native dialog, the hook stopped holding).

The row is an INDEX, never authority. A process that can write ``queue.db`` raw can set any state, so every
delivery of a decision (:meth:`ConsentQueue.decision`, :meth:`ConsentQueue.await_decision`) re-reads the
stored answer and signature, checks the answer binds this queue, this request and the row's decision, and
re-verifies it against a FRESH replay of the authority log taken at delivery (the queue's ``authority``
source): a key revoked after it answered delivers nothing. Anything that fails is ``None``, never an allow;
a raw write can only take a decision away (the native dialog then stands), never make one.

A terminal request never changes again: SQL triggers refuse any update of a terminal row, any change of a
request's fields, and any delete (a guard against a buggy writer; authority is the signature above). Every
terminal transition writes its receipt (built by :mod:`levain.autonomic.receipt`) in the same transaction, so
a transition and its receipt can never disagree. Every expiry-conditioned transition reads the clock AFTER
its ``BEGIN IMMEDIATE`` holds the lock, and expires a due request before considering anything else.

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

from levain.authority import (
    AuthorityError, AuthoritySnapshot, ReplayCache, VerifierUnavailable, verify_link_sig,
)
from levain.autonomic.authority import AuthorityScope
from levain.autonomic.durable_sqlite import open_durable, read_transaction, write_transaction
from levain.autonomic.durable_sqlite import rollback as _rollback
from levain.autonomic.receipt import build_action_face, build_gate_face, build_gate_verdict
from levain.cockpit.authority import RequestAuthority
from levain.cockpit.consent import ConsentError, parse_stamp, utc_stamp
from levain.cockpit.firejournal import _CLASS_RANK, _TIER_NEEDS
from levain.consent.record import (
    ANSWER_FORMAT, ANSWER_NAMESPACE, DECISIONS, ExternalGateRequest, answer_message, answer_payload, redact_display,
    tool_input_digest,
)

__all__ = ["DB_NAME", "STATES", "STORE_FORMAT", "TERMINAL", "AnswerRefused", "AuthoritySource", "ConsentQueue",
           "Decision", "QueueStoreError", "authority_source", "decide_by_policy", "default_queue_dir"]

DB_NAME = "queue.db"
STORE_FORMAT = "levain-consent-queue/2"
STATES: tuple[str, ...] = ("pending", "allowed", "denied", "expired", "withdrawn")
TERMINAL: frozenset[str] = frozenset(STATES[1:])
_ANSWER_KEYS = frozenset({"format", "queue_id", "request_id", "request_digest", "decision", "expires_at",
                          "signer_fp", "signed_at"})
_FIELDS = ("id", "agent", "agent_session_id", "event_id", "tool_name", "tool_input_digest", "display", "cwd",
           "tier", "received_at", "expires_at")
_STATE_OF = {"allow": "allowed", "deny": "denied"}
_ACCESS_BY_OTHERS = 0o077

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, agent TEXT NOT NULL, agent_session_id TEXT NOT NULL, "
    "event_id TEXT NOT NULL, tool_name TEXT NOT NULL, tool_input_digest TEXT NOT NULL, display TEXT NOT NULL, "
    "cwd TEXT NOT NULL, tier TEXT NOT NULL, received_at TEXT NOT NULL, expires_at TEXT NOT NULL, "
    "state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','allowed','denied','expired','withdrawn')), "
    "decided_at TEXT, answer TEXT, signature TEXT, signer_fp TEXT, note TEXT)",
    "CREATE INDEX IF NOT EXISTS requests_pending ON requests (state, expires_at)",
    # a hook's own event id names one permission event: a retry of it, at any time, is refused
    "CREATE UNIQUE INDEX IF NOT EXISTS requests_hook_event ON requests (agent, agent_session_id, event_id) "
    "WHERE event_id LIKE 'hook:%'",
    "CREATE TABLE IF NOT EXISTS receipts (seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL UNIQUE "
    "REFERENCES requests(id), state TEXT NOT NULL, created_at TEXT NOT NULL, face TEXT NOT NULL)",
    # terminal is terminal; a request's fields never change; nothing is deleted; receipts are append-only
    "CREATE TRIGGER IF NOT EXISTS requests_frozen BEFORE UPDATE ON requests WHEN OLD.state != 'pending' "
    "OR NEW.state = 'pending' OR NEW.id IS NOT OLD.id OR NEW.agent IS NOT OLD.agent "
    "OR NEW.agent_session_id IS NOT OLD.agent_session_id OR NEW.event_id IS NOT OLD.event_id "
    "OR NEW.tool_name IS NOT OLD.tool_name "
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
    """A verified answer: what the hook delivers. ``decision`` is ``allow`` or ``deny``. ``request_digest`` is
    the digest of the request the answer signed: an adapter delivers only when it is the digest of the request
    it filed for the input it is holding (``ExternalGateRequest.digest``)."""
    request_id: str
    decision: str
    signer_fp: str
    decided_at: str
    request_digest: str


def default_queue_dir() -> Path:
    """``<levain home>/consent``: ``$LEVAIN_HOME/consent`` if set, else ``~/.levain/consent``, made absolute
    (as ``levain.autonomic.db.default_store_dir`` does for the autonomic store)."""
    home = os.environ.get("LEVAIN_HOME")
    return ((Path(home).expanduser() if home else Path.home() / ".levain") / "consent").resolve()


class AuthoritySource:
    """The trusted authority source for a queue; make it with :func:`authority_source`. Each call opens the
    cockpit database read-only, begins a read transaction and returns the verified log head that
    :meth:`levain.cockpit.authority.RequestAuthority.capture` (``levain/cockpit/authority.py``, the one
    snapshot builder, DESIGN §3.4) takes in it: the anchor file read first, then the replay, then the replay
    checked against that anchor, so an append committing in between can only put the log ahead of the anchor.
    ``anchor=None`` checks no anchor, so the replay is only as trustworthy as the database file: production
    passes the real anchor. Raises on any failure: capture's refusal (a refused log or anchor, a verifier that
    cannot run, an empty or uninitialised log) as :class:`levain.authority.AuthorityError` with capture's
    reason, a journal read as ``sqlite3.Error``; the queue turns either into no decision.

    A queue takes only this class (not a subclass, not any callable): freshness is the queue's guarantee,
    never a promise a caller's ``lambda: snapshot`` could break."""

    __slots__ = ("_uri", "_anchor", "_kw")

    def __init__(self, cockpit_db: Path | str, *, anchor: Path | None, cache: ReplayCache | None = None,
                 ssh_keygen: Path | str | None = None) -> None:
        self._uri = Path(cockpit_db).resolve().as_uri() + "?mode=ro"
        self._anchor = None if anchor is None else Path(anchor)
        self._kw: dict[str, Any] = {"cache": cache}
        if ssh_keygen is not None:
            self._kw["ssh_keygen"] = ssh_keygen

    def __call__(self) -> AuthoritySnapshot:
        conn = sqlite3.connect(self._uri, uri=True, isolation_level=None, timeout=30.0)
        try:
            conn.execute("BEGIN")
            got = RequestAuthority.capture(conn, now=datetime.now(timezone.utc), anchor=self._anchor, **self._kw)
            conn.execute("COMMIT")
        finally:
            conn.close()
        if got.authority is None or got.authority_error is not None:
            raise AuthorityError(got.authority_error or "the authority log yields no head")
        return got.authority


def authority_source(cockpit_db: Path | str, *, anchor: Path | None, cache: ReplayCache | None = None,
                     ssh_keygen: Path | str | None = None) -> AuthoritySource:
    """The trusted authority source for a queue (see :class:`AuthoritySource`)."""
    return AuthoritySource(cockpit_db, anchor=anchor, cache=cache, ssh_keygen=ssh_keygen)


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
    connection, so one object is safe across threads and several processes may share the store.

    ``authority`` is the trusted source every answer and every delivered decision is verified against
    (:func:`authority_source`), called afresh each time: never a snapshot a caller captured earlier."""

    def __init__(self, directory: Path | str | None = None, *, authority: AuthoritySource,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 ssh_keygen: Path | str | None = None) -> None:
        if type(authority) is not AuthoritySource:
            raise ConsentError("authority must be the AuthoritySource authority_source() returns, which replays "
                               "the authority log on every call; a snapshot or another callable is refused")
        self.directory = Path(directory) if directory is not None else default_queue_dir()
        self.path = self.directory / DB_NAME
        self._authority = authority
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
            row = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone() if tables else None
            if row is not None and row[0] != STORE_FORMAT:
                raise QueueStoreError(f"store format {row[0]!r} is not {STORE_FORMAT!r}")
            for stmt in _SCHEMA:
                conn.execute(stmt)
            if row is None:
                conn.execute("INSERT INTO meta (key, value) VALUES ('format', ?)", (STORE_FORMAT,))
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

    def _fresh_authority(self) -> AuthoritySnapshot | None:
        """A replay taken now, or ``None`` when the log cannot be replayed or checked."""
        try:
            snap = self._authority()
        except (AuthorityError, sqlite3.Error, OSError, ValueError):
            return None
        return snap if isinstance(snap, AuthoritySnapshot) else None

    # --- filing --------------------------------------------------------------------------------------
    def file(self, *, agent: str, agent_session_id: str, tool_name: str, tool_input: Any, cwd: str,
             hold: timedelta, tier: str = "T2", event_id: str | None = None) -> ExternalGateRequest:
        """File one gate an agent is waiting on; it expires ``hold`` after now. ``tier`` must be one an
        allow can be signed at (``T2`` or ``T3``). ``event_id`` is the hook's own unique id for this
        permission event when its agent has one; without it the request gets an unpredictable one. Either
        way two filings are two requests, never one returned twice: a hook event id is unique per agent and
        session in the store, so filing the same event again raises, at any time."""
        if tier not in _TIER_NEEDS:
            raise ConsentError(f"tier must be one of {tuple(_TIER_NEEDS)}: {tier!r}")
        if not isinstance(hold, timedelta) or hold <= timedelta(0):
            raise ConsentError("hold must be a positive timedelta")
        if event_id is None:
            event = "rand:" + os.urandom(16).hex()
        elif isinstance(event_id, str) and event_id:
            event = "hook:" + event_id
        else:
            raise ConsentError("event_id must be a non-empty string")
        now = _now_utc(self._clock)
        request = ExternalGateRequest.create(
            agent=agent, agent_session_id=agent_session_id, event_id=event, tool_name=tool_name,
            tool_input_digest=tool_input_digest(tool_input), display=redact_display(tool_name, tool_input),
            cwd=cwd, tier=tier, received_at=utc_stamp(now), expires_at=utc_stamp(now + hold))
        try:
            with self._write() as conn:
                conn.execute(f"INSERT INTO requests ({', '.join(_FIELDS)}) VALUES "
                             f"({', '.join('?' for _ in _FIELDS)})", tuple(request.fields()[f] for f in _FIELDS))
        except sqlite3.IntegrityError:
            raise ConsentError(f"request {request.id} (event {event}) is already filed: one event is filed once"
                               ) from None
        return request

    # --- reading -------------------------------------------------------------------------------------
    @staticmethod
    def _request_of(row: Mapping[str, Any]) -> ExternalGateRequest | None:
        try:
            request = ExternalGateRequest(**{f: row[f] for f in _FIELDS})
            return request if request.seal_matches() else None
        except (ConsentError, TypeError, ValueError):
            return None

    def _row(self, conn: sqlite3.Connection, request_id: str) -> dict[str, Any] | None:
        cur = conn.execute(f"SELECT {', '.join(_FIELDS)}, state, decided_at, answer, signature, signer_fp "
                           "FROM requests WHERE id = ?", (request_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip([d[0] for d in cur.description], row))

    def get(self, request_id: str) -> tuple[ExternalGateRequest, str] | None:
        """The request and its state, after expiring it if its time is up. ``None`` if unknown or its seal
        does not match (a row someone edited is never answered). Takes the write lock only when the row is
        actually due to expire."""
        with self._read() as conn:
            row = self._row(conn, request_id)
        if row is None:
            return None
        if row["state"] == "pending" and row["expires_at"] <= utc_stamp(_now_utc(self._clock)):
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
                               "ORDER BY received_at, rowid")
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

    def _transition(self, conn: sqlite3.Connection, request: ExternalGateRequest, state: str, *, stamp: str,
                    answer: str | None = None, signature: str | None = None, signer_fp: str | None = None,
                    note: str | None = None) -> bool:
        """One ``pending -> state`` move plus its receipt, inside the caller's write transaction. False when
        the request is no longer pending: nothing is written."""
        if conn.execute("UPDATE requests SET state = ?, decided_at = ?, answer = ?, signature = ?, signer_fp = ?, "
                        "note = ? WHERE id = ? AND state = 'pending'",
                        (state, stamp, answer, signature, signer_fp, note, request.id)).rowcount != 1:
            return False
        conn.execute("INSERT INTO receipts (request_id, state, created_at, face) VALUES (?, ?, ?, ?)",
                     (request.id, state, stamp, json.dumps(self._face(request, state, signer_fp),
                                                           ensure_ascii=False, sort_keys=True)))
        return True

    def _settle(self, conn: sqlite3.Connection, request_id: str
                ) -> tuple[ExternalGateRequest | None, str | None, str]:
        """Inside a held write lock: read the clock NOW, expire the request if it is due, and return the
        sealed request (or ``None``), its state after that, and the stamp every further transition in this
        transaction uses."""
        stamp = utc_stamp(_now_utc(self._clock))
        row = self._row(conn, request_id)
        request = None if row is None else self._request_of(row)
        if row is None or request is None:
            return None, None, stamp
        state = row["state"]
        if state == "pending" and request.expires_at <= stamp:
            self._transition(conn, request, "expired", stamp=stamp)
            state = "expired"
        return request, state, stamp

    def _expire_one(self, request_id: str) -> None:
        with self._write() as conn:
            self._settle(conn, request_id)

    def sweep(self) -> int:
        """Expire every pending request whose time is up; returns how many."""
        done = 0
        with self._write() as conn:
            stamp = utc_stamp(_now_utc(self._clock))
            cur = conn.execute(f"SELECT {', '.join(_FIELDS)} FROM requests WHERE state = 'pending' "
                               "AND expires_at <= ?", (stamp,))
            names = [d[0] for d in cur.description]
            for raw in cur.fetchall():
                request = self._request_of(dict(zip(names, raw)))
                if request is not None and self._transition(conn, request, "expired", stamp=stamp):
                    done += 1
        return done

    def withdraw(self, request_id: str, *, note: str = "") -> bool:
        """The requester stops waiting (the human answered the native dialog; the hook gave up). Needs no
        signature: it decides nothing, it leaves the agent's own dialog. False if the request is not pending
        (a request whose time is up is expired, not withdrawn)."""
        with self._write() as conn:
            request, state, stamp = self._settle(conn, request_id)
            if request is None or state != "pending":
                return False
            return self._transition(conn, request, "withdrawn", stamp=stamp, note=note or None)

    # --- answers -------------------------------------------------------------------------------------
    def _refusal(self, request: ExternalGateRequest, queue_id: str, payload: Any, signature: Any,
                 authority: AuthoritySnapshot) -> str | None:
        """Why ``payload``/``signature`` is not authority for ``request`` under ``authority``; ``None`` when it
        verifies. The one check an answer passes to be stored AND again every time it is delivered."""
        if not isinstance(payload, Mapping) or set(payload) != _ANSWER_KEYS:
            return "the answer is not an answer record"
        if payload["format"] != ANSWER_FORMAT:
            return f"answer format {payload['format']!r} is not {ANSWER_FORMAT!r}"
        if payload["queue_id"] != queue_id:
            return "the answer names another queue"
        if payload["request_id"] != request.id:
            return "the answer is for another request"
        if payload["request_digest"] != request.digest:
            return "the answer signs another version of this request"
        if payload["expires_at"] != request.expires_at:
            return "the answer names another expiry"
        signed_at = payload["signed_at"]
        try:
            parse_stamp(signed_at)
        except ConsentError:
            return "the answer's signed_at is not a timestamp"
        if signed_at < request.received_at:
            return "the answer was signed before the request was filed"
        if signed_at >= request.expires_at:
            return "the answer was signed after the request expired"
        decision = payload["decision"]
        if decision not in DECISIONS:
            return f"decision {decision!r} is not one of {DECISIONS}"
        signer_fp = payload["signer_fp"]
        key = authority.key(signer_fp) if isinstance(signer_fp, str) else None
        if key is None:
            return "the signer is not an enrolled key"
        if not key.active:
            return "the signer's key is revoked"
        if decision == "allow":
            need, have = _TIER_NEEDS[request.tier], _CLASS_RANK.get(key.key_class)
            if have is None or have < need:
                return f"key class {key.key_class} does not cover {request.tier}"
        if not isinstance(signature, str) or not signature:
            return "the answer carries no signature"
        try:
            message = answer_message(dict(payload))
        except ConsentError as exc:
            return f"the answer cannot be encoded: {exc}"
        kwargs: dict[str, Any] = {} if self._ssh_keygen is None else {"ssh_keygen": self._ssh_keygen}
        try:
            ok = verify_link_sig(message, signature, public_key=key.public_key, fingerprint=key.fingerprint,
                                 namespace=ANSWER_NAMESPACE, **kwargs)
        except VerifierUnavailable as exc:
            return f"the answer could not be verified: {exc}"
        return None if ok else "the answer's signature does not verify"

    def answer(self, request_id: str, payload: Mapping[str, Any], signature: str) -> Decision:
        """Apply a signed answer to ``request_id``, or raise :class:`AnswerRefused` (the request stays as it
        was, or becomes ``expired`` when the answer came too late). The answer is verified against a fresh
        replay from the queue's authority source."""
        got = self.get(request_id)                         # expires it first if its time is up
        if got is None:
            raise AnswerRefused(f"no sealed request {request_id!r}")
        request, state = got
        if state != "pending":
            raise AnswerRefused(f"request {request_id} is already {state}")
        authority = self._fresh_authority()
        if authority is None:
            raise AnswerRefused("the authority log cannot be replayed now: no answer is authority")
        why = self._refusal(request, self.queue_id, payload, signature, authority)
        if why is not None:
            raise AnswerRefused(why)
        decision = payload["decision"]
        message = answer_message(dict(payload))
        with self._write() as conn:
            current, now_state, stamp = self._settle(conn, request_id)
            moved = current is not None and now_state == "pending" and self._transition(
                conn, request, _STATE_OF[decision], stamp=stamp, answer=message.decode("utf-8"),
                signature=signature, signer_fp=payload["signer_fp"])
        if not moved:
            raise AnswerRefused(f"request {request_id} is already {now_state or 'gone'}")
        return Decision(request_id=request.id, decision=decision, signer_fp=payload["signer_fp"], decided_at=stamp,
                        request_digest=request.digest)

    def answer_payload(self, request_id: str, decision: str, signer_fp: str) -> dict[str, str]:
        """The record to sign to answer ``request_id``, ``signed_at`` now by the queue's clock (what the
        cockpit shows a signer). Raises :class:`AnswerRefused` when there is no sealed pending request."""
        got = self.get(request_id)
        if got is None:
            raise AnswerRefused(f"no sealed request {request_id!r}")
        request, state = got
        if state != "pending":
            raise AnswerRefused(f"request {request_id} is already {state}")
        return answer_payload(queue_id=self.queue_id, request=request, decision=decision, signer_fp=signer_fp,
                              signed_at=utc_stamp(_now_utc(self._clock)))

    def decision(self, request_id: str) -> Decision | None:
        """The decision of a terminal request, re-verified now: the stored answer must be the canonical
        record of this row's decision for this queue and request, decided before its expiry, and its
        signature must verify under a key a fresh replay holds active (and of a class covering the tier,
        for an allow). ``None`` while pending, for expired/withdrawn, and for anything that fails."""
        with self._read() as conn:
            row = self._row(conn, request_id)
            queue_id = conn.execute("SELECT value FROM meta WHERE key = 'queue_id'").fetchone()
        if row is None or queue_id is None or row["state"] not in ("allowed", "denied"):
            return None
        request = self._request_of(row)
        stored, decided_at = row["answer"], row["decided_at"]
        # "in time" is the SIGNED signed_at (checked in _refusal), never decided_at, which a raw writer sets
        if request is None or not isinstance(stored, str) or not isinstance(decided_at, str):
            return None
        try:
            payload = json.loads(stored)
            if not isinstance(payload, dict) or answer_message(payload) != stored.encode("utf-8"):
                return None
        except (ValueError, ConsentError, RecursionError):
            return None
        named = payload.get("decision")
        if not isinstance(named, str) or _STATE_OF.get(named) != row["state"] \
                or payload.get("signer_fp") != row["signer_fp"]:
            return None
        authority = self._fresh_authority()
        if authority is None or self._refusal(request, queue_id[0], payload, row["signature"], authority) is not None:
            return None
        return Decision(request_id=request.id, decision=payload["decision"], signer_fp=payload["signer_fp"],
                        decided_at=decided_at, request_digest=request.digest)

    def await_decision(self, request_id: str, timeout: float, *, poll: float = 0.1,
                       sleep: Callable[[float], None] = time.sleep) -> Decision | None:
        """Wait up to ``timeout`` seconds for a verified decision. ``None`` is NO decision: the request
        expired, was withdrawn, is unknown, its stored answer no longer verifies, or it is still pending
        when the wait ends (the adapter then outputs nothing, so the native dialog stands, and withdraws or
        lets it expire). ``poll`` below 1ms (or not a number) is 1ms."""
        poll = poll if poll >= 0.001 else 0.001             # NaN compares false: it becomes 1ms too
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
