"""levain.coordinator.teamdrafts: the coordinator's DRAFTS of team-ledger entries (C8).

The ruling (Phill 10-19): each operator's coordinator READS the team ledger and may DRAFT entries; only the operator
signs. A draft here is a row in the coordinator store and nothing else: it holds no signature, no ledger id until the
operator promotes it, and this module never imports the ledger writer (only the entry schema, to refuse a draft the
ledger would refuse). Promotion happens in the operator's own verb (``levain team draft promote``): it first RESERVES
the draft (``drafted -> promoting``, one transaction; a withdraw loses from then on), signs through the ordinary record
path with ``agent: coordinator-draft:<draft id>`` on the entry, and then writes a ``promoted`` event naming the ledger
id, which :func:`mark_promoted` checks against the ledger it is handed. The agent value is the idempotency key: a
promote that finds an entry carrying it adopts that entry and never signs a second one.

A reservation is OWNED (the frame of an idempotency key whose request is still in flight: a second request is refused,
never run a second time). The ``promoting`` event records its owner: a random nonce, the pid and start time of the
promote that made it, and the clone it signs in. Only the owner signs. Any other promote that finds the draft reserved
adopts an entry the ledger already holds for it, or is refused. ``--resume`` is the operator's act that takes a
reservation over, and only from the owner's clone once the owner is gone (no process with that pid and start time).

What this module does NOT claim: that nothing else on the machine can sign. The record verb signs with whatever key
this clone is set up with, and a process of the same uid can run it. Drafting keeps the coordinator's own path honest;
the key's reach is the operator's boundary (see the C8 finding).

Tables are append-only like the rest of the store; a draft's state is its latest event.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from collections.abc import Mapping, Sequence
from typing import Any

from levain.coordinator.store import ACTORS, CoordError, CoordStore, _append_only, _in, next_gseq

__all__ = ["DRAFT_SCHEMA", "DRAFT_TYPES", "add_draft", "draft_agent", "get_draft", "list_drafts", "mark_promoted",
           "promoter", "release_promotion", "reserve_promotion", "withdraw_draft"]

DRAFT_TYPES = ("decision", "constraint", "finding", "question", "tension")
# "drafted" again after "promoting" is a reservation given back by a promote that signed nothing
_EVENTS = ("drafted", "promoting", "withdrawn", "promoted")
_DRAFT_ID = re.compile(r"^td-[0-9a-f]{32}$")
_LEDGER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

DRAFT_SCHEMA: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS team_drafts (draft_id TEXT PRIMARY KEY, "
    f"type TEXT NOT NULL CHECK ({_in('type', DRAFT_TYPES)}), body TEXT NOT NULL, body_sha256 TEXT NOT NULL, "
    "thread_id TEXT, at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS team_draft_events (draft_id TEXT NOT NULL REFERENCES team_drafts(draft_id), "
    f"seq INTEGER NOT NULL, event TEXT NOT NULL CHECK ({_in('event', _EVENTS)}), ledger_id TEXT, "
    f"by TEXT NOT NULL CHECK ({_in('by', ACTORS)}), at TEXT NOT NULL, "
    # the owner of a reservation (JSON: nonce, pid, started, clone), on a promoting event and on no other
    "owner TEXT CHECK ((owner IS NOT NULL) = (event = 'promoting')), PRIMARY KEY (draft_id, seq))",
    *_append_only("team_drafts", "a team draft"),
    *_append_only("team_draft_events", "a team draft event"),
    "CREATE TRIGGER IF NOT EXISTS team_drafts_no_replace BEFORE INSERT ON team_drafts WHEN EXISTS "
    "(SELECT 1 FROM team_drafts WHERE draft_id = NEW.draft_id OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a team draft is never replaced'); END",
    "CREATE TRIGGER IF NOT EXISTS team_draft_events_next BEFORE INSERT ON team_draft_events "
    "WHEN NEW.seq != COALESCE((SELECT MAX(seq) FROM team_draft_events WHERE draft_id = NEW.draft_id), 0) + 1 "
    "OR EXISTS (SELECT 1 FROM team_draft_events WHERE rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'an event is the next seq of its draft'); END",
    # a promotion is the end of a draft, once: the ledger id is named, and nothing follows it
    "CREATE TRIGGER IF NOT EXISTS team_draft_events_final BEFORE INSERT ON team_draft_events WHEN EXISTS "
    "(SELECT 1 FROM team_draft_events WHERE draft_id = NEW.draft_id AND event IN ('withdrawn', 'promoted')) "
    "BEGIN SELECT RAISE(ABORT, 'a withdrawn or promoted draft is final'); END",
)


def draft_agent(draft_id: str) -> str:
    """The ``agent`` a promoted draft's ledger entry carries: how a later promote finds it, and what proves it."""
    return "coordinator-draft:" + draft_id


