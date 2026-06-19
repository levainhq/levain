"""Tests for ``levain.spores`` — the disposition vocab + the Open-Loops/Tray/Keep
bucketing partition (control-plane Slice 3)."""

from __future__ import annotations

from levain.spores import (
    BUCKET_KEEP,
    BUCKET_LOOP,
    BUCKET_TRAY,
    KEEP_DISPOSITIONS,
    LOOP_DISPOSITION,
    NON_COGNITION_DISPOSITIONS,
    PARKED_TIER,
    TRAY_DISPOSITIONS,
    VALID_DISPOSITIONS,
    bucket_of,
    disposition_of,
    is_loop,
    is_note,
    is_tray,
)


# --- the frozen-literal contract -------------------------------------------
# ⚠ The drift guard (L2 Q4). This vocab is duplicated in flow's scripts/spores.py BY
# DESIGN (anneal is deliberately disposition-blind — the de-risk). The two copies MUST
# agree, but they live in separate installs with no shared import, so a self-pin here can
# only catch an ACCIDENTAL unilateral edit WITHIN Levain (reddens this repo's CI). The
# TRUE cross-repo equality check (levain.spores == flow's scripts/spores) lives flow-side,
# where both modules are importable in one venv — scripts/test_tray_disposition.py. Pin
# the literals here so any change to this tuple is a deliberate, CI-visible act that
# prompts "did I update flow too?".

def test_frozen_disposition_literals():
    assert LOOP_DISPOSITION == "loop"
    assert TRAY_DISPOSITIONS == ("seed", "handoff", "agenda")
    assert KEEP_DISPOSITIONS == ("note",)
    assert NON_COGNITION_DISPOSITIONS == ("seed", "handoff", "agenda", "note")
    assert VALID_DISPOSITIONS == ("loop", "seed", "handoff", "agenda", "note")


def test_parked_tier_is_a_real_anneal_tier():
    # L2 Q5: PARKED_TIER is a re-declared literal (import-light), but Keep gates on it. If
    # anneal ever renamed/dropped its dormancy tier, a Keep item would silently fall
    # through to Open Loops. anneal IS a Levain dependency, so assert the coupling for real
    # — this reddens Levain's CI the moment anneal's tier vocab moves under us.
    from anneal_memory.spores import VALID_TIERS

    assert PARKED_TIER in VALID_TIERS


# --- disposition_of / is_loop / is_tray ------------------------------------

def test_disposition_of_defaults_and_falsy_read_as_loop():
    assert disposition_of({}) == LOOP_DISPOSITION
    assert disposition_of({"disposition": None}) == LOOP_DISPOSITION
    assert disposition_of({"disposition": ""}) == LOOP_DISPOSITION
    assert disposition_of({"disposition": "seed"}) == "seed"


def test_is_loop_excludes_all_operator_io_dispositions():
    assert is_loop({})  # absent → loop (every pre-Slice-3 spore)
    assert is_loop({"disposition": "loop"})
    # both Tray inbox AND Keep notes are operator I/O → excluded from cognition
    for d in NON_COGNITION_DISPOSITIONS:
        assert not is_loop({"disposition": d})


def test_three_classes_are_distinct_is_tray_is_not_just_not_loop():
    # With `note` added, is_tray is NO LONGER `not is_loop` — a note is non-loop but
    # non-tray (it's Keep reference). The three classes (loop / tray / note) are disjoint.
    for d in TRAY_DISPOSITIONS:
        assert is_tray({"disposition": d}) and not is_note({"disposition": d})
    for d in KEEP_DISPOSITIONS:
        assert is_note({"disposition": d}) and not is_tray({"disposition": d})
    # a note: non-loop (excluded) but NOT a Tray item — the bug the explicit is_tray fixes
    assert not is_loop({"disposition": "note"}) and not is_tray({"disposition": "note"})


def test_is_loop_fails_open_on_unknown():
    # An unknown/typo disposition reads as an in-cognition LOOP (a real loop wrongly
    # hidden is the silent-harm class), never silently swallowed into the Tray.
    for d in ("sed", "Seed", " seed", "todo", "0"):
        assert is_loop({"disposition": d}), f"{d!r} should fail open to loop"


# --- bucket_of: the total partition ----------------------------------------

def test_bucket_of_is_a_total_partition():
    # loop + active tier → Open Loops
    assert bucket_of({"tier": "hot"}) == BUCKET_LOOP
    assert bucket_of({"disposition": "loop", "tier": "warm"}) == BUCKET_LOOP
    # any Tray disposition → Tray (regardless of tier)
    for d in TRAY_DISPOSITIONS:
        assert bucket_of({"disposition": d, "tier": "warm"}) == BUCKET_TRAY
    # both Keep halves → Keep: a note (reference, any tier) AND a parked loop
    for d in KEEP_DISPOSITIONS:
        assert bucket_of({"disposition": d, "tier": "warm"}) == BUCKET_KEEP
    assert bucket_of({"tier": "parked"}) == BUCKET_KEEP
    assert bucket_of({"disposition": "loop", "tier": "parked"}) == BUCKET_KEEP


def test_bucket_of_disposition_wins_over_parked():
    # A parked-but-un-triaged seed is still a Tray item (it needs the AI's sort).
    assert bucket_of({"disposition": "seed", "tier": "parked"}) == BUCKET_TRAY
    # a parked note is still Keep reference (disposition checked before tier).
    assert bucket_of({"disposition": "note", "tier": "parked"}) == BUCKET_KEEP
