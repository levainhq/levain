"""THE LIST (levain.cockpit.list_panel): the Tray's panel becomes a view over band + board + spores. Read-only data, no authority.

Pinned here: the three stores merge into ONE panel (wire id ``tray``, title ``List``); talk seeds oldest first with age; a timed
``next`` is overdue at its minute (``spores._parse_when``, never a date-prefix read); held rows stay held; band and board rows
are read-only and offered NO spore verb; an unreadable band or board is named, never an empty look; the frozen fixture
``tests/fixtures/list_panel.json`` (what the page lane renders against) is what the live panel produces."""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from anneal_memory.spores import SporeStore

from levain import band_view
from levain.cockpit import list_panel
from levain.cockpit.install import REPORTED as INSTALL_CLASS
from levain.cockpit.providers import build_default_cockpit
from levain.cockpit.results import Absent, Read
from levain.cockpit.verbs import VerbRegistry, VerbView, dispatch
from levain.consent.questions import QuestionStore
from levain.desk import ready
from levain.desk.store import CoordStore
from levain.dashboard import SubstrateSource
from levain.writes import EditError
from tests.test_dashboard import _make_levain_install

FIXTURE = Path(__file__).parent / "fixtures" / "list_panel.json"
CRED = {"class": "token", "name": "browser", "device_class": None}
# local noon on a fixed day: +/- an hour stays on the same local day whatever the machine's zone
NOW = datetime(2026, 11, 8, 12, 0).astimezone()
TODAY = NOW.date()


def _local(delta_hours: float = 0.0) -> str:
    return (NOW + timedelta(hours=delta_hours)).astimezone().replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M")


class Rig:
    """An install with spores, a desk store and a question store; the cockpit's clock is NOW."""

    def __init__(self, tmp_path: Path, *, spores: bool = True, band: bool = True, board: bool = True) -> None:
        self.root = _make_levain_install(tmp_path)
        self.src = SubstrateSource.local(self.root)
        self.qdir = tmp_path / "q"
        self.qdir.mkdir(mode=0o700)
        self.sp = SporeStore(self.root / ".levain" / "memory.spores.json")
        self.ids: dict[str, str] = {}
        if spores:
            self._spores()
        self.qs = QuestionStore(self.qdir, clock=lambda: NOW)   # the store files rows on the same pinned clock as the cockpit
        if band:
            self.q = self.qs.file(agent="claude_code", agent_session_id="sess-1", what="Ship the sweep fix tonight?",
                                  options=[("ship", "Ship it"), ("hold", "Hold")], rec="hold",
                                  blocks="the nightly sweep stays red", cwd="/w/levain")
        if board:
            st = CoordStore(self.qdir)
            tok = st.coord_take()
            kw = dict(repo="levain", who="desk", why="queued it.")
            ready.add(st, tok, "levain:a", title="Land A", to="ready", tier="sonnet", done_line="green", **kw)
            ready.add(st, tok, "levain:b", title="Land B", to="ready", tier="opus", done_line="green", **kw)
            ready.add(st, tok, "levain:c", title="Later C", to="backlog", **kw)
        self.ck = build_default_cockpit(self.src, clock=lambda: NOW)

        def reader(_now: Any) -> Any:                      # the consent reader: only its directory is the List's business
            raise RuntimeError("not read here")

        reader.directory = self.qdir                       # type: ignore[attr-defined]
        self.ck.attach_consent_rows(reader)
        self.ck.attach_question_rows(self.qs)
        self.reg = VerbRegistry()
        self.ck.attach_verbs(VerbView(self.reg, self.ck, INSTALL_CLASS))

    def _spores(self) -> None:
        add = self.sp.add
        t = "task"
        self.ids["plain"] = add(type=t, text="a plain seed", disposition="seed", today=TODAY)["id"]
        self.ids["talk_old"] = add(type=t, text="talk about the cockpit", disposition="seed", attention="talk",
                                   today=TODAY - timedelta(days=12))["id"]
        self.ids["talk_new"] = add(type=t, text="talk about the water", disposition="seed", attention="talk",
                                   today=TODAY - timedelta(days=2))["id"]
        self.ids["drop_late"] = add(type=t, text="call Sunil", disposition="agenda", target="desk", next=_local(-1),
                                    today=TODAY - timedelta(days=3))["id"]
        self.ids["drop_soon"] = add(type=t, text="send the invoice", disposition="agenda", target="desk",
                                    next=_local(1), today=TODAY - timedelta(days=1))["id"]
        self.ids["drop_later"] = add(type=t, text="renew the domain", disposition="agenda", target="desk",
                                     next="2026-11-10T09:00", today=TODAY)["id"]
        self.ids["agenda_old"] = add(type=t, text="an agenda from last week", disposition="agenda", next="2026-11-01",
                                     today=TODAY - timedelta(days=9))["id"]
        self.ids["loop"] = add(type=t, text="a machine-side loop", tier="hot", today=TODAY)["id"]

    def panel(self, cred: str = "token") -> dict[str, Any]:
        return self.ck.panel("tray", credential_class=cred)

    def rows(self) -> dict[str, dict[str, Any]]:
        return {r["id"]: r for r in self.panel()["rows"]}


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


