"""PAIR P2, the browser half: the /pair page, its script and the Devices panel (levain project_memory/1024-18/PAIR_DESIGN.md
rev 6). Each assertion is one the real-browser run (project_memory/1024-20/pair_p2/RETURN.md) showed."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from levain.dashboard import SubstrateSource
from levain.web_server import load_web_asset
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import PROXY, T, _call, _serve


@pytest.fixture
def src(tmp_path: Path) -> SubstrateSource:
    return _install(tmp_path)[1]


def test_the_pair_page_and_its_scripts_are_served_without_a_credential_proxied_and_with_no_inline_script(src) -> None:
    with _serve(src) as (port, _h):
        for path, kind in (("/pair", "text/html"), ("/pair.js", "text/javascript"), ("/devices_panel.js", "text/javascript")):
            for headers in (None, PROXY):                    # off-box (his :8443 path) is the one that matters
                st, out, hdrs = _call(port, "GET", path, headers=headers)
                assert st == 200 and hdrs["content-type"].startswith(kind), (path, headers, st)
                assert hdrs["cache-control"] == "no-store" and "script-src 'self'" in hdrs["content-security-policy"]
        html = load_web_asset("pair.html")
        assert '<script src="/pair.js"></script>' in html and not re.search(r"<script(?![^>]*\bsrc=)", html)
        assert '<script src="/devices_panel.js"></script>' in load_web_asset("dashboard.html")


def test_the_pair_script_and_the_panel_use_the_dashboards_own_storage_key() -> None:
    boot = re.search(r'WRITE_TOKEN_KEY = "([^"]+)"', load_web_asset("dashboard_boot.js"))
    assert boot, "dashboard_boot.js no longer names its key"
    for name in ("pair.js", "devices_panel.js"):
        mine = re.search(r'WRITE_TOKEN_KEY = "([^"]+)"', load_web_asset(name))
        assert mine and mine.group(1) == boot.group(1), name
    # L3 r1 F6: the "paired by a link" mark pair.js sets is the one dashboard_boot.js reads to show the unpaired card
    marks = [re.search(r'PAIRED_MARK_KEY = "([^"]+)"', load_web_asset(n)) for n in ("pair.js", "dashboard_boot.js")]
    assert all(marks) and marks[0].group(1) == marks[1].group(1)
    # the claim sends the code and nothing else (RP-6: the label is the offer's; the server answers 400 to a label)
    assert re.search(r"JSON\.stringify\(\{ code: code \}\)", load_web_asset("pair.js"))


def test_the_device_row_carries_the_fields_the_panel_reads(src) -> None:
    with _serve(src) as (port, _h):
        st, off, _ = _call(port, "POST", "/cockpit/pair/offer", {"label": "Run iPad"}, token=T)
        assert st == 200 and "code" in off, (st, off)
        st, cl, _ = _call(port, "POST", "/cockpit/pair/claim", {"code": off["code"]})
        assert st == 200
        st, out, _ = _call(port, "GET", "/cockpit/pair/devices.json", token=T)
        (row,) = out["devices"]
        js = load_web_asset("devices_panel.js")
        for field in ("id", "label", "created", "last_seen", "revoked_at", "paired_by"):
            assert field in row and re.search(r"\bd\.%s\b" % field, js), field
        assert row["label"] == "Run iPad" and row["revoked_at"] is None
        assert off["pair_path"] == "/pair#code=" + off["code"]     # the panel prefixes location.origin to this


NAV = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}


def test_a_pair_link_opened_from_another_site_loads_and_nothing_else_widens(src) -> None:
    # L3 r1 F1: a link clicked in webmail is a cross-site top-level navigation; the page must load (it carries no data).
    with _serve(src) as (port, _h):
        for extra in (None, PROXY):
            st, _o, hdrs = _call(port, "GET", "/pair", headers={**NAV, **(extra or {})})
            assert st == 200 and hdrs["content-type"].startswith("text/html"), (extra, st)
            assert hdrs["x-frame-options"] == "DENY"
        # a cross-site fetch / subresource load of the same page stays refused
        for mode, dest in (("cors", "empty"), ("no-cors", "empty"), ("no-cors", "iframe"), ("navigate", "iframe")):
            st, _o, _h2 = _call(port, "GET", "/pair", headers={**NAV, "Sec-Fetch-Mode": mode, "Sec-Fetch-Dest": dest})
            assert st == 403, (mode, dest, st)
        # no other route is widened by a navigation's headers: the cockpit page, the claim script, a data read, HEAD /pair
        for method, path in (("GET", "/"), ("GET", "/pair.js"), ("GET", "/cockpit/manifest.json"),
                             ("GET", "/pair/"), ("GET", "/pair?x=1"), ("HEAD", "/pair")):
            st, _o, _h2 = _call(port, method, path, headers=NAV)
            assert st == 403, (method, path, st)
        # the Host check still holds for the carve-out
        st, _o, _h2 = _call(port, "GET", "/pair", headers={**NAV, "Host": "evil.example"})
        assert st == 403
        # the claim POST is unchanged: cross-site refused
        st, _o, _h2 = _call(port, "POST", "/cockpit/pair/claim", {"code": "x"}, headers={**NAV, "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"})
        assert st == 403


def test_the_panel_reads_through_one_shared_promise_and_no_flag_can_stick() -> None:
    # L3 r2 G1: the gen/inFlight guard stuck (a no-token return left inFlight true: the panel never polled again). One shared
    # in-flight promise with one queued re-run.
    js = load_web_asset("devices_panel.js")
    assert not re.search(r"\b(inFlight|gen)\b", js), "the generation/in-flight flags are back"
    assert re.search(r"let running = null, rerun = false, gone = false, refused = \"\";", js)
    assert "if (running) { rerun = true; return running; }" in js
    assert re.search(r"finally \{ running = null;", js)
    # G2: the revoke's button is re-enabled only when the revoke FAILED
    assert "if (!revoked && btn.isConnected) btn.disabled = false;" in js


def test_a_403_refuses_that_token_only_and_a_different_token_revives_the_panel() -> None:
    # L3 r3 H2: `gone` is for 404 alone; a 403 remembers the refused token and reads are skipped only while token() is still it.
    js = load_web_asset("devices_panel.js")
    assert re.search(r"res\.status === 404\) \{ section\.hidden = true; gone = true;", js)
    assert re.search(r"res\.status === 403\) \{ section\.hidden = true; refused = tok;", js)
    assert not re.search(r"403[^\n]*gone = true", js), "a 403 must not set gone for the page"
    assert "if (tok === refused)" in js
    assert js.index("if (gone) return true;") < js.index("if (tok === refused)")


def test_only_the_devices_list_read_has_a_timeout_never_the_offer_or_revoke_post() -> None:
    # L3 r3 H3: a committed mutation answered after the timeout must not read as unreachable (and be repeated).
    js = load_web_asset("devices_panel.js")
    assert "async function api(method, path, body, timeoutMs)" in js
    assert "const timer = timeoutMs ? setTimeout(() => ctl.abort(), timeoutMs) : null;" in js
    # every call, by its text: the GET passes READ_TIMEOUT_MS; the POSTs pass no fourth argument
    for m in re.finditer(r'api\("(GET|POST)",[^;]*;', js):
        line = m.group(0)
        if m.group(1) == "GET":
            assert "READ_TIMEOUT_MS" in line, line
        else:
            assert "READ_TIMEOUT_MS" not in line and "timeout" not in line.lower(), line
    assert len(re.findall(r'api\("POST"', js)) == 2


def _fetch_calls(js: str) -> "list[str]":
    """Each `fetch(` call's text up to its closing `);` (the script's few fetches are plain object-literal calls)."""
    return [js[m.start(): js.index(");", m.start()) + 2] for m in re.finditer(r"fetch\(", js)]


def test_pair_it_again_never_makes_a_revoke_request_and_its_only_write_is_the_claim() -> None:
    # L3 r3 H1: the self-revoke is gone (revoke-first strands the browser on a failed claim; revoke-after can cascade onto the
    # device that issued the code). R4: it MAY read, never write: every request but the claim is a GET.
    js = load_web_asset("pair.js")
    assert "retireOldKey" not in js and "pair/revoke" not in js and "revoke(" not in js
    calls = _fetch_calls(js)
    posts = [c for c in calls if '"POST"' in c]
    assert len(posts) == 1 and "/cockpit/pair/claim" in posts[0], calls
    assert all("GET" in c or c in posts for c in calls) and not re.search(r'method: "(PUT|DELETE|PATCH)"', js)
    assert "function pairAgain(code)" in js
    # Try again keeps the re-pair label, so the line survives a failed first claim
    assert js.count("() => attempt(code, true, repaired)") == 1 and js.count(", repaired);") == 2


def test_pair_it_again_reads_the_held_keys_device_label_before_claiming_and_shows_it_as_text() -> None:
    js = load_web_asset("pair.js")
    # the read happens before the claim, with the held key, a GET of the manifest then the devices list
    pair_again = js[js.index("async function pairAgain"): js.index("function run()")]
    assert "previousKeyLabel(" in pair_again and pair_again.index("previousKeyLabel(") < pair_again.index("attempt(")
    assert "/cockpit/manifest.json" in js and "/cockpit/pair/devices.json" in js and "X-Levain-Write-Token" in js
    # only a paired-<id> credential is looked up; an owner surface token yields no label
    assert 'startsWith(PAIRED_PREFIX)' in js and 'PAIRED_PREFIX = "paired-"' in js
    # the label reaches the screen through textContent (say), never innerHTML
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js
    assert "still listed in Devices as " in js and "Revoke it there." in js
    # no label, no line: the screen with no previous label goes straight to the cockpit
    assert re.search(r"if \(repaired\) \{", js) and "Opening the cockpit" in js


def test_the_label_the_page_reads_is_what_the_server_exposes_for_a_paired_key_and_not_for_an_owner_token(src) -> None:
    with _serve(src) as (port, _h):
        st, off, _ = _call(port, "POST", "/cockpit/pair/offer", {"label": "Run iPad"}, token=T)
        st, cl, _ = _call(port, "POST", "/cockpit/pair/claim", {"code": off["code"]})
        tok, dev = cl["token"], cl["device_id"]
        st, man, _ = _call(port, "GET", "/cockpit/manifest.json", token=tok)
        assert st == 200 and man["credential"]["name"] == "paired-" + dev
        st, lst, _ = _call(port, "GET", "/cockpit/pair/devices.json", token=tok)
        assert st == 200 and [d["label"] for d in lst["devices"] if d["id"] == dev] == ["Run iPad"]
        st, man, _ = _call(port, "GET", "/cockpit/manifest.json", token=T)
        assert st == 200 and not man["credential"]["name"].startswith("paired-")


def test_no_previous_key_line_unless_the_read_found_a_paired_label() -> None:
    js = load_web_asset("pair.js")
    body = js[js.index("async function previousKeyLabel"): js.index("async function pairAgain")]
    # a non-200, a throw, a non-paired name, a missing row or an empty label each end in null
    assert body.count("return null") >= 5 and body.count("status !== 200") == 2 and "catch" in body
    assert "setTimeout" in body or "AbortController" in body            # a hung read never holds the claim
    # the plain "still listed" line without a label is gone: only the labelled one remains
    assert "previous key is still listed in Devices. Revoke it there." not in js


def test_the_pair_script_words_the_retry_403_and_keeps_a_failed_mark_write_from_reading_as_a_lost_key() -> None:
    js = load_web_asset("pair.js")
    # G3: only a 403 AFTER a Try again says the first try may have spent the code
    assert "attempt(code, true, repaired)" in js and "The first try may have used this code." in js
    # G4: the mark's write is its own try, after the key's catch has returned
    assert re.search(r'try \{ window\.localStorage\.setItem\(PAIRED_MARK_KEY, "1"\); \} catch', js)
    key_try = js.index("window.localStorage.setItem(WRITE_TOKEN_KEY, data.token);")
    assert js.index("return;", key_try) < js.index("PAIRED_MARK_KEY, \"1\"")      # the key's catch ends in its own return
