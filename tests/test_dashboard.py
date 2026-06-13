"""Tests for levain.dashboard — the Slice-1 read-only substrate assembly.

Pure helpers are unit-tested directly; ``build_substrate_view`` is exercised
against real (temp) anneal stores so the in-process call contract is verified,
not mocked. The populated crystal tier is covered end-to-end by the live-store
smoke (documented in next.md) + the corrupt-file fail-soft test here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from levain.dashboard import (
    AnnealPaths,
    _one_clause,
    _parse_sections,
    _truncate,
    build_substrate_view,
)


# --- pure helpers ----------------------------------------------------------

class TestOneClause:
    def test_em_dash_split(self) -> None:
        assert _one_clause("lead clause — felt prose tail") == "lead clause"

    def test_double_dash_split(self) -> None:
        assert _one_clause("lead -- tail") == "lead"

    def test_sentence_split_when_no_dash(self) -> None:
        assert _one_clause("First sentence. Second sentence.") == "First sentence"

    def test_semicolon_split(self) -> None:
        assert _one_clause("clause a; clause b") == "clause a"

    def test_truncates_long(self) -> None:
        out = _one_clause("word " * 100)
        assert len(out) <= 160
        assert out.endswith("…")

    def test_collapses_whitespace(self) -> None:
        assert _one_clause("a   b\n\nc — tail") == "a b c"


class TestTruncate:
    def test_short_untouched(self) -> None:
        assert _truncate("hi", 10) == "hi"

    def test_long_ellipsis(self) -> None:
        assert _truncate("abcdefghij", 5) == "abcd…"

    def test_collapses_whitespace(self) -> None:
        assert _truncate("a\n  b", 10) == "a b"


class TestParseSections:
    MD = (
        "# Title\n\n## State\nfocus line\nmore\n\n"
        "## Active Threads\n- t1\n- t2\n\n## Patterns\nx\n"
    )

    def test_returns_wanted_in_order(self) -> None:
        secs = _parse_sections(self.MD, ("State", "Active Threads"))
        assert [s.heading for s in secs] == ["State", "Active Threads"]
        assert "focus line" in secs[0].body
        assert "t1" in secs[1].body

    def test_missing_section_skipped(self) -> None:
        secs = _parse_sections(self.MD, ("State", "Nonexistent"))
        assert [s.heading for s in secs] == ["State"]

    def test_order_follows_request_not_doc(self) -> None:
        secs = _parse_sections(self.MD, ("Active Threads", "State"))
        assert [s.heading for s in secs] == ["Active Threads", "State"]

    def test_empty_markdown(self) -> None:
        assert _parse_sections("", ("State",)) == []


# --- AnnealPaths -----------------------------------------------------------

class TestAnnealPaths:
    def test_derives_siblings_by_stem(self) -> None:
        p = AnnealPaths.from_db("/x/y/memory.db")
        assert p.continuity_md == Path("/x/y/memory.continuity.md")
        assert p.crystal_json == Path("/x/y/memory.crystal.json")
        assert p.spores_json == Path("/x/y/memory.spores.json")

    def test_expanduser(self) -> None:
        p = AnnealPaths.from_db("~/foo/bar.db")
        assert "~" not in str(p.episodic_db)

    def test_nondefault_stem(self) -> None:
        p = AnnealPaths.from_db("/x/entity.db")
        assert p.continuity_md.name == "entity.continuity.md"
        assert p.spores_json.name == "entity.spores.json"


# --- build_substrate_view --------------------------------------------------

class TestBuildView:
    def test_missing_store_reports_not_fabricates(self, tmp_path: Path) -> None:
        """PURE READ: a missing store is REPORTED, never created. The dashboard
        must not fabricate the infrastructure it exists to inspect (no db file,
        no parent dirs)."""
        target = tmp_path / "nested" / "memory.db"
        v = build_substrate_view(AnnealPaths.from_db(target))
        assert not target.exists()
        assert not target.parent.exists()
        assert "store" in v.errors
        assert v.health is None
        assert v.graph is None

    def test_empty_existing_store_builds_clean(self, tmp_path: Path) -> None:
        """An existing-but-empty store produces a coherent empty view, no errors."""
        from anneal_memory import Store

        db = tmp_path / "memory.db"
        with Store(db):  # create an empty store
            pass
        v = build_substrate_view(AnnealPaths.from_db(db))
        assert v.errors == {}
        assert v.health is not None
        assert v.health.write_path_live is False
        assert v.health.total_episodes == 0
        assert v.crystal_index == []
        assert v.open_spores == []
        assert v.sections == []
        assert v.graph is not None
        assert v.graph.nodes == []
        assert v.graph.edges == []

    def test_graph_no_dangling_edges(self, tmp_path: Path) -> None:
        """Every emitted edge must have BOTH endpoints present as nodes."""
        from anneal_memory import Store

        db = tmp_path / "memory.db"
        with Store(db) as store:
            a = store.record("episode A", "decision")
            b = store.record("episode B", "observation")
            store.record_associations({(a.id, b.id)})
        v = build_substrate_view(AnnealPaths.from_db(db))
        assert v.errors == {}
        assert v.graph is not None
        assert len(v.graph.edges) >= 1
        node_ids = {n.id for n in v.graph.nodes}
        for e in v.graph.edges:
            assert e.source in node_ids
            assert e.target in node_ids

    def test_health_exposes_local_density(self, tmp_path: Path) -> None:
        from anneal_memory import Store

        db = tmp_path / "memory.db"
        with Store(db):
            pass
        v = build_substrate_view(AnnealPaths.from_db(db))
        assert v.health is not None
        assert hasattr(v.health, "local_density")
        assert "local_density" in v.health.to_dict()
        assert "wrap_in_progress" in v.health.to_dict()
        assert v.health.wrap_in_progress is False

    def test_health_continuity_chars_from_paths(self, tmp_path: Path) -> None:
        """continuity_chars must read the (overridable) AnnealPaths location, so
        it can't disagree with the sections (which read the same file)."""
        from anneal_memory import Store

        db = tmp_path / "memory.db"
        with Store(db):
            pass
        md = tmp_path / "memory.continuity.md"
        md.write_text("## State\nhello world\n", encoding="utf-8")
        v = build_substrate_view(AnnealPaths.from_db(db))
        assert v.health is not None
        assert v.health.continuity_chars == len(md.read_text(encoding="utf-8"))

    def test_graph_edge_cap_marks_truncated(self, tmp_path: Path) -> None:
        """The edge budget bounds materialization AND honestly marks truncated;
        no dangling edges even under the cap; affective_intensity is a float."""
        from anneal_memory import Store

        from levain.dashboard import _build_graph

        db = tmp_path / "memory.db"
        with Store(db) as store:
            ids = [store.record(f"ep{i}", "observation").id for i in range(4)]
            store.record_associations(
                {(ids[0], ids[1]), (ids[1], ids[2]), (ids[2], ids[3])}
            )
        with Store(db, read_only=True) as store:
            g = _build_graph(store, min_strength=0.0, max_nodes=300, max_edges=2)
        assert g.truncated is True
        assert len(g.edges) <= 2
        node_ids = {n.id for n in g.nodes}
        for e in g.edges:
            assert e.source in node_ids
            assert e.target in node_ids
            assert isinstance(e.affective_intensity, float)

    def test_populated_episodes_and_sections(self, tmp_path: Path) -> None:
        from anneal_memory import Store

        db = tmp_path / "memory.db"
        with Store(db) as store:
            store.record("decided X because Y", "decision")
            store.record("noticed Z", "observation")
        (tmp_path / "memory.continuity.md").write_text(
            "## State\ncurrent focus\n\n## Active Threads\n- thread one\n",
            encoding="utf-8",
        )
        v = build_substrate_view(AnnealPaths.from_db(db))
        assert v.errors == {}
        assert v.health is not None
        assert v.health.total_episodes == 2
        assert [s.heading for s in v.sections] == ["State", "Active Threads"]
        assert "current focus" in v.sections[0].body

    def test_open_spores_surfaced(self, tmp_path: Path) -> None:
        from anneal_memory.spores import SporeStore

        db = tmp_path / "memory.db"
        SporeStore(tmp_path / "memory.spores.json").add(
            type="task", text="an open loop", tier="hot", salience=2
        )
        v = build_substrate_view(AnnealPaths.from_db(db))
        assert len(v.open_spores) == 1
        assert v.open_spores[0].text == "an open loop"
        assert v.open_spores[0].tier == "hot"

    def test_max_spores_cap(self, tmp_path: Path) -> None:
        from anneal_memory.spores import SporeStore

        db = tmp_path / "memory.db"
        store = SporeStore(tmp_path / "memory.spores.json")
        for i in range(5):
            store.add(type="task", text=f"loop {i}", tier="warm")
        v = build_substrate_view(AnnealPaths.from_db(db), max_spores=3)
        assert len(v.open_spores) == 3

    def test_corrupt_crystal_is_failsoft(self, tmp_path: Path) -> None:
        """A corrupt crystal store degrades VISIBLY (errors) without blanking the
        rest of the board — the invisible_infrastructure_failure guard."""
        from anneal_memory import Store

        db = tmp_path / "memory.db"
        with Store(db):
            pass
        (tmp_path / "memory.crystal.json").write_text("{not valid json", encoding="utf-8")
        v = build_substrate_view(AnnealPaths.from_db(db))
        assert "crystal_index" in v.errors
        assert v.crystal_index == []
        assert v.health is not None  # health tier unaffected by the crystal fault

    def test_to_dict_is_json_serializable(self, tmp_path: Path) -> None:
        import json

        v = build_substrate_view(AnnealPaths.from_db(tmp_path / "memory.db"))
        # must round-trip without raising
        json.dumps(v.to_dict())


