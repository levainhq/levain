"""levain 34, W1 (server half): the workspace stager on the dispatch run server (``levain.slot.workspace``, the WSBEGIN /
WSDATA ops and the LAUNCH ``workspace`` member of ``levain.slot.services.dispatch_run`` / ``session_client``).

The server is a REAL ``RunServer`` answering a REAL ``DispatchSessionLauncher`` over socketpairs, with the REAL
``RealLauncher`` and the real point-of-use check; the bundles are hand-made from real ``git`` repos through the real
snapshot builder (``build_snapshot`` + the read set's own ``_bundle_into``). The runtime under the launcher is a fake
that calls the REAL stager at the place ``HelperSlotRuntime.start`` does (after the brief, before ``render``), with a
temp staging dir; one test runs the real ``HelperSlotRuntime`` over its scripted helper.

Held: the round trip (bytes staged exactly, 0600, the run record carrying head + manifest digest + withhold digest); and
each refusal below answers ``session_refused:workspace`` with its cause, stages nothing, and (before the mint) records
the zero-usage ``refused`` tombstone: a tampered bundle (in transit; a damaged pack from an honest sender; a swapped
header; a changed object count), an over-cap bundle and an overrunning chunk, a symlink at ``repo.bundle``, a manifest
that does not match the bundle (another snapshot's, not canonical, not what WSBEGIN declared), a head or withhold the
member does not share with the manifest, a snapshot whose ``.claude/settings.json`` loosens the sandbox, one carrying
``.mcp.json``, an upload with no member. A member with no upload on its connection is refused and writes NO tombstone
(the key may be another connection's in-flight upload), nor does a transient host failure (L3 w1srv r1).

The L3 r1 fix round (two real connections where a race needs two): one upload per connection, replaced on re-begin and
dropped by every LAUNCH outcome, so a transient refusal is retryable on the same connection; a foreign LAUNCH neither
drops nor poisons another connection's upload; the key stays reserved while the stager writes; a host failure in the
scratch is transient and leaves the key launchable; the verify unlinks only the input file it created.

What no test here holds (UNVERIFIED until the VM rows): the unit as ``_levain_dispatch`` with real peer credentials, a
real ``staging/<session>`` made by root's ``prepare``, the root-side ``stage_bundle`` reading the file, a repo of
realistic size (the bounds are guesses), the operator-uid side that builds and sends the frames (a later lane).
"""
from __future__ import annotations

import copy
import dataclasses
import os
import shutil
import socket
import stat
import threading
from pathlib import Path

import pytest

from levain.agents import session as S
from levain.agents import session_client as C
from levain.agents import session_launcher as L
from levain.dispatch import readset as RS
from levain.dispatch import snapshot as SN
from levain.slot import workspace as WS
from levain.slot.services import dispatch_run as D
from tests.test_agents_launch import Rig
from levain.agents.role import add_role
from tests.test_agents_session_launch import EXP, ROLE, _occurrence
from tests.test_agents_session_launcher import FakeRuntime
from tests.test_dispatch_snapshot import git, make_repo
from tests.test_slot_dispatch_run_server import Env

pytestmark = [pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen"),
              pytest.mark.skipif(shutil.which("git") is None, reason="needs git")]

FILES = {"src/a.py": "a = 1\n", "README.md": "# hello\n", "docs/x.txt": "x\n"}
REFUSED = WS.WORKSPACE_REFUSAL


class Parts:
    """One hand-made upload: the snapshot's manifest, its bundle, and the repo's HEAD."""

    def __init__(self, manifest: SN.Manifest, bundle: bytes, head: str) -> None:
        self.manifest, self.bundle, self.head = manifest, bundle, head
        self.manifest_bytes = manifest.canonical()

    def workspace(self, **kw) -> S.Workspace:
        return S.Workspace(**{"repo": "/Users/op/repo", "head": self.head, "withhold": (), **kw})


_n = [0]


