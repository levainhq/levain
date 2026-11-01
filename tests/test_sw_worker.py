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
    assert got["caches"] == ["other-app-cache"], got                # every `levain-` cache outside CACHES_KEEP, no other name


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


def test_every_page_a_window_can_show_loads_the_open_listener() -> None:
    web = SW.parent.parent.parent / "templates" / "web"
    for page in ("dashboard.html", "sign.html", "pair.html"):
        assert '<script src="/sw_open.js"></script>' in (web / page).read_text(encoding="utf-8"), page
