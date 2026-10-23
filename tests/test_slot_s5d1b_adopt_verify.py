"""S1 session 5 piece 5d-i session 2: the ``adopt-verify`` root verb (DESIGN §6.0, §6.3, §4.1; CONTRACT §5.1).

Two kinds of rows. The PURE rows (grammar, the caller split by class, the classifier's closed verifier-name set, the
wire line, the verifier publish's output check) run on any host. The SEAM rows run the whole verb on macOS as this
uid, built from the first run (``project_memory/1022-20/adopt_5di/s2_run.txt``): the 5c-ii-c rig's REAL adopt-open
makes the export, then ``runtime_helper.main(["adopt-verify", ...])`` with a second REAL hdiutil image at
``.verify/``, the REAL ``export-verifier`` entry run by the seam exec, real git and the real publish into
``<class>/<s>/verify/``. The seams stand in for root only (the rig's), plus a pf seam; the drop, the profile applied,
the pf live check and the watchdog over a real uid are 5g rows.
"""
from __future__ import annotations

import base64
import dataclasses
import fcntl
import hashlib
import importlib.util
import io
import os
import sys
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import adopt_verify as AV
from levain.slot import bundlefmt as BF
from levain.slot import export_run as ER
from levain.slot import records as R
from levain.slot import root_tree
from levain.slot import runtime_grammar as RG
from levain.slot import runtime_helper as RH

S = "s-" + "c2" * 8
DIGEST = "sha256:" + "ab" * 32
_spec = importlib.util.spec_from_file_location("adopt_fx_5d1b", Path(__file__).with_name("test_slot_s5c2c_adopt_open.py"))
fx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fx)
SEAM = pytest.mark.skipif(sys.platform != "darwin" or fx.GIT is None or not os.path.exists(ER.HDIUTIL),
                          reason="the seam run needs macOS hdiutil and a git")
GIT_ONLY = pytest.mark.skipif(fx.GIT is None, reason="the fixture store needs a git")


# --- grammar --------------------------------------------------------------------------------------------------------

def _argv(a, caller=RG.OPERATOR):
    return RG.parse_runtime_argv(a, configured_slots={"egress": 4, "none": 2}, caller=caller)


def test_adopt_verify_argv_parses_for_both_callers():
    for caller in (RG.OPERATOR, RG.DISPATCH):
        req = _argv(["adopt-verify", S, "--bundle-digest", DIGEST], caller)
        assert (req.verb, req.session, req.bundle_digest, req.needs_lease) == ("adopt-verify", S, DIGEST, False)
    assert "adopt-verify" not in RG.DISPATCH_FORBIDDEN and "adopt-verify" not in RG.STDIN_VERBS


@pytest.mark.parametrize("a,name", [
    (["adopt-verify", S], "missing-argv"),
    (["adopt-verify", S, "--bundle-digest"], "missing-argv"),
    (["adopt-verify", S, "--bundle-digest", DIGEST, "x"], "extra-argv"),
    (["adopt-verify", S, "--digest", DIGEST], "bad-token"),
    (["adopt-verify", "--bundle-digest", DIGEST, S], "bad-token"),
    (["adopt-verify", S, "--bundle-digest", "sha256:" + "AB" * 32], "bad-token"),
    (["adopt-verify", S, "--bundle-digest", "sha1:" + "ab" * 20], "bad-token"),
    (["adopt-verify", S, "--bundle-digest", DIGEST + "\n"], "bad-token"),
    (["adopt-verify", "../x", "--bundle-digest", DIGEST], "bad-token"),
])
def test_adopt_verify_argv_refused_by_name(a, name):
    with pytest.raises(Refused) as e:
        _argv(a)
    assert e.value.name == name