# --- CLI surface: run_dashboard / render_text ------------------------------

class TestCLISurface:
    @staticmethod
    def _make_install(tmp_path: Path) -> Path:
        from anneal_memory import Store

        levain_dir = tmp_path / ".levain"
        levain_dir.mkdir()
        with Store(levain_dir / "memory.db") as store:
            store.record("decided X because Y", "decision")
        (levain_dir / "memory.continuity.md").write_text(
            "## State\nfocus\n\n## Active Threads\n- t1\n", encoding="utf-8"
        )
        return tmp_path

    def test_no_store_returns_1(self, tmp_path: Path, capsys) -> None:
        from levain.dashboard import run_dashboard

        rc = run_dashboard(tmp_path)
        assert rc == 1
        assert "No anneal store" in capsys.readouterr().err

    def test_text_render_returns_0(self, tmp_path: Path, capsys) -> None:
        from levain.dashboard import run_dashboard

        rc = run_dashboard(self._make_install(tmp_path))
        out = capsys.readouterr().out
        assert rc == 0
        assert "Levain substrate" in out
        assert "Health" in out
        assert "State" in out

    def test_json_render(self, tmp_path: Path, capsys) -> None:
        import json

        from levain.dashboard import run_dashboard

        rc = run_dashboard(self._make_install(tmp_path), as_json=True)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["health"]["total_episodes"] == 1
        assert "graph" in payload

    def test_via_cli_main(self, tmp_path: Path, capsys) -> None:
        import json

        from levain.cli import main

        rc = main(["dashboard", "--path", str(self._make_install(tmp_path)), "--json"])
        assert rc == 0
        assert json.loads(capsys.readouterr().out)["health"]["total_episodes"] == 1

    def test_render_text_handles_dark_write_path(self, tmp_path: Path) -> None:
        from anneal_memory import Store

        from levain.dashboard import AnnealPaths, build_substrate_view, render_text

        db = tmp_path / "memory.db"
        with Store(db):
            pass
        v = build_substrate_view(AnnealPaths.from_db(db))
        text = render_text(v)
        assert "DARK" in text  # an existing empty store has no associations

    def test_render_text_with_content(self, tmp_path: Path) -> None:
        from anneal_memory import Store
        from anneal_memory.spores import SporeStore

        from levain.dashboard import AnnealPaths, build_substrate_view, render_text

        db = tmp_path / "memory.db"
        with Store(db) as store:
            a = store.record("episode A", "decision")
            b = store.record("episode B", "observation")
            store.record_associations({(a.id, b.id)})
        SporeStore(tmp_path / "memory.spores.json").add(
            type="task", text="loop", tier="hot"
        )
        (tmp_path / "memory.continuity.md").write_text("## State\nfoc\n", encoding="utf-8")
        text = render_text(build_substrate_view(AnnealPaths.from_db(db)))
        assert "LIVE" in text  # has associations
        assert "Open loops" in text
        assert "State" in text


