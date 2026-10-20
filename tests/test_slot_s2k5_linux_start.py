"""S1 session 2, lane K5: the two Linux ``start`` defects the s2-linux VM run reproduced.

F3: nothing made ``/sys/fs/cgroup/levain.slice`` or ``{e,v}<n>.scope``, so every real ``start`` died
FileNotFoundError; :func:`runtime_helper.make_cgroup` now makes the missing ancestors, refusing an untrusted one.
F4: bwrap made the view's bind parents ``root:root 0700``, so the slot could not reach its own ``<S>`` (clone
exit 121); :func:`wall.linux_view_args` now makes each ancestor itself at its layout mode (CONTRACT §2.1, §2.2)."""

from __future__ import annotations

import os

import pytest

from levain.slot import Refused, layout, wall
from levain.slot.ids import SlotId
from levain.slot.runtime_helper import make_cgroup

SESSION = "s-0123456789abcdef"
E0, E1, V0 = SlotId("egress", 0), SlotId("egress", 1), SlotId("none", 0)
LINKS = {"/bin": "usr/bin", "/sbin": "usr/sbin", "/lib": "usr/lib", "/lib64": None}


# --- F3: the cgroup ancestors ------------------------------------------------------------------------------------

def _mine(st: os.stat_result) -> bool:
    """The test's stand-in for root-owned: owned by this uid, not group/other-writable (the real check's shape)."""
    return st.st_uid == os.getuid() and not st.st_mode & 0o022


@pytest.fixture
def cg(tmp_path):
    """A tmp dir tree this uid owns standing in for /sys/fs/cgroup, under a 022 umask (the helper's)."""
    root = tmp_path / "cgroup"
    root.mkdir(0o755)
    old = os.umask(0o022)
    try:
        yield str(root)
    finally:
        os.umask(old)


def _leaf(root: str, leaf: str = "") -> str:
    base = f"{root}/levain.slice/e0.scope/{SESSION}"
    return f"{base}/{leaf}" if leaf else base


def test_make_cgroup_creates_the_missing_ancestors(cg):
    for leaf in ("", "step1"):
        path = _leaf(cg, leaf)
        make_cgroup(path, root=cg, trusted=_mine)
        for d in (f"{cg}/levain.slice", f"{cg}/levain.slice/e0.scope", _leaf(cg), path):
            st = os.lstat(d)
            assert os.path.isdir(d) and st.st_mode & 0o777 == 0o755, d


def test_make_cgroup_uses_a_trusted_existing_leaf(cg):
    make_cgroup(_leaf(cg), root=cg, trusted=_mine)
    make_cgroup(_leaf(cg), root=cg, trusted=_mine)      # a start that died after making it: used, no error


@pytest.mark.parametrize("bad", ["levain.slice", "levain.slice/e0.scope"])
def test_make_cgroup_refuses_a_writable_ancestor_before_acting_below_it(cg, bad):
    os.makedirs(f"{cg}/{bad}")
    os.chmod(f"{cg}/{bad}", 0o777)
    with pytest.raises(Refused) as e:
        make_cgroup(_leaf(cg), root=cg, trusted=_mine)
    assert e.value.name == "cgroup-ancestor-untrusted" and bad in e.value.detail
    assert os.listdir(f"{cg}/{bad}") == []                 # nothing made inside the untrusted dir


def test_make_cgroup_refuses_an_untrusted_root(cg):
    os.chmod(cg, 0o775)
    with pytest.raises(Refused) as e:
        make_cgroup(_leaf(cg), root=cg, trusted=_mine)
    assert e.value.name == "cgroup-ancestor-untrusted"
    assert os.listdir(cg) == []


