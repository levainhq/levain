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
from levain.coordinator.envelope_draft import ENVELOPE_DRAFT_SCHEMA
from levain.coordinator.store import (
    SCHEMA, CoordError, CoordStore, Drop, Source, _append_only, _in, members_sha256, next_gseq,
)
from levain.dispatch.adopt import GitFault, branch_problem
from levain.dispatch.contract import MAX_LIFETIME, git_env

__all__ = ["ATTENUATIONS", "ENVELOPE_SCHEMA", "NARROW_KEYS", "NOT_IN_FORCE", "Envelope", "EnvelopeConflict",
           "EnvelopeStale", "EnvelopeStore", "SignBasis", "ThreadBasis", "narrow_scope", "seen_past"]

ATTENUATIONS = ("kill", "stop_earlier", "drop_effect", "thread_done")
NOT_IN_FORCE = ("before S1 places the control principal the coordinator grants nothing (C0 §2): this effect is a "
                "per-call SIGN")
# the queue's skew for a signature's signed_at against the clock that receives it
SIGN_SKEW: timedelta = RULING_SKEW

ENVELOPE_SCHEMA: tuple[str, ...] = (
    # the gseq table, one store-wide order for every authority row, is the base SCHEMA's (C0 §3.4)
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
    *_append_only("envelopes", "an envelope"),
    *_append_only("envelope_binds", "an envelope bind"),
    *_append_only("envelope_attenuations", "an attenuation"),
    # INSERT OR REPLACE deletes the old row without firing a delete trigger: refuse a new row reusing any key
    "CREATE TRIGGER IF NOT EXISTS envelopes_no_replace BEFORE INSERT ON envelopes WHEN EXISTS (SELECT 1 FROM "
    "envelopes WHERE envelope_id = NEW.envelope_id OR gseq = NEW.gseq OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'an envelope is never replaced'); END",
    "CREATE TRIGGER IF NOT EXISTS envelope_binds_no_replace BEFORE INSERT ON envelope_binds WHEN EXISTS (SELECT 1 "
    "FROM envelope_binds WHERE (envelope_id = NEW.envelope_id AND thread_id = NEW.thread_id) OR gseq = NEW.gseq "
    "OR rowid = NEW.rowid) BEGIN SELECT RAISE(ABORT, 'a bind is never replaced'); END",
    "CREATE TRIGGER IF NOT EXISTS envelope_attenuations_no_replace BEFORE INSERT ON envelope_attenuations WHEN "
    "EXISTS (SELECT 1 FROM envelope_attenuations WHERE gseq = NEW.gseq OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'an attenuation is never replaced'); END",
)


NARROW_KEYS = ("where", *env.EFFECT_LISTS)


def _high_water(conn: sqlite3.Connection) -> int:
    """The store-wide sequence high-water: what an echo records as ``supersedes.seen``."""
    return conn.execute("SELECT COALESCE(MAX(gseq), 0) FROM gseq").fetchone()[0]


def seen_past(body: Mapping[str, Any], high: int) -> str | None:
    """Why a superseding body's ``seen`` cannot be honest, or None. An echo writes ``seen`` from this store's own
    high-water, which only grows, so a value above the high-water now was never written by one (L3 h11 r1 codex
    HIGH: a horizon past every event would let a later kill pass the narrowed-after check)."""
    sup = body["supersedes"]
    if sup is None or sup["seen"] <= high:
        return None
    return (f"this body says it was composed as of store event {sup['seen']}, but this store's last event is {high}: "
            "no echo from this store wrote it; echo again")


@dataclass(frozen=True)
class ThreadBasis:
    """One bound thread as a superseding body is read against it: the head as narrowed for the thread, the row that
    ended it there (``(kind, by, at)``) or None, the revision the head was bound at, and the member drops the thread
    gained and lost between that revision and the body's."""
    effective: dict[str, Any]
    end: tuple[str, str, str] | None
    from_rev: int | None
    added: list[str]
    removed: list[str]