# --- Slice 1.5: SHOW EVERYTHING — source seam, config tier, IA manifest -----


def _make_levain_install(tmp_path: Path) -> Path:
    """A realistic Levain install: anneal store under ``.levain/`` + the seed/
    config surface at the install root. The shape ``SubstrateSource.local`` +
    the 1.5 config tier read from."""
    from anneal_memory import Store

    root = tmp_path / "entity"
    (root / ".levain").mkdir(parents=True)
    with Store(root / ".levain" / "memory.db") as store:
        store.record("decided X because Y", "decision", metadata={"tags": ["alpha", "beta"]})
        store.record("noticed Z", "observation")
    (root / ".levain" / "memory.continuity.md").write_text(
        "# Continuity — Sage\n\n## State\nfocus line\n\n## Active Threads\n- t1\n\n"
        "## Patterns\n- p1\n\n## Decisions\n- d1\n\n## Context\nctx\n\n"
        "## Understanding\nthe felt layer\n",
        encoding="utf-8",
    )
    seed = root / "seed"
    seed.mkdir()
    (seed / "origin.md").write_text("# Who You Are — Sage\n\nYou are Sage.\n", encoding="utf-8")
    (seed / "world.md").write_text(
        "# Who Your Operator Is\n\n## Identity\nAda. Engineer.\n\n"
        "## Communication\nDirect, dense.\n",
        encoding="utf-8",
    )
    (seed / "partnership.md").write_text("# How We Work\n\nPartner, not assistant.\n", encoding="utf-8")
    (seed / "memory.md").write_text("# Your Memory\n\nanneal.\n", encoding="utf-8")
    (seed / "spore_instructions.md").write_text("# Open Loops\n\nspores.\n", encoding="utf-8")
    activation = root / "activation"
    activation.mkdir()
    (activation / "posture.md").write_text("# Posture\n\nthink out loud.\n", encoding="utf-8")
    (activation / "recency_directives.md").write_text("# Recency\n\nverify.\n", encoding="utf-8")
    return root