def test_wire_carries_the_contract_qualified_names_and_still_refuses_hostile_ones():
    for name in ("fsck:hasDotgit", "fsck:zeroPaddedFilemode", "git-below-floor:2.38.1", "preview-cap:commits"):
        assert RG.refusal_line(Refused(name)) == '{"ok":false,"refused":"%s"}\n' % name
    for hostile in ("A", "Fsck:x", "fsck:", "fsck:a\nb", "fsck:a..b", "fsck:.a", "fsck:a b", "x:y:z", "../x"):
        assert RG.refusal_line(Refused(hostile)) == '{"ok":false,"refused":"internal"}\n', hostile


# --- the classifier: the verifier's closed name set, verbatim (head ruling 1022+20 Q1) -------------------------------

def _vline(refused=None, result=None, enospc=False):
    return canon.canonical_bytes({"v": 1, "enospc": enospc, "refused": refused, "result": result,
                                  "detail": ""}) + b"\n"


def _vclassify(line, code=1):
    return ER.classify(ER.RunObservation("exit", code, line=line), ER.VERIFIER, parse=AV.parse_verifier_line)


@pytest.mark.parametrize("name", sorted(ER.VERIFIER_NAMES))
def test_every_verifier_name_is_answered_verbatim_and_rides_the_wire(name):
    out = _vclassify(_vline(refused=name))
    assert (out.success, out.name) == (False, name)
    assert RG.refusal_line(Refused(out.name)) == '{"ok":false,"refused":"%s"}\n' % name


def test_the_verifier_name_set_is_closed():
    for off in ("fsck:notAnId", "bundle-capability:filter", "verifier-failed:time", "no-ref"):
        out = _vclassify(_vline(refused=off))
        assert out.name == "verifier-failed:result-malformed", off
    # a child that dies, overruns or writes no line keeps verifier-failed:<reason> (§6.0)
    assert _vclassify(_vline(refused=None)).name == "verifier-failed:exit"
    assert ER.classify(ER.RunObservation("signal", None, line=None), ER.VERIFIER,
                       parse=AV.parse_verifier_line).name == "verifier-failed:signal"
    assert _vclassify(None).name == "verifier-failed:result-malformed"
    assert _vclassify(b"x" * 70000).name == "verifier-failed:result-oversize"
    # the importer is unchanged: its refusals still ride as importer-failed:<base> (A1b)
    assert not ER.IMPORTER.verbatim


def test_the_closed_set_holds_every_name_quarantine_verify_raises():
    import re
    src = Path(AV.__file__).read_text()
    raised = set(re.findall(r'Refused\("([a-z][a-z0-9:-]*)"', src)) | {BF.MISMATCH}
    assert raised <= ER.VERIFIER_NAMES, raised - ER.VERIFIER_NAMES


# --- the verifier publish ------------------------------------------------------------------------------------------

def _outputs(tmp, names):
    work = tmp / "work"
    (work / "out").mkdir(parents=True)
    for n in names:
        (work / "out" / n).write_bytes(n.encode() + b"\n")
    sdir = tmp / "s"
    sdir.mkdir()
    return os.open(work, os.O_RDONLY | os.O_DIRECTORY), os.open(sdir, os.O_RDONLY | os.O_DIRECTORY), sdir


def test_verifier_publish_stages_verify_new_and_commits_verify(tmp_path):
    wfd, sfd, sdir = _outputs(tmp_path, ER.VERIFIER_OUTPUTS)
    ok = _vclassify(_vline(result="delta"), code=0)
    try:
        res, pub = ER.publish_if_success(ok, wfd, sfd, "verify", object_format="sha1", owner_uid=os.getuid(),
                                         caps={n: 1 << 20 for n in ER.VERIFIER_OUTPUTS}, seams=ER.PublishSeams(),
                                         role=ER.VERIFIER)
    finally:
        os.close(wfd)
        os.close(sfd)
    assert res == "delta" and sorted(os.listdir(sdir)) == ["verify"]
    assert sorted(os.listdir(sdir / "verify")) == sorted(ER.VERIFIER_OUTPUTS)
    assert pub.digests["q.idx"] == hashlib.sha256(b"q.idx\n").hexdigest()


