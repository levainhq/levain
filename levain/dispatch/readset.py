"""levain.dispatch.readset — the read set made physical: a scratch repo holding the contract's read files and nothing else.

A contract names the exact files an executor may read, each bound to a blob sha at ``base``. The slot's bundle step
(``levain.slot.stage.make_bundle``) clones a BRANCH of a repo, so handed the operator's repo it would carry every
tracked file and that branch's whole history. :func:`build_readset` builds the repo it is handed instead: a scratch
repo whose object database receives only the read blobs (copied across by bytes and checked against the contract's
sha), one tree made from them with the modes recorded at base, and ONE parentless commit. What the executor can
reach is then exactly what the contract bound: no other file, no history, no old blob in a pack.

The commit is a pure function of the contract: a fixed identity, a fixed date, no parent, and a message naming the
contract's digest. Two builds of one contract name one sha, so a later stage compares a sha and builds nothing to
trust. The scratch lives under a directory the caller owns and is never the operator's repo; the operator's repo is
only read, through :func:`levain.dispatch.contract.git_env`, the environment the contract was resolved under.

A write path that exists at base is in the read set only when the contract also names it as a read: the contract
binds each list on its own and this module follows it. A write path that is new is created by the executor.
Stdlib only.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

from levain.dispatch import ContractRefused, DispatchError
from levain.dispatch.contract import GIT_TIMEOUT, ResolvedContract, git_env, parse_contract

__all__ = ["BRANCH", "ReadSet", "ReadSetRefused", "build_readset"]

#: The branch the scratch repo's one commit sits on; the supervisor bundles this name.
BRANCH = "readset"
_IDENTITY = "levain-dispatch"
_EMAIL = "levain-dispatch@localhost"
_DATE = "@0 +0000"   # the epoch; git refuses a bare "0"
_REGULAR = frozenset({"100644", "100755"})
_SHA1 = re.compile(r"[0-9a-f]{40}")
_INDEX = "readset.index"


class ReadSetRefused(DispatchError):
    """A read set that cannot be built, by name (``name``) with a human ``detail``. Nothing was handed on."""

    def __init__(self, name: str, detail: str = "") -> None:
        self.name = name
        self.detail = detail
        super().__init__(f"{name}: {detail}" if detail else name)


@dataclass(frozen=True)
class ReadSet:
    """The built scratch repo: its directory, the branch and commit sha, and the commit's tree sha."""
    repo_dir: str
    branch: str
    commit: str
    tree: str


def _git(cwd: str, *args: str, env: dict[str, str], data: bytes | None = None) -> bytes:
    argv = ["git", "--no-replace-objects", "-c", "core.hooksPath=/dev/null", "-C", cwd, *args]
    try:
        cp = subprocess.run(argv, capture_output=True, timeout=GIT_TIMEOUT, env=env,
                            input=data, stdin=None if data is not None else subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReadSetRefused("readset-git-unavailable", f"git could not run ({type(exc).__name__})") from None
    if cp.returncode != 0:
        tail = (cp.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise ReadSetRefused("readset-git-failed", f"git {args[0]}: {tail[-1][:200] if tail else cp.returncode}")
    return cp.stdout


def _scratch_env() -> dict[str, str]:
    # DECISION: no user git config reaches the scratch (a signing or template setting there would change the commit
    # and break "one contract, one sha"); the identity and date are fixed here, after git_env scrubbed every GIT_*.
    env = git_env()
    env.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": _IDENTITY, "GIT_AUTHOR_EMAIL": _EMAIL,
                "GIT_AUTHOR_DATE": _DATE, "GIT_COMMITTER_NAME": _IDENTITY, "GIT_COMMITTER_EMAIL": _EMAIL,
                "GIT_COMMITTER_DATE": _DATE})
    return env


def build_readset(repo: str, contract: ResolvedContract, dest_dir: str) -> ReadSet:
    """Build the read set of ``contract`` (read from ``repo``, which must be the contract's own) as a bare scratch repo
    in ``dest_dir`` (an existing, empty directory the caller owns), or raise :class:`ReadSetRefused`."""
    if not isinstance(contract, ResolvedContract):
        raise ReadSetRefused("readset-contract", "not a ResolvedContract")
    # the contract is re-parsed from its own canonical bytes: a value that was edited after filing no longer parses
    # to itself, and the entries built from are the parsed ones, never the caller's
    try:
        parsed = parse_contract(contract.canonical())
    except ContractRefused as exc:
        raise ReadSetRefused("readset-contract", str(exc)[:200]) from None
    if parsed != contract or parsed.digest != contract.digest:
        raise ReadSetRefused("readset-contract", "the contract does not parse to itself")
    if not isinstance(repo, str) or repo != parsed.repo:
        raise ReadSetRefused("readset-repo", "the repo is not the contract's own")
    if (not isinstance(dest_dir, str) or not os.path.isabs(dest_dir) or os.path.realpath(dest_dir) != dest_dir
            or not os.path.isdir(dest_dir) or os.listdir(dest_dir)):
        raise ReadSetRefused("readset-dest", "dest_dir must be an existing, empty directory (real absolute path)")
    if dest_dir == repo or dest_dir.startswith(repo.rstrip("/") + "/"):
        raise ReadSetRefused("readset-dest", "dest_dir is inside the operator's repo")
    for e in parsed.read:
        if e.mode not in _REGULAR or _SHA1.fullmatch(e.blob) is None:
            raise ReadSetRefused("readset-entry", f"read path {e.path[:120]} is not a regular blob entry")

    env = _scratch_env()
    _git(dest_dir, "init", "--bare", "-q", "--template=", f"--initial-branch={BRANCH}", ".", env=env)
    index = os.path.join(dest_dir, _INDEX)
    ienv = {**env, "GIT_INDEX_FILE": index}
    try:
        for e in parsed.read:
            body = _git(repo, "cat-file", "blob", e.blob, env=env)
            got = _git(dest_dir, "hash-object", "-w", "--stdin", env=env, data=body).decode("ascii").strip()
            if got != e.blob:
                raise ReadSetRefused("readset-blob-mismatch", f"{e.path[:120]}: the bytes hash to {got[:12]}, "
                                     f"the contract binds {e.blob[:12]}")
            _git(dest_dir, "update-index", "--add", "--cacheinfo", f"{e.mode},{e.blob},{e.path}", env=ienv)
        tree = _git(dest_dir, "write-tree", env=ienv).decode("ascii").strip()
    finally:
        try:
            os.unlink(index)
        except FileNotFoundError:
            pass
    commit = _git(dest_dir, "commit-tree", tree, "-m", f"levain read set {parsed.digest}",
                  env=env).decode("ascii").strip()
    _git(dest_dir, "update-ref", f"refs/heads/{BRANCH}", commit, env=env)
    return ReadSet(repo_dir=dest_dir, branch=BRANCH, commit=commit, tree=tree)
