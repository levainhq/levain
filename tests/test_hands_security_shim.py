"""levain#241 (ruling d241_v2): the `security` shim on the hands PATH, root-installed (L3 r1 fixes: where it lives, who checks it). The shim text is written to tmp_path and RUN for
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
from tests.test_hands_login import Rig, install_test_shim, rig  # noqa: F401

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


@pytest.mark.parametrize("args", [("-q", "-i"), ("-v", "-q", "-i"), ("-q", "-i", "-s")])
def test_dash_i_among_the_leading_options_is_intercepted(shim, tmp_path, args):
    work = tmp_path / "work"
    work.mkdir()
    r = _run(shim, work, *args, stdin=b"add-generic-password -U -s s -a a -w secret\n" * 1000)
    assert r.returncode == 36 and r.stdout == b"" and r.stderr == b"" and list(work.iterdir()) == []


@pytest.mark.parametrize("verb", ["find-generic-password", "add-generic-password", "delete-generic-password"])
@pytest.mark.parametrize("lead", [("-q",), ("-q", "-v")])
def test_an_item_verb_after_global_flags_is_intercepted(shim, tmp_path, verb, lead):
    work = tmp_path / "work"
    work.mkdir()
    r = _run(shim, work, *lead, verb, "-s", "Claude Code-credentials")
    assert r.returncode == 36 and r.stdout == b"" and r.stderr == b"" and list(work.iterdir()) == []


@pytest.fixture
def stub_shim(tmp_path: Path) -> tuple[Path, Path]:
    """The shim text with /usr/bin/security swapped for a stub that records its argv, so pass-through is observable."""
    out = tmp_path / "argv.txt"
    stub = tmp_path / "real-security"
    stub.write_text(f'#!/bin/sh\nfor a in "$@"; do printf \'%s\\n\' "$a" >> {out}; done\nexit 0\n')
    stub.chmod(0o755)
    d = tmp_path / "stubshimdir"
    d.mkdir()
    p = d / "security"
    assert 'exec /usr/bin/security "$@"' in hands_login.SECURITY_SHIM_TEXT
    p.write_text(hands_login.SECURITY_SHIM_TEXT.replace("/usr/bin/security", str(stub)))
    p.chmod(0o755)
    return p, out


@pytest.mark.parametrize("args", [("-i",), ("-q", "-i"), ("-qi",), ("-vi",)])
def test_a_leading_option_with_i_drains_stdin_and_exits_36(stub_shim, tmp_path, args):
    p, out = stub_shim
    r = _run(p, tmp_path, *args, stdin=b"add-generic-password -U -s s -a a -w secret\n" * 1000)
    assert r.returncode == 36 and r.stdout == b"" and r.stderr == b"" and not out.exists()


@pytest.mark.parametrize("args", [("find-generic-password", "-s", "x"), ("-q", "find-generic-password"),
                                  ("-p", "prompt", "delete-generic-password")])
def test_a_leading_item_verb_exits_36(stub_shim, tmp_path, args):
    p, out = stub_shim
    r = _run(p, tmp_path, *args)
    assert r.returncode == 36 and not out.exists()


@pytest.mark.parametrize("args", [("cms", "-D", "-i", "profile.mobileprovision"), ("find-certificate", "-c", "-i"),
                                  ("-p", "prompt", "list-keychains"), ("-p", "-i", "list-keychains"),
                                  ("list-keychains", "find-generic-password")])
def test_everything_else_passes_through_with_the_original_arguments(stub_shim, tmp_path, args):
    p, out = stub_shim
    r = _run(p, tmp_path, *args)
    assert r.returncode == 0
    assert out.read_text().splitlines() == list(args)


def test_the_shim_text_has_no_logging_scans_leading_options_and_uses_absolute_paths():
    t = hands_login.SECURITY_SHIM_TEXT
    assert t.startswith("#!/bin/sh\n") and 'exec /usr/bin/security "$@"' in t and 'for a in "$@"' in t
    assert ">>" not in t and "tee" not in t and "logger" not in t          # the credential on stdin is never kept
    assert "/bin/cat > /dev/null" in t and "\n      cat " not in t and " cat " not in t.replace("/bin/cat", "")


# --- the PATH -------------------------------------------------------------------------------------------------------

def test_the_hands_path_starts_with_the_root_owned_shim_dir_on_macos_and_is_unchanged_on_linux(monkeypatch):
    h = HandsIdentity("levain-hands", 4_000_018, "/Users/levain-hands", Path("/srv/ws"))
    monkeypatch.setattr(confinement.platform, "system", lambda: "Darwin")
    assert confinement.HANDS_SHIM_SYSTEM_DIR == "/Library/Application Support/Levain/hands-bin"
    assert confinement._hands_env(h)["PATH"] == f"/Library/Application Support/Levain/hands-bin:{HANDS_PATH}"
    assert not confinement._hands_env(h)["PATH"].startswith(h.home)        # never under the hands home
    monkeypatch.setattr(confinement.platform, "system", lambda: "Linux")
    assert confinement._hands_env(h)["PATH"] == HANDS_PATH


# --- the check (security_shim_problem): reads only, refuses anything but the exact root-installed shim ----------------

@pytest.fixture
def sys_shim(tmp_path, monkeypatch) -> Path:
    return install_test_shim(tmp_path, monkeypatch)


def test_the_exact_shim_passes(sys_shim):
    assert hands_login.security_shim_problem() is None


def test_a_missing_directory_or_file_is_refused_naming_the_repair(sys_shim):
    (sys_shim / "security").unlink()
    assert "not installed" in hands_login.security_shim_problem()
    assert "sudo levain setup-isolation" in hands_login.security_shim_problem()
    sys_shim.rmdir()
    assert "not installed" in hands_login.security_shim_problem()


def test_the_wrong_owner_is_refused_on_the_directory_and_the_file(sys_shim, monkeypatch):
    monkeypatch.setattr(hands_login, "SECURITY_SHIM_OWNER_UID", os.getuid() + 1)
    p = hands_login.security_shim_problem()
    assert p and "owned by uid" in p and "sudo levain setup-isolation" in p


@pytest.mark.parametrize("mode", [0o1755, 0o2755, 0o4755, 0o775, 0o757, 0o700, 0o644, 0o777])
def test_the_file_mode_must_be_exactly_0755(sys_shim, mode):
    f = sys_shim / "security"
    f.chmod(mode)
    if f.stat().st_mode & 0o7777 != mode:
        pytest.skip("this user cannot set that bit on a file here (the OS drops it)")
    p = hands_login.security_shim_problem()
    assert p and "mode" in p
    assert f"{mode:o}" in p


@pytest.mark.parametrize("mode", [0o1755, 0o2755, 0o775, 0o757, 0o700])
def test_the_directory_mode_must_be_exactly_0755(sys_shim, mode):
    sys_shim.chmod(mode)
    if sys_shim.stat().st_mode & 0o7777 != mode:
        sys_shim.chmod(0o755)
        pytest.skip("this user cannot set that bit on a directory here (the OS drops it)")
    try:
        p = hands_login.security_shim_problem()
    finally:
        sys_shim.chmod(0o755)
    assert p and "mode" in p


def test_different_bytes_are_refused(sys_shim):
    f = sys_shim / "security"
    f.write_text(hands_login.SECURITY_SHIM_TEXT + "# extra\n")
    assert "not the shim Levain installs" in hands_login.security_shim_problem()
    f.write_text("#!/bin/sh\nexit 0\n")
    assert "not the shim Levain installs" in hands_login.security_shim_problem()


def test_a_symlinked_file_is_refused(sys_shim, tmp_path):
    real = tmp_path / "real-security"
    real.write_text(hands_login.SECURITY_SHIM_TEXT)
    real.chmod(0o755)
    (sys_shim / "security").unlink()
    (sys_shim / "security").symlink_to(real)
    assert hands_login.security_shim_problem() is not None


@pytest.mark.parametrize("which", ["hands-bin", "Levain", "Application Support", "Library"])
def test_a_symlink_at_any_path_component_is_refused(sys_shim, tmp_path, which):
    """Every component from the walk root down is lstat'd, so a link anywhere in the chain is refused."""
    comps = {"hands-bin": sys_shim, "Levain": sys_shim.parent, "Application Support": sys_shim.parent.parent,
             "Library": sys_shim.parent.parent.parent}
    target = comps[which]
    moved = tmp_path / f"moved-{which.replace(' ', '_')}"
    target.rename(moved)
    target.symlink_to(moved)
    p = hands_login.security_shim_problem()
    assert p and "symlink" in p and which in p


