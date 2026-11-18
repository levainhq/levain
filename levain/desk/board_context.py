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

The bound: :data:`PART_CHARS` caps each part's characters and :func:`_enforce_total` checks the WHOLE printed page (mode line
and headers included) against :data:`TOTAL_CHARS`, re-cutting the largest part until it fits; a cut part ends in a
``[truncated ...]`` line. A line longer than its share is clipped with a ``[clipped N chars]`` marker, never dropped. Inside
``record`` each source has its own sub-budget (:data:`RECORD_CHARS`), so one cannot evict another.

Where the stores are: ``--anneal PATH`` (else ``$LEVAIN_ANNEAL_DB``) and ``--flow-episodes PATH`` (else
``$LEVAIN_FLOW_EPISODES``). No existing producer or install config names a read-only episodic.db path, so these are explicit.
A source with no path named says ``unavailable``; a path that was named and cannot be read says ``unreadable`` with the first error.

Known edges (judged, not bugs): the flow prefilter's 14-hour widening is at least every real offset's distance and flow writes
``T``-separated ISO stamps only, so no boundary row is dropped. ``total_chars`` is the rendered page's size; the ``--json`` form adds
escaping. ``--repo`` reads git state only (it names a path the registry lacks, so there is nothing to check it against; the state
part says so); charter and conventions come from the registry. The record directory is the operator's own, so its filenames are
not sanitised. Dedupe is conservative by design, and the ``-wal``/``-shm`` side files are named above.
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

__all__ = ["ANNEAL_ENV", "FLOW_ENV", "PART_CHARS", "RECORD_CHARS", "TOTAL_CHARS", "register", "run_context"]

ANNEAL_ENV = "LEVAIN_ANNEAL_DB"
FLOW_ENV = "LEVAIN_FLOW_EPISODES"
TOKEN_TARGET = 8_000
CHARS_PER_TOKEN = 3     # conservative: ids, hashes and paths run near this, prose a little above it
TOTAL_CHARS = TOKEN_TARGET * CHARS_PER_TOKEN
PART_CHARS = {"charter": 6_500, "item": 3_000, "state": 3_500, "record": 9_500, "conventions": 800}
RECORD_CHARS = {"anneal": 3_600, "flow": 3_600, "artifacts": 1_800}
WORDS_CHARS = 1_200
MARK_CHARS = 300
WINDOW_DAYS = 90
MAX_SCAN = 50_000
ITEM_EPISODES = 10
DECISION_EPISODES = 5
EPISODE_CHARS = 300
COMMITS_SHOWN = 15
CONVENTIONS = ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING", "CONTRIBUTING.md")
_ID = bs.ID_RE                                       # the store's own id grammar, not a third regex
_TRUNC_TAIL = re.compile(r"\[truncated: (\d+) more lines?\]")
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

    def chars(self) -> int:
        return sum(len(ln) + 1 for ln in self.lines)

    def to_dict(self) -> dict[str, Any]:
        return {"part": self.name, "status": self.status, "why": self.why, "truncated": self.truncated, "chars": self.chars(),
                "lines": self.lines}


def _unreadable(name: str, why: str) -> Part:
    return Part(name, [f"{name}: unreadable ({why})"], "unreadable", why)


def _clip_marked(text: str, n: int) -> str:
    """``text`` cut to ``n`` characters INCLUDING a visible ``… [clipped N chars]`` marker; verbatim when it fits."""
    if len(text) <= n:
        return text
    marker = f"… [clipped {len(text)} chars]"          # the widest the count can be, so the result never exceeds n
    if n < len(marker):                                 # no room for the marker: a bare cut, still within n
        return (text[:max(0, n - 1)] + "…")[:max(0, n)]
    keep = n - len(marker)
    return text[:keep] + f"… [clipped {len(text) - keep} chars]"


def _s(value: Any) -> str:
    """A value for display: ``—`` for None, never the literal ``None``."""
    return "—" if value is None else str(value)


