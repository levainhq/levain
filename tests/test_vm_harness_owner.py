"""The VM bound: owner.json sidecars. Fake tart and temp dirs only; no VM is touched."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from scripts.vm_harness import owner

T0 = datetime(2026, 11, 7, 12, 0, 0)


def _read(home, name):
    return json.loads((home / "vms" / name / "owner.json").read_text())


def test_clone_writes_owner_json(tmp_path):
    calls = []
    rec = owner.clone_vm("src", "s10b-mac", "seat-63", "drill", home=tmp_path,
                         run=lambda cmd, **kw: calls.append(cmd), now=lambda: T0)
    assert calls == [["tart", "clone", "src", "s10b-mac"]]
    disk = _read(tmp_path, "s10b-mac")
    assert disk == rec
    assert disk["owner"] == "seat-63" and disk["purpose"] == "drill" and disk["pin"] is False
    assert datetime.fromisoformat(disk["expires"]) - datetime.fromisoformat(disk["created"]) == timedelta(days=7)


def test_base_image_written_pinned(tmp_path):
    for name in ("p72-vm-x", "vend-real-abc"):
        owner.clone_vm("src", name, "seat-63", "base", home=tmp_path, run=lambda *a, **k: None, now=lambda: T0)
        assert _read(tmp_path, name)["pin"] is True


def test_pin_and_extend_keep_other_keys(tmp_path):
    owner.write_owner("vm1", "seat-63", "why", home=tmp_path, now=lambda: T0)
    before = _read(tmp_path, "vm1")
    owner.extend("vm1", "2026-12-01", home=tmp_path)
    mid = _read(tmp_path, "vm1")
    assert mid["expires"] == "2026-12-01" and mid["pin"] is False
    assert {k: v for k, v in mid.items() if k != "expires"} == {k: v for k, v in before.items() if k != "expires"}
    owner.pin("vm1", home=tmp_path)
    after = _read(tmp_path, "vm1")
    assert after["pin"] is True and after["expires"] == "2026-12-01" and after["owner"] == "seat-63"
    owner.pin("existing-base", owner="seat-63", home=tmp_path, now=lambda: T0)   # no sidecar yet
    assert _read(tmp_path, "existing-base")["pin"] is True
