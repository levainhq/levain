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
    recall_episode_rows,
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

    def test_long_spore_text_rides_full_and_raw_on_the_wire(self, tmp_path: Path) -> None:
        # DATA-SAFETY regression: the wire MUST carry the FULL, RAW spore text — not a
        # 200-char whitespace-collapsed cap. The old cap (a) hid the tail with no way to
        # see it AND (b) seeded the 3b text-editor, so save wrote the truncation back =
        # silent destruction of everything past char 200. Newlines must survive too (the
        # old _truncate collapsed them → an edit would have flattened structure). The
        # surface clamps the ROW for density; the canonical text stays whole.
        from anneal_memory.spores import SporeStore

        long_text = "first line\n" + ("L" * 600) + "\n\ntail marker"  # past 200, multi-line
        db = tmp_path / "memory.db"
        SporeStore(tmp_path / "memory.spores.json").add(
            type="task", text=long_text, tier="hot", salience=2
        )
        v = build_substrate_view(AnnealPaths.from_db(db))
        assert v.open_spores[0].text == long_text  # FULL + RAW, byte-for-byte

    def test_long_episode_content_rides_full_on_the_wire(self, tmp_path: Path) -> None:
        # Same data-safety contract for episode content (was capped at 280). Read-only, so
        # no edit-loss, but the same un-seeable-tail bug the surface now solves with a
        # per-item expand over the full canonical content.
        from anneal_memory import Store

        long_content = "E" * 900  # past the old 280-char cap
        db = tmp_path / "memory.db"
        with Store(db) as store:
            store.record(long_content, "observation")
        v = build_substrate_view(AnnealPaths.from_db(db))
        assert any(e.content == long_content for e in v.episodes)  # FULL, byte-for-byte

    def test_source_level_max_spores_threads_to_every_build(self, tmp_path: Path) -> None:
        # [codex L3 MED] A source-level max_spores must apply to EVERY build() path —
        # the web /substrate.json, the TUI refresh, and post-write rebuilds all call a
        # BARE source.build(), not an explicit build(max_spores=) kwarg. Without the
        # source-level field, "show ALL spores" silently reverts to the default cap after
        # the first render.
        from anneal_memory.spores import SporeStore

        from levain.dashboard import SubstrateSource

        db = tmp_path / "memory.db"
        spores = SporeStore(tmp_path / "memory.spores.json")
        for i in range(3):
            spores.add(type="task", text=f"loop {i}", tier="hot", salience=2)
        anneal = AnnealPaths.from_db(db)
        capped = SubstrateSource(anneal=anneal, max_spores=2).build()  # source-level cap
        assert len(capped.open_spores) == 2
        uncapped = SubstrateSource(anneal=anneal).build()  # no cap → all three
        assert len(uncapped.open_spores) == 3

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


