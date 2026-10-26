"""V13 session 2: the supervisor's firewall self-probe (DESIGN_V13_REV2 §6; HEAD_RULINGS R2, R5, O2, N1, N4, N5).

T9: the launch probe sits between ``render`` and ``start``; any refusal, and an ``ok`` that is not ``closed``, means
no start and the one teardown. T10: the macOS re-probe inside the relay's select tick, its start-to-start schedule
and streak rule on an injected clock, and the relay against the fake helper while a probe runs.

The runtime helper is the fake script of ``test_slot_s2s_supervisor`` (its ``probe`` branch answers in the order
``FAKE_PROBES`` names). What only the VM shows (the real helper through sudo, pf, the Seatbelt) is V13's M/L rows.
"""
from __future__ import annotations

import inspect
import os
import signal
import threading
import time
from pathlib import Path

import pytest

from levain.slot import supervisor as SV
from tests.test_slot_s2fx3_supervisor import rig as fx3_rig, stop_when
from tests.test_slot_s2s_supervisor import SESSION, Rig, bounded, isolated_git  # noqa: F401


def verbs(r: Rig) -> list:
    return [x["verb"] for x in r.verbs()]


# --- T9: the launch probe ---------------------------------------------------------------------------------------------

@bounded(60)
def test_launch_order_is_render_probe_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = Rig(tmp_path, monkeypatch)
    assert r.run() == 0
    v = r.verbs()
    assert [x["verb"] for x in v] == ["prepare", "render", "probe", "start", "teardown"]
    assert v[2]["args"] == [SESSION]
    err = r.stderr()
    assert "firewall self-probe not run" not in err
    assert "firewall self-probe: closed (api.anthropic.com 192.0.2.10 not reachable as the slot; 2 addresses)" in err


@bounded(60)
@pytest.mark.parametrize("name", ["firewall-open", "firewall-probe-inconclusive:dns",
                                  "firewall-probe-inconclusive:no-control-connect", "firewall-probe-unknown:child",
                                  "session-busy", "not-probeable:route"])
def test_a_launch_probe_refusal_means_no_start_and_one_teardown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                name: str) -> None:
    r = Rig(tmp_path, monkeypatch, FAKE_PROBES=name)
    assert r.run() == 1
    assert verbs(r) == ["prepare", "render", "probe", "teardown"]
    err = r.stderr()
    assert f"refused {name}: runtime helper probe" in err
    assert "api.anthropic.com 192.0.2.10" in err            # the helper's stderr detail (O1), shown escaped


@bounded(60)
def test_an_ok_that_is_not_closed_is_refused_malformed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = Rig(tmp_path, monkeypatch, FAKE_PROBES="malformed")
    assert r.run() == 1
    assert verbs(r) == ["prepare", "render", "probe", "teardown"]
    assert "refused firewall-probe-malformed" in r.stderr()


@bounded(60)
def test_a_launch_probe_past_its_deadline_means_no_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = Rig(tmp_path, monkeypatch, FAKE_PROBES="hang")
    r.seams.helper_deadline_s = {**SV.HELPER_DEADLINE_S, "probe": 0.5}
    assert r.run() == 1
    assert verbs(r) == ["prepare", "render", "probe", "teardown"]
    assert "refused helper-timeout" in r.stderr()


def test_the_probe_deadline_and_no_seam() -> None:
    assert SV.HELPER_DEADLINE_S["probe"] == 45.0
    assert "firewall_probe" not in {f.name for f in SV.Seams.__dataclass_fields__.values()}
    src = inspect.getsource(SV)
    assert "firewall self-probe not run" not in src
