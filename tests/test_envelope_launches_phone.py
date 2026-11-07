"""The envelope ``launches`` grant, through the phone port: the cockpit composer may ask for it, a paired phone never can.
Fixtures are the phone sheet's own (a real authority log, a software passkey, the dispatch store)."""
from __future__ import annotations

import shutil

import pytest

from levain.coordinator import envelope as env
from levain.coordinator.phone import COCKPIT_DEVICE, CoordRefused
from tests.test_coordinator_phone import coord_for, review
from tests.test_dispatch_phone import repo, world  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


def _launch_spec(world, **over):
    path = str(world["repo"][0])
    return {"where": [{"repo": path, "paths": ["."]}], "lands": [], "stop": "2h", "budget": {"tool_calls": 40},
            "launches": [{"repo": path, "count": 2}], **over}


def test_the_cockpit_composer_may_ask_for_launches(world):
    cp = coord_for(world)
    r = cp.step("envelope-review", {"words": "launch for me", "outbox_id": "ph-" + "c" * 32, "spec": _launch_spec(world),
                                    "signer_fp": world["phone"].fp, "store_id": cp.listing()["coord_store_id"]},
                device=COCKPIT_DEVICE)
    body = env.parse_body(r["body_text"])
    assert body["launches"] == _launch_spec(world)["launches"]


def test_the_phone_sheet_cannot_set_launches_however_the_request_is_worded(world):
    cp = coord_for(world)
    with pytest.raises(CoordRefused, match="sets only"):
        review(world, cp, "launch for me", "ph-" + "d" * 32, _launch_spec(world))
    with pytest.raises(CoordRefused, match="sets only"):
        review(world, cp, "launch for me", "ph-" + "e" * 32, _launch_spec(world, launches=[]))
    ok = review(world, cp, "no launches", "ph-" + "f" * 32, {k: v for k, v in _launch_spec(world).items() if k != "launches"})
    assert "launches" not in env.parse_body(ok["body_text"])
