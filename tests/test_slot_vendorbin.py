"""The vendor CLIs in the root-owned vendor prefix (1026+8 VENDOR BINARIES ruling; CONTRACT §2.1 row "vendor prefix";
BUILD_PLAN §2e 14): setup and repair COPY each vendor CLI found on the machine into ``VENDOR_PREFIX`` (Claude Code to
``bin/claude``, a regular file, root's, 0755, never a link; Codex's whole package to ``codex/<version>-<digest>/``, with
``bin/codex`` root's link into it, head ruling 1026-14 V8), an absent vendor is skipped with a line, ``start`` refuses
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


def test_the_slot_tests_vendor_prefix_passes_the_real_check_and_only_whole(tmp_path):
    """tests/vendor_tree.py's prefix (the slot tests' ``start`` runs the real check against it) is one the check
    accepts for every vendor on both hosts, and the check still refuses it once a vendor's copy is gone."""
    from levain.slot import start as SS
    from tests.vendor_tree import _VERSION, build_vendor_prefix

    prefix = build_vendor_prefix(tmp_path / "levain-vendor")
    for host in ("darwin", "linux"):
        for name in RT.VENDORS:
            SS.check_vendor_installed(host, name, prefix=prefix, root_uid=os.getuid())
    os.unlink(prefix / "codex" / _VERSION / "codex-path" / "rg")
    os.unlink(prefix / "bin" / "claude")
    for name in RT.VENDORS:
        with pytest.raises(Refused) as e:
            SS.check_vendor_installed("linux", name, prefix=prefix, root_uid=os.getuid())
        assert e.value.name == f"vendor-not-installed:{name}"


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
    assert e.value.name == "vendor-not-installed:codex" and "not the link" in str(e.value.detail)


def test_v1_the_codex_package_is_copied_as_a_unit(env):
    root, files = _codex_pkg(env)
    ok, why = _codex_copy(env)
    assert ok and "copied" in why, why
    active = _active_root(env.prefix)
    for rel, (data, mode) in files.items():
        st = os.lstat(active / rel)
        assert stat.S_ISREG(st.st_mode) and stat.S_IMODE(st.st_mode) == mode, rel
        assert (active / rel).read_bytes() == data, rel
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
    assert ok and "copied" in why and (_active_root(env.prefix) / "codex-path" / "rg").read_bytes() == MACHO + b"rg2"


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
    os.chmod(_active_root(env.prefix) / "codex-path" / "rg", 0o775)
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


# --- f2 (1026-14 RULINGS "vendorbin verify r2 triage" V8-V13) ---------------------------------------------------------

def _active_root(prefix: Path) -> Path:
    """The package root ``bin/codex`` runs from (links resolved, the way codex finds its own package)."""
    return Path(os.path.realpath(prefix / "bin" / "codex")).parent.parent


def _v2(env, **kw):
    return _codex_pkg(env, extra={"bin/codex": (MACHO + b"v2", 0o755),
                                  "bin/codex-code-mode-host": (MACHO + b"hostv2", 0o755),
                                  "codex-path/rg": (MACHO + b"rg-v2", 0o755),
                                  "codex-resources/voice/manifest.json": (b'{"v": 2}', 0o644)}, **kw)


def test_v8_an_upgrade_that_fails_part_way_leaves_the_old_package_active_and_whole(env):
    """RED at bb334bac: companions were renamed over the active package one at a time, so a failure on a later file
    left the old ``bin/codex`` beside new companions and start accepted the mix."""
    _, v1 = _codex_pkg(env)
    assert _codex_copy(env)[0]
    _v2(env)
    calls = []

    def fsync(fd):
        calls.append(fd)
        if len(calls) == 3:
            raise OSError(5, "Input/output error")
    ok, why = _codex_copy(env, fsync=fsync)
    assert not ok and "failed" in why, why
    root = _active_root(env.prefix)
    for rel, (data, _) in v1.items():
        assert (root / rel).read_bytes() == data, rel
    assert RT.unit_problem("darwin", "codex", prefix=env.prefix, root_uid=os.getuid()) == ""
    ok, why = _codex_copy(env)
    assert ok and "copied" in why, why
    assert (_active_root(env.prefix) / "codex-path" / "rg").read_bytes() == MACHO + b"rg-v2"


