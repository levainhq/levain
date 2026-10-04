"""The git transport: an orphan ``levain-ledger`` branch in the project's own repository.

git is the WIRE, not the memory. The branch holds ``team.toml`` (roles), ``ledger/<author>/<device>.jsonl``
(one append-only, hash-chained file per author PER CLONE, so two clones never write one file and a rebase
can never conflict on an entry) and ``PROJECT.md`` (the generated canon, written only by the owner).

Every read and write goes through a private worktree of that branch kept under
``<git common dir>/levain-team/worktree``: inside ``.git``, so the code branch never tracks it, and under the
COMMON dir, so every ``git worktree`` of the project shares one ledger checkout. Writers hold an exclusive
``flock`` on ``levain-team/lock``; readers hold a shared one, so a hook never reads a half-rebased tree.

POSIX only (``fcntl``). The transport is the class below; a self-hosted server is a second implementation of
the same five operations (append, sync, fetch_if_due, ledger, write_canon), not a change to the callers.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import random
import re
import secrets
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import entry as E
from . import index as I
from . import roles as R

BRANCH = "levain-ledger"
DIRNAME = "levain-team"
CANON_FILE = "PROJECT.md"
_SCRUB_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
              "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_PREFIX", "GIT_NAMESPACE")
_PUSH_RETRIES = 6
WARNINGS: list[str] = []  # process-wide: things a person must hear that did not stop the operation


class TeamError(RuntimeError):
    """A team operation could not complete. The message is meant for a person to read."""


class TeamBusy(TeamError):
    """A lock was not acquired in time."""


def git(args: list[str], cwd: Path, *, timeout: float = 60, check: bool = True,
        input_text: str | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB_ENV}
    env.update(GIT_TERMINAL_PROMPT="0", LC_ALL="C", GIT_EDITOR="true")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")  # never prompt on /dev/tty from a hook
    try:
        cp = subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True,
                            timeout=timeout, input=input_text,
                            stdin=None if input_text is not None else subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise TeamError(f"git {args[0]} timed out after {timeout:.0f}s") from None
    except FileNotFoundError:
        raise TeamError("git is not on PATH") from None
    if check and cp.returncode != 0:
        msg = (cp.stderr or cp.stdout).strip().splitlines()
        raise TeamError(f"git {' '.join(args[:2])} failed: {msg[-1] if msg else 'exit ' + str(cp.returncode)}")
    return cp


def _tail(cp: subprocess.CompletedProcess) -> str:
    lines = (cp.stderr or cp.stdout or "").strip().splitlines()
    return lines[-1] if lines else f"exit {cp.returncode}"


def _existing_dir(start: Path) -> Path:
    p = Path(os.path.abspath(start))
    while not p.is_dir():
        if p.parent == p:
            break
        p = p.parent
    return p


@dataclass
class Repo:
    toplevel: Path
    common: Path

    @classmethod
    def discover(cls, start: Path) -> "Repo | None":
        """The repository containing ``start`` (a file or a directory, existing or not), or None."""
        d = _existing_dir(start)
        try:
            cp = git(["rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir"], d,
                     timeout=10, check=False)
        except TeamError:
            return None
        if cp.returncode != 0:
            return None
        lines = cp.stdout.strip().splitlines()
        if len(lines) != 2:
            return None
        return cls(Path(lines[0]), Path(lines[1]))

    @property
    def base(self) -> Path:
        return self.common / DIRNAME

    @property
    def worktree(self) -> Path:
        return self.base / "worktree"


class GitLedger:
    def __init__(self, repo: Repo):
        self.warnings = WARNINGS
        self.repo = repo
        self.base = repo.base
        self.wt = repo.worktree

    # ---- local state -----------------------------------------------------------------------------------

    @property
    def state_path(self) -> Path:
        return self.base / "state.json"

    def state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_state(self, **changes) -> None:
        data = self.state()
        data.update(changes)
        self.base.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.state_path)

    @property
    def device(self) -> str:
        return str(self.state().get("device", ""))

    @property
    def remote(self) -> str | None:
        r = self.state().get("remote")
        return r if isinstance(r, str) and r else None

    def joined(self) -> bool:
        return (self.wt / ".git").exists() and bool(self.device) and (self.wt / "team.toml").is_file()

    def require_joined(self) -> None:
        if not self.joined():
            raise TeamError(f"this clone has not joined a team ledger ({self.repo.toplevel}): "
                            "run `levain team init` (first engineer) or `levain team join`")
        self._repair_if_moved()

    def _repair_if_moved(self) -> None:
        """A moved or renamed clone leaves the worktree's absolute gitdir link dangling; git can relink it."""
        if git(["rev-parse", "--git-dir"], self.wt, check=False, timeout=10).returncode == 0:
            return
        git(["worktree", "repair", str(self.wt)], self.repo.toplevel, check=False, timeout=30)
        if git(["rev-parse", "--git-dir"], self.wt, check=False, timeout=10).returncode != 0:
            raise TeamError(f"the ledger worktree {self.wt} is detached from this repository and "
                            "`git worktree repair` could not relink it")

    # ---- locking -----------------------------------------------------------------------------------------

    @contextlib.contextmanager
    def lock(self, *, exclusive: bool = True, timeout: float = 30.0, name: str = "lock"):
        """flock on levain-team/<name>. ``lock`` guards the worktree; ``net`` serialises fetch/push.

        Raises TeamBusy if not acquired within timeout.
        """
        self.base.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.base / name, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            mode = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, mode)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TeamBusy("ledger busy (another levain team operation holds the lock)") from None
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)  # closing the descriptor releases the flock

    # ---- identity ------------------------------------------------------------------------------------------

    def email(self) -> str:
        cp = git(["config", "user.email"], self.repo.toplevel, check=False, timeout=10)
        return cp.stdout.strip()

    def team(self) -> R.Team:
        return R.load_team(self.wt / "team.toml")

    def handle(self, team: R.Team | None = None) -> str | None:
        return (team or self.team()).handle_for_email(self.email())

    def ledger(self, *, check_committers: bool = True) -> I.Ledger:
        """The ledger as enforced. Entries in a file whose git committers are not the member it is filed under
        (the owner, for pack files) are left out and reported: entry authorship is asserted text, the commit's
        author email is what the repository host recorded. Cached per ledger tree, so a hook pays one
        ``git log`` per change, not per edit."""
        try:
            team = self.team()
        except R.RolesError:
            team = None  # no owner known: only same-author links are honoured, no committer check
        led = I.load_dir(self.wt / "ledger", team.owner if team else None)
        if team is None or not check_committers:
            return led
        bad = self._committer_problems(team, led)
        if not bad:
            return led
        drop = {e["id"] for f in led.files if f.rel in bad for e in f.entries}
        kept = [e for e in led.entries if e["id"] not in drop]
        files = [f for f in led.files if f.rel not in bad]
        return I.Ledger(kept, led.file_problems + [m for msgs in bad.values() for m in msgs], files, led.owner)

    def _committer_problems(self, team: R.Team, led: I.Ledger) -> dict[str, list[str]]:
        tree = self.ledger_tree()
        cache = self.base / "committers.json"
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if cached.get("tree") == tree and cached.get("team") == R.dump_team(team):
                return {k: list(v) for k, v in cached.get("bad", {}).items()}
        except (OSError, ValueError, AttributeError):
            pass
        by_safe = {E.safe_handle(h): h for h in team.members}
        bad: dict[str, list[str]] = {}
        for f in led.files:
            top = f.rel.split("/", 1)[0]
            expected = team.owner if top.startswith("pack-") else by_safe.get(top)
            cp = git(["log", "--format=%ae", "--", f"ledger/{f.rel}"], self.wt, check=False, timeout=30)
            for mail in sorted(set(cp.stdout.split())):
                who = team.handle_for_email(mail)
                if expected is None or who != expected:
                    bad.setdefault(f.rel, []).append(
                        f"ledger/{f.rel}: committed by {mail} ({who or 'not a member'}), but it holds {top}'s "
                        f"entries (expected {expected or 'a member'}); not enforced")
        try:
            tmp = cache.with_suffix(f".tmp{os.getpid()}")
            tmp.write_text(json.dumps({"tree": tree, "team": R.dump_team(team), "bad": bad}), encoding="utf-8")
            os.replace(tmp, cache)
        except OSError:
            pass
        return bad

    def file_for(self, author: str) -> Path:
        return self.wt / "ledger" / E.safe_handle(author) / f"{self.device}.jsonl"

    # ---- setup ---------------------------------------------------------------------------------------------

    def _remote_has_branch(self, remote: str) -> bool:
        cp = git(["ls-remote", "--heads", remote, f"refs/heads/{BRANCH}"], self.repo.toplevel, timeout=60)
        return bool(cp.stdout.strip())

    def _local_branch_exists(self) -> bool:
        cp = git(["rev-parse", "--verify", "-q", f"refs/heads/{BRANCH}"], self.repo.toplevel, check=False)
        return cp.returncode == 0

    def _default_remote(self) -> str | None:
        cp = git(["remote"], self.repo.toplevel, check=False)
        names = cp.stdout.split()
        if "origin" in names:
            return "origin"
        return names[0] if names else None

    def _attach_worktree(self) -> None:
        if (self.wt / ".git").exists():
            return
        self.base.mkdir(parents=True, exist_ok=True)
        git(["worktree", "prune"], self.repo.toplevel, check=False)
        git(["worktree", "add", "--lock", "--reason", "levain team ledger", str(self.wt), BRANCH],
            self.repo.toplevel)

    def _new_device(self) -> str:
        return self.device or secrets.token_hex(4)

    def init(self, team: R.Team, *, remote: str | None = None, push: bool = True) -> str:
        """Create the ledger branch with team.toml, attach the worktree, push. Returns a status line."""
        R.validate_team(team)
        email = self.email()
        if not email:
            raise TeamError("git config user.email is not set in this repository")
        if team.handle_for_email(email) is None:
            raise TeamError(f"your git user.email ({email}) is not a member in the team you are creating")
        remote = remote or self._default_remote()
        if self._local_branch_exists():
            raise TeamError(f"branch {BRANCH} already exists here: use `levain team join`")
        if remote and self._remote_has_branch(remote):
            raise TeamError(f"{remote} already has {BRANCH}: use `levain team join`")
        top = self.repo.toplevel
        blob = git(["hash-object", "-w", "--stdin"], top, input_text=R.dump_team(team)).stdout.strip()
        tree = git(["mktree"], top, input_text=f"100644 blob {blob}\tteam.toml\n").stdout.strip()
        commit = git(["commit-tree", tree, "-m", f"levain team: init ledger for {team.project}"], top).stdout.strip()
        git(["update-ref", f"refs/heads/{BRANCH}", commit, ""], top)
        self.save_state(device=self._new_device(), remote=remote or "")
        self._attach_worktree()
        if remote and push:
            self._sync(push=True)
            return f"ledger created and pushed to {remote}/{BRANCH}"
        return f"ledger created locally ({'no remote' if not remote else 'not pushed'})"

    def join(self, *, remote: str | None = None) -> str:
        email = self.email()
        if not email:
            raise TeamError("git config user.email is not set in this repository")
        remote = remote or self._default_remote()
        if not self._local_branch_exists():
            if not remote:
                raise TeamError("no git remote to join from")
            git(["fetch", "-q", remote, f"+refs/heads/{BRANCH}:refs/remotes/{remote}/{BRANCH}"],
                self.repo.toplevel, timeout=120)
            git(["branch", BRANCH, f"refs/remotes/{remote}/{BRANCH}"], self.repo.toplevel)
        self.save_state(device=self._new_device(), remote=remote or "")
        self._attach_worktree()
        team = self.team()
        handle = team.handle_for_email(email)
        if handle is None:
            raise TeamError(f"joined, but your git user.email ({email}) is not a member of {team.project}: "
                            f"ask the owner ({team.owner}) to run `levain team member add <handle> {email}`")
        if remote:
            self._sync(push=False)
        return f"joined {team.project} as {handle} (device {self.device})"

    # ---- write path ----------------------------------------------------------------------------------------

    def _dirty(self) -> list[str]:
        cp = git(["status", "--porcelain", "-z", "--untracked-files=all"], self.wt)
        out = []
        for rec in cp.stdout.split("\0"):
            if len(rec) > 3:
                out.append(rec[3:])
        return out

    def _recover_dirty(self) -> None:
        """Bring the worktree back to a committed state after an interrupted operation. Caller holds the lock.

        A rebase left in progress is aborted. This device's own entry files are committed (the entry was
        written; only its commit was lost). An uncommitted team.toml or PROJECT.md is discarded: those are
        regenerated by re-running the command that failed. Anything else is refused for a person to inspect.
        """
        for marker in ("rebase-merge", "rebase-apply"):
            p = git(["rev-parse", "--git-path", marker], self.wt).stdout.strip()
            if p and (self.wt / p).exists():
                git(["rebase", "--abort"], self.wt, check=False)
        dirty = self._dirty()
        if not dirty:
            return
        own = re.compile(r"^ledger/[^/]+/" + re.escape(self.device) + r"\.jsonl$")
        regenerable = [p for p in dirty if p in ("team.toml", CANON_FILE)]
        if regenerable:
            git(["checkout", "-q", "HEAD", "--", *regenerable], self.wt, check=False)
            self.warnings.append(f"discarded an uncommitted {', '.join(regenerable)} left by an interrupted "
                                 "command; re-run it if the change is still wanted")
        foreign = [p for p in dirty if not own.match(p) and p not in regenerable]
        if foreign:
            raise TeamError("the ledger worktree has changes no levain command made: "
                            + ", ".join(foreign[:5]) + f" (inspect {self.wt})")
        mine = [p for p in dirty if own.match(p)]
        if mine:
            git(["add", "--", *mine], self.wt)
            self._commit("levain team: recover an interrupted write")

    def _commit(self, message: str) -> None:
        git(["-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", message], self.wt)

    def append(self, entry: dict, *, push: bool = True, lock_timeout: float = 30.0) -> dict:
        """Validate, seal and append one entry to this author's file for this clone; commit; push.

        A refusal (EntryError) writes nothing. A push failure leaves the entry committed locally and
        raises TeamError saying so; the next write or `levain team sync` pushes it.
        """
        self.require_joined()
        with self.lock(timeout=lock_timeout):
            self._recover_dirty()
            ledger = self.ledger()
            E.validate(entry, known=ledger.by_id)
            for s in entry.get("supersedes", []):
                target = ledger.by_id[s]
                if not I.may_link(entry["author"], target.get("author", ""), ledger.owner):
                    raise E.EntryError(
                        f"{s} was recorded by {target.get('author')}; only they or the owner ({ledger.owner}) may "
                        "supersede or retire it. Record your own entry (refs it with --refs) and ask the owner.")
            path = self.file_for(entry["author"])
            rel = path.relative_to(self.wt / "ledger").as_posix()
            prev = next((f.last_hash for f in ledger.files if f.rel == rel), "")
            sealed = E.seal(entry, prev)
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(sealed, ensure_ascii=False, sort_keys=True) + "\n"
            with open(path, "a+b") as fh:
                fh.seek(0, os.SEEK_END)
                if fh.tell() > 0:
                    fh.seek(-1, os.SEEK_END)
                    if fh.read(1) != b"\n":
                        fh.write(b"\n")  # a torn last line stays torn (and reported); it must not swallow this one
                fh.write(line.encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
            git(["add", "--", str(path.relative_to(self.wt))], self.wt)
            self._commit(f"levain team: {sealed['type']} {sealed['id']}")
            if sealed["id"] not in self.ledger().by_id:
                raise TeamError(f"wrote {sealed['id']} but it does not read back as a valid entry; "
                                "run `levain team verify`")
        if push and self.remote:
            try:
                self._sync(push=True)
            except TeamError as exc:
                raise TeamError(f"recorded {sealed['id']} locally, but the push failed ({exc}); "
                                "it goes out with the next write or `levain team sync`") from None
        return sealed

    def sync(self, *, push: bool = True) -> str:
        self.require_joined()
        with self.lock():
            self._recover_dirty()
        return self._sync(push=push)

    def _fetch(self, remote: str, rref: str, timeout: float) -> bool:
        """Fetch the ledger branch. False when the remote has no ledger branch yet."""
        cp = git(["fetch", "-q", remote, f"+refs/heads/{BRANCH}:{rref}"], self.repo.toplevel, timeout=timeout,
                 check=False)
        if cp.returncode == 0:
            now = time.time()
            self.save_state(last_fetch_attempt=now, last_fetch_ok=now, last_fetch_error="")
            return True
        if "couldn't find remote ref" in (cp.stderr or ""):
            return False
        raise TeamError(f"git fetch failed: {_tail(cp)}")

    def _rebase(self, rref: str, timeout: float, lock_timeout: float) -> None:
        with self.lock(timeout=lock_timeout):
            self._recover_dirty()
            cp = git(["rebase", "-q", "--no-verify", rref], self.wt, check=False, timeout=timeout)
            if cp.returncode == 0:
                return
            git(["rebase", "--abort"], self.wt, check=False)
            # Entry files are per device and cannot conflict; team.toml and PROJECT.md can (an owner on two
            # machines). The remote's version wins those files, and the operator is told to redo the change.
            cp = git(["rebase", "-q", "--no-verify", "-X", "ours", rref], self.wt, check=False, timeout=timeout)
            if cp.returncode != 0:
                git(["rebase", "--abort"], self.wt, check=False)
                raise TeamError(f"could not rebase the ledger onto the remote ({_tail(cp)}); nothing was lost locally")
            self.warnings.append("a local team.toml or PROJECT.md change conflicted with the remote; the remote's "
                                 "version was kept. Re-run the change (`levain team member add` / "
                                 "`levain team consolidate`) if it is still wanted.")

    def _sync(self, *, push: bool, timeout: float = 120, net_timeout: float = 150,
              lock_timeout: float = 30.0) -> str:
        """Fetch, rebase, optionally push. Must be called WITHOUT the worktree lock held.

        Network I/O runs under a separate ``net`` lock, so a hook reading the worktree never waits on a
        slow remote: the worktree lock is held only for the local rebase and commits.
        """
        remote = self.remote
        if not remote:
            return "local only (no remote)"
        rref = f"refs/remotes/{remote}/{BRANCH}"
        local = f"refs/heads/{BRANCH}"
        with self.lock(name="net", timeout=net_timeout):
            for attempt in range(_PUSH_RETRIES):
                if not self._fetch(remote, rref, timeout):
                    if not push:
                        return f"{remote} has no {BRANCH} branch yet"
                else:
                    self._rebase(rref, timeout, lock_timeout)
                    if not push:
                        return "fetched"
                    ahead = git(["rev-list", "--count", f"{rref}..{local}"], self.wt).stdout.strip()
                    if ahead == "0":
                        return "up to date"
                cp = git(["push", "-q", "--no-verify", remote, f"{local}:{local}"], self.repo.toplevel,
                         check=False, timeout=timeout)
                if cp.returncode == 0:
                    self._fetch(remote, rref, timeout)
                    return "pushed"
                err = (cp.stderr or "").lower()
                race = any(s in err for s in ("non-fast-forward", "fetch first", "failed to update ref",
                                              "cannot lock ref", "stale info", "but expected", "incorrect old value"))
                if not race:
                    # a policy refusal (protected branch, hook, permissions) will not change on retry
                    lines = [l for l in (cp.stderr or "").splitlines() if "rejected" in l or "error" in l.lower()]
                    raise TeamError(f"push failed: {lines[0].strip() if lines else _tail(cp)}")
                time.sleep(random.uniform(0.05, 0.4) * (attempt + 1))
        raise TeamError(f"push still rejected after {_PUSH_RETRIES} fetch+rebase rounds")

    def fetch_if_due(self, interval: float, *, timeout: float = 8.0) -> str | None:
        """Best-effort fetch+rebase (never push) when the last attempt is older than ``interval`` seconds.

        Returns None when nothing needed doing, someone else is already syncing, or it succeeded; else a
        one-line reason. Never raises.
        """
        try:
            if not self.remote:
                return None
            last = float(self.state().get("last_fetch_attempt") or 0)
            if time.time() - last < interval:
                return None
            self.save_state(last_fetch_attempt=time.time())
            try:
                self._sync(push=False, timeout=timeout, net_timeout=0.5, lock_timeout=3.0)
            except TeamBusy:
                return None  # another process is syncing right now; it brings the same data
            return None
        except TeamError as exc:
            self.save_state(last_fetch_error=str(exc))
            return str(exc)
        except Exception as exc:  # noqa: BLE001 - a hook path: report, never raise
            return f"{type(exc).__name__}: {exc}"

    # ---- canon ---------------------------------------------------------------------------------------------

    def ledger_tree(self) -> str:
        cp = git(["rev-parse", "-q", "--verify", "HEAD:ledger"], self.wt, check=False)
        return cp.stdout.strip() or "empty"

    def read_canon(self) -> str | None:
        try:
            return (self.wt / CANON_FILE).read_text(encoding="utf-8")
        except OSError:
            return None

    def _write_file(self, name: str, text: str, message: str, push: bool) -> str:
        self.require_joined()
        with self.lock():
            self._recover_dirty()
            (self.wt / name).write_text(text, encoding="utf-8")
            git(["add", "--", name], self.wt)
            if not git(["diff", "--cached", "--quiet"], self.wt, check=False).returncode:
                return f"{name} unchanged"
            self._commit(message)
        if push and self.remote:
            return self._sync(push=True)
        return f"{name} written locally"

    def write_canon(self, text: str, *, push: bool = True) -> str:
        return self._write_file(CANON_FILE, text, "levain team: consolidate PROJECT.md", push)

    def write_team(self, team: R.Team, message: str, *, push: bool = True) -> str:
        R.validate_team(team)
        return self._write_file("team.toml", R.dump_team(team), message, push)

    # ---- per-session hook state (local only) -----------------------------------------------------------------

    def _session_path(self, session: str) -> Path:
        return self.base / "sessions" / (re.sub(r"[^A-Za-z0-9._-]", "_", session)[:120] or "_")

    def session_denied(self, session: str) -> set[str]:
        try:
            data = json.loads(self._session_path(session).read_text(encoding="utf-8"))
            return set(data.get("denied", []))
        except (OSError, ValueError, AttributeError):
            return set()

    def mark_denied(self, session: str, ids: set[str]) -> None:
        p = self._session_path(session)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {"denied": sorted(self.session_denied(session) | ids), "ts": E.now_iso()}
        tmp = p.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, p)
