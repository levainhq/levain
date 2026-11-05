"""levain 34, W1 (operator-uid half): the sender of the workspace stager (``levain.dispatch.stager`` and the
``DispatchSessionLauncher.stage_workspace`` seam, the install's per-agent ``workspaces`` setting, and the fire's handling
of a workspace).

Held here, against the REAL run server and the server half's test rig (``tests.test_slot_dispatch_workspace.World``): a
full client -> RunServer round trip (the client builds the snapshot of a real git repo, bundles it, uploads it on the
LAUNCH connection and the server stages exactly those bytes); dirty working-tree edits, the index and untracked files do
NOT ride along; a snapshot carrying a loosening ``.claude/settings.json`` (or ``.mcp.json``) is refused BEFORE any byte
is sent; the temp directory is gone on success and on every refusal; a head that is not 40-hex is refused; the branch is
resolved at the FIRE and the commit recorded; a refusal before LAUNCH settles the run at zero as
``session_refused:workspace``.

Not held (UNVERIFIED until the VM rows): the unit as ``_levain_dispatch``, a real ``staging/`` made by root, the slot's
clone of the staged file, and a repo of realistic size (calibration is in the lane RETURN, not in a test).
"""
from __future__ import annotations

import base64
import dataclasses
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from levain.agents import session as S
from levain.agents import session_client as C
from levain.agents import session_launcher as L
from levain.agents import session_wire as W
from levain.dispatch import readset as RS
from levain.dispatch import snapshot as SN
from levain.dispatch import stager
from levain.dispatch.policy import ProviderPolicy
from levain.slot import workspace as WS
from tests.test_agents_session_launch import EXP, Stub, _occurrence, _reservation, _run, rig  # noqa: F401
from tests.test_dispatch_snapshot import git, make_repo
from tests.test_slot_dispatch_workspace import REFUSED, World

pytestmark = [pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen"),
              pytest.mark.skipif(shutil.which("git") is None, reason="needs git")]

FILES = {"src/a.py": "a = 1\n", "README.md": "# hello\n", "docs/x.txt": "x\n"}
POLICY = ProviderPolicy.from_mapping({"never": ["**/.env", "**/*.pem"], "providers": {}})


@pytest.fixture(autouse=True)
def _policy(monkeypatch):
    """The operator's active provider policy is the signed control store's; here it is a value."""
    monkeypatch.setattr(stager, "_control_policy", lambda: POLICY)


@pytest.fixture
def tmpdir_(tmp_path, monkeypatch) -> Path:
    """The system temp the sender makes its private directory in, so a test can see it empty."""
    d = tmp_path / "systmp"
    d.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(d))
    return d


class RealSender(World):
    """The server half's rig with the REAL client: ``launch`` builds, uploads and then LAUNCHes (the base class's
    ``launch`` skips the sender so its tests can upload tampered bytes by hand)."""

    connects: list

    def launch(self, sock, key, ws):
        r = self.rig
        self.connects = getattr(self, "connects", [])

        def connect():
            self.connects.append(key)                                  # a connection the client took
            return sock

        client = C.DispatchSessionLauncher(connect, lambda occ: C.link_proof(r.store, r.world.root, occ, now=r.now()))
        return client.spawn(S.SessionLaunch(self.oid, key, "claude", "sonnet", "b", "passthrough", EXP, ws))


@pytest.fixture
def w(tmp_path, tmpdir_) -> RealSender:
    return RealSender(tmp_path)


def op_repo(tmp_path: Path, files=None, name="op") -> Path:
    top = Path(os.path.realpath(tmp_path)) / name
    top.mkdir()
    return make_repo(top, FILES if files is None else files)


def member(repo: Path, branch=None) -> S.Workspace:
    return S.Workspace(str(repo), stager.resolve_head(str(repo), branch), (), None, branch)


def launch_of(ws, key="s-00000000000000b1") -> S.SessionLaunch:
    return S.SessionLaunch("oc-" + "0" * 32, key, "claude", "sonnet", "b", "passthrough", EXP, ws)


def clone_of(bundle: bytes, where: Path) -> Path:
    f = where.parent / (where.name + ".bundle")
    f.write_bytes(bundle)
    subprocess.run(["git", "clone", "-q", str(f), str(where)], check=True, capture_output=True, timeout=60,
                   env={**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, "GIT_CONFIG_NOSYSTEM": "1"})
    return where


class RecordingConn:
    """A connection that answers the upload ops as the server does (or lies about the byte count) and keeps every
    request, so a test can say that nothing was sent."""

    def __init__(self, lie=0) -> None:
        self.requests: list = []
        self.lie, self.closed = lie, False

    def request(self, op, expect, *, timeout=10.0, **fields):
        self.requests.append((op, fields))
        if op == "WSDATA":
            return {"received": fields["offset"] + len(base64.b64decode(fields["data"])) + self.lie}
        return {}

    def close(self):
        self.closed = True


# --- resolving the commit at the fire -----------------------------------------------------------------------------------------

def test_the_branch_setting_resolves_to_that_branchs_tip_and_none_to_the_checked_out_head(tmp_path):
    repo = op_repo(tmp_path)
    main = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "checkout", "-q", "-b", "feat")
    (repo / "src" / "a.py").write_text("a = 2\n")
    git(repo, "commit", "-qam", "c2")
    feat = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "checkout", "-q", "main")
    assert main != feat
    assert stager.resolve_head(str(repo), "feat") == feat
    assert stager.resolve_head(str(repo), "main") == main
    assert stager.resolve_head(str(repo)) == main                        # the checked-out HEAD
    git(repo, "checkout", "-q", "feat")
    assert stager.resolve_head(str(repo)) == feat                        # it follows the checkout, at the moment asked


