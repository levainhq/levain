"""NP S2: the cockpit's Notifications block. GET /webpush/policy, POST /webpush/policy/set and /reset ride the web push
family's gate (same-origin, a surface token on every bind, JSON only) and call presence.set_policy / reset_policy as the
operator's own act; the Devices panel draws the block."""
from __future__ import annotations

import json

import pytest

from levain.webpush import presence
from levain.web_server import load_web_asset
from tests.test_cockpit_pairing_http import PROXY, T, _call
from tests.test_cockpit_webpush import _signed
from tests.test_cockpit_k2a import _install
from tests.test_pair_card_w1b import _drive, needs_browser

SET = "/webpush/policy/set"


@pytest.fixture
def rig(tmp_path):
    q = tmp_path / "signq"
    q.mkdir(mode=0o700)
    return _install(tmp_path)[1], q


def _cell(m="sleep", c="approval", v="push"):
    return {"mute": m, "class": c, "value": v}


def test_read_is_the_policy_view_and_needs_the_token(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        for hdrs in (None, PROXY):
            assert _call(port, "GET", "/webpush/policy", headers=hdrs)[0] == 403
        assert _call(port, "GET", "/webpush/policy", token=T, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
        st, got, _ = _call(port, "GET", "/webpush/policy", token=T)
        assert st == 200 and got == presence.policy_view(q) and got["status_kind"] == "default"
        assert got["sentences"]["sleep"].startswith("In sleep:")


def test_no_signer_serves_no_policy(rig):
    from tests.test_cockpit_pairing_http import _serve
    src, _ = rig
    with _serve(src) as (port, _):
        assert _call(port, "GET", "/webpush/policy", token=T)[0] == 404
        assert _call(port, "POST", SET, _cell(), token=T)[0] == 404


def test_an_authorised_write_round_trips_and_reset_restores_the_default(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        st, got, _ = _call(port, "POST", SET, _cell(), token=T)
        assert st == 200 and got["cells"]["sleep"]["approval"] == "push" and got["status_kind"] == "loaded"
        assert _call(port, "GET", "/webpush/policy", token=T)[1]["cells"]["sleep"]["approval"] == "push"
        assert presence.read_policy(q)[0]["sleep"]["approval"] == "push"
        rows = [json.loads(l) for l in (q / "webpush" / "presence.log").read_text().splitlines()] if (q / "webpush" / "presence.log").exists() else []
        assert not any(r.get("by") for r in rows)           # the operator's own act: no setter identity
        st, got, _ = _call(port, "POST", "/webpush/policy/reset", {}, token=T)
        assert st == 200 and got["status_kind"] == "default" and got["cells"] == presence.DEFAULT_CELLS


def test_an_unauthorised_write_is_refused_and_changes_nothing(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        for hdrs in (None, PROXY, {"Sec-Fetch-Site": "cross-site"}):
            assert _call(port, "POST", SET, _cell(), headers=hdrs)[0] in (403, 400)
        assert _call(port, "POST", SET, _cell(), token="wrong")[0] == 403
        assert _call(port, "POST", "/webpush/policy/reset", {}, token="wrong")[0] == 403
        assert _call(port, "POST", SET, _cell(), token=T, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
        assert presence.policy_view(q)["status_kind"] == "default"


@pytest.mark.parametrize("body", [{}, {"mute": "sleep"}, {**_cell(), "extra": "x"}, _cell("nap"), _cell(c="loud"),
                                  _cell(v="maybe"), {"mute": 1, "class": "approval", "value": "push"}, [1], "x"])
def test_a_malformed_body_is_refused_and_changes_nothing(rig, body):
    src, q = rig
    with _signed(src, q) as (port, _):
        st, got, _ = _call(port, "POST", SET, body, token=T)
        assert st in (400, 422) and got["error"] in ("bad_request", "policy_refused"), got
        assert presence.policy_view(q)["status_kind"] == "default"
        assert _call(port, "POST", "/webpush/policy/reset", {"x": 1}, token=T)[0] == 400


def test_a_lock_failure_is_surfaced_as_an_error_the_block_shows(rig, monkeypatch):
    src, q = rig
    def boom(*a, **k):
        raise presence.PresenceError("presence lock not taken: disk")
    monkeypatch.setattr(presence, "set_policy", boom)
    with _signed(src, q) as (port, _):
        st, got, _ = _call(port, "POST", SET, _cell(), token=T)
        assert st == 422 and got["error"] == "policy_refused" and "lock not taken" in got["message"]


def test_a_malformed_file_is_reported_by_the_read(rig):
    src, q = rig
    (q / "webpush").mkdir(mode=0o700)
    (q / "webpush" / "policy.json").write_text("{nope")
    with _signed(src, q) as (port, _):
        got = _call(port, "GET", "/webpush/policy", token=T)[1]
        assert got["status_kind"] == "malformed" and got["reasons"] and got["cells"] == presence.DEFAULT_CELLS


def test_the_panel_script_carries_the_block_and_its_loud_warning():
    js = load_web_asset("devices_panel.js")[0].decode() if isinstance(load_web_asset("devices_panel.js"), tuple) else load_web_asset("devices_panel.js")
    for needle in ("Notifications", "/webpush/policy", "/webpush/policy/set", "/webpush/policy/reset", "Reset to default",
                   "Seats that need your approval will wait silently until you unmute."):
        assert needle in js, needle


@needs_browser
def test_the_block_renders_edits_round_trip_and_the_approval_hold_warns(rig, tmp_path, monkeypatch):
    src, q = rig
    from tests.test_cockpit_pairing_http import _pair
    cell = lambda c, v: f"document.querySelector('.np-row[data-cell=\"{c}\"] button[data-value=\"{v}\"]')"
    state = "JSON.stringify([...document.querySelectorAll('.np-row')].map(r=>[r.dataset.cell,r.querySelector('[aria-checked=true]').dataset.value]))"
    with _signed(src, q) as (port, _):
        _pair(port)
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": "document.querySelector('.np-panel') && !document.querySelector('.np-panel').hidden", "ms": 20000},
            {"eval": "document.querySelectorAll('.np-warn:not([hidden])').length"},
            {"do": cell("quiet.approval", "hold") + ".click()", "until": cell("quiet.approval", "hold") + ".getAttribute('aria-checked')==='true'", "ms": 8000},
            {"eval": "document.querySelectorAll('.np-warn:not([hidden])').length"},
            {"eval": "location.reload() || 1"},
            {"until": "document.querySelector('.np-panel') && !document.querySelector('.np-panel').hidden", "ms": 20000},
            {"eval": state},
            {"do": "document.querySelector('.np-reset').click()", "until": "document.querySelector('.np-reset').disabled && document.querySelectorAll('.np-warn:not([hidden])').length === 1 && " + cell("quiet.approval", "push") + ".getAttribute('aria-checked')==='true'", "ms": 8000},
            {"eval": "document.documentElement.scrollWidth - document.documentElement.clientWidth"},
        ])
    v = out["values"]
    assert v[1] == 1 and v[3] == 2      # the shipped default already holds sleep approvals (one warning); holding quiet's adds a second
    assert ["quiet.approval", "hold"] in json.loads(v[6])   # it held across a reload
    assert presence.policy_view(q)["status_kind"] == "default"   # reset
