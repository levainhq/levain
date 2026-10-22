"""S1 session 5 unit 5b2, step 3: the importer's refs, parsed as data.

Both tests reproduced (5b2 session 2, ``s2_repro_old.txt``): before step 3 the importer exited 0 on a session carrying
``refs/heads/levain-none`` and on one whose ``main`` was packed AND loose, and imported no ref at all.
``test_the_reserved_branch_name_is_refused``: DONE row R6 (addendum A1: root's HEAD names that branch, so a session
ref of that name would make HEAD resolve to slot-supplied content).
``test_peel_lines_are_skipped_and_a_loose_ref_overrides_its_packed_one``: DONE row R1.
"""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess

import pytest

from levain.contracts import canon
from levain.slot import importer, walkframe

GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="needs a git binary")
SESSION = "s-00000000000000ab"
ENV = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": "/nonexistent",
       "LC_ALL": "C", "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@a", "GIT_COMMITTER_NAME": "a",
       "GIT_COMMITTER_EMAIL": "a@a"}


def _git(*args, cwd=None):
    return subprocess.run([GIT, *args], cwd=cwd, env=ENV, check=True, capture_output=True).stdout.decode().strip()


def _file(path: bytes, data: bytes) -> bytes:
    return walkframe.encode_header(path, "file", 0o644, len(data), "sha256:" + hashlib.sha256(data).hexdigest()) + data


def _session(tmp_path):
    """A base bundle and a session repo two commits past it (the new objects loose)."""
    base = tmp_path / "base"
    _git("init", "-q", "-b", "main", str(base))
    (base / "README").write_text("base\n")
    _git("add", "README", cwd=base)
    _git("commit", "-qm", "base", cwd=base)
    bundle = tmp_path / "base.bundle"
    _git("bundle", "create", "-q", str(bundle), "HEAD", "main", cwd=base)
    repo = tmp_path / "repo"
    _git("clone", "-q", str(bundle), str(repo))
    commits = []
    for name in ("one", "two"):
        (repo / name).write_text(name + "\n")
        _git("add", name, cwd=repo)
        _git("commit", "-qm", name, cwd=repo)
        commits.append(_git("rev-parse", "HEAD", cwd=repo))
    objects = [(p.relative_to(repo / ".git").as_posix().encode(), p.read_bytes())
               for p in sorted((repo / ".git" / "objects").rglob("*")) if p.is_file()]
    return bundle.read_bytes(), _git("rev-parse", "main", cwd=base), objects, commits


def _run(tmp_path, make_refs):
    """``make_refs(commits)`` -> {path under .git: bytes}: the session's ref files."""
    bundle, base_oid, objects, commits = _session(tmp_path)
    refs = make_refs(commits)
    items = [(b"in/repo.bundle", bundle)] + [(b"repo/.git/" + r, d) for r, d in objects]
    items += [(b"repo/.git/" + r, d) for r, d in refs.items()]
    stream = b"".join(_file(p, d) for p, d in items) + walkframe.encode_terminal(len(items),
                                                                                 sum(len(d) for _p, d in items))
    work = tmp_path / "work"
    work.mkdir()
    importer.make_skeleton(work, "sha1")
    r, w = os.pipe()
    try:
        rc = importer.run(["--git", GIT, "--session", SESSION, "--object-format", "sha1", "--base-oid", base_oid,
                           "--max-bytes", str(1 << 30), "--max-input-size", str(1 << 30)],
                          stdin=io.BytesIO(stream), result_fd=w, work=work)
    finally:
        os.close(w)
    with os.fdopen(r, "rb") as fh:
        result = canon.parse(fh.read().rstrip(b"\n"))
    listing = subprocess.run([GIT, "for-each-ref", "--format=%(refname) %(objectname)"], env=importer.git_env(work),
                             capture_output=True, text=True, check=True).stdout
    return rc, result, dict(line.split(" ") for line in listing.splitlines()), work, commits


@pytest.mark.parametrize("where", ["loose", "packed"])
def test_the_reserved_branch_name_is_refused(tmp_path, where):
    def make(c):
        reserved = {"loose": {b"refs/heads/levain-none": c[0].encode() + b"\n"},
                    "packed": {b"packed-refs": c[0].encode() + b" refs/heads/levain-none\n"}}[where]
        return {b"HEAD": b"ref: refs/heads/main\n", b"refs/heads/main": c[1].encode() + b"\n", **reserved}
    rc, result, refs, work, _c = _run(tmp_path, make)
    assert (rc, result["refused"]) == (1, "ref-reserved:refs/heads/levain-none"), result
    assert refs == {}
    assert (work / "repo.git" / "HEAD").read_bytes() == b"ref: refs/heads/levain-none\n"


def test_peel_lines_are_skipped_and_a_loose_ref_overrides_its_packed_one(tmp_path):
    def make(c):
        c1, c2 = (x.encode() for x in c)
        packed = (b"# pack-refs with: peeled fully-peeled sorted \n" + c1 + b" refs/heads/main\n"
                  + b"^" + c2 + b" refs/heads/evil\n" + c1 + b" refs/tags/v1\n")
        return {b"HEAD": b"ref: refs/heads/main\n", b"packed-refs": packed, b"refs/heads/main": c2 + b"\n"}
    rc, result, refs, _work, (_c1, c2) = _run(tmp_path, make)
    assert (rc, result["refused"]) == (0, None), result
    assert refs == {f"refs/levain/{SESSION}/heads/main": c2, f"refs/levain/{SESSION}/head": c2}
