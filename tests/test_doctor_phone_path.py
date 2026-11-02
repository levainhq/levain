"""The doctor's ``phone path`` row: whether a phone can reach the cockpit through Tailscale, read-only."""
from __future__ import annotations

import json
from types import SimpleNamespace

from levain import doctor

PORT = 7420


def _run(status, serve):
    def run(argv, **kw):
        out = status if argv[1] == "status" else serve
        return SimpleNamespace(returncode=0, stdout=json.dumps(out))
    return run


def _row(status=None, serve=None, which=lambda n: "/bin/tailscale"):
    (r,) = doctor._check_phone_path(which=which, run=_run(status, serve), port=PORT)
    return r


def test_not_installed_says_a_phone_cannot_reach_the_laptop():
    r = _row(which=lambda n: None)
    assert "tailscale not found" in r.detail and "cannot reach" in r.detail


def test_logged_out_says_a_phone_cannot_reach_the_laptop():
    r = _row({"BackendState": "NeedsLogin"})
    assert "cannot reach" in r.detail and "NeedsLogin" in r.detail and r.warn


def test_serving_the_cockpit_port_is_ok():
    serve = {"Web": {"m.t.ts.net:443": {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{PORT}"}}}}}
    r = _row({"BackendState": "Running", "Self": {"DNSName": "m.t.ts.net."}}, serve)
    assert r.ok and not r.warn and "https://m.t.ts.net" in r.detail


def test_funnel_on_fails_plainly():
    serve = {"AllowFunnel": {"m.t.ts.net:443": True}}
    r = _row({"BackendState": "Running"}, serve)
    assert not r.ok and "open to the internet" in r.detail
