"""R/dispatch/ (S10b slice R): the dispatch run server's socket dir. Owner ``_levain_dispatch``, group
``levain-control`` (the operator is in it, no slot uid is), 0750 (setgid on Linux); every place that makes the R dirs
makes it the same way."""
from __future__ import annotations

import os
import stat

import pytest

from levain.slot import layout, reaper, service_boot, units
from levain.slot.ids import SlotId
from tests.test_slot_s2f2_runtime_helper import World

HOSTS = ["linux", "darwin"]


def _row(host):
    return next(r for r in layout.base_run_rows(host, "phill") if r.path == layout.RUN[host] / "dispatch")


@pytest.mark.parametrize("host", HOSTS)
def test_the_row_is_the_dispatch_account_in_group_control(host):
    row = _row(host)
    assert (row.owner, row.group, row.kind) == ("_levain_dispatch", "levain-control", "dir")
    assert row.mode == (0o2750 if host == "linux" else 0o750)
    paths = [r.path for r in layout.base_run_rows(host, "phill")]
    assert paths.index(layout.RUN[host]) < paths.index(row.path)          # parents first
    assert [r for r in layout.base_run_rows(host, None) if r.path == row.path] == [row]   # not operator-dependent


@pytest.mark.parametrize("host", HOSTS)
def test_the_service_checks_that_row(host):
    assert service_boot.RUN_DIR_OF["dispatch_run"] == "dispatch"
    assert service_boot.run_dir_row(host, "dispatch_run") == _row(host)


@pytest.mark.parametrize("host", HOSTS)
def test_install_steps_chown_then_mode(host):
    row = _row(host)
    argvs = [s.argv for s in layout.row_steps(host, [row]) if s.argv]
    p = str(row.path)
    assert any(a[0].endswith("mkdir") and a[-1] == p for a in argvs)
    chown = next(i for i, a in enumerate(argvs) if a[0].endswith("chown") and a[-2:] == ("_levain_dispatch:levain-control", p))
    assert argvs[-1][0].endswith("chmod") and argvs[-1][-2:] == (f"{row.mode:04o}", p) and chown < len(argvs) - 1


def test_tmpfiles_recreates_it_at_boot():
    text = layout.tmpfiles_text("linux", [SlotId("egress", 0)], "phill").splitlines()
    line = f"d {layout.RUN['linux']}/dispatch 2750 _levain_dispatch levain-control -"
    assert line in text
    assert text.index(f"d {layout.RUN['linux']} 0755 root root -") < text.index(line)


@pytest.mark.parametrize("host", HOSTS)
def test_the_unit_is_installed_and_started_by_name(host):
    name = "levain-dispatch_run.service" if host == "linux" else "com.levain.dispatch_run"
    assert name in units.start_order(host)
    order = units.start_order(host)
    other = "levain-dispatch.service" if host == "linux" else "com.levain.dispatch"
    assert order.index(other) < order.index(name)                         # start order: after dispatch
    steps = units.install_steps(host, units.services_for(host), run=lambda *a, **k: None, read=lambda p: None)
    assert any(name in repr(s) for s in steps)


@pytest.mark.parametrize("host", HOSTS)
def test_the_reaper_boot_pass_makes_it(tmp_path, host):
    w = World(tmp_path, host=host)
    w.seams.account_uid = lambda name: 800
    w.seams.group_gid = lambda g: 700
    rows = {r.path: r for r in reaper.run_rows(w.seams)}
    row = rows[w.R / "dispatch"]
    assert (row.owner, row.group) == ("_levain_dispatch", "levain-control")
    assert reaper.boot_dirs(w.seams, log=lambda m: None) == []
    assert (w.R / "dispatch").is_dir() and not (w.R / "dispatch").is_symlink()
    assert stat.S_IMODE(os.lstat(w.R / "dispatch").st_mode) & 0o777 == row.mode & 0o777