def make_parts(tmp_path: Path, files=None, withhold=()) -> Parts:
    _n[0] += 1
    top = Path(os.path.realpath(tmp_path)) / f"w{_n[0]}"
    top.mkdir()
    repo = make_repo(top, FILES if files is None else files)
    rs, man = SN.build_snapshot(str(repo), str(top / "snap"), list(withhold))
    out = top / "h.bundle"
    fd = os.open(out, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        RS._bundle_into(rs.repo_dir, fd, RS._scratch_env())
    finally:
        os.close(fd)
    return Parts(man, out.read_bytes(), git(repo, "rev-parse", "HEAD").strip())


class StagingRuntime(FakeRuntime):
    """The fake slot runtime that runs the REAL stager where ``HelperSlotRuntime.start`` does."""

    def __init__(self, stager, root: Path) -> None:
        super().__init__()
        self.stager, self.root, self.before, self.attempts = stager, root, None, []

    def start(self, req):
        self.attempts.append(req)
        d = self.root / "sess-1"
        d.mkdir(parents=True, exist_ok=True)
        if self.before is not None:
            self.before(d)
        self.stager(req, d)
        return super().start(req)


class HandUploaded(C.DispatchSessionLauncher):
    """The client for tests that upload the parts BY HAND (to tamper with them): its ``stage_workspace`` seam sends
    nothing, so the server meets exactly the bytes the test put there. The real sender is held in
    ``tests/test_dispatch_stager_send.py``."""

    def build_workspace(self, launch):
        return None

    def stage_workspace(self, conn, launch, upload=None):
        return None


def role_withholding(*patterns: str) -> dict:
    """The fixture role with ``ceiling.withhold`` (a copy: the shared ROLE is never touched)."""
    role = copy.deepcopy(ROLE)
    role["ceiling"]["withhold"] = list(patterns)
    return role


class World:
    def __init__(self, tmp_path: Path, *, role=None, **uploads_kw) -> None:
        self.rig = Rig(tmp_path)
        add_role(self.rig.store, ROLE if role is None else role)       # W2: the server re-derives the withhold from it
        self.uploads = WS.WorkspaceUploads(tmp_path / "scratch", **uploads_kw)
        self.runtime = StagingRuntime(self.uploads.stage, tmp_path / "staging")
        self.env = Env(self.rig, tmp_path, runtime=self.runtime, uploads=self.uploads)
        self.staged = tmp_path / "staging" / "sess-1" / "repo.bundle"
        self.oid = _occurrence(self.rig, billing="subscription", exposure=EXP, role=role)
        self._ids = 0

    def key(self) -> str:
        self._ids += 1
        return f"s-{self._ids:016x}"

    def send(self, sock, op, **fields):
        self._ids += 1
        sock.sendall(C.encode_run_frame({"p": C.PROTOCOL, "op": op, "id": self._ids, **fields}))
        return C.read_run_frame(sock)

    def upload(self, key, parts: Parts, *, manifest=None, bundle=None, begin=None, stop_on_refusal=True):
        """A connection with the two parts uploaded (``manifest`` / ``bundle`` replace the bytes SENT, while WSBEGIN
        declares the originals unless ``begin`` is given). Returns ``(sock, answers)``."""
        sock = self.env.connect()
        m, b = manifest if manifest is not None else parts.manifest_bytes, bundle if bundle is not None else parts.bundle
        answers = []
        answers.append(self.send(sock, "WSBEGIN", **(begin or C.ws_begin_to_wire(key, parts.manifest_bytes,
                                                                                    parts.bundle))))
        for part, data in (("manifest", m), ("bundle", b)):
            for f in C.ws_chunk_frames(key, part, data):
                if stop_on_refusal and not answers[-1]["ok"]:
                    return sock, answers
                answers.append(self.send(sock, "WSDATA", **f))
        return sock, answers

    def launch(self, sock, key, ws):
        r = self.rig
        client = HandUploaded(lambda: sock, lambda occ: C.link_proof(r.store, r.world.root, occ, now=r.now()),
                              signed_of=lambda occ: C.signed_role_of(r.store, occ))
        return client.spawn(S.SessionLaunch(self.oid, key, "claude", "sonnet", "b", "passthrough", EXP, ws))

    def raw_launch(self, sock, key, ws):
        """A LAUNCH frame sent by hand, so a refusal leaves the connection open for a same-connection retry (the client
        closes it)."""
        r = self.rig
        proof = dataclasses.replace(C.link_proof(r.store, r.world.root, self.oid, now=r.now()),
                                    signed=C.signed_role_of(r.store, self.oid))
        return self.send(sock, "LAUNCH", **C.launch_to_wire(
            S.SessionLaunch(self.oid, key, "claude", "sonnet", "b", "passthrough", EXP, ws), proof))

    def chunks(self, sock, key, parts: Parts):
        return [self.send(sock, "WSDATA", **f) for part, data in (("manifest", parts.manifest_bytes),
                                                                  ("bundle", parts.bundle))
                for f in C.ws_chunk_frames(key, part, data)]

    def refused(self, key, parts: Parts, ws=None, **upload_kw) -> L.LauncherRefused:
        sock, answers = self.upload(key, parts, **upload_kw)
        assert all(a["ok"] for a in answers), answers
        with pytest.raises(L.LauncherRefused) as exc:
            self.launch(sock, key, parts.workspace() if ws is None else ws)
        self.assert_nothing_staged(key)
        return exc.value

    def assert_nothing_staged(self, key, *, tombstone=True):
        assert not self.staged.exists() and not self.staged.is_symlink()
        assert self.uploads._ups == {} and self.runtime.starts == [] and self.env.control.minted == {}
        rec = self.env.launcher.index.get(key)
        if not tombstone:
            assert rec is None, rec
            return
        assert rec is not None and rec["state"] == "refused" and rec["reason"] == REFUSED
        assert self.env.launcher.usage(key).actions == 0      # a refusal before the mint spends nothing


@pytest.fixture
def w(tmp_path) -> World:
    return World(tmp_path)


def cause(exc: L.LauncherRefused) -> str:
    assert exc.name == REFUSED and exc.reason_class == "standing", (exc.name, exc.detail)
    return exc.detail.split(": ", 1)[0]


# --- the round trip ---------------------------------------------------------------------------------------------------

def test_a_snapshot_round_trips_through_a_real_run_server_into_staging(w, tmp_path):
    parts, key = make_parts(tmp_path), w.key()
    sock, answers = w.upload(key, parts)
    assert [a["ok"] for a in answers] == [True] * len(answers) and answers[-1]["received"] == len(parts.bundle)
    child = w.launch(sock, key, parts.workspace())
    assert child.session == "sess-1"
    assert w.staged.read_bytes() == parts.bundle
    assert stat.S_IMODE(os.stat(w.staged).st_mode) == 0o600 and os.stat(w.staged).st_uid == os.getuid()
    commit, _tree, objects = SN.snapshot_commit(parts.manifest)
    assert RS._prefix_problem(os.open(w.staged, os.O_RDONLY), commit, objects) is None
    seen = w.runtime.starts[0].workspace             # the member the slot runtime is handed carries the SERVER's digest
    assert seen.head == parts.head and seen.manifest_digest == parts.manifest.digest()
    rec = w.env.launcher.index.get(key)               # the run record: head sha + manifest digest + withhold digest
    assert rec["state"] == "started" and rec["workspace"] == {
        "head": parts.head, "manifest_digest": parts.manifest.digest(), "withhold_digest": SN.withhold_digest(())}
    assert w.uploads._ups == {}                        # consumed at the stage, nothing held after the LAUNCH
    assert os.listdir(tmp_path / "scratch") == []      # the verify's clone and input file are gone
    child._conn.close()
    w.env.drain()


def test_a_launch_with_no_workspace_is_exactly_what_it_was(w):
    key = w.key()
    sock = w.env.connect()
    child = w.launch(sock, key, None)
    assert child.session == "sess-1" and w.runtime.starts[0].workspace is None and not w.staged.exists()
    assert "workspace" not in w.env.launcher.index.get(key)
    frame = C.launch_to_wire(S.SessionLaunch("o", key, "claude", "sonnet", "b", "passthrough", EXP), _fake_proof())
    assert "workspace" not in frame["launch"]
    child._conn.close()
    w.env.drain()


def _fake_proof():
    row = C.LinkRow(1, "", "r", "fp", "sig")
    return C.LinkProof("o", "h", "l", "s", row, row)


# --- tampered bundles --------------------------------------------------------------------------------------------------

def test_a_bundle_altered_in_transit_is_not_the_bytes_wsbegin_declared(w, tmp_path):
    parts, key = make_parts(tmp_path), w.key()
    bad = bytearray(parts.bundle)
    bad[-30] ^= 0xFF
    assert cause(w.refused(key, parts, bundle=bytes(bad))) == "bundle-digest"


def test_a_damaged_pack_from_an_honest_sender_fails_the_ingest(w, tmp_path):
    parts, key = make_parts(tmp_path), w.key()
    bad = bytearray(parts.bundle)
    bad[len(bad) // 2 + 20] ^= 0xFF                               # inside the pack body, its sha1 trailer now disagrees
    honest = Parts(parts.manifest, bytes(bad), parts.head)
    assert cause(w.refused(key, honest)) == "readset-git-failed"
    assert os.listdir(tmp_path / "scratch") == []


def test_a_swapped_bundle_header_is_not_the_snapshots_commit(w, tmp_path):
    parts, key = make_parts(tmp_path), w.key()
    commit = SN.snapshot_commit(parts.manifest)[0].encode()
    assert cause(w.refused(key, Parts(parts.manifest, parts.bundle.replace(commit, b"0" * 40, 1), parts.head))) \
        == "bundle-header"


def test_a_pack_with_another_object_count_than_the_manifest_derives_is_refused(w, tmp_path):
    parts, key = make_parts(tmp_path), w.key()
    header = RS._header(SN.snapshot_commit(parts.manifest)[0])
    at = len(header) + 8                                            # PACK, version, then the big-endian count
    bad = parts.bundle[:at] + (int.from_bytes(parts.bundle[at:at + 4], "big") + 1).to_bytes(4, "big") \
        + parts.bundle[at + 4:]
    assert cause(w.refused(key, Parts(parts.manifest, bad, parts.head))) == "object-count"


# --- caps ----------------------------------------------------------------------------------------------------------------

def test_an_over_cap_bundle_is_refused_at_wsbegin(tmp_path):
    w = World(tmp_path, bundle_max=300)
    parts, key = make_parts(tmp_path), w.key()
    assert len(parts.bundle) > 300
    sock, answers = w.upload(key, parts)
    assert len(answers) == 1 and answers[0]["ok"] is False and answers[0]["refused"] == REFUSED
    assert answers[0]["detail"].startswith("bundle-over-cap") and w.uploads._ups == {}
    assert w.runtime.attempts == [] and not w.staged.exists()


def test_a_chunk_past_the_declared_size_ends_the_upload(w, tmp_path):
    parts, key = make_parts(tmp_path), w.key()
    begin = dict(C.ws_begin_to_wire(key, parts.manifest_bytes, parts.bundle), bundle_size=100)
    sock, answers = w.upload(key, parts, begin=begin)
    assert answers[0]["ok"] is True and answers[-1]["refused"] == REFUSED
    assert answers[-1]["detail"].startswith("chunk-over-size") and w.uploads._ups == {}
    after = w.send(sock, "WSDATA", **next(C.ws_chunk_frames(key, "manifest", parts.manifest_bytes)))
    assert after["detail"].startswith("no-upload")


def test_the_default_caps_sit_inside_the_slots_own_cap_and_the_snapshots_bound():
    from levain.slot import stage
    assert SN.SNAPSHOT_MAX_BYTES < WS.BUNDLE_MAX_BYTES <= stage.BUNDLE_CAP and WS.BUNDLE_MAX_BYTES < 2 * SN.SNAPSHOT_MAX_BYTES


def test_a_chunk_out_of_order_ends_the_upload(w, tmp_path):
    parts, key = make_parts(tmp_path), w.key()
    sock = w.env.connect()
    assert w.send(sock, "WSBEGIN", **C.ws_begin_to_wire(key, parts.manifest_bytes, parts.bundle))["ok"]
    f = dict(next(C.ws_chunk_frames(key, "manifest", parts.manifest_bytes)), offset=5)
    ans = w.send(sock, "WSDATA", **f)
    assert ans["refused"] == REFUSED and ans["detail"].startswith("chunk-out-of-order") and w.uploads._ups == {}


def test_uploads_are_capped_and_a_full_table_is_transient(tmp_path):
    w = World(tmp_path, max_uploads=1)
    parts = make_parts(tmp_path)
    a, b = w.env.connect(), w.env.connect()
    assert w.send(a, "WSBEGIN", **C.ws_begin_to_wire("s-00000000000000a1", parts.manifest_bytes, parts.bundle))["ok"]
    ans = w.send(b, "WSBEGIN", **C.ws_begin_to_wire("s-00000000000000a2", parts.manifest_bytes, parts.bundle))
    assert (ans["refused"], ans.get("reason_class")) == ("no-workspace-capacity", "transient")
    same = w.send(b, "WSBEGIN", **C.ws_begin_to_wire("s-00000000000000a1", parts.manifest_bytes, parts.bundle))
    assert same["refused"] == REFUSED and same["detail"].startswith("upload-exists")    # another connection's key
    assert list(w.uploads._ups) == ["s-00000000000000a1"] and w.uploads._ups["s-00000000000000a1"].owner.keys \
        == {"s-00000000000000a1"}


def test_one_upload_per_connection_a_rebegin_replaces_it(tmp_path):
    w = World(tmp_path, max_uploads=1)                 # the replace frees its own slot: a cap of one still re-begins
    parts = make_parts(tmp_path)
    sock = w.env.connect()
    k1, k2 = "s-00000000000000a3", "s-00000000000000a4"
    assert w.send(sock, "WSBEGIN", **C.ws_begin_to_wire(k1, parts.manifest_bytes, parts.bundle))["ok"]
    assert w.send(sock, "WSBEGIN", **C.ws_begin_to_wire(k1, parts.manifest_bytes, parts.bundle))["ok"]   # same key
    assert w.send(sock, "WSBEGIN", **C.ws_begin_to_wire(k2, parts.manifest_bytes, parts.bundle))["ok"]   # another
    assert list(w.uploads._ups) == [k2] and w.uploads._ups[k2].owner.keys == {k2}
    assert all(a["ok"] for a in w.chunks(sock, k2, parts))
    assert w.launch(sock, k2, parts.workspace()).session == "sess-1" and w.staged.read_bytes() == parts.bundle


def test_a_connection_that_ends_drops_the_upload_nobody_launched(w, tmp_path):
    parts, key = make_parts(tmp_path), w.key()
    sock, _ = w.upload(key, parts)
    assert key in w.uploads._ups
    sock.close()
    w.env.drain()
    assert w.uploads._ups == {}


# --- the symlink at repo.bundle -----------------------------------------------------------------------------------------

def test_a_symlink_at_repo_bundle_refuses_at_the_stage_and_writes_through_nothing(w, tmp_path):
    parts, key = make_parts(tmp_path), w.key()
    victim = tmp_path / "victim"
    w.runtime.before = lambda d: (d / "repo.bundle").symlink_to(victim)
    sock, answers = w.upload(key, parts)
    with pytest.raises(L.LauncherRefused) as exc:
        w.launch(sock, key, parts.workspace())
    assert cause(exc.value) == "staging-dest"
    assert not victim.exists() and os.listdir(w.staged.parent) == ["repo.bundle"] and w.staged.is_symlink()
    assert w.runtime.starts == [] and w.uploads._ups == {}
    assert w.env.launcher.index.get(key)["state"] == "undone"       # past the mint: undone, as any failed start


def test_a_file_already_at_repo_bundle_is_left_as_it_was(w, tmp_path):
    parts, key = make_parts(tmp_path), w.key()
    w.runtime.before = lambda d: (d / "repo.bundle").write_bytes(b"theirs")
    sock, _ = w.upload(key, parts)
    with pytest.raises(L.LauncherRefused) as exc:
        w.launch(sock, key, parts.workspace())
    assert cause(exc.value) == "staging-dest" and w.staged.read_bytes() == b"theirs"


def test_a_stage_that_fails_after_creating_the_file_removes_it(w, tmp_path, monkeypatch):
    parts, key = make_parts(tmp_path), w.key()
    monkeypatch.setattr(RS, "full_fsync", lambda fd: (_ for _ in ()).throw(OSError(5, "EIO")))
    sock, _ = w.upload(key, parts)
    with pytest.raises(L.LauncherRefused) as exc:
        w.launch(sock, key, parts.workspace())
    assert exc.value.name == "internal-error" and not w.staged.exists() and w.runtime.starts == []


# --- a manifest that is not the bundle's ----------------------------------------------------------------------------------

def test_another_snapshots_manifest_does_not_derive_the_bundles_commit(w, tmp_path):
    parts, key = make_parts(tmp_path), w.key()
    extra = tuple(sorted(parts.manifest.entries + (("zz-extra.txt", "100644", parts.manifest.entries[0][2]),)))
    other = Parts(dataclasses.replace(parts.manifest, entries=extra), parts.bundle, parts.head)
    assert cause(w.refused(key, other)) == "bundle-header"


def test_a_manifest_that_is_not_its_canonical_bytes_is_malformed(w, tmp_path):
    parts, key = make_parts(tmp_path), w.key()
    spaced = Parts(parts.manifest, parts.bundle, parts.head)
    spaced.manifest_bytes = parts.manifest_bytes + b" "
    assert cause(w.refused(key, spaced)) == "manifest-malformed"


def test_a_manifest_that_is_not_the_bytes_wsbegin_declared_is_refused(w, tmp_path):
    parts, key = make_parts(tmp_path), w.key()
    other = bytearray(parts.manifest_bytes)
    other[10] = ord("x") if other[10] != ord("x") else ord("y")
    assert cause(w.refused(key, parts, manifest=bytes(other))) == "manifest-digest"


def test_a_head_the_member_does_not_share_with_the_manifest_is_refused(w, tmp_path):
    parts, key = make_parts(tmp_path), w.key()
    assert cause(w.refused(key, parts, ws=parts.workspace(head="1" * 40))) == "head-mismatch"


def test_a_manifest_whose_path_is_both_a_file_and_a_directory_is_refused_by_the_path_checks(w, tmp_path):
    parts, key = make_parts(tmp_path), w.key()
    blob = parts.manifest.entries[0][2]
    clash = tuple(sorted(parts.manifest.entries + (("src", "100644", blob),)))     # src is a file AND a directory
    bad = Parts(dataclasses.replace(parts.manifest, entries=clash), parts.bundle, parts.head)
    assert cause(w.refused(key, bad)) == "manifest-path"


# --- withhold, .mcp.json, the loosening check ----------------------------------------------------------------------------------

def test_a_withhold_the_signed_role_declares_stages_and_the_run_records_its_digest(tmp_path):
    w = World(tmp_path, role=role_withholding("docs/**"))
    parts, key = make_parts(tmp_path, withhold=("docs/**",)), w.key()
    assert parts.manifest.withheld_paths == ("docs/x.txt",)
    sock, _ = w.upload(key, parts)
    assert w.launch(sock, key, parts.workspace(withhold=("docs/**",))).session == "sess-1"
    assert w.env.launcher.index.get(key)["workspace"]["withhold_digest"] == SN.withhold_digest(("docs/**",))


def test_a_snapshot_that_withholds_less_than_the_signed_role_is_a_loosening_and_is_refused(tmp_path):
    w = World(tmp_path, role=role_withholding("docs/**"))
    parts, key = make_parts(tmp_path), w.key()                    # cut with NO withhold: docs/x.txt is in it
    assert cause(w.refused(key, parts)) == "withhold-loosened"


def test_a_member_and_manifest_that_withhold_more_than_the_signed_role_are_refused(w, tmp_path):
    parts, key = make_parts(tmp_path, withhold=("docs/**",)), w.key()      # the fixture role withholds nothing
    assert cause(w.refused(key, parts, ws=parts.workspace(withhold=("docs/**",)))) == "withhold-mismatch"


def test_a_manifest_that_names_the_signed_withhold_but_still_carries_a_withheld_path_is_refused(tmp_path):
    w = World(tmp_path, role=role_withholding("docs/**"))
    full = make_parts(tmp_path)                                    # the bytes of an unwithheld snapshot
    lying = Parts(dataclasses.replace(full.manifest, withhold=("docs/**",)), full.bundle, full.head)
    key = w.key()
    assert cause(w.refused(key, lying, ws=lying.workspace(withhold=("docs/**",)))) == "withheld-path-carried"


def test_a_withhold_the_member_and_the_manifest_disagree_on_is_refused(tmp_path):
    w = World(tmp_path, role=role_withholding("docs/**"))
    parts, key = make_parts(tmp_path, withhold=("docs/**",)), w.key()
    assert cause(w.refused(key, parts, ws=parts.workspace(withhold=("other/**",)))) in ("withhold-loosened",
                                                                                    "withhold-mismatch")


def test_a_workspace_launch_whose_proof_carries_no_role_evidence_is_refused(w, tmp_path):
    parts, key = make_parts(tmp_path), w.key()
    sock, answers = w.upload(key, parts)
    assert all(a["ok"] for a in answers)
    r = w.rig
    bare = C.link_proof(r.store, r.world.root, w.oid, now=r.now())          # signed=None: no evidence rides
    ans = w.send(sock, "LAUNCH", **C.launch_to_wire(
        S.SessionLaunch(w.oid, key, "claude", "sonnet", "b", "passthrough", EXP, parts.workspace()), bare))
    assert ans["refused"] == REFUSED and ans["detail"].startswith("signed-role-missing")
    w.assert_nothing_staged(key, tombstone=False)


def test_role_evidence_that_is_not_the_signed_spec_is_refused(w, tmp_path):
    parts, key = make_parts(tmp_path), w.key()
    sock, _ = w.upload(key, parts)
    r = w.rig
    ok = C.signed_role_of(r.store, w.oid)
    for bad, name in ((dataclasses.replace(ok, spec=ok.spec.replace('"stop_at":', '"stop_at":1')), "signed-role-spec"),
                      (dataclasses.replace(ok, role=ok.role.replace(ROLE["name"], "other-name", 1)),
                       "signed-role-role-hash")):
        proof = dataclasses.replace(C.link_proof(r.store, r.world.root, w.oid, now=r.now()), signed=bad)
        ans = w.send(sock, "LAUNCH", **C.launch_to_wire(
            S.SessionLaunch(w.oid, key, "claude", "sonnet", "b", "passthrough", EXP, parts.workspace()), proof))
        assert ans["refused"] == REFUSED and ans["detail"].startswith(name), ans
        sock, _ = w.upload(key, parts)                  # a refused LAUNCH ended the upload: bring it back


def test_a_snapshot_whose_settings_loosen_the_sandbox_is_refused_never_stripped(w, tmp_path):
    parts, key = make_parts(tmp_path, {".claude/settings.json": '{"disableAllHooks": true}', "src/a.py": "a\n"}), w.key()
    exc = w.refused(key, parts)
    assert exc.name == REFUSED and exc.detail.startswith("loosening:disable-all-hooks")
    assert os.listdir(tmp_path / "scratch") == []


def test_a_snapshot_whose_settings_do_not_loosen_stages(w, tmp_path):
    parts, key = make_parts(tmp_path, {".claude/settings.json": '{"permissions": {"allow": ["Read"]}}',
                                       "src/a.py": "a\n"}), w.key()
    sock, _ = w.upload(key, parts)
    assert w.launch(sock, key, parts.workspace()).session == "sess-1" and w.staged.read_bytes() == parts.bundle


def test_a_snapshot_carrying_mcp_json_is_refused_and_parity_is_one_constant(w, tmp_path, monkeypatch):
    parts, key = make_parts(tmp_path, {".mcp.json": "{}\n", "src/a.py": "a\n"}), w.key()
    assert cause(w.refused(key, parts)) == "repo-carries-mcp-json"
    monkeypatch.setattr(WS, "DISPATCH_REFUSED_NAMES", ())
    key2 = w.key()
    sock, _ = w.upload(key2, parts)
    assert w.launch(sock, key2, parts.workspace()).session == "sess-1"


# --- the member and the upload must agree to exist ------------------------------------------------------------------------------

def test_a_member_with_no_upload_and_an_upload_with_no_member_are_each_refused(w, tmp_path):
    parts = make_parts(tmp_path)
    key = w.key()
    sock = w.env.connect()
    with pytest.raises(L.LauncherRefused) as exc:
        w.launch(sock, key, parts.workspace())
    assert cause(exc.value) == "no-upload"
    w.assert_nothing_staged(key, tombstone=False)       # no upload here: the key may be another connection's
    key2 = w.key()
    sock, _ = w.upload(key2, parts)
    with pytest.raises(L.LauncherRefused) as exc:
        w.launch(sock, key2, None)
    assert cause(exc.value) == "upload-without-workspace"
    w.assert_nothing_staged(key2)


def test_an_upload_begun_on_another_connection_is_not_this_launchs(w, tmp_path):
    parts, key = make_parts(tmp_path), w.key()
    holder, _ = w.upload(key, parts)                  # connection A stays open, holding its upload
    assert key in w.uploads._ups
    with pytest.raises(L.LauncherRefused) as exc:
        w.launch(w.env.connect(), key, parts.workspace())
    assert cause(exc.value) == "no-upload" and key in w.uploads._ups      # A's upload is not B's to take, nor to drop
    assert w.env.launcher.index.get(key) is None                          # nor to poison (gemini HIGH 1, codex MED 2)
    child = w.launch(holder, key, parts.workspace())                       # A's own LAUNCH still stages
    assert child.session == "sess-1" and w.staged.read_bytes() == parts.bundle
    assert w.env.launcher.index.get(key)["state"] == "started"
    child._conn.close()
    w.env.drain()


def test_a_launch_naming_no_workspace_never_drops_another_connections_upload(w, tmp_path):
    parts, key = make_parts(tmp_path), w.key()
    _a, _ = w.upload(key, parts)
    assert w.uploads.verify(key, WS.UploadOwner(), None) is None      # B holds nothing: nothing to refuse, nothing to drop
    assert key in w.uploads._ups


# --- the L3 r1 fix round: one upload per connection, every LAUNCH ends it ----------------------------------------------------

def test_a_transient_refusal_leaves_no_upload_and_the_same_connection_retries(w, tmp_path):
    parts, key = make_parts(tmp_path), w.key()
    sock, answers = w.upload(key, parts)
    assert all(a["ok"] for a in answers)
    w.env.pool = False                                                    # the pool check refuses, transient
    ans = w.raw_launch(sock, key, parts.workspace())
    assert (ans["refused"], ans["reason_class"]) == ("no-free-slot", "transient")
    assert w.uploads._ups == {} and w.env.launcher.index.get(key) is None   # nothing pinned, nothing tombstoned
    w.env.pool = True
    assert w.send(sock, "WSBEGIN", **C.ws_begin_to_wire(key, parts.manifest_bytes, parts.bundle))["ok"]
    assert all(a["ok"] for a in w.chunks(sock, key, parts))
    ok = w.raw_launch(sock, key, parts.workspace())
    assert ok["ok"] and ok["session"] == "sess-1" and w.staged.read_bytes() == parts.bundle
    sock.close()
    w.env.drain()


def test_a_launch_of_another_key_ends_this_connections_upload(w, tmp_path):
    parts, k1, k2, k3 = make_parts(tmp_path), w.key(), w.key(), w.key()
    sock, _ = w.upload(k1, parts)
    ans = w.raw_launch(sock, k2, None)                                     # bytes for k1, a launch of k2 with none
    assert ans["refused"] == REFUSED and ans["detail"].startswith("upload-without-workspace")
    assert w.uploads._ups == {} and w.env.launcher.index.get(k2) is None and w.env.launcher.index.get(k1) is None
    sock2, _ = w.upload(k1, parts)
    ans = w.raw_launch(sock2, k3, parts.workspace())                       # bytes for k1, a launch of k3 naming one
    assert ans["refused"] == REFUSED and ans["detail"].startswith("no-upload") and w.uploads._ups == {}
    assert w.runtime.starts == []


def test_the_key_stays_reserved_and_counted_while_the_stager_writes(tmp_path, monkeypatch):
    w = World(tmp_path, max_uploads=1)
    parts, key, other = make_parts(tmp_path), w.key(), w.key()
    foreign, seen = w.env.connect(), []
    real = RS.full_fsync

    def during_stage(fd):
        real(fd)
        if not seen:                                                       # mid-write, from a second real connection
            seen.append(w.send(foreign, "WSBEGIN", **C.ws_begin_to_wire(key, parts.manifest_bytes, parts.bundle)))
            seen.append(w.send(foreign, "WSBEGIN", **C.ws_begin_to_wire(other, parts.manifest_bytes, parts.bundle)))
    w.runtime.before = lambda d: monkeypatch.setattr(RS, "full_fsync", during_stage)
    sock, _ = w.upload(key, parts)
    child = w.launch(sock, key, parts.workspace())
    assert child.session == "sess-1" and w.staged.read_bytes() == parts.bundle
    assert seen[0]["refused"] == REFUSED and seen[0]["detail"].startswith("upload-exists")
    assert (seen[1]["refused"], seen[1]["reason_class"]) == ("no-workspace-capacity", "transient")
    assert w.uploads._ups == {}
    child._conn.close()
    w.env.drain()


def test_a_full_scratch_is_transient_and_the_key_stays_launchable(w, tmp_path, monkeypatch):
    parts, key = make_parts(tmp_path), w.key()

    def enospc(fd, data):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(WS, "write_all", enospc)
    sock, _ = w.upload(key, parts)
    with pytest.raises(L.LauncherRefused) as exc:
        w.launch(sock, key, parts.workspace())
    assert (exc.value.name, exc.value.reason_class) == (REFUSED, "transient")
    assert exc.value.detail.startswith("scratch-unavailable")
    w.assert_nothing_staged(key, tombstone=False)
    monkeypatch.undo()
    sock, _ = w.upload(key, parts)                                         # the host recovered: the SAME key launches
    assert w.launch(sock, key, parts.workspace()).session == "sess-1" and w.staged.read_bytes() == parts.bundle


def test_an_unsafe_scratch_parent_is_transient_not_a_tombstone(w, tmp_path):
    parts, key = make_parts(tmp_path), w.key()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    os.chmod(scratch, 0o777)                                               # _open_parent refuses: readset-workdir
    sock, _ = w.upload(key, parts)
    with pytest.raises(L.LauncherRefused) as exc:
        w.launch(sock, key, parts.workspace())
    assert exc.value.reason_class == "transient" and exc.value.detail.startswith("readset-workdir")
    w.assert_nothing_staged(key, tombstone=False)
    os.chmod(scratch, 0o700)
    sock, _ = w.upload(key, parts)
    assert w.launch(sock, key, parts.workspace()).session == "sess-1"


def test_the_verify_unlinks_only_the_input_file_it_created(w, tmp_path):
    parts, key = make_parts(tmp_path), w.key()
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    theirs = scratch / f"in-{key}.bundle"
    theirs.write_bytes(b"another verify's input")
    sock, _ = w.upload(key, parts)
    with pytest.raises(L.LauncherRefused) as exc:
        w.launch(sock, key, parts.workspace())
    assert exc.value.reason_class == "transient" and exc.value.detail.startswith("scratch-unavailable")
    assert theirs.read_bytes() == b"another verify's input"


def test_the_start_up_sweep_removes_a_crashed_verifys_workdir_and_input(w, tmp_path):
    key = w.key()
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    wd = scratch / RS.workdir_name(key)
    (wd / "P" / "objects").mkdir(parents=True)
    (wd / "handoff.bundle").write_bytes(b"x")
    (scratch / f"in-{key}.bundle").write_bytes(b"x")
    (scratch / "unrelated").write_bytes(b"kept")
    w.uploads.sweep()
    assert sorted(os.listdir(scratch)) == ["unrelated"]


def test_a_server_that_stages_no_workspace_refuses_the_member_and_the_upload(tmp_path):
    rig = Rig(tmp_path)
    add_role(rig.store, ROLE)
    env = Env(rig, tmp_path)
    parts = make_parts(tmp_path)
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    sock = env.connect()
    sock.sendall(C.encode_run_frame({"p": C.PROTOCOL, "op": "WSBEGIN", "id": 1,
                                     **C.ws_begin_to_wire("s-00000000000000b1", parts.manifest_bytes, parts.bundle)}))
    ans = C.read_run_frame(sock)
    assert ans["refused"] == REFUSED and ans["detail"].startswith("unavailable")
    client = HandUploaded(lambda: env.connect(), lambda occ: C.link_proof(rig.store, rig.world.root, occ,
                                                                          now=rig.now()),
                          signed_of=lambda occ: C.signed_role_of(rig.store, occ))
    with pytest.raises(L.LauncherRefused) as exc:
        client.spawn(S.SessionLaunch(oid, "s-00000000000000b2", "claude", "sonnet", "b", "passthrough", EXP,
                                     parts.workspace()))
    assert cause(exc.value) == "unavailable" and env.runtime.starts == []


def test_the_stager_stages_only_what_this_server_verified(w, tmp_path):
    parts = make_parts(tmp_path)
    d = tmp_path / "bare"
    d.mkdir()
    req = L.SlotStart("t", "r-0123456789abcdef", "s-00000000000000c1", "claude", "sonnet", "b", "sha256:" + "0" * 64,
                      L.HOOKS, parts.workspace(manifest_digest=parts.manifest.digest()))
    with pytest.raises(WS.WorkspaceRefused) as exc:
        w.uploads.stage(req, d)
    assert exc.value.cause == "not-verified" and os.listdir(d) == []
    plain = dataclasses.replace(req, workspace=None)
    assert w.uploads.stage(plain, d) is None and os.listdir(d) == []


# --- the verify runs before any resource: the LAUNCH order ---------------------------------------------------------------------

def test_the_workspace_judgment_sits_between_the_authority_check_and_the_pool_check():
    text = Path(D.__file__).read_text()
    launch = text[text.index('if op == "LAUNCH":'):text.index('if op in ("WSBEGIN", "WSDATA"):')]
    assert launch.index("self._verify(") < launch.index("self._workspace_of(") < launch.index("self._pool()") \
        < launch.index("self._spawn(")


# --- the wire shapes ------------------------------------------------------------------------------------------------------------

def test_the_launch_frame_carries_the_member_and_never_the_manifest_digest():
    ws = S.Workspace("/r", "a" * 40, ("x/**",), manifest_digest="sha256:" + "b" * 64)
    launch = S.SessionLaunch("o", "s-00000000000000d1", "claude", "sonnet", "b", "passthrough", EXP, ws)
    body = C.launch_to_wire(launch, _fake_proof())
    assert body["launch"]["workspace"] == {"repo": "/r", "head": "a" * 40, "withhold": ["x/**"]}
    back, _ = C.launch_from_wire(body)
    assert back.workspace == S.Workspace("/r", "a" * 40, ("x/**",)) and back.workspace.manifest_digest is None


@pytest.mark.parametrize("member", [
    {"repo": "rel/path", "head": "a" * 40, "withhold": []}, {"repo": "/r", "head": "A" * 40, "withhold": []},
    {"repo": "/r", "head": "a" * 39, "withhold": []}, {"repo": "/r", "head": "a" * 40, "withhold": "x"},
    {"repo": "/r", "head": "a" * 40, "withhold": [1]}, {"repo": "/r", "head": "a" * 40},
    {"repo": "/r", "head": "a" * 40, "withhold": [], "extra": 1}, "nope"])
def test_a_malformed_workspace_member_is_a_wire_error(member):
    launch = S.SessionLaunch("o", "s-00000000000000d2", "claude", "sonnet", "b", "passthrough", EXP)
    body = C.launch_to_wire(launch, _fake_proof())
    body["launch"]["workspace"] = member
    with pytest.raises(C.WireError):
        C.launch_from_wire(body)


def test_ws_frames_are_bounded_canonical_and_in_order():
    data = os.urandom(C.WS_CHUNK_BYTES * 2 + 17)
    frames = list(C.ws_chunk_frames("s-00000000000000d3", "bundle", data))
    assert [f["offset"] for f in frames] == [0, C.WS_CHUNK_BYTES, 2 * C.WS_CHUNK_BYTES]
    got = b""
    for f in frames:
        assert len(C.encode_run_frame({"p": C.PROTOCOL, "op": "WSDATA", "id": 1, **f})) < C.RUN_FRAME_MAX
        key, part, off, raw = C.ws_chunk_from_wire({"p": C.PROTOCOL, "op": "WSDATA", "id": 1, **f})
        assert (key, part, off) == ("s-00000000000000d3", "bundle", len(got))
        got += raw
    assert got == data
    f = dict(frames[0])
    for bad in ({**f, "data": f["data"][:-1]}, {**f, "data": "QQ="}, {**f, "part": "other"},
                {**f, "data": ""}, {**f, "extra": 1}):
        with pytest.raises(C.WireError):
            C.ws_chunk_from_wire(bad)
    with pytest.raises(C.WireError):          # QR== decodes to the same byte as QQ== and is not canonical
        C.ws_chunk_from_wire({**f, "data": "QR=="})


def test_wsbegin_declares_both_parts_and_refuses_a_malformed_digest():
    b = C.ws_begin_to_wire("s-00000000000000d4", b"m", b"bb")
    got = C.ws_begin_from_wire(b)
    assert got.sizes == {"manifest": 1, "bundle": 2} and got.session_key == "s-00000000000000d4"
    with pytest.raises(C.WireError):
        C.ws_begin_from_wire({**b, "bundle_sha256": "sha256:" + "A" * 64})
    with pytest.raises(C.WireError):
        C.ws_begin_from_wire({**b, "manifest_size": 0})


# --- the real HelperSlotRuntime stages from the real stager --------------------------------------------------------------------------

def test_the_real_helper_runtime_stages_the_bundle_before_render_and_ends_the_session_when_it_cannot(tmp_path):
    from levain.slot import dispatch_runtime as DR
    from tests.test_slot_dispatch_runtime import FakeLease, Helper, req
    parts = make_parts(tmp_path)
    store = tmp_path / "store"
    h = Helper(tmp_path, store)
    uploads = WS.WorkspaceUploads(tmp_path / "scratch")
    rt = DR.HelperSlotRuntime(host="darwin", helper=h.prefix, store=store, routes=("pypi", "pass-1"),
                              client_factory=lambda _p: FakeLease(), kill_grace_s=0.5, workspace=uploads.stage)
    base = req()
    owner = WS.UploadOwner()
    b = C.ws_begin_to_wire(base.session_key, parts.manifest_bytes, parts.bundle)
    uploads.begin(base.session_key, owner, {"manifest": b["manifest_size"], "bundle": b["bundle_size"]},
                  {"manifest": b["manifest_sha256"], "bundle": b["bundle_sha256"]})
    for part, data in (("manifest", parts.manifest_bytes), ("bundle", parts.bundle)):
        for off in range(0, len(data), C.WS_CHUNK_BYTES):
            uploads.chunk(base.session_key, owner, part, off, data[off:off + C.WS_CHUNK_BYTES])
    ws = uploads.verify(base.session_key, owner, parts.workspace(), signed_withhold=())
    handle = rt.start(dataclasses.replace(base, workspace=ws))
    handle.child.abort()
    assert (h.staging / "repo.bundle").read_bytes() == parts.bundle and h.verbs()[:2] == ["prepare", "render"]
    # a request whose workspace this stager never verified: the session is ENDed and nothing is staged
    (tmp_path / "again").mkdir()
    h2 = Helper(tmp_path / "again", tmp_path / "again" / "store")
    rt2 = DR.HelperSlotRuntime(host="darwin", helper=h2.prefix, store=tmp_path / "again" / "store", routes=("pypi", "pass-1"),
                               client_factory=lambda _p: FakeLease(), kill_grace_s=0.5, workspace=uploads.stage)
    with pytest.raises(WS.WorkspaceRefused):
        rt2.start(dataclasses.replace(base, workspace=ws))
    assert not (h2.staging / "repo.bundle").exists() and "teardown" in h2.verbs() and "render" not in h2.verbs()
