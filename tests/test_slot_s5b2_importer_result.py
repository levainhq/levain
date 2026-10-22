"""S1 session 5 unit 5b2, step 6 and the head-ruled step-3 fixes: the result, the bundle, the report.

Each reproduced first (5b2 session 4, ``s4_repro_on_b6595140.txt``): an honest session with no ``.git`` that changed
nothing was refused ``no-ref`` (DESIGN T35 answers ``adopt-nothing-new``); a branch naming a blob, and a HEAD naming a
tree, were accepted at step 3 (the blob branch exited 0); a symbolic HEAD to a name ``check-ref-format`` refuses exited
0; and no run wrote a bundle, a report or a ``result``. The symlink-target case is session 1's S row: without the
symlink class an honest bundle's pack does not index alone on a git that checks symlink targets (v2.39.4).
"""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess

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
    assert set(report) == {"format", "session", "result", "walk", "ignored_git_paths", "named", "truncated"}
    assert report["format"] == "levain-adopt-report/1" and report["walk"] == result["walk"]
    assert canon.canonical_bytes(report) == raw
    assert got[f"refs/levain/{SESSION}/head"] == s.c1
