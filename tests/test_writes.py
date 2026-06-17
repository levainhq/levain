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
    _CONTINUITY_SOURCE,
    EditError,
    WriteScope,
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


def _scope(install: Path) -> WriteScope:
    """The WriteScope for an install. apply_edit now takes the explicit write surface;
    ``from_install_root`` reproduces the pre-WriteScope install-convention derivation
    (store paths by the .levain/memory.db stem, ledger + backups under .levain/), so
    these tests exercise the same behavior through the new entry point."""
    return WriteScope.from_install_root(install)


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
            apply_edit(_scope(install), {
                "kind": "config", "source": "seed/origin.md", "heading": None,
                "expected_body": ORIGIN, "new_body": "hacked",
            })
        assert ei.value.code == "not_editable"
        assert ei.value.http_status == 403

    def test_refuses_constitution_edit(self, install: Path) -> None:
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), {
                "kind": "config", "source": "seed/partnership.md", "heading": None,
                "expected_body": CONSTITUTION, "new_body": "hacked",
            })
        assert ei.value.http_status == 403

    def test_refuses_unknown_source(self, install: Path) -> None:
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), {
                "kind": "config", "source": "seed/secrets.md", "heading": None,
                "expected_body": "", "new_body": "x",
            })
        assert ei.value.code == "not_editable"

    def test_refuses_path_escape(self, install: Path) -> None:
        # Even if an allowlist match were faked, the path-confinement refuses ../.
        with pytest.raises(EditError):
            apply_edit(_scope(install), {
                "kind": "config", "source": "../escape.md", "heading": None,
                "expected_body": "", "new_body": "x",
            })

    def test_world_section_with_null_heading_refused(self, install: Path) -> None:
        # world.md has no (source, heading=None) Class-A doc — only per-section docs.
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), {
                "kind": "config", "source": "seed/world.md", "heading": None,
                "expected_body": WORLD, "new_body": "x",
            })
        assert ei.value.code == "not_editable"


# --- section surgery -------------------------------------------------------

class TestSectionEdit:
    def test_replaces_only_target_section(self, install: Path) -> None:
        res = apply_edit(_scope(install), _world_section(
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
        apply_edit(_scope(install), _world_section("Identity", "Phill. 46. Columbus, OH.", "New identity."))
        out = (install / "seed" / "world.md").read_text(encoding="utf-8")
        assert "New identity." in out
        assert "# Who Your Operator Is" in out
        assert "First principles" in out

    def test_edit_last_section(self, install: Path) -> None:
        apply_edit(_scope(install), _world_section("Communication", "Direct, profanity welcome.", "Terse."))
        out = (install / "seed" / "world.md").read_text(encoding="utf-8")
        assert "Terse." in out
        assert "Phill. 46." in out

    def test_reparse_round_trips(self, install: Path) -> None:
        # After an edit the read layer re-parses the new body correctly.
        apply_edit(_scope(install), _world_section("Identity", "Phill. 46. Columbus, OH.", "X.\nY."))
        docs = {d.key: d for d in _read_config_docs(install) if d.source == "seed/world.md"}
        ident = next(d for d in docs.values() if d.heading == "Identity")
        assert ident.body == "X.\nY."

    def test_empty_body_clears_section(self, install: Path) -> None:
        apply_edit(_scope(install), _world_section("Identity", "Phill. 46. Columbus, OH.", ""))
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
            apply_edit(_scope(install), _world_section("Nonexistent", "", "x"))
        assert ei.value.code == "not_editable"
        assert ei.value.http_status == 403

    def test_ambiguous_heading_refused(self, tmp_path: Path) -> None:
        root = _make_install(tmp_path)
        dup = WORLD + "\n## Identity\n\nduplicate.\n"
        (root / "seed" / "world.md").write_text(dup, encoding="utf-8")
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(root), _world_section("Identity", "Phill. 46. Columbus, OH.", "x"))
        assert ei.value.code == "section_ambiguous"


# --- stale check -----------------------------------------------------------

class TestStaleCheck:
    def test_section_stale_409(self, install: Path) -> None:
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), _world_section("Identity", "WRONG expected body", "x"))
        assert ei.value.code == "stale"
        assert ei.value.http_status == 409

    def test_wholefile_stale_409(self, install: Path) -> None:
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), {
                "kind": "config", "source": "activation/posture.md", "heading": None,
                "expected_body": "wrong", "new_body": "new posture",
            })
        assert ei.value.code == "stale"

    def test_wholefile_edit_ok(self, install: Path) -> None:
        res = apply_edit(_scope(install), {
            "kind": "config", "source": "activation/posture.md", "heading": None,
            "expected_body": POSTURE, "new_body": "New posture prose.",
        })
        assert res["ok"]
        assert (install / "activation" / "posture.md").read_text(encoding="utf-8") == "New posture prose.\n"


