"""Band 299: the notification-action device test. The report route (token gate, allowlist, rate limit), the server-sent test push
(the send route, its limits, the one-device addressing, nothing real sent in a test run), the worker's push and tap branches (a
node rig), the page's drain and run logic (a node rig), and the desk's read command."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from levain.web_server import load_web_asset
from levain.webpush import devtest, routes, sender
from tests.test_cockpit_pairing_http import PROXY, _call, _pair, _serve
from tests.test_cockpit_webpush import _signed, rig  # noqa: F401 (rig is a fixture)

HERE = Path(__file__).resolve().parent
SW = HERE.parent / "levain" / "webpush" / "static" / "sw.js"
PAGE = HERE.parent / "levain" / "templates" / "web" / "devtest.js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")
GOOD = {"event": "click", "action": "approve", "state": "none", "maxActions": 0, "ua": "iPhone"}


@pytest.fixture(autouse=True)
def _fresh_send_state(monkeypatch):
    """No test sends a real push: the send state starts empty and a real timer or real subscription lookup is never used."""
    devtest._pending.clear()
    devtest._asked.clear()
    monkeypatch.setattr(sender, "_post", lambda req: (_ for _ in ()).throw(AssertionError("a real push was attempted in a test")))
    yield
    devtest._pending.clear()
    devtest._asked.clear()


class _Timers:
    """The server's timer seam: remembers each (delay, callback) and runs it only when the test says."""

    def __init__(self) -> None:
        self.jobs: list[tuple[float, object]] = []

    def __call__(self, delay, fn):
        self.jobs.append((delay, fn))
        return object()

    def fire(self, i: int = -1) -> None:
        self.jobs[i][1]()


@pytest.fixture
def timers(monkeypatch) -> _Timers:
    t = _Timers()
    monkeypatch.setattr(devtest, "_timer", t)
    monkeypatch.setattr(devtest, "_live", lambda q, d: True)
    return t


def _rig(windows: str, devtest_flag: str, action: str, mode: str = "click", held: str = "actions2") -> dict:
    cp = subprocess.run([NODE, str(HERE / "_sw_devtest_rig.js"), str(SW), windows, devtest_flag, action, mode, held], capture_output=True, text=True, timeout=60)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return json.loads(cp.stdout)


def _page(**scenario) -> dict:
    cp = subprocess.run([NODE, str(HERE / "_page_devtest_rig.js"), str(PAGE), json.dumps(scenario)], capture_output=True, text=True, timeout=60)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return json.loads(cp.stdout)


def _ring(*steps: tuple[str, str, str]) -> dict:
    """Trace-ring entries keyed like the worker's: (step, action, state) in time order."""
    return {f"/__levain_trace/{1000 + i:013d}-{i:04d}": {"t": 1000 + i, "src": "sw", "step": st, "action": a, "state": s}
            for i, (st, a, s) in enumerate(steps)}


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
    {**GOOD, "action": "a\nb"}, {**GOOD, "ua": "\x1b[2J"}, [1], "no", {"event": []}, {"event": {}}, {"event": [1]},
])
def test_the_report_allowlist_refuses_anything_else(bad) -> None:
    assert devtest.clean(bad) is None   # an unhashable event is a refusal, never a TypeError (a 500 from the route)


def test_the_allowlist_keeps_a_minimal_report_and_a_null_max_actions() -> None:
    assert devtest.clean({"event": "shown"}) == {"event": "shown", "action": "", "state": "", "maxActions": None, "ua": ""}
    assert devtest.clean({**GOOD, "maxActions": None})["maxActions"] is None


def test_the_route_answers_400_429_and_503(tmp_path: Path, monkeypatch) -> None:
    call = lambda b=GOOD: routes.handle("POST", "/webpush/devtest", {}, json.dumps(b).encode(), queue_dir=tmp_path, authorized=True, device_id="paired-x", journal=None)
    assert call({"event": "nope"})[0] == 400 and routes.handle("POST", "/webpush/devtest", {}, b"{", queue_dir=tmp_path, authorized=True, device_id="p", journal=None)[0] == 400
    assert call({"event": []})[0] == 400
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


def test_the_rate_limit_counts_this_devices_own_rows_whatever_others_posted(tmp_path: Path) -> None:
    for _ in range(devtest.RATE_MAX):
        devtest.record(tmp_path, "paired-a", GOOD)
    for other in ("paired-b", "paired-c", "paired-d"):          # 36 newer rows from other devices push a's rows out of any short tail
        for _ in range(devtest.RATE_MAX):
            devtest.record(tmp_path, other, GOOD)
    with pytest.raises(devtest.RateLimited):
        devtest.record(tmp_path, "paired-a", GOOD)


