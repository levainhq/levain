"""levain.desk.ready — the ready queue's reader and the ONE writer of lifecycle state (C13).

Contract: READY_SCHEMA v1 (flow's ``READY_SCHEMA.md``, implemented unchanged), conformance fixture
``tests/fixtures/ready_schema/ready_v1.json`` (flow's, byte for byte). Ruled (Phill 10-28): lifecycle is the AI's job;
the desk is the SINGLE WRITER; every transition goes to an append-only journal (who, when, why); reads go direct,
so a seat reads the queue with no desk live; only writes need one.

Three parts:

* :func:`derive` — PURE over the fixture's normalized input shape (the intention rows under the literal key ``spores`` (``READY_SOURCE_KEY``, flow's input key), ``resolved``, ``needs_phill``,
  ``seats``, ``ci``, ``plan``) plus ``store``, the desk store's current items. It applies SCHEMA sections 2-5:
  the closed category set, a dependency holding a declared-ready item back, needs-you winning, the order, and the
  derived ``well_formed`` / ``independent``. It reads no file and no clock (``generated`` is the caller's).
* the store tables (:data:`LIFECYCLE_SCHEMA`), made by the desk store's own ensure-schema path
  (:meth:`levain.desk.store.CoordStore._statements`): ``ready_journal`` (SCHEMA section 6, one row per
  transition) and ``ready_items`` (one versioned state row per transition; an item's current state is its latest
  row). Both append-only by trigger, like every desk table. Structural, not just by API: a state row is refused
  unless its own journal record (same id, version, category, repo, who) is already in the file, and a journal record
  is refused unless it is the NEXT transition of its item (its version, and its ``from_category`` equal to the
  item's current category), so a state change with no record cannot be written, two records for one change cannot be
  written, and a replay of the journal reproduces every item's ``declared_category``.
* the ONE write path, :func:`add` / :func:`move` / :func:`correct`: each runs in ONE desk write transaction
  fenced by the live lease token (the desk identity, :func:`levain.desk.lease.fence`), and writes the
  journal record then the state row; anything that raises rolls both back. Refused before anything is opened: no
  token, an empty ``why`` or ``who``, an unknown category, a move INTO ``needs-you`` (a projection: the band owns it),
  a projection id (``seat:``, ``ci:``, ``np-``).

:func:`list_ready` (exported as ``list``, the contract's verb) reads the store through the ONE read-only connection
(:meth:`CoordStore.open_readonly`: ``mode=ro``, ``query_only``, no write lock) inside one read transaction, so it is
a consistent snapshot and needs no desk. No UI and no CLI verb here: those are C12.

[seat 1028-15 C13] choices the contract does not state: the store's items enter :func:`derive` as the ``store`` input
with provenance source ``plan`` (SCHEMA section 5: "the desk store once C13 exists"); a store item shadows a
plan row or intention of the same id; ``status`` equals the category (v1) and the writer takes no separate status; a write
that changes nothing is refused; an item's repo is fixed at ``add``. Stdlib only.

Known edge (row 156 L3 C5): the current-state read looks up each item's prose hold with its own queries, so a list
costs a query pair per item, which a board the size of one status file's lane menu bears.
"""
from __future__ import annotations

import builtins
import json
import os
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from levain.autonomic.durable_sqlite import read_transaction
from levain.cockpit.consent import parse_stamp
from levain.desk.store import _OID, CoordError, CoordStore, StoreAbsent, _append_only, next_gseq
from levain.dispatch import DispatchError
from levain.desk.legacy import ROLE_DESK, SQL_ROLE_DESK
from levain.intention_legacy import READY_SOURCE_KEY

__all__ = [
    "CATEGORIES", "HANDOFF_RANK_BASE", "HEALTHY_AT", "JOURNAL_SOURCES", "LIFECYCLE_SCHEMA", "SCHEMA_VERSION", "TIERS",
    "JournalAhead", "WRITABLE_CATEGORIES", "add", "correct", "derive", "derive_items", "journal", "list", "list_ready", "move",
    "FACT_COLS", "item_facts", "migrate_item_facts", "ready_counts", "record_landed", "replay", "slug",
]

SCHEMA_VERSION = 1
CATEGORIES = ("backlog", "ready", "running", "waiting", "needs-you", "done")
WRITABLE_CATEGORIES = tuple(c for c in CATEGORIES if c != "needs-you")
TIERS = ("sonnet", "opus", "haiku")
JOURNAL_SOURCES = ("move", "add", "correction", "operator-proposal")
HANDOFF_RANK_BASE = 1000
HEALTHY_AT = 3          # SCHEMA §4 rule 4 (ruled 10-28: "3+ per repo"); reported, never acted on here
STORE_SOURCE = ROLE_DESK
UNREADABLE_DESK = "desk"            # what ``unreadable`` names this store (a view-model word, never stored)

_PROJECTION_PREFIXES = ("seat:", "ci:", "np-")
_ITEM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9+._:-]{0,199}")
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_WHO_MAX = 200
_WHY_MAX = 1000
_TITLE_MAX = 120        # the read side shows a title whole (``_one_line``'s limit): the writer takes no more
_DONE_MAX = 1000
_DEPENDS_MAX = 50
_REF_MAX = 300

# Plan-row dependency markers (flow's, unchanged). A numeric "after N" names a row; the others hold with their words.
_AFTER_N_RE = re.compile(r"\bafter\s+(\d+)\s*(?:[.,;:)*`]|$)", re.I)
_OTHER_MARK_RE = re.compile(r"\bafter\s+\S+\s+lands?\b|\bdeps? NOT met|\bdesign first\b", re.I)


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


# ------------------------------------------------------------------------------------------------ the store tables
# the columns an item's state gained after the first release, one definition for a new table and for the additive
# migration of an old one (:func:`migrate_item_facts`): NULL on every row written before them
FACT_COLS = ("hold", "residue", "ref", "landed_sha", "branch")
_FACT_DDL = {"hold": "hold TEXT", "residue": "residue TEXT", "ref": "ref TEXT",
             "landed_sha": "landed_sha TEXT CHECK (landed_sha IS NULL OR length(landed_sha) IN (40, 64))",
             "branch": "branch TEXT"}
ITEM_FACTS_SQL = "".join(f", {_FACT_DDL[c]}" for c in FACT_COLS)