# --- reversibility: backup / audit / undo ----------------------------------

class TestReversibility:
    def test_backup_and_audit_written(self, install: Path) -> None:
        res = apply_edit(_scope(install), _world_section(
            "Identity", "Phill. 46. Columbus, OH.", "New."), now="2026-06-13T19:00:00+00:00")
        edits = recent_edits(install / ".levain")
        assert len(edits) == 1
        rec = edits[0]
        assert rec["id"] == res["id"]
        assert rec["source"] == "seed/world.md"
        assert rec["heading"] == "Identity"
        assert rec["ts"] == "2026-06-13T19:00:00+00:00"
        # the backup holds the prior file content verbatim (rec["backup"] is relative to
        # the ledger root — .levain/ for a from_install_root scope)
        backup = install / ".levain" / rec["backup"]
        assert backup.is_file()
        assert backup.read_text(encoding="utf-8") == WORLD

    def test_newest_first(self, install: Path) -> None:
        apply_edit(_scope(install), _world_section("Identity", "Phill. 46. Columbus, OH.", "A."))
        ident_body = "A."  # the new current body of Identity
        apply_edit(_scope(install), _world_section("Identity", ident_body, "B."))
        edits = recent_edits(install / ".levain")
        assert len(edits) == 2
        # newest first: the second edit ("B.") is index 0
        assert edits[0]["new_sha256"] != edits[1]["new_sha256"]

    def test_undo_restores_prior(self, install: Path) -> None:
        res = apply_edit(_scope(install), _world_section("Identity", "Phill. 46. Columbus, OH.", "Changed."))
        assert "Changed." in (install / "seed" / "world.md").read_text(encoding="utf-8")
        apply_edit(_scope(install), {"kind": "undo", "edit_id": res["id"]})
        restored = (install / "seed" / "world.md").read_text(encoding="utf-8")
        assert restored == WORLD

    def test_undo_refuses_forged_non_backups_rel(self, install: Path) -> None:
        # [L1 LOW] A forged audit record whose `backup` names a file OUTSIDE the backups
        # tree (e.g. the audit log itself) must be REFUSED, never used as a restore source.
        # The CAS gate runs first, so forge a REAL edit's record (new_sha256 stays valid)
        # and tamper only its `backup` field — this exercises the prefix validation.
        res = apply_edit(_scope(install), _world_section("Identity", "Phill. 46. Columbus, OH.", "New."))
        log = install / ".levain" / "edits.jsonl"
        lines = log.read_text(encoding="utf-8").splitlines()
        rec = json.loads(lines[-1])
        rec["backup"] = "edits.jsonl"  # inside the ledger, but NOT a backups-tree path
        lines[-1] = json.dumps(rec)
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), {"kind": "undo", "edit_id": res["id"]})
        assert ei.value.code == "corrupt_record" and ei.value.http_status == 422

    def test_undo_refuses_backups_traversal(self, install: Path) -> None:
        # [codex L3 HIGH] A backup rel that PASSES the `backups/` prefix but uses `..` to
        # escape the subdir (`backups/../edits.jsonl`) must be refused — the prefix string
        # is not enough; the RESOLVED path must stay under backups/. (CAS runs first, so
        # forge a real edit's record and tamper only `backup`.)
        res = apply_edit(_scope(install), _world_section("Identity", "Phill. 46. Columbus, OH.", "New."))
        log = install / ".levain" / "edits.jsonl"
        lines = log.read_text(encoding="utf-8").splitlines()
        rec = json.loads(lines[-1])
        rec["backup"] = "backups/../edits.jsonl"  # passes the prefix, escapes via ..
        lines[-1] = json.dumps(rec)
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), {"kind": "undo", "edit_id": res["id"]})
        assert ei.value.code == "corrupt_record" and ei.value.http_status == 422

    def test_undo_of_create_removes_file(self, install: Path) -> None:
        # First entity_name on a fresh install creates config.json (no prior).
        res = apply_edit(_scope(install), {"kind": "entity_name", "value": "Aria"})
        cfg = install / ".levain" / "config.json"
        assert cfg.is_file()
        apply_edit(_scope(install), {"kind": "undo", "edit_id": res["id"]})
        assert not cfg.is_file()

    def test_undo_unknown_id_404(self, install: Path) -> None:
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), {"kind": "undo", "edit_id": "deadbeef"})
        assert ei.value.http_status == 404

    def test_no_tmp_files_left(self, install: Path) -> None:
        apply_edit(_scope(install), _world_section("Identity", "Phill. 46. Columbus, OH.", "X."))
        leftovers = list((install / "seed").glob(".*tmp*"))
        assert leftovers == []

    def test_view_surfaces_recent_edits(self, install: Path) -> None:
        apply_edit(_scope(install), _world_section("Identity", "Phill. 46. Columbus, OH.", "New."))
        view = SubstrateSource.local(install).build()
        assert len(view.recent_edits) == 1
        assert view.recent_edits[0]["heading"] == "Identity"
        assert "edits" in [p["kind"] for p in view.layout()]


