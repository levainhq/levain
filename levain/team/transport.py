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
import shlex
import stat
import threading
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from ..launch import NETWORK, child_env
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
_OP = threading.local()     # per thread: (clone base, pid) -> depth of the `op` lock this thread holds
WARNINGS: list[str] = []  # process-wide: things a person must hear that did not stop the operation


class TeamError(RuntimeError):
    """A team operation could not complete. The message is meant for a person to read."""


class JoinIncomplete(TeamError):
    """`join` pinned and persisted the ledger, but a step it was asked to complete (the key confirm, the reconcile)
    did not: the clone IS joined and its state stays; the command still does not report success."""


class InitIncomplete(TeamError):
    """`init` created, attached and pinned the ledger, but its push did not complete: the clone IS initialised and its
    state stays; the command still does not report success. ``outcome`` is what the remote was SEEN to hold:
    "published" (ours is there, only the bookkeeping is pending), "pending" (the branch is absent there: the next sync
    publishes ours), "lost" (another history holds it: ours is never published there) or "unknown" (the remote could
    not be read, which is never taken for absent: code L3 r16 codex 1)."""

    OUTCOMES = ("published", "pending", "lost", "unknown")

    def __init__(self, message: str, *, outcome: str):
        if outcome not in self.OUTCOMES:
            raise ValueError(f"InitIncomplete outcome {outcome!r}")
        super().__init__(message)
        self.outcome = outcome

    @property
    def confirmed(self) -> bool:
        """The remote was SEEN to hold this ledger: only then does the clone's setup (a pack seed, the hooks) run. A
        branch seen absent can still be taken by another init before the next sync (code L3 r17 codex 1 + 2)."""
        return self.outcome == "published"


@dataclass(frozen=True)
class RemoteAbsent:
    """The remote answered, and it has no such branch."""


@dataclass(frozen=True)
class RemoteTip:
    sha: str


@dataclass(frozen=True)
class RemoteUnreadable:
    """The remote could not be asked; nothing is known about the branch (never read as absent)."""
    why: str


def remote_tip(remote: str, ref: str, cwd: Path) -> "RemoteAbsent | RemoteTip | RemoteUnreadable":
    """One ref's advertised tip. `git ls-remote --exit-code` exits 2 for "no matching refs" and 0 for a talk that
    matched; any other status, or a timeout, is a failure to ask (https://git-scm.com/docs/git-ls-remote)."""
    try:
        cp = git(["ls-remote", "--exit-code", remote, ref], cwd, check=False, timeout=60)
    except TeamError as exc:
        return RemoteUnreadable(str(exc))
    if cp.returncode == 2:
        return RemoteAbsent()
    if cp.returncode != 0:
        return RemoteUnreadable(_tail(cp))
    # ls-remote matches a pattern by its tail, so only the row naming exactly this ref counts
    rows = [r.split("\t") for r in cp.stdout.splitlines() if "\t" in r]
    hit = [sha for sha, name in rows if name == ref]
    return RemoteTip(hit[0]) if hit else RemoteAbsent()


class TeamBusy(TeamError):
    """A lock was not acquired in time."""


# Every git call here is levain's own plumbing on a private worktree, so none of them runs the project's hooks: git on
# Linux runs a repository's commit, checkout and reference-transaction hooks on exactly these operations, and a hook that
# fails, or rewrites the index, makes a sync fail or a real entry look like an empty pick.
_NO_HOOKS = ["-c", "core.hooksPath=/dev/null"]
# rerere replays a recorded resolution and can stage it, which makes a conflicting pick look empty. Signing is added by
# the one signing wrapper (GitLedger._sign_cfg): every commit levain writes is signed, replays included.
_REPLAY_CONFIG = ["-c", "rerere.enabled=false", "-c", "rerere.autoupdate=false"]


