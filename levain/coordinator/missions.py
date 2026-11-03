"""levain.coordinator.missions: the mission record, the acceptance snapshot and the ACK rule (CH-H2, COORD C4).

His unit is a whole mission, not a thread (frame ``one_cockpit_workflow_1023`` §3.3). A MISSION groups the work threads a
drop produced (or a later attach added) under its own numbered revision, like a thread's membership. Its ACCEPTANCE signs
a frozen snapshot of the complete current state: the member threads at their revisions, each one's terminal event, its
envelope chain head, the review watermark, the un-ACKed KNOWs and a digest of what the workers said. The close compares
the COMPLETE current state to the snapshot the signature covers (compare-and-swap): a child attached after render moves
the mission revision, a KNOW or an attenuation after render moves the watermark, a reopened child moves its terminal
event, and any of them refuses the close so the card re-renders. A child may still close alone; the acceptance is the
signed statement that the whole thing is done.

The ACK rule (Q2 (c), ruled 10-23): SIGN and ANSWER block their own leg already; a KNOW never stops work and gates
ACCEPTANCE only. An ACK is the clear of the KNOW's review item (:func:`levain.coordinator.review.clear_item`), so no
second record exists for it; this module only reads which KNOWs of a mission's children are still open.

[seat] choices the frame does not state: a mission holds work threads only (an ASK, NOTE or RELAY carries no acceptance,
frame §3.1); a thread belongs to at most one mission at a time; the acceptance record is signed by a verifier the caller
passes (the verb that files and checks the sign request is CH-DS / TERMSIGN's, not this module's), and that verifier's
answer is only ever a signer fingerprint; ``evidence_digests`` digest the worker-written replies (a KNOW or a
coordinator reply) of the children, sorted, so the card can say what the workers supplied and a signature never claims
the evidence is true.

Tables are append-only like the rest of the store. Acceptance is a state read, never a stored flag: ``signed`` while the
latest acceptance's snapshot equals the current one, ``stale`` once any part of it moved.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from levain.autonomic.durable_sqlite import read_transaction
from levain.contracts.canon import canonical, canonical_bytes
from levain.coordinator.store import CoordError, CoordStore, _append_only, _in

__all__ = ["MISSION_SCHEMA", "AcceptanceRefused", "Mission", "MissionAcceptance", "Snapshot", "accept", "accept_message",
           "attach", "create_mission", "detach", "get_mission", "list_missions", "snapshot_of", "unacked_know_items"]

_MISSION_ID = re.compile(r"^ms-[0-9a-f]{32}$")
_CAUSES = ("create", "attach", "detach")
_TERMINAL = ("done", "closed")
_EVIDENCE_KINDS = ("know", "coordinator_reply")

MISSION_SCHEMA: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS missions (mission_id TEXT PRIMARY KEY, created_at TEXT NOT NULL)",
    # the complete sorted member list per revision, and its hash: what an acceptance's snapshot binds
    "CREATE TABLE IF NOT EXISTS mission_revisions (mission_id TEXT NOT NULL REFERENCES missions(mission_id), "
    "rev INTEGER NOT NULL, children TEXT NOT NULL, children_sha256 TEXT NOT NULL, "
    f"cause TEXT NOT NULL CHECK ({_in('cause', _CAUSES)}), by TEXT NOT NULL CHECK (by IN ('operator', 'coordinator')), "
    "at TEXT NOT NULL, PRIMARY KEY (mission_id, rev))",
    # the sign request id and the signature's own digest are each unique: one signature accepts once, whatever id it
    # arrives under
    "CREATE TABLE IF NOT EXISTS mission_acceptances (acceptance_id TEXT PRIMARY KEY, "
    "mission_id TEXT NOT NULL REFERENCES missions(mission_id), mission_revision INTEGER NOT NULL, "
    "snapshot TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL, sign_request_id TEXT NOT NULL UNIQUE, "
    "signature_sha256 TEXT NOT NULL UNIQUE, signer_fp TEXT NOT NULL, at TEXT NOT NULL)",
    *_append_only("missions", "a mission"),
    *_append_only("mission_revisions", "a mission revision"),
    *_append_only("mission_acceptances", "a mission acceptance"),
    "CREATE TRIGGER IF NOT EXISTS missions_no_replace BEFORE INSERT ON missions WHEN EXISTS (SELECT 1 FROM missions "
    "WHERE mission_id = NEW.mission_id OR rowid = NEW.rowid) BEGIN SELECT RAISE(ABORT, 'a mission is never replaced'); END",
    "CREATE TRIGGER IF NOT EXISTS mission_revisions_next BEFORE INSERT ON mission_revisions WHEN NEW.rev != "
    "COALESCE((SELECT MAX(rev) FROM mission_revisions WHERE mission_id = NEW.mission_id), 0) + 1 OR EXISTS "
    "(SELECT 1 FROM mission_revisions WHERE rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a revision is the next rev of its mission'); END",
    "CREATE TRIGGER IF NOT EXISTS mission_acceptances_no_replace BEFORE INSERT ON mission_acceptances WHEN EXISTS "
    "(SELECT 1 FROM mission_acceptances WHERE acceptance_id = NEW.acceptance_id OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a mission acceptance is never replaced'); END",
)


class AcceptanceRefused(CoordError):
    """A refused close: ``why`` is ``stale`` (the state moved since the card rendered: re-render and sign again),
    ``unacked`` (KNOWs still open), ``not_ready`` (a child is not done), ``signature`` (the verifier refused) or
    ``replay`` (that signature already accepted)."""

    def __init__(self, why: str, message: str) -> None:
        super().__init__(message)
        self.why = why


@dataclass(frozen=True)
class Snapshot:
    """What an acceptance signs. ``body`` is the frame's record; ``sha256`` is over its canonical JSON."""
    body: dict[str, Any]
    sha256: str

    @property
    def ready(self) -> bool:
        return all(c["terminal_event_seq"] is not None for c in self.body["children"]) and bool(self.body["children"])

    @property
    def unacked_item_ids(self) -> list[str]:
        return list(self.body["unacked_item_ids"])


