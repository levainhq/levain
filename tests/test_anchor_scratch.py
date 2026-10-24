"""The trust-anchor scratch helper: one swept parent, dead-pid sweep, legacy sweep limited to three prefixes."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests import autonomic_confirm_keys as ck


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(ck, "account_home", lambda: str(home))
    monkeypatch.setattr(ck, "_swept", set())
    return home


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _age(path: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


def _mk(parent: Path, name: str, age: float = 0) -> Path:
    d = parent / name
    if not parent.exists():
        parent.mkdir(mode=0o700, parents=True)
        parent.chmod(0o700)              # umask-free: the helper refuses a group/other-writable parent
    d.mkdir()
    (d / "k").write_text("x")
    _age(d, age)
    return d


def test_sweep_dead_old_kept_live_and_fresh(fake_home):
    parent = fake_home / ck.SCRATCH_PARENT
    dead = _dead_pid()
    old_dead = _mk(parent, f"t3-{dead}-abcd1234", 3600)
    fresh_dead = _mk(parent, f"t3-{dead}-efgh5678", 5)
    live_old = _mk(parent, f"t3-{os.getpid()}-ijkl9012", 3600)
    d = ck.anchor_scratch("t3-")
    assert d.parent == parent and d.name.startswith(f"t3-{os.getpid()}-")
    assert not old_dead.exists()
    assert fresh_dead.exists() and live_old.exists() and d.exists()


def test_legacy_top_level_sweep_only_listed_prefixes_and_old(fake_home):
    old = [_mk(fake_home, f"{p}abc", 25 * 3600) for p in (".levain-confirm-keys-", ".levain-k2b-broker-", ".levain-t3-", ".levain-k2b-crash-", ".levain-k8a-", ".levain-row12b-", ".levain-custody-broker-")]
    fresh = _mk(fake_home, ".levain-t3-new", 60)
    mid = _mk(fake_home, ".levain-t3-mid", 3 * 3600)     # under the 24h legacy bound: kept
    other = _mk(fake_home, ".levain-dbg-old", 25 * 3600)
    other2 = _mk(fake_home, ".levain-k8b-old", 25 * 3600)
    ck.anchor_scratch("t3-")
    assert not any(o.exists() for o in old)
    assert fresh.exists() and mid.exists() and other.exists() and other2.exists()


def test_out_of_range_and_zero_pid_entries_kept_and_anchor_still_works(fake_home):
    parent = fake_home / ck.SCRATCH_PARENT
    huge = _mk(parent, "x-99999999999999999999-abcd1234", 3600)
    zero = _mk(parent, "x-0-abcd1234", 3600)
    d = ck.anchor_scratch("t3-")
    assert huge.exists() and zero.exists() and d.exists()


def test_parent_without_access_refused(fake_home):
    p = fake_home / ck.SCRATCH_PARENT
    p.mkdir(mode=0o700)
    p.chmod(0o000)
    try:
        if os.access(p, os.R_OK | os.W_OK | os.X_OK):
            pytest.skip("running with privileges that ignore directory modes")
        with pytest.raises(pytest.skip.Exception):
            ck.anchor_scratch("t3-")
    finally:
        p.chmod(0o700)


def test_parent_created_private(fake_home):
    ck.anchor_scratch("t3-")
    assert ((fake_home / ck.SCRATCH_PARENT).stat().st_mode & 0o777) == 0o700


def test_sweep_runs_once_per_process(fake_home):
    ck.anchor_scratch("t3-")
    later = _mk(fake_home / ck.SCRATCH_PARENT, f"t3-{_dead_pid()}-abcd1234", 3600)
    ck.anchor_scratch("t3-")
    assert later.exists()


def test_symlinked_parent_refused(fake_home, tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    (fake_home / ck.SCRATCH_PARENT).symlink_to(real)
    with pytest.raises(pytest.skip.Exception):
        ck.anchor_scratch("t3-")
    assert list(real.iterdir()) == []


def test_group_writable_parent_refused(fake_home):
    p = fake_home / ck.SCRATCH_PARENT
    p.mkdir()
    p.chmod(0o770)
    with pytest.raises(pytest.skip.Exception):
        ck.anchor_scratch("t3-")