def test_v8_the_active_name_is_one_link_to_a_complete_versioned_package(env):
    _codex_pkg(env, extra={"codex-package.json": (b'{"version": "0.162.1"}', 0o644)})
    ok, why = _codex_copy(env)
    assert ok, why
    link = env.prefix / "bin" / "codex"
    assert os.path.islink(link)
    target = os.readlink(link)
    assert target.startswith("../codex/0.162.1-") and target.endswith("/bin/codex"), target
    assert _active_root(env.prefix).parent == (env.prefix / "codex").resolve()
    first = _active_root(env.prefix).name
    _codex_pkg(env, extra={"codex-package.json": (b'{"version": "0.163.0"}', 0o644)})
    ok, why = _codex_copy(env)
    assert ok and "copied" in why, why
    assert _active_root(env.prefix).name.startswith("0.163.0-")
    left = sorted(p.name for p in (env.prefix / "codex").iterdir() if p.name != ".lock")
    assert left == sorted([first, _active_root(env.prefix).name]), left
    assert not any(p.name.endswith(".partial") for p in env.prefix.rglob("*"))


@pytest.mark.parametrize("target", ["/bin/sh", "../../elsewhere/bin/codex", "../codex/.staging.partial/bin/codex"])
def test_v8_start_refuses_an_active_link_off_the_package_dir(env, target):
    from levain.slot import start as SS

    _codex_pkg(env)
    _codex_copy(env)
    os.unlink(env.prefix / "bin" / "codex")
    os.symlink(target, env.prefix / "bin" / "codex")
    with pytest.raises(Refused) as e:
        SS.check_vendor_installed("darwin", "codex", prefix=env.prefix, root_uid=os.getuid())
    assert e.value.name == "vendor-not-installed:codex"


def test_v8_a_flat_codex_file_is_not_the_active_package(env):
    from levain.slot import start as SS

    _exe(env.prefix / "codex-package.json", b"{}", 0o644)
    _exe(env.prefix / "bin" / "codex", MACHO)
    _exe(env.prefix / "bin" / "codex-code-mode-host", MACHO)
    _exe(env.prefix / "codex-path" / "rg", MACHO)
    _exe(env.prefix / "codex-resources" / "x", b"x", 0o644)
    with pytest.raises(Refused):
        SS.check_vendor_installed("darwin", "codex", prefix=env.prefix, root_uid=os.getuid())


def test_v9_repair_fixes_a_bad_directory_mode_when_every_file_matches(env):
    """RED at bb334bac: every file equal returned "already matches" before any directory was checked, so a 0775
    directory start refuses was never fixed."""
    _codex_pkg(env)
    _codex_copy(env)
    os.chmod(_active_root(env.prefix) / "codex-resources" / "voice", 0o775)
    assert RT.unit_problem("darwin", "codex", prefix=env.prefix, root_uid=os.getuid())
    ok, why = _codex_copy(env)
    assert ok, why
    assert RT.unit_problem("darwin", "codex", prefix=env.prefix, root_uid=os.getuid()) == ""


def test_v9_a_repair_that_leaves_a_problem_reports_failure(env, monkeypatch):
    """With no chmod at all neither set-in-place nor the V28 rebuild can make the tree whole: a failure, active kept."""
    _codex_pkg(env)
    _codex_copy(env)
    os.chmod(_active_root(env.prefix) / "codex-resources" / "voice", 0o775)
    monkeypatch.setattr(RT.os, "chmod", lambda *a, **k: None)
    ok, why = _codex_copy(env)
    assert not ok and "not whole" in why and "unchanged" in why, why


