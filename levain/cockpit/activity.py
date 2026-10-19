"""levain.cockpit.activity: the Levain-native activity feed behind the ``activity`` ambient (K1_DESIGN §1-2).

One event list over the last ``ACTIVITY_WINDOW_H`` hours, from what a plain install already has: the session
records its hooks write, commits in the repositories those sessions worked in, the consent queue's outcomes,
the episodes the memory store holds, and the questions agents asked. Each event is flow's sky contract
(``{kind, at, repo, agent, outcome}``) plus the optional ``end``, ``label`` and ``since`` (a decision waiting or a
question open since before the window keeps its place at the window's start, its true time in ``since``).

The git read is the boundary. A session record's ``cwd`` is one an agent influences, and the repository there is
one an agent writes, ``.git/config`` included, while this read runs unconfined as the operator. So the repository
is found by walking the tree (no git discovery, which would parse its config): a plain ``.git`` directory owned by
this uid, or a linked worktree's gitfile naming one (whose shared repository is read once, under its main
working tree's name). Before git runs, a partial clone, a borrowed object store, a ``commondir`` or a config
include is refused (``repo_refusal``);
git then runs with ``--git-dir``, ``levain.consent.push.git_env`` (no lazy fetch, no transport) and every
program-naming key overridden on the command line (``NEUTRALISE``). The scan is bounded (``ACTIVITY_REPOS_MAX``
repos, an ``ACTIVITY_GIT_DEADLINE_S`` deadline, the repositories past it named) and cached for
``ACTIVITY_CACHE_S``, so a polling page never re-runs it per request.

Known edge: a session record whose ``cwd`` is forged to name the operator's own repository shows that repository's
commit times (never its contents) to whoever can see the page, the operator and cockpit-token holders; the
confined agent does not see the page.

A source that cannot be read is named in ``unreadable`` with its reason in ``errors``; every source unreadable is a
``Fault``, never an empty sky.
"""
from __future__ import annotations

import os
import platform
import stat
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from levain.cockpit.display_safe import visible
from levain.cockpit.results import Fault, Read, Result

__all__ = ["ACTIVITY_CACHE_S", "ACTIVITY_EVENTS_MAX", "ACTIVITY_GIT_DEADLINE_S", "ACTIVITY_REPOS_MAX",
           "ACTIVITY_WINDOW_H", "GitRefused", "LEGEND", "NEUTRALISE", "activity_reader", "find_repo_gitdir",
           "git_commits", "repo_label", "repo_refusal"]

ACTIVITY_WINDOW_H = 14
ACTIVITY_REPOS_MAX = 12
ACTIVITY_GIT_DEADLINE_S = 6.0
ACTIVITY_GIT_CALL_S = 3.0
ACTIVITY_CACHE_S = 60.0
ACTIVITY_COMMITS_MAX = 500          # per repository, git's own --max-count
ACTIVITY_MEMORY_MAX = 500           # episodes read from the window
ACTIVITY_EVENTS_MAX = 2000          # events served, the newest kept
LABEL_MAX = 80
GITFILE_MAX = 4096                  # a gitfile or commondir larger than this is not one git wrote
PACK_ENTRIES_MAX = 10000            # objects/pack entries scanned for a .promisor before the repository is refused

#: Every program-naming key git could read from the repository's own config (or a file it includes), set on the
#: command line, whose scope wins: ``levain.firing.ws_git._NEUTRALISE`` plus ``log.showSignature`` and
#: ``diff.external``. A test holds this a superset of that tuple, so a key added there is not silently missing here.
NEUTRALISE = (
    "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=", "-c", "core.pager=cat",
    "-c", "core.editor=false", "-c", "core.sshCommand=/usr/bin/ssh", "-c", "credential.helper=",
    "-c", "core.askPass=", "-c", "core.gitProxy=", "-c", "gpg.program=false",
    "-c", "gpg.ssh.program=false", "-c", "gpg.x509.program=false", "-c", "safe.bareRepository=explicit",
    "-c", "protocol.ext.allow=never", "-c", "core.alternateRefsCommand=",
    "-c", "log.showSignature=false", "-c", "diff.external=",
)

LEGEND = ("commits per repo; agent sessions as spans (live while a hook fired in the last 15 minutes); "
          "decisions allowed, denied, waiting or failed; memory writes; questions open or answered")