@dataclass(frozen=True)
class SignBasis:
    """Everything a view of a body read before a signature, from ONE read transaction (:meth:`EnvelopeStore.sign_basis`):
    the store's high-water, the envelope already stored under the body's id, the head it supersedes as stored, each
    bound thread's :class:`ThreadBasis`, and whether a narrowing of the head newer than the body's ``seen`` applies
    to a bound thread."""
    high: int
    stored: Envelope | None
    head: Envelope | None
    threads: dict[str, ThreadBasis]
    narrowed: bool


def _effect_target(kind: str, effect: Mapping[str, Any]) -> str:
    """The one name a role's ceiling string matches an effect by: a land's or push's repo, a publish's package."""
    return effect["package"] if kind == "publishes" else effect["repo"]


def _narrow_paths(have: Sequence[str], allowed: Sequence[str]) -> list[str]:
    """The paths both lists cover, each as the narrower of the two that meet. A ceiling path that is not a signed-body
    path shape is skipped, which can only narrow."""
    out: list[str] = []
    for c in allowed:
        try:
            if env._rel_path(c, "a ceiling path") != c:
                continue
        except env.EnvelopeError:
            continue
        for p in have:
            pick = c if env.path_within(c, (p,)) else p if env.path_within(p, (c,)) else None
            if pick is not None and pick not in out:
                out.append(pick)
    return out


def narrow_scope(scope: Mapping[str, Any], ceiling: Mapping[str, Any]) -> dict[str, Any]:
    """``scope`` (an effective envelope: its ``where`` and its ``lands`` / ``pushes`` / ``publishes``) cut down to a
    role's ceiling, key by key, and never widened (a pure function; it reads no store and grants nothing).

    - ``where``: a ceiling that names it keeps only the repos it names, each with the paths both cover; a ceiling that
      does not name it leaves NO ``where`` (an absent key grants nothing, as it does for the effects below).
    - ``lands`` / ``pushes`` / ``publishes``: an irreversible effect stays only when the ceiling names its repo (its
      package, for a publish); a list the ceiling does not name is emptied, so a role that never mentions an effect is
      never granted it.
    - any other key of ``scope`` passes through, and a ceiling key outside :data:`NARROW_KEYS` is refused.

    The result is checked against ``scope`` before it is returned: whatever a future edit does above, a narrowing that
    held a repo, a path or an effect the envelope lacks is a :class:`CoordError`, not a wider grant."""
    unknown = set(ceiling) - set(NARROW_KEYS)
    if unknown:
        raise CoordError(f"a role ceiling names {sorted(unknown)[0]!r}, which is not one of {list(NARROW_KEYS)}")
    out = {k: v for k, v in scope.items()}
    by_repo = {w["repo"]: list(w["paths"]) for w in ceiling.get("where", ())}
    where = []
    for w in scope.get("where", []):
        if w["repo"] not in by_repo:
            continue
        paths = _narrow_paths(w["paths"], by_repo[w["repo"]])
        if paths:
            where.append({"repo": w["repo"], "paths": paths})
    out["where"] = where
    if env.LAUNCH_LIST in out:
        out[env.LAUNCH_LIST] = []     # a role's scope never inherits the operator's launch grant
    for kind in env.EFFECT_LISTS:
        named = set(ceiling.get(kind, ()))
        out[kind] = [e for e in scope.get(kind, []) if _effect_target(kind, e) in named]
    _assert_not_wider(scope, out)
    return out