@dataclass(frozen=True)
class MissionAcceptance:
    acceptance_id: str
    mission_id: str
    mission_revision: int
    snapshot_sha256: str
    sign_request_id: str
    signer_fp: str
    at: str


@dataclass(frozen=True)
class Mission:
    mission_id: str
    created_at: str
    rev: int
    children: tuple[str, ...]
    children_sha256: str
    acceptance: str                        # not_ready | ready | signed | stale (frame §3.1, derived)
    accepted: MissionAcceptance | None
    unacked_item_ids: tuple[str, ...]


def _digest(body: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(body)).hexdigest()


def _children_sha(children: Sequence[str]) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(sorted(children))).hexdigest()


def accept_message(mission_id: str, snapshot: Snapshot) -> str:
    """What a signature over an acceptance covers: the mission, its revision and the snapshot's digest, so a signature
    over one state accepts no other (a card re-rendered after a KNOW or an attach is a new message)."""
    return (f"levain mission accept {mission_id} {snapshot.body['mission_revision']} {snapshot.sha256}")


def _has(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None


def _latest(conn: sqlite3.Connection, mission_id: str) -> tuple[int, list[str], str]:
    row = conn.execute("SELECT rev, children, children_sha256 FROM mission_revisions WHERE mission_id = ? "
                       "ORDER BY rev DESC LIMIT 1", (mission_id,)).fetchone()
    if row is None:
        raise CoordError(f"no mission {str(mission_id)[:40]!r}")
    return row[0], json.loads(row[1]), row[2]


def _must_mission(conn: sqlite3.Connection, mission_id: Any) -> None:
    if (not isinstance(mission_id, str) or _MISSION_ID.fullmatch(mission_id) is None
            or conn.execute("SELECT 1 FROM missions WHERE mission_id = ?", (mission_id,)).fetchone() is None):
        raise CoordError(f"no mission {str(mission_id)[:40]!r}")


def _marks(ids: Sequence[str]) -> str:
    return ",".join("?" * len(ids))


def _unacked(conn: sqlite3.Connection, children: Sequence[str]) -> list[str]:
    """The open KNOW review items filed to a child thread (an item is open until its latest event is a clear)."""
    if not children:
        return []
    return [r[0] for r in conn.execute(
        f"SELECT i.item_id FROM review_items i WHERE i.feed = 'know' AND i.thread_id IN ({_marks(children)}) AND "
        "COALESCE((SELECT e.op FROM review_events e WHERE e.item_id = i.item_id ORDER BY e.seq DESC LIMIT 1), "
        "'reopened') != 'cleared' ORDER BY i.rowid", tuple(children))]


def _watermark(conn: sqlite3.Connection, children: Sequence[str]) -> int:
    """The largest store-wide sequence number any record about a child carries: its replies, review items and events,
    and the attenuations that apply to it. A row written after a card rendered moves this past the card's value."""
    if not children:
        return 0
    marks, args = _marks(children), tuple(children)
    parts = [(f"SELECT COALESCE(MAX(gseq), 0) FROM replies WHERE thread_id IN ({marks})", args),
             (f"SELECT COALESCE(MAX(gseq), 0) FROM review_items WHERE thread_id IN ({marks})", args),
             (f"SELECT COALESCE(MAX(e.gseq), 0) FROM review_events e JOIN review_items i ON i.item_id = e.item_id "
              f"WHERE i.thread_id IN ({marks})", args)]
    if _has(conn, "envelope_attenuations"):
        # a row with no thread id is envelope-wide: it applies to every thread the envelope is bound to
        parts.append((f"SELECT COALESCE(MAX(gseq), 0) FROM envelope_attenuations WHERE thread_id IN ({marks}) OR "
                      f"(thread_id IS NULL AND envelope_id IN (SELECT envelope_id FROM envelope_binds WHERE thread_id "
                      f"IN ({marks})))", (*args, *args)))
    return max(conn.execute(sql, a).fetchone()[0] for sql, a in parts)


def _child(conn: sqlite3.Connection, thread_id: str) -> dict[str, Any]:
    rev = conn.execute("SELECT MAX(rev) FROM thread_revisions WHERE thread_id = ?", (thread_id,)).fetchone()[0]
    ev = conn.execute("SELECT seq, value FROM thread_events WHERE thread_id = ? AND field = 'state' "
                      "ORDER BY seq DESC LIMIT 1", (thread_id,)).fetchone()
    head = None
    if _has(conn, "envelope_binds"):
        row = conn.execute("SELECT envelope_id FROM envelope_binds WHERE thread_id = ? ORDER BY gseq DESC LIMIT 1",
                           (thread_id,)).fetchone()
        head = None if row is None else row[0]
    return {"thread_id": thread_id, "revision": rev,
            "terminal_event_seq": ev[0] if ev is not None and ev[1] in _TERMINAL else None,
            "envelope_chain_head": head}


def _evidence(conn: sqlite3.Connection, children: Sequence[str]) -> list[str]:
    if not children:
        return []
    return sorted("sha256:" + hashlib.sha256(r[0].encode("utf-8")).hexdigest() for r in conn.execute(
        f"SELECT body FROM replies WHERE kind IN ({_marks(_EVIDENCE_KINDS)}) AND thread_id IN ({_marks(children)})",
        (*_EVIDENCE_KINDS, *children)))


def _snapshot(conn: sqlite3.Connection, mission_id: str) -> Snapshot:
    rev, children, csha = _latest(conn, mission_id)
    body = {"mission_id": mission_id, "mission_revision": rev, "children_digest": csha,
            "review_watermark_gseq": _watermark(conn, children), "unacked_item_ids": _unacked(conn, children),
            "children": [_child(conn, t) for t in children], "evidence_digests": _evidence(conn, children)}
    return Snapshot(body, _digest(body))


def snapshot_of(store: CoordStore, mission_id: str) -> Snapshot:
    """The mission's complete current state as an acceptance would sign it now."""
    with read_transaction(store._connect) as conn:
        _must_mission(conn, mission_id)
        return _snapshot(conn, mission_id)


def unacked_know_items(store: CoordStore, mission_id: str) -> list[str]:
    """The ids of the open KNOW review items of the mission's children: clear one (``review.clear_item``) to ACK it."""
    return snapshot_of(store, mission_id).unacked_item_ids


def _view(conn: sqlite3.Connection, mission_id: str) -> Mission:
    created = conn.execute("SELECT created_at FROM missions WHERE mission_id = ?", (mission_id,)).fetchone()[0]
    rev, children, csha = _latest(conn, mission_id)
    snap = _snapshot(conn, mission_id)
    row = conn.execute("SELECT acceptance_id, mission_revision, snapshot_sha256, sign_request_id, signer_fp, at FROM "
                       "mission_acceptances WHERE mission_id = ? ORDER BY rowid DESC LIMIT 1", (mission_id,)).fetchone()
    accepted = None if row is None else MissionAcceptance(row[0], mission_id, row[1], row[2], row[3], row[4], row[5])
    if accepted is not None:
        state = "signed" if accepted.snapshot_sha256 == snap.sha256 else "stale"
    else:
        state = "ready" if snap.ready and not snap.unacked_item_ids else "not_ready"
    return Mission(mission_id, created, rev, tuple(children), csha, state, accepted, tuple(snap.unacked_item_ids))


def get_mission(store: CoordStore, mission_id: str) -> Mission:
    with read_transaction(store._connect) as conn:
        _must_mission(conn, mission_id)
        return _view(conn, mission_id)


def list_missions(store: CoordStore) -> list[Mission]:
    """Every mission, newest first."""
    with read_transaction(store._connect) as conn:
        ids = [r[0] for r in conn.execute("SELECT mission_id FROM missions ORDER BY rowid DESC")]
        return [_view(conn, i) for i in ids]


def _revise(conn: sqlite3.Connection, mission_id: str, children: Sequence[str], cause: str, by: str, now: str) -> None:
    ordered = sorted(children)
    conn.execute("INSERT INTO mission_revisions (mission_id, rev, children, children_sha256, cause, by, at) VALUES "
                 "(?, COALESCE((SELECT MAX(rev) FROM mission_revisions WHERE mission_id = ?), 0) + 1, ?, ?, ?, ?, ?)",
                 (mission_id, mission_id, json.dumps(ordered), _children_sha(ordered), cause, by, now))


def _member_check(conn: sqlite3.Connection, thread_ids: Sequence[str], *, mission_id: str | None) -> None:
    for t in thread_ids:
        row = conn.execute("SELECT 1 FROM threads WHERE thread_id = ?", (t,)).fetchone() if isinstance(t, str) else None
        if row is None:
            raise CoordError(f"no thread {str(t)[:40]!r}")
        kind = conn.execute("SELECT value FROM thread_events WHERE thread_id = ? AND field = 'kind' "
                            "ORDER BY seq DESC LIMIT 1", (t,)).fetchone()
        if kind is None or kind[0] != "work":
            raise CoordError(f"thread {t} is not a work thread: only work carries an acceptance")
        for (mid,) in conn.execute("SELECT mission_id FROM missions ORDER BY rowid"):
            if mid != mission_id and t in _latest(conn, mid)[1]:
                raise CoordError(f"thread {t} is already in mission {mid}")


def _by(store: CoordStore, by: str, token: object) -> tuple[str, object]:
    return store._actor(by, ("operator", "coordinator")), token


def create_mission(store: CoordStore, thread_ids: Sequence[str], *, by: str = "operator", token: object = None) -> Mission:
    """Group work threads into a new mission (revision 1). One transaction; a coordinator write is fenced by its lease
    token like every other coordinator write."""
    by, token = _by(store, by, token)
    ids = list(dict.fromkeys(thread_ids))
    if not ids:
        raise CoordError("a mission names at least one thread")
    with store._write(by, token) as (conn, now):
        _member_check(conn, ids, mission_id=None)
        mission_id = "ms-" + secrets.token_hex(16)
        conn.execute("INSERT INTO missions (mission_id, created_at) VALUES (?, ?)", (mission_id, now))
        _revise(conn, mission_id, ids, "create", by, now)
        return _view(conn, mission_id)


def attach(store: CoordStore, mission_id: str, thread_id: str, *, by: str = "operator", token: object = None) -> Mission:
    """Add a work thread: a new mission revision, so an acceptance rendered before it is refused as stale."""
    by, token = _by(store, by, token)
    with store._write(by, token) as (conn, now):
        _must_mission(conn, mission_id)
        _, children, _ = _latest(conn, mission_id)
        if thread_id in children:
            raise CoordError(f"thread {thread_id} is already in mission {mission_id}")
        _member_check(conn, [thread_id], mission_id=mission_id)
        _revise(conn, mission_id, [*children, thread_id], "attach", by, now)
        return _view(conn, mission_id)


def detach(store: CoordStore, mission_id: str, thread_id: str, *, by: str = "operator", token: object = None) -> Mission:
    """Remove a thread. A mission is never left empty: detach the last one and it is refused."""
    by, token = _by(store, by, token)
    with store._write(by, token) as (conn, now):
        _must_mission(conn, mission_id)
        _, children, _ = _latest(conn, mission_id)
        if thread_id not in children:
            raise CoordError(f"thread {thread_id} is not in mission {mission_id}")
        if len(children) == 1:
            raise CoordError("a mission is never left with no thread")
        _revise(conn, mission_id, [c for c in children if c != thread_id], "detach", by, now)
        return _view(conn, mission_id)


def _proof(message: str, answer: object) -> tuple[str, str]:
    """The verifier's answer as (signer fingerprint, digest of what was verified). A verifier that holds the signature
    returns ``(fingerprint, signature_bytes)`` and the digest is over those bytes; one that returns only a fingerprint
    is bound by the message it was shown and that fingerprint (the same signer over the same state is one signature)."""
    if isinstance(answer, tuple) and len(answer) == 2 and isinstance(answer[1], (bytes, bytearray)):
        fp, raw = answer[0], bytes(answer[1])
    else:
        fp, raw = answer, (message + "\0" + str(answer)).encode("utf-8")
    if not isinstance(fp, str) or not fp:
        raise AcceptanceRefused("signature", "the signature was not verified; nothing was accepted")
    return fp, "sha256:" + hashlib.sha256(raw).hexdigest()


def accept(store: CoordStore, mission_id: str, *, snapshot_sha256: str, sign_request_id: str,
           verify: Callable[[str], str | tuple[str, bytes]]) -> MissionAcceptance:
    """Close the mission on the operator's signature over the snapshot he saw (the compare-and-swap).

    ``verify(message)`` is the caller's check of its sign request: it takes :func:`accept_message` for the snapshot
    ``snapshot_sha256`` names and returns the signer's fingerprint (or ``(fingerprint, signature_bytes)``), or raises
    :class:`AcceptanceRefused` (``signature``). It runs before the write, and only for the CURRENT state: a digest that
    is not the current snapshot's is refused as stale before any message is built. Inside the write transaction the
    snapshot is recomputed again and must still equal ``snapshot_sha256``, so a signature over an older state accepts
    nothing; the mission must be ready (every child done or closed) and carry no un-ACKed KNOW. One signature accepts
    once: a second acceptance carrying the same verified signature is a replay under any sign request id.

    The coordinator never accepts: this write is the operator's. Whether the fingerprint ``verify`` returns IS the
    operator's key is ``verify``'s to decide, not this module's: it owns the sign request and the key it was signed
    with, and this module records the fingerprint it is handed."""
    if not isinstance(snapshot_sha256, str) or not isinstance(sign_request_id, str) or not sign_request_id:
        raise CoordError("an acceptance names the snapshot digest and the sign request")
    stale = AcceptanceRefused("stale", f"mission {mission_id} moved since the card rendered (a child, a KNOW, an "
                              "attenuation or the membership changed); nothing was accepted; sign the new card")
    with read_transaction(store._connect) as conn:
        _must_mission(conn, mission_id)
        current = _snapshot(conn, mission_id)
    if current.sha256 != snapshot_sha256:
        raise stale
    message = accept_message(mission_id, current)
    signer, signature_sha256 = _proof(message, verify(message))
    with store._write("operator", None) as (conn, now):
        snap = _snapshot(conn, mission_id)
        if snap.sha256 != snapshot_sha256 or message != accept_message(mission_id, snap):
            raise stale
        if not snap.body["children"] or not snap.ready:
            raise AcceptanceRefused("not_ready", f"mission {mission_id} has a child that is not done; nothing was accepted")
        if snap.unacked_item_ids:
            raise AcceptanceRefused("unacked", f"mission {mission_id} has {len(snap.unacked_item_ids)} un-ACKed KNOW(s): "
                                    "ACK them first; nothing was accepted")
        acceptance_id = "ma-" + secrets.token_hex(16)
        try:
            conn.execute("INSERT INTO mission_acceptances (acceptance_id, mission_id, mission_revision, snapshot, "
                          "snapshot_sha256, sign_request_id, signature_sha256, signer_fp, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (acceptance_id, mission_id, snap.body["mission_revision"], canonical(snap.body), snap.sha256,
                           sign_request_id, signature_sha256, signer, now))
        except sqlite3.IntegrityError as exc:
            if "sign_request_id" not in str(exc) and "signature_sha256" not in str(exc):
                raise
            raise AcceptanceRefused("replay", f"that signature (sign request {sign_request_id}) already accepted a "
                                    "mission; nothing was accepted; sign again") from None
        return MissionAcceptance(acceptance_id, mission_id, snap.body["mission_revision"], snap.sha256, sign_request_id,
                                 signer, now)
