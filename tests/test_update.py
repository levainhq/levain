"""Tests for levain.update — the update-the-set-together orchestration.

The reconcile is gated, ordered, and fail-safe. These drive it with an injected
``confirm`` (no TTY) and monkeypatched anneal/pip subprocess calls, asserting:
the env-mutating pip step is gated; the store schema is reconciled; migration
proposals are SURFACED but never auto-acked (anneal's never-clobber contract);
and the lock records reality after reconcile.
"""

from __future__ import annotations


import pytest

from levain import manifest, update
from levain.manifest import CompatSet, InstalledSet


def _capture():
    lines: list[str] = []
    return lines, lines.append


def _inst(**kw) -> InstalledSet:
    base = dict(levain="0.3.4", anneal="0.9.5", schema="partnership",
                migrate_acked="0.9.5", pending_count=0)
    base.update(kw)
    return InstalledSet(**base)


@pytest.fixture
def declared(monkeypatch):
    d = CompatSet(levain="0.3.4", anneal="0.9.5", schema="partnership")
    monkeypatch.setattr(manifest, "declared_set", lambda: d)
    return d


# --------------------------------------------------------------------------
# in-sync / dry-run
# --------------------------------------------------------------------------

def test_already_in_sync_returns_zero_and_refreshes_lock(declared, tmp_path, monkeypatch):
    monkeypatch.setattr(manifest, "discover_installed_set", lambda *a, **k: _inst())
    lines, emit = _capture()
    rc = update.run_update(tmp_path, emit=emit)
    assert rc == 0
    assert any("Already at the known-good set" in ln for ln in lines)
    # A pre-manifest install (no lock) gets recorded on a clean run.
    assert manifest.read_lock(tmp_path) == CompatSet("0.3.4", "0.9.5", "partnership")


def test_dry_run_changes_nothing_and_reports_drift(declared, tmp_path, monkeypatch):
    monkeypatch.setattr(manifest, "discover_installed_set",
                        lambda *a, **k: _inst(anneal="0.9.0"))
    pip_called = []
    monkeypatch.setattr(update.subprocess, "run", lambda *a, **k: pip_called.append(a))
    lines, emit = _capture()
    rc = update.run_update(tmp_path, dry_run=True, emit=emit)
    assert rc == 1  # actionable drift present
    assert pip_called == []  # nothing ran
    assert manifest.read_lock(tmp_path) is None  # nothing written
    assert any("dry-run" in ln.lower() for ln in lines)


def test_dry_run_on_clean_install_writes_no_lock(declared, tmp_path, monkeypatch):
    # --dry-run changes NOTHING even when the set is clean: the early-return lock
    # refresh must not fire under dry-run (codex final — the early-return used to
    # run before the dry-run check and wrote .levain/manifest.json).
    monkeypatch.setattr(manifest, "discover_installed_set", lambda *a, **k: _inst())
    lines, emit = _capture()
    rc = update.run_update(tmp_path, dry_run=True, emit=emit)
    assert rc == 0
    assert manifest.read_lock(tmp_path) is None


# --------------------------------------------------------------------------
# the gated pip step
# --------------------------------------------------------------------------

def test_anneal_behind_runs_pip_when_confirmed(declared, tmp_path, monkeypatch):
    # discover: behind first, then known-good after the (mocked) pip upgrade.
    seq = iter([_inst(anneal="0.9.0"), _inst(), _inst()])
    monkeypatch.setattr(manifest, "discover_installed_set", lambda *a, **k: next(seq))
    monkeypatch.setattr(update, "_run_anneal", lambda *a, **k: (True, ""))

    class _R:
        returncode = 0
        stdout = stderr = ""
    pip_cmds = []
    monkeypatch.setattr(update.subprocess, "run",
                        lambda cmd, **k: pip_cmds.append(cmd) or _R())
    lines, emit = _capture()
    rc = update.run_update(tmp_path, emit=emit, confirm=lambda _p: True)
    assert rc == 0
    assert any("pip" in " ".join(c) and "anneal-memory==0.9.5" in " ".join(c)
               for c in pip_cmds)
    assert manifest.read_lock(tmp_path) == CompatSet("0.3.4", "0.9.5", "partnership")


def test_anneal_behind_skips_pip_when_declined(declared, tmp_path, monkeypatch):
    monkeypatch.setattr(manifest, "discover_installed_set",
                        lambda *a, **k: _inst(anneal="0.9.0"))
    pip_called = []
    monkeypatch.setattr(update.subprocess, "run",
                        lambda *a, **k: pip_called.append(a))
    lines, emit = _capture()
    rc = update.run_update(tmp_path, emit=emit, confirm=lambda _p: False)
    assert pip_called == []  # declined => never ran pip
    assert any("Declined" in ln for ln in lines)
    assert rc == 1  # still drifting


