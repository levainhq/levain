"""S1 session 5 piece 5d-i session 1: the confined verifier's child (``verify_run``) and ``quarantine_verify``.

The rows come from the first run (lane adopt-5di, ``1022-20/adopt_5di/run_s1.txt``): that run used the 5c-ii-c rig's
real adopt-open export; these rows build the same shape by hand (base bundle by ``git bundle create``, the export as
the importer writes it: :func:`bundlefmt.write_header` + a non-thin ``pack-objects``) so they run on Linux CI too.
The child runs as a real process under the real ``export-verifier`` entry, through ``spawn_export``'s fd map.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from levain.slot import adopt_verify as AV, bundlefmt as BF, export_run as ER, root_tree, verify_run as VR
from levain.slot import runtime_helper as RH

GIT = shutil.which("git")
S = "s-" + "d1" * 8
WT = Path(__file__).resolve().parents[1]
ENV = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": "/nonexistent",
       "LC_ALL": "C", "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@a", "GIT_COMMITTER_NAME": "a",
       "GIT_COMMITTER_EMAIL": "a@a"}
needs_git = pytest.mark.skipif(GIT is None, reason="needs a git")


def g(*a, cwd=None, stdin=None) -> bytes:
    return subprocess.run([GIT, *a], cwd=cwd, env=ENV, check=True, capture_output=True, input=stdin).stdout


class Fx:
    """A base repo and its bundle; one commit past it (a new blob ``a.txt``); helpers to build export bundles."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        repo = tmp / "repo"
        g("init", "-q", "-b", "main", str(repo))
        (repo / "README").write_text("base\n")
        g("add", ".", cwd=repo)
        g("commit", "-qm", "base", cwd=repo)
        self.base = g("rev-parse", "HEAD", cwd=repo).decode().strip()
        self.base_bundle = tmp / "base.bundle"
        g("bundle", "create", "-q", str(self.base_bundle), "HEAD", "main", cwd=repo)
        (repo / "a.txt").write_text("a\n")
        g("add", "a.txt", cwd=repo)
        g("commit", "-qm", "one", cwd=repo)
        self.repo = repo
        self.head = g("rev-parse", "HEAD", cwd=repo).decode().strip()
        self.listing = g("rev-list", "--objects", "--no-object-names", self.head, "--not", self.base, cwd=repo).split()
        self.base_blob = g("rev-parse", f"{self.base}:README", cwd=repo).strip()
        self.new_blob = g("rev-parse", f"{self.head}:a.txt", cwd=repo).strip()

    def bundle(self, name: str, oids, refs=None, prereqs=None) -> Path:
        pk = g("pack-objects", "-q", "--stdout", cwd=self.repo, stdin=b"".join(o + b"\n" for o in oids))
        refs = refs if refs is not None else [(self.head, f"refs/levain/{S}/head".encode())]
        p = self.tmp / name
        p.write_bytes(BF.write_header("sha1", refs, [self.base] if prereqs is None else prereqs) + pk)
        return p

    def run(self, name: str, bundle: Path, result: str = "delta"):
        """The child under the real entry, as spawn_export forks it; returns (classify outcome, out/ names)."""
        tree = self.tmp / "import-tree"
        if not tree.exists():
            tree.mkdir()
            os.symlink(str(WT), tree / "current")
        entry = self.tmp / "export-verifier"
        entry.write_text(root_tree.entry_text("linux" if sys.platform.startswith("linux") else "darwin",
                                              "export-verifier", tree=tree))
        os.chmod(entry, 0o755)
        v = self.tmp / f"verify-{name}"
        for d in ("in", "scratch", "out", "log"):
            (v / d).mkdir(parents=True, mode=0o700)
        shutil.copyfile(bundle, v / "in" / "bundle")
        shutil.copyfile(self.base_bundle, v / "in" / "base.bundle")
        args = ("--git", GIT, "--session", S, "--object-format", "sha1", "--base-oid", self.base, "--result", result,
                "--max-input-size", str(1 << 30))

        def exec_child(argv, spec_fd, cgroup=None):
            os.close(spec_fd)
            os.execve(argv[0], list(argv), {})

        cmd = RH.ExportCmd((str(entry),) + args, b"{}", cwd=str(v), exec_child=exec_child)
        fr, fw = os.pipe()
        os.close(fw)
        rr, rw = os.pipe()
        o = os.open(v / "log" / "stdout", os.O_WRONLY | os.O_CREAT, 0o600)
        e = os.open(v / "log" / "stderr", os.O_WRONLY | os.O_CREAT, 0o600)
        pid = RH.spawn_export(cmd, frame_r=fr, stdout_fd=o, stderr_fd=e, result_w=rw,
                              rlimits=RH.export_rlimits(1 << 32))
        for fd in (fr, rw, o, e):
            os.close(fd)
        kind, code, raw, _ = RH.collect_export(pid, rr, 120.0, cap=1 << 16)
        os.close(rr)
        oc = ER.classify(ER.RunObservation(kind, code, line=raw), ER.VERIFIER, parse=AV.parse_verifier_line)
        return oc, sorted(os.listdir(v / "out")), v