# --- entity name -----------------------------------------------------------

class TestEntityName:
    def test_sets_config_json(self, install: Path) -> None:
        res = apply_edit(_scope(install), {"kind": "entity_name", "value": "Aria"})
        assert res["ok"]
        cfg = json.loads((install / ".levain" / "config.json").read_text(encoding="utf-8"))
        assert cfg["entity_name"] == "Aria"

    def test_dashboard_prefers_config_over_h1(self, install: Path) -> None:
        # origin.md H1 says "Aria"; set config to a different name → config wins.
        apply_edit(_scope(install), {"kind": "entity_name", "value": "Sol"})
        view = SubstrateSource.local(install).build()
        assert view.entity_name == "Sol"

    def test_dashboard_falls_back_to_h1(self, install: Path) -> None:
        view = SubstrateSource.local(install).build()
        assert view.entity_name == "Aria"  # from origin.md H1, no config yet

    def test_stale_409(self, install: Path) -> None:
        apply_edit(_scope(install), {"kind": "entity_name", "value": "Aria"})
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), {"kind": "entity_name", "value": "X", "expected": "WrongName"})
        assert ei.value.code == "stale"

    def test_too_long_422(self, install: Path) -> None:
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), {"kind": "entity_name", "value": "z" * 200})
        assert ei.value.http_status == 422

    def test_control_chars_422(self, install: Path) -> None:
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), {"kind": "entity_name", "value": "bad\nname"})
        assert ei.value.code == "bad_value"

    def test_empty_clears_name(self, install: Path) -> None:
        apply_edit(_scope(install), {"kind": "entity_name", "value": "Aria"})
        apply_edit(_scope(install), {"kind": "entity_name", "value": "", "expected": "Aria"})
        cfg = _read_levain_config(install)
        assert "entity_name" not in cfg


# --- untrusted input validation -------------------------------------------

