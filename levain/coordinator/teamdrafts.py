"""levain.coordinator.teamdrafts: the coordinator's DRAFTS of team-ledger entries (C8).

The ruling (Phill 10-19): each operator's coordinator READS the team ledger and may DRAFT entries; only the operator
signs. A draft here is a row in the coordinator store and nothing else: it holds no signature, no ledger id until the
operator promotes it, and this module never imports the ledger writer. Promotion happens in the operator's own verb
(``levain team draft promote``), which signs with the operator's key through the ordinary record path and then writes
a ``promoted`` event here naming the ledger id it got back.

What this module does NOT claim: that nothing else on the machine can sign. The record verb signs with whatever key
this clone is set up with, and a process of the same uid can run it. Drafting keeps the coordinator's own path honest;
the key's reach is the operator's boundary (see the C8 finding).

Tables are append-only like the rest of the store; a draft's state is its latest event.
"""
from __future__ import annotations

import json
import re
import secrets
from collections.abc import Sequence
from typing import Any

from levain.coordinator.store import ACTORS, CoordError, CoordStore, _append_only, _in

__all__ = ["DRAFT_SCHEMA", "DRAFT_TYPES", "add_draft", "get_draft", "list_drafts", "mark_promoted", "withdraw_draft"]

DRAFT_TYPES = ("decision", "constraint", "finding", "question", "tension")
_EVENTS = ("drafted", "withdrawn", "promoted")
_DRAFT_ID = re.compile(r"^td-[0-9a-f]{32}$")
_FIELD_MAX = 20_000
_LEDGER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

DRAFT_SCHEMA: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS team_drafts (draft_id TEXT PRIMARY KEY, "
    f"type TEXT NOT NULL CHECK ({_in('type', DRAFT_TYPES)}), body TEXT NOT NULL, body_sha256 TEXT NOT NULL, "
    "thread_id TEXT, at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS team_draft_events (draft_id TEXT NOT NULL REFERENCES team_drafts(draft_id), "
    f"seq INTEGER NOT NULL, event TEXT NOT NULL CHECK ({_in('event', _EVENTS)}), ledger_id TEXT, "
    f"by TEXT NOT NULL CHECK ({_in('by', ACTORS)}), at TEXT NOT NULL, PRIMARY KEY (draft_id, seq))",
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


def _text(name: str, value: Any, *, required: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise CoordError(f"a team draft needs {name}")
        return None
    if not isinstance(value, str) or len(value) > _FIELD_MAX or "\x00" in value:
        raise CoordError(f"a team draft's {name} is text of at most {_FIELD_MAX} characters")
    return value


def _view(conn, draft_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT type, body, body_sha256, thread_id, at FROM team_drafts WHERE draft_id = ?",
                       (draft_id,)).fetchone()
    if row is None:
        return None
    ev = conn.execute("SELECT event, ledger_id, by, at FROM team_draft_events WHERE draft_id = ? ORDER BY seq DESC "
                      "LIMIT 1", (draft_id,)).fetchone()
    first = conn.execute("SELECT by FROM team_draft_events WHERE draft_id = ? AND seq = 1", (draft_id,)).fetchone()
    return {"draft_id": draft_id, "type": row[0], "entry": json.loads(row[1]), "body_sha256": row[2],
            "thread_id": row[3], "drafted_at": row[4], "drafted_by": first[0] if first else None,
            "state": ev[0] if ev else "drafted", "ledger_id": ev[1] if ev else None}


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
    if not (entry["words"] or entry["summary"]):
        raise CoordError("a team draft needs words or a summary")
    body = json.dumps(entry, sort_keys=True, ensure_ascii=False)
    draft_id = "td-" + secrets.token_hex(16)
    with store._write(by, token if by == "coordinator" else None) as (conn, now):
        conn.execute("INSERT INTO team_drafts (draft_id, type, body, body_sha256, thread_id, at) VALUES (?,?,?,?,?,?)",
                     (draft_id, type_, body, hashlib.sha256(body.encode()).hexdigest(), thread_id, now))
        conn.execute("INSERT INTO team_draft_events (draft_id, seq, event, ledger_id, by, at) "
                     "VALUES (?,1,'drafted',NULL,?,?)", (draft_id, by, now))
        return _view(conn, draft_id)  # type: ignore[return-value]


def get_draft(store: CoordStore, draft_id: str) -> dict[str, Any] | None:
    from levain.autonomic.durable_sqlite import read_transaction

    if not isinstance(draft_id, str) or _DRAFT_ID.fullmatch(draft_id) is None:
        raise CoordError("a draft id is td- and 32 hex")
    with read_transaction(store._connect) as conn:
        return _view(conn, draft_id)


def list_drafts(store: CoordStore, *, state: str | None = "drafted") -> list[dict[str, Any]]:
    from levain.autonomic.durable_sqlite import read_transaction

    with read_transaction(store._connect) as conn:
        ids = [r[0] for r in conn.execute("SELECT draft_id FROM team_drafts ORDER BY rowid DESC")]
        out = [v for v in (_view(conn, i) for i in ids) if v is not None]
    return [v for v in out if state is None or v["state"] == state]


def withdraw_draft(store: CoordStore, draft_id: str, *, by: str = "operator", token: object = None) -> dict[str, Any]:
    with store._write(by, token if by == "coordinator" else None) as (conn, now):
        v = _view(conn, draft_id)
        if v is None:
            raise CoordError(f"no team draft {draft_id}")
        if v["state"] != "drafted":
            raise CoordError(f"draft {draft_id} is already {v['state']}")
        conn.execute("INSERT INTO team_draft_events (draft_id, seq, event, ledger_id, by, at) VALUES "
                     "(?, (SELECT MAX(seq) FROM team_draft_events WHERE draft_id = ?) + 1, 'withdrawn', NULL, ?, ?)",
                     (draft_id, draft_id, by, now))
        return _view(conn, draft_id)  # type: ignore[return-value]


def mark_promoted(store: CoordStore, draft_id: str, ledger_id: str) -> dict[str, Any]:
    """Written only by the operator's promote verb, after the ledger returned ``ledger_id``. Never fenced by a lease:
    the operator's write is the one that is not the coordinator's."""
    if not isinstance(ledger_id, str) or _LEDGER_ID.fullmatch(ledger_id) is None:
        raise CoordError("a promotion names the ledger id the signed entry got")
    with store._write() as (conn, now):
        v = _view(conn, draft_id)
        if v is None:
            raise CoordError(f"no team draft {draft_id}")
        if v["state"] != "drafted":
            raise CoordError(f"draft {draft_id} is already {v['state']}")
        conn.execute("INSERT INTO team_draft_events (draft_id, seq, event, ledger_id, by, at) VALUES "
                     "(?, (SELECT MAX(seq) FROM team_draft_events WHERE draft_id = ?) + 1, 'promoted', ?, "
                     "'operator', ?)", (draft_id, draft_id, ledger_id, now))
        return _view(conn, draft_id)  # type: ignore[return-value]
