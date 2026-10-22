"""adopt's walker: V7's wall holds with a reader that keeps up, and the opened file is the manifest's."""
from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path

import pytest

from levain.slot import Refused, end, walker, walkframe

BUNDLE = b"# v2 git bundle\n" + b"\x00" * 64


def _retained(tmp_path: Path) -> tuple[Path, Path]:
    t = tmp_path / "tree"
    (t / "repo" / ".git").mkdir(parents=True)
    (t / "repo" / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main\n")
    (t / "repo" / "a.txt").write_bytes(b"alpha\n" * 1000)
    (t / "in").mkdir()
    (t / "in" / "repo.bundle").write_bytes(BUNDLE)
    mf = tmp_path / "manifest.jsonl"
    end._stream_jsonl(mf, end.iter_manifest(t, caps=end.Caps(depth=end.MANIFEST_DEPTH)), 1 << 40, "x")
    return t, mf


def _walk(tree: Path, mf: Path, *, deadline: float, seams: walker.WalkSeams,
          depth: int = end.MANIFEST_DEPTH) -> tuple[Exception | None, bytes]:
    r, w = os.pipe()
    got = bytearray()

    def drain() -> None:
        while b := os.read(r, 1 << 16):
            got.extend(b)

    th = threading.Thread(target=drain, daemon=True)
    th.start()
    err = None
    try:
        walker.walk_frames(tree, mf, w, depth=depth,
                           caps=walker.WalkCaps(bytes=1 << 30, entries=1000, deadline=deadline),
                           base_bundle_sha256="sha256:" + hashlib.sha256(BUNDLE).hexdigest(), seams=seams)
    except Refused as e:
        err = e
    finally:
        os.close(w)
    th.join(30)
    os.close(r)
    return err, bytes(got)


def _has_terminal(stream: bytes) -> bool:
    import io
    fh = io.BytesIO(stream)
    try:
        for header, _ in walkframe.iter_frames(fh):
            if "end" in header:
                return True
    except Refused:
        return False
    return False


def test_the_wall_holds_when_the_importer_keeps_up(tmp_path: Path) -> None:
    # Reproduced on 6a4d3aa9: the deadline passing inside the last file's between_passes, with a reader that
    # drains, still wrote the terminal frame (fix_run I3).
    tree, mf = _retained(tmp_path)

    def slow(e: end.Entry, fd: int) -> None:
        if e.path == b"repo/a.txt":
            time.sleep(0.6)

    err, got = _walk(tree, mf, deadline=time.monotonic() + 0.3,
                     seams=walker.WalkSeams(owner_uid=os.getuid(), between_passes=slow))
    assert err is not None and err.name == "walker-cap:time"
    assert not _has_terminal(got)


@pytest.mark.parametrize("change", ["link-after-lstat", "mode-between-passes"])
def test_the_opened_file_is_checked_against_the_manifest(tmp_path: Path, change: str) -> None:
    # Reproduced on 6a4d3aa9: a hard link added after the walk's lstat, or a mode change between the two passes,
    # streamed the file and wrote the terminal frame (fix_run I5a, I5c).
    tree, mf = _retained(tmp_path)
    seams = walker.WalkSeams(owner_uid=os.getuid())
    if change == "link-after-lstat":
        def walk(root, **kw):
            for e in end.walk(root, **kw):
                if e.path == b"repo/a.txt":
                    os.link(root / "repo" / "a.txt", tmp_path / "outside")
                yield e
        seams.walk = walk
    else:
        seams.between_passes = lambda e, fd: os.fchmod(fd, 0o666) if e.path == b"repo/a.txt" else None
    err, got = _walk(tree, mf, deadline=time.monotonic() + 60, seams=seams)
    assert err is not None and err.name == "walk-manifest-mismatch"
    assert not _has_terminal(got)


def test_a_path_past_the_decoders_bound_refuses_before_the_first_frame(tmp_path: Path) -> None:
    # Reproduced on eb6931ea (s5-5a-fix2 fail_before.txt item 2): a directory MAX_DEPTH + 1 components deep was
    # framed after the earlier frames went out, and only the decoder refused it (frame-bad:path_b64).
    tree, _ = _retained(tmp_path)
    fd = os.open(tree / "repo", os.O_RDONLY | os.O_DIRECTORY)
    for _ in range(walkframe.MAX_DEPTH):
        os.mkdir("d", dir_fd=fd)
        nfd = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
        os.close(fd)
        fd = nfd
    os.close(fd)
    depth = walkframe.MAX_DEPTH + 8
    mf = tmp_path / "deep.jsonl"
    end._stream_jsonl(mf, end.iter_manifest(tree, caps=end.Caps(depth=depth)), 1 << 40, "x")
    err, got = _walk(tree, mf, deadline=time.monotonic() + 60, seams=walker.WalkSeams(owner_uid=os.getuid()),
                     depth=depth)
    assert err is not None and err.name == "frame-bad:path_b64"
    assert got == b""


def test_a_lock_taken_past_the_deadline_is_released_and_refused(tmp_path: Path) -> None:
    # Reproduced on eb6931ea (s5-5a-fix2 fail_before.txt item 1): a sleep that overshot V7's deadline while the
    # holder released let flock_until take the adopt lock late and return True.
    import fcntl
    import types

    from levain.slot import runtime_helper as RH

    holder = os.open(tmp_path / "adopt.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    fd = os.open(tmp_path / "adopt.lock", os.O_RDWR)
    now = [100.0]

    def sleep(s: float) -> None:
        now[0] += s + 1.0
        fcntl.flock(holder, fcntl.LOCK_UN)

    try:
        assert RH.flock_until(fd, 100.05, types.SimpleNamespace(monotonic=lambda: now[0], sleep=sleep)) is False
        assert RH.try_flock(holder)   # the late acquisition was let go
    finally:
        os.close(fd)
        os.close(holder)


def test_the_preflight_refuses_at_its_first_clock_read_past_the_deadline(tmp_path: Path, monkeypatch) -> None:
    # Reproduced on afbcd383 (s5-5a-fix3 fail_before.txt item 1): with the deadline passed after the first clock
    # read, the preflight checked all 5006 manifest records without reading the clock again.
    tree, _ = _retained(tmp_path)
    (tree / "repo" / "many").mkdir()
    for i in range(5000):
        (tree / "repo" / "many" / f"f{i:05d}").write_bytes(b"")
    mf = tmp_path / "many.jsonl"
    end._stream_jsonl(mf, end.iter_manifest(tree, caps=end.Caps(depth=end.MANIFEST_DEPTH)), 1 << 40, "x")
    reads, checked = [0], [0]

    def clock() -> float:
        reads[0] += 1
        return 0.0 if reads[0] == 1 else 100.0

    real = walkframe.check_path_bound
    monkeypatch.setattr(walkframe, "check_path_bound", lambda p: (checked.__setitem__(0, checked[0] + 1), real(p)))
    err, got = _walk(tree, mf, deadline=10.0, seams=walker.WalkSeams(owner_uid=os.getuid(), clock=clock))
    assert err is not None and err.name == "walker-cap:time"
    assert checked[0] == walker._MANIFEST_CLOCK_EVERY and reads[0] == 2
    assert got == b""


def test_a_pruned_subtree_does_not_outrun_the_manifest_clock(tmp_path: Path, monkeypatch) -> None:
    # Reproduced on 7cc29d62 (s5-5a-fix4 fail_before.txt): an OUT directory's 9000 descendants were consumed by
    # skip_under, which never read the clock, so the preflight read all 9006 records past the deadline.
    from levain.contracts import canon
    tree, _ = _retained(tmp_path)
    (tree / "aaa").mkdir()
    for i in range(9000):
        (tree / "aaa" / f"f{i:05d}").write_bytes(b"")
    mf = tmp_path / "out.jsonl"
    end._stream_jsonl(mf, end.iter_manifest(tree, caps=end.Caps(depth=end.MANIFEST_DEPTH)), 1 << 40, "x")
    reads, parsed = [0], [0]

    def clock() -> float:
        reads[0] += 1
        return 0.0 if reads[0] == 1 else 100.0

    real = canon.parse
    monkeypatch.setattr(canon, "parse", lambda b: (parsed.__setitem__(0, parsed[0] + 1), real(b))[1])
    err, got = _walk(tree, mf, deadline=10.0, seams=walker.WalkSeams(owner_uid=os.getuid(), clock=clock))
    assert err is not None and err.name == "walker-cap:time"
    assert parsed[0] == walker._MANIFEST_CLOCK_EVERY and reads[0] == 2
    assert got == b""

def test_the_encoder_refuses_a_path_its_decoder_refuses() -> None:
    # Reproduced on afbcd383 (s5-5a-fix3 fail_before.txt item 2): encode_header emitted a frame for a
    # MAX_DEPTH + 1 component path that read_header refused as frame-bad:path_b64.
    import io
    path = b"/".join([b"d"] * (walkframe.MAX_DEPTH + 1))
    with pytest.raises(Refused) as enc:
        walkframe.encode_header(path, "dir", 0o755, 0, "")
    header = {"v": 1, "path_b64": walkframe.b64(path), "type": "dir", "mode": 0o755, "size": 0, "digest": ""}
    with pytest.raises(Refused) as dec:
        walkframe.read_header(io.BytesIO(walkframe._header_bytes(header)))
    assert enc.value.name == dec.value.name == "frame-bad:path_b64"
