"""The git transport: an orphan ``levain-ledger`` branch in the project's own repository.

git is the WIRE, not the memory. The branch holds ``team.toml`` (roles), ``ledger/<author>/<device>.jsonl``
(one append-only, hash-chained file per author PER CLONE, so two clones never write one file and a rebase
can never conflict on an entry) and ``PROJECT.md`` (the generated canon, written only by the owner).

Every read and write goes through a private worktree of that branch kept under
``<git common dir>/levain-team/worktree``: inside ``.git``, so the code branch never tracks it, and under the
COMMON dir, so every ``git worktree`` of the project shares one ledger checkout. Writers hold an exclusive
``flock`` on ``levain-team/lock``; readers take no worktree lock (only the small ``pins.lock`` while they record what
they accepted): they read one commit of the branch (the tip, through git plumbing), never the worktree, so a hook
never sees a half-rebased tree.

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
import threading
import time
from dataclasses import dataclass, field
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


class LedgerReadError(TeamError):
    """git's answer about the ledger's tree or blobs could not be read (an unparseable record, a framing error). The
    ledger cannot be judged, so the edit hook fails CLOSED on this one."""


class TeamBusy(TeamError):
    """A lock was not acquired in time."""


# Every git call here is levain's own plumbing on a private worktree, so none of them runs the project's hooks: git on
# Linux runs a repository's commit, checkout and reference-transaction hooks on exactly these operations, and a hook that
# fails, or rewrites the index, makes a sync fail or a real entry look like an empty pick.
# Nor any attributes but the branch's own (refused unless absent): a user-wide attributes file must not re-encode or
# filter a ledger file levain writes.
_NO_HOOKS = ["-c", "core.hooksPath=/dev/null", "-c", "core.attributesFile=/dev/null"]
# The only names levain writes under ledger/: <handle>/<device>.jsonl (file_for, _new_device). Compared as bytes.
_LEDGER_PATH_RE = re.compile(rb"ledger/[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[0-9a-f]{16}\.jsonl")
_LEDGER_DIR_RE = re.compile(rb"ledger/[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_TOP_FILES = (b"team.toml", b"PROJECT.md")     # with `ledger`, the whole top level of the ledger branch


_INCOMING = "refs/levain/incoming"
_PINS_MAX_BYTES = 8 << 20          # a pins file holds ~120 bytes per ledger file
_READ_ATTEMPTS = 3
_PIN_RACE_TEXT = ("the ledger kept moving while this clone recorded what it accepted (a concurrent read pinned a newer "
                  "tip); nothing was read, try again")


class _PinRace(Exception):
    """The stored pins no longer hold for the bytes being accepted: the tip moved under a concurrent reader."""


@dataclass
class Judgement:
    ledger: I.Ledger                                            # refused when ledger.tamper is non-empty
    datas: dict[str, bytes] = field(default_factory=dict)       # rel -> the accepted bytes (through the last LF)
    files: list[tuple[str, list[str]]] = field(default_factory=list)   # what was built: index.build's input
    problems: list[str] = field(default_factory=list)


_PINS_UNREADABLE = ("pins.json (this clone's local rewrite-protection record) cannot be read or validated; run "
                    "`levain team repin` to start it again")
_NON_UTF8_LINE = "<a line that is not UTF-8>"   # cannot parse as an entry, so it is a reported problem, never an entry
_REGULAR_MODES = (b"100644", b"100755")


def _parse_pins(raw: bytes) -> tuple[dict[str, dict], bool, bool]:
    """(pins, valid, old_format) of a pins.json's bytes. Never raises: anything that is not exactly the shape levain
    writes (``{"<member>/<device>.jsonl": {"sha256": <64 hex>, "length": <int >= 0>}}``) is invalid."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return {}, False, False
    if isinstance(data, dict) and data and all(
            isinstance(v, list) and all(isinstance(x, str) for x in v) for v in data.values()):
        return {}, False, True
    ok = isinstance(data, dict) and all(
        isinstance(k, str) and _LEDGER_PATH_RE.fullmatch(b"ledger/" + k.encode("utf-8", "replace"))
        and isinstance(v, dict) and set(v) == {"sha256", "length"}
        and isinstance(v["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", v["sha256"])
        and isinstance(v["length"], int) and not isinstance(v["length"], bool) and v["length"] >= 0
        for k, v in data.items())
    return (data, True, False) if ok else ({}, False, False)


def _blob_lines(data: bytes) -> list[bytes]:
    """A ledger file's lines as BYTES. Split on LF only (a CR stays inside its line)."""
    lines = data.split(b"\n")
    return lines[:-1] if lines and lines[-1] == b"" else lines


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
    env.update(GIT_TERMINAL_PROMPT="0", LC_ALL="C", GIT_EDITOR="true",
               GIT_NO_REPLACE_OBJECTS="1",   # a replace ref must not change what levain reads
               GIT_ATTR_NOSYSTEM="1")        # nor a system-wide attributes file what it writes
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


_UMASK_LOCK = threading.Lock()


def _umask() -> int:
    """The process umask. Linux reports it in /proc; elsewhere it can only be read by setting it, so that is done
    under a lock and restored at once."""
    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("Umask:"):
                    return int(line.split()[1], 8)
    except (OSError, ValueError):
        pass
    with _UMASK_LOCK:
        mask = os.umask(0o022)
        os.umask(mask)
    return mask


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _atomic_write(path: Path, text: str, *, sync_dir: bool = False) -> None:
    """Write beside the target under a UNIQUE temp name (threads of one process share a pid), fsync, then rename
    into place, so a reader sees the old file or the new one and two writers never share a temp file. ``sync_dir``
    also fsyncs the directory, so the rename itself survives a crash."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    if sync_dir:
        dfd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def require_untampered(ledger) -> None:
    """THE shared refusal: every material reader and writer of the ledger calls this. A tampered ledger (files
    levain never writes) is refused as a whole; nothing is read from it or written to it."""
    if ledger.tamper:
        shown = "; ".join(ledger.tamper[:3]) + (f"; and {len(ledger.tamper) - 3} more" if len(ledger.tamper) > 3 else "")
        raise TeamError(f"the team ledger is REFUSED as tampered: {shown}. Nothing was read or written.")


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
        # ONE git process answers both questions, so they come from one repository discovery: two calls let a
        # directory swapped between them (a retargeted symlink) mix two repositories. rev-parse prints each path on its
        # own line and a path may hold a line break, so the top level is asked twice, as A LF B LF A LF, and the split
        # is accepted only when exactly one reading fits. Only "not a work tree" means None; any other failure (git
        # missing, a timeout, safe.directory, a broken config) raises, so a hook in a joined clone reports it instead
        # of reading "no repository" and going quiet.
        cp = git(["rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir", "--show-toplevel"], d,
                 timeout=10, check=False)
        if cp.returncode != 0:
            err = cp.stderr.lower()
            if "not a git repository" in err or "must be run in a work tree" in err:
                return None
            raise TeamError(f"git rev-parse failed: {_tail(cp)}")
        out = cp.stdout_bytes
        fits = [(out[:k], out[k + 1:len(out) - k - 1]) for k in range(1, len(out)) if out[k:k + 1] == b"\n"
                and len(out) >= 2 * k + 3 and out.endswith(b"\n" + out[:k] + b"\n")]
        fits = [(top, common[:-1]) for top, common in fits if common.endswith(b"\n") and len(common) > 1
                and top.startswith(b"/") and common.startswith(b"/")]
        if len(fits) != 1:
            raise TeamError("git rev-parse gave an answer levain cannot read (this repository's path is ambiguous)")
        top, common = fits[0]
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
        """Read, change and write state.json under ``state.lock``, so two processes never lose each other's
        changes (hooks run concurrently)."""
        self.base.mkdir(parents=True, exist_ok=True)
        with self.lock(name="state.lock", timeout=10.0):
            data = self.state()
            data.update(changes)
            _atomic_write(self.state_path, json.dumps(data, indent=2, sort_keys=True) + "\n")

    @property
    def device(self) -> str:
        dev = str(self.state().get("device", ""))
        if dev and not re.fullmatch(r"[0-9a-f]{16}", dev):
            raise TeamError("this clone's levain team state is invalid (its device id is not 16 hex digits); "
                            "run `levain team join --new-device`")
        return dev

    @property
    def remote(self) -> str | None:
        r = self.state().get("remote")
        return r if isinstance(r, str) and r else None

    def joined(self) -> bool:
        return (self.wt / ".git").exists() and bool(self.device) and os.path.lexists(self.wt / "team.toml")

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
        for _ in range(_READ_ATTEMPTS):
            sha = self.head()
            team = self.team(sha)
            try:
                return sha, team, self._read(team, sha)
            except _PinRace:
                continue                       # the tip moved under a concurrent reader's pin advance: read it again
        raise LedgerReadError(_PIN_RACE_TEXT)

    def ledger(self, team: R.Team | None = None, rev: str | None = None) -> I.Ledger:
        """The ledger as enforced, read from the tip tree of the ledger branch: ``judge`` it, then accept it (the pin
        advance, see ``_accept``). Raises LedgerReadError when the ledger cannot be judged or accepted."""
        for _ in range(_READ_ATTEMPTS if rev is None else 1):
            head = rev or self.head()
            t = team
            if t is None:
                try:
                    t = self.team(head)
                except R.RolesError:
                    t = None
            try:
                return self._read(t, head)
            except _PinRace:
                continue
        raise LedgerReadError(_PIN_RACE_TEXT)

    def _read(self, team: R.Team | None, rev: str) -> I.Ledger:
        """One read: the remote refusal this clone recorded at its last fetch, else the cache, else judge + accept."""
        owner = team.owner if team else None
        refusal = self._incoming_refusal()
        if refusal:
            return I.build([], owner, [], tamper=refusal)
        key = f"parser-v10|{rev}|" + (R.dump_team(team) if team else "")
        cache = self.base / "history.json"
        pins_digest = self._pins_digest()
        if pins_digest is not None:                    # no pins.json = nothing proven pinned: never serve the cache
            try:
                cached = json.loads(cache.read_text(encoding="utf-8"))
                if cached.get("key") == key and cached.get("pins") == pins_digest:
                    return I.build([(r, l) for r, l in cached["files"]], owner, cached["problems"])
            except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
                pass
        pins, pin_problem = self._pins()
        j = self.judge(rev, team, pins, pin_problem)
        if j.ledger.tamper:                            # refused: nothing is pinned, and no content stays cached
            with contextlib.suppress(OSError):
                cache.unlink()
            return j.ledger
        if rev != self.head():                         # only THIS clone's own tip is ever accepted (pinned, cached)
            return j.ledger
        digest = self._accept(j.datas)
        try:
            _atomic_write(cache, json.dumps({"key": key, "pins": digest, "problems": j.problems, "files": j.files}))
        except OSError:
            pass
        return j.ledger

    def judge(self, rev: str, team: R.Team | None, pins: dict[str, dict], pin_problem: str = "") -> "Judgement":
        """THE judgement of the ledger at ``rev``, with no side effect.

        Refused (``ledger.tamper`` non-empty, no entries) when the tip tree holds anything but ``ledger`` (a tree),
        ``ledger/<handle>`` (a tree) and ``ledger/<handle>/<16 hex>.jsonl`` (a regular blob); when ``pins`` are
        unreadable (``pin_problem``) or a pinned file was rewritten or removed; or when one entry id is in two
        files. Otherwise every canonical file is accepted through its last LF: an unterminated last fragment is a
        problem, never an entry, and never pinned, so an append cannot change what an accepted line says. The
        ledger returned is ``index.build`` over exactly those bytes, so what is judged is what is enforced.

        Every git failure here raises LedgerReadError: a ledger that cannot be judged is denied, never allowed.
        """
        try:
            return self._judge(rev, team, pins, pin_problem)
        except LedgerReadError:
            raise
        except TeamError as exc:
            raise LedgerReadError(f"the ledger could not be judged ({exc})") from None

    def _judge(self, rev, team, pins, pin_problem):
        owner = team.owner if team else None
        bad_paths, leaves = self._structure(rev)
        tamper: list[str] = []
        for n, path in enumerate(bad_paths):
            who = ""
            if n < 20:                                                    # names a commit author only
                try:
                    who = git(["log", "-1", "--no-show-signature", "--format=%ae in commit %h", rev, "--",
                               ":(literal)" + path.decode("utf-8")], self.repo.toplevel, timeout=30).stdout.strip()
                except UnicodeDecodeError:
                    pass
            who = E._printable(" ⏎ ".join((who or "an author git does not attribute").splitlines()))
            shown = E._printable(" ⏎ ".join(path.decode("utf-8", "backslashreplace").splitlines()))
            tamper.append(f"{shown!r} (written by {who}) is not a file levain writes; the team owner removes it from "
                          f"the {BRANCH} branch")
        if tamper:                                      # no blob of a structurally refused ledger is read
            return Judgement(I.build([], owner, [], tamper=tamper))
        if pin_problem:
            return Judgement(I.build([], owner, [], tamper=[pin_problem]))
        blobs = self._blobs({sha for _p, sha in leaves})
        whole = {path[len(b"ledger/"):].decode("ascii"): blobs[sha] for path, sha in leaves}
        tamper += self._pin_violations(pins, whole)
        datas: dict[str, bytes] = {}
        files: list[tuple[str, list[str]]] = []
        problems: list[str] = []
        by_safe = {E.safe_handle(h) for h in (team.members if team else {})}
        seen_ids: dict[str, str] = {}
        for rel, data in sorted(whole.items()):
            cut = data.rfind(b"\n") + 1
            datas[rel] = data[:cut]
            if cut < len(data):
                problems.append(f"ledger/{rel}: the last line has no line break, so it is not read (a write was "
                                "cut off, or the file was edited by hand)")
            folder = rel.split("/", 1)[0]
            if team is not None and folder not in by_safe and not folder.startswith("pack-"):
                problems.append(f"ledger/{rel}: filed under {folder}/, who is not a member; those lines are not enforced")
                continue
            lines = [_text(l) for l in _blob_lines(datas[rel])]
            for e in E.verify_lines(lines)[0]:
                other = seen_ids.setdefault(str(e.get("id")), rel)
                if other != rel:
                    tamper.append(f"ledger/{rel} and ledger/{other} both hold entry id {e.get('id')}; levain never "
                                  "writes an id twice (the owner removes the copy)")
            files.append((rel, lines))
        if tamper:
            return Judgement(I.build([], owner, problems, tamper=tamper))
        led = I.build(files, owner, problems)
        # The cross-entry checks `levain team verify` has always made, here so every reader reports them (a
        # `supersedes` to a missing id is already one of the build's link problems).
        extra = []
        for e in led.entries:
            extra += [f"{e['id']}: names {r}, which is not in the ledger" for r in e.get("refs", [])
                      if r not in led.by_id]
            if team is not None and e.get("owner") and not team.owner_ok(e["owner"]):
                extra.append(f"{e['id']}: owner {e['owner']!r} is not allowed by team.toml")
        if extra:
            problems += extra
            led = I.build(files, owner, problems)
        return Judgement(led, datas, files, problems)

    def _structure(self, rev: str) -> tuple[list[bytes], list[tuple[bytes, str]]]:
        """(bad_paths, leaves) of the ledger branch at ``rev``: one ``ls-tree -r -t -z`` of the WHOLE tree, judged as
        bytes. Levain owns the branch's namespace: its top level is exactly ``team.toml`` and ``PROJECT.md`` (regular
        files) and ``ledger`` (a tree), so anything else there (a ``.gitattributes`` that would re-encode or filter
        what levain writes, a ``.gitmodules``, any other file) is tamper too. No blob is read."""
        cp = git(["ls-tree", "-r", "-t", "-z", "--full-tree", rev], self.repo.toplevel, check=False, timeout=30)
        if cp.returncode != 0:
            raise LedgerReadError(f"could not read the ledger tree: {_tail(cp)}")
        bad_paths: list[bytes] = []
        leaves: list[tuple[bytes, str]] = []
        seen_paths: set[bytes] = set()
        for rec in cp.stdout_bytes.split(b"\0"):
            if not rec:
                continue
            meta, tab, path = rec.partition(b"\t")
            fields = meta.split(b" ")
            if not tab or len(fields) != 3 or not re.fullmatch(rb"[0-9a-f]{40}|[0-9a-f]{64}", fields[2]):
                raise LedgerReadError("git ls-tree gave a record levain cannot read")
            mode, kind = fields[0], fields[1]
            if path in seen_paths:                                        # two entries, one path: never valid
                bad_paths.append(path)
                continue
            seen_paths.add(path)
            is_tree = mode == b"040000" and kind == b"tree"
            if path in _TOP_FILES:
                ok = mode in _REGULAR_MODES and kind == b"blob"
            elif path == b"ledger" or _LEDGER_DIR_RE.fullmatch(path):
                ok = is_tree
            elif mode in _REGULAR_MODES and kind == b"blob" and _LEDGER_PATH_RE.fullmatch(path):
                ok = True
                leaves.append((path, fields[2].decode("ascii")))
            else:
                ok = False
            if not ok:
                bad_paths.append(path)
        return bad_paths, leaves

    # ---- rewrite protection: this clone's pins (trust on first use) ---------------------------------------------
    # A pin is the sha256 and length of the exact bytes of a ledger file this clone accepted (through its last LF).
    # It lives beside state.json, never in the ledger branch.

    def _pins_bytes(self) -> bytes | None:
        """pins.json's bytes, None when it is missing. Raises OSError when it cannot be read, ValueError when it is
        larger than _PINS_MAX_BYTES (a pins file grows by ~120 bytes per ledger file)."""
        with open(self.base / "pins.json", "rb") as fh:
            data = fh.read(_PINS_MAX_BYTES + 1)
        if len(data) > _PINS_MAX_BYTES:
            raise ValueError("too large")
        return data

    def _pins_digest(self) -> str | None:
        try:
            data = self._pins_bytes()
        except (OSError, ValueError):
            return None
        return hashlib.sha256(data).hexdigest()

    def _pins(self) -> tuple[dict[str, dict], str]:
        """(pins, problem). A MISSING pins.json is the trust-on-first-use start, and so is one in the old
        list-of-lines format (a one-time restart, as `levain team repin` would do). One that exists and cannot be
        read or validated is a refusal naming the file and `levain team repin`."""
        try:
            raw = self._pins_bytes()
        except FileNotFoundError:
            return {}, ""
        except (OSError, ValueError):
            return {}, _PINS_UNREADABLE
        pins, ok, old = _parse_pins(raw)
        if old:
            return {}, ""                                             # the old format: restart pinning
        return (pins, "") if ok else ({}, _PINS_UNREADABLE)

    def seed_pins_from(self, path: Path) -> dict[str, dict]:
        """A teammate's pins.json, validated as this clone's own would be (size cap, shape), for ``join``."""
        try:
            with open(path, "rb") as fh:
                raw = fh.read(_PINS_MAX_BYTES + 1)
        except OSError as exc:
            raise TeamError(f"--pins-from {path}: cannot be read ({exc.strerror})") from None
        pins, ok, old = _parse_pins(raw) if len(raw) <= _PINS_MAX_BYTES else ({}, False, False)
        if not ok or old:
            raise TeamError(f"--pins-from {path}: not a levain pins file (a clone's .git/{DIRNAME}/pins.json)")
        return pins

    @staticmethod
    def _pin_violations(pins: dict[str, dict], datas: dict[str, bytes]) -> list[str]:
        out = []
        for rel, pin in sorted(pins.items()):
            data = datas.get(rel)
            if data is None or len(data) < pin["length"] or \
                    hashlib.sha256(data[:pin["length"]]).hexdigest() != pin["sha256"]:
                out.append(f"ledger/{rel} was rewritten or removed on the team ledger after this clone accepted it. "
                           f"To recover: the team owner restores the file on the {BRANCH} branch, or, once someone "
                           "has checked that the rewrite is intended, run `levain team repin` in this clone and then "
                           "`levain team sync`")
        return out

    def _accept(self, datas: dict[str, bytes]) -> str:
        """THE acceptance transaction of a judged read: pin every file's accepted bytes, under ``pins.lock``. The
        stored pins are re-read there; if they no longer hold for these bytes (a concurrent reader pinned a newer
        tip), _PinRace (the caller reads the tip again). If the pins cannot be durably saved, the
        read is refused (LedgerReadError). Returns the digest of pins.json as saved."""
        new = {rel: {"sha256": hashlib.sha256(d).hexdigest(), "length": len(d)} for rel, d in datas.items()}
        try:
            with self.lock(name="pins.lock", timeout=5.0):
                stored, bad = self._pins()
                if bad or self._pin_violations(stored, datas):
                    raise _PinRace()
                text = json.dumps(new, sort_keys=True)
                digest = self._pins_digest()
                if stored != new or digest is None or digest != hashlib.sha256(text.encode()).hexdigest():
                    _atomic_write(self.base / "pins.json", text, sync_dir=True)
                    digest = hashlib.sha256(text.encode()).hexdigest()
                return digest
        except (TeamBusy, OSError) as exc:
            raise LedgerReadError(f"this clone's pins (its record of what it accepted) could not be saved ({exc}); "
                                  "nothing was read") from None

    def repin(self, rel: str | None = None) -> list[str]:
        """Drop this clone's pins (all, or one file) under the pins lock and return what was dropped; rewrite
        protection for those files restarts at the next read. A failure to persist is an error."""
        path = self.base / "pins.json"
        self.base.mkdir(parents=True, exist_ok=True)
        with self.lock(name="pins.lock", timeout=10.0):
            pins, bad = self._pins()
            if rel is None:
                dropped = sorted(pins) or (["pins.json (it was unreadable)"] if bad else [])
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise TeamError(f"could not drop the pins: {exc.strerror}") from None
                return dropped
            if bad:
                raise TeamError("pins.json is unreadable, so one file cannot be picked out of it; "
                                "run `levain team repin` to drop them all")
            rel = rel[len("ledger/"):] if rel.startswith("ledger/") else rel
            if rel not in pins:
                return []
            del pins[rel]
            try:
                _atomic_write(path, json.dumps(pins, sort_keys=True), sync_dir=True)
            except OSError as exc:
                raise TeamError(f"could not save the pins: {exc.strerror}") from None
            return [rel]

    def _blobs(self, shas: set[str]) -> dict[str, bytes]:
        """Blob contents by sha through ``cat-file --batch``: length-framed, so content is never read as framing.
        Every frame is bounds-checked: its size must fit the output, a LF must close it, and nothing may remain."""
        if not shas:
            return {}
        order = sorted(shas)
        cp = git(["cat-file", "--batch"], self.repo.toplevel, input_text="".join(f"{s}\n" for s in order),
                 check=False, timeout=60)
        if cp.returncode != 0:
            raise LedgerReadError(f"could not read ledger contents: {_tail(cp)}")
        out: dict[str, bytes] = {}
        buf, pos = cp.stdout_bytes, 0
        for want in order:
            nl = buf.find(b"\n", pos)
            head = buf[pos:nl].split(b" ") if nl >= 0 else []
            if len(head) != 3 or head[0] != want.encode("ascii") or head[1] != b"blob" or not head[2].isdigit():
                raise LedgerReadError(f"git cat-file gave an answer levain cannot read for {want[:10]}")
            size = int(head[2])
            end = nl + 1 + size
            if end >= len(buf) or buf[end:end + 1] != b"\n":
                raise LedgerReadError(f"git cat-file gave a frame levain cannot read for {want[:10]}")
            out[want] = buf[nl + 1:end]
            pos = end + 1
        if pos != len(buf):
            raise LedgerReadError("git cat-file gave output levain cannot read after the last frame")
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

    def join(self, *, remote: str | None = None, new_device: bool = False, pins_from: Path | None = None) -> str:
        """Join the team ledger on the remote. With ``pins_from`` (a teammate's pins.json) this clone starts from
        those pins instead of trusting what it sees first: a ledger that does not hold every byte they pin refuses the
        join, naming the file. Without it, the first read is trusted, and state.json records that (``first_sight``)."""
        email = self.email()
        if not email:
            raise TeamError("git config user.email is not set in this repository")
        seed = self.seed_pins_from(pins_from) if pins_from is not None else None
        remote = remote or self._default_remote()
        if self._local_branch_exists():
            tip = self.head()
        else:
            if not remote:
                raise TeamError("no git remote to join from")
            git(["fetch", "-q", "--refmap=", remote, f"+refs/heads/{BRANCH}:refs/remotes/{remote}/{BRANCH}"],
                self.repo.toplevel, timeout=120)
            tip = git(["rev-parse", "-q", "--verify", f"refs/remotes/{remote}/{BRANCH}"], self.repo.toplevel
                      ).stdout.strip()
        if seed is not None:
            bad = self.judge(tip, self._team_or_none(tip), seed).ledger.tamper
            if bad:
                raise TeamError(f"--pins-from {pins_from}: the team ledger here does not hold what it pins, so this "
                                "clone did not join: " + "; ".join(bad[:3]))
        if not self._local_branch_exists():
            git(["branch", BRANCH, tip], self.repo.toplevel)
        if seed is not None:
            self.base.mkdir(parents=True, exist_ok=True)
            with self.lock(name="pins.lock", timeout=10.0):
                _atomic_write(self.base / "pins.json", json.dumps(seed, sort_keys=True), sync_dir=True)
        self.save_state(device=secrets.token_hex(8) if new_device else self._new_device(), remote=remote or "",
                        first_sight=seed is None)
        self._attach_worktree()
        team = self.team()
        handle = team.handle_for_email(email)
        if handle is None:
            raise TeamError(f"joined, but your git user.email ({email}) is not a member of {team.project}: "
                            f"ask the owner ({team.owner}) to run `levain team member add <handle> {email}`")
        if remote:
            self._sync(push=False)
        line = f"joined {team.project} as {handle} (device {self.device})"
        if seed is None:
            line += ("\nfirst sight trusted: this clone pins whatever the ledger holds now; to verify, re-join with "
                     f"--pins-from <a teammate's .git/{DIRNAME}/pins.json>")
        return line

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

    def _open_own_file(self, handle_dir: str, name: str) -> int:
        """Open ``ledger/<handle_dir>/<name>`` in the worktree for appending, walking one directory at a time from an
        open descriptor (``dir_fd``) with O_NOFOLLOW at every step: a component that is, or is swapped for, a
        symlink fails the open instead of being followed, so no check-then-use window exists between a test of a
        parent directory and the write. The walk starts at the worktree, which sits inside levain's own state directory."""
        rel = f"ledger/{handle_dir}/{name}"
        fds: list[int] = []
        try:
            fds.append(os.open(self.wt, os.O_RDONLY | os.O_DIRECTORY))
            for comp in ("ledger", handle_dir):
                try:
                    os.mkdir(comp, 0o777, dir_fd=fds[-1])
                except FileExistsError:
                    pass
                fds.append(os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1]))
            return os.open(name, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o666, dir_fd=fds[-1])
        except OSError as exc:
            raise TeamError(f"{rel} in the ledger worktree cannot be opened for writing ({exc.strerror}); levain "
                            "never writes through a symlink or a non-directory") from None
        finally:
            for fd in fds:
                os.close(fd)

    def append(self, entry: dict, *, push: bool = True, lock_timeout: float = 30.0) -> dict:
        """Validate, seal and append one entry to this author's file for this clone; commit; push.

        A refusal (EntryError) writes nothing. A push failure leaves the entry committed locally and
        raises TeamError saying so; the next write or `levain team sync` pushes it.
        """
        self.require_joined()
        with self.lock(timeout=lock_timeout):
            require_untampered(self.ledger())           # judged BEFORE recovery commits anything ...
            self._recover_dirty()
            ledger = self.ledger()
            require_untampered(ledger)                  # ... and again after it
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
            line = json.dumps(sealed, ensure_ascii=False, sort_keys=True) + "\n"
            fd = self._open_own_file(path.parent.name, path.name)
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
                    raise TeamError(f"{path.relative_to(self.wt).as_posix()} in the ledger worktree is not a plain "
                                    "single-link file; refusing to write to it")
                data = line.encode("utf-8")
                if st.st_size > 0 and os.pread(fd, 1, st.st_size - 1) != b"\n":
                    data = b"\n" + data  # a torn last line stays torn (and reported); it must not swallow this one
                _write_all(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
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

    def _team_or_none(self, rev: str) -> R.Team | None:
        try:
            return self.team(rev)
        except R.RolesError:
            return None

    def _rref(self) -> str | None:
        return f"refs/remotes/{self.remote}/{BRANCH}" if self.remote else None

    def _incoming(self) -> str | None:
        remote = self.remote
        if not remote:
            return None
        cp = git(["remote", "get-url", remote], self.repo.toplevel, check=False, timeout=10)
        url = cp.stdout.strip() if cp.returncode == 0 and cp.stdout.strip() else remote   # a URL or path remote
        key = hashlib.sha256((url + "\0" + BRANCH).encode("utf-8")).hexdigest()[:16]
        return f"{_INCOMING}/{key}"

    def _ref_sha(self, ref: str | None) -> str | None:
        if not ref:
            return None
        cp = git(["rev-parse", "-q", "--verify", f"{ref}^{{commit}}"], self.repo.toplevel, check=False, timeout=10)
        return cp.stdout.strip() or None if cp.returncode == 0 else None

    def remote_ref(self) -> str | None:
        """The remote's ledger tip as this clone last ACCEPTED it (the remote-tracking ref), or None. A refused fetch
        never moves it, so it is safe to render after ``judge_remote``."""
        return self._ref_sha(self._rref())

    def _incoming_refusal(self) -> list[str]:
        """The reasons the quarantined remote tip is refused, or [] when there is none or it is now accepted."""
        try:
            sha = self._ref_sha(self._incoming())
            if sha is None:
                return []
            bad = self.judge_remote(sha).ledger.tamper
        except TeamError as exc:
            raise LedgerReadError(f"the fetched remote ledger could not be judged ({exc})") from None
        return [f"the REMOTE team ledger ({self.remote}) is refused: {t}" for t in bad]

    def _own_rel(self, rel: str, handle: str | None) -> bool:
        """A file only THIS clone writes: its device id, under its member's folder or a pack folder it seeded."""
        folder, _, name = rel.partition("/")
        return name == f"{self.device}.jsonl" and (folder.startswith("pack-") or
                                                   (handle is not None and folder == E.safe_handle(handle)))

    def _whole(self, rev: str | None, keep) -> dict[str, bytes]:
        """The whole bytes at ``rev`` of the canonical ledger files ``keep(rel)`` selects ({} for no rev)."""
        if not rev:
            return {}
        _bad, leaves = self._structure(rev)
        leaves = [(p[len(b"ledger/"):].decode("ascii"), sha) for p, sha in leaves]
        leaves = [(rel, sha) for rel, sha in leaves if keep(rel)]
        blobs = self._blobs({sha for _r, sha in leaves})
        return {rel: blobs[sha] for rel, sha in leaves}

    def judge_remote(self, rev: str) -> Judgement:
        """The judgement of a REMOTE tip, side-effect free (nothing is pinned or cached; pins advance only when this
        clone reads its own tip). Every other file is judged with this clone's pins; this clone's OWN files (see
        ``_own_rel``) are judged against what this clone wrote: each must still hold every byte the last accepted
        remote tip held (so no repin can adopt a truncation of them), and may lack only this clone's unpushed lines
        (so a line this clone never wrote is refused). A team.toml that does not parse refuses the tip."""
        try:
            try:
                team = self.team(rev)
            except R.RolesError as exc:
                return Judgement(I.build([], None, [], tamper=[f"team.toml does not parse ({exc}); the team owner "
                                                                  "fixes it"]))
            local = self.head()
            handle = self.handle(self.team(local))
            own = lambda rel: self._own_rel(rel, handle)  # noqa: E731
            pins, problem = self._pins()
            j = self.judge(rev, team, {r: p for r, p in pins.items() if not own(r)}, problem)
            if j.ledger.tamper:
                return j
            floor, mine, theirs = self._whole(self._ref_sha(self._rref()), own), self._whole(local, own), \
                self._whole(rev, own)
            bad = []
            for rel in sorted(set(floor) | set(mine) | set(theirs)):
                f, m, t = floor.get(rel, b""), mine.get(rel, b""), theirs.get(rel, b"")
                if not t.startswith(f):
                    bad.append(f"this clone's own ledger/{rel} lost lines it had already pushed; this clone's "
                               f"{BRANCH} branch still holds them: the owner restores the remote's file from it")
                elif not m.startswith(t):
                    bad.append(f"ledger/{rel} holds lines this clone never wrote (only this clone writes that file): "
                               "the owner removes them from the remote (if this clone's .git was copied from another "
                               "clone, that clone wrote them: see `levain team join --new-device`)")
            return Judgement(I.build([], team.owner, [], tamper=bad)) if bad else j
        except LedgerReadError:
            raise
        except (TeamError, R.RolesError) as exc:
            raise LedgerReadError(f"the remote ledger could not be judged ({exc})") from None

    def _fetch_quarantined(self, remote: str, timeout: float) -> bool:
        """Fetch the remote's ledger tip into the quarantine ref (git verifies every object it receives), judge it,
        and only when it is accepted make it the remote-tracking ref and drop the quarantine ref. False when the
        remote has no ledger branch. A refused tip raises TeamError and stays quarantined (the record)."""
        inc, rref, top = self._incoming(), self._rref(), self.repo.toplevel
        # --refmap= : a command-line fetch otherwise ALSO updates the configured remote-tracking ref ("opportunistic"
        # update), which would put a refused tip exactly where the quarantine keeps it out of.
        cp = git(["-c", "fetch.fsckObjects=true", "fetch", "-q", "--no-write-fetch-head", "--refmap=", remote,
                  f"+refs/heads/{BRANCH}:{inc}"], top, timeout=timeout, check=False)
        if cp.returncode != 0:
            if "couldn't find remote ref" in (cp.stderr or ""):
                return False
            raise TeamError(f"git fetch failed: {_tail(cp)}")
        now = time.time()
        self.save_state(last_fetch_attempt=now, last_fetch_ok=now, last_fetch_error="")
        sha = self._ref_sha(inc)
        bad = self.judge_remote(sha).ledger.tamper
        if bad:
            raise TeamError(f"the REMOTE team ledger ({remote}) is refused: " + "; ".join(bad[:3])
                            + ". Nothing was replayed or pushed; every edit here is denied until a sync finds the "
                            "remote accepted.")
        git(["update-ref", rref, sha], top)
        git(["update-ref", "-d", inc, sha], top, check=False)
        return True

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
                if not self._fetch_quarantined(remote, timeout):
                    if not push:
                        return f"{remote} has no {BRANCH} branch yet"
                else:
                    self._rebase(rref, timeout, lock_timeout)
                    if not push:
                        return "fetched"
                    ahead = git(["rev-list", "--count", f"{rref}..{local}"], self.wt).stdout.strip()
                    if ahead == "0":
                        return "up to date"
                pins, problem = self._pins()          # the local tip is judged before every push
                head = self.head()
                bad = self.judge(head, self._team_or_none(head), pins, problem).ledger.tamper
                if bad:
                    raise TeamError("this clone's own ledger is refused, so nothing was pushed: " + "; ".join(bad[:3]))
                cp = git(["push", "-q", "--no-verify", remote, f"{local}:{local}"], self.repo.toplevel,
                         check=False, timeout=timeout)
                if cp.returncode == 0:
                    try:
                        self._fetch_quarantined(remote, timeout)
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

    def fetch_only(self, *, interval: float, timeout: float) -> str | None:
        """Fetch and judge the remote (``_fetch_quarantined``) WITHOUT replaying anything onto the local branch:
        what a read-only surface (the team view) runs. No-op without a remote or when the last attempt is younger
        than ``interval`` seconds. None when nothing needed doing, another process holds the network lock, or the
        fetch was accepted; else a one-line reason. Never raises."""
        try:
            remote = self.remote
            if not remote:
                return None
            if time.time() - float(self.state().get("last_fetch_attempt") or 0) < interval:
                return None
            self.save_state(last_fetch_attempt=time.time())
            try:
                with self.lock(name="net", timeout=0.5):
                    if not self._fetch_quarantined(remote, timeout):
                        return "remote has no ledger branch"
            except TeamBusy:
                return None
            return None
        except TeamError as exc:
            with contextlib.suppress(Exception):
                self.save_state(last_fetch_error=str(exc))
            return str(exc)
        except Exception as exc:  # noqa: BLE001 - a read-only surface: report, never raise
            return f"{type(exc).__name__}: {exc}"

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
            with contextlib.suppress(Exception):
                self.save_state(last_fetch_error=str(exc))
            return str(exc)
        except Exception as exc:  # noqa: BLE001 - a hook path: report, never raise
            return f"{type(exc).__name__}: {exc}"

    # ---- canon ---------------------------------------------------------------------------------------------

    def read_canon(self, rev: str | None = None) -> str | None:
        return self._show(CANON_FILE, rev or REF)

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
                os.fchmod(fh.fileno(), 0o666 & ~_umask())   # what git's own checkout would give it
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
        try:
            fh = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)                                 # fdopen did not take the descriptor: close it here
            raise
        with fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                raise TeamError(f"{name} in the team worktree is not a regular file (a link or other entry was "
                                f"committed to the ledger branch); levain will not read or write through it")
            try:
                return fh.read().decode("utf-8")
            except UnicodeDecodeError:
                raise TeamError(f"{name} in the team worktree is not valid UTF-8; the team owner fixes it on the "
                                f"{BRANCH} branch") from None

    def _write_file(self, name: str, text: str, message: str, push: bool) -> str:
        self.require_joined()
        with self.lock():
            require_untampered(self.ledger())           # judged INSIDE the lock, before recovery and after it
            self._recover_dirty()
            require_untampered(self.ledger())
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
        """Reload team.toml, apply ``change(team)`` and commit, all under the worktree lock, so two concurrent
        membership changes cannot overwrite each other."""
        self.require_joined()
        with self.lock():
            require_untampered(self.ledger())           # judged INSIDE the lock, before recovery and after it
            self._recover_dirty()
            require_untampered(self.ledger())
            team = R.parse_team(self._read_plain("team.toml"), "team.toml")
            change(team)
            R.validate_team(team)
            self._replace_plain("team.toml", R.dump_team(team))
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
