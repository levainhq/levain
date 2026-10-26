"""Tests for levain.dashboard — the Slice-1 read-only substrate assembly.

Pure helpers are unit-tested directly; ``build_substrate_view`` is exercised
against real (temp) anneal stores so the in-process call contract is verified,
not mocked. The populated crystal tier is covered end-to-end by the live-store
smoke (documented in next.md) + the corrupt-file fail-soft test here.
"""

from __future__ import annotations

from pathlib import Path

import time
import os
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

    def test_the_default_set_shows_durable_facts(self) -> None:
        """anneal 0.9.28's optional section, in its schema order, when the continuity has it."""
        from levain.dashboard import _ALL_SECTIONS
        md = "## State\ns\n\n## Durable Facts\n- tree nut allergy — cues: dinner\n\n## Patterns\np\n"
        secs = _parse_sections(md, _ALL_SECTIONS)
        assert [s.heading for s in secs] == ["State", "Durable Facts", "Patterns"]
        assert "tree nut" in secs[1].body

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
        assert v.health.write_path_live is False  # nothing committed yet: not live, and not a fault
        assert v.health.status == "no-wraps"
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

    def test_render_text_handles_a_store_with_no_wraps(self, tmp_path: Path) -> None:
        from anneal_memory import Store

        from levain.dashboard import AnnealPaths, build_substrate_view, render_text

        db = tmp_path / "memory.db"
        with Store(db):
            pass
        v = build_substrate_view(AnnealPaths.from_db(db))
        text = render_text(v)
        assert "no wraps yet" in text  # a fresh store is not a failure
        assert "DARK" not in text and "links" not in text

    def test_render_text_with_content(self, tmp_path: Path) -> None:
        from anneal_memory import Store
        from anneal_memory.spores import SporeStore

        from levain.dashboard import AnnealPaths, build_substrate_view, render_text

        db = tmp_path / "memory.db"
        with Store(db) as store:
            a = store.record("episode A", "decision")
            b = store.record("episode B", "observation")
            store.wrap_started(token="w1", episode_ids=[a.id, b.id])
            store.wrap_completed(2, 20, wrap_token="w1", episode_ids=[a.id, b.id])
        SporeStore(tmp_path / "memory.spores.json").add(
            type="task", text="loop", tier="hot"
        )
        (tmp_path / "memory.continuity.md").write_text("## State\nfoc\n", encoding="utf-8")
        text = render_text(build_substrate_view(AnnealPaths.from_db(db)))
        assert "LIVE" in text and "last wrap" in text  # a committed wrap reads live
        assert "Hebbian" not in text and "0 links" not in text
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


class TestFormationCaption:
    """anneal >= 1.0.2 forms no new links; the graph says so, and an old anneal says nothing."""

    @staticmethod
    def _stub(formation):
        from types import SimpleNamespace

        ep = SimpleNamespace(id="e1", type=SimpleNamespace(value="observation"), content="x")
        kw = {} if formation is None else {"association_formation": lambda: formation}
        return SimpleNamespace(
            recall=lambda limit: SimpleNamespace(episodes=[ep], total_matching=1),
            get_associations=lambda chunk, min_strength, limit: [],
            **kw,
        )

    def test_a_disabled_store_carries_the_wrap_it_stopped_at(self) -> None:
        from levain.dashboard import _build_graph

        g = _build_graph(
            self._stub({"mode": "disabled", "off_at_wrap": 7, "off_by_version": "1.0.2"}),
            min_strength=0.0, max_nodes=300, max_edges=2000,
        )
        assert g.formation_off_at_wrap == 7
        assert g.to_dict()["formation_off_at_wrap"] == 7

    def test_an_enabled_store_or_an_old_anneal_claims_nothing(self) -> None:
        from levain.dashboard import _build_graph

        for formation in (None, {"mode": "enabled", "off_at_wrap": None, "off_by_version": None}, "odd",
                          {"mode": "disabled", "off_at_wrap": "7"}):
            g = _build_graph(self._stub(formation), min_strength=0.0, max_nodes=300, max_edges=2000)
            assert g.formation_off_at_wrap is None, formation

    def test_the_caption_reads_not_formed_for_an_empty_graph_and_never_errors(self) -> None:
        from levain.dashboard import AssociationGraph, GraphEdge, GraphNode, graph_caption

        assert graph_caption(AssociationGraph([], [])) == "not formed"
        assert graph_caption(AssociationGraph([], [], formation_off_at_wrap=3)) == "not formed · no new links since wrap 3"
        full = AssociationGraph([GraphNode("a", "episode", "A"), GraphNode("b", "episode", "B")],
                                [GraphEdge("a", "b", 0.9, 3, None, 0.0)], truncated=True, formation_off_at_wrap=3)
        assert graph_caption(full) == "2 nodes, 1 edges (capped for display) · no new links since wrap 3"

    def test_render_text_prints_the_caption(self) -> None:
        from levain.dashboard import AnnealPaths, AssociationGraph, SubstrateView, render_text

        v = SubstrateView(paths=AnnealPaths.from_db(Path("/x/memory.db")), scope="solo")
        v.graph = AssociationGraph([], [], formation_off_at_wrap=9)
        assert "Association graph: not formed · no new links since wrap 9" in render_text(v)

    def test_a_wrap_row_prints_links_only_when_the_row_has_them(self) -> None:
        from levain.dashboard import AnnealPaths, SubstrateView, WrapRow, render_text

        v = SubstrateView(paths=AnnealPaths.from_db(Path("/x/memory.db")), scope="solo")
        v.wraps = [WrapRow("2026-10-23T01:00:00", 5, 900, 1, 0, 0, 0, 0), WrapRow("2026-10-01T01:00:00", 5, 900, 1, 0, 4, 1, 0)]
        lines = [x for x in render_text(v).splitlines() if x.startswith("  - 2026-")]
        assert "links" not in lines[0] and "+4 links" in lines[1]


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


