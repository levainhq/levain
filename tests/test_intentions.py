"""Tests for ``levain.intentions`` — the disposition vocab + the List bucketing (control-plane Slice 3, and the loop fold,
board item levain#4: there is no ``loop`` kind in Levain 1.0)."""

from __future__ import annotations

import pytest

from levain.intentions import (
    BUCKET_LOOP,
    BUCKET_TRAY,
    DEFAULT_DISPOSITION,
    KEEP_DISPOSITIONS,
    RETIRED_DISPOSITIONS,
    RETIRED_LOOP_MSG,
    LOOP_DISPOSITION,
    NON_COGNITION_DISPOSITIONS,
    PARKED_TIER,
    RetiredDisposition,
    TRAY_DISPOSITIONS,
    TRAY_WRITE_DISPOSITIONS,
    VALID_DISPOSITIONS,
    WRITE_DISPOSITIONS,
    bucket_of,
    disposition_of,
    guarded_intention_store,
    is_loop,
    is_note,
    is_talk_seed,
    is_tray,
    retired_message,
)


# --- the frozen-literal contract -------------------------------------------
# ⚠ The drift guard (L2 Q4). This vocab is duplicated in flow's scripts/intentions.py BY
# DESIGN (anneal is deliberately disposition-blind — the de-risk). The two copies MUST
# agree, but they live in separate installs with no shared import, so a self-pin here can
# only catch an ACCIDENTAL unilateral edit WITHIN Levain (reddens this repo's CI). The
# TRUE cross-repo equality check (levain.intentions == flow's scripts/intentions) lives flow-side,
# where both modules are importable in one venv — scripts/test_tray_disposition.py. Pin
# the literals here so any change to this tuple is a deliberate, CI-visible act that
# prompts "did I update flow too?".

def test_frozen_disposition_literals():
    assert LOOP_DISPOSITION == "loop"
    assert DEFAULT_DISPOSITION == "someday"
    assert TRAY_DISPOSITIONS == ("someday", "seed", "handoff", "agenda")      # the READ set
    assert TRAY_WRITE_DISPOSITIONS == ("someday", "agenda")
    # the loop fold: `loop` is a retired write name, appended after the three older ones
    assert RETIRED_DISPOSITIONS == ("handoff", "note", "seed", "loop")
    assert KEEP_DISPOSITIONS == ("note",)
    # these two are drift-contract tuples (byte-equal with flow's twin and both hooks), so the fold leaves them as they were
    assert NON_COGNITION_DISPOSITIONS == ("someday", "seed", "handoff", "agenda", "note")
    assert VALID_DISPOSITIONS == ("loop", "someday", "seed", "handoff", "agenda", "note")   # every value a row may hold
    assert WRITE_DISPOSITIONS == ("someday", "agenda")                                      # `loop` left the write set


def test_parked_tier_is_a_real_anneal_tier():
    # L2 Q5: PARKED_TIER is a re-declared literal (import-light). If anneal ever renamed/dropped
    # its dormancy tier, a parked row would silently lose its marker and its last rank. anneal IS a
    # Levain dependency, so assert the coupling for real — this reddens Levain's CI the moment
    # anneal's tier vocab moves under us.
    from anneal_memory.spores import VALID_TIERS

    assert PARKED_TIER in VALID_TIERS


# --- disposition_of / is_loop / is_tray ------------------------------------

def test_disposition_of_defaults_and_falsy_read_as_someday():
    """The fold's default: an absent, empty, None or literal `loop` disposition READS as someday (the entity's own
    `intention_add` cannot carry a disposition, so a key-absent row is made after any migration)."""
    assert disposition_of({}) == "someday"
    assert disposition_of({"disposition": None}) == "someday"
    assert disposition_of({"disposition": ""}) == "someday"
    assert disposition_of({"disposition": "loop"}) == "someday"
    assert disposition_of({"tier": "parked"}) == "someday"        # the tier is not the disposition
    assert disposition_of({"disposition": "seed"}) == "seed"       # any other string is returned as stored
    assert disposition_of({"disposition": "GARBAGE"}) == "GARBAGE"  # an unknown value is shown by name


