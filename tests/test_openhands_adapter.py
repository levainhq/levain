"""The hookless `openhands` adapter — install path + doctor + verify (spore-277 step 3).

`openhands` is the first HOOKLESS adapter: it lays down seed + store + a config marker, but
NO activation tree, NO hooks, NO MCP (its activation is the runtime `LevainCondenser`, wired by
`levain run`). These tests lock that shape and prove `doctor` / `verify-hooks` stay GREEN on such
an install instead of FAILing on the activation tree they legitimately have none of.

No `openhands` extra needed — the install/doctor/verify paths are pure Python + the anneal-memory
CLI (the base dep). Only `levain run` itself needs the SDK (see test_run.py).
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from levain.dashboard import installed_adapter
from levain.doctor import _check_install_layout, _check_openhands, run_doctor
from levain.install import (
    HOOKLESS_ADAPTERS,
    _adapter_has_hooks,
    _manifest_rows,
    _next_steps_lines,
    _resolve_adapter,
    _templates_root,
    _write_adapter_marker,
    apply_init,
    effective_adapter,
    hosted_artifacts,
)
from levain.interview import build_field_plan, parse_template
from levain.packs import compose_roster, render_entries, verbatim_entries
from levain.verify import run_verify_hooks


def _openhands_install(install: Path) -> None:
    """A real base (no-pack) openhands install — real seed render + real anneal store."""
    anneal = shutil.which("anneal-memory") or "anneal-memory"
    with _templates_root() as templates_root:
        roster = compose_roster([templates_root])
        specs = [parse_template(e.path) for e in render_entries(roster)]
        verbatim = verbatim_entries(roster)
        answers = {f.slot: f"VAL_{f.slot}" for f in build_field_plan(specs)}
        apply_init(
            install, "openhands", answers, templates_root,
            sys.executable, anneal, specs, verbatim,
        )


# ---------- the predicate + resolver ----------

def test_openhands_is_hookless():
    assert "openhands" in HOOKLESS_ADAPTERS
    assert _adapter_has_hooks("openhands") is False
    assert _adapter_has_hooks("claude-code") is True
    assert _adapter_has_hooks("codex") is True


def test_resolve_adapter_accepts_openhands():
    assert _resolve_adapter("openhands") == "openhands"


# ---------- the install shape ----------

def test_install_lays_down_seed_store_marker_no_hooks(tmp_path: Path):
    install = tmp_path / "ent"
    install.mkdir()
    _openhands_install(install)

    # seed + store present…
    assert (install / "seed" / "world.md").is_file()
    assert (install / ".levain" / "memory.db").is_file()
    # …the adapter marker recorded…
    assert json.loads((install / ".levain" / "config.json").read_text())["adapter"] == "openhands"
    assert installed_adapter(install) == "openhands"
    # …and NO hosted-harness wiring.
    assert not (install / "activation").exists()
    assert not (install / "CLAUDE.md").exists()
    assert not (install / "AGENTS.md").exists()
    assert not (install / ".claude").exists()
    assert not (install / ".mcp.json").exists()


def test_manifest_rows_include_config_marker(tmp_path: Path):
    install = tmp_path / "ent"
    install.mkdir()
    _openhands_install(install)
    store = install / ".levain" / "memory.db"
    labels = {(label, p.name) for label, p in _manifest_rows(install, "openhands", store)}
    assert ("adapter", "config.json") in labels
    # no hosted-harness rows leaked in
    assert not any(p.name in ("CLAUDE.md", "AGENTS.md", ".mcp.json") for _, p in
                   _manifest_rows(install, "openhands", store))


def test_next_steps_point_at_run_not_verify_hooks(tmp_path: Path):
    lines = "\n".join(_next_steps_lines(tmp_path, "openhands"))
    assert "levain run" in lines
    assert "verify-hooks" not in lines  # hookless — no hooks to smoke-test
    assert "sovereign" in lines.lower()


# ---------- the config marker writer ----------

def test_write_adapter_marker_fresh(tmp_path: Path):
    _write_adapter_marker(tmp_path, "openhands", lambda _m: None)
    assert json.loads((tmp_path / ".levain" / "config.json").read_text()) == {"adapter": "openhands"}


def test_write_adapter_marker_preserves_existing_keys(tmp_path: Path):
    cfg = tmp_path / ".levain" / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"entity_name": "Coyote", "surface_name": "X"}))
    _write_adapter_marker(tmp_path, "openhands", lambda _m: None)
    data = json.loads(cfg.read_text())
    assert data == {"entity_name": "Coyote", "surface_name": "X", "adapter": "openhands"}


def test_write_adapter_marker_refuses_unreadable_config(tmp_path: Path):
    cfg = tmp_path / ".levain" / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{ this is not json")
    notes: list[str] = []
    _write_adapter_marker(tmp_path, "openhands", notes.append)
    # config left untouched (not clobbered), and a note explains why.
    assert cfg.read_text() == "{ this is not json"
    assert any("unreadable" in n for n in notes)


# ---------- doctor ----------

def test_check_install_layout_skips_activation_when_hookless(tmp_path: Path):
    seed = tmp_path / "seed"
    seed.mkdir()
    for f in ("origin.md", "partnership.md", "world.md", "memory.md"):
        (seed / f).write_text("x")
    results = _check_install_layout(tmp_path, expect_hooks=False)
    activation = [r for r in results if r.name == "activation/"]
    assert activation and activation[0].ok  # PASS (n/a), not a missing-tree FAIL
    assert "n/a" in activation[0].detail
    # and it does NOT demand hooks
    assert not any(r.name == "activation/hooks/" for r in results)


def test_check_openhands_is_a_clean_pass(tmp_path: Path):
    # Reached only when the gate already confirmed no residue → a clean sovereign PASS.
    ok = _check_openhands(tmp_path)
    assert len(ok) == 1 and ok[0].ok and "sovereign entity" in ok[0].detail


def test_hosted_artifacts_detects_residue(tmp_path: Path):
    assert hosted_artifacts(tmp_path) == []
    (tmp_path / "CLAUDE.md").write_text("x")
    (tmp_path / "activation").mkdir()
    (tmp_path / ".mcp.json").write_text("{}")
    found = hosted_artifacts(tmp_path)
    assert set(found) == {"CLAUDE.md", "activation", ".mcp.json"}


def test_effective_adapter_is_the_shared_source_of_truth(tmp_path: Path):
    # None when empty.
    assert effective_adapter(tmp_path) is None
    # A clean openhands marker → openhands.
    (tmp_path / ".levain").mkdir()
    (tmp_path / ".levain" / "config.json").write_text(json.dumps({"adapter": "openhands"}))
    assert effective_adapter(tmp_path) == "openhands"
    # Hosted files DOMINATE a stale openhands marker (the --force switch): a CLAUDE.md present
    # → claude-code, NOT openhands (so doctor/verify/run all stop treating it as hookless).
    (tmp_path / "CLAUDE.md").write_text("# claude")
    assert effective_adapter(tmp_path) == "claude-code"


def test_effective_adapter_incoherent_residue_is_none(tmp_path: Path):
    # An openhands marker with non-tag residue (only .mcp.json) is neither a clean hookless
    # entity nor a tagged hosted install → None (incoherent), so `run`/`verify` refuse/verify.
    (tmp_path / ".levain").mkdir()
    (tmp_path / ".levain" / "config.json").write_text(json.dumps({"adapter": "openhands"}))
    (tmp_path / ".mcp.json").write_text("{}")
    assert effective_adapter(tmp_path) is None


def test_doctor_openhands_marker_with_hosted_residue_is_not_hookless(tmp_path: Path, capsys):
    # A stale openhands marker sitting on top of a claude tree (the openhands→claude --force
    # switch) must NOT be treated as hookless — the files are ground truth. doctor detects
    # claude-code and does NOT emit the hookless "sovereign entity" line or false-FAIL.
    install = tmp_path / "ent"
    install.mkdir()
    _openhands_install(install)  # writes the openhands marker + store + seed
    # ...then simulate a claude-code reinstall leaving its tag-file + hook tree:
    (install / "CLAUDE.md").write_text("# claude")
    run_doctor(install)
    out = capsys.readouterr().out
    assert "sovereign entity" not in out  # not treated as hookless
    assert "n/a — hookless adapter" not in out  # activation IS checked (expect_hooks=True)


def test_doctor_all_green_on_openhands_install(tmp_path: Path, capsys):
    install = tmp_path / "ent"
    install.mkdir()
    _openhands_install(install)
    rc = run_doctor(install)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "no adapter detected" not in out
    assert "openhands — sovereign entity" in out
    assert "All checks passed." in out


# ---------- verify-hooks ----------

def test_verify_hooks_is_clean_na_on_openhands(tmp_path: Path, capsys):
    install = tmp_path / "ent"
    install.mkdir()
    _openhands_install(install)
    rc = run_verify_hooks(install)
    assert rc == 0
    assert "hookless" in capsys.readouterr().out