class TestSubstrateSource:
    def test_local_derives_store_and_root(self, tmp_path: Path) -> None:
        from levain.dashboard import SubstrateSource

        root = tmp_path / "entity"
        src = SubstrateSource.local(root)
        assert src.install_root == root.resolve()
        assert src.anneal.episodic_db == (root / ".levain" / "memory.db").resolve()
        assert src.scope == "personal"

    def test_build_threads_install_root(self, tmp_path: Path) -> None:
        from levain.dashboard import SubstrateSource

        root = _make_levain_install(tmp_path)
        v = SubstrateSource.local(root).build()
        assert v.errors == {}
        assert v.config_docs  # the seed tier reached because install_root flowed through
        assert v.scope == "personal"


class TestConfigTier:
    def test_config_docs_classed_and_zoned(self, tmp_path: Path) -> None:
        from levain.dashboard import SubstrateSource

        root = _make_levain_install(tmp_path)
        v = SubstrateSource.local(root).build()
        by_key = {d.key: d for d in v.config_docs}
        # origin.md is Class C-view (read-only — the entity's own self-statement, not
        # an operator input; Slice-2 sharpening of §4). posture/recency stay Class A.
        assert by_key["origin"].edit_class == "C"
        assert by_key["origin"].zone == "identity"
        assert by_key["origin"].heading is None  # whole-file doc, no section address
        assert by_key["posture"].edit_class == "A"
        # world.md splits into one Class-A doc per section, each carrying its exact
        # ## heading as the Slice-2 write address.
        assert "world:identity" in by_key and "world:communication" in by_key
        assert by_key["world:identity"].edit_class == "A"
        assert by_key["world:identity"].heading == "Identity"
        assert "Ada. Engineer." in by_key["world:identity"].body
        # constitution files are Class C-view
        assert by_key["partnership"].edit_class == "C"
        assert by_key["memory"].edit_class == "C"
        # every config doc carries a source path relative to the install root
        assert by_key["origin"].source == "seed/origin.md"
        assert by_key["posture"].source == "activation/posture.md"

    def test_entity_name_from_origin_h1(self, tmp_path: Path) -> None:
        from levain.dashboard import SubstrateSource

        root = _make_levain_install(tmp_path)
        v = SubstrateSource.local(root).build()
        assert v.entity_name == "Sage"

    def test_no_install_root_means_no_config_no_error(self, tmp_path: Path) -> None:
        """A bare store (no install context) simply has no config tier — NOT an
        error (there is nothing to read there)."""
        from anneal_memory import Store

        db = tmp_path / "memory.db"
        with Store(db):
            pass
        v = build_substrate_view(AnnealPaths.from_db(db))
        assert v.config_docs == []
        assert "config" not in v.errors
        assert v.entity_name is None

    def test_missing_install_root_is_failsoft(self, tmp_path: Path) -> None:
        """install_root given but absent → degrades VISIBLY (config error), never
        blanks the store tiers."""
        from anneal_memory import Store

        db = tmp_path / "memory.db"
        with Store(db):
            pass
        v = build_substrate_view(
            AnnealPaths.from_db(db), install_root=tmp_path / "nope"
        )
        assert "config" in v.errors
        assert v.health is not None  # store tiers unaffected


