"""levain#241 (ruling d241_v2): the `security` shim on the hands PATH. The shim text is written to tmp_path and RUN for
real, but only with verbs it answers itself (no keychain is reached) and with `list-keychains -d user`, which passes
through to /usr/bin/security and only reads the search list. Never a verb that could reach a keychain item."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from levain.firing import confinement, hands_login
from levain.firing.confinement import HANDS_PATH, HandsIdentity
from tests.test_hands_login import Rig, rig  # noqa: F401

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="POSIX shims")


@pytest.fixture
def shim(tmp_path: Path) -> Path:
    d = tmp_path / "shimdir"
    d.mkdir()
    p = d / "security"
    p.write_text(hands_login.SECURITY_SHIM_TEXT)
    p.chmod(0o755)
    return p


def _run(shim: Path, cwd: Path, *args: str, stdin: bytes = b""):
    return subprocess.run([str(shim), *args], input=stdin, cwd=cwd, capture_output=True, timeout=30,
                          env={"HOME": str(cwd), "PATH": "/usr/bin:/bin"})


def test_the_dash_i_form_drains_stdin_exits_36_and_writes_nothing(shim, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    r = _run(shim, work, "-i", stdin=b"x")
    assert r.returncode == 36 and r.stdout == b"" and r.stderr == b""
    # a large stdin is drained too (a writer is never left blocked on a pipe)
    r = _run(shim, work, "-i", stdin=b"add-generic-password -U -s s -a a -w secret\n" * 20000)
    assert r.returncode == 36 and r.stdout == b"" and r.stderr == b""
    assert list(work.iterdir()) == []                                      # nothing written anywhere
    assert sorted(p.name for p in shim.parent.iterdir()) == ["security"]


@pytest.mark.parametrize("verb", ["find-generic-password", "add-generic-password", "delete-generic-password"])
def test_the_three_item_verbs_exit_36_and_touch_nothing(shim, tmp_path, verb):
    work = tmp_path / "work"
    work.mkdir()
    r = _run(shim, work, verb, "-s", "Claude Code-credentials", "-w")
    assert r.returncode == 36 and r.stdout == b"" and r.stderr == b""
    assert list(work.iterdir()) == []


@pytest.mark.skipif(sys.platform != "darwin", reason="passes through to /usr/bin/security")
def test_any_other_verb_passes_through_to_the_real_program(shim, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    r = _run(shim, work, "list-keychains", "-d", "user")
    assert r.returncode == 0
    real = subprocess.run(["/usr/bin/security", "list-keychains", "-d", "user"], cwd=work, capture_output=True, timeout=30,
                          env={"HOME": str(work), "PATH": "/usr/bin:/bin"})
    assert r.stdout == real.stdout


def test_the_shim_text_has_no_logging_and_execs_the_absolute_program():
    t = hands_login.SECURITY_SHIM_TEXT
    assert t.startswith("#!/bin/sh\n") and 'exec /usr/bin/security "$@"' in t
    assert ">>" not in t and "tee" not in t and "logger" not in t          # the credential on stdin is never kept


# --- the PATH -------------------------------------------------------------------------------------------------------

def test_the_hands_path_starts_with_the_shim_dir_on_macos_and_is_unchanged_on_linux(monkeypatch):
    h = HandsIdentity("levain-hands", 4_000_018, "/Users/levain-hands", Path("/srv/ws"))
    monkeypatch.setattr(confinement.platform, "system", lambda: "Darwin")
    assert confinement._hands_env(h)["PATH"] == f"/Users/levain-hands/.levain-bin:{HANDS_PATH}"
    assert confinement._hands_env(h)["PATH"].split(":")[0] == confinement.hands_shim_dir(h)
    monkeypatch.setattr(confinement.platform, "system", lambda: "Linux")
    assert confinement._hands_env(h)["PATH"] == HANDS_PATH


# --- ensure_security_shim (the as-hands staging helpers, the rig's hands home) ---------------------------------------

def _shim_file(r: Rig) -> Path:
    return r.hands_home / ".levain-bin" / "security"


def test_a_login_writes_the_shim_before_the_vendor_runs(rig):
    hands_login.login(rig.h, "claude", rig.entity, emit=lambda s: None)
    assert _shim_file(rig).read_text() == hands_login.SECURITY_SHIM_TEXT
    assert os.stat(_shim_file(rig)).st_mode & 0o777 == 0o755 and os.stat(_shim_file(rig).parent).st_mode & 0o777 == 0o700


def test_a_missing_or_different_shim_is_rewritten_and_a_right_one_is_left_alone(rig):
    hands_login.ensure_security_shim(rig.h, "claude", entity_dir=rig.entity)
    ino = _shim_file(rig).stat().st_ino
    hands_login.ensure_security_shim(rig.h, "claude", entity_dir=rig.entity)
    assert _shim_file(rig).stat().st_ino == ino                            # byte-for-byte equal: not rewritten
    _shim_file(rig).write_text("#!/bin/sh\nexit 0\n")                        # tampered
    hands_login.ensure_security_shim(rig.h, "claude", entity_dir=rig.entity)
    assert _shim_file(rig).read_text() == hands_login.SECURITY_SHIM_TEXT
    _shim_file(rig).chmod(0o644)                                           # not executable: rewritten with 755
    hands_login.ensure_security_shim(rig.h, "claude", entity_dir=rig.entity)
    assert os.stat(_shim_file(rig)).st_mode & 0o777 == 0o755
    _shim_file(rig).unlink()
    hands_login.ensure_security_shim(rig.h, "claude", entity_dir=rig.entity)
    assert _shim_file(rig).read_text() == hands_login.SECURITY_SHIM_TEXT


def test_an_unwritable_shim_refuses_the_login_before_the_vendor_runs(rig):
    (rig.hands_home / ".levain-bin").write_text("a file where the directory goes")
    with pytest.raises(hands_login.HandsRefused, match="the login was not started"):
        hands_login.login(rig.h, "claude", rig.entity, emit=lambda s: None)
    assert rig.calls() == []
    (rig.hands_home / ".levain-bin").unlink()
    (rig.hands_home / ".levain-bin").mkdir()
    (rig.hands_home / ".levain-bin" / "security").symlink_to(rig.hands_home / "elsewhere")   # a link is refused, not followed
    with pytest.raises(hands_login.HandsRefused, match="the login was not started"):
        hands_login.login(rig.h, "claude", rig.entity, emit=lambda s: None)
    assert rig.calls() == []


def test_codex_has_no_shim(rig):
    hands_login.login(rig.h, "codex", rig.entity, emit=lambda s: None)
    assert not (rig.hands_home / ".levain-bin").exists()
