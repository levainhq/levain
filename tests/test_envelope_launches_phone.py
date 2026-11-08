"""The envelope ``launches`` grant on the cockpit's coord routes: the cockpit page and a paired device's cockpit may ask for
it (Phill 2026-11-08, row 460), any other surface's token never can; the listing says which, from the same test.
Fixtures are the phone sheet's own (a real authority log, a software passkey, the dispatch store)."""
from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest

from levain.coordinator import envelope as env
from levain.coordinator.phone import COCKPIT_DEVICE, CoordRefused, may_launch
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


def test_a_paired_devices_cockpit_may_ask_for_launches(world):
    """Row 460 (a): his cockpit on his phone runs as its paired device id and may add the grant its passkey signs."""
    cp = coord_for(world)
    ok = review(world, cp, "launch for me", "ph-" + "d" * 32, _launch_spec(world))
    assert env.parse_body(ok["body_text"])["launches"] == _launch_spec(world)["launches"]
    ok = review(world, cp, "no launches", "ph-" + "f" * 32, {k: v for k, v in _launch_spec(world).items() if k != "launches"})
    assert "launches" not in env.parse_body(ok["body_text"])


@pytest.mark.parametrize("device, ok", [(COCKPIT_DEVICE, True), ("d-0a1b2c", True), ("d-" + "f" * 12, True),
                                        ("surface:relay", False), ("surface:browser", False), ("d-", False),
                                        ("d-XYZ", False), ("cockpit ", False), ("", False), (None, False), (7, False)])
def test_may_launch_is_the_cockpit_page_or_a_paired_device_and_nothing_else(device, ok):
    assert may_launch(device) is ok


def test_another_surfaces_token_cannot_set_launches_and_is_told_the_toggle_off_still_starts_the_desk(world):
    cp = coord_for(world)
    with pytest.raises(CoordRefused) as e:
        cp.step("envelope-review", {"words": "launch for me", "outbox_id": "ph-" + "e" * 32, "spec": _launch_spec(world),
                                    "signer_fp": world["phone"].fp, "store_id": cp.listing()["coord_store_id"]},
                device="surface:relay")
    assert (e.value.status, e.value.error) == (422, "refused") and "sets only" in e.value.message
    assert "Launching seats off" in e.value.message and "still starts the desk" in e.value.message


def _cockpit_review(world, cp, outbox_id, spec):
    return cp.step("envelope-review", {"words": "launch for me", "outbox_id": outbox_id, "spec": spec,
                                       "signer_fp": world["phone"].fp, "store_id": cp.listing()["coord_store_id"]},
                   device=COCKPIT_DEVICE)


@pytest.mark.parametrize("launches", [[{"repo": "REPO", "count": 2.0}], [{"repo": "REPO", "count": "2"}],
                                      [{"repo": "REPO", "count": 2, "extra": 1}], [{"repo": "REPO"}], ["REPO"],
                                      {"repo": "REPO", "count": 2}, [{"repo": "relative", "count": 2}]])
def test_a_malformed_cockpit_launch_entry_is_a_422_refusal_never_a_500(world, launches):
    """L3 r2 MED: the cockpit path passed spec.launches through untouched, so a float count made canonical() raise a
    ContractError (a ValueError no coord route translates: HTTP 500). Every malformed entry is a refusal in words."""
    path = str(world["repo"][0])
    fill = (lambda v: [({**e, "repo": path} if isinstance(e, dict) and e.get("repo") == "REPO" else
                        path if e == "REPO" else e) for e in v] if isinstance(v, list) else
            {**v, "repo": path})
    cp = coord_for(world)
    with pytest.raises(CoordRefused) as e:
        _cockpit_review(world, cp, "ph-" + "1" * 32, _launch_spec(world, launches=fill(launches)))
    assert (e.value.status, e.value.error) == (422, "refused")
    assert "launches is a list of" in e.value.message


