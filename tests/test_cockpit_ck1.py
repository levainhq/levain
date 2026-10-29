"""CK1: the kind contract v2 (docs/cockpit-contract.md). One contract test per kind (the valid shape is
accepted, each malformed field is refused with the panel and the field named), the levain.cockpit/1
alias round-trip, the searchable property, the query bounds, and a text fallback for every kind."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from levain.cockpit import Cockpit, CockpitRegistrationError, ProviderSpec, Read, RowIn
from levain.cockpit.engine import (
    ALIASES, KINDS, SCHEMA, SEARCH_LIMIT_MAX, SEARCH_Q_MAX, SEARCH_THRESHOLD, QueryError, StoreMatches,
)
from levain.cockpit.routes import handle_get
from levain.cockpit.text import render_lines, snapshot

NOW = "2026-10-11T20:00:00+00:00"


def _value_panel(kind: str, value: Any, **kw: Any) -> Cockpit:
    ck = Cockpit()
    ck.register(ProviderSpec(id="p", kind=kind, title="P", priority="feed", read=lambda c: Read(value=value), **kw))
    return ck


def _ok(kind: str, value: Any, **kw: Any) -> dict[str, Any]:
    p = _value_panel(kind, value, **kw).panel("p")
    assert p is not None and p["status"] in ("ok", "empty"), p
    assert p["kind"] == kind
    return p


def _refused(kind: str, value: Any, *needles: str, **kw: Any) -> None:
    p = _value_panel(kind, value, **kw).panel("p")
    assert p is not None and p["status"] == "error", p
    for n in needles:
        assert n in p["error"], (n, p["error"])
    assert "'p'" in p["error"]       # the panel id is named


def test_the_thirteen_kinds_and_the_four_aliases() -> None:
    """Fails when a kind is added without this count AND the contract changing: every kind has its row in the contract table."""
    assert len(KINDS) == 13 and len(set(KINDS)) == 13 and KINDS[-1] == "board"
    contract = (Path(__file__).resolve().parent.parent / "docs" / "cockpit-contract.md").read_text()
    assert [k for k in KINDS if f"| `{k}` |" not in contract] == []
    assert ALIASES == {"triage-list": "list", "metric": "status-dots", "visual": "ambient", "prose": "brief"}
    assert set(ALIASES.values()) <= set(KINDS)
    assert SCHEMA == "levain.cockpit/2"


def test_an_unknown_kind_is_refused_at_registration() -> None:
    with pytest.raises(CockpitRegistrationError):
        Cockpit().register(ProviderSpec(id="p", kind="carousel", title="P", priority="feed", read=lambda c: Read(value={})))


# --- reading -----------------------------------------------------------------------------------

READING = {"value": 61, "unit": "F", "condition": "clear", "as_of": NOW, "hi": 70, "lo": 48,
           "sub": [{"label": "AQI", "value": 42, "band": "good"}]}


def test_reading_accepts_its_shape_and_refuses_each_malformed_field() -> None:
    _ok("reading", READING)
    _ok("reading", {**READING, "sub": None, "hi": None})
    for bad, field in (({"value": True}, "value.value"), ({"value": None}, "value.value"), ({"unit": 3}, "value.unit"),
                       ({"condition": None}, "value.condition"), ({"as_of": "yesterday"}, "value.as_of"),
                       ({"hi": "hot"}, "value.hi"), ({"sub": [{"value": 1}]}, "value.sub[0].label"),
                       ({"sub": [{"label": "x", "value": None}]}, "value.sub[0].value"),
                       ({"sub": [{"label": "x", "value": 1, "band": 2}]}, "value.sub[0].band")):
        _refused("reading", {**READING, **bad}, field)
    _refused("reading", "61F", "value")


# --- line, brief -------------------------------------------------------------------------------

def test_line_contract() -> None:
    _ok("line", {"lines": [{"label": "state", "text": "ok", "at": NOW}]})
    _refused("line", {"lines": [{"text": 5}]}, "value.lines[0].text")
    _refused("line", {"lines": "x"}, "value.lines")
    _refused("line", {}, "value.lines")


def test_brief_contract() -> None:
    _ok("brief", {"headline": "H", "markdown": "a\nb"})
    _ok("brief", {"lines": [{"label": "Begin", "text": "here"}]})
    _refused("brief", {"headline": "H"}, "markdown")
    _refused("brief", {"markdown": 4}, "value.markdown")
    _refused("brief", {"markdown": "x", "headline": 4}, "value.headline")
    _refused("brief", {"lines": [{"label": "L"}]}, "value.lines[0].text")


# --- status-dots, metric-grid ------------------------------------------------------------------

def test_status_dots_contract() -> None:
    _ok("status-dots", {"metrics": [{"label": "db", "status": "ok", "value": 3}, {"label": "x"}]})
    _refused("status-dots", {"metrics": [{"label": "db", "status": "green"}]}, "value.metrics[0].status")
    _refused("status-dots", {"metrics": [{"status": "ok"}]}, "value.metrics[0].label")
    _refused("status-dots", {"metrics": [], "alerts": [{"severity": "bad"}]}, "value.alerts[0].message")
    _refused("status-dots", {}, "value.metrics")


def test_metric_grid_contract() -> None:
    _ok("metric-grid", {"numbers": [{"label": "episodes", "value": 285, "unit": "today"}]})
    _refused("metric-grid", {"numbers": [{"label": "e", "value": "285"}]}, "value.numbers[0].value")
    _refused("metric-grid", {"numbers": [{"label": "e", "value": True}]}, "value.numbers[0].value")
    _refused("metric-grid", {"numbers": [{"value": 1}]}, "value.numbers[0].label")
    _refused("metric-grid", {"numbers": [{"label": "e", "value": 1, "unit": 4}]}, "value.numbers[0].unit")


# --- ambient (matches flow's cockpit_feed event contract) --------------------------------------

EVENT = {"kind": "commit", "at": NOW, "repo": "levain", "agent": None, "outcome": "ok", "_ts": 1.0}


def test_ambient_accepts_flows_event_shape_extra_keys_and_null_repo_agent() -> None:
    p = _ok("ambient", {"events": [EVENT, {**EVENT, "repo": None, "outcome": "failed"}], "legend": "dots are commits"})
    assert len(p["value"]["events"]) == 2


def test_ambient_refuses_each_malformed_event_field() -> None:
    for k in ("kind", "at", "repo", "agent", "outcome"):
        e = {x: y for x, y in EVENT.items() if x != k}
        _refused("ambient", {"events": [e], "legend": "l"}, f"value.events[0].{k}")
    for bad, field in (({"kind": ""}, "kind"), ({"at": "soon"}, "at"), ({"repo": 4}, "repo"), ({"agent": 4}, "agent"),
                       ({"outcome": "meh"}, "outcome")):
        _refused("ambient", {"events": [{**EVENT, **bad}], "legend": "l"}, f"value.events[0].{field}")
    _refused("ambient", {"events": [EVENT]}, "value.legend")
    _refused("ambient", {"events": [EVENT], "legend": "  "}, "value.legend")
    _refused("ambient", {"legend": "l"}, "value.events")


# --- roster (matches flow's fleet_roster.roster_rows) ------------------------------------------

ROW = {"name": "flow", "role": "main", "memory": "stateful", "cadence": "resident", "group": "core",
       "lifecycle": "active", "wrap_every": "1d", "silence_is_signal": True,
       "digest": {"available": True, "episodes_24h": 4, "store_bytes": 10}, "last_call": None}


def test_roster_accepts_flows_row_shape_and_digest_none_is_no_data() -> None:
    p = _ok("roster", {"rows": [ROW, {**ROW, "name": "anansi", "cadence": "on_call", "memory": "stateless",
                                       "digest": None, "group": None, "role": None, "wrap_every": None,
                                       "last_call": {"at": NOW, "outcome": "ok"}}]})
    assert p["count"] == 2 and p["value"]["rows"][1]["digest"] is None     # never a faked zero


def test_roster_refuses_each_malformed_field() -> None:
    for bad, field in (({"name": ""}, "name"), ({"memory": "sometimes"}, "memory"), ({"cadence": "daily"}, "cadence"),
                       ({"lifecycle": "asleep"}, "lifecycle"), ({"role": 3}, "role"), ({"group": 3}, "group"),
                       ({"silence_is_signal": "yes"}, "silence_is_signal"), ({"digest": 0}, "digest"),
                       ({"last_call": {"at": NOW}}, "last_call")):
        _refused("roster", {"rows": [{**ROW, **bad}]}, f"value.rows[0].{field}")
    _refused("roster", {"rows": [ROW, ROW]}, "value.rows[1].name")


def test_roster_retired_members_do_not_render_and_silence_defaults_from_cadence() -> None:
    r = {k: v for k, v in ROW.items() if k != "silence_is_signal"}
    p = _ok("roster", {"rows": [r, {**ROW, "name": "old", "lifecycle": "retired"}]})
    assert [x["name"] for x in p["value"]["rows"]] == ["flow"] and p["value"]["rows"][0]["silence_is_signal"] is True
    assert p["value"]["retired_hidden"] == 1 and p["count"] == 1


# --- form --------------------------------------------------------------------------------------

def test_form_contract() -> None:
    f = {"fields": [{"name": "message", "type": "text"}, {"name": "for", "type": "recipient", "enum": ["cli", "nightly"]},
                    {"name": "due", "type": "date", "label": "Due"}], "verb": "send_inbox"}
    _ok("form", f)
    _refused("form", {"fields": []}, "value.fields")
    _refused("form", {"fields": [{"name": "m", "type": "number"}]}, "value.fields[0].type")
    _refused("form", {"fields": [{"name": "", "type": "text"}]}, "value.fields[0].name")
    _refused("form", {"fields": [{"name": "a", "type": "text"}, {"name": "a", "type": "text"}]}, "value.fields[1].name")
    _refused("form", {"fields": [{"name": "for", "type": "recipient"}]}, "value.fields[0].enum")
    _refused("form", {"fields": [{"name": "for", "type": "recipient", "enum": []}]}, "value.fields[0].enum")
    _refused("form", {"fields": [{"name": "for", "type": "recipient", "enum": ["a", 3]}]}, "value.fields[0].enum")
    _refused("form", {"fields": [{"name": "m", "type": "text", "enum": ["a"]}]}, "value.fields[0].enum")
    _refused("form", {**f, "verb": 3}, "value.verb")


# --- log ---------------------------------------------------------------------------------------

LOG = {"rows": [{"verb": "snooze", "target": "spore-1238", "actor": "phill", "at": NOW},
                {"verb": "mystery", "target": "x", "actor": "a", "at": NOW}]}


def test_log_contract_and_sentences_fall_back_to_the_raw_verb() -> None:
    p = _ok("log", LOG)
    assert p["value"]["rows"][1]["sentence"] == "mystery · x"        # no label known: the raw verb
    for k in ("verb", "target", "actor"):
        _refused("log", {"rows": [{**LOG["rows"][0], k: ""}]}, f"value.rows[0].{k}")
    _refused("log", {"rows": [{**LOG["rows"][0], "at": "later"}]}, "value.rows[0].at")


def test_log_sentence_uses_the_manifest_verb_label() -> None:
    ck = _value_panel("log", LOG)

    class V:
        def verbs_map(self, cred: str, **k: Any) -> dict[str, Any]:
            return {"snooze": {"label": "Snoozed"}}

        def revision(self, auth: Any) -> str:
            return "r1"

        def panel_actions(self, *a: Any, **k: Any) -> list[Any]:
            return []

    ck._verb_view = V()    # type: ignore[assignment]
    rows = ck.panel("p")["value"]["rows"]
    assert rows[0]["sentence"] == "Snoozed · spore-1238" and rows[1]["sentence"] == "mystery · x"


# --- list / count-queue (the RowIn engine) -----------------------------------------------------

def _rowpanel(kind: str, n: int, **kw: Any) -> Cockpit:
    rows = tuple(RowIn(f"r{i}", f"title {i}", body="needle here" if i == 3 else "plain", facets={}, stored={"id": f"r{i}"})
                 for i in range(n))
    ck = Cockpit()
    ck.register(ProviderSpec(id="p", kind=kind, title="P", priority="feed", read=lambda c: Read(rows=rows),
                             order="time.desc", version_fields=("id",), **kw))
    return ck


@pytest.mark.parametrize("kind", ["list", "count-queue"])
def test_row_engine_kinds_carry_rows_and_a_count(kind: str) -> None:
    p = _rowpanel(kind, 3).panel("p")
    assert p["kind"] == kind and p["count"] == 3 and len(p["rows"]) == 3


def test_a_row_kind_needs_an_ordering_and_versions() -> None:
    with pytest.raises(CockpitRegistrationError):
        Cockpit().register(ProviderSpec(id="p", kind="count-queue", title="P", priority="feed", read=lambda c: Read(rows=())))


# --- the /1 alias round trip -------------------------------------------------------------------

def test_a_levain_cockpit_1_provider_renders_as_the_new_kinds() -> None:
    ck = Cockpit()
    ck.register(ProviderSpec(id="t", kind="triage-list", title="T", priority="feed", order="time.desc",
                             version_fields=("id",), read=lambda c: Read(rows=(RowIn("a", "A", stored={"id": "a"}),))))
    ck.register(ProviderSpec(id="m", kind="metric", title="M", priority="feed",
                             read=lambda c: Read(value={"metrics": [{"label": "x", "status": "ok", "value": 1}]})))
    ck.register(ProviderSpec(id="v", kind="visual", title="V", priority="feed",
                             read=lambda c: Read(value={"visual": "sky", "text": [{"label": "a", "text": "b"}]})))
    ck.register(ProviderSpec(id="r", kind="prose", title="R", priority="feed",
                             read=lambda c: Read(value={"headline": "h", "markdown": "m"})))
    man = ck.manifest({"class": "none", "device_class": None})
    assert man["schema"] == "levain.cockpit/2"
    assert {i: man["panels"][i]["kind"] for i in "tmvr"} == {"t": "list", "m": "status-dots", "v": "ambient", "r": "brief"}
    assert ck.panel("v")["value"]["visual"] == "sky"
    assert ck.panel("r")["value"]["markdown"] == "m" and ck.panel("r")["value"]["value_version"]
    # and the legacy shape is not valid for a NEW ambient
    _refused("ambient", {"visual": "sky", "text": []}, "value.events")


def test_the_manifest_etag_moves_with_the_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    import levain.cockpit.engine as eng
    ck = _value_panel("line", {"lines": []})
    cred = {"class": "none", "device_class": None}
    a = ck.manifest(cred)["etag"]
    monkeypatch.setattr(eng, "SCHEMA", "levain.cockpit/3")
    assert ck.manifest(cred)["etag"] != a


# --- searchable --------------------------------------------------------------------------------

def test_the_box_appears_past_the_threshold_and_cannot_be_turned_off() -> None:
    assert _rowpanel("list", SEARCH_THRESHOLD).panel("p")["searchable"] is False
    p = _rowpanel("list", SEARCH_THRESHOLD + 1).panel("p")
    assert p["searchable"] is True and p["search"]["fields"] == ["title", "body", "id"] and p["search"]["scope"] == "rows"
    # a windowed panel is searchable at any size
    w = _rowpanel("list", 2, window=50, search_scope="store").panel("p")
    assert w["searchable"] is True and w["window"] == 50
    # a non-row kind never is
    assert _value_panel("line", {"lines": []}).panel("p")["searchable"] is False


def test_the_threshold_counts_roster_and_log_rows_too() -> None:
    rows = [{**ROW, "name": f"a{i}"} for i in range(SEARCH_THRESHOLD + 1)]
    assert _value_panel("roster", {"rows": rows}).panel("p")["searchable"] is True
    logs = [{"verb": "v", "target": f"t{i}", "actor": "a", "at": NOW} for i in range(SEARCH_THRESHOLD)]
    assert _value_panel("log", {"rows": logs}).panel("p")["searchable"] is False


def test_rows_scope_filters_folded_terms_anded_over_the_declared_fields() -> None:
    ck = _rowpanel("list", 10, search_fields=("title", "body"))
    p = ck.panel("p", q="NEEDLE plain")
    assert p["matched"] == 0 and p["match_label"] == "0 of 10"
    p = ck.panel("p", q="NEEDLE")
    assert [r["id"] for r in p["rows"]] == ["r3"] and p["match_label"] == "1 of 10"
    assert len(ck.panel("p", q="title")["rows"]) == 10        # default plain text, not a pattern
    assert ck.panel("p", q=".*")["rows"] == []                 # no regex semantics


def test_the_validator_refuses_a_search_field_the_row_contract_does_not_carry() -> None:
    for kind, bad in (("list", "nonsense"), ("roster", "digest"), ("log", "sentence")):
        extra = {"order": "time.desc", "version_fields": ("id",)} if kind == "list" else {}
        with pytest.raises(CockpitRegistrationError, match="search field"):
            Cockpit().register(ProviderSpec(id="p", kind=kind, title="P", priority="feed", read=lambda c: Read(value={"rows": []}),
                                            search_fields=(bad,), **extra))
    with pytest.raises(CockpitRegistrationError, match="searchable"):
        Cockpit().register(ProviderSpec(id="p", kind="line", title="P", priority="feed", read=lambda c: Read(value={"lines": []}),
                                        search_fields=("title",)))
    with pytest.raises(CockpitRegistrationError, match="scope"):
        _rowpanel("list", 1, search_scope="galaxy")
    with pytest.raises(CockpitRegistrationError, match="window"):
        _rowpanel("list", 1, search_scope="store")
    with pytest.raises(CockpitRegistrationError, match="hook"):
        _rowpanel("list", 1, window=5, search=lambda q, n: [])


def test_a_declared_facet_is_a_searchable_field() -> None:
    rows = (RowIn("a", "A", facets={"tags": ["x", "y"]}, stored={"id": "a"}), RowIn("b", "B", facets={"tags": ["z"]}, stored={"id": "b"}))
    ck = Cockpit()
    ck.register(ProviderSpec(id="p", kind="list", title="P", priority="feed", read=lambda c: Read(rows=rows), order="time.desc",
                             version_fields=("id",), facets=frozenset({"tags"}), search_fields=("tags",)))
    assert [r["id"] for r in ck.panel("p", q="y")["rows"]] == ["a"]


def test_roster_and_log_rows_are_searched_by_their_own_fields() -> None:
    ck = _value_panel("roster", {"rows": [ROW, {**ROW, "name": "argus", "group": "ops"}]}, search_fields=("name", "group"))
    assert [r["name"] for r in ck.panel("p", q="ops")["value"]["rows"]] == ["argus"]
    ck = _value_panel("log", LOG)
    p = ck.panel("p", q="1238")
    assert [r["target"] for r in p["value"]["rows"]] == ["spore-1238"] and p["matched"] == 1


def test_store_scope_calls_the_hook_with_the_bounded_limit_and_labels_the_count() -> None:
    seen: list[tuple[str, int]] = []

    def hook(q: str, limit: int) -> StoreMatches:
        seen.append((q, limit))
        return StoreMatches(rows=(RowIn("old1", "old match", stored={"id": "old1"}),), total=41)

    ck = _rowpanel("list", 2, window=2, search_scope="store", search=hook)
    p = ck.panel("p", q="old", limit=10_000)
    assert seen == [("old", SEARCH_LIMIT_MAX)]
    assert [r["id"] for r in p["rows"]] == ["old1"] and p["matched"] == 41 and p["match_label"] == "41 matches in the store"
    assert ck.panel("p", q="old")["rows"][0]["id"] == "old1" and seen[-1][1] == 50       # default limit


def test_a_hook_that_fails_or_returns_junk_is_a_search_error_not_an_empty_answer() -> None:
    def boom(q: str, limit: int) -> Any:
        raise RuntimeError("db locked")

    p = _rowpanel("list", 2, window=2, search_scope="store", search=boom).panel("p", q="x")
    assert "db locked" in p["search_error"] and p["rows"] == []
    p = _rowpanel("list", 2, window=2, search_scope="store", search=lambda q, n: ["not a row"]).panel("p", q="x")
    assert "unusable rows" in p["search_error"]


def test_store_scope_for_roster_and_log_validates_hook_rows_with_the_kind_contract() -> None:
    ck = _value_panel("log", LOG, window=2, search_scope="store",
                      search=lambda q, n: [{"verb": "snooze", "target": "spore-1", "actor": "p", "at": NOW}])
    p = ck.panel("p", q="spore")
    assert p["match_label"] == "1 matches in the store" and p["value"]["rows"][0]["sentence"].endswith("spore-1")
    ck = _value_panel("log", LOG, window=2, search_scope="store", search=lambda q, n: [{"verb": ""}])
    assert "unusable rows" in ck.panel("p", q="spore")["search_error"]


def test_a_windowed_panel_with_no_hook_filters_its_window_and_says_so() -> None:
    p = _rowpanel("list", 10, window=10, search_scope="store").panel("p", q="needle")
    assert [r["id"] for r in p["rows"]] == ["r3"] and p["match_label"] == "searches the newest 10 only: 1 of 10"


def test_q_bounds() -> None:
    ck = _rowpanel("list", 3)
    ck.panel("p", q="x" * SEARCH_Q_MAX)
    with pytest.raises(QueryError):
        ck.panel("p", q="x" * (SEARCH_Q_MAX + 1))
    with pytest.raises(QueryError):
        ck.panel("p", q="a\x00b")
    with pytest.raises(QueryError):
        ck.panel("p", q="ok", limit=0)


def test_the_route_maps_a_bad_query_to_400_and_serves_a_good_one() -> None:
    ck = _rowpanel("list", 10)
    st, body, _ = handle_get(ck, "/cockpit/panel/p.json?q=" + "x" * 201, if_none_match=None)
    assert st == 400 and json.loads(body)["error"] == "bad_request"
    st, body, _ = handle_get(ck, "/cockpit/panel/p.json?q=needle&limit=5", if_none_match=None)
    assert st == 200 and json.loads(body)["matched"] == 1
    assert handle_get(ck, "/cockpit/panel/p.json?q=a&limit=many", if_none_match=None)[0] == 400
    # a different q is a different validator
    h1 = handle_get(ck, "/cockpit/panel/p.json?q=needle", if_none_match=None)[2]
    h2 = handle_get(ck, "/cockpit/panel/p.json?q=plain", if_none_match=None)[2]
    assert dict(h1)["ETag"] != dict(h2)["ETag"]


def test_q_is_not_written_to_the_response_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    ck = _rowpanel("list", 10)
    with caplog.at_level("DEBUG"):
        st, body, hdrs = handle_get(ck, "/cockpit/panel/p.json?q=SECRETQUERY", if_none_match=None)
    assert "SECRETQUERY" not in body.decode() and "SECRETQUERY" not in caplog.text
    assert all("SECRETQUERY" not in v for _, v in hdrs)


# --- text fallbacks ----------------------------------------------------------------------------

def _text(ck: Cockpit) -> str:
    return "\n".join(t for _s, t in render_lines(snapshot(ck), now=datetime(2026, 10, 11, 21, 0, tzinfo=timezone.utc)))


FIXTURES: list[tuple[str, Any, str]] = [
    ("reading", READING, "61 F, clear (hi 70 / lo 48)"),
    ("line", {"lines": [{"label": "state", "text": "fine", "at": NOW}]}, "state: fine"),
    ("brief", {"headline": "Head", "markdown": "body line", "lines": [{"label": "Begin", "text": "here"}]}, "Begin: here"),
    ("ambient", {"events": [EVENT, EVENT], "legend": "dots are commits"}, "2 events"),
    ("status-dots", {"metrics": [{"label": "db", "status": "warn", "value": 3}]}, "[warn] db: 3"),
    ("metric-grid", {"numbers": [{"label": "episodes", "value": 285}]}, "episodes: 285"),
    ("roster", {"rows": [ROW, {**ROW, "name": "anansi", "digest": None}]}, "flow — stateful, resident"),
    ("form", {"fields": [{"name": "for", "type": "recipient", "enum": ["cli", "nightly"]}]}, "one of: cli, nightly"),
    ("log", LOG, "mystery · x"),
]


@pytest.mark.parametrize("kind,value,expect", FIXTURES, ids=[f[0] for f in FIXTURES])
def test_every_value_kind_has_a_text_fallback(kind: str, value: Any, expect: str) -> None:
    out = _text(_value_panel(kind, value))
    assert expect in out and "no text form" not in out


def test_the_text_fallback_covers_the_row_kinds_and_the_legacy_visual() -> None:
    out = _text(_rowpanel("count-queue", 2))
    assert "title 0" in out
    ck = Cockpit()
    ck.register(ProviderSpec(id="v", kind="visual", title="V", priority="feed",
                             read=lambda c: Read(value={"visual": "sky", "text": [{"label": "a", "text": "b"}]})))
    assert "a: b" in _text(ck)


def test_a_search_result_label_reaches_the_text_view() -> None:
    ck = _rowpanel("list", 10)
    snap = snapshot(ck)
    snap["panels"]["p"] = ck.panel("p", q="needle")
    out = "\n".join(t for _s, t in render_lines(snap))
    assert "1 of 10" in out


# --- CK1 L3 r1 fixes ---------------------------------------------------------------------------

import threading
import time

from levain.cockpit.engine import check_query
import levain.cockpit.engine as engine


def _store(search: Any, **kw: Any) -> Cockpit:
    """A windowed list panel over an empty window whose store search is ``search``."""
    ck = Cockpit()
    ck.register(ProviderSpec(id="p", kind="list", title="P", priority="feed", read=lambda c: Read(rows=()),
                             order="time.desc", version_fields=("id",), window=2, search_scope="store",
                             search=search, **kw))
    return ck


def _hit(i: str = "old") -> RowIn:
    return RowIn(i, f"{i} match", stored={"id": i})


def test_f1_a_generator_hook_is_drained_inside_the_deadline_and_cut_at_the_limit() -> None:
    def slow(q: str, n: int) -> Any:
        time.sleep(0.6)
        yield _hit()

    t0 = time.monotonic()
    p = _store(slow, timeout_s=0.2).panel("p", q="x")
    assert time.monotonic() - t0 < 0.5 and "timed out" in p["search_error"]

    def endless(q: str, n: int) -> Any:
        i = 0
        while True:
            i += 1
            yield _hit(f"r{i}")

    p = _store(endless, timeout_s=2).panel("p", q="x", limit=3)
    assert [r["id"] for r in p["rows"]] == ["r1", "r2", "r3"]


def test_f1_a_hung_hook_holds_one_thread_per_panel_and_the_next_search_is_busy() -> None:
    release = threading.Event()
    calls: list[int] = []

    def hang(q: str, n: int) -> Any:
        calls.append(1)
        release.wait(5)
        return []

    ck = _store(hang, timeout_s=0.1)
    before = threading.active_count()
    errs = [ck.panel("p", q="x")["search_error"] for _ in range(6)]
    try:
        n = engine.SEARCH_INFLIGHT
        assert all("timed out" in e for e in errs[:n]) and errs[n:] == ["search busy"] * (6 - n)
        assert len(calls) == n and threading.active_count() - before <= n
    finally:
        release.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and "search_error" in ck.panel("p", q="x"):
        time.sleep(0.05)
    assert "search_error" not in ck.panel("p", q="x")      # the panel is searchable again once the hook ended


def test_f1_overlapping_searches_within_the_deadline_are_not_busy() -> None:
    """A newer query while an older one is still running is ordinary typing; only an abandoned hook makes a panel busy."""
    started, release = threading.Event(), threading.Event()

    def slow(q: str, n: int) -> Any:
        if q == "old":
            started.set()
            release.wait(2)
        return []

    ck = _store(slow, timeout_s=3)
    out: dict[str, Any] = {}
    t = threading.Thread(target=lambda: out.setdefault("old", ck.panel("p", q="old")))
    t.start()
    assert started.wait(2)
    try:
        assert "search_error" not in ck.panel("p", q="new")
    finally:
        release.set()
        t.join(3)
    assert "search_error" not in out["old"]


def test_f1_concurrent_searches_on_a_hung_hook_are_capped_per_panel() -> None:
    """Requests that all arrive before any timeout must not each start a worker (codex r2 reproduced four)."""
    release = threading.Event()
    calls: list[int] = []

    def hang(q: str, n: int) -> Any:
        calls.append(1)
        release.wait(5)
        return []

    ck = _store(hang, timeout_s=0.5)
    ts = [threading.Thread(target=ck.panel, args=("p",), kwargs={"q": f"x{i}"}) for i in range(6)]
    try:
        for t in ts:
            t.start()
        for t in ts:
            t.join(3)
        assert len(calls) <= getattr(engine, "SEARCH_INFLIGHT", 2)
    finally:
        release.set()


def test_f1_a_hook_that_raises_systemexit_is_a_search_error_not_an_escape() -> None:
    def quit_(q: str, n: int) -> Any:
        raise SystemExit(3)

    assert "search failed" in _store(quit_).panel("p", q="x")["search_error"]


def test_f2_the_now_view_advertises_a_rows_filter_only_when_it_has_rows() -> None:
    # 1012+100 item 2 (Phill 10-11: long panels carry filter/search): the Now view is row-shaped, so with rows it
    # carries the same box as Tray / Open loops / Keep, over the rows it holds (scope rows: the page filters them,
    # the kernel never searches a store for it) and never cut to a first screenful; with no rows it advertises none.
    ck = Cockpit()
    rows = [RowIn(f"s:{i}", f"r{i}", {"due": "2026-01-01", "disposition": "seed", "tier": "hot", "salience": 0,
                                       "handoff_expired": False}, stored={"id": f"s:{i}"}) for i in range(3)]
    ck.register(ProviderSpec(id="g", kind="triage-list", title="G", priority="gate", read=lambda c: Read(rows=tuple(rows)),
                             order="spore.tray", version_fields=("id",),
                             facets=frozenset({"due", "disposition", "tier", "salience", "handoff_expired"})))
    p = ck.panel("now")
    assert len(p["rows"]) == 3 and p["searchable"] is True
    assert p["search"] == {"fields": ["title", "body", "id"], "scope": "rows", "default_visible": 3, "hook": False}
    quiet = Cockpit()
    quiet.register(ProviderSpec(id="g", kind="list", title="G", priority="gate", order="time.desc", version_fields=("id",),
                                read=lambda c: Read(rows=())))
    p = quiet.panel("now")
    assert p["rows"] == [] and p["searchable"] is False and p["search"] is None


def test_f3_alerts_alone_are_content_and_an_empty_window_still_reports_the_search() -> None:
    p = _ok("status-dots", {"metrics": [], "alerts": [{"message": "disk full", "severity": "bad"}]})
    assert p["status"] == "ok"
    assert "disk full" in _text(_value_panel("status-dots", {"metrics": [], "alerts": [{"message": "disk full"}]}))
    ck = _store(lambda q, n: StoreMatches(rows=(_hit(),), total=1))
    p = ck.panel("p", q="old")
    assert p["status"] == "ok" and [r["id"] for r in p["rows"]] == ["old"]
    snap = snapshot(ck)
    snap["panels"]["p"] = p
    assert "old match" in "\n".join(t for _s, t in render_lines(snap))
    p = _store(lambda q, n: (_ for _ in ()).throw(RuntimeError("db locked"))).panel("p", q="old")
    snap["panels"]["p"] = p
    assert "db locked" in "\n".join(t for _s, t in render_lines(snap))
    p = _store(lambda q, n: []).panel("p", q="old")
    assert p["status"] == "empty" and p["match_label"] == "0 matches in the store"


def test_f4_a_refused_row_or_a_bad_total_is_a_search_error() -> None:
    ck = Cockpit()
    ck.register(ProviderSpec(id="p", kind="list", title="P", priority="feed", read=lambda c: Read(rows=()), order="time.desc",
                             version_fields=("id",), window=2, search_scope="store", facets=frozenset({"overdue_days"}),
                             search=lambda q, n: [RowIn("a", "a", facets={"overdue_days": "x"}, stored={"id": "a"})]))
    p = ck.panel("p", q="z")
    assert "refused" in p["search_error"] and p["rows"] == []
    for total in (0, "9", True, -1):
        p = _store(lambda q, n, t=total: StoreMatches(rows=(_hit(),), total=t)).panel("p", q="z")
        assert "search_error" in p and p["rows"] == [], total
    assert _store(lambda q, n: StoreMatches(rows=(_hit(),), total=1)).panel("p", q="z")["matched"] == 1


def test_f5_store_rows_carry_provenance() -> None:
    from levain.cockpit.authority import LabelKey

    class Snap:
        revision = None
        error = None
        degraded = 0

        def provenance(self, key: Any) -> Any:
            return {"label": "verified"} if key is not None else None

        def label(self, key: Any) -> str:
            return "verified" if key is not None else "unverified"

    ck = _store(lambda q, n: [_hit()], label_key=lambda stored: LabelKey("r", {"id": stored["id"]}))
    ck.attach_labeller(lambda: Snap())      # type: ignore[arg-type]
    assert ck.panel("p", q="z")["rows"][0]["provenance"] == {"label": "verified"}


def test_f6_a_searched_validator_covers_what_was_returned() -> None:
    state = {"ok": False}

    def flaky(q: str, n: int) -> Any:
        if not state["ok"]:
            raise RuntimeError("db locked")
        return [_hit()]

    ck = _store(flaky)
    _s, _b, h = handle_get(ck, "/cockpit/panel/p.json?q=z", if_none_match=None)
    etag = dict(h)["ETag"]
    state["ok"] = True
    assert handle_get(ck, "/cockpit/panel/p.json?q=z", if_none_match=etag)[0] == 200
    _s, _b, h2 = handle_get(ck, "/cockpit/panel/p.json?q=z", if_none_match=None)
    assert handle_get(ck, "/cockpit/panel/p.json?q=z", if_none_match=dict(h2)["ETag"])[0] == 304


def test_f7_the_route_validates_q_as_sent() -> None:
    ck = _rowpanel("list", 10)
    assert handle_get(ck, "/cockpit/panel/p.json?q=" + "x" * 200 + "%20", if_none_match=None)[0] == 400
    assert handle_get(ck, "/cockpit/panel/p.json?q=a%0A", if_none_match=None)[0] == 400
    assert handle_get(ck, "/cockpit/panel/p.json?q=", if_none_match=None)[0] == 200


def test_f9_nan_and_infinity_are_not_numbers() -> None:
    for bad in (float("nan"), float("inf")):
        _refused("metric-grid", {"numbers": [{"label": "x", "value": bad}]}, "value.numbers[0].value")
        _refused("reading", {**READING, "hi": bad}, "value.hi")


def test_f10_line_at_and_last_call_are_iso_and_the_outcome_is_ok_or_failed() -> None:
    _refused("line", {"lines": [{"text": "x", "at": "not a time"}]}, "value.lines[0].at")
    for lc in ({"at": "junk", "outcome": "ok"}, {"at": NOW, "outcome": ""}, {"at": NOW, "outcome": "maybe"}):
        _refused("roster", {"rows": [{**ROW, "last_call": lc}]}, "value.rows[0].last_call")
    _ok("roster", {"rows": [{**ROW, "last_call": {"at": NOW, "outcome": "failed"}}]})
    _refused("log", {"rows": [{"verb": "v", "target": "t", "actor": "a", "at": "junk"}]}, "value.rows[0].at")


def test_f11_legacy_kind_is_derived_never_passed_and_survives_a_verbs_copy() -> None:
    with pytest.raises(TypeError):
        ProviderSpec(id="p", kind="list", title="P", priority="feed", read=lambda c: Read(rows=()),
                     legacy_kind="visual")     # type: ignore[call-arg]
    spec = ProviderSpec(id="v", kind="visual", title="V", priority="feed", read=lambda c: Read(value={}))
    assert spec.kind == "ambient" and spec.legacy_kind == "visual"
    assert spec.with_verbs(("snooze",)).legacy_kind == "visual"
    assert ProviderSpec(id="a", kind="ambient", title="A", priority="feed", read=lambda c: Read(value={})).legacy_kind == ""


def test_f12_a_null_silence_is_signal_is_omitted() -> None:
    p = _ok("roster", {"rows": [{**ROW, "silence_is_signal": None},
                                {**ROW, "name": "b", "cadence": "on_call", "silence_is_signal": None}]})
    assert [r["silence_is_signal"] for r in p["value"]["rows"]] == [True, False]


def test_f13_text_projections() -> None:
    out = _text(_value_panel("brief", {"lines": [{"label": "L", "text": "hi"}]})).split("\n")
    assert not any(ln.strip() == "" and ln.startswith("    ") for ln in out)
    r = {**READING, "lo": None}
    assert "(hi 70)" in _text(_value_panel("reading", r))
    rows = [{"verb": "v", "target": f"t{i}", "actor": "a", "at": NOW} for i in range(60)]
    assert "+12 more" in _text(_value_panel("log", {"rows": rows}))
    t = _text(_value_panel("roster", {"rows": [{**ROW, "name": "g1", "group": "G"}, {**ROW, "name": "u1", "group": None}]}))
    assert t.index("(no group)") < t.index("u1")


def test_f14_a_query_with_no_folded_term_is_no_query() -> None:
    assert check_query("́")[0] is None and check_query("   ")[0] is None and check_query("a")[0] == "a"
    p = _rowpanel("list", 10).panel("p", q="́")
    assert "match_label" not in p and len(p["rows"]) == 10


def test_f15_a_status_dots_value_unit_and_read_are_renderable_types() -> None:
    for bad, field in (({"value": {"a": 1}}, "value"), ({"value": float("nan")}, "value"), ({"value": True}, "value"),
                       ({"unit": 3}, "unit"), ({"read": 3}, "read")):
        _refused("status-dots", {"metrics": [{"label": "x", **bad}]}, f"value.metrics[0].{field}")
    _ok("status-dots", {"metrics": [{"label": "x", "value": "3 of 4", "unit": "", "read": "fine"}, {"label": "y"}]})