class TestInputValidation:
    def test_non_dict(self, install: Path) -> None:
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), ["not", "a", "dict"])  # type: ignore[arg-type]
        assert ei.value.http_status == 400

    def test_unknown_kind(self, install: Path) -> None:
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), {"kind": "drop_tables"})
        assert ei.value.code == "bad_kind"

    def test_missing_field(self, install: Path) -> None:
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), {"kind": "config", "source": "seed/world.md"})
        assert ei.value.http_status == 400

    def test_oversize_body_413(self, install: Path) -> None:
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), _world_section(
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
        res = apply_edit(_scope(root), _world_section("How They Think", ident.body, "Topological.\nSecond."))
        assert res["ok"]  # NOT a spurious 409
        out = (root / "seed" / "world.md").read_bytes()
        assert b"\r" not in out  # consistent LF, no stray \r (no mixed endings)
        assert b"Topological." in out and b"Phill. 46. Columbus, OH." in out

    def test_crlf_in_new_body_normalized(self, install: Path) -> None:
        # An untrusted client (curl) sending \r\n in new_body must not write mixed endings.
        apply_edit(_scope(install), _world_section("Communication", "Direct, profanity welcome.", "Line A.\r\nLine B."))
        out = (install / "seed" / "world.md").read_bytes()
        assert b"\r" not in out
        assert b"Line A.\nLine B." in out

    def test_section_break_rejected(self, install: Path) -> None:
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), _world_section("Identity", "Phill. 46. Columbus, OH.", "text\n## Sneaky\nmore"))
        assert ei.value.code == "section_break"
        assert ei.value.http_status == 422

    def test_concurrent_same_section_serialized_no_lost_update(self, install: Path) -> None:
        from concurrent.futures import ThreadPoolExecutor

        expected = "Phill. 46. Columbus, OH."

        def attempt(i: int) -> str:
            try:
                apply_edit(_scope(install), _world_section("Identity", expected, f"writer-{i}"))
                return "ok"
            except EditError as e:
                return e.code

        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(attempt, range(8)))
        # the lock serializes: exactly one writer wins; the rest re-read under the lock
        # and get a clean 409 (no lost update, no torn audit).
        assert results.count("ok") == 1
        assert all(r in ("ok", "stale") for r in results)
        assert len(recent_edits(install / ".levain")) == 1  # exactly one edit landed
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
        r1 = apply_edit(_scope(install), _world_section("Identity", "Phill. 46. Columbus, OH.", "A."))
        r2 = apply_edit(_scope(install), _world_section("Communication", "Direct, profanity welcome.", "B."))
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), {"kind": "undo", "edit_id": r1["id"]})
        assert ei.value.code == "stale" and ei.value.http_status == 409
        # the latest edit (r2) undoes cleanly; r1's change to the same file survives
        res = apply_edit(_scope(install), {"kind": "undo", "edit_id": r2["id"]})
        assert res["ok"]
        out = (install / "seed" / "world.md").read_text(encoding="utf-8")
        assert "A." in out and "Direct, profanity welcome." in out

    def test_cannot_double_undo(self, install: Path) -> None:
        r = apply_edit(_scope(install), _world_section("Identity", "Phill. 46. Columbus, OH.", "Once."))
        apply_edit(_scope(install), {"kind": "undo", "edit_id": r["id"]})
        # the file no longer matches r's result → a second undo of r is refused
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), {"kind": "undo", "edit_id": r["id"]})
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
        res = apply_edit(_scope(install), _state_req(before["State"], "**New focus.** Taper 73mg."))
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
        apply_edit(_scope(install), _state_req(_section_body(CONTINUITY, "State"), "X."))
        rec = recent_edits(install / ".levain")[0]
        assert rec["kind"] == "state"
        assert rec["heading"] == "State"
        assert rec["source"] == _CONTINUITY_SOURCE
        assert rec["backup"] is not None  # the prior State is recoverable

    @pytest.mark.parametrize(
        "felt", ["Active Threads", "Patterns", "Decisions", "Context", "Understanding"]
    )
    def test_refuses_felt_layer_sections(self, install: Path, felt: str) -> None:
        cont = _add_continuity(install)
        original = cont.read_text(encoding="utf-8")
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), _state_req(_section_body(CONTINUITY, felt), "hacked", heading=felt))
        assert ei.value.code == "not_editable" and ei.value.http_status == 403
        assert cont.read_text(encoding="utf-8") == original  # nothing written

    def test_stale_check_after_wrap(self, install: Path) -> None:
        cont = _add_continuity(install)
        stale_expected = _section_body(CONTINUITY, "State")
        # a harness wrap rewrote State since the operator loaded the page
        cont.write_text(CONTINUITY.replace(stale_expected, "**Wrapped.** newer."), encoding="utf-8")
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), _state_req(stale_expected, "operator's edit"))
        assert ei.value.code == "stale" and ei.value.http_status == 409

    def test_undo_restores_state_and_preserves_felt_layer(self, install: Path) -> None:
        cont = _add_continuity(install)
        before = dict(_split_sections(CONTINUITY))
        r = apply_edit(_scope(install), _state_req(before["State"], "edited."))
        res = apply_edit(_scope(install), {"kind": "undo", "edit_id": r["id"]})
        assert res["ok"]
        after = dict(_split_sections(cont.read_text(encoding="utf-8")))
        assert after["State"] == before["State"]  # restored
        assert after["Understanding"] == before["Understanding"]

    def test_undo_refuses_across_wrap_no_thread_fork(self, install: Path) -> None:
        # The arrow-of-time guard: once the harness wraps (rewrites the continuity
        # file) AFTER a State edit, undo refuses — it will not restore an old State
        # into a post-wrap file (a thread the linear history never held).
        cont = _add_continuity(install)
        r = apply_edit(_scope(install), _state_req(_section_body(CONTINUITY, "State"), "edited state."))
        wrapped = (cont.read_text(encoding="utf-8")
                   .replace("Phill is a paradox-holding ensemble mind.",
                            "Phill is a paradox-holding ensemble mind, re-consolidated."))
        cont.write_text(wrapped, encoding="utf-8")  # the consolidate touched the felt layer
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), {"kind": "undo", "edit_id": r["id"]})
        assert ei.value.code == "stale" and ei.value.http_status == 409
        # the wrap's consolidation stands — undo did not fork it
        assert "re-consolidated." in cont.read_text(encoding="utf-8")

    def test_section_break_guard(self, install: Path) -> None:
        _add_continuity(install)
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), _state_req(_section_body(CONTINUITY, "State"),
                                           "ok\n## Patterns\ninjected"))
        assert ei.value.code == "section_break" and ei.value.http_status == 422

    def test_not_found_when_no_continuity(self, install: Path) -> None:
        # no continuity file yet (entity hasn't wrapped) → 404, not a crash
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), _state_req("", "first state"))
        assert ei.value.code == "not_found" and ei.value.http_status == 404

    def test_state_target_ignores_request_source(self, install: Path) -> None:
        # a `state` edit derives its target from the convention constant, NOT the
        # request — a crafted `source` cannot redirect the write to a seed file.
        cont = _add_continuity(install)
        seed_before = (install / "seed" / "origin.md").read_text(encoding="utf-8")
        req = _state_req(_section_body(CONTINUITY, "State"), "edited.")
        req["source"] = "seed/origin.md"  # attacker-supplied — must be ignored
        res = apply_edit(_scope(install), req)
        assert res["ok"] and res["source"] == _CONTINUITY_SOURCE
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
        res = apply_edit(_scope(install), _state_req(seen, "edited."))
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
            apply_edit(_scope(install), _state_req(_section_body(CONTINUITY, "State"),
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
            apply_edit(_scope(install), _state_req(_section_body(CONTINUITY, "State"), "my edit"))
        assert ei.value.code == "stale" and ei.value.http_status == 409
        # the wrap's consolidation STANDS — Levain refused, did not clobber it
        assert "re-consolidated." in cont.read_text(encoding="utf-8")
        # a refused edit leaves no spurious audit/backup record (CAS is pre-audit)
        assert recent_edits(install / ".levain") == []


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
        def spy(path, **kw):  # **kw: the governed lock passes require=True
            calls.append(str(path))
            with real(path, **kw) as held:  # delegate to the real lock — exactly as prod
                yield held

        monkeypatch.setattr(W, "continuity_lock", spy)
        return calls

    def test_state_edit_takes_the_lock(self, install: Path, monkeypatch) -> None:
        _add_continuity(install)
        calls = self._spy(monkeypatch)
        apply_edit(_scope(install), _state_req(_section_body(CONTINUITY, STATE_HEADING), "New focus."))
        assert len(calls) == 1 and calls[0].endswith("memory.continuity.md")

    def test_state_edit_fails_closed_on_lockless_fs(self, install: Path, monkeypatch) -> None:
        # spore-091 #2: require=True → 503 (not a silent best-effort write) when the
        # cross-process lock can't be acquired (fcntl None simulates non-POSIX / lock-less).
        import anneal_memory.store as store_module

        monkeypatch.setattr(store_module, "fcntl", None)
        _add_continuity(install)
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), _state_req(_section_body(CONTINUITY, STATE_HEADING), "New focus."))
        assert ei.value.code == "lock_unavailable" and ei.value.http_status == 503
        # the file must be UNTOUCHED — fail closed means no write happened
        cont = (install / _CONTINUITY_REL).read_text(encoding="utf-8")
        assert _section_body(cont, STATE_HEADING) == _section_body(CONTINUITY, STATE_HEADING)

    def test_config_edit_does_not_take_the_lock(self, install: Path, monkeypatch) -> None:
        calls = self._spy(monkeypatch)
        apply_edit(_scope(install), _world_section("Identity", "Phill. 46. Columbus, OH.", "New."))
        assert calls == []

    def test_entity_name_does_not_take_the_lock(self, install: Path, monkeypatch) -> None:
        calls = self._spy(monkeypatch)
        apply_edit(_scope(install), {"kind": "entity_name", "value": "Aria"})
        assert calls == []

    def test_undo_of_state_takes_the_lock(self, install: Path, monkeypatch) -> None:
        _add_continuity(install)
        res = apply_edit(_scope(install), _state_req(_section_body(CONTINUITY, STATE_HEADING), "New focus."))
        calls = self._spy(monkeypatch)  # spy only the undo
        apply_edit(_scope(install), {"kind": "undo", "edit_id": res["id"]})
        assert len(calls) == 1 and calls[0].endswith("memory.continuity.md")

    def test_undo_of_config_does_not_take_the_lock(self, install: Path, monkeypatch) -> None:
        res = apply_edit(_scope(install), _world_section("Identity", "Phill. 46. Columbus, OH.", "New."))
        calls = self._spy(monkeypatch)  # spy only the undo
        apply_edit(_scope(install), {"kind": "undo", "edit_id": res["id"]})
        assert calls == []


