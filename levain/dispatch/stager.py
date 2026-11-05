"""levain.dispatch.stager: the OPERATOR-uid half of the workspace stager (levain 34, W1; design
``1105-32/DESIGN_render_wiring.md`` finding 3, cut W1; research ``1105-3/RESEARCH_stager.md``). Its sibling on the run
server is :mod:`levain.slot.workspace`, which verifies what this module builds.

This module runs ONLY as the operator's uid, in the fire (``DispatchSessionLauncher.stage_workspace`` imports it lazily).
It is not in the dispatch run server's import closure (``tests/ci/root_closure.py``): it reads the operator's repo,
which the server cannot, and it shells out to git and the policy store as the operator.

* :func:`resolve_head`: the install's repo and branch setting -> a 40-hex commit sha, resolved NOW (the fire), by git,
  from the named local branch (default: the repo's checked-out HEAD). The sha, not the branch, is what the launch
  carries and the run records.
* :func:`build_upload`: for a launch's workspace member, the snapshot of that commit (tracked files at that commit
  only: the working tree, the index and untracked files are never read) built by :mod:`levain.dispatch.snapshot` in a
  private temp directory (0700) under the provider policy's NEVER list and the content scan, the repo-layer check
  (``readset._layer_check``: a snapshot whose ``.claude/settings*.json`` loosens the sandbox is REFUSED, never stripped)
  and the refused names the server also refuses (``workspace.refused_name_carried``), then cut into the one parentless
  commit's bundle and read back into memory. The temp directory is removed whatever happens, before this returns.
  Every refusal is a :class:`StageRefused`, raised before the caller has sent a byte.

W1 stages with an EMPTY withhold (a role-declared ``withhold`` is W2); the NEVER list and the scan stay authoritative
HERE, where the repo is readable: the server verifies hash identity and the repo layer, and does not repeat either.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass

from levain.agents.session import Workspace, branch_problem
from levain.dispatch import DispatchError
from levain.dispatch import readset as RS
from levain.dispatch import snapshot as SN
from levain.dispatch.policy import ProviderPolicy
from levain.slot import workspace as WS

__all__ = ["PROVIDER", "StageRefused", "Upload", "build_upload", "never_policy", "resolve_head"]

_log = logging.getLogger("levain.dispatch.stager")

#: The provider name the wrapped policy carries: the NEVER list is checked first and alone decides, and this provider
#: reads everything (a snapshot is the whole repo; narrowing it by a read allowlist is not what W1 stages).
PROVIDER = "workspace"


class StageRefused(DispatchError):
    """The workspace cannot be staged, by name (``name``) with a human ``detail``. Nothing was sent."""

    def __init__(self, name: str, detail: str = "") -> None:
        self.name = name
        self.detail = detail
        super().__init__(f"{name}: {detail}" if detail else name)


@dataclass(frozen=True)
class Upload:
    """The two parts a launch uploads: the snapshot manifest's canonical bytes and the bundle, with the commit and
    object count the server will derive from the manifest (what a test compares)."""
    manifest: bytes
    bundle: bytes
    commit: str
    objects: int


def never_policy(policy: ProviderPolicy) -> ProviderPolicy:
    """``policy``'s NEVER list with one provider that reads everything: the form ``plan_snapshot`` judges a whole-repo
    snapshot under (``policy.refusal`` checks NEVER first, then the provider's read allowlist)."""
    return ProviderPolicy(never=policy.never, providers={PROVIDER: ("**",)})


def _control_policy() -> ProviderPolicy:
    """The operator's active provider policy (the signed dispatch control store). No store, or one that cannot be read,
    is a refusal: with no NEVER list to apply, nothing is staged."""
    from levain.dispatch import cli as dispatch_cli          # heavy; only a fire that stages a repo needs it
    try:
        got = dispatch_cli.resolvers()
    except Exception as exc:  # noqa: BLE001 - an unreadable or untrusted store authorises nothing
        raise StageRefused("workspace-policy-unavailable",
                           f"the dispatch provider policy could not be read ({type(exc).__name__})") from None
    if got is None:
        raise StageRefused("workspace-policy-unavailable", "no dispatch control store holds a provider policy")
    return got[1]


def resolve_head(repo: str, branch: str | None = None) -> str:
    """The 40-hex commit ``branch`` (a local branch of ``repo``; None: the checked-out HEAD) names right now. Refuses
    (:class:`StageRefused`) a repo that is not an absolute path, a branch outside :func:`branch_problem`'s grammar, one
    git cannot resolve to a commit, and any answer that is not 40 lowercase hex (a SHA-256 repo included)."""
    if type(repo) is not str or not os.path.isabs(repo) or "\0" in repo:
        raise StageRefused("workspace-repo", "the repo must be an absolute path")
    if branch is None:
        rev = "HEAD^{commit}"
    else:
        problem = branch_problem(branch)
        if problem is not None:
            raise StageRefused("workspace-branch", problem)
        rev = f"refs/heads/{branch}^{{commit}}"
    try:
        out = SN._git(repo, "rev-parse", "--verify", "-q", rev)
    except SN.SnapshotRefused:
        raise StageRefused("workspace-branch", f"{branch or 'HEAD'} does not resolve to a commit of {repo}") from None
    head = out.decode("ascii", "replace").strip()
    if SN._SHA1.fullmatch(head) is None:
        raise StageRefused("workspace-head", "git did not answer a 40-hex commit sha")
    return head


def _remove_tree(root: str) -> None:
    try:
        shutil.rmtree(root)
    except OSError as exc:
        _log.warning("the workspace temp %s could not be removed (%s); retrying", root, type(exc).__name__)
        shutil.rmtree(root, ignore_errors=True)
        if os.path.lexists(root):
            _log.error("the workspace temp %s is still there", root)


def build_upload(ws: Workspace, *, policy: ProviderPolicy | None = None, tmp_parent: str | None = None) -> Upload:
    """The upload for ``ws`` (module docstring), or :class:`StageRefused`. ``policy`` is the provider policy whose NEVER
    list applies (default: the operator's active one, :func:`_control_policy`); ``tmp_parent`` is where the private
    temp directory is made (default: the system temp). The temp directory is gone when this returns or raises."""
    if type(ws) is not Workspace:
        raise StageRefused("workspace-member", "not a workspace member")
    if SN._SHA1.fullmatch(ws.head) is None:                      # Workspace checks it too: this is the bytes' own gate
        raise StageRefused("workspace-head", "the head is not a 40-hex commit sha")
    if ws.withhold and not WS.W1_WITHHOLD_ALLOWED:
        raise StageRefused("withhold-not-in-w1", "W1 stages a snapshot with an empty withhold")
    wrapped = never_policy(policy if policy is not None else _control_policy())
    root = os.path.realpath(tempfile.mkdtemp(prefix="levain-ws-", dir=tmp_parent))      # 0700, ours
    try:
        return _build_in(ws, wrapped, root)
    finally:
        _remove_tree(root)


def _build_in(ws: Workspace, policy: ProviderPolicy, root: str) -> Upload:
    try:
        rs, man = SN.build_snapshot(ws.repo, os.path.join(root, "snapshot"), [], policy=policy, provider=PROVIDER,
                                    head=ws.head)
    except SN.SnapshotRefused as exc:
        raise StageRefused(exc.name, exc.detail) from None
    if man.head != ws.head:
        raise StageRefused("workspace-head", "the snapshot was cut from another commit than the launch names")
    carried = WS.refused_name_carried(man.entries)
    if carried is not None:
        raise StageRefused(*carried)
    try:
        RS._layer_check(rs.repo_dir, rs.commit)              # the very check the server runs on what it receives
    except RS.ReadSetRefused as exc:
        raise StageRefused(exc.name, exc.detail) from None
    commit, _tree, objects = SN.snapshot_commit(man)
    if commit != rs.commit:
        raise StageRefused("snapshot-commit-mismatch", "the built commit is not the manifest's derived one")
    manifest = man.canonical()
    path = os.path.join(root, "repo.bundle")
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            RS._bundle_into(rs.repo_dir, fd, RS._scratch_env())
            if RS._prefix_problem(fd, commit, objects) is not None:
                raise StageRefused("bundle-shape", "the bundle git cut is not the header and pack the manifest derives")
        finally:
            os.close(fd)
        with open(path, "rb") as fh:
            bundle = fh.read()
    except RS.ReadSetRefused as exc:
        raise StageRefused(exc.name, exc.detail) from None
    if len(manifest) > WS.MANIFEST_MAX_BYTES:
        raise StageRefused("manifest-over-cap", f"{len(manifest)} bytes is over {WS.MANIFEST_MAX_BYTES}")
    if len(bundle) > WS.BUNDLE_MAX_BYTES:
        raise StageRefused("bundle-over-cap", f"{len(bundle)} bytes is over {WS.BUNDLE_MAX_BYTES}")
    return Upload(manifest, bundle, commit, objects)
