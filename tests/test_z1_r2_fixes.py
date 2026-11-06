"""Z1 L3 r2 fixes: a short os.write is looped to completion, and the P2 race sample must reach its planned size."""
from __future__ import annotations

import os

from levain import seat_canary, seat_lease


def test_write_all_survives_short_writes(tmp_path, monkeypatch):
    real = os.write
    calls = []

    def short(fd, data):
        calls.append(len(data))
        return real(fd, bytes(data)[:3])        # never more than 3 bytes per call

    monkeypatch.setattr(seat_lease.os, "write", short)
    p = tmp_path / "f"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT, 0o600)
    seat_lease._write_all(fd, b"0123456789")
    os.close(fd)
    assert p.read_bytes() == b"0123456789" and len(calls) == 4


def test_write_all_refuses_a_zero_write(tmp_path, monkeypatch):
    monkeypatch.setattr(seat_lease.os, "write", lambda fd, data: 0)
    fd = os.open(tmp_path / "f", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        seat_lease._write_all(fd, b"x")
    except OSError:
        pass
    else:
        raise AssertionError("a zero-byte write must raise")
    finally:
        os.close(fd)


def test_p2_cannot_pass_on_a_short_sample():
    ev = {"expected": 20, "attempts": 1, "prompted": 0, "landed": 0, "flips": 50, "flipper_alive": True,
          "flip_end": "stop", "control": True}
    assert seat_canary._p2(ev).status == "inconclusive"
    assert seat_canary._p2({**ev, "attempts": 20}).status == "pass"
