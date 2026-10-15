"""levain.cockpit.team_panels: the cockpit's Team zone (TEAM_VIEW_PLAN T2; the T3 verbs: levain.team.cockpit_verbs).

ONE ``TeamSource`` reads the install's team ledger (``levain.team.reader.read_team``) and caches the answer for
``ttl_s``; every Team panel is a view over that one answer, so a manifest build asks the ledger once however many
panels draw it, and no panel reads the ledger itself. A cold history read can be slow: the source runs it on a
worker thread, a caller waits ``wait_s`` for the FIRST answer only, and a later refresh serves the last answer
while the worker renews it. Nothing here fetches: Sync shows what the transport last recorded. A team verb
(``levain.team.cockpit_verbs``) that changed the ledger calls ``invalidate``, and the next read waits for a fresh
answer instead of serving the copy from before the verb.

The panel SET follows the state (a discoverer registered with ``retire``): ``ok`` and ``judged_partial`` draw the
panes; ``no_team`` and ``unreadable`` draw ONE card, the reader's own ``why`` (its ``detail`` is for a log and is
never drawn). ``judged_partial`` also carries the ``why`` as a note on every pane, beside the rows.
"""
from __future__ import annotations

import dataclasses
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
from levain.team.reader import _NO_TEAM, TeamRead, person_text, read_team
from levain.team.transport import Repo

__all__ = ["TeamSource", "TeamSnapshot", "team_discoverer", "invalidate", "FETCH_VERB", "ACCEPT_VERB"]

_PENDING = "The team ledger is still being read on this machine; this page will fill in."
REGION = "team"
STATUS_ID = "team-status"
FACETS = frozenset({"entry_type", "entry_owner", "agent", "age_days", "exposure_count"})
_ROW_MAX = 400
# the Team zone's verbs (levain.team.cockpit_verbs registers them): Sync offers the fetch while the ledger has a remote,
# Joins pending offers the accept while a join waits on THIS machine
FETCH_VERB = "team_fetch"
ACCEPT_VERB = "team_join_accept"

# Bumped by a team verb after it changed (or tried to change) the ledger: every source's cached answer read before the
# bump is old, and the next read waits for a fresh one, so the page shows what the verb did, not the copy before it.
_EPOCH = 0
_EPOCH_LOCK = threading.Lock()


def invalidate() -> None:
    """Every TeamSource's cached read is old from now: the next ``get`` reads the ledger again."""
    global _EPOCH
    with _EPOCH_LOCK:
        _EPOCH += 1


def _epoch() -> int:
    with _EPOCH_LOCK:
        return _EPOCH


@dataclass(frozen=True)
class TeamSnapshot:
    read: TeamRead
    sync: dict[str, Any]      # the transport's recorded state: remote, last_fetch_ok, last_fetch_error
    age_s: float = 0.0        # > 0 only when served past its ttl because the renewal is stuck: it reads as old


def _unreadable(why: str) -> TeamSnapshot:
    return TeamSnapshot(TeamRead("unreadable", None, why, ""), {})


