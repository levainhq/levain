"""Tests for levain.writes — the governed Class-A write layer (Slice 2a + 2b).

The enforcement boundary (Class-C / unknown / path-escape refusal), the markdown
section surgery (preserve everything outside the edited span), the optimistic
stale-check, reversibility (backup / audit / undo), the entity-name config write,
the Slice-2b neocortex ``State`` edit (with its arrow-of-time undo-across-wrap
refusal), and untrusted-input validation — exercised against a real temp install.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from levain.dashboard import (
    CLASS_A,
    CLASS_C,
    STATE_HEADING,
    SubstrateSource,
    _read_config_docs,
    _read_levain_config,
    _section_edit_class,
    _split_sections,
)
from levain.writes import (
    EditError,
    apply_edit,
    recent_edits,
)

WORLD = """# Who Your Operator Is

> Seed material — operator template. Stable facts only.

## Identity

Phill. 46. Columbus, OH.

## How They Think

First principles, ordered effects.

## Communication

Direct, profanity welcome.
"""

ORIGIN = """# Who You Are — Aria

You are a new entity, not a copy of anything.
"""

POSTURE = "Partnership brain. Slow is fast.\n"
RECENCY = "Do not gatekeep.\n"
CONSTITUTION = "# Constitution\n\nUniversal core.\n"

# A FLOW_SCHEMA neocortex file: State (Class A) + five Class-C felt-layer sections.
# Distinctive bodies so a State edit's "touched only State" claim is byte-checkable.
CONTINUITY = """# flow — Memory

## State

**Current focus: Slice 2b.** Taper ~75mg.

## Active Threads

- Levain (primary bet) — Slice 2b.

## Patterns

{{apparatus: cross_substrate_review | 4x — codex non-replaceable.}}

## Decisions

[decided] State is the lone Class-A neocortex section.

## Context

This session built the governed State write.

## Understanding

