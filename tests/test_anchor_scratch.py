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
    d.mkdir(parents=True)
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


def test_legacy_top_level_sweep_only_three_prefixes_and_old(fake_home):
    old = [_mk(fake_home, f"{p}abc", 3 * 3600) for p in (".levain-confirm-keys-", ".levain-k2b-broker-", ".levain-t3-")]
    fresh = _mk(fake_home, ".levain-t3-new", 60)
    other = _mk(fake_home, ".levain-dbg-old", 3 * 3600)
    other2 = _mk(fake_home, ".levain-k8a-old", 3 * 3600)
    ck.anchor_scratch("t3-")
    assert not any(o.exists() for o in old)
    assert fresh.exists() and other.exists() and other2.exists()


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