def test_v10_start_refuses_a_required_executable_at_0644(env):
    """RED at bb334bac: every non-entry file passed at 0755 or 0644, so a 0644 ``rg`` started and failed EACCES."""
    from levain.slot import start as SS

    _codex_pkg(env)
    _codex_copy(env)
    os.chmod(_active_root(env.prefix) / "codex-path" / "rg", 0o644)
    with pytest.raises(Refused) as e:
        SS.check_vendor_installed("darwin", "codex", prefix=env.prefix, root_uid=os.getuid())
    assert "rg" in str(e.value.detail) and "644" in str(e.value.detail)


@pytest.mark.parametrize("rel", ["codex-path/rg", "bin/codex-code-mode-host", "codex-resources/zsh/bin/zsh"])
def test_v10_a_source_executable_that_is_not_executable_is_not_copied(env, rel):
    _codex_pkg(env, extra={rel: (MACHO + b"x", 0o644)})
    ok, why = _codex_copy(env)
    assert ok and "not copied" in why and rel.split("/")[-1] in why, why
    assert not os.path.lexists(env.prefix / "bin" / "codex")


def test_v11_an_exception_in_a_copy_step_reaches_the_sink(tmp_path):
    """RED at bb334bac: an exception skipped ``sink.append``, so the report step saw no failure."""
    from types import SimpleNamespace

    def boom(host, name, **kw):
        raise RuntimeError("kaboom")
    failed: list = []
    steps = RT.vendor_steps("darwin", "op", failed=failed, copy=boom,
                            lookup=lambda o: SimpleNamespace(pw_dir="/h", pw_uid=1))
    [claude, codex] = [s for s in steps if s.why.startswith("place root's copy")]
    ok, why = claude.call()
    assert not ok and "kaboom" in why and failed == [why]

    def nolook(o):
        raise OSError(5, "Input/output error")
    failed2: list = []
    steps = RT.vendor_steps("darwin", "op", failed=failed2, lookup=nolook)
    ok, why = [s for s in steps if s.why.startswith("place root's copy")][0].call()
    assert not ok and failed2 == [why]


def test_v12_a_codex_package_without_codex_resources_is_not_copied(env):
    """RED at bb334bac: a missing tree was skipped, so a package with no resources was published whole."""
    root, _ = _codex_pkg(env)
    import shutil
    shutil.rmtree(root / "codex-resources")
    ok, why = _codex_copy(env)
    assert ok and "not copied" in why and "codex-resources" in why, why
    assert not os.path.lexists(env.prefix / "bin" / "codex")


def test_v12_start_refuses_an_active_package_without_codex_resources(env):
    import shutil

    from levain.slot import start as SS

    _codex_pkg(env)
    _codex_copy(env)
    shutil.rmtree(_active_root(env.prefix) / "codex-resources")
    with pytest.raises(Refused) as e:
        SS.check_vendor_installed("darwin", "codex", prefix=env.prefix, root_uid=os.getuid())
    assert "codex-resources" in str(e.value.detail)


def test_v13_a_probe_that_raises_anything_is_a_row_not_a_crash(tmp_path):
    def state(n):
        raise TypeError("bad probe")
    from levain import doctor as D

    [r] = D._check_vendor_copies(host="darwin", store=tmp_path, home=str(tmp_path), state=state)
    assert r.warn and "UNREADABLE" in r.detail and "bad probe" in r.detail


# --- f3 (1026-14 RULINGS "vendorbin f2" V14-V15) ----------------------------------------------------------------------

def _versions(prefix: Path) -> list:
    return sorted(p.name for p in (prefix / "codex").iterdir() if not p.name.startswith("."))


