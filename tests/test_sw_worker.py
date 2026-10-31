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


def _run(scenario: str) -> dict:
    cp = subprocess.run([NODE, str(HERE / "_sw_worker_rig.js"), str(SW), scenario], capture_output=True, text=True,
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
