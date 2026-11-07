"""The envelope ``launches`` grant, through the phone port: the cockpit composer may ask for it, a paired phone never can.
Fixtures are the phone sheet's own (a real authority log, a software passkey, the dispatch store)."""
from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest

from levain.coordinator import envelope as env
from levain.coordinator.phone import COCKPIT_DEVICE, CoordRefused
from levain.web_server import CoordSheet
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import T, _call, _pair, _serve
from tests.test_coordinator_phone import coord_for, review
from tests.test_dispatch_phone import ORIGIN, RP, phone_for, repo, world  # noqa: F401  (fixtures)

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


# L3 r1 #1: the device a cockpit coord step runs as comes from the request's AUTHENTICATED credential. The /cockpit/coord/*
# gate accepts a paired device's token as well as the cockpit's surface token, so a fixed "cockpit" label let a paired
# phone handcraft spec.launches, sign it with its passkey, and store the grant the phone sheet may never set.
@pytest.fixture
def served_sheet(world, tmp_path_factory):
    """A real pairing serve (its own install) whose coord sheet is the phone fixtures' real CoordPhone and store."""
    _root, src, _ids = _install(tmp_path_factory.mktemp("coord-pair"))
    with _serve(src) as (port, httpd):
        signer = SimpleNamespace(keys=phone_for(world)._keys)
        sheet = CoordSheet(world["qdir"], RP, ORIGIN, signer)
        sheet.phone = coord_for(world)                 # the very CoordPhone the phone tests drive (same keys and store)
        httpd.coord_sheet = sheet
        yield port, sheet


def _post_review(world, port, sheet, token, outbox_id, spec):
    req = {"words": "launch for me", "outbox_id": outbox_id, "spec": spec, "signer_fp": world["phone"].fp,
           "store_id": sheet.phone.listing()["coord_store_id"]}
    return _call(port, "POST", "/cockpit/coord/envelope-review", req, token=token)


def test_a_paired_phone_token_cannot_set_launches_on_the_cockpit_route_and_the_cockpit_token_can(world, served_sheet):
    port, sheet = served_sheet
    device, paired = _pair(port)
    assert device.startswith("d-") and device != COCKPIT_DEVICE
    st, out, _ = _post_review(world, port, sheet, paired, "ph-" + "a" * 32, _launch_spec(world))
    assert (st, out["error"]) == (422, "refused") and "sets only" in out["message"], (st, out)
    # the same body with the cockpit's own surface token is taken, and the body it builds carries the grant
    st, out, _ = _post_review(world, port, sheet, T, "ph-" + "b" * 32, _launch_spec(world))
    assert st == 200, out
    assert env.parse_body(out["body_text"])["launches"] == _launch_spec(world)["launches"]
    # and a paired phone is still served for everything it may set (the device it runs as is its own id)
    no_launch = {k: v for k, v in _launch_spec(world).items() if k != "launches"}
    st, out, _ = _post_review(world, port, sheet, paired, "ph-" + "c" * 32, no_launch)
    assert st == 200 and "launches" not in env.parse_body(out["body_text"]), out
    assert sheet.phone._reviews[out["review_id"]]["device"] == device
