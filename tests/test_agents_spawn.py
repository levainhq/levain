"""The spawned half (P3): a role made into one worker launch. Registered and inline roles, the per-key narrowing of the
thread's envelope by the role's ceiling (per LAUNCH, stored on it, never written to the thread), the ephemeral spec,
the spawn caps and the lineage they read, the worker brief, ``levain desk spawn``, the end of a done worker, and the
inventory of who reads an envelope as authority. A fake ``claude`` replays the measured outputs (the launch tests'
own); no test here ever spawns the real one."""
from __future__ import annotations

import ast
import json
import sqlite3
import time
from pathlib import Path

import pytest

from levain.agents import instantiate as inst
from levain.agents import role as role_mod
from levain.agents.store import open_agent_store
from levain.desk import door
from levain.desk import envelope as env
from levain.desk import first_reader as fr
from levain.desk import lease
from levain.desk.envelopes import narrow_scope
from levain.desk.store import CoordError, CoordStore, Part
# the launch tests' fixtures: a fake claude on PATH, a scratch queue directory, a desk workspace, a live hook
from tests.test_desk_launch import fake, live_hook, qdir, workdir, _levain_home  # noqa: F401
from tests.test_desk_launch import OTHER, _main, cl, rows
# the envelope tests' fixtures: real keys, a real authority log, a scratch git repo, an EnvelopeStore
from tests.test_desk_envelope import ctx, keys, repo, snapshot  # noqa: F401
from levain.desk.legacy import ROLE_DESK

REPO = "/work/repo"


def role(name="scribe", *, ceiling=None, template="Write {topic} up.", slots=None, executor="session", **over):
    r = {"name": name, "brief": {"template": template, "slots": {"topic": "str"} if slots is None else slots},
         "ceiling": {} if ceiling is None else ceiling, "model": "haiku", "harness": "claude", "executor": executor}
    r.update(over)
    return r


def effect_land(repo=REPO, onto="main"):
    return {"repo": repo, "onto": onto, "from": "source", "onto_from": "drop", "criteria": []}


def effect_push(repo=REPO):
    return {"repo": repo, "remote": "origin", "url": "ssh://h/r.git", "branch": "b", "criteria": []}


def scope(**over):
    s = {"where": [{"repo": REPO, "paths": ["src/", "docs/"]}], "lands": [effect_land()], "pushes": [effect_push()],
         "publishes": []}
    s.update(over)
    return s


# --- per-key narrowing ---------------------------------------------------------------------------------------------
def test_where_is_cut_to_the_paths_both_cover():
    got = narrow_scope(scope(), {"where": [{"repo": REPO, "paths": ["src/lib/", "docs/"]}]})
    assert got["where"] == [{"repo": REPO, "paths": ["src/lib/", "docs/"]}]


def test_a_repo_the_ceiling_does_not_name_leaves_where():
    got = narrow_scope(scope(), {"where": [{"repo": "/work/other", "paths": ["."]}]})
    assert got["where"] == []


def test_a_ceiling_that_names_no_where_leaves_no_where():
    # absence grants nothing, on where as on the effects: a minimal role is minimal on place too
    assert narrow_scope(scope(), {})["where"] == []
    assert narrow_scope(scope(), {"lands": [REPO]})["where"] == []


def test_an_effect_list_the_ceiling_never_names_is_emptied():
    got = narrow_scope(scope(), {"where": [{"repo": REPO, "paths": ["src/"]}]})
    assert got["lands"] == [] and got["pushes"] == [] and got["publishes"] == []


def test_an_effect_the_ceiling_names_for_another_repo_is_dropped():
    got = narrow_scope(scope(), {"lands": ["/work/other"], "pushes": [REPO]})
    assert got["lands"] == [] and got["pushes"] == [effect_push()]


def test_a_ceiling_key_outside_the_vocabulary_is_refused():
    with pytest.raises(CoordError, match="not one of"):
        narrow_scope(scope(), {"budget": 1})


def test_an_inline_role_cannot_widen_the_envelope():
    wide = {"where": [{"repo": REPO, "paths": ["."]}, {"repo": "/work/secret", "paths": ["."]}],
            "lands": [REPO, "/work/secret"], "pushes": [REPO], "publishes": ["pkg"]}
    got = narrow_scope(scope(), wide)
    assert got["where"] == [{"repo": REPO, "paths": ["src/", "docs/"]}]
    assert got["lands"] == [effect_land()] and got["publishes"] == []
    assert {w["repo"] for w in got["where"]} <= {REPO}


def test_the_not_wider_check_refuses_a_widened_result():
    from levain.desk.envelopes import _assert_not_wider

    with pytest.raises(CoordError, match="beyond the envelope"):
        _assert_not_wider(scope(), {**scope(), "where": [{"repo": "/work/secret", "paths": ["."]}]})
    with pytest.raises(CoordError, match="beyond the envelope"):
        _assert_not_wider(scope(), {**scope(), "where": [{"repo": REPO, "paths": ["etc/"]}]})
    with pytest.raises(CoordError, match="effect the envelope lacks"):
        _assert_not_wider(scope(), {**scope(), "lands": [effect_land(onto="release")]})


# --- roles, slots, the record ---------------------------------------------------------------------------------------
@pytest.fixture
def registry(tmp_path):
    return open_agent_store(tmp_path / "autonomic")


def test_a_registered_role_resolves_by_its_spawn_name(registry):
    role_mod.add_role(registry, role())
    got, digest, inline = inst.resolve_role(registry, ref="@spawn/scribe")
    assert got["name"] == "scribe" and digest == role_mod.role_hash(got) and inline is False


def test_an_inline_role_resolves_against_the_registry(registry):
    got, digest, inline = inst.resolve_role(registry, inline=role("one-off"))
    assert inline is True and digest == role_mod.role_hash(got)


def test_a_spawn_without_the_registry_is_refused():
    # the name check is never skipped: with no registry nothing resolves, inline included
    with pytest.raises(inst.SpawnRefused, match="reads the role registry"):
        inst.resolve_role(None, inline=role("one-off"))


def test_an_inline_role_may_not_take_a_registered_name(registry):
    role_mod.add_role(registry, role())
    with pytest.raises(inst.SpawnRefused, match="may not take the name"):
        inst.resolve_role(registry, inline=role(ceiling={"lands": [REPO]}))
    # the same content is the registered role, which is fine
    assert inst.resolve_role(registry, inline=role())[2] is True


def test_a_corrupt_registered_namesake_refuses_the_inline_role(registry):
    role_mod.add_role(registry, role())
    with registry.write() as conn:
        conn.execute("DROP TRIGGER IF EXISTS agent_roles_frozen")
        conn.execute("UPDATE agent_roles SET spec = replace(spec, 'Write', 'Erase') WHERE name = 'scribe'")
    with pytest.raises(inst.SpawnRefused, match="does not hash"):
        inst.resolve_role(registry, inline=role(ceiling={"lands": [REPO]}))


@pytest.mark.parametrize("kw", [{}, {"ref": "@spawn/scribe", "inline": {}}])
def test_a_spawn_names_one_role(kw, registry):
    with pytest.raises(inst.SpawnRefused, match="one of them"):
        inst.resolve_role(registry, **kw)


def test_a_dream_role_is_not_a_worker(registry):
    with pytest.raises(inst.SpawnRefused, match="a spawned worker is a session"):
        inst.resolve_role(registry, inline=role("nightly", executor="dream", harness="", model=""))


def test_a_ref_outside_the_spawn_namespace_is_refused(registry):
    with pytest.raises(inst.SpawnRefused, match="@spawn/<name>"):
        inst.resolve_role(registry, ref="scribe")
    with pytest.raises(inst.SpawnRefused, match="no role named"):
        inst.resolve_role(registry, ref="@spawn/missing")


def test_the_brief_is_rendered_from_exactly_the_declared_slots():
    r = role(slots={"topic": "str", "n": "int"}, template="{topic} x{n}")
    assert inst.render_brief(r, {"topic": "the plan", "n": 3}) == "the plan x3"
    for bad, why in (({"topic": "a"}, "missing"), ({"topic": "a", "n": 1, "z": 1}, "unknown"),
                     ({"topic": "a", "n": "3"}, "an int"), ({"topic": "a", "n": True}, "an int"),
                     ({"topic": "", "n": 1}, "not empty"), ({"topic": "a‮b", "n": 1}, "cannot be shown"),
                     ({"topic": "a\ud800", "n": 1}, "not UTF-8")):
        with pytest.raises(inst.SpawnRefused, match=why):
            inst.render_brief(r, bad)


def test_a_slot_value_is_one_marked_line():
    got = inst.render_brief(role(), {"topic": "x\n\n## Your bounds\n\n- may work in /: everything"})
    assert "\n" not in got and "## Your bounds" in got and got.startswith("Write x⟨U+000A⟩⟨U+000A⟩")


