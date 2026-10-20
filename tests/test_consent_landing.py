"""The landing tier (F11 §4 step 4): what the consent classifiers would file a Write of each landed path at. Real
paths in a real git repository in tmp, a scratch home and CODEX_HOME, the queue directory a real directory."""
from __future__ import annotations

import os
import subprocess

import pytest

from levain.consent.landing import landing_tier
from levain.consent.adapters.claude_code import levain_package_dir


@pytest.fixture
def world(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    codex_dir = tmp_path / "codex-home"
    codex_dir.mkdir()
    qdir = tmp_path / "consent"
    qdir.mkdir(mode=0o700)
    return {"repo": str(repo), "home": home, "codex_dir": codex_dir, "queue_dir": qdir}


def tier(world, *rel, absolute=()):
    paths = [os.path.join(world["repo"], p) for p in rel] + list(absolute)
    return landing_tier(world["repo"], paths, queue_dir=world["queue_dir"], home=world["home"],
                        codex_dir=world["codex_dir"])


def test_plain_source_lands_at_t2(world):
    assert tier(world, "src/app.py", "README.md", "docs/guide/x.md") == "T2"


@pytest.mark.parametrize("rel", [
    ".claude/settings.json", ".claude/settings.local.json", ".claude/hooks/pre.py", ".claude/agents/a.md",
    ".claude/skills/s/SKILL.md", ".mcp.json", ".codex/config.toml", ".codex/hooks.json", ".codex/anything",
    ".git/config", ".git/hooks/pre-commit", ".claude-plugin/plugin.json",
])
def test_agent_config_and_hooks_in_the_repo_land_at_t3(world, rel):
    assert tier(world, "src/app.py", rel) == "T3"


def test_codex_home_the_queue_directory_and_the_levain_package_land_at_t3(world):
    assert tier(world, absolute=[str(world["codex_dir"] / "config.toml")]) == "T3"
    assert tier(world, absolute=[str(world["queue_dir"] / "dispatch.db")]) == "T3"
    assert tier(world, absolute=[os.path.join(levain_package_dir(), "dispatch", "adopt.py")]) == "T3"


def test_a_symlink_into_agent_config_lands_at_t3(world):
    os.symlink(os.path.join(world["repo"], ".claude"), os.path.join(world["repo"], "conf"))
    os.mkdir(os.path.join(world["repo"], ".claude"))
    assert tier(world, "conf/settings.json") == "T3"


def test_a_relative_path_or_no_path_raises(world):
    with pytest.raises(ValueError, match="absolute"):
        landing_tier(world["repo"], ["src/app.py"], queue_dir=world["queue_dir"], home=world["home"],
                     codex_dir=world["codex_dir"])
    with pytest.raises(ValueError, match="at least one path"):
        landing_tier(world["repo"], [], queue_dir=world["queue_dir"], home=world["home"],
                     codex_dir=world["codex_dir"])


def test_the_floor_seam_reads_the_real_tier(world):
    # adopt._tier hands the function the repository and the change set's paths joined to it and symlink-resolved
    from functools import partial

    from levain.dispatch.adopt import _tier

    real = partial(landing_tier, queue_dir=world["queue_dir"], home=world["home"], codex_dir=world["codex_dir"])
    assert _tier(real, world["repo"], ("src/app.py",)) == "T2"
    assert _tier(real, world["repo"], ("src/app.py", ".claude/settings.json")) == "T3"