def _interactive() -> bool:
    """A person is at this command: stdin and stdout are terminals (a hook's or a background sync is not, so it is
    never asked for a touch it cannot see)."""
    try:
        return bool(sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


# `levain team record` (every entry but the edit hook's ack) needs a signature with user presence where the team refuses
# file keys (Phill 10-21 "12a"); the rule and its test seam are tenure's (``presence_required``), judged on read.
PRESENCE_REFUSAL = (
    "refused: a team entry is signed with a key that proves a person was present (Touch ID or a security key), and "
    "{why}. Fix: make a FIDO key (`ssh-keygen -t ecdsa-sk` or `-t ed25519-sk`; Touch ID through an sk provider "
    "works too), add it with `levain team key add <your handle> <its .pub>`, and sign with it "
    "(`levain team join --signing-key <its .pub>`); or the team's owner allows file keys for the whole team with "
    "`levain team file-keys allow` (the line `allow_file_keys = true` in team.toml). Nothing was written.")


def ledger_env(**extra: str) -> dict[str, str]:
    """The environment for levain's local git plumbing on the ledger (signing and verifying included): the allowlist
    plus git's own settings (levain.launch: never a copy of os.environ), the identity and repository-location
    variables scrubbed, and replace objects and grafts off, so no commit reads as another's bytes. ``extra`` is added
    last, after the scrub."""
    env = child_env(prefixes=("GIT_",))
    for k in _SCRUB_ENV:
        env.pop(k, None)
    env.update(LC_ALL="C", GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1", GIT_GRAFT_FILE="/dev/null", **extra)
    return env


def _git_env(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    # The allowlist plus what a fetch or push needs: proxies and CA bundles, git's own settings,
    # and the tokens gh's credential helper reads (levain.launch: never a copy of os.environ).
    env = child_env(*NETWORK, "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN",
                    "GITHUB_ENTERPRISE_TOKEN", prefixes=("GIT_",))
    for k in _SCRUB_ENV:
        env.pop(k, None)
    # GIT_NO_REPLACE_OBJECTS: a `git replace` must never make one ledger commit read as another
    env.update(GIT_TERMINAL_PROMPT="0", LC_ALL="C", GIT_EDITOR="true", GIT_NO_REPLACE_OBJECTS="1",
               GIT_GRAFT_FILE="/dev/null")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")  # never prompt on /dev/tty from a hook
    env.update(extra_env or {})     # a signing call's environment (the FIDO provider, no passphrase prompt)
    return env


def _blobs(cwd: Path, shas: list[str], timeout: float = 120) -> dict[str, bytes]:
    """The exact bytes of each blob, through one ``cat-file --batch`` (text mode would rewrite line endings)."""
    if not shas:
        return {}
    try:
        cp = subprocess.run(["git", *_NO_HOOKS, "cat-file", "--batch"], cwd=str(cwd), env=_git_env(),
                            capture_output=True, input="\n".join(shas).encode() + b"\n", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TeamError(f"git cat-file timed out after {timeout:.0f}s") from None
    except FileNotFoundError:
        raise TeamError("git is not on PATH") from None
    if cp.returncode != 0:
        raise TeamError(f"git cat-file failed: {cp.stderr.decode('utf-8', 'replace').strip()[-200:]}")
    data, out, pos = cp.stdout, {}, 0
    for sha in shas:
        nl = data.index(b"\n", pos)
        head = data[pos:nl].decode("utf-8", "replace").split()
        pos = nl + 1
        if len(head) != 3 or head[0] != sha or head[1] != "blob":
            raise TeamError(f"git cat-file returned {' '.join(head)!r} for {sha[:10]}")
        out[sha] = data[pos:pos + int(head[2])]
        pos += int(head[2]) + 1
    return out


def git(args: list[str], cwd: Path, *, timeout: float = 60, check: bool = True,
        input_text: str | None = None, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = _git_env(extra_env)
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
        self._acache: dict[str, tuple[list[tuple[str, str]], dict[str, str]]] | None = None   # `_appended`, per sync
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
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())   # durable before it replaces: the cockpit's accept marker is read after a crash
            os.replace(tmp, self.state_path)
            try:
                dfd = os.open(self.state_path.parent, os.O_RDONLY)
            except OSError:
                dfd = None  # a directory that cannot be opened to sync: the file itself is synced and in place
            if dfd is not None:
                try:
                    os.fsync(dfd)
                except OSError:
                    pass    # a filesystem that cannot sync a directory still has the file synced
                finally:
                    os.close(dfd)
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
        with self.op():      # a repair is a write: never beside a join that is moving the worktree (code L3 r8 codex 6)
            if self._wt_is_ours():
                return
            git(["worktree", "repair", str(self.wt)], self.repo.toplevel, check=False, timeout=30)
        if not self._wt_is_ours():
            raise TeamError(f"the ledger worktree {self.wt} does not belong to this repository and "
                            "`git worktree repair` could not relink it; run `levain team join --new-device`")

    # ---- locking -----------------------------------------------------------------------------------------

    @contextlib.contextmanager
    def lock(self, *, exclusive: bool = True, timeout: float = 30.0, name: str = "lock", _lower: bool = True):
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
            if not _lower:          # the op lock itself: not one of the locks it is ordered above
                yield
                return
            below = self._below()
            key = self._op_key()
            below[key] = below.get(key, 0) + 1
            try:
                yield
            finally:
                below[key] -= 1
        finally:
            os.close(fd)  # closing the descriptor releases the flock

    @contextlib.contextmanager
    def op(self, *, timeout: float = 150.0):
        """ONE exclusive lock per clone, held for the WHOLE of every operation that changes the pin, the trust state or
        the ledger (init, join, sync, the counted team changes, the clone's own trust acts). Git's frame: a command holds
        index.lock for its whole run, never a lock per step. Code L3 r6 found the per-step construct failing at a new
        site each round (a value read outside the lock that guards its writers: the re-land's decision, init's pin,
        join's tail, its snapshot); inside one held op no other LEDGER-CHANGING operation on this clone interleaves
        (the hook's lock-free bookkeeping, session denials and fetch stamps, is not one).

        Reentrant in the thread that holds it (join -> sync -> re-land -> update_counted nest); any other thread or
        process waits up to ``timeout`` and then gets TeamBusy. Order: op -> net -> worktree -> state, and op() enforces
        its own place in it: a fresh acquisition by a thread that already holds a lower lock of this clone raises,
        loudly, instead of stalling behind itself (code L3 r9 codex 5).

        The depth is keyed by the process id too: a child forked while the op is held does not hold it (it opens a
        fresh lock, which the description it inherited keeps busy, so it is refused, never run unguarded). Levain does
        not fork under the op; r7's at-fork handler for that unrun path was DELETED (code L3 r8 codex 1+2: it was
        already growing guards of its own). Accepted, not guarded (code L3 r9 codex 3): a fork child that outlives its
        parent holds the lock until it exits."""
        held = getattr(_OP, "held", None)
        if held is None:
            held = _OP.held = {}
        key = self._op_key()
        if held.get(key):
            held[key] += 1
            try:
                yield
            finally:
                held[key] -= 1
        else:
            if self._below().get(key):
                raise TeamError("internal: the team operation lock was taken while this thread held one of the clone's "
                                "lower locks (the order is op first); nothing was changed")
            with self.lock(name="op", timeout=timeout, _lower=False):
                held[key] = 1
                try:
                    yield
                finally:
                    held.pop(key, None)

    def _op_key(self) -> tuple[str, int]:
        return os.path.realpath(self.base), os.getpid()

    @staticmethod
    def _below() -> dict:
        """Per thread: (clone base, pid) -> how many of the clone's locks below the op this thread holds."""
        below = getattr(_OP, "below", None)
        if below is None:
            below = _OP.below = {}
        return below

    def _require_op(self) -> None:
        """Fails where a step that decides from state and then writes it runs outside the op (point of use)."""
        if not getattr(_OP, "held", {}).get(self._op_key()):
            raise TeamError("internal: a ledger-changing step ran outside the team operation lock; nothing was changed")

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

    def signing_pubkey(self, key: str | None = None) -> str:
        """The public key line of ``key`` (default: this clone's signing key), a ``key::`` literal or a ``.pub`` file."""
        k = key or self.signing_key
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

    def prove_signing_key(self, key: str | None) -> str:
        """Read, fingerprint and test-sign ``key`` (default: this clone's signing key) BEFORE it is relied on or saved;
        returns its fingerprint. A key that cannot be read or cannot sign here raises, naming the key path (code L3 r5
        codex 3, RUN: `join --signing-key /missing/key.pub` persisted the path, then failed reading it)."""
        from . import signing as S
        k = key or self.signing_key
        line = self.signing_pubkey(k)
        try:
            S.prove_can_sign(k, line)
            return S.fingerprint(line)
        except S.SigningError as exc:
            raise TeamError(f"the signing key {k} cannot be used: {exc}; nothing was changed") from None

    def _sign_cfg(self, key: str | None = None) -> list[str]:
        """The one signing wrapper's git config. Every commit levain writes passes through it. ``key``: an explicit,
        already-proven key not yet saved (init's genesis), else this clone's own."""
        from . import signing as S
        key = key or self.signing_key
        if not key:
            self.signing_pubkey()   # raises the how-to
        return S.sign_config(key)

    def _sign_env(self, key: str | None = None) -> dict[str, str]:
        """The signing environment for the key actually signing: ``key`` when given (init's genesis, before the key is
        saved), else this clone's own. Pass it to every signing git call: a Touch ID key signs only with its provider."""
        from . import signing as S
        return S.signing_env(key or self.signing_key)

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
                          sorted(clone.accepted.items()), sorted(clone.distrust), T._REQUIRE_PRESENCE])
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

    def handle(self, team: R.Team | None = None, *, d=None) -> str | None:
        """This clone's member handle: the member whose keys include this clone's signing key (email as fallback
        only for display, before a key is confirmed). ``d``: a derivation the caller already holds, so a reader that
        draws one snapshot resolves the handle from that same derivation, never from a newer tip."""
        try:
            d = d if d is not None else self.derivation()
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
        return sha, d.team, self.ledger_of(d)

    def ledger(self, team: R.Team | None = None, rev: str | None = None) -> I.Ledger:
        """The ledger as enforced: every line in its chain, enforced only where tenure says so."""
        return self.ledger_of(self.derivation(rev))

    @staticmethod
    def ledger_of(d) -> I.Ledger:
        problems = list(d.problems)
        if d.judged != "full":
            problems.append(f"judged: partial: {d.frozen_why}; verdicts are frozen at {str(d.frozen_at)[:10]}"
                            + (f"; {d.waiting} line(s) after it are waiting" if d.waiting else ""))
        files = [(rel, lines) for rel, lines in sorted(d.files.items())]
        return I.build(files, d.team.owner, problems, unenforced=d.unenforced, authority=d.owner_authority,
                       no_presence=frozenset(h for h, ok in d.line_presence.items() if not ok))

    # ---- the caller-facing surface (S0-b): ledger logic that used to live in callers, moved here whole ------------

    def forget_derivation(self) -> None:
        """Drop the cached derivation (this clone's trust state or the tip changed under it)."""
        self._dcache = None

    def has_remote_ledger(self) -> bool:
        """Whether a remote-tracking strict ledger branch is here."""
        return bool(self.remote and self._has(f"refs/remotes/{self.remote}/{BRANCH}"))

    @property
    def canon_path(self) -> Path:
        """Where the generated canon (PROJECT.md) lives in this clone's private worktree."""
        return self.wt / CANON_FILE

    def export_pos(self, d) -> int:
        """Commits on the walk up to this clone's ``anchor`` (the last PUBLISHED tip it derived in full); with no anchor,
        the walk to the remote-tracking tip, or to the local tip when the clone has no remote. 0 when none resolves."""
        from . import tenure as T
        st = self.state()
        anchor = st.get("anchor") if isinstance(st.get("anchor"), str) and st.get("anchor") else None
        target = anchor
        if target is None:
            if self.remote:
                rref = f"refs/remotes/{self.remote}/{self.branch}"
                target = rref if self._has(rref) else None
            else:
                target = d.tip
        if target is None:
            return 0
        if target in d.walk:
            return d.walk.index(target) + 1
        try:
            top = self.repo.toplevel
            sha = T._git(top, ["rev-parse", "--verify", "-q", target + "^{commit}"]).strip()
            return len(T.effective_walk(T.parents_of(top, sha), sha, self.clone().accepted))
        except T.Unjudgeable:
            return 0

    def location_row(self) -> tuple[bool, str]:
        """The doctor's first row: where this clone's private ledger lives and which device it is."""
        return (True, f"ledger worktree {self.wt} (device {self.device})")

    def publish_rows(self) -> list[tuple[bool, str]]:
        """The doctor's publishing rows: the last fetch and what is not pushed, or that the ledger is local only."""
        from datetime import datetime, timezone

        def iso(ts) -> str:
            try:
                return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except (TypeError, ValueError):
                return ""
        rows: list[tuple[bool, str]] = []
        st = self.state()
        if self.remote:
            err = st.get("last_fetch_error")
            rows.append((not err, f"remote {self.remote}: last fetch "
                         + (I.age(iso(st.get('last_fetch_ok'))) if st.get("last_fetch_ok") else "never")
                         + (f"; last error: {err}" if err else "")))
            ahead = subprocess.run(["git", *_NO_HOOKS, "log", "--format=%s",
                                    f"refs/remotes/{self.remote}/{self.branch}..HEAD"],
                                   cwd=self.wt, capture_output=True, text=True, env=ledger_env())
            subjects = ahead.stdout.splitlines() if ahead.returncode == 0 else ["?"]
            real = [s for s in subjects if not s.startswith("levain team: ack ")]
            rows.append((not real, f"{len(subjects)} local ledger commit(s) not pushed"
                         + ("" if not subjects else (": run `levain team sync`" if real
                            else " (acknowledgements only; they go out with the next write)"))))
        else:
            rows.append((True, "no remote: ledger is local only"))
        return rows

    def ledger_label(self) -> str:
        """The ``ledger <branch>`` part of the first status line."""
        return f"ledger {self.branch}"

    def freshness_lines(self) -> list[str]:
        """The status line on how fresh this clone's fetch is (none when the ledger is local only)."""
        if not self.remote:
            return []
        last = self.state().get("last_fetch_ok")
        age = (time.time() - float(last)) if last else None
        newest = git(["log", "-1", "--format=%cr", self.ref], self.repo.toplevel, check=False).stdout.strip()
        return [f"last fetched {('%d min ago' % (age // 60)) if age is not None else 'never'}; newest ledger "
                f"commit {newest or 'unknown'}. A host can withhold newer commits without any signal: compare "
                "the tip id with a teammate if anything looks stale"]

    def foreign_ledger_lines(self, d) -> list[str]:
        """Status lines on other ledgers on the remote: a 0.6.x branch, and branches claiming to re-genesis this team."""
        from . import tenure as T
        out: list[str] = []
        if not self.remote:
            return out
        refs = git(["for-each-ref", "--format=%(refname)", f"refs/remotes/{self.remote}/"], self.repo.toplevel,
                   check=False).stdout.split()
        if f"refs/remotes/{self.remote}/{LEGACY_BRANCH}" in refs:
            # T46, RUN: a 0.6.x ledger beside the strict one was never mentioned
            out.append(f"a 0.6.x {LEGACY_BRANCH} branch was on {self.remote} at this clone's last fetch: this levain never "
                       "reads it (nothing in it is enforced); the owner deletes it with `levain team retire-legacy`")
        for ref in refs:
            name = ref.rsplit("/", 1)[-1]
            if not name.startswith(BRANCH + "-") or name == self.branch:
                continue
            roots = git(["rev-list", "--max-parents=0", ref], self.repo.toplevel, check=False).stdout.split()
            ten = self._show(T.TENURE_FILE, roots[0]) if roots else None
            try:
                rt = T.parse_tenure(ten or "", "x")
                rteam = R.parse_team(self._show(T.TEAM_FILE, roots[0]) or "", "x")
                prior, rowner = rt.prior, rteam.owner
                rfps = ", ".join(sorted(T.key_fps(rt, rowner))) or "none"
            except Exception:  # noqa: BLE001 - a display line; a foreign ledger's bad file is not this clone's problem
                prior = None
            if prior and prior.get("root") == d.walk[0]:
                # anyone who can push can make one, so the line names its owner and keys and claims nothing for it
                # (docs L3 r1 anansi: an unqualified "to move" line advertised whatever branch was pushed)
                out.append(f"a branch {name} claims to RE-GENESIS this team, owner {rowner} (keys {rfps}); UNVERIFIED: "
                           f"move only if your owner confirms those keys out of band "
                           f"(`levain team join --root {name.rsplit('-', 1)[-1]}`)")
        return out

    def nested_genesis(self, files: dict[str, str], message: str) -> str:
        """A signed parentless commit whose tree holds ``files`` (paths may nest)."""
        top = self.repo.toplevel
        idx = self.base / "regenesis.index"
        env = ledger_env(GIT_INDEX_FILE=str(idx))
        try:
            idx.unlink()
        except FileNotFoundError:
            pass
        for path, text in sorted(files.items()):
            blob = git(["hash-object", "-w", "--stdin"], top, input_text=text).stdout.strip()
            subprocess.run(["git", *_NO_HOOKS, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}"],
                           cwd=str(top), env=env, check=True, capture_output=True)
        tree = subprocess.run(["git", *_NO_HOOKS, "write-tree"], cwd=str(top), env=env, check=True,
                              capture_output=True, text=True).stdout.strip()
        idx.unlink(missing_ok=True)
        cp = subprocess.run(["git", *_NO_HOOKS, *self._sign_cfg(), "commit-tree", "-S", tree, "-m", message],
                            cwd=str(top), capture_output=True, text=True, env={**env, **self._sign_env()},
                            stdin=subprocess.DEVNULL, timeout=60)
        if cp.returncode != 0:
            raise TeamError(f"could not sign the re-genesis: {(cp.stderr or cp.stdout).strip()[-200:]}")
        return cp.stdout.strip()

    def team_history_problems(self, team: R.Team, rev: str | None = None) -> list[str]:
        """Kept for callers: on a strict ledger these are part of the derivation's own problems."""
        return []

    def file_for(self, author: str) -> Path:
        return self.wt / "ledger" / E.safe_handle(author) / f"{self.device}.jsonl"

    # ---- setup ---------------------------------------------------------------------------------------------

    def _remote_has_branch(self, remote: str) -> bool:
        cp = git(["ls-remote", "--heads", remote, f"refs/heads/{self.branch}"], self.repo.toplevel, timeout=60)
        return bool(cp.stdout.strip())

    def has_local_ledger(self) -> bool:
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

    def _genesis_commit(self, tree_text: dict[str, str], message: str, *, key: str | None = None) -> str:
        top = self.repo.toplevel
        rows = []
        for name, text in sorted(tree_text.items()):
            blob = git(["hash-object", "-w", "--stdin"], top, input_text=text).stdout.strip()
            rows.append(f"100644 blob {blob}\t{name}")
        tree = git(["mktree"], top, input_text="\n".join(rows) + "\n").stdout.strip()
        cp = subprocess.run(["git", *_NO_HOOKS, *self._sign_cfg(key), "commit-tree", "-S", tree, "-m", message],
                            cwd=str(top), capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                            env=ledger_env(**self._sign_env(key)))
        if cp.returncode != 0:
            raise TeamError(f"could not sign the genesis commit: {(cp.stderr or cp.stdout).strip()[-200:]}")
        return cp.stdout.strip()

    def init(self, team: R.Team, *, member_keys: dict[str, str] | None = None, remote: str | None = None,
             push: bool = True, signing_key: str | None = None, replace_legacy: bool = False,
             anneal_db: str | None = None) -> str:
        with self.op():
            return self._init_in_op(team, member_keys=member_keys, remote=remote, push=push, signing_key=signing_key,
                                    replace_legacy=replace_legacy, anneal_db=anneal_db)

    def _init_in_op(self, team: R.Team, *, member_keys: dict[str, str] | None, remote: str | None, push: bool,
                    signing_key: str | None, replace_legacy: bool, anneal_db: str | None) -> str:
        """Create a STRICT ledger: a genesis carrying team.toml and tenure.toml, signed by the owner's key (this
        clone's), pinned here in the same operation. Members' keys are pending until each confirms from their own
        machine (`levain team join`). Returns a status line.

        Join's frame (code L3 r4), applied whole: everything is validated and every local step that can fail runs
        BEFORE the clone's state is saved, which is the last local step and one save; nothing is ever rolled back and
        nothing resumes. A roll-back (r7-r9) and then a resume record (r10-r13) each drew new findings every round
        (spore-813), so both are DELETED: a stop before the save names the exact cleanup; a stop after it is only the
        push, which `levain team sync` finishes."""
        from . import signing as S
        from . import tenure as T
        R.validate_team(team)
        # the key is resolved ONCE and that value proves, fingerprints, signs and is saved (code L3 r10 codex 1, r11
        # codex 3 + complement 5); proven and every refusal checked BEFORE anything is written (code L3 r6 codex 2/6)
        key = signing_key or self.signing_key
        if not key:
            self.signing_pubkey()           # raises the how-to
        if not key.startswith("key::"):
            # normalised FIRST, then its public key line read ONCE: that line is proved, fingerprinted and granted
            # (code L3 r14 codex 2: the file was read by the proof and again for the grant, so a swap between the two
            # granted an unproved key), and saved absolute (r12 complement 6)
            key = str(Path(os.path.expanduser(key)).resolve())
        own = self.signing_pubkey(key)
        try:
            S.prove_can_sign(key, own)
            fp = S.fingerprint(own)
        except S.SigningError as exc:
            raise TeamError(f"the signing key {key} cannot be used: {exc}; nothing was changed") from None
        remote = remote or self._default_remote()
        if self.legacy_only() and not replace_legacy:
            raise TeamError(LEGACY_MESSAGE)
        branch = self.branch
        if self.has_local_ledger():
            if self.pinned_root:
                # a real ledger this clone trusts, never "left from an init": no delete advice (r14 complement 2)
                raise TeamError(f"branch {branch} already exists here and this clone is pinned to it: "
                                + ("`levain team sync` publishes it" if self.joined() else
                                   "`levain team join` re-attaches its checkout" if self.remote else
                                   # local-only: join needs a remote (r15 codex 4)
                                   self._reattach_advice(branch)))
            raise TeamError(f"branch {branch} already exists here: use `levain team join`; if it is left from an init "
                            f"that stopped (nothing of it published), remove it first: {self._init_cleanup(branch)}")
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
                                      f"levain team: init strict ledger for {team.project}", key=key)
        # a create-only ref: if another writer made it first this fails and nothing of theirs is touched (r9 codex 2)
        git(["update-ref", self.ref, commit, ""], self.repo.toplevel)
        try:
            # judged on a CANDIDATE trust state, as join judges, so no pin is saved to judge it
            cand = T.Clone(pinned_root=commit, anchor=None, accepted={}, distrust=set())
            try:
                T.derive(self.repo.toplevel, commit, cand, S.SigCache(self.base / "sigcache.json"))
            except T.Unjudgeable as exc:
                raise TeamError(f"the new genesis cannot be judged: {exc}") from None
            self._attach_worktree()
            device = self._new_device()

            def persist(st: dict) -> None:
                st.update(device=device, remote=remote or "", branch=branch, pinned_root=commit, anchor=None,
                          accepted={}, distrust=[], signing_key=key)
                st["own_keys"] = list(dict.fromkeys([*(st.get("own_keys") or []), fp]))
                if anneal_db:
                    st["anneal_db"] = anneal_db     # in the one save: no later setup stage to retry (r15 codex 5)
            # the save is the last step under the wrap (r14 codex 4 + complement 1): save_state's os.replace is its
            # last write, so a raise from it means nothing was saved
            self.save_state(_mutate=persist)
        except Exception as exc:  # noqa: BLE001 - reported with the exact cleanup, never rolled back
            raise TeamError(f"init stopped after creating branch {branch} (genesis {commit[:12]}); nothing was saved on "
                            f"this clone ({exc}). To start again: {self._init_cleanup(branch)}, then run "
                            "`levain team init` again") from exc
        self._dcache = None
        if remote and push:
            # the genesis is published by a plain push, which the remote refuses when another team's ledger got there
            # first (unrelated history is never a fast-forward); a sync would fetch theirs and rebase onto it, and a
            # "run sync" would then never work (code L3 r14 codex 1 + complement 3)
            try:
                cp = git(["push", "--porcelain", remote, f"{commit}:refs/heads/{branch}"], self.repo.toplevel,
                         check=False, timeout=120)
                why = "" if cp.returncode == 0 else _tail(cp)
            except TeamError as exc:      # a timeout or no git: still a failed push, never a raw error (r15 cpl 1)
                why = str(exc)
            if why:
                outcome, said = self._publish_outcome(remote, branch, commit)
                if outcome == "lost":
                    raise InitIncomplete(f"initialised {branch} (genesis {commit[:12]}) on this clone, but it cannot be "
                                         f"published: {remote} {said}", outcome="lost")
                if outcome == "unknown":
                    # RUN (r17 R-D, r19): sync refuses a lost race, and after the drop a bare join refuses too (the pin
                    # still names this genesis) but lists the winner, so the --root form is named
                    raise InitIncomplete(f"initialised {branch} (genesis {commit[:12]}) on this clone, but the push to "
                                         f"{remote} failed ({why}) and {said}, so whether it was published is not "
                                         "known: run `levain team sync` once the remote answers. It publishes this "
                                         "ledger, or refuses because another got there first; then drop "
                                         f"this one: {self._init_cleanup(branch)}, then `levain team join --root <its "
                                         "genesis>` (a bare `levain team join` lists it)", outcome="unknown")
                if outcome == "pending":
                    # absent when probed is not absent at the sync: another init can still take it (r18 codex 2)
                    raise InitIncomplete(f"initialised {branch} (genesis {commit[:12]}) on this clone, but the push to "
                                         f"{remote} failed ({why}): run `levain team sync`. If it refuses because "
                                         f"another ledger got there first, drop this one: {self._init_cleanup(branch)}, "
                                         "then `levain team join --root <its genesis>` (a bare `levain team join` lists "
                                         "it)", outcome="pending")
                # "published": the push landed (its report was lost), or a teammate has already built on it
            try:
                self._sync(push=True)      # the clone's own bookkeeping of what it published
            except Exception as exc:  # noqa: BLE001 - published; only the bookkeeping is pending
                raise InitIncomplete(f"initialised {branch} (genesis {commit[:12]}) and pushed it to {remote}, but the "
                                     f"follow-up sync failed ({exc}): run `levain team sync`",
                                     outcome="published") from exc
            return f"strict ledger created and pushed to {remote}/{branch} (genesis {commit[:12]})"
        return f"strict ledger created locally ({'no remote' if not remote else 'not pushed'}; genesis {commit[:12]})"

    def _publish_outcome(self, remote: str, branch: str, commit: str) -> tuple[str, str]:
        """After a push that reported failure, what the remote holds: decided on the advertised tip and, for a tip that
        is not our genesis, on ANCESTRY in what was fetched (git settles a create-only race on the server and gives the
        client no outcome after a transport failure, so the client re-reads; https://git-scm.com/docs/git-push).
        Returns (outcome, what was seen); a read that fails is "unknown", never "pending" (code L3 r16 codex 1)."""
        from . import signing as S
        from . import tenure as T
        ref = f"refs/heads/{branch}"
        seen = remote_tip(remote, ref, self.repo.toplevel)
        if isinstance(seen, RemoteUnreadable):
            return "unknown", f"its branch could not be read ({seen.why})"
        if isinstance(seen, RemoteAbsent):
            return "pending", "has no such branch"
        if seen.sha == commit:
            return "published", "holds this genesis"
        # a tip that is not ours may still DESCEND from ours: a teammate built on it after our push landed unreported
        # (code L3 r16 codex MED + complement 2), so it is fetched and judged on ancestry, never on the sha alone
        scratch = f"refs/levain-probe/{branch}"
        try:
            got = git(["fetch", "-q", "--no-tags", remote, f"+{ref}:{scratch}"], self.repo.toplevel, check=False,
                      timeout=120)
            if got.returncode != 0:
                return "unknown", f"its branch could not be fetched ({_tail(got)})"
            anc = git(["merge-base", "--is-ancestor", commit, scratch], self.repo.toplevel, check=False, timeout=60)
            if anc.returncode == 0:
                return "published", "holds this genesis and later entries"
            if anc.returncode != 1:
                return "unknown", f"its branch could not be compared ({_tail(anc)})"
            tip = git(["rev-parse", scratch], self.repo.toplevel, timeout=10).stdout.strip()
            roots = git(["rev-list", "--max-parents=0", scratch], self.repo.toplevel, check=False,
                        timeout=60).stdout.split()
            # joinable only as join itself would judge it: a candidate pinned at its one root, derived (code L3 r17
            # codex 3: a root that merely carries team.toml sent the person to delete theirs for a join that refuses)
            # the outcome is already decided ("lost": its history does not contain our genesis); this only decides
            # whether the drop-then-join advice is safe, so anything short of a FULL judgment withholds it (r18 codex 1
            # + complement 2: an unaccepted merge derives "partial" without raising)
            why = "it has more than one root" if len(roots) != 1 else ""
            if not why:
                # the try holds the temp dir too: failing to create it is a failure to judge (r19 codex 2 + complement
                # 1); failing to delete it is not, the judgment being complete by then (r20 codex: so it is ignored); a
                # foreign ledger's verdicts stay out of this clone's cache (r18 complement 3)
                try:
                    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
                        d = T.derive(self.repo.toplevel, tip, T.Clone(pinned_root=roots[0], anchor=None, accepted={},
                                                                      distrust=set()), S.SigCache(Path(tmp) / "c.json"))
                    if d.judged != "full":
                        why = f"its history is frozen: {d.frozen_why}"
                except Exception as exc:  # noqa: BLE001 - not judged in full: no destructive advice
                    why = f"it could not be judged as a team ledger here: {exc}"
        except TeamError as exc:
            return "unknown", f"its branch could not be fetched ({exc})"
        finally:
            # best effort: a leaked probe ref is overwritten by the next probe, and a raise here must never replace
            # the outcome (code L3 r17 complement 1 + codex 4)
            with contextlib.suppress(Exception):
                git(["update-ref", "-d", scratch], self.repo.toplevel, check=False, timeout=10)
        drop = f"drop this unpublished one: {self._init_cleanup(branch)}"
        if not why:
            # RUN (r14): `join --root` alone refuses, the unpublished local branch having "diverged"
            return "lost", (f"received another team ledger first (genesis {roots[0][:12]}). To join that one instead, "
                            f"{drop}, then `levain team join --root {roots[0][:12]}`")
        # join would refuse it as it stands, so no join is advised (code L3 r16 complement 2, r17 codex 3)
        return "lost", (f"holds another history on {branch} (tip {tip[:12]}; {why}). Settle what {ref} on {remote} "
                        f"should be before dropping anything; to start again on this remote afterwards, {drop}, and "
                        "run `levain team init` again")

    def _reattach_advice(self, branch: str) -> str:
        """For a local-only pinned clone that is not joined (its checkout is missing or on another branch):
        _attach_worktree's own sequence, which first removes a checkout that exists on another branch (code L3 r16
        codex MED + complement 1: a bare `worktree add` fails whenever the checkout exists)."""
        top, q = shlex.quote(str(self.repo.toplevel)), shlex.quote
        return (f"re-attach its checkout (levain's private one: anything uncommitted in it is discarded) with "
                f"`git -C {top} worktree remove --force --force {q(str(self.wt))}` (if it is listed), "
                f"`git -C {top} worktree prune` and `git -C {top} worktree add --lock {q(str(self.wt))} {q(branch)}`")

    def _init_cleanup(self, branch: str) -> str:
        """Commands that run from any directory, quoted (code L3 r14 codex 5 + complement 2)."""
        top, q = shlex.quote(str(self.repo.toplevel)), shlex.quote
        return (f"`git -C {top} worktree remove --force --force {q(str(self.wt))}` (if it is listed), "
                f"`git -C {top} worktree prune` and `git -C {top} branch -D {q(branch)}`")

    def join(self, *, remote: str | None = None, new_device: bool = False, root: str | None = None,
             signing_key: str | None = None, accept_merges: dict[str, int] | None = None) -> str:
        # the WHOLE join, its post-pin reconcile and key confirm included, is one op (code L3 r6 complement 1-3, codex
        # 3: the tail ran outside every lock with values captured before it, and the snapshot predated the wait)
        with self.op():
            return self._join_in_op(remote=remote, new_device=new_device, root=root, signing_key=signing_key,
                                    accept_merges=accept_merges)

    def _join_in_op(self, *, remote: str | None, new_device: bool, root: str | None, signing_key: str | None,
                    accept_merges: dict[str, int] | None) -> str:
        """Pin this clone to a strict ledger on the remote (trust on first use: the genesis, its owner in force and
        her key fingerprints are printed for the person to check out of band), then confirm this clone's key."""
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
        multi: dict[str, list[str]] = {}
        for name in names:
            git(["fetch", "-q", remote, f"+refs/heads/{name}:refs/remotes/{remote}/{name}"], self.repo.toplevel,
                timeout=120)
            roots = git(["rev-list", "--max-parents=0", f"refs/remotes/{remote}/{name}"], self.repo.toplevel,
                        timeout=60).stdout.split()
            if len(roots) == 1:
                found[name] = roots[0]
            elif roots:
                multi[name] = roots     # a merge brought in a second root: only an explicit --root may choose (T10)
        # ONE snapshot of the clone's trust state: everything below is judged on it, and the persist is a
        # compare-and-swap against it (code L3 r5 codex 4 + complement 3)
        old = self.state()
        pinned = old.get("pinned_root") or ""
        if root:
            # a branch with two roots is joinable only by naming the genesis: the derivation below must then reach it
            # through the parents accepted with --accept-merge, or nothing is pinned (T10, RUN: no fresh clone could
            # join a ledger after an orphan-first-parent push, whatever it named)
            for n, rs in multi.items():
                hit = [r for r in rs if r.startswith(root)]
                if len(hit) == 1:
                    found[n] = hit[0]
            pick = [n for n, r in found.items() if r.startswith(root)]
            if len(pick) != 1:
                raise TeamError(f"no single team ledger on {remote} has genesis {root}")
            name = pick[0]
        elif pinned and pinned in found.values():
            name = next(n for n, r in found.items() if r == pinned)      # a re-join stays on the ledger it trusts
        elif pinned:
            # a pinned clone never moves to another genesis by itself: that would be trust on first use a second time,
            # silently, on whatever the host now serves (code L3 r2 complement MED, RUN)
            listing = "; ".join(f"{n}: genesis {r[:12]}" for n, r in sorted(found.items()))
            raise TeamError(f"this clone is pinned to genesis {pinned[:12]}, which {remote} no longer has ({listing}). "
                            "Nothing was changed. Ask the owner out of band; to move to a re-genesis they name, run "
                            "`levain team join --root <its genesis>`")
        elif len(found) == 1:
            name = next(iter(found))
        else:
            listing = "; ".join(f"{n}: genesis {r[:12]}" for n, r in sorted(found.items()))
            raise TeamError(f"{remote} has {len(found)} team ledgers ({listing}). Choose the team to trust with "
                            "`levain team join --root <genesis>` (a re-genesis lives beside the ledger it replaced)")
        rref = f"refs/remotes/{remote}/{name}"
        # abbreviated ids resolved after the fetch: the linearity check compares full ids, so a short one never matched
        # and `join` refused the very merge it named (docs L3 r2 anansi LOW)
        resolved: dict[str, int] = {}
        for sha, n in (accept_merges or {}).items():
            full = git(["rev-parse", "--verify", "-q", sha + "^{commit}"], self.repo.toplevel, check=False).stdout.strip()
            resolved[full or sha] = n
        accept_merges = resolved
        keep = bool(pinned) and found[name] == pinned
        moved = bool(pinned) and not keep
        # a same-ledger re-join keeps what this clone decided: its accepted merges and its anchor (resetting the anchor
        # judged a host-rewritten history in full and lifted the freeze that only `repin` may lift: docs L3 r1 anansi
        # HIGH, RUN). The distrust list survives every join: it is the person's own judgement (`distrust --clear`).
        accepted = {**((old.get("accepted") or {}) if keep else {}), **(accept_merges or {})}
        for sha, n in list((accept_merges or {}).items()):
            ps = git(["rev-list", "--parents", "-n", "1", sha], self.repo.toplevel, check=False).stdout.split()[1:]
            if len(ps) < 2 or not 1 <= int(n) <= len(ps):
                raise TeamError(f"--accept-merge {sha[:10]}:{n} does not name a merge and one of its parents")
        merges = git(["rev-list", "--first-parent", "--merges", rref], self.repo.toplevel, timeout=60).stdout.split()
        unaccepted = [m for m in merges if m not in accepted]
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
        # a re-join on the ledger this clone already trusts keeps its unpublished work: that branch is AHEAD-and-behind
        # by design and sync reconciles it (residue run 1008, RUN: a key rotation by `join --signing-key` refused as
        # "diverged" and, the key already saved, said "nothing was changed")
        if not keep and git(["rev-parse", "-q", "--verify", f"refs/heads/{name}"], self.repo.toplevel,
                            check=False).returncode == 0:
            here = git(["rev-parse", f"refs/heads/{name}"], self.repo.toplevel).stdout.strip()
            if git(["merge-base", "--is-ancestor", here, tip0], self.repo.toplevel, check=False).returncode != 0 and \
                    git(["merge-base", "--is-ancestor", tip0, here], self.repo.toplevel, check=False).returncode != 0:
                raise TeamError(f"the local {name} branch has diverged from {remote}'s; nothing was changed")
        # The proposed signing key is read, fingerprinted and made to sign a nonce BEFORE anything is persisted (code L3
        # r5 codex 3, RUN: a missing key's path was saved, then failed reading; a key that cannot sign here joined)
        self.prove_signing_key(signing_key)
        # PROSPECTIVE: the chosen ledger is judged on a candidate trust state, and the destination worktree checked,
        # before anything is written. Nothing is rolled back afterwards, because nothing is written before both pass
        # (code L3 r3 codex 6 + r4 codex/complement: a roll-back restored the state file but clobbered a concurrent
        # `distrust` and left branch, seen-ref and worktree changes behind, once deleting the working ledger's worktree)
        from . import signing as S
        from . import tenure as T
        anchor = (old.get("anchor") or None) if keep else None
        cand = T.Clone(pinned_root=found[name], anchor=anchor, accepted={k: int(v) for k, v in accepted.items()},
                       distrust=set(old.get("distrust") or []))
        # the net lock, then the worktree lock (sync's order), are held from this validation through the worktree
        # attach: no sync or writer runs against a half-moved pin (code L3 r5 codex 1), and a sync reads the ledger it
        # syncs only under net, so none keeps the pin it read before this move (seat ruling, r5 lane item 1)
        with self.lock(name="net", timeout=150.0), self.lock():
            try:
                d = T.derive(self.repo.toplevel, tip0, cand, S.SigCache(self.base / "sigcache.json"))
            except T.Unjudgeable as exc:
                raise TeamError(f"cannot judge the team ledger on this clone: {exc}; nothing was changed") from None
            if (self.wt / ".git").exists():
                # a COPIED clone lists its own private worktree under the original's path until git relinks it (a
                # repair, not a change: it touches no branch or state)
                self._repair_if_moved()
            wt_real = os.path.realpath(self.wt)
            path = ""
            for row in git(["worktree", "list", "--porcelain"], self.repo.toplevel, check=False).stdout.splitlines():
                if row.startswith("worktree "):
                    path = row[len("worktree "):]
                elif row == f"branch refs/heads/{name}" and os.path.realpath(path) != wt_real:
                    raise TeamError(f"{name} is checked out in another worktree ({path}); levain keeps its own private "
                                    "checkout of the ledger. Nothing was changed: remove that worktree, then join again")
            device = secrets.token_hex(8) if new_device else self._new_device()
            # a re-join advances the anchor only as a sync would (_advance_anchor); it is written in the same CAS
            new_anchor = tip0 if (not keep or d.judged == "full") else anchor
            seen = {k: old.get(k) for k in ("pinned_root", "anchor", "accepted")}

            def persist(st: dict) -> None:
                # ONE write, under the state lock every trust-state change takes (distrust, accept-merge, repin and the
                # key commands all write through save_state), of the join's own fields only: a concurrent `distrust`
                # stays. A compare-and-swap: if the pin, anchor or accepted merges are no longer what was validated
                # above, nothing is written (code L3 r5 codex 4 + complement 3: a stale snapshot erased a completed
                # `accept-merge` / `repin --anchor`)
                if {k: st.get(k) for k in seen} != seen:
                    raise TeamError("another team operation changed the pin state (pin, anchor or accepted merges) "
                                    "during this join; nothing was changed by the join: run `levain team join` again")
                st.update(branch=name, pinned_root=found[name], anchor=new_anchor, accepted=accepted, remote=remote,
                          device=device)
                if signing_key:
                    st["signing_key"] = signing_key
            self.save_state(_mutate=persist)
            self._dcache = None
            tip = tip0
            # persisted: a failure from here keeps the new state and says what is still needed, never a plain
            # "failed" (code L3 r5 complement 8)
            try:
                if not self.has_local_ledger():
                    git(["branch", name, rref], self.repo.toplevel)
                self._record_seen_sha(tip)       # the joined tip was published: never movable (code L3 r1 codex HIGH)
                self._remember_own_key()
                self._attach_worktree()
            except TeamError as exc:
                raise JoinIncomplete(f"joined and pinned to {name} (genesis {found[name][:12]}), still needs: {exc}; "
                                     "run `levain team join` again to finish it") from exc
        # from here the clone IS joined: a later failure (a slow remote, a refused push) keeps the new state and says
        # what is still pending; nothing rolls back
        pending: list[str] = []
        confirm_failed = False
        said = len(self.warnings)
        if keep:
            # the re-join reconciles as a sync would: own unpublished commits go on top of the remote (held when this
            # machine's key does not count yet), so the confirm below lands on the remote's line (residue run 1008)
            try:
                self._rebase(rref, 120, 30.0)
            except TeamError as exc:
                pending.append(f"reconciling this clone's unpublished commits ({exc}): `levain team sync`")
            self._dcache = None
        owner = d.team.owner
        fps = ", ".join(sorted(T.key_fps(d.tenure, owner))) or "none"
        tofu = (f"TEAM LEDGER {name}: genesis {found[name]}, owner in force {owner} (keys {fps}). Check these with "
                "the owner out of band before relying on it.")
        if git(["ls-remote", "--heads", remote, f"refs/heads/{LEGACY_BRANCH}"], self.repo.toplevel, check=False,
               timeout=60).stdout.strip():
            tofu += (f"\n{remote} also has a 0.6.x {LEGACY_BRANCH} branch: this levain never reads it (nothing in it "
                     "is enforced); the owner deletes it with `levain team retire-legacy`")
        if moved:
            tofu = f"re-pinned from genesis {pinned[:12]} to {found[name][:12]} on your --root.\n" + tofu
        try:
            confirmed = self.confirm_own_key(d)
        except TeamError as exc:
            confirmed = False
            confirm_failed = True
            pending.append(f"confirming this machine's key ({exc}): `levain team key confirm`")
        if not git(["for-each-ref", "--count=1", self._held_ref(found[name])], self.repo.toplevel,
                   check=False).stdout.strip():
            # the confirm above replayed what the rebase held: its "kept back, confirm the key" line is no longer true
            self.warnings[said:] = [w for w in self.warnings[said:] if "refs/levain/held/" not in w]
        left = self.other_genesis_items() if moved else ""
        if left:
            self.warnings.append(left)
        handle = self.handle(d.team)
        if handle is None:
            fp = self.own_fingerprint()
            out = (tofu + f"\njoined {d.team.project}, but this machine's key ({fp}) is not a member's key yet: from "
                   "one of your machines whose key is in force, run `levain team key add <your handle> <this public "
                   f"key>`; if you have none yet, {owner} proposes your first key (`levain team key add <your "
                   "handle> <your public key>`); then `levain team sync`")
            if not confirm_failed:
                # pinned, but no line this clone signs will count: not a success (code L3 r5 complement 2, the r4
                # ruling "a join that persisted but did not complete exits non-zero")
                pending.append(f"an owner must add this machine's key: {fp}")
        else:
            out = tofu + f"\njoined {d.team.project} as {handle} (device {self.device})" + (
                f"; confirmed this machine's key" if confirmed else "")
        if pending:
            # joined, and the state stays; but a part it was asked to do did not complete, so it is NOT reported as a
            # success (the CLI exits 2, as for an entry recorded locally whose push failed)
            raise JoinIncomplete(out + "\n" + "\n".join(f"joined and pinned to {name}, but this is still pending: {p}"
                                                          for p in pending))
        return out

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
        written; only its commit was lost). On a FIDO clone, a file holding a line the team needs a touch for is
        committed only where a person is at the command, after each such entry is named; elsewhere it is left
        uncommitted and the command stops, naming them (it is never swept into a later commit or discarded by a later
        checkout). An uncommitted team.toml, tenure.toml or PROJECT.md is discarded: those are regenerated by re-running
        the command that failed. Anything else is refused for a person to inspect.
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
        if not mine:
            return
        # the commit is the entry's presence (H-6): an entry the team needs a touch for is named first, as `record`
        # names it, and signed only where a person can see that line; never under an unnamed recovery touch
        touch = self._dirty_needing_touch(mine)
        if touch and not _interactive():
            rest = [p for p in mine if p not in touch]
            if rest:
                git(["add", "--", *rest], self.wt)
                self._commit("levain team: recover an interrupted write")
            names = [f"{t} {i}" + (f": {s}" if s else "") for p in touch for t, i, s in touch[p]]
            raise TeamError(f"an interrupted write left {len(names)} entr{'y' if len(names) == 1 else 'ies'} "
                            "uncommitted that the team needs a touch for (" + "; ".join(names[:5])
                            + (" ..." if len(names) > 5 else "") + "), and this command is not interactive, so it "
                            "asked for none and committed nothing for them; they stay in the worktree "
                            f"({self.wt}). Run `levain team sync` in a terminal: it names each entry and asks one "
                            "touch for them")
        for p in touch:
            for t, i, s in touch[p]:
                print(f"levain team: touch your security key or confirm with Touch ID to sign {t} {i}: {s}",
                      file=sys.stderr, flush=True)
        git(["add", "--", *mine], self.wt)
        self._commit("levain team: recover an interrupted write")

    def _dirty_needing_touch(self, paths: list[str]) -> dict[str, list[tuple[str, str, str]]]:
        """For a clone that signs with a FIDO key: {path: [(type, id, summary)]} of each uncommitted line in ``paths``
        whose type the team needs a touch for, by the counted policy at HEAD (empty for any other key: its commit is
        judged on read as before). A whole file is listed: its other lines chain over these, so they wait with them."""
        if self._presence_key_problem() is not None:
            return {}
        from . import tenure as T
        self._dcache = None
        allow = self.derivation().team.allow_file_keys
        out: dict[str, list[tuple[str, str, str]]] = {}
        for p in paths:
            cp = git(["show", f"HEAD:{p}"], self.wt, check=False)
            committed = set(cp.stdout.splitlines()) if cp.returncode == 0 else set()
            try:
                now = (self.wt / p).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for ln in now:
                o = T._parsed(ln) if ln.strip() and ln not in committed else {}
                if o.get("type") and T.presence_required(allow, o.get("type")):
                    summary = (str(o.get("words") or o.get("summary") or "").splitlines() or [""])[0][:80]
                    out.setdefault(p, []).append((str(o.get("type")), str(o.get("id")), summary))
        return out

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
                            env=ledger_env(**self._sign_env()))
        if cp.returncode != 0:
            msg = (cp.stderr or cp.stdout).strip().splitlines()
            git(["reset", "-q", "--mixed", "HEAD"], self.wt, check=False)
            raise TeamError("could not sign the ledger commit (levain writes nothing unsigned): "
                            + (msg[-1] if msg else f"exit {cp.returncode}")
                            + ". The signing key must be in ssh-agent or have no passphrase")

    def confirm_own_key(self, d, *, push: bool | None = None) -> bool:
        """If this machine's key is PENDING for a member, move it into force with a commit signed by that key.
        ``push`` None pushes when the clone has a remote; False commits here only (a caller that pushes on its own
        bounded wait)."""
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
        self.update_counted(confirm, f"levain team: {holder} confirms a key",
                            push=bool(self.remote) if push is None else push)
        return True

    def accept_offer(self, d, *, push: bool = True) -> str:
        """Accept the live ownership offer in ``d``, made to the member whose key IN FORCE is this machine's: the
        offeree becomes the owner and the offer is gone, in one counted change signed by that key. The one
        implementation of the act (``levain team accept`` and the cockpit's accept both call it)."""
        from . import tenure as T
        off = d.tenure.offer
        fp = self.own_fingerprint()
        if not off or fp not in T.key_fps(d.tenure, off["handle"]):
            raise TeamError("there is no live ownership offer to a member whose key in force is this machine's")

        def accept(team: R.Team, ten) -> None:
            team.owner = off["handle"]
            team.members[off["handle"]] = off.get("email") or team.members[off["handle"]]
            ten.offer = None
        return self.update_counted(accept, f"levain team: {off['handle']} accepts ownership", push=push)

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

        Presence (Phill 10-21 "12a"): where the team refuses file keys, an entry counts only if the commit adding it is
        signed with user presence (a FIDO key's touch); the derivation decides that on every read
        (``tenure.presence_required``), so no caller can waive it. Here it is the tidy path: a key that can never
        touch is refused before anything is written, and a commit the derivation does not count is undone.
        """
        # the whole append is one op, waited for up to its own lock_timeout (the hook's ack gives up in seconds and says
        # so): a pin moved between its validation and its commit filed it under another ledger (code L3 r7 codex 1)
        deadline = time.monotonic() + lock_timeout     # one budget for both waits (code L3 r8 complement 1)
        with self.op(timeout=lock_timeout):
            return self._append_in_op(entry, push=push, deadline=deadline)

    def _presence_key_problem(self) -> str | None:
        """Why this clone's signing key can never sign with user presence (None for a FIDO key)."""
        from . import signing as S
        line = self.signing_pubkey()
        kind = line.split()[0] if line.split() else "?"
        if kind.startswith("sk-"):
            return None
        try:
            fp = S.fingerprint(line)
        except S.SigningError:
            fp = "(unreadable)"
        return f"this clone signs with the {kind} key {fp}, which any program running as you can use without you"

    def _append_in_op(self, entry: dict, *, push: bool, deadline: float) -> dict:
        self.require_joined()
        self._require_own_key_in_force(entry["author"])
        left = deadline - time.monotonic()
        if left <= 0:
            # the budget is spent waiting for the op: busy, never a last 50 ms attempt (code L3 r9 complement 4)
            raise TeamBusy("ledger busy (another levain team operation holds the lock)")
        with self.lock(timeout=left):
            self._recover_dirty()
            self._dcache = None
            from . import tenure as T
            need_presence = T.presence_required(self.derivation().team.allow_file_keys, entry.get("type"))
            fido = self._presence_key_problem() is None
            if need_presence and not fido:
                raise TeamError(PRESENCE_REFUSAL.format(why=self._presence_key_problem()))
            if fido and entry.get("type") == "ack":
                # the edit hook's ack: a FIDO key would wait (up to the commit timeout) for a touch nobody was asked for
                raise TeamError("not recorded: this clone signs with a security key or Touch ID, which asks for a touch "
                                "on every signature, and an automatic acknowledgement never asks for one")
            if fido:
                print(f"levain team: touch your security key or confirm with Touch ID to sign {entry.get('type')} "
                      "into the ledger", file=sys.stderr, flush=True)
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
            before = git(["rev-parse", "HEAD"], self.wt).stdout.strip()
            git(["add", "--", str(path.relative_to(self.wt))], self.wt)
            self._commit(f"levain team: {sealed['type']} {sealed['id']}")
            self._dcache = None
            d = self.derivation()
            if sealed["hash"] in d.presence_refused:
                # the derivation's own verdict, the one every reader reaches: not in force, so not kept (nothing pushed)
                git(["reset", "-q", "--hard", before], self.wt, check=False)
                self._dcache = None
                raise TeamError(PRESENCE_REFUSAL.format(
                    why="the commit was undone: the signature on it carries no user-presence flag (a key made with "
                        "no-touch-required, or the touch was skipped)"))
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
        with self.op():      # the recovery too (code L3 r7 codex 1)
            self._sync_preamble()
            return self._sync(push=push)

    def _sync_preamble(self, lock_timeout: float = 30.0) -> None:
        """What every sync runs first, under the op it holds: this clone has joined, a moved or COPIED clone's worktree
        is relinked to this repository (else every rebase would land in the original's), and an interrupted operation
        is recovered. The ONE copy: ``sync`` and ``sync_now`` both call it (T3 L3 r1 codex HIGH 1)."""
        self._require_op()
        self.require_joined()
        with self.lock(timeout=lock_timeout):
            self._recover_dirty()

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
        self._replay_held(timeout, lock_timeout)

    def _pick_counts(self, d, c: str) -> bool:
        """Re-signed by this clone's key, would commit ``c`` count in ``d``? (``_pick_why`` says why not.)"""
        return self._pick_why(d, c) is None

    def _pick_why(self, d, c: str) -> str | None:
        """Why commit ``c``, re-signed by this clone's key, would NOT count in ``d`` (``"frozen"``, ``"key"`` or
        ``"presence"``), or None when it would. The key must be in force for every folder
        it writes, by the derivation's own rule (the owner for ``pack-*`` lines and PROJECT.md, else the member whose
        folder it is), not merely for some member (docs L3 r1 anansi: a held pack line replayed after this clone
        stopped being owner published without counting). A frozen ``d`` answers with the anchor's tenure, so it is
        ALSO asked of the tip as a clone joining now judges it (no anchor), which must be judged in full: the key must
        count in both. Either view alone was a hole: the anchor's alone re-signed with a key only pending at a
        rewritten tip (code L3 r3 codex 4, RUN); the anchor-less alone let a host rewind to before a removal decide
        (code L3 r4 codex HIGH + complement MED, RUN)."""
        from . import signing as S
        from . import tenure as T
        views = [d]
        if d.judged != "full":
            c0 = self.clone()
            try:
                fresh = T.derive(self.repo.toplevel, d.tip, T.Clone(pinned_root=c0.pinned_root, accepted=c0.accepted,
                                                                    distrust=c0.distrust),
                                 S.SigCache(self.base / "sigcache.json"))
            except T.Unjudgeable:
                return "frozen"
            if fresh.judged != "full":
                return "frozen"
            views.append(fresh)
        fp = self.own_fingerprint()
        files = git(["diff-tree", "--no-commit-id", "--name-only", "-r", c], self.wt).stdout.split()
        for v in views:
            holders: set[str | None] = set()
            for f in files:
                parts = f.split("/")
                if parts[0] == "ledger" and len(parts) >= 3:
                    top = parts[1]
                    holders.add(v.team.owner if top.startswith("pack-") else
                                next((h for h in v.team.members if E.safe_handle(h) == top), None))
                elif f == CANON_FILE:
                    holders.add(v.team.owner)
            if not all(h is not None and fp in T.key_fps(v.tenure, h) for h in holders):
                return "frozen" if d.judged != "full" else "key"
        # re-signed with a key that can never touch, a line the policy at the new parent refuses without presence would
        # publish without counting (codex HIGH 3): the same rule the derivation applies, asked before the re-sign
        if self._presence_key_problem() is not None:
            types = {T._parsed(text).get("type") for _rel, text in self._added_lines(c)}
            if any(T.presence_required(v.team.allow_file_keys, t) for v in views for t in types):
                return "presence"
        return None

    def _added_lines(self, c: str) -> list[tuple[str, str]]:
        """(path, text) of every ledger line commit ``c`` appends, in file order (see ``_appended``)."""
        return [(p, t) for p, t in self._appended(c)[0] if p.startswith("ledger/")]

    def _appended(self, c: str) -> tuple[list[tuple[str, str]], dict[str, str]]:
        """What commit ``c`` appends to each path it changes, read from the parent's and the child's blobs, never from
        diff text (file content ``++ b/<path>`` renders as a ``+++ b/`` header, and a parser of the text then files the
        lines after it under a path of the author's choosing: code L3 r5). Returns ([(path, line)], {path: why}): a path
        is in the first only when it stays a regular file whose new bytes start with every byte of its old ones and add
        a non-empty suffix of exactly as many lines as git counts added, with none removed (or, after a last line left
        without its newline, a suffix that starts by closing it, which git counts as that one line removed and added);
        any other path is in the second, with the reason. A commit id names fixed content, so within one sync each id
        is read once."""
        if self._acache is not None and c in self._acache:
            return self._acache[c]
        rows = git(["diff-tree", "-r", "-z", "--raw", "--no-abbrev", "--no-renames", "--no-commit-id", c],
                   self.wt).stdout.split("\0")
        counts: dict[str, tuple[str, str]] = {}
        for row in git(["diff-tree", "-r", "-z", "--numstat", "--no-renames", "--no-commit-id", c],
                       self.wt).stdout.split("\0"):
            parts = row.split("\t", 2)
            if len(parts) == 3:
                counts[parts[2]] = (parts[0], parts[1])
        def absent(oid: str) -> bool:     # the all-zero id at whatever width the repository hashes (SHA-1 or SHA-256)
            return not oid.strip("0")
        lines: list[tuple[str, str]] = []
        bad: dict[str, str] = {}
        changes = []
        for i in range(0, len(rows) - 1, 2):
            meta, path = rows[i][1:].split(" "), rows[i + 1]
            if not rows[i].startswith(":"):
                continue
            if len(meta) != 5 or meta[1] not in ("100644", "100755") or meta[0] not in ("000000", meta[1]):
                bad[path] = "not an append to a regular file (removed, a link, or a mode change)"
            else:
                changes.append((meta, path))
        # only regular files' objects are read, so a submodule's commit id is never asked for as a blob
        blobs = _blobs(self.wt, list(dict.fromkeys(s for meta, _p in changes for s in meta[2:4] if not absent(s))))
        for meta, path in changes:
            old = blobs[meta[2]] if not absent(meta[2]) else b""
            new = blobs[meta[3]]
            suffix = new[len(old):]
            # a last line left without its newline (a torn write) is closed by the append's leading newline, which git
            # counts as that line removed and added again; any other first byte would change that line
            torn = bool(old) and not old.endswith(b"\n")
            body = suffix[1:] if torn else suffix
            n = body.count(b"\n") + (0 if body.endswith(b"\n") else 1)
            if not new.startswith(old) or not body or (torn and not suffix.startswith(b"\n")):
                bad[path] = "changes or removes bytes already in the file"
            elif counts.get(path) != ((str(n + 1), "1") if torn else (str(n), "0")):
                bad[path] = f"appends {n} line(s) where git counts {'/'.join(counts.get(path, ('?', '?')))}"
            else:
                try:
                    texts = body.decode("utf-8").split("\n")
                except UnicodeDecodeError:
                    bad[path] = "appends bytes that are not UTF-8"
                    continue
                lines.extend((path, t) for t in (texts[:-1] if texts[-1] == "" else texts))
        if self._acache is not None and len(c) >= 40 and not c.strip("0123456789abcdef"):     # an id, not a ref name
            self._acache[c] = (lines, bad)
        return lines, bad

    def _ledger_wrong(self, c: str, *, entries: bool) -> dict[str, str]:
        """{ledger path: why} for every ledger path commit ``c`` does anything to but append: the ONE test of whether
        sync may act on ``c`` at all. Every place that moves, re-signs or replays a commit asks it first, so no reader of
        ``_added_lines`` takes a commit it refuses as one that appends nothing (code L3 r6 codex MED: a held commit that
        replaced a line was cherry-picked and published). With ``entries``, also every ledger path whose appended lines
        are not all valid entries of the folder they are filed under: what a re-seal needs, since it writes each line
        as an entry (a pick carries the bytes as they are, and levain's own recovery commits a torn line)."""
        lines, bad = self._appended(c)
        wrong = {p: why for p, why in bad.items() if p.startswith("ledger/")}
        if entries:
            added: dict[str, list[str]] = {}
            for path, text in lines:
                added.setdefault(path, []).append(text)
            for p, texts in added.items():
                if p.startswith("ledger/") and not self._entry_lines_ok(p, texts):
                    wrong[p] = "appends a line that is not a valid entry of the folder it is filed under"
        return wrong

    def _held_why(self, why: str, d) -> str:
        """The hold message for the reason ``_pick_why`` (or the replay's own read-back) gave."""
        return {
            "frozen": f"the ledger is frozen ({d.frozen_why}) and, replayed now, they would not count on the published "
                      "history (this machine's key is not in force there, or it cannot be judged); the next sync "
                      "retries them",
            "key": "replayed now they would be re-signed with this machine's key, which is not in force for the folder "
                   "they are filed under, and publish without counting. Confirm the key (`levain team key confirm`) "
                   "from this machine, then `levain team sync`",
            "presence": "the team refuses file keys and they are signed with no user presence, so they would publish "
                        "without counting; the owner runs `levain team file-keys allow`, or record them again with a "
                        "Touch ID or security key",
            "depends": "they supersede or refer to an entry kept back above, so they wait for it",
            "touch": "this clone signs with a Touch ID or security key and the team needs a touch for each of them, "
                     "and this sync is not interactive, so it asked for none. Run `levain team sync` in a terminal: it "
                     "names each entry and asks one touch for it",
        }[why]

    def _unsplittable(self, c: str) -> str:
        """What own commit ``c``, which changes team.toml/tenure.toml, also changes OTHER than appended entry lines
        (empty when its other half, if any, is entry lines only, which a re-seal carries whole). An appended line counts
        only in an entry file (``ledger/<author>/<device>.jsonl``, the shape a holder is judged for) and only if it
        parses and validates as its author's entry: anything else would be re-sealed into content no reader counts, or
        throw after earlier lines were written. PROJECT.md is passed over: it is regenerated, never carried."""
        from . import tenure as T
        lines, bad = self._appended(c)
        wrong = self._ledger_wrong(c, entries=True)
        other = [p for p in dict.fromkeys([*bad, *(p for p, _t in lines)])
                 if p not in (T.TEAM_FILE, T.TENURE_FILE, CANON_FILE) and (p in wrong or not p.startswith("ledger/"))]
        return ", ".join(other[:5])

    @staticmethod
    def _entry_lines_ok(path: str, texts: list[str]) -> bool:
        """``path`` is an entry file and every one of ``texts`` is a valid entry of the author it is filed under."""
        parts = path.split("/")
        if len(parts) != 3 or parts[0] != "ledger" or not parts[2].endswith(".jsonl") or parts[2] == ".jsonl":
            return False
        for text in texts:
            try:
                obj = E.parse_line(text)
                if not isinstance(obj, dict) or E.safe_handle(str(obj.get("author") or "")) != parts[1]:
                    return False
                E.validate({k: v for k, v in obj.items() if k not in ("prev", "hash")}, scan=False)
            except (ValueError, TypeError, KeyError, AttributeError):     # LineError and EntryError are ValueErrors
                return False
        return True

    def _refuse_unsplittable(self, commits: list[str]) -> None:
        """Before any ref moves: an own commit mixing a team change with anything but appended ledger lines cannot be
        split without losing a half, and one that changes a ledger file other than by appending to it is never
        published (``_ledger_wrong``), so the sync stops by name (levain writes neither; it was made by hand)."""
        from . import tenure as T
        for c in commits:
            touched = set(git(["diff-tree", "--no-commit-id", "--name-only", "-r", c], self.wt).stdout.split())
            other = self._unsplittable(c) if touched & {T.TEAM_FILE, T.TENURE_FILE} else ""
            if other:
                raise TeamError(f"not pushed: the local commit {c[:10]} changes the team AND {other}, and sync cannot "
                                "split it without losing one half; nothing was changed. See it with "
                                f"`git -C {self.wt} show {c[:10]}`; make each change again with the levain CLI, then "
                                "take that commit off the branch with git")
            wrong = self._ledger_wrong(c, entries=False) if any(p.startswith("ledger/") for p in touched) else {}
            if wrong:
                p, why = next(iter(wrong.items()))
                raise TeamError(f"not pushed: the local commit {c[:10]} changes {', '.join(list(wrong)[:5])} other "
                                f"than by appending lines ({p}: {why}), and sync never publishes that; "
                                f"nothing was changed. See it with `git -C {self.wt} show {c[:10]}`; make each entry "
                                "again with the levain CLI, then take that commit off the branch with git")

    def _strip_team(self, c: str, root: str | None, pending: list[dict]) -> bool:
        """Queue own team/tenure commit ``c``'s change as a pending op (re-landed from the counted state, never replayed
        as text). True when ``c`` ALSO appends ledger lines: the caller holds the whole commit under refs/levain/held/
        and the replay re-seals those lines, so neither half is lost. A change that cannot be re-applied is kept under
        refs/levain/unapplied/ (never pushed) and named. A restore op is not queued: it writes the counted state back
        and decides nothing, and a re-land writes its own restore when one is needed."""
        op = self._delta(c)
        if op is None:
            git(["update-ref", f"refs/levain/unapplied/{c}", c], self.wt, check=False)
            self.warnings.append(f"a local team change {c[:10]} could not be re-applied (no levain trailer, or its "
                                 "parent cannot be judged here); it is kept under refs/levain/unapplied/ and never "
                                 "pushed. Make the change again with the levain CLI")
        elif not op.get("restore"):
            pending.append({**op, "root": root})
        return bool(self._added_lines(c))

    def _reseal(self, c: str, timeout: float, message: str | None = None) -> subprocess.CompletedProcess:
        """Re-seal commit ``c``'s lines onto the current tip of their files and commit them with ``c``'s message (or
        ``message``), signed by this clone's key: an entry kept back earlier in the same file, or published past since,
        left the line's ``prev`` pointing at a line that is not the tip, so a plain cherry-pick would publish a broken
        chain (or conflict). Only the ledger lines are written, so a commit that also changed the team re-lands that
        half as an op, never as its text. The lines are what ``c`` appends, read from its blobs; a ledger path it does
        anything else to, or an appended line that is not a valid entry of the folder it is filed under, fails the
        re-seal by name before any file is written."""
        wrong = self._ledger_wrong(c, entries=True)
        if wrong:
            return subprocess.CompletedProcess([], 1, "", f"{c[:10]} changes {', '.join(list(wrong)[:5])} other than "
                                                          "by appending valid entry lines")
        lines = self._added_lines(c)
        self._dcache = None
        tips = {f.rel: f.last_hash for f in self.ledger().files}
        sealed_lines = []
        for path, text in lines:     # every line is sealed before any file is written: a bad one throws with none
            rel = path.split("/", 1)[1]
            obj = {k: v for k, v in E.parse_line(text).items() if k not in ("prev", "hash")}
            sealed = E.seal(obj, tips.get(rel, ""))
            tips[rel] = sealed["hash"]
            sealed_lines.append((path, sealed))
        paths = []
        for path, sealed in sealed_lines:
            with os.fdopen(self._open_ledger_file(self.wt / path), "a+b") as fh:
                fh.seek(0, os.SEEK_END)
                if fh.tell() > 0:
                    fh.seek(-1, os.SEEK_END)
                    if fh.read(1) != b"\n":
                        fh.write(b"\n")
                fh.write((json.dumps(sealed, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
            paths.append(path)
        git(["add", "--", *dict.fromkeys(paths)], self.wt)
        how = ["-C", c] if message is None else ["-m", message]
        return git([*_REPLAY_CONFIG, *self._sign_cfg(), "commit", "-q", "--no-verify", *how], self.wt,
                   check=False, timeout=timeout, extra_env=self._sign_env())

    def _replay_held(self, timeout: float, lock_timeout: float) -> None:
        """Replay entries held back by ``_rebase_moves`` (T42) or ``_hold_unpresent`` once they would count at the tip,
        so the re-signed copies count; one that would not stays held, and the later ones still replay. Each held ref
        is deleted only after its commit is on the branch; a crash leaves the ref, and the next sync retries it (an
        already-applied pick comes out empty, and an already re-sealed entry is found by id, and is skipped). The root
        is read ONCE, first under the worktree lock (which `join` holds while it moves the pin and the worktree):
        every held ref this call lists or deletes is under that root (code L3 r5 codex 1)."""
        with self.lock(timeout=lock_timeout):
            root = self.pinned_root
            held = git(["for-each-ref", "--format=%(objectname)", self._held_ref(root)], self.wt,
                       check=False).stdout.split()
            if not held:
                return
            self._recover_dirty()
            self._dcache = None
            d = self.derivation()
            want = set(held)
            cp = git(["rev-list", "--reverse", "--topo-order", "--no-merges", *held, "--not", "HEAD"], self.wt,
                     check=False)
            if cp.returncode != 0:     # a held object this git cannot read: kept, and the next sync retries
                self.warnings.append(f"held entries under {self._held_ref(root)} could not be listed ({_tail(cp)}); "
                                     "nothing was replayed")
                return
            order = [c for c in cp.stdout.split() if c in want]
            from . import tenure as T
            fido = self._presence_key_problem() is None
            # in order, and past an entry that stays held (codex MED, r2): a later entry in the same file is re-sealed
            # onto the published chain, and one that supersedes or refers to a held entry waits with it
            kept: dict[str, list[str]] = {}
            kept_ids: set[str] = set()

            def refuse(c: str, ids: set[str], wrong: dict[str, str]) -> None:
                # it stays held, named with the one remedy, and later entries still replay (no `break`)
                p, w = next(iter(wrong.items()))
                self.warnings.append(
                    f"a held commit {c[:10]} ({', '.join(sorted(ids)[:5]) or 'no entry id'}) changes "
                    f"{', '.join(list(wrong)[:5])} other than by appending valid entry lines ({p}: {w}), so sync never "
                    f"publishes it. See it with `git -C {self.wt} show {c[:10]}`; record each entry again with the "
                    f"levain CLI, then drop it with `git -C {self.wt} update-ref -d {self._held_ref(root, c)}`")
                kept_ids.update(ids)

            for c in order:
                lines = self._added_lines(c)
                objs = [T._parsed(text) for _p, text in lines]
                ids = {str(o.get("id")) for o in objs if o.get("id")}
                # asked before anything reads its lines as all it changes: a commit held by an earlier build that
                # replaces, removes or forges a ledger line is never picked or re-sealed (code L3 r6 codex MED)
                wrong = self._ledger_wrong(c, entries=False)
                if wrong:
                    refuse(c, ids, wrong)
                    continue
                why = self._pick_why(d, c)
                if why is None and any((set(o.get("supersedes") or []) | set(o.get("refs") or [])) & kept_ids
                                       for o in objs):
                    why = "depends"
                # the re-sign is the entry's presence (H-6): a person touches for THIS entry, named first as `record`
                # names it, or it is not re-signed at all
                named = fido and any(T.presence_required(d.team.allow_file_keys, o.get("type")) for o in objs)
                if why is None and named and not _interactive():
                    why = "touch"
                if why is None:
                    before = git(["rev-parse", "HEAD"], self.wt).stdout.strip()
                    touched = set(git(["diff-tree", "--no-commit-id", "--name-only", "-r", c], self.wt).stdout.split())
                    mixed = bool(touched & {T.TEAM_FILE, T.TENURE_FILE})
                    self._dcache = None
                    # re-sealed when a file's current tip is not the line its first new line was sealed over (an earlier
                    # line of it held, or published past since: complement A), and always for a commit that also
                    # changed the team, whose team half re-lands as an op, never as its text
                    tips = {f.rel: f.last_hash for f in self.ledger().files}
                    sealed_over: dict[str, str] = {}
                    for (p, _t), o in zip(lines, objs):
                        sealed_over.setdefault(p.split("/", 1)[1], str(o.get("prev") or ""))
                    reseal = mixed or any(prev != tips.get(rel, "") for rel, prev in sealed_over.items())
                    if reseal:
                        if ids and ids <= set(self.ledger().by_id):
                            # replayed by a sync that died before deleting the ref: already on the branch
                            git(["update-ref", "-d", self._held_ref(root, c)], self.wt, check=False)
                            continue
                        # what the re-seal would refuse is refused here, before a touch is asked for, so no sync asks
                        # for a touch it cannot use (code L3 r6 complement LOW)
                        wrong = self._ledger_wrong(c, entries=True)
                        if wrong:
                            refuse(c, ids, wrong)
                            continue
                    if named:
                        for o in objs:
                            summary = (str(o.get("words") or o.get("summary") or "").splitlines() or [""])[0][:80]
                            print(f"levain team: touch your security key or confirm with Touch ID to sign "
                                  f"{o.get('type')} {o.get('id')}: {summary}", file=sys.stderr, flush=True)
                    if reseal:
                        msg = None if not mixed else (
                            f"levain team: {' '.join(sorted({str(o.get('type')) for o in objs}))} "
                            f"{' '.join(sorted(ids))}\n\n{T.DEVICE_TRAILER}: {self.device}")
                        cp = self._reseal(c, timeout, msg)
                    else:
                        cp = git([*_REPLAY_CONFIG, *self._sign_cfg(), "cherry-pick", "--allow-empty", c], self.wt,
                                 check=False, timeout=timeout, extra_env=self._sign_env())
                    if cp.returncode == 0:
                        # read back as every clone will: a FIDO re-sign that came out with no presence stays held
                        self._dcache = None
                        refused = self.derivation().presence_refused
                        if any(T._parsed(text).get("hash") in refused for _p, text in self._added_lines("HEAD")):
                            git(["reset", "-q", "--hard", before], self.wt)
                            self._dcache = None
                            why = "presence"
                        else:
                            d = self.derivation()     # the next pick is judged at the tip this one made (complement B)
                    elif reseal:
                        git(["reset", "-q", "--hard", before], self.wt, check=False)
                        self.warnings.append(f"a held entry {c[:10]} could not be re-sealed ({_tail(cp)}); it stays "
                                             "under refs/levain/held/ and the next sync retries it")
                        break
                    elif cp.returncode == 1 and not git(["diff", "--name-only", "--diff-filter=U"], self.wt,
                                                        check=False).stdout.split() and \
                            git(["diff", "--cached", "--quiet"], self.wt, check=False).returncode == 0:
                        git(["cherry-pick", "--skip"], self.wt, timeout=60)     # already on the branch
                    elif touched == {CANON_FILE} and set(git(["diff", "--name-only", "--diff-filter=U"], self.wt,
                                                             check=False).stdout.split()) == {CANON_FILE}:
                        # a regenerable PROJECT.md is dropped, as the divergence replay drops it, never left to block
                        # every held entry after it
                        git(["cherry-pick", "--abort"], self.wt, check=False)
                        self.warnings.append("a local PROJECT.md conflicted with the remote and was dropped; re-run "
                                             "`levain team consolidate`")
                    else:
                        git(["cherry-pick", "--abort"], self.wt, check=False)
                        self.warnings.append(f"a held entry {c[:10]} could not be replayed ({_tail(cp)}); it stays "
                                             "under refs/levain/held/ and the next sync retries it")
                        break
                if why is not None:
                    kept.setdefault(why, []).append(c)
                    kept_ids |= ids
                    continue
                git(["update-ref", "-d", self._held_ref(root, c)], self.wt, check=False)
            self._dcache = None
            for why, cs in kept.items():
                named = [i for c in cs for _p, t in self._added_lines(c) for i in [str(T._parsed(t).get("id") or "")]
                         if i]
                self.warnings.append(f"{len(cs)} unpublished entr(y/ies) kept back under refs/levain/held/ ("
                                     + ", ".join(named[:5]) + (" ..." if len(named) > 5 else "") + "): "
                                     + self._held_why(why, d))

    def _rebase_moves(self, rref: str, timeout: float, lock_timeout: float) -> None:
        """Put this clone's OWN unpublished commits on top of the remote, under the worktree lock.

        The moved set is ``HEAD --not <every seen tip> --no-merges`` signed by this clone's own keys. Ledger-line
        commits are cherry-picked (and re-signed) onto the remote tip. Team/tenure commits are never replayed as
        text: they are stripped into pending ops and re-landed from the COUNTED state at the new tip, each field only
        if no other counted commit touched it since (history-keyed), so an offline change never overwrites a newer
        decision. Nothing that came from the remote is ever re-published. The pinned root is read ONCE, first under the
        worktree lock (which `join` holds while it moves the pin and the worktree): a held ref and a pending op carry
        that value, never a later read of a pin that may have moved (code L3 r5 codex 1).
        """
        with self.lock(timeout=lock_timeout):
            root = self.pinned_root
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
            self._refuse_unsplittable(own)
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
            pending: list[dict] = []     # THIS call's new ops only: added to, and on failure removed from, the current list
            picks: list[str] = []
            mixed: list[str] = []
            from . import tenure as T
            for c in own:
                touched = set(git(["diff-tree", "--no-commit-id", "--name-only", "-r", c], self.wt).stdout.split())
                if touched & {T.TEAM_FILE, T.TENURE_FILE}:
                    if self._strip_team(c, root, pending):
                        mixed.append(c)    # its ledger lines too: held whole below, re-sealed by the replay
                else:
                    picks.append(c)        # an entry, or PROJECT.md (dropped below only if it conflicts)
            d_remote = self.derivation(remote_tip) if picks else None
            # an entry the team needs a touch for, re-signed by a FIDO key, goes through the replay, which names it and
            # asks a touch for it (H-6), never through an unnamed pick here
            named = bool(picks) and self._presence_key_problem() is None and any(
                T.presence_required(d_remote.team.allow_file_keys, T._parsed(text).get("type"))
                for c in picks for _p, text in self._added_lines(c))
            if mixed or named or (picks and not all(self._pick_counts(d_remote, c) for c in picks)):
                # re-signed now, with a key not in force at the remote (a rotation's still-PENDING key) or onto a remote
                # this clone cannot judge in full, the entries could publish without counting (T42, RAN; code L3 r3
                # codex 4, RAN). Held under a ref instead; `_replay_held` replays them in order
                # after the held team ops (the confirm) re-land, and only once the key counts.
                for c in picks + mixed:
                    git(["update-ref", self._held_ref(root, c), c], self.wt)
                picks = []
            published = added = False
            try:
                git(["checkout", "-q", "--detach", remote_tip], self.wt, timeout=timeout)
                for c in picks:
                    cp = git([*_REPLAY_CONFIG, *self._sign_cfg(), "cherry-pick", "--allow-empty", c],
                             self.wt, check=False, timeout=timeout, extra_env=self._sign_env())
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
                    if foreign:
                        # the entry builds on a commit sync does not move (T36): name THAT, not a device clash (the
                        # 1007+30 suite run printed "two clones share a device id" for a hand-made commit)
                        raise TeamError(f"an unpushed entry cannot be replayed onto the remote ledger: it builds on "
                                        f"{len(foreign)} local commit(s) levain did not write on this clone (listed as "
                                        "NOT moved: " + ", ".join(c[:10] for c in foreign[:5]) + "). Nothing was "
                                        "changed locally; publish or drop those commits with git, then sync")
                    raise TeamError("an unpushed entry cannot be replayed onto the remote ledger (two clones share "
                                    f"a device id, or git could not run: {_tail(cp)}). Nothing was changed locally; "
                                    "if this clone's .git was copied from another, see `levain team join --new-device`")
                new = git(["rev-parse", "HEAD"], self.wt).stdout.strip()
                # the stripped ops are saved BEFORE the ref moves past their commits (code L3 r2 glm MED, complement
                # LOW: a failure between the two lost them), and taken back out if the ref never moves
                self._add_pending(pending)
                added = True
                git(["update-ref", "-m", "levain team: replay onto remote", self.ref, new, orig], self.wt)
                published = True
                git(["checkout", "-q", "-f", self.branch], self.wt, timeout=60)
            finally:
                if not published:
                    if added:
                        self._drop_pending(pending)
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

    @staticmethod
    def _held_ref(root: str | None, sha: str = "") -> str:
        """Held entries are bound to the genesis they were held on: only a clone pinned to THAT genesis replays them
        (code L3 r3 codex 2, RUN: a `join --root` to a re-genesis replayed the old ledger's held entry into it). The
        caller passes the root it captured ONCE at entry, never a fresh read of the pin (code L3 r5 codex 1: a pin moved
        mid-sync filed genesis A's entry under B's refs)."""
        return f"refs/levain/held/{root}/{sha}"

    def other_genesis_items(self) -> str:
        """Held entries and offline team ops made on ANOTHER genesis than the pinned one: kept (never replayed or
        re-landed here, never deleted), and named, for the move that left them behind."""
        root = self.pinned_root
        held = [r.split("/")[3] for r in git(["for-each-ref", "--format=%(refname)", "refs/levain/held/"],
                                             self.repo.toplevel, check=False).stdout.split()
                if not r.startswith(self._held_ref(root))]
        ops = [op for op in self.state().get("pending_ops") or [] if op.get("root") != root]
        if not held and not ops:
            return ""
        roots = sorted({str(x)[:12] for x in held} | {str(op.get("root"))[:12] for op in ops})
        return (f"{len(held)} held entr(y/ies) under refs/levain/held/ and {len(ops)} offline team change(s) were made "
                f"on another genesis ({', '.join(roots)}) than the one this clone is now pinned to; they are kept, "
                f"never replayed or re-landed here (pending_ops in {self.state_path}). Re-record what is still wanted "
                "on this ledger")

    def _record_seen_sha(self, tip: str) -> None:
        git(["update-ref", f"refs/levain/seen/{self.branch}/{tip}", tip], self.repo.toplevel, check=False)

    def _add_pending(self, added: list[dict]) -> None:
        """Append ``added`` to the CURRENT pending ops, under the state lock (never a write of an earlier snapshot)."""
        if added:
            self.save_state(_mutate=lambda st: st.__setitem__("pending_ops", list(st.get("pending_ops") or []) + added))

    def _drop_pending(self, consumed: list[dict]) -> None:
        """Remove ``consumed``, one occurrence each, from the CURRENT pending ops under the state lock: an op another
        operation queued meanwhile stays (seat ruling, r5 lane item 2: `_reland` wrote back its entry snapshot and
        erased it; C's stale-write shape, code L3 r5)."""
        if not consumed:
            return

        def drop(st: dict) -> None:
            cur = list(st.get("pending_ops") or [])
            for op in consumed:
                if op in cur:
                    cur.remove(op)
            st["pending_ops"] = cur
        self.save_state(_mutate=drop)

    def _reland(self) -> None:
        """Re-apply stripped team ops to the counted state at the new tip: net per field, history-keyed."""
        from . import tenure as T
        # it decides from the counted state and then applies: only inside the op, so no owner change lands between the
        # decision and the commit (code L3 r6 codex 1)
        self._require_op()
        # bound to the genesis they were made on (code L3 r3 codex 3, RUN: after `join --root` to a re-genesis that
        # left cy out, an old-ledger `member add cy` re-landed there); the others stay kept, never applied here
        root = self.pinned_root     # read ONCE (code L3 r5 codex 1)
        ops = [op for op in self.state().get("pending_ops") or [] if op.get("root") == root]
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
            self._drop_pending(ops)
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
        self._drop_pending(ops)     # only after the re-land is committed or refused whole
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
        """A sync is one op, waited for up to ``net_timeout`` (fetch_if_due passes its own short wait, then skips)."""
        with self.op(timeout=net_timeout):
            return self._sync_in_op(push=push, timeout=timeout, net_timeout=net_timeout, lock_timeout=lock_timeout)

    def _hold_unpresent(self, rref: str, timeout: float, lock_timeout: float) -> None:
        """The one publish gate for the presence rule (codex HIGH 2 + 3): an unpublished entry the derivation does not
        count for want of user presence (a kill between its commit and the writer's check, or a pick re-signed onto a
        parent that refuses file keys) is never pushed, and nothing is dropped (r2 H-5: a refusal whose remedy was a
        hard reset lost every unpublished entry). From the FIRST such commit on, this clone's own entries are held
        under refs/levain/held/ (one file's lines are one chain, so a later line cannot go out before the one it is
        sealed over) and its team changes are stripped into pending ops; the branch goes back to that commit's parent,
        the ops re-land, and the replay puts back every later entry that counts (re-sealed where its file kept an
        earlier line back). The countable prefix then publishes."""
        if not self.pinned_root:
            return
        d = self.derivation()
        if not d.presence_refused:
            return
        with self.lock(timeout=lock_timeout):
            root = self.pinned_root
            self._recover_dirty()
            orig = git(["rev-parse", "HEAD"], self.wt).stdout.strip()
            fresh = git(["rev-list", "--reverse", "--topo-order", f"{rref}..{orig}" if self._has(rref) else orig],
                        self.wt).stdout.split()
            bad = {d.walk[d.line_pos[h]] for h in d.presence_refused if h in d.line_pos}
            # `presence_refused` covers the whole history (a published line the remote already carries is in it too);
            # only a refused line in THIS clone's unpublished range is held. None there: nothing here to hold, and the
            # push sends no line the rule refuses (complement D)
            first = next((c for c in fresh if c in bad), None)
            if first is None:
                return
            suffix = fresh[fresh.index(first):]
            own, _foreign = self._movable(orig)
            if not set(suffix) <= set(own):
                # held refs are re-signed with this clone's key on replay, so only its own commits may be held
                raise TeamError("not pushed: an unpublished entry carries no user presence and the team refuses file "
                                "keys where it sits, and commits this clone did not write sit after it; nothing was "
                                "changed. Run `levain team doctor`")
            from . import tenure as T
            self._refuse_unsplittable(suffix)
            pending: list[dict] = []
            for c in suffix:
                touched = set(git(["diff-tree", "--no-commit-id", "--name-only", "-r", c], self.wt).stdout.split())
                # a commit with a team change AND ledger lines is held WHOLE, its team half queued too (codex MED: its
                # lines were left dangling when the branch moved back past it)
                if not touched & {T.TEAM_FILE, T.TENURE_FILE} or self._strip_team(c, root, pending):
                    git(["update-ref", self._held_ref(root, c), c], self.wt)
            self._add_pending(pending)
            try:
                git(["update-ref", "-m", "levain team: hold entries that would not count", self.ref, f"{first}^",
                     orig], self.wt)
            except TeamError:
                self._drop_pending(pending)
                raise
            git(["checkout", "-q", "-f", self.branch], self.wt, timeout=60)
            self._dcache = None
        self._reland()
        self._replay_held(timeout, lock_timeout)

    def _sync_in_op(self, *, push: bool, timeout: float, net_timeout: float, lock_timeout: float,
                    retries: int = _PUSH_RETRIES) -> str:
        """One sync, with ``_appended`` read once per commit id for its lifetime (code L3 r6 complement LOW: each
        caller re-read the same commit's blobs)."""
        self._acache = {}
        try:
            return self._sync_body(push=push, timeout=timeout, net_timeout=net_timeout, lock_timeout=lock_timeout,
                                   retries=retries)
        finally:
            self._acache = None

    def _sync_body(self, *, push: bool, timeout: float, net_timeout: float, lock_timeout: float,
                   retries: int) -> str:
        """Fetch, rebase, optionally push. Must be called WITHOUT the worktree lock held.

        Network I/O runs under a separate ``net`` lock, so a hook reading the worktree never waits on a
        slow remote: the worktree lock is held only for the local rebase and commits.
        """
        if not self.remote:
            return "local only (no remote)"
        with self.lock(name="net", timeout=net_timeout):
            # read only under net: every move of the pin (join, repin --root) holds net, so the ledger synced here is
            # the one pinned for the whole sync (seat ruling on the r5 lane's stopped item 1: read before the lock, a
            # `join --root` landing meanwhile synced the old ledger's remote into the new pin)
            remote = self.remote
            if not remote:
                return "local only (no remote)"
            rref = f"refs/remotes/{remote}/{self.branch}"
            local = self.ref
            for attempt in range(retries):
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
                self._hold_unpresent(rref, timeout, lock_timeout)
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
        raise TeamError(f"push still rejected after {retries} fetch+rebase rounds")

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
            # the op first, the attempt stamped only once it is held: a busy op is NOT another sync (it may be any team
            # command), so it is reported and retried next time, never recorded as an attempt (code L3 r7 codex 3 +
            # complement 3: the hook showed the old copy with no note and did not retry for a whole interval)
            try:
                with self.op(timeout=0.5):
                    # decided again under the op: another caller may have just refreshed (code L3 r8 codex 5)
                    prior = self.state().get("last_fetch_attempt")
                    if time.time() - float(prior or 0) < interval:
                        return None
                    self.save_state(last_fetch_attempt=time.time())
                    try:
                        self._sync_in_op(push=False, timeout=timeout, net_timeout=0.5, lock_timeout=3.0)
                    except TeamBusy:
                        # busy INSIDE the body (net or worktree): not an attempt either, so the prior stamp goes back,
                        # still under the op every stamp writer holds (code L3 r9 complement 2; r10 codex 6: a
                        # restore after the release compared wall-clock values, which are not unique)
                        self.save_state(last_fetch_attempt=prior)
                        raise
            except TeamBusy:
                # reported only when the copy shown is itself older than the interval: an overlapping refresh by
                # another caller stays quiet (code L3 r8 complement 2)
                if time.time() - float(self.state().get("last_fetch_ok") or 0) < max(interval, 1.0):
                    return None
                return "another levain team operation on this clone is running"    # the hook adds the rest
            return None
        except TeamError as exc:
            self.save_state(last_fetch_error=str(exc))
            return str(exc)
        except Exception as exc:  # noqa: BLE001 - a hook path: report, never raise
            return f"{type(exc).__name__}: {exc}"

    def sync_now(self, *, push: bool = False, op_wait: float = 5.0, timeout: float = 20.0,
                 retries: int = _PUSH_RETRIES) -> str:
        """One sync now (a fetch and rebase; a push only when ``push``), for a caller that holds a person's request
        open: the op is waited for ``op_wait`` seconds and each git network call gets ``timeout``, never the CLI's
        minutes. ``timeout`` bounds EACH git call, not the sync: one sync may run several (fetch, the strict-ledger
        fetch, push retries), and its rebase waits up to ``op_wait`` for the worktree lock. It runs ``sync``'s own
        preamble first (``_sync_preamble``: joined, a copied clone's worktree relinked, an interrupted op recovered).
        A busy op raises TeamBusy and records nothing (it is not an attempt, as for ``fetch_if_due``); a
        fetch that fails is recorded as ``last_fetch_error``, as the hook records one, and raised; a success is
        stamped by the fetch itself."""
        with self.op(timeout=op_wait):
            try:
                self._sync_preamble(lock_timeout=op_wait)
                return self._sync_in_op(push=push, timeout=timeout, net_timeout=op_wait, lock_timeout=op_wait,
                                        retries=retries)
            except TeamBusy:
                raise
            except TeamError as exc:
                if not push:
                    self.save_state(last_fetch_attempt=time.time(), last_fetch_error=str(exc))
                raise

    # ---- canon ---------------------------------------------------------------------------------------------

    def ledger_tree(self) -> str:
        cp = git(["rev-parse", "-q", "--verify", f"{self.ref}:ledger"], self.repo.toplevel, check=False)
        return cp.stdout.strip() or "empty"

    def read_canon(self, rev: str | None = None, *, d=None) -> str | None:
        """The OWNER's canon: PROJECT.md as of the last commit the owner in force signed, never the tip's file.
        ``d``: a derivation the caller already holds (its canon_sha is used; ``rev`` is then ignored)."""
        try:
            sha = (d if d is not None else self.derivation(rev)).canon_sha
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
        with self.op():      # code L3 r7 codex 1: a canon rendered from one ledger committed into another
            return self._write_file_in_op(name, text, message, push)

    def _write_file_in_op(self, name: str, text: str, message: str, push: bool) -> str:
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
        with self.op():
            return self._update_counted_in_op(change, message, push=push, _check=_check)

    def _update_counted_in_op(self, change, message: str, *, push: bool = True, _check: bool = True) -> str:
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
            meant = [k for k in set(want_f) | set(had_f) if want_f.get(k) != had_f.get(k)]
            missing = sorted(".".join(str(x)[:16] for x in k) for k in meant if now.get(k) != want_f.get(k))
            landed = sorted(".".join(str(x)[:16] for x in k) for k in meant if now.get(k) == want_f.get(k))
            if missing and _check:
                # a re-land is field by field, so name both halves (code L3 r3 glm MED 4: a partial landing read as
                # nothing in force)
                raise TeamError(f"{out}; but after syncing with the remote this change is NOT in force for: "
                                f"{', '.join(missing)} (a newer change landed first); re-issue it if still wanted. "
                                + (f"In force: {', '.join(landed)}" if landed else "None of it is in force"))
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
