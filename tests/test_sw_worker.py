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
    assert "message" not in got["listeners"], got                    # no token can be handed to this worker
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
    a page that never claims it (no listener) leaves nothing behind: the worker holds the click open for the 10 s window,
    then drops its own key (the rig shortens the wait and records the requested one)."""
    got = _run("navigate", "uncontrolled")
    assert got["longWaits"] == [10000], got
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
    assert got["asked"] == {"assigned": 1, "askedKept": True, "expiredNewGone": True}, got   # 'asked' is the message path's (15 s old: alive); 'new' dies at 10 s
    assert got["askedExpired"] == {"assigned": 1, "gone": True}, got               # 'asked' dies at 30 s
    assert got["alreadyHere"] == {"assigned": 1, "pending": False}, got            # consumed, no pointless assign
    assert got["unrelated"] == {"assigned": 1, "pending": True}, got               # an unrelated first load (not '/', not the target) takes nothing
    assert got["unrelatedPair"] == {"assigned": 1, "pending": True}, got
    assert got["resumed"] == {"assigned": 1, "pending": True}, got                 # visibilitychange and any pageshow claim nothing
    assert got["nineSeconds"] == {"assigned": 2, "pending": False}, got           # inside the 10 s window a root landing forwards
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
