"""Integration: the compatibility manifest folded into `levain doctor` and
`levain init` (apply_init's lock write)."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from anneal_memory import migration

from levain import doctor, install, manifest
from levain.manifest import CompatSet, InstalledSet


def _store(tmp_path: Path) -> Path:
    store = tmp_path / ".levain" / "memory.db"
    store.parent.mkdir(parents=True)
    store.write_bytes(b"")  # presence is enough — discovery is monkeypatched
    return store


def _inst(**kw) -> InstalledSet:
    base = dict(levain="0.3.4", anneal="0.9.6", schema="partnership",
                migrate_acked="0.9.6", pending_count=0)
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
                        lambda _i: CompatSet("0.3.4", "0.9.6", "partnership"))
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
                        lambda _i: CompatSet("0.3.4", "0.9.6", "partnership"))
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
                        lambda _i: CompatSet("0.3.4", "0.9.6", "partnership"))
    results = doctor._check_compat_set(tmp_path)
    anneal = next(r for r in results if r.name == "compat: anneal")
    assert anneal.ok
    assert "advisory" in anneal.detail


def test_doctor_pip_pin_unknown_does_not_red_operator(tmp_path, monkeypatch):
    from levain.manifest import AxisVerdict
    _store(tmp_path)
    monkeypatch.setattr(manifest, "discover_installed_set", lambda *a, **k: _inst())
    monkeypatch.setattr(manifest, "read_lock",
                        lambda _i: CompatSet("0.3.4", "0.9.6", "partnership"))
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
                        lambda _i: CompatSet("0.3.4", "0.9.6", "partnership"))
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
    assert lock == CompatSet(manifest.declared_set().levain, "0.9.6", "partnership")


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
    target = manifest.template_ack_target("0.9.6")  # == TEMPLATES_RECONCILED_ANNEAL
    assert ["migrate", "ack", target] in calls
    assert manifest.read_lock(tmp_path) is not None


def test_record_compat_lock_ack_is_advance_only(tmp_path, monkeypatch):
    # A store whose marker is ALREADY ahead of the ack target must NOT be lowered.
    # This fixture is the "marker ahead of the installed runtime" shape (acked 0.9.7
    # over installed 0.9.6 — e.g. a prior newer/manual ack, then run under an older
    # anneal): ack_target = min(reconciled 0.9.6, installed 0.9.6) = 0.9.6, acked 0.9.7
    # > 0.9.6 -> no ack. The cleaner "--force over an already-reviewed higher store"
    # shape is the companion test below.
    store = _store(tmp_path)
    monkeypatch.setattr(manifest, "discover_installed_set",
                        lambda *a, **k: _inst(migrate_acked="0.9.7"))
    calls = []
    monkeypatch.setattr(install, "_run_anneal_cmd",
                        lambda store, ap, args, **k: calls.append(args) or (True, "", []))
    install._record_compat_lock(tmp_path, store, "anneal-memory", emit=lambda _l: None)
    assert not any(a[:2] == ["migrate", "ack"] for a in calls)


def test_record_compat_lock_ack_advance_only_force_reinstall(tmp_path, monkeypatch):
    # The normal advance-only case (codex L3): a --force re-install over a store on a
    # NEWER anneal already acked at that newer version. installed 0.10.0, acked 0.10.0,
    # reconciled cap 0.9.6 -> ack_target = min(0.9.6, 0.10.0) = 0.9.6; acked 0.10.0 >
    # 0.9.6 -> no ack (never lower an already-reviewed higher marker).
    store = _store(tmp_path)
    monkeypatch.setattr(manifest, "discover_installed_set",
                        lambda *a, **k: _inst(anneal="0.10.0", migrate_acked="0.10.0"))
    calls = []
    monkeypatch.setattr(install, "_run_anneal_cmd",
                        lambda store, ap, args, **k: calls.append(args) or (True, "", []))
    install._record_compat_lock(tmp_path, store, "anneal-memory", emit=lambda _l: None)
    assert not any(a[:2] == ["migrate", "ack"] for a in calls)


def test_record_compat_lock_acks_prerelease_to_exact_runtime(tmp_path, monkeypatch):
    # A pre-release anneal runtime is tuple-equal to the cap (version_tuple collapses
    # the suffix) but must be acked to its EXACT string, never substituted with the
    # bare final cap label — a 0.9.6rc1 runtime acked as the final 0.9.6 would record
    # a compose that did not happen (codex L3; init now mirrors update._ack_target).
    store = _store(tmp_path)
    monkeypatch.setattr(manifest, "discover_installed_set",
                        lambda *a, **k: _inst(anneal="0.9.6rc1", migrate_acked=None))
    calls = []
    monkeypatch.setattr(install, "_run_anneal_cmd",
                        lambda store, ap, args, **k: calls.append(args) or (True, "", []))
    install._record_compat_lock(tmp_path, store, "anneal-memory", emit=lambda _l: None)
    assert ["migrate", "ack", "0.9.6rc1"] in calls


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
# spore-216/spore-218: templates reconciled through 0.9.6 -> a fresh install's
# pending drift drops to exactly the entries ABOVE the cap (uncovered, honestly
# surfaced)
# --------------------------------------------------------------------------

def test_templates_reconciled_constant_within_known_good():
    # Release-gate invariant: you cannot reconcile the templates to a version you
    # don't ship. TEMPLATES_RECONCILED_ANNEAL <= KNOWN_GOOD_ANNEAL.
    assert manifest.version_tuple(manifest.TEMPLATES_RECONCILED_ANNEAL) <= \
        manifest.version_tuple(manifest.KNOWN_GOOD_ANNEAL)


def test_template_ack_target_caps_and_skips():
    # Reconciled target when installed is at/above it; capped at installed when
    # older; skipped when unknown. Exercises the 0.9.6 boundary explicitly.
    assert manifest.template_ack_target("0.10.0") == manifest.TEMPLATES_RECONCILED_ANNEAL
    assert manifest.template_ack_target("0.9.7") == manifest.TEMPLATES_RECONCILED_ANNEAL  # just above -> cap
    assert manifest.template_ack_target(manifest.TEMPLATES_RECONCILED_ANNEAL) == \
        manifest.TEMPLATES_RECONCILED_ANNEAL                  # exactly at the cap -> the cap
    assert manifest.template_ack_target("0.9.5") == "0.9.5"   # the old known-good, now below the cap -> installed
    assert manifest.template_ack_target("0.8.2") == "0.8.2"   # just below the cap -> cap at installed
    assert manifest.template_ack_target("0.4.0") == "0.4.0"   # well below -> cap at installed
    assert manifest.template_ack_target(None) is None         # unknown -> skip the ack
    # Pre-release / post / local label of the cap: version_tuple collapses the suffix
    # so it compares EQUAL to the cap, but the ack records the EXACT runtime, never
    # the bare final cap label (codex L3 — init must mirror update._ack_target).
    assert manifest.template_ack_target("0.9.6rc1") == "0.9.6rc1"
    assert manifest.template_ack_target("0.9.6.post1") == "0.9.6.post1"


# Per-MANIFEST-ENTRY coverage sentinels: feature-code → a literal string that MUST
# appear in the seed templates, proving that entry's guidance landed. Keyed by
# `feature` (unique per entry) because two entries share version 0.4.7 — a
# version-keyed map could not hold both.
_COVERAGE_SENTINELS = {
    "AM-SPORES-BOUNDARY": "spore_add",                  # the prospective-layer tools
    "AM-MIGRATE-NOTIFY": "levain update",               # the upgrade habit
    "AM-CRYSTAL": "crystallization candidates",         # crystallize-OUT routing
    "AM-MCP-CRYSTAL": "crystal_recall",                 # the MCP read surface
    "AM-LINKGATE": "Co-citing 2+ episodes",             # co-citation, not single-id
}
# EXPLICIT allowlist of entries that genuinely require NO template edit, so "no
# sentinel" is a reviewed decision rather than an omission.
# - AM-PRESERVE-BARE-PATH (0.4.8) is a transparent engine fix — its own manifest
#   suggested_edit says so.
# - AM-WRAP-GENERATED (0.9.6) is "disposition, not text" — it retires a static
#   WRAP_PROTOCOL.md companion file. Levain's seed carries NO such companion, and
#   memory.md's wrap guidance is already INLINE and points at `prepare_wrap` as the
#   source of truth ("follow what it emits ... the package carries the authoritative
#   contract") — exactly the inline-methodology end-state the entry asks for, so there
#   is nothing to archive and no text to add. Reviewed no-edit (spore-218). UNLIKE the
#   textless engine fix above, this entry HAS a checkable end-state, so it is NOT left
#   to manual review: `test_am_wrap_generated_inline_end_state_is_structurally_guarded`
#   enforces it every run (positive prepare_wrap pointer + negative no-companion-file),
#   so a future seed edit that froze the wrap steps fails CI instead of silently making
#   the init-ack dishonest (L1 MED-1).
_NO_EDIT_REQUIRED = {"AM-PRESERVE-BARE-PATH", "AM-WRAP-GENERATED"}


def test_seed_templates_carry_the_reconciled_guidance():
    # Manifest-DRIVEN coverage (spore-216 L1 finding): TEMPLATES_RECONCILED_ANNEAL
    # asserts the seed templates incorporate the migration-manifest guidance through
    # that version. Iterate the MANIFEST ITSELF — for every entry at/below the
    # reconciled cap, assert a per-entry coverage sentinel (or an explicit no-edit
    # allowlist). A future template edit that DROPS any covered guidance, OR a new
    # manifest entry reconciled-but-unmapped, fails HERE — instead of silently
    # making the init-ack dishonest.
    seed_dir = Path(install.__file__).parent / "templates" / "seed"
    seed_text = "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted(seed_dir.rglob("*")) if p.is_file()
    )
    reconciled = manifest.version_tuple(manifest.TEMPLATES_RECONCILED_ANNEAL)

    checked = 0
    for entry in migration.MIGRATION_MANIFEST:
        if manifest.version_tuple(entry["version"]) > reconciled:
            continue  # past the cap — not claimed as covered, no sentinel required
        feature = entry["feature"]
        if feature in _NO_EDIT_REQUIRED:
            continue
        sentinel = _COVERAGE_SENTINELS.get(feature)
        assert sentinel is not None, (
            f"{feature} (v{entry['version']}) is reconciled (<= "
            f"{manifest.TEMPLATES_RECONCILED_ANNEAL}) but has no coverage sentinel "
            f"or no-edit allowlist entry — add its guidance + a sentinel here, or "
            f"allowlist it as no-edit-required."
        )
        assert sentinel in seed_text, (
            f"{feature} (v{entry['version']}) guidance missing from the seed "
            f"templates — sentinel {sentinel!r} not found. The init-ack would "
            f"silently suppress this proposal; restore the guidance or lower "
            f"TEMPLATES_RECONCILED_ANNEAL."
        )
        checked += 1
    # Non-vacuous: at the 0.9.6 cap we exercise spores + migrate-notify + crystal +
    # mcp-crystal + linkgate via sentinels (0.4.8 AM-PRESERVE-BARE-PATH and 0.9.6
    # AM-WRAP-GENERATED are allowlisted no-edit, so they don't add to the count).
    assert checked >= 5

    # The linkgate example is co-citation, not a bare single-id (AM-LINKGATE detail).
    assert "[evidence: <id1>, <id2>" in seed_text


def test_am_wrap_generated_inline_end_state_is_structurally_guarded():
    # AM-WRAP-GENERATED (0.9.6) is allowlisted no-edit in `_NO_EDIT_REQUIRED`, so the
    # generic coverage loop above skips it. But UNLIKE the textless engine-fix
    # AM-PRESERVE-BARE-PATH, this entry HAS a checkable end-state: raising the cap to
    # 0.9.6 makes `levain init` auto-ack it, and that is honest ONLY while the seed
    # carries NO static WRAP_PROTOCOL.md companion AND its wrap guidance stays INLINE,
    # pointing at `prepare_wrap` as the source of truth (the inline-methodology branch
    # of the entry's suggested_edit). This guard enforces that end-state every run, so a
    # future seed edit that froze the wrap mechanics into a companion file — or
    # de-inlined memory.md and dropped the prepare_wrap pointer — fails CI instead of
    # silently making the init-ack dishonest (L1 MED-1, spore-218).
    templates_root = Path(install.__file__).parent / "templates"
    memory_md = (templates_root / "seed" / "memory.md").read_text(encoding="utf-8")

    # (1) Positive: the wrap guidance is inline and defers to `prepare_wrap` as the
    #     authoritative source — not a frozen, drifting copy of the mechanics.
    assert "prepare_wrap" in memory_md
    assert "the package carries the authoritative contract" in memory_md, (
        "seed/memory.md no longer points at `prepare_wrap` as the wrap-mechanics "
        "source of truth — AM-WRAP-GENERATED's inline end-state is broken, so the "
        "init-ack at the 0.9.6 cap would silently suppress a now-legitimate proposal."
    )

    # (2) Negative: no static WRAP_PROTOCOL.md-style companion file ANYWHERE in the
    #     rendered templates (seed OR adapters), and nothing references/@imports one.
    companion_files = [
        p for p in templates_root.rglob("*")
        if p.is_file() and re.search(r"wrap[_-]?protocol", p.name, re.IGNORECASE)
    ]
    assert not companion_files, (
        f"a static wrap-protocol companion file appeared in the templates "
        f"({[str(p) for p in companion_files]}) — AM-WRAP-GENERATED retires such a "
        f"doc; it must not ship in the seed while the entry is allowlisted no-edit."
    )
    text_files = "\n".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in sorted(templates_root.rglob("*"))
        if p.is_file() and p.suffix in {".md", ".template", ".json", ".toml", ".py"}
    )
    assert not re.search(r"wrap[_-]?protocol\.md", text_files, re.IGNORECASE), (
        "a template references a WRAP_PROTOCOL.md companion (a pointer or @import) — "
        "retire the reference; `prepare_wrap` generates the wrap mechanics in-context."
    )

    # (3) Negative, defence-in-depth: the SEED is inline prose by design — it uses
    #     Jinja only for variable substitution, never file composition. Assert it
    #     carries NO include/extends/import directive, so a future edit that pulls a
    #     (possibly variable-named, regex-evading) companion doc into the rendered
    #     seed trips here rather than slipping past the filename scan above (L3 MED-2).
    seed_text_all = "\n".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in sorted((templates_root / "seed").rglob("*"))
        if p.is_file()
    )
    composition_directive = re.search(
        r"\{%-?\s*(?:include|extends|import|from)\s|@import\b", seed_text_all
    )
    assert composition_directive is None, (
        "a seed template now uses a file-composition directive "
        f"({composition_directive.group(0)!r}) — the seed is inline by design; a "
        "composed-in companion could reintroduce a static, drifting wrap protocol "
        "that the filename scan above would miss."
    )


@pytest.mark.skipif(
    shutil.which("anneal-memory") is None
    and subprocess.run([sys.executable, "-m", "anneal_memory", "--version"],
                       capture_output=True).returncode != 0,
    reason="anneal-memory not installed",
)
def test_fresh_install_acks_the_covered_entries(tmp_path):
    # After `_record_compat_lock` on a fresh store, anneal's own `migrate check`
    # drops exactly the entries the seed templates cover (acked through the
    # reconciled cap). What stays pending is exactly the manifest entries ABOVE the
    # cap but <= the installed anneal — the entries the seed honestly does NOT yet
    # reconcile. We compute that expected count from the LIVE manifest so this stays
    # correct as anneal ships entries above the cap: with the cap at 0.9.6 and an
    # installed 0.9.6 nothing is above it, so this asserts 0; the moment anneal ships
    # an entry above 0.9.6 it asserts 1 without a code change. (A bare `== 0` was the
    # off-by-one — it only held while the installed manifest topped out at the cap;
    # codex/L3 + L1 caught it. spore-218 raised the cap to 0.9.6 ONLY after the seed
    # reconciled AM-WRAP-GENERATED — a no-edit disposition entry — never to silence a
    # pending count.)
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
    # Honest expected pending = manifest entries strictly above the reconciled cap
    # but <= the installed anneal version (these are uncovered by construction).
    reconciled_t = manifest.version_tuple(manifest.TEMPLATES_RECONCILED_ANNEAL)
    installed_t = manifest.version_tuple(after.anneal) if after.anneal else reconciled_t
    expected_pending = sum(
        1 for e in migration.MIGRATION_MANIFEST
        if reconciled_t < manifest.version_tuple(e["version"]) <= installed_t
    )
    assert after.pending_count == expected_pending
    assert after.migrate_acked == manifest.TEMPLATES_RECONCILED_ANNEAL