def test_v14_an_upgrade_keeps_the_version_it_replaced(env):
    """RED at d8ab0b43: every other version was removed right after the switch, so a session started on v1 (codex
    resolves its PHYSICAL version dir at start) met ENOENT on its next spawn of ``codex-path/rg``."""
    _codex_pkg(env)
    assert _codex_copy(env)[0]
    running = os.path.realpath(_active_root(env.prefix) / "codex-path" / "rg")
    v1 = _active_root(env.prefix).name
    _v2(env)
    ok, why = _codex_copy(env)
    assert ok and "copied" in why, why
    v2 = _active_root(env.prefix).name
    assert v2 != v1 and os.path.isfile(running), running
    assert _versions(env.prefix) == sorted([v1, v2])
    _codex_pkg(env, extra={"bin/codex": (MACHO + b"v3", 0o755)})
    ok, why = _codex_copy(env)
    assert ok and "copied" in why, why
    v3 = _active_root(env.prefix).name
    assert _versions(env.prefix) == sorted([v1, v2, v3]) and os.path.exists(running), _versions(env.prefix)   # V18
    assert RT.unit_problem("darwin", "codex", prefix=env.prefix, root_uid=os.getuid()) == ""


def test_v14_a_failed_upgrade_keeps_both_versions_it_found(env):
    _codex_pkg(env)
    _codex_copy(env)
    _v2(env)
    _codex_copy(env)
    before = _versions(env.prefix)
    _codex_pkg(env, extra={"bin/codex": (MACHO + b"v3", 0o755)})

    def fsync(fd):
        raise OSError(5, "Input/output error")
    ok, why = _codex_copy(env, fsync=fsync)
    assert not ok and "unchanged" in why, why
    assert _versions(env.prefix) == before


def test_v15_two_concurrent_copies_take_turns(env):
    """RED at d8ab0b43: both runs used ``codex/.staging.partial``, so the second removed the first's half-built tree
    and the first failed."""
    import threading
    import time

    _codex_pkg(env)
    _codex_copy(env)
    _v2(env)
    second_started = threading.Event()
    seen: dict = {}
    results: dict = {}

    def slow_fsync(fd):
        if "waited" not in seen:
            seen["waited"] = True
            t2.start()
            second_started.wait(5)
            time.sleep(0.4)
            seen["second_done_early"] = not t2.is_alive()
        os.fsync(fd)

    def first():
        results[1] = _codex_copy(env, fsync=slow_fsync)

    def second():
        second_started.set()
        results[2] = _codex_copy(env)
    t1, t2 = threading.Thread(target=first), threading.Thread(target=second)
    t1.start()
    t1.join(30)
    t2.join(30)
    assert not t1.is_alive() and not t2.is_alive()
    assert results[1][0] and results[2][0], results
    assert seen["second_done_early"] is False
    assert "already" in results[2][1], results[2]
    assert RT.unit_problem("darwin", "codex", prefix=env.prefix, root_uid=os.getuid()) == ""
    assert (_active_root(env.prefix) / "codex-path" / "rg").read_bytes() == MACHO + b"rg-v2"
    assert not [p for p in (env.prefix / "codex").iterdir() if p.name.startswith(".staging")]


def test_v15_a_stale_staging_dir_from_a_crashed_run_is_removed(env):
    _codex_pkg(env)
    _codex_copy(env)
    stale = env.prefix / "codex" / ".staging.12345.dead.partial"
    _exe(stale / "bin" / "codex", MACHO + b"half")
    _v2(env)
    ok, why = _codex_copy(env)
    assert ok and "copied" in why, why
    assert not os.path.lexists(stale)
    assert not [p for p in (env.prefix / "codex").iterdir() if p.name.startswith(".staging")]


def test_v15_a_link_at_the_lock_is_refused(env):
    _codex_pkg(env)
    (env.prefix / "codex").mkdir()
    os.symlink(env.tmp / "elsewhere", env.prefix / "codex" / ".lock")
    ok, why = _codex_copy(env)
    assert not ok and ".lock" in why, why
    assert not os.path.lexists(env.tmp / "elsewhere")


# --- f4 (1026-14 RULINGS "vendorbin verify r3" V18-V21) -----------------------------------------------------------

def _v3(env):
    return _codex_pkg(env, extra={"bin/codex": (MACHO + b"v3", 0o755)})


