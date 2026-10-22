"""S1 session 5 unit 5a: the bundle-header observer refuses inside stage_copy's copy loop, so the staged file goes.

Reproduced at 5a session 3's first DONE run: an observer whose refusal came from ``finish()`` after ``stage_bundle``
returned left the fresh ``in/repo.bundle`` behind (``dest left=True``)."""
from __future__ import annotations

import os

import pytest

from levain.slot import Refused, bundlefmt as BF
from levain.slot import stage as st

UID, GID = os.getuid() or 501, os.getgid() or 20
OID = "a" * 40


def _stage(tmp_path, data: bytes):
    (tmp_path / "staging").mkdir()
    (tmp_path / "dest").mkdir()
    (tmp_path / "staging" / st.BUNDLE_NAME).write_bytes(data)
    obs = BF.HeaderObserver(check=BF.base_of)
    st.stage_bundle(str(tmp_path / "staging"), str(tmp_path / "dest"), staging_uid=UID, staging_gid=GID,
                    dest_gid=GID, run_as=st.thread_run_as, chown=lambda fd, u, g: None, observe=obs.feed)
    return obs.finish()


@pytest.mark.parametrize("data, name", [
    (b"# v2 git bundle\n" + OID.encode() + b" HEAD\n", "bundle-header-mismatch"),          # ends before the blank line
    (b"# v2 git bundle\n" + OID.encode() + b" refs/heads/main\n\nPACK", "bundle-no-head"),
])
def test_a_refused_header_leaves_no_staged_bundle(tmp_path, data, name):
    with pytest.raises(Refused) as e:
        _stage(tmp_path, data)
    assert e.value.name == name
    assert not (tmp_path / "dest" / st.BUNDLE_NAME).exists()


@pytest.mark.parametrize("name, ok", [(b"refs/heads/@", True), (b"refs/heads/a/@", True), (b"refs/heads/a@{b", False)])
def test_branch_ref_ok_follows_check_ref_format(name, ok):
    # Reproduced on eb6931ea: refs/heads/@ was refused, though `git check-ref-format refs/heads/@` exits 0.
    assert BF.branch_ref_ok(name) is ok
