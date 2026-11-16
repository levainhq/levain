"""P9a: the agents panel's backend (levain.cockpit.agents_panel and its four kernel verbs).

Every store is the real agent DDL and every authority log is real (ssh-keygen keys, links verified through the replay:
the ``Rig`` of the launch tests). Each case was first run as a script against this rig (/tmp/p9a/run.py in the lane).
"""
from __future__ import annotations

import copy
import re
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
    assert [g["id"] for g in panel["groups"]] == ["operator", "firing", "paused", "unarmed", "retired"]   # P9a-o's pinned group first
    got = _groups(panel)
    assert got["operator"] == ["operator-agent:none"]                                     # a store with no record, uncounted
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
    # levain.agents (spied once, then run for real through the dispatch the server uses); revoke has its own tests (its undo is a new link
    # through `levain agents sign`, not a verb)
    specs = {v.name: v for v in KERNEL_VERBS if v.name in AGENT_VERBS}
    assert set(specs) == set(AGENT_VERBS) == {"agent_pause", "agent_resume", "agent_renew", "agent_revoke",
                                              "agent_retire"}
    assert all(v.route == "cockpit" and v.binding == "row" and v.floor == "C1" for v in specs.values())
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


# --- P9a-k: the kind facet. Each case was first run as a script against this rig ($CLAUDE_JOB_DIR/tmp/p9ak/run.py) ---

def _kind_role(name: str, kind: str | None = None) -> dict:
    role = copy.deepcopy(ROLES["notify-role"])
    role["name"] = name
    if kind is not None:
        role["kind"] = kind
    return role


def _kind_agent(world: World, name: str, role: dict) -> str:
    """Register ``role`` and one agent ``w-<name>`` pinning it; return the agent id."""
    from levain.agents.role import add_role

    add_role(world.rig.store, role)
    d = copy.deepcopy(SPECS["job_push_check"])
    d["name"] = "w-" + name
    d["role"] = {"name": role["name"], "role_hash": role_hash(role)}
    return register_spec(world.rig.store, parse_spec(d), "America/New_York")[0]


def _kind_rows(world: World) -> dict[str, dict]:
    return {r["id"]: r for r in world.panel()["rows"]}


def test_each_row_carries_the_kind_of_the_role_its_spec_pins(world):
    # behaviour: a job role and a role role (named or by default) read as such, on the panel and on read_one alike;
    # `"kind": "role"` is the same role as none, so the two share a row's kind and a hash
    job = _kind_agent(world, "kjob", _kind_role("kjob-role", "job"))
    plain = _kind_agent(world, "kplain", _kind_role("kplain-role"))
    named = _kind_agent(world, "knamed", _kind_role("knamed-role", "role"))
    rows = _kind_rows(world)
    assert [rows["agent:" + a]["facets"]["agent_kind"] for a in (job, plain, named)] == ["job", "role", "role"]
    for a, want in ((job, "job"), (plain, "role"), (named, "role")):
        assert world.ck.read_one(AGENTS_ID, "agent:" + a).value["facets"]["agent_kind"] == want
    # every row has the facet: the retired and the paused ones too
    # (the operator agent's row is the record, not a registered agent, and carries no kind: it is the pinned row, P9a-o)
    assert all("agent_kind" in r["facets"] for r in rows.values() if not r["id"].startswith("operator-agent:"))


def test_the_kind_facet_accepts_every_kind_a_role_can_have_and_unreadable():
    # behaviour: the registry's vocabulary is derived from the role parser's, so a kind the parser learns (K2's
    # `agent`) cannot reach the panel and drop its row as an ill-typed facet; this goes red until the registry follows
    from levain.agents.role import ROLE_KINDS
    from levain.cockpit.registry import validate_facets

    for kind in (*ROLE_KINDS, "unreadable"):
        validate_facets({"agent_kind": kind})


def _damage(world: World, role: dict, how: str) -> None:
    import json

    from levain.agents.role import canonical_role

    digest = role_hash(role)
    text = {"garbage": "{not json",
            "another_roles_text": canonical_role(_kind_role("else")),
            "kind_role_written_out": json.dumps({**role, "kind": "role"}, sort_keys=True, separators=(",", ":")),
            "kind_agent": json.dumps({**role, "kind": "agent"}, sort_keys=True, separators=(",", ":")),
            "a_blob": b"\x00\x01"}.get(how)
    conn = sqlite3.connect(world.wiring.store_dir / "autonomic.db")
    try:
        if how == "row_gone":
            conn.execute("DELETE FROM agent_roles WHERE role_hash = ?", (digest,))
        else:
            conn.execute("UPDATE agent_roles SET spec = ? WHERE role_hash = ?", (text, digest))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("how", ["garbage", "another_roles_text", "kind_role_written_out", "kind_agent", "a_blob",
                                 "row_gone"])
def test_a_role_row_that_fails_its_check_reads_unreadable_never_absent_and_is_one_row(world, how):
    # behaviour: the module's rule (an authority log that cannot be read is NEVER read as health) held for the role:
    # a pinned role whose row is unparsable, does not hash to its key, is not the canonical text (K1: a written-out
    # `"kind": "role"`), carries the refused `agent` kind, is not text, or is gone, reads "unreadable" and says so
    # on the row; its neighbours keep their kinds and the panel does not fault
    role = _kind_role("kbad-role", "job")
    bad = _kind_agent(world, "kbad", role)
    good = _kind_agent(world, "kgood", _kind_role("kgood-role", "job"))
    _damage(world, role, how)
    before = _snapshot(world)
    panel = world.panel()
    rows = {r["id"]: r for r in panel["rows"]}
    assert rows["agent:" + bad]["facets"]["agent_kind"] == "unreadable"
    assert "role unreadable" in rows["agent:" + bad]["body"] and rows["agent:" + bad]["group"] in _groups(panel)
    assert rows["agent:" + good]["facets"]["agent_kind"] == "job"
    assert world.ck.read_one(AGENTS_ID, "agent:" + bad).value["facets"]["agent_kind"] == "unreadable"
    assert _snapshot(world) == before                         # reading a bad role row repairs and writes nothing


