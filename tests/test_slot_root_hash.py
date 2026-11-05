"""1105-7 window 2 (DESIGN_window2 §5 item 7): ``start.hash_worktree_at``, root's own fd-relative hash of the clone's
worktree on macOS. It is host-independent and pure, and every accepted case is compared with real
``git hash-object --no-filters`` so the test pins the hasher to git, not to a re-statement of the algorithm.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from levain.slot import start as SS

UID = os.getuid()


def git_id(path, cwd) -> str:
    return subprocess.run(["git", "hash-object", "--no-filters", "--", path], capture_output=True, check=True,
                          cwd=cwd).stdout.decode().strip()


@pytest.fixture
def tree(tmp_path):
    """``(repo_fd, repo_dir)``; the fd is closed afterwards."""
    repo = tmp_path / "repo"
    repo.mkdir()
    fd = os.open(repo, os.O_RDONLY | os.O_DIRECTORY)
    yield fd, repo
    os.close(fd)


def put(repo, rel, data=b"x\n", mode=0o644):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    p.chmod(mode)
    return rel


def entry(repo, rel, mode="100644", sha=None):
    return SS.IndexEntry(mode, sha if sha is not None else git_id(rel, repo), 0, rel)


def hash_of(tree, entries, **kw):
    return SS.hash_worktree_at(tree[0], entries, kw.pop("owner", UID), **kw)


# --- accepted shapes, each equal to real git ---------------------------------------------------------------------

def test_empty_file(tree):
    _, repo = tree
    put(repo, "e", b"")
    assert hash_of(tree, [entry(repo, "e")]) == 0


def test_crlf_bytes_are_not_converted(tree):
    _, repo = tree
    put(repo, "c.txt", b"hello\r\nworld")
    assert hash_of(tree, [entry(repo, "c.txt")]) == 12


def test_ident_and_eol_attributes_are_not_applied(tree):
    """``* ident`` and ``eol=crlf`` committed as the repo's own attributes: ``--no-filters`` ignores them, and so does
    root."""
    _, repo = tree
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    put(repo, ".gitattributes", b"* ident\n*.txt eol=crlf\n")
    put(repo, "id.txt", b"$Id$\nline\n")
    ents = [entry(repo, ".gitattributes"), entry(repo, "id.txt")]
    assert hash_of(tree, ents) == sum(len(x) for x in (b"* ident\n*.txt eol=crlf\n", b"$Id$\nline\n"))


def test_executable_file(tree):
    _, repo = tree
    put(repo, "run.sh", b"#!/bin/sh\n", 0o755)
    assert hash_of(tree, [entry(repo, "run.sh", "100755")]) == 10


def test_file_larger_than_the_read_chunk(tree, monkeypatch):
    _, repo = tree
    monkeypatch.setattr(SS, "HASH_CHUNK", 1024)
    put(repo, "big", os.urandom(5000))
    assert hash_of(tree, [entry(repo, "big")]) == 5000


def test_default_chunk_size_file_spanning_two_chunks(tree):
    _, repo = tree
    put(repo, "big", os.urandom(SS.HASH_CHUNK + 17))
    assert hash_of(tree, [entry(repo, "big")]) == SS.HASH_CHUNK + 17


def test_unicode_and_space_in_names(tree):
    _, repo = tree
    names = ["sp ace/ü ñ.txt", "日本語/ファイル.txt"]
    for n in names:
        put(repo, n, n.encode())
    assert hash_of(tree, [entry(repo, n) for n in names]) == sum(len(n.encode()) for n in names)


def test_a_1023_byte_nested_path(tree):
    """Longer than the host's PATH_MAX from the repo's absolute path, so the tree is built and read fd-relative, and
    its id is real git's over the same bytes."""
    fd, repo = tree
    parts = [c * 200 for c in "pqrs"] + ["f".ljust(1023 - 4 * 201, "x")]
    rel = "/".join(parts)
    assert len(rel) == 1023
    cur = os.dup(fd)
    for comp in parts[:-1]:
        os.mkdir(comp, dir_fd=cur)
        nxt = os.open(comp, os.O_RDONLY | os.O_DIRECTORY, dir_fd=cur)
        os.close(cur)
        cur = nxt
    leaf = os.open(parts[-1], os.O_WRONLY | os.O_CREAT, 0o644, dir_fd=cur)
    os.write(leaf, b"deep\n")
    os.close(leaf)
    os.close(cur)
    sha = subprocess.run(["git", "hash-object", "--no-filters", "--stdin"], input=b"deep\n", capture_output=True,
                         check=True, cwd="/").stdout.decode().strip()
    assert hash_of(tree, [SS.IndexEntry("100644", sha, 0, rel)]) == 5


