"""levain.desk.board_context: ``levain board context NAME#N [--json]`` (T9; design section 6): what a seat on one item loads.

A READ, bounded to about 8K tokens. It prints five parts in order, each its own function so one part failing never
costs the others:

1. ``charter``: the repo's CHARTER.md on origin/main (path from the registry): Purpose, Criteria, Constraints in full and, from the Plan,
   only the group that holds the item (ids, titles and derived states). A group is a run of Plan lines with no blank line
   between them; today's Plan is flat, so the group is the whole Plan and the budget cuts it, nearest the item first.
2. ``item``: title, his words verbatim, origin, DONE line and ``done_kind``, each dependency with its derived state.
3. ``state``: the item's derived state and reason (the board's own :func:`~levain.desk.board_state.derive_all`, over the ONE
   repo's own git evidence) and the commits on main, chain refs and branches carrying its id.
4. ``record``: three named sources, each reporting for itself. ``record[anneal]``: the anneal store's episodes tagged
   ``item:NAME#N`` then the newest ``decision`` episodes tagged ``repo:NAME``, read the way :mod:`levain.agents.recall` reads
   (the window by TIME, then filter, then cap, ``truncated`` when the scan bound is hit with the cap unmet).
   ``record[flow]``: the same read of flow's own ``episodic.db`` through a READ-ONLY open, its tags read from flow's
   ``episode_tags`` table (T9-a; fix 1); optional, never required.
   ``record[artifacts]``: the record directory's ``pre-track-*`` directories and dated artifact names that carry the item, by name only.
5. ``conventions``: which of AGENTS.md, CLAUDE.md, CONTRIBUTING(.md) exist on origin/main, by name only.

A part that cannot be read prints ``<part>: unreadable (<why>)``, never empty. An unknown item id is refused naming it. An item
of a repo that is not tracked prints what can be read and says the repo is untracked.

The bound: :data:`PART_CHARS` caps each part's characters (tokens are counted as characters / 4); a cut part ends in a
``[truncated ...]`` line. The caps add to :data:`TOTAL_CHARS` or less, so the whole page is under 8K tokens before headers.

Where the stores are: ``--anneal PATH`` (else ``$LEVAIN_ANNEAL_DB``) and ``--flow-episodes PATH`` (else
``$LEVAIN_FLOW_EPISODES``). No existing producer or install config names a read-only episodic.db path, so these are explicit.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from levain.desk import board_cli as cli
from levain.desk import board_evidence as be
from levain.desk import board_producers as bp
from levain.desk import board_registry as br
from levain.desk import board_store as bs
from levain.desk import charter
from levain.desk.board_state import Evidence, derive_all, fold, parse_timestamp

__all__ = ["ANNEAL_ENV", "FLOW_ENV", "PART_CHARS", "TOTAL_CHARS", "register", "run_context"]

ANNEAL_ENV = "LEVAIN_ANNEAL_DB"
FLOW_ENV = "LEVAIN_FLOW_EPISODES"
#: 8K tokens at 4 characters a token is 32,000 characters; the part caps below sum to 30,000 and leave the rest for headers.
TOTAL_CHARS = 32_000
PART_CHARS = {"charter": 9_000, "item": 3_500, "state": 4_500, "record": 12_000, "conventions": 1_000}
WINDOW_DAYS = 90
MAX_SCAN = 50_000
ITEM_EPISODES = 10
DECISION_EPISODES = 5
EPISODE_CHARS = 300
COMMITS_SHOWN = 15
CONVENTIONS = ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING", "CONTRIBUTING.md")
_ID = re.compile(r"([a-z][a-z0-9_-]*)#([0-9]+)")
_DATED = re.compile(r"^(?:\d{4}-\d{2}-\d{2}|\d{8})")
_STAMP = "%Y-%m-%dT%H:%M:%S.%fZ"
EXIT_REFUSED = 2


@dataclass
class Part:
    name: str
    lines: list[str] = field(default_factory=list)
    status: str = "ok"              # ok | unreadable | unavailable
    why: str = ""
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"part": self.name, "status": self.status, "why": self.why, "truncated": self.truncated, "lines": self.lines}


def _unreadable(name: str, why: str) -> Part:
    return Part(name, [f"{name}: unreadable ({why})"], "unreadable", why)


def _cap(lines: list[str], budget: int) -> tuple[list[str], bool]:
    """``lines`` cut at ``budget`` characters; a cut ends in a ``[truncated ...]`` line that is itself inside the budget."""
    out, used = [], 0
    for i, ln in enumerate(lines):
        if used + len(ln) + 1 > budget - 60:
            rest = len(lines) - i
            return out + [f"[truncated: {rest} more line{'s' if rest != 1 else ''}]"], True
        out.append(ln)
        used += len(ln) + 1
    return out, False


def _clip(text: str, n: int) -> str:
    one = " ".join(str(text).split())
    return one if len(one) <= n else one[: n - 1] + "…"


# --------------------------------------------------------------------------------------------------- the derived board


@dataclass
class Derived:
    states: dict[str, Any]
    evidence: Evidence | None
    why: str = ""                   # non-empty: no derived state could be made


def _derive_one_repo(folded: Any, repo_path: Path, repo_name: str, target: bs.Target, now: datetime,
                     no_land_waiter: bool = False) -> Derived:
    """The board's derive over ONE repo's own evidence: the same git read, producers and :func:`derive_all` that
    ``run_state`` uses, in its cross-repo form (so a dependency in a repo this read did not open reads ``unknown``, never a
    guess). The assembly is a re-statement of run_state's lines, not a second derivation; T3 may factor them."""
    store_id = cli._store_id(target.store_dir)
    evidence, _summary = be.read_git_evidence(repo_path, repo_name=repo_name)
    mine = {i: it for i, it in folded.items.items() if it.repo == repo_name}
    evidence["documents"] = be.read_documents(mine, repo_path)
    native = bp.native_blocks(now=now, store_id=store_id, no_land_waiter=no_land_waiter)
    external, failures = bp.read_external(target.store_dir, [])
    cli._attach_producers(evidence, native, external, failures)
    primary = Evidence.from_dict(evidence, now=now, store_id=store_id)
    board = derive_all(folded, primary, now=now, repo_evidence={repo_name: primary}, primary_repo=repo_name)
    return Derived({s.id: s for s in board.items}, primary)