# --- P9a-o: the operator agent's pinned row. Each case was first run against this rig and is red on the tree before
# the row existed (the RETURN carries the red output) ---

OPERATOR_PREFIX = "operator-agent:"


def _dream(world: World) -> str:
    """Register the dream the way ``levain dream enable`` does (attached); returns its ``ag-`` id."""
    from levain.agents.store import set_default_zone
    from levain.dream import agent as dream_agent

    set_default_zone(world.rig.store, "America/New_York")
    return dream_agent.register_dream(world.rig.store).agent_id


def _dream_unattached(world: World) -> str:
    """The dream as an install that registered it before K2b has it: the role and the spec, no attachment."""
    from levain.agents import role as role_mod
    from levain.agents.store import set_default_zone
    from levain.dream import agent as dream_agent

    set_default_zone(world.rig.store, "America/New_York")
    role = role_mod.parse_role(dream_agent.dream_role())
    role_mod.add_role(world.rig.store, role)
    parsed = parse_spec(dream_agent.dream_spec(role_mod.role_hash(role)))
    return register_spec(world.rig.store, parsed, "America/New_York")[0]


def _operator_rows(panel: dict) -> list[dict]:
    return [r for r in panel["rows"] if r["id"].startswith(OPERATOR_PREFIX)]


def test_the_dream_attached_draws_the_operator_agents_row_with_its_role_and_the_others_keep_their_groups(world):
    # behaviour: an attached dream gives ONE row `operator-agent:<oa-id>` pinned first in its own uncounted group, naming
    # the role, its operation and the install facts (none in this store); the existing agents keep their groups and the
    # count is the registered agents' (the record is not one)
    dream = _dream(world)
    panel = world.panel()
    ops = _operator_rows(panel)
    assert len(ops) == 1 and re.fullmatch(r"operator-agent:oa-[0-9a-f]{32}", ops[0]["id"])
    row = ops[0]
    assert row["group"] == "operator" and row["facets"]["agent_group"] == "operator"
    assert "dream" in row["body"] and "active" in row["body"] and "no install facts" in row["body"]
    assert "not attached" not in row["body"]
    assert row["body"].startswith("Operator agent oa-") and "roles: dream v1 as role dream v1 (active)" in row["body"]
    assert [g["id"] for g in panel["groups"]] == ["operator", "firing", "paused", "unarmed", "retired"]
    assert next(g for g in panel["groups"] if g["id"] == "operator").get("uncounted")
    assert panel["count"] == 5 and panel["rows"][0]["id"] == row["id"]       # the four, plus the dream: the record is no agent
    got = _groups(panel)
    assert got["firing"] == [world.rid("firing")] and got["retired"] == [world.rid("retired")]
    # the dream is also an ordinary agent row, and read_one answers the operator row by its id
    assert "agent:" + dream in {r["id"] for r in panel["rows"]}
    assert world.ck.read_one(AGENTS_ID, row["id"]).value["id"] == row["id"]
    assert isinstance(world.ck.read_one(AGENTS_ID, OPERATOR_PREFIX + "oa-" + "0" * 32), Absent)


def test_a_store_with_no_operator_record_draws_the_fixed_none_row(world):
    # behaviour: no record -> `operator-agent:none`, whatever the other agents are; it is not a count
    panel = world.panel()
    assert [r["id"] for r in _operator_rows(panel)] == ["operator-agent:none"]
    row = _operator_rows(panel)[0]
    assert row["body"] == "no operator agent in this store yet"
    assert panel["count"] == 4
    assert world.ck.read_one(AGENTS_ID, "operator-agent:none").value["id"] == "operator-agent:none"


def test_a_pre_k2b_dream_with_no_record_shows_dream_unattached_on_the_none_row(world):
    # behaviour: a dream registered before K2b has no attachment and the store has no record: the none row says so, as a
    # fact (the id of the dream's spec), never as health
    dream = _dream_unattached(world)
    row = _operator_rows(world.panel())[0]
    assert row["id"] == "operator-agent:none" and row["body"].startswith("no operator agent in this store yet")
    assert f"the dream ({dream}) is registered but not attached" in row["body"]


def test_an_unattached_dream_beside_a_record_made_by_another_role_is_shown_on_the_record_row(world):
    # behaviour: a record exists (another role attached) and the dream is not attached: the record's row carries it
    from levain.agents.operator import attach

    other = _kind_agent(world, "partner", {**_kind_role("partner-role", "role"), "executor": "dream"})
    attach(world.rig.store, other)
    dream = _dream_unattached(world)
    row = _operator_rows(world.panel())[0]
    assert row["id"].startswith(OPERATOR_PREFIX + "oa-") and row["body"].startswith("Operator agent oa-")
    assert f"the dream ({dream}) is registered but not attached" in row["body"]


def test_a_corrupt_attached_role_row_is_an_unreadable_operator_row_and_the_rest_of_the_panel_still_reads(world):
    # behaviour: the attached dream's role row fails its check: ONE `unreadable` row with the reason, NOT a whole-panel
    # fault; every other agent keeps its group (the dream's own agent row says its role is unreadable), nothing written
    from levain.agents import role as role_mod
    from levain.dream import agent as dream_agent

    dream = _dream(world)
    digest = role_mod.role_hash(role_mod.parse_role(dream_agent.dream_role()))
    conn = sqlite3.connect(world.wiring.store_dir / "autonomic.db")
    try:
        conn.execute("UPDATE agent_roles SET spec = ? WHERE role_hash = ?", ("{not json", digest))
        conn.commit()
    finally:
        conn.close()
    before = _snapshot(world)
    panel = world.panel()
    assert "fault" not in panel or not panel.get("fault")
    ops = _operator_rows(panel)
    assert len(ops) == 1 and ops[0]["id"] == "operator-agent:unreadable" and ops[0]["group"] == "operator"
    assert ops[0]["body"].startswith("unreadable: ") and "dream" in ops[0]["body"]
    assert _groups(panel)["firing"] == [world.rid("firing")] and "agent:" + dream in {r["id"] for r in panel["rows"]}
    assert world.ck.read_one(AGENTS_ID, ops[0]["id"]).value["body"] == ops[0]["body"]
    assert _snapshot(world) == before


