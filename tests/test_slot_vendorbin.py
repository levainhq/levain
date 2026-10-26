"""The vendor CLIs in the root-owned vendor prefix (1026+8 VENDOR BINARIES ruling; CONTRACT §2.1 row "vendor prefix";
BUILD_PLAN §2e 14): setup and repair COPY each vendor CLI found on the machine to ``VENDOR_PREFIX/bin/<name>`` (a
regular file, root's, 0755, never a link), an absent vendor is skipped with a line, ``start`` refuses
``vendor-not-installed:<name>`` before the clone, and doctor names a copy that differs from the vendor it came from."""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from levain.slot import Refused
from levain.slot import root_tree as RT

MACHO = b"\xcf\xfa\xed\xfe" + b"\x00" * 60
ELF = b"\x7fELF" + b"\x00" * 60


def _exe(p: Path, data: bytes, mode: int = 0o755) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    os.chmod(p, mode)
    return p


class Env:
    """A fake machine: ``home`` (the operator's), one search dir standing for Homebrew's bin, and a prefix."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.home = tmp / "home"
        self.brew = tmp / "brew" / "bin"
        self.prefix = tmp / "prefix"
        (self.prefix / "bin").mkdir(parents=True)
        self.search = ("~/.local/bin", str(self.brew))
        self.chowned: list[tuple[int, int]] = []
        self.dirs_chowned: list[str] = []

    def fchown(self, fd: int, uid: int, gid: int) -> None:
        self.chowned.append((uid, gid))

    def chown(self, path: str, uid: int, gid: int) -> None:
        self.dirs_chowned.append(path)

    def copy(self, name: str, **kw):
        return RT.copy_vendor("darwin", name, home=str(self.home), operator_uid=os.getuid(), prefix=self.prefix,
                              search=self.search, machine="arm64", fchown=self.fchown, chown=self.chown,
                              root_uid=os.getuid(), **kw)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# --- finding the vendor's native binary --------------------------------------------------------------------------

def test_native_binary_found_through_its_link(env):
    real = _exe(env.home / ".local" / "share" / "claude" / "versions" / "2.1.310", MACHO)
    (env.home / ".local" / "bin").mkdir(parents=True)
    os.symlink(real, env.home / ".local" / "bin" / "claude")
    f = RT.find_vendor("darwin", "claude", str(env.home), search=env.search, machine="arm64")
    assert f is not None and f.binary == os.path.realpath(real) and f.found == str(env.home / ".local/bin/claude")


def test_codex_node_launcher_resolves_to_its_native_vendor_binary(env):
    pkg = env.tmp / "brew" / "lib" / "node_modules" / "@openai" / "codex"
    launcher = _exe(pkg / "bin" / "codex.js", b"#!/usr/bin/env node\n")
    native = _exe(pkg / "node_modules" / "@openai" / "codex-darwin-arm64" / "vendor" / "aarch64-apple-darwin" / "bin"
                  / "codex", MACHO)
    env.brew.mkdir(parents=True)
    os.symlink(launcher, env.brew / "codex")
    f = RT.find_vendor("darwin", "codex", str(env.home), search=env.search, machine="arm64")
    assert f is not None and f.binary == os.path.realpath(native)


def test_codex_launcher_for_another_arch_has_no_native(env):
    pkg = env.tmp / "brew" / "lib" / "node_modules" / "@openai" / "codex"
    launcher = _exe(pkg / "bin" / "codex.js", b"#!/usr/bin/env node\n")
    _exe(pkg / "node_modules" / "@openai" / "codex-darwin-x64" / "vendor" / "x86_64-apple-darwin" / "bin" / "codex",
         MACHO)
    env.brew.mkdir(parents=True)
    os.symlink(launcher, env.brew / "codex")
    f = RT.find_vendor("darwin", "codex", str(env.home), search=env.search, machine="arm64")
    assert f is not None and f.binary is None and "no native" in f.why


def test_not_found_anywhere_is_none(env):
    assert RT.find_vendor("darwin", "codex", str(env.home), search=env.search, machine="arm64") is None


# --- the copy ------------------------------------------------------------------------------------------------------

def test_copy_is_a_root_owned_0755_regular_file_never_a_link(env):
    _exe(env.brew / "claude", MACHO + b"v1")
    ok, why = env.copy("claude")
    dest = env.prefix / "bin" / "claude"
    st = os.lstat(dest)
    assert ok and "copied" in why
    assert stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode)
    assert stat.S_IMODE(st.st_mode) == 0o755
    assert env.chowned == [(0, 0)]
    assert dest.read_bytes() == MACHO + b"v1"
    assert not (env.prefix / "bin" / ".claude.partial").exists()


def test_absent_vendor_is_skipped_with_a_line(env):
    ok, why = env.copy("codex")
    assert ok and "skipped" in why and "codex" in why
    assert not (env.prefix / "bin" / "codex").exists()


def test_an_identical_copy_is_left_alone(env):
    _exe(env.brew / "claude", MACHO + b"v1")
    env.copy("claude")
    ino = os.lstat(env.prefix / "bin" / "claude").st_ino
    ok, why = env.copy("claude")
    assert ok and "already" in why
    assert os.lstat(env.prefix / "bin" / "claude").st_ino == ino


def test_an_update_replaces_the_copy(env):
    _exe(env.brew / "claude", MACHO + b"v1")
    env.copy("claude")
    _exe(env.brew / "claude", MACHO + b"v2-longer")
    ok, why = env.copy("claude")
    assert ok and "copied" in why
    assert (env.prefix / "bin" / "claude").read_bytes() == MACHO + b"v2-longer"


@pytest.mark.parametrize("mode", [0o775, 0o757, 0o750, 0o744])
def test_a_source_others_can_write_or_cannot_read_is_not_copied(env, mode):
    _exe(env.brew / "claude", MACHO, mode=mode)
    ok, why = env.copy("claude")
    assert ok and "not copied" in why
    assert not (env.prefix / "bin" / "claude").exists()


def test_a_script_is_not_copied(env):
    _exe(env.brew / "claude", b"#!/bin/sh\necho hi\n")
    ok, why = env.copy("claude")
    assert ok and "skipped" in why
    assert not (env.prefix / "bin" / "claude").exists()


def test_a_refused_source_keeps_the_existing_copy(env):
    _exe(env.brew / "claude", MACHO + b"v1")
    env.copy("claude")
    _exe(env.brew / "claude", MACHO + b"v2", mode=0o777)
    ok, why = env.copy("claude")
    assert ok and "kept" in why
    assert (env.prefix / "bin" / "claude").read_bytes() == MACHO + b"v1"


def test_a_link_at_the_destination_is_replaced_by_a_file(env):
    _exe(env.brew / "claude", MACHO + b"v1")
    os.symlink(env.brew / "claude", env.prefix / "bin" / "claude")
    ok, _ = env.copy("claude")
    assert ok and not os.path.islink(env.prefix / "bin" / "claude")


def test_linux_wants_elf(tmp_path):
    e = Env(tmp_path)
    _exe(e.brew / "claude", MACHO)
    ok, why = RT.copy_vendor("linux", "claude", home=str(e.home), operator_uid=os.getuid(), prefix=e.prefix,
                             search=e.search, machine="x86_64", fchown=e.fchown, chown=e.chown)
    assert ok and "skipped" in why
    _exe(e.brew / "claude", ELF)
    ok, why = RT.copy_vendor("linux", "claude", home=str(e.home), operator_uid=os.getuid(), prefix=e.prefix,
                             search=e.search, machine="x86_64", fchown=e.fchown, chown=e.chown)
    assert ok and "copied" in why


# --- the drift read (doctor's row reads it) ---------------------------------------------------------------------------

def test_vendor_state(env):
    kw = dict(prefix=env.prefix, search=env.search, machine="arm64", root_uid=os.getuid())
    assert RT.vendor_state("darwin", "claude", str(env.home), **kw)[0] == "absent"
    _exe(env.brew / "claude", MACHO + b"v1")
    assert RT.vendor_state("darwin", "claude", str(env.home), **kw)[0] == "not-copied"
    env.copy("claude")
    assert RT.vendor_state("darwin", "claude", str(env.home), **kw)[0] == "current"
    _exe(env.brew / "claude", MACHO + b"v2")
    assert RT.vendor_state("darwin", "claude", str(env.home), **kw)[0] == "stale"
    os.unlink(env.brew / "claude")
    assert RT.vendor_state("darwin", "claude", str(env.home), **kw)[0] == "orphan"


# --- the plans carry the steps ---------------------------------------------------------------------------------------

def test_vendor_steps_name_each_copy_and_the_bin_dir():
    steps = RT.vendor_steps("darwin", "op")
    whys = [s.why for s in steps]
    assert any(str(RT.VENDOR_PREFIX["darwin"] / "bin") in w for w in whys)
    assert any(w.startswith("place root's copy of the claude CLI") for w in whys)
    assert any(w.startswith("place root's copy of the codex CLI") for w in whys)


# --- start's refusal -----------------------------------------------------------------------------------------------

def test_check_vendor_installed(tmp_path):
    from levain.slot import start as SS

    (tmp_path / "bin").mkdir()
    with pytest.raises(Refused) as e:
        SS.check_vendor_installed("darwin", "claude", prefix=tmp_path)
    assert e.value.name == "vendor-not-installed:claude"
    os.symlink("/bin/sh", tmp_path / "bin" / "claude")
    with pytest.raises(Refused) as e:
        SS.check_vendor_installed("darwin", "claude", prefix=tmp_path)
    assert e.value.name == "vendor-not-installed:claude" and "link" in str(e.value.detail)
    os.unlink(tmp_path / "bin" / "claude")
    _exe(tmp_path / "bin" / "claude", MACHO)
    if os.getuid() != 0:
        with pytest.raises(Refused) as e:      # the operator's file, not root's
            SS.check_vendor_installed("darwin", "claude", prefix=tmp_path)
        assert "root" in str(e.value.detail)
    SS.check_vendor_installed("darwin", "claude", prefix=tmp_path, root_uid=os.getuid())
    os.chmod(tmp_path / "bin" / "claude", 0o775)
    with pytest.raises(Refused):
        SS.check_vendor_installed("darwin", "claude", prefix=tmp_path, root_uid=os.getuid())


# --- doctor's row ----------------------------------------------------------------------------------------------------

def _doctor(tmp_path, states):
    from levain import doctor as D

    return D._check_vendor_copies(host="darwin", store=tmp_path, home=str(tmp_path), state=lambda n: states[n])


def test_doctor_row_current(tmp_path):
    [r] = _doctor(tmp_path, {"claude": ("current", "x"), "codex": ("absent", "no codex")})
    assert r.ok and not r.warn and "claude current" in r.detail and "codex not on this machine" in r.detail


def test_doctor_row_names_a_stale_copy(tmp_path):
    [r] = _doctor(tmp_path, {"claude": ("stale", "/p/bin/claude differs from /v/claude"), "codex": ("current", "")})
    assert r.ok and r.warn and "claude STALE" in r.detail and "repair" in r.hint and "claude" in r.hint


def test_doctor_row_names_a_vendor_not_copied(tmp_path):
    [r] = _doctor(tmp_path, {"claude": ("current", ""), "codex": ("not-copied", "/v/codex is not copied")})
    assert r.warn and "vendor-not-installed:codex" in r.detail


def test_doctor_row_absent_without_an_install(tmp_path):
    from levain import doctor as D

    assert D._check_vendor_copies(host="darwin", store=tmp_path / "none", state=lambda n: 1 / 0) == []


def test_doctor_row_reads_the_real_state(env):
    from levain import doctor as D

    _exe(env.brew / "claude", MACHO + b"v1")
    env.copy("claude")
    _exe(env.brew / "claude", MACHO + b"v2")
    state = lambda n: RT.vendor_state("darwin", n, str(env.home), prefix=env.prefix, search=env.search,
                                      machine="arm64", root_uid=os.getuid())
    [r] = D._check_vendor_copies(host="darwin", store=env.tmp, home=str(env.home), state=state)
    assert r.warn and "claude STALE" in r.detail and "codex not on this machine" in r.detail


# --- f1 (1026-10 RULINGS "vendorbin L3 r1 triage -> vbf1") -------------------------------------------------------------

def _codex_pkg(env, *, version: bytes = b"v1", skip=(), extra=None):
    """A Codex npm install the way 0.162.1 lays it out on a Mac (lane vbf1 measured it): the launcher, and the
    platform package's ``vendor/<triple>/`` root holding ``codex-package.json``, ``bin/codex``,
    ``bin/codex-code-mode-host``, ``codex-path/rg`` and ``codex-resources/``."""
    pkg = env.tmp / "brew" / "lib" / "node_modules" / "@openai" / "codex"
    launcher = _exe(pkg / "bin" / "codex.js", b"#!/usr/bin/env node\n")
    root = pkg / "node_modules" / "@openai" / "codex-darwin-arm64" / "vendor" / "aarch64-apple-darwin"
    files = {"codex-package.json": (b'{"layoutVersion": 1}', 0o644), "bin/codex": (MACHO + version, 0o755),
             "bin/codex-code-mode-host": (MACHO + b"host" + version, 0o755), "codex-path/rg": (MACHO + b"rg", 0o755),
             "codex-resources/voice/manifest.json": (b"{}", 0o644),
             "codex-resources/voice/lib/libffi.8.dylib": (MACHO + b"ffi", 0o755),
             "codex-resources/zsh/bin/zsh": (MACHO + b"zsh", 0o755), **(extra or {})}
    for rel, (data, mode) in files.items():
        if rel not in skip:
            _exe(root / rel, data, mode)
    env.brew.mkdir(parents=True, exist_ok=True)
    if not os.path.lexists(env.brew / "codex"):
        os.symlink(launcher, env.brew / "codex")
    return root, {r: v for r, v in files.items() if r not in skip}


def _codex_copy(env, **kw):
    return env.copy("codex", **kw)


def test_v1_a_codex_copy_without_its_companions_is_refused_by_start(tmp_path):
    """RED at e8efdaf6: ``bin/codex`` alone passed start's check, and codex's code-mode host was never there."""
    from levain.slot import start as SS

    _exe(tmp_path / "bin" / "codex", MACHO)
    with pytest.raises(Refused) as e:
        SS.check_vendor_installed("darwin", "codex", prefix=tmp_path, root_uid=os.getuid())
    assert e.value.name == "vendor-not-installed:codex" and "codex-package.json" in str(e.value.detail)


def test_v1_the_codex_package_is_copied_as_a_unit(env):
    root, files = _codex_pkg(env)
    ok, why = _codex_copy(env)
    assert ok and "copied" in why, why
    for rel, (data, mode) in files.items():
        st = os.lstat(env.prefix / rel)
        assert stat.S_ISREG(st.st_mode) and stat.S_IMODE(st.st_mode) == mode, rel
        assert (env.prefix / rel).read_bytes() == data, rel
    assert not any(p.name.endswith(".partial") for p in env.prefix.rglob("*"))
    from levain.slot import start as SS
    SS.check_vendor_installed("darwin", "codex", prefix=env.prefix, root_uid=os.getuid())
    assert RT.vendor_state("darwin", "codex", str(env.home), prefix=env.prefix, search=env.search,
                           machine="arm64", root_uid=os.getuid())[0] == "current"
    ok, why = _codex_copy(env)
    assert ok and "already" in why, why


def test_v1_a_companion_update_makes_the_unit_stale_and_repair_recopies_it(env):
    _codex_pkg(env)
    _codex_copy(env)
    _codex_pkg(env, extra={"codex-path/rg": (MACHO + b"rg2", 0o755)})
    kw = dict(prefix=env.prefix, search=env.search, machine="arm64", root_uid=os.getuid())
    assert RT.vendor_state("darwin", "codex", str(env.home), **kw)[0] == "stale"
    ok, why = _codex_copy(env)
    assert ok and "copied" in why and (env.prefix / "codex-path" / "rg").read_bytes() == MACHO + b"rg2"


@pytest.mark.parametrize("skip", ["codex-package.json", "bin/codex-code-mode-host", "codex-path/rg"])
def test_v1_a_codex_package_missing_a_required_file_is_not_copied(env, skip):
    _codex_pkg(env, skip=(skip,))
    ok, why = _codex_copy(env)
    assert ok and ("skipped" in why or "not copied" in why) and skip.split("/")[-1] in why, why
    assert not (env.prefix / "bin" / "codex").exists()


def test_v1_a_link_inside_the_codex_package_is_not_copied(env):
    root, _ = _codex_pkg(env)
    os.symlink("/etc/hosts", root / "codex-resources" / "voice" / "hosts")
    ok, why = _codex_copy(env)
    assert ok and "not copied" in why and "hosts" in why, why
    assert not (env.prefix / "bin" / "codex").exists()


def test_v1_the_entry_point_is_published_last(env):
    """A copy that fails part-way never leaves a new ``bin/codex`` beside companions that are not all there."""
    _codex_pkg(env)
    calls = []

    def fsync(fd):
        calls.append(fd)
        if len(calls) == 3:
            raise OSError(5, "Input/output error")
    ok, why = _codex_copy(env, fsync=fsync)
    assert not ok and "failed" in why
    assert not (env.prefix / "bin" / "codex").exists()
    assert not any(p.name.endswith(".partial") for p in env.prefix.rglob("*"))


def test_v1_start_refuses_a_codex_unit_with_a_bad_companion(env):
    from levain.slot import start as SS

    _codex_pkg(env)
    _codex_copy(env)
    os.chmod(env.prefix / "codex-path" / "rg", 0o775)
    with pytest.raises(Refused) as e:
        SS.check_vendor_installed("darwin", "codex", prefix=env.prefix, root_uid=os.getuid())
    assert "codex-path/rg" in str(e.value.detail)


def _mutating_pread(monkeypatch, action):
    """``os.pread`` that runs ``action`` once, right after the first full-size chunk read of the copy loop."""
    real, done = os.pread, []

    def pread(fd, n, off):
        data = real(fd, n, off)
        if n > 4 and not done and data:
            done.append(1)
            action()
        return data
    monkeypatch.setattr(RT.os, "pread", pread)


def test_v2_a_same_size_change_during_the_copy_is_refused(env, monkeypatch):
    """RED at e8efdaf6: an in-place same-size rewrite mid-copy published a hybrid of old and new bytes."""
    src = _exe(env.brew / "claude", MACHO + b"v1")

    def rewrite():
        with open(src, "r+b") as f:
            f.seek(len(MACHO))
            f.write(b"v2")
        os.utime(src, ns=(os.stat(src).st_atime_ns, os.stat(src).st_mtime_ns + 10**9))
    _mutating_pread(monkeypatch, rewrite)
    ok, why = env.copy("claude")
    assert not ok and "vendor-changed-during-copy" in why, why
    assert not (env.prefix / "bin" / "claude").exists()
    assert not (env.prefix / "bin" / ".claude.partial").exists()


def test_v2_a_source_that_grows_is_read_only_to_its_captured_size(env, monkeypatch):
    src = _exe(env.brew / "claude", MACHO + b"v1")
    size = os.path.getsize(src)
    seen = []

    def append():
        with open(src, "ab") as f:
            f.write(b"x" * (3 << 20))
    _mutating_pread(monkeypatch, append)
    inner = RT.os.pread

    def spy(fd, n, off):
        seen.append(off + n)
        return inner(fd, n, off)
    monkeypatch.setattr(RT.os, "pread", spy)
    ok, why = env.copy("claude")
    assert not ok and "vendor-changed-during-copy" in why, why
    assert max(seen) <= size + 1        # the one byte past the captured size is the growth probe


def test_v4_a_byte_identical_copy_with_bad_perms_is_a_distinct_doctor_state(env):
    """RED at e8efdaf6: doctor said ``current`` for a copy start refuses."""
    from levain import doctor as D

    _exe(env.brew / "claude", MACHO + b"v1")
    env.copy("claude")
    os.chmod(env.prefix / "bin" / "claude", 0o775)
    kw = dict(prefix=env.prefix, search=env.search, machine="arm64", root_uid=os.getuid())
    st, detail = RT.vendor_state("darwin", "claude", str(env.home), **kw)
    assert st == "bad-copy" and "775" in detail
    [r] = D._check_vendor_copies(host="darwin", store=env.tmp, home=str(env.home),
                                 state=lambda n: RT.vendor_state("darwin", n, str(env.home), **kw))
    assert r.warn and "repair" in r.hint and "claude" in r.hint and "BAD COPY" in r.detail


def test_v5_doctor_searches_from_the_operators_home_under_sudo(tmp_path, monkeypatch):
    """RED at e8efdaf6: ``sudo levain doctor`` searched from root's home."""
    import pwd

    from levain import doctor as D

    me = pwd.getpwuid(os.getuid())
    monkeypatch.setenv("HOME", str(tmp_path / "var-root"))
    monkeypatch.setenv("SUDO_UID", str(me.pw_uid))
    monkeypatch.setattr(D.os, "getuid", lambda: 0)
    homes = []
    monkeypatch.setattr(RT, "vendor_state", lambda host, name, home, **kw: (homes.append(home), ("absent", ""))[1])
    D._check_vendor_copies(host="darwin", store=tmp_path)
    assert homes and set(homes) == {me.pw_dir}