def test_a_malformed_retained_row_never_breaks_the_rate_check(tmp_path: Path) -> None:
    (tmp_path / devtest.FILE).write_text('{"device":"paired-a","ts":"bad"}\n{"device":"paired-a","ts":[1]}\nnot json\n[1]\n', encoding="utf-8")
    assert devtest.record(tmp_path, "paired-a", GOOD) == 5


def test_only_the_newest_reports_are_kept(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(devtest, "RATE_MAX", 10_000)
    for i in range(devtest.KEEP + 7):
        devtest.record(tmp_path, "d", {**GOOD, "action": f"a{i}"})
    got = devtest.events(tmp_path)
    assert len(got) == devtest.KEEP and got[-1]["action"] == f"a{devtest.KEEP + 6}"


def test_the_desk_read_says_what_happened(tmp_path: Path, capsys) -> None:
    from levain.webpush import cli

    assert devtest.summary([])["verdict"] == "no tap reported yet"
    devtest.record(tmp_path, "d", {"event": "shown", "action": "actions=2", "state": "visible", "maxActions": 0, "ua": "x"})
    devtest.record(tmp_path, "d", {**GOOD, "action": "(body tap)"})
    s = devtest.summary(devtest.events(tmp_path))
    assert s["maxActions"] == 0 and "only body taps" in s["verdict"] and len(s["body_taps"]) == 1 and s["held"] == ["actions=2"]
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


def test_the_summary_reads_max_actions_from_a_click_when_no_shown_report_arrived() -> None:
    assert devtest.summary([{"event": "click", "action": "approve", "maxActions": 3}])["maxActions"] == 3


# --- the server-sent test push ------------------------------------------------------------------------------------------

def test_the_send_route_needs_the_credential_and_a_paired_device_with_pings_on(rig, timers, monkeypatch) -> None:
    src, qdir = rig
    with _signed(src, qdir) as (port, _):
        for hdrs in (None, PROXY):
            assert _call(port, "POST", "/webpush/devtest/send", {"delay_s": 10}, headers=hdrs)[0] == 403, hdrs
        _dev, tok = _pair(port)
        monkeypatch.setattr(devtest, "_live", lambda q, d: False)           # paired, but no live subscription
        st, out, _ = _call(port, "POST", "/webpush/devtest/send", {"delay_s": 10}, token=tok)
        assert st == 403 and out["error"] == "no_device", out
        assert timers.jobs == []
        monkeypatch.setattr(devtest, "_live", lambda q, d: True)
        st, out, _ = _call(port, "POST", "/webpush/devtest/send", {"delay_s": 10}, token=tok)
        assert st == 200 and out == {"delay_s": 10, "pending": True}, out
        assert [d for d, _f in timers.jobs] == [10]                         # the push waits its delay on the server
    assert [r["event"] for r in devtest.events(qdir)] == ["requested"]


@pytest.mark.parametrize("bad", [{}, {"delay_s": 5}, {"delay_s": "10"}, {"delay_s": True}, {"delay_s": 10.0}, {"delay_s": 10, "device": "x"}, [10], "10"])
def test_the_send_route_takes_only_a_delay_of_10_or_60(tmp_path: Path, timers, bad) -> None:
    assert devtest.clean_send(bad) is None
    r = routes.handle("POST", "/webpush/devtest/send", {}, json.dumps(bad).encode(), queue_dir=tmp_path, authorized=True, device_id="paired-x", journal=None)
    assert r[0] == 400 and timers.jobs == []
    assert devtest.clean_send({"delay_s": 60}) == 60


def _send(tmp_path: Path, device: str = "paired-x", delay: int = 10):
    return routes.handle("POST", "/webpush/devtest/send", {}, json.dumps({"delay_s": delay}).encode(), queue_dir=tmp_path, authorized=True, device_id=device, journal=None)


def test_one_pending_push_per_device_and_six_an_hour(tmp_path: Path, timers, monkeypatch) -> None:
    monkeypatch.setattr(sender, "send", lambda *a, **k: [(1, 201)])
    assert _send(tmp_path)[0] == 200
    assert _send(tmp_path)[0] == 409                      # still waiting its delay
    assert _send(tmp_path, "paired-y")[0] == 200          # another device is its own count
    timers.fire(0)                                         # x's push goes out; x is free again
    for _ in range(devtest.SEND_MAX - 1):
        assert _send(tmp_path)[0] == 200
        timers.fire()
    assert _send(tmp_path)[0] == 429                      # the seventh inside the hour
    assert _send(tmp_path, "paired-y")[0] == 409          # y is pending, a different refusal than x's limit
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + devtest.SEND_WINDOW + 5)
    assert _send(tmp_path)[0] == 200                      # the hour passed


