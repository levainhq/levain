"""D1-3 completeness: the shadow INSTALL (the facts, the input hook, the run's lock/marker/instructions, the card).

Residue no test here holds: a real shadow night (an Opus compose on a seeded copy of flow's snapshot) and Phill
clearing its card. The compose is stubbed; what is asserted is what the install hands it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from levain.coordinator import review
from levain.coordinator.store import CoordStore
from levain.dream import agent as da
from levain.dream import card as cd
from levain.dream import executor as ex
from levain.dream import ready as rd
from levain.dream import shadow as sh
from levain.dream.cli import add_parsers

sys.path.insert(0, str(Path(__file__).parent))
from test_dream_shadow import DAY, FLOW_TEXT, RUN, SHADOW_TEXT, Flow  # noqa: E402

ZONE = "America/New_York"


@pytest.fixture
def flow(tmp_path):
    return Flow(tmp_path)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))     # the live-store guard follows $HOME
    ag, ck, cdir, scratch = (tmp_path / n for n in ("ag", "ck", "cd", "scratch"))
    for d in (ag, ck, cdir):
        d.mkdir(mode=0o700)
    instr = tmp_path / "instr.md"
    instr.write_text("flow's instructions", encoding="utf-8")
    return dict(ag=ag, kw=dict(store_path=scratch / "memory.db", cockpit_root=ck, coord_dir=cdir, zone=ZONE,
                               flow_instructions=instr), scratch=scratch, instr=instr)


def _shadow(env, flow, ag=None, **over):
    return da.write_install(ag or env["ag"], **{**env["kw"], "shadow_flow_root": flow.root, **over})


def _live_db(tmp_path):
    db = tmp_path / "live" / "memory.db"
    db.parent.mkdir(exist_ok=True)
    db.write_bytes(b"")
    return db


def _live_kw(env, tmp_path):
    return {k: v for k, v in env["kw"].items() if k != "flow_instructions"} | {"store_path": _live_db(tmp_path)}


# -------------------------------------------------------------------------------------------------- the facts, refusals

def test_a_shadow_install_without_flows_instructions_is_refused(env, flow):
    kw = {k: v for k, v in env["kw"].items() if k != "flow_instructions"}
    with pytest.raises(da.InstallRefused, match="needs flow_instructions"):
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
    assert not (env["ag"] / da.INSTALL_FILE).exists() and not (env["ag"] / da.SHADOW_INSTRUCTIONS).exists()


def test_a_scratch_dir_in_the_live_store_is_refused_even_when_its_db_links_out(env, flow, tmp_path):
    # the seed writes the marker, sidecars and lock in the DIRECTORY, so a db symlinked elsewhere does not excuse it
    x = tmp_path / "home" / ".anneal-memory" / "x"
    x.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere" / "memory.db"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(b"")
    (x / "memory.db").symlink_to(elsewhere)
    (x / sh.MARKER).write_text("{}")
    with pytest.raises(da.InstallRefused, match="live store"):
        _shadow(env, flow, store_path=x / "memory.db")


def test_a_relative_shadow_store_and_a_missing_flow_root_are_refused(env, flow, tmp_path):
    with pytest.raises(da.InstallRefused, match="absolute"):
        _shadow(env, flow, store_path="rel/memory.db")
    with pytest.raises(da.InstallRefused, match="does not exist"):
        _shadow(env, flow, shadow_flow_root=tmp_path / "nope")


def test_the_shadow_facts_round_trip_and_the_store_need_not_exist_yet(env, flow):
    _shadow(env, flow)
    inst = da.read_install(env["ag"])
    assert inst.shadow_flow_root == flow.root
    assert inst.flow_instructions == env["instr"] and inst.instructions_path == env["ag"] / da.SHADOW_INSTRUCTIONS
    assert inst.instructions_path.read_text() == "flow's instructions"
    assert inst.instructions_path.stat().st_mode & 0o777 == 0o444
    assert not inst.store_path.exists() and not env["scratch"].exists()


def test_a_read_of_a_shadow_file_that_lost_its_instructions_or_root_refuses(env, flow):
    path = _shadow(env, flow)
    doc = json.loads(path.read_text())
    path.write_text(json.dumps({k: v for k, v in doc.items() if k != "flow_instructions"}))
    with pytest.raises(da.InstallRefused, match="without flow_instructions"):
        da.read_install(env["ag"])
    path.write_text(json.dumps({**doc, "shadow_flow_root": str(flow.root / "gone")}))
    with pytest.raises(da.InstallRefused, match="stale"):
        da.read_install(env["ag"])
    path.write_text(json.dumps(doc))
    os.chmod(env["ag"] / da.SHADOW_INSTRUCTIONS, 0o600)
    (env["ag"] / da.SHADOW_INSTRUCTIONS).unlink()
    with pytest.raises(da.InstallRefused, match="instructions copy"):
        da.read_install(env["ag"])


def test_a_flow_root_without_its_ledger_is_refused(env, flow):
    (flow.root / sh.LEDGER).unlink()
    with pytest.raises(da.InstallRefused, match="ledger.jsonl"):
        _shadow(env, flow)
    assert not (env["ag"] / da.INSTALL_FILE).exists()


def test_a_scratch_dir_inside_flows_root_is_refused_at_write_and_read(env, flow):
    # F7: a seed there would write under flow's state
    with pytest.raises(da.InstallRefused, match="inside flow's dream root"):
        _shadow(env, flow, store_path=flow.root / "scratch" / "memory.db")
    path = _shadow(env, flow)
    doc = json.loads(path.read_text())
    path.write_text(json.dumps({**doc, "store_path": str(flow.root / "scratch" / "memory.db")}))
    with pytest.raises(da.InstallRefused, match="inside flow's dream root"):
        da.read_install(env["ag"])


def test_a_non_empty_scratch_dir_without_the_shadow_marker_is_refused(env, flow):
    env["scratch"].mkdir()
    (env["scratch"] / "notes.txt").write_text("mine")
    with pytest.raises(da.InstallRefused, match="no .levain-shadow marker"):
        _shadow(env, flow)
    (env["scratch"] / sh.MARKER).write_text("{}")            # a scratch dir a seed already marked is fine
    _shadow(env, flow)


# ----------------------------------------------------------------------------- F1: no silent switch between live and shadow

def test_a_live_enable_over_a_shadow_install_refuses_naming_the_switch(env, flow, tmp_path):
    path = _shadow(env, flow)
    before = path.read_bytes()
    with pytest.raises(da.InstallRefused, match="shadow install.*live install.*--replace-install"):
        da.write_install(env["ag"], **_live_kw(env, tmp_path))
    assert path.read_bytes() == before


def test_a_shadow_enable_over_a_live_install_refuses_naming_the_switch(env, flow, tmp_path):
    path = da.write_install(env["ag"], **_live_kw(env, tmp_path))
    before = path.read_bytes()
    with pytest.raises(da.InstallRefused, match="live install.*shadow install.*--replace-install"):
        _shadow(env, flow)
    assert path.read_bytes() == before and not (env["ag"] / da.SHADOW_INSTRUCTIONS).exists()


def test_replace_install_switches_both_ways_and_a_live_write_keeps_no_shadow_fact(env, flow, tmp_path):
    _shadow(env, flow, model="claude-x")
    da.write_install(env["ag"], **_live_kw(env, tmp_path), replace_install=True)
    doc = json.loads((env["ag"] / da.INSTALL_FILE).read_text())
    assert not set(da.SHADOW_FACTS) & set(doc) and doc["model"] == "claude-x"     # mode-agnostic facts still merge
    inst = da.read_install(env["ag"])
    assert inst.shadow_flow_root is None and inst.instructions_path is None and da.install_kind(env["ag"]) == "live"
    _shadow(env, flow, replace_install=True)
    assert da.install_kind(env["ag"]) == "shadow" and da.read_install(env["ag"]).store_path == env["kw"]["store_path"]


def test_a_re_enable_of_the_same_kind_needs_no_flag(env, flow, tmp_path):
    _shadow(env, flow)
    _shadow(env, flow)
    da.write_install(env["ag"], **_live_kw(env, tmp_path), replace_install=True)
    da.write_install(env["ag"], **_live_kw(env, tmp_path))


def test_check_only_makes_every_check_and_writes_nothing(env, flow):
    assert da.write_install(env["ag"], **env["kw"], shadow_flow_root=flow.root, check_only=True) == \
        env["ag"] / da.INSTALL_FILE
    assert list(env["ag"].iterdir()) == []
    with pytest.raises(da.InstallRefused):
        da.write_install(env["ag"], **{**env["kw"], "flow_instructions": env["ag"] / "nope"},
                         shadow_flow_root=flow.root, check_only=True)


# ------------------------------------------------------------------------- F3: the live-store refusal holds at read and fire

def _hand_written_live_store(env, flow, tmp_path):
    path = _shadow(env, flow)
    live = tmp_path / "home" / ".anneal-memory"
    live.mkdir(parents=True)
    doc = json.loads(path.read_text())
    path.write_text(json.dumps({**doc, "store_path": str(live / "memory.db")}))
    return live


def test_a_hand_written_shadow_store_in_the_live_store_is_refused_at_read_hook_and_fire(env, flow, tmp_path):
    live = _hand_written_live_store(env, flow, tmp_path)
    with pytest.raises(da.InstallRefused, match="live store"):
        da.read_install(env["ag"])
    with pytest.raises(da.InstallRefused, match="live store"):
        da.install_on_input(env["ag"])(_row())
    with pytest.raises(da.InstallRefused, match="live store"):
        da.fire_factory(SimpleNamespace(directory=env["ag"]), "oc-x", 0)
    assert list(live.iterdir()) == []


def test_the_fire_refuses_a_live_store_install_however_it_was_built(env, flow, tmp_path):
    # _shadow_run's own check, for a DreamInstall that never went through read_install
    _shadow(env, flow)
    live = tmp_path / "home" / ".Anneal-Memory" / "x"           # a case variant, never created
    inst = da.read_install(env["ag"])
    bad = da.DreamInstall(live / "memory.db", inst.cockpit_root, inst.coord_dir, ZONE,
                          shadow_flow_root=inst.shadow_flow_root, instructions_path=inst.instructions_path,
                          flow_instructions=inst.flow_instructions)
    with pytest.raises(da.InstallRefused, match="live store"):
        da._shadow_run(bad, DAY, "oc-x", tmp_path / "occ", lambda: None)
    assert not (tmp_path / "home").exists()


# ------------------------------------------------------------------------------------------------------ the input hook

def _register(ag, binding=None, *, revise=True):
    """Register the dream in the agent store at ``ag``, bound to its facts file as it stands (or ``binding``)."""
    from levain.agents.store import open_agent_store, set_default_zone

    store = open_agent_store(ag)
    set_default_zone(store, ZONE)
    return da.register_dream(store, revise=revise, binding=da.current_binding(ag) if binding is None else binding)


def _row(env=None, day=date(2026, 11, 2), spec_hash=None):
    """An occurrence row; given ``env``, it runs under the dream spec bound to the facts in env's agent store."""
    row = {"occurrence_id": "oc-x", "agent_id": "ag-1", "kind": "scheduled", "window_key": rd.window_key_for(day),
           "launched_at": "2026-11-03T06:00:00Z"}
    if spec_hash is not None:
        row["spec_hash"] = spec_hash
    elif env is not None:
        row["spec_hash"] = _register(env["ag"]).spec_hash
    return row