def test_a_different_blob_id_is_a_mismatch_naming_both(tree):
    _, repo = tree
    put(repo, "a", b"one\n")
    with pytest.raises(SS.CheckFailed, match=r"blob 0{12} != worktree --no-filters [0-9a-f]{12}"):
        hash_of(tree, [entry(repo, "a", sha="0" * 40)])


def test_header_includes_the_nul(tree):
    """sha1("blob 4" + "one\\n") without the NUL is not the git id; git's id is the answer."""
    import hashlib
    _, repo = tree
    put(repo, "a", b"one\n")
    wrong = hashlib.sha1(b"blob 4" + b"one\n").hexdigest()
    assert wrong != git_id("a", repo)
    with pytest.raises(SS.CheckFailed, match="!= worktree"):
        hash_of(tree, [entry(repo, "a", sha=wrong)])


# --- refusals ----------------------------------------------------------------------------------------------------

def test_symlink_leaf_refuses(tree):
    _, repo = tree
    put(repo, "real", b"r\n")
    os.symlink("real", repo / "link")
    with pytest.raises(SS.CheckFailed, match="no plain worktree file"):
        hash_of(tree, [entry(repo, "link", sha=git_id("real", repo))])


def test_symlink_parent_refuses(tree, tmp_path):
    _, repo = tree
    other = tmp_path / "other"
    other.mkdir()
    (other / "f").write_bytes(b"f\n")
    os.symlink(other, repo / "d")
    with pytest.raises(SS.CheckFailed, match="parent 'd' is not a plain directory"):
        hash_of(tree, [SS.IndexEntry("100644", git_id("f", other), 0, "d/f")])


def test_fifo_refuses_without_blocking(tree):
    _, repo = tree
    os.mkfifo(repo / "fifo")
    import signal

    def alarm(*_):
        raise AssertionError("the FIFO open blocked")
    old = signal.signal(signal.SIGALRM, alarm)
    signal.alarm(10)
    try:
        with pytest.raises(SS.CheckFailed, match="not a regular file"):
            hash_of(tree, [SS.IndexEntry("100644", "0" * 40, 0, "fifo")])
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def test_directory_at_the_path_refuses(tree):
    _, repo = tree
    (repo / "d").mkdir()
    with pytest.raises(SS.CheckFailed, match="not a regular file"):
        hash_of(tree, [SS.IndexEntry("100644", "0" * 40, 0, "d")])


def test_hardlink_refuses(tree):
    _, repo = tree
    put(repo, "a", b"a\n")
    os.link(repo / "a", repo / "b")
    with pytest.raises(SS.CheckFailed, match="2 links"):
        hash_of(tree, [entry(repo, "a")])


def test_wrong_owner_refuses(tree):
    _, repo = tree
    put(repo, "a", b"a\n")
    with pytest.raises(SS.CheckFailed, match="owned by uid"):
        hash_of(tree, [entry(repo, "a")], owner=UID + 1)


def test_exec_bit_mismatch_refuses(tree):
    _, repo = tree
    put(repo, "a", b"a\n", 0o755)
    with pytest.raises(SS.CheckFailed, match="exec bit"):
        hash_of(tree, [entry(repo, "a", "100644")])
    put(repo, "b", b"b\n", 0o644)
    with pytest.raises(SS.CheckFailed, match="exec bit"):
        hash_of(tree, [entry(repo, "b", "100755")])