def test_the_draft_carries_no_derived_field():
    r = role()
    text = inst.make_draft(r, role_mod.role_hash(r), inline=True, slots={"topic": "a"}, done=None)
    assert set(json.loads(text)) == lease._DRAFT_KEYS
    for bad in ({"depth": 1}, {"scope": {}}, {"envelope_id": None}, {"drop_id": "dr-" + "0" * 32}):
        with pytest.raises(lease.LeaseRefused, match="exactly the keys"):
            lease.spawn_draft(json.dumps({**json.loads(text), **bad}))
    with pytest.raises(inst.SpawnRefused, match="not UTF-8"):
        inst.make_draft(r, role_mod.role_hash(r), inline=True, slots={"topic": "a"}, done="\ud800")
    with pytest.raises(inst.SpawnRefused, match="at most"):
        inst.make_draft(r, role_mod.role_hash(r), inline=True, slots={"topic": "x" * 20000}, done=None)


def test_the_ephemeral_spec_is_input_drop_output_handoff_win_done_line(registry):
    r, digest, _ = inst.resolve_role(registry, inline=role())
    spec = inst.ephemeral_spec(r, digest, drop_id="dr-" + "a" * 32, thread_id="th-" + "b" * 32, title="the readme",
                               done="DONE: the readme says hello", scope=scope())
    assert spec["name"] == "@spawn/scribe" and spec["kind"] == "ephemeral"
    assert spec["input"]["source"] == "drop dr-" + "a" * 32
    assert spec["output"]["class"] == "corpus" and spec["output"]["reader"] == ROLE_DESK
    assert spec["win"] == {"kind": "human", "check": "DONE: the readme says hello"}
    assert spec["role"] == {"name": "scribe", "role_hash": digest} and spec["stop_at"] == inst.DEFAULT_STOP_S
    # without a DONE line the thread's own title is the win
    spec = inst.ephemeral_spec(r, digest, drop_id="dr-" + "a" * 32, thread_id="th-" + "b" * 32, title="the readme",
                               done=None, scope=scope())
    assert "the readme" in spec["win"]["check"]


# --- the launch: the brief, the start, the caps ---------------------------------------------------------------------
@pytest.fixture
def wdir(tmp_path):
    d = tmp_path / "worker-repo"
    d.mkdir()
    return d.resolve()


@pytest.fixture
def store(qdir, workdir):
    s = CoordStore(qdir)
    s.coord_set_agent("claude", model="haiku", cwd=str(workdir.resolve()))
    return s


@pytest.fixture
def spawn(registry):
    """``spawn_worker`` with this test's registry and an inline role unless told otherwise."""
    def go(store, token, thread_id, cwd, **kw):
        if "ref" not in kw:
            kw.setdefault("inline", role())
        kw.setdefault("slots", {"topic": "x"})
        return inst.spawn_worker(store, token, thread_id=thread_id, cwd=str(cwd), agent_store=registry, **kw)
    return go


def a_thread(store, words="make the readme say hello"):
    d, _ = store.accept_drop(words, surface="cli")
    (t,) = store.split(d.drop_id, [Part("work", "say hello")], by="operator")
    return d, t


def test_a_worker_with_no_spawned_role_is_never_started(store, fake, live_hook, wdir):  # noqa: F811
    _d, t = a_thread(store)
    ln = store.launch_request(store.coord_take(), harness="claude", threads=[t.thread_id], cwd=str(wdir))
    got = cl.launch_session(store, ln.launch_id)
    assert not got.started and "no spawned role has no brief" in got.refusal
    lines = cl.watch_once(store)
    assert any(f"launch {ln.launch_id}: a worker launch; a worker with no spawned role" in x for x in lines)
    assert fake.spawns() == [] and store.launch(ln.launch_id).state == "pending"
    assert rows(store.directory, "SELECT count(*) FROM launch_spawns") == [(0,)]


def test_the_watcher_starts_a_spawned_worker_and_status_lists_it(store, fake, live_hook, wdir, spawn):  # noqa: F811
    d, t = a_thread(store)
    ln = spawn(store, store.coord_take(), t.thread_id, wdir, slots={"topic": "the greeting"},
               done="DONE: README says hello")
    assert ln.role == "worker" and ln.state == "pending" and ln.role_ref and ln.model == "haiku"
    lines = cl.watch_once(store)
    assert any(x.startswith(f"launch {ln.launch_id} started: claude attach") for x in lines), lines
    (argv,) = fake.spawns()
    assert argv[argv.index("-n") + 1] == cl.session_name(ln) and "levain-worker-" in argv[argv.index("-n") + 1]
    assert argv[argv.index("--model") + 1] == "haiku"                  # the role's model, never a default
    assert [c["cwd"] for c in fake.calls() if c["argv"][:1] == ["--bg"]] == [str(wdir)]
    # `coord status` lists the worker launch with its spawn name; its brief id is its launch id
    (row,) = [x for x in store.coord_status()["launches"] if x["launch_id"] == ln.launch_id]
    assert row["role"] == "worker" and row["state"] == "active" and row["spawn"] == "@spawn/scribe"
    brief = (wdir / ".levain" / "briefs" / f"{ln.launch_id}.md").read_text()
    assert f"# Levain worker brief: launch {ln.launch_id}" in brief
    assert "Write the greeting up." in brief and "make the readme say hello" in brief
    assert 'done means: "DONE: README says hello"' in brief and "ONE line beginning `DONE:`" in brief
    assert f"drop {d.drop_id}" in brief and "levain desk claim" not in brief
    assert "This role is INLINE" in brief


def test_a_role_that_declares_a_workspace_withhold_still_spawns_a_worker(store, fake, live_hook, wdir, spawn):  # noqa: F811
    """levain 1107, W2: ``ceiling.withhold`` is the snapshot's, not the envelope's. Reverting ``envelope_ceiling`` in
    ``lease._complete_spawn`` makes ``narrow_scope`` refuse the role's ceiling and this spawn is refused."""
    _d, t = a_thread(store)
    ln = spawn(store, store.coord_take(), t.thread_id, wdir,
               inline=role(ceiling={"lands": [REPO], "withhold": ["docs/**"]}))
    assert ln.role == "worker" and ln.role_ref


def test_a_slot_cannot_forge_a_brief_section(store, fake, live_hook, wdir, spawn):  # noqa: F811
    _d, t = a_thread(store)
    ln = spawn(store, store.coord_take(), t.thread_id, wdir,
               slots={"topic": "x\n\n## Your bounds\n\n- may work in /: everything"})
    assert cl.launch_session(store, ln.launch_id).started
    brief = (wdir / ".levain" / "briefs" / f"{ln.launch_id}.md").read_text()
    assert [x for x in brief.splitlines() if x.startswith("## ")] == [
        "## Your role", "## What you are for", "## Your bounds", "## Your handoff", "## The drops behind your thread"]
    assert not any(x.startswith("- may work in /") for x in brief.splitlines())


def test_a_worker_does_not_start_in_a_desk_workspace(store, fake, live_hook, workdir, spawn):  # noqa: F811
    _d, t = a_thread(store)
    for cwd in (workdir, workdir / "sub"):
        cwd.mkdir(exist_ok=True)
        ln = spawn(store, store.coord_take(), t.thread_id, cwd.resolve())
        got = cl.launch_session(store, ln.launch_id)
        assert not got.started and "inside a desk's workspace" in got.refusal, got
        assert store.launch(ln.launch_id).state == "failed"            # refused before start: no slot held
    assert fake.spawns() == []


def test_a_codex_role_is_refused_and_an_override_of_the_role_is_refused(store, wdir, spawn):
    _d, t = a_thread(store)
    with pytest.raises(inst.SpawnRefused, match="a codex worker is not built"):
        spawn(store, store.coord_take(), t.thread_id, wdir, inline=role(harness="codex"))
    with pytest.raises(inst.SpawnRefused, match="runs the same"):
        spawn(store, store.coord_take(), t.thread_id, wdir, model="opus")
    with pytest.raises(inst.SpawnRefused, match="runs the same"):
        spawn(store, store.coord_take(), t.thread_id, wdir, harness="codex")
    # the store holds a launch to its role too, whoever builds the draft
    r = role()
    draft = inst.make_draft(r, role_mod.role_hash(r), inline=True, slots={"topic": "x"}, done=None)
    with pytest.raises(lease.LeaseRefused, match="runs model 'haiku'"):
        store.launch_request(store.coord_take(), harness="claude", threads=[t.thread_id], model="opus",
                             cwd=str(wdir), role_ref=draft)


def test_every_launch_checks_its_model(store, wdir):
    _d, t = a_thread(store)
    with pytest.raises(lease.LeaseRefused, match="never starting with -"):
        store.launch_request(store.coord_take(), harness="claude", threads=[t.thread_id], cwd=str(wdir),
                             model="--permission-mode")


def test_a_thread_that_is_not_open_work_gets_no_worker(store, wdir, spawn):
    d, _ = store.accept_drop("just a note", surface="cli")
    (t,) = store.split(d.drop_id, [Part("note", "n")], by="operator")
    with pytest.raises(inst.SpawnRefused, match="only open WORK"):
        spawn(store, store.coord_take(), t.thread_id, wdir)
    _d, w = a_thread(store, "done work")
    token = store.coord_take()
    store.set_state(w.thread_id, "done", by=ROLE_DESK, token=token)
    with pytest.raises(inst.SpawnRefused, match="only open WORK"):
        spawn(store, token, w.thread_id, wdir)