def test_no_pip_flag_prints_command_without_running(declared, tmp_path, monkeypatch):
    monkeypatch.setattr(manifest, "discover_installed_set",
                        lambda *a, **k: _inst(anneal="0.9.0"))
    pip_called = []
    monkeypatch.setattr(update.subprocess, "run",
                        lambda *a, **k: pip_called.append(a))
    lines, emit = _capture()
    rc = update.run_update(tmp_path, no_pip=True, emit=emit, confirm=lambda _p: True)
    assert pip_called == []
    assert any("pip install" in ln and "anneal-memory==0.9.5" in ln for ln in lines)
    assert rc == 1


# --------------------------------------------------------------------------
# schema + migrate
# --------------------------------------------------------------------------

def test_schema_drift_runs_set_schema(declared, tmp_path, monkeypatch):
    seq = iter([_inst(schema="default"), _inst(), _inst()])
    monkeypatch.setattr(manifest, "discover_installed_set", lambda *a, **k: next(seq))
    anneal_calls = []
    monkeypatch.setattr(update, "_run_anneal",
                        lambda store, ap, args, **k: anneal_calls.append(args) or (True, "ok"))
    lines, emit = _capture()
    rc = update.run_update(tmp_path, emit=emit, confirm=lambda _p: True)
    assert ["set-schema", "partnership"] in anneal_calls
    assert rc == 0


def test_migrate_pending_is_surfaced_not_acked_by_default(declared, tmp_path, monkeypatch):
    monkeypatch.setattr(manifest, "discover_installed_set",
                        lambda *a, **k: _inst(pending_count=4))
    calls = []
    monkeypatch.setattr(update, "_run_anneal",
                        lambda store, ap, args, **k: calls.append(args) or (True, "(proposals)"))
    lines, emit = _capture()
    update.run_update(tmp_path, emit=emit, confirm=lambda _p: True)
    assert ["migrate", "check"] in calls       # proposals SHOWN
    assert not any(a[:2] == ["migrate", "ack"] for a in calls)  # never auto-acked
    assert any("--ack" in ln for ln in lines)  # told how to ack after review


def test_ack_flag_advances_marker_after_review(declared, tmp_path, monkeypatch):
    monkeypatch.setattr(manifest, "discover_installed_set",
                        lambda *a, **k: _inst(pending_count=4))
    calls = []
    monkeypatch.setattr(update, "_run_anneal",
                        lambda store, ap, args, **k: calls.append(args) or (True, "ok"))
    update.run_update(tmp_path, ack=True, emit=lambda _l: None, confirm=lambda _p: True)
    assert ["migrate", "ack", "0.9.5"] in calls  # acks to the installed version


def test_ack_skipped_when_proposals_unreadable(declared, tmp_path, monkeypatch):
    # If the `migrate check` DISPLAY read fails, --ack must NOT ack proposals the
    # operator never saw this run (codex L3 HIGH).
    monkeypatch.setattr(manifest, "discover_installed_set",
                        lambda *a, **k: _inst(pending_count=4))
    calls = []

    def fake_anneal(store, ap, args, **k):
        calls.append(args)
        if args[:2] == ["migrate", "check"]:
            return (False, "boom")  # the proposal display FAILS
        return (True, "ok")

    monkeypatch.setattr(update, "_run_anneal", fake_anneal)
    lines, emit = _capture()
    update.run_update(tmp_path, ack=True, emit=emit, confirm=lambda _p: True)
    assert not any(a[:2] == ["migrate", "ack"] for a in calls)  # never acked
    assert any("--ack SKIPPED" in ln for ln in lines)


def test_ack_skipped_when_installed_anneal_unknown(declared, tmp_path, monkeypatch):
    # pending>0 but the installed anneal version is unknown (a partial migrate JSON:
    # pending present, installed_version absent) -> --ack must NOT ack (the target
    # is undeterminable, would fall back to known-good); marker unchanged. (codex
    # re-verify.)
    monkeypatch.setattr(manifest, "discover_installed_set",
                        lambda *a, **k: _inst(pending_count=4, anneal=None))
    calls = []
    monkeypatch.setattr(update, "_run_anneal",
                        lambda store, ap, args, **k: calls.append(args) or (True, "ok"))
    lines, emit = _capture()
    update.run_update(tmp_path, ack=True, emit=emit, confirm=lambda _p: True)
    assert not any(a[:2] == ["migrate", "ack"] for a in calls)
    assert any("installed anneal version is unknown" in ln for ln in lines)


def test_in_sync_lock_write_failure_returns_one(declared, tmp_path, monkeypatch):
    # Even on a clean set, if the lock cannot be recorded the early-return path
    # must NOT exit 0 — there's no provenance baseline (codex re-verify).
    monkeypatch.setattr(manifest, "discover_installed_set", lambda *a, **k: _inst())

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(manifest, "write_lock", boom)
    lines, emit = _capture()
    rc = update.run_update(tmp_path, emit=emit)
    assert rc == 1
    assert any("Could not record the compatibility lock" in ln for ln in lines)


