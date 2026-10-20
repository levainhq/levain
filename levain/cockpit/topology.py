"""levain.cockpit.topology: the Levain feed behind the ``topology`` panel (F12_SPEC, with the 14a amendment).

One value over what the install already holds: the session records its hooks write (the NODES: one per session,
grouped by the repository its working directory is in, a linked worktree folded into its main repository), the
consent queue and the question store (a session waiting on a pending request or an open question is WAITING ON YOU,
and the record is the task it waits on), the dispatch job store and the F7 outcome ledger (a contract is a task, its
ledger labels its outcomes), and the entity's open loops (a spore is a task). A session that no task names is a
worker of the one ``unattached`` task, never of a guessed one.

The two quiet states never merge. WAITING ON YOU is a record: a pending consent request, an open question, a
dispatch contract waiting on a signature or an authorisation. STALLED (a node) and STUCK (a task) are silence with
no record behind it: a session whose last logged event is a prompt or a tool call and that has said nothing for
``STALL_S``, an open loop, which no session is linked to.

"Lost if closed" is per live session, read by ``activity.git_worktree_state`` (its status read runs on a private git
dir the cockpit owns, so no repository config reaches it; see that function), bounded by the same whole-scan deadline,
each working tree past it named, and cached for ``LOST_CACHE_S``.

Not tracked on this branch, sent as null (never a zero, never an estimate): a session's current tool (the session
log holds it only inside text this token-free panel must not carry), file conflicts (no hook records an Edit or
Write path), tokens today, every task's cost (no dispatch spend key and no ledger cost column exist yet), uncaptured
episodes (episodes carry no session) and running children (no hook records a subagent). What does reach the wire is
display-safe: a consent task is its agent and tool, a question its number, a dispatch contract its template, never
its goal or a request's body; but a file name, a branch name, a repository or directory basename and an open loop's
text (all agent- or repository-controlled) are carried, each through ``_wire`` (display-safe, a basename without a
path, at most ``LABEL_MAX`` characters).

A source that cannot be read is named in ``unreadable`` with its reason in ``errors``; unreadable session records
are a ``Fault``, never an empty graph.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from levain.cockpit import activity as act
from levain.cockpit.display_safe import visible
from levain.cockpit.registry import CONSENT_ROW_PREFIX, QUESTION_ROW_PREFIX
from levain.cockpit.results import Fault, Read, Result

__all__ = ["LOST_CACHE_S", "LOST_WORKTREES_MAX", "SPORES_MAX", "STALL_S", "topology_reader"]

STALL_S = 5 * 60                   # a session mid-turn (last event a prompt or a tool) silent this long is STALLED
RATE_WINDOW_S = 5 * 60             # a node's rate: logged events per minute over this window
SPORES_MAX = 20                    # open loops shown as tasks, in the store's order
DISPATCH_DONE_H = 14               # a finished contract stays on the board this long (the sky's window)
LOST_WORKTREES_MAX = act.ACTIVITY_REPOS_MAX
LOST_CACHE_S = 30.0
LABEL_MAX = 48
UNNAMED = "(unnamed)"
_MARKER = re.compile(r"⟨U\+[0-9A-F]{4,6}⟩")
HUB_ID = "levain"
UNATTACHED_ID = "unattached"
NO_COST = {"usd": None, "tokens": None, "source": None}

# the dispatch job's state as a task's (jobs.STATES); filed waits on the operator's signature, verified and
# recommended on his authorisation: a record, so WAITING. signed, queued and authorised are RUNNING because the job
# runtime holds them (a record exists, it is not silence), with no worker named: the job record names no session.
_JOB_STATE = {"filed": "waiting", "signed": "running", "queued": "running", "running": "running",
              "verifying": "running", "verified": "waiting", "recommended": "waiting", "authorised": "running",
              "adopted": "done", "cancelled": "done", "failed": "failed"}
# an F7 ledger label as an outcome's ok; a label not here (filed, left undecided) is a clock, not an outcome
_LEDGER_OK = {"verifier_pass": True, "verifier_fail": False, "contrast_clean": True, "contrast_divergent": False,
              "planner_recommends": True, "planner_refiles": False, "authorised": True, "adopted": True,
              "reverted_14d": False, "gated_allowed": True, "gated_denied": False}


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat()


def _wire(text: Any, *, base: bool = False) -> str:
    """The ONE normaliser every string this provider puts on the wire goes through: ``visible`` (no invisible or
    format character); for a ``base`` name, ``/`` and ``\\`` shown as visible markers (a name is a basename, never a
    path, and is never refused); whitespace collapsed to single spaces; an empty result the fixed ``UNNAMED``; then
    the ``LABEL_MAX`` cut, which never splits a ``⟨U+XXXX⟩`` marker. The engine's validator refuses what this never
    emits."""
    s = visible(str(text))
    if base:
        s = s.replace("/", "⟨U+002F⟩").replace("\\", "⟨U+005C⟩")
    s = " ".join(s.split())
    if not s:
        return UNNAMED
    if len(s) <= LABEL_MAX:
        return s
    cut = LABEL_MAX - 1
    for m in _MARKER.finditer(s):
        if m.start() < cut < m.end():
            cut = m.start()
            break
    return s[:cut] + "…"


# --- the sessions ----------------------------------------------------------------------------------------------

def _records(install_root: Path, now: datetime) -> list[dict[str, Any]]:
    """The session records the Sessions panel lists (its reader, caps and 24-hour horizon), newest first. Raises
    ``OSError`` for a sessions directory that cannot be read; no directory is no sessions."""
    from levain.cockpit.providers import (SESSION_LISTED_S, SESSION_SKEW_S, SESSIONS_SCAN, SESSIONS_SHOWN,
                                          _session_record)

    sdir = install_root / ".levain" / "sessions"
    found: list[tuple[float, Path]] = []
    try:
        with os.scandir(sdir) as it:
            for n, e in enumerate(it):
                if n >= SESSIONS_SCAN:
                    break
                try:
                    if e.name.endswith(".json") and e.is_file(follow_symlinks=False):
                        found.append((e.stat(follow_symlinks=False).st_mtime, Path(e.path)))
                except OSError:
                    continue
    except FileNotFoundError:
        return []
    found.sort(key=lambda p: p[0], reverse=True)
    out = []
    for _mt, path in found[:SESSIONS_SHOWN]:
        rec = _session_record(path)
        if rec is None:
            continue
        age = (now - rec["_seen"]).total_seconds()
        if age < -SESSION_SKEW_S or age > SESSION_LISTED_S:
            continue
        out.append(rec)
    return out


def _log_signal(install_root: Path, stem: str, now: datetime) -> tuple[datetime | None, str | None, float | None]:
    """From the session's event log (Q8), counts and stamps only, never a word of it: the newest event's time and
    kind (prompt, reply, tool), and events per minute over the last ``RATE_WINDOW_S``. No log is (None, None, None):
    the rate is unknown, never a zero."""
    from levain.cockpit.session_log import _event, _tail

    raw = _tail(install_root / ".levain" / "sessions" / f"{stem}.log.jsonl")
    if not raw:
        return None, None, None
    last_at: datetime | None = None
    last_kind: str | None = None
    recent = 0
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        ev = _event(line)
        if ev is None:
            continue
        at = act._parse(ev["at"])
        if at is None or (at - now).total_seconds() > 300:
            continue
        if last_at is None or at >= last_at:
            last_at, last_kind = at, ev["kind"]
        if (now - at).total_seconds() <= RATE_WINDOW_S:
            recent += 1
    return last_at, last_kind, round(recent / (RATE_WINDOW_S / 60), 2)


def _node_state(waiting: bool, age_s: float, last_kind: str | None) -> str:
    """WAITING (a record) > STALLED (mid-turn and silent) > RUNNING > IDLE (quiet past the Sessions panel's Active
    line, or its turn ended in a reply). Silence with no log is never STALLED: nothing says the turn is open."""
    from levain.cockpit.providers import SESSION_ACTIVE_S

    if waiting:
        return "waiting"
    if age_s >= SESSION_ACTIVE_S or last_kind == "reply":
        return "idle"
    if age_s >= STALL_S:
        return "stalled" if last_kind in ("prompt", "tool") else "idle"
    return "running"


# --- lost if closed: the git read ------------------------------------------------------------------------------

def _lost_scan(trees: list[tuple[tuple[Path, Path, Path], str]], clock: Callable[[], float] = time.monotonic,
               state: Callable[..., dict[str, Any]] = act.git_worktree_state
               ) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """``git_worktree_state`` for each working tree (``((common, gitdir, top), label)``) inside the activity feed's
    whole-scan deadline: the states by ``str(top)`` and the errors (each tree not read is named)."""
    got: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    t0 = clock()
    for (common, gitdir, top), label in trees:
        left = act.ACTIVITY_GIT_DEADLINE_S - (clock() - t0)
        if left <= 0:
            errors.append(f"{label}: skipped, the worktree scan passed its {act.ACTIVITY_GIT_DEADLINE_S:g}s deadline")
            continue
        try:
            got[str(top)] = state(common, gitdir, top, min(act.ACTIVITY_GIT_CALL_S, left))
        except subprocess.TimeoutExpired:
            errors.append(f"{label}: the worktree read timed out")
        except act.GitRefused as exc:
            errors.append(f"{label}: {exc}")
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(f"{label}: worktree unreadable ({type(exc).__name__})")
    return got, errors


class _LostCache:
    """The worktree scan, rebuilt at most once per ``LOST_CACHE_S`` for one set of trees; concurrent reads share a
    rebuild, and each gets copies."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._at = 0.0
        self._key: tuple[str, ...] | None = None
        self._got: tuple[dict[str, dict[str, Any]], list[str]] = ({}, [])

    def get(self, trees: list[tuple[tuple[Path, Path, Path], str]], fresh: bool,
            scan: Callable[..., tuple[dict[str, dict[str, Any]], list[str]]]
            ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        key = tuple(str(t[0][2]) for t in trees)
        with self._lock:
            if fresh or self._key != key or time.monotonic() - self._at > LOST_CACHE_S:
                self._got = scan(trees)
                self._key, self._at = key, time.monotonic()
            return ({k: {**v, "dirty_names": list(v["dirty_names"])} for k, v in self._got[0].items()},
                    list(self._got[1]))


# --- the tasks -------------------------------------------------------------------------------------------------

def _consent_tasks(listing: Any, worker_of: Callable[[str, str], str | None]) -> tuple[list[dict[str, Any]], int]:
    """A task per request an agent waits on (pending, or undecided and waiting on a late ruling): its card is the
    Decisions row, its label that row's (agent and tool, never the request's body)."""
    tasks, bad = [], 0
    for e in (*listing.pending, *listing.undecided):
        try:
            if e.state not in ("pending", "undecided"):
                continue
            r = e.request
            card = f"{CONSENT_ROW_PREFIX}{r.id}"
            w = worker_of(r.agent, r.agent_session_id)
            tasks.append({"id": card, "kind": "consent", "label": _wire(f"{r.agent} · {r.tool_name}"),
                          "state": "waiting", "waits_on": [card], "card": card, "outcomes": [], "cost": dict(NO_COST),
                          "workers": [w] if w else []})
        except Exception:  # noqa: BLE001 - one entry that does not build is counted, never the source
            bad += 1
    return tasks, bad


def _question_tasks(open_qs: list[Any], worker_of: Callable[[str, str], str | None]) -> list[dict[str, Any]]:
    """A task per open question: its card the Decisions row, its label the question's number (its words are served
    only to a token holder); a question tied to a consent request also waits on that request."""
    tasks = []
    for q in open_qs:
        card = f"{QUESTION_ROW_PREFIX}{q.n}"
        waits = [card]
        link = getattr(q, "link", None)
        if link is not None and isinstance(getattr(link, "request_id", None), str):
            waits.append(f"{CONSENT_ROW_PREFIX}{_wire(link.request_id)}")
        w = worker_of(q.agent, q.agent_session_id)
        tasks.append({"id": card, "kind": "question", "label": _wire(f"{q.agent} · question #{q.n}"),
                      "state": "waiting", "waits_on": waits, "card": card, "outcomes": [], "cost": dict(NO_COST),
                      "workers": [w] if w else []})
    return tasks


def _dispatch_tasks(directory: Path, now: datetime) -> tuple[list[dict[str, Any]], list[str]]:
    """A task per dispatch contract still open, or finished in the last ``DISPATCH_DONE_H`` hours, through the job
    store's and the outcome ledger's public readers. Neither store is created here: a directory without one has
    no contracts (or no labels). A contract has no worker: the session that filed it is not running it, and the job
    record names no session that does. Raises for a store that cannot be read."""
    from levain.dispatch import jobs as djobs
    from levain.dispatch import outcomes as dout

    errors: list[str] = []
    if not (directory / djobs.DB_NAME).is_file():
        return [], errors
    # JobStore.list() sweeps lapsed leases first: the store's own rule for every read (jobs.py), not a cockpit write
    got = djobs.JobStore(directory).list()
    labels: dict[str, list[dict[str, Any]]] = {}
    if (directory / dout.DB_NAME).is_file():
        try:
            for row in dout.OutcomeStore(directory).rows():
                ok = _LEDGER_OK.get(row.label)
                at = act._parse(row.at)
                if ok is not None and at is not None:
                    labels.setdefault(row.job_id, []).append({"label": row.label.replace("_", " "),
                                                              "at": _iso(at), "ok": ok})
        except Exception as exc:  # noqa: BLE001 - an unread ledger is named; the contracts still show
            errors.append(f"outcomes: unreadable ({type(exc).__name__})")
    since = now - timedelta(hours=DISPATCH_DONE_H)
    tasks = []
    bad = 0
    for job in got:
        try:
            state = _JOB_STATE[job.state]
            last = job.history[-1]
            last_at = act._parse(last.at)
            if job.state in djobs.TERMINAL and (last_at is None or last_at < since):
                continue
            outcomes = list(labels.get(job.id, []))
            if job.state in djobs.TERMINAL and last_at is not None:
                outcomes.append({"label": _wire(f"{job.state} ({last.cause.replace('_', ' ')})"), "at": _iso(last_at),
                                 "ok": job.state == "adopted"})
            outcomes.sort(key=lambda o: o["at"], reverse=True)
            tasks.append({"id": f"dispatch:{job.id}", "kind": "dispatch",
                          "label": _wire(f"dispatch · {job.contract.template.template_id}"),
                          "state": state, "waits_on": [f"dispatch:{job.id}"] if state == "waiting" else [],
                          "card": None, "outcomes": outcomes, "cost": dict(NO_COST), "workers": []})
        except Exception:  # noqa: BLE001 - one job that does not build is counted, never the store
            bad += 1
    if bad:
        errors.append(f"dispatch: {bad} contract(s) that did not read")
    return tasks, errors


def _spore_tasks(spores: list[Any]) -> list[dict[str, Any]]:
    """The first ``SPORES_MAX`` open loops as tasks. The spore store links no loop to a session or a record, so a
    loop is never WAITING, and RUNNING only if a worker is on it (``workers`` is empty today, so STUCK: silence, no
    record). A recent touch is not a worker. No timestamp is on the task wire, so the loop's age is not carried."""
    tasks = []
    for s in spores[:SPORES_MAX]:
        workers: list[str] = []
        tasks.append({"id": f"spore:{_wire(s.id)}", "kind": "spore", "label": _wire(s.text),
                      "state": "running" if workers else "stuck", "waits_on": [], "card": None, "outcomes": [],
                      "cost": dict(NO_COST), "workers": workers})
    return tasks


# --- the read --------------------------------------------------------------------------------------------------

def topology_reader(*, install_root: Path | None,
                    consent: Callable[[], Callable[[datetime], Any] | None],
                    questions: Callable[[], Any],
                    hub_label: Callable[[Any], str],
                    spores: Callable[[Any], list[Any]] | None = None,
                    dispatch_dir: Callable[[], Path | None] = lambda: None,
                    home: str | None = None,
                    scan: Callable[..., tuple[dict[str, dict[str, Any]], list[str]]] = _lost_scan
                    ) -> Callable[[Any], Result]:
    """The ``topology`` provider's read: ``read(ctx)``. ``consent`` and ``questions`` are looked up at each read
    (the server attaches them after the cockpit is built); ``dispatch_dir`` names the directory holding the
    dispatch and outcome stores (the consent queue's directory; this read creates neither store), ``spores`` the entity's open
    loops (raising for a store that cannot be read), ``hub_label`` the entity's name. ``scan`` is the worktree
    scan (a test's seam)."""
    cache = _LostCache()

    def read(ctx: Any) -> Result:
        from levain.cockpit.providers import SESSION_ACTIVE_S, _context_chip

        now = ctx.now
        errors: list[str] = []
        unreadable: list[str] = []

        def blind(name: str, why: str) -> None:
            unreadable.append(name)
            errors.append(f"{name}: {why}")

        if install_root is None:
            return Fault("topology is blind: no install root, the sessions are kept in the install")
        try:
            recs = _records(install_root, now)
        except OSError as exc:
            return Fault(f"topology is blind: the session records are unreadable ({type(exc).__name__})")

        # who waits on a record: a session (agent, session id) -> its first card
        waiting_card: dict[tuple[str, str], str] = {}
        listing = None
        reader = consent()
        if reader is None:
            blind("decisions", "no consent queue is attached to this cockpit")
        else:
            try:
                listing = reader(now)
                for e in (*listing.pending, *listing.undecided):
                    if e.state in ("pending", "undecided"):
                        waiting_card.setdefault((e.request.agent, e.request.agent_session_id),
                                                f"{CONSENT_ROW_PREFIX}{e.request.id}")
            except Exception as exc:  # noqa: BLE001 - an unread queue is named, never shown as nothing waiting
                listing = None
                blind("decisions", f"unreadable ({type(exc).__name__})")
        store = questions()
        open_qs: list[Any] | None = None
        if store is None:
            blind("questions", "no question store is attached to this cockpit")
        else:
            try:
                open_qs = list(store.open_questions())
                for q in open_qs:
                    waiting_card.setdefault((q.agent, q.agent_session_id), f"{QUESTION_ROW_PREFIX}{q.n}")
            except Exception as exc:  # noqa: BLE001
                open_qs = None
                blind("questions", f"unreadable ({type(exc).__name__})")

        groups: dict[str, dict[str, Any]] = {}
        parent_of: dict[str, str] = {}             # a repository group's parent directory's name, for a label clash
        nodes: list[dict[str, Any]] = []
        by_session: dict[tuple[str, str], str] = {}
        live: list[tuple[str, str]] = []           # (node id, cwd) of the sessions Lost-if-closed reads
        walked: dict[str, tuple[tuple[Path, Path, Path] | None, str | None]] = {}

        def tree_of(cwd: str) -> tuple[tuple[Path, Path, Path] | None, str | None]:
            if cwd not in walked:
                walked[cwd] = act.find_worktree(cwd, home) if os.path.isabs(cwd) else (None, None)
            return walked[cwd]

        for rec in recs:
            stem, agent, sid, cwd = rec["_stem"], rec["agent"], rec["session_id"], rec["cwd"]
            nid = f"session:{stem}"
            found, _why = tree_of(cwd)
            if found is not None:
                repo = act.repo_label(found[0])
                # the id is a hash of the common git dir, so two repositories with one basename are two groups
                gid = "repo:" + hashlib.sha1(str(found[0]).encode("utf-8", "surrogateescape")).hexdigest()[:8]
                if gid not in groups:
                    main = found[0].parent if found[0].name == ".git" else found[0]
                    parent_of[gid] = act._base(str(main.parent))
                    groups[gid] = {"id": gid, "label": _wire(repo), "repo": _wire(repo)}
            else:
                base = act._base(cwd) or "?"
                gid = f"dir:{_wire(base)}"
                groups.setdefault(gid, {"id": gid, "label": _wire(base), "repo": None})
            last_log, last_kind, rate = (None, None, None)
            age = (now - rec["_seen"]).total_seconds()
            if age < SESSION_ACTIVE_S:
                last_log, last_kind, rate = _log_signal(install_root, stem, now)
            # no log: the record alone, which a turn's end never updates (so it never reads STALLED)
            last = max(t for t in (rec["_seen"], last_log) if t is not None)
            card = waiting_card.get((agent, sid))
            _chip, reading = _context_chip(rec)
            tokens = reading.tokens if reading is not None and not reading.compacted else None
            model = _wire(reading.model) if reading is not None and reading.model else None
            first = act._parse(rec.get("first_seen")) or rec["_seen"]
            state = _node_state(card is not None, (now - last).total_seconds(), last_kind)
            nodes.append({"id": nid, "group": gid, "parent": None,
                          "label": _wire(f"{agent} · {act._base(cwd) or '/'}"), "agent": _wire(agent),
                          "model": model, "state": state, "tool": None, "tokens": tokens,
                          "started": _iso(min(first, rec["_seen"])), "last_event": _iso(last), "rate": rate,
                          "card": card})
            by_session[(agent, sid)] = nid
            if (now - last).total_seconds() < SESSION_ACTIVE_S:
                live.append((nid, cwd))

        clash: dict[str, int] = {}
        for g in groups.values():
            if g["id"] in parent_of:
                clash[g["label"]] = clash.get(g["label"], 0) + 1
        for g in groups.values():
            if g["id"] in parent_of and clash[g["label"]] > 1:
                g["label"] = _wire(f"{g['repo']} ({parent_of[g['id']]})")

        tasks: list[dict[str, Any]] = []
        worker_of = lambda a, s: by_session.get((a, s))  # noqa: E731
        if listing is not None:
            got, bad = _consent_tasks(listing, worker_of)
            tasks += got
            if bad:
                errors.append(f"decisions: {bad} request(s) that did not read")
        if open_qs is not None:
            try:
                tasks += _question_tasks(open_qs, worker_of)
            except Exception as exc:  # noqa: BLE001 - a question that does not build names the source
                blind("questions", f"unreadable ({type(exc).__name__})")
        ddir = dispatch_dir()
        if ddir is None:
            blind("dispatch", "no consent directory is attached, so no dispatch store to read")
        else:
            try:
                got, errs = _dispatch_tasks(Path(ddir), now)
                tasks += got
                errors += errs
            except Exception as exc:  # noqa: BLE001 - an unread store is named, never shown as no contracts
                blind("dispatch", f"unreadable ({type(exc).__name__})")
        if spores is None:
            blind("spores", "no spore store is attached")
        else:
            try:
                tasks += _spore_tasks(list(spores(ctx)))
            except Exception as exc:  # noqa: BLE001
                blind("spores", f"unreadable ({type(exc).__name__})")
        on_task = {w for t in tasks for w in t["workers"]}
        # unattached work is the work of sessions that are ACTIVE (running, waiting, stalled); an idle session
        # leaves the Work lens (it stays a node), and no state here is ever `done`: silence is not completion
        loose = [n for n in nodes if n["id"] not in on_task and n["state"] != "idle"]
        if loose:
            tasks.append({"id": UNATTACHED_ID, "kind": "unattached", "label": "unattached work",
                          "state": "stuck" if any(n["state"] == "stalled" for n in loose) else "running",
                          "waits_on": [], "card": None, "outcomes": [], "cost": dict(NO_COST),
                          "workers": [n["id"] for n in loose]})

        # lost if closed: one read per working tree the live sessions are in, deduplicated, the newest first
        trees: list[tuple[tuple[Path, Path, Path], str]] = []
        seen_tops: set[str] = set()
        for _nid, cwd in live:
            found, why = tree_of(cwd)
            if why is not None:
                errors.append(f"{_wire(act._base(cwd))}: {why}")
            if found is None or str(found[2]) in seen_tops:
                continue
            seen_tops.add(str(found[2]))
            trees.append((found, _wire(act._base(str(found[2])))))
        if len(trees) > LOST_WORKTREES_MAX:
            errors += [f"{label}: not read, past the cap of {LOST_WORKTREES_MAX} working trees"
                       for _f, label in trees[LOST_WORKTREES_MAX:]]
            trees = trees[:LOST_WORKTREES_MAX]
        states, errs = cache.get(trees, bool(getattr(ctx, "fresh", False)), scan) if trees else ({}, [])
        errors += errs
        lost = []
        for nid, cwd in live:
            found, _why = tree_of(cwd)
            st = states.get(str(found[2])) if found is not None else None
            lost.append({"node": nid, "dirty": st["dirty"] if st else None,
                         "dirty_names": [_wire(n, base=True) for n in st["dirty_names"]] if st else [],
                         "unpushed": st["unpushed"] if st else None,
                         "branches": [_wire(st["branch"])] if st and st["unpushed"] and st["branch"] else [],
                         "uncaptured_episodes": None, "running_children": None})

        errors = list(dict.fromkeys(errors))
        return Read(value={
            "hub": {"id": HUB_ID, "label": _wire(hub_label(ctx) or "Levain")},
            "groups": list(groups.values()), "nodes": nodes, "conflicts": None,
            "counts": {"tokens_today": None},
            # the minute, not the instant: a value stamped per read would change on every poll of an idle install
            "as_of": _iso(now.replace(second=0, microsecond=0)),
            "tasks": tasks, "lost_if_closed": lost, "errors": errors, "unreadable": list(dict.fromkeys(unreadable)),
        })
    return read
