"""S1 session 5 unit 5b2, step 6 and the head-ruled step-3 fixes: the result, the bundle, the report.

Each reproduced first (5b2 session 4, ``s4_repro_on_b6595140.txt``): an honest session with no ``.git`` that changed
nothing was refused ``no-ref`` (DESIGN T35 answers ``adopt-nothing-new``); a branch naming a blob, and a HEAD naming a
tree, were accepted at step 3 (the blob branch exited 0); a symbolic HEAD to a name ``check-ref-format`` refuses exited
0; and no run wrote a bundle, a report or a ``result``. The symlink-target case is session 1's S row: without the
symlink class an honest bundle's pack does not index alone on a git that checks symlink targets (v2.39.4).

Also reproduced on 49a47242 (``s4b_repro_on_49a47242.txt``, L3 r1 of session 3): a file with a group execute bit but
not the owner's made a spurious 100755 wip; a write failing ENOSPC left its ``tmp_*`` name in ``objects/``; and
``_same_object`` held and inflated a whole object (peak RSS +1.9 GB for one 128 MiB blob).

And on b86fe918 (``s5c_repro_on_b86fe918.txt``, L3 r1 of session 4): a report refused past its cap, or a pack-objects
dying midway, left ``out/bundle`` behind a failed run; a report whose one named entry lands exactly on the cap was
truncated; and a side branch the bundle does not carry was named nowhere.
"""
from __future__ import annotations

import base64
import errno
import hashlib
import io
import os
import shutil
import stat
import subprocess
import tracemalloc
import zlib

import pytest

from levain.contracts import canon
from levain.slot import bundlefmt, importer, walkframe

GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="needs a git binary")
SESSION = "s-00000000000000ef"
ENV = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": "/nonexistent",
       "LC_ALL": "C", "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@a", "GIT_COMMITTER_NAME": "a",
       "GIT_COMMITTER_EMAIL": "a@a"}
CLEAN = [(b"README", b"base\n", "file", 0o644), (b"a.txt", b"a\n", "file", 0o644)]


def _git(*args, cwd=None, env=None, stdin=None):
    return subprocess.run([GIT, *args], cwd=cwd, env=env or ENV, check=True, capture_output=True,
                          input=stdin).stdout.decode().strip()


def _frame(path: bytes, data: bytes, type_: str = "file", mode: int = 0o644) -> bytes:
    if type_ in walkframe.HEADER_ONLY_TYPES:
        return walkframe.encode_header(path, type_, mode, 0, "")
    return walkframe.encode_header(path, type_, mode, len(data), "sha256:" + hashlib.sha256(data).hexdigest()) + data


class _Session:
    """A base (README; ``link``: also ``ln -> README``) and a session clone one commit past it (c1 adds a.txt)."""

    def __init__(self, tmp_path, *, link: bool = False) -> None:
        self.tmp = tmp_path
        base = tmp_path / "base"
        _git("init", "-q", "-b", "main", str(base))
        (base / "README").write_text("base\n")
        if link:
            os.symlink("README", base / "ln")
        _git("add", ".", cwd=base)
        _git("commit", "-qm", "base", cwd=base)
        self.base_dir = base
        self.base = _git("rev-parse", "main", cwd=base)
        self.bundle = tmp_path / "base.bundle"
        _git("bundle", "create", "-q", str(self.bundle), "HEAD", "main", cwd=base)
        self.repo = tmp_path / "repo"
        _git("clone", "-q", str(self.bundle), str(self.repo))
        (self.repo / "a.txt").write_text("a\n")
        _git("add", "a.txt", cwd=self.repo)
        _git("commit", "-qm", "one", cwd=self.repo)
        self.c1 = _git("rev-parse", "HEAD", cwd=self.repo)

    def run(self, refs: dict[bytes, bytes] | None, worktree):
        """``refs`` None = no session ``.git`` framed at all. Returns (rc, result line, {ref: oid}, the work dir)."""
        items = [(b"in/repo.bundle", self.bundle.read_bytes(), "file", 0o644)]
        if refs is not None:
            gd = self.repo / ".git"
            items += [(b"repo/.git/" + p.relative_to(gd).as_posix().encode(), p.read_bytes(), "file", 0o644)
                      for p in sorted((gd / "objects").rglob("*")) if p.is_file()]
            items += [(b"repo/.git/" + r, d, "file", 0o644) for r, d in refs.items()]
        items += [(b"repo/" + p, d, t, m) for p, d, t, m in worktree]
        total = sum(len(i[1]) for i in items if i[2] in walkframe.PAYLOAD_TYPES)
        stream = b"".join(_frame(*i) for i in items) + walkframe.encode_terminal(len(items), total)
        work = self.tmp / "work"
        work.mkdir()
        importer.make_skeleton(work, "sha1")
        r, w = os.pipe()
        try:
            rc = importer.run(["--git", GIT, "--session", SESSION, "--object-format", "sha1", "--base-oid", self.base,
                               "--max-bytes", str(1 << 30), "--max-input-size", str(1 << 30)],
                              stdin=io.BytesIO(stream), result_fd=w, work=work)
        finally:
            os.close(w)
        with os.fdopen(r, "rb") as fh:
            result = canon.parse(fh.read().rstrip(b"\n"))
        listing = _git("for-each-ref", "--format=%(refname) %(objectname)", env=importer.git_env(work))
        return rc, result, dict(line.split(" ") for line in listing.splitlines()), work


