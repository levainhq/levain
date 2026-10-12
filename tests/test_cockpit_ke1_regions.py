"""KE1: the hero / brief / widgets regions, the zone grid and the reading tokens (additive to
``levain.cockpit/2``). Each refusal below is the registry's own: register, assert the raise and its text."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from levain.cockpit import Cockpit, CockpitRegistrationError, ProviderSpec, Read
from levain.cockpit.engine import READING_BANDS, READING_SYMBOLS, NOW_ID

CRED = {"class": "none", "device_class": None}
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def _reading(symbol=None, sub=None):
    v = {"value": 61, "unit": "F", "condition": "Cloudy", "as_of": NOW.isoformat()}
    if symbol is not None:
        v["symbol"] = symbol
    if sub is not None:
        v["sub"] = sub
    return lambda ctx: Read(value=v)


def _line(ctx):
    return Read(value={"lines": [{"text": "ok"}]})


def _rows(ctx):
    return Read(rows=())


def _spec(pid, kind, region, **kw):
    read = {"reading": _reading(), "line": _line}.get(kind, _line)
    if kind in ("list", "triage-list"):
        kw.setdefault("order", "time.desc")
        kw.setdefault("facets", frozenset({"at"}))
        kw.setdefault("version_fields", ("id",))
        read = _rows
    return ProviderSpec(pid, kind, pid.title(), "gauge", read, region=region, **kw)


def _ck():
    return Cockpit(clock=lambda: NOW)


def _refused(ck, spec, *needles):
    with pytest.raises(CockpitRegistrationError) as ei:
        ck.register(spec)
    for n in needles:
        assert n in str(ei.value)


def _regions(ck):
    return ck.manifest(CRED)["regions"]


class TestHero:
    def test_reading_and_ambient_register_and_lead_the_hero(self):
        ck = _ck()
        ck.register(_spec("sky", "ambient", "hero", rank=0))
        ck.register(_spec("weather", "reading", "hero", rank=5))
        assert _regions(ck)["hero"] == ["weather", "sky"]      # the reading first, whatever the rank

    def test_a_second_reading_is_refused(self):
        ck = _ck()
        ck.register(_spec("w1", "reading", "hero"))
        _refused(ck, _spec("w2", "reading", "hero"), "at most one reading")

    def test_other_kinds_are_refused(self):
        _refused(_ck(), _spec("l", "line", "hero"), "'line'")

    def test_the_slot_frees_when_the_holder_unregisters(self):
        ck = _ck()
        ck.register(_spec("w1", "reading", "hero"))
        with ck._lock:
            ck._unregister_locked("w1")
        ck.register(_spec("w2", "reading", "hero"))
        assert _regions(ck)["hero"] == ["w2"]


class TestBriefAndWidgets:
    def test_brief_takes_line_and_brief_only(self):
        ck = _ck()
        ck.register(_spec("state", "line", "brief"))
        _refused(ck, _spec("w", "reading", "brief"), "'reading'")
        assert _regions(ck)["brief"] == ["state"]

    def test_a_list_in_widgets_is_refused_with_the_rule(self):
        _refused(_ck(), _spec("inbox", "list", "widgets"),
                 "a widget is one answer, never a list", "face='count'")

    def test_a_triage_list_with_count_face_registers(self):
        ck = _ck()
        ck.register(_spec("inbox", "triage-list", "widgets", face="count"))
        assert _regions(ck)["widgets"] == ["inbox"]
        head = ck.manifest(CRED)["panels"]["inbox"]
        assert head["kind"] == "list" and head["face"] == "count"

    @pytest.mark.parametrize("kind", ["ambient", "roster", "log", "form", "brief"])
    def test_refused_kinds_name_the_kind(self, kind):
        _refused(_ck(), _spec("p", kind, "widgets"), f"a {kind} cannot sit in widgets", "never a list")

    @pytest.mark.parametrize("kind", ["reading", "line", "status-dots", "metric-grid"])
    def test_allowed_kinds_register(self, kind):
        _ck().register(_spec("p", kind, "widgets"))

    def test_face_count_only_on_a_row_engine_kind(self):
        _refused(_ck(), _spec("l", "line", "operate", face="count"), "face 'count' belongs to a row-engine kind")

    def test_face_value_is_validated(self):
        _refused(_ck(), _spec("l", "list", "operate", face="big"), "face must be")

    def test_no_face_key_on_a_head_without_one(self):
        ck = _ck()
        ck.register(_spec("l", "line", "brief"))
        assert "face" not in ck.manifest(CRED)["panels"]["l"]


class TestZoneLayout:
    def test_defaults_and_card_shape(self):
        ck = _ck()
        ck.register(_spec("a", "line", "mind", rank=1))
        z = {z["id"]: z for z in _regions(ck)["zones"]}
        assert z["mind"]["grid"] == 3 and z["mind"]["panels"] == ["a"]
        assert z["mind"]["cards"] == [{"panels": ["a"], "size": "medium", "span": 1, "tabs": None}]

    def test_now_is_its_own_first_card_in_operate(self):
        ck = _ck()
        ck.register(_spec("a", "line", "operate", size="tall", span=2))
        op = _regions(ck)["zones"][0]
        assert op["panels"] == [NOW_ID, "a"]
        assert op["cards"] == [{"panels": [NOW_ID], "size": "medium", "span": 1, "tabs": None},
                               {"panels": ["a"], "size": "tall", "span": 2, "tabs": None}]

    def test_tabs_share_one_card_in_rank_order(self):
        ck = _ck()
        ck.register(_spec("b", "line", "mind", rank=2, tabs="Health", size="tall", span=2))
        ck.register(_spec("a", "line", "mind", rank=1, tabs="Health", size="tall", span=2))
        ck.register(_spec("c", "line", "mind", rank=3))
        cards = next(z for z in _regions(ck)["zones"] if z["id"] == "mind")["cards"]
        assert cards[0] == {"panels": ["a", "b"], "size": "tall", "span": 2, "tabs": "Health"}
        assert cards[1]["panels"] == ["c"]

    def test_tabs_disagreeing_on_size_are_refused_naming_both(self):
        ck = _ck()
        ck.register(_spec("a", "line", "mind", tabs="T", size="tall"))
        _refused(ck, _spec("b", "line", "mind", tabs="T", size="short"), "'a'", "b", "size=tall", "size=short")

    def test_tabs_disagreeing_on_span_are_refused(self):
        ck = _ck()
        ck.register(_spec("a", "line", "mind", tabs="T", span=2))
        _refused(ck, _spec("b", "line", "mind", tabs="T", span=3), "span=2", "span=3")

    def test_same_title_in_another_zone_is_a_different_card(self):
        ck = _ck()
        ck.register(_spec("a", "line", "mind", tabs="T", size="tall"))
        ck.register(_spec("b", "line", "identity", tabs="T", size="short"))

    @pytest.mark.parametrize("kw", [{"size": "huge"}, {"span": 4}, {"span": True}, {"span": 0}])
    def test_bad_size_or_span_refused(self, kw):
        _refused(_ck(), _spec("a", "line", "mind", **kw), "must be one of")

    @pytest.mark.parametrize("region", ["hero", "brief", "widgets"])
    @pytest.mark.parametrize("kw", [{"size": "tall"}, {"span": 2}, {"tabs": "T"}])
    def test_layout_on_a_top_region_panel_is_refused(self, region, kw):
        kind = {"hero": "reading", "brief": "line", "widgets": "line"}[region]
        _refused(_ck(), _spec("a", kind, region, **kw), "lay out a zone card")

    def test_unknown_region_still_refused(self):
        _refused(_ck(), _spec("a", "line", "nowhere"), "unknown region 'nowhere'")


class TestLegacyHeader:
    def test_reading_line_and_list_map(self):
        ck = _ck()
        ck.register(_spec("weather", "reading", "header", rank=1))
        ck.register(_spec("sky", "ambient", "header", rank=2))
        ck.register(_spec("state", "line", "header", rank=0))
        ck.register(_spec("odd", "list", "header"))
        specs = ck._specs
        assert [(specs[i].region, specs[i].legacy_region) for i in ("weather", "sky", "state", "odd")] == [
            ("hero", "header"), ("hero", "header"), ("brief", "header"), ("operate", "header")]
        r = _regions(ck)
        assert r["hero"] == ["weather", "sky"] and r["brief"] == ["state"]
        assert r["header"] == ["weather", "sky", "state"]       # hero then brief, derived
        assert "odd" in r["zones"][0]["panels"]

    def test_a_second_legacy_reading_goes_to_operate(self):
        ck = _ck()
        ck.register(_spec("w1", "reading", "header"))
        ck.register(_spec("w2", "reading", "header"))
        assert ck._specs["w2"].region == "operate" and ck._specs["w2"].legacy_region == "header"
        assert _regions(ck)["hero"] == ["w1"]

    def test_a_declared_hero_after_a_legacy_one_is_refused(self):
        ck = _ck()
        ck.register(_spec("w1", "reading", "header"))
        _refused(ck, _spec("w2", "reading", "hero"), "at most one reading")

    def test_alias_and_header_normalise_together(self):
        ck = _ck()
        ck.register(_spec("p", "prose", "header"))
        s = ck._specs["p"]
        assert (s.kind, s.legacy_kind, s.region, s.legacy_region) == ("brief", "prose", "brief", "header")

    def test_with_verbs_carries_legacy_region(self):
        ck = _ck()
        ck.register(_spec("state", "line", "header"))
        out = ck._specs["state"].with_verbs(("operator_state",))
        assert (out.region, out.legacy_region) == ("brief", "header")

    def test_legacy_region_is_not_an_argument(self):
        with pytest.raises(TypeError):
            ProviderSpec("x", "line", "X", "gauge", _line, legacy_region="header")


class TestEtag:
    def test_a_layout_change_moves_the_manifest_etag(self):
        a, b = _ck(), _ck()
        a.register(_spec("x", "line", "mind", size="tall"))
        b.register(_spec("x", "line", "mind", size="short"))
        assert a.manifest(CRED)["etag"] != b.manifest(CRED)["etag"]


class TestReadingTokens:
    def test_token_sets_are_lowercase(self):
        assert all(t == t.lower() for t in READING_SYMBOLS + READING_BANDS)
        assert "good" in READING_BANDS and "partly-cloudy-night" in READING_SYMBOLS

    def test_a_known_symbol_and_detail_pass(self):
        ck = _ck()
        ck.register(ProviderSpec("w", "reading", "W", "gauge", _reading(
            symbol="partly-cloudy", sub=[{"label": "AQI", "value": 42, "band": "Good",
                                          "detail": "EPA/AirNow measured · Columbus"}]), region="hero"))
        out = ck.panel("w")
        assert out["status"] == "ok" and out["value"]["symbol"] == "partly-cloudy"

    def test_unknown_symbol_marks_the_panel_error_naming_it(self):
        ck = _ck()
        ck.register(ProviderSpec("w", "reading", "W", "gauge", _reading(symbol="cloud.sun.fill"), region="hero"))
        out = ck.panel("w")
        assert out["status"] == "error" and "value.symbol" in out["error"] and "cloud.sun.fill" in out["error"]

    def test_detail_must_be_a_string(self):
        ck = _ck()
        ck.register(ProviderSpec("w", "reading", "W", "gauge", _reading(
            sub=[{"label": "AQI", "value": 42, "detail": 7}]), region="hero"))
        out = ck.panel("w")
        assert out["status"] == "error" and "detail" in out["error"]
