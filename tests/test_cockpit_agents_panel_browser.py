"""P9b: the agents panel in the running cockpit's Build tab, in a real headless Chrome at 390 px and 1280 px.

The scratch store is the real agent DDL and the authority log is real (the ``Rig`` of the launch tests), laid where the
served install reads them: the agent store under ``$LEVAIN_HOME/autonomic`` and the log under the install's
``.levain/cockpit``. The cockpit is the one ``make_server`` builds, so a panel that reaches the page here reached it
through production wiring. Set ``LEVAIN_PANEL_SHOTS`` to a folder to also save a screenshot per width (a review look).
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from levain.agents.role import role_hash
from levain.agents.spec import parse_spec, register_spec, retire_agent, set_operation
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

pytest.importorskip("cryptography")
from tests.test_agents_launch import BINDING, ROLES, SPECS, Rig  # noqa: E402
from tests.test_authority_agent_link import link as link_record  # noqa: E402

pytestmark = [needs_browser, pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")]

TOK = "T"
PANEL = "document.querySelector('[data-ck-panel=\"agents\"]')"
WIDTHS = {"390": {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
          "1280": {"emulate": {"w": 1280, "h": 1000, "dpr": 1}}}
BUILD = {"eval": "window.LevainCockpitKinds.applyMode('build', true); 1"}
SETTLE = {"eval": "new Promise(r => setTimeout(r, 600)).then(() => 1)"}


def _spec(name: str) -> Any:
    d = copy.deepcopy(SPECS["job_push_check"])
    d["name"] = name
    d["role"]["role_hash"] = role_hash(ROLES[d["role"]["name"]])
    return parse_spec(d)


class Scratch:
    """One agent per group, laid out the way the served install reads them. The log's clock is moved to the real
    clock first, because the served page judges a link's expiry at the real time."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "rig").mkdir()
        rig = Rig(tmp_path / "rig")
        rig.world.clock.now = datetime.now(timezone.utc) - timedelta(minutes=10)
        self.ids: dict[str, str] = {}
        hashes: dict[str, str] = {}
        for name in ("firing", "paused", "unarmed", "retired"):
            self.ids[name], hashes[name], *_ = register_spec(rig.store, _spec("w-" + name), "America/New_York")
        rig.world.link({"op": "create_grant", "grant": link_record(
            rig.world, 1, agent=self.ids["firing"], store=rig.sid, spec_hash=hashes["firing"], binding_id=BINDING)})
        set_operation(rig.store, self.ids["paused"], "paused")
        retire_agent(rig.store, self.ids["retired"])
        self.rig_agent = rig.agent
        self.root, self.src, _ = _install(tmp_path)
        home = tmp_path / "levain_home"
        home.mkdir()
        shutil.copytree(rig.world.root, self.root / ".levain" / "cockpit")
        shutil.copytree(tmp_path / "rig" / "s", home / "autonomic")
        monkeypatch.setenv("LEVAIN_HOME", str(home))
        self.store_db = home / "autonomic" / "autonomic.db"

    def state(self, name: str) -> tuple[str, str]:
        """``(registration, operation)`` of one agent, read from the store the served page writes."""
        conn = sqlite3.connect(self.store_db)
        try:
            return conn.execute("SELECT registration, operation FROM agents WHERE agent_id = ?", (self.ids[name],)).fetchone()
        finally:
            conn.close()

    def cards(self, home: Path) -> None:
        """Review cards on the scratch coordinator store: ``w-firing`` has one open and one cleared, ``w-paused`` one open."""
        from levain.coordinator import review
        from levain.coordinator.store import CoordStore

        (home / "consent").mkdir(mode=0o700, exist_ok=True)
        store = CoordStore(home / "consent")
        review.file_item(store, "agent", {"reason": "a"}, dedupe_key=f"dream-night:{self.ids['firing']}:d1")
        done, _ = review.file_item(store, "agent", {"reason": "b"}, dedupe_key=f"dream-night:{self.ids['firing']}:d2")
        review.clear_item(store, done.item_id)
        review.file_item(store, "agent", {"reason": "c"}, dedupe_key=f"dream-night:w-paused:d3")