class TeamSource:
    """The cached, single-flight read of one install's team ledger. ``start`` is where the ledger is looked for
    (never the process's working directory: a caller names it); ``load`` is for tests. A renewal still running
    after ``stuck_s`` is stuck: the last answer is then served marked old, and a new worker may take over."""

    def __init__(self, start: Path | str | None = None, *, ttl_s: float = 15.0, wait_s: float = 8.0,
                 stuck_s: float = 60.0, load: Callable[[], TeamSnapshot] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._start = Path(start) if start is not None else None
        self._ttl, self._wait, self._stuck, self._clock = ttl_s, wait_s, stuck_s, clock
        self._load = load or self._load_ledger
        self._lock = threading.Lock()
        self._snap: TeamSnapshot | None = None
        self._at = 0.0
        self._snap_epoch = -1                         # the epoch the answer was read in
        self._work_epoch = -1                         # the epoch the running worker started in
        self._done: threading.Event | None = None     # set while a worker is reading
        self._began = 0.0                             # when that worker started
        self._cold_late = False                       # a first read outran its wait: later callers do not wait again

    def _load_ledger(self) -> TeamSnapshot:
        repo = Repo.discover(self._start) if self._start is not None else None
        if repo is None:
            return TeamSnapshot(TeamRead("no_team", None, _NO_TEAM), {})
        gl = open_ledger(repo)
        sync = dict(gl.state()) if gl.remote else {}
        if gl.remote:
            sync["remote"] = gl.remote
        return TeamSnapshot(read_team(gl), sync)

    def _work(self, done: threading.Event, epoch: int) -> None:
        snap = None
        try:
            snap = self._load()
        except Exception as exc:  # noqa: BLE001 - the source never raises into a manifest build
            snap = _unreadable(f"The team ledger could not be read on this machine ({type(exc).__name__}).")
        finally:
            with self._lock:
                if self._done is done:       # a worker a newer one took over from commits nothing
                    if snap is not None:
                        self._snap, self._at, self._snap_epoch = snap, self._clock(), epoch
                    self._done, self._cold_late = None, False
            done.set()

    def get(self) -> TeamSnapshot:
        epoch = _epoch()
        with self._lock:
            now = self._clock()
            age = now - self._at
            current = self._snap_epoch == epoch
            if self._snap is not None and age < self._ttl and current:
                return self._snap
            # a worker started before an invalidation reads the ledger as it was: a new one takes over
            if self._done is None or now - self._began > self._stuck or self._work_epoch != epoch:
                self._done, self._began, self._work_epoch = threading.Event(), now, epoch
                threading.Thread(target=self._work, args=(self._done, epoch), daemon=True, name="team-read").start()
            have, done = self._snap, self._done
            if have is not None and current:
                # a renewal that has outrun `stuck_s` is not "being renewed": the answer reads as old
                return dataclasses.replace(have, age_s=age) if age > self._stuck else have
            if have is None and self._cold_late:
                return _unreadable(_PENDING)
        # a first read, or an answer a team verb made old: wait for the new one; past the wait the old one is served
        done.wait(self._wait)
        with self._lock:
            if self._snap is not None:
                return self._snap
            self._cold_late = True
        return _unreadable(_PENDING)


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
            lines.append(("fetch-error", f"last fetch error: {_one_line(person_text(err, said='git could not fetch it.', fallback='the remote could not be fetched.'))}"))
    else:
        lines.append(("remote", "no remote: the ledger is local only"))
    if m.get("problems"):
        lines.append(("problems", f"{m['problems']} integrity problem(s): run `levain team verify`"))
    if m.get("canon_status"):
        lines.append(("canon", _one_line(m["canon_status"])))
    return tuple(RowIn(id=i, title=t, stored={"title": t}) for i, t in lines)


def _member_rows(members: list[dict]) -> tuple[RowIn, ...]:
    out = []
    for m in members:
        who = _one_line(m["handle"]) + (" (you)" if m.get("you") else "")
        keys = f"{m['keys']} key{'s' if m['keys'] != 1 else ''}" if m["keys"] else ""
        # a list row draws its BODY as the line (seen in WebKit: a title-only handle was never drawn), so it leads
        body = _one_line(" · ".join(p for p in (who, m["role"], m["key_state"] + (f" ({keys})" if keys else ""),
                                                f"since {m['since']}" if m.get("since") else "") if p))
        if m["key_state"] == "no key":
            body = _one_line(body + " · cannot sign")
        out.append(RowIn(id=f"member:{m['handle']}", title=who, body=body, stored={"title": who, "body": body}))
    return tuple(out)


# a join that waits on THIS machine names the laptop command that does the act today: the member's own signed act
_MINE_LINE = {"key": "this machine's key is proposed for {h}; run `levain team key confirm` on this machine to bring "
                     "it into force",
              "owner-offer": "ownership is offered to you; run `levain team accept` on this machine to take it"}


def _join_rows(joins: list[dict]) -> tuple[RowIn, ...]:
    out = []
    for j in joins:
        title = _one_line(f"{j['kind']} · {j['handle']}")
        mine = _MINE_LINE.get(j["kind"]) if j.get("mine") else None
        line = mine.format(h=j["handle"]) if mine else j.get("line")
        body = _one_line(" · ".join(p for p in (line, j.get("fingerprint")) if p))
        out.append(RowIn(id=f"join:{j['kind']}:{j['handle']}:{j.get('n', 0)}", title=title, body=body,
                         stored={"title": title, "body": body}))
    return tuple(out)


_PANES: tuple[tuple[str, str, str, str, int, int, Callable[[TeamSnapshot], tuple[RowIn, ...]]], ...] = (
    # id, title, empty line, size, span, rank, rows from the snapshot
    ("team-members", "Members", "No members on team.toml.", "medium", 1, 5,
     lambda sn: _member_rows(sn.read.model["members"])),
    ("team-waiting", "Waiting on you", "Nothing is waiting on you.", "medium", 1, 10,
     lambda sn: _entry_rows(sn.read.model["waiting"])),
    ("team-stopped", "Where agents were stopped", "No acknowledged rulings yet.", "medium", 1, 20,
     lambda sn: _stopped_rows(sn.read.model["stopped"])),
    ("team-held", "Held by nobody", "Nothing is held by nobody.", "medium", 1, 30,
     lambda sn: _entry_rows(sn.read.model["held"])),
    ("team-canon", "In force", "Nothing is in force yet.", "tall", 2, 40,
     lambda sn: _canon_rows(sn.read.model["in_force"])),
    ("team-joins", "Joins pending", "No joins are waiting.", "short", 1, 45,
     lambda sn: _join_rows(sn.read.model["joins"])),
    ("team-sync", "Sync", "", "short", 1, 50, _sync_rows),
)


def _snapshot(src: TeamSource, ctx: ReadContext) -> TeamSnapshot:
    """ONE snapshot per request: the discoverer and every panel of a manifest build or panel read draw the same answer."""
    return ctx.memo(f"team-snapshot:{id(src)}", src.get)


def _age_note(snap: TeamSnapshot) -> str | None:
    return (f"Showing the ledger as read {int(snap.age_s)}s ago: the newer read has not finished."
            if snap.age_s > 0 else None)


def _notes(*parts: str | None) -> str | None:
    return " ".join(p for p in parts if p) or None


def _pane_read(src: TeamSource, build: Callable[[TeamSnapshot], tuple[RowIn, ...]]) -> Callable[[ReadContext], Result]:
    def read(ctx: ReadContext) -> Result:
        snap = _snapshot(src, ctx)
        if snap.read.model is None:       # the zone's shape changed under this read: the page re-reads its manifest
            return Read(rows=(), note=snap.read.why or None)
        return Read(rows=build(snap), note=_notes(snap.read.why if snap.read.state == "judged_partial" else None,
                                                  _age_note(snap)))
    return read


def _status_read(src: TeamSource) -> Callable[[ReadContext], Result]:
    def read(ctx: ReadContext) -> Result:
        snap = _snapshot(src, ctx)
        if snap.read.model is not None:   # the zone's shape changed under this read: the page re-reads its manifest
            return Read(rows=(), note=snap.read.why or None)
        why = snap.read.why or "The team ledger could not be read on this machine."
        return Read(rows=(RowIn(id=snap.read.state, title=_one_line(why), stored={"title": why}),), note=_age_note(snap))
    return read


def team_discoverer(src: TeamSource) -> Callable[[ReadContext], list[ProviderSpec]]:
    """The Team zone's panels for the state the source reads NOW (register with ``Cockpit.discover(..., retire=True)``)."""
    def discover(ctx: ReadContext) -> list[ProviderSpec]:
        snap = _snapshot(src, ctx)
        if snap.read.model is None:
            # one title for both states: a discovered panel's title is its identity, and the row says which state
            return [ProviderSpec(STATUS_ID, "list", "Team", "feed", _status_read(src), region=REGION, rank=0,
                                 order="legacy.source", version_fields=("*",), size="short", span=3)]
        return [ProviderSpec(pid, "list", title, "feed", _pane_read(src, build), region=REGION, rank=rank,
                             order="legacy.source", facets=FACETS, version_fields=("*",), empty=empty, size=size,
                             span=span, verbs=_offers(pid, snap))
                for pid, title, empty, size, span, rank, build in _PANES]
    return discover


def _offers(pid: str, snap: TeamSnapshot) -> tuple[str, ...]:
    """What a pane offers, from the same snapshot it draws: a write re-asks the discoverer (``offer_spec``), so an
    offer that no longer holds is refused there, and the store decides again under the team op lock at apply."""
    if pid == "team-sync" and snap.sync.get("remote"):
        return (FETCH_VERB,)
    if pid == "team-joins" and any(j.get("mine") for j in (snap.read.model or {}).get("joins") or ()):
        return (ACCEPT_VERB,)
    return ()