def test_the_operator_row_offers_no_verb_and_a_verb_aimed_at_it_is_refused(world):
    # behaviour: reading only. No agent verb applies to the operator row, and aiming one at it changes nothing
    _dream(world)
    before = _snapshot(world)
    row = _operator_rows(world.panel())[0]
    req = {"verb": "agent_pause", "panel_id": AGENTS_ID, "row_id": row["id"], "row_version": row["version"], "params": {}}
    with pytest.raises(EditError):
        dispatch(registry=world.reg, cockpit=world.ck, scope=object(), req=req, credential=CRED,
                 install_class=REPORTED, route="cockpit")
    assert _snapshot(world) == before


def test_the_panel_module_reaches_the_attachments_only_through_the_operator_module():
    # behaviour: the gate K2b made conscious. The panel names neither table, and it imports the operator module
    text = (Path(__file__).resolve().parents[1] / "levain" / "cockpit" / "agents_panel.py").read_text()
    assert "agent_attachments" not in text and "agent_operator" not in text and "levain.agents.operator" in text


def test_a_store_with_no_agent_still_says_none_registered_beside_the_uncounted_operator_row(tmp_path):
    # behaviour: the pinned row is not a figure: an empty store keeps "No agent is registered." and a count of zero
    from levain.agents.store import open_agent_store

    open_agent_store(tmp_path / "e")
    ck = Cockpit(entity=lambda ctx: {"name": "t"})
    ck.agents_wiring = AgentsWiring(store_dir=tmp_path / "e")
    ck.discover(agents_discoverer(ck), retire=True)
    ck.manifest(CLOSED)
    panel = ck.panel(AGENTS_ID)
    assert [r["id"] for r in panel["rows"]] == ["operator-agent:none"] and panel["count"] == 0
    assert panel["empty"] == "No agent is registered."


# --- the revoke verb and its undo, a re-sign (np-97463d2f). Trust-boundary code: every refusal is checked to have done
# --- nothing the refusal says it did not do (no link appended, and a bar opened only where the sentence says one is) ---

def _revoke_links(world: World) -> list[tuple[int, str, str]]:
    """``(seq, op, signer_fp)`` of every revoke_agent link in the log."""
    import json

    log = sqlite3.connect(world.rig.world.db)
    try:
        rows = log.execute("SELECT seq, record, signer_fp FROM authority_log ORDER BY seq").fetchall()
    finally:
        log.close()
    return [(s, json.loads(r)["op"], fp) for s, r, fp in rows if json.loads(r)["op"] == "revoke_agent"]


def _bars(world: World) -> list[tuple]:
    conn = sqlite3.connect(world.wiring.store_dir / "autonomic.db")
    try:
        return conn.execute("SELECT agent_id, closed_at, reason, revoke_seq FROM agent_bars").fetchall()
    finally:
        conn.close()


def _policy(world: World):
    from levain.agent_authority import AgentAuthority

    return AgentAuthority(world.rig.world.root).refresh().policy.grants


def _with_signing(world: World, signing) -> None:
    w = world.wiring
    world.wiring = AgentsWiring(store_dir=w.store_dir, cockpit_root=w.cockpit_root, signing=signing, clock=w.clock)


def test_revoke_is_a_c1_confirmed_verb_signed_on_the_wired_key_and_touches_only_that_agent(world):
    # behaviour: the cockpit revoke removes the agent's links through the CLI's own bar protocol, on the wired
    # laptop-presence key; an unconfirmed tap does nothing; another agent's link stands
    import levain.cockpit.verbs as verbs_mod

    spec = next(v for v in KERNEL_VERBS if v.name == "agent_revoke")
    assert (spec.route, spec.binding, spec.floor, spec.confirm_required, spec.reversible) == ("cockpit", "row", "C1",
                                                                                              True, False)
    assert "agent_revoke" in AGENT_VERBS
    world.rig.n = 1                                                   # World signed al-000000000001 itself
    other = world.rig.link()                                          # the rig's own agent has a link too
    world.rig.world.clock.now += timedelta(days=1)
    before = _log_seqs(world)
    with pytest.raises(EditError) as ei:
        world.post("agent_revoke", "firing")                          # unconfirmed: nothing happens
    assert ei.value.code == "confirm_required" and _log_seqs(world) == before and _bars(world) == []
    out = world.post("agent_revoke", "firing", confirm=True)
    assert out["state"] == "landed" and out["agent"] == world.ids["firing"]
    revokes = _revoke_links(world)
    assert [(op, fp) for _s, op, fp in revokes] == [("revoke_agent", world.rig.world.keys["laptop"][0])]
    held = _policy(world)
    assert "al-000000000001" not in held and other in held            # only this agent's link went
    assert world.group_of("firing") == "unarmed"
    from levain.cockpit.agents_panel import REVOKED_WORDS
    assert REVOKED_WORDS in world.ck.read_one(AGENTS_ID, world.rid("firing")).value["body"]
    assert [b[1:3] for b in _bars(world)] == [(_bars(world)[0][1], "signed")] and _bars(world)[0][1] is not None


def test_revoke_with_no_key_wired_refuses_before_any_bar_or_signature(world):
    _with_signing(world, None)
    before = _log_seqs(world)
    with pytest.raises(EditError) as ei:
        world.post("agent_revoke", "firing", confirm=True)
    assert ei.value.code == "not_built" and _log_seqs(world) == before and _bars(world) == []
    assert world.group_of("firing") == "firing"