def test_ack_success_rediscovers_and_returns_zero(declared, tmp_path, monkeypatch):
    # After a successful ack the pending set clears -> the final verdict must see
    # it (re-discover) so --ack returns 0, not 1 (codex L3 MED).
    seq = iter([_inst(pending_count=4), _inst(pending_count=0)])  # before / after ack
    monkeypatch.setattr(manifest, "discover_installed_set", lambda *a, **k: next(seq))
    monkeypatch.setattr(update, "_run_anneal", lambda *a, **k: (True, "ok"))
    rc = update.run_update(tmp_path, ack=True, emit=lambda _l: None, confirm=lambda _p: True)
    assert rc == 0


# --------------------------------------------------------------------------
# _ack_target + helpers
# --------------------------------------------------------------------------

def test_ack_target_is_always_the_installed_version_never_past_it():
    # Ack to what `migrate check` showed (the installed version) — never to a
    # known-good that the installed pre-release only compares EQUAL to.
    assert update._ack_target("0.9.5", "0.9.5") == "0.9.5"
    assert update._ack_target("0.10.0", "0.9.5") == "0.10.0"  # newer installed -> ack installed
    assert update._ack_target("0.9.0", "0.9.5") == "0.9.0"    # older installed  -> ack installed
    assert update._ack_target("0.9.5rc1", "0.9.5") == "0.9.5rc1"  # pre-release -> ack the rc
    assert update._ack_target(None, "0.9.5") == "0.9.5"       # unknown -> fall back


def test_record_lock_skips_write_when_unverified(tmp_path):
    inst = InstalledSet(levain="0.3.4", anneal=None, schema=None,
                        migrate_acked=None, pending_count=None)
    written = update._record_lock(tmp_path, inst, lambda _l: None)
    # Honesty floor: an unverified set is NOT recorded, and _record_lock reports
    # that it did not write (so the caller can refuse to claim full success).
    assert written is False
    assert manifest.read_lock(tmp_path) is None


def test_record_lock_returns_true_when_written(tmp_path):
    written = update._record_lock(tmp_path, _inst(), lambda _l: None)
    assert written is True
    assert manifest.read_lock(tmp_path) == CompatSet("0.3.4", "0.9.5", "partnership")


# --------------------------------------------------------------------------
# regressions — the two HIGHs (L1 + L2)
# --------------------------------------------------------------------------

def test_unknown_discovery_fails_closed_not_reconciled(declared, tmp_path, monkeypatch):
    # All discovery fails -> UNKNOWN axes. update must NOT claim "reconciled",
    # must return 1, and must NOT poison the lock (L1 HIGH-1).
    inst = InstalledSet(levain="0.3.4", anneal=None, schema=None,
                        migrate_acked=None, pending_count=None)
    monkeypatch.setattr(manifest, "discover_installed_set", lambda *a, **k: inst)
    lines, emit = _capture()
    rc = update.run_update(tmp_path, emit=emit, confirm=lambda _p: True)
    assert rc == 1
    assert not any("Set reconciled to known-good" in ln for ln in lines)
    assert any("Could not VERIFY" in ln for ln in lines)
    assert manifest.read_lock(tmp_path) is None  # no poisoned baseline


def test_ahead_plus_lock_drift_never_downgrades(declared, tmp_path, monkeypatch):
    # The out-of-band upgrade: installed 0.10.0 (ahead of known-good 0.9.5), lock
    # 0.9.5 -> BOTH anneal=ahead AND anneal-lock=drift fire. update must NEVER run
    # pip (no downgrade of a working newer anneal), must print "NOT downgrading",
    # and must return 0 (ahead is advisory, not a failure). (L1 HIGH-2 / L2 HIGH-2.)
    # Real lock on disk (0.9.5) so the post-reconcile re-stamp + re-read works.
    manifest.write_lock(tmp_path, CompatSet("0.3.4", "0.9.5", "partnership"))
    monkeypatch.setattr(manifest, "discover_installed_set",
                        lambda *a, **k: _inst(anneal="0.10.0"))
    pip_called = []
    monkeypatch.setattr(update.subprocess, "run", lambda *a, **k: pip_called.append(a))
    lines, emit = _capture()
    rc = update.run_update(tmp_path, emit=emit, confirm=lambda _p: True)
    assert pip_called == []                              # NEVER downgraded
    assert any("NOT downgrading" in ln for ln in lines)
    assert rc == 0                                       # advisory, not a failure


def test_ack_with_no_pending_signals_noop(declared, tmp_path, monkeypatch):
    # update PROCEEDS (schema drift) but nothing is pending; --ack must SAY it had
    # nothing to acknowledge, not silently no-op the flag. (A fully in-sync set
    # early-returns before this, so drive a real reconcile.)
    seq = iter([_inst(schema="default", pending_count=0), _inst(), _inst()])
    monkeypatch.setattr(manifest, "discover_installed_set", lambda *a, **k: next(seq))
    monkeypatch.setattr(update, "_run_anneal", lambda *a, **k: (True, "ok"))
    lines, emit = _capture()
    update.run_update(tmp_path, ack=True, emit=emit, confirm=lambda _p: True)
    assert any("--ack" in ln and "marker unchanged" in ln for ln in lines)
