"""The bwrap plan emits every /dev/null file mask after every bind with a host source.

A later mount on the same dentry wins, so a self-bind emitted after a mask would put the masked file
back whenever the two spellings name one dentry (RUN on argushub 2026-10-03, bwrap 0.9.0: `dir//D`,
`dir/./D` and `dir/x/../D` self-bound after a mask on `dir/D` printed it; before the mask, refused).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from levain.firing import confinement
from levain.firing.confinement import (
    DERIVE_TRUST_ENV,
    ConfinementError,
    _bwrap_argv,
    _refuse_bind_after_mask,
    build_policy,
)
from tests.test_floor_project_memory import _case_insensitive, _entity, home, live, _LINUX  # noqa: F401

_BINDS = ("--bind", "--ro-bind", "--bind-try", "--ro-bind-try")


def _assert_masks_last(argv: list[str]) -> None:
    ops = [(i, argv[i + 1]) for i in range(len(argv) - 2) if argv[i] in _BINDS]
    masks = [i for i, src in ops if src == "/dev/null"]
    hosts = [i for i, src in ops if src != "/dev/null"]
    assert masks, "the plan masks nothing; this check would be vacuous"
    assert max(hosts) < min(masks), [argv[i:i + 3] for i, _ in ops]


def _write_only_link(root: Path, target: Path, monkeypatch) -> Path:
    """A derive-trust symlink in a non-writable directory: the floor write-denies its target and
    step (5) self-binds that target read-only (the route a self-bind of an arbitrary file takes)."""
    opt = root / "opt"
    opt.mkdir()
    (opt / "trust.json").symlink_to(target)
    opt.chmod(0o555)
    monkeypatch.setenv(DERIVE_TRUST_ENV, str(opt / "trust.json"))
    return opt


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores the 0555 precondition")
@pytest.mark.parametrize("ssh_mode", ["agent", "raw"])
def test_every_mask_follows_every_host_bind(home, tmp_path, monkeypatch, ssh_mode):
    ssh = home / ".ssh"
    ssh.mkdir()
    for n in ("authorized_keys", "known_hosts", "config", "id_ed25519"):
        (ssh / n).write_text(n)
    secret = tmp_path / "secrets" / "token"
    secret.parent.mkdir()
    secret.write_text("SECRET")
    readable = tmp_path / "secrets" / "notes"
    readable.write_text("readable")
    opt = _write_only_link(tmp_path, readable, monkeypatch)
    try:
        argv = _bwrap_argv(build_policy(_entity(home), deny_files=(secret,), ssh_mode=ssh_mode))
    finally:
        opt.chmod(0o755)
    t = str(readable.resolve())
    assert ["--ro-bind", t, t] in [argv[i:i + 3] for i in range(len(argv) - 2)]   # a step (5) self-bind
    _assert_masks_last(argv)


def test_the_plan_refuses_a_bind_after_a_mask() -> None:
    ok = ["bwrap", "--bind", "/", "/", "--ro-bind", "/a", "/a", "--ro-bind", "/dev/null", "/b",
          "--tmpfs", "/c", "--remount-ro", "/c"]
    _refuse_bind_after_mask(ok)
    for op in _BINDS:
        bad = ["bwrap", "--ro-bind", "/dev/null", "/b", op, "/a", "/a"]
        with pytest.raises(ConfinementError, match="re-expose"):
            _refuse_bind_after_mask(bad)


def test_the_plan_runs_the_order_check_on_what_it_returns(home, monkeypatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(confinement, "_refuse_bind_after_mask", lambda argv: seen.append(list(argv)))
    argv = _bwrap_argv(build_policy(_entity(home)))
    assert seen and seen[-1] == argv


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores the 0555 precondition")
def test_a_case_sensitive_sibling_of_a_denied_file_is_self_bound_not_masked(home, tmp_path, monkeypatch):
    """0.5.4 known open issue: the case-folded compare matched `config` against a denied `CONFIG`
    on a case-sensitive volume, where they are two files, and hid the write-only one."""
    d = tmp_path / "secrets"
    d.mkdir()
    if _case_insensitive(d):
        pytest.skip("needs a case-sensitive volume")
    denied, sibling = d / "CONFIG", d / "config"
    denied.write_text("DENIED")
    sibling.write_text("READABLE")
    opt = _write_only_link(tmp_path, sibling, monkeypatch)
    try:
        argv = _bwrap_argv(build_policy(_entity(home), deny_files=(denied,)))
    finally:
        opt.chmod(0o755)
    binds = [argv[i:i + 3] for i in range(len(argv) - 2) if argv[i] == "--ro-bind"]
    t = str(sibling.resolve())
    assert ["--ro-bind", t, t] in binds
    assert ["--ro-bind", "/dev/null", t] not in binds
    assert ["--ro-bind", "/dev/null", str(denied.resolve())] in binds


@live
@pytest.mark.skipif(not _LINUX, reason="the case-sensitive sibling is a Linux bwrap plan question")
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores the 0555 precondition")
def test_live_a_case_sensitive_sibling_reads_and_the_denied_file_does_not(home, tmp_path, monkeypatch):
    from levain.firing.confinement import select_provider
    d = home / "secrets"
    d.mkdir()
    if _case_insensitive(d):
        pytest.skip("needs a case-sensitive volume")
    (d / "CONFIG").write_text("DENIED\n")
    (d / "config").write_text("READABLE\n")
    opt = _write_only_link(home, d / "config", monkeypatch)
    try:
        with select_provider().spawn_shell(build_policy(_entity(home), deny_files=(d / "CONFIG",))) as sh:
            r = sh.run(f"cat {d / 'config'} 2>&1", timeout=20)
            assert r.exit_code == 0 and "READABLE" in r.output, r.output
            r = sh.run(f"cat {d / 'CONFIG'} 2>&1", timeout=20)
            assert r.exit_code != 0 and "DENIED" not in r.output, r.output
            r = sh.run(f"echo x >> {d / 'config'} 2>&1", timeout=20)
            assert r.exit_code != 0, r.output   # still write-denied
    finally:
        opt.chmod(0o755)