ROWS = "[...document.querySelectorAll('[data-ck-panel=\"agents\"] .row[data-keep-key]')]"
READ = ("(() => { const p = " + PANEL + "; if (!p) return null; const r = p.getBoundingClientRect();"
        " const rows = " + ROWS + ".map(x => ({key: x.dataset.keepKey, group: x.querySelector('.ext-meta').textContent.trim(),"
        " body: x.querySelector('.clause').textContent.trim(), opens: x.dataset.agentRow === '1'}));"
        " if (!rows.length) return null;"
        " return JSON.stringify({text: p.innerText, rows, w: Math.round(r.width), clipped: p.scrollWidth > p.clientWidth + 1}); })()")


def _row(name: str) -> str:
    """The expression for one row (its body starts with the agent's name, or the operator row's words), or null."""
    return (f"({ROWS}.find(r => r.querySelector('.clause').textContent.startsWith({json.dumps(name)})) || null)")


def _tap_row(name: str) -> dict:
    return {"tap": f"{_row(name)}.querySelector('.ext-meta')", "before": f"{_row(name)}.scrollIntoView({{block: 'center'}})", "ms": 20000}


DETAIL = ("(() => { const d = document.querySelector('#levain-modal [data-agent-detail]'); if (!d) return null;"
          " const m = document.querySelector('#levain-modal .modal-body') || d;"
          " return JSON.stringify({verbs: [...d.querySelectorAll('[data-agent-verb]')].map(b => b.dataset.agentVerb),"
          " text: d.innerText, none: !!d.querySelector('.agent-detail-none'), clipped: m.scrollWidth > m.clientWidth + 1}); })()")
CLOSED = "(!document.getElementById('levain-modal') || null) && 1"
OPEN = {"until": READ, "ms": 30000}


def _shots(name: str, steps: list[dict]) -> list[dict]:
    d = os.environ.get("LEVAIN_PANEL_SHOTS")
    if not d:
        return steps
    Path(d).mkdir(parents=True, exist_ok=True)
    return steps + [{"eval": PANEL + ".scrollIntoView({block: 'start'}); 1"}, {"shot": str(Path(d) / f"{name}.png")}]


def _json(out: dict, at: int) -> dict:
    return json.loads(out["values"][at])