@pytest.mark.parametrize("how", ["unenrolled", "wrong_signature", "signer_raises"])
def test_a_revoke_signed_by_the_wrong_hands_is_refused_with_the_fixed_sentence_and_appends_nothing(world, tmp_path, how):
    # behaviour: a key the log does not know, a signature that is not that key's, a signer that fails (a cancelled
    # touch): each is refused with the one sentence, no revoke link is appended, and the agent stays barred as the CLI says
    import levain.cockpit.verbs as verbs_mod
    from tests.test_consent_queue import Key
    import levain.authority as auth

    (tmp_path / "k2").mkdir()
    stranger = Key(tmp_path / "k2", "stranger")
    laptop_fp = world.rig.world.keys["laptop"][0]
    phone_sign = world.rig.world.keys["phone"][1]

    def raising(message: bytes) -> str:
        raise OSError("touch cancelled SECRET-42")
    signing = {"unenrolled": lambda: (stranger.fp, lambda m: stranger.sign(m, auth.NAMESPACE)),
               "wrong_signature": lambda: (laptop_fp, phone_sign),
               "signer_raises": lambda: (laptop_fp, raising)}[how]
    _with_signing(world, signing)
    before = _log_seqs(world)
    with pytest.raises(EditError) as ei:
        world.post("agent_revoke", "firing", confirm=True)
    # a signer's OSError (a cancelled touch) is a signing refusal, never "the log cannot be written", and no error text
    assert ei.value.code == "refused" and str(ei.value) == verbs_mod.REVOKE_UNSIGNED
    assert "SECRET-42" not in str(ei.value)
    assert _log_seqs(world) == before and _revoke_links(world) == []
    assert "al-000000000001" in _policy(world)                       # the link still stands in the log
    open_bars = [b for b in _bars(world) if b[1] is None]
    assert len(open_bars) == 1                                       # barred, as the sentence says


def test_revoking_an_unknown_or_already_revoked_agent_is_refused_without_a_bar_or_a_link(world):
    # behaviour: no link in the log -> the fixed refusal, no bar, no append (an unsigned agent, an unknown one, and a
    # second revoke of one already revoked all end here); the verb is not even offered on those rows
    import levain.cockpit.verbs as verbs_mod
    from levain.agents.spec import get_agent

    world.post("agent_revoke", "firing", confirm=True)
    seqs, bars_then = _log_seqs(world), _bars(world)
    row = world.ck.read_one(AGENTS_ID, world.rid("firing")).value
    assert not verbs_mod._REVOKABLE_AGENT_ROW(row)
    with pytest.raises(EditError) as ei:
        world.post("agent_revoke", "firing", confirm=True)           # offered no more: the kernel refuses it
    assert ei.value.code == "stale"
    stale = {"row": {**row, "facets": {**row["facets"], "agent_link": "al-000000000001"}},
             "agents": world.ck.agents_wiring}
    for name in ("firing", "unarmed"):                               # already revoked; never signed
        target = {"row": {**row, "id": world.rid(name), "facets": {**row["facets"], "agent_link": "al-000000000001",
                                                                   "agent_version": 1}},
                  "agents": world.ck.agents_wiring}
        with pytest.raises(EditError) as ei:
            verbs_mod._agent_revoke_fire(object(), {}, target, True)
        assert ei.value.code == "refused" and str(ei.value) == verbs_mod.REVOKE_NO_LINK
    ghost = {"row": {**row, "id": "agent:ag-" + "0" * 32}, "agents": world.ck.agents_wiring}
    with pytest.raises(EditError):
        verbs_mod._agent_revoke_fire(object(), {}, ghost, True)
    assert _log_seqs(world) == seqs and _bars(world) == bars_then
    assert get_agent(world.rig.store, world.ids["unarmed"])["registration"] == "registered"


@pytest.mark.parametrize("how", ["expired", "signer_revoked"])
def test_a_lapsed_or_stopped_link_still_stands_for_the_revoke_and_is_revoked(world, how):
    # behaviour (grant-readers registry, tests/test_grant_readers.py): the revoke's two raw policy.grants reads
    # (_agent_revoke_fire's `mine`, _revoke_in_log's `stands`) are PRESENCE reads. A link past its expiry, or one whose
    # signing key was revoked (grants_in_force leaves it out), arms nothing but is still in the log: it still "stands"
    # (no revoke is claimed while it does) and the revoke still removes it. A liveness read would refuse it with
    # REVOKE_NO_LINK and leave the link in the log, or read it as gone before any revoke landed
    import levain.authority as auth
    import levain.cockpit.verbs as verbs_mod
    from tests.test_consent_queue import Key

    name, link_id = "firing", "al-000000000001"
    if how == "expired":
        world.rig.world.clock.now += timedelta(days=40)              # past the link's 29 days
        assert "its link has expired" in world.ck.read_one(AGENTS_ID, world.rid(name)).value["body"]
    else:
        name, link_id = "unarmed", "al-000000000009"
        second = Key(world.rig.world.tmp / "keys", "laptop2")
        world.rig.world.link({"op": "enrol", "fingerprint": second.fp, "public_key": second.public_key,
                              "class": "laptop-presence", "custody": "passphrase"})
        world.rig.world.keys["laptop2"] = (second.fp, lambda m: second.sign(m, auth.NAMESPACE))
        world.rig.world.link({"op": "create_grant", "grant": link_record(
            world.rig.world, 9, agent=world.ids[name], store=world.rig.sid, spec_hash=world.hashes[name],
            binding_id=BINDING)}, "laptop2")
        world.rig.world.link({"op": "revoke_key", "fingerprint": second.fp})
        state = verbs_mod._agent_log_state(world.wiring)
        assert link_id in state.policy.grants and link_id not in auth.grants_in_force(state)
    assert world.group_of(name) == "unarmed"
    seen = verbs_mod._revoke_in_log(world.wiring, world.ids[name], world.rig.sid, 0, link_id)
    assert seen is not None and seen[1:] == (None, True)              # present, so it stands: no revoke is claimed
    row = world.ck.read_one(AGENTS_ID, world.rid(name)).value
    target = {"row": {**row, "facets": {**row["facets"], "agent_link": link_id, "agent_version": 1}},
              "agents": world.ck.agents_wiring}
    out = verbs_mod._agent_revoke_fire(object(), {}, target, True)
    assert out["state"] == "landed" and link_id not in _policy(world)
    seen = verbs_mod._revoke_in_log(world.wiring, world.ids[name], world.rig.sid, 0, link_id)
    assert seen is not None and seen[1] == out["seq"] and seen[2] is False