class TestSporeProjections:
    """Slice 3: the open-spore layer splits into Open Loops / Tray / Keep by disposition
    + tier (``levain.spores.bucket_of``). Tests write spores.json DIRECTLY — anneal's
    ``SporeStore.add`` is disposition-blind by design (the de-risk: anneal round-trips an
    unknown ``disposition`` untouched), so a disposition can only be set out-of-band."""

    @staticmethod
    def _write(path: Path, rows: list[dict]) -> None:
        import json

        path.write_text(
            json.dumps({"schema_version": 1, "resolved": [], "spores": rows}),
            encoding="utf-8",
        )

    @staticmethod
    def _row(**kw: object) -> dict:
        base: dict = {"type": "task", "tier": "warm", "salience": 2, "domain": "x",
                      "seen": "2026-06-18"}
        base.update(kw)
        return base

    def test_buckets_partition_by_disposition_and_tier(self, tmp_path: Path) -> None:
        db = tmp_path / "memory.db"
        self._write(tmp_path / "memory.spores.json", [
            self._row(id="s1", tier="hot", text="live loop"),
            self._row(id="s2", text="fresh dump", disposition="seed"),
            self._row(id="s3", type="question", text="handoff", disposition="handoff"),
            self._row(id="s4", type="idea", text="agenda", disposition="agenda"),
            self._row(id="s5", tier="parked", text="kept reference"),
            self._row(id="s6", tier="parked", text="parked seed", disposition="seed"),
        ])
        v = build_substrate_view(AnnealPaths.from_db(db), max_spores=1000)
        assert [s.id for s in v.open_spores] == ["s1"]
        # disposition WINS over the parked tier: s6 (parked seed) is an un-triaged Tray
        # item, not Keep — keeping the render boundary identical to the cognition-exclude.
        assert {s.id for s in v.tray} == {"s2", "s3", "s4", "s6"}
        assert [s.id for s in v.keep] == ["s5"]
        # TOTAL partition — every spore lands in exactly one bucket, nothing lost.
        assert len(v.open_spores) + len(v.tray) + len(v.keep) == 6

    def test_disposition_defaults_to_loop(self, tmp_path: Path) -> None:
        db = tmp_path / "memory.db"
        self._write(tmp_path / "memory.spores.json",
                    [self._row(id="s1", tier="hot", text="no disposition key")])
        v = build_substrate_view(AnnealPaths.from_db(db))
        assert v.open_spores[0].disposition == "loop"

    def test_unknown_disposition_fails_open_to_loop(self, tmp_path: Path) -> None:
        # is_loop fails OPEN: an unknown/typo disposition reads as an in-cognition loop (a
        # real loop wrongly hidden is the silent-harm class) → it stays in Open Loops, not
        # silently swallowed into the Tray.
        db = tmp_path / "memory.db"
        self._write(tmp_path / "memory.spores.json",
                    [self._row(id="s1", tier="hot", text="typo", disposition="sed")])
        v = build_substrate_view(AnnealPaths.from_db(db))
        assert [s.id for s in v.open_spores] == ["s1"]
        assert v.tray == [] and v.keep == []

    def test_each_bucket_capped_independently(self, tmp_path: Path) -> None:
        db = tmp_path / "memory.db"
        rows: list[dict] = []
        for i in range(4):
            rows.append(self._row(id=f"loop{i}", tier="hot", text=f"loop {i}"))
            rows.append(self._row(id=f"seed{i}", text=f"seed {i}", disposition="seed"))
            rows.append(self._row(id=f"keep{i}", tier="parked", text=f"keep {i}"))
        self._write(tmp_path / "memory.spores.json", rows)
        v = build_substrate_view(AnnealPaths.from_db(db), max_spores=2)
        assert len(v.open_spores) == 2 and len(v.tray) == 2 and len(v.keep) == 2

    def test_keep_not_starved_by_loop_heavy_store(self, tmp_path: Path) -> None:
        # Parked (Keep) items rank LAST (parked sorts after hot/warm/cold); a flat
        # [:max_spores] on the ranked list would drop them. Bucketing the FULL list keeps
        # Keep alive even when active loops exceed the cap.
        db = tmp_path / "memory.db"
        rows = [self._row(id=f"loop{i}", tier="hot", text=f"loop {i}") for i in range(60)]
        rows.append(self._row(id="kept", tier="parked", text="must survive"))
        self._write(tmp_path / "memory.spores.json", rows)
        v = build_substrate_view(AnnealPaths.from_db(db))  # default max_spores=50
        assert len(v.open_spores) == 50
        assert [s.id for s in v.keep] == ["kept"]

    def test_resolved_status_drift_row_is_skipped(self, tmp_path: Path) -> None:
        # A status=resolved row left in the `spores` array is store drift (anneal MOVES
        # resolved spores to the `resolved` array); list_open doesn't filter status, so the
        # dashboard skips it defensively — it must not render as an open loop with verbs.
        db = tmp_path / "memory.db"
        self._write(tmp_path / "memory.spores.json", [
            self._row(id="s1", tier="hot", text="live loop"),
            self._row(id="drift", tier="hot", text="resolved drift", status="resolved"),
        ])
        v = build_substrate_view(AnnealPaths.from_db(db), max_spores=1000)
        all_ids = ([s.id for s in v.open_spores]
                   + [s.id for s in v.tray] + [s.id for s in v.keep])
        assert all_ids == ["s1"]  # the drift row excluded from EVERY bucket

    def test_non_str_next_is_coerced_not_crashed(self, tmp_path: Path) -> None:
        # A corrupted store could carry a non-str `next`; the TUI runs _oneline(next) which
        # would crash on a non-str. The builder str-coerces it (None stays None).
        db = tmp_path / "memory.db"
        self._write(tmp_path / "memory.spores.json",
                    [self._row(id="s1", tier="hot", text="loop", next=20260626)])
        v = build_substrate_view(AnnealPaths.from_db(db))
        assert v.open_spores[0].next == "20260626"

    def test_to_dict_and_render_text_carry_tray_and_keep(self, tmp_path: Path) -> None:
        from levain.dashboard import render_text

        db = tmp_path / "memory.db"
        self._write(tmp_path / "memory.spores.json", [
            self._row(id="s1", tier="hot", text="live loop"),
            self._row(id="s2", text="a fresh dump", disposition="seed"),
            self._row(id="s5", tier="parked", text="kept reference"),
        ])
        v = build_substrate_view(AnnealPaths.from_db(db))
        d = v.to_dict()
        assert [s["id"] for s in d["tray"]] == ["s2"]
        assert [s["id"] for s in d["keep"]] == ["s5"]
        text = render_text(v)
        assert "Tray (1)" in text and "[seed] a fresh dump" in text
        assert "Keep (1)" in text and "kept reference" in text


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
        for k in ("config", "spores", "tray", "keep", "episodes", "edits", "health",
                  "graph", "crystals", "section", "wraps"):
            assert k in kinds
        # zones appear in IA order: identity → operate → mind (no interleaving)
        zones = [e["zone"] for e in layout]
        first_idx = {z: zones.index(z) for z in ("identity", "operate", "mind")}
        assert first_idx["identity"] < first_idx["operate"] < first_idx["mind"]
        last_identity = max(i for i, z in enumerate(zones) if z == "identity")
        assert last_identity < first_idx["operate"]
        # every edit-classed tier carries A/B/C; the only no-class panel is the "edits"
        # audit log (Slice 2a, read-only display). Tray + Keep were read-only in 3a and
        # FLIP to Class B in 3b (the governed dump/sort/park verbs).
        _no_class = {"edits"}
        assert all(
            e["edit_class"] in ("A", "B", "C")
            for e in layout if e["kind"] not in _no_class
        )
        edits_panel = next(e for e in layout if e["kind"] == "edits")
        assert edits_panel["edit_class"] == "" and edits_panel["zone"] == "operate"
        # the edit-class chip encodes the EDIT MODEL (§4): the Operate zone is
        # verb-mediated lifecycle data — spores AND episodes are Class B (episode
        # mutation = tombstone verb), NOT direct-edit. [L4 HIGH]
        by_kind = {e["kind"]: e for e in layout}
        assert by_kind["spores"]["edit_class"] == "B"
        assert by_kind["episodes"]["edit_class"] == "B"
        # Tray + Keep (Slice 3b): now Class B in the Operate zone (dump/triage/park verbs).
        for k in ("tray", "keep"):
            assert by_kind[k]["edit_class"] == "B" and by_kind[k]["zone"] == "operate"
        # consolidated cognition is read-only Class C
        assert by_kind["health"]["edit_class"] == "C"
        assert by_kind["crystals"]["edit_class"] == "C"
        # indexed kinds (config/section) carry a ref into their collection
        for e in layout:
            if e["kind"] in ("config", "section"):
                assert "ref" in e

    def test_section_panels_carry_state_write_address(self, tmp_path: Path) -> None:
        # Slice 2b: section panels carry source + heading (the write-address) so the
        # Class-A State section is editable through the governed seam; every section
        # panel's source is the continuity file, and only State is edit_class A.
        root = _make_levain_install(tmp_path)
        from levain.dashboard import SubstrateSource

        v = SubstrateSource.local(root).build()
        sect_panels = [e for e in v.layout() if e["kind"] == "section"]
        assert sect_panels, "expected section panels"
        cont_rel = str(Path(".levain") / "memory.continuity.md")
        for e in sect_panels:
            assert e["source"] == cont_rel
            assert e["heading"] == e["title"]
        state_panel = next(e for e in sect_panels if e["heading"] == "State")
        assert state_panel["edit_class"] == "A"
        felt = [e for e in sect_panels if e["heading"] != "State"]
        assert felt and all(e["edit_class"] == "C" for e in felt)

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
        """A non-UTF8 continuity.md degrades ONLY its own read. With AM-STATUS-HARDEN
        (anneal 0.9.0) anneal's status() no longer faults on a corrupt continuity — it
        returns valid health with continuity_chars=None — so the blast radius is now
        even tighter than the original L1-H1 fix targeted: the health CARD survives,
        only the continuity-size field (and the felt-layer section read) degrade. The
        wraps, graph, and episode tiers (which never touch continuity) stay clean.
        [L1-H1 lineage; the dashboard's own cont_chars guard was forward-defense for
        exactly this hardening — now the live path.]"""
        from anneal_memory import Store

        db = tmp_path / "memory.db"
        with Store(db):
            pass
        (tmp_path / "memory.continuity.md").write_bytes(b"## State\n\xff\xfe not utf8\n")
        v = build_substrate_view(AnnealPaths.from_db(db))
        # health SURVIVES (anneal 0.9.0 hardened status()); only the one field that
        # actually needs to read the continuity file degrades — visibly, in isolation.
        assert v.health is not None
        assert "health" not in v.errors
        assert v.health.continuity_chars is None
        # the tiers that do NOT depend on the continuity file survive cleanly
        assert "wraps" not in v.errors and v.wraps == []
        assert "graph" not in v.errors and v.graph is not None
        assert "episodes" not in v.errors
        assert "sections" in v.errors  # the felt-layer section read correctly degrades too

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