# --- Class-B lifecycle verbs (Slice 2b-ii): spores + episode tombstone -------

def _add_spore(install: Path, *, stype: str = "task", text: str = "ship 2b-ii", **kw) -> str:
    from anneal_memory.spores import SporeStore

    s = SporeStore(install / ".levain" / "memory.spores.json").add(type=stype, text=text, **kw)
    return str(s["id"])


def _spore_store(install: Path):
    from anneal_memory.spores import SporeStore

    return SporeStore(install / ".levain" / "memory.spores.json")


def _add_episode(install: Path, *, content: str = "built the writable handle") -> str:
    from anneal_memory import Store

    with Store(str(install / ".levain" / "memory.db")) as store:
        ep = store.record(content, "observation")
    return ep.id


class TestSporeVerbs:
    def test_touch_is_non_destructive_and_updates_seen(self, install: Path) -> None:
        sid = _add_spore(install)
        res = apply_edit(_scope(install), {"kind": "spore_touch", "spore_id": sid})
        assert res["ok"] and res["action"] == "touch"
        # still open (touch never resolves) and recorded in the audit trail
        assert any(s["id"] == sid for s in _spore_store(install).list_open())
        rec = recent_edits(install / ".levain")[0]
        assert rec["kind"] == "spore_touch" and rec["source"] == f"spore:{sid}"
        assert rec["undoable"] is False

    def test_descend_requires_confirm(self, install: Path) -> None:
        sid = _add_spore(install)
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), {"kind": "spore_descend", "spore_id": sid, "spore_kind": "done"})
        assert ei.value.code == "confirm_required" and ei.value.http_status == 409
        # refused → the spore is untouched (still open)
        assert any(s["id"] == sid for s in _spore_store(install).list_open())

    def test_descend_with_confirm_composts(self, install: Path) -> None:
        sid = _add_spore(install)
        res = apply_edit(_scope(install), {
            "kind": "spore_descend", "spore_id": sid, "spore_kind": "done", "confirm": True,
        })
        assert res["ok"]
        store = _spore_store(install)
        assert not any(s["id"] == sid for s in store.list_open()), "composted → gone from open"
        rec = recent_edits(install / ".levain")[0]
        assert rec["kind"] == "spore_descend" and rec["verb_kind"] == "done"

    def test_descend_bad_kind_for_type(self, install: Path) -> None:
        sid = _add_spore(install, stype="task")  # task descend kinds = done/dropped/composted
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), {
                "kind": "spore_descend", "spore_id": sid, "spore_kind": "answered",  # question-only
                "confirm": True,
            })
        assert ei.value.code == "bad_verb_arg" and ei.value.http_status == 422
        assert any(s["id"] == sid for s in _spore_store(install).list_open()), "rejected → still open"

    def test_ascend_requires_ref(self, install: Path) -> None:
        sid = _add_spore(install)
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), {
                "kind": "spore_ascend", "spore_id": sid, "spore_kind": "project", "confirm": True,
            })
        assert ei.value.http_status == 400  # _require_str("ref") missing

    def test_ascend_with_ref_and_confirm(self, install: Path) -> None:
        sid = _add_spore(install)
        res = apply_edit(_scope(install), {
            "kind": "spore_ascend", "spore_id": sid, "spore_kind": "project",
            "ref": "projects/levain", "confirm": True,
        })
        assert res["ok"]
        assert not any(s["id"] == sid for s in _spore_store(install).list_open())
        rec = recent_edits(install / ".levain")[0]
        assert rec["kind"] == "spore_ascend" and rec["ref"] == "projects/levain"

    def test_unknown_spore_id(self, install: Path) -> None:
        _add_spore(install)  # store exists, id doesn't
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), {
                "kind": "spore_descend", "spore_id": "spore-999", "spore_kind": "done", "confirm": True,
            })
        assert ei.value.code == "verb_failed" and ei.value.http_status == 422

    def test_no_spore_store_yet(self, install: Path) -> None:
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), {"kind": "spore_touch", "spore_id": "spore-001"})
        assert ei.value.code == "not_found" and ei.value.http_status == 404