@pytest.mark.parametrize("bad", [0, False, [], {}, 7])
def test_disposition_of_passes_a_malformed_value_through_as_unknown(bad):
    """Only None, "" and the literal `loop` default; a falsy non-string is NOT someday (the migration reports it unknown, so
    the read must not disagree and let it into the Tray-only transitions)."""
    assert disposition_of({"disposition": bad}) == bad
    assert not is_tray({"disposition": bad})


def test_is_loop_is_false_for_every_intention():
    """No class flows into the entity's own cognition any more (Phill np-10262267 (a), np-c3029a8a (a)): the name
    stays as the drift-contract twin."""
    assert not is_loop({})
    assert not is_loop({"disposition": "loop"})
    assert not is_loop({"disposition": "GARBAGE"})        # the old fail-open toward the loop is gone with the kind
    for d in NON_COGNITION_DISPOSITIONS:
        assert not is_loop({"disposition": d})


def test_the_classes_are_tray_and_legacy_note():
    for d in TRAY_DISPOSITIONS:
        assert is_tray({"disposition": d}) and not is_note({"disposition": d})
    for d in KEEP_DISPOSITIONS:
        assert is_note({"disposition": d}) and not is_tray({"disposition": d})
    # a key-absent row reads someday, so it is a Tray row (the entity's own plant)
    assert is_tray({}) and not is_note({})
    assert is_tray({"disposition": "loop"})
    # an unknown value is neither: shown by name on the List, and it is not a Tray inbox member
    assert not is_tray({"disposition": "sed"}) and not is_note({"disposition": "sed"})


def test_a_key_absent_row_with_attention_talk_is_a_talk_seed():
    """The sibling raw read: `is_talk_seed` reads the disposition through `disposition_of`, as a key-absent row reads someday."""
    assert is_talk_seed({"attention": "talk"})
    assert is_talk_seed({"disposition": "loop", "attention": "talk"})
    assert not is_talk_seed({"disposition": "agenda", "attention": "talk"})
    assert not is_talk_seed({})


# --- bucket_of: total onto the List ----------------------------------------

def test_bucket_of_is_the_tray_for_every_row():
    for row in ({}, {"disposition": "loop"}, {"disposition": "GARBAGE"}, {"tier": "parked"}, {"tier": "hot"},
                {"disposition": "loop", "tier": "warm"}, {"disposition": "loop", "tier": "parked"}, {"disposition": ""}):
        assert bucket_of(row) == BUCKET_TRAY, row
    # any Tray disposition → Tray (regardless of tier)
    for d in TRAY_DISPOSITIONS + KEEP_DISPOSITIONS:
        assert bucket_of({"disposition": d, "tier": "warm"}) == BUCKET_TRAY
        assert bucket_of({"disposition": d, "tier": "parked"}) == BUCKET_TRAY
    # `BUCKET_LOOP` stays exported for the embedder's reads through 1.0, and nothing returns it
    assert BUCKET_LOOP == "loop" and BUCKET_LOOP != BUCKET_TRAY
    import levain.intentions as sp
    assert not hasattr(sp, "BUCKET_KEEP")


# --- `loop` is retired at the write seams ----------------------------------

def test_loop_is_a_retired_write_name_with_words():
    assert retired_message("loop") == RETIRED_LOOP_MSG
    assert retired_message("Loop") == RETIRED_LOOP_MSG           # any spelling anneal would persist as it
    assert retired_message("loop \n") == RETIRED_LOOP_MSG
    assert "someday" in RETIRED_LOOP_MSG and "agenda" in RETIRED_LOOP_MSG
    assert "loop" in VALID_DISPOSITIONS and "loop" not in WRITE_DISPOSITIONS


@pytest.mark.parametrize("spelling", ["loop", "Loop", "LOOP "])
def test_the_guarded_store_refuses_to_set_a_loop(tmp_path, spelling):
    store = guarded_intention_store(tmp_path / "memory.spores.json")
    with pytest.raises(RetiredDisposition, match="folded into the List"):
        store.add(type="thought", text="x", disposition=spelling)
    made = store.add(type="thought", text="x")           # no disposition named: key-absent, the entity's own plant
    assert "disposition" not in store.get(made["id"])
    with pytest.raises(RetiredDisposition, match="folded into the List"):
        store.update(made["id"], disposition=spelling)
    assert "disposition" not in store.get(made["id"])
