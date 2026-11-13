"""levain.desk.board_evidence — the board's git evidence readers (READY_SCHEMA v2, slice B2).

:mod:`levain.desk.board_state` is pure; this is the half that reads. :func:`read_git_evidence` runs git (read-only: it
never fetches, never writes a ref, a note or the index) and returns the evidence object (``evidence_v`` 1) for the
sources levain owns: ``fetched_at``, ``main``, ``chain``, ``refs``/``merged``, ``branches`` and ``unattributed``.
:func:`read_documents` stats the paths of ``document``/``run`` items. ``seats``, ``band`` and ``waiters`` are FLOW's
(its roster, its band, its land waiters): they arrive as an extra object and are merged by the CLI.

Rules the readers hold, each from a measured or ruled failure:

* EVERY required field is emitted for EVERY record, empty or not. :meth:`board_state.Evidence.from_dict` makes a record
  that omits ``items``/``closes``/``commits``/``ahead`` a malformed source (the whole source reads unknown), so an empty
  branch, an empty commit set or an empty list is written out as ``[]``/``0``, never left out.
* ``ahead`` counts the branch's OWN commits (not on origin/main, not on any live ``queue/*`` ref) whose WORK is on
  neither (the matching rule is the bullet on one-to-one matches below). Never the tip sha, which a cherry-pick or a
  re-chain changes.
* A source git cannot read is named in ``unreadable`` with the reason and left out; it is never an empty list. A source
  that depends on another (branches on chain and main) fails with it.
* Commits are attributed by git's own trailer parser (``Board-Item``, ``Board-Closes``) plus ``refs/notes/board``; a
  ``Board-Item: none - <why>`` is an explicit non-item. A branch is attributed by the fixed ``i<n>`` token of
  ``seat/<stamp>-<N>-i<n>-<slug>`` (the id is ``<repo>#<n>``) and by the trailers on its own commits.
* Nothing before :data:`BOARD_EPOCH` can carry a trailer, so the ``unattributed`` scan and the branch candidates start
  there (``--since`` moves it).
* Every ref is resolved FULLY QUALIFIED (``refs/remotes/origin/main``, never ``origin/main``): a short name resolves
  through git's dwim order, where a tag or a local branch of that name would silently swap the evidence. And ONCE:
  :func:`_snapshot` turns every ref the reader uses into a sha at the start, and every later read (``log``, ``--merged``,
  the negatives) takes those shas, so a concurrent ``git fetch`` cannot move a ref between two reads.
* A commit's work is "landed" only by a ONE-TO-ONE match (each main commit accounts for at most one branch commit) with
  a main commit AFTER the branch's merge-base with main (``git cherry``'s upstream side) that no later main commit
  reverted. A patch-id match is the proof; the trailer-ids + subject key is the fallback ONLY for a commit that has no
  patch-id at all (an empty diff), never for a commit whose content is known.
* A repository whose history is cut (a shallow or a partial clone) is unreadable, never read from what is left; the
  whole read has one deadline, and a stage the deadline stops reads unreadable with the stages that depend on it.
* A commit of main whose change a later ``This reverts commit <sha>`` took back (that revert not itself reverted) is no
  record: it closes nothing and names nothing. The ``reader`` summary carries every field on every path, failure included.
"""
from __future__ import annotations

import contextvars
import os
import re
import stat
import subprocess
import time
from collections import Counter, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from levain.desk.board_state import parse_timestamp

__all__ = ["BOARD_EPOCH", "BRANCH_PREFIXES", "GitFailed", "MAX_MAIN_COMMITS", "MAX_OWN_COMMITS", "MIN_PREWINDOW_TAIL", "READ_DEADLINE_S",
           "read_documents", "read_git_evidence", "repo_name_of"]

REMOTE = "origin"
MAIN_BRANCH = "main"
#: The first day a commit could carry a Board-Item trailer (the protocol's first day). Branches whose tip is older and
#: commits older than this are out of the unattributed scan: they cannot have been written under the protocol.
BOARD_EPOCH = "2026-11-12T00:00:00Z"
BRANCH_PREFIXES = ("seat/", "lane/")
CHAIN_PREFIX = "queue/"
GIT_TIMEOUT_S = 120
#: The whole read (about a dozen git calls) must end inside this; a stage it stops reads unreadable.
READ_DEADLINE_S = 300
#: More own commits than this across the candidate branches: the branches source fails closed instead of guessing.
MAX_OWN_COMMITS = 20000
#: The newest main commits read (a COUNT, not a date: a commit written with a wrong clock cannot hide the history behind
#: it). One more is asked for: a log with a commit past the cap is CUT, and a cut log is whole for the window only if at
#: least MIN_PREWINDOW_TAIL of the commits read predate the window (a count of old commits, so one commit with a
#: wrong clock proves nothing). A cut log without that tail reads unreadable, with what depends on it.
MAX_MAIN_COMMITS = 20000
MIN_PREWINDOW_TAIL = 50
_NOTES_REF = "refs/notes/board"
_RS, _US, _GS = "\x1e", "\x1f", "\x1d"
_FMT = ("%x1e%H%x1f%P%x1f%cI%x1f%s%x1f%(trailers:key=Board-Item,valueonly,unfold,separator=%x1d)"
        "%x1f%(trailers:key=Board-Closes,valueonly,unfold,separator=%x1d)%x1f%b%x1f%N")