def _assert_not_wider(scope: Mapping[str, Any], out: Mapping[str, Any]) -> None:
    held = {w["repo"]: tuple(w["paths"]) for w in scope.get("where", [])}
    for w in out.get("where", []):
        if w["repo"] not in held or any(not env.path_within(p, held[w["repo"]]) for p in w["paths"]):
            raise CoordError(f"a narrowing named {w['repo'][:120]!r} beyond the envelope: refused")
    for kind in env.EFFECT_LISTS:
        have = [env.effect_identity(kind, e) for e in scope.get(kind, [])]
        if any(env.effect_identity(kind, e) not in have for e in out.get(kind, [])):
            raise CoordError(f"a narrowing held a {kind} effect the envelope lacks: refused")


class EnvelopeConflict(CoordError):
    """An ``envelope_id`` already stored with a different body, payload or signature: never an overwrite."""


class EnvelopeStale(CoordError):
    """An envelope signed more than :data:`SIGN_SKEW` from the store's clock: signing it again is the remedy."""


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
                            env=git_env(), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0:
        return None
    return os.path.realpath(cp.stdout.decode("utf-8", "surrogateescape").strip())


class EnvelopeStore(CoordStore):
    """The coordinator store with envelopes (C2). Every check that reads the disk (repo realpaths) runs before the
    write lock; everything that reads the store runs inside one ``BEGIN IMMEDIATE`` with the clock read under it."""

    # the draft tables sit here, never in the base SCHEMA: a read-only surface's currency check asks only for the base,
    # so a store made before drafts stays readable until a writer brings it current
    _SCHEMA = SCHEMA + ENVELOPE_SCHEMA + ENVELOPE_DRAFT_SCHEMA

    def __init__(self, directory: Any = None, *, ssh_keygen: Any = None, **kw: Any) -> None:
        super().__init__(directory, **kw)
        self._ssh_keygen = ssh_keygen

    # --- acceptance ----------------------------------------------------------------------------------------------
    def check_body(self, body_text: Any) -> dict[str, Any]:
        """The body parsed and held to every check that reads the disk (each where repo a git work tree's top by its
        real path, each land's branch by F11's branch_problem), with no signature: what echo runs before writing a
        file and sign runs before asking, so a body the store can only refuse is never signed. Raises CoordError."""
        body = self._parsed(body_text)
        if body["supersedes"] is not None:
            with read_transaction(self._connect) as conn:
                forged = seen_past(body, _high_water(conn))
            if forged is not None:
                raise CoordError(forged)
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
        return next_gseq(conn, now)

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
            raise EnvelopeStale(f"the envelope was signed at {payload['signed_at']}, more than "
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

    @staticmethod
    def _narrowed_after(conn: sqlite3.Connection, envelope_id: str, thread_id: str, seen: int) -> bool:
        """True when a narrowing that applies to ``envelope_id`` for ``thread_id`` (the predicate of
        :meth:`_attenuations`) was written after store point ``seen``. The one check every superseding body meets, in
        :meth:`_bind_checked`, drafted or composed at a terminal."""
        return conn.execute("SELECT 1 FROM envelope_attenuations WHERE envelope_id = ? AND (thread_id IS NULL OR "
                            "thread_id IS ?) AND gseq > ?", (envelope_id, thread_id, seen)).fetchone() is not None

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
            elif cls._narrowed_after(conn, head.envelope_id, thread_id, sup["seen"]):
                # the signature covers the head AS NARROWED up to store point `seen`; a narrowing since is another head
                raise EnvelopeStale(f"envelope {head.envelope_id} was narrowed after this body was shown; echo again")
        for thread_id, rev, _msha in binds:
            conn.execute("INSERT INTO envelope_binds (envelope_id, thread_id, rev, gseq, at) VALUES (?, ?, ?, ?, ?)",
                         (envelope_id, thread_id, rev, cls._next_gseq(conn, now), now))

    def accept_envelope(self, body_text: str, payload: Mapping[str, Any], signature: str, *,
                        authority: AuthoritySnapshot) -> tuple[Envelope, bool]:
        """Accept a WORDED envelope (the coordinator's echo, signed by you) bound to the thread revisions it names;
        ``(envelope, True)``. The identical worded record again writes nothing: ``(stored, False)``, looked up before
        the signature and disk checks, so a retry after a lost answer returns what was accepted even when its signer or
        a branch has since gone (in force is decided at use, never by this record)."""
        try:
            with read_transaction(self._connect) as conn:
                again = self._same_or_conflict(conn, self._parsed(body_text), payload, signature)
        except (CoordError, TypeError, ValueError):
            again = None          # not this exact record: the full checks below give the refusal
        if again is not None and again.origin == "worded":
            return again, False
        try:
            parsed = self._parsed(body_text)
        except CoordError:
            parsed = None          # _checked below gives the refusal
        drafted = parsed is not None and parsed["draft"] is not None
        if drafted:
            self._draft_preflight(parsed)
        body, signer_class = self._checked(body_text, payload, signature, authority)
        if body["origin"] != "worded":
            raise CoordError("a structured envelope arrives with its drop (accept_drop), never later")
        if drafted:
            self._draft_rederive(body)
        with self._write() as (conn, now):
            stored = self._same_or_conflict(conn, body, payload, signature)
            if stored is not None:
                return stored, False
            forged = seen_past(body, _high_water(conn))          # before this write takes a sequence number of its own
            if forged is not None:
                raise CoordError(forged)
            if drafted:
                self._draft_admit(conn, now, body)
            drop = self._must_drop(conn, body["drop_id"])
            self._admit(conn, now, body, payload, drop)
            binds = [(self._must_thread(conn, b["thread_id"]).thread_id, b["rev"], b["members_sha256"])
                     for b in body["binds"]]
            self._insert(conn, now, body, payload, signature, signer_class)
            self._bind_checked(conn, now, body["envelope_id"], body, binds)
            return self._envelope(conn, body["envelope_id"]), True

    # --- a drafted body's checks (H1-1): the rows are read in envelope_drafts, the one home of a draft's state ----------
    def _draft_preflight(self, body: Mapping[str, Any]) -> None:
        """A read transaction before any signature check: the named revision is the series' latest, unsigned (or
        signed by this very envelope, which the write's conflict check words), unwithdrawn, not lapsed, complete, and
        its stored digest is the one the body names. :class:`DraftStale` otherwise. Not authoritative: the write
        transaction runs it again (:meth:`_draft_admit`)."""
        from levain.coordinator import envelope_drafts as ed

        with read_transaction(self._connect) as conn:
            ed.admit_check(conn, self._stamp(), body, cites=False)

    def _draft_rederive(self, body: Mapping[str, Any]) -> None:
        """Outside the lock: the stored row's digest is its own record's, and the body is byte for byte the stored
        proposal and its draft reference (a hand-edited file that names a real revision is refused here)."""
        from levain.coordinator import envelope_drafts as ed

        with read_transaction(self._connect) as conn:
            ed.rederive_check(conn, body)

    def _draft_admit(self, conn: sqlite3.Connection, now: str, body: Mapping[str, Any]) -> None:
        """:meth:`_draft_preflight` again under the write lock, and every cite re-checked against its record."""
        from levain.coordinator import envelope_drafts as ed

        ed.admit_check(conn, now, body, cites=True)

    def accept_drop(self, words: str, *, surface: str, outbox_id: str | None = None, sent_at: str | None = None,
                    source: Source = Source(), envelope: tuple[str, Mapping[str, Any], str] | None = None,
                    authority: AuthoritySnapshot | None = None, keep_plain: bool = True) -> tuple[Drop, bool]:
        """A drop, optionally carrying a STRUCTURED envelope signed at send (T4): ``envelope = (body_text, payload,
        signature)``, whose body names the drop id. Drop and envelope are written in one transaction. An envelope that
        fails any check never costs the words: the drop is stored plain and its delivered drop receipt says why the
        envelope was not taken (:meth:`envelope_refused`). A resend of the same ``outbox_id`` returns the stored drop
        as it was (C1's outbox rule), so "sign it again" for a stored drop is the coordinator's WORDED echo over its
        thread [seat: a structured envelope is only ever written in the drop's insert, C0 §3.1].

        ``keep_plain=False`` (the phone port, whose outbox still holds the words): an envelope that is not taken writes
        NOTHING and raises instead, so a drop that carries a signature is never stored without it. The refusal is the
        check's own: :class:`EnvelopeStale` for a signature past the skew, :class:`EnvelopeConflict` for a drop or
        envelope id already stored, :class:`~levain.authority.VerifierUnavailable` for a verifier that did not answer,
        any other :class:`CoordError` otherwise. A resend of a stored ``outbox_id`` still returns the stored drop."""
        if envelope is None:
            return super().accept_drop(words, surface=surface, outbox_id=outbox_id, sent_at=sent_at, source=source)
        if not isinstance(authority, AuthoritySnapshot):
            raise CoordError("a drop carrying an envelope needs the authority snapshot to verify it against")
        if not keep_plain and isinstance(outbox_id, str):
            # a resend before any check of its carrier: the strict refusals below must never turn a delivered drop
            # into a refusal (the default path gets this from the outbox lookup, which runs before its carry)
            stored = self.resent_drop(outbox_id, words, surface=surface)
            if stored is not None:
                return stored, False
        refused: str | None = None
        body: dict[str, Any] | None = None
        try:
            body_text, payload, signature = envelope
            body, signer_class = self._checked(body_text, payload, signature, authority)
            if body["origin"] != "structured":
                raise CoordError("a drop carries only a structured envelope; a worded one is the coordinator's echo")
        except (CoordError, ValueError, TypeError) as exc:
            if not keep_plain:
                if isinstance(exc, CoordError):
                    raise
                raise CoordError(f"an envelope this store cannot read: {type(exc).__name__}") from None
            refused, body = str(exc)[:300], None
        except VerifierUnavailable as exc:
            if not keep_plain:
                raise
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
                if not keep_plain:
                    raise          # rolls the transaction back: the drop is not written without its envelope
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
            identities = {env.effect_identity(k, e) for k in env.GRANT_LISTS for e in stored.body.get(k, [])}
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

    @classmethod
    def _effective(cls, conn: sqlite3.Connection, envelope_id: str, thread_id: str) -> dict[str, Any]:
        """:meth:`effective` on the caller's connection (a class method: the spawn request reads it inside its own
        write transaction, wherever the store object is)."""
        stored = cls._envelope(conn, envelope_id)
        if stored is None:
            raise CoordError(f"no envelope {str(envelope_id)[:40]}")
        rows = cls._attenuations(conn, envelope_id, thread_id)
        out = dict(stored.body)
        if any(k in ("kill", "thread_done") for k, _v in rows):
            killed = {**out, "where": [], **{k: [] for k in env.EFFECT_LISTS}, "stop_at": out["issued_at"]}
            if env.LAUNCH_LIST in out:
                killed[env.LAUNCH_LIST] = []
            return killed
        dropped = {v for k, v in rows if k == "drop_effect"}
        for k in env.EFFECT_LISTS:
            out[k] = [e for e in stored.body[k] if env.effect_identity(k, e) not in dropped]
        if env.LAUNCH_LIST in out:
            out[env.LAUNCH_LIST] = [e for e in out[env.LAUNCH_LIST]
                                    if env.effect_identity(env.LAUNCH_LIST, e) not in dropped]
        out["stop_at"] = utc_stamp(cls._stop(stored, rows))
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

    def end_of(self, envelope_id: str, thread_id: str | None) -> tuple[str, str, str] | None:
        """The row that ended ``envelope_id`` for ``thread_id`` (a kill, envelope-wide or for that thread, or its thread
        going done or closed) as ``(kind, by, at)``, the first one written; None while none has."""
        with read_transaction(self._connect) as conn:
            return self._end_of(conn, envelope_id, thread_id)

    @staticmethod
    def _end_of(conn: sqlite3.Connection, envelope_id: str, thread_id: str | None) -> tuple[str, str, str] | None:
        row = conn.execute(
            "SELECT kind, by, at FROM envelope_attenuations WHERE envelope_id = ? AND kind IN ('kill', "
            "'thread_done') AND (thread_id IS NULL OR thread_id IS ?) ORDER BY gseq LIMIT 1",
            (envelope_id, thread_id)).fetchone()
        return None if row is None else (row[0], row[1], row[2])

    def sign_basis(self, body: Mapping[str, Any]) -> SignBasis:
        """What a view of ``body`` before a signature reads, in ONE read transaction (L3 h11 r1 codex MED: views built
        from separate reads rendered a base a concurrent kill had already replaced): the high-water first, so every
        later read in the block is of the store at that point; then the envelope stored under the body's id, the head
        it supersedes and, per bound thread, the head as narrowed, its end, its bound revision, the member diff and
        the narrowed-after-``seen`` predicate acceptance runs. A body that supersedes nothing reads the high-water and
        the stored id only."""
        sup = body["supersedes"]
        threads: dict[str, ThreadBasis] = {}
        narrowed = False
        with read_transaction(self._connect) as conn:
            high = _high_water(conn)
            stored = self._envelope(conn, body["envelope_id"])
            head = None if sup is None else self._envelope(conn, sup["envelope_id"])
            for b in body["binds"] if head is not None else ():
                t = b["thread_id"]
                row = conn.execute("SELECT rev FROM envelope_binds WHERE envelope_id = ? AND thread_id = ?",
                                   (head.envelope_id, t)).fetchone()
                from_rev = None if row is None else row[0]
                added, removed = ([], []) if from_rev is None or from_rev == b["rev"] else \
                    self.members_diff(conn, t, from_rev, b["rev"])
                threads[t] = ThreadBasis(self._effective(conn, head.envelope_id, t),
                                         self._end_of(conn, head.envelope_id, t), from_rev, added, removed)
                narrowed = narrowed or self._narrowed_after(conn, head.envelope_id, t, sup["seen"])
        return SignBasis(high, stored, head, threads, narrowed)

    def basis(self, thread_ids: Sequence[str]) -> tuple[Envelope | None, dict[str, dict[str, Any]], int]:
        """What a body composed now supersedes, read in ONE transaction: the head the threads share, its effective
        envelope per thread, and ``seen``, the store-wide sequence high-water a superseding body records
        (``supersedes.seen``). One transaction matters: a ``seen`` read later could cover a narrowing the view never
        showed. ``(None, {}, seen)`` when the threads have no envelope."""
        with read_transaction(self._connect) as conn:
            heads = [self._head(conn, t) for t in thread_ids]
            first = heads[0] if heads else None
            if any((h is None) != (first is None) or (h is not None and h.envelope_id != first.envelope_id)
                   for h in heads):
                raise CoordError("these threads do not share one current envelope; echo them separately")
            seen = _high_water(conn)
            eff = {} if first is None else {t: self._effective(conn, first.envelope_id, t) for t in thread_ids}
        return first, eff, seen

    @staticmethod
    def members_diff(conn: sqlite3.Connection, thread_id: str, from_rev: int, to_rev: int) -> tuple[list[str], list[str]]:
        """``(added, removed)``: the drop ids a thread's revision ``to_rev`` holds and ``from_rev`` did not, and the
        reverse, read from its append-only revisions."""
        def members(rev: int) -> list[str]:
            row = conn.execute("SELECT members FROM thread_revisions WHERE thread_id = ? AND rev = ?",
                               (thread_id, rev)).fetchone()
            return [] if row is None else list(json.loads(row[0]))

        before, after = members(from_rev), members(to_rev)
        return [d for d in after if d not in before], [d for d in before if d not in after]

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
