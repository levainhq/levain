"""The git transport: an orphan ``levain-team-ledger`` branch in the project's own repository.

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
import hashlib
import fcntl
import json
import os
import random
import re
import secrets
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import entry as E
from . import index as I
from . import roles as R

BRANCH = "levain-team-ledger"          # a strict (tenure) ledger; a re-genesis lives on BRANCH-<genesis id>
REF = f"refs/heads/{BRANCH}"
LEGACY_BRANCH = "levain-ledger"        # levain 0.6.x: never read by this levain (B-1), only recognised to refuse it
LEGACY_MESSAGE = ("this team ledger predates strict mode (levain 0.6.x, branch levain-ledger); levain v2 does not read "
                  "it. The owner re-initialises with `levain team init --replace-legacy`; entries to keep are re-recorded")
DIRNAME = "levain-team"
CANON_FILE = "PROJECT.md"
_SCRUB_ENV = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
              "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
              "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_PREFIX", "GIT_NAMESPACE")
_PUSH_RETRIES = 6
WARNINGS: list[str] = []  # process-wide: things a person must hear that did not stop the operation


class TeamError(RuntimeError):
    """A team operation could not complete. The message is meant for a person to read."""


class TeamBusy(TeamError):
    """A lock was not acquired in time."""


# Every git call here is levain's own plumbing on a private worktree, so none of them runs the project's hooks: git on
# Linux runs a repository's commit, checkout and reference-transaction hooks on exactly these operations, and a hook that
# fails, or rewrites the index, makes a sync fail or a real entry look like an empty pick.
_NO_HOOKS = ["-c", "core.hooksPath=/dev/null"]
# rerere replays a recorded resolution and can stage it, which makes a conflicting pick look empty. Signing is added by
# the one signing wrapper (GitLedger._sign_cfg): every commit levain writes is signed, replays included.
_REPLAY_CONFIG = ["-c", "rerere.enabled=false", "-c", "rerere.autoupdate=false"]


def git(args: list[str], cwd: Path, *, timeout: float = 60, check: bool = True,
        input_text: str | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB_ENV}
    # GIT_NO_REPLACE_OBJECTS: a `git replace` must never make one ledger commit read as another
    env.update(GIT_TERMINAL_PROMPT="0", LC_ALL="C", GIT_EDITOR="true", GIT_NO_REPLACE_OBJECTS="1",
               GIT_GRAFT_FILE="/dev/null")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")  # never prompt on /dev/tty from a hook
    try:
        cp = subprocess.run(["git", *_NO_HOOKS, *args], cwd=str(cwd), env=env, capture_output=True, text=True,
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
        self._dcache: tuple[str, object] | None = None
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

    def save_state(self, _mutate=None, **changes) -> None:
        """Read, change and replace the clone's state under ONE lock, so two writers never lose each other's update
        (code L3 r1 codex: a hook's stale copy could drop a `distrust`). ``_mutate(data)`` computes changes that
        depend on the current value (a set union, a counter) inside the lock."""
        self.base.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.base / "state.lock", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = self.state()
            data.update(changes)
            if _mutate is not None:
                _mutate(data)
            tmp = self.state_path.with_suffix(f".tmp{os.getpid()}")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(tmp, self.state_path)
        finally:
            os.close(fd)

    @property
    def device(self) -> str:
        return str(self.state().get("device", ""))

    @property
    def remote(self) -> str | None:
        r = self.state().get("remote")
        return r if isinstance(r, str) and r else None

    # ---- which ledger, and whether this clone believes it ----------------------------------------------------

    @property
    def branch(self) -> str:
        b = self.state().get("branch")
        return b if isinstance(b, str) and b.startswith(BRANCH) else BRANCH

    @property
    def ref(self) -> str:
        return f"refs/heads/{self.branch}"

    @property
    def pinned_root(self) -> str | None:
        r = self.state().get("pinned_root")
        return r if isinstance(r, str) and r else None

    def clone(self):
        """This clone's own trust state (never pushed): its pin, anchor, accepted merges, distrusted commits."""
        from . import tenure as T
        st = self.state()
        return T.Clone(pinned_root=self.pinned_root or "", anchor=st.get("anchor") or None,
                       accepted={k: int(v) for k, v in dict(st.get("accepted") or {}).items()},
                       distrust=set(st.get("distrust") or []))

    def _has(self, ref: str) -> bool:
        return git(["rev-parse", "-q", "--verify", ref], self.repo.toplevel, check=False, timeout=10).returncode == 0

    def legacy_only(self) -> bool:
        """A 0.6.x ledger here (local or remote-tracking) and no strict one: the B-1 refusal applies."""
        legacy = self._has(f"refs/heads/{LEGACY_BRANCH}") or (bool(self.remote) and self._has(
            f"refs/remotes/{self.remote}/{LEGACY_BRANCH}"))
        return legacy and not self._has(self.ref) and not self.pinned_root

    def joined(self) -> bool:
        if not self.pinned_root or not self.device or not (self.wt / ".git").exists():
            return False
        cp = git(["symbolic-ref", "-q", "HEAD"], self.wt, check=False, timeout=10)
        # attached to ANOTHER branch (a 0.6.x worktree, another ledger) = not joined; detached (an interrupted replay) or
        # unreadable (a moved clone) = joined, and require_joined's repair/recovery puts it right (test lane, 1007+19)
        if cp.returncode == 0 and cp.stdout.strip() != self.ref:
            return False
        return True

    def require_joined(self) -> None:
        if not self.joined():
            if self.legacy_only():
                raise TeamError(LEGACY_MESSAGE)
            raise TeamError(f"this clone has not joined a team ledger ({self.repo.toplevel}): "
                            "run `levain team init` (first engineer) or `levain team join`")
        self._repair_if_moved()

    def _wt_is_ours(self) -> bool:
        cp = git(["rev-parse", "--path-format=absolute", "--git-common-dir"], self.wt, check=False, timeout=10)
        return cp.returncode == 0 and os.path.realpath(cp.stdout.strip()) == os.path.realpath(self.repo.common)

    def _repair_if_moved(self) -> None:
        """The worktree's .git file holds an absolute link. A moved clone leaves it dangling; a COPIED clone
        leaves it pointing at the original repository, where every write would silently land. git relinks it."""
        if self._wt_is_ours():
            return
        git(["worktree", "repair", str(self.wt)], self.repo.toplevel, check=False, timeout=30)
        if not self._wt_is_ours():
            raise TeamError(f"the ledger worktree {self.wt} does not belong to this repository and "
                            "`git worktree repair` could not relink it; run `levain team join --new-device`")

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

    # ---- identity: an SSH signing key, never an email ---------------------------------------------------------

    def email(self) -> str:
        cp = git(["config", "user.email"], self.repo.toplevel, check=False, timeout=10)
        return cp.stdout.strip()

    @property
    def signing_key(self) -> str | None:
        """The public-key file this clone signs with: the clone's state, else git's own ssh ``user.signingkey``."""
        k = self.state().get("signing_key")
        if isinstance(k, str) and k:
            return k
        fmt = git(["config", "gpg.format"], self.repo.toplevel, check=False, timeout=10).stdout.strip()
        key = git(["config", "user.signingkey"], self.repo.toplevel, check=False, timeout=10).stdout.strip()
        return key if fmt == "ssh" and key else None

    def signing_pubkey(self) -> str:
        """The public key line of this clone's signing key (a ``key::`` literal or a ``.pub`` file)."""
        k = self.signing_key
        if not k:
            raise TeamError("no signing key: set one with `levain team join --signing-key ~/.ssh/<key>.pub` (an ssh "
                            "key in ssh-agent, or one without a passphrase), or git's gpg.format=ssh + user.signingkey")
        if k.startswith("key::"):
            return k[len("key::"):].strip()
        p = Path(os.path.expanduser(k))
        if p.suffix != ".pub" and Path(str(p) + ".pub").exists():
            p = Path(str(p) + ".pub")
        try:
            return p.read_text(encoding="utf-8").strip().splitlines()[0]
        except (OSError, IndexError):
            raise TeamError(f"cannot read the signing public key {p}") from None

    def own_fingerprint(self) -> str:
        from . import signing as S
        try:
            return S.fingerprint(self.signing_pubkey())
        except S.SigningError as exc:
            raise TeamError(f"the signing key cannot be used: {exc}") from None

    def _sign_cfg(self) -> list[str]:
        """The one signing wrapper's git config. Every commit levain writes passes through it."""
        from . import signing as S
        key = self.signing_key
        if not key:
            self.signing_pubkey()   # raises the how-to
        return S.sign_config(key)

    def _sign_env(self) -> dict[str, str]:
        from . import signing as S
        return S.signing_env()

    def _remember_own_key(self) -> None:
        fps = list(self.state().get("own_keys") or [])
        fp = self.own_fingerprint()
        if fp not in fps:
            self.save_state(own_keys=fps + [fp])

    def own_keys(self) -> set[str]:
        return set(self.state().get("own_keys") or [])

    # ---- read path: everything is DERIVED from the branch's signed history, never read off the tip ------------

    def head(self) -> str:
        cp = git(["rev-parse", "-q", "--verify", self.ref], self.repo.toplevel, check=False, timeout=10)
        if cp.returncode != 0:
            if self.legacy_only():
                raise TeamError(LEGACY_MESSAGE)
            raise TeamError(f"this clone has no {self.branch} branch")
        return cp.stdout.strip()

    def _show(self, path: str, rev: str | None = None) -> str | None:  # rev: a commit SHA for a consistent snapshot
        cp = git(["show", f"{rev or self.ref}:{path}"], self.repo.toplevel, check=False, timeout=30)
        return cp.stdout if cp.returncode == 0 else None

    def derivation(self, rev: str | None = None):
        """The tenure derivation at ``rev`` (default: the tip), cached per (tip, this clone's trust state)."""
        from . import signing as S
        from . import tenure as T
        if not self.pinned_root:
            if self.legacy_only():
                raise TeamError(LEGACY_MESSAGE)
            raise TeamError("this clone has not pinned a team ledger: run `levain team join`")
        tip = rev or self.head()
        clone = self.clone()
        key = json.dumps(["derive-v1", T.RULES, S.CACHE_SCHEMA, tip, clone.pinned_root, clone.anchor,
                          sorted(clone.accepted.items()), sorted(clone.distrust)])
        if self._dcache and self._dcache[0] == key:
            return self._dcache[1]
        try:
            d = T.derive(self.repo.toplevel, tip, clone, S.SigCache(self.base / "sigcache.json"))
        except T.Unjudgeable as exc:
            raise TeamError(f"cannot judge the team ledger on this clone: {exc}") from None
        self._dcache = (key, d)
        return d

    def team(self, rev: str | None = None) -> R.Team:
        """The team IN FORCE at ``rev``: the counted team, never the tip's team.toml."""
        return self.derivation(rev).team

    def handle(self, team: R.Team | None = None) -> str | None:
        """This clone's member handle: the member whose keys include this clone's signing key (email as fallback
        only for display, before a key is confirmed)."""
        try:
            d = self.derivation()
            fp = self.own_fingerprint()
            from . import tenure as T
            for h in d.team.members:
                if fp in T.key_fps(d.tenure, h):
                    return h
        except TeamError:
            pass
        return None     # identity is the key: an email never makes this clone a member (L1 1007+19 #6)

    def snapshot(self) -> tuple[str, R.Team, I.Ledger]:
        """(sha, team, ledger), all derived from ONE commit of the ledger branch."""
        sha = self.head()
        d = self.derivation(sha)
        return sha, d.team, self._ledger_of(d)

    def ledger(self, team: R.Team | None = None, rev: str | None = None) -> I.Ledger:
        """The ledger as enforced: every line in its chain, enforced only where tenure says so."""
        return self._ledger_of(self.derivation(rev))

    @staticmethod
    def _ledger_of(d) -> I.Ledger:
        problems = list(d.problems)
        if d.judged != "full":
            problems.append(f"judged: partial: {d.frozen_why}; verdicts are frozen at {str(d.frozen_at)[:10]}"
                            + (f"; {d.waiting} line(s) after it are waiting" if d.waiting else ""))
        files = [(rel, lines) for rel, lines in sorted(d.files.items())]
        return I.build(files, d.team.owner, problems, unenforced=d.unenforced, authority=d.owner_authority)

    def team_history_problems(self, team: R.Team, rev: str | None = None) -> list[str]:
        """Kept for callers: on a strict ledger these are part of the derivation's own problems."""
        return []

    def file_for(self, author: str) -> Path:
        return self.wt / "ledger" / E.safe_handle(author) / f"{self.device}.jsonl"

    # ---- setup ---------------------------------------------------------------------------------------------

    def _remote_has_branch(self, remote: str) -> bool:
        cp = git(["ls-remote", "--heads", remote, f"refs/heads/{self.branch}"], self.repo.toplevel, timeout=60)
        return bool(cp.stdout.strip())

    def _local_branch_exists(self) -> bool:
        cp = git(["rev-parse", "--verify", "-q", f"refs/heads/{self.branch}"], self.repo.toplevel, check=False)
        return cp.returncode == 0

    def _default_remote(self) -> str | None:
        cp = git(["remote"], self.repo.toplevel, check=False)
        names = cp.stdout.split()
        if "origin" in names:
            return "origin"
        return names[0] if names else None

    def _attach_worktree(self) -> None:
        if (self.wt / ".git").exists():
            self._repair_if_moved()   # a copied clone's worktree may still belong to the original repository
            cp = git(["symbolic-ref", "-q", "HEAD"], self.wt, check=False, timeout=10)
            if cp.returncode == 0 and cp.stdout.strip() == self.ref:
                return
            # a 0.6.x worktree (levain-ledger) or another ledger's: levain's own private checkout, re-created rather
            # than reused, so no state of the old ledger carries over (T r22-2 codex: the legacy worktree was reused)
            git(["worktree", "remove", "--force", "--force", str(self.wt)], self.repo.toplevel, check=False, timeout=60)
            if (self.wt / ".git").exists():
                raise TeamError(f"the ledger worktree {self.wt} belongs to another ledger and could not be removed")
        self.base.mkdir(parents=True, exist_ok=True)
        git(["worktree", "prune"], self.repo.toplevel, check=False)
        git(["worktree", "add", "--lock", "--reason", "levain team ledger", str(self.wt), self.branch],
            self.repo.toplevel)

    def _new_device(self) -> str:
        return self.device or secrets.token_hex(8)

    def _remote_ledgers(self, remote: str) -> list[str]:
        """Every strict ledger branch the remote advertises (a re-genesis adds BRANCH-<id>)."""
        cp = git(["ls-remote", "--heads", remote, f"refs/heads/{BRANCH}*"], self.repo.toplevel, timeout=60)
        out = []
        for row in cp.stdout.splitlines():
            name = row.split("\t", 1)[1][len("refs/heads/"):] if "\t" in row else ""
            if name == BRANCH or name.startswith(BRANCH + "-"):
                out.append(name)
        return sorted(out)

    def _genesis_commit(self, tree_text: dict[str, str], message: str) -> str:
        top = self.repo.toplevel
        rows = []
        for name, text in sorted(tree_text.items()):
            blob = git(["hash-object", "-w", "--stdin"], top, input_text=text).stdout.strip()
            rows.append(f"100644 blob {blob}\t{name}")
        tree = git(["mktree"], top, input_text="\n".join(rows) + "\n").stdout.strip()
        cp = subprocess.run(["git", *_NO_HOOKS, *self._sign_cfg(), "commit-tree", "-S", tree, "-m", message],
                            cwd=str(top), capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                            env={**{k: v for k, v in os.environ.items() if k not in _SCRUB_ENV},
                                 "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", **self._sign_env()})
        if cp.returncode != 0:
            raise TeamError(f"could not sign the genesis commit: {(cp.stderr or cp.stdout).strip()[-200:]}")
        return cp.stdout.strip()

    def init(self, team: R.Team, *, member_keys: dict[str, str] | None = None, remote: str | None = None,
             push: bool = True, signing_key: str | None = None, replace_legacy: bool = False) -> str:
        """Create a STRICT ledger: a genesis carrying team.toml and tenure.toml, signed by the owner's key (this
        clone's), pinned here in the same operation. Members' keys are pending until each confirms from their own
        machine (`levain team join`). Returns a status line."""
        from . import signing as S
        from . import tenure as T
        R.validate_team(team)
        if signing_key:
            self.save_state(signing_key=signing_key)
        own = self.signing_pubkey()
        try:
            S.fingerprint(own)
        except S.SigningError as exc:
            raise TeamError(f"the signing key cannot be used: {exc}") from None
        remote = remote or self._default_remote()
        if self.legacy_only() and not replace_legacy:
            raise TeamError(LEGACY_MESSAGE)
        if self._local_branch_exists():
            raise TeamError(f"branch {self.branch} already exists here: use `levain team join`")
        if remote and self._remote_ledgers(remote):
            raise TeamError(f"{remote} already has a team ledger: use `levain team join`")
        ten = T.Tenure(keys={team.owner: [own]})
        for h, line in (member_keys or {}).items():
            if h not in team.members:
                raise TeamError(f"--member key for {h!r}, who is not a member")
            if h == team.owner:
                continue
            try:
                S.fingerprint(line)
            except S.SigningError as exc:
                raise TeamError(f"{h}'s key cannot be used: {exc}") from None
            ten.pending_keys[h] = [line.strip()]
        commit = self._genesis_commit({T.TEAM_FILE: R.dump_team(team), T.TENURE_FILE: T.dump_tenure(ten)},
                                      f"levain team: init strict ledger for {team.project}")
        git(["update-ref", self.ref, commit, ""], self.repo.toplevel)
        self.save_state(device=self._new_device(), remote=remote or "", branch=self.branch, pinned_root=commit,
                        anchor=None, accepted={}, distrust=[])
        self._remember_own_key()
        self.derivation()      # proves the genesis judges, before anything is pushed
        self._attach_worktree()
        if remote and push:
            self._sync(push=True)
            return f"strict ledger created and pushed to {remote}/{self.branch} (genesis {commit[:12]})"
        return f"strict ledger created locally ({'no remote' if not remote else 'not pushed'}; genesis {commit[:12]})"

    def join(self, *, remote: str | None = None, new_device: bool = False, root: str | None = None,
             signing_key: str | None = None, accept_merges: dict[str, int] | None = None) -> str:
        """Pin this clone to a strict ledger on the remote (trust on first use: the genesis, its owner in force and
        her key fingerprints are printed for the person to check out of band), then confirm this clone's key."""
        from . import tenure as T
        if signing_key:
            self.save_state(signing_key=signing_key)
        remote = remote or self._default_remote()
        if not remote:
            raise TeamError("no git remote to join from")
        names = self._remote_ledgers(remote)
        if not names:
            if git(["ls-remote", "--heads", remote, f"refs/heads/{LEGACY_BRANCH}"], self.repo.toplevel,
                   timeout=60).stdout.strip():
                raise TeamError(LEGACY_MESSAGE)
            raise TeamError(f"{remote} has no team ledger")
        found: dict[str, str] = {}
        for name in names:
            git(["fetch", "-q", remote, f"+refs/heads/{name}:refs/remotes/{remote}/{name}"], self.repo.toplevel,
                timeout=120)
            roots = git(["rev-list", "--max-parents=0", f"refs/remotes/{remote}/{name}"], self.repo.toplevel,
                        timeout=60).stdout.split()
            if len(roots) == 1:
                found[name] = roots[0]
        if root:
            pick = [n for n, r in found.items() if r.startswith(root)]
            if len(pick) != 1:
                raise TeamError(f"no single team ledger on {remote} has genesis {root}")
            name = pick[0]
        elif len(found) == 1:
            name = next(iter(found))
        else:
            listing = "; ".join(f"{n}: genesis {r[:12]}" for n, r in sorted(found.items()))
            raise TeamError(f"{remote} has {len(found)} team ledgers ({listing}). Choose the team to trust with "
                            "`levain team join --root <genesis>` (a re-genesis lives beside the ledger it replaced)")
        rref = f"refs/remotes/{remote}/{name}"
        for sha, n in list((accept_merges or {}).items()):
            ps = git(["rev-list", "--parents", "-n", "1", sha], self.repo.toplevel, check=False).stdout.split()[1:]
            if len(ps) < 2 or not 1 <= int(n) <= len(ps):
                raise TeamError(f"--accept-merge {sha[:10]}:{n} does not name a merge and one of its parents")
        merges = git(["rev-list", "--first-parent", "--merges", rref], self.repo.toplevel, timeout=60).stdout.split()
        unaccepted = [m for m in merges if m not in (accept_merges or {})]
        if unaccepted:
            raise TeamError(f"the ledger's history is not linear ({len(unaccepted)} merge(s), first {unaccepted[0][:10]}); "
                            "join with `--accept-merge <sha>:<parent>` for each merge the owner names")
        if git(["rev-parse", "--is-shallow-repository"], self.repo.toplevel).stdout.strip() == "true":
            git(["fetch", "-q", "--unshallow", remote, f"+refs/heads/{name}:{rref}"], self.repo.toplevel,
                check=False, timeout=300)
            if git(["rev-parse", "--is-shallow-repository"], self.repo.toplevel).stdout.strip() == "true":
                # a shallow cut-off reads as a root: pinning it would trust a commit that is not the genesis (L2 F3)
                raise TeamError("this clone is shallow and could not be deepened; join from a full clone")
        tip0 = git(["rev-parse", rref], self.repo.toplevel).stdout.strip()
        if git(["rev-parse", "-q", "--verify", f"refs/heads/{name}"], self.repo.toplevel, check=False).returncode == 0:
            here = git(["rev-parse", f"refs/heads/{name}"], self.repo.toplevel).stdout.strip()
            if git(["merge-base", "--is-ancestor", here, tip0], self.repo.toplevel, check=False).returncode != 0 and \
                    git(["merge-base", "--is-ancestor", tip0, here], self.repo.toplevel, check=False).returncode != 0:
                raise TeamError(f"the local {name} branch has diverged from {remote}'s; nothing was changed")
        old = self.state()
        self.save_state(branch=name, pinned_root=found[name], anchor=None, accepted=dict(accept_merges or {}),
                        distrust=[], remote=remote, device=secrets.token_hex(8) if new_device else self._new_device())
        self._dcache = None
        try:
            d = self.derivation(git(["rev-parse", rref], self.repo.toplevel).stdout.strip())
        except TeamError:
            self.save_state(**{k: old.get(k) for k in ("branch", "pinned_root", "anchor", "accepted", "distrust")})
            raise
        tip = git(["rev-parse", rref], self.repo.toplevel).stdout.strip()
        if not self._local_branch_exists():
            git(["branch", name, rref], self.repo.toplevel)
        self.save_state(anchor=tip)
        self._record_seen_sha(tip)       # the joined tip was published: never movable (code L3 r1 codex HIGH)
        self._remember_own_key()
        self._attach_worktree()
        owner = d.team.owner
        fps = ", ".join(sorted(T.key_fps(d.tenure, owner))) or "none"
        tofu = (f"TEAM LEDGER {name}: genesis {found[name]}, owner in force {owner} (keys {fps}). Check these with "
                "the owner out of band before relying on it.")
        confirmed = self._confirm_own_key(d)
        handle = self.handle(d.team)
        if handle is None:
            fp = self.own_fingerprint()
            return (tofu + f"\njoined {d.team.project}, but this machine's key ({fp}) is not a member's key yet: from "
                    "one of your machines whose key is in force, run `levain team key add <your handle> <this public "
                    f"key>`; if you have none yet, {owner} proposes your first key (`levain team key add <your "
                    "handle> <your public key>`); then `levain team sync`")
        return tofu + f"\njoined {d.team.project} as {handle} (device {self.device})" + (
            f"; confirmed this machine's key" if confirmed else "")

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
        written; only its commit was lost). An uncommitted team.toml, tenure.toml or PROJECT.md is discarded: those are
        regenerated by re-running the command that failed. Anything else is refused for a person to inspect.
        """
        for marker, op in (("rebase-merge", "rebase"), ("rebase-apply", "rebase"), ("CHERRY_PICK_HEAD", "cherry-pick")):
            p = git(["rev-parse", "--git-path", marker], self.wt).stdout.strip()
            if p and (self.wt / p).exists():
                git([op, "--abort"], self.wt, check=False)
        if git(["symbolic-ref", "-q", "HEAD"], self.wt, check=False).returncode != 0:
            # detached: a replay was interrupted before it published. The branch still holds every entry.
            git(["checkout", "-q", "-f", self.branch], self.wt, timeout=60)
            self.warnings.append("an interrupted sync was rolled back; nothing was lost")
        dirty = self._dirty()
        if not dirty:
            return
        own = re.compile(r"^ledger/[^/]+/" + re.escape(self.device) + r"\.jsonl$")
        regenerable = [p for p in dirty if p in ("team.toml", "tenure.toml", CANON_FILE)]   # r2 codex MED: tenure
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
        """Every commit levain writes is SIGNED here (signing doc §5): no unsigned ledger commit is ever written.

        Each also carries this clone's ``Levain-Device`` trailer: a sync re-publishes only commits this DEVICE wrote,
        never one another clone holding the same key published and a hand ``git pull`` brought in (T36, RAN)."""
        from . import tenure as T
        if not self.device:
            raise TeamError("this clone has no device id, so a commit it writes could not be told from another "
                            "clone's: run `levain team join`")
        last = message.rstrip("\n").rsplit("\n\n", 1)[-1]
        sep = "\n" if T._TRAILER_RE.search(last) else "\n\n"
        message = f"{message.rstrip(chr(10))}{sep}{T.DEVICE_TRAILER}: {self.device}\n"
        cp = subprocess.run(["git", *_NO_HOOKS, *self._sign_cfg(), "commit", "-q", "--no-verify", "-F", "-"],
                            cwd=str(self.wt), capture_output=True, text=True, timeout=60, input=message,
                            env={**{k: v for k, v in os.environ.items() if k not in _SCRUB_ENV},
                                 "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "GIT_NO_REPLACE_OBJECTS": "1",
                                 **self._sign_env()})
        if cp.returncode != 0:
            msg = (cp.stderr or cp.stdout).strip().splitlines()
            git(["reset", "-q", "--mixed", "HEAD"], self.wt, check=False)
            raise TeamError("could not sign the ledger commit (levain writes nothing unsigned): "
                            + (msg[-1] if msg else f"exit {cp.returncode}")
                            + ". The signing key must be in ssh-agent or have no passphrase")

    def _confirm_own_key(self, d) -> bool:
        """If this machine's key is PENDING for a member, move it into force with a commit signed by that key."""
        from . import tenure as T
        fp = self.own_fingerprint()
        holder = next((h for h, lines in d.tenure.pending_keys.items()
                       if fp in {T._fp_or_none(x) for x in lines}), None)
        if holder is None:
            return False

        def confirm(team: R.Team, ten) -> None:
            line = next(x for x in ten.pending_keys[holder] if T._fp_or_none(x) == fp)
            ten.pending_keys[holder] = [x for x in ten.pending_keys[holder] if x != line]
            ten.keys.setdefault(holder, []).append(line)
        self.update_counted(confirm, f"levain team: {holder} confirms a key", push=bool(self.remote))
        return True

    def _require_own_key_in_force(self, author: str) -> None:
        from . import tenure as T
        d = self.derivation()
        holder = d.team.owner if author.startswith("pack:") else author   # pack lines are the owner's (pack-* folders)
        if self.own_fingerprint() not in T.key_fps(d.tenure, holder):
            raise TeamError(f"this machine's key is not confirmed for {author}, so a line it signs would not be "
                            f"enforced: from a machine whose key is in force for {author}, run `levain team key add "
                            f"{author} <this machine's public key>`, then `levain team sync` here (a member who lost "
                            "every key is invited again under a new handle)")

    def append(self, entry: dict, *, push: bool = True, lock_timeout: float = 30.0) -> dict:
        """Validate, seal and append one entry to this author's file for this clone; commit; push.

        A refusal (EntryError) writes nothing. A push failure leaves the entry committed locally and
        raises TeamError saying so; the next write or `levain team sync` pushes it.
        """
        self.require_joined()
        self._require_own_key_in_force(entry["author"])
        with self.lock(timeout=lock_timeout):
            self._recover_dirty()
            self._dcache = None
            ledger = self.ledger()
            E.validate(entry, known=ledger.by_id)
            for s in entry.get("supersedes", []):
                target = ledger.by_id[s]
                why = I.may_link(entry, target, ledger.owner,
                                 (lambda e: self.handle() == ledger.owner) if ledger.authority else None)
                if why is not None:
                    raise E.EntryError(f"{why}. Record your own entry (refs it with --refs) and ask the owner.")
            path = self.file_for(entry["author"])
            rel = path.relative_to(self.wt / "ledger").as_posix()
            prev = next((f.last_hash for f in ledger.files if f.rel == rel), "")
            sealed = E.seal(entry, prev)
            line = json.dumps(sealed, ensure_ascii=False, sort_keys=True) + "\n"
            with os.fdopen(self._open_ledger_file(path), "a+b") as fh:
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
            self._dcache = None
            d = self.derivation()
            if d.judged != "full":
                # frozen: an append still lands (it is WAITING, not enforced, until the owner resolves the freeze); the
                # read-back is of the committed line itself
                shown = self._show(f"ledger/{rel}") or ""
                if line.rstrip("\n") not in shown.split("\n"):
                    raise TeamError(f"wrote {sealed['id']} but it is not in the committed file; run `levain team verify`")
                self.warnings.append(f"{sealed['id']} recorded, but the ledger is frozen ({d.frozen_why}): it is "
                                     "WAITING and not enforced until the owner resolves it")
            elif sealed["id"] not in self.ledger().by_id:
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
        cp = git(["fetch", "-q", remote, f"+refs/heads/{self.branch}:{rref}"], self.repo.toplevel, timeout=timeout,
                 check=False)
        if cp.returncode == 0:
            now = time.time()
            self.save_state(last_fetch_attempt=now, last_fetch_ok=now, last_fetch_error="")
            return True
        if "couldn't find remote ref" in (cp.stderr or ""):
            return False
        raise TeamError(f"git fetch failed: {_tail(cp)}")

    # ---- sync recovery (tenure_design_1005.md §3a, B-2): move only this clone's OWN signed commits ---------------

    def _record_seen(self, rref: str) -> None:
        """Remember every remote tip this clone has fetched, collapsed to descendant-most tips."""
        tip = git(["rev-parse", "-q", "--verify", rref], self.repo.toplevel, check=False).stdout.strip()
        if not tip:
            return
        refs = git(["for-each-ref", "--format=%(refname) %(objectname)", f"refs/levain/seen/{self.branch}/"],
                   self.repo.toplevel, check=False).stdout.split("\n")
        seen = {r.split()[0]: r.split()[1] for r in refs if r.strip()}
        if tip in seen.values():
            return
        for name, sha in seen.items():
            if git(["merge-base", "--is-ancestor", sha, tip], self.repo.toplevel, check=False).returncode == 0:
                git(["update-ref", "-d", name], self.repo.toplevel, check=False)
        git(["update-ref", f"refs/levain/seen/{self.branch}/{tip}", tip], self.repo.toplevel)

    def _seen_tips(self) -> list[str]:
        out = git(["for-each-ref", "--format=%(objectname)", "refs/levain/seen/", "refs/levain/gone/", "refs/remotes/"],
                  self.repo.toplevel, check=False).stdout.split()
        anchor = self.state().get("anchor")
        return sorted(set(out) | ({anchor} if anchor else set()))   # the anchor is published by definition

    def _movable(self, orig: str, remote_tip: str | None = None) -> tuple[list[str], list[str]]:
        """(own commits to move, oldest first; unseen commits NOT moved because no key of this clone signed them).

        The filter is cryptographic (r22 L3: an email filter let a sync re-sign an unsigned commit)."""
        from . import signing as S
        excl = [f"^{t}" for t in self._seen_tips()]
        shas = git(["rev-list", "--reverse", "--topo-order", "--no-merges", orig, *excl], self.repo.toplevel,
                   check=False).stdout.split()
        verdicts = S.SigCache(self.base / "sigcache.json").verify(self.repo.toplevel, shas) if shas else {}
        unsure = [c for c in shas if verdicts[c].kind == "indeterminate"]
        if unsure:
            # this machine cannot check a signature: replay NOTHING rather than file its own work as foreign (code L3
            # r1 complement MED)
            raise TeamError(f"cannot verify the signature of {unsure[0][:10]} ({verdicts[unsure[0]].reason}); nothing "
                            "was replayed. Run `levain team doctor`")
        mine = self.own_keys() | {self.own_fingerprint()}
        revoked: set[str] = set()
        for rev in ([remote_tip] if remote_tip else []) + [orig]:
            # revocations as the REMOTE counts them as well as local (code L3 r1 codex HIGH: a revocation that landed
            # remotely while this clone was offline must stop the replay); a remote this clone cannot judge stops it
            try:
                revoked |= {r["key"] for r in self.derivation(rev).tenure.revokes}
            except TeamError as exc:
                if rev == remote_tip:
                    raise TeamError(f"cannot judge the remote ledger, so nothing is replayed onto it: {exc}") from None
        mine -= revoked          # a revoked own key's commits are never re-signed with the new key (L2 Q6)
        # signed by this clone's key AND written by this device: the same key on another clone is another writer, and
        # its commits reach here only by a hand pull (T36, RAN: a host repair then got them re-published). The device
        # is read from the raw object, inside what the signature covers.
        from . import tenure as T
        try:
            meta = T.metas(self.repo.toplevel, shas)
        except T.Unjudgeable as exc:
            raise TeamError(f"cannot read the unpublished commits, so nothing was replayed: {exc}") from None
        own = [c for c in shas if verdicts[c].kind == "signed" and verdicts[c].fingerprint in mine
               and meta[c].trailer(T.DEVICE_TRAILER) == self.device]
        return own, [c for c in shas if c not in own]

    def _delta(self, sha: str) -> dict | None:
        """A team/tenure commit of this clone as a pending op: the fields it changed against ITS OWN parent, each with
        the commit that had last touched that field (history-keyed compare-and-swap, not value-keyed)."""
        from . import tenure as T
        msg = git(["log", "-1", "--format=%B", sha], self.repo.toplevel).stdout
        if "Levain-Base:" not in msg:
            return None
        if "levain team: restore the counted team" in msg:
            return {"restore": True}
        parent = git(["rev-parse", f"{sha}^"], self.repo.toplevel).stdout.strip()
        try:
            before_d = self.derivation(parent)
            after = T.flat(R.parse_team(self._show(T.TEAM_FILE, sha) or "", "x"),
                           T.parse_tenure(self._show(T.TENURE_FILE, sha) or "", "x"))
        except (TeamError, R.RolesError):
            return None
        before = before_d.state
        fields = []
        for k in set(before) | set(after):
            if before.get(k) != after.get(k):
                fields.append({"k": list(k), "old": before.get(k), "new": after.get(k),
                               "base": before_d.touched.get(k)})
        return {"fields": fields, "message": msg.split("\n\n", 1)[0]}

    def _rebase(self, rref: str, timeout: float, lock_timeout: float) -> None:
        """Move this clone's own commits onto the remote, then re-land held team ops on EVERY successful exit (code L3
        r2 codex HIGH + complement MED, RUN: the fast-forward and already-on-top returns skipped the re-land, so an op
        held while frozen stayed queued after the freeze cleared)."""
        self._rebase_moves(rref, timeout, lock_timeout)
        self._dcache = None
        self._reland()

    def _rebase_moves(self, rref: str, timeout: float, lock_timeout: float) -> None:
        """Put this clone's OWN unpublished commits on top of the remote, under the worktree lock.

        The moved set is ``HEAD --not <every seen tip> --no-merges`` signed by this clone's own keys. Ledger-line
        commits are cherry-picked (and re-signed) onto the remote tip. Team/tenure commits are never replayed as
        text: they are stripped into pending ops and re-landed from the COUNTED state at the new tip, each field only
        if no other counted commit touched it since (history-keyed), so an offline change never overwrites a newer
        decision. Nothing that came from the remote is ever re-published.
        """
        with self.lock(timeout=lock_timeout):
            self._recover_dirty()
            orig = git(["rev-parse", "HEAD"], self.wt).stdout.strip()
            remote_tip = git(["rev-parse", rref], self.wt).stdout.strip()
            # A REWRITTEN remote (a force-push, a host repair): some tip this clone saw published is no longer reachable
            # from it. Then nothing that came from the remote may go back up (r13 daemon M1; the 1007+19 residue run
            # caught a fast-forward push that re-published a force-pushed-away history): the branch is rebuilt on the
            # remote tip from this clone's OWN unseen commits only, and lost published entries are reported.
            published = set(git(["for-each-ref", "--format=%(objectname)", f"refs/levain/seen/{self.branch}/"],
                                self.wt, check=False).stdout.split())
            if self.state().get("anchor"):
                published.add(self.state()["anchor"])
            gone = [t for t in sorted(published)
                    if git(["merge-base", "--is-ancestor", t, remote_tip], self.wt, check=False).returncode != 0]
            if gone:
                self._report_lost(gone, remote_tip)
            elif git(["merge-base", "--is-ancestor", orig, remote_tip], self.wt, check=False).returncode == 0:
                git(["update-ref", "-m", "levain team: fast-forward", self.ref, remote_tip, orig], self.wt)
                git(["checkout", "-q", "-f", self.branch], self.wt, timeout=60)
                return
            own, foreign = self._movable(orig, remote_tip)
            if foreign:
                self.warnings.append(f"{len(foreign)} unpublished ledger commit(s) here were not written by this "
                                     "clone (another key, or another clone of the same key) and were NOT moved or "
                                     "re-signed: "
                                     + ", ".join(c[:10] for c in foreign[:5]))
            for c in foreign:   # kept reachable for a person to inspect, never pushed (L1 1007+19 #5, RAN)
                git(["update-ref", f"refs/levain/foreign/{c}", c], self.wt, check=False)
            if not gone and not foreign and \
                    git(["merge-base", "--is-ancestor", remote_tip, orig], self.wt, check=False).returncode == 0:
                return     # own commits already on top of an unrewritten remote: nothing to replay
            held = list(self.state().get("pending_ops") or [])
            pending = list(held)
            picks: list[str] = []
            for c in own:
                touched = set(git(["diff-tree", "--no-commit-id", "--name-only", "-r", c], self.wt).stdout.split())
                if touched & {"team.toml", "tenure.toml"}:
                    op = self._delta(c)
                    if op is None:
                        self.warnings.append(f"a local team change {c[:10]} could not be re-applied (no levain "
                                             "trailer, or its parent cannot be judged here) and was dropped; re-run "
                                             "it with the levain CLI")
                    elif not op.get("restore"):
                        pending.append(op)
                    rest = touched - {"team.toml", "tenure.toml", CANON_FILE}
                    if rest:
                        self.warnings.append(f"local commit {c[:10]} mixes team and ledger changes; only its team "
                                             "change is kept (re-landed)")
                elif touched <= {CANON_FILE}:
                    picks.append(c)    # PROJECT.md: replayed; dropped below only if it conflicts
                else:
                    picks.append(c)
            published = False
            try:
                git(["checkout", "-q", "--detach", remote_tip], self.wt, timeout=timeout)
                for c in picks:
                    cp = git([*_REPLAY_CONFIG, *self._sign_cfg(), "cherry-pick", "--allow-empty", c],
                             self.wt, check=False, timeout=timeout)
                    if cp.returncode == 0:
                        continue
                    unmerged = git(["diff", "--name-only", "--diff-filter=U"], self.wt, check=False).stdout.split()
                    if unmerged and set(unmerged) <= {CANON_FILE}:
                        git(["cherry-pick", "--abort"], self.wt, check=False)
                        git(["checkout", "-q", "--detach", "HEAD"], self.wt, check=False)
                        self.warnings.append("a local PROJECT.md conflicted with the remote and was dropped; re-run "
                                             "`levain team consolidate`")
                        continue
                    marker = self.wt / git(["rev-parse", "--git-path", "CHERRY_PICK_HEAD"], self.wt).stdout.strip()
                    try:
                        picking = marker.read_text().strip()
                    except OSError:
                        picking = ""
                    # an EMPTY pick (already upstream) is exit 1 with the marker naming THIS commit and nothing staged;
                    # anything else is a failure, never a silent skip (restored after the test lane caught its loss)
                    if cp.returncode == 1 and picking == c and not unmerged and \
                            git(["diff", "--cached", "--quiet"], self.wt, check=False).returncode == 0:
                        git(["cherry-pick", "--skip"], self.wt, timeout=60)
                        continue
                    raise TeamError("an unpushed entry cannot be replayed onto the remote ledger (two clones share "
                                    f"a device id, or git could not run: {_tail(cp)}). Nothing was changed locally; "
                                    "if this clone's .git was copied from another, see `levain team join --new-device`")
                new = git(["rev-parse", "HEAD"], self.wt).stdout.strip()
                # the stripped ops are saved BEFORE the ref moves past their commits (code L3 r2 glm MED, complement
                # LOW: a failure between the two lost them), and put back if the ref never moves
                self.save_state(pending_ops=pending)
                git(["update-ref", "-m", "levain team: replay onto remote", self.ref, new, orig], self.wt)
                published = True
                git(["checkout", "-q", "-f", self.branch], self.wt, timeout=60)
            finally:
                if not published:
                    self.save_state(pending_ops=held)
                    try:
                        self._reattach()
                    except TeamError as exc:
                        self.warnings.append(f"the ledger worktree was left detached ({exc}); the next levain team "
                                             "command re-attaches it")

    def _report_lost(self, gone: list[str], remote_tip: str) -> None:
        """Own published commits the remote no longer has: reported, never re-published, never silently dropped."""
        from . import signing as S
        lost = git(["rev-list", "--no-merges", *gone, f"^{remote_tip}"], self.wt, check=False).stdout.split()
        if not lost:
            return
        verdicts = S.SigCache(self.base / "sigcache.json").verify(self.repo.toplevel, lost)
        mine = self.own_keys() | {self.own_fingerprint()}
        own = [c for c in lost if verdicts[c].kind == "signed" and verdicts[c].fingerprint in mine]
        self.warnings.append(f"the remote ledger was REWRITTEN (force-push or host repair): {len(lost)} published "
                             f"commit(s) are no longer on it, {len(own)} of them yours. Nothing was re-published. "
                             "This clone keeps its last full derivation until the owner names a repair "
                             "(`levain team repin --anchor <commit>`); re-record any of your entries still wanted")
        # the gone tips stay EXCLUDED from every later sync (moved under refs/levain/gone/, so this report fires once
        # per rewrite): a commit that came from the remote is never re-published, even after the remote dropped it
        for name in git(["for-each-ref", "--format=%(refname)", f"refs/levain/seen/{self.branch}/"], self.wt,
                        check=False).stdout.split():
            sha = name.rsplit("/", 1)[-1]
            if sha in gone:
                git(["update-ref", f"refs/levain/gone/{self.branch}/{sha}", sha], self.wt, check=False)
                git(["update-ref", "-d", name], self.wt, check=False)
        self._record_seen_sha(remote_tip)

    def _record_seen_sha(self, tip: str) -> None:
        git(["update-ref", f"refs/levain/seen/{self.branch}/{tip}", tip], self.repo.toplevel, check=False)

    def _reland(self) -> None:
        """Re-apply stripped team ops to the counted state at the new tip: net per field, history-keyed."""
        from . import tenure as T
        ops = list(self.state().get("pending_ops") or [])
        if not ops:
            return
        try:
            d = self.derivation()
        except TeamError:
            return
        if d.judged != "full":
            return     # held while frozen; re-landed on the first sync after the freeze clears
        net: dict[tuple, dict] = {}
        for op in ops:
            for f in op.get("fields", []):
                k = tuple(f["k"])
                if k not in net:
                    net[k] = {"old": f["old"], "new": f["new"], "base": f["base"]}
                else:
                    net[k]["new"] = f["new"]
        # FIELD BY FIELD (design §3b), with a removal's key and pending fields judged as part of the removal: they are
        # consequences, not separate decisions, so a stale member field can no longer re-land its key removals alone
        # (lane A T16, RUN: ben was left a member with no key and a handle nobody could re-key). A set that would not
        # count is dropped with a message, never left to wedge every later sync (lane A, RUN, against the r2 roll-back)
        removed = {k[1] for k, f in net.items() if k[0] == "member" and f["new"] is None}
        net = {k: f for k, f in net.items() if not (k[0] in ("key", "pending") and k[1] in removed)}
        apply: dict[tuple, object] = {}
        for k, f in net.items():
            if d.touched.get(k) != f["base"]:
                self.warnings.append(f"your offline change to {'.'.join(map(str, k))} was NOT re-applied: a newer "
                                     "counted change touched it; re-issue it if still wanted")
            elif f["old"] != f["new"]:
                apply[k] = f["new"]
        if not apply:
            self.save_state(pending_ops=[])
            return

        def change(team: R.Team, ten) -> None:
            st = T.flat(team, ten)
            for k, v in apply.items():
                if v is None:
                    st.pop(k, None)
                else:
                    st[k] = v
            for k in [k for k in st if k[0] in ("key", "pending") and ("member", k[1]) not in st]:
                del st[k]
            t2, n2 = T.unflat(st)
            team.__dict__.update(t2.__dict__)
            ten.__dict__.update(n2.__dict__)
        try:
            self.update_counted(change, "levain team: re-land an offline team change", push=False)
        except TeamError as exc:
            if "would not count" not in str(exc):
                raise          # a signing failure keeps the ops for the next sync
            self.warnings.append(f"your offline team change(s) were NOT re-applied, none of them: {exc}")
        self.save_state(pending_ops=[])     # only after the re-land is committed or refused whole
        self._dcache = None

    def _reattach(self) -> None:
        """Abort any replay in progress and put the worktree back on the branch, wherever the branch points."""
        for op in (["cherry-pick", "--abort"], ["rebase", "--abort"]):
            git(op, self.wt, check=False, timeout=60)
        git(["checkout", "-q", "-f", self.branch], self.wt, timeout=60)

    def _restore(self, orig: str) -> None:
        """Back to exactly ``orig``: abort whatever is in progress, then hard-reset. Raises if that fails."""
        for op in (["rebase", "--abort"], ["cherry-pick", "--abort"]):
            git(op, self.wt, check=False, timeout=60)
        git(["reset", "-q", "--hard", orig], self.wt, timeout=60)

    def _sync(self, *, push: bool, timeout: float = 120, net_timeout: float = 150,
              lock_timeout: float = 30.0) -> str:
        """Fetch, rebase, optionally push. Must be called WITHOUT the worktree lock held.

        Network I/O runs under a separate ``net`` lock, so a hook reading the worktree never waits on a
        slow remote: the worktree lock is held only for the local rebase and commits.
        """
        remote = self.remote
        if not remote:
            return "local only (no remote)"
        rref = f"refs/remotes/{remote}/{self.branch}"
        local = self.ref
        with self.lock(name="net", timeout=net_timeout):
            for attempt in range(_PUSH_RETRIES):
                if not self._fetch(remote, rref, timeout):
                    published = bool(self.state().get("anchor")) or bool(git(
                        ["for-each-ref", "--count=1", f"refs/levain/seen/{self.branch}/", f"refs/levain/gone/{self.branch}/"],
                        self.repo.toplevel, check=False).stdout.strip())
                    if self.pinned_root and (published or self._has(rref)):
                        # durable state, not the tracking ref: `git fetch --prune` deletes that (L1 1007+19 #4, RAN)
                        # a PINNED clone never recreates a deleted ledger: pushing would republish every commit the
                        # host removed (T r22-2 codex HIGH). Only `init` (or a re-genesis, on its own new branch) creates one.
                        raise TeamError(f"{remote} no longer has {self.branch} (deleted or moved); nothing was "
                                        "pushed. Ask the owner what happened before writing more")
                    if not push:
                        return f"{remote} has no {self.branch} branch yet"
                else:
                    self._record_seen(rref)
                    # best effort: other strict ledgers on the remote (a re-genesis names this one as its prior)
                    git(["fetch", "-q", remote, f"+refs/heads/{BRANCH}-*:refs/remotes/{remote}/{BRANCH}-*"],
                        self.repo.toplevel, check=False, timeout=timeout)
                    self._rebase(rref, timeout, lock_timeout)
                    self._advance_anchor(rref)
                    if not push:
                        return "fetched"
                    ahead = git(["rev-list", "--count", f"{rref}..{local}"], self.wt).stdout.strip()
                    if ahead == "0":
                        return "up to date"
                    if git(["rev-list", "--merges", f"{rref}..{local}"], self.wt).stdout.strip():
                        raise TeamError("refusing to push a merge to the team ledger (it must stay linear)")
                cp = git(["push", "-q", "--no-verify", remote, f"{local}:{local}"], self.repo.toplevel,
                         check=False, timeout=timeout)
                if cp.returncode == 0:
                    try:
                        if self._fetch(remote, rref, timeout):
                            self._record_seen(rref)
                            self._advance_anchor(rref)
                    except TeamError:
                        pass  # the push landed; a failed refresh of the tracking ref is not a failed push
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

    def _advance_anchor(self, rref: str) -> None:
        """The anchor is the last PUBLISHED tip this clone derived in full (never a local tip)."""
        tip = git(["rev-parse", "-q", "--verify", rref], self.repo.toplevel, check=False).stdout.strip()
        if not tip or tip == self.state().get("anchor"):
            return
        try:
            d = self.derivation(tip)
        except TeamError:
            return
        if d.judged == "full":
            self.save_state(anchor=tip)
            self._dcache = None

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
        cp = git(["rev-parse", "-q", "--verify", f"{self.ref}:ledger"], self.repo.toplevel, check=False)
        return cp.stdout.strip() or "empty"

    def read_canon(self, rev: str | None = None) -> str | None:
        """The OWNER's canon: PROJECT.md as of the last commit the owner in force signed, never the tip's file."""
        try:
            sha = self.derivation(rev).canon_sha
        except TeamError:
            return None
        return self._show(CANON_FILE, sha) if sha else None

    @staticmethod
    def state_hash(ledger: I.Ledger, team: R.Team) -> str:
        """What the canon is generated from: the non-ack entries and team.toml. Acks are left out (every agent
        retry writes one), so the canon is not called stale by work that cannot change it."""
        ids = sorted(e["id"] for e in ledger.entries if e.get("type") != "ack")
        state = ["v1", ids, R.dump_team(team), sorted(ledger.problems)]
        return hashlib.sha256(json.dumps(state).encode("utf-8")).hexdigest()[:16]

    def _refuse_odd_entry(self, name: str) -> None:
        """A top-level worktree file that a member committed as a directory or gitlink cannot be replaced by a
        rename; say so as a team error instead of failing with a raw OSError."""
        try:
            st = os.lstat(self.wt / name)
        except FileNotFoundError:
            return
        if stat.S_ISDIR(st.st_mode):
            raise TeamError(f"{name} in the team worktree is a directory (it was committed to the ledger branch "
                            f"that way); levain will not replace it. The owner removes it from the ledger branch")

    def _open_ledger_file(self, path: Path) -> int:
        """Open this clone's ledger file for append WITHOUT following a link anywhere under ``ledger/``. Any member
        can push a symlink at another member's file or folder; an open that followed it appended ledger data outside
        the worktree (code L3 r2 codex HIGH, RUN). Every directory component is checked with lstat (created when
        missing), the file is opened O_NOFOLLOW, and anything but a single-link regular file is refused."""
        base = self.wt / "ledger"
        cur = self.wt
        for part in ("ledger", *path.relative_to(base).parts[:-1]):
            cur = cur / part
            try:
                st = os.lstat(cur)
            except FileNotFoundError:
                os.mkdir(cur)
                continue
            if not stat.S_ISDIR(st.st_mode):
                raise TeamError(f"{cur.relative_to(self.wt)} on the ledger is not a plain folder (a link?); refusing to "
                                "write through it: the owner removes it from the ledger branch, then record again")
        try:
            fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        except OSError as exc:
            raise TeamError(f"{path.relative_to(self.wt)} on the ledger cannot be opened as a plain file ({exc}); "
                            "refusing to write through it: the owner removes it from the ledger branch") from None
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            os.close(fd)
            raise TeamError(f"{path.relative_to(self.wt)} on the ledger is not a single-link regular file; refusing "
                            "to write through it: the owner removes it from the ledger branch")
        return fd

    def _replace_plain(self, name: str, text: str) -> None:
        """Write a top-level worktree file as a NEW regular file renamed into place. The ledger branch is written by
        every member, so ``name`` may arrive as a symlink (or a hard link): writing to the path would follow it
        out of the worktree. A rename replaces the directory entry itself, so a link there is replaced, never
        followed. The temp file is made outside the worktree (in the team state directory, the same filesystem)
        under a random name, so a crash leaves nothing git sees and no member can commit the name in advance."""
        self._refuse_odd_entry(name)
        fd, tmp = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=self.base)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fchmod(fh.fileno(), 0o644)
                os.fsync(fh.fileno())
            os.replace(tmp, self.wt / name)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _read_plain(self, name: str) -> str:
        """A top-level worktree file, read only if the entry itself is a regular file: opened without following a
        link and checked on the open descriptor, so a swap between a check and the read cannot redirect it."""
        try:
            fd = os.open(self.wt / name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except OSError as exc:
            raise TeamError(f"{name} in the team worktree cannot be read as a regular file ({exc.strerror}); a link "
                            f"or other entry was committed to the ledger branch, and levain will not follow it") from exc
        with os.fdopen(fd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                raise TeamError(f"{name} in the team worktree is not a regular file (a link or other entry was "
                                f"committed to the ledger branch); levain will not read or write through it")
            return fh.read().decode("utf-8")

    def _write_file(self, name: str, text: str, message: str, push: bool) -> str:
        self.require_joined()
        with self.lock():
            self._recover_dirty()
            self._replace_plain(name, text)
            git(["add", "--", name], self.wt)
            if not git(["diff", "--cached", "--quiet"], self.wt, check=False).returncode:
                return f"{name} unchanged"
            self._commit(message)
        if push and self.remote:
            return self._sync(push=True)
        return f"{name} written locally"

    def write_canon(self, text: str, *, push: bool = True) -> str:
        return self._write_file(CANON_FILE, text, "levain team: consolidate PROJECT.md", push)

    def update_team(self, change, message: str, *, push: bool = True) -> str:
        """Change the COUNTED team (``change(team)``); see ``update_counted``."""
        return self.update_counted(lambda team, ten: change(team), message, push=push)

    def update_counted(self, change, message: str, *, push: bool = True, _check: bool = True) -> str:
        """Apply ``change(team, tenure)`` to the COUNTED state and commit it, signed, under the worktree lock.

        Written from the counted state, never the tip file (§3b): if the tip's team.toml/tenure.toml differ from it
        (an uncounted push), a RESTORE commit comes first, and both carry one ``Levain-Pair``. Each names the last
        counted change as its ``Levain-Base``. Refused while the ledger is frozen (its verdicts are partial).
        """
        from . import tenure as T
        self.require_joined()
        with self.lock():
            self._recover_dirty()
            self._dcache = None
            d = self.derivation()
            if d.judged != "full":
                raise TeamError(f"frozen at {str(d.frozen_at)[:10]} ({d.frozen_why}): this change would not be in "
                                "force; `levain team accept-merge` (on the owner's word) or a host repair comes first")
            team = R.parse_team(R.dump_team(d.team), "team.toml")
            ten = T.parse_tenure(T.dump_tenure(d.tenure), "tenure.toml")
            change(team, ten)
            R.validate_team(team)
            want = {T.TEAM_FILE: R.dump_team(team), T.TENURE_FILE: T.dump_tenure(ten)}
            counted = {T.TEAM_FILE: R.dump_team(d.team), T.TENURE_FILE: T.dump_tenure(d.tenure)}
            if want == counted:
                return "team unchanged"
            pair = secrets.token_hex(6)
            base = d.counted_head
            orig = git(["rev-parse", "HEAD"], self.wt).stdout.strip()
            tip_files = {n: (self._read_plain(n) if os.path.lexists(self.wt / n) else "") for n in want}
            # only the OWNER writes a restore: a member's restore changes nothing it may change, so it would not be a
            # Levain-Base link and would stale the member's own change behind it (L1 1007+19 #3, RAN). A member's change
            # is judged field-wise against the tip file instead; the junk fields it reverts are reported, not counted.
            if tip_files != counted:
                # every writer restores first, so the change is judged against the counted state (code L3 r1 codex:
                # an uncounted tip already equal to the change left nothing to commit). Only the OWNER's restore is a
                # Levain-Base link; a member's is not, so the member's change keeps naming the counted base.
                is_owner = self.own_fingerprint() in T.key_fps(d.tenure, d.team.owner)
                for n, text in counted.items():
                    self._replace_plain(n, text)
                git(["add", "--", *counted], self.wt)
                self._commit(f"levain team: restore the counted team\n\n{T.BASE_TRAILER}: {base}\n"
                             f"{T.PAIR_TRAILER}: {pair}")
                if is_owner:
                    base = git(["rev-parse", "HEAD"], self.wt).stdout.strip()
            for n, text in want.items():
                self._replace_plain(n, text)
            git(["add", "--", *want], self.wt)
            self._commit(f"{message}\n\n{T.BASE_TRAILER}: {base}\n{T.PAIR_TRAILER}: {pair}")
            self._dcache = None
            # every field the change meant to set must COUNT, or nothing is written: a partly refused commit printed
            # success while the derivation dropped the refused field (code L3 r2 codex HIGH: an owner-key revoke
            # removed the key and lost the revocation; complement LOW: `member add --key` with a refused key)
            want_f, had_f = T.flat(team, ten), T.flat(d.team, d.tenure)
            now = self.derivation().state
            refused = sorted(".".join(str(x)[:16] for x in k) for k in set(want_f) | set(had_f)
                             if want_f.get(k) != had_f.get(k) and now.get(k) != want_f.get(k))
            if refused and _check:   # _check=False: a test writing a forged commit past the CLI
                git(["update-ref", "-m", "levain team: roll back a refused change", self.ref, orig], self.wt)
                git(["checkout", "-q", "-f", self.branch], self.wt, timeout=60)
                self._dcache = None
                raise TeamError(f"this change would not count for: {', '.join(refused)}; nothing was written (only "
                                "a key in force for the role that may change a field can change it)")
        out = "team change written locally"
        if push and self.remote:
            out = self._sync(push=True)
            # the sync may have stripped and re-landed (or dropped) the change against a newer remote: the command
            # succeeds only if what it meant to set is in force at the published tip (design §3b; T16 lane A, RUN)
            self._dcache = None
            now = self.derivation().state
            missing = sorted(".".join(str(x)[:16] for x in k) for k in set(want_f) | set(had_f)
                             if want_f.get(k) != had_f.get(k) and now.get(k) != want_f.get(k))
            if missing and _check:
                raise TeamError(f"{out}; but after syncing with the remote this change is NOT in force for: "
                                f"{', '.join(missing)} (a newer change landed first); re-issue it if still wanted")
        self._dcache = None
        return out

    # ---- per-session hook state (local only) -----------------------------------------------------------------

    def _session_path(self, session: str) -> Path:
        return self.base / "sessions" / hashlib.sha256(session.encode("utf-8")).hexdigest()[:32]

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