class TestSlice15Surfaces:
    def test_all_six_sections_with_edit_class(self, tmp_path: Path) -> None:
        root = _make_levain_install(tmp_path)
        from levain.dashboard import SubstrateSource

        v = SubstrateSource.local(root).build()
        headings = [s.heading for s in v.sections]
        assert headings == ["State", "Active Threads", "Patterns", "Decisions",
                            "Context", "Understanding"]
        classes = {s.heading: s.edit_class for s in v.sections}
        assert classes["State"] == "A"  # free-text, last-writer-wins
        assert classes["Patterns"] == "C"  # the consolidate owns it
        assert all(s.zone == "mind" for s in v.sections)

    def test_recent_episodes_surfaced(self, tmp_path: Path) -> None:
        root = _make_levain_install(tmp_path)
        from levain.dashboard import SubstrateSource

        v = SubstrateSource.local(root).build()
        assert len(v.episodes) == 2
        # newest first (recall is ORDER BY timestamp DESC)
        types = {e.type for e in v.episodes}
        assert "decision" in types and "observation" in types
        decision = next(e for e in v.episodes if e.type == "decision")
        assert decision.tags == ["alpha", "beta"]

    def test_max_episodes_cap(self, tmp_path: Path) -> None:
        from anneal_memory import Store

        db = tmp_path / "memory.db"
        with Store(db) as store:
            for i in range(6):
                store.record(f"ep {i}", "observation")
        v = build_substrate_view(AnnealPaths.from_db(db), max_episodes=3)
        assert len(v.episodes) == 3

    def test_wraps_empty_on_unwrapped_store(self, tmp_path: Path) -> None:
        from anneal_memory import Store

        db = tmp_path / "memory.db"
        with Store(db):
            pass
        v = build_substrate_view(AnnealPaths.from_db(db))
        assert v.wraps == []

    def test_layout_manifest_is_ordered_zoned_classed(self, tmp_path: Path) -> None:
        root = _make_levain_install(tmp_path)
        from levain.dashboard import SubstrateSource

        v = SubstrateSource.local(root).build()
        layout = v.layout()
        kinds = [e["kind"] for e in layout]
        # every panel kind is represented
        for k in ("config", "spores", "episodes", "edits", "health", "graph",
                  "crystals", "section", "wraps"):
            assert k in kinds
        # zones appear in IA order: identity → operate → mind (no interleaving)
        zones = [e["zone"] for e in layout]
        first_idx = {z: zones.index(z) for z in ("identity", "operate", "mind")}
        assert first_idx["identity"] < first_idx["operate"] < first_idx["mind"]
        last_identity = max(i for i, z in enumerate(zones) if z == "identity")
        assert last_identity < first_idx["operate"]
        # every edit-classed tier carries A/B/C; the "edits" meta-panel (the audit
        # log, not an editable tier) legitimately carries no class (Slice 2a).
        assert all(
            e["edit_class"] in ("A", "B", "C")
            for e in layout if e["kind"] != "edits"
        )
        edits_panel = next(e for e in layout if e["kind"] == "edits")
        assert edits_panel["edit_class"] == "" and edits_panel["zone"] == "operate"
        # the edit-class chip encodes the EDIT MODEL (§4): the Operate zone is
        # verb-mediated lifecycle data — spores AND episodes are Class B (episode
        # mutation = tombstone verb), NOT direct-edit. [L4 HIGH]
        by_kind = {e["kind"]: e for e in layout}
        assert by_kind["spores"]["edit_class"] == "B"
        assert by_kind["episodes"]["edit_class"] == "B"
        # consolidated cognition is read-only Class C
        assert by_kind["health"]["edit_class"] == "C"
        assert by_kind["crystals"]["edit_class"] == "C"
        # indexed kinds (config/section) carry a ref into their collection
        for e in layout:
            if e["kind"] in ("config", "section"):
                assert "ref" in e

    def test_to_dict_has_15_surfaces_and_is_serializable(self, tmp_path: Path) -> None:
        import json

        root = _make_levain_install(tmp_path)
        from levain.dashboard import SubstrateSource

        d = SubstrateSource.local(root).build().to_dict()
        for key in ("scope", "entity_name", "episodes", "config_docs", "wraps", "layout"):
            assert key in d
        json.dumps(d)  # round-trips without raising

    def test_render_text_includes_new_surfaces(self, tmp_path: Path) -> None:
        from levain.dashboard import SubstrateSource, render_text

        root = _make_levain_install(tmp_path)
        text = render_text(SubstrateSource.local(root).build())
        assert "Sage" in text  # entity name in the header
        assert "Recent episodes" in text
        assert "Seed / config" in text
        assert "Understanding" in text  # all six sections rendered