def test_a_revoke_aimed_at_a_link_the_row_did_not_show_is_stale_and_signs_nothing(world):
    import levain.cockpit.verbs as verbs_mod

    row = world.ck.read_one(AGENTS_ID, world.rid("firing")).value
    target = {"row": {**row, "facets": {**row["facets"], "agent_link": "al-ffffffffffff"}},
              "agents": world.ck.agents_wiring}
    before = _log_seqs(world)
    with pytest.raises(EditError) as ei:
        verbs_mod._agent_revoke_fire(object(), {}, target, True)
    assert ei.value.code == "stale" and _log_seqs(world) == before and _bars(world) == []


# --- revokev fix 2 (1.0 scope ruling 11-16): the undo is NOT a verb. A revoked row says so and names the normal sign
# --- path (`levain agents sign`, a new link, terms set by hand); every revoke-side reply is checked against disk ---

def test_there_is_no_undo_verb_and_no_raw_journal_read_the_undo_is_a_new_link_through_the_normal_sign_path():
    # behaviour: the 1.0 cockpit never reconstructs a revoked link's terms; the card offers the existing CLI sign
    import levain.cockpit.agents_panel as panel_mod
    import levain.cockpit.verbs as verbs_mod

    assert "agent_resign" not in {v.name for v in KERNEL_VERBS} and "agent_resign" not in AGENT_VERBS
    for gone in ("_agent_resign_fire", "_revoked_link_terms", "_RESIGNABLE_AGENT_ROW", "RESIGN_NO_TERMS",
                 "RESIGN_UNSIGNED", "REVOKED_WORDS"):
        assert not hasattr(verbs_mod, gone), gone
    src = Path(verbs_mod.__file__).read_text()
    assert "_auth._rows(" not in src and "_link_hash(" not in src and "sign_card(" not in src
    words = panel_mod.REVOKED_WORDS
    assert "undo = sign a new link: the revoke stays in the log" in words and "levain agents sign" in words
    assert "set terms" in words                                        # no old terms are shown: none are read
    js = (Path(panel_mod.__file__).parents[1] / "templates" / "web" / "agents_view.js").read_text()
    assert "agent_resign" not in js and "sign again" not in js
    assert "undo = sign a new link: the revoke stays in the log" in js and "levain agents sign" in js


@pytest.mark.parametrize("paused", [False, True])
def test_a_revoked_agent_says_so_and_names_the_new_link_line_paused_or_not(world, paused):
    # behaviour (codex MED 3): the revoked state is judged from the log before the pause wins the group, so a paused
    # agent that was revoked still says it, and the undo it names is a new signature (an `agent_revoked` FACET would
    # need levain/cockpit/registry.py's facet table, outside this lane: there is no verb left to key on it)
    import levain.cockpit.agents_panel as panel_mod

    world.rig.world.clock.now += timedelta(days=1)
    assert panel_mod.REVOKED_WORDS not in world.ck.read_one(AGENTS_ID, world.rid("firing")).value["body"]
    out = world.post("agent_revoke", "firing", confirm=True)
    assert "levain agents sign" in out["undo"] and "restore" not in out["undo"]
    if paused:
        world.post("agent_pause", "firing")
    row = world.ck.read_one(AGENTS_ID, world.rid("firing")).value
    assert row["facets"]["agent_group"] == ("paused" if paused else "unarmed")
    assert panel_mod.REVOKED_WORDS in row["body"]
    for name in ("unarmed", "paused"):                                # never signed; paused and never revoked
        other = world.ck.read_one(AGENTS_ID, world.rid(name)).value
        assert panel_mod.REVOKED_WORDS not in other["body"]


def test_a_signing_key_that_cannot_be_opened_is_a_signing_refusal_with_no_error_text_and_no_bar(world):
    # behaviour (deepseek MED 3): wiring.signing() raising OSError is a signing refusal, not "the log cannot be written"
    import levain.cockpit.verbs as verbs_mod

    def broken():
        raise OSError("no such key SECRET-43")
    _with_signing(world, broken)
    before = _log_seqs(world)
    with pytest.raises(EditError) as ei:
        world.post("agent_revoke", "firing", confirm=True)
    assert ei.value.code == "refused" and str(ei.value) == verbs_mod.REVOKE_NOT_BEGUN
    assert "SECRET-43" not in str(ei.value) and _log_seqs(world) == before and _bars(world) == []


@pytest.mark.parametrize("where", ["open_bar", "finish"])
def test_a_store_failure_says_barred_only_when_a_bar_is_open(world, monkeypatch, where):
    # behaviour (codex MED 2): a begin that fails before its bar is committed never says "stays barred"; a failure
    # after the bar is committed does. The sentence is read from the store, not from a flag set before the begin ran
    import levain.agent_authority as aa
    import levain.cockpit.verbs as verbs_mod
    from levain.agents import bars

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked SECRET-44")
    monkeypatch.setattr(bars, "open_bar", locked) if where == "open_bar" else \
        monkeypatch.setattr(aa, "revoke_agent_finish", locked)
    before = _log_seqs(world)
    with pytest.raises(EditError) as ei:
        world.post("agent_revoke", "firing", confirm=True)
    assert ei.value.code == "source_unavailable" and "SECRET-44" not in str(ei.value)
    assert _log_seqs(world) == before and "al-000000000001" in _policy(world)
    open_bars = [b for b in _bars(world) if b[1] is None]
    if where == "open_bar":
        assert open_bars == [] and str(ei.value) == verbs_mod.REVOKE_STORE_NOT_BARRED
    else:
        assert len(open_bars) == 1 and str(ei.value) == verbs_mod.REVOKE_STORE_BARRED