def test_make_cgroup_refuses_a_symlinked_ancestor(cg, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(0o755)
    os.symlink(elsewhere, f"{cg}/levain.slice")
    with pytest.raises(Refused) as e:
        make_cgroup(_leaf(cg), root=cg, trusted=_mine)
    assert e.value.name == "cgroup-ancestor-untrusted"
    assert os.listdir(elsewhere) == []


def test_make_cgroup_default_owner_check_is_root(cg):
    """The real default (``_root_trusted``) refuses a tree this non-root uid owns: the owner check is not the seam."""
    if os.geteuid() == 0:
        pytest.skip("runs as root")
    with pytest.raises(Refused) as e:
        make_cgroup(_leaf(cg), root=cg)
    assert e.value.name == "cgroup-ancestor-untrusted"


def test_make_cgroup_default_root_is_the_cgroup_fs():
    """The path start passes (wall.cgroup_dir) lies under make_cgroup's default root."""
    import inspect
    root = inspect.signature(make_cgroup).parameters["root"].default
    assert root == "/sys/fs/cgroup" and wall.cgroup_dir(E0, SESSION).startswith(root + "/")


# --- F4: the view's ancestors of <S> and of each socket dir -------------------------------------------------------

def _view(slot, **kw):
    return wall.linux_view_args(slot, SESSION, host_links=LINKS, **kw)


def _dirs(a):
    """(mode, path) for each ``--perms <mode> --dir <path>`` pair, in order."""
    return [(a[i + 1], a[i + 3]) for i, x in enumerate(a) if x == "--perms" and a[i + 2] == "--dir"]


def test_view_golden_ancestor_rows_in_order():
    a = _view(E0, kind="interactive", queue_grant=False)
    start = a.index("/run", a.index("--tmpfs", a.index("/var/tmp"))) + 1
    assert a[start:] == (
        "--perms", "0755", "--dir", "/var/levain",
        "--perms", "0711", "--dir", "/var/levain/live",
        "--bind", "/var/levain/live/" + SESSION, "/var/levain/live/" + SESSION,
        "--perms", "0755", "--dir", "/run/levain",
        "--perms", "0755", "--dir", "/run/levain/gw",
        "--bind", "/run/levain/gw/slot0", "/run/levain/gw/slot0",
        "--chdir", "/")


def test_view_verifier_makes_only_the_store_ancestors():
    assert _dirs(_view(V0, kind="verifier", queue_grant=False)) == [("0755", "/var/levain"),
                                                                     ("0711", "/var/levain/live")]


def test_view_each_ancestor_precedes_its_bind_and_is_made_once():
    a = _view(E1, kind="planner", queue_grant=True)
    dirs = [d for _, d in _dirs(a)]
    assert len(dirs) == len(set(dirs))
    assert dirs == ["/var/levain", "/var/levain/live", "/run/levain", "/run/levain/gw", "/run/levain/qb",
                    "/run/levain/planner"]
    binds = [(i, a[i + 2]) for i, x in enumerate(a) if x == "--bind"]
    for i, dest in binds:
        made = {a[j + 1] for j in range(i) if a[j] == "--dir"}
        parent = dest.rsplit("/", 1)[0]
        while parent:
            # /var: made by the /var/tmp tmpfs (0755 in the VM's stat chain); /run: the tmpfs itself
            assert parent in made or parent in ("/var", "/run"), (dest, parent)
            parent = parent.rsplit("/", 1)[0]
        assert dest not in made


def test_view_ancestor_modes_equal_the_layout_rows():
    """One source: each mode the view gives is layout.py's row for that path (CONTRACT §2.1 L/ 0755, L/live/ 0711;
    §2.2 R/ and R/<grant>/ 0755)."""
    rows = {str(r.path): r.mode for r in layout.base_store_rows("linux", "op") + layout.base_run_rows("linux")}
    a = _view(E1, kind="planner", queue_grant=True)
    for mode, d in _dirs(a):
        assert int(mode, 8) == rows[d], d
    assert rows["/var/levain"] == 0o755 and rows["/var/levain/live"] == 0o711


def test_view_ancestor_mode_follows_a_changed_layout_row(monkeypatch):
    """The view reads the row, it does not restate it: change the row and the view's --perms follows."""
    real = layout.base_store_rows

    def rows(host, operator):
        return [layout.Row(r.path, r.owner, r.group, 0o751) if str(r.path) == "/var/levain/live" else r
                for r in real(host, operator)]
    monkeypatch.setattr(layout, "base_store_rows", rows)
    assert ("0751", "/var/levain/live") in _dirs(_view(V0, kind="verifier", queue_grant=False))


def test_view_ancestor_without_a_layout_row_refused(monkeypatch):
    real = layout.base_run_rows
    monkeypatch.setattr(layout, "base_run_rows",
                        lambda host: [r for r in real(host) if str(r.path) != "/run/levain/gw"])
    with pytest.raises(Refused) as e:
        _view(E0, kind="interactive", queue_grant=False)
    assert e.value.name == "view-path-off-list" and "/run/levain/gw" in e.value.detail
