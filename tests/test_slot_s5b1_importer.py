"""S1 session 5 unit 5b1: the importer's ingestion (steps 0-2).

``test_a_pack_whose_links_are_in_a_later_pack_ingests``: reproduced in 5b1's planning probe (``probe_multipack.out``):
``index-pack --stdin --strict`` of an honest incremental pack, into a store that does not yet hold the older pack its
objects name, dies ``did not receive expected object``; a session repo with two packs met it in pack-name order.
``test_no_terminal_frame_starts_no_git``: the 5b1 DONE row "no terminal = exit non-zero with no git process started".
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
ENV = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": "/nonexistent",
       "LC_ALL": "C", "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@a", "GIT_COMMITTER_NAME": "a",
       "GIT_COMMITTER_EMAIL": "a@a"}


def _git(*args, cwd=None):
    return subprocess.run([GIT, *args], cwd=cwd, env=ENV, check=True, capture_output=True).stdout.decode().strip()


def _file(path: bytes, data: bytes) -> bytes:
    return walkframe.encode_header(path, "file", 0o644, len(data), "sha256:" + hashlib.sha256(data).hexdigest()) + data


def _session(tmp_path):
    """A base bundle, and a session repo whose newest pack names objects only its older pack holds."""
    base = tmp_path / "base"
    _git("init", "-q", "-b", "main", str(base))
    (base / "README").write_text("base\n")
    _git("add", "README", cwd=base)
    _git("commit", "-qm", "base", cwd=base)
    bundle = tmp_path / "base.bundle"
    _git("bundle", "create", str(bundle), "HEAD", "main", cwd=base)
    repo = tmp_path / "repo"
    _git("clone", "-q", str(bundle), str(repo))
    packs = []
    for name in ("one", "two"):
        before = set((repo / ".git/objects/pack").glob("*.pack"))
        (repo / name).write_text(name + "\n")
        _git("add", name, cwd=repo)
        _git("commit", "-qm", name, cwd=repo)
        _git("repack", "-q", cwd=repo)
        packs += list(set((repo / ".git/objects/pack").glob("*.pack")) - before)
    return bundle, _git("rev-parse", "main", cwd=base), repo, packs


def _run(tmp_path, stream: bytes, base_oid: str):
    work = tmp_path / "work"
    work.mkdir()
    importer.make_skeleton(work, "sha1")
    log = tmp_path / "git.log"
    r, w = os.pipe()
    try:
        rc = importer.run(["--git", GIT, "--object-format", "sha1", "--base-oid", base_oid, "--max-bytes", str(1 << 30),
                           "--max-input-size", str(1 << 30), "--git-log", str(log)],
                          stdin=io.BytesIO(stream), result_fd=w, work=work)
    finally:
        os.close(w)
    with os.fdopen(r, "rb") as fh:
        result = canon.parse(fh.read().rstrip(b"\n"))
    return rc, result, log.read_text().splitlines() if log.exists() else [], work


def test_a_pack_whose_links_are_in_a_later_pack_ingests(tmp_path):
    bundle, base_oid, repo, (older, newer) = _session(tmp_path)
    frames = [_file(b"in/repo.bundle", bundle.read_bytes()),
              _file(b"repo/.git/objects/pack/a-newer.pack", newer.read_bytes()),     # indexed first
              _file(b"repo/.git/objects/pack/b-older.pack", older.read_bytes())]
    stream = b"".join(frames) + walkframe.encode_terminal(3, sum(len(f) for f in frames) - 0)
    total = bundle.stat().st_size + newer.stat().st_size + older.stat().st_size
    stream = b"".join(frames) + walkframe.encode_terminal(3, total)
    rc, result, gitlog, work = _run(tmp_path, stream, base_oid)
    assert (rc, result["refused"]) == (0, None), result
    head = _git("rev-parse", "HEAD", cwd=repo)
    got = subprocess.run([GIT, "cat-file", "-t", head], env=importer.git_env(work), capture_output=True, text=True)
    assert got.stdout.strip() == "commit"
    assert sum(" index-pack --stdin --fsck-objects " in line for line in gitlog) == 3


def test_no_terminal_frame_starts_no_git(tmp_path):
    bundle, base_oid, _repo, _packs = _session(tmp_path)
    rc, result, gitlog, _work = _run(tmp_path, _file(b"in/repo.bundle", bundle.read_bytes()), base_oid)
    assert rc != 0
    assert (result["refused"], result["terminal"], result["enospc"]) == ("frame-bad:terminal", False, False)
    assert gitlog == []