# --- live operator context (the state line) --------------------------------

class TestStateLine:
    """The state-line primitive's plumbing: the age helper (pure) and the integration
    through ``build_substrate_view`` / ``SubstrateSource``. (Read/expiry/write contract:
    tests/test_operator_state.py.)"""

    import datetime as _dt

    UTC = _dt.timezone.utc

    def _now(self) -> "TestStateLine._dt.datetime":
        return self._dt.datetime(2026, 6, 29, 17, 0, 0, tzinfo=self.UTC)

    def _write(self, tmp_path: Path, payload: object) -> Path:
        import json
        p = tmp_path / "context.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        return p

    def test_age_labels(self) -> None:
        from levain.dashboard import _humanize_age
        assert _humanize_age(10) == "set just now"
        assert _humanize_age(12 * 60) == "set 12m ago"
        assert _humanize_age(3 * 3600) == "set 3h ago"
        assert _humanize_age(2 * 86400) == "set 2d ago"

    def test_age_negative_is_blank(self) -> None:
        # a future stamp (clock skew / bad data) is unparseable-as-age, never a fabricated freshness
        from levain.dashboard import _humanize_age
        assert _humanize_age(-500) == ""
        assert _humanize_age(-0.5) == ""  # int(-0.5)==0 must not slip through

    def test_state_expiry_is_twelve_hours(self) -> None:
        from levain.dashboard import STATE_EXPIRES_AFTER_HOURS
        assert STATE_EXPIRES_AFTER_HOURS == 12

    def test_build_carries_state(self, tmp_path: Path) -> None:
        from levain.dashboard import build_substrate_view, AnnealPaths
        ctx = self._write(tmp_path, {
            "state": "wiring the primitive",
            "state_set_at": "2026-06-29T16:30:00+00:00",
            "state_source": "cli",
        })
        paths = AnnealPaths.from_db(tmp_path / "memory.db")  # no store -> store error, state still reads
        view = build_substrate_view(paths, context_json=ctx, now=self._now())
        assert view.state is not None and view.state.text == "wiring the primitive"
        assert view.to_dict()["state"]["text"] == "wiring the primitive"
        assert view.to_dict()["state"]["age_label"] == "set 30m ago"
        assert "focus" not in view.to_dict()

    def test_build_no_context_json_state_none(self, tmp_path: Path) -> None:
        from levain.dashboard import build_substrate_view, AnnealPaths
        paths = AnnealPaths.from_db(tmp_path / "memory.db")
        view = build_substrate_view(paths, now=self._now())
        assert view.state is None
        assert view.to_dict()["state"] is None

    def test_source_local_defaults_context_path(self, tmp_path: Path) -> None:
        from levain.dashboard import SubstrateSource
        src = SubstrateSource.local(tmp_path)
        assert src.context_json == tmp_path / ".levain" / "context.json"

    def test_source_build_passes_context_json(self, tmp_path: Path) -> None:
        from levain.dashboard import SubstrateSource, AnnealPaths
        ctx = self._write(tmp_path, {"state": "via source", "state_set_at": "2026-06-29T16:00:00+00:00"})
        src = SubstrateSource(anneal=AnnealPaths.from_db(tmp_path / "memory.db"),
                              context_json=ctx)
        view = src.build(now=self._now())
        assert view.state is not None and view.state.text == "via source"

    def test_legacy_focus_keys_are_ignored_by_the_reader(self, tmp_path: Path) -> None:
        # an old context.json may still hold focus keys; nothing reads them and no field carries them
        from levain.dashboard import build_substrate_view, AnnealPaths
        ctx = self._write(tmp_path, {"focus": "old", "focus_set_at": "2026-06-29T16:30:00+00:00",
                                     "focus_source": "cli"})
        view = build_substrate_view(AnnealPaths.from_db(tmp_path / "memory.db"),
                                    context_json=ctx, now=self._now())
        assert view.state is not None and view.state.text is None
        assert "focus" not in view.to_dict()
        assert not hasattr(view, "focus")

    def test_run_state_show_surfaces_source(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from levain.dashboard import run_state
        (tmp_path / ".levain").mkdir()
        run_state(path=tmp_path, text="x", source="flowconnect")
        capsys.readouterr()
        run_state(path=tmp_path)
        assert "via flowconnect" in capsys.readouterr().out

    def test_run_state_warns_when_no_store(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # no .levain/memory.db -> a soft stderr note (still honors the set)
        from levain.dashboard import run_state
        rc = run_state(path=tmp_path, text="early line")
        assert rc == 0
        assert "no Levain store" in capsys.readouterr().err


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
            {"surface_name": "Acme Solutions Harness", "subtitle": "team memory"},
        )
        view = SubstrateSource.local(root).build()
        assert view.brand_wordmark == "Acme Solutions Harness"
        assert view.brand_model == "team memory"
        # what /substrate.json ships to the web JS (wordmark + document.title override)
        assert view.to_dict()["brand_wordmark"] == "Acme Solutions Harness"

    def test_text_masthead_shows_brand(self, tmp_path: Path, capsys) -> None:
        from levain.dashboard import run_dashboard

        root = self._make_branded_install(
            tmp_path, {"entity_name": "Athena", "surface_name": "Acme Solutions Harness"}
        )
        rc = run_dashboard(root)
        out = capsys.readouterr().out
        assert rc == 0
        assert out.splitlines()[0] == "Acme Solutions Harness — Athena"
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


class TestHealthStatusFollowsTheOldKey:
    """A caller that built Health the old way (write_path_live, no status) gets the status it implies."""

    @staticmethod
    def _health(**kw):
        from levain.dashboard import Health

        base = dict(total_links=0, avg_strength=0.0, max_strength=0.0, density=0.0, local_density=0.0, links_formed_total=0,
                    links_strengthened_total=0, links_decayed_total=0, graduations_validated_total=0, graduations_demoted_total=0,
                    total_episodes=0, episodes_since_wrap=0, episodes_by_type={}, tombstones=0, continuity_chars=None,
                    total_wraps=0, last_wrap_at=None, wrap_in_progress=False)
        return Health(**base, **kw)

    def test_the_old_shape_never_contradicts_itself(self) -> None:
        for live, want in ((True, "live"), (False, "no-wraps")):
            d = self._health(write_path_live=live).to_dict()
            assert d["status"] == want and d["write_path_live"] is live, d

    def test_the_new_shape_keeps_the_deprecated_key_in_step(self) -> None:
        for status, live in (("live", True), ("no-wraps", False), ("unreadable", False)):
            d = self._health(write_path_live=not live, status=status).to_dict()
            assert d["status"] == status and d["write_path_live"] is live, d

    def test_a_failing_formation_accessor_costs_the_caption_never_the_graph(self) -> None:
        from types import SimpleNamespace

        from levain.dashboard import _build_graph

        def boom():
            raise RuntimeError("meta table unreadable")

        ep = SimpleNamespace(id="e1", type=SimpleNamespace(value="observation"), content="x")
        stub = SimpleNamespace(
            recall=lambda limit: SimpleNamespace(episodes=[ep], total_matching=1),
            get_associations=lambda chunk, min_strength, limit: [],
            association_formation=boom,
        )
        g = _build_graph(stub, min_strength=0.0, max_nodes=300, max_edges=2000)
        assert g is not None and g.formation_off_at_wrap is None


class TestHealthOracles:
    """The substrate oracles are facts about the last committed save, never an activity counter."""

    def test_recency_convergence_audit_from_the_store_facts(self, tmp_path: Path) -> None:
        import hashlib
        from datetime import datetime, timezone
        from types import SimpleNamespace

        from levain.dashboard import health_oracles

        md = tmp_path / "memory.continuity.md"
        md.write_text("## State\nfoc\n", encoding="utf-8")
        h = hashlib.sha256(md.read_bytes()).hexdigest()
        now = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
        st = SimpleNamespace(last_wrap_at="2026-10-24T09:00:00", total_wraps=5, audit_write_failures=0)
        row = ("2026-10-24T10:00:00", h, "tok-1")  # age and hash come from the ONE row, not from status
        ok = health_oracles(st, row, md, now=now)
        assert ok == {"save_age_hours": 2.0, "save_at": "2026-10-24T10:00:00", "projection_converged": True,
                      "projection_note": None, "audit_backlog": 0}   # save_at: the stamp the age is counted from (a page ticks it)
        assert health_oracles(st, ("2026-10-24T10:00:00", "b" * 64, "tok-1"), md, now=now)["projection_converged"] is False
        # a naive now compares in UTC instead of raising
        assert health_oracles(st, row, md, now=now.replace(tzinfo=None))["save_age_hours"] == 2.0
        # nothing to compare is None, never a guessed ok; a fresh store has no save age
        fresh = SimpleNamespace(last_wrap_at=None, total_wraps=0, audit_write_failures=None)
        assert health_oracles(fresh, None, md, now=now) == {
            "save_age_hours": None, "save_at": None, "projection_converged": None, "projection_note": None, "audit_backlog": None}
        assert health_oracles(st, ("2026-10-24T10:00:00", None, None), md, now=now)["projection_converged"] is None
        st.audit_write_failures = 3
        assert health_oracles(st, row, md, now=now)["audit_backlog"] == 3

    def test_convergence_is_the_content_hash_not_the_size(self, tmp_path: Path, monkeypatch) -> None:
        import hashlib
        from anneal_memory import Store

        from levain.dashboard import AnnealPaths, build_substrate_view

        db = tmp_path / "memory.db"
        with Store(db) as store:
            a = store.record("episode A", "decision")
            store.wrap_started(token="w1", episode_ids=[a.id])
            store.wrap_completed(1, 20, wrap_token="w1", episode_ids=[a.id])
        md = tmp_path / "memory.continuity.md"
        md.write_text("## State\nfoc\n", encoding="utf-8")

        def converged() -> bool | None:
            v = build_substrate_view(AnnealPaths.from_db(db))
            assert v.health is not None
            return v.health.projection_converged

        assert converged() is None  # the wrap row has no hash (an old row): unknown, never converged
        with Store(db) as store:  # a second wrap, committed WITH the hash of the text it saved
            b = store.record("episode B", "decision")
            store.wrap_started(token="w2", episode_ids=[b.id])
            store.wrap_completed(1, 20, wrap_token="w2", episode_ids=[b.id], pair_id="p2",
                                 content_hash=hashlib.sha256(md.read_bytes()).hexdigest())
        assert converged() is True
        md.write_text("## State\nfoO\n", encoding="utf-8")  # same length, different text
        assert converged() is False  # no sidecar proves a rename pending: an edit, not a settle
        md.unlink()
        assert converged() is False  # a committed wrap and no file: diverged, "continuity file missing"

    def test_a_live_view_carries_the_oracle_keys_and_never_a_link_count_as_health(self, tmp_path: Path) -> None:
        from anneal_memory import Store

        db = tmp_path / "memory.db"
        with Store(db):
            pass
        v = build_substrate_view(AnnealPaths.from_db(db))
        assert v.health is not None
        d = v.health.to_dict()
        assert {"save_age_hours", "projection_converged", "audit_backlog"} <= set(d) and "audit_prune_behind" not in d
        assert d["save_age_hours"] is None and d["projection_converged"] is None  # no wrap committed yet


class TestHealthOraclesRound2:
    def test_a_read_only_store_reports_the_durable_audit_failures_as_a_fault(self, tmp_path: Path) -> None:
        from anneal_memory import Store

        from levain.dashboard import health_oracle_line

        db = tmp_path / "memory.db"
        with Store(db, audit=True) as store:
            store._audit_failures_unpersisted = 7  # close() flushes it to the durable metadata row
        v = build_substrate_view(AnnealPaths.from_db(db))  # read-only: audit_enabled is False here
        assert v.health is not None and v.health.audit_backlog == 7
        assert "7 audit writes failed" in (health_oracle_line(v.health) or "")

    @staticmethod
    def _wrapped(tmp_path: Path, text: str = "## State\nfoc\n", pair: str = "0123456789ab-aaaa1111"):
        """A real store with one committed wrap whose content_hash is sha256(text); the continuity file holds text."""
        import hashlib
        from anneal_memory import Store

        db, md = tmp_path / "memory.db", tmp_path / "memory.continuity.md"
        md.write_text(text, encoding="utf-8")
        with Store(db) as store:
            e = store.record("episode A", "decision")
            store.wrap_started(token="w1", episode_ids=[e.id])
            store.wrap_completed(1, 20, wrap_token="w1", episode_ids=[e.id], pair_id=pair,
                                 content_hash=hashlib.sha256(text.encode()).hexdigest())
        return db, md

    def test_another_pairs_stale_sidecar_does_not_excuse_an_edited_file(self, tmp_path: Path) -> None:
        db, md = self._wrapped(tmp_path)
        (tmp_path / "memory.continuity.zzzzzzzzzzzz-deadbeef.md.tmp").write_text("old wrap", encoding="utf-8")
        md.write_text("## State\nedited\n", encoding="utf-8")
        h = build_substrate_view(AnnealPaths.from_db(db)).health
        assert h is not None and h.projection_converged is False
        assert "differs" in (h.projection_note or "")

    def test_the_latest_pairs_sidecar_with_the_matching_hash_is_settling(self, tmp_path: Path) -> None:
        db, md = self._wrapped(tmp_path)
        (tmp_path / "memory.continuity.0123456789ab-aaaa1111.md.tmp").write_text("## State\nfoc\n", encoding="utf-8")
        md.write_text("## State\nthe previous wrap\n", encoding="utf-8")  # the rename has not happened yet
        h = build_substrate_view(AnnealPaths.from_db(db)).health
        assert h is not None and h.projection_converged is None and "settling" in (h.projection_note or "")

    def test_an_old_matching_sidecar_reads_incomplete_and_the_note_is_shown(self, tmp_path: Path) -> None:
        from levain.dashboard import health_oracle_line

        db, md = self._wrapped(tmp_path)
        side = tmp_path / "memory.continuity.0123456789ab-aaaa1111.md.tmp"
        side.write_text("## State\nfoc\n", encoding="utf-8")
        old = time.time() - 3600
        os.utime(side, (old, old))
        md.write_text("## State\nthe previous wrap\n", encoding="utf-8")
        h = build_substrate_view(AnnealPaths.from_db(db)).health
        assert h is not None and h.projection_converged is False and "externalization incomplete" in (h.projection_note or "")
        assert "externalization incomplete" in (health_oracle_line(h) or "")

    def test_an_invalid_pair_id_is_no_settling_proof_never_a_fault(self, tmp_path: Path) -> None:
        from levain.dashboard import health_oracles

        md = tmp_path / "memory.continuity.md"
        md.write_text("something else\n", encoding="utf-8")
        for bad in ("../../etc/x", "a\x00b", "0123456789ab-aaaa1111"):
            o = health_oracles(None, ("2026-01-01T00:00:00Z", "0" * 64, bad), md)
            assert o["projection_converged"] is False and "differs" in o["projection_note"]

    def test_a_file_deleted_seconds_after_a_wrap_with_no_sidecar_is_missing(self, tmp_path: Path) -> None:
        db, md = self._wrapped(tmp_path)
        md.unlink()
        h = build_substrate_view(AnnealPaths.from_db(db)).health
        assert h is not None and h.save_age_hours is not None and h.save_age_hours < 0.1  # seconds old, no window
        assert h.projection_converged is False and h.projection_note == "continuity file missing"

    def test_the_age_rounds_half_up_like_the_web_renderers(self) -> None:
        from levain.dashboard import Health, health_oracle_line

        h = Health(write_path_live=True, total_links=0, avg_strength=0, max_strength=0, density=0, local_density=0,
                   links_formed_total=0, links_strengthened_total=0, links_decayed_total=0,
                   graduations_validated_total=0, graduations_demoted_total=0, total_episodes=0,
                   episodes_since_wrap=0, episodes_by_type={}, tombstones=0, continuity_chars=None, total_wraps=1,
                   last_wrap_at=None, wrap_in_progress=False)
        for age, want in ((2.5, "3h ago"), (3.5, "4h ago"), (0.99, "under 1h ago")):
            h.save_age_hours = age
            assert want in (health_oracle_line(h) or "")