def _text(name: str, value: Any, *, required: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise CoordError(f"a team draft needs {name}")
        return None
    if not isinstance(value, str) or "\x00" in value:
        raise CoordError(f"a team draft's {name} is text")
    return value


def _promotable(entry: dict[str, Any], draft_id: str) -> None:
    """Refuse a draft the ledger would refuse at promote: the ledger's own validator, run on the entry promote would
    build. A ruling's words are stood in for here: they are the decider's, given at promote."""
    from levain.team import entry as E

    words = entry["words"]
    if entry["kind"] == "ruling":
        words = "the decider's words, given at promote"
    candidate = E.build("draft", entry["type"], kind=entry["kind"], owner=entry["owner"], words=words,
                        summary=entry["summary"], reason=entry["reason"], recheck=entry["recheck"],
                        paths=list(entry["paths"]), agent=draft_agent(draft_id))
    try:
        E.validate(candidate)
    except E.EntryError as exc:
        raise CoordError(f"the ledger would refuse this draft at promote: {exc}") from None


def _view(conn, draft_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT type, body, body_sha256, thread_id, at FROM team_drafts WHERE draft_id = ?",
                       (draft_id,)).fetchone()
    if row is None:
        return None
    ev = conn.execute("SELECT event, ledger_id, by, at, owner FROM team_draft_events WHERE draft_id = ? "
                      "ORDER BY seq DESC LIMIT 1", (draft_id,)).fetchone()
    first = conn.execute("SELECT by FROM team_draft_events WHERE draft_id = ? AND seq = 1", (draft_id,)).fetchone()
    return {"draft_id": draft_id, "type": row[0], "entry": json.loads(row[1]), "body_sha256": row[2],
            "thread_id": row[3], "drafted_at": row[4], "drafted_by": first[0] if first else None,
            "state": ev[0] if ev else "drafted", "ledger_id": ev[1] if ev else None,
            "owner": json.loads(ev[4]) if ev and ev[4] else None}


def add_draft(store: CoordStore, type_: str, *, words: str | None = None, kind: str | None = None,
              owner: str | None = None, paths: Sequence[str] = (), summary: str | None = None,
              reason: str | None = None, recheck: str | None = None, thread_id: str | None = None,
              by: str = "coordinator", token: object = None) -> dict[str, Any]:
    """Store a draft. A coordinator's draft is fenced by its lease token like any coordinator write."""
    import hashlib

    if type_ not in DRAFT_TYPES:
        raise CoordError(f"a team draft's type is one of {', '.join(DRAFT_TYPES)}")
    if by not in ("operator", "coordinator"):
        raise CoordError("a team draft is written by the operator or the coordinator")
    if kind not in (None, "ruling", "practice"):
        raise CoordError("a team draft's kind is ruling or practice")
    entry = {"type": type_, "kind": kind, "owner": _text("owner", owner), "words": _text("words", words),
             "summary": _text("summary", summary), "reason": _text("reason", reason),
             "recheck": _text("recheck", recheck), "paths": [_text("a path", p, required=True) for p in paths]}
    for p in entry["paths"]:
        # a draft names what it governs from the repository root: it is not tied to one clone's place on disk
        if p.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", p):
            raise CoordError(f"a team draft governs paths relative to the repository root, and {p!r} is absolute: "
                             "write it from the root (src/app.py, docs/**)")
    if not (entry["words"] or entry["summary"]):
        raise CoordError("a team draft needs words or a summary")
    draft_id = "td-" + secrets.token_hex(16)
    _promotable(entry, draft_id)
    body = json.dumps(entry, sort_keys=True, ensure_ascii=False)
    with store._write(by, token if by == "coordinator" else None) as (conn, now):
        conn.execute("INSERT INTO team_drafts (draft_id, type, body, body_sha256, thread_id, at, gseq) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (draft_id, type_, body, hashlib.sha256(body.encode()).hexdigest(), thread_id, now,
                      next_gseq(conn, now)))
        conn.execute("INSERT INTO team_draft_events (draft_id, seq, event, ledger_id, by, at, gseq) "
                     "VALUES (?,1,'drafted',NULL,?,?,?)", (draft_id, by, now, next_gseq(conn, now)))
        return _view(conn, draft_id)  # type: ignore[return-value]