_NOTE_LINE = re.compile(r"^[ \t]*Board-(Item|Closes):[ \t]*(.*?)[ \t]*$", re.I | re.M)
_REVERTS = re.compile(r"^This reverts commit ([0-9a-f]{40})", re.M)
_NONE = re.compile(r"none\b", re.I)
_TOKEN = re.compile(r"(?:^|-)i([0-9]+)(?=-|$)")
_SHA = re.compile(r"[0-9a-f]{40}")
_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar("board_evidence_deadline", default=None)


class GitFailed(Exception):
    """A git call that could not answer; the message is the reason a source reads unreadable."""


def _env() -> dict[str, str]:
    from levain.dispatch.contract import git_env

    env = dict(git_env())
    env["GIT_OPTIONAL_LOCKS"] = "0"                     # a reader must not take the index lock of a repo someone is committing in
    env["GIT_NO_LAZY_FETCH"] = "1"                      # read-only means read-only: a partial clone fails closed, it never fetches (git >= 2.44)
    env["GIT_ALLOW_PROTOCOL"] = ""                      # ... and no transport at all, on any git version (as levain/dispatch/readset.py does)
    env["GIT_PROTOCOL_FROM_USER"] = "0"
    return env


def _run(repo: Path, *args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    timeout = float(GIT_TIMEOUT_S)
    deadline = _deadline.get()
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            raise GitFailed(f"git {args[0]} not run: the {READ_DEADLINE_S} s read deadline passed")
    try:
        return subprocess.run(["git", "--no-replace-objects", "-C", str(repo), *args], input=stdin if stdin is not None else b"",
                              capture_output=True, timeout=timeout, env=_env())
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitFailed(f"git {args[0]} could not run ({type(exc).__name__})") from None


def _fail_text(args: Sequence[str], cp: subprocess.CompletedProcess[bytes]) -> str:
    lines = (cp.stderr or b"").decode("utf-8", "replace").strip().splitlines()
    return f"git {args[0]} failed: {lines[-1][:200] if lines else cp.returncode}"


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> str:
    cp = _run(repo, *args, stdin=stdin)
    if cp.returncode != 0:
        raise GitFailed(_fail_text(args, cp))
    return cp.stdout.decode("utf-8", "replace")


def _git_yes_no(repo: Path, *args: str) -> bool:
    """A git question whose exit 0 is yes and exit 1 is no (``merge-base --is-ancestor``, ``config --get``); anything
    else is a failure to answer."""
    cp = _run(repo, *args)
    if cp.returncode not in (0, 1):
        raise GitFailed(_fail_text(args, cp))
    return cp.returncode == 0


def repo_name_of(repo: Path) -> str | None:
    """The repo's name for item ids (``<name>#<n>``): the directory that holds its ``.git`` (a linked worktree
    resolves to the main checkout), else the bare repository's name without ``.git``. None when git cannot say."""
    try:
        common = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    except GitFailed:
        return None
    name = common.parent.name if common.name == ".git" else common.name.removesuffix(".git")
    return name or None


# ------------------------------------------------------------------------------------------------ commits
@dataclass(frozen=True)
class _Commit:
    sha: str
    parents: tuple[str, ...]
    date: str | None                                    # UTC, ISO-8601 Z
    ts: float                                           # the same instant (-inf when unreadable)
    subject: str
    items: tuple[str, ...]
    closes: tuple[str, ...]
    none: bool                                          # carries `Board-Item: none - <why>`
    reverts: tuple[str, ...] = ()                       # the commits its message says it reverts (`This reverts commit <sha>`)

    @property
    def merge(self) -> bool:
        return len(self.parents) > 1

    @property
    def attributed(self) -> bool:
        return bool(self.items or self.closes or self.none)

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self.items + self.closes))


def _ids_of(value: str) -> list[str]:
    return [t for t in re.split(r"[,\s]+", value.strip()) if "#" in t.strip("#")]


