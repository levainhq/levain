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


def _walk(tree: Path, mf: Path, *, deadline: float, seams: walker.WalkSeams) -> tuple[Exception | None, bytes]:
    r, w = os.pipe()
    got = bytearray()

    def drain() -> None:
        while b := os.read(r, 1 << 16):
            got.extend(b)

    th = threading.Thread(target=drain, daemon=True)
    th.start()
    err = None
    try:
        walker.walk_frames(tree, mf, w, depth=end.MANIFEST_DEPTH,
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
