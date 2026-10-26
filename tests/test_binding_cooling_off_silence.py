"""The behaviour ``levain binding policy create --help`` promises: a COOLING_OFF graduation with no action allowlisted
to auto-fire is denied on silence, even under a policy that covers it. Built on ``test_autonomic_gate_coverage.CWorld``."""
from __future__ import annotations

import pytest

from levain.autonomic import Posture
from tests.autonomic_test_only_confinement import test_only_confined_executor  # noqa: F401  (the root-run closure has no conftest)
from tests.test_autonomic_gate_coverage import LATER, CWorld, _covering  # noqa: F401

pytestmark = pytest.mark.usefixtures("test_only_confined_executor")


def test_a_covered_cooling_off_hold_with_an_empty_auto_fire_set_is_denied_on_silence(tmp_path):
    w = CWorld(tmp_path, coverage=_covering("link0", lowest="COOLING_OFF"), auto_fire=frozenset())
    w.mint(Posture.COOLING_OFF)
    w.dispatch("m1")
    [p] = w.gate.open_pendings()
    assert p.posture == "COOLING_OFF"
    [out] = w.sweep()
    assert not out.fired and w.outbox() == []
    assert w.gate.open_pendings() == []


def test_the_same_hold_fires_on_silence_once_its_action_is_allowlisted(tmp_path):
    w = CWorld(tmp_path, coverage=_covering("link0", lowest="COOLING_OFF"), auto_fire=frozenset({"link0"}))
    w.mint(Posture.COOLING_OFF)
    w.dispatch("m1")
    [out] = w.sweep()
    assert out.fired and w.outbox() == ["link0"]
