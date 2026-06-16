"""Tests for levain.tui — the PURE layer of the terminal control plane.

The curses DRIVER (``levain._tui_curses``) needs a real terminal and is verified
by the live L4 canary, not here. Everything in ``levain.tui`` is a pure function
of (view, model, input) and is fully covered below: the layout/zone filtering,
the verb-affordance model (the metal), the request shapers (the apply_edit
contract), the EditError→status mapping, the navigation reducers, and the
per-kind detail renderers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from levain.dashboard import (
    CLASS_A,
    CLASS_B,
    CLASS_C,
    ZONE_IDENTITY,
    ZONE_MIND,
    ZONE_OPERATE,
    AnnealPaths,
    AssociationGraph,
    ConfigDoc,
    CrystalEntry,
    EpisodeRow,
    GraphEdge,
    GraphNode,
    Health,
    OpenSpore,
    Section,
    SubstrateView,
    WrapRow,
)
from levain import tui


def _view() -> SubstrateView:
    paths = AnnealPaths.from_db(Path("/x/memory.db"))
    health = Health(
        write_path_live=True,
        total_links=42,
        avg_strength=0.91,
        max_strength=1.0,
        density=0.1,
        local_density=0.3,
        links_formed_total=50,
        links_strengthened_total=20,
        links_decayed_total=8,
        graduations_validated_total=12,
        graduations_demoted_total=3,
        total_episodes=4462,
        episodes_since_wrap=45,
        episodes_by_type={"finding": 24, "decision": 12},
        tombstones=1,
        continuity_chars=21710,
        total_wraps=53,
        last_wrap_at="2026-06-15T16:55:00",
        wrap_in_progress=False,
    )
    graph = AssociationGraph(
        nodes=[GraphNode("a", "episode", "A"), GraphNode("b", "episode", "B")],
        edges=[GraphEdge("a", "b", 0.9, 3, "engaged", 0.7)],
        truncated=False,
    )
    config_docs = [
        ConfigDoc(
            key="operator-identity", title="Operator — Identity", body="who the operator is\nline2",
            edit_class=CLASS_A, zone=ZONE_IDENTITY, source="seed/world.md", heading="Identity",
        ),
        ConfigDoc(
            key="origin", title="Origin", body="the entity's birth statement",
            edit_class=CLASS_C, zone=ZONE_IDENTITY, source="seed/origin.md", heading=None,
        ),
    ]
    sections = [
        Section("State", "current focus line", CLASS_A, ZONE_MIND),
        Section("Active Threads", "thread line", CLASS_C, ZONE_MIND),
        Section("Patterns", "pattern line", CLASS_C, ZONE_MIND),
        Section("Decisions", "decision line", CLASS_C, ZONE_MIND),
        Section("Context", "context line", CLASS_C, ZONE_MIND),
        Section("Understanding", "understanding line", CLASS_C, ZONE_MIND),
    ]
    spores = [
        OpenSpore(
            id="spore-099", type="task", tier="growing", salience=3, domain="levain",
            text="give Levain a canonical [tool.mypy]", seen="2026-06-15", next=None, pointer=None,
            descend_kinds=["done", "dropped"], ascend_kinds=["pattern"],
        ),
        OpenSpore(
            id="spore-093", type="task", tier="resting", salience=2, domain="anneal",
            text="content-store v1", seen="2026-06-14", next="2026-06-26", pointer="next.md",
            descend_kinds=["done"], ascend_kinds=[],
        ),
    ]
    episodes = [
        EpisodeRow("ep1", "2026-06-15T16:55:00", "finding", "session-tui", "built the TUI scaffold", ["levain"]),
        EpisodeRow("ep2", "2026-06-15T12:00:00", "decision", "session-tui", "chose curses", ["levain"]),
    ]
    crystals = [
        CrystalEntry("thinness_is_the_architecture", 3, "build the ports thin", "timeless", "always", "2026-06-14", ["levain"]),
        CrystalEntry("verify_before_acting", 3, "verify against ground truth", "timeless", "on-cue", "2026-06-15", ["apparatus"]),
    ]
    wraps = [
        WrapRow("2026-06-15T16:55:00", 45, 21710, 2, 1, 4, 3, 1),
        WrapRow("2026-06-14T21:31:00", 30, 20100, 1, 0, 2, 1, 0),
    ]
    recent_edits = [
        {"id": "edit1", "ts": "2026-06-15T16:00:00", "kind": "state", "action": "edit",
         "source": ".levain/memory.continuity.md", "heading": "State", "undoable": True,
         "new_sha256": "abc"},
        {"id": "verb1", "ts": "2026-06-15T15:00:00", "kind": "spore_descend", "action": "descend",
         "source": "spore:spore-001", "heading": None, "undoable": False, "verb_kind": "done"},
    ]
    return SubstrateView(
        paths=paths, entity_name="flow", health=health, graph=graph,
        crystal_index=crystals, open_spores=spores, episodes=episodes,
        sections=sections, config_docs=config_docs, wraps=wraps, recent_edits=recent_edits,
    )


# --- zone / panel filtering ------------------------------------------------

def test_panels_for_zone_identity_is_config_docs():
    v = _view()
    panels = tui.panels_for_zone(v, ZONE_IDENTITY)
    assert [p["kind"] for p in panels] == ["config", "config"]
    assert panels[0]["edit_class"] == CLASS_A
    assert panels[1]["edit_class"] == CLASS_C


def test_panels_for_zone_operate_order():
    v = _view()
    panels = tui.panels_for_zone(v, ZONE_OPERATE)
    assert [p["kind"] for p in panels] == ["spores", "episodes", "edits"]
    # spores + episodes are Class B (verb-mediated); the edit log has no chip.
    assert panels[0]["edit_class"] == CLASS_B
    assert panels[1]["edit_class"] == CLASS_B
    assert panels[2]["edit_class"] == ""


def test_panels_for_zone_mind_has_sections_and_state_is_class_a():
    v = _view()
    panels = tui.panels_for_zone(v, ZONE_MIND)
    kinds = [p["kind"] for p in panels]
    assert kinds[:3] == ["health", "graph", "crystals"]
    assert kinds[-1] == "wraps"
    section_panels = [p for p in panels if p["kind"] == "section"]
    assert len(section_panels) == 6
    state = next(p for p in section_panels if p["title"] == "State")
    assert state["edit_class"] == CLASS_A
    assert all(p["edit_class"] == CLASS_C for p in section_panels if p["title"] != "State")


def test_panel_item_count_only_verb_lists_are_selectable():
    v = _view()
    by_kind = {p["kind"]: p for p in v.layout()}
    assert tui.panel_item_count(v, by_kind["spores"]) == 2
    assert tui.panel_item_count(v, by_kind["episodes"]) == 2
    assert tui.panel_item_count(v, by_kind["edits"]) == 2
    # crystals + wraps render rich and scroll, but carry no per-item verb → 0
    assert tui.panel_item_count(v, by_kind["crystals"]) == 0
    assert tui.panel_item_count(v, by_kind["wraps"]) == 0
    assert tui.panel_item_count(v, by_kind["health"]) == 0  # text panel


def test_is_item_list():
    v = _view()
    spores = next(p for p in v.layout() if p["kind"] == "spores")
    health = next(p for p in v.layout() if p["kind"] == "health")
    crystals = next(p for p in v.layout() if p["kind"] == "crystals")
    assert tui.is_item_list(spores) is True
    assert tui.is_item_list(health) is False
    assert tui.is_item_list(crystals) is False  # rich list, but no per-item verb
    assert tui.is_item_list(None) is False


# --- verb affordances (the metal) ------------------------------------------

def test_class_c_panel_offers_no_verb():
    v = _view()
    health = next(p for p in v.layout() if p["kind"] == "health")
    assert tui.panel_verbs(health) == []
    origin = next(p for p in v.layout() if p["kind"] == "config" and p["edit_class"] == CLASS_C)
    assert tui.panel_verbs(origin) == []


def test_class_a_config_and_section_offer_edit():
    v = _view()
    world = next(p for p in v.layout() if p["kind"] == "config" and p["edit_class"] == CLASS_A)
    state = next(p for p in v.layout() if p["kind"] == "section" and p["title"] == "State")
    assert [x.kind for x in tui.panel_verbs(world)] == ["edit"]
    assert [x.kind for x in tui.panel_verbs(state)] == ["edit"]


def test_spore_panel_verbs():
    v = _view()
    spores = next(p for p in v.layout() if p["kind"] == "spores")
    verbs = tui.panel_verbs(spores)
    assert [x.kind for x in verbs] == ["spore_touch", "spore_descend", "spore_ascend"]
    descend = next(x for x in verbs if x.kind == "spore_descend")
    assert descend.destructive and descend.needs_kind
    touch = next(x for x in verbs if x.kind == "spore_touch")
    assert not touch.destructive


def test_episode_panel_offers_tombstone_and_edits_offers_undo():
    v = _view()
    eps = next(p for p in v.layout() if p["kind"] == "episodes")
    edits = next(p for p in v.layout() if p["kind"] == "edits")
    assert [x.kind for x in tui.panel_verbs(eps)] == ["episode_tombstone"]
    assert tui.panel_verbs(eps)[0].destructive
    assert [x.kind for x in tui.panel_verbs(edits)] == ["undo"]


def test_panel_verbs_none_is_empty():
    assert tui.panel_verbs(None) == []


def test_is_record_undoable_mirrors_server_refusal():
    # File edits are undoable; verb-mediated ops, undo-of-undo, and explicit
    # undoable:false are not (mirrors writes._apply_undo's refusal).
    assert tui.is_record_undoable({"kind": "state", "action": "edit"}) is True
    assert tui.is_record_undoable({"kind": "config", "action": "edit", "undoable": True}) is True
    assert tui.is_record_undoable({"kind": "spore_descend", "action": "descend"}) is False
    assert tui.is_record_undoable({"kind": "episode_tombstone", "action": "tombstone"}) is False
    assert tui.is_record_undoable({"kind": "undo", "action": "undo"}) is False
    assert tui.is_record_undoable({"kind": "state", "action": "edit", "undoable": False}) is False


def test_render_edits_flags_non_undoable_rows():
    v = _view()
    panel = next(p for p in v.layout() if p["kind"] == "edits")
    lines = tui.render_panel_lines(v, panel)
    # the fixture's verb1 (spore_descend) row carries the not-file-undoable flag;
    # the state-edit row does not.
    joined = "\n".join(lines)
    assert "(not file-undoable)" in joined
    state_line = next(ln for ln in lines if "edit1" in ln)
    assert "(not file-undoable)" not in state_line


# --- request shapers (the apply_edit contract) -----------------------------

def test_build_touch_req():
    assert tui.build_touch_req("s1") == {"kind": "spore_touch", "spore_id": "s1"}


def test_build_descend_req_carries_confirm_and_kind():
    r = tui.build_descend_req("s1", "done")
    assert r == {"kind": "spore_descend", "spore_id": "s1", "spore_kind": "done", "confirm": True}


def test_build_ascend_req_carries_ref():
    r = tui.build_ascend_req("s1", "pattern", "thinness")
    assert r["kind"] == "spore_ascend" and r["ref"] == "thinness" and r["confirm"] is True


def test_build_tombstone_req_confirms():
    assert tui.build_tombstone_req("ep1") == {
        "kind": "episode_tombstone", "episode_id": "ep1", "confirm": True
    }


def test_build_undo_req():
    assert tui.build_undo_req("e1") == {"kind": "undo", "edit_id": "e1"}


def test_build_state_and_config_edit_reqs():
    s = tui.build_state_edit_req(expected_body="old", new_body="new")
    assert s == {"kind": "state", "heading": "State", "expected_body": "old", "new_body": "new"}
    # heading is data, not a constant — threaded so a future 2nd Class-A section targets right
    assert tui.build_state_edit_req(expected_body="o", new_body="n", heading="Other")["heading"] == "Other"
    c = tui.build_config_edit_req(
        source="seed/world.md", heading="Identity", expected_body="old", new_body="new"
    )
    assert c["kind"] == "config" and c["heading"] == "Identity"


def test_panel_verbs_read_only_suppresses_all_affordances():
    # The inspect-only mode (`levain tui --read-only` / the flow self-ops cockpit):
    # EVERY panel that would otherwise carry a verb offers none, so the footer
    # advertises navigation only and no verb key dispatches (NO THEATER — never
    # advertise a write with no governed target behind it).
    v = _view()
    for kind in ("config", "section", "spores", "episodes", "edits"):
        panels = [p for p in v.layout() if p["kind"] == kind]
        for p in panels:
            assert tui.panel_verbs(p, read_only=True) == [], (kind, p.get("edit_class"))
    # and the would-be-metal Class-A/B panels in isolation
    assert tui.panel_verbs({"kind": "spores", "edit_class": CLASS_B}, read_only=True) == []
    assert tui.panel_verbs({"kind": "section", "edit_class": CLASS_A}, read_only=True) == []


def test_read_only_is_off_by_default():
    # The default model is read+write — the new flag must not change existing behavior.
    assert tui.TuiModel(view=_view()).read_only is False
    world = next(p for p in _view().layout() if p["kind"] == "config" and p["edit_class"] == CLASS_A)
    assert [x.kind for x in tui.panel_verbs(world)] == ["edit"]  # unchanged default path


def _model_on(view: SubstrateView, kind: str, *, read_only: bool) -> tui.TuiModel:
    """A model seated on the first panel of `kind` (so current_panel() returns it)."""
    for zi, (zone, _label) in enumerate(tui.ZONES):
        panels = tui.panels_for_zone(view, zone)
        for pi, p in enumerate(panels):
            if p.get("kind") == kind:
                return tui.TuiModel(view=view, zone_idx=zi, panel_idx=pi, read_only=read_only)
    raise AssertionError(f"no {kind} panel in any zone")


def test_active_verbs_threads_read_only_to_panel_verbs():
    # The driver's _active_verbs feeds BOTH the footer (advertise) and the dispatch
    # map (honor). Lock that read_only threads through it → both go dark (codex/
    # complement L3 — the chokepoint one layer below panel_verbs).
    from levain import _tui_curses

    v = _view()
    rw = _model_on(v, "spores", read_only=False)
    assert [x.kind for x in _tui_curses._active_verbs(rw)] == [
        "spore_touch", "spore_descend", "spore_ascend",
    ]
    ro = _model_on(v, "spores", read_only=True)
    assert _tui_curses._active_verbs(ro) == []


def test_apply_backstop_refuses_write_in_read_only(monkeypatch):
    # The single un-bypassable chokepoint: in a read_only model _apply must refuse
    # BEFORE calling apply_edit (no install_root/.levain/ fabrication), regardless
    # of how dispatch was reached (codex/complement L3).
    from levain import _tui_curses

    called: list = []
    monkeypatch.setattr(_tui_curses, "apply_edit", lambda *a, **k: called.append((a, k)))
    model = tui.TuiModel(view=_view(), read_only=True)
    result = _tui_curses._apply(
        model, Path("/x"), None, {"kind": "spore_touch", "spore_id": "s1"}, "ok"
    )
    assert called == []  # apply_edit NEVER reached
    assert result.read_only is True
    assert "read-only" in result.status


def test_panel_verbs_require_class_b_for_lifecycle():
    # The manifest's edit_class owns the affordance: a mistagged Class-C spores/
    # episodes panel must offer NO mutation verb.
    assert tui.panel_verbs({"kind": "spores", "edit_class": CLASS_C}) == []
    assert tui.panel_verbs({"kind": "episodes", "edit_class": CLASS_C}) == []
    assert [v.kind for v in tui.panel_verbs({"kind": "spores", "edit_class": CLASS_B})] == [
        "spore_touch", "spore_descend", "spore_ascend",
    ]
    assert [v.kind for v in tui.panel_verbs({"kind": "episodes", "edit_class": CLASS_B})] == [
        "episode_tombstone",
    ]


# --- EditError → status mapping --------------------------------------------

def test_edit_error_status_frames_stale_as_action():
    msg = tui.edit_error_status("stale", "x changed")
    assert "moved under you" in msg and "reload" in msg


def test_edit_error_status_known_codes():
    assert "read-only" in tui.edit_error_status("not_editable", "")
    assert "lock" in tui.edit_error_status("lock_unavailable", "")
    assert tui.edit_error_status("confirm_required", "compost?").endswith("compost?")


def test_edit_error_status_default():
    assert tui.edit_error_status("verb_failed", "boom") == "⚠ verb_failed: boom"


# --- navigation reducers ---------------------------------------------------

def test_select_zone_clamps_and_resets():
    m = tui.TuiModel(view=_view(), panel_idx=2, item_idx=1, scroll=5, status="x")
    m2 = tui.select_zone(m, 2)
    assert m2.zone_idx == 2 and m2.panel_idx == 0 and m2.item_idx == 0 and m2.scroll == 0 and m2.status == ""
    assert tui.select_zone(m, 99).zone_idx == len(tui.ZONES) - 1
    assert tui.select_zone(m, -5).zone_idx == 0


def test_select_panel_clamps_within_zone():
    m = tui.TuiModel(view=_view(), zone_idx=1)  # Operate: 3 panels
    assert tui.select_panel(m, +1).panel_idx == 1
    assert tui.select_panel(m, +99).panel_idx == 2
    assert tui.select_panel(m, -99).panel_idx == 0
    # moving panel resets item + scroll
    m2 = tui.TuiModel(view=_view(), zone_idx=1, panel_idx=0, item_idx=1, scroll=3)
    assert tui.select_panel(m2, +1).item_idx == 0
    assert tui.select_panel(m2, +1).scroll == 0


def test_move_in_panel_selects_items_in_list_panel():
    # Operate zone, spores panel (2 items) → j/k moves item cursor, clamped.
    m = tui.TuiModel(view=_view(), zone_idx=1, panel_idx=0)
    assert tui.move_in_panel(m, +1).item_idx == 1
    assert tui.move_in_panel(m, +5).item_idx == 1  # clamped to last
    assert tui.move_in_panel(m, -5).item_idx == 0


def test_move_in_panel_scrolls_text_panel_and_floors():
    # Mind zone, health panel (text) → j/k scrolls, never selects an item.
    m = tui.TuiModel(view=_view(), zone_idx=2, panel_idx=0)
    assert m.current_panel()["kind"] == "health"
    moved = tui.move_in_panel(m, +3)
    assert moved.scroll == 3 and moved.item_idx == 0
    assert tui.move_in_panel(moved, -10_000).scroll == 0  # floors at 0


def test_with_view_clamps_cursors_when_data_shrinks():
    v = _view()
    m = tui.TuiModel(view=v, zone_idx=1, panel_idx=0, item_idx=1)  # spores, item 1
    # rebuild with an empty-spores view → item cursor clamps to 0
    shrunk = SubstrateView(paths=v.paths, entity_name="flow", health=v.health)
    m2 = tui.with_view(m, shrunk, status="refreshed")
    assert m2.item_idx == 0 and m2.status == "refreshed"
    assert m2.panel_idx <= len(m2.panels()) - 1


def test_current_item_helpers():
    m = tui.TuiModel(view=_view(), zone_idx=1, panel_idx=0, item_idx=1)
    assert tui.current_spore(m).id == "spore-093"
    # off the spores panel → None
    assert tui.current_episode(m) is None
    me = tui.TuiModel(view=_view(), zone_idx=1, panel_idx=1, item_idx=0)
    assert tui.current_episode(me).id == "ep1"
    med = tui.TuiModel(view=_view(), zone_idx=1, panel_idx=2, item_idx=0)
    assert tui.current_edit_record(med)["id"] == "edit1"


# --- per-kind detail renderers (smoke + key content) -----------------------

@pytest.mark.parametrize("kind", ["config", "section", "spores", "episodes", "edits",
                                  "health", "graph", "crystals", "wraps"])
def test_render_panel_lines_nonempty_per_kind(kind):
    v = _view()
    panel = next(p for p in v.layout() if p["kind"] == kind)
    lines = tui.render_panel_lines(v, panel)
    assert isinstance(lines, list) and lines and all(isinstance(x, str) for x in lines)


def test_render_health_shows_live_writepath_and_counts():
    v = _view()
    panel = next(p for p in v.layout() if p["kind"] == "health")
    text = "\n".join(tui.render_panel_lines(v, panel))
    assert "LIVE" in text and "42 links" in text and "4,462" in text


def test_render_spores_one_line_per_loop_with_id():
    v = _view()
    panel = next(p for p in v.layout() if p["kind"] == "spores")
    lines = tui.render_panel_lines(v, panel)
    assert len(lines) == 2  # one line per open loop
    assert "spore-099" in lines[0] and "give Levain a canonical" in lines[0]


@pytest.mark.parametrize("kind", ["spores", "episodes", "edits"])
def test_verb_list_panels_render_one_line_per_item(kind):
    # The driver's cursor highlight + auto-scroll depend on 1 line == 1 item.
    v = _view()
    panel = next(p for p in v.layout() if p["kind"] == kind)
    lines = tui.render_panel_lines(v, panel)
    assert len(lines) == tui.panel_item_count(v, panel)


def test_render_wraps_teaches_reprojection():
    v = _view()
    panel = next(p for p in v.layout() if p["kind"] == "wraps")
    text = "\n".join(tui.render_panel_lines(v, panel))
    assert "RE-PROJECTION" in text and "no restore" in text.lower()


def test_render_section_marks_state_editable():
    v = _view()
    state = next(p for p in v.layout() if p["kind"] == "section" and p["title"] == "State")
    text = "\n".join(tui.render_panel_lines(v, state))
    assert "operator-editable" in text


def test_render_sanitizes_embedded_newlines():
    # A spore/episode field with a newline must NOT split into extra lines — the
    # cursor highlight + auto-scroll map item-index 1:1 to line-index (L3 MED-1).
    paths = AnnealPaths.from_db(Path("/x/memory.db"))
    spore = OpenSpore(
        id="s1", type="task", tier="hot", salience=3, domain="d",
        text="line one\nline two", seen="2026-06-15", next="a\nb", pointer=None,
    )
    ep = EpisodeRow("e1", "2026-06-15T00:00:00", "finding", "src", "multi\nline\ncontent", [])
    v = SubstrateView(paths=paths, open_spores=[spore], episodes=[ep])
    sp = next(p for p in v.layout() if p["kind"] == "spores")
    epp = next(p for p in v.layout() if p["kind"] == "episodes")
    sp_lines = tui.render_panel_lines(v, sp)
    ep_lines = tui.render_panel_lines(v, epp)
    assert len(sp_lines) == 1 and "\n" not in sp_lines[0]
    assert len(ep_lines) == 1 and "\n" not in ep_lines[0]


def test_render_empty_collections_degrade_gracefully():
    empty = SubstrateView(paths=AnnealPaths.from_db(Path("/x/memory.db")))
    for kind in ["spores", "episodes", "edits", "crystals", "wraps"]:
        panel = next(p for p in empty.layout() if p["kind"] == kind)
        lines = tui.render_panel_lines(empty, panel)
        assert lines and "(" in lines[0]  # a "(no ...)" placeholder, never a crash
