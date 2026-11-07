"""Band 299: the notification-action device test. The report route (token gate, allowlist, rate limit), the worker's
devtest tap branch (a node rig), the page, and the desk's read command."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from levain.web_server import load_web_asset
from levain.webpush import devtest, routes
from tests.test_cockpit_pairing_http import PROXY, _call, _pair, _serve
from tests.test_cockpit_webpush import _signed, rig  # noqa: F401 (rig is a fixture)

HERE = Path(__file__).resolve().parent
SW = HERE.parent / "levain" / "webpush" / "static" / "sw.js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")
GOOD = {"event": "click", "action": "approve", "state": "none", "maxActions": 0, "ua": "iPhone"}


def _rig(windows: str, devtest_flag: str, action: str) -> dict:
    cp = subprocess.run([NODE, str(HERE / "_sw_devtest_rig.js"), str(SW), windows, devtest_flag, action], capture_output=True, text=True, timeout=60)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return json.loads(cp.stdout)


# --- the report route ---------------------------------------------------------------------------------------------------

def test_the_report_route_needs_the_credential_and_files_one_allowlisted_report(rig) -> None:
    src, qdir = rig
    with _signed(src, qdir) as (port, _):
        for hdrs in (None, PROXY):
            assert _call(port, "POST", "/webpush/devtest", GOOD, headers=hdrs)[0] == 403, hdrs
        assert devtest.events(qdir) == []
        dev, tok = _pair(port)
        st, out, _ = _call(port, "POST", "/webpush/devtest", GOOD, token=tok)
        assert st == 200 and out == {"kept": 1}, out
    [row] = devtest.events(qdir)
    assert row["device"].endswith(dev) and row["action"] == "approve" and row["event"] == "click" and row["maxActions"] == 0
    assert set(row) == {"ts", "at", "device", "event", "action", "state", "maxActions", "ua"}


@pytest.mark.parametrize("bad", [
    {}, {"event": "tap"}, {**GOOD, "extra": 1}, {**GOOD, "action": "x" * 21}, {**GOOD, "state": "y" * 21}, {**GOOD, "ua": "u" * 81},
    {**GOOD, "action": 5}, {**GOOD, "maxActions": "3"}, {**GOOD, "maxActions": True}, {**GOOD, "maxActions": -1},
    {**GOOD, "action": "a\nb"}, {**GOOD, "ua": "\x1b[2J"}, [1], "no",
])
def test_the_report_allowlist_refuses_anything_else(bad) -> None:
    assert devtest.clean(bad) is None


def test_the_allowlist_keeps_a_minimal_report_and_a_null_max_actions() -> None:
    assert devtest.clean({"event": "shown"}) == {"event": "shown", "action": "", "state": "", "maxActions": None, "ua": ""}
    assert devtest.clean({**GOOD, "maxActions": None})["maxActions"] is None


def test_the_route_answers_400_429_and_503(tmp_path: Path, monkeypatch) -> None:
    call = lambda b=GOOD: routes.handle("POST", "/webpush/devtest", {}, json.dumps(b).encode(), queue_dir=tmp_path, authorized=True, device_id="paired-x", journal=None)
    assert call({"event": "nope"})[0] == 400 and routes.handle("POST", "/webpush/devtest", {}, b"{", queue_dir=tmp_path, authorized=True, device_id="p", journal=None)[0] == 400
    assert routes.handle("POST", "/webpush/devtest", {}, b"{}", queue_dir=tmp_path, authorized=False, device_id=None, journal=None)[0] == 403
    for _ in range(devtest.RATE_MAX):
        assert call()[0] == 200
    assert call()[0] == 429                                   # the device's next report inside the minute
    assert routes.handle("POST", "/webpush/devtest", {}, json.dumps(GOOD).encode(), queue_dir=tmp_path, authorized=True, device_id="paired-other", journal=None)[0] == 200
    assert len(devtest.events(tmp_path)) == devtest.RATE_MAX + 1
    monkeypatch.setattr(devtest, "record", lambda *a, **k: (_ for _ in ()).throw(devtest.Busy()))
    assert call()[0] == 503
    monkeypatch.setattr(devtest, "record", lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
    assert call()[0] == 503


def test_only_the_newest_reports_are_kept(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(devtest, "RATE_MAX", 10_000)
    for i in range(devtest.KEEP + 7):
        devtest.record(tmp_path, "d", {**GOOD, "action": f"a{i}"})
    got = devtest.events(tmp_path)
    assert len(got) == devtest.KEEP and got[-1]["action"] == f"a{devtest.KEEP + 6}"


def test_the_desk_read_says_what_happened(tmp_path: Path, capsys) -> None:
    from levain.webpush import cli

    assert devtest.summary([])["verdict"] == "no tap reported yet"
    devtest.record(tmp_path, "d", {"event": "shown", "action": "", "state": "visible", "maxActions": 0, "ua": "x"})
    devtest.record(tmp_path, "d", {**GOOD, "action": "(body tap)"})
    s = devtest.summary(devtest.events(tmp_path))
    assert s["maxActions"] == 0 and "only body taps" in s["verdict"] and len(s["body_taps"]) == 1
    devtest.record(tmp_path, "d", {**GOOD, "action": "deny"})
    assert "ACTION BUTTONS WORKED" in devtest.summary(devtest.events(tmp_path))["verdict"]
    import argparse
    ns = argparse.Namespace(queue_dir=tmp_path, json=True)
    assert cli._devtest(ns) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["summary"]["clicks"] == 2 and len(out["events"]) == 3
    ns.json = False
    cli._devtest(ns)
    assert "-- shown=1 clicks=2" in capsys.readouterr().out
    (tmp_path / "e").mkdir()
    assert cli._devtest(argparse.Namespace(queue_dir=tmp_path / "e", json=False)) == 0
    assert "no device-test report yet" in capsys.readouterr().out


# --- the worker's tap branch --------------------------------------------------------------------------------------------

@needs_node
@pytest.mark.parametrize("windows,state", [("none", "none"), ("hidden", "hidden"), ("visible", "visible")])
def test_a_devtest_tap_closes_traces_the_action_and_state_and_opens_the_page(windows, state) -> None:
    got = _rig(windows, "1", "approve")
    assert got["closed"] is True and got["fetches"] == 0 and got["otherPath"] is False, got   # none of the ping paths ran
    [e] = [t for t in got["trace"] if t["step"] == "devtest-click"]
    assert e["action"] == "approve" and e["state"] == state, got["trace"]
    if windows == "none":
        assert got["opened"] == ["https://box.example/devtest"] and got["navigated"] == []
    else:
        assert got["opened"] == [] and got["navigated"] == ["https://box.example/devtest"] and got["focused"] == 1


@needs_node
def test_a_devtest_body_tap_is_named_and_an_ordinary_ping_tap_never_takes_the_branch() -> None:
    [e] = [t for t in _rig("none", "1", "-")["trace"] if t["step"] == "devtest-click"]
    assert e["action"] == "(body tap)"
    got = _rig("none", "0", "-")
    assert not [t for t in got["trace"] if t["step"] == "devtest-click"], got["trace"]


# --- the page -----------------------------------------------------------------------------------------------------------

def test_the_page_is_served_and_carries_the_controls_and_the_exact_notification_call(rig) -> None:
    src, qdir = rig
    with _serve(src) as (port, _h):
        for path, kind in (("/devtest", "text/html"), ("/devtest.js", "text/javascript")):
            st, _b, hdrs = _call(port, "GET", path)
            assert st == 200 and hdrs["content-type"].startswith(kind), (path, st)
    html, js = load_web_asset("devtest.html"), load_web_asset("devtest.js")
    assert 'id="dt-run10"' in html and 'id="dt-run60"' in html and "Run test" in html
    assert '<script src="/devtest.js"></script>' in html and not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert "sw_open.js" not in html.replace("No sw_open.js here", "")
    for needle in ("Notification.maxActions", "'actions' in Notification.prototype".replace("'", '"'), "Notification.requestPermission", "reg.showNotification",
                   '"approve"', '"deny"', "requireInteraction: false", "devtest: true", 'url: "/devtest"', "/webpush/devtest", "X-Levain-Write-Token", "devtest-click"):
        assert needle in js, needle
    assert "run(10)" in js and "run(60)" in js
