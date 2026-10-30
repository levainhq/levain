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
        # no other route is widened by a navigation's headers: the claim script, a data read, HEAD /pair. The bare "/" is
        # admitted on purpose (W1a: an old phone link lands there); "/" with a query stays refused.
        for method, path in (("GET", "/?x=1"), ("GET", "/pair.js"), ("GET", "/cockpit/manifest.json"),
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
    assert len(re.findall(r'api\("POST"', js)) == 4      # offer, revoke, and the pings subscribe and unsubscribe (OS-4)


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
    assert js.count("() => attempt(code, true, oldTok)") == 1 and js.count(", oldTok);") == 2


def test_pair_it_again_claims_first_and_looks_the_old_key_up_only_after_the_claim_succeeded() -> None:
    # L3 r5 K2: no read ever sits before the claim POST (a short-lived code must not expire behind a slow list read).
    js = load_web_asset("pair.js")
    pair_again = js[js.index("function pairAgain"): js.index("function run()")]
    assert "previousKey" not in pair_again and "await" not in pair_again, "pairAgain must go straight to the claim"
    assert "attempt(code, false, storedToken())" in pair_again       # the held key is snapshotted before the claim replaces it
    att = js[js.index("async function attempt"): js.index("async function previousKey")]
    # inside attempt the lookup comes after the claim, after the new key was stored, and only for a snapshotted key
    assert att.index("await claim(code)") < att.index("setItem(WRITE_TOKEN_KEY, data.token)") < att.index("previousKey(oldTok)")
    assert "if (oldTok) {" in att and js.count("previousKey(") == 2          # the definition and the one call
    # the lookup is GET-only with the snapshot, manifest then devices list
    assert "/cockpit/manifest.json" in js and "/cockpit/pair/devices.json" in js and "X-Levain-Write-Token" in js
    assert 'startsWith(PAIRED_PREFIX)' in js and 'PAIRED_PREFIX = "paired-"' in js
    # the label reaches the screen through textContent (say), never innerHTML
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js


def test_the_previous_key_result_is_tri_state_and_only_a_known_owner_or_revoked_key_is_silent() -> None:
    # L3 r5 K1: a lookup failure used to drop the warning (fail-open). Now only "owner" and "revoked" say nothing.
    js = load_web_asset("pair.js")
    body = js[js.index("async function previousKey"): js.index("function pairAgain")]
    assert body.count("return unknown") >= 5 and body.count("status !== 200") == 2 and "catch" in body
    assert 'return { kind: "owner" }' in body and 'return { kind: "revoked" }' in body and "row.revoked_at" in body
    # the owner verdict needs a 200 manifest whose credential name is a string without the paired prefix, nothing weaker
    assert body.index("man.status !== 200") < body.index('kind: "owner"') and "typeof name !== \"string\"" in body
    att = js[js.index("async function attempt"): js.index("async function previousKey")]
    assert 'prev.kind === "owner" || prev.kind === "revoked"' in att and "PREVIOUS_MAYBE" in att
    assert "may still be listed in Devices. Revoke it there." in js
    assert "still listed in Devices as " in js
    assert "previous key is still listed in Devices. Revoke it there." not in js


def test_the_previous_key_line_names_the_label_with_the_same_short_id_the_devices_panel_shows() -> None:
    # L3 r5 K3: labels are not unique; the line adds a short stable id, and the panel shows that same id on every row.
    pj, dp = load_web_asset("pair.js"), load_web_asset("devices_panel.js")
    rule = "function shortId(id) { return String(id).slice(0, 8); }"
    assert rule in pj and rule in dp                                     # one rule, two copies, equal by this test
    assert "shortId(id)" in pj and "shortId(d.id)" in dp
    # the id the server mints is "d-" + 12 hex: 8 characters keep 24 random bits
    assert re.search(r'"d-" \+ secrets\.token_hex\(6\)', Path(__file__).parent.parent.joinpath("levain/cockpit/firejournal.py").read_text())


def test_the_label_in_the_line_is_quoted_and_bounded_and_the_lookup_cannot_block_the_page() -> None:
    js = load_web_asset("pair.js")
    assert "LABEL_MAX = 60" in js and "function boundLabel" in js and "\\u2026" in js
    assert "\\u202a-\\u202e" in js and "\\u2066-\\u2069" in js                # bidi controls are stripped
    assert "\\u201c" in js and "\\u201d" in js                                 # quoted
    # K4: AbortController is built inside the try; a browser without it falls to the generic line
    body = js[js.index("async function previousKey"): js.index("function pairAgain")]
    assert body.index("try {") < body.index("new AbortController()")
    # glm LOW 3: ids compare as strings
    assert "String(d.id) === id" in js and "d.id === id" not in js


def test_a_refused_held_key_is_silent_not_may_still_be_listed() -> None:
    # L3 r6 M1: measured on the rig, a REVOKED paired key's manifest GET answers 200 with credential {class: "none"} and no name
    # (an unknown key the same); a 401/403 is also a refusal. Both mean the held key is dead: no previous-key line.
    js = load_web_asset("pair.js")
    body = js[js.index("async function previousKey"): js.index("function pairAgain")]
    assert re.search(r'man\.status === 401 \|\| man\.status === 403\) return \{ kind: "revoked" \}', body)
    assert 'm.credential.class === "none"' in body and 'return { kind: "revoked" }' in body
    assert body.index("man.status === 401") < body.index("man.status !== 200") < body.index('class === "none"') < body.index("typeof name")


def test_the_label_is_isolated_in_a_bdi_built_with_text_content_and_the_id_cannot_wrap() -> None:
    # L3 r6 M2 + M4: untrusted RTL label text sits in a <bdi> inside the quotes (textContent only); the short id is a no-wrap span
    # in the Paired line and in every Devices row.
    js, dp = load_web_asset("pair.js"), load_web_asset("devices_panel.js")
    fn = js[js.index("function sayLive"): js.index("async function previousKey")]
    assert 'createElement("bdi")' in fn and "bdi.textContent = prev.label" in fn and "msgEl.append(" in fn
    assert "\\u201c\", bdi, \"\\u201d (\", id," in fn
    assert "innerHTML" not in js and "innerHTML" not in dp
    assert 'id.className = "dev-id"' in fn and 'el("span", "dev-id", shortId(d.id))' in dp
    assert re.search(r"\.dev-id \{[^}]*white-space: nowrap", load_web_asset("dashboard.css"))


def test_the_label_strip_class_holds_the_zero_width_and_arabic_mark_and_an_empty_label_falls_back() -> None:
    # L3 r6 M3: U+061C, U+200B-U+200D, U+FEFF are stripped; a label of only those is "paired device".
    js = load_web_asset("pair.js")
    for part in ("\\u061c", "\\u200b-\\u200f", "\\ufeff"):
        assert part in js
    assert 'label || "paired device"' in js


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


def test_the_pair_script_words_the_retry_403_and_keeps_a_failed_mark_write_from_reading_as_a_lost_key() -> None:
    js = load_web_asset("pair.js")
    # G3: only a 403 AFTER a Try again says the first try may have spent the code
    assert "attempt(code, true, oldTok)" in js and "The first try may have used this code." in js
    # G4: the mark's write is its own try, after the key's catch has returned
    assert re.search(r'try \{ window\.localStorage\.setItem\(PAIRED_MARK_KEY, "1"\); \} catch', js)
    key_try = js.index("window.localStorage.setItem(WRITE_TOKEN_KEY, data.token);")
    assert js.index("return;", key_try) < js.index("PAIRED_MARK_KEY, \"1\"")      # the key's catch ends in its own return


def test_a_devices_label_inside_a_panel_sentence_is_bidi_isolated() -> None:
    # The M2 class in the Devices panel (fix r6 observation): a label interpolated into a revoke note or an aria-label goes through
    # named() (FSI ... PDI); the row title holds the label alone and takes its own direction.
    dp = load_web_asset("devices_panel.js")
    assert 'function named(d) { return "\\u2068" + (d.label || d.id) + "\\u2069"; }' in dp
    assert dp.count('" + (d.label || d.id) + "') == 1      # named() itself; no label concatenated raw into a sentence
    assert 'name.dir = "auto"' in dp