def get_draft(store: CoordStore, draft_id: str) -> dict[str, Any] | None:
    from levain.autonomic.durable_sqlite import read_transaction

    if not isinstance(draft_id, str) or _DRAFT_ID.fullmatch(draft_id) is None:
        raise CoordError("a draft id is td- and 32 hex")
    with read_transaction(store._connect) as conn:
        return _view(conn, draft_id)


def list_drafts(store: CoordStore, *, state: str | Sequence[str] | None = "drafted") -> list[dict[str, Any]]:
    """Drafts newest first; ``state`` is one state, several, or None for every draft."""
    from levain.autonomic.durable_sqlite import read_transaction

    states = (state,) if isinstance(state, str) else state
    with read_transaction(store._connect) as conn:
        ids = [r[0] for r in conn.execute("SELECT draft_id FROM team_drafts ORDER BY rowid DESC")]
        out = [v for v in (_view(conn, i) for i in ids) if v is not None]
    return [v for v in out if states is None or v["state"] in states]


def _next_event(conn, draft_id: str, event: str, ledger_id: str | None, by: str, now: str,
                owner: dict[str, Any] | None = None) -> None:
    conn.execute("INSERT INTO team_draft_events (draft_id, seq, event, ledger_id, by, at, owner, gseq) VALUES "
                 "(?, (SELECT MAX(seq) FROM team_draft_events WHERE draft_id = ?) + 1, ?, ?, ?, ?, ?, ?)",
                 (draft_id, draft_id, event, ledger_id, by, now,
                  json.dumps(owner, sort_keys=True) if owner is not None else None, next_gseq(conn, now)))


def _current(conn, draft_id: str) -> dict[str, Any]:
    v = _view(conn, draft_id)
    if v is None:
        raise CoordError(f"no team draft {draft_id}")
    return v


def withdraw_draft(store: CoordStore, draft_id: str, *, by: str = "operator", token: object = None) -> dict[str, Any]:
    if by not in ("operator", "coordinator"):
        raise CoordError("a team draft is withdrawn by the operator or the coordinator")
    with store._write(by, token if by == "coordinator" else None) as (conn, now):
        v = _current(conn, draft_id)
        if v["state"] == "promoting":
            raise CoordError(f"draft {draft_id} is being promoted (a promote reserved it); it can no longer be "
                             f"withdrawn. If that promote stopped, `levain team draft promote {draft_id}` finishes it")
        if v["state"] != "drafted":
            raise CoordError(f"draft {draft_id} is already {v['state']}")
        _next_event(conn, draft_id, "withdrawn", None, by, now)
        return _view(conn, draft_id)  # type: ignore[return-value]


def promoter(clone: str) -> dict[str, Any]:
    """The owner record of a reservation this process makes: a fresh nonce, this pid and its start time, the clone."""
    from levain.coordinator.awake import process_started

    started = process_started(os.getpid())
    if started is None:
        raise CoordError("this promote cannot read its own process start time (ps), so it cannot own a reservation")
    return {"nonce": secrets.token_hex(16), "pid": os.getpid(), "started": started, "clone": clone}