def test_a_branch_that_is_a_tag_or_an_option_or_missing_does_not_resolve(tmp_path):
    repo = op_repo(tmp_path)
    git(repo, "tag", "v1")
    for bad in ("v1", "nope", "-x", "a..b", "../x", "x/", "a@{1}", "", "a b", "x.lock"):
        with pytest.raises(stager.StageRefused) as exc:
            stager.resolve_head(str(repo), bad)
        assert exc.value.name == "workspace-branch", bad
    with pytest.raises(stager.StageRefused) as exc:
        stager.resolve_head("relative/repo")
    assert exc.value.name == "workspace-repo"
    with pytest.raises(stager.StageRefused):
        stager.resolve_head(str(tmp_path / "not-a-repo"))


def test_an_answer_that_is_not_40_hex_is_refused(tmp_path, monkeypatch):
    repo = op_repo(tmp_path)
    for odd in (b"abc123\n", b"A" * 40 + b"\n", b"a" * 64 + b"\n", b"\n"):
        monkeypatch.setattr(SN, "_git", lambda *a, _o=odd, **k: _o)
        with pytest.raises(stager.StageRefused) as exc:
            stager.resolve_head(str(repo))
        assert exc.value.name == "workspace-head"


def test_a_workspace_member_is_a_40_hex_head_or_it_does_not_exist():
    for bad in ("abc", "A" * 40, "g" * 40, "a" * 39, "a" * 41, "a" * 64, ""):
        with pytest.raises(ValueError):
            S.Workspace("/r", bad)
    with pytest.raises(ValueError):
        S.Workspace("/r", "a" * 40, (), None, "-x")


def test_the_snapshot_takes_only_a_40_hex_commit_it_holds(tmp_path):
    repo = op_repo(tmp_path)
    head = git(repo, "rev-parse", "HEAD").strip()
    assert SN.plan_snapshot(str(repo), head=head)[0].head == head
    for bad in ("HEAD", "main", head[:12], head.upper(), head + "0"):
        with pytest.raises(SN.SnapshotRefused) as exc:
            SN.plan_snapshot(str(repo), head=bad)
        assert exc.value.name == "snapshot-repo" and exc.value.detail.startswith("head is not a 40-hex"), bad
    with pytest.raises(SN.SnapshotRefused) as exc:                          # well-formed, but git does not hold it
        SN.plan_snapshot(str(repo), head="0" * 40)
    assert exc.value.name == "snapshot-git-failed"
    git(repo, "tag", "-a", "-m", "t", "v1")
    tag_object = git(repo, "rev-parse", "v1").strip()                       # 40-hex, resolves to the commit, is not it
    assert tag_object != head
    with pytest.raises(SN.SnapshotRefused) as exc:
        SN.plan_snapshot(str(repo), head=tag_object)
    assert exc.value.name == "snapshot-repo" and exc.value.detail.startswith("head does not name a commit")
    tree = git(repo, "rev-parse", "HEAD^{tree}").strip()                    # a real object that is not a commit
    with pytest.raises(SN.SnapshotRefused):
        SN.plan_snapshot(str(repo), head=tree)


def test_a_stager_handed_a_head_that_is_not_40_hex_refuses_before_building(tmp_path, tmpdir_):
    repo = op_repo(tmp_path)
    ws = member(repo)
    object.__setattr__(ws, "head", "abc")                                    # past the member's own gate
    with pytest.raises(stager.StageRefused) as exc:
        stager.build_upload(ws)
    assert exc.value.name == "workspace-head" and os.listdir(tmpdir_) == []


# --- what the snapshot is ---------------------------------------------------------------------------------------------------------

def test_dirty_edits_the_index_and_untracked_files_do_not_ride_along(tmp_path, tmpdir_):
    repo = op_repo(tmp_path)
    (repo / "src" / "a.py").write_text("a = 'DIRTY'\n")                      # unstaged edit to a tracked file
    (repo / "README.md").write_text("# staged\n")
    git(repo, "add", "README.md")                                            # a staged edit
    (repo / "untracked.txt").write_text("never\n")
    (repo / "docs" / "new.txt").write_text("also never\n")
    ws = member(repo)                                                        # the fire resolves WITH the tree dirty
    up = stager.build_upload(ws)
    got = clone_of(up.bundle, tmp_path / "got")
    assert sorted(git(got, "ls-tree", "-r", "--name-only", "HEAD").split()) == sorted(FILES)
    assert git(got, "show", "HEAD:src/a.py") == "a = 1\n" and git(got, "show", "HEAD:README.md") == "# hello\n"
    assert b"DIRTY" not in up.bundle and b"staged" not in up.bundle and b"never" not in up.bundle
    assert git(got, "rev-list", "--count", "HEAD").strip() == "1"            # one parentless commit, no history
    assert git(got, "rev-parse", "HEAD").strip() == up.commit


