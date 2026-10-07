"""The cloud CLI credential caches in the standard cred floor (lane P2's research note, item 4).

``~/.aws/{sso,cli,login,boto}/cache``, ``~/.config/gcloud`` and ``~/.azure`` each hold a live token
the CLI writes itself, and were readable by a confined entity in every drive. These tests read the
policy, the rendered Seatbelt text and the bwrap argv as data; nothing here runs a sandbox. Linux
behaviour is selected by patching ``platform.system``, which is what the policy code consults.
"""
from __future__ import annotations

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
    for var in ("AWS_LOGIN_CACHE_DIRECTORY", "CLOUDSDK_CONFIG", "AZURE_CONFIG_DIR", "XDG_RUNTIME_DIR"):
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


def test_linux_denies_gcloud_and_azure_only_while_present(home: Path, linux) -> None:
    (home / ".azure").mkdir()
    policy = build_policy(_entity(home), deny_standard_creds=True)
    assert home / ".azure" in policy.deny_read_write
    assert home / ".config" / "gcloud" not in policy.deny_read_write
    argv, create_first = C._bwrap_plan(policy)
    assert [str(home / ".azure")] in _ops(argv, "--tmpfs")
    assert all(".config/gcloud" not in d for d in create_first)
    assert not any(".config/gcloud" in a for a in argv), "no mount, so no host dir is created"


def test_linux_denies_a_gcloud_dir_that_appears_after_the_policy_was_built(home: Path, linux) -> None:
    policy = build_policy(_entity(home), deny_standard_creds=True)
    assert home / ".config" / "gcloud" not in policy.deny_read_write
    (home / ".config" / "gcloud").mkdir(parents=True)      # a first `gcloud auth login`, mid-session
    fresh = refresh_socket_denies(policy)
    assert home / ".config" / "gcloud" in fresh.deny_read_write
    assert home / ".config" in fresh.deny_write_dirs       # pinned against rename as well
    assert crown_jewel_reason(fresh, home / ".config" / "gcloud" / "credentials.db") is not None


def test_linux_refuses_a_symlinked_azure_dir(home: Path, linux) -> None:
    (home / "dotfiles" / "azure").mkdir(parents=True)
    (home / ".azure").symlink_to(home / "dotfiles" / "azure")
    with pytest.raises(C.ConfinementError, match="symlink"):
        C._bwrap_plan(build_policy(_entity(home), deny_standard_creds=True))


def test_linux_masks_an_aws_cache_whose_parent_exists_after_the_read_only_aws_bind(home: Path,
                                                                                    linux) -> None:
    (home / ".aws" / "sso").mkdir(parents=True)
    argv, _ = C._bwrap_plan(build_policy(_entity(home), deny_standard_creds=True))
    aws, cache = str(home / ".aws"), str(home / ".aws" / "sso" / "cache")
    assert [cache] in _ops(argv, "--tmpfs") and [cache] in _ops(argv, "--remount-ro")
    i_ro = next(i for i in range(len(argv) - 2) if argv[i:i + 3] == ["--ro-bind", aws, aws])
    assert i_ro < argv.index(cache), "the read-only bind of ~/.aws must not land on top of the cache tmpfs"
    # an absent ~/.aws/cli cannot be created from inside the read-only ~/.aws, so nothing is mounted
    assert not any(a.startswith(str(home / ".aws" / "cli")) for a in argv)


def test_linux_creates_no_aws_cache_dirs_when_aws_is_absent(home: Path, linux) -> None:
    argv, create_first = C._bwrap_plan(build_policy(_entity(home), deny_standard_creds=True))
    assert create_first.count(str(home / ".aws")) == 1
    assert not any("/cache" in d for d in create_first)
    assert not any(a.endswith("/cache") for a in argv)
