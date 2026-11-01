"""The on-device push-tap trace: the worker and the page write steps into one ring (Cache Storage `levain-trace`), and
/tap_trace lists it. The trace is an instrument only; the tap's behaviour is pinned in test_sw_worker.py."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from levain.dashboard import SubstrateSource
from levain.web_server import load_web_asset
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import PROXY, _call, _serve
from tests.test_sw_worker import _open_rig, _run, needs_node

CARD = "https://box.example/sign/9#sign=a&v=1"


@pytest.fixture
def src(tmp_path: Path) -> SubstrateSource:
    return _install(tmp_path)[1]


def _steps(got: dict) -> list[str]:
    return [e["step"] for e in got["trace"]]


@needs_node
def test_the_ack_path_records_click_matchall_asked_and_the_ack() -> None:
    got = _run("navigate", "controlled+ack")
    steps = _steps(got)
    assert steps[:2] == ["sw-version", "click"], steps
    for want in ("click", "matchAll", "asked", "ack"):
        assert want in steps, steps
    assert "openWindow" not in steps and "navigate" not in steps, steps
    by = {e["step"]: e for e in got["trace"]}
    assert by["sw-version"]["version"] and by["click"]["url"] == "/sign/9#..."          # the fragment (a sign token) is never kept
    assert by["matchAll"]["controlled"] == 1 and by["matchAll"]["controlledWindows"][0]["visibility"] == "visible", by["matchAll"]
    assert by["ack"]["acked"] is True
    assert "sign=" not in json.dumps(got["trace"]), got["trace"]


@needs_node
def test_the_no_window_path_records_the_openwindow_result_and_the_dropped_entry() -> None:
    got = _run("navigate", "matchfail")      # no window at all: the asked entry is for nobody, openWindow runs
    steps = _steps(got)
    for want in ("click", "matchAll", "asked", "openWindow", "openNew-dropped"):
        assert want in steps, steps
    by = {e["step"]: e for e in got["trace"]}
    assert by["matchAll"]["any"] == 0 and by["openWindow"]["client"] == "/sign/9#...", by
    # a refused openWindow is a step too
    assert {e["step"]: e for e in _run("navigate", "opennull")["trace"]}["openNew-dropped"]["after"] == "no client"
    assert {e["step"]: e for e in _run("navigate", "openfail")["trace"]}["openWindow"]["error"] == "TypeError"


@needs_node
def test_a_controlled_window_that_never_acks_records_the_claim_and_the_navigate() -> None:
    got = _run("navigate", "controlled")
    by = {e["step"]: e for e in got["trace"]}
    assert by["ack"]["acked"] is False and by["no-ack-claim"]["mine"] is True and by["navigate"]["ok"] is True, by


@needs_node
def test_a_failing_trace_write_never_breaks_the_tap() -> None:
    for mode, tap in (("controlled+ack+tracefail", "asked"), ("matchfail+tracefail", "openedUrl"), ("controlled+tracefail", "navigated")):
        got = _run("navigate", mode)
        assert got["trace"] == [], got["trace"]                                   # nothing was written, nothing threw
        assert got[tap] and not [m for m in got["console"] if "trace" in m[1]], (mode, got)
    got = _run("navigate", "controlled+tracefail")
    assert got["navigated"] == ["https://box.example/sign/9#sign=a&v=1"], got   # the same tap as without a trace


@needs_node
def test_the_worker_trace_ring_is_capped_and_activation_keeps_it() -> None:
    got = _run("navigate", "controlled+ack+two")
    assert 0 < len(got["trace"]) <= 60
    sw = Path(__file__).resolve().parent.parent / "levain" / "webpush" / "static" / "sw.js"
    assert re.search(r"CACHES_KEEP = \[OPEN_CACHE, TRACE_CACHE, RECEIPT_CACHE\]", sw.read_text(encoding="utf-8"))   # activation keeps the ring


@needs_node
def test_the_page_records_load_message_claim_and_assign_without_a_fragment() -> None:
    got = _open_rig("claim")
    page = [e for e in got["trace"] if e["src"] == "page"]
    steps = [e["step"] for e in page]
    assert steps[0] == "page-load" and "message" in steps and "assign" in steps, steps
    load = page[0]
    assert load["standalone"] is False and load["navStandalone"] is True and load["controller"] is True, load
    claims = [e["claim"] for e in page if e["step"] == "message"]
    assert "won" in claims and "lost" in claims, claims
    assert [e["target"] for e in page if e["step"] == "assign"] == ["/sign/9#..."]
    assert "sign=" not in json.dumps(got["trace"])

    got = _open_rig("load")
    cp = [e for e in got["trace"] if e["step"] == "checkPending"]
    first = cp[0]
    assert first["claimed"] == "/sign/9#..." and first["assigned"] is True and first["seen"][0]["for"] == "new", first


@needs_node
def test_a_url_keeps_only_the_view_param_and_markers_and_no_secret_is_recorded() -> None:
    got = _run("navigate", "secreturl+ack")
    text = json.dumps(got["trace"])
    assert "SECRET" not in text and "write_token" not in text and "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" not in text, text
    by = {e["step"]: e for e in got["trace"]}
    assert by["click"]["url"] == "/?view=needs#...", by["click"]
    assert by["click"]["ping"] is None, by["click"]                                 # an object ping is not recorded
    assert by["matchAll"]["controlledWindows"][0]["url"] == "/?view=needs#...", by["matchAll"]
    assert by["ack"]["window"] == "/?view=needs#..."


@needs_node
def test_a_push_records_a_clipped_tag_and_scalar_ping_only() -> None:
    sw = (Path(__file__).resolve().parent.parent / "levain" / "webpush" / "static" / "sw.js").read_text(encoding="utf-8")
    assert "traceScalar(d.tag" in sw and "traceScalar(d.ping)" in sw and "slice(0, 60)" in sw


@needs_node
def test_the_page_records_the_nonce_as_a_flag_never_its_value_and_url_params_are_dropped() -> None:
    got = _open_rig("claim")
    text = json.dumps(got["trace"])
    assert '"n1"' not in text and '"other"' not in text, text
    msgs = [e for e in got["trace"] if e["step"] == "message"]
    assert msgs and all(e["nonce"] is True for e in msgs), msgs
    sw_open = (Path(__file__).resolve().parent.parent / "levain" / "templates" / "web" / "sw_open.js").read_text(encoding="utf-8")
    assert "nonce: typeof d.nonce" in sw_open and "d.nonce : null" not in sw_open


@needs_node
def test_a_page_whose_clock_throws_inside_trace_still_acks_and_assigns() -> None:
    got = _open_rig("datethrow")
    assert got["afterThrow"]["acks"] == 1 and got["afterThrow"]["assigned"] == ["https://box.example/sign/9#sign=a&v=1"], got
    assert got["trace"] == [], got["trace"]                                          # nothing could be written, nothing threw


@needs_node
def test_a_tap_trace_page_open_is_never_the_window_taken_over() -> None:
    got = _run("navigate", "tapwin+ack")
    assert got["asked"] == [] and got["navigated"] == [] and got["openedUrl"] == CARD, got   # no window: openWindow runs
    steps = _steps(got)
    assert "openWindow" in steps and "ack" not in steps, steps
    got = _run("navigate", "tapwin2+ack")             # another window beside it is the one asked
    assert got["asked"] == [CARD] and got["askedWho"] == ["https://box.example/docs/guide"], got


@needs_node
def test_the_trace_page_skips_entries_that_are_not_step_records() -> None:
    js = (Path(__file__).resolve().parent.parent / "levain" / "templates" / "web" / "tap_trace.js").read_text(encoding="utf-8")
    assert "Number.isFinite(e.t)" in js and 'typeof e.step === "string"' in js and "e && typeof e" in js


def test_tap_trace_is_served_without_a_credential_and_carries_no_token_field(src) -> None:
    with _serve(src) as (port, _h):
        for path, kind in (("/tap_trace", "text/html"), ("/tap_trace.js", "text/javascript")):
            for headers in (None, PROXY):
                st, body, hdrs = _call(port, "GET", path, headers=headers)
                assert st == 200 and hdrs["content-type"].startswith(kind), (path, headers, st)
    html = load_web_asset("tap_trace.html")
    js = load_web_asset("tap_trace.js")
    assert '<script src="/tap_trace.js"></script>' in html and not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert "sw_open.js" not in html.replace("No sw_open.js here", "")             # the page never claims a pending tap
    for text in (html, js):
        assert not re.search(r"token|authorization|localStorage|sessionStorage|fetch\(", text, re.I), text[:80]
