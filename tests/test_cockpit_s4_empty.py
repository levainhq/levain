"""K10 S4: a store a fresh install has not created yet is an EMPTY panel, never a fault."""

from __future__ import annotations

from pathlib import Path

import pytest

from levain.cockpit.providers import build_default_cockpit
from tests.test_cockpit_k1 import _install, _status


@pytest.mark.parametrize("pid", ["tray", "loops", "keep"])
def test_an_absent_spore_store_never_created_is_empty_not_error(tmp_path: Path, pid: str) -> None:
    root, src = _install(tmp_path)
    (root / ".levain" / "memory.spores.json").unlink()
    got = _status(build_default_cockpit(src), pid)
    assert got["status"] == "empty" and got["error"] is None, got
    assert got["rows"] == [] and "Nothing yet" in got["empty"]


def test_a_spore_store_seen_then_gone_is_still_a_fault(tmp_path: Path) -> None:
    root, src = _install(tmp_path)
    ck = build_default_cockpit(src)
    assert _status(ck, "tray")["status"] != "error"
    (root / ".levain" / "memory.spores.json").unlink()
    got = _status(ck, "tray")
    assert got["status"] == "error" and "disappeared" in got["error"]
