"""The page driver's wait for a reloaded document (tests/_reload_wait.mjs): its resend decisions, run in node against stub pages on a virtual
clock (tests/_reload_wait.test.mjs). No Chrome and no real time: a slow load that committed must never be reloaded again; a reload that never
committed is sent once more, on time, with a bound of its own; a resend that is refused or unanswered fails the step by name."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
TESTS = Path(__file__).parent


@pytest.mark.skipif(NODE is None, reason="node is needed")
def test_the_reload_wait_decisions_hold_against_stub_pages() -> None:
    r = subprocess.run([str(NODE), "--test", str(TESTS / "_reload_wait.test.mjs")], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, (r.stdout + r.stderr)[-4000:]
    assert "# fail 0" in r.stdout or "ℹ fail 0" in r.stdout, r.stdout[-2000:]
    assert "# pass 0" not in r.stdout and "ℹ pass 0" not in r.stdout, "no test ran"


@pytest.mark.skipif(NODE is None, reason="node is needed")
def test_the_drive_evidence_block_carries_every_field_and_stays_bounded() -> None:
    """The passive evidence record the driver writes into a timeout dump (tests/_drive_evidence.mjs), run in node against a stub event stream:
    dialog timestamps and Chrome's reply to each, the request ring with a failed script and its errorText, window.__loadErrors, the shell-cache
    entries, the resend count, and a size bound under a flood."""
    r = subprocess.run([str(NODE), "--test", str(TESTS / "_drive_evidence.test.mjs")], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, (r.stdout + r.stderr)[-4000:]
    assert "# fail 0" in r.stdout or "ℹ fail 0" in r.stdout, r.stdout[-2000:]
    assert "# pass 0" not in r.stdout and "ℹ pass 0" not in r.stdout, "no test ran"
