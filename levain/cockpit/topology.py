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
``STALL_S``. An open loop nobody is working is neither: it is OPEN, a record with an age and a heat (the Open loops
panel's own words), not silence.

"Lost if closed" is per live session, read by ``activity.git_worktree_state`` (its status read runs on a private git
dir the cockpit owns, which takes only allow-listed, shape-checked settings from the repository's config and
``config.worktree``; see that function), bounded by the same whole-scan deadline,
each working tree past it named, and cached for ``LOST_CACHE_S``.

File conflicts come from the session log: the PostToolUse hook logs ``Name: file_path`` for an Edit/Write, and two
live nodes with the same whole path inside the live window are a conflict: one record per path naming every node on it
(basename only on the wire; ``null`` when live nodes exist and none has a log), with ``conflicts_total`` the count before
the list's cap. An ``apply_patch`` logs every file it touches, one a line.

Not tracked on this branch, sent as null (never a zero, never an estimate): a session's current tool (the session
log holds it only inside text this token-free panel must not carry), tokens today, every task's cost (no dispatch spend key and no ledger cost column exist yet), uncaptured
episodes (episodes carry no session) and running children (no hook records a subagent). What does reach the wire is
display-safe: a consent task is its agent and tool, a question its number, a dispatch contract its goal's first
line (``_wire``'d; else its template), never a request's body; but a file name, a branch name, a repository or directory basename and an open loop's
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from levain.cockpit import activity as act
from levain.cockpit.display_safe import visible
from levain.cockpit.registry import CONSENT_ROW_PREFIX, QUESTION_ROW_PREFIX, opened_day, parse_date
from levain.cockpit.results import Fault, Read, Result

__all__ = ["LOST_CACHE_S", "LOST_WORKTREES_MAX", "NOT_KEPT", "SPORES_MAX", "STALL_S", "topology_reader"]

STALL_S = 5 * 60                   # a session mid-turn (last event a prompt or a tool) silent this long is STALLED
RATE_WINDOW_S = 5 * 60             # a node's rate: logged events per minute over this window
SPORES_MAX = 20                    # open loops shown as tasks, in the store's order
DISPATCH_DONE_H = 14               # a finished contract stays on the board this long (the sky's window)
LOST_WORKTREES_MAX = act.ACTIVITY_REPOS_MAX
LOST_CACHE_S = 30.0
LABEL_MAX = 48
UNNAMED = "(unnamed)"
_MARKER = re.compile(r"⟨U\+[0-9A-F]{4,6}⟩")
#: a session record's file stem, as ``_session_record`` accepts it (the hash of its agent and session id): the
#: node id is ``session:<stem>``, the Sessions panel's row id, so a stem of any other shape is never drawn
_STEM = re.compile(r"[0-9a-f]{24}")
#: the room a clashing repository label's parent-directory name may take, so the suffix is never cut off
PARENT_MAX = 16
HUB_ID = "levain"
UNATTACHED_ID = "unattached"
NO_COST = {"usd": None, "tokens": None, "source": None}
#: the reason a past read (``read_at``) gives for a source it cannot rebuild for that instant: the source is named in
#: ``unreadable``, its fields are null (unknown), and it is never drawn as empty (absence is not health)
NOT_KEPT = "not kept for the past"
#: the questions a past read scans, newest first; with more than this an older question answered after T is not seen
QUESTIONS_SCAN = 500
#: a session log the hooks never rotated is whole. Rotation (past ``session_log.LOG_TAIL``, the hooks' own rotation
#: size) keeps the newest half of whole lines, so a rotated log is never smaller than half of it less one line; one
#: event line is at most ~24 KB escaped, so a log under half less this margin was never rotated
LOG_LINE_MARGIN = 32 * 1024

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


def _wire(text: Any, *, base: bool = False, limit: int = LABEL_MAX) -> str:
    """The ONE normaliser every string this provider puts on the wire goes through: ``visible`` (no invisible or
    format character); for a ``base`` name, ``/`` and ``\\`` shown as visible markers (a name is a basename, never a
    path, and is never refused); whitespace collapsed to single spaces; an empty result the fixed ``UNNAMED``; then
    the ``limit`` cut (``LABEL_MAX`` unless a caller keeps room for a suffix), which never splits a ``⟨U+XXXX⟩``
    marker. The engine's validator refuses what this never emits."""
    s = visible(str(text))
    if base:
        s = s.replace("/", "⟨U+002F⟩").replace("\\", "⟨U+005C⟩")
    s = " ".join(s.split())
    if not s:
        return UNNAMED
    if len(s) <= limit:
        return s
    cut = limit - 1
    for m in _MARKER.finditer(s):
        if m.start() < cut < m.end():
            cut = m.start()
            break
    return s[:cut] + "…"


def _hash(text: str, n: int = 8) -> str:
    """A short stable hash of a path, for an id that must tell two places apart without carrying either path."""
    return hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()[:n]


def _qualified(repo: str, parent: str, frag: str | None = None) -> str:
    """A clashing repository label: ``repo (parent)``, or ``repo (parent · frag)`` when that still clashes. The
    parent takes at most ``PARENT_MAX`` and the repository name is cut to leave the suffix whole."""
    sfx = f" ({_wire(parent, limit=PARENT_MAX)}{f' · {frag}' if frag else ''})"
    return _wire(repo, limit=LABEL_MAX - len(sfx)) + sfx


# --- the sessions ----------------------------------------------------------------------------------------------

def _records(install_root: Path, now: datetime, back_s: float = 0.0, *, at: datetime | None = None,
             meta: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The session records the Sessions panel lists (its reader, caps and 24-hour horizon), newest first. Raises
    ``OSError`` for a sessions directory that cannot be read; no directory is no sessions. ``back_s`` widens the
    horizon by that much (a past read at ``now - back_s`` keeps every record last seen inside ITS 24 hours; the
    caller then drops what had not started, or was quiet past the horizon, at that instant).

    A past read passes ``at`` (T): every record in the scan is read and the ones that could have been live at T are kept
    BEFORE the ``SESSIONS_SHOWN`` cap (the newest files by mtime are the sessions still running now, which would
    otherwise crowd out the ones that ended after T). When the scan limit or that cap still leaves out records that
    might have been live at T, ``meta["truncated"]`` says so: the caller names the source, never draws it whole."""
    from levain.cockpit.providers import (SESSION_LISTED_S, SESSION_SKEW_S, SESSIONS_SCAN, SESSIONS_SHOWN,
                                          _session_record)

    sdir = install_root / ".levain" / "sessions"
    found: list[tuple[float, Path]] = []
    try:
        with os.scandir(sdir) as it:
            for n, e in enumerate(it):
                if n >= SESSIONS_SCAN:
                    if meta is not None:
                        meta["truncated"] = True
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
    for _mt, path in (found if at is not None else found[:SESSIONS_SHOWN]):
        rec = _session_record(path)
        if rec is None:
            continue
        age = (now - rec["_seen"]).total_seconds()
        if age < -SESSION_SKEW_S or age > SESSION_LISTED_S + back_s:
            continue
        if at is not None:
            first = min(act._parse(rec.get("first_seen")) or rec["_seen"], rec["_seen"])
            if first > at:
                continue                    # not started at T
            if len(out) >= SESSIONS_SHOWN:
                if meta is not None:
                    meta["truncated"] = True
                break
        out.append(rec)
    return out


def _log_signal(install_root: Path, stem: str, now: datetime, *, past: bool = False
                ) -> tuple[datetime | None, str | None, float | None]:
    """From the session's event log (Q8), counts and stamps only, never a word of it: the newest event's time and
    kind (prompt, reply, tool), and events per minute over the last ``RATE_WINDOW_S``. No log is (None, None, None):
    the rate is unknown, never a zero. A ``past`` read (``now`` pinned at T) counts only events at or before T (the
    live read allows a few minutes of clock skew ahead; a past read allows none: an event after T is the future)."""
    from levain.cockpit.session_log import _event, _tail

    raw = _tail(install_root / ".levain" / "sessions" / f"{stem}.log.jsonl")
    if not raw:
        return None, None, None
    ahead = 0 if past else 300
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
        if at is None or (at - now).total_seconds() > ahead:
            continue
        if last_at is None or at >= last_at:
            last_at, last_kind = at, ev["kind"]
        if (now - at).total_seconds() <= RATE_WINDOW_S:
            recent += 1
    return last_at, last_kind, round(recent / (RATE_WINDOW_S / 60), 2)


def _events_since(install_root: Path, stem: str, since: datetime, now: datetime) -> tuple[int, bool] | None:
    """Logged events at or after ``since`` (and not after ``now``) in the session's kept log, and whether that count is
    the whole of it (False: the log was rotated past ``since``, so it is a floor). None when there is no log."""
    from levain.cockpit.session_log import _event, _tail

    raw = _tail(install_root / ".levain" / "sessions" / f"{stem}.log.jsonl")
    if not raw:
        return None
    n = 0
    for line in raw.split(b"\n"):
        ev = _event(line) if line.strip() else None
        at = act._parse(ev["at"]) if ev is not None else None
        if at is not None and since <= at <= now + timedelta(seconds=300):
            n += 1
    return n, _log_reaches(install_root, stem, since) is not False


def _log_reaches(install_root: Path, stem: str, since: datetime) -> bool | None:
    """Whether the session's kept log still holds every event from ``since`` on (a past read's window): a log the
    hooks never rotated (smaller than half their rotation size, less one line) holds all of them, and a rotated one
    holds them only if its oldest kept event is at or before ``since`` (rotation drops the oldest half; appends are in
    time order). None when there is no log (the record alone decides, as on a live read)."""
    from levain.cockpit.session_log import LOG_TAIL, _event, _tail

    path = install_root / ".levain" / "sessions" / f"{stem}.log.jsonl"
    try:
        size = os.stat(path, follow_symlinks=False).st_size
    except OSError:
        return None
    if size < LOG_TAIL // 2 - LOG_LINE_MARGIN:
        return True
    raw = _tail(path)
    if raw is None:
        return None
    for line in raw.split(b"\n"):
        ev = _event(line) if line.strip() else None
        at = act._parse(ev["at"]) if ev is not None else None
        if at is not None:
            return at <= since
    return False


#: the tools whose logged argument is the file they write (the PostToolUse hook logs ``Name: file_path``)
_WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"})
CONFLICTS_MAX = 20
_MORE_PATHS = re.compile(r"\(\+\d+ more\)")   # the hook's count of the patch paths it did not list


def _edit_paths(install_root: Path, stem: str, cwd: str, now: datetime, window_s: float) -> set[str] | None:
    """The files a session's hook logged an Edit/Write on within ``window_s``, as normalised absolute paths (kept
    inside this module: only a basename leaves it). A tool event is ``Name: argument``; an event whose text was cut
    at the log's cap names no whole path and is skipped, never completed. No log is None (unknown, not "none")."""
    from levain.cockpit.session_log import _event, _tail

    raw = _tail(install_root / ".levain" / "sessions" / f"{stem}.log.jsonl")
    if raw is None:
        return None
    out: set[str] = set()
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        ev = _event(line)
        if ev is None or ev["kind"] != "tool" or ev["cut"]:
            continue
        at = act._parse(ev["at"])
        if at is None or not 0 <= (now - at).total_seconds() <= window_s:
            continue
        name, sep, arg = ev["text"].partition(": ")
        if not sep or name not in _WRITE_TOOLS:
            continue
        # an apply_patch logs every file it touches, one per line (the hook's bounded list, then "(+N more)"); any other
        # tool's argument is one path, and a multi-line one is no path
        args = arg.split("\n") if name == "apply_patch" else [arg]
        for one in args:
            one = one.strip()
            if not one or "\n" in one or _MORE_PATHS.fullmatch(one):
                continue
            if not os.path.isabs(one):
                if not os.path.isabs(cwd):
                    continue
                one = os.path.join(cwd, one)
            out.add(os.path.normpath(one))
    return out