def test_the_branch_named_is_the_one_staged_not_the_checked_out_one(tmp_path, tmpdir_):
    repo = op_repo(tmp_path)
    git(repo, "checkout", "-q", "-b", "feat")
    (repo / "src" / "b.py").write_text("b = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "c2")
    git(repo, "checkout", "-q", "main")
    up = stager.build_upload(member(repo, "feat"))
    assert "src/b.py" in git(clone_of(up.bundle, tmp_path / "got"), "ls-tree", "-r", "--name-only", "HEAD").split()
    up2 = stager.build_upload(member(repo, "main"))
    assert "src/b.py" not in git(clone_of(up2.bundle, tmp_path / "got2"), "ls-tree", "-r", "--name-only", "HEAD").split()


def test_the_never_list_and_the_content_scan_refuse_here_whole(tmp_path, tmpdir_):
    repo = op_repo(tmp_path, {**FILES, "app/.env": "X=1\n"})
    with pytest.raises(stager.StageRefused) as exc:
        stager.build_upload(member(repo))
    assert exc.value.name == "snapshot-policy" and ".env" in exc.value.detail
    repo2 = op_repo(tmp_path, {**FILES, "src/k.py": "key = 'AKIAABCDEFGHIJKLMNOP'\n"}, name="op2")
    with pytest.raises(stager.StageRefused) as exc:
        stager.build_upload(member(repo2))
    assert exc.value.name == "snapshot-secret" and "AKIA" not in exc.value.detail
    assert os.listdir(tmpdir_) == []


def test_no_provider_policy_is_a_refusal_not_an_unguarded_snapshot(tmp_path, tmpdir_, monkeypatch):
    monkeypatch.undo()                                                       # the autouse policy off: the real source
    monkeypatch.setattr(tempfile, "tempdir", str(tmpdir_))
    from levain.dispatch import cli as dispatch_cli
    monkeypatch.setattr(dispatch_cli, "resolvers", lambda *a, **k: None)
    repo = op_repo(tmp_path)
    with pytest.raises(stager.StageRefused) as exc:
        stager.build_upload(member(repo))
    assert exc.value.name == "workspace-policy-unavailable" and os.listdir(tmpdir_) == []


def test_the_temp_directory_is_private_and_gone_after_success_and_after_a_failed_bundle(tmp_path, tmpdir_, monkeypatch):
    repo = op_repo(tmp_path)
    seen: list = []
    real = SN.build_snapshot

    def spy(repo_, dest, *a, **k):
        seen.append((os.path.dirname(dest), oct(os.stat(os.path.dirname(dest)).st_mode & 0o777)))
        return real(repo_, dest, *a, **k)

    monkeypatch.setattr(SN, "build_snapshot", spy)
    stager.build_upload(member(repo))
    assert seen and seen[0][1] == "0o700" and os.path.dirname(seen[0][0]) == os.path.realpath(tmpdir_)
    assert os.listdir(tmpdir_) == []
    monkeypatch.setattr(RS, "_bundle_into", lambda *a, **k: (_ for _ in ()).throw(RS.ReadSetRefused("readset-git-failed", "x")))
    with pytest.raises(stager.StageRefused) as exc:
        stager.build_upload(member(repo))
    assert exc.value.name == "readset-git-failed" and os.listdir(tmpdir_) == []
    monkeypatch.setattr(RS, "_bundle_into", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        stager.build_upload(member(repo))
    assert os.listdir(tmpdir_) == []


# --- refused before a byte is sent --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("files,cause", [
    ({".claude/settings.json": '{"disableAllHooks": true}', "src/a.py": "a\n"}, "loosening"),
    ({".claude/settings.local.json": '{"disableAllHooks": true}', "src/a.py": "a\n"}, "loosening"),
    ({".mcp.json": "{}\n", "src/a.py": "a\n"}, "repo-carries-mcp-json"),
])
def test_a_loosening_or_mcp_snapshot_is_refused_before_any_byte_is_sent(tmp_path, tmpdir_, files, cause):
    repo = op_repo(tmp_path, files)
    conn = RecordingConn()
    launcher = C.DispatchSessionLauncher(lambda: None, lambda occ: None)
    with pytest.raises(C.WorkspaceNotSent) as exc:
        launcher.stage_workspace(conn, launch_of(member(repo)))
    assert exc.value.name == S.WORKSPACE_REFUSED and exc.value.detail.startswith(cause), exc.value.detail
    assert conn.requests == [] and os.listdir(tmpdir_) == []


def test_a_clean_settings_file_stages(tmp_path, tmpdir_):
    repo = op_repo(tmp_path, {".claude/settings.json": '{"permissions": {"allow": ["Read"]}}', "src/a.py": "a\n"})
    conn = RecordingConn()
    C.DispatchSessionLauncher(lambda: None, lambda occ: None).stage_workspace(conn, launch_of(member(repo)))
    assert conn.requests[0][0] == "WSBEGIN" and conn.requests[-1][0] == "WSDATA"


def test_the_refused_names_come_from_the_one_derivation_the_server_uses(monkeypatch, tmp_path, tmpdir_):
    monkeypatch.setattr(WS, "DISPATCH_REFUSED_NAMES", ())                     # parity on: both sides stop refusing it
    repo = op_repo(tmp_path, {".mcp.json": "{}\n", "src/a.py": "a\n"})
    conn = RecordingConn()
    C.DispatchSessionLauncher(lambda: None, lambda occ: None).stage_workspace(conn, launch_of(member(repo)))
    assert conn.requests