def test_an_ancestor_owned_by_another_uid_is_refused(sys_shim, monkeypatch):
    real_lstat = os.lstat
    anc = sys_shim.parent.parent

    class _St:
        def __init__(self, st):
            self._st = st
            self.st_uid = st.st_uid + 1

        def __getattr__(self, n):
            return getattr(self._st, n)

    monkeypatch.setattr(os, "lstat", lambda q, *a, **k: _St(real_lstat(q, *a, **k)) if str(q) == str(anc) else real_lstat(q, *a, **k))
    p = hands_login.security_shim_problem()
    assert p and str(anc) in p and "owned by uid" in p and p.endswith(hands_login.SHIM_REPAIR)


@pytest.mark.parametrize("mode", [0o775, 0o757])
@pytest.mark.parametrize("level", ["Levain", "Application Support", "Library", "start"])
def test_an_ancestor_writable_by_group_or_other_is_refused(sys_shim, mode, level):
    anc = {"Levain": sys_shim.parent, "Application Support": sys_shim.parent.parent,
           "Library": sys_shim.parent.parent.parent, "start": sys_shim.parent.parent.parent.parent}[level]
    anc.chmod(mode)
    try:
        p = hands_login.security_shim_problem()
    finally:
        anc.chmod(0o755)
    assert p and str(anc) in p and "no group or other write" in p and p.endswith(hands_login.SHIM_REPAIR)