# --- one panel, three surfaces -----------------------------------------------------------------------------------


def test_the_three_stores_are_one_panel_titled_list_and_each_row_names_its_surface(rig: Rig) -> None:
    p = rig.panel()
    assert p["id"] == "tray" == list_panel.LIST_PANEL_ID and p["title"].startswith("List")
    by_surface: dict[str, list[str]] = {}
    for r in p["rows"]:
        by_surface.setdefault(r["facets"]["surface"], []).append(r["id"])
    assert sorted(by_surface) == ["band", "board", "drop", "spore", "talk"]
    assert by_surface["band"] == [f"band:{rig.q.id}"]
    assert sorted(by_surface["board"]) == ["board:levain:a", "board:levain:b"]       # the ready column; backlog is not the List's
    assert sorted(by_surface["talk"]) == sorted(f"spore:{rig.ids[k]}" for k in ("talk_old", "talk_new"))
    assert sorted(by_surface["drop"]) == sorted(f"spore:{rig.ids[k]}" for k in ("drop_late", "drop_soon", "drop_later"))
    assert f"spore:{rig.ids['loop']}" not in {r["id"] for r in p["rows"]}            # Open loops stays machine-side
    assert [g["id"] for g in p["groups"]] == ["band", "desk", "later", "overdue", "today", "board", "talk", "also", "someday", "done"]
    assert p["note"] == list_panel.LIST_NOTE


def test_the_groups_put_band_first_then_what_is_late_then_today_then_ready_then_talks(rig: Rig) -> None:
    groups = [r["group"] for r in rig.panel()["rows"]]
    order = [g["id"] for g in rig.panel()["groups"]]
    assert groups == sorted(groups, key=order.index)             # the server's order leads
    by = {r["id"]: r["group"] for r in rig.panel()["rows"]}
    assert by[f"band:{rig.q.id}"] == "band"
    assert by[f"spore:{rig.ids['drop_late']}"] == "overdue" and by[f"spore:{rig.ids['agenda_old']}"] == "overdue"
    assert by[f"spore:{rig.ids['drop_soon']}"] == "today"
    assert by["board:levain:a"] == "board" and by[f"spore:{rig.ids['talk_old']}"] == "talk"
    assert by[f"spore:{rig.ids['plain']}"] == "someday" and by[f"spore:{rig.ids['drop_later']}"] == "later"


def test_talk_seeds_are_listed_oldest_first_with_their_age(rig: Rig) -> None:
    talk = [r for r in rig.panel()["rows"] if r["group"] == "talk"]
    assert [r["id"] for r in talk] == [f"spore:{rig.ids['talk_old']}", f"spore:{rig.ids['talk_new']}"]
    assert [r["facets"]["age_days"] for r in talk] == [12, 2]
    assert [r["facets"]["attention"] for r in talk] == ["talk", "talk"]
    assert {"kind": "age_days", "value": 12} in talk[0]["badges"]


