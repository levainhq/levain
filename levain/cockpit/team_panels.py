"""levain.cockpit.team_panels: the cockpit's Team zone, read-only (TEAM_VIEW_PLAN T2).

ONE ``TeamSource`` reads the install's team ledger (``levain.team.reader.read_team``) and caches the answer for
``ttl_s``; every Team panel is a view over that one answer, so a manifest build asks the ledger once however many
panels draw it, and no panel reads the ledger itself. A cold history read can be slow: the source runs it on a
worker thread, a caller waits ``wait_s`` for the FIRST answer only, and a later refresh serves the last answer
while the worker renews it. Nothing here fetches: Sync shows what the transport last recorded.

The panel SET follows the state (a discoverer registered with ``retire``): ``ok`` and ``judged_partial`` draw the
panes; ``no_team`` and ``unreadable`` draw ONE card, the reader's own ``why`` (its ``detail`` is for a log and is
never drawn). ``judged_partial`` also carries the ``why`` as a note on every pane, beside the rows.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from levain.cockpit.engine import ProviderSpec, ReadContext
from levain.cockpit.results import Read, Result, RowIn
from levain.team import index as _index
from levain.team.ledger_api import open_ledger
from levain.team.reader import _NO_TEAM, TeamRead, read_team
from levain.team.transport import Repo

__all__ = ["TeamSource", "TeamSnapshot", "team_discoverer"]

REGION = "team"
STATUS_ID = "team-status"
FACETS = frozenset({"entry_type", "entry_owner", "agent", "age_days", "exposure_count"})
_ROW_MAX = 400


@dataclass(frozen=True)
class TeamSnapshot:
    read: TeamRead
    sync: dict[str, Any]      # the transport's recorded state: remote, last_fetch_ok, last_fetch_error


def _unreadable(why: str) -> TeamSnapshot:
    return TeamSnapshot(TeamRead("unreadable", None, why, ""), {})


class TeamSource:
    """The cached, single-flight read of one install's team ledger. ``start`` is where ``levain team status``
    would start looking (its working directory, by default); ``load`` is for tests."""

    def __init__(self, start: Path | str | None = None, *, ttl_s: float = 15.0, wait_s: float = 8.0,
                 load: Callable[[], TeamSnapshot] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._start = Path(start) if start is not None else Path(os.getcwd())
        self._ttl, self._wait, self._clock = ttl_s, wait_s, clock
        self._load = load or self._load_ledger
        self._lock = threading.Lock()
        self._snap: TeamSnapshot | None = None
        self._at = 0.0
        self._done: threading.Event | None = None     # set while a worker is reading

    def _load_ledger(self) -> TeamSnapshot:
        repo = Repo.discover(self._start)
        if repo is None:
            return TeamSnapshot(TeamRead("no_team", None, _NO_TEAM), {})
        gl = open_ledger(repo)
        sync = dict(gl.state()) if gl.remote else {}
        if gl.remote:
            sync["remote"] = gl.remote
        return TeamSnapshot(read_team(gl), sync)

    def _work(self, done: threading.Event) -> None:
        try:
            snap = self._load()
        except Exception as exc:  # noqa: BLE001 - the source never raises into a manifest build
            snap = _unreadable(f"The team ledger could not be read on this machine ({type(exc).__name__}).")
        with self._lock:
            self._snap, self._at, self._done = snap, self._clock(), None
        done.set()

    def get(self) -> TeamSnapshot:
        with self._lock:
            fresh = self._snap is not None and self._clock() - self._at < self._ttl
            if fresh:
                return self._snap      # type: ignore[return-value]
            if self._done is None:
                self._done = threading.Event()
                threading.Thread(target=self._work, args=(self._done,), daemon=True, name="team-read").start()
            have, done = self._snap, self._done
        if have is not None:
            return have
        done.wait(self._wait)
        with self._lock:
            if self._snap is not None:
                return self._snap
        return _unreadable("The team ledger is still being read on this machine; this page will fill in.")


def _one_line(text: object, limit: int = _ROW_MAX) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[:limit - 1].rstrip() + "…"


def _entry_rows(cards: list[dict]) -> tuple[RowIn, ...]:
    out: list[RowIn] = []
    seen: set[str] = set()
    for c in cards:
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        label = f"{c['type']} · {c['kind']}" if c.get("kind") else str(c.get("type") or "entry")
        notes = []
        if c.get("why"):
            notes.append(str(c["why"]))
        if c.get("reason"):
            notes.append(f"why: {c['reason']}")
        if c.get("paths"):
            notes.append("governs " + ", ".join(c["paths"]))
        notes.append(f"recorded by {c.get('pack') and 'pack ' + c['pack'] or c.get('recorded_by') or '?'} · {c.get('age', '')}")
        facets: dict[str, Any] = {"entry_type": label, "agent": str(c.get("recorded_by") or "?")}
        if c.get("owner"):
            facets["entry_owner"] = str(c["owner"])
        if isinstance(c.get("age_days"), int):
            facets["age_days"] = c["age_days"]
        # a list row draws its BODY as the line and its title as the label (an episode's source), so the words ride in body
        title = _one_line(f"{label} {c['id']}")
        words = _one_line(c.get("words") or c.get("summary") or "")
        body = _one_line(" · ".join(([words] if words else []) + notes))
        out.append(RowIn(id=c["id"], title=title, body=body, facets=facets,
                         stored={"title": title, "body": body}))
    return tuple(out)


def _stopped_rows(items: list[dict]) -> tuple[RowIn, ...]:
    out = []
    for r in items:
        words = " ".join(w for w in r["ruling_words"] if w)
        notes = ([f"review: {r['acks']} acks, this ruling may be stale or too broad"] if r["review"] else [f"{r['acks']} acks"])
        if not r["in_force"]:
            notes.append("ruling no longer in force")
        body = _one_line(" · ".join(notes + ([words] if words else [])))
        out.append(RowIn(id=f"stopped:{r['path']}", title=_one_line(r["path"]), body=body,
                         facets={"exposure_count": int(r["acks"])},
                         stored={"title": r["path"], "body": body}))
    return tuple(out)


def _canon_rows(canon: list[dict]) -> tuple[RowIn, ...]:
    return _entry_rows([c for group in canon for c in group["entries"]])


def _fmt_ts(ts: Any) -> str:
    try:
        iso = datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
    return _index.age(iso)


def _sync_rows(snap: TeamSnapshot) -> tuple[RowIn, ...]:
    m = snap.read.model or {}
    s = snap.sync
    lines: list[tuple[str, str]] = []
    if s.get("remote"):
        ok, err = s.get("last_fetch_ok"), s.get("last_fetch_error")
        lines.append(("remote", f"remote {s['remote']}: last fetch " + (_fmt_ts(ok) if ok else "never")))
        if err:
            lines.append(("fetch-error", f"last fetch error: {_one_line(err)}"))
    else:
        lines.append(("remote", "no remote: the ledger is local only"))
    if m.get("problems"):
        lines.append(("problems", f"{m['problems']} integrity problem(s): run `levain team verify`"))
    if m.get("canon_status"):
        lines.append(("canon", _one_line(m["canon_status"])))
    return tuple(RowIn(id=i, title=t, stored={"title": t}) for i, t in lines)


_PANES: tuple[tuple[str, str, str, str, int, int, Callable[[TeamSnapshot], tuple[RowIn, ...]]], ...] = (
    # id, title, empty line, size, span, rank, rows from the snapshot
    ("team-waiting", "Waiting on you", "Nothing is waiting on you.", "medium", 1, 10,
     lambda sn: _entry_rows(sn.read.model["waiting"])),
    ("team-stopped", "Where agents were stopped", "No acknowledged rulings yet.", "medium", 1, 20,
     lambda sn: _stopped_rows(sn.read.model["stopped"])),
    ("team-held", "Held by nobody", "Nothing is held by nobody.", "medium", 1, 30,
     lambda sn: _entry_rows(sn.read.model["held"])),
    ("team-canon", "In force", "Nothing is in force yet.", "tall", 2, 40,
     lambda sn: _canon_rows(sn.read.model["in_force"])),
    ("team-sync", "Sync", "", "short", 1, 50, _sync_rows),
)


def _pane_read(src: TeamSource, build: Callable[[TeamSnapshot], tuple[RowIn, ...]]) -> Callable[[ReadContext], Result]:
    def read(ctx: ReadContext) -> Result:
        snap = src.get()
        if snap.read.model is None:       # the zone's shape changed under this read: the page re-reads its manifest
            return Read(rows=(), note=snap.read.why or None)
        return Read(rows=build(snap), note=snap.read.why if snap.read.state == "judged_partial" else None)
    return read


def _status_read(src: TeamSource) -> Callable[[ReadContext], Result]:
    def read(ctx: ReadContext) -> Result:
        snap = src.get()
        why = snap.read.why or "The team ledger could not be read on this machine."
        return Read(rows=(RowIn(id=snap.read.state, title=_one_line(why), stored={"title": why}),))
    return read


def team_discoverer(src: TeamSource) -> Callable[[ReadContext], list[ProviderSpec]]:
    """The Team zone's panels for the state the source reads NOW (register with ``Cockpit.discover(..., retire=True)``)."""
    def discover(ctx: ReadContext) -> list[ProviderSpec]:
        snap = src.get()
        if snap.read.model is None:
            # one title for both states: a discovered panel's title is its identity, and the row says which state
            return [ProviderSpec(STATUS_ID, "list", "Team", "feed", _status_read(src), region=REGION, rank=0,
                                 order="legacy.source", version_fields=("*",), size="short", span=3)]
        return [ProviderSpec(pid, "list", title, "feed", _pane_read(src, build), region=REGION, rank=rank,
                             order="legacy.source", facets=FACETS, version_fields=("*",), empty=empty, size=size,
                             span=span)
                for pid, title, empty, size, span, rank, build in _PANES]
    return discover
