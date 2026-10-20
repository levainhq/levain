"""levain.dispatch.adopt — adoption: a verified run lands on the operator's branch (F11).

Adoption is two effects (F11 §2). STAGE (E0): each verified run is a deterministic commit on its own ref
``refs/heads/levain/dispatch/<job>/r<i>`` (:func:`stage_commit`; reversible, so the contract's signature is all it
needs). LAND (E1, "adopted"): one ``update-ref`` compare-and-swap moves a branch outside ``levain/`` from the
contract's ``base`` to a staged commit. A land is authorised by (A) the operator's signed authorise item over the
run's ARTIFACT digest and the branch (:func:`authorise_payload`, its own namespace), or (B) a standing adoption rule
whose typed criteria hold (:func:`levain.dispatch.registry.active_adoption_rules`); (C), the envelope, comes with the
coordinator build. Every authority passes ONE floor (:func:`floor`, F11 §4), whose first failure decides.

:func:`land` is F11 §6: on a verified or recommended job the floor runs in full and only then is ``authorised``
written, its evidence the INTENT (authority, run, branch, staged commit, base, tree, artifact digest). That write is
the linearisation point: an ``authorised`` job never re-enters the floor and reads nothing mutable again. From the
intent: a branch that already contains the staged commit records ``adopted`` and moves nothing (a crash after the
swap, or the operator's own merge with his commits on top); a branch still at ``base`` is checked out somewhere ->
Levain prints the operator's own ``git -C <worktree> merge --ff-only <sha>`` and stops (it never touches a
worktree); else the swap, then ``adopted``. A branch that reads neither is MOVED BASE: ``failed`` (``base_moved``),
which releases the write files so the planner can re-file, and the job's staged refs are deleted.

Every git call goes through :func:`_git`: the never-PATH ``git_bin()``, ``core.hooksPath=/dev/null``,
``core.fsmonitor=false``, an explicit ``--git-dir``, the consent push path's ``git_env("")`` plus contract.py's
graft-file guard, ``cwd=/``, no stdin
unless one is fed, bytes out, a timeout. So no hook, fsmonitor, filter, textconv or external diff of the repository
ever runs here. The change set (:func:`change_set`) comes from the plumbing ``diff-tree --raw`` and ``cat-file``
alone, under fixed caps (:data:`MAX_ENTRIES`, :data:`MAX_BLOB_BYTES`, :data:`MAX_TOTAL_BYTES`) checked on sizes
BEFORE any content is read; line counts and the binary test are Levain's own.

The LANDING TIER (F11 §4 step 4) is :func:`levain.consent.landing.landing_tier`, which reads consent's own
classifiers. It is INJECTED (``landing_tier(repo, paths)``) and there is no default, so a caller names it. Anything
it returns but ``"T2"``/``"T3"``, and any exception from it, refuses the land.

Known edge (F11 §8): until S1 places principals, every actor is the operator's uid, so a planner can merge a staged
ref itself and skip all of this; F11 is the path Levain records, not a boundary, until then. Stdlib only.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shlex
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from levain.authority import PASSKEY, AuthoritySnapshot, verify_link_sig
from levain.cockpit.consent import ConsentError, parse_stamp, utc_stamp
from levain.consent import push
from levain.consent.queue import class_covers
from levain.contracts.canon import canonical_bytes
from levain.dispatch import DispatchError
from levain.dispatch import registry as dregistry
from levain.dispatch.contract import _NO_GRAFT_FILE, ResolvedTemplate
from levain.dispatch.jobs import Job, JobStore, SignatureRefused, TransitionRefused
from levain.dispatch.policy import ProviderPolicy
from levain.dispatch.sign import FIELD_MAX, SIGNATURE_MAX

__all__ = [
    "AUTHORISE_FORMAT", "AUTHORISE_KEYS", "DISPATCH_AUTHORISE_NAMESPACE", "MAX_BLOB_BYTES", "MAX_ENTRIES",
    "MAX_TOTAL_BYTES", "ArtifactRecord", "Change", "ChangeSet", "Floor", "GitFault", "Landing", "artifact_digest",
    "artifact_problem",
    "authorise_message", "authorise_payload", "authorise_refusal", "branch_problem", "change_set",
    "criteria_failure", "floor", "land", "revalidation_problem", "stage_commit", "staged_ref",
]

DISPATCH_AUTHORISE_NAMESPACE = "levain-dispatch-authorise"
AUTHORISE_FORMAT = "levain-dispatch-authorise/1"
AUTHORISE_KEYS = frozenset({"format", "store_id", "job_id", "run", "contract_digest", "artifact_digest", "onto",
                            "signer_fp", "signed_at"})
# the change-set caps (F11 §4 step 3), checked on sizes before any content is read: entries in the raw diff, bytes
# of any one blob on either side, and bytes of every blob on both sides together
MAX_ENTRIES = 1000
MAX_BLOB_BYTES = 1 << 20
MAX_TOTAL_BYTES = 8 << 20
# git's own binary test: a NUL in the first 8000 bytes
_BINARY_PROBE = 8000
# the line-pair work one change set's exact line counts may spend (old middle lines x new middle lines, summed);
# past it a file's counts are the upper bound _line_counts names
_LINE_DIFF_BUDGET = 4_000_000
GIT_TIMEOUT = 60
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_JOB_ID = re.compile(r"dj-[0-9a-f]{32}")
_STAGE_NAME = "levain dispatch"
_STAGE_EMAIL = "dispatch@levain.invalid"
_REGULAR = ("100644", "100755")
_NEW_MODE = "100644"
_GITLINK = "160000"
_ZERO = re.compile(r"0+")
TIERS = ("T2", "T3")


class GitFault(DispatchError):
    """git could not run, or answered in a way this module cannot read: a machine fault, never a refusal."""


# --- the artifact record (F11 §7) ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class ArtifactRecord:
    """One verified RUN, as the control service records it from the supervisor pipe (never an executor). The
    verifier driver writes real ones; until then tests construct them. ``verifier`` is ``{id, hash, kind, result}``
    (``kind`` the supervisor's DECLARED kind), ``model`` is ``{requested, answered, backend_identity}``."""
    job_id: str
    run: int
    contract_digest: str
    base: str
    staged_commit: str
    tree: str
    verifier: Mapping[str, str]
    model: Mapping[str, str]
    verified_at: str

    def problem(self) -> str | None:
        if not isinstance(self.job_id, str) or _JOB_ID.fullmatch(self.job_id) is None:
            return "the record's job_id is not a dispatch job id"
        if type(self.run) is not int or not 0 <= self.run < 64:
            return "the record's run is an integer from 0 to 63"
        if not isinstance(self.contract_digest, str) or _SHA256_DIGEST.fullmatch(self.contract_digest) is None:
            return "the record's contract_digest is sha256:<64 hex>"
        for name in ("base", "staged_commit", "tree"):
            if not isinstance(getattr(self, name), str) or _SHA.fullmatch(getattr(self, name)) is None:
                return f"the record's {name} is a full object id"
        if not isinstance(self.verifier, Mapping) or set(self.verifier) != {"id", "hash", "kind", "result"}:
            return "the record's verifier is exactly id, hash, kind, result"
        if not isinstance(self.model, Mapping) or set(self.model) != {"requested", "answered", "backend_identity"}:
            return "the record's model is exactly requested, answered, backend_identity"
        if not all(isinstance(v, str) and v and len(v) <= FIELD_MAX
                   for v in (*self.verifier.values(), *self.model.values())):
            return f"every verifier and model field is 1 to {FIELD_MAX} characters of text"
        try:
            parse_stamp(self.verified_at)
        except (ConsentError, TypeError):
            return "the record's verified_at is not a timestamp"
        return None

    def to_mapping(self) -> dict[str, Any]:
        return {"job_id": self.job_id, "run": self.run, "contract_digest": self.contract_digest, "base": self.base,
                "staged_commit": self.staged_commit, "tree": self.tree, "verifier": dict(self.verifier),
                "model": dict(self.model), "verified_at": self.verified_at}


def artifact_digest(record: ArtifactRecord) -> str:
    """``sha256:`` + the SHA-256 of the record's canonical bytes (:mod:`levain.contracts.canon`)."""
    return "sha256:" + hashlib.sha256(canonical_bytes(record.to_mapping())).hexdigest()


def staged_ref(job_id: str, run: int) -> str:
    return f"refs/heads/levain/dispatch/{job_id}/r{run}"


# --- git -----------------------------------------------------------------------------------------------------------

def _git_dir(repo: str) -> str:
    """The repository's git directory, read from the file system, never by running git in the work tree: ``.git``
    itself when it is a real directory, else the ``gitdir:`` a ``.git`` file names (a linked work tree)."""
    dot = os.path.join(repo, ".git")
    if os.path.islink(dot):
        raise GitFault(f"{dot} is a symlink")
    if os.path.isdir(dot):
        return dot
    if os.path.isfile(dot):
        try:
            with open(dot, "rb") as f:
                line = f.read(4096).decode("utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise GitFault(f"{dot} cannot be read ({type(exc).__name__})") from None
        if line.startswith("gitdir: "):
            target = os.path.realpath(os.path.join(repo, line[len("gitdir: "):]))
            if os.path.isdir(target):
                return target
    raise GitFault(f"{repo} has no git directory")


def _git(repo: str, *args: str, feed: bytes | None = None,
         extra_env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
    """git, hardened (module docstring); returns the completed process, its output as bytes. Raises
    :class:`GitFault` when git cannot run."""
    try:
        # core.commitGraph=false: a commit-graph file in the object store can report a parent the commit object does
        # not hold (L2 1020+17, run), so parents and ancestry are read from the objects themselves
        argv = [push.git_bin(), "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                "-c", "core.commitGraph=false",
                f"--git-dir={_git_dir(repo)}", *args]
    except push.PushRefused as exc:
        raise GitFault(str(exc)) from None
    env = push.git_env("")
    # contract.py's guard, which the push env does not carry: a graft file rewrites the parents a commit reports
    env["GIT_GRAFT_FILE"] = _NO_GRAFT_FILE
    if extra_env:
        env.update(extra_env)
    kw: dict[str, Any] = {"input": feed} if feed is not None else {"stdin": subprocess.DEVNULL}
    try:
        return subprocess.run(argv, capture_output=True, env=env, cwd="/", timeout=GIT_TIMEOUT, **kw)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitFault(f"git {args[0] if args else ''} could not run in {repo} ({type(exc).__name__})") from None


def _git_ok(repo: str, *args: str, **kw: Any) -> bytes:
    cp = _git(repo, *args, **kw)
    if cp.returncode != 0:
        detail = cp.stderr.decode("utf-8", "replace").strip().splitlines()
        raise GitFault(f"git {args[0]} failed in {repo}: {detail[-1][:200] if detail else cp.returncode}")
    return cp.stdout


def _object_type(repo: str, sha: str) -> str | None:
    """The object's type, or ``None`` when the object is absent; a git that fails otherwise raises, so an unreadable
    fact is never read as an absent one (L3 r1 codex)."""
    present = _git(repo, "cat-file", "-e", sha)
    if present.returncode == 1:
        return None
    if present.returncode != 0:
        raise GitFault(f"git cat-file -e failed in {repo} (exit {present.returncode})")
    return _git_ok(repo, "cat-file", "-t", sha).decode("ascii", "replace").strip()


def _commit_shape(repo: str, sha: str) -> tuple[list[str], str] | None:
    """``(parents, tree)`` of the commit ``sha``, or ``None`` when it is not a commit here."""
    if _object_type(repo, sha) != "commit":
        return None
    out = _git_ok(repo, "rev-list", "--parents", "--no-walk", "--end-of-options", sha).decode("ascii").split()
    tree = _git_ok(repo, "rev-parse", "--verify", "--end-of-options", f"{sha}^{{tree}}").decode("ascii").strip()
    return out[1:], tree


def _read_branch(repo: str, onto: str) -> str | None:
    """The branch's commit, or ``None`` when it does not exist (rev-parse --quiet exit 1); any other failure raises."""
    cp = _git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options", f"refs/heads/{onto}^{{commit}}")
    if cp.returncode == 1:
        return None
    if cp.returncode != 0:
        raise GitFault(f"git rev-parse failed in {repo} (exit {cp.returncode})")
    return cp.stdout.decode("ascii").strip()


def _is_ancestor(repo: str, sha: str, tip: str) -> bool:
    cp = _git(repo, "merge-base", "--is-ancestor", sha, tip)
    if cp.returncode not in (0, 1):
        raise GitFault(f"git merge-base failed in {repo}")
    return cp.returncode == 0


def _symbolic(repo: str, onto: str) -> bool:
    return _git(repo, "symbolic-ref", "-q", f"refs/heads/{onto}").returncode == 0


def branch_problem(repo: str, onto: Any) -> str | None:
    """Why ``onto`` is not a branch a land may target (F11 §4 step 7, its shape): a BARE name (Levain builds
    ``refs/heads/<onto>`` itself), whose ``check-ref-format --branch`` output is the input unchanged (``@{-1}`` and
    the like expand, so they are refused), outside ``levain/`` (casefolded), and not a symbolic ref."""
    if not isinstance(onto, str) or not onto or len(onto) > FIELD_MAX:
        return "the branch is a name of 1 to 256 characters"
    if onto.startswith("refs/") or onto.startswith("-"):
        return f"the branch {onto[:80]!r} is a bare name, never a full ref"
    cp = _git(repo, "check-ref-format", "--branch", onto)
    if cp.returncode != 0 or cp.stdout.decode("utf-8", "replace").strip() != onto:
        return f"the branch {onto[:80]!r} is not a branch name as written"
    if onto.casefold().startswith("levain/"):
        return f"the branch {onto[:80]!r} is under levain/, which is never a land target"
    # the name as git lists it, byte for byte: on a case-insensitive disk git reads and writes a loose
    # refs/heads/Main as main's file, so a case variant would move main while the worktree check missed it
    listed = _git_ok(repo, "for-each-ref", "--format=%(refname)", "refs/heads/").decode("utf-8", "surrogateescape")
    names = [r[len("refs/heads/"):] for r in listed.splitlines() if r.startswith("refs/heads/")]
    if any(n != onto and n.casefold() == onto.casefold() for n in names):
        return f"the branch {onto[:80]!r} differs only in case from another branch here; name it exactly"
    if onto not in names:
        return f"refs/heads/{onto[:80]} is not a branch here, as written"
    if _symbolic(repo, onto):
        return f"refs/heads/{onto[:80]} is a symbolic ref"
    return None


# --- the change set (F11 §4 step 3) --------------------------------------------------------------------------------

@dataclass(frozen=True)
class Change:
    """One raw diff entry, both sides. ``added``/``deleted`` are None for a binary or non-blob entry."""
    path: str
    status: str
    old_mode: str
    new_mode: str
    old_blob: str
    new_blob: str
    old_size: int
    new_size: int
    added: int | None
    deleted: int | None
    binary: bool
    non_blob: bool


@dataclass(frozen=True)
class ChangeSet:
    entries: tuple[Change, ...]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(c.path for c in self.entries)

    @property
    def changed_lines(self) -> int | None:
        if any(c.added is None for c in self.entries):
            return None
        return sum((c.added or 0) + (c.deleted or 0) for c in self.entries)

    @property
    def staged_bytes(self) -> int:
        return sum(c.new_size for c in self.entries if c.status != "D")


def _absent(mode: str, sha: str) -> bool:
    return mode == "000000" and _ZERO.fullmatch(sha) is not None


def _lines(data: bytes) -> list[bytes]:
    return data.splitlines(keepends=True)


def _line_counts(old: bytes, new: bytes, budget: list[int]) -> tuple[int, int]:
    """``(added, deleted)``: exact (a line diff of what is left once the common first and last lines are set aside)
    while ``budget`` (one list per change set, decremented) holds the pair's size product, else the UPPER bound
    ``(len(new middle), len(old middle))``. A line diff is quadratic on repeated lines (L1 1020+17: 8,000 blank lines
    took 9 s), and an upper bound only ever refuses more under ``max_changed_lines``."""
    a, b = _lines(old), _lines(new)
    head = 0
    while head < len(a) and head < len(b) and a[head] == b[head]:
        head += 1
    tail = 0
    while tail < len(a) - head and tail < len(b) - head and a[-1 - tail] == b[-1 - tail]:
        tail += 1
    a, b = a[head:len(a) - tail], b[head:len(b) - tail]
    if len(a) * len(b) > budget[0]:
        return len(b), len(a)
    budget[0] -= len(a) * len(b)
    added = deleted = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            deleted += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, deleted


def change_set(repo: str, base: str, staged: str) -> ChangeSet | str:
    """The change set from ``base`` to ``staged``, or the line saying why it cannot be taken (a cap; nothing is
    read whole before every cap holds). Raises :class:`GitFault` when git cannot run."""
    raw = _git_ok(repo, "diff-tree", "-r", "-z", "--raw", "--no-abbrev", "--no-renames", "--no-ext-diff",
                  "--no-textconv", "--end-of-options", base, staged)
    fields = raw.split(b"\x00")
    if fields and fields[-1] == b"":
        fields.pop()
    if len(fields) % 2:
        raise GitFault("git diff-tree printed an odd number of fields")
    rows: list[tuple[str, str, str, str, str, str]] = []
    for i in range(0, len(fields), 2):
        if len(rows) >= MAX_ENTRIES:
            return f"the change set has more than {MAX_ENTRIES} entries"
        head = fields[i].decode("ascii", "replace")
        parts = head.lstrip(":").split(" ")
        if not head.startswith(":") or len(parts) != 5:
            raise GitFault(f"git diff-tree printed {head[:80]!r}")
        old_mode, new_mode, old_sha, new_sha, status = parts
        path = fields[i + 1].decode("utf-8", "surrogateescape")
        rows.append((path, status[:1], old_mode, new_mode, old_sha, new_sha))
    # sizes first, both sides of every blob entry; a gitlink (or any non-blob) side is never looked up
    blobs: list[str] = []
    for _p, _s, om, nm, osha, nsha in rows:
        for mode, sha in ((om, osha), (nm, nsha)):
            if not _absent(mode, sha) and mode != _GITLINK and sha not in blobs:
                blobs.append(sha)
    sizes: dict[str, int] = {}
    if blobs:
        out = _git_ok(repo, "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)",
                      feed=("\n".join(blobs) + "\n").encode("ascii")).decode("ascii", "replace").splitlines()
        for line in out:
            parts = line.split(" ")
            if len(parts) != 3 or parts[1] != "blob" or not parts[2].isdigit():
                raise GitFault(f"git cat-file reported {line[:80]!r}")
            sizes[parts[0]] = int(parts[2])
    total = 0
    for sha in blobs:
        if sha not in sizes:
            raise GitFault(f"git cat-file did not report {sha}")
        if sizes[sha] > MAX_BLOB_BYTES:
            return f"a blob of the change set is {sizes[sha]} bytes, over the {MAX_BLOB_BYTES}-byte cap"
        total += sizes[sha]
    if total > MAX_TOTAL_BYTES:
        return f"the change set's blobs total {total} bytes, over the {MAX_TOTAL_BYTES}-byte cap"
    contents: dict[str, bytes] = {}
    if blobs:
        out_b = _git_ok(repo, "cat-file", "--batch", feed=("\n".join(blobs) + "\n").encode("ascii"))
        pos = 0
        for sha in blobs:
            try:
                nl = out_b.index(b"\n", pos)
                header = out_b[pos:nl].decode("ascii", "replace").split(" ")
                size = int(header[2])
            except (ValueError, IndexError):
                raise GitFault(f"git cat-file --batch answered no readable header for {sha}") from None
            if header[0] != sha or size != sizes[sha]:
                raise GitFault(f"git cat-file --batch answered {header[0][:64]!r} for {sha}")
            contents[sha] = out_b[nl + 1:nl + 1 + size]
            pos = nl + 1 + size + 1
    entries = []
    budget = [_LINE_DIFF_BUDGET]
    for path, status, om, nm, osha, nsha in rows:
        non_blob = any(not _absent(m, s) and m == _GITLINK for m, s in ((om, osha), (nm, nsha)))
        old = b"" if _absent(om, osha) or om == _GITLINK else contents[osha]
        new = b"" if _absent(nm, nsha) or nm == _GITLINK else contents[nsha]
        binary = b"\x00" in old[:_BINARY_PROBE] or b"\x00" in new[:_BINARY_PROBE]
        if non_blob or binary:
            added = deleted = None
        else:
            added, deleted = _line_counts(old, new, budget)
        entries.append(Change(path=path, status=status, old_mode=om, new_mode=nm, old_blob=osha, new_blob=nsha,
                              old_size=sizes.get(osha, 0), new_size=sizes.get(nsha, 0), added=added,
                              deleted=deleted, binary=binary, non_blob=non_blob))
    return ChangeSet(tuple(entries))


# --- (A) the human authorise item (F11 §3) -------------------------------------------------------------------------

def authorise_payload(job: Job, record: ArtifactRecord, *, onto: str, signer_fp: str,
                      signed_at: datetime) -> dict[str, str]:
    """The payload a human signs to land ``record``'s run of ``job`` on ``onto``: its bytes are
    :func:`authorise_message`'s, signed in :data:`DISPATCH_AUTHORISE_NAMESPACE`. ``run`` is its decimal text."""
    return {"format": AUTHORISE_FORMAT, "store_id": job.store_id, "job_id": job.id, "run": str(record.run),
            "contract_digest": job.digest, "artifact_digest": artifact_digest(record), "onto": onto,
            "signer_fp": signer_fp, "signed_at": utc_stamp(signed_at)}


def authorise_message(payload: Mapping[str, Any]) -> bytes:
    return canonical_bytes(dict(payload))


def authorise_refusal(payload: Any, signature: Any, authority: AuthoritySnapshot, job: Job, *, run: int,
                      artifact: str, onto: str, tier: str, ssh_keygen: Path | str | None = None) -> str | None:
    """Why ``payload``/``signature`` is not the operator's authorisation to land run ``run`` (digest ``artifact``)
    of ``job`` on ``onto`` at landing tier ``tier``; ``None`` when it verifies. :func:`levain.dispatch.sign.refusal`'s
    order, with the run, artifact digest and branch in place of the expiry and the job's lease (filing to expiry) as
    the window; the key's class must cover ``tier``. Raises :class:`levain.authority.VerifierUnavailable` when the
    verifier cannot run."""
    if not isinstance(payload, Mapping) or set(payload) != AUTHORISE_KEYS:
        return "the payload is not a dispatch authorise record"
    if not all(isinstance(payload[k], str) for k in AUTHORISE_KEYS):
        return "every field of a dispatch authorise record is text"
    if any(len(payload[k]) > FIELD_MAX for k in AUTHORISE_KEYS):
        return f"a field of the dispatch authorise record is over {FIELD_MAX} characters"
    if payload["format"] != AUTHORISE_FORMAT:
        return f"authorise format {payload['format']!r} is not {AUTHORISE_FORMAT!r}"
    if payload["store_id"] != job.store_id:
        return "the authorisation is for another dispatch store"
    if payload["job_id"] != job.id:
        return "the authorisation is for another job"
    if payload["run"] != str(run):
        return "the authorisation names another run"
    if payload["contract_digest"] != job.digest:
        return "the authorisation is over another contract digest"
    if payload["artifact_digest"] != artifact:
        return "the authorisation is over another artifact digest"
    if payload["onto"] != onto:
        return "the authorisation names another branch"
    signed_at = payload["signed_at"]
    try:
        parse_stamp(signed_at)
    except ConsentError:
        return "the authorisation's signed_at is not a timestamp"
    if signed_at < job.filed_at:
        return "the authorisation was signed before the contract was filed"
    if signed_at >= job.expires_at:
        return "the authorisation was signed after the job's lease ended"
    if tier not in TIERS:
        return f"the landing tier {str(tier)[:16]!r} is not one of {TIERS}"
    if not isinstance(authority, AuthoritySnapshot):
        return "no authority snapshot to verify the signer against"
    key = authority.key(payload["signer_fp"])
    if key is None:
        return "the signer is not an enrolled key"
    if not key.active:
        return "the signer's key is revoked"
    if not class_covers(key.key_class, tier):
        return f"key class {key.key_class} does not cover {tier}"
    if not isinstance(signature, str) or not signature:
        return "the authorise signature is empty"
    if len(signature) > SIGNATURE_MAX:
        return f"the authorise signature is over {SIGNATURE_MAX} characters"
    if key.custody == PASSKEY:
        from levain.consent.passkey import answer_signature_problem

        problem = answer_signature_problem(signature, key.public_key)
        if problem is not None:
            return problem
    try:
        message = authorise_message(payload)
    except (TypeError, ValueError) as exc:
        return f"the authorise payload cannot be encoded: {exc}"
    kwargs: dict[str, Any] = {} if ssh_keygen is None else {"ssh_keygen": ssh_keygen}
    ok = verify_link_sig(message, signature, public_key=key.public_key, fingerprint=key.fingerprint,
                         namespace=DISPATCH_AUTHORISE_NAMESPACE, **kwargs)
    return None if ok else "the authorise signature does not verify"


# --- revalidation (F11 §4 step 2) ----------------------------------------------------------------------------------

def revalidation_problem(job: Job, *, resolve_template: Callable[[str], ResolvedTemplate | None],
                         policy: ProviderPolicy, now: str) -> str | None:
    """Why ``job``'s contract no longer holds at ``now``: its template id no longer resolves to the content hash the
    digest binds, the provider policy is not the one it binds, or it has expired. ``None`` when it holds. A
    resolver that raises is a refusal naming the template, never a pass."""
    contract = job.contract
    tid = contract.template.template_id
    try:
        template = resolve_template(tid)
    except DispatchError as exc:
        return f"template {tid} no longer resolves: {str(exc)[:200]}"
    if template is None:
        return f"template {tid} is no longer registered"
    if template.content_hash != contract.template.content_hash:
        return f"template {tid} is no longer the content the contract was signed for"
    if policy.content_hash() != contract.policy_hash:
        return "the provider policy is no longer the one the contract was signed under"
    if contract.expires_at <= now:
        return "the contract has expired"
    return None


# --- typed criteria (F11 §5) ---------------------------------------------------------------------------------------

def criteria_failure(criteria: Iterable[tuple[str, Any]], changes: ChangeSet, *, onto: str, verified_at: str,
                     now: str) -> str | None:
    """The first criterion that does not hold over ``changes``, as the line a SIGN shows; ``None`` when all do."""
    for key, value in criteria:
        if key == "onto":
            if onto not in value:
                return f"criterion onto: {onto} is not one of {list(value)}"
        elif key == "max_changed_lines":
            lines = changes.changed_lines
            if lines is None or lines > value:
                return f"criterion max_changed_lines: {lines} changed lines is over {value}"
        elif key == "max_bytes":
            if changes.staged_bytes > value:
                return f"criterion max_bytes: {changes.staged_bytes} bytes is over {value}"
        elif key == "max_files":
            if len(changes.entries) > value:
                return f"criterion max_files: {len(changes.entries)} changed files is over {value}"
        elif key == "paths":
            # exact, never folded: a case or Unicode-form variant is another file on a case-sensitive disk
            exact = {p for p in value if not p.endswith("/")}
            dirs = tuple(p for p in value if p.endswith("/"))
            for c in changes.entries:
                f = c.path
                if f not in exact and not f.startswith(dirs):
                    return f"criterion paths: {c.path} is not under {list(value)}"
        elif key == "no_new_files":
            new = [c.path for c in changes.entries if c.status == "A"]
            if new:
                return f"criterion no_new_files: {new[0]} is created"
        elif key == "max_age_hours":
            if parse_stamp(verified_at) + timedelta(hours=value) < parse_stamp(now):
                return f"criterion max_age_hours: verified at {verified_at}, more than {value} hours ago"
        else:
            return f"criterion {key!r} is not one this reader knows"
    return None


# --- the floor (F11 §4) --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Floor:
    """What the floor decided. ``failure`` is the first failing line (None when it passed); ``moved`` is True when
    that failure is MOVED BASE (the job then fails ``base_moved``). On a pass, ``record``/``changes``/``tier`` are
    the run to land, its change set and its landing tier, ``rule`` the (generation, index) of a rule that held."""
    failure: str | None
    moved: bool = False
    record: ArtifactRecord | None = None
    changes: ChangeSet | None = None
    tier: str | None = None
    rule: tuple[int, int] | None = None


def _refuse(line: str) -> Floor:
    return Floor(failure=line)


def _tier(landing_tier: Callable[[str, tuple[str, ...]], str], repo: str, paths: Sequence[str]) -> str | None:
    # joined and normalised, never resolved here: the classifier judges each path as written AND as resolved, and a
    # path resolved first loses the written half (a .claude symlink would land .claude/settings.json at T2)
    absolute = tuple(os.path.normpath(os.path.join(repo, p)) for p in paths)
    try:
        tier = landing_tier(repo, absolute)
    except Exception:                                    # fail closed: whatever it raised, nothing lands
        return None
    return tier if isinstance(tier, str) and tier in TIERS else None


def artifact_problem(repo: str, job: Job, record: ArtifactRecord) -> str | None:
    """Why ``record`` is not a run of ``job`` whose staged commit this repository holds as recorded (its own
    fields, the job, contract, base and template verifier it names, a commit whose one parent is the base and whose
    tree is the recorded tree); ``None`` when it is. Raises :class:`GitFault` when git cannot run."""
    problem = record.problem()
    if problem is not None:
        return problem
    contract = job.contract
    if record.job_id != job.id or record.contract_digest != job.digest or record.base != contract.base:
        return f"run {record.run}'s record is for another job, contract or base"
    t = contract.template
    if (record.verifier.get("id"), record.verifier.get("hash"), record.verifier.get("kind")) != \
            (t.verifier_id, t.verifier_hash, t.verifier_kind):
        return f"run {record.run}'s record names another verifier than the signed template's"
    shape = _commit_shape(repo, record.staged_commit)
    if shape is None:
        return f"run {record.run}'s staged commit {record.staged_commit} is not a commit in {repo}"
    parents, tree = shape
    if parents != [contract.base]:
        return f"run {record.run}'s staged commit does not have the contract's base as its one parent"
    if tree != record.tree:
        return f"run {record.run}'s staged commit's tree is not the recorded tree"
    return None


def _changes_problem(job: Job, changes: ChangeSet) -> str | None:
    # a path approves only as signed, byte for byte: folding (case, NFC) is for refusing clashes, never for approving,
    # since SRC/A.PY is a file src/a.py's signature never named on a case-sensitive disk (L3 r1, codex + complement)
    allowed = set(job.contract.write_paths)
    for c in changes.entries:
        if c.path not in allowed:
            return f"the change set writes {c.path}, which is not a path the contract's write list names exactly"
    if not changes.entries:
        return "the change set is empty"
    return None


def _contrast_problem(records: Sequence[ArtifactRecord], template: ResolvedTemplate) -> str | None:
    """F11 §4 step 5, the record half: R0, the declared verifier black-box, k >= 2 runs with distinct backend
    identities, every run verified."""
    if template.tier != "R0":
        return f"the template is {template.tier}; only an R0 result may land without a human signature"
    if template.k < 2:
        return f"the template's k={template.k}; a contrast needs at least 2 runs"
    if sorted(r.run for r in records) != list(range(template.k)):
        return f"the runs are not 0..{template.k - 1}, each once (the template's k={template.k})"
    for r in records:
        if r.verifier.get("kind") != "black-box":
            return f"run {r.run}'s recorded verifier kind is {r.verifier.get('kind')!r}, not black-box"
        if r.verifier.get("result") != "pass":
            return f"run {r.run} was not verified (result {r.verifier.get('result')!r})"
    identities = [r.model.get("backend_identity") for r in records]
    if len(set(identities)) != len(identities):
        return "two runs answered from the same backend identity; a contrast needs distinct ones"
    if len({r.tree for r in records}) != 1:
        return "the runs' staged trees differ; only a human can choose between diverging runs"
    return None


def _entries_problem(changes: ChangeSet) -> str | None:
    """F11 §4 step 5, the change half: regular files only, the type and mode they had at base (a new file 100644),
    no binary, no deletion."""
    for c in changes.entries:
        if c.non_blob:
            return f"{c.path} is a gitlink or other non-blob entry"
        if c.binary:
            return f"{c.path} is binary"
        if c.status == "D":
            return f"{c.path} is deleted"
        if c.status == "A":
            if c.new_mode != _NEW_MODE:
                return f"{c.path} is created with mode {c.new_mode}, not {_NEW_MODE}"
        elif c.status != "M" or c.old_mode != c.new_mode or c.new_mode not in _REGULAR:
            return f"{c.path} changes type or mode ({c.old_mode} -> {c.new_mode})"
    return None


def _rule_for(rules: Sequence[dregistry.AdoptionRule], job: Job, onto: str, changes: ChangeSet,
              record: ArtifactRecord, now: str) -> tuple[int, str | None]:
    """The index of the first rule for this template hash, repo and branch whose criteria all hold, else
    ``(-1, the line saying why)``."""
    contract = job.contract
    first: str | None = None
    for i, rule in enumerate(rules):
        if rule.template_hash != contract.template.content_hash or rule.repo != job.repo:
            continue
        failure = criteria_failure(rule.criteria, changes, onto=onto, verified_at=record.verified_at, now=now)
        if failure is None:
            return i, None
        first = first or failure
    return -1, first or "no adoption rule for this template, repository and branch"


def floor(job: Job, records: Sequence[ArtifactRecord], *, onto: str | None,
          landing_tier: Callable[[str, tuple[str, ...]], str],
          resolve_template: Callable[[str], ResolvedTemplate | None], policy: ProviderPolicy, now: str,
          human: Mapping[str, Any] | None = None,
          rules: Callable[[], tuple[int, Sequence[dregistry.AdoptionRule]]] | None = None) -> Floor:
    """F11 §4 for (A) (``human`` is the signed payload) or (B) (``rules`` returns the active adoption rules and
    their generation from one read, :func:`levain.dispatch.registry.adoption_rules_at`). The first failure decides.
    Raises :class:`GitFault` when git cannot run."""
    repo = job.repo
    # 1. state, and the branch the authority names
    if job.state not in ("verified", "recommended"):
        return _refuse(f"job {job.id} is {job.state}; the floor judges a verified or recommended job")
    if (human is None) == (rules is None):
        return _refuse("one authority: a human authorise item or the adoption rules")
    if human is not None:
        named = human.get("onto") if isinstance(human, Mapping) else None
        if not isinstance(named, str):
            return _refuse("the authorise item names no branch")
        if onto is not None and onto != named:
            return _refuse(f"--onto {onto} is not the branch the authorisation names ({named})")
        onto = named
    if onto is None:
        return _refuse("a land by rule names its branch (--onto)")
    # 2. revalidation
    problem = revalidation_problem(job, resolve_template=resolve_template, policy=policy, now=now)
    if problem is not None:
        return _refuse(problem)
    # 3. the artifact
    if human is not None:
        run_text = human.get("run")
        mine = [r for r in records if str(r.run) == run_text]
        if len(mine) != 1:
            return _refuse(f"no single record for run {str(run_text)[:8]}")
        record = mine[0]
        if record.problem() is None and artifact_digest(record) != human.get("artifact_digest"):
            return _refuse("the run's artifact digest is not the one the authorisation names")
        candidates = [record]
    else:
        candidates = sorted(records, key=lambda r: r.run if type(r.run) is int else 1 << 30)
        if not candidates:
            return _refuse("no verified run to land")
    for r in candidates:
        problem = artifact_problem(repo, job, r)
        if problem is not None:
            return _refuse(problem)
    record = candidates[0]
    changes = change_set(repo, record.base, record.staged_commit)
    if isinstance(changes, str):
        return _refuse(changes)
    problem = _changes_problem(job, changes)
    if problem is not None:
        return _refuse(problem)
    # 4. the landing tier
    tier = _tier(landing_tier, repo, changes.paths)
    if tier is None:
        return _refuse("the landing tier could not be read; nothing lands")
    rule_ref: tuple[int, int] | None = None
    if rules is not None:
        # 5. contrast-clean, the record half and the change half
        if tier != "T2":
            return _refuse(f"the change lands at {tier}; a rule lands T2 only, so this needs a human signature")
        template = job.contract.template
        problem = _contrast_problem(candidates, template) or _entries_problem(changes)
        if problem is not None:
            return _refuse(problem)
        # 6. the rule
        try:
            generation, listed = rules()
            active = tuple(listed)
        except DispatchError as exc:
            return _refuse(f"the adoption rules cannot be read: {str(exc)[:200]}")
        index, failure = _rule_for(active, job, onto, changes, record, now)
        if failure is not None:
            return _refuse(failure)
        rule_ref = (generation, index)
    # 7. the target
    problem = branch_problem(repo, onto)
    if problem is not None:
        return _refuse(problem)
    tip = _read_branch(repo, onto)
    if tip is None:
        return _refuse(f"refs/heads/{onto} does not exist")
    for r in records:
        if r.problem() is None and _object_type(repo, r.staged_commit) == "commit" and \
                _is_ancestor(repo, r.staged_commit, tip):
            return _refuse(f"{onto} already contains run {r.run}'s staged commit, a merge Levain did not make "
                           "under an authority; it is not an adoption")
    if tip != record.base:
        return Floor(failure=f"MOVED BASE: {onto} is at {tip}, not the contract's base {record.base}", moved=True,
                     record=record, changes=changes, tier=tier)
    return Floor(failure=None, record=record, changes=changes, tier=tier, rule=rule_ref)


# --- land (F11 §6) -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Landing:
    """What :func:`land` did. ``outcome``: ``adopted`` (``moved`` says whether Levain moved the branch),
    ``merge`` (``command`` is the operator's own merge to run; the job stays authorised), ``refused`` (nothing
    written; ``message`` is the failing line), ``failed`` (MOVED BASE: the job is failed ``base_moved``)."""
    outcome: str
    job: Job
    message: str = ""
    command: str = ""
    moved: bool = False


def _after_cas() -> None:
    """A seam between the swap and the ``adopted`` write, for the crash-recovery test; does nothing."""


def _intent(job: Job) -> dict[str, Any]:
    rows = [h for h in job.history if h.to_state == "authorised"]
    if len(rows) != 1:
        raise TransitionRefused(f"job {job.id} has no single authorisation")
    try:
        intent = json.loads(rows[0].evidence)
    except ValueError:
        raise TransitionRefused(f"job {job.id}'s authorisation holds no intent") from None
    keys = {"authority", "run", "onto", "staged", "base", "tree", "artifact_digest"}
    if not isinstance(intent, dict) or not keys <= set(intent):
        raise TransitionRefused(f"job {job.id}'s authorisation holds no intent")
    return intent


def _worktree_of(repo: str, onto: str) -> str | None:
    out = _git_ok(repo, "worktree", "list", "--porcelain", "-z")
    path: str | None = None
    want = f"branch refs/heads/{onto}"
    for rec in out.split(b"\x00"):
        line = rec.decode("utf-8", "surrogateescape")
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == want and path is not None:
            return path
    return None


def _delete_staged(repo: str, job_id: str) -> list[str]:
    """Delete the job's staged refs; returns the ones git would not delete (reported, never silent)."""
    out = _git_ok(repo, "for-each-ref", "--format=%(refname) %(objectname)",
                  f"refs/heads/levain/dispatch/{job_id}/")
    kept = []
    for line in out.decode("utf-8", "surrogateescape").splitlines():
        ref, _, sha = line.rpartition(" ")
        if _git(repo, "update-ref", "--no-deref", "-d", ref, sha).returncode != 0:
            kept.append(ref)
    return kept


def _settled(store: JobStore, job: Job, *, landed: str | None = None) -> Landing:
    """The job as another writer left it, after this land's transition lost to it (L3 r1): its outcome, never a
    raise. ``landed``: the commit THIS land already moved the branch to, said when the winner was not an adoption."""
    again = store.get(job.id)
    if again is None:
        raise TransitionRefused(f"no dispatch job {job.id}")
    if again.state == "adopted":
        if landed:      # this land's own CAS moved the branch; another finisher recorded it first
            return Landing(outcome="adopted", job=again, moved=True,
                           message=f"moved the branch to {landed}; another land recorded the adoption")
        return Landing(outcome="adopted", job=again, message="another land adopted this job")
    note = (f"; this land had already moved the branch to {landed}: revert it with `git revert` if it is unwanted"
            if landed else "")
    moved = bool(landed)
    if again.state == "failed" and again.history[-1].cause == "base_moved":
        return Landing(outcome="failed", job=again, moved=moved, message="another land found the base moved" + note)
    if again.state == "authorised":
        return Landing(outcome="refused", job=again, moved=moved,
                       message="the job was authorised by another land; run land again to finish it" + note)
    return Landing(outcome="refused", job=again, moved=moved, message=f"the job is {again.state}" + note)


def _move(store: JobStore, job: Job, to: str, cause: str, evidence: str) -> Job | None:
    """``job`` -> ``to`` only from the state it was read in; ``None`` when another writer moved it first."""
    try:
        return store._transition(job.id, to, cause, evidence=evidence, expect=job.state)
    except TransitionRefused:
        now = store.get(job.id)
        if now is not None and now.state != job.state:      # lost a race
            return None
        raise


def _fail_moved(store: JobStore, job: Job, line: str) -> Landing:
    # only from the state this land read: a stale request never fails another land's winning authorisation
    out = _move(store, job, "failed", "base_moved", json.dumps({"why": line[:500]}))
    if out is None:
        return _settled(store, job)
    kept = _delete_staged(job.repo, job.id)
    note = f"; staged refs git would not delete: {', '.join(kept)}" if kept else ""
    return Landing(outcome="failed", job=out, message=line + note)


def _finish(store: JobStore, job: Job, records: Sequence[ArtifactRecord]) -> Landing:
    """An authorised job: decide on ``onto`` alone, from the intent (F11 §6); nothing mutable is read."""
    intent = _intent(job)
    repo, onto, staged, base = job.repo, intent["onto"], intent["staged"], intent["base"]
    tip = _read_branch(repo, onto)
    if tip is not None and _object_type(repo, staged) == "commit" and _is_ancestor(repo, staged, tip):
        out = _move(store, job, "adopted", "adopted",
                    json.dumps({"landed": staged, "onto": onto, "moved": False}, sort_keys=True))
        if out is None:
            return _settled(store, job)
        return Landing(outcome="adopted", job=out, message=f"{onto} already contains {staged}")
    if tip != base:
        return _fail_moved(store, job, f"MOVED BASE: {onto} is at {tip}, not the authorised base {base}")
    # the intent's own artifact, unchanged, and the branch still a plain ref
    shape = _commit_shape(repo, staged)
    if shape is None or shape[0] != [base] or shape[1] != intent["tree"]:
        return Landing(outcome="refused", job=job, message="the staged commit is no longer the one authorised")
    mine = [r for r in records if r.problem() is None and r.run == intent["run"]]
    if mine and artifact_digest(mine[0]) != intent["artifact_digest"]:
        return Landing(outcome="refused", job=job, message="the run's record is no longer the one authorised")
    if _symbolic(repo, onto):
        return Landing(outcome="refused", job=job, message=f"refs/heads/{onto} is a symbolic ref")
    wt = _worktree_of(repo, onto)
    if wt is not None:
        # the path comes from a file in .git an agent can write: never a control character, always quoted
        if not wt.isprintable():
            return Landing(outcome="refused", job=job, message=f"{onto} is checked out in a worktree whose path "
                                                               "holds control characters; nothing is printed to run")
        command = f"git -C {shlex.quote(wt)} merge --ff-only {staged}"
        return Landing(outcome="merge", job=job, command=command,
                       message=f"{onto} is checked out in {wt}; Levain never touches a worktree. Run: {command}")
    cp = _git(repo, "update-ref", "--no-deref", f"refs/heads/{onto}", staged, base)
    if cp.returncode != 0:
        return Landing(outcome="refused", job=job, message=f"{onto} moved during the land; run land again")
    _after_cas()
    out = _move(store, job, "adopted", "adopted",
                json.dumps({"landed": staged, "onto": onto, "moved": True}, sort_keys=True))
    if out is None:
        # a human cancel (or another finisher) between this land's read and its write: say what moved
        return _settled(store, job, landed=staged)
    return Landing(outcome="adopted", job=out, moved=True, message=f"{onto} -> {staged}")


def land(store: JobStore, job_id: str, records: Sequence[ArtifactRecord], *,
         landing_tier: Callable[[str, tuple[str, ...]], str],
         resolve_template: Callable[[str], ResolvedTemplate | None], policy: ProviderPolicy,
         onto: str | None = None, human: tuple[Any, Any, Any] | None = None,
         rules: Callable[[], tuple[int, Sequence[dregistry.AdoptionRule]]] | None = None,
         now: str | None = None) -> Landing:
    """F11 §6 for job ``job_id``. ``human`` is ``(payload, signature, authority)`` for (A); ``rules`` returns the
    active adoption rules and their generation for (B) (``lambda: adoption_rules_at(control_store)``). A MOVED BASE
    before authorisation fails the job only under an (A) item whose signature verifies (the human named that run and branch);
    an unverified item or a rule land is refused and the job and its staged refs are kept. ``landing_tier`` has no
    default. A verified/recommended job passes the floor and is authorised (the INTENT) before anything moves; an
    authorised job goes straight to :func:`_finish`. A refused signature is a ``refused`` landing; a verifier that
    cannot run (:class:`levain.authority.VerifierUnavailable`) and a git that cannot run (:class:`GitFault`) raise."""
    job = store.get(job_id)
    if job is None:
        raise TransitionRefused(f"no dispatch job {str(job_id)[:40]!r}")
    if job.state == "authorised":
        return _finish(store, job, records)
    stamp = now or utc_stamp(store._clock())
    payload = human[0] if human is not None else None
    verdict = floor(job, records, onto=onto, landing_tier=landing_tier, resolve_template=resolve_template,
                    policy=policy, now=stamp, human=payload if isinstance(payload, Mapping) else
                    ({} if human is not None else None), rules=rules)
    if verdict.moved:
        line = verdict.failure or "MOVED BASE"
        if human is not None and verdict.record is not None and verdict.tier is not None and \
                _human_refusal(store, job, human, verdict.record, verdict.tier) is None:
            return _fail_moved(store, job, line)
        return Landing(outcome="refused", job=job, message=f"{line}; nothing was failed or deleted (only a verified "
                                                            "authorisation fails a job on a moved base)")
    if verdict.failure is not None:
        return Landing(outcome="refused", job=job, message=verdict.failure)
    record, changes = verdict.record, verdict.changes
    assert record is not None and changes is not None
    target = onto if human is None else payload["onto"]           # type: ignore[index]
    assert isinstance(target, str)                                 # the floor passed only with a named branch
    intent: dict[str, Any] = {"authority": "A" if human is not None else "B", "run": record.run, "onto": target,
                              "staged": record.staged_commit, "base": record.base, "tree": record.tree,
                              "artifact_digest": artifact_digest(record), "tier": verdict.tier}
    try:
        if human is not None:
            sig, authority = human[1], human[2]
            intent["signer_fp"] = payload["signer_fp"]                 # type: ignore[index]
            refusal = _bound_refusal(run=record.run, artifact=intent["artifact_digest"], onto=target,
                                     tier=verdict.tier or "")
            job = store.authorise(job.id, cause="human_authorises", evidence=json.dumps(intent, sort_keys=True),
                                  human=(payload, sig, authority, refusal))
        else:
            assert verdict.rule is not None
            intent["rule"] = {"generation": verdict.rule[0], "index": verdict.rule[1]}
            job = store.authorise(job.id, cause="human_adoption_rule", evidence=json.dumps(intent, sort_keys=True))
    except SignatureRefused as exc:
        return Landing(outcome="refused", job=job, message=str(exc))
    except TransitionRefused:
        # another land authorised the job after this one's floor read it: the authorisation written first is the
        # authority, so finish from ITS intent, or report where that land already took the job
        again = store.get(job.id)
        if again is None:
            raise
        if again.state == "adopted":
            return Landing(outcome="adopted", job=again, message="another land adopted this job")
        if again.state == "failed" and again.history[-1].cause == "base_moved":
            return Landing(outcome="failed", job=again, message="another land found the base moved")
        if again.state != "authorised":
            raise
        job = again
    return _finish(store, job, records)


def _human_refusal(store: JobStore, job: Job, human: tuple[Any, Any, Any], record: ArtifactRecord,
                   tier: str) -> str | None:
    """Why the (A) item does not verify for ``record``'s run on the branch it names, judged as
    :meth:`JobStore.authorise` judges it (a fresh replay of the authority), writing nothing."""
    from levain.consent.queue import AuthoritySource
    from levain.dispatch.jobs import _replay

    payload, signature, authority = human
    if not isinstance(payload, Mapping) or type(authority) is not AuthoritySource:
        return "no authorise item to verify"
    return authorise_refusal(payload, signature, _replay(authority), job, run=record.run,
                             artifact=artifact_digest(record), onto=str(payload.get("onto")), tier=tier,
                             ssh_keygen=store._ssh_keygen)


def _bound_refusal(*, run: int, artifact: str, onto: str, tier: str) -> Callable[..., str | None]:
    def check(payload: Any, signature: Any, snapshot: AuthoritySnapshot, job: Job, *,
              ssh_keygen: Path | str | None = None) -> str | None:
        return authorise_refusal(payload, signature, snapshot, job, run=run, artifact=artifact, onto=onto,
                                 tier=tier, ssh_keygen=ssh_keygen)
    return check


# --- E0, staging (for tests until the verifier driver stages real runs) -------------------------------------------

def stage_commit(repo: str, *, job_id: str, run: int, contract_digest: str, base: str, tree: str,
                 verified_at: str) -> str:
    """Make the deterministic staged commit of one run (parent ``base``, ``tree``, author and committer
    ``levain dispatch`` dated ``verified_at``, a message naming the job, the run, the contract digest and the tree)
    and point :func:`staged_ref` at it (created, never moved); returns its sha. The same inputs make the same sha, so
    a call whose ref already points at exactly that sha returns it (a crash between the ref and the record it was made
    for must not wedge the retry); a ref that points anywhere else is a :class:`DispatchError`."""
    when = f"{int(parse_stamp(verified_at).timestamp())} +0000"
    env = {"GIT_AUTHOR_NAME": _STAGE_NAME, "GIT_AUTHOR_EMAIL": _STAGE_EMAIL, "GIT_AUTHOR_DATE": when,
           "GIT_COMMITTER_NAME": _STAGE_NAME, "GIT_COMMITTER_EMAIL": _STAGE_EMAIL, "GIT_COMMITTER_DATE": when}
    message = (f"levain dispatch {job_id} run {run}\n\ncontract {contract_digest}\ntree {tree}\n").encode("utf-8")
    sha = _git_ok(repo, "commit-tree", "--no-gpg-sign", tree, "-p", base, feed=message,
                  extra_env=env).decode("ascii").strip()
    ref = staged_ref(job_id, run)
    held = _read_ref(repo, ref)
    if held is None:
        if _git(repo, "update-ref", "--no-deref", ref, sha, "0" * len(sha)).returncode == 0:
            return sha
        # another writer created it between the read and the create: judge what it holds
        held = _read_ref(repo, ref)
    if held != sha:
        raise DispatchError(f"{ref} already points at {str(held)[:64]}, not this run's staged commit {sha}; a staged "
                            "ref is never moved")
    return sha


def _read_ref(repo: str, ref: str) -> str | None:
    """The object ``ref`` names, or ``None`` when it does not exist (rev-parse --quiet exit 1); any other failure
    raises."""
    cp = _git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options", ref)
    if cp.returncode == 1:
        return None
    if cp.returncode != 0:
        raise GitFault(f"git rev-parse failed in {repo} (exit {cp.returncode})")
    return cp.stdout.decode("ascii").strip()