# the consent queue's display states, as the sky's outcomes; a state not listed here (nor in _DECISION_SILENT) is
# skipped and counted. A withdrawn request is the agent's own retraction, not a failure: it draws nothing.
_DECISION_OUTCOMES = {"allowed": "allowed", "denied": "denied", "pending": "waiting", "undecided": "waiting",
                      "expired": "failed"}
_DECISION_SILENT = frozenset({"withdrawn"})


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat()


def _parse(v: Any) -> datetime | None:
    if not isinstance(v, str) or not v or len(v) > 40:
        return None
    try:
        t = datetime.fromisoformat(v[:-1] + "+00:00" if v.endswith("Z") else v)
    except ValueError:
        return None
    return t if t.tzinfo is not None else None


def _label(text: Any) -> str:
    """Agent-controlled text as one display-safe line of at most ``LABEL_MAX`` characters."""
    s = visible(" ".join(str(text).split()))
    return s if len(s) <= LABEL_MAX else s[:LABEL_MAX - 1] + "…"


def _base(path: str) -> str:
    """A working directory's basename, printable, at most 64 characters (the sessions panel's form)."""
    name = os.path.basename(path.rstrip("/\\")) or "/"
    return "".join(c for c in name if c.isprintable())[:64]


# --- the git read --------------------------------------------------------------------------------------------

class GitRefused(RuntimeError):
    """A repository (or a git) this read will not run: the message names why, with no path in it."""


_git_bin: list[str] = []


def _real_git() -> str:
    """The git binary, resolved once. On macOS ``/usr/bin/git`` is an xcrun shim: the real binary is asked of
    xcrun and accepted only if root owns it and nobody else can write it (ws_git's approach); otherwise
    ``levain.consent.push.git_bin`` (a root-owned candidate). Never a ``PATH`` lookup: no trusted git raises
    ``GitRefused``, and nothing is cached, so the next read asks again."""
    if _git_bin:
        return _git_bin[0]
    from levain.consent.push import PushRefused, git_bin
    from levain.launch import child_env

    found = None
    if platform.system() == "Darwin":
        try:
            r = subprocess.run(["/usr/bin/xcrun", "--find", "git"], capture_output=True, text=True, cwd="/",
                               env=child_env(), stdin=subprocess.DEVNULL, timeout=ACTIVITY_GIT_CALL_S)
            cand = r.stdout.strip()
            if r.returncode == 0 and os.path.isabs(cand):
                st = os.stat(cand)
                if st.st_uid == 0 and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    found = cand
        except (OSError, subprocess.SubprocessError):
            pass
    if found is None:
        try:
            found = git_bin()
        except PushRefused:
            raise GitRefused("no root-owned git to run (levain never runs a git found on PATH)") from None
    _git_bin.append(found)
    return _git_bin[0]


def _git_env() -> dict[str, str]:
    """``levain.consent.push.git_env("")``: no ``GIT_*``/``LD_*``/``DYLD_*`` from the caller, the trusted
    ``PATH``, no lazy fetch, no replace objects, no transport at all; plus no optional locks (a read never
    writes the index)."""
    from levain.consent.push import git_env

    env = git_env("")
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _owned_dir(p: Path) -> bool:
    try:
        st = os.lstat(p)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid()