LIFECYCLE_SCHEMA: tuple[str, ...] = (
    # SCHEMA §6, one row per transition; gseq places it in the store-wide order, lease_token names the writer's lease
    "CREATE TABLE IF NOT EXISTS ready_journal (id TEXT NOT NULL, ver INTEGER NOT NULL CHECK (ver >= 1), "
    "gseq INTEGER NOT NULL UNIQUE, ts REAL NOT NULL, iso TEXT NOT NULL, at TEXT NOT NULL, "
    "repo TEXT NOT NULL CHECK (length(repo) > 0), from_status TEXT, to_status TEXT NOT NULL, "
    f"from_category TEXT CHECK (from_category IS NULL OR {_in('from_category', WRITABLE_CATEGORIES)}), "
    f"to_category TEXT NOT NULL CHECK ({_in('to_category', WRITABLE_CATEGORIES)}), "
    "who TEXT NOT NULL CHECK (length(trim(who)) > 0), why TEXT NOT NULL CHECK (length(trim(why)) > 0), "
    f"source TEXT NOT NULL CHECK ({_in('source', JOURNAL_SOURCES)}), applied_by TEXT, "
    "lease_token INTEGER NOT NULL, "
    f"CHECK ((source = 'operator-proposal' AND applied_by = {SQL_ROLE_DESK}) "
    "OR (source != 'operator-proposal' AND applied_by IS NULL)), "
    "CHECK ((ver = 1) = (from_category IS NULL)), PRIMARY KEY (id, ver))",
    # the item's state after each transition; current state is its latest ver
    "CREATE TABLE IF NOT EXISTS ready_items (id TEXT NOT NULL, ver INTEGER NOT NULL CHECK (ver >= 1), "
    "repo TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL, "
    f"category TEXT NOT NULL CHECK ({_in('category', WRITABLE_CATEGORIES)}), "
    f"tier TEXT CHECK (tier IS NULL OR {_in('tier', TIERS)}), done_line TEXT, depends_on TEXT NOT NULL, "
    f"rank INTEGER NOT NULL CHECK (rank >= 0), set_by TEXT NOT NULL, set_at TEXT NOT NULL{ITEM_FACTS_SQL}, "
    "PRIMARY KEY (id, ver))",
    *_append_only("ready_journal", "the ready journal"),
    *_append_only("ready_items", "a ready item state"),     # no quote: it goes into SQL
    # INSERT OR REPLACE deletes the old row without firing a delete trigger: refuse a new row reusing any key
    "CREATE TRIGGER IF NOT EXISTS ready_journal_no_replace BEFORE INSERT ON ready_journal WHEN EXISTS (SELECT 1 FROM "
    "ready_journal WHERE (id = NEW.id AND ver = NEW.ver) OR gseq = NEW.gseq OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a journal record is never replaced'); END",
    "CREATE TRIGGER IF NOT EXISTS ready_items_no_replace BEFORE INSERT ON ready_items WHEN EXISTS (SELECT 1 FROM "
    "ready_items WHERE (id = NEW.id AND ver = NEW.ver) OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a ready item state is never replaced'); END",
    # a record is the NEXT transition of its item: one past both the journal and the state, from its current category,
    # in its own repo. So the journal never runs ahead of the state by more than the one record being applied.
    "CREATE TRIGGER IF NOT EXISTS ready_journal_next BEFORE INSERT ON ready_journal "
    "WHEN NEW.ver != COALESCE((SELECT MAX(ver) FROM ready_journal WHERE id = NEW.id), 0) + 1 "
    "OR NEW.ver != COALESCE((SELECT MAX(ver) FROM ready_items WHERE id = NEW.id), 0) + 1 "
    "OR NEW.from_category IS NOT (SELECT category FROM ready_items WHERE id = NEW.id ORDER BY ver DESC LIMIT 1) "
    "OR NEW.from_status IS NOT (SELECT status FROM ready_items WHERE id = NEW.id ORDER BY ver DESC LIMIT 1) "
    "OR NEW.repo != COALESCE((SELECT repo FROM ready_items WHERE id = NEW.id ORDER BY ver LIMIT 1), NEW.repo) "
    "BEGIN SELECT RAISE(ABORT, 'a journal record is the next transition of its item'); END",
    # a state change exists only beside its own journal record (SCHEMA §6: "a state change with no record is a defect")
    "CREATE TRIGGER IF NOT EXISTS ready_items_journaled BEFORE INSERT ON ready_items WHEN NOT EXISTS (SELECT 1 FROM "
    "ready_journal j WHERE j.id = NEW.id AND j.ver = NEW.ver AND j.to_category = NEW.category "
    "AND j.to_status = NEW.status AND j.repo = NEW.repo AND j.who = NEW.set_by) "
    "BEGIN SELECT RAISE(ABORT, 'a ready item state is written only with its journal record'); END",
    # THE ROLE SPLIT at the schema (the ruling's condition 1; see work_attach): a row the service wrote may change an
    # item in one way only, its landed sha and branch; it never adds an item, moves one, or changes any other field
    "CREATE TRIGGER IF NOT EXISTS ready_journal_service_facts BEFORE INSERT ON ready_journal WHEN NEW.who = 'service' "
    "AND (NEW.source != 'correction' OR NEW.ver = 1 OR NEW.from_category IS NOT NEW.to_category "
    "OR NEW.from_status IS NOT NEW.to_status) "
    "BEGIN SELECT RAISE(ABORT, 'the service records facts only: it never adds or moves an item (who service names the service alone; "
    f"a {ROLE_DESK} writes under another name)'); END",
    "CREATE TRIGGER IF NOT EXISTS ready_items_service_facts BEFORE INSERT ON ready_items WHEN NEW.set_by = 'service' "
    # a missing previous version (a gap) refuses too: there is nothing to compare the row with (row 156 L2 F2)
    "AND (NEW.ver = 1 OR NOT EXISTS (SELECT 1 FROM ready_items p WHERE p.id = NEW.id AND p.ver = NEW.ver - 1) "
    "OR EXISTS (SELECT 1 FROM ready_items p WHERE p.id = NEW.id AND p.ver = NEW.ver - 1 AND "
    "(p.repo IS NOT NEW.repo OR p.title IS NOT NEW.title OR p.status IS NOT NEW.status OR p.category IS NOT "
    "NEW.category OR p.tier IS NOT NEW.tier OR p.done_line IS NOT NEW.done_line OR p.depends_on IS NOT NEW.depends_on "
    "OR p.rank IS NOT NEW.rank OR p.hold IS NOT NEW.hold OR p.residue IS NOT NEW.residue OR p.ref IS NOT NEW.ref))) "
    "BEGIN SELECT RAISE(ABORT, 'the service records facts only: it changes the landed sha and branch of an item, "
    f"nothing else (who service names the service alone; a {ROLE_DESK} writes under another name)'); END",
)