def test_the_hook_is_flows_committed_day_then_a_seed_and_the_episode_check(env, flow):
    _shadow(env, flow)
    assert da.install_on_input(env["ag"])(_row(env)) is True
    marker = json.loads((env["scratch"] / sh.MARKER).read_text())
    assert marker["run_id"] == RUN and (env["scratch"] / "memory.db").is_file()


def test_a_day_flow_did_not_commit_is_quiet_and_seeds_nothing(env, flow):
    _shadow(env, flow)
    flow.ledger([flow.end(RUN, DAY, committed=False)])
    assert da.install_on_input(env["ag"])(_row(env)) is False
    assert not env["scratch"].exists() or not (env["scratch"] / "memory.db").exists()


def test_a_row_without_an_agent_id_is_dead_input_not_a_quiet_day(env, flow):
    _shadow(env, flow)
    row = {k: v for k, v in _row(env).items() if k != "agent_id"}
    with pytest.raises(da.InputUnreadable, match="agent_id"):
        da.install_on_input(env["ag"])(row)


def test_a_seed_whose_instructions_cannot_be_copied_is_dead_input(env, flow):
    _shadow(env, flow)
    os.chmod(env["instr"], 0)                         # still a file (the read of the facts passes), no longer readable
    try:
        with pytest.raises(da.InputUnreadable, match="after the seed"):
            da.install_on_input(env["ag"])(_row(env))
    finally:
        os.chmod(env["instr"], 0o600)
    env["instr"].unlink()                             # gone altogether: the facts are stale, also dead input
    with pytest.raises(da.InstallRefused, match="stale: flow_instructions"):
        da.install_on_input(env["ag"])(_row(env))