def _one_line(p: Path, limit: int = GITFILE_MAX) -> str | None:
    """A small regular file owned by this uid, opened without following a link, as its one line; else None."""
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size > limit:
            return None
        raw = os.read(fd, limit + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    text = text[:-1] if text.endswith("\n") else text
    return None if not text or any(c in text for c in "\r\n\0") else text


def _linked_common(gitfile: Path) -> tuple[Path | None, str | None]:
    """The common git directory a linked worktree's ``.git`` gitfile names, or why it is not read. Accepted only
    as git itself writes it: one line ``gitdir: <absolute path>`` naming ``<common>/worktrees/<name>``, every
    one of those a real (unlinked) directory owned by this uid, whose ``commondir`` file leads back to
    ``<common>``, which names no other repository through a ``commondir`` of its own."""
    line = _one_line(gitfile)
    if line is None or not line.startswith("gitdir: "):
        return None, "its .git file is not one gitdir line you own, not read"
    target = line[len("gitdir: "):]
    if not os.path.isabs(target) or os.path.normpath(target) != target:
        return None, "its .git file names no absolute worktree path, not read"
    wt = Path(target)
    if wt.parent.name != "worktrees" or not wt.name:
        return None, "its .git file names no <repository>/worktrees/<name>, not read"
    common = wt.parent.parent
    if os.path.realpath(wt) != str(wt):
        return None, "its .git file names a path through a link, not read"
    if not all(_owned_dir(d) for d in (wt, wt.parent, common)):
        return None, "its .git file names a worktree that is not a directory you own, not read"
    back = _one_line(wt / "commondir")
    if back is None or os.path.realpath(os.path.join(wt, back)) != str(common):
        return None, "its worktree's commondir does not lead back to its repository, not read"
    if os.path.lexists(common / "commondir"):
        # git run on <common> would follow this one too, into a store the refusal never looked at
        return None, "its repository names another repository (commondir), not read"
    return common, None


def repo_label(common: Path) -> str:
    """The lane a repository's events share: the main working tree's basename (the common dir's parent), or for
    a bare common dir its own name without ``.git``."""
    if common.name == ".git":
        return _base(str(common.parent))
    name = common.name[:-4] if common.name.endswith(".git") and len(common.name) > 4 else common.name
    return _base(name)


def find_repo_gitdir(cwd: str, home: str | None = None) -> tuple[Path | None, str | None]:
    """The common git directory of the repository containing ``cwd``, by walking the tree upward (never git's
    discovery, which parses the repository's config to answer): ``(gitdir, None)``, ``(None, None)`` for no
    repository, or ``(None, why)`` for one that is not read. The walk checks ``cwd`` itself and its parents
    below the user's home (home and ``/`` are where it stops, unread unless ``cwd`` is one of them). The first
    ``.git`` found decides: a plain directory owned by this uid (and naming no other repository through a
    ``commondir``), or a linked worktree's gitfile as ``_linked_common`` accepts it, whose shared repository is
    the one read. A link is never followed."""
    if not os.path.isabs(cwd):
        return None, "not an absolute path"
    try:
        cur = Path(os.path.realpath(cwd))
        stop = Path(os.path.realpath(home if home is not None else os.path.expanduser("~")))
    except (OSError, ValueError):
        return None, "unresolvable"
    first = True
    while True:
        if first or (cur != stop and cur != cur.parent):
            cand = cur / ".git"
            try:
                st = os.lstat(cand)
            except FileNotFoundError:
                st = None
            except OSError:
                return None, "unreadable"
            if st is not None:
                if stat.S_ISLNK(st.st_mode):
                    return None, "its .git is a link, not read"
                if stat.S_ISREG(st.st_mode):
                    return _linked_common(cand)
                if not stat.S_ISDIR(st.st_mode):
                    return None, "its .git is not a directory, not read"
                if st.st_uid != os.getuid():
                    return None, "not owned by you, not read"
                if os.path.lexists(cand / "commondir"):
                    return None, "its .git names another repository (commondir), not read"
                return cand, None
        first = False
        if cur == stop or cur == cur.parent:
            return None, None
        cur = cur.parent


def _run(argv: list[str], timeout: float) -> tuple[int, str]:
    """git in its own process group, killed whole at the timeout. Something git started that left the group and
    still holds the output pipe would keep a plain ``communicate`` waiting forever, holding the scan's cache lock:
    after the kill the read waits one more second, then closes its ends of the pipes and moves on."""
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, cwd="/",
                         env=_git_env(), text=True, start_new_session=True)
    try:
        stdout, _err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, 9)
        except OSError:
            pass
        try:
            p.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            for f in (p.stdout, p.stderr):
                try:
                    if f is not None:
                        f.close()
                except OSError:
                    pass
            p.poll()
        raise
    return p.returncode, stdout