# --------------------------------------------------------------------------------------------------------------- parts


def _sections(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    cur: list[str] | None = None
    for ln in text.split("\n"):
        if ln.startswith("## "):
            h = ln[3:].strip()
            cur = out.setdefault(h, []) if h in charter.SECTIONS else None
            continue
        if cur is not None:
            cur.append(ln)
    return out


def _trim_blank(lines: list[str]) -> list[str]:
    while lines and not lines[0].strip():
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    return lines


def _plan_group(plan: list[str], item_id: str) -> tuple[list[str], str]:
    """``(ids, note)``: the ids of the run of Plan lines (no blank line between) that holds ``item_id``."""
    groups: list[list[str]] = [[]]
    for ln in plan:
        m = charter._PLAN_ID.match(ln)
        if m:
            groups[-1].append(f"{m.group(1)}#{m.group(2)}")
        elif not ln.strip() and groups[-1]:
            groups.append([])
    for g in groups:
        if item_id in g:
            return g, ""
    return [], f"{item_id} is not on the charter's Plan"


def _charter_part(repo: Any, name: str, item_id: str, folded: Any, derived: Derived | None, repo_why: str) -> Part:
    if repo is None:
        return _unreadable("charter", repo_why)
    text, why = charter._read_ref(Path(repo.path), charter.DEFAULT_REF)
    if text is None:
        return _unreadable("charter", "no CHARTER.md on origin/main" if why == "absent" else why)
    sec = _sections(text)
    lines = ["charter (CHARTER.md on origin/main):"]
    for h in ("Purpose", "Criteria", "Constraints"):
        lines.append(f"## {h}")
        lines.extend(_trim_blank(sec.get(h, [])) or [f"{h}: unreadable (the section is missing)"])
    ids, note = _plan_group(sec.get("Plan", []), item_id)
    lines.append("## Plan (the group that holds this item)")
    if note:
        lines.append(note)
    # nearest the item first, so a cut keeps what is next to it
    pos = ids.index(item_id) if item_id in ids else 0
    for pid in sorted(ids, key=lambda p: (abs(ids.index(p) - pos), ids.index(p))):
        it = folded.items.get(pid)
        st = derived.states.get(pid) if derived is not None else None
        lines.append(f"- {pid}  {st.state if st else 'state unreadable'}  {_clip(it.title, 100) if it else '(not in the store)'}")
    out, cut = _cap(lines, PART_CHARS["charter"])
    return Part("charter", out, truncated=cut)


def _item_part(item: Any, derived: Derived | None) -> Part:
    lines = [f"item {item.id}: {item.title}",
             f"words: {item.words or '(none given)'}",
             f"origin: {item.origin}",
             f"done: {item.done or '(none)'}  done_kind: {item.done_kind}"]
    if item.decision is not None:
        lines.append(f"decision: {item.decision.decision} ({item.decision.date}): {item.decision.words}")
    if not item.depends_on:
        lines.append("depends on: nothing")
    for link in item.depends_on:
        if link.kind == "item" and link.ref:
            st = derived.states.get(link.ref) if derived is not None else None
            lines.append(f"depends on: {link.label}: {st.state if st else 'state unreadable'}")
        else:
            lines.append(f"depends on: {link.label}")
    out, cut = _cap(lines, PART_CHARS["item"])
    return Part("item", out, truncated=cut)


def _state_part(item_id: str, derived: Derived) -> Part:
    if derived.why:
        return _unreadable("state", derived.why)
    st = derived.states.get(item_id)
    if st is None:
        return _unreadable("state", "the derive did not return the item")
    ev = derived.evidence
    lines = [f"state: {st.state} ({st.column}): {st.reason}"]
    if ev is None:
        lines.append("commits: unreadable (no evidence)")
    else:
        for src, why in sorted(ev.unreadable.items()):
            lines.append(f"{src}: unreadable ({why})")
        main = [c for c in ev.main if item_id in c.items or item_id in c.closes]
        lines.append(f"commits on main: {len(main)}")
        lines.extend(f"  {c.sha[:9]} {c.date or '-'}{' closes' if item_id in c.closes else ''}" for c in main[:COMMITS_SHOWN])
        if len(main) > COMMITS_SHOWN:
            lines.append(f"  [truncated: {len(main) - COMMITS_SHOWN} more commits]")
        for ch in ev.chain:
            n = sum(1 for c in ch.commits if item_id in c.items)
            if n:
                lines.append(f"chain {ch.ref}: {n} commit{'s' if n != 1 else ''}")
        for b in ev.branches:
            if item_id in b.items:
                lines.append(f"branch {b.name}: tip {b.tip[:9]}, {b.ahead} ahead")
    out, cut = _cap(lines, PART_CHARS["state"])
    return Part("state", out, truncated=cut)


@dataclass(frozen=True)
class _Ep:
    at: str
    type: str
    source: str
    content: str
    tags: tuple[str, ...]


def _tags_of(meta: Any) -> tuple[str, ...]:
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, RecursionError):
            raise ValueError("metadata is not JSON") from None
    if meta is None:
        return ()
    if not isinstance(meta, dict):
        raise ValueError("metadata is not a mapping")
    tags = meta.get("tags")
    return tuple(t for t in tags if isinstance(t, str)) if isinstance(tags, (list, tuple)) else ()


