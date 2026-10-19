"""levain.cockpit.activity: the Levain-native activity feed behind the ``activity`` ambient (K1_DESIGN §1-2).

One event list over the last ``ACTIVITY_WINDOW_H`` hours, from what a plain install already has: the session
records its hooks write, commits in the repositories those sessions worked in, the consent queue's outcomes,
the episodes the memory store holds, and the questions agents asked. Each event is flow's sky contract
(``{kind, at, repo, agent, outcome}``) plus the optional ``end`` and ``label``.

The git read is the boundary. A session record's ``cwd`` is one an agent influences, and the repository there is
one an agent writes, ``.git/config`` included, while this read runs unconfined as the operator. So the repository
is found by walking the tree (no git discovery, which would parse its config), must be a plain ``.git`` directory
owned by this uid, and git runs on it with ``--git-dir`` and every program-naming key overridden on the command
line (``NEUTRALISE``). The scan is bounded (``ACTIVITY_REPOS_MAX`` repos, an ``ACTIVITY_GIT_DEADLINE_S`` deadline,
the repositories past it named) and cached for ``ACTIVITY_CACHE_S``, so a polling page never re-runs it per request.

A source that cannot be read is named in ``unreadable`` with its reason in ``errors``; every source unreadable is a
``Fault``, never an empty sky.
"""
from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from levain.cockpit.display_safe import visible
from levain.cockpit.results import Fault, Read, Result

__all__ = ["ACTIVITY_CACHE_S", "ACTIVITY_GIT_DEADLINE_S", "ACTIVITY_REPOS_MAX", "ACTIVITY_WINDOW_H", "LEGEND",
           "NEUTRALISE", "activity_reader", "find_repo_gitdir", "git_commits"]

ACTIVITY_WINDOW_H = 14
ACTIVITY_REPOS_MAX = 12
ACTIVITY_GIT_DEADLINE_S = 6.0
ACTIVITY_GIT_CALL_S = 3.0
ACTIVITY_CACHE_S = 60.0
ACTIVITY_COMMITS_MAX = 500          # per repository, git's own --max-count
ACTIVITY_MEMORY_MAX = 500           # episodes read from the window
LABEL_MAX = 80
SECURE_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"

#: Every program-naming key git could read from the repository's own config (or a file it includes), set on the
#: command line, whose scope wins. A copy of ``levain.firing.ws_git._NEUTRALISE`` plus the keys a ``log`` reaches;
#: a test holds this a superset of that tuple, so a key added there is not silently missing here.
NEUTRALISE = (
    "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=", "-c", "core.pager=cat",
    "-c", "core.editor=false", "-c", "core.sshCommand=/usr/bin/ssh", "-c", "credential.helper=",
    "-c", "core.askPass=", "-c", "core.gitProxy=", "-c", "gpg.program=false",
    "-c", "gpg.ssh.program=false", "-c", "gpg.x509.program=false", "-c", "safe.bareRepository=explicit",
    "-c", "protocol.ext.allow=never", "-c", "core.alternateRefsCommand=",
    "-c", "log.showSignature=false", "-c", "diff.external=", "-c", "core.untrackedCache=false",
)

LEGEND = ("commits per repo; agent sessions as spans (live while a hook fired in the last 15 minutes); "
          "decisions allowed, denied, waiting or failed; memory writes; questions open or answered")

# the consent queue's display states, as the sky's outcomes; a state not listed here is skipped and counted
_DECISION_OUTCOMES = {"allowed": "allowed", "denied": "denied", "pending": "waiting", "undecided": "waiting",
                      "expired": "failed", "withdrawn": "failed"}


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

_git_bin: list[str] = []


def _real_git() -> str:
    """The git binary, resolved once. On macOS ``/usr/bin/git`` is an xcrun shim: the real binary is asked of
    xcrun and accepted only if root owns it and nobody else can write it (ws_git's approach); otherwise git on the
    secure PATH, never the caller's PATH."""
    if _git_bin:
        return _git_bin[0]
    from levain.launch import child_env

    found = None
    if platform.system() == "Darwin":
        try:
            r = subprocess.run(["/usr/bin/xcrun", "--find", "git"], capture_output=True, text=True, cwd="/",
                               env=child_env(), stdin=subprocess.DEVNULL, timeout=ACTIVITY_GIT_CALL_S)
            cand = r.stdout.strip()
            if r.returncode == 0 and cand:
                st = os.stat(cand)
                if st.st_uid == 0 and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    found = cand
        except (OSError, subprocess.SubprocessError):
            pass
    _git_bin.append(found or shutil.which("git", path=SECURE_PATH) or "git")
    return _git_bin[0]


