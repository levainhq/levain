"""levain.coordinator.envelopes — thread envelopes in the coordinator's store: accepted, bound, narrowed, live (C2).

The rules are C0 §4 (rev 6); this module is their store half, beside the drops and threads of
:mod:`levain.coordinator.store` in the same ``dispatch.db``, so an envelope's liveness and any write it will authorise
share one ``BEGIN IMMEDIATE`` (C0 §2).

- **Acceptance** (:meth:`EnvelopeStore.accept_envelope` for a WORDED envelope, the coordinator's echo signed by you;
  :meth:`EnvelopeStore.accept_drop` with ``envelope=`` for a STRUCTURED one signed at send). The signature is checked
  first (:func:`levain.coordinator.envelope.refusal`); then, in one transaction: an exact retry writes nothing and any
  other reuse of the id is a conflict; this store, this drop, these words; ``signed_at`` within the skew of the
  store's clock, ``stop_at`` ahead of it and no more than ``MAX_LIFETIME`` after signing; every bind is its thread's
  CURRENT, non-empty revision (``members_sha256`` recomputed); the supersede names every covered thread's current
  head, digest included, and no head may be forked. A structured envelope binds mechanically when its drop is first
  split into exactly one new thread holding that drop alone [seat: a split into several threads binds nothing until
  an echo naming them is signed, C0 §4.3 "split or merged any other way"].
- **Narrowing** (:meth:`EnvelopeStore.attenuate`): kill, stop earlier, drop one effect; and ``thread_done``, written
  by the store itself when a bound thread goes done or closed (T7). Never a signature, never undone.
- **Liveness** (:meth:`EnvelopeStore.live_refusal`, C0 §4.5) and the **cumulative ceiling**
  (:meth:`EnvelopeStore.ceiling_refusal`: the envelope and every successor in its chain for the thread, each by its
  effective envelope, all must allow).

**BEFORE S1 NOTHING HERE IS AUTHORITY** (C0 §2; T3, *"RESTS ON S1 (without S1: per-call consent)"*). Until S1 places
the control principal, any process of your uid can write this file, so :meth:`EnvelopeStore.authority_refusal` refuses
every effect with :data:`NOT_IN_FORCE`, whatever the rows say, and every gated call stays a per-call SIGN. The S1
cutover makes a NEW store (C0 §2), so no row written here can become authority later. Stdlib only.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from levain.authority import AuthoritySnapshot, VerifierUnavailable
from levain.autonomic.durable_sqlite import read_transaction
from levain.cockpit.consent import ConsentError, parse_stamp, utc_stamp
from levain.consent.queue import RULING_SKEW
from levain.contracts.canon import canonical
from levain.coordinator import envelope as env
from levain.coordinator.store import (
    SCHEMA, CoordError, CoordStore, Drop, Source, _append_only, _in, members_sha256,
)
from levain.dispatch.adopt import GitFault, branch_problem
from levain.dispatch.contract import MAX_LIFETIME

__all__ = ["ATTENUATIONS", "ENVELOPE_SCHEMA", "NOT_IN_FORCE", "Envelope", "EnvelopeConflict", "EnvelopeStore"]

ATTENUATIONS = ("kill", "stop_earlier", "drop_effect", "thread_done")
NOT_IN_FORCE = ("before S1 places the control principal the coordinator grants nothing (C0 §2): this effect is a "
                "per-call SIGN")
# the queue's skew for a signature's signed_at against the clock that receives it
SIGN_SKEW: timedelta = RULING_SKEW

ENVELOPE_SCHEMA: tuple[str, ...] = (
    # one store-wide order for every authority row: never wall-clock (C0 §3.4)
    "CREATE TABLE IF NOT EXISTS gseq (gseq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS envelopes (envelope_id TEXT PRIMARY KEY, gseq INTEGER NOT NULL UNIQUE, "
    "origin TEXT NOT NULL CHECK (origin IN ('structured', 'worded')), drop_id TEXT NOT NULL REFERENCES drops(drop_id), "
    "body TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL, signature TEXT NOT NULL, "
    "signer_fp TEXT NOT NULL, signer_class TEXT NOT NULL, supersedes TEXT, stop_at TEXT NOT NULL, "
    "accepted_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS envelope_binds (envelope_id TEXT NOT NULL REFERENCES envelopes(envelope_id), "
    "thread_id TEXT NOT NULL REFERENCES threads(thread_id), rev INTEGER NOT NULL, gseq INTEGER NOT NULL UNIQUE, "
    "at TEXT NOT NULL, PRIMARY KEY (envelope_id, thread_id))",
    "CREATE TABLE IF NOT EXISTS envelope_attenuations (envelope_id TEXT NOT NULL REFERENCES envelopes(envelope_id), "
    f"thread_id TEXT REFERENCES threads(thread_id), kind TEXT NOT NULL CHECK ({_in('kind', ATTENUATIONS)}), "
    "value TEXT NOT NULL, by TEXT NOT NULL CHECK (by IN ('operator', 'service')), gseq INTEGER NOT NULL UNIQUE, "
    "at TEXT NOT NULL, PRIMARY KEY (envelope_id, gseq))",
    *_append_only("gseq", "the sequence"),
    *_append_only("envelopes", "an envelope"),
    *_append_only("envelope_binds", "an envelope bind"),
    *_append_only("envelope_attenuations", "an attenuation"),
    "CREATE TRIGGER IF NOT EXISTS envelopes_no_replace BEFORE INSERT ON envelopes WHEN EXISTS (SELECT 1 FROM "
    "envelopes WHERE envelope_id = NEW.envelope_id OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'an envelope is never replaced'); END",
    "CREATE TRIGGER IF NOT EXISTS envelope_binds_no_replace BEFORE INSERT ON envelope_binds WHEN EXISTS (SELECT 1 "
    "FROM envelope_binds WHERE (envelope_id = NEW.envelope_id AND thread_id = NEW.thread_id) OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a bind is never replaced'); END",
    "CREATE TRIGGER IF NOT EXISTS envelope_attenuations_no_replace BEFORE INSERT ON envelope_attenuations WHEN "
    "EXISTS (SELECT 1 FROM envelope_attenuations WHERE gseq = NEW.gseq OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'an attenuation is never replaced'); END",
)


class EnvelopeConflict(CoordError):
    """An ``envelope_id`` already stored with a different body, payload or signature: never an overwrite."""


@dataclass(frozen=True)
class Envelope:
    envelope_id: str
    gseq: int
    origin: str
    drop_id: str
    body: dict[str, Any]
    digest: str
    payload: dict[str, str]
    signature: str
    signer_fp: str
    signer_class: str
    supersedes: str | None
    stop_at: str
    accepted_at: str


def _git_toplevel(repo: str) -> str | None:
    """The realpath of ``repo``'s work tree top, by git itself; None when it is not one (a bare repo included)."""
    try:
        cp = subprocess.run(["git", "-C", repo, "rev-parse", "--show-toplevel"], capture_output=True, timeout=30,
                            env={**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
                                 "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0:
        return None
    return os.path.realpath(cp.stdout.decode("utf-8", "surrogateescape").strip())


class EnvelopeStore(CoordStore):
    """The coordinator store with envelopes (C2). Every check that reads the disk (repo realpaths) runs before the
    write lock; everything that reads the store runs inside one ``BEGIN IMMEDIATE`` with the clock read under it."""

    _SCHEMA = SCHEMA + ENVELOPE_SCHEMA

    def __init__(self, directory: Any = None, *, ssh_keygen: Any = None, **kw: Any) -> None:
        super().__init__(directory, **kw)
        self._ssh_keygen = ssh_keygen

    # --- acceptance ----------------------------------------------------------------------------------------------
    def check_body(self, body_text: Any) -> dict[str, Any]:
        """The body parsed and held to every check that reads the disk (each where repo a git work tree's top by its
        real path, each land's branch by F11's branch_problem), with no signature: what echo runs before writing a
        file and sign runs before asking, so a body the store can only refuse is never signed. Raises CoordError."""
        body = self._parsed(body_text)
        self._disk_checks(body)
        return body

    def _parsed(self, body_text: Any) -> dict[str, Any]:
        try:
            body = env.parse_body(body_text)
        except env.EnvelopeError as exc:
            raise CoordError(str(exc)) from None
        except (TypeError, ValueError, KeyError) as exc:
            # a shape parse_body did not name is still a refusal, never a lost drop
            raise CoordError(f"an envelope body this reader cannot read: {type(exc).__name__}") from None
        return body

    def _disk_checks(self, body: Mapping[str, Any]) -> None:
        for w in body["where"]:
            if _git_toplevel(w["repo"]) != w["repo"]:
                raise CoordError(f"where repo {w['repo'][:120]} is not a git work tree's top, by its real path")
        for land in body["lands"]:
            try:
                problem = branch_problem(land["repo"], land["onto"])
            except GitFault as exc:
                raise CoordError(f"a land onto {land['onto'][:80]} could not be checked on this machine: "
                                 f"{str(exc)[:160]}") from None
            if problem is not None:
                raise CoordError(f"a land onto {land['onto'][:80]}: {problem}")

    def _checked(self, body_text: Any, payload: Any, signature: Any,
                 authority: AuthoritySnapshot) -> tuple[dict[str, Any], str]:
        """The body parsed, its signature verified and its disk checks run: ``(body, signer_class)``."""
        body = self._parsed(body_text)
        refused = env.refusal(payload, signature, authority, body, ssh_keygen=self._ssh_keygen)
        if refused is not None:
            raise CoordError(refused)
        self._disk_checks(body)
        return body, authority.key(payload["signer_fp"]).key_class

    @staticmethod
    def _next_gseq(conn: sqlite3.Connection, now: str) -> int:
        return conn.execute("INSERT INTO gseq (at) VALUES (?)", (now,)).lastrowid

    def _same_or_conflict(self, conn: sqlite3.Connection, body: Mapping[str, Any], payload: Mapping[str, Any],
                          signature: str) -> Envelope | None:
        stored = self._envelope(conn, body["envelope_id"])
        if stored is None:
            return None
        if canonical(stored.body) != canonical(dict(body)) or stored.payload != dict(payload) \
                or stored.signature != signature:
            raise EnvelopeConflict(f"envelope {body['envelope_id']} is already stored with a different body, payload "
                                   "or signature; nothing was written")
        return stored

    def _admit(self, conn: sqlite3.Connection, now: str, body: Mapping[str, Any], payload: Mapping[str, Any],
               drop: Drop) -> None:
        """Every acceptance check that reads the store or its clock (C0 §4.3), binds excepted."""
        store_id = self._jobs._store_id(conn)
        if body["store_id"] != store_id or payload["store_id"] != store_id:
            raise CoordError("the envelope is for another dispatch store")
        if drop.drop_id != body["drop_id"] or drop.words_sha256 != body["words_sha256"]:
            raise CoordError("the envelope is for other words than its drop's")
        clock = parse_stamp(now)
        signed = parse_stamp(payload["signed_at"])
        if abs(signed - clock) > SIGN_SKEW:
            raise CoordError(f"the envelope was signed at {payload['signed_at']}, more than "
                             f"{int(SIGN_SKEW.total_seconds())} s from the store's clock ({now}); sign it again")
        stop = parse_stamp(body["stop_at"])
        if stop <= signed:
            raise CoordError("the envelope stops before it was signed")
        if stop <= clock:
            raise CoordError(f"the envelope's stop time {body['stop_at']} has passed")
        if stop - signed > MAX_LIFETIME:
            raise CoordError(f"an envelope stops at most {int(MAX_LIFETIME.total_seconds() // 3600)} hours after it "
                             "is signed")
        for land in body["lands"]:
            if land["from"] == "source":
                want = "drop" if drop.source.how == "cwd" else "picker" if drop.source.how == "picker" else None
                if want is None or land["onto_from"] != want or land["repo"] != drop.source.repo \
                        or f"refs/heads/{land['onto']}" != drop.source.ref:
                    raise CoordError(f"a land from source names the branch the drop was made from "
                                     f"({drop.source.ref or 'none recorded'}); this one does not")

    def _insert(self, conn: sqlite3.Connection, now: str, body: Mapping[str, Any], payload: Mapping[str, Any],
                signature: str, signer_class: str) -> None:
        sup = body["supersedes"]
        conn.execute(
            "INSERT INTO envelopes (envelope_id, gseq, origin, drop_id, body, digest, payload, signature, signer_fp, "
            "signer_class, supersedes, stop_at, accepted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (body["envelope_id"], self._next_gseq(conn, now), body["origin"], body["drop_id"], canonical(dict(body)),
             env.envelope_digest(body), canonical(dict(payload)), signature, payload["signer_fp"], signer_class,
             None if sup is None else sup["envelope_id"], body["stop_at"], now))

    @classmethod
    def _bind_checked(cls, conn: sqlite3.Connection, now: str, envelope_id: str, body: Mapping[str, Any],
                      binds: Sequence[tuple[str, int, str]]) -> None:
        """Every bind's checks, then the supersede CAS over the covered threads, then the bind rows (C0 §4.3). Each
        ``(thread_id, rev, members_sha256)`` must be the thread's CURRENT, non-empty revision (the sha recomputed from
        its members), holding the envelope's drop, on a thread that is not done or closed: an envelope never binds
        where no end would reach it, so reopening a thread revives nothing (C0 §4.5). Every bind path comes here."""
        for thread_id, rev, msha in binds:
            t = cls._thread(conn, thread_id)
            if t.rev != rev or members_sha256(t.members) != msha or t.members_sha256 != msha:
                raise CoordError(f"thread {thread_id} moved since the echo (revision {t.rev}, not {rev}); echo again")
            if not t.members:
                raise CoordError(f"thread {thread_id} holds no drops; nothing to bind")
            if t.state in ("done", "closed"):
                raise CoordError(f"thread {thread_id} is {t.state}; reopen it before signing an envelope for it")
            if body["drop_id"] not in t.members:
                raise CoordError(f"thread {thread_id} does not hold the envelope's drop")
        sup = body["supersedes"]
        for thread_id, _rev, _msha in binds:
            head = cls._head(conn, thread_id)
            if sup is None:
                if head is not None:
                    raise CoordError(f"thread {thread_id} already has envelope {head.envelope_id}; a new envelope "
                                     "for it supersedes that one")
            elif head is None or head.envelope_id != sup["envelope_id"] or head.digest != sup["digest"]:
                raise CoordError(f"the envelope supersedes {sup['envelope_id']}, which is not thread {thread_id}'s "
                                 "current envelope; echo again")
        for thread_id, rev, _msha in binds:
            conn.execute("INSERT INTO envelope_binds (envelope_id, thread_id, rev, gseq, at) VALUES (?, ?, ?, ?, ?)",
                         (envelope_id, thread_id, rev, cls._next_gseq(conn, now), now))

    def accept_envelope(self, body_text: str, payload: Mapping[str, Any], signature: str, *,
                        authority: AuthoritySnapshot) -> tuple[Envelope, bool]:
        """Accept a WORDED envelope (the coordinator's echo, signed by you) bound to the thread revisions it names;
        ``(envelope, True)``. The identical record again writes nothing: ``(stored, False)``."""
        body, signer_class = self._checked(body_text, payload, signature, authority)
        if body["origin"] != "worded":
            raise CoordError("a structured envelope arrives with its drop (accept_drop), never later")
        with self._write() as (conn, now):
            stored = self._same_or_conflict(conn, body, payload, signature)
            if stored is not None:
                return stored, False
            drop = self._must_drop(conn, body["drop_id"])
            self._admit(conn, now, body, payload, drop)
            binds = [(self._must_thread(conn, b["thread_id"]).thread_id, b["rev"], b["members_sha256"])
                     for b in body["binds"]]
            self._insert(conn, now, body, payload, signature, signer_class)
            self._bind_checked(conn, now, body["envelope_id"], body, binds)
            return self._envelope(conn, body["envelope_id"]), True

    def accept_drop(self, words: str, *, surface: str, outbox_id: str | None = None, sent_at: str | None = None,
                    source: Source = Source(), envelope: tuple[str, Mapping[str, Any], str] | None = None,
                    authority: AuthoritySnapshot | None = None) -> tuple[Drop, bool]:
        """A drop, optionally carrying a STRUCTURED envelope signed at send (T4): ``envelope = (body_text, payload,
        signature)``, whose body names the drop id. Drop and envelope are written in one transaction. An envelope that
        fails any check never costs the words: the drop is stored plain and its delivered drop receipt says why the
        envelope was not taken (:meth:`envelope_refused`). A resend of the same ``outbox_id`` returns the stored drop
        as it was (C1's outbox rule), so "sign it again" for a stored drop is the coordinator's WORDED echo over its
        thread [seat: a structured envelope is only ever written in the drop's insert, C0 §3.1]."""
        if envelope is None:
            return super().accept_drop(words, surface=surface, outbox_id=outbox_id, sent_at=sent_at, source=source)
        if not isinstance(authority, AuthoritySnapshot):
            raise CoordError("a drop carrying an envelope needs the authority snapshot to verify it against")
        refused: str | None = None
        body: dict[str, Any] | None = None
        try:
            body_text, payload, signature = envelope
            body, signer_class = self._checked(body_text, payload, signature, authority)
            if body["origin"] != "structured":
                raise CoordError("a drop carries only a structured envelope; a worded one is the coordinator's echo")
        except (CoordError, ValueError, TypeError) as exc:
            refused, body = str(exc)[:300], None
        except VerifierUnavailable as exc:
            # a fault on this machine, never a verdict: the words are still kept, and the receipt says why
            refused, body = f"the signature could not be checked on this machine ({str(exc)[:200]})", None

        def carry(conn: sqlite3.Connection, now: str, **drop: Any) -> Drop:
            if body is None:
                return self._insert_drop(conn, now, **drop, receipt={"envelope_refused": refused})
            probe = Drop(body["drop_id"], drop["surface"], drop["outbox_id"], drop["words"], drop["sha"],
                         drop["sent_at"], now, drop["source"], body["envelope_id"])
            try:
                if conn.execute("SELECT 1 FROM drops WHERE drop_id = ?", (body["drop_id"],)).fetchone() is not None:
                    raise EnvelopeConflict(f"drop {body['drop_id']} is already stored; an envelope signed for it "
                                           "arrives only with it")
                if self._envelope(conn, body["envelope_id"]) is not None:
                    raise EnvelopeConflict(f"envelope {body['envelope_id']} is already stored; its id is never reused "
                                           "(for another drop or another body)")
                self._admit(conn, now, body, payload, probe)
            except CoordError as exc:
                return self._insert_drop(conn, now, **drop, receipt={"envelope_refused": str(exc)[:300]})
            stored = self._insert_drop(conn, now, **drop, drop_id=body["drop_id"], envelope_id=body["envelope_id"])
            self._insert(conn, now, body, payload, signature, signer_class)
            return stored

        return super().accept_drop(words, surface=surface, outbox_id=outbox_id, sent_at=sent_at, source=source,
                                   _carry=carry)

    # --- the store's own hooks: C1's CoordStore calls these from every split and every end, whichever class opened
    # the file, so no opener (C1's CLI builds a plain CoordStore) can skip T7 or the structured bind
    @staticmethod
    def _has_envelopes(conn: sqlite3.Connection) -> bool:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'envelope_binds'").fetchone() \
            is not None

    @classmethod
    def bind_on_split(cls, conn: sqlite3.Connection, drop_id: str, thread_ids: list[str], now: str) -> None:
        """A drop's first split: its structured envelope binds when the split is exactly one thread holding that drop
        alone at revision 1 (C0 §4.3)."""
        if not cls._has_envelopes(conn) or len(thread_ids) != 1:
            return
        drop = cls._drop(conn, drop_id)
        if drop.envelope_id is None:
            return
        t = cls._thread(conn, thread_ids[0])
        stored = cls._envelope(conn, drop.envelope_id)
        if stored is not None and t.rev == 1 and t.members == (drop_id,) and cls._head(conn, t.thread_id) is None:
            cls._bind_checked(conn, now, stored.envelope_id, stored.body, [(t.thread_id, 1, t.members_sha256)])

    @classmethod
    def end_thread(cls, conn: sqlite3.Connection, thread_id: str, now: str) -> None:
        """T7: a thread gone done or closed kills its envelopes, inside the transaction that ended it."""
        if not cls._has_envelopes(conn):
            return
        for (envelope_id,) in conn.execute("SELECT envelope_id FROM envelope_binds WHERE thread_id = ? ORDER BY gseq",
                                           (thread_id,)).fetchall():
            cls._attenuate(conn, now, envelope_id, "thread_done", "", thread_id, "service")

    # --- narrowing -----------------------------------------------------------------------------------------------
    def attenuate(self, envelope_id: str, kind: str, value: str = "", *, thread_id: str | None = None,
                  by: str = "operator") -> bool:
        """Narrow an envelope with no signature (C0 §4.4): ``kill``; ``stop_earlier`` (``value`` a stamp before its
        current stop); ``drop_effect`` (``value`` an effect identity its signed body holds). ``thread_id`` scopes it to
        one bound thread. True when a row was written; a narrowing already in force writes nothing (False)."""
        if kind not in ("kill", "stop_earlier", "drop_effect"):
            raise CoordError("an attenuation you write is kill, stop_earlier or drop_effect")
        if not isinstance(envelope_id, str) or not isinstance(value, str) \
                or not (thread_id is None or isinstance(thread_id, str)):
            raise CoordError("an attenuation names an envelope id, a text value and at most one thread id")
        by = self._actor(by, ("operator",))
        with self._write() as (conn, now):
            return self._attenuate(conn, now, envelope_id, kind, value, thread_id, by)

    @classmethod
    def _attenuate(cls, conn: sqlite3.Connection, now: str, envelope_id: str, kind: str, value: str,
                   thread_id: str | None, by: str) -> bool:
        stored = cls._envelope(conn, envelope_id)
        if stored is None:
            raise CoordError(f"no envelope {str(envelope_id)[:40]}")
        if thread_id is not None and conn.execute(
                "SELECT 1 FROM envelope_binds WHERE envelope_id = ? AND thread_id = ?",
                (envelope_id, thread_id)).fetchone() is None:
            raise CoordError(f"envelope {envelope_id} is not bound to thread {str(thread_id)[:40]}")
        rows = cls._attenuations(conn, envelope_id, thread_id)
        if kind in ("kill", "thread_done"):
            if not isinstance(value, str) or value:
                raise CoordError(f"a {kind} carries no value")
            if any(k in ("kill", "thread_done") for k, _v in rows):
                return False
        elif kind == "stop_earlier":
            try:
                at = parse_stamp(value)
            except (ConsentError, TypeError):
                raise CoordError("stop_earlier names a UTC timestamp") from None
            if at == cls._stop(stored, rows):
                return False
            if at > cls._stop(stored, rows):
                raise CoordError("stop_earlier names a time before the envelope's current stop (a later one is a "
                                 "widening, which needs a new signed envelope)")
        else:
            identities = {env.effect_identity(k, e) for k in env.EFFECT_LISTS for e in stored.body[k]}
            if value not in identities:
                raise CoordError("drop_effect names an effect identity the signed envelope holds")
            if ("drop_effect", value) in rows:
                return False
        conn.execute("INSERT INTO envelope_attenuations (envelope_id, thread_id, kind, value, by, gseq, at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (envelope_id, thread_id, kind, value, by, cls._next_gseq(conn, now), now))
        return True

    # --- reading -------------------------------------------------------------------------------------------------
    @staticmethod
    def _envelope(conn: sqlite3.Connection, envelope_id: str) -> Envelope | None:
        row = conn.execute("SELECT envelope_id, gseq, origin, drop_id, body, digest, payload, signature, signer_fp, "
                           "signer_class, supersedes, stop_at, accepted_at FROM envelopes WHERE envelope_id = ?",
                           (envelope_id,)).fetchone()
        if row is None:
            return None
        return Envelope(row[0], row[1], row[2], row[3], json.loads(row[4]), row[5], json.loads(row[6]), *row[7:])

    @classmethod
    def _head(cls, conn: sqlite3.Connection, thread_id: str) -> Envelope | None:
        """The thread's current envelope: the one most recently bound to it (a supersede CAS keeps that a chain)."""
        row = conn.execute("SELECT envelope_id FROM envelope_binds WHERE thread_id = ? ORDER BY gseq DESC LIMIT 1",
                           (thread_id,)).fetchone()
        return None if row is None else cls._envelope(conn, row[0])

    @staticmethod
    def _attenuations(conn: sqlite3.Connection, envelope_id: str, thread_id: str | None) -> list[tuple[str, str]]:
        """The narrowings that apply to ``envelope_id`` for ``thread_id``: envelope-wide ones and that thread's."""
        return [(k, v) for k, v in conn.execute(
            "SELECT kind, value FROM envelope_attenuations WHERE envelope_id = ? AND (thread_id IS NULL OR "
            "thread_id IS ?) ORDER BY gseq", (envelope_id, thread_id)).fetchall()]

    @staticmethod
    def _stop(stored: Envelope, rows: Sequence[tuple[str, str]]) -> Any:
        stops = [parse_stamp(stored.stop_at)] + [parse_stamp(v) for k, v in rows if k == "stop_earlier"]
        return min(stops)

    def envelope(self, envelope_id: str) -> Envelope:
        with read_transaction(self._connect) as conn:
            stored = self._envelope(conn, envelope_id)
        if stored is None:
            raise CoordError(f"no envelope {str(envelope_id)[:40]}")
        return stored

    def effective(self, envelope_id: str, thread_id: str) -> dict[str, Any]:
        """The signed body minus every dropped effect, with the earliest stop (C0 §4.4); empty effects and where when
        it is killed for the thread."""
        with read_transaction(self._connect) as conn:
            return self._effective(conn, envelope_id, thread_id)

    def _effective(self, conn: sqlite3.Connection, envelope_id: str, thread_id: str) -> dict[str, Any]:
        stored = self._envelope(conn, envelope_id)
        if stored is None:
            raise CoordError(f"no envelope {str(envelope_id)[:40]}")
        rows = self._attenuations(conn, envelope_id, thread_id)
        out = dict(stored.body)
        if any(k in ("kill", "thread_done") for k, _v in rows):
            return {**out, "where": [], **{k: [] for k in env.EFFECT_LISTS}, "stop_at": out["issued_at"]}
        dropped = {v for k, v in rows if k == "drop_effect"}
        for k in env.EFFECT_LISTS:
            out[k] = [e for e in stored.body[k] if env.effect_identity(k, e) not in dropped]
        out["stop_at"] = utc_stamp(self._stop(stored, rows))
        return out

    # --- liveness and the ceiling (C0 §4.5) ----------------------------------------------------------------------
    def live_refusal(self, envelope_id: str, thread_id: str, *, authority: AuthoritySnapshot,
                     new_pin: bool = False) -> str | None:
        """Why ``envelope_id`` is not live for ``thread_id`` now; None when it is. ``new_pin`` also requires it to be
        the thread's current head (a launch pinning it now)."""
        with read_transaction(self._connect) as conn:
            return self._live_refusal(conn, self._stamp(), envelope_id, thread_id, authority, new_pin)

    def _live_refusal(self, conn: sqlite3.Connection, now: str, envelope_id: str, thread_id: str,
                      authority: AuthoritySnapshot, new_pin: bool) -> str | None:
        stored = self._envelope(conn, envelope_id)
        if stored is None:
            return f"no envelope {str(envelope_id)[:40]}"
        row = conn.execute("SELECT rev FROM envelope_binds WHERE envelope_id = ? AND thread_id = ?",
                           (envelope_id, thread_id)).fetchone()
        if row is None:
            return f"envelope {envelope_id} is not bound to thread {str(thread_id)[:40]}"
        if self._thread(conn, thread_id).rev != row[0]:
            return f"thread {thread_id} moved past the revision envelope {envelope_id} was signed for"
        if new_pin:
            head = self._head(conn, thread_id)
            if head is None or head.envelope_id != envelope_id:
                return f"envelope {envelope_id} is superseded for thread {thread_id}"
        rows = self._attenuations(conn, envelope_id, thread_id)
        if any(k == "kill" for k, _v in rows):
            return f"envelope {envelope_id} was killed"
        if any(k == "thread_done" for k, _v in rows):
            return f"thread {thread_id} is done, which ended envelope {envelope_id}"
        if parse_stamp(now) >= self._stop(stored, rows):
            return f"envelope {envelope_id} stopped"
        refused = env.refusal(stored.payload, stored.signature, authority, stored.body, ssh_keygen=self._ssh_keygen)
        if refused is not None:
            return f"envelope {envelope_id}'s signature no longer holds: {refused}"
        return None

    def _chain(self, conn: sqlite3.Connection, envelope_id: str, thread_id: str) -> list[str]:
        """``envelope_id`` and every successor in its chain for ``thread_id``, oldest first. Single-parent: the
        supersede CAS refuses a fork."""
        chain = [envelope_id]
        while True:
            row = conn.execute(
                "SELECT e.envelope_id FROM envelopes e JOIN envelope_binds b ON b.envelope_id = e.envelope_id "
                "WHERE e.supersedes = ? AND b.thread_id = ? ORDER BY b.gseq LIMIT 1",
                (chain[-1], thread_id)).fetchone()
            if row is None:
                return chain
            chain.append(row[0])

    def ceiling_refusal(self, envelope_id: str, thread_id: str, *, effect: tuple[str, Mapping[str, Any]] | None = None,
                        scope: tuple[str, Sequence[str]] | None = None) -> str | None:
        """Why the cumulative ceiling of ``envelope_id`` for ``thread_id`` (it and every successor since, each by its
        effective envelope: a narrowing counts for good) does not hold ``effect`` (``(list, object)``, matched by its
        target) or ``scope`` (``(repo, paths)``); None when it does. Liveness is :meth:`chain_live_refusal`'s question."""
        with read_transaction(self._connect) as conn:
            return self._ceiling_refusal(conn, envelope_id, thread_id, effect, scope)

    def _ceiling_refusal(self, conn: sqlite3.Connection, envelope_id: str, thread_id: str,
                         effect: tuple[str, Mapping[str, Any]] | None,
                         scope: tuple[str, Sequence[str]] | None) -> str | None:
        key = None
        if effect is not None:
            try:
                kind, obj = effect
                key = env.effect_key(kind, obj)
                if not all(isinstance(k, str) for k in key):
                    raise TypeError
            except (env.EnvelopeError, KeyError, TypeError, ValueError):
                return "the effect asked for is not a land, push or publish this reader knows"
        if scope is not None:
            try:
                repo, paths = scope
                if isinstance(paths, str):
                    raise TypeError
                paths = tuple(paths)  # an iterator would be spent by the check below and pass the loop vacuously
                if not isinstance(repo, str) or not all(isinstance(p, str) for p in paths):
                    raise TypeError
            except (TypeError, ValueError):
                return "the scope asked for is not (repo, [paths])"
        for eid in self._chain(conn, envelope_id, thread_id):
            eff = self._effective(conn, eid, thread_id)
            if key is not None and key not in {env.effect_key(kind, e) for e in eff[kind]}:
                return f"envelope {eid} does not hold that {kind[:-1]}"
            if scope is not None:
                allowed = {w["repo"]: tuple(w["paths"]) for w in eff["where"]}
                if repo not in allowed:
                    return f"envelope {eid} does not reach {str(repo)[:120]}"
                for p in paths:
                    if not env.path_within(p, allowed[repo]):
                        return f"envelope {eid} does not reach {str(p)[:120]} in {repo[:120]}"
        return None

    def envelope_refused(self, drop_id: str) -> str | None:
        """Why the envelope a drop carried was not taken (its delivered drop receipt's line); None when it was taken
        or the drop carried none."""
        for r in self.drop_receipts(drop_id):
            if r.body.get("stage") == "delivered" and "envelope_refused" in r.body:
                return r.body["envelope_refused"]
        return None

    def chain_live_refusal(self, envelope_id: str, thread_id: str, *, authority: AuthoritySnapshot) -> str | None:
        """Why a job bound to ``envelope_id`` may not continue for ``thread_id``: ``envelope_id`` itself not live, or the
        current head of its supersede chain not live (C0 §4.5: both, plus the ceiling); None when both are. One read
        transaction and one clock (:meth:`_chain_live_refusal` is the entry an authorising write calls on its own)."""
        with read_transaction(self._connect) as conn:
            return self._chain_live_refusal(conn, self._stamp(), envelope_id, thread_id, authority)

    def _chain_live_refusal(self, conn: sqlite3.Connection, now: str, envelope_id: str, thread_id: str,
                            authority: AuthoritySnapshot) -> str | None:
        refused = self._live_refusal(conn, now, envelope_id, thread_id, authority, False)
        if refused is not None:
            return refused
        head = self._chain(conn, envelope_id, thread_id)[-1]
        if head == envelope_id:
            return None
        refused = self._live_refusal(conn, now, head, thread_id, authority, False)
        return None if refused is None else f"the chain's current envelope is not live: {refused}"

    def ceiling_criteria(self, envelope_id: str, thread_id: str, kind: str,
                         effect: Mapping[str, Any]) -> list[list[dict[str, Any]]] | None:
        """Every criteria list in the chain that targets ``effect``'s key, exactly one per chain member, all of which
        must hold together (a supersede that edits criteria narrows by conjunction); None when some member does not
        hold it. One read transaction."""
        with read_transaction(self._connect) as conn:
            if self._ceiling_refusal(conn, envelope_id, thread_id, (kind, effect), None) is not None:
                return None
            key = env.effect_key(kind, effect)
            out = []
            for eid in self._chain(conn, envelope_id, thread_id):
                match = [e for e in self._effective(conn, eid, thread_id)[kind] if env.effect_key(kind, e) == key]
                if len(match) != 1:
                    return None
                out.append(match[0].get("criteria", []))
            return out

    def authority_refusal(self, envelope_id: str, thread_id: str, *, authority: AuthoritySnapshot | None = None,
                          effect: tuple[str, Mapping[str, Any]] | None = None,
                          scope: tuple[str, Sequence[str]] | None = None) -> str:
        """What every caller that would act on an envelope reads today: :data:`NOT_IN_FORCE`. Before S1 no row in this
        file is authority (C0 §2), so this never returns None; S1's cutover replaces it with live + ceiling, read in the
        transaction of the write it authorises (C0 §5): :meth:`chain_live_refusal`, then :meth:`ceiling_refusal` and
        :meth:`ceiling_criteria`. The budget is a gauge, never part of the ceiling (T6, *"the thread budget is a
        GAUGE"*), so a successor's smaller budget does not narrow a running job here; its meter is C6's."""
        return NOT_IN_FORCE

    def head(self, thread_id: str) -> Envelope | None:
        """The thread's current envelope (the one a new echo for it supersedes), or None."""
        with read_transaction(self._connect) as conn:
            return self._head(conn, thread_id)

    def store_id(self) -> str:
        """This dispatch store's random id, which every envelope body and signature names."""
        with read_transaction(self._connect) as conn:
            return self._jobs._store_id(conn)

    def threads_of_envelope(self, envelope_id: str) -> list[str]:
        """The threads ``envelope_id`` is bound to, in bind order."""
        with read_transaction(self._connect) as conn:
            return [r[0] for r in conn.execute(
                "SELECT thread_id FROM envelope_binds WHERE envelope_id = ? ORDER BY gseq", (envelope_id,)).fetchall()]

    def envelopes_of(self, thread_id: str) -> list[Envelope]:
        with read_transaction(self._connect) as conn:
            return [self._envelope(conn, r[0]) for r in conn.execute(
                "SELECT envelope_id FROM envelope_binds WHERE thread_id = ? ORDER BY gseq", (thread_id,)).fetchall()]
