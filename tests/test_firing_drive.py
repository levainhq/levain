"""Tests for levain.firing.drive — the single drive-mode authority (K4a).

The three-rung mode replaced a ``human_present`` bool. The gate collapses ``headless`` and
``unattended`` (neither has anyone to fan an action in to), and since 2026-10-07 the cred floor's
default draws the same line (closed everywhere but the interactive REPL). These pin that line, and
that an explicit setting still overrides it, because a change to either is a security regression
no other test would catch.
"""

from __future__ import annotations

import pytest

from levain.firing.drive import (
    DRIVE_MODES,
    human_present,
    resolve_cred_floor,
)


# --- human_present: the gate's derivation ------------------------------------------------------

def test_only_interactive_has_a_human_present() -> None:
    assert human_present("interactive") is True
    assert human_present("headless") is False
    assert human_present("unattended") is False


def test_the_gate_deliberately_collapses_headless_and_unattended() -> None:
    """The gate's job is FAN-IN, and neither mode has anyone to fan an action in to at the moment
    it would fire. Pinned explicitly so a future change that splits them here has to argue with
    this test rather than drift past it."""
    assert human_present("headless") == human_present("unattended")


def test_unknown_mode_resolves_to_no_human() -> None:
    """Fail-SAFE: an unrecognized drive gets the gate ARMED, never waved through."""
    assert human_present("nonsense") is False


# --- resolve_cred_floor: the floor's distinction -----------------------------------------------

@pytest.mark.parametrize("mode", DRIVE_MODES)
def test_explicit_true_denies_in_every_mode(mode: str) -> None:
    assert resolve_cred_floor(True, mode=mode) is True


@pytest.mark.parametrize("mode", DRIVE_MODES)
def test_explicit_false_allows_in_every_mode(mode: str) -> None:
    """Including ``unattended`` — an explicit false is an operator OPT-IN, and a seat whose job is
    "open a PR nightly" genuinely needs gh. The unattended default is a DEFAULT, not a prohibition."""
    assert resolve_cred_floor(False, mode=mode) is False


def test_absent_derives_from_the_drive() -> None:
    assert resolve_cred_floor(None, mode="interactive") is False
    assert resolve_cred_floor(None, mode="headless") is True    # flipped 2026-10-07 (cockpit chat)
    assert resolve_cred_floor(None, mode="unattended") is True


@pytest.mark.parametrize("mode", [*DRIVE_MODES, "garbage"])
def test_the_absent_cred_floor_follows_the_gates_own_line(mode) -> None:
    """Since 2026-10-07 (Phill ruled the default flipped): the standard credential stores are open by
    default only where a human watches the read as it happens, which is exactly human_present. A
    headless turn (a --task run, a cockpit chat turn) is captured before anyone reads it."""
    assert resolve_cred_floor(None, mode=mode) is (not human_present(mode))


def test_garbage_mode_denies_on_BOTH_halves_of_the_authority() -> None:
    """A binding refuses an unknown mode outright (`ConversationBinding.create`);
    `resolve_cred_floor` must deny a garbage mode just the same, or a direct library caller with a
    typo'd mode gets "allowed" from one half and "denied" from the other — one authority reporting
    two answers (codex L3 LOW)."""
    assert resolve_cred_floor(None, mode="NONSENSE") is True
    assert resolve_cred_floor(None, mode="unattended") is True