def test_the_delayed_push_goes_to_the_requesting_device_only_with_the_marker_and_actions(tmp_path: Path, timers, monkeypatch) -> None:
    sent = []
    monkeypatch.setattr(sender, "send", lambda qdir, **kw: sent.append((qdir, kw)) or [(7, 201)])
    assert _send(tmp_path, "paired-x")[0] == 200 and sent == []   # nothing goes before the delay
    timers.fire()
    [(qdir, kw)] = sent
    assert kw["device"] == "paired-x" and kw["kind"] == "devtest" and kw["extra"]["devtest"] is True
    assert [a["action"] for a in kw["extra"]["actions"]] == ["approve", "deny"]
    assert kw["ref"] is None and kw["url"] == "/devtest"
    rows = devtest.events(tmp_path)
    assert [(r["event"], r["state"]) for r in rows] == [("requested", "delay=10s"), ("pushed", "201")]
    assert _send(tmp_path, "paired-x")[0] == 200            # the pending slot was freed


def test_a_test_run_sends_nothing_real_unless_the_test_replaced_the_sender(tmp_path: Path, timers) -> None:
    assert _send(tmp_path)[0] == 200
    timers.fire()      # the autouse fixture makes a real network post raise; the real sender is also refused by its own guard
    assert [r["state"] for r in devtest.events(tmp_path) if r["event"] == "pushed"] == ["blocked: test run"]


