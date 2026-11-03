"""P9a: the agents panel's backend (levain.cockpit.agents_panel and its four kernel verbs).

Every store is the real agent DDL and every authority log is real (ssh-keygen keys, links verified through the replay:
the ``Rig`` of the launch tests). Each case was first run as a script against this rig (/tmp/p9a/run.py in the lane).
"""
from __future__ import annotations

import copy
import shutil
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from levain.agents.role import role_hash
from levain.agents.spec import parse_spec, register_spec, retire_agent, set_operation
from levain.cockpit.agents_panel import AGENT_VERBS, AGENTS_ID, AgentsWiring, agents_discoverer
from levain.cockpit.engine import Cockpit
from levain.cockpit.firejournal import CockpitDB
from levain.cockpit.install import REPORTED
from levain.cockpit.results import Absent
from levain.cockpit.verbs import KERNEL_VERBS, VerbRegistry, VerbView, dispatch
from levain.writes import EditError

pytest.importorskip("cryptography")
from tests.test_agents_launch import BINDING, ROLES, SPECS, Rig  # noqa: E402
from tests.test_authority_agent_link import link as link_record  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

CRED = {"class": "token", "name": "browser", "device_class": None}
CLOSED = {"class": "none", "device_class": None}


def _spec(name: str) -> dict:
    d = copy.deepcopy(SPECS["job_push_check"])
    d["name"] = name
    d["role"]["role_hash"] = role_hash(ROLES[d["role"]["name"]])
    return parse_spec(d)


class World:
    """One agent per group on a scratch store: ``firing`` has a live link, ``paused`` is paused, ``unarmed`` has no link
    and ``retired`` is retired. (The rig's own agent, ``job_push_check``, is a second unarmed one.)"""

    def __init__(self, tmp: Path, *, cockpit_root: bool = True) -> None:
        self.rig = Rig(tmp)
        CockpitDB(self.rig.world.root).set_install_class("reported")     # the signing entry opens a reported journal
        self.ids = {}
        self.hashes = {}
        for name in ("firing", "paused", "unarmed", "retired"):
            self.ids[name], self.hashes[name], *_ = register_spec(self.rig.store, _spec("w-" + name), "America/New_York")
        self.rig.world.link({"op": "create_grant", "grant": link_record(
            self.rig.world, 1, agent=self.ids["firing"], store=self.rig.sid, spec_hash=self.hashes["firing"],
            binding_id=BINDING)})
        set_operation(self.rig.store, self.ids["paused"], "paused")
        retire_agent(self.rig.store, self.ids["retired"])
        self.ck = Cockpit(entity=lambda ctx: {"name": "t"})
        self.wiring = AgentsWiring(store_dir=tmp / "s", cockpit_root=self.rig.world.root if cockpit_root else None,
                                   signing=lambda: self.rig.world.keys["laptop"], clock=lambda: self.rig.now())
        self.ck.discover(agents_discoverer(self.ck), retire=True)
        self.reg = VerbRegistry()
        self.ck.attach_verbs(VerbView(self.reg, self.ck, REPORTED))
        self.ck.manifest(CLOSED)                     # discovery runs on a manifest, as in the server

    @property
    def wiring(self) -> AgentsWiring:
        return self.ck.agents_wiring

    @wiring.setter
    def wiring(self, value: AgentsWiring) -> None:
        self.ck.agents_wiring = value            # the one place the panel and the verbs both read it

    def panel(self) -> dict:
        self.ck.manifest(CLOSED)
        return self.ck.panel(AGENTS_ID)

    def rid(self, name: str) -> str:
        return "agent:" + self.ids[name]

    def group_of(self, name: str) -> str:
        return self.ck.read_one(AGENTS_ID, self.rid(name)).value["facets"]["agent_group"]

    def post(self, verb: str, name: str, params: dict | None = None, **env) -> dict:
        row = self.ck.read_one(AGENTS_ID, self.rid(name)).value
        req = {"verb": verb, "panel_id": AGENTS_ID, "row_id": row["id"], "row_version": row["version"],
               "params": params or {}}
        req.update(env)
        return dispatch(registry=self.reg, cockpit=self.ck, scope=object(), req=req, credential=CRED,
                        install_class=REPORTED, route="cockpit")


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