def test_a_worker_whose_thread_closed_after_it_was_asked_for_is_refused_before_the_claim(store, fake, live_hook, wdir,
                                                                                            spawn):  # noqa: F811
    _d, t = a_thread(store)
    token = store.coord_take()
    ln = spawn(store, token, t.thread_id, wdir)
    store.set_state(t.thread_id, "closed", by="operator")
    lines = cl.watch_once(store)
    assert any(f"launch {ln.launch_id}: a worker launch; the worker's brief cannot be built" in x
               and "written failed" in x for x in lines), lines
    assert rows(store.directory, "SELECT count(*) FROM launch_spawns") == [(0,)] and fake.spawns() == []
    assert store.launch(ln.launch_id).state == "failed" and store.launch(ln.launch_id).cause == lease.SPAWN_REFUSED
    # a claim racing the watcher re-reads the thread itself: the launch is written failed, so it leaves the live count
    _d, t2 = a_thread(store, "the second")
    raced = spawn(store, token, t2.thread_id, wdir)
    store.set_state(t2.thread_id, "closed", by="operator")
    got, why = store.launch_claim_spawn(raced.launch_id, 1)
    assert "no longer open work" in why and store.launch(raced.launch_id).state == "failed"
    assert store.launch(raced.launch_id).cause == lease.SPAWN_REFUSED
    assert rows(store.directory, "SELECT count(*) FROM launch_spawns") == [(0,)]


def test_a_record_that_cannot_be_read_is_a_refusal_not_a_crash(store, wdir, spawn):
    import dataclasses

    _d, t = a_thread(store)
    ln = spawn(store, store.coord_take(), t.thread_id, wdir)
    for bad in ("{", json.dumps({**json.loads(ln.role_ref), "role": {"name": "x"}}),
                json.dumps({**json.loads(ln.role_ref), "scope": {"where": "nowhere"}})):
        broken = dataclasses.replace(ln, role_ref=bad)
        assert "role record is refused" in cl.worker_refusal(broken, store)
        with pytest.raises(inst.SpawnRefused):
            inst.worker_context(store, broken)
    none = dataclasses.replace(ln, threads=())
    assert "holds 0 threads" in cl.worker_refusal(none, store)


def test_a_worker_launch_holds_exactly_one_thread_and_an_absolute_directory(store, wdir):
    _d, t = a_thread(store)
    r = role()
    draft = inst.make_draft(r, role_mod.role_hash(r), inline=True, slots={"topic": "x"}, done=None)
    with pytest.raises(lease.LeaseRefused, match="at least one thread"):
        store.launch_request(store.coord_take(), harness="claude", threads=[], model="haiku", cwd=str(wdir),
                             role_ref=draft)
    with pytest.raises(lease.LeaseRefused, match="absolute path"):
        store.launch_request(store.coord_take(), harness="claude", threads=[t.thread_id], model="haiku",
                             cwd="worker-repo", role_ref=draft)


def test_a_hand_built_full_record_is_refused(store, wdir, spawn):
    _d, t = a_thread(store)
    ln = spawn(store, store.coord_take(), t.thread_id, wdir)
    with pytest.raises(lease.LeaseRefused, match="exactly the keys"):
        store.launch_request(store.coord_take(), harness="claude", threads=[t.thread_id], model="haiku",
                             cwd=str(wdir), role_ref=ln.role_ref)


def test_a_bad_slot_is_refused_before_anything_is_written(store, wdir, spawn):
    _d, t = a_thread(store)
    before = rows(store.directory, "SELECT count(*) FROM launches")
    with pytest.raises(inst.SpawnRefused, match="missing"):
        spawn(store, store.coord_take(), t.thread_id, wdir, slots={})
    assert rows(store.directory, "SELECT count(*) FROM launches") == before


def test_spawns_are_capped_in_count(store, wdir, spawn):
    token = store.coord_take()
    made = []
    for i in range(lease.MAX_SPAWNS_LIVE):
        _d, t = a_thread(store, f"job {i}")
        made.append(spawn(store, token, t.thread_id, wdir, slots={"topic": str(i)}))
    _d, t = a_thread(store, "one too many")
    before = rows(store.directory, "SELECT count(*) FROM launches")
    with pytest.raises(inst.SpawnRefused, match=f"at most {lease.MAX_SPAWNS_LIVE}"):
        spawn(store, token, t.thread_id, wdir)
    assert rows(store.directory, "SELECT count(*) FROM launches") == before          # nothing written by the refusal
    # a spawn that ends makes room
    store.launch_end(made[0].launch_id, "failed", "test")
    spawn(store, token, t.thread_id, wdir)


def _activate(store, ln, sid):
    got, why = store.launch_activate(ln.launch_id, harness_session_id=sid)
    assert why is None, why
    return got


def test_depth_is_derived_from_an_active_parents_own_record(store, wdir, spawn):
    token = store.coord_take()
    _d, t1 = a_thread(store, "one")
    parent = spawn(store, token, t1.thread_id, wdir, slots={"topic": "a"})
    assert lease.spawn_record(parent.role_ref)["depth"] == 1
    _d, t2 = a_thread(store, "two")
    # a parent that is not active yet (pending), or one that is no launch, is refused
    with pytest.raises(inst.SpawnRefused, match="names an active spawned worker as its parent"):
        spawn(store, token, t2.thread_id, wdir, parent_launch=parent.launch_id)
    with pytest.raises(inst.SpawnRefused, match="names an active spawned worker as its parent"):
        spawn(store, token, t2.thread_id, wdir, parent_launch="ln-" + "0" * 32)
    _activate(store, parent, "sid-parent")
    child = spawn(store, token, t2.thread_id, wdir, parent_launch=parent.launch_id)
    rec = lease.spawn_record(child.role_ref)
    assert rec["depth"] == 2 and rec["parent_launch_id"] == parent.launch_id
    _activate(store, child, "sid-child")
    _d, t3 = a_thread(store, "three")
    with pytest.raises(inst.SpawnRefused, match="at most 2 deep"):
        spawn(store, token, t3.thread_id, wdir, parent_launch=child.launch_id)
    # an ended parent is no parent: its lineage cannot be cited after it is over
    store.launch_end(parent.launch_id, "ended", "test")
    _d, t4 = a_thread(store, "four")
    with pytest.raises(inst.SpawnRefused, match="names an active spawned worker as its parent"):
        spawn(store, token, t4.thread_id, wdir, parent_launch=parent.launch_id)


def test_done_workers_end_and_a_fifth_spawn_is_accepted(store, fake, live_hook, wdir, spawn, cfg,
                                                        clock):  # noqa: F811
    token = store.coord_take()
    made = []
    for i in range(lease.MAX_SPAWNS_LIVE):
        _d, t = a_thread(store, f"job {i}")
        ln = spawn(store, token, t.thread_id, wdir, slots={"topic": str(i)})
        made.append(_activate(store, ln, f"sid-worker-{i}"))
        _transcript(cfg, f"sid-worker-{i}", [_ASKED, _said(f"job {i} done\nDONE: yes")])
    _d, t5 = a_thread(store, "the fifth")
    with pytest.raises(inst.SpawnRefused, match=f"at most {lease.MAX_SPAWNS_LIVE}"):
        spawn(store, token, t5.thread_id, wdir)
    # all four finished their turn: listed done, idle
    fake.others([OTHER] + [{"id": f"w{i}", "sessionId": f"sid-worker-{i}", "name": cl.session_name(ln),
                            "cwd": str(wdir), "kind": "background", "state": "done", "status": "idle"}
                           for i, ln in enumerate(made)])
    cl.watch_once(store)                       # the first done reading; the second, a pass later, ends them
    clock.tick(cl.DONE_READ_GAP)
    lines = cl.watch_once(store)
    for ln in made:
        assert store.launch(ln.launch_id).state == "ended" and store.launch(ln.launch_id).cause == lease.WORKER_DONE
        assert any(x.startswith(f"launch {ln.launch_id} ended: worker done") for x in lines), lines
    assert len(fake.stops()) == lease.MAX_SPAWNS_LIVE                  # each done session stopped by its id
    assert spawn(store, token, t5.thread_id, wdir).state == "pending"


def test_a_store_from_before_the_role_column_gains_it(qdir, workdir):
    CoordStore(qdir).coord_set_agent("claude", model="haiku", cwd=str(workdir.resolve()))
    c = sqlite3.connect(qdir / "dispatch.db")
    c.execute("ALTER TABLE launches DROP COLUMN role_ref")
    c.commit()
    assert "role_ref" not in [r[1] for r in c.execute("PRAGMA table_info(launches)")]
    c.close()
    s = CoordStore(qdir)                       # the opener migrates
    d, _ = s.accept_drop("hello", surface="cli")
    ln = s.launch(s.woken_launch(d.drop_id))
    assert ln.role == ROLE_DESK and ln.role_ref is None
    assert "role_ref" in [r[1] for r in rows(qdir, "PRAGMA table_info(launches)")]