def repo_refusal(gitdir: Path, timeout: float = ACTIVITY_GIT_CALL_S) -> str | None:
    """Why a repository is not read, from its tree and its config file, before any git runs on it: a
    ``commondir`` (git would read another repository's config and objects), an object store that is not a plain
    directory, one that borrows objects (a non-empty ``objects/info/alternates`` or ``http-alternates``), a
    partial clone (an ``objects/pack/*.promisor`` file, ``extensions.partialclone`` or any
    ``remote.<name>.promisor``, whatever its value, in ``config`` or ``config.worktree``), or a config that
    includes another file (``include.path``, ``includeIf.<cond>.path``): read here with ``git config --file``,
    outside the repository, a ``gitdir:`` or ``onbranch:`` condition is false that the real ``--git-dir`` run
    finds true, so an include is refused unread rather than evaluated in the wrong context. The config is read
    with ``git config --file --no-includes``, which runs nothing. The message names the reason, never the path."""
    if os.path.lexists(gitdir / "commondir"):
        return "it names another repository (commondir), not read"
    objs = gitdir / "objects"
    try:
        ost = os.lstat(objs)
    except OSError:
        return "it has no object store, not read"
    if not stat.S_ISDIR(ost.st_mode):
        return "its object store is not a plain directory, not read"
    for alt in ("alternates", "http-alternates"):
        f = objs / "info" / alt
        try:
            ast = os.lstat(f)
        except FileNotFoundError:
            continue
        except OSError:
            return f"its objects/info/{alt} is unreadable, not read"
        if not stat.S_ISREG(ast.st_mode) or ast.st_size:
            return f"it borrows objects (objects/info/{alt}), not read"
    try:
        with os.scandir(objs / "pack") as it:
            for n, e in enumerate(it):
                if n >= PACK_ENTRIES_MAX:
                    return f"its pack directory holds more than {PACK_ENTRIES_MAX} entries, not read"
                if e.name.endswith(".promisor"):
                    return "a partial clone (a .promisor pack), not read"
    except FileNotFoundError:
        pass
    except OSError:
        return "its pack directory is unreadable, not read"
    for name in ("config", "config.worktree"):
        f = gitdir / name
        if not os.path.lexists(f):
            continue
        code, out = _run([_real_git(), "--no-pager", "config", "--file", str(f), "--no-includes", "-z",
                          "--get-regexp", r"^(extensions\.partialclone|remote\..*\.promisor|include\.path|"
                          r"includeif\..*\.path)$"], timeout)
        if code not in (0, 1):
            return f"its {name} does not parse (git config exited {code}), not read"
        keys = [item.partition("\n")[0].lower() for item in out.split("\0") if item]
        if any(k == "include.path" or (k.startswith("includeif.") and k.endswith(".path")) for k in keys):
            return f"its {name} includes another config file, not read"
        for key in keys:
            if key == "extensions.partialclone" or (key.startswith("remote.") and key.endswith(".promisor")):
                shown = "extensions.partialclone" if key.startswith("extensions.") else "remote.<name>.promisor"
                return f"a partial clone ({shown} in {name}), not read"
    return None


def git_commits(gitdir: Path, since: datetime, timeout: float) -> list[int]:
    """Commit times (epoch seconds) on the repository's branches, remote branches and tags since ``since``, newest
    first, at most ``ACTIVITY_COMMITS_MAX``. ``repo_refusal`` first (``GitRefused``); then git runs on exactly
    this git directory (``--git-dir``, cwd ``/``) under ``_git_env`` (no lazy fetch: a promisor remote's
    upload-pack never runs), with no pager and every program-naming key overridden (``NEUTRALISE``). Raises
    ``subprocess.TimeoutExpired``, ``OSError`` or ``RuntimeError`` (git refused)."""
    deadline = time.monotonic() + timeout
    why = repo_refusal(gitdir, timeout)
    if why is not None:
        raise GitRefused(why)
    left = deadline - time.monotonic()
    if left <= 0:
        raise subprocess.TimeoutExpired("git", timeout)
    # refs/stash is left out on purpose (``--all`` would walk it): branches, remotes and tags are the work
    argv = [_real_git(), "--no-pager", *NEUTRALISE, f"--git-dir={gitdir}", "log", "--branches", "--remotes", "--tags",
            "--no-show-signature", "--no-ext-diff", "--no-textconv", f"--since=@{int(since.timestamp())}",
            f"--max-count={ACTIVITY_COMMITS_MAX}", "--format=%H%x1f%ct"]
    code, stdout = _run(argv, left)
    if code != 0:
        raise RuntimeError(f"git exited {code}")
    out: list[int] = []
    for ln in stdout.splitlines():
        _h, sep, ct = ln.partition("\x1f")
        if sep and ct.strip().isdigit():
            out.append(int(ct.strip()))
    return out


