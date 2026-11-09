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
import sys
from pathlib import Path

import pytest

from levain.firing import hands
from levain.slot import layout
from levain.slot.install_helper import _act
from levain.slot.layout import Row

#: The darwin param runs the real BSD tools (``/usr/sbin/chown``, ``chmod -h -N``), so it runs only on macOS; the CI test
#: job is ubuntu. The linux param runs wherever its tools exist.
HOSTS = (
    "linux",
    pytest.param("darwin", marks=pytest.mark.skipif(sys.platform != "darwin", reason="runs the macOS tools")),
)
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


@pytest.mark.parametrize("kind", ["dir", "file"])
@pytest.mark.parametrize("host", HOSTS)
def test_plan_only_reports_a_converged_row_as_nothing_to_do(tmp_path, host, kind):
    """PREL3 fix2 r1 L1-F1/L2-M1: ``--plan-only`` reads each step it would run; the row steps' shapes (``chown -h``,
    ``chmod -h``, ``chmod -h -N``, the Linux mode call, the link refusal) must read ``----`` on a made row."""
    p = tmp_path / "row"
    mode = 0o2750 if kind == "dir" else 0o640
    steps = layout.row_steps(host, [Row(p, ME, MY_GROUP, mode, kind)])
    assert any(_act(s) for s in steps), "an unmade row has something to do"
    assert _run(steps) == []
    acting = [s.why for s in steps if _act(s)]
    assert acting == [], f"a converged row still reports ACT: {acting}"
    os.chmod(p, 0o700)
    assert [s.why for s in steps if _act(s)] == [f"set {p} to {mode:04o}"], "a wrong mode is the one thing to do"


@pytest.mark.parametrize("host", HOSTS)
def test_plan_only_reports_a_planted_link_as_acting(tmp_path, host):
    _, link = _planted(tmp_path, "dir")
    steps = layout.row_steps(host, [Row(link, ME, MY_GROUP, 0o755, "dir")])
    assert [s.why for s in steps if _act(s)][:1] == [f"refuse a link at {link}"]


@pytest.mark.parametrize("host", HOSTS)
def test_the_refusal_names_the_path_and_says_it_is_a_symlink(tmp_path, host):
    _, link = _planted(tmp_path, "dir")
    out: list[str] = []
    plan = hands.Plan(host, "op", "h", 1, tmp_path, tmp_path, tuple(layout.row_steps(host, [Row(link, ME, MY_GROUP, 0o755, "dir")])))
    assert hands.run_plan(plan, dry_run=False, emit=out.append) == 1
    failed = [ln for ln in out if "FAILED:" in ln]
    assert failed and str(link) in failed[0] and "is a symlink" in failed[0], out