# ------------------------------------------------------------------------------------------------------- the run body

def _seeded(env, flow):
    _shadow(env, flow)
    assert da.install_on_input(env["ag"])(_row(env)) is True
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
    occ = env["scratch"] / "dream" / "oc-x"
    run, _card = da._shadow_run(inst, DAY, "oc-x", occ, lambda: None)
    assert run().outcome == "saved"
    # the compose reads the occurrence's own copy, made under the lock from the agent store's copy
    assert seen == {"instr": occ / da.SHADOW_INSTRUCTIONS, "model": inst.model}
    assert (occ / da.SHADOW_INSTRUCTIONS).read_text() == "flow's instructions"


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


def _night(env, flow, inst, occ, monkeypatch):
    """One shadow night through _shadow_run with the compose stubbed; returns the comparison card."""
    monkeypatch.setattr(ex, "consolidate_store", lambda *a, **k: ex.ConsolidateResult("saved", 0))
    monkeypatch.setattr(cd, "texts_for", lambda db, occ_dir: ("", SHADOW_TEXT))
    occ.mkdir(parents=True, exist_ok=True)
    run, saved_card = da._shadow_run(inst, DAY, occ.name, occ, lambda: None)
    assert run().outcome == "saved"
    return saved_card()


def test_a_saved_shadow_night_files_the_comparison_never_the_consolidated_card(env, flow, monkeypatch, tmp_path):
    inst = _seeded(env, flow)
    occ = env["scratch"] / "dream" / "oc-x"
    card = _night(env, flow, inst, occ, monkeypatch)
    _, saved_card = da._shadow_run(inst, DAY, "oc-x", occ, lambda: None)
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    store = CoordStore(q)
    out = da._file_run_card("saved", {}, "oc-x", occ, inst.store_path, store, DAY, False, None, da.CARD_FEED, saved_card)
    assert out == "filed"
    # the diff beside the occurrence is flow's text against the shadow's, never the prior-vs-saved diff of a live card
    assert (occ / cd.DIFF_FILE).read_text() == sh.diff_outputs(FLOW_TEXT, SHADOW_TEXT).text
    again = da._file_run_card("saved", {}, "oc-x", occ, inst.store_path, store, DAY, False, None, da.CARD_FEED, saved_card)
    assert again == "duplicate"
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