class TestRecallEpisodeRows:
    """recall_episode_rows — the read-only keyword-search data layer (spore-107)."""

    def _db(self, tmp_path: Path):
        from anneal_memory import Store

        db = tmp_path / "memory.db"
        with Store(db) as store:
            store.record("decided X because Y", "decision")
            store.record("noticed Z about bridges", "observation")
        return db

    def test_keyword_matches_content(self, tmp_path: Path) -> None:
        rows, err = recall_episode_rows(self._db(tmp_path), keyword="bridges", limit=10)
        assert err is None
        assert len(rows) == 1
        assert "bridges" in rows[0].content

    def test_row_shape_matches_panel(self, tmp_path: Path) -> None:
        # search rows MUST carry the SAME field set as the recent-episodes panel
        rows, _err = recall_episode_rows(self._db(tmp_path), keyword="decided", limit=10)
        assert set(rows[0].to_dict()) == {"id", "timestamp", "type", "source", "content", "tags"}

    def test_no_match_is_empty_not_error(self, tmp_path: Path) -> None:
        rows, err = recall_episode_rows(self._db(tmp_path), keyword="zzznotpresent", limit=10)
        assert err is None and rows == []

    def test_empty_keyword_is_noop(self, tmp_path: Path) -> None:
        # an empty keyword never asks the store to match everything
        rows, err = recall_episode_rows(self._db(tmp_path), keyword="", limit=10)
        assert err is None and rows == []

    def test_missing_store_degrades_not_raises(self, tmp_path: Path) -> None:
        rows, err = recall_episode_rows(tmp_path / "nope.db", keyword="x", limit=10)
        assert rows == []
        assert err is not None and "no anneal store" in err

    def test_limit_caps_results(self, tmp_path: Path) -> None:
        from anneal_memory import Store

        db = tmp_path / "memory.db"
        with Store(db) as store:
            for i in range(5):
                store.record(f"shared token episode {i}", "observation")
        rows, _err = recall_episode_rows(db, keyword="shared token", limit=3)
        assert len(rows) == 3

    def test_keyword_over_cap_rejected(self, tmp_path: Path) -> None:
        # an over-long keyword is refused (parity with the POST-body cap) rather than
        # forcing an unbounded LIKE scan — degrades via the error channel, no rows
        rows, err = recall_episode_rows(self._db(tmp_path), keyword="x" * 300, limit=10)
        assert rows == []
        assert err is not None and "too long" in err