def _groups(panel: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for r in panel["rows"]:
        out.setdefault(r["group"], []).append(r["id"])
    return out


def test_rows_group_firing_paused_unarmed_retired_and_an_unreadable_log_is_never_health(world, tmp_path):
    # behaviour: one agent per group lands in its group, in the group order, and only the retired one is uncounted
    panel = world.panel()
    assert [g["id"] for g in panel["groups"]] == ["firing", "paused", "unarmed", "retired"]
    got = _groups(panel)
    assert got["firing"] == [world.rid("firing")] and got["paused"] == [world.rid("paused")]
    assert got["retired"] == [world.rid("retired")]
    assert set(got["unarmed"]) == {world.rid("unarmed"), "agent:" + world.rig.agent}     # the launch rig's agent too
    assert panel["count"] == 4 and next(g for g in panel["groups"] if g["id"] == "retired").get("uncounted")
    bodies = {r["id"]: r["body"] for r in panel["rows"]}
    assert "armed · link until " in bodies[world.rid("firing")]
    assert "no signed link covers this spec" in bodies[world.rid("unarmed")]
    # an expired link is Unarmed, and says it expired (the clock moves past the 29 days)
    world.wiring = AgentsWiring(store_dir=world.wiring.store_dir, cockpit_root=world.wiring.cockpit_root,
                                clock=lambda: world.rig.now(timedelta(days=40)))
    assert world.group_of("firing") == "unarmed"
    assert "its link has expired" in world.ck.read_one(AGENTS_ID, world.rid("firing")).value["body"]
    # a cockpit that cannot read the authority log shows NO agent as firing, and says why in the note
    (tmp_path / "blind").mkdir()
    blind = World(tmp_path / "blind", cockpit_root=False)
    panel = blind.panel()
    assert "firing" not in _groups(panel) and "Arming is unknown" in panel["note"]
    assert "arming unknown" in {r["id"]: r["body"] for r in panel["rows"]}[blind.rid("firing")]


def test_read_one_reads_the_source_for_one_agent_and_its_version_moves_with_its_state(world):
    # behaviour: read_one answers the one row (retired included), an unknown or foreign id is absent, and a pause
    # moves the row's version (so a verb bound to the old render is refused as stale)
    for name in ("firing", "paused", "unarmed", "retired"):
        res = world.ck.read_one(AGENTS_ID, world.rid(name))
        assert res.value["id"] == world.rid(name) and res.value["facets"]["agent_group"] == name
    assert isinstance(world.ck.read_one(AGENTS_ID, "agent:ag-" + "0" * 32), Absent)
    assert isinstance(world.ck.read_one(AGENTS_ID, "review:x"), Absent)
    drawn = {r["id"]: r["version"] for r in world.panel()["rows"]}
    assert world.ck.read_one(AGENTS_ID, world.rid("firing")).value["version"] == drawn[world.rid("firing")]
    stale = world.ck.read_one(AGENTS_ID, world.rid("firing")).value["version"]
    set_operation(world.rig.store, world.ids["firing"], "paused")
    assert world.ck.read_one(AGENTS_ID, world.rid("firing")).value["version"] != stale
    with pytest.raises(EditError) as ei:
        world.post("agent_resume", "firing", row_version=stale)
    assert ei.value.code == "stale"


def _snapshot(world: World) -> tuple:
    """Everything the panel could write: every agent-store table's rows, the authority log and its anchor."""
    conn = sqlite3.connect(world.wiring.store_dir / "autonomic.db")
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        store = [(t, conn.execute(f'SELECT * FROM "{t}"').fetchall()) for t in tables]
    finally:
        conn.close()
    log = sqlite3.connect(world.rig.world.db)
    try:
        authority = log.execute("SELECT seq, record FROM authority_log ORDER BY seq").fetchall()
    finally:
        log.close()
    anchors = sorted((p.name, p.read_bytes()) for p in world.rig.world.root.iterdir() if p.is_file()
                     and p.suffix not in (".db", ".sqlite", "") and "-wal" not in p.name and "-shm" not in p.name)
    return store, authority, anchors


def test_the_panel_never_writes_and_a_missing_store_draws_nothing_and_makes_nothing(world, tmp_path):
    # behaviour: reading the panel, every row and its manifest changes no store row, no log record and creates no file
    before = _snapshot(world)
    world.panel()
    for name in ("firing", "paused", "unarmed", "retired"):
        world.ck.read_one(AGENTS_ID, world.rid(name))
    world.ck.read_one(AGENTS_ID, "agent:ag-" + "0" * 32)
    assert _snapshot(world) == before
    # an install that never used agents draws no panel and the read creates no store
    empty = tmp_path / "never"
    empty.mkdir()
    ck = Cockpit(entity=lambda ctx: {"name": "t"})
    ck.agents_wiring = AgentsWiring(store_dir=empty)
    ck.discover(agents_discoverer(ck), retire=True)
    ids = [p for z in ck.manifest(CLOSED)["regions"]["zones"] for p in z["panels"]]
    assert AGENTS_ID not in ids and list(empty.iterdir()) == []


def test_each_verb_is_a_kernel_verbspec_calling_its_owner_and_the_signing_entry_makes_the_renew(world, monkeypatch):
    # behaviour: pause, resume, retire and renew are VerbSpecs the panel offers; each reaches the owner of its act in
    # levain.agents (spied once, then run for real through the dispatch the server uses), and `revoke` is not here
    specs = {v.name: v for v in KERNEL_VERBS if v.name in AGENT_VERBS}
    assert set(specs) == set(AGENT_VERBS) == {"agent_pause", "agent_resume", "agent_renew", "agent_retire"}
    assert all(v.route == "cockpit" and v.binding == "row" and v.floor == "C1" for v in specs.values())
    assert not any("revoke" in v.name for v in KERNEL_VERBS if v.name.startswith("agent"))
    import levain.agents.signing as signing
    import levain.agents.spec as spec_mod

    calls = []
    for owner, mod in (("set_operation", spec_mod), ("retire_agent", spec_mod), ("renew_agent_link", signing)):
        real = getattr(mod, owner)
        monkeypatch.setattr(mod, owner, lambda *a, _o=owner, _r=real, **k: (calls.append(_o), _r(*a, **k))[1])
    # pause -> Paused; resume -> Firing again (the link is untouched)
    assert world.post("agent_pause", "firing")["operation"] == "paused" and world.group_of("firing") == "paused"
    assert world.post("agent_resume", "firing")["operation"] == "active" and world.group_of("firing") == "firing"
    # a verb the row is not offered is refused by the kernel (resume on a live agent)
    with pytest.raises(EditError):
        world.post("agent_resume", "firing")
    # renew moves the link's expiry through the signing entry on the cockpit's laptop-presence key
    world.rig.world.clock.now += timedelta(days=1)
    out = world.post("agent_renew", "firing", {"days": 29}, confirm=True)
    assert out["link"] == "al-000000000001" and out["expires_at"] > "2026-11-"
    assert f"link until {out['expires_at']}" in world.ck.read_one(AGENTS_ID, world.rid("firing")).value["body"]
    # no signing key held: refused, 501, nothing appended
    world.wiring = AgentsWiring(store_dir=world.wiring.store_dir, cockpit_root=world.wiring.cockpit_root,
                                clock=world.wiring.clock)
    with pytest.raises(EditError) as ei:
        world.post("agent_renew", "firing", {"days": 29}, confirm=True)
    assert ei.value.code == "not_built"
    # retire is confirmed and final: the row leaves its group for Retired
    with pytest.raises(EditError) as ei:
        world.post("agent_retire", "unarmed")
    assert ei.value.code != "stale" and world.group_of("unarmed") == "unarmed"          # unconfirmed: nothing happened
    assert world.post("agent_retire", "unarmed", confirm=True)["registration"] == "retired"
    assert world.group_of("unarmed") == "retired"
    assert calls == ["set_operation", "set_operation", "renew_agent_link", "retire_agent"]


# --- L3 r1 fixes: each case below was first reproduced against the r1 tree (/tmp/p9a_fix1/repro.py in the lane) ---

def _revise(world: World, name: str) -> int:
    from levain.agents.spec import get_agent

    spec = copy.deepcopy(_spec("w-" + name))
    spec["trigger"]["cron"] = f"{10 + get_agent(world.rig.store, world.ids[name])['current_version']} 4 * * *"
    return register_spec(world.rig.store, spec, "America/New_York", revise=True)[2]


def _race(world: World, monkeypatch, act) -> None:
    """Run ``act`` after the dispatch has matched the row to its render and before the handler writes: what a second
    process landing in that gap does."""
    import levain.cockpit.verbs as verbs_mod

    real = verbs_mod._agent_target

    def racing(target, params, allowed=()):
        out = real(target, params, allowed)
        act(target)
        return out
    monkeypatch.setattr(verbs_mod, "_agent_target", racing)


def _log_seqs(world: World) -> list[int]:
    log = sqlite3.connect(world.rig.world.db)
    try:
        return [r[0] for r in log.execute("SELECT seq FROM authority_log ORDER BY seq")]
    finally:
        log.close()


def test_the_panel_and_the_verbs_read_one_wiring_and_a_verb_with_none_refuses(world, tmp_path, monkeypatch):
    # behaviour (codex H1, deepseek H2): r1 drew the panel from store A and retired the same agent id in the levain
    # home's default store B when the cockpit's own attribute was unset. Now a cockpit with no agents wiring draws no
    # agents panel, and an agent verb handed no wiring refuses and touches no store
    from levain.agents.spec import get_agent
    from levain.agents.store import open_agent_store
    import levain.cockpit.verbs as verbs_mod

    home = tmp_path / "homeB"
    shutil.copytree(world.wiring.store_dir, home / "autonomic")
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    row = world.ck.read_one(AGENTS_ID, world.rid("unarmed")).value
    world.ck.agents_wiring = None
    ids = [p for z in world.ck.manifest(CLOSED)["regions"]["zones"] for p in z["panels"]]
    assert AGENTS_ID not in ids
    for fire in (verbs_mod._agent_retire_fire, verbs_mod._agent_operation_fire("paused")):
        with pytest.raises(EditError) as ei:
            fire(object(), {}, {"row": row, "agents": None}, True)
        assert ei.value.code == "not_built"
    assert get_agent(open_agent_store(home / "autonomic"), world.ids["unarmed"])["registration"] == "registered"


def test_retire_and_pause_check_the_version_the_row_showed_inside_their_write(world, monkeypatch):
    # behaviour (codex H2): r1 retired version 2 of an agent whose version 1 the operator confirmed, when a revise
    # landed between the dispatch's row check and the write. Now the write refuses as stale and changes nothing
    from levain.agents.spec import get_agent

    _race(world, monkeypatch, lambda target: _revise(world, "firing"))
    with pytest.raises(EditError) as ei:
        world.post("agent_retire", "firing", confirm=True)
    assert ei.value.code == "stale"
    agent = get_agent(world.rig.store, world.ids["firing"])
    assert agent["registration"] == "registered" and agent["current_version"] == 2
    with pytest.raises(EditError) as ei:
        world.post("agent_pause", "firing")              # the race revises again: version 3 under a version-2 row
    assert ei.value.code == "stale" and get_agent(world.rig.store, world.ids["firing"])["operation"] == "active"


@pytest.mark.parametrize("what", ["revise", "retire"])
def test_renew_refuses_when_the_agent_moved_after_the_row_check_and_appends_nothing(world, monkeypatch, what):
    # behaviour (codex H3): r1 renewed the link of a spec that a revise had just superseded and said it renewed the
    # agent. Now a revise or a retire landing after the dispatch's row check refuses before the signature
    world.rig.world.clock.now += timedelta(days=1)
    before = _log_seqs(world)
    act = {"revise": lambda target: _revise(world, "firing"),
           "retire": lambda target: retire_agent(world.rig.store, world.ids["firing"])}[what]
    _race(world, monkeypatch, act)
    with pytest.raises(EditError) as ei:
        world.post("agent_renew", "firing", {"days": 29}, confirm=True)
    assert ei.value.code == "stale" and "nothing was signed" in str(ei.value)
    assert _log_seqs(world) == before


def test_renew_refuses_a_link_id_that_names_another_agents_link(world, monkeypatch):
    world.rig.world.clock.now += timedelta(days=1)
    world.rig.n = 1                                           # World signed al-000000000001 itself
    other = world.rig.link()                                  # a link for the rig's own agent, not w-firing
    before = _log_seqs(world)
    _race(world, monkeypatch, lambda target: target["row"]["facets"].__setitem__("agent_link", other))
    with pytest.raises(EditError) as ei:
        world.post("agent_renew", "firing", {"days": 29}, confirm=True)
    assert ei.value.code == "stale" and "does not cover" in str(ei.value) and _log_seqs(world) == before


def test_a_store_error_while_renewing_is_source_unavailable_not_a_refusal(world, monkeypatch):
    # behaviour (codex L5): r1 answered a locked or broken journal as a policy refusal (409)
    import levain.agents.signing as signing

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(signing, "renew_agent_link", locked)
    with pytest.raises(EditError) as ei:
        world.post("agent_renew", "firing", {"days": 29}, confirm=True)
    assert ei.value.code == "source_unavailable"


def test_a_pause_wins_over_an_unreadable_log_and_one_bad_judgement_is_one_row(tmp_path, monkeypatch):
    # behaviour (codex M4, complement M1, deepseek M4): r1 drew a paused agent as Unarmed when the log could not be
    # read, so resume was not offered; and any error but AgentAuthorityError faulted the whole panel
    blind = World(tmp_path, cockpit_root=False)
    row = blind.ck.read_one(AGENTS_ID, blind.rid("paused")).value
    assert row["facets"]["agent_group"] == "paused" and row["facets"]["agent_link"] == ""
    assert blind.post("agent_resume", "paused")["operation"] == "active"
    from levain.agent_authority import AgentAuthority

    (tmp_path / "seeing").mkdir()
    seeing = World(tmp_path / "seeing")
    monkeypatch.setattr(AgentAuthority, "peek", lambda self, *a, **k: (_ for _ in ()).throw(KeyError("spec_hash")))
    panel = seeing.panel()
    bodies = {r["id"]: r for r in panel["rows"]}
    assert "firing" not in _groups(panel)
    assert "arming unknown" in bodies[seeing.rid("firing")]["body"]
    assert bodies[seeing.rid("firing")]["facets"]["agent_link"] == ""
    assert bodies[seeing.rid("paused")]["group"] == "paused"