def test_the_upload_is_begin_then_whole_chunks_in_order_and_an_ack_that_disagrees_is_refused(tmp_path, tmpdir_):
    repo = op_repo(tmp_path, {**FILES, "big.bin": base64.b64encode(os.urandom(3 * C.WS_CHUNK_BYTES)).decode()})
    conn = RecordingConn()
    ws = member(repo)
    C.DispatchSessionLauncher(lambda: None, lambda occ: None).stage_workspace(conn, launch_of(ws))
    ops = [(op, f.get("part"), f.get("offset")) for op, f in conn.requests]
    assert ops[0][0] == "WSBEGIN" and ops[1] == ("WSDATA", "manifest", 0)
    bundle = [o for o in ops if o[1] == "bundle"]
    assert len(bundle) >= 2 and [o[2] for o in bundle] == sorted(o[2] for o in bundle) and bundle[0][2] == 0
    lying = RecordingConn(lie=1)
    with pytest.raises(C.WorkspaceNotSent) as exc:
        C.DispatchSessionLauncher(lambda: None, lambda occ: None).stage_workspace(lying, launch_of(ws))
    assert exc.value.detail.startswith("upload-ack") and lying.closed and len(lying.requests) == 2


def test_a_bundle_that_is_not_the_manifests_is_refused_here_too(tmp_path, tmpdir_, monkeypatch):
    repo = op_repo(tmp_path)

    def garbage(repo_dir, fd, env):
        os.write(fd, b"not a bundle at all")

    monkeypatch.setattr(RS, "_bundle_into", garbage)
    with pytest.raises(stager.StageRefused) as exc:
        stager.build_upload(member(repo))
    assert exc.value.name == "bundle-shape" and os.listdir(tmpdir_) == []


def test_the_senders_caps_are_the_servers(tmp_path, tmpdir_, monkeypatch):
    repo = op_repo(tmp_path)
    monkeypatch.setattr(WS, "BUNDLE_MAX_BYTES", 100)
    with pytest.raises(stager.StageRefused) as exc:
        stager.build_upload(member(repo))
    assert exc.value.name == "bundle-over-cap"
    monkeypatch.setattr(WS, "BUNDLE_MAX_BYTES", 10 ** 9)
    monkeypatch.setattr(WS, "MANIFEST_MAX_BYTES", 100)
    with pytest.raises(stager.StageRefused) as exc:
        stager.build_upload(member(repo))
    assert exc.value.name == "manifest-over-cap" and os.listdir(tmpdir_) == []


def test_w1_sends_no_withhold_and_only_a_workspace_member_is_built(tmp_path, tmpdir_):
    repo = op_repo(tmp_path)
    with pytest.raises(stager.StageRefused) as exc:
        stager.build_upload(dataclasses.replace(member(repo), withhold=("docs/**",)))
    assert exc.value.name == "withhold-not-in-w1"
    with pytest.raises(stager.StageRefused) as exc:
        stager.build_upload(object())
    assert exc.value.name == "workspace-member" and os.listdir(tmpdir_) == []