def _fails_on(*paths: Path):
    """An fsync that raises EIO for a directory whose inode is one of ``paths`` (taken now)."""
    inos = {os.stat(p).st_ino for p in paths}

    def fsync(fd):
        st = os.fstat(fd)
        if stat.S_ISDIR(st.st_mode) and st.st_ino in inos:
            raise OSError(5, "Input/output error")
        os.fsync(fd)
    return fsync


def test_v18_two_upgrades_keep_the_version_a_live_session_started_on(env):
    """RED at 95eac6af: the second upgrade removed v1, which a session started before the first one still spawns
    ``codex-path/rg`` from (codex resolves its physical version dir at start)."""
    _codex_pkg(env)
    assert _codex_copy(env)[0]
    running = os.path.realpath(_active_root(env.prefix) / "codex-path" / "rg")
    _v2(env)
    assert _codex_copy(env)[0]
    _v3(env)
    ok, why = _codex_copy(env)
    assert ok and "copied" in why, why
    assert os.path.isfile(running), running
    assert len(_versions(env.prefix)) == 3, _versions(env.prefix)


def _three_versions(env) -> str:
    _codex_pkg(env)
    _codex_copy(env)
    _v2(env)
    _codex_copy(env)
    _v3(env)
    _codex_copy(env)
    return _active_root(env.prefix).name


def _prune(env, live):
    return RT.prune_vendor("darwin", "codex", live=live, prefix=env.prefix, fchown=env.fchown, chown=env.chown,
                           root_uid=os.getuid())


def test_v18_the_prune_keeps_every_version_while_a_session_is_live(env):
    _three_versions(env)
    before = _versions(env.prefix)
    ok, why = _prune(env, lambda: ("sess-1",))
    assert ok and "sess-1" in why and "kept" in why, why
    assert _versions(env.prefix) == before


def test_v18_the_prune_removes_every_non_active_version_once_no_session_is_live(env):
    v3 = _three_versions(env)
    stale = env.prefix / "codex" / ".staging.12345.dead.partial"
    _exe(stale / "bin" / "codex", MACHO + b"half")
    ok, why = _prune(env, lambda: ())
    assert ok and "removed" in why, why
    assert _versions(env.prefix) == [v3] and not os.path.lexists(stale)
    assert os.path.isfile(env.prefix / "codex" / ".lock")
    assert RT.unit_problem("darwin", "codex", prefix=env.prefix, root_uid=os.getuid()) == ""


def test_v18_the_prune_keeps_everything_when_the_live_check_fails(env):
    _three_versions(env)
    before = _versions(env.prefix)

    def live():
        raise OSError(5, "Input/output error")
    ok, why = _prune(env, live)
    assert ok and "kept" in why, why
    assert _versions(env.prefix) == before


def test_v18_the_prune_with_no_active_version_removes_nothing(env):
    _three_versions(env)
    before = _versions(env.prefix)
    os.unlink(env.prefix / "bin" / "codex")
    ok, why = _prune(env, lambda: ())
    assert ok and "nothing" in why, why
    assert _versions(env.prefix) == before


def test_v18_repair_prunes_after_the_vendor_copies_and_never_stops_on_it(tmp_path):
    from tests.test_repair_plan import _pre_change, _plan

    facts, rf = _pre_change(tmp_path)
    steps = _plan(facts, rf).steps
    whys = [s.why for s in steps]
    prune = [i for i, w in enumerate(whys) if w.startswith("remove the old versions")]
    copies = [i for i, w in enumerate(whys) if w.startswith("place root's copy")]
    assert len(prune) == 1 and copies and prune[0] > max(copies), whys
    assert steps[prune[0]].allow_fail