def test_a_failed_send_is_recorded_and_frees_the_slot(tmp_path: Path, timers, monkeypatch) -> None:
    monkeypatch.setattr(sender, "send", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert _send(tmp_path)[0] == 200
    timers.fire()
    assert [r["state"] for r in devtest.events(tmp_path) if r["event"] == "pushed"] == ["error RuntimeError"]
    assert _send(tmp_path)[0] == 200


def test_the_extra_members_ride_in_the_push_payload_and_skip_the_declarative_form(tmp_path: Path, monkeypatch) -> None:
    from levain.webpush.store import PushStore

    seen = []
    monkeypatch.setattr(PushStore, "live_subscriptions", lambda self, *, device=None: [{"id": 1, "endpoint": "e", "p256dh": "p", "auth": "a"}])
    monkeypatch.setattr(PushStore, "add_targets", lambda *a, **k: None)
    monkeypatch.setattr(PushStore, "add_delivery", lambda *a, **k: None)
    monkeypatch.setattr(sender, "_prepare", lambda q, sub, data, topic: seen.append(json.loads(data)) or sender._TRANSIENT)
    sender.send(tmp_path, kind="devtest", ref=None, title="t", body="b", url="/devtest", device="paired-x", extra=devtest.EXTRA)
    [payload] = seen
    assert payload["devtest"] is True and [a["action"] for a in payload["actions"]] == ["approve", "deny"] and payload["title"] == "t"
    plain = json.dumps({"title": "t", "body": "b", "url": "https://box.example/x", "tag": "x", "ping": 1}).encode()
    assert "web_push" in json.loads(sender.declarative_payload(plain))               # an ordinary absolute-url push is declarative
    marked = json.dumps({"title": "t", "body": "b", "url": "https://box.example/x", "tag": "x", "ping": 1, "devtest": True}).encode()
    assert sender.declarative_payload(marked) == marked                              # the test never is: a declarative push has no buttons
    seen.clear()
    sender.send(tmp_path, kind="test", ref=None, title="t", body="b", device="paired-x")
    assert "devtest" not in seen[0] and "actions" not in seen[0]                     # an ordinary ping is untouched


# --- the worker ---------------------------------------------------------------------------------------------------------

@needs_node
@pytest.mark.parametrize("held,said", [("actions2", "actions=2"), ("absent", "actions=absent"), ("none", "not-listed"), ("showfail", "show-failed")])
def test_the_push_is_shown_with_action_buttons_and_traced(held, said) -> None:
    got = _rig("hidden", "0", json.dumps({"devtest": True, "title": "x", "tag": "devtest", "ping": 5}), mode="push", held=held)
    [call] = got["shownCalls"]
    assert [a["action"] for a in call["o"]["actions"]] == ["approve", "deny"] and call["o"]["data"] == {"devtest": True, "url": "/devtest"}
    [e] = [t for t in got["trace"] if t["step"] == "devtest-shown"]
    assert e["action"] == said and e["state"] == "hidden", got["trace"]
    assert got["fetches"] == 0 and got["otherPath"] is False    # no receipts, no pending-open entries: the product paths did not run


@needs_node
def test_an_ordinary_push_never_takes_the_devtest_branch() -> None:
    for payload in ({"title": "x", "tag": "t", "ping": 5}, {"title": "x", "ping": 5, "devtest": "true"}):
        got = _rig("none", "0", json.dumps(payload), mode="push")
        assert not [t for t in got["trace"] if t["step"] == "devtest-shown"], got["trace"]
        assert all("actions" not in c["o"] for c in got["shownCalls"]), got["shownCalls"]


@needs_node
def test_the_active_worker_answers_the_version_question() -> None:
    got = _rig("none", "0", "devtest-version", mode="version")
    assert got["version"]["devtest"] is True and got["version"]["version"] == re.search(r"SW_VERSION = '([^']+)'", SW.read_text()).group(1)


@needs_node
@pytest.mark.parametrize("windows,state", [("none", "none"), ("hidden", "hidden"), ("visible", "visible"), ("dt-hidden", "hidden"), ("dt-visible", "visible")])
def test_a_devtest_tap_closes_traces_the_action_and_state(windows, state) -> None:
    got = _rig(windows, "1", "approve")
    assert got["closed"] is True and got["fetches"] == 0 and got["otherPath"] is False, got   # none of the ping paths ran
    [e] = [t for t in got["trace"] if t["step"] == "devtest-click"]
    assert e["action"] == "approve" and e["state"] == state, got["trace"]


@needs_node
@pytest.mark.parametrize("windows", ["none", "hidden", "visible"])
def test_a_tap_never_hijacks_a_window_that_is_not_on_devtest(windows) -> None:
    got = _rig(windows, "1", "approve")
    assert got["opened"] == ["https://box.example/devtest"] and got["navigated"] == [] and got["focused"] == 0 and got["posted"] == [], got


@needs_node
@pytest.mark.parametrize("windows", ["dt-hidden", "dt-visible"])
def test_a_tap_with_devtest_open_focuses_it_and_never_reloads_its_diagnostics(windows) -> None:
    got = _rig(windows, "1", "deny")
    assert got["opened"] == [] and got["navigated"] == [] and got["focused"] == 1, got
    assert got["posted"] == [{"type": "devtest-drain"}]       # told to report, not reloaded


@needs_node
def test_a_devtest_body_tap_is_named_and_an_ordinary_ping_tap_never_takes_the_branch() -> None:
    [e] = [t for t in _rig("none", "1", "-")["trace"] if t["step"] == "devtest-click"]
    assert e["action"] == "(body tap)"
    got = _rig("none", "0", "-")
    assert not [t for t in got["trace"] if t["step"] == "devtest-click"], got["trace"]


# --- the page -----------------------------------------------------------------------------------------------------------

@needs_node
def test_a_failed_report_is_retried_and_nothing_after_it_is_sent_first() -> None:
    ring = _ring(("devtest-shown", "actions=2", "hidden"), ("devtest-click", "approve", "hidden"), ("devtest-click", "deny", "none"))
    keys = sorted(ring)
    got = _page(entries=ring, plan=[200, 503, 200], steps=["load", "visible", "wait"])
    assert [(p["event"], p["action"]) for p in got["posts"]] == [("shown", "actions=2"), ("click", "approve"), ("click", "approve"), ("click", "deny")], got["posts"]
    assert got["sent"] == keys[-1]                              # the watermark moved only past accepted reports, in order


@needs_node
def test_the_watermark_stays_before_a_refused_report() -> None:
    ring = _ring(("devtest-click", "approve", "none"), ("devtest-click", "deny", "none"))
    keys = sorted(ring)
    got = _page(entries=ring, plan=[200, 429], steps=["load"])
    assert len(got["posts"]) == 2 and got["sent"] == keys[0], got
    again = _page(entries=ring, sent=keys[0], plan=[200], steps=["load"])
    assert [p["action"] for p in again["posts"]] == ["deny"] and again["sent"] == keys[1]


@needs_node
def test_a_transient_cache_read_failure_stops_the_pass_and_loses_no_tap() -> None:
    # L3 r1 LOW-MED: a cache.match()/json() throw was "passed over" and the watermark advanced, so the valid tap was skipped forever
    ring = _ring(("devtest-click", "approve", "none"), ("devtest-click", "deny", "none"))
    keys = sorted(ring)
    got = _page(entries=ring, plan=[200], matchThrow={keys[0]: 1}, steps=["load"])
    assert got["posts"] == [] and got["sent"] in (None, ""), got              # stopped at the first failure: nothing passed, nothing skipped
    got = _page(entries=ring, plan=[200], matchThrow={keys[0]: 1}, steps=["load", "visible", "wait"])
    assert [p["action"] for p in got["posts"]] == ["approve", "deny"] and got["sent"] == keys[1], got   # the next pass reports both, in order


@needs_node
def test_a_report_the_network_dropped_is_retried_not_lost() -> None:
    ring = _ring(("devtest-shown", "actions=2", "none"))
    got = _page(entries=ring, plan=["throw", 200], steps=["load", "visible"])
    assert [p["event"] for p in got["posts"]] == ["shown", "shown"] and got["sent"] == sorted(ring)[0]


@needs_node
@pytest.mark.parametrize("steps", [["load", "visible2"], ["load", "swmsg", "visible"]])
def test_overlapping_drains_report_a_tap_once(steps) -> None:
    ring = _ring(("devtest-click", "approve", "hidden"))
    got = _page(entries=ring, plan=[200], slow=40, steps=steps)
    assert [p["action"] for p in got["posts"]] == ["approve"], got["posts"]


@needs_node
def test_the_worker_message_makes_the_page_report_a_tap_that_arrived_after_it_loaded() -> None:
    late = {"/__levain_trace/0000000009999-0009": {"t": 9999, "step": "devtest-click", "action": "deny", "state": "visible"}}
    got = _page(late=late, plan=[200], steps=["load", "late", "wait"])
    assert got["posts"] == []                                    # nothing tells the page: it stays visible, no visibilitychange
    got = _page(late=late, plan=[200], steps=["load", "late", "swmsg", "wait"])
    assert [p["action"] for p in got["posts"]] == ["deny"]


@needs_node
def test_an_unprintable_action_is_made_printable_so_it_cannot_stall_the_queue() -> None:
    ring = _ring(("devtest-click", "a\nb", "no\x00ne"))
    got = _page(entries=ring, plan=[200], steps=["load"])
    assert got["posts"][0]["action"] == "a?b" and got["posts"][0]["state"] == "no?ne"


@needs_node
def test_run_waits_for_the_new_worker_then_asks_the_server_to_push() -> None:
    got = _page(active="new", steps=["load", "run10"])
    assert got["sends"] == [{"delay_s": 10}], got
    text = " | ".join(got["lines"])
    assert "SW ready, build V-NEW" in text and "lock the phone" in text and "Requested" in text


@needs_node
@pytest.mark.parametrize("active", ["old", "none"])
def test_run_with_a_stale_or_absent_active_worker_asks_for_a_reopen_and_sends_nothing(active) -> None:
    got = _page(active=active, steps=["load", "run10"])
    assert got["sends"] == [] and "reopen the app once to pick up the new worker" in " | ".join(got["lines"]), got["lines"]
    assert "SW ready" not in " | ".join(got["lines"])


@needs_node
def test_a_double_tap_on_run_asks_the_server_once() -> None:
    got = _page(steps=["load", "run10x2"])
    assert len(got["sends"]) == 1


@needs_node
@pytest.mark.parametrize("status,said", [(409, "already on its way"), (429, "Six test notifications"), (403, "not paired")])
def test_run_says_why_the_server_refused(status, said) -> None:
    got = _page(sendStatus=status, steps=["load", "run10"])
    assert said in " | ".join(got["lines"]), got["lines"]


def test_the_page_is_served_and_carries_the_controls(rig) -> None:
    src, qdir = rig
    with _serve(src) as (port, _h):
        for path, kind in (("/devtest", "text/html"), ("/devtest.js", "text/javascript")):
            st, _b, hdrs = _call(port, "GET", path)
            assert st == 200 and hdrs["content-type"].startswith(kind), (path, st)
    html, js = load_web_asset("devtest.html"), load_web_asset("devtest.js")
    assert 'id="dt-run10"' in html and 'id="dt-run60"' in html and "Send test notification (10 s)" in html
    assert '<script src="/devtest.js"></script>' in html and not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert "sw_open.js" not in html.replace("No sw_open.js here", "")
    for needle in ("Notification.maxActions", '"actions" in Notification.prototype', "Notification.requestPermission", "/webpush/devtest/send",
                   "/webpush/devtest", "X-Levain-Write-Token", "devtest-click", "devtest-shown", "devtest-version"):
        assert needle in js, needle
    assert "setTimeout(async" not in js and "showNotification" not in js      # no page timer, no page-made notification: the server pushes it
    assert "run(10)" in js and "run(60)" in js