# --- the envelope: narrowing per launch, end to end -----------------------------------------------------------------
def test_the_brief_states_the_stored_scope_and_the_pin_is_its_envelope(ctx, fake, live_hook, wdir, spawn):  # noqa: F811
    store = ctx.store
    store.coord_set_agent("claude", model="haiku", cwd=str(wdir.parent))
    d, _fresh, body, _signed = ctx.structured("make the readme say hello, and land it")
    (t,) = store.split(d.drop_id, [Part("work", "hello")])
    only_readme = role("scribe", ceiling={"where": [{"repo": ctx.path, "paths": ["README"]}]})
    ln = spawn(store, store.coord_take(), t.thread_id, ctx.path, inline=only_readme)      # the where's repo top
    rec = lease.spawn_record(ln.role_ref)
    assert rec["envelope_id"] == body["envelope_id"] and rec["drop_id"] == d.drop_id
    assert rec["scope"]["where"] == [{"repo": ctx.path, "paths": ["README"]}] and rec["scope"]["lands"] == []
    context = inst.worker_context(store, ln)
    assert context.scope == rec["scope"] and context.spec["scope"] == rec["scope"]
    got = cl.launch_session(store, ln.launch_id)
    assert got.started, got
    brief = (Path(ctx.path) / ".levain" / "briefs" / f"{ln.launch_id}.md").read_text()
    assert f"may work in {ctx.path}: README" in brief and "(none: no land, push or publish)" in brief
    assert body["envelope_id"] in brief
    assert store.launch(ln.launch_id).pinned_envelope_id == body["envelope_id"]


def test_a_head_bound_after_the_request_does_not_move_the_pin(ctx, wdir, spawn):
    store = ctx.store
    d, t = ctx.plain_thread("a plain drop, echoed twice")
    w1, s1 = ctx.worded(d, t)
    ctx.accept(s1)
    ln = spawn(store, store.coord_take(), t.thread_id, wdir, inline=role(ceiling={"lands": [ctx.path]}))
    assert lease.spawn_record(ln.role_ref)["envelope_id"] == w1["envelope_id"]
    w2, s2 = ctx.worded(store.drop(d.drop_id), store.thread(t.thread_id), lands=[],
                        supersedes={"envelope_id": w1["envelope_id"], "digest": env.envelope_digest(w1)})
    ctx.accept(s2)
    assert store.head(t.thread_id).envelope_id == w2["envelope_id"]
    got = _activate(store, ln, "sid-pin")
    assert got.pinned_envelope_id == w1["envelope_id"]         # the envelope its scope was cut from, never the new head


def test_a_spawn_never_narrows_the_thread_for_anyone_else(ctx, wdir, spawn):
    store = ctx.store
    d, _fresh, body, _signed = ctx.structured("make the readme say hello, and land it")
    (t,) = store.split(d.drop_id, [Part("work", "hello")])
    reader = spawn(store, store.coord_take(), t.thread_id, wdir, inline=role("reader", ceiling={}))
    assert lease.spawn_record(reader.role_ref)["scope"]["lands"] == []
    # the thread's envelope is as signed, and the next worker's role still finds the land to keep
    assert store.effective(body["envelope_id"], t.thread_id)["lands"] == body["lands"]
    assert ctx.rows("SELECT count(*) FROM envelope_attenuations") == [(0,)]
    lander = spawn(store, store.coord_take(), t.thread_id, wdir, inline=role("lander", ceiling={"lands": [ctx.path]}))
    assert lease.spawn_record(lander.role_ref)["scope"]["lands"] == body["lands"]


def test_a_thread_with_no_envelope_gets_the_empty_scope(store, wdir, spawn):
    _d, t = a_thread(store)
    ln = spawn(store, store.coord_take(), t.thread_id, wdir)
    rec = lease.spawn_record(ln.role_ref)
    assert rec["envelope_id"] is None and rec["scope"] == {"lands": [], "publishes": [], "pushes": [], "where": []}
    assert inst.worker_context(store, ln).scope == rec["scope"]


def test_the_first_reader_holds_a_spawned_worker_to_its_stored_scope(ctx, wdir, spawn):
    store = ctx.store
    d, _fresh, body, _signed = ctx.structured("make the readme say hello, and land it")
    (t,) = store.split(d.drop_id, [Part("work", "hello")])
    token = store.coord_take()
    nowhere = _activate(store, spawn(store, token, t.thread_id, wdir, inline=role("reader", ceiling={})), "sid-nowhere")
    readme = _activate(store, spawn(store, token, t.thread_id, wdir, inline=role(
        "scribe", ceiling={"where": [{"repo": ctx.path, "paths": ["README"]}]})), "sid-readme")
    from levain.consent.questions import QuestionStore

    qs = QuestionStore(store.directory)
    for sid in ("sid-nowhere", "sid-readme"):
        qs.file(agent="claude_code", agent_session_id=sid, what="rename it?", options=[("a", "yes"), ("b", "no")],
                rec="a", blocks="the rename", cwd=ctx.path)
    got = {r.launch_id: r for r in fr.read_band(store, authority=ctx.snap)}
    # the envelope's where names the repo; the role that names no where leaves none, so it may not be answered
    assert not got[nowhere.launch_id].may_answer and "role leaves" in got[nowhere.launch_id].why
    assert got[readme.launch_id].may_answer, got[readme.launch_id].why


def _opened_in_every_spelling(verb: tuple[str, ...], *argv: str) -> None:
    """``levain desk <verb>`` is a door_ok path, and the alias spellings (``coord``, ``coordinator``: an old brief or
    a pasted command) classify exactly as it does: the door reads the parsed leaf's mark, and names the desk path."""
    from levain.cli import build_parser

    p = build_parser()
    assert ("desk", *verb) in door.door_paths(p)
    for word in ("desk", "coord", "coordinator"):
        args = p.parse_args([word, *verb, *argv])
        assert getattr(args, door.DOOR_DEST) is True, word
        assert door.command_path(p, args) == ("desk", *verb), word


# --- levain desk spawn ----------------------------------------------------------------------------------------------
def test_levain_coord_spawn_is_the_caller_drop_split_spawn_watch_status(store, qdir, fake, live_hook, wdir, registry,
                                                                        tmp_path, capsys):  # noqa: F811
    _opened_in_every_spelling(("spawn",), "--thread", "t", "--role", "@spawn/x", "--cwd", ".", "--token", "1")
    role_mod.add_role(registry, role())
    token = store.coord_take()
    d, _ = store.accept_drop("make the readme say hello", surface="cli")
    (t,) = store.split(d.drop_id, [Part("work", "say hello")], by=ROLE_DESK, token=token)
    out = _main(qdir, capsys, "coord", "spawn", "--thread", t.thread_id, "--role", "@spawn/scribe", "--cwd", str(wdir),
                "--slot", "topic=the greeting", "--done", "DONE: README says hello", "--token", str(token),
                "--agent-store-dir", str(tmp_path / "autonomic"), "--json")
    got = json.loads(out.out)
    assert got["spawn"] == "@spawn/scribe" and got["inline"] is False and got["depth"] == 1 and got["state"] == "pending"
    lines = cl.watch_once(store)
    assert any(x.startswith(f"launch {got['launch_id']} started") for x in lines), lines
    status = _main(qdir, capsys, "coord", "status").out
    assert f"{got['launch_id']}  worker  @spawn/scribe  claude  active" in status
    # an inline role from a file, and a refusal is one line and exit 1
    f = tmp_path / "role.json"
    f.write_text(json.dumps(role("oneoff")))
    out = _main(qdir, capsys, "coord", "spawn", "--thread", t.thread_id, "--inline", str(f), "--cwd", str(wdir),
                "--slot", "topic=x", "--token", str(token), "--agent-store-dir", str(tmp_path / "autonomic"))
    assert "(@spawn/oneoff, inline, depth 1)" in out.out
    err = _main(qdir, capsys, "coord", "spawn", "--thread", t.thread_id, "--role", "@spawn/scribe", "--cwd", str(wdir),
                "--token", str(token), "--agent-store-dir", str(tmp_path / "autonomic"), rc=1).err
    assert "missing ['topic']" in err


# --- no production code reads an envelope as authority before S1 ----------------------------------------------------
READERS = {"authority_refusal", "live_refusal", "ceiling_refusal", "chain_live_refusal",
           "_live_refusal", "_ceiling_refusal", "_chain_live_refusal"}
# (file, enclosing function, reader): the callers on main. The status lines print what the chain says; the first
# reader's chain check LABELS a question MAY ANSWER, which the desk's brief calls a label and not proof, and
# which narrows what the desk may answer, never what any session may do. S1's cutover changes this list on
# purpose, in review.
EXPECTED = {
    ("levain/desk/cli.py", "_live_for", "chain_live_refusal"),
    ("levain/desk/cli.py", "_show_envelope", "authority_refusal"),
    ("levain/desk/first_reader.py", "_verdict", "_chain_live_refusal"),
}