def _node_state(waiting: bool, age_s: float, last_kind: str | None) -> str:
    """WAITING (a record) > STALLED (mid-turn and silent) > RUNNING > IDLE (quiet past the Sessions panel's Active
    line, or its turn ended in a reply). Silence with no log is never STALLED: nothing says the turn is open. A
    coordinator wake (``wake``) opens a turn as a prompt does, and its ``wake_reply`` ends it."""
    from levain.cockpit.providers import SESSION_ACTIVE_S

    if waiting:
        return "waiting"
    if age_s >= SESSION_ACTIVE_S or last_kind in ("reply", "wake_reply"):
        return "idle"
    if age_s >= STALL_S:
        return "stalled" if last_kind in ("prompt", "tool", "wake") else "idle"
    return "running"


def _conflicts(install_root: Path, live: list[tuple[str, str]], stems: dict[str, str], now: datetime,
               window_s: float) -> tuple[list[dict[str, Any]] | None, int | None]:
    """Live nodes whose hooks logged an Edit/Write on the same path within ``window_s`` (the live window): ONE record
    per path, naming every node on it (two or more), basename only on the wire, and the number of conflicting paths
    found, which exceeds the list when ``CONFLICTS_MAX`` cut it. ``(None, None)`` when live nodes exist and none has a
    readable log (nothing was observed, so "no conflicts" would be absence rendered as health); a list (maybe empty)
    and its total once any log was read."""
    seen: dict[str, set[str]] = {}
    read_any = False
    for nid, cwd in live:
        paths = _edit_paths(install_root, stems[nid], cwd, now, window_s)
        if paths is None:
            continue
        read_any = True
        for path in paths:
            seen.setdefault(path, set()).add(nid)
    if live and not read_any:
        return None, None
    out = [{"path": _wire(os.path.basename(path), base=True), "nodes": sorted(seen[path])}
           for path in sorted(seen) if len(seen[path]) >= 2]
    return out[:CONFLICTS_MAX], len(out)


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

