"""Tests for levain.manifest — the compatibility-manifest core.

Covers the pure state machine (version parsing, per-axis drift classification,
the honesty floor), lock I/O (atomic write + fail-soft read), the pip-pin
release-gate, and discovery against a real anneal store.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from levain import __version__, manifest
from levain.manifest import CompatSet, InstalledSet


# --------------------------------------------------------------------------
# version_tuple / _cmp
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("0.9.5", (0, 9, 5)),
    ("0.9.5rc1", (0, 9, 5)),
    ("0.9.5+ubuntu.1", (0, 9, 5)),
    ("0.10.0", (0, 10, 0)),
    ("0.5", (0, 5)),
    ("", ()),
    ("garbage", ()),
    ("v1.2", ()),
])
def test_version_tuple(raw, expected):
    assert manifest.version_tuple(raw) == expected


def test_cmp_orders_numerically_not_lexically():
    assert manifest._cmp("0.9.5", "0.10.0") == -1   # 9 < 10 numerically
    assert manifest._cmp("0.10.0", "0.9.5") == 1
    assert manifest._cmp("0.9.5", "0.9.5") == 0


def test_declared_set_uses_source_version_not_metadata():
    d = manifest.declared_set()
    # The source __version__ is the truth (importlib.metadata can lag an editable
    # install). The constant carries anneal + schema.
    assert d.levain == __version__
    assert d.anneal == manifest.KNOWN_GOOD_ANNEAL
    assert d.schema == manifest.KNOWN_GOOD_SCHEMA


# --------------------------------------------------------------------------
# compute_drift — the per-axis state machine
# --------------------------------------------------------------------------

def _declared() -> CompatSet:
    return CompatSet(levain="0.3.4", anneal="0.9.5", schema="partnership")


def _installed(**kw) -> InstalledSet:
    base = dict(
        levain="0.3.4", anneal="0.9.5", schema="partnership",
        migrate_acked="0.9.5", pending_count=0,
    )
    base.update(kw)
    return InstalledSet(**base)


def test_drift_all_in_sync():
    d = _declared()
    drift = manifest.compute_drift(d, _installed(), CompatSet("0.3.4", "0.9.5", "partnership"))
    assert drift.in_sync
    assert not drift.has_actionable_drift
    assert not drift.has_unknown


def test_drift_anneal_behind():
    drift = manifest.compute_drift(_declared(), _installed(anneal="0.9.0"), None)
    v = drift.of("anneal")
    assert v.status == "behind"
    assert not v.ok
    assert drift.has_actionable_drift


def test_drift_anneal_ahead_is_advisory_not_actionable():
    # `ahead` (installed newer than known-good) must NOT be actionable: `update`
    # would otherwise try to downgrade a working newer anneal (the L1+L2 bug).
    # It is honestly reported (not ok), but not a reconcile action.
    drift = manifest.compute_drift(_declared(), _installed(anneal="0.10.0"), None)
    assert drift.of("anneal").status == "ahead"
    assert not drift.of("anneal").ok
    assert not drift.has_actionable_drift


def test_drift_schema_mismatch():
    drift = manifest.compute_drift(_declared(), _installed(schema="default"), None)
    assert drift.of("schema").status == "drift"


def test_drift_migrate_pending():
    drift = manifest.compute_drift(_declared(), _installed(pending_count=3), None)
    v = drift.of("migrate")
    assert v.status == "pending"
    assert "3" in v.detail


def test_drift_anneal_changed_underneath_lock():
    # Installed anneal differs from the recorded lock = an out-of-band upgrade.
    lock = CompatSet(levain="0.3.4", anneal="0.9.5", schema="partnership")
    drift = manifest.compute_drift(_declared(), _installed(anneal="0.9.6"), lock)
    assert drift.of("anneal-lock") is not None
    assert drift.of("anneal-lock").status == "drift"


def test_drift_no_lock_skips_lock_axes():
    drift = manifest.compute_drift(_declared(), _installed(), None)
    assert drift.of("levain") is None       # levain-vs-lock skipped without a lock
    assert drift.of("anneal-lock") is None  # anneal-vs-lock skipped without a lock
    assert drift.in_sync


def test_drift_levain_upgraded_since_lock():
    lock = CompatSet(levain="0.3.3", anneal="0.9.5", schema="partnership")
    drift = manifest.compute_drift(_declared(), _installed(levain="0.3.4"), lock)
    v = drift.of("levain")
    assert v.status == "drift"
    assert "upgraded" in v.detail


def test_drift_honesty_floor_unknown_is_not_in_sync():
    # Every discovery failed -> None -> UNKNOWN, never a false green.
    inst = InstalledSet(levain="0.3.4", anneal=None, schema=None,
                        migrate_acked=None, pending_count=None)
    drift = manifest.compute_drift(_declared(), inst, None)
    assert drift.of("anneal").status == "unknown"
    assert drift.of("schema").status == "unknown"
    assert drift.of("migrate").status == "unknown"
    assert not drift.in_sync
    assert drift.has_unknown
    # unknown alone is NOT actionable (it means "could not determine", e.g. no store).
    assert not drift.has_actionable_drift


def test_axis_verdict_ok_only_for_in_sync():
    from levain.manifest import AxisVerdict
    assert AxisVerdict("x", "in_sync", "").ok
    for s in ("behind", "ahead", "drift", "pending", "unknown"):
        assert not AxisVerdict("x", s, "").ok


# --------------------------------------------------------------------------
# Lock I/O
# --------------------------------------------------------------------------

def test_lock_round_trip(tmp_path: Path):
    assert manifest.read_lock(tmp_path) is None
    cs = CompatSet(levain="0.3.4", anneal="0.9.5", schema="partnership")
    manifest.write_lock(tmp_path, cs)
    assert manifest.read_lock(tmp_path) == cs


def test_lock_stamps_recorded_at(tmp_path: Path):
    manifest.write_lock(tmp_path, CompatSet("0.3.4", "0.9.5", "partnership"))
    raw = json.loads(manifest.lock_path(tmp_path).read_text())
    assert "recorded_at" in raw and raw["recorded_at"]


def test_lock_path_location(tmp_path: Path):
    assert manifest.lock_path(tmp_path) == tmp_path / ".levain" / "manifest.json"


def test_read_lock_missing_is_none(tmp_path: Path):
    assert manifest.read_lock(tmp_path) is None


def test_read_lock_corrupt_json_is_none(tmp_path: Path):
    p = manifest.lock_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    assert manifest.read_lock(tmp_path) is None


def test_read_lock_status_distinguishes_absent_ok_corrupt(tmp_path: Path):
    # absent (no file) and corrupt (exists, unreadable) are DIFFERENT states —
    # collapsing them lets a corrupt lock read as a clean "nothing recorded".
    assert manifest.read_lock_status(tmp_path) == (None, "absent")

    cs = CompatSet("0.3.4", "0.9.5", "partnership")
    manifest.write_lock(tmp_path, cs)
    assert manifest.read_lock_status(tmp_path) == (cs, "ok")

    manifest.lock_path(tmp_path).write_text("{bad json", encoding="utf-8")
    assert manifest.read_lock_status(tmp_path) == (None, "corrupt")


def test_read_lock_status_missing_field_is_corrupt(tmp_path: Path):
    p = manifest.lock_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"levain": "0.3.4", "anneal": "0.9.5"}), encoding="utf-8")
    assert manifest.read_lock_status(tmp_path) == (None, "corrupt")  # not absent


def test_read_lock_missing_field_is_none(tmp_path: Path):
    p = manifest.lock_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"levain": "0.3.4", "anneal": "0.9.5"}), encoding="utf-8")
    assert manifest.read_lock(tmp_path) is None  # schema missing


def test_read_lock_non_string_field_is_none(tmp_path: Path):
    p = manifest.lock_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"levain": "0.3.4", "anneal": 95, "schema": "partnership"}),
                 encoding="utf-8")
    assert manifest.read_lock(tmp_path) is None


def test_write_lock_overwrites(tmp_path: Path):
    manifest.write_lock(tmp_path, CompatSet("0.3.3", "0.9.4", "partnership"))
    manifest.write_lock(tmp_path, CompatSet("0.3.4", "0.9.5", "partnership"))
    assert manifest.read_lock(tmp_path) == CompatSet("0.3.4", "0.9.5", "partnership")


def test_write_lock_leaves_no_tmp_sidecar(tmp_path: Path):
    manifest.write_lock(tmp_path, CompatSet("0.3.4", "0.9.5", "partnership"))
    leftovers = list((tmp_path / ".levain").glob("*.tmp"))
    assert leftovers == []


# --------------------------------------------------------------------------
# pip-pin release-gate
# --------------------------------------------------------------------------

def test_pip_floor_reads_anneal_lower_bound():
    # In the installed dist metadata the anneal floor is the pyproject `>=`.
    floor = manifest.pip_floor()
    assert floor is not None
    assert manifest.version_tuple(floor)  # parseable


@pytest.mark.parametrize("req, expected", [
    ("anneal-memory<0.10,>=0.9.5", "0.9.5"),           # this backend's order
    ("anneal-memory>=0.9.5,<0.10", "0.9.5"),           # source order
    ("anneal-memory>=0.9.5", "0.9.5"),                 # single specifier
    ("anneal-memory (>=0.9.5,<0.10)", "0.9.5"),        # PEP 508 parenthesized
    ("anneal-memory[extra]>=0.9.5,<0.10", "0.9.5"),    # with extras
    ("anneal-memory<0.10,>=0.9.5; python_version >= '3.12'", "0.9.5"),  # marker
    ('mcp<2,>=1.27; extra == "app"', None),            # not anneal-memory
    ("anneal_memory>=0.9.5", "0.9.5"),                 # underscore-normalized name
])
def test_pip_floor_parses_all_pin_forms(req, expected, monkeypatch):
    # The parser must be specifier-ORDER-INDEPENDENT — the old `,`-split + startswith
    # only matched when `>=` was not first (L2 HIGH: a backend reorder silently
    # broke the release-gate to UNKNOWN).
    monkeypatch.setattr(manifest, "_levain_requires", lambda: [req])
    assert manifest.pip_floor() == expected


def test_pip_floor_verdict_in_sync_when_constant_matches_floor():
    # The repo's KNOWN_GOOD_ANNEAL is kept == the pyproject floor; the gate is green.
    v = manifest.pip_floor_verdict()
    assert v.axis == "pip-pin"
    assert v.status in ("in_sync", "unknown")  # unknown only if metadata is unreadable


def test_pip_floor_verdict_flags_drift(monkeypatch):
    # If the manifest constant and the pip floor disagree, the gate catches it.
    monkeypatch.setattr(manifest, "KNOWN_GOOD_ANNEAL", "0.8.5")
    monkeypatch.setattr(manifest, "pip_floor", lambda: "0.9.5")
    v = manifest.pip_floor_verdict()
    assert v.status == "drift"


# --------------------------------------------------------------------------
# discover_installed_set — unit (monkeypatched) + integration (real store)
# --------------------------------------------------------------------------

def test_discover_parses_anneal_json(monkeypatch, tmp_path):
    def fake(store, anneal_path, sub_args, *, timeout=10.0):
        if sub_args[:2] == ["migrate", "check"]:
            return {"installed_version": "0.9.5", "acknowledged_version": "0.4.7",
                    "pending": [{"version": "0.8.3"}, {"version": "0.8.2"}]}
        if sub_args[:1] == ["status"]:
            return {"schema": "partnership"}
        return None
    monkeypatch.setattr(manifest, "_run_anneal_json", fake)
    inst = manifest.discover_installed_set(tmp_path / "memory.db", "anneal-memory")
    assert inst.anneal == "0.9.5"
    assert inst.schema == "partnership"
    assert inst.migrate_acked == "0.4.7"
    assert inst.pending_count == 2
    assert inst.errors == []


def test_discover_honesty_floor_on_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(manifest, "_run_anneal_json", lambda *a, **k: None)
    inst = manifest.discover_installed_set(tmp_path / "memory.db", "anneal-memory")
    assert inst.anneal is None
    assert inst.schema is None
    assert inst.pending_count is None
    assert len(inst.errors) == 2  # both reads recorded as failed, not silently zeroed


def test_discover_never_acked_is_none_not_error(monkeypatch, tmp_path):
    # acknowledged_version: null is a legitimate "never acked", not a failure.
    monkeypatch.setattr(manifest, "_run_anneal_json", lambda s, a, sub, **k: (
        {"installed_version": "0.9.5", "acknowledged_version": None, "pending": []}
        if sub[:2] == ["migrate", "check"] else {"schema": "partnership"}
    ))
    inst = manifest.discover_installed_set(tmp_path / "memory.db", "anneal-memory")
    assert inst.migrate_acked is None
    assert inst.pending_count == 0
    assert inst.errors == []


@pytest.mark.skipif(
    shutil.which("anneal-memory") is None
    and subprocess.run([sys.executable, "-m", "anneal_memory", "--version"],
                       capture_output=True).returncode != 0,
    reason="anneal-memory not installed",
)
def test_discover_against_real_store(tmp_path):
    store = tmp_path / ".levain" / "memory.db"
    store.parent.mkdir(parents=True)
    anneal_path = shutil.which("anneal-memory") or "anneal-memory"
    subprocess.run(
        [sys.executable, "-m", "anneal_memory", "--db", str(store),
         "init", "--schema", "partnership"],
        capture_output=True, check=True,
    )
    inst = manifest.discover_installed_set(store, anneal_path)
    assert inst.anneal is not None and manifest.version_tuple(inst.anneal)
    assert inst.schema == "partnership"
    # A fresh store has never acked, so every shipped migration entry is pending.
    assert inst.pending_count is not None and inst.pending_count > 0


def test_run_anneal_json_timeout_returns_none(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="x", timeout=1)
    monkeypatch.setattr(manifest.subprocess, "run", boom)
    assert manifest._run_anneal_json(tmp_path, "anneal-memory", ["status", "--json"]) is None
