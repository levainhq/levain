"""levain#226: on Linux the export walls' bwrap enters ``/`` and the stage enters the mount root after its drop
(the account owns the 0700 mount root; the capability-dropped root bwrap cannot enter it). Plan-level only: views and
specs are text, every OS act the s2d fake kernel."""
from __future__ import annotations

import dataclasses

import pytest

from levain.slot import Refused
from levain.slot import wall
from tests.execve_guard import refuse_real_execve
from tests.test_slot_s2d_wall import FakeOS
from tests.test_slot_s5c1_export_wall import IDS, LINKS, ROLES, SHARED


@pytest.fixture(autouse=True)
def _no_real_execve(monkeypatch):
    yield from refuse_real_execve(monkeypatch, wall.os, route=True)


#: Pinned from the tree before #226: the darwin spec JSON, byte for byte.
DARWIN_JSON = {
    "importer": (b'{"argv":["/usr/local/libexec/levain/export-importer"],"cwd":"/private/var/levain/export/.work",'
                 b'"env_file":null,"exec_fd":null,"extra_env":{},"format":"levain-wall-spec/1","gid":3798,'
                 b'"groups":[3798,3784],"keep_fds":[4],"nproc":64,"profile":"/private/var/levain/export/importer.sb",'
                 b'"resolve":false,"target":"importer","uid":3798}'),
    "verifier": (b'{"argv":["/usr/local/libexec/levain/export-verifier"],"cwd":"/private/var/levain/export/.verify",'
                 b'"env_file":null,"exec_fd":null,"extra_env":{},"format":"levain-wall-spec/1","gid":3797,'
                 b'"groups":[3797,3781],"keep_fds":[4],"nproc":64,"profile":"/private/var/levain/export/verifier.sb",'
                 b'"resolve":false,"target":"verifier","uid":3797}'),
}


def _spec(host, role):
    return wall.export_launch_spec(host, role, ids=IDS[host], shared=SHARED[host], export_class=ROLES[role][2])


@pytest.mark.parametrize("role", sorted(ROLES))
def test_226_capdropped_export_view_never_chdirs_into_a_writable_bind(role):
    facts = wall.export_wall_facts("linux", role)
    binds = [p for group in facts.writable for p in group]
    assert facts.chdir == "/"
    assert all(facts.chdir != b and not facts.chdir.startswith(b + "/") for b in binds)
    for bad in (binds[0], binds[0] + "/sub"):
        with pytest.raises(Refused) as ei:
            wall.wall_view_args(dataclasses.replace(facts, chdir=bad), LINKS)
        assert ei.value.name == "view-path-off-list"
    assert wall.wall_view_args(facts, LINKS)[-2:] == ("--chdir", "/")


@pytest.mark.parametrize("role", sorted(ROLES))
def test_226_export_spec_cwd_is_the_mount_root_both_hosts(role):
    """Pinned-behaviour test: passes on the code before #226 by design. It fails if the spec's cwd drops to the view's
    ``/``, which would run the importer outside its directory."""
    d = ROLES[role][1]
    assert _spec("darwin", role).cwd == f"/private/var/levain/export/{d}"
    assert _spec("linux", role).cwd == f"/var/levain/export/{d}"
    assert _spec("darwin", role).to_json() == DARWIN_JSON[role]


@pytest.mark.parametrize("role", sorted(ROLES))
def test_226_linux_export_stage_enters_mount_root_after_the_drop(role, monkeypatch):
    """Pinned-behaviour test: passes on the code before #226 by design. It fails if the stage's chdir ever moves
    ahead of the drop, or if a failed chdir still execs."""
    sp = _spec("linux", role)
    mount = f"/var/levain/export/{ROLES[role][1]}"
    monkeypatch.setattr(wall.sys, "platform", "linux")
    fake = FakeOS(linux=True, files={}, fds=(0, 1, 2, 3, 4), spec_bytes=sp.to_json())
    wall.stage_main(os_calls=fake.calls)
    names = fake.names()
    assert ("chdir", mount) in fake.log
    assert names.index("setuid") < names.index("chdir") < names.index("execve")
    bad = FakeOS({"chdir": "raise"}, linux=True, files={}, fds=(0, 1, 2, 3, 4), spec_bytes=sp.to_json())
    assert wall.stage_main(os_calls=bad.calls) == wall.STAGE_EXIT
    assert "execve" not in bad.names()


@pytest.mark.parametrize("role", sorted(ROLES))
def test_226_linux_export_launch_pairs_the_view_chdir_with_the_mount_root_spec_cwd(role):
    """The launch argv carries bwrap's ``--chdir /`` and the spec the stage reads carries the mount root: the two
    halves of the fix in one launch (a view that entered the mount root again would fail EPERM for the capless root)."""
    sp = _spec("linux", role)
    launch = wall.export_linux_launch_argv(sp, host_links=LINKS)
    argv = list(launch.argv)
    assert argv[argv.index("--chdir") + 1] == "/"
    assert sp.cwd == f"/var/levain/export/{ROLES[role][1]}"
