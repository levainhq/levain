"""W1b slice C: the push service worker holds no credential. A node rig runs ``levain/webpush/static/sw.js`` over a stub
IndexedDB, Cache Storage and clients (``tests/_sw_worker_rig.js``); the page/worker contract it pins is IndexedDB
``levain-sw-2`` v1, object store ``opened`` (key = the ping id as a string, value = the tap time in ms), window message
``{type: "levain-opened"}``."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SW = HERE.parent / "levain" / "webpush" / "static" / "sw.js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _run(scenario: str, *extra: str) -> dict:
    cp = subprocess.run([NODE, str(HERE / "_sw_worker_rig.js"), str(SW), scenario, *extra], capture_output=True, text=True,
                        timeout=60)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return json.loads(cp.stdout)


@needs_node
def test_activating_the_new_worker_deletes_the_old_database_and_every_levain_cache_it_does_not_keep() -> None:
    """Reproduced red on the base worker (a69ac9c8's sw.js): its `activate` only claimed clients, so `levain-sw` (the
    phone token's database) and `levain-old-shell` survived the activation."""
    got = _run("activate")
    assert got["claimed"] is True
    assert got["dbs"] == ["levain-sw-2"], got                       # the phone-token database is gone, the new one stays
    assert got["keptOpened"] == [["7", 1]], got                     # and it was not touched
    assert got["caches"] == ["levain-open", "other-app-cache"], got  # every `levain-` cache outside CACHES_KEEP (levain-open is kept), no other name


@needs_node
def test_activating_deletes_the_pre_fix4_singleton_pending_key_and_only_that() -> None:
    """(d) a rolling update: the old worker's one fixed key '/__levain_open' (no claim path reads it any more) goes at
    activation; the per-entry keys stay."""
    got = _run("activate")
    assert got["openKeys"] == ["/__levain_open/abc"], got


@needs_node
def test_a_tap_writes_the_ping_id_to_opened_makes_no_fetch_and_the_worker_hears_no_message() -> None:
    """Reproduced red on the base worker: a tap read the token from IndexedDB and called fetch('/webpush/opened')."""
    got = _run("click")
    assert got["fetches"] == 0, got
    # no token can be handed to this worker: its one message listener (update flow, 1104-27) acts only on
    # {type: "levain-skip-waiting"} and reads nothing else (test_only_the_page_message_skips_waiting)
    assert "message" in got["listeners"], got
    assert got["db"] == {"version": 1, "stores": ["opened"]}, got
    (key, tapped), = got["opened"]
    assert key == "42" and isinstance(tapped, int) and tapped > 1_700_000_000_000, got
    assert got["openedAfterNoPing"] == 1, got                        # a tap with no ping id writes nothing
    assert got["posted"] == [{"type": "levain-opened"}], got         # the nudge carries no data
    assert got["closed"] is True


CARD = "https://box.example/sign/9#sign=a&v=1"


@needs_node
def test_a_page_that_acks_is_asked_to_open_the_card_itself_and_the_worker_does_nothing_more() -> None:
    for kind in ("controlled", "uncontrolled"):
        got = _run("navigate", kind + "+ack")
        assert got["asked"] == [CARD] and got["focused"] == 1, got
        assert got["navigated"] == [] and "openWindow" not in got, (kind, got)


@needs_node
def test_a_controlled_page_that_never_acks_is_navigated_by_the_worker() -> None:
    got = _run("navigate", "controlled")
    assert got["asked"] == [CARD] and got["navigated"] == [CARD] and got["focused"] == 1, got
    assert "openWindow" not in got, got


@needs_node
def test_an_uncontrolled_page_that_never_acks_gets_a_new_window_and_no_navigate() -> None:
    """Reproduced in headless Chrome 10-31: WindowClient.navigate() on a window opened before the worker took control
    rejects with 'TypeError: This service worker is not the client's active service worker.'"""
    got = _run("navigate", "uncontrolled")
    assert got["openedUrl"] == CARD and got["navigated"] == [], got


@needs_node
def test_a_navigate_that_rejects_opens_the_card() -> None:
    got = _run("navigate", "reject")
    assert got["openedUrl"] == CARD, got


@needs_node
def test_a_navigate_that_resolves_null_opens_the_card() -> None:
    got = _run("navigate", "null")
    assert got["navigated"] == [CARD] and got["openedUrl"] == CARD, got


@needs_node
def test_a_navigate_that_resolves_at_a_different_url_opens_the_card() -> None:
    got = _run("navigate", "mismatch")
    assert got["navigated"] == [CARD] and got["openedUrl"] == CARD, got


@needs_node
def test_a_cross_origin_url_is_sent_as_the_root() -> None:
    got = _run("navigate", "crossorigin+ack")
    assert got["asked"] == ["https://box.example/"] and got["navigated"] == [], got


@needs_node
def test_the_page_listener_acks_before_it_assigns_and_ignores_a_cross_origin_url() -> None:
    cp = subprocess.run([NODE, str(HERE / "_sw_open_rig.js"), str(SW.parent.parent.parent / "templates" / "web" / "sw_open.js")],
                        capture_output=True, text=True, timeout=60)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    got = json.loads(cp.stdout)
    assert got["assigned"] == [CARD], got                                  # only the same-origin url moved the page
    assert got["acks"] == [{"m": {"ok": True}, "assignedSoFar": 0}], got   # and the ack went out before the assign
    assert got["started"] is True, got


def _open_rig(scenario: str) -> dict:
    cp = subprocess.run([NODE, str(HERE / "_sw_open_rig.js"), str(SW.parent.parent.parent / "templates" / "web" / "sw_open.js"), scenario],
                        capture_output=True, text=True, timeout=60)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return json.loads(cp.stdout)


CARD2 = "https://box.example/sign/10"
KEYS = "/__levain_open/"


@needs_node
def test_a_tap_writes_its_own_keyed_entry_before_it_asks_and_the_levain_open_cache_survives_activation() -> None:
    got = _run("navigate", "controlled+ack")
    pend = got["pending"]
    assert pend["url"] == CARD and pend["for"] == "asked" and got["nonces"] == [pend["nonce"]], got   # the message carries the nonce
    assert pend["key"] == KEYS + pend["nonce"], got                                                   # one key per entry
    assert _run("activate")["caches"] == ["levain-open", "other-app-cache"]   # kept; every other levain- cache still goes


@needs_node
def test_a_late_ack_after_the_worker_took_over_double_navigates_nothing() -> None:
    """(b) the worker timed out and navigated; the page's ack arrived after. The worker deleted its own key, so the page's
    delete of that key lost (no ack, no assign): exactly one navigation."""
    got = _run("navigate", "controlled+late")
    assert got["navigated"] == [CARD] and got["lateClaimed"] is False and got["pendingLeft"] == 0, got
    assert "openWindow" not in got, got


@needs_node
def test_two_overlapping_taps_each_claim_only_their_own_entry() -> None:
    """(a) tap B writes while tap A waits; A's timeout deletes A's key only, so B still owns its entry, times out, claims it
    and navigates to its own card. (A shared key would make B read A's delete as a late ack and drop its card.)"""
    got = _run("navigate", "controlled+silent+two")
    assert got["navigated"] == [CARD, CARD2] and "openWindow" not in got and got["pendingLeft"] == 0, got
    assert len(set(got["nonces"])) == 2, got


@needs_node
def test_a_refused_open_window_warns_and_leaves_no_entry() -> None:
    """(e) no window; openWindow rejects: the 'new' entry the worker wrote just before is deleted, so a later plain visit to
    the root is never sent to a card nobody opened."""
    got = _run("navigate", "openfail")
    assert got["openAttempt"] == CARD, got
    assert any(k == "warn" and "openWindow" in m and "refused" in m for k, m in got["console"]), got
    assert ["info", "levain sw: tap -> openWindow-failed"] in got["console"], got
    assert got["pendingLeft"] == 0, got


@needs_node
def test_an_open_window_that_returns_no_client_leaves_no_entry() -> None:
    got = _run("navigate", "opennull")
    assert got["openAttempt"] == CARD and got["pendingLeft"] == 0, got
    assert [e["for"] for e in got["entriesAtOpen"]] == ["new"], got   # the no-window path writes ONLY the 'new' entry
    assert ["info", "levain sw: tap -> openWindow-failed"] in got["console"], got


@needs_node
def test_a_navigate_that_resolves_at_the_target_with_another_fragment_counts_as_arrived() -> None:
    got = _run("navigate", "fragment")
    assert got["navigated"] == [CARD] and "openWindow" not in got, got


@needs_node
def test_a_navigate_that_resolves_at_the_root_is_not_arrival_at_a_query_target() -> None:
    """(f) the target is '/?view=needs'; the client stayed at '/': same origin and path but another query is NOT success."""
    got = _run("navigate", "search")
    assert got["navigated"] == ["https://box.example/?view=needs"] and got["openedUrl"] == "https://box.example/?view=needs", got


@needs_node
def test_the_window_preference_picks_the_cockpit_page_over_a_docs_page() -> None:
    got = _run("navigate", "prefer+ack")
    assert got["askedWho"] == ["https://box.example/sign/3"], got


@needs_node
def test_a_fallback_open_window_writes_one_fresh_new_entry_just_before_it_and_drops_it_after_ten_seconds() -> None:
    """(c, d) the asked entry is gone (the worker claimed it) and ONE 'new' entry, another nonce, is present when
    openWindow runs, so the asked window can never claim it and the page iOS lands on '/' can. A successful openWindow into
    a page that never claims it (no listener) leaves nothing behind: the worker holds the click open for the NEW_TTL_MS window,
    then drops its own key (the rig shortens the wait and records the requested one)."""
    got = _run("navigate", "uncontrolled")
    assert got["longWaits"] == [30000], got
    entry, = got["entriesAtOpen"]
    assert entry["for"] == "new" and entry["nonce"] != got["nonces"][0], got
    assert got["pendingLeft"] == 0, got


@needs_node
def test_a_window_lookup_that_rejects_still_reaches_the_fallback_and_cleans_the_asked_entry() -> None:
    """a rejected matchAll counts as no window: the asked entry is dropped, openWindow runs with its 'new' entry"""
    got = _run("navigate", "matchfail")
    assert got["openedUrl"] == CARD and got["asked"] == [], got
    assert [e["for"] for e in got["entriesAtOpen"]] == ["new"], got   # the asked entry was dropped before the fallback wrote its own
    assert got["pendingLeft"] == 0, got


@needs_node
def test_a_message_deletes_only_the_key_its_nonce_names() -> None:
    got = _open_rig("claim")
    assert got["afterWrong"] == {"assigned": 0, "acks": 0, "n1": True, "n2": True}, got   # no entry for the nonce: no ack, no move, nothing deleted
    assert got["assigned"] == [CARD] and len(got["acks"]) == 1, got                        # the right nonce won once; the repeat and the gone entry did nothing
    assert got["afterRight"] == {"n1": False, "n2": True}, got                             # another tap's entry is never touched


@needs_node
def test_only_a_documents_first_script_run_claims_a_fresh_new_entry_at_the_root_or_at_its_own_target() -> None:
    got = _open_rig("load")
    assert got["firstLoad"] == {"assigned": [CARD], "pending": False}, got         # iOS landed on '/': the first-load check forwards
    assert got["noPageshowHandler"] is True, got                                   # the scan is one per document: pageshow runs none
    assert got["asked"] == {"assigned": 1, "askedKept": True, "expiredNewGone": True}, got   # 'asked' is the message path's (15 s old: alive); 'new' dies at 30 s
    assert got["askedExpired"] == {"assigned": 1, "gone": True}, got               # 'asked' dies at 30 s
    assert got["alreadyHere"] == {"assigned": 1, "pending": False}, got            # consumed, no pointless assign
    assert got["unrelated"] == {"assigned": 1, "pending": True}, got               # an unrelated first load (not '/', not the target) takes nothing
    assert got["unrelatedPair"] == {"assigned": 1, "pending": True}, got
    assert got["resumed"] == {"assigned": 1, "pending": True}, got                 # visibilitychange and any pageshow claim nothing
    assert got["nineSeconds"] == {"assigned": 2, "pending": False}, got           # inside the 30 s window a root landing forwards
    assert got["cross"] == {"assigned": 2}, got                                    # never follows another origin
    assert got["query"] == {"assigned": ["https://box.example/?view=needs"], "pending": False}, got   # '/' is not '/?view=needs'
    assert got["fragment"] == {"moved": 0, "pending": False}, got                  # only a fragment differs: already here


@needs_node
def test_a_first_load_takes_the_entry_for_its_own_url_else_the_newest() -> None:
    got = _open_rig("first")
    assert got["first"] == {"assigned": ["https://box.example/sign/3"], "left": ["mid", "old"]}, got
    assert got["own"] == {"assigned": 1, "left": ["mid"]}, got   # at /sign/1 it takes that entry and stays put


@needs_node
def test_a_page_opened_for_one_tap_does_not_consume_the_next_taps_entry_on_its_initial_pageshow() -> None:
    """overlapping taps A and B, a page opened at A: A's entry is its own; B's survives A's load and A's pageshow, and the
    page opened for B takes it. (The pageshow rescan this replaces moved A's page to B.)"""
    got = _open_rig("overlap")
    assert got["afterLoad"] == {"assigned": [], "left": ["tapB"]}, got
    assert got["afterPageshow"] == {"assigned": [], "left": ["tapB"]}, got
    assert got["afterB"] == {"assigned": [], "left": []}, got


@needs_node
def test_without_cache_storage_the_page_is_message_only() -> None:
    got = _open_rig("nocache")
    assert got["assigned"] == [CARD, CARD] and len(got["acks"]) == 2, got


def test_every_page_a_window_can_show_loads_the_open_listener() -> None:
    web = SW.parent.parent.parent / "templates" / "web"
    for page in ("dashboard.html", "sign.html", "pair.html"):
        assert '<script src="/sw_open.js"></script>' in (web / page).read_text(encoding="utf-8"), page


def _fetch(**m) -> dict:
    return _run("fetch", json.dumps(m))


@needs_node
def test_an_offline_navigation_gets_the_cached_shell_or_the_offline_page_never_a_browser_error() -> None:
    """Reproduced red on main (239660c9): the worker had no fetch listener, so a navigation with the network down was
    the browser's own error page. Now: network first; the cached shell when there is one, else the offline text."""
    shell = _fetch(url="https://box.example/", net="down", shell=True)
    assert shell["responded"] is True and shell["body"] == "<html>cached shell</html>", shell
    off = _fetch(url="https://box.example/", net="down", shell=False)
    assert off["status"] == 503 and "text/html" in off["ctype"], off
    assert "Can't reach your cockpit. Is Tailscale on?" in off["body"] and "Retry" in off["body"], off
    assert "safe-area-inset" in off["body"], off                                   # safe-area aware
    # a navigation to anything but the shell page never shows a cached dashboard in its place
    other = _fetch(url="https://box.example/sign/9?x=1", net="down", shell=True)
    assert "Can't reach your cockpit" in other["body"], other
    assert _fetch(url="https://box.example/webpush", net="down")["responded"] is False
    live = _fetch(url="https://box.example/", net="ok", shell=True)
    assert live["body"] == "<html>live</html>", live                                # online: the network wins


@needs_node
def test_the_fetch_handler_never_touches_posts_subresources_webpush_cockpit_or_a_credentialed_url() -> None:
    for m in (dict(url="https://box.example/", method="POST"), dict(url="https://box.example/app.js", mode="cors"),
              dict(url="https://box.example/webpush/subscribe", net="down"), dict(url="https://box.example/cockpit/x", net="down"),
              dict(url="https://other.example/", net="down")):
        assert _fetch(**m)["responded"] is False, m
    # only the bare shell page is ever written to the cache: no query (a credential rides there), not a 500
    for u, net in (("https://box.example/?token=sekrit", "ok"), ("https://box.example/sign/3", "ok"), ("https://box.example/", "500")):
        got = _fetch(url=u, net=net, shell=False)
        assert all(not entries for _, entries in got["shells"]), (u, got)
    ok = _fetch(url="https://box.example/", net="ok", shell=False)
    assert [e for _, es in ok["shells"] for e, _b in es] == ["/"], ok


@needs_node
def test_install_precaches_the_shell_without_skipping_waiting_and_a_failed_fetch_never_blocks_install() -> None:
    got = _run("install", "ok")
    assert got["skipWaiting"] is False, got                                          # the page asks for it, never the worker
    assert [e for _, es in got["shells"] for e, _b in es] == ["/"], got
    assert _run("install", "down")["skipWaiting"] is False
    assert all(not es for _, es in _run("install", "redirected")["shells"])         # a redirect is not a shell


SHELL_HTML = ('<link rel="stylesheet" href="/dashboard.css"><script src="/b.js?v=2"></script>')


@needs_node
def test_install_precaches_the_css_and_js_the_shell_names_and_an_offline_asset_fetch_is_answered_from_the_cache() -> None:
    """Reproduced red before the fix: only '/' was cached, so an offline cold open was an unstyled page. The asset list is
    derived from the shell's own markup (same-origin scripts and stylesheets; not /webpush/, not a cross-origin one, not an
    icon), the exact URL with its query, single- or double-quoted."""
    got = _run("install", "assets")
    entries = {e: b for _, es in got["shells"] for e, b in es}
    assert sorted(entries) == ["/", "/a.js", "/b.js?v=2", "/dashboard.css", "/q.css", "/q.js"], entries
    assert entries["/dashboard.css"] == "ASSET /dashboard.css", entries
    off = _fetch(url="https://box.example/dashboard.css", mode="no-cors", destination="style", net="down", shell=True, shellHtml=SHELL_HTML, cached="/dashboard.css")
    assert off["responded"] is True and off["body"] == "CACHED /dashboard.css", off
    miss = _fetch(url="https://box.example/other.js", mode="no-cors", destination="script", net="down", shell=True)
    assert miss["responded"] is True and miss["status"] == 0, miss                               # a miss is a network error, as without a worker
    live = _fetch(url="https://box.example/dashboard.css", mode="no-cors", destination="style", net="ok", shell=True,
                  shellHtml=SHELL_HTML, cached="/dashboard.css")
    assert live["body"] == "LIVE /dashboard.css", live                                            # online: the network wins
    assert [b for _, es in live["shells"] for e, b in es if e == "/dashboard.css"] == ["LIVE /dashboard.css"], live   # and is written through
    for m in (dict(destination="image"), dict(destination="script", method="POST"), dict(destination="script", url="https://other.example/x.js"),
              dict(destination="script", url="https://box.example/webpush/x.js")):
        got = _fetch(**{"url": "https://box.example/x.js", "mode": "no-cors", "net": "down", **m})
        assert got["responded"] is False, m


@needs_node
def test_a_query_versioned_shell_asset_is_cached_and_served_by_its_exact_url_and_any_other_query_stays_network_only() -> None:
    """N1: the install stores '/b.js?v=2' and the fetch handler must intercept that exact request (it used to refuse every
    query, so the cached copy was never served offline). A query the shell does not name is never cached."""
    off = _fetch(url="https://box.example/b.js?v=2", mode="no-cors", destination="script", net="down", shell=True, shellHtml=SHELL_HTML, cached="/b.js?v=2")
    assert off["responded"] is True and off["body"] == "CACHED /b.js?v=2", off
    on = _fetch(url="https://box.example/b.js?v=2", mode="no-cors", destination="script", net="ok", shell=True, shellHtml=SHELL_HTML)
    assert [b for _, es in on["shells"] for e, b in es if e == "/b.js?v=2"] == ["LIVE /b.js?v=2"], on
    other = _fetch(url="https://box.example/b.js?t=SECRET", mode="no-cors", destination="script", net="ok", shell=True, shellHtml=SHELL_HTML)
    assert other["body"] == "LIVE /b.js?t=SECRET" and all(e == "/" for _, es in other["shells"] for e, _b in es), other   # network only


@needs_node
def test_a_failed_or_soft_200_asset_commits_no_shell_and_a_prior_generation_is_carried_whole() -> None:
    """Reproduced red: '/' was committed before its assets, so one failed asset left a fresh shell with a missing dependency."""
    for mode in ("badasset", "softhtml"):
        got = _run("install", mode)
        assert got["skipWaiting"] is False and all(not es for _, es in got["shells"]), (mode, got)   # install succeeds, nothing cached
        prior = {n: dict(es) for n, es in _run("install", mode + ":prior")["shells"]}
        cur = [v for n, v in prior.items() if n != "levain-shell-vOLD"]
        assert cur == [{"/": "<html>old shell</html>"}], (mode, prior)   # the old '/' and exactly the assets it names (none here)
        assert _run("install", mode)["cancelled"] == 1, mode                # the rejected response's body was cancelled, not held


@needs_node
def test_content_type_gates_the_write_through_and_a_refresh_replaces_the_shell_only_with_its_assets() -> None:
    soft = _fetch(url="https://box.example/dashboard.css", mode="no-cors", destination="style", net="ok", shell=True, shellHtml=SHELL_HTML,
                  ctype="text/html")
    assert all(e == "/" for _, es in soft["shells"] for e, _b in es), soft                       # an HTML error page is never written
    js_as_css = _fetch(url="https://box.example/b.js?v=2", mode="no-cors", destination="script", net="ok", shell=True, shellHtml=SHELL_HTML,
                       ctype="text/css")
    assert all(e == "/" for _, es in js_as_css["shells"] for e, _b in es), js_as_css
    new_html = '<link rel="stylesheet" href="/dashboard.css">NEW'
    good = _fetch(url="https://box.example/", net="ok", shell=True, html=new_html)
    cached = {e: b for _, es in good["shells"] for e, b in es}
    assert cached == {"/": new_html, "/dashboard.css": "LIVE /dashboard.css"}, good               # the refresh stages its assets, then commits '/'
    bad = _fetch(url="https://box.example/", net="ok", shell=True, html=new_html, assetFail=["/dashboard.css"])
    assert {e: b for _, es in bad["shells"] for e, b in es} == {"/": "<html>cached shell</html>"}, bad   # '/' is NOT replaced


@needs_node
def test_only_the_page_message_skips_waiting() -> None:
    assert _run("message", json.dumps({"type": "levain-skip-waiting"}))["skipWaiting"] is True
    for junk in ({"type": "other"}, "levain-skip-waiting", None):
        assert _run("message", json.dumps(junk))["skipWaiting"] is False, junk


def test_no_notification_in_the_worker_carries_an_actions_key() -> None:
    """Phill 11-04: no notification action buttons, on any platform, ever. Parsed from the source, not a run."""
    import re
    src = SW.read_text()
    calls = [m.start() for m in re.finditer(r"showNotification\(", src)]
    assert calls, "no showNotification call found: the pin would pass vacuously"
    for at in calls:
        depth, i = 0, src.index("(", at)
        for j in range(i, len(src)):
            depth += (src[j] == "(") - (src[j] == ")")
            if depth == 0:
                break
        assert not re.search(r"\bactions\b", src[at:j]), src[at:j]


@needs_node
def test_activation_carries_the_last_good_shell_forward_when_the_new_install_cached_none() -> None:
    got = _run("carry")
    assert [c for c in got["caches"] if c.startswith("levain-shell")] == ["levain-shell-v1105-pwaab-1"], got   # the old shell cache is gone
    assert got["shells"][0][1] == [["/", "<html>old shell</html>"]], got          # its page was carried first


@needs_node
def test_the_carried_shell_is_the_numerically_newest_older_generation_and_a_malformed_label_falls_back_safely() -> None:
    """Versions were sorted as strings, so 0.11.9 outranked 0.11.10 and the older page was carried. Numeric runs compare as
    numbers; a label with no digits at all compares as text (after any numbered one) and never throws."""
    def carried(*labels: str) -> str:
        got = _run("carry", json.dumps(list(labels)))
        return got["shells"][0][1][0][1]
    assert carried("0.11.9", "0.11.10") == "<html>shell 0.11.10</html>"
    assert carried("0.11.10", "0.11.9") == "<html>shell 0.11.10</html>"      # input order does not matter
    assert carried("0.2.0", "0.10.0", "0.9.5") == "<html>shell 0.10.0</html>"
    assert carried("abc", "zzz") == "<html>shell zzz</html>"                  # no digits: plain text order, no throw
    assert carried("0.11.9", "garbage") == "<html>shell 0.11.9</html>"        # a numbered label outranks a malformed one


@needs_node
def test_activation_carries_only_the_old_shell_and_the_assets_it_names_each_revalidated_else_nothing() -> None:
    """Reproduced red on 61196fff: activation copied EVERY entry of the old generation (strays, a query-mismatched asset,
    an HTML body under a script key) and the old shell stayed whole regardless."""
    good = {n: dict(es) for n, es in _run("install", "badasset:good")["shells"]}
    cur = [v for n, v in good.items() if n != "levain-shell-vOLD"]
    assert cur == [{"/": '<link rel="stylesheet" href="/dashboard.css"><script src="/b.js?v=2"></script>',
                    "/dashboard.css": "OLD CSS", "/b.js?v=2": "OLD B"}], good                  # no /stray.js
    stale = {n: dict(es) for n, es in _run("install", "badasset:stale")["shells"]}
    cur = [v for n, v in stale.items() if n != "levain-shell-vOLD"]
    assert all(not v for v in cur), stale            # a missing/mismatched/soft-200 asset: a half shell is never carried


@needs_node
def test_a_commit_prunes_keys_the_new_shell_does_not_name_and_a_failed_put_restores_the_previous_generation() -> None:
    pruned = _fetch(url="https://box.example/", net="ok", shell=True, html='<link rel="stylesheet" href="/dashboard.css">N', stray=["/old.js", "/b.js?v=1"])
    assert {e for _, es in pruned["shells"] for e, _b in es} == {"/", "/dashboard.css"}, pruned
    # '/' fails to write after the asset overwrote a same-URL entry: the old asset and old '/' are put back
    html = '<link rel="stylesheet" href="/dashboard.css">N'
    got = _fetch(url="https://box.example/", net="ok", shell=True, shellHtml=SHELL_HTML, cached="/dashboard.css", html=html, putFail="/")
    assert {e: b for _, es in got["shells"] for e, b in es} == {"/": SHELL_HTML, "/dashboard.css": "CACHED /dashboard.css"}, got


@needs_node
def test_an_offline_asset_is_served_only_when_the_cached_shell_names_that_exact_key() -> None:
    named = _fetch(url="https://box.example/dashboard.css", mode="no-cors", destination="style", net="down", shell=True, shellHtml=SHELL_HTML,
                   cached="/dashboard.css")
    assert named["body"] == "CACHED /dashboard.css", named
    unnamed = _fetch(url="https://box.example/old.js", mode="no-cors", destination="script", net="down", shell=True, shellHtml=SHELL_HTML,
                     cached="/old.js")
    assert unnamed.get("body") != "CACHED /old.js", unnamed


def _whole(got: dict) -> dict:
    return {e: b for _, es in got["shells"] for e, b in es}


@needs_node
def test_two_overlapping_commits_end_with_the_second_generation_whole() -> None:
    """FIX ROUND 4 (red on b87bcce6): commit A's prune deleted the asset only commit B's '/' names."""
    a = '<script src="/a1.js"></script>A'
    b = '<script src="/a2.js"></script>B'
    got = _whole(_run("overlap", json.dumps({"shellHtml": SHELL_HTML, "runs": [{"html": a}, {"html": b}]})))
    assert got == {"/": b, "/a2.js": "LIVE /a2.js"}, got


@needs_node
def test_a_failing_commit_overlapping_a_successful_one_does_not_revert_it() -> None:
    """FIX ROUND 4 (red on b87bcce6): the failing older commit's rollback restored its snapshot over the newer commit."""
    a = '<script src="/a1.js"></script>A'
    b = '<script src="/a2.js"></script>B'
    got = _whole(_run("overlap", json.dumps({"shellHtml": SHELL_HTML, "putFail": "/", "runs": [{"html": a}, {"html": b}]})))
    assert got == {"/": b, "/a2.js": "LIVE /a2.js"}, got


@needs_node
def test_a_restore_after_a_failed_put_keeps_every_header_of_the_cached_shell() -> None:
    """FIX ROUND 4 (red on b87bcce6): the rollback rebuilt saved entries with only Content-Type, dropping the CSP."""
    csp = "default-src 'self'"
    html = '<link rel="stylesheet" href="/dashboard.css">N'
    got = _fetch(url="https://box.example/", net="ok", shell=True, shellHtml=SHELL_HTML, shellHeaders={"content-security-policy": csp},
                 cached="/dashboard.css", html=html, putFail="/")
    assert _whole(got)["/"] == SHELL_HTML and got["rootCsp"] == [csp], got


@needs_node
def test_an_offline_asset_request_answers_while_a_write_through_put_never_settles() -> None:
    """Fix round 5: the offline lookup used to join the shell-mutation chain, so one never-settling put left every offline
    script/style request pending (a hung page). Red on 39be06f7: the offline request times out."""
    got = _run("hold", json.dumps({"shellHtml": SHELL_HTML, "key": "/dashboard.css", "hang": True}))
    assert got["liveBody"] == "LIVE /dashboard.css", got
    assert got["offline"] == {"status": 200, "body": "CACHED /dashboard.css"}, got


@needs_node
def test_a_failing_asset_write_through_drains_its_copy() -> None:
    """Fix round 5: a put that throws left the tee branch unread. Red on 39be06f7: copyDrained is absent."""
    got = _run("hold", json.dumps({"shellHtml": SHELL_HTML, "key": "/dashboard.css", "putFail": True}))
    assert got["copyDrained"] is True, got