def _scan(repos: list[tuple[Path, str]], since: datetime, clock: Callable[[], float] = time.monotonic,
          commits: Callable[[Path, datetime, float], list[int]] = git_commits
          ) -> tuple[list[dict[str, Any]], list[str], int]:
    """Commit events for ``repos`` (``(gitdir, label)``), inside the whole-scan deadline, the errors (each
    repository not read is named) and how many repositories were read."""
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    n_read = 0
    t0 = clock()
    for gitdir, label in repos:
        left = ACTIVITY_GIT_DEADLINE_S - (clock() - t0)
        if left <= 0:
            errors.append(f"{label}: skipped, the commit scan passed its {ACTIVITY_GIT_DEADLINE_S:g}s deadline")
            continue
        try:
            times = commits(gitdir, since, min(ACTIVITY_GIT_CALL_S, left))
        except subprocess.TimeoutExpired:
            errors.append(f"{label}: the commit read timed out")
            continue
        except GitRefused as exc:
            errors.append(f"{label}: {exc}")
            continue
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(f"{label}: commits unreadable ({type(exc).__name__})")
            continue
        n_read += 1
        if len(times) >= ACTIVITY_COMMITS_MAX:
            errors.append(f"{label}: only the newest {ACTIVITY_COMMITS_MAX} commits read")
        events += [{"kind": "commit", "at": _iso(datetime.fromtimestamp(ct, timezone.utc)), "repo": label,
                    "agent": None, "outcome": "ok"} for ct in times]
    return events, errors, n_read


