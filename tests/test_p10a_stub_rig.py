"""The P10a rig's stub ``claude`` (levain#121, L0): the install fact ``claude_bin`` points at a stub, and the stub is
what the dream's fire runs."""
from __future__ import annotations

import shutil
import time

import pytest

pytest.importorskip("cryptography")
from tests._p10a_rig import Install  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


def test_install_fact_claude_bin_is_the_stub_after_enable(tmp_path):
    from levain.dream import agent as da

    inst = Install(tmp_path, stub=True)
    assert inst.enable("--time", "00:00").rc == 0
    assert da.read_install(inst.store_dir).claude_bin == str(inst.stub.path)
    assert inst.stub.calls() == 0


def test_a_fire_runs_the_stub_and_nothing_else(tmp_path):
    inst = Install(tmp_path, stub=True)
    assert inst.enable("--time", "00:00").rc == 0
    assert inst.sign(inst.link_doc()).rc == 0
    started = inst.cli("dream", "start", spawn_gate=False)
    assert started.rc == 0, started.text
    deadline = time.monotonic() + 90
    while inst.stub.calls() == 0 and time.monotonic() < deadline:
        time.sleep(0.5)
    assert inst.stub.calls() >= 1, started.text
    argv = inst.stub.argv(1)
    assert argv[:2] == ["-p", "--output-format"] or "-p" in argv
    assert "json" in argv


def test_default_install_has_no_stub(tmp_path):
    from levain.dream import agent as da

    inst = Install(tmp_path)
    assert inst.stub is None
    assert inst.enable("--time", "00:00").rc == 0
    assert da.read_install(inst.store_dir).claude_bin in (None, "claude")
