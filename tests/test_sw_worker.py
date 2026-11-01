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


@needs_node
def test_a_tap_writes_the_pending_target_before_it_asks_and_the_levain_open_cache_survives_activation() -> None:
    got = _run("navigate", "controlled+ack")
    assert got["pending"]["url"] == CARD and got["nonces"] == [got["pending"]["nonce"]], got   # the message carries the nonce
    assert _run("activate")["caches"] == ["levain-open", "other-app-cache"]   # kept; every other levain- cache still goes


@needs_node
def test_a_late_ack_after_the_worker_took_over_double_navigates_nothing() -> None:
    """L1 W1: the worker timed out and navigated; the page's ack arrived after. The worker deleted the pending target, so
    the page's claim lost (no ack, no assign): exactly one navigation."""
    got = _run("navigate", "controlled+late")
    assert got["navigated"] == [CARD] and got["lateClaimed"] is False and got["pendingLeft"] == 0, got
    assert "openWindow" not in got, got


@needs_node
def test_a_refused_open_window_warns_and_nothing_throws() -> None:
    got = _run("navigate", "openfail")
    assert got["openAttempt"] == CARD, got
    assert any(k == "warn" and "openWindow" in m and "refused" in m for k, m in got["console"]), got
    assert ["info", "levain sw: tap -> openWindow-failed"] in got["console"], got


@needs_node
def test_a_navigate_that_resolves_at_the_target_with_another_fragment_counts_as_arrived() -> None:
    got = _run("navigate", "fragment")
    assert got["navigated"] == [CARD] and "openWindow" not in got, got


@needs_node
def test_the_window_preference_picks_the_cockpit_page_over_a_docs_page() -> None:
    got = _run("navigate", "prefer+ack")
    assert got["askedWho"] == ["https://box.example/sign/3"], got


@needs_node
def test_a_fallback_open_window_leaves_a_fresh_target_for_the_page_that_lands_on_root() -> None:
    got = _run("navigate", "uncontrolled")
    assert got["pendingLeft"] == 1 and got["pending"]["nonce"] != got["nonces"][0], got        # a late ack cannot claim it


@needs_node
def test_the_page_claims_by_nonce_and_only_once() -> None:
    got = _open_rig("claim")
    assert got["afterWrong"] == {"assigned": 0, "acks": 0, "pending": True}, got   # a wrong nonce: no ack, no move, entry kept
    assert got["assigned"] == [CARD] and len(got["acks"]) == 1, got                # the right nonce won once; the repeat and the gone entry did nothing
    assert got["afterRight"] == {"pending": False}, got


@needs_node
def test_a_page_that_loads_or_resumes_after_the_tap_forwards_itself_and_an_expired_target_is_dropped() -> None:
    got = _open_rig("load")
    assert got["firstLoad"] == {"assigned": [CARD], "pending": False}, got         # iOS landed on '/': the load check forwards
    assert got["expired"] == {"assigned": 1, "pending": False}, got                # older than 30 s: deleted, ignored
    assert got["alreadyHere"] == {"assigned": 1, "pending": False}, got            # consumed, no pointless assign
    assert got["hidden"] == {"assigned": 1, "pending": True}, got                  # a hidden page does not act
    assert got["visible"] == {"assigned": 2, "pending": False}, got
    assert got["cross"] == {"assigned": 2, "pending": False}, got                  # never follows another origin


@needs_node
def test_without_cache_storage_the_page_is_message_only() -> None:
    got = _open_rig("nocache")
    assert got["assigned"] == [CARD, CARD] and len(got["acks"]) == 2, got


def test_every_page_a_window_can_show_loads_the_open_listener() -> None:
    web = SW.parent.parent.parent / "templates" / "web"
    for page in ("dashboard.html", "sign.html", "pair.html"):
        assert '<script src="/sw_open.js"></script>' in (web / page).read_text(encoding="utf-8"), page