def _consent_waiting(listing: Any, at: datetime | None, listed_at: datetime | None = None) -> list[Any]:
    """The requests an agent waits on. Live (``at`` None): the pending and the undecided (waiting on a late ruling).
    At a past instant T: every listed request filed at or before T and not decided by T (``decided_at`` is when it
    was answered, ruled or expired; an undecided one waits on a ruling whatever its ``decided_at``), from the
    pending, the undecided and the recently decided alike. Raises ``LookupError`` when the listing cannot answer
    for T: no decided list, or one cut at its cap before reaching back to T (a request decided in between is lost)."""
    if at is None:
        return [e for e in (*listing.pending, *listing.undecided) if e.state in ("pending", "undecided")]
    from levain.consent.listing import RECENT, RECENT_CAP

    recent = getattr(listing, "recent", None)
    if recent is None:
        raise LookupError("this consent reader lists no decided requests")
    if getattr(listing, "overflow", False) or getattr(listing, "skipped", 0):
        raise LookupError("the consent listing is incomplete (a pending list cut at its cap, or rows that did not check "
                          "out): a request waiting at T may be missing")
    if len(recent) >= RECENT_CAP:
        stamps = [t for t in (act._parse(e.decided_at) for e in recent if e.decided_at) if t is not None]
        if not stamps or min(stamps) > at:
            raise LookupError(f"more than {RECENT_CAP} requests were decided since T: the decided list does not reach it")
    if listed_at is not None and listed_at - RECENT > at:   # the listing's own horizon (a day; wider than the replay's)
        raise LookupError("T is before the decided list's horizon")
    out = []
    for e in (*listing.pending, *listing.undecided, *recent):
        filed = act._parse(e.request.received_at)
        if filed is None or filed > at:
            continue
        decided = act._parse(e.decided_at) if e.decided_at and e.state != "undecided" else None
        if decided is None or decided > at:
            out.append(e)
    return out