def _select(rows: list[_Ep], total: int, item_id: str, repo: str, unreadable: int) -> tuple[list[str], bool]:
    """Filter (rows are newest first), then cap, as :mod:`levain.agents.recall` does; ``truncated`` when the scan bound stopped
    the read and a cap was not met."""
    mine = [e for e in rows if f"item:{item_id}" in e.tags][:ITEM_EPISODES]
    item_hit = sum(1 for e in rows if f"item:{item_id}" in e.tags)
    decisions = [e for e in rows if e.type == "decision" and f"repo:{repo}" in e.tags][:DECISION_EPISODES]
    cut = total > len(rows) and (item_hit < ITEM_EPISODES or len(decisions) < DECISION_EPISODES)
    lines = [f"scanned {len(rows)} episodes in the last {WINDOW_DAYS} days" + (f"; {unreadable} with unreadable metadata" if unreadable else "")]
    lines.append(f"tagged item:{item_id}: {len(mine)}" + (" (newest first)" if mine else ""))
    lines.extend(f"  {e.at} {e.type} {e.source}: {_clip(e.content, EPISODE_CHARS)}" for e in mine)
    lines.append(f"decisions tagged repo:{repo}: {len(decisions)}" + (" (newest first)" if decisions else ""))
    lines.extend(f"  {e.at} {e.source}: {_clip(e.content, EPISODE_CHARS)}" for e in decisions)
    if cut:
        lines.append("[truncated: the scan bound was hit before the window was exhausted; an absence is not proof]")
    return lines, cut


