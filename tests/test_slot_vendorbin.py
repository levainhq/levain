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

    def fchown(self, fd: int, uid: int, gid: int) -> None:
        self.chowned.append((uid, gid))

    def copy(self, name: str, **kw):
        return RT.copy_vendor("darwin", name, home=str(self.home), operator_uid=os.getuid(), prefix=self.prefix,
                              search=self.search, machine="arm64", fchown=self.fchown, root_uid=os.getuid(), **kw)


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
                             search=e.search, machine="x86_64", fchown=e.fchown)
    assert ok and "skipped" in why
    _exe(e.brew / "claude", ELF)
    ok, why = RT.copy_vendor("linux", "claude", home=str(e.home), operator_uid=os.getuid(), prefix=e.prefix,
                             search=e.search, machine="x86_64", fchown=e.fchown)
    assert ok and "copied" in why


# --- the drift read (doctor's row reads it) ---------------------------------------------------------------------------

def test_vendor_state(env):
    kw = dict(prefix=env.prefix, search=env.search, machine="arm64")
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
    assert any(w.startswith("place root's copy (0755) of the claude CLI") for w in whys)
    assert any(w.startswith("place root's copy (0755) of the codex CLI") for w in whys)


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
                                      machine="arm64")
    [r] = D._check_vendor_copies(host="darwin", store=env.tmp, home=str(env.home), state=state)
    assert r.warn and "claude STALE" in r.detail and "codex not on this machine" in r.detail