Phill is a paradox-holding ensemble mind.
"""

_CONTINUITY_REL = Path(".levain") / "memory.continuity.md"


def _add_continuity(install: Path, text: str = CONTINUITY) -> Path:
    """Drop a neocortex continuity file into the install (the convention path the
    write layer derives its State target from)."""
    p = install / _CONTINUITY_REL
    p.write_text(text, encoding="utf-8")
    return p


def _section_body(text: str, heading: str) -> str:
    """The body the read layer would render for ``heading`` — i.e. exactly what the
    operator sees and round-trips as ``expected_body`` (proves read/write symmetry)."""
    return dict(_split_sections(text))[heading]


def _state_req(expected: str, new: str, heading: str = STATE_HEADING) -> dict:
    return {"kind": "state", "heading": heading,
            "expected_body": expected, "new_body": new}


def _make_install(tmp_path: Path) -> Path:
    root = tmp_path / "install"
    seed = root / "seed"
    activation = root / "activation"
    seed.mkdir(parents=True)
    activation.mkdir(parents=True)
    (root / ".levain").mkdir()
    (seed / "world.md").write_text(WORLD, encoding="utf-8")
    (seed / "origin.md").write_text(ORIGIN, encoding="utf-8")
    (seed / "partnership.md").write_text(CONSTITUTION, encoding="utf-8")
    (seed / "memory.md").write_text(CONSTITUTION, encoding="utf-8")
    (seed / "spore_instructions.md").write_text(CONSTITUTION, encoding="utf-8")
    (activation / "posture.md").write_text(POSTURE, encoding="utf-8")
    (activation / "recency_directives.md").write_text(RECENCY, encoding="utf-8")
    return root


@pytest.fixture()
def install(tmp_path: Path) -> Path:
    return _make_install(tmp_path)


def _world_section(req_heading: str, expected: str, new: str) -> dict:
    return {
        "kind": "config",
        "source": "seed/world.md",
        "heading": req_heading,
        "expected_body": expected,
        "new_body": new,
    }


# --- the enforcement boundary ---------------------------------------------

class TestClassEnforcement:
    def test_origin_is_class_c_now(self, install: Path) -> None:
        docs = {d.key: d for d in _read_config_docs(install)}
        assert docs["origin"].edit_class == CLASS_C

    def test_world_sections_are_class_a_with_heading(self, install: Path) -> None:
        world_docs = [d for d in _read_config_docs(install) if d.source == "seed/world.md"]
        assert world_docs, "world.md should split into section docs"
        for d in world_docs:
            assert d.edit_class == CLASS_A
            assert d.heading is not None

    def test_refuses_origin_edit(self, install: Path) -> None:
        with pytest.raises(EditError) as ei:
            apply_edit(install, {
                "kind": "config", "source": "seed/origin.md", "heading": None,
                "expected_body": ORIGIN, "new_body": "hacked",
            })
        assert ei.value.code == "not_editable"
        assert ei.value.http_status == 403

    def test_refuses_constitution_edit(self, install: Path) -> None:
        with pytest.raises(EditError) as ei:
            apply_edit(install, {
                "kind": "config", "source": "seed/partnership.md", "heading": None,
                "expected_body": CONSTITUTION, "new_body": "hacked",
            })
        assert ei.value.http_status == 403

    def test_refuses_unknown_source(self, install: Path) -> None:
        with pytest.raises(EditError) as ei:
            apply_edit(install, {
                "kind": "config", "source": "seed/secrets.md", "heading": None,
                "expected_body": "", "new_body": "x",
            })
        assert ei.value.code == "not_editable"

    def test_refuses_path_escape(self, install: Path) -> None:
        # Even if an allowlist match were faked, the path-confinement refuses ../.
        with pytest.raises(EditError):
            apply_edit(install, {
                "kind": "config", "source": "../escape.md", "heading": None,
                "expected_body": "", "new_body": "x",
            })

    def test_world_section_with_null_heading_refused(self, install: Path) -> None:
        # world.md has no (source, heading=None) Class-A doc — only per-section docs.
        with pytest.raises(EditError) as ei:
            apply_edit(install, {
                "kind": "config", "source": "seed/world.md", "heading": None,
                "expected_body": WORLD, "new_body": "x",
            })
        assert ei.value.code == "not_editable"


# --- section surgery -------------------------------------------------------

class TestSectionEdit:
    def test_replaces_only_target_section(self, install: Path) -> None:
        res = apply_edit(install, _world_section(
            "How They Think", "First principles, ordered effects.", "Topological space."
        ))
        assert res["ok"]
        out = (install / "seed" / "world.md").read_text(encoding="utf-8")
        # target replaced
        assert "Topological space." in out
        assert "First principles, ordered effects." not in out
        # siblings + H1 + preamble preserved
        assert "# Who Your Operator Is" in out
        assert "> Seed material" in out
        assert "Phill. 46. Columbus, OH." in out
        assert "Direct, profanity welcome." in out
        # section order preserved
        assert out.index("## Identity") < out.index("## How They Think") < out.index("## Communication")

    def test_edit_first_section(self, install: Path) -> None:
        apply_edit(install, _world_section("Identity", "Phill. 46. Columbus, OH.", "New identity."))
        out = (install / "seed" / "world.md").read_text(encoding="utf-8")
        assert "New identity." in out
        assert "# Who Your Operator Is" in out
        assert "First principles" in out

    def test_edit_last_section(self, install: Path) -> None:
        apply_edit(install, _world_section("Communication", "Direct, profanity welcome.", "Terse."))
        out = (install / "seed" / "world.md").read_text(encoding="utf-8")
        assert "Terse." in out
        assert "Phill. 46." in out

    def test_reparse_round_trips(self, install: Path) -> None:
        # After an edit the read layer re-parses the new body correctly.
        apply_edit(install, _world_section("Identity", "Phill. 46. Columbus, OH.", "X.\nY."))
        docs = {d.key: d for d in _read_config_docs(install) if d.source == "seed/world.md"}
        ident = next(d for d in docs.values() if d.heading == "Identity")
        assert ident.body == "X.\nY."

    def test_empty_body_clears_section(self, install: Path) -> None:
        apply_edit(install, _world_section("Identity", "Phill. 46. Columbus, OH.", ""))
        out = (install / "seed" / "world.md").read_text(encoding="utf-8")
        assert "## Identity" in out
        assert "Phill. 46." not in out
        # next section still intact
        assert "## How They Think" in out

    def test_nonexistent_section_refused_by_allowlist(self, install: Path) -> None:
        # A heading that isn't a real world.md section isn't a Class-A target, so the
        # allowlist refuses it (403) BEFORE the section-locate path — fail-closed. The
        # section_not_found (422) path stays as defense for the benign TOCTOU where a
        # real section vanishes between the allowlist read and the replacement read.
        with pytest.raises(EditError) as ei:
            apply_edit(install, _world_section("Nonexistent", "", "x"))
        assert ei.value.code == "not_editable"
        assert ei.value.http_status == 403

    def test_ambiguous_heading_refused(self, tmp_path: Path) -> None:
        root = _make_install(tmp_path)
        dup = WORLD + "\n## Identity\n\nduplicate.\n"
        (root / "seed" / "world.md").write_text(dup, encoding="utf-8")
        with pytest.raises(EditError) as ei:
            apply_edit(root, _world_section("Identity", "Phill. 46. Columbus, OH.", "x"))
        assert ei.value.code == "section_ambiguous"


# --- stale check -----------------------------------------------------------

class TestStaleCheck:
    def test_section_stale_409(self, install: Path) -> None:
        with pytest.raises(EditError) as ei:
            apply_edit(install, _world_section("Identity", "WRONG expected body", "x"))
        assert ei.value.code == "stale"
        assert ei.value.http_status == 409

    def test_wholefile_stale_409(self, install: Path) -> None:
        with pytest.raises(EditError) as ei:
            apply_edit(install, {
                "kind": "config", "source": "activation/posture.md", "heading": None,
                "expected_body": "wrong", "new_body": "new posture",
            })
        assert ei.value.code == "stale"

    def test_wholefile_edit_ok(self, install: Path) -> None:
        res = apply_edit(install, {
            "kind": "config", "source": "activation/posture.md", "heading": None,
            "expected_body": POSTURE, "new_body": "New posture prose.",
        })
        assert res["ok"]
        assert (install / "activation" / "posture.md").read_text(encoding="utf-8") == "New posture prose.\n"


# --- reversibility: backup / audit / undo ----------------------------------

class TestReversibility:
    def test_backup_and_audit_written(self, install: Path) -> None:
        res = apply_edit(install, _world_section(
            "Identity", "Phill. 46. Columbus, OH.", "New."), now="2026-06-13T19:00:00+00:00")
        edits = recent_edits(install)
        assert len(edits) == 1
        rec = edits[0]
        assert rec["id"] == res["id"]
        assert rec["source"] == "seed/world.md"
        assert rec["heading"] == "Identity"
        assert rec["ts"] == "2026-06-13T19:00:00+00:00"
        # the backup holds the prior file content verbatim
        backup = install / rec["backup"]
        assert backup.is_file()
        assert backup.read_text(encoding="utf-8") == WORLD

    def test_newest_first(self, install: Path) -> None:
        apply_edit(install, _world_section("Identity", "Phill. 46. Columbus, OH.", "A."))
        out1 = (install / "seed" / "world.md").read_text(encoding="utf-8")
        ident_body = "A."  # the new current body of Identity
        apply_edit(install, _world_section("Identity", ident_body, "B."))
        edits = recent_edits(install)
        assert len(edits) == 2
        # newest first: the second edit ("B.") is index 0
        assert edits[0]["new_sha256"] != edits[1]["new_sha256"]

    def test_undo_restores_prior(self, install: Path) -> None:
        res = apply_edit(install, _world_section("Identity", "Phill. 46. Columbus, OH.", "Changed."))
        assert "Changed." in (install / "seed" / "world.md").read_text(encoding="utf-8")
        apply_edit(install, {"kind": "undo", "edit_id": res["id"]})
        restored = (install / "seed" / "world.md").read_text(encoding="utf-8")
        assert restored == WORLD

    def test_undo_of_create_removes_file(self, install: Path) -> None:
        # First entity_name on a fresh install creates config.json (no prior).
        res = apply_edit(install, {"kind": "entity_name", "value": "Aria"})
        cfg = install / ".levain" / "config.json"
        assert cfg.is_file()
        apply_edit(install, {"kind": "undo", "edit_id": res["id"]})
        assert not cfg.is_file()

    def test_undo_unknown_id_404(self, install: Path) -> None:
        with pytest.raises(EditError) as ei:
            apply_edit(install, {"kind": "undo", "edit_id": "deadbeef"})
        assert ei.value.http_status == 404

    def test_no_tmp_files_left(self, install: Path) -> None:
        apply_edit(install, _world_section("Identity", "Phill. 46. Columbus, OH.", "X."))
        leftovers = list((install / "seed").glob(".*tmp*"))
        assert leftovers == []

    def test_view_surfaces_recent_edits(self, install: Path) -> None:
        apply_edit(install, _world_section("Identity", "Phill. 46. Columbus, OH.", "New."))
        view = SubstrateSource.local(install).build()
        assert len(view.recent_edits) == 1
        assert view.recent_edits[0]["heading"] == "Identity"
        assert "edits" in [p["kind"] for p in view.layout()]


# --- entity name -----------------------------------------------------------

class TestEntityName:
    def test_sets_config_json(self, install: Path) -> None:
        res = apply_edit(install, {"kind": "entity_name", "value": "Aria"})
        assert res["ok"]
        cfg = json.loads((install / ".levain" / "config.json").read_text(encoding="utf-8"))
        assert cfg["entity_name"] == "Aria"

    def test_dashboard_prefers_config_over_h1(self, install: Path) -> None:
        # origin.md H1 says "Aria"; set config to a different name → config wins.
        apply_edit(install, {"kind": "entity_name", "value": "Sol"})
        view = SubstrateSource.local(install).build()
        assert view.entity_name == "Sol"

    def test_dashboard_falls_back_to_h1(self, install: Path) -> None:
        view = SubstrateSource.local(install).build()
        assert view.entity_name == "Aria"  # from origin.md H1, no config yet

    def test_stale_409(self, install: Path) -> None:
        apply_edit(install, {"kind": "entity_name", "value": "Aria"})
        with pytest.raises(EditError) as ei:
            apply_edit(install, {"kind": "entity_name", "value": "X", "expected": "WrongName"})
        assert ei.value.code == "stale"

    def test_too_long_422(self, install: Path) -> None:
        with pytest.raises(EditError) as ei:
            apply_edit(install, {"kind": "entity_name", "value": "z" * 200})
        assert ei.value.http_status == 422

    def test_control_chars_422(self, install: Path) -> None:
        with pytest.raises(EditError) as ei:
            apply_edit(install, {"kind": "entity_name", "value": "bad\nname"})
        assert ei.value.code == "bad_value"

    def test_empty_clears_name(self, install: Path) -> None:
        apply_edit(install, {"kind": "entity_name", "value": "Aria"})
        apply_edit(install, {"kind": "entity_name", "value": "", "expected": "Aria"})
        cfg = _read_levain_config(install)
        assert "entity_name" not in cfg


# --- untrusted input validation -------------------------------------------

class TestInputValidation:
    def test_non_dict(self, install: Path) -> None:
        with pytest.raises(EditError) as ei:
            apply_edit(install, ["not", "a", "dict"])  # type: ignore[arg-type]
        assert ei.value.http_status == 400

    def test_unknown_kind(self, install: Path) -> None:
        with pytest.raises(EditError) as ei:
            apply_edit(install, {"kind": "drop_tables"})
        assert ei.value.code == "bad_kind"

    def test_missing_field(self, install: Path) -> None:
        with pytest.raises(EditError) as ei:
            apply_edit(install, {"kind": "config", "source": "seed/world.md"})
        assert ei.value.http_status == 400

    def test_oversize_body_413(self, install: Path) -> None:
        with pytest.raises(EditError) as ei:
            apply_edit(install, _world_section(
                "Identity", "Phill. 46. Columbus, OH.", "z" * (256 * 1024 + 1)))
        assert ei.value.http_status == 413


# --- apparatus fixes (L1 + L2) --------------------------------------------

class TestApparatusFixes:
    def test_crlf_file_normalizes_to_consistent_lf(self, tmp_path: Path) -> None:
        # The REAL behavior (verified): a CRLF file reads as LF (universal newlines),
        # edits without a spurious 409, and is written consistent LF — never mixed.
        root = _make_install(tmp_path)
        (root / "seed" / "world.md").write_bytes(WORLD.replace("\n", "\r\n").encode("utf-8"))
        ident = next(d for d in _read_config_docs(root) if d.heading == "How They Think")
        assert "\r" not in ident.body  # the read layer presents LF
        res = apply_edit(root, _world_section("How They Think", ident.body, "Topological.\nSecond."))
        assert res["ok"]  # NOT a spurious 409
        out = (root / "seed" / "world.md").read_bytes()
        assert b"\r" not in out  # consistent LF, no stray \r (no mixed endings)
        assert b"Topological." in out and b"Phill. 46. Columbus, OH." in out

    def test_crlf_in_new_body_normalized(self, install: Path) -> None:
        # An untrusted client (curl) sending \r\n in new_body must not write mixed endings.
        apply_edit(install, _world_section("Communication", "Direct, profanity welcome.", "Line A.\r\nLine B."))
        out = (install / "seed" / "world.md").read_bytes()
        assert b"\r" not in out
        assert b"Line A.\nLine B." in out

    def test_section_break_rejected(self, install: Path) -> None:
        with pytest.raises(EditError) as ei:
            apply_edit(install, _world_section("Identity", "Phill. 46. Columbus, OH.", "text\n## Sneaky\nmore"))
        assert ei.value.code == "section_break"
        assert ei.value.http_status == 422

    def test_concurrent_same_section_serialized_no_lost_update(self, install: Path) -> None:
        from concurrent.futures import ThreadPoolExecutor

        expected = "Phill. 46. Columbus, OH."

        def attempt(i: int) -> str:
            try:
                apply_edit(install, _world_section("Identity", expected, f"writer-{i}"))
                return "ok"
            except EditError as e:
                return e.code

        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(attempt, range(8)))
        # the lock serializes: exactly one writer wins; the rest re-read under the lock
        # and get a clean 409 (no lost update, no torn audit).
        assert results.count("ok") == 1
        assert all(r in ("ok", "stale") for r in results)
        assert len(recent_edits(install)) == 1  # exactly one edit landed
        out = (install / "seed" / "world.md").read_text(encoding="utf-8")
        assert sum(f"writer-{i}" in out for i in range(8)) == 1  # one writer's content, not a mix

    def test_backup_self_confines(self, install: Path) -> None:
        from levain.writes import _backup

        with pytest.raises(EditError) as ei:
            _backup(install, "../../../../tmp/PWNED", "content", "someid")
        assert ei.value.code == "path_escape"

    def test_undo_refuses_stale_id_no_discard_of_newer(self, install: Path) -> None:
        # Two edits to the SAME file; undoing the OLDER one is refused (it would
        # silently discard the newer edit). Only the latest edit to a file undoes.
        r1 = apply_edit(install, _world_section("Identity", "Phill. 46. Columbus, OH.", "A."))
        r2 = apply_edit(install, _world_section("Communication", "Direct, profanity welcome.", "B."))
        with pytest.raises(EditError) as ei:
            apply_edit(install, {"kind": "undo", "edit_id": r1["id"]})
        assert ei.value.code == "stale" and ei.value.http_status == 409
        # the latest edit (r2) undoes cleanly; r1's change to the same file survives
        res = apply_edit(install, {"kind": "undo", "edit_id": r2["id"]})
        assert res["ok"]
        out = (install / "seed" / "world.md").read_text(encoding="utf-8")
        assert "A." in out and "Direct, profanity welcome." in out

    def test_cannot_double_undo(self, install: Path) -> None:
        r = apply_edit(install, _world_section("Identity", "Phill. 46. Columbus, OH.", "Once."))
        apply_edit(install, {"kind": "undo", "edit_id": r["id"]})
        # the file no longer matches r's result → a second undo of r is refused
        with pytest.raises(EditError) as ei:
            apply_edit(install, {"kind": "undo", "edit_id": r["id"]})
        assert ei.value.code == "stale"


# --- Slice 2b: the neocortex State edit -----------------------------------

class TestStateEdit:
    """Class-A State write: the lone operator-editable section of the consolidated-
    cognition file. The felt layer (five Class-C sections) is refused + provably
    untouched; the arrow-of-time net (undo refuses across a wrap) holds."""

    def test_section_edit_class_rule(self) -> None:
        # The single source of truth both read + write derive from.
        assert _section_edit_class("State") == CLASS_A
        for felt in ("Active Threads", "Patterns", "Decisions", "Context", "Understanding"):
            assert _section_edit_class(felt) == CLASS_C

    def test_edit_state_happy_path_touches_only_state(self, install: Path) -> None:
        cont = _add_continuity(install)
        before = dict(_split_sections(CONTINUITY))
        res = apply_edit(install, _state_req(before["State"], "**New focus.** Taper 73mg."))
        assert res["ok"] and res["heading"] == "State"
        after = dict(_split_sections(cont.read_text(encoding="utf-8")))
        assert after["State"] == "**New focus.** Taper 73mg."
        # every felt-layer section is byte-identical — the write touched only State.
        for felt in ("Active Threads", "Patterns", "Decisions", "Context", "Understanding"):
            assert after[felt] == before[felt], f"{felt} must be untouched"
        # and the H1/preamble + section ORDER outside State are preserved verbatim:
        # only the State body span differs from the original file.
        rebuilt = CONTINUITY.replace(before["State"], "**New focus.** Taper 73mg.")
        assert cont.read_text(encoding="utf-8") == rebuilt

    def test_audit_records_kind_state(self, install: Path) -> None:
        _add_continuity(install)
        apply_edit(install, _state_req(_section_body(CONTINUITY, "State"), "X."))
        rec = recent_edits(install)[0]
        assert rec["kind"] == "state"
        assert rec["heading"] == "State"
        assert rec["source"] == str(_CONTINUITY_REL)
        assert rec["backup"] is not None  # the prior State is recoverable

    @pytest.mark.parametrize(
        "felt", ["Active Threads", "Patterns", "Decisions", "Context", "Understanding"]
    )
    def test_refuses_felt_layer_sections(self, install: Path, felt: str) -> None:
        cont = _add_continuity(install)
        original = cont.read_text(encoding="utf-8")
        with pytest.raises(EditError) as ei:
            apply_edit(install, _state_req(_section_body(CONTINUITY, felt), "hacked", heading=felt))
        assert ei.value.code == "not_editable" and ei.value.http_status == 403
        assert cont.read_text(encoding="utf-8") == original  # nothing written

    def test_stale_check_after_wrap(self, install: Path) -> None:
        cont = _add_continuity(install)
        stale_expected = _section_body(CONTINUITY, "State")
        # a harness wrap rewrote State since the operator loaded the page
        cont.write_text(CONTINUITY.replace(stale_expected, "**Wrapped.** newer."), encoding="utf-8")
        with pytest.raises(EditError) as ei:
            apply_edit(install, _state_req(stale_expected, "operator's edit"))
        assert ei.value.code == "stale" and ei.value.http_status == 409

    def test_undo_restores_state_and_preserves_felt_layer(self, install: Path) -> None:
        cont = _add_continuity(install)
        before = dict(_split_sections(CONTINUITY))
        r = apply_edit(install, _state_req(before["State"], "edited."))
        res = apply_edit(install, {"kind": "undo", "edit_id": r["id"]})
        assert res["ok"]
        after = dict(_split_sections(cont.read_text(encoding="utf-8")))
        assert after["State"] == before["State"]  # restored
        assert after["Understanding"] == before["Understanding"]

    def test_undo_refuses_across_wrap_no_thread_fork(self, install: Path) -> None:
        # The arrow-of-time guard: once the harness wraps (rewrites the continuity
        # file) AFTER a State edit, undo refuses — it will not restore an old State
        # into a post-wrap file (a thread the linear history never held).
        cont = _add_continuity(install)
        r = apply_edit(install, _state_req(_section_body(CONTINUITY, "State"), "edited state."))
        wrapped = (cont.read_text(encoding="utf-8")
                   .replace("Phill is a paradox-holding ensemble mind.",
                            "Phill is a paradox-holding ensemble mind, re-consolidated."))
        cont.write_text(wrapped, encoding="utf-8")  # the consolidate touched the felt layer
        with pytest.raises(EditError) as ei:
            apply_edit(install, {"kind": "undo", "edit_id": r["id"]})
        assert ei.value.code == "stale" and ei.value.http_status == 409
        # the wrap's consolidation stands — undo did not fork it
        assert "re-consolidated." in cont.read_text(encoding="utf-8")

    def test_section_break_guard(self, install: Path) -> None:
        _add_continuity(install)
        with pytest.raises(EditError) as ei:
            apply_edit(install, _state_req(_section_body(CONTINUITY, "State"),
                                           "ok\n## Patterns\ninjected"))
        assert ei.value.code == "section_break" and ei.value.http_status == 422

    def test_not_found_when_no_continuity(self, install: Path) -> None:
        # no continuity file yet (entity hasn't wrapped) → 404, not a crash
        with pytest.raises(EditError) as ei:
            apply_edit(install, _state_req("", "first state"))
        assert ei.value.code == "not_found" and ei.value.http_status == 404

    def test_state_target_ignores_request_source(self, install: Path) -> None:
        # a `state` edit derives its target from the convention constant, NOT the
        # request — a crafted `source` cannot redirect the write to a seed file.
        cont = _add_continuity(install)
        seed_before = (install / "seed" / "origin.md").read_text(encoding="utf-8")
        req = _state_req(_section_body(CONTINUITY, "State"), "edited.")
        req["source"] = "seed/origin.md"  # attacker-supplied — must be ignored
        res = apply_edit(install, req)
        assert res["ok"] and res["source"] == str(_CONTINUITY_REL)
        # origin.md untouched; the State write landed in the continuity file
        assert (install / "seed" / "origin.md").read_text(encoding="utf-8") == seed_before
        assert "edited." in dict(_split_sections(cont.read_text(encoding="utf-8")))["State"]

    def test_unicode_line_separator_round_trips(self, install: Path) -> None:
        # [L1+L2 convergent MEDIUM] A State body with a Unicode LINE SEPARATOR
        # ( ) — which the read layer's splitlines() breaks on but read_text does
        # NOT normalize. The stale-check must accept the operator-visible (rendered)
        # body, NOT 409 forever; and the edit must not normalize a separator in a
        # sibling FELT section (byte-preserving surgery).
        text = (
            "# M\n\n## State\n\nline A line B\n\n"
            "## Patterns\n\nfelt layer\n\n## Understanding\n\nmind.\n"
        )
        cont = _add_continuity(install, text)
        # what the operator sees == what the write compares (both splitlines now)
        seen = _section_body(text, "State")
        assert seen == "line A\nline B"  # the rendered body, separator folded
        res = apply_edit(install, _state_req(seen, "edited."))
        assert res["ok"]  # would have been a permanent 409 before the fix
        on_disk = cont.read_text(encoding="utf-8")
        assert dict(_split_sections(on_disk))["State"] == "edited."
        # the sibling felt section's   survives byte-for-byte (surgery = split"\n")
        assert "felt layer" in on_disk

    @pytest.mark.parametrize("sep", [" ", " ", "\x0c", "\x85"])
    def test_section_break_guard_catches_unicode_separator_injection(
        self, install: Path, sep: str
    ) -> None:
        # [codex L3 HIGH] A new_body hiding a `## State` behind a Unicode line
        # separator the read parser breaks on (but split("\n") doesn't) would inject a
        # DUPLICATE section and brick State editing. The guard must use splitlines() —
        # the same boundary the read layer re-parses with — and refuse it.
        cont = _add_continuity(install)
        original = cont.read_text(encoding="utf-8")
        with pytest.raises(EditError) as ei:
            apply_edit(install, _state_req(_section_body(CONTINUITY, "State"),
                                           f"ok{sep}## State{sep}shadow"))
        assert ei.value.code == "section_break" and ei.value.http_status == 422
        assert cont.read_text(encoding="utf-8") == original  # nothing written

    def test_commit_cas_refuses_external_wrap_no_clobber(
        self, install: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [codex L3 HIGH] A harness consolidate (another process) rewrites the
        # continuity file BETWEEN Levain's read and its write; _WRITE_LOCK (threading)
        # can't see it. The _commit CAS re-read must refuse, not clobber the wrap with
        # Levain's stale snapshot.
        import levain.writes as W

        cont = _add_continuity(install)
        wrapped = CONTINUITY.replace(
            "Phill is a paradox-holding ensemble mind.",
            "Phill is a paradox-holding ensemble mind, re-consolidated.",
        )
        real = W._edit_one_section

        def _inject_wrap(raw: str, heading: str, exp: str, new: str) -> str:
            cont.write_text(wrapped, encoding="utf-8")  # wrap lands after read, before CAS
            return real(raw, heading, exp, new)

        monkeypatch.setattr(W, "_edit_one_section", _inject_wrap)
        with pytest.raises(EditError) as ei:
            apply_edit(install, _state_req(_section_body(CONTINUITY, "State"), "my edit"))
        assert ei.value.code == "stale" and ei.value.http_status == 409
        # the wrap's consolidation STANDS — Levain refused, did not clobber it
        assert "re-consolidated." in cont.read_text(encoding="utf-8")
        # a refused edit leaves no spurious audit/backup record (CAS is pre-audit)
        assert recent_edits(install) == []


class TestContinuityLockWiring:
    """AM-CONTLOCK: the continuity-targeting writes (State edit + undo-of-State) take
    anneal's SHARED `continuity_lock` across their read→write so a cross-process wrap
    can't interleave; the Levain-only targets (world.md config, entity_name,
    undo-of-config) do NOT — their only writer is Levain, `_WRITE_LOCK` suffices, and
    a needless cross-process lock there would be misleading."""

    @staticmethod
    def _spy(monkeypatch) -> list[str]:
        import contextlib
        import levain.writes as W

        calls: list[str] = []
        real = W.continuity_lock

        @contextlib.contextmanager
        def spy(path):
            calls.append(str(path))
            with real(path):  # delegate to the real lock — behaves exactly as prod
                yield

        monkeypatch.setattr(W, "continuity_lock", spy)
        return calls

    def test_state_edit_takes_the_lock(self, install: Path, monkeypatch) -> None:
        _add_continuity(install)
        calls = self._spy(monkeypatch)
        apply_edit(install, _state_req(_section_body(CONTINUITY, STATE_HEADING), "New focus."))
        assert len(calls) == 1 and calls[0].endswith("memory.continuity.md")

    def test_config_edit_does_not_take_the_lock(self, install: Path, monkeypatch) -> None:
        calls = self._spy(monkeypatch)
        apply_edit(install, _world_section("Identity", "Phill. 46. Columbus, OH.", "New."))
        assert calls == []

    def test_entity_name_does_not_take_the_lock(self, install: Path, monkeypatch) -> None:
        calls = self._spy(monkeypatch)
        apply_edit(install, {"kind": "entity_name", "value": "Aria"})
        assert calls == []

    def test_undo_of_state_takes_the_lock(self, install: Path, monkeypatch) -> None:
        _add_continuity(install)
        res = apply_edit(install, _state_req(_section_body(CONTINUITY, STATE_HEADING), "New focus."))
        calls = self._spy(monkeypatch)  # spy only the undo
        apply_edit(install, {"kind": "undo", "edit_id": res["id"]})
        assert len(calls) == 1 and calls[0].endswith("memory.continuity.md")

    def test_undo_of_config_does_not_take_the_lock(self, install: Path, monkeypatch) -> None:
        res = apply_edit(install, _world_section("Identity", "Phill. 46. Columbus, OH.", "New."))
        calls = self._spy(monkeypatch)  # spy only the undo
        apply_edit(install, {"kind": "undo", "edit_id": res["id"]})
        assert calls == []
