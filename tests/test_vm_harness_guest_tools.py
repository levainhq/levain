"""The guest-tools copy into the profile's tools dir (/tmp/fire), against a fake exec. No VM, no tart, no ssh."""
from __future__ import annotations

import subprocess

import pytest

from scripts.vm_harness import guest_tools
from scripts.vm_harness.host_profile import DARWIN, LINUX

TART = "/fake/tart"


class FakeRun:
    def __init__(self, rc=0):
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.rc = rc

    def __call__(self, argv, **kw):
        assert isinstance(argv, list) and kw.get("shell") is not True
        self.calls.append((list(argv), kw.get("input")))
        return subprocess.CompletedProcess(argv, self.rc, b"", b"boom")


@pytest.fixture
def src(tmp_path):
    for n in guest_tools.GUEST_TOOLS:
        (tmp_path / n).write_bytes(f"# {n}\n".encode())
    return tmp_path


@pytest.fixture(autouse=True)
def _tart(monkeypatch):
    monkeypatch.setattr(guest_tools, "TART", TART)


def expected(profile, src):
    d = profile.tools_dir
    out = [([TART, "exec", "vend-x", "sudo", "-n", "/bin/mkdir", "-p", d], None)]
    for n in guest_tools.GUEST_TOOLS:
        out.append(([TART, "exec", "-i", "vend-x", "sudo", "-n", "/usr/bin/tee", f"{d}/{n}"], (src / n).read_bytes()))
    out.append(([TART, "exec", "vend-x", "sudo", "-n", "/bin/chmod", "-R", "u=rwX,go=rX", d], None))
    return out


def test_tool_list_is_the_three_the_page_names():
    assert guest_tools.GUEST_TOOLS == ("guest_ops.py", "guest_dispatch.py", "host_profile.py")


@pytest.mark.parametrize("profile", [DARWIN, LINUX])
def test_exact_argv_lists(src, profile):
    run = FakeRun()
    guest_tools.copy_guest_tools("vend-x", profile, src, run=run)
    assert run.calls == expected(profile, src)


def test_idempotent_second_run_is_identical(src):
    run = FakeRun()
    guest_tools.copy_guest_tools("vend-x", DARWIN, src, run=run)
    guest_tools.copy_guest_tools("vend-x", DARWIN, src, run=run)
    n = len(run.calls) // 2
    assert run.calls[:n] == run.calls[n:] == expected(DARWIN, src)


def test_missing_source_fails_loudly_before_touching_the_guest(src):
    (src / "guest_dispatch.py").unlink()
    run = FakeRun()
    with pytest.raises(FileNotFoundError, match="guest_dispatch.py"):
        guest_tools.copy_guest_tools("vend-x", DARWIN, src, run=run)
    assert run.calls == []


def test_a_failed_exec_raises_with_the_step(src):
    with pytest.raises(RuntimeError, match="mkdir"):
        guest_tools.copy_guest_tools("vend-x", DARWIN, src, run=FakeRun(rc=1))


def test_only_vend_clones_are_addressed(src):
    run = FakeRun()
    with pytest.raises(SystemExit, match="vend-"):
        guest_tools.copy_guest_tools("macos-base", DARWIN, src, run=run)
    assert run.calls == []
