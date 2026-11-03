"""R/fence/ (P7-2 wiring, shape C): the operator-uid fence server's socket dir. Owner the operator, group
``_levain_qbroker``, 0750 (setgid on Linux); every place that makes the R dirs makes it the same way."""
from __future__ import annotations

import os
import stat

import pytest

from levain.qbroker import fence_server
from levain.slot import layout, reaper
from levain.slot.ids import SlotId
from tests.test_slot_s2f2_runtime_helper import OP_UID, World

HOSTS = ["linux", "darwin"]


def _fence(host, op="phill"):
    return next(r for r in layout.base_run_rows(host, op) if r.path == layout.RUN[host] / "fence")


@pytest.mark.parametrize("host", HOSTS)
def test_fence_row_is_the_operators_group_qbroker(host):
    row = _fence(host)
    assert (row.owner, row.group, row.kind) == ("phill", "_levain_qbroker", "dir")
    assert row.mode == (0o2750 if host == "linux" else 0o750)
    assert row.mode in fence_server._DIR_MODES          # the shape the server insists on at start
    paths = [r.path for r in layout.base_run_rows(host, "phill")]
    assert paths.index(layout.RUN[host]) < paths.index(row.path)          # parents first


@pytest.mark.parametrize("host", HOSTS)
def test_the_owner_follows_the_operator(host):
    assert _fence(host, "alice").owner == "alice"


@pytest.mark.parametrize("host", HOSTS)
def test_install_steps_chown_then_mode(host):
    row = _fence(host)
    steps = layout.row_steps(host, [row])
    argvs = [s.argv for s in steps if s.argv]
    p = str(row.path)
    assert any(a[0].endswith("mkdir") and a[-1] == p for a in argvs)
    chown = next(i for i, a in enumerate(argvs) if a[0].endswith("chown") and a[-2:] == ("phill:_levain_qbroker", p))
    assert argvs[-1][0].endswith("chmod") and argvs[-1][-2:] == (f"{row.mode:04o}", p) and chown < len(argvs) - 1


def test_tmpfiles_recreates_it_at_boot():
    text = layout.tmpfiles_text("linux", [SlotId("egress", 0)], "phill").splitlines()
    assert f"d {layout.RUN['linux']}/fence 2750 phill _levain_qbroker -" in text
    assert text.index(f"d {layout.RUN['linux']} 0755 root root -") < text.index(f"d {layout.RUN['linux']}/fence 2750 phill _levain_qbroker -")
    steps = layout.tmpfiles_steps("linux", [], "phill")
    assert "phill _levain_qbroker" in steps[0].write[1]


@pytest.mark.parametrize("host", HOSTS)
def test_the_reaper_boot_pass_makes_it(tmp_path, host):
    """The operator name comes from L/policy/operator; the boot pass chowns to its uid and the qbroker group."""
    w = World(tmp_path, host=host)
    accounts = {"phill": OP_UID}
    w.seams.account_uid = lambda name: accounts.get(name, 800)
    w.seams.group_gid = lambda g: 4242 if g == "_levain_qbroker" else 700
    rows = {r.path: r for r in reaper.run_rows(w.seams)}
    row = rows[w.R / "fence"]
    assert (row.owner, row.group) == ("phill", "_levain_qbroker")
    assert reaper.boot_dirs(w.seams, log=lambda m: None) == []
    assert (w.R / "fence").is_dir() and not (w.R / "fence").is_symlink()
    # permission bits only: the test's chown is a recorder, so the dir keeps a group the test user may not be in, and
    # the kernel then drops setgid from the fchmod (the row's own mode is asserted above)
    assert stat.S_IMODE(os.lstat(w.R / "fence").st_mode) & 0o777 == row.mode & 0o777
    assert ("", OP_UID, 4242) not in w.chowns and any(c[1:] == (OP_UID, 4242) for c in w.chowns)