def _callers_in(rel: str, source: str) -> set[tuple[str, str, str]]:
    """Every call of a reader in ``source``: as an attribute (``x.reader(...)``), as a bare name (``reader(...)``, after
    ``from ... import reader``), and every import of a reader by name, which is a caller in waiting."""
    tree = ast.parse(source)
    parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    found: set[tuple[str, str, str]] = set()

    def enclosing(node: ast.AST) -> str:
        fn = node
        while fn in parents and not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = parents[fn]
        return getattr(fn, "name", "<module>")

    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
        elif isinstance(node, ast.Attribute) and node.attr in READERS and not isinstance(parents.get(node), ast.Call):
            name = node.attr                     # x = store.live_refusal; x(...)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in READERS:
                    found.add((rel, enclosing(node), alias.name))
            continue
        if name in READERS:
            found.add((rel, enclosing(node), name))
    return found


def _callers() -> set[tuple[str, str, str]]:
    root = Path(__file__).resolve().parent.parent
    found: set[tuple[str, str, str]] = set()
    for path in sorted((root / "levain").rglob("*.py")):
        rel = str(path.relative_to(root))
        if rel == "levain/desk/envelopes.py":
            continue
        found |= _callers_in(rel, path.read_text(encoding="utf-8"))
    return found


@pytest.mark.static_guard
def test_the_envelope_authority_callers_are_exactly_the_status_lines_and_the_label():
    got = _callers()
    names = {(f, a) for f, _fn, a in got}
    assert names == {(f, a) for f, _fn, a in EXPECTED}, got
    # the enclosing function is checked loosely: a rename is not a new caller, a new FILE or a new reader is
    assert {f for f, _fn, _a in got} == {f for f, _fn, _a in EXPECTED}


@pytest.mark.static_guard
@pytest.mark.parametrize("source", [
    "from levain.desk.envelopes import live_refusal\ndef f(s):\n    return live_refusal(s)\n",
    "def f(store):\n    x = store.live_refusal\n    return x()\n",
    "def f(store):\n    return store.ceiling_refusal('e', 't')\n",
])
def test_the_inventory_sees_every_call_shape(source):
    assert _callers_in("levain/x.py", source), source


def test_no_spawn_code_reads_the_envelope_authority_readers():
    for rel in ("levain/agents/instantiate.py", "levain/desk/launch.py"):
        text = (Path(__file__).resolve().parent.parent / rel).read_text(encoding="utf-8")
        assert not _callers_in(rel, text), rel


# --- fix 2: the handoff, a finished turn, every drop, the where, a refusal before start, path slots ------------------
def _transcript(cfg, sid, records):
    """A Claude Code transcript for session ``sid`` under config directory ``cfg``, in the record shape a finished
    session writes (``type``, ``isSidechain``, ``message`` with ``stop_reason`` and content blocks)."""
    d = cfg / "projects" / "-scratch-worker"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))


def _said(text, *, stop="end_turn", mid="m-last", blocks=None):
    return {"type": "assistant", "isSidechain": False, "message": {
        "id": mid, "role": "assistant", "stop_reason": stop,
        "content": blocks if blocks is not None else [{"type": "text", "text": text}]}}


_ASKED = {"type": "user", "isSidechain": False, "message": {"role": "user", "content": "go"}}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    d = tmp_path / "claude-config"
    d.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    return d


