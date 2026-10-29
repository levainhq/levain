"""A1c2: the fire check on the resolve paths (1026-20 RULINGS R13-10's silence row): a covered cooling-off pending
proposed under one policy and fired by its silence default under another must fire only if THAT policy's guard lets
the trigger event through. The tripping arm is the reproduced failure, held as a strict xfail until it is built: it
FIRES today (the forward fire check evaluated policy A's guard at the proposal; nothing evaluates B's)."""
from __future__ import annotations

import json
import shutil
from datetime import timedelta

import pytest

from levain.autonomic import Posture

pytest.importorskip("cryptography")
from tests.autonomic_test_only_coverage import TEST_GUARD  # noqa: E402
from tests.test_autonomic_fire_check import TRIPS  # noqa: E402
from tests.test_graduation_journey import Journey  # noqa: E402

pytestmark = [pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen"),
              pytest.mark.usefixtures("test_only_confined_executor")]


def _create(j: Journey, tmp, gid: str, guard) -> None:
    path = tmp / f"{gid}.json"
    path.write_text(json.dumps([dict(e) for e in guard]))
    assert j.cli("policy", "create", "--action", "link0", "--lowest", "COOLING_OFF", "--days", "30",
                 "--id", gid, "--guard", str(path)) == 0


_UNBUILT = pytest.mark.xfail(strict=True, reason=(
    "A1c2 residue (R13-10's silence row): a /2 hold carries no trigger event, so the resolve path cannot evaluate the "
    "guard of a policy that began covering after the proposal; the silence default fires it"))


@pytest.mark.parametrize("second", [pytest.param("trips", marks=_UNBUILT), "quiet"])
def test_a_silence_fire_under_a_policy_other_than_the_proposing_one_evaluates_that_policys_guard(tmp_path, monkeypatch,
                                                                                               second):
    j = Journey(tmp_path, monkeypatch)
    _create(j, tmp_path, "gp-aaaaaaaaaaaa", TEST_GUARD)                  # does not trip on the rig's event
    j.fire.mint(Posture.COOLING_OFF)
    o = j.dispatch("proposed under policy A")
    assert o.pending and o.posture is Posture.COOLING_OFF
    assert j.cli("policy", "revoke", "gp-aaaaaaaaaaaa") == 0
    _create(j, tmp_path, "gp-bbbbbbbbbbbb", TRIPS if second == "trips" else TEST_GUARD)
    j.later(timedelta(days=1))
    out = j.fire.sweep(j.fire.now)
    for s in out:
        print(f"  sweep: fired={s.fired} posture={s.posture.name} refused={s.refused} reason={s.reason}")
    if second == "trips":
        assert out and not any(s.fired for s in out) and j.fire.outbox() == []
    else:
        assert j.fire.outbox() == ["link0"]
