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
import hashlib
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
REF = f"refs/heads/{BRANCH}"
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
# The only names levain writes under ledger/: <handle>/<device>.jsonl (file_for, _new_device). Compared as bytes.
_LEDGER_PATH_RE = re.compile(rb"ledger/[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[0-9a-f]{16}\.jsonl")
_SHA_RE = re.compile(rb"[0-9a-f]{40}|[0-9a-f]{64}")


_NON_UTF8_LINE = "<a line that is not UTF-8>"   # cannot parse as an entry, so it is a reported problem, never an entry
_REGULAR_MODES = (b"100644", b"100755")


def _blob_lines(data: bytes) -> list[bytes]:
    """A ledger file's lines as BYTES. Split on LF only (a CR stays inside its line)."""
    lines = data.split(b"\n")
    return lines[:-1] if lines and lines[-1] == b"" else lines


def _added_removed(old: list[bytes], new: list[bytes]) -> tuple[list[bytes], list[bytes]]:
    """Lines added and removed between two versions of a file, in linear time (a hook runs this).

    Append-only is the shape levain writes, so it is the fast path; anything else is a set difference with order
    kept, which is also what the retired quadratic diff reduced to for the question asked here."""
    if new[:len(old)] == old:
        return new[len(old):], []
    sold, snew = set(old), set(new)
    return [l for l in new if l not in sold], [l for l in old if l not in snew]


def _text(line: bytes) -> str:
    try:
        return line.decode("utf-8")
    except UnicodeDecodeError:
        return _NON_UTF8_LINE


# rerere replays a recorded resolution and can stage it, which makes a conflicting pick look empty; signing needs a
# prompt a replay cannot answer
_REPLAY_CONFIG = ["-c", "rerere.enabled=false", "-c", "rerere.autoupdate=false", "-c", "commit.gpgsign=false"]