def test_v18_the_live_check_is_the_runs_quiesce_and_fails_closed_without_it(tmp_path, monkeypatch):
    from levain.slot import install_helper as IH

    store = tmp_path / "L"
    (store / "slots").mkdir(parents=True)
    monkeypatch.setattr(IH, "live_lease_sessions", lambda d: ("from-records",))
    assert IH.vendor_prune_live(store) == ("from-records",)          # no pool.lock: no prepare can lease
    (store / "slots" / "pool.lock").write_bytes(b"")
    assert IH.vendor_prune_live(store)                              # a lock no run holds: counted live
    monkeypatch.setattr(IH, "live_lease_sessions", lambda d: ())
    assert IH.vendor_prune_live(store)

    class Held:
        held = True

        def live(self):
            return ("from-quiesce",)
    monkeypatch.setitem(IH._QUIESCING, IH._store_key(store), Held())
    assert IH.vendor_prune_live(store) == ("from-quiesce",)


def test_v19_a_staged_dir_fsync_failure_aborts_before_the_switch(env):
    """RED at 95eac6af: a staged directory's fsync failure was a warning, and the link switched to it."""
    _codex_pkg(env)
    _codex_copy(env)
    v1 = _active_root(env.prefix).name
    _v2(env)
    bin_dir, pdir = env.prefix / "bin", env.prefix / "codex"
    keep = {os.stat(p).st_ino for p in (bin_dir, pdir, env.prefix)}

    def fsync(fd):
        st = os.fstat(fd)
        if stat.S_ISDIR(st.st_mode) and st.st_ino not in keep:
            raise OSError(5, "Input/output error")
        os.fsync(fd)
    ok, why = _codex_copy(env, fsync=fsync)
    assert not ok and "unchanged" in why, why
    assert _active_root(env.prefix).name == v1


def test_v19_the_version_dirs_parent_fsync_failure_aborts_before_the_switch(env):
    _codex_pkg(env)
    _codex_copy(env)
    v1 = _active_root(env.prefix).name
    _v2(env)
    ok, why = _codex_copy(env, fsync=_fails_on(env.prefix / "codex"))
    assert not ok and "unchanged" in why, why
    assert _active_root(env.prefix).name == v1
    ok, why = _codex_copy(env)                       # the next repair finishes it
    assert ok, why
    assert (_active_root(env.prefix) / "codex-path" / "rg").read_bytes() == MACHO + b"rg-v2"


def test_v19_the_link_dirs_fsync_failure_after_the_switch_is_indeterminate(env):
    _codex_pkg(env)
    _codex_copy(env)
    _v2(env)
    ok, why = _codex_copy(env, fsync=_fails_on(env.prefix / "bin"))
    assert not ok and "indeterminate" in why, why
    assert (_active_root(env.prefix) / "codex-path" / "rg").read_bytes() == MACHO + b"rg-v2"
    ok, why = _codex_copy(env, fsync=_fails_on(env.prefix / "bin"))   # still not durable: never "already matches"
    assert not ok and "indeterminate" in why, why
    ok, why = _codex_copy(env)
    assert ok and "already" in why, why


def test_v20_a_rollback_reuses_the_kept_version_and_never_removes_it(env):
    """RED at 95eac6af: a copy back to v1's bytes removed v1's kept directory and renamed a new copy over its name,
    so a session still running on it met ENOENT in between."""
    _codex_pkg(env)
    _codex_copy(env)
    v1 = _active_root(env.prefix)
    ino = os.stat(v1).st_ino
    _v2(env)
    _codex_copy(env)
    _codex_pkg(env)
    ok, why = _codex_copy(env)
    assert ok and "copied" not in why, why
    assert _active_root(env.prefix) == v1.resolve() and os.stat(v1).st_ino == ino
    assert RT.unit_problem("darwin", "codex", prefix=env.prefix, root_uid=os.getuid()) == ""