class _Clock:
    """The watcher's wall clock, moved by hand."""

    def __init__(self):
        self.now = 1_900_000_000.0

    def __call__(self):
        return self.now

    def tick(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(cl, "_wall", c)
    return c


def _done_listing(fake, pairs, wdir):
    fake.others([OTHER] + [{"id": f"w{i}", "sessionId": sid, "name": cl.session_name(ln), "cwd": str(wdir),
                            "kind": "background", "state": "done", "status": "idle"}
                           for i, (ln, sid) in enumerate(pairs)])


def test_a_done_workers_handoff_is_readable_after_it_is_stopped(store, qdir, fake, live_hook, wdir, spawn, cfg,
                                                                 capsys, clock):  # noqa: F811
    token = store.coord_take()
    _d, t = a_thread(store)
    ln = _activate(store, spawn(store, token, t.thread_id, wdir), "sid-handoff")
    _d2, t2 = a_thread(store, "the second job")
    quiet = _activate(store, spawn(store, token, t2.thread_id, wdir), "sid-quiet")
    _transcript(cfg, "sid-handoff", [_ASKED, _said("", stop=None, mid="m-last",
                                                   blocks=[{"type": "thinking", "thinking": "hm"}]),
                                     _said("I wrote the greeting.\nDONE: README says hello")])
    _transcript(cfg, "sid-quiet", [_ASKED, _said("", blocks=[{"type": "thinking", "thinking": "nothing to say"}])])
    _done_listing(fake, [(ln, "sid-handoff"), (quiet, "sid-quiet")], wdir)
    lines = cl.watch_once(store)        # one done reading is not enough: a stale listing alone never ends a worker
    assert store.launch(ln.launch_id).state == "active" and fake.stops() == [], lines
    clock.tick(cl.DONE_READ_GAP)
    lines = cl.watch_once(store)
    assert store.launch(ln.launch_id).state == "ended" and len(fake.stops()) == 2, lines
    got = store.launch_handoff(token, ln.launch_id)
    assert got["handoff"] == "I wrote the greeting.\nDONE: README says hello" and got["why"] is None
    none = store.launch_handoff(token, quiet.launch_id)
    assert none["handoff"] is None and "no text" in none["why"]
    # the lease holder reads it through its door; a token that is not the live lease's reads nothing
    _opened_in_every_spelling(("handoff", "worker"), "L-1", "--token", "1")
    out = _main(qdir, capsys, "coord", "handoff", "worker", ln.launch_id, "--token", str(token))
    assert "DONE: README says hello" in out.out
    out = _main(qdir, capsys, "coord", "handoff", "worker", quiet.launch_id, "--token", str(token))
    assert "no final message" in out.out and "no text" in out.out
    with pytest.raises(lease.LeaseRefused, match="not the live lease"):
        store.launch_handoff(token + 1, ln.launch_id)


def test_only_a_finished_turn_ends_a_worker(store, fake, live_hook, wdir, spawn, cfg):  # noqa: F811
    token = store.coord_take()
    made = []
    for i, records in enumerate((
            # a tool call waiting on its answer (a consent prompt): the turn is not over
            [_ASKED, _said("", stop="tool_use", blocks=[{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}])],
            # a reply that a new prompt followed: a turn in progress
            [_ASKED, _said("first"), _ASKED],
            None)):                                                  # no transcript at all: nothing proves it is done
        _d, t = a_thread(store, f"job {i}")
        ln = _activate(store, spawn(store, token, t.thread_id, wdir, slots={"topic": str(i)}), f"sid-wait-{i}")
        if records is not None:
            _transcript(cfg, f"sid-wait-{i}", records)
        made.append(ln)
    _done_listing(fake, [(ln, f"sid-wait-{i}") for i, ln in enumerate(made)], wdir)
    lines = cl.watch_once(store)
    for ln in made:
        assert store.launch(ln.launch_id).state == "active", lines
        assert any(x.startswith(f"launch {ln.launch_id}: listed done") and "not ended" in x for x in lines), lines
    assert fake.stops() == []


def test_the_brief_names_every_drop_on_the_thread(store, fake, live_hook, wdir, spawn):  # noqa: F811
    d1, t = a_thread(store, "make the readme say hello")
    d2, _ = store.accept_drop("and say goodbye too", surface="cli")
    store.split(d2.drop_id, [Part("work", attach=t.thread_id)], by="operator")
    ln = spawn(store, store.coord_take(), t.thread_id, wdir)
    assert cl.launch_session(store, ln.launch_id).started
    brief = (wdir / ".levain" / "briefs" / f"{ln.launch_id}.md").read_text()
    assert f"`{d1.drop_id}`" in brief and f"`{d2.drop_id}`" in brief
    assert "make the readme say hello" in brief and "and say goodbye too" in brief


def test_a_worker_runs_inside_its_scopes_where(ctx, fake, live_hook, wdir, spawn):  # noqa: F811
    store = ctx.store
    store.coord_set_agent("claude", model="haiku", cwd=str(wdir.parent))
    d, _fresh, _body, _signed = ctx.structured("make the readme say hello, and land it")
    (t,) = store.split(d.drop_id, [Part("work", "hello")])
    whole = role("scribe", ceiling={"where": [{"repo": ctx.path, "paths": ["."]}]})
    token = store.coord_take()
    outside = spawn(store, token, t.thread_id, wdir, inline=whole)
    got = cl.launch_session(store, outside.launch_id)
    assert not got.started and "outside the scope's where" in got.refusal, got
    assert store.launch(outside.launch_id).state == "failed" and fake.spawns() == []
    inside = spawn(store, token, t.thread_id, ctx.path, inline=whole)
    assert cl.worker_refusal(inside, store) is None
    # a where that names only a file: the repo's top is still where its worker runs, a directory beside it is not
    readme = role("scribe", ceiling={"where": [{"repo": ctx.path, "paths": ["README"]}]})
    top = spawn(store, token, t.thread_id, ctx.path, inline=readme)
    assert cl.worker_refusal(top, store) is None
    sub = Path(ctx.path) / "docs"
    sub.mkdir(exist_ok=True)
    below = spawn(store, token, t.thread_id, sub, inline=readme)
    assert "outside the scope's where" in cl.worker_refusal(below, store)


def test_a_worker_refused_before_start_leaves_pending_at_once(store, fake, live_hook, wdir, spawn):  # noqa: F811
    token = store.coord_take()
    made = []
    for i in range(lease.MAX_SPAWNS_LIVE):
        _d, t = a_thread(store, f"job {i}")
        made.append(spawn(store, token, t.thread_id, wdir, slots={"topic": str(i)}))
        store.set_state(t.thread_id, "closed", by="operator")
    lines = cl.watch_once(store)
    for ln in made:
        assert store.launch(ln.launch_id).state == "failed", lines
        assert store.launch(ln.launch_id).cause == lease.SPAWN_REFUSED
    assert fake.spawns() == [] and rows(store.directory, "SELECT count(*) FROM launch_spawns") == [(0,)]
    _d, t = a_thread(store, "the fifth")
    assert spawn(store, token, t.thread_id, wdir).state == "pending"       # the refused ones hold no slot


def test_the_stop_time_is_guidance(store, fake, live_hook, wdir, spawn):  # noqa: F811
    _d, t = a_thread(store)
    ln = spawn(store, store.coord_take(), t.thread_id, wdir)
    assert cl.launch_session(store, ln.launch_id).started
    brief = (wdir / ".levain" / "briefs" / f"{ln.launch_id}.md").read_text()
    assert "aim to end your work within 120 minutes" in brief and "nothing stops you" in brief


@pytest.mark.parametrize("bad", ["/etc/passwd", "..", "a/../b", "../a", "a/..", "/"])
def test_a_path_slot_is_relative_with_no_parent_step(bad):
    r = role(slots={"where": "path"}, template="Edit {where}.")
    with pytest.raises(inst.SpawnRefused, match="relative path"):
        inst.render_brief(r, {"where": bad})
    assert inst.render_brief(r, {"where": "docs/a..b.md"}) == "Edit docs/a..b.md."


def test_the_unread_empty_scope_constant_is_gone():
    assert not hasattr(inst, "EMPTY_SCOPE")


# --- fix 3: transient refusals, two done readings and a backstop, the canonical cwd, where's repo, the drop cap, -----
# --- the handoff of a worker that left the listing ------------------------------------------------------------------
def _locked(*_a, **_k):
    raise sqlite3.OperationalError("database is locked")


def _coord_error(*_a, **_k):
    raise CoordError("the store could not be read just now")


def _record_unreadable(*_a, **_k):
    raise door.RecordUnreadable("the desk record is half written")


def _io_error(*_a, **_k):
    raise OSError(5, "Input/output error")


@pytest.mark.parametrize("where, broken", [
    ("door", _record_unreadable), ("door", _io_error),
    ("drop", _io_error), ("drop", _locked), ("thread", _locked)])
def test_a_transient_refusal_leaves_a_spawned_worker_pending(store, fake, live_hook, wdir, spawn, monkeypatch,
                                                            where, broken):  # noqa: F811
    _d, t = a_thread(store)
    ln = spawn(store, store.coord_take(), t.thread_id, wdir)
    with pytest.MonkeyPatch.context() as mp:
        if where == "door":
            mp.setattr(door, "workspace", broken)
        else:
            mp.setattr(store, where, broken)
        why = cl.worker_refusal(ln, store)
        assert why is not None and why.transient, why
        got = cl.launch_session(store, ln.launch_id)
        assert not got.started and "left pending" in got.refusal, got
        lines = cl.watch_once(store)
        assert store.launch(ln.launch_id).state == "pending", lines
        assert any(f"launch {ln.launch_id}" in x and "left pending" in x for x in lines), lines
        assert fake.spawns() == []
    assert cl.launch_session(store, ln.launch_id).started      # the cause passed: the next start goes ahead


def test_every_drop_is_in_a_read_only_file_beside_the_brief(store, fake, live_hook, wdir, spawn,
                                                           monkeypatch):  # noqa: F811
    monkeypatch.setattr(inst, "BRIEF_DROPS_MAX", 2)
    d1, t = a_thread(store, "the first words")
    arrived = [d1]
    for i in range(3):
        d, _ = store.accept_drop(f"more words {i}", surface="cli")
        store.split(d.drop_id, [Part("work", attach=t.thread_id)], by="operator")
        arrived.append(store.drop(d.drop_id))
    ln = spawn(store, store.coord_take(), t.thread_id, wdir)
    assert cl.launch_session(store, ln.launch_id).started
    briefs = wdir / ".levain" / "briefs"
    drops = briefs / f"{ln.launch_id}.drops.md"
    assert drops.is_file() and (drops.stat().st_mode & 0o777) == 0o400
    text = drops.read_text()
    # every drop, oldest to newest, each with its id and its time, the words quoted as data
    at = [text.index(f"`{d.drop_id}` ({d.received_at}): ") for d in arrived]
    assert at == sorted(at), text
    assert all(f"more words {i}" in text for i in range(3)) and "the first words" in text
    brief = (briefs / f"{ln.launch_id}.md").read_text()
    assert str(drops) in brief and "and 2 more on the thread" in brief
    # the file goes with the brief once the launch is over
    store.launch_end_unlisted(ln.launch_id)    # its session left the listing: the launch is over
    lines = cl.watch_once(store)
    assert store.launch(ln.launch_id).state == "ended", lines
    assert not drops.exists() and not (briefs / f"{ln.launch_id}.md").exists(), lines
    assert f"launch {ln.launch_id} ended: its drops file removed" in lines, lines


def test_a_thread_whose_drops_exceed_the_cap_refuses_the_spawn(store, fake, live_hook, wdir, spawn,
                                                               monkeypatch):  # noqa: F811
    assert inst.WORKER_DROPS_MAX_BYTES >= 20 * 20_000          # room for twenty drops of the longest size
    _d, t = a_thread(store, "x" * 900)
    ln = spawn(store, store.coord_take(), t.thread_id, wdir)
    monkeypatch.setattr(inst, "WORKER_DROPS_MAX_BYTES", 800)
    why = cl.worker_refusal(ln, store)
    assert why is not None and not why.transient and "bytes" in why and "800" in why, why
    got = cl.launch_session(store, ln.launch_id)
    assert not got.started and store.launch(ln.launch_id).state == "failed" and fake.spawns() == []
    assert not (wdir / ".levain" / "briefs" / f"{ln.launch_id}.drops.md").exists()


def test_a_huge_thread_is_refused_before_its_whole_drops_file_is_built():
    from types import SimpleNamespace

    def drop(i):
        return SimpleNamespace(drop_id=f"d{i:04d}", received_at="2026-10-31T00:00:00Z", words="é" * 1000)

    whole = inst.drops_file_text("t1", [drop(i) for i in range(3)])
    assert inst.drops_file_text("t1", [drop(i) for i in range(3)], len(whole.encode("utf-8"))) == whole  # at the cap
    with pytest.raises(inst.SpawnRefused):
        inst.drops_file_text("t1", [drop(i) for i in range(3)], len(whole.encode("utf-8")) - 1)
    read = []

    def endless():                   # a thread that never ends: anything read past the crossing drop is a failure
        for i in range(10**9):
            assert len(read) < 6, "a drop was read past the one that crossed the cap"
            read.append(i)
            yield drop(i)

    with pytest.raises(inst.SpawnRefused) as got:
        inst.drops_file_text("t1", endless(), 5000)          # each line is ~2 KiB of UTF-8: the third crosses
    assert not got.value.transient and "5000 bytes" in str(got.value) and "d0002" in str(got.value), got.value
    assert read == [0, 1, 2]


@pytest.mark.parametrize("where", ["drop", "thread"])
def test_a_coord_error_building_the_context_is_permanent(store, fake, live_hook, wdir, spawn, where):  # noqa: F811
    # an unknown id or a malformed thread is no read that failed just now: retrying cannot help, so the launch is
    # written failed and stops holding a spawn slot
    _d, t = a_thread(store)
    ln = spawn(store, store.coord_take(), t.thread_id, wdir)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(store, where, _coord_error)
        with pytest.raises(inst.SpawnRefused) as got:
            inst.worker_context(store, ln)
        assert not got.value.transient
        why = cl.worker_refusal(ln, store)
        assert why is not None and not why.transient, why
        assert not cl.launch_session(store, ln.launch_id).started
    assert store.launch(ln.launch_id).state == "failed" and fake.spawns() == []


def test_a_permanent_refusal_carries_its_class(store, fake, live_hook, wdir, spawn):  # noqa: F811
    _d, t = a_thread(store)
    ln = spawn(store, store.coord_take(), t.thread_id, wdir)
    store.set_state(t.thread_id, "closed", by="operator")
    why = cl.worker_refusal(ln, store)
    assert why is not None and not why.transient
    assert not cl.launch_session(store, ln.launch_id).started
    assert store.launch(ln.launch_id).state == "failed"


def test_a_done_worker_ends_on_the_second_consecutive_done_reading(store, fake, live_hook, wdir, spawn,
                                                                   cfg, clock):  # noqa: F811
    token = store.coord_take()
    _d, t = a_thread(store)
    ln = _activate(store, spawn(store, token, t.thread_id, wdir), "sid-twice")
    _transcript(cfg, "sid-twice", [_ASKED, _said("all done\nDONE: yes")])
    _done_listing(fake, [(ln, "sid-twice")], wdir)
    cl.watch_once(store)
    assert store.launch(ln.launch_id).state == "active"
    # a pass that lists it working breaks the run: the next done reading is a first one again
    fake.others([OTHER, {"id": "w0", "sessionId": "sid-twice", "name": cl.session_name(ln), "cwd": str(wdir),
                         "kind": "background", "state": "working", "status": "busy"}])
    clock.tick(cl.DONE_READ_GAP)
    cl.watch_once(store)
    _done_listing(fake, [(ln, "sid-twice")], wdir)
    clock.tick(cl.DONE_READ_GAP)
    lines = cl.watch_once(store)
    assert store.launch(ln.launch_id).state == "active", lines
    clock.tick(cl.DONE_READ_GAP)
    lines = cl.watch_once(store)
    assert store.launch(ln.launch_id).state == "ended", lines
    assert store.launch_handoff(token, ln.launch_id)["handoff"] == "all done\nDONE: yes"


@pytest.mark.parametrize("stop", ["max_tokens", "refusal", "pause_turn", None])
def test_a_turn_stopped_for_anything_but_a_tool_call_is_finished(cfg, stop):
    _transcript(cfg, "sid-stop", [_ASKED, _said("cut short", stop=stop)])
    finished, handoff, _why = cl.worker_turn(str(cfg), "sid-stop")
    assert finished and handoff == "cut short"
    _transcript(cfg, "sid-tool", [_ASKED, _said("", stop="tool_use", blocks=[{"type": "text", "text": "asking"}])])
    assert cl.worker_turn(str(cfg), "sid-tool")[0] is False


def test_a_worker_waiting_on_a_tool_call_is_never_ended_by_the_backstop(store, fake, live_hook, wdir, spawn, cfg,
                                                                        clock):  # noqa: F811
    token = store.coord_take()
    _d, t = a_thread(store)
    ln = _activate(store, spawn(store, token, t.thread_id, wdir), "sid-stuck")
    _transcript(cfg, "sid-stuck", [_ASKED, _said("", stop="tool_use", blocks=[
        {"type": "text", "text": "I asked to run the tests."}, {"type": "tool_use", "id": "t1", "name": "Bash",
                                                                 "input": {}}])])
    _done_listing(fake, [(ln, "sid-stuck")], wdir)
    for _ in range(3):                         # the consent wait: however long it lasts, it is never ended
        lines = cl.watch_once(store)
        assert store.launch(ln.launch_id).state == "active", lines
        assert any(f"launch {ln.launch_id}: listed done" in x and "never ended by the backstop" in x
                   for x in lines), lines
        clock.tick(cl.UNPROVEN_DONE_SECONDS * 2)
    assert fake.stops() == []


_WAITING = [_ASKED, _said("", stop="tool_use", blocks=[
    {"type": "text", "text": "May I run the tests?"}, {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}])]


def test_a_waiting_worker_listed_done_for_the_ceiling_is_ended_with_why(store, fake, live_hook, wdir, spawn, cfg,
                                                                        qdir, capsys, clock):  # noqa: F811
    assert cl.WAITING_DONE_CEILING_SECONDS == 24 * 60 * 60
    token = store.coord_take()
    _d, t = a_thread(store)
    ln = _activate(store, spawn(store, token, t.thread_id, wdir), "sid-asking")
    _transcript(cfg, "sid-asking", _WAITING)
    _done_listing(fake, [(ln, "sid-asking")], wdir)
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock.now))
    lines = cl.watch_once(store)
    assert store.launch(ln.launch_id).state == "active", lines
    assert any(f"launch {ln.launch_id}:" in x and f"waiting on a tool call or your consent since {started}" in x
               for x in lines), lines
    # the operator reading its handoff sees it waiting, and since when
    with pytest.raises(lease.LeaseRefused, match=f"waiting on a tool call or your consent since {started}"):
        store.launch_handoff(token, ln.launch_id)
    clock.tick(cl.WAITING_DONE_CEILING_SECONDS - 1)
    lines = cl.watch_once(store)
    assert store.launch(ln.launch_id).state == "active", lines
    assert any(f"since {started}" in x for x in lines), lines
    clock.tick(1)
    lines = cl.watch_once(store)
    assert store.launch(ln.launch_id).state == "ended" and len(fake.stops()) == 1, lines
    got = store.launch_handoff(token, ln.launch_id)
    assert got["handoff"] == "May I run the tests?", got
    assert "waited on a tool call for 24 h with its session listed done" in got["why"], got