class TestEpisodeTombstone:
    def test_requires_confirm(self, install: Path) -> None:
        eid = _add_episode(install)
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), {"kind": "episode_tombstone", "episode_id": eid})
        assert ei.value.code == "confirm_required" and ei.value.http_status == 409

    def test_with_confirm_deletes_and_keeps_tombstone(self, install: Path) -> None:
        from anneal_memory import Store

        eid = _add_episode(install)
        res = apply_edit(_scope(install), {"kind": "episode_tombstone", "episode_id": eid, "confirm": True})
        assert res["ok"] and res["action"] == "tombstone"
        with Store(str(install / ".levain" / "memory.db"), read_only=True) as store:
            assert store.status().tombstone_count >= 1
        rec = recent_edits(install / ".levain")[0]
        assert rec["kind"] == "episode_tombstone" and rec["undoable"] is False

    def test_unknown_episode_id(self, install: Path) -> None:
        _add_episode(install)  # db exists, id doesn't
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), {"kind": "episode_tombstone", "episode_id": "nope", "confirm": True})
        assert ei.value.code == "not_found" and ei.value.http_status == 404


class TestClassBGovernance:
    def test_undo_refuses_a_verb_record(self, install: Path) -> None:
        sid = _add_spore(install)
        res = apply_edit(_scope(install), {"kind": "spore_touch", "spore_id": sid})
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), {"kind": "undo", "edit_id": res["id"]})
        assert ei.value.code == "not_undoable" and ei.value.http_status == 400

    def test_no_crystal_retire_verb_exists(self, install: Path) -> None:
        # Fork 1 = A: a crystal is the entity's own consolidated wisdom (Class C),
        # never an operator button — there is no retire kind to dispatch.
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), {"kind": "crystal_retire", "name": "some_pattern", "confirm": True})
        assert ei.value.code == "bad_kind" and ei.value.http_status == 400


