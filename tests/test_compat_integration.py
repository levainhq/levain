"""Integration: the compatibility manifest folded into `levain doctor` and
`levain init` (apply_init's lock write)."""

from __future__ import annotations

from pathlib import Path

from levain import doctor, install, manifest
from levain.manifest import CompatSet, InstalledSet


def _store(tmp_path: Path) -> Path:
    store = tmp_path / ".levain" / "memory.db"
    store.parent.mkdir(parents=True)
    store.write_bytes(b"")  # presence is enough — discovery is monkeypatched
    return store


def _inst(**kw) -> InstalledSet:
    base = dict(levain="0.3.4", anneal="0.9.5", schema="partnership",
                migrate_acked="0.9.5", pending_count=0)
    base.update(kw)
    return InstalledSet(**base)


# --------------------------------------------------------------------------
# doctor fold
# --------------------------------------------------------------------------

def test_doctor_compat_skips_when_no_store(tmp_path):
    # No store -> `_check_store` already reports it; compat doesn't double-fail.
    assert doctor._check_compat_set(tmp_path) == []


def test_doctor_compat_all_green_when_in_sync(tmp_path, monkeypatch):
    _store(tmp_path)
    monkeypatch.setattr(manifest, "discover_installed_set", lambda *a, **k: _inst())
    monkeypatch.setattr(manifest, "read_lock",
                        lambda _i: CompatSet("0.3.4", "0.9.5", "partnership"))
    results = doctor._check_compat_set(tmp_path)
    assert results and all(r.ok for r in results)
    assert any(r.name == "compat: pip-pin" for r in results)


def test_doctor_compat_fails_on_version_drift(tmp_path, monkeypatch):
    _store(tmp_path)
    monkeypatch.setattr(manifest, "discover_installed_set",
                        lambda *a, **k: _inst(anneal="0.9.0", schema="default"))
    monkeypatch.setattr(manifest, "read_lock", lambda _i: None)
    results = doctor._check_compat_set(tmp_path)
    by = {r.name: r for r in results}
    assert not by["compat: anneal"].ok   # behind = real FAIL
    assert not by["compat: schema"].ok   # drift = real FAIL


def test_doctor_compat_pending_is_advisory_not_fail(tmp_path, monkeypatch):
    # A fresh install legitimately has unreviewed proposals — that must NOT make
    # `levain doctor` report a FAIL (it would false-alarm a healthy install).
    _store(tmp_path)
    monkeypatch.setattr(manifest, "discover_installed_set",
                        lambda *a, **k: _inst(pending_count=6))
    monkeypatch.setattr(manifest, "read_lock",
                        lambda _i: CompatSet("0.3.4", "0.9.5", "partnership"))
    results = doctor._check_compat_set(tmp_path)
    migrate = next(r for r in results if r.name == "compat: migrate")
    assert migrate.ok                          # advisory, green
    assert "advisory" in migrate.detail        # but clearly labeled as a review queue
    assert "6" in migrate.detail


def test_doctor_compat_ahead_is_advisory_not_fail(tmp_path, monkeypatch):
    # anneal NEWER than known-good (within the pin) = pip allowed it, install
    # works, update won't downgrade -> advisory-green, never a doctor FAIL.
    _store(tmp_path)
    monkeypatch.setattr(manifest, "discover_installed_set",
                        lambda *a, **k: _inst(anneal="0.10.0"))
    monkeypatch.setattr(manifest, "read_lock",
                        lambda _i: CompatSet("0.3.4", "0.9.5", "partnership"))
    results = doctor._check_compat_set(tmp_path)
    anneal = next(r for r in results if r.name == "compat: anneal")
    assert anneal.ok
    assert "advisory" in anneal.detail


def test_doctor_pip_pin_unknown_does_not_red_operator(tmp_path, monkeypatch):
    from levain.manifest import AxisVerdict
    _store(tmp_path)
    monkeypatch.setattr(manifest, "discover_installed_set", lambda *a, **k: _inst())
    monkeypatch.setattr(manifest, "read_lock",
                        lambda _i: CompatSet("0.3.4", "0.9.5", "partnership"))
    monkeypatch.setattr(manifest, "pip_floor_verdict",
                        lambda: AxisVerdict("pip-pin", "unknown", "metadata unreadable"))
    pin = next(r for r in doctor._check_compat_set(tmp_path) if r.name == "compat: pip-pin")
    assert pin.ok  # an operator can't act on unreadable metadata