def test_verifier_publish_a_missing_output_is_output_missing_and_publishes_nothing(tmp_path):
    wfd, sfd, sdir = _outputs(tmp_path, ER.VERIFIER_OUTPUTS[:2])
    ok = _vclassify(_vline(result="delta"), code=0)
    try:
        with pytest.raises(Refused) as e:
            ER.publish_if_success(ok, wfd, sfd, "verify", object_format="sha1", owner_uid=os.getuid(),
                                  caps={n: 1 << 20 for n in ER.VERIFIER_OUTPUTS}, seams=ER.PublishSeams(),
                                  role=ER.VERIFIER)
    finally:
        os.close(wfd)
        os.close(sfd)
    assert e.value.name == "verifier-failed:output-missing" and os.listdir(sdir) == []


# --- the caller split by class (CONTRACT §5.1; c7.10 fix1 F2, head 1022+20 Q1) -----------------------------------------

TICKET = base64.urlsafe_b64encode(b"\x07" * 32).rstrip(b"=").decode()   # a well-formed handle
DISPATCH_UID = 3901


def _ctx(L, caller, sudo_uid, seams=None):
    seams = seams or RH.Seams(host="darwin", store=L, euid=lambda: 0, trusted=lambda st: True)
    return RH.Ctx(seams, RH.Store(seams), sudo_uid, caller, os.getuid(), {})


def _ticketed(L, *, supervisor):
    a = R.parse_alloc((L / "alloc" / f"{S}.json").read_bytes())
    a = dataclasses.replace(a, kind="dispatch", ticket=TICKET, supervisor_uid=supervisor)
    (L / "alloc" / f"{S}.json").write_bytes(R.alloc_bytes(a))


@GIT_ONLY
def test_dispatch_adopt_verify_requires_minted_ticket(tmp_path):
    L = fx.make_store(tmp_path, "delta")
    with pytest.raises(Refused) as e:          # dispatch on an alloc with no ticket: an operator's session
        RH.verify_caller(_ctx(L, RG.DISPATCH, DISPATCH_UID), S)
    assert e.value.name == "no-session"
    _ticketed(L, supervisor=DISPATCH_UID + 1)  # a ticket another lease peer minted
    with pytest.raises(Refused) as e:
        RH.verify_caller(_ctx(L, RG.DISPATCH, DISPATCH_UID), S)
    assert e.value.name == "no-session"


@GIT_ONLY
def test_dispatch_adopt_verify_ticketed_call_succeeds(tmp_path):
    L = fx.make_store(tmp_path, "delta")
    a, k = RH.verify_caller(_ctx(L, RG.OPERATOR, os.getuid()), S)
    assert (a.session, k) == (S, "operator")
    _ticketed(L, supervisor=DISPATCH_UID)
    a, k = RH.verify_caller(_ctx(L, RG.DISPATCH, DISPATCH_UID), S)
    assert (a.ticket, k) == (TICKET, "dispatch")


@GIT_ONLY
def test_adopt_verify_caller_split_wrong_class_refused(tmp_path):
    L = fx.make_store(tmp_path, "delta")
    _ticketed(L, supervisor=DISPATCH_UID)
    with pytest.raises(Refused) as e:          # the operator uid on a dispatch-class session
        RH.verify_caller(_ctx(L, RG.OPERATOR, os.getuid()), S)
    assert e.value.name == "adopt-kind:dispatch"
    with pytest.raises(Refused) as e:          # another uid's operator session
        RH.verify_caller(_ctx(L, RG.OPERATOR, os.getuid() + 1), S)
    assert e.value.name == "no-session"


# --- the seam run: the whole verb, real image, real verifier entry, real publish --------------------------------------

