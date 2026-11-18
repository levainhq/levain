"""The P10a rig's stub ``claude`` (levain#121, L0): the install fact ``claude_bin`` points at a stub, and the stub is
what the dream's fire runs."""
from __future__ import annotations

import contextlib
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


def test_the_stub_looks_to_ps_the_way_the_real_claude_does(tmp_path):
    """A native ``claude`` shows ``ps`` its own path as argv[0] (measured on claude 2.1.327, 11-18, levain#131), so the recorded compose argv
    equals the live command line and recovery's identity check holds. The stub must show the same, never ``/bin/sh <stub>``."""
    import os
    import signal
    import subprocess

    from levain.consent.delivery import process_identity
    from levain.dream.recover import _identity_matches
    from tests._p10a_stub import StubClaude

    stub = StubClaude(tmp_path / "stubclaude")
    if not stub.native:
        pytest.skip("no C compiler: the stub is an sh script")
    stub.sleep(30)
    occ = tmp_path / "occ"
    occ.mkdir()
    argv = [str(stub.path), "-p", "--model", "m", "--add-dir", str(occ)]
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True)
    try:
        deadline = time.monotonic() + 30
        while stub.calls() == 0 and time.monotonic() < deadline:
            time.sleep(0.1)
        assert stub.calls() == 1
        ident = process_identity(proc.pid)
        assert ident is not None and ident[1] == " ".join(argv)
        assert _identity_matches(proc.pid, ident[0], argv, occ) is True
        other = list(argv)
        other[3] = "other"                                       # one argument differs: not the recorded command line
        assert _identity_matches(proc.pid, ident[0], other, occ) is False
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)


def test_the_launcher_runs_a_script_whose_path_has_a_quote_a_backslash_and_non_ascii(tmp_path):
    """The script path is written into the launcher as a C string literal: every byte outside plain ASCII, and the quote
    and the backslash, must survive it (json.dumps wrote non-BMP characters as a pair of escapes C rejects)."""
    import stat
    import subprocess

    from tests._p10a_stub import _build_launcher

    odd = tmp_path / 'd"q\\b é\U0001F600'
    odd.mkdir()
    script = odd / "claude.sh"
    script.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    if not _build_launcher(tmp_path / "launcher.c", tmp_path / "launcher", script):
        pytest.skip("no C compiler")
    assert subprocess.run([str(tmp_path / "launcher")], timeout=30).returncode == 3