def _consent_tasks(entries: list[Any], worker_of: Callable[[str, str], str | None]
                   ) -> tuple[list[dict[str, Any]], int]:
    """A task per request an agent waits on (``_consent_waiting``): its card is the Decisions row, its label that
    row's (agent and tool, never the request's body)."""
    tasks, bad = [], 0
    for e in entries:
        try:
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


def _dispatch_label(job: Any) -> str:
    """A dispatch task's name: the first line of the contract's own goal (the text the operator signed), else
    ``dispatch · <template id>`` when the goal does not read."""
    try:
        first = next((ln for ln in job.contract.goal.splitlines() if ln.strip()), "")
        if first.strip():
            return _wire(first)
    except Exception:  # noqa: BLE001 - a goal that does not read leaves the template's name
        pass
    return _wire(f"dispatch · {job.contract.template.template_id}")


def _questions_at(store: Any, at: datetime | None) -> list[Any]:
    """The questions an agent waits on. Live: the store's open questions. At a past instant T: every question filed
    at or before T and not answered by T (the store keeps both stamps and never deletes), oldest first; raises
    ``LookupError`` when the store cannot answer for T (no ``recent`` read, or more than ``QUESTIONS_SCAN`` questions,
    so an older one answered after T would not be seen)."""
    if at is None:
        return list(store.open_questions())
    recent = getattr(store, "recent", None)
    if recent is None:
        raise LookupError("this question store lists no answered questions")
    got = list(recent(QUESTIONS_SCAN))
    if len(got) >= QUESTIONS_SCAN:
        raise LookupError(f"more than {QUESTIONS_SCAN} questions: an older one answered after T would not be seen")
    out = []
    for q in got:
        filed = act._parse(q.filed_at)
        if filed is None or filed > at:
            continue
        answered = act._parse(q.answer.answered_at) if q.answer is not None else None
        if answered is None or answered > at:
            out.append(q)
    return sorted(out, key=lambda q: q.n)