@needs_git
def test_a_good_export_verifies_and_its_outputs_are_the_claims(tmp_path):
    fx = Fx(tmp_path)
    b = fx.bundle("good.bundle", fx.listing)
    oc, outs, v = fx.run("good", b)
    assert oc.success and oc.line.result == "delta"
    assert outs == ["preview.txt", "q.idx", "verified.json"]
    import hashlib
    from levain.contracts import canon
    got = canon.parse((v / "out" / "verified.json").read_bytes()[:-1])
    hdr = AV.read_header(b)
    data = b.read_bytes()
    assert got["refs"] == {"head": fx.head} and got["prerequisites"] == [fx.base]
    assert got["pack_offset"] == hdr.size and got["pack_sha256"] == hashlib.sha256(data[hdr.size:]).hexdigest()
    pack_dir = v / "scratch" / "q" / "repo.git" / "objects" / "pack"
    assert sorted(p.suffix for p in pack_dir.iterdir()) == [".idx", ".pack"]       # --no-rev-index: no .rev
    assert (v / "out" / "q.idx").read_bytes() == (pack_dir / got["q_idx"]["name"]).read_bytes()
    assert (v / "out" / "preview.txt").read_text().splitlines()[0] == AV.PREVIEW_HEADER
    assert not (v / "scratch" / "q" / "repo.git" / "objects" / "info" / "alternates").exists()


@needs_git
@pytest.mark.parametrize("case,expected", [
    ("extra", "bundle-extra-objects"), ("missing", "preview-graph-unresolved"),
    ("refname", "bundle-header-mismatch"), ("tiptree", "bundle-tip-not-commit"),
    ("tipbase", "bundle-tip-in-base"), ("unrelated", "export-result-mismatch"), ("tampered", None)])