def test_owner_unreadable_refuses(tree, monkeypatch):
    """L3_win2_r1 codex #2: root reads past permissions, the slot's vendor does not. The test runs unprivileged, where
    a mode-000 open already fails, so ``fstat`` answers the mode root would see on such a file it could open."""
    _, repo = tree
    put(repo, "a", b"a\n")
    ents = [entry(repo, "a")]
    real = os.fstat

    def fstat(fd):
        st = list(real(fd))
        st[0] &= ~0o400
        return os.stat_result(st)
    monkeypatch.setattr(os, "fstat", fstat)
    with pytest.raises(SS.CheckFailed, match="not readable by its owner"):
        hash_of(tree, ents)


def test_a_mode_outside_the_blob_modes_is_check_failed(tree):
    _, repo = tree
    put(repo, "a", b"a\n")
    with pytest.raises(SS.CheckFailed, match="not a blob mode"):
        hash_of(tree, [entry(repo, "a", "100664")])


@pytest.mark.parametrize("seam", ["dup", "fstat", "read"])
def test_an_oserror_is_check_failed(tree, monkeypatch, seam):
    """L3_win2_r1 gemini LOW: every failure of the pass is ``CheckFailed`` (clone_check's contract)."""
    _, repo = tree
    put(repo, "d/a", b"a\n")
    ents = [entry(repo, "d/a")]

    def eio(*_a, **_k):
        raise OSError(5, "EIO")
    kw = {}
    if seam == "read":
        kw["read"] = eio
    else:
        monkeypatch.setattr(os, seam, eio)
    with pytest.raises(SS.CheckFailed, match="root hash failed \\(errno 5\\)"):
        hash_of(tree, ents, **kw)


def test_absent_file_refuses(tree):
    with pytest.raises(SS.CheckFailed, match="no plain worktree file"):
        hash_of(tree, [SS.IndexEntry("100644", "0" * 40, 0, "nope")])


@pytest.mark.parametrize("bad", ["", "a//b", "./a", "a/../b", "a/."])
def test_not_a_plain_relative_path_refuses(tree, bad):
    with pytest.raises(SS.CheckFailed, match="not a plain relative path"):
        hash_of(tree, [SS.IndexEntry("100644", "0" * 40, 0, bad)])


def _reading(real, delta):
    """A read seam: the file's bytes with ``delta`` more (>0: that many extra bytes after EOF) or fewer (<0: each
    chunk cut) than ``st_size``."""
    sent = {"extra": False}

    def read(fd, n):
        data = real(fd, n)
        if delta > 0:
            if not data and not sent["extra"]:
                sent["extra"] = True
                return b"z" * delta
            return data
        return data[:delta] if data else data
    return read


def test_a_file_that_grew_refuses(tree):
    _, repo = tree
    put(repo, "a", b"abc\n")
    with pytest.raises(SS.CheckFailed, match="read 5 bytes for a size of 4"):
        hash_of(tree, [entry(repo, "a")], read=_reading(os.read, 1))


def test_a_file_that_shrank_refuses(tree):
    _, repo = tree
    put(repo, "a", b"abc\n")
    with pytest.raises(SS.CheckFailed, match="read 3 bytes for a size of 4"):
        hash_of(tree, [entry(repo, "a")], read=_reading(os.read, -1))


def test_deadline_exceeded_refuses(tree):
    _, repo = tree
    put(repo, "a", b"abc\n")
    ticks = iter([0.0, 5.0, 5.0, 5.0])
    with pytest.raises(SS.CheckFailed, match="passed its 1s bound"):
        hash_of(tree, [entry(repo, "a")], deadline_s=1.0, clock=lambda: next(ticks))


def test_fds_are_not_leaked(tree):
    _, repo = tree
    put(repo, "d/a", b"a\n")
    before = len(os.listdir("/dev/fd"))
    hash_of(tree, [entry(repo, "d/a")])
    with pytest.raises(SS.CheckFailed):
        hash_of(tree, [SS.IndexEntry("100644", "0" * 40, 0, "d/missing")])
    with pytest.raises(SS.CheckFailed):
        hash_of(tree, [SS.IndexEntry("100644", "0" * 40, 0, "d/a")])
    assert len(os.listdir("/dev/fd")) == before