def _main(oid: str) -> dict[bytes, bytes]:
    return {b"HEAD": b"ref: refs/heads/main\n", b"refs/heads/main": oid.encode() + b"\n"}


def test_an_unchanged_session_with_no_git_dir_is_adopt_nothing_new(tmp_path):
    s = _Session(tmp_path)
    rc, result, got, work = s.run(None, [(b"README", b"base\n", "file", 0o644)])
    assert (rc, result["refused"], result["result"]) == (0, None, "adopt-nothing-new"), result
    assert got == {} and not (work / "out" / "bundle").exists()
    assert canon.parse((work / "out" / "report.json").read_bytes())["result"] == "adopt-nothing-new"
    # A framed session .git that leaves no ref is still the explicit refusal.
    shutil.rmtree(work)
    rc, result, got, _work = s.run({b"HEAD": b"ref: refs/heads/main\n"}, [(b"README", b"base\n", "file", 0o644)])
    assert (rc, result["refused"], result["result"]) == (1, "no-ref", None), result


@pytest.mark.parametrize("case", ["branch-blob", "head-tree", "symbolic-target"])
def test_step3_refuses_a_non_commit_tip_and_a_bad_symbolic_target_by_name(tmp_path, case):
    s = _Session(tmp_path)
    if case == "branch-blob":
        refs = {**_main(s.c1), b"refs/heads/x": _git("rev-parse", "HEAD:a.txt", cwd=s.repo).encode() + b"\n"}
        want = "ref-not-commit"
    elif case == "head-tree":
        refs = _main(_git("rev-parse", "HEAD^{tree}", cwd=s.repo))
        want = "ref-not-commit"
    else:
        refs = {b"HEAD": b"ref: refs/heads/bad..name\n", b"refs/heads/main": s.c1.encode() + b"\n"}
        want = "ref-name-bad:refs/heads/bad..name"
    rc, result, got, work = s.run(refs, CLEAN)
    assert (rc, result["refused"], result["result"]) == (1, want, None), result
    assert got == {} and not (work / "out" / "bundle").exists()


def test_a_delta_bundle_stands_alone_with_its_symlink_targets(tmp_path):
    s = _Session(tmp_path, link=True)
    ln = _git("rev-parse", "main:ln", cwd=s.base_dir)
    rc, result, got, work = s.run(_main(s.c1), CLEAN + [(b"ln", b"README", "link", 0o755)])
    assert (rc, result["refused"], result["result"]) == (0, None, "delta"), result
    data = (work / "out" / "bundle").read_bytes()
    end = data.index(b"\n\n") + 2
    header = bundlefmt.parse(data[:end])
    assert header.prerequisites == (s.base,)
    assert header.refs == ((s.c1, f"refs/levain/{SESSION}/head".encode()),)
    alone = tmp_path / "alone"
    _git("init", "-q", "--bare", str(alone))
    p = subprocess.run([GIT, "index-pack", "--stdin", "--fsck-objects"], env={**ENV, "GIT_DIR": str(alone)},
                       input=data[end:], capture_output=True)
    assert p.returncode == 0 and len(p.stdout.splitlines()) == 1, p.stderr
    held = _git("cat-file", "--batch-all-objects", "--batch-check=%(objectname)", env={**ENV, "GIT_DIR": str(alone)})
    assert ln in held.split(), "the unchanged symlink's blob is a special and must be in the pack"
    raw = (work / "out" / "report.json").read_bytes()
    report = canon.parse(raw)
    assert set(report) == {"format", "session", "result", "walk", "ignored_git_paths", "unsent_branches", "named",
                           "truncated"}
    assert report["format"] == "levain-adopt-report/1" and report["walk"] == result["walk"]
    assert canon.canonical_bytes(report) == raw
    assert got[f"refs/levain/{SESSION}/head"] == s.c1