# --- live operator context (focus) -----------------------------------------

class TestFocus:
    """The focus primitive: the freshness helper (pure), the fail-soft +
    superset-tolerant ``_read_focus``, and the integration through
    ``build_substrate_view`` / ``SubstrateSource``."""

    import datetime as _dt

    UTC = _dt.timezone.utc

    def _now(self) -> "TestFocus._dt.datetime":
        return self._dt.datetime(2026, 6, 29, 17, 0, 0, tzinfo=self.UTC)

    # -- _humanize_focus_age (pure) --
    def test_age_just_now(self) -> None:
        from levain.dashboard import _humanize_focus_age
        assert _humanize_focus_age(10) == "set just now"

    def test_age_minutes(self) -> None:
        from levain.dashboard import _humanize_focus_age
        assert _humanize_focus_age(12 * 60) == "set 12m ago"

    def test_age_hours(self) -> None:
        from levain.dashboard import _humanize_focus_age
        assert _humanize_focus_age(3 * 3600) == "set 3h ago"

    def test_age_days(self) -> None:
        from levain.dashboard import _humanize_focus_age
        assert _humanize_focus_age(2 * 86400) == "set 2d ago"

    def test_age_negative_is_blank(self) -> None:
        # a future set_at (clock skew / bad data) is unparseable-as-age → "" not a lie
        from levain.dashboard import _humanize_focus_age
        assert _humanize_focus_age(-500) == ""

    def test_age_subsecond_future_is_blank(self) -> None:
        # int(-0.5)==0 would slip an int-only guard → a sub-second future stamp must
        # still be "" (the float guard); honesty-floor contract (complement L3 FIND-1).
        from levain.dashboard import _humanize_focus_age
        assert _humanize_focus_age(-0.5) == ""

    # -- _read_focus (fail-soft + contract) --
    def _write(self, tmp_path: Path, payload: object) -> Path:
        import json
        p = tmp_path / "context.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        return p

    def test_no_source_returns_none(self) -> None:
        from levain.dashboard import _read_focus
        assert _read_focus(None, self._now()) is None

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        from levain.dashboard import _read_focus
        assert _read_focus(tmp_path / "nope.json", self._now()) is None

    def test_malformed_json_is_dark_not_crash(self, tmp_path: Path) -> None:
        from levain.dashboard import _read_focus
        p = tmp_path / "context.json"
        p.write_text("{not json", encoding="utf-8")
        f = _read_focus(p, self._now())
        assert f is not None and f.text is None

    def test_non_dict_json_is_dark(self, tmp_path: Path) -> None:
        from levain.dashboard import _read_focus
        f = _read_focus(self._write(tmp_path, [1, 2, 3]), self._now())
        assert f is not None and f.text is None

    def test_file_present_no_focus_renders_unset(self, tmp_path: Path) -> None:
        from levain.dashboard import _read_focus
        f = _read_focus(self._write(tmp_path, {"location": "home"}), self._now())
        assert f is not None and f.text is None and f.stale is False

    def test_empty_focus_is_none(self, tmp_path: Path) -> None:
        from levain.dashboard import _read_focus
        f = _read_focus(self._write(tmp_path, {"focus": "   "}), self._now())
        assert f is not None and f.text is None

    def test_fresh_focus(self, tmp_path: Path) -> None:
        from levain.dashboard import _read_focus
        payload = {
            "focus": "Pressable work + flow work",
            "focus_set_at": "2026-06-29T14:00:00+00:00",  # 3h before now
            "focus_source": "flowconnect",
        }
        f = _read_focus(self._write(tmp_path, payload), self._now())
        assert f is not None
        assert f.text == "Pressable work + flow work"
        assert f.source == "flowconnect"
        assert f.age_label == "set 3h ago"
        assert f.stale is False

    def test_stale_focus_flagged(self, tmp_path: Path) -> None:
        from levain.dashboard import _read_focus
        payload = {
            "focus": "yesterday's thing",
            "focus_set_at": "2026-06-27T14:00:00+00:00",  # ~51h before now
        }
        f = _read_focus(self._write(tmp_path, payload), self._now())
        assert f is not None and f.text == "yesterday's thing"
        assert f.age_label == "set 2d ago"
        assert f.stale is True

    def test_focus_without_set_at_no_age_no_stale(self, tmp_path: Path) -> None:
        from levain.dashboard import _read_focus
        f = _read_focus(self._write(tmp_path, {"focus": "headless task"}), self._now())
        assert f is not None and f.text == "headless task"
        assert f.age_label == "" and f.stale is False

    def test_unparseable_set_at_no_age_no_stale(self, tmp_path: Path) -> None:
        from levain.dashboard import _read_focus
        payload = {"focus": "x", "focus_set_at": "not-a-timestamp"}
        f = _read_focus(self._write(tmp_path, payload), self._now())
        assert f is not None and f.age_label == "" and f.stale is False

    def test_naive_set_at_treated_utc(self, tmp_path: Path) -> None:
        from levain.dashboard import _read_focus
        payload = {"focus": "x", "focus_set_at": "2026-06-29T14:00:00"}  # naive → UTC
        f = _read_focus(self._write(tmp_path, payload), self._now())
        assert f is not None and f.age_label == "set 3h ago" and f.stale is False

    def test_superset_file_only_focus_keys_read(self, tmp_path: Path) -> None:
        # flow's context_state.json is a SUPERSET — body/mind/sensors are IGNORED; the
        # primitive stays general (only the three focus keys are contract).
        from levain.dashboard import _read_focus
        payload = {
            "focus": "the real thing",
            "focus_set_at": "2026-06-29T16:00:00+00:00",
            "focus_source": "flowconnect",
            "body": 5, "mind": 5, "social": 3,
            "ios_focus_mode": "none", "battery_level": 0.45, "pressure_hpa": 989.9,
        }
        f = _read_focus(self._write(tmp_path, payload), self._now())
        assert f is not None and f.text == "the real thing"
        assert f.age_label == "set 1h ago" and f.freshness == "fresh"
        d = f.to_dict()
        assert set(d) == {"text", "set_at", "source", "age_label", "stale", "freshness"}
        assert "body" not in d and "ios_focus_mode" not in d

    def test_freshness_unknown_for_missing_stamp(self, tmp_path: Path) -> None:
        # text present, no stamp → freshness "unknown" (not "fresh") so no surface
        # renders it as current. honesty floor (codex L3 MED1).
        from levain.dashboard import _read_focus
        f = _read_focus(self._write(tmp_path, {"focus": "no stamp"}), self._now())
        assert f is not None and f.text == "no stamp" and f.freshness == "unknown"

    def test_freshness_unknown_for_future_stamp(self, tmp_path: Path) -> None:
        from levain.dashboard import _read_focus
        payload = {"focus": "tomorrow's", "focus_set_at": "2026-06-30T17:00:00+00:00"}
        f = _read_focus(self._write(tmp_path, payload), self._now())
        assert f is not None and f.freshness == "unknown" and f.age_label == ""

    def test_freshness_unknown_for_unparseable_stamp(self, tmp_path: Path) -> None:
        from levain.dashboard import _read_focus
        payload = {"focus": "x", "focus_set_at": "not-a-timestamp"}
        f = _read_focus(self._write(tmp_path, payload), self._now())
        assert f is not None and f.freshness == "unknown"

    def test_naive_now_does_not_raise(self, tmp_path: Path) -> None:
        # _read_focus documents "never raises"; a naive `now` + aware stamp must not
        # crash the subtraction (codex L3 MED2).
        import datetime as dt
        from levain.dashboard import _read_focus
        payload = {"focus": "x", "focus_set_at": "2026-06-29T16:00:00+00:00"}
        naive_now = dt.datetime(2026, 6, 29, 17, 0, 0)  # no tzinfo
        f = _read_focus(self._write(tmp_path, payload), naive_now)
        assert f is not None and f.freshness == "fresh"

    # -- integration through build_substrate_view / to_dict --
    def test_build_carries_focus(self, tmp_path: Path) -> None:
        from levain.dashboard import build_substrate_view, AnnealPaths
        ctx = self._write(tmp_path, {
            "focus": "wiring the primitive",
            "focus_set_at": "2026-06-29T16:30:00+00:00",
            "focus_source": "cli",
        })
        paths = AnnealPaths.from_db(tmp_path / "memory.db")  # no store → store error, focus still reads
        view = build_substrate_view(paths, context_json=ctx, now=self._now())
        assert view.focus is not None and view.focus.text == "wiring the primitive"
        assert view.to_dict()["focus"]["text"] == "wiring the primitive"
        assert view.to_dict()["focus"]["age_label"] == "set 30m ago"

    def test_build_no_context_json_focus_none(self, tmp_path: Path) -> None:
        from levain.dashboard import build_substrate_view, AnnealPaths
        paths = AnnealPaths.from_db(tmp_path / "memory.db")
        view = build_substrate_view(paths, now=self._now())
        assert view.focus is None
        assert view.to_dict()["focus"] is None

    def test_source_local_defaults_context_path(self, tmp_path: Path) -> None:
        from levain.dashboard import SubstrateSource
        src = SubstrateSource.local(tmp_path)
        assert src.context_json == tmp_path / ".levain" / "context.json"

    def test_source_build_passes_context_json(self, tmp_path: Path) -> None:
        from levain.dashboard import SubstrateSource, AnnealPaths
        ctx = self._write(tmp_path, {"focus": "via source"})
        src = SubstrateSource(anneal=AnnealPaths.from_db(tmp_path / "memory.db"),
                              context_json=ctx)
        view = src.build(now=self._now())
        assert view.focus is not None and view.focus.text == "via source"