def test_each_seed_re_copies_flows_instructions_and_the_card_carries_the_copys_sha(env, flow, monkeypatch):
    # F4: flow's instructions change between two nights; the second card measures the new ones
    inst = _seeded(env, flow)
    first = _night(env, flow, inst, env["scratch"] / "dream" / "oc-1", monkeypatch)
    old = hashlib.sha256(b"flow's instructions").hexdigest()[:16]
    assert any(x.startswith("shadow: instructions ") and old in x for x in first.body["own"])
    env["instr"].write_text("flow's instructions, revised", encoding="utf-8")
    assert da.install_on_input(env["ag"])(_row(env)) is True
    assert inst.instructions_path.read_text() == "flow's instructions, revised"
    assert inst.instructions_path.stat().st_mode & 0o777 == 0o444
    second = _night(env, flow, inst, env["scratch"] / "dream" / "oc-2", monkeypatch)
    new = hashlib.sha256(b"flow's instructions, revised").hexdigest()[:16]
    lines = [x for x in second.body["own"] if x.startswith("shadow: instructions ")]
    assert len(lines) == 1 and new in lines[0] and old not in lines[0]


# ------------------------------------------------------------------------- F5: the shadow fire, end to end through run_body

from tests.test_agents_supervise_fire import _fire, _supervisor, Ex, rig  # noqa: E402,F401


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
def test_a_shadow_fire_runs_the_shadow_body_and_files_only_the_comparison(rig, env, flow, monkeypatch):
    sup, journal = _supervisor(rig)
    oid = _fire(rig, "shadow-e2e", journal)
    ag = rig.store.directory
    _shadow(env, flow, ag=ag)
    monkeypatch.setattr(rd, "occurrence_dream_day", lambda row, zone: date.fromisoformat(DAY))
    monkeypatch.setattr(ex, "DreamExecutor", lambda **kw: Ex())
    # the rig's link signs the rig's own spec, which binds none of these shadow facts: the hook and the fire refuse
    with pytest.raises(da.InstallRefused, match="not the ones the occurrence's spec binds"):
        da.install_on_input(ag)(rig.row(oid))
    with pytest.raises(da.InstallRefused, match="not the ones the occurrence's spec binds"):
        da.fire_factory(rig.store, oid, 0)
    assert not env["scratch"].exists()                                     # nothing seeded under an unsigned spec
    # stand the rig's spec in for the shadow spec bound to these facts (the binding itself is tested on its own)
    want = da.SHADOW_SOURCE + da.current_binding(ag)
    monkeypatch.setattr(da, "_spec_source", lambda agent_dir, spec_hash: want)
    assert da.install_on_input(ag)(rig.row(oid)) is True                   # the seed
    seen = {}

    def compose(store_path, **kw):
        seen.update(kw, store_path=store_path)
        return ex.ConsolidateResult("saved", 0)

    monkeypatch.setattr(ex, "consolidate_store", compose)
    monkeypatch.setattr(cd, "texts_for", lambda db, occ_dir: ("", SHADOW_TEXT))
    kw = da.fire_factory(rig.store, oid, 0)
    kw.pop("sup")                                                          # the rig's own supervisor holds the link
    rep = da.run_body(sup, oid, 0, **kw)
    assert rep.outcome == "saved" and rep.card == "filed" and rep.state == "done"
    assert rig.row(oid)["state"] == "done"
    assert seen["store_path"] == env["kw"]["store_path"]
    assert seen["instructions_path"] == kw["occ_dir"] / da.SHADOW_INSTRUCTIONS
    items = review.list_items(kw["review_store"], feed=da.CARD_FEED)
    assert len(items) == 1                                                 # no consolidated card beside it
    own = items[0].body["own"]
    assert any(x.startswith("shadow: full diff") for x in own) and any(x.startswith("shadow: instructions") for x in own)
    assert (kw["occ_dir"] / cd.DIFF_FILE).read_text() == sh.diff_outputs(FLOW_TEXT, SHADOW_TEXT).text


# ---------------------------------------------------------------------------------------------------------- the verb

def _enable(env, tmp_path, *extra):
    from levain.agents.store import open_agent_store, set_default_zone

    set_default_zone(open_agent_store(env["ag"]), ZONE)
    p = argparse.ArgumentParser()
    add_parsers(p.add_subparsers())
    args = p.parse_args(["dream", "enable", "--agent-store-dir", str(env["ag"]), "--cockpit-root", str(env["kw"]["cockpit_root"]),
                         "--coord-dir", str(env["kw"]["coord_dir"]), *extra])
    return args.func(args)