def test_v20_a_damaged_kept_version_is_never_removed_by_a_copy(env):
    _codex_pkg(env)
    _codex_copy(env)
    v1 = _active_root(env.prefix)
    ino = os.stat(v1).st_ino
    _v2(env)
    _codex_copy(env)
    os.chmod(v1 / "codex-resources" / "voice", 0o775)                # v1's dir no longer passes _tree_problem
    _codex_pkg(env)
    ok, why = _codex_copy(env)
    assert ok and "copied" in why, why
    now = _active_root(env.prefix)
    assert now != v1.resolve() and now.name.startswith(v1.name + "."), now
    assert os.stat(v1).st_ino == ino
    assert RT.unit_problem("darwin", "codex", prefix=env.prefix, root_uid=os.getuid()) == ""


def test_v21_a_structural_problem_in_a_matching_active_version_is_rebuilt(env):
    """RED at 95eac6af: a link where a directory belongs made the ``same`` branch fail, and every repair after it."""
    _codex_pkg(env)
    _codex_copy(env)
    (env.tmp / "elsewhere").mkdir()
    os.symlink(env.tmp / "elsewhere", _active_root(env.prefix) / "codex-resources" / "evil")
    ok, why = _codex_copy(env)
    assert ok and "copied" in why, why
    assert RT.unit_problem("darwin", "codex", prefix=env.prefix, root_uid=os.getuid()) == ""
    ok, why = _codex_copy(env)
    assert ok and "already" in why, why


def _failing_vendor_steps(failed):
    def fake(host, operator, *, failed, **kw):
        def call():
            failed.append("codex: copying failed (EIO)")
            return False, "codex: copying failed (EIO)"
        return [RT.Step("place root's copy of codex", call=call, allow_fail=True)]
    return fake


def test_v23_a_vendor_copy_failing_on_every_run_does_not_stop_setup_before_the_operator_record(monkeypatch):
    from levain.firing.hands import Plan, run_plan
    from levain.slot import install_helper as IH
    from levain.firing import hands
    from tests.test_slot_install_plan import _facts, _setup

    real = hands.shutil.which
    monkeypatch.setattr(hands.shutil, "which", lambda n, path=None: real(n, path=path) or f"/usr/sbin/{n}")
    monkeypatch.setattr(RT, "vendor_steps", _failing_vendor_steps(None))
    steps = list(IH.plan_setup(_setup(), _facts("linux")).steps)
    whys = [s.why for s in steps]
    copy = next(i for i, w in enumerate(whys) if w.startswith("place root's copy"))
    report = next(i for i, w in enumerate(whys) if w.startswith("report the vendor CLI copies"))
    record = next(i for i, w in enumerate(whys) if w.startswith("record ") and "one operator" in w)
    assert copy < report < record and steps[report].allow_fail
    ran = []
    tail = RT.Step("the operator record", call=lambda: (ran.append(1), (True, ""))[1])
    plan = Plan("linux", "op", "h", 1, Path("/x"), Path("/y"), steps=(steps[copy], steps[report], tail))
    out: list = []
    assert run_plan(plan, dry_run=False, emit=out.append) == 0
    assert ran == [1]
    assert any(o.strip().startswith("skipped (") and "codex" in o and "EIO" in o for o in out), out


def test_v23_repair_with_the_same_failure_still_fails_the_run(monkeypatch, tmp_path):
    from levain.firing.hands import Plan, run_plan
    from tests.test_repair_plan import _pre_change, _plan

    monkeypatch.setattr(RT, "vendor_steps", _failing_vendor_steps(None))
    facts, rf = _pre_change(tmp_path)
    steps = list(_plan(facts, rf).steps)
    whys = [s.why for s in steps]
    copy = next(i for i, w in enumerate(whys) if w.startswith("place root's copy"))
    report = next(i for i, w in enumerate(whys) if w.startswith("report the vendor CLI copies"))
    assert not steps[report].allow_fail
    plan = Plan("linux", "op", "h", 1, Path("/x"), Path("/y"), steps=(steps[copy], steps[report]))
    out: list = []
    assert run_plan(plan, dry_run=False, emit=out.append) == 1
    assert any("FAILED" in o and "codex" in o for o in out), out


