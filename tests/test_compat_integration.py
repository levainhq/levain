"""Integration: the compatibility manifest folded into `levain doctor` and
`levain init` (apply_init's lock write)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

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
# install: apply_init writes the lock + acks a fresh install to the reconciled
# templates version (advance-only, capped at installed) — spore-216
# --------------------------------------------------------------------------

def test_record_compat_lock_writes_lock(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(manifest, "discover_installed_set", lambda *a, **k: _inst())
    install._record_compat_lock(tmp_path, store, "anneal-memory", emit=lambda _l: None)
    lock = manifest.read_lock(tmp_path)
    assert lock == CompatSet(manifest.declared_set().levain, "0.9.5", "partnership")


def test_record_compat_lock_acks_fresh_install_to_reconciled(tmp_path, monkeypatch):
    # A fresh install (marker None) acks UP to the templates-reconciled version
    # (capped at installed) so a current adopter sees a clean doctor (spore-216).
    store = _store(tmp_path)
    monkeypatch.setattr(manifest, "discover_installed_set",
                        lambda *a, **k: _inst(migrate_acked=None))
    calls = []
    monkeypatch.setattr(install, "_run_anneal_cmd",
                        lambda store, ap, args, **k: calls.append(args) or (True, "", []))
    install._record_compat_lock(tmp_path, store, "anneal-memory", emit=lambda _l: None)
    target = manifest.template_ack_target("0.9.5")  # == TEMPLATES_RECONCILED_ANNEAL
    assert ["migrate", "ack", target] in calls
    assert manifest.read_lock(tmp_path) is not None


def test_record_compat_lock_ack_is_advance_only(tmp_path, monkeypatch):
    # A --force re-install over a store already acked HIGHER than the reconciled
    # version must NOT be lowered (it would needlessly re-surface reviewed edits).
    store = _store(tmp_path)
    monkeypatch.setattr(manifest, "discover_installed_set",
                        lambda *a, **k: _inst(migrate_acked="0.9.0"))
    calls = []
    monkeypatch.setattr(install, "_run_anneal_cmd",
                        lambda store, ap, args, **k: calls.append(args) or (True, "", []))
    install._record_compat_lock(tmp_path, store, "anneal-memory", emit=lambda _l: None)
    assert not any(a[:2] == ["migrate", "ack"] for a in calls)


def test_record_compat_lock_no_reack_at_target(tmp_path, monkeypatch):
    # A store already acked EXACTLY at the target must not be redundantly re-acked
    # (strict `<` guard; guards a refactor to `<=` re-acking every --force, L1 LOW).
    store = _store(tmp_path)
    monkeypatch.setattr(manifest, "discover_installed_set",
                        lambda *a, **k: _inst(migrate_acked=manifest.TEMPLATES_RECONCILED_ANNEAL))
    calls = []
    monkeypatch.setattr(install, "_run_anneal_cmd",
                        lambda store, ap, args, **k: calls.append(args) or (True, "", []))
    install._record_compat_lock(tmp_path, store, "anneal-memory", emit=lambda _l: None)
    assert not any(a[:2] == ["migrate", "ack"] for a in calls)


def test_record_compat_lock_no_ack_when_anneal_unknown(tmp_path, monkeypatch):
    # Discovery failed -> no lock AND no ack (the cap is unverifiable; honesty floor).
    store = _store(tmp_path)
    monkeypatch.setattr(manifest, "discover_installed_set",
                        lambda *a, **k: _inst(anneal=None, schema=None, migrate_acked=None))
    calls = []
    monkeypatch.setattr(install, "_run_anneal_cmd",
                        lambda store, ap, args, **k: calls.append(args) or (True, "", []))
    install._record_compat_lock(tmp_path, store, "anneal-memory", emit=lambda _l: None)
    assert not any("ack" in str(c) for c in calls)


def test_record_compat_lock_skips_write_on_unknown(tmp_path, monkeypatch):
    # codex L3 HIGH: init must mirror update — NEVER record a declared-fallback
    # baseline when discovery failed (it would read as a verified compose).
    store = _store(tmp_path)
    monkeypatch.setattr(manifest, "discover_installed_set",
                        lambda *a, **k: _inst(anneal=None, schema=None))
    install._record_compat_lock(tmp_path, store, "anneal-memory", emit=lambda _l: None)
    assert manifest.read_lock(tmp_path) is None  # no poisoned baseline


# --------------------------------------------------------------------------
# spore-216: templates reconciled -> a fresh install shows zero pending drift
# --------------------------------------------------------------------------

def test_templates_reconciled_constant_within_known_good():
    # Release-gate invariant: you cannot reconcile the templates to a version you
    # don't ship. TEMPLATES_RECONCILED_ANNEAL <= KNOWN_GOOD_ANNEAL.
    assert manifest.version_tuple(manifest.TEMPLATES_RECONCILED_ANNEAL) <= \
        manifest.version_tuple(manifest.KNOWN_GOOD_ANNEAL)


def test_template_ack_target_caps_and_skips():
    assert manifest.template_ack_target("0.9.5") == manifest.TEMPLATES_RECONCILED_ANNEAL
    assert manifest.template_ack_target("0.4.0") == "0.4.0"   # installed older than reconciled -> cap at installed
    assert manifest.template_ack_target(None) is None         # unknown -> skip the ack


def test_seed_templates_carry_the_reconciled_guidance():
    # TEMPLATES_RECONCILED_ANNEAL asserts the seed templates incorporate the
    # migrate-manifest guidance through that version. Lock the two reconciled gaps
    # (spore-216) so a future template edit that DROPS them fails here — forcing a
    # re-review of the constant instead of silently making the init-ack dishonest.
    memory_md = (
        Path(install.__file__).parent / "templates" / "seed" / "memory.md"
    ).read_text(encoding="utf-8")
    # AM-LINKGATE (0.8.3): co-citation guidance, not a bare single-id example.
    assert "Co-citing 2+ episodes" in memory_md
    assert "[evidence: <id1>, <id2>" in memory_md
    # AM-MIGRATE-NOTIFY (0.4.7): the levain-native upgrade habit.
    assert "## On upgrade" in memory_md
    assert "levain update" in memory_md


@pytest.mark.skipif(
    shutil.which("anneal-memory") is None
    and subprocess.run([sys.executable, "-m", "anneal_memory", "--version"],
                       capture_output=True).returncode != 0,
    reason="anneal-memory not installed",
)
def test_fresh_install_acks_the_covered_entries(tmp_path):
    # After `_record_compat_lock` on a fresh store, anneal's own `migrate check`
    # drops the entries the seed templates cover (acked through the reconciled
    # version). At the current 0.4.8 cap that clears the spores + migrate-notify +
    # bare-path entries; the crystal-tier entries (0.7.1/0.8.2) + linkgate (0.8.3)
    # stay HONEST pending until the crystal-recall slice raises the cap to 0.8.3.
    store = tmp_path / ".levain" / "memory.db"
    store.parent.mkdir(parents=True)
    ap = shutil.which("anneal-memory") or "anneal-memory"
    subprocess.run(
        [sys.executable, "-m", "anneal_memory", "--db", str(store),
         "init", "--schema", "partnership"],
        capture_output=True, check=True,
    )
    before = manifest.discover_installed_set(store, ap)
    assert before.pending_count and before.pending_count > 0  # fresh store: all pending
    install._record_compat_lock(tmp_path, store, ap, emit=lambda _l: None)
    after = manifest.discover_installed_set(store, ap)
    # The ack REDUCED pending (covered entries cleared) but did not zero it (crystal
    # deferred) — honest: never suppresses an uncovered proposal.
    assert after.pending_count is not None
    assert 0 < after.pending_count < before.pending_count
    assert after.migrate_acked == manifest.TEMPLATES_RECONCILED_ANNEAL