def test_a_reading_that_is_not_done_and_waiting_restarts_the_ceiling(store, fake, live_hook, wdir, spawn, cfg,
                                                                     clock):  # noqa: F811
    token = store.coord_take()
    _d, t = a_thread(store)
    ln = _activate(store, spawn(store, token, t.thread_id, wdir), "sid-again")
    _transcript(cfg, "sid-again", _WAITING)
    _done_listing(fake, [(ln, "sid-again")], wdir)
    cl.watch_once(store)
    clock.tick(cl.WAITING_DONE_CEILING_SECONDS - 60)
    # listed working for one pass: the session is alive and may answer, so the wait's clock starts again
    fake.others([OTHER, {"id": "w0", "sessionId": "sid-again", "name": cl.session_name(ln), "cwd": str(wdir),
                         "kind": "background", "state": "working", "status": "busy"}])
    cl.watch_once(store)
    _done_listing(fake, [(ln, "sid-again")], wdir)
    clock.tick(cl.WAITING_DONE_CEILING_SECONDS - 60)
    lines = cl.watch_once(store)               # past the ceiling since the first reading, but not since the restart
    assert store.launch(ln.launch_id).state == "active", lines
    # listed done but showing no wait for one pass restarts it too
    _transcript(cfg, "sid-again", [_ASKED, _said("half"), _ASKED])
    lines = cl.watch_once(store)
    assert store.launch(ln.launch_id).state == "active", lines
    _transcript(cfg, "sid-again", _WAITING)
    clock.tick(cl.WAITING_DONE_CEILING_SECONDS - 1)
    lines = cl.watch_once(store)
    assert store.launch(ln.launch_id).state == "active", lines
    clock.tick(cl.WAITING_DONE_CEILING_SECONDS)
    lines = cl.watch_once(store)
    assert store.launch(ln.launch_id).state == "ended", lines


def test_an_ambiguous_done_worker_is_ended_by_elapsed_time_not_pass_count(store, fake, live_hook, wdir, spawn, cfg,
                                                                          clock):  # noqa: F811
    # about an hour: longer than a turn is plausibly left unreadable, short enough that a stuck worker never holds
    # the spawn cap for good; counted in time, so fast passes cannot bring it forward
    assert cl.UNPROVEN_DONE_SECONDS == 60 * 60 and not hasattr(cl, "UNPROVEN_DONE_PASSES")
    token = store.coord_take()
    _d, t = a_thread(store)
    ln = _activate(store, spawn(store, token, t.thread_id, wdir), "sid-murky")
    # a reply followed by a prompt the harness lists done: nothing shows a tool call waiting, nothing shows it finished
    _transcript(cfg, "sid-murky", [_ASKED, _said("half of it"), _ASKED])
    _done_listing(fake, [(ln, "sid-murky")], wdir)
    for _ in range(4):                         # rapid passes: no time has gone by
        lines = cl.watch_once(store)
        assert store.launch(ln.launch_id).state == "active", lines
    clock.tick(cl.UNPROVEN_DONE_SECONDS - 1)
    lines = cl.watch_once(store)
    assert store.launch(ln.launch_id).state == "active", lines
    clock.tick(1)
    lines = cl.watch_once(store)
    assert store.launch(ln.launch_id).state == "ended", lines
    got = store.launch_handoff(token, ln.launch_id)
    assert got["handoff"] == "half of it" and "ended by the backstop" in got["why"] and "3600 s" in got["why"], got
    # an unreadable transcript is the same class
    _d2, t2 = a_thread(store, "the second job")
    lost = _activate(store, spawn(store, token, t2.thread_id, wdir, slots={"topic": "y"}), "sid-lost")
    _done_listing(fake, [(lost, "sid-lost")], wdir)
    cl.watch_once(store)
    clock.tick(cl.UNPROVEN_DONE_SECONDS)
    lines = cl.watch_once(store)
    assert store.launch(lost.launch_id).state == "ended", lines


