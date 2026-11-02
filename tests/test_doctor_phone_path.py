"""The doctor's ``phone path`` row: whether a phone can reach the cockpit through Tailscale, read-only."""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

from levain import doctor

PORT = 7420


def _run(status, serve):
    def run(argv, **kw):
        out = status if argv[1] == "status" else serve
        if out is _BAD:
            return SimpleNamespace(returncode=1, stdout="")
        return SimpleNamespace(returncode=0, stdout=json.dumps(out))
    return run


_BAD = object()


def _row(status=None, serve=None, which=lambda n: "/bin/tailscale"):
    (r,) = doctor._check_phone_path(which=which, run=_run(status, serve), port=PORT, app="/nonexistent/tailscale")
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


def test_unreadable_serve_status_warns_and_is_never_ok_or_not_serving():
    r = _row({"BackendState": "Running"}, _BAD)
    assert r.warn and "cannot tell whether the cockpit is exposed" in r.detail


def test_a_8443_handler_reports_its_real_port_and_a_foreign_target_is_no_match():
    st = {"BackendState": "Running", "Self": {"DNSName": "m.t.ts.net."}}
    r = _row(st, {"Web": {"m.t.ts.net:8443": {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{PORT}"}}}}})
    assert r.ok and not r.warn and "https://m.t.ts.net:8443" in r.detail
    for bad in ({"/x": {"Proxy": f"http://127.0.0.1:{PORT}"}}, {"/": {"Proxy": f"http://10.0.0.5:{PORT}"}},
                {"/": {"Proxy": f"https://127.0.0.1:{PORT}"}}):
        assert _row(st, {"Web": {"m.t.ts.net:443": {"Handlers": bad}}}).warn


def test_the_tailscale_child_gets_no_secret_environment(monkeypatch):
    monkeypatch.setenv("LEVAIN_TEST_SECRET_TOKEN", "hunter2")
    r = doctor._bounded_tailscale([sys.executable, "-c", "import os,json;print(json.dumps(dict(os.environ)))"])
    assert r.returncode == 0 and "LEVAIN_TEST_SECRET_TOKEN" not in json.loads(r.stdout)


def test_a_proxy_to_a_subpath_or_a_plain_http_listener_is_not_the_cockpit():
    def serve(key, target):
        return {"Web": {key: {"Handlers": {"/": {"Proxy": target}}}}}
    st = {"BackendState": "Running", "Self": {"DNSName": "m.t.ts.net."}}
    assert _row(st, serve("m.t.ts.net:443", f"http://127.0.0.1:{PORT}/foo")).warn
    assert _row(st, serve("m.t.ts.net:80", f"http://127.0.0.1:{PORT}")).warn
    assert not _row(st, serve("m.t.ts.net:443", f"http://127.0.0.1:{PORT}")).warn
