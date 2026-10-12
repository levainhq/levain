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

    def test_the_mind_zone_ends_with_the_compact_row(self, regions) -> None:
        r, _ = regions
        cards = _zone(r, "mind")["cards"]
        assert [c["panels"] for c in cards[-2:]] == [["graph"], ["wraps"]]
        assert [c["size"] for c in cards[-2:]] == ["short", "short"]
        assert not any(c["size"] == "short" for c in cards[:-2])

    def test_every_panel_is_placed_exactly_once(self, regions) -> None:
        r, heads = regions
        placed = r["hero"] + r["brief"] + r["widgets"] + [p for z in r["zones"] for c in z["cards"] for p in c["panels"]]
        assert sorted(placed) == sorted(set(placed))
        assert set(placed) == set(heads)