def _dispatch_tasks(directory: Path, now: datetime, *, past: bool = False) -> tuple[list[dict[str, Any]], list[str]]:
    """A task per dispatch contract still open, or finished in the last ``DISPATCH_DONE_H`` hours, through the job
    store's and the outcome ledger's public readers. Neither store is created here: a directory without one has
    no contracts (or no labels). A contract has no worker: the session that filed it is not running it, and neither the job
    record, its transitions nor the outcome ledger names a session that does; nor is any spend recorded (the contract's
    budget is a cap), so ``cost`` stays NO_COST and "no agent attached" is the honest drawing. Raises for a store that cannot be read.

    A ``past`` read (``now`` pinned at T) rebuilds each contract from its own history: its state is its last
    transition at or before T (a contract with none had not been filed), and its outcomes are the ledger rows and the
    terminal transition at or before T. Known edge: a lapse the store sweeps lazily is stamped when it is swept."""
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
                if ok is not None and at is not None and not (past and at > now):
                    labels.setdefault(row.job_id, []).append({"label": row.label.replace("_", " "),
                                                              "at": _iso(at), "ok": ok})
        except Exception as exc:  # noqa: BLE001 - an unread ledger is named; the contracts still show
            errors.append(f"outcomes: unreadable ({type(exc).__name__})")
    since = now - timedelta(hours=DISPATCH_DONE_H)
    tasks = []
    bad = 0
    for job in got:
        try:
            if past:
                hist = [t for t in job.history if (t_at := act._parse(t.at)) is not None and t_at <= now]
                if not hist:
                    continue            # not filed yet at T
                cur = hist[-1].to_state
            else:
                hist, cur = list(job.history), job.state
            state = _JOB_STATE[cur]
            last = hist[-1]
            last_at = act._parse(last.at)
            if cur in djobs.TERMINAL and (last_at is None or last_at < since):
                continue
            outcomes = list(labels.get(job.id, []))
            if cur in djobs.TERMINAL and last_at is not None:
                outcomes.append({"label": _wire(f"{cur} ({last.cause.replace('_', ' ')})"), "at": _iso(last_at),
                                 "ok": cur == "adopted"})
            outcomes.sort(key=lambda o: o["at"], reverse=True)
            tasks.append({"id": f"dispatch:{job.id}", "kind": "dispatch",
                          "label": _dispatch_label(job),
                          "state": state, "waits_on": [f"dispatch:{job.id}"] if state == "waiting" else [],
                          "card": None, "outcomes": outcomes, "cost": dict(NO_COST), "workers": []})
        except Exception:  # noqa: BLE001 - one job that does not build is counted, never the store
            bad += 1
    if bad:
        errors.append(f"dispatch: {bad} contract(s) that did not read")
    return tasks, errors


#: the heat an open loop carries, as the spore store's own tier names it (the Open loops panel's word for it)
HEATS = ("hot", "warm", "cold", "parked")


def _opened(created: Any, now: datetime) -> str | None:
    """When a spore was opened, as an instant: its own ISO stamp, or its date taken as local midnight (the Open loops
    panel counts calendar days in the install's own clock, so the age the renderer draws agrees with it). None when
    the store has no usable date: unknown, never a guess."""
    t = act._parse(created) if isinstance(created, str) else None
    if t is not None:
        return _iso(t)
    d = parse_date(created)
    if d is None:
        return None
    return _iso(datetime(d.year, d.month, d.day, tzinfo=now.astimezone().tzinfo))


def _age_days(created: Any, now: datetime) -> int | None:
    """Whole calendar days from the day a spore was opened to the provider's own day (the install's local day, the one the
    Open loops panel counts in): the number the page draws, so no browser clock or zone decides it. None when the store has no
    usable date."""
    opened = opened_day(created)
    if opened is None:
        return None
    return max(0, (now.astimezone().date() - opened).days)


def _spore_tasks(spores: list[Any], now: datetime | None = None) -> list[dict[str, Any]]:
    """The first ``SPORES_MAX`` open loops as tasks. The spore store links no loop to a session or a record, so a
    loop is never WAITING, and RUNNING only if a worker is on it (``workers`` is empty today). A loop nobody is on is
    OPEN, not STUCK: STUCK is a live worker gone silent. It carries ``opened`` (its created time) and ``heat`` (its
    tier, the Open loops panel's own value), each null when the store does not say. A recent touch is not a worker."""
    now = now or datetime.now(timezone.utc)
    tasks = []
    for s in spores[:SPORES_MAX]:
        workers: list[str] = []
        tier = getattr(s, "tier", None)
        tasks.append({"id": f"spore:{_wire(s.id)}", "kind": "spore", "label": _wire(s.text),
                      "state": "running" if workers else "open", "waits_on": [], "card": None, "outcomes": [],
                      "cost": dict(NO_COST), "workers": workers, "opened": _opened(getattr(s, "created", None), now),
                      "age_days": _age_days(getattr(s, "created", None), now),
                      "heat": tier if tier in HEATS else None})
    return tasks


def _transcript_rate(rec: dict[str, Any], now: datetime) -> float | None:
    """A node's rate from its transcript's message stamps (events per minute over ``RATE_WINDOW_S``), for a session whose
    hooks wrote no log. None when the transcript cannot be found or read: unknown, never a zero."""
    from levain.cockpit.session_context import find_transcript, recent_rate

    try:
        path, _why = find_transcript(rec)
        return recent_rate(path, rec["agent"], now, RATE_WINDOW_S) if path is not None else None
    except Exception:  # noqa: BLE001 - a transcript that will not read is an unknown rate
        return None