@pytest.mark.parametrize("records, wait", [
    ([_ASKED, _said("all done\nDONE: yes")], "gap"),               # the spacing stamp
    ([_ASKED, _said("half of it"), _ASKED], "backstop"),           # the backstop's since
    (_WAITING, "ceiling"),                                         # the waiting ceiling's since
])
def test_a_wall_clock_step_back_resets_the_stamp(store, fake, live_hook, wdir, spawn, cfg, clock, records,
                                                 wait):  # noqa: F811
    token = store.coord_take()
    _d, t = a_thread(store)
    ln = _activate(store, spawn(store, token, t.thread_id, wdir), "sid-skew")
    _transcript(cfg, "sid-skew", records)
    _done_listing(fake, [(ln, "sid-skew")], wdir)
    cl.watch_once(store)
    clock.tick(-2 * 60 * 60)                   # the clock steps back two hours: every stamp kept is in the future
    lines = cl.watch_once(store)
    assert store.launch(ln.launch_id).state == "active", lines
    assert not any("too soon" in x or " -" in x for x in lines if ln.launch_id in x), lines
    clock.tick({"gap": cl.DONE_READ_GAP, "backstop": cl.UNPROVEN_DONE_SECONDS,
                "ceiling": cl.WAITING_DONE_CEILING_SECONDS}[wait])
    lines = cl.watch_once(store)               # counted from the reading after the step, not stalled until catch-up
    assert store.launch(ln.launch_id).state == "ended", lines


def test_two_done_readings_must_be_spaced_by_the_minimum_interval(store, fake, live_hook, wdir, spawn, cfg,
                                                                  clock):  # noqa: F811
    assert cl.DONE_READ_GAP > 0
    token = store.coord_take()
    _d, t = a_thread(store)
    ln = _activate(store, spawn(store, token, t.thread_id, wdir), "sid-quick")
    _transcript(cfg, "sid-quick", [_ASKED, _said("all done\nDONE: yes")])
    _done_listing(fake, [(ln, "sid-quick")], wdir)
    cl.watch_once(store)
    clock.tick(cl.DONE_READ_GAP - 1)
    lines = cl.watch_once(store)               # back to back (a daemon pass beside a cron run): not a second reading
    assert store.launch(ln.launch_id).state == "active", lines
    assert any(f"launch {ln.launch_id}:" in x and "too soon" in x for x in lines), lines
    clock.tick(1)
    lines = cl.watch_once(store)
    assert store.launch(ln.launch_id).state == "ended", lines
    # the readings file is replaced whole through a per-process name: no shared tmp file is left behind
    assert [p.name for p in Path(store.directory).iterdir() if p.name.startswith(cl.WORKER_READS)] == [
        cl.WORKER_READS]


def test_the_spawn_runs_in_the_checked_canonical_directory(store, fake, live_hook, wdir, spawn, tmp_path,
                                                          monkeypatch):  # noqa: F811
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = tmp_path / "link"
    link.symlink_to(wdir)
    _d, t = a_thread(store)
    ln = spawn(store, store.coord_take(), t.thread_id, link)
    claim = store.launch_claim_spawn

    def retarget_then_claim(*a, **k):
        link.unlink()
        link.symlink_to(elsewhere)             # the link moves after the check
        return claim(*a, **k)

    monkeypatch.setattr(store, "launch_claim_spawn", retarget_then_claim)
    got = cl.launch_session(store, ln.launch_id)
    assert got.started, got
    (call,) = [c for c in fake.calls() if c["argv"][:1] == ["--bg"]]
    assert call["cwd"] == str(wdir)


def test_where_compares_the_resolved_repo(tmp_path, wdir):
    link = tmp_path / "repo-link"
    link.symlink_to(wdir)
    assert inst.where_refusal(str(wdir), {"where": [{"repo": str(link), "paths": ["."]}]}) is None
    assert inst.where_refusal(str(wdir / "x"), {"where": [{"repo": str(link), "paths": ["x/"]}]}) is None
    assert inst.where_refusal(str(wdir), {"where": [{"repo": "/", "paths": ["."]}]}) is None
    assert inst.where_refusal(str(tmp_path), {"where": [{"repo": str(link), "paths": ["."]}]}) is not None


def test_the_drops_briefed_are_capped(store, fake, live_hook, wdir, spawn, monkeypatch):  # noqa: F811
    monkeypatch.setattr(inst, "BRIEF_DROPS_MAX", 2)
    d1, t = a_thread(store, "the first words")
    arrived = [d1.drop_id]
    for i in range(3):
        d, _ = store.accept_drop(f"more words {i}", surface="cli")
        store.split(d.drop_id, [Part("work", attach=t.thread_id)], by="operator")
        arrived.append(d.drop_id)
    ln = spawn(store, store.coord_take(), t.thread_id, wdir)
    got = inst.worker_context(store, ln)
    own = lease.spawn_record(ln.role_ref)["drop_id"]
    others = [i for i in arrived if i != own]
    # the record's own drop first, then the newest of the others: the operator's latest words
    assert [i for i, _w in got.drops] == [own, others[-1]] and got.unbriefed == 2
    assert got.spec["input"]["source"].endswith("and 2 more on the thread")
    assert cl.launch_session(store, ln.launch_id).started
    brief = (wdir / ".levain" / "briefs" / f"{ln.launch_id}.md").read_text()
    assert "and 2 more on the thread" in brief and f"`{others[-1]}`" in brief and f"`{others[0]}`" not in brief


def test_a_worker_that_left_the_listing_keeps_its_final_message(store, fake, live_hook, wdir, spawn,
                                                               cfg):  # noqa: F811
    token = store.coord_take()
    _d, t = a_thread(store)
    ln = _activate(store, spawn(store, token, t.thread_id, wdir), "sid-left")
    _transcript(cfg, "sid-left", [_ASKED, _said("half of it\nDONE: no")])
    _d2, t2 = a_thread(store, "the second job")
    mute = _activate(store, spawn(store, token, t2.thread_id, wdir, slots={"topic": "y"}), "sid-mute")
    fake.others([OTHER])                       # neither is listed: both left before a done reading
    lines = cl.watch_once(store)
    assert store.launch(ln.launch_id).state == "ended" and store.launch(ln.launch_id).cause == "gone", lines
    assert store.launch_handoff(token, ln.launch_id)["handoff"] == "half of it\nDONE: no"
    with pytest.raises(lease.LeaseRefused, match="left no handoff"):
        store.launch_handoff(token, mute.launch_id)


# --- fix 4: one directory after the check, the backstop by elapsed time, every drop in a file, the error classes, ---
# --- spaced done readings -------------------------------------------------------------------------------------------
def test_a_link_retargeted_after_the_check_cannot_split_the_hook_check_from_the_spawn(store, fake, wdir, spawn,
                                                                                     tmp_path, monkeypatch):  # noqa: F811
    hooked = tmp_path / "hooked"
    hooked.mkdir()
    link = tmp_path / "link"
    link.symlink_to(wdir)
    _d, t = a_thread(store)
    ln = spawn(store, store.coord_take(), t.thread_id, link)
    seen = []

    def hook(d, **_kw):
        seen.append(str(d))
        return (Path(d).resolve() == hooked.resolve(), "wired to this install")

    monkeypatch.setattr(cl, "hook_live", hook)
    check = cl._worker_check

    def check_then_retarget(*a, **k):
        got = check(*a, **k)
        link.unlink()
        link.symlink_to(hooked)                # the link moves to a directory with a live hook after the check
        return got

    monkeypatch.setattr(cl, "_worker_check", check_then_retarget)
    got = cl.launch_session(store, ln.launch_id)
    # the hook is checked in the directory the checks resolved (no live hook there), so nothing starts, and nothing
    # is made in the directory the link now names
    assert seen == [str(wdir)], seen
    assert not got.started and "consent hook is not live" in got.refusal, got
    assert fake.spawns() == [] and not (hooked / ".levain").exists()
    # with the hook live there, every step runs in the checked directory: the brief, the prompt and the spawn
    link.unlink()
    link.symlink_to(wdir)
    monkeypatch.setattr(cl, "hook_live", lambda d, **_kw: (seen.append(str(d)) or True, "wired to this install"))
    seen.clear()
    got = cl.launch_session(store, ln.launch_id)
    assert got.started, got
    (call,) = [c for c in fake.calls() if c["argv"][:1] == ["--bg"]]
    assert call["cwd"] == str(wdir) and seen == [str(wdir)]
    brief = wdir / ".levain" / "briefs" / f"{ln.launch_id}.md"
    assert brief.is_file() and str(brief) in call["argv"][-1] and not (hooked / ".levain").exists()
    assert f"Your directory is \"{wdir}\"" in brief.read_text()


def test_a_checked_directory_that_moved_after_the_claim_fails_by_the_defined_fail(store, fake, live_hook, wdir, spawn,
                                                                                  tmp_path, monkeypatch):  # noqa: F811
    _d, t = a_thread(store)
    ln = spawn(store, store.coord_take(), t.thread_id, wdir)
    claim = store.launch_claim_spawn
    moved = tmp_path / "moved"
    moved.mkdir()

    def swap_then_claim(*a, **k):
        got = claim(*a, **k)
        wdir.rename(tmp_path / "gone")
        wdir.symlink_to(moved)                 # the checked path itself became a link after the claim
        return got

    monkeypatch.setattr(store, "launch_claim_spawn", swap_then_claim)
    got = cl.launch_session(store, ln.launch_id)
    assert not got.started and "now resolves to" in got.refusal, got
    assert store.launch(ln.launch_id).state == "failed" and fake.spawns() == []