def _window(now: datetime) -> str:
    return (now - timedelta(days=WINDOW_DAYS)).strftime(_STAMP)


def _read_anneal(path: Path, now: datetime) -> tuple[list[_Ep], int, int]:
    from anneal_memory import Store
    from levain.agents import recall as _recall
    with Store(path, read_only=True) as store:
        res = store.recall(since=_window(now), until=now.strftime(_STAMP), limit=MAX_SCAN)
    rows, bad = [], 0
    for ep in res.episodes:
        try:
            tags = _tags_of(_recall._metadata_of(ep))
        except ValueError:
            bad += 1
            continue
        t = getattr(ep.type, "value", ep.type)
        rows.append(_Ep(str(ep.timestamp), str(t), str(getattr(ep, "source", "")), str(ep.content), tags))
    return rows, res.total_matching, bad


def _read_flow(path: Path, now: datetime) -> tuple[list[_Ep], int, int]:
    """Flow's episodic.db opened READ-ONLY. ``mode=ro`` first (a live WAL store reads consistently); ``immutable=1`` only when
    that open cannot be made (a read-only directory); neither ever writes the file.

    Flow keeps tags in its own table, ``episode_tags(episode_id, tag)`` (``scripts/episodic.py`` writes them there; no flow
    writer puts tags in ``episodes.metadata``, so that path is not read). The window is an index-friendly lower bound on
    ``episodes.timestamp`` (flow stamps carry a local offset, so the bound is widened by the largest UTC offset, 14 hours,
    to never drop an episode at the edge); the newest MAX_SCAN rows are read, then only their tags."""
    base = "file:" + quote(str(path))
    last: Exception | None = None
    for q in ("?mode=ro", "?mode=ro&immutable=1"):
        try:
            conn = sqlite3.connect(base + q, uri=True, timeout=5)
            try:
                since = (now - timedelta(days=WINDOW_DAYS, hours=14)).strftime(_STAMP)
                total = conn.execute("SELECT COUNT(*) FROM episodes WHERE timestamp >= ?", (since,)).fetchone()[0]
                got = conn.execute("SELECT id, timestamp, type, source, content FROM episodes WHERE timestamp >= ?"
                                   " ORDER BY timestamp DESC LIMIT ?", (since, MAX_SCAN)).fetchall()
                tags: dict[str, list[str]] = {}
                ids = [r[0] for r in got]
                for i in range(0, len(ids), 500):
                    chunk = ids[i:i + 500]
                    for eid, tag in conn.execute("SELECT episode_id, tag FROM episode_tags WHERE episode_id IN (%s)"
                                                 % ",".join("?" * len(chunk)), chunk):
                        tags.setdefault(eid, []).append(tag)
            finally:
                conn.close()
            break
        except sqlite3.Error as exc:
            last = exc
    else:
        raise last or sqlite3.Error("cannot open")
    rows = [_Ep(str(ts), str(typ), str(src or ""), str(content), tuple(tags.get(eid, ()))) for eid, ts, typ, src, content in got]
    return rows, total, 0