def test_a_timed_next_is_overdue_at_its_minute_and_a_later_one_is_not(rig: Rig) -> None:
    rows = rig.rows()
    late = rows[f"spore:{rig.ids['drop_late']}"]["facets"]
    soon = rows[f"spore:{rig.ids['drop_soon']}"]["facets"]
    assert late["overdue"] is True and late["fires"] == _local(-1) and late["target"] == "desk"
    assert soon["overdue"] is False and soon["fires"] == _local(1)
    assert rows[f"spore:{rig.ids['drop_late']}"]["emphasis"] == "accent"
    assert rows[f"spore:{rig.ids['drop_soon']}"]["emphasis"] != "accent"
    # a date-only next before today is overdue, with no `fires`
    old = rows[f"spore:{rig.ids['agenda_old']}"]["facets"]
    assert old["overdue"] is True and old["fires"] is None and old["overdue_days"] == 7
    # the same minute, read a minute later: the row flips
    later = NOW + timedelta(hours=1, minutes=1)
    ck = build_default_cockpit(rig.src, clock=lambda: later)
    flipped = {r["id"]: r for r in ck.panel("tray", credential_class="token")["rows"]}
    assert flipped[f"spore:{rig.ids['drop_soon']}"]["facets"]["overdue"] is True


def test_a_timed_next_on_a_future_day_is_later_exactly_as_a_dated_one_is(rig: Rig) -> None:
    p = rig.panel()
    later = [r for r in p["rows"] if r["group"] == "later"]
    assert [r["id"] for r in later] == [f"spore:{rig.ids['drop_later']}"]
    assert later[0]["facets"]["due"] == "2026-11-10" and later[0]["facets"]["time_state"] == "later"
    assert later[0]["facets"]["fires"] == "2026-11-10T09:00" and later[0]["facets"]["surface"] == "drop"
    assert "held" not in p and p["filtered"] == []


def test_a_malformed_timed_next_is_never_read_as_a_date(tmp_path: Path) -> None:
    r = Rig(tmp_path, spores=False, band=False, board=False)
    r.ids["bad"] = r.sp.add(type="task", text="hand edited", disposition="agenda", next="2026-11-01", today=TODAY)["id"]
    path = r.root / ".levain" / "memory.spores.json"          # anneal refuses this on write; a hand edit can still make it
    path.write_text(path.read_text(encoding="utf-8").replace('"2026-11-01"', '"2026-11-01T25:61"'), encoding="utf-8")
    row = r.rows()[f"spore:{r.ids['bad']}"]
    assert row["facets"]["due"] is None and row["facets"]["fires"] is None and row["facets"]["overdue"] is False


# --- read-only band and board rows -------------------------------------------------------------------------------


def test_band_and_board_rows_are_read_only_labelled_and_point_at_where_they_are_answered(rig: Rig) -> None:
    rows = rig.rows()
    band = rows[f"band:{rig.q.id}"]
    assert band["facets"]["list_pointer"] == list_panel.BAND_POINTER and "Decisions" in band["facets"]["list_pointer"]
    assert band["facets"]["band_number"] is None and band["facets"]["source"] == "levain · claude_code"
    assert band["title"] == "Ship the sweep fix tonight?" and band["body"] == "the nightly sweep stays red"
    board = rows["board:levain:a"]
    assert board["title"] == "Land A" and board["body"] == "green" and board["facets"]["repo"] == "levain"
    assert board["facets"]["board_tier"] == "sonnet" and board["facets"]["list_pointer"] == list_panel.BOARD_POINTER