# ------------------------------------------------------------------------------------------------ pure derivation
def slug(title: str) -> str:
    """SCHEMA §5: lower-case, every run of non-alphanumerics one ``-``, trimmed."""
    return re.sub(r"[^a-z0-9]+", "-", str(title).lower()).strip("-")


def _one_line(text: Any, limit: int = 120) -> str:
    s = str(text or "").strip()
    return s.splitlines()[0][:limit] if s else ""


def _category(raw: Any) -> str:
    """The set is closed: an unknown category reads as ``backlog``."""
    return raw if raw in CATEGORIES else "backlog"


def _repo_of_pointer(pointer: str, repos: Iterable[str] | None, home: str | None) -> str | None:
    """The first path component of a handoff pointer (an absolute one made relative to ``<home>/Briefcase`` first;
    outside it, no repo); a worktree folder (``.repo-<seat>``) names its repo before the first dash. With ``repos``
    given, a first component outside it is flow's own tree (flow's rule, without flow's directory check: pure)."""
    p = str(pointer or "").strip()
    if not p:
        return None
    if p.startswith("~/"):
        p = "/HOME/" + p[2:] if home is None else f"{home.rstrip('/')}/{p[2:]}"
    if p.startswith("/"):
        base = (home.rstrip("/") if home is not None else "/HOME") + "/Briefcase/"
        if not p.startswith(base):
            return None
        p = p[len(base):]
    first = p.split("/", 1)[0]
    if first.startswith("."):
        return first.lstrip(".").split("-", 1)[0] or None
    if first and repos is not None and first not in set(repos):
        return "flow"
    return first or None


def _item(*, id: str, repo: str, title: Any, declared: str, status: str | None = None, done_line: Any = None,
          tier: Any = None, depends_on: Any = None, held_reason: str | None = None, needs_phill: bool = False,
          rank: int = 0, source: str, ref: str, set_by: str | None = None, set_at: str | None = None) -> dict:
    deps = [str(d) for d in depends_on] if isinstance(depends_on, (builtins.list, tuple)) else []
    return {
        "id": id,
        "repo": repo,
        "title": _one_line(title),
        "done_line": done_line or None,
        "tier": tier or None,
        "depends_on": deps,
        "status": status or declared,
        "status_category": declared,
        "declared_category": declared,
        "independent": False,
        "held_reason": held_reason,
        "needs_phill": bool(needs_phill),
        "well_formed": bool(done_line) and tier in TIERS,
        "rank": rank,
        "provenance": {"source": source, "ref": ref, "set_by": set_by, "set_at": set_at},
    }


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _work_item(row: Mapping[str, Any], *, source: str, ref: str, default_repo: str) -> dict:
    """A writer-owned item (a flow intention's ``work`` object, or a desk store row)."""
    raw = row.get("category")
    it = _item(id=str(row["id"]), repo=str(row.get("repo") or default_repo), title=row.get("title"),
               declared=_category(raw), status=row.get("status") if raw in CATEGORIES else None,
               done_line=row.get("done_line"), tier=row.get("tier"), depends_on=row.get("depends_on"),
               held_reason=row.get("hold") or None,
               rank=_int(row.get("rank")), source=source, ref=ref, set_by=row.get("set_by"),
               set_at=row.get("set_at"))
    it["unknown_category"] = raw if raw is not None and raw not in CATEGORIES else None
    return it


def _plan_title(text: str) -> str:
    m = re.search(r"\*\*(.+?)\*\*", text)
    return (m.group(1) if m else text).strip()


def _plan_items(rows: Sequence[Mapping[str, Any]]) -> builtins.list:
    ids = {(r.get("repo"), r.get("n")): f"{r.get('repo') or 'levain'}:{slug(_plan_title(str(r.get('text') or '')))}"
           for r in rows}
    out = []
    for r in rows:
        text = str(r.get("text") or "")
        deps, held = [], None
        am = _AFTER_N_RE.search(text)
        if am:
            dep = ids.get((r.get("repo"), int(am.group(1))))
            if dep:
                deps.append(dep)
        else:
            om = _OTHER_MARK_RE.search(text)
            if om:
                held = "design first" if om.group(0).lower() == "design first" else om.group(0)
        out.append(_item(id=ids[(r.get("repo"), r.get("n"))], repo=str(r.get("repo") or "levain"),
                         title=_plan_title(text) or text, declared="ready", depends_on=deps, held_reason=held,
                         rank=_int(r.get("n")), source="plan", ref=f"STATUS_1_0.md#LANE MENU:{r.get('n')}"))
    return out


def _names(item_id: str, text: str) -> bool:
    """Does ``text`` name ``item_id`` as a whole id? A token boundary by the id alphabet (``A-Z a-z 0-9 + . _ : -``):
    no id character before it, and after it no run of id characters that continues it into a longer id
    (``levain:gate`` is not named by ``levain:gate-two``, ``levain:gate:2``, ``levain:gate.v2``, ``levain:gate_x``).
    Trailing punctuation that continues nothing (``levain:gate.`` ending a sentence, ``levain:gate:`` before a
    reason) still names it: a missed needs-you is the fail-open direction."""
    return re.search(r"(?<![A-Za-z0-9_:+.-])" + re.escape(item_id) + r"(?![A-Za-z0-9_:+.-]*[A-Za-z0-9_+])",
                     text) is not None


def _sort_key(item: Mapping[str, Any]) -> tuple:
    return (item["repo"], CATEGORIES.index(item["status_category"]), item["rank"], item["id"])