@pytest.mark.parametrize("how", ["same_bar", "own_bar", "abandoned_only"])
def test_an_abandoned_step_reads_the_log_before_it_says_revoked_or_not(world, monkeypatch, how):
    # behaviour (codex MED 5): "abandoned" from a step is not "NOT revoked" until the bar and the log say so; a
    # concurrent revoke that landed (on this bar, or on its own after an abandon) is reported landed
    import levain.agent_authority as aa
    import levain.cockpit.verbs as verbs_mod
    from levain.agents import bars
    from levain.authority import anchor_path
    from levain.cockpit.keys import _journal

    world.rig.world.clock.now += timedelta(days=1)
    real_begin, real_sign, real_finish = aa.revoke_agent_begin, aa.revoke_agent_sign, aa.revoke_agent_finish
    fp, sign = world.rig.world.keys["laptop"]

    def concurrent(autonomic, bar_id, _sign, *, clock):
        db = _journal(world.wiring.cockpit_root)
        conn = sqlite3.connect(world.wiring.store_dir / "autonomic.db", isolation_level=None)
        try:
            with db.autocommit() as cockpit:
                if how == "same_bar":
                    real_sign(conn, bar_id, sign, clock=clock)
                    assert real_finish(conn, cockpit, bar_id, clock=clock, anchor=anchor_path(db.data_root)).stands
                else:
                    assert bars.close_bar(conn, bar_id, revoke_seq=None, reason="abandoned", now=clock())
                    if how == "own_bar":
                        step = real_begin(conn, cockpit, world.ids["firing"], store_id=world.rig.sid, signer_fp=fp,
                                          clock=clock)
                        real_sign(conn, step.bar_id, sign, clock=clock)
                        assert real_finish(conn, cockpit, step.bar_id, clock=clock,
                                           anchor=anchor_path(db.data_root)).stands
        finally:
            conn.close()
        return aa.RevokeStep("abandoned", bar_id, None, "the bar was closed")
    monkeypatch.setattr(aa, "revoke_agent_sign", concurrent)
    if how == "abandoned_only":
        with pytest.raises(EditError) as ei:
            world.post("agent_revoke", "firing", confirm=True)
        assert str(ei.value) == verbs_mod.REVOKE_ABANDONED and _revoke_links(world) == []
        assert "al-000000000001" in _policy(world)
        return
    out = world.post("agent_revoke", "firing", confirm=True)
    revokes = _revoke_links(world)
    assert len(revokes) == 1 and out["state"] == "landed" and out["seq"] == revokes[0][0]
    assert "al-000000000001" not in _policy(world)


# --- revokev fix 3 (L3 round 2): each case below failed on 66ba733f1 before its fix ---

def _stored(world: World, name: str) -> dict:
    """The row's stored fields (``revoked`` is one; it is not a facet), read as the panel reads them."""
    from levain.cockpit.agents_panel import read_rows

    rows, _note = read_rows(world.wiring, only=world.ids[name])
    return next(r for r in rows if r.id == world.rid(name)).stored


def test_the_changelog_names_the_stored_field_revoked_and_no_agent_revoked_facet():
    # behaviour (4 seats): the entry said "facet `agent_revoked`", which nothing emits
    import levain.cockpit.agents_panel as panel_mod

    text = (Path(panel_mod.__file__).parents[2] / "CHANGELOG.md").read_text()
    entry = next(line for line in text.splitlines() if "The cockpit revokes an agent's links" in line)
    assert "agent_revoked" not in entry and "stored field `revoked`, no facet" in entry
    assert "agent_revoked" not in panel_mod.AGENT_FACETS
    assert "agent_revoked" not in Path(panel_mod.__file__).read_text()


def test_a_revoke_that_landed_before_this_calls_bar_closes_that_bar(world, monkeypatch):
    # behaviour (codex MED 2): another revoke lands between this call's check and its begin; the begin opens a new bar
    # and has nothing left to prepare. The reply is landed, and the bar is closed (R3), not left to refuse the undo
    import levain.agent_authority as aa
    import levain.cockpit.agents_panel as panel_mod
    from levain.authority import anchor_path
    from levain.cockpit.keys import _journal

    world.rig.world.clock.now += timedelta(days=1)
    real_begin, real_sign, real_finish = aa.revoke_agent_begin, aa.revoke_agent_sign, aa.revoke_agent_finish
    _fp, sign = world.rig.world.keys["laptop"]

    def other_first(autonomic, cockpit, agent_id, **kw):
        conn = sqlite3.connect(world.wiring.store_dir / "autonomic.db", isolation_level=None)
        try:
            step = real_begin(conn, cockpit, agent_id, **kw)
            real_sign(conn, step.bar_id, sign, clock=kw["clock"])
            assert real_finish(conn, cockpit, step.bar_id, clock=kw["clock"],
                               anchor=anchor_path(_journal(world.wiring.cockpit_root).data_root)).stands
        finally:
            conn.close()
        return real_begin(autonomic, cockpit, agent_id, **kw)        # this call's own begin, after the other landed
    monkeypatch.setattr(aa, "revoke_agent_begin", other_first)
    out = world.post("agent_revoke", "firing", confirm=True)
    assert out["state"] == "landed" and "barred" not in out and len(_revoke_links(world)) == 1
    assert [b for b in _bars(world) if b[1] is None] == []
    assert sorted(b[2] for b in _bars(world)) == ["nothing_present", "signed"]
    assert panel_mod.REVOKED_WORDS in world.ck.read_one(AGENTS_ID, world.rid("firing")).value["body"]


@pytest.mark.parametrize("where", ["prepare", "append"])
def test_a_verifier_that_cannot_run_is_a_retry_never_a_signing_refusal(world, monkeypatch, where):
    # behaviour (codex MED 3): VerifierUnavailable subclasses AuthorityError; the revoke steps keep it a retry, and
    # so does the reply (503, the store sentence read from the bar), never "the revoke was not signed in"
    import levain.authority as auth
    import levain.cockpit.verbs as verbs_mod

    world.rig.world.clock.now += timedelta(days=1)

    def gone(*a, **k):
        raise auth.VerifierUnavailable("ssh-keygen cannot run SECRET-45")
    monkeypatch.setattr(auth, where, gone)
    before = _log_seqs(world)
    with pytest.raises(EditError) as ei:
        world.post("agent_revoke", "firing", confirm=True)
    assert ei.value.code == "source_unavailable" and str(ei.value) == verbs_mod.REVOKE_STORE_BARRED
    assert "SECRET-45" not in str(ei.value) and _log_seqs(world) == before
    assert len([b for b in _bars(world) if b[1] is None]) == 1


