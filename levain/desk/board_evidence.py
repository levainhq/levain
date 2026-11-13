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
  neither: a commit whose patch-id matches a commit on main or in the chain is landed work, then a commit with the same
  ``Board-Item`` trailer ids and subject. Never the tip sha, which a cherry-pick or a re-chain changes.
* A source git cannot read is named in ``unreadable`` with the reason and left out; it is never an empty list. A source
  that depends on another (branches on chain and main) fails with it.
* Commits are attributed by git's own trailer parser (``Board-Item``, ``Board-Closes``) plus ``refs/notes/board``; a
  ``Board-Item: none - <why>`` is an explicit non-item. A branch is attributed by the fixed ``i<n>`` token of
  ``seat/<stamp>-<N>-i<n>-<slug>`` (the id is ``<repo>#<n>``) and by the trailers on its own commits.
* Nothing before :data:`BOARD_EPOCH` can carry a trailer, so the ``unattributed`` scan and the branch candidates start
  there (``--since`` moves it).
"""
from __future__ import annotations

import os
import re
import subprocess
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from levain.desk.board_state import parse_timestamp

__all__ = ["BOARD_EPOCH", "BRANCH_PREFIXES", "GitFailed", "MAX_OWN_COMMITS", "read_documents", "read_git_evidence",
           "repo_name_of"]

REMOTE = "origin"
MAIN_BRANCH = "main"
#: The first day a commit could carry a Board-Item trailer (the protocol's first day). Branches whose tip is older and
#: commits older than this are out of the unattributed scan: they cannot have been written under the protocol.
BOARD_EPOCH = "2026-11-12T00:00:00Z"
BRANCH_PREFIXES = ("seat/", "lane/")
CHAIN_PREFIX = "queue/"
GIT_TIMEOUT_S = 120
#: More own commits than this across the candidate branches: the branches source fails closed instead of guessing.
MAX_OWN_COMMITS = 20000
_NOTES_REF = "refs/notes/board"
_RS, _US, _GS = "\x1e", "\x1f", "\x1d"
_FMT = ("%x1e%H%x1f%P%x1f%cI%x1f%s%x1f%(trailers:key=Board-Item,valueonly,unfold,separator=%x1d)"
        "%x1f%(trailers:key=Board-Closes,valueonly,unfold,separator=%x1d)%x1f%N")
_NOTE_LINE = re.compile(r"^[ \t]*Board-(Item|Closes):[ \t]*(.*?)[ \t]*$", re.I | re.M)
_NONE = re.compile(r"none\b", re.I)
_TOKEN = re.compile(r"(?:^|-)i([0-9]+)(?=-|$)")
_SHA = re.compile(r"[0-9a-f]{40}")


class GitFailed(Exception):
    """A git call that could not answer; the message is the reason a source reads unreadable."""


def _env() -> dict[str, str]:
    from levain.dispatch.contract import git_env

    env = dict(git_env())
    env["GIT_OPTIONAL_LOCKS"] = "0"                     # a reader must not take the index lock of a repo someone is committing in
    return env


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> str:
    try:
        cp = subprocess.run(["git", "--no-replace-objects", "-C", str(repo), *args], input=stdin if stdin is not None else b"",
                            capture_output=True, timeout=GIT_TIMEOUT_S, env=_env())
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitFailed(f"git {args[0]} could not run ({type(exc).__name__})") from None
    if cp.returncode != 0:
        lines = (cp.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise GitFailed(f"git {args[0]} failed: {lines[-1][:200] if lines else cp.returncode}")
    return cp.stdout.decode("utf-8", "replace")


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
        if len(f) != 7 or not _SHA.fullmatch(f[0].strip()):
            raise GitFailed("git log gave a record this reader cannot parse")
        sha, parents, when, subject, item_vals, close_vals, note = f
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
                               tuple(dict.fromkeys(closes)), none)
    return commits


def _log(repo: Path, revs: Iterable[str], *, negatives: Iterable[str] = (), since: str | None = None) -> dict[str, _Commit]:
    stdin = "".join(f"{r}\n" for r in revs) + "".join(f"^{n}\n" for n in negatives)
    window = [f"--since={since}"] if since else []
    return _parse_log(_git(repo, "log", f"--notes={_NOTES_REF}", f"--format={_FMT}", *window, "--stdin", stdin=stdin.encode()))


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
def _fetched_at(repo: Path, main_ref: str) -> str | None:
    """When origin was last fetched, as the newer of two witnesses: FETCH_HEAD's mtime (only when that fetch carried
    main: a fetch of one other branch writes the same file and says nothing about main) and the last reflog entry of
    the remote main ref (a floor, so it errs toward stale). None when neither can be read."""
    stamps: list[datetime] = []
    try:
        common = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
        fh = common / "FETCH_HEAD"
        text = fh.read_text("utf-8", "replace") if fh.is_file() else ""
        if f"branch '{MAIN_BRANCH}' of" in text:
            stamps.append(datetime.fromtimestamp(fh.stat().st_mtime, timezone.utc))
    except (GitFailed, OSError):
        pass
    try:
        entry = _git(repo, "reflog", "show", "-n1", "--date=iso-strict", "--format=%gd", f"refs/remotes/{main_ref}").strip()
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
def _main_records(commits: Mapping[str, _Commit]) -> list[dict[str, Any]]:
    return [{"sha": c.sha, "date": c.date, "items": list(c.items), "closes": list(c.closes)}
            for c in commits.values() if c.items or c.closes]


def _token_ids(name: str, repo_name: str | None) -> list[str]:
    if repo_name is None:
        return []
    rest = name.split("/", 1)[1] if "/" in name else name
    m = _TOKEN.search(rest)
    return [f"{repo_name}#{m.group(1).lstrip('0') or '0'}"] if m else []


def _tips(repo: Path, remote: str, prefixes: tuple[str, ...], since_ts: float, *, heads: bool
          ) -> dict[str, tuple[str, float, str | None]]:
    """``{short name: (tip sha, tip time, tip date)}`` of the remote-tracking branches under ``prefixes`` (and the local
    heads too when ``heads``, which win on a name both have), newest-epoch filter applied: a tip older than the epoch
    cannot be on the board."""
    out: dict[str, tuple[str, float, str | None]] = {}
    bases = [(f"refs/remotes/{remote}", f"refs/remotes/{remote}/")] + ([("refs/heads", "refs/heads/")] if heads else [])
    for base, strip in bases:                                           # the heads run last, so a local head overwrites
        for pfx in prefixes:
            text = _git(repo, "for-each-ref", "--format=%(refname)%1f%(objectname)%1f%(committerdate:iso-strict)",
                        f"{base}/{pfx.rstrip('/')}")
            for line in text.splitlines():
                ref, sha, when = (line.split("\x1f") + ["", ""])[:3]
                name = ref[len(strip):]
                date, ts = _utc(when)
                if _SHA.fullmatch(sha) and name.startswith(pfx) and ts >= since_ts:
                    out[name] = (sha, ts, date)
    return out


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


def read_git_evidence(repo: Path, *, repo_name: str | None = None, since: str = BOARD_EPOCH,
                      remote: str = REMOTE, main: str = MAIN_BRANCH) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(evidence, summary)``: the git-derived part of the evidence object and counts for a human to check. Never
    raises on git trouble: an unreadable source is named in ``evidence["unreadable"]`` and left out.

    Work older than ``since`` (less a day, for the clock skew between a commit and the cherry-pick of it) is outside
    every judgement below: it cannot carry a trailer, so it is neither attributed nor flagged, and a chain ref or
    branch whose tip is older is not read at all."""
    repo = Path(repo)
    main_ref = f"{remote}/{main}"
    unreadable: dict[str, str] = {}
    ev: dict[str, Any] = {"evidence_v": 1, "unreadable": unreadable}
    summary: dict[str, Any] = {"repo": repo_name, "since": since, "main_ref": main_ref}
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
        _git(repo, "rev-parse", "--verify", "--quiet", f"{main_ref}^{{commit}}")
    except GitFailed as exc:
        fail(every, f"{main_ref} cannot be read ({exc})")
        return ev, summary
    fetched = _fetched_at(repo, main_ref)
    if fetched is not None:
        ev["fetched_at"] = fetched

    refs: list[str] = []
    merged: list[str] = []
    try:                                                                # origin's refs, and which are already on main
        fmt = "--format=%(refname:strip=3)"
        base = f"refs/remotes/{remote}"
        refs = sorted(r for r in _git(repo, "for-each-ref", fmt, base).split("\n") if r and r != "HEAD")
        merged = sorted(r for r in _git(repo, "for-each-ref", fmt, f"--merged={main_ref}", base).split("\n")
                        if r and r != "HEAD")
        ev["refs"], ev["merged"] = refs, merged
    except GitFailed as exc:
        fail(("refs", "chain", "branches", "unattributed"), f"origin's refs cannot be listed ({exc})")

    main_commits: dict[str, _Commit] = {}
    try:                                                                # main: every commit that carries a trailer or a note
        main_commits = _log(repo, [main_ref], since=datetime.fromtimestamp(window, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        ev["main"] = _main_records(main_commits)
    except GitFailed as exc:
        fail(("main", "chain", "branches", "unattributed"), f"{main_ref} history cannot be read ({exc})")

    unattributed: dict[str, dict[str, str]] = {}

    def note_unattributed(c: _Commit) -> None:
        if not c.merge and not c.attributed and c.ts >= since_ts:
            unattributed[c.sha] = {"sha": c.sha, "subject": c.subject}

    chain_rows: list[dict[str, Any]] = []
    branch_rows: list[dict[str, Any]] = []
    if not any(s in unreadable for s in ("main", "refs")):
        try:
            chain_rows, branch_rows = _chain_and_branches(repo, remote, main_ref, set(merged), main_commits, repo_name,
                                                          since_ts, window, note_unattributed)
            ev["chain"], ev["branches"] = chain_rows, branch_rows
        except GitFailed as exc:
            fail(("chain", "branches", "unattributed"), f"the chain and the seat branches cannot be read ({exc})")
    if not any(s in unreadable for s in ("main", "chain", "refs", "branches")):
        for c in main_commits.values():
            note_unattributed(c)
        ev["unattributed"] = sorted(unattributed.values(), key=lambda u: u["sha"])
    summary.update(main_commits=len(main_commits), main_records=len(ev.get("main", ())), chain_refs=len(chain_rows),
                   chain_commits=sum(len(r["commits"]) for r in chain_rows), branches=len(branch_rows), refs=len(refs),
                   merged=len(merged), unattributed=len(ev.get("unattributed", ())))
    return ev, summary


def _chain_and_branches(repo: Path, remote: str, main_ref: str, merged: set[str], main_commits: Mapping[str, _Commit],
                        repo_name: str | None, since_ts: float, window: float, note_unattributed: Any
                        ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The live chain refs and the attributed seat branches, classified against ONE patch-id pass.

    A queue ref is live when it has a commit of its own whose work is not already on main (a ref re-chained after a
    cherry-pick keeps its old commits, which main holds by patch-id). A branch's ``ahead`` counts its own commits (on
    neither main nor any chain ref by sha) whose work is on neither by patch-id, then by trailer ids and subject."""
    queue = {n: v for n, v in _tips(repo, remote, (CHAIN_PREFIX,), since_ts, heads=False).items() if n not in merged}
    cands = _tips(repo, remote, BRANCH_PREFIXES, since_ts, heads=True)
    chain_log = _log(repo, sorted({v[0] for v in queue.values()}), negatives=[main_ref]) if queue else {}
    branch_log = (_log(repo, sorted({v[0] for v in cands.values()}), negatives=[main_ref, *(f"{remote}/{n}" for n in queue)])
                  if cands else {})
    if len(chain_log) + len(branch_log) > MAX_OWN_COMMITS:
        raise GitFailed(f"more than {MAX_OWN_COMMITS} commits beyond main on the chain and the candidate branches")

    def in_window(c: _Commit) -> bool:
        return not c.merge and c.ts >= window

    ref_main = [c for c in main_commits.values() if in_window(c)]
    work = [c for c in (*chain_log.values(), *branch_log.values()) if in_window(c)]
    pids = _patch_ids(repo, sorted({c.sha for c in ref_main} | {c.sha for c in work}))

    def key(c: _Commit) -> tuple[tuple[str, ...], str] | None:
        return (tuple(sorted(c.ids)), c.subject) if c.ids else None

    main_pids = {pids[c.sha] for c in ref_main if c.sha in pids}
    main_keys = {k for c in ref_main if (k := key(c))}

    def on_main(c: _Commit) -> bool:
        return (c.sha in pids and pids[c.sha] in main_pids) or (key(c) in main_keys if key(c) else False)

    chain_rows: list[dict[str, Any]] = []
    chain_live: dict[str, _Commit] = {}
    for name in sorted(queue):
        tip = queue[name][0]
        live = [c for c in _own(chain_log, tip).values() if in_window(c) and not on_main(c)]
        if live:
            chain_rows.append({"ref": name, "tip": tip, "commits": [{"sha": c.sha, "items": list(c.ids)} for c in live]})
            chain_live.update({c.sha: c for c in live})
    chain_pids = {pids[c.sha] for c in chain_live.values() if c.sha in pids}
    chain_keys = {k for c in chain_live.values() if (k := key(c))}
    for c in chain_live.values():
        note_unattributed(c)

    def landed(c: _Commit) -> bool:
        return on_main(c) or (c.sha in pids and pids[c.sha] in chain_pids) or (key(c) in chain_keys if key(c) else False)

    branch_rows: list[dict[str, Any]] = []
    for name in sorted(cands):
        tip, _ts, date = cands[name]
        own = [c for c in _own(branch_log, tip).values() if in_window(c)]
        token = _token_ids(name, repo_name)
        ids = sorted(dict.fromkeys([*token, *(i for c in own for i in c.ids)]))
        ahead = [c for c in own if not landed(c)]
        if ids:
            branch_rows.append({"name": name, "tip": tip, "date": date, "ahead": len(ahead), "items": ids})
        if not token:                                   # a token branch is attributed whole; any other commit needs its own trailer
            for c in ahead:
                note_unattributed(c)
    return chain_rows, branch_rows

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
