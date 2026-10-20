"""levain.dispatch.contract — a planner's task contract, resolved, refused whole or content-addressed.

A planner submits a contract (:func:`resolve_contract`): a registered ``template``, a ``goal``, the exact files the
executor may ``read`` and ``write``, a ``base`` commit, a ``budget``, the typed tool classes it is ``grant``ed, an
``expires_at`` and the ``repo``. Levain resolves it and either refuses it WHOLE (:class:`ContractRefused`, the
message says why, nothing is filed) or returns a :class:`ResolvedContract` whose canonical bytes the human signs, by
digest, once.

Every path resolves FROM GIT AT ``base``, never from the working tree, so what the human is shown is what the executor
and the verifier will see: a read path must be a regular file at base (a symlink, a submodule or a missing path refuses
the contract) and its blob is bound; a write path is either a regular file at base (its blob bound) or a NEW file whose
every ancestor is a plain directory or absent at base, and neither it nor any ancestor has a twin at base that differs
only in case (the target disk is case-insensitive). A regular mode must name a blob. No write path is an ancestor of
another, compared without case: one path cannot be both a file and a directory. Git runs with an argv list and :func:`git_env`, never a
shell: every inherited ``GIT_*`` variable scrubbed, replace objects and graft files off, so ``base`` names its own
tree and no ref or graft can swap another in. The contract binds that tree's sha (``base_tree``) beside ``base``, so
the stages that later build and check a worktree compare one sha, with the same :func:`git_env`. The listing is read
once, whole, so no pathspec rule shapes the answer.

The template is resolved through an interface (``resolve_template``): tier, executor, the distinct model list, ``k``
and the verifier come FROM the template, never from the planner, since a model does not choose its own gate
thickness. An unknown template, or a write path outside the template's exact write set, makes the contract R2, and R2
is never dispatched. The provider policy (:mod:`levain.dispatch.policy`) then judges the resolved sets for the
template's provider. Bash, or any shell-ish tool class, is never grantable: a shell command is never parsed, so no
grant can scope one.

:func:`parse_contract` reads a stored contract back and refuses any text that is not the canonical form of what it
parses to, so one contract has one digest. Parsing re-checks shape, not the world: whether ``base``, the blobs, the
template and the policy still hold is revalidation, owned by the stages that run and adopt. Stdlib only.
"""
from __future__ import annotations

import hashlib
import os
import posixpath
import re
import subprocess
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any

from levain import unshowable
from levain.cockpit.consent import ConsentError, parse_stamp
from levain.contracts import ContractError
from levain.contracts.canon import canonical, canonical_bytes, parse
from levain.contracts.grammar import JSON_INT_MAX
from levain.dispatch import ContractRefused
from levain.dispatch.policy import ProviderPolicy, path_text_problem

__all__ = [
    "BUDGET_KEYS", "FORMAT", "GOAL_MAX_BYTES", "GRANTABLE", "GitUnavailable", "MAX_LIFETIME", "NEW", "PathEntry",
    "ResolvedContract", "ResolvedTemplate", "SUBMISSION_KEYS", "git_env", "parse_contract", "resolve_contract",
]

FORMAT = "levain-dispatch-contract/1"
SUBMISSION_KEYS = frozenset({"template", "goal", "read", "write", "base", "budget", "grant", "expires_at", "repo"})
BUDGET_KEYS = frozenset({"tool_calls", "tokens", "dollars_cents"})
GRANTABLE = frozenset({"Write", "Edit", "apply_patch"})
# the longest a contract may live from filing: a signature is never open-ended
MAX_LIFETIME = timedelta(hours=24)
GOAL_MAX_BYTES = 16 * 1024
PATHS_MAX = 512
PATH_MAX_BYTES = 1024
GIT_TIMEOUT = 60.0
# the mode a new write path carries in the resolved contract, and its blob; neither can be a git mode or a sha
NEW = "new"
_REGULAR = frozenset({"100644", "100755"})
_SYMLINK, _GITLINK, _TREE = "120000", "160000", "040000"
_SHA1 = re.compile(r"[0-9a-f]{40}")
_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@:/+-]{0,127}")
_TIERS = ("R0", "R1")
_VERIFIER_KINDS = ("black-box", "in-process")
_GLOB = frozenset("*?[")
# tool class names that are a shell under another name; refused with the Bash message, not the unknown-class one
_SHELLISH = re.compile(r"(?i).*(bash|shell|zsh|exec|terminal|command|powershell|cmd|^sh$|^run$).*")
# a path under /dev/null cannot exist (/dev/null is not a directory), so git reads no graft file through it
_NO_GRAFT_FILE = "/dev/null/levain-dispatch-no-grafts"


