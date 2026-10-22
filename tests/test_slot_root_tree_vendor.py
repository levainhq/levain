"""3b-8a0: ``tree_steps`` vendors h11 into the root import tree through the same staged copy and checks as
``levain`` (CONTRACT c7.4's note; T:1/test_root_tree_vendors_deps)."""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

from levain.slot import Refused, root_tree

HOSTS = ("darwin", "linux")


def _tree(host):
    return f"{'/usr/local/lib' if host == 'darwin' else '/usr/lib'}/levain-root"


@pytest.mark.parametrize("host", HOSTS)
def test_root_tree_vendors_deps(host):
    h11_dir = Path("/venv/site-packages/h11")
    steps = root_tree.tree_steps(host, Path("/src/levain"), h11_dir=h11_dir)
    tree = _tree(host)
    argvs = [s.argv for s in steps if s.argv]
    cps = [a for a in argvs if a[0].endswith("cp")]
    assert [(a[-2], a[-1]) for a in cps] == [("/src/levain", f"{tree}/levain.new"), (str(h11_dir), f"{tree}/h11.new")]
    # h11 gets the whole staged discipline, in order: no-link check < chown -R < chmod -R < foreign check < swap
    why = [s.why for s in steps]
    start = next(i for i, a in enumerate(why) if "copy the h11 package" in a)
    seg = steps[start:]
    no_link = next(i for i, s in enumerate(seg) if s.call and "holds no link" in s.why)
    chown = next(i for i, s in enumerate(seg) if s.argv and s.argv[0].endswith("chown") and "-R" in s.argv)
    chmod = next(i for i, s in enumerate(seg) if s.argv and s.argv[0].endswith("chmod") and "-R" in s.argv)
    foreign = next(i for i, s in enumerate(seg) if s.call and "only root's plain files" in s.why)
    aside = next(i for i, s in enumerate(seg) if s.argv and s.argv[1:] == (f"{tree}/h11", f"{tree}/h11.old"))
    into = next(i for i, s in enumerate(seg) if s.argv and s.argv[1:] == (f"{tree}/h11.new", f"{tree}/h11"))
    assert no_link < chown < chmod < foreign < aside < into
    assert seg[aside].skip_if[-3:] == ("!", "-e", f"{tree}/h11")
    # the entries are unchanged: keys gets no installed entry in 3b
    assert {s.write[0].name for s in steps if s.write} == {"install-helper", "runtime-helper", "service-entry"}
    # the real h11 the installing Levain imports is the default source, and it passes the floor
    real = root_tree.h11_source()
    assert real.name == "h11" and (real / "__init__.py").is_file()
    assert root_tree.tree_steps(host, Path("/src/levain"))[0].why == steps[0].why


def test_root_tree_refuses_a_bad_h11(monkeypatch, tmp_path):
    pkg = tmp_path / "h11"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    fake = types.ModuleType("h11")
    fake.__file__, fake.__version__ = str(pkg / "__init__.py"), "0.15.9"
    monkeypatch.setitem(sys.modules, "h11", fake)
    with pytest.raises(Refused) as e:
        root_tree.h11_source()
    assert e.value.name == "bad-root" and "older" in str(e.value)
    fake.__version__, fake.__file__ = "0.16.0", "/x/h11.py"
    with pytest.raises(Refused):
        root_tree.h11_source()
    with pytest.raises(Refused):
        root_tree.tree_steps("linux", Path("/src/levain"), h11_dir=Path("/x/notit"))


def test_root_tree_refuses_a_symlinked_package(monkeypatch, tmp_path):
    """L3 r1 H1: a package source that is itself a link (``site-packages/h11 -> elsewhere``) is refused when
    planned, and a staged copy that is a link fails both checks (os.walk follows a top-level link)."""
    real = tmp_path / "elsewhere" / "h11"
    real.mkdir(parents=True)
    (real / "__init__.py").write_text("")
    site = tmp_path / "site-packages"
    site.mkdir()
    (site / "h11").symlink_to(real)
    (site / "levain").symlink_to(real)
    fake = types.ModuleType("h11")
    fake.__file__, fake.__version__ = str(site / "h11" / "__init__.py"), "0.16.0"
    monkeypatch.setitem(sys.modules, "h11", fake)
    for refuse in (root_tree.h11_source, lambda: root_tree.tree_steps("linux", Path("/src/levain"), h11_dir=site / "h11"),
                   lambda: root_tree.tree_steps("darwin", site / "levain", h11_dir=Path("/v/h11"))):
        with pytest.raises(Refused) as e:
            refuse()
        assert e.value.name == "bad-root" and "symlink" in str(e.value)
    staged = tmp_path / "tree" / "h11.new"
    staged.parent.mkdir()
    staged.symlink_to(real)  # what cp -R -P leaves for a linked source

    def as_root(p):
        st = os.lstat(p)
        return os.stat_result((st.st_mode & ~0o022, *tuple(st)[1:4], 0, 0, *tuple(st)[6:10]))

    for check in (root_tree._no_links, lambda t: root_tree._no_foreign_entries(t, lstat=as_root)):
        ok, why = check(staged)
        assert not ok and "not a real directory" in why
    # the real directory itself still passes
    assert root_tree._no_links(real) == (True, "")
    assert root_tree._no_foreign_entries(real, lstat=as_root) == (True, "")
    assert root_tree._no_links(tmp_path / "absent")[0] is False


@pytest.mark.parametrize("version,ok", [
    ("0.16.0", True), ("0.16.1rc1", True), ("0.16.0+vendor", True), ("0.17", True), ("1.0.0rc1", True),
    ("1.0", True), ("0.16.0.post1", True), ("0.15.9", False), ("0.15", False), ("0.15.99+vendor", False),
    ("0.9.0rc1", False),
])
def test_h11_version_floor(monkeypatch, tmp_path, version, ok):
    """L3 r1 V1: leading digits per release component, zero-padded; pre/post/local suffixes ignored."""
    pkg = tmp_path / "h11"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    fake = types.ModuleType("h11")
    fake.__file__, fake.__version__ = str(pkg / "__init__.py"), version
    monkeypatch.setitem(sys.modules, "h11", fake)
    if ok:
        assert root_tree.h11_source() == pkg
    else:
        with pytest.raises(Refused) as e:
            root_tree.h11_source()
        assert "older" in str(e.value)
