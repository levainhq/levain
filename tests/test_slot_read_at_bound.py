"""read_at never reads more than cap + 1 bytes, even when the file grows after its fstat (row 33 R1 L3 r2, R-h)."""
from __future__ import annotations

import os

import pytest

from levain.slot import runtime_helper as RH


def test_a_record_that_grows_after_fstat_is_read_to_cap_plus_one_at_most(tmp_path, monkeypatch):
    cap = 100
    (tmp_path / "rec").write_bytes(b"x" * (cap + 70_000))
    real_fstat, real_read = os.fstat, os.read
    asked: list[int] = []

    class _Small:
        def __init__(self, st):
            self.st_mode, self.st_size = st.st_mode, 0      # the size fstat saw before the file grew

    monkeypatch.setattr(RH.os, "fstat", lambda fd: _Small(real_fstat(fd)))
    monkeypatch.setattr(RH.os, "read", lambda fd, n: (asked.append(n), real_read(fd, n))[1])
    dirfd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(RH.Refused) as e:
            RH.read_at(dirfd, "rec", "the record", cap=cap)
    finally:
        os.close(dirfd)
    assert e.value.name == "record-too-large"
    assert sum(asked) <= cap + 1, asked