def test_the_seam_does_nothing_for_a_launch_with_no_workspace_and_names_a_sender_that_broke(tmp_path, tmpdir_, monkeypatch):
    launcher = C.DispatchSessionLauncher(lambda: None, lambda occ: None)
    conn = RecordingConn()
    launcher.stage_workspace(conn, launch_of(None))
    assert conn.requests == []
    monkeypatch.setattr(stager, "build_upload", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(C.WorkspaceNotSent) as exc:
        launcher.stage_workspace(conn, launch_of(member(op_repo(tmp_path))))
    assert exc.value.name == S.WORKSPACE_REFUSED and exc.value.detail == "stage-failed: RuntimeError"
    assert conn.requests == []


def test_a_wire_lost_mid_upload_is_a_transient_not_sent_refusal_and_closes_the_connection(tmp_path, tmpdir_):
    class Lossy(RecordingConn):
        def request(self, op, expect, **fields):
            if op == "WSDATA":
                raise C._Down("WSDATA: EOF")
            return super().request(op, expect, **fields)

    conn = Lossy()
    with pytest.raises(C.WorkspaceNotSent) as exc:
        C.DispatchSessionLauncher(lambda: None, lambda occ: None).stage_workspace(conn, launch_of(member(op_repo(tmp_path))))
    assert (exc.value.name, exc.value.reason_class) == ("dispatch-unreachable", "transient") and conn.closed


def test_the_refusal_name_the_fire_records_is_the_one_the_server_answers():
    assert S.WORKSPACE_REFUSED == WS.WORKSPACE_REFUSAL


# --- a full client -> real RunServer round trip -------------------------------------------------------------------------------

def test_the_client_builds_and_uploads_and_the_real_server_stages_exactly_those_bytes(w, tmp_path, tmpdir_):
    repo = op_repo(tmp_path)
    (repo / "src" / "a.py").write_text("a = 'DIRTY'\n")                       # dirty tree on the operator's side
    (repo / "untracked.txt").write_text("never\n")
    ws = member(repo)
    key = w.key()
    sock = w.env.connect()
    child = w.launch(sock, key, ws)
    expect = stager.build_upload(ws)
    assert child.session == "sess-1"
    staged = w.staged.read_bytes()
    SN_manifest = WS.manifest_from_bytes(expect.manifest)
    commit, _tree, objects = SN.snapshot_commit(SN_manifest)
    assert RS._prefix_problem(os.open(w.staged, os.O_RDONLY), commit, objects) is None
    assert commit == expect.commit and SN_manifest.head == ws.head
    got = clone_of(staged, tmp_path / "got")
    assert git(got, "show", "HEAD:src/a.py") == "a = 1\n" and not (got / "untracked.txt").exists()
    seen = w.runtime.starts[0].workspace
    assert seen.head == ws.head and seen.manifest_digest == SN_manifest.digest()
    rec = w.env.launcher.index.get(key)
    assert rec["state"] == "started" and rec["workspace"]["head"] == ws.head
    assert rec["workspace"]["manifest_digest"] == SN_manifest.digest()
    assert w.uploads._ups == {} and os.listdir(tmp_path / "scratch") == [] and os.listdir(tmpdir_) == []
    child._conn.close()
    w.env.drain()


def test_a_loosening_snapshot_never_reaches_the_real_server(w, tmp_path, tmpdir_):
    repo = op_repo(tmp_path, {".claude/settings.json": '{"disableAllHooks": true}', "src/a.py": "a\n"})
    key = w.key()
    sock = w.env.connect()
    with pytest.raises(C.WorkspaceNotSent) as exc:
        w.launch(sock, key, member(repo))
    assert exc.value.name == REFUSED and exc.value.detail.startswith("loosening")
    assert w.connects == [] and sock.fileno() != -1          # the refusal came before the client took a connection at all
    assert w.uploads._ups == {} and w.runtime.attempts == [] and not w.staged.exists()
    assert w.env.launcher.index.get(key) is None             # the server never heard of the key: no LAUNCH, no tombstone
    assert os.listdir(tmpdir_) == []
    sock.close()
    w.env.drain()


def test_a_server_that_refuses_the_upload_is_a_not_sent_refusal_too(tmp_path, tmpdir_):
    w2 = RealSender(tmp_path, bundle_max=300)                     # the server's bundle cap: refused at WSBEGIN
    repo = op_repo(tmp_path)
    sock = w2.env.connect()
    with pytest.raises(C.WorkspaceNotSent) as exc:
        w2.launch(sock, w2.key(), member(repo))
    assert exc.value.name == REFUSED and exc.value.detail.startswith("bundle-over-cap")
    assert exc.value.nothing_sent is True and w2.runtime.attempts == [] and os.listdir(tmpdir_) == []


def test_a_launch_with_no_workspace_builds_nothing_and_opens_no_temp(w, tmpdir_, monkeypatch):
    monkeypatch.setattr(stager, "build_upload", lambda *a, **k: pytest.fail("a launch with no workspace stages nothing"))
    key = w.key()
    sock = w.env.connect()
    child = w.launch(sock, key, None)
    assert child.session == "sess-1" and os.listdir(tmpdir_) == []
    child._conn.close()
    w.env.drain()


# --- the fire: a refusal before LAUNCH settles at zero -------------------------------------------------------------------------------

class NotSentStub(Stub):
    """A launcher whose sender refuses (or loses the wire) before any LAUNCH."""

    def __init__(self, exc, **kw):
        super().__init__(**kw)
        self.exc = exc

    def spawn(self, launch):
        self.launches.append(launch)
        raise self.exc

    def usage(self, session_key):                              # an unindexed key: what a real server answers for one it never saw
        from levain.agents.accounting import Usage
        return Usage(None, None, None, "session:unindexed")


MEMBER = S.Workspace("/Users/op/repo", "a" * 40, (), None, "main")


def test_a_refusal_before_launch_ends_the_run_as_session_refused_workspace_at_zero(rig):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    launcher = NotSentStub(C.WorkspaceNotSent(S.WORKSPACE_REFUSED, "loosening:disable-all-hooks"), auth="passthrough")
    _sup, report = _run(rig, oid, launcher, exposure=EXP, workspace=MEMBER)
    row = rig.row(oid)
    assert (row["state"], row["reason"]) == ("failed", "session_refused:workspace")
    assert launcher.launches[0].workspace == MEMBER
    state, why, actions, tunnel, seconds, usd = _reservation(rig, oid)
    assert (actions, tunnel, usd) == (0, 0, 0), "nothing was sent, so nothing is debited"


def test_a_transient_wire_loss_before_launch_is_a_spawn_failure_that_still_settles_at_zero(rig):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    launcher = NotSentStub(C.WorkspaceNotSent("dispatch-unreachable", "WSDATA: _Down", reason_class="transient"),
                           auth="passthrough")
    _run(rig, oid, launcher, exposure=EXP, workspace=MEMBER)
    assert rig.row(oid)["reason"] == "session_spawn_failed"
    assert _reservation(rig, oid)[2:4] == (0, 0)


def test_an_error_that_is_not_marked_nothing_sent_still_settles_at_the_caps(rig):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    launcher = NotSentStub(L.LauncherRefused(S.WORKSPACE_REFUSED, "from the server at LAUNCH"), auth="passthrough")
    _run(rig, oid, launcher, exposure=EXP, workspace=MEMBER)
    assert rig.row(oid)["reason"] == "session_refused:workspace"          # recorded under its name either way
    assert _reservation(rig, oid)[2] == EXP.actions                       # but a LAUNCH may have run: unreadable usage is the caps


def test_a_workspace_that_did_not_resolve_ends_the_run_refused_before_anything_starts(rig):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    launcher = Stub(auth="passthrough")
    _sup, report = _run(rig, oid, launcher, exposure=EXP, workspace_problem="workspace-branch: nope does not resolve")
    assert launcher.launches == [] and report.note.startswith("workspace-branch")
    assert (rig.row(oid)["state"], rig.row(oid)["reason"]) == ("failed", "session_refused:workspace")
    assert rig.row(oid)["session_key"] is None and _reservation(rig, oid)[2] == 0


def test_the_fires_record_names_the_repo_the_branch_and_the_commit_resolved(rig, monkeypatch):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    results: list = []
    from levain.agents import supervise
    real = supervise.Supervisor.effect

    def spy(self, *a, **k):
        eff = real(self, *a, **k)
        results.append(eff.outcome.result)
        return eff

    monkeypatch.setattr(supervise.Supervisor, "effect", spy)
    launcher = Stub(auth="passthrough")
    _run(rig, oid, launcher, exposure=EXP, workspace=MEMBER)
    assert results[0]["workspace"] == {"repo": MEMBER.repo, "branch": "main", "head": MEMBER.head}
    assert launcher.launches[0].workspace is MEMBER and rig.row(oid)["state"] == "done"


def test_a_launch_with_no_workspace_hashes_and_records_as_it_always_did(rig, monkeypatch):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    results: list = []
    from levain.agents import supervise
    real = supervise.Supervisor.effect

    def spy(self, *a, **k):
        eff = real(self, *a, **k)
        results.append(eff.outcome.result)
        return eff

    monkeypatch.setattr(supervise.Supervisor, "effect", spy)
    _run(rig, oid, Stub(auth="passthrough"), exposure=EXP)
    assert "workspace" not in results[0]


# --- the install's per-agent setting ----------------------------------------------------------------------------------------------------

AG = "ag-" + "0" * 32


def _doc(tmp_path, **ws):
    root = tmp_path / "cockpit"
    root.mkdir(exist_ok=True)
    return {"schema": 1, "cockpit_root": str(root), **ws}


def test_the_install_reads_a_per_agent_workspace_and_an_install_without_one_reads_as_before(tmp_path):
    d = tmp_path / "agent"
    d.mkdir()
    (d / W.INSTALL_FILE).write_text(json.dumps(_doc(tmp_path)))
    assert W.read_install(d).workspaces == {}
    (d / W.INSTALL_FILE).write_text(json.dumps(
        _doc(tmp_path, workspaces={AG: {"repo": "/Users/op/repo", "branch": "release"}, "ag-" + "1" * 32: {"repo": "/r"}})))
    got = W.read_install(d).workspaces
    assert got[AG] == W.WorkspaceSetting("/Users/op/repo", "release") and got["ag-" + "1" * 32] == W.WorkspaceSetting("/r", None)


@pytest.mark.parametrize("ws", [
    {"not-an-agent": {"repo": "/r"}}, {AG: {"repo": "relative"}}, {AG: {"repo": "/r", "extra": 1}}, {AG: {}},
    {AG: {"repo": "/r", "branch": "-x"}}, {AG: {"repo": "/r", "branch": "a..b"}}, {AG: "/r"}, [AG], {AG: {"repo": 3}},
    {AG: {"repo": "/r\0x"}}])
def test_a_malformed_workspace_setting_is_refused(tmp_path, ws):
    d = tmp_path / "agent"
    d.mkdir()
    (d / W.INSTALL_FILE).write_text(json.dumps(_doc(tmp_path, workspaces=ws)))
    with pytest.raises(W.InstallRefused):
        W.read_install(d)


def test_write_install_round_trips_the_workspace_setting(tmp_path):
    d = tmp_path / "agent"
    d.mkdir()
    root = tmp_path / "cockpit"
    root.mkdir()
    W.write_install(d, cockpit_root=root, workspaces={AG: {"repo": "/Users/op/repo", "branch": "main"}})
    assert W.read_install(d).workspaces == {AG: W.WorkspaceSetting("/Users/op/repo", "main")}
    with pytest.raises(W.InstallRefused):
        W.write_install(d, cockpit_root=root, workspaces={AG: {"repo": "relative"}})
    assert W.read_install(d).workspaces == {AG: W.WorkspaceSetting("/Users/op/repo", "main")}    # the working install stays


def test_the_factory_resolves_the_branch_at_the_fire_not_at_install(tmp_path):
    repo = op_repo(tmp_path)
    git(repo, "checkout", "-q", "-b", "feat")
    first = git(repo, "rev-parse", "HEAD").strip()
    install = W.SessionInstall(tmp_path, None, None, {}, {AG: W.WorkspaceSetting(str(repo), "feat")})
    assert W._workspace_kwargs(install, AG)["workspace"] == S.Workspace(str(repo), first, (), None, "feat")
    (repo / "src" / "a.py").write_text("a = 2\n")
    git(repo, "commit", "-qam", "c2")
    second = git(repo, "rev-parse", "HEAD").strip()
    assert second != first
    got = W._workspace_kwargs(install, AG)["workspace"]
    assert got.head == second and got.branch == "feat"                       # the tip NOW
    assert W._workspace_kwargs(install, "ag-" + "9" * 32) == {}              # an agent with no setting stages nothing


def test_a_setting_that_does_not_resolve_is_handed_over_as_a_problem_not_raised(tmp_path):
    repo = op_repo(tmp_path)
    for setting in (W.WorkspaceSetting(str(repo), "nope"), W.WorkspaceSetting(str(tmp_path / "gone"), None)):
        kw = W._workspace_kwargs(W.SessionInstall(tmp_path, None, None, {}, {AG: setting}), AG)
        assert set(kw) == {"workspace_problem", "workspace_setting"} and kw["workspace_problem"].startswith("workspace-")
        assert kw["workspace_setting"] == (setting.repo, setting.branch)      # what a rerun hashes to replay its first attempt


def test_the_factory_hands_run_session_the_workspace_for_a_configured_agent(rig, tmp_path, monkeypatch):
    from tests.test_agents_session_wire import _install, _session_occurrence
    repo = op_repo(tmp_path)
    oid = _session_occurrence(rig)
    agent = rig.row(oid)["agent_id"]
    _install(rig, workspaces={agent: {"repo": str(repo), "branch": "main"}})
    kw = W.fire_factory(rig.store, oid, rig.row(oid)["launch_epoch"])
    assert kw["workspace"] == S.Workspace(str(repo), git(repo, "rev-parse", "HEAD").strip(), (), None, "main")
    import inspect
    assert set(kw) - {"sup"} <= set(inspect.signature(S.run_session).parameters)
    _install(rig)
    assert "workspace" not in W.fire_factory(rig.store, oid, rig.row(oid)["launch_epoch"])


# --- a crashed fire's rerun: the SETTING is the launch as asked, the resolved commit is not ----------------------------------------------

def _crash_first_finish(sup):
    real, calls = sup.finish, {"n": 0}

    def finish(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt
        return real(*a, **k)

    sup.finish = finish
    return sup


def _crashed_then_rerun(rig, rerun_ws):
    from tests.test_agents_session_launch import _sup
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    launcher = Stub(auth="passthrough")
    sup = _crash_first_finish(_sup(rig, oid))
    with pytest.raises(KeyboardInterrupt):
        _run(rig, oid, launcher, exposure=EXP, sup=sup, workspace=MEMBER)
    _, again = _run(rig, oid, launcher, exposure=EXP, sup=sup, workspace=rerun_ws)
    return launcher, again


def test_a_rerun_after_the_branch_moved_is_a_replay(rig):
    launcher, again = _crashed_then_rerun(rig, dataclasses.replace(MEMBER, head="b" * 40))
    assert len(launcher.launches) == 1 and again.state == "done"              # nothing spawned twice


@pytest.mark.parametrize("over", [{"branch": "release"}, {"repo": "/Users/op/other"}])
def test_a_rerun_asking_for_another_branch_or_repo_is_not_a_replay(rig, over):
    launcher, again = _crashed_then_rerun(rig, dataclasses.replace(MEMBER, **over))
    assert len(launcher.launches) == 1 and again.state == "launched"          # asking for other work is not a replay



# --- a rerun whose setting no longer resolves (L3 w1cli r1: gemini HIGH, complement MED) ---------------------------------------

def _crashed_then_rerun_unresolved(rig, setting=(MEMBER.repo, MEMBER.branch)):
    """The first attempt resolved its workspace, launched, and crashed before it recorded the end; at the rerun the
    setting no longer resolves (the branch since deleted, the repo moved), so the fire hands over a problem."""
    from tests.test_agents_session_launch import _sup
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    launcher = Stub(auth="passthrough")
    sup = _crash_first_finish(_sup(rig, oid))
    with pytest.raises(KeyboardInterrupt):
        _run(rig, oid, launcher, exposure=EXP, sup=sup, workspace=MEMBER)
    extra = {} if setting is None else {"workspace_setting": setting}
    _, again = _run(rig, oid, launcher, exposure=EXP, sup=sup, workspace_problem="workspace-branch: gone", **extra)
    return oid, launcher, again


def test_a_rerun_whose_setting_no_longer_resolves_still_replays_the_first_attempts_effect(rig):
    oid, launcher, again = _crashed_then_rerun_unresolved(rig)
    assert len(launcher.launches) == 1                                         # nothing spawned twice
    assert (again.state, rig.row(oid)["state"], rig.row(oid)["reason"]) == ("done", "done", "completed")   # not session_refused:workspace


def test_a_rerun_with_a_problem_and_other_work_spawns_nothing(rig):
    oid, launcher, again = _crashed_then_rerun_unresolved(rig, setting=(MEMBER.repo, "release"))
    assert len(launcher.launches) == 1 and again.state == "launched"          # not a replay of the first: left to lapse recovery


def test_a_rerun_with_a_problem_and_no_setting_is_still_a_refusal(rig):
    oid, launcher, again = _crashed_then_rerun_unresolved(rig, setting=None)
    assert len(launcher.launches) == 1 and rig.row(oid)["reason"] == "session_refused:workspace"   # nothing to hash: the old rule


def test_a_first_attempt_with_a_problem_and_a_setting_is_refused_and_launches_nothing(rig):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    launcher = Stub(auth="passthrough")
    _run(rig, oid, launcher, exposure=EXP, workspace_problem="workspace-branch: gone",
         workspace_setting=(MEMBER.repo, MEMBER.branch))
    assert launcher.launches == [] and rig.row(oid)["reason"] == "session_refused:workspace"
    assert rig.row(oid)["session_key"] is None                                 # no earlier attempt: the refusal stands


# --- the snapshot is built before the connection is opened (L3 w1cli r1: codex HIGH) -----------------------------------------------

def _ordered_client(w, events, sock):
    r = w.rig

    def connect():
        events.append("connect")
        return sock

    def proof(occ):
        events.append("proof")
        return C.link_proof(r.store, r.world.root, occ, now=r.now())

    client = C.DispatchSessionLauncher(connect, proof)
    real = client.build_workspace

    def build(launch):
        events.append("build")
        return real(launch)

    client.build_workspace = build
    return client


def test_the_snapshot_is_built_before_the_proof_is_read_and_before_the_connection_is_opened(w, tmp_path):
    repo = op_repo(tmp_path)
    events: list = []
    sock = w.env.connect()
    client = _ordered_client(w, events, sock)
    child = client.spawn(S.SessionLaunch(w.oid, w.key(), "claude", "sonnet", "b", "passthrough", EXP, member(repo)))
    assert events == ["build", "proof", "connect"] and child.session == "sess-1"
    child._conn.close()
    w.env.drain()


def test_a_build_refusal_happens_with_no_proof_read_and_no_connection_opened(w, tmp_path):
    repo = op_repo(tmp_path, {".claude/settings.json": '{"disableAllHooks": true}', "src/a.py": "a\n"})
    events: list = []
    sock = w.env.connect()
    client = _ordered_client(w, events, sock)
    with pytest.raises(C.WorkspaceNotSent) as exc:
        client.spawn(S.SessionLaunch(w.oid, w.key(), "claude", "sonnet", "b", "passthrough", EXP, member(repo)))
    assert exc.value.name == S.WORKSPACE_REFUSED and events == ["build"]
    sock.close()
    w.env.drain()


def test_a_launch_with_no_workspace_builds_nothing_before_it_connects(w):
    events: list = []
    sock = w.env.connect()
    client = _ordered_client(w, events, sock)
    child = client.spawn(S.SessionLaunch(w.oid, w.key(), "claude", "sonnet", "b", "passthrough", EXP, None))
    assert events == ["build", "proof", "connect"]                            # the build step runs and has nothing to do
    child._conn.close()
    w.env.drain()


# --- a host fault is not a standing refusal (L3 w1cli r1: codex MED) -----------------------------------------------------------------

def _git_unavailable(monkeypatch):
    def down(*a, **k):
        raise SN.SnapshotRefused("snapshot-git-unavailable", "git could not run (TimeoutExpired)")

    monkeypatch.setattr(SN, "_git", down)


def test_resolve_head_keeps_a_git_that_could_not_run_transient(tmp_path, monkeypatch):
    repo = op_repo(tmp_path)
    _git_unavailable(monkeypatch)
    with pytest.raises(stager.StageRefused) as exc:
        stager.resolve_head(str(repo), "main")
    assert exc.value.name == "snapshot-git-unavailable" and exc.value.transient is True


def test_resolve_head_still_names_a_branch_git_cannot_resolve(tmp_path):
    repo = op_repo(tmp_path)
    with pytest.raises(stager.StageRefused) as exc:
        stager.resolve_head(str(repo), "nope")
    assert exc.value.name == "workspace-branch" and exc.value.transient is False


def test_the_factory_raises_for_a_host_fault_and_hands_over_a_problem_for_a_bad_setting(tmp_path, monkeypatch):
    repo = op_repo(tmp_path)
    install = W.SessionInstall(tmp_path, None, None, {}, {AG: W.WorkspaceSetting(str(repo), "main")})
    _git_unavailable(monkeypatch)
    with pytest.raises(stager.StageRefused) as exc:                          # fire_factory raises: make_run leaves the row launched
        W._workspace_kwargs(install, AG)
    assert exc.value.transient is True


def test_a_build_that_met_a_host_fault_is_a_transient_not_a_workspace_refusal(tmp_path, monkeypatch):
    def down(ws, **k):
        raise stager.StageRefused("snapshot-git-unavailable", "git could not run (TimeoutExpired)")

    monkeypatch.setattr(stager, "build_upload", down)
    with pytest.raises(C.WorkspaceNotSent) as exc:
        C.DispatchSessionLauncher(lambda: None, lambda occ: None).build_workspace(launch_of(member(op_repo(tmp_path))))
    assert (exc.value.name, exc.value.reason_class, exc.value.nothing_sent) == ("snapshot-git-unavailable", "transient", True)


# --- the bundle's size is judged before it is read (L3 w1cli r1: complement LOW) ---------------------------------------------------

def test_a_bundle_over_the_cap_is_refused_on_its_size_without_being_read(tmp_path, tmpdir_, monkeypatch):
    repo = op_repo(tmp_path)
    monkeypatch.setattr(WS, "BUNDLE_MAX_BYTES", 100)
    opened: list = []
    real_open = open

    def spy(path, *a, **k):
        opened.append(str(path))
        return real_open(path, *a, **k)

    monkeypatch.setattr(stager, "open", spy, raising=False)
    with pytest.raises(stager.StageRefused) as exc:
        stager.build_upload(member(repo))
    assert exc.value.name == "bundle-over-cap" and opened == [] and os.listdir(tmpdir_) == []


def test_an_effect_body_run_with_a_problem_starts_nothing(rig):
    """A key on the row with no journaled effect behind it (not a state the fire writes, but one the body must not
    trust): the refusal is let past for a rerun, and the body that then runs has no resolved workspace to launch."""
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    with rig.store.write() as conn:
        conn.execute("UPDATE agent_occurrences SET session_key = ? WHERE occurrence_id = ?", ("s-00000000000000c1", oid))
    launcher = Stub(auth="passthrough")
    _run(rig, oid, launcher, exposure=EXP, workspace_problem="workspace-branch: gone",
         workspace_setting=(MEMBER.repo, MEMBER.branch))
    assert launcher.launches == [] and rig.row(oid)["reason"] == "session_refused:workspace"