class TestClassBRobustness:
    """codex L3 findings: a committed verb must not be reported as failure when only the
    secondary edit-log append fails (HIGH), and raw store OSErrors map to a clean 503 (MED)."""

    def test_committed_verb_survives_audit_append_failure(self, install: Path, monkeypatch) -> None:
        import warnings

        import levain.writes as W

        sid = _add_spore(install)

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(W, "_append_audit", boom)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            res = apply_edit(_scope(install), {
                "kind": "spore_descend", "spore_id": sid, "spore_kind": "done", "confirm": True,
            })
        # committed op reports SUCCESS (not a 500) even though the edit-log line failed;
        # no audit id is returned, and the spore is genuinely composted (canonical in anneal).
        assert res["ok"] is True and res["id"] == ""
        assert not any(s["id"] == sid for s in _spore_store(install).list_open())

    def test_spore_verb_oserror_maps_to_503(self, install: Path, monkeypatch) -> None:
        from anneal_memory.spores import SporeStore

        sid = _add_spore(install)

        def boom(*a, **k):
            raise OSError("no locks available")

        monkeypatch.setattr(SporeStore, "touch", boom)
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), {"kind": "spore_touch", "spore_id": sid})
        assert ei.value.code == "store_unavailable" and ei.value.http_status == 503

    def test_tombstone_oserror_maps_to_503(self, install: Path, monkeypatch) -> None:
        from anneal_memory import Store

        eid = _add_episode(install)

        def boom(*a, **k):
            raise OSError("permission denied")

        monkeypatch.setattr(Store, "delete", boom)
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), {"kind": "episode_tombstone", "episode_id": eid, "confirm": True})
        assert ei.value.code == "store_unavailable" and ei.value.http_status == 503


# --- the WriteScope decoupling: a NON-install substrate (flow's own store) ----

