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
        self.wiring = AgentsWiring(store_dir=tmp / "s", cockpit_root=self.rig.world.root if cockpit_root else None,
                                   signing=lambda: self.rig.world.keys["laptop"], clock=lambda: self.rig.now())
        self.ck = Cockpit(entity=lambda ctx: {"name": "t"})
        self.ck.discover(agents_discoverer(lambda: self.wiring), retire=True)
        self.ck.agents_wiring = self.wiring
        self.reg = VerbRegistry()
        self.ck.attach_verbs(VerbView(self.reg, self.ck, REPORTED))
        self.ck.manifest(CLOSED)                     # discovery runs on a manifest, as in the server

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
    ck.discover(agents_discoverer(lambda: AgentsWiring(store_dir=empty)), retire=True)
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
    world.ck.agents_wiring = world.wiring
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