def _registered(env):
    from levain.agents import spec as _spec
    from levain.agents.store import open_agent_store

    return {a["name"] for a in _spec.list_agents(open_agent_store(env["ag"]))}


def _shadow_args(env, flow, scratch=None, instr=None):
    return ("--store-dir", str(scratch or env["scratch"]), "--shadow-flow-root", str(flow.root),
            "--flow-instructions", str(instr or env["instr"]))


def _nothing_left(env, scratch=None):
    assert "dream" not in _registered(env)
    assert not (env["ag"] / da.INSTALL_FILE).exists() and not (env["ag"] / da.SHADOW_INSTRUCTIONS).exists()
    assert not (scratch or env["scratch"]).exists()


def test_the_verb_refuses_a_partial_shadow_recipe_and_leaves_nothing(env, flow, tmp_path, capsys):
    assert _enable(env, tmp_path, "--shadow-flow-root", str(flow.root)) == 1
    assert "together" in capsys.readouterr().err
    _nothing_left(env)


def test_the_verb_writes_the_shadow_install_with_a_read_only_instruction_copy(env, flow, tmp_path, capsys):
    rc = _enable(env, tmp_path, *_shadow_args(env, flow))
    assert rc == 0, capsys.readouterr().err
    inst = da.read_install(env["ag"])
    copy = env["ag"] / da.SHADOW_INSTRUCTIONS
    assert inst.shadow_flow_root is not None and inst.instructions_path == copy
    assert copy.read_text() == "flow's instructions" and copy.stat().st_mode & 0o777 == 0o444
    # a second enable replaces the copy (a read-only file is not a refusal)
    env["instr"].write_text("revised")
    assert _enable(env, tmp_path, *_shadow_args(env, flow)) == 0
    assert copy.read_text() == "revised"


@pytest.mark.parametrize("variant", ["plain", "case", "symlink", "trailing-slash"])
def test_the_verb_refuses_a_scratch_dir_inside_the_live_store_and_leaves_nothing(env, flow, tmp_path, capsys, variant):
    home = tmp_path / "home"
    live = home / ".anneal-memory"
    live.mkdir(parents=True)
    scratch = {"plain": live / "x", "case": home / ".Anneal-Memory" / "x", "symlink": tmp_path / "ln" / "x",
               "trailing-slash": Path(str(live / "x") + "/")}[variant]
    if variant == "symlink":
        (tmp_path / "ln").symlink_to(live, target_is_directory=True)
    rc = _enable(env, tmp_path, *_shadow_args(env, flow, scratch=scratch))
    assert rc == 1 and "live store" in capsys.readouterr().err
    _nothing_left(env, scratch=live / "x")
    assert list(live.iterdir()) == []


@pytest.mark.parametrize("case", ["no-ledger", "no-instructions", "unmarked-scratch", "scratch-in-flow-root"])
def test_every_shadow_refusal_leaves_nothing_behind(env, flow, tmp_path, capsys, case):
    scratch, instr = env["scratch"], env["instr"]
    if case == "no-ledger":
        (flow.root / sh.LEDGER).unlink()
    elif case == "no-instructions":
        instr = tmp_path / "missing.md"
    elif case == "unmarked-scratch":
        scratch.mkdir()
        (scratch / "mine.txt").write_text("x")
    else:
        scratch = flow.root / "scratch"
    before = sorted(p.name for p in flow.root.iterdir())
    rc = _enable(env, tmp_path, *_shadow_args(env, flow, scratch=scratch, instr=instr))
    assert rc == 1 and "nothing was registered or written" in capsys.readouterr().err
    assert "dream" not in _registered(env)
    assert not (env["ag"] / da.INSTALL_FILE).exists() and not (env["ag"] / da.SHADOW_INSTRUCTIONS).exists()
    if case == "unmarked-scratch":
        assert [p.name for p in scratch.iterdir()] == ["mine.txt"]
    else:
        assert not scratch.exists()
    assert sorted(p.name for p in flow.root.iterdir()) == before