def find_repo_gitdir(cwd: str, home: str | None = None) -> tuple[Path | None, str | None]:
    """The ``.git`` directory of the repository containing ``cwd``, by walking the tree upward (never git's
    discovery, which parses the repository's config to answer): ``(gitdir, None)``, ``(None, None)`` for no
    repository, or ``(None, why)`` for one that is not read. The walk checks ``cwd`` itself and its parents
    below the user's home (home and ``/`` are where it stops, unread unless ``cwd`` is one of them). The first
    ``.git`` found decides: it must be a plain directory owned by this uid (a gitfile is a linked worktree or a
    submodule, which shares its refs elsewhere, and a link points anywhere)."""
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
                    return None, "linked worktree, not read"
                if not stat.S_ISDIR(st.st_mode):
                    return None, "its .git is not a directory, not read"
                if st.st_uid != os.getuid():
                    return None, "not owned by you, not read"
                return cand, None
        first = False
        if cur == stop or cur == cur.parent:
            return None, None
        cur = cur.parent


def git_commits(gitdir: Path, since: datetime, timeout: float) -> list[int]:
    """Commit times (epoch seconds) on the repository's branches, remote branches and tags since ``since``, newest
    first, at most ``ACTIVITY_COMMITS_MAX``. git runs on exactly this git directory (``--git-dir``, cwd ``/``),
    with no pager and every program-naming key overridden (``NEUTRALISE``). Raises ``subprocess.TimeoutExpired``,
    ``OSError`` or ``RuntimeError`` (git refused)."""
    from levain.launch import child_env

    env = child_env()
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1")
    # refs/stash is left out on purpose (``--all`` would walk it): branches, remotes and tags are the work
    argv = [_real_git(), "--no-pager", *NEUTRALISE, f"--git-dir={gitdir}", "log", "--branches", "--remotes", "--tags",
            "--no-show-signature", "--no-ext-diff", "--no-textconv", f"--since=@{int(since.timestamp())}",
            f"--max-count={ACTIVITY_COMMITS_MAX}", "--format=%H%x1f%ct"]
    # its own process group, killed whole at the timeout: anything git started that still holds the output pipe
    # would otherwise keep the read waiting past the deadline
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, cwd="/",
                         env=env, text=True, start_new_session=True)
    try:
        stdout, _err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, 9)
        except OSError:
            pass
        p.communicate()
        raise
    if p.returncode != 0:
        raise RuntimeError(f"git exited {p.returncode}")
    out: list[int] = []
    for ln in stdout.splitlines():
        _h, sep, ct = ln.partition("\x1f")
        if sep and ct.strip().isdigit():
            out.append(int(ct.strip()))
    return out


def _scan(repos: list[tuple[Path, str]], since: datetime, clock: Callable[[], float] = time.monotonic,
          commits: Callable[[Path, datetime, float], list[int]] = git_commits) -> tuple[list[dict[str, Any]], list[str]]:
    """Commit events for ``repos`` (``(gitdir, label)``), inside the whole-scan deadline; each repository not read
    is named in the returned errors."""
    events: list[dict[str, Any]] = []
    errors: list[str] = []
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
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(f"{label}: commits unreadable ({type(exc).__name__})")
            continue
        if len(times) >= ACTIVITY_COMMITS_MAX:
            errors.append(f"{label}: only the newest {ACTIVITY_COMMITS_MAX} commits read")
        events += [{"kind": "commit", "at": _iso(datetime.fromtimestamp(ct, timezone.utc)), "repo": label,
                    "agent": None, "outcome": "ok"} for ct in times]
    return events, errors


class _GitCache:
    """The commit scan, rebuilt at most once per ``ACTIVITY_CACHE_S`` for one set of repositories; the lock makes
    concurrent reads share one rebuild."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._at = 0.0
        self._key: tuple[Any, ...] | None = None
        self._got: tuple[list[dict[str, Any]], list[str]] = ([], [])

    def get(self, repos: list[tuple[Path, str]], since: datetime, fresh: bool,
            scan: Callable[..., tuple[list[dict[str, Any]], list[str]]]) -> tuple[list[dict[str, Any]], list[str]]:
        key = tuple(str(g) for g, _l in repos)
        with self._lock:
            if fresh or self._key != key or time.monotonic() - self._at > ACTIVITY_CACHE_S:
                self._got = scan(repos, since)
                self._key, self._at = key, time.monotonic()
            return list(self._got[0]), list(self._got[1])


# --- the sources ---------------------------------------------------------------------------------------------

def _session_events(install_root: Path | None, now: datetime, start: datetime
                    ) -> tuple[list[dict[str, Any]], list[tuple[datetime, str]]]:
    """Session spans in the window, and the working directories they name (newest activity first). Read with the
    sessions panel's own reader and caps. Raises ``OSError`` for a sessions directory that cannot be read."""
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
    events: list[dict[str, Any]] = []
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
        events.append({"kind": "session", "at": _iso(min(first, seen)), "end": _iso(seen), "repo": _base(rec["cwd"]),
                       "agent": rec["agent"], "outcome": "live" if live else "ok"})
        if os.path.isabs(rec["cwd"]):
            cwds.append((seen, rec["cwd"]))
    cwds.sort(key=lambda p: p[0], reverse=True)
    return events, cwds