def _refuse(message: str) -> ContractRefused:
    return ContractRefused(message)


class GitUnavailable(ContractRefused):
    """git could not run at all (it timed out or could not start): a fault on this machine, not in the submission, so
    a caller that reads input faults from a refusal must never report it as one."""


# the name this class had while it was private, kept so a caller written against it still catches it
_GitUnavailable = GitUnavailable


def _text_problem(text: str, what: str) -> str | None:
    # prose (a goal): what is signed must show as what it is (levain.unshowable); newline and tab are prose
    found = unshowable.first_hidden(text, allow="\n\t")
    if found is not None:
        ch = found[1]
        return (f"the {what} holds U+{ord(ch):04X} ({unicodedata.name(ch, 'unnamed')}), which can show differently "
                "from what is signed")
    return None


# --- the template interface ----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ResolvedTemplate:
    """What a registered template resolves to (the registry and its signed store are F3's). Immutable and
    content-addressed: an edited template is a new ``content_hash``. ``models`` keeps its order, since run ``i`` uses
    model ``i``; it is distinct and at least ``k`` long so a contrast compares different models."""
    template_id: str
    content_hash: str
    tier: str
    executor: str
    models: tuple[str, ...]
    k: int
    verifier_id: str
    verifier_hash: str
    verifier_kind: str
    write_paths: frozenset[str]
    provider: str
    route: str
    tool_versions: tuple[tuple[str, str], ...]

    def problem(self) -> str | None:
        """Why this template cannot bind a contract (it is input, so it is checked), or ``None``."""
        if not isinstance(self.template_id, str) or _ID.fullmatch(self.template_id) is None:
            return f"template id {self.template_id!r} must match {_ID.pattern}"
        for name in ("content_hash", "verifier_hash"):
            value = getattr(self, name)
            if not isinstance(value, str) or _SHA256_DIGEST.fullmatch(value) is None:
                return f"the template's {name} must be sha256:<64 hex>"
        if self.tier not in _TIERS:
            return f"template tier {self.tier!r} is not one of {_TIERS} (R2 is never a template)"
        for name in ("executor", "verifier_id", "provider", "route"):
            value = getattr(self, name)
            if not isinstance(value, str) or _NAME.fullmatch(value) is None:
                return f"the template's {name} must match {_NAME.pattern}"
        if type(self.k) is not int or self.k < 1:
            return "the template's k must be an integer >= 1"
        if self.tier == "R0" and self.k < 2:
            return "an R0 template needs k >= 2 (contrast across distinct models)"
        if not isinstance(self.models, tuple) or not all(isinstance(m, str) and _NAME.fullmatch(m) for m in
                                                         self.models):
            return "the template's models must be a tuple of model names"
        if len(set(self.models)) != len(self.models):
            return "the template's models must be distinct"
        if len(self.models) < self.k:
            return f"the template names {len(self.models)} models, fewer than k={self.k}"
        if self.verifier_kind not in _VERIFIER_KINDS:
            return f"verifier kind {self.verifier_kind!r} is not one of {_VERIFIER_KINDS}"
        if not isinstance(self.write_paths, frozenset) or not self.write_paths:
            return "the template's write_paths must be a non-empty frozenset of exact paths"
        for p in self.write_paths:
            problem = _path_problem(p)
            if problem is not None:
                return f"the template's write_paths: {problem}"
        tv = self.tool_versions
        if not isinstance(tv, tuple) or not all(
                isinstance(pair, tuple) and len(pair) == 2 and all(isinstance(x, str) and _NAME.fullmatch(x)
                                                                    for x in pair) for pair in tv):
            return "the template's tool_versions must be a tuple of (name, version) string pairs"
        if list(tv) != sorted(tv) or len({n for n, _ in tv}) != len(tv):
            return "the template's tool_versions must be sorted with each tool once"
        return None

    def to_mapping(self) -> dict[str, Any]:
        return {"id": self.template_id, "content_hash": self.content_hash, "tier": self.tier,
                "executor": self.executor, "models": list(self.models), "k": self.k,
                "verifier_id": self.verifier_id, "verifier_hash": self.verifier_hash,
                "verifier_kind": self.verifier_kind, "write_paths": sorted(self.write_paths),
                "provider": self.provider, "route": self.route,
                "tool_versions": [list(pair) for pair in self.tool_versions]}


