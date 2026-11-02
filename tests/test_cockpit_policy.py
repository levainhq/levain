"""NP S2: the cockpit's Notifications block. GET /webpush/policy, POST /webpush/policy/set and /reset ride the web push
family's gate (same-origin, a surface token on every bind, JSON only) and call presence.set_policy / reset_policy as the
operator's own act; the Devices panel draws the block."""
from __future__ import annotations

import fcntl
import json
import time

import pytest

from levain.webpush import presence
from levain import web_server
from levain.web_server import load_web_asset
from tests.test_cockpit_pairing_http import PROXY, T, _call
from tests.test_cockpit_webpush import _signed
from tests.test_cockpit_k2a import _install
from tests.test_pair_card_w1b import _drive, needs_browser

SET = "/webpush/policy/set"
SEG_LOOK = ("(r => { const look = b => { const c = getComputedStyle(b); return { bg: c.backgroundColor, color: c.color }; };"
            " return { pressed: look(r.querySelector('button[aria-pressed=\"true\"]')),"
            " unpressed: look(r.querySelector('button[aria-pressed=\"false\"]')) }; })(document.querySelector('.np-row'))")


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


# Re-baselined (sleep default 10-25): the written cell is sleep.approval hold, a real change from the new push default.
def test_an_authorised_write_round_trips_and_reset_restores_the_default(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        st, got, _ = _call(port, "POST", SET, _cell(v="hold"), token=T)
        assert st == 200 and got["cells"]["sleep"]["approval"] == "hold" and got["status_kind"] == "loaded"
        assert _call(port, "GET", "/webpush/policy", token=T)[1]["cells"]["sleep"]["approval"] == "hold"
        assert presence.read_policy(q)[0]["sleep"]["approval"] == "hold"
        log = q / "webpush" / presence.LOG_NAME
        assert log.exists(), "the write left no log row"
        rows = [json.loads(l) for l in log.read_text().splitlines()]
        row = rows[-1]
        assert row["word"] == "policy set sleep approval hold" and row["outcome"] == "set"
        assert "by" not in row                              # the operator's own act: no setter identity
        assert row["via"] == "browser"                      # the credential's name, never the secret
        assert T not in log.read_text()
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


def test_a_held_presence_lock_answers_503_within_the_deadline_and_writes_nothing(rig, monkeypatch):
    src, q = rig
    monkeypatch.setattr(web_server, "_POLICY_LOCK_WAIT_SECS", 0.4)
    d = q / "webpush"
    d.mkdir(mode=0o700)
    holder = open(d / presence.LOCK_NAME, "w")
    fcntl.flock(holder, fcntl.LOCK_EX)                      # a stalled holder, from this test
    try:
        with _signed(src, q) as (port, _):
            for path, body in ((SET, _cell()), ("/webpush/policy/reset", {})):
                t0 = time.monotonic()
                st, got, _ = _call(port, "POST", path, body, token=T)
                assert st == 503 and got["error"] == "policy_busy" and "nothing was changed" in got["message"], (st, got)
                assert time.monotonic() - t0 < 5
            assert presence.policy_view(q)["status_kind"] == "default"      # nothing written
            assert not (d / "policy.json").exists()
    finally:
        holder.close()
    with _signed(src, q) as (port, _):                       # the lock freed: the same write goes through
        assert _call(port, "POST", SET, _cell(), token=T)[0] == 200


def test_a_failing_close_is_the_malformed_default_never_a_500_after_a_write(rig, monkeypatch):
    src, q = rig
    armed = {"on": False}
    policy_fds: set = set()
    real_os = presence.os

    class _FailingClose:                                    # presence's own `os` only: no other module's close is touched
        def __getattr__(self, name):
            return getattr(real_os, name)

        def open(self, path, *a, **k):
            fd = real_os.open(path, *a, **k)
            if str(path).endswith(presence.POLICY_NAME):
                policy_fds.add(fd)
            return fd

        def close(self, fd):
            real_os.close(fd)
            if armed["on"] and fd in policy_fds:            # only the policy file's own descriptor fails to close
                policy_fds.discard(fd)
                raise OSError(5, "close failed")
            policy_fds.discard(fd)
    monkeypatch.setattr(presence, "os", _FailingClose())
    with _signed(src, q) as (port, _):
        armed["on"] = True
        st, got, _ = _call(port, "POST", SET, _cell("quiet", "approval", "hold"), token=T)
        assert st == 200, (st, got)                         # the write went through; the answer is a view, not a 500
        assert got["status_kind"] == "malformed" and any("OSError" in r for r in got["reasons"]), got
        armed["on"] = False
        assert presence.policy_view(q)["status_kind"] == "loaded"      # the file is intact
        armed["on"] = True
        st, got, _ = _call(port, "GET", "/webpush/policy", token=T)
        assert st == 200 and got["status_kind"] == "malformed"


def test_the_view_names_which_cells_hold_by_default(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        v = _call(port, "GET", "/webpush/policy", token=T)[1]
        assert v["default_cells"] == presence.DEFAULT_CELLS and v["held_by_default"] == {}   # re-baselined: no approval cell holds by default now
        # an explicit table of the operator's own, sleep approvals held: his hold, not the default's, so still nothing BY DEFAULT
        assert _call(port, "POST", SET, _cell("sleep", "approval", "hold"), token=T)[0] == 200
        v = _call(port, "GET", "/webpush/policy", token=T)[1]
        assert v["status_kind"] == "loaded" and v["cells"]["sleep"]["approval"] == "hold" and v["held_by_default"] == {}


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
    state = "JSON.stringify([...document.querySelectorAll('.np-row')].map(r=>[r.dataset.cell,r.querySelector('[aria-pressed=true]').dataset.value]))"
    with _signed(src, q) as (port, _):
        _pair(port)
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": "document.querySelector('.np-panel') && !document.querySelector('.np-panel').hidden", "ms": 20000},
            {"eval": "document.querySelectorAll('.np-warn:not([hidden])').length"},
            {"eval": "(d=>d.hidden?'hidden':d.textContent+'|'+getComputedStyle(d).color)(document.querySelector('.np-default'))"},
            {"do": cell("quiet.approval", "hold") + ".click()", "until": cell("quiet.approval", "hold") + ".getAttribute('aria-pressed')==='true'", "ms": 8000},
            {"eval": "document.querySelectorAll('.np-warn:not([hidden])').length"},
            {"eval": "location.reload() || 1"},
            {"until": "document.querySelector('.np-panel') && !document.querySelector('.np-panel').hidden", "ms": 20000},
            {"eval": state},
            {"do": "document.querySelector('.np-reset').click()", "until": "document.querySelector('.np-reset').disabled && document.querySelectorAll('.np-warn:not([hidden])').length === 0 && " + cell("quiet.approval", "push") + ".getAttribute('aria-pressed')==='true'", "ms": 8000},
            {"eval": "document.documentElement.scrollWidth - document.documentElement.clientWidth"},
            {"eval": SEG_LOOK},
            {"scheme": "dark"},
            {"eval": SEG_LOOK},
        ])
    v = out["values"]
    for look in (v[-2], v[-1]):          # light, then dark: the pressed segment LOOKS different from the unpressed one
        assert look["pressed"]["bg"] != look["unpressed"]["bg"], look
        assert look["pressed"]["color"] != look["unpressed"]["color"], look
    assert v[1] == 0                    # the shipped default shows no warning ...
    assert v[2] == "hidden"             # ... and (re-baselined) no default line either: no approval cell holds by default now
    assert v[4] == 1                    # a hold in a non-default cell (quiet approvals) warns
    assert ["quiet.approval", "hold"] in json.loads(v[7])   # it held across a reload
    assert presence.policy_view(q)["status_kind"] == "default"   # reset