class TestFailSoftBoundaries:
    """Apparatus L1/L2 catches: a non-UTF8 file must degrade ONLY its own read,
    never escape to blank a store-derived or sibling tier."""

    def test_non_utf8_continuity_contains_blast_radius(self, tmp_path: Path) -> None:
        """A non-UTF8 continuity.md faults anneal's status() (which reads the file),
        so health degrades VISIBLY — but the wraps, graph, and episode tiers (which
        do NOT read continuity) must survive as INDEPENDENT tries, no collateral.
        [L1-H1 — root cause: anneal status() reads continuity; fix decouples wraps]"""
        from anneal_memory import Store

        db = tmp_path / "memory.db"
        with Store(db):
            pass
        (tmp_path / "memory.continuity.md").write_bytes(b"## State\n\xff\xfe not utf8\n")
        v = build_substrate_view(AnnealPaths.from_db(db))
        # health degrades VISIBLY (not a silent blank) — the fail-soft contract
        assert v.health is None
        assert "health" in v.errors
        # but the tiers that do NOT depend on the continuity file survive cleanly
        assert "wraps" not in v.errors and v.wraps == []
        assert "graph" not in v.errors and v.graph is not None
        assert "episodes" not in v.errors
        assert "sections" in v.errors  # the section read correctly degrades too

    def test_one_non_utf8_seed_file_keeps_config_tier(self, tmp_path: Path) -> None:
        """A single corrupt seed file is skipped like a missing one — the rest of
        the config tier (and entity_name) survive. [L1-M1]"""
        root = _make_levain_install(tmp_path)
        # corrupt ONE seed file
        (root / "seed" / "world.md").write_bytes(b"## Identity\n\xff\xfe broken\n")
        from levain.dashboard import SubstrateSource

        v = SubstrateSource.local(root).build()
        keys = {d.key for d in v.config_docs}
        assert "origin" in keys  # the other config docs survive
        assert "posture" in keys
        assert not any(k.startswith("world:") for k in keys)  # the corrupt file skipped
        assert v.entity_name == "Sage"  # name read (origin.md) unaffected
        assert "config" not in v.errors  # NOT a whole-tier failure

    def test_malformed_wrap_counter_contains_blast_radius(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wrap row with a non-int counter (SQLite's loose INTEGER affinity, or a
        legacy/buggy writer) must degrade ONLY the wraps tier — it must NOT escape as
        a TypeError through the sort key or health's sum() and blank the whole view.
        [codex L3 HIGH — the wraps/health analogue of the non-utf8 blast-radius]"""
        import anneal_memory

        db = tmp_path / "memory.db"
        with anneal_memory.Store(db):
            pass

        class _BadRec:  # a wrap record with TEXT where an INTEGER counter belongs
            wrapped_at = "2026-06-12T10:00:00"
            episodes_compressed = 5
            continuity_chars = 100
            graduations_validated = 1
            graduations_demoted = 0
            associations_formed = "bad"  # ← the affinity-loose poison value
            associations_strengthened = 2
            associations_decayed = 0

        monkeypatch.setattr(
            anneal_memory.Store, "get_wrap_history", lambda self: [_BadRec()]
        )
        v = build_substrate_view(AnnealPaths.from_db(db))  # must NOT raise

        assert "wraps" in v.errors  # the wraps tier degrades VISIBLY, in isolation
        assert v.wraps == []
        assert v.health is not None  # health survives (sums over the empty wraps → 0)
        assert "episodes" not in v.errors  # sibling tiers untouched


class TestRenderSummaryContract:
    def test_summary_omits_non_headline_sections(self, tmp_path: Path) -> None:
        """The MODEL-visible summary must stay to State + Active Threads even though
        the view now carries all six sections — the all-six expansion must not bloat
        model context. [L2-M2]"""
        from levain.dashboard import SubstrateSource, render_summary

        root = _make_levain_install(tmp_path)
        summary = render_summary(SubstrateSource.local(root).build())
        assert "State:" in summary
        assert "Active Threads:" in summary
        assert "Patterns:" not in summary
        assert "Understanding:" not in summary
        assert "Decisions:" not in summary


class TestL3Fixes:
    def test_config_layout_entries_carry_source(self, tmp_path: Path) -> None:
        """The manifest must thread ConfigDoc.source so the UI can show which
        seed/config file a panel came from. [codex L3 LOW]"""
        from levain.dashboard import SubstrateSource

        root = _make_levain_install(tmp_path)
        layout = SubstrateSource.local(root).build().layout()
        config_entries = [e for e in layout if e["kind"] == "config"]
        assert config_entries
        assert all("source" in e and e["source"] for e in config_entries)
        origin = next(e for e in config_entries if "origin" in e["source"])
        assert origin["source"] == "seed/origin.md"

    def test_graph_truncated_when_recall_window_drops_episodes(self) -> None:
        """If the 100k recall window itself dropped episodes (total_matching >
        returned), the graph is already truncated — honest from the start. [codex L3]"""
        from types import SimpleNamespace

        from levain.dashboard import _build_graph

        # a stub store: recall returns 1 episode but reports 5 total matching
        ep = SimpleNamespace(id="e1", type=SimpleNamespace(value="observation"), content="x")
        stub = SimpleNamespace(
            recall=lambda limit: SimpleNamespace(episodes=[ep], total_matching=5),
            get_associations=lambda chunk, min_strength, limit: [],
        )
        g = _build_graph(stub, min_strength=0.0, max_nodes=300, max_edges=2000)
        assert g.truncated is True


class TestEntityNameExtraction:
    def test_h1_suffix_forms(self) -> None:
        from levain.dashboard import _h1_name_suffix

        assert _h1_name_suffix("# Who You Are — Sage\n") == "Sage"
        assert _h1_name_suffix("# Continuity — Atlas\nbody") == "Atlas"
        assert _h1_name_suffix("# Who You Are - Hyphen\n") == "Hyphen"

    def test_h1_without_suffix_is_none(self) -> None:
        """The flow-store shape ``# flow — Memory (v1)`` would mis-extract a name;
        the suffix form is only trusted from origin.md, and a plain H1 yields None
        (no fabricated name)."""
        from levain.dashboard import _h1_name_suffix

        assert _h1_name_suffix("# Just A Title\nbody") is None
        assert _h1_name_suffix("no heading here") is None