def test_doctor_pip_pin_is_advisory_never_fails_operator(tmp_path, monkeypatch):
    # pip-pin is a RELEASE-integrity gate, not operator-actionable — a drift (a
    # mis-cut wheel) is surfaced loudly but must NEVER fail an operator's doctor
    # (their exit code composes with pipelines; they can't fix a wheel).
    from levain.manifest import AxisVerdict
    _store(tmp_path)
    monkeypatch.setattr(manifest, "discover_installed_set", lambda *a, **k: _inst())
    monkeypatch.setattr(manifest, "read_lock",
                        lambda _i: CompatSet("0.3.4", "0.9.5", "partnership"))
    monkeypatch.setattr(manifest, "pip_floor_verdict",
                        lambda: AxisVerdict("pip-pin", "drift", "KNOWN_GOOD != the pin"))
    pin = next(r for r in doctor._check_compat_set(tmp_path) if r.name == "compat: pip-pin")
    assert pin.ok                          # advisory, not a failure
    assert "advisory" in pin.detail        # but loudly labeled


def test_doctor_surfaces_a_corrupt_lock(tmp_path, monkeypatch):
    # A CORRUPT lock (file exists but unreadable) is an UNKNOWN provenance state
    # worth a loud FAIL; an absent lock is benign and gets no line.
    _store(tmp_path)
    (tmp_path / ".levain" / "manifest.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(manifest, "discover_installed_set", lambda *a, **k: _inst())
    results = doctor._check_compat_set(tmp_path)
    lock = next((r for r in results if r.name == "compat: lock"), None)
    assert lock is not None and not lock.ok


def test_doctor_no_lock_line_when_absent(tmp_path, monkeypatch):
    _store(tmp_path)  # no manifest.json written
    monkeypatch.setattr(manifest, "discover_installed_set", lambda *a, **k: _inst())
    results = doctor._check_compat_set(tmp_path)
    assert not any(r.name == "compat: lock" for r in results)  # absent = benign


def test_doctor_compat_unknown_is_not_green(tmp_path, monkeypatch):
    _store(tmp_path)
    monkeypatch.setattr(manifest, "discover_installed_set",
                        lambda *a, **k: _inst(anneal=None, schema=None, pending_count=None))
    monkeypatch.setattr(manifest, "read_lock", lambda _i: None)
    results = doctor._check_compat_set(tmp_path)
    by = {r.name: r for r in results}
    assert not by["compat: anneal"].ok   # unknown is honestly NOT green
    assert not by["compat: schema"].ok


# --------------------------------------------------------------------------
# install: apply_init writes the lock, does NOT auto-ack
# --------------------------------------------------------------------------

def test_record_compat_lock_writes_lock(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(manifest, "discover_installed_set", lambda *a, **k: _inst())
    install._record_compat_lock(tmp_path, store, "anneal-memory", emit=lambda _l: None)
    lock = manifest.read_lock(tmp_path)
    assert lock == CompatSet(manifest.declared_set().levain, "0.9.5", "partnership")


def test_record_compat_lock_does_not_ack(tmp_path, monkeypatch):
    # Acking on a fresh install would silently suppress proposals the templates
    # may not yet cover — the worst failure direction for a drift tool. So the
    # lock is written but the migrate marker is NEVER advanced here.
    store = _store(tmp_path)
    monkeypatch.setattr(manifest, "discover_installed_set",
                        lambda *a, **k: _inst(pending_count=6))
    anneal_calls = []
    monkeypatch.setattr(install, "_run_anneal_cmd",
                        lambda *a, **k: anneal_calls.append(a) or (True, "", []))
    install._record_compat_lock(tmp_path, store, "anneal-memory", emit=lambda _l: None)
    assert not any("ack" in str(c) for c in anneal_calls)
    assert manifest.read_lock(tmp_path) is not None


def test_record_compat_lock_skips_write_on_unknown(tmp_path, monkeypatch):
    # codex L3 HIGH: init must mirror update — NEVER record a declared-fallback
    # baseline when discovery failed (it would read as a verified compose).
    store = _store(tmp_path)
    monkeypatch.setattr(manifest, "discover_installed_set",
                        lambda *a, **k: _inst(anneal=None, schema=None))
    install._record_compat_lock(tmp_path, store, "anneal-memory", emit=lambda _l: None)
    assert manifest.read_lock(tmp_path) is None  # no poisoned baseline
