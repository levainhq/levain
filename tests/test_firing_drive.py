"""Tests for levain.firing.drive — the single drive-mode authority (K4a).

The three-rung mode replaced a ``human_present`` bool that had been an UNDER-MODELED AXIS: the
gate is right to collapse ``headless`` and ``unattended`` (neither has anyone to fan an action in
to), and the crown-jewels cred floor is wrong to (only the unattended one compounds unsupervised).
These pin BOTH halves of that asymmetry — the collapse and the distinction — because a change that
loses either one is a security regression that no other test would catch.
"""

from __future__ import annotations

import pytest

from levain.firing.drive import (
    DRIVE_MODES,
    LEVAIN_DRIVE_MODE_ENV,
    bind_drive_mode,
    current_drive_mode,
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
    assert resolve_cred_floor(None, mode="headless") is False
    assert resolve_cred_floor(None, mode="unattended") is True


def test_the_floor_does_NOT_collapse_headless_and_unattended() -> None:
    """The counterpart to the gate test above, and the whole reason this module exists: a human
    typing `--task "open a PR"` legitimately needs gh, while a scheduled seat's silent credential
    read can compound into always-loaded memory with nobody in the loop."""
    assert resolve_cred_floor(None, mode="headless") != resolve_cred_floor(None, mode="unattended")


# --- the fork-safe channel ---------------------------------------------------------------------

def test_bind_then_read_round_trips(monkeypatch) -> None:
    monkeypatch.delenv(LEVAIN_DRIVE_MODE_ENV, raising=False)
    bind_drive_mode("interactive")
    assert current_drive_mode() == "interactive"
    bind_drive_mode("unattended")
    assert current_drive_mode() == "unattended"


def test_unbound_fails_CLOSED_to_unattended(monkeypatch) -> None:
    """The asymmetry, pinned: wrongly denying is a visible refusal fixed in one config line;
    wrongly granting is a silent credential exposure on an unattended seat with nobody watching.
    The only way this is unset in a real run is a WIRING failure, which must never widen the floor."""
    monkeypatch.delenv(LEVAIN_DRIVE_MODE_ENV, raising=False)
    assert current_drive_mode() == "unattended"
    assert resolve_cred_floor(None, mode=current_drive_mode()) is True


def test_a_garbage_env_value_also_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv(LEVAIN_DRIVE_MODE_ENV, "INTERACTIVE!!")   # not a valid mode
    assert current_drive_mode() == "unattended"