class _GitCache:
    """The commit scan, rebuilt at most once per ``ACTIVITY_CACHE_S`` for one set of repositories; the lock makes
    concurrent reads share one rebuild. A read gets copies, so nothing it changes reaches the next read, and only
    the events still inside its own window."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._at = 0.0
        self._key: tuple[Any, ...] | None = None
        self._got: tuple[list[dict[str, Any]], list[str], int] = ([], [], 0)

    def get(self, repos: list[tuple[Path, str]], since: datetime, fresh: bool,
            scan: Callable[..., tuple[list[dict[str, Any]], list[str], int]]
            ) -> tuple[list[dict[str, Any]], list[str], int]:
        key = tuple(str(g) for g, _l in repos)
        with self._lock:
            if fresh or self._key != key or time.monotonic() - self._at > ACTIVITY_CACHE_S:
                self._got = scan(repos, since)
                self._key, self._at = key, time.monotonic()
            # a scan is up to ACTIVITY_CACHE_S old: a commit it found inside the window may have left it since
            kept = [dict(e) for e in self._got[0] if (_parse(e.get("at")) or since) >= since]
            return kept, list(self._got[1]), self._got[2]


# --- the sources ---------------------------------------------------------------------------------------------

def _session_events(install_root: Path | None, now: datetime, start: datetime
                    ) -> tuple[list[tuple[dict[str, Any], str]], list[tuple[datetime, str]]]:
    """Session spans in the window, each with its working directory (the caller names its lane), and the working
    directories they name (newest activity first). Read with the sessions panel's own reader and caps. Raises
    ``OSError`` for a sessions directory that cannot be read."""
    from levain.cockpit.providers import SESSION_ACTIVE_S, SESSION_SKEW_S, SESSIONS_SCAN, SESSIONS_SHOWN, _session_record

    if install_root is None:
        raise OSError("no install root: the sessions are kept in the install")
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
        return [], []
    found.sort(key=lambda p: p[0], reverse=True)
    spans: list[tuple[dict[str, Any], str]] = []
    cwds: list[tuple[datetime, str]] = []
    for _mt, path in found[:SESSIONS_SHOWN]:
        rec = _session_record(path)
        if rec is None:
            continue
        seen = rec["_seen"]
        if (seen - now).total_seconds() > SESSION_SKEW_S or seen < start:
            continue
        first = _parse(rec.get("first_seen")) or seen
        live = (now - seen).total_seconds() < SESSION_ACTIVE_S
        spans.append(({"kind": "session", "at": _iso(min(first, seen)), "end": _iso(seen), "repo": None,
                       "agent": rec["agent"], "outcome": "live" if live else "ok"}, rec["cwd"]))
        if os.path.isabs(rec["cwd"]):
            cwds.append((seen, rec["cwd"]))
    cwds.sort(key=lambda p: p[0], reverse=True)
    return spans, cwds


def _clamped(at: datetime, start: datetime) -> dict[str, str]:
    """An item still waiting now that began before the window: placed at the window's start (the sky draws ``at``),
    its true time carried in ``since``."""
    return {"at": _iso(at)} if at >= start else {"at": _iso(start), "since": _iso(at)}


def _decision_events(listing: Any, start: datetime) -> tuple[list[tuple[dict[str, Any], str | None]], int]:
    events: list[tuple[dict[str, Any], str | None]] = []
    bad = 0
    for e in (*listing.pending, *listing.undecided, *listing.recent):
        try:
            if e.state in _DECISION_SILENT:
                continue
            r = e.request
            at = _parse(r.received_at)
            outcome = _DECISION_OUTCOMES.get(e.state)
            if at is None or outcome is None:
                bad += 1
                continue
            if at < start and outcome != "waiting":
                continue
            # the label is what the decisions panel shows token-free (the tool), never the request's body
            events.append(({"kind": "decision", **_clamped(at, start), "repo": None, "agent": _label(r.agent),
                            "outcome": outcome, "label": _label(r.tool_name)}, r.cwd or None))
        except Exception:  # noqa: BLE001 - one entry that does not build is counted, never the source
            bad += 1
    return events, bad


def _question_events(store: Any, start: datetime) -> list[tuple[dict[str, Any], str | None]]:
    qs = {q.n: q for q in store.recent(50)}
    qs.update((q.n, q) for q in store.open_questions())
    events: list[tuple[dict[str, Any], str | None]] = []
    for q in qs.values():
        at = _parse(q.filed_at)
        if at is None:
            continue
        answered = q.answer is not None
        if at < start and answered:
            continue
        # the decisions panel serves a question's words only to a token holder: the label is its number
        events.append(({"kind": "question", **_clamped(at, start), "repo": None, "agent": _label(q.agent),
                        "outcome": "answered" if answered else "open", "label": f"question #{q.n}"}, q.cwd or None))
    return events


def _memory_events(db: Path, start: datetime) -> tuple[list[dict[str, Any]], bool]:
    if not Path(db).is_file():
        raise OSError("no episodic store")
    from anneal_memory import Store

    with Store(str(db), read_only=True) as st:
        got = st.recall(since=_iso(start), limit=ACTIVITY_MEMORY_MAX)
    events = []
    for ep in got.episodes:
        at = _parse(ep.timestamp)
        if at is None or at < start:
            continue
        events.append({"kind": "memory", "at": _iso(at), "repo": None, "agent": _label(ep.source) or None,
                       "outcome": "ok", "label": str(getattr(ep.type, "value", ep.type))})
    return events, got.total_matching > len(got.episodes)


def activity_reader(*, install_root: Path | None, episodic_db: Path,
                    consent: Callable[[], Callable[[datetime], Any] | None],
                    questions: Callable[[], Any], home: str | None = None,
                    scan: Callable[..., tuple[list[dict[str, Any]], list[str], int]] = _scan
                    ) -> Callable[[Any], Result]:
    """The ``activity`` provider's read: ``read(ctx)`` over the install's session records, the consent queue and
    question store the cockpit holds (looked up at each read, since the server attaches them after the cockpit is
    built), and the episodic store. ``scan`` is the commit scan (a test's seam). Sessions, decisions and questions
    take their lane (``repo``) from the repository their working directory is in (``repo_label``), so a linked
    worktree's sessions and its repository's commits share one; the directory's own name when none is found."""
    cache = _GitCache()

    def read(ctx: Any) -> Result:
        now = ctx.now
        start = now - timedelta(hours=ACTIVITY_WINDOW_H)
        events: list[dict[str, Any]] = []
        errors: list[str] = []
        unreadable: list[str] = []
        found: dict[str, tuple[Path | None, str | None]] = {}

        def blind(name: str, why: str) -> None:
            unreadable.append(name)
            errors.append(f"{name}: {why}")

        def repo_of(cwd: str) -> tuple[Path | None, str | None]:
            if cwd not in found:
                found[cwd] = find_repo_gitdir(cwd, home)
            return found[cwd]

        def lane(cwd: str | None) -> str | None:
            if not cwd:
                return None
            gitdir, _why = repo_of(cwd) if os.path.isabs(cwd) else (None, None)
            return repo_label(gitdir) if gitdir is not None else _base(cwd)

        def placed(pairs: list[tuple[dict[str, Any], str | None]]) -> list[dict[str, Any]]:
            return [{**e, "repo": lane(cwd)} for e, cwd in pairs]

        cwds: list[tuple[datetime, str]] = []
        try:
            spans, cwds = _session_events(install_root, now, start)
            events += placed(spans)
        except OSError as exc:
            blind("sessions", str(exc) if "install root" in str(exc) else f"unreadable ({type(exc).__name__})")
            blind("commits", "no session records, so no repositories to read")
        if "commits" not in unreadable:
            repos: list[tuple[Path, str]] = []
            seen_dirs: set[str] = set()
            refused = 0
            for _t, cwd in cwds:
                gitdir, why = repo_of(cwd)
                if why is not None:
                    refused += 1
                    errors.append(f"{_base(cwd)}: {why}")
                    continue
                if gitdir is None or str(gitdir) in seen_dirs:
                    continue
                seen_dirs.add(str(gitdir))
                repos.append((gitdir, repo_label(gitdir)))
            if len(repos) > ACTIVITY_REPOS_MAX:
                errors.append(f"{len(repos) - ACTIVITY_REPOS_MAX} repositories past the cap of {ACTIVITY_REPOS_MAX} "
                              "not read (the most recently worked are)")
                repos = repos[:ACTIVITY_REPOS_MAX]
            got, errs, n_read = cache.get(repos, start, bool(getattr(ctx, "fresh", False)), scan)
            events += got
            errors += errs
            if (repos or refused) and not n_read:
                # every repository the sessions worked in failed: no commits is not a quiet window
                blind("commits", "no repository the sessions worked in could be read")
        reader = consent()
        if reader is None:
            blind("decisions", "no consent queue is attached to this cockpit")
        else:
            try:
                got, bad = _decision_events(reader(now), start)
                events += placed(got)
                if bad:
                    errors.append(f"decisions: {bad} request(s) that did not read")
            except Exception as exc:  # noqa: BLE001 - an unread queue is named, never shown as no decisions
                blind("decisions", f"unreadable ({type(exc).__name__})")
        store = questions()
        if store is None:
            blind("questions", "no question store is attached to this cockpit")
        else:
            try:
                events += placed(_question_events(store, start))
            except Exception as exc:  # noqa: BLE001
                blind("questions", f"unreadable ({type(exc).__name__})")
        try:
            got, more = _memory_events(episodic_db, start)
            events += got
            if more:
                errors.append(f"memory: only the newest {ACTIVITY_MEMORY_MAX} episodes in the window read")
        except Exception as exc:  # noqa: BLE001 - any store fault names the source
            blind("memory", str(exc) if isinstance(exc, OSError) and str(exc) == "no episodic store"
                  else f"unreadable ({type(exc).__name__})")
        errors = list(dict.fromkeys(errors))
        if len(set(unreadable)) == len(("sessions", "commits", "decisions", "questions", "memory")):
            return Fault("activity is blind: " + "; ".join(errors))
        events.sort(key=lambda e: (_parse(e["at"]) or start, e["kind"]))
        if len(events) > ACTIVITY_EVENTS_MAX:
            # an item still waiting sits at the window's start, the oldest slot: the cut takes the newest of the
            # rest around it, never it (unless the waiting items alone pass the cap, the newest of them kept)
            waiting = [e for e in events if "since" in e][-ACTIVITY_EVENTS_MAX:]
            rest = [e for e in events if "since" not in e]
            rest = rest[len(rest) - (ACTIVITY_EVENTS_MAX - len(waiting)):] if len(waiting) < ACTIVITY_EVENTS_MAX else []
            errors.append(f"{len(events) - len(waiting) - len(rest)} older events past the cap of "
                          f"{ACTIVITY_EVENTS_MAX} not shown (the newest, and every item still waiting, are)")
            events = waiting + rest
            events.sort(key=lambda e: (_parse(e["at"]) or start, e["kind"]))
        # the window is its length only: a clock-stamped start/end would change the value on every read, so an
        # idle install would stream panel-changed for ever
        return Read(value={"events": events, "legend": LEGEND, "window": {"hours": ACTIVITY_WINDOW_H},
                           "errors": errors, "unreadable": unreadable})
    return read
