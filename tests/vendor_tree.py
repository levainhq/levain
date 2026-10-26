"""TEST ONLY: a vendor prefix that passes ``start``'s ``vendor`` step through the real check.

``start`` refuses ``vendor-not-installed:<vendor>`` unless root's copy of the vendor is in the vendor prefix
(``levain.slot.start.check_vendor_installed``, which reads ``levain.slot.root_tree.unit_problem``). A test of the slot
path that is not about that check takes :func:`vendor_installed` in its module (import it; it is autouse): the
module's ``start`` then runs the REAL check against :func:`build_vendor_prefix`'s tree, owned by the test's uid where a
deployment's is root's. tests/conftest.py replaces the step with a no-op for every test, but the root-run CI job runs
with ``--noconftest``, so a module that reaches the step needs this fixture to pass there. The check's own tests
(tests/test_slot_vendorbin.py) build their own trees, missing and malformed ones included."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from levain.slot import root_tree as RT
from levain.slot import start as SS

#: The version directory name each package vendor's active link points at.
_VERSION = "0.0.0-test"


def _file(path: Path, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00")
    os.chmod(path, mode)


def build_vendor_prefix(prefix: Path) -> Path:
    """Every vendor of ``root_tree.VENDORS`` installed under ``prefix`` in the shape ``unit_problem`` accepts: a
    0755 regular file at ``bin/<name>``, or, for a ``VENDOR_UNITS`` package, ``<name>/<version>/`` holding the unit's
    required files and trees, with ``bin/<name>`` the link to its binary. Every directory 0755."""
    (prefix / "bin").mkdir(parents=True, exist_ok=True)
    for name in RT.VENDORS:
        unit = RT.VENDOR_UNITS.get(name)
        if unit is None:
            _file(prefix / "bin" / name, 0o755)
            continue
        root = prefix / name / _VERSION
        for rel, mode in unit.required.items():
            _file(root / rel, mode)
        for tree in unit.trees:
            (root / tree).mkdir(parents=True, exist_ok=True)
        os.symlink(f"../{name}/{_VERSION}/bin/{name}", prefix / "bin" / name)
    for d, dirs, _files in os.walk(prefix):
        os.chmod(d, 0o755)
    return prefix


@pytest.fixture(scope="session")
def vendor_prefix(tmp_path_factory) -> Path:
    return build_vendor_prefix(tmp_path_factory.mktemp("vendor-prefix") / "levain-vendor")


@pytest.fixture(autouse=True)
def vendor_installed(monkeypatch, vendor_prefix):
    """``start``'s ``vendor`` step runs the real check against :func:`vendor_prefix`. A test that means a missing or
    bad copy patches ``vendor_ready`` again itself."""
    uid = os.getuid()
    monkeypatch.setattr(SS, "vendor_ready",
                        lambda host, vendor: SS.check_vendor_installed(host, vendor, prefix=vendor_prefix,
                                                                       root_uid=uid))