class VRig:
    """The adopt-open rig after its real adopt-open, plus the verifier's launch and seams."""

    def __init__(self, tmp: Path, *, pf_live=lambda uid: True, git_version=None) -> None:
        self.rig = fx.Rig(tmp, "delta")
        rc, line, err = self.rig.run()
        assert rc == 0, err
        self.digest = canon.parse(line.strip().encode())["bundle_digest"]
        self.L = self.rig.L
        self.sdir = self.L / "export" / "operator" / S
        self.mnt = ER.mount_path(str(self.L / "export"), "verifier")
        self.entry = tmp / "export-verifier"
        self.entry.write_text(root_tree.entry_text("darwin", "export-verifier", tree=tmp / "import-tree"))
        os.chmod(self.entry, 0o755)
        vs = {"pf_live": pf_live}
        if git_version is not None:
            vs["git_version"] = git_version
        self.vseams = RH.VerifySeams(**vs)

    def launch(self, ctx, args, export_class):
        return RH.ExportCmd((str(self.entry),) + tuple(args), b"{}", cwd=self.mnt, exec_child=fx.seam_exec)

    def seams(self):
        return dataclasses.replace(self.rig.seams(), verify=self.vseams, verify_run=RH.AdoptRunSeams(
            seal=self.rig.seal, launch=self.launch, spawn=self.rig.spawn, publish_gid=lambda c, k: None))

    def verify(self, digest=None, sudo=None):
        del self.rig.log[:]
        out, err = io.StringIO(), io.StringIO()
        rc = RH.main(["adopt-verify", S, "--bundle-digest", digest or self.digest], stdin=b"",
                     env={"SUDO_UID": str(sudo if sudo is not None else os.getuid())}, seams=self.seams(),
                     out=out, err=err)
        return rc, canon.parse(out.getvalue().strip().encode()), err.getvalue()

    def snap(self):
        v = self.sdir / "verify"
        return {n: (v / n).read_bytes() for n in sorted(os.listdir(v))} if v.exists() else None

    def clean(self) -> bool:
        """Nothing of the run is left: no verify.new, no probe dir, the image detached and unlinked."""
        left = set(os.listdir(self.sdir)) - {"bundle", "report.json", "verify"}
        return (not left and not os.path.ismount(self.mnt)
                and not os.path.exists(ER.image_path(str(self.L / "export"), "verifier", "darwin")))


def _sha(p):
    return "sha256:" + hashlib.sha256(Path(p).read_bytes()).hexdigest()


@pytest.fixture
def vrig(tmp_path):
    r = VRig(tmp_path)
    yield r
    r.rig.cleanup()


@SEAM
def test_adopt_verify_publishes_by_copy(vrig):
    rc, out, err = vrig.verify()
    assert rc == 0, err
    v = vrig.sdir / "verify"
    assert sorted(out) == ["idx_digest", "ok", "preview_digest", "verified_digest"]
    assert (out["verified_digest"], out["idx_digest"], out["preview_digest"]) == (
        _sha(v / "verified.json"), _sha(v / "q.idx"), _sha(v / "preview.txt"))
    assert {n: (v / n).stat().st_mode & 0o777 for n in os.listdir(v)} == dict.fromkeys(ER.VERIFIER_OUTPUTS, 0o640)
    rec = canon.parse((v / "verified.json").read_bytes().strip())
    assert rec["result"] == "delta" and set(rec["refs"]) == {"head"}
    assert (v / "preview.txt").read_text().startswith(AV.PREVIEW_HEADER)
    assert vrig.clean()
    log = vrig.rig.log
    order = ["verify:2", "verify:3", "verify:4", "verify:5", "verify:floor", "verify:6", "seal-in done",
             "verify:copy-in", "verify:7", "spawned", "verify:10", "verify:11", "seal-out domain proven empty",
             "verify:classify", "verify:publish", "fsync staged", "image detached", "noreplace", "verify:12",
             "verify:teardown", "verify:adopt-unlocked", "verify:session-unlocked"]
    assert [x for x in log if x in order] == order
    # a re-verify commits by the exchange over the older verify/, then deletes the older tree
    rc, out2, err = vrig.verify()
    assert rc == 0 and out2 == out and "exchange" in vrig.rig.log and "delete older" in vrig.rig.log
    assert vrig.clean()