class TestWriteFocus:
    """write_focus — the write-peer; round-trips with _read_focus, merge-preserves
    sibling keys, atomically writes, and clears on blank."""

    import datetime as _dt
    UTC = _dt.timezone.utc

    def test_set_then_read_roundtrip(self, tmp_path: Path) -> None:
        from levain.dashboard import write_focus, _read_focus
        ctx = tmp_path / ".levain" / "context.json"  # parent created by write_focus
        write_focus(ctx, "  shipping  the   primitive ")
        f = _read_focus(ctx, self._dt.datetime.now(self.UTC))
        assert f is not None
        assert f.text == "shipping the primitive"  # whitespace collapsed
        assert f.source == "cli"
        assert f.age_label == "set just now" and f.stale is False

    def test_merge_preserves_sibling_keys(self, tmp_path: Path) -> None:
        import json
        from levain.dashboard import write_focus
        ctx = tmp_path / "context.json"
        ctx.write_text(json.dumps({"body": 5, "location": "home"}), encoding="utf-8")
        write_focus(ctx, "new focus", source="flowconnect")
        data = json.loads(ctx.read_text(encoding="utf-8"))
        assert data["body"] == 5 and data["location"] == "home"  # untouched
        assert data["focus"] == "new focus" and data["focus_source"] == "flowconnect"
        assert "focus_set_at" in data

    def test_blank_clears(self, tmp_path: Path) -> None:
        from levain.dashboard import write_focus, _read_focus
        ctx = tmp_path / "context.json"
        write_focus(ctx, "something")
        write_focus(ctx, "   ")  # clear
        f = _read_focus(ctx, self._dt.datetime.now(self.UTC))
        assert f is not None and f.text is None

    def test_corrupt_existing_replaced_not_failed(self, tmp_path: Path) -> None:
        import json
        from levain.dashboard import write_focus
        ctx = tmp_path / "context.json"
        ctx.write_text("{not json", encoding="utf-8")
        write_focus(ctx, "recovered")  # must not raise
        assert json.loads(ctx.read_text(encoding="utf-8"))["focus"] == "recovered"

    def test_set_at_is_tz_aware(self, tmp_path: Path) -> None:
        import json
        from datetime import datetime
        from levain.dashboard import write_focus
        ctx = tmp_path / "context.json"
        write_focus(ctx, "x")
        stamp = json.loads(ctx.read_text(encoding="utf-8"))["focus_set_at"]
        assert datetime.fromisoformat(stamp).tzinfo is not None