def derive_items(sources: Mapping[str, Any]) -> builtins.list:
    """Every item, SCHEMA §§2-5, in the §4 rule 3 order (repo, column, rank, id). Pure: reads only ``sources``.

    ``sources`` keys (each optional): ``store`` (desk store rows: id, repo, title, category, status, tier,
    done_line, depends_on, rank, set_by, set_at, ref), the intentions under flow's input key (``READY_SOURCE_KEY``) + ``resolved`` (flow), ``plan`` (LANE MENU rows),
    ``needs_phill`` (open band rows: id, repo, what, stopped), ``seats`` (token, repo, name), ``ci`` (id, repo,
    branch), ``repos`` (known repo names, for a handoff pointer), ``home`` (the home directory, for one)."""
    resolved = {str(r) for r in sources.get("resolved") or ()}
    items: builtins.list = []
    seen: set[str] = set()

    def keep(it: dict) -> None:
        # a writer-owned item shadows a later source's item of the same id (the store replaces the plan parse)
        if it["id"] not in seen:
            seen.add(it["id"])
            items.append(it)

    for row in sources.get("store") or ():
        if isinstance(row, Mapping) and row.get("id"):
            keep(_work_item(row, source="plan", ref=str(row.get("ref") or f"dispatch.db#ready_items/{row['id']}"),
                            default_repo="levain"))
    handoff_order = 0
    for s in sources.get(READY_SOURCE_KEY) or ():
        if not isinstance(s, Mapping) or not s.get("id") or s.get("status", "open") != "open":
            continue
        work = s.get("work")
        if isinstance(work, Mapping):
            keep(_work_item({**work, "id": s["id"], "title": s.get("text"), "set_by": work.get("set_by"),
                             "set_at": work.get("set_at")},
                            source="intentions", ref=f"state/intentions.json#{s['id']}", default_repo="flow"))
        elif s.get("disposition") == "handoff" and isinstance(s.get("pointer"), str):
            repo = _repo_of_pointer(s["pointer"], sources.get("repos"), sources.get("home"))
            if repo:
                handoff_order += 1
                keep(_item(id=str(s["id"]), repo=repo, title=s.get("text"), declared="ready",
                           rank=HANDOFF_RANK_BASE + handoff_order, source="intentions", ref=s["pointer"]))
    for it in _plan_items([r for r in sources.get("plan") or () if isinstance(r, Mapping)]):
        keep(it)

    known = {it["id"]: it for it in items}

    def is_done(dep: str) -> bool:
        if dep in resolved:
            return True
        it = known.get(dep)
        return it is not None and it["declared_category"] == "done"

    projections: builtins.list = []
    for row in sources.get("needs_phill") or ():
        if not isinstance(row, Mapping) or not row.get("id"):
            continue
        text = f"{row.get('what') or ''} {row.get('stopped') or ''}"
        projections.append(_item(id=str(row["id"]), repo=str(row.get("repo") or "flow"), title=row.get("what"),
                                 declared="needs-you", needs_phill=True, rank=0, source="needs_phill",
                                 ref="state/needs_phill.jsonl"))
        for it in items:
            if _names(it["id"], text):
                it["needs_phill"] = True

    for it in items:
        if it["needs_phill"]:                       # §4 rule 2: needs-you wins
            it["status_category"] = it["status"] = "needs-you"
            it["held_reason"] = None
        elif it["declared_category"] == "ready":    # §4 rule 1: a dependency holds it back
            unmet = next((d for d in it["depends_on"] if not is_done(d)), None)
            if unmet is not None:
                it["held_reason"] = it["held_reason"] or f"depends on {unmet}"
            if it["held_reason"]:
                it["status_category"] = it["status"] = "backlog"
        it["independent"] = it["status_category"] == "ready" and it["held_reason"] is None

    for seat in sources.get("seats") or ():
        if isinstance(seat, Mapping) and seat.get("token"):
            repo = seat.get("repo")
            projections.append(_item(id=f"seat:{seat['token']}", repo="flow" if repo is None else str(repo),
                                     title=seat.get("name") or seat["token"],
                                     declared="waiting" if seat.get("declared") == "waiting" else "running", source="seat",
                                     ref="roster"))
    for ci in sources.get("ci") or ():
        if isinstance(ci, Mapping) and ci.get("id"):
            projections.append(_item(id=f"ci:{ci['id']}", repo=str(ci.get("repo") or "flow"),
                                     title=f"CI wait {ci.get('branch') or ci['id']}", declared="waiting",
                                     source="ci", ref=f"state/land_waiter/{ci['id']}.json"))
    return sorted(items + projections, key=_sort_key)


def derive(sources: Mapping[str, Any], *, generated: str | None = None,
           unreadable: Sequence[str] = ()) -> dict:
    """SCHEMA §3's list shape over ``sources``: ``{"schema": 1, "generated", "items", "unreadable"}``, every
    category (the ready-only filter is :func:`list_ready`'s). Pure: ``generated`` is the caller's stamp."""
    return {"schema": SCHEMA_VERSION, "generated": generated, "items": derive_items(sources),
            "unreadable": [*unreadable]}


def ready_counts(items: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Well-formed effectively-ready items per repo (§4 rule 4: healthy at :data:`HEALTHY_AT`). A count, no rule."""
    out: dict[str, int] = {}
    for it in items:
        if it.get("status_category") == "ready" and it.get("well_formed"):
            out[it["repo"]] = out.get(it["repo"], 0) + 1
    return out


# ------------------------------------------------------------------------------------------------ reads
_CURRENT = ("SELECT i.id, i.ver, i.repo, i.title, i.status, i.category, i.tier, i.done_line, i.depends_on, i.rank, "
            "i.set_by, i.set_at FROM ready_items i "
            "WHERE i.ver = (SELECT MAX(ver) FROM ready_items WHERE id = i.id)")
_STATE_FIELDS = ("repo", "title", "status", "category", "tier", "done_line", "depends_on", "rank")
_JOURNAL_FIELDS = ("ts", "iso", "id", "repo", "from_status", "to_status", "from_category", "to_category", "who",
                   "why", "source", "applied_by")


_TABLES = ("ready_items", "ready_journal")


class JournalAhead(CoordError):
    """An item's journal and its state rows disagree on its version (a journal record committed without its state
    row: only a direct-SQL writer can do that, never the API's one transaction). Nothing is healed here: the repair
    is a desk-side correction of the store, made knowingly."""

    def __init__(self, item_id: str, journal_ver: int, state_ver: int) -> None:
        self.item_id, self.journal_ver, self.state_ver = item_id, journal_ver, state_ver
        super().__init__(f"{item_id}: its journal is at version {journal_ver} but its state is at version "
                         f"{state_ver}; the store was written outside the lifecycle writer. Nothing was written; the "
                         f"repair is a desk-side correction of the store, never a silent heal")

    @property
    def tag(self) -> str:
        """Its entry in a list's ``unreadable``."""
        return f"{UNREADABLE_DESK}:{self.item_id} (journal v{self.journal_ver}, state v{self.state_ver})"


def _lifecycle_present(conn: sqlite3.Connection) -> bool:
    """Both lifecycle tables are in the file (True) or neither is (False: a store made before C13, which every reader
    reads as current and as no items). One without the other is a damaged store, raised."""
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name IN (?, ?)",
                                       _TABLES)}
    if have and len(have) != len(_TABLES):
        raise CoordError(f"the desk store holds {', '.join(sorted(have))} without the rest of the lifecycle "
                         "tables")
    return bool(have)