# --- paths ---------------------------------------------------------------------------------------------------------

def _path_problem(p: Any) -> str | None:
    """Why ``p`` is not an exact, normalised, repo-relative POSIX file path; ``None`` when it is."""
    if not isinstance(p, str) or not p:
        return "a path is a non-empty string"
    if "\x00" in p:
        return "a path holds NUL"
    if len(p.encode("utf-8", "surrogatepass")) > PATH_MAX_BYTES:
        return f"a path is at most {PATH_MAX_BYTES} bytes"
    if p.startswith("/"):
        return f"path {p[:80]!r} is absolute; paths are repo-relative"
    if any(c in _GLOB for c in p):
        return f"path {p[:80]!r} holds a glob character; paths are exact files"
    if posixpath.normpath(p) != p or p == "." or ".." in p.split("/"):
        return f"path {p[:80]!r} is not normalised (no '..', '.', doubled or trailing slash)"
    if any(part.casefold() == ".git" for part in p.split("/")):
        return f"path {p[:80]!r} reaches into .git, which is never a contract file"
    if unicodedata.normalize("NFC", p) != p:
        return f"path {p[:80]!r} is not in Unicode NFC (a case- or form-insensitive disk would merge it)"
    problem = path_text_problem(p)
    return None if problem is None else f"{problem} ({p[:80]!r})"


def _paths(raw: Any, what: str) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        raise _refuse(f"{what} must be a list of paths")
    if len(raw) > PATHS_MAX:
        raise _refuse(f"{what} lists more than {PATHS_MAX} paths")
    for p in raw:
        problem = _path_problem(p)
        if problem is not None:
            raise _refuse(f"{what}: {problem}")
    if len(set(raw)) != len(raw):
        raise _refuse(f"{what} lists a path twice")
    if len({p.casefold() for p in raw}) != len(raw):
        raise _refuse(f"{what} lists two paths that differ only in case")
    return sorted(raw)


def _nested_problem(paths: Iterable[str]) -> str | None:
    """Why a write set cannot be materialised because one path is an ancestor of another (``tmp`` and
    ``tmp/x.py``: one file cannot also be a directory), compared without case; ``None`` when no path nests."""
    folded = {p.casefold(): p for p in paths}
    for p in sorted(folded.values()):
        parts = p.split("/")
        for i in range(1, len(parts)):
            ancestor = folded.get("/".join(parts[:i]).casefold())
            if ancestor is not None:
                return f"write lists {ancestor} and {p}, which is under it; a file cannot also be a directory"
    return None


@dataclass(frozen=True)
class PathEntry:
    """A resolved path: its git mode and blob sha at base, or :data:`NEW` for both when it is a file to create."""
    path: str
    mode: str
    blob: str

    @property
    def new(self) -> bool:
        return self.mode == NEW

    def to_mapping(self) -> dict[str, str]:
        return {"path": self.path, "mode": self.mode, "blob": self.blob}