class TestRunFocus:
    """run_focus — the `levain focus` entry point: set / show / clear over an
    install's .levain/context.json."""

    def test_set_and_show(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from levain.dashboard import run_focus
        (tmp_path / ".levain").mkdir()
        rc = run_focus(path=tmp_path, text="auditing the seam")
        assert rc == 0 and "focus set: auditing the seam" in capsys.readouterr().out
        rc = run_focus(path=tmp_path)  # show
        assert rc == 0 and "⊙ auditing the seam" in capsys.readouterr().out

    def test_show_when_unset(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from levain.dashboard import run_focus
        rc = run_focus(path=tmp_path)
        assert rc == 0 and "no focus set" in capsys.readouterr().out

    def test_clear(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from levain.dashboard import run_focus
        run_focus(path=tmp_path, text="temp")
        rc = run_focus(path=tmp_path, clear=True)
        assert rc == 0 and "focus cleared" in capsys.readouterr().out
        run_focus(path=tmp_path)
        assert "no focus set" in capsys.readouterr().out


class TestFocusReviewFixes:
    """Apparatus-driven hardening: collapse internal whitespace on READ (L1-#3), and
    focus in the model-visible render_summary (L1-#1)."""

    import datetime as _dt
    UTC = _dt.timezone.utc

    def test_collapses_internal_newlines_on_read(self, tmp_path: Path) -> None:
        # a foreign/hand-edited focus with a newline must not split the single-line
        # render_text / TUI rule row → collapse on read (not just on write).
        import json
        from levain.dashboard import _read_focus
        p = tmp_path / "context.json"
        p.write_text(json.dumps({"focus": "line one\nline two\t  tabbed"}), encoding="utf-8")
        f = _read_focus(p, self._dt.datetime.now(self.UTC))
        assert f is not None and f.text == "line one line two tabbed"

    def test_render_summary_includes_focus(self, tmp_path: Path) -> None:
        from levain.dashboard import build_substrate_view, render_summary, AnnealPaths
        import json
        ctx = tmp_path / "context.json"
        ctx.write_text(json.dumps({
            "focus": "the model-visible frame",
            "focus_set_at": "2026-06-29T16:00:00+00:00",
        }), encoding="utf-8")
        view = build_substrate_view(
            AnnealPaths.from_db(tmp_path / "memory.db"), context_json=ctx,
            now=self._dt.datetime(2026, 6, 29, 17, 0, 0, tzinfo=self.UTC),
        )
        out = render_summary(view)
        assert "Focus: the model-visible frame" in out
        assert "set 1h ago" in out

    def test_render_summary_no_focus_omits_line(self, tmp_path: Path) -> None:
        from levain.dashboard import build_substrate_view, render_summary, AnnealPaths
        view = build_substrate_view(AnnealPaths.from_db(tmp_path / "memory.db"))
        assert "Focus:" not in render_summary(view)

    def test_run_focus_show_surfaces_source(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from levain.dashboard import run_focus
        (tmp_path / ".levain").mkdir()
        run_focus(path=tmp_path, text="x", source="flowconnect")
        capsys.readouterr()
        run_focus(path=tmp_path)
        assert "via flowconnect" in capsys.readouterr().out

    def test_run_focus_warns_when_no_store(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # no .levain/memory.db → a soft stderr note (still honors the set)
        from levain.dashboard import run_focus
        rc = run_focus(path=tmp_path, text="early focus")
        assert rc == 0
        err = capsys.readouterr().err
        assert "no Levain store" in err


# --- pack white-labeling: config brand → view → render surfaces --------------

class TestBrandWhiteLabel:
    @staticmethod
    def _make_branded_install(tmp_path: Path, brand: dict) -> Path:
        import json

        from anneal_memory import Store

        lev = tmp_path / ".levain"
        lev.mkdir()
        with Store(lev / "memory.db") as store:
            store.record("decided X because Y", "decision")
        (lev / "memory.continuity.md").write_text(
            "## State\nfocus\n\n## Active Threads\n- t1\n", encoding="utf-8"
        )
        (lev / "config.json").write_text(json.dumps(brand) + "\n", encoding="utf-8")
        return tmp_path

    def test_config_brand_populates_view(self, tmp_path: Path) -> None:
        from levain.dashboard import SubstrateSource

        root = self._make_branded_install(
            tmp_path,
            {"surface_name": "Pressable Solutions Harness", "subtitle": "team memory"},
        )
        view = SubstrateSource.local(root).build()
        assert view.brand_wordmark == "Pressable Solutions Harness"
        assert view.brand_model == "team memory"
        # what /substrate.json ships to the web JS (wordmark + document.title override)
        assert view.to_dict()["brand_wordmark"] == "Pressable Solutions Harness"

    def test_text_masthead_shows_brand(self, tmp_path: Path, capsys) -> None:
        from levain.dashboard import run_dashboard

        root = self._make_branded_install(
            tmp_path, {"entity_name": "Athena", "surface_name": "Pressable Solutions Harness"}
        )
        rc = run_dashboard(root)
        out = capsys.readouterr().out
        assert rc == 0
        assert out.splitlines()[0] == "Pressable Solutions Harness — Athena"
        assert "Levain substrate" not in out.splitlines()[0]

    def test_masthead_is_the_wordmark_not_the_subtitle(self, tmp_path: Path) -> None:
        # The masthead is the NAME (brand_wordmark), never the subtitle (brand_model):
        # a subtitle-only brand shows the Levain default here, consistent with the
        # TUI/web wordmark (all three key the name off brand_wordmark). [L1 #3]
        from levain.dashboard import AnnealPaths, SubstrateView, render_summary, render_text

        paths = AnnealPaths.from_db(tmp_path / "x.db")
        v = SubstrateView(paths=paths, brand_wordmark="Wordmark Co", brand_model="a subtitle")
        assert render_text(v).splitlines()[0].startswith("Wordmark Co —")
        assert render_summary(v).splitlines()[0].startswith("Wordmark Co —")
        # subtitle-only → the subtitle is NOT promoted to the masthead name.
        v2 = SubstrateView(paths=paths, brand_model="a subtitle")
        assert render_text(v2).splitlines()[0].startswith("Levain substrate —")

    def test_source_brand_override_wins_over_config(self, tmp_path: Path) -> None:
        # The bridge flow-brands its cockpit programmatically — that override must
        # WIN over an install's config brand (build() copies it on top).
        import dataclasses

        from levain.dashboard import SubstrateSource

        root = self._make_branded_install(tmp_path, {"surface_name": "Install Brand"})
        src = dataclasses.replace(SubstrateSource.local(root), brand_wordmark="Bridge Cockpit")
        assert src.build().brand_wordmark == "Bridge Cockpit"

    def test_no_config_brand_leaves_view_default(self, tmp_path: Path) -> None:
        from anneal_memory import Store

        from levain.dashboard import SubstrateSource

        lev = tmp_path / ".levain"
        lev.mkdir()
        with Store(lev / "memory.db") as store:
            store.record("x", "decision")
        view = SubstrateSource.local(tmp_path).build()
        assert view.brand_wordmark is None and view.brand_model is None