def _diverged(conn: sqlite3.Connection, item_id: str | None = None) -> builtins.list:
    """Every item (or ``item_id``) whose latest journal version is not its latest state version."""
    if not _lifecycle_present(conn):
        return []
    q = ("SELECT j.id, MAX(j.ver), COALESCE((SELECT MAX(ver) FROM ready_items WHERE id = j.id), 0) FROM ready_journal j"
         + ("" if item_id is None else " WHERE j.id = ?") + " GROUP BY j.id ORDER BY j.id")
    rows = conn.execute(q, () if item_id is None else (item_id,)).fetchall()
    return [JournalAhead(i, jv, sv) for i, jv, sv in rows if jv != sv]


def _deps(raw: Any, item_id: str) -> builtins.list:
    """A state row's ``depends_on``: a JSON array of item ids, or the row is unreadable (never "no dependencies")."""
    deps = json.loads(raw)
    if not isinstance(deps, builtins.list) or not builtins.all(isinstance(d, str) for d in deps):
        raise ValueError(f"{item_id}'s depends_on is not a list of item ids")
    return deps


def _row(r: Sequence[Any]) -> dict:
    return {"id": r[0], "ver": r[1], "repo": r[2], "title": r[3], "status": r[4], "category": r[5], "tier": r[6],
            "done_line": r[7], "depends_on": _deps(r[8], r[0]), "rank": r[9], "set_by": r[10], "set_at": r[11],
            "ref": f"dispatch.db#ready_items/{r[0]}@{r[1]}"}


def _current(conn: sqlite3.Connection, item_id: str | None = None) -> builtins.list:
    if not _lifecycle_present(conn):
        return []
    if item_id is None:
        rows = [_row(r) for r in conn.execute(_CURRENT + " ORDER BY i.id")]
    else:
        rows = [_row(r) for r in conn.execute(_CURRENT + " AND i.id = ?", (item_id,))]
    for r in rows:      # the prose hold rides on the store row so a reader derives "held" from it
        r["hold"] = _facts(conn, r["id"])["hold"]
    return rows


def migrate_item_facts(conn: sqlite3.Connection) -> None:
    """The additive migration (run in the opener's schema transaction, as every other): ``ready_items`` gains
    :data:`FACT_COLS`, NULL on every row written before them. A file with no ``ready_items`` (made before C13) is left
    to the lifecycle tables' own creation, which already carries the columns."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(ready_items)")}
    if not have:
        return
    for c in FACT_COLS:
        if c not in have:
            conn.execute(f"ALTER TABLE ready_items ADD COLUMN {_FACT_DDL[c]}")


def _facts(conn: sqlite3.Connection, item_id: str) -> dict:
    """The fact columns of ``item_id``'s current state (None for each on a store not yet migrated, or no such item)."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(ready_items)")}
    if not set(FACT_COLS) <= have:
        return dict.fromkeys(FACT_COLS)
    row = conn.execute(f"SELECT {', '.join(FACT_COLS)} FROM ready_items WHERE id = ? ORDER BY ver DESC LIMIT 1",
                       (item_id,)).fetchone()
    return dict.fromkeys(FACT_COLS) if row is None else dict(zip(FACT_COLS, row))


def item_facts(store: CoordStore | Path | str, item_id: str) -> dict:
    """An item's fact columns (``hold``, ``residue``, ``ref``, ``landed_sha``, ``branch``), read on the read-only
    connection. They are kept off :func:`list_ready`'s item shape (that shape is flow's schema, unchanged). Refuses an
    id that is not in the store."""
    st = _store_of(store)

    def query(conn: sqlite3.Connection) -> dict:
        if not _current(conn, item_id):
            raise CoordError(f"{item_id} is not in the desk store")
        return _facts(conn, item_id)
    return _read(st, query)


def record_landed(conn: sqlite3.Connection, now: str, token: int, item_id: str, sha: str, branch: str | None, *,
                  by: str, why: str) -> int:
    """A ``landed`` fact on an item: a new state version identical to the current one but for ``landed_sha`` and
    ``branch``, with its journal record (same category, same status, source ``correction``, ``who`` = ``by``). Inside
    the caller's write transaction. Never moves an item: for the service (``by == "service"``) a schema trigger
    refuses any other change (:data:`levain.desk.work_attach.WORK_SCHEMA`). Returns the new version."""
    if not isinstance(sha, str) or _OID.fullmatch(sha) is None:
        raise CoordError("a landed sha is a full 40 or 64 hex object id")
    if branch is not None:
        branch = _line("branch", branch, 200)
    diverged = _diverged(conn, item_id)
    if diverged:
        raise diverged[0]
    found = _current(conn, item_id)
    if not found:
        raise CoordError(f"{item_id} is not in the desk store")
    prior = found[0]
    state = {**{k: prior[k] for k in ("repo", "title", "tier", "done_line", "depends_on", "rank")},
             **_facts(conn, item_id), "category": prior["category"], "status": prior["status"],
             "landed_sha": sha, "branch": branch}
    ver = prior["ver"] + 1
    ts, iso = _local_iso(now)
    rec = {"ts": ts, "iso": iso, "id": item_id, "repo": state["repo"], "from_status": prior["status"],
           "to_status": prior["status"], "from_category": prior["category"], "to_category": prior["category"],
           "who": by, "why": _line("why", why, _WHY_MAX), "source": "correction", "applied_by": None}
    _insert_journal(conn, rec, ver=ver, gseq=next_gseq(conn, now), at=now, token=token)
    _insert_state(conn, state, item_id=item_id, ver=ver, who=by, at=now)
    return ver


def _store_of(store: CoordStore | Path | str) -> CoordStore:
    return store if isinstance(store, CoordStore) else CoordStore(store)


def _read(store: CoordStore, query: Any) -> Any:
    """``query(conn)`` in one read transaction on the read-only connection: no write lock, no desk."""
    with read_transaction(store.open_readonly) as conn:
        return query(conn)


def _absent(store: CoordStore) -> bool:
    """After a read found no store: is there truly none (the directory or the file not there, ``FileNotFoundError``
    on ``lstat``)? Anything else there (a dangling link, a directory that cannot be traversed, a file holding no
    current desk store) is a store that cannot be read, never "nothing waiting"."""
    for p in (store.directory, store.path):
        try:
            os.lstat(p)
        except FileNotFoundError:
            return True
        except OSError:
            return False
    return False


_READ_ERRORS = (StoreAbsent, sqlite3.Error, OSError, ValueError, CoordError, DispatchError)


