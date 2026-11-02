"""The notification policy is RETIRED (presence 1b). GET /webpush/policy still answers (the retirement view: what each preset
lets through, derived) so an older page paints something true, and POST /webpush/policy/set and /reset still ride the push
family's gate but write nothing: a 422 with the pointer to ``levain ping lets``. The cockpit no longer draws the block."""
from __future__ import annotations

import pytest

from levain.web_server import load_web_asset
from levain.webpush import intent, presence
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import PROXY, T, _call
from tests.test_cockpit_webpush import _signed

SET = "/webpush/policy/set"
RESET = "/webpush/policy/reset"


@pytest.fixture
def rig(tmp_path):
    q = tmp_path / "signq"
    q.mkdir(mode=0o700)
    return _install(tmp_path)[1], q


def _cell(m="sleep", c="approval", v="push"):
    return {"mute": m, "class": c, "value": v}


def test_read_is_the_retirement_view_and_needs_the_token(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        for hdrs in (None, PROXY):
            assert _call(port, "GET", "/webpush/policy", headers=hdrs)[0] == 403
        assert _call(port, "GET", "/webpush/policy", token=T, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
        st, got, _ = _call(port, "GET", "/webpush/policy", token=T)
        assert st == 200 and got == presence.policy_view(q) and got["retired"] is True and got["status_kind"] == "retired"
        # derived from the presets, so what an older reader paints is true: sleep lets approvals and attention through, holds the rest
        assert got["cells"]["sleep"] == {"approval": "push", "attention": "push", "other": "hold"}
        assert got["presets"]["sleep"]["lets_through"] == intent.PRESET_TABLE["sleep"]["lets_through"]


def test_no_signer_serves_no_policy(rig):
    from tests.test_cockpit_pairing_http import _serve
    src, _ = rig
    with _serve(src) as (port, _):
        assert _call(port, "GET", "/webpush/policy", token=T)[0] == 404
        assert _call(port, "POST", SET, _cell(), token=T)[0] == 404


@pytest.mark.parametrize("path,body", [(SET, _cell()), (RESET, {}), (SET, {}), (SET, [1])])
def test_a_policy_write_is_refused_with_the_pointer_and_writes_nothing(rig, path, body):
    src, q = rig
    with _signed(src, q) as (port, _):
        st, got, _ = _call(port, "POST", path, body, token=T)
        assert st == 422 and got["error"] == "policy_refused" and "levain ping lets" in got["message"]
        assert not (q / "webpush" / intent.FILE_NAME).exists() and not (q / "webpush" / "policy.json").exists()


def test_an_unauthorised_write_is_refused_before_it_is_read(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        for hdrs in (None, PROXY, {"Sec-Fetch-Site": "cross-site"}):
            assert _call(port, "POST", SET, _cell(), headers=hdrs)[0] in (403, 400)
        assert _call(port, "POST", SET, _cell(), token="wrong")[0] == 403
        assert _call(port, "POST", RESET, {}, token="wrong")[0] == 403
        assert _call(port, "POST", SET, _cell(), token=T, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403


def test_the_panel_script_no_longer_draws_the_policy_block():
    js = load_web_asset("devices_panel.js")
    js = js[0].decode() if isinstance(js, tuple) else js
    for gone in ("buildPolicy", "/webpush/policy", "HOLD_WARN", "Reach me"):
        assert gone not in js, gone
    assert "Lets through" in js and "Standing default" in js