def test_bounds_and_a_repo_named_twice_from_the_cockpit_are_refusals_too(world):
    path = str(world["repo"][0])
    cp = coord_for(world)
    for i, launches in enumerate(([{"repo": path, "count": env.LAUNCH_MAX + 1}], [{"repo": path, "count": 1}, {"repo": path, "count": 2}])):
        with pytest.raises(CoordRefused) as e:
            _cockpit_review(world, cp, "ph-" + str(i + 2) * 32, _launch_spec(world, launches=launches))
        assert (e.value.status, e.value.error) == (422, "refused")


def test_the_cockpit_key_refusal_names_the_cockpit_and_lists_launches():
    """L3 r2 LOW: the refusal said "from the phone" and listed the phone's keys to a cockpit request."""
    from levain.coordinator.phone import CoordPhone
    from levain.coordinator.store import CoordError

    with pytest.raises(CoordError) as e:
        CoordPhone._spec(None, {"where": [], "launches": [], "foo": 1}, COCKPIT_DEVICE)
    assert "from the cockpit" in str(e.value) and "'launches'" in str(e.value)
    with pytest.raises(CoordError) as e:
        CoordPhone._spec(None, {"where": [], "launches": []}, "surface:relay")
    assert "from this page" in str(e.value) and "'launches'" not in str(e.value)


@pytest.mark.parametrize("cred, device", [
    ({"class": "token", "name": "browser"}, COCKPIT_DEVICE),
    ({"class": "token", "name": "relay"}, "surface:relay"),
    ({"class": "token", "name": "paired-d-abc"}, "d-abc"),
    ({"class": "token", "name": "paired-"}, None),
    ({"class": "none", "device_class": None}, None),
])
def test_only_the_cockpit_pages_own_token_runs_as_the_cockpit(cred, device):
    """L3 r2 MED: every non-paired surface token ran as "cockpit", so any surface could set the launch grant; and a
    bare paired prefix reached CoordPhone as "". Only ``browser`` is the cockpit; a bare prefix is refused here; a
    paired device runs as its own id, which may_launch takes (row 460) and another surface's never."""
    from levain import web_server

    fake = SimpleNamespace(_credential=lambda *, required=None: cred)
    assert web_server._Handler._coord_device(fake) == device
    if device is not None and not may_launch(device):
        from levain.coordinator.phone import CoordPhone
        from levain.coordinator.store import CoordError

        with pytest.raises(CoordError, match="sets only"):
            CoordPhone._spec(None, {"where": [], "launches": []}, device)


# L3 r1 #1: the device a cockpit coord step runs as comes from the request's AUTHENTICATED credential. The /cockpit/coord/*
# gate accepts a paired device's token as well as the cockpit's surface token; since row 460 (Phill 2026-11-08) both may
# set the grant their passkey signs, and the listing tells each credential which it is.
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


def test_a_paired_phone_token_and_the_cockpit_token_both_set_launches_on_the_cockpit_route(world, served_sheet):
    port, sheet = served_sheet
    device, paired = _pair(port)
    assert device.startswith("d-") and device != COCKPIT_DEVICE
    # the listing and the review agree, per credential: the page draws the toggle exactly where it is taken
    for token in (paired, T):
        st, got, _ = _call(port, "GET", "/cockpit/coord/drops.json", token=token)
        assert st == 200 and got["coord_may_launch"] is True, (st, got)
    st, out, _ = _post_review(world, port, sheet, paired, "ph-" + "a" * 32, _launch_spec(world))
    assert st == 200, out
    assert env.parse_body(out["body_text"])["launches"] == _launch_spec(world)["launches"]
    # the same body with the cockpit's own surface token is taken, and the body it builds carries the grant
    st, out, _ = _post_review(world, port, sheet, T, "ph-" + "b" * 32, _launch_spec(world))
    assert st == 200, out
    assert env.parse_body(out["body_text"])["launches"] == _launch_spec(world)["launches"]
    # and a paired phone is still served for everything it may set (the device it runs as is its own id)
    no_launch = {k: v for k, v in _launch_spec(world).items() if k != "launches"}
    st, out, _ = _post_review(world, port, sheet, paired, "ph-" + "c" * 32, no_launch)
    assert st == 200 and "launches" not in env.parse_body(out["body_text"]), out
    assert sheet.phone._reviews[out["review_id"]]["device"] == device