def list_ready(store: CoordStore | Path | str, *, all: bool = False, repo: str | None = None,
               sources: Mapping[str, Any] | None = None, generated: str | None = None,
               conn: sqlite3.Connection | None = None) -> dict:
    """The ready queue (SCHEMA §3 shape): the store's items plus any caller-read ``sources`` (the band's open rows,
    the seat roster, CI waits: C12 reads those), derived. Without ``all``, only effectively-ready items. A store that
    cannot be read is named in ``unreadable`` (never "nothing waiting"); a store that was never made is no items.
    ``conn``: a read-only connection whose read transaction the caller already holds; the board is read on it (no
    second open, one snapshot with the caller's other sections). Default ``None``: the board opens its own.

    With ``conn`` the board is a pure function of the store and ``generated``, so ``generated`` is REQUIRED (a
    ``CoordError`` otherwise: the board must be stamped by the caller, never by this function's wall clock). ``conn`` is
    trusted: it is not checked to be read-only or to be this store's, and the caller does the table-presence check
    (a connection whose database has no board tables reads as an EMPTY board, NOT as ``unreadable``: only the default
    path's ``open_readonly`` names a missing or foreign store, so a caller that holds the connection opened it through
    ``store.open_readonly`` and has already had that answer)."""
    if conn is not None and not generated:
        raise CoordError("list_ready(conn=) needs generated: the board must be stamped by the caller")
    st = _store_of(store)
    merged: dict[str, Any] = dict(sources or {})
    given = merged.pop("unreadable", ())
    unreadable = [given] if isinstance(given, str) else [*given]
    rows: builtins.list = []
    try:
        read = lambda c: (_current(c), _diverged(c))  # noqa: E731
        rows, diverged = read(conn) if conn is not None else _read(st, read)
        unreadable.extend(d.tag for d in diverged)      # shown at its state, and named: a write to it will refuse
    except StoreAbsent:
        if not _absent(st):
            unreadable.append(UNREADABLE_DESK)
    except _READ_ERRORS:
        unreadable.append(UNREADABLE_DESK)
    merged["store"] = [*rows, *(merged.get("store") or ())]
    stamp = generated or datetime.now().astimezone().isoformat(timespec="seconds")
    out = derive(merged, generated=stamp, unreadable=unreadable)
    if not all:
        out["items"] = [i for i in out["items"] if i["status_category"] == "ready"]
    if repo is not None:
        out["items"] = [i for i in out["items"] if i["repo"] == repo]
    return out


SEATS_SOURCE = "seats"
JOURNAL_SOURCE = "journal"


def _seat_rows(conn: sqlite3.Connection, now: str) -> builtins.list:
    """The live seats (SCHEMA section 5's roster projection) from the launch rows the desk already keeps: every launch
    whose latest event is ``active``. ``token`` is the launch id, ``name`` says harness, role and thread.

    Liveness is the lease's own reading (:func:`levain.desk.lease.lease`), not a second rule: a desk launch is
    ``running`` only while the lease stands live and is HELD BY ITS SESSION; one whose lease lapsed or is held by another
    session has no liveness evidence and is projected ``waiting`` ("no live lease"), never ``running``. A worker holds no
    lease and the store keeps no heartbeat for it: the watcher ends a launch whose session is no longer listed
    (``end_unlisted``), so for a worker ``active`` is that watcher's reading, shown as is.

    ``repo`` is ``""``: a launch records no repository (``cwd`` is an arbitrary execution directory, its basename wrong for a
    nested directory or a worktree), and no wrong repo is better than a guessed one. A store before the launch tables reads as
    no seats."""
    from levain.desk import lease

    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'launch_events'").fetchone() is None:
        return []
    held = lease.lease(conn, now)
    out = []
    for ln in lease._in_state(conn, "active"):
        seat = {"token": ln.launch_id, "repo": "", "name": " ".join([str(ln.harness), str(ln.role), *map(str, ln.threads)])}
        if ln.role == ROLE_DESK and not (held is not None and held.live and held.holder == ln.session_key):
            seat["declared"] = "waiting"
            seat["name"] += " (no live lease)"
        out.append(seat)
    return out


def snapshot(store: CoordStore | Path | str, *, sources: Mapping[str, Any] | None = None, generated: str | None = None) -> dict:
    """ONE consistent read of everything the board draws: :func:`list_ready` (every category) plus the journal's records
    (``journal``, store order) plus the live seats as ``running`` projections (SCHEMA section 5), all in ONE read transaction, so a
    card's journal line is never newer than its column: a journal record newer than its item's state row (journal ahead of
    state) is left out of ``journal``, and the item is named in ``unreadable`` as :func:`list_ready` names it. A source that
    cannot be read is named in ``unreadable`` (the seat roster as ``seats``, the journal as ``journal``: the cards stay, without
    their journal lines), never an empty column. No desk needed."""
    st = _store_of(store)
    merged: dict[str, Any] = dict(sources or {})
    given = merged.pop("unreadable", ())
    unreadable = [given] if isinstance(given, str) else [*given]
    rows: builtins.list = []
    records: builtins.list = []
    seats: builtins.list = []
    cols = ", ".join(_JOURNAL_FIELDS)
    now = st._stamp()

    def q(conn: sqlite3.Connection) -> tuple:
        cur = _current(conn)
        try:
            div = _diverged(conn)       # reads the journal too: a damaged journal degrades to "journal", the cards stay
            recs = ([(r[0], dict(zip(_JOURNAL_FIELDS, r[1:])))
                     for r in conn.execute(f"SELECT ver, {cols} FROM ready_journal ORDER BY gseq")]
                    if _lifecycle_present(conn) else [])
        except (sqlite3.Error, ValueError, CoordError):
            div, recs = [], None
        try:
            seat = _seat_rows(conn, now)     # one malformed launch row names "seats" for the lot (named degradation, not a blank board)
        except (sqlite3.Error, ValueError, CoordError, TypeError, AttributeError):
            seat = None
        return cur, div, recs, seat

    try:
        rows, diverged, recs, seat_rows = _read(st, q)
        unreadable.extend(d.tag for d in diverged)
        if recs is None:
            unreadable.append(JOURNAL_SOURCE)
        else:
            state_ver = {r["id"]: r["ver"] for r in rows}
            records = [rec for ver, rec in recs if ver <= state_ver.get(rec["id"], ver)]
        if seat_rows is None:
            unreadable.append(SEATS_SOURCE)
        else:
            seats = seat_rows
    except StoreAbsent:
        if not _absent(st):
            unreadable.append(UNREADABLE_DESK)
    except _READ_ERRORS:
        unreadable.append(UNREADABLE_DESK)
    merged["store"] = [*rows, *(merged.get("store") or ())]
    merged["seats"] = [*seats, *(merged.get("seats") or ())]
    stamp = generated or datetime.now().astimezone().isoformat(timespec="seconds")
    out = derive(merged, generated=stamp, unreadable=unreadable)
    out["journal"] = records
    return out