def _source_lines(label: str, given: str | None, env: str, reader: Any, status: str, item_id: str, repo: str, now: datetime
                  ) -> tuple[list[str], bool, bool]:
    """``(lines, truncated, read)`` for one episode source. ``status`` is the word an absent source says (``unreadable`` for
    the anneal store, ``unavailable`` for flow's optional one)."""
    named = given or os.environ.get(env)
    if not named:
        return [f"record[{label}]: {status} (no path named: pass the flag or set ${env})"], False, False
    path = Path(named).expanduser()
    if not path.is_file():
        return [f"record[{label}]: {status} ({path} is not a file)"], False, False
    try:
        rows, total, bad = reader(path, now)
    except Exception as exc:  # noqa: BLE001 - an unreadable store is said, never shown as an empty record
        return [f"record[{label}]: {status} ({type(exc).__name__}: {_clip(str(exc), 120)})"], False, False
    lines, cut = _select(rows, total, item_id, repo, bad)
    return [f"record[{label}]:"] + ["  " + ln for ln in lines], cut, True


def _artifact_lines(repo: Any, item_id: str) -> list[str]:
    """Names only, from the record directory: the ``pre-track-*`` directories (the repo's archive of what tracking replaced) and
    the dated entries whose name carries the item (``NAME#N``, ``NAME-N`` or ``NAME_N``)."""
    if repo is None or not repo.record_dir:
        return ["record[artifacts]: unavailable (no record directory)"]
    base = Path(repo.record_dir).expanduser()
    try:
        names = sorted(os.listdir(base))
    except OSError as exc:
        return [f"record[artifacts]: unreadable ({base} cannot be listed: {type(exc).__name__})"]
    name, _, n = item_id.partition("#")
    forms = (item_id, f"{name}-{n}", f"{name}_{n}")
    hits: list[str] = []
    for e in names:
        if e.startswith("pre-track-"):
            hits.append(e)
        elif _DATED.match(e) and any(f in e for f in forms):
            hits.append(e)
    return [f"record[artifacts]: {base}"] + [f"  {h}" for h in hits] if hits else \
        [f"record[artifacts]: nothing in {base} names {item_id} (pre-track-* and dated artifacts)"]


def _record_part(repo: Any, name: str, item_id: str, args: argparse.Namespace, now: datetime) -> Part:
    lines: list[str] = ["record:"]
    cut = False
    a, c1, ok_a = _source_lines("anneal", args.anneal, ANNEAL_ENV, _read_anneal, "unreadable", item_id, name, now)
    f, c2, ok_f = _source_lines("flow", args.flow_episodes, FLOW_ENV, _read_flow, "unavailable", item_id, name, now)
    lines += a + f + _artifact_lines(repo, item_id)
    cut = c1 or c2
    out, c3 = _cap(lines, PART_CHARS["record"])
    part = Part("record", out, truncated=cut or c3)
    if not (ok_a or ok_f):
        part.status = "unreadable"
        part.why = "no episode source could be read"
    return part


def _conventions_part(repo: Any) -> Part:
    if repo is None:
        return _unreadable("conventions", "the repo is not in the registry, so no path is known")
    rc, out = br._git(repo.path, "ls-tree", "--name-only", charter.DEFAULT_REF)
    if rc != 0:
        return _unreadable("conventions", f"origin/main cannot be listed in {repo.path}")
    have = [n for n in CONVENTIONS if n in out.split("\n")]
    line = ("conventions: " + ", ".join(have)) if have else "conventions: none of AGENTS.md, CLAUDE.md, CONTRIBUTING(.md) exist on origin/main"
    return Part("conventions", [line])


# ------------------------------------------------------------------------------------------------------------- the verb


def _refuse(msg: str) -> int:
    print(f"levain board context: refused: {msg}", file=sys.stderr)
    return EXIT_REFUSED