def _alive(owner: dict[str, Any]) -> bool:
    """Is the process that owns a reservation still running: the same pid with the same start time. A pid the system
    has since given to another process is not the owner. When it cannot be told, it counts as alive."""
    from levain.coordinator.awake import process_started

    pid = owner.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    started = process_started(pid)
    if started is None:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        return True
    return started == owner.get("started")


def reserve_promotion(store: CoordStore, draft_id: str, owner: dict[str, Any], *,
                      resume: bool = False) -> tuple[dict[str, Any], bool]:
    """The operator's promote, before it signs: ``drafted -> promoting`` in one transaction, owned by ``owner`` (see
    :func:`promoter`). Returns the draft and whether ``owner`` now owns the reservation. A draft another promote has
    reserved comes back unowned (the caller adopts that promote's entry or is refused), unless ``resume``: then it is
    taken over, but only from the owner's own clone and only once the owner is gone. A withdrawn or promoted draft is
    refused."""
    with store._write() as (conn, now):
        v = _current(conn, draft_id)
        if v["state"] == "promoting":
            if not resume:
                return v, False
            held = v["owner"] or {}
            if _alive(held):
                raise CoordError(f"the promote that reserved draft {draft_id} is still alive (pid {held.get('pid')}); "
                                 "--resume takes over only from a promote that is gone")
            if held.get("clone") != owner["clone"]:
                raise CoordError(f"draft {draft_id} was reserved by a promote in the clone at {held.get('clone')}: "
                                 "resume it from that clone, which holds anything it signed")
            _next_event(conn, draft_id, "promoting", None, "operator", now, owner)
            return _view(conn, draft_id), True  # type: ignore[return-value]
        if v["state"] != "drafted":
            raise CoordError(f"draft {draft_id} is already {v['state']}")
        _next_event(conn, draft_id, "promoting", None, "operator", now, owner)
        return _view(conn, draft_id), True  # type: ignore[return-value]


def release_promotion(store: CoordStore, draft_id: str, nonce: str) -> dict[str, Any]:
    """Give a reservation back: called by its owner (``nonce``) when it stopped and its ledger holds no entry for this
    draft. A reservation someone else now owns is not given back."""
    with store._write() as (conn, now):
        v = _current(conn, draft_id)
        if v["state"] != "promoting":
            raise CoordError(f"draft {draft_id} is {v['state']}, not reserved")
        if (v["owner"] or {}).get("nonce") != nonce:
            raise CoordError(f"draft {draft_id} is reserved by another promote; this one gives nothing back")
        _next_event(conn, draft_id, "drafted", None, "operator", now)
        return _view(conn, draft_id)  # type: ignore[return-value]


def mark_promoted(store: CoordStore, draft_id: str, ledger_id: str, signed: Mapping[str, dict]) -> dict[str, Any]:
    """Written only by the operator's promote verb, once the entry is in the ledger. ``signed`` is that ledger's
    entries by id, read by the caller from the ledger itself; the id is taken only if it is there and its agent is
    this draft's (:func:`draft_agent`). Never fenced by a lease: the operator's write is not the coordinator's."""
    if not isinstance(ledger_id, str) or _LEDGER_ID.fullmatch(ledger_id) is None:
        raise CoordError("a promotion names the ledger id the signed entry got")
    got = signed.get(ledger_id)
    if not isinstance(got, dict) or got.get("agent") != draft_agent(draft_id):
        raise CoordError(f"the ledger holds no entry {ledger_id} signed from draft {draft_id}")
    with store._write() as (conn, now):
        v = _current(conn, draft_id)
        if v["state"] == "promoted" and v["ledger_id"] == ledger_id:
            return v        # already marked with this very entry (its owner and an adopter both got here)
        if v["state"] != "promoting":
            raise CoordError(f"draft {draft_id} is {v['state']}: only a reserved draft is marked promoted")
        _next_event(conn, draft_id, "promoted", ledger_id, "operator", now)
        return _view(conn, draft_id)  # type: ignore[return-value]