# --- git -----------------------------------------------------------------------------------------------------------

def git_env() -> dict[str, str]:
    """The environment every dispatch git call runs with (resolution here; materialising and verifying a worktree
    later): no inherited ``GIT_*`` variable may redirect the repository, its object store or its index; replace
    objects are off (a ``refs/replace/`` entry would swap the tree a commit sha names) and the graft file is a path
    that cannot exist (a graft rewrites a commit's parents)."""
    from levain.launch import child_env

    env = {k: v for k, v in child_env().items() if not k.startswith("GIT_")}
    env.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_NO_REPLACE_OBJECTS": "1", "GIT_GRAFT_FILE": _NO_GRAFT_FILE})
    return env


def _git(repo: str, *args: str) -> bytes:
    try:
        cp = subprocess.run(["git", "--no-replace-objects", "-C", repo, *args], capture_output=True,
                            timeout=GIT_TIMEOUT, env=git_env(), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _GitUnavailable(f"git could not run in {repo} ({type(exc).__name__}); nothing in the contract was "
                              "judged, so the same submission may be filed again") from None
    if cp.returncode != 0:
        detail = (cp.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise _refuse(f"git {args[0]} failed in {repo}: {detail[-1][:200] if detail else cp.returncode}")
    return cp.stdout


def _tree_at(repo: str, tree: str) -> dict[str, tuple[str, str, str]]:
    """Every entry of the tree ``tree`` (base's, by sha), trees included: path -> (mode, type, sha). One listing, no
    pathspec."""
    out: dict[str, tuple[str, str, str]] = {}
    for record in _git(repo, "ls-tree", "-r", "-t", "-z", "--full-tree", tree).split(b"\x00"):
        if not record:
            continue
        meta, _, raw_path = record.partition(b"\t")
        mode, kind, sha = meta.decode("ascii").split(" ")
        out[raw_path.decode("utf-8", "surrogateescape")] = (mode, kind, sha)
    return out


def _resolve_repo(repo: Any) -> str:
    if not isinstance(repo, str) or not repo or "\x00" in repo:
        raise _refuse("repo must be a non-empty absolute path")
    if not repo.startswith("/") or os.path.normpath(repo) != repo:
        raise _refuse(f"repo {repo[:120]!r} must be an absolute path in its normal form")
    if os.path.realpath(repo) != repo:
        raise _refuse(f"repo {repo[:120]!r} goes through a symlink; name the real path")
    if not os.path.isdir(repo):
        raise _refuse(f"repo {repo[:120]!r} is not a directory")
    top = _git(repo, "rev-parse", "--show-toplevel").decode("utf-8", "surrogateescape").strip()
    if os.path.realpath(top) != repo:
        raise _refuse(f"repo {repo[:120]!r} is not the top of a git work tree")
    return repo


def _resolve_base(repo: str, base: Any) -> tuple[str, str]:
    """``(base, its tree's sha)``, both read under :func:`git_env`."""
    if not isinstance(base, str) or _SHA1.fullmatch(base) is None:
        raise _refuse("base must be a full 40-hex commit sha (lowercase)")
    try:
        got = _git(repo, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}").decode("ascii").strip()
    except _GitUnavailable:
        raise
    except ContractRefused:
        # git's own stderr is not a planner's message: the sha names no commit here (absent, or a blob or tree)
        raise _refuse(f"base {base} is not a commit in {repo}") from None
    if got != base:
        raise _refuse(f"base {base} does not resolve to that commit (got {got[:40]})")
    tree = _git(repo, "rev-parse", "--verify", "--end-of-options", f"{base}^{{tree}}").decode("ascii").strip()
    if _SHA1.fullmatch(tree) is None:
        raise _refuse(f"base {base}'s tree did not resolve to a sha (got {tree[:40]!r})")
    return base, tree


def _resolve_paths(tree: Mapping[str, tuple[str, str, str]], read: list[str],
                   write: list[str]) -> tuple[tuple[PathEntry, ...], tuple[PathEntry, ...]]:
    def existing(p: str, what: str) -> PathEntry | None:
        entry = tree.get(p)
        if entry is None:
            return None
        mode, kind, sha = entry
        if mode == _SYMLINK:
            raise _refuse(f"{what} path {p} is a symlink at base; symlinks are never contract files")
        if mode == _GITLINK:
            raise _refuse(f"{what} path {p} is a submodule (gitlink) at base")
        if mode == _TREE:
            raise _refuse(f"{what} path {p} is a directory at base; name exact files")
        if mode not in _REGULAR:
            raise _refuse(f"{what} path {p} has mode {mode} at base, not a regular file")
        # git's own word on the object, not the mode alone: a regular mode over anything but a blob is a damaged tree
        if kind != "blob" or _SHA1.fullmatch(sha) is None:
            raise _refuse(f"{what} path {p} has a regular mode at base but is a {kind[:16]!r} object "
                          f"{sha[:40]!r}, not a blob")
        return PathEntry(p, mode, sha)

    # every spelling at base of each case-folded path (git is case-sensitive; the target disk is not)
    folded: dict[str, set[str]] | None = None

    def twin(p: str) -> str | None:
        nonlocal folded
        if folded is None:
            folded = {}
            for q in tree:
                folded.setdefault(q.casefold(), set()).add(q)
        others = sorted(folded.get(p.casefold(), set()) - {p})
        return others[0] if others else None

    reads = []
    for p in read:
        entry = existing(p, "read")
        if entry is None:
            raise _refuse(f"read path {p} does not exist at base")
        reads.append(entry)
    writes = []
    for p in write:
        entry = existing(p, "write")
        if entry is None:
            parts = p.split("/")
            for i in range(1, len(parts)):
                ancestor = "/".join(parts[:i])
                # a differently-cased twin of an ancestor is the same directory (or file, or symlink) on a
                # case-insensitive disk, so the new file would land somewhere the contract never named
                other = twin(ancestor)
                if other is not None:
                    raise _refuse(f"new write path {p}: its parent {ancestor} differs only in case from {other} "
                                  "at base")
                got = tree.get(ancestor)
                if got is None:
                    continue
                if got[0] == _SYMLINK:
                    raise _refuse(f"new write path {p}: its parent {ancestor} is a symlink at base")
                if got[0] == _GITLINK:
                    raise _refuse(f"new write path {p}: its parent {ancestor} is a submodule (gitlink) at base")
                if got[0] != _TREE:
                    raise _refuse(f"new write path {p}: its parent {ancestor} is a file at base")
                if got[1] != "tree":
                    raise _refuse(f"new write path {p}: its parent {ancestor} has a directory mode at base but is "
                                  f"a {got[1][:16]!r} object")
            other = twin(p)
            if other is not None:
                raise _refuse(f"new write path {p} differs only in case from {other} at base")
            entry = PathEntry(p, NEW, NEW)
        writes.append(entry)
    return tuple(reads), tuple(writes)


# --- the resolved contract -----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ResolvedContract:
    """A contract as resolved at filing: every field the digest binds. :meth:`canonical` is what is stored and what
    the human signs by :attr:`digest`."""
    goal: str
    repo: str
    base: str
    base_tree: str
    read: tuple[PathEntry, ...]
    write: tuple[PathEntry, ...]
    budget: Mapping[str, int]
    grant: tuple[str, ...]
    expires_at: str
    template: ResolvedTemplate
    policy_hash: str
    format: str = FORMAT

    def __post_init__(self) -> None:
        # the budget is a read-only view over the contract's own copy: no holder can change what the digest binds
        object.__setattr__(self, "budget", MappingProxyType(dict(self.budget)))

    def to_mapping(self) -> dict[str, Any]:
        return {"format": self.format, "goal": self.goal, "repo": self.repo, "base": self.base,
                "base_tree": self.base_tree, "read": [e.to_mapping() for e in self.read], "write": [e.to_mapping() for e in self.write],
                "budget": dict(self.budget), "grant": list(self.grant), "expires_at": self.expires_at,
                "template": self.template.to_mapping(), "policy_hash": self.policy_hash}

    def canonical(self) -> str:
        return canonical(self.to_mapping())

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()

    @property
    def write_paths(self) -> frozenset[str]:
        return frozenset(e.path for e in self.write)


_R2 = "so the contract is R2, which is never dispatched"


def _goal(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise _refuse("goal must be non-empty text")
    if len(raw.encode("utf-8", "surrogatepass")) > GOAL_MAX_BYTES:
        raise _refuse(f"goal is over {GOAL_MAX_BYTES} bytes")
    problem = _text_problem(raw, "goal")
    if problem is not None:
        raise _refuse(problem)
    return raw


def _budget(raw: Any) -> dict[str, int]:
    if not isinstance(raw, Mapping) or set(raw) != BUDGET_KEYS:
        raise _refuse(f"budget is exactly {sorted(BUDGET_KEYS)}")
    for key in sorted(BUDGET_KEYS):
        v = raw[key]
        if type(v) is not int or not 1 <= v <= JSON_INT_MAX:
            raise _refuse(f"budget {key} must be a positive integer (no floats, at most {JSON_INT_MAX})")
    return {k: raw[k] for k in sorted(BUDGET_KEYS)}


def _grant(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        raise _refuse("grant must be a list of tool classes")
    for g in raw:
        if not isinstance(g, str):
            raise _refuse("a grant entry is a tool class name")
        if g in GRANTABLE:
            continue
        if _SHELLISH.fullmatch(g):
            raise _refuse(f"grant {g[:40]!r} is a shell: Bash is never grantable (a shell command is never parsed, "
                          "so no grant can scope one)")
        raise _refuse(f"grant {g[:40]!r} is not a grantable tool class (only {sorted(GRANTABLE)}; Bash is never "
                      "grantable)")
    if len(set(raw)) != len(raw):
        raise _refuse("grant lists a tool class twice")
    return tuple(sorted(raw))


def _expiry(raw: Any, now: datetime) -> str:
    try:
        when = parse_stamp(raw)
    except ConsentError:
        raise _refuse("expires_at must be a UTC stamp (YYYY-MM-DDTHH:MM:SS.ffffffZ)") from None
    if when <= now:
        raise _refuse("expires_at is not in the future")
    if when > now + MAX_LIFETIME:
        raise _refuse(f"expires_at is more than {MAX_LIFETIME} away; a contract is never open-ended")
    return raw


def resolve_contract(submission: Any, *, resolve_template: Callable[[str], ResolvedTemplate | None],
                     policy: ProviderPolicy, now: datetime) -> ResolvedContract:
    """Resolve a planner's ``submission`` (a mapping of exactly :data:`SUBMISSION_KEYS`) at ``now`` (aware), or raise
    :class:`ContractRefused` saying why. The template comes from ``resolve_template`` (None: unknown), the data
    boundary from ``policy`` for the template's provider."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ContractRefused("the clock returned a naive datetime")
    if not isinstance(submission, Mapping) or set(submission) != SUBMISSION_KEYS:
        got = sorted(submission) if isinstance(submission, Mapping) else type(submission).__name__
        raise _refuse(f"a contract is exactly {sorted(SUBMISSION_KEYS)} (got {got})")
    if not isinstance(policy, ProviderPolicy):
        raise _refuse("the provider policy is not a loaded ProviderPolicy")
    goal = _goal(submission["goal"])
    budget = _budget(submission["budget"])
    grant = _grant(submission["grant"])
    expires_at = _expiry(submission["expires_at"], now)
    read = _paths(submission["read"], "read")
    write = _paths(submission["write"], "write")
    if not write:
        raise _refuse("write names no file; a dispatch contract changes at least one file")
    problem = _nested_problem(write)
    if problem is not None:
        raise _refuse(problem)
    template_id = submission["template"]
    if not isinstance(template_id, str) or _ID.fullmatch(template_id) is None:
        raise _refuse(f"template {str(template_id)[:64]!r} is not a template id, {_R2}")
    try:
        template = resolve_template(template_id)
    except Exception as exc:  # noqa: BLE001 - the resolver is input to resolution: its failure is a refusal
        raise _refuse(f"the template resolver failed on template {template_id} ({type(exc).__name__}); nothing "
                      "is filed") from None
    if template is None:
        raise _refuse(f"template {template_id} is not registered, {_R2}")
    if not isinstance(template, ResolvedTemplate):
        raise _refuse("the template resolver returned something other than a ResolvedTemplate")
    problem = template.problem()
    if problem is not None:
        raise _refuse(f"template {template_id} cannot bind a contract: {problem}")
    if template.template_id != template_id:
        raise _refuse(f"the resolver returned template {template.template_id} for {template_id}")
    outside = sorted(set(write) - template.write_paths)
    if outside:
        raise _refuse(f"write path {outside[0]} is not in template {template_id}'s write set, {_R2}")
    # git resolution before the data boundary: a path's own defect (a directory, a symlink, missing at base) is the
    # message the planner can act on. Both refuse the contract whole; the order changes only which reason is named.
    repo = _resolve_repo(submission["repo"])
    base, base_tree = _resolve_base(repo, submission["base"])
    reads, writes = _resolve_paths(_tree_at(repo, base_tree), read, write)
    problem = policy.refusal(template.provider, read, write)
    if problem is not None:
        raise _refuse(f"data boundary: {problem}")
    return ResolvedContract(goal=goal, repo=repo, base=base, base_tree=base_tree, read=reads, write=writes, budget=budget, grant=grant,
                            expires_at=expires_at, template=template, policy_hash=policy.content_hash())


# --- parse ---------------------------------------------------------------------------------------------------------

_CONTRACT_KEYS = frozenset({"format", "goal", "repo", "base", "base_tree", "read", "write", "budget", "grant", "expires_at",
                            "template", "policy_hash"})
_TEMPLATE_KEYS = frozenset({"id", "content_hash", "tier", "executor", "models", "k", "verifier_id", "verifier_hash",
                            "verifier_kind", "write_paths", "provider", "route", "tool_versions"})


def _entries(raw: Any, what: str, *, allow_new: bool) -> tuple[PathEntry, ...]:
    if not isinstance(raw, list):
        raise _refuse(f"{what} is a list of path entries")
    out = []
    for e in raw:
        if not isinstance(e, dict) or set(e) != {"path", "mode", "blob"}:
            raise _refuse(f"a {what} entry is exactly path, mode, blob")
        problem = _path_problem(e["path"])
        if problem is not None:
            raise _refuse(f"{what}: {problem}")
        if not isinstance(e["mode"], str):
            raise _refuse(f"{what}: a mode is text")
        if e["mode"] == NEW and allow_new:
            if e["blob"] != NEW:
                raise _refuse(f"{what}: a new file's blob is {NEW!r}")
        elif e["mode"] not in _REGULAR or not isinstance(e["blob"], str) or _SHA1.fullmatch(e["blob"]) is None:
            raise _refuse(f"{what}: entry {str(e['path'])[:80]!r} is not a regular file with a blob sha")
        out.append(PathEntry(e["path"], e["mode"], e["blob"]))
    return tuple(out)


def parse_contract(text: str | bytes) -> ResolvedContract:
    """The contract ``text`` holds, refused unless ``text`` is exactly its canonical form (byte for byte). Anything
    that goes wrong while parsing or checking it is :class:`ContractRefused`, never another exception."""
    try:
        return _parse_contract(text)
    except ContractRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - stored text is input: any failure on it is a refusal
        raise _refuse(f"the contract text is not a dispatch contract ({type(exc).__name__})") from None


def _parse_contract(text: str | bytes) -> ResolvedContract:
    if not isinstance(text, (str, bytes)):
        raise _refuse("the contract text is text or bytes")
    data = text.encode("utf-8") if isinstance(text, str) else text
    try:
        obj = parse(data)
    except ContractError as exc:
        raise _refuse(f"the contract text does not parse: {exc}") from None
    if not isinstance(obj, dict) or set(obj) != _CONTRACT_KEYS:
        raise _refuse("the contract text is not a dispatch contract")
    if obj["format"] != FORMAT:
        raise _refuse(f"contract format {obj['format']!r} is not {FORMAT!r}")
    t = obj["template"]
    if not isinstance(t, dict) or set(t) != _TEMPLATE_KEYS:
        raise _refuse("the contract's template is not a resolved template")
    for name in ("models", "write_paths", "tool_versions"):
        if not isinstance(t[name], list):
            raise _refuse(f"the template's {name} is a list")
    if not all(isinstance(p, list) and len(p) == 2 for p in t["tool_versions"]):
        raise _refuse("the template's tool_versions are [name, version] pairs")
    try:
        template = ResolvedTemplate(
            template_id=t["id"], content_hash=t["content_hash"], tier=t["tier"], executor=t["executor"],
            models=tuple(t["models"]), k=t["k"], verifier_id=t["verifier_id"], verifier_hash=t["verifier_hash"],
            verifier_kind=t["verifier_kind"], write_paths=frozenset(t["write_paths"]), provider=t["provider"],
            route=t["route"], tool_versions=tuple(tuple(p) for p in t["tool_versions"]))
    except TypeError:
        raise _refuse("the contract's template holds an unhashable value") from None
    problem = template.problem()
    if problem is not None:
        raise _refuse(f"the contract's template: {problem}")
    goal = _goal(obj["goal"])
    if not isinstance(obj["repo"], str) or not obj["repo"].startswith("/") or \
            os.path.normpath(obj["repo"]) != obj["repo"] or "\x00" in obj["repo"]:
        raise _refuse("repo is an absolute path in its normal form")
    if not isinstance(obj["base"], str) or _SHA1.fullmatch(obj["base"]) is None:
        raise _refuse("base is a 40-hex commit sha")
    if not isinstance(obj["base_tree"], str) or _SHA1.fullmatch(obj["base_tree"]) is None:
        raise _refuse("base_tree is a 40-hex tree sha")
    if not isinstance(obj["policy_hash"], str) or _SHA256_DIGEST.fullmatch(obj["policy_hash"]) is None:
        raise _refuse("policy_hash is sha256:<64 hex>")
    try:
        parse_stamp(obj["expires_at"])
    except ConsentError:
        raise _refuse("expires_at is a UTC stamp") from None
    reads = _entries(obj["read"], "read", allow_new=False)
    writes = _entries(obj["write"], "write", allow_new=True)
    if not writes:
        raise _refuse("write names no file")
    for what, entries in (("read", reads), ("write", writes)):
        paths = [e.path for e in entries]
        if paths != sorted(paths) or len({p.casefold() for p in paths}) != len(paths):
            raise _refuse(f"{what} entries must be sorted, each path once")
    problem = _nested_problem(e.path for e in writes)
    if problem is not None:
        raise _refuse(problem)
    if not {e.path for e in writes} <= template.write_paths:
        raise _refuse("a write path is outside the template's write set")
    contract = ResolvedContract(goal=goal, repo=obj["repo"], base=obj["base"], base_tree=obj["base_tree"], read=reads, write=writes,
                                budget=_budget(obj["budget"]), grant=_grant(obj["grant"]),
                                expires_at=obj["expires_at"], template=template, policy_hash=obj["policy_hash"])
    if canonical_bytes(contract.to_mapping()) != data:
        raise _refuse("the contract text is not in its canonical form")
    return contract
