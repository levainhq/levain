"""CK1: the kind contract v2 (docs/cockpit-contract.md). One contract test per kind (the valid shape is
accepted, each malformed field is refused with the panel and the field named), the levain.cockpit/1
alias round-trip, the searchable property, the query bounds, and a text fallback for every kind."""

from __future__ import annotations

import json
from datetime import datetime, timezone
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


def test_the_eleven_kinds_and_the_four_aliases() -> None:
    assert len(KINDS) == 11 and len(set(KINDS)) == 11
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
        def verbs_map(self, cred: str) -> dict[str, Any]:
            return {"snooze": {"label": "Snoozed"}}

        def revision(self) -> str:
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