def test_only_the_owner_execute_bit_makes_100755(tmp_path):
    s = _Session(tmp_path)
    rc, result, got, _work = s.run(_main(s.c1), [CLEAN[0], (b"a.txt", b"a\n", "file", 0o654)])
    assert (rc, result["refused"]) == (0, None), result
    assert f"refs/levain/{SESSION}/wip" not in got, "git's index rule reads the owner bit only: no change, no wip"


@pytest.mark.parametrize("with_git", [True, False])
def test_a_failed_object_write_leaves_no_temp_name(tmp_path, monkeypatch, with_git):
    s = _Session(tmp_path)
    real_write_all, real_spool = importer.write_all, importer.spool_frames
    armed = {"on": False}

    def seam(fd, data):
        if armed["on"] and not stat.S_ISFIFO(os.fstat(fd).st_mode):  # the result line's pipe is never failed
            raise OSError(errno.ENOSPC, "seam")
        return real_write_all(fd, data)

    def spool_then_arm(*a, **k):
        out = real_spool(*a, **k)
        armed["on"] = True
        return out
    monkeypatch.setattr(importer, "write_all", seam)
    monkeypatch.setattr(importer, "spool_frames", spool_then_arm)
    rc, result, _got, work = s.run(_main(s.c1) if with_git else None, CLEAN)
    assert (rc, result["enospc"]) == (1, True), result
    assert not list((work / "repo.git" / "objects").rglob("tmp_*"))


def test_same_object_streams(tmp_path):
    raw = b"blob %d\0" % (32 << 20) + os.urandom(32 << 20)
    oid = hashlib.sha1(raw).hexdigest()
    path = tmp_path / "obj"
    path.write_bytes(zlib.compress(raw, 1))
    del raw
    tracemalloc.start()
    try:
        assert importer._same_object(path, oid, "sha1")
        _now, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 << 20, f"peak {peak >> 20} MiB for a 32 MiB object"
    with open(path, "ab") as fh:
        fh.write(b"x")
    assert not importer._same_object(path, oid, "sha1"), "bytes after the zlib stream are not the object"


@pytest.mark.parametrize("seam", ["report-refused", "pack-objects-dies"])
def test_a_failed_step_6_leaves_no_output(tmp_path, monkeypatch, seam):
    s = _Session(tmp_path)
    if seam == "report-refused":
        real = importer.write_bundle

        def write_bundle(*a, **k):
            real(*a, **k)
            monkeypatch.setattr(importer, "REPORT_CAP", 64)
        monkeypatch.setattr(importer, "write_bundle", write_bundle)
    else:
        real_must = importer.GitRunner.must

        def must(self, *args, **k):
            if args[0] == "pack-objects":
                os.write(k["stdout_fd"], b"P" * 100)
                raise importer.Fatal(None, "seam: pack-objects died")
            return real_must(self, *args, **k)
        monkeypatch.setattr(importer.GitRunner, "must", must)
    rc, result, _got, work = s.run(_main(s.c1), CLEAN + [(b"new", b"n\n", "file", 0o644)])
    assert rc == 1 and result["result"] is None, result
    left = [p for p in ("out/bundle", "out/report.json", "spool/bundle.part", "spool/report.part")
            if os.path.lexists(work / p)]
    assert left == [], "a failed run publishes nothing"


def test_side_branches_left_behind_are_named(tmp_path):
    s = _Session(tmp_path)
    refs = {**_main(s.base), b"refs/heads/side": s.c1.encode() + b"\n", b"refs/heads/old": s.base.encode() + b"\n"}
    rc, result, _got, work = s.run(refs, [CLEAN[0]])
    assert (rc, result["result"]) == (0, "adopt-nothing-new"), result
    report = canon.parse((work / "out" / "report.json").read_bytes())
    assert report["unsent_branches"] == [{"ref_b64": base64.b64encode(b"refs/heads/side").decode()}]


def test_an_entry_that_lands_exactly_on_the_cap_is_kept(tmp_path, monkeypatch):
    wt = [CLEAN[0], (b"p", b"", "fifo", 0o644)]
    rc, _result, _got, work = _Session(tmp_path / "a").run(None, wt)
    size = (work / "out" / "report.json").stat().st_size
    for cap, kept in ((size, 1), (size - 1, 0)):
        monkeypatch.setattr(importer, "REPORT_CAP", cap)
        rc, _result, _got, work = _Session(tmp_path / f"cap{cap}").run(None, wt)
        report = canon.parse((work / "out" / "report.json").read_bytes())
        assert (rc, len(report["named"]), report["truncated"]) == (0, kept, not kept), cap