def test_v26_an_oserror_reading_the_manifest_is_a_failed_copy_not_a_raise(env, monkeypatch):
    """RED at a9ffc283: the manifest ``os.pread`` sat above the ``try``, so an EIO there escaped ``copy_vendor``."""
    _codex_pkg(env)
    _codex_copy(env)
    v1 = _active_root(env.prefix).name
    _v2(env)
    real = os.pread

    def pread(fd, n, off):
        if n == 1 << 16:
            raise OSError(5, "Input/output error")
        return real(fd, n, off)
    monkeypatch.setattr(os, "pread", pread)
    ok, why = _codex_copy(env)
    assert not ok and "unchanged" in why, why
    assert _active_root(env.prefix).name == v1


def test_v28_a_matching_active_version_fix_tree_leaves_unfixed_is_rebuilt(env, monkeypatch):
    """RED at a9ffc283: ``_fix_tree`` returned without raising, ``unit_problem`` stayed non-empty, and the ``same``
    branch failed with "repair could not fix it" on every repair after."""
    _codex_pkg(env)
    _codex_copy(env)
    v1 = _active_root(env.prefix).name
    os.chmod(_active_root(env.prefix) / "codex-path" / "rg", 0o600)
    assert RT.unit_problem("darwin", "codex", prefix=env.prefix, root_uid=os.getuid())
    monkeypatch.setattr(RT, "_fix_tree", lambda *a, **k: None)
    ok, why = _codex_copy(env)
    assert ok and "copied" in why, why
    assert _active_root(env.prefix).name != v1
    assert RT.unit_problem("darwin", "codex", prefix=env.prefix, root_uid=os.getuid()) == ""


def _prune_run(env, monkeypatch, fsync):
    """The repair plan's R10 copy step then its prune step for codex, sharing one ``failed`` sink, on ``env``."""
    from levain.slot import install_helper as IH

    failed: list = []
    monkeypatch.setitem(RT.VENDOR_PREFIX, "darwin", env.prefix)
    real = RT.prune_vendor
    monkeypatch.setattr(RT, "prune_vendor", lambda host, name, **kw: real(
        host, name, prefix=env.prefix, fchown=env.fchown, chown=env.chown, root_uid=os.getuid(), **kw))
    monkeypatch.setattr(IH, "vendor_prune_live", lambda L: ())
    copy = RT.vendor_steps("darwin", "op", failed=failed, lookup=lambda n: type("P", (), {
        "pw_dir": str(env.home), "pw_uid": os.getuid()})(),
        copy=lambda host, name, **kw: env.copy(name, fsync=fsync) if name == "codex" else (True, "skip"))
    prune = IH.vendor_prune_steps("darwin", store=env.tmp, failed=failed)
    codex_copy = next(s for s in copy if "codex" in s.why and s.call is not None)
    codex_prune = next(s for s in prune if "codex" in s.why)
    return codex_copy, codex_prune


def test_v25_a_prune_after_an_indeterminate_publish_keeps_the_previous_version(env, monkeypatch):
    """RED at a9ffc283: the prune ran after a copy whose ``bin`` fsync failed and removed the previous version."""
    _codex_pkg(env)
    _codex_copy(env)
    before = _versions(env.prefix)
    _v2(env)
    copy, prune = _prune_run(env, monkeypatch, _fails_on(env.prefix / "bin"))
    ok, why = copy.call()
    assert not ok and "indeterminate" in why, why
    assert len(_versions(env.prefix)) == 2
    ok, why = prune.call()
    assert ok and "kept" in why and "codex" in why, why
    assert len(_versions(env.prefix)) == 2 and before[0] in _versions(env.prefix)


def test_v25_a_prune_after_a_durable_copy_removes_the_old_versions_as_before(env, monkeypatch):
    _codex_pkg(env)
    _codex_copy(env)
    _v2(env)
    copy, prune = _prune_run(env, monkeypatch, os.fsync)
    ok, why = copy.call()
    assert ok, why
    ok, why = prune.call()
    assert ok and "removed" in why, why
    assert _versions(env.prefix) == [_active_root(env.prefix).name]