@pytest.mark.parametrize("exc", ["format", "refused"])
@pytest.mark.parametrize("verb", ["agent_revoke", "agent_renew"])
def test_a_store_that_moves_after_it_was_opened_is_source_unavailable_never_a_500(world, monkeypatch, exc, verb):
    # behaviour (codex MED 5): the second read of the store raising StoreFormatError or AgentStoreRefused (the store
    # gone, or its marker moved since it was opened) is the fixed 503, not an uncaught error
    from contextlib import contextmanager

    import levain.cockpit.verbs as verbs_mod
    from levain.agents.store import AgentStoreRefused
    from levain.autonomic.db import StoreFormatError

    world.rig.world.clock.now += timedelta(days=1)
    real = verbs_mod._agent_store_write

    class Moved:
        def __init__(self, store) -> None:
            self._db = store._db

        @contextmanager
        def read(self):
            raise StoreFormatError("moved") if exc == "format" else AgentStoreRefused("moved")
            yield  # pragma: no cover
    monkeypatch.setattr(verbs_mod, "_agent_store_write", lambda wiring, store_id: Moved(real(wiring, store_id)))
    before = _log_seqs(world)
    with pytest.raises(EditError) as ei:
        world.post(verb, "firing", {"days": 29} if verb == "agent_renew" else None, confirm=True)
    assert ei.value.code == "source_unavailable" and _log_seqs(world) == before and _bars(world) == []


def test_an_agent_signed_again_then_edited_is_not_revoked_and_its_flag_follows_its_words(world):
    # behaviour (codex MED 4, complement MED 1 + LOW 2): "revoked" is judged on every link of the agent, not only its
    # current spec's, and the stored flag is True exactly when the words say REVOKED_WORDS
    import levain.cockpit.agents_panel as panel_mod

    world.rig.world.clock.now += timedelta(days=1)
    world.post("agent_revoke", "firing", confirm=True)
    row = world.ck.read_one(AGENTS_ID, world.rid("firing")).value
    assert panel_mod.REVOKED_WORDS in row["body"] and _stored(world, "firing")["revoked"] is True
    world.rig.world.clock.now += timedelta(minutes=5)
    world.rig.world.link({"op": "create_grant", "grant": link_record(
        world.rig.world, 7, agent=world.ids["firing"], store=world.rig.sid, spec_hash=world.hashes["firing"],
        binding_id=BINDING)})                                         # the undo: a new link of v1
    assert world.group_of("firing") == "firing" and _stored(world, "firing")["revoked"] is False
    _revise(world, "firing")                                          # then the spec is edited: no link covers v2
    row = world.ck.read_one(AGENTS_ID, world.rid("firing")).value
    stored = _stored(world, "firing")
    assert row["facets"]["agent_group"] == "unarmed" and panel_mod.REVOKED_WORDS not in row["body"]
    assert stored["revoked"] is False and stored["words"] == "no signed link covers this spec"


@pytest.mark.parametrize("how", ["abandoned", "store_failure"])
def test_a_row_link_another_change_removed_is_stale_never_its_links_stand(world, monkeypatch, how):
    # behaviour (codex MED 4): the row's link removed by a revoke_grant (no revoke_agent: agent_last_revoke does not
    # move) while this call's bar was abandoned, or before its bar was opened: never "its links stand"
    import levain.agent_authority as aa
    import levain.cockpit.verbs as verbs_mod
    from levain.agents import bars

    world.rig.world.clock.now += timedelta(days=1)

    def remove() -> None:
        world.rig.world.link({"op": "revoke_grant", "grant_id": "al-000000000001"})
    if how == "abandoned":
        def concurrent(autonomic, bar_id, _sign, *, clock):
            conn = sqlite3.connect(world.wiring.store_dir / "autonomic.db", isolation_level=None)
            try:
                assert bars.close_bar(conn, bar_id, revoke_seq=None, reason="abandoned", now=clock())
            finally:
                conn.close()
            remove()
            return aa.RevokeStep("abandoned", bar_id, None, "the bar was closed")
        monkeypatch.setattr(aa, "revoke_agent_sign", concurrent)
    else:
        def locked(*a, **k):
            remove()
            raise sqlite3.OperationalError("database is locked")
        monkeypatch.setattr(bars, "open_bar", locked)
    with pytest.raises(EditError) as ei:
        world.post("agent_revoke", "firing", confirm=True)
    assert ei.value.code == "stale" and str(ei.value) == verbs_mod.REVOKE_LINK_GONE
    assert "links stand" not in str(ei.value) and _revoke_links(world) == []
    assert "al-000000000001" not in _policy(world)


def test_a_cockpit_journal_that_cannot_open_is_a_store_failure_never_a_signing_refusal(world, monkeypatch):
    # behaviour (deepseek MED 2): KeyError_ from `_journal` is the cockpit journal's, not the signer's
    import levain.cockpit.keys as keys
    import levain.cockpit.verbs as verbs_mod

    def broken(root):
        raise keys.KeyError_("journal unreadable SECRET-46")
    monkeypatch.setattr(keys, "_journal", broken)
    before = _log_seqs(world)
    with pytest.raises(EditError) as ei:
        world.post("agent_revoke", "firing", confirm=True)
    assert ei.value.code == "source_unavailable" and str(ei.value) == verbs_mod.REVOKE_STORE_NOT_BARRED
    assert "SECRET-46" not in str(ei.value) and _log_seqs(world) == before and _bars(world) == []


def test_a_log_judgement_that_raises_after_the_replay_is_source_unavailable_never_a_500(world, monkeypatch):
    # behaviour (deepseek LOW 5): what is derived from the fresh replay is guarded as the replay is
    import levain.agent_authority as aa
    import levain.authority as auth

    real = auth.agent_last_revoke
    armed: list[int] = []

    def flaky(*a, **k):
        if armed:
            raise TypeError("not a seq")
        return real(*a, **k)

    def locked(*a, **k):
        armed.append(1)
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(auth, "agent_last_revoke", flaky)
    monkeypatch.setattr(aa, "revoke_agent_finish", locked)
    with pytest.raises(EditError) as ei:
        world.post("agent_revoke", "firing", confirm=True)
    assert ei.value.code == "source_unavailable" and "cannot be read now" in str(ei.value)


