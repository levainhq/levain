"""spore-451: the memory wrap procedure (seed/memory_workflow.md) loads eagerly or on demand, per install and per
agent. Eager is the default; on-demand moves the file behind the carrier's pointer block with a retention summary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from levain import doctor
from levain import install as inst
from levain import packs
from levain.install import refresh_adapter
from tests.test_repin_on_install import make_install  # noqa: F401  (fixture)

WORKFLOW = "memory_workflow.md"
SEED = Path(packs.__file__).parent / "templates" / "seed"

# The head-written retention summary, pinned verbatim (PLAN_451 item 4): a reworded summary is a changed ruling.
SUMMARY = (
    "**Every session ends with a wrap: `prepare_wrap` -> compress -> `save_continuity`, all three, once.** "
    "When your operator signals the end (\"let's wrap up\", \"save memory\", \"we're done\" or any natural "
    "equivalent), run it rather than acknowledging it; an unwrapped session's experiences were never "
    "consolidated. The compression step is the thinking, not filing. A `graduations_demoted` result is the "
    "system working: never re-run `save_continuity` to chase a clean report. **Read this file before you run "
    "a wrap, or call `prepare_wrap` mid-session for stale-pattern warnings** — it holds the pre-compose "
    "recall, the contradiction scan, cold-pattern routing, durable facts and what the citation check does and "
    "does not catch."
)


def _roster(*names: str):
    return [packs.SeedEntry(name=n, path=SEED / n, mode="verbatim") for n in names]


ALL = ("origin.md", "partnership.md", "world.md", "memory.md", WORKFLOW, "spore_instructions.md",
       "continuity.md", "README.md")


def _init(tmp_path, capsys, name, adapter, force=False, **kw):
    from tests.test_init_answers import _filled

    af = tmp_path / f"{name}-answers.json"
    af.write_text(json.dumps(_filled(capsys)), encoding="utf-8")
    rc = inst.run_init(tmp_path / name, adapter, force, answers_file=af, **kw)
    return rc, tmp_path / name, capsys.readouterr().out


def _config(install: Path) -> dict:
    return json.loads((install / ".levain" / "config.json").read_text(encoding="utf-8"))


# --- the seed split ------------------------------------------------------------------------------------------

def test_the_wrap_procedure_is_its_own_seed_file_and_memory_md_points_at_it():
    memory = (SEED / "memory.md").read_text(encoding="utf-8")
    workflow = (SEED / WORKFLOW).read_text(encoding="utf-8")
    assert "prepare_wrap -> compress -> save_continuity" in memory
    assert f"`{WORKFLOW}`" in memory
    # the rules the entity runs every turn stay resident in every mode
    for rule in ("**Start of session:**", "**During work:**", "**Recording cadence:**",
                 "**Before decisions (NON-NEGOTIABLE):**"):
        assert rule in memory and rule not in workflow
    # the procedure moved whole
    for part in ("**Pair with staleness:**", "Pre-compose pattern recall", "Contradiction-scan",
                 "**Wrap once.**", "**Single-process invariant"):
        assert part in workflow and part not in memory
    assert workflow.count("the *why*, the package carries the authoritative contract") == 1


# --- the resolver --------------------------------------------------------------------------------------------

def test_the_resolver_gives_each_mode_its_import_order_and_on_demand_set():
    order, on_demand = packs.seed_classes("eager")
    assert order[order.index("memory.md") + 1] == WORKFLOW
    assert WORKFLOW not in on_demand
    order, on_demand = packs.seed_classes("on-demand")
    assert WORKFLOW not in order and order == ("origin.md", "partnership.md", "world.md", "memory.md")
    assert on_demand == ("spore_instructions.md", WORKFLOW)
    with pytest.raises(ValueError):
        packs.seed_classes("lazy")


@pytest.mark.parametrize("mode", ["eager", "on-demand"])
def test_the_classes_stay_disjoint_in_both_modes(mode):
    order, on_demand = packs.seed_classes(mode)
    packs.check_seed_classes_disjoint(order, on_demand, packs.NON_IMPORT_SEED)
    assert set(order) | set(on_demand) == set(packs.BASE_SEED_REACHABLE)


def test_the_roster_selectors_follow_the_mode():
    roster = _roster(*ALL)
    assert packs.import_names(roster, "eager") == ["origin.md", "partnership.md", "world.md", "memory.md", WORKFLOW]
    assert packs.on_demand_names(roster, "eager") == ["spore_instructions.md"]
    assert packs.import_names(roster, "on-demand") == ["origin.md", "partnership.md", "world.md", "memory.md"]
    assert packs.on_demand_names(roster, "on-demand") == ["spore_instructions.md", WORKFLOW]
    # no argument is eager: every pre-existing caller keeps its behaviour
    assert packs.import_names(roster) == packs.import_names(roster, "eager")


def test_the_workflow_has_a_verbatim_retention_summary():
    assert packs.ON_DEMAND_SUMMARY[WORKFLOW] == SUMMARY
    for mode in packs.MEMORY_LOAD_CHOICES:
        for name in packs.seed_classes(mode)[1]:
            assert packs.ON_DEMAND_SUMMARY.get(name, "").strip(), f"{name} is on-demand with no summary"


# --- the setting: parse, record, read ------------------------------------------------------------------------

def test_the_flag_parses_a_default_and_per_agent_choices():
    assert inst.parse_memory_load([]) == (None, {})
    assert inst.parse_memory_load(["on-demand"]) == ("on-demand", {})
    assert inst.parse_memory_load(["eager", "codex=on-demand", "claude-code=eager"]) == (
        "eager", {"codex": "on-demand", "claude-code": "eager"})
    assert inst.parse_memory_load(["codex=eager", "codex=on-demand"]) == (None, {"codex": "on-demand"})


@pytest.mark.parametrize("bad", ["lazy", "codex=lazy", "vim=on-demand", "=on-demand", "codex="])
def test_the_flag_refuses_an_unknown_mode_or_adapter(bad):
    with pytest.raises(inst.InitError):
        inst.parse_memory_load([bad])
    with pytest.raises(inst.InitError):
        inst.parse_memory_load(["eager", "on-demand"])


@pytest.mark.parametrize("body, expected", [
    (None, "eager"),
    ("{}", "eager"),
    ('{"memory_load": "eager"}', "eager"),
    ('{"memory_load": "on-demand"}', "on-demand"),
    ('{"memory_load": "On-Demand"}', "eager"),
    ('{"memory_load": 3}', "eager"),
    ('{"memory_load": "on-demand', "eager"),
    ("[]", "eager"),
    ('{"memory_load": "on-demand", "memory_load_by_adapter": {"claude-code": "eager"}}', "eager"),
    ('{"memory_load": "eager", "memory_load_by_adapter": {"claude-code": "on-demand"}}', "on-demand"),
    ('{"memory_load": "on-demand", "memory_load_by_adapter": {"codex": "eager"}}', "on-demand"),
    ('{"memory_load": "on-demand", "memory_load_by_adapter": {"claude-code": "nope"}}', "eager"),
    ('{"memory_load": "on-demand", "memory_load_by_adapter": ["x"]}', "on-demand"),
])
def test_the_recorded_choice_reads_back_and_anything_damaged_reads_eager(tmp_path, body, expected):
    install = tmp_path / "i"
    (install / ".levain").mkdir(parents=True)
    if body is not None:
        (install / ".levain" / "config.json").write_text(body, encoding="utf-8")
    assert inst.persisted_memory_load(install, "claude-code") == expected


# --- the carrier render, per adapter per mode ----------------------------------------------------------------

def test_a_default_install_imports_the_workflow(tmp_path, capsys, make_install):
    rc, install, out = _init(tmp_path, capsys, "x", "claude-code")
    assert rc == 0, out
    carrier = (install / "CLAUDE.md").read_text(encoding="utf-8")
    assert "@seed/memory.md\n@seed/memory_workflow.md\n" in carrier
    assert f"- `seed/{WORKFLOW}`" not in carrier
    assert (install / "seed" / WORKFLOW).is_file()
    assert "memory_load" not in _config(install)  # absent is eager


def test_claude_code_on_demand_points_at_the_workflow_with_its_summary(tmp_path, capsys, make_install):
    rc, install, out = _init(tmp_path, capsys, "x", "claude-code", memory_load=["on-demand"])
    assert rc == 0, out
    carrier = (install / "CLAUDE.md").read_text(encoding="utf-8")
    assert "@seed/memory_workflow.md" not in carrier
    assert "@seed/memory.md\n" in carrier
    assert f"- `seed/{WORKFLOW}` — {SUMMARY}" in carrier
    assert (install / "seed" / WORKFLOW).is_file()  # installed either way, so the pointer resolves
    assert _config(install)["memory_load"] == "on-demand"


def test_codex_on_demand_moves_the_workflow_off_the_numbered_read_list(tmp_path, capsys, make_install):
    rc, install, out = _init(tmp_path, capsys, "x", "codex", memory_load=["on-demand"])
    assert rc == 0, out
    carrier = (install / "AGENTS.md").read_text(encoding="utf-8")
    assert "`seed/memory_workflow.md` — Your Memory" not in carrier
    assert "4. `seed/memory.md` — Your Memory" in carrier and "\n5. " not in carrier
    assert f"- `seed/{WORKFLOW}` — {SUMMARY}" in carrier
    rc, eager, out = _init(tmp_path, capsys, "y", "codex")
    assert rc == 0, out
    assert "5. `seed/memory_workflow.md`" in (eager / "AGENTS.md").read_text(encoding="utf-8")


def test_a_per_agent_choice_changes_only_that_agents_carrier(tmp_path, capsys, make_install):
    rc, cc, out = _init(tmp_path, capsys, "a", "claude-code", memory_load=["codex=on-demand"])
    assert rc == 0, out
    assert "@seed/memory_workflow.md" in (cc / "CLAUDE.md").read_text(encoding="utf-8")
    assert _config(cc)["memory_load_by_adapter"] == {"codex": "on-demand"}
    rc, cx, out = _init(tmp_path, capsys, "b", "codex", memory_load=["codex=on-demand"])
    assert rc == 0, out
    assert "memory_workflow.md" not in (cx / "AGENTS.md").read_text(encoding="utf-8").split(
        "## Read these when you need them")[0]
    rc, cc2, out = _init(tmp_path, capsys, "c", "claude-code", memory_load=["on-demand", "claude-code=eager"])
    assert rc == 0, out
    assert "@seed/memory_workflow.md" in (cc2 / "CLAUDE.md").read_text(encoding="utf-8")


def test_a_refused_flag_writes_nothing(tmp_path, capsys, make_install):
    for bad in (["lazy"], ["vim=eager"], ["eager", "on-demand"]):
        rc, install, out = _init(tmp_path, capsys, "x", "claude-code", memory_load=bad)
        assert rc == 1 and "--memory-load" in out
        assert not install.exists()


# --- update and init --force keep the recorded mode ----------------------------------------------------------

@pytest.mark.parametrize("adapter", ["claude-code", "codex"])
def test_update_and_force_keep_on_demand_and_a_new_flag_changes_it(tmp_path, capsys, make_install, adapter):
    rc, install, out = _init(tmp_path, capsys, "x", adapter, memory_load=["on-demand"])
    assert rc == 0, out
    carrier = install / ("CLAUDE.md" if adapter == "claude-code" else "AGENTS.md")
    before = carrier.read_bytes()
    r = refresh_adapter(install, apply=True, emit=lambda _l: None)
    assert r.review == [], r
    assert carrier.read_bytes() == before
    rc, _, out = _init(tmp_path, capsys, "x", adapter, force=True)
    assert rc == 0, out
    assert carrier.read_bytes() == before
    assert "memory load: on-demand (kept from .levain/config.json)" in out
    rc, _, out = _init(tmp_path, capsys, "x", adapter, force=True, memory_load=["eager"])
    assert rc == 0, out
    assert WORKFLOW in carrier.read_text(encoding="utf-8")
    assert f"- `seed/{WORKFLOW}`" not in carrier.read_text(encoding="utf-8")
    assert _config(install)["memory_load"] == "eager"
    # the flip is a plain update of the carrier levain wrote, so `update` after it is quiet
    assert refresh_adapter(install, apply=True, emit=lambda _l: None).review == []


def test_an_unreadable_config_refuses_a_named_choice_before_any_write(tmp_path, capsys, make_install):
    install = make_install("x")
    cfg = install / ".levain" / "config.json"
    cfg.write_text("{broken", encoding="utf-8")
    carrier = (install / "CLAUDE.md").read_bytes()
    rc, _, out = _init(tmp_path, capsys, "x", "claude-code", force=True, memory_load=["on-demand"])
    assert rc == 1 and "memory load choice" in out
    assert cfg.read_text(encoding="utf-8") == "{broken"
    assert (install / "CLAUDE.md").read_bytes() == carrier


# --- doctor --------------------------------------------------------------------------------------------------

def _row(results, name):
    (r,) = [r for r in results if r.name == name]
    return r


@pytest.mark.parametrize("adapter", ["claude-code", "codex"])
def test_doctor_reports_the_mode_as_an_advisory_row_and_does_not_fail_on_it(tmp_path, capsys, make_install, adapter):
    check = doctor._check_claude_code if adapter == "claude-code" else doctor._check_codex
    rc, install, out = _init(tmp_path, capsys, "x", adapter)
    row = _row(check(install), "memory workflow")
    assert row.ok and not row.warn and row.detail == "eager"
    rc, install, out = _init(tmp_path, capsys, "y", adapter, memory_load=["on-demand"])
    results = check(install)
    row = _row(results, "memory workflow")
    assert row.ok and not row.warn and row.detail == "on-demand (summary in the carrier)"
    # the freshness check honours the mode: the workflow behind a pointer is not a stale eager entry...
    assert _row(results, "CLAUDE.md freshness" if adapter == "claude-code" else "AGENTS.md freshness").ok


def test_doctor_flags_an_on_demand_install_whose_carrier_still_imports_the_workflow(tmp_path, capsys, make_install):
    rc, install, out = _init(tmp_path, capsys, "x", "claude-code", memory_load=["on-demand"])
    carrier = install / "CLAUDE.md"
    carrier.write_text(carrier.read_text(encoding="utf-8").replace(
        "@seed/memory.md\n", "@seed/memory.md\n@seed/memory_workflow.md\n"), encoding="utf-8")
    row = _row(doctor._check_claude_code(install), "CLAUDE.md freshness")
    assert not row.ok and WORKFLOW in row.detail


def test_doctor_requires_the_workflow_seed_file():
    assert WORKFLOW in doctor._SEED_REQUIRED


# --- the budget script ---------------------------------------------------------------------------------------

def test_the_budget_script_passes_eager_and_prints_the_on_demand_figure(capsys):
    from scripts import check_seed_budget as budget

    assert budget.main([]) == 0
    out = capsys.readouterr().out
    assert "eager" in out and "on-demand memory workflow:" in out
    eager, *_ = budget.measure("eager")
    lazy, *_ = budget.measure("on-demand")
    assert lazy < eager <= budget.TOTAL_BYTES_LIMIT


# --- upgrade from a release that has no memory_workflow.md ---------------------------------------------------

def _as_released_before_the_split(install: Path, old_memory: str) -> None:
    """Put an install in the state an earlier release left it: the whole procedure in memory.md, no
    memory_workflow.md, a carrier that does not import it, and the adapter receipt recording those bytes."""
    receipt = inst._read_adapter_receipt(install)
    assert receipt is not None
    (install / "seed" / WORKFLOW).unlink()
    receipt.pop(f"seed/{WORKFLOW}")
    (install / "seed" / "memory.md").write_text(old_memory, encoding="utf-8")
    receipt["seed/memory.md"] = inst._sha256_text(old_memory)
    carrier = install / "CLAUDE.md"
    text = carrier.read_text(encoding="utf-8").replace("@seed/memory_workflow.md\n", "")
    carrier.write_text(text, encoding="utf-8")
    receipt["CLAUDE.md"] = inst._sha256_text(text)
    inst._write_adapter_receipt(install, receipt)


def test_update_lands_the_trimmed_memory_md_and_the_new_file_on_an_older_install(tmp_path, capsys, make_install):
    rc, install, out = _init(tmp_path, capsys, "x", "claude-code")
    assert rc == 0, out
    _as_released_before_the_split(install, "the old whole memory.md\n")
    r = refresh_adapter(install, apply=True, emit=lambda _l: None)
    assert r.review == [], r
    assert (install / "seed" / "memory.md").read_text(encoding="utf-8") == (SEED / "memory.md").read_text("utf-8")
    assert (install / "seed" / WORKFLOW).read_text(encoding="utf-8") == (SEED / WORKFLOW).read_text("utf-8")
    assert "@seed/memory_workflow.md\n" in (install / "CLAUDE.md").read_text(encoding="utf-8")
    assert refresh_adapter(install, apply=True, emit=lambda _l: None).review == []


def test_update_stages_an_edited_memory_md_and_still_lands_the_new_file(tmp_path, capsys, make_install):
    rc, install, out = _init(tmp_path, capsys, "x", "claude-code")
    assert rc == 0, out
    _as_released_before_the_split(install, "the old whole memory.md\n")
    mine = "the old whole memory.md\n- my own rule\n"
    (install / "seed" / "memory.md").write_text(mine, encoding="utf-8")
    lines: list[str] = []
    r = refresh_adapter(install, apply=True, emit=lines.append)
    assert "seed/memory.md" in r.review
    assert (install / "seed" / "memory.md").read_text(encoding="utf-8") == mine
    staged = install / ".levain" / "pending" / "seed" / "memory.md"
    assert staged.read_text(encoding="utf-8") == (SEED / "memory.md").read_text("utf-8")
    assert (install / "seed" / WORKFLOW).is_file()
    assert "yours is kept" in "\n".join(lines)