def _utc(text: str) -> tuple[str | None, float]:
    d = parse_timestamp(text)
    if d is None:
        return None, float("-inf")
    d = d.astimezone(timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ"), d.timestamp()


def _parse_log(out: str) -> dict[str, _Commit]:
    commits: dict[str, _Commit] = {}
    for rec in out.split(_RS):
        if not rec.strip():
            continue
        f = rec.split(_US)
        if len(f) != 8 or not _SHA.fullmatch(f[0].strip()):
            raise GitFailed("git log gave a record this reader cannot parse")
        sha, parents, when, subject, item_vals, close_vals, body, note = f
        items: list[str] = []
        closes: list[str] = []
        none = False
        item_values = [v for v in item_vals.split(_GS) if v.strip()]
        close_values = [v for v in close_vals.split(_GS) if v.strip()]
        for kind, value in _NOTE_LINE.findall(note):
            (item_values if kind.lower() == "item" else close_values).append(value)
        for v in item_values:
            if _NONE.match(v.strip()):
                none = True
            else:
                items.extend(_ids_of(v))
        for v in close_values:
            closes.extend(_ids_of(v))
        date, ts = _utc(when.strip())
        commits[sha] = _Commit(sha, tuple(parents.split()), date, ts, subject.strip(), tuple(dict.fromkeys(items)),
                               tuple(dict.fromkeys(closes)), none, tuple(dict.fromkeys(_REVERTS.findall(body))))
    return commits


def _log(repo: Path, revs: Iterable[str], *, negatives: Iterable[str] = (), limit: int) -> dict[str, _Commit]:
    """The commits reachable from ``revs`` and from none of ``negatives``, newest first, at most ``limit`` of them (a count,
    never a date: ``--since`` stops the walk at a commit dated wrong and hides the history behind it)."""
    stdin = "".join(f"{r}\n" for r in revs) + "".join(f"^{n}\n" for n in negatives)
    return _parse_log(_git(repo, "log", f"--notes={_NOTES_REF}", f"--format={_FMT}", f"-n{limit}", "--stdin", stdin=stdin.encode()))


def _patch_ids(repo: Path, shas: list[str]) -> dict[str, str]:
    """sha -> stable patch-id for the non-merge commits among ``shas`` (an empty diff has none)."""
    if not shas:
        return {}
    diff = _git(repo, "diff-tree", "--root", "-p", "--stdin", stdin="".join(f"{s}\n" for s in shas).encode())
    out = _git(repo, "patch-id", "--stable", stdin=diff.encode())
    pids: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            pids[parts[1]] = parts[0]
    return pids


# ------------------------------------------------------------------------------------------------ fetch time
def _read_stamped(path: Path) -> tuple[str, float] | None:
    """A regular file's text and its mtime, both from ONE open descriptor (a path read twice can name two different
    files if a fetch replaces it in between). None when it is absent, not a regular file or unreadable."""
    try:
        with open(path, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                return None
            return fh.read().decode("utf-8", "replace"), st.st_mtime
    except OSError:
        return None


def _fetch_head_sources(url: str) -> set[str]:
    """The ways git may print a remote's url after ``branch 'main' of `` in FETCH_HEAD: as configured, and with trailing
    slashes, a ``.git`` suffix and a ``user@`` prefix removed (git anonymises and trims the url it writes there)."""
    forms = {url}
    trimmed = url.rstrip("/").removesuffix(".git")
    forms.add(trimmed)
    m = re.match(r"^((?:[A-Za-z][A-Za-z0-9+.-]*://)?)[^/@]*@(.*)$", trimmed)
    if m:
        forms.add(m.group(1) + m.group(2))
    return forms


def _fetched_at(repo: Path, remote: str, main_full: str) -> str | None:
    """When ``remote`` was last fetched, as the newest of these witnesses: the mtime of a FETCH_HEAD (the worktree's own
    and the common dir's: a fetch run inside a linked worktree writes only the first) that holds a line for
    ``branch 'main' of <this remote's url>`` (a fetch of one other branch, or from another remote, says nothing about
    this main), and the last reflog entry of the remote main ref (a floor, so it errs toward stale). None when none can
    be read; an mtime that is not a time is no witness."""
    stamps: list[datetime] = []
    try:
        url = _git(repo, "remote", "get-url", remote).strip()
        dirs = _git(repo, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir").split("\n")
    except GitFailed:
        url, dirs = "", []
    prefix = f"branch '{MAIN_BRANCH}' of "
    sources = _fetch_head_sources(url) if url else set()
    for d in dict.fromkeys(x.strip() for x in dirs if x.strip()):
        got = _read_stamped(Path(d) / "FETCH_HEAD")
        if got is None:
            continue
        text, mtime = got
        carried = any(len(f) == 3 and f[2].startswith(prefix) and f[2][len(prefix):].strip() in sources
                      for f in (line.split("\t", 2) for line in text.splitlines()))
        if not carried:
            continue
        try:
            stamps.append(datetime.fromtimestamp(mtime, timezone.utc))
        except (OverflowError, ValueError, OSError):
            continue
    try:
        entry = _git(repo, "reflog", "show", "-n1", "--date=iso-strict", "--format=%gd", main_full).strip()
        m = re.search(r"@\{(.+)\}$", entry)                 # the reflog ENTRY's time, not the commit's (%cI is the commit's)
        parsed = parse_timestamp(m.group(1)) if m else None
        if parsed is not None:
            stamps.append(parsed)
    except GitFailed:
        pass
    if not stamps:
        return None
    return max(stamps).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------------------------------------ the readers
def _in_effect(commits: Mapping[str, _Commit]) -> set[str]:
    """The commits of ``commits`` whose change is still on main: not taken back by a ``This reverts commit <sha>`` of
    a commit that is itself in effect (so a revert of the revert puts the first one back). Independent of log order (a
    commit dated wrong can sit before its own parent); a revert always follows its target, so the walk ends."""
    taken_by: dict[str, list[str]] = {}
    for c in commits.values():
        for r in c.reverts:
            if r in commits:
                taken_by.setdefault(r, []).append(c.sha)
    state: dict[str, bool] = {}
    for root in commits:
        stack = [root]
        while stack:
            sha = stack[-1]
            if sha in state:
                stack.pop()
                continue
            pending = [r for r in taken_by.get(sha, ()) if r not in state]
            if pending:
                stack.extend(pending)
                continue
            state[sha] = not any(state[r] for r in taken_by.get(sha, ()))
            stack.pop()
    return {sha for sha, live in state.items() if live}


def _main_records(commits: Mapping[str, _Commit]) -> list[dict[str, Any]]:
    """A record for every trailer-bearing commit of main that is still in effect: a closing commit that was reverted
    closes nothing (until the revert is itself reverted)."""
    live = _in_effect(commits)
    return [{"sha": c.sha, "date": c.date, "items": list(c.items), "closes": list(c.closes)}
            for c in commits.values() if (c.items or c.closes) and c.sha in live]


def _token_ids(name: str, repo_name: str | None) -> list[str]:
    if repo_name is None:
        return []
    rest = name.split("/", 1)[1] if "/" in name else name
    m = _TOKEN.search(rest)
    return [f"{repo_name}#{m.group(1).lstrip('0') or '0'}"] if m else []


@dataclass(frozen=True)
class _Pick:
    """The tip(s) one branch name is read from."""
    shas: tuple[str, ...]                               # one; both when the local head and its origin copy have diverged
    tip: str                                            # the one the row reports (the newer by commit time when both are read)
    date: str | None
    diverged: bool


_Snapshot = dict[str, tuple[str, float, str | None]]     # fully qualified ref name -> (sha, commit time, UTC date)


def _snapshot(repo: Path, remote: str) -> _Snapshot:
    """EVERY ref the reader will use, as shas, in one ``for-each-ref``: the remote-tracking refs and the seat/lane local
    heads. Everything after reads this and shas, never a ref by name: a ``git fetch`` that moves a ref between two of the
    reader's reads (the desk and the seats fetch every few minutes) must not leave one read's negatives excluding commits
    that another read never saw."""
    out: _Snapshot = {}
    pats = [f"refs/remotes/{remote}", *(f"refs/heads/{p.rstrip('/')}" for p in BRANCH_PREFIXES)]
    text = _git(repo, "for-each-ref", "--format=%(refname)%1f%(objectname)%1f%(committerdate:iso-strict)", *pats)
    for line in text.splitlines():
        ref, sha, when = (line.split("\x1f") + ["", ""])[:3]
        date, ts = _utc(when)
        if ref and _SHA.fullmatch(sha):
            out[ref] = (sha, ts, date)
    return out


def _tips(snap: _Snapshot, remote: str, prefixes: tuple[str, ...], since_ts: float, *, heads: bool
          ) -> dict[str, list[tuple[str, float, str | None]]]:
    """``{short name: [(tip sha, tip time, tip date), ...]}`` of the remote-tracking branches under ``prefixes`` and, when
    ``heads``, the local heads too: the remote-tracking tip first, then the local one when it differs. A tip older than
    the epoch cannot be on the board and is left out."""
    out: dict[str, list[tuple[str, float, str | None]]] = {}
    bases = [f"refs/remotes/{remote}/"] + (["refs/heads/"] if heads else [])
    for strip in bases:
        for ref, (sha, ts, date) in sorted(snap.items()):
            name = ref[len(strip):]
            if ref.startswith(strip) and any(name.startswith(p) for p in prefixes) and ts >= since_ts:
                seen = out.setdefault(name, [])
                if all(t[0] != sha for t in seen):
                    seen.append((sha, ts, date))
    return out


def _pick(repo: Path, tips: Sequence[tuple[str, float, str | None]]) -> _Pick:
    """One name, up to two tips (origin's copy, then the local head). When they differ, the evidence reads from the one
    that is NOT an ancestor of the other, the newer: a local head left behind never hides what origin holds. When they
    have diverged neither is newer: BOTH are read (the row's ``ahead`` is the union) and the name is reported."""
    if len(tips) == 1:
        sha, _ts, date = tips[0]
        return _Pick((sha,), sha, date, False)
    (r_sha, r_ts, r_date), (l_sha, l_ts, l_date) = tips[0], tips[1]
    if _git_yes_no(repo, "merge-base", "--is-ancestor", l_sha, r_sha):
        return _Pick((r_sha,), r_sha, r_date, False)
    if _git_yes_no(repo, "merge-base", "--is-ancestor", r_sha, l_sha):
        return _Pick((l_sha,), l_sha, l_date, False)
    tip, date = (l_sha, l_date) if l_ts >= r_ts else (r_sha, r_date)
    return _Pick((r_sha, l_sha), tip, date, True)


def _own(log: Mapping[str, _Commit], tip: str) -> dict[str, _Commit]:
    """The commits of ``log`` reachable from ``tip`` through ``log`` (the log excludes what main and the chain hold)."""
    own: dict[str, _Commit] = {}
    todo = deque([tip])
    while todo:
        sha = todo.popleft()
        c = log.get(sha)
        if c is None or sha in own:
            continue
        own[sha] = c
        todo.extend(c.parents)
    return own


def _key(c: _Commit) -> tuple[tuple[str, ...], str] | None:
    return (tuple(sorted(c.ids)), c.subject) if c.ids else None


class _Tally:
    """A pool of landed work that each branch commit may consume ONCE (a main commit accounts for one branch commit,
    never for every later one that shares its subject). A commit with a patch-id matches by patch-id and nothing else;
    only a commit with no patch-id at all (an empty diff) falls back to its trailer ids + subject, against pool
    commits that have none either."""

    def __init__(self, by_pid: Counter[str] | None = None, by_key: Counter[tuple[tuple[str, ...], str]] | None = None) -> None:
        self.by_pid: Counter[str] = by_pid if by_pid is not None else Counter()
        self.by_key: Counter[tuple[tuple[str, ...], str]] = by_key if by_key is not None else Counter()

    @classmethod
    def of(cls, commits: Iterable[_Commit], pids: Mapping[str, str]) -> _Tally:
        t = cls()
        for c in commits:
            pid = pids.get(c.sha)
            if pid is not None:
                t.by_pid[pid] += 1
            elif (k := _key(c)) is not None:
                t.by_key[k] += 1
        return t

    def copy(self) -> _Tally:
        return _Tally(Counter(self.by_pid), Counter(self.by_key))

    def take(self, c: _Commit, pids: Mapping[str, str]) -> str | None:
        """``"pid"`` / ``"key"`` when ``c``'s work is in the pool (and is now spent), else None."""
        pid = pids.get(c.sha)
        if pid is not None:
            if self.by_pid[pid] > 0:
                self.by_pid[pid] -= 1
                return "pid"
            return None
        k = _key(c)
        if k is not None and self.by_key[k] > 0:
            self.by_key[k] -= 1
            return "key"
        return None


class _Matcher:
    """Which main commits a branch (or a queue ref) may be compared with: those AFTER its merge-base with main (the
    commits main holds that the branch's history does not: ``git cherry``'s upstream side), in the window, and not
    reverted by a later main commit. (A reverted landing that is applied again, by a revert of the revert, matches through
    that second commit's own patch-id.)"""

    def __init__(self, main: Mapping[str, _Commit], others: Mapping[str, _Commit], window: float) -> None:
        self.main, self.others, self.window = main, others, window
        reverted = {r for c in main.values() for r in c.reverts if r in main}     # landings a later main commit took back
        self.candidates = [c.sha for c in main.values() if not c.merge and c.ts >= window and c.sha not in reverted]
        self._pools: dict[frozenset[str], tuple[str, ...]] = {}

    def boundary(self, tips: Iterable[str]) -> frozenset[str]:
        """The main commits the non-main history of ``tips`` grew from (a tip that is itself on main is its own)."""
        out: set[str] = set()
        seen: set[str] = set()
        todo = list(tips)
        while todo:
            sha = todo.pop()
            if sha in seen:
                continue
            seen.add(sha)
            if sha in self.main:
                out.add(sha)
                continue
            c = self.others.get(sha)
            if c is not None:
                todo.extend(c.parents)
        return frozenset(out)

    def _reach(self, starts: Iterable[str]) -> set[str]:
        """Main commits reachable from ``starts``. The walk does not go past a commit dated before the window: that can
        only enlarge the pool (it is never a way to read work as landed that is not), and bounds the cost."""
        seen: set[str] = set()
        todo = list(starts)
        while todo:
            sha = todo.pop()
            c = self.main.get(sha)
            if c is None or sha in seen:
                continue
            seen.add(sha)
            if c.ts >= self.window:
                todo.extend(c.parents)
        return seen

    def pool(self, boundary: frozenset[str]) -> tuple[str, ...]:
        got = self._pools.get(boundary)
        if got is None:
            reach = self._reach(boundary)
            got = self._pools[boundary] = tuple(s for s in self.candidates if s not in reach)
        return got


def _read_git_evidence(repo: Path, remote: str, main: str, since: str, repo_name: str | None
                       ) -> tuple[dict[str, Any], dict[str, Any]]:
    main_short = f"{remote}/{main}"
    main_full = f"refs/remotes/{main_short}"            # fully qualified: a tag or a branch named `origin/main` must not win
    unreadable: dict[str, str] = {}
    ev: dict[str, Any] = {"evidence_v": 1, "unreadable": unreadable}
    summary: dict[str, Any] = {"repo": repo_name, "since": since, "main_ref": main_short, "main_commits": 0, "main_records": 0,
                               "chain_refs": 0, "chain_commits": 0, "branches": 0, "refs": 0, "merged": 0, "unattributed": 0,
                               "diverged": [], "matched_by_key": 0}      # whole on every path: a failure board keeps its shape
    every = ("main", "chain", "branches", "refs", "unattributed")

    def fail(sources: Iterable[str], why: str) -> None:
        for s in sources:
            unreadable.setdefault(s, why)

    since_d = parse_timestamp(since)
    if since_d is None:
        fail(every, f"--since {since!r} is not a timestamp")
        return ev, summary
    since_ts = since_d.timestamp()
    window = since_ts - 86400
    if repo_name is None:
        repo_name = repo_name_of(repo)
        summary["repo"] = repo_name
    try:
        shallow = _git(repo, "rev-parse", "--is-shallow-repository").strip()
        if shallow not in ("true", "false"):            # an old git echoes a flag it does not know and exits 0
            raise GitFailed("git cannot say whether this is a shallow clone")
        partial = _git_yes_no(repo, "config", "--get-regexp", r"^(extensions\.partialclone|remote\..*\.promisor)$")
        fetched = _fetched_at(repo, remote, main_full)  # BEFORE the snapshot: a fetch landing after it makes the stamp older, never newer, than the refs
        snap = _snapshot(repo, remote)                  # the one read of the refs; everything below uses these shas
        main_sha = snap.get(main_full, ("", 0.0, None))[0]
        if not main_sha:
            raise GitFailed("no such ref")
        _git(repo, "rev-parse", "--verify", "--quiet", f"{main_sha}^{{commit}}")
    except GitFailed as exc:
        fail(every, f"{main_short} cannot be read ({exc})")
        return ev, summary
    if shallow == "true" or partial:
        fail(every, ("a shallow clone" if shallow == "true" else "a partial clone")
             + ": its history is not whole, so no count of commits can be trusted")
        return ev, summary
    if fetched is not None:
        ev["fetched_at"] = fetched

    refs: list[str] = []
    merged: list[str] = []
    try:                                                                # origin's refs, and which are already on main
        remote_pfx = f"refs/remotes/{remote}/"
        refs = sorted(r[len(remote_pfx):] for r in snap if r.startswith(remote_pfx) and r != remote_pfx + "HEAD")
        on_main = _git(repo, "for-each-ref", "--format=%(refname)%1f%(objectname)", f"--merged={main_sha}", f"refs/remotes/{remote}")
        merged = sorted(r[len(remote_pfx):] for r, sha in (line.split("\x1f") for line in on_main.splitlines() if "\x1f" in line)
                        if r in snap and snap[r][0] == sha and r != remote_pfx + "HEAD")     # a ref that moved since the snapshot is not "merged"
        ev["refs"], ev["merged"] = refs, merged
    except GitFailed as exc:
        fail(("refs", "chain", "branches", "unattributed"), f"origin's refs cannot be listed ({exc})")

    main_commits: dict[str, _Commit] = {}
    try:        # main: the newest MAX_MAIN_COMMITS; one more is asked for, because ONLY a log that has a commit past the cap is cut
        main_commits = _log(repo, [main_sha], limit=MAX_MAIN_COMMITS + 1)
        if len(main_commits) > MAX_MAIN_COMMITS:    # a log cut at the cap: whole FOR THE WINDOW only if a real tail of it is older than the window
            tail = sum(1 for c in main_commits.values() if c.ts < window)    # a count of old commits, never one date: one skewed commit proves nothing
            if tail < MIN_PREWINDOW_TAIL:
                raise GitFailed(f"more than {MAX_MAIN_COMMITS} commits reachable from {main_short}, and fewer than {MIN_PREWINDOW_TAIL} of them predate the window")
        ev["main"] = _main_records(main_commits)
    except GitFailed as exc:
        main_commits = {}
        fail(("main", "chain", "branches", "unattributed"), f"{main_short} history cannot be read ({exc})")

    unattributed: dict[str, dict[str, str]] = {}

    def note_unattributed(c: _Commit) -> None:
        if not c.merge and not c.attributed and c.ts >= since_ts:
            unattributed[c.sha] = {"sha": c.sha, "subject": c.subject}

    chain_rows: list[dict[str, Any]] = []
    branch_rows: list[dict[str, Any]] = []
    extra: dict[str, Any] = {"diverged": [], "matched_by_key": 0}
    if not any(s in unreadable for s in ("main", "refs")):
        try:
            chain_rows, branch_rows, extra = _chain_and_branches(repo, remote, main_sha, snap, set(merged), main_commits,
                                                                 repo_name, since_ts, window, note_unattributed)
            ev["chain"], ev["branches"] = chain_rows, branch_rows
        except GitFailed as exc:
            fail(("chain", "branches", "unattributed"), f"the chain and the seat branches cannot be read ({exc})")
    if not any(s in unreadable for s in ("main", "chain", "refs", "branches")):
        for c in main_commits.values():
            note_unattributed(c)
        ev["unattributed"] = sorted(unattributed.values(), key=lambda u: u["sha"])
    summary.update(main_commits=len(main_commits), main_records=len(ev.get("main", ())), chain_refs=len(chain_rows),
                   chain_commits=sum(len(r["commits"]) for r in chain_rows), branches=len(branch_rows), refs=len(refs),
                   merged=len(merged), unattributed=len(ev.get("unattributed", ())), **extra)
    return ev, summary


def read_git_evidence(repo: Path, *, repo_name: str | None = None, since: str = BOARD_EPOCH,
                      remote: str = REMOTE, main: str = MAIN_BRANCH) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(evidence, summary)``: the git-derived part of the evidence object and counts for a human to check. Never
    raises on git trouble: an unreadable source is named in ``evidence["unreadable"]`` and left out.

    Work older than ``since`` (less a day, for the clock skew between a commit and the cherry-pick of it) is outside
    every judgement below: it cannot carry a trailer, so it is neither attributed nor flagged, and a chain ref or
    branch whose tip is older is not read at all. The whole read has one deadline (:data:`READ_DEADLINE_S`); a shallow
    or a partial clone reads unreadable. ``summary["diverged"]`` names the branches whose local head and origin copy
    have each commits the other lacks (their ``ahead`` is the union: the head decides which is the truth)."""
    token = _deadline.set(time.monotonic() + READ_DEADLINE_S)
    try:
        return _read_git_evidence(Path(repo), remote, main, since, repo_name)
    finally:
        _deadline.reset(token)


def _chain_and_branches(repo: Path, remote: str, main_sha: str, snap: _Snapshot, merged: set[str],
                        main_commits: Mapping[str, _Commit], repo_name: str | None, since_ts: float, window: float,
                        note_unattributed: Any
                        ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """The live chain refs and the attributed seat branches, classified against ONE patch-id pass.

    A queue ref is live when it has a commit of its own whose work main (after the ref's merge-base) lacks (a ref
    re-chained after a cherry-pick keeps its old commits, which main holds by patch-id). A branch's ``ahead`` counts its
    own commits (on neither main nor any chain ref by sha) whose work is on neither by patch-id, one main or chain commit
    for one branch commit."""
    queue = {n: v[0][0] for n, v in _tips(snap, remote, (CHAIN_PREFIX,), since_ts, heads=False).items() if n not in merged}
    picks = {n: _pick(repo, t) for n, t in _tips(snap, remote, BRANCH_PREFIXES, since_ts, heads=True).items()}
    cap = MAX_OWN_COMMITS + 1
    chain_log = _log(repo, sorted(set(queue.values())), negatives=[main_sha], limit=cap) if queue else {}
    branch_log = (_log(repo, sorted({s for p in picks.values() for s in p.shas}), limit=cap,
                       negatives=[main_sha, *sorted(set(queue.values()))]) if picks else {})   # shas only: the snapshot's, not whatever a ref names now
    if len(chain_log) + len(branch_log) > MAX_OWN_COMMITS:
        raise GitFailed(f"more than {MAX_OWN_COMMITS} commits beyond main on the chain and the candidate branches")

    def in_window(c: _Commit) -> bool:
        return not c.merge and c.ts >= window

    matcher = _Matcher(main_commits, {**chain_log, **branch_log}, window)
    chain_own = {n: [c for c in _own(chain_log, tip).values() if in_window(c)] for n, tip in queue.items()}
    chain_bound = {n: matcher.boundary([tip]) for n, tip in queue.items()}
    branch_bound = {n: matcher.boundary(p.shas) for n, p in picks.items()}
    for tips, bound in (*((tuple([queue[n]]), b) for n, b in chain_bound.items()), *((p.shas, branch_bound[n]) for n, p in picks.items())):
        if not bound and not any(s in main_commits for s in tips):          # main's log does not hold the fork point (cut, or mis-dated)
            for s in tips:
                if _git_yes_no(repo, "merge-base", main_sha, s):            # exit 1 = no common ancestor at all: a true empty boundary
                    raise GitFailed("main's history read does not reach where a chain ref or a branch forked from it")
    needed = {s for b in {*chain_bound.values(), *branch_bound.values()} for s in matcher.pool(b)}
    work = [c for c in (*chain_log.values(), *branch_log.values()) if in_window(c)]
    pids = _patch_ids(repo, sorted(needed | {c.sha for c in work}))

    tallies: dict[frozenset[str], _Tally] = {}

    def main_tally(boundary: frozenset[str]) -> _Tally:             # a fresh copy: a branch spends what it takes
        t = tallies.get(boundary)
        if t is None:
            t = tallies[boundary] = _Tally.of((main_commits[s] for s in matcher.pool(boundary)), pids)
        return t.copy()

    keyed = 0
    chain_rows: list[dict[str, Any]] = []
    chain_live: dict[str, _Commit] = {}
    for name in sorted(queue):
        own = chain_own[name]
        pool = main_tally(chain_bound[name])
        live_shas: set[str] = set()
        for c in sorted(own, key=lambda c: (c.ts, c.sha)):
            how = pool.take(c, pids)
            if how is None:
                live_shas.add(c.sha)
            elif how == "key":
                keyed += 1
        live = [c for c in own if c.sha in live_shas]
        if live:
            chain_rows.append({"ref": name, "tip": queue[name], "commits": [{"sha": c.sha, "items": list(c.ids)} for c in live]})
            chain_live.update({c.sha: c for c in live})
    chain_tally = _Tally.of(chain_live.values(), pids)
    for c in chain_live.values():
        note_unattributed(c)

    branch_rows: list[dict[str, Any]] = []
    diverged: list[str] = []
    for name in sorted(picks):
        pick = picks[name]
        if pick.diverged:
            diverged.append(name)
        mine = {c.sha: c for s in pick.shas for c in _own(branch_log, s).values() if in_window(c)}
        token = _token_ids(name, repo_name)
        ids = sorted(dict.fromkeys([*token, *(i for c in mine.values() for i in c.ids)]))
        main_pool, chain_pool = main_tally(branch_bound[name]), chain_tally.copy()
        ahead: list[_Commit] = []
        for c in sorted(mine.values(), key=lambda c: (c.ts, c.sha)):
            how = main_pool.take(c, pids) or chain_pool.take(c, pids)
            if how is None:
                ahead.append(c)
            elif how == "key":
                keyed += 1
        if ids:
            branch_rows.append({"name": name, "tip": pick.tip, "date": pick.date, "ahead": len(ahead), "items": ids})
        if not token:                                   # a token branch is attributed whole; any other commit needs its own trailer
            for c in ahead:
                note_unattributed(c)
    return chain_rows, branch_rows, {"diverged": diverged, "matched_by_key": keyed}


# ------------------------------------------------------------------------------------------------ documents
def read_documents(items: Mapping[str, Any], repo: Path) -> dict[str, bool]:
    """``{path: bool}`` for the ``done`` path of every ``document``/``run`` item: True when it exists and is not empty
    (a non-empty file or directory). A relative path is read from ``repo``; ``~`` is expanded. A path that cannot be
    examined for any reason but being absent (a permission) is left out, so the item reads unknown, not not-done."""
    out: dict[str, bool] = {}
    for item in items.values():
        if getattr(item, "done_kind", None) not in ("document", "run") or not getattr(item, "done", None):
            continue
        key = item.done.strip()
        path = Path(os.path.expanduser(key))
        if not path.is_absolute():
            path = Path(repo) / path
        try:
            if path.is_dir():
                out[key] = any(True for _ in path.iterdir())
            elif path.is_file():
                out[key] = path.stat().st_size > 0
            else:
                out[key] = False
        except OSError:
            continue
    return out
