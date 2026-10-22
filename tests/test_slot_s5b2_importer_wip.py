"""S1 session 5 unit 5b2, steps 3-4: a refs pair git cannot hold, and the importer's worktree commit.

Both reproduced (5b2 session 3, ``s3_repro_on_1ead577a.txt``): before step 4 the importer imported no wip at all, and two
branches ``a`` and ``a/b`` failed ``update-ref`` with no name (``git update-ref rc 128``, refused null).
``test_a_branch_that_is_a_directory_of_another_is_refused_by_name``: DONE row W8.
``test_the_wip_keeps_modes_and_names_a_path_update_index_drops``: DONE rows W1, W5 and W9 (``update-index
--index-info`` drops a path it rejects with exit 0, so the importer counts the index it wrote).
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
SESSION = "s-00000000000000cd"
ENV = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": "/nonexistent",
       "LC_ALL": "C", "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@a", "GIT_COMMITTER_NAME": "a",
       "GIT_COMMITTER_EMAIL": "a@a"}


def _git(*args, cwd=None, env=None):
    return subprocess.run([GIT, *args], cwd=cwd, env=env or ENV, check=True, capture_output=True).stdout


def _frame(path: bytes, data: bytes, type_: str = "file", mode: int = 0o644) -> bytes:
    return walkframe.encode_header(path, type_, mode, len(data), "sha256:" + hashlib.sha256(data).hexdigest()) + data


def _run(tmp_path, refs, worktree):
    """A base (README) and a session one commit past it; ``refs(c1)`` -> the session's ref files; ``worktree``:
    (path, data, type, mode) under ``repo/``. Returns (rc, result, {ref: oid}, the importer's env)."""
    base = tmp_path / "base"
    _git("init", "-q", "-b", "main", str(base))
    (base / "README").write_text("base\n")
    _git("add", "README", cwd=base)
    _git("commit", "-qm", "base", cwd=base)
    bundle = tmp_path / "base.bundle"
    _git("bundle", "create", "-q", str(bundle), "HEAD", "main", cwd=base)
    repo = tmp_path / "repo"
    _git("clone", "-q", str(bundle), str(repo))
    (repo / "a.txt").write_text("a\n")
    _git("add", "a.txt", cwd=repo)
    _git("commit", "-qm", "one", cwd=repo)
    c1 = _git("rev-parse", "HEAD", cwd=repo).decode().strip()
    items = [(b"in/repo.bundle", bundle.read_bytes(), "file", 0o644)]
    items += [(b"repo/.git/" + p.relative_to(repo / ".git").as_posix().encode(), p.read_bytes(), "file", 0o644)
              for p in sorted((repo / ".git" / "objects").rglob("*")) if p.is_file()]
    items += [(b"repo/.git/" + r, d, "file", 0o644) for r, d in refs(c1).items()]
    items += [(b"repo/" + p, d, t, m) for p, d, t, m in worktree]
    stream = b"".join(_frame(*i) for i in items) + walkframe.encode_terminal(len(items), sum(len(i[1]) for i in items))
    work = tmp_path / "work"
    work.mkdir()
    importer.make_skeleton(work, "sha1")
    r, w = os.pipe()
    try:
        rc = importer.run(["--git", GIT, "--session", SESSION, "--object-format", "sha1",
                           "--base-oid", _git("rev-parse", "main", cwd=base).decode().strip(),
                           "--max-bytes", str(1 << 30), "--max-input-size", str(1 << 30)],
                          stdin=io.BytesIO(stream), result_fd=w, work=work)
    finally:
        os.close(w)
    with os.fdopen(r, "rb") as fh:
        result = canon.parse(fh.read().rstrip(b"\n"))
    genv = importer.git_env(work)
    listing = _git("for-each-ref", "--format=%(refname) %(objectname)", env=genv).decode()
    return rc, result, dict(line.split(" ") for line in listing.splitlines()), genv, c1


CLEAN = [(b"README", b"base\n", "file", 0o644), (b"a.txt", b"a\n", "file", 0o644)]


@pytest.mark.parametrize("loose_a", [False, True])
def test_a_branch_that_is_a_directory_of_another_is_refused_by_name(tmp_path, loose_a):
    def refs(c1):
        line = c1.encode()
        if loose_a:
            return {b"refs/heads/a": line + b"\n", b"packed-refs": line + b" refs/heads/a/b\n"}
        return {b"packed-refs": line + b" refs/heads/a\n" + line + b" refs/heads/a/b\n"}
    rc, result, got, _env, _c1 = _run(tmp_path, refs, CLEAN)
    assert (rc, result["refused"]) == (1, "ref-conflict:refs/heads/a"), result
    assert got == {}


@pytest.mark.parametrize("extra, refused", [
    ([], None),
    ([(".g‌it/x".encode(), b"x\n", "file", 0o644)], "wip-path-refused:.g\\xe2\\x80\\x8cit/x"),
])
def test_the_wip_keeps_modes_and_names_a_path_update_index_drops(tmp_path, extra, refused):
    worktree = [(b"README", b"base\n", "file", 0o644), (b"a.txt", b"a\n", "file", 0o755),
                (b"ln", b"a.txt", "link", 0o755)] + extra
    rc, result, got, genv, c1 = _run(tmp_path, lambda c1: {b"HEAD": b"ref: refs/heads/main\n",
                                                           b"refs/heads/main": c1.encode() + b"\n"}, worktree)
    wip = f"refs/levain/{SESSION}/wip"
    if refused is not None:
        assert (rc, result["refused"]) == (1, refused), result
        assert wip not in got
        return
    assert (rc, result["refused"]) == (0, None), result
    tree = _git("ls-tree", "-z", got[wip], env=genv).split(b"\0")
    modes = {rec.split(b"\t")[1]: rec.split(b" ")[0] for rec in tree if rec}
    assert modes == {b"README": b"100644", b"a.txt": b"100755", b"ln": b"120000"}
    assert _git("rev-parse", got[wip] + "^", env=genv).decode().strip() == c1
    assert _git("cat-file", "blob", f"{got[wip]}:ln", env=genv) == b"a.txt"