def test_the_verb_refuses_a_silent_switch_and_names_a_replaced_one(env, flow, tmp_path, capsys):
    live = _live_db(tmp_path)
    assert _enable(env, tmp_path, "--store-dir", str(live.parent)) == 0
    capsys.readouterr()
    before = (env["ag"] / da.INSTALL_FILE).read_bytes()
    assert _enable(env, tmp_path, *_shadow_args(env, flow)) == 1
    assert "--replace-install" in capsys.readouterr().err
    assert (env["ag"] / da.INSTALL_FILE).read_bytes() == before and not (env["ag"] / da.SHADOW_INSTRUCTIONS).exists()
    # the kind is bound into the spec too: the switch is a new version, so --revise as well, else nothing is written
    assert _enable(env, tmp_path, *_shadow_args(env, flow), "--replace-install") == 1
    assert "new signed link" in capsys.readouterr().err
    assert (env["ag"] / da.INSTALL_FILE).read_bytes() == before and not (env["ag"] / da.SHADOW_INSTRUCTIONS).exists()
    assert _enable(env, tmp_path, *_shadow_args(env, flow), "--replace-install", "--revise") == 0
    assert "replaced the live install with a shadow install" in capsys.readouterr().out
    assert _enable(env, tmp_path, "--store-dir", str(live.parent)) == 1
    assert "--replace-install" in capsys.readouterr().err
    # back to live is the content of version 1, a revert the spec store refuses: the operator retires and registers
    assert _enable(env, tmp_path, "--store-dir", str(live.parent), "--replace-install", "--revise") == 1
    assert "revert" in capsys.readouterr().err
    assert da.install_kind(env["ag"]) == "shadow"


def test_the_shadow_signing_card_says_what_it_reads_where_it_writes_and_what_it_costs(env, flow, tmp_path, capsys):
    # F2: the words come from the install's facts and the executor's R, never a number typed into prose
    live = _live_db(tmp_path)
    assert _enable(env, tmp_path, "--store-dir", str(live.parent), "--json") == 0
    live_doc = json.loads(capsys.readouterr().out)
    assert "always-loaded memory text" in live_doc["signing_card"]
    assert _enable(env, tmp_path, *_shadow_args(env, flow), "--replace-install", "--revise", "--json") == 0
    doc = json.loads(capsys.readouterr().out)
    card = doc["signing_card"]
    r = ex.PER_RUN_LIMIT_USD_MICROS / 1_000_000
    inst = da.read_install(env["ag"])
    # every place it writes, named from the facts: scratch, the card's queue, its own agent store, the instructions copy
    for where in (flow.root, inst.store_path.parent, inst.coord_dir, env["ag"], inst.flow_instructions):
        assert str(where) in card
    assert "SHADOW" in card and "Read, Glob, Grep" in card and "bound into the spec" in card
    assert "checked against the hash that snapshot's own manifest records" in card and "read as is" in card
    assert inst.model in card and f"${r:.2f} a run" in card and f"${2 * r:.2f} a night" in card
    assert "always-loaded memory text" not in card and "rollback" not in card
    assert doc["install_kind"] == "shadow"
    # the signed object differs from the live one: a live link never covers the shadow, nor the reverse
    assert doc["spec_hash"] != live_doc["spec_hash"]


# ---------------------------------------------------------- L3 r1 #1: the signed spec binds the shadow install's facts

def _spec_hash_of(env):
    from levain.agents import spec as _spec
    from levain.agents.store import open_agent_store

    return {a["name"]: a for a in _spec.list_agents(open_agent_store(env["ag"]))}["dream"]


def test_a_same_kind_re_enable_with_other_facts_needs_revise_and_a_new_link(env, flow, tmp_path, capsys):
    # reproduced on d300015b: the re-enable below exited 0 with the old spec_hash, the facts moved under the old link
    assert _enable(env, tmp_path, *_shadow_args(env, flow), "--json") == 0
    first = json.loads(capsys.readouterr().out)["spec_hash"]
    before = (env["ag"] / da.INSTALL_FILE).read_bytes()
    other = tmp_path / "scratch2"
    assert _enable(env, tmp_path, *_shadow_args(env, flow, scratch=other)) == 1
    assert "new signed link" in capsys.readouterr().err
    assert (env["ag"] / da.INSTALL_FILE).read_bytes() == before                 # nothing moved under the old link
    assert _enable(env, tmp_path, *_shadow_args(env, flow, scratch=other), "--revise", "--json") == 0
    second = json.loads(capsys.readouterr().out)["spec_hash"]
    assert second != first and da.read_install(env["ag"]).store_path == other / "memory.db"
    # an occurrence of the old spec (the one the old link signs) no longer matches the facts: hook and fire refuse
    with pytest.raises(da.InstallRefused, match="not the ones the occurrence's spec binds"):
        da.install_on_input(env["ag"])(_row(spec_hash=first))
    assert not other.exists()
    assert da.install_on_input(env["ag"])(_row(spec_hash=second)) is True


def test_every_bound_fact_moves_the_spec(env, flow, tmp_path):
    base = dict(shadow_flow_root=flow.root, store_path=env["kw"]["store_path"], coord_dir=env["kw"]["coord_dir"],
                cockpit_root=env["kw"]["cockpit_root"], flow_instructions=env["instr"], model=None, claude_bin=None)
    b0 = da.shadow_binding(**base)
    for key, value in (("shadow_flow_root", tmp_path / "f2"), ("store_path", tmp_path / "s2" / "memory.db"),
                       ("coord_dir", tmp_path / "c2"), ("cockpit_root", tmp_path / "k2"),
                       ("flow_instructions", tmp_path / "i2.md"), ("model", "claude-other"), ("claude_bin", "/x/claude")):
        assert da.shadow_binding(**{**base, key: value}) != b0, key
    assert da.dream_spec("r" * 64)["input"]["source"] == da.LIVE_SOURCE
    assert da.dream_spec("r" * 64, b0)["input"]["source"] == da.SHADOW_SOURCE + b0


