"""KE1b: the kernel's default cockpit is mock-e's layout (design 1011, "mock-e: what it decides"),
built from the real build_default_cockpit over a real install, not a hand fixture."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from levain.cockpit.providers import build_default_cockpit
from levain.dashboard import SubstrateSource
from tests.test_dashboard import _make_levain_install

CRED = {"class": "token", "name": "browser", "device_class": None}


@pytest.fixture
def regions(tmp_path):
    src = SubstrateSource.local(_make_levain_install(tmp_path))
    ck = build_default_cockpit(src, clock=lambda: datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc))
    m = ck.manifest(CRED)
    return m["regions"], m["panels"]


def _zone(r, zid):
    return next(z for z in r["zones"] if z["id"] == zid)


class TestDefaultLayout:
    def test_hero_is_empty_the_kernel_has_no_reading_or_ambient(self, regions) -> None:
        r, _ = regions
        assert r["hero"] == []

    def test_brief_holds_the_state_line(self, regions) -> None:
        r, _ = regions
        assert r["brief"] == ["state"]

    def test_health_is_a_widget(self, regions) -> None:
        r, heads = regions
        assert r["widgets"] == ["health"]
        assert heads["health"]["kind"] == "status-dots"
        assert "health" not in _zone(r, "mind")["panels"]

    def test_work_zone_order(self, regions) -> None:
        r, _ = regions
        ids = [c["panels"][0] for c in _zone(r, "operate")["cards"]]
        assert ids[:3] == ["now", "tray", "loops"]
        assert ids.index("loops") < ids.index("episodes") < ids.index("keep") < ids.index("edits")
        assert all(c["span"] == 1 and c["tabs"] is None for c in _zone(r, "operate")["cards"])

    def test_the_neocortex_sections_are_tabs_of_one_card_span_2(self, regions) -> None:
        r, heads = regions
        cards = _zone(r, "mind")["cards"]
        neo = cards[0]
        assert neo["tabs"] == "Neocortex" and neo["span"] == 2
        assert len(neo["panels"]) >= 2
        assert all(p.startswith("section:") for p in neo["panels"])
        assert [c["panels"] for c in cards[1:2]] == [["crystals"]]    # beside the card, not before it

    def test_the_mind_zone_ends_with_one_cognition_card(self, regions) -> None:
        r, _ = regions
        cards = _zone(r, "mind")["cards"]
        last = cards[-1]
        assert last["panels"] == ["graph", "wraps"] and last["tabs"] == "Cognition"
        assert last["size"] == "short" and last["span"] == 1
        assert not any(c["size"] == "short" for c in cards[:-1])

    def test_tray_and_loops_are_tall_the_rest_of_the_work_zone_medium(self, regions) -> None:
        r, _ = regions
        sizes = {c["panels"][0]: c["size"] for c in _zone(r, "operate")["cards"]}
        assert sizes["tray"] == "tall" and sizes["loops"] == "tall" and sizes["now"] == "medium"
        assert sizes["episodes"] == sizes["keep"] == "medium"

    def test_ranks_leave_the_embedder_gap_and_loops_clears_it(self, regions) -> None:
        _, heads = regions
        assert heads["tray"]["rank"] == 10 and heads["loops"]["rank"] == 15
        assert heads["loops"]["rank"] < 16 <= 39 < heads["episodes"]["rank"]
        assert heads["graph"]["rank"] == 200 and heads["wraps"]["rank"] == 210

    def test_every_panel_is_placed_exactly_once(self, regions) -> None:
        r, heads = regions
        placed = r["hero"] + r["brief"] + r["widgets"] + [p for z in r["zones"] for c in z["cards"] for p in c["panels"]]
        assert sorted(placed) == sorted(set(placed))
        assert set(placed) == set(heads)


@pytest.mark.parametrize("headings,want", [([], 0), (["State"], 1)])
def test_zero_or_one_neocortex_section(tmp_path, headings, want) -> None:
    root = _make_levain_install(tmp_path)
    body = "# Continuity — Sage\n\n" + "".join(f"## {h}\nbody\n\n" for h in headings)
    (root / ".levain" / "memory.continuity.md").write_text(body, encoding="utf-8")
    ck = build_default_cockpit(SubstrateSource.local(root),
                               clock=lambda: datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc))
    m = ck.manifest(CRED)
    assert not m.get("errors")
    cards = next(z for z in m["regions"]["zones"] if z["id"] == "mind")["cards"]
    neo = [c for c in cards if c["tabs"] == "Neocortex"]
    if want == 0:
        assert neo == []
    else:
        assert len(neo) == 1 and len(neo[0]["panels"]) == 1


class TestDiscoveredRankFollowsItsOwner:
    """L3 r1 (codex MED): a discovered panel's rank is its owner's answer per pass, like its verbs, so a
    heading reordered or inserted while the server runs moves its tab without a restart."""

    def test_reordered_headings_reorder_their_tabs_on_the_next_manifest(self) -> None:
        from levain.cockpit import Cockpit
        from tests.test_cockpit_k1 import NONE_CRED, _simple

        order = ["a", "b"]
        ck = Cockpit()
        ck.discover(lambda ctx: [_simple(h, region="mind", rank=i, tabs="Neocortex", span=2, size="tall")
                                 for i, h in enumerate(order)])
        first = _zone(ck.manifest(NONE_CRED)["regions"], "mind")["cards"][0]["panels"]
        assert first == ["a", "b"]
        order[:] = ["c", "b", "a"]                 # a heading inserted, two moved
        again = _zone(ck.manifest(NONE_CRED)["regions"], "mind")["cards"][0]["panels"]
        assert again == ["c", "b", "a"]

    def test_a_non_int_rank_on_a_later_pass_is_an_error_entry_and_the_last_rank_holds(self) -> None:
        from levain.cockpit import Cockpit
        from tests.test_cockpit_k1 import NONE_CRED, _simple

        rank: list[object] = [0]
        ck = Cockpit()
        ck.discover(lambda ctx: [_simple("a", rank=rank[0]), _simple("b", rank=1)])
        assert ck.manifest(NONE_CRED)["errors"] == []
        rank[0] = "bad"                            # codex r2: once a 500 in the manifest sort
        m = ck.manifest(NONE_CRED)
        assert [e["source"] for e in m["errors"]] == ["discovery:0"]
        assert m["panels"]["a"]["rank"] == 0
