"""The Team zone: one shared read per refresh, the panel set follows the reader's state, and every unhappy state is named."""
from __future__ import annotations

import time

from levain.cockpit.engine import ZONES, Cockpit
from levain.cockpit.team_panels import TeamSnapshot, TeamSource, team_discoverer
from levain.team.reader import TeamRead

CRED = {"class": "none", "device_class": None}
MODEL = {"project": "p", "owner": "ana", "mode": "ask", "you": "ben", "canon_status": "no canon yet", "problems": 0,
         "waiting": [{"id": "q1", "type": "question", "kind": None, "owner": "ben", "words": "", "summary": "Discounts first?",
                      "reason": "", "recheck": "", "paths": ["src/a.py"], "recorded_by": "ana", "age": "1d ago",
                      "age_days": 1, "pack": ""}],
         "stopped": [], "held": [], "in_force": []}


def _cockpit(reads: list, loads: list[int]) -> Cockpit:
    def load() -> TeamSnapshot:
        loads.append(1)
        return TeamSnapshot(reads[0], {})
    ck = Cockpit(entity=lambda ctx: {"name": "t"})
    ck.discover(team_discoverer(TeamSource(load=load, ttl_s=0.0)), retire=True)
    return ck


def _panels(ck: Cockpit, want: int | None = None) -> list[str]:
    """The Team zone's panel ids. A refresh serves the last answer while a worker renews it, so a changed state shows
    on a later manifest: ``want`` polls (briefly) for that many panels."""
    for _ in range(100):
        zone = [z for z in ck.manifest(CRED)["regions"]["zones"] if z["id"] == "team"][0]
        if want is None or len(zone["panels"]) == want:
            break
        time.sleep(0.02)
    return zone["panels"]


def test_team_is_a_peer_zone_and_the_panel_set_follows_the_state():
    assert ("team", "Team") in ZONES
    reads = [TeamRead("ok", MODEL)]
    ck = _cockpit(reads, [])
    assert _panels(ck) == ["team-waiting", "team-stopped", "team-held", "team-canon", "team-sync"]
    reads[0] = TeamRead("no_team", None, "This install has no team ledger.")
    assert _panels(ck, 1) == ["team-status"]                   # the four panes are RETIRED, not left reading Absent
    reads[0] = TeamRead("ok", MODEL)
    assert len(_panels(ck, 5)) == 5


def test_unreadable_draws_one_card_with_the_why_and_never_the_detail():
    ck = _cockpit([TeamRead("unreadable", None, "The team ledger could not be read.", "OSError: /secret/path")], [])
    assert _panels(ck) == ["team-status"]
    rows = ck.panel("team-status")["rows"]
    assert [r["title"] for r in rows] == ["The team ledger could not be read."]
    assert "/secret/path" not in str(ck.panel("team-status")) and "/secret/path" not in str(ck.manifest(CRED))


def test_judged_partial_draws_every_pane_with_the_why_beside_the_rows():
    ck = _cockpit([TeamRead("judged_partial", MODEL, "Part of the ledger could not be judged.")], [])
    assert len(_panels(ck)) == 5
    waiting = ck.panel("team-waiting")
    assert waiting["note"] == "Part of the ledger could not be judged." and [r["id"] for r in waiting["rows"]] == ["q1"]
    assert "Discounts first?" in waiting["rows"][0]["body"]    # the line a list row draws is its body


def test_one_ledger_read_feeds_the_manifest_and_every_panel():
    loads: list[int] = []
    clock = [0.0]
    src = TeamSource(load=lambda: (loads.append(1), TeamSnapshot(TeamRead("ok", MODEL), {}))[1], ttl_s=15.0,
                     clock=lambda: clock[0])
    ck = Cockpit(entity=lambda ctx: {"name": "t"})
    ck.discover(team_discoverer(src), retire=True)
    ck.manifest(CRED)
    for pid in _panels(ck):
        ck.panel(pid)
    assert len(loads) == 1
