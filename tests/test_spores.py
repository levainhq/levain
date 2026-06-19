"""Tests for ``levain.spores`` — the disposition vocab + the Open-Loops/Tray/Keep
bucketing partition (control-plane Slice 3)."""

from __future__ import annotations

from levain.spores import (
    BUCKET_KEEP,
    BUCKET_LOOP,
    BUCKET_TRAY,
    LOOP_DISPOSITION,
    PARKED_TIER,
    TRAY_DISPOSITIONS,
    VALID_DISPOSITIONS,
    bucket_of,
    disposition_of,
    is_loop,
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
    assert VALID_DISPOSITIONS == ("loop", "seed", "handoff", "agenda")


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


def test_is_loop_excludes_only_known_tray_dispositions():
    assert is_loop({})  # absent → loop (every pre-Slice-3 spore)
    for d in TRAY_DISPOSITIONS:
        assert not is_loop({"disposition": d})
        assert is_tray({"disposition": d})
    # is_tray is the exact complement of is_loop
    for item in ({}, {"disposition": "loop"}, {"disposition": "seed"}):
        assert is_tray(item) == (not is_loop(item))


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
    # loop + parked tier → Keep
    assert bucket_of({"tier": "parked"}) == BUCKET_KEEP
    assert bucket_of({"disposition": "loop", "tier": "parked"}) == BUCKET_KEEP


def test_bucket_of_disposition_wins_over_parked():
    # A parked-but-un-triaged seed is still a Tray item (it needs the AI's sort), which is
    # exactly what keeps the render boundary identical to layer 2 (is_tray ≡ not is_loop).
    assert bucket_of({"disposition": "seed", "tier": "parked"}) == BUCKET_TRAY