def test_a_hand_edited_fact_is_refused_at_the_hook_and_the_fire(env, flow, tmp_path):
    path = _shadow(env, flow)
    row = _row(env)
    doc = json.loads(path.read_text())
    path.write_text(json.dumps({**doc, "model": "claude-other"}))       # changed after the spec was registered
    with pytest.raises(da.InstallRefused, match="not the ones the occurrence's spec binds"):
        da.install_on_input(env["ag"])(row)
    assert not env["scratch"].exists()


def test_live_facts_never_run_under_a_shadow_spec(env, flow, tmp_path):
    _shadow(env, flow)
    shadow_hash = _register(env["ag"]).spec_hash
    da.write_install(env["ag"], **_live_kw(env, tmp_path), replace_install=True)   # facts now live, spec still shadow
    with pytest.raises(da.InstallRefused, match="link signed for a shadow never runs a live dream"):
        da.check_bound(env["ag"], {"spec_hash": shadow_hash}, da.read_install(env["ag"]))
    with pytest.raises(da.InstallRefused, match="no spec_hash"):
        da.check_bound(env["ag"], {}, da.read_install(env["ag"]))


# ---------------------------------------------------- L3 r1 #2: every write destination is containment-checked

@pytest.mark.parametrize("where", ["coord-live", "coord-flow", "agent-live", "agent-flow"])
def test_a_card_queue_or_agent_store_inside_a_protected_root_is_refused(env, flow, tmp_path, where):
    # reproduced on d300015b for coord-live: the facts were written (the card queue and agent store were never judged)
    live = tmp_path / "home" / ".anneal-memory"
    target = {"coord-live": live / "q", "coord-flow": flow.root / "q", "agent-live": live / "ag",
              "agent-flow": flow.root / "ag"}[where]
    target.mkdir(parents=True)
    kw = dict(env["kw"])
    ag = env["ag"]
    if where.startswith("coord"):
        kw["coord_dir"] = target
    else:
        ag = target
    with pytest.raises(da.InstallRefused, match="live store|flow's dream root"):
        da.write_install(ag, **kw, shadow_flow_root=flow.root)
    assert not (ag / da.INSTALL_FILE).exists() and not (ag / da.SHADOW_INSTRUCTIONS).exists()


def test_a_hand_written_card_queue_in_the_live_store_is_refused_at_read_and_fire(env, flow, tmp_path):
    path = _shadow(env, flow)
    q = tmp_path / "home" / ".anneal-memory" / "q"
    q.mkdir(parents=True)
    path.write_text(json.dumps({**json.loads(path.read_text()), "coord_dir": str(q)}))
    with pytest.raises(da.InstallRefused, match="coord_dir.*live store"):
        da.read_install(env["ag"])
    with pytest.raises(da.InstallRefused, match="coord_dir.*live store"):
        da.fire_factory(SimpleNamespace(directory=env["ag"]), "oc-x", 0)
    assert list(q.iterdir()) == []


# ------------------------------------------- L3 r1 #3: a scratch dir made after the check is judged again under the lock

def test_a_scratch_dir_swapped_for_a_symlink_after_the_check_is_refused_under_the_lock(env, flow, tmp_path,
                                                                                       monkeypatch):
    # reproduced on d300015b: the hook seeded through the symlink, writing files into the live store directory
    _shadow(env, flow)                                     # scratch absent: the install's checks pass
    row = _row(env)
    live = tmp_path / "home" / ".anneal-memory" / "x"
    live.mkdir(parents=True)
    real = sh.read_flow_day

    def swap_after_the_checks(root, day):                  # runs after read_install, before the night lock
        if not env["scratch"].exists():
            env["scratch"].symlink_to(live, target_is_directory=True)
        return real(root, day)

    monkeypatch.setattr(sh, "read_flow_day", swap_after_the_checks)
    with pytest.raises(da.InputUnreadable, match="live store"):
        da.install_on_input(env["ag"])(row)
    assert list(live.iterdir()) == []


def test_a_swap_between_the_mkdir_and_the_lock_is_caught_by_the_check_under_the_lock(env, flow, tmp_path, monkeypatch):
    live = tmp_path / "home" / ".anneal-memory" / "x"
    live.mkdir(parents=True)
    real = sh._lock

    def swap_then_lock(store_dir, name="wrap.lock"):
        if store_dir == env["scratch"] and not env["scratch"].is_symlink():
            env["scratch"].rmdir()                         # the night lock just made it; replace it with a symlink
            env["scratch"].symlink_to(live, target_is_directory=True)
        return real(store_dir, name)

    monkeypatch.setattr(sh, "_lock", swap_then_lock)
    with pytest.raises(sh.ShadowRefused, match="live store"):
        with sh.night_lock(sh.read_flow_day(flow.root, DAY), env["scratch"] / "memory.db"):
            pytest.fail("the night ran in the live store")
    assert [p.name for p in live.iterdir()] == [sh.NIGHT_LOCK]    # the lock file only; nothing the night writes