def _today_counts(install_root: Path, recs: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """The header's day figures. ``tokens_today``: fresh tokens (input + output + cache write; Codex input - cached +
    output) the sessions' transcripts spent since LOCAL midnight, by each record's own timestamp, summed over the
    transcripts that read; ``tokens_unread`` names how many did not (null total when none read); ``tokens_skipped`` counts subagent
    transcripts of the ones that read that were left out (the total is then a floor). ``events_today``:
    logged hook events since local midnight (``events_partial``: a log rotated past midnight, so a floor)."""
    from levain.cockpit.session_context import find_transcript, tokens_since_detail

    midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    total, read, unread, skipped = 0, 0, 0, 0
    events, logs, partial = 0, 0, False
    for rec in recs:
        try:
            path, _why = find_transcript(rec)
            got, _why, left = (tokens_since_detail(path, rec["agent"], midnight) if path is not None
                               else (None, "no transcript", 0))
        except Exception:  # noqa: BLE001
            got, left = None, 0
        if got is None:
            unread += 1
        else:
            total, read, skipped = total + got, read + 1, skipped + left
        stem = rec.get("_stem")
        if isinstance(stem, str) and _STEM.fullmatch(stem):
            ev = _events_since(install_root, stem, midnight, now)
            if ev is not None:
                events, logs, partial = events + ev[0], logs + 1, partial or not ev[1]
    return {"tokens_today": total if read else None, "tokens_read": read, "tokens_unread": unread,
            "tokens_skipped": skipped,
            "events_today": events if logs else None, "events_partial": partial}


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
    scan (a test's seam).

    ``read.read_at(ctx, iso)`` is the same read at a past instant T (F12 Replay): ONE reader with its clock pinned at
    T and every source bounded to what was known at T (events, filings, decisions, answers and transitions stamped at
    or before T). A source that cannot be rebuilt for T (the open loops, lost-if-closed, a node's model and tokens, a
    log the hooks rotated past T, a listing cut before T) is named in ``unreadable`` with the reason ``NOT_KEPT`` and
    its fields are null; it is never drawn as empty. T is checked by the engine's ``check_replay_at``: minute-aligned
    ISO inside the replay window, refused (a Fault), never clamped. The value carries ``as_of`` = T and ``replay``."""
    cache = _LostCache()

    def _read(ctx: Any, at: datetime | None) -> Result:
        from levain.cockpit.providers import SESSION_ACTIVE_S, SESSION_LISTED_S, _context_chip

        past = at is not None
        # the read's clock: a past read pins every helper at T; ``ctx.now`` stays the instant the stores are read at
        now = at if at is not None else ctx.now
        errors: list[str] = []
        unreadable: list[str] = []

        def blind(name: str, why: str) -> None:
            unreadable.append(name)
            errors.append(f"{name}: {why}")

        if install_root is None:
            return Fault("topology is blind: no install root, the sessions are kept in the install")
        try:
            meta: dict[str, Any] = {}
            recs = (_records(install_root, ctx.now, back_s=(ctx.now - now).total_seconds(), at=at, meta=meta) if past
                    else _records(install_root, ctx.now))
        except OSError as exc:
            return Fault(f"topology is blind: the session records are unreadable ({type(exc).__name__})")
        if meta.get("truncated"):
            blind("session_records", f"{NOT_KEPT} (more session records than were read: a session live at T may be missing)")

        # who waits on a record: a session (agent, session id) -> its first card
        waiting_card: dict[tuple[str, str], str] = {}
        waiting: list[Any] | None = None
        reader = consent()
        if reader is None:
            blind("decisions", "no consent queue is attached to this cockpit")
        else:
            try:
                # the listing as it is now; a past read keeps what was waiting at T (filed by T, not decided by T)
                waiting = _consent_waiting(reader(ctx.now), at, ctx.now)
                for e in waiting:
                    waiting_card.setdefault((e.request.agent, e.request.agent_session_id),
                                            f"{CONSENT_ROW_PREFIX}{e.request.id}")
            except LookupError as exc:
                waiting = None
                blind("decisions", f"{NOT_KEPT} ({exc})")
            except Exception as exc:  # noqa: BLE001 - an unread queue is named, never shown as nothing waiting
                waiting = None
                blind("decisions", f"unreadable ({type(exc).__name__})")
        store = questions()
        open_qs: list[Any] | None = None
        if store is None:
            blind("questions", "no question store is attached to this cockpit")
        else:
            try:
                open_qs = _questions_at(store, at)
                for q in open_qs:
                    waiting_card.setdefault((q.agent, q.agent_session_id), f"{QUESTION_ROW_PREFIX}{q.n}")
            except LookupError as exc:
                open_qs = None
                blind("questions", f"{NOT_KEPT} ({exc})")
            except Exception as exc:  # noqa: BLE001
                open_qs = None
                blind("questions", f"unreadable ({type(exc).__name__})")

        groups: dict[str, dict[str, Any]] = {}
        parent_of: dict[str, str] = {}             # a repository group's parent directory's name, for a label clash
        nodes: list[dict[str, Any]] = []
        by_session: dict[tuple[str, str], str] = {}
        live: list[tuple[str, str]] = []           # (node id, cwd) of the sessions Lost-if-closed reads
        live_stems: dict[str, str] = {}            # node id -> record stem, for the conflict read
        walked: dict[str, tuple[tuple[Path, Path, Path] | None, str | None]] = {}

        def tree_of(cwd: str) -> tuple[tuple[Path, Path, Path] | None, str | None]:
            if cwd not in walked:
                walked[cwd] = act.find_worktree(cwd, home) if os.path.isabs(cwd) else (None, None)
            return walked[cwd]

        unsure: set[str] = set()                   # past read: nodes whose log was rotated past T's window
        for rec in recs:
            stem, agent, sid, cwd = rec["_stem"], rec["agent"], rec["session_id"], rec["cwd"]
            if not (isinstance(stem, str) and _STEM.fullmatch(stem)):
                errors.append("sessions: a record of an unknown name, not drawn")
                continue
            nid = f"session:{stem}"
            first = min(act._parse(rec.get("first_seen")) or rec["_seen"], rec["_seen"])
            seen = rec["_seen"]
            if past:
                if first > now:
                    continue                       # not started at T
                if seen > now:
                    seen = first                   # its last_seen is after T: the record's only stamp at T is its first
            last_log, last_kind, rate = (None, None, None)
            rate_source = None
            # a past read always reads the log: the record's stamp at T may be its first, older than its activity at T
            if past or (now - seen).total_seconds() < SESSION_ACTIVE_S:
                last_log, last_kind, rate = _log_signal(install_root, stem, now, past=past)
                if rate is None and not past:
                    # no hook log: the rate from the transcript's own message stamps over the same window; a log's rate wins
                    rate = _transcript_rate(rec, now)
                    rate_source = "transcript" if rate is not None else None
                else:
                    rate_source = "log" if rate is not None else None
            rotated = past and _log_reaches(install_root, stem, now - timedelta(seconds=SESSION_ACTIVE_S)) is False
            if rotated:
                # rotated past T's window: its kind and rate at T are not kept (its newest event at or before T, when
                # one survived, is still a real event); the node is drawn from the record, as one with no log is
                last_kind, rate, rate_source = None, None, None
            # no log: the record alone, which a turn's end never updates (so it never reads STALLED)
            last = max(t for t in (seen, last_log) if t is not None)
            if past and (now - last).total_seconds() > SESSION_LISTED_S:
                continue                           # quiet past the Sessions panel's horizon at T: not listed then
            if rotated:
                unsure.add(nid)                    # counted only for a node that is drawn
            found, _why = tree_of(cwd)
            if found is not None:
                repo = act.repo_label(found[0])
                # the id is a hash of the common git dir, so two repositories with one basename are two groups
                gid = "repo:" + _hash(str(found[0]))
                if gid not in groups:
                    main = found[0].parent if found[0].name == ".git" else found[0]
                    parent_of[gid] = act._base(str(main.parent))
                    groups[gid] = {"id": gid, "label": _wire(repo), "repo": repo}
            else:
                # the id a hash of the directory, so two directories with one basename are two groups
                gid = "dir:" + _hash(os.path.normpath(cwd))
                groups.setdefault(gid, {"id": gid, "label": _wire(act._base(cwd) or "?"), "repo": None})
            card = waiting_card.get((agent, sid))
            tokens = model = None
            if not past:
                # the context reading is the transcript as it is now: a past read has no model or tokens for T
                _chip, reading = _context_chip(rec)
                tokens = reading.tokens if reading is not None and not reading.compacted else None
                model = _wire(reading.model) if reading is not None and reading.model else None
            state = _node_state(card is not None, (now - last).total_seconds(), last_kind)
            nodes.append({"id": nid, "group": gid, "parent": None,
                          "label": _wire(f"{agent} · {act._base(cwd) or '/'}"), "agent": _wire(agent),
                          "model": model, "state": state, "tool": None, "tokens": tokens,
                          "started": _iso(first), "last_event": _iso(last), "rate": rate, "rate_source": rate_source,
                          "card": card})
            by_session[(agent, sid)] = nid
            if (now - last).total_seconds() < SESSION_ACTIVE_S:
                live.append((nid, cwd))
                live_stems[nid] = stem
        if past:
            blind("context", f"{NOT_KEPT} (a session's model and tokens are read from its transcript as it is now)")
            if unsure:
                blind("session_logs", f"{NOT_KEPT} ({len(unsure)} session log(s) rotated past T: state and rate "
                                      "from the record alone)")

        # a repository label two groups share names the parent directory, then a fragment of the id if still shared
        repos = [g for g in groups.values() if g["id"] in parent_of]
        for frag in (False, True):
            clash: dict[str, int] = {}
            for g in repos:
                clash[g["label"]] = clash.get(g["label"], 0) + 1
            for g in repos:
                if clash[g["label"]] > 1:
                    g["label"] = _qualified(g["repo"], parent_of[g["id"]], g["id"][5:11] if frag else None)
        for g in repos:
            g["repo"] = _wire(g["repo"])

        tasks: list[dict[str, Any]] = []
        worker_of = lambda a, s: by_session.get((a, s))  # noqa: E731
        if waiting is not None:
            got, bad = _consent_tasks(waiting, worker_of)
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
                got, errs = _dispatch_tasks(Path(ddir), now, past=past)
                tasks += got
                errors += errs
            except Exception as exc:  # noqa: BLE001 - an unread store is named, never shown as no contracts
                blind("dispatch", f"unreadable ({type(exc).__name__})")
        if spores is None:
            blind("spores", "no spore store is attached")
        elif past:
            # the store dates a loop's opening by DAY (a loop opened on T's day cannot be placed before or after T),
            # and this read is handed only the loops open now (one resolved since T is not among them)
            blind("spores", f"{NOT_KEPT} (a loop is dated by day, and only the loops open now are read)")
        else:
            try:
                tasks += _spore_tasks(list(spores(ctx)), now)
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

        lost = []
        if past:
            # git keeps no worktree state for a past instant: each node live at T, every count unknown
            lost = [{"node": nid, "dirty": None, "dirty_names": [], "unpushed": None, "branches": [],
                     "uncaptured_episodes": None, "running_children": None} for nid, _cwd in live]
            if live:
                blind("lost_if_closed", f"{NOT_KEPT} (a worktree's files and commits are read as they are now)")
        else:
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
            for nid, cwd in live:
                found, _why = tree_of(cwd)
                st = states.get(str(found[2])) if found is not None else None
                lost.append({"node": nid, "dirty": st["dirty"] if st else None,
                             "dirty_names": [_wire(n, base=True) for n in st["dirty_names"]] if st else [],
                             "unpushed": st["unpushed"] if st else None,
                             "branches": [_wire(st["branch"])] if st and st["unpushed"] and st["branch"] else [],
                             "uncaptured_episodes": None, "running_children": None})

        if past and any(nid in unsure for nid, _cwd in live):
            # a live node at T whose log no longer holds T's window: a conflict on it would be missed, so the list
            # is unknown, never "none"
            conflicts, conflicts_total = None, None
            blind("conflicts", f"{NOT_KEPT} (a live session's log rotated past T's window)")
        else:
            conflicts, conflicts_total = _conflicts(install_root, live, live_stems, now, SESSION_ACTIVE_S)

        errors = list(dict.fromkeys(errors))
        value = {
            # the entity's name as it is now (a label, not a reading: no history of it is kept)
            "hub": {"id": HUB_ID, "label": _wire(hub_label(ctx) or "Levain")},
            "groups": list(groups.values()), "nodes": nodes, "conflicts": conflicts,
            "conflicts_total": conflicts_total,
            "counts": ({"tokens_today": None} if past else _today_counts(install_root, recs, now)),
            # the minute, not the instant: a value stamped per read would change on every poll of an idle install
            "as_of": _iso(now.replace(second=0, microsecond=0)),
            "tasks": tasks, "lost_if_closed": lost, "errors": errors, "unreadable": list(dict.fromkeys(unreadable)),
        }
        if past:
            value["replay"] = True
        return Read(value=value)

    def read(ctx: Any) -> Result:
        return _read(ctx, None)

    def read_at(ctx: Any, iso: str) -> Result:
        """The read at the past instant ``iso`` (F12 Replay). A T the engine's rule refuses is a Fault, never clamped."""
        from levain.cockpit.engine import QueryError, check_replay_at

        try:
            at = check_replay_at(iso, ctx.now)
        except QueryError as exc:
            return Fault(f"topology replay refused: {exc}")
        return _read(ctx, at)

    read.read_at = read_at  # type: ignore[attr-defined]  - the engine finds a provider's past read here
    return read