def test_no_spore_verb_is_offered_on_a_band_or_board_row_and_none_can_be_fired_on_one(rig: Rig) -> None:
    rows = rig.rows()
    for rid in (f"band:{rig.q.id}", "board:levain:a", "board:levain:b"):
        assert rows[rid]["actions"] == [], rid
    spore = rows[f"spore:{rig.ids['plain']}"]
    assert {a["verb"] for a in spore["actions"]} >= {"spore_touch", "spore_descend", "spore_surface_at"}
    panel_verbs = {a["verb"] for a in rig.panel()["actions"]}
    assert "spore_seed" in panel_verbs
    for rid in (f"band:{rig.q.id}", "board:levain:a"):
        for verb in ("spore_touch", "spore_descend", "spore_surface_at", "spore_update", "spore_set_disposition"):
            with pytest.raises(EditError):
                dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope, route="cockpit",
                         credential=CRED, install_class=INSTALL_CLASS,
                         req={"verb": verb, "panel_id": "tray", "row_id": rid, "row_version": rows[rid]["version"],
                              "params": {"surface_at": "2026-12-01"} if verb == "spore_surface_at" else {}, "confirm": True})


def test_read_one_finds_a_spore_even_when_held_and_has_no_write_row_for_band_or_board(rig: Rig) -> None:
    ctx_read = rig.ck.read_one
    assert isinstance(ctx_read("tray", f"spore:{rig.ids['drop_later']}"), Read)           # held, but a write finds it
    assert isinstance(ctx_read("tray", f"spore:{rig.ids['talk_old']}"), Read)
    assert isinstance(ctx_read("tray", "spore:ghost"), Absent)
    assert isinstance(ctx_read("tray", f"band:{rig.q.id}"), Absent)
    assert isinstance(ctx_read("tray", "board:levain:a"), Absent)


def test_a_spore_rows_version_is_the_one_a_write_binds_to(rig: Rig) -> None:
    from levain.cockpit.providers import spore_row_version

    row = rig.rows()[f"spore:{rig.ids['plain']}"]
    assert row["version"] == spore_row_version(rig.sp.get(rig.ids["plain"]))


# --- faults are named, never an empty look -----------------------------------------------------------------------


def test_an_unreadable_band_is_named_and_the_rest_still_shows(rig: Rig, monkeypatch) -> None:
    def boom(_store: Any) -> Any:
        raise OSError("band db locked")

    monkeypatch.setattr(band_view, "_read", boom)
    p = rig.panel()
    assert p["status"] == "partial"
    assert any("needs-you band could not be read" in s["reason"] for s in p["skipped"]), p["skipped"]
    assert not [r for r in p["rows"] if r["facets"]["surface"] == "band"]
    assert [r for r in p["rows"] if r["facets"]["surface"] == "board"] and [r for r in p["rows"] if r["facets"]["surface"] == "talk"]


def test_an_unreadable_board_is_named_and_the_rest_still_shows(rig: Rig, monkeypatch) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise OSError("dispatch.db locked")

    monkeypatch.setattr(ready, "snapshot", boom)
    p = rig.panel()
    assert p["status"] == "partial"
    assert any("agents' board could not be fully read (desk" in s["reason"] for s in p["skipped"]), p["skipped"]
    assert not [r for r in p["rows"] if r["facets"]["surface"] == "board"]
    assert [r for r in p["rows"] if r["facets"]["surface"] == "band"]


def test_nothing_readable_is_never_an_empty_look(tmp_path: Path, monkeypatch) -> None:
    r = Rig(tmp_path, spores=False, board=False)
    monkeypatch.setattr(band_view, "_read", lambda _s: (_ for _ in ()).throw(OSError("locked")))
    p = r.panel()
    assert p["status"] == "partial" and p["rows"] == [] and p["skipped"], p       # a fault line, not "Nothing yet"


