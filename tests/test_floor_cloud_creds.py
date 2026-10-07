"""The cloud CLI credential caches in the standard cred floor (lane P2's research note, item 4).

``~/.aws/{sso,cli,login,boto}/cache``, ``~/.config/gcloud`` and ``~/.azure`` each hold a live token
the CLI writes itself, and were readable by a confined entity in every drive. These tests read the
policy, the rendered Seatbelt text and the bwrap argv as data; nothing here runs a sandbox. Linux
behaviour is selected by patching ``platform.system``, which is what the policy code consults.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from levain.firing import confinement as C
from levain.firing.confinement import (
    SeatbeltProvider,
    build_policy,
    cred_floor_label,
    crown_jewel_reason,
    refresh_socket_denies,
)

_AWS_CACHES = (".aws/sso/cache", ".aws/cli/cache", ".aws/login/cache", ".aws/boto/cache")


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    for var in ("XDG_RUNTIME_DIR", "GIT_CONFIG_GLOBAL",
                *{v for v, _, _ in C._CRED_OVERRIDES}):   # a CI runner sets XDG_CONFIG_HOME
        monkeypatch.delenv(var, raising=False)
    return h


@pytest.fixture
def linux(monkeypatch) -> None:
    monkeypatch.setattr(C.platform, "system", lambda: "Linux")


def _entity(home: Path) -> Path:
    d = home / "ent"
    (d / ".levain").mkdir(parents=True)
    return d


def _ops(argv: list[str], op: str) -> list[list[str]]:
    width = {"--tmpfs": 1, "--remount-ro": 1}.get(op, 2)
    return [argv[i + 1:i + 1 + width] for i, a in enumerate(argv) if a == op]


def test_macos_denies_every_cloud_cache_whether_or_not_it_exists(home: Path, monkeypatch) -> None:
    monkeypatch.setattr(C.platform, "system", lambda: "Darwin")
    policy = build_policy(_entity(home), deny_standard_creds=True)
    profile = SeatbeltProvider().render_profile(policy)
    for rel in (*_AWS_CACHES, ".config/gcloud", ".azure"):
        assert home / rel in policy.deny_read_write, rel
        assert f'(subpath "{home / rel}")' in profile, rel
    assert home / ".aws" / "cli" not in policy.deny_read_write   # it holds `alias`
    assert crown_jewel_reason(policy, home / ".azure" / "msal_token_cache.json") is not None
    assert crown_jewel_reason(policy, home / ".config" / "gcloud" / "credentials.db") is not None
    assert crown_jewel_reason(policy, home / ".aws" / "sso" / "cache" / "x.json") is not None
    assert crown_jewel_reason(policy, home / ".aws" / "config") is None


def test_the_cloud_caches_are_off_with_the_cred_floor(home: Path) -> None:
    policy = build_policy(_entity(home))
    assert crown_jewel_reason(policy, home / ".azure" / "msal_token_cache.json") is None
    assert policy.cred_dir_sources == ()


def test_the_banner_label_names_the_cloud_caches() -> None:
    label = cred_floor_label("Linux")
    for name in ("~/.aws/sso/cache", "~/.aws/cli/cache", "~/.aws/login/cache", "~/.aws/boto/cache",
                 "~/.config/gcloud", "~/.azure"):
        assert name in label


def test_the_environment_overrides_are_denied_with_the_defaults(home: Path, tmp_path: Path,
                                                                 monkeypatch) -> None:
    monkeypatch.setattr(C.platform, "system", lambda: "Darwin")
    gc, az, al = tmp_path / "gc", tmp_path / "az", tmp_path / "awslogin"
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(gc))
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(az))
    monkeypatch.setenv("AWS_LOGIN_CACHE_DIRECTORY", str(al))
    policy = build_policy(_entity(home), deny_standard_creds=True)
    for d in (gc, az, al, home / ".config" / "gcloud", home / ".azure"):
        assert d in policy.deny_read_write, d


def test_a_relative_override_is_ignored_not_resolved_against_the_cwd(home: Path, monkeypatch) -> None:
    monkeypatch.setattr(C.platform, "system", lambda: "Darwin")
    monkeypatch.setenv("AZURE_CONFIG_DIR", "relative/az")
    assert C._cred_dir_sources() == [home / ".config" / "gcloud", home / ".azure"]


def test_linux_creates_and_masks_an_absent_gcloud_and_azure(home: Path, linux) -> None:
    policy = build_policy(_entity(home), deny_standard_creds=True)
    assert home / ".config" / "gcloud" in policy.deny_read_write and home / ".azure" in policy.deny_read_write
    argv, _ = C._bwrap_plan(policy)
    for d in (home / ".config" / "gcloud", home / ".azure"):
        assert [str(d)] in _ops(argv, "--tmpfs") and [str(d)] in _ops(argv, "--remount-ro"), d
    mounted, _ = C._mount_plan_paths(argv, policy)
    assert mounted[str(home / ".azure")] == "dir", "the provider creates it first, and the ledger owns it"


def test_linux_skips_a_cred_dir_override_this_user_cannot_create(home: Path, tmp_path: Path,
                                                                 linux, monkeypatch) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    try:
        monkeypatch.setenv("AZURE_CONFIG_DIR", str(locked / "az"))
        argv, _ = C._bwrap_plan(build_policy(_entity(home), deny_standard_creds=True))
        assert not any(str(locked / "az") in a for a in argv), "the shell cannot create it either"
    finally:
        locked.chmod(0o755)


def test_linux_masks_every_aws_cache_after_the_aws_view(home: Path, linux) -> None:
    (home / ".aws" / "sso").mkdir(parents=True)
    argv, _ = C._bwrap_plan(build_policy(_entity(home), deny_standard_creds=True))
    aws = str(home / ".aws")
    i_view = next(i for i in range(len(argv) - 1) if argv[i:i + 2] == ["--tmpfs", aws])
    for rel in _AWS_CACHES:
        cache = str(home / rel)
        assert [cache] in _ops(argv, "--tmpfs") and [cache] in _ops(argv, "--remount-ro"), rel
        assert i_view < argv.index(cache), "the ~/.aws view must not land on top of a cache"


def test_linux_masks_the_aws_caches_when_aws_is_absent_too(home: Path, linux) -> None:
    argv, create_first = C._bwrap_plan(build_policy(_entity(home), deny_standard_creds=True))
    assert create_first.count(str(home / ".aws")) == 1
    for rel in _AWS_CACHES:
        assert [str(home / rel)] in _ops(argv, "--tmpfs"), rel


def test_the_credential_overrides_are_followed(home: Path, tmp_path: Path, monkeypatch) -> None:
    o = tmp_path / "o"
    gitcfg = o / "gitconfig"
    gitcfg.parent.mkdir()
    gitcfg.write_text("[credential]\n\thelper = store --file=%s\n" % (o / "git-store"))
    env = {
        "AWS_SHARED_CREDENTIALS_FILE": str(o / "aws-creds"),
        "KUBECONFIG": f"{o / 'k1'}{os.pathsep}{o / 'k2'}{os.pathsep}relative/k3",
        "DOCKER_CONFIG": str(o / "docker"),
        "GH_CONFIG_DIR": str(o / "gh"),
        "NETRC": str(o / "netrc"),
        "NPM_CONFIG_USERCONFIG": str(o / "npmrc"),
        "XDG_CONFIG_HOME": str(o / "xdg"),
        "GIT_CONFIG_GLOBAL": str(gitcfg),
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    policy = build_policy(_entity(home), deny_standard_creds=True)
    for f in ("aws-creds", "k1", "k2", "docker/config.json", "netrc", "npmrc", "xdg/git/credentials",
              "git-store"):
        assert o / f in policy.deny_files, f
    for d in ("gh", "xdg/gh"):
        assert o / d in policy.deny_read_write, d
    assert home / ".kube" / "config" in policy.deny_files, "the default stays denied as well"
    assert not any("relative" in str(p) for p in policy.deny_files)


def test_the_api_key_file_is_denied_to_the_entity(home: Path, monkeypatch) -> None:
    from levain import launch

    key = home / "keys" / "model.key"
    key.parent.mkdir()
    key.write_text("sk-x")
    monkeypatch.setattr(launch, "_secret_files", [])
    launch.add_secret_file(key)
    policy = build_policy(_entity(home))
    assert key in policy.deny_files
    assert crown_jewel_reason(policy, key) is not None


def test_a_symlink_loop_in_a_cred_path_does_not_crash_the_build(home: Path, linux) -> None:
    (home / ".netrc").symlink_to(home / ".netrc")
    policy = build_policy(_entity(home), deny_standard_creds=True)
    assert home / ".netrc" in policy.deny_files
    with pytest.raises(C.ConfinementError):
        C._bwrap_plan(policy)


def test_a_link_root_whose_target_the_floor_does_not_name_refuses(home: Path, tmp_path: Path) -> None:
    import dataclasses

    target = tmp_path / "t"
    target.mkdir()
    ro = tmp_path / "ro"
    ro.mkdir()
    (ro / "link").symlink_to(target)
    ro.chmod(0o555)
    try:
        base = build_policy(_entity(home))
        policy = dataclasses.replace(base, deny_read_write=(*base.deny_read_write, ro / "link"))
        with pytest.raises(C.ConfinementError, match="does not name"):
            C._bwrap_plan(policy)
    finally:
        ro.chmod(0o755)