def test_v6_a_failed_vendor_copy_does_not_block_later_steps_and_still_fails_the_run():
    from levain.firing.hands import Plan, run_plan

    failed: list = []
    from types import SimpleNamespace

    steps = RT.vendor_steps("darwin", "op", failed=failed, lookup=lambda o: SimpleNamespace(pw_dir="/h", pw_uid=1),
                            copy=lambda host, name, **kw: (False, f"{name}: copying failed (EIO)"))
    copies = [s for s in steps if s.why.startswith("place root's copy")]
    assert copies and all(s.allow_fail for s in copies)
    ran = []
    later = RT.Step("the sudoers rule", call=lambda: (ran.append(1), (True, ""))[1])
    plan = Plan("darwin", "op", "h", 1, Path("/x"), Path("/y"),
                steps=(*copies, later, RT.vendor_report_step(failed)))
    out: list = []
    assert run_plan(plan, dry_run=False, emit=out.append) == 1
    assert ran == [1]
    assert any("FAILED" in o and "claude" in o and "codex" in o for o in out), out


def test_v6_repair_reports_vendor_failures_after_the_sudoers_and_units(tmp_path):
    from tests.test_repair_plan import _pre_change, _plan

    facts, rf = _pre_change(tmp_path)
    whys = [s.why for s in _plan(facts, rf).steps]
    report = [i for i, w in enumerate(whys) if w.startswith("report the vendor CLI copies")]
    sudoers = [i for i, w in enumerate(whys) if "sudoers" in w]
    copies = [i for i, w in enumerate(whys) if w.startswith("place root's copy")]
    assert len(report) == 1 and copies and sudoers
    assert max(copies) < min(sudoers) and report[0] > max(sudoers)


def test_v7_a_failed_rename_unlinks_its_partial(env):
    _exe(env.brew / "claude", MACHO + b"v1")
    (env.prefix / "bin" / "claude").mkdir()
    (env.prefix / "bin" / "claude" / "x").write_bytes(b"x")     # a non-empty dir: the rename over it fails
    ok, why = env.copy("claude")
    assert not ok and "failed" in why
    assert not (env.prefix / "bin" / ".claude.partial").exists()


def test_v7_a_dir_fsync_failure_after_the_rename_is_a_warning(env):
    _exe(env.brew / "claude", MACHO + b"v1")
    calls = []

    def fsync(fd):
        calls.append(fd)
        if len(calls) > 1:
            raise OSError(5, "Input/output error")
    ok, why = env.copy("claude", fsync=fsync)
    assert ok and "copied" in why and "warning" in why, why
    assert (env.prefix / "bin" / "claude").read_bytes() == MACHO + b"v1"


def test_low3_an_unreadable_copy_gets_the_repair_hint(tmp_path):
    [r] = _doctor(tmp_path, {"claude": ("unreadable", "cannot compare"), "codex": ("current", "")})
    assert r.warn and "repair" in r.hint and "claude" in r.hint and "leaves it" not in r.hint