# --- revokev fix 4 (L3 verify round): each case below failed on 16457530e before its fix ---

def _open_bar(world: World, name: str) -> None:
    """An open bar of the agent, as a revoke that could not finish leaves it."""
    from levain.agents import bars

    conn = sqlite3.connect(world.wiring.store_dir / "autonomic.db", isolation_level=None)
    try:
        bars.open_bar(conn, world.ids[name], head_seq=0, now=world.rig.now())
    finally:
        conn.close()


@pytest.mark.parametrize("paused", [False, True])
def test_an_open_bar_reads_barred_never_revoked_paused_or_not(world, paused):
    # behaviour (codex OPEN 5, gemini MED, complement LOW): a paused agent with a revoke in the log, no link and an
    # open bar said REVOKED_WORDS (undo = sign a new link) with `revoked` True; the open bar refuses that new link
    import levain.cockpit.agents_panel as panel_mod

    world.rig.world.clock.now += timedelta(days=1)
    world.post("agent_revoke", "firing", confirm=True)
    if paused:
        world.post("agent_pause", "firing")
    _open_bar(world, "firing")
    row = world.ck.read_one(AGENTS_ID, world.rid("firing")).value
    stored = _stored(world, "firing")
    assert row["facets"]["agent_group"] == ("paused" if paused else "unarmed")
    assert panel_mod.REVOKED_WORDS not in row["body"] and "barred: a revoke is closing out" in stored["words"]
    assert stored["revoked"] is False


@pytest.mark.parametrize("links", ["only_the_row_link", "another_link_stands"])
def test_a_row_link_gone_after_this_calls_bar_opened_closes_that_bar_or_says_it_stays_open(world, monkeypatch, links):
    # behaviour (complement MED): this call's bar is open, a store error follows, and the row's link was removed by a
    # revoke_grant: the stale reply left the bar open and said nothing, so `levain agents sign` then refused
    import levain.agent_authority as aa
    import levain.cockpit.verbs as verbs_mod

    world.rig.world.clock.now += timedelta(days=1)
    if links == "another_link_stands":
        world.rig.world.link({"op": "create_grant", "grant": link_record(
            world.rig.world, 9, agent=world.ids["firing"], store=world.rig.sid, spec_hash=world.hashes["firing"],
            binding_id=BINDING)})
    row_link = world.ck.read_one(AGENTS_ID, world.rid("firing")).value["facets"]["agent_link"]

    def locked(*a, **k):
        world.rig.world.link({"op": "revoke_grant", "grant_id": row_link})
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(aa, "revoke_agent_sign", locked)
    with pytest.raises(EditError) as ei:
        world.post("agent_revoke", "firing", confirm=True)
    assert ei.value.code == "stale" and _revoke_links(world) == [] and row_link not in _policy(world)
    still_open = [b for b in _bars(world) if b[1] is None]
    if links == "only_the_row_link":
        assert str(ei.value) == verbs_mod.REVOKE_LINK_GONE and still_open == []
        assert [b[2] for b in _bars(world)] == ["nothing_present"]
    else:
        assert str(ei.value) == verbs_mod.REVOKE_LINK_GONE_BARRED and len(still_open) == 1
        assert verbs_mod.REVOKE_LANDED_STILL_BARRED in str(ei.value)


def test_an_agent_store_refused_mid_revoke_is_source_unavailable_never_a_500(world, monkeypatch):
    # behaviour (complement LOW): AgentStoreRefused (a RuntimeError) was in no clause of the revoke's store reads
    import levain.cockpit.verbs as verbs_mod
    from levain.agents import bars
    from levain.agents.store import AgentStoreRefused

    world.rig.world.clock.now += timedelta(days=1)

    def moved(*a, **k):
        raise AgentStoreRefused("the marker moved SECRET-47")
    monkeypatch.setattr(bars, "open_bar", moved)
    before = _log_seqs(world)
    with pytest.raises(EditError) as ei:
        world.post("agent_revoke", "firing", confirm=True)
    assert ei.value.code == "source_unavailable" and str(ei.value) == verbs_mod.REVOKE_STORE_NOT_BARRED
    assert "SECRET-47" not in str(ei.value) and _log_seqs(world) == before and _bars(world) == []


def test_a_bar_close_that_raises_after_a_landed_revoke_is_landed_and_barred_never_a_500(world, monkeypatch):
    # behaviour (complement LOW): _close_bar_after_landing caught only the store errors; anything else from the
    # recovery rows turned a landed revoke into a 500
    import levain.agent_authority as aa
    import levain.cockpit.verbs as verbs_mod
    from levain.agents import bars
    from levain.authority import anchor_path
    from levain.cockpit.keys import _journal

    world.rig.world.clock.now += timedelta(days=1)
    real_begin, real_sign, real_finish = aa.revoke_agent_begin, aa.revoke_agent_sign, aa.revoke_agent_finish
    _fp, sign = world.rig.world.keys["laptop"]

    def other_first(autonomic, cockpit, agent_id, **kw):
        conn = sqlite3.connect(world.wiring.store_dir / "autonomic.db", isolation_level=None)
        try:
            step = real_begin(conn, cockpit, agent_id, **kw)
            real_sign(conn, step.bar_id, sign, clock=kw["clock"])
            assert real_finish(conn, cockpit, step.bar_id, clock=kw["clock"],
                               anchor=anchor_path(_journal(world.wiring.cockpit_root).data_root)).stands
        finally:
            conn.close()
        return real_begin(autonomic, cockpit, agent_id, **kw)

    def broken(*a, **k):
        raise TypeError("a recovery row changed shape")
    monkeypatch.setattr(aa, "revoke_agent_begin", other_first)
    monkeypatch.setattr(bars, "_r3", broken)
    out = world.post("agent_revoke", "firing", confirm=True)
    assert out["state"] == "landed" and out["barred"] == verbs_mod.REVOKE_LANDED_STILL_BARRED
