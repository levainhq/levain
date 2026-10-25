"""Module-scoped rigs for the ported probes. Round 3 used `origin.py` (every byte of every connection) and the round-3
DNS responder; round 4 uses `git_origin.py`. The glue in both is the SPIKE glue of round 4 (ruling P-4): strict mode,
closed env, the levain-xlat denied table."""

from __future__ import annotations

import pytest

from ..conftest import build_rig


@pytest.fixture(scope="module")
def r3(tmp_path_factory):
    rig = build_rig(tmp_path_factory, "r3", origin="bytes")
    yield rig
    rig.stop()


@pytest.fixture(scope="module")
def r3_glue(r3):
    """round 3's run B2: strict glue, CLOSED env, upstream trust = the origin CA only."""
    return r3.start_glue("B2")


@pytest.fixture(scope="module")
def r4(tmp_path_factory):
    rig = build_rig(tmp_path_factory, "r4", origin="git")
    yield rig
    rig.stop()


@pytest.fixture(scope="session")
def git_streams(tmp_path_factory):
    """mkrepos.py's fast-import streams (small / mid / big / more / client), written once per session."""
    from ..fixtures.repos import write_streams
    d = tmp_path_factory.mktemp("git-streams")
    write_streams(d)
    return d


@pytest.fixture(scope="module")
def r4_repos(r4, git_streams):
    """declared.git and undeclared.git (5 commits each) under the git origin's root."""
    from ..fixtures.repos import build_bare
    home = r4.root / "home"
    home.mkdir(exist_ok=True)
    for name in ("declared", "undeclared"):
        build_bare(r4.repo_root, name, git_streams / "small.fi", home)
    return home


@pytest.fixture(scope="module")
def r4_glue(r4, r4_repos):
    """round 4's run E: cap 2,097,152 (a stand-in above the measured max, not a ruling), safe logs, scheme check on."""
    return r4.start_glue("E", cap=2097152, upstream_ca="origin")