def test_an_extra_entry_in_the_shim_directory_is_refused(sys_shim):
    (sys_shim / "claude").write_text("x")
    p = hands_login.security_shim_problem()
    assert p and "holds something other than the shim (claude)" in p and "root removes it by hand" in p
    assert p.endswith(hands_login.SHIM_REPAIR)


def test_the_real_mac_system_directories_pass_the_ancestor_rule():
    if sys.platform != "darwin":
        pytest.skip("macOS system directories")
    for q in ("/Library", "/Library/Application Support"):
        st = os.lstat(q)
        assert st.st_uid == 0 and (st.st_mode & 0o022) == 0


def test_a_directory_where_the_file_goes_is_refused(sys_shim):
    (sys_shim / "security").unlink()
    (sys_shim / "security").mkdir()
    assert hands_login.security_shim_problem() is not None


def test_check_security_shim_is_for_claude_on_macos_only(sys_shim, monkeypatch):
    monkeypatch.setattr(hands_login.platform, "system", lambda: "Darwin")
    (sys_shim / "security").unlink()
    with pytest.raises(hands_login.HandsLoginError, match="sudo levain setup-isolation"):
        hands_login.check_security_shim("claude")
    hands_login.check_security_shim("codex")                               # Codex has no shim
    monkeypatch.setattr(hands_login.platform, "system", lambda: "Linux")
    hands_login.check_security_shim("claude")                              # nor does Linux


# --- login, status, logout refuse on a miss and run nothing; nothing writes the shim ---------------------------------

def _tamper(rig):
    (rig.shim_dir / "security").write_text("#!/bin/sh\nexit 0\n")


def test_a_login_refuses_before_the_vendor_runs_when_the_shim_is_not_exact(rig):
    _tamper(rig)
    with pytest.raises(hands_login.HandsRefused, match="sudo levain setup-isolation"):
        hands_login.login(rig.h, "claude", rig.entity, emit=lambda s: None)
    assert rig.calls() == [] and not (rig.hands_home / ".claude").exists()
    assert (rig.shim_dir / "security").read_text() == "#!/bin/sh\nexit 0\n"   # never repaired from here
    (rig.shim_dir / "security").unlink()
    with pytest.raises(hands_login.HandsRefused, match="the login was not started"):
        hands_login.login(rig.h, "claude", rig.entity, emit=lambda s: None)
    assert rig.calls() == [] and not (rig.shim_dir / "security").exists()
    assert not (rig.hands_home / ".levain-bin").exists()


def test_a_login_with_the_exact_shim_runs_and_never_rewrites_it(rig):
    f = rig.shim_dir / "security"
    ino, mtime = f.stat().st_ino, f.stat().st_mtime_ns
    hands_login.login(rig.h, "claude", rig.entity, emit=lambda s: None)
    assert f.stat().st_ino == ino and f.stat().st_mtime_ns == mtime and f.read_text() == hands_login.SECURITY_SHIM_TEXT


def test_status_refuses_before_claude_runs_when_the_shim_is_not_exact(rig):
    _tamper(rig)
    st = hands_login.run_status(rig.h, "claude", entity_dir=rig.entity)
    assert st.state == hands_login.UNKNOWN and "sudo levain setup-isolation" in st.error
    assert rig.calls() == []
    ok = hands_login.run_status(rig.h, "codex", entity_dir=rig.entity)      # codex has no shim to check
    assert "sudo levain" not in ok.error


def test_logout_refuses_before_any_claude_run_when_the_shim_is_not_exact(rig):
    _tamper(rig)
    with pytest.raises(hands_login.HandsRefused, match="sudo levain setup-isolation"):
        hands_login.run_logout(rig.h, "claude", rig.entity)
    assert rig.calls() == []
    assert hands_login.cmd_hands_logout(rig.entity, "claude") == 1


def test_codex_has_no_shim_check(rig):
    (rig.shim_dir / "security").unlink()
    hands_login.login(rig.h, "codex", rig.entity, emit=lambda s: None)
    assert not (rig.hands_home / ".levain-bin").exists()


def test_the_hands_home_shim_directory_and_its_as_hands_writer_are_gone():
    assert not hasattr(hands_login, "ensure_security_shim")
    assert not hasattr(confinement, "HANDS_SHIM_DIRNAME")