def _decision_events(listing: Any, start: datetime) -> tuple[list[dict[str, Any]], int]:
    events: list[dict[str, Any]] = []
    bad = 0
    for e in (*listing.pending, *listing.undecided, *listing.recent):
        try:
            r = e.request
            at = _parse(r.received_at)
            outcome = _DECISION_OUTCOMES.get(e.state)
            if at is None or outcome is None:
                bad += 1
                continue
            if at < start and outcome != "waiting":
                continue
            # the label is what the decisions panel shows token-free (the tool), never the request's body
            events.append({"kind": "decision", "at": _iso(at), "repo": _base(r.cwd) if r.cwd else None,
                           "agent": _label(r.agent), "outcome": outcome, "label": _label(r.tool_name)})
        except Exception:  # noqa: BLE001 - one entry that does not build is counted, never the source
            bad += 1
    return events, bad


def _question_events(store: Any, start: datetime) -> list[dict[str, Any]]:
    qs = {q.n: q for q in store.recent(50)}
    qs.update((q.n, q) for q in store.open_questions())
    events: list[dict[str, Any]] = []
    for q in qs.values():
        at = _parse(q.filed_at)
        if at is None:
            continue
        answered = q.answer is not None
        if at < start and answered:
            continue
        # the decisions panel serves a question's words only to a token holder: the label is its number
        events.append({"kind": "question", "at": _iso(at), "repo": _base(q.cwd) if q.cwd else None,
                       "agent": _label(q.agent), "outcome": "answered" if answered else "open",
                       "label": f"question #{q.n}"})
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
                    scan: Callable[..., tuple[list[dict[str, Any]], list[str]]] = _scan) -> Callable[[Any], Result]:
    """The ``activity`` provider's read: ``read(ctx)`` over the install's session records, the consent queue and
    question store the cockpit holds (looked up at each read, since the server attaches them after the cockpit is
    built), and the episodic store. ``scan`` is the commit scan (a test's seam)."""
    cache = _GitCache()

    def read(ctx: Any) -> Result:
        now = ctx.now
        start = now - timedelta(hours=ACTIVITY_WINDOW_H)
        events: list[dict[str, Any]] = []
        errors: list[str] = []
        unreadable: list[str] = []

        def blind(name: str, why: str) -> None:
            unreadable.append(name)
            errors.append(f"{name}: {why}")

        cwds: list[tuple[datetime, str]] = []
        try:
            got, cwds = _session_events(install_root, now, start)
            events += got
        except OSError as exc:
            blind("sessions", str(exc) if "install root" in str(exc) else f"unreadable ({type(exc).__name__})")
            blind("commits", "no session records, so no repositories to read")
        if "commits" not in unreadable:
            repos: list[tuple[Path, str]] = []
            seen_dirs: set[str] = set()
            for _t, cwd in cwds:
                gitdir, why = find_repo_gitdir(cwd, home)
                if why is not None:
                    errors.append(f"{_base(cwd)}: {why}")
                    continue
                if gitdir is None or str(gitdir) in seen_dirs:
                    continue
                seen_dirs.add(str(gitdir))
                repos.append((gitdir, _base(str(gitdir.parent))))
            if len(repos) > ACTIVITY_REPOS_MAX:
                errors.append(f"{len(repos) - ACTIVITY_REPOS_MAX} repositories past the cap of {ACTIVITY_REPOS_MAX} "
                              "not read (the most recently worked are)")
                repos = repos[:ACTIVITY_REPOS_MAX]
            got, errs = cache.get(repos, start, bool(getattr(ctx, "fresh", False)), scan)
            events += got
            errors += errs
        reader = consent()
        if reader is None:
            blind("decisions", "no consent queue is attached to this cockpit")
        else:
            try:
                got, bad = _decision_events(reader(now), start)
                events += got
                if bad:
                    errors.append(f"decisions: {bad} request(s) that did not read")
            except Exception as exc:  # noqa: BLE001 - an unread queue is named, never shown as no decisions
                blind("decisions", f"unreadable ({type(exc).__name__})")
        store = questions()
        if store is None:
            blind("questions", "no question store is attached to this cockpit")
        else:
            try:
                events += _question_events(store, start)
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
        if len(set(unreadable)) == len(("sessions", "commits", "decisions", "questions", "memory")):
            return Fault("activity is blind: " + "; ".join(errors))
        events.sort(key=lambda e: (_parse(e["at"]) or start, e["kind"]))
        return Read(value={"events": events, "legend": LEGEND,
                           "window": {"hours": ACTIVITY_WINDOW_H, "start": _iso(start), "end": _iso(now)},
                           "errors": errors, "unreadable": unreadable})
    return read
