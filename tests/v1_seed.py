"""Test seeding for the RETIRED v1 ready queue (T7, levain#101): rows written straight into ``ready_journal`` and
``ready_items`` the way the one writer used to write them, so a reader that still reads the tables (the cockpit's board
until T6 moves it, the retired verbs' own tests) can be exercised on data. Nothing in ``levain`` writes them now:
:func:`levain.desk.ready.add` raises. Test code only; it has no validation of its own."""
from __future__ import annotations

import json
from typing import Any

from levain.cockpit.consent import parse_stamp
from levain.desk import ready
from levain.desk.legacy import ROLE_DESK
from levain.desk.store import CoordStore, next_gseq

_UNSET = object()


def _state(prior: dict | None, facts: dict, repo: Any, to: str, fields: dict) -> dict:
    changes = {k: v for k, v in fields.items() if v is not _UNSET}
    if prior is None:
        state = {"repo": repo, "title": changes["title"], "tier": changes.get("tier"),
                 "done_line": changes.get("done_line"), "depends_on": list(changes.get("depends_on") or []),
                 "rank": changes.get("rank")}
        state.update({c: changes[c] for c in ready.FACT_COLS if c in changes})
    else:
        state = {k: prior[k] for k in ("repo", "title", "tier", "done_line", "depends_on", "rank")}
        state.update(changes)
    for c in ready.FACT_COLS:
        state.setdefault(c, facts[c])
    state["category"] = state["status"] = to
    return state


def write(store: CoordStore, token: int, item_id: str, *, to: str, who: str, why: str, source: str, creating: bool,
          repo: Any = None, **fields: Any) -> dict:
    with store._write(ROLE_DESK, token, create=False) as (conn, now):
        if not ready._lifecycle_present(conn):
            for stmt in ready.LIFECYCLE_SCHEMA:
                conn.execute(stmt)
        found = ready._current(conn, item_id)
        prior = found[0] if found else None
        facts = ready._facts(conn, item_id) if prior is not None else dict.fromkeys(ready.FACT_COLS)
        state = _state(prior, facts, repo, to, fields)
        if state["rank"] is None:
            top = conn.execute(f"SELECT MAX(rank) FROM ({ready._CURRENT}) WHERE repo = ?", (state["repo"],)).fetchone()[0]
            state["rank"] = 1 if top is None else top + 1
        ver = 1 if prior is None else prior["ver"] + 1
        when = parse_stamp(now)
        rec = {"ts": when.timestamp(), "iso": when.astimezone().isoformat(timespec="seconds"), "id": item_id,
               "repo": state["repo"], "from_status": None if prior is None else prior["status"],
               "to_status": to, "from_category": None if prior is None else prior["category"], "to_category": to,
               "who": who, "why": why, "source": source, "applied_by": None}
        conn.execute(
            "INSERT INTO ready_journal (id, ver, gseq, ts, iso, at, repo, from_status, to_status, from_category, "
            "to_category, who, why, source, applied_by, lease_token) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, ver, next_gseq(conn, now), rec["ts"], rec["iso"], now, rec["repo"], rec["from_status"],
             rec["to_status"], rec["from_category"], rec["to_category"], who, why, source, None, token))
        conn.execute(
            "INSERT INTO ready_items (id, ver, repo, title, status, category, tier, done_line, depends_on, rank, set_by, "
            f"set_at, {', '.join(ready.FACT_COLS)}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            f"{', '.join('?' * len(ready.FACT_COLS))})",
            (item_id, ver, state["repo"], state["title"], to, to, state["tier"], state["done_line"],
             json.dumps(state["depends_on"]), state["rank"], who, now, *(state.get(c) for c in ready.FACT_COLS)))
    out = dict(rec)
    return out


def add(store, token, item_id, *, repo, title, who, why, to="backlog", tier=None, done_line=None, depends_on=(),
        rank=None, hold=_UNSET, residue=_UNSET, ref=_UNSET):
    return write(store, token, item_id, to=to, who=who, why=why, source="add", creating=True, repo=repo, title=title,
                 tier=tier, done_line=done_line, depends_on=list(depends_on), rank=rank, hold=hold, residue=residue, ref=ref)


def move(store, token, item_id, *, to, who, why, **fields):
    return write(store, token, item_id, to=to, who=who, why=why, source="move", creating=False, **fields)


def correct(store, token, item_id, *, to, who, why, **fields):
    return write(store, token, item_id, to=to, who=who, why=why, source="correction", creating=False, **fields)
