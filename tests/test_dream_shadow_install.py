"""D1-3 completeness: the shadow INSTALL (the facts, the input hook, the run's lock/marker/instructions, the card).

Residue no test here holds: a real shadow night (an Opus compose on a seeded copy of flow's snapshot) and Phill
clearing its card. The compose is stubbed; what is asserted is what the install hands it."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import pytest

from levain.coordinator.store import CoordStore
from levain.dream import agent as da
from levain.dream import card as cd
from levain.dream import executor as ex
from levain.dream import ready as rd
from levain.dream import shadow as sh
from levain.dream.cli import add_parsers

sys.path.insert(0, str(Path(__file__).parent))
from test_dream_shadow import DAY, RUN, SHADOW_TEXT, Flow  # noqa: E402

ZONE = "America/New_York"


@pytest.fixture
def flow(tmp_path):
    return Flow(tmp_path)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))     # the live-store guard follows $HOME
    ag, ck, cdir, scratch = (tmp_path / n for n in ("ag", "ck", "cd", "scratch"))
    for d in (ag, ck, cdir):
        d.mkdir()
    instr = tmp_path / "instr.md"
    instr.write_text("flow's instructions", encoding="utf-8")
    return dict(ag=ag, kw=dict(store_path=scratch / "memory.db", cockpit_root=ck, coord_dir=cdir, zone=ZONE,
                               instructions_path=instr), scratch=scratch, instr=instr)


def _shadow(env, flow, **over):
    return da.write_install(env["ag"], **{**env["kw"], "shadow_flow_root": flow.root, **over})


# -------------------------------------------------------------------------------------------------- the facts, refusals

def test_a_shadow_install_without_flows_instructions_is_refused(env, flow):
    kw = {k: v for k, v in env["kw"].items() if k != "instructions_path"}
    with pytest.raises(da.InstallRefused, match="needs instructions_path"):
        da.write_install(env["ag"], **kw, shadow_flow_root=flow.root)
    assert not (env["ag"] / da.INSTALL_FILE).exists()


def test_instructions_without_a_shadow_root_is_refused(env):
    with pytest.raises(da.InstallRefused, match="shadow install fact"):
        da.write_install(env["ag"], **{**env["kw"], "store_path": env["instr"]})


def test_a_shadow_store_inside_the_live_store_is_refused_and_writes_nothing(env, flow, tmp_path):
    live = tmp_path / "home" / ".anneal-memory"
    live.mkdir(parents=True)
    with pytest.raises(da.InstallRefused, match="live store"):
        _shadow(env, flow, store_path=live / "memory.db")
    assert not (env["ag"] / da.INSTALL_FILE).exists()


def test_a_relative_shadow_store_and_a_missing_flow_root_are_refused(env, flow, tmp_path):
    with pytest.raises(da.InstallRefused, match="absolute"):
        _shadow(env, flow, store_path="rel/memory.db")
    with pytest.raises(da.InstallRefused, match="does not exist"):
        _shadow(env, flow, shadow_flow_root=tmp_path / "nope")


def test_the_shadow_facts_round_trip_and_the_store_need_not_exist_yet(env, flow):
    _shadow(env, flow)
    inst = da.read_install(env["ag"])
    assert inst.shadow_flow_root == flow.root.resolve() or inst.shadow_flow_root == flow.root
    assert inst.instructions_path == env["instr"] and not inst.store_path.exists()


def test_a_read_of_a_shadow_file_that_lost_its_instructions_or_root_refuses(env, flow):
    path = _shadow(env, flow)
    doc = json.loads(path.read_text())
    path.write_text(json.dumps({k: v for k, v in doc.items() if k != "instructions_path"}))
    with pytest.raises(da.InstallRefused, match="without instructions_path"):
        da.read_install(env["ag"])
    path.write_text(json.dumps({**doc, "shadow_flow_root": str(flow.root / "gone")}))
    with pytest.raises(da.InstallRefused, match="stale"):
        da.read_install(env["ag"])


# ------------------------------------------------------------------------------------------------------ the input hook

def _row(day=date(2026, 11, 2)):
    return {"occurrence_id": "oc-x", "agent_id": "ag-1", "kind": "scheduled", "window_key": rd.window_key_for(day),
            "launched_at": "2026-11-03T06:00:00Z"}


def test_the_hook_is_flows_committed_day_then_a_seed_and_the_episode_check(env, flow):
    _shadow(env, flow)
    assert da.install_on_input(env["ag"])(_row()) is True
    marker = json.loads((env["scratch"] / sh.MARKER).read_text())
    assert marker["run_id"] == RUN and (env["scratch"] / "memory.db").is_file()


def test_a_day_flow_did_not_commit_is_quiet_and_seeds_nothing(env, flow):
    _shadow(env, flow)
    flow.ledger([flow.end(RUN, DAY, committed=False)])
    assert da.install_on_input(env["ag"])(_row()) is False
    assert not env["scratch"].exists() or not (env["scratch"] / "memory.db").exists()


def test_a_row_without_an_agent_id_is_dead_input_not_a_quiet_day(env, flow):
    _shadow(env, flow)
    row = {k: v for k, v in _row().items() if k != "agent_id"}
    with pytest.raises(da.InputUnreadable, match="agent_id"):
        da.install_on_input(env["ag"])(row)


# ------------------------------------------------------------------------------------------------------- the run body

def _seeded(env, flow):
    _shadow(env, flow)
    assert da.install_on_input(env["ag"])(_row()) is True
    return da.read_install(env["ag"])


def test_the_consolidate_gets_flows_instructions_and_runs_under_the_night_lock(env, flow, monkeypatch):
    inst = _seeded(env, flow)
    seen = {}

    def fake(store_path, **kw):
        seen["instr"] = kw["instructions_path"]
        seen["model"] = kw["composer"]
        with pytest.raises(sh.ShadowRefused, match="busy"):          # the lock is held while the compose runs
            with sh.night_lock(sh.read_flow_day(flow.root, DAY), inst.store_path):
                pass
        return ex.ConsolidateResult("saved", 0)

    monkeypatch.setattr(ex, "consolidate_store", fake)
    run, _card = da._shadow_run(inst, DAY, "oc-x", env["scratch"] / "dream" / "oc-x", lambda: None)
    assert run().outcome == "saved"
    assert seen == {"instr": env["instr"], "model": inst.model}


def test_a_store_not_seeded_from_this_days_snapshot_is_a_precondition_never_a_compose(env, flow, monkeypatch):
    inst = _seeded(env, flow)
    (env["scratch"] / sh.MARKER).write_text(json.dumps({"run_id": "2026-11-01-other", "day": "2026-11-01"}))
    monkeypatch.setattr(ex, "consolidate_store", lambda *a, **k: pytest.fail("composed on a stale seed"))
    run, _ = da._shadow_run(inst, DAY, "oc-x", env["scratch"] / "occ", lambda: None)
    res = run()
    assert res.outcome == "precondition" and "not seeded" in res.detail
    (env["scratch"] / sh.MARKER).unlink()
    assert run().outcome == "precondition"


def test_an_unreadable_flow_day_stops_the_run_before_it_exists(env, flow):
    inst = _seeded(env, flow)
    with pytest.raises(sh.ShadowRefused):
        da._shadow_run(inst, "2026-11-09", "oc-x", env["scratch"] / "occ", lambda: None)


def test_a_saved_shadow_night_files_the_comparison_never_the_consolidated_card(env, flow, monkeypatch, tmp_path):
    inst = _seeded(env, flow)
    monkeypatch.setattr(cd, "texts_for", lambda db, occ: ("", SHADOW_TEXT))
    occ = env["scratch"] / "dream" / "oc-x"
    occ.mkdir(parents=True)
    _, saved_card = da._shadow_run(inst, DAY, "oc-x", occ, lambda: None)
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    store = CoordStore(q)
    out = da._file_run_card("saved", {}, "oc-x", occ, inst.store_path, store, DAY, False, None, da.CARD_FEED, saved_card)
    assert out == "filed" and (occ / cd.DIFF_FILE).is_file()
    again = da._file_run_card("saved", {}, "oc-x", occ, inst.store_path, store, DAY, False, None, da.CARD_FEED, saved_card)
    assert again == "duplicate"
    card = saved_card()
    assert card.authority_relevant is False and any(x.startswith("shadow:") for x in card.body["own"])


def test_a_failed_shadow_outcome_still_files_the_ordinary_failed_card(env, flow, tmp_path):
    inst = _seeded(env, flow)
    occ = env["scratch"] / "dream" / "oc-y"
    occ.mkdir(parents=True)
    q = tmp_path / "q2"
    q.mkdir(mode=0o700)
    out = da._file_run_card("refused", {"detail": "d"}, "oc-y", occ, inst.store_path, CoordStore(q), DAY, False, None,
                            da.CARD_FEED, lambda: pytest.fail("a failed night has no comparison"))
    assert out == "filed"


# ---------------------------------------------------------------------------------------------------------- the verb

def _enable(env, tmp_path, *extra):
    from levain.agents.store import open_agent_store, set_default_zone

    set_default_zone(open_agent_store(env["ag"]), ZONE)
    p = argparse.ArgumentParser()
    add_parsers(p.add_subparsers())
    args = p.parse_args(["dream", "enable", "--agent-store-dir", str(env["ag"]), "--cockpit-root", str(env["kw"]["cockpit_root"]),
                         "--coord-dir", str(env["kw"]["coord_dir"]), *extra])
    return args.func(args)


def test_the_verb_refuses_a_partial_shadow_recipe_and_writes_no_facts(env, flow, tmp_path, capsys):
    assert _enable(env, tmp_path, "--shadow-flow-root", str(flow.root)) == 1
    assert "together" in capsys.readouterr().err and not (env["ag"] / da.INSTALL_FILE).exists()


def test_the_verb_writes_the_shadow_install_with_a_read_only_instruction_copy(env, flow, tmp_path, capsys):
    rc = _enable(env, tmp_path, "--store-dir", str(env["scratch"]), "--shadow-flow-root", str(flow.root),
                 "--flow-instructions", str(env["instr"]))
    assert rc == 0, capsys.readouterr().err
    inst = da.read_install(env["ag"])
    copy = env["ag"] / "shadow_instructions.md"
    assert inst.shadow_flow_root is not None and inst.instructions_path == copy.resolve()
    assert copy.read_text() == "flow's instructions" and copy.stat().st_mode & 0o777 == 0o444
    # a second enable replaces the copy (a read-only file is not a refusal)
    env["instr"].write_text("revised")
    assert _enable(env, tmp_path, "--store-dir", str(env["scratch"]), "--shadow-flow-root", str(flow.root),
                   "--flow-instructions", str(env["instr"])) == 0
    assert copy.read_text() == "revised"


def test_the_verb_refuses_a_scratch_dir_inside_the_live_store(env, flow, tmp_path, capsys):
    live = tmp_path / "home" / ".anneal-memory" / "x"
    live.parent.mkdir(parents=True)
    rc = _enable(env, tmp_path, "--store-dir", str(live), "--shadow-flow-root", str(flow.root),
                 "--flow-instructions", str(env["instr"]))
    assert rc == 1 and "live store" in capsys.readouterr().err
    assert not (env["ag"] / da.INSTALL_FILE).exists() and not live.exists()