def _cap(lines: list[str], budget: int) -> tuple[list[str], bool]:
    """``lines`` cut at ``budget`` characters. A line longer than a third of the budget is clipped (marker), so one
    over-long line never evicts the lines after it; when later lines do not fit, the cut ends in a ``[truncated ...]`` line
    that is itself inside the budget."""
    carried = 0
    if lines and (m := _TRUNC_TAIL.fullmatch(lines[-1])):      # a re-cap: drop the earlier marker, keep its count
        carried = int(m.group(1))
        lines = lines[:-1]
    out, used, clipped = [], 0, False
    line_max = max(200, budget // 3)
    for i, raw in enumerate(lines):
        ln = _clip_marked(raw, line_max)
        clipped = clipped or ln is not raw
        room = budget - 60 - used - 1
        if len(ln) > room:
            if room >= 100 and not out:
                out.append(_clip_marked(ln, room))      # the very first line is never lost entirely
                used += len(out[-1]) + 1
                i += 1
            rest = len(lines) - i + carried
            return (out + [f"[truncated: {rest} more line{'s' if rest != 1 else ''}]"] if rest > 0 else out), True
        out.append(ln)
        used += len(ln) + 1
    if carried:
        out.append(f"[truncated: {carried} more line{'s' if carried != 1 else ''}]")
        clipped = True
    return out, clipped


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
        lines.append(f"- {pid}  {st.state if st else 'state unreadable'}  {_clip(_s(it.title), 100) if it else '(not in the store)'}")
    out, cut = _cap(lines, PART_CHARS["charter"])
    return Part("charter", out, truncated=cut)


def _item_part(item: Any, derived: Derived | None) -> Part:
    # his words verbatim, on their own sub-budget, so the done line and the dependencies always survive
    lines = [f"item {item.id}: {_clip_marked(_s(item.title), MARK_CHARS)}",
             f"words: {_clip_marked(str(item.words), WORDS_CHARS) if item.words else '(none given)'}",
             f"origin: {_clip_marked(_s(item.origin), MARK_CHARS)}",
             f"done: {_clip_marked(str(item.done), MARK_CHARS) if item.done else '(none)'}  done_kind: {_s(item.done_kind)}"]
    if item.decision is not None:
        lines.append(f"decision: {_s(item.decision.decision)} ({_s(item.decision.date)}): {_clip_marked(item.decision.words or '', MARK_CHARS)}")
    if not item.depends_on:
        lines.append("depends on: nothing")
    for link in item.depends_on:
        if link.kind == "item" and link.ref:
            st = derived.states.get(link.ref) if derived is not None else None
            lines.append(f"depends on: {_s(link.label)}: {st.state if st else 'state unreadable'}")
        else:
            lines.append(f"depends on: {_s(link.label)}")
    out, cut = _cap(lines, PART_CHARS["item"])
    return Part("item", out, truncated=cut)


def _state_part(item_id: str, derived: Derived, repo_note: str = "") -> Part:
    if derived.why:
        return _unreadable("state", derived.why)
    st = derived.states.get(item_id)
    if st is None:
        return _unreadable("state", "the derive did not return the item")
    ev = derived.evidence
    lines = [f"state: {_s(st.state)} ({_s(st.column)}): {_s(st.reason)}"]
    if repo_note:
        lines.append(repo_note)
    if ev is None:
        lines.append("commits: unreadable (no evidence)")
    else:
        for src, why in sorted(ev.unreadable.items()):
            lines.append(f"{src}: unreadable ({why})")
        main = [c for c in ev.main if item_id in c.items or item_id in c.closes]
        lines.append(f"commits on main: {len(main)}")
        lines.extend(f"  {c.sha[:9]} {c.date or '-'}{' closes' if item_id in c.closes else ''}" for c in main[:COMMITS_SHOWN])
        if len(main) > COMMITS_SHOWN:
            lines.append(f"  [{len(main) - COMMITS_SHOWN} more]")
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
    id: str
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


def _keys(e: _Ep) -> set[tuple[str, str]]:
    """What makes two episodes the same one: the same id, or the same body (a capture sync copies an episode across stores)."""
    k = {("body", " ".join(e.content.split()))}
    if e.id:
        k.add(("id", e.id))
    return k


def _distinct(matches: list[_Ep], seen: set[tuple[str, str]]) -> list[_Ep]:
    out, local = [], set()
    for e in matches:
        k = _keys(e)
        if k & seen or k & local:
            continue
        local |= k
        out.append(e)
    return out


def _select(rows: list[_Ep], total: int, item_id: str, repo: str, unreadable: int,
            seen: set[tuple[str, str]] | None = None) -> tuple[list[str], bool]:
    """Filter (rows are newest first), then cap, as :mod:`levain.agents.recall` does; ``truncated`` when the scan bound stopped
    the read and a cap was not met. ``seen`` carries what an earlier source (or the item section) already printed, so one
    episode is never shown twice."""
    seen = set() if seen is None else seen
    item_all = _distinct([e for e in rows if f"item:{item_id}" in e.tags], seen)
    mine = item_all[:ITEM_EPISODES]
    for e in mine:
        seen |= _keys(e)
    dec_all = _distinct([e for e in rows if e.type == "decision" and f"repo:{repo}" in e.tags], seen)
    decisions = dec_all[:DECISION_EPISODES]
    for e in decisions:
        seen |= _keys(e)
    cut = total > len(rows) + unreadable and (len(item_all) < ITEM_EPISODES or len(dec_all) < DECISION_EPISODES)
    lines = [f"scanned {len(rows)} episodes in the last {WINDOW_DAYS} days" + (f"; {unreadable} unreadable" if unreadable else "")]
    lines.append(f"tagged item:{item_id}: {len(mine)}" + (" (newest first)" if mine else ""))
    lines.extend(f"  {e.at} {e.type} {e.source}: {_clip(e.content, EPISODE_CHARS)}" for e in mine)
    if len(item_all) > len(mine):
        lines.append(f"  [{len(item_all) - len(mine)} more]")
    lines.append(f"decisions tagged repo:{repo}: {len(decisions)}" + (" (newest first)" if decisions else ""))
    lines.extend(f"  {e.at} {e.source}: {_clip(e.content, EPISODE_CHARS)}" for e in decisions)
    if len(dec_all) > len(decisions):
        lines.append(f"  [{len(dec_all) - len(decisions)} more]")
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
        rows.append(_Ep(str(getattr(ep, "id", "") or ""), str(ep.timestamp), str(t), str(getattr(ep, "source", "")), str(ep.content), tags))
    return rows, res.total_matching, bad


def _read_flow(path: Path, now: datetime) -> tuple[list[_Ep], int, int]:
    """Flow's episodic.db opened READ-ONLY with ``mode=ro``, ONE attempt: any failure is the source's ``unreadable`` with that
    first error (no second open, no ``immutable`` fallback: that ignores the live WAL and would serve stale rows). The main db
    file is never written; SQLite may create the ``-wal``/``-shm`` side files next to it (they belong to the owning writer,
    which creates them anyway).

    Flow keeps tags in its own table, ``episode_tags(episode_id, tag)`` (``scripts/episodic.py`` writes them there; no flow
    writer puts tags in ``episodes.metadata``, so that path is not read). Flow stamps carry a local offset, so the window is a
    string prefilter widened by the largest UTC offset (14 hours, index-friendly) and then the exact instant by
    ``julianday(timestamp)``, which is also the sort: the window and the order are chronological across offsets. The newest
    MAX_SCAN rows are read, then only their tags, all inside one read transaction."""
    conn = sqlite3.connect("file:" + quote(str(path)) + "?mode=ro", uri=True, timeout=5, isolation_level=None)
    try:
        conn.execute("BEGIN")
        wide = (now - timedelta(days=WINDOW_DAYS, hours=14)).strftime(_STAMP)
        lo, hi = _window(now), now.strftime(_STAMP)
        in_window = "timestamp >= ? AND julianday(timestamp) >= julianday(?) AND julianday(timestamp) <= julianday(?)"
        total = conn.execute(f"SELECT COUNT(*) FROM episodes WHERE {in_window}", (wide, lo, hi)).fetchone()[0]
        # Unparseable stamps cannot be placed by instant, so the same string prefilter bounds them to the window (widened at
        # both ends by the largest UTC offset). ``total`` INCLUDES them, as anneal's does, so ``_select``'s one formula
        # (``total > len(rows) + unreadable``) is right for both sources.
        wide_hi = (now + timedelta(hours=14)).strftime(_STAMP)
        bad = conn.execute("SELECT COUNT(*) FROM episodes WHERE timestamp >= ? AND timestamp <= ? AND julianday(timestamp) IS NULL",
                           (wide, wide_hi)).fetchone()[0]
        total += bad
        got = conn.execute(f"SELECT id, timestamp, type, source, content FROM episodes WHERE {in_window}"
                           " ORDER BY julianday(timestamp) DESC LIMIT ?", (wide, lo, hi, MAX_SCAN)).fetchall()
        tags: dict[str, list[str]] = {}
        ids = [r[0] for r in got]
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for eid, tag in conn.execute("SELECT episode_id, tag FROM episode_tags WHERE episode_id IN (%s)"
                                         % ",".join("?" * len(chunk)), chunk):
                tags.setdefault(eid, []).append(tag)
        conn.execute("COMMIT")
    finally:
        conn.close()
    rows = [_Ep(str(eid), str(ts), str(typ), str(src or ""), str(content), tuple(tags.get(eid, ()))) for eid, ts, typ, src, content in got]
    return rows, total, bad


def _source_lines(label: str, given: str | None, env: str, reader: Any, item_id: str, repo: str, now: datetime,
                  seen: set[tuple[str, str]]) -> tuple[list[str], bool, bool]:
    """``(lines, truncated, read)`` for one episode source. No path named says ``unavailable``; a path that was named (a flag,
    even an empty one, else the environment) and cannot be read says ``unreadable`` with the first error."""
    named = given if given is not None else (os.environ.get(env) or None)
    if named is None:
        return [f"record[{label}]: unavailable (no path named: pass the flag or set ${env})"], False, False
    if not named.strip():
        return [f"record[{label}]: unreadable (an empty path was named)"], False, False
    try:                                         # a user-given path: ~nosuchuser (RuntimeError), a NUL (ValueError) is this source's
        path = Path(named).expanduser()
        is_file = path.is_file()
    except (OSError, ValueError, RuntimeError) as exc:
        return [f"record[{label}]: unreadable ({type(exc).__name__}: {_clip(str(exc), 120)})"], False, False
    if not is_file:
        return [f"record[{label}]: unreadable ({path} is not a file)"], False, False
    try:
        rows, total, bad = reader(path, now)
    except Exception as exc:  # noqa: BLE001 - an unreadable store is said, never shown as an empty record
        return [f"record[{label}]: unreadable ({type(exc).__name__}: {_clip(str(exc), 120)})"], False, False
    lines, cut = _select(rows, total, item_id, repo, bad, seen)
    return [f"record[{label}]:"] + ["  " + ln for ln in lines], cut, True


def _artifact_lines(repo: Any, item_id: str) -> list[str]:
    """Names only, from the record directory: the ``pre-track-*`` directories (the repo's archive of what tracking replaced) and
    the dated entries whose name carries the item (``NAME#N``, ``NAME-N`` or ``NAME_N``)."""
    if repo is None or not repo.record_dir:
        return ["record[artifacts]: unavailable (no record directory)"]
    try:
        base = Path(repo.record_dir).expanduser()
        names = sorted(os.listdir(base))
    except (OSError, ValueError, RuntimeError) as exc:
        return [f"record[artifacts]: unreadable ({_clip(str(repo.record_dir), 120)} cannot be listed: {type(exc).__name__})"]
    name, _, n = item_id.partition("#")
    forms = [re.compile(r"(?<![A-Za-z0-9])" + re.escape(f) + r"(?!\d)") for f in (item_id, f"{name}-{n}", f"{name}_{n}")]   # not a prefix of a longer number
    hits: list[str] = []
    for e in names:
        if e.startswith("pre-track-"):
            hits.append(e)
        elif _DATED.match(e) and any(f.search(e) for f in forms):
            hits.append(e)
    return [f"record[artifacts]: {base}"] + [f"  {h}" for h in hits] if hits else \
        [f"record[artifacts]: nothing in {base} names {item_id} (pre-track-* and dated artifacts)"]


def _record_part(repo: Any, name: str, item_id: str, args: argparse.Namespace, now: datetime) -> Part:
    seen: set[tuple[str, str]] = set()          # one episode prints once, across both sources and both sections
    a, c1, ok_a = _source_lines("anneal", args.anneal, ANNEAL_ENV, _read_anneal, item_id, name, now, seen)
    f, c2, ok_f = _source_lines("flow", args.flow_episodes, FLOW_ENV, _read_flow, item_id, name, now, seen)
    lines: list[str] = ["record:"]
    cut = c1 or c2
    for key, src in (("anneal", a), ("flow", f), ("artifacts", _artifact_lines(repo, item_id))):
        capped, c = _cap(src, RECORD_CHARS[key])   # each source on its own sub-budget: one cannot evict another
        lines += capped
        cut = cut or c
    out, c3 = _cap(lines, PART_CHARS["record"])
    part = Part("record", out, truncated=cut or c3)
    if not (ok_a or ok_f):
        named = any(g is not None or os.environ.get(env) for g, env in ((args.anneal, ANNEAL_ENV), (args.flow_episodes, FLOW_ENV)))
        part.status, part.why = ("unreadable", "no episode source could be read") if named else ("unavailable", "no episode source was named")
    else:
        failed = [src[0].partition(": ")[0] + " " + src[0].partition(": ")[2] for src, ok, g, env in
                  ((a, ok_a, args.anneal, ANNEAL_ENV), (f, ok_f, args.flow_episodes, FLOW_ENV))
                  if not ok and (g is not None or os.environ.get(env))]
        if failed:                                # status stays readable; the failed named source is said
            part.why = "; ".join(failed)
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


def _render(mode_line: str, parts: list[Part]) -> str:
    pieces = [mode_line] if mode_line else []
    for i, p in enumerate(parts, 1):
        pieces.append(f"--- {i}. {p.name}")
        pieces.append("\n".join(p.lines))
    return "\n".join(pieces)


def _enforce_total(mode_line: str, parts: list[Part]) -> str:
    """The rendered page, checked against :data:`TOTAL_CHARS` as a whole: while over, the largest part is re-cut by the excess."""
    text = _render(mode_line, parts)
    for _ in range(20):
        if len(text) <= TOTAL_CHARS:
            return text
        big = max(parts, key=Part.chars)
        big.lines, _cut = _cap(big.lines, max(200, big.chars() - (len(text) - TOTAL_CHARS)))
        big.truncated = True
        text = _render(mode_line, parts)
    tail = "\n[truncated: page bound]"            # unreachable with sane caps; the bound holds regardless, visibly
    return (text[:max(0, TOTAL_CHARS - len(tail))] + tail)[:TOTAL_CHARS]


def _refuse(msg: str) -> int:
    print(f"levain board context: refused: {msg}", file=sys.stderr)
    return EXIT_REFUSED


def _guarded(name: str, build: Any) -> Part:
    """One part's builder under its own guard: an exception is that part's ``unreadable``; the other parts still print."""
    try:
        return build()
    except Exception as exc:  # noqa: BLE001
        return _unreadable(name, f"{type(exc).__name__}: {_clip(str(exc), 120)}")


def run_context(args: argparse.Namespace) -> int:
    if not isinstance(args.item, str) or _ID.fullmatch(args.item) is None:
        return _refuse(f"{args.item!r} is not an item id (NAME#N)")
    name, item_id = args.item.partition("#")[0], args.item
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
    now = now.astimezone(timezone.utc)          # the stamps below end in a literal Z, so the instant must be UTC
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

    repo_path, repo_err = None, ""
    try:
        repo_path = Path(repo.path) if repo is not None else (Path(args.repo).expanduser() if args.repo else None)
    except (OSError, ValueError, RuntimeError) as exc:
        repo_err = f"--repo cannot be used ({type(exc).__name__}: {_clip(str(exc), 120)})"
    derived: Derived | None
    if repo_err:
        derived = Derived({}, None, repo_err)
    elif repo_path is None:
        derived = Derived({}, None, f"{repo_why or 'no repository path'}; pass --repo PATH to read git")
    else:
        try:
            derived = _derive_one_repo(folded, repo_path, name, target, now, args.no_land_waiter)
        except Exception as exc:  # noqa: BLE001 - a derive that cannot be made is said as unreadable, never as an empty state
            derived = Derived({}, None, f"git evidence cannot be read ({type(exc).__name__}: {_clip(str(exc), 120)})")
    dv = derived if not derived.why else None

    repo_note = ""
    if repo is None and repo_path is not None:
        repo_note = f"git evidence was read from {repo_path}, given by --repo; the path's identity is not checked"
    parts = [_guarded("charter", lambda: _charter_part(repo, name, item_id, folded, dv, repo_why)),
             _guarded("item", lambda: _item_part(item, dv)),
             _guarded("state", lambda: _state_part(item_id, derived, repo_note)),
             _guarded("record", lambda: _record_part(repo, name, item_id, args, now)),
             _guarded("conventions", lambda: _conventions_part(repo))]
    mode_line = _clip(mode_line, MARK_CHARS) if mode_line else ""
    text = _enforce_total(mode_line, parts)
    if args.json:
        print(json.dumps({"item": item_id, "repo": name, "untracked": bool(mode_line), "note": mode_line or None,
                          "total_chars": len(text), "parts": [p.to_dict() for p in parts]}, indent=2))
    else:
        print(text)
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
    from levain.desk.door import door_ok_author

    door_ok_author(p)     # the desk brief's read; opened where it is defined