@pytest.mark.parametrize("width", sorted(WIDTHS))
def test_the_panel_draws_in_build_with_the_four_groups_and_the_pinned_operator_row(width, tmp_path, monkeypatch) -> None:
    s = Scratch(tmp_path, monkeypatch)
    steps = [WIDTHS[width], BUILD, OPEN, SETTLE, {"eval": READ}]
    with _serve(s.src, surface_tokens={"browser": TOK}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token={TOK}", _shots("agents_" + width, steps))
    seen = _json(out, -1)
    text = seen["text"]
    groups = [r["group"] for r in seen["rows"]]
    assert groups == ["Agent", "Firing", "Paused", "Unarmed", "Unarmed", "Retired"], seen["rows"]    # the pinned row, then the four groups in order
    assert seen["rows"][0]["body"] == "no operator agent in this store yet", seen["rows"][0]
    for name in ("w-firing", "w-paused", "w-unarmed", "w-retired"):
        assert name in text, (name, text)
    assert all(r["opens"] for r in seen["rows"]), seen["rows"]                  # every row takes the open gesture
    assert not seen["clipped"], seen
    for bad in ("undefined", "[object", "null", "Traceback"):
        assert bad not in text, (bad, text)
    assert out["posts"] == [], out["posts"]                                      # drawing it sent nothing


@pytest.mark.parametrize("width", sorted(WIDTHS))
def test_the_detail_offers_each_verb_only_where_the_state_allows_it_and_none_on_the_operator_row(width, tmp_path, monkeypatch) -> None:
    s = Scratch(tmp_path, monkeypatch)
    steps: list[dict] = [WIDTHS[width], BUILD, OPEN, SETTLE]
    order = ("operator", "w-firing", "w-paused", "w-unarmed", "w-retired")
    for name in order:
        target = "no operator agent" if name == "operator" else name
        steps += [_tap_row(target), {"until": DETAIL, "ms": 20000}, {"eval": DETAIL}, {"tap": "document.querySelector('#levain-modal .modal-close')", "ms": 10000}, {"until": CLOSED, "ms": 10000}]
    with _serve(s.src, surface_tokens={"browser": TOK}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token={TOK}", _shots("agents_detail_" + width, steps))
    got = {name: json.loads(v) for name, v in zip(order, [x for x in out["values"] if isinstance(x, str) and '"verbs"' in x][1::2])}   # each `until` collects too; the eval is the second
    assert got["w-firing"]["verbs"] == ["agent_pause", "agent_renew", "agent_retire"], got["w-firing"]
    assert got["w-paused"]["verbs"] == ["agent_resume", "agent_retire"], got["w-paused"]
    assert got["w-unarmed"]["verbs"] == ["agent_pause", "agent_retire"], got["w-unarmed"]
    for name in ("operator", "w-retired"):
        assert got[name]["verbs"] == [] and got[name]["none"] is True, (name, got[name])
    assert not any(g["clipped"] for g in got.values()), got
    assert out["posts"] == [], out["posts"]                                      # reading a detail sent nothing


@pytest.mark.parametrize("width", sorted(WIDTHS))
def test_a_verb_round_trip_hits_the_verbspec_and_the_row_moves(width, tmp_path, monkeypatch) -> None:
    s = Scratch(tmp_path, monkeypatch)
    moved = lambda name, group: (f"({_row(name)} && {_row(name)}.querySelector('.ext-meta').textContent.trim() === {json.dumps(group)}) || null")
    verb = lambda v: f"document.querySelector('#levain-modal [data-agent-verb={v}]')"
    steps: list[dict] = [WIDTHS[width], BUILD, OPEN, SETTLE,
                         # pause: one tap, no confirm; the unarmed agent moves to Paused
                         _tap_row("w-unarmed"), {"until": DETAIL, "ms": 20000}, {"tap": verb("agent_pause"), "ms": 20000},
                         {"until": moved("w-unarmed", "Paused") + " && 1", "ms": 30000},
                         {"tap": "document.querySelector('#levain-modal .modal-close')", "ms": 10000}, {"until": CLOSED, "ms": 10000},
                         # retire: the first tap only asks, the second tap retires
                         _tap_row("w-paused"), {"until": DETAIL, "ms": 20000}, {"tap": verb("agent_retire"), "ms": 20000},
                         {"eval": f"{verb('agent_retire')}.textContent"},
                         {"tap": verb("agent_retire"), "ms": 20000},
                         {"until": moved("w-paused", "Retired") + " && 1", "ms": 30000}]
    with _serve(s.src, surface_tokens={"browser": TOK}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token={TOK}", steps)
        after = {n: s.state(n) for n in ("unarmed", "paused", "firing")}
    assert after["unarmed"] == ("registered", "paused"), after           # the store the verb's owner writes
    assert after["paused"][0] == "retired", after
    assert after["firing"] == ("registered", "active"), after            # no other agent was touched
    asked = [v for v in out["values"] if isinstance(v, str) and v.startswith("confirm:")]
    assert asked == ["confirm: retire for good"], out["values"]          # the irreversible verb asked first
    verbs = [json.loads(p.split(" ", 1)[1]) for p in out["posts"] if p.startswith("/cockpit/verb ")]
    fired = [(v["verb"], v.get("confirm") is True) for v in verbs if not v.get("dry_run")]
    assert fired == [("agent_pause", False), ("agent_retire", True)], verbs


@pytest.mark.parametrize("width", sorted(WIDTHS))
def test_the_cleared_or_not_cell_reflects_the_agent_review_cards_cleared_and_not(width, tmp_path, monkeypatch) -> None:
    s = Scratch(tmp_path, monkeypatch)
    s.cards(tmp_path / "levain_home")
    steps = [WIDTHS[width], BUILD, OPEN, SETTLE, {"eval": READ}]
    with _serve(s.src, surface_tokens={"browser": TOK}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token={TOK}", steps)
    bodies = {r["body"].split(" · ")[0]: r["body"] for r in _json(out, -1)["rows"]}
    assert bodies["w-firing"].endswith("cards: 1 open, 1 cleared") or "cards: 1 open, 1 cleared" in bodies["w-firing"], bodies
    assert "cards: 1 open, 0 cleared" in bodies["w-paused"], bodies      # keyed by the agent's name too
    for quiet in ("w-unarmed", "w-retired"):
        assert "cards:" not in bodies[quiet], bodies                      # no card, nothing said (not "0 open")