@needs_browser
def test_the_block_shows_when_the_device_list_is_gone_and_the_policy_is_not(rig, tmp_path, monkeypatch):
    src, q = rig
    # a cockpit whose pairing list answers 404 while /webpush/policy answers 200 keeps the Notifications block
    monkeypatch.setattr(web_server._Handler, "_serve_pair_list",
                        lambda self, *, head: self._pair_send({"error": "not_found"}, 404, head=head))
    with _signed(src, q) as (port, _):
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": "document.querySelector('.np-panel') && !document.querySelector('.np-panel').hidden", "ms": 20000},
            {"eval": "(s=>s && !s.hidden)(document.getElementById('presence-settings'))"},
            {"eval": "[...document.querySelectorAll('.dev-panel')].map(p=>p.hidden)"},
        ])
    assert out["values"][1] is True and out["values"][2] == [True, False, False]   # the settings section shows; the pairing panel does not, the tap tokens and Notifications do


@needs_browser
def test_a_slow_read_started_before_a_write_does_not_repaint_over_it(rig, tmp_path, monkeypatch):
    src, q = rig
    from tests.test_cockpit_pairing_http import _pair
    cell = lambda c, v: f"document.querySelector('.np-row[data-cell=\"{c}\"] button[data-value=\"{v}\"]')"
    # Installed by the driver's init step, before any page script runs. It counts the page's policy GETs: the SECOND one
    # (the page's own re-read after load) is held, its body already fixed at the state when it began, until the test
    # releases it; every other policy GET passes and bumps __fin when it resolves. Nothing here waits on a clock.
    wrap = ("(() => { const f = window.fetch; let n = 0; window.__held = 0; window.__fin = 0;"
            " const gate = new Promise((ok) => { window.__release = ok; });"
            " window.fetch = function (u, o) {"
            " const p = f.apply(this, arguments);"
            " if (!String(u).includes('/webpush/policy') || (o && o.method === 'POST')) return p;"
            " if (++n === 2) { window.__held = 1; return p.then((r) => gate.then(() => r)); }"
            " return p.then((r) => { window.__fin++; return r; }); }; })()")
    with _signed(src, q) as (port, _):
        _pair(port)
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"init": wrap},
            {"until": "document.querySelector('.np-panel') && !document.querySelector('.np-panel').hidden", "ms": 20000},
            {"until": "window.__held === 1", "ms": 20000},                     # the stale read is in flight, held
            {"do": cell("quiet.approval", "hold") + ".click()", "until": cell("quiet.approval", "hold") + ".getAttribute('aria-pressed')==='true' && window.__fin >= 2 && ![...document.querySelectorAll('.np-row button')].some(b => b.disabled)", "ms": 20000},
            {"eval": "window.__release() || 1"},                                  # now the stale body arrives, after the write and its follow-up GET
            {"eval": "new Promise((ok) => setTimeout(() => ok(" + cell("quiet.approval", "hold") + ".getAttribute('aria-pressed')), 600))"},
            {"eval": "[...document.querySelectorAll('.np-row button')].some(b => b.disabled)"},
        ])
    assert out["values"][4] == "true"       # the late stale read (default: push) did not take the written value back
    assert out["values"][5] is False        # the buttons are enabled again


@needs_browser
def test_a_custom_table_holding_sleep_approvals_shows_no_default_line(rig, tmp_path, monkeypatch):
    src, q = rig
    from tests.test_cockpit_pairing_http import _pair
    with _signed(src, q) as (port, _):
        _pair(port)
        assert _call(port, "POST", SET, _cell("quiet", "other", "push"), token=T)[0] == 200   # an explicit table of his own
        assert _call(port, "POST", SET, _cell("sleep", "approval", "hold"), token=T)[0] == 200
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": "document.querySelector('.np-panel') && !document.querySelector('.np-panel').hidden", "ms": 20000},
            {"eval": "document.querySelector('.np-default').hidden"},
            {"eval": "document.querySelectorAll('.np-warn:not([hidden])').length"},
        ])
    # re-baselined: sleep approvals held is now a non-default hold, so the warning shows; the default line stays hidden
    assert out["values"][1] is True and out["values"][2] == 1