def test_a_scratch_dir_holding_a_symlink_is_refused_under_the_lock(env, flow, tmp_path):
    _shadow(env, flow)
    row = _row(env)
    env["scratch"].mkdir()
    elsewhere = tmp_path / "elsewhere.txt"                 # a file the seed's marker write would follow and overwrite
    elsewhere.write_text("not the shadow's")
    (env["scratch"] / sh.MARKER).symlink_to(elsewhere)
    with pytest.raises(da.InputUnreadable, match="symlink"):
        da.install_on_input(env["ag"])(row)
    assert elsewhere.read_text() == "not the shadow's"


# -------------------------------------------- L3 r1 #4: the sha on the card is the sha of the bytes the compose was given

def test_the_card_sha_is_the_composes_bytes_even_after_a_re_enable_and_a_replay(env, flow, monkeypatch):
    # reproduced on d300015b: the card read the sha from a dict filled only by this process's compose, so a card built
    # by a fresh closure (a replay) said "not recorded". The replacement of the agent store's copy mid-compose stands
    # for an enable (write_install takes no night lock): the card still names the bytes the compose was given.
    inst = _seeded(env, flow)
    occ = env["scratch"] / "dream" / "oc-r"
    composed_with = {}

    def compose(store_path, **kw):
        composed_with["bytes"] = Path(kw["instructions_path"]).read_bytes()
        # an enable replaces the agent store's copy while the compose runs (write_install takes no night lock)
        os.chmod(inst.instructions_path, 0o600)
        inst.instructions_path.write_text("replaced mid-compose", encoding="utf-8")
        return ex.ConsolidateResult("saved", 0)

    monkeypatch.setattr(ex, "consolidate_store", compose)
    monkeypatch.setattr(cd, "texts_for", lambda db, occ_dir: ("", SHADOW_TEXT))
    occ.mkdir(parents=True)
    run, saved_card = da._shadow_run(inst, DAY, "oc-r", occ, lambda: None)
    assert run().outcome == "saved"
    want = hashlib.sha256(composed_with["bytes"]).hexdigest()[:16]
    assert composed_with["bytes"] == b"flow's instructions"
    lines = [x for x in saved_card().body["own"] if x.startswith("shadow: instructions ")]
    assert len(lines) == 1 and want in lines[0]
    # a replay: a fresh closure (a new process after a crash) that never ran the compose builds the same card line
    _, replayed = da._shadow_run(inst, DAY, "oc-r", occ, lambda: None)
    again = [x for x in replayed().body["own"] if x.startswith("shadow: instructions ")]
    assert again == lines


# -------------------------------------------------- L3 r1 #5: check_only judges the instructions copy's destination

def test_a_directory_where_the_instructions_copy_goes_is_refused_before_anything_is_registered(env, flow, tmp_path,
                                                                                            capsys):
    # reproduced on d300015b: check_only passed, the dream registered, then the copy failed ("registered, but ...")
    (env["ag"] / da.SHADOW_INSTRUCTIONS).mkdir()
    with pytest.raises(da.InstallRefused, match="not a regular file"):
        _shadow(env, flow, check_only=True)
    assert _enable(env, tmp_path, *_shadow_args(env, flow)) == 1
    assert "nothing was registered or written" in capsys.readouterr().err
    assert "dream" not in _registered(env) and not (env["ag"] / da.INSTALL_FILE).exists()


# ----------------------------------------------- deepseek MED (r1 #6): the card's text after the lock is released

def test_a_re_seed_between_the_save_and_the_card_cannot_change_the_cards_text(env, flow, tmp_path):
    # the card reads the occurrence's own composed file, pinned by the sha the manifest recorded at the save; a re-seed
    # replaces the store's text, not that file, and a store text that moved fails the pin (the card refuses, never lies)
    from levain.dream import snapshot as sn

    inst = _seeded(env, flow)
    occ = env["scratch"] / "dream" / "oc-s"
    (occ / sn.SNAP_DIR).mkdir(parents=True)
    (occ / "composed.continuity.md").write_text(SHADOW_TEXT, encoding="utf-8")
    sha = hashlib.sha256(SHADOW_TEXT.encode()).hexdigest()
    (occ / sn.SNAP_DIR / sn.MANIFEST).write_text(json.dumps({"files": {}, "post_files": {"continuity": sha}}))
    assert da.install_on_input(env["ag"])(_row(env)) is True                  # the next slot re-seeds the scratch store
    assert cd.texts_for(inst.store_path, occ)[1] == SHADOW_TEXT
    (occ / "composed.continuity.md").unlink()
    with pytest.raises(cd.CardError):
        cd.texts_for(inst.store_path, occ)