class TestExplicitPathsScope:
    """The capability the Bridge write-enable rests on: a NON-install substrate —
    ``install_root=None``, explicit anneal paths, a ledger NOT under any install —
    exactly flow's own store (anneal at ~/.anneal-memory, spores in the repo, no
    ``.levain/``). The governed write seam runs off the explicit paths, the ledger lands
    where the scope says, and the seed/config kinds (which need an install) refuse cleanly.
    The pre-WriteScope code re-derived every target from ``install_root/.levain`` — so
    none of this was reachable; these tests lock the decoupling."""

    @staticmethod
    def _make(tmp_path: Path, *, continuity: bool = False, spores: bool = False):
        """A WriteScope mirroring flow's N-of-1 layout: the anneal store in one dir, the
        spores file in a SEPARATE 'repo' tree, the ledger under the repo's state dir —
        and NO install_root. Returns ``(scope, spore_id|None)``."""
        from anneal_memory.spores import SporeStore

        from levain.dashboard import AnnealPaths

        anneal_dir = tmp_path / "anneal"
        repo_state = tmp_path / "repo" / "state"
        anneal_dir.mkdir(parents=True, exist_ok=True)
        repo_state.mkdir(parents=True, exist_ok=True)
        paths = AnnealPaths(
            episodic_db=anneal_dir / "memory.db",
            continuity_md=anneal_dir / "memory.continuity.md",
            crystal_json=anneal_dir / "memory.crystal.json",
            spores_json=repo_state / "spores.json",  # the N-of-1 split (spores in the repo)
        )
        if continuity:
            paths.continuity_md.write_text(CONTINUITY, encoding="utf-8")
        sid = None
        if spores:
            sid = str(SporeStore(paths.spores_json).add(type="task", text="ship slice 1")["id"])
        ledger = repo_state / "bridge"  # flow's chosen ledger home (state/bridge/)
        return WriteScope(anneal=paths, ledger_root=ledger, install_root=None), sid

    def test_state_edit_targets_explicit_out_of_tree_continuity(self, tmp_path: Path) -> None:
        scope, _ = self._make(tmp_path, continuity=True)
        res = apply_edit(scope, _state_req(_section_body(CONTINUITY, "State"), "explicit edit."))
        assert res["ok"] and res["source"] == _CONTINUITY_SOURCE
        # landed in the EXPLICIT continuity file — there is no install root anywhere
        assert "explicit edit." in scope.anneal.continuity_md.read_text(encoding="utf-8")
        # the ledger landed under the EXPLICIT ledger_root (flow's state/bridge/), and the
        # backup is recoverable there (resolved relative to ledger_root)
        rec = recent_edits(scope.ledger_root)[0]
        assert rec["kind"] == "state"
        assert (scope.ledger_root / "edits.jsonl").is_file()
        assert (scope.ledger_root / rec["backup"]).is_file()

    def test_spore_touch_targets_explicit_spores_json(self, tmp_path: Path) -> None:
        scope, sid = self._make(tmp_path, spores=True)
        res = apply_edit(scope, {"kind": "spore_touch", "spore_id": sid})
        assert res["ok"] and res["source"] == f"spore:{sid}"
        rec = recent_edits(scope.ledger_root)[0]
        assert rec["kind"] == "spore_touch" and rec["source"] == f"spore:{sid}"

    def test_config_edit_refused_without_install_root(self, tmp_path: Path) -> None:
        scope, _ = self._make(tmp_path)
        with pytest.raises(EditError) as ei:
            apply_edit(scope, _world_section("Identity", "x", "y"))
        assert ei.value.http_status == 422 and ei.value.code == "no_install"

    def test_entity_name_refused_without_install_root(self, tmp_path: Path) -> None:
        scope, _ = self._make(tmp_path)
        with pytest.raises(EditError) as ei:
            apply_edit(scope, {"kind": "entity_name", "value": "flow"})
        assert ei.value.code == "no_install"

    def test_undo_state_edit_without_install_root(self, tmp_path: Path) -> None:
        scope, _ = self._make(tmp_path, continuity=True)
        before = scope.anneal.continuity_md.read_text(encoding="utf-8")
        res = apply_edit(scope, _state_req(_section_body(CONTINUITY, "State"), "tweak."))
        assert "tweak." in scope.anneal.continuity_md.read_text(encoding="utf-8")
        undo = apply_edit(scope, {"kind": "undo", "edit_id": res["id"]})
        assert undo["ok"]
        assert scope.anneal.continuity_md.read_text(encoding="utf-8") == before