@SEAM
def test_stale_verify_new_is_cleared_at_seal_in(vrig):
    (vrig.sdir / "verify.new").mkdir()
    (vrig.sdir / "verify.new" / "junk").write_bytes(b"stale\n")
    rc, out, err = vrig.verify()
    assert rc == 0, err
    assert "stale-staging deleted verify.new" in vrig.rig.log
    assert vrig.rig.log.index("stale-staging deleted verify.new") < vrig.rig.log.index("spawned")
    assert vrig.clean()


@SEAM
def test_a_verifier_refusal_is_verbatim_and_publishes_nothing(vrig, tmp_path):
    assert vrig.verify()[0] == 0
    before = vrig.snap()
    export = vrig.sdir / "bundle"
    real = export.read_bytes()
    h = AV.read_header(export)
    full = tmp_path / "full.git"
    g = lambda *a, stdin=None: fx.subprocess.run([fx.GIT, *a], env=fx.ENV, check=True, capture_output=True,
                                                 input=stdin).stdout
    base_bundle = vrig.L / "retained" / S / "tree" / "in" / "repo.bundle"
    g("init", "-q", "--bare", str(full))
    g("--git-dir", str(full), "fetch", "-q", str(base_bundle), "refs/heads/*:refs/base/*")
    g("--git-dir", str(full), "bundle", "unbundle", str(export))
    base_oid, _ = BF.base_of(AV.read_header(base_bundle))
    head = dict((n.decode().rsplit("/", 1)[1], o) for o, n in h.refs)["head"]
    listing = g("--git-dir", str(full), "rev-list", "--objects", "--no-object-names", head, "--not", base_oid).split()
    extra = g("--git-dir", str(full), "rev-parse", f"{base_oid}:README").strip()
    pk = g("--git-dir", str(full), "pack-objects", "-q", "--stdout", stdin=b"".join(x + b"\n" for x in listing + [extra]))
    export.write_bytes(BF.write_header("sha1", [(head, f"refs/levain/{S}/head".encode())], [base_oid]) + pk)
    rc, out, err = vrig.verify(_sha(export))
    assert (rc, out) == (RH.EXIT_REFUSED, {"ok": False, "refused": "bundle-extra-objects"})
    assert vrig.snap() == before and vrig.clean()
    # a corrupt pack has no verifier name: root's own observation (5d-i S1 Q5)
    t = bytearray(real)
    t[h.size + (len(real) - h.size) // 2] ^= 0xFF
    export.write_bytes(bytes(t))
    rc, out, err = vrig.verify(_sha(export))
    assert out == {"ok": False, "refused": "verifier-failed:exit"} and "spawned" in vrig.rig.log
    assert vrig.snap() == before and vrig.clean()


@SEAM
def test_a_changed_export_is_refused_before_the_spawn(vrig):
    assert vrig.verify()[0] == 0
    before = vrig.snap()
    export = vrig.sdir / "bundle"
    real = export.read_bytes()
    h = AV.read_header(export)
    # a byte in the pack header (root's own pack-count read refuses it first: still export-changed, the digest is
    # checked on that path) and one mid pack (caught by the copy-in's hash)
    for at in (h.size + 1, h.size + (len(real) - h.size) // 2):
        t = bytearray(real)
        t[at] ^= 0xFF
        export.write_bytes(bytes(t))
        for d in (vrig.digest, "sha256:" + "0" * 64):
            rc, out, err = vrig.verify(d)
            assert out == {"ok": False, "refused": "export-changed"} and "spawned" not in vrig.rig.log, (at, d)
            assert vrig.snap() == before and vrig.clean()
    export.write_bytes(real)
    rc, out, err = vrig.verify("sha256:" + "0" * 64)     # the real file, the wrong digest
    assert out == {"ok": False, "refused": "export-changed"} and "spawned" not in vrig.rig.log
    assert vrig.verify()[0] == 0


@SEAM
def test_session_lock_held_is_session_busy(vrig):
    fd = os.open(vrig.L / "alloc" / f"{S}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rc, out, err = vrig.verify()
    finally:
        os.close(fd)
    assert out == {"ok": False, "refused": "session-busy"} and "verify:6" not in vrig.rig.log
    assert vrig.verify()[0] == 0


@SEAM
def test_adopt_verify_takes_adopt_lock(vrig):
    fd = os.open(vrig.L / "export" / "adopt.lock", os.O_RDWR | os.O_CREAT, 0o600)
    vrig.vseams = dataclasses.replace(vrig.vseams, lock_wait_s=0.3)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rc, out, err = vrig.verify()
    finally:
        os.close(fd)
    assert out == {"ok": False, "refused": "adopt-busy"} and "verify:6" not in vrig.rig.log


@SEAM
def test_wrong_caller_refused_by_name(vrig):
    rc, out, err = vrig.verify(sudo=os.getuid() + 1)
    assert out == {"ok": False, "refused": "unknown-caller"} and "verify:2" not in vrig.rig.log


@SEAM
def test_git_below_floor_refused(tmp_path):
    r = VRig(tmp_path, git_version=lambda: "git version 2.38.1\n")
    try:
        rc, out, err = r.verify()
        assert out == {"ok": False, "refused": "git-below-floor:2.38.1"} and "verify:6" not in r.rig.log
    finally:
        r.rig.cleanup()


@SEAM
def test_pf_not_live_refuses_before_the_spawn_and_the_default_fails_closed(tmp_path):
    r = VRig(tmp_path, pf_live=lambda uid: False)
    try:
        rc, out, err = r.verify()
        assert out == {"ok": False, "refused": "verifier-network-unisolated"} and "spawned" not in r.rig.log
        assert r.clean()
        r.vseams = RH.VerifySeams()                 # the real default: no live check exists yet (5g)
        rc, out, err = r.verify()
        assert out == {"ok": False, "refused": "verifier-network-unisolated"} and "spawned" not in r.rig.log
        assert r.clean() and not (r.sdir / "verify").exists()
    finally:
        r.rig.cleanup()


def test_v30_formulas():
    assert RH.idx_bound(0, "sha1") == 8 + 1024 + 2 * 20
    assert RH.idx_bound(10, "sha256") == 8 + 1024 + 10 * 48 + 64
    got = RH.verify_image_bytes(bundle_bytes=100, base_bytes=50, bundle_objects=3, base_objects=2,
                                object_format="sha1")
    assert got == (300 + RH.idx_bound(3, "sha1") + RH.idx_bound(2, "sha1") + RH.VERIFY_OUTPUT_CAP + 2 * 5 * 41
                   + RH.VERIFY_MARGIN_BYTES)
    assert RH.verify_wall_s(1 << 30, 1 << 30) == 300 + 240


@pytest.mark.parametrize("host,uid,gid", [("darwin", 3797, 3781), ("linux", 797, 781)])
def test_the_product_launch_is_the_verifier_entry_as_levain_verify(host, uid, gid):
    # verify_launch_default: the verifier's spec (no class), argv[0] the export-verifier entry, root's fixed argv
    # within the spec's string cap.
    from levain.slot import wall
    from levain.slot.layout import HELPER_DIR
    seams = RH.Seams(host=host, account_uid={"_levain_verify": uid}.__getitem__,
                     group_gid={"levain-verify": gid}.__getitem__, host_links=lambda: {})
    ctx = RH.Ctx(seams, None, 0, RG.OPERATOR, 501, {})
    base = {"object_format": "sha1", "oid": "f" * 40}
    cmd = RH.verify_launch_default(ctx, RH.verifier_args(S, base, "delta", 1234), "dispatch")
    spec = canon.parse(cmd.spec_json)
    assert spec["argv"][0] == str(HELPER_DIR[host] / wall.EXPORT_ENTRY["verifier"])
    assert spec["argv"][1:] == ["--git", wall.GIT, "--session", S, "--object-format", "sha1", "--base-oid", "f" * 40,
                                "--result", "delta", "--max-input-size", "1234"]
    assert spec["uid"] == uid and spec["target"] == "verifier"
