"""PREL3 1b-7: a row's root ``chown``/``chmod`` never follow a symlink planted at the row's path. A service account owns
the parent of some rows (``L/sessions`` is ``_levain_control``'s), so it can swap a row for a link; root following it
would chown/chmod a path that account chose. The steps run here as this test's own uid on a pytest tmp tree, with the
real tools: a chown to the uid's own user and group needs no root."""
from __future__ import annotations

import grp
import os
import pwd
import stat
import subprocess
from pathlib import Path

import pytest

from levain.slot import layout
from levain.slot.layout import Row

HOSTS = ("linux", "darwin")
ME = pwd.getpwuid(os.getuid()).pw_name
MY_GROUP = grp.getgrgid(os.getgid()).gr_name


def _run(steps, *, skip_refusal: bool = False) -> list[str]:
    """Run each step as the executor would: skip_if first, then the act; stop at the first failure. Returns why the
    run stopped ('' when every step ran)."""
    for s in steps:
        if skip_refusal and s.why.startswith("refuse a link at"):
            continue
        if s.skip_if and subprocess.run(s.skip_if, capture_output=True).returncode == 0:
            continue
        if s.argv:
            r = subprocess.run(s.argv, capture_output=True, text=True)
            if r.returncode != 0:
                return [s.why]
        elif s.write is not None:
            path, content, mode = s.write
            Path(path).write_text(content)
            os.chmod(path, mode)
        elif s.call is not None:
            ok, why = s.call()
            if not ok:
                return [s.why, why]
    return []


def _planted(tmp_path, kind):
    target = tmp_path / "target"
    if kind == "dir":
        target.mkdir()
    else:
        target.write_text("")
    os.chmod(target, 0o700)
    link = tmp_path / "row"
    link.symlink_to(target)
    return target, link


@pytest.mark.parametrize("kind", ["dir", "file"])
@pytest.mark.parametrize("host", HOSTS)
def test_a_planted_link_is_refused_and_its_target_untouched(tmp_path, host, kind):
    target, link = _planted(tmp_path, kind)
    stopped = _run(layout.row_steps(host, [Row(link, ME, MY_GROUP, 0o755, kind)]))
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o700, "root's chmod followed the link to its target"
    assert stopped and stopped[0].startswith("refuse a link at"), stopped
    assert link.is_symlink()


@pytest.mark.parametrize("kind", ["dir", "file"])
@pytest.mark.parametrize("host", HOSTS)
def test_a_link_swapped_in_after_the_refusal_still_is_not_followed(tmp_path, host, kind):
    """The parent's owner can swap the row between the refusal and the acts: each act is no-follow on its own."""
    target, link = _planted(tmp_path, kind)
    _run(layout.row_steps(host, [Row(link, ME, MY_GROUP, 0o755, kind)]), skip_refusal=True)
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o700, "an act followed the link to its target"


@pytest.mark.parametrize("kind", ["dir", "file"])
@pytest.mark.parametrize("host", HOSTS)
def test_a_plain_row_is_made_owned_and_moded(tmp_path, host, kind):
    p = tmp_path / "row"
    assert _run(layout.row_steps(host, [Row(p, ME, MY_GROUP, 0o2750 if kind == "dir" else 0o640, kind)])) == []
    st = os.lstat(p)
    assert (stat.S_ISDIR(st.st_mode) if kind == "dir" else stat.S_ISREG(st.st_mode))
    assert stat.S_IMODE(st.st_mode) == (0o2750 if kind == "dir" else 0o640)
    # a re-run over the made row is a no-op that succeeds
    assert _run(layout.row_steps(host, [Row(p, ME, MY_GROUP, 0o2750 if kind == "dir" else 0o640, kind)])) == []


@pytest.mark.parametrize("host", HOSTS)
def test_the_wrong_kind_at_a_row_path_is_refused(tmp_path, host):
    p = tmp_path / "row"
    p.write_text("")
    os.chmod(p, 0o600)
    stopped = _run(layout.row_steps(host, [Row(p, ME, MY_GROUP, 0o755, "dir")]))
    assert stopped and stat.S_IMODE(os.stat(p).st_mode) == 0o600