def test_each_bad_export_refuses_by_its_name_and_leaves_no_output(tmp_path, case, expected):
    fx = Fx(tmp_path)
    ref = f"refs/levain/{S}/head".encode()
    tree = g("rev-parse", f"{fx.head}^{{tree}}", cwd=fx.repo).decode().strip()
    result = "delta"
    if case == "extra":
        b = fx.bundle("x.bundle", fx.listing + [fx.base_blob])
    elif case == "missing":
        b = fx.bundle("x.bundle", [o for o in fx.listing if o != fx.new_blob])
    elif case == "refname":
        b = fx.bundle("x.bundle", fx.listing, refs=[(fx.head, b"refs/heads/x")])
    elif case == "tiptree":
        b = fx.bundle("x.bundle", fx.listing, refs=[(tree, ref)])
    elif case == "tipbase":
        b = fx.bundle("x.bundle", [fx.base.encode()], refs=[(fx.base, ref)])
    elif case == "unrelated":
        b, result = fx.bundle("x.bundle", fx.listing), "unrelated"
    else:
        b = fx.bundle("x.bundle", fx.listing)
        data = bytearray(b.read_bytes())
        hdr = AV.read_header(b)
        data[hdr.size + (len(data) - hdr.size) // 2] ^= 0xFF
        b.write_bytes(bytes(data))
    oc, outs, _v = fx.run(case, b, result)
    assert not oc.success and outs == []
    assert oc.line.refused == expected        # tampered: no verifier name; root answers verifier-failed:exit
    if expected is None:
        assert oc.name == "verifier-failed:exit"


def test_the_verifier_line_parser_holds_its_closed_shape():
    ok = VR.result_line(refused=None, detail="", enospc=False, result="delta")
    assert AV.parse_verifier_line(ok).result == "delta"
    for bad in (b'{"v":1}\n', ok + ok, ok[:-1], b"x" * (1 << 17)):
        with pytest.raises(ER.LineBad):
            AV.parse_verifier_line(bad)
    # an exit 0 line with enospc true is never SUCCESS (r9 S12-1)
    oc = ER.classify(ER.RunObservation("exit", 0, line=VR.result_line(refused=None, detail="", enospc=True,
                                                                      result="delta")),
                     ER.VERIFIER, parse=AV.parse_verifier_line)
    assert oc.name == "verifier-failed:result-malformed"


def test_export_verifier_is_an_entry_and_not_a_sudo_helper():
    assert root_tree.ENTRY_MODULES["export-verifier"] == "levain.slot.verify_run"
    assert "export-verifier" not in root_tree.HELPER_MODULES
    with pytest.raises(Exception):
        root_tree.helper_entry_text("darwin", "export-verifier")


# --- fix round 1 (L3 r1, lane adopt-5di-fix1): each row built red against 662b22a9 -----------------------------------

def _two_past(fx: Fx):
    """A second commit on top of ``fx.head`` (a new blob ``b.txt``); returns its oid and the head's tree oid."""
    (fx.repo / "b.txt").write_text("b\n")
    g("add", "b.txt", cwd=fx.repo)
    g("commit", "-qm", "two", cwd=fx.repo)
    c2 = g("rev-parse", "HEAD", cwd=fx.repo).decode().strip()
    return c2, g("rev-parse", f"{fx.head}^{{tree}}", cwd=fx.repo).strip()


@needs_git
@pytest.mark.parametrize("case,expected", [
    ("tip-absent", "bundle-tip-not-commit"), ("parent-absent", "preview-graph-unresolved"),
    ("tree-absent", "preview-graph-unresolved")])
def test_an_absent_tip_parent_or_tree_refuses_by_name_not_verifier_failed(tmp_path, case, expected):
    # complement 1 (MED): an absent tip died in update-ref and an absent parent in merge-base, both unnamed (exit).
    fx = Fx(tmp_path)
    ref = f"refs/levain/{S}/head".encode()
    if case == "tip-absent":
        b = fx.bundle("x.bundle", fx.listing, refs=[("1" * 40, ref)])
    elif case == "parent-absent":
        c2, head_tree = _two_past(fx)
        objs = g("rev-list", "--objects", "--no-object-names", c2, "--not", fx.base, cwd=fx.repo).split()
        b = fx.bundle("x.bundle", [o for o in objs if o not in (fx.head.encode(), head_tree)], refs=[(c2, ref)])
    else:
        head_tree = g("rev-parse", f"{fx.head}^{{tree}}", cwd=fx.repo).strip()
        b = fx.bundle("x.bundle", [o for o in fx.listing if o != head_tree])
    oc, outs, _v = fx.run(case, b)
    assert not oc.success and outs == []
    assert oc.line.refused == expected, oc.detail


def test_the_verifier_line_parser_refuses_a_noncanonical_line_or_a_bool_version():
    # complement 3 + codex 1 (MED): canon.parse took whitespace and key order, and True == 1 passed the version.
    ok = VR.result_line(refused=None, detail="", enospc=False, result="delta")
    spaced = b'{ "v":1, "enospc":false, "refused":null, "result":"delta", "detail":"" }\n'
    reordered = b'{"v":1,"detail":"","enospc":false,"refused":null,"result":"delta"}\n'
    booly = ok.replace(b'"v":1', b'"v":true')
    assert booly != ok and reordered != ok
    for bad in (spaced, reordered, booly):
        with pytest.raises(ER.LineBad):
            AV.parse_verifier_line(bad)
        oc = ER.classify(ER.RunObservation("exit", 0, line=bad), ER.VERIFIER, parse=AV.parse_verifier_line)
        assert oc.name == "verifier-failed:result-malformed"


class _FakeGit:
    def __init__(self, rc: int, err: str) -> None:
        self.rc, self.err = rc, err

    def run(self, *a, **kw):
        return self.rc, "", self.err


def test_index_pack_names_fsck_only_from_error_or_fatal_lines():
    # complement 2 (LOW): a closed-list WARNING beside another failure was taken as the failure's name.
    warn = ("warning: object 1234abcd: zeroPaddedFilemode: contains zero-padded file modes\n"
            "fatal: pack exceeds maximum allowed size\n")
    from levain.slot import importer
    with pytest.raises(importer.Fatal):
        AV._index_pack_q(_FakeGit(128, warn), AV._Region(Path(os.devnull)), 0, 0, 1)
    err = "error: object 1234abcd: hasDotgit: contains '.git'\nfatal: fsck error in packed object\n"
    with pytest.raises(AV.Refused) as r:
        AV._index_pack_q(_FakeGit(128, err), AV._Region(Path(os.devnull)), 0, 0, 1)
    assert r.value.name == "fsck:hasDotgit"


@needs_git
def test_a_run_called_inside_an_except_block_still_succeeds(tmp_path):
    # complement 4 (LOW): run() read sys.exc_info() in its finally, so a caller's active exception rewrote a pass.
    fx = Fx(tmp_path)
    b = fx.bundle("good.bundle", fx.listing)
    v = tmp_path / "verify-inproc"
    for d in ("in", "scratch", "out", "log"):
        (v / d).mkdir(parents=True, mode=0o700)
    shutil.copyfile(b, v / "in" / "bundle")
    shutil.copyfile(fx.base_bundle, v / "in" / "base.bundle")
    argv = ["--git", GIT, "--session", S, "--object-format", "sha1", "--base-oid", fx.base, "--result", "delta",
            "--max-input-size", str(1 << 30)]
    r, w = os.pipe()
    try:
        try:
            raise ValueError("the caller's own exception")
        except ValueError:
            rc = VR.run(argv, result_fd=w, work=v)
        os.close(w)
        line = os.read(r, 1 << 16)
    finally:
        os.close(r)
    assert rc == 0, line
    assert AV.parse_verifier_line(line).result == "delta"
    assert sorted(os.listdir(v / "out")) == ["preview.txt", "q.idx", "verified.json"]