list = list_ready       # the contract's verb (`ready.py list`); nothing in this module calls the builtin by name


def journal(store: CoordStore | Path | str, item_id: str | None = None) -> builtins.list:
    """The journal's records (SCHEMA §6 fields, exactly), in store order. Read-only, no desk needed. No store,
    or a store made before C13, has none (``[]``, as :func:`list_ready` has no items); a store that cannot be read
    raises."""
    cols = ", ".join(_JOURNAL_FIELDS)

    def q(conn: sqlite3.Connection) -> builtins.list:
        if not _lifecycle_present(conn):
            return []
        if item_id is None:
            cur = conn.execute(f"SELECT {cols} FROM ready_journal ORDER BY gseq")
        else:
            cur = conn.execute(f"SELECT {cols} FROM ready_journal WHERE id = ? ORDER BY gseq", (item_id,))
        return [dict(zip(_JOURNAL_FIELDS, r)) for r in cur]

    st = _store_of(store)
    try:
        return _read(st, q)
    except StoreAbsent:
        if _absent(st):
            return []
        raise


def replay(records: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Every item's ``declared_category`` from the journal alone (SCHEMA §6: the backstop if the store is lost)."""
    out: dict[str, str] = {}
    for rec in records:
        out[str(rec["id"])] = str(rec["to_category"])
    return out


# ------------------------------------------------------------------------------------------------ the one writer
def _line(name: str, value: Any, high: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CoordError(f"{name} is required, never empty")
    value = value.strip()
    # printable only: no line break of any kind (\x0b, \x85, \u2028 ...), no escape sequence for a terminal
    if len(value) > high or not value.isprintable():
        raise CoordError(f"{name} is one line of at most {high} printable characters")
    return value


def _check_token(token: Any) -> int:
    if token is None:
        raise CoordError("a lifecycle write needs the desk's identity (the live lease token); no state "
                         "was written. Reads need none")
    if not isinstance(token, int) or isinstance(token, bool):
        raise CoordError("the desk's identity is the lease token, an integer")
    return token


def _check_to(to: Any) -> str:
    if to == "needs-you":
        raise CoordError("needs-you is a projection of the band (SCHEMA §2, §5): a move into it goes through the "
                         "band, never through the writer")
    if to not in WRITABLE_CATEGORIES:
        raise CoordError(f"unknown category {str(to)[:40]!r}; one of {', '.join(WRITABLE_CATEGORIES)}")
    return to


def _check_id(item_id: Any) -> str:
    if not isinstance(item_id, str) or _ITEM_ID.fullmatch(item_id) is None:
        raise CoordError("an item id is 1 to 200 of A-Z a-z 0-9 + . _ : - (e.g. levain:<slug>), never a position")
    if item_id.startswith(_PROJECTION_PREFIXES):
        raise CoordError(f"{item_id} is a projection id (seat:, ci:, np-): projections are never writable")
    return item_id


_UNSET = object()


def _fields(item_id: str, *, title: Any, tier: Any, done_line: Any, depends_on: Any, rank: Any,
            hold: Any = _UNSET, residue: Any = _UNSET, ref: Any = _UNSET) -> dict:
    """The optional state fields a caller set, checked; ``_UNSET`` ones are left out (kept from the current state)."""
    out: dict[str, Any] = {}
    if title is not _UNSET:
        out["title"] = _line("title", title, _TITLE_MAX)
    if tier is not _UNSET:
        if tier is not None and tier not in TIERS:
            raise CoordError(f"unknown tier {str(tier)[:20]!r}; one of {', '.join(TIERS)}")
        out["tier"] = tier
    if done_line is not _UNSET:
        out["done_line"] = None if done_line is None else _line("done_line", done_line, _DONE_MAX)
    if depends_on is not _UNSET:
        if isinstance(depends_on, str) or not isinstance(depends_on, (builtins.list, tuple)):
            raise CoordError("depends_on is a list of item ids")
        deps: builtins.list = []
        for d in depends_on:
            if not isinstance(d, str) or _ITEM_ID.fullmatch(d) is None:
                raise CoordError(f"a dependency is an item id (got {str(d)[:40]!r})")
            if d == item_id:
                raise CoordError(f"{item_id} cannot depend on itself")
            if d.startswith(_PROJECTION_PREFIXES):
                raise CoordError(f"{d} is a projection id (seat:, ci:, np-): never a dependency, it is never done")
            if d not in deps:
                deps.append(d)
        if len(deps) > _DEPENDS_MAX:
            raise CoordError(f"at most {_DEPENDS_MAX} dependencies")
        out["depends_on"] = deps
    for name, value, high in (("hold", hold, _WHY_MAX), ("residue", residue, _WHY_MAX), ("ref", ref, _REF_MAX)):
        if value is not _UNSET:
            out[name] = None if value is None else _line(name, value, high)
    if rank is not _UNSET and rank is not None:
        if not isinstance(rank, int) or isinstance(rank, bool) or not 0 <= rank <= 10**9:
            raise CoordError("rank is an integer from 0")
        out["rank"] = rank
    return out


def _local_iso(now: str) -> tuple[float, str]:
    when = parse_stamp(now)
    return when.timestamp(), when.astimezone().isoformat(timespec="seconds")


def _insert_journal(conn: sqlite3.Connection, rec: Mapping[str, Any], *, ver: int, gseq: int, at: str,
                    token: int) -> None:
    conn.execute(
        "INSERT INTO ready_journal (id, ver, gseq, ts, iso, at, repo, from_status, to_status, from_category, "
        "to_category, who, why, source, applied_by, lease_token) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rec["id"], ver, gseq, rec["ts"], rec["iso"], at, rec["repo"], rec["from_status"], rec["to_status"],
         rec["from_category"], rec["to_category"], rec["who"], rec["why"], rec["source"], rec["applied_by"], token))


def _insert_state(conn: sqlite3.Connection, state: Mapping[str, Any], *, item_id: str, ver: int, who: str,
                  at: str) -> None:
    conn.execute(
        "INSERT INTO ready_items (id, ver, repo, title, status, category, tier, done_line, depends_on, rank, set_by, "
        f"set_at, {', '.join(FACT_COLS)}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, {', '.join('?' * len(FACT_COLS))})",
        (item_id, ver, state["repo"], state["title"], state["status"], state["category"], state["tier"],
         state["done_line"], json.dumps(state["depends_on"]), state["rank"], who, at,
         *(state.get(c) for c in FACT_COLS)))


def _refuse_cycle(conn: sqlite3.Connection, item_id: str, deps: Sequence[str]) -> None:
    """Refuse ``depends_on`` that closes a cycle through the store's current items (read under the write lock):
    every item on it would be held forever."""
    graph = {r["id"]: r["depends_on"] for r in _current(conn)}
    stack, seen = [*deps], set()
    while stack:
        d = stack.pop()
        if d == item_id:
            raise CoordError(f"{item_id} depending on {', '.join(deps)} closes a dependency cycle back to itself")
        if d not in seen:
            seen.add(d)
            stack.extend(graph.get(d, ()))


class _Written(dict):
    """A journal record that also carries ``ver``: the version its own write transaction produced. Still exactly the
    SCHEMA §6 fields as a mapping."""

    def __init__(self, rec: Mapping[str, Any], *, ver: int) -> None:
        super().__init__(rec)
        self.ver = ver


def _transition(store: CoordStore, token: Any, item_id: Any, *, to: Any, who: Any, why: Any, source: str,
                creating: bool, repo: Any = None, **fields: Any) -> dict:
    """THE write path: checks, then ONE fenced write transaction that writes the journal record and then the state
    row; a failure anywhere rolls both back. Returns the journal record (SCHEMA §6 fields) as a :class:`_Written` (a plain dict with ``.ver``, the version this transaction wrote)."""
    token = _check_token(token)
    why = _line("why", why, _WHY_MAX)
    who = _line("who", who, _WHO_MAX)
    to = _check_to(to)
    item_id = _check_id(item_id)
    changes = _fields(item_id, **fields)
    if creating:
        if not isinstance(repo, str) or _REPO.fullmatch(repo) is None:
            raise CoordError("an item's repo is its project key (flow, levain, ...)")
        if "title" not in changes:
            raise CoordError("a new item needs a title")
    try:
        # a lifecycle write never makes a store: a desk holding a live lease always has one
        with store._write(ROLE_DESK, token, create=False) as (conn, now):
            if not _lifecycle_present(conn):        # a store made before C13: the write path makes the tables, in its txn
                for stmt in LIFECYCLE_SCHEMA:
                    conn.execute(stmt)
            diverged = _diverged(conn, item_id)     # loud and named, before a write that would fail on the journal's key
            if diverged:
                raise diverged[0]
            found = _current(conn, item_id)
            prior = found[0] if found else None
            if creating and prior is not None:
                raise CoordError(f"{item_id} already exists ({prior['category']}); move or correct it")
            if not creating and prior is None:
                raise CoordError(f"{item_id} is not in the desk store; add it first")
            facts = _facts(conn, item_id) if prior is not None else dict.fromkeys(FACT_COLS)
            if prior is None:
                rank = changes.get("rank")
                if rank is None:
                    top = conn.execute(f"SELECT MAX(rank) FROM ({_CURRENT}) WHERE repo = ?", (repo,)).fetchone()[0]
                    rank = 1 if top is None else top + 1
                state = {"repo": repo, "title": changes["title"], "tier": changes.get("tier"),
                         "done_line": changes.get("done_line"), "depends_on": changes.get("depends_on", []),
                         "rank": rank, **{c: changes[c] for c in FACT_COLS if c in changes}}
            else:
                state = {k: prior[k] for k in ("repo", "title", "tier", "done_line", "depends_on", "rank")}
                state.update(changes)
            if "depends_on" in changes:
                _refuse_cycle(conn, item_id, state["depends_on"])
            for c in FACT_COLS:         # a move carries every fact it was not asked to change
                state.setdefault(c, facts[c])
            state["category"] = state["status"] = to        # v1: the status IS the category name (§3)
            if prior is not None and all(state[k] == {**prior, **facts}[k] for k in (*_STATE_FIELDS, *FACT_COLS)):
                raise CoordError(f"{item_id} already reads so; nothing to write")
            ver = 1 if prior is None else prior["ver"] + 1
            ts, iso = _local_iso(now)
            rec = {"ts": ts, "iso": iso, "id": item_id, "repo": state["repo"],
                   "from_status": None if prior is None else prior["status"], "to_status": state["status"],
                   "from_category": None if prior is None else prior["category"], "to_category": to,
                   "who": who, "why": why, "source": source, "applied_by": None}
            _insert_journal(conn, rec, ver=ver, gseq=next_gseq(conn, now), at=now, token=token)
            _insert_state(conn, state, item_id=item_id, ver=ver, who=who, at=now)
    except StoreAbsent as exc:
        raise CoordError(f"no desk store at {Path(str(store.path)).parent}; no state was written") from exc
    return _Written(rec, ver=ver)           # the version THIS transaction wrote, never a later read of the journal


def add(store: CoordStore, token: Any, item_id: str, *, repo: str, title: str, who: str, why: str,
        to: str = "backlog", tier: Any = None, done_line: Any = None, depends_on: Any = (),
        rank: Any = None, hold: Any = _UNSET, residue: Any = _UNSET, ref: Any = _UNSET) -> dict:
    """A new item, in ``to`` (default ``backlog``), with its ``add`` journal record. Refused if the id exists."""
    return _transition(store, token, item_id, to=to, who=who, why=why, source="add", creating=True, repo=repo,
                       title=title, tier=tier, done_line=done_line, depends_on=[*depends_on]
                       if isinstance(depends_on, (builtins.list, tuple)) else depends_on, rank=rank,
                       hold=hold, residue=residue, ref=ref)


def move(store: CoordStore, token: Any, item_id: str, *, to: str, who: str, why: str, title: Any = _UNSET,
         tier: Any = _UNSET, done_line: Any = _UNSET, depends_on: Any = _UNSET, rank: Any = _UNSET,
         hold: Any = _UNSET, residue: Any = _UNSET, ref: Any = _UNSET) -> dict:
    """Move an existing item to ``to`` (and change any field given), with its ``move`` journal record."""
    return _transition(store, token, item_id, to=to, who=who, why=why, source="move", creating=False,
                       title=title, tier=tier, done_line=done_line, depends_on=depends_on, rank=rank,
                       hold=hold, residue=residue, ref=ref)


def correct(store: CoordStore, token: Any, item_id: str, *, to: str, who: str, why: str, title: Any = _UNSET,
            tier: Any = _UNSET, done_line: Any = _UNSET, depends_on: Any = _UNSET, rank: Any = _UNSET,
         hold: Any = _UNSET, residue: Any = _UNSET, ref: Any = _UNSET) -> dict:
    """Correct a wrong state: a NEW record with ``source: "correction"`` (§6), never an edit of an old one."""
    return _transition(store, token, item_id, to=to, who=who, why=why, source="correction", creating=False,
                       title=title, tier=tier, done_line=done_line, depends_on=depends_on, rank=rank,
                       hold=hold, residue=residue, ref=ref)
