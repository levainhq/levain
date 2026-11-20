"""ND-8 step 5 (levain#47): the night seat's laptop-side ssh config and the pinned ``-F`` argument.

Nothing here connects anywhere, reads a key or touches the real ``~/.levain``: the writer takes an injectable home (a
tmp_path), the argv tests only build lists, and the one ssh run is ``ssh -G`` (print the effective config of a written
file: no connection, no name lookup), skipped where no ssh binary exists.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from levain.desk import effects, night_ssh

ADDR = "taoserv.example.net"


def test_render_is_the_runbooks_block_with_absolute_paths(tmp_path):
    text = night_ssh.render_config(ADDR, tmp_path)
    nd = tmp_path / ".levain" / "night"
    assert text == (
        "Host taoserv-night\n"
        f"    HostName {ADDR}\n"
        "    User nightkey\n"
        f'    IdentityFile "{nd}/taoserv_night"\n'
        "    IdentitiesOnly yes\n"
        "    IdentityAgent none\n"
        f'    UserKnownHostsFile "{nd}/known_hosts"\n'
        "    GlobalKnownHostsFile none\n"
        "    StrictHostKeyChecking yes\n"
        "    RequestTTY no\n"
        "    ForwardAgent no\n")


def test_the_alias_is_the_one_effects_uses(tmp_path):
    assert f"Host {effects.NIGHT_ALIAS['taoserv']}\n" in night_ssh.render_config(ADDR, tmp_path)


@pytest.mark.parametrize("bad", ["", "a b", "a\nHost evil", "a\tb", "-oProxyCommand=x", "a;b", "a$(x)", "x" * 300,
                                 "REPLACE-WITH-TAOSERV-ADDRESS", "replace-with-taoserv-address", "é.example", "a/b"])
def test_a_hostname_that_is_not_a_plain_address_is_refused(tmp_path, bad):
    with pytest.raises(ValueError):
        night_ssh.render_config(bad, tmp_path)


def test_a_non_str_hostname_is_refused(tmp_path):
    for bad in (None, b"a.example", 5):
        with pytest.raises(ValueError):
            night_ssh.render_config(bad, tmp_path)


def test_an_address_with_a_host_key_alias_renders_both_lines(tmp_path):
    text = night_ssh.render_config("172.233.220.105", tmp_path, host_key_alias="taoserv.example.net")
    lines = text.splitlines()
    assert "    HostName 172.233.220.105" in lines and "    HostKeyAlias taoserv.example.net" in lines
    assert lines.index("    HostKeyAlias taoserv.example.net") == lines.index("    HostName 172.233.220.105") + 1
    assert text.count("HostKeyAlias") == 1


def test_no_alias_renders_no_host_key_alias_line(tmp_path):
    assert "HostKeyAlias" not in night_ssh.render_config(ADDR, tmp_path)
    assert night_ssh.render_config(ADDR, tmp_path, host_key_alias=None) == night_ssh.render_config(ADDR, tmp_path)


@pytest.mark.parametrize("bad", ["", "a b", "a\nHost evil", "a\tb", "a/b", "-oProxyCommand=x", "-a.example", "é.example",
                                 "a;b", "x" * 300, "REPLACE-WITH-TAOSERV-ADDRESS", "replace-with-taoserv-address"])
def test_a_host_key_alias_that_is_not_a_plain_name_is_refused(tmp_path, bad):
    with pytest.raises(ValueError):
        night_ssh.render_config(ADDR, tmp_path, host_key_alias=bad)
    with pytest.raises(ValueError):
        night_ssh.write_config(ADDR, tmp_path, host_key_alias=bad)
    assert not (tmp_path / ".levain").exists()                                   # refused before anything is written


def test_a_non_str_host_key_alias_is_refused(tmp_path):
    for bad in (b"a.example", 5):
        with pytest.raises(ValueError):
            night_ssh.render_config(ADDR, tmp_path, host_key_alias=bad)


def test_write_config_writes_the_alias_and_ssh_G_reads_it(tmp_path):
    cfg = night_ssh.write_config("172.233.220.105", tmp_path, host_key_alias="taoserv.example.net")
    assert cfg.read_text() == night_ssh.render_config("172.233.220.105", tmp_path, host_key_alias="taoserv.example.net")
    eff = _ssh_G(cfg)
    assert eff["hostname"] == "172.233.220.105" and eff["hostkeyalias"] == "taoserv.example.net"


def test_write_config_writes_under_the_injected_home_only(tmp_path):
    path = night_ssh.write_config(ADDR, tmp_path)
    assert path == tmp_path / ".levain" / "night" / "ssh_config" == night_ssh.config_path(tmp_path)
    assert path.read_text() == night_ssh.render_config(ADDR, tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert sorted(p.name for p in path.parent.iterdir()) == ["ssh_config"]         # no key, no known_hosts written


def test_write_config_replaces_and_leaves_no_temp(tmp_path):
    night_ssh.write_config("old.example", tmp_path)
    path = night_ssh.write_config(ADDR, tmp_path)
    assert f"HostName {ADDR}" in path.read_text() and "old.example" not in path.read_text()
    assert [p.name for p in path.parent.iterdir()] == ["ssh_config"]


def test_write_config_refuses_a_symlinked_target(tmp_path):
    nd = tmp_path / ".levain" / "night"
    nd.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (nd / "ssh_config").symlink_to(victim)
    with pytest.raises(ValueError):
        night_ssh.write_config(ADDR, tmp_path)
    assert victim.read_text() == "keep"


# ---- G1: the global known-hosts files are off, checked by ssh's own parser (ssh -G: no connection, no DNS) ---------
def _ssh_G(cfg: Path) -> dict[str, str]:
    ssh = shutil.which("ssh", path="/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin")
    if ssh is None:
        pytest.skip("no ssh binary on this machine: the effective-config check needs `ssh -G`")
    done = subprocess.run([ssh, "-G", "-F", str(cfg), "taoserv-night"], stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=20, check=False, env={"PATH": "/usr/bin:/bin", "HOME": str(cfg.parent)})
    assert done.returncode == 0, done.stderr
    out: dict[str, str] = {}
    for line in done.stdout.splitlines():
        key, _, val = line.partition(" ")
        out.setdefault(key, val)
    return out


def test_the_rendered_config_turns_global_known_hosts_off(tmp_path):
    text = night_ssh.render_config(ADDR, tmp_path)
    assert "    GlobalKnownHostsFile none\n" in text and "    StrictHostKeyChecking yes\n" in text


def test_ssh_G_reads_only_the_pinned_known_hosts(tmp_path):
    cfg = night_ssh.write_config(ADDR, tmp_path)
    nd = cfg.parent
    eff = _ssh_G(cfg)
    assert eff["hostname"] == ADDR and eff["user"] == "nightkey"
    assert eff["globalknownhostsfile"] == "none"
    assert eff["userknownhostsfile"] == f"{nd}/known_hosts"
    assert eff["stricthostkeychecking"] == "true"
    assert eff["identityfile"] == f"{nd}/taoserv_night" and eff["identitiesonly"] == "yes"
    assert eff["identityagent"] == "none" and eff["forwardagent"] == "no"


# ---- G2: a unique, exclusive, 0600 temp; only our own temp is removed ------------------------------------------------
def test_a_stale_wide_temp_does_not_set_the_config_mode(tmp_path):
    nd = tmp_path / ".levain" / "night"
    nd.mkdir(parents=True)
    stale = nd / "ssh_config.tmp"
    stale.write_text("someone else's\n")
    os.chmod(stale, 0o644)
    path = night_ssh.write_config(ADDR, tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text() == night_ssh.render_config(ADDR, tmp_path)
    assert stale.read_text() == "someone else's\n" and stat.S_IMODE(stale.stat().st_mode) == 0o644


def test_the_config_is_0600_under_a_restrictive_umask(tmp_path):
    (tmp_path / ".levain" / "night").mkdir(parents=True)          # the directories exist; the umask strips the file's w
    old = os.umask(0o277)
    try:
        path = night_ssh.write_config(ADDR, tmp_path)
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_each_write_uses_its_own_temp_name(tmp_path, monkeypatch):
    seen: list[str] = []
    real = os.replace

    def spy(src, dst):
        seen.append(os.fspath(src))
        return real(src, dst)

    monkeypatch.setattr(night_ssh.os, "replace", spy)
    night_ssh.write_config(ADDR, tmp_path)
    night_ssh.write_config(ADDR, tmp_path)
    nd = tmp_path / ".levain" / "night"
    assert len(seen) == 2 and seen[0] != seen[1]
    assert all(Path(s).parent == nd and Path(s).name != "ssh_config.tmp" for s in seen)


def test_a_failed_replace_removes_only_its_own_temp(tmp_path, monkeypatch):
    nd = tmp_path / ".levain" / "night"
    nd.mkdir(parents=True)
    (nd / "ssh_config.tmp").write_text("not mine\n")

    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(night_ssh.os, "replace", boom)
    with pytest.raises(OSError):
        night_ssh.write_config(ADDR, tmp_path)
    assert sorted(p.name for p in nd.iterdir()) == ["ssh_config.tmp"]
    assert (nd / "ssh_config.tmp").read_text() == "not mine\n"


# ---- G3: the home path: absolute, no control character, `%` or `$`, quoted, UTF-8 -----------------------------------
@pytest.mark.parametrize("bad", ["", "rel/home", ".", "/a\nHost evil", "/a\tb", "/a\rb", "/a\x7fb", "/a\x00b",
                                 "/Users/a%d", "/Users/${HOME}", "/Users/a$b", '/Users/a"b', "/Users/a\\b"])
def test_a_home_that_cannot_be_written_safely_is_refused(bad):
    with pytest.raises(ValueError):
        night_ssh.render_config(ADDR, bad)


def test_a_non_str_home_is_refused():
    for bad in (None, b"/tmp/x", 5):
        with pytest.raises(ValueError):
            night_ssh.render_config(ADDR, bad)


def test_a_relative_home_is_refused_by_the_writer_too(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        night_ssh.write_config(ADDR, "rel")
    assert not (tmp_path / "rel").exists()


def test_a_home_with_a_space_is_quoted_and_ssh_reads_it_whole(tmp_path):
    home = tmp_path / "Night Seat #1"
    home.mkdir()
    cfg = night_ssh.write_config(ADDR, home)
    nd = home / ".levain" / "night"
    assert f'    IdentityFile "{nd}/taoserv_night"\n' in cfg.read_text()
    eff = _ssh_G(cfg)
    assert eff["identityfile"] == f"{nd}/taoserv_night"
    assert eff["userknownhostsfile"] == f"{nd}/known_hosts"


def test_a_non_ascii_home_is_written_as_utf8(tmp_path):
    home = tmp_path / "rené"
    home.mkdir()
    cfg = night_ssh.write_config(ADDR, home)
    assert f'"{home}/.levain/night/taoserv_night"' in cfg.read_bytes().decode("utf-8")


# ---- gemini's fourth: the night directory itself is not followed through a symlink ----------------------------------
@pytest.mark.parametrize("link", [".levain", ".levain/night"])
def test_a_symlinked_night_directory_is_refused_and_not_chmodded(tmp_path, link):
    victim = tmp_path / "victim"
    (victim / "night").mkdir(parents=True)
    os.chmod(victim, 0o755)
    os.chmod(victim / "night", 0o755)
    home = tmp_path / "home"
    if link == ".levain":
        home.mkdir()
        (home / ".levain").symlink_to(victim)
    else:
        (home / ".levain").mkdir(parents=True)
        (home / ".levain" / "night").symlink_to(victim / "night")
    with pytest.raises(ValueError):
        night_ssh.write_config(ADDR, home)
    assert stat.S_IMODE(victim.stat().st_mode) == 0o755
    assert stat.S_IMODE((victim / "night").stat().st_mode) == 0o755
    assert list((victim / "night").iterdir()) == []


def test_write_config_does_not_touch_the_real_home(tmp_path, monkeypatch):
    real = Path(os.path.expanduser("~")) / ".levain" / "night" / "ssh_config"
    before = real.stat().st_mtime_ns if real.exists() else None
    night_ssh.write_config(ADDR, tmp_path)
    assert (real.stat().st_mtime_ns if real.exists() else None) == before


# ---- the -F pin in effects._night_argv -----------------------------------------------------------------------------
def test_argv_pins_F_to_the_night_config_exactly(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = str(tmp_path / ".levain" / "night" / "ssh_config")
    assert effects.diagnosis_argv("taoserv", "status", "php8.3-fpm") == [
        "/usr/bin/ssh", "-F", cfg, "-T", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o",
        "ClearAllForwardings=yes", "-o", "StrictHostKeyChecking=yes", "taoserv-night", "status", "php8.3-fpm"]
    assert effects.restart_argv("taoserv", "apache2") == [
        "/usr/bin/ssh", "-F", cfg, "-T", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o",
        "ClearAllForwardings=yes", "-o", "StrictHostKeyChecking=yes", "taoserv-night", "restart_service", "apache2"]


def test_argv_F_is_the_file_the_writer_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    written = night_ssh.write_config(ADDR, tmp_path)
    argv = effects.diagnosis_argv("taoserv", "disk")
    assert argv[argv.index("-F") + 1] == str(written)
    assert argv.count("-F") == 1 and argv.index("-F") < argv.index("taoserv-night")


def test_argv_F_follows_the_runner_home_and_nothing_else(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert effects._runner_env()["HOME"] == str(tmp_path)
    argv = effects.diagnosis_argv("taoserv", "memory")
    assert argv[argv.index("-F") + 1].startswith(effects._runner_env()["HOME"] + "/")
