"""levain.dispatch.readset — the read set made physical: a scratch repo holding the contract's read files and nothing else.

A contract names the exact files an executor may read, each bound to a blob sha at ``base``. The slot's bundle step
(``levain.slot.stage.make_bundle``) clones a BRANCH of a repo, so handed the operator's repo it would carry every
tracked file and that branch's whole history. :func:`build_readset` builds the repo it is handed instead: a scratch
repo whose object database receives only the read blobs (copied across by bytes and checked against the contract's
sha), one tree made from them with the modes recorded at base, and ONE parentless commit. What the executor can
reach is then exactly what the contract bound: no other file, no history, no old blob in a pack.

Which contract is built is the caller's word checked against the record: the caller passes the digest the job store
recorded at filing (the one the human signs), and a contract whose own digest is not that one is refused before any
git work. A contract value edited after filing therefore no longer builds, whatever its read list says.

The commit is a pure function of the contract: a fixed identity, a fixed date, no parent, and a message naming the
contract's digest. Two builds of one contract name one sha, so a later stage compares a sha and builds nothing to
trust. The scratch lives under a directory the caller owns and is never the operator's repo; the operator's repo is
only read, under :func:`levain.dispatch.contract.git_env` with no user or system git config added on top (the
scratch's own environment, ``_scratch_env``) and lazy fetch off.

A write path that exists at base is in the read set only when the contract also names it as a read: the contract
binds each list on its own and this module follows it. A write path that is new is created by the executor.
Stdlib only.
"""
from __future__ import annotations

import os
import re
import shutil
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


def _empty(dest_dir: str) -> None:
    # dest_dir was required empty, so everything in it is this call's: a refusal leaves it empty again for a retry
    for name in os.listdir(dest_dir):
        p = os.path.join(dest_dir, name)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        else:
            os.unlink(p)


def build_readset(repo: str, contract: ResolvedContract, dest_dir: str, *, contract_digest: str) -> ReadSet:
    """Build the read set of ``contract`` (read from ``repo``, which must be the contract's own) as a bare scratch repo
    in ``dest_dir`` (an existing, empty directory the caller owns), or raise :class:`ReadSetRefused`.

    ``contract_digest`` is the digest the job store recorded when the contract was filed (``Job.digest``, the one the
    human signs); ``contract`` is refused (``readset-digest-mismatch``) unless its own digest is that one. A refusal
    after the scratch was begun empties ``dest_dir`` again. A source repo owned by another uid is refused
    (``readset-git-failed``): no user git config is read, so a ``safe.directory`` entry there does not apply."""
    if not isinstance(contract, ResolvedContract):
        raise ReadSetRefused("readset-contract", "not a ResolvedContract")
    if not isinstance(contract_digest, str) or contract.digest != contract_digest:
        raise ReadSetRefused("readset-digest-mismatch", "the contract is not the one filed under that digest")
    # the entries built from are decoded from the digest-bound canonical bytes under today's parser, never taken off
    # the caller's object
    try:
        parsed = parse_contract(contract.canonical())
    except ContractRefused as exc:
        raise ReadSetRefused("readset-contract", str(exc)[:200]) from None
    if not isinstance(repo, str) or repo != parsed.repo:
        raise ReadSetRefused("readset-repo", "the repo is not the contract's own")
    try:
        usable = (isinstance(dest_dir, str) and os.path.isabs(dest_dir) and os.path.realpath(dest_dir) == dest_dir
                  and os.path.isdir(dest_dir) and not os.listdir(dest_dir))
    except OSError as exc:
        raise ReadSetRefused("readset-dest", f"dest_dir cannot be read ({type(exc).__name__})") from None
    if not usable:
        raise ReadSetRefused("readset-dest", "dest_dir must be an existing, empty directory (real absolute path)")
    real_repo = os.path.realpath(repo).rstrip("/")
    if dest_dir == real_repo or dest_dir.startswith(real_repo + "/"):
        raise ReadSetRefused("readset-dest", "dest_dir is inside the operator's repo")
    for e in parsed.read:
        if e.mode not in _REGULAR or _SHA1.fullmatch(e.blob) is None:
            raise ReadSetRefused("readset-entry", f"read path {e.path[:120]} is not a regular blob entry")

    env = _scratch_env()
    # a partial clone's missing blob is a refusal, never a network fetch run under the source repo's own config
    source_env = {**env, "GIT_NO_LAZY_FETCH": "1"}
    index = os.path.join(dest_dir, _INDEX)
    ienv = {**env, "GIT_INDEX_FILE": index}
    try:
        _git(dest_dir, "init", "--bare", "-q", "--template=", f"--initial-branch={BRANCH}", ".", env=env)
        try:
            for e in parsed.read:
                body = _git(repo, "cat-file", "blob", e.blob, env=source_env)
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
    except BaseException:
        try:
            _empty(dest_dir)
        except OSError:
            pass    # the refusal stands; a dest_dir left non-empty fails the empty check on any retry
        raise
    return ReadSet(repo_dir=dest_dir, branch=BRANCH, commit=commit, tree=tree)
