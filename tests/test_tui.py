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
    tray = [
        OpenSpore(
            id="spore-110", type="task", tier="warm", salience=2, domain="ops",
            text="a fresh freeform dump", seen="2026-06-18", next=None, pointer=None,
            disposition="seed",
        ),
        OpenSpore(
            id="spore-111", type="question", tier="warm", salience=2, domain="ops",
            text="pick up the Tray build next session", seen="2026-06-18", next="2026-06-19",
            pointer=None, disposition="handoff",
        ),
    ]
    keep = [
        OpenSpore(
            id="spore-029", type="task", tier="parked", salience=1, domain="ops",
            text="open-model partner laptop", seen="2026-06-10", next=None, pointer=None,
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
        crystal_index=crystals, open_spores=spores, tray=tray, keep=keep, episodes=episodes,
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
    # the three spore projections are adjacent (Open Loops · Tray · Keep), then episodes,
    # then the edit log (Slice 3 capture-UX (A) ordering).
    assert [p["kind"] for p in panels] == ["spores", "tray", "keep", "episodes", "edits"]
    by_kind = {p["kind"]: p for p in panels}
    # spores + episodes are Class B (verb-mediated); Tray + Keep FLIP to Class B in 3b
    # (the governed dump/triage/park verbs); only the edit log carries no chip.
    assert by_kind["spores"]["edit_class"] == CLASS_B
    assert by_kind["episodes"]["edit_class"] == CLASS_B
    assert by_kind["edits"]["edit_class"] == ""
    assert by_kind["tray"]["edit_class"] == CLASS_B
    assert by_kind["keep"]["edit_class"] == CLASS_B


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
    assert [x.kind for x in verbs] == [
        "spore_touch", "spore_edit", "spore_schedule", "spore_park",
        "spore_descend", "spore_ascend",
    ]
    descend = next(x for x in verbs if x.kind == "spore_descend")
    assert descend.destructive and descend.needs_kind
    ascend = next(x for x in verbs if x.kind == "spore_ascend")
    assert ascend.destructive and ascend.needs_kind
    # the metadata verbs (touch/edit/schedule/park) are non-destructive — no confirm gate
    for k in ("spore_touch", "spore_edit", "spore_schedule", "spore_park"):
        assert not next(x for x in verbs if x.kind == k).destructive
    # every Open-Loops verb acts on the selected row (no panel-level capture here)
    assert all(x.row_scoped for x in verbs)


def test_tray_panel_verbs():
    # Tray = the operator inbox: a panel-level capture (the dump) + the forming-workbench
    # (edit / reclassify) + the route levers (metabolize / schedule / dismiss).
    verbs = tui.panel_verbs({"kind": "tray", "edit_class": CLASS_B}, read_only=False)
    assert [x.kind for x in verbs] == [
        "spore_capture", "spore_edit", "spore_reclassify",
        "spore_metabolize", "spore_schedule", "spore_descend",
    ]
    cap = next(x for x in verbs if x.kind == "spore_capture")
    assert cap.row_scoped is False  # the dump works on an EMPTY Tray
    dismiss = next(x for x in verbs if x.kind == "spore_descend")
    assert dismiss.destructive and dismiss.needs_kind


def test_keep_panel_verbs():
    # Keep SUPERSET (panel-level; _active_verbs prunes the note-only verbs for a parked loop).
    verbs = tui.panel_verbs({"kind": "keep", "edit_class": CLASS_B}, read_only=False)
    assert [x.kind for x in verbs] == [
        "spore_capture", "spore_edit", "keep_activate",
        "spore_to_tray", "keep_remind", "spore_descend",
    ]
    assert next(x for x in verbs if x.kind == "spore_capture").row_scoped is False
    assert next(x for x in verbs if x.kind == "spore_descend").destructive
    # the promote/activate verbs are GAINS into cognition / a lateral move → no confirm gate
    for k in ("keep_activate", "spore_to_tray", "keep_remind"):
        assert not next(x for x in verbs if x.kind == k).destructive


def test_episode_panel_offers_tombstone_and_edits_offers_undo():
    v = _view()
    eps = next(p for p in v.layout() if p["kind"] == "episodes")
    edits = next(p for p in v.layout() if p["kind"] == "edits")
    assert [x.kind for x in tui.panel_verbs(eps)] == ["episode_tombstone"]
    assert tui.panel_verbs(eps)[0].destructive
    assert [x.kind for x in tui.panel_verbs(edits)] == ["undo"]


def test_panel_verbs_none_is_empty():
    assert tui.panel_verbs(None) == []


def test_tray_keep_read_only_suppresses_all_verbs():
    # The inspect-only mode (levain tui --read-only / the flow cockpit over a no-.levain
    # substrate) suppresses EVERY Tray/Keep verb so the chip renders glass + the footer
    # advertises navigation only (NO THEATER — no governed write target behind them).
    for kind in ("tray", "keep"):
        assert tui.panel_verbs({"kind": kind, "edit_class": CLASS_B}, read_only=True) == []


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
        "spore_touch", "spore_edit", "spore_schedule", "spore_park",
        "spore_descend", "spore_ascend",
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
        model, None, {"kind": "spore_touch", "spore_id": "s1"}, "ok"
    )
    assert called == []  # apply_edit NEVER reached
    assert result.read_only is True
    assert "read-only" in result.status


def test_panel_verbs_require_class_b_for_lifecycle():
    # The manifest's edit_class owns the affordance: a mistagged Class-C spores/
    # episodes panel must offer NO mutation verb.
    for kind in ("spores", "tray", "keep", "episodes"):
        assert tui.panel_verbs({"kind": kind, "edit_class": CLASS_C}) == []
    assert [v.kind for v in tui.panel_verbs({"kind": "spores", "edit_class": CLASS_B})] == [
        "spore_touch", "spore_edit", "spore_schedule", "spore_park",
        "spore_descend", "spore_ascend",
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
    m = tui.TuiModel(view=_view(), zone_idx=1)  # Operate: 5 panels (spores·tray·keep·episodes·edits)
    assert tui.select_panel(m, +1).panel_idx == 1
    assert tui.select_panel(m, +99).panel_idx == 4
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
    # Operate panels: 0=spores, 1=tray, 2=keep, 3=episodes, 4=edits (Slice 3 inserted
    # Tray + Keep between Open Loops and episodes).
    me = tui.TuiModel(view=_view(), zone_idx=1, panel_idx=3, item_idx=0)
    assert tui.current_episode(me).id == "ep1"
    med = tui.TuiModel(view=_view(), zone_idx=1, panel_idx=4, item_idx=0)
    assert tui.current_edit_record(med)["id"] == "edit1"


# --- per-kind detail renderers (smoke + key content) -----------------------

@pytest.mark.parametrize("kind", ["config", "section", "spores", "tray", "keep",
                                  "episodes", "edits", "health", "graph", "crystals", "wraps"])
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


def test_render_tray_badges_disposition():
    # The Tray badges the DISPOSITION (the operator-I/O class), not the tier — that is
    # the salient axis for the operator inbox.
    v = _view()
    panel = next(p for p in v.layout() if p["kind"] == "tray")
    lines = tui.render_panel_lines(v, panel)
    assert len(lines) == 2
    assert "[seed]" in lines[0] and "spore-110" in lines[0]
    assert "[handoff]" in lines[1] and "surface 2026-06-19" in lines[1]


def test_render_keep_badges_parked_tier():
    v = _view()
    panel = next(p for p in v.layout() if p["kind"] == "keep")
    lines = tui.render_panel_lines(v, panel)
    assert len(lines) == 1
    assert "[parked]" in lines[0] and "spore-029" in lines[0]


def test_render_tray_keep_empty_states():
    v = SubstrateView(paths=AnnealPaths.from_db(Path("/x/memory.db")))
    tray_panel = next(p for p in v.layout() if p["kind"] == "tray")
    keep_panel = next(p for p in v.layout() if p["kind"] == "keep")
    assert tui.render_panel_lines(v, tray_panel) == ["(tray clear)"]
    assert tui.render_panel_lines(v, keep_panel) == ["(keep empty)"]


def test_tray_keep_are_verb_navigable():
    # Tray + Keep grew Class-B operator-I/O verbs (the curses peer of the web's Slice-3b
    # verbs), so they are now item-navigable: a per-row cursor the verbs act on. (The
    # inverse of the retired 3a-era assertion that they were read-only display panels.)
    v = _view()
    for kind in ("tray", "keep"):
        panel = next(p for p in v.layout() if p["kind"] == kind)
        assert tui.panel_item_count(v, panel) > 0
        assert tui.is_item_list(panel)


@pytest.mark.parametrize("kind", ["spores", "tray", "keep", "episodes", "edits"])
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


# --- Slice-3b operator-I/O: new request shapers ----------------------------

def test_build_seed_req():
    assert tui.build_seed_req("dump it", "seed") == {
        "kind": "spore_seed", "text": "dump it", "disposition": "seed",
    }
    assert tui.build_seed_req("a note", "note")["disposition"] == "note"


def test_build_set_disposition_req_omits_surface_at_unless_given():
    # metabolize / -> tray carry NO surface_at (omitted -> server leaves `next` untouched)...
    assert tui.build_set_disposition_req("spore-1", "loop") == {
        "kind": "spore_set_disposition", "spore_id": "spore-1", "disposition": "loop",
    }
    # ...the keep-note "remind me" rides surface_at atomically (note->seed + schedule, one CAS).
    assert tui.build_set_disposition_req("spore-1", "seed", surface_at="2026-07-01") == {
        "kind": "spore_set_disposition", "spore_id": "spore-1",
        "disposition": "seed", "surface_at": "2026-07-01",
    }
    # an explicit clear (None) is DISTINCT from omission -> it must be threaded through.
    assert tui.build_set_disposition_req("spore-1", "seed", surface_at=None)["surface_at"] is None


def test_build_surface_at_req():
    assert tui.build_surface_at_req("spore-1", "2026-07-01") == {
        "kind": "spore_surface_at", "spore_id": "spore-1", "surface_at": "2026-07-01",
    }
    assert tui.build_surface_at_req("spore-1", "")["surface_at"] == ""  # explicit clear


def test_build_spore_update_req_threads_only_given_fields():
    assert tui.build_spore_update_req("spore-1", text="x") == {
        "kind": "spore_update", "spore_id": "spore-1", "text": "x",
    }
    assert tui.build_spore_update_req("spore-1", tier="parked") == {
        "kind": "spore_update", "spore_id": "spore-1", "tier": "parked",
    }
    # `spore_type` maps to the wire key `type` (kwarg renamed to avoid shadowing the builtin)
    assert tui.build_spore_update_req("spore-1", spore_type="task")["type"] == "task"


# --- current_row: the unified selector over the three spore projections -----

def _spore(sid: str, **kw: object) -> OpenSpore:
    base: dict[str, object] = dict(
        id=sid, type="task", tier="warm", salience=1, domain="d",
        text=f"text {sid}", seen="2026-06-20", next=None, pointer=None,
    )
    base.update(kw)
    return OpenSpore(**base)  # type: ignore[arg-type]


def test_current_row_selects_from_the_active_projection():
    from dataclasses import replace as _replace

    v = SubstrateView(
        paths=AnnealPaths.from_db(Path("/x/memory.db")),
        open_spores=[_spore("spore-1")],
        tray=[_spore("spore-2", disposition="seed")],
        keep=[_spore("spore-3", disposition="note")],
    )
    assert tui.current_row(_model_on(v, "spores", read_only=False)).id == "spore-1"
    assert tui.current_row(_model_on(v, "tray", read_only=False)).id == "spore-2"
    assert tui.current_row(_model_on(v, "keep", read_only=False)).id == "spore-3"
    # out of range -> None; a non-spore panel -> None
    m = _model_on(v, "keep", read_only=False)
    assert tui.current_row(_replace(m, item_idx=9)) is None
    assert tui.current_row(_model_on(v, "episodes", read_only=False)) is None


# --- _active_verbs: row-aware Keep + panel-level capture on empty lists ------

def test_active_verbs_keep_note_offers_promote_lifecycle():
    from levain import _tui_curses
    v = SubstrateView(paths=AnnealPaths.from_db(Path("/x/memory.db")),
                      keep=[_spore("spore-n", disposition="note")])
    kinds = [x.kind for x in _tui_curses._active_verbs(_model_on(v, "keep", read_only=False))]
    assert "spore_to_tray" in kinds and "keep_remind" in kinds and "keep_activate" in kinds


def test_active_verbs_keep_parked_loop_drops_note_only_verbs():
    from levain import _tui_curses
    # a pinned-dormant LOOP (tier=parked, disposition defaults to loop) -- NOT a note.
    v = SubstrateView(paths=AnnealPaths.from_db(Path("/x/memory.db")),
                      keep=[_spore("spore-p", tier="parked")])
    kinds = [x.kind for x in _tui_curses._active_verbs(_model_on(v, "keep", read_only=False))]
    assert "keep_activate" in kinds       # un-park stays (row-dispatched)
    assert "spore_to_tray" not in kinds    # the note-only promote verbs are pruned
    assert "keep_remind" not in kinds


def test_active_verbs_empty_list_keeps_only_panel_level_capture():
    from levain import _tui_curses
    v = SubstrateView(paths=AnnealPaths.from_db(Path("/x/memory.db")),
                      open_spores=[], tray=[], keep=[])
    # empty Tray/Keep -> only the panel-level dump survives (you capture INTO an empty inbox)...
    for kind in ("tray", "keep"):
        kinds = [x.kind for x in _tui_curses._active_verbs(_model_on(v, kind, read_only=False))]
        assert kinds == ["spore_capture"]
    # ...but Open Loops has no panel-level verb -> an empty list advertises nothing.
    assert _tui_curses._active_verbs(_model_on(v, "spores", read_only=False)) == []


# --- _handle_verb dispatch routing (the driver decides WHICH builder runs per row) ----
# The pure builders + _active_verbs are covered above; these lock the ROUTING in
# _handle_verb — the riskiest branch logic (keep_activate's note-vs-parked fork, the
# panel-aware descend, the schedule '-'-clear). apply_edit + the modal helpers are
# monkeypatched, so we assert the exact request _handle_verb hands to the governed seam.

def _verb_of(panel_kind: str, vkind: str) -> tui.Verb:
    return next(v for v in tui.panel_verbs({"kind": panel_kind, "edit_class": CLASS_B})
               if v.kind == vkind)


def _capture_apply(monkeypatch) -> dict:
    """Monkeypatch _tui_curses._apply to capture the req (and skip the real write)."""
    from levain import _tui_curses
    cap: dict = {}

    def fake_apply(model, source, req, ok_msg):  # noqa: ANN001
        cap["req"] = req
        cap["ok_msg"] = ok_msg
        return model

    monkeypatch.setattr(_tui_curses, "_apply", fake_apply)
    return cap


def test_handle_verb_keep_activate_dispatches_by_row_type(monkeypatch):
    from levain import _tui_curses
    cap = _capture_apply(monkeypatch)
    verb = _verb_of("keep", "keep_activate")
    p = AnnealPaths.from_db(Path("/x/memory.db"))
    # a NOTE → metabolize into a loop (set_disposition{loop})
    vnote = SubstrateView(paths=p, keep=[_spore("spore-n", disposition="note")])
    _tui_curses._handle_verb(None, _model_on(vnote, "keep", read_only=False), None, verb)
    assert cap["req"] == {"kind": "spore_set_disposition", "spore_id": "spore-n", "disposition": "loop"}
    # a parked LOOP → un-park (spore_update{tier:warm}) — the SAME key, different op
    cap.clear()
    vpark = SubstrateView(paths=p, keep=[_spore("spore-p", tier="parked")])
    _tui_curses._handle_verb(None, _model_on(vpark, "keep", read_only=False), None, verb)
    assert cap["req"] == {"kind": "spore_update", "spore_id": "spore-p", "tier": "warm"}


def test_handle_verb_descend_is_panel_aware(monkeypatch):
    from levain import _tui_curses
    cap = _capture_apply(monkeypatch)
    monkeypatch.setattr(_tui_curses, "_confirm", lambda stdscr, msg: True)
    monkeypatch.setattr(_tui_curses, "_choose", lambda stdscr, label, opts: opts[0])
    p = AnnealPaths.from_db(Path("/x/memory.db"))
    # Open Loops: a kind picker (_choose → first), then confirm
    vlo = SubstrateView(paths=p, open_spores=[_spore("spore-1", descend_kinds=["done", "dropped"])])
    _tui_curses._handle_verb(None, _model_on(vlo, "spores", read_only=False), None,
                             _verb_of("spores", "spore_descend"))
    assert cap["req"] == {"kind": "spore_descend", "spore_id": "spore-1",
                          "spore_kind": "done", "confirm": True}
    # Keep "remove": NO picker — auto-picks 'composted' over the first kind ('done')
    cap.clear()
    vk = SubstrateView(paths=p, keep=[_spore("spore-k", disposition="note",
                                             descend_kinds=["done", "composted"])])
    _tui_curses._handle_verb(None, _model_on(vk, "keep", read_only=False), None,
                             _verb_of("keep", "spore_descend"))
    assert cap["req"]["kind"] == "spore_descend" and cap["req"]["spore_kind"] == "composted"


def test_handle_verb_schedule_dash_clears_the_alarm(monkeypatch):
    from levain import _tui_curses
    cap = _capture_apply(monkeypatch)
    monkeypatch.setattr(_tui_curses, "_prompt_line", lambda stdscr, label: "-")
    p = AnnealPaths.from_db(Path("/x/memory.db"))
    v = SubstrateView(paths=p, open_spores=[_spore("spore-1", next="2026-07-01")])
    _tui_curses._handle_verb(None, _model_on(v, "spores", read_only=False), None,
                             _verb_of("spores", "spore_schedule"))
    assert cap["req"] == {"kind": "spore_surface_at", "spore_id": "spore-1", "surface_at": ""}


def test_handle_verb_capture_disposition_by_panel(monkeypatch):
    from levain import _tui_curses
    cap = _capture_apply(monkeypatch)
    monkeypatch.setattr(_tui_curses, "_edit_via_editor", lambda stdscr, initial: "a fresh dump")
    p = AnnealPaths.from_db(Path("/x/memory.db"))
    v = SubstrateView(paths=p, tray=[], keep=[])
    # Tray panel → a seed; Keep panel → a note (and NO type is sent — the AI sorts).
    _tui_curses._handle_verb(None, _model_on(v, "tray", read_only=False), None,
                             _verb_of("tray", "spore_capture"))
    assert cap["req"] == {"kind": "spore_seed", "text": "a fresh dump", "disposition": "seed"}
    cap.clear()
    _tui_curses._handle_verb(None, _model_on(v, "keep", read_only=False), None,
                             _verb_of("keep", "spore_capture"))
    assert cap["req"] == {"kind": "spore_seed", "text": "a fresh dump", "disposition": "note"}


def test_active_verbs_prunes_resolve_verbs_when_row_has_no_kinds():
    # NO THEATER (L1-M1): a row whose type yields no descend/ascend kinds must NOT advertise
    # [d]/[a] — the web gates these; the TUI footer now does too.
    from levain import _tui_curses
    p = AnnealPaths.from_db(Path("/x/memory.db"))
    v = SubstrateView(paths=p, open_spores=[_spore("spore-x", descend_kinds=[], ascend_kinds=[])])
    kinds = [x.kind for x in _tui_curses._active_verbs(_model_on(v, "spores", read_only=False))]
    assert "spore_descend" not in kinds and "spore_ascend" not in kinds
    assert "spore_touch" in kinds and "spore_edit" in kinds  # the kind-independent verbs stay
    # a row WITH kinds advertises them
    v2 = SubstrateView(paths=p,
                       open_spores=[_spore("spore-y", descend_kinds=["done"], ascend_kinds=["pattern"])])
    kinds2 = [x.kind for x in _tui_curses._active_verbs(_model_on(v2, "spores", read_only=False))]
    assert "spore_descend" in kinds2 and "spore_ascend" in kinds2


def test_verb_mediated_kinds_mirror_writes():
    # L1-L4: the undoable-check mirror MUST stay byte-equal to the server's verb-kind set,
    # else the edits panel offers [u]ndo on a row the server would 400.
    from levain import writes
    assert tui._VERB_MEDIATED_KINDS == writes._VERB_KINDS


def test_active_verbs_episodes_offers_tombstone():
    # Regression guard (codex L3): the _active_verbs early-return restructure must NOT route
    # the episodes panel through current_row (which covers only spores/tray/keep) — that would
    # silently drop episode_tombstone. A non-empty episodes panel advertises [x] tombstone;
    # read-only suppresses it.
    from levain import _tui_curses
    v = _view()  # the fixture carries 2 episodes
    kinds = [x.kind for x in _tui_curses._active_verbs(_model_on(v, "episodes", read_only=False))]
    assert kinds == ["episode_tombstone"]
    assert _tui_curses._active_verbs(_model_on(v, "episodes", read_only=True)) == []