def git(args: list[str], cwd: Path, *, timeout: float = 60, check: bool = True,
        input_text: str | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB_ENV}
    env.update(GIT_TERMINAL_PROMPT="0", LC_ALL="C", GIT_EDITOR="true")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")  # never prompt on /dev/tty from a hook
    # Bytes in, bytes out: text=True would decode with the parent's locale and turn a CR into a line break. The
    # str fields are for messages and simple tokens (replacement characters, never a lone surrogate); anything
    # that is a path or ledger content is read from stdout_bytes.
    try:
        raw = subprocess.run(["git", *_NO_HOOKS, *args], cwd=str(cwd), env=env, capture_output=True,
                             timeout=timeout, input=None if input_text is None else input_text.encode("utf-8"),
                             stdin=None if input_text is not None else subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise TeamError(f"git {args[0]} timed out after {timeout:.0f}s") from None
    except FileNotFoundError:
        raise TeamError("git is not on PATH") from None
    cp = subprocess.CompletedProcess(raw.args, raw.returncode, raw.stdout.decode("utf-8", "replace"),
                                     raw.stderr.decode("utf-8", "replace"))
    cp.stdout_bytes = raw.stdout
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

        def one(flag: str) -> bytes | None:
            # One path per call, as bytes, exactly one trailing LF removed: a path may hold any character but NUL,
            # so a two-path answer split into lines cannot be trusted. Only "not a work tree" means None; any other
            # failure (git missing, a timeout, safe.directory, a broken config) raises, so a hook in a joined clone
            # reports it instead of reading "no repository" and going quiet.
            cp = git(["rev-parse", "--path-format=absolute", flag], d, timeout=10, check=False)
            if cp.returncode != 0:
                err = cp.stderr.lower()
                if "not a git repository" in err or "must be run in a work tree" in err:
                    return None
                raise TeamError(f"git rev-parse {flag} failed: {_tail(cp)}")
            if not cp.stdout_bytes.endswith(b"\n") or len(cp.stdout_bytes) < 2:
                raise TeamError(f"git rev-parse {flag} gave an answer levain cannot read")
            return cp.stdout_bytes[:-1]

        top = one("--show-toplevel")
        common = one("--git-common-dir") if top is not None else None
        if top is None or common is None:
            return None
        try:
            return cls(Path(top.decode("utf-8")), Path(common.decode("utf-8")))
        except UnicodeDecodeError:
            # The hooks run only in clones that installed them (joined clones), so this is said, never swallowed.
            raise TeamError("this repository's path is not valid UTF-8, which levain team does not support "
                            "(rename the directory)") from None

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

    # ---- identity ------------------------------------------------------------------------------------------

    def email(self) -> str:
        cp = git(["config", "user.email"], self.repo.toplevel, check=False, timeout=10)
        return cp.stdout.strip()

    # ---- read path: everything is read from the branch REF, never the worktree ------------------------------
    # The ref moves atomically when a rebase or commit completes, so readers need no lock and never see a
    # half-rebased tree. The worktree is only where writes are prepared.

    def head(self) -> str:
        cp = git(["rev-parse", "-q", "--verify", REF], self.repo.toplevel, check=False, timeout=10)
        if cp.returncode != 0:
            raise TeamError(f"this clone has no {BRANCH} branch")
        return cp.stdout.strip()

    def _show(self, path: str, rev: str = REF) -> str | None:  # rev: a commit SHA for a consistent snapshot
        cp = git(["show", f"{rev}:{path}"], self.repo.toplevel, check=False, timeout=30)
        return cp.stdout if cp.returncode == 0 else None

    def team(self, rev: str | None = None) -> R.Team:
        """team.toml at ``rev`` (default: the branch tip); if that version does not parse, the newest one before it
        that does (reported)."""
        rev = rev or REF
        text = self._show("team.toml", rev)
        try:
            return R.parse_team(text or "", "team.toml")
        except R.RolesError as exc:
            first = exc
        cp = git(["log", "--format=%H", rev, "--", "team.toml"], self.repo.toplevel, check=False, timeout=30)
        for sha in cp.stdout.split()[1:]:
            try:
                t = R.parse_team(self._show("team.toml", sha) or "", "team.toml")
            except R.RolesError:
                continue
            self.warnings.append(f"team.toml at the tip is unusable ({first}); using the version from {sha[:10]}")
            return t
        raise first

    def handle(self, team: R.Team | None = None) -> str | None:
        return (team or self.team()).handle_for_email(self.email())

    def snapshot(self) -> tuple[str, R.Team, I.Ledger]:
        """(sha, team, ledger), all read from ONE commit of the ledger branch."""
        sha = self.head()
        team = self.team(sha)
        return sha, team, self.ledger(team, sha)

    def ledger(self, team: R.Team | None = None, rev: str | None = None) -> I.Ledger:
        """The ledger as enforced, rebuilt from history: every line ever ADDED under ledger/, in the order it
        was added, attributed to the author email of the commit that added it.

        Removing or rewriting a line therefore changes nothing (the ledger is append-only by construction;
        the removal is reported). A line whose adding commit is not by the member it is filed under (the owner,
        for pack files) is dropped on its own and reported; the rest of that file still counts. Cached per
        branch tip.
        """
        head = rev or self.head()
        if team is None:
            try:
                team = self.team(head)
            except R.RolesError:
                team = None
        key = f"parser-v5|{head}|" + (R.dump_team(team) if team else "")
        cache = self.base / "history.json"
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if cached.get("key") == key:
                return I.build([(r, l) for r, l in cached["files"]], team.owner if team else None, cached["problems"],
                               tamper=cached["tamper"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        files, problems, tamper = self._history(team, head)
        try:
            tmp = cache.with_suffix(f".tmp{os.getpid()}")
            tmp.write_text(json.dumps({"key": key, "files": sorted(files.items()), "problems": problems,
                                       "tamper": tamper}),
                           encoding="utf-8")
            os.replace(tmp, cache)
        except OSError:
            pass
        return I.build(sorted(files.items()), team.owner if team else None, problems, tamper=tamper)

    def _history(self, team: R.Team | None, rev: str) -> tuple[dict[str, list[str]], list[str], list[str]]:
        """(files, problems, tamper) from the ledger branch, read through git plumbing only.

        Levain owns the ledger namespace: it only ever writes ``ledger/<handle>/<16 hex>.jsonl`` as a regular file.
        Tamper is judged from the TIP TREE alone (``ls-tree -r -z``): any entry under ``ledger/`` that is not that
        grammar, or is not a regular file (a gitlink, a symlink), is tamper, however it got there (a merge
        resolution included), and deleting it clears the refusal. Paths are compared as BYTES; a tamper path or
        gitlink is never read. History only attributes the lines of canonical paths: a non-canonical path in history
        was refused while it existed and is skipped silently. No human display format of git is parsed: commits
        from ``log -z``, paths from ``diff-tree -z --raw`` / ``ls-tree -z``, contents from ``cat-file --batch``.
        """
        top = self.repo.toplevel
        cp = git(["ls-tree", "-r", "-z", "--full-tree", rev, "--", "ledger/"], top, check=False, timeout=30)
        if cp.returncode != 0:
            raise TeamError(f"could not read the ledger tree: {_tail(cp)}")
        bad_paths: list[bytes] = []
        for rec in cp.stdout_bytes.split(b"\0"):
            if not rec:
                continue
            meta, tab, path = rec.partition(b"\t")
            fields = meta.split(b" ")
            if not tab or len(fields) != 3:
                raise TeamError("git ls-tree gave a record levain cannot read")
            if not (_LEDGER_PATH_RE.fullmatch(path) and fields[0] in _REGULAR_MODES and fields[1] == b"blob"):
                bad_paths.append(path)

        cp = git(["log", "-z", "--reverse", "--topo-order", "--full-history", "--no-merges", "--format=%H%x1f%ae",
                  rev, "--", "ledger/"], top, check=False, timeout=60)
        if cp.returncode != 0:
            raise TeamError(f"could not read the ledger history: {_tail(cp)}")
        commits: list[tuple[str, str]] = []
        for rec in cp.stdout_bytes.split(b"\0"):
            rec = rec.strip(b"\n")
            if not rec:
                continue
            sha, sep, mail = rec.partition(b"\x1f")
            if not sep or not _SHA_RE.fullmatch(sha):
                raise TeamError("git log gave a record levain cannot read")
            commits.append((sha.decode("ascii"), mail.decode("utf-8", "replace")))
        cp = git(["rev-list", "--merges", "--full-history", rev, "--", "ledger/"], top, check=False, timeout=30)
        if cp.returncode != 0:
            raise TeamError(f"could not list the ledger's merge commits: {_tail(cp)}")
        merges = cp.stdout.split()

        # sha, mail, path, status, old mode, new mode, old blob, new blob
        changes: list[tuple[str, str, bytes, str, bytes, bytes, str, str]] = []
        last_by: dict[bytes, str] = {}                                    # latest writer of a path, for tamper naming
        for sha, mail, merge in [(s_, m_, False) for s_, m_ in commits] + [(m, "", True) for m in merges]:
            args = ["diff-tree", "-z", "-r", "--raw", "--root", "--no-renames", "--no-commit-id"]
            cp = git(args + (["-m"] if merge else []) + [sha, "--", "ledger/"], top, check=False, timeout=30)
            if cp.returncode != 0:
                raise TeamError(f"could not read commit {sha[:10]}: {_tail(cp)}")
            if merge:
                mail = git(["log", "-1", "--format=%ae", sha], top, timeout=30).stdout.strip()
            parts = cp.stdout_bytes.split(b"\0")
            i = 0
            while i + 1 < len(parts) and parts[i].startswith(b":"):
                meta = parts[i][1:].split(b" ")
                path = parts[i + 1]
                i += 2
                if len(meta) != 5:
                    raise TeamError(f"git diff-tree gave a record levain cannot read in {sha[:10]}")
                if meta[4][:1] != b"D":
                    last_by[path] = f"{mail} in commit {sha[:10]}"
                if merge or not _LEDGER_PATH_RE.fullmatch(path):
                    continue
                old_mode, new_mode = meta[0], meta[1]
                old_blob, new_blob, status = (m.decode("ascii", "replace") for m in meta[2:])
                changes.append((sha, mail, path, status, old_mode, new_mode, old_blob, new_blob))
            if any(parts[i:]) and i < len(parts):
                raise TeamError(f"git diff-tree gave output levain cannot read in {sha[:10]}")

        tamper: list[str] = []
        for path in bad_paths:
            shown = E._printable(" ⏎ ".join(path.decode("utf-8", "backslashreplace").splitlines()))
            who = E._printable(" ⏎ ".join(last_by.get(path, "an author git does not attribute").splitlines()))
            tamper.append(f"{shown!r} (written by {who}) is not a file levain writes")

        # Only regular files of canonical paths are ever read: never a tamper path, never a gitlink.
        def regular(mode: bytes) -> bool:
            return mode in _REGULAR_MODES

        wanted: set[str] = set()
        for _sha, _mail, _path, status, old_mode, new_mode, old_blob, new_blob in changes:
            if status != "A" and regular(old_mode) and set(old_blob) - {"0"}:
                wanted.add(old_blob)
            if status != "D" and regular(new_mode) and set(new_blob) - {"0"}:
                wanted.add(new_blob)
        blobs = self._blobs(wanted)
        by_safe = {E.safe_handle(h): h for h in (team.members if team else {})}
        files: dict[str, list[str]] = {}
        problems: list[str] = []
        stranger: dict[tuple[str, str], int] = {}
        for sha, mail, path, status, old_mode, new_mode, old_blob, new_blob in changes:
            if status != "D" and not regular(new_mode):
                continue                                                  # a gitlink/symlink here is tamper at the tip
            rel = path[len(b"ledger/"):].decode("ascii")
            old = _blob_lines(blobs.get(old_blob, b"")) if status != "A" and regular(old_mode) else []
            new = _blob_lines(blobs.get(new_blob, b"")) if status != "D" else []
            added, removed = _added_removed(old, new)
            gone = [r for r in removed if r.strip()]
            if gone:
                problems.append(f"ledger/{rel}: {len(gone)} line(s) removed or rewritten in commit {sha[:10]} by "
                                f"{mail}; the ledger is append-only, so the original lines still count")
            owner_dir = rel.split("/", 1)[0]
            expected = (team.owner if owner_dir.startswith("pack-") else by_safe.get(owner_dir)) if team else None
            who = team.handle_for_email(mail) if team else None
            for raw in added:
                if not raw.strip():
                    continue
                if team is not None and (expected is None or who != expected):
                    stranger[(rel, mail)] = stranger.get((rel, mail), 0) + 1
                    continue
                files.setdefault(rel, []).append(_text(raw))
        for m_sha in merges:
            problems.append(f"merge commit {m_sha[:10]} touches ledger/: levain keeps the ledger linear, so lines "
                            "that exist only in a merge resolution are not read")
        for (rel, m), n in sorted(stranger.items()):
            problems.append(f"ledger/{rel}: {n} line(s) added by {m} ({team.handle_for_email(m) or 'not a member'}), "
                            f"who is not {rel.split('/', 1)[0]}; those lines are not enforced")
        return files, problems, tamper

    def _blobs(self, shas: set[str]) -> dict[str, bytes]:
        """Blob contents by sha through ``cat-file --batch``: length-framed, so content is never read as framing.
        Every frame is bounds-checked: its size must fit the output, a LF must close it, and nothing may remain."""
        if not shas:
            return {}
        order = sorted(shas)
        cp = git(["cat-file", "--batch"], self.repo.toplevel, input_text="".join(f"{s}\n" for s in order),
                 check=False, timeout=60)
        if cp.returncode != 0:
            raise TeamError(f"could not read ledger contents: {_tail(cp)}")
        out: dict[str, bytes] = {}
        buf, pos = cp.stdout_bytes, 0
        for want in order:
            nl = buf.find(b"\n", pos)
            head = buf[pos:nl].split(b" ") if nl >= 0 else []
            if len(head) != 3 or head[0] != want.encode("ascii") or head[1] != b"blob" or not head[2].isdigit():
                raise TeamError(f"git cat-file gave an answer levain cannot read for {want[:10]}")
            size = int(head[2])
            end = nl + 1 + size
            if end >= len(buf) or buf[end:end + 1] != b"\n":
                raise TeamError(f"git cat-file gave a frame levain cannot read for {want[:10]}")
            out[want] = buf[nl + 1:end]
            pos = end + 1
        if pos != len(buf):
            raise TeamError("git cat-file gave output levain cannot read after the last frame")
        return out

    def team_history_problems(self, team: R.Team, rev: str | None = None) -> list[str]:
        """team.toml and PROJECT.md changes not committed by the owner of the version before them.

        Reported, not enforced: identity here is the commit's author email, which anyone can set, so a check
        would only stop honest mistakes while claiming to stop forgery. Authentication is the git host's job.
        """
        out: list[str] = []
        rev = rev or REF
        # -z: NUL-separated records, never split on a line separator an author field may hold (L3, RUN)
        cp = git(["log", "-z", "--reverse", "--format=%H%x09%ae", rev, "--", "team.toml"], self.repo.toplevel,
                 check=False, timeout=30)
        if cp.returncode != 0:
            raise TeamError(f"could not read the team.toml history: {_tail(cp)}")
        prev: R.Team | None = None
        for row in cp.stdout.split("\0"):
            row = row.strip("\n")
            if not row:
                continue
            sha, _, raw_mail = row.partition("\t")
            mail = E._printable(" ⏎ ".join(raw_mail.splitlines()))   # shown; matching uses raw_mail
            try:
                cur = R.parse_team(self._show("team.toml", sha) or "", "team.toml")
            except R.RolesError:
                out.append(f"team.toml at {sha[:10]} (by {mail}) does not parse")
                continue
            judge = prev or cur
            if judge.handle_for_email(raw_mail) != judge.owner:
                out.append(f"team.toml changed in {sha[:10]} by {mail}, who is not the owner ({judge.owner}) "
                           "of the version before it")
            prev = cur
        cp = git(["log", "-z", "--format=%H%x09%ae", rev, "--", CANON_FILE], self.repo.toplevel, check=False,
                 timeout=30)
        if cp.returncode != 0:
            raise TeamError(f"could not read the {CANON_FILE} history: {_tail(cp)}")
        for row in cp.stdout.split("\0"):
            row = row.strip("\n")
            if not row:
                continue
            sha, _, raw_mail = row.partition("\t")
            mail = E._printable(" ⏎ ".join(raw_mail.splitlines()))
            if team.handle_for_email(raw_mail) != team.owner:
                out.append(f"{CANON_FILE} changed in {sha[:10]} by {mail}, who is not the owner ({team.owner})")
        return out

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
            self._repair_if_moved()   # a copied clone's worktree may still belong to the original repository
            return
        self.base.mkdir(parents=True, exist_ok=True)
        git(["worktree", "prune"], self.repo.toplevel, check=False)
        git(["worktree", "add", "--lock", "--reason", "levain team ledger", str(self.wt), BRANCH],
            self.repo.toplevel)

    def _new_device(self) -> str:
        return self.device or secrets.token_hex(8)

    def init(self, team: R.Team, *, remote: str | None = None, push: bool = True) -> str:
        """Create the ledger branch with team.toml, attach the worktree, push. Returns a status line."""
        R.validate_team(team)
        email = self.email()
        if not email:
            raise TeamError("git config user.email is not set in this repository")
        if team.handle_for_email(email) != team.owner:
            raise TeamError(f"the ledger is created by its owner ({team.owner}); your git user.email ({email}) "
                            "maps to someone else")
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

    def join(self, *, remote: str | None = None, new_device: bool = False) -> str:
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
        self.save_state(device=secrets.token_hex(8) if new_device else self._new_device(), remote=remote or "")
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
        for marker, op in (("rebase-merge", "rebase"), ("rebase-apply", "rebase"), ("CHERRY_PICK_HEAD", "cherry-pick")):
            p = git(["rev-parse", "--git-path", marker], self.wt).stdout.strip()
            if p and (self.wt / p).exists():
                git([op, "--abort"], self.wt, check=False)
        if git(["symbolic-ref", "-q", "HEAD"], self.wt, check=False).returncode != 0:
            # detached: a replay was interrupted before it published. The branch still holds every entry.
            git(["checkout", "-q", "-f", BRANCH], self.wt, timeout=60)
            self.warnings.append("an interrupted sync was rolled back; nothing was lost")
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
                why = I.may_link(entry, target, ledger.owner)
                if why is not None:
                    raise E.EntryError(f"{why}. Record your own entry (refs it with --refs) and ask the owner.")
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
        """Put this clone's unpushed commits on top of the remote, under the worktree lock.

        Entry files are per device, so only team.toml and PROJECT.md can conflict (an owner on two machines).
        The one rule: a local commit touching NOTHING but those two files may be discarded (the owner re-runs
        the command; a warning says so); every commit that touches an entry file is replayed, and if one cannot
        be replayed cleanly the sync stops with the local branch exactly as it was. No side is ever picked.
        """
        with self.lock(timeout=lock_timeout):
            self._recover_dirty()
            orig = git(["rev-parse", "HEAD"], self.wt).stdout.strip()
            try:
                cp = git([*_REPLAY_CONFIG, "rebase", "-q", "--no-verify", "--empty=drop", rref], self.wt,
                         check=False, timeout=timeout)
            except TeamError:
                self._restore(orig)
                raise
            if cp.returncode == 0:
                return
            self._restore(orig)
            # Commits the remote already has under another SHA (--cherry-pick) and merges are not replayed.
            local = git(["rev-list", "--reverse", "--topo-order", "--right-only", "--cherry-pick", "--no-merges",
                         f"{rref}...{orig}"], self.wt).stdout.split()
            keep, drop = [], []
            for c in local:
                touched = set(git(["diff-tree", "--no-commit-id", "--name-only", "-r", c], self.wt).stdout.split())
                (drop if touched and touched <= {"team.toml", CANON_FILE} else keep).append(c)
            # Replay on a DETACHED HEAD: the branch ref stays at `orig` until one compare-and-swap publishes the
            # finished result, so a reader never sees a tip missing local entries and a crash loses nothing.
            published = False
            try:
                git(["checkout", "-q", "--detach", rref], self.wt, timeout=timeout)
                for c in keep:
                    cp = git([*_REPLAY_CONFIG, "cherry-pick", "--allow-empty", c],
                             self.wt, check=False, timeout=timeout)
                    if cp.returncode == 0:
                        continue
                    unmerged = git(["diff", "--name-only", "--diff-filter=U"], self.wt, check=False).stdout.strip()
                    marker = self.wt / git(["rev-parse", "--git-path", "CHERRY_PICK_HEAD"], self.wt).stdout.strip()
                    try:
                        picking = marker.read_text().strip()
                    except OSError:
                        picking = ""
                    # git stops with exit 1 and CHERRY_PICK_HEAD naming THIS commit on an empty pick; any other state
                    # (another exit code, a marker for a different commit, a conflict, staged changes) is a failure
                    if (cp.returncode == 1 and picking == c and not unmerged
                            and git(["diff", "--cached", "--quiet"], self.wt, check=False).returncode == 0):
                        git(["cherry-pick", "--skip"], self.wt, timeout=60)   # already upstream: nothing to add
                        continue
                    raise TeamError("an unpushed entry cannot be replayed onto the remote ledger (two clones "
                                    "share a device id, or git could not run: "
                                    f"{_tail(cp)}). Nothing was changed locally; if this clone's .git was "
                                    "copied from another, see `levain team join --new-device`")
                new = git(["rev-parse", "HEAD"], self.wt).stdout.strip()
                git(["update-ref", "-m", "levain team: replay onto remote", REF, new, orig], self.wt)
                published = True
                if drop:   # the branch now carries the replay, so nothing later can rediscover what was dropped
                    self.warnings.append(f"{len(drop)} local team.toml/PROJECT.md change(s) conflicted with the "
                                         "remote and were discarded; the remote's version stands. Re-run the "
                                         "change (`levain team member add` / `levain team consolidate`) if still "
                                         "wanted.")
                try:
                    git(["checkout", "-q", BRANCH], self.wt, timeout=60)
                except TeamError as exc:
                    try:
                        self._reattach()    # the same checkout, forced
                    except TeamError:
                        raise TeamError(f"the replayed ledger was published to the local branch ({exc}) but the "
                                        f"worktree could not be moved onto it; run `git -C {self.wt} checkout -f "
                                        f"{BRANCH}`") from None
            finally:
                if not published:
                    try:
                        self._reattach()
                    except TeamError as exc:
                        # the replay's own error is the one to raise; say what state this leaves behind
                        self.warnings.append(f"the ledger worktree was left detached ({exc}); the next levain team "
                                             "command re-attaches it")

    def _reattach(self) -> None:
        """Abort any replay in progress and put the worktree back on the branch, wherever the branch points."""
        for op in (["cherry-pick", "--abort"], ["rebase", "--abort"]):
            git(op, self.wt, check=False, timeout=60)
        git(["checkout", "-q", "-f", BRANCH], self.wt, timeout=60)

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
                    try:
                        self._fetch(remote, rref, timeout)
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
        cp = git(["rev-parse", "-q", "--verify", f"{REF}:ledger"], self.repo.toplevel, check=False)
        return cp.stdout.strip() or "empty"

    def read_canon(self, rev: str | None = None) -> str | None:
        return self._show(CANON_FILE, rev or REF)

    @staticmethod
    def state_hash(ledger: I.Ledger, team: R.Team) -> str:
        """What the canon is generated from: the non-ack entries and team.toml. Acks are left out (every agent
        retry writes one), so the canon is not called stale by work that cannot change it."""
        ids = sorted(e["id"] for e in ledger.entries if e.get("type") != "ack")
        state = ["v1", ids, R.dump_team(team), sorted(ledger.problems)]
        return hashlib.sha256(json.dumps(state).encode("utf-8")).hexdigest()[:16]

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

    def update_team(self, change, message: str, *, push: bool = True) -> str:
        """Reload team.toml, apply ``change(team)`` and commit, all under the worktree lock, so two concurrent
        membership changes cannot overwrite each other."""
        self.require_joined()
        with self.lock():
            self._recover_dirty()
            team = R.parse_team((self.wt / "team.toml").read_text(encoding="utf-8"), "team.toml")
            change(team)
            R.validate_team(team)
            (self.wt / "team.toml").write_text(R.dump_team(team), encoding="utf-8")
            git(["add", "--", "team.toml"], self.wt)
            if not git(["diff", "--cached", "--quiet"], self.wt, check=False).returncode:
                return "team.toml unchanged"
            self._commit(message)
        if push and self.remote:
            return self._sync(push=True)
        return "team.toml written locally"

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