def run_context(args: argparse.Namespace) -> int:
    m = _ID.fullmatch(args.item)
    if not m:
        return _refuse(f"{args.item!r} is not an item id (NAME#N)")
    name, item_id = m.group(1), args.item
    try:
        target = bs.resolve_store(args.items)
        lines = bs.read_item_lines(target)
    except bs.BoardError as exc:
        return _refuse(str(exc))
    except (OSError, UnicodeDecodeError) as exc:
        return _refuse(f"the items file cannot be read ({type(exc).__name__}: {exc})")
    now = datetime.now(timezone.utc) if args.now is None else parse_timestamp(args.now)
    if now is None:
        return _refuse(f"--now {args.now!r} is not an ISO-8601 timestamp")
    folded = fold(lines)
    item = folded.items.get(item_id)
    if item is None:
        return _refuse(f"no item {item_id} in the store")

    repo, repo_why, mode_line = None, "", ""
    try:
        view = br.read_registry(target, allow_inactive=True)
        repo = view.fold.repos.get(name)
        if repo is None:
            repo_why = f"{name} is not in the registry" if view.active else "the registry has no repos.jsonl yet"
            mode_line = f"repo {name} is untracked: {repo_why}"
        elif repo.mode != "tracked":
            mode_line = f"repo {name} is untracked (mode {repo.mode}); what follows is what can be read"
    except bs.BoardError as exc:
        repo_why = f"the registry is unreadable ({_clip(str(exc), 120)})"
        mode_line = f"repo {name}: {repo_why}"

    repo_path = Path(repo.path) if repo is not None else (Path(args.repo).expanduser() if args.repo else None)
    derived: Derived | None
    if repo_path is None:
        derived = Derived({}, None, f"{repo_why or 'no repository path'}; pass --repo PATH to read git")
    else:
        try:
            derived = _derive_one_repo(folded, repo_path, name, target, now, args.no_land_waiter)
        except Exception as exc:  # noqa: BLE001 - a derive that cannot be made is said as unreadable, never as an empty state
            derived = Derived({}, None, f"git evidence cannot be read ({type(exc).__name__}: {_clip(str(exc), 120)})")
    dv = derived if not derived.why else None

    parts = [_charter_part(repo, name, item_id, folded, dv, repo_why),
             _item_part(item, dv),
             _state_part(item_id, derived),
             _record_part(repo, name, item_id, args, now),
             _conventions_part(repo)]
    if args.json:
        print(json.dumps({"item": item_id, "repo": name, "untracked": bool(mode_line), "note": mode_line or None,
                          "parts": [p.to_dict() for p in parts]}, indent=2))
    else:
        if mode_line:
            print(mode_line)
        for i, p in enumerate(parts, 1):
            print(f"--- {i}. {p.name}")
            print("\n".join(p.lines))
    return 0


def register(sub: Any) -> None:
    """Add ``context`` under ``levain board`` (called from :func:`levain.desk.board_cli.register`)."""
    p = sub.add_parser(
        "context", description=("What a seat on one item loads: the charter, the item, its derived state, its record and the repo's "
                                "conventions files, bounded to about 8K tokens. A read; a part that cannot be read says why."))
    p.add_argument("item", metavar="NAME#N")
    p.add_argument("--json", action="store_true")
    p.add_argument("--items", type=str, metavar="PATH", help="The store directory or its items.jsonl (default: the pointer, else $LEVAIN_BOARD_ITEMS).")
    p.add_argument("--anneal", metavar="PATH", help=f"The anneal store (episodic db) to read the record from. Default: ${ANNEAL_ENV}.")
    p.add_argument("--flow-episodes", dest="flow_episodes", metavar="PATH",
                   help=f"Flow's episodic.db, opened read-only for its tags (T9-a; optional). Default: ${FLOW_ENV}.")
    p.add_argument("--repo", type=Path, metavar="PATH", help="The repository to read when the registry does not know it.")
    p.add_argument("--no-land-waiter", dest="no_land_waiter", action="store_true",
                   help="This install runs no land waiter (as `levain board state --no-land-waiter`): waiters are then declared absent.")
    p.add_argument("--now", metavar="ISO", help="Judge at this instant (default: the clock).")
    p.set_defaults(func=run_context)