def test_a_spore_store_that_cannot_be_read_is_a_fault_alone_and_a_named_skip_beside_other_rows(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    r = Rig(tmp_path / "a", spores=False, band=False, board=False)
    (r.root / ".levain" / "memory.spores.json").write_text("{not json", encoding="utf-8")
    p = r.panel()
    assert p["status"] == "error" and "spores" in (p["error"] or ""), p
    (tmp_path / "b").mkdir()
    r2 = Rig(tmp_path / "b", spores=False)
    (r2.root / ".levain" / "memory.spores.json").write_text("{not json", encoding="utf-8")
    p2 = r2.panel()
    assert p2["status"] == "partial" and [x for x in p2["rows"] if x["facets"]["surface"] == "board"]
    assert any(s["reason"].startswith("spores") for s in p2["skipped"])


def test_a_spore_store_seen_then_gone_is_still_a_fault(tmp_path: Path) -> None:
    r = Rig(tmp_path, band=False, board=False)
    assert r.panel()["status"] != "error"
    (r.root / ".levain" / "memory.spores.json").unlink()
    assert r.panel()["status"] == "error"


def test_an_empty_install_is_empty_with_the_entity_sentence_not_a_fault(tmp_path: Path) -> None:
    r = Rig(tmp_path, spores=False, band=False, board=False)
    p = r.panel()
    assert p["status"] == "empty" and p["rows"] == [] and p["empty"].startswith("Nothing yet.")


# --- the registered shape ----------------------------------------------------------------------------------------


def test_the_panel_is_registered_where_the_tray_was_and_loops_is_untouched(rig: Rig) -> None:
    spec = rig.ck.spec("tray")
    assert spec.title == "List" and spec.order == "spore.list" and spec.rank == 10 and spec.region == "operate"
    assert "list_intake" in spec.verbs and "spore_seed" in spec.verbs
    loops = rig.ck.spec("loops")
    assert loops.title == "Open loops" and loops.order == "spore.loops"
    assert {r["id"] for r in rig.ck.panel("loops", credential_class="token")["rows"]} == {f"spore:{rig.ids['loop']}"}


# --- the frozen fixture the page lane renders against ------------------------------------------------------------


def _normal(panel: dict[str, Any], rig: Rig) -> dict[str, Any]:
    """The panel with what varies per run made stable: the question id, the etag, the read time, the versions."""
    text = json.dumps(panel, sort_keys=True).replace(rig.q.id, "q-0001")
    text = re.sub(r'"(version|etag|as_of|read_at)": "[^"]*"', lambda m: f'"{m.group(1)}": "<{m.group(1)}>"', text)
    return json.loads(text)


def test_the_frozen_fixture_is_what_the_live_panel_produces(rig: Rig) -> None:
    live = _normal(rig.panel(), rig)
    # the fixture is written for a fixed zone-free clock: the timed values are re-based so it never depends on the machine
    live = json.loads(json.dumps(live).replace(_local(-1), "2026-11-08T11:00").replace(_local(1), "2026-11-08T13:00"))
    if os.environ.get("LIST_FIXTURE_WRITE") == "1":
        FIXTURE.parent.mkdir(exist_ok=True)
        FIXTURE.write_text(json.dumps(live, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    frozen = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert live == frozen


# --- 531 round 4 (levain 73, key 1108-37): the host's section and a LATER day reach the List, and sort its band rows ---------


def _band_with(monkeypatch: pytest.MonkeyPatch, rig: Rig, **fields: Any) -> None:
    """The band as a host store reads it: the one question carries the host's own section / carried / snoozed_until / dated."""
    from dataclasses import replace

    real = band_view._read

    def read(store: Any) -> Any:
        items, numbers, closed = real(store)
        return [(ref, replace(q, **fields) if q is not None else q) for ref, q in items], numbers, closed

    monkeypatch.setattr(band_view, "_read", read)


def test_r1_a_desk_section_row_carries_its_section_and_sorts_under_desk_owes(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    before = rig.panel()["count"]
    _band_with(monkeypatch, rig, section="desk", carried=False)
    row = rig.rows()[f"band:{rig.q.id}"]
    assert row["facets"]["band_section"] == "desk" and row["facets"]["band_carried"] is False, row["facets"]
    assert row["group"] == "desk", row["group"]
    assert "desk" in {g["id"] for g in rig.panel()["groups"]}
    assert rig.panel()["count"] == before - 1       # a desk row not yet carried is not the operator's: the header does not count it


def test_listpanel_r1_a_carried_desk_row_is_needs_you_and_counted_as_flows_footer_does(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """Flow's build_footer: a desk row open > 24h is `carried`, listed under NEEDS YOU (tagged desk-owed) AND under DESK OWES, never LATER."""
    before = rig.panel()["count"]
    _band_with(monkeypatch, rig, section="desk", carried=True)
    p = rig.panel()
    row = {r["id"]: r for r in p["rows"]}[f"band:{rig.q.id}"]
    assert row["facets"]["band_section"] == "desk" and row["facets"]["band_carried"] is True, row["facets"]
    assert row["group"] == "band", row["group"]
    assert p["count"] == before, (before, p["count"])      # counted in the header, as the footer's NEEDS YOU counts it
    day = (TODAY + timedelta(days=3)).isoformat()
    _band_with(monkeypatch, rig, section="desk", carried=True, snoozed_until=day)    # carried is never LATER in flow
    assert {r["id"]: r for r in rig.panel()["rows"]}[f"band:{rig.q.id}"]["group"] == "band"


def test_r1_a_you_section_row_is_needs_you_and_a_store_with_no_section_draws_no_facet(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    plain = rig.rows()[f"band:{rig.q.id}"]
    assert "band_section" not in plain["facets"] and "band_carried" not in plain["facets"] and plain["group"] == "band", plain
    _band_with(monkeypatch, rig, section="you")
    row = rig.rows()[f"band:{rig.q.id}"]
    assert row["facets"]["band_section"] == "you" and row["group"] == "band", row


def test_r1_a_section_that_is_not_you_or_desk_and_a_carried_that_is_not_a_bool_are_dropped(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    _band_with(monkeypatch, rig, section="elsewhere", carried="false")
    f = rig.rows()[f"band:{rig.q.id}"]["facets"]
    assert "band_section" not in f and "band_carried" not in f, f
    _band_with(monkeypatch, rig, section="desk", carried="false")
    f = rig.rows()[f"band:{rig.q.id}"]["facets"]
    assert f["band_section"] == "desk" and "band_carried" not in f, f


def test_r2_a_snoozed_or_dated_row_sorts_under_later_with_its_day_words_and_is_not_counted(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    before = rig.panel()["count"]
    day = (TODAY + timedelta(days=3)).isoformat()
    _band_with(monkeypatch, rig, snoozed_until=day)
    p = rig.panel()
    row = {r["id"]: r for r in p["rows"]}[f"band:{rig.q.id}"]
    assert row["facets"]["held_until"] == day and row["group"] == "later", row
    assert row["facets"]["band_later_words"], row["facets"]
    assert p["count"] == before - 1, (before, p["count"])       # a LATER row is not due: the header does not count it
    _band_with(monkeypatch, rig, dated=day, section="you")      # a dated row of the operator's is LATER too
    assert {r["id"]: r for r in rig.panel()["rows"]}[f"band:{rig.q.id}"]["group"] == "later"
    _band_with(monkeypatch, rig, dated=day, section="desk")     # a dated DESK row stays desk owes, as flow's footer files it
    assert {r["id"]: r for r in rig.panel()["rows"]}[f"band:{rig.q.id}"]["group"] == "desk"


def test_r2_a_desk_owes_row_is_not_counted_either(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    before = rig.panel()["count"]
    _band_with(monkeypatch, rig, section="desk")
    assert rig.panel()["count"] == before - 1


@pytest.mark.parametrize("fields,heading", [({"section": "desk", "carried": False}, "Desk owes"),
                                            ({"snoozed_until": (TODAY + timedelta(days=3)).isoformat()}, "Later")])
def test_listpanel_r1_the_text_cockpit_draws_desk_owes_and_later_as_the_footer_does(rig: Rig, monkeypatch: pytest.MonkeyPatch,
                                                                                  fields: dict, heading: str) -> None:
    from levain.cockpit.text import render_lines, snapshot

    def lines() -> list[str]:
        return [t for _s, t in render_lines(snapshot(rig.ck), now=NOW)]

    _band_with(monkeypatch, rig, **fields)
    out = lines()
    head = next(i for i, t in enumerate(out) if heading in t)                    # a dated drop is LATER too: look under THIS heading
    title = next(i for i, t in enumerate(out) if "Ship the sweep fix tonight?" in t and i > head)
    assert not any(t.startswith("  ") and not t.startswith("    ") for t in out[head + 1:title]), out   # the row is drawn under it, not silently dropped


# --- 531 round 6 (levain 75, key 1108-41): the age is the clock flow's footer reads --------------------------------------


def _iso(delta_hours: float) -> str:
    return (NOW + timedelta(hours=delta_hours)).astimezone().isoformat()


def test_listpanel_r6_a_band_rows_age_reads_the_refile_clock_in_the_footers_form(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """A row added 2 h ago and refiled 30 h ago is carried; its age is the footer's `1d6h` (the refile clock), not a whole-day
    count from the add time."""
    _band_with(monkeypatch, rig, section="desk", carried=True, filed_at=_iso(-2), section_since=_iso(-30))
    row = {r["id"]: r for r in rig.panel()["rows"]}[f"band:{rig.q.id}"]
    assert row["facets"]["band_age"] == "1d6h", row["facets"]
    # no refile clock sent: the add time is the clock, in the same form
    _band_with(monkeypatch, rig, section="you", filed_at=_iso(-3), section_since="")
    assert {r["id"]: r for r in rig.panel()["rows"]}[f"band:{rig.q.id}"]["facets"]["band_age"] == "3h"
    _band_with(monkeypatch, rig, section="you", filed_at=_iso(-0.5), section_since="")
    assert {r["id"]: r for r in rig.panel()["rows"]}[f"band:{rig.q.id}"]["facets"]["band_age"] == "30m"


# --- 531 round 7 (levain 76, lpr3): L3 r1 fixes --------------------------------------------------------------------------


@pytest.mark.parametrize("fields,heading", [({"section": "desk", "carried": False}, "Desk owes"),
                                            ({"snoozed_until": (TODAY + timedelta(days=3)).isoformat()}, "Later")])
def test_lpr3_a_list_holding_only_desk_or_later_rows_draws_them_in_the_text_cockpit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                                  fields: dict, heading: str) -> None:
    """Both groups are uncounted, so a List with nothing else is classified empty; the text view must still draw its rows."""
    from levain.cockpit.text import render_lines, snapshot

    r = Rig(tmp_path, spores=False, board=False)
    _band_with(monkeypatch, r, **fields)
    out = "\n".join(t for _s, t in render_lines(snapshot(r.ck), now=NOW))
    assert heading in out and "Ship the sweep fix tonight?" in out, out


def test_lpr3_later_rows_sort_by_return_date_then_band_number_as_the_footer_does(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """Band #1 returns Dec 1 and #2 returns Nov 9: #2 is listed first; the same date falls back to the band number."""
    from dataclasses import replace

    q2 = rig.qs.file(agent="claude_code", agent_session_id="sess-2", what="Second question?",
                     options=[("a", "A"), ("b", "B")], rec="a", blocks="x", cwd="/w/levain")
    real = band_view._read
    days = {rig.q.id: "2026-12-01", q2.id: "2026-11-09"}

    def read(store: Any) -> Any:
        items, numbers, closed = real(store)
        return [(ref, replace(q, snoozed_until=days[q.id]) if q is not None and q.id in days else q) for ref, q in items], {rig.q.id: 1, q2.id: 2}, closed

    monkeypatch.setattr(band_view, "_read", read)
    p = rig.panel()
    later = [r for r in p["rows"] if r["group"] == "later" and r["id"].startswith("band:")]
    nums = {r["id"]: r["facets"].get("band_number") for r in later}
    assert [r["id"] for r in later] == [f"band:{q2.id}", f"band:{rig.q.id}"], (nums, [r["facets"].get("held_until") for r in later])
    assert nums[f"band:{rig.q.id}"] < nums[f"band:{q2.id}"], nums           # the band number alone would have said the opposite
    days[q2.id] = "2026-12-01"                                              # one date: the band number decides
    later = [r for r in rig.panel()["rows"] if r["group"] == "later" and r["id"].startswith("band:")]
    assert [r["id"] for r in later] == [f"band:{rig.q.id}", f"band:{q2.id}"]
